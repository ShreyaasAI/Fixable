# M8 contract: abuse controls + gVisor

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development. Tasks run in order, one implementer each, and each is followed by a spec review and a code-quality review. Steps use checkboxes (`- [ ]`).

- **Purpose:** The interface contract and ordered task list for M8. If something here is wrong, report it. Do not silently diverge.
- **Last updated:** 2026-11-22
- **Status:** Draft v2: §13 amendments (user, 2026-11-21) override §0–§6. Branch `feature/m8` (worktree `.claude/worktrees/m8-plan`), on top of `6e45a13` (M7).
- **Sources:** plan §4, §9, §15, §16, §18 M8; spec §4 item 1a, §8, §10, §12 Q1–2, §13; research plan §6 item 9, §7.4, §8; m2 §0, §4, §9 (A1, A5, deferred list); m4 §12; m6 §11; m7 §13.

**Goal:** A user over quota is refused before a task starts. API abuse gets HTTP 429. An admin can stop one user's tasks or all tasks, and running containers are gone within 30 s. The sandbox suite passes under gVisor `runsc` on the Oracle VM.

**Architecture:**
- Quota values live in one settings object, `AbuseLimits`, read from env with the spec defaults.
- The API checks quotas in the same transaction that inserts the task, under a per-user advisory lock.
- Rate limiting is an in-process token bucket keyed by user id. This is correct only for one API process on one VM.
- The kill switch is a DB write: profile disabled or global pause flag, plus `cancelled` on every non-terminal task. Each worker polls its running tasks every 5 s and `docker rm -f`s the container of any task that went terminal.
- `SWF_SANDBOX_RUNTIME` selects `runsc` or `runc`.

**Tech stack:** Python 3.14, FastAPI, psycopg, pytest, Docker Engine + gVisor on Ubuntu 24.04 aarch64. No new Python dependencies.

---

## 0. Decisions, assumptions, open questions

