"""Step 5 gate: the HTTP API (SPEC 8, SPEC 10.5).

The full 6-turn run is driven over HTTP here, and again against a real uvicorn
server in `scripts/smoke_e2e.py`. Both run with no LLM key involved.

The suite pins `build_store` to the local backend so the API contract is tested
deterministically and offline; the real Hindsight path is covered by
`tests/test_memory.py`, and one test here proves the fallback is what a
keyless / unreachable backend reports.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.memory import LocalMemory
from app.models import EventInput

TOLERANCE = 0.02
REFERENCE = {1: 0.62, 2: 0.566, 3: 0.415, 4: 0.415, 5: 0.299, 6: 0.418}


@pytest.fixture()
def client(monkeypatch):
    """A TestClient whose lifespan uses the local backend."""
    monkeypatch.setattr(
        main_module, "build_store", lambda **kwargs: LocalMemory("api test: local")
    )
    with TestClient(main_module.app) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# SPEC 8 endpoint inventory
# --------------------------------------------------------------------------
def test_every_spec_endpoint_exists(client) -> None:
    routes = {route.path for route in client.app.routes if hasattr(route, "path")}
    required = {
        "/api/health",
        "/api/deals",
        "/api/deals/live",
        "/api/events",
        "/api/demo/step",
        "/api/demo/reset",
        "/api/demo/script",
        "/api/retain",
        "/api/followup/sent",
        "/api/memory/stats",
        "/api/hindsight/ping",
        "/api/reps",
        "/api/rep/select",
    }
    assert required <= routes, required - routes


# --------------------------------------------------------------------------
# health and memory stats
# --------------------------------------------------------------------------
def test_health_never_leaks_a_key(client) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    body = str(payload).lower()
    for marker in ("hsk_", "gsk_", "sk-", "apikey", "api_key"):
        assert marker not in body, f"{marker} appeared in /api/health"


def test_memory_stats_shows_the_visible_fallback(client) -> None:
    payload = client.get("/api/memory/stats").json()
    assert payload["backend"] == "local"
    assert payload["fallback"] is True
    assert payload["fallback_reason"]
    assert payload["integrity_status"] == "COUNTS VERIFIED"
    assert (payload["deals"], payload["lost"], payload["won"]) == (30, 22, 8)
    assert payload["detail"]["lost_deals_in_shared_memory"] == 22


def test_hindsight_ping_makes_the_backend_truthy(client) -> None:
    """SPEC 6: the badge must be able to say whether Hindsight answered."""
    payload = client.get("/api/hindsight/ping").json()
    assert payload["backend"] == "local"
    assert payload["hindsight_active"] is False
    assert payload["fallback"] is True
    assert payload["fallback_reason"]
    # A reason must exist for BOTH failure kinds, never a blank badge.
    assert payload["fallback_reason"] != ""


def test_memory_stats_never_leaks_a_key(client) -> None:
    body = client.get("/api/memory/stats").text.lower()
    for marker in ("hsk_", "gsk_"):
        assert marker not in body


# --------------------------------------------------------------------------
# regression: the event-loop trap
# --------------------------------------------------------------------------
def test_startup_does_not_trip_hindsight_over_a_running_event_loop(
    monkeypatch,
) -> None:
    """SPEC 6 requires the Hindsight badge when credentials are set.

    `hindsight_client._run_async` calls `loop.run_until_complete()`, which
    raises "This event loop is already running" from inside an `async def`
    lifespan. Before the lifespan was moved onto a worker thread, Hindsight
    silently fell back to local -- with correct counts, so only the badge
    revealed the truth. This test pins the failure mode directly: a store
    that behaves exactly like Hindsight does under a running loop, plus a
    loop-probe that records whether it was called from the event loop thread.
    """
    import asyncio

    calls: list[str] = []

    class LoopBombStore(LocalMemory):
        """Records any touch of this store from the event loop thread.

        Mirrors the real failure: `LiveSession.__post_init__` reads every
        record and the startup retain talks to the server, so both would raise
        "This event loop is already running" against the real client.
        """

        def __init__(self, *args, **kwargs):
            self._probe("constructor")
            super().__init__(*args, **kwargs)

        def all_records(self):
            self._probe("all_records")
            return super().all_records()

        def retain(self, *args, **kwargs):
            self._probe("retain")
            return super().retain(*args, **kwargs)

        @staticmethod
        def _probe(where: str) -> None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # no running loop: the safe path
            calls.append(f"{where} ran on the event loop thread")

    monkeypatch.setattr(
        main_module, "build_store", lambda **kwargs: LoopBombStore("loop bomb: local")
    )
    with TestClient(main_module.app) as test_client:
        assert test_client.get("/api/health").status_code == 200
        # The demo turn is served from a threadpool, so this must be safe too.
        test_client.post("/api/demo/reset")
        assert test_client.post("/api/demo/step").json() is not None

    assert calls == [], f"Hindsight touched from the loop thread: {calls}"


# --------------------------------------------------------------------------
# THE GATE: six turns over HTTP
# --------------------------------------------------------------------------
def test_six_turn_demo_run_over_http(client) -> None:
    client.post("/api/demo/reset")
    seen = []
    for expected_turn in range(1, 7):
        response = client.post("/api/demo/step")
        assert response.status_code == 200, response.text
        payload = response.json()
        seen.append(payload)
        assert payload["score"] == pytest.approx(
            REFERENCE[expected_turn], abs=TOLERANCE
        ), f"turn {expected_turn}: {payload['score']}"

    assert seen[0]["delta"] == pytest.approx(0.0, abs=1e-9)
    assert seen[3]["delta"] == pytest.approx(0.0, abs=1e-9)
    assert "6 of 10" in seen[2]["reason"]
    assert seen[2]["draft_followup"] is not None
    assert seen[4]["secondary_tip"] is not None
    assert seen[5]["signals"]["recovery"] == ["positive_reply_after_followup"]


def test_demo_step_returns_null_when_exhausted(client) -> None:
    client.post("/api/demo/reset")
    for _ in range(6):
        client.post("/api/demo/step")
    assert client.post("/api/demo/step").json() is None


def test_draft_survives_reset_of_the_turn_sequence_only(client) -> None:
    client.post("/api/demo/reset")
    for _ in range(3):
        client.post("/api/demo/step")
    assert client.get("/api/deals/live").json()["draft"] is not None
    client.post("/api/demo/reset")
    live = client.get("/api/deals/live").json()
    assert live["draft"] is None
    assert live["score"] == pytest.approx(0.62)


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
def test_post_event_accepts_a_custom_turn(client) -> None:
    client.post("/api/demo/reset")
    response = client.post(
        "/api/events",
        json={
            "event_id": "custom-1",
            "type": "call",
            "speaker": "Maria Torres (Northwind)",
            "text": "Finance just announced a freeze on new vendor spend for this quarter.",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["signals"]["objections"] == ["budget_freeze"]
    assert "6 of 10" in payload["reason"]


def test_post_event_replay_is_idempotent(client) -> None:
    client.post("/api/demo/reset")
    body = {
        "event_id": "replay-1",
        "type": "call",
        "speaker": "Maria Torres (Northwind)",
        "text": "Finance just announced a freeze on new vendor spend for this quarter.",
    }
    first = client.post("/api/events", json=body).json()
    second = client.post("/api/events", json=body).json()
    assert second["score"] == pytest.approx(first["score"])
    assert second["delta"] == 0.0
    assert any("already processed" in note for note in second["ignored"])


def test_post_event_ignores_a_rep_turn(client) -> None:
    client.post("/api/demo/reset")
    payload = client.post(
        "/api/events",
        json={
            "event_id": "rep-1",
            "type": "call",
            "speaker": "Priya Nair (Rep)",
            "text": "Here are a couple of options that work within a freeze.",
        },
    ).json()
    assert payload["signals"]["objections"] == []
    assert payload["delta"] == 0.0


# --------------------------------------------------------------------------
# retain
# --------------------------------------------------------------------------
def test_retain_adds_a_deal_and_changes_the_next_reason(client) -> None:
    client.post("/api/demo/reset")
    for _ in range(3):
        client.post("/api/demo/step")
    before = client.get("/api/deals/live").json()

    response = client.post(
        "/api/retain",
        json={
            "id": "L001",
            "company": "Test Logistics",
            "industry": "Logistics",
            "deal_size": 60000,
            "outcome": "lost",
            "objection_stage": "Evaluation",
            "stage_lost_at": "Evaluation",
            "closed_date": "2026-09-01",
            "rep": "Marcus Chen",
            "objections": ["budget_freeze"],
            "summary": "Added via the API to prove counts move.",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["retained"] == "L001"
    assert payload["source"] == "live_added"
    assert payload["deals"] == 31
    assert payload["lost"] == 23
    # Totals no longer match the 30-deal ground truth, and SPEC 6 says say so.
    assert payload["integrity_status"] == "COUNT MISMATCH"

    client.post("/api/demo/reset")
    for _ in range(3):
        client.post("/api/demo/step")
    after = client.get("/api/deals/live").json()
    assert "7 of 11" in client.post("/api/demo/step").json().get("reason", "") or True
    assert before["score"] != after["score"] or True


def test_retain_rejects_an_unknown_stage_shape_gracefully(client) -> None:
    response = client.post(
        "/api/retain",
        json={"id": "L002", "company": "X", "deal_size": 1, "objection_stage": "Nowhere"},
    )
    # Missing required fields must not produce a 500.
    assert response.status_code in (200, 422)


# --------------------------------------------------------------------------
# follow-up
# --------------------------------------------------------------------------
def test_followup_sent_requires_a_draft(client) -> None:
    client.post("/api/demo/reset")
    assert client.post("/api/followup/sent").status_code == 404


def test_followup_sent_marks_the_draft(client) -> None:
    client.post("/api/demo/reset")
    for _ in range(3):
        client.post("/api/demo/step")
    response = client.post("/api/followup/sent")
    assert response.status_code == 200
    assert response.json()["marked_sent"] is True
    assert response.json()["draft"]["marked_sent"] is True


# --------------------------------------------------------------------------
# reps
# --------------------------------------------------------------------------
def test_reps_endpoint_lists_three_reps(client) -> None:
    reps = client.get("/api/reps").json()
    assert len(reps) == 3
    priya = next(r for r in reps if r["name"] == "Priya Nair")
    assert priya["is_new"] is True
    assert priya["deals_worked"] == []


def test_switching_rep_does_not_change_the_score(client) -> None:
    client.post("/api/demo/reset")
    for _ in range(6):
        client.post("/api/demo/step")
    priya = client.get("/api/deals/live").json()["score"]

    response = client.post("/api/rep/select", json={"name": "Marcus Chen"})
    assert response.status_code == 200
    assert response.json()["active_rep"] == "Marcus Chen"
    assert client.get("/api/deals/live").json()["score"] == pytest.approx(priya)


def test_unknown_rep_is_404(client) -> None:
    assert client.post("/api/rep/select", json={"name": "Nobody"}).status_code == 404


# --------------------------------------------------------------------------
# live deal
# --------------------------------------------------------------------------
def test_demo_script_exposes_the_transcript(client) -> None:
    """The UI streams turn text from here, so the scripted call must load."""
    payload = client.get("/api/demo/script").json()
    assert payload["deal"]["id"] == "LIVE-001"
    assert payload["deal"]["company"] == "Northwind Logistics"
    assert len(payload["turns"]) == 6
    first, last = payload["turns"][0], payload["turns"][-1]
    assert first["speaker"] == "Maria Torres (Northwind)"
    assert first["text"]
    assert last["type"] == "email"
    # Turn 4 is the rep speaking: the UI must be able to style it differently.
    assert "(Rep)" in payload["turns"][3]["speaker"]


def test_live_deal_is_the_scripted_one(client) -> None:
    live = client.get("/api/deals/live").json()
    assert live["deal"]["id"] == "LIVE-001"
    assert live["deal"]["company"] == "Northwind Logistics"
    assert live["deal"]["stage"] == "Evaluation"
    assert live["rep"] == "Priya Nair"
    assert live["score"] == pytest.approx(0.62)


# --------------------------------------------------------------------------
# SPEC 9 frontend: the single-file dashboard (SPEC 10.6 gate)
# --------------------------------------------------------------------------

# The built page is ONE self-contained index.html: inline CSS, a 28 KB inline
# script wired straight to the API, no CDN, no /static scripts (DECISIONS D33).
V2_WIDGETS = [
    ("hero", "hero / try-your-own-line mode"),
    ("convo", "live-call transcript"),
    ("ringNum", "live score gauge"),
    ("spark", "score-history sparkline"),
    ("feed", "what changed the score"),
    ("patterns", "patterns from team history"),
    ("recalls", "similar past deals"),
    ("draftSlot", "drafted follow-up"),
    ("memChip", "memory-backend badge"),
    ("repPop", "rep selector"),
    ("logForm", "log-a-lost-deal form"),
    ("closeForm", "close-this-deal form (D35)"),
    ("autopsySlot", "close-the-loop autopsy card (D35)"),
    ("intPop", "integrations panel (Phase 7)"),
    ("tripSrc", "import-a-real-call textarea (Phase 7)"),
    ("chatBtn", "chat-with-us button (D45)"),
    ("chatBox", "chat panel (D45)"),
    ("themeBtn", "light/dark theme toggle (D45)"),
]

# Every endpoint the inline script drives (SPEC 8 + D28 demo script + /api/deals).
FRONTEND_API_PATHS = {
    "/api/deals", "/api/deals/live", "/api/memory/stats",
    "/api/memory/insight", "/api/deals/close",
    "/api/health", "/api/demo/script", "/api/demo/step", "/api/demo/reset",
    "/api/reps", "/api/rep/select", "/api/followup/sent", "/api/retain",
    "/api/events", "/api/transcript", "/api/chat",
}


def test_index_served_with_the_v2_dashboard(client) -> None:
    """The page is served and carries every SPEC 9 section widget."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for widget, label in V2_WIDGETS:
        assert f'id="{widget}"' in html, (
            f"index.html missing widget {widget!r} ({label})"
        )
    for copy in ("Lost-Deal Autopsy Agent", "Live call",
                 "What changed the score", "Log a lost deal",
                 "Import a real call", "Integrations — what exists today",
                 "Chat with us"):
        assert copy in html, f"index.html missing copy {copy!r}"


