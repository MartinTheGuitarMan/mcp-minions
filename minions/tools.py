from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

import jsonschema

import time

from . import calllog, engine, lifecycle, manage, ratelimit, router, tokens
from .config import ASK, Roster
from .errors import BadConfig, BadInput, MinionDown, MinionError, NoQuorum, PrivacyRefused, RateLimited, SchemaError


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


def _go(cfg, tool, build, text, name, labels=None, sensitive=True, descriptions=None, after=None) -> dict:
    """after(out, meta) -> out: a check that runs inside the same log line and token tally as the call."""
    roster = Roster.coerce(cfg)
    started = time.time()
    tally = tokens.start()
    try:
        out, meta = router.run(roster, tool, build, text, name, labels, sensitive, descriptions)
        if after is not None:
            out = after(out, meta)
    except MinionError as e:
        calllog.record(tool, sensitive, started, text, None, e.code, tally.summary())
        raise
    finally:
        tokens.stop()
    calllog.record(tool, sensitive, started, text, meta, None, tally.summary())
    return {**out, "_minion": meta} if roster.meta else out


def _routed(cfg, tool: str):
    roster = Roster.coerce(cfg)
    if tool not in roster.routes:
        raise BadInput(f'no model in the roster serves {tool!r}; add it to a model\'s "caps" or list a route')
    return roster


def _check_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise BadInput("text must be a non-empty string")


def _check_labels(labels) -> None:
    if not labels or len(set(labels)) != len(labels) or not all(isinstance(l, str) and l for l in labels):
        raise BadInput("labels must be a non-empty list of unique non-empty strings")
    if len(labels) > 50:
        raise BadInput("at most 50 labels")


def _check_classify_args(labels, descriptions) -> None:
    _check_labels(labels)
    if descriptions is not None and (not isinstance(descriptions, dict) or not set(descriptions) <= set(labels)
                                     or not all(isinstance(v, str) for v in descriptions.values())):
        raise BadInput("descriptions must map labels from the label list to short strings")


def _check_tag_args(labels, max_labels) -> None:
    _check_labels(labels)
    if not isinstance(max_labels, int) or not 1 <= max_labels <= len(labels):
        raise BadInput("max_labels must be an integer between 1 and the number of labels")


def classify(cfg, text: str, labels: list[str], hint: str = "", sensitive: bool = True,
             descriptions: dict | None = None) -> dict:
    _check_text(text)
    _check_classify_args(labels, descriptions)
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


def tag(cfg, text: str, labels: list[str], max_labels: int = 3, hint: str = "", sensitive: bool = True) -> dict:
    """Zero or more of the given labels (at most max_labels). Like classify, but the text may fit several."""
    _check_text(text)
    _check_tag_args(labels, max_labels)
    schema = {"type": "object", "properties": {"labels": {"type": "array", "items": {"type": "string", "enum": list(labels)},
                                                          "uniqueItems": True, "maxItems": max_labels}},
              "required": ["labels"], "additionalProperties": False}
    task = (f"Pick every label that clearly applies to the data, at most {max_labels}, from: {', '.join(labels)}. "
            "Pick none if none applies." + (f" {hint}" if hint else ""))
    cfg = _routed(cfg, "tag")
    return _go(cfg, "tag", lambda order=None: (task, schema), text, "tags", None, sensitive)


MAX_BATCH = 50          # items per batch call
CHUNK_CHARS = 8000      # about 2000 tokens: fits the default 4096-token context with the prompt
MAX_CHUNKS = 4          # a longer text is cut after this many chunks and marked truncated
# after one of these the rest of the batch would fail the same way (or hammer a limit), so it is not attempted
BATCH_STOP = (MinionDown, RateLimited, PrivacyRefused, BadConfig)


