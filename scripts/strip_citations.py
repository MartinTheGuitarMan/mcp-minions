"""Remove source citations and links from wiki lines before they go to `check_claims`.

Plain code, no model, so it lives here and not in the MCP tool list. Why it exists: `check_claims` insists that every
number and name of the text comes back in some claim, and a citation such as `(source: raw/2026-10-10-notes.md)` carries a
date no claim repeats, so the whole call fails. Strip first, then check.

    uv run python scripts/strip_citations.py page.md > page.stripped.md
    echo "Two files moved. (source: raw/x.md)" | uv run python scripts/strip_citations.py

What goes: parenthetical or bracketed citations (`(source: ...)`, `(sources: ...)`, `(conversation <date>)`, anything in
brackets or parentheses that names a `raw/` path), footnote marks (`[^1]`, `[2]`), images, bare and angle-bracket urls.
What stays but loses its target: markdown links keep their text, wiki links keep their alias or page name. A link whose
text is only "source", "link" or a number is a citation and goes entirely. Parentheses that are not citations
(`(import day)`, `(2026-03-04)`) are left alone, and so is every other digit, date and word. Frontmatter is not touched.
"""
from __future__ import annotations

import re
import sys

_CITE_WORDS = r"(?:sources?|conversation|cited|citation|ref|refs|reference|references)"
_PAREN_CITE = re.compile(
    r"\(\s*(?:" + _CITE_WORDS + r"\b\s*:?[^()]*|[^()]*\braw/[^()]*)\)", re.I)
_BRACKET_CITE = re.compile(r"\[\s*(?:" + _CITE_WORDS + r"\b\s*:?[^\[\]]*|[^\[\]]*\braw/[^\[\]]*)\]", re.I)
_FOOTNOTE = re.compile(r"\[\^[^\]\s]+\]|\[\d+(?:\s*[,–-]\s*\d+)*\](?!\()")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|!\[\[[^\]]*\]\]")
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()\s]|\([^)]*\))*(?:\s+\"[^\"]*\")?\)")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_AUTOLINK = re.compile(r"<(?:https?://|www\.)[^>\s]+>")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'\"]")
_LINK_ONLY_CITE = re.compile(r"(?:sources?|src|link|links|ref|refs|reference|cite|here|\d+)", re.I)
_EMPTY_PAIRS = re.compile(r"\(\s*\)|\[\s*\]")


def _md_link(m: re.Match) -> str:
    text = m.group(1).strip()
    return "" if _LINK_ONLY_CITE.fullmatch(text) else text


def _wiki_link(m: re.Match) -> str:
    target, alias = m.group(1).strip(), (m.group(2) or "").strip()
    if target.startswith(("raw/", "/raw/")):
        return ""
    return alias or target


def _strip_line(line: str) -> str:
    indent = re.match(r"\s*", line).group(0)
    s = line[len(indent):]
    for _ in range(2):   # a second pass catches a citation exposed by removing a link inside it
        s = _IMAGE.sub("", s)
        s = _MD_LINK.sub(_md_link, s)
        s = _WIKI_LINK.sub(_wiki_link, s)
        s = _PAREN_CITE.sub("", s)
        s = _BRACKET_CITE.sub("", s)
        s = _AUTOLINK.sub("", s)
        s = _URL.sub("", s)
        s = _FOOTNOTE.sub("", s)
        s = _EMPTY_PAIRS.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return (indent + s.strip()) if s.strip() else ""


def strip_citations(text: str) -> str:
    """The text with citations and link targets removed, line by line (line breaks and indentation are kept)."""
    return "\n".join(_strip_line(line) for line in text.split("\n"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    text = "".join(open(p, encoding="utf-8").read() for p in args) if args else sys.stdin.read()
    sys.stdout.write(strip_citations(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
