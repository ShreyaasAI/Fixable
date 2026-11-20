"""Schema, grants and RLS (plan §9, M1; spec §10 "User A cannot read user B's tasks/events").

Threat model: the browser holds the user's JWT and can call Supabase's REST API directly,
bypassing our backend. So for `anon` and `authenticated` the database itself must allow only
SELECT on the caller's own rows, and nothing on `github_tokens`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import psycopg
import pytest
from helpers import FakeUser
from psycopg import sql

from swf.db import Database
from swf.store import TASK_STATUSES

USER_TABLES = {
    "profiles",
    "github_tokens",
    "tasks",
    "events",
    "approvals",
    "questions",
    "evidence",
    "audit_log",
    "quotas_usage",
}
USER_READABLE = USER_TABLES - {"github_tokens"}
PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]


def _seed_all(db: Database, user: FakeUser) -> uuid.UUID:
    """One row for `user` in every user table (via the backend role)."""
    with db.backend() as conn:
        conn.execute(
            "insert into profiles (user_id, github_login) values (%s, %s)", [user.id, user.login]
        )
        row = conn.execute(
            "insert into tasks (user_id, repo, base_branch, description)"
            " values (%s, 'o/r', 'main', 'fix it') returning id",
            [user.id],
        ).fetchone()
        assert row is not None
        task_id: uuid.UUID = row["id"]
        conn.execute("insert into events (task_id, seq, type) values (%s, 0, 'status')", [task_id])
        conn.execute("insert into approvals (task_id, kind) values (%s, 'plan')", [task_id])
        conn.execute("insert into questions (task_id, question) values (%s, 'why?')", [task_id])
        conn.execute("insert into evidence (task_id, kind) values (%s, 'baseline')", [task_id])
        conn.execute(
            "insert into audit_log (user_id, action, target) values (%s, 'test.seed', %s)",
            [user.id, str(task_id)],
        )
        conn.execute(
            "insert into quotas_usage (user_id, day, tasks) values (%s, current_date, 1)",
            [user.id],
        )
        conn.execute(
            "insert into github_tokens (user_id, encrypted_token, github_user_id)"
            " values (%s, 'ciphertext', %s)",
            [user.id, user.github_id],
        )
    return task_id


def _count(conn: psycopg.Connection[dict[str, object]], table: str) -> int:
    row = conn.execute(
        sql.SQL("select count(*) as n from public.{}").format(sql.Identifier(table))
    ).fetchone()
    assert row is not None
    n = row["n"]
    assert isinstance(n, int)
    return n


def test_public_tables_are_exactly_the_user_tables_and_all_have_rls(db: Database) -> None:
    with db.backend() as conn:
        rows = conn.execute(
            "select c.relname, c.relrowsecurity from pg_class c"
            " join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'public' and c.relkind in ('r', 'p')"
        ).fetchall()
    assert {r["relname"] for r in rows} == USER_TABLES
    assert [r["relname"] for r in rows if not r["relrowsecurity"]] == []


@pytest.mark.parametrize("table", sorted(USER_TABLES))
def test_anon_has_no_privileges(db: Database, table: str) -> None:
    with db.backend() as conn:
        for priv in PRIVILEGES:
            row = conn.execute(
                "select has_table_privilege('anon', %s, %s) as ok", [f"public.{table}", priv]
            ).fetchone()
            assert row is not None
            assert row["ok"] is False, (table, priv)


@pytest.mark.parametrize("table", sorted(USER_TABLES))
def test_authenticated_is_select_only(db: Database, table: str) -> None:
    with db.backend() as conn:
        for priv in PRIVILEGES:
            row = conn.execute(
                "select has_table_privilege('authenticated', %s, %s) as ok",
                [f"public.{table}", priv],
            ).fetchone()
            assert row is not None
            expected = priv == "SELECT" and table in USER_READABLE
            assert row["ok"] is expected, (table, priv)


def test_no_sequence_or_function_privileges_for_api_roles(db: Database) -> None:
    with db.backend() as conn:
        seqs = conn.execute(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'public' and c.relkind = 'S'"
            " and (has_sequence_privilege('anon', c.oid, 'USAGE,SELECT,UPDATE')"
            "   or has_sequence_privilege('authenticated', c.oid, 'USAGE,SELECT,UPDATE'))"
        ).fetchall()
        funcs = conn.execute(
            "select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'public'"
            " and (has_function_privilege('anon', p.oid, 'EXECUTE')"
            "   or has_function_privilege('authenticated', p.oid, 'EXECUTE'))"
        ).fetchall()
    assert seqs == []
    assert funcs == []


def test_user_b_sees_none_of_user_a_rows(db: Database, make_user: Callable[..., FakeUser]) -> None:
    a, b = make_user(), make_user()
    _seed_all(db, a)
    with db.as_user(b.claims()) as conn:
        for table in sorted(USER_READABLE):
            assert _count(conn, table) == 0, table
    with db.as_user(a.claims()) as conn:
        for table in sorted(USER_READABLE):
            assert _count(conn, table) >= 1, table


def test_no_sub_claim_sees_nothing(db: Database, make_user: Callable[..., FakeUser]) -> None:
    _seed_all(db, make_user())
    with db.as_user({"role": "authenticated"}) as conn:
        for table in sorted(USER_READABLE):
            assert _count(conn, table) == 0, table


@pytest.mark.parametrize(
    "statement",
    [
        "insert into tasks (user_id, repo, base_branch, description)"
        " values (auth.uid(), 'o/r', 'main', 'x')",
        "update tasks set description = 'hacked'",
        "delete from tasks",
        "update approvals set status = 'approved', decided_at = now()",
        "insert into audit_log (user_id, action) values (auth.uid(), 'forged')",
        "update quotas_usage set tasks = 0",
        "update profiles set is_admin = true",
        "select * from github_tokens",
    ],
)
def test_users_cannot_write_or_read_tokens_directly(
    db: Database, make_user: Callable[..., FakeUser], statement: str
) -> None:
    a = make_user()
    _seed_all(db, a)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.as_user(a.claims()) as conn:
        conn.execute(statement)  # type: ignore[arg-type]


def test_audit_log_is_append_only(db: Database, make_user: Callable[..., FakeUser]) -> None:
    a = make_user()
    _seed_all(db, a)
    for statement in [
        "update audit_log set action = 'x'",
        "delete from audit_log",
        "truncate audit_log",
    ]:
        with pytest.raises(psycopg.Error, match="append-only"), db.backend() as conn:
            conn.execute(statement)  # type: ignore[arg-type]


def test_task_status_constraint_matches_python(postgres_url: str, db: Database) -> None:
    with psycopg.connect(postgres_url) as conn:
        uid = uuid.uuid4()
        conn.execute("insert into auth.users (id) values (%s)", [uid])
        conn.execute("insert into profiles (user_id) values (%s)", [uid])
        for status in TASK_STATUSES:
            conn.execute(
                "insert into tasks (user_id, repo, base_branch, description, status)"
                " values (%s, 'o/r', 'main', 'x', %s)",
                [uid, status],
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into auth.users (id) values (%s)", [uid])
            conn.execute("insert into profiles (user_id) values (%s)", [uid])
            conn.execute(
                "insert into tasks (user_id, repo, base_branch, description, status)"
                " values (%s, 'o/r', 'main', 'x', 'bogus')",
                [uid],
            )
        conn.rollback()


def test_deleting_task_removes_events_but_keeps_audit(
    db: Database, make_user: Callable[..., FakeUser]
) -> None:
    a = make_user()
    task_id = _seed_all(db, a)
    with db.backend() as conn:
        conn.execute("delete from tasks where id = %s", [task_id])
        events = conn.execute("select count(*) as n from events where task_id = %s", [task_id])
        audit = conn.execute(
            "select count(*) as n from audit_log where target = %s", [str(task_id)]
        )
        ev_row, au_row = events.fetchone(), audit.fetchone()
    assert ev_row is not None and ev_row["n"] == 0
    assert au_row is not None and au_row["n"] == 1


def test_list_tasks_newest_first_is_deterministic(
    db: Database, make_user: Callable[..., FakeUser]
) -> None:
    """Tasks created in one transaction share now(); order must still follow creation."""
    from swf import store

    a = make_user()
    with db.backend() as conn:
        conn.execute("insert into profiles (user_id) values (%s)", [a.id])
        ids = [
            conn.execute(
                "insert into tasks (user_id, repo, base_branch, description)"
                " values (%s, 'o/r', 'main', %s) returning id",
                [a.id, f"t{i}"],
            ).fetchone()["id"]  # type: ignore[index]
            for i in range(6)
        ]
    with db.as_user(a.claims()) as conn:
        listed = [t.id for t in store.list_tasks(conn, a.id)]
    assert listed == list(reversed(ids))
