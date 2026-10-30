# Tool trials

Rule: every tool idea goes straight to an eval, and a pass puts it in the tool list. This file fixes the pass
criteria **before anything was run**, then holds the results. Five candidates: `check_claims`, `commit_message`,
`score`, `translate`. Each is one commit; a failed one is removed in its own commit and its result stays here.

## What each tool does and what the code checks

| tool | does | code check (a reply that fails it is an error, never returned) |
|---|---|---|
| `check_claims(text, source)` | splits `text` into claims, marks each supported or not against `source` | every claim has a `supported` boolean; a supported claim carries a quote found in the source (case, accents, punctuation and thousands separators ignored; a quote not found downgrades that claim to unsupported with flag `quote_not_found`, added 2026-10-30) and its numbers and capitalised names occur in the source; numbers and names of the text must not be dropped from the claims |
| `commit_message(diff)` | drafts a subject and body from a diff | subject non-empty and at most 72 characters, first word imperative, no final period; every file path named in the message is in the diff |
| `dedupe(items)` | groups near-identical texts in a batch of `{id, text}` | every id in exactly one group, no unknown ids |
| `score(text, criterion)` | rates a text 1 to 5 against a criterion | integer 1 to 5; the quote given as the reason is found in the text |
| `translate(text, target)` | Swedish to English (`en`) and back (`sv`) | every number survives and none is added; capitalised names survive and none is added (days, months and nationalities excepted); length stays within a sane ratio |

Each tool has a route in `config.py` (`TRIAL_FALLBACK`): a roster with no route under the tool's own name borrows the
route of `extract` (`check_claims`, `dedupe`, `score`) or `summarize` (`commit_message`, `translate`).
No new error codes: the checks raise the existing `MINION_NOT_IN_SOURCE`, `MINION_INVENTED`, `MINION_TOO_LONG`
and `MINION_SCHEMA`.

> **Outcome: `dedupe` failed and its code was removed in its own commit** (results below). The other four passed.

## Pass criteria (fixed before the first run)

