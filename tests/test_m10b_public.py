"""M10b §0.2, §0.7: the worker's public-mode gate and `swf.deploycheck`."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from swf import deploycheck
from swf.settings import SettingsError
from swf.worker import public_startup_check

SLUG = "pc-swf-m7-d1cf5c"
RUNSC = frozenset({"runc", "runsc"})
RUNC = frozenset({"runc"})

PUBLIC = {
    "SWF_PUBLIC": "1",
    "SWF_CORS_ORIGINS": "https://swf-web.vercel.app",
    "SWF_SANDBOX_RUNTIME": "runsc",
    "SWF_PORTKEY_CONFIG": SLUG,
}


# --- worker gate ------------------------------------------------------------------------------


def test_local_mode_has_no_gate() -> None:
    public_startup_check({"SWF_PORTKEY_CONFIG": "off"}, runtimes=RUNC)


def test_public_mode_with_gvisor_and_guardrails_passes() -> None:
    public_startup_check(PUBLIC, runtimes=RUNSC)


@pytest.mark.parametrize(
    ("change", "runtimes", "match"),
    [
        ({"SWF_SANDBOX_RUNTIME": ""}, RUNSC, "runsc"),
        ({"SWF_SANDBOX_RUNTIME": "runc"}, RUNSC, "runsc"),
        ({}, RUNC, "runsc"),  # configured, but Docker does not list it
        ({"SWF_PORTKEY_CONFIG": "off"}, RUNSC, "SWF_PORTKEY_CONFIG"),
        ({"SWF_CORS_ORIGINS": ""}, RUNSC, "SWF_CORS_ORIGINS"),
    ],
)
def test_public_mode_refuses_to_start_without_its_protections(
    change: dict[str, str], runtimes: frozenset[str], match: str
) -> None:
    with pytest.raises(SettingsError, match=match):
        public_startup_check({**PUBLIC, **change}, runtimes=runtimes)


# --- deploycheck ------------------------------------------------------------------------------


def ready_env() -> dict[str, str]:
    return {
        **PUBLIC,
        "SWF_DATABASE_URL": "postgresql://u:p@db.example:5432/postgres",
        "SWF_SUPABASE_URL": "https://ref.supabase.co",
        "SWF_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PORTKEY_API_KEY": "pk-test",
        "SWF_SANDBOX_USER": "1000:1000",
    }


def test_a_ready_deployment_passes_every_check() -> None:
    results = deploycheck.checks(ready_env(), docker_runtimes=RUNSC)
    assert [c.name for c in results] == [
        "env",
        "token key",
        "portkey guardrails",
        "public mode",
        "cors origins",
        "gvisor",
        "sandbox user",
    ]
    assert all(c.ok for c in results), [c for c in results if not c.ok]


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        ({"SWF_DATABASE_URL": ""}, "env"),
        ({"PORTKEY_API_KEY": ""}, "env"),
        ({"SWF_TOKEN_ENCRYPTION_KEY": "not-a-key"}, "token key"),
        ({"SWF_PORTKEY_CONFIG": "off"}, "portkey guardrails"),
        ({"SWF_PUBLIC": ""}, "public mode"),
        ({"SWF_CORS_ORIGINS": "http://swf.example"}, "cors origins"),
        ({"SWF_SANDBOX_RUNTIME": "runc"}, "gvisor"),
        ({"SWF_SANDBOX_USER": "0:0"}, "sandbox user"),
    ],
)
def test_each_check_fails_on_its_own(change: dict[str, str], failing: str) -> None:
    results = {
        c.name: c for c in deploycheck.checks({**ready_env(), **change}, docker_runtimes=RUNSC)
    }
    assert results[failing].ok is False
    assert results[failing].fix  # says what to do
    assert [n for n, c in results.items() if not c.ok] == [failing]


def test_gvisor_fails_when_docker_lacks_runsc() -> None:
    results = {c.name: c for c in deploycheck.checks(ready_env(), docker_runtimes=RUNC)}
    assert results["gvisor"].ok is False


def test_report_never_prints_a_value(capsys: pytest.CaptureFixture[str]) -> None:
    env = ready_env()
    code = deploycheck.report(deploycheck.checks(env, docker_runtimes=RUNC))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL gvisor" in out
    assert "PASS env" in out
    for secret in (
        env["SWF_TOKEN_ENCRYPTION_KEY"],
        env["PORTKEY_API_KEY"],
        env["SWF_DATABASE_URL"],
    ):
        assert secret not in out


def test_report_exit_code_is_zero_when_all_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert deploycheck.report(deploycheck.checks(ready_env(), docker_runtimes=RUNSC)) == 0


def test_a_failing_cors_check_does_not_print_the_value(capsys: pytest.CaptureFixture[str]) -> None:
    bad = "http://not-local-origin.example"
    env = {**ready_env(), "SWF_CORS_ORIGINS": bad}
    deploycheck.report(deploycheck.checks(env, docker_runtimes=RUNSC))
    out = capsys.readouterr().out
    assert "FAIL cors origins" in out
    assert bad not in out


def test_main_runs_the_public_gate_before_any_docker_or_database_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import swf.sandbox
    import swf.worker

    calls: list[str] = []

    class NoDatabase:
        def __init__(self, *_a: object, **_k: object) -> None:
            calls.append("Database()")
            raise AssertionError("Database must not be built before the public gate")

    def no(name: str) -> object:
        def fn(*_a: object, **_k: object) -> object:
            calls.append(name)
            raise AssertionError(f"{name} must not run before the public gate")

        return fn

    for k, v in PUBLIC.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(swf.sandbox, "docker_runtimes", lambda **_k: RUNC)
    monkeypatch.setattr(swf.worker, "Database", NoDatabase)
    monkeypatch.setattr(swf.sandbox, "check_runtime", no("check_runtime"))
    monkeypatch.setattr(swf.sandbox, "cleanup_orphans", no("cleanup_orphans"))
    with pytest.raises(SettingsError, match="runsc"):
        swf.worker.main()
    assert calls == []
