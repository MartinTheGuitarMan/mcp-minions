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
- **Stable error codes:** `MINION_DOWN`, `MINION_BACKEND_ERROR`, `MINION_SCHEMA`, `MINION_BAD_INPUT`, `MINION_BAD_CONFIG`, and (v0.2) `MINION_NO_QUORUM`.

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

## Roster, quorum and judge (v0.2)

Set `MINION_ROSTER` to inline JSON or a path to a JSON file to use several models. Without it, the single-model setup above applies unchanged.

```json
{
  "models": {
    "g1":  {"backend": "lmstudio", "model": "google/gemma-3-1b",   "kind": "fast"},
    "e4b": {"backend": "lmstudio", "model": "google/gemma-4-e4b",  "kind": "fast"},
    "q9":  {"backend": "ollama",   "model": "qwen3.5:9b", "kind": "reasoning", "max_tokens": 4096}
  },
  "routes": {
    "classify":  {"fast": ["g1", "e4b"], "quorum": 2, "judge": "q9"},
    "extract":   {"fast": ["e4b"], "judge": "q9"},
    "summarize": {"fast": ["g1"]}
  }
}
```

- **`kind: fast`** models use constrained decoding (schema requested, then validated by us). **`kind: reasoning`** models are called without constrained decoding: they think freely, the think block is stripped, the last JSON object is extracted and validated against the schema by us, with one retry that feeds the error back.
- **Quorum (classify only).** Every fast model in the route votes; a label wins with at least `quorum` votes (default: majority) and a strict lead.
- **Judge.** Runs only on disagreement or schema failure, never otherwise. It *verifies* the proposals (`confirm` one, or `correct` it) instead of redoing the task; on a schema failure it repairs the fast model's invalid output. With no judge configured, disagreement raises `MINION_NO_QUORUM` and a schema failure raises `MINION_SCHEMA`.
- **Hard failures are never routed around.** `MINION_DOWN` and backend errors from any model propagate; the judge is not used to mask them.
- Set `MINION_META=1` to add a `_minion` key (votes, judge used) to results.

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

Version 0.2. Early and small; expect changes.

## License

MIT, see [LICENSE](LICENSE).
