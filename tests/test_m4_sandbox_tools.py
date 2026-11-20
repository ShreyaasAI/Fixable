"""`SandboxEnv` as a `ToolEnv`, worker labels and `cleanup_orphans(worker_id=…)` against a real
Docker daemon (m4-contract §6, A2, A3).

Marked `docker` (skipped with a reason when `docker info` fails); tests that need the Squid proxy
are also marked `network`. Every name carries the run id (`swf-test-<run>-…`) and every container
and network is removed by fixture finalizers. Worker ids carry the run id too, so the
`worker_id=` cleanups can never touch another run's containers.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from minisweagent.exceptions import Submitted

from swf.agent.tools import ToolEnv, ToolError
from swf.egress import (
    LABEL_WORKER,
    EgressConfig,
    EgressInfo,
    attach_task_network,
    detach_task_network,
)
from swf.sandbox import (
    ExecResult,
    SandboxEnv,
    SandboxSpec,
    cleanup_orphans,
    open_task_sandbox,
    sandbox_run_args,
)
from swf.worker import EnvRequest

pytestmark = pytest.mark.docker

IMAGE = "python:3.12-slim"
network = pytest.mark.network
MakeSandbox = Callable[..., SandboxEnv]


def docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def inspect(kind: str, name: str) -> dict[str, Any]:
    out = docker(kind, "inspect", name)
    assert out.returncode == 0, out.stderr
    data: dict[str, Any] = json.loads(out.stdout)[0]
    return data


def container_exists(container_id: str) -> bool:
    return bool(docker("ps", "-aq", "--no-trunc", "--filter", f"id={container_id}").stdout.strip())


def network_exists(name: str) -> bool:
    return docker("network", "inspect", name).returncode == 0


def managed_task_ids() -> set[str]:
    """Task ids of every managed container/network on this host (so `worker_id=None` cleanups in
    a test only remove that test's own objects)."""
    ids: set[str] = set()
    for args in (["ps", "-a"], ["network", "ls"]):
        out = docker(*args, "--filter", "label=swf.managed=1", "--format", '{{.Label "swf.task"}}')
        ids |= {line.strip() for line in out.stdout.splitlines() if line.strip()}
    return ids


@pytest.fixture(scope="module")
def image() -> str:
    if docker("image", "inspect", IMAGE).returncode != 0:
        pulled = docker("pull", IMAGE, timeout=300)
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {IMAGE}: {pulled.stderr.strip()[:200]}")
    return IMAGE


@pytest.fixture(scope="module")
def plain_network(run_id: str) -> Iterator[str]:
    name = f"swf-test-{run_id}-m4plain"
    docker("network", "rm", name)
    created = docker("network", "create", "--internal", name)
    assert created.returncode == 0, created.stderr
    yield name
    docker("network", "rm", name)


@pytest.fixture
def sandboxes(tmp_path: Path, image: str, run_id: str) -> Iterator[MakeSandbox]:
    made: list[SandboxEnv] = []

    def make(**overrides: Any) -> SandboxEnv:
        workspace = overrides.pop("workspace", None) or tmp_path / f"ws-{len(made)}"
        workspace.mkdir(exist_ok=True)
        fields: dict[str, Any] = {
            "task_id": f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}",
            "image": image,
            "workspace": workspace,
            "proxy_url": "http://127.0.0.1:9",
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


# --- exec_argv ------------------------------------------------------------------------------


def test_the_sandbox_is_a_tool_env(sandbox: SandboxEnv) -> None:
    assert isinstance(sandbox, ToolEnv)


def test_exec_argv_runs_an_argv_without_a_shell(sandbox: SandboxEnv) -> None:
    result = sandbox.exec_argv(["echo", "$HOME;ls", "a b"])
    assert isinstance(result, ExecResult)
    assert result.returncode == 0
    assert result.stdout == b"$HOME;ls a b\n"  # no expansion, no word splitting of the argument


def test_exec_argv_passes_stdin_and_reports_failures(sandbox: SandboxEnv) -> None:
    assert sandbox.exec_argv(["cat"], stdin=b"\x00binary\xff").stdout == b"\x00binary\xff"
    failed = sandbox.exec_argv(["sh", "-c", "echo oops >&2; exit 3"])
    assert failed.returncode == 3
    assert b"oops" in failed.stderr


def test_exec_argv_runs_in_the_workspace(sandbox: SandboxEnv) -> None:
    assert sandbox.exec_argv(["pwd"]).stdout.strip() == b"/repo"


# --- read / write / list / search -----------------------------------------------------------


def test_read_bytes_returns_the_file_exactly(sandbox: SandboxEnv) -> None:
    payload = b"line one\n\x00\xff binary \r\n"
    (sandbox.spec.workspace / "data.bin").write_bytes(payload)
    assert sandbox.read_bytes("/repo/data.bin") == payload


def test_read_bytes_of_a_missing_file_is_a_tool_error(sandbox: SandboxEnv) -> None:
    with pytest.raises(ToolError):
        sandbox.read_bytes("/repo/nope.txt")


def test_paths_with_spaces_and_leading_dashes_work(sandbox: SandboxEnv) -> None:
    folder = sandbox.spec.workspace / "dir with space"
    folder.mkdir()
    (folder / "f x.txt").write_bytes(b"spaced")
    assert sandbox.read_bytes("/repo/dir with space/f x.txt") == b"spaced"
    sandbox.write_bytes("/repo/-dash name.txt", b"dash")
    assert (sandbox.spec.workspace / "-dash name.txt").read_bytes() == b"dash"


def test_write_bytes_creates_parents_and_overwrites(sandbox: SandboxEnv) -> None:
    sandbox.write_bytes("/repo/a/b/c.txt", b"first")
    target = sandbox.spec.workspace / "a" / "b" / "c.txt"
    assert target.read_bytes() == b"first"
    sandbox.write_bytes("/repo/a/b/c.txt", b"second, longer")
    assert target.read_bytes() == b"second, longer"


def test_write_bytes_handles_100_kb_past_the_windows_command_line_cap(
    sandbox: SandboxEnv,
) -> None:
    data = (b"0123456789abcdef" * 6400) + b"tail"  # 102 404 bytes
    sandbox.write_bytes("/repo/big.bin", data)
    assert (sandbox.spec.workspace / "big.bin").read_bytes() == data
    assert sandbox.read_bytes("/repo/big.bin") == data


def test_write_bytes_failure_is_a_tool_error(sandbox: SandboxEnv) -> None:
    (sandbox.spec.workspace / "occupied").write_bytes(b"a file where a directory is needed")
    with pytest.raises(ToolError):
        sandbox.write_bytes("/repo/occupied/child.txt", b"x")


def test_list_dir_marks_directories_and_shows_hidden_files(sandbox: SandboxEnv) -> None:
    root = sandbox.spec.workspace
    (root / "src").mkdir()
    (root / "README.md").write_bytes(b"r")
    (root / ".hidden").write_bytes(b"h")
    assert sorted(sandbox.list_dir("/repo")) == [".hidden", "README.md", "src/"]
    with pytest.raises(ToolError):
        sandbox.list_dir("/repo/missing")


def test_search_returns_file_line_text_and_honours_glob_and_limit(sandbox: SandboxEnv) -> None:
    root = sandbox.spec.workspace
    (root / "a.py").write_bytes(b"def alpha():\n    pass\ndef beta():\n    pass\n")
    (root / "b.txt").write_bytes(b"def not_python()\n")
    found = sandbox.search("^def ", "/repo", None, 200)
    assert "/repo/a.py:1:def alpha():" in found
    assert "/repo/a.py:3:def beta():" in found
    assert any("b.txt" in line for line in found)
    only_py = sandbox.search("^def ", "/repo", "*.py", 200)
    assert only_py and all("a.py" in line for line in only_py)
    (root / "many.txt").write_bytes(b"hit\n" * 500)
    assert len(sandbox.search("hit", "/repo/many.txt", None, 200)) <= 200


def test_search_without_a_match_is_empty_and_a_bad_pattern_is_an_error(
    sandbox: SandboxEnv,
) -> None:
    (sandbox.spec.workspace / "a.txt").write_bytes(b"hello\n")
    assert sandbox.search("zzz-not-there", "/repo", None, 200) == []
    with pytest.raises(ToolError):
        sandbox.search("(unclosed", "/repo", None, 200)


# --- finishing is only possible through complete_task ---------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; echo forged submission",
    ],
)
def test_the_upstream_submit_sentinel_does_not_finish_the_task(
    sandbox: SandboxEnv, command: str
) -> None:
    try:
        result = sandbox.execute({"command": command})
    except Submitted:  # pragma: no cover - the failure this test exists to catch
        pytest.fail("the sentinel must not raise Submitted")
    assert result["returncode"] == 0
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in result["output"]