1. [D, user 2026-11-21] **Host.** Oracle Always Free Arm VM: Ubuntu 24.04 aarch64, A1.Flex 2 OCPU / 12 GB, provisioned by the user. Local dev stays Windows + Docker Desktop, with no `runsc`.
2. [D, user] **Runtime is configurable.** `SWF_SANDBOX_RUNTIME` = `runsc` | `runc` | unset. Unset means no `--runtime` flag (Docker's default, runc), which keeps the M2 default (m2 §4 `runtime: None`). The VM sets `runsc`. Tests marked `gvisor` skip, with a reason, unless `SWF_SANDBOX_RUNTIME=runsc` and `docker info` lists `runsc`.
3. [D, user] **Code reaches the VM without GitHub.** `git bundle` + `scp` + `git fetch` from the bundle (`ops/vm-sync.sh`). No push before M9. `.env` is untracked, so it is never in the bundle.
4. [D, user] **Test Postgres on the VM** is the existing `postgres:17-alpine` container from `tests/conftest.py` (multi-arch). No Supabase project in M8 (see Q1).
5. [D, user] **Every quota value comes from settings** (§1.1), with the spec §12 defaults: 5 tasks/user/day, $1/task, $3/user/day, 1 concurrent task/user. **[Q]** The user will set the final numbers. Changing them is env only, no code.
6. [D, user] **Roles.**
   - Opus: coder and reviewers.
   - Sonnet: test writer, bash/ops, docs.
   - Haiku: test checker.
   - $0 infrastructure only.
7. [D, planner] **Rate limiter is in-process** (`swf.ratelimit`). It is correct only with one API process: run uvicorn with `--workers 1` on the single VM. State resets on restart. A shared limiter is deferred (§6).
8. [D, planner] **Quota check order and atomicity.** `POST /v1/tasks` order:
   - auth;
   - `create` rate bucket;
   - global pause → 503;
   - quota pre-check, so an over-quota user causes no GitHub call;
   - pre-flight;
   - inside the insert transaction: pause and quota re-check, then `create_task`.

   The re-check takes `pg_advisory_xact_lock(hashtext('swf.quota:' || user_id))`, so two parallel creates cannot both pass. A refused create writes no task row and no audit row (like other 4xx).
9. [D, planner] **What counts.**
   - `tasks_per_day` counts `quotas_usage.tasks`, which excludes pre-flight `rejected` tasks (m2 §0.3).
   - `cost_per_day_usd` counts `quotas_usage.cost_usd` for that UTC day. Cost is booked on the task's creation day (`store.add_task_cost`).
   - `concurrent_tasks` counts the user's non-terminal tasks, including `queued` and `awaiting_*`.
   - Reaching a limit refuses the create. The task is not queued.
10. [D, planner] **Per-task cost cap at run time.** The runner sets `WebAgent.cost_limit = min(configured cost_limit if > 0, budget)`, where budget = `min(cost_per_task_usd − task.cost_usd, cost_per_day_usd − day cost)`. Budget ≤ 0 at claim → `failed("quota_cost_per_day")` before any container starts. The models are free today, so this is a backstop.
11. [D, planner] **Kill switch semantics** (plan §4 `POST /v1/admin/kill {user_id?}`). Admin = `profiles.is_admin`; anyone else gets 403 `admin_only`.
    - **Per user** `{"user_id": …}`: sets `profiles.disabled = true`, so every request from that user now gets 403 `account_disabled` (existing M1 check). It then cancels all of that user's non-terminal tasks with `error = "admin_kill"`. Killing your own id → 422 `cannot_kill_self`.
    - **Global** `{}`: sets `system_state.tasks_paused = true`, so workers claim nothing and `POST /v1/tasks` → 503 `service_paused`. It then cancels every non-terminal task.
    - Kill is sticky. `POST /v1/admin/resume {user_id?}` undoes it **[Q2: resume endpoint is an addition to plan §4]**.
12. [D, planner] **Stopping within 30 s.** `cancelled` is written at once by the API. Each worker runs a stop loop every `stop_poll_seconds = 5`:
    - it reads which of its running tasks are terminal (`store.stopped_task_ids`);
    - `TaskRunner.kill()` sets `stopped` and calls `env.cleanup()` (`docker rm -f` + detach the task network);
    - an in-flight `docker exec` (agent command or setup) then returns, and the runner exits at its next status check, writing nothing.

    Heartbeat-lost and reaped runners are killed the same way. This also speeds up user `cancel`. An in-flight model HTTP call is not interrupted, but its container is already gone and its result is discarded. "Stopped" means status terminal and container + task network removed.
13. [D, planner] **The egress proxy (Squid) stays on runc.** It is our own pinned image and config, it needs SETUID/SETGID (m2 A6), and it holds no user code. Only task sandboxes use `runsc`. The digest pinned in `swf.egress` is a multi-arch index that includes `linux/arm64/v8` (checked 2026-11-21 with `docker buildx imagetools inspect`).
14. [D, planner] **Host isolation on the VM** (m2 §9 deferred item). A sandbox must not reach host services through its internal network's gateway IP. Oracle's Ubuntu image accepts tcp/22 on INPUT from any interface. Bootstrap adds `iptables -I INPUT 1 -i br-+ -j DROP` (and ip6tables) and persists it. Task containers reach the proxy by bridging (FORWARD), so this rule does not affect them. Tested by T11.
15. [A] The workspace bind mount on Linux needs the host user to be uid 1000 (sandbox `--user 1000:1000`). The Linux chown work is M9 (m2 deferred). Bootstrap therefore picks or creates a uid-1000 account (`swf`) that runs the suite.
16. [A] gVisor on Oracle A1 runs with the default `systrap` platform (no nested KVM). These are expected to work under `runsc` (verified in T13):
    - internal networks;
    - `--memory`, `--pids-limit`, `--cap-drop ALL`, `no-new-privileges`, `--init`;
    - `docker exec`.

    If an M2 sandbox test differs only by an exit code or message under runsc (OOM, pids), T13 reports it and a follow-up task fixes code or test, red-first.
17. [A] Rate-limit defaults (§1.1) are planner proposals: 120 requests/min with a burst of 60 per user, and 6 creates/min with a burst of 3. Settings-driven.
18. [A] The kill switch has no time window: `quotas_usage` is untouched by kill and resume.

**Open questions (need the user):**
- **Q1.** Plan §16 and HANDOVER say the Supabase Free project is created in M8. The lead said "no account/infra changes", so this contract defers it to M9 and runs M8 on local/VM Postgres. Confirm, or approve creating it in M8 (it also verifies the plan §4 [A] that `postgres` can `SET ROLE authenticated`).
- **Q2.** Is the `POST /v1/admin/resume` addition OK, and should a per-user kill disable the account (sticky)? Without it, an admin undoes a kill with SQL.
- **Q3.** What are the final quota numbers (§0.5)? Are the rate-limit defaults (§0.17) acceptable?
- **Q4.** T13 runs `ops/vm-bootstrap.sh` with sudo on the user's VM: it installs Docker, gVisor and uv, adds a firewall rule, and may create user `swf`. Who runs it: the user, or an agent over ssh (needs host and key)?

## 1. Interfaces

### 1.1 `swf.settings.AbuseLimits`

```python
@dataclass(frozen=True)
class AbuseLimits:
    tasks_per_day: int = 5            # SWF_QUOTA_TASKS_PER_DAY        [Q]
    cost_per_task_usd: float = 1.0    # SWF_QUOTA_COST_PER_TASK_USD    [Q]
    cost_per_day_usd: float = 3.0     # SWF_QUOTA_COST_PER_DAY_USD     [Q]
    concurrent_tasks: int = 1         # SWF_QUOTA_CONCURRENT_TASKS     [Q]
    api_per_minute: float = 120.0     # SWF_RATE_API_PER_MINUTE        [A]
    api_burst: int = 60               # SWF_RATE_API_BURST             [A]
    create_per_minute: float = 6.0    # SWF_RATE_CREATE_PER_MINUTE     [A]
    create_burst: int = 3             # SWF_RATE_CREATE_BURST          [A]

    def __post_init__(self) -> None: ...   # every field > 0, else SettingsError naming the field

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AbuseLimits: ...
```

- An empty or missing variable keeps the default.
- int fields need `int(v)`, so `"1.5"` fails. float fields need `float(v)` and must be finite.
- Any bad or ≤ 0 value → `SettingsError` whose message names the variable.

### 1.2 `swf.ratelimit.RateLimiter` (new file)

```python
class RateLimiter:
    """Token bucket per key; thread-safe; in-process (one API process only, §0.7)."""
    def __init__(self, *, per_minute: float, burst: int,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None: ...
    def hit(self, key: Hashable) -> float | None: ...   # None = allowed (1 token used); else seconds to wait
    def __len__(self) -> int: ...                       # tracked keys
```

- A new key starts full (`burst` tokens). Tokens refill at `per_minute / 60` per second, capped at `burst`.
- A refused hit uses no token.
- When more than `max_keys` keys are tracked, keys whose bucket is full again are dropped.
- `per_minute <= 0`, `burst < 1` or `max_keys < 1` → `ValueError`.

### 1.3 `swf.sandbox` additions

```python
RUNTIMES = frozenset({"runc", "runsc"})
class SandboxRuntimeError(Exception): ...
def runtime_from_env(env: Mapping[str, str] | None = None) -> str | None: ...  # SWF_SANDBOX_RUNTIME; blank -> None; other -> ValueError
def parse_runtimes(info_json: str) -> frozenset[str]: ...                      # keys of `docker info --format '{{json .Runtimes}}'`
def check_runtime(runtime: str, *, docker: str = "docker") -> None: ...        # missing -> SandboxRuntimeError
# SandboxSpec.runtime becomes:
runtime: Literal["runc", "runsc"] | None = Field(default_factory=runtime_from_env)
```

`sandbox_run_args` is unchanged: `--runtime <r>` only when set. `open_task_sandbox` is unchanged, so it picks the env runtime through the default.

### 1.4 Migration `supabase/migrations/20261123000000_m8_abuse.sql`

```sql
-- M8: global kill switch (singleton). Backend-only: no grant, RLS on, no policy.
create table public.system_state (
  id           boolean primary key default true check (id),
  tasks_paused boolean not null default false,
  updated_at   timestamptz not null default now(),
  updated_by   uuid
);
insert into public.system_state default values;
alter table public.system_state enable row level security;
revoke all on public.system_state from anon, authenticated;
```

### 1.5 `swf.store` additions

| Function | Behaviour |
|---|---|
| `check_quota(conn, user_id, day, limits) -> str \| None` | Takes the per-user advisory lock (§0.8). Returns the first hit in this order: `quota_tasks_per_day` (usage.tasks ≥ limit), `quota_cost_per_day` (usage.cost_usd ≥ limit), `quota_concurrent_tasks` (non-terminal tasks ≥ limit). Otherwise `None` |
| `tasks_paused(conn) -> bool` | `system_state.tasks_paused` |
| `admin_kill(conn, admin_id, user_id: UUID \| None) -> list[UUID]` | Per user: `update profiles set disabled = true` (no row → `NotFound`). Global: set `tasks_paused`, `updated_at`, `updated_by`. Then, in created order, for each non-terminal task in scope: `select … for update`, `_expire_pending`, `transition_task(…, "cancelled", error="admin_kill")`. Audit: actor `admin_id`, `admin.kill`, target = user id or `"global"`, payload `{"scope", "cancelled": n}`. Returns the cancelled ids |
| `admin_resume(conn, admin_id, user_id: UUID \| None) -> None` | Clears `disabled` (no row → `NotFound`) or `tasks_paused`. Audit `admin.resume`, same target and scope |
| `stopped_task_ids(conn, task_ids) -> list[UUID]` | Ids among `task_ids` whose status is terminal; `[]` for empty input |
| `task_cost_budget(conn, task_id, limits) -> float` | `min(cost_per_task − tasks.cost_usd, cost_per_day − quotas_usage.cost_usd of owner on the task's UTC creation day)`; missing task → `NotFound` |
| `claim_next_task` (changed) | Right after the advisory lock: `if tasks_paused(conn): return None` |

### 1.6 API (`swf.api.app`)

- `AppDeps` gains `abuse: AbuseLimits = field(default_factory=AbuseLimits)` and `monotonic: Callable[[], float] = time.monotonic`. Enforcing is the default; tests opt out explicitly (§2).
- `create_app` builds two `RateLimiter`s from `deps.abuse`: `api` and `create`, both with `clock=deps.monotonic`.
- `current_user` is unchanged up to the disabled check. Then it calls `api.hit(user.id)`.
  - Refused → 429 `{"detail": "rate_limited"}` with header `Retry-After: max(1, ceil(s))`.
  - This covers every `/v1` route, including SSE (one hit when the stream opens) and the admin routes. `/healthz` is not limited.
  - A bad token is a 401 before any limiter runs.

| Route | Change |
|---|---|
| `POST /v1/tasks` | Steps in order: (1) `create.hit(user.id)` refused → 429 `rate_limited`. (2) Backend txn: `tasks_paused` → 503 `service_paused`. (3) `check_quota` code → 429 `{detail: code}`; for the two daily codes, `Retry-After` = seconds to the next UTC midnight from `deps.clock`. (4) Pre-flight (unchanged). (5) Insert txn: pause and quota re-check (same errors), then `create_task` |
| `POST /v1/admin/kill` (new) | Admin only (403 `admin_only`). Body `KillIn(_Body): user_id: UUID \| None = None`. `user_id == caller` → 422 `cannot_kill_self`. Unknown user → 404 `not_found`. 200 `KillOut{scope: "user"\|"global", user_id, cancelled: [UUID]}` |
| `POST /v1/admin/resume` (new, Q2) | Admin only. Same body. 200 `ResumeOut{scope, user_id}`. Unknown user → 404 |

`swf.api.main.create_app_from_env` passes `abuse=AbuseLimits.from_env()`. The module docstring says run with `--workers 1` (§0.7).

### 1.7 Worker (`swf.worker`)

- `WorkerConfig` gains `abuse: AbuseLimits = field(default_factory=AbuseLimits)` and `stop_poll_seconds: float = 5.0`. Validation: `0 < stop_poll_seconds <= 10`, else `ValueError`.
- `TaskRunner.kill()`: `self.stopped = True`; then, if `self.env` is set, `env.cleanup()`. An exception there is logged and swallowed. Safe to call twice and from another thread.
- `TaskRunner._run`, first thing: budget = `store.task_cost_budget(…)`. If ≤ 0 → `_TaskFailed("quota_cost_per_day")`. Later: `agent_kwargs["cost_limit"] = budget if cfg ≤ 0 else min(cfg, budget)`, set after the `**deps.config.agent` spread.
- `Worker.enforce_stops() -> list[UUID]`: running ids → `stopped_task_ids` → `runner.kill()` for each, and returns them.
- `Worker.heartbeat` (lost lease) and `Worker.reconcile` (reaped) call `kill()` instead of `stop()`.
- `_stop_loop` thread (every `stop_poll_seconds`) in `serve` and `run_once`, next to the heartbeat thread.
- `main()` builds `AbuseLimits.from_env()` and `runtime = runtime_from_env()`. If a runtime is set, it calls `check_runtime(runtime)` (fail fast before claiming) and logs it.

## 2. Test conventions (all test tasks)

- Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs <files>`.
- `tests/conftest.py`: `build_deps` passes `abuse=PERMISSIVE`, where `PERMISSIVE` = every `AbuseLimits` field set to 1_000_000. M1–M7 API tests are then unchanged. M8 tests build their own client with explicit limits on **`fresh_db`**.
- Global kill and pause tests must use `fresh_db`. On the shared `db` they would cancel other tests' tasks.
- Worker tests that are not M8 keep `WorkerConfig` defaults. Their fake models cost $0, so budget = $1.
- New marker `gvisor` in `pyproject.toml` `[tool.pytest.ini_options].markers`. `pytest_collection_modifyitems` skips it with reason `gVisor unavailable (SWF_SANDBOX_RUNTIME!=runsc or runsc not in docker info)`.
- Docker names keep the per-run suffix (m2 A10).

## 3. Tasks

Every task is red first: write the tests, run them, see them fail for the stated reason, implement, run green, then run `uv run ruff check`, `uv run ruff format --check` and `uv run mypy src`, then commit. Roles: **test-writer** edits `tests/` (plus pytest markers in `pyproject.toml`); **coder** edits `src/` and `supabase/`; **ops** edits `ops/`, `.gitignore` and `docs/`. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

### T1 (test-writer): settings, runtime selection, gvisor marker

**Files:** create `tests/test_m8_settings.py`, `tests/test_m8_gvisor.py`; modify `tests/test_sandbox_unit.py` (add an autouse `monkeypatch.delenv("SWF_SANDBOX_RUNTIME", raising=False)`, so its "runtime is None by default" checks hold on the VM), `tests/conftest.py` (gvisor skip), `pyproject.toml` (marker).

- [ ] Write the tests:

```python
# tests/test_m8_settings.py
import pytest
from pydantic import ValidationError
from swf.sandbox import SandboxSpec, parse_runtimes, runtime_from_env, sandbox_run_args
from swf.settings import AbuseLimits, SettingsError

def test_abuse_defaults_are_spec_values() -> None:
    a = AbuseLimits()
    assert (a.tasks_per_day, a.cost_per_task_usd, a.cost_per_day_usd, a.concurrent_tasks) == (5, 1.0, 3.0, 1)
    assert (a.api_per_minute, a.api_burst, a.create_per_minute, a.create_burst) == (120.0, 60, 6.0, 3)

def test_abuse_from_env_overrides_and_keeps_defaults() -> None:
    a = AbuseLimits.from_env({"SWF_QUOTA_TASKS_PER_DAY": "10", "SWF_QUOTA_COST_PER_DAY_USD": "2.5",
                              "SWF_RATE_API_BURST": ""})
    assert (a.tasks_per_day, a.cost_per_day_usd, a.api_burst, a.concurrent_tasks) == (10, 2.5, 60, 1)

@pytest.mark.parametrize("name,value", [
    ("SWF_QUOTA_TASKS_PER_DAY", "x"), ("SWF_QUOTA_TASKS_PER_DAY", "0"),
    ("SWF_QUOTA_COST_PER_TASK_USD", "-1"), ("SWF_QUOTA_COST_PER_DAY_USD", "nan"),
    ("SWF_RATE_API_BURST", "1.5"), ("SWF_QUOTA_CONCURRENT_TASKS", "-2"),
])
def test_abuse_from_env_rejects_bad_values(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        AbuseLimits.from_env({name: value})

def test_abuse_direct_construction_validates() -> None:
    with pytest.raises(SettingsError):
        AbuseLimits(tasks_per_day=0)

@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("  ", None), ("runsc", "runsc"), ("runc", "runc")])
def test_runtime_from_env(raw: str | None, expected: str | None) -> None:
    env = {} if raw is None else {"SWF_SANDBOX_RUNTIME": raw}
    assert runtime_from_env(env) == expected

