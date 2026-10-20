"""Step 1 (read-only status): parsers, gate truth table, snapshot. No model, runtime or real machine is touched."""
import json
from datetime import datetime

import pytest

from minions import lifecycle as lc
from minions import tools
from minions.config import Roster

LMS_PS = json.dumps([{"identifier": "example/m-14b", "sizeBytes": 9 * lc.GB, "contextLength": 4096, "ttlMs": 600000}])
LMS_LS = json.dumps([{"modelKey": "example/m-14b", "sizeBytes": 9 * lc.GB}, {"modelKey": "example/s-1b", "sizeBytes": lc.GB}])
MEMP = "The system has 25769803776 (6291456 pages)\nSystem-wide memory free percentage: 62%\n"
BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; charging\n"
BATT_BAT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging\n"
DAY = datetime(2026, 10, 21, 12, 0)
NIGHT = datetime(2026, 10, 21, 23, 30)


def runner(mp=MEMP, level="1\n", batt=BATT_AC, ps=LMS_PS, ls=LMS_LS):
    table = {("memory_pressure",): mp, ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): level,
             ("pmset", "-g", "batt"): batt, ("lms", "ps", "--json"): ps, ("lms", "ls", "--json"): ls}
    return lambda cmd, timeout=15.0: table[cmd]


def no_runtime(cmd, timeout=15.0):
    return None if cmd[0] == "lms" else runner()(cmd)


def test_parsers():
    assert lc.parse_lms_ps(LMS_PS) == [{"name": "example/m-14b", "bytes": 9 * lc.GB, "context": 4096, "ttl_ms": 600000}]
    assert [m["name"] for m in lc.parse_lms_ls(LMS_LS)] == ["example/m-14b", "example/s-1b"]
    assert lc.parse_lms_ps("not json") == [] and lc.parse_lms_ps(None) == []
    assert lc.parse_ollama_ps({"models": [{"name": "x:1b", "size": 5, "context_length": 2048}]})[0]["context"] == 2048
    assert lc.parse_ollama_tags({"models": [{"name": "x:1b", "size": 5}, {"junk": 1}]}) == [{"name": "x:1b", "bytes": 5}]
    assert lc.parse_free_pct(MEMP) == 62 and lc.parse_free_pct("nothing") is None
    assert [lc.parse_pressure_level(x) for x in ("1", "2\n", "4", "9", None)] == ["normal", "warn", "critical", None, None]
    assert lc.parse_on_ac(BATT_AC) is True and lc.parse_on_ac(BATT_BAT) is False and lc.parse_on_ac(None) is None
    assert lc.parse_on_ac("Now drawing from 'AC Power'\n") is True    # desktop, no battery


def test_command_allowlist_rejects_anything_else():
    with pytest.raises(ValueError):
        lc.run_command(("lms", "unload", "--all"))
    with pytest.raises(ValueError):
        lc.run_command(("rm", "-rf", "x"))


def test_windows_wrap_midnight():
    assert lc.in_window("23:00-06:00", NIGHT) and lc.in_window("23:00-06:00", datetime(2026, 10, 21, 5, 59))
    assert not lc.in_window("23:00-06:00", DAY) and not lc.in_window("23:00-06:00", datetime(2026, 10, 21, 6, 0))
    assert lc.in_window("09:00-17:00", DAY) and not lc.in_window("garbage", DAY)


def test_hold_marker_env_expiry(tmp_path):
    p = tmp_path / "hold"
    assert lc.read_hold(p, DAY, {}) is None
    p.write_text("")
    assert lc.read_hold(p, DAY, {}) == {"reason": "hold marker present"}
    p.write_text("night job\n2026-10-22T06:00:00\n")
    assert lc.read_hold(p, DAY, {}) == {"reason": "night job", "until": "2026-10-22T06:00:00"}
    p.write_text("night job\n2026-10-21T06:00:00\n")
    assert lc.read_hold(p, DAY, {}) is None                       # expired marker is ignored
    assert lc.read_hold(None, DAY, {"MINION_HOLD": "1"})["reason"] == "MINION_HOLD=1"


