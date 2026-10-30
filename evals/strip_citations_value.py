"""Value test for scripts/strip_citations.py: does stripping citations stop check_claims from failing on a citation's date?

Eight invented wiki lines, each ending in a citation that carries a date, are checked against an invented source note,
once as written and once stripped. One model (default `air-qwen`), `sensitive=true`, the roster file is only read.

    uv run python evals/strip_citations_value.py --roster roster.json [--model air-qwen] [--repeats 1]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "scripts"))
from minions import tools  # noqa: E402
from minions.errors import MinionError  # noqa: E402
from strip_citations import strip_citations  # noqa: E402

spec = importlib.util.spec_from_file_location("tool_trials", HERE.parent / "scripts" / "tool_trials.py")
tt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tt)

SOURCE = """Harbor survey, import day 2026-03-04
Files scanned: 10174
With GPS: 1712
Missing dates: 3 files in folder Velmar-2019
Largest folder: Oskarsvik-2021 (412 files)
Copy finished: 604 GB of 931 GB
Duplicates removed: 88
Cameras found: 2 (Luma X1, Luma X2)
"""

LINES = [
    "The survey scanned 10,174 files. (source: raw/2026-10-10-harbor-survey.md)",
    "1,712 files have GPS data. (source: raw/2026-10-10-harbor-survey.md)",
    "Three files in the Velmar-2019 folder are missing dates. (sources: raw/2026-10-10-harbor-survey.md, raw/2026-10-12-followup.md)",
    "The largest folder is Oskarsvik-2021 with 412 files. [[harbor-survey|survey notes]] (conversation 2026-10-12)",
    "The copy stopped at 604 GB of 931 GB. ([report](https://example.org/reports/2026-10-11/copy.html))",
    "88 duplicates were removed.[^2]",
    "Two cameras were found, the Luma X1 and the Luma X2. (source: raw/2026-10-11-camera-list.md)",
    "The import day was 2026-03-04. See https://example.org/2026-10-10/notes for the log.",
]


def run(roster, text: str) -> tuple[str, str]:
    try:
        out = tools.check_claims(roster, text, SOURCE)
        return "ok", f"{sum(c['supported'] for c in out['claims'])}/{len(out['claims'])} supported"
    except MinionError as e:
        return "error", f"{e.code}: {str(e)[:90]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roster", required=True)
    ap.add_argument("--model", default="air-qwen")
    ap.add_argument("--repeats", type=int, default=1)
    a = ap.parse_args()
    roster = tt.pinned_roster(json.loads(Path(a.roster).expanduser().read_text()), a.model)
    tally = {"raw": Counter(), "stripped": Counter()}
    for n, line in enumerate(LINES, 1):
        for _ in range(a.repeats):
            for mode, text in (("raw", line), ("stripped", strip_citations(line))):
                kind, detail = run(roster, text)
                dropped = kind == "error" and "leave out what the text says" in detail
                tally[mode]["calls"] += 1
                tally[mode]["errors"] += kind == "error"
                tally[mode]["dropped_numbers"] += dropped
                print(f"line {n} {mode:8} {kind:5} {detail}", flush=True)
    print()
    for mode, c in tally.items():
        print(f"{mode:9} calls {c['calls']}  errors {c['errors']}  'text leaves out' errors {c['dropped_numbers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
