"""T5-T8 review fixes for the eval harness: the infra-failure classifier, run state persisted as
tasks finish, the package-cache dir the harness and the worker share, cancel waiting for a
terminal status, approval/answer POSTs that are retried on failure, and the egress-network label
check. Fakes only; no Docker, no network."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from m9_eval_fakes import ALLOWED_REPO, rec

import swf_eval.__main__ as cli
from swf import egress
from swf.egress import EgressConfig, EgressError
from swf_eval.budget import Budget, SharedBudget
from swf_eval.client import EvalClient
from swf_eval.localstack import LocalStack
from swf_eval.seeds import load_seeds

# --- 1. infra-failure classifier -----------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        "clone_failed",
        "pr_failed: push",
        "pr_failed: create_pull",
        "pr_failed: commit",
        "internal_error: EgressError",
        "internal_error: SandboxRuntimeError",
        "internal_error: SandboxError",
    ],
)
def test_infra_failures_are_re_run(error: str) -> None:
    assert cli.is_infra_failure("failed", error)


@pytest.mark.parametrize(
    "error",
    [
        None,
        "",
        "pr_failed: empty_diff",
        "pr_failed: unsupported_change: bin/blob.dat",
        "retries_exhausted",
        "limits_exceeded",
        "time_exceeded",
        "internal_error: KeyError",
        "internal_error: RuntimeError: docker said no",
        "docker daemon not reachable",
        "baseline_not_reproduced",
        "clone_failed_extra",
    ],
)
def test_agent_outcomes_are_not_re_run(error: str | None) -> None:
    assert not cli.is_infra_failure("failed", error)


def test_only_a_failed_status_is_infra() -> None:
    assert not cli.is_infra_failure("completed", "clone_failed")
    assert not cli.is_infra_failure("cancelled", "clone_failed")


# --- 2. run state persisted as each task completes -----------------------------------------


def make_state(tmp_path: Path) -> cli.RunState:
    return cli.RunState(
        repo=ALLOWED_REPO,
        db_url="postgresql://unused",
        budget=SharedBudget(Budget()),
        clients={},
        out=tmp_path / "out",
        info={"run_id": "r1", "repo": ALLOWED_REPO},
    )


def test_an_exception_mid_run_leaves_run_json_with_the_recorded_pr_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = load_seeds()[:3]
    state = make_state(tmp_path)
    done = rec(
        "single", seeds[0].name, 10.0, False,
        task_id=str(uuid.UUID(int=1)), pr_url=f"https://github.com/{ALLOWED_REPO}/pull/41",
    )  # fmt: skip
    calls: list[str] = []

    def fake_run_task(st: cli.RunState, pass_: Any, seed: Any, user: str) -> Any:
        calls.append(seed.name)
        if len(calls) == 1:
            st.ids[done.task_id] = (pass_, seed.name, user)
            return done
        raise RuntimeError("the API went away")

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    cli.drive_jobs(state, "single", [(s, "eval-a") for s in seeds])  # must not raise
    assert calls == [seeds[0].name, seeds[1].name]  # stops the thread's jobs after the error
    assert any("RuntimeError" in n and "the API went away" in n for n in state.notes)
    run = json.loads((state.out / "run.json").read_text("utf-8"))
    assert run["run_id"] == "r1"
    assert run["pr_urls"] == {done.task_id: done.pr_url}
    assert run["ids"] == {done.task_id: ["single", seeds[0].name, "eval-a"]}
    assert any("the API went away" in n for n in run["notes"])
    task = json.loads((state.out / "tasks" / f"single-{seeds[0].name}.json").read_text("utf-8"))
    assert task["pr_url"] == done.pr_url


def test_an_infra_failure_is_replaced_and_both_states_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = load_seeds()[0]
    state = make_state(tmp_path)
    first = rec("single", seed.name, 5.0, False, "failed", task_id=str(uuid.UUID(int=1)),
                error="clone_failed")  # fmt: skip
    second = rec("single", seed.name, 9.0, True, task_id=str(uuid.UUID(int=2)))
    queue = [first, second]

    def fake_run_task(st: cli.RunState, pass_: Any, s: Any, user: str) -> Any:
        r = queue.pop(0)
        st.ids[r.task_id] = (pass_, s.name, user)
        return r

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    cli.drive_jobs(state, "single", [(seed, "eval-a")])
    run = json.loads((state.out / "run.json").read_text("utf-8"))
    assert list(run["ids"]) == [second.task_id]
    assert [x["task_id"] for x in run["replaced"]] == [first.task_id]


# --- 3. the package-cache dir is shared by the harness and the worker ----------------------


def test_the_default_pkgcache_dir_is_the_repo_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWF_PKGCACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    root = Path(egress.__file__).resolve().parents[2]
    assert EgressConfig().pkgcache_dir == (root / "pkgcache").resolve()


@pytest.mark.parametrize("from_env", [False, True])
def test_localstack_passes_the_harness_pkgcache_dir_to_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_env: bool
) -> None:
    if from_env:
        monkeypatch.setenv("SWF_PKGCACHE_DIR", str(tmp_path / "cache"))
    else:
        monkeypatch.delenv("SWF_PKGCACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    stack = LocalStack(tmp_path / "run", github_id=1, github_login="u", knobs={})
    env = stack.env()
    assert env["SWF_PKGCACHE_DIR"] == str(EgressConfig().pkgcache_dir)
    # the worker computes its EgressConfig from exactly that env: same dir, same conf-sha input
    monkeypatch.setenv("SWF_PKGCACHE_DIR", env["SWF_PKGCACHE_DIR"])
    assert EgressConfig().pkgcache_dir == Path(env["SWF_PKGCACHE_DIR"])


# --- 4. cancel waits for a terminal status; approval/answer POSTs are checked ---------------

TASK = uuid.UUID(int=7)
APPROVAL = uuid.UUID(int=8)


def sse(events: list[tuple[int, str, dict[str, Any]]], after: int) -> bytes:
    return b"".join(
        f"id: {i}\nevent: {t}\ndata: {json.dumps(d)}\n\n".encode()
        for i, t, d in events
        if i > after
    )


class FakeApi:
    def __init__(self, statuses: list[str], events: list[tuple[int, str, dict[str, Any]]]) -> None:
        self.statuses = statuses
        self.events = events
        self.requests: list[httpx.Request] = []
        self.approval_codes = [500, 200]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelled"})
        if request.method == "GET" and path == f"/v1/tasks/{TASK}":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"status": status})
        if request.method == "GET" and path.endswith("/events"):
            after = int(request.headers.get("Last-Event-ID", "-1"))
            return httpx.Response(
                200, content=sse(self.events, after), headers={"content-type": "text/event-stream"}
            )
        if request.method == "POST" and "/approvals/" in path:
            return httpx.Response(self.approval_codes.pop(0) if self.approval_codes else 200)
        return httpx.Response(404)

    def client(self) -> EvalClient:
        http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(self.handler))
        client = EvalClient(http, "jwt")
        client.poll_seconds = 0.0
        return client

    def count(self, method: str, fragment: str) -> int:
        return sum(1 for r in self.requests if r.method == method and fragment in r.url.path)


def test_give_up_polls_until_the_task_is_terminal() -> None:
    api = FakeApi(["executing", "executing", "cancelled"], [])
    result = api.client().drive(TASK, deadline=1.0, clock=lambda: 2.0)
    assert result.status == "cancelled"
    assert api.count("POST", "/cancel") == 1
    assert api.count("GET", f"/v1/tasks/{TASK}") == 3


def test_give_up_stops_polling_after_the_timeout() -> None:
    api = FakeApi(["executing"], [])
    client = api.client()
    client.cancel_wait_seconds = 0.05
    result = client.drive(TASK, deadline=1.0, clock=lambda: 2.0)
    assert result.status == "executing"  # reported as it is; the caller records it
    assert api.count("GET", f"/v1/tasks/{TASK}") >= 1


def test_a_failed_approval_post_is_retried_and_counted_once() -> None:
    events = [
        (1, "approval_requested", {"approval_id": str(APPROVAL), "kind": "plan", "payload": {}}),
        (2, "status", {"from": "awaiting_plan_approval", "to": "completed"}),
    ]
    api = FakeApi(["completed"], events)
    result = api.client().drive(TASK, deadline=10**12)
    assert result.status == "completed"
    assert api.count("POST", "/approvals/") == 2  # 500, then retried -> 200
    assert result.approvals == {"plan": 1}
    gets = [r for r in api.requests if r.url.path.endswith("/events")]
    assert len(gets) >= 2 and "Last-Event-ID" not in gets[1].headers  # event 1 redelivered


# --- minor: egress network must carry the swf.egress=1 label --------------------------------


def test_an_unlabelled_internal_egress_network_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    net = {"Internal": True, "Labels": {}}
    monkeypatch.setattr(egress, "_inspect", lambda *_a: net)
    monkeypatch.setattr(
        egress, "_docker", lambda *a, **k: subprocess.CompletedProcess(list(a), 0, "", "")
    )
    with pytest.raises(EgressError, match="swf.egress"):
        egress._ensure_egress_network(EgressConfig(), "docker")
    net["Labels"] = {"swf.egress": "1"}
    egress._ensure_egress_network(EgressConfig(), "docker")
