from __future__ import annotations

import os
from pathlib import Path

import jsonschema

import time

from . import calllog, lifecycle, ratelimit, router, tokens
from .config import ASK, Roster
from .errors import BadInput, MinionError


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


def _go(cfg, tool, build, text, name, labels=None, sensitive=True, descriptions=None) -> dict:
    roster = Roster.coerce(cfg)
    started = time.time()
    tally = tokens.start()
    try:
        out, meta = router.run(roster, tool, build, text, name, labels, sensitive, descriptions)
    except MinionError as e:
        calllog.record(tool, sensitive, started, text, None, e.code, tally.summary())
        raise
    finally:
        tokens.stop()
    calllog.record(tool, sensitive, started, text, meta, None, tally.summary())
    return {**out, "_minion": meta} if roster.meta else out


def _check_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise BadInput("text must be a non-empty string")


def classify(cfg, text: str, labels: list[str], hint: str = "", sensitive: bool = True,
             descriptions: dict | None = None) -> dict:
    _check_text(text)
    if not labels or len(set(labels)) != len(labels) or not all(isinstance(l, str) and l for l in labels):
        raise BadInput("labels must be a non-empty list of unique non-empty strings")
    if len(labels) > 50:
        raise BadInput("at most 50 labels")
    if descriptions is not None and (not isinstance(descriptions, dict) or not set(descriptions) <= set(labels)
                                     or not all(isinstance(v, str) for v in descriptions.values())):
        raise BadInput("descriptions must map labels from the label list to short strings")
    meaning = ("\nLabel meanings:\n" + "\n".join(f"- {l}: {d}" for l, d in descriptions.items())) if descriptions else ""

    def build(order=None):
        order = list(order or labels)
        schema = {"type": "object", "properties": {"label": {"type": "string", "enum": order}},
                  "required": ["label"], "additionalProperties": False}
        return f"Classify the data into exactly one of: {', '.join(order)}." + (f" {hint}" if hint else "") + meaning, schema

    return _go(cfg, "classify", build, text, "classification", list(labels), sensitive, descriptions)


def extract(cfg, text: str, schema: dict, hint: str = "", sensitive: bool = True) -> dict:
    _check_text(text)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise BadInput('schema must be a JSON Schema object with "type": "object"')
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise BadInput(f"invalid JSON Schema: {e.message}") from e
    task = "Extract the requested fields from the data. Use only what the data states." + (f" {hint}" if hint else "")
    return _go(cfg, "extract", lambda order=None: (task, schema), text, "extraction", None, sensitive)


def summarize(cfg, text: str, max_words: int = 100, sensitive: bool = True) -> dict:
    _check_text(text)
    if not 5 <= max_words <= 1000:
        raise BadInput("max_words must be between 5 and 1000")
    schema = {"type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"], "additionalProperties": False}
    task = f"Summarize the data in at most {max_words} words."
    return _go(cfg, "summarize", lambda order=None: (task, schema), text, "summary", None, sensitive)


ASK_SCHEMA = {"type": "object",
              "properties": {"answer": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
              "required": ["answer", "confidence"], "additionalProperties": False}
ASK_TASK = ("Answer the question in the data. Use only what you actually know. If the question rests on something you "
            "cannot confirm (a paper, species, person, product or fact), say you cannot confirm it instead of answering "
            "as if it were real. Never invent names, citations, numbers or dates. Keep the answer short. "
            "Set confidence to low when you are unsure.")


def ask(cfg, question: str, sensitive: bool = True) -> dict:
    """Open question for a model that passed the false-premise test. Not for anything that must be exactly right."""
    _check_text(question)
    roster = Roster.coerce(cfg)
    if ASK not in roster.routes:
        raise BadInput('no model is trusted with open questions: mark a roster model "knowledge": true '
                       "after it passes the hippo test")
    return _go(roster, ASK, lambda order=None: (ASK_TASK, ASK_SCHEMA), question, "answer", None, sensitive)


def report(days: float = 7.0) -> dict:
    """Usage summary from the call log (MINION_LOG): no texts or answers are ever stored."""
    return calllog.report(days)


def status(cfg) -> dict:
    """What the roster holds and how much of each minion's local request budget is used. Never shows keys."""
    roster = Roster.coerce(cfg)
    rows = []
    for name, m in roster.models.items():
        row = {"name": name, "model": m.model, "kind": m.kind, "cloud": m.cloud,
               "caps": list(m.caps) if m.caps is not None else "all"}
        if m.key_env:
            row["key_set"] = bool(os.environ.get(m.key_env, "").strip())
        if m.rpm is not None:
            row["rpm"] = {"limit": m.rpm, "used_last_minute": ratelimit.used(m)}
        spent = ratelimit.usage(m)
        if spent:
            row["tokens_used_by_this_server"] = spent
        quota = ratelimit.provider_quota(m)
        if quota:
            row["provider_quota"] = quota
        rows.append(row)
    out = {"models": rows, "note": "used_last_minute is counted by this server; provider_quota is what the "
           "provider reported with its latest reply (absent until the minion has been called in this server process)"}
    if os.environ.get("MINION_LIFECYCLE") == "1":   # opt-in, read-only: resident/installed models and the load gate
        out["lifecycle"] = lifecycle.snapshot(roster)
    return out
