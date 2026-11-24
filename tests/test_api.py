"""Backend API v1, M1 subset (plan §4, §11; spec §8 "approval by task submitter only").

Every mutating call must write exactly one audit row in the same transaction.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeRegistry, FakeUser, agents_md, index_manifest, repo_json

from swf import store
from swf.db import Database
from swf.tokens import TokenCipher

AuthFn = Callable[..., dict[str, str]]
MakeUser = Callable[..., FakeUser]

NEW_TASK = {
    "repo": "octo/widgets",
    "base_branch": "main",
    "description": "test_paginate fails on the last page",
    "failing_test": "tests/test_pages.py::test_last_page",
}


def _new_user(
    database: Database,
    github: FakeGitHub,
    cipher: TokenCipher,
    *,
    linked: bool = True,
    login: str | None = None,
) -> FakeUser:
    """A signed-in user; when `linked`, a GitHub token is stored by raw SQL (no audit row)."""
    uid = uuid.uuid4()
    user = FakeUser(
        id=uid, github_id=random.randint(1, 2**40), login=login or f"user-{uid.hex[:8]}"
    )
    meta = {"user_name": user.login, "provider_id": str(user.github_id)}
    with database.backend() as conn:
        conn.execute(
            "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
            [user.id, f"{user.login}@example.test", json.dumps(meta)],
        )
        if linked:
            token = f"gho_{uuid.uuid4().hex}"
            github.users[token] = (user.github_id, user.login)
            conn.execute(
                "insert into profiles (user_id, github_login) values (%s, %s)"
                " on conflict (user_id) do nothing",
                [user.id, user.login],
            )
            conn.execute(
                "insert into github_tokens (user_id, encrypted_token, scopes, github_user_id)"
                " values (%s, %s, %s, %s)",
                [user.id, cipher.encrypt(token), ["repo", "read:user"], user.github_id],
            )
    return user


@pytest.fixture
def make_user(db: Database, github: FakeGitHub, cipher: TokenCipher) -> MakeUser:
    """Overrides conftest's: users have a linked GitHub token unless `linked=False`."""

    def _make(login: str | None = None, *, linked: bool = True) -> FakeUser:
        return _new_user(db, github, cipher, linked=linked, login=login)

    return _make


def _audit(db: Database, user: FakeUser) -> list[dict[str, Any]]:
    with db.backend() as conn:
        return conn.execute(
            "select action, target, payload from audit_log where user_id = %s order by id",
            [user.id],
        ).fetchall()


def _create_task(client: TestClient, auth: AuthFn, user: FakeUser, **body: Any) -> dict[str, Any]:
    resp = client.post("/v1/tasks", json={**NEW_TASK, **body}, headers=auth(user))
    assert resp.status_code == 201, resp.text
    task: dict[str, Any] = resp.json()["task"]
    return task


# --- auth -------------------------------------------------------------------------------


def test_healthz_needs_no_auth(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/me"),
        ("POST", "/v1/me/github-token"),
        ("GET", "/v1/repos"),
        ("GET", "/v1/tasks"),
        ("POST", "/v1/tasks"),
        ("GET", f"/v1/tasks/{uuid.uuid4()}"),
        ("POST", f"/v1/tasks/{uuid.uuid4()}/approvals/{uuid.uuid4()}"),
        ("POST", f"/v1/tasks/{uuid.uuid4()}/answers/{uuid.uuid4()}"),
        ("POST", f"/v1/tasks/{uuid.uuid4()}/cancel"),
    ],
)
def test_v1_requires_bearer_token(client: TestClient, method: str, path: str) -> None:
    for headers in [{}, {"Authorization": "Bearer garbage"}, {"Authorization": "Basic x"}]:
        resp = client.request(method, path, headers=headers, json={})
        assert resp.status_code == 401, (path, headers, resp.text)
        assert resp.headers.get("www-authenticate") == "Bearer"


def test_expired_token_rejected(client: TestClient, auth: AuthFn, make_user: MakeUser) -> None:
    user = make_user()
    resp = client.get("/v1/me", headers=auth(user, exp=1))
    assert resp.status_code == 401


