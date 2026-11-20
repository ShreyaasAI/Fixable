"""Egress for sandboxes: one Squid allowlist proxy plus one internal network per task (plan M2).

Each task gets its own Docker network created with `--internal` (no route out, and no route to
other tasks: spec §7). The proxy container sits on the default bridge (internet) and is
connected to every task network; it is the sole way out, and only `CONNECT <allowed
domain>:443` passes. Everything else (plain HTTP, IP literals, other ports, other domains) is
denied, so a sandbox cannot use the proxy to reach another task either. Sandboxes have no DNS,
so names are resolved only by Squid (`dstdomain -n`: never reverse-resolve an IP to match).

M2 has no package cache (`cache deny all`); a read-only pull-through cache is M9.
The proxy is labelled `swf.egress=1`, never `swf.managed=1`, so orphan cleanup cannot remove
it; task networks carry `swf.managed=1` + `swf.task=<id>` like the sandboxes. The proxy image is
pinned by digest and the sha256 of image + config is a label: any change recreates the proxy.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

LABEL_MANAGED = "swf.managed"
LABEL_TASK = "swf.task"
LABEL_WORKER = "swf.worker"  # the worker that started the task (multi-worker cleanup, M4)
LABEL_EGRESS = "swf.egress"
LABEL_CONF_SHA = "swf.egress.conf-sha256"
SQUID_IMAGE = (
    "ubuntu/squid:6.6-24.04_edge"
    "@sha256:d9c34c5ab74c6dd41cf1070d9b4221aac072e67e983527ca2d41512958f77506"
)
SQUID_CONF_PATH = "/etc/squid/squid.conf"
READY_LINE = "Accepting HTTP Socket connections"
READY_TIMEOUT_SECONDS = 30.0
_DOCKER_TIMEOUT_SECONDS = 120
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DOMAIN_RE = re.compile(r"^\.?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Squid starts as root and drops to the `proxy` user: it needs SETUID/SETGID, nothing else.
_PROXY_HARDENING = (
    "--cap-drop", "ALL",
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
    "--security-opt", "no-new-privileges",
    "--memory", "256m",
    "--pids-limit", "256",
)  # fmt: skip


class EgressError(Exception):
    pass


class EgressConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    network_prefix: str = Field(default="swf-task-", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    proxy_name: str = "swf-egress-proxy"
    image: str = SQUID_IMAGE
    port: int = Field(default=3128, ge=1, le=65535)
    allow_domains: tuple[str, ...] = ("pypi.org", "files.pythonhosted.org")

    @field_validator("allow_domains")
    @classmethod
    def _domains(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("allow_domains must not be empty")
        bad = [d for d in v if not _DOMAIN_RE.fullmatch(d)]
        if bad:
            raise ValueError(f"invalid allow_domains entries: {bad}")
        return v


def render_squid_conf(cfg: EgressConfig) -> str:
    # Re-check here too: a config built with model_construct() skips validation, and a
    # newline in a domain would inject Squid directives.
    bad = [d for d in cfg.allow_domains if not _DOMAIN_RE.fullmatch(d)]
    if bad or not cfg.allow_domains or not 0 < cfg.port < 65536:
        raise ValueError(f"refusing to render squid.conf: invalid domains {bad!r}")
    lines = [
        f"http_port {cfg.port}",
        "acl allowed_domains dstdomain -n " + " ".join(cfg.allow_domains),
        "acl SSL_ports port 443",
        "acl CONNECT method CONNECT",
        "http_access deny !CONNECT",
        "http_access deny CONNECT !SSL_ports",
        "http_access allow CONNECT allowed_domains",
        "http_access deny all",
        "cache deny all",
        "via off",
        "forwarded_for delete",
        "httpd_suppress_version_string on",
        "shutdown_lifetime 1 seconds",
        # Logs are written by the `proxy` user; the image's entrypoint forwards them to
        # `docker logs` with `tail` as root, which (all caps dropped) can only read 0644 files.
        "umask 022",
    ]
    return "\n".join(lines) + "\n"


def _conf_sha(cfg: EgressConfig, conf: str) -> str:
    return hashlib.sha256(f"{cfg.image}\n{conf}".encode()).hexdigest()


@dataclass(frozen=True)
class EgressInfo:
    container_id: str


@dataclass(frozen=True)
class TaskNetwork:
    name: str
    proxy_ip: str
    proxy_url: str


def _docker(
    docker: str, *args: str, check: bool = True, timeout: float = _DOCKER_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    try:
        run = subprocess.run(
            [docker, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EgressError(f"docker {args[0]} failed: {exc}") from exc
    if check and run.returncode != 0:
        raise EgressError(f"docker {' '.join(args[:2])} failed: {run.stderr.strip()[:500]}")
    return run


def _inspect(docker: str, kind: str, name: str) -> dict[str, Any] | None:
    run = _docker(docker, kind, "inspect", name, check=False)
    if run.returncode != 0:
        return None
    data = json.loads(run.stdout)
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def _network_ip(container: dict[str, Any], network: str) -> str | None:
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    ip = (networks.get(network) or {}).get("IPAddress")
    return ip if isinstance(ip, str) and ip else None


def _labels(obj: dict[str, Any]) -> dict[str, str]:
    labels = (obj.get("Config") or {}).get("Labels") or obj.get("Labels") or {}
    return labels if isinstance(labels, dict) else {}


def _wait_ready(docker: str, name: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        run = _docker(docker, "logs", name, check=False)
        logs = run.stdout + run.stderr
        if READY_LINE in logs:
            return
        state = (_inspect(docker, "container", name) or {}).get("State") or {}
        if time.monotonic() > deadline or state.get("Status") in {"exited", "dead"}:
            tail = "\n".join(logs.strip().splitlines()[-20:])
            raise EgressError(f"egress proxy {name} did not become ready:\n{tail}")
        time.sleep(0.5)


def _task_networks(docker: str, prefix: str) -> list[str]:
    run = _docker(
        docker, "network", "ls", "--filter", f"label={LABEL_MANAGED}=1", "--format", "{{.Name}}"
    )
    return [n for n in run.stdout.split() if n.startswith(prefix)]


def _create_proxy(cfg: EgressConfig, conf: str, sha: str, docker: str) -> None:
    """Create + configure + start; on a concurrent creator's name conflict, wait for theirs."""
    _docker(docker, "rm", "-f", cfg.proxy_name, check=False)
    create = _docker(
        docker, "create",
        "--name", cfg.proxy_name,
        "--label", f"{LABEL_EGRESS}=1",
        "--label", f"{LABEL_CONF_SHA}={sha}",
        "--restart", "unless-stopped",
        *_PROXY_HARDENING,
        cfg.image,
        check=False,
    )  # fmt: skip
    if create.returncode != 0:
        if "already in use" in create.stderr:
            _wait_ready(docker, cfg.proxy_name)
            return
        raise EgressError(f"docker create failed: {create.stderr.strip()[:500]}")
    with tempfile.TemporaryDirectory(prefix="swf-squid-") as tmp:
        path = Path(tmp) / "squid.conf"
        path.write_text(conf, encoding="utf-8", newline="\n")
        _docker(docker, "cp", str(path), f"{cfg.proxy_name}:{SQUID_CONF_PATH}")
    _docker(docker, "start", cfg.proxy_name)
    _wait_ready(docker, cfg.proxy_name)
    # A recreated proxy rejoins the task networks that still exist (running tasks keep egress).
    for network in _task_networks(docker, cfg.network_prefix):
        _docker(docker, "network", "connect", network, cfg.proxy_name, check=False)


