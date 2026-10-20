"""Token counts for one tool call, collected from every model call it makes (voters, shortcut, judge, defence).
The call log turns them into the saving estimate. Counts are whatever the provider reported; a reply without
usage is recorded as unknown, never guessed."""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field

_current: contextvars.ContextVar["Tally | None"] = contextvars.ContextVar("minion_tally", default=None)


@dataclass
class Tally:
    calls: list = field(default_factory=list)   # (cloud, prompt_tokens or None, completion_tokens or None)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, cloud: bool, tokens_in, tokens_out) -> None:
        ok = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
        with self._lock:
            self.calls.append((bool(cloud), ok(tokens_in), ok(tokens_out)))

    def summary(self) -> dict:
        """Totals over all model calls. Token fields are absent when no provider reported any."""
        out: dict = {"model_calls": len(self.calls), "cloud": any(c for c, _, _ in self.calls)}
        for key, idx in (("prompt_tokens", 1), ("completion_tokens", 2)):
            known = [(c[0], c[idx]) for c in self.calls if c[idx] is not None]
            if known:
                out[key] = sum(v for _, v in known)
                out["cloud_" + key] = sum(v for cloud, v in known if cloud)
        if any(c[1] is None or c[2] is None for c in self.calls) and self.calls:
            out["tokens_partial"] = True
        return out


def start() -> Tally:
    t = Tally()
    _current.set(t)
    return t


def stop() -> None:
    _current.set(None)


def note(cfg, tokens_in, tokens_out) -> None:
    """Called by the backends after every model reply (also when the reply carried no usage)."""
    t = _current.get()
    if t is not None:
        t.add(cfg.cloud, tokens_in, tokens_out)
