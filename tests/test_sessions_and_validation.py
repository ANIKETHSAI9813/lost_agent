"""Phase 6 (D42): per-browser sessions + validation hardening.

Two browser windows can share sre delegated memory but must not share a
scoreboard, so sessions are keyed by the X-Session-Id header over one shared
store, capped with an LRU so a refresh loop can't leak sessions. The retain and
event endpoints reject bad shapes with 4xx in code, never a 500.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.memory import LocalMemory

A = {"X-Session-Id": "browser-a"}
B = {"X-Session-Id": "browser-b"}


@pytest.fixture()
def client(monkeypatch):
    """A TestClient whose lifespan uses the local backend (same as test_api)."""
    monkeypatch.setattr(
        main_module, "build_store", lambda **kwargs: LocalMemory("sessions test: local")
    )
    with TestClient(main_module.app) as test_client:
        yield test_client


def _retain_body(**overrides):
    body = {
        "id": "L900",
        "company": "Phase Six Ltd",
        "industry": "Logistics",
        "deal_size": 120000,
        "outcome": "lost",
        "objection_stage": "Evaluation",
        "stage_lost_at": "Evaluation",
        "closed_date": "2026-09-01",
        "rep": "Marcus Chen",
        "objections": ["budget_freeze"],
        "summary": "cornerstone of the phase-six validation gate",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------
def test_per_browser_sessions_share_memory_but_not_scoreboards(client) -> None:
    for _ in range(3):
        assert client.post("/api/demo/step", headers=A).status_code == 200

    live_a = client.get("/api/deals/live", headers=A).json()
    assert live_a["score"] == pytest.approx(0.415, abs=0.02)
    assert live_a["turn"] == 3

    live_b = client.get("/api/deals/live", headers=B).json()
    assert live_b["score"] == pytest.approx(0.62, abs=1e-9)
    assert live_b["history"] == []
    assert live_b["turn"] == 0

    stats_b = client.get("/api/memory/stats", headers=B).json()
    assert (stats_b["deals"], stats_b["lost"], stats_b["won"]) == (30, 22, 8)


def test_the_same_header_reuses_the_same_scoreboard(client) -> None:
    client.post("/api/demo/step", headers=A)
    client.post("/api/demo/step", headers=A)
    assert client.get("/api/deals/live", headers=A).json()["turn"] == 2
    assert client.get("/api/deals/live", headers=B).json()["turn"] == 0


def test_sessions_are_capped_by_the_lru(client) -> None:
    for i in range(60):
        assert client.get("/api/health", headers={"X-Session-Id": f"tab-{i}"}).status_code == 200
    assert len(main_module._sessions) <= main_module._SESSION_LIMIT
    # The default (no-header) session is still usable after the thrash.
    assert client.post("/api/demo/step").status_code == 200


# --------------------------------------------------------------------------
# retain validation
# --------------------------------------------------------------------------
def test_retain_rejects_a_duplicate_id(client) -> None:
    assert client.post("/api/retain", json=_retain_body()).status_code == 200
    duplicate = client.post("/api/retain", json=_retain_body())
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


def test_retain_rejects_an_unknown_objection(client) -> None:
    response = client.post(
        "/api/retain",
        json=_retain_body(objections=["not_a_real_objection"]),
    )
    assert response.status_code == 422


def test_retain_rejects_more_than_one_objection(client) -> None:
    response = client.post(
        "/api/retain",
        json=_retain_body(objections=["budget_freeze", "timing_slip"]),
    )
    assert response.status_code == 422
    assert "exactly one objection" in response.json()["detail"]


def test_retain_rejects_an_unknown_stage(client) -> None:
    response = client.post(
        "/api/retain",
        json=_retain_body(objection_stage="Neverland"),
    )
    assert response.status_code == 422


def test_retain_rejects_bad_sizes_and_dates(client) -> None:
    assert client.post("/api/retain", json=_retain_body(deal_size=0)).status_code == 422
    assert client.post("/api/retain", json=_retain_body(deal_size=-5)).status_code == 422
    assert (
        client.post(
            "/api/retain", json=_retain_body(closed_date="2026/09/01")
        ).status_code
        == 422
    )
    assert (
        client.post("/api/retain", json=_retain_body(id="L 1")).status_code == 422
    )


def test_close_rejects_an_unknown_stage(client) -> None:
    for _ in range(3):
        client.post("/api/demo/step")
    response = client.post(
        "/api/deals/close",
        json={"outcome": "lost", "stage": "Neverland"},
    )
    assert response.status_code == 422


def test_events_reject_empty_or_oversized_payloads(client) -> None:
    base = {
        "event_id": "bad-1",
        "type": "call",
        "speaker": "Maria Torres (Northwind)",
    }
    assert (
        client.post("/api/events", json={**base, "text": "   "}).status_code == 422
    )
    assert (
        client.post(
            "/api/events", json={**base, "text": "x" * 2001}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/events", json={**base, "text": "fine", "speaker": " "}).status_code
        == 422
    )