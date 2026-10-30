"""Live eval for summarize_url through the real roster (MINION_ROSTER or MINION_CONFIG must be set).

Twenty public pages, each with key terms chosen before the run. A page passes when the call succeeds, the summary
contains at least one key term (case-insensitive), and the summary passes summarize's content check again against
the page text fetched independently (no invented number, date or name). Also reports repairs (calls whose first
reply named an invented term) and p50/p95 latency. Bar: 18 of 20 pass, p95 under 60 s.

    MINION_ROSTER=path/to/roster.json uv run python scripts/eval_summarize_url.py [--max-words 80]
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
from minions.config import Roster  # noqa: E402
from minions.errors import MinionError  # noqa: E402
from minions.values import make_summary_check  # noqa: E402

PAGES = [  # (url, key terms: at least one must appear in the summary)
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


def source_text(url: str) -> str:
    _, ctype, raw = fetch.fetch(url)
    return fetch.page_text(ctype, raw)[:tools.URL_TEXT_CHARS]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-words", type=int, default=80)
    ap.add_argument("--only", type=int, help="run only the first N pages")
    a = ap.parse_args()
    log = Path(tempfile.mkdtemp()) / "calls.jsonl"
    os.environ["MINION_LOG"] = str(log)
    roster = Roster.from_env()
    rows, passed = [], 0
    for url, keys in PAGES[: a.only]:
        t = time.time()
        verdict, summary = "", ""
        try:
            out = tools.summarize_url(roster, url, a.max_words)
            summary = out["summary"]
            lat = time.time() - t
            hit = [k for k in keys if k.lower() in summary.lower()]
            try:
                make_summary_check(source_text(url), a.max_words)({"summary": summary})
                invented = ""
            except MinionError as e:
                invented = str(e)
            ok = bool(hit) and not invented
            verdict = "PASS" if ok else ("INVENTED " + invented if invented else "NO KEY TERM")
        except MinionError as e:
            lat, ok, verdict = time.time() - t, False, "ERROR " + str(e)[:150]
        except Exception as e:   # a page the fetch layer could not read, etc.
            lat, ok, verdict = time.time() - t, False, f"ERROR {type(e).__name__}: {e}"[:200]
        passed += ok
        rows.append(lat)
        print(f"{'PASS' if ok else 'FAIL'}  {lat:5.1f}s  {url}\n      {verdict if not ok else summary[:140]}", flush=True)
    lat = sorted(rows)
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    repairs = sum(1 for c in calls if c.get("check_failures") and not c.get("error"))
    print(f"\nscore {passed}/{len(rows)}  p50 {p(0.5):.1f}s  p95 {p(0.95):.1f}s  max {lat[-1]:.1f}s  repairs fired {repairs}")
    return 0 if passed >= 0.9 * len(rows) and p(0.95) < 60 else 1


if __name__ == "__main__":
    sys.exit(main())
