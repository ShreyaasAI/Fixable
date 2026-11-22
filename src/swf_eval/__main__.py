"""`python -m swf_eval <command>` (m9-contract §1.5, §3 T8).

Every command that names or reads a repo runs `check_eval_repo` before any git, network or
subprocess call. Heavy imports (sandbox, worker, Docker helpers) happen inside the commands.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swf.github import GitHubError
from swf.gitops import GitOpsError
from swf_eval.budget import Budget, SharedBudget
from swf_eval.metrics import Pass, TaskRecord
from swf_eval.resolve import Resolution
from swf_eval.seeds import Seed, SeedBranchConflict, check_eval_repo, load_seeds, seed_branch

if TYPE_CHECKING:
    from swf.egress import EgressConfig
    from swf_eval.client import EvalClient
    from swf_eval.resolve import Runner

EVAL_MODEL = "@glm-4-7-flash/glm-4.7-flash"
AGENT_LIMITS = {"step_limit": 80, "wall_time_limit_seconds": 1800}
# Infra failures get one re-run (§0.13); agent outcomes never do (`pr_failed: empty_diff`,
# `pr_failed: unsupported_change: ...`, limits, retries_exhausted, any other internal_error).
_INFRA_RE = re.compile(
    r"^(?:clone_failed"
    r"|pr_failed: (?!empty_diff$|unsupported_change\b)[a-z_]+"
    r"|internal_error: (?:EgressError|Sandbox[A-Za-z]*Error))$",
    re.ASCII,
)
BUDGET_CHECK_SECONDS = 30.0


def is_infra_failure(status: str, error: str | None) -> bool:
    return status == "failed" and bool(_INFRA_RE.fullmatch(error or ""))


class EvalError(Exception):
    """A refused or failed eval step; the message never holds a secret."""


# --- small helpers -------------------------------------------------------------------------


def _out(argv: Sequence[str]) -> str:
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise EvalError(f"{argv[0]} {argv[1]} failed (exit {proc.returncode})")
    return proc.stdout.strip()


def gh_token() -> str:
    token = _out(["gh", "auth", "token"])
    if not token:
        raise EvalError("gh auth token returned nothing")
    return token


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def select_seeds(spec: str | None) -> list[Seed]:
    seeds = load_seeds()
    if not spec:
        return seeds
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    chosen = [s for s in seeds if s.name[:2] in wanted]
    if len(chosen) != len(wanted):
        raise EvalError(f"unknown seed numbers in {spec!r}")
    return chosen


def record_to_json(r: TaskRecord) -> dict[str, Any]:
    return dataclasses.asdict(r)


def record_from_json(d: dict[str, Any]) -> TaskRecord:
    res = d.get("resolution")
    resolution = None
    if res is not None:
        resolution = Resolution(
            setup_ok=bool(res["setup_ok"]),
            target_exit=res["target_exit"],
            suite_exit=res["suite_exit"],
            tests_touched=tuple(res["tests_touched"]),
            files_added=tuple(res["files_added"]),
        )
    return TaskRecord(**{**d, "resolution": resolution})


def task_file(out: Path, r: TaskRecord) -> Path:
    return out / "tasks" / f"{r.pass_}-{r.seed}.json"


def load_run(out: Path) -> tuple[dict[str, Any], list[TaskRecord]]:
    run = read_json(out / "run.json")
    if not isinstance(run, dict):
        raise EvalError("run.json is not an object")
    records = [record_from_json(read_json(p)) for p in sorted((out / "tasks").glob("*.json"))]
    return run, records


def egress_up() -> EgressConfig:
    from swf.egress import EgressConfig, ensure_egress

    egress = EgressConfig()
    ensure_egress(egress)
    return egress


def runner_for(egress: EgressConfig) -> Runner:
    from swf_eval.resolve import sandbox_runner

    return sandbox_runner(egress)


# --- seed-branches, warm-cache, controls ---------------------------------------------------


def cmd_seed_branches(args: argparse.Namespace) -> int:
    from swf.gitops import GitOps
    from swf_eval.seeds import publish_seed_branches

    repo = check_eval_repo(args.repo)
    token = gh_token()
    with tempfile.TemporaryDirectory(prefix="swf-eval-seeds-") as work:
        shas = publish_seed_branches(GitOps(), repo, token, load_seeds(), Path(work))
    for name, sha in shas.items():
        print(f"seed/{name} {sha}")
    return 0


def warm_cache(runner: Runner, seeds: Sequence[Seed]) -> None:
    """Every seed's setup in a sandbox (no LLM), so neither pass gets a cold cache (§0.13)."""
    from swf.config import load_agents_file

    for seed in seeds:
        setup = load_agents_file(seed.repo_dir).config.setup
        with tempfile.TemporaryDirectory(prefix="swf-eval-warm-") as tmp:
            tree = Path(tmp) / "repo"
            shutil.copytree(seed.repo_dir, tree)
            exits = runner(seed, tree, setup)
        if exits != [0] * len(setup):
            raise EvalError(f"warm-cache: setup of {seed.name} failed: {exits}")


