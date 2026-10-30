"""check_claims: split a text into claims and mark each one supported or not against a source.
A schema-valid reply is not an answer yet: every supported claim needs a quote found in the source (one whose quote is not found is
downgraded to unsupported with flag quote_not_found) and may only use numbers and capitalised names the source has, and no number or name of the text may drop out of the claims."""
from __future__ import annotations

import re
import unicodedata

from . import values
from .errors import Invented, SchemaError

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


MIN_QUOTE = 3   # a quote shorter than this (non-space characters) proves nothing
_GROUP = re.compile(r"(?<=\d)[,'\u00a0\u2009\u202f](?=\d{3}(?!\d))")   # thousands separators: 1,712 -> 1712
_EDGE = ".,;:!?\"'\u201c\u201d\u2018\u2019()[]"


def _norm(s: str) -> str:
    """The light normalisation of values.py (case, accents, punctuation, whitespace), with thousands separators
    removed from numbers first, so '1,712' and '1712' are one number. No fuzzy matching."""
    return f" {values._fold(_GROUP.sub('', s))} "


def quote_found(quote: str, source: str) -> bool:
    """The quote occurs in the source as whole words, after normalisation. Punctuation round the quote is ignored."""
    q = values._fold(_GROUP.sub("", quote.strip().strip(_EDGE)))
    return len(q.replace(" ", "")) >= MIN_QUOTE and f" {q} " in _norm(source)


# ---- hole 2: a returned claim must come from the input text
MIN_IN_TEXT = 0.65  # share of a claim's content words (and numbers) that must occur in the text, else it is dropped
# ---- hole 1: an unsupported claim with numbers is re-checked in code against single source lines
MIN_IN_LINE = 0.5   # share of the claim's non-number content words that must occur in that one line
_STOP = frozenset("""a an the and or but of in on at to for from by with as is are was were be been being it its this that these
those there here have has had do does did not no than then so such into over under about up down out also which who whom whose
what when where while their his her they them he she we you i our your can could will would may might should shall just very
more most some any all each both per via percent pct""".split())
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[^\W\d_]+")
_NEGATION = frozenset("missing no not without none never lack lacks lacking lacked fail fails failed failing except".split())


def _negated(s: str) -> bool:
    """Any negation word (missing, no, not, without, lack, fail ...): a claim and its line must agree on it."""
    return any(w in _NEGATION for w in values._fold(s).split())


def _stem(w: str) -> str:
    for end in ("ing", "ed", "es", "s"):
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[:-len(end)]
    return w


def _tokens(s: str) -> tuple[set[str], set[str]]:
    """(numbers, stemmed content words) of s. Thousands separators are removed from numbers, so 1,712 is 1712."""
    nums, words = set(), set()
    for t in _TOKEN.findall(_fold_keep_dots(_GROUP.sub("", s))):
        if t[0].isdigit():
            nums.add(t)
        elif t not in _STOP and len(t) > 1:
            words.add(_stem(t))
    return nums, words


def _fold_keep_dots(s: str) -> str:
    """Case and accents folded; punctuation kept, so that 99.2 stays one number."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()


def in_text(claim: str, text: str) -> bool:
    """The claim's content words and numbers mostly (MIN_IN_TEXT) occur in the text."""
    cn, cw = _tokens(claim)
    tn, tw = _tokens(text)
    wanted = cn | cw
    if not wanted:
        return True
    return len((cn & tn) | (cw & tw)) / len(wanted) >= MIN_IN_TEXT


def _lines(source: str) -> list[str]:
    out = []
    for raw in source.splitlines():
        out += [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw) if p.strip()]
    return out


def line_for(claim: str, source: str) -> str | None:
    """A source line that holds every number of the claim (read as whole numbers, separators ignored) and most of its
    other content words, with the same negations. Only claims with a number qualify. -> the line, or None."""
    cn, cw = _tokens(claim)
    if not cn:
        return None
    cneg = _negated(claim)
    best = None
    for line in _lines(source):
        ln, lw = _tokens(line)
        lneg = _negated(line)
        if not cn <= ln or cneg != lneg:
            continue
        if cw and (len(cw & lw) < 1 or len(cw & lw) / len(cw) < MIN_IN_LINE):
            continue
        score = len(cw & lw)
        if best is None or score > best[0]:
            best = (score, line)
    return best[1] if best else None


def make_check(text: str, source: str, quote_found=quote_found):
    src = values.Source(source)
    wanted = terms(text, values.Source(text))[1]   # numbers and names the text mentions

    def check(obj: dict) -> None:
        kept = [c for c in obj["claims"] if not c["claim"].strip() or in_text(c["claim"], text)]
        extra = [c["claim"] for c in obj["claims"] if not any(c is k for k in kept)]
        if extra:
            obj["dropped_not_in_text"] = extra   # claims the model took from the source, not from the text
        obj["claims"] = claims = kept
        if not claims:
            raise SchemaError("no claims came back", str(obj)[:200])
        for c in claims:
            if not c["claim"].strip():
                raise SchemaError("a claim is empty", str(obj)[:200])
            if not c["supported"]:
                if c["quote"].strip():
                    c["quote"] = ""   # a quote on an unsupported claim proves nothing: dropped, not trusted
                line = line_for(c["claim"], source)
                if line and quote_found(line, source) and not terms(c["claim"], src)[0]:
                    c["supported"], c["quote"], c["flag"] = True, line, "rechecked_in_code"
                continue
            if not quote_found(c["quote"], source):
                # never trusted, but only this claim is lost: it is downgraded and flagged, the rest stands
                c["supported"], c["quote"], c["flag"] = False, "", "quote_not_found"
                continue
            missing, _ = terms(c["claim"], src)
            if missing:
                raise Invented(f"supported claim {c['claim'][:60]!r} names what the source does not: {missing}",
                               str(obj)[:200])
        joined = values._fold(" ".join(c["claim"] for c in claims))
        dropped = [t for t in wanted if values._fold(t) not in joined]
        if dropped:
            raise SchemaError(f"claims leave out what the text says: {dropped[:5]}", str(obj)[:200])

    return check
