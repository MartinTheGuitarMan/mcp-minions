"""Per-tool routing: fast models (quorum for classify and grounded) -> judge only on disagreement or schema failure.
Hard failures (MINION_DOWN, backend errors) always propagate; the judge never papers over them.
Every call to a model with "manage": true goes through manage.ready first: it is loaded if needed, behind the quiet gate."""
from __future__ import annotations

import contextvars
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from . import decision, defend, engine, judge as judge_mod, manage
from .config import VOTING_TOOLS, Roster
from .errors import Busy, NoQuorum, PrivacyRefused, RateLimited, SchemaError


# Soft failures: the model gave no answer and nothing was tried on it, so the route may pass to the next model.
# A rate limit and a refusal by the quiet gate (MINION_BUSY) are the only ones. MINION_LOAD_FAILED is not: a load
# that was tried and failed is a hard failure.
SOFT = (RateLimited, Busy)


def _skipped(meta: dict, name: str, e: Exception) -> None:
    meta["busy" if isinstance(e, Busy) else "rate_limited"].append(name)


VOTE_KEY = {"classify": "label", "grounded": "verdict"}   # the field the fast models vote on
assert set(VOTE_KEY) == set(VOTING_TOOLS)


def _rotate(labels: list, i: int) -> list:
    k = i % len(labels)
    return labels[k:] + labels[:k]


def _gate(route, tool: str, sensitive: bool):
    """Privacy gate. A sensitive call never reaches a cloud model: those are removed from the route, and a
    route left with no local fast model is refused, not quietly answered somewhere else."""
    if not sensitive:
        return route
    fast = tuple(m for m in route.fast if not m.cloud)
    if not fast:
        raise PrivacyRefused(f"{tool}: every fast model on this route is cloud and the call is sensitive; "
                             f"pass sensitive=false only for public text")
    judge = route.judge if route.judge is not None and not route.judge.cloud else None
    shortcut = route.shortcut if route.shortcut and not route.shortcut[0].cloud else ()
    return replace(route, fast=fast, judge=judge, quorum=min(route.quorum, len(fast)), shortcut=shortcut,
                   debias=route.debias and len(fast) == 1 and (judge is not None or route.judge is None),
                   fallback=tuple(m for m in route.fallback if not m.cloud))


