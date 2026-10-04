from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import BadConfig

DEFAULT_URLS = {"lmstudio": "http://127.0.0.1:1234/v1", "ollama": "http://127.0.0.1:11434/v1"}


@dataclass(frozen=True)
class Config:
    backend: str                 # lmstudio | ollama
    base_url: str                # OpenAI-compatible root, ending in /v1
    model: str
    timeout: float = 120.0
    max_file_bytes: int = 1_000_000
    ollama_native: bool = False  # use /api/chat `format` instead of response_format json_schema

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
