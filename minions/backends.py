"""HTTP clients for the two backends. Both request schema-constrained JSON; we never trust that
it was honoured (engine.py validates), because whether enforcement holds is backend-dependent."""
from __future__ import annotations

import httpx

from .config import Config
from .errors import BackendError, MinionDown


def _post(cfg: Config, url: str, payload: dict) -> dict:
    try:
        r = httpx.post(url, json=payload, timeout=httpx.Timeout(cfg.timeout, connect=5.0))
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise MinionDown(f"{cfg.backend} backend not reachable at {url} ({e.__class__.__name__})") from e
    except httpx.TimeoutException as e:
        raise BackendError(f"{cfg.backend} timed out after {cfg.timeout}s") from e
    except httpx.HTTPError as e:
        raise BackendError(f"{cfg.backend} transport error: {e}") from e
    if r.status_code >= 400:
        raise BackendError(f"{cfg.backend} HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError as e:
        raise BackendError(f"{cfg.backend} returned non-JSON body: {r.text[:200]}") from e


def complete_json(cfg: Config, system: str, user: str, schema: dict, name: str = "result") -> str:
    """Return the raw assistant message content (not parsed)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if cfg.backend == "ollama" and cfg.ollama_native:
        root = cfg.base_url.removesuffix("/v1")
        body = _post(cfg, f"{root}/api/chat", {
            "model": cfg.model, "messages": messages, "stream": False,
            "format": schema, "options": {"temperature": 0},
        })
        try:
            return body["message"]["content"]
        except (KeyError, TypeError) as e:
            raise BackendError(f"unexpected ollama response shape: {str(body)[:200]}") from e
    body = _post(cfg, f"{cfg.base_url}/chat/completions", {
        "model": cfg.model, "messages": messages, "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
    })
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise BackendError(f"unexpected response shape: {str(body)[:200]}") from e
