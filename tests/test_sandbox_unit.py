"""Sandbox run arguments, spec validation and the Squid config (contract §4, §5). No Docker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from minisweagent.environments.docker import DockerEnvironment
from pydantic import ValidationError

from swf.egress import EgressConfig, render_squid_conf
from swf.sandbox import (
    LABEL_MANAGED,
    LABEL_TASK,
    SandboxEnv,
    SandboxSpec,
    sandbox_run_args,
)

PROXY = "http://172.30.0.2:3128"


def spec_for(ws: Path, **overrides: Any) -> SandboxSpec:
    fields: dict[str, Any] = {
        "task_id": "task-1",
        "image": "python:3.12-slim",
        "workspace": ws,
        "proxy_url": PROXY,
        "network": "swf-sandbox",
    }
    return SandboxSpec(**{**fields, **overrides})


def has_pair(args: list[str], flag: str, value: str) -> bool:
    return any(a == flag and b == value for a, b in zip(args, args[1:], strict=False))


def env_items(args: list[str]) -> list[str]:
    return [b for a, b in zip(args, args[1:], strict=False) if a == "-e"]


# --- run args -------------------------------------------------------------------------------


def test_label_constants() -> None:
    assert LABEL_MANAGED == "swf.managed"
    assert LABEL_TASK == "swf.task"


def test_run_args_have_every_hardening_flag(tmp_path: Path) -> None:
    args = sandbox_run_args(spec_for(tmp_path, memory_mb=1024, cpus=0.5, pids_limit=100))
    assert "--rm" in args
    assert "--init" in args
    assert has_pair(args, "--label", "swf.managed=1")
    assert has_pair(args, "--label", "swf.task=task-1")
    assert has_pair(args, "--network", "swf-sandbox")
    assert has_pair(args, "--dns", "127.0.0.1")
    assert has_pair(args, "--memory", "1024m")
    assert has_pair(args, "--memory-swap", "1024m")
    assert has_pair(args, "--cpus", "0.5")
    assert has_pair(args, "--pids-limit", "100")
    assert has_pair(args, "--cap-drop", "ALL")
    assert has_pair(args, "--security-opt", "no-new-privileges")
    assert has_pair(args, "--user", "1000:1000")


def test_run_args_defaults(tmp_path: Path) -> None:
    args = sandbox_run_args(spec_for(tmp_path))
    assert has_pair(args, "--memory", "4096m")
    assert has_pair(args, "--memory-swap", "4096m")
    assert has_pair(args, "--pids-limit", "512")
    (cpus,) = [b for a, b in zip(args, args[1:], strict=False) if a == "--cpus"]
    assert float(cpus) == 1.0


def test_run_args_mount_only_the_workspace_at_repo(tmp_path: Path) -> None:
    args = sandbox_run_args(spec_for(tmp_path))
    mounts = [b for a, b in zip(args, args[1:], strict=False) if a == "--mount"]
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount.startswith("type=bind,source=")
    assert mount.endswith(",target=/repo")
    source = mount.removeprefix("type=bind,source=").removesuffix(",target=/repo")
    assert source in {str(tmp_path), tmp_path.as_posix()}


def test_run_args_environment(tmp_path: Path) -> None:
    items = env_items(sandbox_run_args(spec_for(tmp_path)))
    for expected in [
        "HOME=/tmp/home",
        "PYTHONDONTWRITEBYTECODE=1",
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "PIP_NO_INPUT=1",
        f"HTTP_PROXY={PROXY}",
        f"HTTPS_PROXY={PROXY}",
        f"http_proxy={PROXY}",
        f"https_proxy={PROXY}",
        "NO_PROXY=",
        "no_proxy=",
    ]:
        assert expected in items
    # Nothing else is injected: in particular no host or secret variable.
    assert len(items) == 10
    assert not any("TOKEN" in i or "KEY" in i or "SECRET" in i for i in items)


def test_runtime_flag_only_when_set(tmp_path: Path) -> None:
    assert "--runtime" not in sandbox_run_args(spec_for(tmp_path))
    args = sandbox_run_args(spec_for(tmp_path, runtime="runsc"))
    assert has_pair(args, "--runtime", "runsc")


def test_run_args_never_contain_dangerous_flags(tmp_path: Path) -> None:
    for spec in (spec_for(tmp_path), spec_for(tmp_path, runtime="runsc")):
        args = sandbox_run_args(spec)
        for forbidden in (
            "--privileged",
            "-v",
            "--volume",
            "--cap-add",
            "-p",
            "--publish",
            "-P",
            "--pid",
            "--ipc",
            "--device",
        ):
            assert forbidden not in args, forbidden
        assert not any("docker.sock" in a for a in args)
        assert not has_pair(args, "--network", "host")
        assert not any(a.startswith("--network=") for a in args)
        assert not any(a.startswith("--privileged") for a in args)


def test_custom_network_and_user(tmp_path: Path) -> None:
    args = sandbox_run_args(spec_for(tmp_path, network="swf-test-sandbox", user="2000:2000"))
    assert has_pair(args, "--network", "swf-test-sandbox")
    assert has_pair(args, "--user", "2000:2000")


def test_run_args_are_all_strings(tmp_path: Path) -> None:
    assert all(isinstance(a, str) for a in sandbox_run_args(spec_for(tmp_path)))


# --- SandboxEnv wiring (container start stubbed out) ----------------------------------------


@pytest.fixture
def no_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerEnvironment, "_start_container", lambda self: None)


def test_env_passes_the_hardened_configuration_to_upstream(
    tmp_path: Path, no_container: None
) -> None:
    spec = spec_for(tmp_path, command_timeout=77, container_timeout="30m")
    env = SandboxEnv(spec)
    assert env.spec is spec
    assert env.config.image == spec.image
    assert env.config.cwd == "/repo"
    assert env.config.run_args == sandbox_run_args(spec)
    assert env.config.env == {}
    assert env.config.forward_env == []  # no host variable is forwarded
    assert env.config.timeout == 77
    assert env.config.container_timeout == "30m"


def test_env_uses_a_non_login_shell(tmp_path: Path, no_container: None) -> None:
    # `bash -lc` re-runs /etc/profile, which resets PATH and breaks venv/Go/Rust images.
    env = SandboxEnv(spec_for(tmp_path))
    assert env.config.interpreter == ["bash", "-c"]


def test_env_cleanup_without_a_container_is_a_no_op(tmp_path: Path, no_container: None) -> None:
    env = SandboxEnv(spec_for(tmp_path))
    env.container_id = None
    env.cleanup()
    env.cleanup()
    assert env.container_id is None


def test_env_cleanup_removes_the_container_synchronously(
    tmp_path: Path, no_container: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    env = SandboxEnv(spec_for(tmp_path))
    env.container_id = "abc123"
    monkeypatch.setattr(subprocess, "run", fake_run)
    env.cleanup()
    assert calls == [["docker", "rm", "-f", "abc123"]]
    assert env.container_id is None
    env.cleanup()
    assert len(calls) == 1  # idempotent


def test_env_cleanup_ignores_docker_errors(
    tmp_path: Path, no_container: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 60)

    env = SandboxEnv(spec_for(tmp_path))
    env.container_id = "abc123"
    monkeypatch.setattr(subprocess, "run", boom)
    env.cleanup()
    assert env.container_id is None


# --- spec validation ------------------------------------------------------------------------


def test_spec_defaults(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    assert spec.memory_mb == 4096
    assert spec.cpus == 1.0
    assert spec.pids_limit == 512
    assert spec.user == "1000:1000"
    assert spec.runtime is None
    assert spec.command_timeout == 300
    assert spec.container_timeout == "2h"


@pytest.mark.parametrize("task_id", ["a", "A_b.c-9", "x" * 64])
def test_spec_accepts_valid_task_ids(tmp_path: Path, task_id: str) -> None:
    assert spec_for(tmp_path, task_id=task_id).task_id == task_id


@pytest.mark.parametrize(
    "task_id", ["", "a b", "a/b", "../x", "x" * 65, "a;rm", "a\nb", "$(id)", "tâche"]
)
def test_spec_rejects_bad_task_ids(tmp_path: Path, task_id: str) -> None:
    with pytest.raises(ValidationError):
        spec_for(tmp_path, task_id=task_id)


def test_spec_rejects_relative_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        spec_for(tmp_path, workspace=Path("ws"))


def test_spec_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        spec_for(tmp_path, workspace=tmp_path / "does-not-exist")


def test_spec_rejects_file_as_workspace(tmp_path: Path) -> None:
    file = tmp_path / "f.txt"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError):
        spec_for(tmp_path, workspace=file)


def test_spec_forbids_extra_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        spec_for(tmp_path, privileged=True)


def test_spec_requires_network(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SandboxSpec(  # type: ignore[call-arg]
            task_id="t", image="python:3.12-slim", workspace=tmp_path, proxy_url=PROXY
        )


@pytest.mark.parametrize("network", ["", "a b", "-x", "a;b", "a\nb"])
def test_spec_rejects_odd_network_names(tmp_path: Path, network: str) -> None:
    with pytest.raises(ValidationError):
        spec_for(tmp_path, network=network)


# Defence in depth at execution time (the image comes from the user's repo).


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12-slim",
        "docker.io/library/python:3.12",
        "ghcr.io/org/img:1",
        "quay.io/org/img:1",
        "public.ecr.aws/x/y:1",
        "mcr.microsoft.com/dotnet/sdk:8.0",
        "registry.gitlab.com/g/p:1",
    ],
)
def test_spec_accepts_allow_listed_images(tmp_path: Path, image: str) -> None:
    assert spec_for(tmp_path, image=image).image == image


@pytest.mark.parametrize(
    "image",
    [
        "evil.example/x:1",
        "localhost:5000/x:1",
        "169.254.169.254/x:1",
        "10.0.0.5/a/b:c",
        "Python:3",
        "python:",
        "http://ghcr.io/x:1",
        "--privileged",
        "python:3 --privileged",
        "a b",
    ],
)
def test_spec_rejects_disallowed_or_invalid_images(tmp_path: Path, image: str) -> None:
    with pytest.raises(ValidationError):
        spec_for(tmp_path, image=image)


def test_spec_memory_and_cpu_are_capped_at_the_task_limit(tmp_path: Path) -> None:
    assert spec_for(tmp_path, memory_mb=4096, cpus=1.0).memory_mb == 4096
    with pytest.raises(ValidationError):
        spec_for(tmp_path, memory_mb=4097)
    with pytest.raises(ValidationError):
        spec_for(tmp_path, cpus=1.01)
    with pytest.raises(ValidationError):
        spec_for(tmp_path, cpus=2)


def test_spec_rejects_a_comma_in_the_workspace_path(tmp_path: Path) -> None:
    bad = tmp_path / "a,b"
    bad.mkdir()
    with pytest.raises(ValidationError):
        spec_for(tmp_path, workspace=bad)


@pytest.mark.skipif(sys.platform == "win32", reason="a double quote cannot be in a Windows path")
def test_spec_rejects_a_quote_in_the_workspace_path(tmp_path: Path) -> None:
    bad = tmp_path / 'a"b'
    bad.mkdir()
    with pytest.raises(ValidationError):
        spec_for(tmp_path, workspace=bad)


def test_spec_requires_proxy_url(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SandboxSpec(  # type: ignore[call-arg]
            task_id="t", image="python:3.12-slim", workspace=tmp_path, network="n"
        )


@pytest.mark.parametrize("field", ["memory_mb", "cpus", "pids_limit"])
@pytest.mark.parametrize("value", [0, -1])
def test_spec_rejects_non_positive_limits(tmp_path: Path, field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        spec_for(tmp_path, **{field: value})


def test_spec_is_frozen(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    with pytest.raises(ValidationError):
        spec.memory_mb = 1  # type: ignore[misc]


# --- squid.conf -----------------------------------------------------------------------------


def conf_lines(cfg: EgressConfig) -> list[str]:
    return [line.strip() for line in render_squid_conf(cfg).splitlines() if line.strip()]


def test_squid_conf_defaults() -> None:
    lines = conf_lines(EgressConfig())
    for expected in [
        "http_port 3128",
        "acl allowed_domains dstdomain -n pypi.org files.pythonhosted.org",
        "acl SSL_ports port 443",
        "acl CONNECT method CONNECT",
        "http_access deny !CONNECT",
        "http_access deny CONNECT !SSL_ports",
        "http_access allow CONNECT allowed_domains",
        "http_access deny all",
        "cache deny all",
        "via off",
        "forwarded_for delete",
        "httpd_suppress_version_string on",
        "shutdown_lifetime 1 seconds",
    ]:
        assert expected in lines, expected


def test_squid_conf_access_rules_are_ordered_and_closed() -> None:
    lines = conf_lines(EgressConfig())
    order = [
        "http_access deny !CONNECT",
        "http_access deny CONNECT !SSL_ports",
        "http_access allow CONNECT allowed_domains",
        "http_access deny all",
    ]
    access = [line for line in lines if line.startswith("http_access")]
    assert access == order  # nothing else, in particular no `allow all` / `allow localhost`
    assert not any("allow all" in line or "allow localhost" in line for line in lines)


def test_squid_conf_custom_port_and_domains() -> None:
    cfg = EgressConfig(port=3129, allow_domains=("pypi.org", ".pythonhosted.org", "a-b.example.io"))
    lines = conf_lines(cfg)
    assert "http_port 3129" in lines
    assert "acl allowed_domains dstdomain -n pypi.org .pythonhosted.org a-b.example.io" in lines


def test_egress_config_defaults() -> None:
    cfg = EgressConfig()
    assert cfg.network_prefix == "swf-task-"
    assert cfg.proxy_name == "swf-egress-proxy"
    # pinned by digest (A6)
    assert cfg.image.startswith("ubuntu/squid:6.6-24.04_edge@sha256:")
    assert len(cfg.image.rsplit(":", 1)[1]) == 64
    assert cfg.port == 3128
    assert cfg.allow_domains == ("pypi.org", "files.pythonhosted.org")


BAD_DOMAINS = [
    "",
    " ",
    "a b",
    "evil.com ",
    " evil.com",
    "evil.com\n",
    "evil.com\nhttp_access allow all",
    "evil.com\r\nhttp_access allow all",
    "evil.com#",
    "evil.com;",
    "localhost",
    "1.2.3.4",
    "*.evil.com",
    "EVIL.com",
    "-evil.com",
    "evil-.com",
    "evil..com",
    "evil.c",
    "http://evil.com",
    "evil.com/path",
    "evil.com:443",
]


@pytest.mark.parametrize("domain", BAD_DOMAINS)
def test_egress_config_rejects_invalid_domains(domain: str) -> None:
    with pytest.raises(ValidationError):
        EgressConfig(allow_domains=("pypi.org", domain))


def test_egress_config_has_no_shared_network_any_more() -> None:
    # Each task gets its own network (`network_prefix + task_id`); a shared one is gone.
    with pytest.raises(ValidationError):
        EgressConfig(network="swf-sandbox")  # type: ignore[call-arg]


def test_render_revalidates_domains_even_if_validation_was_bypassed() -> None:
    """Defence in depth: a config built without validation must not inject Squid directives."""
    from swf.egress import EgressConfig, render_squid_conf

    forged = EgressConfig.model_construct(
        allow_domains=("pypi.org\nhttp_access allow all",), port=3128
    )
    with pytest.raises(ValueError):
        render_squid_conf(forged)
