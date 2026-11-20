# M5 contract — evidence, harness re-run, read-only backstop

- **Purpose:** The interface contract for the parallel M5 build. The coder implements it, the tester tests it, the checker verifies both against it. If something here is wrong, report it; do not silently diverge.
- **Last updated:** 2026-11-20
- **Status:** Draft, awaiting user approval of §0 items marked **[A, needs user OK]**. Branch `feature/m5`, on top of `87a3231` (M4 + its §12 amendments are the baseline).
- Sources: plan §13 (bundle, harness confirmation, backstop), §18 M5; spec §1.3, §8 (read-only row), §9 steps 3/6/7, §10 criteria 1–3, §11 (failed with partial evidence); m4-contract §0.5, §12 A1.

## Decisions needing user approval (summary)

1. §0.1 The harness runs the baseline itself (target + full suite) at the end of `preparing`; a passing baseline fails the task.
2. §0.2 Backstop response = `failed("readonly_violation")`, not an approval.
3. §0.3 Post/suite evidence comes only from the agent's real `run_tests` results in execute/verify, pinned to a workspace digest.
4. §0.4 No `failing_test` → the target is the whole suite.
5. §0.6 Secure defaults: completion is refused unless a harness (or an explicit `AcceptAllEvidence`) is wired; M4 tests opt out explicitly (tester edits 3 M4 test files).
6. §0.8 Diff = host-side copy at investigate start + `difflib`, not `git diff` (M5 workspaces are not git repos).

## 0. Decisions and assumptions

