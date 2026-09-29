# PROGRESS.md

Checklist of SPEC 10. **A step is ticked only after its gate passes** (SPEC 0).
Evidence column records what was actually run, not what was intended.

Legend: `[ ]` not started · `[~]` in progress · `[x]` gate passed

---

## Step 1 — Scaffold
- [x] venv on py-3.14 — **verified**: `.venv/` Python 3.14.3, pip 25.3
- [x] `requirements.txt` — **verified installed**: fastapi 0.141.1, uvicorn 0.54.0, pydantic 2.13.5, httpx 0.28.1, python-dotenv 1.2.3, pytest 9.1.1 (pip exit 0)
- [x] `requirements-hindsight.txt` — **verified installed**: hindsight-client 0.10.1
- [x] `requirements-llm.txt` — **verified installed**: groq 1.7.0 (see D15)
- [x] `.env.example` — **verified**: present, all credential values empty
- [x] `.gitignore` (includes `.env`) — **verified**: covers both `env` and `.env`
- [x] pydantic models — **verified**: `app/models.py` (Deal, Rep, ScoringConfig, Turn, Pattern, EventResult, …)
- [x] `DECISIONS.md` — **verified**: D1–D15
- [x] `PROGRESS.md` — **verified**: this file
- [x] `app/config.py`, `app/main.py` — **verified**: `Hindsight` import path clean

**GATE (SPEC 10.1): app imports cleanly — PASSED.**
Evidence: `python -c "import app.main"` → `import app.main OK`, `app object: FastAPI`, exit 0.
`settings.describe()` → both services detected, `env_files_loaded: ["env"]`, **no secret values printed**.
SPEC 11 no-secrets grep: `pytest tests/test_no_secrets.py -v` → **3 passed**.

---

## Step 2 — Seed loader
- [x] validate all five seed files
- [x] reject objection tags outside `objection_taxonomy` — **verified by test**: an injected `totally_made_up_tag` is reported
- [x] assert totals 30 / 22 lost / 8 won
- [x] assert Priya's `deals_worked` is empty — **verified**: R3 `deals_worked == []`
- [x] assert reps' `deals_worked` equal the `rep` field in deals
- [x] `app/seed_loader.py` also holds the reference pattern-count implementation
- [x] `resolve_objection_pattern()` implements the SPEC 4.1 stage fallback (D3)
- [x] seed files proven unmodified by load (bytes + mtime compared)

**GATE (SPEC 10.2): tests pass — PASSED.**
Evidence: `pytest tests/ -v` → **27 passed** (24 seed + 3 no-secrets).
`test_clean_seed_has_no_problems` → `validate_seed()` returns `[]`.

**Data finding logged as D16:** SPEC 3's `competitor_pricing is 2 / 2 / 0` is an
ALL-STAGES figure — D022 is at Evaluation, D023 at Proposal, so it is 1 per
stage. SPEC 3 qualifies the stage-scoped figures with `@Evaluation` and does not
qualify this one. Seed data left unmodified; the fallback path is tested.

---

## Step 3 — Hindsight spike + LocalMemory
- [x] install verified: hindsight-client 0.10.1 present on 3.14
- [x] inspect installed package for real method names (SPEC 0: never guess) —
      read the real signatures of `retain`, `retain_batch`, `recall`, `reflect`,
      `list_memories`, `MemoryUnitListItem`
- [x] `MemoryStore` interface with `retain()` / `recall()` / `reflect()`
- [x] `LocalMemory` implementation
- [x] retain all 30 deals into bank `sales-team-shared`
- [x] **prove all matching deals are retrievable, not a truncated top-k**
- [x] startup integrity check vs `expected_patterns.json`
- [x] visible fallback badge when Hindsight is unconfigured or unreachable
- [x] live `recall` and live `reflect` both exercised against the real API
- [x] ingest is schema-versioned and idempotent

**GATE (SPEC 10.3): `COUNTS VERIFIED` on Hindsight — PASSED (live).**
Evidence: `pytest tests/ -v` → **53 passed**, including 6 live-network tests.
`python scripts/verify_memory.py` → exit 0:

