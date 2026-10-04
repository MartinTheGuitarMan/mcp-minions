import json

import pytest

from minions import reasoning, tools
from minions.config import Roster
from minions.errors import BadConfig, MinionDown, NoQuorum, SchemaError

LABELS = ["positive", "negative", "neutral"]


def L(label):
    return json.dumps({"label": label})


def roster(fake, *, fast=("f1", "f2", "f3"), judge="j", quorum=None, meta=False):
    models = {n: {"backend": "lmstudio", "base_url": fake.url, "model": n, "timeout": 5} for n in ("f1", "f2", "f3")}
    models["j"] = {"backend": "lmstudio", "base_url": fake.url, "model": "j", "kind": "reasoning", "timeout": 5}
    route = {"fast": list(fast)}
    if judge:
        route["judge"] = judge
    if quorum:
        route["quorum"] = quorum
    return Roster.from_dict({"models": models, "routes": {"classify": route, "extract": {"fast": ["f1"], "judge": judge} if judge else {"fast": ["f1"]}}}, meta=meta)


# ---------- roster parsing ----------
def test_roster_defaults_and_inline_env(fake):
    raw = {"models": {"a": {"backend": "ollama", "model": "m"}}}
    r = Roster.from_env({"MINION_ROSTER": json.dumps(raw)})
    assert r.route("classify").fast[0].base_url.endswith(":11434/v1") and r.route("classify").quorum == 1


def test_roster_from_file(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"models": {"a": {"backend": "lmstudio", "model": "m"}}}))
    assert Roster.from_env({"MINION_ROSTER": str(p)}).models["a"].model == "m"


@pytest.mark.parametrize("raw", [
    {},
    {"models": {"a": {"backend": "nope", "model": "m"}}},
    {"models": {"a": {"backend": "ollama", "model": "m", "kind": "weird"}}},
    {"models": {"a": {"backend": "ollama", "model": "m"}}, "routes": {"classify": {"fast": ["zzz"]}}},
    {"models": {"a": {"backend": "ollama", "model": "m", "kind": "reasoning"}}},  # no fast model at all
    {"models": {"a": {"backend": "ollama", "model": "m"}}, "routes": {"classify": {"judge": "a"}}},  # judge not reasoning
    {"models": {"a": {"backend": "ollama", "model": "m"}, "b": {"backend": "ollama", "model": "n"}},
     "routes": {"extract": {"fast": ["a", "b"]}}},  # quorum only for classify
    {"models": {"a": {"backend": "ollama", "model": "m"}}, "routes": {"classify": {"fast": ["a"], "quorum": 3}}},
])
def test_roster_validation(raw):
    with pytest.raises(BadConfig):
        Roster.from_dict(raw)


# ---------- quorum and judge ----------
def test_quorum_reached_judge_not_called(fake):
    fake.by_model.update(f1=[L("positive")], f2=[L("positive")], f3=[L("negative")])
    assert tools.classify(roster(fake), "great", LABELS) == {"label": "positive"}
    assert fake.calls("j") == []


def test_disagreement_calls_judge_which_verifies_candidates(fake):
    fake.by_model.update(f1=[L("positive")], f2=[L("negative")], f3=[L("neutral")],
                         j=['<think>hmm</think>\n{"verdict": "confirm", "label": "negative"}'])
    assert tools.classify(roster(fake), "the battery died", LABELS) == {"label": "negative"}
    (jcall,) = fake.calls("j")
    prompt = jcall["messages"][1]["content"]
    assert "'positive' (1 vote)" in prompt and "'negative' (1 vote)" in prompt
    assert "verdict" in prompt and "response_format" not in jcall      # judge is unconstrained


def test_judge_can_correct_to_another_label(fake):
    fake.by_model.update(f1=[L("positive")], f2=[L("negative")], f3=[L("positive")], j=['{"verdict":"correct","label":"neutral"}'])
    r = roster(fake, quorum=3)
    assert tools.classify(r, "x", LABELS) == {"label": "neutral"}


def test_judge_confirming_a_non_candidate_is_rejected_then_retried(fake):
    fake.by_model.update(f1=[L("positive")], f2=[L("negative")], f3=[L("positive")],
                         j=['{"verdict":"confirm","label":"neutral"}', '{"verdict":"correct","label":"neutral"}'])
    assert tools.classify(roster(fake, quorum=3), "x", LABELS) == {"label": "neutral"}
    assert len(fake.calls("j")) == 2


def test_disagreement_without_judge_raises_no_quorum(fake):
    fake.by_model.update(f1=[L("positive")], f2=[L("negative")])
    with pytest.raises(NoQuorum, match="MINION_NO_QUORUM"):
        tools.classify(roster(fake, fast=("f1", "f2"), judge=None), "x", LABELS)


def test_schema_failure_of_fast_model_triggers_judge_repair(fake):
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}
    fake.by_model.update(f1=['{"age": "forty"}'], j=['```json\n{"age": 40}\n```'])
    assert tools.extract(roster(fake), "He is forty", schema) == {"age": 40}
    repair_prompt = fake.calls("j")[0]["messages"][1]["content"]
    assert '"forty"' in repair_prompt and "failed validation" in repair_prompt


def test_valid_fast_answer_never_reaches_judge(fake):
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}
    fake.by_model.update(f1=['{"age": 40}'])
    assert tools.extract(roster(fake), "x", schema) == {"age": 40}
    assert fake.calls("j") == []


def test_all_fast_fail_without_judge_reports_schema_error_not_quorum(fake):
    fake.by_model.update(f1=["nope"], f2=["nope"])
    with pytest.raises(SchemaError):
        tools.classify(roster(fake, fast=("f1", "f2"), judge=None), "x", LABELS)


