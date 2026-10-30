# mcp-minions

An [MCP](https://modelcontextprotocol.io) server that hands narrow, well-defined jobs to **small models** instead of a big one: classify text, extract fields into a JSON Schema, check a claim against a source, tag, and summarize. By default the model runs on your own machine through [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai), so the text never leaves it.

An optional roster adds more models, on this machine or others you reach over the network, with voting and a judge. It can also list hosted cloud models. Those are used only when the caller marks the text as public (`sensitive=false`); every call is private by default, and the `*_file` tools always stay local. With the `*_file` tools the calling model passes a path and never reads the text itself.

The idea is "strong system, slim model": a small model is fine at reading and rewriting, so the server gives it one clear task, forces structured output, and **validates the result in code** before returning it. It does not trust the model to follow the format. How far the check goes depends on the tool: a label must be one of yours; extracted values and grounding quotes must occur in the source; and every number, date and capitalised name in a summary must occur in the source too. The summary check is a net for invented details, not proof that a summary is fair or complete.

## Tools

| Tool | What it does |
|---|---|
| `classify(text, labels, hint="")` | Picks exactly one of the given labels (1 to 50, unique). Returns `{"label": ...}`. |
| `extract(text, schema, hint="")` | Fills an object matching your JSON Schema (`"type": "object"`). Returns the object. Every value must occur in the text, or the call fails; see [Value check](#value-check-extract). |
| `summarize(text, max_words=100)` | Summary in at most `max_words` words (5 to 1000). Returns `{"summary": ...}`. Numbers, dates and capitalised names in it must occur in the text, or the call fails; see [Content check](#content-check-summarize). |
| `tag(text, labels, max_labels=3, hint="")` | Zero or more of the given labels (at most `max_labels`). Returns `{"labels": [...]}`. The schema sent to the backend has no `uniqueItems` (LM Studio's MLX models reject it with HTTP 400); a repeated label is rejected in code as `MINION_SCHEMA`, not deduped. |
| `find_sensitive(text)` | Personal or secret-looking spans (names, emails, phones, ids, addresses, keys): patterns plus a model. Every span the model names must occur verbatim in the text, or the call fails. Returns `{"found", "spans": [{text, kind, by}]}`. |
| `scan_diff(folder, range, chunk_chars=3000)` | `find_sensitive` over what a git commit range adds (added lines and commit messages), in chunks, before a push. Findings per chunk with `file:line`; a failing chunk does not fail the scan. Always local only. See [Diff scan](#diff-scan-scan_diff). |
| `same(a, b, hint="")` | Do two texts state the same facts? `same`, `different` or `partly`, with a phrase quoted from one of the texts as evidence. |
| `grounded(claim, source, hint="")` | Is the claim backed by the source? `supported`, `contradicted` or `unclear`, with a quote copied from the source. A verdict whose quote is not in the source becomes `unclear`. See [Grounding check](#grounding-check-grounded). |
| `check_claims(text, source, hint="")` | Splits `text` into claims and marks each supported or not against `source`. A supported claim carries a quote found in the source and may use only numbers and names the source has; a claim of the text that was left out fails the call. Trial tool: see [docs/tool-trials.md](docs/tool-trials.md). |
| `commit_message(diff, hint="")` | Drafts `{"subject", "body"}` for a git diff (up to 12000 characters). The subject is at most 72 characters, imperative and without a final period; every file the message names must be in the diff. Trial tool: see [docs/tool-trials.md](docs/tool-trials.md). |
| `dedupe(items, hint="")` | Groups near-identical texts among up to 50 `[{"id", "text"}]`. Returns `{"groups": [[id, ...], ...]}`; every id is in exactly one group, checked in code. Trial tool: see [docs/tool-trials.md](docs/tool-trials.md). |
| `score(text, criterion, hint="")` | Rates a text 1 to 5 on a stated criterion. Returns `{"score", "quote"}`: an integer in range, and a quote that occurs in the text. Trial tool: see [docs/tool-trials.md](docs/tool-trials.md). |
| `translate(text, target="en")` | Swedish to English (`en`) or English to Swedish (`sv`). Every number and capitalised name of the source must survive and none may be added; a place with an English form (Goteborg, Gothenburg) counts as a changed name. Trial tool: see [docs/tool-trials.md](docs/tool-trials.md). |
| `classify_batch(items, labels, ...)`, `tag_batch(items, labels, max_labels=3, ...)` | `classify` or `tag` for up to 50 items `[{"id", "text"}]`, one result per id; a failing item does not fail the batch. See [Batches](#batches-classify_batch-and-tag_batch). |
| `classify_file`, `extract_file`, `summarize_file` | Same, but reading UTF-8 text from a local file path. |
| `classify_files(folder, pattern, labels, ...)`, `tag_files(folder, pattern, labels, max_labels=3, ...)` | The batch tools over up to 50 files in one folder matching a glob (`"*.md"`, `"**/*.md"`); ids are paths relative to the folder. Always local only. |

`tag`, `find_sensitive`, `same` and `grounded` are served by any model whose `caps` allow them (all models by default). A roster whose models list explicit `caps` without them keeps working; calling the tool then says no model serves it.

## How it behaves

- **Schema first, then verify.** Each call asks the backend for schema-constrained JSON, and then checks the reply with `jsonschema`. Whether a backend actually enforces the schema varies, so a reply that is not valid JSON or does not match the schema is an error, not a guess.
- **No silent fallback.** If the backend is down you get `MINION_DOWN`, also when it drops the connection mid-request (reset, or disconnected without a response); a timeout is `MINION_BACKEND_ERROR`. Nothing is retried on another model. The one exception is a soft failure (a rate limit, or a managed model refused by the quiet gate with `MINION_BUSY`) on a route with a configured `fallback` (see cloud minions).
- **Untrusted input.** Your text is wrapped in `<data>` tags, a closing `</data>` inside it is neutralised, and the system prompt tells the model never to follow instructions found in the data. This reduces prompt-injection risk. It is not a guarantee, which is why the output schema is checked in code.
- **Stable error codes:** `MINION_DOWN`, `MINION_BACKEND_ERROR`, `MINION_SCHEMA`, `MINION_BAD_INPUT`, `MINION_BAD_CONFIG`, (v0.2) `MINION_NO_QUORUM`, (v0.3) `MINION_PRIVACY` and `MINION_RATE_LIMITED`, (managed models) `MINION_BUSY` and `MINION_LOAD_FAILED`, (extract and summarize) `MINION_EMPTY` and `MINION_NOT_IN_SOURCE`, and (summarize) `MINION_INVENTED` (a kind of `MINION_NOT_IN_SOURCE`) and `MINION_TOO_LONG`. These four are kinds of schema failure.

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
- **Failover is for soft failures only.** A route may set `"fallback": ["other-fast-model"]`, tried in order on `MINION_RATE_LIMITED` and on `MINION_BUSY` (a managed model refused by the quiet gate: nothing was loaded or sent) and on nothing else: `MINION_DOWN`, `MINION_BACKEND_ERROR`, `MINION_LOAD_FAILED` (a load that was tried and failed) and schema failures still propagate. If every model is skipped, the last such error is raised; there is never a canned answer. In a quorum (classify, grounded) a busy voter is skipped exactly like a rate-limited one (its own fallback is tried first); if no vote is left the error is raised, and the judge is not asked to verify an empty vote. Skipped models are logged (`busy`, `rate_limited`, `failover`) and counted by `minion_report`. Sensitive calls never fail over to a cloud model. Fallbacks that already vote in a quorum are skipped.
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

## Managed models: auto-load, `minion_load` and `minion_unload`

A local LM Studio or Ollama roster entry with `"manage": true` (optional `context` default 4096, `ttl_s` default 600, `mem_gb` estimate) can be loaded and unloaded by minions. The runtime unloads it itself after `ttl_s` idle seconds.

- `minion_load(name)` loads it only if the quiet gate passes (see the machine status variables above): enough free memory, no memory pressure, on AC power, no hold marker or quiet window, and no other large model. Otherwise it raises `MINION_BUSY` with the reasons and changes nothing. If another large model that minions itself loaded is resident, that one is unloaded first. A runtime failure is `MINION_LOAD_FAILED`.
- **Auto-load before a call.** When a route (fast model, fallback, judge, or the grounded quote model) reaches a managed model, minions first checks that it is resident (`lms ps --json`, or Ollama's `/api/ps`). If it is, the call goes ahead and the gate is not consulted. If not, the same gate and load as `minion_load` run first, with the entry's `context`, `ttl_s` and `mem_gb`. A refusal is `MINION_BUSY` and the route falls back to its next model (`fallback`); a failed load is `MINION_LOAD_FAILED` and propagates. The request is never sent to a managed model that is not resident. One lock per model, so concurrent calls load it once, and a model answering a call is never unloaded to make room for another (that load is refused with `MINION_BUSY`). The load has its own time budget (up to 600 s for the `lms load` command); the model's `timeout` covers only the request, which starts once the model is resident.
- **LM Studio just-in-time loading.** With "Just in Time model loading" on (the default in recent versions), LM Studio loads a model by itself when a request names one that is not loaded, at its own context and TTL, past the quiet gate. Auto-load avoids this by checking residency before every call to a managed model. One small window remains: if the model's TTL ends between the check and the request, LM Studio JIT-loads it. The window is the time between the check and the request (a fraction of a second); to close it entirely, turn JIT loading off in LM Studio's server settings. Ollama also loads on request; the same check applies.
- `minion_unload(name)` only unloads a model that minions loaded itself (recorded in `MINION_STATE_DIR`, default `~/.minions`). A model loaded by anything else is never unloaded from here.
- Unmanaged models are never touched. Loads, unloads and refusals are logged as events in `MINION_LOG` (no text) and counted in `minion_report` under `lifecycle`.
- Nothing downloads or deletes. See [docs/dynamic-models.md](docs/dynamic-models.md).

## Decision models (System One)

A System One model answers a typed question about a text with a probability instead of generating text. A roster entry like TypeSafe's Jev (`"backend": "systemone", "kind": "decision"`, optionally `"cloud": true` with `key_env`) can serve `classify` and the verdict of `grounded`. It works in two ways:

- **As a voter:** list it in a classify route's `fast` models; its label is one vote.
- **As a confident first line:** `"shortcut": {"model": "<name>", "min_confidence": 0.9}` on the classify (or grounded) route. If the decision model's confidence reaches the threshold, its label is returned alone and nothing else is called. Below it (or when it is rate limited or fails the schema check), the normal voters and judge run. With `MINION_META=1` the result shows whether the shortcut escalated.

A cloud decision model is never used for sensitive calls, as for every cloud model. Confidence values are model-specific, so tune `min_confidence` on your own labelled data rather than copying a number. A threshold set too high means the shortcut never answers alone and only adds a call. To see where it stands: the usage log records the shortcut's confidence and threshold on every call, `minion_report` shows their spread under `shortcut_confidence` (per tool and model: min, p10, p50, p90, max, how many calls reached the threshold, how many it answered alone, and how many had no confidence: `before_logging` for log lines written before confidence was logged, `model_gave_none` for replies without a usable number). The local `jev-style serve` does return a confidence and per-label probabilities on `/v1/systemone`; a `null` in an old report means those old lines, not the server. On a dozen invented customer messages its confidence ranged from 0.44 to 0.97 (median about 0.92) and only 3 of 12 reached 0.95, so with that threshold the shortcut answers alone only on the clearest texts. `scripts/eval.py --confidence` prints confidence-versus-correct pairs on labelled items with a table of how many calls each threshold would answer alone and how many of those are right:

```bash
MINION_ROSTER=my-roster.json uv run python scripts/eval.py --confidence --data evals/dataset_large.json --split dev
```

Pick the threshold on `dev`, then check it on `test`. The eval asks the decision model directly with the classify task text; the injection defence that runs before a live shortcut is not applied. Request shape (a `choice` question with `criteria` over the labels) follows the public Jev examples; the label is checked in code like every other model's output.

## Quick accuracy check: `scripts/quick_eval.py`

A small fixed set of invented cases (5 to 9 per tool) for `classify`, `tag`, `extract`, `summarize`, `grounded`, `ask` and `find_sensitive`, sent through the real routes the way a caller would and scored in code. It is meant to run weekly and take a few minutes (the full bench takes hours). `ask` includes made-up animals, papers and places: any answer that does not refuse is an invented answer and is printed on its own `INVENTED` line. `find_sensitive` includes planted fake secrets, a fake address and clean texts that must produce nothing.

```bash
MINION_ROSTER=my-roster.json MINION_ACCURACY_LOG=accuracy.jsonl uv run python scripts/quick_eval.py
```

- One JSON line per run is appended to the file `MINION_ACCURACY_LOG` names: date, a hash of the roster (models and routes, never key values), per tool the score, latency p50 and p95, which models answered and any errors or skipped cases, plus the invented count. Without the variable the summary is printed and nothing is written.
- Calls are `sensitive=true` (local models only) unless you pass `--public`. A rate limit, a refusal by the quiet gate or privacy gate, or a tool the roster does not serve counts as skipped, not wrong; any other error counts as a wrong answer. A managed model the quiet gate refuses is failed over as usual, so the `models` field shows who really answered.
- The run's own call log goes to `quick_eval_calls.jsonl` next to the accuracy log, so `minion_report` is not skewed. Managed models the run loaded are unloaded at the end (`--no-unload` to keep them). `--budget-s` (default 540) stops starting new cases; the line then has `"partial": true`.
- With about 8 cases per tool one case is 12 points: look for changes between runs, not for small differences.

## Grounding check: `grounded`

`grounded(claim, source)` checks one claim against one source text: a fact proposed from a note, or a sentence of a summary against the text it summarizes (it catches invented numbers, names and dates). It returns `{"verdict": "supported" | "contradicted" | "unclear", "quote": "..."}`, plus `confidence` (0 to 1) when a decision-model shortcut gave the verdict.

- **The quote is checked in code.** A `supported` or `contradicted` verdict stands only if its quote occurs in the **source** (whitespace and letter case normalised, at least 3 characters). A quote taken from the claim, an invented passage or an empty quote downgrades the answer to `unclear`, with a `reason`. `unclear` always has an empty quote. A verdict outside the three values is `MINION_SCHEMA`, as everywhere.
- **Routing like classify.** The route key is `grounded` and the `caps` name is `grounded`. Several fast models vote on the verdict with a quorum; the winning verdict keeps the quote of a model that voted for it. On disagreement the judge verifies the proposals and gives its own quote, which is checked like any other; with no judge, a missed quorum is `MINION_NO_QUORUM`.
- **Decision models** (`backend: systemone`) answer the verdict as a pick-one question, as a voter or as a `shortcut`. They give no quote, so when they decide alone, the first generative fast model on the route is asked only for the quote; with none, the answer is `unclear`. The injection defence that strips instruction segments runs on `classify` only for now.

```json
"routes": {"grounded": {"fast": ["g1", "e4b"], "judge": "q9"}}
```

## Value check: `extract`

A reply can match the schema and still say nothing: with vague field names a small model may return `{"order": "", "date": "", "city": "", "boxes": 0}` for a text that states all four. So after the schema check, `extract` (and `extract_file`) checks the values against the source text in code:

- **Required strings must not be empty.** An empty or whitespace-only value is `MINION_EMPTY`.
- **Values must occur in the source.** A string must appear as whole words after folding case, accents, whitespace and punctuation (`"MALMO"` matches `Malmö,`; `"Lund"` does not match `Lundby`). A date also matches the same date in another simple form (`2026-11-03`, `3 November 2026`, `Nov 3, 2026`, `03/11/2026`; English month names, d/m and m/d both accepted). A number must appear as digits in the source, with or without grouping (`1,250.50`, `1.250,50`, `12 400`), or as an English number word up to ninety-nine. A computed value (a sum, a converted unit) is therefore not found. A required value that is not found is `MINION_NOT_IN_SOURCE`.
- **A required number that is 0** passes when the source has a 0 in it; otherwise it is treated as a placeholder and is `MINION_EMPTY`. If "none" is a real answer for your data, make the field optional or nullable.
- **Optional fields are flagged, not failed.** Missing or empty is fine (not stated). A value not found in the source is kept and its field is listed in `"_unverified": [...]` on the result.
- **Not checked:** booleans, `null`, fields with `enum` or `const`, and nested objects. Only top-level properties are checked, plus the strings and numbers inside a top-level array. For a field the model should infer rather than copy (a category, a mood), use an `enum` or make it optional.

Both codes are `MINION_SCHEMA` subclasses, so they route like any schema failure: with a judge on the route, the judge repairs the answer and its repair must pass the same check (one retry with the reason fed back); without one, the error is returned. The privacy gate, quorum and failover rules are unchanged. The usage log records the kinds a judge repaired as `check_failures`, and `minion_report` counts them under `check_failures_repaired`; unrepaired ones show up in `errors`.

## Content check: `summarize`

A summary can be well-formed and still invent a figure, shift a date or add a name. So after the schema check, `summarize` (and `summarize_file`) checks the summary against the source text in code, with the same normalisation as extract's value check:

- **Numbers** must occur in the source: as written, with or without grouping (`1,250,000` for `1250000`), with either decimal mark, or scaled by a word the source or summary spells out (`1.25 million` for `1,250,000`). A plain decimal must match as a whole (`1.3` is not found from a `1` and a `3`); a token of three or more parts (`1.2.3`, `3.11.2026`) also passes when every part occurs. A rounded, summed or converted number fails (`about 45%` for `44.6%`).
- **Dates** in a simple form (`2026-11-03`, `3 November 2026`, `Nov 3, 2026`, `03/11/2026`) pass when the source has the same date in any of those forms. Otherwise their numbers and month name are checked one by one.
- **Capitalised names and terms** (a word starting with a capital) must occur in the source as a whole word after folding case and accents, without a plural or possessive ending (`Okafor's`, `Okafors`), or share all but their last three letters with a source word, for words over four letters (`Swedish` for `Sweden`). Letters inside a code (`4B`) are not words; its digits are checked as a number.
- **Not checked, to avoid false failures:** the first word of a sentence, a list item or a clause after a colon (unless it is all capitals or has a capital inside it, like `NASA` or `McKinley`), common words such as `The`, `Overall` or `I`, lower-case words, and number words (`three`).
- **Length:** at most `max_words` plus 10 % (at least 2 words), counting whitespace-separated words, since models count loosely. A longer summary is `MINION_TOO_LONG`; an empty one is `MINION_EMPTY`.

A failing summary is `MINION_INVENTED`, listing the tokens not found. All of these are `MINION_SCHEMA` subclasses, so they route like extract's checks: with a judge on the route, the judge repairs the summary and its repair must pass the same check (one retry); without one, the error is returned. The usage log records the kinds a judge repaired as `check_failures`.

**Limits.** Each token is checked alone, so a wrong pairing of facts that each occur (`March 2026` when the source has `March` and `2026` separately, or names swapped between roles) is not caught, and neither is a false statement in lower-case words (`rose` for `fell`). Use `grounded` per sentence for that. Sentence-initial names are skipped, so an invented name that opens a sentence passes unless it is all capitals or appears again later. English month names and number words only.

## Diff scan: `scan_diff`

`scan_diff(folder, range)` screens what a push would publish. `folder` is a git work tree; `range` is `BASE..HEAD` (for example `origin/main..HEAD`) or a single base ref, meaning `BASE..HEAD`. It scans:

- the **added lines** of `git diff` from the merge base of the two refs to the head (removed and context lines are not scanned), and
- the **commit messages** of `BASE..HEAD`.

The text is split into chunks of about `chunk_chars` characters (default 3,000, 500 to 8,000; a small model's list of spans is more often valid JSON on short chunks), and `find_sensitive` runs on each, local models only. It returns `{"chunks": [...], "ok", "failed", "found", "complete", "skipped", "commits", "files", "base", "head", "merge_base"}`. Each chunk has an `id` (`diff-1`, ..., `msg-1`, ...), `from` (the files or commits in it), and `spans` with `text`, `kind`, `by` (`rule` or `model`) and `where` (`path:line` or `commit <id>`).

- **One bad chunk does not fail the scan.** A chunk whose model answer is not valid JSON, fails the schema or names a span that is not in the text gets `error: {code, message}`; its pattern hits (emails, keys, phone numbers, long ids, which need no model) are still reported in `spans`. The other chunks still run. After `MINION_DOWN`, `MINION_RATE_LIMITED`, `MINION_PRIVACY` or `MINION_BAD_CONFIG` the remaining chunks are marked `not attempted` instead of hammering the same failure.
- **`complete` is true only** when every chunk was scanned and no file was skipped. Read `failed` and `skipped` before trusting a clean result.
- **Binary and non-UTF-8 files** are not decoded or scanned; they are listed in `skipped` with the reason.
- **Size caps.** The raw diff is capped at 4 MB, the text to scan at 300,000 characters and the range at 500 commits. A bigger range is refused with `MINION_BAD_INPUT`, never silently cut; scan it in parts.
- **Refs are checked before git sees them.** Each side of the range must be letters, digits and `._/~^@{}+-`, at most 200 characters, not starting with `-` or `.`, with no `..` inside, no spaces and no `:` (so `HEAD:file` and options like `--output=...` are refused). `A...B` is refused. Each ref must then resolve to a commit (`git rev-parse --verify --end-of-options REF^{commit}`), and only the resulting commit ids are passed to `git diff` and `git log`.
- **How git runs.** From a fixed argument list, never through a shell, with `GIT_*` environment variables removed (so `GIT_DIR` cannot point it at another repository), system config ignored, `core.fsmonitor` off, and `--no-ext-diff --no-textconv` so a repository's own config cannot run a diff helper.

A `find_sensitive` route is required (any model with that capability); every chunk is one `find_sensitive` line in the usage log. A clean scan is a second pair of eyes, not a guarantee: a small model misses things, and patterns only catch what they describe.

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

Set `MINION_LOG` to a file path and every tool call appends one JSON line: tool, whether it was sensitive, text length, latency, which models answered, shortcut or judge use (with the shortcut's confidence and threshold), failovers, rate limits, the error code and the token counts the providers reported, summed over every model call of the tool call (voters, shortcut, judge; left out when a provider gives none; `cloud` marks a call in which a cloud model took part). It never stores the text, the labels or the answer. The `minion_report(days)` tool summarizes the log: calls per tool, p50/p95 latency, how often the shortcut answered and the spread of its confidence, judge calls, failovers, errors, extract and summarize check failures a judge repaired, and which models answered.

The report also has a `tokens` block: tokens handled locally and by cloud models (as the providers counted them; older log lines without counts are listed under `calls_without_token_counts`), and `estimated_kept_off_calling_model`. That figure is an **estimate, not a billing figure**. It counts only calls that succeeded, stayed on local models and did not fail over; errored, failed-over and cloud calls count zero. Input side: the logged text length divided by about 4 characters per token (the text the calling model did not have to process). Output side: the completion tokens the local models reported for those calls, quorum and judge calls included. The calling model still pays for passing the text in and reading the answer, so treat the number as an order of magnitude. The report repeats this method in its output.

## Evaluating minions

`evals/dataset_large.json` (generated by `evals/make_dataset.py`, fixed seed, 327 distinct invented classify items split into `dev` and `test` halves) is for tuning a confidence threshold on one half and checking it on the other. `evals/dataset.json` holds invented public test items with known answers (sentiment, topic and prompt-injection classification, field extraction (including a required field the text does not state, where the only right outcome is `MINION_EMPTY` or `MINION_NOT_IN_SOURCE`), summaries checked for invented numbers, grounding checks of claims against a source (with invented numbers and a quote that must come from the source), short notes for `tag_batch` (labels that must and must not be chosen), and general questions with and without a false premise). `scripts/eval.py` runs each roster model alone, and optionally the whole classify route as a quorum, then prints a table of correct answers, schema failures, rate limits and median latency. Rate limits are waited out and counted, never scored as wrong answers.

```bash
MINION_ROSTER=my-roster.json uv run python scripts/eval.py --quorum --out results.json
```

Scoring is deliberately simple (exact label, field containment, a refusal pattern for false-premise questions), so read the raw outputs saved with `--out` before you trust a single miss. Use a small `--max-calls name=N` for models on free tiers with daily caps.

## Security note

The `*_file` tools read **any UTF-8 text file** the caller names (up to the size cap), and `classify_files` and `tag_files` read up to 50 such files from any folder the caller names. Whoever can call this server can therefore read files your user account can read. `scan_diff` widens this read surface further: it reads the added lines and commit messages of any git work tree your account can read, through `git` (see [Diff scan](#diff-scan-scan_diff) for the checks on refs and how git is run). Only connect clients you trust, and leave the file tools and `scan_diff` out of any setup where that is a problem.

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
