"""Hard stops for the eval run (m9-contract §0.15)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    max_usd: float = 1.0
    max_calls: int = 4000
    max_prs: int = 22
    max_seconds: float = 6 * 3600
    max_consecutive_model_failures: int = 5

    def exceeded(
        self, *, usd: float, calls: int, prs: int, elapsed: float, model_failures: int
    ) -> str | None:
        """The first limit reached (>= triggers), in contract order; None if none."""
        if usd >= self.max_usd:
            return "budget_usd"
        if calls >= self.max_calls:
            return "max_calls"
        if prs >= self.max_prs:
            return "max_prs"
        if elapsed >= self.max_seconds:
            return "max_hours"
        if model_failures >= self.max_consecutive_model_failures:
            return "model_failures"
        return None


class SharedBudget:
    """Counters shared by the driver threads under one lock; elapsed time from `clock`."""

    def __init__(self, budget: Budget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._clock = clock
        self._start = clock()
        self._lock = threading.Lock()
        self._usd = 0.0
        self._calls = 0
        self._prs = 0
        self._streak = 0

    @property
    def usd(self) -> float:
        with self._lock:
            return self._usd

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    @property
    def prs(self) -> int:
        with self._lock:
            return self._prs

    def elapsed(self) -> float:
        return self._clock() - self._start

    def add(self, *, usd: float = 0.0, calls: int = 0, prs: int = 0) -> None:
        with self._lock:
            self._usd += usd
            self._calls += calls
            self._prs += prs

    def task_done(self, *, model_failed: bool) -> None:
        """A model/provider failure extends the streak; any other outcome resets it."""
        with self._lock:
            self._streak = self._streak + 1 if model_failed else 0

    def exceeded(self) -> str | None:
        with self._lock:
            return self.budget.exceeded(
                usd=self._usd,
                calls=self._calls,
                prs=self._prs,
                elapsed=self._clock() - self._start,
                model_failures=self._streak,
            )
