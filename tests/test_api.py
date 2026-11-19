"""Backend API v1, M1 subset (plan §4, §11; spec §8 "approval by task submitter only").

Every mutating call must write exactly one audit row in the same transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import FakeGitHub, FakeUser, repo_json

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
    user = make_user()
    resp = client.get("/v1/me", headers=auth(user))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "user_id": str(user.id),
        "github_login": user.login,
        "is_admin": False,
        "github_linked": False,
        "usage_today": {"day": "2026-11-19", "tasks": 0, "cost_usd": 0.0},
    }


def test_github_token_must_belong_to_signed_in_account(
    client: TestClient, auth: AuthFn, make_user: MakeUser, github: FakeGitHub, db: Database
) -> None:
    user = make_user()
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
        "/v1/me/github-token", json={"provider_token": "gho_revoked"}, headers=auth(make_user())
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
    user = make_user()
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
    user = make_user()
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
    resp = client.get("/v1/repos", headers=auth(make_user()))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "github_token_missing"


def test_repos_lists_from_github(
    client: TestClient, auth: AuthFn, make_user: MakeUser, github: FakeGitHub
) -> None:
    user = make_user()
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
    user = make_user()
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
    assert body["preflight"] is None  # pre-flight arrives in M2
    task = body["task"]
    assert task["status"] == "created"
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
