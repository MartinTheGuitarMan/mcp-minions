"""scripts/eval.py --confidence: confidence-versus-correct pairs for tuning a shortcut threshold."""
import importlib.util
import json
from pathlib import Path

from minions.config import Roster
from test_cloud import KEY, decision, local

spec = importlib.util.spec_from_file_location("eval_script", Path(__file__).resolve().parent.parent / "scripts" / "eval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

ITEMS = [{"labels": ["positive", "negative"], "text": f"item {i}", "expected": e}
         for i, e in enumerate(["positive", "negative", "positive", "negative"])]


def test_pairs_and_threshold_table(fake, monkeypatch, capsys):
    monkeypatch.setenv(KEY, "secret-test-key")
    answers = [("positive", 0.99), ("positive", 0.96), ("positive", 0.7), ("negative", 0.9)]   # second one is wrong
    fake.by_model["d"] = [(200, {"answers": {"label": {"type": "choice", "choice": l, "confidence": c}}})
                          for l, c in answers]
    r = Roster.from_dict({"models": {"l": local(fake, "l"), "d": decision(fake)}})
    pairs = ev.confidence_pairs(r, "d", ITEMS, None)
    assert [(p["confidence"], p["correct"]) for p in pairs] == [(0.99, True), (0.96, False), (0.7, True), (0.9, True)]
    assert "Classify the data into exactly one of: positive, negative." in json.dumps(fake.calls("d")[0])
    ev.print_confidence("d", pairs)
    out = capsys.readouterr().out
    assert "0.9900\t1\tpositive\tpositive\tc0" in out
    assert "| 0.95 | 2 | 1 | 0.500 | 2 |" in out and "| 0.9 | 3 | 2 | 0.667 | 1 |" in out
