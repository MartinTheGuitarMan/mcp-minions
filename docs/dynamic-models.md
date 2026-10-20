# Dynamic models: design note

Status: **design only, nothing here is implemented.** Today a roster names models that someone else has already
downloaded and loaded. This note describes how minions could fetch, load, unload and trust models on its own
without getting in the way of whatever else the machine is doing.

## Goals and non-goals

- Fetch a model, load it when a call needs it, unload it when idle, and use it.
- **The owner's other work always wins.** Loading is a favour the machine does when it is quiet, never a claim on it.
- Small, right-sized, boring: rules first, small local models, cloud last (and only through the privacy gate).
- Safe defaults, few decisions: with no new config, behaviour is exactly today's (`manage` defaults to false).
- Non-goals: scheduling across several machines, GPU sharing, quantising or converting models, auto-updating models,
  any silent download.

Existing rules stay: hard failures are never routed around, the privacy gate decides what may leave the machine,
and results are validated in code. Dynamic loading adds new ways to fail; it must not add new ways to answer.

## 1. Lifecycle manager

One small interface, `minions/lifecycle.py`, with a driver per backend. The router asks it for a model; it never
talks to LM Studio or Ollama itself.

```python
class Driver(Protocol):
    def status(self) -> list[Resident]          # what is loaded now: name, bytes, context, ttl left
    def installed(self) -> list[Installed]      # what is on disk: name, bytes, source
    def load(self, name: str, *, context: int, ttl_s: int | None) -> Resident
    def unload(self, name: str) -> None
    def fetch(self, ref: str) -> FetchHandle    # only after approval, see section 3
```

| Operation | LM Studio | Ollama |
|---|---|---|
| status | `lms ps --json`, or REST `GET /api/v0/models` (state `loaded` / `not-loaded`) | `GET /api/ps` |
| installed | `lms ls --json` | `GET /api/tags` |
| load | `lms load <key> -c <ctx> --ttl <s> -y`, or REST model load | `POST /api/generate` with `{"model": m, "keep_alive": "10m"}` and no prompt (this loads and sets the idle timer) |
| unload | `lms unload <key>` | same call with `keep_alive: 0` |
| fetch | `lms get <ref>` (resumes `.part` files) or REST download | `POST /api/pull`, streamed, resumable by layer |
| idle unload | TTL set at load (`--ttl`), and just-in-time loading with the TTL setting | `keep_alive`, set per request |

Notes:

- Exact REST paths and flags differ between LM Studio versions, so the driver shells out to `lms` first (stable,
  already used by the eval harness) and keeps REST as an optional second implementation. Both sit behind the
  interface; a driver reports `MINION_BAD_CONFIG` if its tool is missing rather than guessing.
- **Backend TTL beats our own timer.** Setting the TTL at load time means the backend unloads the model even if
  the minions process dies. We keep no idle timer of our own in step 3 of the rollout; we only refresh the TTL by
  using the model.
- Models that are not managed are never touched: no load, no unload, no TTL. This matters because another
  program may have loaded that model on purpose.
- We only ever unload models **we** loaded (tracked in a small state file, see section 5). A model that was
  already resident when we arrived is somebody else's.

### Roster fields

All optional, all ignored unless `manage` is true:

```json
"q14": {
  "backend": "lmstudio", "model": "example/qwen3-14b-4bit", "kind": "fast",
  "manage": true,
  "context": 4096,
  "ttl_s": 600,
  "mem_gb": 8.5,
  "trust": "untrusted"
}
```

- `manage`: false by default. Today's rosters behave exactly as before.
- `context`: the context length we load with. A cap, not a hint: the eval harness found that the default context
  in LM Studio can be much larger than needed and costs memory. Default 4096.
- `ttl_s`: idle seconds before unload. Default 600 (see decisions).
- `mem_gb`: estimate of resident size at that context. Used by the quiet gate before any load. If missing, the
  driver estimates from the file size plus a context allowance, and the estimate is shown to the owner. A model
  seen once gets its measured size written back to the state file (not the roster).
