# Web Coding Agent — Implementation Plan

- **Purpose:** The ordered, test-first build plan for the approved MVP. A new session should be able to pick up the next unchecked milestone from here.
- **Last updated:** 2026-11-23
- **Status:** Approved (user, 2026-11-19, together with M0). M0–M4 done; M4 approved (user, 2026-11-20); M5 and M6 approved, merged to `main` (`1235cdc`); M7 and M8 approved, merged to `main` (M8: `4f982a4`); M9 built and evaluated on `feature/m9`, wall-ratio miss accepted (user, 2026-11-23), awaiting final M9 approval
- **Related artifacts:** `docs/software-factory/problem-specification.md` (v2, Approved — scope source of truth) · `docs/software-factory/research-plan.md` (v2, Approved — rationale, options, RQs) · `PROMPT.md` (process rules)

Labels: **[D]** confirmed decision · **[A]** assumption · **[Q]** open question. Nothing here goes beyond the approved spec; roadmap items are listed only as non-goals.

**Working rules [D]:**
- Tests are written first in every milestone, then the infrastructure/implementation.
- One milestone increment at a time. Before it: what changes, what doesn't, how it's tested, what proves completion. After it: evidence, then stop for review.
- Zero infrastructure cost (free tiers only).
- **Languages [D, 2026-11-19]:** backend, agent, harness and all tooling in **Python only**. SQL for the database (migrations, RLS) and a separate language/framework for the frontend are allowed.
- Line endings: LF everywhere (`.gitattributes`).

---

## 1. MVP scope (summary)

Open beta, GitHub sign-in → pick a repo → describe a bug or failing test → pre-flight estimate → agent investigates (read-only) → approve the plan → agent fixes with approvals/questions → agent evidence + harness re-run → backend opens a PR on `agent/<task-id>` → a human reviews in GitHub.

## 2. Task flows (user stories)

| ID | Story | Acceptance |
|---|---|---|
| US1 | As a developer, I sign in with GitHub and see my repos | OAuth via Supabase; repo list from the GitHub API |
| US2 | I create a fix task for a repo/branch with a description and an optional failing test | Task row created; pre-flight result returned before any container starts |
| US3 | I watch the agent work live | SSE stream of events in order |
| US4 | I approve or reject the plan | Execute phase only after approval; rejection comment goes to the agent |
| US5 | I approve/reject flagged commands and answer questions | Blocked until I decide; audit row |
| US6 | I get a PR with evidence | PR on `agent/<task-id>` whose body contains the evidence summary |
| US7 | I am told up-front if my task is too big or the host is busy | Warning/queue position at creation |
| US8 | An admin can stop abuse | Quotas, rate limits, kill switch |

## 3. Components

