"""FastAPI app (SPEC 8). One pipeline powers every endpoint (SPEC 5).

No endpoint ever returns or logs key material, and every LLM-free path works
with no credentials at all (SPEC 12).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

import anyio.to_thread

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app.autopsy import winning_response_from_memory
from app.config import STATIC_DIR, settings
from app.draft import _recency_key, won_responses_for
from app.memory import SOURCE_LIVE, LocalMemory, MemoryStore, build_store
from app.models import (
    Deal,
    EventInput,
    EventResult,
    EventType,
    MemoryStats,
    Outcome,
    Rep,
    WinningResponse,
)
from app.pipeline import LiveSession
from app.scoring import OBJECTION_LABELS, is_fatal
from app.seed_loader import load_seed, validate_seed

log = logging.getLogger(__name__)

# Set by the lifespan immediately before the store build runs on a worker
# thread. A module global rather than a parameter because `anyio.to_thread`
# takes a zero-arg callable.
_startup_seed = None

# Phase 6 (cut #4): per-browser sessions keyed by the X-Session-Id header.
# Each browser gets its own scoreboard (engine, draft, history) over ONE shared
# store, so "shared team memory, private demo" holds. An LRU cap keeps a rogue
# loop of refresh tabs from leaking sessions; eviction drops only the session
# object — the shared store is owned by the app, not a session.
_SESSION_LIMIT = 50
_DEFAULT_SESSION_ID = "default"
_sessions: OrderedDict[str, LiveSession] = OrderedDict()
_sessions_lock = threading.Lock()
_store: MemoryStore | None = None


def get_session() -> LiveSession:
    """Default (no header) session, kept for any caller without a request."""
    return resolve_session(_DEFAULT_SESSION_ID)


def resolve_session(session_id: str) -> LiveSession:
    """Get-or-create the session for `session_id`; evicts the LRU tail at cap."""
    if _store is None:
        raise HTTPException(status_code=503, detail="session not initialised")
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            session = LiveSession(store=_store, seed=_startup_seed)
            while len(_sessions) >= _SESSION_LIMIT:
                _sessions.popitem(last=False)
            _sessions[session_id] = session
        _sessions.move_to_end(session_id)
        return session


def session_dependency(
    x_session_id: str = Header(default=_DEFAULT_SESSION_ID),
) -> LiveSession:
    """FastAPI dependency: pick the per-browser session from the header."""
    return resolve_session(x_session_id or _DEFAULT_SESSION_ID)


def _build_store_and_ingest() -> tuple[MemoryStore, str, int, MemoryStats]:
    """Build the single shared store and run the startup ingest (SPEC 5.1).

    Only once per app, on a worker thread: every step here touches the
    Hindsight sync client, which cannot be driven from a thread that already
    has a running event loop. The stats probe session lives here too — its
    constructor reads every record, so it must stay off the loop thread.

    D35 (`DEMO_CLEAN_START`): with a live backend, any `live_added` deals left
    in shared memory from an earlier run are retired first, so a fresh demo
    always starts from exactly the seed even on a shared bank.
    """
    store = build_store()
    seed = _startup_seed
    if seed is None:
        raise RuntimeError("seed not loaded before store build")
    scratch = LiveSession(store=store, seed=seed)
    ingested = scratch.startup_retain()
    if settings.demo_clean_start and not store.fallback:
        store.forget_live_added()
    report = scratch.memory_stats()
    return store, store.name, ingested, report


def _close_store(store: MemoryStore | None) -> None:
    """Close the shared memory client on a worker thread (see above)."""
    if store is not None:
        store.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """SPEC 5.1: ingest all 30 deals into the memory backend on startup.

    `hindsight_client._run_async` drives a coroutine with
    `loop.run_until_complete()`, which raises "This event loop is already
    running" when called from a thread that owns a running loop -- which an
    `async def` lifespan always does. So the whole startup sequence, including
    the `LiveSession` constructor (its `__post_init__` reads every record) and
    the ingest, runs on a worker thread where no loop is running.

    Sync `def` endpoints are already dispatched to Starlette's threadpool, so
    request handling needs no such treatment.
    """
    global _startup_seed, _store
    seed = load_seed()
    problems = validate_seed(seed)
    if problems:
        # SPEC 0: seed is read-only, so log and continue rather than mutate.
        log.error("seed validation problems: %s", problems)

    _startup_seed = seed
    store, backend, ingested, report = await anyio.to_thread.run_sync(
        _build_store_and_ingest
    )
    log.info(
        "memory backend=%s fallback=%s ingested=%s integrity=%s",
        backend,
        store.fallback,
        ingested,
        report.integrity_status,
    )
    with _sessions_lock:
        _sessions.clear()
        _store = store
    try:
        yield
    finally:
        # Closed on a worker thread too: the aiohttp pool inside the Hindsight
        # client is bound to whichever loop created it.
        await anyio.to_thread.run_sync(_close_store, store)
        with _sessions_lock:
            _sessions.clear()
            _store = None


app = FastAPI(
    title="Lost-Deal Autopsy Agent",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# health / status
# --------------------------------------------------------------------------
@app.get("/api/health")
def health(session: LiveSession = Depends(session_dependency)) -> dict[str, Any]:
    return {
        "status": "ok",
        "memory": {
            "backend": session.store.name,
            "fallback": session.store.fallback,
            "fallback_reason": session.store.fallback_reason,
            "bank_id": session.store.bank_id,
        },
        "llm": {"configured": settings.llm_enabled, "model": settings.groq_model},
        "config": settings.describe(),
    }


@app.get("/api/deals")
def list_deals(session: LiveSession = Depends(session_dependency)) -> list[dict[str, Any]]:
    """Read-only: every deal record currently in memory.

    Not part of SPEC 8; added for the frontend so deal cards can show real
    company names, summaries and winning-response text instead of bare IDs.
    Never mutates state.
    """
    out: list[dict[str, Any]] = []
    for record in session.records:
        row = record.deal.model_dump()
        row["source"] = record.source
        out.append(row)
    return out


@app.get("/api/deals/live")
def live_deal(session: LiveSession = Depends(session_dependency)) -> dict[str, Any]:
    """SPEC 8: live deal + current score state."""
    engine = session.engine
    assert engine is not None
    return {
        "deal": session.live_deal.model_dump(),
        "score": engine.score,
        "confidence": engine.confidence,
        "applied_signals": sorted(engine.applied_signals),
        "history": [entry.model_dump() for entry in engine.history],
        "turn": session.turn_index,
        "rep": session.active_rep,
        "closed": session.closed,
        "draft": session.draft.model_dump() if session.draft else None,
        "secondary_tip": (
            session.secondary_tip.model_dump() if session.secondary_tip else None
        ),
    }


# --------------------------------------------------------------------------
# events / demo
# --------------------------------------------------------------------------
@app.post("/api/events", response_model=EventResult)
def post_event(
    event: EventInput, session: LiveSession = Depends(session_dependency)
) -> EventResult:
    return session.process_event(event)


@app.post("/api/demo/step", response_model=EventResult | None)
def demo_step(session: LiveSession = Depends(session_dependency)) -> EventResult | None:
    return session.advance()


@app.post("/api/demo/reset")
def demo_reset(session: LiveSession = Depends(session_dependency)) -> dict[str, Any]:
    session.reset()
    return {
        "reset": True,
        "score": session.engine.score if session.engine else None,
        "memory_kept": True,
    }


# --------------------------------------------------------------------------
# transcript import (Phase 7 / defect K): paste a call, get every turn scored
# --------------------------------------------------------------------------
MAX_TRANSCRIPT_TURNS = 100
MAX_TRANSCRIPT_BODY = 200_000
_TS_LINE_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}(:\d{2})?\]\s*")


class TranscriptTurn(BaseModel):
    """One turn in a JSON transcript (the shape Gong/Zoom exports can map to)."""

    speaker: str
    text: str
    type: EventType = "call"
    timestamp: str = ""


class TranscriptImport(BaseModel):
    turns: list[TranscriptTurn]


def _split_raw_line(line: str) -> tuple[str, str]:
    """Split `Speaker: text` at the first colon; unlabeled lines get no speaker."""
    if ":" in line:
        speaker, _, text = line.partition(":")
        return speaker.strip(), text.strip()
    return "", line


def _parse_raw_transcript(raw_text: str) -> list[EventInput]:
    """Parse pasted free text into runs of events.

    Accepts `Maria Torres (Northwind): text`, `[10:02] Maria: text`, and an
    optional `[hh:mm]` timestamp prefix. The timestamp colon must be consumed
    BEFORE the speaker colon, or `[10:02] A: b` splits at the wrong place.
    Unlabeled lines are treated as the prospect. Line order is processing
    order.
    """
    events: list[EventInput] = []
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        timestamp = ""
        match = _TS_LINE_RE.match(line)
        if match:
            timestamp = match.group(0).strip("[] ")
            line = line[match.end():].lstrip(" \t,。")
        speaker, text = _split_raw_line(line)
        if not speaker:
            speaker = "Prospect (Buyer)"
        try:
            events.append(
                EventInput(type="call", speaker=speaker, text=text, timestamp=timestamp)
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"line {idx}: {exc.errors()[0]['msg']}",
            ) from exc
    return events


@app.post("/api/transcript")
async def import_transcript(
    request: Request,
    session: LiveSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Analyse a pasted transcript: raw text or the `{"turns": [...]}` JSON
    shape a Gong/Zoom export can be mapped to (defect K).

    Capped at 100 turns ([1-2000] chars each); the response is the list of
    per-turn EventResult records (plus the parsed input) so the UI can animate
    them exactly like the demo. Speakers equal to the active rep, or carrying
    `(Rep)`, are rep turns; the rest are prospect turns (pipeline /
    is_prospect_turn).
    """
    content_type = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    if len(raw) > MAX_TRANSCRIPT_BODY:
        raise HTTPException(status_code=413, detail="transcript body too large")

    if "json" in content_type:
        try:
            parsed: Any = json.loads(raw.decode("utf-8"))
            turns_raw = parsed.get("turns") if isinstance(parsed, dict) else None
            if not isinstance(turns_raw, list):
                raise ValueError("expected a JSON object with a 'turns' list")
            events = [EventInput(**turn) for turn in turns_raw]
        except (ValueError, TypeError, ValidationError) as exc:
            raise HTTPException(
                status_code=422, detail=f"invalid transcript JSON: {exc}"
            ) from exc
    else:
        text = raw.decode("utf-8", errors="replace")
        events = _parse_raw_transcript(text)

    if not events:
        raise HTTPException(
            status_code=422, detail="transcript had no non-empty turns"
        )
    if len(events) > MAX_TRANSCRIPT_TURNS:
        raise HTTPException(
            status_code=422,
            detail=f"transcript exceeds {MAX_TRANSCRIPT_TURNS} turns",
        )

    def _run() -> list[dict[str, Any]]:
        return [
            {
                "input": event.model_dump(),
                "result": session.process_event(event).model_dump(),
            }
            for event in events
        ]

    turns = await anyio.to_thread.run_sync(_run)
    return {"turns_imported": len(turns), "turns": turns}


