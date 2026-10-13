"""Optional per-call log: one JSON line per tool call, so everyday use shows which models earn their keep.
Set MINION_LOG to a file path to turn it on. It records tool, sensitivity, which models answered, whether the
shortcut or judge ran, failovers, latency and the error code; never the text, the labels or the answer."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path


def _path() -> Path | None:
    p = os.environ.get("MINION_LOG", "").strip()
    return Path(p).expanduser() if p else None


def record(tool: str, sensitive: bool, started: float, text: str, meta: dict | None, error: str | None) -> None:
    p = _path()
    if p is None:
        return
    meta = meta or {}
    short = meta.get("shortcut") or {}
    row = {"ts": round(time.time()), "tool": tool, "sensitive": sensitive, "chars": len(text or ""),
           "latency_s": round(time.time() - started, 2), "error": error,
           "fast": meta.get("fast"), "judge": meta.get("judge"),
           "shortcut": (None if not short else ("answered" if not short.get("escalated") else "escalated")),
           "failover": meta.get("failover") or None, "rate_limited": meta.get("rate_limited") or None,
           "injection_removed": len(meta.get("injection_removed") or []) or None}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({k: v for k, v in row.items() if v is not None}) + "\n")
    except OSError:
        pass   # logging must never break a tool call


def report(days: float = 7.0) -> dict:
    p = _path()
    if p is None or not p.is_file():
        return {"log": str(p) if p else None, "calls": 0,
                "note": "no log yet: set MINION_LOG to a file path and make some calls"}
    cutoff = time.time() - days * 86400
    rows = []
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) >= cutoff:
            rows.append(r)
    lat = sorted(r["latency_s"] for r in rows)
    pct = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else None
    short = Counter(r["shortcut"] for r in rows if r.get("shortcut"))
    return {"days": days, "calls": len(rows),
            "by_tool": dict(Counter(r["tool"] for r in rows)),
            "sensitive_share": round(sum(1 for r in rows if r.get("sensitive")) / len(rows), 2) if rows else None,
            "errors": dict(Counter(r["error"] for r in rows if r.get("error"))),
            "latency_s": {"p50": pct(0.5), "p95": pct(0.95), "max": lat[-1] if lat else None},
            "shortcut": dict(short) or None,
            "judge_calls": sum(1 for r in rows if r.get("judge")),
            "failovers": sum(1 for r in rows if r.get("failover")),
            "rate_limited": sum(1 for r in rows if r.get("rate_limited")),
            "injection_segments_removed": sum(r.get("injection_removed") or 0 for r in rows),
            "answered_by": dict(Counter(m for r in rows for m in (r.get("fast") or [])))}
