"""Value check for extract: a schema-valid answer can still be empty or invented. Every extracted string and
number must occur in the source, after light normalisation (case, accents, whitespace, punctuation, simple number
and date forms). Required fields that fail make the answer a schema failure; optional ones are only flagged."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from .errors import EmptyValue, NotInSource

MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                          "september", "october", "november", "december"))}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())}, sept=9)
_MON = "(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?\.?"
# (pattern, order of the groups): y = year, m = month number, n = month name, d = day
DATE_FORMS = ((re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
              (re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"), "ymd"),
              (re.compile(rf"{_DAY}\s+(?:of\s+)?{_MON},?\s+(\d{{4}})", re.I), "dny"),
              (re.compile(rf"{_MON}\s+{_DAY},?\s+(\d{{4}})", re.I), "ndy"),
              (re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})"), "dmy"),    # also read as m/d/y below
              (re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})"), "mdy"))

WORDS = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                                     "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
                                     "seventeen", "eighteen", "nineteen"))}
TENS = {w: (i + 2) * 10 for i, w in enumerate(("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty",
                                               "ninety"))}
_WORD = re.compile(rf"\b({'|'.join(TENS)})(?:[ -]({'|'.join(list(WORDS)[1:10])}))?\b|\b({'|'.join(WORDS)})\b", re.I)
# digits with grouping or decimal separators: 1,250.50  1.250,50  12 400  3'000. A space only groups three digits.
_NUM = re.compile(r"\d+(?:(?:[,.' ]|\s(?=\d{3}\b))\d+)*")


def _fold(s: str) -> str:
    """Case and accents folded, every run of punctuation or space one space: 'Malmö, INV-2291' -> 'malmo inv 2291'."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def _dates(s: str, whole: bool = False) -> set[date]:
    """Dates written in s (whole=True: s must be nothing but one date). Ambiguous d/m/y gives both readings."""
    out = set()
    for rx, order in DATE_FORMS:
        if whole:
            m = rx.fullmatch(s.strip().rstrip(".,"))
            found = [m] if m else []
        else:
            found = rx.finditer(s)
        for m in found:
            g = dict(zip(order, m.groups()))
            month = MONTHS[g["n"].lower()] if "n" in g else int(g["m"])
            try:
                out.add(date(int(g["y"]), month, int(g["d"])))
            except ValueError:
                pass
    return out


def _numbers(s: str) -> set[float]:
    """Every reading of every number in s: each digit run alone, the whole as grouped thousands, and with the
    last separator as decimal point. Number words up to ninety-nine count too."""
    out = set()
    for m in _NUM.finditer(s):
        parts = re.split(r"[,.'\s ]", m.group(0))
        out.update(float(p) for p in parts)
        out.add(float("".join(parts)))
        if len(parts) > 1:
            out.add(float("".join(parts[:-1]) + "." + parts[-1]))
            out.update(float(f"{a}.{b}") for a, b in zip(parts, parts[1:]))
    for m in _WORD.finditer(s):
        tens, unit, word = (g.lower() if g else g for g in m.groups())
        out.add(float(WORDS[word]) if word else float(TENS[tens] + (WORDS[unit] if unit else 0)))
    return out


class Source:
    """The source text, normalised once for every lookup."""

    def __init__(self, text: str):
        self.folded = f" {_fold(text)} "
        self.numbers = _numbers(text)
        self.dates = _dates(text)

    def has_number(self, v: float) -> bool:
        return any(abs(abs(v) - n) <= 1e-9 * max(1.0, n) for n in self.numbers)

    def has_string(self, v: str) -> bool:
        f = _fold(v)
        if f and f" {f} " in self.folded:   # whole words only: "Lund" is not found in "Lundby"
            return True
        if (d := _dates(v, whole=True)) and d & self.dates:   # "2026-11-03" for "3 November 2026"
            return True
        num = v.strip().replace(",", "").replace(" ", "")
        return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", num)) and self.has_number(float(num))   # "1250.50" for "1,250.50"


def _checked(spec: dict) -> bool:
    return isinstance(spec, dict) and "enum" not in spec and "const" not in spec


def make_check(schema: dict, text: str):
    """check(obj) -> optional fields not found in the source. Raises EmptyValue or NotInSource for a required one.
    Top-level properties only: strings and numbers, and the strings and numbers in a top-level array. Booleans,
    null, nested objects and fields with enum or const are not checked."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    src = Source(text)

    def problem(v) -> tuple[type, str] | None:
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, str):
            if not v.strip():
                return EmptyValue, "is empty"
            return None if src.has_string(v) else (NotInSource, f"{v[:60]!r} is not in the source")
        if isinstance(v, (int, float)):
            if src.has_number(v):
                return None
            return (EmptyValue, "is 0 and the source has no 0") if v == 0 else (NotInSource, f"{v!r} is not in the source")
        return None

    def check(obj: dict) -> list[str]:
        unverified = []
        for field, v in obj.items():
            if not _checked(props.get(field)):
                continue
            if field not in required and isinstance(v, str) and not v.strip():
                continue   # an optional field left empty says "not stated"
            if isinstance(v, list):
                if not _checked(props[field].get("items") or {}):
                    continue
                bad = next((p for p in map(problem, v) if p), None)
            else:
                bad = problem(v)
            if bad is None:
                continue
            if field in required:
                raise bad[0](f"required field {field!r} {bad[1]}", str(obj)[:200])
            unverified.append(field)
        return unverified

    return check
