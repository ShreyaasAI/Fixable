# M10 runbook: run the web UI on localhost

- **Purpose:** One-time setup you do in the GitHub, Supabase and Portkey dashboards, then the commands that start SWF on your PC with the web UI.
- **Last updated:** 2026-11-24
- **Sources:** m10a-contract §0 (decisions 2, 3, 14), §1.8, §5.

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

Docker Desktop must be running. Use three terminals, all in the repo root:

```powershell
uv run uvicorn swf.api.main:create_app_from_env --factory --workers 1
```

```powershell
uv run python -m swf.worker
```

```powershell
cd web; npm ci; npm run dev
```

Open `http://localhost:5173` and sign in with GitHub. The Vite dev server proxies `/v1` to `127.0.0.1:8000`; set `SWF_API_PROXY` to use another API address.

## 5. Tests

| What | Command |
|---|---|
| Non-live suite (no browser) | `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m "not live and not e2e"` |
| Chromium for e2e (once) | `uv run playwright install chromium` |
| E2E (Docker, npm, Chromium) | `PYTHONDONTWRITEBYTECODE=1 uv run pytest -o addopts="" -q -p no:cacheprovider -m e2e tests/e2e tests/test_m10_web_build.py` |
| Web typecheck and build | `cd web; npm run typecheck; npm run build` |

The e2e stack writes its logs to `<pytest tmp>/…/logs/{api,worker,vite}.log`. Set `SWF_E2E_HEADED=1` to watch the browser.
