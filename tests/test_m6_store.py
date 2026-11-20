"""Store additions for M6 (m6-contract §6): `record_pr`, `get_task_github_token`, RLS on `pr_url`,
and the API read. Private database clone; no migration."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from helpers import FakeUser
from m4_fakes import make_db_user, queued_task, task_row
from m5_fakes import events_of_type

from swf import store
from swf.db import Database
from swf.tokens import TokenCipher

pytest_plugins = ["m4_fakes", "m5_fakes"]

AuthFn = Callable[..., dict[str, str]]
URL = "https://github.com/octo/widgets/pull/7"
SHA = "a" * 40


def new_task(db: Database) -> tuple[FakeUser, store.Task]:
    user = make_db_user(db)
    return user, queued_task(db, user=user)


def record(db: Database, task: store.Task, url: str = URL) -> None:
    with db.backend() as conn:
        store.record_pr(conn, task.id, url=url, number=7, branch=f"agent/{task.id}", commit_sha=SHA)


def test_record_pr_sets_the_url_the_event_and_the_audit_row(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    record(fresh_db, task)
    assert task_row(fresh_db, task.id)["pr_url"] == URL
    (event,) = events_of_type(fresh_db, task.id, "pr_opened")
    assert event == {
        "url": URL,
        "number": 7,
        "branch": f"agent/{task.id}",
        "commit_sha": SHA,
    }
    with fresh_db.backend() as conn:
        rows = conn.execute(
            "select user_id, target from public.audit_log where action = 'pr.open'"
        ).fetchall()
    assert [(r["user_id"], r["target"]) for r in rows if r["target"] == str(task.id)]


def test_a_different_second_url_is_refused_and_changes_nothing(fresh_db: Database) -> None:
    _, task = new_task(fresh_db)
    record(fresh_db, task)
    with pytest.raises(ValueError):
        record(fresh_db, task, url="https://github.com/octo/widgets/pull/8")
    assert task_row(fresh_db, task.id)["pr_url"] == URL
    assert len(events_of_type(fresh_db, task.id, "pr_opened")) == 1


def test_get_task_github_token_returns_the_owners_blob_or_none(
    fresh_db: Database, cipher: TokenCipher
) -> None:
    owner, task = new_task(fresh_db)
    _, other_task = new_task(fresh_db)
    blob = cipher.encrypt("ghs_owner_token")
    with fresh_db.backend() as conn:
        store.store_github_token(
            conn, owner.id, encrypted_token=blob, scopes=("repo",), github_user_id=1
        )
        assert store.get_task_github_token(conn, task.id) == blob
        assert store.get_task_github_token(conn, other_task.id) is None
        assert store.get_task_github_token(conn, uuid.uuid4()) is None
    assert cipher.decrypt(blob) == "ghs_owner_token"


def test_user_b_cannot_see_user_as_pr_url(fresh_db: Database) -> None:
    a, task = new_task(fresh_db)
    b = make_db_user(fresh_db)
    record(fresh_db, task)
    with fresh_db.as_user(b.claims()) as conn:
        assert store.get_task(conn, b.id, task.id) is None
        assert conn.execute("select pr_url from public.tasks").fetchall() == []
    with fresh_db.as_user(a.claims()) as conn:
        mine = store.get_task(conn, a.id, task.id)
    assert mine is not None and mine.pr_url == URL


def test_the_task_api_shows_the_pr_url(
    fresh_client: TestClient, fresh_db: Database, auth: AuthFn
) -> None:
    user, task = new_task(fresh_db)
    before = fresh_client.get(f"/v1/tasks/{task.id}", headers=auth(user))
    assert before.status_code == 200 and before.json()["pr_url"] is None
    record(fresh_db, task)
    after = fresh_client.get(f"/v1/tasks/{task.id}", headers=auth(user))
    assert after.json()["pr_url"] == URL
