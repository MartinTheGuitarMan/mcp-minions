from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import BadConfig

DEFAULT_URLS = {"lmstudio": "http://127.0.0.1:1234/v1", "ollama": "http://127.0.0.1:11434/v1"}
BACKENDS = (*DEFAULT_URLS, "openai-compat", "systemone")   # openai-compat: any OpenAI-style endpoint, base_url required
CONSTRAIN = ("json_schema", "json_object", "none")
TOOLS = ("classify", "extract", "summarize")
ASK = "ask"   # open questions: only models marked "knowledge" (they passed the hippo test) may serve it
KINDS = ("fast", "reasoning", "decision")   # decision: System One model, classify only
DEFAULT_TIMEOUT = {"fast": 120.0, "reasoning": 600.0, "decision": 60.0}
SYSTEMONE_URL = "https://api.typesafe.ai/v1"


@dataclass(frozen=True)
class Config:
    """One model endpoint. Also usable on its own as a single-model setup (v0.1 behaviour)."""
    backend: str                 # lmstudio | ollama
    base_url: str                # OpenAI-compatible root, ending in /v1
    model: str
    timeout: float = 120.0
    max_file_bytes: int = 1_000_000
    ollama_native: bool = False  # use /api/chat `format` instead of response_format json_schema
    kind: str = "fast"           # fast = constrained decoding; reasoning = free thinking, we validate
    name: str = "default"
    max_tokens: int | None = None
    cloud: bool = False          # data leaves this machine: never gets a call marked sensitive
    key_env: str | None = None   # name of the env var holding the API key (the key itself is never stored)
    caps: tuple | None = None    # tools this model may serve; None = all
    rpm: int | None = None       # requests per minute, counted locally
    constrain: str = "json_schema"   # how the schema is requested: json_schema | json_object | none
    knowledge: bool = False      # trusted with open questions: set only after it passed the false-premise (hippo) test
    manage: bool = False         # minions may load and unload this local model (docs/dynamic-models.md)
    context: int = 4096          # context length used when minions loads it
    ttl_s: int = 600             # idle seconds before the runtime unloads it
    mem_gb: float | None = None  # estimated resident size, for the quiet gate

    def serves(self, tool: str) -> bool:
        if tool == ASK and not self.knowledge:
            return False
        if self.kind == "decision" and tool != "classify":
            return False
        return self.caps is None or tool in self.caps

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        backend = env.get("MINION_BACKEND", "lmstudio").lower()
        if backend not in DEFAULT_URLS:
            raise BadConfig(f"MINION_BACKEND must be one of {sorted(DEFAULT_URLS)}, got {backend!r}")
        model = env.get("MINION_MODEL")
        if not model:
            raise BadConfig("MINION_MODEL is not set")
        return cls(
            backend=backend,
            base_url=env.get("MINION_BASE_URL", DEFAULT_URLS[backend]).rstrip("/"),
            model=model,
            timeout=float(env.get("MINION_TIMEOUT", "120")),
            max_file_bytes=int(env.get("MINION_MAX_FILE_BYTES", "1000000")),
            ollama_native=env.get("MINION_OLLAMA_NATIVE", "") == "1",
        )


@dataclass(frozen=True)
class Route:
    fast: tuple          # Configs; more than one only for classify (quorum)
    judge: Config | None
    quorum: int
    debias: bool = False   # classify with ONE fast model: ask twice with the label order reversed
    fallback: tuple = ()   # fast models tried in order when a fast model is rate limited (and only then)
    shortcut: tuple = ()   # (decision Config, min_confidence): a confident answer ends a classify call early


def _is_local_host(url: str) -> bool:
    """Loopback, private address, `.local` name or a bare hostname: a machine you own, not a public service."""
    host = urlparse(url).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return host == "localhost" or host.endswith((".local", ".localhost")) or "." not in host