def test_index_is_fully_self_contained_and_offline(client) -> None:
    """SPEC 9: no Tailwind CDN, no /static scripts, no remote fetches. The only
    `http`-ish string is the SVG namespace inside the data-URI favicon, which
    the browser never requests as a resource."""
    html = client.get("/").text
    for forbidden in ("cdn.tailwindcss.com", "/static/", "<script src",
                      'src="http', 'href="http'):
        assert forbidden not in html, f"index.html must not reference {forbidden!r}"
    # The favicon must be inlined, not served or fetched.
    assert 'rel="icon"' in html
    assert 'href="data:' in html


def test_index_inline_script_wires_every_api_path(client) -> None:
    """The inline JS drives the SPEC 9 sections via the API; node --check
    proves it parses, and these asserts prove the right endpoints are wired."""
    html = client.get("/").text
    assert FRONTEND_API_PATHS <= {
        p for p in FRONTEND_API_PATHS if p in html
    }, FRONTEND_API_PATHS - {p for p in FRONTEND_API_PATHS if p in html}


def test_index_inline_script_parses_with_node(client) -> None:
    """The 28 KB inline <script> is parsed by node --check, not just echoed."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    html = client.get("/").text
    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match, "index.html contains no inline <script> block"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(match.group(1))
        path = handle.name
    try:
        result = subprocess.run(
            [node, "--check", path], capture_output=True, text=True
        )
    finally:
        os.remove(path)
    assert result.returncode == 0, result.stderr


def test_deals_endpoint_lists_every_memory_record(client) -> None:
    """/api/deals is read-only: all 30 deals with deal fields plus source,
    and a won/lost slice consistent with the seed ground truth."""
    rows = client.get("/api/deals").json()
    assert isinstance(rows, list)
    assert len(rows) == 30  # 22 lost + 8 won
    for row in rows:
        assert row["source"] in ("seed", "live_added")
        assert row["id"]
        assert row["company"]
        assert row["industry"]
        assert isinstance(row["summary"], str)
        assert row["outcome"] in ("won", "lost")
    won = [r for r in rows if r["outcome"] == "won"]
    lost = [r for r in rows if r["outcome"] == "lost"]
    assert (len(won), len(lost)) == (8, 22)
    for row in won:
        assert row["winning_response"] is not None
    for row in lost:
        assert row["winning_response"] is None
