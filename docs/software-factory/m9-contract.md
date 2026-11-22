# M9 contract: concurrency + evaluation

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development. Tasks run in order, one implementer each, and each is followed by a spec review and a code-quality review. Steps use checkboxes (`- [ ]`).

- **Purpose:** The interface contract and ordered task list for M9. If something here is wrong, report it. Do not silently diverge.
- **Last updated:** 2026-11-23
- **Status:** Draft v2: user answers Q1–Q4 and review fixes applied (2026-11-22); awaiting user approval to build. Branch `feature/m9` (worktree `.claude/worktrees/m9-plan`), on top of `4f982a4` (M8 merged).
- **Sources:** plan §5, §7, §13, §14, §16, §18 M9; spec §4 items 12–13, §8 (package cache row), §10 (criteria), §12 Q1 and Q3; research plan §6 item 9, §7.2, §8, RQ3, RQ9; m2 §0.2, §4, A6; m6 §0; m7 §13; m8 §0.15, §5, §6, §13 A5.
- **Roles [D, user]:** Opus = planner, coder, reviewer. Sonnet = bash/ops, docs, test writer. Haiku = tester (runs suites, reports counts and failures, edits nothing). $0 infrastructure only.

**Goal:** All 10 seeded repos run through the full pipeline (API → pre-flight → worker → sandbox → live agent → harness → real PR) once with one task at a time and once with 2 tasks at a time. The results table shows per-task wall time under concurrency ≤ 1.25× single and a resolve-rate drop ≤ 10 points. Tasks install packages through a 1-day pull-through package cache they can only read. Sandboxes run as the host user on Linux, so workspaces need no chown.

**Architecture:**
- New stdlib-only module `swf.pkgcache`: a small PyPI pull-through HTTP server. It runs in its own hardened container on the egress network, fetches upstream through Squid, verifies sha256, keeps entries 24 h, and answers GET/HEAD only. Each task network gets the cache next to Squid; the sandbox gets `PIP_INDEX_URL` pointing at it.
- `SandboxSpec.user` comes from `sandbox_user()`: the host uid:gid on POSIX hosts, `1000:1000` on Windows, never root.
- New package `swf_eval` (under `src/`, harness-only, never imported by `swf`). It starts a local Postgres, the real API (uvicorn) and the real worker as subprocesses. It acts as two eval users over HTTP, auto-approves, records metrics, checks each PR head independently with the seed's pristine tests, and writes the results table.

**Tech stack:** Python 3.14, FastAPI/uvicorn, psycopg, httpx, pytest, Docker Desktop (Windows, x86_64, runc) for the eval; GitHub Actions (x86_64, runsc) for the non-live suite. No new Python dependencies.

---

## 0. Decisions, assumptions, open questions

### User decisions (2026-11-22)

1. [D, user] **Eval host = the local PC.** Windows, Docker Desktop, x86_64, runc. Docker Desktop has no `runsc`, so the eval runs without gVisor. gVisor is already proven on CI (m8 §13 A5: full non-live suite under runsc). The eval measures concurrency and model quality; the runtime changes only overhead. Carried risk: runsc overhead (Sentry, systrap) may change the wall-time ratio on a production Linux host, and arm64 stays untested.
2. [D, user] **Live models via Portkey, real PRs.** Model `@glm-4-7-flash/glm-4.7-flash` (as in M7) for every phase: the eval sets `SWF_MODEL_INVESTIGATE` and `SWF_MODEL_EXECUTE` to it (env only; see conflict C2 and §0.25). PRs go to the throwaway `ShreyaasAI/swf-pr-sandbox` only. **No push to `ShreyaasAI/Fixable` until M9 is done; the rule holds during M9.**
3. [D, user] **PR cap and cleanup.** Single pass 10 tasks + concurrent pass 10 tasks = at most 20 PRs. Hard cap 22 (two spare for an infra re-run; §0.12). After the URLs are recorded in the raw JSON and the results table, the eval's PRs are closed (not merged) and their `agent/<task-id>` branches deleted. Closed PRs stay readable as evidence.
4. [D, user] **$0 infrastructure.** Nothing new is provisioned. The eval DB is a local `postgres:17-alpine` container, not the Supabase Free project.
5. [D, user] **Public link is not M9 work** (§0.23).

### Planner decisions

