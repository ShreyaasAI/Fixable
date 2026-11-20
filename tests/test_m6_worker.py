"""`TaskRunner` with the GitHub workspace and publisher (m6-contract §1, §7, §8): the happy path
to a recorded PR, and every way it fails. Private database clone, scripted model, `FakeEnv`, a local
bare remote and a fake REST API instead of GitHub."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from m4_fakes import (
    FAILING_TEST,
    FIX_NEW,
    AutoDecider,
    FakeEnv,
    ScriptedModel,
    Step,
    call,
    finish_steps,
    fix_steps,
    investigate_steps,
    queued_task,
    run_tests_call,
    status_events,
    task_row,
)
from m5_fakes import (
    events_of_type,
    fake_pytest_output,
    is_harness_suite,
    is_harness_target,
    is_suite,
    is_target,
    pager_bash,
    scripted_bash,
    write_source,
)
from m6_fakes import (
    SENTINEL_TOKEN,
    FakeGitHub,
    bare_remote,
    bare_tree,
    clone_plain,
    git,
    ref_sha,
    refs,
    scan_for,
    write_files,
)
from swf.gitops import GitOps, branch_name
from swf.pr import GitHubPublisher, GitHubWorkspace, TokenUnavailable

from swf import store
from swf.db import Database
from swf.worker import EnvRequest, RunnerDeps, TaskRunner, WorkerConfig

pytest_plugins = ["m4_fakes", "m5_fakes"]

PR_URL = "https://github.com/octo/widgets/pull/1"
BIN_COMMAND = "python -c \"open('blob.bin','wb').write(b'x')\""


@dataclass
class Rig:
    deps: RunnerDeps
    workspaces: Path
    base: str
    bare: Path
    gh: FakeGitHub
    requests: list[EnvRequest] = field(default_factory=list)
    envs: list[FakeEnv] = field(default_factory=list)


def whole_flow() -> list[Step]:
    return [*investigate_steps(), *fix_steps(), [run_tests_call(None, "suite1")], *finish_steps()]


def binary_flow() -> list[Step]:
    return [
        *investigate_steps(),
        *fix_steps(),
        [call("bash", "binw", command=BIN_COMMAND)],
        [run_tests_call(None, "suite1")],
        *finish_steps(),
    ]


def binary_bash(root: Path) -> Any:
    inner = pager_bash(root)

    def bash(command: str) -> dict[str, Any]:
        if "blob.bin" in command:
            (root / "blob.bin").write_bytes(b"\x00\x01\x02")
            return {"output": "", "returncode": 0, "exception_info": ""}
        return inner(command)

    return bash


def mismatch_bash(root: Path) -> Any:
    """Baseline and re-run fail; the agent's own runs pass."""
    return scripted_bash(
        [
            (is_harness_target, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_harness_suite, lambda n: (fake_pytest_output(FAILING_TEST), 1)),
            (is_target, lambda n: (fake_pytest_output(), 0)),
            (is_suite, lambda n: (fake_pytest_output(), 0)),
        ]
    )


