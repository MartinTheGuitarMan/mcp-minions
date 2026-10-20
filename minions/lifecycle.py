"""Read-only view of the machine and its model runtimes (step 1 of docs/dynamic-models.md).

Nothing here loads, unloads, downloads or deletes. It asks LM Studio / Ollama what is resident and installed, reads a
few machine probes (free memory, memory pressure, power, a hold marker, quiet windows, free disk) and says whether a
load *would* be allowed right now. Every external call goes through `run_command` or an injected `http_get`, so
tests never need a model, a runtime or a real machine. Parsers are tolerant: a field a runtime version does not send
is left out, never guessed."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

# The only commands this module may run. Constants, never built from caller text or model output.
ALLOWED = {
    ("lms", "ps", "--json"), ("lms", "ls", "--json"),
    ("memory_pressure",), ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"), ("sysctl", "-n", "hw.memsize"),
    ("pmset", "-g", "batt"),
}
GB = 1024 ** 3


def run_command(cmd: tuple[str, ...], timeout: float = 15.0) -> str | None:
    """Run one allowlisted read-only command. None when it is missing, fails or times out."""
    if cmd not in ALLOWED:
        raise ValueError(f"command not on the read-only list: {cmd}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------- parsers (pure) ----------
def _num(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def _rows(text: str | None) -> list[dict]:
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("models", [])
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def parse_lms_ps(text: str | None) -> list[dict]:
    """Resident models from `lms ps --json`."""
    out = []
    for d in _rows(text):
        name = d.get("identifier") or d.get("modelKey") or d.get("path")
        if name:
            out.append(_clean({"name": name, "bytes": _num(d, "sizeBytes", "size"),
                               "context": _num(d, "contextLength", "context"), "ttl_ms": _num(d, "ttlMs", "ttl")}))
    return out


def parse_lms_ls(text: str | None) -> list[dict]:
    """Installed models from `lms ls --json`."""
    return [_clean({"name": d.get("modelKey") or d.get("path"), "bytes": _num(d, "sizeBytes", "size")})
            for d in _rows(text) if d.get("modelKey") or d.get("path")]


def parse_ollama_ps(data: dict | None) -> list[dict]:
    """Resident models from GET /api/ps."""
    return [_clean({"name": m.get("name") or m.get("model"), "bytes": _num(m, "size"),
                    "context": _num(m, "context_length"), "expires_at": m.get("expires_at")})
            for m in (data or {}).get("models", []) if isinstance(m, dict) and (m.get("name") or m.get("model"))]


def parse_ollama_tags(data: dict | None) -> list[dict]:
    """Installed models from GET /api/tags."""
    return [_clean({"name": m.get("name") or m.get("model"), "bytes": _num(m, "size")})
            for m in (data or {}).get("models", []) if isinstance(m, dict) and (m.get("name") or m.get("model"))]


def parse_free_pct(text: str | None) -> int | None:
    """'System-wide memory free percentage: 62%' from `memory_pressure`."""
    m = re.search(r"free percentage:\s*(\d+)\s*%", text or "")
    return int(m.group(1)) if m else None


def parse_pressure_level(text: str | None) -> str | None:
    """kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical."""
    return {"1": "normal", "2": "warn", "4": "critical"}.get((text or "").strip())


def parse_on_ac(text: str | None) -> bool | None:
    """True on AC (or a machine with no battery), False on battery, None if unknown. From `pmset -g batt`."""
    if not text:
        return None
    if "Battery Power" in text:
        return False
    return True if "AC Power" in text or "InternalBattery" not in text else None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ---------- hold marker and quiet windows ----------
def read_hold(path: Path | None, now: datetime, env: dict | None = None) -> dict | None:
    """A hold exists when MINION_HOLD=1 or the marker file exists. Optional contents: a reason line, then an
    ISO 8601 expiry on a line of its own; a marker whose expiry has passed is ignored."""
    env = os.environ if env is None else env
    if env.get("MINION_HOLD") == "1":
        return {"reason": "MINION_HOLD=1"}
    if path is None or not path.exists():
        return None
    reason, until = "hold marker present", None
    try:
        lines = [x.strip() for x in path.read_text().splitlines() if x.strip()]
    except OSError:
        lines = []
    for line in lines:
        try:
            until = datetime.fromisoformat(line)
        except ValueError:
            reason = line[:120]
    if until is not None and until.replace(tzinfo=None) <= now.replace(tzinfo=None):
        return None
    out = {"reason": reason}
    if until is not None:
        out["until"] = until.isoformat()
    return out


def in_window(spec: str, now: datetime) -> bool:
    """'23:00-06:00' (wraps midnight) in local time."""
    m = re.fullmatch(r"(\d\d):(\d\d)-(\d\d):(\d\d)", spec.strip())
    if not m:
        return False
    a, b, t = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4]), now.hour * 60 + now.minute
    return a <= t < b if a <= b else (t >= a or t < b)


# ---------- the gate (pure) ----------
@dataclass
class Probes:
    free_pct: int | None = None
    pressure: str | None = None
    on_ac: bool | None = None
    hold: dict | None = None
    window: str | None = None                 # the quiet window we are inside, if any
    resident_large_gb: list[float] = field(default_factory=list)


def gate(p: Probes, *, need_gb: float = 0.0, total_gb: float | None = None, min_free_pct: int = 45,
         floor_pct: int = 25, allow_battery: bool = False) -> dict:
    """Would a load be allowed now? Returns {allowed, reasons}. A probe that could not be read blocks the load."""
    why: list[str] = []
    if p.free_pct is None:
        why.append("free memory could not be read")
    else:
        if p.free_pct < min_free_pct:
            why.append(f"free memory {p.free_pct} percent, need {min_free_pct}")
        elif total_gb and need_gb and p.free_pct - 100 * need_gb / total_gb < floor_pct:
            why.append(f"free memory would fall below {floor_pct} percent after loading about {need_gb:g} GB")
    if p.pressure in ("warn", "critical"):
        why.append(f"memory pressure is {p.pressure}")
    elif p.pressure is None:
        why.append("memory pressure could not be read")
    if p.on_ac is False and not allow_battery:
        why.append("on battery")
    elif p.on_ac is None:
        why.append("power state could not be read")
    if p.hold:
        why.append("hold: " + p.hold["reason"] + (f" until {p.hold['until']}" if p.hold.get("until") else ""))
    if p.window:
        why.append(f"inside quiet window {p.window}")
    if p.resident_large_gb:
        why.append("a large model is already resident")
    return {"allowed": not why, "reasons": why}


# ---------- gathering ----------
def local_probes(env: dict | None = None, now: datetime | None = None, runner: Callable | None = None,
                 resident: list[dict] | None = None) -> Probes:
    env = os.environ if env is None else env
    runner = runner or run_command     # looked up at call time so tests can replace it
    now = now or datetime.now()
    large = float(env.get("MINION_LARGE_GB", "6"))
    hold_path = env.get("MINION_HOLD_FILE")
    mp = runner(("memory_pressure",))
    windows = [w for w in re.split(r"[,\s]+", env.get("MINION_QUIET_WINDOWS", "")) if w]
    return Probes(
        free_pct=parse_free_pct(mp),
        pressure=parse_pressure_level(runner(("sysctl", "-n", "kern.memorystatus_vm_pressure_level"))),
        on_ac=parse_on_ac(runner(("pmset", "-g", "batt"))),
        hold=read_hold(Path(hold_path).expanduser() if hold_path else Path.home() / ".minions" / "hold", now, env),
        window=next((w for w in windows if in_window(w, now)), None),
        resident_large_gb=[round(r["bytes"] / GB, 1) for r in resident or [] if r.get("bytes", 0) / GB > large])


def _ollama_root(base_url: str) -> str:
    return base_url[:-3] if base_url.endswith("/v1") else base_url.rstrip("/")


def http_get_json(url: str, timeout: float = 5.0) -> dict | None:
    import httpx
    try:
        r = httpx.get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def snapshot(roster, env: dict | None = None, now: datetime | None = None, runner: Callable | None = None,
             http_get: Callable | None = None) -> dict:
    """Read-only picture: what is resident and installed per runtime, the probes, and the gate verdict now.
    Runtimes that do not answer are reported as unreachable; that is never an error."""
    env = os.environ if env is None else env
    runner, http_get = runner or run_command, http_get or http_get_json
    backends = {m.backend for m in roster.models.values() if not m.cloud}
    runtimes: dict[str, dict] = {}
    if "lmstudio" in backends:
        ps, ls = runner(("lms", "ps", "--json")), runner(("lms", "ls", "--json"))
        runtimes["lmstudio"] = ({"reachable": False} if ps is None and ls is None else
                                {"reachable": True, "resident": parse_lms_ps(ps), "installed": parse_lms_ls(ls)})
    if "ollama" in backends:
        root = next(_ollama_root(m.base_url) for m in roster.models.values() if m.backend == "ollama" and not m.cloud)
        ps, tags = http_get(root + "/api/ps"), http_get(root + "/api/tags")
        runtimes["ollama"] = ({"reachable": False} if ps is None and tags is None else
                              {"reachable": True, "resident": parse_ollama_ps(ps), "installed": parse_ollama_tags(tags)})
    resident = [r for rt in runtimes.values() for r in rt.get("resident", [])]
    probes = local_probes(env, now, runner, resident)
    model_dir = env.get("MINION_MODEL_DIR")
    disk = None
    if model_dir and Path(model_dir).expanduser().exists():
        u = shutil.disk_usage(Path(model_dir).expanduser())
        disk = {"free_gb": round(u.free / GB, 1), "used_pct": round(100 * u.used / u.total)}
    return {"runtimes": runtimes,
            "probes": {k: v for k, v in vars(probes).items() if v is not None},
            "load_gate": gate(probes, min_free_pct=int(env.get("MINION_MIN_FREE_PCT", "45")),
                              allow_battery=env.get("MINION_ALLOW_BATTERY") == "1"),
            "model_volume": disk, "read_only": True}
