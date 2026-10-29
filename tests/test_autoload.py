"""Step 3: a route loads a managed model before calling it, behind the quiet gate. Fakes only: the runtime is the
fake backend plus a fake `lms`, the machine probes are canned, and no model or real machine is touched."""
import json
import os
import threading
import time

import pytest

from minions import lifecycle as lc, manage, tools
from minions.config import Roster
from minions.errors import Busy, LoadFailed

GB = lc.GB
LABELS = ["positive", "negative"]


def L(label):
    return json.dumps({"label": label})


class Machine:
    """Fake `lms` and probes. Records every command; `loaded` is what `lms ps` reports."""
    def __init__(self, loaded=(), battery=False, load_ok=True, load_s=0.0):
        self.loaded, self.battery, self.load_ok, self.load_s = set(loaded), battery, load_ok, load_s
        self.commands, self.lock = [], threading.Lock()

    def runner(self, cmd, timeout=15.0):
        with self.lock:
            self.commands.append(cmd)
        if cmd == ("lms", "ps", "--json"):
            return json.dumps([{"identifier": k, "sizeBytes": 9 * GB if "big" in k else GB} for k in self.loaded])
        if cmd == ("lms", "ls", "--json"):
            return "[]"
        if cmd[0] == "memory_pressure":
            return "System-wide memory free percentage: 80%"
        if cmd[:2] == ("sysctl", "-n"):
            return "1" if cmd[2].startswith("kern") else str(24 * GB)
        if cmd[0] == "pmset":
            return "Now drawing from 'Battery Power'\n -InternalBattery-0" if self.battery else "Now drawing from 'AC Power'"
        raise AssertionError(cmd)

    def action(self, cmd, timeout=600.0):
        time.sleep(self.load_s if cmd[1] == "load" else 0)
        with self.lock:
            self.commands.append(cmd)
            if cmd[1] == "load" and self.load_ok:
                self.loaded.add(cmd[2])
            elif cmd[1] == "unload":
                self.loaded.discard(cmd[2])
        return (self.load_ok if cmd[1] == "load" else True), "out"

    def loads(self):
        return [c for c in self.commands if c[:2] == ("lms", "load")]


