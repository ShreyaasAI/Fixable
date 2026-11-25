"""M10a T7: wiring splits for the e2e launchers (m10a-contract §1.6, §1.7).

`worker.build_runner_deps` and `api.main.build_app_deps` assemble dependencies without opening
the database; `create_app_from_env` keeps its behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

import swf.worker
from swf.agent.model import PhaseModel
from swf.api.app import AppDeps
from swf.api.main import build_app_deps, create_app_from_env  # type: ignore[attr-defined]
from swf.egress import EgressConfig
from swf.github import GitHubClient
from swf.guard import Guardrails
from swf.images import RegistryClient
from swf.pr import GitHubPublisher, GitHubWorkspace
from swf.settings import Settings
from swf.worker import (  # type: ignore[attr-defined]
    LocalCopyWorkspace,
    RunnerDeps,
    WorkerConfig,
    build_runner_deps,
)


class FakeDb:
    """Any attribute access is a failure: wiring must not touch the database."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"db.{name} used while wiring")


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql://u:p@127.0.0.1:1/x",
        supabase_url="https://abc.supabase.co",
        token_encryption_key=Fernet.generate_key().decode(),
        github_api_url="https://ghe.example.test/api/v3",
    )


def make_config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(worker_id="w-test", workspaces_root=tmp_path / "ws")


def build(tmp_path: Path, **kw: Any) -> RunnerDeps:
    db: Any = FakeDb()
    return build_runner_deps(make_settings(), db, make_config(tmp_path), EgressConfig(), **kw)


# --- build_runner_deps -------------------------------------------------------------------------


def test_unset_sentinel_is_exported() -> None:
    assert swf.worker.UNSET is not None
    assert swf.worker.UNSET is swf.worker.UNSET


def test_defaults_without_local_source_use_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SWF_LOCAL_SOURCE", raising=False)
    config = make_config(tmp_path)
    db: Any = FakeDb()
    deps = build_runner_deps(make_settings(), db, config, EgressConfig())
    assert deps.db is db
    assert deps.config is config
    assert deps.model_factory == PhaseModel.from_env
    assert deps.guard_factory == Guardrails.from_env
    assert isinstance(deps.workspaces, GitHubWorkspace)
    assert isinstance(deps.publisher, GitHubPublisher)
    assert callable(deps.env_factory)


def test_local_source_selects_local_copy_and_no_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("SWF_LOCAL_SOURCE", str(src))
    deps = build(tmp_path)
    assert isinstance(deps.workspaces, LocalCopyWorkspace)
    assert deps.publisher is None
    assert deps.model_factory == PhaseModel.from_env
    assert deps.guard_factory == Guardrails.from_env


def test_overrides_are_used_as_passed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SWF_LOCAL_SOURCE", raising=False)

    def model_factory() -> Any:
        raise AssertionError("not called")

    def guard_factory() -> Guardrails:
        raise AssertionError("not called")

    workspaces = object()
    publisher = object()
    deps = build(
        tmp_path,
        model_factory=model_factory,
        workspaces=workspaces,
        publisher=publisher,
        guard_factory=guard_factory,
    )
    assert deps.model_factory is model_factory
    assert deps.guard_factory is guard_factory
    assert deps.workspaces is workspaces
    assert deps.publisher is publisher


def test_explicit_none_publisher_is_kept_even_without_local_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SWF_LOCAL_SOURCE", raising=False)
    deps = build(tmp_path, publisher=None)
    assert deps.publisher is None
    assert isinstance(deps.workspaces, GitHubWorkspace)


def test_workspaces_override_does_not_change_publisher_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SWF_LOCAL_SOURCE", str(tmp_path))
    workspaces = object()
    deps = build(tmp_path, workspaces=workspaces)
    assert deps.workspaces is workspaces
    assert deps.publisher is None


# --- build_app_deps ----------------------------------------------------------------------------


def test_build_app_deps_uses_injected_clients() -> None:
    github = GitHubClient(base_url="https://injected.example.test")
    registry = RegistryClient()
    db: Any = FakeDb()
    deps = build_app_deps(make_settings(), db, github=github, registry=registry)
    assert isinstance(deps, AppDeps)
    assert deps.db is db
    assert deps.github is github
    assert deps.preflight.github is github
    assert deps.preflight.registry is registry


def test_build_app_deps_defaults() -> None:
    db: Any = FakeDb()
    deps = build_app_deps(make_settings(), db)
    assert isinstance(deps.github, GitHubClient)
    assert str(deps.github._base) == "https://ghe.example.test/api/v3"  # noqa: SLF001
    assert deps.preflight.github is deps.github
    assert isinstance(deps.preflight.registry, RegistryClient)


# --- create_app_from_env -----------------------------------------------------------------------


def test_create_app_from_env_still_builds_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWF_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("SWF_SUPABASE_URL", "https://abc.supabase.co")
    monkeypatch.setenv("SWF_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = create_app_from_env()  # lifespan is not started: the database stays closed
    assert isinstance(app, FastAPI)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/preflight" in paths
    assert "/v1/admin/status" in paths
