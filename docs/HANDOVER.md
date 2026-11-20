# Handover

- **Purpose:** Bring a new session from zero to the next action. This file is the project's state, not its content; details live in the artifacts it points to.
- **Last updated:** 2026-11-20
- **Status:** Current

## Where the project is

| Stage | State |
|---|---|
| Research (interview → spec) | Done. Spec v2 **Approved** |
| Planning (research plan) | Done. **Approved**, via the user's go-ahead to implement |
| Implementation plan | **Approved** (user, 2026-11-19) |
| M0 skeleton, `AGENTS.md` loader, 10 seeded repos | **Approved** (user, 2026-11-19) |
| M1 data model, RLS, API skeleton, auth, audit | **Approved** (user, 2026-11-20); committed in `e2a9863` |
| M2 sandbox, egress proxy, pre-flight | **Built, awaiting user review**. 405 passed incl. docker/network + 10 seeds in the sandbox; 9/9 security mutations caught. Contract: `docs/software-factory/m2-contract.md` |
| M3 command classifier + phase policy | **Built, awaiting user review**. Corpus 260 cmds, 0 forbidden bypasses; full suite 692 passed, 1 skipped; ruff/format/mypy clean |
| M4–M10 | Not started |

Repo state: `main` holds one local commit, `e2a9863` ("M0 + M1: …", 98 files). `origin` is not configured (the GitHub repo `https://github.com/ShreyaasAI/Fixable.git` is not a remote), and nothing is pushed. **Do not push until M9 is complete** (user). M2 lives only in the worktree above, branched from local `main`; a new session must work there (or ask the user), not on `main`. Docker Desktop is running on the dev machine (x86_64).

## Waiting on the user

The user said (2026-11-20): build M3 without asking for approval and review after M3. Pending now:
1. Review and approve **M2 and M3** (evidence in implementation-plan §18).
2. Whether to commit M2 + M3 on the feature branch and merge into `main` (nothing committed since `e2a9863`; no push before M9).

Settled: the Supabase Free project is created in M8 (local Postgres until then); the GitHub token comes in via `POST /v1/me/github-token`; the M2 decisions (Squid proxy, package cache deferred to M9, over-limit estimates rejected at creation, 1 vCPU / 4 GB per task) are in `m2-contract.md` §0.

## Next action

Wait for the user's review of M2 + M3. After approval: commit, then **M4** (WebAgent, tools, approvals, model switch, worker) from implementation-plan §18; M4 must also apply the M2 open risks listed there (`MSWEA_GLOBAL_CONFIG_DIR`, serialised admission).

## Reading guide

Read by need, not front to back:

| You need | Read |
|---|---|
| What we build and what we don't; permissions; success criteria | `docs/enterprise-agent/problem-specification.md` — §1 objective, §4–5 scope, §8 permissions, §10 criteria, §12–13 open questions/assumptions |
| Why a design was chosen; mini-swe-agent facts; options rejected | `docs/software-factory/research-plan.md` — §2 verified facts, §6 security, §7 options, §8 architecture |
| What to build next, how, and its definition of done | `docs/software-factory/implementation-plan.md` — §18 milestones (checkboxes = progress), §3 components, §4 API, §9 data model, §12 tools/permissions |
| M2 interfaces, decisions and amendments (working contract) | `docs/software-factory/m2-contract.md` — §0 decisions; §9 amendments override §1–§8 |
| The original process rules from the user | `PROMPT.md` |
| Baseline for the agent's system prompt | `system_prompts/Cursor_*` (adapt the behavioural rules; the tool schemas don't apply) |

Each artifact labels its statements: **[D]** decision, **[A]** assumption, **[Q]** open question. Treat [A] and [Q] as unsettled.

## Session notes

- Upstream `DockerEnvironment.cleanup()` uses POSIX shell syntax that fails under `cmd.exe`; `SandboxEnv` overrides it.
- Docker test resources are named `swf-test-*` with a per-session suffix; the coder's manual experiments use `swf-coder-*`.
- Foreground `sleep` is blocked for agents; wait with background until-loops.

## Keeping this file current

At the end of each increment, update the stage table, "Waiting on the user", and "Next action". Content changes go to the owning artifact, not here.
