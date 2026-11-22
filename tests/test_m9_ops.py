"""Static checks on the CI workflow and its JUnit gate (m9 contract §0.7, T9)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "m8-gvisor.yml"
GATE = ROOT / "ops" / "ci_check_junit.py"


def _workflow() -> str:
    text = WORKFLOW.read_bytes().decode("utf-8")
    assert "\r" not in text
    return text


def test_ci_triggers_on_feature_m8_and_m9() -> None:
    assert "branches: [feature/m8, feature/m9]" in _workflow()


def test_ci_runs_pytest_as_the_runner_user_not_uid_1000() -> None:
    text = _workflow()
    assert "getent passwd 1000" not in text
    assert "useradd" not in text
    assert "chown -R 1000:1000" not in text
    assert "sudo -u" not in text
    assert "TEST_USER" not in text
    assert "usermod -aG docker" not in text
    assert "run --locked pytest" in text
    assert "primary gid" in text  # the docker-group note stays


def test_ci_sets_pkgcache_dir_under_runner_temp() -> None:
    assert "SWF_PKGCACHE_DIR: ${{ runner.temp }}/pkgcache" in _workflow()


def test_ci_keeps_the_guard_and_the_offline_suite() -> None:
    text = _workflow()
    assert "if: github.repository == 'ShreyaasAI/swf-ci'" in text
    assert '-m "not live"' in text
    assert "SWF_SANDBOX_RUNTIME=runsc" in text
    assert "-i br-+ -j DROP" in text
    assert "ops/ci_check_junit.py" in text
    assert "secrets." not in text


def _case(classname: str, name: str, skipped: str | None = None, failed: bool = False) -> str:
    inner = ""
    if skipped is not None:
        inner = f'<skipped type="pytest.skip" message="{skipped}"/>'
    if failed:
        inner = '<failure message="boom"/>'
    return f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'


GVISOR = "tests.test_m8_gvisor"
OWNER = "tests.test_m9_workspace_owner"
PKG = "tests.test_m9_pkgcache_docker"


def _run(tmp_path: Path, *cases: str) -> subprocess.CompletedProcess[str]:
    xml = tmp_path / "j.xml"
    xml.write_text(f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>", "utf-8")
    return subprocess.run(
        [sys.executable, str(GATE), str(xml)], capture_output=True, text=True, check=False
    )


def _good() -> list[str]:
    return [_case(GVISOR, "a"), _case(OWNER, "b"), _case(PKG, "c")]


def test_gate_passes_when_all_three_ran(tmp_path: Path) -> None:
    out = _run(tmp_path, *_good())
    assert out.returncode == 0, out.stderr


def test_gate_fails_when_workspace_owner_did_not_run(tmp_path: Path) -> None:
    out = _run(tmp_path, _case(GVISOR, "a"), _case(PKG, "c"))
    assert out.returncode == 1
    assert "test_m9_workspace_owner" in out.stderr


def test_gate_fails_when_workspace_owner_only_skipped(tmp_path: Path) -> None:
    out = _run(tmp_path, _case(GVISOR, "a"), _case(PKG, "c"), _case(OWNER, "b", skipped="x"))
    assert out.returncode == 1
    assert "test_m9_workspace_owner" in out.stderr


def test_gate_fails_when_workspace_owner_failed(tmp_path: Path) -> None:
    out = _run(tmp_path, _case(GVISOR, "a"), _case(PKG, "c"), _case(OWNER, "b", failed=True))
    assert out.returncode == 1


def test_gate_fails_on_any_skipped_pkgcache_docker_case(tmp_path: Path) -> None:
    out = _run(tmp_path, *_good(), _case(PKG, "d", skipped="cannot pull python:3.12-slim"))
    assert out.returncode == 1
    assert "test_m9_pkgcache_docker" in out.stderr


def test_gate_fails_when_no_pkgcache_docker_case_ran(tmp_path: Path) -> None:
    out = _run(tmp_path, _case(GVISOR, "a"), _case(OWNER, "b"))
    assert out.returncode == 1
    assert "test_m9_pkgcache_docker" in out.stderr
