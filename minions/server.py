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
    """Extract fields from text into an object matching a JSON Schema (type: object). sensitive=false
    (public text only) allows cloud minions."""
    return tools.extract(Roster.from_env(), text, schema, hint, sensitive)


@mcp.tool()
@guarded
def summarize(text: str, max_words: int = 100, sensitive: bool = True) -> dict:
    """Summarize text in at most max_words words. sensitive=false (public text only) allows cloud minions."""
    return tools.summarize(Roster.from_env(), text, max_words, sensitive)


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
    """Like summarize, reading UTF-8 text from a local file (size-capped)."""
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
    """Usage summary from the call log: calls per tool, latency, shortcut answers, judge calls, failovers, errors, tokens handled locally or by cloud, and an
    estimate (not a billing figure) of tokens kept off the calling model."""
    return tools.report(days)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