| Component | Responsibility | Milestone |
|---|---|---|
| `swf.config` — AgentConfigLoader | Parse `AGENTS.md`: prose + ```` ```yaml agent-config ```` block → validated `AgentConfig` | M0 |
| `seeded_repos/` + eval fixtures | 10 seeded bug repos with failing tests and reference fixes | M0 |
| `swf.db` | Supabase/Postgres schema, migrations, RLS policies, repositories | M1 |
| `swf.api` (FastAPI) | REST + SSE, auth (Supabase JWT), quotas, rate limits | M1, M8 |
| `swf.sandbox` | `SandboxEnv(DockerEnvironment)`: hardened container, egress proxy network, gVisor, tool execution | M2, M8 |
| `swf.preflight` | Resource estimate + admission control | M2 |
| `swf.policy` | Bash command classifier + phase policy | M3 |
| `swf.agent` | `WebAgent(DefaultAgent)`, `PhaseModel`, tools, approvals, retries, events | M4 |
| `swf.evidence` | Evidence bundle, harness re-run, read-only backstop | M5 |
| `swf.github` | Clone, clean-clone apply, push, PR | M6 |
| `swf.guard` | Input (glm-4.7-flash judge + in-process regex for PII/secrets) and output (in-process guard; Portkey saved guardrail opt-in) | M7 |
| `swf.worker` | Queue claim, admission, task runner, restart reconciliation | M4–M5 |
| `web/` | Minimal UI | M10 |

## 4. Backend API (v1)

All endpoints require a Supabase JWT, except `/healthz`. Every mutating call writes an audit row.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness |
| GET | `/v1/me` | User, quota usage |
| POST | `/v1/me/github-token` | `{provider_token}` → 204. The browser sends the GitHub token Supabase returns at sign-in; the backend checks with GitHub that it belongs to the JWT's GitHub account, encrypts it (Fernet) and stores it. Never returned [D, 2026-11-19] |
| GET | `/v1/repos` | User's GitHub repos (via the OAuth token) |
| POST | `/v1/tasks` | Create task `{repo, base_branch, description, failing_test?}` → 201 `{task, preflight}`. Pre-flight runs first (GitHub repo, branch, `AGENTS.md`; image arm64 check; capacity). Unknown repo → 422 `repo_not_found`, unknown branch → 422 `branch_not_found`, with no task row. A rejected estimate is stored as status `rejected`. The result is stored in `tasks.preflight` [D, 2026-11-20] |
| GET | `/v1/tasks` / `/v1/tasks/{id}` | List / get |
| GET | `/v1/tasks/{id}/events` | SSE stream (`Last-Event-ID` resume) |
| POST | `/v1/tasks/{id}/approvals/{approval_id}` | `{decision: approve|reject, comment?}` — submitter only |
| POST | `/v1/tasks/{id}/answers/{question_id}` | `{answer}` — submitter only |
| POST | `/v1/tasks/{id}/cancel` | Cancel |
| DELETE | `/v1/tasks/{id}` | Delete task + workspace |
| GET | `/v1/tasks/{id}/evidence` | Evidence bundle |
| POST | `/v1/admin/kill` | Kill switch `{user_id?}` (admin) |
| POST | `/v1/admin/resume` | Undo a kill `{user_id?}` (admin; M8 A2) |

There is no endpoint that pushes, merges, or chooses a branch name.

**DB access model [D, 2026-11-19]:** the browser can call Supabase's REST API directly with its JWT, so `anon` gets nothing and `authenticated` gets SELECT on its own rows only (RLS); `github_tokens` has no grant and no policy. The API reads as `authenticated` (`SET LOCAL ROLE` + `request.jwt.claims`, so RLS applies) and writes with the backend role, taking `user_id` from the verified JWT and writing the audit row in the same transaction. Another user's row → 404. `audit_log` is append-only by trigger (UPDATE/DELETE/TRUNCATE refused for every role).
**JWT [D]:** ES256/RS256 via the project JWKS; HS256 only if the legacy secret is configured; `aud=authenticated`, `role=authenticated`, issuer `<project>/auth/v1`.
**[A]** On Supabase Free, the backend's `postgres` role may `SET ROLE authenticated` (Supabase grants it); verify when the cloud project is created in M8.

## 5. Agent orchestration

- **Phases:** `prepare → investigate → plan → execute → verify → confirm → pr`.
- **Model per phase [D, 2026-11-20]:** `baidu/ernie-5.5-flash-preview:free` for investigate/plan (Portkey wire `@baidu-ernie/baidu/ernie-5.5-flash-preview:free`; OpenRouter, often 429 upstream); `@glm-4-7-flash/glm-4.7-flash` (z-ai) for execute/verify. All calls via Portkey, through litellm `openai/<wire>`; no portkey-ai dependency.
- **Loop:** mini-swe-agent `DefaultAgent.run`. `WebAgent.execute_actions` handles each tool call in order: phase policy → classifier → guard flags → (approval wait) → execute → input guard on the observation → event.
- **Waiting:** an approval/question writes a row and blocks on Postgres LISTEN/poll; the answer re-enters as an observation via `InterruptAgentFlow`. Idle-approval timeout [D: 24 h, 2026-11-20] → `failed` with evidence so far.
- **Retries [D]:** a failing setup/test/infra step retries ≤ 3 times, then `ask_user`.
- **Limits:** `step_limit`, `cost_limit` (from quota), `wall_time_limit_seconds`.
- **Restart:** worker start marks orphaned `running` tasks `interrupted` and removes labelled containers.

## 6. mini-swe-agent integration boundary

| Reused unchanged | Subclassed/extended | Ours |
|---|---|---|
| `DefaultAgent` loop, limits, serialization | `WebAgent` (execute_actions, add_messages), `PhaseModel` (tools, model switch, Portkey config), `SandboxEnv` (run_args, tool dispatch) | API, DB, worker, policy, guard, evidence, GitHub, preflight |

The version is pinned in `pyproject.toml`; subclass contract tests fail loudly on upstream changes.

## 7. Execution environment and sandboxing

- **Host [D]:** Oracle Always Free Arm VM (2 OCPU / 12 GB, arm64), Docker + gVisor (`runsc`).
- **Per task:** 4 GB / 1 OCPU, `--pids-limit 512`, `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root, `--runtime runsc`, workspace bind-mounted at `/repo`, no Docker socket.
- **Network:** internal network → egress proxy (domain allowlist of package registries) plus a 1-day pull-through package cache (own container, read-only to tasks; M9, m9-contract §0.6).
- **Images:** from `AGENTS.md`; pre-flight rejects images without an arm64 variant.

## 8. Repository projection and isolation

1. The backend clones with the OAuth token via askpass into `workspaces/<task-id>/repo` (the token is never in `.git/config`).
2. The workspace is mounted into the task container only.
3. Diff export: `git -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --binary <base>`.
4. A clean clone in `workspaces/<task-id>/pr` → `git apply` → commit (hooks off) → push `agent/<task-id>` → open the PR via the API.
5. Workspaces are kept until the user deletes the task [D].

