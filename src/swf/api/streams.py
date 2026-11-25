"""Caps on open event streams (m10b §0.6). Each open `/events` stream holds a threadpool thread,
so the API limits how many are open per user and in all. In-process, like the rate limiters."""

from __future__ import annotations

import threading
import uuid
import weakref
from collections import Counter


class Slot:
    """One open stream. `release()` is idempotent."""

    def __init__(self, owner: StreamSlots, user_id: uuid.UUID) -> None:
        self._owner = owner
        self._user_id = user_id
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._owner._free(self._user_id)

    def release_with(self, obj: object) -> None:
        """Also release when `obj` (the stream generator) is collected, even if it never ran."""
        weakref.finalize(obj, self.release)


class StreamSlots:
    def __init__(self, *, per_user: int, total: int) -> None:
        if per_user < 1 or total < 1:
            raise ValueError("per_user >= 1 and total >= 1 required")
        self._per_user = per_user
        self._total = total
        self._open: Counter[uuid.UUID] = Counter()
        # Reentrant: a slot's GC finalizer can run inside `acquire` on the same thread.
        self._lock = threading.RLock()

    def acquire(self, user_id: uuid.UUID) -> Slot | None:
        """A slot, or None when the user or the whole API is at its cap."""
        with self._lock:
            if self._open[user_id] >= self._per_user or self._open.total() >= self._total:
                return None
            self._open[user_id] += 1
        return Slot(self, user_id)

    def _free(self, user_id: uuid.UUID) -> None:
        with self._lock:
            self._open[user_id] -= 1
            if self._open[user_id] <= 0:
                del self._open[user_id]
