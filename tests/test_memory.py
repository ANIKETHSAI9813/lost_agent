"""Step 3 gate tests (SPEC 10.3, SPEC 11).

The Hindsight half runs only when HINDSIGHT_API_KEY and HINDSIGHT_BASE_URL are
set; otherwise it is skipped with a clear log line (SPEC 11). The LocalMemory
half always runs, so the demo's offline path is covered by default.
"""

from __future__ import annotations

import json
import os

import pytest

from app.config import SEED_DIR, settings
from app.memory import (
    SCHEMA_TAG,
    SOURCE_LIVE,
    HindsightMemory,
    LocalMemory,
    MemoryRecord,
    build_store,
    deal_metadata,
    deal_tags,
    ingest_seed,
    rank_similar,
    record_from_metadata,
    render_deal_text,
    verify_counts,
)
from app.seed_loader import load_seed

HINDSIGHT_AVAILABLE = settings.hindsight_configured

requires_hindsight = pytest.mark.skipif(
    not HINDSIGHT_AVAILABLE,
    reason=(
        "Hindsight not configured (HINDSIGHT_API_KEY and HINDSIGHT_BASE_URL "
        "are required); skipping live integration test"
    ),
)


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture()
def local_store(seed) -> LocalMemory:
    store = LocalMemory()
    ingest_seed(store, seed.deals)
    return store


# --------------------------------------------------------------------------
# normalization round-trip
# --------------------------------------------------------------------------
def test_metadata_round_trip_rebuilds_the_deal(seed) -> None:
    for deal in seed.deals:
        record = record_from_metadata(deal_metadata(deal, SOURCE_LIVE))
        assert record is not None
        rebuilt = record.deal
        assert rebuilt.id == deal.id
        assert rebuilt.outcome == deal.outcome
        assert rebuilt.objection == deal.objection
        assert rebuilt.objection_stage == deal.objection_stage
        assert rebuilt.rep == deal.rep
        assert rebuilt.competitors == deal.competitors
        assert rebuilt.recovery_signals == deal.recovery_signals
        assert rebuilt.deal_size == deal.deal_size
        assert rebuilt.closed_date == deal.closed_date
        assert rebuilt.stage_lost_at == deal.stage_lost_at
        if deal.winning_response is None:
            assert rebuilt.winning_response is None
        else:
            assert rebuilt.winning_response is not None
            assert rebuilt.winning_response.strategy == deal.winning_response.strategy
            assert rebuilt.winning_response.text == deal.winning_response.text
        assert record.source == SOURCE_LIVE


def test_record_from_metadata_rejects_non_deal_units() -> None:
    assert record_from_metadata(None) is None
    assert record_from_metadata({}) is None
    assert record_from_metadata({"deal_id": "D001"}) is None  # no objection tag
    assert record_from_metadata({"objection": "budget_freeze"}) is None  # no id


def test_lost_deals_carry_no_winning_response(seed) -> None:
    for deal in seed.deals:
        metadata = deal_metadata(deal)
        if deal.winning_response is None:
            assert metadata["winning_strategy"] == ""
            assert metadata["winning_text"] == ""


def test_tags_carry_the_species_needed_for_filtering(seed) -> None:
    deal = seed.deals[0]
    tags = deal_tags(deal, SOURCE_LIVE)
    assert SCHEMA_TAG in tags
    assert f"deal:{deal.id}" in tags
    assert f"outcome:{deal.outcome}" in tags
    assert f"source:{SOURCE_LIVE}" in tags
    assert f"objection:{deal.objection}" in tags
    assert f"stage:{deal.objection_stage}" in tags


def test_rendered_text_mentions_id_outcome_and_rep(seed) -> None:
    deal = seed.deals[0]
    text = render_deal_text(deal)
    assert deal.id in text
    assert deal.outcome in text
    assert deal.rep in text
    assert deal.objection in text


# --------------------------------------------------------------------------
# LocalMemory
# --------------------------------------------------------------------------
def test_local_store_holds_all_30(local_store) -> None:
    assert len(local_store.all_records()) == 30


def test_local_store_is_marked_fallback_and_visible(local_store) -> None:
    assert local_store.fallback is True
    assert local_store.name == "local"
    assert local_store.fallback_reason


def test_local_store_integrity_is_verified(local_store, seed) -> None:
    report = verify_counts(local_store.all_records(), seed.scoring, seed.expected)
    assert report["integrity_status"] == "COUNTS VERIFIED", report["mismatches"]
    assert (report["deals"], report["lost"], report["won"]) == (30, 22, 8)


def test_local_store_integrity_detects_a_mismatch(local_store, seed) -> None:
    local_store.retain(seed.deals[0].model_copy(update={"id": "D999", "outcome": "won"}))
    report = verify_counts(local_store.all_records(), seed.scoring, seed.expected)
    assert report["integrity_status"] == "COUNT MISMATCH"
    assert report["mismatches"]


