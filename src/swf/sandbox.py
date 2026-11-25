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
Every sandbox and task network carries `swf.managed=1` + `swf.task=<id>` labels (and
`swf.worker=<id>` when a worker started it) so `cleanup_orphans` can remove what a crashed
worker left behind.

M4: `SandboxEnv` is also the agent's `ToolEnv`. File tools run in the container through
`docker exec -i` with an argv list and stdin (`exec_argv`): no shell string, so no quoting bugs,
no Windows command-line cap, and paths resolve inside the container (no host-side symlink
escape). `_check_finished` is a no-op: only the backend `complete_task` tool ends a task, so
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` cannot forge a submission.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from minisweagent.environments.docker import DockerEnvironment
from pydantic import BaseModel, ConfigDict, Field, field_validator

from swf.agent.tools import ToolError
from swf.egress import LABEL_MANAGED as LABEL_MANAGED
from swf.egress import LABEL_TASK as LABEL_TASK
from swf.egress import LABEL_WORKER as LABEL_WORKER
from swf.egress import EgressConfig, attach_task_network, detach_task_network, remove_network
from swf.images import ALLOWED_REGISTRIES, InvalidImageRef, parse_image_ref
from swf.preflight import Limits

if TYPE_CHECKING:
    from swf.worker import EnvRequest

WORKSPACE_TARGET = "/repo"
_RM_TIMEOUT_SECONDS = 60
_LIMITS = Limits()
RUNTIMES = frozenset({"runc", "runsc"})
Runtime = Literal["runc", "runsc"]


class SandboxRuntimeError(Exception):
    pass


def runtime_from_env(env: Mapping[str, str] | None = None) -> Runtime | None:
    """`SWF_SANDBOX_RUNTIME`: blank or unset -> None (Docker's default); else runc|runsc."""
    e = os.environ if env is None else env
    raw = (e.get("SWF_SANDBOX_RUNTIME") or "").strip()
    if not raw:
        return None
    if raw not in RUNTIMES:
        raise ValueError(f"SWF_SANDBOX_RUNTIME must be one of {sorted(RUNTIMES)}, got {raw!r}")
    return cast(Runtime, raw)


def parse_runtimes(info_json: str) -> frozenset[str]:
    """Runtime names from `docker info --format '{{json .Runtimes}}'`."""
    return frozenset(json.loads(info_json))


def docker_runtimes(*, docker: str = "docker") -> frozenset[str]:
    """The runtimes the Docker daemon lists; empty when Docker cannot be asked."""
    try:
        info = subprocess.run(
            [docker, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    try:
        return parse_runtimes(info.stdout.strip()) if info.returncode == 0 else frozenset()
    except ValueError:
        return frozenset()


def check_runtime(runtime: str, *, docker: str = "docker") -> None:
    """Raise SandboxRuntimeError unless the Docker daemon lists `runtime`."""
    if runtime not in docker_runtimes(docker=docker):
        raise SandboxRuntimeError(f"docker runtime {runtime!r} not available")


_USER_PATTERN = r"^[1-9][0-9]*:[1-9][0-9]*$"  # ASCII digits only
_USER_RE = re.compile(_USER_PATTERN)
_HOST_IDS: tuple[int, int] | None = None
if sys.platform != "win32":
    _HOST_IDS = (os.getuid(), os.getgid())


class SandboxUserError(Exception):
    pass


def sandbox_user(
    env: Mapping[str, str] | None = None, host_ids: tuple[int, int] | None = _HOST_IDS
) -> str:
    """uid:gid the sandbox runs as: the host user on POSIX, 1000:1000 on Windows (m9 §0.7)."""
    raw = (os.environ if env is None else env).get("SWF_SANDBOX_USER", "").strip()
    if raw:
        if not _USER_RE.fullmatch(raw):
            raise ValueError("SWF_SANDBOX_USER must be a non-root uid:gid")
        return raw
    if host_ids is None:
        return "1000:1000"
    uid, gid = host_ids
    if uid == 0 or gid == 0:
        raise SandboxUserError("refusing to run sandboxes as root; set SWF_SANDBOX_USER")
    return f"{uid}:{gid}"


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
    user: str = Field(default_factory=sandbox_user, pattern=_USER_PATTERN)
    index_url: str | None = Field(
        default=None, pattern=r"^http://[0-9]{1,3}(\.[0-9]{1,3}){3}:[0-9]{1,5}/simple/$"
    )
    runtime: Runtime | None = Field(default_factory=runtime_from_env)
    command_timeout: int = 300
    container_timeout: str = "2h"
    worker_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")

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

    @field_validator("index_url")
    @classmethod
    def _index_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        host, port = v.removeprefix("http://").removesuffix("/simple/").split(":")
        ip = ipaddress.IPv4Address(host)  # ValueError on octets > 255
        if not ip.is_private or ip.is_loopback:
            raise ValueError("index_url host must be a private, non-loopback IPv4 address")
        if not 1 <= int(port) <= 65535:
            raise ValueError("index_url port must be 1-65535")
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
    if spec.index_url is not None:
        host = spec.index_url.removeprefix("http://").split(":", 1)[0]
        env |= {
            "PIP_INDEX_URL": spec.index_url,
            "PIP_TRUSTED_HOST": host,
            "NO_PROXY": host,
            "no_proxy": host,
        }
    args = [
        "--rm",
        "--init",
        "--label", f"{LABEL_MANAGED}=1",
        "--label", f"{LABEL_TASK}={spec.task_id}",
        *([] if spec.worker_id is None else ["--label", f"{LABEL_WORKER}={spec.worker_id}"]),
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


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _stderr_text(result: ExecResult) -> str:
    text = result.stderr.decode("utf-8", "replace").strip()
    return text or f"command failed with exit code {result.returncode}"


class SandboxEnv(DockerEnvironment):
    """A started sandbox container; `execute()` (bash) comes from upstream unchanged."""

    def __init__(self, spec: SandboxSpec, **kwargs: Any) -> None:
        self.spec = spec  # before super(): upstream __init__ starts the container
        self.on_cleanup: Callable[[], None] | None = None
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

    def _check_finished(self, output: dict[str, Any]) -> None:
        """No-op: only `complete_task` finishes a task (m4-contract §0.7)."""

    def _executable(self) -> str:
        return str(getattr(getattr(self, "config", None), "executable", "docker"))

    def exec_argv(
        self, argv: Sequence[str], *, stdin: bytes | None = None, timeout: int | None = None
    ) -> ExecResult:
        """`docker exec -i -w /repo <id> <argv...>`: no shell, input via stdin."""
        container_id = getattr(self, "container_id", None)
        if container_id is None:
            raise ToolError("sandbox is not running")
        cmd = [self._executable(), "exec", "-i", "-w", WORKSPACE_TARGET, container_id, *argv]
        try:
            run = subprocess.run(
                cmd,
                input=stdin if stdin is not None else b"",
                capture_output=True,
                timeout=timeout or self.spec.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"timed out after {exc.timeout} seconds") from exc
        except OSError as exc:
            raise ToolError(f"cannot run docker exec: {exc}") from exc
        return ExecResult(run.returncode, run.stdout, run.stderr)

    def read_bytes(self, path: str) -> bytes:
        result = self.exec_argv(["cat", "--", path])
        if result.returncode != 0:
            raise ToolError(_stderr_text(result))
        return result.stdout

    def write_bytes(self, path: str, data: bytes) -> None:
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        result = self.exec_argv(["sh", "-c", script, "sh", path], stdin=data)
        if result.returncode != 0:
            raise ToolError(_stderr_text(result))

    def list_dir(self, path: str) -> list[str]:
        result = self.exec_argv(["ls", "-1Ap", "--", path])
        if result.returncode != 0:
            raise ToolError(_stderr_text(result))
        return [n for n in result.stdout.decode("utf-8", "replace").splitlines() if n]

    def search(self, pattern: str, path: str, glob: str | None, max_results: int) -> list[str]:
        argv = ["grep", "-rnE", "-m", str(max_results)]
        if glob:
            argv.append(f"--include={glob}")
        argv += ["--", pattern, path]
        result = self.exec_argv(argv)
        if result.returncode == 1:  # no match
            return []
        if result.returncode != 0:
            raise ToolError(_stderr_text(result))
        lines = result.stdout.decode("utf-8", "replace").splitlines()
        return lines[:max_results]

    def cleanup(self) -> None:
        """Remove the container now (blocking); safe to call twice or after a failed start.

        Then runs `on_cleanup` once (`open_task_sandbox` sets it to detach the task network).
        """
        container_id: str | None = getattr(self, "container_id", None)
        if container_id is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [self._executable(), "rm", "-f", container_id],
                    capture_output=True,
                    timeout=_RM_TIMEOUT_SECONDS,
                    check=False,
                )
            self.container_id = None
        hook = getattr(self, "on_cleanup", None)
        self.on_cleanup = None
        if hook is not None:
            hook()


def open_task_sandbox(egress: EgressConfig, req: EnvRequest) -> SandboxEnv:
    """Task network + sandbox; `cleanup()` removes both. A failed start detaches the network."""
    worker_id = getattr(req, "worker_id", None)
    net = attach_task_network(egress, req.task_id, worker_id=worker_id)
    try:
        env = SandboxEnv(
            SandboxSpec(
                task_id=req.task_id,
                image=req.image,
                workspace=req.workspace,
                proxy_url=net.proxy_url,
                network=net.name,
                memory_mb=req.memory_mb,
                cpus=req.cpus,
                worker_id=worker_id,
                index_url=net.index_url,
            )
        )
    except BaseException:
        with contextlib.suppress(Exception):
            detach_task_network(egress, req.task_id)
        raise
    env.on_cleanup = lambda: detach_task_network(egress, req.task_id)
    return env


def _managed(docker: str, kind: list[str], name_field: str) -> list[tuple[str, str, str]]:
    """(id-or-name, task id, worker id) of `swf.managed=1` objects; `kind` is e.g. ["ps", "-a"]."""
    listing = subprocess.run(
        [
            docker, *kind,
            "--filter", f"label={LABEL_MANAGED}=1",
            "--format",
            "{{" + name_field + '}}\\t{{.Label "' + LABEL_TASK + '"}}'
            + '\\t{{.Label "' + LABEL_WORKER + '"}}',
        ],
        capture_output=True, text=True, timeout=_RM_TIMEOUT_SECONDS, check=True,
    )  # fmt: skip
    found = []
    for line in listing.stdout.splitlines():
        ident, _, rest = line.strip().partition("\t")
        task_id, _, worker = rest.partition("\t")
        if ident:
            found.append((ident, task_id.strip(), worker.strip()))
    return found


def cleanup_orphans(
    active_task_ids: Collection[str],
    *,
    worker_id: str | None = None,
    reap_task_ids: Collection[str] = (),
    egress: EgressConfig | None = None,
    docker: str = "docker",
) -> list[str]:
    """Remove managed sandboxes, then task networks, whose task is not active.

    With `worker_id`, only objects labelled with that worker, or whose task is in
    `reap_task_ids` (tasks just interrupted for a lost lease), are removed: other workers'
    sandboxes are theirs. `worker_id=None` removes every inactive one (M2 behaviour).
    Returns the removed container ids followed by the removed network names. With `egress`,
    the proxy is disconnected from a network before it is removed (else removal fails).
    Already-gone objects are not an error.
    """
    active = {str(t) for t in active_task_ids}
    reap = {str(t) for t in reap_task_ids}

    def removable(task_id: str, worker: str) -> bool:
        if task_id in active:
            return False
        return worker_id is None or worker == worker_id or task_id in reap

    removed: list[str] = []
    for container_id, task_id, worker in _managed(docker, ["ps", "-a"], ".ID"):
        if not removable(task_id, worker):
            continue
        rm = subprocess.run(
            [docker, "rm", "-f", container_id],
            capture_output=True, text=True, timeout=_RM_TIMEOUT_SECONDS, check=False,
        )  # fmt: skip
        if rm.returncode == 0:
            removed.append(container_id)
    for network, task_id, worker in _managed(docker, ["network", "ls"], ".Name"):
        if removable(task_id, worker) and remove_network(egress, network, docker=docker):
            removed.append(network)
    return removed