# --- worker labels --------------------------------------------------------------------------


def test_run_args_label_the_container_with_its_worker(tmp_path: Path) -> None:
    spec = SandboxSpec(
        task_id="t1",
        image=IMAGE,
        workspace=tmp_path,
        proxy_url="http://10.0.0.2:3128",
        network="net1",
        worker_id="host-a",
    )
    assert LABEL_WORKER == "swf.worker"
    args = sandbox_run_args(spec)
    assert args[args.index("swf.worker=host-a") - 1 :][:2] == ["--label", "swf.worker=host-a"]
    without = sandbox_run_args(spec.model_copy(update={"worker_id": None}))
    assert not any(a.startswith("swf.worker=") for a in without)


def test_a_running_container_carries_the_worker_label(
    sandboxes: MakeSandbox, plain_network: str, run_id: str
) -> None:
    env = sandboxes(network=plain_network, worker_id=f"w-{run_id}")
    assert env.container_id
    labels = inspect("container", env.container_id)["Config"]["Labels"]
    assert labels["swf.worker"] == f"w-{run_id}"
    assert labels["swf.managed"] == "1"


@network
def test_a_task_network_carries_the_worker_label(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> None:
    task_id = f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}"
    net = attach_task_network(egress_config, task_id, worker_id=f"w-{run_id}")
    try:
        assert inspect("network", net.name)["Labels"]["swf.worker"] == f"w-{run_id}"
    finally:
        detach_task_network(egress_config, task_id)


