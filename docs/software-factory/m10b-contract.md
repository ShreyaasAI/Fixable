# M10b contract: deploy readiness

- **Last updated:** 2026-11-25
- **Status:** Contract v1, planner decisions under the user's delegation (2026-11-25: "Do everything and don't ask me anything. Complete the next part of M10 and complete the whole project. Just leave the UI."). Branch `feature/m10`, on top of M10a.
- **Sources:** implementation-plan §16 and §18 M10 (M10b scope: API host/tunnel, gVisor on the PC, CORS allowlist, Vercel env, provider 429; notes: preflight limit, SSE threads), m9-results (provider 429), m10a contract.
- **Out of scope:** the manual UI checklist and screenshots (the user, next session); the Vercel deploy itself (the user); payments (spec §17 non-goal; a post-M10 milestone needs a spec change first).

## 0. Decisions

1. **Hosting.** Vercel serves `web/` as static files (Root Directory `web/`). API and worker stay on the PC. The public HTTPS address for the API is a Cloudflare quick tunnel (`cloudflared tunnel --url http://127.0.0.1:8000`): $0, no account, no domain. Its URL changes on each start, so `VITE_API_BASE` on Vercel changes with it (redeploy). A named tunnel needs a domain (not $0); the runbook lists it as the upgrade. Nothing in this increment starts a tunnel.
2. **Public mode.** `SWF_PUBLIC=1` marks a deployment that strangers can reach. The API then requires `SWF_CORS_ORIGINS`. The worker then refuses to start unless sandboxes run on gVisor (`SWF_SANDBOX_RUNTIME=runsc` and Docker lists `runsc`) and Portkey guardrails are on (a `pc-…` slug, not `off`). No override: running strangers' code under runc on the PC is the risk M8 removed.
3. **CORS allowlist.** `SWF_CORS_ORIGINS`: comma-separated exact origins (`scheme://host[:port]`, no path, no `*`). Each must be `https://`, except `http://localhost` and `http://127.0.0.1` (any port). Unset: no CORS headers (localhost uses the Vite proxy). Allowed: methods `GET, POST, DELETE`; request headers `Authorization, Content-Type, Last-Event-ID`; exposed `Retry-After`; no credentials (the bearer token is a header, not a cookie); preflight cache 600 s.
4. **Provider 429.** A model call that fails with HTTP 429 (litellm `RateLimitError` or `status_code == 429`) waits and retries without using one of the 3 ordinary attempts: waits `(10, 20, 40, 60, 60, 60)` s (about 4 min in all), or the provider's `Retry-After` when longer (capped at 120 s). Each wait emits `retry` with `rate_limited: true` and `wait_seconds`. When the 429 budget is spent, further 429s count as ordinary failures.
5. **Pre-flight limit.** `POST /v1/preflight` gets its own per-user token bucket: 10/min, burst 5 (`SWF_RATE_PREFLIGHT_PER_MINUTE`, `SWF_RATE_PREFLIGHT_BURST`). Refusal: 429 `rate_limited` with `Retry-After`.
6. **SSE stream cap.** Each open `/events` stream holds a threadpool thread. Caps: 4 open streams per user, 40 in all (`SWF_SSE_STREAMS_PER_USER`, `SWF_SSE_STREAMS_TOTAL`). Over the cap: 429 `too_many_streams`, `Retry-After: 5`. A slot is released when the stream ends, the client leaves, or the stream object is collected without starting.
7. **Deploy check.** `uv run --env-file .env python -m swf.deploycheck` prints one line per check (pass/fail plus the fix) and exits 1 if any fails. Checks: required env present; Portkey slug set; `SWF_PUBLIC=1`; `SWF_CORS_ORIGINS` valid; runtime `runsc` configured and listed by Docker; sandbox user non-root (from M8).
8. **Vercel config.** `web/vercel.json`: framework Vite, build `npm run build`, output `dist`, security headers (`X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`, `Permissions-Policy` with camera, microphone and geolocation off). Env on Vercel: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_BASE` (the tunnel URL). `VITE_AUTH_MODE` is never set there (the dev form is also compiled out of production builds, m10a A7).
9. **Supabase keep-alive.** `.github/workflows/supabase-keepalive.yml`: every 3 days plus manual, one REST read with the publishable key. It skips with a notice when the repo secrets `SUPABASE_URL` / `SUPABASE_ANON_KEY` are missing; the user adds them (both are public values).
10. **E2E in CI.** `.github/workflows/m10-e2e.yml` runs the e2e suite and the web build tests on the private CI repo (`ShreyaasAI/swf-ci`) on push to `feature/m10` and on demand. Never on Fixable.
11. **Unchanged.** Product default model stays as is (the M9 DoD missed the wall-ratio target). Guard latency budget stays as is (no new data). Paid tasks stay deferred.

## 1. Interfaces

- `swf.settings`: `AbuseLimits` gains `preflight_per_minute=10.0`, `preflight_burst=5`, `sse_streams_per_user=4`, `sse_streams_total=40` (env as in §0). New `Deployment.from_env(env)` with `public: bool`, `cors_origins: tuple[str, ...]`; `SettingsError` on a bad origin, or `public` without origins.
- `swf.api.app.create_app(deps, *, lifespan=None)`: adds `CORSMiddleware` when `deps.cors_origins` is not empty; `AppDeps.cors_origins: tuple[str, ...] = ()`. `build_app_deps` fills it from `Deployment.from_env()`.
- `swf.api.streams.StreamSlots(per_user=, total=)`: `acquire(user_id) -> Slot | None`; `Slot.release()` is idempotent; `Slot.release_with(obj)` also releases when `obj` (the stream generator) is collected.
- `swf.sandbox.docker_runtimes() -> frozenset[str]` (empty when Docker cannot be asked); `check_runtime` uses it.
- `swf.agent.web_agent.WebAgentConfig.rate_limit_backoff_seconds: tuple[float, ...] = (10, 20, 40, 60, 60, 60)`; `is_rate_limit(exc) -> bool`; `retry_after_seconds(exc) -> float | None`.
- `swf.worker.public_startup_check(env, *, runtimes)`: raises `SettingsError` in public mode without runsc or with Portkey off.
- `swf.deploycheck`: `checks(env, *, docker_runtimes) -> list[Check]`; `main() -> int`.

## 2. Tests (written first)

- `tests/test_m10b_cors.py`: origin parsing (valid, `*`, path, http non-local, public without origins); allowed origin gets `access-control-allow-origin` on a preflight `OPTIONS` and on GET; other origins get none; `Retry-After` exposed; no CORS headers when unset.
- `tests/test_m10b_limits.py`: preflight bucket refuses the 6th call with 429 and leaves create untouched; SSE cap per user and total; slot released after the stream ends.
- `tests/test_m10b_rate_limit.py`: 429 waits follow the schedule and do not use ordinary attempts; `Retry-After` honoured and capped; budget spent → ordinary path; non-429 errors unchanged.
- `tests/test_m10b_public.py`: worker public check (runc refused, runsc missing refused, Portkey off refused, all good passes); deploycheck lines and exit code.
- `tests/test_m10b_web_config.py`: `vercel.json` fields and headers; workflows parse and are gated to the right repos.
- `tests/test_m10_web_build.py` (e2e marker): a Vercel-style production build embeds `VITE_API_BASE` and has no dev form even with `VITE_AUTH_MODE=dev` set.

## 3. Evidence at close

Full non-live suite, e2e suite, ruff, mypy, web typecheck/build, CI e2e run on `swf-ci`, `deploycheck` output on this PC (expected to fail the gVisor and public checks: the PC runs Docker Desktop with runc).
