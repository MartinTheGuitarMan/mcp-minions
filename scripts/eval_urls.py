"""Score a list of public pages through `summarize_url`, each against key terms chosen before the run.

Plain code: it only *scores* a tool that uses a model, so it lives here, not in the MCP tool list. It needs the real roster
(MINION_ROSTER or MINION_CONFIG must be set) and the network (it fetches each page through the same guarded fetch layer
as the tool, twice: once for the tool, once to check the summary against the page).

    MINION_ROSTER=roster.json uv run python scripts/eval_urls.py                  # the built-in 20 pages
    MINION_ROSTER=roster.json uv run python scripts/eval_urls.py --pages my.json  # your own list

`my.json` is a list of `{"url": "https://...", "keys": ["term", "other term"]}` (or `["https://...", ["term"]]`). A page
passes when the call succeeds, the summary contains at least one key term (case-insensitive), and the summary passes
summarize's content check again against the page text fetched independently (no invented number, date or name). Also
reports repairs (calls whose first reply named an invented term) and p50/p95 latency. Exit code 0 when at least 90% of the
pages pass; latency is reported only. Calls are sensitive=true (local models) unless `--public`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from minions import fetch, tools  # noqa: E402
from minions.errors import MinionError  # noqa: E402
from minions.values import make_summary_check  # noqa: E402

BAR = 0.9

DEFAULT_PAGES = [  # (url, key terms: at least one must appear in the summary)
    ("https://www.gnu.org/licenses/gpl-3.0.html", ["GPL", "General Public License", "copyleft", "free software"]),
    ("https://www.sqlite.org/about.html", ["database", "SQL"]),
    ("https://www.rfc-editor.org/rfc/rfc2119.txt", ["MUST", "requirement", "SHOULD", "key words"]),
    ("https://en.wikipedia.org/wiki/Hippopotamus", ["hippopotamus", "Africa", "semiaquatic", "herbivor"]),
    ("https://en.wikipedia.org/wiki/Sweden", ["Sweden", "Scandinavia", "Stockholm", "Nordic"]),
    ("https://www.postgresql.org/about/", ["PostgreSQL", "open source", "database"]),
    ("https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404", ["404", "not found", "status"]),
    ("https://docs.python.org/3/library/json.html", ["JSON", "encode", "decode", "serializ"]),
    ("https://www.w3.org/TR/WCAG21/", ["accessib", "WCAG", "guidelines"]),
    ("https://opensource.org/license/mit", ["MIT", "license", "permission"]),
    # ten more, picked before the first run
    ("https://www.iana.org/help/example-domains", ["example", "domain", "reserved"]),
    ("https://www.apache.org/licenses/LICENSE-2.0", ["Apache", "License", "Version 2.0"]),
    ("https://en.wikipedia.org/wiki/Giraffe", ["giraffe", "Africa", "tall", "ungulate", "neck"]),
    ("https://www.rfc-editor.org/rfc/rfc1918.txt", ["private", "address", "Internet"]),
    ("https://docs.python.org/3/library/pathlib.html", ["path", "pathlib", "filesystem"]),
    ("https://www.sqlite.org/whentouse.html", ["SQLite", "appropriate", "database"]),
    ("https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/200", ["200", "OK", "success"]),
    ("https://www.gnu.org/philosophy/free-sw.html", ["free software", "freedom", "users"]),
    ("https://opensource.org/osd", ["Open Source Definition", "open source", "license"]),
    ("https://en.wikipedia.org/wiki/Lighthouse", ["lighthouse", "tower", "light", "navigation"]),
]


def load_pages(path: str) -> list[tuple[str, list[str]]]:
    """Read a page list file. Raises ValueError naming the problem (not a list, a missing url, no key terms)."""
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{path}: cannot read as JSON: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: must be a non-empty list")
    pages = []
    for i, item in enumerate(raw, 1):
        url, keys = (item.get("url"), item.get("keys")) if isinstance(item, dict) else (item[0], item[1]) if (
            isinstance(item, list) and len(item) == 2) else (None, None)
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"{path}: item {i} has no url")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k.strip() for k in keys):
            raise ValueError(f"{path}: item {i} ({url}) needs at least one key term")
        pages.append((url.strip(), [k.strip() for k in keys]))
    return pages


def source_text(url: str) -> str:
    _, ctype, raw = fetch.fetch(url)
    return fetch.page_text(ctype, raw)[:tools.URL_TEXT_CHARS]


def judge(summary: str, keys: list[str], invented: str) -> tuple[bool, str]:
    """(passed, verdict). `invented` is the content check's complaint about the summary, or empty."""
    hit = [k for k in keys if k.lower() in summary.lower()]
    if invented:
        return False, "INVENTED " + invented
    if not hit:
        return False, "NO KEY TERM"
    return True, "PASS"


def score_page(roster, url: str, keys: list[str], max_words: int, sensitive: bool = True,
               summarize=None, source=None) -> dict:
    """One page -> {url, ok, verdict, latency_s, summary}. `summarize` and `source` are injectable for tests."""
    summarize = summarize or (lambda u: tools.summarize_url(roster, u, max_words, sensitive))
    source = source or source_text
    t = time.time()
    summary = ""
    try:
        summary = summarize(url)["summary"]
        lat = time.time() - t
        try:
            make_summary_check(source(url), max_words)({"summary": summary})
            invented = ""
        except MinionError as e:
            invented = str(e)
        ok, verdict = judge(summary, keys, invented)
    except MinionError as e:
        lat, ok, verdict = time.time() - t, False, "ERROR " + str(e)[:150]
    except Exception as e:   # a page the fetch layer could not read, etc.
        lat, ok, verdict = time.time() - t, False, f"ERROR {type(e).__name__}: {e}"[:200]
    return {"url": url, "ok": ok, "verdict": verdict, "latency_s": lat, "summary": summary}


def percentile(sorted_xs: list[float], q: float) -> float:
    return sorted_xs[min(len(sorted_xs) - 1, int(q * len(sorted_xs)))]


def main(argv: list[str] | None = None, pages=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pages", help="JSON file with the page list (default: the built-in 20 pages)")
    ap.add_argument("--max-words", type=int, default=80)
    ap.add_argument("--only", type=int, help="run only the first N pages")
    ap.add_argument("--public", action="store_true", help="sensitive=false: cloud models on the route may answer too")
    a = ap.parse_args(argv)
    try:
        pages = load_pages(a.pages) if a.pages else (pages or DEFAULT_PAGES)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    from minions.config import Roster
    log = Path(tempfile.mkdtemp()) / "calls.jsonl"
    os.environ["MINION_LOG"] = str(log)
    roster = Roster.from_env()
    rows = []
    for url, keys in pages[: a.only]:
        r = score_page(roster, url, keys, a.max_words, sensitive=not a.public)
        rows.append(r)
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['latency_s']:5.1f}s  {url}\n      "
              f"{r['verdict'] if not r['ok'] else r['summary'][:140]}", flush=True)
    passed = sum(r["ok"] for r in rows)
    lat = sorted(r["latency_s"] for r in rows)
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    repairs = sum(1 for c in calls if c.get("check_failures") and not c.get("error"))
    print(f"\nscore {passed}/{len(rows)}  p50 {percentile(lat, 0.5):.1f}s  p95 {percentile(lat, 0.95):.1f}s  "
          f"max {lat[-1]:.1f}s  repairs fired {repairs}")
    return 0 if passed >= BAR * len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
