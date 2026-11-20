# M6 contract — GitHub clone, clean-clone apply, PR

- **Purpose:** The interface contract for the M6 build. The coder implements it, the tester tests it, the checker verifies both against it. If something here is wrong, report it; do not silently diverge.
- **Last updated:** 2026-11-21
- **Status:** Draft, awaiting user approval. Branch `feature/m6`, on top of `582ac1d` (M5 + its §12 amendments are the baseline).
- Sources: plan §8 (repository projection), §9 (data model), §18 M6; spec §4 item 2, §8 (token / push / PR rows), §9 steps 2 and 8, §10 criteria, §14 risks (hooks, token scope); research plan §6 items 1–2, §7.5, RQ10; m5-contract §0.8, §2, §6.

## Decisions from the user (2026-11-21)

1. Live test token: read at test time from `gh auth token`. It is never printed and never stored. The live test skips cleanly if `gh` is not installed, `gh auth token` fails, or `SWF_TEST_REPO` is unset.
2. Throwaway repo: `SWF_TEST_REPO=ShreyaasAI/swf-pr-sandbox` (public; created by the user). The value is `owner/name`. A full `https://github.com/owner/name(.git)` URL is normalised to `owner/name`, and nothing fancier is accepted.
3. OAuth token at rest: app-level Fernet, `github_tokens` table, service role only (RLS on, no policy, no grant). **This is already built in M1** (`swf.tokens.TokenCipher`, `store.store_github_token`, `store.get_encrypted_github_token`, migration `20261119000000_m1_init.sql`). M6 adds no migration and no new cipher. [A] The key variable stays `SWF_TOKEN_ENCRYPTION_KEY` (the M1 name, already in `Settings`, tests and `.env`) rather than `SWF_TOKEN_KEY`. If the user wants the rename, it is a one-line change in `settings.py` plus tests.
4. Never push to the project's own remote (`ShreyaasAI/Fixable`). Only the throwaway repo, and only in the live test. The live test fails hard (it does not skip) if `SWF_TEST_REPO` names `ShreyaasAI/Fixable`, case-insensitive.
5. Infrastructure $0. GitHub REST + git over HTTPS only. No GitHub App, no Actions.

## 0. Decisions and assumptions