```
backend        : hindsight
bank_id        : sales-team-shared
fallback       : False
ingested       : 30
reconstructed  : 30 deals   lost: 22   won: 8
integrity      : COUNTS VERIFIED
   objection:budget_freeze@Evaluation                    10/  6/  4   (expected 10/6/4)
   objection:champion_left@Evaluation                     6/  5/  1   (expected 6/5/1)
   competitor:Acme                                        7/  6/  1   (expected 7/6/1)
   competitor:Acme+objection:no_exec_sponsor              5/  4/  1   (expected 5/4/1)
   recovery:positive_reply_after_followup@budget_freeze   5/  1/  4   (expected 5/1/4)
raw memory units: 106 for 30 deals
```

**Truncation finding (logged as D17–D18):** `recall` has no `limit` and no
pagination, only `max_tokens`, and returned 4 near-duplicate facts for a single
deal; the 30-deal ingest produced **106 memory units for 30 deals** because the
server splits each retained deal. So counts come from paginated
`list_memories` deduplicated by `metadata.deal_id`, never from `recall`.
Also logged: D19 `reflect` invented a close date (display-only), D20 metadata
survives verbatim while text is paraphrased, D21 schema-versioned idempotent
ingest, D22 ranking implemented in code so both backends agree, D23 the Hindsight
badge requires a successful live call.

---

## Step 4 — Scoring engine
- [x] log-odds update, constants read from `scoring_config.json` (none hardcoded)
- [x] `clip` named constant (D1) — CLIP_MIN/CLIP_MAX in `app.config`
- [x] confidence = min(1, total / confidence_full_at_n)
- [x] risk never raises, recovery never lowers (`max(0, delta)` on both)
- [x] clamp to `clamp.min`..`clamp.max`
- [x] dedupe per live deal; replayed event id is a no-op
- [x] Vantage 5/0 and competitor_pricing 2/0 stay finite
- [x] stage fallback feeds score and fatality from one resolved pattern (D3)
- [x] combined-pattern weight 0.8 (D2)
- [x] no retroactive merge of signals (D5)
- [x] UI confidence = min across applied signals (D8)

**GATE (SPEC 10.4): reference trajectory reproduced — PASSED.**
Evidence: `pytest tests/test_scoring.py -v` → **33 passed**.
`python scripts/verify_trajectory.py` → exit 0:

```
turn    score     ref  delta pts     ref  note
0       0.620   0.620        0.0     0.0  prior
1       0.620   0.620        0.0     0.0  neutral, no signal
2       0.566   0.566       -5.4    -5.4  competitor Acme
3       0.415   0.415      -15.1   -15.1  budget freeze, fatal -> draft
4       0.415   0.415        0.0     0.0  rep turn, no signal
5       0.299   0.299      -11.6   -11.6  champion leaving, fatal -> heads-up
6       0.418   0.418       11.9    11.9  positive reply after follow-up
OK: reference trajectory reproduced within +/-0.02 on every level
```

Every level and delta matches to 3 decimals, not just inside ±0.02 (logged as
D25). Turns 1 and 4 produce zero signals and zero delta. Fatal checks confirmed
for budget_freeze (6/10 = 0.6, `>=`) and champion_left (5/6); competitor-only
patterns are fatal by ratio but excluded from the draft path by the pipeline.

D24: order matters for the trajectory and history, but the endpoint is
order-invariant because SPEC 4 updates in log-odds. Both properties asserted.

---

## Step 5 — Pipeline + API + demo runner
- [x] RETAIN → EXTRACT → RECALL → REFLECT → SCORE → EXPLAIN → DRAFT, one path —
  **verified**: `app/pipeline.py` `LiveSession.advance()` is the only scoring path,
  and `/api/events`, `/api/demo/step` and `/api/retain` all go through it
- [x] keyword extractor; prospect turns only; turn 4 yields zero signals —
  **verified by test**: turn 1 and turn 4 both yield `delta == 0`
- [x] all eight SPEC 8 endpoints — **verified by test**: all present, plus
  `/api/hindsight/ping` for the SPEC 6 badge
- [x] draft marked sent at turn 6 (D4), recovery credited on turn 6

**GATE (SPEC 10.5): full 6-turn run via pytest and curl with no API keys.**
- **verified by pytest**: `pytest tests/test_pipeline.py tests/test_api.py` → 48 passed
- **verified by curl** against real `uvicorn`, no LLM key involved, scores
  `0.620 / 0.566 / 0.415 / 0.415 / 0.299 / 0.418`, deltas
  `0 / -5.4 / -15.1 / 0 / -11.6 / +11.9`
