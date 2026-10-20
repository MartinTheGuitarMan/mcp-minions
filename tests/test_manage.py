"""Step 2: manual load/unload with fakes only. No runtime, model or real machine is touched."""
import json
from datetime import datetime

import pytest

from minions import calllog, lifecycle as lc, manage
from minions.config import Roster
from minions.errors import BadConfig, BadInput, Busy, LoadFailed

GB = lc.GB
DAY = datetime(2026, 10, 21, 12, 0)
NIGHT = datetime(2026, 10, 21, 23, 30)


class Machine:
    """A fake machine: resident LM Studio models, probes, and a log of every action requested."""
    def __init__(self, resident=(), free=70, battery=False, load_ok=True):
        self.resident = {k: v for k, v in resident}          # key -> bytes
        self.free, self.battery, self.load_ok, self.actions, self.posts = free, battery, load_ok, [], []

    def runner(self, cmd, timeout=15.0):
        if cmd == ("lms", "ps", "--json"):
            return json.dumps([{"identifier": k, "sizeBytes": b} for k, b in self.resident.items()])
        if cmd == ("lms", "ls", "--json"):
            return "[]"
        if cmd[0] == "memory_pressure":
            return f"System-wide memory free percentage: {self.free}%"
        if cmd[:2] == ("sysctl", "-n") and cmd[2].startswith("kern"):
            return "1"
        if cmd[:2] == ("sysctl", "-n"):
            return str(24 * GB)
        if cmd[0] == "pmset":
            return "Now drawing from 'Battery Power'\n -InternalBattery-0" if self.battery else "Now drawing from 'AC Power'"
        raise AssertionError(cmd)

    def action(self, cmd):
        self.actions.append(cmd)
        if cmd[1] == "load" and self.load_ok:
            self.resident[cmd[2]] = 9 * GB if "big" in cmd[2] else GB
        elif cmd[1] == "unload":
            self.resident.pop(cmd[2], None)
        return (self.load_ok if cmd[1] == "load" else True), "out"


def roster(**extra):
    m = {"big": {"backend": "lmstudio", "model": "ex/big-14b", "manage": True, "mem_gb": 9, "context": 2048, "ttl_s": 300},
         "big2": {"backend": "lmstudio", "model": "ex/big2-14b", "manage": True, "mem_gb": 9},
         "small": {"backend": "lmstudio", "model": "ex/small-1b", "manage": True, "mem_gb": 1},
         "plain": {"backend": "lmstudio", "model": "ex/plain"}}
    m.update(extra)
    return Roster.from_dict({"models": m})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINION_LOG", str(tmp_path / "log.jsonl"))     # calllog reads the real environment
    return {"MINION_STATE_DIR": str(tmp_path / "state"), "MINION_HOLD_FILE": str(tmp_path / "nohold"),
            "MINION_LOG": str(tmp_path / "log.jsonl")}


def L(mc, name, env, now=DAY, r=None):
    return manage.load(r or roster(), name, env, now, mc.runner, None, None, mc.action)


def U(mc, name, env, r=None):
    return manage.unload(r or roster(), name, env, DAY, mc.runner, None, None, mc.action)


def test_roster_fields_and_validation():
    c = roster().models["big"]
    assert (c.manage, c.context, c.ttl_s, c.mem_gb) == (True, 2048, 300, 9.0)
    assert roster().models["plain"].manage is False and roster().models["plain"].ttl_s == 600
    for bad in ({"manage": True, "cloud": True, "key_env": "K", "base_url": "https://a.example/v1"},
                {"context": 10}, {"ttl_s": 5}, {"mem_gb": -1}):
        with pytest.raises(BadConfig):
            roster(x={"backend": "lmstudio", "model": "m", **bad})


def test_load_runs_lms_load_and_records_ours(env):
    mc = Machine()
    out = L(mc, "big", env)
    assert out["state"] == "loaded"
    assert mc.actions == [("lms", "load", "ex/big-14b", "-c", "2048", "--ttl", "300", "-y")]
    assert "big" in manage.read_ours(env)
    assert L(mc, "big", env)["state"] == "already_loaded" and len(mc.actions) == 1


