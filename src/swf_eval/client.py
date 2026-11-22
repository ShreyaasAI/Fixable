"""The eval's stand-in user (m9-contract §0.16): creates tasks over the real API and drives them
over SSE, approving every approval and answering every question with EVAL_ANSWER."""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from swf.states import TERMINAL_STATUSES

EVAL_ANSWER = "No human is available. Use your best judgement and continue."


class CreateRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"task creation refused: {status} {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class DriveResult:
    status: str
    approvals: dict[str, int] = field(default_factory=dict)
    questions: int = 0


@dataclass
class _Event:
    id: int | None = None
    type: str = "message"
    data: list[str] = field(default_factory=list)


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return resp.reason_phrase
    return detail if isinstance(detail, str) else json.dumps(detail)


class EvalClient:
    def __init__(self, http: httpx.Client, jwt: str) -> None:
        self.http = http
        self._auth = {"Authorization": f"Bearer {jwt}"}

    def link_github_token(self, token: str) -> None:
        resp = self.http.post(
            "/v1/me/github-token", json={"provider_token": token}, headers=self._auth
        )
        if resp.status_code != 204:
            raise RuntimeError(f"linking the GitHub token failed: {resp.status_code}")

    def create_task(
        self, *, repo: str, base_branch: str, description: str, failing_test: str
    ) -> uuid.UUID:
        body = {
            "repo": repo,
            "base_branch": base_branch,
            "description": description,
            "failing_test": failing_test,
        }
        resp = self.http.post("/v1/tasks", json=body, headers=self._auth)
        if resp.status_code != 201:
            raise CreateRefused(resp.status_code, _detail(resp))
        return uuid.UUID(resp.json()["task"]["id"])

    def status(self, task_id: uuid.UUID) -> str:
        resp = self.http.get(f"/v1/tasks/{task_id}", headers=self._auth)
        resp.raise_for_status()
        return str(resp.json()["status"])

    def cancel(self, task_id: uuid.UUID) -> None:
        resp = self.http.post(f"/v1/tasks/{task_id}/cancel", headers=self._auth)
        if resp.status_code not in (200, 409):  # 409: already terminal
            resp.raise_for_status()

    def _events(self, task_id: uuid.UUID, last_id: int | None) -> Iterator[_Event]:
        headers = dict(self._auth)
        if last_id is not None:
            headers["Last-Event-ID"] = str(last_id)
        with self.http.stream("GET", f"/v1/tasks/{task_id}/events", headers=headers) as resp:
            resp.raise_for_status()
            event = _Event()
            for line in resp.iter_lines():
                if line == "":
                    if event.data or event.id is not None:
                        yield event
                    event = _Event()
                    continue
                if line.startswith(":"):
                    yield _Event(type="")  # keep-alive: lets the caller check the clock
                    continue
                name, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if name == "id" and value.isdigit():
                    event.id = int(value)
                elif name == "event":
                    event.type = value
                elif name == "data":
                    event.data.append(value)

    def drive(
        self,
        task_id: uuid.UUID,
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
        stop: Callable[[], bool] | None = None,
    ) -> DriveResult:
        """Until a terminal status; past `deadline` (or `stop()` true) the task is cancelled."""
        approvals: Counter[str] = Counter()
        questions = 0
        seen: set[str] = set()
        last_id: int | None = None

        def give_up() -> DriveResult:
            self.cancel(task_id)
            return DriveResult(self.status(task_id), dict(approvals), questions)

        while True:
            if clock() >= deadline or (stop is not None and stop()):
                return give_up()
            try:
                for event in self._events(task_id, last_id):
                    if clock() >= deadline or (stop is not None and stop()):
                        return give_up()
                    if not event.type:
                        continue
                    data: dict[str, Any] = json.loads("\n".join(event.data) or "{}")
                    if event.type == "approval_requested":
                        approval_id = str(data["approval_id"])
                        if approval_id not in seen:
                            seen.add(approval_id)
                            approvals[str(data.get("kind"))] += 1
                            self.http.post(
                                f"/v1/tasks/{task_id}/approvals/{approval_id}",
                                json={"decision": "approve"},
                                headers=self._auth,
                            )
                    elif event.type == "question_asked":
                        question_id = str(data["question_id"])
                        if question_id not in seen:
                            seen.add(question_id)
                            questions += 1
                            self.http.post(
                                f"/v1/tasks/{task_id}/answers/{question_id}",
                                json={"answer": EVAL_ANSWER},
                                headers=self._auth,
                            )
                    if event.id is not None:
                        last_id = event.id
                    if event.type == "status" and data.get("to") in TERMINAL_STATUSES:
                        return DriveResult(str(data["to"]), dict(approvals), questions)
            except httpx.TransportError:
                pass  # dropped stream: reconnect with Last-Event-ID
            time.sleep(0.2)
