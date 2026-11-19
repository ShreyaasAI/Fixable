"""Postgres access (Supabase in production, plain Postgres + shim in tests).

Two transaction kinds:
- `backend()`: the connection's own role (postgres / service role). Used for every write; the
  caller enforces `user_id` from the verified JWT.
- `as_user(claims)`: `SET LOCAL ROLE authenticated` plus the JWT claims, exactly as Supabase's
  REST layer does, so RLS filters every read. Both settings end with the transaction.

A persistent backend uses a direct or session-mode pooler connection (research-plan §2.3).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

Conn = Connection[DictRow]


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 5) -> None:
        self._pool: ConnectionPool[Conn] = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            connection_class=Connection[DictRow],
            kwargs={"row_factory": dict_row},
        )

    def open(self) -> None:
        self._pool.open(wait=True, timeout=30)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def backend(self) -> Iterator[Conn]:
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    @contextmanager
    def as_user(self, claims: Mapping[str, Any]) -> Iterator[Conn]:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims', %s, true)", [json.dumps(dict(claims))]
            )
            yield conn
