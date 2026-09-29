"""The one pipeline that powers everything (SPEC 5).

    RETAIN -> EXTRACT -> RECALL -> REFLECT -> SCORE -> EXPLAIN -> DRAFT

`POST /api/events`, `POST /api/demo/step` and the pytest demo run all call
`LiveSession.process_event`, so there is exactly one implementation of the
scoring rules (SPEC 5 opening line).

Sequencing decisions, all logged in DECISIONS.md:
  * D4  the demo marks the follow-up sent as it advances into turn 6, which is
    what lets turn 6's positive reply fire the recovery signal.
  * D5  signals are applied one at a time in arrival order and never merged
    retroactively; the reason line reports the last signal that moved the score.
  * D6  a competitor name in the text is itself the extraction cue.
  * D7  objections are applied before competitors, so a turn that mentions both
    is explained by the objection first.
  * D8  the reported confidence is the engine's minimum across applied signals.
  * D3  one resolved pattern feeds both the score and the fatality check.
"""

from __future__ import annotations

import functools
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from .autopsy import (
    lesson_line,
    next_live_id,
    pick_objection,
    resolve_pattern,
    winning_response_from_memory,
)
from .config import settings
from .draft import build_draft, build_secondary_tip, citation_line, won_responses_for
from .extract import extract_turn, is_prospect_turn
from . import llm as llm_layer
from .memory import (
    SOURCE_LIVE,
    MemoryRecord,
    MemoryStore,
    ingest_seed,
    verify_counts,
)
from .models import (
    Deal,
    DraftFollowup,
    EventInput,
    EventResult,
    ExtractedSignals,
    MemoryStats,
    Pattern,
    RecalledDeal,
    Rep,
    ScoreHistoryEntry,
    SecondaryTip,
    WinningResponse,
)
from .scoring import ScoringEngine, describe_pattern, is_fatal
from .seed_loader import COMBINED_KEY, RECOVERY_SIGNAL, SeedData, load_seed

# SPEC 5.2: the demo's contact. Taken from demo_call.json, never invented.
CONTACT_NAME = "Maria Torres"


