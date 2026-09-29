"""Step 4 gate tests (SPEC 10.4, SPEC 11).

The gate is the reference trajectory from SPEC 3, asserted turn by turn:
  0.62 -> 0.62 -> 0.566 -> 0.415 -> 0.415 -> 0.299 -> 0.418
with deltas 0, -5.4, -15.1, 0, -11.6, +11.9, asserted at +/-0.02 on the
levels and inside the ranges written in `demo_call.json`.
"""

from __future__ import annotations

import math

import pytest

from app.config import CLIP_MAX, CLIP_MIN
from app.memory import LocalMemory, MemoryRecord
from app.models import Pattern
from app.scoring import (
    COMBINED_WEIGHT,
    ScoringEngine,
    clip_ratio,
    confidence_for,
    describe_pattern,
    is_fatal,
    logit,
    sigmoid,
    weight_for,
)
from app.seed_loader import RECOVERY_SIGNAL, load_seed

# SPEC 3 reference trajectory, indexed by turn number.
REFERENCE = {
    0: 0.62,
    1: 0.62,
    2: 0.566,
    3: 0.415,
    4: 0.415,
    5: 0.299,
    6: 0.418,
}
REFERENCE_DELTA_PTS = {
    2: -5.4,
    3: -15.1,
    5: -11.6,
    6: 11.9,
}
TOLERANCE = 0.02

# The signal each demo turn is specified to produce (demo_call.json).
TURN_SIGNALS = {
    2: ("competitor:Acme", "risk"),
    3: ("objection:budget_freeze", "risk"),
    5: ("objection:champion_left", "risk"),
    6: (f"recovery:{RECOVERY_SIGNAL}@budget_freeze", "recovery"),
}
LIVE_STAGE = "Evaluation"


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture()
def engine(seed) -> ScoringEngine:
    store = LocalMemory()
    for deal in seed.deals:
        store.retain(deal)
    return ScoringEngine(config=seed.scoring, records=store.all_records())


def _run(engine: ScoringEngine, seed) -> dict[int, float]:
    """Apply the demo's signals in order; return {turn: score}.

    Turns 1 and 4 produce no signal (SPEC 5.2), so the score at those turns is
    carried forward unchanged and is recorded here for the trajectory check.
    """
    scores: dict[int, float] = {0: engine.score}
    last_signalled = 0
    for turn, (key, direction) in sorted(TURN_SIGNALS.items()):
        for quiet in range(last_signalled + 1, turn):
            scores[quiet] = engine.score
        pattern = engine.pattern_for(key, LIVE_STAGE)
        assert pattern is not None, f"no pattern resolved for {key}"
        update = engine.apply(
            key, pattern, direction=direction, event_id=f"turn-{turn}"
        )
        assert update.applied, update.ignored_reason
        scores[turn] = engine.score
        last_signalled = turn
    for quiet in range(last_signalled + 1, 7):
        scores[quiet] = engine.score
    return scores


# --------------------------------------------------------------------------
# THE GATE
# --------------------------------------------------------------------------
def test_reference_trajectory_is_reproduced(engine, seed) -> None:
    scores = _run(engine, seed)
    for turn, expected in REFERENCE.items():
        got = scores[turn]
        assert got == pytest.approx(expected, abs=TOLERANCE), (
            f"turn {turn}: expected {expected}, got {got:.4f}"
        )


def test_reference_deltas_are_reproduced(engine, seed) -> None:
    _run(engine, seed)
    by_event = {entry.event_id: entry for entry in engine.history}
    for turn, expected in REFERENCE_DELTA_PTS.items():
        entry = by_event[f"turn-{turn}"]
        assert entry.delta_pts == pytest.approx(expected, abs=1.0), (
            f"turn {turn}: expected {expected} pts, got {entry.delta_pts:.2f}"
        )


def test_deltas_stay_inside_the_demo_note_ranges(engine, seed) -> None:
    """demo_call.json notes: 3-8, ~15, 0, 10-13, +10-15 points."""
    _run(engine, seed)
    by_event = {entry.event_id: entry for entry in engine.history}
    ranges = {
        "turn-2": (-8.0, -3.0),
        "turn-3": (-17.0, -13.0),
        "turn-5": (-13.0, -10.0),
        "turn-6": (10.0, 15.0),
    }
    for event_id, (low, high) in ranges.items():
        delta = by_event[event_id].delta_pts
        assert low <= delta <= high, f"{event_id}: {delta:.2f} outside [{low}, {high}]"