def test_unmanaged_and_unknown_are_rejected(env):
    mc = Machine()
    for name in ("plain", "nope"):
        with pytest.raises(BadInput):
            L(mc, name, env)
        with pytest.raises(BadInput):
            U(mc, name, env)
    assert mc.actions == []


@pytest.mark.parametrize("kw,now,envx,word", [
    ({"free": 30}, DAY, {}, "free memory"),
    ({"battery": True}, DAY, {}, "battery"),
    ({}, NIGHT, {"MINION_QUIET_WINDOWS": "23:00-06:00"}, "quiet window"),
    ({}, DAY, {"MINION_HOLD": "1"}, "hold"),
])
def test_gate_refuses_with_busy_and_does_nothing(env, kw, now, envx, word):
    mc = Machine(**kw)
    with pytest.raises(Busy, match=word):
        manage.load(roster(), "small", {**env, **envx}, now, mc.runner, None, None, mc.action)
    assert mc.actions == [] and manage.read_ours(env) == {}
    assert "refused" in open(env["MINION_LOG"]).read()


def test_foreign_large_model_blocks_a_large_load_but_not_a_small_one(env):
    mc = Machine(resident=[("someone/else-20b", 12 * GB)])
    with pytest.raises(Busy, match="large model"):
        L(mc, "big", env)
    assert L(mc, "small", env)["state"] == "loaded"
    assert ("lms", "unload", "someone/else-20b") not in mc.actions


def test_our_own_large_model_is_unloaded_to_make_room(env):
    mc = Machine(free=70)
    L(mc, "big", env)
    mc.free = 30                      # our resident model is what uses the memory
    assert L(mc, "big2", env)["state"] == "loaded"
    assert ("lms", "unload", "ex/big-14b") in mc.actions
    assert list(manage.read_ours(env)) == ["big2"]


def test_load_failure_and_not_resident_afterwards(env):
    with pytest.raises(LoadFailed):
        L(Machine(load_ok=False), "small", env)
    assert manage.read_ours(env) == {}

    class Silent(Machine):
        def action(self, cmd):
            self.actions.append(cmd)
            return True, ""            # says ok, but nothing became resident
    with pytest.raises(LoadFailed, match="not resident"):
        L(Silent(), "small", env)


def test_unload_only_what_minions_loaded(env):
    mc = Machine(resident=[("ex/small-1b", GB)])        # resident, but loaded by someone else
    with pytest.raises(BadInput, match="not by minions"):
        U(mc, "small", env)
    assert mc.actions == []
    mc2 = Machine()
    L(mc2, "small", env)
    assert U(mc2, "small", env)["state"] == "unloaded" and manage.read_ours(env) == {}
    assert U(mc2, "small", env)["state"] == "not_loaded"


def test_ollama_uses_keep_alive_posts(env):
    r = Roster.from_dict({"models": {"o": {"backend": "ollama", "model": "x:1b", "manage": True, "mem_gb": 1, "ttl_s": 120}}})
    resident = {}

    def get(url, timeout=5.0):
        return {"models": [{"name": k, "size": GB} for k in resident]}

    def post(url, body, timeout=600.0):
        (resident.__setitem__(body["model"], 1) if body["keep_alive"] != 0 else resident.pop(body["model"], None))
        posts.append((url, body))
        return {}
    posts = []
    mc = Machine()
    assert manage.load(r, "o", env, DAY, mc.runner, get, post, None)["state"] == "loaded"
    assert posts[0] == ("http://127.0.0.1:11434/api/generate", {"model": "x:1b", "keep_alive": "120s", "options": {"num_ctx": 4096}})
    assert manage.unload(r, "o", env, DAY, mc.runner, get, post, None)["state"] == "unloaded"
    assert posts[1][1] == {"model": "x:1b", "keep_alive": 0}


def test_action_allowlist_and_key_pattern():
    for bad in (("lms", "get", "x"), ("rm", "-rf", "x"), ("lms", "load", "-evil"), ("lms", "load", "a b")):
        with pytest.raises((ValueError, LoadFailed)):
            manage.run_action(bad)


def test_report_keeps_events_apart(env):
    mc = Machine()
    L(mc, "small", env)
    U(mc, "small", env)
    rep = calllog.report(1)
    assert rep["calls"] == 0 and rep["lifecycle"]["loads"] == 1 and rep["lifecycle"]["unloads"] == 1