def cmd_warm_cache(args: argparse.Namespace) -> int:
    warm_cache(runner_for(egress_up()), select_seeds(args.seeds))
    print("cache warmed")
    return 0


def cmd_controls(args: argparse.Namespace) -> int:
    from swf_eval.resolve import run_controls

    runner = runner_for(egress_up())
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="swf-eval-controls-") as tmp:
        for seed in select_seeds(args.seeds):
            pristine, reference = run_controls(seed, runner, Path(tmp) / seed.name)
            ok = (
                pristine.setup_ok
                and pristine.target_exit not in (0, None)
                and not pristine.resolved
                and reference.resolved
            )
            rows.append(
                {
                    "seed": seed.name,
                    "pristine_target_exit": pristine.target_exit,
                    "reference_resolved": reference.resolved,
                    "ok": ok,
                }
            )
            print(f"{seed.name}: pristine target {pristine.target_exit}, "
                  f"reference resolved {reference.resolved}{'' if ok else '  FAILED'}")  # fmt: skip
    if args.out:
        write_json(Path(args.out) / "controls.json", rows)
    return 0 if all(r["ok"] for r in rows) else 1


# --- run -----------------------------------------------------------------------------------


def docker_host() -> dict[str, Any]:
    info = json.loads(_out(["docker", "info", "--format", "{{json .}}"]))
    ncpu, mem = int(info.get("NCPU") or 0), int(info.get("MemTotal") or 0)
    host = {"ncpu": ncpu, "mem_total": mem, "runtime": str(info.get("DefaultRuntime") or "")}
    if ncpu < 2 or mem < 4 * 2**30:
        raise EvalError(f"Docker has too little capacity for the eval (§0.20): {host}")
    return host


