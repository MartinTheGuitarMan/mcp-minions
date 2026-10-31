"""commit_message_staged: the staged diff of a work tree, read with one fixed `git diff --cached`, then commit_message's checks."""
import json
import subprocess
import threading

import pytest

from minions import gitdiff, tools
from minions.config import Config, Roster
from minions.errors import BadInput, Invented, PrivacyRefused, SchemaError, TooLong


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test Author", "-c", "user.email=author@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True).stdout


def write(repo, files: dict):
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    write(r, {"app/pager.py": "def page(items, n):\n    return items[0:n + 1]\n", "README.md": "# Demo\n"})
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Start")
    return r


def stage_fix(repo):
    write(repo, {"app/pager.py": "def page(items, n):\n    return items[0:n]\n"})
    git(repo, "add", "-A")


def reply(fake, subject="Fix off-by-one in page", body="The slice end was one too far."):
    fake.replies.append(json.dumps({"subject": subject, "body": body}))


# ---------------------------------------------------------------- the read
def test_reads_the_staged_diff_only(repo):
    stage_fix(repo)
    write(repo, {"README.md": "# Demo\n\nunstaged edit\n"})
    s = gitdiff.read_staged(str(repo))
    assert s.files == ["app/pager.py"] and "n + 1" in s.diff and "unstaged" not in s.diff and s.skipped == []


