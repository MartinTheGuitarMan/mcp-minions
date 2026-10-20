"""Token accounting in the call log and the report. Everything runs against the fake backend."""
import json
import time

import pytest

from minions import calllog, tools
from minions.errors import PrivacyRefused
from test_cloud import KEY, LABELS, L, cloud, jev, local, roster
import test_cloud


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv(KEY, "secret-test-key")
    monkeypatch.setenv("MINION_LOG", str(tmp_path / "calls.jsonl"))
    return tmp_path / "calls.jsonl"


def reply(label, tin, tout):
    return (200, {"choices": [{"message": {"content": L(label)}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": tin, "completion_tokens": tout}})


def lines(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_tokens_are_logged_summed_over_all_model_calls_and_never_the_text(fake, _env):
    r = roster({"a": local(fake, "a"), "b": local(fake, "b")}, {"classify": {"fast": ["a", "b"]}})
    fake.by_model.update(a=[reply("positive", 100, 5)], b=[reply("positive", 110, 6)])
    tools.classify(r, "PRIVATE-WORDS here", LABELS)
    (row,) = lines(_env)
    assert row["prompt_tokens"] == 210 and row["completion_tokens"] == 11
    assert "cloud" not in row and "tokens_partial" not in row and "PRIVATE-WORDS" not in _env.read_text()


def test_quorum_with_judge_counts_the_judge_tokens_too(fake, _env):
    r = roster({"a": local(fake, "a"), "b": local(fake, "b"), "j": local(fake, "j", kind="reasoning")},
               {"classify": {"fast": ["a", "b"], "judge": "j"}})
    fake.by_model.update(a=[reply("positive", 10, 2)], b=[reply("negative", 10, 2)],
                         j=[(200, {"choices": [{"message": {"content": json.dumps({"verdict": "confirm", "label": "positive"})},
                                                "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 40}})])
    tools.classify(r, "x", LABELS)
    (row,) = lines(_env)
    assert row["judge"] == "j" and row["prompt_tokens"] == 70 and row["completion_tokens"] == 44


def test_missing_usage_logs_null_and_partial_is_flagged(fake, _env):
    r = roster({"a": local(fake, "a")}, {"classify": {"fast": ["a"]}})
    fake.by_model["a"] = [L("positive")]            # fake replies without a usage object
    tools.classify(r, "x", LABELS)
    row = lines(_env)[0]
    assert "prompt_tokens" not in row and "completion_tokens" not in row and "tokens_partial" in row
    r2 = roster({"a": local(fake, "a"), "b": local(fake, "b")}, {"classify": {"fast": ["a", "b"]}})
    fake.by_model.update(a=[reply("positive", 10, 2)], b=[L("positive")])
    tools.classify(r2, "x", LABELS)
    row = lines(_env)[1]
    assert row["prompt_tokens"] == 10 and row["tokens_partial"] is True


def test_cloud_calls_are_flagged_and_split_out(fake, _env):
    r = roster({"c": cloud(fake, "c")}, {"classify": {"fast": ["c"]}})
    fake.by_model["c"] = [reply("positive", 30, 3)]
    tools.classify(r, "x", LABELS, sensitive=False)
    (row,) = lines(_env)
    assert row["cloud"] is True and row["cloud_prompt_tokens"] == 30 and row["cloud_completion_tokens"] == 3


def test_decision_model_usage_is_counted(fake, _env):
    r = test_cloud.shortcut_roster(fake)
    fake.by_model.update(d=[jev("negative", 0.99)])
    tools.classify(r, "bad", LABELS, sensitive=False)
    (row,) = lines(_env)
    assert row["prompt_tokens"] == 9 and row["completion_tokens"] == 1 and row["cloud"] is True


def test_errors_are_logged_with_the_tokens_spent(fake, _env):
    r = roster({"a": local(fake, "a")}, {"classify": {"fast": ["a"]}})
    fake.by_model["a"] = [(200, {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 12, "completion_tokens": 4}})]
    with pytest.raises(Exception):
        tools.classify(r, "x", LABELS)
    row = lines(_env)[0]
    assert row["error"] and row["prompt_tokens"] == 12


def write_log(path, rows):
    now = time.time()
    path.write_text("\n".join(json.dumps({"ts": round(now), "tool": "classify", "sensitive": True, "latency_s": 1.0, **r})
                              for r in rows) + "\n")


def test_report_totals_on_a_synthetic_log(_env):
    write_log(_env, [
        {"chars": 400, "prompt_tokens": 150, "completion_tokens": 8},                       # local ok: 100 in + 8 out
        {"chars": 1000, "prompt_tokens": 300, "completion_tokens": 20},                     # local ok: 250 + 20
        {"chars": 800, "prompt_tokens": 90, "completion_tokens": 6, "cloud": True,
         "cloud_prompt_tokens": 90, "cloud_completion_tokens": 6},                          # cloud: zero
        {"chars": 800, "prompt_tokens": 40, "completion_tokens": 3, "error": "MINION_SCHEMA"},   # error: zero
        {"chars": 800, "prompt_tokens": 40, "completion_tokens": 3, "failover": [["a", "b"]]},   # failover: zero
    ])
    t = calllog.report(1)["tokens"]
    assert t["handled_locally"] == {"prompt": 150 + 300 + 40 + 40, "completion": 8 + 20 + 3 + 3}
    assert t["handled_by_cloud"] == {"prompt": 90, "completion": 6}
    k = t["estimated_kept_off_calling_model"]
    assert (k["input"], k["output"], k["total"], k["calls_counted"]) == (350, 28, 378, 2)
    assert k["estimate"] is True and "not a billing figure" in k["method"] and "4 characters per token" in k["method"]


def test_old_log_lines_still_work(_env):
    write_log(_env, [{"chars": 400}, {"chars": 120, "error": "MINION_DOWN"}])   # no token fields at all
    rep = calllog.report(1)
    assert rep["calls"] == 2 and rep["tokens"]["calls_without_token_counts"] == 2
    assert rep["tokens"]["handled_locally"] == {"prompt": 0, "completion": 0}
    k = rep["tokens"]["estimated_kept_off_calling_model"]
    assert k["input"] == 100 and k["output"] == 0 and k["calls_counted"] == 1     # text length alone still gives the input side


@pytest.mark.parametrize("row", [{"error": "MINION_DOWN"}, {"cloud": True}, {"failover": [["a", "b"]]}])
def test_zero_saving_cases(_env, row):
    write_log(_env, [{"chars": 4000, "prompt_tokens": 1000, "completion_tokens": 50, **row}])
    k = calllog.report(1)["tokens"]["estimated_kept_off_calling_model"]
    assert k["total"] == 0 and k["calls_counted"] == 0


def test_refused_call_saves_nothing_and_logs_no_tokens(fake, _env):
    with pytest.raises(PrivacyRefused):
        tools.classify(roster({"c": cloud(fake, "c")}), "x", LABELS)
    assert "prompt_tokens" not in lines(_env)[0]
    assert calllog.report(1)["tokens"]["estimated_kept_off_calling_model"]["total"] == 0
