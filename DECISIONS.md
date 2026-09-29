# DECISIONS.md

One line per decision: the decision, then why. SPEC 0 forbids asking questions,
so every ambiguity in SPEC.md is resolved here and logged rather than discussed.
`seed/*.json` is read-only and was never modified.

## SPEC deviations (SPEC was not edited; the deviation is recorded here)

- **D12 — Groq replaces Claude.** SPEC 7 names `claude-haiku-4-5-20251001` /
  `ANTHROPIC_API_KEY`. Per the user's instruction we use Groq
  (`openai/gpt-oss-120b` / `GROQ_API_KEY`) and drop Anthropic entirely. Model
  line confirmed against console.groq.com/docs/models and
  /docs/structured-outputs. All SPEC 7 guardrails are unchanged.
- **D13 — Python 3.14, not 3.11/3.12.** SPEC 6 prefers 3.11/3.12 but explicitly
  says to verify the install first when only 3.14 is available. Verified by
  running: every package has a 3.14 artifact, including the compiled transitive
  deps (pydantic-core, uvloop, httptools, watchfiles, PyYAML, aiohttp, multidict,
  yarl, frozenlist all ship cp314 wheels). Installed successfully on 3.14.3.

## Scoring engine (SPEC 4)

- **D1 — `clip` is a named constant in `app/config.py`.** `CLIP_MIN = 0.05`,
  `CLIP_MAX = 0.95`. Not in `scoring_config.json` (SPEC 4 says it is the spec
  author's addition); kept out of the config so the config stays authoritative
  for its own constants, and documented in the README.
- **D2 — `competitor:Acme+objection:no_exec_sponsor` uses weight 0.8.** The key
  has no entry in `signal_weights`; 0.8 is the `objection:no_exec_sponsor`
  weight. The combined key never fires on the demo path (turn 2 is Acme only,
  turn 3 is budget_freeze), so it is covered by a unit test instead.
- **D3 — the stage fallback feeds scoring AND the fatal check.** SPEC 4.1 says
  to fall back to all-stage stats below `min_deals_for_fatal_pattern` but does
  not say which consumer it applies to. One resolved stat set feeds both, so
  the reason line and the score can never disagree.
- **D4 — no retroactive merge across turns.** The Acme + no_exec_sponsor combine
  only applies within a single event. If a competitor is seen in an earlier
  turn its separate signal has already been applied, and history is append-only.
- **D5 — objections are applied before competitors** within one event, so
  multi-signal ordering is deterministic (SPEC 4 is a sequential log-odds walk).
- **D8 — UI confidence for a turn is the minimum** across the signals applied in
  that turn (the most cautious reading); `/api/events` returns the full list.

## Extraction and drafting (SPEC 5)

- **D6 — a known competitor name is itself the extraction cue.** SPEC 5.2 lists
  no competitor keywords, and only Acme and Vantage exist, so a name mention in
  a prospect turn is the signal. Safe here because no other demo turn names a
  competitor. Matching is word-boundary throughout, so `push` never fires inside
  another word.
- **D7 — SPEC 5.7's `"Based on: D007, D009 (Marcus Chen, Marcus Chen)"` is read
  as `(Marcus Chen)`.** The repeated name is a typo in the spec; both D007 and
  D009 are Marcus Chen's.
- **D9 — demo turn 6 marks the draft sent before ingesting the turn.** SPEC 5.2
  requires a draft to be marked sent before `positive_reply_after_followup` can
  fire, and SPEC 5.7 says turn 6 does the marking. Without this, clicking
  through the six turns without pressing Copy leaves turn 6 flat at 0.299 and
  breaks the SPEC 3 trajectory. The Copy button marks sent too.
- **D10 — a two-option budget_freeze draft is required** (SPEC 5.7) and is
  adapted from `phased_rollout` (D007) and `pilot_first` (D009). `cost_of_delay`
  (D008) is excluded because it needs a figure the seed data does not provide.
  The "30-day pilot" number is quoted verbatim from D009 and is therefore
  traceable, which satisfies SPEC 7's digit-provenance rule.

## LLM and config (SPEC 7)

- **D11 — `openai/gpt-oss-120b` is the default model**, overridable with
  `GROQ_MODEL`. It is a production model on Groq and supports `strict: true`
  constrained decoding. Startup verifies the configured model against
  `GET /openai/v1/models` and logs what is live; if it is unavailable it falls
  back to `openai/gpt-oss-20b`.
- **D14 — a Groq `429` is logged distinctly** from a generic degradation
  ("rate limited, using deterministic template"). Free-tier rate limits are a
  realistic demo-day failure and a silent template fallback would be confusing.
- **D15 — `requirements-llm.txt` was added as a third requirements file.** SPEC
  10.1 names only `requirements.txt` and `requirements-hindsight.txt`. The LLM
  must stay optional for SPEC 12 to hold, so its dependency is isolated the same
  way Hindsight's is.

## Findings from step 2 (seed validation)

- **D16 — SPEC 3's `competitor_pricing is 2 / 2 / 0` is an ALL-STAGES figure.**
  Found by running: D022 is `competitor_pricing` at **Evaluation** and D023 is at
  **Proposal**, so there is 1 deal per stage, not 2 at Evaluation. SPEC 3
  qualifies `budget_freeze` and `champion_left` with `@Evaluation` but gives
  `competitor_pricing` and the Vantage figure no stage qualifier, which is how
  the two readings are told apart. Consequence for scoring: a live
  `competitor_pricing` at Evaluation has a stage-scoped total of 1, below
  `min_deals_for_fatal_pattern` (4), so SPEC 4.1's fallback applies and the
  reason line uses 2 of 2 labelled "(all stages)". Handled by
  `resolve_objection_pattern()` and covered by two tests. The seed data was not
  modified (SPEC 0).

## Findings from step 3 (live Hindsight spike)

Measured, not assumed, via `scripts/spike_hindsight.py` against
`hindsight-client` 0.10.1 / server `api_version` 0.10.1.

- **D17 — `retain` is one-to-many; a deal count is never a unit count.** One
  retained deal came back as 2 memory units, and the full 30-deal ingest
  produced **106 units for 30 deals**, because the server LLM decomposes content
  into separate `world` facts. `all_records()` therefore dedupes by
  `metadata.deal_id` and keeps `state == 'valid'` only. `list_memories` has real
  `limit`/`offset` pagination and returns `total`, so it is the only path used
  for counts.
- **D18 — counts are NEVER taken from `recall`.** `recall` has no `limit` and no
  pagination, only `max_tokens`, and it returns several near-duplicate facts for
  the same deal (4 facts for one deal at `max_tokens=32768`). This is SPEC 6's
  "Counts must NOT be computed from a truncated list" made concrete, so
  `recall` is used only for a display-only candidate/narrative read.
- **D19 — `reflect` is narrative-only and demonstrably unreliable for numbers.**
  It reported a close date of 2026-09-28 (the day of the run) for D001, whose
  real `closed_date` is different. It is shown under "Hindsight reflection" and
  the computed counts always win (SPEC 6).
- **D20 — `metadata` is the reliable channel; the server rewrites `text`.**
  Retained text is paraphrased by the server, so every field that affects a
  count, a pattern, a draft citation or the SPEC 5.3 ordering is written into
  `metadata` as a string and read back verbatim. When several units share a deal
  id, the one with the most populated metadata wins.
- **D21 — ingest is schema-versioned and idempotent.** Every write carries the
  `ingest:v1` tag. A deal is re-ingested when it is missing **or** present under
  an older schema, because the spike had already written a reduced metadata set
  for D001 and a bare "does this id exist?" check would have kept that
  incomplete record permanently. Re-ingest is safe because reads dedupe by id.
- **D22 — SPEC 5.3 ranking is implemented in code, not delegated to the
  backend.** A semantic top-5 cannot be stable (D18), and SPEC 11 requires
  Priya and a veteran to see identical results, so the filter and ordering are
  ours: same objection and/or competitor, then same stage, then deal-size
  proximity, ordered by score, then `closed_date` desc, then id. Both backends
  run the same function, so switching `MEMORY_BACKEND` cannot change the list.
- **D23 — the fallback badge is driven by an actual call.** `build_store()` pings
  the server and only returns a non-fallback `HindsightMemory` when that call
  succeeds, so a configured-but-unreachable Hindsight shows the red
  "LOCAL FALLBACK" badge (SPEC 6).

## Findings from step 4 (scoring engine)

- **D24 — SPEC 11's "order matters" refers to the trajectory, not the endpoint.**
  Found by running: applying `budget_freeze` then `champion_left`, and the
  reverse, end at the *same* score to 12 decimal places. That is correct, not a
  bug — SPEC 4 updates in log-odds, where risk updates are additive, so
  `σ(L(p₀) − d₁ − d₂) = σ(L(p₀) − d₂ − d₁)`. What the order actually changes is
  the intermediate score, each signal's own `delta_pts`, and the order the
  reason lines are recorded in, all of which the UI shows. The endpoint only
  becomes order-dependent once the `clamp` binds, which
  `test_clamp_breaks_log_odds_additivity` pins down. Both properties are now
  asserted, so the additivity is deliberate rather than accidental.
- **D25 — the reference trajectory is reproduced to 3 decimals, not merely
  within ±0.02.** `scripts/verify_trajectory.py` reproduces every level and
  every delta exactly (0.566/−5.4, 0.415/−15.1, 0.299/−11.6, 0.418/+11.9),
  which confirms the intended reading of two judgement calls: turn 2 uses
  `competitor:Acme` 7/6/1 with the `competitor` weight 0.12, and turn 5's
  hedged `champion_left` uses the raw `objection:champion_left` weight 0.35
  (SPEC 5.2's "lower weight", not an extra discount factor).

## D26 - Hindsight must never be called from the event loop thread

hindsight_client._run_async drives its coroutine with
loop.run_until_complete(). Called from a thread that already owns a running
event loop, that raises RuntimeError: This event loop is already running.

The app lifespan is sync def, so it *is* such a thread. The failure was
silent and expensive: uild_store() caught the error and fell back to
LocalMemory, which produces the same 30/22/8 counts and the same
COUNTS VERIFIED. Every score, reason and draft was correct, and only the
memory badge revealed the live backend was never used. The first live
/api/memory/stats call showed it:

    "fallback_reason": "Hindsight unreachable: RuntimeError: This event loop is already running"

Fix: the whole startup sequence runs on a worker thread via
nyio.to_thread.run_sync. That is more than uild_store() and the retain,
because LiveSession.__post_init__ calls store.all_records() through the
same sync client, and store.close() tears down an aiohttp pool bound to the
loop that created it. All three moved.

Request handling needs no such treatment: every endpoint is a plain def, so
Starlette already dispatches them to a threadpool where no loop is running.
	ests/test_api.py::test_startup_does_not_trip_hindsight_over_a_running_event_loop
probes for any store access on the loop thread, and
scripts/verify_lifespan.py drives the real startup path against the live
backend (exit 0 = live backend, 2 = visible local fallback).

## D27 - the local fallback reason is a top-level field on MemoryStats

SPEC 6 requires the fallback to be VISIBLE, not merely detectable. The reason
string was first buried in detail["fallback_reason"], which forces the UI to
know a nested shape to render the warning. It is now MemoryStats.fallback_reason
at the top level, right beside allback, so the badge and its explanation are
always read together.

## D28 - a read-only /api/demo/script endpoint powers the transcript

SPEC 8 pins /api/demo/step's response to the /api/events score shape, which
does not carry the turn's text. The UI must stream the actual words of the
scripted call, so rather than widen the specified contract, the transcript is
exposed read-only at /api/demo/script. This also lets the frontend know the
six turns in advance (for the next/auto-play controls) without hardcoding any
text in the page, keeping index.html free of seed-derived content.

## D29 - the LLM layer (SPEC 7) mapping and its failure rules

`app/llm.py` is the entire optional Groq layer; `app/pipeline.py` calls it only
when a `GROQ_API_KEY` exists, so a keyless run never imports groq. Model
`GROQ_MODEL` (default `openai/gpt-oss-120b`) at `GROQ_BASE_URL` (default
`https://api.groq.com`; corrected in D32).

Mapping of SPEC 7 to code:
  * EXTRACT: `candidate_signals()` asks for strict JSON {objections,
    competitors}. The LLM only *suggests* candidates; every existing gate in
    `extract_turn()` still runs on them (prospect-only, recovery, at-most-one
    objection, unknown-tag rejection). Because the domain models exactly one
    objection per deal, the first taxonomically-valid tag wins and any surplus
    candidates go to `ignored` (a note the UI can surface).
  * EXPLAIN: `phrase_reason()` may reword the reason line but MUST keep the
    exact "{lost} of {total}" counts and the "N pts" figure; otherwise the
    template reason is shown.
  * DRAFT: `rephrase_draft()` may reword the follow-up but `validate_draft()`
    enforces citations within the passed ids and digit-traceability into the
    provided context (the winning-response texts of the cited deals, built by
    `LiveSession._draft_context`).

Fallback ladder shared by all three (SPEC 7: "Any API error degrades to
templates; it never raises to the client"): try once, retry once, then use the
deterministic template, with a human-readable entry in `ignored` describing
exactly what was rejected or failed. Any exception anywhere in `app/llm.py` is
caught; a caller never sees a Groq error.

## D30 - LLM degradation is explicit, not silent

Every time the LLM fails to take over a step, the pipeline emits an
`ignored` note ("LLM extraction returned nothing; keyword fallback used",
"LLM draft failed validation; template used", "LLM reason failed validation;
template used"). This mirrors the SPEC 6 rule for the memory fallback: a
degraded mode must be VISIBLE, because the template path still yields correct
scores, reasons and drafts and is otherwise indistinguishable from a live LLM
run.


## D31 - Frontend is a single-page app; client-side run log; no invented data

Rewrite of the page (per the judge-facing brief) with three hard rules:

* SPA with hash routing (`#/home`, `#/livcall`, `#/winprob`, `#/teamhistory`,
  `#/transcript`, `#/followup`, `#/autopsy`) served by one `index.html` shell.
  Each section is its own module in `static/views/*.js` and registers with the
  shared router; the sidebar surfaces Info Supply + all SPEC 9 deliverables
  (no dumping everything on one scroll).
* The run log (per-turn analyzed results) lives in the browser
  (`sessionStorage["lda.runLog"]`) so the timeline, transcript insights and
  autopsy pages work with zero backend changes. Win-probability history always
  comes from the server's own `/api/deals/live` history.
* The page NEVER invents data: labels, reasons, pattern bars and past-deal
  cards are derived in code from real API responses and the processed run
  log, with honest empty states. The "regenerate" button re-syncs from live
  state; rephrasing still must pass `validate_draft()` before it reaches the
  page.

## D32 - LLM live path was doubly broken; found and fixed by live verification

* `GROQ_BASE_URL` default was doubled: `https://api.groq.com/openai/v1`, but
  this version of the groq SDK composes its request path *from the base_url
  itself* (`.../openai/v1/chat/completions`), so every call became
  `/openai/v1/openai/v1/chat/completions` (404), every LLM step fell back to
  templates, and the D30 notes made the degradation visible. Default +
  `.env.example` fixed to `https://api.groq.com`; `GROQ_BASE_URL` is still an
  override.
* Name collision: the cached client and the lazy factory both lived in the
  module global `_client`, so `reset_client()` set the *function* to None and
  the next `_client()` call raised `TypeError` -- always caught, always
  degraded. `app/llm.py` now uses `_client_cache` for the instance and keeps
  `_client()` as the factory. The unit tests never caught either bug because
  they patch `_client` with a lambda for every case.

Both were surfaced only by the first live run of `scripts/verify_llm.py`
(exit 2 with D30-visible degradation); after the fixes it proves live
engagement end-to-end.

## D33 - Frontend replaced by the v2 dashboard; /api/deals added

The judging-facing redesign ("frontend v2") replaced the D31 SPA wholesale.
`static/` is now ONE self-contained `index.html` (inline CSS + one ~28 KB
inline script, zero CDN / zero `/static` module references, data-URI favicon;
D34 later deleted the legacy page). The page drives the same
SPEC 8 pipeline but shows hero/demo stepper, conversation transcript with
"try your own line" (`POST /api/events`), score ring + sparkline, "what
changed the score" feed, patterns, similar past deals with real
company/summary text, draft panel, retain dialog and rep/integrations UI.

* New read-only `GET /api/deals` (not in SPEC 8): returns every memory record
  as `deal.model_dump()` plus `source`, so deal cards and the industry
  datalist show real text instead of bare IDs. Never mutates state.
* The v2 snapshot's backend was NOT taken: its `app/config.py`, `app/llm.py`
  and `.env.example` predate D32 (`GROQ_BASE_URL=/openai/v1` doubling + the
  `_client` name collision) and would silently re-break live Groq; its
  `tests/test_api.py` SPEC-9 gate asserts the legacy DOM (`memBadge`, Tailwind,
  `.grid3`) that v2's own `index.html` no longer contains, so it was stale in
  its own tree. All D32 fixes, `tests/conftest.py` and `scripts/verify_llm.py`
  were kept.
* SPEC 9 gate rewritten in `tests/test_api.py` against the v2 DOM: widget ids,
  self-containment (no network fetch besides the SVG-namespace favicon),
  endpoint wiring (incl. `/api/deals`), `node --check` on the inline script,
  and a `deals` endpoint test. Known cosmetic mismatch kept "as is": the page
  checks `draft.generated_by === "llm"`, the pipeline emits `"groq"`, so the
  "AI-polished wording" label is never shown.

## D34 - Final-sprint phase 1: packaging & hygiene

First phase of the final-fix sprint. `.gitignore` gains `dist/`, `build/`,
`*.log`, `.coverage`, `htmlcov/`. New `scripts/make_release.py` (stdlib-only)
builds `dist/LostAgent_release.zip` excluding `env`/`.env`, `.venv`, caches,
`dist` and the now-deleted legacy page, INCLUDES `.env.example`, then scans the
archive bytes with the same key patterns as `tests/test_no_secrets.py` and exits
non-zero on any hit (the artifact that ships can never carry a live key).
`static/index_legacy.html` was deleted (dead, unlinked, and the sprint's
packaging rule says drop it) — D33's "kept as reference" is superseded. Prose
that referenced the legacy page was updated, and the dangling
`scripts/verify_api.sh` reference in `tests/test_api.py` now points at
`scripts/smoke_e2e.py` (built in phase 8). `tests/test_no_secrets.py` gains an
archive-scan test so the release guard is part of the suite; `dist`/`build`/
`htmlcov` join `SKIP_DIRS`.

## D35 - Final-sprint phase 2: close the loop

The live demo deal now has an outcome. `POST /api/deals/close` retains it as a
`live_added` deal (next free live id `C###`), refresh, then returns a "memory
autopsy": pattern counts before/after, the next-run reason line, which rep wins
backed each option, timeline of what actually drove the score, and a one-line
lesson. A lost close grows budget_freeze@Evaluation 10/6/4 -> 11/7/4 so the
next run says "7 of 11" and scores harder; a won close flips it to 11/6/5.
Closing **won** at loss_ratio 0.545 (< fatal 0.6) makes the fatal-loss draft
stop firing — that is the pattern no longer being fatal, NOT a rules hack;
documented here so the demo's "turn 4: why no draft?" reads as the intended
story. Won closing reuses the winning text and rep verbatim from the earliest
seed win on that strategy, and it is the only source of a draft-first
strategy. `DEMO_CLEAN_START` (default on) retires leftover `live_added` deals
at startup so a shared/e.g. cloud bank still replays the exact seeding the
first time. Closed-again on the same run is 409; won without a known winning
strategy, or a close before any signal applied, is 422.

## D36 - Draft-from-memory ranking

Special case (replaces generic ranking in `build_draft`). Budget-freeze
strategies are ordered by (wins in shared memory on this objection DESC,
`BUDGET_FREEZE_STRATEGIES` order ASC, newest `closed_date` DESC), with one
citation per option. The seed alone still yields D007 `phased_rollout` then
D009 `pilot_first`, byte-identical "Based on: D007, D009 (Marcus Chen, Marcus
Chen)". When a teammate logs a *new* won `pilot_first` deal (Negotiation in the
staging story), pilot outranks phased, the draft flips to pilot-first and cites
the new deal, while Evaluation's fatal 10/6/4 is untouched (stage-scoped
pattern). Forbidden/requires-a-number strategies (`cost_of_delay`) can never be
drafted, only cited.

## D37 - Forgetting live adds against shared memory

`forget_live_added()` is implemented per backend. Locally it deletes
`SOURCE_LIVE` records. On Hindsight it retires each matching memory unit with
`update_memory(..., state="invalidated", reason="reset_live")` (world +
experience units), because that backend curates only its own units and cannot
delete others'; observation/unknown-fact-type units are dropped in-process via
a `_retired_ids` set that `all_records()` filters, keeping counts consistent
without touching the shared bank.

## D38 - Hindsight memory insight endpoint

`GET /api/memory/insight?objection=&q=` gives the frontend a read-only "what
does the team's shared memory know?" card. Deterministic arm (pattern counts
incl. fatal flag, won backings with reps/strategies/recency, the three most
recent lost notes, per-strategy win tallies, a one-line lesson counted from the
records) works identically with no keys, so the judging build never shows an
empty slab. Semantic arm is invoked ONLY on a live Hindsight backend and is
display-only: recall ids + the reflection narrative, memoized per
(objection, q) so a live bank is touched once per question, wrapped in a 6s
`ThreadPoolExecutor` timeout, and any exception degrades to `available:false`
— never a 500, never a number. The card is rendered inside the existing memory
popover so the "how is this powered" and "what does it remember" stories sit
together. Frontend: `loadInsight()` refetches on every render; the server keeps
the deterministic counts live after a close while the cached semantic list may
lag — acceptable, since the counts are the source of truth and the narrative is
explicitly display-only.

