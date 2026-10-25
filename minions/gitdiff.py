"""Read the added lines and commit messages of a commit range, for scan_diff. A read surface, so it is strict:
git runs from a fixed argument list (no shell), refs are checked before git sees them and then replaced by the
commit ids they resolve to, the folder must be a git work tree, the output is size-capped, and binary or non-UTF-8
files are skipped and reported, never decoded."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BadInput

MAX_DIFF_BYTES = 4_000_000   # raw `git diff` output; a bigger range is refused, not cut
MAX_COMMITS = 500
TIMEOUT = 60
# ref characters only: branch and tag names, origin/main, HEAD~2, HEAD^, v1.2, @{u}. No spaces, no leading - or .,
# no ".." inside one ref, no ":" (HEAD:path names a file, not a commit), no glob or quoting characters.
_REF = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9._/~^@{}+-]{0,199}")
_SHA = re.compile(r"[0-9a-f]{40,64}")
# git options that keep a repo's own config from running anything or changing the output format
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "-c", "diff.noprefix=false",
         "-c", "diff.mnemonicPrefix=false", "-c", "color.ui=never"]


@dataclass
class Unit:
    """Text to scan with where each line came from: an added line of one file, or a commit message."""
    label: str                                   # the file path, or "commit <id>"
    lines: list = field(default_factory=list)    # (line number or None, text)


@dataclass
class Range:
    base: str
    head: str
    merge_base: str
    commits: int
    files: list            # Units of added lines, one per file
    messages: list         # Units, one per commit message
    skipped: list          # {"path", "reason"}


def check_ref(ref) -> str:
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref:
        raise BadInput(f"not a valid ref: {str(ref)[:60]!r} (letters, digits and ._/~^@{{}}+- only, no spaces, "
                       "no leading - or .)")
    return ref


def parse_range(spec) -> tuple[str, str]:
    """'A..B' or 'A' (meaning A..HEAD), each side a checked ref. 'A...B' and other forms are refused."""
    if not isinstance(spec, str) or not spec:
        raise BadInput("range must be 'BASE..HEAD' (for example origin/main..HEAD) or a single base ref")
    parts = spec.split("..")
    if len(parts) == 1:
        return check_ref(parts[0]), "HEAD"
    if len(parts) != 2 or parts[1].startswith("."):
        raise BadInput("range must be 'BASE..HEAD' with two refs; 'A...B' is not supported")
    return check_ref(parts[0]), check_ref(parts[1] or "HEAD")


def _git(folder: Path, *args: str) -> bytes:
    exe = shutil.which("git")
    if exe is None:
        raise BadInput("git is not installed or not on PATH")
    # GIT_* variables could point git at another repository or config; drop them
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1")
    try:
        p = subprocess.run([exe, "-C", str(folder), *_SAFE, *args], capture_output=True, env=env, timeout=TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise BadInput(f"git {args[0]} took more than {TIMEOUT}s") from e
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BadInput(f"git {args[0]} failed: {msg[-1][:200] if msg else 'exit ' + str(p.returncode)}")
    if len(p.stdout) > MAX_DIFF_BYTES:
        raise BadInput(f"git {args[0]} output is over {MAX_DIFF_BYTES} bytes; scan a narrower range")
    return p.stdout


def _commit(folder: Path, ref: str) -> str:
    out = _git(folder, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    if not _SHA.fullmatch(out):
        raise BadInput(f"{ref!r} does not name a commit")
    return out


def work_tree(folder) -> Path:
    if not isinstance(folder, str) or not folder.strip():
        raise BadInput("folder must be a path to a git work tree")
    p = Path(folder).expanduser()
    if not p.is_dir():
        raise BadInput(f"not a folder: {folder}")
    p = p.resolve()
    if _git(p, "rev-parse", "--is-inside-work-tree").decode().strip() != "true":
        raise BadInput(f"not a git work tree: {folder}")
    return p


def _path(header: str) -> str:
    """The new path from a 'diff --git a/X b/Y' line (quoted paths unquoted roughly; good enough for a label)."""
    tail = header[len("diff --git "):]
    i = tail.rfind(" b/") if '"' not in tail else tail.rfind(' "b/')
    return tail[i + 1:].strip('"')[2:] if i >= 0 else tail


_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: bytes) -> tuple[list, list]:
    """(Units of added lines per file, skipped files). A binary file, or one whose added lines are not UTF-8,
    is skipped with its reason; nothing of it is scanned."""
    files, skipped = [], []
    for block in re.split(rb"(?m)^(?=diff --git )", diff):
        if not block.startswith(b"diff --git "):
            continue
        first = block.split(b"\n", 1)[0].decode("utf-8", "replace")
        path = _path(first)
        if re.search(rb"(?m)^(?:Binary files .* differ|GIT binary patch)$", block):
            skipped.append({"path": path, "reason": "binary"})
            continue
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": path, "reason": "not UTF-8"})
            continue
        unit, line, in_hunk = Unit(path), None, False
        for row in text.split("\n"):
            if m := _HUNK.match(row):
                line, in_hunk = int(m.group(1)), True
            elif not in_hunk:
                continue   # file header lines (---, +++, index, mode)
            elif row.startswith("+"):
                unit.lines.append((line, row[1:]))
                line += 1
            elif row.startswith(" "):
                line += 1
        if any(t.strip() for _, t in unit.lines):
            files.append(unit)
    return files, skipped


def read_range(folder, spec) -> Range:
    base_ref, head_ref = parse_range(spec)
    root = work_tree(folder)
    base, head = _commit(root, base_ref), _commit(root, head_ref)
    mb = _git(root, "merge-base", base, head).decode().strip()
    if not _SHA.fullmatch(mb):
        raise BadInput(f"{base_ref} and {head_ref} have no common ancestor")
    # only commit ids from here on: nothing the caller typed reaches git again
    diff = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=0",
                "--src-prefix=a/", "--dst-prefix=b/", mb, head, "--")
    log = _git(root, "log", f"--max-count={MAX_COMMITS + 1}", "--no-color", "--format=%x00%H%x01%B",
               f"{base}..{head}", "--")
    messages = []
    for entry in log.split(b"\x00")[1:]:
        sha, _, body = entry.partition(b"\x01")
        unit = Unit(f"commit {sha.decode()[:12]}")
        unit.lines = [(None, t) for t in body.decode("utf-8", "replace").strip().split("\n")]
        messages.append(unit)
    if len(messages) > MAX_COMMITS:
        raise BadInput(f"the range has more than {MAX_COMMITS} commits; scan a narrower range")
    files, skipped = added_lines(diff)
    return Range(base, head, mb, len(messages), files, messages, skipped)