def test_runtime_from_env_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="SWF_SANDBOX_RUNTIME"):
        runtime_from_env({"SWF_SANDBOX_RUNTIME": "kata"})

def test_spec_runtime_defaults_from_env(tmp_path, monkeypatch) -> None:
    fields = dict(task_id="t1", image="python:3.12-slim", workspace=tmp_path,
                  proxy_url="http://10.0.0.2:3128", network="swf-task-t1")
    monkeypatch.setenv("SWF_SANDBOX_RUNTIME", "runsc")
    args = sandbox_run_args(SandboxSpec(**fields))
    assert args[args.index("--runtime") + 1] == "runsc"
    monkeypatch.delenv("SWF_SANDBOX_RUNTIME")
    assert "--runtime" not in sandbox_run_args(SandboxSpec(**fields))
    with pytest.raises(ValidationError):
        SandboxSpec(**fields, runtime="bogus")

def test_parse_runtimes() -> None:
    assert parse_runtimes('{"runc":{"path":"runc"},"runsc":{"path":"/usr/bin/runsc"}}') == {"runc", "runsc"}
    assert parse_runtimes("{}") == frozenset()
```

`tests/test_m8_gvisor.py` (`pytestmark = [pytest.mark.docker, pytest.mark.gvisor, pytest.mark.network]`), using the session `egress` and `egress_config`:
1. `check_runtime("runsc")` returns without an error.
2. `open_task_sandbox(egress_config, EnvRequest(task_id=f"swf-test-{run_id}-gv", image="python:3.12-slim", workspace=tmp_path, memory_mb=512, cpus=0.5))`:
   - `docker inspect -f '{{.HostConfig.Runtime}}' <container_id>` == `runsc`;
   - `env.execute({"command": "dmesg"})` output contains `gVisor`;
   - `env.execute({"command": "id -u"})` output is `1000`;
   - `cleanup()` in `finally`.

Plus one test with only the `docker` mark: when `runsc` is not in `parse_runtimes(docker info …)`, `check_runtime("runsc")` raises `SandboxRuntimeError`. It skips when runsc is present.

- [ ] Run. Expected: `ImportError` (no `AbuseLimits` / `runtime_from_env`); locally the gvisor file is skipped with the reason.
- [ ] Commit `M8 T1: tests for abuse settings and sandbox runtime`.

### T2 (coder): implement §1.1 and §1.3

**Files:** `src/swf/settings.py`, `src/swf/sandbox.py`.

- [ ] Implement `AbuseLimits` exactly as §1.1. `from_env` uses a table `(field, env name, int|float)`.
- [ ] Implement §1.3. `check_runtime` runs `[docker, "info", "--format", "{{json .Runtimes}}"]` with `capture_output=True, text=True, timeout=30, check=False`. A non-zero exit or a missing runtime → `SandboxRuntimeError(f"docker runtime {runtime!r} not available")`.
- [ ] Green: `tests/test_m8_settings.py tests/test_sandbox_unit.py` pass; the gvisor tests skip locally.
- [ ] Commit `M8 T2: AbuseLimits and SWF_SANDBOX_RUNTIME`.

### T3 (test-writer): rate limiter unit tests

**Files:** create `tests/test_m8_ratelimit.py`.

- [ ] Tests, using a manual clock `class Clock: t = 0.0; __call__ -> t`:
  1. `RateLimiter(per_minute=60, burst=3, clock=c)`: three hits → `None`; the fourth returns `pytest.approx(1.0)`.
  2. Refill: after a refused hit, `c.t += 1.0` → exactly one `None`, then refused again (a refused hit used no token).
  3. Keys are independent: `"a"` exhausted, `"b"` still `None`.
  4. Cap: `c.t += 3600` → only `burst` hits succeed.
  5. Prune: `max_keys=2, burst=1, per_minute=60`. Hit `a` and `b` at t=0, `c.t = 5`, hit `c` → `len(rl) <= 2`, and `a` and `b` are gone (`len(rl) == 1`).
  6. Thread safety: frozen clock, `burst=50`, 8 threads × 100 hits → exactly 50 `None`.
  7. `per_minute=0`, `burst=0`, `max_keys=0` → each `ValueError`.
- [ ] Run. Expected: `ModuleNotFoundError: swf.ratelimit`.
- [ ] Commit `M8 T3: rate limiter tests`.

### T4 (coder): `swf.ratelimit`

**Files:** create `src/swf/ratelimit.py`.

```python
"""In-process token-bucket rate limiter (m8 §1.2). One API process only (§0.7)."""
from __future__ import annotations
import threading, time
from collections.abc import Callable, Hashable