def test_works_before_the_first_commit(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    write(r, {"hello.py": "print('hi')\n"})
    git(r, "add", "-A")
    assert gitdiff.read_staged(str(r)).files == ["hello.py"]


def test_rename_and_deletion_are_in_the_diff(repo):
    git(repo, "mv", "README.md", "docs.md")
    git(repo, "rm", "-q", "app/pager.py")
    s = gitdiff.read_staged(str(repo))
    assert set(s.files) == {"docs.md", "app/pager.py"} and "rename from README.md" in s.diff


def test_binary_file_is_skipped_and_reported(repo):
    write(repo, {"logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe", "app/pager.py": "def page(items, n):\n    return items[:n]\n"})
    git(repo, "add", "-A")
    s = gitdiff.read_staged(str(repo))
    assert s.files == ["app/pager.py"] and s.skipped == [{"path": "logo.png", "reason": "binary"}] and "logo.png" not in s.diff


def test_non_utf8_file_is_skipped(repo):
    write(repo, {"notes.txt": b"caf\xe9 au lait\n", "app/pager.py": "def page(items, n):\n    return items[:n]\n"})
    git(repo, "add", "-A")
    s = gitdiff.read_staged(str(repo))
    assert s.files == ["app/pager.py"] and s.skipped == [{"path": "notes.txt", "reason": "not UTF-8"}]


def test_only_binary_staged_is_an_error(repo):
    write(repo, {"logo.png": b"\x89PNG\x00\x00\xff"})
    git(repo, "add", "-A")
    with pytest.raises(BadInput, match="only binary"):
        gitdiff.read_staged(str(repo))


def test_nothing_staged(repo):
    write(repo, {"README.md": "# Demo\n\nunstaged\n"})
    with pytest.raises(BadInput, match="nothing is staged"):
        gitdiff.read_staged(str(repo))


def test_not_a_repo_and_not_a_folder(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(BadInput):
        gitdiff.read_staged(str(tmp_path / "plain"))
    with pytest.raises(BadInput, match="not a folder"):
        gitdiff.read_staged(str(tmp_path / "missing"))
    with pytest.raises(BadInput):
        gitdiff.read_staged("")


def test_over_size_is_refused_not_cut(repo):
    write(repo, {"big.txt": "".join(f"line {i} of a long file\n" for i in range(900))})
    git(repo, "add", "-A")
    with pytest.raises(BadInput, match="limit is 12000"):
        gitdiff.read_staged(str(repo))


def test_just_under_the_limit_is_read_whole(repo):
    write(repo, {"mid.txt": "".join(f"line {i}\n" for i in range(1000))})
    git(repo, "add", "-A")
    s = gitdiff.read_staged(str(repo))
    assert len(s.diff) <= gitdiff.MAX_STAGED_CHARS and "+line 999" in s.diff


def test_git_env_cannot_redirect_the_read(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    write(other, {"secret.py": "x = 1\n"})
    git(other, "add", "-A")
    stage_fix(repo)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    assert gitdiff.read_staged(str(repo)).files == ["app/pager.py"]


# ---------------------------------------------------------------- a hostile repo runs nothing
def test_hostile_repo_config_runs_nothing(repo, tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n")
    script.chmod(0o755)
    write(repo, {".gitattributes": "*.py diff=evil\n", "app/pager.py": "def page(items, n):\n    return items[:n]\n"})
    git(repo, "add", "-A")   # stage first: the hostile settings go in afterwards
    cfgs = [("diff.external", str(script)), ("diff.evil.textconv", str(script)), ("diff.evil.command", str(script)),
            ("core.fsmonitor", str(script)), ("core.pager", f"touch '{marker}'"), ("core.hooksPath", str(tmp_path / "hooks")),
            ("alias.diff", f"!touch '{marker}'"), ("core.editor", str(script)), ("core.sshCommand", str(script)),
            ("diff.submodule", "log"), ("core.attributesFile", str(tmp_path / "attrs")), ("include.path", str(tmp_path / "inc"))]
    for k, v in cfgs:
        git(repo, "config", k, v)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for h in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction", "fsmonitor-watchman"):
        for d in (hooks, repo / ".git" / "hooks"):
            (d / h).write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            (d / h).chmod(0o755)
    (tmp_path / "attrs").write_text("*.py diff=evil\n")
    (tmp_path / "inc").write_text(f"[diff]\n\texternal = {script}\n")
    s = gitdiff.read_staged(str(repo))
    assert set(s.files) == {".gitattributes", "app/pager.py"} and "items[:n]" in s.diff
    assert not marker.exists()


def test_reading_does_not_change_the_repo(repo):
    stage_fix(repo)
    before = (git(repo, "status", "--porcelain=v2"), git(repo, "ls-files", "-s"), (repo / ".git" / "index").read_bytes())
    gitdiff.read_staged(str(repo))
    assert before == (git(repo, "status", "--porcelain=v2"), git(repo, "ls-files", "-s"), (repo / ".git" / "index").read_bytes())


# ---------------------------------------------------------------- the tool
def test_tool_good_reply(fake, cfg, repo):
    stage_fix(repo)
    reply(fake)
    out = tools.commit_message_staged(cfg, str(repo))
    assert out["subject"] == "Fix off-by-one in page" and out["files"] == ["app/pager.py"] and out["skipped"] == []
    body = fake.requests[0][1]["messages"]
    assert "items[0:n]" in json.dumps(body) and "n + 1" in json.dumps(body)


def test_tool_passes_the_hint_and_reports_skipped(fake, cfg, repo):
    write(repo, {"logo.png": b"\x89PNG\x00\xff", "app/pager.py": "def page(items, n):\n    return items[:n]\n"})
    git(repo, "add", "-A")
    reply(fake)
    out = tools.commit_message_staged(cfg, str(repo), hint="Mention the pager.")
    assert out["skipped"] == [{"path": "logo.png", "reason": "binary"}]
    assert "Mention the pager." in json.dumps(fake.requests[0][1])


@pytest.mark.parametrize("subject,exc", [("Fix the pager so that it stops returning one item too many at the end of a page", TooLong),
                                         ("Fixed the pager", SchemaError), ("Fixes the pager", SchemaError),
                                         ("Fix the pager.", SchemaError), ("", Exception), ("Fix pager in app/other.py", Invented)])
def test_tool_catches_planted_bad_subjects(fake, cfg, repo, subject, exc):
    stage_fix(repo)
    reply(fake, subject=subject)
    with pytest.raises(exc):
        tools.commit_message_staged(cfg, str(repo))


def test_tool_refuses_an_invented_file_in_the_body(fake, cfg, repo):
    stage_fix(repo)
    reply(fake, body="Also touches settings.yaml.")
    with pytest.raises(Invented):
        tools.commit_message_staged(cfg, str(repo))


def test_a_skipped_binary_counts_as_not_in_the_diff(fake, cfg, repo):
    write(repo, {"logo.png": b"\x89PNG\x00\xff", "app/pager.py": "def page(items, n):\n    return items[:n]\n"})
    git(repo, "add", "-A")
    reply(fake, subject="Add logo.png and fix pager")
    with pytest.raises(Invented):
        tools.commit_message_staged(cfg, str(repo))


def test_tool_input_errors_call_no_model(fake, cfg, repo, tmp_path):
    for folder in (str(tmp_path), str(repo)):   # not a repo, nothing staged
        with pytest.raises(BadInput):
            tools.commit_message_staged(cfg, folder)
    with pytest.raises(BadInput):
        tools.commit_message_staged(cfg, str(repo), hint="x" * 301)
    assert fake.requests == []


def test_always_local_cloud_only_route_is_refused_before_reading(fake, repo):
    cloud = Config(backend="lmstudio", base_url=fake.url, model="m", timeout=5, cloud=True, name="c")
    stage_fix(repo)
    with pytest.raises(PrivacyRefused):
        tools.commit_message_staged(Roster.single(cloud), str(repo))
    assert fake.requests == []


def test_has_no_sensitive_parameter():
    import inspect
    from minions import server
    assert "sensitive" not in inspect.signature(tools.commit_message_staged).parameters
    assert "sensitive" not in inspect.signature(server.commit_message_staged).parameters


def test_borrows_the_commit_message_route():
    cfg = lambda n: {"backend": "lmstudio", "base_url": "http://127.0.0.1:1/v1", "model": n}
    raw = {"models": {"a": cfg("a"), "b": cfg("b"), "c": cfg("c")},
           "routes": {"classify": {"fast": ["a"]}, "extract": {"fast": ["a"]}, "summarize": {"fast": ["a"]},
                      "commit_message": {"fast": ["b"], "fallback": ["c"]}}}
    r = Roster.from_dict(raw)
    assert r.route("commit_message_staged").fast[0].name == "b" and r.route("commit_message_staged").fallback[0].name == "c"
    del raw["routes"]["commit_message"]
    assert Roster.from_dict(raw).route("commit_message_staged").fast[0].name == "a"


def test_concurrent_calls_on_one_route(fake, cfg, repo):
    stage_fix(repo)
    fake.dynamic = lambda body: json.dumps({"subject": "Fix off-by-one in page", "body": ""})
    fake.delay = 0.05
    res, errs = [], []

    def go():
        try:
            res.append(tools.commit_message_staged(cfg, str(repo))["subject"])
        except Exception as e:   # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and res == ["Fix off-by-one in page"] * 6


# ---------------------------------------------------------------- the trial's case set builds and scores
def test_trial_cases_build_and_score(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
    import staged_trial as st
    cases = st.SET_A + st.SET_B
    assert len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        repo = st.build(c, tmp_path)
        s = gitdiff.read_staged(str(repo))
        assert s.files, c["id"]
    assert st.invented_files({"subject": "Fix page in other.py", "body": ""}, ["src/pager.py"]) == ["other.py"]
    assert st.invented_files({"subject": "Fix pager.py", "body": "Touches src/pager.py"}, ["src/pager.py"]) == []


def test_a_skipped_binary_may_be_named_when_the_diff_mentions_it(fake, cfg, repo):
    write(repo, {"README.md": "# Demo\n\n![logo](logo.png)\n", "logo.png": b"\x89PNG\x00\xff"})
    git(repo, "add", "-A")
    reply(fake, subject="Add logo to the readme", body="Shows logo.png in README.md.")
    assert tools.commit_message_staged(cfg, str(repo))["skipped"] == [{"path": "logo.png", "reason": "binary"}]


def test_trial_scorer_knows_old_names_of_renames(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
    import staged_trial as st
    repo = st.build(next(c for c in st.SET_A if c["id"] == "s15"), tmp_path)
    assert set(st.staged_names(repo)) == {"lib/strutil.py", "lib/text.py"}
