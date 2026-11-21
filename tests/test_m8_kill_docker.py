"""M8: a global admin kill removes a running task's real container and network within 30 s
(m8-contract §0.12, §3 T9). Real `open_task_sandbox` over the session egress proxy, default
`stop_poll_seconds` (5). Runs under runsc on the VM through the `SWF_SANDBOX_RUNTIME` default.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from conftest import Clock, build_deps
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeRegistry
from m4_fakes import SEED_REPO, ScriptedModel, make_db_user, queued_task, task_row, wait_until

from swf import policy
from swf.agent.web_agent import AcceptAllEvidence
from swf.api.app import create_app
from swf.auth import JwtVerifier
from swf.db import Database
from swf.egress import EgressConfig, EgressInfo, detach_task_network
from swf.sandbox import open_task_sandbox
from swf.tokens import TokenCipher
from swf.worker import LocalCopyWorkspace, RunnerDeps, Worker, WorkerConfig

pytestmark = [pytest.mark.docker, pytest.mark.network]

IMAGE = "python:3.12-slim"
SLEEP = "sleep 600"
DEADLINE_SECONDS = 30.0


def docker(*args: str, timeout: float = 120) -> str:
    run = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    return run.stdout.strip()


def by_task(task_id: str) -> tuple[str, str]:
    """Container ids (any state) and network ids labelled with the task."""
    label = f"label=swf.task={task_id}"
    return (
        docker("ps", "-aq", "--filter", label),
        docker("network", "ls", "-q", "--filter", label),
    )


def write_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    shutil.copytree(SEED_REPO, source)
    config = {
        "image": IMAGE,
        "setup": [SLEEP],
        "test": "true",
        "allow": [],
        "resources": {"memory_mb": 512, "cpus": 0.5},
    }
    text = "# Pager\n\n```yaml agent-config\n" + yaml.safe_dump(config) + "```\n"
    (source / "AGENTS.md").write_text(text, encoding="utf-8", newline="\n")
    return source


def test_global_kill_removes_the_container_and_network_within_30_seconds(
    fresh_db: Database,
    egress: EgressInfo,
    egress_config: EgressConfig,
    tmp_path: Path,
    verifier: JwtVerifier,
    cipher: TokenCipher,
    github: FakeGitHub,
    registry: FakeRegistry,
    clock: Clock,
    auth: Callable[..., dict[str, str]],
) -> None:
    assert policy.classify(SLEEP, phase="prepare").decision != "forbid"
    if not docker("image", "ls", "-q", IMAGE):
        pulled = subprocess.run(["docker", "pull", IMAGE], capture_output=True, timeout=300)
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {IMAGE}")

    admin = make_db_user(fresh_db)
    with fresh_db.backend() as conn:
        conn.execute("update public.profiles set is_admin = true where user_id = %s", [admin.id])
    task = queued_task(fresh_db, image=IMAGE)
    task_id = str(task.id)

    config = WorkerConfig(  # stop_poll_seconds stays at the default (5)
        worker_id="w1",
        workspaces_root=tmp_path / "workspaces",
        poll_seconds=0.2,
        agent={"poll_seconds": 0.05},
    )
    deps = RunnerDeps(
        db=fresh_db,
        workspaces=LocalCopyWorkspace(write_source(tmp_path), config.workspaces_root),
        env_factory=lambda req: open_task_sandbox(egress_config, req),
        model_factory=lambda: ScriptedModel([]),
        config=config,
        evidence=AcceptAllEvidence(),
    )
    worker = Worker(deps, orphan_cleaner=lambda *a, **k: [])
    stop = threading.Event()
    server = threading.Thread(target=lambda: worker.serve(stop), daemon=True)
    api = TestClient(create_app(build_deps(fresh_db, verifier, cipher, github, registry, clock)))
    server.start()
    try:
        wait_until(lambda: by_task(task_id)[0], timeout=120, what="the task container")
        with fresh_db.backend() as conn:
            setup_events = conn.execute(
                "select 1 from public.events where task_id = %s and type = 'setup'", [task.id]
            ).fetchall()
        assert setup_events == []  # still inside `sleep 600`

        t0 = time.monotonic()
        resp = api.post("/v1/admin/kill", json={}, headers=auth(admin))
        assert resp.status_code == 200, resp.text
        assert resp.json()["cancelled"] == [task_id]

        def gone() -> bool:
            return by_task(task_id) == ("", "")

        deadline = t0 + DEADLINE_SECONDS + 15  # poll a little past the limit to report a number
        while not gone() and time.monotonic() < deadline:
            time.sleep(0.25)
        elapsed = time.monotonic() - t0
        print(f"\nM8 kill: container and network gone after {elapsed:.1f} s (limit 30 s)")
        assert gone(), "container or network still present"
        assert elapsed <= DEADLINE_SECONDS
        assert task_row(fresh_db, task.id)["status"] == "cancelled"
    finally:
        containers, _ = by_task(task_id)
        for cid in containers.split():  # unblocks an in-flight `docker exec` so serve can stop
            docker("rm", "-f", cid)
        stop.set()
        server.join(timeout=60)
        api.close()
        with contextlib.suppress(Exception):
            detach_task_network(egress_config, task_id)
        for cid in by_task(task_id)[0].split():
            docker("rm", "-f", cid)
        for nid in by_task(task_id)[1].split():
            docker("network", "rm", nid)
