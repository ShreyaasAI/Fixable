# CLAUDE.md

SWF ("Fixable"): a hosted web app where an agent built on mini-swe-agent fixes a failing test in a user's GitHub repo and opens a PR with evidence. Python 3.14 (uv) FastAPI API + worker, Postgres/Supabase (SQL, RLS), Docker sandboxes (gVisor in public mode), Vite + React + TypeScript UI in `web/`.

**Session start:** read `docs/HANDOVER.md` first: current state, what waits on the user, run commands, pitfalls, reading guide. State on 2026-11-26: M0–M10 built and merged to `main`; left to the user are the M10 manual UI checklist + live smoke PR and the public deploy (runbook §6).

## Project map

- `src/swf/` - backend: `api/` (FastAPI app, SSE stream slots), `agent/` (WebAgent, tools, model retries), `worker.py`, `store.py` + `db.py` (data access, RLS), `sandbox.py`, `egress.py`, `policy.py`, `guard.py`, `preflight.py`, `github.py` / `pr.py`, `settings.py` (all `SWF_*` env), `deploycheck.py`
- `src/swf_eval/` - M9 evaluation harness (`swf_eval report --write`, `seed-branches`)
- `web/` - Vite + React UI (`src/screens/`, `api.ts`, `sse.ts`, `auth.ts`), `vercel.json`
- `supabase/` - SQL migrations
- `tests/` - pytest suite; `tests/e2e/` Playwright package with fake API/worker
- `seeded_repos/` - 10 seeded eval repos
- `ops/`, `.github/workflows/` - CI scripts (gVisor, e2e, Supabase keep-alive)
- `docs/HANDOVER.md`, `docs/software-factory/` - spec, research plan, implementation plan, per-milestone contracts, `m10-runbook.md`, `m10a-checklist.md`

<important if="you need to run commands to build, test, lint, run, or deploy">

From a worktree, `.env` is the main checkout's: `..\..\..\.env`. Docker Desktop must be running.

| Command | What it does |
|---|---|
| `uv run --env-file .env uvicorn swf.api.main:create_app_from_env --factory --workers 1` | API on 127.0.0.1:8000 |
| `uv run --env-file .env python -m swf.worker` | Worker |
| `cd web; npm ci; npm run dev` | UI on localhost:5173 (proxies `/v1` to :8000) |
| `cd web; npm run typecheck; npm run build` | Web typecheck and build |
| `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m "not live and not e2e"` | Non-live suite |
| `uv run playwright install chromium` | Chromium for e2e (once) |
| `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m e2e tests/e2e tests/test_m10_web_build.py` | E2E (Docker, npm, Chromium) |
| `uv run ruff check .` / `uv run mypy` | Lint / strict types |
| `uv run --env-file .env python -m swf.deploycheck` | Public-deploy readiness (all PASS required) |
</important>

<important if="you are starting, planning, or closing a milestone or increment">

- Work moves in approved increments: one milestone from `docs/software-factory/implementation-plan.md` at a time.
- Before an increment, state what changes, what stays, how it is tested, and what proves it done.
- Close with evidence (commands run plus output), then stop for user review. The next milestone starts only after approval.
- At the end of each increment, update `docs/HANDOVER.md` (stage table, "Waiting on the user", "Next action").
</important>

<important if="you are writing new code or fixing a bug">
- Write the failing tests first, then the implementation.
</important>

<important if="scope, security, permissions, infrastructure, or cost is even slightly unclear">
- Ask the user before acting. Infrastructure must cost $0 (free tiers only).
</important>

<important if="you are considering invoking a skill">
- Invoke skills only when the user names them.
</important>

<important if="docs or the user's instructions disagree, or you are recording a decision">

Source-of-truth order:
1. The user's latest instruction.
2. `docs/software-factory/problem-specification.md` (scope).
3. `docs/software-factory/research-plan.md` (rationale).
4. `docs/software-factory/implementation-plan.md` (build steps).

Report a conflict before writing code. Record each new decision in the one artifact that owns it and update its "Last updated" date.
</important>

<important if="you are committing, pushing, or touching git branches">

- Work in a worktree off `main`, never on `main` directly. The main checkout's `.env` is untracked: never `git add -A` there; never commit `.env` or `web/.env.local`.
- `origin` (`ShreyaasAI/Fixable`) is PUBLIC: push `main` only after an approved increment, and secret-scan the history first.
- `ci` (`ShreyaasAI/swf-ci`) is private and runs the e2e and gVisor workflows; only `swf-pr-sandbox` receives live test PRs.
</important>

<important if="you are handling secrets, tokens, or .env values">
- Never print or log secret values (Portkey key, Supabase keys, `SWF_TOKEN_ENCRYPTION_KEY`). The GitHub OAuth client secret lives only in Supabase.
</important>

<important if="you are choosing a language or adding a script">
- Python only, via `uv run …` (3.14 pinned in `.python-version`; the home folder pins an uninstalled 3.11). SQL for the database and TypeScript for `web/` are allowed.
</important>

<important if="you are running Python tests">
- Set `PYTHONDONTWRITEBYTECODE=1`: otherwise a same-size edit within the same second reuses a stale `.pyc` and fakes a result.
- `tests/e2e/` is a package: import its helpers as `from e2e import fakes`.
</important>

<important if="you are writing or generating files">
- Use LF line endings (`.gitattributes`). In Python on Windows pass `newline="\n"`; CRLF once broke the container seed checks.
</important>

<important if="you are running Docker with volume mounts from Git Bash">
- Prefix with `MSYS_NO_PATHCONV=1` and use `$(pwd -W)` for host paths.
</important>

<important if="you are working with seeded_repos or the agent's view of a repo">
- `seeded_repos/<seed>/reference/` and `seed.yaml` are harness-only. The agent sees only `repo/`.
</important>

<important if="you are debugging the local web UI or API">
- 404 on `/v1/preflight` or `/v1/admin/status` = an API from an older checkout is running; restart it from `main`.
- 500 on `/v1/...` with no API log line = the API is down (Vite proxy cannot connect).
- Missing settings = forgot `--env-file`; SWF reads the environment only.
- Vite's `import.meta.env.DEV` follows `NODE_ENV`, not `--mode`; the dev sign-in form exists only when DEV and `VITE_AUTH_MODE=dev`.
</important>

<important if="you are working on public deploy, CORS, or gVisor">
- `SWF_PUBLIC=1` requires `SWF_CORS_ORIGINS` (exact https origins) and makes the worker refuse to start without runsc and Portkey guardrails. Steps: `docs/software-factory/m10-runbook.md` §6; decisions: `m10b-contract.md`.
</important>
