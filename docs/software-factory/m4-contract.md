# M4 contract — WebAgent, tools, approvals, model switch, worker

- **Purpose:** The interface contract for the parallel M4 build. The coder implements it, the tester tests it, the checker verifies both against it. If something here is wrong, report it; do not silently diverge.
- **Last updated:** 2026-11-20
- **Status:** Working contract for M4 (worktree `m2-sandbox-preflight-1120a`, on top of `1296b28`)
- Verified against `mini-swe-agent==2.4.6` (`.venv/Lib/site-packages/minisweagent`): `DefaultAgent(model, env, *, config_class=AgentConfig, **kwargs)`; `run(task, **kw) -> dict` loops `step()` and catches `FormatError`/`InterruptAgentFlow` (adds `e.messages`) and stops when `messages[-1]["role"] == "exit"`; other exceptions → `handle_uncaught_exception` + re-raise; `step() = execute_actions(query())`; `query()` checks `step_limit`/`cost_limit`/`wall_time_limit_seconds` against `self._start_time`; `add_messages(*messages) -> list[dict]`; `execute_actions(message) -> list[dict]`. `LitellmModel._query` hard-codes `tools=[BASH_TOOL]`; `format_toolcall_observation_messages(actions=…, outputs=…, observation_template=…, template_vars=…)` needs only `tool_call_id` per action. `DockerEnvironment.execute(action, cwd="", *, timeout=None)` calls `_check_finished`, which raises `Submitted` on a `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` first line. `minisweagent/__init__` reads `MSWEA_GLOBAL_CONFIG_DIR`, `mkdir`s it and `load_dotenv`s its `.env` **at import**. Upstream retry = tenacity, `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT` (default 10).

## 0. Decisions and assumptions

1. [D user 2026-11-20] Idle-approval/question timeout default **24 h** (`approval_timeout_seconds=86400`), configurable; on expiry the approval/question is `expired` and the task → `failed` (`error="approval_timeout"` / `"question_timeout"`).
2. [D] Retries ≤ 3 per failing step, then a **system `ask_user`**: answer `retry` (case-insensitive, trimmed) → 3 more attempts; any other answer → `failed` (`error="retries_exhausted"`). "Step" = container start, each AGENTS.md `setup` command, a model query that raises, a `run_tests` infra failure (returncode −1 / `exception_info` set, e.g. timeout). A test that runs and fails is **not** a retryable failure.
3. [D] Models (plan §5): investigate/plan → `opencode_go/beam-free`, execute/verify → `zai/glm-4.7-flash`, through Portkey's OpenAI-compatible gateway using **litellm** (`openai/<wire model>`). `portkey-ai` is not installed and is not added. [A] The wire model string is `"@" + id` (Portkey Model Catalog), overridable with `SWF_MODEL_INVESTIGATE` / `SWF_MODEL_EXECUTE`. The live smoke test settles it. `cost_tracking="ignore_errors"`: free models have no litellm price, so cost = 0 (RQ2 records this).
4. [A, needs user OK] New backend tool **`start_verify`** (execute only). Plan §12 has no way for the agent to move execute → verify. `policy.TOOL_PHASES["start_verify"] = {"execute"}`.
5. [D] `record_evidence` is an M4 stub: it emits an event and returns `recorded`. `complete_task` goes through an `EvidenceGate` whose M4 default accepts everything; M5 replaces it. After `complete_task`: `verifying → confirming → completed` (M5 inserts the harness re-run in `confirming`; M6 inserts `opening_pr`).
6. [D] Retry layering: `swf._mswea` sets `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1`, so WebAgent's own 3-attempt loop (injected sleep, backoff 5 s, 15 s) is the only model retry.
7. [D] `SandboxEnv._check_finished` becomes a no-op. Only `complete_task` can finish a task; otherwise `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` would let the agent forge a submission.
8. [A] AGENTS.md `setup` commands are classified with `phase="prepare"` and the file's `allow` patterns: `forbid` → `failed` (`error="setup_forbidden"`); `approve`/`allow` run unattended (the repo owner wrote them; the sandbox is the boundary).
9. [D] File tools run **in the container**: `docker exec -i` with argv and stdin, no shell string. This avoids quoting bugs, the Windows 32 K command-line cap, and host-side symlink escapes.
10. [D] Waits do not count toward `wall_time_limit_seconds` (default 3600 = `Limits.time_limit_minutes*60`): the agent shifts `_start_time` by the time spent waiting.
11. [A] One worker process per host database, so at worker start every task in `ACTIVE_STATUSES` is orphaned. `worker_id` is recorded for later multi-worker reconciliation.
12. [D] Admission is authoritative in the worker claim: one transaction holds `pg_advisory_xact_lock(hashtext('swf.admission'))` and takes the FIFO head with `FOR UPDATE SKIP LOCKED`. If the head does not fit, nothing is admitted (no skipping, so no starvation). The pre-flight decision stays advisory.
13. [D] SSE polls the `events` table (no LISTEN/NOTIFY). [A] M10's browser client uses fetch-streaming with the `Authorization` header (EventSource cannot set headers).
14. [D] M4 has no GitHub clone (M6). The worker takes a `WorkspaceProvider`; M4 ships `LocalCopyWorkspace` (copies a directory, used by the smoke test).

## 1. `swf._mswea` (import-order guard; M2 open risk)

- `ISOLATED_DIR_DEFAULT = Path(tempfile.gettempdir()) / "swf-mswea-empty"`.
- `isolate(config_dir: Path | None = None) -> Path`: dir = `config_dir` or `$SWF_MSWEA_CONFIG_DIR` or the default; `mkdir(parents=True, exist_ok=True)`. If `dir/.env` exists → `RuntimeError`. Then **always overwrite** `os.environ`: `MSWEA_GLOBAL_CONFIG_DIR=<dir>`, `MSWEA_SILENT_STARTUP=1`, `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1`. If `minisweagent` is already in `sys.modules` and `minisweagent.global_config_dir != dir` → `RuntimeError`. Idempotent; returns dir.
- `src/swf/__init__.py` calls `_mswea.isolate()`, so any `import swf.*` runs it before `swf.sandbox`/`swf.agent` import minisweagent. `swf.worker.main()` calls it again and asserts.

