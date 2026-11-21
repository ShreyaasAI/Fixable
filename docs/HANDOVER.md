# Handover

- **Purpose:** Bring a new session from zero to the next action. This file is the project's state, not its content; details live in the artifacts it points to.
- **Last updated:** 2026-11-21
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
| M5 evidence, harness re-run, read-only backstop | **Implemented, awaiting user review** (2026-11-21). Contract `docs/software-factory/m5-contract.md` (§12 amendments override §0–§8). Evidence: see M5 close-out |
| M6 GitHub clone, clean-clone apply, PR | **Implemented, awaiting user review** (2026-11-21). Live PR: https://github.com/ShreyaasAI/swf-pr-sandbox/pull/1 (open, not merged). Contract `docs/software-factory/m6-contract.md`. Evidence: see M6 close-out |
| M7–M10 | Not started |

Repo state: M2 + M3 committed as `1296b28` on `feature/m2-sandbox-preflight-1120a` (worktree `.claude/worktrees/m2-sandbox-preflight-1120a`); M4 committed there too. `main` still at `e2a9863` ("M0 + M1: …", 98 files). `origin` is not configured (the GitHub repo `https://github.com/ShreyaasAI/Fixable.git` is not a remote), and nothing is pushed. **Do not push until M9 is complete** (user). A new session must work in the worktree (or ask the user), not on `main`. Docker Desktop is running on the dev machine (x86_64).

**Warning:** the main checkout's `.env` is untracked and not ignored on `main` until this branch merges. Never `git add -A` on `main`.

## Waiting on the user

Pending now:
1. Review M5 (evidence, harness re-run, backstop) and M6 (GitHub clone, clean-clone apply, PR). Approve to start M7.
2. Whether to merge `feature/m2-sandbox-preflight-1120a` (M2–M4) into `main` (no push before M9).

Settled: the Supabase Free project is created in M8 (local Postgres until then); the GitHub token comes in via `POST /v1/me/github-token`; the M2 decisions (Squid proxy, package cache deferred to M9, over-limit estimates rejected at creation, 1 vCPU / 4 GB per task) are in `m2-contract.md` §0.

## Next action

M6 implemented (2026-11-21), awaiting user review. On approval, start **M7** (guardrails) from implementation-plan §18. No push before M9.

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
| The original process rules from the user | `PROMPT.md` |
| Baseline for the agent's system prompt | `system_prompts/Cursor_*` (adapt the behavioural rules; the tool schemas don't apply) |

Each artifact labels its statements: **[D]** decision, **[A]** assumption, **[Q]** open question. Treat [A] and [Q] as unsettled.

## Session notes

- Upstream `DockerEnvironment.cleanup()` uses POSIX shell syntax that fails under `cmd.exe`; `SandboxEnv` overrides it.
- Docker test resources are named `swf-test-*` with a per-session suffix; the coder's manual experiments use `swf-coder-*`.
- Foreground `sleep` is blocked for agents; wait with background until-loops.

## Keeping this file current

At the end of each increment, update the stage table, "Waiting on the user", and "Next action". Content changes go to the owning artifact, not here.