- **live Hindsight during that run**: `backend=hindsight`, `fallback=false`,
  `integrity_status=COUNTS VERIFIED`, 30/22/8
- **bug found and fixed by this gate (D26)**: startup ran inside the event loop
  and silently fell back to local. `scripts/verify_lifespan.py` now proves the
  live backend is in use. Re-run it after touching startup code.

---

## Step 6 — Frontend
- [x] single `index.html`, vanilla JS, Tailwind CDN + critical CSS — **verified**:
      `static/index.html` is served from `/`, inline script passes `node --check`,
      critical-CSS block is CDN-free (readable offline, SPEC 9)
- [x] header badges, transcript, gauge + sparkline, pattern cards — **verified by
      test**: `tests/test_api.py` asserts every SPEC 9 section renders
- [x] similar past deals, draft panel with Copy, secondary heads-up card —
      **verified**: recalled deals list worked-by-you, draft panel slide-in,
      "Based on: D007, D009 (Marcus Chen, Marcus Chen)" format
- [x] add-lost-deal form, stubbed integrations — **verified**: form POSTs
      `/api/retain`, integrations panel lists Salesforce/HubSpot as planned

**GATE (SPEC 10.6): page loads; 6 turns produce a visible drop, reason, draft.**
- **verified**: `GET /` → 200 (25 KB), demo script serves 6 turns, and a 3-turn
  HTTP run reproduced 0.62 → 0.566 → 0.415 with the draft on turn 3
- **verified by pytest**: full suite 138 passed
- decision D28: `/api/demo/script` read-only transcript endpoint so the UI can
  stream turn text without widening the SPEC 8 `/api/demo/step` contract

**GATE re-verified after D31 SPA redesign.**
- the page is now a hash-routed SPA (7 sections, one per view file); **verified
  by test**: shell + navbar + per-view `App.registerView()` + every API path the
  frontend drives in `tests/test_api.py`; **verified by smoke**: uvicorn serves
  `/`, `/static/app.css`, `/static/app.js`, all 7 `/static/views/*.js` (200,
  correct content types) and all SPEC 8 endpoints; demo script still 6 turns,
  live score 0.62
- `node --check` passes on `app.js` + all 7 view modules; DOM ids referenced by
  `app.js` exist in the shell; full suite now 149 passed

**GATE re-verified after D33 v2 dashboard adoption (frontend replaced).**
- `static/` is now a single self-contained `index.html` (inline CSS + one inline
  script, no CDN / no `/static` modules),
  shipped "as is" from the v2 snapshot; all D32 backend fixes + `conftest.py` +
  `verify_llm.py` retained
- **verified by test**: `tests/test_api.py` SPEC 9 gates rewritten for the v2
  DOM — every widget id present, page is self-contained/offline-safe, all API
  paths wired (incl. `/api/deals`), inline script passes `node --check`, `/api/deals`
  returns all 30 records with a clean 8 won / 22 lost slice; full suite 150 passed
- **verified by smoke**: fresh uvicorn on :8000 serves the new page (`/` lacks
  `/static/app.js`), `/api/deals` → 30 rows, demo script 6 turns, live score 0.62
- decision D33 documents what was adopted vs. rejected from the v2 snapshot

---

## Step 7 — LLM (Groq)
- [x] `openai/gpt-oss-120b`, strict JSON schema for extraction — **verified**:
      `app/llm.py` requests `json_object` and rejects invalid/unknown tags;
      keys come from `GROQ_API_KEY`/`GROQ_MODEL`/`GROQ_BASE_URL` (no key → no call)
- [x] reason line must contain the exact `lost of total` numbers — **verified**:
      `phrase_reason()` keeps "{lost} of {total}" and "N pts" or the template
      is used; asserted by `tests/test_llm.py`
- [x] draft may cite only passed deal IDs; every digit must be traceable —
      **verified**: `rephrase_draft()` runs `validate_draft()` against the cited
      winning responses; an invented figure like "87%" is thrown away
- [x] retry once, then template; any error degrades, never raises — **verified**:
      API-error test runs all six turns to the reference trajectory untouched

**GATE (SPEC 10.7): demo fully works with the key removed.**
- **verified by test**: `test_keyless_demo_never_touches_groq` asserts the Groq
  client is never constructed on a keyless run and the 6-turn reference
  trajectory still holds
