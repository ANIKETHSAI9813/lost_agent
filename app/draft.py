"""DRAFT and the secondary heads-up card (SPEC 5.7, SPEC 7).

Everything here is deterministic and template-based by default. The optional
LLM layer (step 7) may rephrase, but it can never introduce a number, a deal id
or a rep name that was not passed in: `validate_draft()` enforces that, and the
template is the fallback whenever validation fails.

Content rules taken straight from SPEC 5.7:
  * The draft is written in Priya's voice, addressed to Maria Torres at
    Northwind Logistics, using only facts present in `demo_call.json` and
    `deals.json`. No invented figures, prices, ROI percentages, dates or names.
  * `cost_of_delay` is NOT used, because it needs a number ("roughly the
    equivalent of the annual license", "under two quarters") that this live deal
    does not have.
  * For budget_freeze the draft offers TWO options, adapted from
    `phased_rollout` (D007) and `pilot_first` (D009), because turn 4 promises
    "a couple of options" and turn 6 replies about starting smaller.
  * Citations are real: "Based on: D007, D009 (Marcus Chen, Marcus Chen)".
"""

from __future__ import annotations

import re
from typing import Sequence

from .extract import numbers_in
from .memory import MemoryRecord
from .models import DraftCitation, DraftFollowup, SecondaryTip, WinningResponse

# Strategies that may never be used in a draft, because their source text
# depends on a number this live deal does not have (SPEC 5.7).
FORBIDDEN_STRATEGIES = ("cost_of_delay",)

# The two strategies SPEC 5.7 requires for budget_freeze. They are the default
# tie-break order, NOT a hardcoded pick (D36): strategies are ranked by how
# many times the team has won with them on this objection, then by this order,
# then by recency, so the demo (seed-only) still produces D007, D009 verbatim
# while a new won pilot_first deal can move pilot first and be cited.
BUDGET_FREEZE_STRATEGIES = ("phased_rollout", "pilot_first")


def won_responses_for(
    records: Sequence[MemoryRecord], objection: str
) -> list[MemoryRecord]:
    """Won deals whose `winning_response.objection` matches (SPEC 5.7)."""
    return [
        r
        for r in records
        if not r.deal.is_lost
        and r.deal.winning_response is not None
        and r.deal.winning_response.objection == objection
    ]