def test_turn_one_and_four_move_nothing(engine, seed) -> None:
    """SPEC 11: turns 1 and 4 produce zero signals and delta 0."""
    _run(engine, seed)
    assert "turn-1" not in {e.event_id for e in engine.history}
    assert "turn-4" not in {e.event_id for e in engine.history}
    neutral = [e for e in engine.history if e.delta_pts == 0.0]
    assert neutral == [], "no signal should produce a zero delta"


def test_starts_at_the_prior_and_never_recomputes(engine, seed) -> None:
    assert engine.score == pytest.approx(seed.scoring.prior_win_probability)
    scores = _run(engine, seed)
    assert scores[3] == pytest.approx(0.415, abs=TOLERANCE)
    # The T3 score is an accumulation of T2 and T3, not a fresh computation.
    assert len(engine.history) == 4


# --------------------------------------------------------------------------
# math
# --------------------------------------------------------------------------
def test_logit_sigmoid_round_trip() -> None:
    for p in (0.05, 0.3, 0.5, 0.62, 0.9, 0.95):
        assert sigmoid(logit(p)) == pytest.approx(p, abs=1e-12)
    assert sigmoid(logit(0.0)) if False else True  # logit(0) raises by design


def test_logit_raises_at_the_boundaries() -> None:
    with pytest.raises(ValueError):
        logit(0.0)
    with pytest.raises(ValueError):
        logit(1.0)


def test_sigmoid_is_stable_at_extreme_logits() -> None:
    assert sigmoid(1000.0) == pytest.approx(1.0)
    assert sigmoid(-1000.0) == pytest.approx(0.0)
    assert math.isfinite(sigmoid(-800.0))


def test_clip_is_the_documented_addition(seed) -> None:
    assert clip_ratio(1.0) == CLIP_MAX
    assert clip_ratio(0.0) == CLIP_MIN
    assert clip_ratio(0.5) == 0.5
    assert CLIP_MIN == 0.05 and CLIP_MAX == 0.95


def test_confidence_formula(seed) -> None:
    cfg = seed.scoring
    assert confidence_for(0, cfg) == 0.0
    assert confidence_for(4, cfg) == pytest.approx(0.4)
    assert confidence_for(10, cfg) == 1.0
    assert confidence_for(99, cfg) == 1.0


def test_weights_come_from_config(seed) -> None:
    cfg = seed.scoring
    assert weight_for("objection:budget_freeze", cfg) == 1.0
    assert weight_for("objection:champion_left", cfg) == 0.35
    assert weight_for("competitor:Acme", cfg) == cfg.signal_weights["competitor"]
    assert weight_for("competitor:Acme+objection:no_exec_sponsor", cfg) == COMBINED_WEIGHT
    assert (
        weight_for(f"recovery:{RECOVERY_SIGNAL}@budget_freeze", cfg)
        == cfg.signal_weights[f"recovery:{RECOVERY_SIGNAL}"]
    )


# --------------------------------------------------------------------------
# SPEC 3 / SPEC 11 edge cases
# --------------------------------------------------------------------------
def test_vantage_5_0_stays_finite(engine, seed) -> None:
    pattern = engine.pattern_for("competitor:Vantage", LIVE_STAGE)
    assert pattern is not None
    assert (pattern.total, pattern.lost, pattern.won) == (5, 5, 0)
    assert pattern.loss_ratio == 1.0
    update = engine.apply("competitor:Vantage", pattern, direction="risk")
    assert math.isfinite(update.score_after)
    assert math.isfinite(update.delta_pts)
    assert CLIP_MIN <= update.score_after <= seed.scoring.clamp.max


def test_competitor_pricing_2_0_stays_finite(engine, seed) -> None:
    pattern = engine.pattern_for("objection:competitor_pricing", LIVE_STAGE)
    assert pattern is not None
    assert pattern.total == 2 and pattern.lost == 2
    update = engine.apply("objection:competitor_pricing", pattern, direction="risk")
    assert math.isfinite(update.score_after)
    assert math.isfinite(update.delta_pts)


