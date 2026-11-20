"""Pre-flight: resource estimate and admission before any container starts (spec §3, §9.1, §11).

Runs inside `POST /v1/tasks` from GitHub API data alone (repo size, branch, `AGENTS.md`), so a
task that cannot run is rejected without cloning. Decisions [D, 2026-11-19/20]:
- per task at most 4 GB / 1 vCPU; over the limit -> reject (the user lowers the hints);
- the image must exist on an allow-listed registry with a `linux/arm64` variant (arm64 host);
- host capacity is 2 concurrent tasks, 8 GB, 2 vCPU; demand = running + already-queued tasks
  (FIFO, so a small task never overtakes a waiting one); no room -> queue with its position;
- the token must have push access (the agent ends by opening a PR).

`evaluate` is pure (capacity and the image check are inputs) so every rule is unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from swf.config import AGENTS_FILENAME, AgentsFileError, Resources, parse_agents_md
from swf.github import GitHubClient, GitHubFileTooLarge, GitHubNotFound
from swf.images import (
    ImageError,
    ImageNotFound,
    InvalidImageRef,
    RegistryClient,
    RegistryNotAllowed,
)

_CPU_EPSILON = 1e-9


@dataclass(frozen=True)
class Limits:
    task_memory_mb: int = 4096
    task_cpus: float = 1.0
    host_memory_mb: int = 8192
    host_cpus: float = 2.0
    max_concurrent: int = 2
    max_repo_mb: int = 1024
    time_limit_minutes: int = 60
    default_image: str = "python:3.12-slim"


@dataclass(frozen=True)
class Reservation:
    memory_mb: int
    cpus: float


@dataclass(frozen=True)
class Capacity:
    active: tuple[Reservation, ...]
    queued: tuple[Reservation, ...]  # in FIFO order


@dataclass(frozen=True)
class RepoFacts:
    size_kb: int
    agents_md: str | None
    agents_md_error: str | None = None  # AGENTS.md exists but cannot be read (e.g. > 1 MiB)


class PreflightWarning(BaseModel):
    code: str
    message: str


class PreflightResult(BaseModel):
    decision: Literal["admit", "queue", "reject"]
    image: str
    memory_mb: int
    cpus: float
    time_limit_minutes: int
    queue_position: int | None
    warnings: list[PreflightWarning]


class PreflightInputError(Exception):
    """The request names something that does not exist (shown to the user as a 422)."""


class RepoNotFound(PreflightInputError):
    pass


class BranchNotFound(PreflightInputError):
    pass


class RepoNotWritable(PreflightInputError):
    """The token cannot push, so the agent could never open its PR."""


def _fmt_cpus(cpus: float) -> str:
    return f"{cpus:g}"


def _image_warning(image: str, image_check: Callable[[str], bool]) -> PreflightWarning | None:
    try:
        if image_check(image):
            return None
        return PreflightWarning(
            code="image_not_arm64", message=f"Image {image} has no linux/arm64 variant."
        )
    except ImageNotFound as exc:
        return PreflightWarning(code="image_not_found", message=str(exc))
    except RegistryNotAllowed as exc:
        return PreflightWarning(code="registry_not_allowed", message=str(exc))
    except InvalidImageRef as exc:
        return PreflightWarning(code="image_invalid", message=str(exc))
    except ImageError as exc:
        return PreflightWarning(code="registry_error", message=str(exc))


def fits(
    active: Sequence[Reservation],
    r: Reservation,
    limits: Limits = Limits(),  # noqa: B008 - frozen dataclass, safe as a default
) -> bool:
    """True if `r` can start next to `active`: a free slot, and memory and CPU left on the host.

    `evaluate` (advisory, at creation) and `store.claim_next_task` (authoritative) share it.
    """
    used_mb = sum(x.memory_mb for x in active)
    used_cpus = sum(x.cpus for x in active)
    return (
        len(active) < limits.max_concurrent
        and used_mb + r.memory_mb <= limits.host_memory_mb
        and used_cpus + r.cpus <= limits.host_cpus + _CPU_EPSILON
    )


def evaluate(
    facts: RepoFacts,
    capacity: Capacity,
    image_check: Callable[[str], bool],
    limits: Limits = Limits(),  # noqa: B008 - frozen dataclass, safe as a default
) -> PreflightResult:
    defaults = Resources()

    def result(
        decision: Literal["admit", "queue", "reject"],
        image: str,
        memory_mb: int,
        cpus: float,
        warnings: list[PreflightWarning],
        queue_position: int | None = None,
    ) -> PreflightResult:
        return PreflightResult(
            decision=decision,
            image=image,
            memory_mb=memory_mb,
            cpus=cpus,
            time_limit_minutes=limits.time_limit_minutes,
            queue_position=queue_position,
            warnings=warnings,
        )

    if facts.agents_md_error is not None:
        unreadable = PreflightWarning(code="agents_md_invalid", message=facts.agents_md_error)
        return result(
            "reject", limits.default_image, defaults.memory_mb, defaults.cpus, [unreadable]
        )
    warnings: list[PreflightWarning] = []
    if facts.agents_md is None:
        image, memory_mb, cpus = limits.default_image, defaults.memory_mb, defaults.cpus
        warnings.append(
            PreflightWarning(
                code="agents_md_missing",
                message=(
                    f"No {AGENTS_FILENAME} in the repository root; using image {image} and "
                    f"{memory_mb} MB / {_fmt_cpus(cpus)} vCPU."
                ),
            )
        )
    else:
        try:
            config = parse_agents_md(facts.agents_md).config
        except AgentsFileError as exc:
            invalid = PreflightWarning(code="agents_md_invalid", message=str(exc))
            return result(
                "reject", limits.default_image, defaults.memory_mb, defaults.cpus, [invalid]
            )
        image = config.image
        memory_mb, cpus = config.resources.memory_mb, config.resources.cpus

    rejects: list[PreflightWarning] = []
    over: list[str] = []
    if memory_mb > limits.task_memory_mb:
        over.append(f"{memory_mb} MB requested, limit {limits.task_memory_mb} MB")
    if cpus > limits.task_cpus + _CPU_EPSILON:
        over.append(f"{_fmt_cpus(cpus)} vCPU requested, limit {_fmt_cpus(limits.task_cpus)} vCPU")
    if over:
        rejects.append(
            PreflightWarning(
                code="over_task_limit",
                message="Over the per-task limit: " + "; ".join(over) + ".",
            )
        )
    if facts.size_kb > limits.max_repo_mb * 1024:
        rejects.append(
            PreflightWarning(
                code="repo_too_large",
                message=(
                    f"Repository is {facts.size_kb // 1024} MB, limit {limits.max_repo_mb} MB."
                ),
            )
        )
    image_problem = _image_warning(image, image_check)
    if image_problem is not None:
        rejects.append(image_problem)
    if rejects:
        return result("reject", image, memory_mb, cpus, warnings + rejects)

    # FIFO: tasks already waiting run before this one, so they count as demand too.
    demand = capacity.active + capacity.queued
    if fits(demand, Reservation(memory_mb, cpus), limits):
        return result("admit", image, memory_mb, cpus, warnings)
    position = max(1, len(demand) + 1 - limits.max_concurrent)
    busy = PreflightWarning(
        code="host_busy",
        message=f"The host is busy; the task is queued at position {position}.",
    )
    return result("queue", image, memory_mb, cpus, [*warnings, busy], queue_position=position)


class PreflightService:
    """Pre-flight over the GitHub API and the image registry (no clone, no container)."""

    def __init__(
        self,
        github: GitHubClient,
        registry: RegistryClient,
        limits: Limits = Limits(),  # noqa: B008 - frozen dataclass, safe as a default
        platform: str = "linux/arm64",
    ) -> None:
        self.github = github
        self.registry = registry
        self.limits = limits
        self.platform = platform

    def check(self, token: str, repo: str, branch: str, capacity: Capacity) -> PreflightResult:
        """`GitHubAuthError` / other `GitHubError` propagate; the API maps them."""
        try:
            meta = self.github.get_repo(token, repo)
        except GitHubNotFound as exc:
            raise RepoNotFound(f"repository {repo} not found or not accessible") from exc
        if not meta.can_push:
            raise RepoNotWritable(f"no push access to {repo}")
        if not self.github.branch_exists(token, repo, branch):
            raise BranchNotFound(f"branch {branch} not found in {repo}")
        text: str | None = None
        error: str | None = None
        try:
            text = self.github.get_file(token, repo, AGENTS_FILENAME, ref=branch)
        except GitHubFileTooLarge as exc:
            error = str(exc)
        return evaluate(
            RepoFacts(size_kb=meta.size_kb, agents_md=text, agents_md_error=error),
            capacity,
            lambda image: self.registry.supports(image, self.platform),
            self.limits,
        )
