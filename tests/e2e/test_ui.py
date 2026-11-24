"""The 12 UI tests of m10a-contract §4.2. Real API, real worker, real Docker sandbox, real Postgres;
fake model, GitHub, registry and PR publisher. Selectors: only the §2.1 test ids, roles, labels."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable

import fakes
import pytest
from conftest import (
    DONE,
    ERRORS,
    SHORT,
    FinishedTask,
    Stack,
    check_preflight,
    choose_repo,
    confirm_dialog,
    create_task_via_ui,
    drive_to_terminal,
    fill_new_task,
    seqs,
    sign_in,
    task_status,
    wait_released,
    wait_status,
)
from playwright.sync_api import Page, expect

from swf_eval.localstack import EvalUser

pytestmark = pytest.mark.e2e

NOISE = ("guard_check", "phase_model", "command_verdict", "output_guard", "evidence_check")


def row_of(page: Page, task_id: str):  # type: ignore[no-untyped-def]
    return page.get_by_test_id("task-row").and_(page.locator(f'[data-task-id="{task_id}"]'))


def test_sign_in_links_github_and_shows_quota(page: Page, e2e_stack: Stack, user: EvalUser) -> None:
    page.goto("/")
    expect(page).to_have_url(re.compile(r"#/signin"))  # signed out: every route redirects
    sign_in(page, user)
    expect(page.get_by_test_id("user-login")).to_contain_text(fakes.GITHUB_LOGIN)
    for name in ("quota-tasks", "quota-cost", "quota-concurrent"):
        expect(page.get_by_test_id(name)).to_contain_text(re.compile(r"\d.*left"))
    # the browser posted the provider token: the API now holds a linked GitHub token
    me = e2e_stack.api(user, "GET", "/v1/me").json()
    assert me["github_linked"] is True


def test_repo_picker_prefills_default_branch(page: Page, user: EvalUser) -> None:
    sign_in(page, user)
    page.get_by_test_id("nav-new").click()
    select = page.get_by_test_id("repo-select")
    expect(select.locator("option", has_text=fakes.REPO).first).to_be_enabled(timeout=SHORT)
    expect(select.locator("option", has_text=fakes.READONLY_REPO).first).to_be_disabled()
    choose_repo(page, fakes.REPO)
    expect(page.get_by_test_id("branch-input")).to_have_value("main")


def test_preflight_shows_estimate_without_creating(page: Page, user: EvalUser) -> None:
    sign_in(page, user)
    fill_new_task(page, "Estimate only.")
    check_preflight(page)
    result = page.get_by_test_id("preflight-result")
    expect(result).to_contain_text("python:3.12-slim")
    expect(result).to_contain_text("512")
    page.get_by_test_id("nav-dashboard").click()
    expect(page.get_by_test_id("empty-tasks")).to_be_visible()
    expect(page.get_by_test_id("task-row")).to_have_count(0)


def test_preflight_error_is_inline(page: Page, user: EvalUser) -> None:
    # A read-only repo cannot be picked (its option is disabled), so the inline error is shown
    # for the other pre-flight refusal a user can reach: an unknown base branch (422).
    sign_in(page, user)
    fill_new_task(page, "Bad branch.", branch=fakes.MISSING_BRANCH)
    page.get_by_test_id("preflight-button").click()
    field_error = page.get_by_test_id("field-error")
    expect(field_error).to_be_visible(timeout=SHORT)
    expect(field_error).to_have_attribute("data-field", re.compile("branch"))
    summary = page.get_by_test_id("error-summary")
    expect(summary).to_be_visible()
    expect(summary).to_have_attribute("role", "alert")
    expect(summary).to_be_focused()
    expect(page.get_by_test_id("start-button")).to_be_disabled()


def test_task_runs_live_to_pr_with_evidence(page: Page, user: EvalUser) -> None:
    sign_in(page, user)
    create_task_via_ui(page, fakes.TAG_FIX)
    card = page.get_by_test_id("approval-card")
    expect(card.first).to_be_visible(timeout=DONE)  # the plan approval
    page.get_by_test_id("approval-comment").first.fill("Looks right.")
    page.get_by_test_id("approve-button").first.click()
    assert drive_to_terminal(page) == "completed"
    expect(page.get_by_test_id("event-row").first).to_be_visible()
    expect(page.locator('[data-testid="event-row"][data-type="completed"]')).to_have_count(1)
    expect(page.get_by_test_id("evidence-list")).to_be_visible()
    assert page.get_by_test_id("evidence-item").count() >= 1
    link = page.get_by_test_id("pr-link")
    expect(link).to_have_attribute("href", fakes.PR_URL)
    expect(link).to_have_attribute("rel", re.compile("noopener"))


def test_question_card_answers(page: Page, user: EvalUser) -> None:
    sign_in(page, user)
    create_task_via_ui(page, fakes.TAG_ASK)
    card = page.get_by_test_id("question-card")
    expect(card).to_be_visible(timeout=DONE)
    expect(card).to_contain_text(fakes.ASK_QUESTION)
    page.get_by_test_id("answer-input").fill("Yes, keep it.")
    page.get_by_test_id("answer-button").click()
    expect(card).to_be_hidden(timeout=SHORT * 2)
    assert drive_to_terminal(page) == "completed"
    assert page.locator('[data-testid="event-row"][data-type="question_answered"]').count() == 1


def test_reload_resumes_without_duplicates(page: Page, user: EvalUser) -> None:
    sign_in(page, user)
    create_task_via_ui(page, fakes.TAG_ASK)  # parks on a question: a stable mid-run state
    expect(page.get_by_test_id("question-card")).to_be_visible(timeout=DONE)
    before = seqs(page)
    assert before and len(before) == len(set(before))
    page.reload()
    expect(page.get_by_test_id("question-card")).to_be_visible(timeout=SHORT * 2)
    after = seqs(page)
    assert len(after) == len(set(after)), f"duplicate seq after reload: {after}"
    assert set(before) <= set(after)
    # the stream continues after the resume
    page.get_by_test_id("answer-input").fill("Yes, keep it.")
    page.get_by_test_id("answer-button").click()
    assert drive_to_terminal(page) == "completed"
    final = seqs(page)
    assert len(final) == len(set(final)) and final == sorted(final)
    assert set(after) <= set(final) and len(final) > len(after)


def test_noise_toggle(page: Page, finished_task: FinishedTask) -> None:
    sign_in(page, finished_task.user)
    page.goto(f"/#/tasks/{finished_task.task_id}")
    expect(page.get_by_test_id("timeline")).to_be_visible(timeout=SHORT)
    expect(page.get_by_test_id("event-row").first).to_be_visible()
    hidden = page.get_by_test_id("noise-hidden-count")
    expect(hidden).to_contain_text(re.compile(r"[1-9]\d* hidden"))
    for kind in NOISE:
        expect(page.locator(f'[data-testid="event-row"][data-type="{kind}"]')).to_have_count(0)
    page.get_by_test_id("noise-toggle").check()
    expect(page.locator('[data-testid="event-row"][data-type="phase_model"]').first).to_be_visible()
    page.get_by_test_id("noise-toggle").uncheck()
    expect(page.locator('[data-testid="event-row"][data-type="phase_model"]')).to_have_count(0)


def test_cancel_then_delete(page: Page, e2e_stack: Stack, user: EvalUser) -> None:
    sign_in(page, user)
    task_id = create_task_via_ui(page, fakes.TAG_SLOW)
    # the agent is inside the long command: a tool call is on the timeline
    expect(page.locator('[data-testid="event-row"][data-type="tool_call"]').first).to_be_visible(
        timeout=DONE
    )
    assert (e2e_stack.workspaces / task_id).is_dir()
    page.get_by_test_id("cancel-button").click()
    dialog = page.get_by_test_id("confirm-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(task_id[:8])  # names the task
    page.get_by_test_id("confirm-yes").click()
    wait_status(page, "cancelled", timeout=60_000)
    wait_released(e2e_stack, task_id)  # delete refuses a task a worker still holds
    page.get_by_test_id("delete-button").click()
    confirm_dialog(page)
    page.goto("/#/")
    expect(page.get_by_test_id("task-list").or_(page.get_by_test_id("empty-tasks"))).to_be_visible()
    expect(row_of(page, task_id)).to_have_count(0)
    assert e2e_stack.api(user, "GET", f"/v1/tasks/{task_id}").status_code == 404
    deadline = time.monotonic() + 30  # the worker sweeps every 2 s here
    while (e2e_stack.workspaces / task_id).exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not (e2e_stack.workspaces / task_id).exists(), "workspace not removed by the sweep"


def test_long_output_collapsed(page: Page, finished_task: FinishedTask) -> None:
    sign_in(page, finished_task.user)
    page.goto(f"/#/tasks/{finished_task.task_id}")
    expect(page.get_by_test_id("timeline")).to_be_visible(timeout=SHORT)
    long_output = page.get_by_test_id("tool-output").filter(has_text="/usr/lib:").first
    expect(long_output).to_be_attached(timeout=SHORT)
    assert long_output.evaluate("el => el.open") is False  # > 40 lines: collapsed
    long_output.locator("summary").click()
    expect(long_output).to_have_js_property("open", True)
    # a short output stays open
    short = page.get_by_test_id("tool-output").filter(has_not_text="/usr/lib:")
    assert short.count() >= 1
    assert short.first.evaluate("el => el.open") is True


def test_admin_pause_resume_and_kill(new_page: Callable[[], Page], e2e_stack: Stack) -> None:
    member = e2e_stack.take_user()
    admin = e2e_stack.user("e2e-admin")
    member_page, admin_page = new_page(), new_page()

    # a non-admin has no admin link and gets the API's 403 as text
    sign_in(member_page, member)
    expect(member_page.get_by_test_id("nav-admin")).to_have_count(0)
    member_page.goto("/#/admin")
    expect(member_page.get_by_text(re.compile("admins only", re.I)).first).to_be_visible(
        timeout=SHORT
    )

    sign_in(admin_page, admin)
    admin_page.get_by_test_id("nav-admin").click()
    state = admin_page.get_by_test_id("admin-state")
    expect(state).to_have_attribute("data-paused", "false")
    try:
        admin_page.get_by_test_id("pause-all").click()
        _confirm_if_asked(admin_page)
        expect(state).to_have_attribute("data-paused", "true")
        # a paused service refuses at the pre-flight, before Start
        member_page.get_by_test_id("nav-new").click()
        choose_repo(member_page, fakes.REPO)
        member_page.get_by_test_id("description-input").fill("Paused.")
        member_page.get_by_test_id("preflight-button").click()
        expect(member_page.locator(ERRORS).first).to_contain_text(
            re.compile("service_paused|paused", re.I), timeout=SHORT
        )
    finally:
        e2e_stack.api(admin, "POST", "/v1/admin/resume", json={})
    admin_page.reload()
    expect(admin_page.get_by_test_id("admin-state")).to_have_attribute("data-paused", "false")

    # kill one user: their running task is cancelled and they are locked out
    task_id = create_task_via_ui(member_page, fakes.TAG_SLOW)
    expect(
        member_page.locator('[data-testid="event-row"][data-type="tool_call"]').first
    ).to_be_visible(timeout=DONE)
    admin_page.get_by_test_id("nav-admin").click()
    row = admin_page.get_by_test_id("admin-user-row").and_(
        admin_page.locator(f'[data-user-id="{member.user_id}"]')
    )
    expect(row).to_have_attribute("data-disabled", "false", timeout=SHORT)
    row.get_by_test_id("kill-user").click()
    confirm_dialog(admin_page)
    expect(row).to_have_attribute("data-disabled", "true", timeout=SHORT)
    # The kill cancelled the member's task. A disabled account gets 403 on every route, so the
    # member's page can no longer load it: check the row, then the member's view of the lockout.
    rows = e2e_stack.sql("select status from public.tasks where id = %s", [uuid.UUID(task_id)])
    assert rows[0][0] == "cancelled", rows
    member_page.reload()
    expect(member_page.locator(ERRORS).first).to_contain_text(
        re.compile("account_disabled|disabled", re.I), timeout=SHORT
    )

    admin_page.get_by_test_id("admin-user-row").and_(
        admin_page.locator(f'[data-user-id="{member.user_id}"]')
    ).get_by_test_id("resume-user").click()
    _confirm_if_asked(admin_page)
    expect(row).to_have_attribute("data-disabled", "false", timeout=SHORT)


def _confirm_if_asked(page: Page) -> None:
    dialog = page.get_by_test_id("confirm-dialog")
    try:
        dialog.wait_for(state="visible", timeout=1500)
    except Exception:  # noqa: BLE001 - no dialog for this action
        return
    page.get_by_test_id("confirm-yes").click()
    expect(dialog).to_be_hidden(timeout=SHORT)


def _focus_ring_visible(page: Page) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const s = getComputedStyle(document.activeElement);
                const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0;
                return outline || (s.boxShadow && s.boxShadow !== 'none');
            }"""
        )
    )


