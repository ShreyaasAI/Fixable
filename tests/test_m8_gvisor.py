"""gVisor (`runsc`) sandbox checks (contract §0.2, §3 T1).

Marked `gvisor`: skipped with a reason unless `SWF_SANDBOX_RUNTIME=runsc` and `docker info` lists
`runsc` (see `tests/conftest.py`). The last test is the opposite: it runs only where `runsc` is
absent.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from swf.egress import EgressConfig, EgressInfo
from swf.sandbox import (
    SandboxRuntimeError,
    check_runtime,
    open_task_sandbox,
    parse_runtimes,
    sandbox_user,
)
from swf.worker import EnvRequest

IMAGE = "python:3.12-slim"


def _docker_runtimes() -> frozenset[str]:
    out = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return parse_runtimes(out.stdout.strip())


@pytest.mark.docker
@pytest.mark.gvisor
@pytest.mark.network
def test_check_runtime_accepts_runsc() -> None:
    check_runtime("runsc")


@pytest.mark.docker
@pytest.mark.gvisor
@pytest.mark.network
def test_sandbox_runs_under_runsc(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str, tmp_path: Path
) -> None:
    request = EnvRequest(
        task_id=f"swf-test-{run_id}-gv{uuid.uuid4().hex[:6]}",
        image=IMAGE,
        workspace=tmp_path,
        memory_mb=512,
        cpus=0.5,
    )
    env = open_task_sandbox(egress_config, request)
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "-f", "{{.HostConfig.Runtime}}", env.container_id],
            capture_output=True,
            text=True,
            check=True,
        )
        assert inspected.stdout.strip() == "runsc"
        assert "gVisor" in env.execute({"command": "dmesg"})["output"]
        assert env.execute({"command": "id -u"})["output"].strip() == sandbox_user().split(":")[0]
    finally:
        env.cleanup()


@pytest.mark.docker
def test_check_runtime_raises_when_runsc_missing() -> None:
    if "runsc" in _docker_runtimes():
        pytest.skip("runsc is installed in this Docker daemon")
    with pytest.raises(SandboxRuntimeError):
        check_runtime("runsc")
