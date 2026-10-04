"""Per-tool routing: fast models (quorum for classify) -> judge only on disagreement or schema failure.
Hard failures (MINION_DOWN, backend errors) always propagate; the judge never papers over them."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import engine, judge as judge_mod
from .config import Roster
from .errors import NoQuorum, SchemaError


def run(roster: Roster, tool: str, task: str, text: str, schema: dict, name: str,
        labels: list | None = None) -> tuple[dict, dict]:
    route = roster.route(tool)
    meta = {"tool": tool, "fast": [m.name for m in route.fast], "judge": None, "votes": {}, "schema_failures": []}

    def one(m):
        try:
            return m, engine.run(m, task, text, schema, name), None
        except SchemaError as e:
            return m, None, e

    if len(route.fast) == 1:
        outs = [one(route.fast[0])]
    else:
        with ThreadPoolExecutor(len(route.fast)) as ex:
            outs = list(ex.map(one, route.fast))   # non-schema errors propagate from here
    valid = [(m, r) for m, r, e in outs if r is not None]
    failures = [(m, e) for m, r, e in outs if e is not None]
    meta["schema_failures"] = [m.name for m, _ in failures]

    if tool == "classify":
        votes = Counter(r["label"] for _, r in valid)
        meta["votes"] = dict(votes)
        ranked = votes.most_common()
        if ranked and ranked[0][1] >= route.quorum and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            return {"label": ranked[0][0]}, meta
        if route.judge is None:
            if not votes:
                raise failures[0][1]   # every fast model failed schema: report that, not a quorum miss
            raise NoQuorum(f"no judge configured and fast models did not reach quorum {route.quorum}: "
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
