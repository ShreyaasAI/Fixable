# SWF — web coding agent (MVP)

A hosted web app where an agent built on mini-swe-agent fixes a failing test in a user's GitHub repo and opens a PR with evidence.

**Session start:** read `docs/HANDOVER.md` first. It holds the current state, pending user decisions, and the reading order for the plans.

## Gates

Work moves in approved **increments**: one milestone from `docs/software-factory/implementation-plan.md` at a time.
- Before an increment, state what changes, what stays, how it is tested, and what proves it done.
- Write the failing tests first, then the implementation.
- Close the increment with **evidence**: commands run plus their output. Then stop for user review.
- The next milestone starts only after the user approves the current one.
- When scope, security, permissions, infrastructure, or cost is even slightly unclear, ask the user before acting. Infrastructure must cost $0 (free tiers only).
- Invoke skills only when the user names them.

## Source-of-truth order

1. The user's latest instruction.
2. `docs/software-factory/problem-specification.md` (scope).
3. `docs/software-factory/research-plan.md` (rationale).
4. `docs/software-factory/implementation-plan.md` (build steps).

When these conflict, report the conflict before writing code. Record each new decision in the one artifact that owns it, and update that artifact's "Last updated" date.

## Gotchas

- Use Python only (via `uv run …`); SQL for the database and another language for the frontend are allowed. The project pins Python 3.14 in `.python-version`; the home folder pins an uninstalled 3.11.
- Test runs set `PYTHONDONTWRITEBYTECODE=1`. Otherwise a same-size edit within the same second reuses a stale `.pyc` and fakes a result.
- Write files with LF line endings (`.gitattributes`). In Python on Windows, pass `newline="\n"`; CRLF broke the container seed checks once.
- For Docker volume mounts from Git Bash, prefix the command with `MSYS_NO_PATHCONV=1` and use `$(pwd -W)`.
- `seeded_repos/<seed>/reference/` and `seed.yaml` are harness-only. The agent sees only `repo/`.
