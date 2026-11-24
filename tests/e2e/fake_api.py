"""E2E API launcher (m10a §4.2): the real app with in-process fake GitHub and registry.

Run by `LocalStack(api_argv=[sys.executable, <this file>])`, which appends `--host H --port P`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402
import uvicorn  # noqa: E402

from swf.api.main import build_app_deps, create_app_with_lifespan  # noqa: E402
from swf.db import Database  # noqa: E402
from swf.github import GitHubClient  # noqa: E402
from swf.images import RegistryClient  # noqa: E402
from swf.settings import Settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    db = Database(settings.database_url)
    deps = build_app_deps(
        settings,
        db,
        github=GitHubClient(transport=fakes.make_github().transport()),
        registry=RegistryClient(transport=fakes.make_registry().transport()),
    )
    uvicorn.run(create_app_with_lifespan(deps, db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