class MemSampler(threading.Thread):
    """Peak `docker stats` memory per task, every 5 s (§0.22); informational only."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak: dict[str, int] = {}
        self.stop = threading.Event()

    def run(self) -> None:
        from swf_eval.metrics import parse_mem_mb

        while not self.stop.wait(5.0):
            try:
                ps = _out([
                    "docker", "ps", "--filter", "label=swf.managed=1",
                    "--format", '{{.ID}} {{.Label "swf.task"}}',
                ])  # fmt: skip
                tasks = dict(line.split(" ", 1) for line in ps.splitlines() if " " in line)
                if not tasks:
                    continue
                stats = _out(
                    ["docker", "stats", "--no-stream", "--format", "{{.ID}}|{{.MemUsage}}", *tasks]
                )
                for line in stats.splitlines():
                    cid, _, mem = line.partition("|")
                    task = next((t for c, t in tasks.items() if c.startswith(cid)), None)
                    if task:
                        self.peak[task] = max(self.peak.get(task, 0), parse_mem_mb(mem))
            except (EvalError, ValueError, subprocess.SubprocessError, OSError):
                continue


@dataclasses.dataclass
class RunState:
    repo: str
    db_url: str
    budget: SharedBudget
    clients: dict[str, EvalClient]
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    ids: dict[str, tuple[Pass, str, str]] = dataclasses.field(default_factory=dict)
    replaced: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    aborted: str | None = None
    out: Path | None = None  # run dir: run.json and tasks/ are rewritten as tasks finish
    info: dict[str, Any] = dataclasses.field(default_factory=dict)
    records: dict[str, TaskRecord] = dataclasses.field(default_factory=dict)

    def note(self, text: str) -> None:
        with self.lock:
            self.notes.append(text)
        self.write_run()

    def persist(self, record: TaskRecord) -> None:
        """Write the task file and run.json now, so a crash later loses nothing recorded."""
        with self.lock:
            self.records[record.task_id] = record
            if self.out is not None:
                write_json(task_file(self.out, record), record_to_json(record))
        self.write_run()

    def write_run(self) -> None:
        if self.out is None:
            return
        with self.lock:
            kept = [r for tid, r in self.records.items() if tid in self.ids]
            data = {
                **self.info,
                "aborted": self.aborted,
                "ids": dict(self.ids),
                "pr_urls": {r.task_id: r.pr_url for r in kept if r.pr_url},
                "replaced": list(self.replaced),
                "notes": list(self.notes),
                "tasks": sorted({task_file(self.out, r).name for r in kept}),
                "budget": {
                    "usd": self.budget.usd,
                    "calls": self.budget.calls,
                    "prs": self.budget.prs,
                    "elapsed_seconds": round(self.budget.elapsed(), 1),
                },
            }
            write_json(self.out / "run.json", data)

    def check(self) -> bool:
        """True (and record the reason) once any hard stop is reached."""
        with self.lock:
            if self.aborted is None:
                self.aborted = self.budget.exceeded()
            return self.aborted is not None


def _throttled(fn: Callable[[], bool], every: float) -> Callable[[], bool]:
    last = [0.0]

    def check() -> bool:
        now = time.monotonic()
        if now - last[0] < every:
            return False
        last[0] = now
        return fn()

    return check


def run_task(state: RunState, pass_: Pass, seed: Seed, user: str) -> TaskRecord | None:
    from swf_eval.client import CreateRefused
    from swf_eval.localstack import connect
    from swf_eval.metrics import records_from_db

    client = state.clients[user]
    try:
        task_id = client.create_task(
            repo=state.repo,
            base_branch=seed_branch(seed),
            description=seed.description,
            failing_test=seed.target_test,
        )
    except CreateRefused as exc:
        state.note(f"{pass_} {seed.name}: create refused ({exc.status} {exc.detail})")
        return None
    with state.lock:
        state.ids[str(task_id)] = (pass_, seed.name, user)
    state.write_run()
    deadline = time.monotonic() + AGENT_LIMITS["wall_time_limit_seconds"] + 900
    client.drive(task_id, deadline=deadline, stop=_throttled(state.check, BUDGET_CHECK_SECONDS))
    with connect(state.db_url) as conn:
        record = records_from_db(conn, {str(task_id): (pass_, seed.name, user)})[0]
    state.budget.add(
        usd=record.cost_usd,
        calls=record.agent_calls + record.guard_chunks,
        prs=1 if record.pr_url else 0,
    )
    error = record.error or ""
    state.budget.task_done(
        model_failed=record.status == "failed" and error.startswith("retries_exhausted")
    )
    return record


def drive_jobs(state: RunState, pass_: Pass, jobs: Sequence[tuple[Seed, str]]) -> None:
    """Sequential jobs; an infra failure gets one re-run that replaces the record (§0.13).

    Each record is persisted as it finishes. An exception ends this thread's jobs with a note;
    the other driver thread and the run's final write still happen."""
    seed_name = ""
    try:
        for seed, user in jobs:
            seed_name = seed.name
            for attempt in (1, 2):
                if state.check():
                    return
                record = run_task(state, pass_, seed, user)
                if record is None:
                    break
                state.persist(record)
                if not is_infra_failure(record.status, record.error) or attempt == 2:
                    break
                with state.lock:
                    state.ids.pop(record.task_id, None)
                    state.replaced.append(
                        {"pass": pass_, "seed": seed.name, "task_id": record.task_id,
                         "cause": record.error, "status": record.status}
                    )  # fmt: skip
                state.write_run()
    except Exception as exc:
        state.note(f"{pass_} {seed_name}: driver stopped: {type(exc).__name__}: {exc}")


def guard_flags(conn: Any, ids: dict[str, tuple[Pass, str, str]]) -> list[dict[str, str]]:
    """One line per auto-approved guard flag category (§0.16)."""
    flags: list[dict[str, str]] = []
    for task_id, (pass_, seed, _user) in ids.items():
        rows = conn.execute(
            "select payload from public.events where task_id = %s and type = 'approval_requested'"
            " order by seq",
            [uuid.UUID(task_id)],
        ).fetchall()
        for row in rows:
            payload = row["payload"] or {}
            if payload.get("kind") != "guard_flag":
                continue
            for flag in (payload.get("payload") or {}).get("flags") or []:
                for category in flag.get("categories") or []:
                    flags.append({"pass": pass_, "seed": seed, "category": str(category),
                                  "source": str(flag.get("source"))})  # fmt: skip
    return flags


def scan_for_secrets(out: Path, secrets: Sequence[str]) -> list[str]:
    needles = [s.encode() for s in secrets if s]
    hits = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(n in data for n in needles):
                hits.append(str(path.relative_to(out)))
    return hits


def secrets_ok(out: Path, secrets: Sequence[str]) -> bool:
    hits = scan_for_secrets(out, secrets)
    if hits:
        print(f"secret found in the run dir: {', '.join(hits)}", file=sys.stderr)
    return not hits


