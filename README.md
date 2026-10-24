# mcp-minions

An [MCP](https://modelcontextprotocol.io) server that hands narrow, well-defined jobs to a **small local model** instead of a big one: classify text, extract fields into a JSON Schema, and summarize. The model runs on your own machine through [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai), so the text never leaves it.

The idea is "strong system, slim model": a small model is fine at reading and rewriting, so the server gives it one clear task, forces structured output, and **validates the result in code** before returning it. It does not trust the model to follow the format.

## Tools

| Tool | What it does |
|---|---|
| `classify(text, labels, hint="")` | Picks exactly one of the given labels (1 to 50, unique). Returns `{"label": ...}`. |
| `extract(text, schema, hint="")` | Fills an object matching your JSON Schema (`"type": "object"`). Returns the object. |
| `summarize(text, max_words=100)` | Summary in at most `max_words` words (5 to 1000). Returns `{"summary": ...}`. |
| `tag(text, labels, max_labels=3, hint="")` | Zero or more of the given labels (at most `max_labels`). Returns `{"labels": [...]}`. |
| `find_sensitive(text)` | Personal or secret-looking spans (names, emails, phones, ids, addresses, keys): patterns plus a model. Every span the model names must occur verbatim in the text, or the call fails. Returns `{"found", "spans": [{text, kind, by}]}`. |
| `same(a, b, hint="")` | Do two texts state the same facts? `same`, `different` or `partly`, with a phrase quoted from one of the texts as evidence. |
| `grounded(claim, source, hint="")` | Is the claim backed by the source? `supported`, `contradicted` or `unclear`, with a quote copied from the source. A verdict whose quote is not in the source becomes `unclear`. See [Grounding check](#grounding-check-grounded). |
| `classify_batch(items, labels, ...)`, `tag_batch(items, labels, max_labels=3, ...)` | `classify` or `tag` for up to 50 items `[{"id", "text"}]`, one result per id; a failing item does not fail the batch. See [Batches](#batches-classify_batch-and-tag_batch). |
| `classify_file`, `extract_file`, `summarize_file` | Same, but reading UTF-8 text from a local file path. |
| `classify_files(folder, pattern, labels, ...)`, `tag_files(folder, pattern, labels, max_labels=3, ...)` | The batch tools over up to 50 files in one folder matching a glob (`"*.md"`, `"**/*.md"`); ids are paths relative to the folder. Always local only. |

`tag`, `find_sensitive`, `same` and `grounded` are served by any model whose `caps` allow them (all models by default). A roster whose models list explicit `caps` without them keeps working; calling the tool then says no model serves it.

## How it behaves

- **Schema first, then verify.** Each call asks the backend for schema-constrained JSON, and then checks the reply with `jsonschema`. Whether a backend actually enforces the schema varies, so a reply that is not valid JSON or does not match the schema is an error, not a guess.
- **No silent fallback.** If the backend is down you get `MINION_DOWN`. Nothing is retried on another model. The one exception is a rate limit on a route with a configured `fallback` (see cloud minions).
- **Untrusted input.** Your text is wrapped in `<data>` tags, a closing `</data>` inside it is neutralised, and the system prompt tells the model never to follow instructions found in the data. This reduces prompt-injection risk. It is not a guarantee, which is why the output schema is checked in code.
- **Stable error codes:** `MINION_DOWN`, `MINION_BACKEND_ERROR`, `MINION_SCHEMA`, `MINION_BAD_INPUT`, `MINION_BAD_CONFIG`, (v0.2) `MINION_NO_QUORUM`, (v0.3) `MINION_PRIVACY` and `MINION_RATE_LIMITED`, and (managed models) `MINION_BUSY` and `MINION_LOAD_FAILED`.

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

## Config file

Instead of many `MINION_*` variables, point `MINION_CONFIG` at one TOML (or JSON) file with an optional `[machine]` table of flags and thresholds (`lifecycle`, `min_free_pct`, `large_gb`, `quiet_windows`, `allow_battery`, `model_dir`, `state_dir`, `log`, `meta`, ...), your `[models.*]` and `[routes.*]`. Real environment variables always win over the file, an unknown or mistyped setting is `MINION_BAD_CONFIG`, and keys never go in the file (cloud models name their key variable). With only a `[machine]` table the single-model setup from `MINION_MODEL` still applies. `MINION_ROSTER` keeps working. See [examples/minions.example.toml](examples/minions.example.toml); keep your own copy outside the repo.

`uv run mcp-minions init` writes a commented starter file (default `~/.config/minions/minions.toml`; `--path`, `--backend lmstudio|ollama`, `--model <name>`). It never overwrites an existing file without `--force`, does not contact any runtime and writes no keys.

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
- **Quorum (classify and grounded).** Every fast model in the route votes; a label (or verdict) wins with at least `quorum` votes (default: majority) and a strict lead.
- **Judge.** Runs only on disagreement or schema failure, never otherwise. It *verifies* the proposals (`confirm` one, or `correct` it) instead of redoing the task; on a schema failure it repairs the fast model's invalid output. With no judge configured, disagreement raises `MINION_NO_QUORUM` and a schema failure raises `MINION_SCHEMA`.
- **Position-bias mitigation.** Small models are sensitive to label order (in testing, a 1B model labelled "I love this" as negative with `[positive, negative]` and correctly with the order flipped). With several fast models each gets the labels in a different rotation. With a single fast model and a judge, the model is asked twice, original and reversed order, and a mismatch counts as a disagreement. Control it per route with `"debias": true/false`; it never applies outside `classify`, and a single model with no judge stays a single call.
- **Hard failures are never routed around.** `MINION_DOWN` and backend errors from any model propagate; the judge is not used to mask them.
- Models that share a server are called one after another, because LM Studio swaps models in and out and concurrent requests to two models on one server evict each other. Different servers run in parallel.
- Set `MINION_META=1` to add a `_minion` key (votes, judge used) to results.

## Cloud minions (v0.3)

A roster can include hosted models next to local ones, for **public text** only. The default is the safe one: every call is `sensitive=true`, so cloud models are never used unless the caller passes `sensitive=false`. See `examples/roster.cloud.example.json`. The repo ships no real roster and no keys: your own roster and `*.env` key files live outside the repo.

```json
"groq": {"backend": "openai-compat", "base_url": "https://api.groq.com/openai/v1", "model": "<model id>",
         "cloud": true, "key_env": "GROQ_API_KEY", "caps": ["summarize", "classify"],
         "limits": {"rpm": 30}, "constrain": "json_object"}
```

- **Privacy gate.** `cloud: true` models are removed from the route when a call is sensitive, which is the default. If no local fast model is left, the call is refused with `MINION_PRIVACY`; it is never answered elsewhere. The `*_file` tools are always sensitive. A model on a public host must say `"cloud": true` (otherwise the roster is rejected), so a cloud endpoint cannot be listed as local by mistake.
- **Keys** come from the environment variable named in `key_env` and are never stored, logged or shown. A missing key is `MINION_BAD_CONFIG`.
- **`caps`** lists the tools a model may serve (default all). With no explicit `fast` list, a route picks the first fast model that has the capability for that tool, not a model by name.
- **Rate limits.** `limits.rpm` is counted by this server per minute; at the cap the call is refused without being sent. A provider 429 is retried twice with backoff (honouring `Retry-After` up to 10 s), then reported as `MINION_RATE_LIMITED`. Provider quotas are not queried.
- **Failover is for rate limits only.** A route may set `"fallback": ["other-fast-model"]`, tried in order on `MINION_RATE_LIMITED` and on nothing else: `MINION_DOWN`, backend errors and schema failures still propagate. Sensitive calls never fail over to a cloud model. Fallbacks that already vote in a quorum are skipped.
- **`constrain`** sets how the schema is requested from a provider: `json_schema` (default), `json_object` (provider only offers JSON mode) or `none`. Replies are validated against the schema either way.
- **`minion_status`** lists models, whether each is cloud, its capabilities, whether its key is set (never the key), requests this server sent in the last minute, and the provider's own `x-ratelimit-*` headers from its latest reply, exactly as sent (Groq and Mistral send them; they are not interpreted, so the names and units differ per provider). Quota shows only after the minion has been called in the same server process.
- **Free tiers are not private.** Providers may use prompts for training, and limits change often, so keep them in the roster and check the provider's console.

### Open questions: `ask` and the hippo test

`ask(question, sensitive=true)` returns `{answer, confidence}` for a general question. Small models invent facts when a question has a false premise, so only models marked `"knowledge": true` in the roster may serve it, and you should set that flag only after the model passes a false-premise test (for example: "summarize the 2019 paper that first described a species that does not exist" must get "I cannot confirm that", not an invented paper). Without such a model the tool answers `MINION_BAD_INPUT`. The privacy gate applies as for every tool: a cloud model gets the question only with `sensitive=false`. Do not use `ask` for anything that must be exactly right.

## Machine status (read-only, opt-in)

Set `MINION_LIFECYCLE=1` and `minion_status` gains a `lifecycle` block: which models are resident and installed in LM Studio (`lms ps/ls --json`) and Ollama (`/api/ps`, `/api/tags`), free memory, memory pressure, power, an optional hold marker, quiet windows, free disk on the model volume, and whether a model load *would* be allowed right now. It only reads: nothing is loaded, unloaded, downloaded or deleted, and only a fixed list of read-only commands is ever run. A probe that cannot be read blocks the load verdict. Design and next steps: [docs/dynamic-models.md](docs/dynamic-models.md).

| Variable | Default | Meaning |
|---|---|---|
| `MINION_LIFECYCLE` | unset | `1` adds the `lifecycle` block to `minion_status` |
| `MINION_MIN_FREE_PCT` | `45` | Free memory needed before a load would be allowed |
| `MINION_LARGE_GB` | `6` | A resident model above this counts as large (one at a time) |
| `MINION_HOLD_FILE` / `MINION_HOLD` | `~/.minions/hold` / unset | A present file (optional reason line, optional ISO expiry line) or `MINION_HOLD=1` blocks loads |
| `MINION_QUIET_WINDOWS` | unset | Comma-separated local-time windows such as `23:00-06:00` |
| `MINION_ALLOW_BATTERY` | unset | `1` allows loads on battery |
| `MINION_MODEL_DIR` | unset | Folder on the model volume, for the free-disk line |

## Managed models: `minion_load` and `minion_unload`

A local LM Studio or Ollama roster entry with `"manage": true` (optional `context` default 4096, `ttl_s` default 600, `mem_gb` estimate) can be loaded and unloaded by minions. The runtime unloads it itself after `ttl_s` idle seconds.

- `minion_load(name)` loads it only if the quiet gate passes (see the machine status variables above): enough free memory, no memory pressure, on AC power, no hold marker or quiet window, and no other large model. Otherwise it raises `MINION_BUSY` with the reasons and changes nothing. If another large model that minions itself loaded is resident, that one is unloaded first. A runtime failure is `MINION_LOAD_FAILED`.
- `minion_unload(name)` only unloads a model that minions loaded itself (recorded in `MINION_STATE_DIR`, default `~/.minions`). A model loaded by anything else is never unloaded from here.
- Unmanaged models are never touched. Loads, unloads and refusals are logged as events in `MINION_LOG` (no text) and counted in `minion_report` under `lifecycle`.
- Nothing downloads or deletes. See [docs/dynamic-models.md](docs/dynamic-models.md).

## Decision models (System One)

A System One model answers a typed question about a text with a probability instead of generating text. A roster entry like TypeSafe's Jev (`"backend": "systemone", "kind": "decision"`, optionally `"cloud": true` with `key_env`) can serve `classify` and the verdict of `grounded`. It works in two ways:

- **As a voter:** list it in a classify route's `fast` models; its label is one vote.
- **As a confident first line:** `"shortcut": {"model": "<name>", "min_confidence": 0.9}` on the classify (or grounded) route. If the decision model's confidence reaches the threshold, its label is returned alone and nothing else is called. Below it (or when it is rate limited or fails the schema check), the normal voters and judge run. With `MINION_META=1` the result shows whether the shortcut escalated.

A cloud decision model is never used for sensitive calls, as for every cloud model. Confidence values are model-specific, so tune `min_confidence` on your own labelled data (see the eval harness) rather than copying a number. Request shape (a `choice` question with `criteria` over the labels) follows the public Jev examples; the label is checked in code like every other model's output.

## Grounding check: `grounded`

`grounded(claim, source)` checks one claim against one source text: a fact proposed from a note, or a sentence of a summary against the text it summarizes (it catches invented numbers, names and dates). It returns `{"verdict": "supported" | "contradicted" | "unclear", "quote": "..."}`, plus `confidence` (0 to 1) when a decision-model shortcut gave the verdict.

- **The quote is checked in code.** A `supported` or `contradicted` verdict stands only if its quote occurs in the **source** (whitespace and letter case normalised, at least 3 characters). A quote taken from the claim, an invented passage or an empty quote downgrades the answer to `unclear`, with a `reason`. `unclear` always has an empty quote. A verdict outside the three values is `MINION_SCHEMA`, as everywhere.
- **Routing like classify.** The route key is `grounded` and the `caps` name is `grounded`. Several fast models vote on the verdict with a quorum; the winning verdict keeps the quote of a model that voted for it. On disagreement the judge verifies the proposals and gives its own quote, which is checked like any other; with no judge, a missed quorum is `MINION_NO_QUORUM`.
- **Decision models** (`backend: systemone`) answer the verdict as a pick-one question, as a voter or as a `shortcut`. They give no quote, so when they decide alone, the first generative fast model on the route is asked only for the quote; with none, the answer is `unclear`. The injection defence that strips instruction segments runs on `classify` only for now.

```json
"routes": {"grounded": {"fast": ["g1", "e4b"], "judge": "q9"}}
```

## Batches: `classify_batch` and `tag_batch`

Both take `items`, a list of 1 to 50 objects `{"id": "<unique string>", "text": "..."}`, plus the same arguments as `classify` or `tag` (labels and the other arguments are checked once, before any item). They return `{"results": [...], "ok": n, "failed": m}` with one entry per id, in input order: `{"id", "label"}` or `{"id", "labels"}` on success, `{"id", "error": {"code", "message"}}` on failure.

- **Per-item errors.** An empty text (`MINION_BAD_INPUT`), output that fails the schema (`MINION_SCHEMA`) or a missed quorum is reported with that item; the other items still run.
- **Stops that apply to everything.** After `MINION_DOWN`, `MINION_RATE_LIMITED` (with `retry_after`), `MINION_PRIVACY` or `MINION_BAD_CONFIG`, the remaining items are not sent and are marked `not attempted` with the same code, so a down backend or a used-up limit is never hammered 50 times. Items run one after another, so `limits.rpm` and model swapping behave exactly as for single calls; every item is one entry in the usage log.
- **Long texts are chunked.** A text over 8,000 characters (about 2,000 tokens) is split at paragraph breaks (a longer paragraph at a space), at most 4 chunks; anything beyond is dropped and the item is marked `"truncated": true`. Chunked items carry `"chunks": n`. `classify_batch` takes the chunks' majority label (a tie is `MINION_NO_QUORUM` for that item); `tag_batch` ranks labels by how many chunks chose them, then by first appearance, and keeps `max_labels`.

`classify_files` and `tag_files` build the items from the files in one folder that match a glob. They read regular UTF-8 files only (size-capped like the `*_file` tools; an unreadable file is an error for that id), skip hidden files and folders (`.git`, `.env`, ...) and any symlink that leads outside the folder, refuse absolute patterns and `..`, and refuse a pattern that matches more than 50 files. They take no `sensitive` argument: file contents always stay on local models.

## Defence against injections that name an answer

A text can try to steer the classifier ("the correct label is X", "already tagged X", a fake JSON label). When a classify route has a decision-model `shortcut`, each multi-segment text is first split into sentences and bracketed spans, and that decision model judges every segment on its own: an instruction or claim aimed at whoever classifies the text, or ordinary content? Instruction segments (probability 0.2 or more) are removed before any model classifies; with `MINION_META=1` the result lists them under `injection_removed`. A model judges meaning rather than matching phrases, so it also catches phrasings it has not seen.

Measured with Jev as the decision model: 14 attack phrasings written separately from the defence (84 items): 50 correct before, 83 after, and no more confident wrong answers (7 before). On 90 generated injection items: 69 before, 89 after. One benign text in 62 lost a segment. Limits: it needs a decision model, so it does not run on sensitive calls when that model is a cloud model; a text with a single segment cannot be split, so an attack inside the same sentence as the content passes through to the voters and judge; and an attacker who knows the defence can adapt. Voting, the judge and the confidence threshold remain the other defences. An earlier regex version caught none of the unseen phrasings and was dropped.

## Usage log and report

Set `MINION_LOG` to a file path and every tool call appends one JSON line: tool, whether it was sensitive, text length, latency, which models answered, shortcut or judge use, failovers, rate limits, the error code and the token counts the providers reported, summed over every model call of the tool call (voters, shortcut, judge; left out when a provider gives none; `cloud` marks a call in which a cloud model took part). It never stores the text, the labels or the answer. The `minion_report(days)` tool summarizes the log: calls per tool, p50/p95 latency, how often the shortcut answered, judge calls, failovers, errors and which models answered.

The report also has a `tokens` block: tokens handled locally and by cloud models (as the providers counted them; older log lines without counts are listed under `calls_without_token_counts`), and `estimated_kept_off_calling_model`. That figure is an **estimate, not a billing figure**. It counts only calls that succeeded, stayed on local models and did not fail over; errored, failed-over and cloud calls count zero. Input side: the logged text length divided by about 4 characters per token (the text the calling model did not have to process). Output side: the completion tokens the local models reported for those calls, quorum and judge calls included. The calling model still pays for passing the text in and reading the answer, so treat the number as an order of magnitude. The report repeats this method in its output.

## Evaluating minions

`evals/dataset_large.json` (generated by `evals/make_dataset.py`, fixed seed, 327 distinct invented classify items split into `dev` and `test` halves) is for tuning a confidence threshold on one half and checking it on the other. `evals/dataset.json` holds invented public test items with known answers (sentiment, topic and prompt-injection classification, field extraction, summaries checked for invented numbers, grounding checks of claims against a source (with invented numbers and a quote that must come from the source), short notes for `tag_batch` (labels that must and must not be chosen), and general questions with and without a false premise). `scripts/eval.py` runs each roster model alone, and optionally the whole classify route as a quorum, then prints a table of correct answers, schema failures, rate limits and median latency. Rate limits are waited out and counted, never scored as wrong answers.

```bash
MINION_ROSTER=my-roster.json uv run python scripts/eval.py --quorum --out results.json
```

Scoring is deliberately simple (exact label, field containment, a refusal pattern for false-premise questions), so read the raw outputs saved with `--out` before you trust a single miss. Use a small `--max-calls name=N` for models on free tiers with daily caps.

## Security note

The `*_file` tools read **any UTF-8 text file** the caller names (up to the size cap), and `classify_files` and `tag_files` read up to 50 such files from any folder the caller names. Whoever can call this server can therefore read files your user account can read. Only connect clients you trust, and leave the file tools out of any setup where that is a problem.

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