def ok(**kw):
    base = dict(free_pct=60, pressure="normal", on_ac=True)
    base.update(kw)
    return lc.gate(lc.Probes(**base))


def test_gate_truth_table():
    assert ok() == {"allowed": True, "reasons": []}
    assert not ok(free_pct=40)["allowed"] and "need 45" in ok(free_pct=40)["reasons"][0]
    assert not ok(pressure="warn")["allowed"] and not ok(pressure="critical")["allowed"]
    assert not ok(on_ac=False)["allowed"]
    assert lc.gate(lc.Probes(free_pct=60, pressure="normal", on_ac=False), allow_battery=True)["allowed"]
    assert not ok(hold={"reason": "x"})["allowed"]
    assert not ok(window="23:00-06:00")["allowed"]
    assert not ok(resident_large_gb=[9.0])["allowed"]
    for unread in ({"free_pct": None}, {"pressure": None}, {"on_ac": None}):     # a failed probe blocks loading
        assert not ok(**unread)["allowed"]
    low = lc.gate(lc.Probes(free_pct=50, pressure="normal", on_ac=True), need_gb=8, total_gb=24)
    assert not low["allowed"] and "fall below 25" in low["reasons"][0]


def lm_roster():
    return Roster.from_dict({"models": {"a": {"backend": "lmstudio", "model": "example/m-14b"}}})


def test_snapshot_lmstudio_quiet_machine(tmp_path):
    s = lc.snapshot(lm_roster(), {"MINION_HOLD_FILE": str(tmp_path / "none"), "MINION_MODEL_DIR": str(tmp_path)},
                    DAY, runner())
    lm = s["runtimes"]["lmstudio"]
    assert lm["reachable"] and len(lm["resident"]) == 1 and len(lm["installed"]) == 2
    assert s["read_only"] and s["model_volume"]["free_gb"] > 0
    # the 9 GB model is resident and above the 6 GB large threshold, so no second large load
    assert not s["load_gate"]["allowed"] and "large model" in s["load_gate"]["reasons"][0]


def test_snapshot_night_window_and_unreachable(tmp_path):
    env = {"MINION_HOLD_FILE": str(tmp_path / "none"), "MINION_QUIET_WINDOWS": "23:00-06:00"}
    s = lc.snapshot(lm_roster(), env, NIGHT, no_runtime)
    assert s["runtimes"]["lmstudio"] == {"reachable": False}
    assert any("quiet window" in r for r in s["load_gate"]["reasons"])


def test_snapshot_ollama_uses_http_get_and_native_root():
    seen = []

    def get(url, timeout=5.0):
        seen.append(url)
        return {"models": [{"name": "x:1b", "size": 2 * lc.GB}]}
    r = Roster.from_dict({"models": {"o": {"backend": "ollama", "model": "x:1b"}}})
    s = lc.snapshot(r, {"MINION_HOLD_FILE": "/nonexistent/hold"}, DAY, runner(), get)
    assert seen == ["http://127.0.0.1:11434/api/ps", "http://127.0.0.1:11434/api/tags"]
    assert s["runtimes"]["ollama"]["resident"][0]["name"] == "x:1b" and s["load_gate"]["allowed"]


def test_cloud_only_roster_has_no_runtimes():
    r = Roster.from_dict({"models": {"c": {"backend": "openai-compat", "base_url": "https://api.example.com/v1",
                                           "model": "m", "cloud": True, "key_env": "K"}}})
    assert lc.snapshot(r, {"MINION_HOLD_FILE": "/nonexistent/hold"}, DAY, runner())["runtimes"] == {}


def test_status_unchanged_without_opt_in(monkeypatch):
    monkeypatch.delenv("MINION_LIFECYCLE", raising=False)
    assert "lifecycle" not in tools.status(lm_roster())
    monkeypatch.setenv("MINION_LIFECYCLE", "1")
    monkeypatch.setattr(lc, "run_command", lambda cmd, timeout=15.0: None)    # nothing real is ever run
    st = tools.status(lm_roster())
    assert st["lifecycle"]["runtimes"]["lmstudio"] == {"reachable": False} and not st["lifecycle"]["load_gate"]["allowed"]
