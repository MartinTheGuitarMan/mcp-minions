"""Reasoning path: no constrained decoding. The model thinks freely, we strip the think block,
pull out the JSON, validate against the schema ourselves, and allow exactly one retry."""
from __future__ import annotations

import json
import re

import jsonschema

from . import backends
from .config import Config
from .errors import SchemaError

DEFAULT_MAX_TOKENS = 4096
_DECODER = json.JSONDecoder()


class Invalid(ValueError):
    pass


def strip_think(text: str) -> str:
    """Remove <think>…</think>. Also handles templates that emit only the closing tag.
    An unterminated think block means the answer never arrived (truncated)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        raise Invalid("output ended inside an unterminated <think> block (truncated?)")
    return text.strip()


def extract_json(text: str) -> dict:
    """Last top-level JSON object in the text (fenced or bare)."""
    objs, i = [], 0
    while True:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = _DECODER.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            objs.append(obj)
        i = end
    if not objs:
        raise Invalid("no JSON object found in the reply")
    return objs[-1]


def run(cfg: Config, system: str, user: str, schema: dict, validator=None) -> dict:
    """validator(obj) may raise ValueError for semantic checks beyond the schema."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last = "unknown"
    for attempt in (1, 2):
        raw = backends.complete(cfg, messages, None, max_tokens=cfg.max_tokens or DEFAULT_MAX_TOKENS)
        answer = ""
        try:
            answer = strip_think(raw)
            obj = extract_json(answer)
            try:
                jsonschema.validate(obj, schema)
            except jsonschema.ValidationError as e:
                raise Invalid(f"violates schema: {e.message}") from e
            if validator:
                try:
                    validator(obj)
                except ValueError as e:
                    raise Invalid(str(e)) from e
            return obj
        except Invalid as e:
            last = str(e)
            tail = answer or raw[-1500:]
            messages = messages + [
                {"role": "assistant", "content": tail[-3000:]},
                {"role": "user", "content": f"Your reply was rejected: {last}. Reply again with only the "
                                            "corrected JSON object matching the schema."},
            ]
    raise SchemaError(f"{cfg.name}: reasoning model failed validation after one retry: {last}")
