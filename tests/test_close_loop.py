"""Final-sprint phase 2: close-the-loop (D35) behavior tests.

Keyless and deterministic, exactly like the rest of the suite: LocalMemory,
no network, template path only. These gate the three final-sprint stories:

  1. Close lost  -> budget_freeze@Evaluation 10/6/4 -> 11/7/4, "7 of 11",
                    and the replay scores harder.
  2. Close won   -> 11/6/5 (< fatal_loss_ratio), so the draft stops firing
                    (documented in DECISIONS.md D35) and the winning text is
                    verbatim from the oldest seed win on that strategy.
  3. New won pilot_first deal -> draft ranks pilot first and cites the new id.
"""

from __future__ import annotations

import pytest

from app import main as main_module
from app.draft import build_draft, citation_line
from app.memory import SOURCE_LIVE, LocalMemory
from app.models import Deal, WinningResponse
from app.pipeline import CONTACT_NAME, LiveSession
from app.seed_loader import load_seed
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture()
def session(seed) -> LiveSession:
    store = LocalMemory(reason="test: forced local")
    sess = LiveSession(store=store, seed=seed)
    sess.startup_retain()
    return sess


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        main_module, "build_store", lambda **kwargs: LocalMemory("close test: local")
    )
    with TestClient(main_module.app) as test_client:
        yield test_client


def run_demo(session: LiveSession) -> list:
    results = []
    while True:
        result = session.advance()
        if result is None:
            break
        results.append(result)
    return results


def pattern_counts(payload):
    if payload is None:
        return None
    return (payload["total"], payload["lost"], payload["won"])


# --------------------------------------------------------------------------
# starting point
# --------------------------------------------------------------------------
def test_evaluation_pattern_starts_10_6_4(session) -> None:
    engine = session.engine
    assert engine is not None
    pattern = engine.pattern_for("objection:budget_freeze", "Evaluation")
    assert pattern is not None
    assert (pattern.total, pattern.lost, pattern.won) == (10, 6, 4)


# --------------------------------------------------------------------------
# close as LOST after a full run
# --------------------------------------------------------------------------
def test_close_lost_retains_and_grows_the_pattern(session) -> None:
    results = run_demo(session)
    assert results, "demo should run 6 turns"
    assert session.draft is not None, "budget freeze at T3 must draft"

    close = session.close_deal(outcome="lost")

    assert close["retained_id"] == "C001"
    assert close["outcome"] == "lost"
    assert close["objection"] == "budget_freeze"
    assert close["stage"] == "Evaluation"
    assert close["deals"] == 31
    assert close["lost"] == 23
    assert close["won"] == 8
    assert close["winning_response"] is None
    assert pattern_counts(close["pattern_before"]) == (10, 6, 4)
    assert pattern_counts(close["pattern_after"]) == (11, 7, 4)

    autopsy = close["autopsy"]
    assert autopsy["followup_sent"] is True  # D4: draft is sent entering turn 6
    assert "recovery_signals" in autopsy
    assert any(h["signal_key"] == "objection:budget_freeze"
               for h in autopsy["timeline"])
    assert "6 of 10" in autopsy["lesson"]
    assert "7 of 11" in autopsy["lesson"]

    # The retained record really is in memory, tagged as a live add.
    record = next(r for r in session.records if r.id == "C001")
    assert record.source == SOURCE_LIVE
    assert record.deal.stage_lost_at == "Evaluation"


def test_second_close_on_same_run_is_rejected(session) -> None:
    run_demo(session)
    session.close_deal(outcome="lost")
    with pytest.raises(ValueError):
        session.close_deal(outcome="lost")


def test_close_requires_a_signal_and_an_objection(session) -> None:
    with pytest.raises(ValueError):
        session.close_deal(outcome="lost")  # no signals applied yet


# --------------------------------------------------------------------------
# replay after a lost close sees the grown pattern
# --------------------------------------------------------------------------
def test_replay_after_lost_close_scores_harder_and_says_7_of_11(session) -> None:
    results = run_demo(session)
    t3_before = results[2]  # turn-3 budget freeze
    assert t3_before.delta < 0
    assert "6 of 10" in t3_before.reason

    session.close_deal(outcome="lost")
    session.reset()

    replay = []
    for _ in range(3):
        result = session.advance()
        if result is None:
            break
        replay.append(result)
    assert len(replay) == 3

    t3_after = replay[2]
    assert "7 of 11" in t3_after.reason
    assert session.draft is not None, "11/7/4 (0.636) is still fatal -> draft fires"
    # 7 of 11 is a worse ratio than 6 of 10, so the drop is larger.
    assert t3_after.delta < t3_before.delta


