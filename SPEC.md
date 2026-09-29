# SPEC.md — Lost-Deal Autopsy Agent (hackathon prototype)

## 0. How to use this file (read first, re-read often)
- This file plus the five files in `seed/` are the ONLY source of truth. Chat history, earlier plans and your own memory are NOT.
- Re-read this file at the start of every build step, and immediately after any context reset or compaction.
- Never write a number, deal ID, rep name, strategy name or quote from memory. Read it from `seed/*.json` or from code output.
- `seed/*.json` are read-only. If you think one is wrong, log it in `DECISIONS.md` and continue using it as-is.
- Do not ask questions. Make the most reasonable choice, log it in `DECISIONS.md` (one line: decision + why), and keep going.
- Keep `PROGRESS.md`: a checklist of the build steps in section 10. Tick a step only after its gate passes.
- When unsure of a library API (especially Hindsight), inspect the installed package (`pip show`, `python -c "help(...)"`, read its source) or the official docs. Never guess method names.
- The final report must separate "verified by running (with command output)" from "not verified".

## 1. What we are building
A sales agent with ONE shared memory of a team's past deals. During a live deal it (1) keeps a live win-probability score that updates on every call/email/CRM event, (2) explains each change in one line using real counts from team history, and (3) drafts a follow-up using language that worked in past deals that survived the same objection. Memory is shared across reps: the brand-new rep (Priya Nair, week 3, 0 deals worked) gets the same warnings as veterans.

## 2. Files in `seed/` (the source of truth)
| File | Contents |
|---|---|
| `deals.json` | 30 past deals. Fields: id, company, industry, deal_size, outcome (lost/won), objection_stage, stage_lost_at (null for won), closed_date, rep, competitors[], objections[] (exactly one per deal), stakeholders_in_room[], summary, winning_response ({objection, strategy, text} or null), recovery_signals[] |
| `demo_call.json` | Live deal LIVE-001 (Northwind Logistics, Evaluation, rep Priya Nair) and 6 scripted turns with `expected_signals` notes |
| `reps.json` | 3 reps: Marcus Chen (R1), Elena Rossi (R2), Priya Nair (R3, is_new, deals_worked = []) |
| `scoring_config.json` | All scoring constants (authoritative — see section 4) |
| `expected_patterns.json` | Ground-truth pattern counts for tests |

## 3. Verified ground truth (already checked against the data)
- 30 deals: 22 lost, 8 won. All 8 won deals survived an objection and have a `winning_response`. No lost deal has one.
- Every deal has exactly one objection. Use `objection_stage` for stage-scoped stats (won deals have `stage_lost_at = null`).
- Patterns (total / lost / won):
  - `objection:budget_freeze@Evaluation` 10 / 6 / 4
  - `objection:champion_left@Evaluation` 6 / 5 / 1
  - `competitor:Acme+objection:no_exec_sponsor` 5 / 4 / 1
  - `competitor:Acme` 7 / 6 / 1
  - `recovery:positive_reply_after_followup@budget_freeze` 5 / 1 / 4 (the one loss is D002 — keep that honest, do not hide it)
- Edge cases in the data that will break naive math: competitor `Vantage` is 5 / 5 lost / 0 won; `competitor_pricing` is 2 / 2 / 0. A loss ratio of 1.0 gives ln(∞).
- budget_freeze winning strategies: `phased_rollout` (D007), `cost_of_delay` (D008), `pilot_first` (D009), `cfo_one_pager` (D010). Other won deals: champion_left → `multi_thread_handoff` (D016), no_exec_sponsor → `exec_briefing` (D021), security_review_stall → `preempt_security_docs` (D026), legal_redlines → `joint_legal_call` (D028).
- Reference score trajectory (computed with the exact spec below, then rounded): 0.62 → T1 0.62 → T2 0.566 (−5.4 pts) → T3 0.415 (−15.1) → T4 0.415 (0) → T5 0.299 (−11.6) → T6 0.418 (+11.9). Tests assert ±0.02 on levels and the ranges in `demo_call.json` notes on deltas.

