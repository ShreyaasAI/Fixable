"""M8 T3: in-process token-bucket rate limiter (m8-contract §1.2)."""

from __future__ import annotations

import threading

import pytest
from swf.ratelimit import RateLimiter


class Clock:
    """Manual clock: tests advance `t` by hand, so nothing sleeps."""

    t = 0.0

    def __call__(self) -> float:
        return self.t


def test_burst_then_refused_with_wait_time() -> None:
    c = Clock()
    rl = RateLimiter(per_minute=60, burst=3, clock=c)
    assert [rl.hit("a") for _ in range(3)] == [None, None, None]
    assert rl.hit("a") == pytest.approx(1.0)


def test_refill_gives_one_token_and_refused_hit_uses_none() -> None:
    c = Clock()
    rl = RateLimiter(per_minute=60, burst=3, clock=c)
    for _ in range(3):
        assert rl.hit("a") is None
    assert rl.hit("a") is not None
    c.t += 1.0
    assert rl.hit("a") is None
    assert rl.hit("a") is not None


def test_keys_are_independent() -> None:
    c = Clock()
    rl = RateLimiter(per_minute=60, burst=1, clock=c)
    assert rl.hit("a") is None
    assert rl.hit("a") is not None
    assert rl.hit("b") is None


def test_refill_is_capped_at_burst() -> None:
    c = Clock()
    rl = RateLimiter(per_minute=60, burst=3, clock=c)
    assert rl.hit("a") is None
    c.t += 3600
    results = [rl.hit("a") for _ in range(10)]
    assert results.count(None) == 3


def test_full_buckets_are_pruned_beyond_max_keys() -> None:
    c = Clock()
    rl = RateLimiter(per_minute=60, burst=1, clock=c, max_keys=2)
    assert rl.hit("a") is None
    assert rl.hit("b") is None
    c.t = 5.0
    assert rl.hit("c") is None
    assert len(rl) <= 2
    assert len(rl) == 1


def test_thread_safety_exact_token_count() -> None:
    c = Clock()
    rl = RateLimiter(per_minute=60, burst=50, clock=c)
    results: list[float | None] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [rl.hit("k") for _ in range(100)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 800
    assert results.count(None) == 50


@pytest.mark.parametrize(
    "kwargs",
    [
        {"per_minute": 0, "burst": 1},
        {"per_minute": 60, "burst": 0},
        {"per_minute": 60, "burst": 1, "max_keys": 0},
    ],
)
def test_invalid_arguments_raise_value_error(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RateLimiter(**kwargs)  # type: ignore[arg-type]
