# M10a contract: minimal web UI on localhost

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development. Tasks run in order, one implementer each. A spec review and a code-quality review follow each one. Steps use checkboxes (`- [ ]`).

- **Purpose:** The interface contract and ordered task list for M10a. If something here is wrong, report it. Do not silently diverge.
- **Last updated:** 2026-11-24
- **Status:** Draft v1. Awaiting user approval. Branch `feature/m10` (worktree `.claude/worktrees/m10`), on top of `main` `e60f9dd`.
- **Sources:**
  - plan §18 M10 (design, grill rounds 1-3, plus the M10-0 refactors);
  - spec §3 (user actions), §9, §11;
  - m2 §4 (pre-flight);
  - m4 (SSE, events);
  - m7 §13 A1 (Portkey saved guardrails);
  - m8 §0.7-§0.11, §1.6 (abuse controls);
  - m9 §0.9 (`LocalStack`).
- **Roles [D, user]:**
  - Opus = planner, coder, reviewer.
  - Sonnet = bash/ops, docs, test writer.
  - Haiku = tester (runs suites, reports counts and failures, edits nothing).
  - $0 infrastructure only.

**Goal:** A signed-in user does every spec §3 action in a browser on localhost:
- sign in;
- link GitHub;
- see quota;
- pick a repo;
- pre-flight;
- create a task;
- watch it live;
- approve or answer;
- cancel;
- see evidence and the PR;
- delete a finished task.

An admin can also pause, resume, kill and resume users. About 12 Playwright e2e tests prove this against the real API and worker, with only the model, GitHub and the PR publisher faked. One live smoke run with real Supabase sign-in opens a real PR on `swf-pr-sandbox`.

**Architecture:**
- **Backend:** small, tested additions on top of the six approved M10-0 refactors (§1.1-§1.7).
  - Four routes: `POST /v1/preflight`, `GET /v1/admin/status`, `DELETE /v1/tasks/{id}`, and `limits` / `remaining` in `GET /v1/me`.
  - The worker refuses to start without a Portkey guardrail config (§1.8).
- **Frontend:** `web/` holds a Vite + React + TypeScript single-page app.
  - Its only runtime dependency besides React is `@supabase/supabase-js`.
  - Hash routes, one CSS file of design tokens, no UI library.
  - On localhost, the Vite dev server proxies `/v1` to the API, so no CORS is needed.
- **E2E:** Python Playwright drives Chromium against `swf_eval.LocalStack`, with two test-only launchers:
  - `tests/e2e/fake_api.py`: the real app, with in-process fake GitHub and fake registry transports;
  - `tests/e2e/fake_worker.py`: the real worker, with a scripted model, a local-copy workspace and a fake publisher.
  - Real Postgres, real Docker sandboxes, real harness.