@dataclass(frozen=True)
class Roster:
    models: dict
    routes: dict
    max_file_bytes: int = 1_000_000
    meta: bool = False   # include a "_minion" key in results (votes, judge usage)

    def route(self, tool: str) -> Route:
        return self.routes[tool]

    @classmethod
    def single(cls, cfg: Config) -> "Roster":
        return cls({cfg.name: cfg}, {t: Route((cfg,), None, 1) for t in TOOLS}, cfg.max_file_bytes)

    @classmethod
    def coerce(cls, x) -> "Roster":
        return x if isinstance(x, Roster) else cls.single(x)

    @classmethod
    def from_env(cls, env=os.environ) -> "Roster":
        src = env.get("MINION_ROSTER", "").strip()
        if not src:
            return cls.single(Config.from_env(env))
        try:
            raw = json.loads(src if src.startswith("{") else Path(src).expanduser().read_text())
        except (OSError, ValueError) as e:
            raise BadConfig(f"cannot read MINION_ROSTER: {e}") from e
        return cls.from_dict(raw, max_file_bytes=int(env.get("MINION_MAX_FILE_BYTES", "1000000")),
                             meta=env.get("MINION_META", "") == "1")

    @classmethod
    def from_dict(cls, raw: dict, max_file_bytes: int = 1_000_000, meta: bool = False) -> "Roster":
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict) or not raw["models"]:
            raise BadConfig('roster needs a non-empty "models" object')
        models: dict[str, Config] = {}
        for name, m in raw["models"].items():
            backend, kind = m.get("backend"), m.get("kind", "fast")
            if backend not in BACKENDS:
                raise BadConfig(f"model {name!r}: backend must be one of {sorted(BACKENDS)}")
            if kind not in KINDS:
                raise BadConfig(f"model {name!r}: kind must be one of {KINDS}")
            if not m.get("model"):
                raise BadConfig(f"model {name!r}: missing \"model\"")
            if (backend == "systemone") != (kind == "decision"):
                raise BadConfig(f"model {name!r}: backend \"systemone\" and kind \"decision\" go together")
            base_url = m.get("base_url") or DEFAULT_URLS.get(backend) or (SYSTEMONE_URL if backend == "systemone" else None)
            if not base_url:
                raise BadConfig(f"model {name!r}: backend {backend!r} needs a \"base_url\"")
            cloud, key_env = bool(m.get("cloud", False)), m.get("key_env")
            if cloud and not key_env:
                raise BadConfig(f"model {name!r}: a cloud model needs \"key_env\" (the env var holding its API key)")
            if not cloud and not _is_local_host(base_url):
                # the privacy gate trusts this flag, so a public host must not slip through as local
                raise BadConfig(f"model {name!r}: {base_url} is not a local address; set \"cloud\": true")
            caps = m.get("caps")
            if caps is not None and (not isinstance(caps, list) or not set(caps) <= {*TOOLS, ASK}):
                raise BadConfig(f"model {name!r}: caps must be a list drawn from {[*TOOLS, ASK]}")
            limits = m.get("limits") or {}
            rpm = limits.get("rpm")
            if rpm is not None and (not isinstance(rpm, int) or rpm < 1):
                raise BadConfig(f"model {name!r}: limits.rpm must be a positive integer")
            constrain = m.get("constrain", "json_schema")
            if constrain not in CONSTRAIN:
                raise BadConfig(f"model {name!r}: constrain must be one of {CONSTRAIN}")
            manage = bool(m.get("manage", False))
            if manage and (cloud or backend not in ("lmstudio", "ollama")):
                raise BadConfig(f"model {name!r}: \"manage\" only applies to local lmstudio or ollama models")
            context, ttl_s, mem_gb = m.get("context", 4096), m.get("ttl_s", 600), m.get("mem_gb")
            if not (isinstance(context, int) and context >= 256 and isinstance(ttl_s, int) and ttl_s >= 60
                    and (mem_gb is None or (isinstance(mem_gb, (int, float)) and mem_gb > 0))):
                raise BadConfig(f"model {name!r}: context must be an integer >= 256, ttl_s an integer >= 60, mem_gb > 0")
            models[name] = Config(
                backend=backend, base_url=base_url.rstrip("/"),
                model=m["model"], timeout=float(m.get("timeout", DEFAULT_TIMEOUT[kind])),
                max_file_bytes=max_file_bytes, ollama_native=bool(m.get("ollama_native", False)),
                kind=kind, name=name, max_tokens=m.get("max_tokens"),
                cloud=cloud, key_env=key_env, caps=tuple(caps) if caps is not None else None, rpm=rpm,
                constrain=constrain, knowledge=bool(m.get("knowledge", False)),
                manage=manage, context=context, ttl_s=ttl_s, mem_gb=float(mem_gb) if mem_gb is not None else None,
            )

        def ref(name, role, tool):
            if name not in models:
                raise BadConfig(f"route {tool!r}: unknown model {name!r}")
            return models[name]

        routes: dict[str, Route] = {}
        has_ask = ASK in raw.get("routes", {}) or any(c.knowledge for c in models.values())
        for tool in TOOLS + ((ASK,) if has_ask else ()):
            r = raw.get("routes", {}).get(tool, {})
            fast_names = r.get("fast")
            if fast_names is None:   # route by capability, not by model name
                fast_names = [n for n, c in models.items() if c.kind == "fast" and c.serves(tool)][:1]
            if isinstance(fast_names, str):
                fast_names = [fast_names]
            if not fast_names:
                raise BadConfig(f"route {tool!r}: no fast model available")
            fast = tuple(ref(n, "fast", tool) for n in fast_names)
            if any(c.kind not in ("fast", "decision") for c in fast):
                raise BadConfig(f"route {tool!r}: entries in \"fast\" must have kind fast or decision")
            if any(not c.serves(tool) for c in fast):
                raise BadConfig(f"route {tool!r}: a model in \"fast\" does not list {tool!r} in its caps")
            if len(fast) > 1 and tool != "classify":
                raise BadConfig(f"route {tool!r}: only classify supports more than one fast model")
            if tool == ASK and r.get("judge"):
                raise BadConfig('route "ask": a judge does not apply')
            judge = ref(r["judge"], "judge", tool) if r.get("judge") else None
            if judge is not None and judge.kind != "reasoning":
                raise BadConfig(f"route {tool!r}: judge must have kind reasoning")
            if judge is not None and not judge.serves(tool):
                raise BadConfig(f"route {tool!r}: the judge does not list {tool!r} in its caps")
            fallback = tuple(ref(n, "fallback", tool) for n in r.get("fallback", []))
            if any(c.kind != "fast" or not c.serves(tool) for c in fallback):
                raise BadConfig(f"route {tool!r}: \"fallback\" entries must be fast models that serve {tool!r}")
            shortcut = ()
            if r.get("shortcut"):
                sc = r["shortcut"]
                sm = ref(sc.get("model"), "shortcut", tool)
                conf = sc.get("min_confidence")
                if tool != "classify" or sm.kind != "decision" or not isinstance(conf, (int, float)) or not 0 < conf <= 1:
                    raise BadConfig(f"route {tool!r}: shortcut needs a decision model and min_confidence in (0, 1], classify only")
                shortcut = (sm, float(conf))
            quorum = int(r.get("quorum", len(fast) // 2 + 1))
            if not 1 <= quorum <= len(fast):
                raise BadConfig(f"route {tool!r}: quorum must be between 1 and {len(fast)}")
            debias = r.get("debias")
            if debias is None:   # auto: worth the extra call only when a judge can resolve a mismatch
                debias = tool == "classify" and len(fast) == 1 and judge is not None
            routes[tool] = Route(fast, judge, quorum, bool(debias) and tool == "classify" and len(fast) == 1, fallback, shortcut)
        return cls(models, routes, max_file_bytes, meta)
