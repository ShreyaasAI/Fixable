"""Pre-flight resource estimate and admission (spec §3, §9.1, §11; plan M2).

Rules [D, 2026-11-19]:
- per task at most 4 GB / 1 vCPU; host capacity 2 tasks, 8 GB, 2 vCPU;
- over the per-task limit, no arm64 image, or an invalid AGENTS.md -> reject before any container;
- admission counts active AND waiting tasks (FIFO): no room for the new task -> queue, with
  position max(1, len(active + queued) + 1 - max_concurrent).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from helpers import FakeGitHub, FakeRegistry, agents_md, index_manifest

from swf.github import GitHubAuthError, GitHubClient
from swf.images import ImageError, ImageNotFound, RegistryClient, RegistryNotAllowed
from swf.preflight import (
    BranchNotFound,
    Capacity,
    Limits,
    PreflightInputError,
    PreflightService,
    RepoFacts,
    RepoNotFound,
    RepoNotWritable,
    Reservation,
    evaluate,
)

SEED_LIKE = agents_md(memory_mb=512, cpus=0.5)
IDLE = Capacity(active=(), queued=())


def arm64_ok(_image: str) -> bool:
    return True


def arm64_missing(_image: str) -> bool:
    return False


def raises(exc: ImageError) -> Callable[[str], bool]:
    def check(_image: str) -> bool:
        raise exc

    return check


def codes(result: object) -> list[str]:
    return [w.code for w in result.warnings]  # type: ignore[attr-defined]


def test_admit_with_hints() -> None:
    r = evaluate(RepoFacts(size_kb=100, agents_md=SEED_LIKE), IDLE, arm64_ok)
    assert r.decision == "admit"
    assert (r.image, r.memory_mb, r.cpus) == ("python:3.12-slim", 512, 0.5)
    assert r.time_limit_minutes == Limits().time_limit_minutes
    assert r.queue_position is None
    assert r.warnings == []


def test_missing_agents_md_uses_defaults_and_warns() -> None:
    r = evaluate(RepoFacts(size_kb=100, agents_md=None), IDLE, arm64_ok)
    assert r.decision == "admit"
    assert (r.image, r.memory_mb, r.cpus) == ("python:3.12-slim", 2048, 1.0)
    assert codes(r) == ["agents_md_missing"]


@pytest.mark.parametrize(
    "text",
    [
        "# no config block here\n",
        "```yaml agent-config\nimage: [unclosed\n```\n",
        "```yaml agent-config\ntest: pytest\n```\n",  # image missing
    ],
)
def test_invalid_agents_md_rejected_without_image_check(text: str) -> None:
    called: list[str] = []

    def check(image: str) -> bool:
        called.append(image)
        return True

    r = evaluate(RepoFacts(size_kb=100, agents_md=text), IDLE, check)
    assert r.decision == "reject"
    assert codes(r) == ["agents_md_invalid"]
    assert called == []


def test_memory_over_task_limit() -> None:
    r = evaluate(RepoFacts(size_kb=1, agents_md=agents_md(memory_mb=8192)), IDLE, arm64_ok)
    assert r.decision == "reject"
    assert codes(r) == ["over_task_limit"]
    assert "8192" in r.warnings[0].message and "4096" in r.warnings[0].message


def test_cpu_over_task_limit() -> None:
    r = evaluate(RepoFacts(size_kb=1, agents_md=agents_md(cpus=2)), IDLE, arm64_ok)
    assert r.decision == "reject"
    assert codes(r) == ["over_task_limit"]


def test_exactly_at_limit_is_admitted() -> None:
    text = agents_md(memory_mb=4096, cpus=1)
    assert evaluate(RepoFacts(size_kb=1, agents_md=text), IDLE, arm64_ok).decision == "admit"


def test_repo_too_large() -> None:
    r = evaluate(RepoFacts(size_kb=2 * 1024 * 1024, agents_md=SEED_LIKE), IDLE, arm64_ok)
    assert r.decision == "reject"
    assert codes(r) == ["repo_too_large"]


@pytest.mark.parametrize(
    ("check", "code"),
    [
        (arm64_missing, "image_not_arm64"),
        (raises(ImageNotFound("nope")), "image_not_found"),
        (raises(RegistryNotAllowed("evil.example")), "registry_not_allowed"),
        (raises(ImageError("boom")), "registry_error"),
    ],
)
def test_image_problems_reject(check: Callable[[str], bool], code: str) -> None:
    r = evaluate(RepoFacts(size_kb=1, agents_md=SEED_LIKE), IDLE, check)
    assert r.decision == "reject"
    assert codes(r) == [code]


def test_all_reject_reasons_reported() -> None:
    text = agents_md(memory_mb=9000, cpus=4)
    r = evaluate(RepoFacts(size_kb=5 * 1024 * 1024, agents_md=text), IDLE, arm64_missing)
    assert r.decision == "reject"
    assert sorted(set(codes(r))) == ["image_not_arm64", "over_task_limit", "repo_too_large"]


def test_reject_ignores_capacity() -> None:
    busy = Capacity(
        active=(Reservation(4096, 1.0), Reservation(4096, 1.0)),
        queued=(Reservation(512, 0.5),) * 5,
    )
    r = evaluate(RepoFacts(size_kb=1, agents_md=agents_md(memory_mb=9000)), busy, arm64_ok)
    assert r.decision == "reject"
    assert r.queue_position is None
    assert "host_busy" not in codes(r)


SMALL = Reservation(512, 0.5)
BIG = Reservation(4096, 1.0)


@pytest.mark.parametrize(
    ("capacity", "decision", "position"),
    [
        (Capacity(active=(), queued=()), "admit", None),
        (Capacity(active=(BIG,), queued=()), "admit", None),
        (Capacity(active=(), queued=(SMALL,)), "admit", None),  # a waiting task still leaves room
        (Capacity(active=(SMALL,), queued=()), "admit", None),
        (Capacity(active=(SMALL, SMALL), queued=()), "queue", 1),
        (Capacity(active=(BIG, BIG), queued=()), "queue", 1),
        (Capacity(active=(SMALL,), queued=(SMALL,)), "queue", 1),  # two tasks already in demand
        (Capacity(active=(), queued=(SMALL, SMALL)), "queue", 1),
        (Capacity(active=(), queued=(SMALL, SMALL, SMALL)), "queue", 2),
        (Capacity(active=(SMALL, SMALL), queued=(SMALL,)), "queue", 2),
        (Capacity(active=(SMALL, SMALL), queued=(SMALL, SMALL)), "queue", 3),
        (Capacity(active=(Reservation(7900, 0.5),), queued=()), "queue", 1),  # memory full
        (Capacity(active=(Reservation(512, 1.75),), queued=()), "queue", 1),  # cpu full
        (Capacity(active=(), queued=(Reservation(7900, 0.5),)), "queue", 1),  # queued memory counts
        (Capacity(active=(), queued=(Reservation(512, 1.75),)), "queue", 1),  # queued cpu counts
    ],
)
def test_capacity(capacity: Capacity, decision: str, position: int | None) -> None:
    r = evaluate(RepoFacts(size_kb=1, agents_md=SEED_LIKE), capacity, arm64_ok)
    assert r.decision == decision
    assert r.queue_position == position
    assert ("host_busy" in codes(r)) == (decision == "queue")


def test_exact_fit_on_memory_and_cpu_is_admitted() -> None:
    # 7680 + 512 = 8192 MB and 1.5 + 0.5 = 2.0 vCPU: exactly the host, so it still fits.
    capacity = Capacity(active=(Reservation(7680, 1.5),), queued=())
    r = evaluate(RepoFacts(size_kb=1, agents_md=SEED_LIKE), capacity, arm64_ok)
    assert r.decision == "admit"


def test_custom_limits_change_admission() -> None:
    limits = Limits(max_concurrent=1)
    capacity = Capacity(active=(SMALL,), queued=())
    r = evaluate(RepoFacts(size_kb=1, agents_md=SEED_LIKE), capacity, arm64_ok, limits)
    assert (r.decision, r.queue_position) == ("queue", 1)


def test_oversized_agents_md_is_rejected_with_the_reader_message() -> None:
    called: list[str] = []

    def check(image: str) -> bool:
        called.append(image)
        return True

    message = "AGENTS.md is larger than 1 MiB"
    r = evaluate(RepoFacts(size_kb=1, agents_md=None, agents_md_error=message), IDLE, check)
    assert r.decision == "reject"
    assert codes(r) == ["agents_md_invalid"]
    assert r.warnings[0].message == message
    assert (r.image, r.memory_mb, r.cpus) == ("python:3.12-slim", 2048, 1.0)
    assert called == []


def test_result_is_json_serialisable() -> None:
    r = evaluate(RepoFacts(size_kb=1, agents_md=SEED_LIKE), IDLE, arm64_ok)
    data = r.model_dump(mode="json")
    assert data["decision"] == "admit"
    assert type(r).model_validate(data) == r


# --- PreflightService over the GitHub API and the registry (mocked) -------------------------

REPO = "octo/widgets"
HUB = "registry-1.docker.io"


def _service(
    gh: FakeGitHub | None = None, reg: FakeRegistry | None = None
) -> tuple[PreflightService, FakeGitHub, FakeRegistry]:
    gh = gh or FakeGitHub(users={"tok": (1, "octo")})
    if reg is None:
        reg = FakeRegistry()
        reg.manifests[(HUB, "library/python", "3.12-slim")] = index_manifest(
            "linux/amd64", "linux/arm64/v8"
        )
    github = GitHubClient(base_url="https://api.github.com", transport=gh.transport())
    service = PreflightService(github, RegistryClient(transport=reg.transport()))
    return service, gh, reg


def test_service_admits_a_healthy_repo() -> None:
    service, gh, _ = _service()
    gh.repo_host.agents[REPO] = SEED_LIKE
    r = service.check("tok", REPO, "main", IDLE)
    assert (r.decision, r.image, r.memory_mb, r.cpus) == ("admit", "python:3.12-slim", 512, 0.5)
    paths = [req.url.path for req in gh.requests]
    assert f"/repos/{REPO}/contents/AGENTS.md" in paths
    contents = next(req for req in gh.requests if req.url.path.endswith("/AGENTS.md"))
    assert contents.url.params["ref"] == "main"


def test_service_reports_the_registry_platform() -> None:
    reg = FakeRegistry()
    reg.manifests[(HUB, "library/python", "3.12-slim")] = index_manifest("linux/amd64")
    service, gh, _ = _service(reg=reg)
    gh.repo_host.agents[REPO] = SEED_LIKE
    r = service.check("tok", REPO, "main", IDLE)
    assert r.decision == "reject"
    assert codes(r) == ["image_not_arm64"]


def test_service_missing_agents_md_uses_defaults() -> None:
    service, gh, _ = _service()
    gh.repo_host.agents[REPO] = None
    r = service.check("tok", REPO, "main", IDLE)
    assert codes(r) == ["agents_md_missing"]
    assert r.decision == "admit"


def test_service_large_repo_is_rejected() -> None:
    service, gh, _ = _service()
    gh.repo_host.agents[REPO] = SEED_LIKE
    gh.repo_host.sizes_kb[REPO] = 2 * 1024 * 1024
    assert codes(service.check("tok", REPO, "main", IDLE)) == ["repo_too_large"]


def test_service_oversized_agents_md_is_a_rejection_not_an_error() -> None:
    service, gh, _ = _service()
    gh.repo_host.agents[REPO] = "x" * (1024 * 1024 + 1)
    r = service.check("tok", REPO, "main", IDLE)
    assert r.decision == "reject"
    assert codes(r) == ["agents_md_invalid"]


def test_service_unknown_repo() -> None:
    service, gh, _ = _service()
    gh.repo_host.missing_repos.add(REPO)
    with pytest.raises(RepoNotFound):
        service.check("tok", REPO, "main", IDLE)


def test_service_unknown_branch() -> None:
    service, gh, _ = _service()
    gh.repo_host.missing_branches.add("nope")
    with pytest.raises(BranchNotFound):
        service.check("tok", REPO, "nope", IDLE)


def test_service_repo_without_push_access() -> None:
    service, gh, _ = _service()
    gh.repo_host.readonly_repos.add(REPO)
    with pytest.raises(RepoNotWritable):
        service.check("tok", REPO, "main", IDLE)


def test_input_errors_share_a_base_class() -> None:
    for exc in (RepoNotFound, BranchNotFound, RepoNotWritable):
        assert issubclass(exc, PreflightInputError)


def test_service_bad_token_propagates() -> None:
    service, _, _ = _service()
    with pytest.raises(GitHubAuthError):
        service.check("wrong", REPO, "main", IDLE)
