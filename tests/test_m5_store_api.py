"""Evidence in the database and the API (m5-contract §3, §5, §12 A1/A2): `add_evidence`,
`list_evidence`, the migration's constraints, RLS, `PgEvidenceSink`, `GET /evidence`, and the
`readonly_violation` approval kind. Runs against a private database clone (`fresh_db`)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from helpers import FakeUser
from m4_fakes import make_db_user, queued_task
from m5_fakes import evidence_rows

from swf import store
from swf.db import Database
from swf.evidence import EvidenceRecord, PgEvidenceSink

pytest_plugins = ["m4_fakes", "m5_fakes"]

AuthFn = Callable[..., dict[str, str]]
DIGEST = "a" * 64


def new_task(db: Database) -> tuple[FakeUser, store.Task]:
    user = make_db_user(db)
    return user, queued_task(db, user=user)


def add(db: Database, task_id: uuid.UUID, **kwargs: Any) -> store.EvidenceRow:
    kwargs.setdefault("kind", "baseline")
    with db.backend() as conn:
        return store.add_evidence(conn, task_id, **kwargs)


# --- add_evidence ---------------------------------------------------------------------------


def test_add_evidence_round_trips_every_field(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    row = add(
        fresh_db,
        task.id,
        kind="post",
        source="agent_run",
        command="python -m pytest -q tests/test_pager.py::test_second_page",
        selector="tests/test_pager.py::test_second_page",
        exit_code=0,
        output="1 passed",
        output_truncated=True,
        output_ref="evidence/003-post.txt",
        failures=["tests/b.py::t", "tests/a.py::t"],
        duration_ms=1234,
        workspace_digest=DIGEST,
        tool_call_id="call_9",
        details={"covers_target": True},
    )
    assert isinstance(row.id, int)
    assert row.task_id == task.id
    assert (row.kind, row.source, row.exit_code, row.duration_ms) == ("post", "agent_run", 0, 1234)
    assert row.command is not None and row.command.startswith("python -m pytest")
    assert row.selector == "tests/test_pager.py::test_second_page"
    assert (row.output, row.output_truncated) == ("1 passed", True)
    assert row.output_ref == "evidence/003-post.txt"
    assert row.failures == ["tests/a.py::t", "tests/b.py::t"]  # stored sorted
    assert (row.workspace_digest, row.tool_call_id) == (DIGEST, "call_9")
    assert row.details == {"covers_target": True}
    assert row.created_at is not None
    assert evidence_rows(fresh_db, task.id) == [row]


def test_add_evidence_defaults(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    row = add(fresh_db, task.id, kind="baseline")
    assert row.source == "harness"
    assert row.failures == []
    assert row.details == {}
    assert row.output_truncated is False
    assert (row.command, row.selector, row.exit_code, row.output) == (None, None, None, None)
    assert (row.output_ref, row.duration_ms, row.workspace_digest) == (None, None, None)


def test_add_evidence_emits_an_evidence_recorded_event_in_the_same_transaction(
    fresh_db: Database,
) -> None:
    user, task = new_task(fresh_db)
    row = add(
        fresh_db,
        task.id,
        kind="suite",
        source="agent_run",
        exit_code=1,
        selector=None,
        workspace_digest=DIGEST,
    )
    with fresh_db.as_user(user.claims()) as conn:
        events = [
            e for e in store.list_events(conn, user.id, task.id) if e.type == "evidence_recorded"
        ]
    assert [e.payload for e in events] == [
        {
            "evidence_id": row.id,
            "kind": "suite",
            "source": "agent_run",
            "exit_code": 1,
            "selector": None,
            "workspace_digest": DIGEST,
        }
    ]


def test_a_failed_insert_leaves_no_event_behind(fresh_db: Database) -> None:
    user, task = new_task(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        add(fresh_db, task.id, kind="not-a-kind")
    with fresh_db.as_user(user.claims()) as conn:
        assert [
            e for e in store.list_events(conn, user.id, task.id) if e.type == "evidence_recorded"
        ] == []


# --- constraints ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["baseline", "baseline_suite", "post", "suite", "harness_rerun", "diff"]
)
def test_every_documented_kind_is_accepted(fresh_db: Database, kind: str) -> None:
    _, task = new_task(fresh_db)
    assert add(fresh_db, task.id, kind=kind).kind == kind


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "nonsense"},
        {"source": "agent"},
        {"workspace_digest": "xyz"},
        {"workspace_digest": "A" * 64},  # uppercase hex
        {"workspace_digest": "a" * 63},
        {"selector": "s" * 301},
        {"output": "o" * 20001},
        {"duration_ms": -1},
    ],
    ids=[
        "kind",
        "source",
        "digest-short",
        "digest-upper",
        "digest-63",
        "selector",
        "output",
        "duration",
    ],
)
def test_the_migration_constraints_reject_bad_values(
    fresh_db: Database, bad: dict[str, Any]
) -> None:
    _, task = new_task(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        add(fresh_db, task.id, **bad)


def test_the_limits_themselves_are_accepted(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    row = add(fresh_db, task.id, selector="s" * 300, output="o" * 20000, workspace_digest="0" * 64)
    assert len(row.selector or "") == 300
    assert len(row.output or "") == 20000


@pytest.mark.parametrize(
    ("column", "value"),
    [("failures", '"not-an-array"'), ("failures", "{}"), ("details", "[]"), ("details", "3")],
)
def test_failures_must_be_an_array_and_details_an_object(
    fresh_db: Database, column: str, value: str
) -> None:
    _, task = new_task(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation), fresh_db.backend() as conn:
        conn.execute(
            f"insert into public.evidence (task_id, kind, {column})"
            " values (%s, 'baseline', %s::jsonb)",
            [task.id, value],
        )


def test_the_m1_insert_with_only_task_and_kind_stays_valid(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        conn.execute(
            "insert into public.evidence (task_id, kind) values (%s, 'baseline')", [task.id]
        )
        count = conn.execute(
            "select count(*) as n from public.evidence where task_id = %s", [task.id]
        ).fetchone()
    assert count is not None and count["n"] == 1


# --- list_evidence and RLS ------------------------------------------------------------------


def test_list_evidence_is_ordered_by_id(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    ids = [
        add(fresh_db, task.id, kind=k).id for k in ("baseline", "baseline_suite", "post", "diff")
    ]
    assert [r.id for r in evidence_rows(fresh_db, task.id)] == ids
    assert ids == sorted(ids)


def test_another_user_sees_none_of_the_rows(fresh_db: Database) -> None:
    a, task = new_task(fresh_db)
    b = make_db_user(fresh_db)
    add(fresh_db, task.id, kind="baseline")
    add(fresh_db, task.id, kind="post")
    with fresh_db.as_user(b.claims()) as conn:
        assert store.list_evidence(conn, b.id, task.id) == []
        seen = conn.execute("select count(*) as n from public.evidence").fetchone()
    assert seen is not None and seen["n"] == 0
    with fresh_db.as_user(a.claims()) as conn:
        assert len(store.list_evidence(conn, a.id, task.id)) == 2
        seen = conn.execute("select count(*) as n from public.evidence").fetchone()
    assert seen is not None and seen["n"] == 2


def test_the_user_role_cannot_write_evidence(fresh_db: Database) -> None:
    a, task = new_task(fresh_db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), fresh_db.as_user(a.claims()) as conn:
        conn.execute("insert into public.evidence (task_id, kind) values (%s, 'post')", [task.id])


def test_evidence_of_one_task_is_not_listed_for_another(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    one = queued_task(fresh_db, user=user)
    two = queued_task(fresh_db, user=user)
    add(fresh_db, one.id, kind="baseline")
    assert evidence_rows(fresh_db, two.id) == []


# --- PgEvidenceSink -------------------------------------------------------------------------


def test_the_pg_sink_writes_a_row_and_returns_its_id(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    sink = PgEvidenceSink(fresh_db, task.id)
    record = EvidenceRecord(
        kind="post",
        source="agent_run",
        command="pytest",
        selector="tests/t.py::a",
        exit_code=0,
        output="ok",
        output_ref="evidence/002-post.txt",
        failures=frozenset({"tests/z.py::z", "tests/a.py::a"}),
        duration_ms=5,
        workspace_digest=DIGEST,
        tool_call_id="c1",
        covers_target=True,
        details={"note": "n"},
    )
    evidence_id = sink.add(record)
    (row,) = evidence_rows(fresh_db, task.id)
    assert row.id == evidence_id
    assert (row.kind, row.source, row.exit_code, row.selector) == (
        "post",
        "agent_run",
        0,
        "tests/t.py::a",
    )
    assert row.failures == ["tests/a.py::a", "tests/z.py::z"]
    assert row.workspace_digest == DIGEST
    assert row.details["covers_target"] is True
    assert row.details["note"] == "n"


def test_a_stopped_sink_writes_nothing_and_returns_minus_one(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    stopped = {"on": False}
    sink = PgEvidenceSink(fresh_db, task.id, is_stopped=lambda: stopped["on"])
    assert sink.add(EvidenceRecord(kind="baseline", source="harness")) > 0
    stopped["on"] = True
    assert sink.add(EvidenceRecord(kind="post", source="agent_run")) == -1
    assert [r.kind for r in evidence_rows(fresh_db, task.id)] == ["baseline"]


# --- GET /v1/tasks/{id}/evidence ------------------------------------------------------------


def test_get_evidence_returns_the_task_status_and_the_rows_in_order(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)
    first = add(fresh_db, task.id, kind="baseline", exit_code=1, failures=["tests/a.py::a"])
    second = add(
        fresh_db, task.id, kind="post", source="agent_run", exit_code=0, workspace_digest=DIGEST
    )
    res = fresh_client.get(f"/v1/tasks/{task.id}/evidence", headers=auth(user))
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"task_id", "status", "items"}
    assert body["task_id"] == str(task.id)
    assert body["status"] == "queued"
    assert [i["id"] for i in body["items"]] == [first.id, second.id]
    item = body["items"][0]
    assert {
        "id", "task_id", "kind", "source", "command", "selector", "exit_code", "output",
        "output_truncated", "output_ref", "failures", "duration_ms", "workspace_digest",
        "tool_call_id", "details", "created_at",
    } <= set(item)  # fmt: skip
    assert item["failures"] == ["tests/a.py::a"]
    assert item["kind"] == "baseline"
    assert body["items"][1]["workspace_digest"] == DIGEST


def test_get_evidence_of_a_task_without_rows_is_an_empty_list(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)
    res = fresh_client.get(f"/v1/tasks/{task.id}/evidence", headers=auth(user))
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_get_evidence_of_another_users_task_is_a_404(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    _, task = new_task(fresh_db)
    add(fresh_db, task.id, kind="baseline")
    other = make_db_user(fresh_db)
    assert fresh_client.get(f"/v1/tasks/{task.id}/evidence", headers=auth(other)).status_code == 404


def test_get_evidence_of_an_unknown_task_is_a_404(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user = make_db_user(fresh_db)
    res = fresh_client.get(f"/v1/tasks/{uuid.uuid4()}/evidence", headers=auth(user))
    assert res.status_code == 404


def test_get_evidence_without_a_token_is_a_401(
    fresh_client: TestClient, fresh_db: Database
) -> None:
    _, task = new_task(fresh_db)
    assert fresh_client.get(f"/v1/tasks/{task.id}/evidence").status_code == 401


def test_reading_evidence_writes_no_audit_row(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)

    def audit_rows() -> int:
        with fresh_db.backend() as conn:
            row = conn.execute(
                "select count(*) as n from public.audit_log where user_id = %s", [user.id]
            ).fetchone()
        assert row is not None
        return int(row["n"])

    before = audit_rows()
    fresh_client.get(f"/v1/tasks/{task.id}/evidence", headers=auth(user))
    assert audit_rows() == before


# --- the readonly_violation approval kind (A1) ----------------------------------------------


def request_readonly(db: Database, task_id: uuid.UUID) -> store.Approval:
    with db.backend() as conn:
        return store.create_approval(
            conn,
            task_id,
            "readonly_violation",
            {
                "added": ["notes.txt"],
                "counts": {"added": 1},
                "diff": "--- /dev/null",
                "diff_truncated": False,
            },
        )


def test_the_approvals_kind_constraint_accepts_readonly_violation(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    approval = request_readonly(fresh_db, task.id)
    assert approval.kind == "readonly_violation"
    assert approval.status == "pending"


@pytest.mark.parametrize("kind", ["plan", "command", "guard_flag"])
def test_the_older_approval_kinds_still_work(fresh_db: Database, kind: str) -> None:
    _, task = new_task(fresh_db)
    with fresh_db.backend() as conn:
        assert store.create_approval(conn, task.id, kind, {"x": 1}).kind == kind


def test_the_approvals_kind_constraint_rejects_unknown_kinds(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    with pytest.raises(psycopg.errors.CheckViolation), fresh_db.backend() as conn:
        store.create_approval(conn, task.id, "something_else", {})


def test_the_submitter_can_decide_a_readonly_violation_through_the_api(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)
    approval = request_readonly(fresh_db, task.id)
    res = fresh_client.post(
        f"/v1/tasks/{task.id}/approvals/{approval.id}",
        json={"decision": "approve"},
        headers=auth(user),
    )
    assert res.status_code == 200
    assert res.json()["status"] == "approved"
    assert res.json()["kind"] == "readonly_violation"


def test_another_user_cannot_decide_a_readonly_violation(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    _, task = new_task(fresh_db)
    approval = request_readonly(fresh_db, task.id)
    other = make_db_user(fresh_db)
    res = fresh_client.post(
        f"/v1/tasks/{task.id}/approvals/{approval.id}",
        json={"decision": "approve"},
        headers=auth(other),
    )
    assert res.status_code == 404
    with fresh_db.backend() as conn:
        row = conn.execute(
            "select status from public.approvals where id = %s", [approval.id]
        ).fetchone()
    assert row is not None and row["status"] == "pending"


def test_a_decided_readonly_violation_cannot_be_decided_again(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)
    approval = request_readonly(fresh_db, task.id)
    url = f"/v1/tasks/{task.id}/approvals/{approval.id}"
    assert (
        fresh_client.post(url, json={"decision": "reject"}, headers=auth(user)).status_code == 200
    )
    assert (
        fresh_client.post(url, json={"decision": "approve"}, headers=auth(user)).status_code == 409
    )
