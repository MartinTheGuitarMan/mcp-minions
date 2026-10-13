"""System One decision models (for example TypeSafe's Jev): answer a typed question about a text with a
probability instead of generating text. Used for classify only, as a voter or as a confident first line."""
from __future__ import annotations

from . import backends, ratelimit
from .config import Config
from .errors import SchemaError


def ask_choice(cfg: Config, text: str, instructions: str, criteria: dict) -> tuple[str, float | None, dict]:
    """One pick-one question about a text. Returns (choice, confidence or None, probabilities).
    The choice is checked in code against the criteria keys."""
    labels = list(criteria)
    body = {"model": cfg.model, "state": text,
            "questions": {"label": {"type": "choice", "instructions": instructions, "criteria": criteria}}}
    raw = backends._post(cfg, f"{cfg.base_url}/systemone", body)
    u = raw.get("usage") if isinstance(raw, dict) else None
    if isinstance(u, dict):
        ratelimit.add_usage(cfg, u.get("input_tokens"), u.get("output_tokens"))
    try:
        ans = raw["answers"]["label"]
        label = ans["choice"]
    except (KeyError, TypeError) as e:
        raise SchemaError(f"{cfg.name}: unexpected decision response: {str(raw)[:200]}", str(raw)) from e
    if label not in labels:
        raise SchemaError(f"{cfg.name}: decision model chose {label!r}, not one of the labels", str(label))
    conf = ans.get("confidence")
    probs = ans.get("probabilities") if isinstance(ans.get("probabilities"), dict) else {}
    return label, float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None, probs


def run(cfg: Config, task: str, text: str, labels: list) -> tuple[str, float | None]:
    """Classify: one pick-one question over the labels. Returns (label, confidence in 0..1 or None)."""
    label, conf, _ = ask_choice(cfg, text, task, {l: l for l in labels})
    return label, conf
