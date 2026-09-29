# Lost-Deal Autopsy Agent

A hackathon prototype built by a three-member team: a sales agent with ONE shared memory of a team's past deals. During a live deal it (1) keeps a live win-probability score that
updates on every call/email/CRM event, (2) explains each change in one line
using real counts from team history, and (3) drafts a follow-up using language
that worked in past deals that survived the same objection.


## Team Contributions

| Member | Contribution |
|---|---|
| **C. Aniketh Sai** | Backend architecture, Hindsight shared-memory integration, data ingestion/recall, and API pipeline |
| **K.V.K Sreekar** | Frontend dashboard, live deal interaction flow, score/pattern visualizations, and session-based UI behavior |
| **D. Srikar Reddy** | LLM/Groq integration, signal extraction and validation, testing/evaluation scripts, and release verification |

The three members jointly worked on the overall architecture, integration, debugging, and final validation of the prototype.


The authority is `SPEC.md` plus the five files in `seed/`. Nothing in this
README overrides them; where the README and SPEC disagree, the SPEC wins.

## Quickstart

```powershell
# PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-hindsight.txt -r requirements-llm.txt
Copy-Item .env.example env   # then fill in your own keys
python -m uvicorn app.main:app --reload
```

```bash
# bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-hindsight.txt -r requirements-llm.txt
cp .env.example env   # then fill in your own keys
python -m uvicorn app.main:app --reload
```

The page loads at http://127.0.0.1:8000. With no API keys at all, the demo
fully works: the memory layer falls back to an in-process store (shown with a
red **LOCAL FALLBACK** badge) and the LLM layer never runs (`MEMORY_BACKEND=local`
forces this deterministically; `scripts/smoke_e2e.py` proves every gate keyless).

The frontend is a single self-contained `static/index.html` (D33): one
vanilla-JS dashboard with inline CSS, no CDN and no `/static` modules
(offline-safe). It drives the live call, the six-turn demo ("try your own line"
posts to `/api/events`), the score ring/sparkline, "what changed the score",
patterns, similar past deals, the drafted follow-up, the log-a-deal dialog, the
close-the-loop autopsy card (D35), an insights card over team memory (D38), a
transcript-import card and an Integrations panel (D43), a floating data-grounded
"Chat with us" panel + explicit light/dark theme toggle (D45), a per-browser
session
id on every request (D42), and honors rep selection. Deal cards enrich recalls
with actual company/industry/summary via `GET /api/deals` (read-only, D33). The
page never invents data: every label, reason, bar and card is derived in code
from real API responses, with honest empty states.

```powershell
# all tests, including the live Hindsight checks (skipped if no key)
pytest tests/ -q
# prove the startup path really uses the live Hindsight backend
python scripts/verify_lifespan.py     # exit 0 = live, 2 = visible fallback
# reproduce the exact score trajectory
python scripts/verify_trajectory.py
# prove the live Groq path engages (exit 0 = live, 2 = visible template fallback, 3 = no key)
python scripts/verify_llm.py
# full gate over a real (keyless) uvicorn: 6 turns, close lost, replay warns 7-of-11, transcript
python scripts/smoke_e2e.py           # exit 0 = all gates
python scripts/sample_webhook.py      # POST a JSON transcript to /api/transcript
python scripts/eval_extraction.py     # 43 labelled extraction cases
python scripts/make_release.py        # package dist/ + byte-level key scan
```

## How the pipeline maps to the code (SPEC 12)

One pipeline powers every endpoint (SPEC 5): **RETAIN → EXTRACT → RECALL →
REFLECT → SCORE → EXPLAIN → DRAFT**. `LiveSession.process_event` in
`app/pipeline.py` is the single implementation; `POST /api/events`,
`POST /api/demo/step` and the pytest demo run all call it.

