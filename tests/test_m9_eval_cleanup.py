"""PR cleanup (m9-contract §0.3, §1.5): the plan is only this run's PRs in the allowed repo, and
`apply_cleanup` closes and deletes only what a fresh GET proves is the harness's own PR. Fake
GitHub API on `httpx.MockTransport`; no network. Also the two new `GitHubClient` methods."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from m9_eval_fakes import ALLOWED_REPO, rec
from swf_eval.cleanup import apply_cleanup, cleanup_plan
from swf_eval.metrics import TaskRecord

from swf.github import GitHubClient, GitHubError

TOKEN = "ghs_CLEANUPSENTINEL" + "0123456789abcdef0123456789"
HOST = "https://github.com"


def tid(n: int) -> str:
    return str(uuid.UUID(int=n))


def branch(n: int) -> str:
    return f"agent/{tid(n)}"


def pr_url(n: int, repo: str = ALLOWED_REPO) -> str:
    return f"{HOST}/{repo}/pull/{n}"


def record(n: int, url: str | None) -> TaskRecord:
    return rec("single", f"{n:02}", 10.0, True, task_id=tid(n), pr_url=url, status="completed")


# --- cleanup_plan --------------------------------------------------------------------------


def test_plan_lists_the_prs_of_this_run() -> None:
    records = [record(1, pr_url(11)), record(2, pr_url(12))]
    assert sorted(cleanup_plan(records, ALLOWED_REPO)) == [(11, branch(1)), (12, branch(2))]


@pytest.mark.parametrize(
    "url",
    [
        None,
        f"{HOST}/ShreyaasAI/Fixable/pull/3",  # another repo
        f"{HOST}/other/swf-pr-sandbox/pull/3",
        f"{HOST}/{ALLOWED_REPO}/pull/3/files",  # not exactly /pull/<n>
        f"{HOST}/{ALLOWED_REPO}/pull/3#issuecomment-1",
        f"{HOST}/{ALLOWED_REPO}/pull/3?x=1",
        f"{HOST}/{ALLOWED_REPO}/pull/abc",
        f"{HOST}/{ALLOWED_REPO}/pull/",
        f"{HOST}/{ALLOWED_REPO}/issues/3",
        f"http://github.com/{ALLOWED_REPO}/pull/3",
        f"https://evil.example/{ALLOWED_REPO}/pull/3",
        f"https://github.com.evil.example/{ALLOWED_REPO}/pull/3",
        f"{HOST}/{ALLOWED_REPO}/pull/3 ",
        "not a url",
    ],
)
def test_plan_excludes_a_pr_url_that_is_not_exactly_this_repos_pull(url: str | None) -> None:
    records = [record(1, url), record(2, pr_url(12))]
    assert cleanup_plan(records, ALLOWED_REPO) == [(12, branch(2))]


def test_plan_takes_the_branch_from_the_records_own_task_id() -> None:
    plan = cleanup_plan([record(7, pr_url(70))], ALLOWED_REPO)
    assert plan == [(70, f"agent/{tid(7)}")]


@pytest.mark.parametrize("repo", ["ShreyaasAI/Fixable", "shreyaasai/fixable", "ShreyaasAI/swf-ci"])
def test_plan_refuses_a_disallowed_repo_first(repo: str) -> None:
    with pytest.raises(ValueError):
        cleanup_plan([record(1, f"{HOST}/{repo}/pull/1")], repo)


def test_plan_of_nothing_is_empty() -> None:
    assert cleanup_plan([], ALLOWED_REPO) == []


# --- apply_cleanup -------------------------------------------------------------------------


@dataclass
class FakePulls:
    """GET/PATCH /repos/{repo}/pulls/{n} and DELETE /repos/{repo}/git/refs/heads/{branch}."""

    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(
        self,
        number: int,
        *,
        ref: str,
        html_url: str | None = None,
        head_repo: str = ALLOWED_REPO,
        base_repo: str = ALLOWED_REPO,
        state: str = "open",
    ) -> None:
        self.pulls[number] = {
            "number": number,
            "state": state,
            "html_url": html_url or pr_url(number),
            "head": {"ref": ref, "repo": {"full_name": head_repo}},
            "base": {"ref": f"seed/{number}", "repo": {"full_name": base_repo}},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        prefix = f"/repos/{ALLOWED_REPO}"
        if request.url.host != "api.github.com" or not path.startswith(prefix + "/"):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(prefix) :]
        if rest.startswith("/pulls/"):
            number = int(rest.rsplit("/", 1)[1])
            if number not in self.pulls:
                return httpx.Response(404, json={"message": "Not Found"})
            if request.method == "GET":
                return httpx.Response(200, json=self.pulls[number])
            if request.method == "PATCH":
                self.pulls[number]["state"] = json.loads(request.content)["state"]
                return httpx.Response(200, json=self.pulls[number])
        if rest.startswith("/git/refs/heads/") and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(405, json={"message": "no"})

    def client(self) -> GitHubClient:
        return GitHubClient(transport=httpx.MockTransport(self.handler))

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]

    def patched(self) -> list[int]:
        return [int(unquote(r.url.path).rsplit("/", 1)[1]) for r in self.calls("PATCH")]

    def deleted(self) -> list[str]:
        marker = "/git/refs/heads/"
        return [unquote(r.url.path).split(marker, 1)[1] for r in self.calls("DELETE")]


def test_apply_closes_and_deletes_a_matching_pr() -> None:
    fake = FakePulls()
    fake.add(11, ref=branch(1))
    log = apply_cleanup(fake.client(), TOKEN, ALLOWED_REPO, [(11, branch(1))])
    assert fake.patched() == [11] and fake.deleted() == [branch(1)]
    assert json.loads(fake.calls("PATCH")[0].content) == {"state": "closed"}
    assert isinstance(log, list) and log and all(isinstance(ln, str) for ln in log)
    assert fake.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_apply_skips_everything_that_does_not_match_and_acts_only_on_the_rest() -> None:
    fake = FakePulls()
    fake.add(11, ref=branch(1))  # matches
    fake.add(12, ref="agent/" + tid(99))  # head.ref differs from the recorded branch
    fake.add(13, ref=branch(3), head_repo="someone-else/swf-pr-sandbox")  # a fork's head
    fake.add(14, ref=branch(4), html_url=pr_url(999))  # html_url differs from the recorded one
    fake.add(15, ref=branch(5), base_repo="other/elsewhere")  # base is not the allowed repo
    fake.add(16, ref=branch(6))  # matches
    plan = [(11, branch(1)), (12, branch(2)), (13, branch(3)), (14, branch(4)), (15, branch(5))]
    plan.append((16, branch(6)))
    log = apply_cleanup(fake.client(), TOKEN, ALLOWED_REPO, plan)
    assert fake.patched() == [11, 16]
    assert fake.deleted() == [branch(1), branch(6)]
    for skipped in (12, 13, 14, 15):
        assert any(str(skipped) in ln for ln in log), (skipped, log)  # a logged reason each
    assert fake.pulls[12]["state"] == fake.pulls[13]["state"] == "open"


def test_apply_never_merges_and_only_uses_get_patch_delete() -> None:
    fake = FakePulls()
    for n in (1, 2, 3):
        fake.add(n, ref=branch(n))
    apply_cleanup(fake.client(), TOKEN, ALLOWED_REPO, [(n, branch(n)) for n in (1, 2, 3)])
    assert {r.method for r in fake.requests} <= {"GET", "PATCH", "DELETE"}
    assert not any("merge" in unquote(r.url.path) for r in fake.requests)
    assert all(r.url.host == "api.github.com" for r in fake.requests)


def test_apply_checks_each_pr_with_a_get_before_touching_it() -> None:
    fake = FakePulls()
    fake.add(11, ref=branch(1))
    apply_cleanup(fake.client(), TOKEN, ALLOWED_REPO, [(11, branch(1))])
    methods = [r.method for r in fake.requests]
    assert methods[0] == "GET" and methods.index("PATCH") > 0 and methods.index("DELETE") > 0


def test_apply_skips_a_pr_that_no_longer_exists() -> None:
    fake = FakePulls()
    log = apply_cleanup(fake.client(), TOKEN, ALLOWED_REPO, [(11, branch(1))])
    assert fake.patched() == [] and fake.deleted() == []
    assert any("11" in ln for ln in log)


def test_apply_refuses_a_disallowed_repo_before_any_request() -> None:
    fake = FakePulls()
    fake.add(11, ref=branch(1))
    with pytest.raises(ValueError):
        apply_cleanup(fake.client(), TOKEN, "ShreyaasAI/Fixable", [(11, branch(1))])
    assert fake.requests == []


def test_apply_never_logs_the_token() -> None:
    fake = FakePulls()
    fake.add(11, ref=branch(1))
    fake.add(12, ref="agent/" + tid(98))
    log = apply_cleanup(fake.client(), TOKEN, ALLOWED_REPO, [(11, branch(1)), (12, branch(2))])
    assert not any(TOKEN in ln for ln in log)


# --- GitHubClient.close_pull / delete_branch -----------------------------------------------


def test_close_pull_patches_state_closed() -> None:
    fake = FakePulls()
    fake.add(5, ref=branch(5))
    fake.client().close_pull(TOKEN, ALLOWED_REPO, 5)
    assert fake.patched() == [5]
    assert json.loads(fake.calls("PATCH")[0].content) == {"state": "closed"}
    assert fake.pulls[5]["state"] == "closed"


def test_delete_branch_deletes_an_agent_or_seed_ref() -> None:
    fake = FakePulls()
    client = fake.client()
    client.delete_branch(TOKEN, ALLOWED_REPO, branch(5))
    client.delete_branch(TOKEN, ALLOWED_REPO, "seed/01-paginate-off-by-one")
    assert fake.deleted() == [branch(5), "seed/01-paginate-off-by-one"]


@pytest.mark.parametrize(
    "bad",
    [
        "main",
        "master",
        "develop",
        "agent/x",
        "agent/../main",
        "agent/" + tid(1) + "/extra",
        "seed/../main",
        "seed/01-a/b",
        "refs/heads/main",
        "+seed/01-a",
        "",
        "agent/" + tid(1) + "\n",
        "feature/m9",
    ],
)
def test_delete_branch_refuses_any_other_branch_without_a_request(bad: str) -> None:
    fake = FakePulls()
    with pytest.raises((ValueError, GitHubError)):
        fake.client().delete_branch(TOKEN, ALLOWED_REPO, bad)
    assert fake.requests == []
