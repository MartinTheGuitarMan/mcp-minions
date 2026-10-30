"""scripts/strip_citations.py: wiki-shaped lines (invented content), what must stay, and the effect on check_claims."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from minions import tools

spec = importlib.util.spec_from_file_location("strip_citations", Path(__file__).resolve().parent.parent / "scripts" / "strip_citations.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

CASES = [   # (line as a wiki page has it, expected)
    ("A 931 GB drive held 604 GB when the copy stopped. (source: raw/2026-10-10-copy-notes.md)",
     "A 931 GB drive held 604 GB when the copy stopped."),
    ("The copy was read-only, so nothing was lost (source: raw/2026-10-10-copy-notes.md).",
     "The copy was read-only, so nothing was lost."),
    ("- Copy one file per folder. (sources: raw/2026-10-10-copy-notes.md, raw/2026-10-12-followup.md)",
     "- Copy one file per folder."),
    ("The plan changed on the third day. (conversation 2026-10-12)", "The plan changed on the third day."),
    ("See the [[apfs-clones|clone note]] for sizes.", "See the clone note for sizes."),
    ("Sizes come from [[apfs-clones]] and [[disk-usage#Totals|the totals]].", "Sizes come from apfs-clones and the totals."),
    ("Dates follow the [EXIF guide](https://example.org/exif/guide.html) closely.", "Dates follow the EXIF guide closely."),
    ("About 1,712 files have GPS. [source](https://example.org/a_(b)/c)", "About 1,712 files have GPS."),
    ("About 1,712 files have GPS.[^3]", "About 1,712 files have GPS."),
    ("Most photos are from 2019 [1] and 2021 [2, 3].", "Most photos are from 2019 and 2021."),
    ("Details at https://example.org/photos/2026-10-10/report.html, and more below.", "Details at, and more below."),
    ("Read <https://example.org/x> first.", "Read first."),
    ("![chart](img/2026-10-10-chart.png) Totals by year.", "Totals by year."),
    ("The scan found 3 gaps (import day) on 2026-03-04. (source: raw/scan-2026-10-11.md)",
     "The scan found 3 gaps (import day) on 2026-03-04."),
    ("Counts (2026-03-04) are final. (source: raw/a.md)", "Counts (2026-03-04) are final."),
    ("  - Nested item with a note. [[raw/2026-10-10-notes.md]]", "  - Nested item with a note."),
    ("No citation here at all, 42 items in 7 folders.", "No citation here at all, 42 items in 7 folders."),
    ("A table row | 1,453 GB | 821 GB | (source: raw/disk-2026-10-10.md)", "A table row | 1,453 GB | 821 GB |"),
    ("Dates were off by one day (see [[exif-offsets]]).", "Dates were off by one day (see exif-offsets)."),
    ("", ""),
]


@pytest.mark.parametrize("line,expected", CASES)
def test_wiki_lines(line, expected):
    assert sc.strip_citations(line) == expected


@pytest.mark.parametrize("line,expected", CASES)
def test_idempotent_and_digits_outside_citations_survive(line, expected):
    once = sc.strip_citations(line)
    assert sc.strip_citations(once) == once
    # every number the expected line has must survive; the digits of removed citations and urls may go
    assert re.findall(r"\d[\d,.]*", once) == re.findall(r"\d[\d,.]*", expected)


def test_multiline_keeps_breaks_and_indent():
    text = "# Title\n\n- one (source: raw/a.md)\n    - two [[b]]\n"
    assert sc.strip_citations(text) == "# Title\n\n- one\n    - two b\n"


def test_non_citation_parentheses_and_brackets_stay():
    s = "Totals (rounded) are in [brackets] and (from the scan) too."
    assert sc.strip_citations(s) == s


def test_a_planted_bad_regex_would_eat_numbers():
    # the digits of the claim survive even when a citation sits in the middle of the sentence
    assert sc.strip_citations("Moved 12 files (source: raw/2026-10-10-x.md) to 3 folders.") == "Moved 12 files to 3 folders."


def test_cli_reads_stdin(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("Two files moved. (source: raw/x.md)\n"))
    assert sc.main([]) == 0
    assert capsys.readouterr().out == "Two files moved.\n"


# ---------------------------------------------------------------- the point of it: check_claims
SRC = "The scan found 3 gaps on 2026-03-04. 1712 files have GPS."
LINE = "The scan found 3 gaps on 2026-03-04. (source: raw/2026-10-11-scan-notes.md)"


def reply(fake, claims):
    fake.replies.append(json.dumps({"claims": [{"claim": c, "supported": True, "quote": q} for c, q in claims]}))


def test_check_claims_fails_on_a_citation_date_and_passes_once_stripped(fake, cfg):
    from minions.errors import SchemaError
    reply(fake, [("The scan found 3 gaps on 2026-03-04.", "found 3 gaps on 2026-03-04")])
    with pytest.raises(SchemaError):
        tools.check_claims(cfg, LINE, SRC)      # the citation's date is a number of the text that no claim repeats
    reply(fake, [("The scan found 3 gaps on 2026-03-04.", "found 3 gaps on 2026-03-04")])
    out = tools.check_claims(cfg, sc.strip_citations(LINE), SRC)
    assert out["claims"][0]["supported"]
