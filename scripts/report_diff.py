"""Compare two lines of `accuracy.jsonl` (written by `scripts/quick_eval.py`) and flag regressions.

Plain code, no model: it lives here, not in the MCP tool list.

    uv run python scripts/report_diff.py accuracy.jsonl            # the last line against the one before
    uv run python scripts/report_diff.py old.jsonl new.jsonl       # the last line of each
    uv run python scripts/report_diff.py accuracy.jsonl --old 0 --new -1   # any two lines by index

Flags have a level. `regression` makes the exit code 1; `note` and `info` do not.
  regression  a tool's score fell by 0.10 or more; `invented` rose; a tool that was scored is not scored now; an error
              code appeared that the old line did not have, or an old one occurs more often
  note        the invented count is still above zero; a smaller score drop; a partial run; a different set of answering
              models; a different roster hash; sensitive flag changed (local only against cloud allowed); p95 more than
              twice as slow
  info        a score went up
One quick_eval case is about 12 points of score, so a drop under 0.10 can be one wrong case; it is a note, not a verdict.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DROP = 0.10      # score drop that counts as a regression
SLOW = 2.0       # p95 ratio worth a note
EPS = 1e-9


def _flag(level: str, tool: str | None, msg: str) -> dict:
    return {"level": level, "tool": tool, "msg": msg}


def _n(x) -> float:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def compare(old: dict, new: dict) -> list[dict]:
    """Flags for `new` against `old`, regressions first. Never raises on a missing field: a line from an older
    quick_eval version just has fewer things to compare."""
    flags: list[dict] = []
    ot, nt = old.get("tools") or {}, new.get("tools") or {}
    for tool in sorted(set(ot) | set(nt)):
        o, n = ot.get(tool), nt.get(tool)
        if o and _n(o.get("scored")) > 0 and not (n and _n(n.get("scored")) > 0):
            flags.append(_flag("regression", tool, "was scored before and is not scored now"
                               + (f" (skipped: {n['skipped']})" if n and n.get("skipped") else "")))
            continue
        if not o or not n or o.get("score") is None or n.get("score") is None:
            if n and not o:
                flags.append(_flag("note", tool, "new tool, nothing to compare"))
            continue
        delta = round(_n(n["score"]) - _n(o["score"]), 3)
        if delta <= -DROP + EPS:
            flags.append(_flag("regression", tool, f"score {o['score']} -> {n['score']} ({delta:+.2f}; "
                               f"{n.get('correct')}/{n.get('scored')} against {o.get('correct')}/{o.get('scored')})"))
        elif delta < 0:
            flags.append(_flag("note", tool, f"score {o['score']} -> {n['score']} ({delta:+.2f}), under the {DROP} bar"))
        elif delta > 0:
            flags.append(_flag("info", tool, f"score {o['score']} -> {n['score']} ({delta:+.2f})"))
        oe, ne = o.get("errors") or {}, n.get("errors") or {}
        for code, cnt in sorted(ne.items()):
            if code not in oe:
                flags.append(_flag("regression", tool, f"new error code {code} (x{cnt})"))
            elif cnt > oe[code]:
                flags.append(_flag("regression", tool, f"error {code} more often ({oe[code]} -> {cnt})"))
        op, np_ = o.get("p95_s"), n.get("p95_s")
        if isinstance(op, (int, float)) and isinstance(np_, (int, float)) and op > 0 and np_ > SLOW * op and np_ - op >= 1:
            flags.append(_flag("note", tool, f"p95 {op}s -> {np_}s"))
        if (o.get("models") or {}).keys() != (n.get("models") or {}).keys() and (o.get("models") or n.get("models")):
            flags.append(_flag("note", tool, f"answered by {sorted(o.get('models') or {})} before, "
                                             f"{sorted(n.get('models') or {})} now"))
    oi, ni = int(_n(old.get("invented"))), int(_n(new.get("invented")))
    if ni > oi:
        flags.append(_flag("regression", None, f"invented answers {oi} -> {ni}"))
    elif ni > 0:
        flags.append(_flag("note", None, f"invented answers still {ni}"))
    if new.get("partial"):
        flags.append(_flag("note", None, "the new run is partial (budget hit): scores cover fewer cases"))
    if old.get("roster_hash") != new.get("roster_hash"):
        flags.append(_flag("note", None, f"roster changed ({old.get('roster_hash')} -> {new.get('roster_hash')})"))
    if old.get("sensitive") != new.get("sensitive"):
        flags.append(_flag("note", None, f"sensitive {old.get('sensitive')} -> {new.get('sensitive')}: local only against cloud allowed"))
    order = {"regression": 0, "note": 1, "info": 2}
    return sorted(flags, key=lambda f: order[f["level"]])


def read_lines(path: str) -> list[dict]:
    out = []
    for n, raw in enumerate(Path(path).expanduser().read_text().splitlines(), 1):
        if raw.strip():
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}: line {n} is not JSON: {e}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+", help="one file (last two lines) or two files (last line of each)")
    ap.add_argument("--old", type=int, help="index of the old line (first file)")
    ap.add_argument("--new", type=int, help="index of the new line (the second file if given, else the first)")
    a = ap.parse_args(argv)
    if len(a.files) > 2:
        ap.error("at most two files")
    first = read_lines(a.files[0])
    second = read_lines(a.files[1]) if len(a.files) == 2 else first
    try:
        if len(a.files) == 2:
            old, new = first[-1 if a.old is None else a.old], second[-1 if a.new is None else a.new]
        else:
            old, new = first[-2 if a.old is None else a.old], first[-1 if a.new is None else a.new]
    except IndexError:
        print("not enough lines to compare (need two)", file=sys.stderr)
        return 2
    flags = compare(old, new)
    print(f"old {old.get('date')} (roster {old.get('roster_hash')})  ->  new {new.get('date')} (roster {new.get('roster_hash')})")
    if not flags:
        print("no changes worth flagging")
    for f in flags:
        print(f"{f['level'].upper():10} {f['tool'] or '(run)':15} {f['msg']}")
    return 1 if any(f["level"] == "regression" for f in flags) else 0


if __name__ == "__main__":
    sys.exit(main())
