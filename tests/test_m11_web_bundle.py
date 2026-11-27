"""m11 §4: the redesigned bundle. Three.js loads only with the sign-in hero; fonts are self-hosted.

Needs npm (marked `e2e`, like test_m10_web_build)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

WEB = Path(__file__).resolve().parent.parent / "web"
NPM = shutil.which("npm")
THREE_MARKER = "WebGLRenderer"  # a class name three.js keeps in its minified build


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if NPM is None:
        pytest.skip("npm is not installed")
    if not (WEB / "node_modules" / ".package-lock.json").is_file():
        subprocess.run([NPM, "ci", "--no-audit", "--no-fund"], cwd=WEB, check=True, timeout=900)
    out = tmp_path_factory.mktemp("m11") / "dist"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VITE_")}
    subprocess.run(
        [NPM, "exec", "--", "vite", "build", "--outDir", str(out), "--emptyOutDir"],
        cwd=WEB, check=True, env=env, timeout=900,
    )  # fmt: skip
    return out


def entry_chunk(dist: Path) -> Path:
    html = (dist / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<script type="module"[^>]*src="/?([^"]+)"', html)
    assert match, html
    return dist / match.group(1)


def test_three_is_not_in_the_entry_chunk(dist: Path) -> None:
    assert THREE_MARKER not in entry_chunk(dist).read_text(encoding="utf-8")


def test_three_ships_in_a_lazy_chunk(dist: Path) -> None:
    lazy = [p for p in dist.rglob("*.js") if p != entry_chunk(dist)]
    assert any(THREE_MARKER in p.read_text(encoding="utf-8") for p in lazy)


def test_fonts_are_bundled_not_fetched(dist: Path) -> None:
    text = "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in dist.rglob("*")
        if p.suffix in {".js", ".css", ".html"}
    )
    assert "fonts.googleapis" not in text and "fonts.gstatic" not in text
    families = {p.name.split("-")[0].lower() for p in dist.rglob("*.woff2")}
    # m11 A5: the display face is Sansita (a free Britannic Bold look-alike), not Fraunces
    assert {"sansita", "ibm", "jetbrains"} <= families, families
    assert "fraunces" not in families, families


def test_css_has_both_themes(dist: Path) -> None:
    css = "".join(p.read_text(encoding="utf-8") for p in dist.rglob("*.css"))
    assert 'data-theme="light"' in css or "data-theme=light" in css