@pytest.fixture
def machine(tmp_path, monkeypatch):
    for k, v in {"MINION_STATE_DIR": tmp_path / "state", "MINION_HOLD_FILE": tmp_path / "nohold",
                 "MINION_LOG": tmp_path / "log.jsonl"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("MINION_ALLOW_BATTERY", raising=False)
    mc = Machine()
    monkeypatch.setattr(lc, "run_command", mc.runner)
    monkeypatch.setattr(manage, "run_action", mc.action)
    return mc


def build(fake, routes, **extra):
    models = {"big": {"model": "ex/big-26b", "manage": True, "mem_gb": 9, "context": 2048, "ttl_s": 300},
              "big2": {"model": "ex/big2-14b", "manage": True, "mem_gb": 9},
              "small": {"model": "ex/small-9b"}}
    models.update(extra)
    for m in models.values():
        m.setdefault("backend", "lmstudio")
        m.setdefault("timeout", 5)
        if m["backend"] == "lmstudio":
            m.setdefault("base_url", fake.url)
    return Roster.from_dict({"models": models, "routes": routes}, meta=True)


ROUTE = {"classify": {"fast": ["big"], "fallback": ["small"]}}


def test_not_loaded_and_gate_open_loads_then_calls(fake, machine):
    fake.by_model["ex/big-26b"] = [L("positive")]
    out = tools.classify(build(fake, ROUTE), "x", LABELS)
    assert out["label"] == "positive" and out["_minion"]["failover"] == []
    assert machine.loads() == [("lms", "load", "ex/big-26b", "-c", "2048", "--ttl", "300", "-y")]
    assert len(fake.calls("ex/big-26b")) == 1 and fake.calls("ex/small-9b") == []
    assert "big" in manage.read_ours(dict(os.environ))
    log = [json.loads(x) for x in open(os.environ["MINION_LOG"])]
    assert [e["by"] for e in log if e.get("event") == "load"] == ["auto"]


def test_gate_refuses_raises_busy_and_falls_back(fake, machine):
    machine.battery = True
    fake.by_model["ex/small-9b"] = [L("negative")]
    out = tools.classify(build(fake, ROUTE), "x", LABELS)
    assert out["label"] == "negative"
    assert out["_minion"]["busy"] == ["big"] and out["_minion"]["failover"] == [["big", "small"]]
    assert machine.loads() == [] and fake.calls("ex/big-26b") == []   # never sent: LM Studio would JIT-load it


def test_gate_refuses_with_no_fallback_raises_busy(fake, machine):
    machine.battery = True
    with pytest.raises(Busy, match="on battery"):
        tools.classify(build(fake, {"classify": {"fast": ["big"]}}), "x", LABELS)
    assert fake.requests == []


def test_busy_never_turns_a_sensitive_call_into_a_cloud_call(fake, machine):
    machine.battery = True
    r = build(fake, {"classify": {"fast": ["big"], "fallback": ["web"]}},
              web={"backend": "openai-compat", "model": "web", "cloud": True, "base_url": fake.url, "key_env": "NO_KEY"})
    with pytest.raises(Busy):
        tools.classify(r, "x", LABELS)
    assert fake.requests == []


def test_load_failure_propagates(fake, machine):
    machine.load_ok = False
    fake.by_model["ex/small-9b"] = [L("negative")]
    with pytest.raises(LoadFailed):
        tools.classify(build(fake, ROUTE), "x", LABELS)
    assert fake.requests == []


def test_already_loaded_makes_no_load_call(fake, machine):
    machine.loaded.add("ex/big-26b")
    machine.battery = True   # the gate is only for loads; using what is resident costs nothing extra
    fake.by_model["ex/big-26b"] = [L("positive")]
    assert tools.classify(build(fake, ROUTE), "x", LABELS)["label"] == "positive"
    assert machine.loads() == [] and machine.commands == [("lms", "ps", "--json")]


def test_concurrent_calls_load_once(fake, machine):
    machine.load_s = 0.3
    fake.dynamic = lambda body: L("positive")
    r, outs = build(fake, ROUTE), []
    threads = [threading.Thread(target=lambda: outs.append(tools.classify(r, "x", LABELS)["label"])) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outs == ["positive"] * 4
    assert len(machine.loads()) == 1 and len(fake.calls("ex/big-26b")) == 4


def test_unmanaged_models_are_not_checked(fake, machine):
    fake.by_model["ex/small-9b"] = [L("positive")]
    tools.classify(build(fake, {"classify": {"fast": ["small"]}}), "x", LABELS)
    assert machine.commands == []


def test_a_model_in_flight_is_not_unloaded_to_make_room(fake, machine, monkeypatch):
    r = build(fake, {"classify": {"fast": ["big2"]}})
    manage.load(r, "big")                         # ours and large
    monkeypatch.setitem(manage._inflight, "big", 1)   # another call is answering with it
    with pytest.raises(Busy, match="answering a call"):
        tools.classify(r, "x", LABELS)
    assert "ex/big-26b" in machine.loaded and fake.requests == []


def test_an_idle_model_of_ours_is_unloaded_to_make_room(fake, machine):
    r = build(fake, {"classify": {"fast": ["big2"]}})
    manage.load(r, "big")
    fake.by_model["ex/big2-14b"] = [L("negative")]
    assert tools.classify(r, "x", LABELS)["label"] == "negative"
    assert machine.loaded == {"ex/big2-14b"}


def test_managed_judge_is_loaded_before_it_verifies(fake, machine):
    r = build(fake, {"classify": {"fast": ["small", "big2"], "quorum": 2, "judge": "j"}},
              j={"model": "ex/judge-big", "kind": "reasoning", "manage": True, "mem_gb": 4})
    machine.loaded.add("ex/big2-14b")
    fake.by_model.update({"ex/small-9b": [L("positive")], "ex/big2-14b": [L("negative")],
                          "ex/judge-big": [json.dumps({"verdict": "confirm", "label": "negative"})]})
    out = tools.classify(r, "x", LABELS)
    assert out["_minion"]["judge"] == "j"
    assert [c[2] for c in machine.loads()] == ["ex/judge-big"]


def test_ollama_residency_is_read_from_api_ps():
    cfg = Roster.from_dict({"models": {"o": {"backend": "ollama", "base_url": "http://127.0.0.1:1/v1",
                                             "model": "ex-small:9b", "manage": True}}}).models["o"]
    seen = []

    def get(url):
        seen.append(url)
        return {"models": [{"name": "ex-small:9b", "size": GB}]}
    assert manage.is_resident(cfg, http_get=get) and seen == ["http://127.0.0.1:1/api/ps"]
    assert not manage.is_resident(cfg, http_get=lambda url: None)   # unreachable counts as not resident
