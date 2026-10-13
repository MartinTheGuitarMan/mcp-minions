"""System One decision models (for example TypeSafe's Jev): answer a typed question about a text with a
probability instead of generating text. Used for classify only, as a voter or as a confident first line."""
from __future__ import annotations

from . import backends
from .config import Config
from .errors import SchemaError


def run(cfg: Config, task: str, text: str, labels: list) -> tuple[str, float | None]:
    """One pick-one question. Returns (label, confidence in 0..1 or None). The label is checked in code."""
    body = {"model": cfg.model, "state": text,
            "questions": {"label": {"type": "choice", "instructions": task, "criteria": {l: l for l in labels}}}}
    raw = backends._post(cfg, f"{cfg.base_url}/systemone", body)
    try:
        ans = raw["answers"]["label"]
        label = ans["choice"]
    except (KeyError, TypeError) as e:
        raise SchemaError(f"{cfg.name}: unexpected decision response: {str(raw)[:200]}", str(raw)) from e
    if label not in labels:
        raise SchemaError(f"{cfg.name}: decision model chose {label!r}, not one of the labels", str(label))
    conf = ans.get("confidence")
    return label, float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None
