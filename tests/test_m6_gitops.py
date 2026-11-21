"""`swf.gitops` (m6-contract §2, §0.2, §0.3, §0.12, §0.13): hermetic env, argv shape, the askpass
helper, token hygiene on disk, repo-name and branch rules."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from m6_fakes import (
    DEFAULT_FILES,
    REPO,
    SENTINEL_TOKEN,
    argv_runner,
    bare_remote,
    git,
    new_git_ops,
    ref_sha,
    scan_for,
)

from swf.gitops import (
    GitOps,
    GitOpsError,
    InvalidRepoName,
    askpass_script,
    branch_name,
    normalize_repo,
    redact,
    validate_repo_name,
)

TASK = uuid.UUID("12345678-1234-5678-1234-567812345678")
BRANCH = f"agent/{TASK}"
BASE_KEYS = {
    "PATH",
    "HOME",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_TERMINAL_PROMPT",
    "LC_ALL",
}
WINDOWS_KEYS = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC"}


# --- env ------------------------------------------------------------------------------------


def test_env_has_exactly_the_hermetic_keys_and_no_token_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWF_GIT_TOKEN", "leak")
    monkeypatch.setenv("GITHUB_TOKEN", "leak")
    monkeypatch.setenv("GIT_DIR", "leak")
    env = GitOps().env(None)
    assert set(env) <= BASE_KEYS | WINDOWS_KEYS
    assert set(env) >= BASE_KEYS
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["LC_ALL"] == "C"
    home = Path(env["HOME"])
    assert home.is_dir() and not any(home.iterdir())
    assert "leak" not in env.values()


def test_env_with_a_token_adds_only_askpass_and_the_token() -> None:
    plain, with_token = GitOps().env(None), GitOps().env(SENTINEL_TOKEN)
    assert set(with_token) - set(plain) == {"GIT_ASKPASS", "SWF_GIT_TOKEN"}
    assert with_token["SWF_GIT_TOKEN"] == SENTINEL_TOKEN
    assert Path(with_token["GIT_ASKPASS"]) == askpass_script()


def test_env_does_not_inherit_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWF_PROBE_VAR", "x")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/some/trap")
    env = GitOps().env(None)
    assert "SWF_PROBE_VAR" not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


# --- argv -----------------------------------------------------------------------------------

COMMON = [
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "credential.helper=",
    "protocol.allow=never",
    "protocol.https.allow=always",
]


def c_options(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-c" and i + 1 < len(argv)]


def test_every_call_carries_the_hermetic_options_and_never_the_token(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    ops = GitOps(runner=argv_runner(calls))
    ops.clone(REPO, "main", tmp_path / "dest", token=SENTINEL_TOKEN)
    ops.head(tmp_path)
    ops.prepare_branch(tmp_path, branch=BRANCH, base_sha="abc")
    ops.has_staged_changes(tmp_path)
    ops.commit(tmp_path, "msg")
    ops.push(tmp_path, BRANCH, token=SENTINEL_TOKEN)
    assert len(calls) >= 6
    for argv, _env in calls:
        opts = c_options(argv)
        for expected in COMMON:
            assert expected in opts, (argv, expected)
        assert "protocol.file.allow=always" not in opts
        assert not any(SENTINEL_TOKEN in a for a in argv)
        assert not any("x-access-token" in a for a in argv)


def test_allow_file_adds_the_file_protocol_option(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    GitOps(allow_file=True, runner=argv_runner(calls)).head(tmp_path)
    assert "protocol.file.allow=always" in c_options(calls[0][0])


def test_clone_argv(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    GitOps(runner=argv_runner(calls)).clone(REPO, "dev", tmp_path / "d", token=SENTINEL_TOKEN)
    argv, env = calls[0]
    assert "clone" in argv
    assert any(a.startswith("--template=") for a in argv)
    for flag in ("--no-tags", "--single-branch", "--no-recurse-submodules"):
        assert flag in argv
    assert argv[argv.index("--branch") + 1] == "dev"
    assert f"https://github.com/{REPO}.git" in argv
    assert env["SWF_GIT_TOKEN"] == SENTINEL_TOKEN  # network call: token only in env


def test_commit_argv_and_identity(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    GitOps(runner=argv_runner(calls)).commit(tmp_path, "msg")
    argv, env = calls[0]
    assert "--no-verify" in argv
    opts = c_options(argv)
    assert "commit.gpgsign=false" in opts
    assert "user.name=SWF Agent" in opts
    assert "user.email=swf-agent@users.noreply.github.com" in opts
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "SWF Agent"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"]
    assert "SWF_GIT_TOKEN" not in env  # local call: no token


def test_push_argv_is_one_unforced_ref(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    GitOps(runner=argv_runner(calls)).push(tmp_path, BRANCH, token=SENTINEL_TOKEN)
    argv, env = calls[0]
    assert "push" in argv and "--no-verify" in argv
    refspecs = [a for a in argv if "refs/heads/" in a]
    assert refspecs == [f"refs/heads/{BRANCH}:refs/heads/{BRANCH}"]
    assert not any(a in ("--force", "-f", "--force-with-lease", "--mirror", "--all") for a in argv)
    assert not any(a.startswith("+") for a in argv)
    assert env["SWF_GIT_TOKEN"] == SENTINEL_TOKEN


def test_git_failures_are_redacted(tmp_path: Path) -> None:
    def runner(argv: Any, **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        err = f"fatal: unable to access 'https://x:{SENTINEL_TOKEN}@github.com/': denied".encode()
        return subprocess.CompletedProcess(argv, 128, b"", err)

    with pytest.raises(GitOpsError) as exc:
        GitOps(runner=runner).push(tmp_path, BRANCH, token=SENTINEL_TOKEN)
    assert exc.value.stage == "push"
    assert SENTINEL_TOKEN not in str(exc.value)
    assert "***" in str(exc.value)


# --- askpass --------------------------------------------------------------------------------


def ask(prompt: str, token: str | None) -> str:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh on PATH")
    env = {"PATH": os.environ["PATH"]}
    if token is not None:
        env["SWF_GIT_TOKEN"] = token
    run = subprocess.run(
        [sh, str(askpass_script()), prompt], env=env, capture_output=True, text=True, check=True
    )
    return run.stdout.strip()


def test_askpass_answers_the_username_and_password_prompts() -> None:
    assert ask("Username for 'https://github.com':", SENTINEL_TOKEN) == "x-access-token"
    assert (
        ask("Password for 'https://x-access-token@github.com':", SENTINEL_TOKEN) == SENTINEL_TOKEN
    )


def test_askpass_script_is_static_and_holds_no_secret() -> None:
    script = askpass_script()
    assert script == askpass_script()
    text = script.read_text(encoding="utf-8")
    assert "SWF_GIT_TOKEN" in text and "x-access-token" in text
    assert SENTINEL_TOKEN not in text and "ghs_" not in text


# --- token hygiene on disk ------------------------------------------------------------------


def test_clone_leaves_no_token_anywhere(tmp_path: Path) -> None:
    base, bare = bare_remote(tmp_path, DEFAULT_FILES)
    dest = tmp_path / "ws" / "repo"
    dest.parent.mkdir()
    ops = new_git_ops(base)
    ops.clone(REPO, "main", dest, token=SENTINEL_TOKEN)
    assert (dest / "pager.py").is_file()
    assert ops.head(dest) == ref_sha(bare, "refs/heads/main")
    url = git("config", "remote.origin.url", cwd=dest)
    assert url == f"{base}/{REPO}.git"
    assert "@" not in url
    assert scan_for(tmp_path, SENTINEL_TOKEN.encode()) == []
    assert "ghs_" not in (dest / ".git" / "config").read_text(encoding="utf-8")


def test_a_failed_clone_removes_dest_and_raises_clone(tmp_path: Path) -> None:
    base, _ = bare_remote(tmp_path)
    dest = tmp_path / "gone"
    with pytest.raises(GitOpsError) as exc:
        new_git_ops(base).clone("octo/missing", "main", dest, token=SENTINEL_TOKEN)
    assert exc.value.stage == "clone"
    assert not dest.exists()
    assert SENTINEL_TOKEN not in str(exc.value)


def test_clone_of_a_missing_branch_fails(tmp_path: Path) -> None:
    base, _ = bare_remote(tmp_path)
    with pytest.raises(GitOpsError):
        new_git_ops(base).clone(REPO, "nope", tmp_path / "d", token=SENTINEL_TOKEN)


# --- redact, names, branch ------------------------------------------------------------------


def test_redact_replaces_every_occurrence_and_handles_none() -> None:
    text = f"a {SENTINEL_TOKEN} b {SENTINEL_TOKEN}"
    assert redact(text, SENTINEL_TOKEN) == "a *** b ***"
    assert redact("plain", None) == "plain"
    assert redact("plain", "") == "plain"


@pytest.mark.parametrize(
    "value",
    ["ShreyaasAI/swf-pr-sandbox", "a/b", "octo/wid.gets_x-1", "a-b/.github"],
)
def test_valid_repo_names(value: str) -> None:
    assert validate_repo_name(value) == value


@pytest.mark.parametrize(
    "value",
    ["../x", "a/..", "a/.", "a/b/c", "a/b?x", "a b/c", "/b", "a/", "", "a/b\n", "a_b/c"],
)
def test_invalid_repo_names(value: str) -> None:
    with pytest.raises(InvalidRepoName):
        validate_repo_name(value)


def test_normalize_repo_accepts_owner_name_and_github_urls() -> None:
    assert normalize_repo("ShreyaasAI/swf-pr-sandbox") == "ShreyaasAI/swf-pr-sandbox"
    for url in (
        "https://github.com/ShreyaasAI/swf-pr-sandbox",
        "https://github.com/ShreyaasAI/swf-pr-sandbox.git",
        "https://github.com/ShreyaasAI/swf-pr-sandbox/",
    ):
        assert normalize_repo(url) == "ShreyaasAI/swf-pr-sandbox"


@pytest.mark.parametrize(
    "value",
    [
        "../x",
        "a/..",
        "a/b/c",
        "a/b?x",
        "http://evil/a/b",
        "https://evil.com/a/b",
        "git@github.com:a/b",
        "https://github.com/a/b/tree/main",
        "https://user@github.com/a/b",
        "ftp://github.com/a/b",
    ],
)
def test_normalize_repo_rejects_everything_else(value: str) -> None:
    with pytest.raises(InvalidRepoName):
        normalize_repo(value)


def test_the_clone_url_is_validated_before_it_is_built() -> None:
    with pytest.raises(InvalidRepoName):
        GitOps().remote_url("../etc/passwd")
    assert GitOps().remote_url(REPO) == f"https://github.com/{REPO}.git"
    assert GitOps(clone_base="file:///r").remote_url(REPO) == f"file:///r/{REPO}.git"


def test_branch_name_is_agent_slash_task_id() -> None:
    assert branch_name(TASK) == f"agent/{TASK}"
    assert branch_name(uuid.UUID(int=0)) == "agent/00000000-0000-0000-0000-000000000000"