def make_rig(
    db: Database,
    tmp_path: Path,
    *,
    steps: Callable[[], list[Step]] = whole_flow,
    bash: Callable[[Path], Any] = pager_bash,
    token: Callable[[store.Task], str] | None = None,
    with_publisher: bool = True,
    remote_repo: str = "octo/widgets",
) -> Rig:
    source = write_source(tmp_path)
    files = {
        p.relative_to(source).as_posix(): p.read_text(encoding="utf-8")
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    base, bare = bare_remote(tmp_path, files, repo=remote_repo)
    workspaces = tmp_path / "workspaces"
    gh = FakeGitHub()
    rig = Rig(deps=None, workspaces=workspaces, base=base, bare=bare, gh=gh)  # type: ignore[arg-type]
    tokens = token if token is not None else (lambda _t: SENTINEL_TOKEN)
    git = GitOps(clone_base=base, allow_file=True)

    def env_factory(request: EnvRequest) -> FakeEnv:
        rig.requests.append(request)
        env = FakeEnv(request.workspace, bash(request.workspace))
        rig.envs.append(env)
        return env

    rig.deps = RunnerDeps(
        db=db,
        workspaces=GitHubWorkspace(git, tokens, workspaces),
        env_factory=env_factory,
        model_factory=lambda: ScriptedModel(steps()),
        config=WorkerConfig(
            worker_id="w1",
            workspaces_root=workspaces,
            poll_seconds=0.05,
            agent={"poll_seconds": 0.02, "retry_backoff_seconds": []},
        ),
        publisher=GitHubPublisher(
            git,
            gh.client(),
            tokens,
            sleep=lambda _s: None,
        )
        if with_publisher
        else None,
    )
    return rig


def run_task(rig: Rig, db: Database, task: store.Task | None = None) -> tuple[store.Task, str]:
    task = task or queued_task(db)
    with db.backend() as conn:
        claimed = store.claim_next_task(conn, worker_id="w1", lease_seconds=90)
    assert claimed is not None and claimed.id == task.id
    with AutoDecider(db):
        status = TaskRunner(rig.deps, claimed).run()
    return task, status


def error_of(db: Database, task: store.Task) -> str:
    return str(task_row(db, task.id)["error"])


def status_path(db: Database, task: store.Task) -> list[str]:
    return [e["to"] for e in status_events(db, task.id)]


# --- happy path -----------------------------------------------------------------------------


def test_a_task_clones_fixes_and_opens_a_pr(fresh_db: Database, tmp_path: Path) -> None:
    rig = make_rig(fresh_db, tmp_path)
    main_before = ref_sha(rig.bare, "refs/heads/main")
    task, status = run_task(rig, fresh_db)
    assert status == "completed", error_of(fresh_db, task)
    assert status_path(fresh_db, task)[-4:] == [
        "verifying",
        "confirming",
        "opening_pr",
        "completed",
    ]
    (cloned,) = events_of_type(fresh_db, task.id, "repo_cloned")
    assert cloned == {"repo": "octo/widgets", "base_branch": "main", "base_sha": main_before}
    (opened,) = events_of_type(fresh_db, task.id, "pr_opened")
    assert opened["url"] == PR_URL and opened["branch"] == branch_name(task.id)
    assert task_row(fresh_db, task.id)["pr_url"] == PR_URL
    branch = f"refs/heads/{branch_name(task.id)}"
    assert refs(rig.bare) == {"refs/heads/main", branch}
    assert ref_sha(rig.bare, "refs/heads/main") == main_before
    assert FIX_NEW in bare_tree(rig.bare, branch)["pager.py"].decode()
    (pull,) = rig.gh.pulls
    assert pull["head"] == branch_name(task.id) and pull["base"] == "main"
    assert "Evidence" in pull["body"] and "AssertionError" not in pull["body"]


def test_no_token_reaches_the_env_request_or_the_workspace(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path)
    task, status = run_task(rig, fresh_db)
    assert status == "completed", error_of(fresh_db, task)
    assert len(rig.requests) == 1
    assert SENTINEL_TOKEN not in repr(rig.requests[0])
    assert scan_for(rig.workspaces, SENTINEL_TOKEN.encode()) == []
    assert scan_for(tmp_path / "remote", SENTINEL_TOKEN.encode()) == []


def test_without_a_publisher_the_task_completes_with_no_pr(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, with_publisher=False)
    task, status = run_task(rig, fresh_db)
    assert status == "completed", error_of(fresh_db, task)
    assert "opening_pr" not in status_path(fresh_db, task)
    assert task_row(fresh_db, task.id)["pr_url"] is None
    assert events_of_type(fresh_db, task.id, "pr_opened") == []
    assert rig.gh.calls == [] and refs(rig.bare) == {"refs/heads/main"}


# --- token failures -------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["github_token_missing", "github_token_invalid"])
def test_a_token_problem_fails_the_task_before_any_container(
    code: str, fresh_db: Database, tmp_path: Path
) -> None:
    def tokens(_task: store.Task) -> str:
        raise TokenUnavailable(code)

    rig = make_rig(fresh_db, tmp_path, token=tokens)
    task, status = run_task(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == code
    assert rig.envs == [] and rig.requests == []


def test_a_401_when_opening_the_pr_is_github_token_invalid(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path)
    rig.gh.script = [(401, {"message": "Bad credentials"})]
    task, status = run_task(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == "github_token_invalid"
    assert task_row(fresh_db, task.id)["pr_url"] is None


# --- clone failure --------------------------------------------------------------------------


def test_a_clone_that_keeps_failing_is_retried_then_clone_failed(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, remote_repo="other/repo")  # octo/widgets does not exist
    task, status = run_task(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == "clone_failed"
    retries = [e for e in events_of_type(fresh_db, task.id, "retry") if e["step"] == "clone"]
    assert [e["attempt"] for e in retries] == [1, 2, 3]
    assert rig.envs == []
    assert SENTINEL_TOKEN not in str(retries)
    assert events_of_type(fresh_db, task.id, "repo_cloned") == []


# --- PR failures ----------------------------------------------------------------------------


def test_a_rejected_push_is_pr_failed_push(fresh_db: Database, tmp_path: Path) -> None:
    rig = make_rig(fresh_db, tmp_path)
    task = queued_task(fresh_db)
    # an unrelated commit already sits on the agent branch of this task
    work = tmp_path / "squatter"
    clone_plain(rig.base, "octo/widgets", work)
    write_files(work, {"squat.txt": "x\n"})
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "squat", cwd=work)
    git("push", "-q", "origin", f"HEAD:refs/heads/{branch_name(task.id)}", cwd=work)
    squatted = ref_sha(rig.bare, f"refs/heads/{branch_name(task.id)}")
    _, status = run_task(rig, fresh_db, task)
    assert status == "failed"
    assert error_of(fresh_db, task) == "pr_failed: push"
    assert ref_sha(rig.bare, f"refs/heads/{branch_name(task.id)}") == squatted
    assert rig.gh.calls == []
    assert status_path(fresh_db, task)[-2:] == ["opening_pr", "failed"]


def test_a_binary_file_in_the_change_fails_the_pr_by_name(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, steps=binary_flow, bash=binary_bash)
    task, status = run_task(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task) == "pr_failed: unsupported_change: blob.bin"
    assert rig.gh.calls == [] and refs(rig.bare) == {"refs/heads/main"}


def test_a_harness_mismatch_pushes_nothing_and_calls_no_api(
    fresh_db: Database, tmp_path: Path
) -> None:
    rig = make_rig(fresh_db, tmp_path, bash=mismatch_bash)
    task, status = run_task(rig, fresh_db)
    assert status == "failed"
    assert error_of(fresh_db, task).startswith("harness_mismatch")
    assert "opening_pr" not in status_path(fresh_db, task)
    assert rig.gh.calls == []
    assert refs(rig.bare) == {"refs/heads/main"}
    assert task_row(fresh_db, task.id)["pr_url"] is None