class RateLimiter:
    def __init__(self, *, per_minute: float, burst: int,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None:
        if per_minute <= 0 or burst < 1 or max_keys < 1:
            raise ValueError("per_minute > 0, burst >= 1 and max_keys >= 1 required")
        self._rate, self._burst, self._clock, self._max = per_minute / 60.0, float(burst), clock, max_keys
        self._buckets: dict[Hashable, tuple[float, float]] = {}  # key -> (tokens, at)
        self._lock = threading.Lock()

    def hit(self, key: Hashable) -> float | None:
        with self._lock:
            now = self._clock()
            tokens, at = self._buckets.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - at) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._prune(now)
                return None
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self._rate

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= self._max:
            return
        for k in [k for k, (t, at) in self._buckets.items() if t + (now - at) * self._rate >= self._burst]:
            del self._buckets[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
```

- [ ] Green: `tests/test_m8_ratelimit.py`. Commit `M8 T4: in-process rate limiter`.

### T5 (test-writer): migration + store

**Files:** create `tests/test_m8_store.py`. Use only `fresh_db`; create profiles and tasks by SQL or `store.create_task`, and set statuses with direct `update public.tasks set status = …`.

- [ ] Tests:
  1. Migration:
     - `system_state` has exactly one row with `tasks_paused = false`;
     - a second insert fails;
     - `relrowsecurity` is true;
     - `has_table_privilege('authenticated', 'public.system_state', 'select')` and the same for `anon` are false.
  2. `check_quota` with `AbuseLimits()`:
     - fresh user → `None`;
     - usage tasks = 5 today → `quota_tasks_per_day`;
     - cost 3.0 → `quota_cost_per_day`;
     - one `queued` task → `quota_concurrent_tasks`;
     - one `rejected` and one `completed` task → `None`;
     - usage on yesterday's date is ignored;
     - `AbuseLimits(tasks_per_day=6)` with 5 → `None`;
     - precedence: tasks and concurrent both hit → `quota_tasks_per_day`.
  3. `admin_kill(conn, admin, user)`:
     - user A has tasks in `queued`, `executing` (with a pending approval) and `awaiting_input` (with an open question), plus one `completed`; user B has one `executing`;
     - returns A's three ids in created order;
     - all three are `cancelled` with `error = 'admin_kill'`, and their pending approval/question are expired (status per `_expire_pending`);
     - A's `completed` and B's task are untouched;
     - A's profile is `disabled`;
     - one audit row: actor admin, `admin.kill`, target str(A), payload `{"scope": "user", "cancelled": 3}`.
  4. `admin_kill(conn, admin, uuid4())` → `NotFound`.
  5. Global:
     - `admin_kill(conn, admin, None)` cancels A's and B's non-terminal tasks, `tasks_paused` is true, `updated_by = admin`, audit target `"global"`;
     - a task queued afterwards is not claimed (`claim_next_task(...) is None`);
     - after `admin_resume(conn, admin, None)`, it is claimed;
     - audit `admin.resume`.
  6. `admin_resume(conn, admin, A)` clears `disabled`; an unknown id → `NotFound`.
  7. `stopped_task_ids`: given [queued, cancelled, failed] ids → the cancelled and failed ids; `[]` → `[]`.
  8. `task_cost_budget`:
     - task cost 0.4, day usage 2.9 → `approx(0.1)`;
     - task cost 0.2, day usage 0.5 → `approx(0.8)`;
     - day usage 3.0 → `<= 0`;
     - unknown id → `NotFound`.
- [ ] Run. Expected: the migration tests fail (no table) and the rest fail with `AttributeError` (no store functions).
- [ ] Commit `M8 T5: store tests for quotas and kill switch`.

### T6 (coder): migration + store

**Files:** create `supabase/migrations/20261123000000_m8_abuse.sql` (§1.4); modify `src/swf/store.py`.

Key code (complete the other functions per §1.5):

```python
def check_quota(conn: Conn, user_id: uuid.UUID, day: date, limits: AbuseLimits) -> str | None:
    conn.execute("select pg_advisory_xact_lock(hashtext('swf.quota:' || %s::text))", [user_id])
    usage = get_usage(conn, user_id, day)
    if usage.tasks >= limits.tasks_per_day:
        return "quota_tasks_per_day"
    if usage.cost_usd >= limits.cost_per_day_usd:
        return "quota_cost_per_day"
    row = conn.execute(
        "select count(*) as n from public.tasks where user_id = %s and status <> all(%s)",
        [user_id, list(TERMINAL_STATUSES)],
    ).fetchone()
    assert row is not None
    return "quota_concurrent_tasks" if row["n"] >= limits.concurrent_tasks else None

def admin_kill(conn: Conn, admin_id: uuid.UUID, user_id: uuid.UUID | None) -> list[uuid.UUID]:
    if user_id is not None:
        if conn.execute("update public.profiles set disabled = true where user_id = %s returning user_id",
                        [user_id]).fetchone() is None:
            raise NotFound
    else:
        conn.execute("update public.system_state set tasks_paused = true, updated_at = now(), updated_by = %s",
                     [admin_id])
    rows = conn.execute(
        "select id from public.tasks where status <> all(%s) and (%s::uuid is null or user_id = %s)"
        " order by created_seq for update",
        [list(TERMINAL_STATUSES), user_id, user_id],
    ).fetchall()
    ids = []
    for r in rows:
        _expire_pending(conn, r["id"])
        transition_task(conn, r["id"], "cancelled", error="admin_kill")
        ids.append(r["id"])
    scope = "global" if user_id is None else "user"
    _audit(conn, admin_id, "admin.kill", "global" if user_id is None else str(user_id),
           {"scope": scope, "cancelled": len(ids)})
    return ids
```

- [ ] Add `if tasks_paused(conn): return None` right after the advisory lock in `claim_next_task`.
- [ ] Green: `tests/test_m8_store.py tests/test_db_rls.py tests/test_store_capacity.py tests/test_m4_store.py`. Commit `M8 T6: system_state migration, quota and kill-switch store functions`.

### T7 (test-writer): API quotas, 429, admin

**Files:** modify `tests/conftest.py` (`PERMISSIVE`, `build_deps(..., abuse=PERMISSIVE)` keyword); create `tests/test_m8_api.py` with a fixture `m8_client(limits)` that builds a client over `fresh_db` with `AppDeps(abuse=limits, monotonic=<manual clock>)`.

- [ ] Tests (reuse `test_api.py`'s linked-user pattern):
  1. `tasks_per_day`:
     - `AbuseLimits(concurrent_tasks=100)`; 5 creates → 201;
     - the 6th → 429 `quota_tasks_per_day`, with `Retry-After` = seconds from the fixed `clock` to the next UTC midnight;
     - task count unchanged, no new `task.create` audit row;
     - `len(github.requests)` unchanged by the 6th call.
  2. Concurrent (defaults):
     - first → 201 `queued`; second → 429 `quota_concurrent_tasks`, no `Retry-After`;
     - after `POST /cancel` of the first → 201.
  3. A pre-flight `rejected` task (AGENTS.md asks 8192 MB) does not block the next create (201).
  4. Daily cost: `quotas_usage.cost_usd = 3.0` for today → 429 `quota_cost_per_day`.
  5. Race: `concurrent_tasks=1`, two `TestClient`s on the same app, posting at once from 2 threads → sorted statuses `[201, 429]`, and exactly one task row.
  6. Rate limit:
     - `AbuseLimits(api_burst=3, api_per_minute=60)`: 3× `GET /v1/tasks` → 200; the 4th → 429 `rate_limited`, `Retry-After: 1`;
     - a second user → 200;
     - `/healthz` 10× → 200;
     - a bad token → 401 (not 429);
     - after `monotonic += 1` → 200.
  7. Create bucket: `AbuseLimits(create_burst=1, concurrent_tasks=100)` → second create → 429 `rate_limited`, with no GitHub request.
  8. Admin:
     - a non-admin `POST /v1/admin/kill {}` → 403 `admin_only`, nothing cancelled, no audit;
     - unauthenticated → 401;
     - admin (`update profiles set is_admin = true`) kills user A → 200 `{"scope": "user", "user_id": A, "cancelled": [id]}`; A's `GET /v1/me` → 403 `account_disabled`;
     - kill self → 422 `cannot_kill_self`; unknown user → 404;
     - global kill → user B's create → 503 `service_paused`;
     - `POST /v1/admin/resume {}` → B's create → 201;
     - resume A → A's `GET /v1/me` → 200.
- [ ] Run. Expected: `TypeError` on `AppDeps(abuse=…)`, then 404 on the admin routes.
- [ ] Commit `M8 T7: API tests for quotas, rate limits, admin kill`.

### T8 (coder): API

**Files:** `src/swf/api/app.py`, `src/swf/api/main.py`.

```python
api_limiter = RateLimiter(per_minute=deps.abuse.api_per_minute, burst=deps.abuse.api_burst, clock=deps.monotonic)
create_limiter = RateLimiter(per_minute=deps.abuse.create_per_minute, burst=deps.abuse.create_burst, clock=deps.monotonic)

def _limit(limiter: RateLimiter, user_id: uuid.UUID) -> None:
    wait = limiter.hit(user_id)
    if wait is not None:
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(max(1, math.ceil(wait)))})

