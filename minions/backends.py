"""HTTP clients for the two backends. Fast models get schema-constrained JSON, but we never trust that
it was honoured (engine.py validates). Reasoning models are called unconstrained."""
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
        raise BackendError(f"{cfg.backend}/{cfg.model} timed out after {cfg.timeout}s") from e
    except httpx.HTTPError as e:
        raise BackendError(f"{cfg.backend} transport error: {e}") from e
    if r.status_code >= 400:
        raise BackendError(f"{cfg.backend} HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError as e:
        raise BackendError(f"{cfg.backend} returned non-JSON body: {r.text[:200]}") from e


def complete(cfg: Config, messages: list, schema: dict | None = None, name: str = "result",
             max_tokens: int | None = None) -> str:
    """Return the raw assistant message content. schema=None means unconstrained (free) generation."""
    return complete_ex(cfg, messages, schema, name, max_tokens)[0]


def complete_ex(cfg: Config, messages: list, schema: dict | None = None, name: str = "result",
                max_tokens: int | None = None) -> tuple[str, str]:
    """Like complete, but also returns the finish reason ("length" means the token budget ran out,
    which for a thinking model usually leaves the content empty)."""
    if cfg.backend == "ollama" and cfg.ollama_native:
        payload = {"model": cfg.model, "messages": messages, "stream": False, "options": {"temperature": 0}}
        if schema is not None:
            payload["format"] = schema
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        body = _post(cfg, f"{cfg.base_url.removesuffix('/v1')}/api/chat", payload)
        try:
            finish = "length" if body.get("done_reason") == "length" else "stop"
            return body["message"]["content"] or "", finish
        except (KeyError, TypeError) as e:
            raise BackendError(f"unexpected ollama response shape: {str(body)[:200]}") from e
    payload = {"model": cfg.model, "messages": messages, "temperature": 0}
    if schema is not None:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": name, "strict": True, "schema": schema}}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    body = _post(cfg, f"{cfg.base_url}/chat/completions", payload)
    try:
        ch = body["choices"][0]
        return ch["message"]["content"] or "", ch.get("finish_reason") or "stop"
    except (KeyError, IndexError, TypeError) as e:
        raise BackendError(f"unexpected response shape: {str(body)[:200]}") from e
