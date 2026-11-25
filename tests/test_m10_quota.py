"""M10a T3: quota remainder (m10a-contract §1.3).

`store.remaining` is pure and floors each field at 0; `store.check_quota` refuses exactly when
the matching `remaining` field is exhausted (tasks, then cost, then concurrent);
`store.open_task_count` counts non-terminal tasks; `GET /v1/me` reports `limits` and `remaining`.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import date
from typing import Any

import pytest
import test_m8_api
from m4_fakes import make_db_user
from test_m8_api import TODAY, Env, M8Client

from swf import store
from swf.db import Database
from swf.settings import AbuseLimits

LIMITS = AbuseLimits(tasks_per_day=2, cost_per_day_usd=1.0, concurrent_tasks=2)

m8_client = test_m8_api.m8_client  # reuse the M8 app fixture


def _usage(tasks: int, cost: float) -> store.Usage:
    return store.Usage(day=TODAY, tasks=tasks, cost_usd=cost)


def test_remaining_is_limit_minus_usage() -> None:
    rem = store.remaining(_usage(1, 0.25), 1, LIMITS)
    assert rem.tasks_today == 1
    assert rem.cost_today_usd == pytest.approx(0.75)
    assert rem.concurrent_tasks == 1


def test_remaining_floors_every_field_at_zero() -> None:
    rem = store.remaining(_usage(9, 7.5), 5, LIMITS)
    assert (rem.tasks_today, rem.cost_today_usd, rem.concurrent_tasks) == (0, 0, 0)


def test_remaining_with_no_usage_is_the_full_limit() -> None:
    rem = store.remaining(_usage(0, 0.0), 0, LIMITS)
    assert rem.tasks_today == 2
    assert rem.cost_today_usd == pytest.approx(1.0)
    assert rem.concurrent_tasks == 2


def _insert_task(env_db: Database, user_id: uuid.UUID, status: str) -> None:
    with env_db.backend() as conn:
        conn.execute(
            "insert into public.tasks (user_id, repo, base_branch, description, status)"
            " values (%s, 'octo/widgets', 'main', 'd', %s)",
            [user_id, status],
        )


def test_open_task_count_counts_only_non_terminal_tasks(fresh_db: Database) -> None:
    user, other = make_db_user(fresh_db), make_db_user(fresh_db)
    for status in ("queued", "executing", "awaiting_input", "completed", "failed", "cancelled"):
        _insert_task(fresh_db, user.id, status)
    _insert_task(fresh_db, other.id, "queued")
    with fresh_db.backend() as conn:
        assert store.open_task_count(conn, user.id) == 3
        assert store.open_task_count(conn, other.id) == 1
        assert store.open_task_count(conn, uuid.uuid4()) == 0


def test_check_quota_refuses_exactly_when_remaining_is_exhausted(fresh_db: Database) -> None:
    user = make_db_user(fresh_db)
    day: date = TODAY
    for tasks, cost, open_n in itertools.product((0, 1, 2, 3), (0.0, 0.5, 1.0, 1.5), (0, 1, 2, 3)):
        with fresh_db.backend() as conn:
            conn.execute("delete from public.tasks where user_id = %s", [user.id])
            conn.execute("delete from public.quotas_usage where user_id = %s", [user.id])
            conn.execute(
                "insert into public.quotas_usage (user_id, day, tasks, cost_usd)"
                " values (%s, %s, %s, %s)",
                [user.id, day, tasks, cost],
            )
        for _ in range(open_n):
            _insert_task(fresh_db, user.id, "queued")
        with fresh_db.backend() as conn:
            rem = store.remaining(
                store.get_usage(conn, user.id, day), store.open_task_count(conn, user.id), LIMITS
            )
            code = store.check_quota(conn, user.id, day, LIMITS)
        expected: str | None = None
        if rem.tasks_today <= 0:
            expected = "quota_tasks_per_day"
        elif rem.cost_today_usd <= 0:
            expected = "quota_cost_per_day"
        elif rem.concurrent_tasks <= 0:
            expected = "quota_concurrent_tasks"
        assert code == expected, (tasks, cost, open_n, rem)
        # Cross-check the property against the raw numbers, not only against `remaining`.
        raw_exhausted = tasks >= 2 or cost >= 1.0 or open_n >= 2
        assert (code is not None) == raw_exhausted, (tasks, cost, open_n)


# --- GET /v1/me ------------------------------------------------------------------------------


def _me(env: Env, user: Any) -> dict[str, Any]:
    resp = env.client.get("/v1/me", headers=env.auth(user))
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def test_me_reports_the_limits_in_use_and_the_full_remainder(m8_client: M8Client) -> None:
    limits = AbuseLimits(
        tasks_per_day=7, cost_per_day_usd=2.5, cost_per_task_usd=0.75, concurrent_tasks=3
    )
    env = m8_client(limits)
    user = env.user()

    body = _me(env, user)

    assert body["limits"] == {
        "tasks_per_day": 7,
        "cost_per_day_usd": 2.5,
        "cost_per_task_usd": 0.75,
        "concurrent_tasks": 3,
    }
    assert body["remaining"] == {"tasks_today": 7, "cost_today_usd": 2.5, "concurrent_tasks": 3}
    assert "usage_today" in body


def test_me_remaining_follows_usage_and_open_tasks(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(tasks_per_day=5, concurrent_tasks=2, cost_per_day_usd=3.0))
    user = env.user()
    assert env.post(user).status_code == 201

    body = _me(env, user)

    assert body["remaining"]["tasks_today"] == 4
    assert body["remaining"]["concurrent_tasks"] == 1
    assert body["usage_today"]["tasks"] == 1


def test_me_remaining_is_floored_at_zero(m8_client: M8Client) -> None:
    env = m8_client(AbuseLimits(cost_per_day_usd=3.0))
    user = env.user()
    env.sql(
        "insert into quotas_usage (user_id, day, tasks, cost_usd) values (%s, %s, 9, 4.0)",
        [user.id, TODAY],
    )

    body = _me(env, user)

    assert body["remaining"]["tasks_today"] == 0
    assert body["remaining"]["cost_today_usd"] == 0
