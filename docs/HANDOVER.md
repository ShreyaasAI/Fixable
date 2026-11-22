# Handover

- **Purpose:** Bring a new session from zero to the next action. This file is the project's state, not its content; details live in the artifacts it points to.
- **Last updated:** 2026-11-22
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
| M9 concurrency + evaluation | **In progress** on `feature/m9` (worktree `.claude/worktrees/m9-plan`). Contract `docs/software-factory/m9-contract.md` v2 **approved** (user, 2026-11-22). T1–T6 done; T7 tests done; T8 in progress; T9–T12 pending |
| M10 | Not started |

Repo state: `main` contains M0–M8; M9 is on `feature/m9`. **Do not push to `ShreyaasAI/Fixable` until M9 is complete** (user). `ShreyaasAI/swf-ci` is the private CI-only repo. Work in a worktree, not on `main`. Main checkout's `.env` is untracked: never `git add -A` on `main`. Docker Desktop is running on the dev machine (x86_64).

## Waiting on the user

Nothing blocking. Settled 2026-11-22 (m9-contract §0.23–§0.26):
- Public link deferred to M10 (GitHub Pages UI + Cloudflare tunnel only once the PC has gVisor; Actions can't serve traffic).
- Eval may push 10 `seed/<name>` branches to `swf-pr-sandbox`; delete after M9 approval.
- Eval uses glm-4.7-flash for all phases (product default switches only if DoD passes).
- Paid tasks ("2 free/day, then $1 per task") = real payments: deferred to a new post-M10 milestone (spec §5 lists billing as a non-goal; spec update needed then). M9 keeps spec-default quotas.
- M9 eval host: local PC (Docker Desktop, runc); gVisor already proven on CI (M8 A5). Live LLM + real PRs to `swf-pr-sandbox` approved.

Open risks: M7 guard latency (p95 50 s per judged text) will dominate M9 wall time; gVisor pids-limit can end the sandbox (m8 §5); cache memory kept at 256m (lead's call; m9 §13 A1).

## Next action

M9 in progress (2026-11-22). Commits on `feature/m9`: T1 `2798ead`, T2 `9f0de61`+`fe04ee0`, T3 `be91815`, T4 `18ac6cd`+`f2f48d1`, T5 `55d588c`, T6 `6a0dd3c`, T7 `c39f63a`.
- T6 deviations accepted by lead: pkgcache.py bind-mounted read-only (docker cp fails on `--read-only`); conf-sha also covers proxy URL, user, cache dir.
- Next: finish T8 (`swf_eval`), review T5–T8, then T9 (CI on swf-ci as uid 1001), T10–T11 live eval (PC only), T12 results + cleanup. No push to Fixable before M9 is approved.

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
| The original process rules from the user | `PROMPT.md` |
| Baseline for the agent's system prompt | `system_prompts/Cursor_*` (adapt the behavioural rules; the tool schemas don't apply) |

Each artifact labels its statements: **[D]** decision, **[A]** assumption, **[Q]** open question. Treat [A] and [Q] as unsettled.

## Session notes

- Upstream `DockerEnvironment.cleanup()` uses POSIX shell syntax that fails under `cmd.exe`; `SandboxEnv` overrides it.
- Docker test resources are named `swf-test-*` with a per-session suffix; the coder's manual experiments use `swf-coder-*`.
- Foreground `sleep` is blocked for agents; wait with background until-loops.

## Keeping this file current

At the end of each increment, update the stage table, "Waiting on the user", and "Next action". Content changes go to the owning artifact, not here.
