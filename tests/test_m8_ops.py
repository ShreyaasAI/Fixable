"""Static checks on the VM ops scripts (contract §3 T11, T12, §13 A4). No VM needed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "ops"
SCRIPTS = ["vm-bootstrap.sh", "vm-sync.sh", "vm-test.sh"]


def _text(name: str) -> str:
    path = OPS / name
    assert path.is_file(), f"{path} does not exist"
    return path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_header_and_line_endings(name: str) -> None:
    text = _text(name)
    assert "\r" not in text
    lines = text.split("\n")
    assert lines[0] == "#!/usr/bin/env bash"
    assert lines[1] == "set -euo pipefail"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses_with_bash(name: str) -> None:
    _text(name)
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not found")
    out = subprocess.run(
        [bash, "-n", str(OPS / name)], capture_output=True, text=True, timeout=30, check=False
    )
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_never_pushes(name: str) -> None:
    assert "git push" not in _text(name)


def test_sync_uses_a_bundle_and_never_names_env() -> None:
    text = _text("vm-sync.sh")
    assert "git bundle create" in text
    assert ".env" not in text


def test_sync_is_non_interactive() -> None:
    text = _text("vm-sync.sh")
    assert "scp -o BatchMode=yes" in text
    assert "ssh -o BatchMode=yes" in text


def test_bootstrap_installs_gvisor_and_isolates_the_host() -> None:
    text = _text("vm-bootstrap.sh")
    assert "runsc install" in text
    assert "-i br-+ -j DROP" in text
    assert "getent passwd 1000" in text  # the uid 1000 test-user check


def test_bootstrap_is_non_interactive() -> None:
    text = _text("vm-bootstrap.sh")
    assert "DEBIAN_FRONTEND=noninteractive" in text
    assert "debconf-set-selections" in text
    assert "iptables-persistent/autosave_v4" in text
    assert "iptables-persistent/autosave_v6" in text


def test_test_runner_uses_gvisor_and_skips_live() -> None:
    text = _text("vm-test.sh")
    assert "SWF_SANDBOX_RUNTIME=runsc" in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert '-m "not live"' in text


def test_gitignore_covers_the_vm_log_dir() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".m8-vm/" in [line.strip() for line in lines]


WORKFLOW = ROOT / ".github" / "workflows" / "m8-gvisor.yml"


def _workflow() -> str:
    assert WORKFLOW.is_file(), f"{WORKFLOW} does not exist"
    text = WORKFLOW.read_bytes().decode("utf-8")
    assert "\r" not in text
    return text


def test_ci_workflow_runs_the_non_live_suite_under_gvisor() -> None:
    text = _workflow()
    assert "runs-on: ubuntu-24.04" in text
    assert "sudo runsc install" in text
    assert "SWF_SANDBOX_RUNTIME=runsc" in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert '-m "not live"' in text
    assert "-i br-+ -j DROP" in text
    assert "ops/ci_check_junit.py" in text
    assert "label=swf.test=1" in text
    assert "timeout-minutes:" in text


def test_ci_workflow_uses_no_secrets_and_never_pushes() -> None:
    text = _workflow()
    assert "secrets." not in text
    assert "git push" not in text
    assert "persist-credentials: false" in text


def test_ci_workflow_triggers_only_on_dispatch_and_feature_branches() -> None:
    text = _workflow()
    assert "workflow_dispatch:" in text
    assert "branches: [feature/m8, feature/m9]" in text  # m9 T9
    assert "pull_request" not in text and "schedule:" not in text