| Concept | Code |
|---|---|
| **Retain** (ingest the 30 deals; `POST /api/retain` for new ones) | `MemoryStore.retain`/`retain_many`, `ingest_seed` in `app/memory.py`; startup ingest in `app/main.py` lifespan |
| **Recall** (top-5 similar deals, deterministic order) | `MemoryStore.recall_similar` (local + Hindsight implementations in `app/memory.py`) |
| **Reflect** (the `N of M` counts behind every signal) | computed in code from memory records — `verify_counts`/`pattern_index`/`resolve_objection_pattern` in `app/seed_loader.py`, surfaced as REFLECT via `ScoringEngine.pattern_for` in `app/scoring.py`. Never an LLM. The optional Hindsight `reflect` narrative is display-only. |
| **Confidence-scored belief** | log-odds engine in `app/scoring.py`: `delta = logit_scale × L(clip(loss_ratio)) × weight × confidence`, `p' = sigmoid(L(p) ∓ max(0, delta))`, clamped to `clamp.min..clamp.max`. `confidence = min(1, total / confidence_full_at_n)`. |
| **Extraction** (keyword or optional Groq) | `extract_turn` in `app/extract.py`; `candidate_signals` in `app/llm.py` (LLM suggests, code validates) |
| **Reason line** | `_reason_line` in `app/pipeline.py`; optional Groq rephrase in `app/llm.py` that must keep the exact counts |
| **Draft** | `build_draft` in `app/draft.py`; optional Groq rephrase validated for cite-ids and digit traceability |
| **Frontend** | single self-contained `static/index.html` (inline CSS + one inline script, no CDN/`/static` deps, D33) driving the demo, live-call transcript, score ring + sparkline, "what changed the score", patterns, similar deals, draft panel and retain dialog over the REAL API |

## Data honesty

The 30 deals in `seed/*.json` are **synthetic**, engineered to produce clear
patterns for the demo (SPEC 12). The ground truth is 30 deals, 22 lost, 8 won.
Patterns are checked against `expected_patterns.json` on startup and shown in
the UI as **COUNTS VERIFIED** or **COUNT MISMATCH**.

Business constraints honored in code:

- Every deal has exactly one objection. A turn never contributes more than one,
  no matter how many keywords it contains.
- A signal can only ever be driven by real counts from memory. `total == 0`
  means the signal is ignored, never invented.
- Drafts cite only real winning-response deals. For budget_freeze that is
  `D007`, `D009 (Marcus Chen, Marcus Chen)`; `cost_of_delay` is never used
  because its text needs a number this live deal does not have.
- The recovery pattern is kept honest including its one loss (D002): the UI
  says "4 of 5 ... were won", never hides the loss.

### The `clip` constant (documented addition)

SPEC 4 fixes `clip(x) = min(max(x, 0.05), 0.95)` and explicitly requires it be
documented: it is **not** a seed-derived constant. Without it, a competitor
with 5 losses and 0 wins (Vantage) gives a loss ratio of 1.0 and `ln(∞)` → NaN.
It is defined once as `CLIP_MIN`/`CLIP_MAX` in `app/config.py`.

## Decisions log

`DECISIONS.md` holds every judgement call (D1–D45) with a one-line reason, per
SPEC 0. Notable ones: the competitor name mention being the extraction cue
(D6); objection signals applied before competitor signals (D7); the combined
pattern weight 0.8 (D2); the local fallback reason being a top-level field so
the UI can render it (D27); Hindsight never being called from the event-loop
thread (D26); the LLM only ever suggesting, never commanding, facts (D29–D30);
negation-aware signal extraction with a 43-case labelled corpus (D41); and
per-browser sessions over a shared Hindsight bank with an idle-evicting LRU of
50 (D42).

## Verification report

### Verified by running (with output)

- Seed validation gate — `pytest tests/test_seed_loader.py` passed: 30/22/8,
  Priya deals_worked is empty, unknown objection tags rejected, seed bytes
  unchanged.
- Live Hindsight spike — bank `sales-team-shared`:
  - `python scripts/verify_memory.py` → 30 deals reconstructed, 22 lost, 8 won,
    `COUNTS VERIFIED` (exit 0). Raw pages: 106 memory units for 30 deals.
  - `python scripts/verify_lifespan.py` now returns exit 0:
    `{"backend": "hindsight", "fallback": false, "integrity_status":
    "COUNTS VERIFIED", "deals": 30, "lost": 22, "won": 8}`.
