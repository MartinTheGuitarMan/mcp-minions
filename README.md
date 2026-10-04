# mcp-minions

An [MCP](https://modelcontextprotocol.io) server that hands narrow, well-defined jobs to a **small local model** instead of a big one: classify text, extract fields into a JSON Schema, and summarize. The model runs on your own machine through [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai), so the text never leaves it.

The idea is "strong system, slim model": a small model is fine at reading and rewriting, so the server gives it one clear task, forces structured output, and **validates the result in code** before returning it. It does not trust the model to follow the format.

## Tools

| Tool | What it does |
|---|---|
| `classify(text, labels, hint="")` | Picks exactly one of the given labels (1 to 50, unique). Returns `{"label": ...}`. |
| `extract(text, schema, hint="")` | Fills an object matching your JSON Schema (`"type": "object"`). Returns the object. |
| `summarize(text, max_words=100)` | Summary in at most `max_words` words (5 to 1000). Returns `{"summary": ...}`. |
| `classify_file`, `extract_file`, `summarize_file` | Same, but reading UTF-8 text from a local file path. |

## How it behaves

- **Schema first, then verify.** Each call asks the backend for schema-constrained JSON, and then checks the reply with `jsonschema`. Whether a backend actually enforces the schema varies, so a reply that is not valid JSON or does not match the schema is an error, not a guess.
- **No silent fallback.** If the backend is down you get `MINION_DOWN`. Nothing is retried on another model.
- **Untrusted input.** Your text is wrapped in `<data>` tags, a closing `</data>` inside it is neutralised, and the system prompt tells the model never to follow instructions found in the data. This reduces prompt-injection risk. It is not a guarantee, which is why the output schema is checked in code.
- **Stable error codes:** `MINION_DOWN`, `MINION_BACKEND_ERROR`, `MINION_SCHEMA`, `MINION_BAD_INPUT`, `MINION_BAD_CONFIG`.

## Install and run

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). You also need Ollama or LM Studio running with a model loaded.

```bash
git clone https://github.com/MartinTheGuitarMan/mcp-minions
cd mcp-minions
uv sync
MINION_BACKEND=ollama MINION_MODEL=<your-model> uv run mcp-minions
```

Example MCP client entry (the exact file and format depend on your client):

```json
{
  "mcpServers": {
    "minions": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-minions", "run", "mcp-minions"],
      "env": { "MINION_BACKEND": "ollama", "MINION_MODEL": "<your-model>" }
    }
  }
}
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MINION_MODEL` | required | Model name as the backend knows it |
| `MINION_BACKEND` | `lmstudio` | `lmstudio` or `ollama` |
| `MINION_BASE_URL` | `http://127.0.0.1:1234/v1` (LM Studio), `http://127.0.0.1:11434/v1` (Ollama) | OpenAI-compatible root, ending in `/v1` |
| `MINION_TIMEOUT` | `120` | Seconds to wait for the model |
| `MINION_MAX_FILE_BYTES` | `1000000` | Size cap for the `*_file` tools |
| `MINION_OLLAMA_NATIVE` | unset | Set to `1` to use Ollama's native `/api/chat` `format` instead of `response_format` |

## Security note

The `*_file` tools read **any UTF-8 text file** the caller names (up to the size cap). Whoever can call this server can therefore read files your user account can read. Only connect clients you trust, and leave the file tools out of any setup where that is a problem.

## Tests

```bash
uv run pytest
```

The unit tests use a fake backend. To try a real model, `scripts/smoke.py` runs every tool against a live backend and repeatedly probes whether the schema constraint holds, including hostile and prose-bait inputs:

```bash
uv run python scripts/smoke.py ollama <your-model> --repeat 5
```

## Status

Version 0.1. Early and small; expect changes.

## License

MIT, see [LICENSE](LICENSE).