def cmd_run(args: argparse.Namespace) -> int:
    from swf.github import GitHubClient

    repo = check_eval_repo(args.repo)
    out = Path(args.out)
    if (out / "run.json").exists():
        raise EvalError(f"{out} already holds a run")
    passes: list[Pass] = ["single", "concurrent"] if args.pass_ == "both" else [args.pass_]
    seeds = select_seeds(args.seeds)
    portkey = os.environ.get("PORTKEY_API_KEY") or ""
    if not portkey:
        raise EvalError("PORTKEY_API_KEY is not set (run with uv run --env-file)")
    host = docker_host()
    token = gh_token()
    gh_user = GitHubClient().get_user(token)
    out.mkdir(parents=True, exist_ok=True)
    budget = SharedBudget(
        Budget(
            max_usd=args.budget_usd,
            max_calls=args.max_calls,
            max_prs=args.max_prs,
            max_seconds=args.max_hours * 3600,
        )
    )
    knobs = {
        "SWF_MODEL_INVESTIGATE": EVAL_MODEL,
        "SWF_MODEL_EXECUTE": EVAL_MODEL,
        "SWF_QUOTA_TASKS_PER_DAY": "12",
        "SWF_AGENT_STEP_LIMIT": str(AGENT_LIMITS["step_limit"]),
        "SWF_AGENT_WALL_TIME_SECONDS": str(AGENT_LIMITS["wall_time_limit_seconds"]),
    }
    info: dict[str, Any] = {
        "run_id": out.name,
        "date": datetime.now(UTC).date().isoformat(),
        "git_sha": _out(["git", "rev-parse", "--short", "HEAD"]),
        "repo": repo,
        "host": host,
        "model": EVAL_MODEL,
        "agent_limits": AGENT_LIMITS,
        "cleanup": [],
    }
    controls = out / "controls.json"
    info["controls"] = read_json(controls) if controls.exists() else []
    state = RunState(repo=repo, db_url="", budget=budget, clients={}, out=out, info=info)
    sampler = MemSampler()
    try:
        return _run_passes(state, passes, seeds, token, portkey, gh_user, knobs, sampler)
    finally:
        state.write_run()


def _run_passes(
    state: RunState,
    passes: Sequence[Pass],
    seeds: Sequence[Seed],
    token: str,
    portkey: str,
    gh_user: Any,
    knobs: dict[str, str],
    sampler: MemSampler,
) -> int:
    import httpx

    from swf_eval.client import EvalClient
    from swf_eval.localstack import LocalStack, connect
    from swf_eval.metrics import records_from_db

    assert state.out is not None
    out = state.out
    records: list[TaskRecord] = []
    with LocalStack(out, github_id=gh_user.id, github_login=gh_user.login, knobs=knobs) as stack:
        state.db_url = stack.db_url
        http = {n: httpx.Client(base_url=stack.api_url, timeout=60.0) for n in stack.users}
        try:
            clients = {n: EvalClient(http[n], u.jwt) for n, u in stack.users.items()}
            for client in clients.values():
                client.link_github_token(token)
            state.clients = clients
            runner = runner_for(egress_up())
            sampler.start()
            for pass_ in passes:
                if state.check():
                    break
                warm_cache(runner, seeds)
                if pass_ == "single":
                    users = ["eval-a" if i % 2 == 0 else "eval-b" for i in range(len(seeds))]
                    drive_jobs(state, pass_, list(zip(seeds, users, strict=True)))
                else:
                    odd = [(s, "eval-a") for s in seeds if int(s.name[:2]) % 2 == 1]
                    even = [(s, "eval-b") for s in seeds if int(s.name[:2]) % 2 == 0]
                    threads = [
                        threading.Thread(target=drive_jobs, args=(state, pass_, jobs))
                        for jobs in (odd, even)
                    ]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
            sampler.stop.set()
            with connect(stack.db_url) as conn:
                records = records_from_db(conn, state.ids)
                state.info["guard_flags"] = guard_flags(conn, state.ids)
        finally:
            sampler.stop.set()
            for h in http.values():
                h.close()
    for r in records:
        state.persist(dataclasses.replace(r, peak_mem_mb=sampler.peak.get(r.task_id)))
    if not secrets_ok(out, [token, portkey]):
        return 1
    print(f"run written to {out} ({len(records)} tasks, aborted: {state.aborted or 'no'})")
    return 0


# --- check, report, cleanup ----------------------------------------------------------------


