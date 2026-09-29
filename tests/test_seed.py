"""SPEC 10.2 gate: seed validation.

  - reject unknown objection tags
  - assert totals 30 / 22 lost / 8 won
  - assert Priya's deals_worked is empty
  - assert reps' deals_worked equal the `rep` field in deals
"""

from __future__ import annotations

import pytest

from app.config import KNOWN_COMPETITORS
from app.models import Deal, WinningResponse
from app.seed_loader import (
    RECOVERY_SIGNAL,
    SeedData,
    all_stage_patterns,
    compute_pattern_counts,
    load_seed,
    pattern_index,
    resolve_objection_pattern,
    validate_seed,
)


@pytest.fixture(scope="module")
def seed() -> SeedData:
    return load_seed()


# --------------------------------------------------------------------------
# SPEC 3 ground truth
# --------------------------------------------------------------------------
def test_totals_are_30_22_8(seed: SeedData) -> None:
    assert len(seed.deals) == 30
    lost = [d for d in seed.deals if d.is_lost]
    assert len(lost) == 22
    assert len(seed.deals) - len(lost) == 8


def test_every_deal_has_exactly_one_objection(seed: SeedData) -> None:
    for deal in seed.deals:
        assert len(deal.objections) == 1, f"{deal.id} has {len(deal.objections)}"


def test_objection_tags_are_all_in_taxonomy(seed: SeedData) -> None:
    for deal in seed.deals:
        for tag in deal.objections:
            assert tag in seed.scoring.objection_taxonomy, f"{deal.id}: {tag}"


def test_competitor_names_are_known(seed: SeedData) -> None:
    for deal in seed.deals:
        for name in deal.competitors:
            assert name in KNOWN_COMPETITORS, f"{deal.id}: {name}"


def test_won_deals_have_winning_response_and_lost_do_not(seed: SeedData) -> None:
    for deal in seed.deals:
        if deal.is_lost:
            assert deal.winning_response is None, deal.id
        else:
            assert deal.winning_response is not None, deal.id
            assert deal.stage_lost_at is None, deal.id


# --------------------------------------------------------------------------
# SPEC 10.2: reps
# --------------------------------------------------------------------------
def test_new_rep_has_worked_none_of_these_deals(seed: SeedData) -> None:
    new_reps = [r for r in seed.reps if r.is_new]
    assert len(new_reps) == 1
    assert new_reps[0].deals_worked == []


def test_reps_deals_worked_equal_deal_rep_field(seed: SeedData) -> None:
    for rep in seed.reps:
        assert set(rep.deals_worked) == seed.rep_deal_ids(rep.name), rep.id


def test_every_deal_rep_is_a_known_rep(seed: SeedData) -> None:
    names = {r.name for r in seed.reps}
    for deal in seed.deals:
        assert deal.rep in names, deal.id


# --------------------------------------------------------------------------
# SPEC 11: counts equal expected_patterns.json exactly
# --------------------------------------------------------------------------
def test_computed_patterns_match_expected_patterns(seed: SeedData) -> None:
    computed = pattern_index(compute_pattern_counts(seed.deals, seed.scoring))
    for expected in seed.expected.patterns:
        actual = computed[expected.key]
        assert actual.total == expected.total, expected.key
        assert actual.lost == expected.lost, expected.key
        assert actual.won == expected.won, expected.key


def test_expected_totals_match(seed: SeedData) -> None:
    assert seed.expected.totals.deals == len(seed.deals)
    assert seed.expected.totals.lost == sum(1 for d in seed.deals if d.is_lost)
    assert seed.expected.totals.won == sum(1 for d in seed.deals if not d.is_lost)


def test_recovery_pattern_keeps_the_one_loss_honest(seed: SeedData) -> None:
    """SPEC 3: D002 is the single loss in the recovery pattern. Do not hide it."""
    key = f"recovery:{RECOVERY_SIGNAL}@budget_freeze"
    computed = pattern_index(compute_pattern_counts(seed.deals, seed.scoring))[key]
    assert (computed.total, computed.lost, computed.won) == (5, 1, 4)
    losers = [
        d.id
        for d in seed.deals
        if RECOVERY_SIGNAL in d.recovery_signals
        and "budget_freeze" in d.objections
        and d.is_lost
    ]
    assert losers == ["D002"]


def test_budget_freeze_winning_strategies(seed: SeedData) -> None:
    strategies = [
        d.winning_response.strategy
        for d in seed.deals
        if not d.is_lost
        and "budget_freeze" in d.objections
        and d.winning_response is not None
    ]
    assert sorted(strategies) == sorted(
        seed.expected.winning_responses_for_budget_freeze
    )


# --------------------------------------------------------------------------
# SPEC 3 edge cases that break naive math
# --------------------------------------------------------------------------
def test_vantage_is_5_lost_0_won(seed: SeedData) -> None:
    computed = pattern_index(compute_pattern_counts(seed.deals, seed.scoring))
    vantage = computed["competitor:Vantage"]
    assert (vantage.total, vantage.lost, vantage.won) == (5, 5, 0)
    assert vantage.loss_ratio == 1.0


