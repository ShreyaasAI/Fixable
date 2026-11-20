"""Import-order guard for mini-swe-agent (m4-contract §1; M2 open risk).

`minisweagent/__init__` reads `MSWEA_GLOBAL_CONFIG_DIR`, creates that directory and loads its
`.env` into `os.environ` **at import time**, and prints a banner unless `MSWEA_SILENT_STARTUP`
is set. A developer's `%LOCALAPPDATA%/mini-swe-agent/.env` (API keys, retry settings) must never
leak into the worker, so `isolate()` points the variable at an empty directory first.
`swf/__init__.py` calls it, so every `import swf.*` runs it before `minisweagent` is imported.

`MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1` turns off upstream's tenacity retries: `WebAgent` owns
the only model retry loop (m4-contract §0.6).

litellm (imported by mini-swe-agent's model and by `swf.agent`) runs `dotenv.load_dotenv()` at
import unless `LITELLM_MODE` is not "DEV" (`litellm/_process_defaults.py`). `find_dotenv` walks
up from litellm's own directory, so from a worktree it found the main checkout's `.env` (Portkey
and Supabase secrets) and loaded it into every worker and API process. `LITELLM_MODE=PRODUCTION`
switches that off; secrets come only from the real process environment.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ISOLATED_DIR_DEFAULT = Path(tempfile.gettempdir()) / "swf-mswea-empty"


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def isolate(config_dir: Path | None = None) -> Path:
    """Point mini-swe-agent at an empty config dir; idempotent; returns the directory."""
    raw = config_dir or os.environ.get("SWF_MSWEA_CONFIG_DIR") or ISOLATED_DIR_DEFAULT
    directory = Path(raw)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / ".env").exists():
        raise RuntimeError(
            f"{directory / '.env'} exists; the mini-swe-agent config dir must stay empty"
        )
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(directory)
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
    os.environ["LITELLM_MODE"] = "PRODUCTION"  # no import-time load_dotenv in litellm
    loaded = sys.modules.get("minisweagent")
    if loaded is not None:
        current = Path(str(getattr(loaded, "global_config_dir", "")))
        if not _same(current, directory):
            raise RuntimeError(
                f"minisweagent was imported before swf with config dir {current}; import swf first"
            )
    return directory
