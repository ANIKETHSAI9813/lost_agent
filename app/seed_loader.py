"""Seed loader and validator (SPEC 2, SPEC 10.2).

`seed/*.json` is READ-ONLY (SPEC 0). Nothing here writes to it. If a seed file
looks wrong we log it in DECISIONS.md and keep using it as-is; we never "fix"
the data.

This module is also the reference implementation of pattern counting. Step 3
recomputes the same keys from records reconstructed out of the memory backend
and compares them with expected_patterns.json, so any drift between "what the
seed says" and "what memory actually holds" becomes visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import KNOWN_COMPETITORS, SEED_DIR
from .models import (
    Deal,
    DemoCall,
    ExpectedPatterns,
    Pattern,
    Rep,
    ScoringConfig,
)

RECOVERY_SIGNAL = "positive_reply_after_followup"
COMBINED_KEY = "competitor:Acme+objection:no_exec_sponsor"


class SeedError(ValueError):
    """Raised when a seed file cannot be parsed or fails validation."""


@dataclass(frozen=True)
class SeedData:
    deals: list[Deal]
    reps: list[Rep]
    scoring: ScoringConfig
    expected: ExpectedPatterns
    demo: DemoCall

    def deal_by_id(self, deal_id: str) -> Deal:
        for deal in self.deals:
            if deal.id == deal_id:
                return deal
        raise KeyError(f"unknown deal id: {deal_id}")

    def rep_by_name(self, name: str) -> Rep:
        for rep in self.reps:
            if rep.name == name:
                return rep
        raise KeyError(f"unknown rep: {name}")

    def rep_deal_ids(self, rep_name: str) -> set[str]:
        return {d.id for d in self.deals if d.rep == rep_name}

    def primary_rep_name(self) -> str:
        """Priya Nair is the demo's active rep: the new rep (SPEC 2)."""
        new_reps = [r for r in self.reps if r.is_new]
        if len(new_reps) == 1:
            return new_reps[0].name
        return self.demo.deal.rep


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def _read_json(path: Path):
    if not path.is_file():
        raise SeedError(f"missing seed file: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise SeedError(f"{path.name} is not valid JSON: {exc}") from exc


def load_deals() -> list[Deal]:
    return [Deal.model_validate(d) for d in _read_json(SEED_DIR / "deals.json")]


def load_reps() -> list[Rep]:
    return [Rep.model_validate(r) for r in _read_json(SEED_DIR / "reps.json")]


def load_scoring_config() -> ScoringConfig:
    return ScoringConfig.model_validate(_read_json(SEED_DIR / "scoring_config.json"))


def load_expected_patterns() -> ExpectedPatterns:
    return ExpectedPatterns.model_validate(
        _read_json(SEED_DIR / "expected_patterns.json")
    )


def load_demo_call() -> DemoCall:
    return DemoCall.model_validate(_read_json(SEED_DIR / "demo_call.json"))


@lru_cache(maxsize=1)
def load_seed() -> SeedData:
    data = SeedData(
        deals=load_deals(),
        reps=load_reps(),
        scoring=load_scoring_config(),
        expected=load_expected_patterns(),
        demo=load_demo_call(),
    )
    problems = validate_seed(data)
    if problems:
        raise SeedError("seed validation failed:\n  - " + "\n  - ".join(problems))
    return data


def clear_cache() -> None:
    """Tests that mutate loaded state need a fresh read."""
    load_seed.cache_clear()


# --------------------------------------------------------------------------
# pattern counting (reference implementation)
# --------------------------------------------------------------------------
def _counts(deals: list[Deal]) -> tuple[int, int]:
    total = len(deals)
    lost = sum(1 for d in deals if d.is_lost)
    return total, lost


def _pattern(key: str, deals: list[Deal], stage_scope: str = "stage") -> Pattern:
    total, lost = _counts(deals)
    won = total - lost
    return Pattern(
        key=key,
        total=total,
        lost=lost,
        won=won,
        loss_ratio=(lost / total) if total else 0.0,
        stage_scope="all_stages" if stage_scope == "all_stages" else "stage",
    )


def compute_pattern_counts(deals: list[Deal], scoring: ScoringConfig) -> list[Pattern]:
    """Compute every non-empty pattern key from deal records.

    Keys follow SPEC 3 / SPEC 4:
      objection:<tag>@<stage>
      competitor:<name>
      competitor:Acme+objection:no_exec_sponsor
      recovery:positive_reply_after_followup@<objection>
    """
    out: list[Pattern] = []

    for tag in scoring.objection_taxonomy:
        tagged = [d for d in deals if tag in d.objections]
        for stage in scoring.stages:
            scoped = [d for d in tagged if d.objection_stage == stage]
            if scoped:
                out.append(
                    _pattern(f"objection:{tag}@{stage}", scoped)
                )

    for name in KNOWN_COMPETITORS:
        comp = [d for d in deals if name in d.competitors]
        if comp:
            out.append(_pattern(f"competitor:{name}", comp))

    combined = [
        d
        for d in deals
        if "Acme" in d.competitors and "no_exec_sponsor" in d.objections
    ]
    if combined:
        out.append(_pattern(COMBINED_KEY, combined))

    recovered = [d for d in deals if RECOVERY_SIGNAL in d.recovery_signals]
    for tag in scoring.objection_taxonomy:
        scoped = [d for d in recovered if tag in d.objections]
        if scoped:
            out.append(
                _pattern(f"recovery:{RECOVERY_SIGNAL}@{tag}", scoped)
            )

    return out


def pattern_index(patterns: list[Pattern]) -> dict[str, Pattern]:
    return {p.key: p for p in patterns}


def all_stage_patterns(deals: list[Deal], scoring: ScoringConfig) -> list[Pattern]:
    """The same keys, ignoring stage. This is the SPEC 4.1 fallback source.

    Needed because the data is not stage-uniform: competitor_pricing is 1 deal
    at Evaluation and 1 at Proposal, so its 2/2/0 figure is all-stages only
    (see DECISIONS.md D16).
    """
    out: list[Pattern] = []

    for tag in scoring.objection_taxonomy:
        tagged = [d for d in deals if tag in d.objections]
        if tagged:
            out.append(_pattern(f"objection:{tag}", tagged, stage_scope="all_stages"))

    for name in KNOWN_COMPETITORS:
        comp = [d for d in deals if name in d.competitors]
        if comp:
            out.append(_pattern(f"competitor:{name}", comp, stage_scope="all_stages"))

    combined = [
        d for d in deals if "Acme" in d.competitors and "no_exec_sponsor" in d.objections
    ]
    if combined:
        out.append(_pattern(COMBINED_KEY, combined, stage_scope="all_stages"))

    recovered = [d for d in deals if RECOVERY_SIGNAL in d.recovery_signals]
    for tag in scoring.objection_taxonomy:
        scoped = [d for d in recovered if tag in d.objections]
        if scoped:
            out.append(
                _pattern(
                    f"recovery:{RECOVERY_SIGNAL}@{tag}", scoped, stage_scope="all_stages"
                )
            )

    return out


def resolve_objection_pattern(
    deals: list[Deal], scoring: ScoringConfig, tag: str, stage: str
) -> Pattern | None:
    """SPEC 4.1: prefer `objection:<tag>@<live stage>`; if it has fewer than
    `min_deals_for_fatal_pattern` deals, fall back to all-stage stats and label
    the pattern "(all stages)".

    Per DECISIONS.md D3 the single resolved pattern feeds BOTH the score and
    the fatal-pattern check, so the reason line and the score can never
    disagree about which set of deals they are talking about.
    """
    tagged = [d for d in deals if tag in d.objections]
    if not tagged:
        return None  # SPEC 4: no team history -> ignored, never invented

    scoped = [d for d in tagged if d.objection_stage == stage]
    if len(scoped) >= scoring.min_deals_for_fatal_pattern:
        return _pattern(f"objection:{tag}@{stage}", scoped)

    return _pattern(f"objection:{tag}", tagged, stage_scope="all_stages")


# --------------------------------------------------------------------------
# validation (SPEC 10.2 gate)
# --------------------------------------------------------------------------
def validate_seed(data: SeedData) -> list[str]:
    """Return a list of problems. Empty list means the seed is consistent."""
    problems: list[str] = []
    deals, reps, scoring = data.deals, data.reps, data.scoring

    # --- totals: 30 / 22 lost / 8 won (SPEC 3) ---------------------------
    if len(deals) != 30:
        problems.append(f"expected 30 deals, found {len(deals)}")
    lost = sum(1 for d in deals if d.is_lost)
    won = len(deals) - lost
    if lost != 22:
        problems.append(f"expected 22 lost deals, found {lost}")
    if won != 8:
        problems.append(f"expected 8 won deals, found {won}")

    # --- exactly one objection per deal, from the taxonomy ---------------
    for deal in deals:
        if len(deal.objections) != 1:
            problems.append(
                f"{deal.id}: expected exactly one objection, "
                f"found {len(deal.objections)}"
            )
        for tag in deal.objections:
            if tag not in scoring.objection_taxonomy:
                problems.append(
                    f"{deal.id}: objection '{tag}' is not in objection_taxonomy"
                )
        for name in deal.competitors:
            if name not in KNOWN_COMPETITORS:
                problems.append(
                    f"{deal.id}: competitor '{name}' is not a known competitor"
                )
        if deal.objection_stage not in scoring.stages:
            problems.append(
                f"{deal.id}: objection_stage '{deal.objection_stage}' is not a known stage"
            )

    # --- winning_response only on won deals (SPEC 3) ---------------------
    for deal in deals:
        if deal.is_lost and deal.winning_response is not None:
            problems.append(f"{deal.id}: lost deal must not have a winning_response")
        if not deal.is_lost and deal.winning_response is None:
            problems.append(f"{deal.id}: won deal must have a winning_response")
        if deal.winning_response is not None:
            if deal.winning_response.objection not in deal.objections:
                problems.append(
                    f"{deal.id}: winning_response.objection "
                    f"'{deal.winning_response.objection}' is not one of the deal's objections"
                )
        # won deals have stage_lost_at = null (SPEC 3)
        if not deal.is_lost and deal.stage_lost_at is not None:
            problems.append(f"{deal.id}: won deal must have stage_lost_at = null")
        if deal.is_lost and deal.stage_lost_at is None:
            problems.append(f"{deal.id}: lost deal must have a stage_lost_at")

    # --- reps: deals_worked must equal the deals' rep field (SPEC 10.2) ---
    for rep in reps:
        from_data = {d.id for d in deals if d.rep == rep.name}
        from_seed = set(rep.deals_worked)
        if from_data != from_seed:
            missing = sorted(from_data - from_seed)
            extra = sorted(from_seed - from_data)
            problems.append(
                f"rep {rep.name} ({rep.id}): deals_worked mismatch "
                f"(missing={missing}, unexpected={extra})"
            )

    # --- the new rep has worked none of these deals (SPEC 2) -------------
    for rep in reps:
        if rep.is_new and rep.deals_worked:
            problems.append(
                f"rep {rep.name} is marked is_new but has deals_worked="
                f"{rep.deals_worked}"
            )
    if len([r for r in reps if r.is_new]) != 1:
        problems.append("expected exactly one rep with is_new = true")

    # --- every deal's rep must be a known rep -----------------------------
    rep_names = {r.name for r in reps}
    for deal in deals:
        if deal.rep not in rep_names:
            problems.append(f"{deal.id}: rep '{deal.rep}' is not in reps.json")

    # --- demo call wiring -----------------------------------------------
    demo = data.demo
    if demo.deal.rep not in rep_names:
        problems.append(f"demo deal rep '{demo.deal.rep}' is not in reps.json")
    if len(demo.turns) != 6:
        problems.append(f"expected 6 demo turns, found {len(demo.turns)}")
    rep_turns = [t for t in demo.turns if demo.deal.rep in t.speaker]
    if len(rep_turns) != 1:
        problems.append(
            f"expected exactly 1 rep turn in the demo call, found {len(rep_turns)}"
        )

    # --- expected_patterns.json must match the deals (SPEC 11) ----------
    computed = pattern_index(compute_pattern_counts(deals, scoring))
    for expected in data.expected.patterns:
        actual = computed.get(expected.key)
        if actual is None:
            problems.append(f"expected pattern '{expected.key}' not found in deals")
            continue
        if (actual.total, actual.lost, actual.won) != (
            expected.total,
            expected.lost,
            expected.won,
        ):
            problems.append(
                f"pattern '{expected.key}': expected "
                f"{expected.lost}/{expected.total} lost, computed "
                f"{actual.lost}/{actual.total} lost"
            )
    if (
        data.expected.totals.deals != len(deals)
        or data.expected.totals.lost != lost
        or data.expected.totals.won != won
    ):
        problems.append("expected_patterns.json totals do not match deals.json")

    return problems
