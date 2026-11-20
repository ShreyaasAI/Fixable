"""Hardened per-task container on top of mini-swe-agent's `DockerEnvironment` (plan M2).

The agent's shell runs here, so the container holds nothing worth stealing and cannot reach
anything but the egress proxy:
- non-root uid, all capabilities dropped, `no-new-privileges`, memory (no extra swap), CPU and
  pids caps, an init process to reap zombies;
- only the task workspace is mounted (at `/repo`); never the Docker socket, never host env;
- attached only to its own internal task network (`swf.egress.attach_task_network`), with DNS
  pointed at a dead address (`127.0.0.1`), so the only way out is the proxy, addressed by IP;
  Squid resolves names. Other tasks are on other networks and unreachable.

`SandboxSpec` re-checks the image registry allowlist and the per-task limits at execution time
(defence in depth behind pre-flight). Commands run with `bash -c`, not upstream's `bash -lc`: a
login shell resets PATH and breaks venv/Go/Rust images.
Upstream `cleanup()` backgrounds a POSIX shell line that breaks under cmd.exe and returns before
the container is gone, so `SandboxEnv.cleanup()` removes the container synchronously.
Every sandbox and task network carries `swf.managed=1` + `swf.task=<id>` labels so
`cleanup_orphans` can remove what a crashed worker left behind.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Collection
from pathlib import Path
from typing import Any

from minisweagent.environments.docker import DockerEnvironment
from pydantic import BaseModel, ConfigDict, Field, field_validator

from swf.egress import LABEL_MANAGED as LABEL_MANAGED
from swf.egress import LABEL_TASK as LABEL_TASK
from swf.egress import EgressConfig, remove_network
from swf.images import ALLOWED_REGISTRIES, InvalidImageRef, parse_image_ref
from swf.preflight import Limits

WORKSPACE_TARGET = "/repo"
_RM_TIMEOUT_SECONDS = 60
_LIMITS = Limits()


class SandboxSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    image: str = Field(min_length=1)
    workspace: Path
    proxy_url: str = Field(min_length=1)
    network: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    memory_mb: int = Field(default=4096, gt=0, le=_LIMITS.task_memory_mb)
    cpus: float = Field(default=1.0, gt=0, le=_LIMITS.task_cpus)
    pids_limit: int = Field(default=512, gt=0)
    user: str = "1000:1000"
    runtime: str | None = None
    command_timeout: int = 300
    container_timeout: str = "2h"

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        try:
            ref = parse_image_ref(v)
        except InvalidImageRef as exc:
            raise ValueError(str(exc)) from exc
        if ref.registry not in ALLOWED_REGISTRIES:
            raise ValueError(f"registry {ref.registry} is not allowed")
        return v

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("workspace must be an absolute path")
        if not v.is_dir():
            raise ValueError("workspace must be an existing directory")
        # `--mount` is a comma-separated key=value list; these would let a path inject options.
        if any(c in str(v) for c in ',"\n\r'):
            raise ValueError("workspace path must not contain commas, quotes or newlines")
        return v


def sandbox_run_args(spec: SandboxSpec) -> list[str]:
    """`docker run` options for a sandbox (upstream adds `-d --name -w`, image and command)."""
    env = {
        "HOME": "/tmp/home",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "HTTP_PROXY": spec.proxy_url,
        "HTTPS_PROXY": spec.proxy_url,
        "http_proxy": spec.proxy_url,
        "https_proxy": spec.proxy_url,
        "NO_PROXY": "",
        "no_proxy": "",
    }
    args = [
        "--rm",
        "--init",
        "--label", f"{LABEL_MANAGED}=1",
        "--label", f"{LABEL_TASK}={spec.task_id}",
        "--network", spec.network,
        "--dns", "127.0.0.1",
        "--memory", f"{spec.memory_mb}m",
        "--memory-swap", f"{spec.memory_mb}m",
        "--cpus", f"{spec.cpus}",
        "--pids-limit", f"{spec.pids_limit}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", spec.user,
        "--mount", f"type=bind,source={spec.workspace},target={WORKSPACE_TARGET}",
    ]  # fmt: skip
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    if spec.runtime:
        args += ["--runtime", spec.runtime]
    return args


class SandboxEnv(DockerEnvironment):
    """A started sandbox container; `execute()` comes from upstream unchanged."""

    def __init__(self, spec: SandboxSpec, **kwargs: Any) -> None:
        self.spec = spec  # before super(): upstream __init__ starts the container
        super().__init__(
            image=spec.image,
            cwd=WORKSPACE_TARGET,
            run_args=sandbox_run_args(spec),
            env={},
            forward_env=[],
            timeout=spec.command_timeout,
            container_timeout=spec.container_timeout,
            interpreter=["bash", "-c"],
            **kwargs,
        )

    def cleanup(self) -> None:
        """Remove the container now (blocking); safe to call twice or after a failed start."""
        container_id: str | None = getattr(self, "container_id", None)
        if container_id is None:
            return
        config = getattr(self, "config", None)
        executable = str(getattr(config, "executable", "docker"))
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [executable, "rm", "-f", container_id],
                capture_output=True,
                timeout=_RM_TIMEOUT_SECONDS,
                check=False,
            )
        self.container_id = None


def _managed(docker: str, kind: list[str], name_field: str) -> list[tuple[str, str]]:
    """(id-or-name, task id) of `swf.managed=1` objects; `kind` is e.g. ["ps", "-a"]."""
    listing = subprocess.run(
        [
            docker, *kind,
            "--filter", f"label={LABEL_MANAGED}=1",
            "--format", "{{" + name_field + '}}\\t{{.Label "' + LABEL_TASK + '"}}',
        ],
        capture_output=True, text=True, timeout=_RM_TIMEOUT_SECONDS, check=True,
    )  # fmt: skip
    found = []
    for line in listing.stdout.splitlines():
        ident, _, task_id = line.strip().partition("\t")
        if ident:
            found.append((ident, task_id))
    return found


def cleanup_orphans(
    active_task_ids: Collection[str],
    *,
    egress: EgressConfig | None = None,
    docker: str = "docker",
) -> list[str]:
    """Remove managed sandboxes, then task networks, whose task is not active.

    Returns the removed container ids followed by the removed network names. With `egress`,
    the proxy is disconnected from a network before it is removed (else removal fails).
    """
    active = set(active_task_ids)
    removed: list[str] = []
    for container_id, task_id in _managed(docker, ["ps", "-a"], ".ID"):
        if task_id in active:
            continue
        rm = subprocess.run(
            [docker, "rm", "-f", container_id],
            capture_output=True, text=True, timeout=_RM_TIMEOUT_SECONDS, check=False,
        )  # fmt: skip
        if rm.returncode == 0:
            removed.append(container_id)
    for network, task_id in _managed(docker, ["network", "ls"], ".Name"):
        if task_id not in active and remove_network(egress, network, docker=docker):
            removed.append(network)
    return removed
