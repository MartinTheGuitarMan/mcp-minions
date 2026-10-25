"""scan_diff: find_sensitive over the added lines and commit messages of a commit range, chunk by chunk."""
import json
import subprocess

import pytest

from minions import gitdiff, tools
from minions.config import Roster
from minions.errors import BadInput


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test Author", "-c", "user.email=author@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def commit(repo, files: dict, message: str):
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, {"README.md": "# Demo\n\nOld line.\n"}, "Start")
    git(r, "branch", "base")
    return r


def spans(*items):
    return json.dumps({"spans": [{"text": t, "kind": k} for t, k in items]})


def test_scans_only_added_lines_and_messages(fake, cfg, repo):
    commit(repo, {"README.md": "# Demo\n\nOld line.\nContact Ada Quill for access.\n"},
           "Add contact line\n\nReviewed with Bo Lind.")
    fake.replies += [spans(("Ada Quill", "name")), spans(("Bo Lind", "name"))]
    out = tools.scan_diff(cfg, str(repo), "base..HEAD")
    sent = [b["messages"][1]["content"] for _, b in fake.requests]
    assert "Contact Ada Quill for access." in sent[0] and "Old line." not in sent[0] and "# Demo" not in sent[0]
    assert "Reviewed with Bo Lind." in sent[1]
    assert out["commits"] == 1 and out["files"] == 1 and out["complete"] and out["found"]
    diff, msg = out["chunks"]
    assert diff["id"] == "diff-1" and diff["from"] == ["README.md"]
    assert diff["spans"] == [{"text": "Ada Quill", "kind": "name", "by": "model", "where": "README.md:4"}]
    assert msg["id"] == "msg-1" and msg["spans"][0]["where"].startswith("commit ")


def test_single_ref_means_up_to_head(fake, cfg, repo):
    commit(repo, {"a.txt": "nothing here\n"}, "Add a")
    fake.replies += [spans(), spans()]
    out = tools.scan_diff(cfg, str(repo), "base")
    assert out["commits"] == 1 and out["found"] is False and out["complete"]


def test_a_failing_chunk_is_reported_and_the_rest_still_run(fake, cfg, repo):
    # what a small model did on a long diff: invalid JSON for some chunks. Those are reported per chunk, with
    # the pattern hits that need no model, and the scan is marked incomplete
    lines = [f"line {i}: nothing to see" for i in range(60)] + ["mail ada@example.invalid about it"]
    commit(repo, {"big.txt": "\n".join(lines) + "\n"}, "Add big file")
    fake.replies += [spans(), '{"spans": [{"text": "unterminated', spans()]
    out = tools.scan_diff(cfg, str(repo), "base..HEAD", chunk_chars=1000)
    assert [c["id"] for c in out["chunks"]] == ["diff-1", "diff-2", "msg-1"]
    ok1, bad, ok2 = out["chunks"]
    assert "error" not in ok1 and "error" not in ok2
    assert bad["error"]["code"] == "MINION_SCHEMA" and "not valid JSON" in bad["error"]["message"]
    assert bad["spans"] == [{"text": "ada@example.invalid", "kind": "email", "by": "rule", "where": "big.txt:61"}]
    assert out["ok"] == 2 and out["failed"] == 1 and out["complete"] is False and out["found"] is True


def test_down_stops_the_scan_and_marks_the_rest(cfg, repo, monkeypatch):
    commit(repo, {"a.txt": "x\n" * 900}, "Add a")
    down = cfg.__class__(backend="lmstudio", base_url="http://127.0.0.1:9/v1", model="m", timeout=2)
    out = tools.scan_diff(down, str(repo), "base..HEAD", chunk_chars=500)
    codes = [c["error"]["code"] for c in out["chunks"]]
    assert set(codes) == {"MINION_DOWN"} and len(codes) > 2
    assert out["chunks"][1]["error"]["message"].startswith("not attempted: MINION_DOWN")