- Scoring trajectory — `python scripts/verify_trajectory.py` (exit 0) and
  `pytest tests/test_scoring.py`: `0.620 → 0.566 → 0.415 → 0.415 → 0.299 →
  0.418` with deltas `0 / -5.4 / -15.1 / 0 / -11.6 / +11.9`, each assertion
  within SPEC's ±0.02.
- Pipeline + API gate — `pytest tests/test_pipeline.py tests/test_api.py` and a
  curl run against a real uvicorn server reproduced the six-turn trajectory
  keyless, with the draft appearing at turn 3 and the turn-6 recovery credited.
- Frontend gate — `tests/test_api.py` asserts the served dashboard carries every
  SPEC 9 widget, is fully self-contained (no Tailwind CDN, no `/static/*`, data-URI
  favicon only), wires every endpoint the inline script drives (incl. `/api/deals`),
  and that the inline `node --check` parses; a live uvicorn smoke served `/` and all
  SPEC 8 endpoints plus `/api/deals` (30 records), demo script 6 turns, live score 0.62.
- LLM gate — `tests/test_llm.py`: a keyless run never constructs a Groq client
  and still reproduces the reference trajectory; a scripted fake Groq client
  proved invalid JSON, unknown tags, invented numbers and API errors all
  degrade to templates without raising.
- **Live Groq gate — verified** — `python scripts/verify_llm.py` → **exit 0**:
  the six-turn call on the real model engaged (extraction via LLM on every
  prospect turn, draft generated by Groq with citations D007/D009 intact) and
  the score trajectory was byte-identical to the reference. The live run found
  and fixed two bugs scripted tests could not (D32: doubled GROQ_BASE_URL; the
  `_client` name collision that made `reset_client()` break the factory).
- Full suite: `pytest tests/ -q` → **199 passed** (tests run keyless by
  default via `tests/conftest.py`; opt-in LLM tests patch a fake client).
- Close-the-loop gate (D35) — close a demo deal as lost; the autopsy returns,
  the draft stops once closed, the replay of the same six turns scores *harder*
  because the pattern grew to 7-of-11, exactly as designed.
- Extraction corpus gate — `scripts/eval_extraction.py` → **43/43 cases pass**,
  and `tests/test_extraction_cases.py` locks the corpus plus the demo call's
  turn signals (competitor at turn 2, budget freeze at turn 3, negation-aware
  behave, champion-left at turn 5, recovery email at turn 6).
- Validation & sessions gate — `tests/test_sessions_and_validation.py` (10
  tests): per-browser session isolation, header reuse, LRU eviction at the 50
  cap, duplicate-deal 409, extra-objection 422, unknown objection/stage 422,
  non-positive/large deal sizes 422, bad closed dates 422, oversized/empty
  events 422.
- Transcript gate — `tests/test_transcript.py` and the UI card: JSON and raw
  (plain-text, timestamped) transcripts import over `POST /api/transcript`
  (100-turn / 200 KB caps), each turn echoes its scored result, rep turns are
  attributed via speaker labels for follow-up drafting.
- End-to-end smoke — `scripts/smoke_e2e.py` (exit 0): spawns a *keyless*
  uvicorn on :8001, plays the six-turn demo against the reference trajectory
  ±0.02, verifies the turn-3 draft and the turn-6 recovery, closes as lost
  (7 lost of 11), replays to confirm the harder 7-of-11 warning, and imports a
  two-turn JSON transcript that is caught as `security_review_stall`.
- Packaging gate — `scripts/make_release.py` builds `dist/LostAgent_release.zip`
  and byte-scans it for key material.

### Not verified (honest gaps)