def chunks(text: str) -> tuple[list[str], bool]:
    """Split a long text at paragraph breaks into pieces of at most CHUNK_CHARS (a longer paragraph is cut at
    a space). Returns (at most MAX_CHUNKS pieces, whether anything was dropped)."""
    if len(text) <= CHUNK_CHARS:
        return [text], False
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > CHUNK_CHARS:
            cut = para.rfind(" ", 0, CHUNK_CHARS)
            cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:cut])
            para = para[cut:].lstrip()
        if cur and len(cur) + 2 + len(para) > CHUNK_CHARS:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    out.append(cur)
    out = [c for c in out if c.strip()]
    return out[:MAX_CHUNKS], len(out) > MAX_CHUNKS


def _check_items(items) -> list[tuple[str, str]]:
    if not isinstance(items, list) or not items:
        raise BadInput("items must be a non-empty list of {id, text} objects")
    if len(items) > MAX_BATCH:
        raise BadInput(f"at most {MAX_BATCH} items per batch, got {len(items)}")
    if not all(isinstance(i, dict) and isinstance(i.get("id"), str) and i["id"] and isinstance(i.get("text"), str)
               for i in items):
        raise BadInput("every item needs a non-empty string id and a string text")
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        raise BadInput("item ids must be unique")
    return [(i["id"], i["text"]) for i in items]


def _error(e: MinionError, prefix: str = "") -> dict:
    err = {"code": e.code, "message": prefix + str(e)}
    if isinstance(e, RateLimited) and e.retry_after is not None:
        err["retry_after"] = round(e.retry_after, 1)
    return err


def _batch(items: list, one) -> dict:
    """one(text) -> result for each item, one after another (rate limits and model swaps stay as for single
    calls). An item's error is reported with that item; an error in BATCH_STOP also marks the rest not attempted."""
    results, stop = [], None
    for iid, text in items:
        if stop is not None:
            results.append({"id": iid, "error": _error(stop, "not attempted: ")})
            continue
        try:
            if isinstance(text, MinionError):   # a file that could not be read
                raise text
            results.append({"id": iid, **one(text)})
        except MinionError as e:
            results.append({"id": iid, "error": _error(e)})
            if isinstance(e, BATCH_STOP):
                stop = e
    ok = sum("error" not in r for r in results)
    return {"results": results, "ok": ok, "failed": len(results) - ok}


def _over_chunks(text: str, call, combine) -> dict:
    pieces, truncated = chunks(text)
    if len(pieces) == 1 and not truncated:
        return call(text)
    out = combine([call(p) for p in pieces])
    out["chunks"] = len(pieces)
    if truncated:
        out["truncated"] = True
    return out


def classify_batch(cfg, items: list, labels: list[str], hint: str = "", sensitive: bool = True,
                   descriptions: dict | None = None) -> dict:
    """classify for each {id, text}. A long text is classified per chunk; the chunks' majority label wins,
    and a tie is MINION_NO_QUORUM for that item."""
    pairs = _check_items(items)
    _check_classify_args(labels, descriptions)
    roster = Roster.coerce(cfg)

    def combine(outs):
        votes = Counter(o["label"] for o in outs).most_common()
        if len(votes) > 1 and votes[0][1] == votes[1][1]:
            raise NoQuorum(f"chunks of the text disagree: {[o['label'] for o in outs]}")
        return {"label": votes[0][0]}

    call = lambda t: classify(roster, t, labels, hint, sensitive, descriptions)
    return _batch(pairs, lambda text: _over_chunks(text, call, combine))


def tag_batch(cfg, items: list, labels: list[str], max_labels: int = 3, hint: str = "", sensitive: bool = True) -> dict:
    """tag for each {id, text}. A long text is tagged per chunk; labels are ranked by how many chunks chose
    them (then first seen) and cut to max_labels."""
    pairs = _check_items(items)
    _check_tag_args(labels, max_labels)
    roster = _routed(cfg, "tag")

    def combine(outs):
        seen = [l for o in outs for l in o["labels"]]
        count = Counter(seen)
        return {"labels": sorted(count, key=lambda l: (-count[l], seen.index(l)))[:max_labels]}

    call = lambda t: tag(roster, t, labels, max_labels, hint, sensitive)
    return _batch(pairs, lambda text: _over_chunks(text, call, combine))