1. [D] **Branch is computed server-side:** `branch_name(task_id: uuid.UUID) -> "agent/<task-id>"` (canonical lowercase UUID string). Nothing the agent, the description or the repo says can change it. The publisher takes no branch argument.
2. [D] **The token lives only in the environment of a backend git subprocess.** Git reads it through a `GIT_ASKPASS` helper script. The script's content is static and contains no secret. It prints `x-access-token` for a `Username` prompt and `$SWF_GIT_TOKEN` for anything else. The token never appears in argv, in a remote URL, in `.git/config`, in a file, in an event, in an error string or in the container. The REST client sends it only to the configured API host (as M1).
3. [D] **Every backend git call is hermetic.** The environment is built from scratch, never inherited: `PATH` (plus `SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `COMSPEC` on Windows only), `HOME=<private empty dir>`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<os.devnull>`, `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, and for network calls only `GIT_ASKPASS` + `SWF_GIT_TOKEN`. Every call gets these leading options: `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper= -c protocol.allow=never -c protocol.https.allow=always` (plus `-c protocol.file.allow=always` only when `GitOps(allow_file=True)`, which only tests set). Clones add `--template=<private empty dir> --no-tags --single-branch --branch <base> --no-recurse-submodules`. Commits add `--no-verify`, `-c commit.gpgsign=false`, and a fixed identity. Pushes add `--no-verify`. So no system config, global config, template hooks, credential helper or fsmonitor reaches a backend git process. Git LFS filters from global config are therefore off too: LFS files stay pointers [A].
4. [D] **The backend never runs git in the agent's workspace after the container starts.** `workspaces/<id>/repo/.git` is mounted and agent-writable, so it is treated as hostile. The backend runs git in `repo/` only for the clone and for one `rev-parse HEAD` right after it, both before the container exists.
5. [D] **The PR patch is the evidence diff, applied to a clean clone.** At PR time the patch is `evidence.pr_patch(harness.baseline_dir, ws.repo)`: the M5 diff (the start copy at S0 vs the workspace, pure Python, no git), with git-style headers. `confirm()` has already checked that the workspace digest equals the one the gate accepted, so this is exactly what the evidence showed. The clean clone is a fresh clone from GitHub into `workspaces/<id>/pr`, never a local clone of `repo/`. The flow is `git checkout -b agent/<id> <base_sha>`, then `git apply --check` and `git apply`, then `git add -A` (which honours the clone's `.gitignore`), then commit, then push. [A] Known limit: a change approved through the M5 backstop (rebaseline) became part of the start copy, so it is not in the PR. Plan §8 step 3 (`git diff --binary`) is replaced by this, as m5 §0.8 allowed.
6. [D] **Unsupported changes fail the PR, never get silently dropped.** `pr_patch` raises `UnsupportedChange(path, reason)` for a binary file, a symlink (added, removed or changed), a special file, or any path with a component that equals `.git` case-insensitively. Result: `failed("pr_failed: unsupported_change: <path>")`. [A] File modes are not carried: new files are `100644`, and an existing file keeps its mode.
7. [D] **There is no merge call.** No code path calls `PUT /repos/.../pulls/{n}/merge`, auto-merge, `merge_method` or `git merge`. The PR is opened ready for review. A human merges it in GitHub.
8. [D] **The PR body holds the evidence summary only, never command output.** Agent-produced output is untrusted (prompt injection, possible secrets) and would become public, so the body lists commands, exit codes, parsed failure ids and changed files only. Output guarding is M7.
9. [D] **Retries:** clone, push and create-PR are each tried up to `max_retries` (3) times, with the M4 backoff and a `retry {step, attempt, error}` event each time. Then the task fails with `clone_failed` or `pr_failed: push|create_pr`. There is no system question and no change to the m4 §2 transition table (it already has `confirming → opening_pr → completed`, and `opening_pr → failed`). The token is never retried: no token or an undecryptable one → `failed("github_token_missing" | "github_token_invalid")`; 401 from git or REST → `failed("github_token_invalid")`.
10. [D] **The PR is recorded before `completed`:** `store.record_pr(...)` writes `tasks.pr_url` and the event `pr_opened` in one transaction, then the task moves to `completed`. If the lease was lost after the PR opened, the PR is still recorded (a real side effect, as `add_cost` in M4). The status is still not written.
11. [D] **Opt-out:** `RunnerDeps.publisher: PullRequestPublisher | None = None`. `None` keeps the M5 behaviour (`confirming → completed`). The publisher is used only when a `TaskHarness` exists (the patch comes from it). `main()` wires the publisher whenever it wires `GitHubWorkspace`. M4 and M5 tests stay unchanged.
12. [D] Repo names are validated before any URL is built: `^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$`, and the name part may not be `.` or `..` (`InvalidRepoName`). The clone URL is `f"{clone_base}/{repo}.git"`, with `clone_base="https://github.com"` in production and `file://…` in tests.
13. [D] Every git error message is `redact(stderr_tail[-500:], token)` (each occurrence of the token becomes `***`) before it reaches an exception, an event or `tasks.error`.

## 1. Flow (additions marked +)

```
preparing: + GitHubWorkspace.prepare: token → clone repo/ (retry ≤3 → clone_failed) → base_sha; event repo_cloned
           start env → setup → baseline → S0 (unchanged)
investigating … verifying (unchanged)
confirming: harness.confirm() → passed:
   publisher None → completed (M5)
 + publisher set → opening_pr (phase "pr"):
      pr_patch(baseline_dir, repo) → UnsupportedChange → failed(pr_failed: unsupported_change: <path>)
      empty → failed(pr_failed: empty_diff)
      publisher.publish(...) → PublishError(stage) → failed(pr_failed: <stage>)
      record_pr (pr_url + event pr_opened) → completed
```

## 2. `swf.gitops` (new module)

- `class GitOpsError(Exception)`: `.stage: str`, message already redacted.
- `class InvalidRepoName(ValueError)`.
- `validate_repo_name(repo: str) -> str` (§0.12; returns it unchanged).
- `normalize_repo(value: str) -> str`: `owner/name`, or `https://github.com/owner/name` with an optional `.git` or trailing `/` → `owner/name`, then `validate_repo_name`. Anything else → `InvalidRepoName`. (Used by the live test; available to the API later.)
- `branch_name(task_id: uuid.UUID) -> str` (§0.1).
- `redact(text: str, secret: str | None) -> str` (§0.13).
- `askpass_script() -> Path`: writes the static helper once per process into a private `tempfile.mkdtemp()` dir (mode 0o700) and returns its path. POSIX `sh`; Git for Windows runs it through its shebang handling.
  ```sh
  #!/bin/sh
  case "$1" in Username*) printf '%s\n' x-access-token ;; *) printf '%s\n' "$SWF_GIT_TOKEN" ;; esac
  ```
- `class GitOps`:
  - `__init__(self, *, clone_base: str = "https://github.com", allow_file: bool = False, runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run, timeout_seconds: float = 600.0)`. Tests inject `runner` to record argv.
  - `env(self, token: str | None) -> dict[str, str]` (§0.3).
  - `remote_url(self, repo: str) -> str` (§0.12).
  - `clone(self, repo: str, branch: str, dest: Path, *, token: str) -> None`. `dest` must not exist. On failure it removes `dest` and raises `GitOpsError("clone")`.
  - `head(self, path: Path) -> str`: `rev-parse HEAD`.
  - `prepare_branch(self, path: Path, *, branch: str, base_sha: str | None) -> None`: `checkout -q -b <branch> <base_sha or HEAD>`.
  - `apply(self, path: Path, patch_file: Path) -> None`: `apply --check` then `apply`, then `add -A`. Failure → `GitOpsError("apply")`.
  - `has_staged_changes(self, path: Path) -> bool`: `diff --cached --quiet` (rc 1 → True).
  - `commit(self, path: Path, message: str) -> str`: author and committer are `SWF Agent <swf-agent@users.noreply.github.com>`, set through `-c user.name/-c user.email` and the `GIT_AUTHOR_*`/`GIT_COMMITTER_*` name/email env vars. Returns the commit sha.
  - `push(self, path: Path, branch: str, *, token: str) -> None`: `push --no-verify --porcelain origin refs/heads/<b>:refs/heads/<b>`. Never `--force`, never a `+` refspec, never another ref. A rejection → `GitOpsError("push")`.
  - Every call uses `runner([git, *common_opts, *args], cwd=…, env=self.env(…), capture_output=True, timeout=…, check=False)`. Nothing goes through a shell.

## 3. `swf.github` additions (REST)

- `class GitHubUnprocessable(GitHubError)` for 422 (for example "a pull request already exists").
- `@dataclass(frozen=True) class PullRequest: number: int; html_url: str`.
- `GitHubClient.create_pull(self, token: str, full_name: str, *, head: str, base: str, title: str, body: str) -> PullRequest`: `POST /repos/{full_name}/pulls` with `{"title", "head", "base", "body", "draft": false, "maintainer_can_modify": false}`. 201 → `PullRequest`; 401 → `GitHubAuthError`; 404 → `GitHubNotFound`; 422 → `GitHubUnprocessable`; other ≥ 400 → `GitHubError`. `html_url` must start with `https://github.com/` in production. Tests may set `GitHubClient(html_base=…)`.
- The default branch already comes from `get_repo(...).default_branch` (M2). No new method is needed.
- No other new endpoint. Specifically there is no merge, update, close or comment call.

## 4. `swf.pr` (new module): workspace, publisher, body

- `TokenSource = Callable[[store.Task], str]`. It raises `TokenUnavailable(code)` with `code ∈ {"github_token_missing", "github_token_invalid"}`.
- `db_token_source(db: Database, cipher: TokenCipher) -> TokenSource`: `store.get_task_github_token(conn, task.id)`, then `cipher.decrypt`. `None` → missing; `TokenDecryptError` → invalid.
- `class GitHubWorkspace` (implements `worker.WorkspaceProvider`):
  - `__init__(self, git: GitOps, tokens: TokenSource, workspaces_root: Path)`.
  - `prepare(self, task) -> TaskWorkspace`: `root = workspaces_root/<id>` (resolved). It removes a stale `root/repo`, calls `git.clone(task.repo, task.base_branch, root/"repo", token=tokens(task))`, then `base_sha = git.head(repo)`, and returns `TaskWorkspace(root, repo, base_sha=base_sha)`.
- `worker.TaskWorkspace` gains `base_sha: str | None = None` (`LocalCopyWorkspace` leaves it `None`).
- `@dataclass(frozen=True) class PrRequest: task_id: uuid.UUID; repo: str; base_branch: str; base_sha: str | None; root: Path; patch: str; title: str; body: str`.
- `@dataclass(frozen=True) class PrResult: url: str; number: int; branch: str; commit_sha: str`.
- `class PublishError(Exception)`: `.stage ∈ {"clone", "apply", "empty_diff", "push", "create_pr"}`.
- `class PullRequestPublisher(Protocol): def publish(self, req: PrRequest, task: store.Task) -> PrResult: ...`
- `class GitHubPublisher`:
  - `__init__(self, git: GitOps, api: GitHubClient, tokens: TokenSource, *, max_retries: int = 3, sleep: Callable[[float], None] = time.sleep, backoff: Sequence[float] = (1, 4, 10), on_retry: Callable[[str, int, str], None] | None = None)`.
  - `publish(req, task)`: (1) `token = tokens(task)`. (2) Remove `root/pr`, then clone (with retries). (3) `prepare_branch(pr, branch=branch_name(task_id), base_sha=…)`. (4) Write `root/pr.patch` (LF), then `apply`. (5) `has_staged_changes` False → `PublishError("empty_diff")`. (6) `commit(title + "\n\nSWF task " + id)`. (7) `push` (with retries). (8) `api.create_pull(head=branch, base=req.base_branch, …)` (with retries). 401 or `GitHubAuthError` at any step → re-raised as `GitHubAuthError` (the runner maps it to `github_token_invalid`). `GitHubUnprocessable` is not retried → `PublishError("create_pr")`. A push retry pushes the same local commit again (idempotent).
- `pr_title(task) -> str`: `f"SWF: fix {failing_test}"`, or `f"SWF: {first line of description}"`. One line, ≤ 72 chars, ellipsised.
- `render_pr_body(summary: PrSummary) -> str`, `≤ 60000` chars:
  - `PrSummary` fields: `task_id`, `description`, `failing_test`, `test_command`, `baseline`, `baseline_suite`, `post`, `suite`, `rerun` (each a `EvidenceRecord | None`), `files: list[str]`, `base_sha`.
  - Markdown sections, in order: a "What" heading followed by the description, cut to 2000 chars and quoted; then an "Evidence" table (step, command, exit code, failures ≤ 20 each; rows are baseline target, baseline suite, post-fix target, post-fix suite, harness re-run); then "Files changed" (≤ 100, then "… and N more"); then the footer `Opened by SWF for task <id> on base <sha[:12]>. The agent never merges; review and merge in GitHub.`
  - No command output anywhere (§0.8). Built by the runner from `TaskHarness` state (`baseline`, `baseline_suite`, the latest `post` and `suite` runs at the checked digest, and the rerun record). `TaskHarness` gains `rerun: EvidenceRecord | None`, set in `confirm()`.

## 5. `swf.evidence` additions

- `class UnsupportedChange(Exception)`: `.path`, `.reason ∈ {"binary", "symlink", "special", "git_dir"}`.
- `pr_patch(base: Path, repo: Path) -> str`: over the paths of `diff_snapshots(take_snapshot(base), take_snapshot(repo))`, sorted, it applies §0.6 first. Each text file then gets a `diff --git a/<p> b/<p>` header, `new file mode 100644` or `deleted file mode 100644` when it applies, `--- a/<p>`|`/dev/null`, `+++ b/<p>`|`/dev/null`, and the difflib hunks with the same `\ No newline at end of file` handling as `unified_diff`. An added or removed empty file is header-only, so `git apply` accepts it. Nothing changed → `""`. Paths are POSIX. A path with a space or a non-ASCII character is written as-is [A]: git apply accepts unquoted paths without tabs or newlines, and a path containing a tab or newline → `UnsupportedChange(reason="special")`.
- `unified_diff` and the M5 `diff` row are unchanged.

## 6. `swf.store` additions (no migration)

- `get_task_github_token(conn, task_id) -> str | None`: the encrypted blob for the task owner (`tasks join github_tokens using (user_id)`). Backend role only.
- `record_pr(conn, task_id, *, url: str, number: int, branch: str, commit_sha: str) -> None`: sets `tasks.pr_url = url` and `updated_at`, and appends the event `pr_opened {url, number, branch, commit_sha}` in the same transaction, plus an `audit_log` row `pr.open` (target = task id). It refuses (`ValueError`) if `pr_url` is already set to a different URL.
- `tasks.pr_url` (M1 column) is already returned by `GET /v1/tasks/{id}`. No API change.

## 7. `swf.worker` changes

- `RunnerDeps.publisher: PullRequestPublisher | None = None` (§0.11).
- `_run`: `self.workspace = ws = self._prepare_workspace()`:
  - It calls `deps.workspaces.prepare(task)` up to `max_retries` times, retrying on `GitOpsError` (event `retry {step: "clone"}`, backoff).
  - When retries run out → `failed("clone_failed")`. `TokenUnavailable` → `failed(code)`; `GitHubAuthError` → `failed("github_token_invalid")`. In every failure case no container is started.
  - If `ws.base_sha` is set → event `repo_cloned {repo, base_branch, base_sha}`.
- `_confirm`: on `passed`:
  - If `deps.publisher` is None or there is no harness → `completed` (unchanged).
  - Otherwise `transition("opening_pr", phase="pr")`, then `_open_pr(harness)` (§1). The patch comes from `pr_patch(harness.baseline_dir, ws.repo)`, the summary from harness state, and the title from `pr_title`. On success: `with db.backend(): store.record_pr(...)`, then `transition("completed")`.
  - Error strings: `pr_failed: <stage>` and `pr_failed: unsupported_change: <path>` (path ≤ 200 chars).
- `EnvRequest` is unchanged and carries no token.
- `main()`:
  - With `SWF_LOCAL_SOURCE` set, it keeps `LocalCopyWorkspace` and runs with no publisher (dev).
  - Otherwise it builds `cipher = TokenCipher(settings.token_encryption_key)`, `tokens = db_token_source(db, cipher)`, `git = GitOps()`, `api = GitHubClient(base_url=settings.github_api_url)`, `GitHubWorkspace(git, tokens, root)` and `GitHubPublisher(git, api, tokens)`.
  - The `SWF_LOCAL_SOURCE`-required error is removed.

## 8. Tests (tester). Fakes in `tests/m6_fakes.py` (reuses `m4_fakes`, `m5_fakes`)

- `SENTINEL_TOKEN = "ghs_SWFSENTINEL" + 30 hex chars`. It is used as the token everywhere outside the live test.
- `bare_remote(tmp_path, files: Mapping[str, str] | seed: str, branch="main") -> tuple[str, Path]`: creates a non-bare repo, commits, and clones `--bare`. Returns the `file://` URL base (for `GitOps(clone_base=…, allow_file=True)`, laid out as `<base>/<owner>/<name>.git`) and the bare path.
- `FakeGitHub`: an `httpx.MockTransport` that records `(method, path, json)` and returns a scripted response per call (default: `POST /repos/{r}/pulls` → 201 `{number: 1, html_url: "https://github.com/<r>/pull/1"}`).
- `plant_hook_traps(tmp_path, monkeypatch) -> Path`. It returns a markers dir, after setting `GIT_CONFIG_GLOBAL` to a config with `core.hooksPath=<trap>`, `core.fsmonitor=<trap script>`, `init.templateDir=<trap template>` and `credential.helper=<trap>`. It also sets `GIT_TEMPLATE_DIR=<trap template>`, `GIT_CONFIG_SYSTEM`, and `XDG_CONFIG_HOME`/`HOME` to trap dirs. Each trap hook writes `<markers>/<hook-name>`. The hooks are post-checkout, pre-commit, prepare-commit-msg, commit-msg, post-commit, pre-push, reference-transaction, post-index-change and post-merge.
- `plant_workspace_traps(repo: Path, markers: Path)`: writes `repo/.git/hooks/*` (the same hooks), and sets `repo/.git/config` with `core.hooksPath`, `core.fsmonitor`, `credential.helper` and `remote.origin.pushurl` pointing at traps.
- `scan_for(root: Path, needle: bytes) -> list[Path]`: every file under `root` (symlinks not followed) that contains `needle`.

| File | DoD item | Must cover |
|---|---|---|
| `test_m6_gitops.py` | no token; hooks never run | `env()` exact key set (no inherited var, token only with network calls); argv for clone/commit/push has the §0.3 options, no `--force`, no `+`, refspec `refs/heads/agent/<id>` only, token never in argv; askpass script prints `x-access-token` for `Username for 'https://github.com':` and the env token otherwise; `clone` leaves no token in `repo/.git/config`, the remote URL has no userinfo, `scan_for(root, token)` is empty; `redact`; `validate_repo_name` and `normalize_repo` tables (accept `ShreyaasAI/swf-pr-sandbox`, `https://github.com/ShreyaasAI/swf-pr-sandbox.git`; reject `../x`, `a/..`, `a/b/c`, `a/b?x`, `http://evil/a/b`, `git@github.com:a/b`); `branch_name` |
| `test_m6_hooks.py` | **hooks never run** | with `plant_hook_traps`: clone + publish (bare remote + FakeGitHub) → `markers` empty; with `plant_workspace_traps` on `repo/` before publish → `markers` empty and the push went to the bare remote (not the trap `pushurl`); a patch that adds `.githooks/pre-commit` plus `.gitattributes` with `* filter=evil` publishes as plain files and no marker appears; a template hook fires when the same git commands run **without** `GitOps` (proves the trap works) |
| `test_m6_patch.py` | clean-clone apply | `pr_patch` round trip: modified / added / deleted / empty added / empty removed / no trailing newline / nested dirs → `git apply` in a fresh clone of the base reproduces the repo tree byte-for-byte; binary, symlink, FIFO (POSIX only) and tab-in-name → `UnsupportedChange` with the right reason; unchanged → `""`; `.git` content changes are not in the patch |
| `test_m6_publish.py` | **branch fixed server-side**; **no merge call** | `GitHubPublisher.publish` → the bare remote has `refs/heads/agent/<id>` whose parent is `base_sha`, whose author/committer is the fixed identity and whose tree equals base + patch; the base branch sha is unchanged; no other ref is created; a description and patch containing `agent/evil`, `main` or `refs/heads/main` change nothing; the PR request has `head == "agent/<id>"`, `base == task.base_branch`, `draft == false`; all recorded requests are `POST /repos/<r>/pulls` only; an existing remote `agent/<id>` at another commit → `PublishError("push")` after 3 attempts with `on_retry` calls; create_pull 502, 502, 201 → success; 401 → `GitHubAuthError`; 422 → `PublishError("create_pr")` with no retry; an empty patch → `empty_diff`; a base that diverged after the clone still yields a branch on `base_sha` |
| `test_m6_body.py` | evidence in PR | `pr_title` rules and the 72-char cap; `render_pr_body` has the sections, exit codes, failure ids, the file list cap and the footer; it never contains output text (a record whose `output` holds `SECRET_OUTPUT` → absent); length ≤ 60000 with a huge description |
| `test_m6_no_merge.py` | **no merge call** | static: no file under `src/swf/` except `policy.py` (whose classifier legitimately lists `git merge` as forbidden) matches `/merge\b`, `merge_method`, `auto_?merge`, `enablePullRequestAutoMerge`, or a `"merge"` string literal; `GitHubClient` public methods are exactly the known set (`get_user, list_repos, get_repo, branch_exists, get_file, create_pull`) |
| `test_m6_store.py` (DB) | link recorded | `record_pr` sets `pr_url`, adds the event `pr_opened` and the audit row; a different second URL → `ValueError`; `get_task_github_token` returns the owner's blob and `None` without a token; RLS: user B cannot see A's `pr_url` through `as_user`; `GET /v1/tasks/{id}` shows `pr_url` |
| `test_m6_worker.py` (DB, fakes) | all | rig = the M5 worker rig with `GitHubWorkspace(GitOps(allow_file=True, clone_base=…), lambda t: SENTINEL_TOKEN, …)` + `GitHubPublisher` + FakeGitHub + `FakeEnv` over `ws.repo`. Happy path: statuses `… confirming → opening_pr → completed`, events `repo_cloned` and `pr_opened`, `tasks.pr_url` set, the agent branch on the bare remote holds the fix; the captured `EnvRequest` and `scan_for(workspaces_root, token)` contain no token. Failures: token missing → `failed("github_token_missing")` and no `env_factory` call; clone fails 3× → `clone_failed` with 3 `retry {step: clone}` events; push rejected → `pr_failed: push`; a binary file written in execute → `pr_failed: unsupported_change: <path>`; a harness mismatch → no push and no API call; `publisher=None` → `completed` with no PR (M5 path) |
| `test_m6_e2e.py` (`docker`, `network`, DB) | **no token in the workspace** | seed `01-paginate-off-by-one` from a `file://` bare remote, the real sandbox, `reference_steps` plus one approved bash step in investigate, `env; cat /repo/.git/config; grep -rl ghs_ /repo \|\| true` (read-only); → `completed`; that step's output has no `SENTINEL_TOKEN` and no `ghs_` match; the bare remote's `agent/<id>` diff vs base touches only the reference file(s); `pr_opened` recorded; 0 Docker leftovers |
| `test_m6_live_pr.py` (`live`, `network`, DB) | **PR on a test repo**; DoD | see below |

**Live test (`test_m6_live_pr.py`):**
- Skips with a reason when any of these hold: `shutil.which("gh")` is None; `gh auth token` exits non-zero or prints nothing; `SWF_TEST_REPO` is unset; the repo is empty (`size_kb == 0`; "add a README"); `can_push` is false.
- Fails if `normalize_repo(SWF_TEST_REPO)` is `shreyaasai/fixable` (case-insensitive).
- The token is held in a local variable only. It is never in an assertion message, `print`, log or file, and the test asserts `scan_for(tmp_path, token)` is empty at the end.
- Flow:
  1. Create a DB task row for the repo and its default branch (`get_repo`).
  2. Run `GitHubWorkspace.prepare` with a token source that returns the gh token.
  3. `copy_tree(repo, root/"baseline")`, then write `swf-live/<task-id>.txt` into `repo/`.
  4. `patch = pr_patch(...)`, then `GitHubPublisher(GitOps(), GitHubClient(), …).publish(...)`.
  5. `store.record_pr(...)`.
- Asserts:
  - `html_url` matches `^https://github\.com/ShreyaasAI/swf-pr-sandbox/pull/\d+$` (or the given repo).
  - `tasks.pr_url` equals it, and an event `pr_opened` exists.
  - A `GET /repos/<r>/pulls/<n>` made from the test (not from `src`) shows `head.ref == "agent/<id>"`, `state == "open"` and `merged == false`.
- The PR and branch are left open as DoD evidence. The user closes them.
- Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -m live tests/test_m6_live_pr.py -rs`.

- Everything else: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -m "not live" tests/test_m6_*.py`. M4 + M5 + the full suite stay green.
- `pyproject.toml`: the tester may reword the `live` marker to "calls external services (models or GitHub)". No new marker.
- Windows dev host: hook and askpass tests must pass under Git for Windows 2.52. The FIFO case is POSIX-only (`skipif`).

## 9. What proves M6 done (DoD)

1. `test_m6_hooks.py` is green: no marker file appears from global, template or workspace-planted hooks, fsmonitor or a credential helper.
2. `test_m6_publish.py` is green: the branch is always `agent/<task-id>`; only that ref is pushed, without force.
3. `test_m6_gitops.py`, `test_m6_worker.py` and `test_m6_e2e.py` are green: the token is absent from `.git/config`, argv, the container env, workspace files and errors.
4. `test_m6_no_merge.py` is green.
5. The live test passed once: the PR URL on `ShreyaasAI/swf-pr-sandbox`, `tasks.pr_url` and the `pr_opened` event are pasted into the close-out.
6. ruff, ruff format check and mypy strict are clean; the full non-live suite is green; 0 Docker leftovers.

## 10. File ownership

- **Coder:** `src/swf/gitops.py` (new), `src/swf/pr.py` (new), `src/swf/github.py`, `src/swf/evidence.py` (`pr_patch`, `UnsupportedChange`, `TaskHarness.rerun`), `src/swf/store.py`, `src/swf/worker.py`. No migration. Not `policy.py`, `sandbox.py`, `agent/*`, any test.
- **Tester:** `tests/m6_fakes.py`, `tests/test_m6_*.py` (new); `pyproject.toml` marker text only.
- **Checker:** owns nothing. Reports deviations; runs ruff, format, mypy strict, M4 + M5 + M6 suites, and a mutation pass on the hermetic env (drop `GIT_CONFIG_GLOBAL`, drop `core.hooksPath`, inherit `os.environ`, use the agent branch from input, add `--force`). Each mutant must fail a test.
- **Planner:** `docs/` (this file; plan §18 M6 status and HANDOVER at close).
- Rules as m4 §11: own worktree; **no push** of this project; LF endings (`newline="\n"`); no `uv add` (httpx, cryptography are already deps); Docker names `swf-test-*` / `swf-coder-*`.

## 11. Deferred (needs a user OK, since plan §18 had parked these on M6)

1. `DELETE /v1/tasks/{id}` + workspace removal (M1 deferral list said M6) → **M10** (UI). It is not in the M6 DoD, and workspaces are kept until deletion, per plan §8.5.
2. Linux workspace ownership (chown for uid 1000; M2 deferral said "M6 clone") → **M9** (Oracle VM). It is not needed on Docker Desktop, and it needs a root or user-namespace decision on the VM.
3. A `can_push` check at task creation (pre-flight) → M8. In M6, no push access → `pr_failed: push`.
4. Fork-based PRs, PR updates after review comments, draft PRs, closing or deleting the agent branch → post-MVP.
5. Carrying file modes, binary files and symlinks into the PR → post-MVP (they fail visibly now, §0.6).
