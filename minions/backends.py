"""HTTP clients for the two backends. Fast models get schema-constrained JSON, but we never trust that
it was honoured (engine.py validates). Reasoning models are called unconstrained."""
from __future__ import annotations

import os
import time

import httpx

from . import ratelimit
from .config import Config
from .errors import BackendError, BadConfig, MinionDown, RateLimited, SchemaError

RETRIES_ON_429 = 2
MAX_WAIT = 10.0   # longer waits are not worth blocking a tool call for: report RateLimited and let the route fail over


def _headers(cfg: Config) -> dict:
    if not cfg.key_env:
        return {}
    key = os.environ.get(cfg.key_env, "").strip()
    if not key:
        raise BadConfig(f"{cfg.name}: environment variable {cfg.key_env} is not set")
    return {"Authorization": f"Bearer {key}"}


def _retry_after(r: httpx.Response, attempt: int) -> float:
    try:
        return max(0.0, float(r.headers["Retry-After"]))
    except (KeyError, ValueError):
        return 2.0 ** attempt   # 1s, 2s


def _send(cfg: Config, url: str, payload: dict) -> httpx.Response:
    """POST, retrying a 429 with backoff. Still limited after the retries: RateLimited."""
    headers = _headers(cfg)
    for attempt in range(RETRIES_ON_429 + 1):
        ratelimit.acquire(cfg)
        r = _post_once(cfg, url, payload, headers)
        ratelimit.record(cfg, r.headers)
        if r.status_code != 429:
            return r
        wait = _retry_after(r, attempt)
        if attempt == RETRIES_ON_429 or wait > MAX_WAIT:
            raise RateLimited(f"{cfg.name}: provider rate limit (HTTP 429), retry after {wait:g}s", wait)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _post_once(cfg: Config, url: str, payload: dict, headers: dict) -> httpx.Response:
    try:
        return httpx.post(url, json=payload, headers=headers, timeout=httpx.Timeout(cfg.timeout, connect=5.0))
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise MinionDown(f"{cfg.backend} backend not reachable at {url} ({e.__class__.__name__})") from e
    except httpx.TimeoutException as e:
        raise BackendError(f"{cfg.backend}/{cfg.model} timed out after {cfg.timeout}s") from e
    except httpx.HTTPError as e:
        raise BackendError(f"{cfg.backend} transport error: {e}") from e


def _post(cfg: Config, url: str, payload: dict) -> dict:
    r = _send(cfg, url, payload)
    if r.status_code == 400 and ("does not match the expected schema" in r.text or "json_validate_failed" in r.text):
        # the provider checked the model's JSON against our schema and rejected it: a schema failure, not an outage
        raise SchemaError(f"{cfg.name}: provider rejected the model output as not matching the schema: {r.text[:200]}")
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
    if schema is not None and cfg.constrain == "json_schema":
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": name, "strict": True, "schema": schema}}
    elif schema is not None and cfg.constrain == "json_object":
        payload["response_format"] = {"type": "json_object"}   # we validate against the schema anyway
    if max_tokens:
        payload["max_tokens"] = max_tokens
    body = _post(cfg, f"{cfg.base_url}/chat/completions", payload)
    try:
        ch = body["choices"][0]
        return ch["message"]["content"] or "", ch.get("finish_reason") or "stop"
    except (KeyError, IndexError, TypeError) as e:
        raise BackendError(f"unexpected response shape: {str(body)[:200]}") from e