def _run_repo(out: Path) -> tuple[str, dict[str, Any], list[TaskRecord]]:
    run, records = load_run(out)
    return check_eval_repo(str(run.get("repo") or "")), run, records


def cmd_check(args: argparse.Namespace) -> int:
    from swf.gitops import GitOps
    from swf_eval.resolve import check_resolution

    out = Path(args.out)
    repo, _run, records = _run_repo(out)
    todo = [r for r in records if r.status == "completed" and r.pr_url]
    if not todo:
        print("nothing to check")
        return 0
    token = gh_token()
    git = GitOps()
    runner = runner_for(egress_up())
    seeds = {s.name: s for s in load_seeds()}
    failed = 0
    for r in todo:
        work = out / "check" / f"{r.pass_}-{r.seed}"
        try:
            shutil.rmtree(work, ignore_errors=True)
            git.clone(repo, f"agent/{r.task_id}", work / "pr", token=token)
            res = check_resolution(seeds[r.seed], work / "pr", work / "trees", runner)
        except Exception as exc:  # one failed check must not stop the rest
            failed += 1
            msg = f"{type(exc).__name__}: {exc}".replace(token, "***")
            print(f"{r.pass_} {r.seed}: check failed: {msg}", file=sys.stderr)
            continue
        updated = dataclasses.replace(r, resolution=res)
        write_json(task_file(out, updated), record_to_json(updated))
        print(f"{r.pass_} {r.seed}: resolved {res.resolved}")
    if not secrets_ok(out, [token, os.environ.get("PORTKEY_API_KEY") or ""]):
        return 1
    return 1 if failed else 0


def cmd_report(args: argparse.Namespace) -> int:
    from swf_eval.metrics import verdict
    from swf_eval.report import render_results

    out = Path(args.out)
    run, records = load_run(out)
    controls = out / "controls.json"
    if not run.get("controls") and controls.exists():
        run["controls"] = read_json(controls)
    text = render_results(run, records, verdict(records))
    if args.write:
        with open(args.write, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from swf.github import GitHubClient
    from swf_eval.cleanup import apply_cleanup, cleanup_plan

    out = Path(args.out)
    repo, run, records = _run_repo(out)
    plan = cleanup_plan(records, repo)
    seed_refs = [seed_branch(s) for s in load_seeds()] if args.seeds else []
    if not args.apply:
        for number, branch in plan:
            print(f"would close PR {number} and delete {branch}")
        for ref in seed_refs:
            print(f"would delete {ref}")
        return 0
    token = gh_token()
    api = GitHubClient()
    log = apply_cleanup(api, token, repo, plan)
    for ref in seed_refs:
        try:
            api.delete_branch(token, repo, ref)
            log.append(f"deleted branch {ref}")
        except Exception as exc:  # noqa: BLE001 - logged, the next ref still runs
            log.append(f"{ref}: {type(exc).__name__}")
    run["cleanup"] = [*(run.get("cleanup") or []), *log]
    write_json(out / "run.json", run)
    print("\n".join(log))
    return 0 if secrets_ok(out, [token, os.environ.get("PORTKEY_API_KEY") or ""]) else 1


# --- argparse ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swf_eval", description="M9 evaluation harness")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed-branches", help="push seed/<name> branches to the eval repo")
    p.add_argument("--repo", required=True)
    p.set_defaults(func=cmd_seed_branches)

    p = sub.add_parser("warm-cache", help="run every seed's setup once (no LLM)")
    p.add_argument("--seeds")
    p.set_defaults(func=cmd_warm_cache)

    p = sub.add_parser("controls", help="pristine must fail, reference must resolve")
    p.add_argument("--seeds")
    p.add_argument("--out")
    p.set_defaults(func=cmd_controls)

    p = sub.add_parser("run", help="the single and/or concurrent pass")
    p.add_argument("--pass", dest="pass_", choices=["single", "concurrent", "both"], required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seeds")
    p.add_argument("--budget-usd", type=float, default=1.0)
    p.add_argument("--max-calls", type=int, default=4000)
    p.add_argument("--max-prs", type=int, default=22)
    p.add_argument("--max-hours", type=float, default=6.0)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="resolution check of each completed PR")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("report", help="render m9-results.md")
    p.add_argument("--out", required=True)
    p.add_argument("--write")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("cleanup", help="close the run's PRs and delete their branches")
    p.add_argument("--out", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--seeds", action="store_true")
    p.set_defaults(func=cmd_cleanup)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.func(args)
    except (ValueError, EvalError, OSError, GitOpsError, GitHubError, SeedBranchConflict) as exc:
        print(f"swf_eval {args.command}: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