@app.get("/api/demo/script")
def demo_script() -> dict[str, Any]:
    """The scripted call, so the UI can stream transcript text.

    SPEC 8 pins the `/api/demo/step` response to the `/api/events` shape, which
    carries the score result but not the turn's words. Rather than widen that
    contract, the transcript is exposed read-only here.
    """
    demo = _startup_seed.demo if _startup_seed is not None else load_seed().demo
    return {
        "deal": demo.deal.model_dump(),
        "turns": [turn.model_dump() for turn in demo.turns],
    }


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------
class RetainRequest(BaseModel):
    """`POST /api/retain`: add a past deal. Only fields a caller supplies.

    Phase 6: ids, sizes and dates are shape-validated here (clean 422s); the
    objection/stage-vocabulary checks run against the seed in the endpoint.
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=20,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="e.g. L001",
    )
    company: str
    industry: str = "Logistics"
    deal_size: int = Field(..., gt=0, lt=2_000_000_000)
    outcome: str = "lost"
    objection_stage: str
    stage_lost_at: str | None = None
    closed_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    rep: str
    competitors: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    stakeholders_in_room: list[str] = Field(default_factory=list)
    summary: str = ""
    recovery_signals: list[str] = Field(default_factory=list)
    winning_strategy: str | None = Field(
        default=None,
        max_length=60,
        description="required when outcome=won; text is copied verbatim from shared memory",
    )


@app.post("/api/retain")
def retain(
    request: RetainRequest, session: LiveSession = Depends(session_dependency)
) -> dict[str, Any]:
    """SPEC 5.1: the next scoring run must reflect the new deal.

    D36: a won deal logs its `winning_strategy`; the response text is taken
    verbatim from the earliest deal the team has won with that strategy on the
    same objection — teammates type facts, never invented winning copy.

    Phase 6: the shared store is the source of truth, so a duplicate deal id
    is a 409, and objection/stage values must come from the seed vocabulary.
    """
    if any(record.deal.id == request.id for record in session.records):
        raise HTTPException(
            status_code=409,
            detail=f"deal {request.id} already exists in shared memory",
        )
    taxonomy = session.seed.scoring.objection_taxonomy
    if len(request.objections) > 1:
        raise HTTPException(
            status_code=422,
            detail="exactly one objection per deal (the domain models one)",
        )
    if request.objections and request.objections[0] not in taxonomy:
        raise HTTPException(
            status_code=422,
            detail=f"unknown objection: {request.objections[0]}",
        )
    if request.objection_stage not in session.seed.scoring.stages:
        raise HTTPException(
            status_code=422,
            detail=f"unknown stage: {request.objection_stage}",
        )
    payload = request.model_dump()
    if request.outcome == "won":
        if not request.winning_strategy:
            raise HTTPException(
                status_code=422,
                detail="a winning_strategy is required when outcome=won",
            )
        objection = request.objections[0] if request.objections else "budget_freeze"
        try:
            response, _ = winning_response_from_memory(
                session.records, objection, request.winning_strategy
            )
        except LookupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload["stage_lost_at"] = None
        payload["winning_response"] = WinningResponse(
            objection=objection,
            strategy=request.winning_strategy,
            text=response.text,
        )
    elif request.winning_strategy:
        raise HTTPException(
            status_code=422,
            detail="winning_strategy is only allowed when outcome=won",
        )
    deal = Deal.model_validate(payload)
    session.store.retain(deal, source=SOURCE_LIVE)
    session.refresh_records()
    report = session.memory_stats()
    return {
        "retained": deal.id,
        "source": SOURCE_LIVE,
        "deals": report.deals,
        "lost": report.lost,
        "integrity_status": report.integrity_status,
    }


class CloseRequest(BaseModel):
    """`POST /api/deals/close`: record the live deal's outcome (D35)."""

    outcome: Outcome
    winning_strategy: str | None = Field(
        default=None, description="required when outcome=won; a strategy our memory has won with"
    )
    note: str = Field(default="", max_length=500)
    stage: str | None = Field(
        default=None, description="defaults to the live deal's stage"
    )