**Tech stack:**
- Python 3.14, FastAPI, psycopg, pytest.
- New dev dependency `playwright` (Python), with Chromium installed once via `uv run playwright install chromium`.
- Node 25 / npm 11.
- `web/` packages: `react`, `react-dom`, `@supabase/supabase-js`.
- `web/` dev packages: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom`.

---

## 0. Decisions, assumptions, open questions

### User decisions (2026-11-24, grill rounds 1-3 and M10-0)

1. [D, user] **Increments.** M10-0 (done), then M10a (this contract), then M10b (deploy readiness, own grill), then the user deploys `web/` to Vercel.
2. [D, user] **Hosting.** Vercel serves the static frontend only. The API and worker stay on the PC. The API base URL comes from env (`VITE_API_BASE`, empty on localhost, so requests go to the same origin and through the proxy).
3. [D, user] **Sign-in.**
   - Supabase GitHub OAuth with scope `repo`; the sign-in screen states the scope.
   - After each sign-in the browser re-POSTs `provider_token` to `/v1/me/github-token`.
   - The user configures the GitHub OAuth App and the Supabase provider from the runbook (§5).
4. [D, user] **UI scope.** Sign-in and quota, repo picker (base branch prefilled from `default_branch`), create with pre-flight, task list, and live timeline. Approval and question cards are rebuilt from the event replay. Evidence, PR link, and an admin page.
5. [D, user] **Out of M10a.** Branch dropdown, deploy or tunnel, gVisor on the PC, CORS allowlist, payments, quota editing, e2e in CI.
6. [D, user] **M10-0 refactors.** All six approved ("Approve all 6"). Rejected items stay rejected (plan §18 M10).
7. [D, user] **Portkey saved guardrails on by default** (plan §18 M10, added 2026-11-23).
8. [D, user] **UI design** via the `ui-ux-pro-max` skill (§3).

### Planner decisions

9. **Fake GitHub inside the e2e API process, not a fake server behind `SWF_GITHUB_API_URL`.**
   - `tests/e2e/fake_api.py` builds the real app through `build_app_deps(...)` (§1.7), passing `GitHubClient(transport=FakeGitHub(...).transport())` and `RegistryClient(transport=FakeRegistry(...).transport())` from `tests/helpers.py`.
   - Why: it reuses tested fakes, covers the registry too (otherwise pre-flight calls Docker Hub), and needs no extra port. The worker needs no GitHub: it uses a local-copy workspace and a fake publisher. This is conflict C1.
10. **Delete needs a released task.**
    - `DELETE` returns 409 `task_not_finished` for non-terminal tasks and 409 `task_busy` while `worker_id` is still set.
    - The worker clears `worker_id` and the lease when its runner exits (`store.release_task`). The reaper does the same when it interrupts.
    - Why: a cancelled task's runner may still write a few rows before it stops; deleting under it would make those inserts fail on the foreign key.
    - Never-claimed tasks (rejected, or cancelled while queued) can be deleted at once.
    - The workspace folder is removed by the worker's sweep (§1.5), at most one sweep interval (60 s) after the delete.
11. **Pre-flight dry run.**
    - `POST /v1/preflight` spends one `api` token only (not `create`).
    - It runs the same admission check as create (pause 503, quota 429), so the user sees a refusal before pressing Start.
    - It writes nothing and adds no audit row.
12. **Fonts.** System font stacks only. No web-font request: no third-party call, no dependency (§3).
13. **No JS test runner.** Frontend behaviour is proven by the Python e2e tests. `npm run typecheck` (`tsc --noEmit`) and `npm run build` are evidence steps.
14. **Dev sign-in.**
    - Only in builds with `VITE_AUTH_MODE=dev`. It takes a pasted JWT and a provider token.
    - Vite replaces `import.meta.env.VITE_AUTH_MODE` at build time, so a production build has no dev branch.
    - A test builds production and checks that the marker string `swf-dev-signin` is absent from `web/dist`.
15. **E2E marker.**
    - E2E tests carry `@pytest.mark.e2e`. They skip with a reason when Docker, npm or Chromium is missing.
    - CI excludes them (`-m "not live and not e2e"`) until M10b.
16. **Event list.**
    - `swf/events.py` lists the types; `PgTaskChannel.emit` and `store.append_event` reject unknown ones (`ValueError`).
    - A test collects every string-literal type passed to `emit(` / `append_event(` under `src/swf` and requires it to be in the list. This is the guard against a rare emit path crashing a task.
    - A second test requires `web/src/events.ts` to name the same types and the same noise subset.

### Conflicts between sources (reported before code)

- **C1.** The plan says "a fake GitHub API (`SWF_GITHUB_API_URL`)". This contract uses in-process transports (decision 9). The env var stays supported and unchanged.
- **C2.** The plan says "workspace removed" on delete. Here the removal is deferred, up to one sweep (60 s), after the row is deleted (decision 10). It stays worker-side, so the API never touches the workspace folder, which matters once the API runs on another host (M10b).

### Open questions for the user

- **Q1. Portkey "on by default" (§1.8).**
  - Proposal: the worker refuses to start unless `SWF_PORTKEY_CONFIG` is a `pc-…` slug or the literal `off` (logged as a warning).
  - You create the saved config in the Portkey dashboard (runbook §5.3): a regex output check for secrets, not in deny mode, so hook results come back and the existing fail-closed rule raises a `guard_flag` when they are missing.
  - The e2e worker and the scripted tests use `off`.
  - OK?

---

## 1. Interfaces (backend)

### 1.1 `swf.events` (new; R3)

```python
EVENT_TYPES: frozenset[str] = frozenset({
    "status", "message", "tool_call", "tool_result", "tool_refused", "retry", "setup",
    "repo_cloned", "phase_model", "phase_switch", "completed",
    "guard_check", "guard_flag", "output_guard", "command_verdict",
    "approval_requested", "approval_decided", "approval_expired",
    "question_asked", "question_answered", "question_expired",
    "evidence", "evidence_note", "evidence_check", "evidence_recorded",
    "baseline_not_reproduced", "backstop_snapshot", "backstop_rebaseline",
    "backstop_violation", "harness_discrepancy", "harness_rerun", "pr_opened",
})  # the T1 literal-scan test is authoritative; add any type it finds that is missing here
NOISE_TYPES: frozenset[str] = frozenset({
    "guard_check", "phase_model", "backstop_snapshot", "backstop_rebaseline",
    "command_verdict", "output_guard", "evidence_check", "retry",
})  # subset of EVENT_TYPES; hidden behind the UI toggle (counted, not dropped)

def check_type(type: str) -> str: ...  # returns type; ValueError("unknown event type: …") otherwise
```

- `store.append_event` calls `check_type` first, which also covers `PgTaskChannel.emit`.
- Payloads stay plain dicts. The UI renders known keys and shows anything else as a generic key/value row.
- Payload keys the UI relies on (already emitted today):
  - `status`: `from`, `to`, `phase`, `error`
  - `message`: `role`, `content`, `model?`, `phase?`
  - `tool_call`: `tool`, `args`, `tool_call_id`
  - `tool_result`: `tool`, `tool_call_id`, `returncode`, `output`, `truncated`
  - `approval_requested`: `approval_id`, `kind`, `payload`
  - `approval_decided`: `approval_id`, `decision`, `comment`
  - `approval_expired`: `approval_id`
  - `question_asked`: `question_id`, `question`, `source`
  - `question_answered` / `question_expired`: `question_id`
  - `pr_opened`: `url`, `number`, `branch`, `commit_sha`
  - `completed`: `summary`

### 1.2 Pre-flight helper and `POST /v1/preflight` (R1)

Inside `create_app` (closures, as today):

```python
def _github_token(user: CurrentUser) -> str:
    # 409 github_token_missing | 409 github_token_invalid (TokenDecryptError)
def _preflight(user: CurrentUser, ref: RepoRef, capacity: Capacity) -> PreflightResult:
    # deps.preflight.check(...), with today's exception mapping (unchanged codes):
    # RepoNotFound/BranchNotFound/RepoNotWritable -> 422; GitHubAuthError -> 409 github_token_invalid;
    # GitHubError -> 502 github_unavailable
```

- `/v1/repos` uses `_github_token`.
- `POST /v1/tasks` uses both. Its behaviour, codes and order stay unchanged; the existing tests must pass as they are.
- New body model `RepoRef(_Body)`: `repo` and `base_branch` with today's validators. `TaskCreate(RepoRef)` adds `description` and `failing_test`.
- `POST /v1/preflight`:
  - Body: `RepoRef`. Response 200: `PreflightResult`.
  - Order: auth (api token) → `_admission` in one backend transaction (503 `service_paused`, 429 `quota_*`) → token → `capacity_snapshot` → `_preflight`.
  - No row, no audit, no `create` token.

### 1.3 Quota remainder (R6)

In `store`:

```python
class Limits(BaseModel):     # mirror of AbuseLimits quota fields, for the API
    tasks_per_day: int; cost_per_day_usd: float; cost_per_task_usd: float; concurrent_tasks: int
class Remaining(BaseModel):
    tasks_today: int; cost_today_usd: float; concurrent_tasks: int   # each floored at 0

def remaining(usage: Usage, open_tasks: int, limits: AbuseLimits) -> Remaining: ...  # pure
def open_task_count(conn, user_id) -> int: ...   # non-terminal tasks of the user
```

- `check_quota` keeps its lock and return codes. It computes `remaining(...)` and returns the first exhausted quota, in today's order: tasks, then cost, then concurrent.
- A test asserts that `check_quota` refuses exactly when the matching `remaining` field is `<= 0` (property-style, over a small grid).
- `Me` gains `limits: store.Limits` and `remaining: store.Remaining`. `usage_today` stays.

### 1.4 Admin read model and `GET /v1/admin/status` (R4)

```python
class AdminUser(BaseModel):
    user_id: uuid.UUID; github_login: str | None; is_admin: bool; disabled: bool; open_tasks: int
class SystemStatus(BaseModel):
    tasks_paused: bool; updated_at: datetime | None; updated_by: uuid.UUID | None
    queued: int; active: int
    users: list[AdminUser]   # disabled users first, then by latest task; at most 100

def system_status(conn) -> SystemStatus: ...   # plain selects; no `for share`, no writes
def _scope_target(user_id: uuid.UUID | None) -> tuple[Literal["user", "global"], str]: ...
```

- `admin_kill` and `admin_resume` use `_scope_target`, with no behaviour change.
- `GET /v1/admin/status`: admins only (403 `admin_only`). Response: `SystemStatus`.

### 1.5 Task delete (R5)

```python
def delete_task(conn, user_id, task_id) -> None:
    # lock owned row (NotFound -> 404); non-terminal -> Conflict("task_not_finished");
    # worker_id is not null -> Conflict("task_busy"); delete (FK cascade: events, approvals,
    # questions, evidence); audit "task.delete" target=str(task_id) {"repo", "status"}.
    # quotas_usage is not changed (usage was spent).
def release_task(conn, task_id, worker_id) -> None:
    # set worker_id = null, lease_expires_at = null where id and worker_id match and status is terminal
def existing_task_ids(conn, ids: Collection[uuid.UUID]) -> set[uuid.UUID]: ...
```

- **`interrupt_orphans`** also sets `worker_id = null` and `lease_expires_at = null` on the rows it interrupts.
- **Worker release:** the runner calls `release_task` in its final `finally`, after the environment is released and the trajectory saved. It logs and swallows DB errors.
- **Workspace sweep:**
  - `worker.orphan_workspaces(names: Iterable[str], existing: set[uuid.UUID], running: set[uuid.UUID]) -> list[str]` is pure. It takes the names under `workspaces_root` that parse as UUIDs and returns those neither existing nor running. Any other name is ignored and never deleted.
  - `Worker.serve` runs the sweep at most every 60 s (`sweep_seconds` in `WorkerConfig`). It removes those folders with `shutil.rmtree` (errors logged), and only for paths whose resolved parent is `workspaces_root`.
- **Route:** `DELETE /v1/tasks/{task_id}` returns 204, or 404 `not_found`, 409 `task_not_finished`, 409 `task_busy`.

### 1.6 Worker wiring split (R2)

```python
def build_runner_deps(
    settings: Settings, db: Database, config: WorkerConfig, egress: EgressConfig, *,
    model_factory: Callable[[], AgentModel] | None = None,   # default PhaseModel.from_env
    workspaces: WorkspaceProvider | None = None,             # default from SWF_LOCAL_SOURCE / GitHub
    publisher: PullRequestPublisher | None | _Unset = UNSET,  # default as today
    guard_factory: Callable[[], Guardrails] | None = None,    # default Guardrails.from_env
) -> RunnerDeps: ...
def main() -> None: ...  # fail-fast checks (sandbox_user, portkey config, Guardrails/env,
                         # check_runtime, ensure_egress) stay in main, BEFORE build_runner_deps and serve
```

`tests/e2e/fake_worker.py` repeats the fail-fast checks it needs (`sandbox_user`, `ensure_egress`), then calls `build_runner_deps(...)` with fakes and `Worker(...).serve(stop)`.

### 1.7 API wiring split (for the e2e launcher)

```python
def build_app_deps(settings: Settings, db: Database, *, github: GitHubClient | None = None,
                   registry: RegistryClient | None = None) -> AppDeps: ...
def create_app_from_env() -> FastAPI: ...  # unchanged behaviour; uses build_app_deps
```

### 1.8 Portkey guardrails on by default

```python
def portkey_config_from_env(env: Mapping[str, str]) -> str | None:
    # "off" -> None; a value matching PORTKEY_CONFIG_RE -> it; missing/blank/other ->
    # SettingsError("set SWF_PORTKEY_CONFIG to a pc-... slug, or 'off'")
```

- `worker.main` calls it first and fails fast; on `off` it logs a warning.
- `PhaseModel` reads the config through it.
- Existing tests that build `PhaseModel` from env pass `SWF_PORTKEY_CONFIG=off` or a slug.
- `LocalStack` knobs default to `off` unless set. The M9 eval behaviour is unchanged and its reports note it.

---

## 2. Interfaces (frontend, `web/`)

```
web/
  package.json            scripts: dev, build, preview, typecheck
  vite.config.ts          proxy /v1 -> process.env.SWF_API_PROXY ?? "http://127.0.0.1:8000"
  index.html              lang="en", viewport meta, color-scheme
  .env.example            VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY, VITE_API_BASE, VITE_AUTH_MODE
  src/main.tsx  src/App.tsx (hash router)  src/styles.css (tokens + components)
  src/api.ts      fetch wrapper: base URL, bearer, JSON, ApiError{status, code, retryAfter}
  src/sse.ts      fetch-streaming SSE parser: id/event/data, ": ping", Last-Event-ID resume,
                  reconnect with backoff 1 s → 30 s, abort on unmount, stop on terminal status
  src/auth.ts     supabase session or dev token; onSignIn → POST /v1/me/github-token
  src/events.ts   EVENT_TYPES, NOISE_TYPES (mirror of swf.events; checked by a pytest)
  src/messages.ts API error code → user text (every code in §1 and app.py)
  src/screens/    SignIn, Dashboard, NewTask, TaskDetail, Admin
  src/components/ Timeline, EventRow, ApprovalCard, QuestionCard, EvidenceList, QuotaBar, ConfirmDialog
```

**Routes:**
- `#/signin`
- `#/` (dashboard: quota and task list)
- `#/new`
- `#/tasks/<id>`
- `#/admin`, linked only when `me.is_admin`. Non-admins who open it get the API's 403, shown as "Admins only".

**Behaviour rules:**
- **Signed out:** every route redirects to `#/signin`.
- **401:** the session is cleared and the app returns to sign-in.
- **429:** shows the `Retry-After` wait.
- **New task:** the form has repo (select from `/v1/repos`; only `can_push` repos enabled), base branch (prefilled from `default_branch`, editable), description, and failing test (optional).
  - "Check" calls `/v1/preflight` and shows decision, image, memory/CPU, time limit, queue position and warnings.
  - "Start" is enabled only after a non-reject pre-flight on the same repo and branch. Editing either invalidates the result.
- **Timeline:**
  - The timeline replays from seq 0, then streams.
  - Event rows are keyed by `seq` (no duplicates on resume).
  - Tool output longer than 40 lines is collapsed (`<details>`, first 40 shown). `returncode` is shown as a badge.
  - The noise toggle (default off) hides `NOISE_TYPES` and shows "N hidden".
  - The current status sits in one `aria-live="polite"` region; the log itself is not live.
- **Cards:**
  - A pending approval is an `approval_requested` with no later `approval_decided` / `approval_expired` for its id. The card has Approve / Reject buttons and an optional comment.
  - A pending question is a `question_asked` with no later answered/expired event. The card has an answer box.
  - Buttons disable while a request is in flight. On 409 the card shows "Already decided" and waits for the replay.
- **Actions:**
  - Cancel and Delete use a confirm dialog that names the task.
  - Delete is shown only for terminal tasks. On 409 `task_busy` it shows "Finishing up; try again in a moment".
- **Evidence and PR:** evidence comes from `/v1/tasks/{id}/evidence` (kind, source, exit code). The PR link comes from `task.pr_url` or `pr_opened`, and opens with `rel="noopener noreferrer"`.
- **Admin page:**
  - Shows the pause state with Pause all / Resume all, and the users table with Kill / Resume per row (own row disabled).
  - Kill asks for confirmation and lists the number of tasks it will cancel (`open_tasks`).

---

## 3. Design system (ui-ux-pro-max, 2026-11-24)

The skill was run with query "developer tool dashboard coding agent", variance 3, motion 2, density 8.

- **Style:** Minimalism / Swiss: grid-based, high contrast, functional, few shadows, clear type hierarchy.
- **Mode:** dark by default (`prefers-color-scheme` respected; light supported). The skill flags a light default as an anti-pattern for this product.
- **Tokens** (CSS custom properties; components use tokens only, never raw hex):

| Token | Dark (default) | Light |
|---|---|---|
| `--bg` | `#0F172A` | `#F8FAFC` |
| `--fg` | `#F8FAFC` | `#0F172A` |
| `--card` | `#1B2336` | `#FFFFFF` |
| `--muted` | `#272F42` | `#F2F3F4` |
| `--muted-fg` | `#94A3B8` | `#475569` |
| `--border` | `#475569` | `#E2E8F0` |
| `--primary` | `#1E293B` | `#334155` |
| `--accent` (Start, success) | `#22C55E`, text `#0F172A` | `#047857`, text `#FFFFFF` |
| `--danger` | `#EF4444`, text `#000000` | `#DC2626`, text `#FFFFFF` |
| `--warn` | `#F59E0B`, text `#0F172A` | `#B45309`, text `#FFFFFF` |
| `--ring` | `#FFFFFF` | `#0F172A` |

  The skill's light accent `#059669` is 3.8:1 on white, so the contract uses `#047857` (5.5:1) to meet 4.5:1.
- **Type:**
  - Body: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`, 16 px base, line-height 1.5.
  - Code, headings and timeline: `ui-monospace, "JetBrains Mono", "Cascadia Mono", Consolas, monospace`.
  - The skill pairs IBM Plex Sans with JetBrains Mono. We keep the system stacks, with no font download (decision 12).
  - No text under 12 px.
- **Spacing** (dense): 4 / 8 / 12 / 16 / 24 / 32 px. Max content width 1100 px. Single column under 768 px, with no horizontal page scroll; long output scrolls inside its own `<pre>`.
- **Motion:** 150-200 ms colour and opacity transitions only. `prefers-reduced-motion: reduce` turns them off. No animation library.
- **Accessibility (must):**
  - Contrast at least 4.5:1 for text.
  - A visible focus ring (`--ring`, 2 px, offset 2 px) on every interactive element.
  - Full keyboard use.
  - Semantic landmarks (`header`, `nav`, `main`).
  - A visible `<label>` for every field.
  - Errors appear next to the field and, after a failed submit, in a focused `role="alert"` summary.
  - Icon-only buttons have `aria-label`. Icons are inline SVG, never emoji.
  - Status is never conveyed by colour alone: badges carry text.
  - Hit targets at least 44 × 44 px for primary actions.
  - Confirm dialogs use `<dialog>` with focus moved in and returned on close.
  - `cursor: pointer` on clickables.
- **Pre-delivery check:** run at 375, 768, 1024 and 1440 px in both modes. Include the screenshots in the manual checklist (§4).

---

## 4. Tests and evidence

### 4.1 Backend (pytest, written first, in the default non-live suite)

| File | Proves |
|---|---|
| `tests/test_m10_events.py` | `check_type` accepts the list and rejects others; `append_event` refuses unknown types (no row); every literal emit type under `src/swf` is in `EVENT_TYPES`; `NOISE_TYPES ⊆ EVENT_TYPES`; `web/src/events.ts` matches both sets |
| `tests/test_m10_preflight_api.py` | dry run returns the estimate and writes no task and no audit row; same error codes as create (repo/branch/not-writable/token missing/invalid/github down); pause 503 and quota 429 before any GitHub call; spends no `create` token; `POST /v1/tasks` regressions unchanged |
| `tests/test_m10_quota.py` | `remaining` floors at 0; `check_quota` refuses exactly when `remaining` is exhausted (grid); `/v1/me` includes `limits` and `remaining` |
| `tests/test_m10_admin_status.py` | admin-only; pause state and who/when; counts; users ordering and limit 100; read-only (no audit row, no lock wait against an open `for update` on `system_state`) |
| `tests/test_m10_delete.py` | 204 then 404; cascade removes events/approvals/questions/evidence; audit kept; 409 non-terminal; 409 `task_busy` while `worker_id` set; RLS: another user's task → 404; `release_task` only for terminal and matching worker; reaper clears `worker_id`; `orphan_workspaces` (non-UUID names ignored, running kept) |
| `tests/test_m10_wiring.py` | `build_runner_deps` defaults equal today's `main` wiring; overrides are used; fail-fast order in `main` (portkey → sandbox user → guard → runtime) before any claim; `build_app_deps` with injected github/registry |
| `tests/test_m10_portkey.py` | `portkey_config_from_env`: `off` → None plus a warning; valid slug; missing/blank/bad → `SettingsError`; worker `main` exits before DB open on a missing value |
| `tests/test_m10_web_build.py` (`e2e`) | `npm ci && npm run typecheck && npm run build` pass; `dist/` lacks `swf-dev-signin`; a dev build contains it |

### 4.2 E2E (`tests/e2e/`, `@pytest.mark.e2e`, Playwright Chromium, written before the UI tasks)

Shared fixture `e2e_stack`:
1. `LocalStack(api_argv=[…fake_api.py], worker_argv=[…fake_worker.py], knobs={"SWF_PORTKEY_CONFIG": "off", …})`.
2. Start `npm run dev` with `VITE_AUTH_MODE=dev` and `SWF_API_PROXY=<api_url>` on a free port.
3. Make the "admin" user an admin via `db_url`.
4. Seed: one seeded repo, copied by `LocalCopyWorkspace`. The scripted model is chosen by a description tag: `[e2e:fix]` = the reference fix (m5 `reference_steps`), `[e2e:ask]` = asks one question then fixes, `[e2e:slow]` = waits for cancel.
5. The fake publisher returns `https://github.com/e2e/repo/pull/1`.

| # | Test | Proves |
|---|---|---|
| 1 | `test_sign_in_links_github_and_shows_quota` | dev sign-in → `POST github-token` → dashboard shows login and remaining quota |
| 2 | `test_repo_picker_prefills_default_branch` | only pushable repos enabled; branch prefilled |
| 3 | `test_preflight_shows_estimate_without_creating` | estimate shown; task list still empty |
| 4 | `test_preflight_error_is_inline` | not-writable repo → message by the field, alert summary focused |
| 5 | `test_task_runs_live_to_pr_with_evidence` | `[e2e:fix]`: plan approval card → approve → timeline to `completed`; evidence listed; PR link |
| 6 | `test_question_card_answers` | `[e2e:ask]`: question card → answer → run continues |
| 7 | `test_reload_resumes_without_duplicates` | reload mid-run: each seq rendered once, stream continues |
| 8 | `test_noise_toggle` | `guard_check`/`phase_model` hidden by default with a count; shown when toggled |
| 9 | `test_cancel_then_delete` | `[e2e:slow]` cancel via dialog → cancelled; delete → gone from list; workspace folder removed within one sweep |
| 10 | `test_long_output_collapsed` | tool output > 40 lines collapsed, expands on click |
| 11 | `test_admin_pause_resume_and_kill` | non-admin: no link, 403 text; admin pause → New task shows `service_paused`; resume; kill user → their task cancelled, they see `account_disabled`; resume user |
| 12 | `test_keyboard_only_create` | create and approve using the keyboard only; focus ring visible (computed style) |

### 4.3 Manual (DoD)

- **Checklist** `docs/software-factory/m10a-checklist.md`: every spec §3 action, with screenshots at 375 / 1440 px in dark and light mode, run against the Supabase Free project with real GitHub sign-in and the real worker (glm/ernie via Portkey, saved guardrail config on).
- **Live smoke:** one real task on `ShreyaasAI/swf-pr-sandbox` opens a PR. Its URL is recorded. Afterwards the PR is closed and the branch deleted.

### 4.4 Evidence at close

- Full non-live suite (`-m "not live and not e2e"`): counts.
- E2E: counts.
- `ruff`, `mypy --strict`.
- `npm run typecheck`, `npm run build`.
- Checklist and smoke PR URL.
- Secret scan of the new commits (no `.env` value; `web/.env.local` gitignored).

---

## 5. Runbook (user setup, written in T14 as `docs/software-factory/m10-runbook.md`)

1. **GitHub OAuth App:** callback `https://<project>.supabase.co/auth/v1/callback`. The client secret goes only into Supabase.
2. **Supabase:**
   - Auth → Providers → GitHub (client id and secret).
   - URL config: site URL `http://localhost:5173`, redirect allow-list `http://localhost:5173/**`.
   - Copy the project URL and the publishable/anon key into `web/.env.local`.
3. **Portkey:**
   - Create a guardrail with a regex output check for secrets: GitHub tokens `gh[pousr]_[A-Za-z0-9]{36,}`, `github_pat_…`, AWS `AKIA[0-9A-Z]{16}`, private-key headers.
   - Leave deny mode off, so responses return with `hook_results`.
   - Attach it to a config and put its `pc-…` slug in `.env` as `SWF_PORTKEY_CONFIG`.
4. **Run:**
   - `uv run uvicorn swf.api.main:create_app_from_env --factory --workers 1`
   - `uv run python -m swf.worker`
   - `cd web && npm ci && npm run dev`, then open `http://localhost:5173`.

---

## 6. Tasks (in order; each one: failing tests first → implementation → `ruff`/`mypy` → spec review → quality review)

- [ ] **T1 Events list (R3).** `swf/events.py`; `append_event` check; literal-scan test; `web/src/events.ts` parity test (the file is created here). §1.1.
- [ ] **T2 Pre-flight helper and route (R1).** `RepoRef`, `_github_token`, `_preflight`, `POST /v1/preflight`. §1.2.
- [ ] **T3 Quota remainder (R6).** `Limits`, `Remaining`, `remaining`, `open_task_count`; `check_quota` via `remaining`; `/v1/me`. §1.3.
- [ ] **T4 Admin status (R4).** `system_status`, `_scope_target`, `GET /v1/admin/status`. §1.4.
- [ ] **T5 Delete and sweep (R5).** `delete_task`, `release_task`, `existing_task_ids`, reaper change, runner release, `orphan_workspaces`, sweep, route. §1.5.
- [ ] **T6 Portkey on by default.** `portkey_config_from_env`; worker fail-fast; test env updates; LocalStack default `off`. §1.8. **Blocked on Q1.**
- [ ] **T7 Wiring splits (R2).** `build_runner_deps`, `build_app_deps`. §1.6, §1.7.
- [ ] **T8 E2E harness and failing e2e tests.** `playwright` dev dependency; `LocalStack` `api_argv`/`worker_argv`; `tests/e2e/{conftest.py,fake_api.py,fake_worker.py}`; tests §4.2, red against a stub `web/`; `pytest.ini` marker; CI `-m` filter.
- [ ] **T9 `web/` scaffold.** package and lockfile, Vite proxy, tokens CSS (§3), `api.ts`, `sse.ts`, `auth.ts` (supabase + dev), `messages.ts`, router; `test_m10_web_build.py` green.
- [ ] **T10 Sign-in, dashboard, new task.** e2e 1-4 green.
- [ ] **T11 Task detail.** timeline, SSE resume, cards, evidence, PR, cancel and delete; e2e 5-10, 12 green.
- [ ] **T12 Admin page.** e2e 11 green.
- [ ] **T13 Full suites.** Haiku runs the non-live and e2e suites and reports counts; fix failures.
- [ ] **T14 Docs.** Runbook (§5), checklist template, HANDOVER, plan §18 status; Sonnet writes, Opus reviews.
- [ ] **T15 Manual checklist and live smoke** with the user (needs their dashboard setup from §5). Then evidence, then stop for approval, then `finishing-a-development-branch`.

**Done when:** every row in §4.1 and §4.2 passes, the §4.3 checklist is complete with screenshots, the smoke PR URL is recorded, the §4.4 evidence is posted, and the user approves.
