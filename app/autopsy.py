"""Close-the-loop helpers (final-sprint D35).

Builds the deal post-mortem `POST /api/deals/close` returns: the memory diff
before/after the close, the deterministic lesson line and the verbatim
winning-response sourcing. Nothing here invents a number, deal id, strategy or
rep name; every field is drawn from memory, the live deal or the scoring
engine's own history.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .draft import won_responses_for
from .memory import SOURCE_SEED, MemoryRecord
from .models import Deal, WinningResponse
from .scoring import OBJECTION_LABELS
from .seed_loader import resolve_objection_pattern


def next_live_id(records: Sequence[MemoryRecord]) -> str:
    """Next free `C###` id (live-added deals use the C prefix)."""
    used = {r.id for r in records}
    number = 1
    while f"C{number:03d}" in used:
        number += 1
    return f"C{number:03d}"


def pick_objection(session) -> str:
    """The objection this run is really about.

    The drafted objection wins (it was fatal enough to generate the draft);
    otherwise the applied objection signal whose score move was largest.
    """
    if session.draft is not None and session.draft.objection:
        return session.draft.objection
    engine = session.engine
    assert engine is not None
    candidates = [h for h in engine.history if h.signal_key.startswith("objection:")]
    if not candidates:
        raise LookupError("no objection signal was ever applied this run")
    strongest = max(candidates, key=lambda h: abs(h.delta_pts))
    return strongest.signal_key.split(":", 1)[1].split("@", 1)[0]


def winning_response_from_memory(
    records: Sequence[MemoryRecord], objection: str, strategy: str
) -> tuple[WinningResponse, MemoryRecord]:
    """Verbatim winning response for a won close.

    Prefers seed deals (the audited source) and, within that, the earliest
    `closed_date`, so a C###-id live win can never invent its own text.
    """
    matches = [
        r
        for r in won_responses_for(records, objection)
        if r.deal.winning_response.strategy == strategy
    ]
    if not matches:
        raise LookupError(f"no won deal with strategy '{strategy}' on '{objection}'")
    seed = [r for r in matches if r.source == SOURCE_SEED]
    pool = seed or matches
    best = min(pool, key=lambda r: _closed_key(r.deal.closed_date))
    return best.deal.winning_response, best


def pattern_payload(pattern) -> dict[str, Any] | None:
    """Serialize a resolved pattern (either backend's Pattern shape)."""
    if pattern is None:
        return None
    return {
        "key": pattern.key,
        "total": pattern.total,
        "lost": pattern.lost,
        "won": pattern.won,
        "loss_ratio": round(float(pattern.loss_ratio), 4),
        "stage_scope": pattern.stage_scope,
    }


def resolve_pattern(
    records: Sequence[MemoryRecord], scoring, objection: str, stage: str
) -> dict[str, Any] | None:
    """The exact stage-scoped pattern a live run would see (SPEC 4.1/D3)."""
    pattern = resolve_objection_pattern(
        [r.deal for r in records], scoring, objection, stage
    )
    return pattern_payload(pattern)


def lesson_line(*, outcome: str, objection: str, stage: str, before: dict | None,
                after: dict | None, strategy: str | None = None,
                strategy_deals: Sequence[str] = ()) -> str:
    """A deterministic, verbatim-number verdict for the autopsy card."""
    label = OBJECTION_LABELS.get(objection, objection)
    if before is None:
        before = {}
    if after is None:
        after = {}
    b_note = f"{before.get('lost')} of {before.get('total')}" if before.get("total") else "n/a"
    a_note = f"{after.get('lost')} of {after.get('total')}" if after.get("total") else "n/a"
    if outcome == "lost":
        return (
            f"We lost this one. {label.title()} at {stage}: before, "
            f"{b_note} similar deals were lost; after this close, {a_note}."
        )
    cite = f" using '{strategy}'" if strategy else ""
    won_note = ""
    if strategy and strategy_deals:
        won_note = " The strategy has won before in this shared memory."
    return (
        f"We turned it around{cite}. {label.title()} at {stage}: before, "
        f"{b_note} similar deals were lost; after this close, {a_note}."
        + won_note
    )


def _closed_key(value: str) -> float:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return float("inf")
    return float(digits)  # ascending: earliest date first


def today_iso() -> str:
    return date.today().isoformat()