- **Literal browser clicking**: the SPEC 10.6 gate ("page loads; 6 turns
  produce a visible drop, reason, draft") is asserted via server responses,
  the served HTML, `node --check`, and a live uvicorn smoke — not by driving a
  real browser with Playwright/Selenium.
- **Hindsight key rotation**: the key was exposed in chat earlier and must be
  rotated before any external hand-off; `scripts/verify_memory.py` and
  `scripts/verify_lifespan.py` are the checks to re-run afterwards.

### What changed in the final sprint

- **Extraction is now negation-aware** (D41): "we're *not* choosing Acme" or "a
  competitor — well, not really" no longer fires competitor/objection signals;
  a 43-case labelled corpus (`tests/data/extraction_cases.json`) pins the
  exact spans and demo-call behaviour.
- **Per-browser sessions over shared memory** (D42): the app runs one shared
  Hindsight bank, but every browser gets its own view keyed by
  `X-Session-Id` (LRU of 50, idle-evicted), so `demo reset` cannot corrupt a
  teammate's running demo. A live store lock (RLock) makes every mutator
  reentrant-safe.
- **Real input validation**: events and deal records are length/payload
  validated with explicit 409/422s, and timestamps keep their `^YYYY-MM-DD$`
  contract.
- **Transcript import** (defect K): paste a raw call transcript (with or
  without `[hh:mm]` timestamps) or `POST` JSON — each turn is scored live, rep
  turns are detected for follow-up drafting, capped at 100 turns.
- **Integrations surface honesty** (D43): the Integrations panel marks
  Salesforce/HubSpot/Gong/Zoom as **Planned** and only ever exposes the two
  things that exist today — transcript import and `POST /api/events`.
- **Close-the-loop demo** (D35): closing a lost deal now returns an autopsy and
  makes the next replay warn harder (7-of-11), which is *the* scripted proof
  the memory loop closes.
- **A real end-to-end smoke** (`scripts/smoke_e2e.py`) spawns a keyless server
  and exercises all of the above over HTTP.
- **Chat with us + theming** (D45): a floating "Chat with us" panel answers
  questions (patterns, score, draft, competitors) with a template-only reply
  counted from the session's own records — deterministic, keyless, never
  hallucinated; after a lost close the same question recounts ("11 budget
  freeze deals… 4 won, 7 lost."). A manual light/dark toggle persists to
  `localStorage`, with OS auto-dark still honoured until the user chooses.
- Test suite grew 149 → 199 tests; every script under `scripts/` now has a
  documented exit contract.

### Known limitations (honest)

- The scoring weights and the pattern ratio are hand-picked to make the story
  obvious, not fit to real closed-won data — an uncalibrated heuristic.
- The base 30-deal team memory itself is *synthetic*: a real workspace would
  start from the customer's actual past deals, and the 22/30 team-lost baseline
  means an 11-deal pattern column is only illustrative.
- Extractor coverage is keyword-based with a measured (not claimed) accuracy
  surface; punctuation-heavy or heavily nested negation can still mislabel, and
  the marketed accuracy numbers live in the corpus report, not in prose.
- The score is one dial for an early-warning read; it does not decompose into
  a full attribution model or feed a forecast.
- Memory is single-tenant: one shared team bank with per-session views, not
  per-rep/per-team ACLs.
- The in-app "Chat with us" panel is a data-grounded template assistant, not an
  LLM: it answers with real stored counts and says so when a question is out of
  scope, rather than improvising.
- Browser-driven UI automation (Playwright/Selenium) remains unrun; UI behaviour
  is asserted through served HTML, `node --check`, and the HTTP smoke.

## Why this can pass the SPEC 12 definition of done

- `uvicorn` starts; the page loads offline-readable; clicking the 6 turns shows
  a visible score drop (gauge + sparkline + one-line reason) and a drafted
  follow-up with Copy — with no API keys set. Gate asserted by the full suite.
- With Hindsight credentials set, the badge shows **Hindsight** and
  **COUNTS VERIFIED** — proven by `scripts/verify_lifespan.py` (exit 0).
- Switching reps gives identical warnings and scores (shared memory), and
  adding a lost deal via `/api/retain` changes the next reason from "6 of 10"
  to "7 of 11" — asserted by `tests/test_api.py`.
- README maps Retain/Recall/Reflect/confidence-scored belief to code, states the
  seed data is synthetic, and documents the `clip` addition.
