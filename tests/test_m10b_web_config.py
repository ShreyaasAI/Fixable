"""M10b §0.8-0.10: Vercel config and the two new workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def load_yaml(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_vercel_config() -> None:
    cfg = json.loads((ROOT / "web" / "vercel.json").read_text(encoding="utf-8"))
    assert cfg["framework"] == "vite"
    assert cfg["buildCommand"] == "npm run build"
    assert cfg["outputDirectory"] == "dist"
    headers = {h["key"]: h["value"] for block in cfg["headers"] for h in block["headers"]}
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in headers["Permissions-Policy"]
    assert [b["source"] for b in cfg["headers"]] == ["/(.*)"]


def test_vercel_env_is_documented_without_the_dev_mode() -> None:
    example = (ROOT / "web" / ".env.example").read_text(encoding="utf-8")
    for name in ("VITE_SUPABASE_URL", "VITE_SUPABASE_ANON_KEY", "VITE_API_BASE"):
        assert name in example


def test_keepalive_workflow() -> None:
    wf = load_yaml(WORKFLOWS / "supabase-keepalive.yml")
    on = wf[True]  # PyYAML reads the `on` key as True
    assert "workflow_dispatch" in on
    assert on["schedule"][0]["cron"]
    steps = wf["jobs"]["ping"]["steps"]
    text = json.dumps(steps)
    assert "SUPABASE_URL" in text and "SUPABASE_ANON_KEY" in text
    assert "rest/v1/" in text
    assert wf["permissions"] == {"contents": "read"}


def test_e2e_workflow_runs_only_on_the_ci_repo() -> None:
    wf = load_yaml(WORKFLOWS / "m10-e2e.yml")
    job = wf["jobs"]["e2e"]
    assert job["if"] == "github.repository == 'ShreyaasAI/swf-ci'"
    on = wf[True]
    assert "workflow_dispatch" in on
    assert on["push"]["branches"] == ["feature/m10"]
    run = json.dumps(job["steps"])
    assert "-m e2e" in run
    assert "playwright install" in run
    assert wf["permissions"] == {"contents": "read"}