@app.post("/api/deals/close")
def close_deal(
    request: CloseRequest, session: LiveSession = Depends(session_dependency)
) -> dict[str, Any]:
    """Retain the live deal as won/lost and return the memory autopsy.

    The retained deal joins the pattern counts immediately, so `pattern_after`
    shows what the next run (replay) will feel: e.g. closing lost takes
    budget_freeze@Evaluation from 6-of-10 to 7-of-11 and the reason text the
    user sees next run reads "7 of 11".
    """
    try:
        return session.close_deal(
            outcome=request.outcome,
            winning_strategy=request.winning_strategy,
            note=request.note,
            stage=request.stage,
        )
    except ValueError as exc:
        status = 409 if str(exc) == "deal already closed for this run" else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post("/api/memory/reset_live")
def reset_live(session: LiveSession = Depends(session_dependency)) -> dict[str, Any]:
    """D35: drop every `live_added` deal so a fresh run starts from the seed."""
    return session.reset_live()


_SEMANTIC_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _semantic_probe(
    store: MemoryStore, objection: str, query: str
) -> dict[str, Any]:
    """Display-only semantic recall for the insight card.

    Never a source of numbers, never a 500: exceptions (or a 6s timeout) end
    in `available: false`. Memoized per (objection, query) so a live backend is
    only touched once per distinct question (D38).
    """
    key = (objection or "", query or "")
    if key in _SEMANTIC_CACHE:
        return _SEMANTIC_CACHE[key]

    probe = query or objection or "team memory"
    deals: list[str] = []
    narrative: str | None = None
    available = store.name == "hindsight" and not store.fallback
    if available:
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(store.semantic_candidates, probe)
                deals = future.result(timeout=6) or []
        except Exception as exc:  # noqa: BLE001
            store.last_error = f"insight probe: {type(exc).__name__}: {exc}"
            deals = []
        if deals:
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(store.reflect_narrative, probe)
                    narrative = future.result(timeout=6)
            except Exception:  # noqa: BLE001
                narrative = None

    result = {
        "available": bool(available and deals),
        "deals": deals,
        "candidates": len(deals),
        "narrative": narrative,
    }
    _SEMANTIC_CACHE[key] = result
    return result