def test_binary_and_non_utf8_files_are_skipped_and_reported(fake, cfg, repo):
    commit(repo, {"logo.png": b"\x89PNG\x00\x01\x02XYZZY-IN-BINARY", "latin1.txt": "caf\xe9 au lait\n".encode("latin-1"),
                  "ok.txt": "plain\n"}, "Add files")
    fake.replies += [spans(), spans()]
    out = tools.scan_diff(cfg, str(repo), "base..HEAD")
    assert {"path": "logo.png", "reason": "binary"} in out["skipped"]
    assert {"path": "latin1.txt", "reason": "not UTF-8"} in out["skipped"]
    assert out["files"] == 1 and out["complete"] is False
    assert "XYZZY" not in json.dumps(fake.requests)


def test_only_what_the_range_adds_since_the_merge_base(fake, cfg, repo):
    commit(repo, {"feature.txt": "feature text\n"}, "Feature")
    git(repo, "checkout", "-q", "base")
    commit(repo, {"other.txt": "already on base\n"}, "Other")
    git(repo, "checkout", "-q", "main")
    fake.replies += [spans(), spans()]
    out = tools.scan_diff(cfg, str(repo), "base..main")
    assert out["commits"] == 1 and "already on base" not in json.dumps(fake.requests)


def test_always_local_only(fake, repo):
    commit(repo, {"a.txt": "text\n"}, "Add a")
    r = Roster.from_dict({"models": {"c": {"backend": "openai-compat", "base_url": fake.url, "model": "c",
                                           "cloud": True, "key_env": "NOPE"}}})
    with pytest.raises(Exception, match="MINION_PRIVACY"):
        tools.scan_diff(r, str(repo), "base..HEAD")
    assert fake.requests == []


def test_size_cap_refuses_rather_than_cutting(cfg, repo, monkeypatch):
    commit(repo, {"a.txt": "y" * 5000 + "\n"}, "Add a")
    monkeypatch.setattr(tools, "SCAN_MAX_CHARS", 1000)
    with pytest.raises(BadInput, match="narrower range"):
        tools.scan_diff(cfg, str(repo), "base..HEAD")
    monkeypatch.setattr(gitdiff, "MAX_DIFF_BYTES", 100)
    with pytest.raises(BadInput, match="over 100 bytes"):
        gitdiff.read_range(str(repo), "base..HEAD")


@pytest.mark.parametrize("spec", ["--output=/tmp/x", "-p", "base..--all", "HEAD:README.md", "base HEAD",
                                  "base...HEAD", "a..b..c", ".hidden", "base;rm", "$(id)", "ba`se`", "", "x" * 300,
                                  "base..HEAD\n", "ma*n", "a\\b"])
def test_bad_refs_are_refused_before_git_runs(spec, repo, monkeypatch):
    monkeypatch.setattr(gitdiff, "_git", lambda *a, **k: pytest.fail("git must not run"))
    with pytest.raises(BadInput):
        gitdiff.read_range(str(repo), spec)


@pytest.mark.parametrize("spec", ["base..HEAD", "base", "HEAD~1..HEAD", "base..main", "main^..main", "base.."])
def test_good_refs_resolve(spec, repo):
    commit(repo, {"a.txt": "text\n"}, "Add a")
    assert gitdiff.read_range(str(repo), spec).commits == 1


def test_unknown_ref_and_non_repo_folder(repo, tmp_path):
    with pytest.raises(BadInput, match="failed"):
        gitdiff.read_range(str(repo), "nope..HEAD")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(BadInput):
        gitdiff.read_range(str(plain), "base..HEAD")
    with pytest.raises(BadInput, match="not a git work tree"):
        gitdiff.read_range(str(repo / ".git"), "base..HEAD")
    with pytest.raises(BadInput, match="not a folder"):
        gitdiff.read_range(str(repo / "README.md"), "base..HEAD")


def test_git_environment_cannot_redirect_the_repo(repo, tmp_path, monkeypatch):
    commit(repo, {"a.txt": "text\n"}, "Add a")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))   # would make git read the empty repo instead
    assert gitdiff.read_range(str(repo), "base..HEAD").commits == 1


def test_chunk_size_is_checked(cfg, repo):
    with pytest.raises(BadInput, match="chunk_chars"):
        tools.scan_diff(cfg, str(repo), "base..HEAD", chunk_chars=10)
