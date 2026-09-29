"""Pydantic models for seed data and pipeline payloads (SPEC 2, SPEC 8).

Field names mirror the seed JSON exactly so `model_validate` on raw seed data
is the only mapping step. seed/*.json is read-only (SPEC 0).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Outcome = Literal["lost", "won"]
EventType = Literal["call", "email", "crm"]


# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------
class WinningResponse(BaseModel):
    """Present on won deals only (SPEC 3: 8 won, 8 winning_response)."""

    objection: str
    strategy: str
    text: str


class Deal(BaseModel):
    id: str
    company: str
    industry: str
    deal_size: int
    outcome: Outcome
    objection_stage: str
    stage_lost_at: str | None = None
    closed_date: str
    rep: str
    competitors: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    stakeholders_in_room: list[str] = Field(default_factory=list)
    summary: str
    winning_response: WinningResponse | None = None
    recovery_signals: list[str] = Field(default_factory=list)

    @property
    def objection(self) -> str | None:
        """Every deal has exactly one objection (SPEC 3)."""
        return self.objections[0] if self.objections else None

    @property
    def is_lost(self) -> bool:
        return self.outcome == "lost"


class Rep(BaseModel):
    id: str
    name: str
    title: str
    tenure: str
    is_new: bool
    badge: str | None = None
    deals_worked: list[str] = Field(default_factory=list)


class Clamp(BaseModel):
    min: float
    max: float


class ScoringConfig(BaseModel):
    """Every constant comes from seed/scoring_config.json (SPEC 4)."""

    prior_win_probability: float
    logit_scale: float
    confidence_full_at_n: float
    min_deals_for_fatal_pattern: int
    fatal_loss_ratio: float
    signal_weights: dict[str, float]
    objection_taxonomy: list[str]
    stages: list[str]
    clamp: Clamp


class PatternExpectation(BaseModel):
    key: str
    total: int
    lost: int
    won: int


class ExpectedTotals(BaseModel):
    deals: int
    lost: int
    won: int


class ExpectedPatterns(BaseModel):
    totals: ExpectedTotals
    patterns: list[PatternExpectation]
    winning_responses_for_budget_freeze: list[str]


class ExpectedSignals(BaseModel):
    objections: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    recovery: list[str] = Field(default_factory=list)
    note: str = ""


class Turn(BaseModel):
    id: int
    type: EventType
    speaker: str
    timestamp: str
    text: str
    expected_signals: ExpectedSignals


class LiveDeal(BaseModel):
    id: str
    company: str
    industry: str
    deal_size: int
    stage: str
    rep: str
    stakeholders_in_room: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)


class DemoCall(BaseModel):
    deal: LiveDeal
    turns: list[Turn]


# --------------------------------------------------------------------------
# Pipeline payloads
# --------------------------------------------------------------------------
class ExtractedSignals(BaseModel):
    """Output of EXTRACT (SPEC 5.2). Only from PROSPECT turns."""

    objections: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    recovery: list[str] = Field(default_factory=list)
    source: Literal["llm", "keyword", "none"] = "none"


class Pattern(BaseModel):
    """Output of REFLECT (SPEC 5.4). Computed in code, never by an LLM."""

    key: str
    total: int
    lost: int
    won: int
    loss_ratio: float
    stage_scope: Literal["stage", "all_stages"] = "stage"
    display_text: str = ""


class ScoreHistoryEntry(BaseModel):
    """Append-only score history (SPEC 4)."""

    event_id: str
    score_before: float
    score_after: float
    delta_pts: float
    signal_key: str
    pattern_text: str
    confidence: float
    timestamp: str


class RecalledDeal(BaseModel):
    """Output of RECALL (SPEC 5.3), top 5."""

    id: str
    company: str
    outcome: Outcome
    rep: str
    worked_by_you: bool
    objection: str | None = None
    objection_stage: str | None = None
    deal_size: int | None = None
    closed_date: str | None = None


class DraftCitation(BaseModel):
    id: str
    rep: str
    strategy: str


class DraftFollowup(BaseModel):
    """Output of DRAFT (SPEC 5.7)."""

    subject: str
    body: str
    objection: str
    based_on: list[DraftCitation] = Field(default_factory=list)
    generated_by: Literal["llm", "template"] = "template"
    marked_sent: bool = False
    created_at_turn: int | None = None


class SecondaryTip(BaseModel):
    """Turn 5 heads-up card (SPEC 5.7): does not replace the main draft."""

    title: str
    body: str
    based_on: list[DraftCitation] = Field(default_factory=list)
    generated_by: Literal["llm", "template"] = "template"


class EventResult(BaseModel):
    """`POST /api/events` response shape (SPEC 8)."""

    score: float
    delta: float
    reason: str
    confidence: float
    patterns: list[Pattern] = Field(default_factory=list)
    recalled_deals: list[RecalledDeal] = Field(default_factory=list)
    draft_followup: DraftFollowup | None = None
    secondary_tip: SecondaryTip | None = None
    signals: ExtractedSignals = Field(default_factory=ExtractedSignals)
    ignored: list[str] = Field(default_factory=list)


class EventInput(BaseModel):
    """One live event, for `POST /api/events` and `POST /api/demo/step` (SPEC 8).

    Phase 6: text and speaker are length-checked so a megabyte paste or an
    empty body gets a clean 422 instead of an unbounded extraction call. The
    demo's own turns are well inside both limits.
    """

    event_id: str = ""
    type: EventType
    speaker: str
    text: str
    timestamp: str = ""

    @field_validator("text")
    @classmethod
    def _text_length(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be empty")
        if len(v) > 2000:
            raise ValueError("text is too long (max 2000 chars)")
        return v

    @field_validator("speaker")
    @classmethod
    def _speaker_length(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("speaker must not be empty")
        if len(v) > 80:
            raise ValueError("speaker is too long (max 80 chars)")
        return v


class MemoryStats(BaseModel):
    """`GET /api/memory/stats` (SPEC 8)."""

    backend: str
    fallback: bool
    integrity_status: Literal["COUNTS VERIFIED", "COUNT MISMATCH", "UNKNOWN"]
    bank_id: str
    deals: int
    lost: int
    won: int
    patterns: list[PatternExpectation]
    # SPEC 6: the fallback must be VISIBLE, so the reason is a top-level field
    # the UI can render next to the red badge rather than a nested detail.
    fallback_reason: str = ""
    hindsight_reflection: str | None = None
    detail: dict = Field(default_factory=dict)
