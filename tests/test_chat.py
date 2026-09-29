"""D45: the data-grounded, template-only `/api/chat` endpoint.

Keyless and deterministic like the rest of the suite. Replies are counted from
the session's own records (never the LLM), so the numbers must match the seed
ground truth exactly, two identical questions must give identical answers, and
an unknown phrase must never make up a fact or leak server detail.
"""

from __future__ import annotations

import pytest

from app import main as main_module
from app.memory import LocalMemory
from app.seed_loader import load_seed
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        main_module, "build_store", lambda **kwargs: LocalMemory("chat test: local")
    )
    with TestClient(main_module.app) as test_client:
        yield test_client


def _chat(client, message):
    return client.post("/api/chat", json={"message": message})


def test_greeting_is_template_grounded_and_deterministic(client):
    first = _chat(client, "hello").json()
    second = _chat(client, "hello").json()
    assert first["via"] == "template"
    assert first["topic"] == "greeting"
    assert first["reply"] == second["reply"]
    assert "memory" in first["reply"].lower()


def test_budget_freeze_matches_seed_counts(client):
    r = _chat(client, "what happened to budget-freeze deals?").json()
    assert r["topic"] == "budget_freeze"
    assert r["via"] == "template"
    assert "10 budget freeze deals in shared memory: 4 won, 6 lost." in r["reply"]


def test_close_lost_recounts_and_chat_answers_change(client):
    for _ in range(3):
        assert client.post("/api/demo/step").status_code == 200
    assert client.post("/api/deals/close", json={"outcome": "lost"}).status_code == 200
    r = _chat(client, "budget freeze").json()
    assert "11 budget freeze deals in shared memory: 4 won, 7 lost." in r["reply"]


def test_champion_left_topic(client):
    r = _chat(client, "champion is leaving").json()
    assert r["topic"] == "champion_left"
    assert "champion" in r["reply"]


def test_competitor_asked_by_name(client):
    r = _chat(client, "has Acme won before?").json()
    assert r["topic"] == "competitor"
    assert "Acme" in r["reply"]


def test_scoring_explainer_is_laymans_view(client):
    r = _chat(client, "how do I read the score?").json()
    assert r["topic"] == "scoring"
    assert "0\u2013100" in r["reply"]


def test_draft_explains_verbatim_memory_source(client):
    r = _chat(client, "what is the follow-up draft based on?").json()
    assert r["topic"] == "draft"
    assert "verbatim" in r["reply"] and "oldest win" in r["reply"]


def test_patterns_summary_totals_match_stats(client):
    r = _chat(client, "show me team history").json()
    assert r["topic"] == "patterns"
    assert r["reply"].startswith("Team memory holds 30 deals (8 won, 22 lost).")


def test_help_messages_are_answers(client):
    r = _chat(client, "help").json()
    assert r["topic"] == "help"
    assert "How do I read the score?" in r["reply"]
    assert _chat(client, "   ").json()["topic"] == "help"


def test_unknown_phrase_never_invents_or_leaks(client):
    r = _chat(client, "tell me about quantum widgets").json()
    assert r["topic"] == "fallback"
    assert r["via"] == "template"
    assert "\"tell me about quantum widgets\"" in r["reply"]
    for leak in ("Traceback", "error", "Exception", "GROQ", "api_key", "secret"):
        assert leak.lower() not in r["reply"].lower()


def test_request_validation(client):
    assert _chat(client, "").status_code == 422
    assert _chat(client, "x" * 501).status_code == 422
    assert _chat(client, "x" * 500).status_code == 200