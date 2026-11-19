# Handover

- **Purpose:** Bring a new session from zero to the next action. This file is the project's state, not its content; details live in the artifacts it points to.
- **Last updated:** 2026-11-19
- **Status:** Current

## Where the project is

| Stage | State |
|---|---|
| Research (interview → spec) | Done. Spec v2 **Approved** |
| Planning (research plan) | Done. **Approved**, via the user's go-ahead to implement |
| Implementation plan | **Approved** (user, 2026-11-19) |
| M0 skeleton, `AGENTS.md` loader, 10 seeded repos | **Approved** (user, 2026-11-19) |
| M1 data model, RLS, API skeleton, auth, audit | **Built, awaiting user review**. Evidence: 149 pytest passed (DB tests on `postgres:17-alpine` in Docker); ruff, ruff format, mypy strict clean |
| M2–M10 | Not started |

The repo is git-initialized on `main`; nothing is committed yet (the user declined a commit on 2026-11-19). Remote: `https://github.com/ShreyaasAI/Fixable.git`. **Do not push until M9 is complete** (user, 2026-11-19). Docker Desktop is running on the dev machine (x86_64).

## Waiting on the user

These are blocking; ask if they are still unanswered:
1. Review and approve M1 → this unlocks M2.
2. Make the initial commit now? (Declined once, on 2026-11-19; ask again at each milestone close.)

Settled: the Supabase Free project is created in M8 (local Postgres until then); the GitHub token comes in via `POST /v1/me/github-token`.

## Next action

Start **M2** once item 1 above is approved. Find the next unchecked milestone in `docs/software-factory/implementation-plan.md` §18 and follow the gates in `CLAUDE.md`.

## Reading guide

Read by need, not front to back:

| You need | Read |
|---|---|
| What we build and what we don't; permissions; success criteria | `docs/enterprise-agent/problem-specification.md` — §1 objective, §4–5 scope, §8 permissions, §10 criteria, §12–13 open questions/assumptions |
| Why a design was chosen; mini-swe-agent facts; options rejected | `docs/software-factory/research-plan.md` — §2 verified facts, §6 security, §7 options, §8 architecture |
| What to build next, how, and its definition of done | `docs/software-factory/implementation-plan.md` — §18 milestones (checkboxes = progress), §3 components, §4 API, §9 data model, §12 tools/permissions |
| The original process rules from the user | `PROMPT.md` |
| Baseline for the agent's system prompt | `system_prompts/Cursor_*` (adapt the behavioural rules; the tool schemas don't apply) |

Each artifact labels its statements: **[D]** decision, **[A]** assumption, **[Q]** open question. Treat [A] and [Q] as unsettled.

## Keeping this file current

At the end of each increment, update the stage table, "Waiting on the user", and "Next action". Content changes go to the owning artifact, not here.