# --------------------------------------------------------------------------
# close as WON -> pattern flips below the fatal ratio
# --------------------------------------------------------------------------
def test_close_won_uses_verbatim_seed_text_and_stops_the_draft(session, seed) -> None:
    d009 = next(d for d in seed.deals if d.id == "D009")
    expected = d009.winning_response.text

    run_demo(session)
    close = session.close_deal(outcome="won", winning_strategy="pilot_first")

    assert close["winning_response"]["strategy"] == "pilot_first"
    assert close["winning_response"]["text"] == expected  # verbatim, D009
    assert pattern_counts(close["pattern_after"]) == (11, 6, 5)

    session.reset()
    replay = [session.advance() for _ in range(3)]
    t3 = replay[2]
    # 6/11 = 0.545 < 0.6, so the draft no longer fires. Not hacked: the
    # pattern is simply no longer fatal (D35 documents this).
    assert "6 of 11" in t3.reason
    assert session.draft is None


def test_close_won_requires_a_known_strategy(session) -> None:
    run_demo(session)
    with pytest.raises(ValueError):
        session.close_deal(outcome="won")  # strategy missing
    with pytest.raises(ValueError):
        session.close_deal(outcome="won", winning_strategy="vaporware")


# --------------------------------------------------------------------------
# draft-from-memory ranking (D36): a new won pilot_first deal flips the order
# --------------------------------------------------------------------------
def test_won_pilot_first_at_negotiation_flips_draft_order_and_cites_it(
    session, seed
) -> None:
    d007_pilot_text = next(
        d.winning_response.text for d in seed.deals if d.id == "D009"
    )
    baseline = build_draft(
        objection="budget_freeze",
        records=session.records,
        contact=CONTACT_NAME,
        company="Northwind Logistics",
    )
    assert [c.id for c in baseline.based_on] == ["D007", "D009"]
    assert "1. Phased rollout. Instead of the full commitment now" in baseline.body
    assert "2. Pilot first. Rather than a purchase" in baseline.body
    assert baseline.subject == "Two ways to keep Northwind Logistics moving through the freeze"

    # A teammate logs a won pilot_first deal at Negotiation.
    session.store.retain(
        Deal(
            id="C099",
            company="Meridian Freight",
            industry="Logistics",
            deal_size=120_000,
            outcome="won",
            objection_stage="Negotiation",
            stage_lost_at=None,
            closed_date="2026-09-29",
            rep="Elena Rossi",
            objections=["budget_freeze"],
            summary="Pilot won at Negotiation.",
            winning_response=WinningResponse(
                objection="budget_freeze",
                strategy="pilot_first",
                text=d007_pilot_text,
            ),
        ),
        source=SOURCE_LIVE,
    )
    session.refresh_records()

    # Evaluation still has exactly 10/6/4 (the Negotiation win is stage-scoped
    # OUT), so the fatal draft still triggers.
    engine = session.engine
    assert engine is not None
    pattern = engine.pattern_for("objection:budget_freeze", "Evaluation")
    assert (pattern.total, pattern.lost, pattern.won) == (10, 6, 4)

    build = build_draft(
        objection="budget_freeze",
        records=session.records,
        contact=CONTACT_NAME,
        company="Northwind Logistics",
    )
    assert build is not None
    assert [c.id for c in build.based_on] == ["C099", "D007"]
    assert [c.strategy for c in build.based_on] == ["pilot_first", "phased_rollout"]
    assert "1. Pilot first. Rather than a purchase" in build.body
    assert "2. Phased rollout. Instead of the full commitment now" in build.body
    assert citation_line(build.based_on) == (
        "Based on: C099, D007 (Elena Rossi, Marcus Chen)"
    )


# --------------------------------------------------------------------------
# reset_live restores the seed state
# --------------------------------------------------------------------------
def test_reset_live_drops_every_live_added_deal(session) -> None:
    run_demo(session)
    session.close_deal(outcome="lost")
    assert session.records[0].source == SOURCE_LIVE or len(session.records) == 31

    report = session.reset_live()

    assert report["retired"] == 1
    assert report["deals"] == 30
    assert report["lost"] == 22
    assert report["won"] == 8
    assert session.engine.score == pytest.approx(0.62, abs=0.02)
    assert session.closed is False
    assert session.draft is None


# --------------------------------------------------------------------------
# Phase 3: the Hindsight memory insight endpoint (D38)
# --------------------------------------------------------------------------
def test_insight_reports_seed_pattern_and_backings(client) -> None:
    ins = client.get("/api/memory/insight?objection=budget_freeze").json()
    assert ins["objection"] == "budget_freeze"
    assert ins["pattern"] == {
        "present": True,
        "key": "objection:budget_freeze@Evaluation",
        "stage_scope": "stage",
        "total": 10,
        "lost": 6,
        "won": 4,
        "loss_ratio": 0.6,
        "fatal": True,
    }
    backs = {(b["id"], b["rep"], b["strategy"]) for b in ins["won_backings"]}
    assert ("D007", "Marcus Chen", "phased_rollout") in backs
    assert ("D008", "Elena Rossi", "cost_of_delay") in backs  # real win; draft-only banned
    assert ("D009", "Marcus Chen", "pilot_first") in backs
    assert ("D010", "Elena Rossi", "cfo_one_pager") in backs
    # Newest first on the card.
    assert ins["won_backings"][0]["id"] == "D010"
    assert ins["by_strategy"] == {
        "cfo_one_pager": 1,
        "cost_of_delay": 1,
        "phased_rollout": 1,
        "pilot_first": 1,
    }
    assert ins["lost_notes"], "lost deals on this objection are visible"
    assert "lost" in ins["lesson"]
    # Semantic arm is display-only; with no keys it is simply not available.
    assert ins["semantic"]["available"] is False
    assert ins["semantic"]["deals"] == []
    assert ins["backend"]["fallback"] is True


