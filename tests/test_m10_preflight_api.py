"""M10a T2: `POST /v1/preflight`, the dry run (m10a-contract §1.2, decision 11).

It runs the same admission and pre-flight steps as `POST /v1/tasks`, with the same error codes,
but writes no task and no audit row and spends no `create` token.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import test_m8_api
from helpers import agents_md
from test_m8_api import NEW_TASK, REPO, TODAY, Env, M8Client

from swf.settings import AbuseLimits

BODY = {"repo": NEW_TASK["repo"], "base_branch": NEW_TASK["base_branch"]}

m8_client = test_m8_api.m8_client  # reuse the M8 app fixture


def preflight(env: Env, user: Any, **body: Any) -> Any:
    return env.client.post("/v1/preflight", json={**BODY, **body}, headers=env.auth(user))


def _detail(resp: Any) -> tuple[int, Any]:
    return resp.status_code, resp.json().get("detail")


def test_preflight_returns_the_estimate_and_writes_nothing(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()

    resp = preflight(env, user)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "admit"
    assert body["image"] == "python:3.12-slim"
    assert env.tasks(user) == []
    assert env.audit(user) == []
    with env.db.backend() as conn:
        usage = conn.execute(
            "select count(*) as n from quotas_usage where user_id = %s", [user.id]
        ).fetchone()
    assert usage == {"n": 0}
    # The same shape that create returns, so the UI can show either with one component.
    created = env.post(env.user())
    assert set(body) == set(created.json()["preflight"])


def test_preflight_shows_a_reject_decision_without_storing_a_rejected_task(
    m8_client: M8Client,
) -> None:
    env = m8_client()
    user = env.user()
    env.github.repo_host.agents[REPO] = agents_md(memory_mb=8192, cpus=0.5)

    resp = preflight(env, user)

    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "reject"
    assert env.tasks(user) == []


def test_preflight_requires_a_valid_token(m8_client: M8Client) -> None:
    env = m8_client()
    assert env.client.post("/v1/preflight", json=BODY).status_code == 401


# --- same error codes as create ---------------------------------------------------------------


def _repo_missing(env: Env, _user: Any) -> None:
    env.github.repo_host.missing_repos.add(REPO)


def _branch_missing(env: Env, _user: Any) -> None:
    env.github.repo_host.missing_branches.add(NEW_TASK["base_branch"])


def _read_only(env: Env, _user: Any) -> None:
    env.github.repo_host.readonly_repos.add(REPO)


def _no_token(env: Env, user: Any) -> None:
    env.sql("delete from github_tokens where user_id = %s", [user.id])


def _undecryptable(env: Env, user: Any) -> None:
    env.sql("update github_tokens set encrypted_token = 'garbage' where user_id = %s", [user.id])


def _github_rejects_token(env: Env, _user: Any) -> None:
    env.github.users.clear()


def _github_down(env: Env, _user: Any) -> None:
    env.github.repo_host.handle = lambda _request: httpx.Response(500)  # type: ignore[method-assign]


ERRORS: list[tuple[str, Callable[[Env, Any], None], int, str]] = [
    ("repo_not_found", _repo_missing, 422, "repo_not_found"),
    ("branch_not_found", _branch_missing, 422, "branch_not_found"),
    ("repo_not_writable", _read_only, 422, "repo_not_writable"),
    ("github_token_missing", _no_token, 409, "github_token_missing"),
    ("github_token_undecryptable", _undecryptable, 409, "github_token_invalid"),
    ("github_token_rejected", _github_rejects_token, 409, "github_token_invalid"),
    ("github_unavailable", _github_down, 502, "github_unavailable"),
]


@pytest.mark.parametrize(
    ("setup", "status", "code"), [e[1:] for e in ERRORS], ids=[e[0] for e in ERRORS]
)
def test_preflight_errors_match_create(
    m8_client: M8Client, setup: Callable[[Env, Any], None], status: int, code: str
) -> None:
    env = m8_client()
    user = env.user()
    setup(env, user)

    dry = preflight(env, user)
    real = env.post(user)

    assert _detail(dry) == (status, code), dry.text
    assert _detail(real) == (status, code), real.text
    assert env.tasks(user) == []
    assert env.audit(user) == []


# --- admission before any GitHub call ---------------------------------------------------------


def test_preflight_is_refused_with_503_while_paused_and_calls_no_github(
    m8_client: M8Client,
) -> None:
    env = m8_client()
    user = env.user()
    env.sql("update system_state set tasks_paused = true")

    resp = preflight(env, user)

    assert _detail(resp) == (503, "service_paused")
    assert env.github.requests == []
    assert env.tasks(user) == []


@pytest.mark.parametrize(
    ("usage_sql", "code"),
    [
        (
            "insert into quotas_usage (user_id, day, tasks) values (%s, %s, 5)",
            "quota_tasks_per_day",
        ),
        (
            "insert into quotas_usage (user_id, day, cost_usd) values (%s, %s, 3.0)",
            "quota_cost_per_day",
        ),
    ],
)
def test_preflight_is_refused_with_429_over_quota_and_calls_no_github(
    m8_client: M8Client, usage_sql: str, code: str
) -> None:
    env = m8_client()
    user = env.user()
    env.sql(usage_sql, [user.id, TODAY])

    resp = preflight(env, user)

    assert _detail(resp) == (429, code)
    assert int(resp.headers["Retry-After"]) >= 1
    assert env.github.requests == []


def test_preflight_is_refused_when_the_concurrent_quota_is_used(m8_client: M8Client) -> None:
    env = m8_client()
    user = env.user()
    assert env.post(user).status_code == 201
    seen = len(env.github.requests)

    resp = preflight(env, user)

    assert _detail(resp) == (429, "quota_concurrent_tasks")
    assert len(env.github.requests) == seen


def _paused(env: Env, _user: Any) -> None:
    env.sql("update system_state set tasks_paused = true")


def _over_quota(env: Env, user: Any) -> None:
    env.sql("insert into quotas_usage (user_id, day, tasks) values (%s, %s, 5)", [user.id, TODAY])


# Fail-fast order (m10a §1.2): admission, then the GitHub token, then the repo checks. Each
# check is set up to fail together with every later one; the earliest must be the one reported.
ORDER: list[tuple[Callable[[Env, Any], None], tuple[int, str]]] = [
    (_paused, (503, "service_paused")),
    (_over_quota, (429, "quota_tasks_per_day")),
    (_no_token, (409, "github_token_missing")),
    (_read_only, (422, "repo_not_writable")),
]


@pytest.mark.parametrize("first", range(len(ORDER)), ids=[o[1][1] for o in ORDER])
def test_preflight_reports_the_earliest_failing_check(m8_client: M8Client, first: int) -> None:
    env = m8_client()
    user = env.user()
    for setup, _expected in ORDER[first:]:
        setup(env, user)

    assert _detail(preflight(env, user)) == ORDER[first][1]
    if first < 3:  # refused before the token is read: no GitHub call at all
        assert env.github.requests == []


# --- rate limits ------------------------------------------------------------------------------


def test_preflight_spends_no_create_token(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(create_burst=1, concurrent_tasks=100))
    user = env.user()

    assert preflight(env, user).status_code == 200
    assert preflight(env, user).status_code == 200
    assert env.post(user).status_code == 201  # the single create token is still there
    assert env.post(user).status_code == 429  # and now it is gone


def test_preflight_spends_one_api_token(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(api_burst=2, api_per_minute=60))
    user = env.user()

    assert preflight(env, user).status_code == 200
    assert preflight(env, user).status_code == 200
    limited = preflight(env, user)

    assert _detail(limited) == (429, "rate_limited")


# --- body validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"repo": "not-a-repo", "base_branch": "main"},
        {"repo": "octo/..", "base_branch": "main"},
        {"repo": "octo/widgets", "base_branch": "bad..branch"},
        {"repo": "octo/widgets", "base_branch": "-x"},
        {"repo": "octo/widgets", "base_branch": ""},
        {"repo": "octo/widgets"},
        {"base_branch": "main"},
    ],
)
def test_preflight_validates_the_body_like_create(
    m8_client: M8Client, body: dict[str, str]
) -> None:
    env = m8_client()
    user = env.user()

    resp = env.client.post("/v1/preflight", json=body, headers=env.auth(user))

    assert resp.status_code == 422
    assert env.github.requests == []