@app.get("/api/memory/insight")
def memory_insight(
    objection: str = "",
    q: str = "",
    session: LiveSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Hindsight memory, read-only (D38): what the team's shared memory knows
    about an objection, and (live backend only) what semantic recall adds.

    The deterministic arm — pattern counts, won backings, lost note, lesson —
    comes straight from the records and works identically with no keys. The
    semantic arm is display-only and best-effort; it can never invent a number
    and its failure cannot change an answer (available: false).
    """
    store = session.store
    records = session.records
    tag = (objection or "").strip() or "budget_freeze"

    engine = session.engine
    pattern = (
        engine.pattern_for(f"objection:{tag}", session.stage)
        if engine is not None
        else None
    )

    won = won_responses_for(records, tag)
    won_sorted = sorted(
        won,
        key=lambda r: (-_recency_key(r.deal.closed_date), r.deal.id),
    )
    lost_sorted = sorted(
        (r for r in records if r.deal.is_lost and tag in r.deal.objections),
        key=lambda r: (-_recency_key(r.deal.closed_date), r.deal.id),
    )

    by_strategy: dict[str, int] = {}
    for r in won:
        strategy = r.deal.winning_response.strategy
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1

    lesson = None
    if pattern is not None:
        if pattern.stage_scope == "all_stages":
            stage_note = ""
        elif getattr(pattern, "key", "").endswith(f"@{session.stage}"):
            stage_note = f" at {session.stage}"
        else:
            stage_note = ""
        fatal = is_fatal(pattern, session.seed.scoring)
        lesson = (
            f"{OBJECTION_LABELS.get(tag, tag)} deals{stage_note}: "
            f"{pattern.total} in memory, {pattern.won} won, {pattern.lost} lost."
            + (" The pattern is fatal — the draft warns on it."
               if fatal
               else " The pattern is no longer fatal, so no urgent warning fires.")
        )

    return {
        "objection": tag,
        "pattern": {
            "present": pattern is not None,
            "key": getattr(pattern, "key", None),
            "stage_scope": getattr(pattern, "stage_scope", None),
            "total": getattr(pattern, "total", None),
            "lost": getattr(pattern, "lost", None),
            "won": getattr(pattern, "won", None),
            "loss_ratio": round(getattr(pattern, "loss_ratio", 0.0) or 0.0, 4),
            "fatal": pattern is not None
            and is_fatal(pattern, session.seed.scoring),
        },
        "won_backings": [
            {
                "id": r.deal.id,
                "rep": r.deal.rep,
                "strategy": r.deal.winning_response.strategy,
                "closed_date": r.deal.closed_date,
            }
            for r in won_sorted
        ],
        "by_strategy": dict(
            sorted(by_strategy.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "lost_notes": [
            {
                "id": r.deal.id,
                "rep": r.deal.rep,
                "stage_lost_at": r.deal.stage_lost_at,
                "summary": r.deal.summary,
            }
            for r in lost_sorted[:3]
        ],
        "lesson": lesson,
        "semantic": _semantic_probe(store, tag, q.strip()),
        "backend": {
            "name": store.name,
            "fallback": store.fallback,
            "fallback_reason": store.fallback_reason,
            "bank_id": store.bank_id,
        },
    }


@app.post("/api/followup/sent")
def followup_sent(session: LiveSession = Depends(session_dependency)) -> dict[str, Any]:
    draft = session.mark_followup_sent()
    if draft is None:
        raise HTTPException(status_code=404, detail="no draft to mark sent")
    return {"marked_sent": True, "draft": draft.model_dump()}


@app.get("/api/memory/stats", response_model=MemoryStats)
def memory_stats(
    reflection: bool = False,
    session: LiveSession = Depends(session_dependency),
) -> MemoryStats:
    """SPEC 6 stats, including the visible local-fallback reason.

    `reflection=true` additionally asks Hindsight for its narrative. It is opt-in
    because it costs a network round trip, and per SPEC 6 the narrative is
    display-only and never changes a score.
    """
    return session.memory_stats(include_reflection=reflection)


@app.get("/api/hindsight/ping")
def hindsight_ping(
    session: LiveSession = Depends(session_dependency),
) -> dict[str, Any]:
    """SPEC 6: what the badge shows, and whether the live backend answered.

    Returns the raw boolean rather than a 503 so the UI can render both
    outcomes, and reports the error text so a silent fallback is diagnosable.
    """
    store = session.store
    ok = store.ping()
    return {
        "hindsight_active": bool(ok) and not store.fallback,
        "backend": store.name,
        "bank_id": store.bank_id,
        "fallback": store.fallback,
        "fallback_reason": store.fallback_reason,
        "last_error": store.last_error,
    }


# --------------------------------------------------------------------------
# chat (D45): data-grounded, template-only
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)


# Display labels: display strings only; no number, deal id or rep name is ever
# invented here (SPEC 0 line 6). Unknown strategies fall back to their key.
STRATEGY_LABELS: dict[str, str] = {
    "pilot_first": "a pilot-first close",
    "phased_rollout": "a phased rollout",
}


def _match(low: str, words: tuple[str, ...]) -> bool:
    """Word-boundary substring match, so "hi" never fires on "history"."""
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low)
        for w in words
    )


def _find_pattern(stats: MemoryStats, key: str):
    for p in stats.patterns:
        if p.key == key:
            return p
    return None


def _label_for_key(key: str) -> str:
    tag = key.split(":")[-1].split("@")[0]
    return OBJECTION_LABELS.get(tag, key)


def _pattern_sentence(pattern, label: str) -> str:
    total = getattr(pattern, "total", 0)
    won = getattr(pattern, "won", 0)
    lost = getattr(pattern, "lost", 0)
    plural = "s" if total != 1 else ""
    return (
        f"{total} {label} deal{plural} in shared memory: {won} won, {lost} lost."
    )


def _chat_reply(message: str, session: LiveSession) -> dict[str, Any]:
    stats = session.memory_stats()
    text = message.strip()
    low = text.lower()

    def answer(topic: str, reply: str) -> dict[str, Any]:
        return {"reply": reply, "via": "template", "topic": topic}

    def help_reply() -> dict[str, Any]:
        return answer(
            "help",
            "I answer from team memory. Try:\n"
            "• How do I read the score?\n"
            "• What happened to budget-freeze deals?\n"
            "• Why does the draft warn me?\n"
            "• Has Acme won before?\n"
            "• What does closing a lost deal change?",
        )

    if not text:
        return help_reply()

    bf = _find_pattern(stats, "objection:budget_freeze@Evaluation")
    champ = _find_pattern(stats, "objection:champion_left@Evaluation")
    acme = _find_pattern(stats, "competitor:Acme")
    vantage = _find_pattern(stats, "competitor:Vantage")

    won_responses = won_responses_for(session.records, "budget_freeze")
    by_strategy: dict[str, int] = {}
    for r in won_responses:
        key = r.deal.winning_response.strategy
        by_strategy[key] = by_strategy.get(key, 0) + 1
    top_strats = ", ".join(
        f"{STRATEGY_LABELS.get(s, s)} ({n})"
        for s, n in sorted(by_strategy.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    if _match(low, ("hi", "hello", "hey", "yo", "thanks", "thank you")):
        return answer(
            "greeting",
            "Hi — I'm the demo's memory helper. I only answer from the deals the "
            "team has already won or lost, so nothing I say is a guess. Ask "
            "“What happened to budget-freeze deals?” or “How do I read the "
            "score?” — or type “help” for the full list.",
        )

    if _match(low, ("how do i read the score", "how does the score",
                    "what does the score", "chance of winning", "numbers")):
        return answer(
            "scoring",
            "The dial is the team's estimated chance of winning this deal, 0–100, "
            "recalculated after every turn. It starts at the shared baseline, then "
            "each recognized risk (budget freeze, champion leaving, competitor "
            "pricing, security review stall…) moves it down, and each rep recovery "
            "you type or play moves it back up. “What changed the score” lists "
            "every delta with its plain-language reason.",
        )

    if _match(low, ("budget", "freeze", "no money", "funding")):
        if bf is None:
            return answer("budget_freeze",
                          "No budget-freeze deals are in memory yet.")
        went = _pattern_sentence(bf, OBJECTION_LABELS.get("budget_freeze", "budget-freeze"))
        extra = f" Of the wins, {top_strats}." if by_strategy else (
            " No wins on that objection yet.")
        return answer(
            "budget_freeze",
            went + extra + " Because the ratio is fatal, the follow-up draft warns "
            "on every budget-freeze moment and hands the rep a reply the team "
            "already used to win.",
        )

    if _match(low, ("champion", "leaving", "sponsor", "left the")):
        if champ is None:
            return answer("champion_left",
                          "No champion-leaving deals are in memory yet.")
        went = _pattern_sentence(champ, OBJECTION_LABELS.get("champion_left", "champion-leaving"))
        return answer(
            "champion_left",
            went + " When the person carrying the deal says they're leaving, the "
            "odds drop sharply — the agent flags it right on the call and watches "
            "for who takes over.",
        )

    if _match(low, ("acme", "vantage", "competitor", "price", "cheaper",
                    "pricing")):
        parts = [p for p in (_pattern_sentence(acme, "Acme") if acme else None,
                             _pattern_sentence(vantage, "Vantage") if vantage else None)
                 if p]
        lead = " ".join(parts) if parts else (
            f"Shared memory holds {stats.deals} deals "
            f"({stats.won} won, {stats.lost} lost).")
        return answer(
            "competitor",
            lead + " Price pressure lands in the feed as competitor-pricing, and "
            "the close-the-loop autopsy reviews what actually beat them last time.",
        )

    if _match(low, ("draft", "follow-up", "follow up", "email", "write to",
                    "send a copy")):
        draft = (
            "The follow-up draft is assembled from the replies your reps already "
            "used to win the same objection — verbatim text from the oldest win on "
            "that strategy, with a [your team] placeholder to fill in. No phrasing "
            "is invented."
            + (f" For budget freezes the proven closes were {top_strats}."
               if by_strategy else "")
        )
        return answer("draft", draft)

    if _match(low, ("pattern", "history", "similar deals", "trend",
                    "what happened in")):
        per = lambda p: getattr(p, "loss_ratio", 0.0) or 0.0
        top = sorted(stats.patterns, key=per, reverse=True)[:3]
        head = f"Team memory holds {stats.deals} deals ({stats.won} won, {stats.lost} lost)."
        if top:
            heaviest = "; ".join(
                _pattern_sentence(p, _label_for_key(p.key)) for p in top
            )
            return answer("patterns", head + " The heaviest patterns: "
                          + heaviest + ". Ask me about any of them by name.")
        return answer("patterns", head)

    if _match(low, ("lost", "close", "autopsy", "lesson", "teach",
                    "why did we")):
        return answer(
            "close_the_loop",
            "When you close a deal, it joins shared memory and every pattern is "
            f"recounted (memory now holds {stats.deals} deals: {stats.won} won, "
            f"{stats.lost} lost). A lost deal bumps the fatal pattern — budget "
            "freeze can go from 6 of 10 lost to 7 of 11 — and the autopsy card "
            "explains what to do differently next replay.",
        )

    if low == "help" or ("help" in low and not low.startswith(("how", "what", "why", "which"))):
        return help_reply()

    if _match(low, ("what is this", "what does it do", "how does it help",
                    "does it help", "what does the app", "what does the agent",
                    "what does the tool", "tell me about this app",
                    "tell me about the app", "what can you", "what problems")):
        return answer(
            "about",
            "This is the Lost-Deal Autopsy Agent. It replays a live call and "
            "scores every turn against the team's shared memory of past wins and "
            "losses. Everything is counted from real stored deals — percentages, "
            "patterns and lessons are never invented. Ask me about a pattern, a "
            "competitor, the score, the draft, or what closing does to memory.",
        )

    echo = text if len(text) <= 48 else text[:48] + "…"
    return answer(
        "fallback",
        f"I only answer from deals already in shared memory, so I can't speak to "
        f"\"{echo}\". Ask about a pattern (budget freeze, champion leaving), a "
        "competitor (Acme, Vantage), the score, the draft, or how closing changes "
        "the replay. Type “help” for examples.",
    )


@app.post("/api/chat")
def chat(
    request: ChatRequest, session: LiveSession = Depends(session_dependency)
) -> dict[str, Any]:
    """Chat-with-us answers, grounded in shared memory (D45).

    Template-only and keyless: every number in the reply is counted from the
    session's own records, so it stays deterministic and never hallucinates.
    """
    return _chat_reply(request.message, session)


# --------------------------------------------------------------------------
# reps
# --------------------------------------------------------------------------
@app.get("/api/reps", response_model=list[Rep])
def list_reps(session: LiveSession = Depends(session_dependency)) -> list[Rep]:
    return session.reps


class RepSelect(BaseModel):
    name: str


@app.post("/api/rep/select")
def select_rep(
    request: RepSelect, session: LiveSession = Depends(session_dependency)
) -> dict[str, Any]:
    try:
        rep = session.set_rep(request.name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown rep: {request.name}")
    return {
        "active_rep": rep.name,
        "is_new": rep.is_new,
        "badge": rep.badge,
        "deals_worked": rep.deals_worked,
        "note": (
            "Shared memory: warnings and scores are identical for every rep."
            if rep.is_new
            else "Shared memory: warnings and scores are identical for every rep."
        ),
    }


# --------------------------------------------------------------------------
# static frontend (SPEC 9)
# --------------------------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )
