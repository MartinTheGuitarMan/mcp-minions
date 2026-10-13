import json

import pytest

from minions import backends, ratelimit, tools
from minions.config import Roster
from minions.errors import BadConfig, MinionDown, PrivacyRefused, RateLimited

LABELS = ["positive", "negative"]
KEY = "MINION_TEST_KEY"


def L(label):
    return json.dumps({"label": label})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ratelimit.reset()
    monkeypatch.setenv(KEY, "secret-test-key")
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)


def local(fake, model, **kw):
    return {"backend": "lmstudio", "base_url": fake.url, "model": model, "timeout": 5, **kw}


def cloud(fake, model, **kw):
    return {"backend": "openai-compat", "base_url": fake.url, "model": model, "timeout": 5,
            "cloud": True, "key_env": KEY, **kw}


def roster(models, routes=None):
    return Roster.from_dict({"models": models, "routes": routes or {}})


# ---------- config ----------
@pytest.mark.parametrize("model", [
    {"backend": "openai-compat", "model": "m", "cloud": True, "key_env": KEY},                 # no base_url
    {"backend": "openai-compat", "base_url": "http://x/v1", "model": "m", "cloud": True},      # no key_env
    {"backend": "openai-compat", "base_url": "https://api.example.com/v1", "model": "m"},     # public host, not cloud
    {"backend": "lmstudio", "model": "m", "caps": ["translate"]},
    {"backend": "lmstudio", "model": "m", "limits": {"rpm": 0}},
    {"backend": "lmstudio", "model": "m", "constrain": "magic"},
])
def test_cloud_config_validation(model):
    with pytest.raises(BadConfig):
        Roster.from_dict({"models": {"a": model}})


def test_lan_and_loopback_hosts_count_as_local():
    for url in ("http://127.0.0.1:1/v1", "http://192.168.1.5:1/v1", "http://mac-mini.local:1/v1", "http://mac-mini:1/v1"):
        Roster.from_dict({"models": {"a": {"backend": "openai-compat", "base_url": url, "model": "m"}}})


def test_routing_by_capability_not_by_name(fake):
    r = roster({"a": local(fake, "a", caps=["summarize"]), "b": local(fake, "b", caps=["classify", "extract"])})
    assert r.route("summarize").fast[0].name == "a"
    assert r.route("classify").fast[0].name == "b"
    with pytest.raises(BadConfig):
        roster({"a": local(fake, "a", caps=["summarize"])}, {"classify": {"fast": ["a"]}})


# ---------- privacy gate ----------
def test_sensitive_is_the_default_and_cloud_is_refused_not_preferred(fake):
    r = roster({"c": cloud(fake, "c")})
    with pytest.raises(PrivacyRefused):
        tools.classify(r, "text", LABELS)
    assert fake.requests == []
    fake.by_model["c"] = [L("positive")]
    assert tools.classify(r, "text", LABELS, sensitive=False) == {"label": "positive"}


def test_cloud_never_votes_on_a_sensitive_call(fake):
    r = roster({"l": local(fake, "l"), "c": cloud(fake, "c")}, {"classify": {"fast": ["l", "c"]}})
    fake.by_model.update(l=[L("positive")], c=[L("positive")])
    assert tools.classify(r, "x", LABELS) == {"label": "positive"}
    assert fake.calls("c") == [] and len(fake.calls("l")) == 1
    fake.by_model.update(l=[L("positive")], c=[L("positive")])
    tools.classify(r, "x", LABELS, sensitive=False)
    assert len(fake.calls("c")) == 1


