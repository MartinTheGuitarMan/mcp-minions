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
_usage: dict[str, list] = {}   # tokens in, tokens out, calls, per minion, since this server process started
_quota: dict[str, tuple[float, dict]] = {}   # latest x-ratelimit-* headers a provider sent, per minion


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


def record(cfg: Config, headers) -> None:
    """Keep the provider's own rate-limit headers (Groq, Mistral and others send x-ratelimit-*), verbatim:
    providers name and scale them differently, so they are reported as sent and never interpreted."""
    seen = {k.lower(): v for k, v in headers.items() if k.lower().startswith("x-ratelimit-")}
    if seen:
        with _lock:
            _quota[_key(cfg)] = (time.monotonic(), seen)


def add_usage(cfg: Config, tokens_in, tokens_out) -> None:
    """Count the tokens a provider says a reply used (OpenAI-style or System One usage objects)."""
    if isinstance(tokens_in, int) and isinstance(tokens_out, int):
        with _lock:
            u = _usage.setdefault(_key(cfg), [0, 0, 0])
            u[0] += tokens_in
            u[1] += tokens_out
            u[2] += 1


def usage(cfg: Config) -> dict | None:
    with _lock:
        u = _usage.get(_key(cfg))
    return {"tokens_in": u[0], "tokens_out": u[1], "calls": u[2]} if u else None


def provider_quota(cfg: Config) -> dict | None:
    with _lock:
        got = _quota.get(_key(cfg))
    if got is None:
        return None
    return {"as_reported_by_provider": got[1], "age_seconds": round(time.monotonic() - got[0])}


def reset() -> None:
    with _lock:
        _hits.clear()
        _quota.clear()
        _usage.clear()