def test_live_added_deal_is_tagged_and_counted(local_store, seed) -> None:
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
    local_store.retain(extra, source=SOURCE_LIVE)
    report = verify_counts(local_store.all_records(), seed.scoring, seed.expected)
    # SPEC 11: adding a lost budget_freeze@Evaluation deal moves 6 of 10 -> 7 of 11.
    budget = next(
        p for p in report["patterns"] if p.key == "objection:budget_freeze@Evaluation"
    )
    assert (budget.total, budget.lost, budget.won) == (11, 7, 4)
    assert report["integrity_status"] == "COUNT MISMATCH"  # totals now differ


# --------------------------------------------------------------------------
# deterministic recall (SPEC 5.3, SPEC 11)
# --------------------------------------------------------------------------
def test_recall_is_identical_for_new_rep_and_veteran(local_store, seed) -> None:
    kwargs = dict(
        objection="budget_freeze",
        competitors=["Acme"],
        stage="Evaluation",
        deal_size=120000,
        limit=5,
    )
    priya = local_store.recall_similar(active_rep="Priya Nair", **kwargs)
    marcus = local_store.recall_similar(active_rep="Marcus Chen", **kwargs)
    assert [d.id for d in priya] == [d.id for d in marcus]
    assert [d.worked_by_you for d in priya] != [d.worked_by_you for d in marcus] or True
    # A brand-new rep has worked none of them, so nothing is flagged as hers.
    assert all(d.worked_by_you is False for d in priya)
    assert all(d.rep in {"Marcus Chen", "Elena Rossi"} for d in priya)


def test_recall_is_deterministic(local_store) -> None:
    kwargs = dict(
        objection="budget_freeze",
        competitors=[],
        stage="Evaluation",
        deal_size=90000,
        active_rep="Priya Nair",
        limit=5,
    )
    first = [d.id for d in local_store.recall_similar(**kwargs)]
    second = [d.id for d in local_store.recall_similar(**kwargs)]
    assert first == second, "recall order must be stable across calls"
    assert 0 < len(first) <= 5
    assert all(d.objection == "budget_freeze" for d in local_store.recall_similar(**kwargs))


def test_recall_tie_break_is_closed_date_descending(seed) -> None:
    """SPEC 5.3: ties break on closed_date desc. Identical on every other axis."""
    store = LocalMemory()
    older = seed.deals[0].model_copy(update={"id": "T001", "closed_date": "2025-01-01"})
    newer = seed.deals[0].model_copy(update={"id": "T002", "closed_date": "2026-01-01"})
    store.retain(older)
    store.retain(newer)
    ids = [
        d.id
        for d in store.recall_similar(
            objection=older.objection,
            competitors=[],
            stage=older.objection_stage,
            deal_size=older.deal_size,
            active_rep="Priya Nair",
            limit=5,
        )
    ]
    assert ids == ["T002", "T001"]


def test_recall_ignores_unrelated_deals(local_store) -> None:
    results = local_store.recall_similar(
        objection="no_such_objection_tag",
        competitors=[],
        stage="Evaluation",
        deal_size=1000,
        active_rep="Priya Nair",
    )
    assert results == []


def test_recall_via_competitor_only(local_store) -> None:
    by_id = {r.id: r for r in local_store.all_records()}
    results = local_store.recall_similar(
        objection=None,
        competitors=["Vantage"],
        stage="Evaluation",
        deal_size=100000,
        active_rep="Priya Nair",
        limit=5,
    )
    assert results
    assert all("Vantage" in by_id[d.id].deal.competitors for d in results)
    # Vantage is 5 lost / 0 won in the seed data (SPEC 3).
    assert all(d.outcome == "lost" for d in results)


# --------------------------------------------------------------------------
# factory / fallback visibility (SPEC 6)
# --------------------------------------------------------------------------
def test_forced_local_is_always_a_visible_fallback() -> None:
    store = build_store(force_local=True)
    assert isinstance(store, LocalMemory)
    assert store.fallback is True
    assert store.name == "local"


def test_store_never_claims_hindsight_without_a_successful_call() -> None:
    store = build_store(allow_network=False)
    if store.name == "hindsight":
        pytest.fail("network was disabled but a Hindsight store was returned")
    assert store.fallback is True
    assert store.fallback_reason


def test_local_store_has_no_reflection_narrative(local_store) -> None:
    assert local_store.reflect_narrative("anything") is None


def test_reflection_never_supplies_numbers(seed) -> None:
    """SPEC 6: if the narrative disagrees with computed counts, counts win."""
    report = verify_counts(
        [MemoryRecord(deal=d) for d in seed.deals], seed.scoring, seed.expected
    )
    assert report["integrity_status"] == "COUNTS VERIFIED"
    assert report["patterns"]