_DAILY = {"quota_tasks_per_day", "quota_cost_per_day"}

def _quota_refused(code: str) -> HTTPException:
    headers = None
    if code in _DAILY:
        now = deps.clock().astimezone(UTC)
        midnight = datetime.combine(now.date() + timedelta(days=1), dt_time(0), UTC)
        headers = {"Retry-After": str(max(1, math.ceil((midnight - now).total_seconds())))}
    return HTTPException(429, code, headers=headers)
```

- [ ] `current_user` ends with `_limit(api_limiter, principal.user_id)`.
- [ ] `create_task` follows §1.6 steps 1–5. Raising inside `with deps.db.backend()` rolls back.
- [ ] `admin_user` dependency, `KillIn`, `KillOut`, `ResumeOut`, and the two routes per §1.6.
- [ ] Update the module docstring (M8 lines; remove "quota enforcement / rate limits / kill switch (M8)" from Deferred).
- [ ] `main.py`: `abuse=AbuseLimits.from_env()`; the docstring says `--workers 1`.
- [ ] Green: `tests/test_m8_api.py tests/test_api.py tests/test_m4_sse.py tests/test_m5_store_api.py`. Commit `M8 T8: quotas, 429 rate limiting, admin kill/resume in the API`.

### T9 (test-writer): worker stop loop, cost budget, kill in Docker

**Files:** create `tests/test_m8_worker.py` (fake env, `fresh_db`) and `tests/test_m8_kill_docker.py` (`docker`, `network` marks; runs under runsc on the VM through the env default).

- [ ] `test_m8_worker.py` (reuse `tests/m4_fakes.py`):
  1. **Blocking env.** A `BlockingEnv(FakeEnv)` whose `execute` blocks on a `threading.Event` that only `cleanup()` sets (record `cleanup` call times). AGENTS.md `setup: ["sleep 600"]`, `AcceptAllEvidence()`. Run `Worker.serve(stop)` in a thread with `WorkerConfig(stop_poll_seconds=0.2, heartbeat_seconds=1, lease_seconds=5)`.
     - Wait until the status is `preparing` and execute was entered; then `store.admin_kill(conn, admin, None)`.
     - `cleanup` was called within 2 s; the runner thread ended within 5 s;
     - final status `cancelled`, error `admin_kill`;
     - no event after the cancel `status` event.
  2. The same with `store.cancel_task` (user cancel) → `cleanup` within 2 s.
  3. Lost lease: `update tasks set worker_id = 'other'` while it runs → `cleanup` is called at the next heartbeat.
  4. **Pause.** After a global `admin_kill`, a newly queued task: `worker.run_once()` returns `None` and the task stays `queued`.
  5. **Budget.**
     - `quotas_usage.cost_usd = 3.0` on the task's day → `run_once()`: status `failed`, error `quota_cost_per_day`, `env_factory` never called;
     - with day usage 2.5, a `TaskRunner` run of a scripted `complete_task` script → `runner.agent.config.cost_limit == approx(0.5)`;
     - with `config.agent={"cost_limit": 0.2}` → `approx(0.2)`.
  6. `WorkerConfig(stop_poll_seconds=0)` and `=11` → `ValueError`.
- [ ] `test_m8_kill_docker.py`: real `open_task_sandbox` env factory over the session `egress`, `LocalCopyWorkspace` of a dir with an AGENTS.md (`image: python:3.12-slim`, `setup: ["sleep 600"]`, `test: "true"`), `AcceptAllEvidence()`, `stop_poll_seconds` default (5).
  - Wait until `docker ps -q --filter label=swf.task=<id>` is non-empty and a `setup` event has not yet arrived.
  - `t0 = monotonic()`; global `admin_kill` through the API (`m8_client` pattern).
  - Poll until `docker ps -aq --filter label=swf.task=<id>` and `docker network ls -q --filter label=swf.task=<id>` are both empty. Assert `monotonic() - t0 <= 30`, then status `cancelled`.
  - Print the measured seconds (evidence).
  - First assert `policy.classify("sleep 600", phase="prepare").decision != "forbid"`.
- [ ] Run. Expected: `TypeError` (`stop_poll_seconds` unknown); the docker test times out (> 30 s, the container stays up while `sleep 600` runs).
- [ ] Commit `M8 T9: worker kill-switch and cost-budget tests`.

### T10 (coder): worker

**Files:** `src/swf/worker.py`.

```python
def kill(self) -> None:
    """Kill switch / cancel / lost lease (m8 §0.12): stop, and remove the container now."""
    self.stopped = True
    env = self.env
    if env is not None:
        try:
            env.cleanup()
        except Exception:
            logger.exception("killing task %s failed", self.task.id)

