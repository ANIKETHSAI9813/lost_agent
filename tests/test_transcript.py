"""Phase 7 (defect K): `/api/transcript` analyses a pasted call.

Accepts raw `Speaker: text` lines (with optional `[hh:mm]` prefixes and
unlabeled prospect lines) or the `{"turns": [...]}` JSON shape a Gong/Zoom
export maps to, runs every turn through the same pipeline as the demo, and
returns per-turn EventResult records so the UI can animate them identically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.memory import LocalMemory

RAW_CALL = """\
Maria White (Prospect): Hey, thanks for the walkthrough — it looked good.
Maria White (Prospect): Before anything moves forward, our security team wants another security review — that has been sitting with them for two weeks.
[10:14] Maria White (Prospect): Also worth being straight with you: Vantage quoted us a lower price for something very similar.
Priya Nair (Rep): That's fair, Maria. Let me share the security docs we used at two customers this quarter and come back with a cost plan.
Maria White (Prospect): If you can get the security packet to us by Friday, I can make time to walk through it with our team.
"""


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        main_module, "build_store", lambda **kwargs: LocalMemory("transcript test: local")
    )
    with TestClient(main_module.app) as test_client:
        yield test_client


def _post_raw(client, text, headers=None):
    return client.post(
        "/api/transcript",
        content=text,
        headers={"Content-Type": "text/plain", **(headers or {})},
    )


def test_raw_transcript_runs_every_turn_through_the_pipeline(client) -> None:
    response = _post_raw(client, RAW_CALL)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["turns_imported"] == 5
    turns = payload["turns"]

    # Turn 2: security review stall is caught like the demo would.
    assert "security_review_stall" in turns[1]["result"]["signals"]["objections"]
    # Turn 3: Vantage flagged as a competitor.
    assert "Vantage" in turns[2]["result"]["signals"]["competitors"]
    # Turn 4 is a rep turn: nothing applied.
    assert turns[3]["result"]["delta"] == 0.0
    assert turns[3]["result"]["signals"]["objections"] == []
    # Turn 5 is a neutral close: a benign line must not fire a false warning.
    assert turns[4]["result"]["delta"] == 0.0
    # Every input is echoed back so the UI can animate speaker + text.
    first = payload["turns"][0]
    assert first["input"]["speaker"] == "Maria White (Prospect)"
    assert first["input"]["text"].startswith("Hey, thanks")


def test_json_transcript_accepts_the_export_shape(client) -> None:
    response = client.post(
        "/api/transcript",
        json={
            "turns": [
                {
                    "speaker": "Maria Torres (Northwind)",
                    "text": (
                        "Finance just announced a freeze on new vendor spend "
                        "for this quarter."
                    ),
                    "type": "call",
                    "timestamp": "2026-09-28T10:11:00",
                },
                {
                    "speaker": "Priya Nair (Rep)",
                    "text": "Let me pull together a couple of options for you.",
                    "type": "email",
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    turns = response.json()["turns"]
    assert turns[0]["result"]["signals"]["objections"] == ["budget_freeze"]
    assert turns[1]["result"]["delta"] == 0.0
    assert turns[1]["input"]["type"] == "email"


def test_unlabeled_lines_are_treated_as_the_prospect(client) -> None:
    response = _post_raw(client, "We have no budget from finance this quarter.\n")
    assert response.status_code == 200
    turn = response.json()["turns"][0]
    assert turn["input"]["speaker"] == "Prospect (Buyer)"
    assert "budget_freeze" in turn["result"]["signals"]["objections"]


def test_timestamp_prefix_is_parsed_for_display(client) -> None:
    response = _post_raw(client, "[10:02] Maria (Northwind): Just checking in.\n")
    assert response.status_code == 200
    turn = response.json()["turns"][0]
    assert turn["input"]["timestamp"] == "10:02"


def test_transcript_cap_is_hard(client) -> None:
    lines = "\n".join(f"Maria (Northwind): note number {i}." for i in range(101))
    response = _post_raw(client, lines)
    assert response.status_code == 422
    assert "100 turns" in response.json()["detail"]


def test_empty_and_blank_transcripts_are_422(client) -> None:
    assert _post_raw(client, "").status_code == 422
    assert _post_raw(client, "   \n  \n").status_code == 422


def test_an_empty_turn_line_names_its_line_number(client) -> None:
    response = _post_raw(client, "Maria (Northwind):   \n")
    assert response.status_code == 422
    assert "line 1" in response.json()["detail"]


def test_json_transcript_requires_a_turns_list(client) -> None:
    assert client.post("/api/transcript", json={"deals": []}).status_code == 422
    assert client.post("/api/transcript", json=[]).status_code == 422