@dataclass
class LiveSession:
    """Live-deal state: score, history, applied signals, draft, rep, turn."""

    store: MemoryStore
    seed: SeedData = field(default_factory=load_seed)
    records: list[MemoryRecord] = field(default_factory=list)
    engine: ScoringEngine | None = None
    draft: DraftFollowup | None = None
    secondary_tip: SecondaryTip | None = None
    active_rep: str = "Priya Nair"
    turn_index: int = 0
    processed_events: set[str] = field(default_factory=set)
    history: list[ScoreHistoryEntry] = field(default_factory=list)
    closed: bool = False
    # RLock (not Lock): D35's close path re-enters through refresh_records and
    # the demo's advance re-enters through process_event. Phase 6 wraps every
    # mutating entry point in `_locked`, so the reentrancy must be safe.
    lock: threading.RLock = field(default_factory=threading.RLock)

    # -- lifecycle -------------------------------------------------------
    def __post_init__(self) -> None:
        if not self.records:
            self.records = self.store.all_records()
        if self.engine is None:
            self.engine = ScoringEngine(
                config=self.seed.scoring, records=self.records
            )
        if not self.active_rep:
            self.active_rep = self.seed.demo.deal.rep

    def startup_retain(self) -> int:
        """SPEC 5.1: on startup ingest all 30 deals, then refresh the records."""
        count = ingest_seed(self.store, self.seed.deals)
        self.refresh_records()
        return count

    def refresh_records(self) -> None:
        """Re-read memory after a retain so the next run sees it (SPEC 5.1)."""
        self.records = self.store.all_records()
        if self.engine is not None:
            self.engine.replace_records(self.records)

    def reset(self) -> None:
        """`POST /api/demo/reset`: reset live state but keep memory (SPEC 8)."""
        self.engine.reset()
        self.draft = None
        self.secondary_tip = None
        self.turn_index = 0
        self.processed_events = set()
        self.active_rep = self.seed.demo.deal.rep
        self.closed = False

    def set_rep(self, name: str) -> Rep:
        rep = next((r for r in self.seed.reps if r.name == name), None)
        if rep is None:
            raise KeyError(name)
        self.active_rep = rep.name
        return rep

    @property
    def reps(self) -> list[Rep]:
        return list(self.seed.reps)

    @property
    def live_deal(self):
        return self.seed.demo.deal

    @property
    def stage(self) -> str:
        return self.seed.demo.deal.stage

    # -- the pipeline -----------------------------------------------------
    def plan_signals(
        self, signals: ExtractedSignals
    ) -> list[tuple[str, str, str]]:
        """Order the signals for this event.

        Returns [(signal_key, direction, kind)] where kind is
        "objection" | "competitor" | "combined" | "recovery".

        SPEC 4.2: Acme together with no_exec_sponsor is one combined signal
        instead of two separate ones. D7: objections before competitors.
        """
        plan: list[tuple[str, str, str]] = []

        objections = [o for o in signals.objections]
        competitors = list(signals.competitors)

        if "Acme" in competitors and "no_exec_sponsor" in objections:
            plan.append((COMBINED_KEY, "risk", "combined"))
            objections = [o for o in objections if o != "no_exec_sponsor"]
            competitors = [c for c in competitors if c != "Acme"]

        for tag in objections:  # D7: objections first
            plan.append((f"objection:{tag}", "risk", "objection"))
        for name in competitors:
            plan.append((f"competitor:{name}", "risk", "competitor"))

        if signals.recovery and RECOVERY_SIGNAL in signals.recovery:
            tag = self.draft.objection if self.draft is not None else ""
            if tag:
                plan.append(
                    (f"recovery:{RECOVERY_SIGNAL}@{tag}", "recovery", "recovery")
                )
        return plan

    def process_event(self, event: EventInput) -> EventResult:
        """Run the full pipeline for one event."""
        engine = self.engine
        assert engine is not None
        event_id = event.event_id or f"evt-{uuid.uuid4().hex[:8]}"

        # Idempotent replay (SPEC 4 line 51).
        if event_id in self.processed_events:
            return EventResult(
                score=engine.score,
                delta=0.0,
                reason=f"Replay of {event_id} ignored; score unchanged.",
                confidence=engine.confidence,
                draft_followup=self.draft,
                secondary_tip=self.secondary_tip,
                signals=ExtractedSignals(source="none"),
                ignored=[f"event {event_id} already processed"],
            )

        score_before = engine.score
        ignored: list[str] = []

        # 2. EXTRACT
        signals, extract_notes = extract_turn(
            text=event.text,
            speaker=event.speaker,
            event_type=event.type,
            active_rep=self.active_rep,
            taxonomy=self.seed.scoring.objection_taxonomy,
            draft_exists=self.draft is not None,
            draft_marked_sent=bool(self.draft and self.draft.marked_sent),
        )
        ignored.extend(extract_notes)

        # SPEC 7: the LLM is optional and only suggests extraction candidates.
        # Any failure degrades back to the keyword result above, visibly.
        if settings.llm_enabled and is_prospect_turn(event.speaker, self.active_rep):
            candidates, note = llm_layer.candidate_signals(
                text=event.text,
                taxonomy=self.seed.scoring.objection_taxonomy,
            )
            if note:
                ignored.append(note)
            if candidates is not None:
                signals, llm_notes = extract_turn(
                    text=event.text,
                    speaker=event.speaker,
                    event_type=event.type,
                    active_rep=self.active_rep,
                    taxonomy=self.seed.scoring.objection_taxonomy,
                    draft_exists=self.draft is not None,
                    draft_marked_sent=bool(self.draft and self.draft.marked_sent),
                    candidates=candidates,
                    source="llm",
                )
                ignored.extend(llm_notes)

        # 3. RECALL + 4. REFLECT + 5. SCORE
        patterns: list[Pattern] = []
        for key, direction, kind in self.plan_signals(signals):
            pattern = engine.pattern_for(key, self.stage)
            if pattern is None:
                ignored.append(f"no team history for {key}")
                continue
            patterns.append(pattern)
            update = engine.apply(
                key,
                pattern,
                direction=direction,
                event_id=event_id,
                timestamp=event.timestamp,
            )
            if not update.applied:
                ignored.append(update.ignored_reason)
                continue

            # 7. DRAFT, only for a fatal objection signal (SPEC 5.7)
            if kind in ("objection", "combined") and is_fatal(
                pattern, self.seed.scoring
            ):
                if kind == "objection" and self.draft is None:
                    draft = build_draft(
                        objection=pattern.key.split(":", 1)[1].split("@", 1)[0],
                        records=self.records,
                        contact=CONTACT_NAME,
                        company=self.live_deal.company,
                        turn=self.turn_index,
                    )
                    if draft is not None and settings.llm_enabled:
                        rephrased = llm_layer.rephrase_draft(
                            draft=draft,
                            allowed_ids=[c.id for c in draft.based_on],
                            context_text=self._draft_context(draft),
                        )
                        if rephrased is not None:
                            draft = rephrased
                        else:
                            ignored.append(
                                "LLM draft failed validation; template used"
                            )
                    if draft is not None:
                        self.draft = draft
            if (
                kind == "objection"
                and pattern.key.startswith("objection:champion_left")
                and is_fatal(pattern, self.seed.scoring)
                and self.secondary_tip is None
            ):
                tip = build_secondary_tip(records=self.records)
                if tip is not None:
                    self.secondary_tip = tip

        # 6. EXPLAIN
        reason = self._reason_line(event_id, score_before)
        if settings.llm_enabled:
            entries = [e for e in engine.history if e.event_id == event_id]
            if entries:
                phrased = llm_layer.phrase_reason(
                    template_reason=reason,
                    pattern_text=entries[-1].pattern_text,
                )
                if phrased is not None:
                    reason = phrased
                else:
                    ignored.append("LLM reason failed validation; template used")
        recalled = self._recalled_for(signals)

        self.processed_events.add(event_id)
        return EventResult(
            score=engine.score,
            delta=(engine.score - score_before) * 100.0,
            reason=reason,
            confidence=engine.confidence,
            patterns=patterns,
            recalled_deals=recalled,
            draft_followup=self.draft,
            secondary_tip=self.secondary_tip,
            signals=signals,
            ignored=ignored,
        )

    # -- helpers ----------------------------------------------------------
    def _reason_line(self, event_id: str, score_before: float) -> str:
        engine = self.engine
        assert engine is not None
        entries = [e for e in engine.history if e.event_id == event_id]
        if not entries:
            return "No new signals in this turn; score unchanged."
        last = entries[-1]
        pts = round(last.delta_pts)
        if pts > 0:
            return f"Recovered {pts} pts: {last.pattern_text}"
        if pts < 0:
            return f"Dropped {abs(pts)} pts: {last.pattern_text}"
        return f"Unchanged: {last.pattern_text}"

    def _recalled_for(self, signals: ExtractedSignals) -> list[RecalledDeal]:
        objection = signals.objections[0] if signals.objections else None
        if objection is None and self.draft is not None:
            objection = self.draft.objection
        return self.store.recall_similar(
            objection=objection,
            competitors=list(signals.competitors),
            stage=self.stage,
            deal_size=self.live_deal.deal_size,
            active_rep=self.active_rep,
            limit=5,
        )

    def _draft_context(self, draft: DraftFollowup) -> str:
        """The verified context an LLM rephrase is allowed to draw from.

        Every digit in an LLM draft must occur somewhere in this text
        (SPEC 7). It contains the winning_response of each cited deal and the
        deal brief, so a faithful rephrase passes and an invented figure fails.
        """
        cited = {c.id for c in draft.based_on}
        parts: list[str] = []
        for record in self.records:
            deal = record.deal
            if deal.id not in cited:
                continue
            parts.append(f"{deal.id} ({deal.rep})")
            if deal.winning_response is not None:
                parts.append(deal.winning_response.text)
        return " ".join(parts)

    # -- demo runner ------------------------------------------------------
    def next_turn(self) -> EventInput | None:
        turns = self.seed.demo.turns
        if self.turn_index >= len(turns):
            return None
        turn = turns[self.turn_index]
        return EventInput(
            event_id=f"turn-{turn.id}",
            type=turn.type,
            speaker=turn.speaker,
            text=turn.text,
            timestamp=turn.timestamp,
        )

    def advance(self) -> EventResult | None:
        """`POST /api/demo/step`: advance one scripted turn."""
        event = self.next_turn()
        if event is None:
            return None
        # D4: the draft is marked sent as the demo enters turn 6, so turn 6's
        # positive reply can fire the recovery signal (SPEC 5.2c).
        if event.event_id == "turn-6" and self.draft is not None:
            self.draft.marked_sent = True
        result = self.process_event(event)
        self.turn_index += 1
        return result

    def mark_followup_sent(self) -> DraftFollowup | None:
        """`POST /api/followup/sent`."""
        if self.draft is not None:
            self.draft.marked_sent = True
        return self.draft

    # -- close the loop (D35) ---------------------------------------------
    def close_deal(
        self,
        *,
        outcome: str,
        winning_strategy: str | None = None,
        note: str = "",
        stage: str | None = None,
        rep: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/deals/close`: retain the live deal and autopsy the run.

        Never invents data: company/industry/size/stakeholders come from the
        live deal, objection from the applied signals, the winning text
        verbatim from the oldest seed win on that strategy, counts from the
        real pattern resolver before AND after the retain.
        """
        engine = self.engine
        assert engine is not None
        if self.closed:
            raise ValueError("deal already closed for this run")
        if not engine.history:
            raise ValueError("no signals were applied this run; nothing to close")

        objection = pick_objection(self)
        stage = stage or self.stage
        if stage not in self.seed.scoring.stages:
            raise ValueError(f"unknown stage: {stage!r}")
        rep = rep or self.active_rep

        before = resolve_pattern(self.records, self.seed.scoring, objection, stage)

        winning_response = None
        if outcome == "won":
            if not winning_strategy:
                raise ValueError("a winning strategy is required when outcome=won")
            try:
                response, source = winning_response_from_memory(
                    self.records, objection, winning_strategy
                )
            except LookupError as exc:
                raise ValueError(str(exc)) from exc
            winning_response = WinningResponse(
                objection=objection,
                strategy=winning_strategy,
                text=response.text,
            )
            _win_source = source
        elif winning_strategy:
            raise ValueError("winning_strategy is only allowed when outcome=won")

        competitors = self._detected_competitors()
        recovery = self._recovery_flags()

        new_id = next_live_id(self.records)
        deal = Deal(
            id=new_id,
            company=self.live_deal.company,
            industry=self.live_deal.industry,
            deal_size=self.live_deal.deal_size,
            outcome=outcome,
            objection_stage=stage,
            stage_lost_at=stage if outcome == "lost" else None,
            closed_date=date.today().isoformat(),
            rep=rep,
            competitors=competitors,
            objections=[objection],
            stakeholders_in_room=list(self.live_deal.stakeholders_in_room),
            summary=(
                f"{self.live_deal.company} closed {outcome} at the {stage} stage "
                f"on the {objection} objection."
                + (f" Note: {note}" if note else "")
            ),
            winning_response=winning_response,
            recovery_signals=recovery,
        )

        self.store.retain(deal, source=SOURCE_LIVE)
        self.refresh_records()

        after = resolve_pattern(self.records, self.seed.scoring, objection, stage)
        followup_sent = bool(self.draft and self.draft.marked_sent)
        what_worked = [
            {
                "id": r.deal.id,
                "rep": r.deal.rep,
                "strategy": r.deal.winning_response.strategy,
                "text": r.deal.winning_response.text,
            }
            for r in won_responses_for(self.records, objection)
        ]
        lesson = lesson_line(
            outcome=outcome,
            objection=objection,
            stage=stage,
            before=before,
            after=after,
            strategy=winning_strategy,
            strategy_deals=(
                [
                    r.deal.id
                    for r in won_responses_for(self.records, objection)
                    if r.deal.winning_response.strategy == winning_strategy
                ]
                if outcome == "won" and winning_strategy
                else []
            ),
        )

        self.closed = True
        return {
            "retained_id": new_id,
            "outcome": outcome,
            "objection": objection,
            "stage": stage,
            "rep": rep,
            "score_at_close": round(float(engine.score), 4),
            "deals": len(self.records),
            "lost": sum(1 for r in self.records if r.deal.is_lost),
            "won": sum(1 for r in self.records if not r.deal.is_lost),
            "pattern_before": before,
            "pattern_after": after,
            "winning_response": (
                winning_response.model_dump() if winning_response else None
            ),
            "autopsy": {
                "timeline": [entry.model_dump() for entry in engine.history],
                "outcome": outcome,
                "followup_sent": followup_sent,
                "recovery_signals": recovery,
                "competitors": competitors,
                "what_worked_before": what_worked,
                "lesson": lesson,
            },
        }

    def _detected_competitors(self) -> list[str]:
        engine = self.engine
        assert engine is not None
        return sorted(
            {
                key.split(":", 1)[1]
                for key in engine.applied_signals
                if key.startswith("competitor:")
            }
        )

    def _recovery_flags(self) -> list[str]:
        engine = self.engine
        assert engine is not None
        if any(key.startswith("recovery:") for key in engine.applied_signals):
            return [RECOVERY_SIGNAL]
        return []

    def reset_live(self) -> dict[str, Any]:
        """`POST /api/memory/reset_live`: removable memory gives a clean demo.

        Live-added deals are dropped from memory (per-backend), records are
        refreshed and the run is reset, so an autoplayed/replayed flow always
        starts from exactly the seed.
        """
        retired = self.store.forget_live_added()
        self.refresh_records()
        self.reset()
        return {
            "retired": retired,
            "deals": len(self.records),
            "lost": sum(1 for r in self.records if r.deal.is_lost),
            "won": sum(1 for r in self.records if not r.deal.is_lost),
        }

    # -- memory stats -----------------------------------------------------
    def memory_stats(self, include_reflection: bool = False) -> MemoryStats:
        report = verify_counts(self.records, self.seed.scoring, self.seed.expected)
        reflection = None
        if include_reflection and not self.store.fallback:
            reflection = self.store.reflect_narrative(
                "Which past objections did this team lose most often?"
            )
        return MemoryStats(
            backend=self.store.name,
            fallback=self.store.fallback,
            integrity_status=report["integrity_status"],
            bank_id=self.store.bank_id,
            deals=report["deals"],
            lost=report["lost"],
            won=report["won"],
            patterns=report["patterns"],
            fallback_reason=self.store.fallback_reason,
            hindsight_reflection=reflection,
            detail={
                "lost_deals_in_shared_memory": report["lost"],
                "mismatches": report["mismatches"],
                "citation": citation_line(
                    build_draft(
                        objection="budget_freeze",
                        records=self.records,
                        contact=CONTACT_NAME,
                        company=self.live_deal.company,
                    ).based_on
                )
                if build_draft(
                    objection="budget_freeze",
                    records=self.records,
                    contact=CONTACT_NAME,
                    company=self.live_deal.company,
                )
                is not None
                else None,
            },
        )


# --------------------------------------------------------------------------
# Phase 6: one lock per session, every mutation serialized
# --------------------------------------------------------------------------
def _locked(method):
    """Wraps a `LiveSession` method so it holds the session lock.

    A browser can fan out requests (drag the same demo window open twice, the
    replay button racing a close), and each session lives on a threadpool
    thread. Without this, `advance` and `close_deal` could interleave inside
    one engine. RLock makes the nesting safe: `advance` -> `process_event`,
    `close_deal` -> `refresh_records`.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


for _mutator in (
    "startup_retain",
    "refresh_records",
    "reset",
    "set_rep",
    "process_event",
    "advance",
    "mark_followup_sent",
    "close_deal",
    "reset_live",
):
    setattr(LiveSession, _mutator, _locked(getattr(LiveSession, _mutator)))
