from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import tools
from .config import Roster
from .errors import MinionError

mcp = MCPServer("minions")


def guarded(fn):
    """Surface MinionError (MINION_DOWN, MINION_SCHEMA, ...) to the client with its code intact.
    The SDK masks every other exception as "Error executing tool", but shows ToolError messages."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MinionError as e:
            raise ToolError(str(e)) from e
    return wrapper


@mcp.tool()
@guarded
def classify(text: str, labels: list[str], hint: str = "", sensitive: bool = True,
             descriptions: dict[str, str] | None = None) -> dict:
    """Classify text into exactly one of the given labels. Local models only, unless sensitive=false
    (public text), which also allows cloud minions. descriptions (label -> one line) tells the models what each label means."""
    return tools.classify(Roster.from_env(), text, labels, hint, sensitive, descriptions)


@mcp.tool()
@guarded
def extract(text: str, schema: dict, hint: str = "", sensitive: bool = True) -> dict:
    """Extract fields from text into an object matching a JSON Schema (type: object). Required values must be
    non-empty and occur in the text (MINION_EMPTY, MINION_NOT_IN_SOURCE); optional values not found are listed in
    _unverified. sensitive=false (public text only) allows cloud minions."""
    return tools.extract(Roster.from_env(), text, schema, hint, sensitive)


@mcp.tool()
@guarded
def summarize(text: str, max_words: int = 100, sensitive: bool = True) -> dict:
    """Summarize text in at most max_words words. Numbers, dates and capitalised names in the summary must occur in
    the text (MINION_INVENTED) and it must keep to max_words plus 10% (MINION_TOO_LONG). sensitive=false (public text
    only) allows cloud minions."""
    return tools.summarize(Roster.from_env(), text, max_words, sensitive)


@mcp.tool()
@guarded
def tag(text: str, labels: list[str], max_labels: int = 3, hint: str = "", sensitive: bool = True) -> dict:
    """Pick every label that clearly applies to the text (none to max_labels). Returns {"labels": [...]}."""
    return tools.tag(Roster.from_env(), text, labels, max_labels, hint, sensitive)


@mcp.tool()
@guarded
def find_sensitive(text: str, sensitive: bool = True) -> dict:
    """Find personal or secret-looking spans (names, emails, phones, ids, addresses, keys) before text leaves the
    machine. Patterns plus a local model; each span is checked to occur verbatim in the text."""
    return tools.find_sensitive(Roster.from_env(), text, sensitive)


@mcp.tool()
@guarded
def scan_diff(folder: str, range: str, chunk_chars: int = 3000) -> dict:
    """find_sensitive over what a git commit range adds, before a push: the added lines of the diff and the commit
    messages. folder is a git work tree; range is "BASE..HEAD" (for example "origin/main..HEAD") or one base ref.
    Returns findings per chunk with file:line; a failing chunk is reported with its error and pattern hits, the rest
    still run. complete is false if any chunk failed or a binary file was skipped. Always local only."""
    return tools.scan_diff(Roster.from_env(), folder, range, chunk_chars)


@mcp.tool()
@guarded
def same(a: str, b: str, hint: str = "", sensitive: bool = True) -> dict:
    """Do two texts state the same facts? Returns verdict same, different or partly, plus a phrase quoted from
    one of the texts that shows the difference."""
    return tools.same(Roster.from_env(), a, b, hint, sensitive)


@mcp.tool()
@guarded
def grounded(claim: str, source: str, hint: str = "", sensitive: bool = True) -> dict:
    """Is the claim backed by the source? Returns verdict supported, contradicted or unclear, plus a quote copied
    from the source. The quote is checked in code to occur in the source; a verdict without one becomes unclear."""
    return tools.grounded(Roster.from_env(), claim, source, hint, sensitive)


@mcp.tool()
@guarded
def check_claims(text: str, source: str, hint: str = "", sensitive: bool = True) -> dict:
    """Split text into factual claims and mark each supported or not against source. Returns {"claims": [{claim,
    supported, quote}]}. A supported claim carries a quote copied from the source (checked in code) and may use only
    numbers and names the source has; a claim of the text that the model left out fails the call."""
    return tools.check_claims(Roster.from_env(), text, source, hint, sensitive)


@mcp.tool()
@guarded
def classify_batch(items: list[dict], labels: list[str], hint: str = "", sensitive: bool = True,
                   descriptions: dict[str, str] | None = None) -> dict:
    """classify for up to 50 items [{id, text}]. Returns one result per id ({id, label} or {id, error: {code,
    message}}); one item failing does not fail the batch. Long texts are classified in chunks."""
    return tools.classify_batch(Roster.from_env(), items, labels, hint, sensitive, descriptions)


@mcp.tool()
@guarded
def tag_batch(items: list[dict], labels: list[str], max_labels: int = 3, hint: str = "", sensitive: bool = True) -> dict:
    """tag for up to 50 items [{id, text}]. Returns one result per id ({id, labels} or {id, error: {code,
    message}}); one item failing does not fail the batch. Long texts are tagged in chunks."""
    return tools.tag_batch(Roster.from_env(), items, labels, max_labels, hint, sensitive)


@mcp.tool()
@guarded
def classify_files(folder: str, pattern: str, labels: list[str], hint: str = "",
                   descriptions: dict[str, str] | None = None) -> dict:
    """Like classify_batch, for up to 50 UTF-8 files in a folder matching a glob (for example "*.md"); ids are
    paths relative to the folder. Hidden files and anything outside the folder are skipped. Always local only."""
    return tools.classify_files(Roster.from_env(), folder, pattern, labels, hint, descriptions)


@mcp.tool()
@guarded
def tag_files(folder: str, pattern: str, labels: list[str], max_labels: int = 3, hint: str = "") -> dict:
    """Like tag_batch, for up to 50 UTF-8 files in a folder matching a glob (for example "*.md"); ids are paths
    relative to the folder. Hidden files and anything outside the folder are skipped. Always local only."""
    return tools.tag_files(Roster.from_env(), folder, pattern, labels, max_labels, hint)


@mcp.tool()
@guarded
def classify_file(path: str, labels: list[str], hint: str = "") -> dict:
    """Like classify, reading UTF-8 text from a local file (size-capped)."""
    cfg = Roster.from_env()
    return tools.classify(cfg, tools.read_text_file(cfg, path), labels, hint)


@mcp.tool()
@guarded
def extract_file(path: str, schema: dict, hint: str = "") -> dict:
    """Like extract, reading UTF-8 text from a local file (size-capped)."""
    cfg = Roster.from_env()
    return tools.extract(cfg, tools.read_text_file(cfg, path), schema, hint)


@mcp.tool()
@guarded
def summarize_file(path: str, max_words: int = 100) -> dict:
    """Like summarize (with the same content check), reading UTF-8 text from a local file (size-capped)."""
    cfg = Roster.from_env()
    return tools.summarize(cfg, tools.read_text_file(cfg, path), max_words)


@mcp.tool()
@guarded
def ask(question: str, sensitive: bool = True) -> dict:
    """Short answer to an open question from a model trusted with knowledge questions (it said no to a made-up
    paper). Never use for anything that must be exactly right. sensitive=false (nothing private in the question)
    allows cloud models."""
    return tools.ask(Roster.from_env(), question, sensitive)


@mcp.tool()
@guarded
def minion_status() -> dict:
    """List the configured minions: local or cloud, capabilities, whether a key is set, requests used this minute."""
    return tools.status(Roster.from_env())


@mcp.tool()
@guarded
def minion_load(name: str) -> dict:
    """Load a roster model marked "manage": true, only if the machine is quiet (free memory, no hold marker or quiet
    window, on AC power, no other large model). Refused loads raise MINION_BUSY with the reasons."""
    return tools.load_model(Roster.from_env(), name)


@mcp.tool()
@guarded
def minion_unload(name: str) -> dict:
    """Unload a managed model that minions loaded itself. Models loaded by anything else are never unloaded."""
    return tools.unload_model(Roster.from_env(), name)


@mcp.tool()
@guarded
def minion_report(days: float = 7.0) -> dict:
    """Usage summary from the call log: calls per tool, latency, shortcut answers and confidence spread, judge calls, failovers, errors, repaired extract and summarize check failures, tokens handled locally or by cloud, and an
    estimate (not a billing figure) of tokens kept off the calling model."""
    return tools.report(days)


def main() -> None:
    import sys
    if sys.argv[1:2] == ["init"]:
        from . import init
        raise SystemExit(init.main(sys.argv[2:]))
    mcp.run()


if __name__ == "__main__":
    main()
