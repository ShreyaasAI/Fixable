"""store.capacity_snapshot and the M2 task columns (contract §6), on a private database."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import date

from swf import store
from swf.db import Database
from swf.preflight import Capacity, PreflightResult, Reservation

ACTIVE = [
    "preparing",
    "investigating",
    "awaiting_plan_approval",
    "executing",
    "awaiting_command_approval",
    "awaiting_input",
    "verifying",
    "confirming",
    "opening_pr",
]
INACTIVE = ["created", "queued", "completed", "rejected", "failed", "interrupted", "cancelled"]
DAY = date(2026, 11, 20)


def _user(db: Database) -> uuid.UUID:
    uid = uuid.uuid4()
    with db.backend() as conn:
        conn.execute("insert into auth.users (id) values (%s)", [uid])
        conn.execute("insert into profiles (user_id) values (%s)", [uid])
    return uid


def _insert(
    db: Database, uid: uuid.UUID, status: str, mb: int | None = None, cpu: float | None = None
) -> None:
    with db.backend() as conn:
        conn.execute(
            "insert into tasks (user_id, repo, base_branch, description, status,"
            " reservation_mb, reservation_cpu) values (%s, 'o/r', 'main', 'x', %s, %s, %s)",
            [uid, status, mb, cpu],
        )


def test_active_statuses_constant() -> None:
    assert set(store.ACTIVE_STATUSES) == set(ACTIVE)
    assert set(store.ACTIVE_STATUSES) <= set(store.TASK_STATUSES)
    assert not set(store.ACTIVE_STATUSES) & set(INACTIVE)


def test_empty_database_has_no_capacity_in_use(fresh_db: Database) -> None:
    with fresh_db.backend() as conn:
        assert store.capacity_snapshot(conn) == Capacity(active=(), queued=())


def test_capacity_snapshot_counts_only_active_statuses(fresh_db: Database) -> None:
    uid = _user(fresh_db)
    for status in ACTIVE:
        _insert(fresh_db, uid, status, 512, 0.5)
    for status in INACTIVE:
        _insert(fresh_db, uid, status, 9999, 3.0)
    with fresh_db.backend() as conn:
        snap = store.capacity_snapshot(conn)
    assert Counter(snap.active) == Counter({Reservation(512, 0.5): len(ACTIVE)})
    assert snap.queued == (Reservation(9999, 3.0),)  # the single 'queued' row


def test_capacity_snapshot_coalesces_missing_reservations(fresh_db: Database) -> None:
    uid = _user(fresh_db)
    _insert(fresh_db, uid, "executing")  # no reservation: the 4 GB / 1 vCPU task limit
    _insert(fresh_db, uid, "verifying", 1024, 0.5)
    with fresh_db.backend() as conn:
        snap = store.capacity_snapshot(conn)
    assert Counter(snap.active) == Counter({Reservation(4096, 1.0): 1, Reservation(1024, 0.5): 1})
    assert all(isinstance(r.cpus, float) for r in snap.active)


def test_capacity_snapshot_spans_all_users_and_counts_queue(fresh_db: Database) -> None:
    a, b = _user(fresh_db), _user(fresh_db)
    _insert(fresh_db, a, "executing", 512, 0.5)
    _insert(fresh_db, b, "awaiting_input", 2048, 1.0)
    for uid in (a, a, b):
        _insert(fresh_db, uid, "queued", 512, 0.5)
    with fresh_db.backend() as conn:
        snap = store.capacity_snapshot(conn)
    assert Counter(snap.active) == Counter({Reservation(512, 0.5): 1, Reservation(2048, 1.0): 1})
    assert snap.queued == (Reservation(512, 0.5),) * 3


def _result(decision: str) -> PreflightResult:
    return PreflightResult(
        decision=decision,  # type: ignore[arg-type]
        image="python:3.12-slim",
        memory_mb=512,
        cpus=0.5,
        time_limit_minutes=60,
        queue_position=None,
        warnings=[],
    )


def _create(
    db: Database, uid: uuid.UUID, status: str, pre: PreflightResult | None, mb: int | None
) -> store.Task:
    with db.backend() as conn:
        return store.create_task(
            conn,
            uid,
            repo="o/r",
            base_branch="main",
            description="d",
            failing_test=None,
            day=DAY,
            status=status,
            reservation_mb=mb,
            reservation_cpu=None if mb is None else 0.5,
            preflight=pre,
        )


def _payloads(db: Database, uid: uuid.UUID) -> list[dict[str, object]]:
    with db.backend() as conn:
        rows = conn.execute("select payload from audit_log where user_id = %s", [uid]).fetchall()
    return [r["payload"] for r in rows]


def test_create_task_stores_preflight_and_counts_quota(fresh_db: Database) -> None:
    uid = _user(fresh_db)
    pre = _result("admit")
    task = _create(fresh_db, uid, "queued", pre, 512)
    assert task.preflight == pre
    with fresh_db.backend() as conn:
        assert store.get_usage(conn, uid, DAY).tasks == 1
    assert _payloads(fresh_db, uid) == [
        {"repo": "o/r", "base_branch": "main", "preflight": "admit"}
    ]


def test_create_task_rejected_does_not_count_quota(fresh_db: Database) -> None:
    uid = _user(fresh_db)
    task = _create(fresh_db, uid, "rejected", _result("reject"), None)
    assert (task.status, task.reservation_mb, task.reservation_cpu) == ("rejected", None, None)
    with fresh_db.backend() as conn:
        assert store.get_usage(conn, uid, DAY).tasks == 0
    assert len(_payloads(fresh_db, uid)) == 1


def test_create_task_without_preflight(fresh_db: Database) -> None:
    uid = _user(fresh_db)
    task = _create(fresh_db, uid, "queued", None, 512)
    assert task.preflight is None
    assert _payloads(fresh_db, uid)[0]["preflight"] is None


def test_queued_reservations_are_coalesced_and_in_fifo_order(fresh_db: Database) -> None:
    uid = _user(fresh_db)
    _insert(fresh_db, uid, "queued", 1024, 0.5)
    _insert(fresh_db, uid, "queued")  # no reservation: the 4 GB / 1 vCPU task limit
    _insert(fresh_db, uid, "queued", 256, 0.25)
    with fresh_db.backend() as conn:
        snap = store.capacity_snapshot(conn)
    assert snap.active == ()
    assert isinstance(snap.queued, tuple)
    assert snap.queued == (
        Reservation(1024, 0.5),
        Reservation(4096, 1.0),
        Reservation(256, 0.25),
    )