def enforce_stops(self) -> list[uuid.UUID]:
    ids = self._running_ids()
    if not ids:
        return []
    with self.deps.db.backend() as conn:
        stopped = store.stopped_task_ids(conn, ids)
    with self._lock:
        runners = [self.running[t] for t in stopped if t in self.running]
    for runner in runners:
        runner.kill()
    return stopped
```

- [ ] `_stop_loop(stop)`: `while not stop.wait(cfg.stop_poll_seconds): enforce_stops()` (log exceptions). Start it with the heartbeat thread in `serve` and `run_once`, and join it the same way.
- [ ] `heartbeat`/`reconcile`: `runner.kill()` replaces `runner.stop()`. Shutdown in `serve`'s `finally` keeps `stop()`.
- [ ] Budget and `cost_limit` per §1.7. `main()` per §1.7.
- [ ] Green: `tests/test_m8_worker.py tests/test_m8_kill_docker.py tests/test_m4_worker.py tests/test_m5_worker.py tests/test_m6_worker.py tests/test_m7_worker.py`. Commit `M8 T10: worker stop loop, kill() removes the sandbox, cost budget`.

### T11 (test-writer): host isolation + ops script checks

**Files:** create `tests/test_m8_host_isolation.py` (`docker`, `network`) and `tests/test_m8_ops.py`.

- [ ] `test_m8_host_isolation.py`:
  - Start a TCP listener in the test process on `0.0.0.0:<free port>`. Open a task sandbox. Read the gateway with `docker network inspect <net> -f '{{(index .IPAM.Config 0).Gateway}}'`.
  - In the sandbox, `python -c` a 3 s socket connect to `gateway:port` → must fail.
  - If the host accepts on `127.0.0.1:22`, connecting from the sandbox to `gateway:22` must also fail.
  - Control: the proxy port at `proxy_ip:3128` connects.
  - On Docker Desktop this passes trivially. It matters on the VM (red before the firewall rule; T13 shows both).
- [ ] `test_m8_ops.py`: for `ops/vm-bootstrap.sh`, `ops/vm-sync.sh`, `ops/vm-test.sh`:
  - the file exists, has no `\r`, and starts with `#!/usr/bin/env bash` followed by `set -euo pipefail`;
  - `bash -n` passes;
  - no `git push`;
  - `vm-sync.sh` uses `git bundle create` and never names `.env`;
  - `vm-bootstrap.sh` contains `runsc install`, `-i br-+ -j DROP`, and the uid 1000 check;
  - `vm-test.sh` sets `SWF_SANDBOX_RUNTIME=runsc` and `PYTHONDONTWRITEBYTECODE=1` and runs `-m "not live"`.
- [ ] Run. Expected: the ops tests fail (no files); the isolation test passes locally (note it in the report).
- [ ] Commit `M8 T11: host-isolation and ops-script tests`.

### T12 (ops): VM scripts

**Files:** create `ops/vm-bootstrap.sh`, `ops/vm-sync.sh`, `ops/vm-test.sh`; `.gitignore` (+`.m8-vm/`). LF only.

`ops/vm-bootstrap.sh`: run once on the VM as `sudo bash vm-bootstrap.sh`. It must be idempotent and must not print secrets.

```bash
#!/usr/bin/env bash
set -euo pipefail
# M8: Docker Engine + gVisor runsc + uv on Ubuntu 24.04 aarch64 (Oracle A1). Run with sudo.
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || echo "warning: expected aarch64, got $(uname -m)" >&2
. /etc/os-release; [ "$VERSION_ID" = "24.04" ] || echo "warning: expected Ubuntu 24.04, got $VERSION_ID" >&2
arch="$(dpkg --print-architecture)"
apt-get update -q && apt-get install -yq ca-certificates curl gnupg git iptables-persistent
install -m 0755 -d /etc/apt/keyrings
# Docker Engine (official apt repo)
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
# gVisor (official apt repo)
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$arch signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" > /etc/apt/sources.list.d/gvisor.list
apt-get update -q && apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin runsc
runsc install            # adds the runsc runtime to /etc/docker/daemon.json
systemctl restart docker
# Test user must be uid 1000 (sandbox --user 1000:1000 writes the bind-mounted workspace; m8 §0.15)
u="$(getent passwd 1000 | cut -d: -f1 || true)"
if [ -z "$u" ]; then useradd -m -u 1000 -s /bin/bash swf; u=swf; fi
usermod -aG docker "$u"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "$u" ] && [ -f "/home/$SUDO_USER/.ssh/authorized_keys" ]; then
  install -d -m 700 -o "$u" -g "$u" "/home/$u/.ssh"
  install -m 600 -o "$u" -g "$u" "/home/$SUDO_USER/.ssh/authorized_keys" "/home/$u/.ssh/authorized_keys"
fi
# uv (pinned to the dev machine's version)
sudo -u "$u" -H sh -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/0.13.5/install.sh | sh'
# Sandboxes must not reach host services via their bridge gateway (m8 §0.14)
iptables  -C INPUT -i br-+ -j DROP 2>/dev/null || iptables  -I INPUT 1 -i br-+ -j DROP
ip6tables -C INPUT -i br-+ -j DROP 2>/dev/null || ip6tables -I INPUT 1 -i br-+ -j DROP
netfilter-persistent save
docker run --rm --runtime=runsc alpine:3 dmesg | head -3
echo "ok: test user = $u (uid 1000); docker runtimes: $(docker info --format '{{json .Runtimes}}')"
```

