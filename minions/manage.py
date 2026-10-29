"""Load and unload of managed local models (steps 2 and 3 of docs/dynamic-models.md).

Only roster models with "manage": true can be touched, only ones minions loaded itself can be unloaded, and every load
passes the quiet gate first (lifecycle.gate). The read-only module `lifecycle` stays read-only: everything that changes
a runtime lives here, behind `run_action` and `http_post_json`, which tests replace. Commands are built from the
roster's model key (owner config, never caller text), and the key is checked against a strict pattern.

`ready` is the router's way in (step 3): before a call to a managed model it checks the model is resident and, if not,
runs the same gate and load as `minion_load`. The request is never sent to a model that is not resident, because
LM Studio (with just-in-time loading on) and Ollama would load it themselves and skip the gate."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import calllog, lifecycle
from .errors import BadInput, Busy, LoadFailed

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


def _check_key(key: str) -> str:
    if not KEY_RE.match(key):
        raise LoadFailed(f"model key {key!r} has characters minions will not pass to a command")
    return key


def run_action(cmd: tuple[str, ...], timeout: float = 600.0) -> tuple[bool, str]:
    """Run one `lms load` or `lms unload` command. Anything else is refused."""
    if len(cmd) < 3 or cmd[:2] not in (("lms", "load"), ("lms", "unload")):
        raise ValueError(f"not an allowed action: {cmd}")
    _check_key(cmd[2])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr)[-300:]


def http_post_json(url: str, body: dict, timeout: float = 600.0) -> dict | None:
    import httpx
    try:
        r = httpx.post(url, json=body, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


# ---------- state: which resident models are ours ----------
def _state_dir(env: dict) -> Path:
    return Path(env.get("MINION_STATE_DIR") or "~/.minions").expanduser()


@contextmanager
def _locked(env: dict):
    d = _state_dir(env)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_ours(env: dict) -> dict:
    try:
        data = json.loads((_state_dir(env) / "state.json").read_text())
        return data.get("ours", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_ours(env: dict, ours: dict) -> None:
    p = _state_dir(env) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ours": ours}, indent=1))
    tmp.replace(p)


# ---------- helpers ----------
def _managed(roster, name: str):
    cfg = roster.models.get(name)
    if cfg is None:
        raise BadInput(f"unknown model {name!r}")
    if not cfg.manage:
        raise BadInput(f'model {name!r} is not managed; set "manage": true in the roster to let minions load it')
    return cfg


def _same(model: str, resident_name: str) -> bool:
    return resident_name == model or resident_name.endswith("/" + model) or model.endswith("/" + resident_name)


def _resident(roster, env, now, runner, http_get) -> list[dict]:
    snap = lifecycle.snapshot(roster, env, now, runner, http_get)
    return [r for rt in snap["runtimes"].values() for r in rt.get("resident", [])]


def _do(cfg, op: str, action: Callable, http_post: Callable) -> None:
    """Ask the runtime to load or unload. Raises LoadFailed when it says no."""
    if cfg.backend == "lmstudio":
        cmd = (("lms", "load", _check_key(cfg.model), "-c", str(cfg.context), "--ttl", str(cfg.ttl_s), "-y")
               if op == "load" else ("lms", "unload", _check_key(cfg.model)))
        ok, out = action(cmd)
        if not ok:
            raise LoadFailed(f"lms {op} {cfg.model} failed: {out.strip()[-200:]}")
        return
    body = ({"model": cfg.model, "keep_alive": f"{cfg.ttl_s}s", "options": {"num_ctx": cfg.context}}
            if op == "load" else {"model": cfg.model, "keep_alive": 0})
    if http_post(lifecycle._ollama_root(cfg.base_url) + "/api/generate", body) is None:
        raise LoadFailed(f"ollama {op} {cfg.model} failed or the server did not answer")


def _total_gb(env, runner) -> float | None:
    if env.get("MINION_TOTAL_GB"):
        return float(env["MINION_TOTAL_GB"])
    try:
        return int((runner(("sysctl", "-n", "hw.memsize")) or "").strip()) / lifecycle.GB
    except ValueError:
        return None


# ---------- load and unload ----------
def load(roster, name: str, env: dict | None = None, now: datetime | None = None, runner: Callable | None = None,
         http_get: Callable | None = None, http_post: Callable | None = None, action: Callable | None = None,
         by: str = "manual") -> dict:
    env = os.environ if env is None else env
    runner, http_get = runner or lifecycle.run_command, http_get or lifecycle.http_get_json
    http_post, action = http_post or http_post_json, action or run_action
    now = now or datetime.now()
    cfg = _managed(roster, name)
    with _locked(env):
        resident = _resident(roster, env, now, runner, http_get)
        ours = read_ours(env)
        if any(_same(cfg.model, r["name"]) for r in resident):
            return {"model": name, "state": "already_loaded", "loaded_by_minions": name in ours}
        large = float(env.get("MINION_LARGE_GB", "6"))
        is_large = cfg.mem_gb is None or cfg.mem_gb > large
        mine = [r for r in resident if any(_same(o["model"], r["name"]) for o in ours.values())]
        mine_large = [r for r in mine if r.get("bytes", 0) / lifecycle.GB > large]
        total = _total_gb(env, runner)
        probes = lifecycle.local_probes(env, now, runner, [r for r in resident if r not in mine_large] if is_large else [])
        if is_large and probes.free_pct is not None and total:
            probes.free_pct = min(100, probes.free_pct + round(100 * sum(r.get("bytes", 0) for r in mine_large) / lifecycle.GB / total))
        verdict = lifecycle.gate(probes, need_gb=cfg.mem_gb or 0.0, total_gb=total,
                                 min_free_pct=int(env.get("MINION_MIN_FREE_PCT", "45")),
                                 allow_battery=env.get("MINION_ALLOW_BATTERY") == "1")
        if not verdict["allowed"]:
            calllog.event("refused", model=name, gate=verdict["reasons"][0].split(" ")[0])
            raise Busy("; ".join(verdict["reasons"]))
        if is_large:   # one large model at a time: make room by unloading our own, never someone else's
            keys = [next(o["model"] for o in ours.values() if _same(o["model"], r["name"])) for r in mine_large]
            busy = [c.name for c in roster.models.values() if c.manage and c.model in keys and _inflight[c.name]]
            if busy:   # never unload a model with a request in flight
                calllog.event("refused", model=name, gate="in_use")
                raise Busy(f"{', '.join(busy)} is answering a call; it will not be unloaded to make room")
            for r in mine_large:
                key = next(o["model"] for o in ours.values() if _same(o["model"], r["name"]))
                other = next(c for c in roster.models.values() if c.model == key and c.manage)
                _do(other, "unload", action, http_post)
                ours = {k: v for k, v in ours.items() if v["model"] != key}
                calllog.event("unload", model=other.name, reason="room")
        t0 = time.time()
        _do(cfg, "load", action, http_post)
        if not any(_same(cfg.model, r["name"]) for r in _resident(roster, env, now, runner, http_get)):
            _write_ours(env, ours)
            raise LoadFailed(f"{name} is not resident after the load command")
        ours[name] = {"model": cfg.model, "runtime": cfg.backend, "loaded_at": round(time.time())}
        _write_ours(env, ours)
        secs = round(time.time() - t0, 1)
    calllog.event("load", model=name, ctx=cfg.context, free_before_pct=probes.free_pct, load_s=secs, by=by)
    return {"model": name, "state": "loaded", "context": cfg.context, "ttl_s": cfg.ttl_s, "load_s": secs}


def unload(roster, name: str, env: dict | None = None, now: datetime | None = None, runner: Callable | None = None,
           http_get: Callable | None = None, http_post: Callable | None = None, action: Callable | None = None) -> dict:
    env = os.environ if env is None else env
    runner, http_get = runner or lifecycle.run_command, http_get or lifecycle.http_get_json
    http_post, action = http_post or http_post_json, action or run_action
    cfg = _managed(roster, name)
    with _locked(env):
        ours = read_ours(env)
        is_resident = any(_same(cfg.model, r["name"]) for r in _resident(roster, env, now or datetime.now(), runner, http_get))
        if not is_resident:
            _write_ours(env, {k: v for k, v in ours.items() if k != name})
            return {"model": name, "state": "not_loaded"}
        if name not in ours:
            raise BadInput(f"{name} is loaded, but not by minions; it will not be unloaded from here")
        _do(cfg, "unload", action, http_post)
        _write_ours(env, {k: v for k, v in ours.items() if k != name})
    calllog.event("unload", model=name, reason="manual")
    return {"model": name, "state": "unloaded"}


# ---------- auto-load before a call (step 3) ----------
_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_inflight: Counter = Counter()   # calls in progress per managed model name


def is_resident(cfg, runner: Callable | None = None, http_get: Callable | None = None) -> bool:
    """One cheap look at the model's own runtime (`lms ps --json` or Ollama's /api/ps). An unreachable runtime
    counts as not resident, so the caller goes through the load path and fails there instead of sending a request."""
    if cfg.backend == "lmstudio":
        rows = lifecycle.parse_lms_ps((runner or lifecycle.run_command)(("lms", "ps", "--json")))
    else:
        rows = lifecycle.parse_ollama_ps((http_get or lifecycle.http_get_json)(lifecycle._ollama_root(cfg.base_url) + "/api/ps"))
    return any(_same(cfg.model, r["name"]) for r in rows)


@contextmanager
def ready(roster, cfg, env: dict | None = None):
    """Around one call to `cfg`. Unmanaged models pass straight through. A managed one that is not resident is loaded
    first through `load` (quiet gate, TTL, context): a refusal raises MINION_BUSY, a failed load MINION_LOAD_FAILED.
    One lock per model, so concurrent calls load it once; the model counts as in use until the call returns."""
    if not cfg.manage:
        yield
        return
    with _guard:
        lock = _locks.setdefault(cfg.name, threading.Lock())
        _inflight[cfg.name] += 1
    try:
        with lock:
            if not is_resident(cfg):
                load(roster, cfg.name, env, by="auto")
        yield
    finally:
        with _guard:
            _inflight[cfg.name] -= 1
