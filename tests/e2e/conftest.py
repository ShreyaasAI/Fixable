"""E2E fixtures (m10a §4.2): `LocalStack` (Postgres, real API, real worker, with the two fake
launchers) plus `npm run dev` for `web/`, and Playwright Chromium.

The whole package is skipped, with a reason, when Docker, npm or Chromium is missing.
Debug a test with `PWDEBUG=1` (headed, inspector) or `SWF_E2E_HEADED=1` (headed).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from playwright.sync_api import Browser, Page, Playwright, expect, sync_playwright

from e2e import fakes
from swf_eval.localstack import EvalUser, LocalStack

HERE = Path(__file__).resolve().parent
WEB = HERE.parent.parent / "web"

SHORT = 15_000  # ms: a UI reaction
DONE = 180_000  # ms: a whole task, sandbox start included

ADMIN = "e2e-admin"
FIX_USER = "e2e-user"  # owns the shared finished task (`finished_task`)
POOL = tuple(f"e2e-u{i:02d}" for i in range(1, 17))  # one fresh user per test
_PLATFORM = (
    "PATHEXT", "COMSPEC", "windir", "SystemDrive", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE",
)  # fmt: skip

# Quotas and rate limits are for abuse control, not for what the UI tests exercise.
KNOBS = {
    "SWF_PORTKEY_CONFIG": "off",
    "SWF_QUOTA_TASKS_PER_DAY": "1000",
    "SWF_QUOTA_COST_PER_DAY_USD": "1000",
    "SWF_QUOTA_COST_PER_TASK_USD": "100",
    "SWF_QUOTA_CONCURRENT_TASKS": "5",
    "SWF_RATE_API_PER_MINUTE": "6000",
    "SWF_RATE_API_BURST": "1000",
    "SWF_RATE_CREATE_PER_MINUTE": "600",
    "SWF_RATE_CREATE_BURST": "100",
}


def _skip_reason() -> str | None:
    if shutil.which("docker") is None:
        return "e2e needs Docker: the docker CLI was not found"
    try:
        run = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"e2e needs Docker: docker info failed: {exc}"
    if run.returncode != 0:
        return f"e2e needs Docker: docker info failed: {run.stderr.strip()[:200]}"
    if shutil.which("npm") is None:
        return "e2e needs npm (Node) to serve web/ with Vite; npm was not found"
    if not (WEB / "node_modules").is_dir():
        return "e2e needs `npm install` in web/ (web/node_modules is missing)"
    try:
        with sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():
                return "e2e needs Chromium: run `uv run playwright install chromium`"
    except Exception as exc:  # noqa: BLE001 - any Playwright failure means "cannot run"
        return f"e2e needs Playwright Chromium: {exc}"
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    mine = [i for i in items if HERE in Path(str(i.fspath)).parents]
    if not mine:
        return
    reason = _skip_reason()
    if reason is not None:
        marker = pytest.mark.skip(reason=reason)
        for item in mine:
            item.add_marker(marker)


# --- processes -----------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def _vite_env(api_url: str) -> dict[str, str]:
    keep = (
        "PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME",
        *_PLATFORM,
    )  # fmt: skip
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"VITE_AUTH_MODE": "dev", "SWF_API_PROXY": api_url, "NO_COLOR": "1"})
    return env


@dataclass
class Stack:
    """What the tests need from the running stack."""

    local: LocalStack
    web_url: str
    run_dir: Path
    _free: list[str] = field(default_factory=lambda: list(POOL))

    @property
    def api_url(self) -> str:
        return self.local.api_url

    @property
    def db_url(self) -> str:
        return self.local.db_url

    @property
    def workspaces(self) -> Path:
        return self.run_dir / "workspaces"

    def user(self, name: str) -> EvalUser:
        return self.local.users[name]

    def take_user(self) -> EvalUser:
        return self.user(self._free.pop(0))

    def api(self, user: EvalUser, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.request(
            method,
            f"{self.api_url}{path}",
            headers={"Authorization": f"Bearer {user.jwt}"},
            timeout=30,
            **kwargs,
        )

    def sql(self, query: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            cur = conn.execute(query, params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []


@pytest.fixture(scope="session")
def e2e_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    run_dir = tmp_path_factory.mktemp("e2e")
    web_port = _free_port()
    local = LocalStack(
        run_dir,
        github_id=fakes.GITHUB_ID,
        github_login=fakes.GITHUB_LOGIN,
        knobs=KNOBS,
        user_names=(FIX_USER, ADMIN, *POOL),
        api_argv=[sys.executable, str(HERE / "fake_api.py")],
        worker_argv=[sys.executable, str(HERE / "fake_worker.py")],
    )
    vite: subprocess.Popen[bytes] | None = None
    vite_log = None
    try:
        with local:
            stack = Stack(local, f"http://127.0.0.1:{web_port}", run_dir)
            # the profile row exists only after the first API call, then make the admin
            admin = stack.user(ADMIN)
            assert stack.api(admin, "GET", "/v1/me").status_code == 200
            stack.sql(
                "update public.profiles set is_admin = true where user_id = %s", [admin.user_id]
            )

            npm = shutil.which("npm")
            assert npm is not None
            (run_dir / "logs").mkdir(exist_ok=True)
            vite_log = open(run_dir / "logs" / "vite.log", "ab")  # noqa: SIM115
            vite = subprocess.Popen(
                [
                    npm,
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--strictPort",
                ],  # fmt: skip
                cwd=WEB,
                env=_vite_env(local.api_url),
                stdout=vite_log,
                stderr=subprocess.STDOUT,
                start_new_session=sys.platform != "win32",
            )
            _wait_vite(stack.web_url, vite)
            yield stack
    finally:
        if vite is not None:
            _stop_tree(vite)
        if vite_log is not None:
            vite_log.close()


def _wait_vite(url: str, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 90
    while True:
        try:
            # the page is up and the proxy reaches the API: no token -> 401, never a 404/502
            if (
                httpx.get(url, timeout=2).status_code == 200
                and httpx.get(f"{url}/v1/me", timeout=5).status_code == 401
            ):
                return
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("Vite did not start or does not proxy /v1 (see logs/vite.log)")
        time.sleep(0.5)


# --- browser -------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _playwright() -> Iterator[Playwright]:
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def _browser(_playwright: Playwright) -> Iterator[Browser]:
    headed = bool(os.environ.get("PWDEBUG") or os.environ.get("SWF_E2E_HEADED"))
    browser = _playwright.chromium.launch(headless=not headed)
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def _warm_vite(_browser: Browser, e2e_stack: Stack) -> None:
    """Load the app once: Vite bundles its dependencies on the first page load (slow, and it may
    reload the page), which must not count against the first test's assertion timeouts."""
    context = _browser.new_context(base_url=e2e_stack.web_url)
    try:
        page = context.new_page()
        page.goto("/")
        expect(page.get_by_test_id("dev-token")).to_be_visible(timeout=120_000)
    finally:
        context.close()