def test_file_tools_stay_local(fake, tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("hello")
    r = roster({"c": cloud(fake, "c")})
    with pytest.raises(PrivacyRefused):
        tools.classify(r, tools.read_text_file(r, str(f)), LABELS)   # what classify_file does


# ---------- keys ----------
def test_key_sent_as_bearer_and_missing_key_is_a_config_error(fake, monkeypatch):
    r = roster({"c": cloud(fake, "c")})
    fake.by_model["c"] = [L("positive")]
    tools.classify(r, "x", LABELS, sensitive=False)
    assert fake.headers[0]["Authorization"] == "Bearer secret-test-key"
    monkeypatch.delenv(KEY)
    with pytest.raises(BadConfig):
        tools.classify(r, "x", LABELS, sensitive=False)


def test_local_models_send_no_authorization(fake):
    fake.by_model["l"] = [L("positive")]
    tools.classify(roster({"l": local(fake, "l")}), "x", LABELS)
    assert "Authorization" not in fake.headers[0]


# ---------- rate limits ----------
def test_429_is_retried_then_reported(fake):
    r = roster({"c": cloud(fake, "c")})
    fake.by_model["c"] = [(429, {"error": "slow down"})] * 3
    with pytest.raises(RateLimited):
        tools.classify(r, "x", LABELS, sensitive=False)
    assert len(fake.calls("c")) == 3   # first try + two retries


def test_429_then_success_needs_no_failover(fake):
    r = roster({"c": cloud(fake, "c")})
    fake.by_model["c"] = [(429, {"error": "x"}), L("negative")]
    assert tools.classify(r, "x", LABELS, sensitive=False) == {"label": "negative"}


def test_rate_limited_minion_fails_over_to_the_fallback(fake):
    models = {"c1": cloud(fake, "c1"), "c2": cloud(fake, "c2")}
    r = Roster.from_dict({"models": models, "routes": {"classify": {"fast": ["c1"], "fallback": ["c2"]}}}, meta=True)
    fake.by_model.update(c1=[(429, {})] * 3, c2=[L("positive")])
    out = tools.classify(r, "x", LABELS, sensitive=False)
    assert out["label"] == "positive" and out["_minion"]["failover"] == [["c1", "c2"]]


def test_failover_never_leaves_the_machine_for_a_sensitive_call(fake):
    models = {"l": local(fake, "l", limits={"rpm": 1}), "c": cloud(fake, "c")}
    r = roster(models, {"classify": {"fast": ["l"], "fallback": ["c"]}})
    fake.by_model.update(l=[L("positive"), L("positive")], c=[L("positive")])
    tools.classify(r, "x", LABELS)
    with pytest.raises(RateLimited):
        tools.classify(r, "x", LABELS)    # local cap used up; the cloud fallback is not allowed to answer
    assert fake.calls("c") == []


def test_local_rpm_cap_refuses_without_sending(fake):
    r = roster({"c": cloud(fake, "c", limits={"rpm": 2})})
    fake.by_model["c"] = [L("positive")] * 3
    tools.classify(r, "x", LABELS, sensitive=False)
    tools.classify(r, "x", LABELS, sensitive=False)
    with pytest.raises(RateLimited):
        tools.classify(r, "x", LABELS, sensitive=False)
    assert len(fake.calls("c")) == 2


def test_down_is_never_failed_over(fake):
    models = {"c1": {**cloud(fake, "c1"), "base_url": "http://127.0.0.1:1/v1"}, "c2": cloud(fake, "c2")}
    r = roster(models, {"classify": {"fast": ["c1"], "fallback": ["c2"]}})
    fake.by_model["c2"] = [L("positive")]
    with pytest.raises(MinionDown):
        tools.classify(r, "x", LABELS, sensitive=False)
    assert fake.calls("c2") == []


# ---------- constrain, status ----------
def test_constrain_modes(fake):
    r = roster({"a": local(fake, "a", constrain="json_object"), "b": local(fake, "b", constrain="none")},
               {"classify": {"fast": ["a"]}, "summarize": {"fast": ["b"]}})
    fake.by_model.update(a=[L("positive")], b=[json.dumps({"summary": "s"})])
    tools.classify(r, "x", LABELS)
    tools.summarize(r, "some text to summarize here")
    assert fake.calls("a")[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in fake.calls("b")[0]


def test_status_reports_but_never_shows_the_key(fake):
    r = roster({"l": local(fake, "l"), "c": cloud(fake, "c", limits={"rpm": 5}, caps=["summarize"])})
    st = tools.status(r)
    assert "secret-test-key" not in json.dumps(st)
    c = next(m for m in st["models"] if m["name"] == "c")
    assert c["cloud"] and c["key_set"] and c["rpm"] == {"limit": 5, "used_last_minute": 0} and c["caps"] == ["summarize"]


def test_status_shows_the_quota_the_provider_reported(fake):
    r = roster({"c": cloud(fake, "c")})
    assert "provider_quota" not in tools.status(r)["models"][0]    # nothing known before the first call
    fake.extra_headers = {"x-ratelimit-limit-req-minute": "188", "X-RateLimit-Remaining-Req-Minute": "187", "x-other": "no"}
    fake.by_model["c"] = [L("positive")]
    tools.classify(r, "x", LABELS, sensitive=False)
    q = tools.status(r)["models"][0]["provider_quota"]
    assert q["as_reported_by_provider"] == {"x-ratelimit-limit-req-minute": "188",
                                            "x-ratelimit-remaining-req-minute": "187"}
    assert "secret-test-key" not in json.dumps(tools.status(r))


# ---------- ask: open questions only for models that passed the hippo test ----------
def ask_reply(text):
    return json.dumps({"answer": text, "confidence": "low"})


def test_ask_needs_a_knowledge_model(fake):
    r = roster({"l": local(fake, "l")})
    assert "ask" not in r.routes
    with pytest.raises(Exception, match="MINION_BAD_INPUT"):
        tools.ask(r, "What is a VA panel?")
    with pytest.raises(BadConfig):                      # an untrusted model cannot be named for ask
        roster({"l": local(fake, "l")}, {"ask": {"fast": ["l"]}})


def test_ask_only_uses_knowledge_models_and_keeps_the_privacy_gate(fake):
    r = roster({"l": local(fake, "l"), "c": cloud(fake, "c", knowledge=True)})
    assert r.route("ask").fast[0].name == "c"          # the model without knowledge: true is never picked
    with pytest.raises(PrivacyRefused):
        tools.ask(r, "Which OSS note app should I use?")
    fake.by_model["c"] = [ask_reply("I cannot confirm that paper exists.")]
    out = tools.ask(r, "Summarize the 2019 paper on the spotted mountain hippo", sensitive=False)
    assert out["confidence"] == "low" and "cannot confirm" in out["answer"]
    sent = json.dumps(fake.calls("c")[0])
    assert "Never invent names" in sent and "<data>" in sent


def test_caps_can_include_ask_but_ask_still_needs_knowledge(fake):
    every = ["ask", "classify", "extract", "summarize"]
    r = roster({"c": cloud(fake, "c", caps=every, knowledge=True)})
    assert r.route("ask").fast[0].name == "c"
    untrusted = roster({"c": cloud(fake, "c", caps=every)})
    assert "ask" not in untrusted.routes and not untrusted.models["c"].serves("ask")


def test_provider_side_schema_rejection_is_a_schema_failure_not_an_outage(fake):
    from minions.errors import SchemaError
    r = roster({"c": cloud(fake, "c")})
    fake.by_model["c"] = [(400, {"error": {"message": "Generated JSON does not match the expected schema.",
                                           "code": "json_validate_failed"}})]
    with pytest.raises(SchemaError):
        tools.classify(r, "x", LABELS, sensitive=False)


# ---------- System One decision models ----------
def jev(label, conf=0.95):
    return (200, {"model": "jev-1.13.0", "answers": {"label": {"type": "choice", "choice": label,
            "probabilities": {label: conf}, "confidence": conf}}, "usage": {"input_tokens": 9, "output_tokens": 1}})


def decision(fake, name="d", **kw):
    return {"backend": "systemone", "base_url": fake.url, "model": name, "kind": "decision", "cloud": True,
            "key_env": KEY, "timeout": 5, **kw}


def test_decision_model_needs_the_systemone_backend_and_only_serves_classify(fake):
    with pytest.raises(BadConfig):
        roster({"d": {**decision(fake), "backend": "lmstudio"}})
    r = roster({"l": local(fake, "l"), "d": decision(fake)}, {"classify": {"fast": ["d"]}})
    assert not r.models["d"].serves("extract")
    with pytest.raises(BadConfig):
        roster({"l": local(fake, "l"), "d": decision(fake)}, {"extract": {"fast": ["d"]}})


def test_decision_voter_sends_state_and_criteria_and_checks_the_label(fake):
    r = roster({"l": local(fake, "l"), "d": decision(fake)}, {"classify": {"fast": ["d"]}})
    fake.by_model["d"] = [jev("positive")]
    assert tools.classify(r, "nice", LABELS, sensitive=False) == {"label": "positive"}
    path, body = fake.requests[0]
    assert path.endswith("/systemone") and body["state"] == "nice"
    assert body["questions"]["label"]["criteria"] == {"positive": "positive", "negative": "negative"}
    assert fake.headers[0]["Authorization"] == "Bearer secret-test-key"
    from minions.errors import SchemaError
    fake.by_model["d"] = [jev("sports")]
    with pytest.raises(SchemaError):
        tools.classify(r, "nice", LABELS, sensitive=False)


def shortcut_roster(fake, threshold=0.9):
    return Roster.from_dict({"models": {"l": local(fake, "l"), "d": decision(fake)},
        "routes": {"classify": {"fast": ["l"], "shortcut": {"model": "d", "min_confidence": threshold}}}}, meta=True)


def test_confident_shortcut_answers_alone_and_unsure_escalates(fake):
    r = shortcut_roster(fake)
    fake.by_model.update(d=[jev("negative", 0.97)])
    out = tools.classify(r, "bad", LABELS, sensitive=False)
    assert out["label"] == "negative" and out["_minion"]["shortcut"]["escalated"] is False
    assert fake.calls("l") == []
    fake.by_model.update(d=[jev("negative", 0.55)], l=[L("positive")])
    out = tools.classify(r, "meh", LABELS, sensitive=False)
    assert out["label"] == "positive" and out["_minion"]["shortcut"]["escalated"] is True


def test_cloud_shortcut_is_skipped_for_sensitive_calls(fake):
    r = shortcut_roster(fake)
    fake.by_model.update(l=[L("positive")])
    assert tools.classify(r, "private text", LABELS)["label"] == "positive"
    assert fake.calls("d") == []


def test_shortcut_that_is_rate_limited_falls_through_to_the_voters(fake):
    r = shortcut_roster(fake)
    fake.by_model.update(d=[(429, {})] * 3, l=[L("negative")])
    assert tools.classify(r, "x", LABELS, sensitive=False)["label"] == "negative"


def test_status_counts_tokens_the_provider_reports(fake):
    r = roster({"l": local(fake, "l"), "d": decision(fake)}, {"classify": {"fast": ["d"]}})
    fake.by_model["d"] = [jev("positive"), jev("negative")]
    tools.classify(r, "a", LABELS, sensitive=False)
    tools.classify(r, "b", LABELS, sensitive=False)
    row = next(m for m in tools.status(r)["models"] if m["name"] == "d")
    assert row["tokens_used_by_this_server"] == {"tokens_in": 18, "tokens_out": 2, "calls": 2}
