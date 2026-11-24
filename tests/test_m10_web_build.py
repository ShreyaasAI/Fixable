"""m10a §0 decision 14, §4.1: `web/` typechecks and builds; only a dev build has the dev sign-in.

Needs npm (marked `e2e`, so CI skips it until M10b)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

WEB = Path(__file__).resolve().parent.parent / "web"
MARKER = "swf-dev-signin"
NPM = shutil.which("npm")


def npm(*args: str, env: dict[str, str] | None = None) -> None:
    assert NPM is not None
    subprocess.run([NPM, *args], cwd=WEB, check=True, env=env, timeout=600)


def marker_in(dist: Path) -> bool:
    return any(MARKER in p.read_text(encoding="utf-8", errors="ignore") for p in dist.rglob("*.js"))


@pytest.fixture(scope="module")
def installed() -> None:
    if NPM is None:
        pytest.skip("npm is not installed")
    # A running Vite (the e2e stack) locks esbuild.exe on Windows, so `npm ci` (which deletes
    # node_modules) cannot run then; install only when the lockfile's packages are missing.
    if not (WEB / "node_modules" / ".package-lock.json").is_file():
        npm("ci", "--no-audit", "--no-fund")


def test_typecheck_passes(installed: None) -> None:
    npm("run", "typecheck")


def test_production_build_has_no_dev_sign_in(installed: None, tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VITE_")}
    out = tmp_path / "prod"
    npm("exec", "--", "vite", "build", "--outDir", str(out), "--emptyOutDir", env=env)
    assert (out / "index.html").is_file()
    assert not marker_in(out)


def test_dev_build_has_the_dev_sign_in(installed: None, tmp_path: Path) -> None:
    env = {**os.environ, "VITE_AUTH_MODE": "dev"}
    out = tmp_path / "dev"
    npm("exec", "--", "vite", "build", "--outDir", str(out), "--emptyOutDir", env=env)
    assert marker_in(out)
