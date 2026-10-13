"""Per-minion request counter for the `limits: {rpm}` setting. The server builds its Roster on every call,
so the counters live at module level. Counting is client-side only: free tiers rarely report remaining quota."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from .config import Config
from .errors import RateLimited

WINDOW = 60.0
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _key(cfg: Config) -> str:
    return f"{cfg.name}@{cfg.base_url}"


def _prune(q: deque, now: float) -> None:
    while q and now - q[0] >= WINDOW:
        q.popleft()


def acquire(cfg: Config) -> None:
    """Count one request, or raise RateLimited without sending it when the local cap is used up."""
    if cfg.rpm is None:
        return
    now = time.monotonic()
    with _lock:
        q = _hits[_key(cfg)]
        _prune(q, now)
        if len(q) >= cfg.rpm:
            raise RateLimited(f"{cfg.name}: local limit of {cfg.rpm} requests per minute reached",
                              retry_after=WINDOW - (now - q[0]))
        q.append(now)


def used(cfg: Config) -> int:
    now = time.monotonic()
    with _lock:
        q = _hits[_key(cfg)]
        _prune(q, now)
        return len(q)


def reset() -> None:
    with _lock:
        _hits.clear()
