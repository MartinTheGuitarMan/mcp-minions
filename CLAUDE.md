# mcp-minions

An MCP server that gives narrow jobs (classify, extract, summarize) to small local models through Ollama or LM Studio, forces schema-constrained JSON, and **validates every result in code** before returning it. v0.2 adds an optional roster: several fast models, a classify quorum, and a reasoning judge. Public repo, MIT license.

## Layout
- `minions/server.py`: the MCP tools, thin wrappers only.
- `minions/tools.py`: input checks and task definitions (`classify`, `extract`, `summarize`, `read_text_file`).
- `minions/router.py`: per-tool routing. Fast models first (classify polls several for a quorum), the judge only on disagreement or schema failure.
- `minions/engine.py`: the fast path. Builds the prompt, calls the backend with a schema, parses and validates the reply.
- `minions/reasoning.py`: the reasoning path (no constrained decoding). Strips the think block, extracts the JSON, validates, allows exactly one retry.
- `minions/judge.py`: the judge. It *verifies* proposals (confirm or correct) and repairs schema failures. It does not redo the task from scratch.
- `minions/backends.py`: HTTP clients for LM Studio (OpenAI-compatible) and Ollama (compatible or native `/api/chat`).
- `minions/config.py`: `Config` (one model endpoint), `Roster` and `Route`. Single-model setup from `MINION_*` env vars, roster from `MINION_ROSTER` (inline JSON or a file path).
- `minions/errors.py`: error classes, each with a stable code.
- `tests/`: unit tests against `fake_backend.py` (`test_tools.py`, `test_v02.py`). `scripts/smoke.py` and `scripts/live_v02.py`: checks against live backends.

## Commands
```bash
uv sync                                   # keep uv.lock in sync with pyproject.toml
uv run pytest -q                          # unit tests, fake backend, no model needed
MINION_BACKEND=ollama MINION_MODEL=<model> uv run mcp-minions     # run the server, single model
uv run python scripts/smoke.py ollama <model> --repeat 5          # live backend and a loaded model required
```
`scripts/live_v02.py` needs real backends: `demo` runs a quorum with a judge, `down` requires Ollama to be stopped and checks that `MINION_DOWN` is raised and nothing falls back.

## Design rules (do not break these)
- **Never trust a model to follow the schema.** Fast-model replies are parsed and checked with `jsonschema` in `engine.run`. Reasoning-model replies are checked in `reasoning.run`. Invalid output is an error, never a guess.
- **Hard failures are never routed around.** `MINION_DOWN` and `MINION_BACKEND_ERROR` from any model propagate. The judge must not mask them, and no route may silently switch to another model or a canned answer.
- **The judge runs only on disagreement or schema failure, and it verifies.** It is not a second opinion on every call. With no judge configured, a missed quorum raises `MINION_NO_QUORUM`.
- **Stable error codes** live in `errors.py`: `MINION_DOWN`, `MINION_BACKEND_ERROR`, `MINION_SCHEMA`, `MINION_NO_QUORUM`, `MINION_BAD_INPUT`, `MINION_BAD_CONFIG`. A new failure mode gets a new class and code, plus a README entry.
- **Input is untrusted.** Text is wrapped in `<data>` tags, a closing `</data>` inside the text is neutralised, and the prompts tell the model not to follow instructions in the data. Keep `temperature` at 0.
- **Without `MINION_ROSTER` the single-model behaviour must stay unchanged** (`Roster.single`).
- **The `*_file` tools can read any UTF-8 file the caller names** (size-capped). Treat that as a security-sensitive surface. Do not widen it (no directory reads, no globbing, no other encodings) without discussing it first.
- Adding a tool: a thin wrapper in `server.py`, the logic in `tools.py`, a route entry in `config.py` (`TOOLS`), tests with the fake backend, and a row in the README table.

## This repo is PUBLIC
- No secrets, tokens, API keys, real hostnames, personal paths (`/Users/...`) or employer or work details, in code, tests, docs or commit messages. Test data must be invented.
- Do not commit `.venv/`, caches or `.env` files.
- Git identity for this repo is set locally to Martin Johansson with the GitHub no-reply address. Do not change it, and never put a personal email in a commit.
- **Do not push without the human's say-so, and never force-push.** History is public.
- Commit messages: short imperative subject, one logical change per commit.
- Other sessions may also be working in this repo. Run `git status -sb` and `git log --oneline -5` before you start, and do not revert commits you did not make.

## Style
Python 3.10+, type hints, small functions, `from __future__ import annotations`. Keep dependencies minimal (currently `mcp`, `httpx`, `jsonschema`).
