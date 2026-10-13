"""Evaluate minions on the invented public items in evals/dataset.json.

Each model is tried alone (sensitive=false, so cloud models are included) and, with --quorum, the whole
classify route is tried together. Provider rate limits are waited out and counted, never scored as wrong answers.

usage: MINION_ROSTER=<roster.json> eval.py [--models a,b] [--max-calls name=N ...] [--quorum] [--out FILE]
Cloud keys come from the environment. Nothing in the dataset is private.
"""
import argparse
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from minions import tools  # noqa: E402
from minions.config import ASK, TOOLS, Roster, Route  # noqa: E402
from minions.errors import MinionError, RateLimited, SchemaError  # noqa: E402

SENS = False   # set by --sensitive: calls then go through the privacy gate and only local models answer
DATA = json.loads((Path(__file__).resolve().parent.parent / "evals" / "dataset.json").read_text())
REFUSAL = re.compile(r"cannot confirm|can't confirm|not aware|no (such|record|evidence|record of)|does not exist|doesn't exist|"
                     r"unable to (find|verify|confirm)|couldn't find|could not find|not (a )?(real|recognized|known)|"
                     r"i (do not|don't) (know|have)|no information|fictional|made[- ]up|unverified|cannot verify", re.I)


def single(roster: Roster, name: str) -> Roster:
    m = roster.models[name]
    routes = {t: Route((m,), None, 1) for t in TOOLS if m.serves(t)}
    if m.serves(ASK):
        routes[ASK] = Route((m,), None, 1)
    return Roster({name: m}, routes, roster.max_file_bytes)


def numbers(s: str) -> set:
    return set(re.findall(r"\d+(?:[.,]\d+)?", s))


def norm(v):
    return v.strip().lower() if isinstance(v, str) else v


def items(model: Roster, name: str, quorum_only: bool = False):
    """Yield (task, id, callable returning (correct, detail))."""
    m = next(iter(model.models.values()))
    if m.serves("classify") or quorum_only:
        for i, c in enumerate(DATA["classify"]):
            def f(c=c):
                out = tools.classify(model, c["text"], c["labels"], sensitive=SENS)
                return out["label"] == c["expected"], out["label"]
            yield f"classify/{c['tag']}", f"c{i}", f
    if quorum_only:
        return
    if m.serves("extract"):
        for i, e in enumerate(DATA["extract"]):
            def f(e=e):
                out = tools.extract(model, e["text"], e["schema"], sensitive=SENS)
                ok = all(norm(out.get(k)) == norm(v) if not isinstance(v, float) else abs(float(out.get(k, 1e9)) - v) < 1e-6
                         for k, v in e["expected"].items())
                return ok, out
            yield "extract", f"e{i}", f
    if m.serves("summarize"):
        for i, s in enumerate(DATA["summarize"]):
            def f(s=s):
                out = tools.summarize(model, s["text"], s["max_words"], sensitive=SENS)["summary"]
                invented = numbers(out) - numbers(s["text"])
                ok = (not invented and all(w in out.lower() for w in s["must"]) and len(out.split()) <= s["max_words"] * 1.3)
                return ok, out
            yield "summarize", f"s{i}", f
    if m.serves(ASK):
        for i, a in enumerate(DATA["ask"]):
            def f(a=a):
                out = tools.ask(model, a["q"], sensitive=SENS)
                said_no = bool(REFUSAL.search(out["answer"]))
                ok = said_no if a["kind"] == "false" else (not said_no or all(w in out["answer"].lower() for w in a["must"])) and all(w in out["answer"].lower() for w in a["must"])
                return ok, out["answer"]
            yield f"ask/{a['kind']}-premise", f"a{i}", f


def run_model(roster: Roster, name: str, cap: int | None, quorum_only: bool = False, only: str | None = None) -> dict:
    model = roster if quorum_only else single(roster, name)
    stats = {"name": name, "tasks": {}, "latency": [], "rate_limited": 0, "schema_fail": 0, "errors": [], "raw": []}
    calls = 0
    for task, iid, fn in items(model, name, quorum_only):
        if only and only not in task:
            continue
        if cap is not None and calls >= cap:
            break
        calls += 1
        for attempt in range(4):
            t = time.time()
            try:
                ok, detail = fn()
                stats["latency"].append(time.time() - t)
                break
            except RateLimited as e:
                stats["rate_limited"] += 1
                time.sleep(min(e.retry_after or 20, 65))
                ok, detail = None, "rate limited"
            except SchemaError as e:
                stats["schema_fail"] += 1
                ok, detail = False, f"schema: {str(e)[:100]}"
                break
            except MinionError as e:
                stats["errors"].append(str(e)[:120])
                ok, detail = None, str(e)[:120]
                break
        if ok is None:
            continue   # not answered: reported as "no answer", not as wrong
        stats["tasks"].setdefault(task, []).append(bool(ok))
        stats["raw"].append({"task": task, "id": iid, "ok": bool(ok), "out": detail})
    return stats


def cell(v):
    return f"{sum(v)}/{len(v)}" if v else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models"), ap.add_argument("--max-calls", action="append", default=[])
    ap.add_argument("--quorum", action="store_true"), ap.add_argument("--only-quorum", action="store_true")
    ap.add_argument("--only", help="run only items whose task name contains this text, e.g. hard")
    ap.add_argument("--sensitive", action="store_true", help="mark every call sensitive (local models only)")
    ap.add_argument("--out")
    a = ap.parse_args()
    global SENS
    SENS = a.sensitive
    roster = Roster.from_env()
    names = a.models.split(",") if a.models else list(roster.models)
    caps = {k: int(v) for k, v in (x.split("=") for x in a.max_calls)}
    results = []
    if not a.only_quorum:
        with ThreadPoolExecutor(len(names)) as ex:   # one thread per model; a model's own calls stay sequential
            results = list(ex.map(lambda n: run_model(roster, n, caps.get(n), only=a.only), names))
    if a.quorum or a.only_quorum:
        results.append(run_model(roster, "QUORUM(classify)", None, quorum_only=True, only=a.only))
    tasks = sorted({t for r in results for t in r["tasks"]})
    print("| model | " + " | ".join(tasks) + " | schema fails | 429s | p50 s | no answer |")
    print("|---|" + "---|" * (len(tasks) + 4))
    for r in results:
        p50 = f"{statistics.median(r['latency']):.1f}" if r["latency"] else "-"
        print(f"| {r['name']} | " + " | ".join(cell(r["tasks"].get(t)) for t in tasks) +
              f" | {r['schema_fail']} | {r['rate_limited']} | {p50} | {len(r['errors'])} |")
    for r in results:
        for e in sorted(set(r["errors"])):
            print(f"  {r['name']}: {e}")
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    main()
