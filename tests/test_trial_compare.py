"""scripts/trial_compare.py: per-case diff of two trial result sets, and the run mode on a throwaway git repo."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("trial_compare", Path(__file__).resolve().parent.parent / "scripts" / "trial_compare.py")
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)


def rec(case, status, rep=0, code=None, tool="check_claims", model="air-qwen", rev="aaa1111", reason=None):
    # the shape tool_trials.py writes
    return {"status": status, "invented": False, "code": code, "reason": reason, "detail": "", "latency_s": 1.0,
            "prompt_tokens": 10, "completion_tokens": 5, "tool": tool, "model": model, "case": case, "rep": rep,
            "cloud": False, "ts": 1, "rev": rev}


def write(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return str(path)


def three(case, statuses, **kw):
    return [rec(case, s, i, code="MINION_INVENTED" if s == "check_failed" else None, **kw) for i, s in enumerate(statuses)]


def test_planted_flips_are_all_listed(tmp_path):
    old = (three("a", ["ok"] * 3) + three("b", ["ok"] * 3) + three("c", ["wrong"] * 3) + three("d", ["wrong"] * 3)
           + three("e", ["ok", "ok", "wrong"]) + three("f", ["ok"] * 3) + three("g", ["ok"] * 3) + three("only_old", ["ok"] * 3))
    new = (three("a", ["wrong"] * 3, rev="bbb2222") + three("b", ["ok"] * 3, rev="bbb2222") + three("c", ["ok"] * 3, rev="bbb2222")
           + three("d", ["check_failed"] * 3, rev="bbb2222") + three("e", ["ok", "wrong", "wrong"], rev="bbb2222")
           + three("f", ["ok", "ok", "ok"], rev="bbb2222") + three("g", ["ok", "ok", "check_failed"], rev="bbb2222")
           + three("only_new", ["ok"] * 3, rev="bbb2222"))
    res, text = tc.diff_paths(write(tmp_path / "o.jsonl", old), write(tmp_path / "n.jsonl", new))
    verdicts = {r["key"][2]: r["verdict"] for r in res["rows"]}
    assert verdicts == {"a": "worse", "b": "same", "c": "better", "d": "changed", "e": "worse", "f": "same", "g": "worse",
                        "only_old": "only_old", "only_new": "only_new"}
    assert "| check_claims | air-qwen | 17/24 | 15/24 |" in text or "check_claims | air-qwen | 17/" in text
    assert "aaa1111" in text and "bbb2222" in text
    assert tc.diff_cli(str(tmp_path / "o.jsonl"), str(tmp_path / "n.jsonl"), None, None) == 1


def test_identical_sets_exit_zero(tmp_path, capsys):
    f = write(tmp_path / "a.jsonl", three("a", ["ok"] * 3) + three("b", ["wrong"] * 3))
    assert tc.diff_cli(f, f, None, None) == 0
    assert "all 2 cases the same" in capsys.readouterr().out


def test_not_run_records_and_the_stop_marker_are_left_out(tmp_path):
    recs = three("a", ["ok"] * 3) + [rec("a", "not_run", 3, reason="busy"), rec("*", "not_run", 0, reason="stopped: x")]
    got = tc.per_case(recs)
    assert got[("check_claims", "air-qwen", "a")]["n"] == 3 and len(got) == 1


def test_filters_by_model_and_tool(tmp_path):
    recs = three("a", ["ok"] * 3) + three("a", ["wrong"] * 3, model="other") + three("a", ["ok"] * 3, tool="score")
    assert set(tc.per_case(recs, model="other")) == {("check_claims", "other", "a")}
    assert set(tc.per_case(recs, tool="score")) == {("score", "air-qwen", "a")}


def test_a_directory_of_model_files_is_read(tmp_path):
    write(tmp_path / "air-qwen.jsonl", three("a", ["ok"] * 3))
    write(tmp_path / "other.jsonl", three("a", ["ok"] * 3, model="other"))
    assert len(tc.load(str(tmp_path))) == 6


def test_bad_inputs_are_named(tmp_path):
    (tmp_path / "bad.jsonl").write_text("{}\nnope\n")
    with pytest.raises(SystemExit, match="line 2"):
        tc.load(str(tmp_path / "bad.jsonl"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no .jsonl"):
        tc.load(str(empty))


# ---------------------------------------------------------------- run mode on a throwaway git repo
def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    revs = []
    for status in ("ok", "wrong"):    # rev 1: the stand-in "runner" writes ok records, rev 2 writes wrong ones
        (repo / "runner.py").write_text(
            "import json, sys, pathlib\n"
            "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1]); out.mkdir(parents=True, exist_ok=True)\n"
            f"rec = {{'status': '{status}', 'tool': 'check_claims', 'model': 'm', 'case': 'c1', 'rep': 0, 'rev': 'x'}}\n"
            "(out / 'm.jsonl').write_text(json.dumps(rec) + '\\n')\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"runner {status}")
        revs.append(git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(tc, "REPO", repo)
    monkeypatch.setattr(tc, "trial_command", lambda roster, models, tools, repeats, out: [
        sys.executable, "runner.py", "--roster", roster, "--models", models, "--tools", tools, "--out", out])
    return repo, revs


def run_args(tmp_path, revs, dry=False):
    return tc.argparse.Namespace(old=revs[0], new=revs[1], roster="roster.json", models="m", tools="check_claims",
                                 repeats=1, out=str(tmp_path / "out"), dry_run=dry)


def test_plan_has_two_worktrees_and_the_runner_command(tmp_path, fake_repo):
    repo, revs = fake_repo
    steps = tc.plan(revs[0], revs[1], tmp_path / "w", run_args(tmp_path, revs))
    assert [s["side"] for s in steps] == ["old", "new"]
    assert steps[0]["add"][-1] == revs[0] and steps[1]["add"][-1] == revs[1]
    assert "--detach" in steps[0]["add"] and "--force" in steps[0]["remove"]
    assert steps[0]["out"] != steps[1]["out"]


def test_run_checks_out_each_revision_runs_it_and_cleans_up(tmp_path, fake_repo, capsys):
    repo, revs = fake_repo
    code = tc.do_run(run_args(tmp_path, revs))
    out = capsys.readouterr().out
    assert code == 1                               # old ok, new wrong: the case got worse
    assert "WORSE" in out
    assert len(git(repo, "worktree", "list").splitlines()) == 1     # the temp worktrees are gone


def test_dry_run_changes_nothing(tmp_path, fake_repo, capsys):
    repo, revs = fake_repo
    assert tc.do_run(run_args(tmp_path, revs, dry=True)) == 0
    assert len(git(repo, "worktree", "list").splitlines()) == 1
    assert not (tmp_path / "out").exists()
    assert "[old]" in capsys.readouterr().err


def test_out_inside_the_repo_is_refused(tmp_path, fake_repo, capsys):
    repo, revs = fake_repo
    a = run_args(tmp_path, revs)
    a.out = str(repo / "results")
    assert tc.do_run(a) == 2