1. [A, needs user OK] **Baseline is run by the harness, not the agent.** After the AGENTS.md `setup` commands and before `investigating`, the harness runs (a) the target test and (b) the full AGENTS.md suite, each once, through `harness_command` (§2). Rows `baseline` and `baseline_suite`, source `harness`. Target exit code 0 → `failed("baseline_not_reproduced")` before any model call (spec §11: no speculative fix). No AGENTS.md test command → `failed("no_test_command")`. Infra failure (rc −1 / `exception_info`) → the M4 §0.2 retry path (`_with_retries("baseline", …)`, system question, back to `preparing`).
2. [A, needs user OK] **Backstop response = fail (safer than approval).** Snapshot S0 of the host workspace (`ws.repo`) right after the baseline, i.e. at investigate start. At **every** `submit_plan` (before validation, before any approval row), a new snapshot is compared to S0; any added/removed/modified path → event `backstop_violation`, exit `ReadOnlyViolation` → `failed("readonly_violation")`. No plan approval is created. Spec §8 "blocked, flagged" = terminal block + event + `tasks.error`. An approval would let a user accept an unplanned write; failing cannot.
3. [A, needs user OK] **Evidence is recorded by the harness, never from agent text.** Every `run_tests` result in phase `execute` or `verify` (not infra failures) that covers the target or has no selector becomes an `agent_run` row (kind `suite` without a selector, else `post`; `covers_target` stored; see §2 `record_run`). Each row stores the workspace digest taken **after** the run. Only rows whose digest equals the digest at `complete_task` count, so any later write (any tool, any bash) invalidates them. `record_evidence` writes **no** row (§0.7).
4. [A, needs user OK] `task.failing_test is None` → the target is the whole suite: baseline = one suite run recorded as both `baseline` and `baseline_suite` (`details.shared_run = true`); post = a passing run with no selector.
5. [D] Harness confirmation in `confirming` (plan §13): re-run the target once via `harness_command` (caches cleared, `PYTHONDONTWRITEBYTECODE=1`), row `harness_rerun`. Exit ≠ 0, or the workspace digest differs from the one the gate accepted → `failed("harness_mismatch: …")` with event `harness_discrepancy`. [A] Infra failure: ≤ `max_retries` attempts with backoff, then `failed("harness_rerun_error")`; no retry question (there is no `confirming ↔ awaiting_input` edge, and §2 of m4 stays unchanged).
6. [A, needs user OK] **Secure defaults.** `WebAgent(evidence=None)` now uses `RefuseAllEvidence` (completion always refused). `RunnerDeps.evidence: EvidenceGate | None = None`; `None` → the runner builds a `TaskHarness` per task (production path, `main()` unchanged). `AcceptAllEvidence` stays as an explicit opt-out (no baseline, no backstop, no re-run: M4 behaviour). So M4 tests that complete tasks must pass `AcceptAllEvidence()`: the tester edits `tests/m4_fakes.py` (`make_agent` default), `tests/test_m4_worker.py` (rig default), `tests/test_m4_live_smoke.py` (the `WebAgent(...)` call), opt-out lines only.
7. [D] `record_evidence` keeps its M4 schema (no tool/policy change). With a harness it returns `TaskHarness.status_text()` (rc 0) and emits `evidence_note {kind, note, command}` (untrusted agent text, not a row). Without one (opt-out) it keeps the M4 stub behaviour.
8. [A, needs user OK] Diff: at S0 the harness copies `ws.repo` to `ws.root/baseline/` (same ignore set, symlinks copied as links). The diff is `unified_diff(baseline, repo)` (difflib, git-style headers). M6 may switch to `git diff --binary` once workspaces are clones.
9. [D] Snapshots are host-side (`os.walk(followlinks=False)`, `lstat`): no container dependency, the agent cannot tamper with the hashing, works with `FakeEnv`. Ignored: directories `.git`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`; files ending `.pyc`/`.pyo`. [A] Mode bits are not compared (unreliable on the Windows dev host). Known limit: a write reverted before `submit_plan` is invisible (the classifier is the primary control).
10. [D] Stale-bytecode lesson (M0): every harness test run uses `HARNESS_PREAMBLE` (exports `PYTHONDONTWRITEBYTECODE=1`, deletes `__pycache__` dirs and `.pytest_cache` under `/repo`). Agent `run_tests` keep the M4 command (the container already sets `PYTHONDONTWRITEBYTECODE=1`); the re-run is the arbiter. Host-side test runs keep `PYTHONDONTWRITEBYTECODE=1` (CLAUDE.md).
11. [A] Suite rule "no new failures vs baseline": the latest current-digest suite run exits 0, **or** its parsed failure ids are non-empty and ⊆ the baseline suite's. A failing suite whose failures cannot be parsed → refused (`suite_unparsed`). Parsing = pytest short-summary lines (`FAILED <id>` / `ERROR <id>`, id ends at ` - ` or end of line); all 10 seeds are pytest. A post run must exit 0 (a selector run, or a no-selector run that exits 0).
12. [D] DB rows keep the **tail** of test output (≤ `output_limit` = 16000 chars) and the **head** of the diff; full text goes to `ws.root/evidence/<NNN>-<kind>.txt|.patch`; `output_ref` is that path **relative to `ws.root`** (never a host absolute path in the API).
13. [D] `GET /v1/tasks/{id}/evidence` (deferred from M1): `as_user` + RLS (`evidence_select_own` from M1); another user's task → 404.

## 1. Flow changes (no change to the m4 §2 transition table)

```
preparing: start env → setup → [harness] run_baseline (retryable) → take_start_snapshot (S0 + baseline copy)
investigating: agent … submit_plan → [harness] check_readonly vs S0 → violation: failed(readonly_violation)
executing/verifying: each run_tests result → [harness] record_run
verifying: complete_task → gate = harness.check(): refusal text as observation, or accept (diff row)
confirming: [harness] confirm() → completed | failed(harness_mismatch: …) | failed(harness_rerun_error)
```
New exit statuses (`swf.agent.channel`, all `AgentExit`): `ReadOnlyViolation` ("ReadOnlyViolation"), `BaselineNotReproduced` ("BaselineNotReproduced"), `NoTestCommand` ("NoTestCommand"). `worker._EXIT_ERRORS` adds `readonly_violation`, `baseline_not_reproduced`, `no_test_command`.

## 2. `swf.evidence` (new module)

Imports `swf.agent.channel`, `swf.agent.tools`, `swf.store`, `swf.db`; nothing in `swf.agent` imports it (no cycle).

```python
SNAPSHOT_IGNORE_DIRS: frozenset[str]  # §0.9
SNAPSHOT_IGNORE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")
HARNESS_TEST_TIMEOUT_SECONDS = 600
HARNESS_PREAMBLE = ("export PYTHONDONTWRITEBYTECODE=1; "
    "find /repo -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; "
    "rm -rf /repo/.pytest_cache; ")