## D39 - Frontend close-the-loop

The page now closes a run and shows what that does to memory. A "Close this
deal" button appears once the call has started (and is hidden once closed);
its dialog picks Lost/Won, and for Won pre-fills the winning move list from the
insight endpoint's real won-backings (so the strategy is always one memory has
actually won with), plus an optional note. Submit calls `/api/deals/close`,
then renders the autopsy as a card: pattern before -> after for the objection
(6 of 10 -> 7 of 11 phrasing), the won move + verbatim winning text, the
lesson, what-won-before by rep, and the score drivers; a replay button restarts
so the judge can watch "7 of 11" fire. The log dialog gained the missing
outcome selector (won logging) so a teammate can join a WIN to shared memory;
`/api/retain` now derives that deal's winning text verbatim from memory and
rejects invented/unknown strategies with 422. `/api/deals/live` exposes
`closed`. Frontend deaths removed: `expectedDeals`, the unused `ownLine`
parameter and the dead `data-chips` attribute; the "AI-polished wording" label
now also matches `generated_by: "groq"`.

## D40 - Tabs skipped, single-panel layout kept

The optional per-objection tabs (cut-list #4) were skipped on purpose: the
all-in-one scroll (call, draft/autopsy under it, memory/similar-deals rail on
the right) is simpler to demo, keeps every narrative one page, and avoids
reworking the v2 DOM gate for zero judgement value. Documented instead of done.

## D41 - Negation-aware extraction + corpus gate

Keyword matching now checks the clause a keyword sits in: a negator within 5
words before the phrase, without crossing a clause boundary (punctuation, or
"but" / "however" / "although"), suppresses that match. `no` alone is NEVER a
negator (the SPEC phrase "no budget" and "we have no exec sponsor" must keep
firing). Competitors get the same lens ("it's not Acme we're worried about" no
longer flags Acme), and "no longer" / "not at all" / won't-contractions count
as whole-token negators, not substrings. This is correctness the demo leans on
(the trajectory's "6 of 10 lost" reasons must stay honest) and it is locked by
`tests/data/extraction_cases.json` (43 hand-labelled cases) driven by
`scripts/eval_extraction.py`, which is both runnable and imported by pytest.
The LLM `asserted_as_current` flag (cut-list #5) was skipped: it is display-only,
would force a widening of the frozen `EventResult` contract and the round-trip
into the DOM for a marginal chip, and the negation lens already defeats the
"buyer says it's resolved" false positives it would have been for; documented
instead of done.

## D42 - Per-browser sessions, validation, one lock per session (Phase 6)

Two browser windows over the same team memory must not wreck each other's run,
so the FastAPI app now resolves a LiveSession per X-Session-Id header (default
"default", kept for callers without a request) over ONE shared store: memory is
the team's, the scoreboard is the tab's. Sessions are get-or-create with an LRU
cap of 50; eviction drops only the session object, never the app's store. The
frontend sends a sessionStorage id on every request (survives F5, fresh per new
tab). Every mutating LiveSession method runs under one session-level
threading.RLock (reentrant, because advance->process_event and
close_deal->refresh_records nest), so a replay racing a close can't interleave
inside one engine. Inputs are now hard-checked in code: EventInput text/speaker
lengths, RetainRequest id/deal_size/closed_date shapes, and endpoint-level
vocabulary (objection exactly one and in the taxonomy, stage in the stage list)
with a 409 for duplicate deal ids -- every guard is a clean 4xx, never a 500.

## D43 - Transcript import + honest Integrations panel (Phase 7)

POST /api/transcript analyses a pasted call with the SAME pipeline as the demo:
raw text ("Speaker: line", optional [hh:mm] prefix, unlabeled lines treated as
the prospect) or the {"turns":[...]} JSON shape a Gong/Zoom export maps to.
Capped at 100 turns, each 1-2000 chars, body capped at 200KB; the response
echoes each parsed input with its EventResult so the UI animates imported turns
identically to the demo. The timestamp prefix is consumed BEFORE the speaker
split, because "[10:02] A: b" partitions at the wrong colon otherwise. The
Integrations popover states honestly what exists (transcript import + REST
/api/events) and what is planned (Salesforce/HubSpot/Gong/Zoom: Planned; road:
CRM sync of closed deals, live call streaming, outcome-calibrated scoring) --
it never shows "Connected". scripts/sample_webhook.py proves the wiring path
with stdlib-only urllib.

### D44: one Hindsight client per OS thread (aiohttp loop affinity)

Every endpoint 500'd (e.g. /api/reps) with `RuntimeError: Timeout context
manager should be used inside a task` whenever the real Hindsight backend was
active: the generated client shares ONE aiohttp connector, which binds to the
loop of whatever thread created it, but uvicorn dispatches sync endpoints to
threadpool threads that each own a different loop. Fix: `_ThreadLocalHindsight`
(every thread lazily builds its own Hindsight client, so all of that thread's
calls stay on the loop the connector was created on) plus `_drive`, which runs
`forget_live_added`'s curated invalidation on the current thread's cached loop
instead of `asyncio.run`'s brand-new loop. Verified: 6 concurrent worker
threads each reconstruct 30 deals; live server /api/reps, /api/deals/live,
/api/memory/stats all 200; suite 188 passed; verify_lifespan PASS live. Local
fallback path (used by smoke_e2e and keyless demos) is untouched.
## D45: Chat with us — data-grounded, template-only (Phase 9)

POST /api/chat answers the "chat with us" button with a template-only reply
that is counted from the session's own records, never the LLM, so it stays
deterministic and keyless with no hallucination risk. Intent routing is
word-boundary substring matching (`hi` must not match `history`, `about` must
not match "tell me about X"), requests are 1-500 chars (over-long -> 422),
and the reply is grounded in the real pattern counts: budget freeze answers
"10 budget freeze deals in shared memory: 4 won, 6 lost." and after a lost
close the same question returns the recounted "11 ... 4 won, 7 lost."
Unknown phrases echo their first 48 chars back and name what IS answerable
(it never fabricates a number or leaks server detail). The floating button
+ panel (chatBtn/chatBox/chatInput/chatSend/chatClose) sits above the toast
layer, renders replies and errors with textContent (no HTML injection), and
is listed in the SPEC 9 widget gate alongside the new themeToggle.

### D45 also: light/dark theme, explicit and remembering

The page has always been light-with-auto-dark via `prefers-color-scheme`.
D45 adds a manual toggle (themeBtn) stored in localStorage(`la.theme`) that
sets `data-theme="light|dark"` on <html>; the auto media query now only
applies to `:root:not([data-theme])`, so a user's explicit choice wins while
no stored preference still follows the OS. Button label mirrors the current
effective theme and stays in sync via a change listener. +11 tests
(tests/test_chat.py). Full suite: 199 passed; make_release clean.