def test_down_fast_model_propagates_and_judge_is_not_used(fake):
    models = {"f1": {"backend": "lmstudio", "base_url": fake.url, "model": "f1"},
              "dead": {"backend": "lmstudio", "base_url": "http://127.0.0.1:9/v1", "model": "d", "timeout": 2},
              "j": {"backend": "lmstudio", "base_url": fake.url, "model": "j", "kind": "reasoning"}}
    r = Roster.from_dict({"models": models, "routes": {"classify": {"fast": ["f1", "dead"], "judge": "j"}}})
    fake.by_model.update(f1=[L("positive")])
    with pytest.raises(MinionDown):
        tools.classify(r, "x", LABELS)
    assert fake.calls("j") == []


def test_meta_flag_adds_minion_key(fake):
    fake.by_model.update(f1=[L("positive")], f2=[L("positive")], f3=[L("positive")])
    out = tools.classify(roster(fake, meta=True), "x", LABELS)
    assert out["label"] == "positive" and out["_minion"]["votes"] == {"positive": 3}


# ---------- reasoning path ----------
def test_strip_think_and_extract_json():
    assert reasoning.strip_think('<think>a {"x":1} b</think>\n{"y": 2}') == '{"y": 2}'
    assert reasoning.strip_think('hidden thoughts</think>{"y": 2}') == '{"y": 2}'
    with pytest.raises(reasoning.Invalid):
        reasoning.strip_think("<think>never finishes")
    assert reasoning.extract_json('x {"a": 1} then ```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    with pytest.raises(reasoning.Invalid):
        reasoning.extract_json("no json here")


def test_reasoning_retries_once_then_succeeds(fake):
    fake.by_model.update(j=["I think it is fine", '{"verdict":"correct","label":"neutral"}'])
    r = roster(fake)
    from minions import judge
    out, _ = judge.verify_classification(r.models["j"], "t", "x", LABELS, {})
    assert out["label"] == "neutral"
    second = fake.calls("j")[1]["messages"]
    assert second[-1]["role"] == "user" and "rejected" in second[-1]["content"]


def test_reasoning_fails_after_one_retry(fake):
    fake.by_model.update(j=["nothing", "still nothing", "never reached"])
    from minions import judge
    with pytest.raises(SchemaError, match="after one retry"):
        judge.verify_classification(roster(fake).models["j"], "t", "x", LABELS, {})
    assert len(fake.calls("j")) == 2


def test_reasoning_truncated_think_block_is_retried(fake):
    fake.by_model.update(j=["<think>cut off mid-thought", '{"verdict":"correct","label":"negative"}'])
    from minions import judge
    out, _ = judge.verify_classification(roster(fake).models["j"], "t", "x", LABELS, {})
    assert out["label"] == "negative"


# ---------- concurrency ----------
def test_models_on_one_server_run_sequentially(fake):
    fake.delay = 0.15
    fake.by_model.update(f1=[L("positive")], f2=[L("positive")], f3=[L("positive")])
    tools.classify(roster(fake), "x", LABELS)
    assert fake.max_inflight == 1


def test_models_on_different_servers_run_in_parallel():
    from fake_backend import FakeBackend
    import time
    a, b = FakeBackend(), FakeBackend()
    try:
        a.delay = b.delay = 0.4
        a.by_model["m1"], b.by_model["m2"] = [L("positive")], [L("positive")]
        models = {"m1": {"backend": "lmstudio", "base_url": a.url, "model": "m1"},
                  "m2": {"backend": "ollama", "base_url": b.url, "model": "m2"}}
        r = Roster.from_dict({"models": models, "routes": {"classify": {"fast": ["m1", "m2"], "quorum": 2}}})
        t = time.time()
        assert tools.classify(r, "x", LABELS) == {"label": "positive"}
        assert time.time() - t < 0.75      # two 0.4s calls overlapped, not 0.8s back to back
    finally:
        a.close(); b.close()


# ---------- truncation ----------
def test_truncated_thinking_is_reported_and_retried_with_bigger_budget(fake):
    from minions import judge
    fake.by_model.update(j=[{"content": "", "finish_reason": "length"}, '{"verdict":"correct","label":"neutral"}'])
    out, _ = judge.verify_classification(roster(fake).models["j"], "t", "x", LABELS, {})
    assert out["label"] == "neutral"
    first, second = fake.calls("j")
    assert second["max_tokens"] == 2 * first["max_tokens"]
    assert "token budget ran out" in second["messages"][-1]["content"]
    assert "very short" in second["messages"][-1]["content"]


def test_truncation_twice_fails_with_a_clear_reason(fake):
    from minions import judge
    fake.by_model.update(j=[{"content": "", "finish_reason": "length"}] * 2)
    with pytest.raises(SchemaError, match="token budget ran out"):
        judge.verify_classification(roster(fake).models["j"], "t", "x", LABELS, {})


# ---------- MCP layer ----------
def test_server_tools_surface_minion_codes_as_tool_errors(monkeypatch, tmp_path):
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError
    import minions.server as server
    monkeypatch.setenv("MINION_MODEL", "m")
    monkeypatch.setenv("MINION_BASE_URL", "http://127.0.0.1:9/v1")   # nothing listens here
    monkeypatch.setenv("MINION_TIMEOUT", "2")
    with pytest.raises(ToolError, match="MINION_DOWN"):
        asyncio.run(server.mcp.call_tool("classify", {"text": "x", "labels": ["a", "b"]}))
    with pytest.raises(ToolError, match="MINION_BAD_INPUT"):
        asyncio.run(server.mcp.call_tool("classify", {"text": "x", "labels": []}))
    with pytest.raises(ToolError, match="MINION_BAD_INPUT"):
        asyncio.run(server.mcp.call_tool("summarize_file", {"path": str(tmp_path / "nope.txt")}))
