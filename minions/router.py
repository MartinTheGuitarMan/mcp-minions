"""Per-tool routing: fast models (quorum for classify) -> judge only on disagreement or schema failure.
Hard failures (MINION_DOWN, backend errors) always propagate; the judge never papers over them."""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import engine, judge as judge_mod
from .config import Roster
from .errors import NoQuorum, SchemaError


def _rotate(labels: list, i: int) -> list:
    k = i % len(labels)
    return labels[k:] + labels[:k]


def run(roster: Roster, tool: str, build, text: str, name: str, labels: list | None = None) -> tuple[dict, dict]:
    """build(order) -> (task, schema); order is a label ordering (classify only) or None."""
    route = roster.route(tool)
    task, schema = build(None)

    # Each run is (model, label order). Small models are position-biased, so label order is varied:
    # one rotation per model when there are several, or original + reversed when a single model has a judge.
    if tool == "classify":
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
            "debias": len(route.fast) == 1 and len(runs) == 2}

    def one(m, order):
        t, sch = build(order)
        try:
            return m, engine.run(m, t, text, sch, name), None
        except SchemaError as e:
            return m, None, e

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
            done = [x for part in ex.map(run_group, groups.values()) for x in part]  # hard errors propagate
    outs = [o for _, o in sorted(done, key=lambda t: t[0])]
    valid = [(m, r) for m, r, e in outs if r is not None]
    failures = [(m, e) for m, r, e in outs if e is not None]
    meta["schema_failures"] = [m.name for m, _ in failures]

    if tool == "classify":
        votes = Counter(r["label"] for _, r in valid)
        meta["votes"] = dict(votes)
        ranked = votes.most_common()
        if ranked and ranked[0][1] >= quorum and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            return {"label": ranked[0][0]}, meta
        if route.judge is None:
            if not votes:
                raise failures[0][1]   # every fast model failed schema: report that, not a quorum miss
            raise NoQuorum(f"no judge configured and fast models did not reach quorum {quorum}: "
                           f"votes={dict(votes)} schema_failures={meta['schema_failures']}")
        out, _ = judge_mod.verify_classification(route.judge, task, text, labels, dict(votes))
        meta["judge"] = route.judge.name
        meta["judge_verdict"] = out["verdict"]
        return {"label": out["label"]}, meta

    if valid:
        return valid[0][1], meta
    first = failures[0][1]
    if route.judge is None:
        raise first
    meta["judge"] = route.judge.name
    return judge_mod.repair(route.judge, task, text, schema, first.raw, str(first)), meta