## 9. Data and state model (Postgres on Supabase, RLS on all user tables)

| Table | Key fields |
|---|---|
| `profiles` | `user_id`, `github_login`, `is_admin`, `disabled` |
| `github_tokens` | `user_id`, `encrypted_token`, `scopes` (service role only) |
| `tasks` | `id`, `user_id`, `repo`, `base_branch`, `description`, `failing_test`, `status`, `phase`, `reservation_mb`, `reservation_cpu`, `cost_usd`, `pr_url`, timestamps |
| `events` | `id` (bigserial), `task_id`, `seq`, `type`, `payload jsonb`, `created_at` |
| `approvals` | `id`, `task_id`, `kind` (plan / command / guard_flag), `payload`, `status`, `decided_by`, `decided_at`, `comment` |
| `questions` | `id`, `task_id`, `question`, `answer`, `answered_at` |
| `evidence` | `task_id`, `kind` (baseline / post / suite / harness_rerun / diff), `command`, `exit_code`, `output_ref`, `duration_ms` |
| `audit_log` | `id`, `user_id`, `action`, `target`, `payload`, `created_at` (append-only) |
| `quotas_usage` | `user_id`, `day`, `tasks`, `cost_usd` |
| `system_state` | singleton: `tasks_paused`, `updated_at`, `updated_by` (backend only; M8) |

**Statuses:** `created, queued, preparing, investigating, awaiting_plan_approval, executing, awaiting_command_approval, awaiting_input, verifying, confirming, opening_pr, completed, rejected, failed, interrupted, cancelled`.

**500 MB free-tier cap:** large command outputs go to files in the workspace, and `events` stores truncated text plus `output_ref`. Retention: events of deleted tasks are removed.

## 10. Task session trajectory

The mini-swe-agent trajectory JSON is saved at `workspaces/<task-id>/trajectory.json`. Every message also becomes an `events` row, giving the live UI and an audit view. The trajectory is the replay source; it is not used for automatic resume (non-goal).

## 11. Approval and artifact model

- **Approval kinds:** `plan`, `command` (classifier), `guard_flag` (next command after a flag).
- Only the submitter can decide. Each decision → `approvals` + `audit_log`.
- **Artifacts:** diff, evidence outputs, plan (JSON schema), trajectory, PR URL.

## 12. Tools and permission model

| Tool | Runs in | Phases | Gate |
|---|---|---|---|
| `bash` | container | all | classifier: allow / approve / forbid |
| `read_file`, `list_dir`, `search` | container | all | none |
| `edit_file`, `write_file` | container | execute | blocked in investigate |
| `run_tests` | container | all | none (`AGENTS.md` test command) |
| `submit_plan` | backend | investigate | user approval |
| `ask_user` | backend | all | user answer |
| `record_evidence` | backend | verify | none |
| `complete_task` | backend | verify | refused without a complete evidence bundle |

**Always forbidden:** `rm -rf` (any flag order), DB drops, git push/remote changes, network tools outside the proxy, `sudo`, Docker. `AGENTS.md` `allow` cannot override these.

## 13. Verification and evidence

- **Bundle (required for `complete_task`):** baseline failing run of the target test; post-fix passing run; `AGENTS.md` suite run with no new failures vs baseline; diff.
- **Harness confirmation:** re-run the target test once in the container; a mismatch → `failed`, with the discrepancy shown.
- **Read-only backstop:** workspace snapshot at investigate start vs at `submit_plan`.

## 14. Testing strategy

| Layer | Approach |
|---|---|
| Unit | pytest; pure modules (config loader, classifier, preflight, evidence rules) |
| Agent | Deterministic **fake model** scripts drive `WebAgent` through every state transition |
| Sandbox | Docker-marked integration tests (skipped when Docker is unavailable, and the skip is reported) |
| DB/RLS | Local Postgres (Supabase CLI or a Docker image) with policy tests |
| Security | Adversarial command corpus; seeded injection/PII corpus; token-leak checks |
| E2E/eval | The 10 seeded repos via the full pipeline; live models only in marked tests |
| Static | ruff (lint + format check), mypy (strict on `src/`) |

## 15. Security guardrails (checklist)

Token backend-only + encrypted · clean-clone PR, hooks off · branch computed server-side · no merge API · RLS on every user table · gVisor + hardening · egress allowlist · classifier ceiling · read-only backstop · input/output guards with approval on flag · quotas, rate limits, kill switch · audit log.

## 16. Rollout

1. Local dev (Windows + Docker Desktop) through M7.
2. Supabase Free project from M8. M9 evaluation ran on the local PC (2026-11-23); the public-deploy host is still open (M10) and must be $0 with no credit card (Oracle dropped 2026-11-22).
3. Open beta after M10 with quotas on. A free-tier keep-alive (scheduled health ping) prevents Supabase pausing.

