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
def classify(text: str, labels: list[str], hint: str = "") -> dict:
    """Classify text into exactly one of the given labels using a local model."""
    return tools.classify(Roster.from_env(), text, labels, hint)


@mcp.tool()
@guarded
def extract(text: str, schema: dict, hint: str = "") -> dict:
    """Extract fields from text into an object matching a JSON Schema (type: object)."""
    return tools.extract(Roster.from_env(), text, schema, hint)


@mcp.tool()
@guarded
def summarize(text: str, max_words: int = 100) -> dict:
    """Summarize text in at most max_words words."""
    return tools.summarize(Roster.from_env(), text, max_words)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
