import json

import pytest

from minions import backends, ratelimit, tools
from minions.config import Config, Roster
from minions.errors import BadInput

LABELS = ["travel", "cooking", "garden"]
KEY = "MINION_TEST_KEY"


def T(*labels):
    return json.dumps({"labels": list(labels)})


def C(label):
    return json.dumps({"label": label})


def items(*texts):
    return [{"id": f"n{i}", "text": t} for i, t in enumerate(texts)]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ratelimit.reset()
    monkeypatch.setenv(KEY, "secret-test-key")
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)


def test_tag_batch_one_result_per_id_in_order(fake, cfg):
    fake.replies += [T("travel"), T(), T("cooking", "garden")]
    out = tools.tag_batch(cfg, items("Train to the coast.", "Nothing much.", "Tomatoes from the plot, made soup."), LABELS)
    assert out == {"ok": 3, "failed": 0, "results": [{"id": "n0", "labels": ["travel"]}, {"id": "n1", "labels": []},
                                                     {"id": "n2", "labels": ["cooking", "garden"]}]}


def test_partial_failure_does_not_fail_the_batch(fake, cfg):
    fake.replies += [T("travel"), T("not-a-label"), C("cooking")]
    out = tools.tag_batch(cfg, items("Ferry trip.", "Bad output.", "", "ok"), LABELS)
    codes = [r.get("error", {}).get("code") for r in out["results"]]
    assert codes == [None, "MINION_SCHEMA", "MINION_BAD_INPUT", "MINION_SCHEMA"]   # last: a label, not labels
    assert out["ok"] == 1 and out["failed"] == 3 and out["results"][0]["labels"] == ["travel"]
    assert len(fake.requests) == 3   # the empty text never reached the model


def test_classify_batch(fake, cfg):
    fake.replies += [C("garden"), C("nope")]
    out = tools.classify_batch(cfg, items("Pruned the apple tree.", "Something else."), LABELS)
    assert out["results"][0] == {"id": "n0", "label": "garden"}
    assert out["results"][1]["error"]["code"] == "MINION_SCHEMA" and out["ok"] == 1


def test_backend_down_stops_the_batch():
    dead = Config(backend="lmstudio", base_url="http://127.0.0.1:9/v1", model="m", timeout=2)
    out = tools.classify_batch(dead, items("a", "b", "c"), LABELS)
    errs = [r["error"] for r in out["results"]]
    assert [e["code"] for e in errs] == ["MINION_DOWN"] * 3 and out["failed"] == 3
    assert errs[1]["message"].startswith("not attempted: MINION_DOWN")


def test_rate_limit_marks_the_rest_not_attempted_and_sends_nothing_more(fake):
    r = Roster.from_dict({"models": {"l": {"backend": "lmstudio", "base_url": fake.url, "model": "l", "timeout": 5,
                                           "limits": {"rpm": 1}}}})
    fake.replies += [T("travel")] * 3
    out = tools.tag_batch(r, items("a", "b", "c"), LABELS)
    assert out["results"][0]["labels"] == ["travel"]
    assert out["results"][1]["error"]["code"] == "MINION_RATE_LIMITED" and "retry_after" in out["results"][1]["error"]
    assert out["results"][2]["error"]["message"].startswith("not attempted")
    assert len(fake.requests) == 1


def test_sensitive_batch_on_a_cloud_only_roster_is_refused_per_item(fake):
    r = Roster.from_dict({"models": {"c": {"backend": "openai-compat", "base_url": fake.url, "model": "c",
                                           "cloud": True, "key_env": KEY}}})
    out = tools.tag_batch(r, items("a", "b"), LABELS)
    assert [x["error"]["code"] for x in out["results"]] == ["MINION_PRIVACY"] * 2 and fake.requests == []


def test_batch_input_checks(cfg):
    bad = [[], [{"id": "a"}], [{"id": "", "text": "x"}], [{"id": 1, "text": "x"}], items("x") * 2,
           [{"id": f"n{i}", "text": "x"} for i in range(tools.MAX_BATCH + 1)]]
    for b in bad:
        with pytest.raises(BadInput):
            tools.tag_batch(cfg, b, LABELS)
    with pytest.raises(BadInput):   # shared arguments are checked once, before any item
        tools.tag_batch(cfg, items("x"), LABELS, max_labels=9)
    with pytest.raises(BadInput):
        tools.classify_batch(cfg, items("x"), ["a", "a"])


def test_tag_batch_needs_a_tag_route(fake):
    r = Roster.from_dict({"models": {"m": {"backend": "lmstudio", "base_url": fake.url, "model": "m",
                                           "caps": ["classify", "extract", "summarize"]}}})
    with pytest.raises(BadInput, match="serves 'tag'"):
        tools.tag_batch(r, items("x"), LABELS)


# ---------- chunking ----------
def para(word, n):
    return " ".join([word] * n)


def test_chunks_split_at_paragraphs_and_cut_long_ones():
    assert tools.chunks("short") == (["short"], False)
    text = "\n\n".join([para("alpha", 1000), para("beta", 1000), para("gamma", 2000)])   # 6k, 5k, 12k chars
    pieces, truncated = tools.chunks(text)
    assert all(len(p) <= tools.CHUNK_CHARS for p in pieces) and not truncated
    assert pieces[0].startswith("alpha") and pieces[1].startswith("beta") and len(pieces) == 4
    many = "\n\n".join(para("w", 3000) for _ in range(6))
    pieces, truncated = tools.chunks(many)
    assert len(pieces) == tools.MAX_CHUNKS and truncated


def by_chunk(answers):
    """Reply by the word a chunk is made of."""
    def reply(body):
        user = body["messages"][1]["content"]
        return next(v for k, v in answers.items() if k in user)
    return reply


def test_long_text_classify_takes_the_chunks_majority_and_a_tie_is_no_quorum(fake, cfg):
    long = "\n\n".join([para("alpha", 1000), para("beta", 1000), para("gamma", 1000)])
    fake.dynamic = by_chunk({"alpha": C("travel"), "beta": C("garden"), "gamma": C("travel")})
    out = tools.classify_batch(cfg, [{"id": "a", "text": long}], LABELS)
    assert out["results"][0] == {"id": "a", "label": "travel", "chunks": 3}
    fake.dynamic = by_chunk({"alpha": C("travel"), "beta": C("garden")})
    tie = "\n\n".join([para("alpha", 1000), para("beta", 1000)])
    assert tools.classify_batch(cfg, [{"id": "a", "text": tie}], LABELS)["results"][0]["error"]["code"] == "MINION_NO_QUORUM"


def test_long_text_tags_are_ranked_across_chunks(fake, cfg):
    long = "\n\n".join([para("alpha", 1000), para("beta", 1000), para("gamma", 1000)])
    fake.dynamic = by_chunk({"alpha": T("travel"), "beta": T("garden", "cooking"), "gamma": T("cooking")})
    out = tools.tag_batch(cfg, [{"id": "a", "text": long}], LABELS, max_labels=2)
    assert out["results"][0] == {"id": "a", "labels": ["cooking", "travel"], "chunks": 3}
