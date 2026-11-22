"""The package cache behind the egress proxy, with a real Docker daemon (m9 contract T5).

Contract sections: s0.6 (design), s1.4 (egress additions), T5.

Marked `docker` and `network` (the cache fetches from PyPI through Squid). Each test builds its
own egress stack (proxy, cache, `swf-egress`, task networks) named with the run id and a random
suffix, with the cache directory under `tmp_path` (never `./pkgcache`). The stack is built inside
the test body, not in a fixture, so a missing T6 feature is a test failure, not a fixture error.
Everything is removed in `finally`.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from swf.egress import EgressConfig, detach_task_network, ensure_egress, teardown_egress
from swf.sandbox import SandboxEnv, open_task_sandbox
from swf.worker import EnvRequest

pytestmark = [pytest.mark.docker, pytest.mark.network]

IMAGE = "python:3.12-slim"
PYTEST_PIN = "pytest==9.2.0"
INSTALL = f"python -m pip install --quiet --target /tmp/pkgs {PYTEST_PIN}"


def docker(*args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def inspect(kind: str, name: str) -> dict[str, Any]:
    out = docker(kind, "inspect", name)
    assert out.returncode == 0, out.stderr
    data: dict[str, Any] = json.loads(out.stdout)[0]
    return data


def py(env: SandboxEnv, script: str) -> dict[str, Any]:
    return env.execute({"command": f"python -c {shlex.quote(script)}"})


@dataclass
class Stack:
    cfg: EgressConfig
    cache_dir: Path
    tmp: Path
    run_id: str
    envs: list[SandboxEnv]
    task_ids: list[str]

    def sandbox(self) -> SandboxEnv:
        task_id = f"swf-test-{self.run_id}-{uuid.uuid4().hex[:8]}"
        workspace = self.tmp / f"ws-{len(self.envs)}"
        workspace.mkdir()
        self.task_ids.append(task_id)
        env = open_task_sandbox(
            self.cfg,
            EnvRequest(task_id=task_id, image=IMAGE, workspace=workspace, memory_mb=512, cpus=1.0),
        )
        self.envs.append(env)
        return env

    def cache_logs(self) -> str:
        out = docker("logs", self.cfg.pkgcache_name)
        return out.stdout + out.stderr


@contextmanager
def stack(tmp_path: Path, run_id: str, *, pkgcache: bool = True) -> Iterator[Stack]:
    """Proxy + (optionally) cache on their own names; always torn down, even if setup fails."""
    if docker("image", "inspect", IMAGE).returncode != 0:
        pulled = docker("pull", IMAGE, timeout=300)
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {IMAGE}: {pulled.stderr.strip()[:200]}")
    tag = f"swf-test-{run_id}-pc{uuid.uuid4().hex[:6]}"
    cache_dir = tmp_path / "pkgcache"
    cache_dir.mkdir()
    st: Stack | None = None
    cfg = None
    try:
        cfg = EgressConfig(
            proxy_name=f"{tag}-egress",
            network_prefix=f"{tag}-task-",
            pkgcache=pkgcache,
            pkgcache_name=f"{tag}-cache",
            pkgcache_dir=cache_dir,
            egress_network=f"{tag}-net",
        )
        st = Stack(cfg, cache_dir, tmp_path, run_id, [], [])
        ensure_egress(cfg)
        yield st
    finally:
        if st is not None:
            for env in st.envs:  # containers first: a network with endpoints cannot be removed
                env.cleanup()
            for task_id in st.task_ids:
                detach_task_network(st.cfg, task_id)
        if cfg is not None:
            teardown_egress(cfg)
            docker("rm", "-f", cfg.pkgcache_name)
            docker("rm", "-f", cfg.proxy_name)
            docker("network", "rm", cfg.egress_network)


def test_cache_container_is_idempotent_hardened_and_internal_only(
    tmp_path: Path, run_id: str
) -> None:
    with stack(tmp_path, run_id) as st:
        name = st.cfg.pkgcache_name
        first = inspect("container", name)["Id"]
        ensure_egress(st.cfg)
        info = inspect("container", name)
        assert info["Id"] == first  # idempotent: reused, not recreated
        host = info["HostConfig"]
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert any(opt.startswith("no-new-privileges") for opt in host["SecurityOpt"])
        labels = info["Config"]["Labels"]
        assert labels["swf.egress"] == "1"
        assert "swf.managed" not in labels
        nets = info["NetworkSettings"]["Networks"]
        assert st.cfg.egress_network in nets
        assert "bridge" not in nets
        for net in nets:
            assert inspect("network", net)["Internal"] is True, net


def test_cache_is_reused_across_tasks(tmp_path: Path, run_id: str) -> None:
    with stack(tmp_path, run_id) as st:
        a = st.sandbox()
        installed = a.execute({"command": INSTALL})
        assert installed["returncode"] == 0, installed["output"][-500:]
        stored = [p for p in st.cache_dir.rglob("*") if p.is_file()]
        assert stored, "the cache directory on the host holds no files after the first install"

        b = st.sandbox()  # another task, another task network
        assert b.spec.task_id != a.spec.task_id
        assert b.spec.network != a.spec.network
        again = b.execute({"command": INSTALL})
        assert again["returncode"] == 0, again["output"][-500:]
        wheel_lines = [
            line
            for line in st.cache_logs().splitlines()
            if "/files/" in line and "pytest-9.2.0" in line
        ]
        assert wheel_lines, "no pytest wheel request in the cache log"
        assert any(re.search(r"\bhit$", line.strip()) for line in wheel_lines), wheel_lines


def test_cache_is_read_only_to_tasks(tmp_path: Path, run_id: str) -> None:
    with stack(tmp_path, run_id) as st:
        env = st.sandbox()
        index_url = env.spec.index_url
        assert index_url is not None
        host, _, rest = index_url.removeprefix("http://").partition(":")
        port = int(rest.split("/", 1)[0])
        script = (
            "import http.client, json\n"
            "out = {}\n"
            "for method in ('POST', 'PUT', 'DELETE'):\n"
            f"    c = http.client.HTTPConnection({host!r}, {port}, timeout=20)\n"
            "    c.request(method, '/simple/pytest/', body=b'x')\n"
            "    out[method] = c.getresponse().status\n"
            f"c = http.client.HTTPConnection({host!r}, {port}, timeout=20)\n"
            "c.request('GET', '/files/../../etc/passwd')\n"
            "out['traversal'] = c.getresponse().status\n"
            "print(json.dumps(out))\n"
        )
        result = py(env, script)
        assert result["returncode"] == 0, result["output"][-500:]
        assert json.loads(result["output"].strip().splitlines()[-1]) == {
            "POST": 405,
            "PUT": 405,
            "DELETE": 405,
            "traversal": 404,
        }
        container = inspect("container", env.container_id or "")
        mounts = [(m["Type"], m["Destination"]) for m in container["Mounts"]]
        assert mounts == [("bind", "/repo")]  # the workspace only, never the cache dir
        assert all("pkgcache" not in m.get("Source", "") for m in container["Mounts"])
        listing = env.execute({"command": "ls /"})
        assert listing["returncode"] == 0
        assert "pkgcache" not in listing["output"]


def test_cache_has_no_direct_internet_and_the_proxy_still_filters(
    tmp_path: Path, run_id: str
) -> None:
    direct = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.build_opener(urllib.request.ProxyHandler({})).open("
        "'https://example.com/', timeout=10)\n"
        "    print('REACHED')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    via_proxy = (
        "import os, urllib.request\n"
        "proxy = os.environ['HTTPS_PROXY']\n"
        "opener = urllib.request.build_opener("
        "urllib.request.ProxyHandler({'https': proxy}))\n"
        "for url in ('https://example.com/', 'https://pypi.org/simple/pip/'):\n"
        "    try:\n"
        "        print(url, opener.open(url, timeout=20).status)\n"
        "    except Exception as exc:\n"
        "        print(url, 'ERR', exc)\n"
    )
    with stack(tmp_path, run_id) as st:
        name = st.cfg.pkgcache_name
        got = docker("exec", name, "python", "-I", "-c", direct)
        assert got.returncode == 0, got.stderr[-300:]
        assert got.stdout.strip() == "BLOCKED"
        proxied = docker("exec", name, "python", "-I", "-c", via_proxy)
        assert proxied.returncode == 0, proxied.stderr[-300:]
        lines = proxied.stdout.strip().splitlines()
        assert lines[0].startswith("https://example.com/ ERR") and "403" in lines[0], lines
        assert lines[1] == "https://pypi.org/simple/pip/ 200", lines  # allowlisted host works


def test_pkgcache_off_means_no_cache_container_and_no_index_env(
    tmp_path: Path, run_id: str
) -> None:
    with stack(tmp_path, run_id, pkgcache=False) as st:
        assert docker("container", "inspect", st.cfg.pkgcache_name).returncode != 0
        env = st.sandbox()
        assert env.spec.index_url is None
        container = inspect("container", env.container_id or "")
        keys = {e.split("=", 1)[0] for e in container["Config"]["Env"]}
        assert "PIP_INDEX_URL" not in keys
        assert "PIP_TRUSTED_HOST" not in keys