def test_competitor_pricing_is_2_lost_0_won_all_stages(seed: SeedData) -> None:
    """SPEC 3's `2 / 2 / 0` for competitor_pricing has no `@stage` qualifier.

    The two deals sit at different stages (D022 Evaluation, D023 Proposal), so
    the figure is all-stages and each stage has only 1 deal. Asserted both ways
    so the stage-scoping machinery cannot silently drift.
    """
    all_stages = pattern_index(all_stage_patterns(seed.deals, seed.scoring))
    pricing = all_stages["objection:competitor_pricing"]
    assert (pricing.total, pricing.lost, pricing.won) == (2, 2, 0)
    assert pricing.loss_ratio == 1.0
    assert pricing.stage_scope == "all_stages"

    scoped = pattern_index(compute_pattern_counts(seed.deals, seed.scoring))
    for stage in ("Evaluation", "Proposal"):
        stage_pattern = scoped[f"objection:competitor_pricing@{stage}"]
        assert (stage_pattern.total, stage_pattern.lost, stage_pattern.won) == (1, 1, 0)


def test_competitor_pricing_falls_back_to_all_stages(seed: SeedData) -> None:
    """SPEC 4.1: 1 deal at Evaluation is below min_deals_for_fatal_pattern (4),
    so the live deal must use the all-stages 2/2/0 figure, labelled as such."""
    resolved = resolve_objection_pattern(
        seed.deals, seed.scoring, "competitor_pricing", "Evaluation"
    )
    assert resolved is not None
    assert (resolved.total, resolved.lost, resolved.won) == (2, 2, 0)
    assert resolved.stage_scope == "all_stages"


def test_budget_freeze_resolves_to_stage_scoped(seed: SeedData) -> None:
    """budget_freeze@Evaluation has 10 deals, above the threshold, so no
    fallback: the reason line reads 6 of 10."""
    resolved = resolve_objection_pattern(
        seed.deals, seed.scoring, "budget_freeze", "Evaluation"
    )
    assert resolved is not None
    assert (resolved.total, resolved.lost, resolved.won) == (10, 6, 4)
    assert resolved.stage_scope == "stage"


# --------------------------------------------------------------------------
# demo call wiring
# --------------------------------------------------------------------------
def test_demo_call_shape(seed: SeedData) -> None:
    demo = seed.demo
    assert demo.deal.id == "LIVE-001"
    assert demo.deal.stage == "Evaluation"
    assert len(demo.turns) == 6
    assert [t.id for t in demo.turns] == [1, 2, 3, 4, 5, 6]


def test_turn_four_is_the_only_rep_turn(seed: SeedData) -> None:
    """SPEC 5.2: turn 4 is the rep talking about a freeze and must yield nothing."""
    rep_turns = [t for t in seed.demo.turns if "(Rep)" in t.speaker]
    assert [t.id for t in rep_turns] == [4]


# --------------------------------------------------------------------------
# the validator must actually reject bad data
# --------------------------------------------------------------------------
def test_validator_rejects_unknown_objection_tag(seed: SeedData) -> None:
    bad = seed.deals[0].model_copy(update={"objections": ["totally_made_up_tag"]})
    broken = SeedData(
        deals=[bad, *seed.deals[1:]],
        reps=seed.reps,
        scoring=seed.scoring,
        expected=seed.expected,
        demo=seed.demo,
    )
    problems = validate_seed(broken)
    assert any("totally_made_up_tag" in p for p in problems), problems


def test_validator_rejects_wrong_totals(seed: SeedData) -> None:
    broken = SeedData(
        deals=seed.deals[:-1],
        reps=seed.reps,
        scoring=seed.scoring,
        expected=seed.expected,
        demo=seed.demo,
    )
    problems = validate_seed(broken)
    assert any("expected 30 deals" in p for p in problems), problems


def test_validator_rejects_lost_deal_with_winning_response(seed: SeedData) -> None:
    original = seed.deals[0]
    assert original.is_lost
    tampered: Deal = original.model_copy(
        update={
            "winning_response": WinningResponse(
                objection=original.objections[0],
                strategy="invented",
                text="should not be here",
            )
        }
    )
    broken = SeedData(
        deals=[tampered, *seed.deals[1:]],
        reps=seed.reps,
        scoring=seed.scoring,
        expected=seed.expected,
        demo=seed.demo,
    )
    problems = validate_seed(broken)
    assert any("must not have a winning_response" in p for p in problems), problems


def test_validator_rejects_new_rep_with_history(seed: SeedData) -> None:
    new_rep = [r for r in seed.reps if r.is_new][0]
    tampered = new_rep.model_copy(update={"deals_worked": ["D001"]})
    broken_reps = [tampered if r.id == new_rep.id else r for r in seed.reps]
    broken = SeedData(
        deals=seed.deals,
        reps=broken_reps,
        scoring=seed.scoring,
        expected=seed.expected,
        demo=seed.demo,
    )
    problems = validate_seed(broken)
    assert any("is_new" in p for p in problems), problems


def test_clean_seed_has_no_problems(seed: SeedData) -> None:
    assert validate_seed(seed) == []


# --------------------------------------------------------------------------
# SPEC 0: seed files are read-only
# --------------------------------------------------------------------------
def test_loading_does_not_modify_seed_files() -> None:
    from app.config import SEED_DIR

    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(SEED_DIR.iterdir())
        if p.is_file()
    }
    load_seed.cache_clear()
    load_seed()
    after = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(SEED_DIR.iterdir())
        if p.is_file()
    }
    assert before == after