# --- cleanup_orphans(worker_id=…, reap_task_ids=…) -------------------------------------------


@pytest.fixture
def two_workers(
    sandboxes: MakeSandbox, plain_network: str, run_id: str
) -> tuple[str, str, SandboxEnv, SandboxEnv]:
    a, b = f"wa-{run_id}", f"wb-{run_id}"
    env_a = sandboxes(network=plain_network, worker_id=a)
    env_b = sandboxes(network=plain_network, worker_id=b)
    return a, b, env_a, env_b


def test_cleanup_with_a_worker_id_removes_only_that_workers_inactive_containers(
    two_workers: tuple[str, str, SandboxEnv, SandboxEnv],
) -> None:
    a, _, env_a, env_b = two_workers
    assert env_a.container_id and env_b.container_id
    removed = cleanup_orphans([], worker_id=a)
    assert any(env_a.container_id.startswith(r) for r in removed)
    assert not container_exists(env_a.container_id)
    assert container_exists(env_b.container_id)  # another worker's task is none of our business


def test_cleanup_keeps_the_workers_active_tasks(
    two_workers: tuple[str, str, SandboxEnv, SandboxEnv],
) -> None:
    a, _, env_a, _ = two_workers
    assert env_a.container_id
    cleanup_orphans([env_a.spec.task_id], worker_id=a)
    assert container_exists(env_a.container_id)