def ensure_egress(cfg: EgressConfig, *, docker: str = "docker") -> EgressInfo:
    """Idempotent: reuse a running proxy with the same image + config, else (re)create it."""
    conf = render_squid_conf(cfg)
    sha = _conf_sha(cfg, conf)
    current = _inspect(docker, "container", cfg.proxy_name)
    reusable = (
        current is not None
        and (current.get("State") or {}).get("Running") is True
        and _labels(current).get(LABEL_CONF_SHA) == sha
    )
    if not reusable:
        _create_proxy(cfg, conf, sha, docker)
        current = _inspect(docker, "container", cfg.proxy_name)
    if current is None:
        raise EgressError(f"egress proxy {cfg.proxy_name} vanished after start")
    if _labels(current).get(LABEL_CONF_SHA) != sha:
        raise EgressError(f"egress proxy {cfg.proxy_name} runs a different configuration")
    return EgressInfo(container_id=str(current["Id"]))


def task_network_name(cfg: EgressConfig, task_id: str) -> str:
    return f"{cfg.network_prefix}{task_id}"


def attach_task_network(
    cfg: EgressConfig, task_id: str, *, worker_id: str | None = None, docker: str = "docker"
) -> TaskNetwork:
    """Create (or reuse) the task's internal network and connect the proxy to it.

    With `worker_id`, the network is labelled `swf.worker=<id>` like the task's sandbox.
    """
    if worker_id is not None and not _WORKER_ID_RE.fullmatch(worker_id):
        raise ValueError(f"invalid worker id {worker_id!r}")
    name = task_network_name(cfg, task_id)
    net = _inspect(docker, "network", name)
    if net is None:
        worker_label = [] if worker_id is None else ["--label", f"{LABEL_WORKER}={worker_id}"]
        create = _docker(
            docker, "network", "create", "--internal",
            "--label", f"{LABEL_MANAGED}=1",
            "--label", f"{LABEL_TASK}={task_id}",
            *worker_label,
            name,
            check=False,
        )  # fmt: skip
        if create.returncode != 0 and "already exists" not in create.stderr:
            raise EgressError(f"docker network create failed: {create.stderr.strip()[:500]}")
        net = _inspect(docker, "network", name)
    if net is None or not net.get("Internal"):
        raise EgressError(f"network {name} exists but is not --internal")
    connect = _docker(docker, "network", "connect", name, cfg.proxy_name, check=False)
    if connect.returncode != 0 and "already exists" not in connect.stderr:
        raise EgressError(f"cannot connect proxy to {name}: {connect.stderr.strip()[:500]}")
    proxy = _inspect(docker, "container", cfg.proxy_name)
    ip = None if proxy is None else _network_ip(proxy, name)
    if ip is None:
        raise EgressError(f"egress proxy {cfg.proxy_name} has no address on {name}")
    return TaskNetwork(name=name, proxy_ip=ip, proxy_url=f"http://{ip}:{cfg.port}")


def detach_task_network(cfg: EgressConfig, task_id: str, *, docker: str = "docker") -> None:
    """Disconnect the proxy and remove the task network; a no-op if already gone."""
    remove_network(cfg, task_network_name(cfg, task_id), docker=docker)


def remove_network(cfg: EgressConfig | None, name: str, *, docker: str = "docker") -> bool:
    """Disconnect the proxy (if `cfg`) and remove `name`; True if the network is gone."""
    if cfg is not None:
        _docker(docker, "network", "disconnect", "-f", name, cfg.proxy_name, check=False)
    rm = _docker(docker, "network", "rm", name, check=False)
    return rm.returncode == 0 or _inspect(docker, "network", name) is None


def teardown_egress(cfg: EgressConfig, *, docker: str = "docker") -> None:
    """Remove the proxy (tests; production keeps it running). Task networks are per task."""
    _docker(docker, "rm", "-f", cfg.proxy_name, check=False)
