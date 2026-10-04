from __future__ import annotations

from pathlib import Path

import jsonschema

from . import router
from .config import Roster
from .errors import BadInput


def read_text_file(cfg, path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise BadInput(f"not a file: {path}")
    size = p.stat().st_size
    if size > cfg.max_file_bytes:
        raise BadInput(f"file is {size} bytes, limit is {cfg.max_file_bytes}")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise BadInput(f"file is not valid UTF-8: {path}") from e


def _go(cfg, tool, task, text, schema, name, labels=None) -> dict:
    roster = Roster.coerce(cfg)
    out, meta = router.run(roster, tool, task, text, schema, name, labels)
    return {**out, "_minion": meta} if roster.meta else out


def _check_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise BadInput("text must be a non-empty string")


def classify(cfg, text: str, labels: list[str], hint: str = "") -> dict:
    _check_text(text)
    if not labels or len(set(labels)) != len(labels) or not all(isinstance(l, str) and l for l in labels):
        raise BadInput("labels must be a non-empty list of unique non-empty strings")
    if len(labels) > 50:
        raise BadInput("at most 50 labels")
    schema = {"type": "object", "properties": {"label": {"type": "string", "enum": list(labels)}},
              "required": ["label"], "additionalProperties": False}
    task = f"Classify the data into exactly one of: {', '.join(labels)}." + (f" {hint}" if hint else "")
    return _go(cfg, "classify", task, text, schema, "classification", labels)


def extract(cfg, text: str, schema: dict, hint: str = "") -> dict:
    _check_text(text)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise BadInput('schema must be a JSON Schema object with "type": "object"')
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise BadInput(f"invalid JSON Schema: {e.message}") from e
    task = "Extract the requested fields from the data. Use only what the data states." + (f" {hint}" if hint else "")
    return _go(cfg, "extract", task, text, schema, "extraction")


def summarize(cfg, text: str, max_words: int = 100) -> dict:
    _check_text(text)
    if not 5 <= max_words <= 1000:
        raise BadInput("max_words must be between 5 and 1000")
    schema = {"type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"], "additionalProperties": False}
    return _go(cfg, "summarize", f"Summarize the data in at most {max_words} words.", text, schema, "summary")
