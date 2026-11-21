"""Host isolation (contract §0.14, §3 T11): a sandbox must not reach host services through its
task network's gateway IP.

On Docker Desktop the gateway lives inside the Docker VM, so this passes trivially. On the Oracle
Ubuntu VM the default INPUT chain already REJECTs arbitrary ports, so only the gateway:22 case is
meaningful red evidence for the bootstrap rule (`iptables -I INPUT 1 -i br-+ -j DROP`); the
listener case guards hosts without that default (and Docker Desktop).
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from swf.egress import EgressConfig, EgressInfo, attach_task_network, detach_task_network
from swf.sandbox import SandboxEnv, SandboxSpec

pytestmark = [pytest.mark.docker, pytest.mark.network]

IMAGE = "python:3.12-slim"
CONNECT = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.settimeout(3)\n"
    "try:\n"
    "    s.connect(({host!r}, {port}))\n"
    "    print('CONNECTED')\n"
    "except OSError as e:\n"
    "    print('BLOCKED:' + type(e).__name__)\n"
)


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _connect(env: SandboxEnv, host: str, port: int) -> dict[str, object]:
    script = CONNECT.format(host=host, port=port)
    return env.execute({"command": f"python -c {shlex.quote(script)}"}, timeout=30)


def _assert_blocked(out: dict[str, object]) -> None:
    text = str(out["output"])
    assert "BLOCKED:" in text and "CONNECTED" not in text, out


@pytest.fixture
def isolated(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str, tmp_path: Path
) -> Iterator[tuple[SandboxEnv, str, str]]:
    """(sandbox, gateway ip, proxy ip) on a fresh task network behind the proxy."""
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pulled = _docker("pull", IMAGE, timeout=300)
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {IMAGE}: {pulled.stderr.strip()[:200]}")
    task_id = f"swf-test-{run_id}-hi{uuid.uuid4().hex[:6]}"
    net = attach_task_network(egress_config, task_id)
    env: SandboxEnv | None = None
    try:
        env = SandboxEnv(
            SandboxSpec(
                task_id=task_id,
                image=IMAGE,
                workspace=tmp_path,
                network=net.name,
                proxy_url=net.proxy_url,
                memory_mb=256,
                command_timeout=120,
            )
        )
        gw = _docker(
            "network", "inspect", net.name, "-f", "{{(index .IPAM.Config 0).Gateway}}"
        ).stdout.strip()
        assert gw, "task network has no gateway address"
        yield env, gw, net.proxy_ip
    finally:
        if env is not None:
            env.cleanup()
        detach_task_network(egress_config, task_id)


def test_sandbox_cannot_reach_a_host_listener_via_the_gateway(
    isolated: tuple[SandboxEnv, str, str],
) -> None:
    env, gateway, _ = isolated
    with closing(socket.socket()) as server:
        server.bind(("0.0.0.0", 0))
        server.listen(5)
        port = server.getsockname()[1]
        out = _connect(env, gateway, port)
    _assert_blocked(out)


def test_sandbox_cannot_reach_host_ssh_via_the_gateway(
    isolated: tuple[SandboxEnv, str, str],
) -> None:
    env, gateway, _ = isolated
    try:
        with closing(socket.create_connection(("127.0.0.1", 22), timeout=1)):
            pass
    except OSError:
        pytest.skip("host does not accept on 127.0.0.1:22 (nothing to protect here)")
    out = _connect(env, gateway, 22)
    _assert_blocked(out)


def test_control_the_proxy_is_still_reachable(isolated: tuple[SandboxEnv, str, str]) -> None:
    env, _, proxy_ip = isolated
    out = _connect(env, proxy_ip, 3128)
    assert "CONNECTED" in str(out["output"]), out
