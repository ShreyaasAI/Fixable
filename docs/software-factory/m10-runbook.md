# M10 runbook: run the web UI on localhost

- **Purpose:** One-time setup you do in the GitHub, Supabase and Portkey dashboards, then the commands that start SWF on your PC with the web UI.
- **Last updated:** 2026-11-25
- **Sources:** m10a-contract §0 (decisions 2, 3, 14), §1.8, §5; m10b-contract §0 (public deploy, §6 here).

No secret goes into git. Server secrets live in the repo-root `.env`. The browser gets only `web/.env.local`, which holds public values.

## 1. GitHub OAuth App (once)

1. Open GitHub → Settings → Developer settings → OAuth Apps → **New OAuth App**.
2. Fill in:
   - **Application name:** `SWF (local)`.
   - **Homepage URL:** `http://localhost:5173`.
   - **Authorization callback URL:** `https://<project-ref>.supabase.co/auth/v1/callback`, using your Supabase project ref.
3. Create the app, then **Generate a new client secret**. Copy the client ID and the secret.
4. The secret goes only into Supabase (step 2.1). Do not put it in `.env` or anywhere else.

## 2. Supabase (once, on the Free project from M8)

1. Authentication → Sign In / Providers → **GitHub**: enable it and paste the client ID and secret.
2. Authentication → URL Configuration:
   - **Site URL:** `http://localhost:5173`
   - **Redirect URLs:** add `http://localhost:5173/**`
3. Project Settings → API: copy the **Project URL** and the **publishable (anon) key** into `web/.env.local` (see `web/.env.example`):

   ```
   VITE_SUPABASE_URL=https://<project-ref>.supabase.co
   VITE_SUPABASE_ANON_KEY=<publishable key>
   ```

4. Admin account (after your first sign-in): run this in the SQL editor:

   ```sql
   update public.profiles set is_admin = true where github_login = '<your-login>';
   ```

The UI asks GitHub for the `repo` scope, and the sign-in screen says so. The API stores the GitHub token Fernet-encrypted (`SWF_TOKEN_ENCRYPTION_KEY`). The worker uses it only to clone and push the `agent/<task-id>` branch.

## 3. Portkey saved guardrails (once; required since M10a)

The worker refuses to start unless `SWF_PORTKEY_CONFIG` is a `pc-…` slug or the literal `off` (m10a §1.8).

1. In Portkey, go to Guardrails → **Create**. Add a **Regex Match** check on the **output** for secrets. One pattern per check, or one alternation:
   - `gh[pousr]_[A-Za-z0-9]{36,}` (GitHub tokens)
   - `github_pat_[A-Za-z0-9_]{22,}`
   - `AKIA[0-9A-Z]{16}` (AWS access key id)
   - `-----BEGIN [A-Z ]*PRIVATE KEY-----`
2. Set the guardrail's action to **not deny**, so the request still returns. The response then carries `hook_results`, and SWF turns a failed check into a `guard_flag`, which pauses before the next guarded tool. If the results are missing, that is also flagged (fail-closed).
3. Configs → **Create**: attach the guardrail as an *output guardrail* (after-request hook) and save. Copy the config slug (`pc-…`).
4. Add it to the repo-root `.env`: `SWF_PORTKEY_CONFIG=pc-…`. Use `off` only for local experiments; the worker logs a warning when it is off.

## 4. Run (each session)

Docker Desktop must be running. Use three terminals, all in the repo root. SWF reads settings from the environment only, so the API and the worker load `.env` with `--env-file` (from a worktree, point at the main checkout's `.env`, e.g. `..\..\..\.env`):

```powershell
uv run --env-file .env uvicorn swf.api.main:create_app_from_env --factory --workers 1
```

```powershell
uv run --env-file .env python -m swf.worker
```

```powershell
cd web; npm ci; npm run dev
```

Open `http://localhost:5173` and sign in with GitHub. The Vite dev server proxies `/v1` to `127.0.0.1:8000`; set `SWF_API_PROXY` to use another API address. A 500 on any `/v1/...` request with no API log line means the API is not running (the proxy cannot connect).

## 5. Tests

| What | Command |
|---|---|
| Non-live suite (no browser) | `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m "not live and not e2e"` |
| Chromium for e2e (once) | `uv run playwright install chromium` |
| E2E (Docker, npm, Chromium) | `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m e2e tests/e2e tests/test_m10_web_build.py` |
| Web typecheck and build | `cd web; npm run typecheck; npm run build` |

The e2e stack writes its logs to `<pytest tmp>/…/logs/{api,worker,vite}.log`. Set `SWF_E2E_HEADED=1` to watch the browser.

## 6. Public deploy (m10b)

Vercel serves the web app; the API and the worker stay on this PC behind a Cloudflare quick tunnel. Everything here is $0. Run `deploycheck` (step 5) before you open the tunnel.

1. **gVisor where the worker runs (required in public mode).** Docker Desktop on Windows has no `runsc`. Run the worker in a WSL2 Ubuntu distro with Docker Engine (not Docker Desktop) and gVisor:
   - In Ubuntu: install Docker Engine (docs.docker.com/engine/install/ubuntu), then gVisor exactly as `.github/workflows/m8-gvisor.yml` does: add the gVisor apt repo, `sudo apt-get install -y runsc`, `sudo runsc install`, `sudo systemctl restart docker`.
   - Check: `docker info --format '{{json .Runtimes}}'` lists `runsc`.
   - Clone the repo inside Ubuntu, copy `.env`, and start the worker there (step 6). The API can stay on Windows.
2. **`.env` additions** (both the API's and the worker's):
   ```
   SWF_PUBLIC=1
   SWF_CORS_ORIGINS=https://<project>.vercel.app
   SWF_SANDBOX_RUNTIME=runsc
   ```
   `SWF_PORTKEY_CONFIG` must be the `pc-…` slug (not `off`). The worker refuses to start in public mode without gVisor or the guardrails.
3. **Tunnel.** Install `cloudflared` (`winget install --id Cloudflare.cloudflared`), then:
   ```powershell
   cloudflared tunnel --url http://127.0.0.1:8000
   ```
   It prints `https://<random>.trycloudflare.com`. That URL changes on every start: update `VITE_API_BASE` on Vercel and redeploy each time. A stable URL needs a named tunnel on a domain you own (not $0).
4. **Vercel.** Import the Fixable repo; Root Directory `web/` (framework, build and output come from `web/vercel.json`). Environment variables (Production): `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_BASE` = the tunnel URL. Never set `VITE_AUTH_MODE`.
5. **Supabase and GitHub.** Supabase → Authentication → URL Configuration: Site URL `https://<project>.vercel.app`; Redirect URLs add `https://<project>.vercel.app/**` (keep the localhost entry for development). GitHub OAuth App: Homepage URL may stay as is; the callback stays the Supabase one.
6. **Check, then run.**
   ```powershell
   uv run --env-file .env python -m swf.deploycheck
   ```
   Every line must say PASS. Then start the API (Windows) and the worker (WSL Ubuntu) as in §4, then the tunnel.
7. **Keep-alive.** In the GitHub repo that hosts `.github/workflows/supabase-keepalive.yml`: Settings → Secrets and variables → Actions → add `SUPABASE_URL` and `SUPABASE_ANON_KEY` (public values). The workflow reads one row every three days so the Free project is not paused.

Limits in front of strangers (defaults; `SWF_*` overrides in `settings.py`): 5 tasks/day, $3/day, 1 running task per user, 120 API calls/min, 10 pre-flights/min, 4 open event streams per user and 40 in all. A provider HTTP 429 waits up to about 4 minutes before it counts as a failed model call.

