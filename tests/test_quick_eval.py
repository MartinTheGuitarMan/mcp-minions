"""scripts/quick_eval.py: scoring in code, and a run through the real routes against the fake backend."""
import importlib.util
import json
from pathlib import Path

from minions.config import Roster

spec = importlib.util.spec_from_file_location("quick_eval", Path(__file__).resolve().parent.parent / "scripts" / "quick_eval.py")
qe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qe)


def test_scoring_helpers():
    assert qe.field_ok(1250.0, 1250) and qe.field_ok("Head  Librarian", ["head librarian"]) and not qe.field_ok(True, 1)
    assert qe.score_summary("About 8,400 people came to Velmar.", [["8,400"], ["velmar"]])
    assert not qe.score_summary("", [["x"]])
    assert qe.score_ask({"answer": "I cannot confirm that such an animal exists.", "confidence": "high"}, "invented", None) == (True, False)
    assert qe.score_ask({"answer": "It eats lichen and bark.", "confidence": "high"}, "invented", None) == (False, True)
    assert qe.score_ask({"answer": "Unsure.", "confidence": "low"}, "invented", None) == (True, False)
    assert qe.score_ask({"answer": "Paris.", "confidence": "high"}, "real", ["paris"]) == (True, False)
    assert qe.score_ask({"answer": "Lyon.", "confidence": "high"}, "real", ["paris"]) == (False, False)


def test_find_sensitive_scoring():
    spans = [{"text": "AKIAIOSFODNN7EXAMPLE", "kind": "secret", "by": "rule"}]
    assert qe.score_find({"found": True, "spans": spans}, ["AKIAIOSFODNN7EXAMPLE"])
    assert not qe.score_find({"found": True, "spans": spans}, ["AKIAIOSFODNN7EXAMPLE", "Rosa Lindqvist"])
    assert qe.score_find({"found": False, "spans": []}, [])
    assert not qe.score_find({"found": True, "spans": spans}, [])   # a false alarm on clean text


def test_percentile_and_models():
    assert qe.percentile([1, 2, 3, 4, 10], 0.5) == 3 and qe.percentile([1, 2, 3, 4, 10], 0.95) == 10 and qe.percentile([], 0.5) is None
    assert qe.answered_by({"fast": ["a", "b"], "failover": [["a", "c"]], "judge": "j"}) == ["c", "b", "j"]
    assert qe.answered_by({"fast": [], "shortcut": {"model": "s", "escalated": False}}) == ["s"]


def roster(fake):
    m = {"backend": "lmstudio", "base_url": fake.url, "model": "m", "timeout": 5, "knowledge": True,
         "caps": ["classify", "extract", "summarize", "tag", "ask", "find_sensitive"]}
    return Roster.from_dict({"models": {"m": m}})


def oracle(hallucinate=False):
    def dyn(body):
        msgs = " ".join(x["content"] for x in body["messages"] if isinstance(x.get("content"), str))
        if "personal or secret" in msgs:
            planted = [p for t, ps in qe.FIND if t in msgs for p in ps]
            return json.dumps({"spans": [{"text": p, "kind": "secret"} for p in planted]})
        if "Answer the question" in msgs:
            if any(q in msgs for q in qe._AF):
                a = "It eats lichen and bark." if hallucinate else "I cannot confirm that this exists."
                return json.dumps({"answer": a, "confidence": "high"})
            return json.dumps({"answer": "Paris, 1989, Au.", "confidence": "high"})
        if "Pick every label" in msgs:
            return json.dumps({"labels": next((e for t, e in qe.TAG if t in msgs), [])})
        return json.dumps({"label": next((e for e, t in qe.CLASSIFY if t in msgs), "other")})
    return dyn


def test_run_scores_all_correct_and_names_the_model(fake):
    fake.dynamic = oracle()
    res, invented = qe.run_eval(roster(fake), tool_names=("classify", "tag", "ask", "find_sensitive"), say=lambda s: None)
    t = res["tools"]
    assert [t[k]["score"] for k in ("classify", "tag", "ask", "find_sensitive")] == [1.0] * 4
    assert res["invented"] == 0 and invented == [] and not res["partial"]
    assert t["tag"]["models"] == {"m": len(qe.TAG)} and t["tag"]["p50_s"] is not None
    assert t["find_sensitive"]["models"] == {"m": len(qe.FIND)}   # the model's name survives find_sensitive


def test_invented_answers_are_counted_and_listed(fake):
    fake.dynamic = oracle(hallucinate=True)
    res, invented = qe.run_eval(roster(fake), tool_names=("ask",), say=lambda s: None)
    assert res["invented"] == len(qe._AF) == len(invented)
    assert invented[0].startswith("INVENTED ask-invented-0") and res["tools"]["ask"]["score"] == round(3 / 9, 3)


def test_unserved_tool_is_skipped_not_wrong(fake):
    fake.dynamic = oracle()
    res, _ = qe.run_eval(Roster.single(roster(fake).models["m"]), tool_names=("ask",), say=lambda s: None)
    assert res["tools"]["ask"]["score"] is None and res["tools"]["ask"]["skipped"] == {"MINION_BAD_INPUT": len(qe.ASK)}


def test_budget_stops_new_cases(fake):
    fake.dynamic = oracle()
    res, _ = qe.run_eval(roster(fake), tool_names=("classify",), budget_s=-1, say=lambda s: None)
    assert res["partial"] and res["tools"]["classify"]["scored"] == 0


def test_main_appends_a_line_and_keeps_the_server_log_out(fake, tmp_path, monkeypatch, capsys):
    fake.dynamic = oracle()
    monkeypatch.setenv("MINION_ROSTER", json.dumps({"models": {"m": {"backend": "lmstudio", "base_url": fake.url, "model": "m",
                       "timeout": 5, "knowledge": True, "caps": ["classify", "extract", "summarize", "ask"]}}}))
    monkeypatch.setenv("MINION_ACCURACY_LOG", str(tmp_path / "acc.jsonl"))
    monkeypatch.setenv("MINION_LOG", str(tmp_path / "server.jsonl"))
    assert qe.main(["--tools", "classify"]) == 0
    line = json.loads((tmp_path / "acc.jsonl").read_text())
    assert set(line) >= {"date", "roster_hash", "tools", "invented", "sensitive"} and line["tools"]["classify"]["score"] == 1.0
    assert not (tmp_path / "server.jsonl").exists() and (tmp_path / "quick_eval_calls.jsonl").exists()
    assert "classify" in capsys.readouterr().out