## 4. Scoring engine (exact spec — `scoring_config.json` overrides any earlier plan)
IMPORTANT: any earlier plan that used `p' = (n·s + k·q)/(n + k)` with `k = 4` is REPLACED by the rules below. Read every constant from `scoring_config.json`; hardcode none of them.

State: `p` (win probability), starting at `prior_win_probability` (0.62). It carries across events and is never recomputed from scratch.

For each newly detected signal, work in log-odds of the win probability. Let `L(x) = ln(x / (1 − x))`.
- Risk signal (objection or competitor): `loss_ratio = lost / total`, then `delta = logit_scale × L(clip(loss_ratio)) × signal_weights[key] × confidence`, and `p' = sigmoid(L(p) − max(0, delta))`.
- Recovery signal: use `win_ratio = won / total`, `delta = logit_scale × L(clip(win_ratio)) × signal_weights[key] × confidence`, and `p' = sigmoid(L(p) + max(0, delta))`.
- `confidence = min(1, total / confidence_full_at_n)`.
- `clip(x) = min(max(x, 0.05), 0.95)`. This constant is not in the config (my addition, so Vantage 5/0 cannot produce infinity); define it as a named constant and document it in the README.
- A risk signal can never raise the score; a recovery signal can never lower it. Finally clamp `p'` to `clamp.min`..`clamp.max`.
- Signal keys map to weights: `objection:<tag>`, `competitor`, `recovery:positive_reply_after_followup`.
- A signal with `total == 0` is ignored, and the reason line says "no team history for <signal>". Never invent a pattern.
- Dedupe: each signal key applies at most once per live deal (track `applied_signals`). Replaying an event id is a no-op (idempotent).
- Keep the history: event_id, score_before, score_after, delta_pts, signal_key, pattern text, confidence, timestamp.

Which stats feed a signal:
1. Objection signal: use `objection:<tag>@<live stage>` stats. If that has fewer than `min_deals_for_fatal_pattern` deals, fall back to all-stage stats and label the pattern "(all stages)".
2. If the live deal has both competitor Acme and objection no_exec_sponsor, use `competitor:Acme+objection:no_exec_sponsor` instead of the two separate signals. Otherwise a competitor uses `competitor:<name>` stats with the `competitor` weight.
3. Recovery uses `recovery:positive_reply_after_followup@<objection that triggered the draft>`.

## 5. Pipeline (one pipeline powers everything)
RETAIN → EXTRACT → RECALL → REFLECT → SCORE → EXPLAIN → DRAFT

1. RETAIN — on startup ingest all 30 deals into the memory backend (section 6). `POST /api/retain` adds a new past deal live; the next scoring run must reflect it.
2. EXTRACT — turn text into signals: `{objections[], competitors[], recovery[]}`.
   - Only extract from PROSPECT turns. Skip turns whose speaker contains "(Rep)" or matches the active rep. Reason: turn 4 is the rep saying "options that work within a freeze", which must produce ZERO signals.
   - LLM extraction returns strict JSON, validated against `objection_taxonomy` and the known competitors (Acme, Vantage). Reject unknown tags.
   - Keyword fallback (starting point, tune until the six demo turns pass): budget_freeze = "freeze", "hold on spend", "no budget"; champion_left = "moving over to", "moving to another team", "leaving", "new role"; no_exec_sponsor = "exec sponsor", "haven't looped in"; competitor_pricing = "cheaper", "lower price", "discount"; security_review_stall = "security review", "SOC 2"; legal_redlines = "redlines", "indemnity", "MSA"; timing_slip = "next quarter", "push"; integration_concern = "integration", "SSO".
   - Recovery `positive_reply_after_followup` fires only if (a) a draft exists, (b) it has been marked sent, and (c) the event is a prospect email with positive intent ("thanks for sending", "can we set up", "take this to").