REFUSAL_PREFIX = "complete_task refused: the evidence bundle is incomplete."
EvidenceKind = Literal["baseline", "baseline_suite", "post", "suite", "harness_rerun", "diff"]
EvidenceSource = Literal["harness", "agent_run"]

@dataclass(frozen=True)
class Snapshot:
    entries: Mapping[str, str]  # posix relpath -> "f:<sha256 hex>" | "l:<link target>" | "o"
    @property
    def digest(self) -> str: ...  # sha256 hex of sorted f"{path}\0{entry}\n" lines
    @property
    def files(self) -> int: ...
def take_snapshot(root: Path) -> Snapshot  # never follows symlinks; ignores per §0.9
@dataclass(frozen=True)
class SnapshotDiff:
    added: tuple[str, ...]; removed: tuple[str, ...]; modified: tuple[str, ...]  # each sorted
    def is_empty(self) -> bool: ...
    def as_payload(self, limit: int = 100) -> dict[str, Any]: ...
        # {"added": [...≤limit], "removed": [...], "modified": [...], "counts": {"added": n, ...}}
def diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDiff
def copy_tree(src: Path, dst: Path) -> None  # dst replaced; same ignores; symlinks as links
def unified_diff(base: Path, repo: Path) -> str
    # per changed path (sorted): "--- a/<p>"/"+++ b/<p>" ("/dev/null" for added/removed), difflib
    # hunks; non-UTF-8 or NUL in first 8 KiB -> "Binary files a/<p> and b/<p> differ";
    # symlink -> "Symlink <p> -> <target>". Empty string when nothing changed.
def parse_failures(output: str) -> frozenset[str]  # §0.11
def covers_target(selector: str | None, failing_test: str | None) -> bool
    # failing_test None -> selector is None; else selector is None or failing_test in selector
def harness_command(test_command: str | None, selector: str | None) -> str
    # HARNESS_PREAMBLE + tools.build_test_command(...); None test command -> NoTestCommand()

@dataclass(frozen=True)
class EvidenceRecord:
    kind: EvidenceKind
    source: EvidenceSource
    command: str | None = None
    selector: str | None = None
    exit_code: int | None = None
    output: str | None = None          # tail (tests) / head (diff), <= output_limit
    output_truncated: bool = False
    output_ref: str | None = None      # relative to ws.root
    failures: frozenset[str] = frozenset()
    duration_ms: int | None = None
    workspace_digest: str | None = None
    tool_call_id: str | None = None
    covers_target: bool = False        # persisted as details["covers_target"]
    details: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class BundleProblem:
    code: Literal["baseline_missing", "baseline_not_failing", "post_missing", "post_failing",
                  "suite_missing", "suite_new_failures", "suite_unparsed", "diff_empty"]
    message: str
def bundle_problems(*, baseline: EvidenceRecord | None, baseline_suite: EvidenceRecord | None,
                    runs: Sequence[EvidenceRecord], current_digest: str, start_digest: str
                    ) -> list[BundleProblem]   # [] = complete; order = the code order above
```
`bundle_problems` rules (`runs` = agent_run rows, oldest first; "current" = `workspace_digest == current_digest`):
- `baseline` None → `baseline_missing`; its `exit_code == 0` → `baseline_not_failing`.
- latest current run with `covers_target` → none: `post_missing`; `exit_code != 0`: `post_failing`.
- latest current run with `selector is None` → none: `suite_missing`; `exit_code == 0`: ok; failures empty: `suite_unparsed`; `failures - baseline_suite.failures` non-empty: `suite_new_failures` (message lists the ids, ≤ 20).
- `current_digest == start_digest` → `diff_empty`.

```python
class EvidenceSink(Protocol):
    def add(self, record: EvidenceRecord) -> int: ...   # returns the evidence id
