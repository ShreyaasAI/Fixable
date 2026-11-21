"""Checks against a real Supabase project (M8 amendment A1, T6a).

Skipped unless `SWF_SUPABASE_DB_URL` is set (direct or session-pooler URL, port 5432, `postgres`
role). Every check runs in a transaction that is rolled back, so no rows persist. Messages never
include the URL or password.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

pytestmark = [
    pytest.mark.supabase,
    pytest.mark.skipif(
        not os.environ.get("SWF_SUPABASE_DB_URL"),
        reason="SWF_SUPABASE_DB_URL not set (no Supabase project to check)",
    ),
]

PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[DictRow]]:
    c: psycopg.Connection[DictRow] = psycopg.connect(
        os.environ["SWF_SUPABASE_DB_URL"], row_factory=dict_row, autocommit=False
    )
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _one(
    conn: psycopg.Connection[DictRow], query: str, params: list[object] | None = None
) -> dict[str, Any]:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row


def _as_authenticated(conn: psycopg.Connection[DictRow], claims: dict[str, object]) -> None:
    conn.execute("set local role authenticated")
    conn.execute("select set_config('request.jwt.claims', %s, true)", [json.dumps(claims)])


def test_all_migrations_applied(conn: psycopg.Connection[DictRow]) -> None:
    # Supabase's MCP `apply_migration` stamps its own version numbers, so compare by effect,
    # not by file name: M8's system_state singleton and M2's tasks.preflight column.
    assert _one(conn, "select to_regclass('public.system_state') is not null as ok")["ok"]
    assert _one(conn, "select count(*) as n from public.system_state")["n"] == 1
    assert (
        _one(
            conn,
            "select count(*) as n from information_schema.columns"
            " where table_schema = 'public' and table_name = 'tasks' and column_name = 'preflight'",
        )["n"]
        == 1
    )


def test_rls_enabled_on_every_public_table(conn: psycopg.Connection[DictRow]) -> None:
    rows = conn.execute(
        "select c.relname, c.relrowsecurity from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and c.relkind in ('r', 'p')"
    ).fetchall()
    assert rows, "no public tables found"
    assert [r["relname"] for r in rows if not r["relrowsecurity"]] == []


@pytest.mark.parametrize("table", ["github_tokens", "system_state"])
@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_api_roles_have_no_access_to_backend_only_tables(
    conn: psycopg.Connection[DictRow], table: str, role: str
) -> None:
    for priv in PRIVILEGES:
        ok = _one(
            conn, "select has_table_privilege(%s, %s, %s) as ok", [role, f"public.{table}", priv]
        )["ok"]
        assert ok is False, (role, table, priv)


def test_authenticated_is_select_only_on_tasks(conn: psycopg.Connection[DictRow]) -> None:
    for priv in PRIVILEGES:
        ok = _one(
            conn,
            "select has_table_privilege('authenticated', 'public.tasks', %s) as ok",
            [priv],
        )["ok"]
        assert ok is (priv == "SELECT"), priv


def test_backend_role_can_switch_to_authenticated_with_jwt_claims(
    conn: psycopg.Connection[DictRow],
) -> None:
    uid = str(uuid.uuid4())
    _as_authenticated(conn, {"role": "authenticated", "sub": uid})
    row = _one(conn, "select current_user as u, auth.uid()::text as uid")
    assert row["u"] == "authenticated"
    assert row["uid"] == uid
    conn.execute("reset role")
    assert _one(conn, "select current_user as u")["u"] != "authenticated"


def test_user_cannot_read_another_users_task(conn: psycopg.Connection[DictRow]) -> None:
    ids = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    tasks: dict[str, uuid.UUID] = {}
    try:
        with conn.transaction():
            for uid in ids.values():
                conn.execute("insert into auth.users (id) values (%s)", [uid])
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.NotNullViolation) as e:
        pytest.skip(f"cannot insert into auth.users: {type(e).__name__}")
    for key, uid in ids.items():
        conn.execute(
            "insert into public.profiles (user_id, github_login) values (%s, %s)", [uid, key]
        )
        tasks[key] = _one(
            conn,
            "insert into public.tasks (user_id, repo, base_branch, description)"
            " values (%s, 'o/r', 'main', 'fix it') returning id",
            [uid],
        )["id"]

    _as_authenticated(conn, {"role": "authenticated", "sub": str(ids["a"])})
    seen = {r["id"] for r in conn.execute("select id from public.tasks").fetchall()}
    assert seen == {tasks["a"]}
    assert (
        _one(conn, "select count(*) as n from public.tasks where id = %s", [tasks["b"]])["n"] == 0
    )