## 2. `swf.states` (pure)

- `PHASE_STATUS: dict[str, str] = {"prepare":"preparing","investigate":"investigating","plan":"awaiting_plan_approval","execute":"executing","verify":"verifying","confirm":"confirming","pr":"opening_pr"}`.
- `WAITING_STATUSES = frozenset({"awaiting_plan_approval","awaiting_command_approval","awaiting_input"})`.
- `TRANSITIONS: dict[str, frozenset[str]]` (`X` = `failed, cancelled, interrupted`):

| From | To |
|---|---|
| created | queued, rejected, cancelled |
| queued | preparing, cancelled |
| preparing | investigating, awaiting_input, X |
| investigating | awaiting_plan_approval, awaiting_command_approval, awaiting_input, X |
| awaiting_plan_approval | executing (approved), investigating (rejected), X |
| executing | awaiting_command_approval, awaiting_input, verifying, X |
| awaiting_command_approval | investigating, executing, verifying, X |
| awaiting_input | preparing, investigating, executing, verifying, X |
| verifying | awaiting_command_approval, awaiting_input, confirming, X |
| confirming | opening_pr, completed, X |
| opening_pr | completed, X |
| completed, rejected, failed, interrupted, cancelled | — (terminal) |

  A wait always returns to `PHASE_STATUS[current phase]` (runner invariant, tested).
- `class InvalidTransition(Exception)`, `class TaskFinished(Exception)` (the row is already terminal, e.g. cancelled or interrupted concurrently).
- `check_transition(from_status: str, to_status: str) -> None` raises `TaskFinished` if `from` is terminal, else `InvalidTransition` if `to ∉ TRANSITIONS[from]`.

## 3. Migration `supabase/migrations/20261121000000_m4_agent.sql`

```sql
-- M4: worker claim, task events sequence, expiring approvals/questions (m4-contract §3).
alter table public.tasks
  add column error       text check (error is null or length(error) <= 10000),
  add column worker_id   text,
  add column claimed_at  timestamptz,
  add column finished_at timestamptz,
  add column event_seq   integer not null default 0 check (event_seq >= 0);
create index tasks_queued_idx on public.tasks (created_seq) where status = 'queued';

alter table public.approvals drop constraint approvals_status_check;
alter table public.approvals add constraint approvals_status_check
  check (status in ('pending', 'approved', 'rejected', 'expired'));
create unique index approvals_one_pending_idx on public.approvals (task_id) where status = 'pending';

alter table public.questions
  add column source     text not null default 'agent' check (source in ('agent', 'system')),
  add column expired_at timestamptz,
  add constraint questions_answer_or_expiry check (not (answer is not null and expired_at is not null));
create unique index questions_one_open_idx on public.questions (task_id)
  where answer is null and expired_at is null;
```
The existing grants and RLS cover the new columns (authenticated: SELECT own rows; backend: writes). `events` (M1) is unchanged: `unique (task_id, seq)` already exists, and `seq` comes from `tasks.event_seq` (the row lock serialises writers).

## 4. `swf.store` additions and changes