SENSITIVE_KINDS = ["name", "email", "phone", "id", "address", "secret", "other"]
# rules first: patterns that need no model. The model adds what patterns cannot see (names, addresses).
SENSITIVE_RULES = (("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
                   ("secret", re.compile(r"\b(?:sk|pk|ghp|gho|xox[bap]|AKIA)[-_A-Za-z0-9]{12,}\b")),
                   ("phone", re.compile(r"(?<!\d)\+?\d[\d ()-]{7,}\d(?!\d)")),
                   ("id", re.compile(r"(?<!\d)\d{9,}(?!\d)")))


def find_sensitive(cfg, text: str, sensitive: bool = True) -> dict:
    """Spans of personal or secret-looking data, for screening text before it leaves the machine. Patterns plus a
    model; every span the model names must occur verbatim in the text or the call fails."""
    _check_text(text)
    schema = {"type": "object", "properties": {"spans": {"type": "array", "items": {
        "type": "object", "properties": {"text": {"type": "string", "minLength": 1}, "kind": {"type": "string", "enum": SENSITIVE_KINDS}},
        "required": ["text", "kind"], "additionalProperties": False}}}, "required": ["spans"], "additionalProperties": False}
    task = ("List every span in the data that is personal or secret: person names, email addresses, phone numbers, "
            "identifiers, postal addresses, passwords, keys, tokens. Copy each span exactly as written. "
            "List nothing that is not in the data.")
    cfg = _routed(cfg, "find_sensitive")
    out = _go(cfg, "find_sensitive", lambda order=None: (task, schema), text, "spans", None, sensitive)
    spans = {(m.group(0), kind): "rule" for kind, rx in SENSITIVE_RULES for m in rx.finditer(text)}
    for s in out["spans"]:
        if s["text"] not in text:
            raise SchemaError(f"model named a span that is not in the text: {s['text'][:60]!r}", str(out)[:200])
        spans.setdefault((s["text"], s["kind"]), "model")
    return {"found": bool(spans), "spans": [{"text": t, "kind": k, "by": by} for (t, k), by in spans.items()]}


SAME_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["same", "different", "partly"]},
                                                "evidence": {"type": "string"}},
               "required": ["verdict", "evidence"], "additionalProperties": False}


def _squash(s: str) -> str:
    return " ".join(s.split()).lower()


def same(cfg, a: str, b: str, hint: str = "", sensitive: bool = True) -> dict:
    """Do two texts say the same thing? same / different / partly, plus evidence quoted from one of the texts."""
    _check_text(a)
    _check_text(b)
    clean = lambda t: t.replace("=====", "== ==")
    text = f"=====A=====\n{clean(a)}\n=====B=====\n{clean(b)}"
    task = ("The data holds two texts, A and B. Say whether they state the same facts: same, different, or partly. "
            "For different or partly, put in evidence one short phrase copied exactly from A or B that shows the "
            "difference; for same, leave evidence empty." + (f" {hint}" if hint else ""))
    cfg = _routed(cfg, "same")
    out = _go(cfg, "same", lambda order=None: (task, SAME_SCHEMA), text, "comparison", None, sensitive)
    ev = out["evidence"].strip()
    if out["verdict"] == "same":
        return {"verdict": "same", "evidence": ""}
    if not ev or not any(_squash(ev) in _squash(t) for t in (a, b)):
        raise SchemaError("model gave no evidence that appears in either text", str(out)[:200])
    return {"verdict": out["verdict"], "evidence": ev}


VERDICTS = ["supported", "contradicted", "unclear"]
VERDICT_MEANINGS = {"supported": "The source states what the claim says, with the same numbers, names and dates.",
                    "contradicted": "The source states something that conflicts with the claim.",
                    "unclear": "The source does not say enough to support or contradict the claim."}
