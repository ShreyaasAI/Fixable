"""Backend API v1, M1 subset (implementation-plan §4).

Every `/v1` route requires a Supabase user JWT. Reads go through `Database.as_user` (RLS);
writes go through `Database.backend` with the user id taken from the verified token, and each
write records its audit row in the same transaction.

Deferred: pre-flight (M2), SSE events (M4), evidence (M5), task delete + workspace (M6),
quota enforcement / rate limits / kill switch (M8).
"""

import re
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from swf import store
from swf.auth import AuthError, JwtVerifier, Principal
from swf.db import Database
from swf.github import GitHubAuthError, GitHubClient, GitHubError
from swf.tokens import TokenCipher, TokenDecryptError

REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
BRANCH_CHARS_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AppDeps:
    db: Database
    verifier: JwtVerifier
    cipher: TokenCipher
    github: GitHubClient
    clock: Callable[[], datetime] = _utcnow

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


class TaskCreate(_Body):
    repo: str
    base_branch: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=10_000)
    failing_test: str | None = Field(default=None, min_length=1, max_length=500)

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


class TaskCreated(BaseModel):
    task: store.Task
    preflight: None = None  # M2


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


class RepoOut(BaseModel):
    full_name: str
    default_branch: str
    private: bool
    can_push: bool


# --- app -----------------------------------------------------------------------------------


def create_app(
    deps: AppDeps,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="SWF API", version="0.1.0", lifespan=lifespan)

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
        return CurrentUser(principal=principal, profile=profile)

    User = Annotated[CurrentUser, Depends(current_user)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    def me(user: User) -> Me:
        with deps.db.as_user(user.principal.claims) as conn:
            usage = store.get_usage(conn, user.id, deps.today())
        with deps.db.backend() as conn:
            linked = store.get_encrypted_github_token(conn, user.id) is not None
        return Me(
            user_id=user.id,
            github_login=user.profile.github_login,
            is_admin=user.profile.is_admin,
            github_linked=linked,
            usage_today=usage,
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
        with deps.db.backend() as conn:
            blob = store.get_encrypted_github_token(conn, user.id)
        if blob is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "github_token_missing")
        try:
            found = deps.github.list_repos(deps.cipher.decrypt(blob))
        except (GitHubAuthError, TokenDecryptError):
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

    @app.post("/v1/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(user: User, body: TaskCreate) -> TaskCreated:
        with deps.db.backend() as conn:
            task = store.create_task(
                conn,
                user.id,
                repo=body.repo,
                base_branch=body.base_branch,
                description=body.description,
                failing_test=body.failing_test,
                day=deps.today(),
            )
        return TaskCreated(task=task)

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

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel(user: User, task_id: uuid.UUID) -> store.Task:
        with deps.db.backend() as conn:
            return store.cancel_task(conn, user.id, task_id)

    return app
