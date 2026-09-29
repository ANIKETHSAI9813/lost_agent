# 3-minute Demo Script

Live demo for _LostAgent_ — the sales rep's hindsight copilot. All of this runs
with **no API keys** (the memory layer falls back to LOCAL and the score is
byte-identical; see `scripts/smoke_e2e.py` proving it end-to-end).

Prep: `python -m uvicorn app.main:app` at http://127.0.0.1:8000. If a previous
server is still running on :8000, kill it first so the fresh-install clean-start
(D35) retires any leftover demo deals from shared memory.

---

**The problem (20s).** "Close won" or "close lost." Every CRM tracks the outcome,
but the *pattern* — why this one went sideways — stays inside one rep's head, and
a new rep re-learns it the hard way. LostAgent writes that pattern down every
single call and lets any teammate ask it "has anyone seen this before?"

**The device (20s).** Score on the right, 0–100, live during the call. It's not
a vibe — every point is a named, keyword-extracted signal: budget freeze,
competitor mention, security-review stall, champion leaving. What changed the
score sits right under it, with the exact words.

**Play the call (60s).** Hit **Demo: play a saved call**. Turn 3 — the buyer
says "we have no budget until next quarter," the score drops 15 points, and the
box under the reason clocks a **drafted follow-up** the rep can send. Turn 5 a
silent org chart leaks the champion left; turn 6 the rep lands the recovery
email and the score rebounds — and the app credits the *recovery*, not the call.

**Close the loop (30s).** **Log this call as lost.** An autopsy card appears —
the one-word theme, the turn the inflection happened, what the drafted follow-up
would have fixed. The pattern column flips: now **7 of 11** team deals in
Evaluation have lost.

**The memory story (40s).** Click **Replay**. Same six turns, same person — but
the *turn-3 score drops harder*, because the pattern now says 7-of-11, not 6-of-10.
That is hindsight memory operating on the dashboard: closing one small deal
changed what a future rep will be warned with. Switch to a fresh browser tab —
**its own session** starts clean, your tab keeps its run; shared bank, no
cross-tab corruption.

**Teammate logs a win (30s).** In the second tab, log a **Won** Evaluation deal
and open **Hindsight insights**: the card summarizes what teammates have actually
retained — the themes, the words, what tends to recur.

**Integrations honesty (20s).** The Integrations panel: transcript import and
`POST /api/events` are **Available today**; Salesforce / HubSpot / Gong / Zoom
are **Planned** — nothing claims "Connected" that isn't. Paste a raw call
transcript (timestamps optional) into **Import a real call** and watch each turn
get scored and the stall caught.

**Close.** "Win or lose, this deal just made every future call slightly smarter."
That's the product: hindsight for a rep who's on the phone right now.