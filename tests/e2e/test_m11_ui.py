"""m11 §4.2: theme toggle, the sign-in hero, the repo combobox, and the 375/1440 layouts.

Set SWF_SCREENSHOT_DIR to also save one PNG per screen, width and theme."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from e2e import fakes
from e2e.conftest import SHORT, FinishedTask, Stack, check_preflight, fill_new_task, sign_in
from swf_eval.localstack import EvalUser

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("SWF_SCREENSHOT_DIR")


def html_theme(page: Page) -> str:
    return str(page.evaluate("() => document.documentElement.dataset.theme ?? ''"))


def test_theme_toggle_switches_and_persists(page: Page) -> None:
    page.goto("/#/signin")
    toggle = page.get_by_test_id("theme-toggle")
    toggle.get_by_role("radio", name="Light").click()
    assert html_theme(page) == "light"
    page.reload()
    assert html_theme(page) == "light"
    expect(page.get_by_test_id("theme-toggle").get_by_role("radio", name="Light")).to_be_checked()
    page.get_by_test_id("theme-toggle").get_by_role("radio", name="Dark").click()
    assert html_theme(page) == "dark"


def test_theme_follows_the_system_by_default(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    page.goto("/#/signin")
    page.evaluate("() => localStorage.removeItem('fixable-theme')")
    page.reload()
    assert html_theme(page) == "light"
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_hero_only_on_sign_in(page: Page, user: EvalUser) -> None:
    page.goto("/#/signin")
    hero = page.get_by_test_id("hero-3d")
    expect(hero).to_be_visible(timeout=SHORT)
    expect(hero).to_have_attribute("aria-hidden", "true")
    sign_in(page, user)
    expect(page.get_by_test_id("hero-3d")).to_have_count(0)


def test_hero_is_static_with_reduced_motion(page: Page) -> None:
    page.emulate_media(reduced_motion="reduce")
    page.goto("/#/signin")
    expect(page.get_by_test_id("hero-3d")).to_have_attribute("data-motion", "static")


def test_repo_combobox_filters_and_marks_readonly(page: Page, user: EvalUser) -> None:
    sign_in(page, user)
    page.get_by_test_id("nav-new").click()
    trigger = page.get_by_test_id("repo-select")
    expect(trigger).to_have_attribute("role", "combobox")
    trigger.click()
    expect(page.get_by_role("option", name=fakes.REPO)).to_be_enabled(timeout=SHORT)
    expect(page.get_by_role("option", name=fakes.READONLY_REPO)).to_be_disabled()
    page.keyboard.type("readonly")
    expect(page.get_by_role("option", name=fakes.REPO, exact=True)).to_have_count(0)
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()


def _check_layout(page: Page, name: str, width: int, theme: str) -> None:
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{name} at {width}px ({theme}) scrolls sideways by {overflow}px"
    if SHOTS:
        out = Path(SHOTS)
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f"{name}-{width}-{theme}.png"), full_page=True)


@pytest.mark.parametrize("width", [375, 1440])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_screens_fit_and_capture(
    page: Page, e2e_stack: Stack, finished_task: FinishedTask, width: int, theme: str
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto("/#/signin")
    page.evaluate("() => localStorage.removeItem('fixable-theme')")
    page.reload()
    expect(page.get_by_test_id("hero-3d")).to_be_visible(timeout=SHORT)
    page.wait_for_timeout(600)  # let the hero draw a frame
    _check_layout(page, "signin", width, theme)

    sign_in(page, finished_task.user)
    expect(page.get_by_test_id("task-list")).to_be_visible(timeout=SHORT)
    _check_layout(page, "dashboard", width, theme)

    fill_new_task(page, "Rounding drifts by a cent on split invoices.")
    check_preflight(page)
    _check_layout(page, "new-task", width, theme)

    page.goto(f"/#/tasks/{finished_task.task_id}")
    expect(page.get_by_test_id("task-status")).to_have_attribute(
        "data-status", "completed", timeout=SHORT
    )
    expect(page.get_by_test_id("evidence-list")).to_be_visible(timeout=SHORT)
    _check_layout(page, "task-detail", width, theme)

    page.get_by_test_id("signout").click()
    sign_in(page, e2e_stack.user("e2e-admin"))
    page.get_by_test_id("nav-admin").click()
    expect(page.get_by_test_id("admin-state")).to_be_visible(timeout=SHORT)
    _check_layout(page, "admin", width, theme)