3. RECALL — fetch similar past deals: same objection and/or competitor, same stage, then deal-size proximity; deterministic order (ties by `closed_date` desc). Return top 5 with rep, outcome, and "worked by you: yes/no" for the active rep.
4. REFLECT — compute the stated pattern for each signal: `{key, total, lost, won, loss_ratio, stage_scope}`. These numbers are computed in code from memory records, never by an LLM.
5. SCORE — section 4.
6. EXPLAIN — reason line, e.g. "Dropped 15 pts: budget freeze mentioned, and 6 of 10 past budget_freeze deals at Evaluation were lost." Round pts to integers for display only. Numbers must appear verbatim.
7. DRAFT — trigger only when the selected pattern is fatal: `loss_ratio >= fatal_loss_ratio` AND `total >= min_deals_for_fatal_pattern` AND the signal is an objection (competitor-only signals never trigger drafts). Budget_freeze at 6/10 = 0.6 IS fatal (>=).
   - Retrieve `winning_response` texts from won deals whose `winning_response.objection` equals the triggering objection.
   - For budget_freeze, the draft offers TWO options adapted from `phased_rollout` and `pilot_first` (turn 4 promises "a couple of options"; turn 6's reply refers to "start smaller"). Log this in `DECISIONS.md`.
   - Write in Priya's voice, addressed to Maria Torres at Northwind Logistics. Only use facts present in `demo_call.json` and `deals.json`. No invented figures, prices, ROI percentages, dates or names. If a strategy needs a number that isn't provided (`cost_of_delay`), do not use it.
   - Show "Based on: D007, D009 (Marcus Chen, Marcus Chen)" — real IDs and reps only.
   - Turn 5 (champion_left is also fatal, 5/6): do NOT replace the main draft. Add a secondary "heads-up" card using D016's `multi_thread_handoff` text as a suggested talking point.
   - The draft stays visible after it appears. Demo turn 6 marks it sent (add `POST /api/followup/sent`; the Copy button calls it too).

## 6. Memory layer
- Interface `MemoryStore` with `retain()`, `recall()`, `reflect()`.
- Default `MEMORY_BACKEND=hindsight`. Bank id `sales-team-shared` (one bank for the whole team, never per rep). Fallback `local` only when Hindsight is unconfigured or unreachable.
- Fallback must be VISIBLE: a red badge "LOCAL FALLBACK — Hindsight not active" in the UI and in `/api/memory/stats`. Never show a Hindsight badge unless Hindsight calls actually succeeded.
- Do the Hindsight spike right after the scaffold, before scoring. Install `hindsight-client` in a Python 3.11/3.12 venv (check `python --version`; if only 3.14 is available, verify the install first). Read the official docs (https://hindsight.vectorize.io/ and https://github.com/vectorize-io/hindsight) and inspect the installed package for real method names.
- Retain each deal as one memory item: a deterministic text rendering of the deal, with the normalized fields (id, outcome, objection, objection_stage, competitors, rep, strategy, deal_size, source) in metadata/tags, so recall can filter on them.
- Recall may return only top-k. Counts must NOT be computed from a truncated list. Verify recall can return all matching deals (raise the limit, page, or filter by tags). If it cannot, log it and design around it explicitly.
- Startup integrity check: reconstruct all deal records from the memory backend and compare with `expected_patterns.json`. Show `COUNTS VERIFIED` or `COUNT MISMATCH` in the UI and in `/api/memory/stats`. On mismatch, do not silently continue.
- REFLECT: numbers come from the computed counts. When the backend is Hindsight, optionally also call its reflect operation for a narrative labelled "Hindsight reflection". It is display-only; if it contradicts the computed counts, show the computed counts.
- Deals added via `/api/retain` are tagged `source=live_added`.

## 7. LLM guardrails
- Model `claude-haiku-4-5-20251001` (override with `ANTHROPIC_MODEL`), key from `ANTHROPIC_API_KEY` in `.env`. The LLM is optional; without a key, every LLM step falls back to deterministic templates and the demo must fully work.
- The LLM only phrases (extraction JSON, reason wording, email drafting). All numbers, IDs, reps and winning-response content are passed in.
- Validate every LLM output: reason lines must contain the exact `lost of total` numbers; drafts may only cite deal IDs that were passed in; any digit sequence in a draft must exist in the provided context. If validation fails, retry once, then use the template.
- Any API error degrades to templates; it never raises to the client.

## 8. API
- `GET /api/deals/live` — live deal + current score state
- `POST /api/events` — ingest one event → `{score, delta, reason, confidence, patterns[], recalled_deals[], draft_followup|null, secondary_tip|null}`
- `POST /api/demo/step` — advance the scripted call one turn (same pipeline as `/api/events`)
- `POST /api/demo/reset` — reset live-deal state (score, history, applied signals, draft) but keep memory
- `POST /api/retain` — add a past deal
- `POST /api/followup/sent` — mark the draft as sent
- `GET /api/memory/stats` — deal/objection/pattern counts, backend in use, fallback flag, integrity status
- `GET /api/reps`, `POST /api/rep/select`

## 9. Frontend (single static `index.html`, vanilla JS, Tailwind CDN, served by FastAPI)
- Header: rep selector (default Priya Nair, badge from `reps.json`), memory backend badge, integrity badge, "N lost deals in shared memory".
- Left: transcript streaming turn by turn, "Next turn" button and "Auto-play" toggle.
- Center: animated win-probability gauge, sparkline of history, confidence, one-line reason with red/green delta.
- Right: "Patterns from team history" cards and "Similar past deals" list (rep, outcome, worked by you).
- Bottom: "Drafted follow-up" panel that slides in when a fatal pattern is detected, with Copy and a "Based on: <deals>" line. Secondary heads-up card for turn 5.
- "Add lost deal" form → `/api/retain`. Stubbed "Integrations" section listing Salesforce/HubSpot as planned.
- Tailwind CDN is a network dependency: add a small critical-CSS block so the page stays readable offline.

## 10. Build order with gates (do not skip a gate)
1. Scaffold — venv, `requirements.txt`, `requirements-hindsight.txt`, `.env.example`, `.gitignore` (includes `.env`), pydantic models, `DECISIONS.md`, `PROGRESS.md`. Gate: app imports cleanly.
2. Seed loader — validate seed files; reject unknown objection tags; assert 30/22/8; assert Priya's `deals_worked` is empty; assert reps' `deals_worked` equal the `rep` field in deals. Gate: tests pass.
3. Hindsight spike — real retain/recall/reflect against the bank; verify recall returns all 30 deals; integrity check. Gate: `COUNTS VERIFIED` on Hindsight, or a logged, visible fallback. Also implement `LocalMemory` here.
4. Scoring engine — section 4. Gate: reference trajectory reproduced with the six demo turns.
5. Pipeline + API + demo runner. Gate: full 6-turn run via pytest and curl with no API keys.
6. Frontend. Gate: page loads; 6 turns produce a visible drop, reason, draft.
7. LLM extraction, reason and draft with validation and fallbacks.
8. Polish — animations (score-drop flash, draft slide-in), README, `.env.example`.

## 11. Required tests
- All counts equal `expected_patterns.json` exactly; totals 30/22/8.
- Trajectory matches section 3 (±0.02); deltas fall inside the ranges in `demo_call.json` notes.
- Turns 1 and 4 produce zero signals and delta 0.
- Signals apply once (dedupe); event replay is idempotent; order matters; no NaN or infinity for Vantage 5/0 and competitor_pricing 2/0; score stays inside the clamp.
- Draft appears at turn 3 only; competitor-only signals never trigger a draft; the draft cites only D-IDs that have a winning_response for the triggering objection; every number in the reason and the draft is traceable to the data.
- Priya vs a veteran rep produce identical scores and warnings.
- Adding a lost budget_freeze@Evaluation deal changes the next turn-3 reason from "6 of 10" to "7 of 11".
- Hindsight integration test runs only if `HINDSIGHT_API_KEY` and `HINDSIGHT_BASE_URL` exist; otherwise it is skipped with a clear log line.
- No secrets in the repo (grep test for key patterns).

## 12. Definition of done
- `uvicorn` starts, the page loads, and clicking through the 6 turns shows a visible score drop with a grounded reason and a drafted follow-up with no API keys set.
- With Hindsight credentials set, the badge shows Hindsight and `COUNTS VERIFIED`.
- Switching reps gives identical warnings; adding a lost deal changes a pattern count on the next run.
- README maps Retain / Recall / Reflect / confidence-scored belief to the code, states the seed data is synthetic and engineered to produce clear patterns, and lists the `clip` constant as a documented addition.
- Final report: what was verified by running (with output) versus what was not.