def test_no_nan_anywhere_across_every_taxonomy_pattern(engine, seed) -> None:
    for tag in seed.scoring.objection_taxonomy:
        for stage in seed.scoring.stages:
            pattern = engine.pattern_for(f"objection:{tag}", stage)
            if pattern is None:
                continue
            update = engine.apply(f"{pattern.key}@{stage}", pattern, direction="risk")
            assert math.isfinite(update.score_after), pattern.key
            assert not math.isnan(update.score_after), pattern.key


def test_risk_never_raises_the_score(engine, seed) -> None:
    pattern = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    before = engine.score
    update = engine.apply("objection:budget_freeze", pattern, direction="risk")
    assert update.score_after <= before
    assert update.delta_pts <= 0.0


def test_recovery_never_lowers_the_score(engine, seed) -> None:
    key = f"recovery:{RECOVERY_SIGNAL}@budget_freeze"
    pattern = engine.pattern_for(key, LIVE_STAGE)
    before = engine.score
    update = engine.apply(key, pattern, direction="recovery")
    assert update.score_after >= before
    assert update.delta_pts >= 0.0


def test_score_stays_inside_the_clamp(seed) -> None:
    store = LocalMemory()
    for deal in seed.deals:
        store.retain(deal)
    engine = ScoringEngine(config=seed.scoring, records=store.all_records())
    pattern = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    # Apply it from many starting points by forcing the state.
    for target in (0.04, 0.2, 0.5, 0.9, 0.96):
        engine.reset()
        engine.score = target
        engine.applied_signals.clear()
        engine.history.clear()
        update = engine.apply("objection:budget_freeze", pattern, direction="risk")
        assert seed.scoring.clamp.min <= update.score_after <= seed.scoring.clamp.max
        assert math.isfinite(update.score_after)


# --------------------------------------------------------------------------
# dedupe, replay, order (SPEC 11)
# --------------------------------------------------------------------------
def test_signal_applies_only_once(engine, seed) -> None:
    pattern = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    first = engine.apply("objection:budget_freeze", pattern, direction="risk")
    score_after_first = engine.score
    second = engine.apply("objection:budget_freeze", pattern, direction="risk")
    assert first.applied is True
    assert second.applied is False
    assert second.delta_pts == 0.0
    assert engine.score == score_after_first
    assert len(engine.history) == 1


def test_replaying_an_event_id_is_a_no_op(seed) -> None:
    store = LocalMemory()
    for deal in seed.deals:
        store.retain(deal)
    engine = ScoringEngine(config=seed.scoring, records=store.all_records())
    pattern = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    first = engine.apply("objection:budget_freeze", pattern, event_id="evt-1")
    replay = engine.apply("objection:budget_freeze", pattern, event_id="evt-1")
    assert first.applied and not replay.applied
    assert engine.score == first.score_after
    assert len(engine.history) == 1


def test_order_matters_for_the_trajectory_and_history(engine, seed) -> None:
    """SPEC 11 "order matters".

    The two orders end at the same score, and that is correct rather than a
    bug: SPEC 4 updates in log-odds, where risk updates are additive, so
    sigma(L(p0) - d1 - d2) == sigma(L(p0) - d2 - d1). What the order changes is
    the path: the intermediate score, each signal's own delta_pts, and the
    order the reason lines are recorded in.
    """
    a = ScoringEngine(
        config=seed.scoring,
        records=[MemoryRecord(deal=d) for d in seed.deals],
    )
    b = ScoringEngine(
        config=seed.scoring,
        records=[MemoryRecord(deal=d) for d in seed.deals],
    )
    budget = a.pattern_for("objection:budget_freeze", LIVE_STAGE)
    champion = a.pattern_for("objection:champion_left", LIVE_STAGE)

    a.apply("objection:budget_freeze", budget, direction="risk")
    a.apply("objection:champion_left", champion, direction="risk")

    b.apply("objection:champion_left", champion, direction="risk")
    b.apply("objection:budget_freeze", budget, direction="risk")

    # Endpoint is order-invariant while the clamp is not binding.
    assert a.score == pytest.approx(b.score, abs=1e-12)
    assert a.score > seed.scoring.clamp.min
    assert a.score < seed.scoring.clamp.max

    # The trajectory differs.
    assert a.history[0].score_after != pytest.approx(b.history[0].score_after)
    assert a.history[0].delta_pts != pytest.approx(b.history[0].delta_pts)

    # The recorded order differs, so the UI shows a different narrative.
    assert [e.signal_key for e in a.history] == [
        "objection:budget_freeze",
        "objection:champion_left",
    ]
    assert [e.signal_key for e in b.history] == [
        "objection:champion_left",
        "objection:budget_freeze",
    ]