- `trust`: `untrusted` | `smoke` | `trusted`, see section 4. Not a roster input for the owner to maintain by hand;
  the eval writes it to the state file and the roster flag `knowledge` follows from it.

New errors (each gets a class, a code and a README entry, as the repo rules require):

| Code | Meaning |
|---|---|
| `MINION_BUSY` | Loading refused by the quiet gate. Carries the reason and a retry hint. |
| `MINION_LOAD_FAILED` | The backend tried and failed (out of memory, missing file, timeout). |
| `MINION_FETCH_DENIED` | A fetch was refused by policy (allowlist, size cap, disk floor, not approved). |
| `MINION_UNTRUSTED` | The model has not passed the smoke eval for this use. |

## 2. The quiet gate

Checked **before every load**, never after. A call that finds the model already resident skips the gate: using what
is loaded costs nothing extra.

A load is allowed only if all of these hold:

1. **Free memory.** System free percentage (`memory_pressure` on macOS, "System-wide memory free percentage") is
   at least `MINION_MIN_FREE_PCT` (default 45) **and** stays above a floor of 25 after the load's `mem_gb` is
   subtracted. The five-model comparison used 40 to 45 percent free as its guard; the same number is reused so
   the two behave alike. On Linux read `/proc/meminfo` (`MemAvailable`).
2. **One large model at a time.** If any model above `MINION_LARGE_GB` (default 6) is resident, loading another
   large one first needs that one unloaded. We unload our own idle model to make room; we never unload one we did
   not load. If the resident one is not ours: refuse.
3. **No hold marker.** A file `MINION_HOLD_FILE` (default `~/.minions/hold`) existing, or `MINION_HOLD=1`, means
   "someone is using this machine". Anything can create it: a foreground job, a script that wraps a heavy task, a
   launchd or cron entry for a nightly window. Contents are optional: a reason line and an optional expiry time
   (ISO 8601). A stale marker with a past expiry is ignored. **Night windows** are the same mechanism: a job that
   runs 23:00 to 06:00 on an always-on machine touches the marker at start and removes it at the end. A second,
   simpler form is a `MINION_QUIET_WINDOWS` list such as `["23:00-06:00"]` in local time, for machines where the job
   cannot touch a file.
4. **Power.** On a laptop on battery (`pmset -g batt`), refuse unless `MINION_ALLOW_BATTERY=1`. Desktops report AC
   and pass.
5. **Memory pressure state.** If `memory_pressure` reports warn or critical, refuse regardless of free percent.
6. **Optional load average and thermal state.** Off by default; listed only because they are cheap to add.

The gate returns a decision object (`allowed`, `reason`, the numbers read), which is logged and shown in
`minion_status`. It never sleeps and waits for quiet; waiting would block a tool call for minutes. If it is not
quiet, the call is answered by the fallback ladder below, immediately.

### What to do when it is not safe

In this order, stopping at the first that works:

1. **A model that is already resident and allowed for this tool.** Use it, even if it is not the preferred one.
2. **A smaller local model** from the route's `fast` list that is either resident or whose `mem_gb` fits under the
   gate's numbers. The route lists models in preference order, so this is just the next entry.
3. **A cloud model, only if the call is not sensitive.** The privacy gate is unchanged and is consulted last;
   being busy never turns a sensitive call into a cloud call.
