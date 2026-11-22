"""M9 T1: the sandbox runs as the host uid:gid, so the bind-mounted workspace needs no chown (0.7).

POSIX hosts only: on Windows/Docker Desktop the sandbox keeps 1000:1000 and ownership is virtual.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from swf.egress import EgressConfig, EgressInfo, detach_task_network
from swf.sandbox import open_task_sandbox, sandbox_user
from swf.worker import EnvRequest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.skipif(
        not hasattr(os, "getuid"), reason="host-uid ownership applies to POSIX hosts only"
    ),
]

IMAGE = "python:3.12-slim"


def test_workspace_is_writable_and_owned_by_host_user(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    task_id = f"swf-test-{run_id}-own{uuid.uuid4().hex[:6]}"
    env = open_task_sandbox(
        egress_config,
        EnvRequest(task_id=task_id, image=IMAGE, workspace=ws, memory_mb=256, cpus=0.5),
    )
    try:
        assert env.execute({"command": "id -u"})["output"].strip() == str(os.getuid())
        made = env.execute(
            {"command": "touch /repo/agent.txt && mkdir /repo/d && echo x > /repo/d/f"}
        )
        assert made["returncode"] == 0, made
        assert (ws / "agent.txt").stat().st_uid == os.getuid()
        owner = env.execute({"command": "stat -c %u:%g /repo/d/f"})["output"].strip()
        assert owner == sandbox_user()
    finally:
        env.cleanup()
        detach_task_network(egress_config, task_id)
    shutil.rmtree(ws)
    assert not ws.exists()