class PgEvidenceSink:  # EvidenceSink over store.add_evidence, one backend txn per call
    def __init__(self, db: Database, task_id: uuid.UUID, *,
                 is_stopped: Callable[[], bool] = lambda: False) -> None: ...  # stopped -> no write, returns -1
class HarnessInfraError(Exception): ...  # a harness test run did not run (rc -1 / exception_info)
@dataclass(frozen=True)
class Confirmation:
    passed: bool; exit_code: int | None; evidence_id: int | None; discrepancy: dict[str, Any] | None

class TaskHarness:  # implements web_agent.EvidenceHarness (§4)
    def __init__(self, *, env: ToolEnv, channel: TaskChannel, sink: EvidenceSink, repo: Path,
                 root: Path, test_command: str | None, failing_test: str | None,
                 output_limit: int = 16000, clock: Callable[[], float] = time.monotonic) -> None
    started: bool                      # True after take_start_snapshot
    start_digest: str | None
    checked_digest: str | None         # digest accepted by the last successful check()
    def run_baseline(self) -> None
    def take_start_snapshot(self) -> Snapshot
    def check_readonly(self) -> dict[str, Any]        # SnapshotDiff.as_payload(); empty lists = clean
    def record_run(self, *, tool_call_id: str, selector: str | None, output: dict[str, Any],
                   phase: str) -> None
    def status_text(self) -> str
    def check(self, channel: TaskChannel) -> str | None   # EvidenceGate
    def confirm(self) -> Confirmation
```
Behaviour:
- `run_baseline`: `NoTestCommand` if no test command. Runs `harness_command(tc, failing_test)` then (if `failing_test` is set) `harness_command(tc, None)`, each `env.execute({"command": …}, timeout=HARNESS_TEST_TIMEOUT_SECONDS)`. Infra failure → `HarnessInfraError` and **no** rows written (rows only after all runs succeed). Writes `baseline` (+ `baseline_suite`) via the sink, output files under `root/evidence/`. Then target `exit_code == 0` → emit `baseline_not_reproduced {exit_code}` and raise `BaselineNotReproduced`.
- `take_start_snapshot`: S0 = `take_snapshot(repo)`, `copy_tree(repo, root/"baseline")`, event `backstop_snapshot {digest, files}`, `started = True`.
- `check_readonly`: `RuntimeError` if not started; `diff_snapshots(S0, take_snapshot(repo)).as_payload()`.
- `record_run`: ignored unless `phase in {"execute","verify"}`, not an infra failure, and (`covers_target(selector, failing_test)` or `selector is None`). Kind = `suite` if `selector is None` else `post`. Digest = snapshot after the run. Command = `build_test_command(tc, selector)`. Source `agent_run`.
- `check`: current digest + `bundle_problems`. Problems → event `evidence_check {complete: false, problems: [codes]}`, returns `REFUSAL_PREFIX + "".join(f"\n- [{p.code}] {p.message}" …)`. None → writes `diff` row (output = diff head, full in `NNN-diff.patch`, `details {files_changed}`), event `evidence_check {complete: true, problems: []}`, sets `checked_digest`, returns None.
- `status_text`: `"evidence bundle complete"` or `"evidence bundle incomplete:"` + the same `- [code] message` lines.
- `confirm`: digest ≠ `checked_digest` → no re-run, `Confirmation(False, None, None, {"reason": "workspace_changed", "checked_digest", "current_digest"})`. Else runs `harness_command(tc, failing_test)` (infra → `HarnessInfraError`), writes `harness_rerun` with `details {post_evidence_id, expected_exit_code: 0, passed}`, event `harness_rerun {evidence_id, exit_code, passed}`. Exit ≠ 0 → discrepancy `{"reason": "rerun_failed", "post_evidence_id", "post_exit_code": 0, "rerun_exit_code", "rerun_failures": [...], "output_tail": ≤ 2000 chars}`. Any discrepancy → event `harness_discrepancy <discrepancy>`.

## 3. Migration `supabase/migrations/20261122000000_m5_evidence.sql`

```sql
-- M5: evidence recorded by the harness (m5-contract §3).
alter table public.evidence drop constraint evidence_kind_check;
alter table public.evidence add constraint evidence_kind_check
  check (kind in ('baseline', 'baseline_suite', 'post', 'suite', 'harness_rerun', 'diff'));
