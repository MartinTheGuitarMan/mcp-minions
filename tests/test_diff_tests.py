"""scripts/diff_tests.py: planted bad test sets are each caught with the right verdict (small invented tasks)."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("diff_tests", Path(__file__).resolve().parent.parent / "scripts" / "diff_tests.py")
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)

REF = "def add(a, b):\n    return a + b\n"
BUGGY = "def add(a, b):\n    return a - b\n"
TESTS = "from solution import add\n\ndef test_pos():\n    assert add(2, 3) == 5\n\ndef test_zero():\n    assert add(0, 0) == 0\n"


def runner(sol, tests):
    return dt.host_runner(sol, tests, trusted=True, timeout=3)


def check(tests=TESTS, ref=REF, buggy=BUGGY):
    return dt.check_task(tests, ref, buggy, runner=runner)


def test_good_tests_catch_the_bug():
    r = check()
    assert r["verdict"] == "ok" and r["catches"] == ["test_pos"] and r["reference"]["passed"] == 2


def test_tests_that_pass_on_the_buggy_code():
    r = check(tests="from solution import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    assert r["verdict"] == "buggy_passes"


def test_a_reference_that_fails():
    assert check(ref=BUGGY)["verdict"] == "reference_fails"


def test_buggy_code_that_breaks_at_import_shows_no_behaviour():
    assert check(buggy="def add(a, b:\n    pass\n")["verdict"] == "buggy_broken"


def test_a_missing_name_in_the_buggy_code_is_broken_not_ok():
    assert check(buggy="def plus(a, b):\n    return a + b\n")["verdict"] == "buggy_broken"


def test_no_tests_at_all():
    assert check(tests="from solution import add\n")["verdict"] == "no_tests"


def test_an_endless_loop_in_the_buggy_code():
    assert check(buggy="def add(a, b):\n    while True:\n        pass\n")["verdict"] == "buggy_timeout"


def test_an_endless_loop_in_the_reference():
    assert check(ref="def add(a, b):\n    while True:\n        pass\n")["verdict"] == "reference_fails"


def test_stub_for_tasks_without_a_buggy_version():
    r = dt.check_task(TESTS, REF, None, runner=runner)
    assert r["verdict"] == "ok" and r["stub"] and len(r["catches"]) == 2


def test_stub_never_matching_regex():
    tests = "import re\nfrom solution import PATTERN\n\ndef test_a():\n    assert PATTERN.fullmatch('#fff')\n"
    ref = "import re\nPATTERN = re.compile(r'#[0-9a-f]{3}')\n"
    assert dt.check_task(tests, ref, None, runner=runner)["verdict"] == "ok"
    assert "(?!x)x" in dt.stub_for(tests)


def test_stub_handles_several_names_and_parentheses():
    s = dt.stub_for("from solution import max_profit, min_rooms\nfrom solution import (A,\n")
    assert "def max_profit" in s and "def min_rooms" in s


def test_host_runner_refuses_untrusted():
    with pytest.raises(dt.Untrusted):
        dt.host_runner(REF, TESTS)
    with pytest.raises(dt.Untrusted):
        dt.check_task(TESTS, REF, BUGGY)      # the default runner is untrusted


def test_the_run_has_no_secrets_and_a_throwaway_home(monkeypatch):
    monkeypatch.setenv("API_TOKEN_FAKE", "x")
    tests = ("import os\nfrom solution import f\n\ndef test_env():\n    assert f() == 'ok'\n"
             "    assert 'API_TOKEN_FAKE' not in os.environ\n    assert os.path.realpath(os.environ['HOME']) == os.path.realpath(os.getcwd())\n")
    assert dt.host_runner("def f():\n    return 'ok'\n", tests, trusted=True, timeout=10)["outcome"] == "passed"


def test_cli_requires_trusted_and_one_source(tmp_path):
    with pytest.raises(SystemExit):
        dt.main(["--dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        dt.main(["--trusted"])


def test_cli_on_a_task_folder(tmp_path, capsys):
    (tmp_path / "tests.py").write_text(TESTS)
    (tmp_path / "reference.py").write_text(REF)
    (tmp_path / "buggy.py").write_text(BUGGY)
    assert dt.main(["--trusted", "--dir", str(tmp_path)]) == 0
    assert "OK" in capsys.readouterr().out
    (tmp_path / "buggy.py").write_text(REF)
    assert dt.main(["--trusted", "--dir", str(tmp_path)]) == 1
