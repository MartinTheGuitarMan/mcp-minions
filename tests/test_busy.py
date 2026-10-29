"""A model refused by the quiet gate (MINION_BUSY) is skipped like a rate-limited one; load failures are not."""
import json

import pytest

from minions import engine, tools
from minions.config import Roster
from minions.errors import Busy, LoadFailed, MinionDown, NoQuorum, RateLimited

LABELS = ["positive", "negative"]


def L(label):
    return json.dumps({"label": label})


def build(fake, routes, **kinds):
    models = {n: {"backend": "lmstudio", "base_url": fake.url, "model": n, "timeout": 5, **kinds.get(n, {})}
              for n in ("big", "small", "other")}
    return Roster.from_dict({"models": models, "routes": routes}, meta=True)


def refuse(monkeypatch, errors):
    """The named models raise the given error before any request is sent, as the gate does."""
    real = engine.run

    def run(cfg, *a, **kw):
        if cfg.name in errors:
            raise errors[cfg.name]
        return real(cfg, *a, **kw)
    monkeypatch.setattr(engine, "run", run)


ROUTE = {"classify": {"fast": ["big"], "fallback": ["small"]}}


def test_busy_model_falls_back_and_is_logged(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("on battery")})
    fake.by_model["small"] = [L("positive")]
    out = tools.classify(build(fake, ROUTE), "x", LABELS)
    assert out["label"] == "positive"
    assert out["_minion"]["busy"] == ["big"] and out["_minion"]["failover"] == [["big", "small"]]
    assert fake.calls("big") == []


def test_every_model_busy_raises_the_last_error(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("on battery"), "small": Busy("free memory 30 percent")})
    with pytest.raises(Busy, match="free memory"):
        tools.classify(build(fake, ROUTE), "x", LABELS)


def test_busy_and_rate_limited_mix_raises_the_last(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("on battery"), "small": RateLimited("cap")})
    with pytest.raises(RateLimited):
        tools.classify(build(fake, ROUTE), "x", LABELS)


def test_busy_does_not_reach_the_judge_or_a_canned_answer(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("hold marker")})
    r = build(fake, {"classify": {"fast": ["big"], "judge": "other"}}, other={"kind": "reasoning"})
    with pytest.raises(Busy):
        tools.classify(r, "x", LABELS)
    assert fake.calls("other") == []


@pytest.mark.parametrize("err", [LoadFailed("lms load failed"), MinionDown("down")])
def test_load_failure_and_down_still_propagate(fake, monkeypatch, err):
    refuse(monkeypatch, {"big": err})
    fake.by_model["small"] = [L("positive")]
    with pytest.raises(type(err)):
        tools.classify(build(fake, ROUTE), "x", LABELS)
    assert fake.calls("small") == []


def test_busy_voter_is_skipped_in_a_quorum_like_a_rate_limited_one(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("on battery")})
    r = build(fake, {"classify": {"fast": ["big", "small", "other"], "quorum": 2}})
    fake.by_model.update(small=[L("negative")], other=[L("negative")])
    out = tools.classify(r, "x", LABELS)
    assert out["label"] == "negative" and out["_minion"]["busy"] == ["big"]


def test_quorum_missed_because_of_a_busy_voter_is_a_no_quorum_not_a_guess(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("on battery")})
    r = build(fake, {"classify": {"fast": ["big", "small", "other"], "quorum": 3}})
    fake.by_model.update(small=[L("negative")], other=[L("negative")])
    with pytest.raises(NoQuorum):
        tools.classify(r, "x", LABELS)


def test_every_voter_busy_raises_busy(fake, monkeypatch):
    refuse(monkeypatch, {"big": Busy("a"), "small": Busy("b")})
    r = build(fake, {"classify": {"fast": ["big", "small"], "quorum": 2}})
    with pytest.raises(Busy):
        tools.classify(r, "x", LABELS)