- **verified by pytest**: `tests/test_llm.py` 10 passed; full suite 148 passed
- **verified**: `scripts/verify_lifespan.py` still exit 0 on the live backend
- decisions D29 (LLM mapping + fallback ladder) and D30 (degradation is
  explicit via `ignored` notes)

**GATE re-verified live (real Groq, D32 fixes).**
- `scripts/verify_llm.py` runs the six turns through the real pipeline on a
  local memory store with the Groq key from `env`: **exit 0**. The LLM engaged
  on 5/5 extraction turns (`signals.source = "llm"`), the draft was generated
  by Groq (`generated_by = "groq"`, citations D007/D009 intact, marked sent),
  one reason rephrase was rejected by the count/pts validator (D30 note, as
  designed), and the score trajectory was byte-identical to the reference —
  `0.620 → 0.566 → 0.415 → 0.415 → 0.299 → 0.418`.
- the live run also caught two real bugs the scripted tests could not: the
  doubled `GROQ_BASE_URL` and the `_client` name collision, both fixed (D32);
  full suite still **149 passed** (now with an autouse keyless-LLM pin in
  `tests/conftest.py` so the gate tests never make network calls)

---

## Step 8 — Polish
- [x] score-drop flash, draft slide-in — **verified**: `static/index.html`
      defines `@keyframes flashgood/flashbad/rise/pulse/slide` and toggles
      `flashgood`/`flashbad` on the score ring for the drop, `slide` on the
      draft/recap slots (D33 page; no Tailwind critical-CSS block anymore)
- [x] README maps Retain / Recall / Reflect / confidence-scored belief to code —
      **verified**: `README.md` has a pipeline→`app/*.py` table plus each engine
      formula
- [x] README states seed data is synthetic; documents the `clip` addition —
      **verified**: README "Data honesty" + "The `clip` constant" sections
- [x] final report: verified-by-running (with output) vs not verified —
      **verified**: README "Verification report" splits the two, including the
      honest gaps (no scripted browser, key rotation pending) — the live Groq
      call is now verified via `verify_llm.py` (exit 0), no longer a gap.

**Definition of done (SPEC 12) is met by the evidence above; the only open item
is rotating the exposed Hindsight key before external use (README records the
exact re-check commands).**