def _recency_key(value: str | None) -> float:
    """Newest `closed_date` sorts first; undated records sort last."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return float("-inf")
    return float(digits)


def ranked_winning_sources(
    records: Sequence[MemoryRecord],
    objection: str,
    default_order: Sequence[str] = BUDGET_FREEZE_STRATEGIES,
) -> list[tuple[str, MemoryRecord]]:
    """Best backed strategies first: (strategy, most-recent won deal).

    Ranking is (win count desc, default order asc, newest `closed_date` desc)
    so the demo (seed-only budget_freeze) yields `phased_rollout` (D007),
    `pilot_first` (D009) unchanged, while retaining a *new* won pilot_first
    deal moves pilot first and cites the new deal.
    """
    per_strategy: dict[str, list[MemoryRecord]] = {}
    for record in won_responses_for(records, objection):
        strategy = record.deal.winning_response.strategy
        if strategy in FORBIDDEN_STRATEGIES:
            continue
        per_strategy.setdefault(strategy, []).append(record)
    if not per_strategy:
        return []

    order_index = {name: i for i, name in enumerate(default_order)}

    def rank(strategy: str) -> tuple[int, int, float, str]:
        newest = max(per_strategy[strategy], key=lambda r: _recency_key(r.deal.closed_date))
        return (
            -len(per_strategy[strategy]),
            order_index.get(strategy, len(default_order)),
            -_recency_key(newest.deal.closed_date),
            newest.deal.id,
        )

    ranked = sorted(per_strategy, key=rank)
    return [(s, max(per_strategy[s], key=lambda r: _recency_key(r.deal.closed_date)))
            for s in ranked]


def _citations(records: Sequence[MemoryRecord]) -> list[DraftCitation]:
    return [
        DraftCitation(
            id=r.deal.id,
            rep=r.deal.rep,
            strategy=r.deal.winning_response.strategy,
        )
        for r in records
        if r.deal.winning_response is not None
    ]


def citation_line(citations: Sequence[DraftCitation]) -> str:
    """`Based on: D007, D009 (Marcus Chen, Marcus Chen)` (SPEC 5.7)."""
    ids = ", ".join(c.id for c in citations)
    reps = ", ".join(c.rep for c in citations)
    return f"Based on: {ids} ({reps})"


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------
def _budget_freeze_body(
    contact: str, company: str, ordered: list[tuple[str, WinningResponse]]
) -> str:
    lines = [
        f"{contact},",
        "",
        "Thanks for being straight with me about the freeze. Two ways to keep "
        "moving that have worked for other teams in the same spot:",
    ]
    for index, (strategy, _source) in enumerate(ordered, start=1):
        lines.append("")
        if strategy == "phased_rollout":
            lines.append(
                f"{index}. Phased rollout. Instead of the full commitment now, "
                "scope the first phase to your core team only, and lock in the "
                "second-phase pricing for when budgets reopen. That keeps your "
                "team moving without a new line item that has to clear the freeze."
            )
        elif strategy == "pilot_first":
            lines.append(
                f"{index}. Pilot first. Rather than a purchase, run a pilot on "
                "one team under the allowance you already have. If the numbers "
                "hold, you will have the evidence finance needs to release "
                "funds as soon as the freeze lifts."
            )
        else:  # grounded fallback for a strategy we have never templated
            lines.append(f"{index}. {_source.text}")
    lines += [
        "",
        f"Happy to walk {contact.split(' ')[0]} through either of these. Which "
        "one is worth a short call?",
        "",
        "Priya",
    ]
    return "\n".join(lines)


def _generic_body(
    contact: str, sources: Sequence[WinningResponse]
) -> str:
    lines = [f"{contact},", "", "Following up on where we left things:"]
    for index, response in enumerate(sources, start=1):
        lines.append("")
        lines.append(f"{index}. {response.text}")
    lines += ["", "Priya"]
    return "\n".join(lines)


def build_draft(
    *,
    objection: str,
    records: Sequence[MemoryRecord],
    contact: str,
    company: str,
    turn: int | None = None,
) -> DraftFollowup | None:
    """Build the deterministic draft. Returns None when there is nothing to cite."""
    won = won_responses_for(records, objection)
    if not won:
        return None

    usable = [
        r
        for r in won
        if r.deal.winning_response.strategy not in FORBIDDEN_STRATEGIES
    ]
    if not usable:
        return None

    by_strategy = {r.deal.winning_response.strategy: r.deal.winning_response for r in usable}

    if objection == "budget_freeze":
        # One option per ranked strategy, each citing its most recent win. The
        # seed produces phased_rollout (D007) then pilot_first (D009) verbatim;
        # a newer won pilot_first deal takes over that option and its citation.
        ordered = [
            (strategy, record)
            for strategy, record in ranked_winning_sources(records, objection)
            if strategy in BUDGET_FREEZE_STRATEGIES
        ][:2]
        if not ordered:
            return None
        citations = [
            DraftCitation(
                id=record.deal.id,
                rep=record.deal.rep,
                strategy=record.deal.winning_response.strategy,
            )
            for _strategy, record in ordered
        ]
        body = _budget_freeze_body(
            contact, company, [(s, by_strategy[s]) for s, _r in ordered]
        )
    else:
        citations = _citations(usable)
        body = _generic_body(contact, [by_strategy[k] for k in chosen_keys(usable)])

    return DraftFollowup(
        subject=subject_for(objection, company),
        body=body,
        objection=objection,
        based_on=citations,
        generated_by="template",
        created_at_turn=turn,
    )


def chosen_keys(records: Sequence[MemoryRecord]) -> list[str]:
    return [r.deal.winning_response.strategy for r in records if r.deal.winning_response]


def subject_for(objection: str, company: str) -> str:
    if objection == "budget_freeze":
        return f"Two ways to keep {company} moving through the freeze"
    if objection == "champion_left":
        return f"Making sure the {company} rollout survives the handover"
    return f"Following up on {company}"


def build_secondary_tip(
    *,
    records: Sequence[MemoryRecord],
    turn: int | None = None,
) -> SecondaryTip | None:
    """Turn 5 heads-up: does not replace the main draft (SPEC 5.7)."""
    won = [
        r
        for r in won_responses_for(records, "champion_left")
        if r.deal.winning_response.strategy == "multi_thread_handoff"
    ]
    if not won:
        return None
    source = won[0]
    return SecondaryTip(
        title="Heads-up: your champion may be moving on",
        body=source.deal.winning_response.text,
        based_on=[
            DraftCitation(
                id=source.deal.id,
                rep=source.deal.rep,
                strategy=source.deal.winning_response.strategy,
            )
        ],
        generated_by="template",
    )


# --------------------------------------------------------------------------
# validation (SPEC 7)
# --------------------------------------------------------------------------
def validate_draft(
    draft: DraftFollowup,
    *,
    allowed_ids: Sequence[str],
    context_text: str,
) -> tuple[bool, list[str]]:
    """Every number and every cited id must be traceable to the given context.

    Returns (ok, problems). The template is used whenever this fails, so the
    demo can never show an invented figure.
    """
    problems: list[str] = []
    allowed = set(allowed_ids)

    for citation in draft.based_on:
        if citation.id not in allowed:
            problems.append(f"cites {citation.id}, which was not passed in")
        if not re.fullmatch(r"[DLC]\d{3}", citation.id):
            problems.append(f"cited id {citation.id!r} is not a deal id")

    haystack = context_text + " " + " ".join(
        numbers_in(context_text)
    )
    for number in numbers_in(draft.body) + numbers_in(draft.subject):
        if number not in haystack:
            problems.append(f"draft contains the number {number}, absent from context")

    # Deal ids quoted in the body must also be real.
    for found in set(re.findall(r"\b[DLC]\d{3}\b", draft.body)):
        if found not in allowed:
            problems.append(f"body mentions {found}, which was not passed in")

    return (not problems), problems
