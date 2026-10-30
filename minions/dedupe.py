"""dedupe: group near-identical texts in a batch. The partition is checked in code: every id in exactly one group."""
from __future__ import annotations

from .errors import SchemaError

TASK = ("The data lists texts, one per line as [id] text. Group the ids of texts that say the same thing: the same "
        "facts, only worded, spelled or ordered differently. Texts that differ in any fact (a number, a name, a date, "
        "a negation) are NOT the same and go in different groups. Every id goes in exactly one group; a text with no "
        "twin is a group of its own.")


def schema(ids: list[str]) -> dict:
    return {"type": "object",
            "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "string", "enum": ids}}}},
            "required": ["groups"], "additionalProperties": False}


def render(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{i}] {' '.join(t.split())}" for i, t in pairs)


def make_check(ids: list[str]):
    def check(obj: dict) -> None:
        flat = [i for g in obj["groups"] for i in g]
        if any(not g for g in obj["groups"]):
            raise SchemaError("a group is empty", str(obj)[:200])
        unknown = sorted(set(flat) - set(ids))
        if unknown:
            raise SchemaError(f"unknown ids in the groups: {unknown[:5]}", str(obj)[:200])
        seen = {i for i in flat if flat.count(i) > 1}
        if seen:
            raise SchemaError(f"ids in more than one group: {sorted(seen)[:5]}", str(obj)[:200])
        missing = [i for i in ids if i not in flat]
        if missing:
            raise SchemaError(f"ids left out of the groups: {missing[:5]}", str(obj)[:200])

    return check