MIN_QUOTE = 3   # a quote shorter than this (non-space characters) proves nothing


def _quote_found(quote: str, source: str) -> bool:
    q = _squash(quote)
    return len(q.replace(" ", "")) >= MIN_QUOTE and q in _squash(source)


def grounded(cfg, claim: str, source: str, hint: str = "", sensitive: bool = True) -> dict:
    """Is the claim supported by the source, contradicted, or unclear? A supported or contradicted verdict
    stands only with a quote that occurs in the source (whitespace and case normalised); otherwise it is
    downgraded to unclear. Decision models may vote on the verdict; they give no quote."""
    _check_text(claim)
    _check_text(source)
    clean = lambda t: t.replace("=====", "== ==")
    text = f"=====CLAIM=====\n{clean(claim)}\n=====SOURCE=====\n{clean(source)}"
    meaning = "\n".join(f"- {v}: {d}" for v, d in VERDICT_MEANINGS.items())
    intro = ("The data holds a CLAIM and a SOURCE. Check the claim against the SOURCE only, not against what you "
             "know. Every number, name and date in the claim must match the source for supported.")

    def build(order=None):
        schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": list(order or VERDICTS)},
                                                   "quote": {"type": "string"}},
                  "required": ["verdict", "quote"], "additionalProperties": False}
        return (f"{intro} Verdicts:\n{meaning}\nFor supported or contradicted, put in quote the shortest passage "
                "copied exactly from the SOURCE that shows it; for unclear, leave quote empty."
                + (f" {hint}" if hint else "")), schema

    roster = _routed(cfg, "grounded")

    def after(out, meta):
        verdict = out["verdict"]
        short = meta.get("shortcut") or {}
        conf = {"confidence": short["confidence"]} if short and not short["escalated"] else {}
        if verdict == "unclear":
            return {"verdict": "unclear", "quote": "", **conf}
        if "quote" in out:
            quote = out["quote"].strip()
        else:   # only decision models gave the verdict: a generative model on the route supplies the quote
            quote = _fetch_quote(roster, text, intro, verdict, sensitive, meta)
        if not _quote_found(quote, source):   # never trusted: a verdict without a real quote is not a verdict
            meta["downgraded"] = verdict
            return {"verdict": "unclear", "quote": "", "reason": f"no quote from the source backs {verdict!r}"
                    + (f"; the model quoted {quote[:60]!r}" if quote else "")}
        return {"verdict": verdict, "quote": quote, **conf}

    return _go(roster, "grounded", build, text, "grounding", list(VERDICTS), sensitive, VERDICT_MEANINGS, after)


def _fetch_quote(roster, text: str, intro: str, verdict: str, sensitive: bool, meta: dict) -> str:
    """The quote for a verdict a decision model gave. No generative model on the route, or one that fails the
    schema, gives an empty quote (so the verdict becomes unclear). Hard failures propagate."""
    route = router._gate(roster.route("grounded"), "grounded", sensitive)
    m = next((c for c in route.fast + route.fallback if c.kind != "decision"), None)
    if m is None:
        return ""
    schema = {"type": "object", "properties": {"quote": {"type": "string"}}, "required": ["quote"],
              "additionalProperties": False}
    task = (f"{intro} The claim was judged {verdict}. Copy into quote the shortest passage from the SOURCE that "
            "shows it, exactly as written. Leave quote empty if no passage shows it.")
    meta["quote_by"] = m.name
    try:
        return engine.run(m, task, text, schema, "quote")["quote"].strip()
    except SchemaError:
        meta["schema_failures"].append(m.name)
        return ""


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
    from .config import apply_config_file
    apply_config_file()   # the log path may come from the config file
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


def load_model(cfg, name: str) -> dict:
    """Load a managed local model through its runtime, if the quiet gate allows it."""
    return manage.load(Roster.coerce(cfg), name)


def unload_model(cfg, name: str) -> dict:
    """Unload a managed model that minions itself loaded."""
    return manage.unload(Roster.coerce(cfg), name)