- `Task` gains `error: str | None`, `worker_id: str | None`, `claimed_at`, `finished_at: datetime | None` (`event_seq` stays internal). `Approval.status` may be `expired`. `Question` gains `source: str`, `expired_at: datetime | None`.
- `class Event(BaseModel): seq: int; type: str; payload: dict[str, Any]; created_at: datetime`.
- `append_event(conn, task_id, type: str, payload: dict[str, Any]) -> Event`: `update tasks set event_seq = event_seq + 1 … returning event_seq - 1` → insert with that seq (0-based, gap-free per task).
- `list_events(conn, user_id, task_id, *, after_seq: int = -1, limit: int = 500) -> list[Event]` (as_user; filters `user_id` too; ordered by seq).
- `transition_task(conn, task_id, to_status: str, *, phase: str | None = None, error: str | None = None) -> Task`: `select … for update`, `states.check_transition`, update status (and phase if given, and error if given; `finished_at = now()` if terminal), then `append_event("status", {"from", "to", "phase", "error"})`.
- `create_task`: also appends `status {"from": None, "to": <status>}`. `cancel_task`: goes through `check_transition`, expires pending approvals/open questions, emits `status`. `decide_approval` emits `approval_decided {approval_id, kind, decision, comment}`; `answer_question` emits `question_answered {question_id}`; a decision/answer on an `expired` row → `Conflict("already_decided")`/`Conflict("already_answered")`.
- Worker side (backend): `create_approval` (unchanged signature) emits `approval_requested {approval_id, kind, payload}`; `create_question(conn, task_id, question, *, source: Literal["agent","system"] = "agent")` emits `question_asked {question_id, question, source}`; `get_approval(conn, approval_id) -> Approval | None`; `get_question(conn, question_id) -> Question | None`; `expire_approval(conn, approval_id) -> None` (`status='expired', decided_at=now()`, event `approval_expired`); `expire_question(conn, question_id) -> None` (event `question_expired`); `add_task_cost(conn, task_id, usd: float) -> None` (tasks.cost_usd and quotas_usage.cost_usd for the task's `created_at` day).
- `claim_next_task(conn, *, worker_id: str, limits: Limits = Limits()) -> Task | None`: §8 algorithm.
- `interrupt_orphans(conn, *, reason: str = "worker_restarted") -> list[uuid.UUID]`: every task in `ACTIVE_STATUSES` → `interrupted` (`error=reason`) via `transition_task`; their pending approvals/open questions are expired.
- `active_task_ids(conn) -> list[str]`.
- `swf.preflight.fits(active: Sequence[Reservation], r: Reservation, limits: Limits = Limits()) -> bool`: the same rule as `evaluate` step 5. `evaluate` uses it.

## 5. `swf.agent` package (`tools.py`, `model.py`, `channel.py`, `web_agent.py`, `prompts.py`; `__init__` re-exports)

### 5.1 Tools (`swf.agent.tools`)
- Action shape (produced by `PhaseModel`, consumed by `WebAgent`): `{"tool": str, "args": dict[str, Any], "tool_call_id": str}`; bash actions also carry `"command": str`.
- `TOOL_SCHEMAS: dict[str, dict]` (OpenAI `{"type":"function","function":{name, description, parameters}}`); `tool_schemas(phase: str) -> list[dict]` = the tools `policy.check_tool` allows in `phase`, in `TOOL_SCHEMAS` order.

| Tool | Required args (JSON schema, `additionalProperties: false`) | Optional | Runs |
|---|---|---|---|
| bash | `command: string (1..20000)` | `timeout_seconds: int 1..600` | container, classifier |
| read_file | `path: string` | `start_line: int ≥1`, `end_line: int ≥1` | container |
| list_dir | — | `path: string` (default `.`) | container |
| search | `pattern: string (1..500)` | `path: string`, `glob: string` | container (`grep -rnE`, ≤ 200 lines) |
| edit_file | `path`, `old: string (≥1)`, `new: string` | `replace_all: bool` | container; `old` must match exactly once unless `replace_all` |
| write_file | `path`, `content: string (≤ 1 MiB UTF-8)` | — | container |
| run_tests | — | `selector: string` matching `^[A-Za-z0-9_./:\[\]=-]{1,300}$`, appended shell-quoted | container (AGENTS.md `test`) |
| submit_plan | `summary (1..2000)`, `root_cause (1..2000)`, `steps: string[1..20]`, `files: string[0..50]`, `test_plan (1..2000)` | — | backend → plan approval |
| ask_user | `question: string (1..2000)` | — | backend → question |
| start_verify | `summary: string (1..2000)` | — | backend → phase verify |
| record_evidence | `kind: enum[baseline,post,suite,harness_rerun,diff]`, `note: string` | `command: string` | backend (M4 stub) |
| complete_task | `summary: string (1..4000)` | — | backend → EvidenceGate |

- `class Plan(BaseModel, extra="forbid")` with the submit_plan fields above.
- `class ToolError(Exception)` (message becomes the observation with returncode 1).
- `resolve_repo_path(path: str) -> str`: relative or `/repo/…`, POSIX-normalised; NUL, empty, or escaping `/repo` (`..`) → `ToolError`; returns an absolute `/repo/...`.
- `@runtime_checkable class ToolEnv(Protocol)`: `execute(action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]`; `read_bytes(path: str) -> bytes`; `write_bytes(path: str, data: bytes) -> None`; `list_dir(path: str) -> list[str]` (names, dirs suffixed `/`); `search(pattern: str, path: str, glob: str | None, max_results: int) -> list[str]`; `cleanup() -> None`; `get_template_vars(**kw) -> dict`; `serialize() -> dict`. Paths passed in are already resolved. Missing file → `ToolError`.
- `run_tool(env: ToolEnv, action: dict, *, test_command: str | None, output_limit: int) -> dict` for the container tools other than bash: returns `{"output", "returncode", "exception_info"}`. `read_file` caps at 256 KiB, with line numbers; `run_tests` with no test command → `ToolError("no test command configured in AGENTS.md")`.

### 5.2 `PhaseModel` (`swf.agent.model`)
- `DEFAULT_PHASE_MODELS = {"investigate": "@opencode_go/beam-free", "plan": "@opencode_go/beam-free", "execute": "@zai/glm-4.7-flash", "verify": "@zai/glm-4.7-flash"}`; `PORTKEY_BASE_URL = "https://api.portkey.ai/v1"`.
- `class PhaseModelConfig(LitellmModelConfig)`: `phase_models: dict[str, str] = DEFAULT_PHASE_MODELS`, `api_base: str = PORTKEY_BASE_URL`, `api_key_env: str = "PORTKEY_API_KEY"`, `cost_tracking = "ignore_errors"`; `model_name` defaults to the investigate model.
- `class MissingApiKey(Exception)`.
- `class PhaseModel(LitellmModel)`: `__init__(self, *, completion: Callable[..., Any] = litellm.completion, env: Mapping[str, str] | None = None, **kwargs)`; reads the key at init (missing → `MissingApiKey`) and **never stores it in `config`** (so `serialize()` and the trajectory stay key-free). `phase: str` (initially `"investigate"`); `set_phase(phase) -> None`; `current_model -> str`. `_query` calls `completion(model="openai/"+current_model, messages=…, tools=tool_schemas(phase), api_base=…, api_key=key, extra_headers={"x-portkey-api-key": key}, **model_kwargs)`. `_parse_actions` → action dicts (§5.1); no tool calls, an unknown tool, or non-object/invalid JSON args → `FormatError` (upstream message shape). `query()` adds `extra["model"]` and `extra["phase"]`. `from_env(env=None) -> PhaseModel` applies the `SWF_MODEL_*` overrides.
- `class AgentModel(Protocol)`: minisweagent `Model` + `set_phase(phase: str) -> None`.

### 5.3 Channel (`swf.agent.channel`)
- `@dataclass(frozen=True) ApprovalOutcome(status: Literal["approved","rejected","expired"], comment: str | None)`.
- `class TaskChannel(Protocol)`: `task_id: uuid.UUID`; `status() -> str`; `transition(to_status: str, *, phase: str | None = None, error: str | None = None) -> None` (raises `states.TaskFinished`/`InvalidTransition`); `emit(type: str, payload: dict[str, Any]) -> None`; `request_approval(kind: str, payload: dict) -> uuid.UUID`; `approval(approval_id) -> ApprovalOutcome | None` (None = pending); `expire_approval(approval_id) -> None`; `ask(question: str, *, source: Literal["agent","system"]) -> uuid.UUID`; `answer(question_id) -> str | None`; `expire_question(question_id) -> None`; `add_cost(usd: float) -> None`.
- `class PgTaskChannel(TaskChannel)`: `__init__(self, db: Database, task_id: uuid.UUID)`; one `db.backend()` transaction per call, over the §4 store functions.
- `wait_for(poll: Callable[[], T | None], *, channel, timeout_seconds: float, poll_seconds: float, clock: Callable[[], float], sleep: Callable[[float], None], on_timeout: Callable[[], None]) -> T`: each round, a terminal `channel.status()` → raises `TaskStopped`; a non-None `poll()` → return it; past the deadline → `on_timeout()` and raise `WaitTimedOut`; else `sleep(poll_seconds)`.

### 5.4 `WebAgent` (`swf.agent.web_agent`)
- Exit exceptions (all `InterruptAgentFlow`; each carries one `{"role":"exit","content":…,"extra":{"exit_status":…,"submission":…}}`): `TaskSubmitted` ("Submitted"), `TaskStopped` ("Stopped"), `WaitTimedOut` ("ApprovalTimeout" or "QuestionTimeout"), `RetriesExhausted` ("RetriesExhausted"), `SetupForbidden` ("SetupForbidden").
- `class EvidenceGate(Protocol)`: `check(channel: TaskChannel) -> str | None` (None = complete; else the refusal text for the observation). `AcceptAllEvidence` = M4 default.
- `class WebAgentConfig(AgentConfig)`: `system_template`/`instance_template` default to `prompts.SYSTEM_TEMPLATE`/`INSTANCE_TEMPLATE`; `step_limit=150`, `cost_limit=0.0`, `wall_time_limit_seconds=3600`, `approval_timeout_seconds: int = 86400`, `poll_seconds: float = 2.0`, `max_retries: int = 3`, `retry_backoff_seconds: tuple[float, ...] = (5.0, 15.0)`, `output_limit: int = 16000`, `test_command: str | None = None`, `allow: list[str] = []`, `outputs_dir: Path | None = None`.
- `class WebAgent(DefaultAgent)`: `__init__(self, model: AgentModel, env: ToolEnv, *, channel: TaskChannel, evidence: EvidenceGate = AcceptAllEvidence(), clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, config_class: type = WebAgentConfig, **kwargs)`. Attribute `phase: str = "investigate"`.
  - `run(task: str = "", **kwargs) -> dict`: sets `model.set_phase("investigate")`, then upstream `run`.
  - `query()`: if `channel.status()` is terminal → `TaskStopped`. Calls `super().query()` up to `max_retries` times on non-`InterruptAgentFlow` exceptions (event `retry {step:"model", attempt, error}`, sleeping per backoff); when exhausted → system question (§0.2) → `retry` resets, else `RetriesExhausted`. Adds the message's cost via `channel.add_cost`.
  - `execute_actions(message)`: for each action in order: `policy.check_tool(tool, phase)` forbid → observation `forbidden: …` (rc 126) and event `tool_refused`; bash → `policy.classify(command, phase=phase, allow=config.allow)`, event `command_verdict {command, decision, reasons}`; `forbid` → not executed (rc 126); `approve` → approval `command {command, reasons, phase}`, status `awaiting_command_approval`, wait, back to `PHASE_STATUS[phase]`; approved → execute, rejected → observation `rejected by user: <comment>` (rc 1). Container tools via `env.execute`/`run_tool`; backend tools as in §5.1/§0. After `submit_plan`, `start_verify` or `complete_task`, the remaining actions in that message are not executed (upstream padding). `submit_plan`: invalid `Plan` → `ToolError` text; else approval `plan`, `transition("awaiting_plan_approval", phase="plan")`, wait; approved → `transition("executing", phase="execute")`, `model.set_phase("execute")`, event `phase_model {phase, model}`; rejected → `transition("investigating", phase="investigate")` and the comment is the observation. `start_verify` → `transition("verifying", phase="verify")`, `set_phase("verify")`. `ask_user` → question (agent), `awaiting_input`, wait, back, answer = observation. `complete_task` → gate refusal = observation, else event `completed {summary}` and raises `TaskSubmitted`. A `run_tests` infra failure retries per §0.2. Every action emits `tool_call {tool, args (truncated), tool_call_id}` and `tool_result {tool, tool_call_id, returncode, output (≤ output_limit), truncated: bool, output_ref: str | None}`; full output → `outputs_dir/<tool_call_id>.txt`.
  - `add_messages(*messages)`: emits `message {role, content (truncated), model?, phase?, exit_status?}` per message, then `super()`.
  - Waits use `wait_for` with `approval_timeout_seconds`, then add the waited time to `self._start_time`. On timeout the row is expired, and `WaitTimedOut` lets the runner set `failed`.
- `prompts.py`: Jinja templates (StrictUndefined). Vars: `task` (description), `repo`, `failing_test`, `agents_prose`, `test_command`. Rules adapted from `system_prompts/Cursor_*`: investigate is read-only, then `submit_plan`, edit only after approval, `start_verify`, then `complete_task`.

## 6. `swf.sandbox` changes

- `ExecResult` dataclass `(returncode: int, stdout: bytes, stderr: bytes)`; `SandboxEnv.exec_argv(argv: Sequence[str], *, stdin: bytes | None = None, timeout: int | None = None) -> ExecResult` = `docker exec -i -w /repo <id> <argv…>` (argv list, no shell).
- `SandboxEnv` implements `ToolEnv`: `read_bytes` (`cat -- p`), `write_bytes` (`sh -c 'mkdir -p "$(dirname "$1")" && cat > "$1"' sh p` with stdin), `list_dir` (`ls -1Ap -- p`), `search` (`grep -rnE -m 200 [--include=g] -- pat p`). Nonzero → `ToolError(stderr)`.
- `_check_finished(self, output) -> None`: no-op (§0.7).
- `open_task_sandbox(egress: EgressConfig, req: EnvRequest) -> SandboxEnv`: `attach_task_network` → `SandboxEnv(SandboxSpec(task_id, image, workspace, proxy_url, network, memory_mb, cpus))`; its `cleanup()` also calls `detach_task_network`. If the start fails, the network is detached before re-raising.

## 7. `swf.worker`

- `@dataclass(frozen=True) EnvRequest(task_id: str, image: str, workspace: Path, memory_mb: int, cpus: float)`; `EnvFactory = Callable[[EnvRequest], ToolEnv]`.
- `@dataclass(frozen=True) TaskWorkspace(root: Path, repo: Path)` (root = `workspaces/<task-id>`, repo = `root/repo`); `class WorkspaceProvider(Protocol): prepare(task: store.Task) -> TaskWorkspace`; `LocalCopyWorkspace(source: Path, workspaces_root: Path)`.
- `@dataclass(frozen=True) WorkerConfig(worker_id: str, workspaces_root: Path, max_concurrent: int = 2, poll_seconds: float = 2.0, limits: Limits = Limits(), agent: dict[str, Any] = {})` (`agent` = `WebAgentConfig` overrides, e.g. `approval_timeout_seconds`).
- `@dataclass RunnerDeps(db: Database, workspaces: WorkspaceProvider, env_factory: EnvFactory, model_factory: Callable[[], AgentModel], config: WorkerConfig, evidence: EvidenceGate = AcceptAllEvidence(), clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)`.
- `class TaskRunner`: `__init__(self, deps: RunnerDeps, task: store.Task)`; `run(self) -> str` (final status):
  1. `channel = PgTaskChannel`; `ws = workspaces.prepare(task)`; AGENTS.md from `ws.repo` (missing/invalid → no setup, no test command, no allow).
  2. Image/memory/cpus come from `task.preflight` (`reservation_*`); start the env with ≤ 3 attempts (§0.2).
  3. Each `setup` command: classify (§0.8), `env.execute` with timeout 600, ≤ 3 attempts each (§0.2), events `setup {command, attempt, returncode}`.
  4. `transition("investigating", phase="investigate")`; `agent = WebAgent(model_factory(), env, channel=…, output_path=ws.root/"trajectory.json", outputs_dir=ws.root/"outputs", test_command=…, allow=…, **config.agent)`; `agent.run(task.description, repo=…, failing_test=…, agents_prose=…, test_command=…)`.
  5. Exit-status mapping: `Submitted` → `confirming` → `completed`; `Stopped` → no write; `ApprovalTimeout` → `failed("approval_timeout")`; `QuestionTimeout` → `failed("question_timeout")`; `LimitsExceeded`/`TimeExceeded`/`RepeatedFormatError`/`RetriesExhausted`/`SetupForbidden` → `failed(<snake_case exit status>)`; uncaught exception → `failed("internal_error: <Type>")`. `TaskFinished` from any transition → stop quietly.
  6. `finally`: `env.cleanup()` (if started), trajectory saved, never raises.
- `class Worker`: `__init__(self, deps: RunnerDeps, *, orphan_cleaner: Callable[[Collection[str]], list[str]])`; `reconcile() -> list[uuid.UUID]` (`interrupt_orphans`, then `orphan_cleaner(active_task_ids)`, events `status`); `claim() -> store.Task | None`; `run_once() -> uuid.UUID | None` (claim + run synchronously; tests); `serve(stop: threading.Event) -> None` (`reconcile()` once, then loop: while running < `max_concurrent`, claim and start a thread running `TaskRunner(...).run()`; `sleep(poll_seconds)`; on stop, join threads).
- `main() -> None` (`uv run python -m swf.worker`): `_mswea.isolate()` first, `Settings.from_env()`, `ensure_egress(EgressConfig())`, `orphan_cleaner=lambda a: cleanup_orphans(a, egress=cfg)`, `PhaseModel.from_env`, `open_task_sandbox`, and `SWF_WORKSPACES_ROOT` (default `./workspaces`). M4 has no production `WorkspaceProvider` (M6 adds the clone), so `main` exits with a clear error unless `SWF_LOCAL_SOURCE` is set.

## 8. Claim and worker algorithm

```
claim_next_task (one backend transaction):
  select pg_advisory_xact_lock(hashtext('swf.admission'))         -- serialises admission
  head := select * from tasks where status='queued' order by created_seq limit 1 for update skip locked
  if head is null: return None
  active := [Reservation(coalesce(mb,4096), coalesce(cpu,1.0)) for tasks where status in ACTIVE_STATUSES]
  if not preflight.fits(active, Reservation(head.mb, head.cpu), limits): return None   -- FIFO: never skip the head
  update tasks set worker_id=?, claimed_at=now() where id=head.id
  transition_task(head.id, 'preparing', phase='prepare')            -- emits status event
  return task
serve:
  reconcile(); while not stop: [reap finished threads]; while len(running) < max_concurrent and (t := claim()): start TaskRunner(t)
  sleep(poll_seconds)
```

## 9. API (`swf.api.app`)

- `AppDeps` gains `sse_poll_seconds: float = 1.0`, `sse_keepalive_seconds: float = 15.0`.
- `GET /v1/tasks/{task_id}/events` (auth): the task must be the caller's (as_user), else 404 before streaming. Resume from header `Last-Event-ID: <seq>` or query `?after=<seq>` (header wins; default −1). `StreamingResponse(media_type="text/event-stream")` with a sync generator. Each event is `id: <seq>\nevent: <type>\ndata: <json payload + "created_at">\n\n`; a `: ping\n\n` goes out after `sse_keepalive_seconds` without data. Polls `list_events` every `sse_poll_seconds`. **Ends** once the task is terminal and every event up to the current `event_seq` has been sent. Headers: `Cache-Control: no-store`, `X-Accel-Buffering: no`.
- Approval/answer/cancel endpoints are unchanged except for the events (§4); deciding an `expired` approval → 409.

## 10. Tests (tester). Fakes in `tests/m4_fakes.py`

- `FakeClock`: `now: float`; `__call__() -> float`; `sleep(s)` advances `now` and records the sleeps. Never sleeps for real.
- `call(tool: str, id: str | None = None, **args) -> dict` (one action); `ScriptedModel(steps: list[list[dict] | Exception], *, cost: float = 0.0)`: implements `AgentModel`; each `query` returns `{"role":"assistant","content":"","tool_calls":[…],"extra":{"actions":…, "cost":…, "model": f"fake-{phase}", "phase":…}}` or raises the Exception; records `phases: list[str]` (per query) and `set_phase_calls`; `format_observation_messages` = upstream `format_toolcall_observation_messages`; running out of steps → `AssertionError`.
- `FakeCompletion(responses)`: a litellm.completion stand-in recording kwargs; responses built with `litellm.ModelResponse` tool calls.
- `FakeEnv(root: Path, bash: dict[str, tuple[str, int]] | Callable[[str], dict] = {})`: `ToolEnv` over a host tmp dir (`/repo` ↔ `root`); records `executed: list[str]`, `cleanups: int`; `fail_next(n, kind)` to script infra errors.
- `FakeChannel(status="investigating", *, decide: Callable[[str, dict], ApprovalOutcome | None] = auto_approve, answers: Callable[[str], str | None] = …)`: in-memory, validates with `swf.states.check_transition`; records `transitions: list[tuple[str, str | None]]`, `events: list[tuple[str, dict]]`, `costs`; `cancel()` sets `cancelled`. Pending decisions can be released by a `FakeClock` hook.
- Fixtures (in `m4_fakes.py`, imported by the test modules): `fake_clock`, `fake_env` (tmp copy of `seeded_repos/01-paginate-off-by-one/repo`), `fake_channel`, `make_agent(steps, **cfg) -> (WebAgent, ScriptedModel, FakeEnv, FakeChannel)`, `queued_task(fresh_db, **reservation) -> Task` (inserts a user and task directly by SQL).

| File | Must cover |
|---|---|
| `test_m4_states.py` | §2 table exactly; terminals have no exits; `check_transition` errors; `PHASE_STATUS` |
| `test_m4_mswea.py` | `isolate()` sets the 3 env vars; refuses a dir with `.env`; a subprocess `import swf.sandbox` with a planted `%LOCALAPPDATA%`-style `.env` (`MSWEA_GLOBAL_CONFIG_DIR` pre-set to it) does **not** load its variables and prints no banner; upstream subclass contract (`__version__ == "2.4.6"`, `DefaultAgent.execute_actions/add_messages/query/step` signatures, `InterruptAgentFlow(*messages)`) |
| `test_m4_tools.py` | `tool_schemas(phase)` == `policy.TOOL_PHASES` (incl. `start_verify` = execute); every schema is valid JSON schema and has `additionalProperties: false`; `resolve_repo_path` accept/reject (`..`, NUL, abs outside /repo); read range and 256 KiB cap; list_dir; search ≤ 200; edit_file once/none/multiple/replace_all; write_file 1 MiB cap; run_tests selector validation, quoting, no-test-command; `Plan` validation |
| `test_m4_model.py` | per-phase wire model and tools; Portkey kwargs (`api_base`, header, key from env); missing key → `MissingApiKey`; key absent from `serialize()`; parse → action shape; no/unknown tool or bad JSON → `FormatError`; `extra.model`/`phase`; one shared message history across the switch (RQ1 offline); `SWF_MODEL_*` overrides |
| `test_m4_agent.py` | happy path investigate → plan approval → execute → start_verify → verify → complete (exact transitions + `set_phase` calls); plan rejected → investigating + comment observation; command approve/reject; forbid never executes; edit/write refused in investigate; ask_user round trip; **24 h timeout** (FakeClock) → approval `expired`, `ApprovalTimeout`; configurable timeout; question timeout; cancel between steps and during a wait → `Stopped`; model error ×3 → system question → `retry` resets / other answer → `RetriesExhausted`; run_tests infra retry; `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` output does not finish; waits excluded from wall time; step limit → `LimitsExceeded`; events (`tool_call`, `tool_result`, `command_verdict`, `message`, `status`) and output truncation + `output_ref`; actions after `submit_plan` not executed |
| `test_m4_store.py` (DB, `fresh_db`) | `transition_task` validates, emits `status`, sets `finished_at`/`error`; gap-free seq under 2 threads × 50 appends; one pending approval / one open question per task; expire functions + 409 on deciding expired; cancel/decide/answer emit events and cancel expires pendings; RLS: another user sees no events |
| `test_m4_worker.py` (DB) | FIFO claim; 2 concurrent claimers never take the same task; capacity for one more + 2 claimers → exactly one admitted; non-fitting head blocks the queue; `reconcile` → active tasks `interrupted` (`worker_restarted`), pendings expired, cleaner called with `[]` active; `TaskRunner` with fakes through `PgTaskChannel` → `completed`, `trajectory.json` written, env cleaned up even on exception; setup failing 3× → awaiting_input → `retry` → success / other answer → `failed`; setup `forbid` → `failed`; approval via the API endpoint unblocks a runner thread; API cancel stops it; uncaught error → `failed internal_error` |
| `test_m4_sse.py` (API) | 404 for another user's task; framing (`id`/`event`/`data`); order; `Last-Event-ID` and `?after=` resume; stream ends at terminal; an event appended by another thread mid-stream arrives |
| `test_m4_sandbox_tools.py` (`docker`) | `SandboxEnv` read/write/list/search via `exec_argv` incl. a 100 KB write (past the Windows command-line cap); path with spaces; sentinel output does not raise `Submitted`; `open_task_sandbox` cleanup removes container + network |
| `test_m4_live_smoke.py` (`live`, `docker`, `network`) | skipped (with reason) without `PORTKEY_API_KEY`; seed 01 in a sandbox; `FakeChannel` auto-approves; `PhaseModel.from_env()`; ≥ 1 tool-call response from each model (RQ1); cost per call recorded (RQ2, 0 allowed); trajectory written to `<worktree>/.m4-smoke/trajectory.json` and contains no key |

- `pyproject.toml [tool.pytest.ini_options]`: register `live: calls paid/free external models (skipped without PORTKEY_API_KEY)`.

## 11. File ownership (exclusive)

- **Coder:** `src/swf/__init__.py`, `src/swf/_mswea.py` (new), `src/swf/states.py` (new), `src/swf/agent/{__init__,tools,model,channel,web_agent,prompts}.py` (new), `src/swf/worker.py` (new), `src/swf/store.py`, `src/swf/sandbox.py`, `src/swf/policy.py` (only `start_verify` in `TOOL_PHASES`), `src/swf/preflight.py` (only `fits`), `src/swf/api/app.py`, `src/swf/api/main.py`, `supabase/migrations/20261121000000_m4_agent.sql` (new).
- **Tester:** `tests/test_m4_*.py` (new), `tests/m4_fakes.py` (new), `pyproject.toml [tool.pytest.ini_options]` only; touch `tests/conftest.py`/`helpers.py` only if a shared fixture is unavoidable (report it).
- **Planner:** `docs/` (this file, implementation-plan §18 M4 status, HANDOVER).
- Rules as in m2-contract §8: work only in this worktree; no commit/push/stash; LF endings; no `uv add` (litellm, tenacity and jinja2 come with mini-swe-agent; report any missing dependency); Docker names `swf-test-*` / `swf-coder-*`; done markers `.m4-done-code`, `.m4-done-tests`. mypy strict and ruff stay clean.

## 12. Amendments (v2, user 2026-11-20) — override §0–§11

### A1 No `start_verify`: the harness switches execute → verify

- **Removed:** §0.4, the `start_verify` row in §5.1, the `start_verify` bullet in §5.4, and the `policy.py` change in §11. `policy.TOOL_PHASES` is **unchanged** from M3.
- **Edit tracking.** `WebAgent` keeps `edits_since_approval: int`, reset to 0 when the plan is approved. +1 for each successful (rc 0) `edit_file`/`write_file`, and for each **executed** bash command with `returncode == 0` that is a writer: `policy.classify(cmd, phase="investigate", allow=config.allow).decision == "forbid"` while the execute-phase verdict allowed or approved it.
- **Trigger** (checked only after a `run_tests` result, only in phase `execute`), all must hold:
  1. `returncode == 0` and not an infra failure (`exception_info` empty);
  2. `edits_since_approval >= 1`;
  3. the run covers the target: `task.failing_test` is None, **or** `selector` is None (whole AGENTS.md suite), **or** `failing_test` is a substring of `selector`.
  `run_tests` in investigate or verify never switches. A rc 0 run that misses a condition returns its normal observation.
- **Switch, in order:**
  1. `channel.transition("verifying", phase="verify")`;
  2. `self.phase = "verify"`, then `model.set_phase("verify")` (wire model stays `zai/glm-4.7-flash`; tools change: no `edit_file`/`write_file`; `record_evidence`/`complete_task` appear);
  3. events `phase_switch {from: "execute", to: "verify", trigger: {tool_call_id, selector, edits_since_approval}}`, then `phase_model {phase: "verify", model}`;
  4. append to that `run_tests` observation the constant `prompts.VERIFY_SWITCH_NOTE`:
     `\n[harness] Tests passed after your changes, so the task is now in the verify phase. edit_file and write_file are no longer available. Re-run the checks you need, record evidence, then call complete_task.`
  5. Later actions in the same message are not executed (upstream padding), as after `submit_plan`.
- `WebAgentConfig` gains `failing_test: str | None = None`; `TaskRunner` passes `task.failing_test`. `prompts.SYSTEM_TEMPLATE` tells the agent a passing `run_tests` after its fix moves it to verify.
- Flow: `investigate → submit_plan → (approved) → edits → run_tests rc0 → verify → complete_task`. §2 table unchanged.

### A2 Multiple workers: leases and heartbeats (replaces §0.11 and `reconcile`/`interrupt_orphans` in §4, §7, §8)

- [D] Any number of workers per DB, one or more hosts, Postgres only. Lease times always from DB `now()`, never a worker clock.
- **Worker identity:** `WorkerConfig.worker_id` = `$SWF_WORKER_ID` or `socket.gethostname()`; stable across restarts; two workers on one host must set distinct ids; must match `^[A-Za-z0-9_.-]{1,64}$`.
- **Migration additions** (in `20261121000000_m4_agent.sql`):
```sql
alter table public.tasks
  add column lease_expires_at timestamptz,
  add column heartbeat_at     timestamptz,
  add check (worker_id is null or worker_id ~ '^[A-Za-z0-9_.-]{1,64}$');
create index tasks_lease_idx on public.tasks (lease_expires_at)
  where status not in ('created', 'queued', 'completed', 'rejected', 'failed', 'interrupted', 'cancelled');
```
  An active row with `lease_expires_at is null` counts as **expired**.
- **`WorkerConfig`** gains `lease_seconds: int = 90`, `heartbeat_seconds: int = 30`; `heartbeat_seconds * 2 < lease_seconds` enforced (`ValueError`).
- **Store signature changes:**
  - `claim_next_task(conn, *, worker_id: str, lease_seconds: int, limits: Limits = Limits()) -> Task | None`: §8 plus `lease_expires_at = now() + lease`, `heartbeat_at = now()`.
  - `renew_leases(conn, *, worker_id: str, task_ids: Collection[uuid.UUID], lease_seconds: int) -> list[uuid.UUID]`: `update … set lease_expires_at = now()+lease, heartbeat_at = now() where id = any(ids) and worker_id = %s and status = any(ACTIVE_STATUSES) returning id`. Ids not returned are **lost**.
  - `interrupt_orphans(conn, *, worker_id: str, restart: bool) -> list[tuple[uuid.UUID, str | None]]` (`(task_id, previous worker_id)`): locks active rows (`for update skip locked`) where `lease_expires_at is null or lease_expires_at < now()` (error `lease_expired`), or, when `restart`, `worker_id = %s` (error `worker_restarted`); each via `transition_task(…, "interrupted", error=…)`; pending approvals/open questions expired.
  - `transition_task` to a terminal status also sets `lease_expires_at = null`.
  - `active_task_ids(conn)` unchanged; no longer used for cleanup.
- **Sandbox/egress changes:**
  - `LABEL_WORKER = "swf.worker"`.
  - `SandboxSpec` gains `worker_id: str | None = None` (same pattern); `sandbox_run_args` adds `--label swf.worker=<id>` when set.
  - `attach_task_network(cfg, task_id, *, worker_id: str | None = None, docker="docker")` labels the network the same.
  - `EnvRequest` gains `worker_id: str`; `open_task_sandbox` passes it on.
  - `cleanup_orphans(active_task_ids, *, worker_id: str | None = None, reap_task_ids: Collection[str] = (), egress=None, docker="docker") -> list[str]`: a managed container/network is removed iff its task ∉ `active_task_ids` **and** (`worker_id` is None (M2 behaviour) or its `swf.worker` label == `worker_id` or its task ∈ `reap_task_ids`). Already-gone is not an error.
- **`Worker` changes:**
  - `__init__(self, deps, *, orphan_cleaner: Callable[..., list[str]])`; called as `orphan_cleaner(active_task_ids, worker_id=…, reap_task_ids=…)`.
  - `running: dict[uuid.UUID, TaskRunner]`.
  - `reconcile(*, restart: bool) -> list[uuid.UUID]`: `interrupt_orphans`, then `orphan_cleaner(active_task_ids=<ids in self.running>, worker_id=self.id, reap_task_ids=<interrupted ids>)`.
  - `heartbeat() -> list[uuid.UUID]`: `renew_leases` for `running`; for each lost id `runner.stop()`; returns lost ids.
  - `serve(stop)`: `reconcile(restart=True)` first; a **separate heartbeat thread** runs `heartbeat()` every `heartbeat_seconds` (model calls/waits never block renewal); main loop runs `reconcile(restart=False)` every `heartbeat_seconds` and claims per §8. On stop: stop runners, join, let leases lapse.
  - `run_once()` holds the lease the same way for its duration.
- **`TaskRunner`:** gains `stop() -> None`, `stopped: bool`. A stopped runner never writes a transition (stale worker must not overwrite `interrupted`); its `finally` still cleans up the env.
- **`main()`:** cleaner = `partial(cleanup_orphans, egress=cfg)`; passes `worker_id` into `EnvRequest`.

### A3 Test-table changes (override §10)

- `test_m4_tools.py`: no `start_verify`; assert absent from `TOOL_SCHEMAS` and `policy.TOOL_PHASES`; `tool_schemas(phase)` matches the M3 table exactly.
- `test_m4_agent.py`: happy path = investigate → plan approved → `edit_file` → `run_tests` rc 0 → **auto** verify → `complete_task`; assert exact transitions, `set_phase` calls, `phase_switch`/`phase_model` events and note text. New cases:
  - **No switch:** rc ≠ 0; rc 0 with no edit since approval; rc 0 with selector missing `failing_test`; infra failure; `run_tests` rc 0 in investigate or verify.
  - **Switch:** selector equal to/containing `failing_test`; `failing_test` None with any rc 0 run; bash writer edit (`sed -i …`) counts; read-only bash (`cat`) does not; failed `edit_file` does not.
  - **Edge:** edit counter resets after a second plan approval; actions after the triggering `run_tests` not executed.
- `test_m4_worker.py`: replace reconcile cases with:
  - **Claim/leases:** claim sets `worker_id`, `lease_expires_at > now()`, `heartbeat_at`; `renew_leases` advances the lease and leaves other workers' rows alone; terminal transition clears the lease.
  - **Reconcile:** other worker's expired lease → `interrupted`/`lease_expired`; other worker's live lease untouched; null lease on an active row → reaped; `restart=True` interrupts own rows even with a live lease (`worker_restarted`); `restart=False` does not; pendings expired.
  - **Lost lease:** row interrupted externally → `heartbeat()` returns its id, runner stops, does not overwrite status, still cleans up env.
  - **Cleaner/runs:** cleaner called with `worker_id` and reaped ids; two `Worker`s (distinct ids) over one `fresh_db` run 4 queued tasks with fakes, each exactly once, `max_concurrent` respected across both; heartbeat thread keeps renewing during a 3-poll approval wait.
  - **Config:** `heartbeat_seconds * 2 >= lease_seconds` → `ValueError`.
- `test_m4_sandbox_tools.py` (`docker`): run args carry `--label swf.worker=<id>`; task network carries it too; `cleanup_orphans(worker_id="A")` removes A's inactive container+network, keeps B's; with `reap_task_ids=[B's task]` removes B's too; `worker_id=None` keeps M2 behaviour.
- `test_m4_store.py`: add `renew_leases` return semantics and the `worker_id` check constraint.
- §11 ownership unchanged except: coder does **not** touch `src/swf/policy.py`; `src/swf/egress.py` joins the coder's list.