A fixed set of invented cases per tool in `scripts/tool_trials.py` (15 `check_claims` plus 15 hard ones added 2026-10-30, see "Follow-up", 14 `commit_message`, 13 `dedupe`,
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

Run on 2026-10-30. 222 calls per tool and model where run (3 repeats of every case). `score` = correct calls / calls scored; `stable` = share of cases whose three repeats agree; `invented` = answers returned that hold an invented fact (`check_claims`: an invented entity marked supported; `translate`: a number or name not in the source), `(+n refused)` = replies the code check rejected, which are counted as wrong but never returned; `tok/s` = completion tokens over end-to-end seconds, where the provider reported counts. Latency is reported only (the p95 bar was dropped, see criterion 4).

Summary against the pass criteria (local model, `sensitive=true`, the route's first model):

| tool | result | route's first local model |
|---|---|---|
| `check_claims` | **PASS** | air-qwen: 45/45, 0 invented, stable. Default route (`extract`) is already the right first model. |
| `commit_message` | **PASS** | air-qwen: 42/42. The three always-refused calls of run 1 were a check bug (a directory path, `/var/backups`, counted as a file), fixed; see Findings. |
| `dedupe` | **FAIL** | best local model reaches 77% (air-qwen and mini-phi4mini), below the 90% bar; air-gemma4-26b not run. Only `groq-gptoss120b` reaches 100% (public text only). |
| `score` | **PASS** | air-qwen: 42/45 (93%), within 1 of the expected value, quote always in the text. The three misses are one case (`s04`, a bare imperative) scored 1 instead of 3. |
| `translate` | **PASS** | air-qwen: 51/51, nothing added, nothing dropped. |

### `check_claims`: PASS

| model | where | score | stable | invented returned (+refused by code) | code-check failures | p50 s | p95 s | tok/s | not run |
|---|---|---|---|---|---|---|---|---|---|
| air-gemma4-26b | local | - | - | 0 (+0 refused) | - | - | - | - | {'busy': 45} |
| air-qwen | local | 45/45 (100%) | 1.0 | 0 (+0 refused) | - | 5.0 | 11.5 | 20.7 | - |
| gemini-flashlite | cloud | 45/45 (100%) | 1.0 | 0 (+0 refused) | - | 1.6 | 2.7 | 59.8 | - |
| groq-gptoss120b | cloud | 43/45 (96%) | 0.93 | 0 (+0 refused) | - | 1.4 | 2.0 | 333.3 | - |
| groq-gptoss20b | cloud | 45/45 (100%) | 1.0 | 0 (+0 refused) | - | 2.6 | 5.1 | 216.4 | - |
| mini-gemma4b | local | 27/45 (60%) | 1.0 | 0 (+12 refused) | {'MINION_NOT_IN_SOURCE': 12} | 6.7 | 12.1 | 16.2 | - |
| mini-phi4mini | local | 33/45 (73%) | 1.0 | 0 (+12 refused) | {'MINION_NOT_IN_SOURCE': 12} | 5.6 | 12.8 | 17.6 | - |
| mistral-ministral8b | cloud | 42/45 (93%) | 1.0 | 0 (+0 refused) | - | 1.1 | 2.3 | 89.4 | - |

**Recommended route:** first `air-qwen`; fallback `groq-gptoss20b` for public text only (100%, cloud). No local fallback earns its place: the two small tunnel models mark invented claims as supported with a made-up quote (60% and 73%); the code check refused every one of those (12 each), so nothing invented was returned, but the answers are not usable. No quorum needed.

### `commit_message`: PASS

| model | where | score | stable | invented returned (+refused by code) | code-check failures | p50 s | p95 s | tok/s | not run |
|---|---|---|---|---|---|---|---|---|---|
| air-gemma4-26b | local | - | - | 0 (+0 refused) | - | - | - | - | {'busy': 42} |
| air-qwen | local | 42/42 (100%) | 1.0 | 0 (+0 refused) | - | 2.3 | 3.5 | 20.3 | - |
| gemini-flashlite | cloud | 42/42 (100%) | 1.0 | 0 (+0 refused) | - | 1.0 | 11.2 | 42.4 | - |
| groq-gptoss120b | cloud | 42/42 (100%) | 1.0 | 0 (+0 refused) | - | 0.8 | 2.4 | 257.1 | - |
| groq-gptoss20b | cloud | 42/42 (100%) | 1.0 | 0 (+0 refused) | - | 3.0 | 5.1 | 130.0 | - |
| mini-gemma4b | local | 42/42 (100%) | 1.0 | 0 (+0 refused) | - | 1.9 | 3.7 | 12.0 | - |
| mini-phi4mini | local | 39/42 (93%) | 1.0 | 0 (+0 refused) | {'MINION_SCHEMA': 3} | 2.1 | 2.8 | 17.0 | - |
| mistral-ministral8b | cloud | 41/42 (98%) | 0.93 | 0 (+1 refused) | {'MINION_INVENTED': 1} | 0.8 | 1.1 | 69.5 | - |

**Recommended route:** first `air-qwen`; fallback `mini-gemma4b` (100% local). No quorum.

### `dedupe`: FAIL

| model | where | score | stable | invented returned (+refused by code) | code-check failures | p50 s | p95 s | tok/s | not run |
|---|---|---|---|---|---|---|---|---|---|
| air-gemma4-26b | local | - | - | 0 (+0 refused) | - | - | - | - | {'busy': 39} |
| air-qwen | local | 30/39 (77%) | 1.0 | 0 (+0 refused) | {'MINION_SCHEMA': 3} | 2.6 | 4.1 | 20.2 | - |
| gemini-flashlite | cloud | 36/39 (92%) | 1.0 | 0 (+0 refused) | - | 1.2 | 26.5 | 22.2 | - |
| groq-gptoss120b | cloud | 39/39 (100%) | 1.0 | 0 (+0 refused) | - | 0.9 | 1.5 | 288.1 | - |
| groq-gptoss20b | cloud | 37/39 (95%) | 0.92 | 0 (+0 refused) | - | 1.8 | 4.6 | 147.8 | - |
| mini-gemma4b | local | 15/39 (38%) | 1.0 | 0 (+0 refused) | {'MINION_SCHEMA': 3} | 1.6 | 3.1 | 13.4 | - |
| mini-phi4mini | local | 30/39 (77%) | 1.0 | 0 (+0 refused) | - | 1.7 | 3.0 | 16.7 | - |
| mistral-ministral8b | cloud | 30/39 (77%) | 1.0 | 0 (+0 refused) | - | 0.6 | 2.0 | 48.3 | - |

**Recommended route:** none for sensitive text. For public text `groq-gptoss120b` would do, but the tool's pass bar is local, so the tool is removed from the code (own commit); the result stays here.

### `score`: PASS

| model | where | score | stable | invented returned (+refused by code) | code-check failures | p50 s | p95 s | tok/s | not run |
|---|---|---|---|---|---|---|---|---|---|
| air-gemma4-26b | local | - | - | 0 (+0 refused) | - | - | - | - | {'busy': 45} |
| air-qwen | local | 42/45 (93%) | 1.0 | 0 (+0 refused) | - | 1.2 | 1.7 | 19.8 | - |
| gemini-flashlite | cloud | 42/45 (93%) | 1.0 | 0 (+0 refused) | - | 0.9 | 1.7 | 28.8 | - |
| groq-gptoss120b | cloud | 45/45 (100%) | 1.0 | 0 (+0 refused) | - | 0.7 | 1.2 | 243.2 | - |
| groq-gptoss20b | cloud | 39/45 (87%) | 1.0 | 0 (+0 refused) | - | 0.6 | 4.3 | 296.2 | - |
| mini-gemma4b | local | 36/45 (80%) | 1.0 | 0 (+0 refused) | - | 1.8 | 2.5 | 12.9 | - |
| mini-phi4mini | local | 45/45 (100%) | 1.0 | 0 (+0 refused) | - | 1.6 | 2.1 | 15.6 | - |
| mistral-ministral8b | cloud | 42/45 (93%) | 1.0 | 0 (+0 refused) | - | 0.6 | 1.2 | 40.4 | - |

**Recommended route:** first `mini-phi4mini` (45/45, local over the tunnel), fallback `air-qwen` (93%, local on this machine). The gap is one case; if the tunnel is a worry, `air-qwen` alone is fine. No quorum.

### `translate`: PASS

| model | where | score | stable | invented returned (+refused by code) | code-check failures | p50 s | p95 s | tok/s | not run |
|---|---|---|---|---|---|---|---|---|---|
| air-gemma4-26b | local | - | - | 0 (+0 refused) | - | - | - | - | {'busy': 51} |
| air-qwen | local | 51/51 (100%) | 1.0 | 0 (+0 refused) | - | 1.3 | 1.9 | 18.8 | - |
| gemini-flashlite | cloud | 49/51 (96%) | 0.94 | 0 (+0 refused) | - | 0.8 | 1.3 | 28.9 | - |
| groq-gptoss120b | cloud | 46/51 (90%) | 0.94 | 0 (+0 refused) | - | 0.8 | 1.3 | 296.5 | - |
| groq-gptoss20b | cloud | 45/51 (88%) | 1.0 | 0 (+0 refused) | - | 0.6 | 1.4 | 356.2 | - |
| mini-gemma4b | local | 51/51 (100%) | 1.0 | 0 (+0 refused) | - | 1.6 | 2.6 | 13.9 | - |
| mini-phi4mini | local | 36/51 (71%) | 1.0 | 0 (+6 refused) | {'MINION_NOT_IN_SOURCE': 3, 'MINION_INVENTED': 3} | 1.4 | 1.9 | 16.2 | - |
| mistral-ministral8b | cloud | 51/51 (100%) | 1.0 | 0 (+0 refused) | - | 0.9 | 2.3 | 23.1 | - |

**Recommended route:** first `air-qwen`; fallback `mini-gemma4b` (51/51, local). `mini-phi4mini` is not usable (71%, it adds names and drops numbers; 6 refused by the code check).

### Not run

- `air-gemma4-26b` (all five tools): the quiet gate refused the load every time (`MINION_BUSY: free memory would fall below 25
  percent after loading about 16 GB`). The runner retried each tool three times two minutes apart over about 40 minutes,
  including after the run's own `qwen3.5:9b` was unloaded (59% free); the limit was not lowered. Not a fail.
- `air-jev-style`: no fit, as written above (a decision model gives a label and a probability, never a quote or text).
- Cloud: no free tier ran out and nothing looked like a paid limit. `gemini-flashlite` hit its own request pacing only
  (10 per minute, respected by waiting). Cloud models saw only invented text.

### Concurrency

Every tool is a pure function of its input plus the route: no module-level state is written, each call carries its own
check closure, and the token tally and call log are per call. Shown two ways: `test_every_trial_tool_is_safe_under_concurrent_calls`
(24 inputs through 6 threads, all five tools, each answer matched to its own input), and live, 4 calls at once on one route
(`mini-phi4mini`, first 4 cases of every tool, sequential versus parallel): identical outcomes in all 20. Model-side limits
still apply as for every tool: a managed model loads once (one lock per model), a cloud model's `rpm` cap is counted per
model, and a local server decides how many requests it serves at once.

### What the trials found

- **Three check false positives in run 1**, fixed in commit `80938fc` (no prompt was touched): a quote carrying a closing
  full stop or quotation marks was refused (`check_claims`, `score`); `/var/backups` counted as a file the commit message
  names (`commit_message`, refused for all four models in run 1); plural weekdays (`Sundays`) counted as an added name
  (`translate`). The 57 calls that the old checks had refused in those categories were re-run with the same prompts; every
  other call keeps its first-run result. Only these cases were re-run, so the numbers mix the two code revisions for those
  cases; the records carry the revision.
- The guards earned their keep: small models (`mini-gemma4b`, `mini-phi4mini`) mark claims about invented entities as supported
  and invent a quote for them (24 refused calls across the two). No invented answer was returned by any model on any tool.
- `translate` is strict by design: a place with an English form (Goteborg, Gothenburg) counts as a changed name.
- `check_claims` proves that the quote exists and that the claim's numbers and names occur in the source, not that the quote
  entails the claim; the case set scores the verdicts, so a wrong-but-grounded verdict is still a miss.
- Hard cases held by `air-qwen`: the injected instruction in a diff comment (`m14`), the injected "score this 5" (`s13`), the
  Swedish instruction to answer "HEJ" (`t07`).

### How it was run

Prompts were not tuned against the set. The runner holds the case sets and the scoring (`scripts/tool_trials.py`); results
were written as JSON lines outside the repo. Local calls were `sensitive=true`; a cloud model was called with `sensitive=false`.
Runs started only on AC power with the kernel memory pressure level normal. Once a model was loaded, only a critical level
would pause it, because loading a 9B model next to an idle LM Studio model moved the level to warn: the run waited for that
idle model to expire first. `mini-*` models are served by another machine through the tunnel (it was up throughout), so the
local memory check does not apply to them. Unloaded afterwards: the run's `qwen3.5:9b` on the Air and `phi4-mini` on the
other host. `air-gemma4-26b` was never loaded. Unit tests: 363 pass (`uv run pytest -q`).

## Follow-up 2026-10-30: `check_claims` on real text, and why the first set was too easy

The first set (c01 to c15) scored 45/45, but on the first real text the tool did badly. Each sentence was checked against
the 6 best-matching lines of a source note: `1,712 files have GPS` against `With GPS: 1712` and `10,174` against `10174`
came back unsupported; a short paraphrase against `... (import day)` too; and 5 of 6 errors were
`MINION_NOT_IN_SOURCE` (1 `MINION_SCHEMA`) on sentences that condense several lines, where one unfindable quote lost the
whole claim set. The first set was too easy because it is prose, one fact per sentence, claims that come back nearly
verbatim, so quotes were always exact copies. Real sources are table-like lines and real sentences condense them.

Fix (code, not prompt): quotes are matched with the same light normalisation `values.py` uses (case, accents,
punctuation, thousands separators in numbers; whole words, no fuzzy matching). A supported claim whose quote is still not
found is downgraded to unsupported with `"flag": "quote_not_found"`; the other claims stand. A reply that does not fit the
schema still raises, a supported claim with a number or name the source lacks still raises `MINION_INVENTED`, and a
planted invented quote is still caught (`tests/test_trial_tools.py`). The prompt was not changed.

15 hard cases (h01 to h15) were added: number formats, table-like lines, sentences that condense two lines, a short
paraphrase, near-miss numbers, invented entities. `air-qwen`, `check_claims` only, 3 repeats, old code (`e60e919`) against
new code, same 30-case set:

| code | first set (45 calls) | hard set (45 calls) | invented returned | check failures |
|---|---|---|---|---|
| old | 45/45 | 39/45 | 0 | `MINION_INVENTED` 3 (h09) |
| new | 45/45 | 39/45 | 0 | `MINION_INVENTED` 3 (h09) |

No difference: on invented text `air-qwen` quoted exactly and wrote number formats the way the source has them, so the
failures of the real run did not reproduce, and these cases do not prove the fix on the model. What proves it is the fake-backend tests (number
formats, case, punctuation, a quote that is not in the source, one bad quote among good claims). The two misses are
findings about the cases and the check, not about the fix: h09 (`4 March 2026` against `2026-03-04`) is refused as
`MINION_INVENTED` because the month name counts as a name the source lacks, in the old and the new code alike; h13 mixes a
wrong firmware number with a right site name in one claim, so one verdict cannot satisfy both keys (a case flaw, left in
because both runs share it). The set still needs cases taken from real notes (private, kept out of this repo).

## Follow-up 2026-10-30 (2): two holes left in `check_claims`, and their trial

Criteria fixed before the new case set was written into `scripts/tool_trials.py` and before any run of it.

Real runs (one sentence at a time against the best-matching lines of a source note, `air-qwen`) showed two holes:

1. **Numbers.** The model itself says "unsupported" for a true claim when the number format differs (`1,712 files have
   GPS` against the line `With GPS: 1712`). There is no flag, so the quote fix never runs. Fix in code: an unsupported
   claim with at least one number is upgraded to supported (flag `rechecked_in_code`, quote = the source line) only when
   every number of the claim, read as a whole number with separators ignored, occurs in one source line, at least 50%
   of its other content words occur in that line (stemmed, stop words and "percent" ignored), the line and the claim
   agree on negation (missing, no, not, without, lack, fail), and the claim's names pass the existing name check. A
   wrong number is never in a line, so it stays unsupported. Claims without a number are never upgraded.
2. **Extra claims.** Given one sentence, the model returns claims taken from the source, not from the text. Fix in
   code: a returned claim needs at least 65% of its content words and numbers in the text (stemmed); the others are
   removed and listed under `dropped_not_in_text`. The threshold comes from the unit tests: a claim with 3 of 5 content
   words in the text (a source fact) is dropped, 2 of 3 and 4 of 6 (a reworded condensed claim) stay. The existing
   check that no number or name of the text drops out of the claims is unchanged.

**Set:** 24 new invented cases (`scripts/tool_trials.py`, ids `r01` to `r24`, runner name `check_claims_holes`): table-like
source lines, number formats, condensed sentences, wrong numbers (r05, r06, r07, r16, r20, r24), claims missing from the source, and
cases where the source holds a tempting extra fact (r09, r10, r17, r23). `air-qwen`, `check_claims` only, 3 repeats, AC power,
local (`sensitive=true`), roster read-only.

**Pass:** all of
- at least 90% of calls correct (a call the code check rejects counts as wrong);
- every wrong number stays unsupported (0 wrong-number claims returned as supported);
- 0 claims returned that are not in the text (also 0 invented-entity claims returned as supported).

The scorer counts the last two together as "invented" and the case id says which (list above); the record of a failing case is
reported as a failure, not tuned away. After the set is committed, no code or prompt changes: a fail is reported as a fail.

### Results of the two-holes trial (2026-10-30, code `9f1887c`, set committed in `e4d1de0`)

`air-qwen`, `check_claims_holes`, 24 cases x 3 repeats = 72 calls, AC power, local, roster read-only, other jobs may have
shared the Air. **PASS on all three bars:** 72/72 correct (100%), stable 1.0, 0 wrong numbers returned as supported,
0 claims returned that are not in the text, 0 code-check failures. Caveat: on these short invented cases `air-qwen`
did not itself produce the two failures (it answered the number-format cases correctly and added no extra claims), so
the trial shows that the new checks do no harm and let correct answers through; the code paths that fix the holes are
proven by the fake-backend tests (planted `unsupported` verdicts for true claims, planted wrong numbers, planted
source-only claims). Same limit as the first follow-up.

Real pages, read-only and local (`air-qwen`, one line or bullet of the page at a time against its 6 best-matching source
lines by word overlap; source citations and links removed from the page lines first; counts only):

| page | before | after |
|---|---|---|
| photo dates (6 sections) | 22 supported, 7 unsupported | 22 supported, 5 unsupported (1 of them rechecked in code), 16 claims dropped as not in the text |
| music plan | 93 supported, 12 flagged, 1 error (106) | 44 supported, 18 unsupported (5 `quote_not_found`), 1 error `no claims came back`, 22 dropped as not in the text |

The dropped claims account for the inflation (the earlier counts held claims taken from the source). Without removing the
page's own citations from each line the same run failed on 7 of 12 lines, because a citation's date is a number of the
text that no claim repeats; that is the old "claims leave out what the text says" check, unchanged, and a reason to strip
citations before calling the tool. The earlier before-counts may have used a different split of the page into lines, so
the comparison is indicative only.

## Idea evals 2026-10-30: six logged tool ideas

Criteria fixed before anything below was built or run. The rule is the same as above (every idea gets an eval, a pass is
kept), but the first question is what the idea *is*. A minion tool uses a model. Something that is plain code, with no
model, and only helps whoever builds or checks the tools, is not a minion: it goes in `scripts/` and stays out of the MCP
tool list. All six turned out to be plain code (decided below, before building), so none gets a route, an error code or a
README tool row. The eval still applies: a code tool passes on real inputs from this repo, scored against a result worked
out another way, and a fail is removed in its own commit with the result kept here.

| idea | model? | home | why |
|---|---|---|---|
| `strip_citations` | no | `scripts/strip_citations.py` (function + stdin/stdout) | text surgery on wiki lines before `check_claims`; a regex job. Its value is shown by what `check_claims` does with and without it. |
| `report_diff` | no | `scripts/report_diff.py` | compares two JSON lines written by `quick_eval.py`; arithmetic. |
| `trial_compare` | no (it *drives* trials, which use models) | `scripts/trial_compare.py` | diffs two `tool_trials.py` result sets per case; the `run` mode only starts the existing runner in two git worktrees. |
| `eval_urls` | no (it *scores* `summarize_url`, which uses a model) | `scripts/eval_urls.py`; `eval_summarize_url.py` becomes a thin caller | an eval harness for developers, the page list and key terms come from a file. |
| `check_url_safe` | no | `scripts/check_url_safe.py` (importable) | reuses `fetch.check_url` and `fetch.address_problem`; syntax and literal-address rules only. |
| `diff_tests` | no | `scripts/diff_tests.py` | runs pytest, so it executes code: only for trusted in-repo task files. Model-written code must go through the Docker sandbox of the coding bench. |

Security: nothing here adds a read path for minions. `check_url_safe` opens no socket and resolves no name (a test
makes any socket call fail); it can only say a URL is refused *without* a lookup, never that it is safe to fetch (the
fetch layer still resolves and pins at fetch time). `fetch.py` is not changed. `eval_urls` calls the existing
`summarize_url` and nothing else. `diff_tests` is not reachable from the server.

### Pass criteria (fixed first)

Common: real inputs from this repo wherever the tool reads something this repo already produces, invented data otherwise,
no personal data; at least 12 cases per tool with planted bad inputs the tool must catch; the output compared with a
result worked out another way (by hand, or by an independent count); no model needed to run the tests.

1. **`strip_citations`**: 14 or more lines in the real wiki shape (`(source: raw/<file>)`, `(sources: ...)`,
   `(conversation <date>)`, markdown and wiki links, footnote marks, bare urls), every output equal to the expected line,
   100%. No digit, date or word that is not part of a citation or a link target may change (checked on every case), and the
   function is idempotent. A parenthesis that is not a citation (`(import day)`, `(2026-03-04)`) stays. Value test, live on
   `air-qwen` (`sensitive=true`): 8 invented wiki lines, each citing a dated source, checked against an invented source
   note with and without stripping; with stripping there are 0 "numbers or names of the text dropped" errors, and no more
   errors in total than without it.
2. **`report_diff`**: the real accuracy line compared with itself gives no flag; 12 or more planted pairs (made by editing
   the real line) are flagged exactly: a score drop of 0.10 or more on a tool, any rise in `invented`, a tool that was
   scored and is not now, a new error code, a partial run, a different answering model set, a changed roster hash (note
   only), p95 more than twice as slow (note only). An improvement is never a regression. 100% of the pairs, no false flag
   on the unchanged ones.
3. **`trial_compare`**: on two real result sets (the `air-qwen` runs of the claims fix, old code against new), the per-case
   diff and the totals equal a count made independently (a one-off reading of the raw files); planted flips
   (ok to wrong, wrong to ok, a case missing on one side, a changed status) are all listed. `run` mode: the worktrees and
   commands are built and cleaned up correctly (tested on a throwaway git repo), and one live run of `check_claims` at the
   claims-fix parent and at `HEAD` (`air-qwen`, 1 repeat, local) produces a diff the tool prints without error.
4. **`eval_urls`**: 12 or more *new* real public pages (not the 20 of `eval_summarize_url.py`) with key terms chosen before
   the run; the harness's PASS/FAIL per page agrees with a by-hand reading of every printed summary (0 disagreements);
   planted summaries (no key term, an invented number, a call error, a bad page-list file) get the right verdict in tests.
   The 20-page default list moves to the new script unchanged.
5. **`check_url_safe`**: 20 or more URLs: real public ones from the lists in this repo (all must come out `ok`, and each
   must also be refused-free when the fetch layer's own resolution is run on it once, outside the tool), and attack forms
   (loopback, private, link-local, metadata, `localhost`, `.local`, `.internal`, decimal, hex and octal IP, IPv4-mapped
   IPv6, credentials in the url, a non-default port, a `file:` scheme) which must all be refused with the reason the fetch
   layer gives. 100%. A test makes every socket call raise, and the tool still answers.
6. **`diff_tests`**: on the coding bench's real tasks (`bench/code_tasks.py`, read from its branch, not copied into this
   repo): every task with a `buggy` version gives verdict `ok` (reference passes all its tests, buggy fails at least one),
   and every write task checked against a stub gives `ok`; planted bad sets are each caught with the right verdict:
   tests that pass on the buggy code, a reference that fails, tests that crash at import (no behaviour shown), zero tests,
   and an endless loop (timeout). 100%.

A fail is removed in its own commit and the result stays here. The prompt-tuning rule does not apply (no prompts).

### Result: `strip_citations`: PASS (stays in `scripts/`)

- **Cases:** 20 wiki-shaped lines (invented content; `(source: raw/...)`, `(sources: ...)`, `(conversation <date>)`, wiki and
  markdown links, footnote marks, bare and angle urls, images, a table row, an indented bullet, an empty line, and lines
  whose parentheses are not citations). 20/20 equal to the expected line; all idempotent; every number outside a citation
  survives (`tests/test_strip_citations.py`, 45 tests). One real bug found on the way and fixed before the run: bracket
  citations were matched before markdown links, so `[source](url)` left `(url)` behind.
- **Value test**, live, `air-qwen`, `sensitive=true`, 8 invented lines against an invented source note, 1 repeat
  (`evals/strip_citations_value.py`): as written 4 of 8 calls failed with `claims leave out what the text says` (the
  citation's date digits: `2026`, `10`, `10`), and 4 of 8 returned an extra unsupported claim made from the citation; stripped
  0 of 8 failed and every line came back with only its own claims. Bar met (0 "leaves out" errors, no more errors than
  without).
- **Limits:** only the citation shapes in the wiki's own rules are known; other shapes pass through untouched (never
  wrongly removed, per the tests). A link's text is kept, so `[[page|alias]]` leaves `alias` in the sentence. Frontmatter is not
  handled. Wiring it into the call path (a `strip_citations` option on `check_claims`) is a separate, later choice.

### Result: `report_diff`: PASS (stays in `scripts/`)

- The real `accuracy.jsonl` line (the one the weekly job wrote on 2026-10-30; only one exists so far) compared with itself:
  no flag. 16 planted pairs made by editing that line (score drop 0.25, exactly 0.10, 0.0625; a rise; a tool no longer scored;
  a tool missing; `invented` up; a new error code; an old one more often; the same count; partial; other answering models;
  roster hash; sensitive flipped; p95 doubled; p95 up under twofold), plus an older-version line, an improvement and
  a still-nonzero `invented`: all flagged exactly as written down, the unchanged ones not flagged (23 tests).
- Not verified: no second real line exists yet, so real-against-real has only the identity case; the 0.10 bar is the
  README's "one case is about 12 points" reading, not tuned on data.

### Result: `trial_compare`: PASS (stays in `scripts/`)

- **Real result sets:** the `air-qwen` `check_claims` runs of the claims fix, old code `e60e919` against new `c33be39`, 30
  cases x 3 repeats each. Tool output: 84/90 against 84/90, all 30 cases the same. Independent count (a one-off script over the
  raw files, not the tool's code): 84 ok on both sides, the only non-ok cases are `h09` (3 check failures) and `h13` (3 wrong)
  on both sides. Equal, and equal to the result recorded in "Follow-up" above.
- **Planted flips** (10 tests): ok to wrong, wrong to ok, wrong to check failure, a partly worse case, one repeat worse,
  a case only on one side, `not_run` records and the stop marker ignored, model and tool filters, a directory of model
  files, bad JSON named by line. All listed; exit code 1 when any case got worse.
- **`run` mode:** tested on a throwaway git repo (detached worktrees added, the runner executed in each, worktrees removed,
  dry run changes nothing, `--out` inside the repo refused). Live: `check_claims`, `air-qwen`, 1 repeat, local, old `c162857`
  against `8ccc2e3` (the claims fix is in the new code): 28/30 on both sides, 30 cases the same; the temp worktrees were
  removed. The two misses are `h09` and `h13`, as before.
- Not verified: the real data has no flip, so "lists a flip" rests on the planted tests; the live run has `--repeats 1`, so
  its stability column says nothing.