## 17. Non-goals (MVP)

Web IDE · cloud memory/preferences · plans/billing/uploads · voice · non-bugfix tasks · GitLab/Bitbucket · merging/deploy · multi-agent · auto-resume · paid infrastructure.

## 18. Milestones (each starts with failing tests)

### M0 — Skeleton, AGENTS.md loader, seeded repos
- [x] `pyproject.toml` (uv), `src/swf/`, ruff + mypy + pytest configured
- [x] Tests: `AGENTS.md` loader (valid, missing block, invalid YAML, schema errors, prose extraction)
- [x] `swf.config` loader implemented
- [x] Tests: each of the 10 seeded repos has a valid `AGENTS.md`, its target test fails at baseline, and passes with no other failures after applying the reference fix
- [x] 10 seeded Python repos created (`seeded_repos/<name>/{repo,reference,seed.yaml}`)
- **DoD:** `uv run pytest`, `uv run ruff check`, `uv run mypy src` all pass; output shown. **Status 2026-11-19: done, awaiting review** (39 passed; ruff clean; mypy strict clean). Seeded repos are Python-only [D]. Also verified inside `python:3.12-slim` (x86_64) on 2026-11-19: 10/10 seeds fail at baseline with exit code 1 and pass after the reference fix.

### M1 — Data model, API skeleton, auth, audit
- [x] Tests: RLS cross-user denial; task create/list/get; submitter-only approvals; audit rows; quota counters
- [x] Migrations + RLS policies; FastAPI app; Supabase JWT verification; GitHub repo listing (mocked in tests)
- **DoD:** API + RLS tests green against local Postgres. **Status 2026-11-19: done, awaiting review** (149 passed against `postgres:17-alpine` + `tests/fixtures/supabase_shim.sql`; ruff, ruff format, mypy strict clean). Mutation check: dropping the table revokes, opening the RLS policies, or dropping the write ownership check each turns tests red.
- Files: `supabase/migrations/20261119000000_m1_init.sql`; `swf.{db,store,auth,tokens,github,settings}`, `swf.api.{app,main}`.
- Deferred by design: `preflight` is `null` until M2; SSE `/events` → M4; `/evidence` → M5; `DELETE /v1/tasks/{id}` + workspace → M10; the repo/branch access check at task creation → M2 (moved from M6 [D, 2026-11-20]); quota *enforcement*, rate limits, kill switch → M8 (M1 only counts). The Supabase Free project is created in M8 (user, 2026-11-19); M1 runs on local Postgres only.