def test_insight_defaults_to_budget_freeze(client) -> None:
    ins = client.get("/api/memory/insight").json()
    assert ins["objection"] == "budget_freeze"
    assert ins["pattern"]["total"] == 10


def test_insight_after_won_close_flips_to_non_fatal(client) -> None:
    for _ in range(6):
        client.post("/api/demo/step")
    assert client.post(
        "/api/deals/close", json={"outcome": "won", "winning_strategy": "pilot_first"}
    ).status_code == 200

    ins = client.get("/api/memory/insight?objection=budget_freeze").json()
    assert ins["pattern"]["total"] == 11
    assert ins["pattern"]["lost"] == 6
    assert ins["pattern"]["won"] == 5
    assert ins["pattern"]["fatal"] is False
    assert ins["by_strategy"]["pilot_first"] == 2
    assert "no longer fatal" in ins["lesson"]


def test_insight_never_invents_an_unknown_objection(client) -> None:
    ins = client.get("/api/memory/insight?objection=vaporware").json()
    assert ins["pattern"]["present"] is False
    assert ins["pattern"]["total"] is None
    assert ins["lesson"] is None
    assert ins["won_backings"] == []
# --------------------------------------------------------------------------
# HTTP contract
# --------------------------------------------------------------------------
def test_close_endpoint_over_http(client) -> None:
    for _ in range(6):
        step = client.post("/api/demo/step")
        assert step.status_code == 200

    # Won without a strategy, or with an unknown one, -> 422.
    assert client.post("/api/deals/close", json={"outcome": "won"}).status_code == 422
    assert (
        client.post(
            "/api/deals/close", json={"outcome": "won", "winning_strategy": "vaporware"}
        ).status_code
        == 422
    )

    close = client.post("/api/deals/close", json={"outcome": "lost"})
    assert close.status_code == 200
    body = close.json()
    assert body["retained_id"] == "C001"
    assert pattern_counts(body["pattern_after"]) == (11, 7, 4)

    # Second close on the same run -> 409.
    again = client.post("/api/deals/close", json={"outcome": "lost"})
    assert again.status_code == 409

    # Reset live -> back to 30 seed deals.
    reset = client.post("/api/memory/reset_live")
    assert reset.status_code == 200
    assert reset.json()["deals"] == 30
    assert len(client.get("/api/deals").json()) == 30

    # A fresh run after reset can be closed again.
    for _ in range(6):
        assert client.post("/api/demo/step").status_code == 200
    assert client.post("/api/deals/close", json={"outcome": "lost"}).status_code == 200


def test_close_endpoint_rejects_unknown_stage(client) -> None:
    for _ in range(6):
        client.post("/api/demo/step")
    resp = client.post("/api/deals/close", json={"outcome": "lost", "stage": "WarRoom"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Phase 4: teammate logging a WIN via /api/retain (frontend defect F fixed)
# --------------------------------------------------------------------------
def test_retain_won_requires_known_strategy_and_moves_the_insight(client) -> None:
    body = {
        "id": "L099",
        "company": "Meridian Freight",
        "industry": "Logistics",
        "deal_size": 120_000,
        "outcome": "won",
        "objection_stage": "Negotiation",
        "closed_date": "2026-09-28",
        "rep": "Elena Rossi",
        "objections": ["budget_freeze"],
        "summary": "Pilot won at Negotiation.",
        "winning_strategy": "pilot_first",
    }
    missing = dict(body)
    missing["winning_strategy"] = None
    assert client.post("/api/retain", json=missing).status_code == 422

    bogus = dict(body)
    bogus["winning_strategy"] = "vaporware"
    assert client.post("/api/retain", json=bogus).status_code == 422

    ok = client.post("/api/retain", json=body)
    assert ok.status_code == 200

    ins = client.get("/api/memory/insight?objection=budget_freeze").json()
    assert ins["by_strategy"]["pilot_first"] == 2
    won_ids = [b["id"] for b in ins["won_backings"]]
    assert "L099" in won_ids
    # Winning text is copied verbatim from memory, never invented by the UI.
    d009 = next(b for b in ins["won_backings"] if b["id"] == "L099")
    assert d009["rep"] == "Elena Rossi"

    # The new win at Negotiation leaves Evaluation's fatal 10/6/4 untouched.
    assert client.get("/api/deals/live").json()["score"] == pytest.approx(0.62, abs=0.02)


def test_live_reports_closed_flag(client) -> None:
    assert client.get("/api/deals/live").json()["closed"] is False
    for _ in range(6):
        client.post("/api/demo/step")
    assert client.post("/api/deals/close", json={"outcome": "lost"}).status_code == 200
    assert client.get("/api/deals/live").json()["closed"] is True
    client.post("/api/demo/reset")
    assert client.get("/api/deals/live").json()["closed"] is False