"""scripts/report_diff.py against the real shape of an accuracy line (a line written by quick_eval, copied verbatim),
then pairs planted by editing it."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("report_diff", Path(__file__).resolve().parent.parent / "scripts" / "report_diff.py")
rd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rd)

REAL = json.loads('{"date": "2026-10-30", "ts": 1793349182, "roster_hash": "d61a354643b1", "sensitive": true, "tools": {"classify": {"score": 0.75, "correct": 6, "scored": 8, "skipped": null, "errors": {"MINION_SCHEMA": 1}, "p50_s": 1.77, "p95_s": 12.45, "models": {"air-jev-style": 7, "air-qwen": 5, "mini-gemma4b": 5, "air-qwen-judge": 2}}, "tag": {"score": 0.75, "correct": 6, "scored": 8, "skipped": null, "errors": null, "p50_s": 1.11, "p95_s": 1.24, "models": {"air-qwen": 8}}, "extract": {"score": 1.0, "correct": 6, "scored": 6, "skipped": null, "errors": null, "p50_s": 2.73, "p95_s": 3.01, "models": {"air-qwen": 6}}, "summarize": {"score": 1.0, "correct": 5, "scored": 5, "skipped": null, "errors": null, "p50_s": 1.77, "p95_s": 2.32, "models": {"air-qwen": 5}}, "grounded": {"score": 0.75, "correct": 6, "scored": 8, "skipped": null, "errors": null, "p50_s": 21.53, "p95_s": 24.0, "models": {"air-qwen": 8, "mini-gemma4b": 8, "mini-phi4mini": 8}}, "ask": {"score": 1.0, "correct": 9, "scored": 9, "skipped": null, "errors": null, "p50_s": 1.62, "p95_s": 2.02, "models": {"air-qwen": 9}}, "find_sensitive": {"score": 1.0, "correct": 7, "scored": 7, "skipped": null, "errors": null, "p50_s": 2.44, "p95_s": 5.32, "models": {"air-qwen": 7}}}, "invented": 0, "duration_s": 261, "partial": false, "unloaded": null}')


def edit(fn):
    d = copy.deepcopy(REAL)
    fn(d)
    return d


def keys(flags):
    return {(f["level"], f["tool"]) for f in flags}


def test_the_real_line_against_itself_has_no_flags():
    assert rd.compare(REAL, REAL) == []


def test_ts_and_duration_changes_alone_are_not_flags():
    assert rd.compare(REAL, edit(lambda d: d.update(ts=1, duration_s=999, date="2026-11-06"))) == []


def set_tool(tool, **kw):
    def f(d):
        d["tools"][tool].update(kw)
    return f


PLANTED = [   # (name, edit, expected (level, tool) set)
    ("score drop of 0.25", set_tool("extract", score=0.75, correct=4), {("regression", "extract")}),
    ("score drop of exactly 0.10", set_tool("ask", score=0.9, correct=8), {("regression", "ask")}),
    ("score drop of 0.0625 is a note", set_tool("classify", score=0.6875, correct=5), {("note", "classify")}),
    ("score rise is info", set_tool("tag", score=0.875, correct=7), {("info", "tag")}),
    ("tool no longer scored", lambda d: d["tools"].__setitem__("summarize", {"score": None, "correct": 0, "scored": 0, "skipped": {"RATE": 5}, "errors": None, "p50_s": None, "p95_s": None, "models": None}), {("regression", "summarize")}),
    ("tool missing", lambda d: d["tools"].pop("grounded"), {("regression", "grounded")}),
    ("invented rose", lambda d: d.update(invented=1), {("regression", None)}),
    ("new error code", set_tool("tag", errors={"MINION_INVENTED": 1}), {("regression", "tag")}),
    ("old error code more often", set_tool("classify", errors={"MINION_SCHEMA": 3}), {("regression", "classify")}),
    ("same error count is no flag", set_tool("classify", errors={"MINION_SCHEMA": 1}), set()),
    ("partial run", lambda d: d.update(partial=True), {("note", None)}),
    ("different answering models", set_tool("tag", models={"mini-gemma4b": 8}), {("note", "tag")}),
    ("roster changed", lambda d: d.update(roster_hash="aaaaaaaaaaaa"), {("note", None)}),
    ("sensitive flipped", lambda d: d.update(sensitive=False), {("note", None)}),
    ("p95 more than twice", set_tool("ask", p95_s=9.0), {("note", "ask")}),
    ("p95 up under twofold is no flag", set_tool("tag", p95_s=2.0), set()),
]


@pytest.mark.parametrize("name,fn,expected", PLANTED, ids=[p[0] for p in PLANTED])
def test_planted_pairs(name, fn, expected):
    assert keys(rd.compare(REAL, edit(fn))) == expected


def test_improvement_is_not_a_regression():
    flags = rd.compare(edit(set_tool("tag", score=0.5, correct=4)), REAL)
    assert flags and all(f["level"] == "info" for f in flags)


def test_still_invented_is_a_note_not_a_regression():
    old = edit(lambda d: d.update(invented=2))
    assert keys(rd.compare(old, old)) == {("note", None)}


def test_lines_from_an_older_version_do_not_crash():
    assert rd.compare({"tools": {"tag": {"score": 1.0, "scored": 4}}}, {"tools": {"tag": {"score": 0.5, "scored": 4}}})[0]["level"] == "regression"
    assert rd.compare({}, {}) == []


def test_cli_exit_codes_and_input_shapes(tmp_path, capsys):
    f = tmp_path / "accuracy.jsonl"
    bad = edit(set_tool("extract", score=0.5, correct=3))
    f.write_text(json.dumps(REAL) + "\n\n" + json.dumps(bad) + "\n")
    assert rd.main([str(f)]) == 1
    assert "REGRESSION" in capsys.readouterr().out
    assert rd.main([str(f), "--old", "0", "--new", "0"]) == 0
    g = tmp_path / "other.jsonl"
    g.write_text(json.dumps(REAL) + "\n")
    assert rd.main([str(g), str(f)]) == 1
    assert rd.main([str(g)]) == 2     # one line: nothing to compare


def test_a_line_that_is_not_json_is_named(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("{}\nnot json\n")
    with pytest.raises(SystemExit, match="line 2"):
        rd.main([str(f)])