@pytest.fixture
def new_page(_browser: Browser, e2e_stack: Stack, _warm_vite: None) -> Iterator[Callable[[], Page]]:
    """Open further pages (own context, own storage), e.g. a second signed-in user."""
    contexts = []

    def make() -> Page:
        context = _browser.new_context(
            base_url=e2e_stack.web_url, viewport={"width": 1280, "height": 900}
        )
        context.set_default_timeout(SHORT)
        contexts.append(context)
        return context.new_page()

    yield make
    for context in contexts:
        context.close()


@pytest.fixture
def page(new_page: Callable[[], Page]) -> Page:
    return new_page()


@pytest.fixture
def user(e2e_stack: Stack) -> EvalUser:
    """A user no other test has used (so lists and quotas start empty)."""
    return e2e_stack.take_user()


# --- UI helpers ----------------------------------------------------------------------------

ERRORS = '[data-testid="error-summary"], [data-testid="app-error"], [data-testid="page-error"]'
TERMINAL = ("completed", "rejected", "failed", "interrupted", "cancelled")


def sign_in(page: Page, user: EvalUser) -> None:
    """Dev sign-in: paste the user's JWT and the (fake) GitHub provider token."""
    page.goto("/#/signin")
    page.get_by_test_id("dev-token").fill(user.jwt)
    page.get_by_test_id("dev-provider-token").fill(fakes.PROVIDER_TOKEN)
    page.get_by_test_id("dev-signin").click()
    expect(page.get_by_test_id("user-login")).to_be_visible(timeout=SHORT)


def choose_repo(page: Page, name: str) -> None:
    """m11 A1: the repo picker is a combobox; open it and click the option by its role."""
    trigger = page.get_by_test_id("repo-select")
    trigger.click()
    option = page.get_by_role("option", name=name, exact=True)
    expect(option).to_be_enabled(timeout=SHORT)
    option.click()
    expect(trigger).to_contain_text(name)


def fill_new_task(page: Page, description: str, *, repo: str = fakes.REPO,
                  failing_test: str | None = None, branch: str | None = None) -> None:  # fmt: skip
    page.get_by_test_id("nav-new").click()
    choose_repo(page, repo)
    if branch is not None:
        page.get_by_test_id("branch-input").fill(branch)
    page.get_by_test_id("description-input").fill(description)
    if failing_test:
        page.get_by_test_id("failing-test-input").fill(failing_test)


