"""check_claims: split a text into claims and mark each one supported or not against a source.
A schema-valid reply is not an answer yet: every supported claim needs a quote found in the source and may only use
numbers and capitalised names the source has, and no number or name of the text may drop out of the claims."""
from __future__ import annotations

from . import values
from .errors import BadInput, Invented, NotInSource, SchemaError

MAX_CLAIMS = 40
SCHEMA = {"type": "object",
          "properties": {"claims": {"type": "array", "maxItems": MAX_CLAIMS, "items": {
              "type": "object",
              "properties": {"claim": {"type": "string"}, "supported": {"type": "boolean"}, "quote": {"type": "string"}},
              "required": ["claim", "supported", "quote"], "additionalProperties": False}}},
          "required": ["claims"], "additionalProperties": False}
TASK = ("The data holds a TEXT and a SOURCE. Split the TEXT into its separate factual claims, one fact each, and keep "
        "every one: do not leave any out. For each claim set supported to true only if the SOURCE states it; judge by "
        "the SOURCE alone, never by what you know. A claim with a number, name or date that the SOURCE does not have is "
        "not supported. For a supported claim put in quote the shortest passage copied exactly from the SOURCE that "
        "shows it; for a claim that is not supported leave quote empty.")


def terms(text: str, src: values.Source) -> tuple[list[str], list[str]]:
    """(numbers, names) of text that the source does not have, plus all of them: -> (missing, all)."""
    missing, every = [], []
    for m in values._NUM.finditer(text):
        every.append(m.group(0))
        if not values._number_ok(m.group(0), text[m.end():], src):
            missing.append(m.group(0))
    for m in values._WORD_TOKEN.finditer(text):
        w = values._ENDING.sub("", m.group(0))
        if w and values._is_name(w, text[:m.start()]):
            every.append(w)
            if not values._word_ok(w, src):
                missing.append(w)
    return missing, every


def make_check(text: str, source: str, quote_found):
    src = values.Source(source)
    wanted = terms(text, values.Source(text))[1]   # numbers and names the text mentions

    def check(obj: dict) -> None:
        claims = obj["claims"]
        if not claims:
            raise SchemaError("no claims came back", str(obj)[:200])
        for c in claims:
            if not c["claim"].strip():
                raise SchemaError("a claim is empty", str(obj)[:200])
            if not c["supported"]:
                if c["quote"].strip():
                    c["quote"] = ""   # a quote on an unsupported claim proves nothing: dropped, not trusted
                continue
            if not quote_found(c["quote"], source):
                raise NotInSource(f"supported claim {c['claim'][:60]!r} has no quote found in the source "
                                  f"(quoted {c['quote'][:60]!r})", str(obj)[:200])
            missing, _ = terms(c["claim"], src)
            if missing:
                raise Invented(f"supported claim {c['claim'][:60]!r} names what the source does not: {missing}",
                               str(obj)[:200])
        joined = values._fold(" ".join(c["claim"] for c in claims))
        dropped = [t for t in wanted if values._fold(t) not in joined]
        if dropped:
            raise SchemaError(f"claims leave out what the text says: {dropped[:5]}", str(obj)[:200])

    return check
