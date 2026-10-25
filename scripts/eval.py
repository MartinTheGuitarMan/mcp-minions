"""Evaluate minions on the invented public items in evals/dataset.json.

Each model is tried alone (sensitive=false, so cloud models are included) and, with --quorum, the whole
classify route is tried together. Provider rate limits are waited out and counted, never scored as wrong answers.

usage: MINION_ROSTER=<roster.json> eval.py [--models a,b] [--max-calls name=N ...] [--quorum] [--out FILE]
       MINION_ROSTER=<roster.json> eval.py --confidence [--data evals/dataset_large.json --split dev] [--out FILE]
--confidence asks each decision model (or those in --models) every classify item once, as a shortcut would,
and prints confidence-versus-correct pairs plus, per threshold, how many calls would be answered alone and how
many of those are right. Use it to tune min_confidence on one split and check it on the other.
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
from minions import decision, tools  # noqa: E402
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
                try:
                    out = tools.extract(model, e["text"], e["schema"], sensitive=SENS)
                except SchemaError as x:
                    if "expected_error" in e:   # the right answer is a clear error, not a guessed value
                        return x.code in e["expected_error"], x.code
                    raise
                if "expected_error" in e:
                    return False, out
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
    if m.serves("grounded"):
        for i, g in enumerate(DATA.get("grounded", [])):
            def f(g=g):
                out = tools.grounded(model, g["claim"], g["source"], sensitive=SENS)
                return out["verdict"] == g["expected"], out
            yield f"grounded/{g['tag']}", f"g{i}", f
    if m.serves("tag") and DATA.get("tag"):
        batch = {}   # the tag items go through tag_batch in one call (same labels for all); each item is scored

        def tagged(i):
            if not batch:
                t = DATA["tag"]
                batch.update({r["id"]: r for r in tools.tag_batch(
                    model, [{"id": str(k), "text": x["text"]} for k, x in enumerate(t)], t[0]["labels"],
                    t[0]["max_labels"], sensitive=SENS)["results"]})
            r = batch[str(i)]
            if "error" in r:
                raise SchemaError(r["error"]["message"]) if r["error"]["code"] == "MINION_SCHEMA" else MinionError(r["error"]["message"])
            return r["labels"]
        for i, t in enumerate(DATA["tag"]):
            def f(i=i, t=t):
                got = tagged(i)
                return set(t["must"]) <= set(got) and not set(t["never"]) & set(got), got
            yield "tag_batch", f"t{i}", f
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


THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99)


def confidence_pairs(roster: Roster, name: str, items: list, cap: int | None) -> list:
    """(confidence, correct, label, expected, id) for each classify item, as the shortcut sees it: the same task
    text, no voters. The injection defence that runs before a shortcut is not applied here."""
    m, pairs = roster.models[name], []
    for i, c in enumerate(items[:cap]):
        task = tools.classify_build(c["labels"])(None)[0]
        for attempt in range(4):
            try:
                label, conf = decision.run(m, task, c["text"], c["labels"])
                break
            except RateLimited as e:
                time.sleep(min(e.retry_after or 20, 65))
            except MinionError as e:
                label, conf = None, None
                print(f"  {name} c{i}: {str(e)[:100]}", file=sys.stderr)
                break
        else:
            continue
        if label is not None:
            pairs.append({"confidence": conf, "correct": label == c["expected"], "label": label,
                          "expected": c["expected"], "id": f"c{i}"})
    return pairs


def print_confidence(name: str, pairs: list) -> None:
    print(f"\n## {name}: {len(pairs)} answers, {sum(p['correct'] for p in pairs)} correct")
    print("confidence\tcorrect\tlabel\texpected\tid")
    for p in sorted(pairs, key=lambda p: (p["confidence"] is None, p["confidence"] or 0)):
        conf = "-" if p["confidence"] is None else f"{p['confidence']:.4f}"
        print(f"{conf}\t{int(p['correct'])}\t{p['label']}\t{p['expected']}\t{p['id']}")
    print("\n| min_confidence | answered alone | of those correct | accuracy | escalated |")
    print("|---|---|---|---|---|")
    for t in THRESHOLDS:
        alone = [p for p in pairs if p["confidence"] is not None and p["confidence"] >= t]
        acc = f"{sum(p['correct'] for p in alone) / len(alone):.3f}" if alone else "-"
        print(f"| {t} | {len(alone)} | {sum(p['correct'] for p in alone)} | {acc} | {len(pairs) - len(alone)} |")


def cell(v):
    return f"{sum(v)}/{len(v)}" if v else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models"), ap.add_argument("--max-calls", action="append", default=[])
    ap.add_argument("--quorum", action="store_true"), ap.add_argument("--only-quorum", action="store_true")
    ap.add_argument("--only", help="run only items whose task name contains this text, e.g. hard")
    ap.add_argument("--sensitive", action="store_true", help="mark every call sensitive (local models only)")
    ap.add_argument("--confidence", action="store_true",
                    help="print decision models' confidence-versus-correct pairs on the classify items")
    ap.add_argument("--data", help="dataset file for --confidence (default evals/dataset.json)")
    ap.add_argument("--split", help="only items with this split, e.g. dev or test (dataset_large.json)")
    ap.add_argument("--out")
    a = ap.parse_args()
    global SENS
    SENS = a.sensitive
    roster = Roster.from_env()
    names = a.models.split(",") if a.models else list(roster.models)
    caps = {k: int(v) for k, v in (x.split("=") for x in a.max_calls)}
    if a.confidence:
        data = json.loads(Path(a.data).read_text()) if a.data else DATA
        cls = [c for c in data["classify"] if not a.split or c.get("split") == a.split]
        names = [n for n in names if roster.models[n].kind == "decision"]
        if not names:
            sys.exit("no decision model in the roster (or in --models)")
        out = {n: confidence_pairs(roster, n, cls, caps.get(n)) for n in names}
        for n, pairs in out.items():
            print_confidence(n, pairs)
        if a.out:
            Path(a.out).write_text(json.dumps(out, indent=1))
        return
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