alter table public.evidence
  add column source           text not null default 'harness'
                              check (source in ('harness', 'agent_run')),
  add column selector         text check (selector is null or length(selector) <= 300),
  add column output           text check (output is null or length(output) <= 20000),
  add column output_truncated boolean not null default false,
  add column failures         jsonb not null default '[]'::jsonb
                              check (jsonb_typeof(failures) = 'array'),
  add column workspace_digest text
                              check (workspace_digest is null or workspace_digest ~ '^[0-9a-f]{64}$'),
  add column tool_call_id     text,
  add column details          jsonb not null default '{}'::jsonb
                              check (jsonb_typeof(details) = 'object');
create index evidence_task_id_idx on public.evidence (task_id, id);
```
M1 grants (authenticated: SELECT) and `evidence_select_own` cover the new columns; writes stay backend-only. The M1 RLS test's `insert (task_id, kind) values (…, 'baseline')` stays valid.

## 4. `swf.agent` changes

- `channel.py`: the three exits of §1.
- `web_agent.py`:
  - `class RefuseAllEvidence` (`check` → `"complete_task refused: no evidence harness is configured"`); `WebAgent.__init__(…, evidence: EvidenceGate | None = None, …)` → `None` means `RefuseAllEvidence()`. `AcceptAllEvidence` unchanged.
  - `@runtime_checkable class EvidenceHarness(EvidenceGate, Protocol)`: `check_readonly() -> dict[str, Any]`, `record_run(*, tool_call_id: str, selector: str | None, output: dict[str, Any], phase: str) -> None`, `status_text() -> str` (`EvidenceGate` becomes `@runtime_checkable` too). Hooks run only when `isinstance(self.evidence, EvidenceHarness)`.
  - `_run_tests`: after a non-infra result, `record_run(…, phase=self.phase)` **before** the A1 switch check.
  - `_submit_plan`: first `report = check_readonly()`; any non-empty list → `emit("backstop_violation", report)` and raise `ReadOnlyViolation()` (no `request_approval`, no transition).
  - `record_evidence`: §0.7. `complete_task`: unchanged (`self.evidence.check(channel)`).
- `prompts.py`: `SYSTEM_TEMPLATE` verify paragraph says: the harness ran the target test and the suite before you started (baseline); it records evidence from your `run_tests` results only; before `complete_task`, after your last change, run the target test (must pass) and `run_tests` with no selector (no new failures); `record_evidence` shows what is missing; any change in investigate fails the task. `VERIFY_SWITCH_NOTE` and `INSTANCE_TEMPLATE` unchanged (M4 tests assert them).
- `__init__.py` re-exports `RefuseAllEvidence`, `EvidenceHarness`, the new exits.

## 5. `swf.store` and API

- `class EvidenceRow(BaseModel)`: `id: int, task_id: uuid.UUID, kind: str, source: str, command: str | None, selector: str | None, exit_code: int | None, output: str | None, output_truncated: bool, output_ref: str | None, failures: list[str], duration_ms: int | None, workspace_digest: str | None, tool_call_id: str | None, details: dict[str, Any], created_at: datetime`.
- `add_evidence(conn, task_id, *, kind: str, source: str = "harness", command: str | None = None, selector: str | None = None, exit_code: int | None = None, output: str | None = None, output_truncated: bool = False, output_ref: str | None = None, failures: Sequence[str] = (), duration_ms: int | None = None, workspace_digest: str | None = None, tool_call_id: str | None = None, details: dict[str, Any] | None = None) -> EvidenceRow` (backend; failures stored sorted; same txn: event `evidence_recorded {evidence_id, kind, source, exit_code, selector, workspace_digest}`).
- `list_evidence(conn, user_id, task_id) -> list[EvidenceRow]` (as_user; joins `tasks.user_id = user_id`; ordered by id).
- API: `class EvidenceOut(BaseModel): task_id: uuid.UUID; status: str; items: list[store.EvidenceRow]`. `GET /v1/tasks/{task_id}/evidence` → `as_user`: `get_task` None → 404; else `EvidenceOut`. No audit row (read).

## 6. `swf.worker` changes

- `RunnerDeps.evidence: EvidenceGate | None = None` (§0.6).
- `TaskRunner._run`, after setup and `_check_stopped()`: if `deps.evidence is None`: `harness = TaskHarness(env=env, channel=channel, sink=PgEvidenceSink(deps.db, task.id, is_stopped=lambda: self.stopped), repo=ws.repo, root=ws.root, test_command=test_command, failing_test=task.failing_test, output_limit=self.agent_config.output_limit, clock=deps.clock)`; `self._with_retries("baseline", lambda attempt: harness.run_baseline())`; `harness.take_start_snapshot()`. `self.harness` attribute (None when opted out). The gate passed to `WebAgent` is `harness or deps.evidence`.
- `Submitted` → `transition("confirming", phase="confirm")`; no harness → `completed` (M4). With a harness: `_confirm(harness)`: up to `max_retries` calls of `harness.confirm()`; `HarnessInfraError` → event `retry {step: "harness_rerun", attempt, error}`, backoff via `deps.sleep`; exhausted → `failed("harness_rerun_error")`. `passed` → `completed`; else `failed(f"harness_mismatch: {reason}; post-fix exit 0, harness re-run exit {exit_code}")` (`exit_code` may print `None`).
- `_EXIT_ERRORS` additions (§1). A stopped runner writes no evidence (sink guard) and no status (M4 A2).

## 7. Tests (tester). Fakes in `tests/m5_fakes.py` (imports `tests/m4_fakes.py`)

- `MemorySink`: `EvidenceSink` in memory; `records: list[EvidenceRecord]`, ids 1, 2, ….
- `harness_for(env: FakeEnv, channel: FakeChannel, tmp_path, *, failing_test=FAILING_TEST, test_command=TEST_COMMAND) -> tuple[TaskHarness, MemorySink]` (`repo = env.root`, `root = tmp_path / "ws"`).
- `make_harness_agent(steps, *, bash=…, failing_test=…, **cfg) -> (WebAgent, ScriptedModel, FakeEnv, FakeChannel, TaskHarness, MemorySink)`: runs `run_baseline` + `take_start_snapshot`, then builds the agent with `evidence=harness`.
- `scripted_bash(rules: Sequence[tuple[str, Callable[[int], tuple[str, int]]]])`: a `FakeEnv` bash callable matching by substring with a per-rule call counter (baseline fails, post passes, re-run fails, …).
- `pytest_out(*failed: str) -> str`: realistic `pytest -q` output with `FAILED <id> - AssertionError` summary lines.
- `reference_steps(seed: str) -> list[Step]`: `read_file`, `submit_plan`, one `write_file` per file in `seeded_repos/<seed>/reference/` (same relative path in `repo/`), `run_tests(selector=target)` (A1 switch), `run_tests()` (suite), `complete_task`. Reads `seed.yaml` for the target.
- Reused: `FakeClock`, `FakeEnv`, `FakeChannel`, `ScriptedModel`, `call`, `AutoDecider`, `queued_task`, `make_db_user`, `task_row`.

| File | Must cover |
|---|---|
| `test_m5_snapshot.py` | digest stable and order-independent; ignore set (§0.9) incl. nested `__pycache__`, `.git/index`; symlink recorded as link, target never read (link to a file outside root); added/removed/modified; `as_payload` limit + counts; `copy_tree` replaces dst and keeps links; `unified_diff` added/removed/modified/binary/symlink headers, empty when unchanged |
| `test_m5_rules.py` | `parse_failures` (FAILED/ERROR, ` - msg` stripped, param ids `t[a-b]`, none); `covers_target` table; `harness_command` = preamble + quoted selector, `NoTestCommand` without test command; `bundle_problems`: every code alone, complete bundle → `[]`, stale-digest runs ignored, latest current run wins (flaky pass-then-fail → `post_failing`), only-baseline failures in suite → ok, failing_test None path |
| `test_m5_harness.py` | `run_baseline` rows (kinds, source, exit codes, tail + `output_ref` file, `shared_run` when no target); target passing → `BaselineNotReproduced` + event; infra → `HarnessInfraError` and zero rows; `record_run` phase/selector filter and digest; `check` refusal text (`REFUSAL_PREFIX`, codes) and `evidence_check` events; accept → `diff` row with the fix diff; `status_text`; `confirm` pass, `rerun_failed`, `workspace_changed`, infra; preamble present in every harness command |
| `test_m5_agent.py` | `complete_task` refused without the bundle (no post, no suite, stale after a later `edit_file`, stale after a bash writer in verify) and accepted once complete; refusal observation lists codes; `record_evidence` returns status, emits `evidence_note`, writes no row; **backstop**: a write in investigate (approved `python -c` writing a file, scripted to touch `env.root`) → `ReadOnlyViolation`, `backstop_violation` lists the path, no `plan` approval requested; clean investigate → plan approval as M4; default `WebAgent` (no gate) refuses completion; `AcceptAllEvidence` keeps M4 behaviour |
| `test_m5_store_api.py` (DB) | `add_evidence` round trip + `evidence_recorded` event; constraints (bad kind, bad source, bad digest, failures not array → error); `list_evidence` order; RLS: user B sees none of A's rows via `as_user`; `GET /v1/tasks/{id}/evidence` 200 shape, 404 for another user's and an unknown task, 401 without JWT |
| `test_m5_worker.py` (DB, fakes) | runner with the default harness → `completed`, rows `baseline, baseline_suite, post, suite, diff, harness_rerun` in order; `baseline_not_reproduced`; `no_test_command`; baseline infra 3× → system question → `retry` → ok; harness mismatch → `failed`, `error` starts `harness_mismatch`, `harness_discrepancy` event; re-run infra 3× → `harness_rerun_error`; backstop → `readonly_violation` with partial evidence kept; opt-out (`AcceptAllEvidence`) → no evidence rows |
| `test_m5_e2e.py` (`docker`, `network`, DB) | **DoD**: seeds `01-paginate-off-by-one` and `02-mutable-default` via `Worker.run_once()` + `open_task_sandbox` (session `egress_config`) + `LocalCopyWorkspace(seed repo)` + `reference_steps` + `AutoDecider` → `completed`; baseline exit ≠ 0, post/suite/re-run exit 0, diff touches only the reference file(s); a planted `__pycache__/` in the repo is gone after the run; backstop on seed 01 with a real write in investigate → `failed("readonly_violation")`; 0 Docker leftovers |

- Tests run with `PYTHONDONTWRITEBYTECODE=1 uv run pytest -m "not live" tests/test_m5_*.py`; M4 + full suite stay green.
- `pyproject.toml`: no new marker needed (`docker`, `network` exist); the tester may add one only if unavoidable.

## 8. File ownership (exclusive; coder and tester branch from this commit and merge later)

- **Coder:** `src/swf/evidence.py` (new), `src/swf/agent/{__init__,channel,web_agent,prompts}.py`, `src/swf/worker.py`, `src/swf/store.py`, `src/swf/api/app.py`, `supabase/migrations/20261122000000_m5_evidence.sql` (new). Not `tools.py`, `policy.py`, `sandbox.py`, any test.
- **Tester:** `tests/test_m5_*.py` (new), `tests/m5_fakes.py` (new), `pyproject.toml [tool.pytest.ini_options]` markers only, and the §0.6 opt-out lines in `tests/m4_fakes.py`, `tests/test_m4_worker.py`, `tests/test_m4_live_smoke.py` (nothing else in them).
- **Checker:** owns nothing; reports contract deviations, runs ruff, ruff format check, mypy strict, the M4 + M5 suites and a mutation pass on the gate, backstop and re-run.
- **Planner:** `docs/` (this file; plan §18 M5 status and HANDOVER at close).
- Rules as m4 §11: work only in your own worktree; no push; LF endings (`newline="\n"`); no `uv add`; Docker names `swf-test-*` / `swf-coder-*`; mypy strict and ruff clean.