def test_disabled_account_forbidden(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    user = make_user()
    assert client.get("/v1/me", headers=auth(user)).status_code == 200
    with db.backend() as conn:
        conn.execute("update profiles set disabled = true where user_id = %s", [user.id])
    resp = client.get("/v1/tasks", headers=auth(user))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "account_disabled"


# --- /v1/me and GitHub token capture ----------------------------------------------------


def test_me_creates_profile_and_reports_usage(
    client: TestClient, auth: AuthFn, make_user: MakeUser
) -> None:
    user = make_user(linked=False)
    resp = client.get("/v1/me", headers=auth(user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.pop("limits")) == {  # m10a §1.3 (values: tests/test_m10_quota.py)
        "tasks_per_day",
        "cost_per_day_usd",
        "cost_per_task_usd",
        "concurrent_tasks",
    }
    assert set(body.pop("remaining")) == {"tasks_today", "cost_today_usd", "concurrent_tasks"}
    assert body == {
        "user_id": str(user.id),
        "github_login": user.login,
        "is_admin": False,
        "github_linked": False,
        "usage_today": {"day": "2026-11-19", "tasks": 0, "cost_usd": 0.0},
    }


def test_github_token_must_belong_to_signed_in_account(
    client: TestClient, auth: AuthFn, make_user: MakeUser, github: FakeGitHub, db: Database
) -> None:
    user = make_user(linked=False)
    github.users["gho_someone_else"] = (user.github_id + 1, "someone-else")
    resp = client.post(
        "/v1/me/github-token", json={"provider_token": "gho_someone_else"}, headers=auth(user)
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "github_account_mismatch"
    with db.backend() as conn:
        assert store.get_encrypted_github_token(conn, user.id) is None
    assert _audit(db, user) == []


def test_github_token_invalid(client: TestClient, auth: AuthFn, make_user: MakeUser) -> None:
    resp = client.post(
        "/v1/me/github-token",
        json={"provider_token": "gho_revoked"},
        headers=auth(make_user(linked=False)),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "github_token_invalid"


def test_github_token_stored_encrypted_and_audited_without_secret(
    client: TestClient,
    auth: AuthFn,
    make_user: MakeUser,
    github: FakeGitHub,
    db: Database,
    cipher: TokenCipher,
) -> None:
    user = make_user(linked=False)
    secret = f"gho_{uuid.uuid4().hex}"
    github.users[secret] = (user.github_id, user.login)
    resp = client.post("/v1/me/github-token", json={"provider_token": secret}, headers=auth(user))
    assert resp.status_code == 204, resp.text
    assert secret not in resp.text

    with db.backend() as conn:
        row = conn.execute(
            "select encrypted_token, scopes, github_user_id from github_tokens where user_id = %s",
            [user.id],
        ).fetchone()
    assert row is not None
    assert secret not in row["encrypted_token"]
    assert cipher.decrypt(row["encrypted_token"]) == secret
    assert row["scopes"] == ["repo", "read:user"]
    assert row["github_user_id"] == user.github_id

    audit = _audit(db, user)
    assert [a["action"] for a in audit] == ["github_token.store"]
    assert secret not in str(audit)
    assert client.get("/v1/me", headers=auth(user)).json()["github_linked"] is True


def test_github_token_body_validation(
    client: TestClient, auth: AuthFn, make_user: MakeUser
) -> None:
    user = make_user(linked=False)
    for body in [{}, {"provider_token": ""}, {"provider_token": "x" * 5000}]:
        resp = client.post("/v1/me/github-token", json=body, headers=auth(user))
        assert resp.status_code == 422, body


# --- /v1/repos ----------------------------------------------------------------------------


def _link_token(client: TestClient, auth: AuthFn, user: FakeUser, github: FakeGitHub) -> str:
    secret = f"gho_{uuid.uuid4().hex}"
    github.users[secret] = (user.github_id, user.login)
    resp = client.post("/v1/me/github-token", json={"provider_token": secret}, headers=auth(user))
    assert resp.status_code == 204, resp.text
    return secret


def test_repos_requires_linked_token(client: TestClient, auth: AuthFn, make_user: MakeUser) -> None:
    resp = client.get("/v1/repos", headers=auth(make_user(linked=False)))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "github_token_missing"


def test_repos_lists_from_github(
    client: TestClient, auth: AuthFn, make_user: MakeUser, github: FakeGitHub
) -> None:
    user = make_user(linked=False)
    secret = _link_token(client, auth, user, github)
    github.repos[secret] = [
        repo_json("octo/a"),
        repo_json("octo/b", push=False, private=True, default_branch="dev"),
        repo_json("org/c"),
    ]
    resp = client.get("/v1/repos", headers=auth(user))
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"full_name": "octo/a", "default_branch": "main", "private": False, "can_push": True},
        {"full_name": "octo/b", "default_branch": "dev", "private": True, "can_push": False},
        {"full_name": "org/c", "default_branch": "main", "private": False, "can_push": True},
    ]


def test_repos_revoked_token(
    client: TestClient, auth: AuthFn, make_user: MakeUser, github: FakeGitHub
) -> None:
    user = make_user(linked=False)
    secret = _link_token(client, auth, user, github)
    del github.users[secret]
    resp = client.get("/v1/repos", headers=auth(user))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "github_token_invalid"


# --- tasks ------------------------------------------------------------------------------


def test_create_task(client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database) -> None:
    user = make_user()
    resp = client.post("/v1/tasks", json=NEW_TASK, headers=auth(user))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    task = body["task"]
    # The decision depends on what else ran on the shared database: admit or queue.
    assert body["preflight"]["decision"] in {"admit", "queue"}
    assert task["status"] == "queued"
    assert task["phase"] is None
    assert {k: task[k] for k in NEW_TASK} == NEW_TASK
    assert task["cost_usd"] == 0.0
    assert task["pr_url"] is None
    assert [(a["action"], a["target"]) for a in _audit(db, user)] == [("task.create", task["id"])]


@pytest.mark.parametrize(
    "bad",
    [
        {"repo": "no-slash"},
        {"repo": "a/b/c"},
        {"repo": "../etc"},
        {"repo": "octo/.."},
        {"base_branch": ""},
        {"base_branch": "-rf"},
        {"base_branch": "a..b"},
        {"base_branch": "feat/"},
        {"base_branch": "x.lock"},
        {"base_branch": "has space"},
        {"description": ""},
        {"description": "x" * 10_001},
        {"failing_test": ""},
        {"failing_test": "x" * 501},
        {"extra": "field"},
    ],
)
def test_create_task_validation(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database, bad: dict[str, Any]
) -> None:
    user = make_user()
    resp = client.post("/v1/tasks", json={**NEW_TASK, **bad}, headers=auth(user))
    assert resp.status_code == 422, resp.text
    assert _audit(db, user) == []
    assert client.get("/v1/me", headers=auth(user)).json()["usage_today"]["tasks"] == 0


def test_failing_test_is_optional(client: TestClient, auth: AuthFn, make_user: MakeUser) -> None:
    body = {k: v for k, v in NEW_TASK.items() if k != "failing_test"}
    resp = client.post("/v1/tasks", json=body, headers=auth(make_user()))
    assert resp.status_code == 201, resp.text
    assert resp.json()["task"]["failing_test"] is None


def test_quota_counter_increments_per_utc_day(
    client: TestClient, auth: AuthFn, make_user: MakeUser, clock: Any, db: Database
) -> None:
    user = make_user()
    _create_task(client, auth, user)
    _create_task(client, auth, user)
    assert client.get("/v1/me", headers=auth(user)).json()["usage_today"]["tasks"] == 2

    clock.now = datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC)
    _create_task(client, auth, user)
    usage = client.get("/v1/me", headers=auth(user)).json()["usage_today"]
    assert usage == {"day": "2026-11-20", "tasks": 1, "cost_usd": 0.0}

    with db.backend() as conn:
        rows = conn.execute(
            "select day, tasks from quotas_usage where user_id = %s order by day", [user.id]
        ).fetchall()
    assert [(r["day"].isoformat(), r["tasks"]) for r in rows] == [
        ("2026-11-19", 2),
        ("2026-11-20", 1),
    ]


def test_list_and_get_are_isolated_per_user(
    client: TestClient, auth: AuthFn, make_user: MakeUser
) -> None:
    a, b = make_user(), make_user()
    t1 = _create_task(client, auth, a, description="first")
    t2 = _create_task(client, auth, a, description="second")
    tb = _create_task(client, auth, b)

    listed = client.get("/v1/tasks", headers=auth(a)).json()
    assert [t["id"] for t in listed] == [t2["id"], t1["id"]]  # newest first
    assert [t["id"] for t in client.get("/v1/tasks", headers=auth(b)).json()] == [tb["id"]]

    assert client.get(f"/v1/tasks/{t1['id']}", headers=auth(a)).json()["id"] == t1["id"]
    assert client.get(f"/v1/tasks/{t1['id']}", headers=auth(b)).status_code == 404
    assert client.get(f"/v1/tasks/{uuid.uuid4()}", headers=auth(a)).status_code == 404
    assert client.get("/v1/tasks/not-a-uuid", headers=auth(a)).status_code == 422


# --- approvals (submitter only) ---------------------------------------------------------


def _approval(db: Database, task_id: str, kind: str = "plan") -> str:
    with db.backend() as conn:
        approval = store.create_approval(conn, uuid.UUID(task_id), kind, {"plan": "edit x.py"})
    return str(approval.id)


def test_submitter_approves_plan(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a = make_user()
    task = _create_task(client, auth, a)
    approval_id = _approval(db, task["id"])

    resp = client.post(
        f"/v1/tasks/{task['id']}/approvals/{approval_id}",
        json={"decision": "approve"},
        headers=auth(a),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == str(a.id)
    assert body["decided_at"] is not None
    actions = [(x["action"], x["target"], x["payload"]) for x in _audit(db, a)]
    assert actions[-1] == (
        "approval.decide",
        approval_id,
        {"task_id": task["id"], "kind": "plan", "decision": "approve"},
    )


def test_reject_keeps_comment(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a = make_user()
    task = _create_task(client, auth, a)
    approval_id = _approval(db, task["id"], kind="command")
    resp = client.post(
        f"/v1/tasks/{task['id']}/approvals/{approval_id}",
        json={"decision": "reject", "comment": "don't touch the lockfile"},
        headers=auth(a),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert resp.json()["comment"] == "don't touch the lockfile"


def test_other_user_cannot_decide(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a, b = make_user(), make_user()
    task = _create_task(client, auth, a)
    approval_id = _approval(db, task["id"])

    resp = client.post(
        f"/v1/tasks/{task['id']}/approvals/{approval_id}",
        json={"decision": "approve"},
        headers=auth(b),
    )
    assert resp.status_code == 404
    with db.backend() as conn:
        row = conn.execute("select status from approvals where id = %s", [approval_id]).fetchone()
    assert row is not None and row["status"] == "pending"
    assert _audit(db, b) == []


def test_approval_must_belong_to_path_task(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a = make_user()
    t1, t2 = _create_task(client, auth, a), _create_task(client, auth, a)
    approval_id = _approval(db, t1["id"])
    resp = client.post(
        f"/v1/tasks/{t2['id']}/approvals/{approval_id}",
        json={"decision": "approve"},
        headers=auth(a),
    )
    assert resp.status_code == 404


def test_approval_decided_once(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a = make_user()
    task = _create_task(client, auth, a)
    approval_id = _approval(db, task["id"])
    url = f"/v1/tasks/{task['id']}/approvals/{approval_id}"
    assert client.post(url, json={"decision": "approve"}, headers=auth(a)).status_code == 200
    resp = client.post(url, json={"decision": "reject"}, headers=auth(a))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "already_decided"


def test_approval_body_validation(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a = make_user()
    task = _create_task(client, auth, a)
    url = f"/v1/tasks/{task['id']}/approvals/{_approval(db, task['id'])}"
    for body in [{}, {"decision": "maybe"}, {"decision": "approve", "comment": "x" * 5001}]:
        assert client.post(url, json=body, headers=auth(a)).status_code == 422, body


# --- questions --------------------------------------------------------------------------


def _question(db: Database, task_id: str) -> str:
    with db.backend() as conn:
        q = store.create_question(conn, uuid.UUID(task_id), "Which Python version?")
    return str(q.id)


def test_submitter_answers_question_once(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a, b = make_user(), make_user()
    task = _create_task(client, auth, a)
    qid = _question(db, task["id"])
    url = f"/v1/tasks/{task['id']}/answers/{qid}"

    assert client.post(url, json={"answer": "3.12"}, headers=auth(b)).status_code == 404
    assert _audit(db, b) == []

    resp = client.post(url, json={"answer": "3.12"}, headers=auth(a))
    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"] == "3.12"
    assert resp.json()["answered_at"] is not None
    assert _audit(db, a)[-1]["action"] == "question.answer"

    again = client.post(url, json={"answer": "3.13"}, headers=auth(a))
    assert again.status_code == 409
    assert again.json()["detail"] == "already_answered"


# --- cancel -----------------------------------------------------------------------------


def test_cancel(client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database) -> None:
    a, b = make_user(), make_user()
    task = _create_task(client, auth, a)
    url = f"/v1/tasks/{task['id']}/cancel"

    assert client.post(url, headers=auth(b)).status_code == 404
    resp = client.post(url, headers=auth(a))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert _audit(db, a)[-1]["action"] == "task.cancel"

    again = client.post(url, headers=auth(a))
    assert again.status_code == 409
    assert again.json()["detail"] == "task_finished"


def test_finished_task_refuses_decisions(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database
) -> None:
    a = make_user()
    task = _create_task(client, auth, a)
    approval_id = _approval(db, task["id"])
    qid = _question(db, task["id"])
    assert client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth(a)).status_code == 200

    resp = client.post(
        f"/v1/tasks/{task['id']}/approvals/{approval_id}",
        json={"decision": "approve"},
        headers=auth(a),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "task_finished"
    resp = client.post(
        f"/v1/tasks/{task['id']}/answers/{qid}", json={"answer": "x"}, headers=auth(a)
    )
    assert resp.status_code == 409


# --- audit visibility -------------------------------------------------------------------


def test_every_mutation_writes_one_audit_row(
    client: TestClient, auth: AuthFn, make_user: MakeUser, db: Database, github: FakeGitHub
) -> None:
    a = make_user()
    _link_token(client, auth, a, github)
    task = _create_task(client, auth, a)
    approval_id = _approval(db, task["id"])
    qid = _question(db, task["id"])
    client.post(
        f"/v1/tasks/{task['id']}/approvals/{approval_id}",
        json={"decision": "approve"},
        headers=auth(a),
    )
    client.post(f"/v1/tasks/{task['id']}/answers/{qid}", json={"answer": "y"}, headers=auth(a))
    client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth(a))
    # reads do not audit
    client.get("/v1/me", headers=auth(a))
    client.get("/v1/tasks", headers=auth(a))
    client.get(f"/v1/tasks/{task['id']}", headers=auth(a))

    assert [x["action"] for x in _audit(db, a)] == [
        "github_token.store",
        "task.create",
        "approval.decide",
        "question.answer",
        "task.cancel",
    ]
    with db.as_user(a.claims()) as conn:
        visible = conn.execute("select count(*) as n from audit_log").fetchone()
    assert visible is not None and visible["n"] == 5


# --- pre-flight at task creation (private database per test) --------------------------------

REPO = NEW_TASK["repo"]
DOCKER_HUB = "registry-1.docker.io"


def _seed_task(
    database: Database,
    user: FakeUser,
    status: str,
    *,
    mb: int | None = 4096,
    cpu: float | None = 1.0,
) -> None:
    """Insert a task straight into the database (backend role), as the worker would have."""
    with database.backend() as conn:
        conn.execute(
            "insert into profiles (user_id, github_login) values (%s, %s)"
            " on conflict (user_id) do nothing",
            [user.id, user.login],
        )
        conn.execute(
            "insert into tasks (user_id, repo, base_branch, description, status,"
            " reservation_mb, reservation_cpu) values (%s, 'o/r', 'main', 'x', %s, %s, %s)",
            [user.id, status, mb, cpu],
        )


def _post(client: TestClient, auth: AuthFn, user: FakeUser) -> Any:
    return client.post("/v1/tasks", json=NEW_TASK, headers=auth(user))


def _warning_codes(preflight: dict[str, Any]) -> list[str]:
    return [w["code"] for w in preflight["warnings"]]


def _task_count(database: Database, user: FakeUser) -> int:
    with database.backend() as conn:
        row = conn.execute(
            "select count(*) as n from tasks where user_id = %s", [user.id]
        ).fetchone()
    assert row is not None
    return int(row["n"])


def test_preflight_admits_with_reservation_from_agents_md(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.agents[REPO] = agents_md(memory_mb=768, cpus=0.5)
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    pre = body["preflight"]
    assert pre["decision"] == "admit"
    assert (pre["image"], pre["memory_mb"], pre["cpus"]) == ("python:3.12-slim", 768, 0.5)
    assert pre["queue_position"] is None
    assert pre["warnings"] == []
    task = body["task"]
    assert task["status"] == "queued"
    assert (task["reservation_mb"], task["reservation_cpu"]) == (768, 0.5)
    assert task["preflight"] == pre
    usage = fresh_client.get("/v1/me", headers=auth(user)).json()["usage_today"]
    assert usage["tasks"] == 1
    audit = _audit(fresh_db, user)
    assert [(a["action"], a["target"]) for a in audit] == [("task.create", task["id"])]
    assert audit[0]["payload"] == {"repo": REPO, "base_branch": "main", "preflight": "admit"}


def test_preflight_over_limit_is_rejected_and_not_counted(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.agents[REPO] = agents_md(memory_mb=8192, cpus=0.5)
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["preflight"]["decision"] == "reject"
    assert "over_task_limit" in _warning_codes(body["preflight"])
    assert body["preflight"]["queue_position"] is None
    task = body["task"]
    assert task["status"] == "rejected"
    assert task["reservation_mb"] is None
    assert task["reservation_cpu"] is None
    # A rejected task is stored and audited, but does not use the daily task quota.
    assert _task_count(fresh_db, user) == 1
    usage = fresh_client.get("/v1/me", headers=auth(user)).json()["usage_today"]
    assert usage["tasks"] == 0
    with fresh_db.backend() as conn:
        rows = conn.execute("select * from quotas_usage where user_id = %s", [user.id]).fetchall()
    assert all(r["tasks"] == 0 for r in rows)
    audit = _audit(fresh_db, user)
    assert [a["action"] for a in audit] == ["task.create"]
    assert audit[0]["payload"]["preflight"] == "reject"


def test_preflight_non_arm64_image_is_rejected(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
    registry: FakeRegistry,
) -> None:
    registry.manifests[(DOCKER_HUB, "library/python", "3.12-slim")] = index_manifest("linux/amd64")
    user = _new_user(fresh_db, github, cipher)
    body = _post(fresh_client, auth, user).json()
    assert body["preflight"]["decision"] == "reject"
    assert "image_not_arm64" in _warning_codes(body["preflight"])
    assert body["task"]["status"] == "rejected"


def test_preflight_missing_agents_md_queues_with_defaults(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.agents[REPO] = None
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    pre = body["preflight"]
    assert pre["decision"] == "admit"
    assert (pre["image"], pre["memory_mb"], pre["cpus"]) == ("python:3.12-slim", 2048, 1.0)
    assert _warning_codes(pre) == ["agents_md_missing"]
    assert body["task"]["status"] == "queued"
    assert (body["task"]["reservation_mb"], body["task"]["reservation_cpu"]) == (2048, 1.0)


def test_preflight_invalid_agents_md_is_rejected(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.agents[REPO] = "# Just prose, no agent-config block\n"
    user = _new_user(fresh_db, github, cipher)
    body = _post(fresh_client, auth, user).json()
    assert body["preflight"]["decision"] == "reject"
    assert "agents_md_invalid" in _warning_codes(body["preflight"])
    assert body["task"]["status"] == "rejected"
    assert body["task"]["reservation_mb"] is None


def test_preflight_repo_missing_is_422_and_leaves_no_trace(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.missing_repos.add(REPO)
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "repo_not_found"
    assert _task_count(fresh_db, user) == 0
    assert _audit(fresh_db, user) == []
    assert fresh_client.get("/v1/me", headers=auth(user)).json()["usage_today"]["tasks"] == 0


def test_preflight_branch_missing_is_422_and_leaves_no_trace(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.missing_branches.add("main")
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "branch_not_found"
    assert _task_count(fresh_db, user) == 0
    assert _audit(fresh_db, user) == []


def test_preflight_without_linked_token_is_409(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    user = _new_user(fresh_db, github, cipher, linked=False)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "github_token_missing"
    assert _task_count(fresh_db, user) == 0
    assert _audit(fresh_db, user) == []


def test_preflight_undecryptable_token_is_409(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    user = _new_user(fresh_db, github, cipher)
    with fresh_db.backend() as conn:
        conn.execute(
            "update github_tokens set encrypted_token = 'not-a-fernet-blob' where user_id = %s",
            [user.id],
        )
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "github_token_invalid"
    assert _task_count(fresh_db, user) == 0


def test_preflight_revoked_token_is_409(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    user = _new_user(fresh_db, github, cipher)
    github.users.clear()  # GitHub now answers 401 to the stored token
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "github_token_invalid"
    assert _task_count(fresh_db, user) == 0
    assert _audit(fresh_db, user) == []


def test_preflight_queues_when_two_tasks_are_active(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    other = _new_user(fresh_db, github, cipher, linked=False)
    _seed_task(fresh_db, other, "executing", mb=512, cpu=0.5)
    _seed_task(fresh_db, other, "executing", mb=512, cpu=0.5)
    user = _new_user(fresh_db, github, cipher)

    first = _post(fresh_client, auth, user).json()
    assert first["preflight"]["decision"] == "queue"
    assert first["preflight"]["queue_position"] == 1
    assert "host_busy" in _warning_codes(first["preflight"])
    assert first["task"]["status"] == "queued"
    assert first["task"]["reservation_mb"] == 512  # the estimate, not the limit

    second = _post(fresh_client, auth, user).json()
    assert second["preflight"]["decision"] == "queue"
    assert second["preflight"]["queue_position"] == 2


def test_preflight_counts_waiting_tasks_as_demand(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    other = _new_user(fresh_db, github, cipher, linked=False)
    _seed_task(fresh_db, other, "queued", mb=None, cpu=None)  # 4096 MB / 1 vCPU by default
    user = _new_user(fresh_db, github, cipher)

    # An idle host plus one waiting task still has room for a second one (2 concurrent).
    first = _post(fresh_client, auth, user).json()
    assert first["preflight"]["decision"] == "admit"
    assert first["preflight"]["queue_position"] is None
    assert first["task"]["status"] == "queued"

    # Now two tasks are in demand: the third one waits, first in line behind them.
    second = _post(fresh_client, auth, user).json()
    assert second["preflight"]["decision"] == "queue"
    assert second["preflight"]["queue_position"] == 1
    assert "host_busy" in _warning_codes(second["preflight"])

    third = _post(fresh_client, auth, user).json()
    assert third["preflight"]["decision"] == "queue"
    assert third["preflight"]["queue_position"] == 2


def test_preflight_one_active_and_one_queued_means_queue(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    other = _new_user(fresh_db, github, cipher, linked=False)
    _seed_task(fresh_db, other, "executing", mb=512, cpu=0.5)
    _seed_task(fresh_db, other, "queued", mb=512, cpu=0.5)
    user = _new_user(fresh_db, github, cipher)
    pre = _post(fresh_client, auth, user).json()["preflight"]
    assert (pre["decision"], pre["queue_position"]) == ("queue", 1)


def test_preflight_admits_with_one_active_task(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    other = _new_user(fresh_db, github, cipher, linked=False)
    _seed_task(fresh_db, other, "investigating", mb=512, cpu=0.5)
    user = _new_user(fresh_db, github, cipher)
    pre = _post(fresh_client, auth, user).json()["preflight"]
    assert pre["decision"] == "admit"
    assert pre["queue_position"] is None


def test_finished_tasks_do_not_hold_capacity(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    other = _new_user(fresh_db, github, cipher, linked=False)
    for status in ("completed", "failed", "cancelled", "interrupted", "rejected", "created"):
        _seed_task(fresh_db, other, status)
    user = _new_user(fresh_db, github, cipher)
    assert _post(fresh_client, auth, user).json()["preflight"]["decision"] == "admit"


def test_get_task_returns_stored_preflight(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    user = _new_user(fresh_db, github, cipher)
    created = _post(fresh_client, auth, user).json()
    got = fresh_client.get(f"/v1/tasks/{created['task']['id']}", headers=auth(user))
    assert got.status_code == 200, got.text
    assert got.json()["preflight"] == created["preflight"]
    listed = fresh_client.get("/v1/tasks", headers=auth(user)).json()
    assert listed[0]["preflight"] == created["preflight"]


def test_preflight_oversized_agents_md_is_rejected(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.agents[REPO] = "x" * (1024 * 1024 + 1)
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["preflight"]["decision"] == "reject"
    assert _warning_codes(body["preflight"]) == ["agents_md_invalid"]
    assert body["task"]["status"] == "rejected"
    assert fresh_client.get("/v1/me", headers=auth(user)).json()["usage_today"]["tasks"] == 0


def test_preflight_repo_without_push_access_is_422_and_leaves_no_trace(
    fresh_client: TestClient,
    fresh_db: Database,
    auth: AuthFn,
    github: FakeGitHub,
    cipher: TokenCipher,
) -> None:
    github.repo_host.readonly_repos.add(REPO)
    user = _new_user(fresh_db, github, cipher)
    resp = _post(fresh_client, auth, user)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "repo_not_writable"
    assert _task_count(fresh_db, user) == 0
    assert _audit(fresh_db, user) == []
    assert fresh_client.get("/v1/me", headers=auth(user)).json()["usage_today"]["tasks"] == 0
