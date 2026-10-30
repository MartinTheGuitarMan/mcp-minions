# Tool trials

Rule: every tool idea goes straight to an eval, and a pass puts it in the tool list. This file fixes the pass
criteria **before anything was run**, then holds the results. Five candidates: `check_claims`, `commit_message`,
`dedupe`, `score`, `translate`. Each is one commit; a failed one is removed in its own commit and its result stays here.

## What each tool does and what the code checks

| tool | does | code check (a reply that fails it is an error, never returned) |
|---|---|---|
| `check_claims(text, source)` | splits `text` into claims, marks each supported or not against `source` | every claim has a `supported` boolean; a supported claim carries a quote found in the source and its numbers and capitalised names occur in the source; numbers and names of the text must not be dropped from the claims |
| `commit_message(diff)` | drafts a subject and body from a diff | subject non-empty and at most 72 characters, first word imperative, no final period; every file path named in the message is in the diff |
| `dedupe(items)` | groups near-identical texts in a batch of `{id, text}` | every id in exactly one group, no unknown ids |
| `score(text, criterion)` | rates a text 1 to 5 against a criterion | integer 1 to 5; the quote given as the reason is found in the text |
| `translate(text, target)` | Swedish to English (`en`) and back (`sv`) | every number survives and none is added; capitalised names survive and none is added (days, months and nationalities excepted); length stays within a sane ratio |

Each tool has a route in `config.py` (`TRIAL_FALLBACK`): a roster with no route under the tool's own name borrows the
route of `extract` (`check_claims`, `dedupe`, `score`) or `summarize` (`commit_message`, `translate`).
No new error codes: the checks raise the existing `MINION_NOT_IN_SOURCE`, `MINION_INVENTED`, `MINION_TOO_LONG`
and `MINION_SCHEMA`.

## Pass criteria (fixed before the first run)

A fixed set of invented cases per tool in `scripts/tool_trials.py` (15 `check_claims`, 14 `commit_message`, 13 `dedupe`,
15 `score`, 17 `translate`), hard cases included (prompt injection in the data, negation, sarcasm, near-miss
numbers and names, invented entities). Scored in code, never by a model.

A tool passes when a local model (`sensitive=true`), on the tool's default route, meets all of:

1. **At least 90% correct** on the set. `dedupe`: the whole partition of a case must be right. `score`: within 1 of the
   expected value, with a quote found in the text. A call the code check rejects counts as wrong.
2. **0 invented answers.** `check_claims` must mark claims about invented entities unsupported; `translate` must not add
   facts (a number or name that is not in the source). Counted on what is *returned*; replies the code check refused are
   counted separately ("refused").
3. **The code check catches planted bad output**, proven in `tests/test_trial_tools.py`.
4. ~~**p95 under 60 s per call** on the local model.~~ **Dropped 2026-10-30** (Martin: slow is fine as long as work can
   run in parallel). Latency (p50, p95) is reported only. A call that times out still counts as a failure.
5. Added 2026-10-30: each tool is shown to be safe under concurrent calls on the same route (`tests/test_trial_tools.py`,
   threads against the fake backend, plus a live check, see Results). Route advice prefers the better score over the
   faster model.

Procedure: the route's first model is tried first; if the tool fails, one other local model; the model that passed
is recorded. The prompt may be tuned against the set **at most once**; if so it is stated in the results.
Runs only on AC power and without memory pressure (`pmset -g batt`, `memory_pressure`; the runner waits, it never lowers
a limit). Calls from the same model run one after another.

## Model-fit eval (added before the first run)

Beyond the pass criteria above (criterion 4 as it was originally written is kept above, struck through), every tool's set is run against every usable roster model, **3 repeats per case**, one
model pinned at a time through a throwaway in-memory roster built from a copy of the roster file (the roster itself is
not changed; the file is only read).

- local: `air-qwen`, `air-gemma4-26b` (load on demand; if the quiet gate refuses it, that is recorded and retried later, the
  limit is not lowered), `mini-phi4mini`, `mini-gemma4b` (over the tunnel; if it is down: not run);
- decision models (`air-jev-style`): only where the tool fits a typed decision. None of the five does: a decision model
  returns a label and a probability, no quote and no text, and every check here needs a quote or text. Recorded as not run
  for that reason, not as a fail;
- cloud (`sensitive=false`, only because every case is invented and holds nothing real): `groq-gptoss20b`,
  `groq-gptoss120b`, `mistral-ministral8b`, `gemini-flashlite`. Per-model request limits are respected; an exhausted free
  tier is recorded as not run, never as a fail, and cloud runs stop at anything that looks like a paid limit.

Recorded per tool and model: score, stability (share of cases whose 3 repeats all agree), invented answers, code-check
failures by code, p50 and p95 latency, tokens per second (end to end, only where the provider reports counts), errors,
not-run reasons. An unreachable model is "not run", never a fail.

Output per tool: the fit table, pass or fail, and a recommended route (first model, fallback; a quorum only if it earns
its cost; local preferred when the score gap is small). The recommendation is advice; the roster is the owner's to change.

## Results

(filled in after the runs)
