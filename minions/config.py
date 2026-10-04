from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BadConfig

DEFAULT_URLS = {"lmstudio": "http://127.0.0.1:1234/v1", "ollama": "http://127.0.0.1:11434/v1"}
TOOLS = ("classify", "extract", "summarize")
KINDS = ("fast", "reasoning")
DEFAULT_TIMEOUT = {"fast": 120.0, "reasoning": 600.0}


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
            if backend not in DEFAULT_URLS:
                raise BadConfig(f"model {name!r}: backend must be one of {sorted(DEFAULT_URLS)}")
            if kind not in KINDS:
                raise BadConfig(f"model {name!r}: kind must be one of {KINDS}")
            if not m.get("model"):
                raise BadConfig(f"model {name!r}: missing \"model\"")
            models[name] = Config(
                backend=backend, base_url=m.get("base_url", DEFAULT_URLS[backend]).rstrip("/"),
                model=m["model"], timeout=float(m.get("timeout", DEFAULT_TIMEOUT[kind])),
                max_file_bytes=max_file_bytes, ollama_native=bool(m.get("ollama_native", False)),
                kind=kind, name=name, max_tokens=m.get("max_tokens"),
            )

        def ref(name, role, tool):
            if name not in models:
                raise BadConfig(f"route {tool!r}: unknown model {name!r}")
            return models[name]

        routes: dict[str, Route] = {}
        for tool in TOOLS:
            r = raw.get("routes", {}).get(tool, {})
            fast_names = r.get("fast")
            if fast_names is None:
                fast_names = [n for n, c in models.items() if c.kind == "fast"][:1]
            if isinstance(fast_names, str):
                fast_names = [fast_names]
            if not fast_names:
                raise BadConfig(f"route {tool!r}: no fast model available")
            fast = tuple(ref(n, "fast", tool) for n in fast_names)
            if any(c.kind != "fast" for c in fast):
                raise BadConfig(f"route {tool!r}: entries in \"fast\" must have kind fast")
            if len(fast) > 1 and tool != "classify":
                raise BadConfig(f"route {tool!r}: only classify supports more than one fast model")
            judge = ref(r["judge"], "judge", tool) if r.get("judge") else None
            if judge is not None and judge.kind != "reasoning":
                raise BadConfig(f"route {tool!r}: judge must have kind reasoning")
            quorum = int(r.get("quorum", len(fast) // 2 + 1))
            if not 1 <= quorum <= len(fast):
                raise BadConfig(f"route {tool!r}: quorum must be between 1 and {len(fast)}")
            routes[tool] = Route(fast, judge, quorum)
        return cls(models, routes, max_file_bytes, meta)
