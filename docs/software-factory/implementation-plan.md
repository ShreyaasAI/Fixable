# Web Coding Agent — Implementation Plan

- **Purpose:** The ordered, test-first build plan for the approved MVP. A new session should be able to pick up the next unchecked milestone from here.
- **Last updated:** 2026-11-19
- **Status:** Approved (user, 2026-11-19, together with M0). M1 built, awaiting review
- **Related artifacts:** `docs/enterprise-agent/problem-specification.md` (v2, Approved — scope source of truth) · `docs/software-factory/research-plan.md` (v2, Approved — rationale, options, RQs) · `PROMPT.md` (process rules)

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
| `swf.guard` | Input (free LLM + Guardrails AI) and output (Portkey) guardrails | M7 |
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
| POST | `/v1/tasks` | Create task `{repo, base_branch, description, failing_test?}` → `{task, preflight}` |
| GET | `/v1/tasks` / `/v1/tasks/{id}` | List / get |
| GET | `/v1/tasks/{id}/events` | SSE stream (`Last-Event-ID` resume) |
| POST | `/v1/tasks/{id}/approvals/{approval_id}` | `{decision: approve|reject, comment?}` — submitter only |
| POST | `/v1/tasks/{id}/answers/{question_id}` | `{answer}` — submitter only |
| POST | `/v1/tasks/{id}/cancel` | Cancel |
| DELETE | `/v1/tasks/{id}` | Delete task + workspace |
| GET | `/v1/tasks/{id}/evidence` | Evidence bundle |
| POST | `/v1/admin/kill` | Kill switch `{user_id?}` (admin) |

There is no endpoint that pushes, merges, or chooses a branch name.

**DB access model [D, 2026-11-19]:** the browser can call Supabase's REST API directly with its JWT, so `anon` gets nothing and `authenticated` gets SELECT on its own rows only (RLS); `github_tokens` has no grant and no policy. The API reads as `authenticated` (`SET LOCAL ROLE` + `request.jwt.claims`, so RLS applies) and writes with the backend role, taking `user_id` from the verified JWT and writing the audit row in the same transaction. Another user's row → 404. `audit_log` is append-only by trigger (UPDATE/DELETE/TRUNCATE refused for every role).
**JWT [D]:** ES256/RS256 via the project JWKS; HS256 only if the legacy secret is configured; `aud=authenticated`, `role=authenticated`, issuer `<project>/auth/v1`.
**[A]** On Supabase Free, the backend's `postgres` role may `SET ROLE authenticated` (Supabase grants it); verify when the cloud project is created in M8.

## 5. Agent orchestration

- **Phases:** `prepare → investigate → plan → execute → verify → confirm → pr`.
- **Model per phase [D]:** `opencode_go/beam-free` for investigate/plan; `zai/glm-4.7-flash` for execute/verify; all calls via Portkey.
- **Loop:** mini-swe-agent `DefaultAgent.run`. `WebAgent.execute_actions` handles each tool call in order: phase policy → classifier → guard flags → (approval wait) → execute → input guard on the observation → event.
- **Waiting:** an approval/question writes a row and blocks on Postgres LISTEN/poll; the answer re-enters as an observation via `InterruptAgentFlow`. Idle-approval timeout [Q: default 24 h] → `failed` with evidence so far.
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
- **Network:** internal network → egress proxy (domain allowlist of package registries) with a 1-day package cache on the host.
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
2. Oracle Always Free VM + Supabase Free project for M8–M9.
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
- Deferred by design: `preflight` is `null` until M2; SSE `/events` → M4; `/evidence` → M5; `DELETE /v1/tasks/{id}` + workspace and the repo-access check at task creation → M6; quota *enforcement*, rate limits, kill switch → M8 (M1 only counts). The Supabase Free project is created in M8 (user, 2026-11-19); M1 runs on local Postgres only.

### M2 — Sandbox, egress proxy, pre-flight
- [ ] Tests: allowlisted registry reachable, other hosts blocked; token absent from the container; memory/CPU/pids limits; orphan cleanup; arm64 image check; over-threshold warning before start; queueing at capacity
- [ ] `SandboxEnv`, proxy config, `swf.preflight`
- **DoD:** Docker-marked tests green locally.

### M3 — Command classifier + phase policy
- [ ] Adversarial corpus tests (chaining, subshells, `eval`, `python -c`, heredocs, `rm -rf` variants, DB drops)
- [ ] `swf.policy`
- **DoD:** 0 forbidden bypasses on the corpus; unparseable → approval.

### M4 — WebAgent, tools, approvals, model switch, worker
- [ ] Fake-model tests for every status transition, retries ≤ 3 → ask, idle-approval timeout, restart → interrupted
- [ ] `WebAgent`, `PhaseModel`, tools, worker queue (`SKIP LOCKED`), events
- [ ] Live smoke test: one 2-model run via Portkey (RQ1/RQ2)
- **DoD:** transition tests green; the smoke-run trajectory is attached.

### M5 — Evidence + harness re-run + backstop
- [ ] Tests: `complete_task` refused without the bundle; harness mismatch → failed; backstop catches a write in investigate
- [ ] Test runs set `PYTHONDONTWRITEBYTECODE=1` (or clear caches): in M0 a same-size edit within the same second reused a stale `.pyc` and produced a false failure
- **DoD:** tests green on 2 seeded repos end-to-end (fake model).

### M6 — GitHub clone, clean-clone apply, PR
- [ ] Tests: hooks never run (planted hook fixture); branch fixed server-side; no token in the workspace; PR created on a test repo (marked live test)
- **DoD:** a PR opened on a throwaway repo; the link is recorded.

### M7 — Guardrails
- [ ] Tests: seeded injection/PII/toxic/malicious corpus flagged; flag → approval required for the next command; malformed output caught by the output guard (Portkey or in-process fallback)
- **DoD:** corpus results table with false-positive rate on the clean corpus.

### M8 — Abuse controls + gVisor
- [ ] Tests: over-quota refused pre-start; 429 rate limiting; kill switch stops tasks ≤ 30 s; sandbox suite passes under `runsc`
- **DoD:** tests green on the Oracle VM.

### M9 — Concurrency + evaluation (Oracle VM)
- [ ] 10 seeded repos through the full pipeline; 2 concurrent tasks
- **DoD:** wall time ≤ 1.25× single-task; resolve-rate drop ≤ 10%; results table.

### M10 — Minimal web UI
- [ ] Every spec §3 action works in the browser (manual checklist + screenshots)
- **DoD:** checklist complete.

## 19. Open questions [Q]

1. Idle-approval timeout default (proposed 24 h).
2. SSE (default) vs Supabase Realtime for live updates.
3. Portkey free-plan guardrail availability (else in-process output checks).
4. ~~Language mix of seeded repos~~ — resolved: Python only [D].