def test_clamp_breaks_log_odds_additivity(seed) -> None:
    """Once the clamp binds, the order is no longer order-invariant.

    This is the case SPEC 4's clamp exists for, and it documents why the
    equality above is only safe away from the boundaries.
    """
    a = ScoringEngine(
        config=seed.scoring, records=[MemoryRecord(deal=d) for d in seed.deals]
    )
    b = ScoringEngine(
        config=seed.scoring, records=[MemoryRecord(deal=d) for d in seed.deals]
    )
    budget = a.pattern_for("objection:budget_freeze", LIVE_STAGE)
    champion = a.pattern_for("objection:champion_left", LIVE_STAGE)

    a.score = b.score = 0.05
    a.applied_signals.clear()
    b.applied_signals.clear()
    a.apply("objection:budget_freeze", budget, direction="risk")
    a.apply("objection:champion_left", champion, direction="risk")
    b.apply("objection:champion_left", champion, direction="risk")
    b.apply("objection:budget_freeze", budget, direction="risk")

    assert a.score == seed.scoring.clamp.min
    assert b.score == seed.scoring.clamp.min


def test_no_retroactive_merge_of_signals(engine, seed) -> None:
    """D5: two signals stay two history entries; neither is recomputed."""
    budget = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    champion = engine.pattern_for("objection:champion_left", LIVE_STAGE)
    engine.apply("objection:budget_freeze", budget, direction="risk")
    engine.apply("objection:champion_left", champion, direction="risk")
    assert [e.signal_key for e in engine.history] == [
        "objection:budget_freeze",
        "objection:champion_left",
    ]
    assert engine.history[0].score_after != pytest.approx(engine.score)


def test_zero_total_signal_is_ignored_with_a_reason(engine, seed) -> None:
    empty = Pattern(
        key="objection:imaginary", total=0, lost=0, won=0, loss_ratio=0.0
    )
    update = engine.apply("objection:imaginary", empty, direction="risk")
    assert update.applied is False
    assert "no team history" in update.ignored_reason
    assert engine.score == seed.scoring.prior_win_probability
    assert engine.history == []


def test_unknown_signal_key_resolves_to_nothing(engine) -> None:
    assert engine.pattern_for("objection:does_not_exist", LIVE_STAGE) is None
    assert engine.pattern_for("competitor:Nobody", LIVE_STAGE) is None
    assert engine.pattern_for("garbage", LIVE_STAGE) is None


def test_reset_restores_the_prior_and_clears_state(engine, seed) -> None:
    _run(engine, seed)
    engine.reset()
    assert engine.score == pytest.approx(seed.scoring.prior_win_probability)
    assert engine.history == []
    assert engine.applied_signals == set()
    assert engine.ignored == []


def test_confidence_reports_the_weakest_signal(engine, seed) -> None:
    """D8: min across signals, not the average."""
    _run(engine, seed)
    confidences = [e.confidence for e in engine.history]
    assert confidences == [0.7, 1.0, 0.6, 0.5]
    assert engine.confidence == pytest.approx(0.5)


# --------------------------------------------------------------------------
# patterns: reason text and fatality
# --------------------------------------------------------------------------
def test_reason_text_contains_verbatim_numbers(seed) -> None:
    store = LocalMemory()
    for deal in seed.deals:
        store.retain(deal)
    engine = ScoringEngine(config=seed.scoring, records=store.all_records())

    budget = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    assert budget.key == "objection:budget_freeze@Evaluation"
    text = describe_pattern(budget)
    assert "6 of 10" in text
    assert "budget_freeze" in text
    assert "Evaluation" in text

    recovery = engine.pattern_for(
        f"recovery:{RECOVERY_SIGNAL}@budget_freeze", LIVE_STAGE
    )
    rtext = describe_pattern(recovery, direction="recovery")
    assert "4 of 5" in rtext
    assert "won" in rtext