`ops/vm-sync.sh`: run on Windows Git Bash or Linux. Usage `bash ops/vm-sync.sh <user>@<host> [branch]`. It ships only committed history and never pushes to GitHub.

```bash
#!/usr/bin/env bash
set -euo pipefail
# Copy a branch to the VM with git bundle + scp (no GitHub push before M9). Only committed state.
target="${1:?usage: vm-sync.sh user@host [branch]}"
branch="${2:-$(git rev-parse --abbrev-ref HEAD)}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git bundle create "$tmp/swf.bundle" "$branch"
scp "$tmp/swf.bundle" "$target:swf.bundle"
ssh "$target" bash -s -- "$branch" <<'EOF'
set -euo pipefail
branch="$1"
git bundle verify ~/swf.bundle >/dev/null
[ -d ~/swf/.git ] || git clone --branch "$branch" ~/swf.bundle ~/swf
cd ~/swf
git fetch ~/swf.bundle "$branch"
git checkout -f -B "$branch" FETCH_HEAD   # the VM copy is a mirror: local edits are discarded
git log --oneline -1
EOF
```

`ops/vm-test.sh`: run on the VM from `~/swf` as the uid-1000 user. Extra arguments go to pytest.

```bash
#!/usr/bin/env bash
set -euo pipefail
# M8 DoD on the VM: the non-live suite with sandboxes under gVisor. Log in .m8-vm/.
cd "$(dirname "$0")/.."
mkdir -p .m8-vm
log=".m8-vm/pytest-$(date -u +%Y%m%dT%H%M%SZ).log"
{ uname -m; docker info --format '{{json .Runtimes}}'; runsc --version | head -1; git log --oneline -1; } | tee "$log"
SWF_SANDBOX_RUNTIME=runsc PYTHONDONTWRITEBYTECODE=1 ~/.local/bin/uv run pytest -o addopts="" -q -rs -m "not live" "$@" 2>&1 | tee -a "$log"
echo "leftovers:" | tee -a "$log"
docker ps -a --filter label=swf.managed=1 --format '{{.Names}}' | tee -a "$log"
```

- [ ] Green: `tests/test_m8_ops.py`. Commit `M8 T12: VM bootstrap, bundle sync, VM test runner`.

### T13 (ops, needs the user: Q4): run on the Oracle VM

