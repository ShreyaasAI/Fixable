"""Backend API v1, M1 + M2 subset (implementation-plan §4).

Every `/v1` route requires a Supabase user JWT. Reads go through `Database.as_user` (RLS);
writes go through `Database.backend` with the user id taken from the verified token, and each
write records its audit row in the same transaction.

`POST /v1/tasks` runs pre-flight (M2) over the GitHub API before writing anything: a missing
repo/branch, no push access, or a GitHub failure creates no row; a pre-flight reject is stored
as a `rejected` task (shown to the user, not counted against the quota); admit/queue store a
`queued` task with its reservation.

`GET /v1/tasks/{id}/events` (M4) streams the task's events as SSE: it polls the `events`
table (no LISTEN/NOTIFY), resumes after `Last-Event-ID` (or `?after=`), sends a `: ping`
comment when idle, and ends once the task is terminal and every event has been sent.

`GET /v1/tasks/{id}/evidence` (M5) lists the task's evidence rows (as_user + RLS); the full
outputs stay in the workspace (`output_ref` is relative to it).

M8 abuse controls (m8-contract §1.6):
- Every `/v1` route spends one token of the user's `api` bucket after auth (an SSE stream
  spends one when it opens); a refusal is 429 `rate_limited` with `Retry-After`. `/healthz` is
  not limited, and a bad token is a 401 before any limiter runs. The limiters are in-process,
  so run the API as one process (`--workers 1`, §0.7).
- `POST /v1/tasks` also spends a `create` token, then checks the global pause (503
  `service_paused`) and the user's quotas (429 `quota_*`) before pre-flight, so a refused
  create makes no GitHub call. The insert transaction repeats the disabled, pause and quota
  checks under locks before `create_task`; a refusal there rolls back (no task, no audit row).
- `POST /v1/admin/kill` and `POST /v1/admin/resume` (admins only) disable or re-enable one
  user, or pause or resume all tasks; a kill cancels every non-terminal task in scope.

M10a: `DELETE /v1/tasks/{id}` removes a terminal, released task; the worker sweeps its
workspace folder.
"""

import json
import math
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from swf import store
from swf.auth import AuthError, JwtVerifier, Principal
from swf.db import Conn, Database
from swf.github import GitHubAuthError, GitHubClient, GitHubError
from swf.preflight import (
    BranchNotFound,
    Capacity,
    PreflightResult,
    PreflightService,
    RepoNotFound,
    RepoNotWritable,
)
from swf.ratelimit import RateLimiter
from swf.settings import AbuseLimits
from swf.tokens import TokenCipher, TokenDecryptError

REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
BRANCH_CHARS_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_DAILY_QUOTAS = frozenset({"quota_tasks_per_day", "quota_cost_per_day"})
Scope = Literal["user", "global"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AppDeps:
    db: Database
    verifier: JwtVerifier
    cipher: TokenCipher
    github: GitHubClient
    preflight: PreflightService
    clock: Callable[[], datetime] = _utcnow
    sse_poll_seconds: float = 1.0
    sse_keepalive_seconds: float = 15.0
    abuse: AbuseLimits = field(default_factory=AbuseLimits)
    monotonic: Callable[[], float] = time.monotonic

    def today(self) -> date:
        return self.clock().astimezone(UTC).date()


@dataclass(frozen=True)
class CurrentUser:
    principal: Principal
    profile: store.Profile

    @property
    def id(self) -> uuid.UUID:
        return self.principal.user_id


# --- request / response bodies -------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoRef(_Body):
    repo: str
    base_branch: str = Field(min_length=1, max_length=255)

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        name = v.split("/", 1)[-1]
        if not REPO_RE.fullmatch(v) or name in {".", ".."}:
            raise ValueError("repo must be 'owner/name'")
        return v

    @field_validator("base_branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        # A conservative subset of git-check-ref-format; the value later reaches git (M6).
        bad = (
            not BRANCH_CHARS_RE.fullmatch(v)
            or v.startswith(("-", "/", "."))
            or v.endswith(("/", ".", ".lock"))
            or ".." in v
            or "//" in v
            or "/." in v
        )
        if bad:
            raise ValueError("invalid branch name")
        return v


class TaskCreate(RepoRef):
    description: str = Field(min_length=1, max_length=10_000)
    failing_test: str | None = Field(default=None, min_length=1, max_length=500)


class TaskCreated(BaseModel):
    task: store.Task
    preflight: PreflightResult


class GitHubTokenIn(_Body):
    provider_token: str = Field(min_length=1, max_length=512)


class DecisionIn(_Body):
    decision: store.Decision
    comment: str | None = Field(default=None, max_length=5000)


class AnswerIn(_Body):
    answer: str = Field(min_length=1, max_length=10_000)


class Me(BaseModel):
    user_id: uuid.UUID
    github_login: str | None
    is_admin: bool
    github_linked: bool
    usage_today: store.Usage
    limits: store.QuotaLimits
    remaining: store.Remaining


class EvidenceOut(BaseModel):
    task_id: uuid.UUID
    status: str
    items: list[store.EvidenceRow]


class RepoOut(BaseModel):
    full_name: str
    default_branch: str
    private: bool
    can_push: bool


class KillIn(_Body):
    user_id: uuid.UUID | None = None


class ResumeOut(BaseModel):
    scope: Scope
    user_id: uuid.UUID | None


class KillOut(ResumeOut):
    cancelled: list[uuid.UUID]


# --- app -----------------------------------------------------------------------------------


def create_app(
    deps: AppDeps,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="SWF API", version="0.1.0", lifespan=lifespan)
    abuse = deps.abuse
    api_limiter = RateLimiter(
        per_minute=abuse.api_per_minute, burst=abuse.api_burst, clock=deps.monotonic
    )
    create_limiter = RateLimiter(
        per_minute=abuse.create_per_minute, burst=abuse.create_burst, clock=deps.monotonic
    )

    def _limit(limiter: RateLimiter, user_id: uuid.UUID) -> None:
        wait = limiter.hit(user_id)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "rate_limited",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

    def _quota_refused(code: str) -> HTTPException:
        headers = None
        if code in _DAILY_QUOTAS:
            now = deps.clock().astimezone(UTC)
            midnight = datetime.combine(now.date() + timedelta(days=1), dt_time(0), UTC)
            wait = math.ceil((midnight - now).total_seconds())
            headers = {"Retry-After": str(max(1, wait))}
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, code, headers=headers)

    def _admission(conn: Conn, user_id: uuid.UUID) -> None:
        """Global pause (503), then the user's quotas (429): §1.6 steps 2-3 and 5."""
        if store.tasks_paused(conn):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service_paused")
        code = store.check_quota(conn, user_id, deps.today(), abuse)
        if code is not None:
            raise _quota_refused(code)

    @app.exception_handler(store.NotFound)
    def _not_found(_request: Request, _exc: store.NotFound) -> JSONResponse:
        return JSONResponse({"detail": "not_found"}, status_code=404)

    @app.exception_handler(store.Conflict)
    def _conflict(_request: Request, exc: store.Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def current_user(authorization: Annotated[str | None, Header()] = None) -> CurrentUser:
        scheme, _, token = (authorization or "").partition(" ")
        try:
            if scheme.lower() != "bearer" or not token:
                raise AuthError("missing bearer token")
            principal = deps.verifier.verify(token.strip())
        except AuthError:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        with deps.db.backend() as conn:
            profile = store.ensure_profile(conn, principal.user_id, principal.github_login)
        if profile.disabled:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="account_disabled")
        _limit(api_limiter, principal.user_id)
        return CurrentUser(principal=principal, profile=profile)

    User = Annotated[CurrentUser, Depends(current_user)]

    def _github_token(user: CurrentUser) -> str:
        """The decrypted GitHub token; 409 `github_token_missing` / `github_token_invalid`."""
        with deps.db.backend() as conn:
            blob = store.get_encrypted_github_token(conn, user.id)
        if blob is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "github_token_missing")
        try:
            return deps.cipher.decrypt(blob)
        except TokenDecryptError:
            raise HTTPException(status.HTTP_409_CONFLICT, "github_token_invalid") from None

    def _preflight(token: str, ref: RepoRef, capacity: Capacity) -> PreflightResult:
        """Pre-flight over GitHub with the API's error mapping (m10a §1.2)."""
        try:
            return deps.preflight.check(token, ref.repo, ref.base_branch, capacity)
        except RepoNotFound:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "repo_not_found") from None
        except BranchNotFound:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "branch_not_found") from None
        except RepoNotWritable:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "repo_not_writable"
            ) from None
        except GitHubAuthError:
            raise HTTPException(status.HTTP_409_CONFLICT, "github_token_invalid") from None
        except GitHubError:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "github_unavailable") from None

    def admin_user(user: User) -> CurrentUser:
        if not user.profile.is_admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admin_only")
        return user

    Admin = Annotated[CurrentUser, Depends(admin_user)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    def me(user: User) -> Me:
        with deps.db.as_user(user.principal.claims) as conn:
            usage = store.get_usage(conn, user.id, deps.today())
            open_tasks = store.open_task_count(conn, user.id)
        with deps.db.backend() as conn:
            linked = store.get_encrypted_github_token(conn, user.id) is not None
        return Me(
            user_id=user.id,
            github_login=user.profile.github_login,
            is_admin=user.profile.is_admin,
            github_linked=linked,
            usage_today=usage,
            limits=store.QuotaLimits.of(abuse),
            remaining=store.remaining(usage, open_tasks, abuse),
        )

    @app.post("/v1/me/github-token", status_code=status.HTTP_204_NO_CONTENT)
    def link_github_token(user: User, body: GitHubTokenIn) -> Response:
        try:
            gh_user = deps.github.get_user(body.provider_token)
        except GitHubAuthError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "github_token_invalid") from None
        except GitHubError:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "github_unavailable") from None
        if user.principal.github_user_id is None or gh_user.id != user.principal.github_user_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "github_account_mismatch")
        with deps.db.backend() as conn:
            store.store_github_token(
                conn,
                user.id,
                encrypted_token=deps.cipher.encrypt(body.provider_token),
                scopes=gh_user.scopes,
                github_user_id=gh_user.id,
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/repos")
    def repos(user: User) -> list[RepoOut]:
        token = _github_token(user)
        try:
            found = deps.github.list_repos(token)
        except GitHubAuthError:
            raise HTTPException(status.HTTP_409_CONFLICT, "github_token_invalid") from None
        except GitHubError:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "github_unavailable") from None
        return [
            RepoOut(
                full_name=r.full_name,
                default_branch=r.default_branch,
                private=r.private,
                can_push=r.can_push,
            )
            for r in found
        ]

    def _checked_preflight(user: CurrentUser, ref: RepoRef) -> PreflightResult:
        """Admission pre-check (so a paused service or an over-quota user causes no GitHub call),
        then pre-flight. Shared by `POST /v1/preflight` and `POST /v1/tasks`."""
        with deps.db.backend() as conn:
            _admission(conn, user.id)
        token = _github_token(user)
        with deps.db.backend() as conn:
            capacity = store.capacity_snapshot(conn)
        return _preflight(token, ref, capacity)

    @app.post("/v1/preflight")
    def preflight(user: User, body: RepoRef) -> PreflightResult:
        """Dry run: the estimate a create would get; no task, no audit row (m10a §1.2)."""
        return _checked_preflight(user, body)

    @app.post("/v1/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(user: User, body: TaskCreate) -> TaskCreated:
        _limit(create_limiter, user.id)
        result = _checked_preflight(user, body)
        rejected = result.decision == "reject"
        with deps.db.backend() as conn:
            # Re-check under locks; raising here rolls the transaction back. The profile share
            # lock makes a concurrent per-user kill wait for this commit (and then cancel the new
            # task); `tasks_paused` does the same for a global kill; `check_quota` holds the
            # per-user advisory lock, so parallel creates cannot both pass (§0.8).
            row = conn.execute(
                "select disabled from public.profiles where user_id = %s for share", [user.id]
            ).fetchone()
            if row is None or row["disabled"]:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "account_disabled")
            _admission(conn, user.id)
            task = store.create_task(
                conn,
                user.id,
                repo=body.repo,
                base_branch=body.base_branch,
                description=body.description,
                failing_test=body.failing_test,
                day=deps.today(),
                status="rejected" if rejected else "queued",
                reservation_mb=None if rejected else result.memory_mb,
                reservation_cpu=None if rejected else result.cpus,
                preflight=result,
            )
        return TaskCreated(task=task, preflight=result)

    @app.get("/v1/tasks")
    def list_tasks(user: User) -> list[store.Task]:
        with deps.db.as_user(user.principal.claims) as conn:
            return store.list_tasks(conn, user.id)

    @app.get("/v1/tasks/{task_id}")
    def get_task(user: User, task_id: uuid.UUID) -> store.Task:
        with deps.db.as_user(user.principal.claims) as conn:
            task = store.get_task(conn, user.id, task_id)
        if task is None:
            raise store.NotFound
        return task

    @app.get("/v1/tasks/{task_id}/evidence")
    def evidence(user: User, task_id: uuid.UUID) -> EvidenceOut:
        with deps.db.as_user(user.principal.claims) as conn:
            task = store.get_task(conn, user.id, task_id)
            if task is None:
                raise store.NotFound
            items = store.list_evidence(conn, user.id, task_id)
        return EvidenceOut(task_id=task.id, status=task.status, items=items)

    @app.get("/v1/tasks/{task_id}/events")
    def events(
        user: User,
        task_id: uuid.UUID,
        after: int = -1,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        with deps.db.as_user(user.principal.claims) as conn:
            if store.get_task(conn, user.id, task_id) is None:
                raise store.NotFound
        cursor = after
        if last_event_id is not None and last_event_id.strip():
            try:
                cursor = int(last_event_id.strip())
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_last_event_id") from None
        claims = user.principal.claims
        user_id = user.id

        def stream() -> Iterator[str]:
            sent = cursor
            last_output = time.monotonic()
            while True:
                with deps.db.as_user(claims) as conn:
                    row = conn.execute(
                        "select status, event_seq from public.tasks where id = %s and user_id = %s",
                        [task_id, user_id],
                    ).fetchone()
                    batch = store.list_events(conn, user_id, task_id, after_seq=sent)
                if row is None:
                    return
                for event in batch:
                    data = {**event.payload, "created_at": event.created_at.isoformat()}
                    yield (
                        f"id: {event.seq}\nevent: {event.type}\n"
                        f"data: {json.dumps(data, default=str)}\n\n"
                    )
                    sent = event.seq
                    last_output = time.monotonic()
                if row["status"] in store.TERMINAL_STATUSES and sent >= row["event_seq"] - 1:
                    return
                if batch and len(batch) >= 500:
                    continue  # more queued up; no sleep
                if time.monotonic() - last_output >= deps.sse_keepalive_seconds:
                    yield ": ping\n\n"
                    last_output = time.monotonic()
                time.sleep(deps.sse_poll_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/tasks/{task_id}/approvals/{approval_id}")
    def decide(
        user: User, task_id: uuid.UUID, approval_id: uuid.UUID, body: DecisionIn
    ) -> store.Approval:
        with deps.db.backend() as conn:
            return store.decide_approval(
                conn, user.id, task_id, approval_id, decision=body.decision, comment=body.comment
            )

    @app.post("/v1/tasks/{task_id}/answers/{question_id}")
    def answer(
        user: User, task_id: uuid.UUID, question_id: uuid.UUID, body: AnswerIn
    ) -> store.Question:
        with deps.db.backend() as conn:
            return store.answer_question(conn, user.id, task_id, question_id, answer=body.answer)

    @app.delete("/v1/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_task(user: User, task_id: uuid.UUID) -> Response:
        with deps.db.backend() as conn:
            store.delete_task(conn, user.id, task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel(user: User, task_id: uuid.UUID) -> store.Task:
        with deps.db.backend() as conn:
            return store.cancel_task(conn, user.id, task_id)

    @app.get("/v1/admin/status")
    def admin_status(_admin: Admin) -> store.SystemStatus:
        with deps.db.backend() as conn:
            return store.system_status(conn)

    @app.post("/v1/admin/kill")
    def admin_kill(admin: Admin, body: KillIn) -> KillOut:
        if body.user_id == admin.id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "cannot_kill_self")
        with deps.db.backend() as conn:
            cancelled = store.admin_kill(conn, admin.id, body.user_id)
        scope: Scope = "global" if body.user_id is None else "user"
        return KillOut(scope=scope, user_id=body.user_id, cancelled=cancelled)

    @app.post("/v1/admin/resume")
    def admin_resume(admin: Admin, body: KillIn) -> ResumeOut:
        with deps.db.backend() as conn:
            store.admin_resume(conn, admin.id, body.user_id)
        scope: Scope = "global" if body.user_id is None else "user"
        return ResumeOut(scope=scope, user_id=body.user_id)

    return app
