"""Check that a task's hidden tests tell buggy code from correct code: they must fail on the buggy version and pass on the
reference. A test file that passes on buggy code, or fails on the reference, scores nothing meaningful.

Plain code, no model, so it lives here, not in the MCP tool list. It RUNS PYTHON, so it is for trusted files only (the
tasks and reference solutions in this repo's coding bench, which are written by people). Code written by a model must
never go through this script: the coding bench runs that only inside its locked-down Docker sandbox (`bench/sandbox.py`).
The host runner therefore needs an explicit `--trusted` on the command line (or `trusted=True` in code), runs in a temp
folder with a scrubbed environment (no secrets, a throwaway HOME), and kills a run that goes over the time limit. It is not
a security boundary: the code it runs can still do anything the user can.

    uv run python scripts/diff_tests.py --trusted --bench /path/to/bench/code_tasks.py [--task id ...]
    uv run python scripts/diff_tests.py --trusted --dir my_task/       # my_task/tests.py, reference.py, buggy.py

Tasks without a buggy version (the write and regex tasks) are checked against a generated stub: every name the tests import
from `solution` becomes a function that raises NotImplementedError (a never-matching regex for `PATTERN`).

Verdicts: `ok`; `no_tests` (the reference collects no tests); `reference_fails` (a failing, erroring or hanging reference);
`buggy_passes` (the tests do not notice the bug); `buggy_broken` (the buggy code makes the tests crash before any assertion:
no behaviour is tested); `buggy_timeout`. Exit code 1 when any task is not `ok`.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_S = 20
_LINE = re.compile(r"^test_task\.py::(.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", re.M)


class Untrusted(RuntimeError):
    """The host runner was asked to run code without `trusted=True`."""


def _env(home: str) -> dict:
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    return {**keep, "HOME": home, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTEST_ADDOPTS": ""}


def host_runner(solution: str, tests: str, *, trusted: bool = False, timeout: int = TIMEOUT_S) -> dict:
    """Run `tests` against `solution` with pytest in a temp folder. -> {outcome, tests: {name: STATUS}, passed, failed}.
    outcome: passed | failed | no_tests | error (collection or internal error) | timeout."""
    if not trusted:
        raise Untrusted("the host runner runs code on this machine: pass trusted=True only for files written by people")
    with tempfile.TemporaryDirectory(prefix="diff-tests-") as d:
        (Path(d) / "solution.py").write_text(solution)
        (Path(d) / "test_task.py").write_text(tests)
        cmd = [sys.executable, "-I", "-m", "pytest", "-v", "--tb=no", "-p", "no:cacheprovider", "-p", "no:randomly", "test_task.py"]
        p = subprocess.Popen(cmd, cwd=d, env={**_env(d), "PYTHONPATH": d}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return {"outcome": "timeout", "tests": {}, "passed": 0, "failed": 0}
    seen = {m.group(1): m.group(2) for m in _LINE.finditer(out)}
    passed = sum(s in ("PASSED", "XFAIL") for s in seen.values())
    failed = sum(s in ("FAILED", "ERROR", "XPASS") for s in seen.values())
    rc = p.returncode
    outcome = {0: "passed", 1: "failed", 5: "no_tests"}.get(rc, "error")
    if outcome == "passed" and not seen:
        outcome = "no_tests"
    return {"outcome": outcome, "tests": seen, "passed": passed, "failed": failed}


def stub_for(tests: str) -> str:
    """A solution that exists and fails every test: each imported name raises NotImplementedError when used."""
    names: list[str] = []
    for m in re.finditer(r"^from solution import (.+)$", tests, re.M):
        names += [n.strip() for n in m.group(1).replace("(", "").replace(")", "").split(",") if n.strip()]
    lines = ["import re"]
    for n in dict.fromkeys(names):
        if n == "PATTERN":
            lines.append("PATTERN = re.compile(r'(?!x)x')")
        else:
            lines += [f"def {n}(*a, **k):", "    raise NotImplementedError"]
    return "\n".join(lines) + "\n"


def check_task(tests: str, reference: str, buggy: str | None = None, runner=None) -> dict:
    """-> {verdict, reference, buggy, catches: [tests that pass on the reference and fail on the buggy code]}"""
    runner = runner or (lambda sol, t: host_runner(sol, t, trusted=False))
    ref = runner(reference, tests)
    bug = runner(buggy if buggy is not None else stub_for(tests), tests)
    res = {"reference": ref, "buggy": bug, "stub": buggy is None,
           "catches": sorted(t for t, s in bug["tests"].items() if s in ("FAILED", "ERROR") and ref["tests"].get(t) == "PASSED")}
    if ref["outcome"] == "no_tests":
        res["verdict"] = "no_tests"
    elif ref["outcome"] != "passed":
        res["verdict"] = "reference_fails"
    elif bug["outcome"] == "timeout":
        res["verdict"] = "buggy_timeout"
    elif bug["outcome"] == "passed":
        res["verdict"] = "buggy_passes"
    elif bug["outcome"] in ("error", "no_tests") or not res["catches"]:
        res["verdict"] = "buggy_broken"
    else:
        res["verdict"] = "ok"
    return res


def load_bench_tasks(path: str) -> list[dict]:
    spec = importlib.util.spec_from_file_location("bench_code_tasks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # trusted: this runs the task definitions file
    return list(mod.TASKS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trusted", action="store_true", help="required: the files are written by people, not a model")
    ap.add_argument("--bench", help="path to bench/code_tasks.py")
    ap.add_argument("--dir", help="a folder with tests.py, reference.py and optionally buggy.py")
    ap.add_argument("--task", action="append", help="only these bench task ids")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S)
    a = ap.parse_args(argv)
    if not a.trusted:
        ap.error("--trusted is required (this script runs the files it is given)")
    if bool(a.bench) == bool(a.dir):
        ap.error("give exactly one of --bench and --dir")
    if a.bench:
        tasks = [t for t in load_bench_tasks(a.bench) if not a.task or t["id"] in a.task]
        items = [(t["id"], t["tests"], t["reference"], t["buggy"]) for t in tasks]
    else:
        d = Path(a.dir)
        items = [(d.name, (d / "tests.py").read_text(), (d / "reference.py").read_text(),
                  (d / "buggy.py").read_text() if (d / "buggy.py").exists() else None)]
    bad = 0
    for tid, tests, ref, buggy in items:
        r = check_task(tests, ref, buggy, runner=lambda s, t: host_runner(s, t, trusted=True, timeout=a.timeout))
        bad += r["verdict"] != "ok"
        print(f"{r['verdict'].upper():16} {tid:22} reference {r['reference']['passed']}p/{r['reference']['failed']}f, "
              f"{'stub' if r['stub'] else 'buggy'} {r['buggy']['passed']}p/{r['buggy']['failed']}f, catches {len(r['catches'])}")
    print(f"\n{len(items) - bad}/{len(items)} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