def _type_into(page: Page, test_id: str, text: str) -> None:
    page.get_by_test_id(test_id).focus()
    page.keyboard.type(text)


def test_keyboard_only_create(page: Page, e2e_stack: Stack, user: EvalUser) -> None:
    sign_in(page, user)
    page.get_by_test_id("nav-new").focus()
    page.keyboard.press("Enter")
    # the repo: ArrowDown through the select until the pushable repo is selected
    select = page.get_by_test_id("repo-select")
    expect(select.locator("option", has_text=fakes.REPO).first).to_be_enabled(timeout=SHORT)
    select.focus()
    for _ in range(6):
        if fakes.REPO in (select.evaluate("el => el.selectedOptions[0]?.textContent ?? ''") or ""):
            break
        page.keyboard.press("ArrowDown")
    expect(select.locator("option:checked")).to_contain_text(fakes.REPO)
    expect(page.get_by_test_id("branch-input")).to_have_value("main")
    _type_into(page, "description-input", f"{fakes.TAG_FIX} Page 2 repeats the last item.")
    page.get_by_test_id("preflight-button").focus()
    assert _focus_ring_visible(page), "no visible focus ring on the Check button"
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("preflight-result")).to_be_visible(timeout=SHORT)
    start = page.get_by_test_id("start-button")
    expect(start).to_be_enabled()
    start.focus()
    assert _focus_ring_visible(page), "no visible focus ring on the Start button"
    page.keyboard.press("Enter")
    page.wait_for_url("**/#/tasks/*", timeout=SHORT)

    approve = page.get_by_test_id("approve-button").first
    expect(approve).to_be_visible(timeout=DONE)
    approve.focus()
    assert _focus_ring_visible(page), "no visible focus ring on the Approve button"
    page.keyboard.press("Enter")
    assert drive_to_terminal(page) == "completed"
    assert task_status(page) == "completed"
