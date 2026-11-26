# Handover

- **Purpose:** Bring a new session from zero to the next action. This file is the project's state, not its content; details live in the artifacts it points to.
- **Last updated:** 2026-11-26
- **Status:** Current

## Where the project is

| Stage | State |
|---|---|
| Research (interview → spec) | Done. Spec v2 **Approved** |
| Planning (research plan) | Done. **Approved**, via the user's go-ahead to implement |
| Implementation plan | **Approved** (user, 2026-11-19) |
| M0 skeleton, `AGENTS.md` loader, 10 seeded repos | **Approved** (user, 2026-11-19) |
| M1 data model, RLS, API skeleton, auth, audit | **Approved** (user, 2026-11-20); committed in `e2a9863` |
| M2 sandbox, egress proxy, pre-flight | **Built, committed `1296b28` on `feature/m2-sandbox-preflight-1120a`**, user said complete (2026-11-20). Contract: `docs/software-factory/m2-contract.md` |
| M3 command classifier + phase policy | **Built, committed `1296b28` on `feature/m2-sandbox-preflight-1120a`**, user said complete (2026-11-20). Corpus 260 cmds, 0 forbidden bypasses |
| M4 WebAgent, tools, approvals, model switch, worker | **Approved (user, 2026-11-20), committed on the feature branch**. 415 M4 tests passed; live 2-model Portkey smoke passed ($0); contract `docs/software-factory/m4-contract.md` |
| M5 evidence, harness re-run, read-only backstop | **Approved, merged to `main`** (`1235cdc`). Contract `docs/software-factory/m5-contract.md` (§12 amendments override §0–§8) |
| M6 GitHub clone, clean-clone apply, PR | **Approved, merged to `main`** (`1235cdc`). Live PR: https://github.com/ShreyaasAI/swf-pr-sandbox/pull/1. Contract `docs/software-factory/m6-contract.md` |
| M7 guardrails (input judge + regex, output guard, Portkey opt-in) | **Approved (user, 2026-11-21), merged to `main`**. Contract `docs/software-factory/m7-contract.md` (§13 A1–A5 override §0–§12). Results: implementation-plan §18 M7 |
| M8 abuse controls + gVisor | **Approved, merged to `main`** (`4f982a4`, 2026-11-22). Contract `docs/software-factory/m8-contract.md` (§13 A1–A5 override §0–§6; §5 results). gVisor DoD ran on GitHub Actions, not Oracle (needs a card) |
| M9 concurrency + evaluation | **Done, approved (user, 2026-11-23), merged to `main` (`955818b`), pushed to `ShreyaasAI/Fixable`**. Contract `docs/software-factory/m9-contract.md` (§5 results, §13 A1–A2). Live run `run-20261123b`: single 10/10, concurrent 9/10 (drop 10 points, met), wall ratio 1.26 vs <= 1.25 (**not met**, miss accepted by the user 2026-11-23: "Accept, close M9"). Results: `docs/software-factory/m9-results.md` |
| M10 | **M10-0** refactors approved (user, 2026-11-24). **M10a** localhost UI (contract `m10a-contract.md`, approved "Approve, Q1 yes") and **M10b** deploy readiness (contract `m10b-contract.md`, planner decisions under the user's delegation 2026-11-25) are built, tested and merged to `main`. Automated: non-live suite, e2e (local and CI on `swf-ci`), gVisor CI. **Not done (user):** the manual UI checklist + live smoke PR (`m10a-checklist.md`), and the public deploy steps in `m10-runbook.md` §6 |

Repo state (2026-11-26):
- `main` = `519582e` ("Merge M10: web UI (M10a) and deploy readiness (M10b)"), contains M0–M10, pushed to `origin` = `ShreyaasAI/Fixable` (**public**; every push follows a history secret scan). Push only after an approved increment (or the user's explicit delegation, as on 2026-11-25).
- `ci` = `ShreyaasAI/swf-ci` (private, CI only). Branches there: `feature/m8`, `feature/m9`, `feature/m10`. The e2e and gVisor workflows run there, never on Fixable.
- Worktrees: `.claude/worktrees/m10` (`feature/m10`, merged into `main`, safe to remove or reuse), `m8-plan` and `m9-plan` (merged long ago, removable). New work: a fresh worktree off `main`.
- The main checkout's `.env` is untracked and holds the secrets (Supabase, Portkey, `SWF_TOKEN_ENCRYPTION_KEY`). Never `git add -A` on `main`; never commit `.env` or `web/.env.local`.
- Dev machine: Windows 11, x86_64, Docker Desktop (runc only, no runsc), Python 3.14 via uv, Node + npm for `web/`.

## Waiting on the user

The user said (2026-11-25): "Do everything and don't ask me anything. Complete the next part of M10 and complete the whole project. Just leave the UI. I will continue the UI in next session." M10b was built, reviewed and merged under that delegation. Left to the user:

1. **UI (next session).** Manual checklist `m10a-checklist.md` rows 1–17 with screenshots (375 px and 1440 px, dark and light), then one live smoke PR on `swf-pr-sandbox`; record its URL in the checklist, close it, delete its branch. That ticks the M10 DoD box in implementation-plan §18.
2. **Public deploy (when wanted).** `m10-runbook.md` §6 in order: gVisor in WSL2 Ubuntu (Docker Engine + runsc) for the worker → `.env` `SWF_PUBLIC=1`, `SWF_CORS_ORIGINS=https://<project>.vercel.app`, `SWF_SANDBOX_RUNTIME=runsc` → `cloudflared tunnel --url http://127.0.0.1:8000` → Vercel (Root Directory `web/`; env `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_BASE` = tunnel URL; never `VITE_AUTH_MODE`) → Supabase Site URL + redirect `https://<project>.vercel.app/**` → keep-alive secrets `SUPABASE_URL`, `SUPABASE_ANON_KEY` on the repo that runs the workflow. `uv run --env-file .env python -m swf.deploycheck` must print all PASS first; on this PC today it fails `public mode`, `cors origins` and `gvisor` (expected).

## Next session: start here

Docker Desktop running. From a worktree, `.env` is three levels up (`..\..\..\.env`); from the main checkout it is `.env`. Three terminals (runbook §4):

```powershell
uv run --env-file ..\..\..\.env uvicorn swf.api.main:create_app_from_env --factory --workers 1
```

```powershell
uv run --env-file ..\..\..\.env python -m swf.worker
```

```powershell
cd web; npm ci; npm run dev
```

Open `http://localhost:5173`, sign in with GitHub. Admin page: after the first sign-in, set `profiles.is_admin = true` for your GitHub **login** (not URL) in the Supabase SQL editor (runbook §2 step 4).

Pitfalls seen in the 2026-11-25 session:
- **Stale backend.** An API started from an older checkout (M9) answers 404 on `/v1/preflight` and `/v1/admin/status`; the Start button then stays disabled and Admin says "not found". Stop every old uvicorn/worker and start from `main` or the m10 worktree.
- **500 on `/v1/...` with no API log line** = the API is not running; the Vite proxy cannot connect.
- **Missing settings** = forgot `--env-file`; SWF reads the environment only, never `.env` by itself.
- **Worker refuses to start** unless `SWF_PORTKEY_CONFIG` is a `pc-…` slug or `off`; in public mode also without runsc.
- **Blank page** was a render error on an old backend's response; an error boundary now shows "page-error" instead. If it recurs, read the browser console.
- `web/.env.local` holds the `VITE_*` values for dev; `web/.env.example` is the tracked template (restore it with `git checkout` if deleted).

Tests (runbook §5):
- Non-live: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m "not live and not e2e"` (last full run: 2338 passed, then the 2 ops tests fixed → 28 passed).
- E2E (Docker, npm, Chromium; `uv run playwright install chromium` once): `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m e2e tests/e2e tests/test_m10_web_build.py` (17 passed locally and on CI).
- `tests/e2e/` is a package (`from e2e import fakes`), so the full suite collects without name clashes. The dev-build test sets `NODE_ENV=development`: Vite's `import.meta.env.DEV` follows `NODE_ENV`, not `--mode`.
- CI on `swf-ci` for `519582e`'s tree: `m10-e2e` 17 passed, 0 skipped; `m8-gvisor` 2346 passed, 10 skipped.

Memory note not saved (the auto-mode classifier refused the write): the "push only after an approved increment" rule was relaxed by the user's 2026-11-25 delegation for M10b only.

Settled 2026-11-23: the wall-ratio miss (1.26 vs <= 1.25) is accepted as recorded; no second trial. Eval PRs #2-#20 on `swf-pr-sandbox` were closed (not merged) and their branches deleted; PR #1 (older live test) left open.

Settled 2026-11-22 (m9-contract §0.23–§0.26):
- Public link deferred to M10 (GitHub Pages UI + Cloudflare tunnel only once the PC has gVisor; Actions can't serve traffic).
- The 10 `seed/<name>` branches on `swf-pr-sandbox` were deleted 2026-11-23 after M9 approval (`swf_eval seed-branches` recreates them).
- Eval uses glm-4.7-flash for all phases (product default switches only if DoD passes).
- Paid tasks ("2 free/day, then $1 per task") = real payments: deferred to a new post-M10 milestone (spec §5 lists billing as a non-goal; spec update needed then). M9 keeps spec-default quotas.
- M9 eval host: local PC (Docker Desktop, runc); gVisor already proven on CI (M8 A5). Live LLM + real PRs to `swf-pr-sandbox` approved.

Open risks: the worker refuses to start unless `SWF_PORTKEY_CONFIG` is a `pc-…` slug or `off` (m10a §1.8), and in public mode without gVisor (m10b §0.2); provider HTTP 429 now waits up to ~4 min per model call before it counts as a failure (m10b §0.4), but provider capacity is still the limit under concurrency (not re-measured); the quick-tunnel URL changes on every start (Vercel env + redeploy each time); M7 guard latency (p95 50 s per judged text; cold-start guard 170-254 s on the first two single tasks); gVisor pids-limit can end the sandbox (m8 §5); cache memory kept at 256m (lead's call; m9 §13 A1); runsc and arm64 untested on the eval host; `swf_eval report` to stdout fails on a cp1252 Windows console (use `--write`).

## Next action

All milestones M0–M10 are built and merged to `main` (`519582e`, pushed to `ShreyaasAI/Fixable` after a secret scan). What remains is the user's: the manual UI checklist + live smoke PR (next session; UI fixes found there go in a new worktree off `main`, tests first, e2e ids in m10a-contract §2.1), then the public deploy per runbook §6. Paid tasks stay a post-M10 milestone that needs a spec change first (spec §17 lists billing as a non-goal).

## Reading guide

Read by need, not front to back:

| You need | Read |
|---|---|
| What we build and what we don't; permissions; success criteria | `docs/software-factory/problem-specification.md` — §1 objective, §4–5 scope, §8 permissions, §10 criteria, §12–13 open questions/assumptions |
| Why a design was chosen; mini-swe-agent facts; options rejected | `docs/software-factory/research-plan.md` — §2 verified facts, §6 security, §7 options, §8 architecture |
| What to build next, how, and its definition of done | `docs/software-factory/implementation-plan.md` — §18 milestones (checkboxes = progress), §3 components, §4 API, §9 data model, §12 tools/permissions |
| M2 interfaces, decisions and amendments (working contract) | `docs/software-factory/m2-contract.md` — §0 decisions; §9 amendments override §1–§8 |
| M4 interfaces and amendments (working contract) | `docs/software-factory/m4-contract.md` — §12 amendments override §0–§11 |
| M5 interfaces and amendments (working contract) | `docs/software-factory/m5-contract.md` — §12 amendments override §0–§8 |
| M6 interfaces and decisions (working contract) | `docs/software-factory/m6-contract.md` — §0 decisions; §4 `swf.pr`; §11 deferred |
| M7 interfaces, decisions and amendments (working contract) | `docs/software-factory/m7-contract.md` — §13 amendments A1–A5 override §0–§12; §11 close-out |
| M8 interfaces, decisions and amendments (working contract) | `docs/software-factory/m8-contract.md` — §13 amendments A1–A5 override §0–§6; §5 results and carried risks |
| M9 interfaces, decisions, DoD (working contract) | `docs/software-factory/m9-contract.md` — §0 decisions (§0.23–26 user answers), §1 interfaces, §3 tasks, §13 amendments |
| M10b deploy readiness (public mode, CORS, 429, limits, deploycheck) | `docs/software-factory/m10b-contract.md` — §0 decisions, §4 review amendments; deploy steps: `m10-runbook.md` §6 |
| M10a UI interfaces, test ids, design system, tasks | `docs/software-factory/m10a-contract.md` — §0 decisions, §1 backend, §2 frontend (§2.1 test ids, §2.2 amendments), §4 tests; setup: `m10-runbook.md`; manual: `m10a-checklist.md` |
| The original process rules from the user | `PROMPT.md` |
| Baseline for the agent's system prompt | `system_prompts/Cursor_*` (adapt the behavioural rules; the tool schemas don't apply) |

Each artifact labels its statements: **[D]** decision, **[A]** assumption, **[Q]** open question. Treat [A] and [Q] as unsettled.

## Session notes

- Upstream `DockerEnvironment.cleanup()` uses POSIX shell syntax that fails under `cmd.exe`; `SandboxEnv` overrides it.
- Docker test resources are named `swf-test-*` with a per-session suffix; the coder's manual experiments use `swf-coder-*`.
- Foreground `sleep` is blocked for agents; wait with background until-loops.

## Keeping this file current

At the end of each increment, update the stage table, "Waiting on the user", and "Next action". Content changes go to the owning artifact, not here.