**GATE (SPEC 10.8 / SPEC 12: definition of done.**

## FINAL SPRINT (D34+)
- Phase 1 packaging & hygiene (D34): .gitignore + dist//build//*.log/.coverage/htmlcov/; scripts/make_release.py builds dist/LostAgent_release.zip excluding env/.env/.venv/caches/legacy and scans the archive bytes for secret patterns (exit 0, 40 files); static/index_legacy.html deleted; stale prose updated; test_no_secrets gains release-archive scan. Suite: 151 passed.
- Phase 2 close-the-loop (D35-D37): POST /api/deals/close retains the live deal (C###) and returns a memory autopsy (pattern before/after, next-run reason line, winning-response backing, score timeline, lesson); lost close 10/6/4 -> 11/7/4 "7 of 11" and replay scores harder; won close -> 11/6/5 stops the fatal draft (documented, not hacked) with verbatim seed winning text; second close 409, won-invalid/unknown 422; POST /api/memory/reset_live + DEMO_CLEAN_START retire live adds (Hindsight forget via update_memory state=invalidated + in-process _retired_ids); draft-from-memory ranking (D36) keeps seed "Based on: D007, D009" verbatim while a new won pilot_first flips pilot-first and cites it (stage-scoped 10/6/4 intact). New tests/test_close_loop.py. Suite: 162 passed.
- Phase 3 memory insight (D38): GET /api/memory/insight returns the deterministic pattern/backings/by-strategy/lost-notes/lesson arms from records (keyless, never empty) plus a display-only semantic arm (Hindsight recall ids + reflection narrative, memoized per (objection,q), 6s timeout, exceptions -> available:false, never 500); frontend renders it inside the memory popover (loadInsight()/memInsight). 4 new tests. Suite: 166 passed.
- Phase 4 frontend close-the-loop (D39/D40): Close-this-deal button + dialog (Lost/Won + winning move from real wins + note) -> /api/deals/close -> autopsy card (pattern before/after 6of10->7of11, won move verbatim, lesson, what-won-before, score drivers, replay); log dialog gains outcome selector so teammates log WINS (/api/retain derives winning text verbatim from memory, unknown strategy or won-without-strategy 422); /api/deals/live exposes closed; deaths removed (expectedDeals, ownLine, data-chips; llm/groq label). Tabs (cut #4) skipped+documented. +2 tests. Suite: 168 passed.
- Phase 5 extraction correctness (D41): negation-aware keyword matching (negator <=5 words back, clause-boundary aware; "no" alone never negates so "no budget"/"no exec sponsor" still fire; competitor names same lens); tests/data/extraction_cases.json (43 labelled cases) + scripts/eval_extraction.py (runnable, and imported by pytest); demo turn signals re-locked. LLM asserted_as_current (cut #5) skipped+documented. +2 test files. Suite: 170 passed.
- Phase 6 reliability (D42): per-browser sessions via X-Session-Id (one shared store, LRU cap 50, frontend sessionStorage id), every mutating LiveSession method serialized under one session-level RLock, and hard input validation (EventInput text/speaker lengths, retain id/date/size shapes, objection/stage vocabulary, duplicate-id 409). +10 tests. Suite: 180 passed.
- Phase 7 integration (D43): POST /api/transcript (raw "Speaker: line" or {"turns":[...]} JSON, 100-turn & 200KB caps, per-turn EventResult echoes; timestamp-prefix parsing fixed); frontend "Import a real call" card (Load sample + Analyze that animates through the same handler) and an Integrations popover that is honest (Planned for Salesforce/HubSpot/Gong/Zoom, Available today: transcript import + REST API, 3-line roadmap, never "Connected"); scripts/sample_webhook.py (stdlib-only urllib proof). +8 tests. Suite: 188 passed.

- Phase 8 docs & final gate: scripts/smoke_e2e.py proves keyless end-to-end over a spawned uvicorn on :8001 (6-turn trajectory +-0.02, turn-3 draft, turn-6 recovery, lost close -> 7-of-11, replay warns harder, JSON transcript catches security_review_stall, exit 0). README rewritten to real numbers (188 passed), bash+PowerShell quickstart, actionable runnable script list, 'What changed in the final sprint' and 'Known limitations' sections; stale D31/D33 line removed. DEMO.md created (3-minute script). verify_lifespan.py / verify_memory.py updated for the shared-store architecture (were broken by D42) and re-verified live: 30 deals, 22 lost, 8 won, COUNTS VERIFIED, no event-loop error, exit 0. verify_llm.py re-run live: LLM ENGAGED 3/3 -- exact reference trajectory rechecked (verify_trajectory exit 0). eval_extraction.py 43/43. A stray live_added deal leaked into the shared bank from an early keyless-smoke spawn; retired via the existing forget_live_added mechanism (D35), bank back to exactly 30.. Full suite: 188 passed. GATE COMPLETE.
- Phase 8 hotfix (D44): with the real Hindsight backend active, every endpoint 500'd (`RuntimeError: Timeout context manager should be used inside a task`) because the shared aiohttp connector was created on one thread's event loop but uvicorn's threadpool threads each own their own loop. Fixed with `_ThreadLocalHindsight` (one client per OS thread keeps every call on the loop its connector was created on) and `_drive` (runs `forget_live_added`'s curated invalidation on the current thread's cached loop, not `asyncio.run`'s fresh loop). Verified via 6 concurrent worker threads, live server /api/reps + /api/deals/live + /api/memory/stats all 200, full suite still 188 passed, verify_lifespan PASS live, smoke_e2e OK. Root `/` now serves with `Cache-Control: no-cache` so refreshed browsers always get the current page.

- Phase 9 chat + theme (D45): the dashboard gains a "Chat with us" button and panel wired to POST /api/chat (template-only, keyless, word-boundary intent routing, 1-500 char validation, replies counted from the session's own records - budget freeze answers "10 ... 4 won, 6 lost." and recount to "11 ... 4 won, 7 lost." right after a lost close; unknown phrases echo safely and never fabricate or leak). Granular theme toggle: explicit light/dark via data-theme on <html> persisted in localStorage (auto OS dark still applies when no choice is stored), plus chatBtn/chatBox + themeBtn added to the SPEC 9 widget/copy/API-path gate. +11 tests. Full suite: 199 passed; smoke_e2e + verify_trajectory + make_release all re-run clean.