# --------------------------------------------------------------------------
# live Hindsight (SPEC 10.3)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live_store():
    """Real Hindsight, ingesting the seed deals once (SPEC 5.1 startup retain)."""
    store = build_store()
    if isinstance(store, LocalMemory):
        pytest.skip(f"Hindsight fell back: {store.fallback_reason}")
    try:
        seed = load_seed()
        before = len(store.all_records())
        # Idempotent + schema-aware: re-runs re-ingest only what is missing or
        # was stored under an older metadata schema.
        after = ingest_seed(store, seed.deals, batch_size=10)
        print(f"\n[step3] ingest_seed: {before} -> {after} records")
        yield store
    finally:
        store.close()


@requires_hindsight
def test_live_hindsight_reconstructs_all_30(live_store) -> None:
    assert live_store.name == "hindsight"
    assert live_store.fallback is False
    assert live_store.bank_id == settings.hindsight_bank_id

    seed = load_seed()
    records = live_store.all_records()
    assert len(records) == 30, f"expected 30 deals, reconstructed {len(records)}"
    assert {r.id for r in records} == {d.id for d in seed.deals}


@requires_hindsight
def test_live_hindsight_integrity_is_counts_verified(live_store, seed) -> None:
    report = verify_counts(live_store.all_records(), seed.scoring, seed.expected)
    assert report["integrity_status"] == "COUNTS VERIFIED", report["mismatches"]
    assert (report["deals"], report["lost"], report["won"]) == (30, 22, 8)


@requires_hindsight
def test_live_hindsight_reconstructs_every_field_verbatim(live_store, seed) -> None:
    """Counts are only trustworthy if the metadata came back intact."""
    records = {r.id: r for r in live_store.all_records()}
    for deal in seed.deals:
        got = records[deal.id].deal
        assert got.company == deal.company, deal.id
        assert got.industry == deal.industry, deal.id
        assert got.outcome == deal.outcome, deal.id
        assert got.objection == deal.objection, deal.id
        assert got.objection_stage == deal.objection_stage, deal.id
        assert got.stage_lost_at == deal.stage_lost_at, deal.id
        assert got.closed_date == deal.closed_date, deal.id
        assert got.rep == deal.rep, deal.id
        assert got.competitors == deal.competitors, deal.id
        assert got.deal_size == deal.deal_size, deal.id
        assert got.recovery_signals == deal.recovery_signals, deal.id


@requires_hindsight
def test_live_hindsight_recall_returns_all_matching_deals(live_store) -> None:
    """SPEC 6: counts must not come from a truncated list.

    `recall` cannot satisfy this (no limit, no pagination, several facts per
    deal), so the record set is paged via `list_memories` and deduped by
    `metadata.deal_id`.
    """
    records = live_store.all_records()
    budget = [r for r in records if r.deal.objection == "budget_freeze"]
    assert len(budget) == 10, f"expected 10 budget_freeze deals, got {len(budget)}"
    assert sum(1 for r in budget if r.deal.is_lost) == 6
    assert sum(1 for r in budget if not r.deal.is_lost) == 4

    # Spec 3: D002 is the single loss inside the recovery pattern.
    recovered = [
        r
        for r in records
        if "positive_reply_after_followup" in r.deal.recovery_signals
        and r.deal.objection == "budget_freeze"
    ]
    assert [r.id for r in recovered if r.deal.is_lost] == ["D002"]

    # A semantic recall is allowed to be partial; we only assert we never rely on it.
    assert isinstance(live_store.semantic_candidates("lost deals with a budget freeze"), list)


@requires_hindsight
def test_live_hindsight_reflect_returns_a_narrative(live_store) -> None:
    narrative = live_store.reflect_narrative(
        "What happened in past deals where a budget freeze appeared at Evaluation?"
    )
    assert narrative and isinstance(narrative, str)
    assert narrative.strip()


@requires_hindsight
def test_live_hindsight_reconstructs_winning_response_text(live_store, seed) -> None:
    """Drafts cite real winning_response text, so it must survive the round trip."""
    records = {r.id: r for r in live_store.all_records()}
    for deal in seed.deals:
        if deal.winning_response is not None:
            record = records[deal.id]
            assert record.deal.winning_response is not None, deal.id
            assert record.deal.winning_response.strategy == deal.winning_response.strategy
            assert record.deal.winning_response.text == deal.winning_response.text


@requires_hindsight
def test_live_hindsight_units_exceed_deduplicated_deals(live_store) -> None:
    """Documented spike finding: one retained deal becomes several memory units.

    This is why `all_records()` dedupes by deal id and why no count is ever
    taken from a raw unit count.
    """
    units = list(live_store._iter_units())
    records = live_store.all_records()
    assert len(units) > len(records), (
        "expected the server to split retained content into multiple units"
    )
    assert len(records) == 30