def run(roster: Roster, tool: str, build, text: str, name: str, labels: list | None = None,
        sensitive: bool = True, descriptions: dict | None = None, check=None) -> tuple[dict, dict]:
    """build(order) -> (task, schema); order is a label ordering (voting tools only) or None.
    check(obj) may raise a SchemaError for a schema-valid reply (extract's value check); the fast model's answer
    then counts as a schema failure and the judge's repair must pass it too.
    sensitive=True (the default) keeps the call on local models. For a voting tool, labels are the values
    voted on (the classify labels, or the grounded verdicts)."""
    route = _gate(roster.route(tool), tool, sensitive)
    task, schema = build(None)

    # Each run is (model, label order). Small models are position-biased, so label order is varied:
    # one rotation per model when there are several, or original + reversed when a single model has a judge.
    key = VOTE_KEY.get(tool)
    if key:
        if len(route.fast) > 1:
            runs = [(m, _rotate(labels, i)) for i, m in enumerate(route.fast)]
        elif route.debias and len(labels) > 1:
            runs = [(route.fast[0], list(labels)), (route.fast[0], list(reversed(labels)))]
        else:
            runs = [(route.fast[0], list(labels))]
        quorum = route.quorum if len(route.fast) > 1 else (2 if len(runs) == 2 else 1)
    else:
        runs, quorum = [(route.fast[0], None)], 1
    meta = {"tool": tool, "fast": [m.name for m, _ in runs], "judge": None, "votes": {}, "schema_failures": [],
            "debias": len(route.fast) == 1 and len(runs) == 2, "rate_limited": [], "busy": [], "failover": []}

    if route.shortcut and tool == "classify":   # screen the text first: instruction-like segments are dropped before anything classifies it
        text, removed = defend.strip(route.shortcut[0], text)
        if removed:
            meta["injection_removed"] = removed
    if route.shortcut:   # a confident decision model answers alone; otherwise the usual voters and judge follow
        sm, threshold = route.shortcut
        try:
            label, conf = decision.run(sm, task, text, labels, descriptions)
            meta["shortcut"] = {"model": sm.name, "confidence": conf, "threshold": threshold,
                                "escalated": conf is None or conf < threshold}
            if not meta["shortcut"]["escalated"]:
                meta["fast"] = []   # nobody else was called
                return {key: label}, meta
        except SOFT as e:
            _skipped(meta, sm.name, e)
        except SchemaError:
            meta["schema_failures"].append(sm.name)

    def one(m, order):
        t, sch = build(order)
        # Rate limits and busy refusals are the only failures a route may fail over on; every other hard failure
        # propagates. If every candidate is skipped, the last such error is raised.
        # A fallback never duplicates a model that already votes in this route.
        tried = [m] + [f for f in route.fallback if f is not m and f not in route.fast]
        limited = None
        for cand in tried:
            try:
                with manage.ready(roster, cand):   # a managed model is loaded first; the gate may refuse (MINION_BUSY)
                    if cand.kind == "decision":
                        out = {key: decision.run(cand, t, text, order, descriptions)[0]}, None
                    else:
                        out = engine.run(cand, t, text, sch, name, check), None
            except SchemaError as e:
                out = None, e
            except SOFT as e:
                _skipped(meta, cand.name, e)
                limited = e
                continue
            if cand is not m:
                meta["failover"].append([m.name, cand.name])
            return (cand, *out)
        return m, None, limited

    # Models on the same server run one after another (LM Studio swaps models in and out on demand, so
    # concurrent requests to two different models on one server evict each other mid-request). Different
    # servers run in parallel.
    groups = defaultdict(list)
    for i, (m, order) in enumerate(runs):
        groups[(m.backend, m.base_url)].append((i, m, order))

    def run_group(items):
        return [(i, one(m, order)) for i, m, order in items]

    if len(groups) == 1:
        done = run_group(next(iter(groups.values())))
    else:
        with ThreadPoolExecutor(len(groups)) as ex:
            # each worker thread runs in a copy of this context so its token counts reach the call's tally
            futures = [ex.submit(contextvars.copy_context().run, run_group, g) for g in groups.values()]
            done = [x for f in futures for x in f.result()]  # hard errors propagate
    outs = [o for _, o in sorted(done, key=lambda t: t[0])]
    valid = [(m, r) for m, r, e in outs if r is not None]
    failures = [(m, e) for m, r, e in outs if e is not None]
    meta["schema_failures"] += [m.name for m, e in failures if isinstance(e, SchemaError)]
    kinds = [e.code for _, e in failures if isinstance(e, SchemaError) and e.code != SchemaError.code]
    if kinds:
        meta["check_failures"] = kinds   # the value-check kinds (MINION_EMPTY, ...), so the report can count them

    if key:
        votes = Counter(r[key] for _, r in valid)
        meta["votes"] = dict(votes)
        ranked = votes.most_common()
        if ranked and ranked[0][1] >= quorum and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            if tool == "classify":
                return {"label": ranked[0][0]}, meta
            # a generative winner keeps its other fields (the quote); decision models give the verdict alone
            return next((r for _, r in valid if r[key] == ranked[0][0] and len(r) > 1), {key: ranked[0][0]}), meta
        if not votes and failures and all(isinstance(e, SOFT) for _, e in failures):
            raise failures[-1][1]   # nobody answered (busy or rate limited): the judge has nothing to verify
        if route.judge is None:
            if not votes:
                raise failures[0][1]   # every fast model failed (schema, rate limit or busy): report that, not a quorum miss
            raise NoQuorum(f"no judge configured and fast models did not reach quorum {quorum}: "
                           f"votes={dict(votes)} schema_failures={meta['schema_failures']}")
        meta["judge"] = route.judge.name
        if tool == "grounded":
            with manage.ready(roster, route.judge):
                out = judge_mod.verify_grounding(route.judge, task, text, labels, dict(votes))
            meta["judge_verdict"] = out["check"]
            return {"verdict": out["verdict"], "quote": out["quote"]}, meta
        with manage.ready(roster, route.judge):
            out, _ = judge_mod.verify_classification(route.judge, task, text, labels, dict(votes))
        meta["judge_verdict"] = out["verdict"]
        return {"label": out["label"]}, meta

    if valid:
        return valid[0][1], meta
    first = failures[0][1]
    if isinstance(first, SOFT) or route.judge is None:   # the judge repairs bad output, not a missing answer
        raise first
    meta["judge"] = route.judge.name
    with manage.ready(roster, route.judge):
        return judge_mod.repair(route.judge, task, text, schema, first.raw, str(first), check), meta
