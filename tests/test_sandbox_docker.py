"""Sandbox hardening, cleanup, per-task networks and egress filtering against a real Docker daemon
(contract §4, §5, §9 A1/A3/A6/A10).

Marked `docker` (skipped with a reason when `docker info` fails). Tests that need the Squid proxy
(pulling its image, reaching PyPI) are also marked `network`. Names carry the run id
(`swf-test-<run>-...`), so parallel runs do not touch each other; every container and network is
removed by fixture finalizers.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from swf.egress import (
    EgressConfig,
    EgressError,
    EgressInfo,
    TaskNetwork,
    attach_task_network,
    detach_task_network,
    ensure_egress,
    render_squid_conf,
    teardown_egress,
)
from swf.sandbox import (
    SandboxEnv,
    SandboxSpec,
    cleanup_orphans,
    runtime_from_env,
    sandbox_user,
)

pytestmark = pytest.mark.docker

IMAGE = "python:3.12-slim"
MakeSandbox = Callable[..., SandboxEnv]
MakeTaskSandbox = Callable[..., tuple[SandboxEnv, TaskNetwork]]
network = pytest.mark.network


def docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def inspect(kind: str, name: str) -> dict[str, Any]:
    out = docker(kind, "inspect", name)
    assert out.returncode == 0, out.stderr
    data: dict[str, Any] = json.loads(out.stdout)[0]
    return data


def exists(container_id: str) -> bool:
    return bool(docker("ps", "-aq", "--no-trunc", "--filter", f"id={container_id}").stdout.strip())


def network_exists(name: str) -> bool:
    return docker("network", "inspect", name).returncode == 0


def managed_task_ids() -> set[str]:
    """Task ids of every `swf.managed=1` container and network on this Docker host.

    Passed as "active" to `cleanup_orphans` so a test removes only its own victims, never other
    runs' containers (A10).
    """
    ids: set[str] = set()
    for args in (["ps", "-a"], ["network", "ls"]):
        out = docker(*args, "--filter", "label=swf.managed=1", "--format", '{{.Label "swf.task"}}')
        ids |= {line.strip() for line in out.stdout.splitlines() if line.strip()}
    return ids


def run(env: SandboxEnv, command: str, **kwargs: Any) -> dict[str, Any]:
    return env.execute({"command": command}, **kwargs)


def py(env: SandboxEnv, script: str, **kwargs: Any) -> dict[str, Any]:
    return run(env, f"python -c {shlex.quote(script)}", **kwargs)


def container_ip(container_id: str, net: str) -> str:
    info = inspect("container", container_id)
    return str(info["NetworkSettings"]["Networks"][net]["IPAddress"])


@pytest.fixture(scope="module")
def image() -> str:
    if docker("image", "inspect", IMAGE).returncode != 0:
        pulled = docker("pull", IMAGE, timeout=300)
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {IMAGE}: {pulled.stderr.strip()[:200]}")
    return IMAGE


@pytest.fixture(scope="module")
def plain_network(run_id: str) -> Iterator[str]:
    """An internal network with no proxy on it: containers have no route anywhere."""
    name = f"swf-test-{run_id}-plain"
    docker("network", "rm", name)
    created = docker("network", "create", "--internal", name)
    assert created.returncode == 0, created.stderr
    yield name
    docker("network", "rm", name)


@pytest.fixture
def sandboxes(tmp_path: Path, image: str, run_id: str) -> Iterator[MakeSandbox]:
    """Factory for sandboxes; every one is removed afterwards. Pass network= (and proxy_url=)."""
    made: list[SandboxEnv] = []

    def make(**overrides: Any) -> SandboxEnv:
        workspace = overrides.pop("workspace", None) or tmp_path / f"ws-{len(made)}"
        workspace.mkdir(exist_ok=True)
        fields: dict[str, Any] = {
            "task_id": f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}",
            "image": image,
            "workspace": workspace,
            "proxy_url": "http://127.0.0.1:9",  # nothing listens there; irrelevant offline
            "memory_mb": 256,
            "command_timeout": 120,
        }
        env = SandboxEnv(SandboxSpec(**{**fields, **overrides}))
        made.append(env)
        return env

    yield make
    for env in made:
        env.cleanup()


@pytest.fixture
def sandbox(sandboxes: MakeSandbox, plain_network: str) -> SandboxEnv:
    return sandboxes(network=plain_network)


@pytest.fixture
def task_sandboxes(
    sandboxes: MakeSandbox, egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> Iterator[MakeTaskSandbox]:
    """Sandboxes on their own task network behind the proxy: (env, TaskNetwork)."""
    envs: list[SandboxEnv] = []
    task_ids: list[str] = []

    def make(**overrides: Any) -> tuple[SandboxEnv, TaskNetwork]:
        task_id = f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}"
        task_ids.append(task_id)
        net = attach_task_network(egress_config, task_id)
        env = sandboxes(task_id=task_id, network=net.name, proxy_url=net.proxy_url, **overrides)
        envs.append(env)
        return env, net

    yield make
    for env in envs:  # containers first: a network with endpoints cannot be removed
        env.cleanup()
    for task_id in task_ids:
        detach_task_network(egress_config, task_id)


# --- hardening --------------------------------------------------------------------------------


def test_container_is_hardened(sandboxes: MakeSandbox, plain_network: str) -> None:
    env = sandboxes(network=plain_network, memory_mb=256, cpus=0.5, pids_limit=100)
    assert env.container_id
    info = inspect("container", env.container_id)
    host = info["HostConfig"]
    assert host["Privileged"] is False
    assert host["Memory"] == 256 * 1024 * 1024
    assert host["MemorySwap"] == 256 * 1024 * 1024
    assert host["NanoCpus"] == 500_000_000
    assert host["PidsLimit"] == 100
    assert host["CapDrop"] == ["ALL"]
    assert not host.get("CapAdd")
    assert any(opt.startswith("no-new-privileges") for opt in host["SecurityOpt"])
    assert host["Init"] is True
    assert host["AutoRemove"] is True
    assert host["NetworkMode"] == plain_network
    assert host["Dns"] == ["127.0.0.1"]
    assert not host.get("PortBindings")
    assert not host.get("Binds")
    assert info["Config"]["User"] == sandbox_user()
    assert info["Config"]["Labels"]["swf.managed"] == "1"
    assert info["Config"]["Labels"]["swf.task"] == env.spec.task_id
    mounts = [(m["Type"], m["Destination"], m["RW"]) for m in info["Mounts"]]
    assert mounts == [("bind", "/repo", True)]  # workspace only: no docker.sock, no volumes


def test_runs_as_unprivileged_uid_and_can_write_workspace(sandbox: SandboxEnv) -> None:
    out = run(sandbox, "id -u; id -g; pwd")
    assert out["returncode"] == 0, out
    uid, gid = sandbox_user().split(":")
    assert out["output"].split() == [uid, gid, "/repo"]
    out = run(sandbox, "echo hello > /repo/made-in-container.txt")
    assert out["returncode"] == 0, out
    written = sandbox.spec.workspace / "made-in-container.txt"
    assert written.read_text(encoding="utf-8").strip() == "hello"


def test_home_and_environment(sandbox: SandboxEnv) -> None:
    out = run(sandbox, "mkdir -p $HOME && touch $HOME/x && echo $HOME $PYTHONDONTWRITEBYTECODE")
    assert out["returncode"] == 0, out
    assert out["output"].split() == ["/tmp/home", "1"]


def test_image_path_survives_and_the_shell_is_not_a_login_shell(
    sandbox: SandboxEnv, image: str
) -> None:
    # A login shell re-runs /etc/profile and resets PATH, which breaks venv/Go/Rust images.
    env_list = json.loads(docker("image", "inspect", image).stdout)[0]["Config"]["Env"]
    image_path = next(e for e in env_list if e.startswith("PATH=")).removeprefix("PATH=")
    out = run(sandbox, "echo $PATH")
    assert out["returncode"] == 0, out
    assert out["output"].strip() == image_path
    login = run(sandbox, "shopt -q login_shell && echo login || echo nonlogin")
    assert login["output"].strip() == "nonlogin"


def test_no_capabilities_and_no_new_privileges(sandbox: SandboxEnv) -> None:
    out = run(sandbox, "grep -E '^(CapEff|CapBnd|NoNewPrivs):' /proc/self/status")
    assert out["returncode"] == 0, out
    fields = dict(line.split(":\t", 1) for line in out["output"].strip().splitlines())
    assert int(fields["CapEff"], 16) == 0
    assert int(fields["CapBnd"], 16) == 0
    assert fields["NoNewPrivs"].strip() == "1"


def test_host_secrets_never_reach_the_container(
    sandboxes: MakeSandbox, plain_network: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = {
        "GITHUB_TOKEN": f"ghp_sentinel_{uuid.uuid4().hex}",
        "SWF_TOKEN_ENCRYPTION_KEY": f"fernet_sentinel_{uuid.uuid4().hex}",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    env = sandboxes(network=plain_network)
    assert env.container_id

    shell_env = run(env, "env")
    pid1_env = run(env, "cat /proc/1/environ | tr '\\0' '\\n'")
    assert shell_env["returncode"] == 0 and pid1_env["returncode"] == 0
    inspected = json.dumps(inspect("container", env.container_id)["Config"]["Env"])
    for key, value in secrets.items():
        for where, text in [
            ("env", shell_env["output"]),
            ("/proc/1/environ", pid1_env["output"]),
            ("docker inspect", inspected),
        ]:
            assert value not in text, f"{key} leaked via {where}"
            assert f"{key}=" not in text, f"{key} name leaked via {where}"


def test_memory_limit_kills_a_runaway_process(sandboxes: MakeSandbox, plain_network: str) -> None:
    env = sandboxes(network=plain_network, memory_mb=128)
    small = py(env, "x = b'a' * (20 * 1024 * 1024); print('ok')")
    assert small["returncode"] == 0, small
    big = py(env, "x = b'a' * (400 * 1024 * 1024); print('allocated')")
    assert big["returncode"] != 0, big
    assert "allocated" not in big["output"]


def test_pids_limit_caps_process_count(sandboxes: MakeSandbox, plain_network: str) -> None:
    env = sandboxes(network=plain_network, pids_limit=64)
    script = (
        "import subprocess\n"
        "procs, failed = [], 0\n"
        "for _ in range(200):\n"
        "    try:\n"
        "        procs.append(subprocess.Popen(['sleep', '30']))\n"
        "    except OSError:\n"
        "        failed += 1\n"
        "print(len(procs), failed)\n"
        "for p in procs:\n"
        "    p.kill()\n"
    )
    out = py(env, script)
    if runtime_from_env() == "runsc" and out["returncode"] != 0:
        # gVisor: the host pids cgroup also counts the Sentry's threads, so hitting the cap
        # can end the whole sandbox (exec wait fails with EOF). Contained either way (m8 A5).
        assert "WaitPID" in out["output"] or "EOF" in out["output"], out
        return
    assert out["returncode"] == 0, out
    started, failed = (int(x) for x in out["output"].split()[-2:])
    assert failed > 0
    assert 0 < started < 64


# --- cleanup ----------------------------------------------------------------------------------


def test_cleanup_is_synchronous_and_idempotent(sandboxes: MakeSandbox, plain_network: str) -> None:
    env = sandboxes(network=plain_network)
    container_id = env.container_id
    assert container_id and exists(container_id)
    env.cleanup()
    assert not exists(container_id)  # gone immediately, not "soon"
    assert env.container_id is None
    env.cleanup()  # second call: no error


def test_cleanup_orphans_removes_only_inactive_tasks(
    sandboxes: MakeSandbox, plain_network: str
) -> None:
    keep = sandboxes(network=plain_network)
    orphan = sandboxes(network=plain_network)
    assert keep.container_id and orphan.container_id
    # Everything managed on this host counts as active except the victim (A10).
    active = managed_task_ids() - {orphan.spec.task_id}

    removed = cleanup_orphans(active)

    assert any(orphan.container_id.startswith(r) for r in removed)
    assert not any(keep.container_id.startswith(r) for r in removed)
    assert not exists(orphan.container_id)
    assert exists(keep.container_id)
    assert run(keep, "echo still-alive")["output"].strip() == "still-alive"


def test_cleanup_orphans_ignores_unmanaged_containers(image: str, run_id: str) -> None:
    name = f"swf-test-{run_id}-unmanaged"
    started = docker("run", "-d", "--rm", "--name", name, image, "sleep", "60")
    assert started.returncode == 0, started.stderr
    try:
        cleanup_orphans(managed_task_ids())
        assert docker("ps", "-q", "--filter", f"name={name}").stdout.strip()
    finally:
        docker("rm", "-f", name)


# --- isolation on an internal network without a proxy ------------------------------------------

CONNECT = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.settimeout(5)\n"
    "s.connect(({host!r}, {port}))\n"
    "print('connected')\n"
)


def test_plain_network_is_internal(plain_network: str) -> None:
    assert inspect("network", plain_network)["Internal"] is True


def test_no_direct_route_out(sandbox: SandboxEnv) -> None:
    out = py(sandbox, CONNECT.format(host="1.1.1.1", port=443), timeout=30)
    assert out["returncode"] != 0, out
    assert "connected" not in out["output"]


def test_dns_does_not_resolve(sandbox: SandboxEnv) -> None:
    for name in ("example.com", "pypi.org"):
        out = py(sandbox, f"import socket; print(socket.gethostbyname({name!r}))", timeout=30)
        assert out["returncode"] != 0, (name, out)


# --- egress proxy (needs the Squid image and the internet) -------------------------------------


@pytest.fixture
def via_proxy(task_sandboxes: MakeTaskSandbox) -> SandboxEnv:
    env, _net = task_sandboxes()
    return env


FETCH = (
    "import urllib.request as u\n"
    "try:\n"
    "    r = u.urlopen({url!r}, timeout=30)\n"
    "    print('STATUS', r.status)\n"
    "except Exception as e:\n"
    "    print('ERROR', e)\n"
    "    raise SystemExit(1)\n"
)


@network
def test_allowed_domain_is_reachable_through_the_proxy(via_proxy: SandboxEnv) -> None:
    out = py(via_proxy, FETCH.format(url="https://pypi.org/simple/pip/"), timeout=60)
    assert out["returncode"] == 0, out
    assert "STATUS 200" in out["output"]


@network
def test_pip_installs_through_the_proxy(via_proxy: SandboxEnv) -> None:
    out = run(via_proxy, "python -m pip install --quiet --target /tmp/pkgs six", timeout=120)
    assert out["returncode"] == 0, out
    assert run(via_proxy, "ls /tmp/pkgs")["output"].count("six") >= 1


@network
@pytest.mark.parametrize(
    "url", ["https://example.com/", "https://github.com/", "https://evil.test/"]
)
def test_other_domains_are_refused(via_proxy: SandboxEnv, url: str) -> None:
    out = py(via_proxy, FETCH.format(url=url), timeout=60)
    assert out["returncode"] != 0, out
    assert "STATUS" not in out["output"]
    assert "403" in out["output"]


@network
def test_ip_literal_is_refused(via_proxy: SandboxEnv) -> None:
    out = py(via_proxy, FETCH.format(url="https://1.1.1.1/"), timeout=60)
    assert out["returncode"] != 0, out
    assert "STATUS" not in out["output"]
    assert "403" in out["output"]


@network
def test_plain_http_is_refused_even_for_an_allowed_domain(via_proxy: SandboxEnv) -> None:
    out = py(via_proxy, FETCH.format(url="http://pypi.org/simple/pip/"), timeout=60)
    assert out["returncode"] != 0, out
    assert "STATUS 200" not in out["output"]
    assert "403" in out["output"]


@network
def test_direct_route_is_still_closed_with_a_proxy_present(via_proxy: SandboxEnv) -> None:
    out = py(via_proxy, CONNECT.format(host="1.1.1.1", port=443), timeout=30)
    assert out["returncode"] != 0, out
    assert "connected" not in out["output"]


@network
def test_dns_is_dead_even_with_a_proxy_present(via_proxy: SandboxEnv) -> None:
    out = py(via_proxy, "import socket; print(socket.gethostbyname('pypi.org'))", timeout=30)
    assert out["returncode"] != 0, out


# --- per-task networks (A1): tasks must not reach each other -------------------------------------

LISTEN = (
    "import http.server, socketserver\n"
    "socketserver.TCPServer.allow_reuse_address = True\n"
    "http.server.ThreadingHTTPServer(('0.0.0.0', 8000), http.server.SimpleHTTPRequestHandler)"
    ".serve_forever()\n"
)
WAIT_LOCAL = (
    "import socket, time\n"
    "for _ in range(50):\n"
    "    s = socket.socket()\n"
    "    s.settimeout(1)\n"
    "    try:\n"
    "        s.connect(({ip!r}, 8000))\n"
    "        print('listening')\n"
    "        raise SystemExit(0)\n"
    "    except OSError:\n"
    "        time.sleep(0.2)\n"
    "raise SystemExit(1)\n"
)


@network
def test_two_tasks_cannot_reach_each_other(task_sandboxes: MakeTaskSandbox) -> None:
    a, net_a = task_sandboxes()
    b, net_b = task_sandboxes()
    assert net_a.name != net_b.name
    assert a.container_id and b.container_id
    ip_b = container_ip(b.container_id, net_b.name)
    assert container_ip(a.container_id, net_a.name) != ip_b

    # A server in B, reachable from B itself (control) ...
    started = run(b, f"nohup python -c {shlex.quote(LISTEN)} >/dev/null 2>&1 &")
    assert started["returncode"] == 0, started
    ready = py(b, WAIT_LOCAL.format(ip=ip_b), timeout=30)
    assert "listening" in ready["output"], ready
    # ... but not from A, neither directly nor through B's side of the proxy.
    direct = py(a, CONNECT.format(host=ip_b, port=8000), timeout=30)
    assert direct["returncode"] != 0 and "connected" not in direct["output"], direct
    other_proxy = py(a, CONNECT.format(host=net_b.proxy_ip, port=3128), timeout=30)
    assert other_proxy["returncode"] != 0 and "connected" not in other_proxy["output"]


@network
def test_a_task_reaches_its_own_proxy_address(task_sandboxes: MakeTaskSandbox) -> None:
    env, net = task_sandboxes()
    host, port = net.proxy_url.removeprefix("http://").split(":")
    out = py(env, CONNECT.format(host=host, port=int(port)), timeout=30)
    assert "connected" in out["output"], out


# --- proxy container and task network lifecycle ------------------------------------------------


@network
def test_proxy_container_shape(egress: EgressInfo, egress_config: EgressConfig) -> None:
    info = inspect("container", egress_config.proxy_name)
    assert info["Id"] == egress.container_id
    assert info["State"]["Running"] is True
    labels = info["Config"]["Labels"]
    assert labels["swf.egress"] == "1"
    expected = hashlib.sha256(
        (egress_config.image + "\n" + render_squid_conf(egress_config)).encode()
    ).hexdigest()
    assert labels["swf.egress.conf-sha256"] == expected
    assert "swf.managed" not in labels  # or orphan cleanup would remove the proxy
    assert "swf.task" not in labels
    host = info["HostConfig"]
    assert not host.get("PortBindings")  # no published ports
    assert host["CapDrop"] == ["ALL"]
    assert not {"NET_ADMIN", "NET_RAW", "SYS_ADMIN"} & set(host.get("CapAdd") or [])
    assert any(opt.startswith("no-new-privileges") for opt in host["SecurityOpt"])
    assert host["Memory"] == 256 * 1024 * 1024
    assert host["PidsLimit"] == 256
    assert host["Privileged"] is False


@network
def test_attach_task_network(egress: EgressInfo, egress_config: EgressConfig, run_id: str) -> None:
    task_id = f"swf-test-{run_id}-attach"
    try:
        net = attach_task_network(egress_config, task_id)
        assert net.name == f"{egress_config.network_prefix}{task_id}"
        assert net.proxy_url == f"http://{net.proxy_ip}:{egress_config.port}"
        info = inspect("network", net.name)
        assert info["Internal"] is True
        assert info["Labels"]["swf.managed"] == "1"
        assert info["Labels"]["swf.task"] == task_id
        assert container_ip(egress.container_id, net.name) == net.proxy_ip
        # idempotent: same network, same address
        assert attach_task_network(egress_config, task_id) == net
    finally:
        detach_task_network(egress_config, task_id)


@network
def test_detach_task_network_removes_it_and_is_idempotent(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> None:
    task_id = f"swf-test-{run_id}-detach"
    net = attach_task_network(egress_config, task_id)
    detach_task_network(egress_config, task_id)
    assert not network_exists(net.name)
    proxy_networks = inspect("container", egress.container_id)["NetworkSettings"]["Networks"]
    assert net.name not in proxy_networks
    detach_task_network(egress_config, task_id)  # nothing left: no error


@network
def test_attach_refuses_an_existing_non_internal_network(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> None:
    task_id = f"swf-test-{run_id}-open"
    name = f"{egress_config.network_prefix}{task_id}"
    assert docker("network", "create", name).returncode == 0
    try:
        with pytest.raises(EgressError):
            attach_task_network(egress_config, task_id)
    finally:
        detach_task_network(egress_config, task_id)
        docker("network", "rm", name)


@network
def test_orphan_cleanup_removes_networks_of_inactive_tasks_only(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> None:
    dead, alive = f"swf-test-{run_id}-dead", f"swf-test-{run_id}-alive"
    dead_net = attach_task_network(egress_config, dead)
    alive_net = attach_task_network(egress_config, alive)
    try:
        removed = cleanup_orphans(managed_task_ids() - {dead}, egress=egress_config)
        assert dead_net.name in removed
        assert alive_net.name not in removed
        assert not network_exists(dead_net.name)
        assert network_exists(alive_net.name)
        # the proxy itself is neither a managed container nor removed
        assert inspect("container", egress_config.proxy_name)["State"]["Running"] is True
    finally:
        detach_task_network(egress_config, dead)
        detach_task_network(egress_config, alive)


# --- ensure_egress lifecycle (own names, so the session proxy is untouched) --------------------


@pytest.fixture
def lifecycle_config(run_id: str) -> Iterator[EgressConfig]:
    cfg = EgressConfig(
        proxy_name=f"swf-test-{run_id}-life-proxy", network_prefix=f"swf-test-{run_id}-life-"
    )
    teardown_egress(cfg)
    yield cfg
    teardown_egress(cfg)


@network
def test_ensure_egress_is_idempotent(lifecycle_config: EgressConfig) -> None:
    first = ensure_egress(lifecycle_config)
    second = ensure_egress(lifecycle_config)
    assert second == first
    assert inspect("container", lifecycle_config.proxy_name)["Id"] == first.container_id


@network
def test_ensure_egress_recreates_the_proxy_when_config_changes(
    lifecycle_config: EgressConfig,
) -> None:
    first = ensure_egress(lifecycle_config)
    old_hash = inspect("container", first.container_id)["Config"]["Labels"][
        "swf.egress.conf-sha256"
    ]

    changed = lifecycle_config.model_copy(
        update={"allow_domains": (*lifecycle_config.allow_domains, "example.org")}
    )
    second = ensure_egress(changed)

    assert second.container_id != first.container_id
    assert not exists(first.container_id)
    new = inspect("container", second.container_id)
    assert new["Config"]["Labels"]["swf.egress.conf-sha256"] != old_hash
    conf = docker("exec", changed.proxy_name, "cat", "/etc/squid/squid.conf").stdout
    assert "example.org" in conf


@network
def test_recreated_proxy_rejoins_existing_task_networks(
    lifecycle_config: EgressConfig, run_id: str
) -> None:
    ensure_egress(lifecycle_config)
    task_id = f"swf-test-{run_id}-rejoin"
    net = attach_task_network(lifecycle_config, task_id)
    try:
        changed = lifecycle_config.model_copy(
            update={"allow_domains": (*lifecycle_config.allow_domains, "example.org")}
        )
        info = ensure_egress(changed)
        assert container_ip(info.container_id, net.name)  # running tasks keep their egress
    finally:
        detach_task_network(lifecycle_config, task_id)


@network
def test_teardown_removes_the_proxy_only(lifecycle_config: EgressConfig, run_id: str) -> None:
    ensure_egress(lifecycle_config)
    task_id = f"swf-test-{run_id}-teardown"
    net = attach_task_network(lifecycle_config, task_id)
    try:
        teardown_egress(lifecycle_config)
        assert docker("container", "inspect", lifecycle_config.proxy_name).returncode != 0
        assert network_exists(net.name)  # task networks belong to their tasks
        teardown_egress(lifecycle_config)  # nothing left: must not raise
    finally:
        detach_task_network(lifecycle_config, task_id)