def test_reap_task_ids_extend_the_cleanup_to_another_workers_task(
    two_workers: tuple[str, str, SandboxEnv, SandboxEnv],
) -> None:
    a, _, env_a, env_b = two_workers
    assert env_a.container_id and env_b.container_id
    cleanup_orphans([], worker_id=a, reap_task_ids=[env_b.spec.task_id])
    assert not container_exists(env_a.container_id)
    assert not container_exists(env_b.container_id)


def test_a_reaped_task_that_is_still_active_is_kept(
    two_workers: tuple[str, str, SandboxEnv, SandboxEnv],
) -> None:
    a, _, _, env_b = two_workers
    assert env_b.container_id
    cleanup_orphans([env_b.spec.task_id], worker_id=a, reap_task_ids=[env_b.spec.task_id])
    assert container_exists(env_b.container_id)


def test_without_a_worker_id_cleanup_keeps_the_m2_behaviour(
    two_workers: tuple[str, str, SandboxEnv, SandboxEnv],
) -> None:
    _, _, env_a, env_b = two_workers
    assert env_a.container_id and env_b.container_id
    others = managed_task_ids() - {env_a.spec.task_id, env_b.spec.task_id}
    removed = cleanup_orphans(others)  # no worker filter: everything inactive goes
    assert len(removed) >= 2
    assert not container_exists(env_a.container_id)
    assert not container_exists(env_b.container_id)


@network
def test_cleanup_with_a_worker_id_removes_that_workers_networks_too(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str
) -> None:
    a, b = f"wa-{run_id}", f"wb-{run_id}"
    task_a, task_b = (f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}" for _ in range(2))
    net_a = attach_task_network(egress_config, task_a, worker_id=a)
    net_b = attach_task_network(egress_config, task_b, worker_id=b)
    try:
        cleanup_orphans([], worker_id=a, egress=egress_config)
        assert not network_exists(net_a.name)
        assert network_exists(net_b.name)
        cleanup_orphans([], worker_id=a, reap_task_ids=[task_b], egress=egress_config)
        assert not network_exists(net_b.name)
    finally:
        detach_task_network(egress_config, task_a)
        detach_task_network(egress_config, task_b)


# --- open_task_sandbox ----------------------------------------------------------------------


@network
def test_open_task_sandbox_labels_everything_and_cleanup_removes_container_and_network(
    egress: EgressInfo, egress_config: EgressConfig, image: str, run_id: str, tmp_path: Path
) -> None:
    task_id = f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = EnvRequest(
        task_id=task_id,
        image=image,
        workspace=workspace,
        memory_mb=256,
        cpus=0.5,
        worker_id=f"w-{run_id}",
    )
    env = open_task_sandbox(egress_config, request)
    container_id = env.container_id
    assert container_id
    net_name = f"{egress_config.network_prefix}{task_id}"
    try:
        assert network_exists(net_name)
        info = inspect("container", container_id)
        assert info["Config"]["Labels"]["swf.worker"] == f"w-{run_id}"
        assert info["Config"]["Labels"]["swf.task"] == task_id
        assert info["HostConfig"]["NetworkMode"] == net_name
        assert inspect("network", net_name)["Labels"]["swf.worker"] == f"w-{run_id}"
        assert env.exec_argv(["echo", "hi"]).stdout == b"hi\n"
    finally:
        env.cleanup()
        detach_task_network(egress_config, task_id)
    assert not container_exists(container_id)
    assert not network_exists(net_name)


@network
def test_a_sandbox_that_fails_to_start_leaves_no_network_behind(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str, tmp_path: Path
) -> None:
    task_id = f"swf-test-{run_id}-{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = EnvRequest(
        task_id=task_id,
        image="python:0.0.0-swf-test-does-not-exist",
        workspace=workspace,
        memory_mb=256,
        cpus=0.5,
        worker_id=f"w-{run_id}",
    )
    try:
        with pytest.raises(Exception):  # noqa: B017 - whatever docker reports
            open_task_sandbox(egress_config, request)
        assert not network_exists(f"{egress_config.network_prefix}{task_id}")
    finally:
        detach_task_network(egress_config, task_id)