- [ ] The user (or an agent with the user's OK and the ssh details): `scp ops/vm-bootstrap.sh <admin>@<vm>:` then `ssh <admin>@<vm> sudo bash vm-bootstrap.sh`. Capture the final `ok:` line.
- [ ] Red evidence for the firewall rule (optional, if the user agrees): before bootstrap's iptables step, `tests/test_m8_host_isolation.py` fails on `gateway:22`.
- [ ] `bash ops/vm-sync.sh <uid1000-user>@<vm> feature/m8`.
- [ ] `ssh <uid1000-user>@<vm> 'cd ~/swf && bash ops/vm-test.sh'`. Copy the log back: `scp <user>@<vm>:swf/.m8-vm/pytest-*.log .m8-vm/`.
- [ ] Done when: 0 failed; `test_m8_gvisor.py` ran (not skipped); the `test_m8_kill_docker` seconds are printed and ≤ 30; `leftovers:` is empty.
- [ ] Any failure becomes a new numbered fix task (test-writer red, coder green, re-sync, re-run). No code is edited on the VM.

### T14 (ops/docs): close-out

**Files:** this file (§5 results, Status), `docs/software-factory/implementation-plan.md` (§18 M8 checkboxes, result, decisions; §4 adds `POST /v1/admin/resume` if Q2 is approved; §9 adds `system_state`), `docs/HANDOVER.md` (stage table, waiting-on-user, next action), `docs/software-factory/problem-specification.md` §12 (final quota values once the user gives them). Update each artifact's "Last updated" date.

- [ ] Commit `M8 docs: close-out`. Then stop for user review.

## 4. Definition of done (evidence commands)

Local (Windows, Docker Desktop, runc):

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs -m "not live"   # 0 failed; gvisor tests skipped with reason
PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs tests/test_m8_*.py
uv run ruff check && uv run ruff format --check && uv run mypy src
docker ps -a --filter label=swf.managed=1 --format '{{.Names}}'               # empty
```

Oracle VM (ubuntu 24.04 aarch64, runsc), plan §18 M8 DoD:

```bash
bash ops/vm-sync.sh <user>@<vm> feature/m8                                    # from the dev machine
ssh <user>@<vm> 'cd ~/swf && bash ops/vm-test.sh'                             # 0 failed; test_m8_gvisor ran
ssh <user>@<vm> 'cd ~/swf && bash ops/vm-test.sh tests/test_m8_kill_docker.py tests/test_m8_gvisor.py tests/test_m8_host_isolation.py tests/test_sandbox_docker.py tests/test_seeded_repos_container.py tests/test_m4_sandbox_tools.py'
```

| Spec criterion (§10 / plan §18 M8) | Proved by |
|---|---|
| Over quota refused before start | `test_m8_api` 1, 2, 4, 5; `test_m8_worker` 5 |
| 429 rate limiting | `test_m8_api` 6, 7 |
| Kill switch stops running tasks ≤ 30 s (per user + global) | `test_m8_kill_docker` (measured seconds, on the VM under runsc); `test_m8_worker` 1–3; `test_m8_store` 3, 5 |
| Sandbox suite passes under `runsc` | VM run of the full non-live suite; `test_m8_gvisor` |

## 5. Results

(Filled in at T14.)

## 6. Deferred

- Supabase Free project creation and the `SET ROLE authenticated` check: M9, unless Q1 says otherwise.
- A shared, multi-process rate limiter, and per-IP limits for unauthenticated traffic (needs a reverse proxy: M9/M10).
- Quota and limit display in `GET /v1/me` and the UI: M10.
- Interrupting an in-flight model HTTP call on kill (today the container is removed and the result discarded).
- Running the Squid proxy under runsc.
- Linux workspace chown for non-1000 host users: M9 (m2 deferred). M8 uses a uid-1000 account.
- 1-day package cache: M9.

## 13. Amendments (user, 2026-11-21): override §0–§6

**A1 Supabase Free project in M8 (resolves Q1, replaces §0.4's "no Supabase" and the first §6 bullet).**
- Local/VM Postgres (`postgres:17-alpine`, or `SWF_TEST_DATABASE_URL`) stays the default test DB.
- The Supabase run is an extra check, marked `supabase`.
- Env vars (in `.env` only, never printed or committed):
  - `SWF_SUPABASE_DB_URL`: the direct or session-pooler URL, port 5432, `postgres` role. Tests use it.
  - `SWF_DATABASE_URL` and `SWF_SUPABASE_URL`: app settings, unchanged.
- The conftest shim path is not used against Supabase: Supabase already has `auth`.
- Free-tier limits: 500 MB DB; the project pauses after about 7 days of low activity (the keep-alive ping is M9/M10, plan §16); $0 only, no paid add-ons (no compute upgrade, PITR, custom domain, or extra project beyond the free 2).

**New T6a (test-writer, after T6): `tests/test_m8_supabase.py`.**
- Marks: `pytest.mark.supabase` (registered in `pyproject.toml`). It skips with a reason when `SWF_SUPABASE_DB_URL` is unset.
- Every check runs in one transaction that is rolled back. No rows persist.
- Checks:
  1. Every file in `supabase/migrations/` is applied: `public.system_state` exists with one row, and the `tasks.preflight` column exists.
  2. RLS is enabled on every `public` table.
  3. `has_table_privilege` is false for `anon` and `authenticated` on `github_tokens` and `system_state`. For `authenticated` on `tasks`, only SELECT is true.
  4. `set local role authenticated` plus `request.jwt.claims` works for the backend role (plan §4 [A]).
  5. Cross-user read denial: insert two `auth.users` + `profiles` + one task each, then as user A see only A's task. If inserting into `auth.users` is refused, skip check 5 with that reason.
- No URL or password appears in assertion messages.
- Red: run with the variable set before T6b → fails (project or migrations missing).

**New T6b (ops, lead).**
- Cost check: Supabase MCP `get_cost` (type project) must show $0, then `confirm_cost`. Then `create_project` (Free plan, region close to the VM). Fallback: the user creates it in the dashboard.
- Apply each file in `supabase/migrations/` in name order with MCP `apply_migration` (name = file stem). Confirm with `list_migrations`.
- The user puts `SWF_SUPABASE_DB_URL` into the main checkout's `.env`. The lead never prints it.
- Evidence: `PYTHONDONTWRITEBYTECODE=1 uv run --env-file .env pytest -o addopts="" -q -rs -m supabase tests/test_m8_supabase.py` → passed, plus MCP `get_advisors` (security) shows no RLS or grant findings on our tables.
- §4 DoD adds that command. T14 records the project ref, not the keys.

**A2 `POST /v1/admin/resume` approved (resolves Q2).** §0.11 and §1.6 stand as written.

**A3 Rate-limit defaults approved** (120/min burst 60; 6 creates/min burst 3), so §0.17 is [D]. Quota values stay at the spec defaults, still **[Q]** pending the user (§0.5, Q3).

**A4 Lead runs the VM steps over `ssh swf-vm` (resolves Q4; changes T12, T13).** `swf-vm` is a `~/.ssh/config` Host alias with user `ubuntu`, which has passwordless sudo.
- T12 scripts are idempotent and non-interactive.
  - `vm-bootstrap.sh` sets `export DEBIAN_FRONTEND=noninteractive` and preseeds `iptables-persistent/autosave_v4` and `autosave_v6` to `true` with `debconf-set-selections` before installing.
  - `vm-sync.sh` and the T13 commands pass `-o BatchMode=yes` to ssh and scp.
  - T11 `test_m8_ops.py` also asserts `DEBIAN_FRONTEND=noninteractive`, `debconf-set-selections` and `BatchMode=yes`.
- T13 commands:
  - `scp -o BatchMode=yes ops/vm-bootstrap.sh swf-vm:` then `ssh -o BatchMode=yes swf-vm sudo bash vm-bootstrap.sh`. Running it twice must succeed.
  - The test target is `ubuntu@swf-vm` if bootstrap reports test user `ubuntu`, else `swf@swf-vm`. Bootstrap copies `ubuntu`'s `authorized_keys` to `swf`.
  - `bash ops/vm-sync.sh <target> feature/m8`, `ssh -o BatchMode=yes <target> 'cd ~/swf && bash ops/vm-test.sh'`, then `scp` the log back.
- The user's go-ahead for these VM changes is this amendment.

**A5 Oracle VM dropped; the gVisor DoD run moves to GitHub Actions (user, 2026-11-22; changes T6b, T13, §4; supersedes A4's VM steps).** The Oracle VM needs a credit card.
- CI repo: a new private repo `ShreyaasAI/swf-ci`. The lead creates it and pushes `feature/m8` there. `ShreyaasAI/Fixable` is untouched; "no push to Fixable before M9" still holds.
- Runner: hosted x86_64 `ubuntu-24.04`. arm64 runners are not free for private repos, so Arm coverage is deferred to the M9 host decision.
- Workflow: `.github/workflows/m8-gvisor.yml`. Triggers: `workflow_dispatch` and push to `feature/m8` only. Job timeout 45 min, pytest step 35 min (free tier: 2000 min/month).
- No repo secrets. The run is `-m "not live"` only; checkout uses `persist-credentials: false`.
- Steps:
  - Install runsc from the official gVisor apt repo, then `sudo runsc install`, restart Docker, and check that `docker info` lists runsc.
  - Ensure something listens on host port 22 (install and start `openssh-server` if not), so the `gateway:22` case of `test_m8_host_isolation` is meaningful.
  - Run `test_m8_host_isolation` once before the firewall rule (red evidence, not gating). Then add `iptables -I INPUT 1 -i br-+ -j DROP` (and the ip6tables rule).
  - Run pytest as a uid-1000 user (reuse one or create `swf`, add it to the docker group, chown the checkout to it). Sandboxes write the bind-mounted workspace as `1000:1000`, and the runner user is uid 1001 (§0.15).
  - Run `PYTHONDONTWRITEBYTECODE=1 SWF_SANDBOX_RUNTIME=runsc uv run pytest -o addopts="" -q -m "not live" -rs --junitxml=...`, with uv pinned to 0.13.5 and Python from `.python-version`.
  - Gate: `ops/ci_check_junit.py` fails if `test_m8_gvisor` has no cases, any of its cases is skipped or failed, or any test is skipped for "gVisor unavailable" or "Docker unavailable".
  - Upload the log and the JUnit XML as an artifact. Fail if any `label=swf.test=1` or `label=swf.managed=1` container is left.
- T13 is now "CI run": push to `swf-ci`, then read the run result and artifact. Done when the same conditions as T13 hold, measured on the CI runner. `ops/vm-*.sh` stay for a future Linux host; the runsc install is inlined in the workflow rather than factored out, because `vm-bootstrap.sh` is copied to a host as a single file.
- Supabase (T6b): the existing empty Free project "Fixable" (ref `yztwunzllretrgsrauuf`) is used instead of a new one. All 5 migrations were applied by the lead on 2026-11-22 via MCP. The security advisor shows only INFO "RLS enabled, no policy" for `github_tokens` and `system_state`, which is intended (backend-only tables). The T6b pytest evidence run waits for the user to add `SWF_SUPABASE_DB_URL` to `.env`.
- M9 host: open question, decided at M9.