### M2 — Sandbox, egress proxy, pre-flight
- [x] Tests: allowlisted registry reachable, other hosts blocked; token absent from the container; memory/CPU/pids limits; orphan cleanup; arm64 image check; over-threshold warning before start; queueing at capacity
- [x] `SandboxEnv`, proxy config, `swf.preflight`
- **DoD:** Docker-marked tests green locally. **Status 2026-11-20: done, awaiting review** (405 passed, 1 skipped (Windows-only path case), incl. all docker/network tests and the 10 seeds set up and verified inside the sandbox; ruff, ruff format, mypy strict clean; 0 Docker leftovers). Built by 4 parallel agents per the user; the contract was amended (v2 §9: per-task networks, execution-time image/limit checks, queued tasks count in admission). Checker mutation run: 9/9 security mutations caught. Process deviation (user's instruction): tests were written alongside the code from the contract, not seen red first; the two post-review fixes (deterministic task order via `tasks.created_seq`; `render_squid_conf` re-validates domains) were red-first.
- Interface contract for the parallel build: `docs/software-factory/m2-contract.md`.
- Decisions [D, 2026-11-20] (user): egress proxy = Squid, image `ubuntu/squid:6.6-24.04_edge` (multi-arch incl. arm64); only `squid.conf` is ours, `cache deny all`. The 1-day package cache is deferred to M9; M2 is allowlist only. An estimate above the per-task limit is rejected at creation: the task row is stored as `rejected` before any container starts, and it does not count toward the daily task quota (this is how M2 meets "over-threshold warning before start"). 1 vCPU per task (`--cpus 1`), 4 GB. Host capacity for admission: 2 concurrent tasks, 8192 MB, 2.0 CPUs (the 12 GB host keeps 4 GB for OS/API/worker/proxy). Registry allowlist as the SSRF guard, because `image:` comes from the user's repo: `registry-1.docker.io` (`docker.io`/`index.docker.io` normalise to it), `ghcr.io`, `quay.io`, `public.ecr.aws`, `mcr.microsoft.com`, `registry.gitlab.com`. Sandboxes get no external DNS (`--dns 127.0.0.1`) and reach the proxy by its IP on the internal network; Squid resolves names. Pre-flight runs inside `POST /v1/tasks` through the GitHub API (repo, branch, `AGENTS.md`), so the repo/branch access check moves from M6 to M2. Seeds' `setup` installs `pytest==9.2.0` through the proxy. Test runs set `PYTHONDONTWRITEBYTECODE=1` on the host and in the container.
- Files: `src/swf/images.py` (new: image-ref parser, registry allowlist, anonymous manifest/platform lookup) · `src/swf/preflight.py` (new: `Limits`, pure `evaluate`, `PreflightService` over GitHub + registry + capacity) · `src/swf/sandbox.py` (new: `SandboxSpec`, hardened `docker run` args, `SandboxEnv(DockerEnvironment)` with synchronous cleanup, label-based `cleanup_orphans`) · `src/swf/egress.py` (new: `squid.conf` renderer, `ensure_egress`/`teardown_egress` for the internal network + proxy container) · `src/swf/github.py` (`GitHubNotFound`, `RepoMeta`, `get_repo`, `branch_exists`, `get_file`) · `src/swf/store.py` (`ACTIVE_STATUSES`, `capacity_snapshot`, `Task.preflight`, `create_task` with status/reservations/preflight; no quota count for `rejected`) · `src/swf/api/app.py` (`AppDeps.preflight`; pre-flight and error mapping in `POST /v1/tasks`) · `src/swf/api/main.py` (wires `RegistryClient` + `PreflightService`) · `supabase/migrations/20261120000000_m2_preflight.sql` (new: `tasks.preflight jsonb`) · `seeded_repos/*/repo/AGENTS.md` ×10 (`setup` installs pytest via the proxy) · `pyproject.toml` + `uv.lock` (pin `mini-swe-agent==2.4.6`; pytest markers `docker`, `network`) · `tests/conftest.py`, `tests/helpers.py` (template-DB clones `db`/`fresh_db`, `FakeGitHub`, `FakeRegistry`, session `egress` fixture) · `tests/test_images.py`, `test_preflight.py`, `test_sandbox_unit.py`, `test_sandbox_docker.py`, `test_seeded_repos_container.py`, `test_store_capacity.py` (new) · `tests/test_api.py`, `test_tokens_github.py` (pre-flight integration, GitHub additions) · `docs/software-factory/m2-contract.md` (new).
- Deferred: gVisor `--runtime runsc` → M8 (the `runtime` field exists, unset locally); 1-day package cache → M9; worker claim and admission with `SKIP LOCKED` → M4 (the M2 decision is advisory and is not a reservation); Linux workspace ownership (chown for uid 1000) → M9 (Docker Desktop bind mounts are writable by any uid).
- Open risks → later milestones: importing mini-swe-agent loads `%LOCALAPPDATA%/mini-swe-agent/.env` into the backend (set `MSWEA_GLOBAL_CONFIG_DIR` to an empty dir and `MSWEA_SILENT_STARTUP=1` in the worker, M4); admission is not serialised (authoritative claim in M4); proxy sits on Docker's default bridge and a recreated proxy may get a new task-network IP (M8 hardening on the VM); `squid.conf` carries `umask 022` so the entrypoint can tail logs with all caps dropped. **Resolved in M4 (2026-11-20):** `MSWEA_GLOBAL_CONFIG_DIR` and `MSWEA_SILENT_STARTUP` set in the worker; serialised admission replaced by an advisory lock + `FOR UPDATE SKIP LOCKED` in `claim_next_task`.

### M3 — Command classifier + phase policy
- [x] Adversarial corpus tests (chaining, subshells, `eval`, `python -c`, heredocs, `rm -rf` variants, DB drops)
- [x] `swf.policy`
- **DoD:** 0 forbidden bypasses on the corpus; unparseable → approval. **Status 2026-11-20: done, awaiting review** (user: proceed without approval, review after M3). Corpus `tests/corpus/policy_commands.yaml`: 260 commands (185 forbid, 42 approve, 33 allow), **0 forbidden bypasses**; full suite 692 passed, 1 skipped; ruff, ruff format, mypy strict clean. A second bypass round found 13 real misses after the first green run (e.g. `cat < /dev/tcp/…` was `allow`, `bash -o errexit -c …`, `rm -{r,f}`, `env -S`, `git config --file … core.hooksPath`); all added to the corpus red-first and fixed. Mutation check on the classifier: 8/8 caught.
- Decisions [D, 2026-11-20]: parser = tree-sitter-bash (MIT; pinned `tree-sitter==0.26.1`, `tree-sitter-bash==0.25.1`), not bashlex (GPL). Every simple command anywhere in the tree is checked; nested scripts (`bash -c`, `eval`, heredocs into shells, `find -exec`, `xargs`, `env -S`, wrappers) are analysed recursively; literal resolution covers quotes, escapes, `$'..'`, concatenation and variables assigned to literals in the same script; anything dynamic is never `allow`. Read-only phases = investigate, plan (writers → forbid). Forbidden beyond §12's list: `gh`, `/dev/tcp|udp`, container-runtime sockets, credential paths, proxy-variable changes, dangerous `git config` keys (remote/url/hooksPath/sshCommand/credential/alias/…). `rm -r` without force → approval (spec forbids `rm -rf` any form). Tool gate `check_tool(tool, phase)` implements the §12 table.
- Files: `src/swf/policy.py` (new), `tests/test_policy.py`, `tests/corpus/policy_commands.yaml`, `pyproject.toml` (tree-sitter deps).
- Known limits: runtime behaviour of programs (a Python script can still delete files) is invisible; the sandbox is the boundary. `python -c`/`node -e` are approval, with only a SQL-drop scan inside.

### M4 — WebAgent, tools, approvals, model switch, worker
- [x] Fake-model tests for every status transition, retries ≤ 3 → ask, idle-approval timeout, restart → interrupted
- [x] `WebAgent`, `PhaseModel`, tools, worker queue (`SKIP LOCKED`), events
- [x] Live smoke test: one 2-model run via Portkey (RQ1/RQ2)
- **DoD:** transition tests green; the smoke-run trajectory is attached. **Status 2026-11-20: done, approved (user, 2026-11-20)** (M4 tests 415 passed: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -m "not live" tests/test_m4_*.py`, 123 s; full suite earlier 1105 passed, 1 skipped, 1 failed = M2 seed `10-clamp-upper-bound` pip ReadTimeout from files.pythonhosted.org, passes alone; ruff, ruff format, mypy strict clean; 0 Docker leftovers). Live smoke passed on the first attempt: exit Submitted; ernie 4 tool-call responses + glm 5; cost $0 for all 9 calls; phases investigate → plan approval → executing → verifying (auto switch) → complete_task. Trajectory `.m4-smoke/trajectory.json` (gitignored) contains no key. Built by 3 agents per the user: Opus planner (contract), Opus coder, Sonnet tester.
- Contract: `docs/software-factory/m4-contract.md` (§12 amendments override §0–§11).
- Decisions [D, user 2026-11-20]:
  - Idle-approval/question timeout 24 h (resolves §19 Q1).
  - No `start_verify` tool: the harness switches execute → verify on a passing `run_tests` covering the target after ≥ 1 edit since plan approval.
  - Multi-worker: leases (`lease_expires_at`, `heartbeat_at`; 90 s lease, 30 s heartbeat); worker id from `SWF_WORKER_ID` or hostname; cleanup limited to own-labelled (`swf.worker`) or reaped tasks.
  - Models changed: investigate/plan `baidu/ernie-5.5-flash-preview:free`; execute/verify `@glm-4-7-flash/glm-4.7-flash`; called through litellm `openai/<wire>` to Portkey's gateway; no portkey-ai dependency (§5).
- Files: new `src/swf/{_mswea,states,worker}.py`, `src/swf/agent/{__init__,tools,model,channel,web_agent,prompts}.py`, `supabase/migrations/20261121000000_m4_agent.sql`; modified `src/swf/{__init__,store,sandbox,egress,preflight}.py`, `src/swf/api/app.py` (SSE `GET /v1/tasks/{id}/events`); tests `tests/m4_fakes.py`, `tests/test_m4_{states,mswea,tools,model,agent,store,worker,sse,sandbox_tools,live_smoke}.py`; `.gitignore` (+`.m4-smoke/`, `.env`, `.env.*`); pyproject `live` marker.
- Security fixes: litellm's import-time `load_dotenv` loaded the main checkout's `.env` (Portkey + Supabase secret) into every backend process. `_mswea.isolate()` sets `LITELLM_MODE=PRODUCTION`, `MSWEA_GLOBAL_CONFIG_DIR` (empty dir), `MSWEA_SILENT_STARTUP=1`, `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1`; regression test added. `SandboxEnv._check_finished` is a no-op, so the agent cannot forge submission via `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`. `.env` was not gitignored → added.
- Deferred: `record_evidence` stub + accept-all `EvidenceGate` → M5; production `WorkspaceProvider` (clone) → M6 (`swf.worker.main` needs `SWF_LOCAL_SOURCE` until then). Known: importing `swf.sandbox` loads litellm (~8 s).

### M5 — Evidence + harness re-run + backstop
- [x] Tests: `complete_task` refused without the bundle; harness mismatch → failed; backstop catches a write in investigate
- [x] Test runs set `PYTHONDONTWRITEBYTECODE=1` (or clear caches): in M0 a same-size edit within the same second reused a stale `.pyc` and produced a false failure
- **DoD:** tests green on 2 seeded repos end-to-end (fake model). **Status 2026-11-21: implemented, awaiting user review.** Evidence: see M5 close-out.
- Contract: `docs/software-factory/m5-contract.md` (§12 amendments override §0–§8).
- Decisions [D, user 2026-11-20]:
  - The harness runs the baseline (target + full suite) at the end of `preparing`; a passing baseline fails the task (`baseline_not_reproduced`).
  - Backstop: a write in investigate asks for approval (`readonly_violation`) before plan approval. Rejected → `failed("readonly_violation")`. Approved → rebaseline (new baseline rows and start snapshot), then the normal plan path. Timeout (24 h) → `failed("approval_timeout")`.
  - Post/suite evidence comes only from the agent's real `run_tests` results in execute/verify, pinned to a workspace digest. No `failing_test` → the whole suite is the target.
  - Secure gate: completion is refused unless a harness (or an explicit `AcceptAllEvidence`) is wired. M4 tests opt out explicitly.
  - Diff = host-side copy at investigate start + `difflib` (M5 workspaces are not git repos).
- Files: new `src/swf/evidence.py` (snapshots, difflib diffs, pytest failure parsing, `harness_command`, `TaskHarness`); modified `src/swf/{store,worker}.py`, `src/swf/agent/{__init__,channel,prompts,web_agent}.py`, `src/swf/api/app.py` (`GET /v1/tasks/{id}/evidence`); migration `supabase/migrations/20261122000000_m5_evidence.sql` (`baseline_suite`/`post` kinds, `readonly_violation` approval kind); tests `tests/m5_fakes.py`, `tests/test_m5_{agent,e2e,harness,rules,snapshot,store_api,worker}.py`; M4 tests opted out of the gate (`tests/m4_fakes.py`, `tests/test_m4_worker.py`, `tests/test_m4_live_smoke.py`).
- Exits added: `ReadOnlyViolation`, `BaselineNotReproduced`, `NoTestCommand`.
- Fixes: approval row is created only after the `awaiting_*` transition (fixes an M4 flake); the post suite run passes when only baseline failures remain and the target is not among them.
- Deferred: production `WorkspaceProvider` (clone) and PR → M6.

### M6 — GitHub clone, clean-clone apply, PR
- [x] Tests: hooks never run (planted hook fixture); branch fixed server-side; no token in the workspace; PR created on a test repo (marked live test)
- [x] Live PR opened on the throwaway repo and the link recorded
- **DoD:** met. PR https://github.com/ShreyaasAI/swf-pr-sandbox/pull/1 (branch `agent/<task-id>`, open, not merged). **Status 2026-11-21: implemented, awaiting user review.** Evidence: see M6 close-out.
- Contract: `docs/software-factory/m6-contract.md`.
- Decisions [D, user 2026-11-21]:
  - Live test token comes from `gh auth token`; never stored or printed.
  - Throwaway repo `ShreyaasAI/swf-pr-sandbox` (env `SWF_TEST_REPO`), seeded with one commit.
  - Token at rest: Fernet with key `SWF_TOKEN_ENCRYPTION_KEY` (existing M1 name).
  - No push to `ShreyaasAI/Fixable` before M9.
- Built: `swf.gitops` (clone, apply, commit, push). Git env is built from scratch: `core.hooksPath=/dev/null`, fsmonitor off, credential helper reset, askpass reads `SWF_GIT_TOKEN`. Branch is always `agent/<uuid>`; no force push. `swf.pr` (`GitHubWorkspace` clones before the sandbox; `GitHubPublisher` makes a clean clone into `pr/`, then `git apply`, commit, push, PR). `github.create_pull`; no merge call anywhere. PR body holds commands, exit codes, and failure ids only.
- Evidence: binary, symlink, or `.git` paths → `pr_failed: unsupported_change`. M5 backstop rebaselines are excluded from the PR.
- Store: `record_pr` (sets `pr_url`, event `pr_opened`, audit) and `get_task_github_token`. Worker: `confirming` → `opening_pr` → `completed`. Failures: `clone_failed`, `pr_failed:<stage>`, `github_token_missing`, `github_token_invalid`. No migration.
- Deferred: `DELETE /v1/tasks/{id}` → M10; Linux workspace chown → M9; `can_push` pre-check → M8.

### M7 — Guardrails
- [x] Tests: seeded injection/PII/toxic/malicious corpus flagged; flag → approval required for the next command; malformed output caught by the output guard (Portkey or in-process fallback)
- **DoD:** corpus results table with false-positive rate on the clean corpus.
- **Result (2026-11-21, `m7-check`, awaiting review):** non-live 238 passed; live 3 passed; ruff/format/mypy clean. Live corpus (102 items): injection 20/22 (91%), toxicity 12/12, malicious 14/14, pii 12/12, secret 10/10; clean FP 0/32 (0%); guard_error 1; latency p50 6.2 s, p95 50 s, max 194 s per judged text. Open risk: latency misses research-plan A3 target (≤500 ms). Amendments: `m7-contract.md` §13.

### M8 — Abuse controls + gVisor
- [x] Tests: over-quota refused pre-start; 429 rate limiting; kill switch stops tasks ≤ 30 s; sandbox suite passes under `runsc`
- **DoD:** tests green on the Oracle VM. **Changed [D, 2026-11-22, user]:** the Oracle VM needs a credit card, so the DoD ran on GitHub Actions (private repo `ShreyaasAI/swf-ci`, x86_64, runsc). Arm is untested.
- **Result (2026-11-22, awaiting review):** CI run 44985961092: 1774 passed, 10 skipped, 0 failed (7m43s). Host isolation red before the firewall rule, green after. CI run 3 44987517565 green: 1774 passed, 0 failed (after a gVisor pids-limit test fix, `m8-contract.md` §5). Local: 1778 passed, 0 failed. Kill measured 2.3 s / 1.9 s locally (limit 30 s). Supabase Free project "Fixable" (`yztwunzllretrgsrauuf`): 5 migrations applied, 9 Supabase tests passed. Quota values remain the spec defaults [Q]. Adds `POST /v1/admin/resume`, table `system_state`, setting `SWF_SANDBOX_RUNTIME`. Carried risks and amendments: `m8-contract.md` §5, §13.

### M9 — Concurrency + evaluation (local PC)
- [x] 10 seeded repos through the full pipeline; 2 concurrent tasks. **Ticked with a note: the wall-ratio DoD was missed (1.26 vs <= 1.25) and the miss was accepted by the user (2026-11-23). Awaiting final approval.**
- **DoD:** wall time ≤ 1.25× single-task (**not met, 1.26**); resolve-rate drop ≤ 10% (met, 10 points); results table (`m9-results.md`, done). **Changed [D, 2026-11-22, user]:** host is the local PC (Docker Desktop, runc), not the Oracle VM; gVisor was proven on CI in M8.
- Also built here: the 1-day pull-through package cache, read-only to tasks (deferred from M2 [D, 2026-11-20]); Linux sandbox user = host uid:gid.
- **Result (2026-11-23, live run `run-20261123b`, git sha 1562d27):** host local PC (16 CPU, ~6.2 GB Docker Desktop, runc), model `@glm-4-7-flash/glm-4.7-flash`, repo `ShreyaasAI/swf-pr-sandbox`, 10 seeds. Controls 10/10 (pristine target fails, reference resolves). Single pass 10/10 completed and resolved, median wall 90.3 s. Concurrent pass (2 users) 9/10 resolved, median wall 114.1 s. **Wall ratio 1.26 vs DoD <= 1.25: not met (miss by 0.01).** Resolve drop 10 points vs <= 10: met. Cost $0.00 (<= $1): met. 463 calls, 19 PRs (cap 22), 2763.6 s. Cause of the miss: concurrent seed 01 failed `retries_exhausted` after 473 s (z-ai HTTP 429 three times in verify after its tests passed). A provider 429 is not an infra failure (m9 §0.13), so no re-run. Completed-only ratio 1.17. Concurrent pass had 21 model-step retries vs 0 single: the provider rate limit, not the host, is the bottleneck. First live run `run-20261123a` aborted (2 tasks, 0 PRs): glm-4.7-flash wrote `submit_plan.steps` as a bare numbered list (invalid JSON); fixed in 1562d27 (`steps` is a string on the wire, m9 §13 A2). **User decision 2026-11-23: "Accept, close M9"**: the DoD miss is accepted as recorded, no second trial. PRs #2-#20 closed (not merged) and their branches deleted; `seed/*` branches await final approval. Table: `m9-results.md`. Portkey usage before/after: to be filled by the user. Haiku retest: 2138 passed, 16 skipped (platform/env only), 0 failed at `1562d27` (2026-11-23); ruff, ruff format, mypy clean.
- **Decisions [D, 2026-11-23]:** (1) accept the 1.26 wall ratio; (2) package cache is our own pull-through server (`swf.pkgcache`), not Squid (m9 §0.6); (3) Linux sandbox user is the host uid:gid via `sandbox_user()` (1000:1000 on Windows); (4) product default model unchanged: switching to glm for all phases needed the DoD to pass, and it did not (m9 §6). Follow-up before a public deploy: handle provider 429 under concurrency (backoff or provider capacity).

### M10 — Minimal web UI
- [ ] Every spec §3 action works in the browser (manual checklist + screenshots)
- **DoD:** checklist complete.

## 19. Open questions [Q]

1. ~~Idle-approval timeout default (proposed 24 h).~~ — resolved: 24 h [D, 2026-11-20].
2. SSE (default) vs Supabase Realtime for live updates.
3. ~~Portkey free-plan guardrail availability (else in-process output checks).~~ — resolved 2026-11-21 [D]: free plan supports saved regex and JSON-schema output guardrails (`pg-swf-m7-c8f3fc`, config `pc-swf-m7-d1cf5c`); used opt-in via `SWF_PORTKEY_CONFIG`; in-process output guard stays mandatory (`m7-contract.md` §13 A1).
4. ~~Language mix of seeded repos~~ — resolved: Python only [D].
