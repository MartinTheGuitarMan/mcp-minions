from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import tools
from .config import Config

mcp = MCPServer("minions")


@mcp.tool()
def classify(text: str, labels: list[str], hint: str = "") -> dict:
    """Classify text into exactly one of the given labels using a local model."""
    return tools.classify(Config.from_env(), text, labels, hint)


@mcp.tool()
def extract(text: str, schema: dict, hint: str = "") -> dict:
    """Extract fields from text into an object matching a JSON Schema (type: object)."""
    return tools.extract(Config.from_env(), text, schema, hint)


@mcp.tool()
def summarize(text: str, max_words: int = 100) -> dict:
    """Summarize text in at most max_words words."""
    return tools.summarize(Config.from_env(), text, max_words)


@mcp.tool()
def classify_file(path: str, labels: list[str], hint: str = "") -> dict:
    """Like classify, reading UTF-8 text from a local file (size-capped)."""
    cfg = Config.from_env()
    return tools.classify(cfg, tools.read_text_file(cfg, path), labels, hint)


@mcp.tool()
def extract_file(path: str, schema: dict, hint: str = "") -> dict:
    """Like extract, reading UTF-8 text from a local file (size-capped)."""
    cfg = Config.from_env()
    return tools.extract(cfg, tools.read_text_file(cfg, path), schema, hint)


@mcp.tool()
def summarize_file(path: str, max_words: int = 100) -> dict:
    """Like summarize, reading UTF-8 text from a local file (size-capped)."""
    cfg = Config.from_env()
    return tools.summarize(cfg, tools.read_text_file(cfg, path), max_words)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
