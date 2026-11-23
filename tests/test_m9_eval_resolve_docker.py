"""The resolution checker in the real sandbox (m9-contract §0.10, §1.5, §3 T7): controls on seeds
01 and 07 (pristine must not resolve, pristine + `reference/` must), and the cases an agent could
use to fake a pass: deleted tests, and planted `pytest.py` / `pytest.toml` / `conftest.py` /
`sitecustomize.py`. Docker and PyPI (through the egress proxy and package cache) are needed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from m9_eval_fakes import copy_seed_repo, write
from swf_eval.resolve import Runner, check_resolution, run_controls, sandbox_runner
from swf_eval.seeds import Seed, load_seeds

from swf.egress import EgressConfig, EgressInfo

pytestmark = [pytest.mark.docker, pytest.mark.network]

PLANTED = {
    "pytest.py": "raise SystemExit(0)\n",
    "pytest.toml": "[pytest]\naddopts = '--collect-only -q'\n",
    "conftest.py": "def pytest_collection_modifyitems(items):\n    items.clear()\n",
    "sitecustomize.py": "import os\nos._exit(0)\n",
}


@pytest.fixture(scope="module")
def seeds() -> dict[str, Seed]:
    return {s.name[:2]: s for s in load_seeds() if s.name.startswith(("01-", "07-"))}


@pytest.fixture
def runner(egress: EgressInfo, egress_config: EgressConfig) -> Runner:
    return sandbox_runner(egress_config)


@pytest.mark.parametrize("number", ["01", "07"])
def test_controls_pristine_fails_and_reference_resolves(
    seeds: dict[str, Seed], runner: Runner, tmp_path: Path, number: str
) -> None:
    seed = seeds[number]
    pristine, reference = run_controls(seed, runner, tmp_path / "work")
    assert pristine.setup_ok and pristine.target_exit not in (0, None), pristine
    assert not pristine.resolved
    assert reference.setup_ok and reference.target_exit == 0 and reference.suite_exit == 0
    assert reference.resolved
    assert pristine.files_added == () and reference.files_added == ()


def test_a_real_fix_resolves_and_nothing_is_reported_as_added_or_touched(
    seeds: dict[str, Seed], runner: Runner, tmp_path: Path
) -> None:
    seed = seeds["01"]
    pr = copy_seed_repo(seed.repo_dir, tmp_path / "pr")
    shutil.copyfile(seed.reference_dir / "pager.py", pr / "pager.py")
    res = check_resolution(seed, pr, tmp_path / "work", runner)
    assert res.resolved, res
    assert res.files_added == () and res.tests_touched == ()


def test_fixing_by_deleting_the_target_test_does_not_resolve(
    seeds: dict[str, Seed], runner: Runner, tmp_path: Path
) -> None:
    seed = seeds["01"]
    pr = copy_seed_repo(seed.repo_dir, tmp_path / "pr")
    (pr / "tests" / "test_pager.py").unlink()
    res = check_resolution(seed, pr, tmp_path / "work", runner)
    assert not res.resolved
    assert res.target_exit not in (0, None)  # the pristine test ran, against the unfixed code
    assert "tests/test_pager.py" in res.tests_touched


def test_a_test_edited_to_pass_does_not_resolve(
    seeds: dict[str, Seed], runner: Runner, tmp_path: Path
) -> None:
    seed = seeds["01"]
    pr = copy_seed_repo(seed.repo_dir, tmp_path / "pr")
    write(pr, "tests/test_pager.py", "def test_second_page():\n    assert True\n")
    write(pr, "pytest.ini", "[pytest]\n")
    res = check_resolution(seed, pr, tmp_path / "work", runner)
    assert not res.resolved and res.target_exit not in (0, None)
    assert set(res.tests_touched) >= {"tests/test_pager.py"}


def test_planted_pytest_files_are_ignored_so_the_unfixed_tree_still_fails(
    seeds: dict[str, Seed], runner: Runner, tmp_path: Path
) -> None:
    seed = seeds["01"]
    pr = copy_seed_repo(seed.repo_dir, tmp_path / "pr")
    for rel, text in PLANTED.items():
        write(pr, rel, text)
    # Control: run raw in the planted tree, the local pytest.py shadows pytest and "passes".
    raw = runner(seed, pr, [f"python -m pytest -q {seed.target_test}"])
    assert raw == [0], "the planted pytest.py should fool a plain run"
    res = check_resolution(seed, pr, tmp_path / "work", runner)
    assert not res.resolved
    assert res.target_exit not in (0, None)
    assert set(res.files_added) >= set(PLANTED)
    assert "conftest.py" in res.tests_touched  # protected-shaped: reported as touched too
    assert "pytest.py" not in res.tests_touched and "pytest.toml" not in res.tests_touched


def test_planted_files_do_not_stop_a_real_fix_from_resolving(
    seeds: dict[str, Seed], runner: Runner, tmp_path: Path
) -> None:
    seed = seeds["01"]
    pr = copy_seed_repo(seed.repo_dir, tmp_path / "pr")
    shutil.copyfile(seed.reference_dir / "pager.py", pr / "pager.py")
    for rel, text in PLANTED.items():
        write(pr, rel, text)
    res = check_resolution(seed, pr, tmp_path / "work", runner)
    assert res.resolved, res  # honest edits count, planted files are only reported
    assert set(res.files_added) >= set(PLANTED)