6. [D, planner] **Package cache = own pull-through server, not Squid.** Squid runs `cache deny all` and sees only CONNECT tunnels (m2 §0.2, §4), so it cannot cache HTTPS without TLS interception. TLS interception would mean a CA inside every sandbox; rejected. Third-party mirrors (devpi, proxpi) were rejected: they add upload or admin surfaces and their arm64 images are unverified. Design:
   - `swf.pkgcache` is stdlib-only. It is copied into a pinned `python:3.12-slim` container (multi-arch) as `/app/pkgcache.py` and run with `python -I`.
   - Routes: `GET|HEAD /simple/<name>/` and `GET|HEAD /files/<path>`. Every other method → 405; every other path → 404.
   - Index pages are fetched from `https://pypi.org/simple/<name>/` and rebuilt, not passed through. Only links to `files.pythonhosted.org/packages/` with a `#sha256=` fragment survive. Each becomes `/files/<path>#sha256=<hex>`; `data-requires-python` and `data-yanked` are kept; PEP 658 metadata attributes are dropped.
   - A file is fetched only if its path appeared in a cached index page, so the cache is not a general fetcher. Its sha256 must match the index; on a mismatch the cache answers 502 and stores nothing. Writes are atomic (temp file + `os.replace`). Files over 200 MB → 502. Total size is capped at `MAX_CACHE_BYTES` = 5 GiB: after each stored file, the oldest stored files are evicted until the total is ≤ the cap.
   - TTL 24 h for index pages and files (file age = time since it was stored). A stale entry is never served; it is refetched and overwritten. One sweep at start removes stale entries. There is no periodic sweep, because the size cap bounds disk use.
   - Upstream goes through Squid (`HTTPS_PROXY` = the proxy's IP on a new internal network `swf-egress` that joins only the proxy and the cache). The allowlist stays the single egress choke point.
   - Container: on internal networks only (`swf-egress` plus each task network). It is never on the default bridge, so it has no direct route out. Flags: `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, `--tmpfs /tmp`, `--memory 256m`, `--pids-limit 128`, `--dns 127.0.0.1`, `--user` = `sandbox_user()`. Labels: `swf.egress=1` and a conf-hash label, never `swf.managed=1`. Cache dir = host bind `SWF_PKGCACHE_DIR` (default `./pkgcache`), mounted read-write in the cache container only.
   - **Read-only to tasks:** sandboxes never mount the cache dir. Their only access is HTTP GET/HEAD to the cache. Package hashes come from PyPI over TLS, so a task cannot plant a package for another task.
   - Sandbox env adds `PIP_INDEX_URL=http://<cache-ip>:3141/simple/`, `PIP_TRUSTED_HOST=<cache-ip>`, `NO_PROXY` and `no_proxy` = `<cache-ip>`. The proxy env stays, so other allowlisted HTTPS still works. The cache runs on runc even under runsc (our image and code, no user code; same reasoning as m8 §0.13 for Squid).
   - `EgressConfig.pkgcache: bool = True`. Off → no cache container and no `PIP_*` index variables (M8 behaviour).
7. [D, planner] **Linux workspace ownership without chown.** The backend clones as the host user. The sandbox now runs as that same uid:gid on POSIX hosts (`sandbox_user()`, §1.1), so the bind-mounted workspace is writable with no privilege. Files the agent creates are owned by the backend, and git inside the container sees no "dubious ownership". Rejected: `chown -R 1000:1000` after clone. It needs root or CAP_CHOWN on the host, and the backend would then lose write access to its own workspaces (snapshots, delete). Root is refused: uid 0 or gid 0 → `SandboxUserError`. A backend running as root must set `SWF_SANDBOX_USER` to a non-root `uid:gid` and own the workspaces as that user; otherwise the worker does not start. Windows/Docker Desktop keeps `1000:1000`. This replaces m8 §0.15's uid-1000 account: CI now runs the suite as the runner user (uid 1001), which is the proof. On the CI runner the primary gid is the `docker` group. That is harmless: sandboxes get no Docker socket, and the gid only tags files in the workspace.
8. [D, planner] **Seed repos on GitHub.** Each seed's `repo/` tree (only; never `reference/` or `seed.yaml`) is pushed as branch `seed/<seed-name>` of `ShreyaasAI/swf-pr-sandbox`. The commit is deterministic (fixed author, committer and dates), so re-publishing is a no-op. The push is never forced; a differing remote branch → `SeedBranchConflict`. `GitOps.push` refuses any branch that is not `agent/<uuid>` (`src/swf/gitops.py:261-263`) and stays unchanged.

   Seed branches use a separate hardened push inside `swf_eval.seeds`, with the same env as `GitOps.env(token)`: hooks off, no system or global config, askpass token, no prompts. It accepts only `^seed/[0-9]{2}-[a-z0-9-]{1,60}$` and an exact `refs/heads/<b>:refs/heads/<b>` refspec, never `+`. A task is `{repo: "ShreyaasAI/swf-pr-sandbox", base_branch: "seed/<name>", description: seed.description, failing_test: seed.target_test}`, so each PR targets its seed branch. The harness, not the backend, does this push. The backend still pushes only `agent/<task-id>`. Approved by the user (§0.24).
9. [D, planner] **Quotas for the eval without changing product behaviour.** Two eval users, `eval-a` and `eval-b`, keep the default 1 concurrent task each. Host concurrency 2 comes from the worker (`max_concurrent=2`, admission limits as in m2 §0.5). The eval API process sets only `SWF_QUOTA_TASKS_PER_DAY=12` (each user runs 10 tasks plus spare). Cost quotas stay at the defaults ($1/task, $3/day). Both JWTs carry the same GitHub `provider_id` (the `gh` account), so `POST /v1/me/github-token` passes for both. No code path changes. Final quota numbers: §0.26.
10. [D, planner] **What "resolved" means.** A task is resolved iff all of these hold:
    - its status is `completed` and it has a `pr_url`;
    - a check tree is built from the pristine seed `repo/` (§1.5 `build_check_tree`):
      - Each pristine **non-protected** file is replaced by the agent's version, taken from a fresh clone of the PR head (branch `agent/<task-id>`).
      - Protected files (tests, `conftest.py`, `pytest.ini`, `AGENTS.md`, other config) always stay pristine.
      - Files the agent added are ignored and reported as `files_added`. So a planted `pytest.py`, `pytest.toml`, `conftest.py` or `sitecustomize.py` cannot change the run.
      - A pristine non-protected file the agent deleted stays deleted (`files_deleted`).
    - in a fresh sandbox with the seed's image and `setup`, both `python -m pytest -q <target_test>` and `python -m pytest -q` exit 0.

    The check runs after both passes, sequentially, so it does not load the host during a timed pass.

    Controls run for all 10 seeds first and validate the checker: the pristine tree must not resolve (target exit ≠ 0), and pristine + `reference/` must resolve. A failed control stops the eval.

    Two lists are reported. `tests_touched` holds the protected pristine files the PR changed or deleted, plus protected-shaped files it added. `files_added` holds every added file. Protected files stay pristine and added files are ignored, so a fix that only edits or adds tests does not resolve.

    Known limit: a correct fix that needs a new module does not resolve. No seed needs one.
11. [D, planner] **How wall time is measured and compared.**
    - Per-task wall = from the task's `status` event `to: "preparing"` (worker claim) to its `status` event into a terminal status. Server timestamps from `events.created_at`. Queue time is excluded: queueing is expected at 2 concurrent and not a slowdown.
    - DoD metric: `wall_ratio = median(concurrent per-task walls) / median(single per-task walls)`, required ≤ 1.25. The spec text "wall time ≤ 1.25× single-task" compares one task's latency with and without a neighbour. A batch total cannot be compared with a single task. The median is robust to one runaway trajectory.
    - Each median needs ≥ 8 tasks with both timestamps; otherwise `wall_ratio = None` and `wall_ok = False` ("insufficient data").
    - Also reported, not gating: per-seed paired ratios; seconds per agent step (less sensitive to trajectory length); batch wall per pass (first claim → last terminal). With 2 workers the concurrent batch should take roughly 0.5–0.65 × the single batch.
    - Also reported, next to the gating median: the median over `completed` tasks only, per pass. It is informational, so a fast-failing task cannot hide a slowdown.
    - A re-run (§0.13) replaces the infra-failed record for that pass and seed in every metric. The replaced record stays in `run.json` under `replaced`, with its cause.
    - Auto-approval latency (driver reacts within ~1 s) is inside both passes equally.
12. [D, planner] **Resolve-rate drop.** `rate = resolved / 10` per pass. A task that never ran (abort) counts as unresolved. `drop = rate_single − rate_concurrent`, required ≤ 0.10. [D] The drop is in absolute percentage points (≤ 10 points = one seed of ten), not relative to the single rate. With n = 10, one flip is within noise; the DoD allows exactly that.
13. [D, planner] **Trials and variance.** One trial per pass (cap 20 PRs). Both passes run back to back on the same day with the same model, the same agent limits and a cache warmed before each pass (`warm-cache` runs every seed's `setup`, no LLM), so neither pass gets a cold-cache penalty. Provider load drift is not controlled; it is noted in the results. An infra failure gets one re-run of that seed in its pass, within the 22-PR cap. The re-run replaces the failed record (§0.11), and the cause is recorded. Infra failures are: `clone_failed`, a `pr_failed:*` stage, Docker errors, or a Portkey 5xx storm before the first model reply. A model failure (wrong fix, step or wall limit, `failed` by evidence) is a result, not re-run. If a DoD check fails, record it and stop; a second trial needs the user's OK (more PRs).
14. [D, planner] **Agent limits for both passes.** `step_limit=80`, `wall_time_limit_seconds=1800`, set through new worker env knobs `SWF_AGENT_STEP_LIMIT` and `SWF_AGENT_WALL_TIME_SECONDS` (§1.2). Unset keeps the product defaults (150, 3600). Guards stay on (`Guardrails.from_env`) because they are part of the pipeline. The guard time per task is reported separately: the M7 judge p95 is 50 s per text, and it will dominate wall time.
15. [D, planner] **Cost and hard stops.** M4's live smoke (9 calls) and M7's live corpus (102 judged texts) both cost $0. glm-4.7-flash is a free model, so the expected LLM cost is $0. litellm reports 0 for unknown prices (`cost_tracking="ignore_errors"`), so the booked `tasks.cost_usd` cannot prove $0. The user checks Portkey's usage page before and after the eval (§4).

    The runner stops (cancels running tasks, records `aborted: <reason>`) when any of these is reached:
    - total `tasks.cost_usd` ≥ $1.00 (`--budget-usd`);
    - agent model calls + guard judge chunks ≥ 4000 (`--max-calls`);
    - `pr_opened` events ≥ 22 (`--max-prs`);
    - total wall ≥ 6 h (`--max-hours`);
    - 5 consecutive tasks fail on model or provider errors (`retry` events with step `model` exhausted).

    The budget is checked after every task. In the concurrent pass, each driver thread also checks it before it creates its next task and every 30 s while it drives one. The counters are shared under a lock, so one thread cannot overrun while the other is mid-task.

    [A] One run is about 20 tasks × (≤ 80 agent calls + guard chunks) ≈ 2000–3500 Portkey requests, within the free plan's monthly allowance. The user confirms the allowance on the Portkey dashboard before the run.
16. [D, planner] **Auto-approval in the eval.** The driver approves every `plan`, `command`, `guard_flag` and `readonly_violation` approval, and answers every question with "No human is available. Use your best judgement and continue." Each decision is counted by approval kind in the results. Auto-approved `guard_flag`s are also counted by guard category (injection, toxicity, malicious, pii, secret, output_check, guard_error). Each one is listed on its own line (task, category, source), because each is a point where a real user would have had to decide. Forbidden commands never reach an approval (classifier), and the sandbox boundary is unchanged. This is an eval-only stand-in for the user; it is not in `swf`.
17. [D, planner] **Isolation of the eval stack.** The runner builds the API and worker env from scratch. It passes only the variables they need, plus `PATH` and `SYSTEMROOT` on Windows:
    - a local DB URL;
    - a fake `SWF_SUPABASE_URL=http://eval.localhost`;
    - a per-run random HS256 `SWF_SUPABASE_JWT_SECRET`;
    - a per-run random Fernet key;
    - `PORTKEY_API_KEY` (from the main checkout's `.env` via `uv run --env-file`);
    - the model, quota and agent-limit knobs;
    - workspace and cache dirs under the run dir.

    Nothing else from `.env` reaches the subprocesses (no `SWF_SUPABASE_DB_URL`). The GitHub token comes from `gh auth token` and is held in memory, then sent only to `POST /v1/me/github-token`. At the end the runner scans the run dir for the token and the Portkey key and fails the run on a hit. Secrets are never printed.
18. [D, planner] **Where results live.** Raw JSON in `.m9-eval/<run-id>/` (gitignored): `run.json` and one `tasks/<pass>-<seed>.json` per task. The table is `docs/software-factory/m9-results.md`, rendered by `swf_eval report` and committed. Implementation-plan §18 M9 links it.
19. [D, planner] **Which tests run where.** Non-live (`-m "not live"`, run locally and in CI): every `test_m9_*` file except `test_m9_eval_live.py`. That covers `sandbox_user`, agent env knobs, the pkgcache unit and Docker tests, the eval metrics, budget and report, seed publishing to a local bare repo, the driver against the real app, and checker controls on 2 seeds in Docker. Live (local only, `-m live`): `test_m9_eval_live.py` (seed branches exist on GitHub) plus the CLI eval run (§3 T11–T12), which is an experiment, not a pytest. CI: `.github/workflows/m8-gvisor.yml` also triggers on `feature/m9` and runs as the runner user (uid 1001). Pushing `feature/m9` to the private `ShreyaasAI/swf-ci` follows m8 §13 A5; Fixable stays untouched.
20. [A] Docker Desktop gives the WSL2 VM at least 4 CPUs and 6 GB. The runner records `docker info` (NCPU, MemTotal, default runtime) in `run.json` and refuses to start below 2 CPUs / 4 GB.
21. [A] glm-4.7-flash on Portkey's free route accepts 2 agents plus their guard calls at once without sustained 429s. If not, concurrent wall grows; that is a real finding (retry counts are in the table).
22. [A] Peak memory per task (RQ9) is sampled every 5 s with `docker stats --no-stream` for `label=swf.managed=1` containers. It is informational only, sampled the same way in both passes.

### Conflicts between sources (reported before code)

- **C1 Host.** Plan §7, §18 M9 title "(Oracle VM)" and spec §4 item 12 name the Oracle Arm VM with gVisor. The user's 2026-11-22 decision: eval on the local PC (x86_64, runc); Oracle dropped (needs a card). T13 updates plan §18 M9 and spec §4 item 12 / §12 Q3 to match. Spec §10's gVisor requirement for the MVP still stands for the public deploy (§0.23).
- **C2 Models.** Plan §5 sets ernie-5.5-flash for investigate/plan and glm for execute/verify. The user asked for glm-4.7-flash (as in M7). The eval uses glm for all phases via env, and the product default is unchanged. See §0.25.
- **C3 Cache location.** Research plan §8 draws the 1-day cache inside the egress proxy, and plan §7 says "egress proxy … with a 1-day package cache". Squid only tunnels HTTPS, so the cache becomes a separate container behind the proxy (§0.6). T13 updates research plan §8 and plan §7 wording.
- **C4 uid 1000.** M2/M6/M8 deferred "chown for uid 1000". This contract runs the sandbox as the host user instead (§0.7), so there is no chown. M8's tests that assert `id -u == 1000` change to assert the `sandbox_user()` uid.
- **C5 Quotas.** Spec §12 Q1 defaults allow 5 tasks/day and 1 concurrent per user; the eval needs 20 tasks and 2 at a time. Resolved without product change (§0.9).
- **C6 Plan location.** The writing-plans skill saves to `docs/superpowers/plans/`; the lead's instruction puts this contract in `docs/software-factory/` (user instruction wins).
- **C7 Paid tasks vs spec non-goals.** The user wants 2 free tasks/day, then $1 per extra task, as real paid tasks (§0.26). This conflicts with three sources:
  - spec §5 non-goals list "Billing / paid plans for open users" (quotas only in the MVP);
  - spec §5 lists "Free/Pro plans, billing" as a vision item;
  - plan §17 lists billing as a non-goal.

  Resolution: a new post-M10 payments milestone. The lead updates the spec and plan then.

### User answers (2026-11-22)

23. [D, user] **Q1 Public link: deferred to M10.** Pages + Cloudflare quick tunnel becomes the M10 path only once the PC has a gVisor runtime (for example Docker Engine + runsc in WSL2; [A] untested). The caveats stand:
    - Without gVisor, strangers' code would run on the user's PC under runc.
    - The API would need real Supabase Auth (GitHub OAuth), per-IP limits for unauthenticated traffic (m8 §6, not built) and a watched kill switch.
    - Quick-tunnel URLs change on restart and have no SLA, and the PC must stay on.
    - GitHub Actions cannot host the app: no inbound traffic, and jobs end within 6 h.
24. [D, user] **Q2 Seed branches: yes.** The harness pushes the 10 `seed/<name>` branches to `ShreyaasAI/swf-pr-sandbox` (§0.8) and deletes them after the user approves M9.
25. [D, user] **Q3 Models: glm-4.7-flash for all phases in the eval, via env.** The product default (plan §5: ernie for investigate/plan) switches to glm only if the M9 DoD passes. T12 records that change in plan §5.
26. [D, user] **Q4 Quotas: "2 free tasks per day, then $1 per extra task", as real paid tasks. Deferred to a new post-M10 milestone (payments).** It needs a payment provider, and the spec lists billing as a non-goal (conflict C7). M9 changes nothing for it: the eval quotas stay as in §0.9, and `AbuseLimits` stays at the spec defaults. The lead records the spec change later; this contract does not edit the spec.

## 1. Interfaces

### 1.1 `swf.sandbox` additions

```python
class SandboxUserError(Exception): ...

_HOST_IDS: tuple[int, int] | None = (os.getuid(), os.getgid()) if hasattr(os, "getuid") else None

def sandbox_user(env: Mapping[str, str] | None = None,
                 host_ids: tuple[int, int] | None = _HOST_IDS) -> str: ...
# 1. SWF_SANDBOX_USER set (non-blank): must match ^[1-9]\d*:[1-9]\d*$ -> returned, else ValueError naming it.
# 2. host_ids None (Windows) -> "1000:1000".
# 3. host_ids with uid 0 or gid 0 -> SandboxUserError("refusing to run sandboxes as root; set SWF_SANDBOX_USER").
# 4. else f"{uid}:{gid}".

# SandboxSpec changes:
user: str = Field(default_factory=sandbox_user, pattern=r"^[1-9]\d*:[1-9]\d*$")
index_url: str | None = None   # http://<ip>:<port>/simple/ ; validated ^http://\d{1,3}(\.\d{1,3}){3}:\d{1,5}/simple/$
```

`sandbox_run_args`: with `index_url`, it adds `PIP_INDEX_URL=<index_url>`, `PIP_TRUSTED_HOST=<ip>`, `NO_PROXY=<ip>`, `no_proxy=<ip>`. These replace the empty `NO_PROXY`/`no_proxy`. Without `index_url`, output is unchanged. `open_task_sandbox` passes `net.index_url`.

### 1.2 `swf.worker` additions

```python
def agent_overrides_from_env(env: Mapping[str, str] | None = None) -> dict[str, int]: ...
# SWF_AGENT_STEP_LIMIT -> {"step_limit": n}; SWF_AGENT_WALL_TIME_SECONDS -> {"wall_time_limit_seconds": n}
# blank/missing -> key absent; non-int or <= 0 -> SettingsError naming the variable.
```

`main()` calls `sandbox_user()` once before `ensure_egress` (fail fast on root) and passes `agent=agent_overrides_from_env()` to `WorkerConfig`.

### 1.3 `swf.pkgcache` (new, stdlib only; it must import nothing from `swf`)

```python
UPSTREAM_INDEX = "https://pypi.org/simple/"
UPSTREAM_FILES = "https://files.pythonhosted.org/packages/"
TTL_SECONDS = 86_400
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_CACHE_BYTES = 5 * 1024**3

class HashMismatch(Exception): ...

def normalize_name(name: str) -> str: ...      # PEP 503: re.sub(r"[-_.]+", "-", name).lower(); result must match
                                               # ^[a-z0-9]([a-z0-9-]{0,198}[a-z0-9])?$ else ValueError
def valid_file_path(path: str) -> bool: ...    # ^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/[A-Za-z0-9][A-Za-z0-9._+-]{0,250}$
def rebuild_index(html: str) -> tuple[str, dict[str, str]]: ...
# Parses upstream HTML (html.parser). Keeps <a> whose href starts with UPSTREAM_FILES, whose remainder
# passes valid_file_path, and whose fragment is sha256=<64 hex>. Returns (new page, {path: sha256}).
# New page: "<!DOCTYPE html><html><body>\n" + one line per kept link:
#   <a href="/files/<path>#sha256=<hex>"[ data-requires-python="<html-escaped>"][ data-yanked="<escaped>"]><filename></a><br/>
# + "</body></html>\n". Everything else (other hosts, relative links, metadata attributes) is dropped.

class Store:
    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time, ttl: float = TTL_SECONDS,
                 max_bytes: int = MAX_CACHE_BYTES) -> None: ...
    def get_index(self, name: str) -> str | None: ...                 # fresh only
    def put_index(self, name: str, page: str, hashes: Mapping[str, str]) -> None: ...
    def expected_sha(self, path: str) -> str | None: ...              # from a fresh index entry, else None
    def get_file(self, path: str) -> Path | None: ...                 # fresh only
    def put_file(self, path: str, chunks: Iterable[bytes], sha256: str) -> Path: ...  # HashMismatch / ValueError(>MAX);
                                                                      # then evicts oldest stored files until total <= max_bytes
    def total_bytes(self) -> int: ...
    def sweep(self) -> int: ...                                       # removes stale entries; returns count

class Fetcher(Protocol):
    def get(self, url: str) -> Iterator[bytes]: ...                   # raises OSError on upstream failure

class UrllibFetcher:  # urllib.request with the process env proxy (HTTPS_PROXY), timeout 60 s, no redirects off-host
    ...

def make_handler(store: Store, fetcher: Fetcher) -> type[http.server.BaseHTTPRequestHandler]: ...
def main(argv: Sequence[str] | None = None) -> None: ...            # --root DIR --port 3141 --bind 0.0.0.0; one sweep() at start
```

Handler rules:
- `GET|HEAD /simple/<name>/`: a non-normalized name → 301 to the normalized URL. Cache miss → fetch `UPSTREAM_INDEX + name + "/"` with `Accept: text/html`, then `rebuild_index`, then `put_index`. Response 200 `text/html; charset=utf-8`.
- `GET|HEAD /files/<path>`: invalid path → 404. `expected_sha(path)` None → 404. Cache miss → fetch `UPSTREAM_FILES + path`, then `put_file`. `HashMismatch` or too large → 502. Response 200 `application/octet-stream`.
- Upstream `OSError` → 502. Any other method → 405 with `Allow: GET, HEAD`. Every other path → 404. Nothing echoes request data back.
- Logs one line per request: method, path, status, `hit|miss`. No headers are logged.

### 1.4 `swf.egress` additions

```python
PKGCACHE_IMAGE = "python:3.12-slim@sha256:<coder pins the multi-arch index digest, checked with docker buildx imagetools inspect>"
class EgressConfig:  # new fields
    pkgcache: bool = True
    pkgcache_name: str = "swf-pkgcache"
    pkgcache_image: str = PKGCACHE_IMAGE
    pkgcache_port: int = 3141
    pkgcache_dir: Path = Field(default_factory=lambda: Path(os.environ.get("SWF_PKGCACHE_DIR") or "pkgcache").resolve())
    egress_network: str = "swf-egress"   # internal; proxy + cache only
@dataclass(frozen=True) class TaskNetwork:  # new field
    index_url: str | None = None
```

- `ensure_egress` also ensures the cache container when `pkgcache` is on. The flags are in §0.6; the conf-hash label covers the image, the `pkgcache.py` source bytes and the port. Same reuse/recreate rules as the proxy. Readiness = the log line `pkgcache listening on` within 30 s, else `EgressError` with the log tail. The container env holds only `HTTPS_PROXY=http://<proxy ip on swf-egress>:<port>`.
- `swf-egress` is created `--internal` if missing (exists but not internal → `EgressError`), and the proxy is connected to it.
- `attach_task_network` also connects the cache to the task network and sets `index_url=f"http://{cache_ip}:{port}/simple/"`. If it has no IP → `EgressError`.
- `detach_task_network` and `teardown_egress` disconnect or remove the cache too.

### 1.5 `swf_eval` (new package `src/swf_eval/`; `pyproject` wheel packages add `"src/swf_eval"`)

```python
# swf_eval/seeds.py
@dataclass(frozen=True)
class Seed:
    name: str; target_test: str; description: str
    repo_dir: Path        # seeded_repos/<name>/repo   (pristine; what the agent sees)
    reference_dir: Path   # seeded_repos/<name>/reference (harness-only)
def load_seeds(root: Path = SEEDED_REPOS) -> list[Seed]: ...                # sorted by name; 10 expected
FORBIDDEN_REPO = "shreyaasai/fixable"
ALLOWED_EVAL_REPOS = frozenset({"ShreyaasAI/swf-pr-sandbox"})
def check_eval_repo(repo: str) -> str: ...   # normalize_repo(repo); not in ALLOWED_EVAL_REPOS (case-insensitive) -> ValueError.
                                             # Called first by run, seed-branches, check and cleanup, before any network call.
SEED_BRANCH_RE = re.compile(r"^seed/[0-9]{2}-[a-z0-9-]{1,60}$")
class SeedBranchConflict(Exception): ...
def seed_branch(seed: Seed) -> str: ...                                     # "seed/<name>"
def publish_seed_branches(git: GitOps, repo: str, token: str, seeds: Sequence[Seed], work: Path) -> dict[str, str]: ...
# per seed: git init in work/<name>, copy repo_dir only, commit (hooks off, GIT_AUTHOR/COMMITTER_* fixed,
# dates "2026-11-22T00:00:00Z"), then push_seed_branch. Same sha remote -> skip.
# Different remote sha -> SeedBranchConflict. check_eval_repo(repo) first, so Fixable or any other repo
# -> ValueError before any git call. Returns {name: commit sha}.
def push_seed_branch(git: GitOps, path: Path, branch: str, *, token: str) -> None: ...
# Own hardened push (GitOps.push is agent/<uuid>-only and unchanged): env = git.env(token), hooks off,
# branch must match SEED_BRANCH_RE, refspec exactly refs/heads/<b>:refs/heads/<b> (no "+", no --force),
# "!" in porcelain output -> SeedBranchConflict (token redacted).

# swf_eval/resolve.py
PROTECTED_NAMES = frozenset({"AGENTS.md", "pytest.ini", "conftest.py", "setup.cfg", "tox.ini", "pyproject.toml"})
def is_protected(rel: str) -> bool: ...          # under tests/ , or basename in PROTECTED_NAMES
@dataclass(frozen=True)
class CheckTree:
    root: Path; tests_touched: tuple[str, ...]; files_added: tuple[str, ...]; files_deleted: tuple[str, ...]
def build_check_tree(seed: Seed, pr_tree: Path, dest: Path) -> CheckTree: ...
# dest must not exist. Copies seed.repo_dir to dest. For each pristine file that is not protected: present in
# pr_tree -> copy the agent's version over; absent -> delete it in dest (files_deleted). Protected files stay
# pristine; those that differ in or are missing from pr_tree -> tests_touched. Files in pr_tree that are not in
# the pristine repo are never copied -> files_added (protected-shaped ones also go to tests_touched). `.git/`
# is ignored. Symlinks in pr_tree are never followed or copied (reported in files_added). All lists sorted.
@dataclass(frozen=True)
class Resolution:
    setup_ok: bool; target_exit: int | None; suite_exit: int | None
    tests_touched: tuple[str, ...]; files_added: tuple[str, ...]
    @property
    def resolved(self) -> bool: ...  # setup_ok and target_exit == 0 and suite_exit == 0
Runner = Callable[[Seed, Path, Sequence[str]], list[int]]   # (seed, tree, commands) -> exit codes, stops at first setup failure
def check_resolution(seed: Seed, pr_tree: Path, work: Path, run: Runner) -> Resolution: ...  # build_check_tree, then run
def sandbox_runner(egress: EgressConfig) -> Runner: ...   # open_task_sandbox per call; image/resources from AGENTS.md
def run_controls(seed: Seed, run: Runner, work: Path) -> tuple[Resolution, Resolution]: ...  # (pristine, reference)

# swf_eval/client.py
class CreateRefused(Exception):
    def __init__(self, status: int, detail: str) -> None: ...
@dataclass(frozen=True)
class DriveResult:
    status: str; approvals: dict[str, int]; questions: int
EVAL_ANSWER = "No human is available. Use your best judgement and continue."
class EvalClient:
    def __init__(self, http: httpx.Client, jwt: str) -> None: ...
    def link_github_token(self, token: str) -> None: ...                    # POST /v1/me/github-token, expects 204
    def create_task(self, *, repo: str, base_branch: str, description: str, failing_test: str) -> uuid.UUID: ...
    def drive(self, task_id: uuid.UUID, *, deadline: float, clock: Callable[[], float] = time.monotonic) -> DriveResult: ...
    # Reads GET /v1/tasks/{id}/events (SSE), reconnecting with Last-Event-ID. approval_requested -> POST approve;
    # question_asked -> POST EVAL_ANSWER; status event into a terminal status -> return. deadline passed -> cancel, return.
    def cancel(self, task_id: uuid.UUID) -> None: ...

# swf_eval/localstack.py
def mint_jwt(secret: str, *, user_id: uuid.UUID, github_id: int, login: str, now: int) -> str: ...
# HS256; claims as tests/helpers.FakeUser.claims: sub, aud=authenticated, role=authenticated,
# iss="http://eval.localhost/auth/v1", exp=now+12h, user_metadata.provider_id=str(github_id), user_name=login.
def child_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]: ...  # §0.17 allowlist only
class LocalStack:  # context manager: postgres container (label swf.eval=1, 127.0.0.1 random port, random password),
    ...            # migrations + tests/fixtures/supabase_shim.sql, auth.users rows, API (uvicorn --factory
                   # swf.api.main:create_app_from_env --workers 1) and worker (python -m swf.worker) subprocesses;
                   # logs to run_dir/logs/; __exit__ stops both, removes the container.

# swf_eval/metrics.py
Pass = Literal["single", "concurrent"]
@dataclass(frozen=True)
class TaskRecord:
    pass_: Pass; seed: str; user: str; task_id: str; status: str; error: str | None
    claimed_at: float | None; finished_at: float | None
    agent_calls: int; guard_chunks: int; guard_seconds: float; retries: int
    approvals: dict[str, int]; questions: int; cost_usd: float
    pr_url: str | None; resolution: Resolution | None; peak_mem_mb: int | None
    @property
    def wall_seconds(self) -> float | None: ...
    @property
    def resolved(self) -> bool: ...          # resolution is not None and resolution.resolved
@dataclass(frozen=True)
class Verdict:
    wall_single_median: float | None; wall_concurrent_median: float | None
    wall_ratio: float | None; wall_ok: bool
    rate_single: float; rate_concurrent: float; drop: float; resolve_ok: bool
    batch_single_seconds: float | None; batch_concurrent_seconds: float | None
def verdict(records: Sequence[TaskRecord], *, seeds: int = 10, max_ratio: float = 1.25,
            max_drop: float = 0.10, min_tasks: int = 8) -> Verdict: ...
def records_from_db(conn: Conn, task_ids: Mapping[str, tuple[Pass, str, str]]) -> list[TaskRecord]: ...
# agent_calls = `message` events with payload role "assistant"; guard_chunks/seconds = sum of `guard_check`
# chunks and latency_ms/1000; retries = `retry` events; approvals by `approval_requested` kind;
# questions = `question_asked`; claimed/finished from `status` events (§0.11); cost from tasks.cost_usd.
def parse_mem_mb(mem_usage: str) -> int: ...  # "123.4MiB / 512MiB" -> 124; "1.5GiB / 4GiB" -> 1536; KiB/B handled

# swf_eval/budget.py
@dataclass(frozen=True)
class Budget:
    max_usd: float = 1.0; max_calls: int = 4000; max_prs: int = 22; max_seconds: float = 6 * 3600
    max_consecutive_model_failures: int = 5
    def exceeded(self, *, usd: float, calls: int, prs: int, elapsed: float, model_failures: int) -> str | None: ...
    # first reached limit name: "budget_usd" | "max_calls" | "max_prs" | "max_hours" | "model_failures"; >= triggers

# swf_eval/report.py
def render_results(run: Mapping[str, Any], records: Sequence[TaskRecord], v: Verdict) -> str: ...  # §1.6

# swf_eval/cleanup.py
def cleanup_plan(records: Sequence[TaskRecord], repo: str) -> list[tuple[int, str]]: ...  # (pr number, branch)
# check_eval_repo(repo) first. Number parsed from the recorded pr_url, which must be exactly
# https://github.com/<repo>/pull/<n>; branch = "agent/<task_id>" of that record, matching ^agent/[0-9a-f-]{36}$.
def apply_cleanup(api: GitHubClient, token: str, repo: str, plan: Sequence[tuple[int, str]]) -> list[str]: ...
# Per item: GET /repos/{repo}/pulls/{n}; skip with a logged reason unless html_url == recorded pr_url,
# head.repo.full_name == base.repo.full_name == repo, and head.ref == branch. Then
# PATCH {"state":"closed"} and DELETE /repos/{repo}/git/refs/heads/{branch}. Returns log lines.
# Seed branches: cleanup --seeds deletes seed/<name> refs after the user approves M9 (§0.24).

# swf_eval/__main__.py  (argparse subcommands)
# seed-branches --repo R | warm-cache | controls | run --pass single|concurrent|both --repo R --out DIR
#   [--seeds 01,02] [--budget-usd 1] [--max-calls 4000] [--max-prs 22] [--max-hours 6]
# | check --out DIR | report --out DIR [--write docs/software-factory/m9-results.md] | cleanup --out DIR [--apply] [--seeds]
```

`GitHubClient` gains two methods for cleanup: `close_pull(token, repo, number)` and `delete_branch(token, repo, branch)`. `delete_branch` refuses any branch not matching `^agent/[0-9a-f-]{36}$` or `^seed/[0-9a-z-]{1,64}$`. There is still no merge call (M6 no-merge test unchanged).

### 1.6 Results file format (`docs/software-factory/m9-results.md`)

Header: run id, date, git sha, host (`docker info` NCPU/MemTotal/runtime), model, agent limits, budget used, abort reason (or none), Portkey usage before/after (filled in by the user).

Summary:

| Metric | Single | Concurrent | Value | DoD | Met |
|---|---|---|---|---|---|
| Median per-task wall (s) | … | … | ratio c/s | ≤ 1.25 | yes/no |
| Resolve rate | x/10 | y/10 | drop (points) | ≤ 10 | yes/no |
| Batch wall (s) | … | … | ratio c/s | info | — |
| Agent calls / guard chunks | … | … | — | info | — |
| Cost (booked $) | … | … | — | ≤ $1 total | yes/no |

Per task (20 rows, sorted by pass then seed):

| Pass | Seed | User | Status | Error | Wall s | Guard s | Agent calls | Retries | Approvals (plan/cmd/flag/ro) | Questions | Cost $ | PR | Tests touched | Target | Suite | Resolved | Peak MB |

The per-task table also has a `Files added` column. The summary adds the row "Median wall, completed only (s)" (info).

Then, in order:
- auto-approved guard flags: count by category, then one line per flag (pass, seed, category, source);
- controls table (10 seeds: pristine target exit, reference resolved);
- replaced records (infra re-runs);
- notes (infra re-runs and causes, provider errors), and the PR cleanup log (closed PR numbers, deleted branches).

## 2. Test conventions (all test tasks)

- Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs <files>`.
- Fixtures: `fresh_db`, `make_user`, `build_deps`, `egress`, `egress_config`, `run_id` from `tests/conftest.py`. Docker names keep the per-run suffix (m2 A10); eval containers are labelled `swf.eval=1`.
- Docker tests mark `docker` (+ `network` when they reach PyPI). Linux-only tests skip on non-POSIX hosts with reason `host-uid ownership applies to POSIX hosts only`.
- No secret in any assertion message. Live tests read the token with `gh auth token` (m6 pattern) and the key from env.
- Tests that pin the sandbox user set `SWF_SANDBOX_USER` with `monkeypatch`, or compare with `sandbox_user()`. None hard-code `1000` except the Windows-default unit case.

## 3. Tasks

Every task is red first: write the tests, run them, see them fail for the stated reason, implement, run green, then `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, then commit. Roles: **test-writer** (Sonnet) edits `tests/` and pytest markers; **coder** (Opus) edits `src/`, `pyproject.toml`; **ops** (Sonnet) edits `.github/`, `ops/`, `.gitignore`, `docs/`; **tester** (Haiku) runs the named suites and reports; **reviewer** (Opus) reviews each task. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

### T1 (test-writer): sandbox user and agent env knobs

**Files:** create `tests/test_m9_settings.py`, `tests/test_m9_workspace_owner.py`; modify `tests/test_sandbox_unit.py:73,231`, `tests/test_sandbox_docker.py:186,196`, `tests/test_m8_gvisor.py:64` (expect `sandbox_user()`; in the unit file add an autouse fixture `monkeypatch.setenv("SWF_SANDBOX_USER", "1000:1000")` so the literal checks hold on Linux CI).

- [ ] Write the tests:

```python
# tests/test_m9_settings.py
import pytest
from swf.sandbox import SandboxSpec, SandboxUserError, sandbox_run_args, sandbox_user
from swf.settings import SettingsError
from swf.worker import agent_overrides_from_env

def test_windows_default_is_1000() -> None:
    assert sandbox_user({}, host_ids=None) == "1000:1000"

def test_posix_uses_host_ids() -> None:
    assert sandbox_user({}, host_ids=(1001, 127)) == "1001:127"

@pytest.mark.parametrize("ids", [(0, 0), (0, 1000), (1000, 0)])
def test_root_is_refused(ids: tuple[int, int]) -> None:
    with pytest.raises(SandboxUserError, match="SWF_SANDBOX_USER"):
        sandbox_user({}, host_ids=ids)

def test_env_override_wins_even_for_root_host() -> None:
    assert sandbox_user({"SWF_SANDBOX_USER": "1000:1000"}, host_ids=(0, 0)) == "1000:1000"

@pytest.mark.parametrize("raw", ["0:0", "1000", "1000:0", "a:b", "-1:5", "1000:1000:1"])
def test_env_override_validated(raw: str) -> None:
    with pytest.raises(ValueError, match="SWF_SANDBOX_USER"):
        sandbox_user({"SWF_SANDBOX_USER": raw}, host_ids=None)

def test_index_url_sets_pip_env(tmp_path) -> None:
    spec = SandboxSpec(task_id="t1", image="python:3.12-slim", workspace=tmp_path, user="1000:1000",
                       proxy_url="http://10.0.0.2:3128", network="swf-task-t1",
                       index_url="http://10.0.0.3:3141/simple/")
    args = sandbox_run_args(spec)
    for pair in ("PIP_INDEX_URL=http://10.0.0.3:3141/simple/", "PIP_TRUSTED_HOST=10.0.0.3",
                 "NO_PROXY=10.0.0.3", "no_proxy=10.0.0.3", "HTTPS_PROXY=http://10.0.0.2:3128"):
        assert pair in args

def test_index_url_validated(tmp_path) -> None:
    with pytest.raises(ValueError):
        SandboxSpec(task_id="t1", image="python:3.12-slim", workspace=tmp_path, user="1000:1000",
                    proxy_url="http://10.0.0.2:3128", network="n", index_url="http://evil.example/simple/")

def test_agent_overrides() -> None:
    assert agent_overrides_from_env({}) == {}
    assert agent_overrides_from_env({"SWF_AGENT_STEP_LIMIT": "80", "SWF_AGENT_WALL_TIME_SECONDS": "1800"}) == {
        "step_limit": 80, "wall_time_limit_seconds": 1800}

@pytest.mark.parametrize("name,value", [("SWF_AGENT_STEP_LIMIT", "0"), ("SWF_AGENT_STEP_LIMIT", "x"),
                                        ("SWF_AGENT_WALL_TIME_SECONDS", "-5"), ("SWF_AGENT_WALL_TIME_SECONDS", "1.5")])
def test_agent_overrides_rejects(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        agent_overrides_from_env({name: value})
```

`tests/test_m9_workspace_owner.py` (marks `docker`; skips on non-POSIX with the §2 reason). Using `egress_config`, a workspace `tmp_path/"ws"` created by the test process:
1. `open_task_sandbox` → `id -u` output == `str(os.getuid())`.
2. `touch /repo/agent.txt && mkdir /repo/d && echo x > /repo/d/f` exits 0.
3. On the host, `os.stat(ws/"agent.txt").st_uid == os.getuid()`, and `shutil.rmtree(ws)` succeeds after `cleanup()`.
4. `stat -c %u:%g /repo/d/f` in the sandbox equals `sandbox_user()`.

- [ ] Run. Expected: `ImportError` (`SandboxUserError`, `sandbox_user`, `agent_overrides_from_env`); the owner file skips locally on Windows.
- [ ] Commit `M9 T1: tests for sandbox user and agent env knobs`.

### T2 (coder): implement §1.1 and §1.2

**Files:** `src/swf/sandbox.py`, `src/swf/worker.py`.

- [ ] Implement exactly as §1.1 and §1.2. `index_url` is not yet passed by `open_task_sandbox` (T6 does that).
- [ ] Green: `tests/test_m9_settings.py tests/test_sandbox_unit.py tests/test_sandbox_docker.py tests/test_m8_settings.py`.
- [ ] Commit `M9 T2: sandbox runs as host user; agent limit env knobs`.

### T3 (test-writer): pkgcache unit tests

**Files:** create `tests/test_m9_pkgcache_unit.py`, `tests/fixtures/pypi_simple_pytest.html` (hand-written, LF, ~6 links: 2 valid wheels with sha256 and `data-requires-python="&gt;=3.9"`, 1 yanked, 1 without a hash, 1 on another host, 1 relative link, 1 with `data-dist-info-metadata`).

- [ ] Tests:
  1. `normalize_name`: `"Py_Test.Thing"` → `"py-test-thing"`; `""`, `"../x"`, `"a/b"`, `"-x"` → `ValueError`.
  2. `valid_file_path`: a real-shaped path passes; `../etc/passwd`, an empty name, a 59-hex dir, `aa/bb/<60hex>/..` and a name with `/` fail.
  3. `rebuild_index(fixture)`: exactly the 3 hashed `files.pythonhosted.org` links survive (2 wheels + yanked). Hrefs start with `/files/`. `data-requires-python` is kept and escaped, `data-yanked` is kept, and no `data-dist-info-metadata` or `pypi.org`/other host text remains. The returned dict maps 3 paths to their hashes.
  4. `Store` with a manual clock: `put_index`, then `get_index` returns the page and `expected_sha` the hash. At `clock += 86_401`, both are None. `sweep()` removes the entries and returns > 0.
  4b. Size cap: `Store(root, max_bytes=25, clock=c)`. Store three 10-byte files at t=1, 2, 3 (each with its index hash) → the t=1 file is gone, the other two remain, and `total_bytes() <= 25`. A single file larger than `max_bytes` (but under `MAX_FILE_BYTES`) is stored, and every older file is evicted.
  5. `put_file` with the right sha → file exists, `get_file` returns it. Wrong sha → `HashMismatch`, no file and no temp file left. More than `MAX_FILE_BYTES` (monkeypatched to 10) → `ValueError`.
  6. Handler via `ThreadingHTTPServer` on `127.0.0.1:0` and a `FakeFetcher` (dict url → bytes, counts calls):
     - `/simple/pytest/` → 200, twice; upstream index fetched once.
     - `/simple/PyTest/` → 301 to `/simple/pytest/`.
     - `/files/<known path>` → 200 with exact bytes, fetched once over two GETs.
     - `/files/<valid but unknown path>` → 404, with 0 upstream calls.
     - A known path whose upstream bytes have the wrong hash → 502, and the next GET is 502 again (nothing stored).
     - `POST`, `PUT`, `DELETE` on both routes → 405 with `Allow: GET, HEAD`.
     - `/`, `/simple/`, `/files/../../etc/passwd`, `/admin` → 404.
     - Fetcher raising `OSError` → 502.
     - `HEAD /simple/pytest/` → 200 with an empty body.
  7. `swf.pkgcache` imports nothing from `swf` (AST scan of its imports: stdlib only).
- [ ] Run. Expected: `ModuleNotFoundError: swf.pkgcache`.
- [ ] Commit `M9 T3: pkgcache unit tests`.

### T4 (coder): `swf.pkgcache`

**Files:** create `src/swf/pkgcache.py`.

- [ ] Implement §1.3 with `http.server.ThreadingHTTPServer`, `html.parser.HTMLParser`, `hashlib`, `urllib.request`. `main` prints `pkgcache listening on <bind>:<port>` and flushes, runs `sweep()` once at start, with no periodic thread. `put_file` enforces `max_bytes` by evicting the oldest stored files. Storage layout (coder's choice) stays under `root`. Keys are hashed with sha256 of the name or path, so user input never becomes a filesystem path.
- [ ] Green: `tests/test_m9_pkgcache_unit.py`.
- [ ] Commit `M9 T4: stdlib PyPI pull-through cache`.

### T5 (test-writer): pkgcache in Docker with the egress stack

**Files:** create `tests/test_m9_pkgcache_docker.py` (marks `docker`, `network`); modify `tests/conftest.py` if the session `egress_config` needs `pkgcache_dir=tmp_path_factory...` (a per-session dir, never `./pkgcache`).

- [ ] Tests (each opens sandboxes with `open_task_sandbox(egress_config, EnvRequest(...))`, cleans up in `finally`):
  1. `ensure_egress` twice → the same cache container id (idempotent). `docker inspect` shows `ReadonlyRootfs` true, `CapDrop` ALL, `no-new-privileges`, the `swf.egress=1` label, no `swf.managed` label, and only internal networks.
  2. In sandbox A, `python -m pip install --quiet pytest==9.2.0` exits 0, and the cache dir on the host now holds files. Sandbox B (another task network) runs the same install → exit 0. The cache log shows `hit` for B's `pytest` wheel.
  3. Read-only to tasks:
     - from a sandbox, `python -c` urllib `POST`/`PUT`/`DELETE` to `index_url` → 405;
     - `GET <base>/files/../../etc/passwd` → 404;
     - `docker inspect` of the sandbox has no mount of the cache dir;
     - `ls /` in the sandbox shows no cache path.
  4. The cache container cannot reach the internet directly: `docker exec swf-pkgcache python -I -c "<urllib GET https://example.com without proxy>"` fails, and the same GET through the proxy for a non-allowlisted host fails with 403.
  5. With `EgressConfig(pkgcache=False)` (separate names), `sandbox_run_args` for its task has no `PIP_INDEX_URL`.
- [ ] Run. Expected: fail (`EgressConfig` has no `pkgcache`; `TaskNetwork.index_url` missing).
- [ ] Commit `M9 T5: pkgcache Docker tests`.

### T6 (coder): egress integration

**Files:** `src/swf/egress.py`, `src/swf/sandbox.py` (`open_task_sandbox` passes `index_url`), `.gitignore` (add `pkgcache/`, `.m9-eval/`).

- [ ] Implement §1.4 and the §0.6 container flags. `docker cp` the module source (`Path(swf.pkgcache.__file__)`) into the created container before start, as with `squid.conf`. Pin `PKGCACHE_IMAGE` by digest (multi-arch index including `linux/arm64/v8` and `linux/amd64`).
- [ ] Green: `tests/test_m9_pkgcache_docker.py tests/test_sandbox_docker.py tests/test_seeded_repos_container.py tests/test_m4_sandbox_tools.py`. The seeds now install pytest through the cache.
- [ ] Commit `M9 T6: package cache on every task network`.

### T7 (test-writer): eval harness tests (non-live)

**Files:** create `tests/test_m9_eval_unit.py`, `tests/test_m9_eval_seeds.py`, `tests/test_m9_eval_driver.py`, `tests/test_m9_eval_resolve_docker.py`.

- [ ] `test_m9_eval_unit.py`:

```python
import dataclasses

import pytest

from swf_eval.budget import Budget
from swf_eval.metrics import TaskRecord, parse_mem_mb, verdict
from swf_eval.resolve import Resolution

def rec(pass_, seed, wall, resolved, status="completed"):
    res = Resolution(True, 0 if resolved else 1, 0, ()) if status == "completed" else None
    return TaskRecord(pass_=pass_, seed=seed, user="eval-a", task_id=seed, status=status, error=None,
                      claimed_at=0.0, finished_at=wall, agent_calls=10, guard_chunks=5, guard_seconds=3.0,
                      retries=0, approvals={"plan": 1}, questions=0, cost_usd=0.0,
                      pr_url="https://github.com/o/r/pull/1", resolution=res, peak_mem_mb=None)

def run(single_wall, conc_wall, single_ok, conc_ok):
    return [rec("single", f"{i:02}", single_wall, i < single_ok) for i in range(10)] + \
           [rec("concurrent", f"{i:02}", conc_wall, i < conc_ok) for i in range(10)]

def test_ratio_at_limit_passes() -> None:
    v = verdict(run(100.0, 125.0, 9, 8))
    assert v.wall_ratio == 1.25 and v.wall_ok
    assert v.drop == pytest.approx(0.1) and v.resolve_ok

def test_ratio_over_limit_fails() -> None:
    assert not verdict(run(100.0, 126.0, 9, 9)).wall_ok

def test_drop_of_two_fails() -> None:
    assert not verdict(run(100.0, 100.0, 9, 7)).resolve_ok

def test_concurrent_better_is_ok() -> None:
    v = verdict(run(100.0, 90.0, 7, 9))
    assert v.wall_ok and v.resolve_ok and v.drop == pytest.approx(-0.2)

def test_insufficient_walls_fail_closed() -> None:
    records = [r if r.pass_ == "single" or int(r.seed) < 7
               else dataclasses.replace(r, status="interrupted", claimed_at=None, resolution=None)
               for r in run(100.0, 100.0, 9, 9)]
    v = verdict(records)                         # 7 concurrent walls < min_tasks 8
    assert v.wall_ratio is None and not v.wall_ok

def test_missing_pass_counts_unresolved() -> None:
    v = verdict([rec("single", f"{i:02}", 100.0, True) for i in range(10)])
    assert v.rate_concurrent == 0.0 and not v.resolve_ok

def test_budget_first_limit_wins() -> None:
    b = Budget()
    assert b.exceeded(usd=0, calls=0, prs=0, elapsed=0, model_failures=0) is None
    assert b.exceeded(usd=1.0, calls=0, prs=0, elapsed=0, model_failures=0) == "budget_usd"
    assert b.exceeded(usd=0, calls=4000, prs=22, elapsed=0, model_failures=0) == "max_calls"
    assert b.exceeded(usd=0, calls=0, prs=22, elapsed=0, model_failures=0) == "max_prs"
    assert b.exceeded(usd=0, calls=0, prs=0, elapsed=6 * 3600, model_failures=0) == "max_hours"
    assert b.exceeded(usd=0, calls=0, prs=0, elapsed=0, model_failures=5) == "model_failures"

@pytest.mark.parametrize("raw,mb", [("123.4MiB / 512MiB", 124), ("1.5GiB / 4GiB", 1536),
                                    ("900KiB / 512MiB", 1), ("0B / 0B", 0)])
def test_parse_mem(raw: str, mb: int) -> None:
    assert parse_mem_mb(raw) == mb
```

  Plus:
  - `render_results` output contains the summary header row, 20 task rows and the strings `≤ 1.25` and `≤ 10`. It contains no `ghp_`/`gho_`/`github_pat_` or `sk-` prefixes even when an input record's error text holds a fake token (the report redacts token-shaped strings).
  - `build_check_tree` on a PR tree made from a copy of seed 01:
    - the agent's `pager.py` edit is in the check tree;
    - an edited `tests/test_pager.py` and a deleted `pytest.ini` → pristine in the check tree, both in `tests_touched`;
    - added `tests/test_extra.py`, `conftest.py`, `pytest.py`, `pytest.toml` and `sitecustomize.py` → absent from the check tree, all in `files_added`; the test file and `conftest.py` also in `tests_touched`;
    - a symlink in the PR tree → not copied, listed in `files_added`;
    - an existing `dest` → error.
  - `check_eval_repo`:
    - `ShreyaasAI/swf-pr-sandbox`, `shreyaasai/SWF-PR-SANDBOX` and `https://github.com/ShreyaasAI/swf-pr-sandbox` pass;
    - `ShreyaasAI/Fixable`, `ShreyaasAI/swf-ci` and `other/swf-pr-sandbox` → `ValueError`;
    - each of the `run`, `seed-branches`, `check` and `cleanup` CLI entry points with a disallowed `--repo` exits non-zero before any HTTP or git call (spies).
  - `cleanup_plan`: only this run's PRs. A pr_url for another repo, or not of the form `https://github.com/<repo>/pull/<n>`, is excluded.
  - `apply_cleanup` with a fake API: it skips a PR whose `head.ref` differs, whose head repo is a fork, or whose `html_url` differs. It closes and deletes only the matching ones.
  - Seed push: `push_seed_branch` refuses `agent/x`, `seed/../x`, `+seed/01-a` and `main`. `GitOps.push(path, "seed/01-a", ...)` still raises `GitOpsError` (backend guard unchanged).
  - Metrics: a replaced record (infra re-run) is not in the `verdict` inputs, and `render_results` lists it under replaced records. Guard flags are counted by category and listed.
  - Budget in the concurrent pass: two fake driver threads share one `Budget` counter. When the shared cost crosses the limit, neither thread creates another task.
  - `mint_jwt` decodes with the secret: `aud`, `role`, `iss`, `provider_id`.
  - `child_env` drops `SWF_SUPABASE_DB_URL` and any key not on the allowlist.
  - `load_seeds()` returns 10 seeds, each with `target_test` and `description` from `seed.yaml`.
- [ ] `test_m9_eval_seeds.py` (no network):
  - Set up a local bare repo and `GitOps(clone_base=<file url of tmp dir>, allow_file=True)`.
  - `publish_seed_branches` for 2 seeds → 2 branches `seed/<name>`. `git ls-tree -r` of each equals the seed `repo/` file list exactly (no `reference/`, no `seed.yaml`).
  - Re-publishing returns the same shas with no new push.
  - A branch pre-set to another commit → `SeedBranchConflict`, and the remote is unchanged.
  - repo `shreyaasai/FIXABLE` → `ValueError` with no git process started (runner spy).
  - A planted hook in the template never runs (m6 hook fixture pattern).
- [ ] `test_m9_eval_driver.py` (db):
  - Run the real app (`build_deps` with `fresh_db`) under `uvicorn` on `127.0.0.1:0` in a thread (SSE needs a real server). One user has a queued task.
  - A helper thread plays the worker with `store`: `approval_requested` (plan), then `approval_requested` (command), then `question_asked`, then the terminal status.
  - `EvalClient.drive` approves both approvals and answers the question with `EVAL_ANSWER`. It returns `DriveResult(status="completed", approvals={"plan": 1, "command": 1}, questions=1)`. The `approvals` rows are decided by the submitter, and the audit rows exist.
  - A drop of the SSE connection mid-stream resumes with `Last-Event-ID`, so no event is handled twice.
  - A deadline passed → the task is `cancelled`.
  - `create_task` when the user already has a non-terminal task → `CreateRefused(429, "quota_concurrent_tasks")` (the M8 detail string; the test-writer copies the exact code from `swf.api.app`).
- [ ] `test_m9_eval_resolve_docker.py` (marks `docker`, `network`): for seeds 01 and 07, `run_controls(seed, sandbox_runner(egress_config), tmp_path)` → pristine `target_exit != 0` and not resolved; reference resolved. A tree that "fixes" by deleting the target test → the pristine test is used, so not resolved, and `tests_touched` lists it. A PR tree with a planted `pytest.py` (exits 0 on import) and a `pytest.toml` → both ignored, so the unfixed tree still does not resolve, and both are in `files_added`.
- [ ] Run. Expected: `ModuleNotFoundError: swf_eval`.
- [ ] Commit `M9 T7: eval harness tests`.

### T8 (coder): `swf_eval` package

**Files:** create `src/swf_eval/{__init__,__main__,seeds,resolve,client,localstack,metrics,budget,report,cleanup}.py`; modify `pyproject.toml` (wheel packages), `src/swf/github.py` (`close_pull`, `delete_branch`), then run `uv sync`.

- [ ] Implement §1.5. `run` subcommand order:
  1. Check `docker info` (§0.20).
  2. `LocalStack` up; mint both users; link the `gh` token for both.
  3. `warm-cache`.
  4. Single pass: for each seed in order, user alternating A/B, create → drive → wait for terminal. After each task, check the budget.
  5. `warm-cache`.
  6. Concurrent pass: user A takes the odd seeds and user B the even seeds. Each user creates its next task when its previous one is terminal (two driver threads). Each thread checks the shared budget before each create and every 30 s while driving (§0.15). The `docker stats` sampler runs throughout.
  7. `records_from_db` → JSON.
  8. Token/key scan of the run dir.
  9. `LocalStack` down.

  `check` clones each completed PR head with `GitOps.clone(repo, "agent/<task-id>", ...)` and runs `check_resolution`. `report` writes §1.6. `cleanup` without `--apply` prints the plan only.
- [ ] `swf_eval` never imports `tests`. The shim path is the constant `REPO_ROOT / "tests/fixtures/supabase_shim.sql"`.
- [ ] Green: all T7 files, plus `tests/test_m6_no_merge.py`.
- [ ] Commit `M9 T8: eval harness`.

### T9 (ops): CI on feature/m9 as the runner user

**Files:** `.github/workflows/m8-gvisor.yml`, `ops/ci_check_junit.py`, `tests/test_m8_ops.py` (if it asserts the uid-1000 step).

- [ ] Add `feature/m9` to `on.push.branches`. Remove the step that creates or uses a uid-1000 user and chowns the checkout; pytest runs as the default runner user (uid 1001). Its primary gid is the `docker` group, which is harmless because sandboxes get no Docker socket. Keep runsc, the firewall rule, the gate and the leftovers check. Add `SWF_PKGCACHE_DIR: ${{ runner.temp }}/pkgcache`.
- [ ] Gate: also fail if `test_m9_workspace_owner` has no executed cases, or any `test_m9_pkgcache_docker` case is skipped.
- [ ] Red evidence: before T2 is merged into the CI branch, the owner test fails as uid 1001. Run it once on a commit with T1 only, if cheap; otherwise state that it was not captured.
- [ ] Push `feature/m9` to `ShreyaasAI/swf-ci` (never Fixable). Read the run with `gh run view`. Done when it shows 0 failed, `test_m8_gvisor` ran, `test_m9_workspace_owner` ran, and no leftovers.
- [ ] Commit `M9 T9: CI runs as runner uid; M9 gates`.

### T10 (test-writer + lead, live; §0.24): seed branches on GitHub

**Files:** create `tests/test_m9_eval_live.py` (marks `live`, `network`; skips without `SWF_TEST_REPO` or `gh`; fails if the repo is Fixable).

- [ ] Test: for each of the 10 seeds, `GET /repos/{repo}/branches/seed/<name>` → 200, and `AGENTS.md` at that ref equals the local seed file byte for byte.
- [ ] Red: run before publishing → 404 failures.
- [ ] Lead: `uv run python -m swf_eval seed-branches --repo ShreyaasAI/swf-pr-sandbox`, then the test → green.
- [ ] Commit `M9 T10: seed branches live check`.

### T11 (lead, live): controls, then the two passes

- [ ] Preconditions:
  - the user has OK'd this contract;
  - the user has noted Portkey usage;
  - Docker Desktop resources meet §0.20;
  - no `swf.managed` leftovers.
- [ ] `uv run python -m swf_eval controls`: 10/10 pristine fail and 10/10 reference resolve, else stop.
- [ ] `uv run --env-file <main checkout>/.env python -m swf_eval run --pass both --repo ShreyaasAI/swf-pr-sandbox --out .m9-eval/<run-id>`. Watch with a background until-loop on `run.json` `ended_at` (foreground sleep is blocked).
- [ ] An infra failure gets one re-run of that seed (`--pass <p> --seeds <nn>`), within the cap (§0.13). Record the cause.
- [ ] `uv run python -m swf_eval check --out .m9-eval/<run-id>`.
- [ ] Haiku tester: re-run the non-live suite after the run (no code changed) and report counts.

### T12 (lead + ops): results, cleanup, close-out

**Files:** `docs/software-factory/m9-results.md` (new, generated then annotated), this file (§5, Status), `docs/software-factory/implementation-plan.md` (§7, §16, §18 M9 checkbox, result line, decisions), `docs/software-factory/research-plan.md` (§8 cache wording, RQ3 and RQ9 results), `docs/software-factory/problem-specification.md` (§4 item 12, §12 Q3 host, §10 criteria ticks), `docs/HANDOVER.md`. Update each "Last updated".

- [ ] `uv run python -m swf_eval report --out .m9-eval/<run-id> --write docs/software-factory/m9-results.md`. The user adds the Portkey usage after the run.
- [ ] `uv run python -m swf_eval cleanup --out .m9-eval/<run-id>` (dry run, show the list), then `--apply`. Record the closed PRs and deleted branches in m9-results.md. `seed/*` branches stay until the user approves M9; then `cleanup --seeds --apply` deletes them (§0.24).
- [ ] Commit `M9: results and close-out`. Stop for user review. The push to Fixable happens only after the user approves M9.

## 4. Definition of done (evidence commands)

Local (Windows, Docker Desktop, runc):

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs -m "not live"        # 0 failed; owner test skipped (POSIX only)
PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs tests/test_m9_*.py -m "not live"
uv run ruff check && uv run ruff format --check && uv run mypy src
PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -rs -m live tests/test_m9_eval_live.py   # 10 seed branches
uv run python -m swf_eval controls                                                 # 10/10 + 10/10
uv run --env-file <main>/.env python -m swf_eval run --pass both --repo ShreyaasAI/swf-pr-sandbox --out .m9-eval/<id>
uv run python -m swf_eval check --out .m9-eval/<id>
uv run python -m swf_eval report --out .m9-eval/<id> --write docs/software-factory/m9-results.md
docker ps -a --filter label=swf.managed=1 --filter label=swf.eval=1 --format '{{.Names}}'   # empty
```

CI (`ShreyaasAI/swf-ci`, branch `feature/m9`, runsc, uid 1001): `gh run view <id>` shows 0 failed; the gate shows `test_m8_gvisor`, `test_m9_workspace_owner` and `test_m9_pkgcache_docker` ran.

| Criterion | Proved by |
|---|---|
| 10 seeded repos through the full pipeline | m9-results.md per-task table: 20 rows, each with status, and PR URLs for completed tasks |
| 2 concurrent tasks | concurrent pass claim/finish overlap (two tasks running at once, visible in the timestamps) |
| Wall time ≤ 1.25× single-task | summary row `wall_ratio` (§0.11) |
| Resolve-rate drop ≤ 10% | summary row `drop` (§0.10, §0.12), checker controls 10/10 |
| Results table | `docs/software-factory/m9-results.md` committed; raw JSON in `.m9-eval/<id>/` |
| 1-day pull-through cache, read-only to tasks | `test_m9_pkgcache_unit` (TTL, hash, 405), `test_m9_pkgcache_docker` (hit across tasks, no mount, no direct egress) |
| Linux workspace ownership | `test_m9_workspace_owner` on CI as uid 1001 |
| $0 and budget | `run.json` budget fields; Portkey usage before/after (user); no new infra |
| No secret leaked | runner token/key scan result in `run.json`; `scan_for`-style test in T7 |

If the wall or resolve checks fail, M9 still closes with the measured table and a stated finding. A re-run needs the user's OK (§0.13).

## 5. Results

To be filled in T12: run id, controls, summary table (copied from m9-results.md), CI run id, PR cleanup log, risks carried.

## 6. Deferred

- Public deploy and link → M10 (§0.23).
- Paid tasks (2 free per day, then $1 per extra task) → a new post-M10 payments milestone (§0.26, C7). It needs a payment provider and a spec change.
- Product default model → glm for all phases, only if the M9 DoD passes (§0.25).
- gVisor for the eval host; Arm coverage → the M10 host decision.
- Caching non-PyPI ecosystems (npm, crates): the seeds are Python only.
- Multi-trial evaluation and confidence intervals (cost and PR cap).
- Supabase keep-alive ping (plan §16) → with the public deploy.
- Guard latency budget and parallel judging (m7 §12); M9 only measures guard seconds per task.

## 13. Amendments

Amendments from the user override §0–§6.

- **A1 [A, T4 review, 2026-11-22] Index size cap.** §1.3 adds `MAX_INDEX_BYTES = 32 * 1024 * 1024`: an upstream index page over 32 MiB → 502, nothing stored. The page is buffered in memory before `rebuild_index`, so 3–4 concurrent cold misses on very large pages could approach the `--memory 256m` limit in §0.6. Suggestion for T5/T6 (needs approval, not applied): raise the cache container to `--memory 512m`.
- **A2 [A, T11 live run, 2026-11-23] `submit_plan.steps` is a string on the wire.** The first live run (`run-20261123a`, aborted after 2 tasks, 0 PRs) failed every task with `repeated_format_error`: glm-4.7-flash writes an array-of-strings argument as a bare numbered list (`"steps":1. Edit … 2. Run …,`), which is invalid JSON (offline probe: 5/5 on the task-2 history). With `steps` typed as a string it emits valid JSON 5/5. Fix: the `submit_plan` schema types `steps` as a string (1..4000 chars, "one per line"); `Plan` splits it into lines (stripped, blanks dropped, 1..20), so the approval payload and UI still get a list. `files` (short paths) parsed fine and stays an array. The run restarts from a fresh `--out`.
