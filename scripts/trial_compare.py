"""Run one trial set on two code revisions and diff the per-case results.

Plain code, no model of its own (the trials it starts use models, through the existing `scripts/tool_trials.py`), so it
lives here, not in the MCP tool list.

    # diff two result sets that already exist (a .jsonl file, or a folder of <model>.jsonl)
    uv run python scripts/trial_compare.py diff results-old/ results-new/ [--model air-qwen] [--tool check_claims]

    # run the trials at two revisions, one after the other, then diff
    uv run python scripts/trial_compare.py run --old 9f1887c~1 --new HEAD --roster roster.json \\
        --models air-qwen --tools check_claims --repeats 1 --out /somewhere/outside/the/repo [--dry-run]

`run` makes a detached git worktree for each revision in a temp folder, runs that revision's own `scripts/tool_trials.py run`
in it (so the old runner and the old case set are used for the old code), removes the worktree, and diffs. The runs are
never in parallel (memory) and the AC power and memory-pressure waits are the runner's own. The roster file is only read.

A case is keyed by (tool, model, case id). Over its repeats it has `ok` of `n` answers that were scored (a `not_run` record
is left out). Against the old side it is: `worse` (smaller share ok), `better`, `changed` (same share, different mix of
statuses, e.g. wrong against check_failed), `same`, or `only_old` / `only_new` (the case ran on one side only). Exit code 1
when any case is `worse`, so it can gate a change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load(path: str) -> list[dict]:
    p = Path(path).expanduser()
    files = sorted(p.glob("*.jsonl")) if p.is_dir() else [p]
    if not files:
        raise SystemExit(f"{path}: no .jsonl files")
    recs = []
    for f in files:
        for n, raw in enumerate(f.read_text().splitlines(), 1):
            if raw.strip():
                try:
                    recs.append(json.loads(raw))
                except json.JSONDecodeError as e:
                    raise SystemExit(f"{f}: line {n} is not JSON: {e}")
    return recs


def per_case(recs: list[dict], model: str | None = None, tool: str | None = None) -> dict:
    """{(tool, model, case): {"n": scored answers, "ok": ok answers, "mix": Counter of statuses}}"""
    out: dict = {}
    for r in recs:
        if r.get("status") == "not_run" or r.get("case") in (None, "*"):
            continue
        if (model and r.get("model") != model) or (tool and r.get("tool") != tool):
            continue
        d = out.setdefault((r["tool"], r["model"], r["case"]), {"n": 0, "ok": 0, "mix": Counter()})
        d["n"] += 1
        d["ok"] += r["status"] == "ok"
        d["mix"][r["status"] if r["status"] != "check_failed" else f"check_failed:{r.get('code')}"] += 1
    return out


def compare(old: dict, new: dict) -> dict:
    rows = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        if o is None or n is None:
            verdict = "only_new" if o is None else "only_old"
        else:
            so, sn = o["ok"] / o["n"], n["ok"] / n["n"]
            verdict = ("worse" if sn < so - 1e-9 else "better" if sn > so + 1e-9
                       else "changed" if o["mix"] != n["mix"] else "same")
        rows.append({"key": key, "old": o, "new": n, "verdict": verdict})
    totals: dict = defaultdict(lambda: {"old_ok": 0, "old_n": 0, "new_ok": 0, "new_n": 0})
    for key, side in (*((k, "old") for k in old), *((k, "new") for k in new)):
        t = totals[key[:2]]
        d = (old if side == "old" else new)[key]
        t[f"{side}_ok"] += d["ok"]
        t[f"{side}_n"] += d["n"]
    return {"rows": rows, "totals": dict(totals), "counts": dict(Counter(r["verdict"] for r in rows))}


def _mix(d: dict | None) -> str:
    return "-" if d is None else f"{d['ok']}/{d['n']} " + ",".join(f"{k}x{v}" for k, v in sorted(d["mix"].items()))


def render(res: dict, old_revs: set, new_revs: set) -> str:
    lines = [f"old code {sorted(r for r in old_revs if r) or '?'}  ->  new code {sorted(r for r in new_revs if r) or '?'}", ""]
    lines.append("| tool | model | old ok | new ok |")
    lines.append("|---|---|---|---|")
    for (tool, model), t in sorted(res["totals"].items()):
        lines.append(f"| {tool} | {model} | {t['old_ok']}/{t['old_n']} | {t['new_ok']}/{t['new_n']} |")
    lines.append("")
    interesting = [r for r in res["rows"] if r["verdict"] != "same"]
    if not interesting:
        lines.append(f"per case: all {len(res['rows'])} cases the same")
    else:
        lines.append("| tool | model | case | verdict | old | new |")
        lines.append("|---|---|---|---|---|---|")
        for r in interesting:
            lines.append(f"| {r['key'][0]} | {r['key'][1]} | {r['key'][2]} | {r['verdict'].upper()} | {_mix(r['old'])} | {_mix(r['new'])} |")
    lines.append("")
    lines.append("cases: " + ", ".join(f"{k} {v}" for k, v in sorted(res["counts"].items())))
    return "\n".join(lines)


def diff_paths(old_path: str, new_path: str, model: str | None = None, tool: str | None = None) -> tuple[dict, str]:
    ro, rn = load(old_path), load(new_path)
    res = compare(per_case(ro, model, tool), per_case(rn, model, tool))
    return res, render(res, {r.get("rev") for r in ro}, {r.get("rev") for r in rn})


# ---------------------------------------------------------------- run mode
def trial_command(roster: str, models: str, tools: str, repeats: int, out: str) -> list[str]:
    return ["uv", "run", "python", "scripts/tool_trials.py", "run", "--roster", roster, "--models", models,
            "--tools", tools, "--repeats", str(repeats), "--out", out]


def plan(old: str, new: str, workdir: Path, args) -> list[dict]:
    """The steps of a run: for each side a worktree add, the trial command in it, and a worktree remove."""
    steps = []
    for side, rev in (("old", old), ("new", new)):
        wt = workdir / side
        steps.append({"side": side, "rev": rev, "worktree": wt, "out": Path(args.out).expanduser() / side,
                      "add": ["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), rev],
                      "run": trial_command(str(Path(args.roster).expanduser()), args.models, args.tools, args.repeats,
                                           str(Path(args.out).expanduser() / side)),
                      "remove": ["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)]})
    return steps


def do_run(args) -> int:
    out = Path(args.out).expanduser()
    if out.resolve().is_relative_to(REPO):
        print("--out must be outside the repo", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="trial-compare-"))
    steps = plan(args.old, args.new, work, args)
    try:
        for s in steps:
            print(f"[{s['side']}] {s['rev']}: " + " ".join(s["run"]), file=sys.stderr)
            if args.dry_run:
                continue
            subprocess.run(s["add"], check=True)
            try:
                r = subprocess.run(s["run"], cwd=s["worktree"])
                if r.returncode:
                    print(f"[{s['side']}] trial run exited {r.returncode}", file=sys.stderr)
                    return r.returncode
            finally:
                subprocess.run(s["remove"], check=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.dry_run:
        return 0
    return diff_cli(str(out / "old"), str(out / "new"), args.models if "," not in args.models else None, None)


def diff_cli(old: str, new: str, model: str | None, tool: str | None) -> int:
    res, text = diff_paths(old, new, model, tool)
    print(text)
    return 1 if res["counts"].get("worse") else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diff", help="diff two result sets")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--model")
    d.add_argument("--tool")
    r = sub.add_parser("run", help="run the trials at two revisions and diff")
    r.add_argument("--old", required=True, help="git revision of the old code")
    r.add_argument("--new", required=True, help="git revision of the new code")
    r.add_argument("--roster", required=True)
    r.add_argument("--models", required=True)
    r.add_argument("--tools", required=True)
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--out", required=True, help="folder for old/ and new/ results (outside the repo)")
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return diff_cli(a.old, a.new, a.model, a.tool) if a.cmd == "diff" else do_run(a)


if __name__ == "__main__":
    sys.exit(main())
