"""Optional per-call log: one JSON line per tool call, so everyday use shows which models earn their keep.
Set MINION_LOG to a file path to turn it on. It records tool, sensitivity, which models answered, whether the
shortcut or judge ran, failovers, latency, the error code and the token counts the providers reported (summed over every
model call of the tool call); never the text, the labels or the answer."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path


def _path() -> Path | None:
    p = os.environ.get("MINION_LOG", "").strip()
    return Path(p).expanduser() if p else None


def event(kind: str, **fields) -> None:
    """Lifecycle event line (load, unload, refused). Same file, no text; report() keeps these apart from tool calls."""
    p = _path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": round(time.time()), "event": kind, **{k: v for k, v in fields.items() if v is not None}}) + "\n")
    except OSError:
        pass


def record(tool: str, sensitive: bool, started: float, text: str, meta: dict | None, error: str | None,
           usage: dict | None = None) -> None:
    p = _path()
    if p is None:
        return
    meta = meta or {}
    usage = usage or {}
    short = meta.get("shortcut") or {}
    row = {"ts": round(time.time()), "tool": tool, "sensitive": sensitive, "chars": len(text or ""),
           "latency_s": round(time.time() - started, 2), "error": error,
           "fast": meta.get("fast"), "judge": meta.get("judge"),
           "shortcut": (None if not short else ("answered" if not short.get("escalated") else "escalated")),
           "shortcut_model": short.get("model"),
           "failover": meta.get("failover") or None, "rate_limited": meta.get("rate_limited") or None,
           "injection_removed": len(meta.get("injection_removed") or []) or None,
           "downgraded": True if meta.get("downgraded") else None,
           "check_failures": meta.get("check_failures"),
           "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
           "cloud_prompt_tokens": usage.get("cloud_prompt_tokens") or None,
           "cloud_completion_tokens": usage.get("cloud_completion_tokens") or None,
           "cloud": True if usage.get("cloud") else None, "tokens_partial": usage.get("tokens_partial")}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({k: v for k, v in row.items() if v is not None}) + "\n")
    except OSError:
        pass   # logging must never break a tool call


CHARS_PER_TOKEN = 4
SAVING_METHOD = (
    "ESTIMATE, not a billing figure. Counts only calls that succeeded, stayed entirely on local models (no cloud "
    "model took part) and did not fail over; errored, failed-over and cloud calls count zero. Input side: the text "
    f"the calling model did not have to process, estimated from the logged text length at about {CHARS_PER_TOKEN} "
    "characters per token (the provider's prompt tokens are not used: they include the minion's own prompt and "
    "every quorum or judge call). Output side: the completion tokens the local models reported for those calls, "
    "all model calls of the tool call summed (quorum and judge calls included; none if the provider gave no count). "
    "The caller still pays for passing the text and reading the result, and a real answer might have been longer or shorter.")


def _saved(r: dict) -> tuple[int, int, bool]:
    """(input tokens, output tokens, counted) the calling model was spared by this call."""
    if r.get("error") or r.get("failover") or r.get("cloud"):
        return 0, 0, False
    return int(r.get("chars") or 0) // CHARS_PER_TOKEN, int(r.get("completion_tokens") or 0), True


def _tokens(rows: list) -> dict:
    """Old log lines carry no token fields; they add nothing to the totals and are counted as unknown."""
    seen = [r for r in rows if r.get("prompt_tokens") is not None or r.get("completion_tokens") is not None]
    cloud_in = sum(r.get("cloud_prompt_tokens") or 0 for r in seen)
    cloud_out = sum(r.get("cloud_completion_tokens") or 0 for r in seen)
    tot_in = sum(r.get("prompt_tokens") or 0 for r in seen)
    tot_out = sum(r.get("completion_tokens") or 0 for r in seen)
    kept = [_saved(r) for r in rows]
    return {"handled_locally": {"prompt": tot_in - cloud_in, "completion": tot_out - cloud_out},
            "handled_by_cloud": {"prompt": cloud_in, "completion": cloud_out},
            "calls_without_token_counts": len(rows) - len(seen),
            "estimated_kept_off_calling_model": {
                "estimate": True, "input": sum(k[0] for k in kept), "output": sum(k[1] for k in kept),
                "total": sum(k[0] + k[1] for k in kept), "calls_counted": sum(1 for k in kept if k[2]),
                "method": SAVING_METHOD}}


def report(days: float = 7.0) -> dict:
    p = _path()
    if p is None or not p.is_file():
        return {"log": str(p) if p else None, "calls": 0,
                "note": "no log yet: set MINION_LOG to a file path and make some calls"}
    cutoff = time.time() - days * 86400
    rows, events = [], []
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) < cutoff:
            continue
        (events if "event" in r else rows).append(r)
    lat = sorted(r["latency_s"] for r in rows)
    pct = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else None
    short = Counter(r["shortcut"] for r in rows if r.get("shortcut"))
    kinds = Counter(e["event"] for e in events)
    return {"days": days, "calls": len(rows),
            **({"lifecycle": {"loads": kinds["load"], "unloads": kinds["unload"], "refused": kinds["refused"],
                              "refused_by": dict(Counter(e.get("gate") for e in events if e["event"] == "refused"))}}
               if events else {}),
            "by_tool": dict(Counter(r["tool"] for r in rows)),
            "sensitive_share": round(sum(1 for r in rows if r.get("sensitive")) / len(rows), 2) if rows else None,
            "errors": dict(Counter(r["error"] for r in rows if r.get("error"))),
            "latency_s": {"p50": pct(0.5), "p95": pct(0.95), "max": lat[-1] if lat else None},
            "shortcut": dict(short) or None,
            "judge_calls": sum(1 for r in rows if r.get("judge")),
            "failovers": sum(1 for r in rows if r.get("failover")),
            # value-check failures of fast models that the judge then repaired (unrepaired ones are in errors)
            "check_failures_repaired": dict(Counter(c for r in rows for c in r.get("check_failures") or [])),
            "rate_limited": sum(1 for r in rows if r.get("rate_limited")),
            "injection_segments_removed": sum(r.get("injection_removed") or 0 for r in rows),
            "tokens": _tokens(rows),
            "answered_by": dict(Counter([m for r in rows for m in (r.get("fast") or [])] +
                                         [r["shortcut_model"] for r in rows if r.get("shortcut") == "answered" and r.get("shortcut_model")]))}
