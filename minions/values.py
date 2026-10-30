"""Value check for extract: a schema-valid answer can still be empty or invented. Every extracted string and
number must occur in the source, after light normalisation (case, accents, whitespace, punctuation, simple number
and date forms). Required fields that fail make the answer a schema failure; optional ones are only flagged."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from .errors import EmptyValue, Invented, NotInSource, TooLong

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


# ---------- summarize: the facts in a summary must come from the source ----------

# Capitalised words that are not names: they may open a clause after a colon or a dash, or be the pronoun "I".
COMMON = frozenset("""i a an the and or but nor so yet of in on at to for with by from into about after before over
under this that these those it its he she they we you his her their our there here then also overall however
both all some many most more less no not one each every other such while although though because as if when
mr mrs ms dr prof summary note key point points main brief briefly""".split())
_OPEN = r"""['"“‘(\[]*"""
_SENT_START = re.compile(rf"(?:^|[.!?:;]['\")\]”’]*\s+|\n\s*(?:[-*•]|\d+[.)])?\s*){_OPEN}$")
_WORD_TOKEN = re.compile(r"(?<!\w)[^\W\d_][\w'’]*")   # a word, not the letters of a code like 4B
_ENDING = re.compile(r"['’](?:s|m|re|ve|ll|d|t)?$", re.I)
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_SCALE_WORD = re.compile(r"\s*(thousand|million|billion)\b", re.I)


def word_limit(max_words: int) -> int:
    """The most words a summary may have: max_words plus 10% (at least 2 words), since models count loosely."""
    return max_words + max(2, -(-max_words // 10))


def _readings(token: str) -> tuple[set[float], list[float]]:
    """(whole readings of a number token: grouped, or with the last separator as decimal point; its digit runs)."""
    parts = re.split(r"[,.'\s ]", token)
    whole = {float("".join(parts))}
    if len(parts) > 1:
        whole.add(float("".join(parts[:-1]) + "." + parts[-1]))
    return whole, [float(p) for p in parts]


def _number_ok(token: str, after: str, src: Source) -> bool:
    """A number in the summary is found when a whole reading of it is in the source, or it has three or more parts
    and every one is (a version or dotted date written another way), or it is scaled by a word the source spells
    out ('1.2 million'). A plain decimal must match as a whole: 1.3 is not found from a 1 and a 3."""
    whole, parts = _readings(token)
    if any(src.has_number(v) for v in whole) or (len(parts) > 2 and all(src.has_number(p) for p in parts)):
        return True
    m = _SCALE_WORD.match(after)
    return bool(m) and any(src.has_number(v * _SCALE[m.group(1).lower()]) for v in whole)


def _word_ok(word: str, src: Source) -> bool:
    """A capitalised word is found as a whole word after folding, without a plural or possessive ending, or
    (for words over 4 letters) as the start of a source word sharing all but its last 3 letters (Sweden, Swedish)."""
    f = _fold(word)
    forms = {f, f[:-1]} if f.endswith("s") and len(f) > 3 else {f}
    if any(f" {x} " in src.folded for x in forms):
        return True
    return len(f) > 4 and f" {f[:max(4, len(f) - 3)]}" in src.folded


def _is_name(word: str, before: str) -> bool:
    """Should this word be checked as a name or term? Capitalised and not a common word; at the start of a
    sentence, a list item or a clause after a colon only if it is clearly a name (all capitals, or a capital
    inside it: NASA, McKinley)."""
    if not word[0].isupper() or word.casefold() in COMMON:
        return False
    if _SENT_START.search(before):
        return len(word) > 1 and any(c.isupper() for c in word[1:])
    return True


def make_summary_check(text: str, max_words: int):
    """check(obj) for summarize: raises EmptyValue for an empty summary, TooLong over word_limit(max_words), and
    Invented when a number, date or capitalised name or term in the summary does not occur in the source.
    Number words, lower-case words and sentence-initial words (unless all capitals) are not checked."""
    src = Source(text)
    for m in _NUM.finditer(text):   # '1.2 million' in the source also reads as 1200000
        if sc := _SCALE_WORD.match(text, m.end()):
            src.numbers |= {v * _SCALE[sc.group(1).lower()] for v in _readings(m.group(0))[0]}
    limit = word_limit(max_words)

    def check(obj: dict) -> list[str]:
        summary = obj["summary"]
        if not summary.strip():
            raise EmptyValue("the summary is empty", str(obj)[:200])
        n = len(summary.split())
        if n > limit:
            raise TooLong(f"the summary has {n} words; max_words is {max_words} (up to {limit} accepted)",
                          str(obj)[:200])
        rest = summary
        for rx, _ in DATE_FORMS:   # a date the source has, in any simple form, is not checked token by token
            rest = rx.sub(lambda m: " " * len(m.group(0)) if _dates(m.group(0), whole=True) & src.dates
                          else m.group(0), rest)
        missing = [m.group(0) for m in _NUM.finditer(rest) if not _number_ok(m.group(0), rest[m.end():], src)]
        for m in _WORD_TOKEN.finditer(rest):
            w = _ENDING.sub("", m.group(0))
            if w and _is_name(w, rest[:m.start()]) and not _word_ok(w, src):
                missing.append(w)
        if missing:
            shown = ", ".join(repr(t) for t in dict.fromkeys(missing))
            err = Invented(f"the summary names what the source does not: {shown[:200]}", str(obj)[:200])
            err.terms = list(dict.fromkeys(missing))   # for summarize's one repair retry
            raise err
        return []

    return check