def check_preflight(page: Page) -> None:
    page.get_by_test_id("preflight-button").click()
    result = page.get_by_test_id("preflight-result")
    expect(result).to_be_visible(timeout=SHORT)
    expect(result).to_have_attribute("data-decision", re.compile("admit|queue"))


def create_task_via_ui(page: Page, tag: str, *, repo: str = fakes.REPO) -> str:
    """New task -> check -> start; returns the task id from the `#/tasks/<id>` route."""
    description = f"{tag} Page 2 of `paginate` repeats the last item of page 1."
    fill_new_task(
        page, description, repo=repo, failing_test="tests/test_pager.py::test_second_page"
    )
    check_preflight(page)
    start = page.get_by_test_id("start-button")
    expect(start).to_be_enabled(timeout=SHORT)
    start.click()
    page.wait_for_url("**/#/tasks/*", timeout=SHORT)
    return page.url.rsplit("/", 1)[-1]


def task_status(page: Page) -> str:
    return page.get_by_test_id("task-status").get_attribute("data-status") or ""


def wait_status(page: Page, status: str, *, timeout: float = DONE) -> None:
    expect(page.get_by_test_id("task-status")).to_have_attribute(
        "data-status", status, timeout=timeout
    )


def confirm_dialog(page: Page, *, yes: bool = True) -> None:
    dialog = page.get_by_test_id("confirm-dialog")
    expect(dialog).to_be_visible(timeout=SHORT)
    page.get_by_test_id("confirm-yes" if yes else "confirm-no").click()
    expect(dialog).to_be_hidden(timeout=SHORT)


def drive_to_terminal(page: Page, *, timeout: float = DONE) -> str:
    """Approve every approval card that shows up until the task is terminal; returns the status."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        status = task_status(page)
        if status in TERMINAL:
            return status
        card = page.get_by_test_id("approval-card")
        if card.count() and card.first.is_visible():
            approve = page.get_by_test_id("approve-button").first
            if approve.is_enabled():
                approve.click()
        page.wait_for_timeout(500)
    raise AssertionError(
        f"task not terminal after {timeout / 1000:.0f} s (status {task_status(page)!r})"
    )


def seqs(page: Page) -> list[int]:
    return [
        int(v)
        for v in page.get_by_test_id("event-row").evaluate_all(
            "els => els.map(e => e.getAttribute('data-seq'))"
        )
    ]


# --- backend helpers (setup that is not the point of a test) --------------------------------


def link_github(stack: Stack, user: EvalUser) -> None:
    resp = stack.api(
        user, "POST", "/v1/me/github-token", json={"provider_token": fakes.PROVIDER_TOKEN}
    )
    assert resp.status_code == 204, resp.text


def wait_released(stack: Stack, task_id: str, *, timeout: float = 60) -> None:
    """The worker cleared `worker_id` (delete refuses a task that a worker still holds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = stack.sql("select worker_id from public.tasks where id = %s", [uuid.UUID(task_id)])
        if rows and rows[0][0] is None:
            return
        time.sleep(0.5)
    raise AssertionError("the worker never released the task")


@dataclass(frozen=True)
class FinishedTask:
    user: EvalUser
    task_id: str


@pytest.fixture(scope="session")
def finished_task(e2e_stack: Stack) -> FinishedTask:
    """One `[e2e:fix]` task run to `completed` through the API (plan approved over HTTP); read by
    the tests that only look at a finished timeline (noise toggle, long output)."""
    stack, owner = e2e_stack, e2e_stack.user(FIX_USER)
    link_github(stack, owner)
    resp = stack.api(
        owner, "POST", "/v1/tasks",
        json={
            "repo": fakes.REPO,
            "base_branch": "main",
            "description": f"{fakes.TAG_FIX} Page 2 of `paginate` repeats the last item of page 1.",
            "failing_test": "tests/test_pager.py::test_second_page",
        },
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task"]["id"]
    deadline = time.monotonic() + DONE / 1000
    while time.monotonic() < deadline:
        row = stack.sql("select status from public.tasks where id = %s", [uuid.UUID(task_id)])[0]
        if row[0] in TERMINAL:
            assert row[0] == "completed", row
            return FinishedTask(owner, task_id)
        for (approval_id,) in stack.sql(
            "select id from public.approvals where task_id = %s and status = 'pending'",
            [uuid.UUID(task_id)],
        ):
            stack.api(
                owner, "POST", f"/v1/tasks/{task_id}/approvals/{approval_id}",
                json={"decision": "approve"},
            )  # fmt: skip
        time.sleep(0.5)
    raise AssertionError("the shared fix task did not finish")


__all__ = ["expect"]
