"""LocalStack process argv construction (m10a T8): pure, starts nothing."""

from __future__ import annotations

import sys

from swf_eval.localstack import api_command, worker_command


def test_default_api_command_is_uvicorn_factory() -> None:
    argv = api_command(None, 8123)
    assert argv[:3] == [sys.executable, "-m", "uvicorn"]
    assert "swf.api.main:create_app_from_env" in argv
    assert argv[-4:] == ["--host", "127.0.0.1", "--port", "8123"]


def test_custom_api_command_gets_host_and_port_appended() -> None:
    argv = api_command([sys.executable, "launcher.py"], 9001)
    assert argv == [sys.executable, "launcher.py", "--host", "127.0.0.1", "--port", "9001"]


def test_worker_command_default_and_custom() -> None:
    assert worker_command(None) == [sys.executable, "-m", "swf.worker"]
    assert worker_command([sys.executable, "w.py"]) == [sys.executable, "w.py"]