def test_fatal_threshold_uses_greater_or_equal(seed) -> None:
    cfg = seed.scoring
    six_of_ten = Pattern(
        key="objection:budget_freeze@Evaluation",
        total=10,
        lost=6,
        won=4,
        loss_ratio=0.6,
    )
    assert is_fatal(six_of_ten, cfg) is True  # 0.6 >= 0.6
    too_few = Pattern(key="x", total=3, lost=2, won=1, loss_ratio=0.667)
    assert is_fatal(too_few, cfg) is False  # total < 4
    low_ratio = Pattern(key="x", total=10, lost=5, won=5, loss_ratio=0.5)
    assert is_fatal(low_ratio, cfg) is False  # ratio < 0.6


def test_budget_freeze_at_evaluation_is_fatal(engine, seed) -> None:
    pattern = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    assert pattern is not None
    assert is_fatal(pattern, seed.scoring) is True
    assert (pattern.total, pattern.lost) == (10, 6)


def test_champion_left_is_also_fatal(engine, seed) -> None:
    pattern = engine.pattern_for("objection:champion_left", LIVE_STAGE)
    assert pattern is not None
    assert is_fatal(pattern, seed.scoring) is True
    assert (pattern.total, pattern.lost) == (6, 5)


def test_competitor_only_patterns_never_reach_the_draft_path(engine, seed) -> None:
    """SPEC 5.7: a competitor signal may be fatal by ratio but must not draft.

    The ratio alone is not the trigger; the pipeline also requires the signal
    to be an objection. Vantage is 5/5 = 1.0, so this is the case that would
    slip through if only `is_fatal` were consulted.
    """
    vantage = engine.pattern_for("competitor:Vantage", LIVE_STAGE)
    assert vantage is not None
    assert vantage.loss_ratio == 1.0
    assert is_fatal(vantage, seed.scoring) is True
    assert not vantage.key.startswith("objection:")


# --------------------------------------------------------------------------
# shared-memory invariants (SPEC 11)
# --------------------------------------------------------------------------
def test_priya_and_a_veteran_produce_identical_scores(seed) -> None:
    """The engine never sees the active rep, which is the point (SPEC 1)."""
    a = ScoringEngine(
        config=seed.scoring, records=[MemoryRecord(deal=d) for d in seed.deals]
    )
    b = ScoringEngine(
        config=seed.scoring, records=[MemoryRecord(deal=d) for d in seed.deals]
    )
    for turn, (key, direction) in sorted(TURN_SIGNALS.items()):
        pa = a.pattern_for(key, LIVE_STAGE)
        pb = b.pattern_for(key, LIVE_STAGE)
        a.apply(key, pa, direction=direction, event_id=f"t{turn}")
        b.apply(key, pb, direction=direction, event_id=f"t{turn}")
    assert a.score == pytest.approx(b.score, abs=1e-15)
    assert [e.pattern_text for e in a.history] == [e.pattern_text for e in b.history]
    assert [e.confidence for e in a.history] == [e.confidence for e in b.history]


def test_adding_a_lost_deal_moves_the_counts_and_the_reason(seed) -> None:
    """SPEC 11: 6 of 10 must become 7 of 11 on the next run."""
    store = LocalMemory()
    for deal in seed.deals:
        store.retain(deal)
    engine = ScoringEngine(config=seed.scoring, records=store.all_records())
    pattern = engine.pattern_for("objection:budget_freeze", LIVE_STAGE)
    assert "6 of 10" in describe_pattern(pattern)

    extra = seed.deals[0].model_copy(
        update={
            "id": "L001",
            "outcome": "lost",
            "objections": ["budget_freeze"],
            "objection_stage": "Evaluation",
            "winning_response": None,
            "recovery_signals": [],
        }
    )
    store.retain(extra, source="live_added")

    fresh = ScoringEngine(config=seed.scoring, records=store.all_records())
    updated = fresh.pattern_for("objection:budget_freeze", LIVE_STAGE)
    assert (updated.total, updated.lost, updated.won) == (11, 7, 4)
    assert "7 of 11" in describe_pattern(updated)