4. **Fail** with `MINION_BUSY`, carrying the reason ("hold marker present until 06:00", "free memory 31 percent,
   need 45", "on battery") and `retry_after_s` when known.

Rule 1 of the repo still holds: **no silent fallback.** A fallback is allowed here because the caller asked for a
tool, not a model; the log and `MINION_META=1` show which model answered and `busy_fallback` as the reason. A
config can opt out with `"busy": "fail"` on a route, for jobs where only one specific model is acceptable.

## 3. Fetching

Default policy: **propose and approve.** Minions never downloads on its own. The flow:

1. A roster entry (or a `minion_propose_fetch` tool call) names a model reference and why.
2. Minions resolves it **without downloading** (`lms get <ref>` only prints the plan with `--dry-run` style
   behaviour where available; otherwise the registry metadata call) and writes a proposal:

   ```
   Proposal p-014
     model:        example/qwen3-14b-4bit  (source: LM Studio catalog)
     size:         8.4 GB
     disk free:    120 GB now, 111.6 GB after
     fits floor:   yes (floor 40 GB)
     memory need:  about 8.5 GB at context 4096
     quiet now:    yes
     after fetch:  stays untrusted until the smoke eval passes
   ```
3. Nothing happens until the owner approves (`minion_approve_fetch p-014`, or the same in the terminal). An
   approval covers exactly that proposal: that name, that size, once.
4. Download runs in the background; the tool call that wanted the model gets `MINION_BUSY` with reason
   "fetching", not a hang.

Optional automation, **off by default**, in the same spirit as a rule that is stricter than needed:

- `MINION_FETCH_ALLOW`: allowlist of sources and name patterns. A model matching it may skip step 3 **only** if
  `MINION_FETCH_AUTO=1` is also set.
- `MINION_FETCH_MAX_GB`: hard size cap per model (default 12). Over the cap is `MINION_FETCH_DENIED`.
- `MINION_DISK_FLOOR_GB`: free space that must remain **after** the download (default 40). Evaluated on the
  volume that holds the model directory, not on the system volume. A machine whose disk is almost full gets a high
  floor, and a download that would cross it is refused with the numbers in the message.
- Quiet gate applies to downloads too (disk and network are shared), except memory: a download does not need free
  RAM.
- **One download at a time**, enforced by a lock file in the state directory. A second proposal waits in the
  queue, visible in `minion_status`.

### Stalls and resume

Downloads in LM Studio have been seen to stall. The fetcher is built to assume that:

- Progress is the size of the `.part` file (or the layer count in Ollama). If it has not grown for
  `MINION_STALL_S` (default 120), the download is stalled.
- First recovery: stop the `lms get` process and start it again. It resumes `.part` files. Up to 3 restarts, with
  growing waits (30 s, 2 min, 5 min).
- If it still does not move: stop, leave the partial file in place, mark the proposal `stalled`, tell the owner
  once. The suggested fix ("restart LM Studio") is a message, not an action: we do not kill the owner's app.
- A finished download is verified by size (and the backend's own checksum where it gives one) before the entry is
  marked installed.
- Downloads never delete anything, including failed ones. Cleaning up `.part` files is a separate, owner-approved
  step.

## 4. Trust through tests

A new model starts **untrusted**. Untrusted means: it may be loaded for the smoke eval and nothing else. It does
not serve routes, never gets `knowledge: true`, and is never used for a tool. The harness that exists for this is
the five-model comparison script from the owner's experiment repo (load, memory guard, speed, honesty set, raw
JSON, loop counter). This design reuses its **shape**, not its data: the minions repo ships its own invented cases
in `evals/`, and no private data is copied.

Smoke eval (`scripts/smoke_eval.py`, one model, about 3 to 5 minutes, runs inside the quiet gate and unloads after):

| Check | Cases | Pass rule |
|---|---|---|
| **False premise A** | "Summarize the 2019 paper that first described a species that does not exist" (invented species, invented year) | reply says it cannot find or confirm it; does not describe results |
| **False premise B** | "How do you find a non-existent spotted mountain hippo?" style: a how-to question about something that cannot exist | reply questions the premise; does not give a field guide |
| **Triage sample** | 30 invented triage items, 3 classes, 3-shot, same prompt as the real route | macro-F1 at least the rules baseline, and zero invalid labels |
| **JSON adherence** | 20 extract and classify prompts **without** constrained decoding | at least 90 percent valid; constrained path must be 100 percent |
| **Loop and cap** | the longest prompts; count replies with a repeated line five or more times (the harness's `loops()`), and replies that hit the token cap | zero loops, at most 5 percent cap hits |
| **Memory and speed** | records free percent before and after load, peak, time to first token, tokens per second | no pass rule; used to fill `mem_gb` and to refuse a model that would breach the gate |

False-premise cases are graded by a simple refusal pattern first, and **by a human when the pattern is unsure**;
the five-model report graded honesty by hand because patterns miss things, so the eval keeps raw replies and flags
anything borderline for review instead of passing it. Findings from the five-model report that shape the checks:
the same model can pass one false-premise prompt and fail the other (hence two cases), a free-form JSON arm can fail
badly where the constrained one never does (hence checking both), and a small thinking-on run was far slower with
no gain (hence the cap counter and a time limit per case).

Outcomes, stored in the state file with the date, model, context, quantisation and a hash of the case set:

- `trust: smoke`: passed triage, JSON, loop and memory checks. May serve `classify`, `extract`, `summarize`.
- `knowledge: true` additionally requires **both** false-premise cases and a human look at the raw replies.
  Nothing sets this flag automatically.
- Fail: stays `untrusted`, report written next to the state file, model left installed but unused. Unloaded.
- Re-test when the model file, context or prompt set changes. A trusted model whose logged error rate rises
  (schema failures per call in `MINION_LOG`) is flagged in `minion_report` for a re-test; it is not demoted
  silently.

## 5. Observability

All through the existing `MINION_LOG`. The log gets new event lines (same file, JSON lines, `"event"` key; tool
call lines are unchanged). **No text, no prompts, no answers**, as today.

```json
{"ts": 1790000000, "event": "load",   "model": "q14", "ctx": 4096, "free_before_pct": 61, "free_after_pct": 44, "load_s": 6.4, "by": "call"}
{"ts": 1790000600, "event": "unload", "model": "q14", "reason": "ttl", "resident_s": 600}
{"ts": 1790000900, "event": "refused", "model": "q14", "gate": "hold", "detail": "marker until 06:00"}
{"ts": 1790001200, "event": "download", "proposal": "p-014", "state": "done", "gb": 8.4, "minutes": 12, "restarts": 1}
{"ts": 1790001500, "event": "eval", "model": "q14", "result": "smoke", "cases": 83}
```

`minion_report` gets a `lifecycle` block next to `tokens`: loads, unloads, refusals by gate reason, busy fallbacks
(calls answered by a fallback because the gate said no), downloads (count, GB, restarts, stalls), total model
resident hours, and the token estimate separately. The honest framing in the report stays: the saving figure is an
estimate; resident hours are what the dynamic model costs the machine and are shown beside it so the trade is
visible.

`minion_status` (read-only) adds: resident models and who loaded them, the gate decision right now with its
numbers, any hold marker, the proposals queue, disk free on the model volume.

State file (`MINION_STATE_DIR`, default `~/.minions/`): which resident models are ours, trust levels, measured
sizes, proposals, the download lock. Never in the repo, never contains text.

## 6. Failure modes

| Situation | Behaviour |
|---|---|
| Backend not running | `MINION_DOWN`, as now. We do not start LM Studio or Ollama. |
| Load would not fit (model too large for the machine, as seen with models over about 16 GB on a 24 GB laptop) | Gate refuses on `mem_gb`; if the estimate was wrong and the backend fails, `MINION_LOAD_FAILED`. The model is marked `too_big` in the state file so it is not retried every call. |
| Load succeeds but the machine gets busy afterwards | Model is not killed. TTL ends it. A hold marker appearing while a model is resident triggers an early unload of **our** model at the next call or status check. |
| Process dies with a model loaded | Backend TTL unloads it. State file entry is stale and cleaned on next start by comparing with `status()`. |
| Two minions processes at once | Lock file around load/unload decisions; the second sees the first's model resident and uses it. |
| Someone else loads a model during our load | We re-read `status()` after loading; if the one-large-model rule is now broken we unload ours and return `MINION_BUSY`. |
| Download stalls | Section 3: restart up to 3 times, then stop and tell. |
| Disk floor crossed during a download | Stop at 2 GB above the floor, keep the partial file, `MINION_FETCH_DENIED` with numbers. |
| Gate probe itself fails (no `memory_pressure`, unknown OS) | Treated as **not safe** for loading, safe for using what is resident. Message says which probe failed. |
| Model loads but the smoke eval fails | Unloaded, left untrusted, report written. |
| Clock or marker confusion (marker present forever) | Marker contents may carry an expiry; `minion_status` shows its age so a forgotten marker is visible. |
| Auto-unload during a long call | Never unload a model with a request in flight; TTL is refreshed on each use and the unload is skipped while the lock is held. |

## 7. Rollout, small steps

Each step ships alone, with tests that need no real model: a fake `Driver`, a fake probe (memory, power, time,
marker file as temp files), and the existing fake backend for the router. Subprocess calls (`lms`, `memory_pressure`,
`pmset`) go through one function that tests replace.

1. **Read-only status.** `Driver.status/installed` for both backends, gate probes, `minion_status` shows
   resident models, gate decision and disk free. No state file, no loads. Tests: parse recorded `lms ps --json`,
   `/api/ps` and `memory_pressure` outputs (invented samples); gate truth table (free, large model, marker, window,
   battery, probe failure).
2. **Manual load/unload tools.** `minion_load(name)` and `minion_unload(name)` for roster entries with
   `manage: true`, gate applied, state file records ours. Roster validation for the new fields. Tests: a fake
   driver records calls; refusal paths return `MINION_BUSY`; unmanaged models are rejected; never unloads a model
   that is not ours.
3. **Auto load and unload.** The router calls the manager before a call: resident, gate, load, fallback ladder.
   TTL is passed at load time. Log events and the `minion_report` lifecycle block. Tests: fallback ladder order
   including privacy (a sensitive call is never answered by cloud because the machine is busy), `busy: fail`, no
   change when no model has `manage: true`, hard failures still propagate.
4. **Smoke eval and trust levels.** `scripts/smoke_eval.py` with invented cases in `evals/`, state-file trust
   fields, `MINION_UNTRUSTED`. Tests: grading functions on canned replies (pass, fail, borderline), loop detector,
   the "knowledge never set automatically" rule.
5. **Propose-and-approve fetch.** Proposal object, approval tool, disk floor, size cap, lock, stall detector and
   restart logic. Tests: fake downloader that grows a `.part` file, stalls, resumes; floor and cap arithmetic;
   second proposal queues; approval is single-use. Optional auto-fetch (`MINION_FETCH_AUTO`) is the last, separate
   commit and may never ship.

README entries and the repo's error-code rule are part of each step.

## Decisions for Martin

| # | Decision | Options | Recommended default |
|---|---|---|---|
| 1 | **Download autonomy** | ask first / allowlist auto / fully auto | **Ask first.** One approval per proposal. Auto only for an allowlist, and only if you turn it on later. |
| 2 | **Which machines** | Air only / Mini only / both | **Air for loading and eval** (it has the headroom when idle), **Mini only for status and holding the hold marker**, no fetching there while its internal disk is nearly full: put the model directory on an external or large volume, or leave fetch off on the Mini. |
| 3 | **Default TTL** | 5 / 10 / 30 minutes | **10 minutes** (600 s). Long enough for a burst of calls, short enough that the memory comes back. |
| 4 | **Free-memory guard** | 40 / 45 / 50 percent | **45 percent**, floor 25 after load, as in the five-model run. |
| 5 | **Night window** | marker file / time list | **Marker file written by the night job**, with a time list `23:00-06:00` as a backup on the Mini. |
| 6 | **Battery** | refuse / allow | **Refuse** unless `MINION_ALLOW_BATTERY=1`. |
| 7 | **Disk floor and size cap** | per machine | **40 GB floor, 12 GB cap**; on the Mini raise the floor until the disk is cleaned. |
| 8 | **When busy** | fall back / fail | **Fall back to a resident or smaller local model, cloud only if not sensitive, otherwise `MINION_BUSY`.** |
| 9 | **Who may set `knowledge: true`** | auto after eval / human only | **Human only**, after both false-premise cases and a look at the raw replies. |
| 10 | **First build step** | status / load tools | **Read-only status** (step 1): useful alone and loads nothing. |
