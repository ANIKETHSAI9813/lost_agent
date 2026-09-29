"""Step 7 gate: the optional Groq layer (SPEC 7, SPEC 10.7).

The gate is "the demo fully works with the key removed" -- asserted by
`test_keyless_demo_never_touches_groq`. A keyless run must be byte-for-byte the
template pipeline. The rest of these tests inject a *scripted fake* Groq client
so every degradation path is proved without the network: bad JSON, unknown
tags, invented numbers, API errors, and a well-behaved model. Any failure must
fall back to the deterministic template and never raise.
"""

from __future__ import annotations

import pytest
from groq import Groq  # noqa: F401  (client signature already verified)

import app.llm as llm_module
import app.pipeline as pipeline_module
from app.config import Settings
from app.memory import LocalMemory
from app.models import EventInput
from app.pipeline import LiveSession
from app.seed_loader import load_seed

REFERENCE = {1: 0.62, 2: 0.566, 3: 0.415, 4: 0.415, 5: 0.299, 6: 0.418}
TOLERANCE = 0.02


class Fake:
    """A scripted Groq client. `script` is contents to return in order; an
    item may be a callable that raises, to simulate an API error."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

        class _Message:
            pass

        class _Choice:
            def __init__(self, content):
                message = _Message()
                message.content = content
                self.message = message

        class _Response:
            def __init__(self, content):
                self.choices = [_Choice(content)]

        class _Completions:
            owner = self

            def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                if not self.owner._script:
                    return _Response(None)
                item = self.owner._script.pop(0)
                if callable(item):
                    raise item()
                return _Response(item)

        class _Chat:
            def __init__(self, completions):
                self.completions = completions

        self.chat = _Chat(_Completions())


@pytest.fixture()
def session() -> LiveSession:
    store = LocalMemory("llm test: local")
    sess = LiveSession(store=store, seed=load_seed())
    sess.startup_retain()
    return sess


@pytest.fixture()
def enable_llm(monkeypatch):
    fake_settings = Settings(groq_api_key="fake-test-key")
    monkeypatch.setattr(llm_module, "settings", fake_settings)
    monkeypatch.setattr(pipeline_module, "settings", fake_settings)
    return fake_settings


class _ClientUse:
    def __init__(self):
        self.uses = 0
        self.client = None

    def get(self):
        if self.client is not None:
            self.uses += 1
        return self.client


def turn(text, etype="call", speaker="Maria Torres (Northwind)") -> EventInput:
    return EventInput(
        event_id="llmt-" + str(hash(text)),
        type=etype,
        speaker=speaker,
        text=text,
        timestamp="2026-09-28T10:00:00",
    )


# --------------------------------------------------------------------------
# EXTRACTION
# --------------------------------------------------------------------------
def test_llm_candidates_replace_keywords_and_set_source(session, enable_llm, monkeypatch):
    fake = Fake([
        '{"objections": ["budget_freeze"], "competitors": []}',  # candidate
        None,  # draft rephrase -> handled by returning None content
        "",  # reason phrase -> invalid, template kept
    ])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(
        turn("We have no budget for this right now, sorry.")
    )
    assert result.signals.source == "llm"
    assert result.signals.objections == ["budget_freeze"]
    assert result.draft_followup is not None
    assert result.draft_followup.generated_by == "template"
    assert "6 of 10" in result.reason  # template phrased, numbers kept


def test_last_candidate_wins_when_llm_suggests_two(session, enable_llm, monkeypatch):
    fake = Fake([
        '{"objections": ["budget_freeze", "timing_slip"], "competitors": []}',
        "",  # reason
    ])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(
        turn("Finance froze spend and we may push this to next quarter.")
    )
    assert result.signals.objections == ["budget_freeze"]
    assert any("kept only" in n for n in result.ignored)


def test_garbage_json_falls_back_to_keyword(session, enable_llm, monkeypatch):
    fake = Fake(["this is not json at all", ""])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(
        turn("We are also talking to Acme, their pitch is similar.")
    )
    assert result.signals.source == "keyword"
    assert result.signals.competitors == ["Acme"]
    assert any("not strict JSON" in n for n in result.ignored)
    assert "6 of 7" in result.reason


def test_unknown_llm_tag_is_rejected(session, enable_llm, monkeypatch):
    fake = Fake(['{"objections": ["made_up_tag"], "competitors": []}', ""])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(turn("Something odd happened with the deal."))
    assert result.signals.objections == []
    assert any("made_up_tag" in n for n in result.ignored)


# --------------------------------------------------------------------------
# REASON
# --------------------------------------------------------------------------
def test_reason_rephrase_must_keep_exact_counts(session, enable_llm, monkeypatch):
    fake = Fake([
        '{"objections": ["budget_freeze"], "competitors": []}',
        "",  # draft rephrase (invalid)
        "It looks like this deal is in trouble now.",  # numbers dropped
    ])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(turn("Finance just froze new vendor spend."))
    assert "6 of 10" in result.reason  # template kept, exact numbers intact
    assert any("reason failed validation" in n for n in result.ignored)


def test_reason_rephrase_used_when_numbers_preserved(session, enable_llm, monkeypatch):
    fake = Fake([
        '{"objections": ["budget_freeze"], "competitors": []}',
        "",  # draft rephrase (invalid)
        "Trouble: 6 of 10 past budget_freeze deals at Evaluation were lost, "
        "so we dropped 15 pts.",
    ])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(turn("Finance just froze new vendor spend."))
    assert result.reason.startswith("Trouble: 6 of 10")
    assert "15 pts" in result.reason


# --------------------------------------------------------------------------
# DRAFT
# --------------------------------------------------------------------------
def test_draft_rephrase_inventing_a_number_is_rejected(session, enable_llm, monkeypatch):
    fake = Fake([
        '{"objections": ["budget_freeze"], "competitors": []}',
        '{"subject": "Keep Northwind moving", "body": "We can close this with an 87% ROI guarantee."}',
        "",  # reason (invalid)
    ])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(turn("Finance just froze new vendor spend."))
    assert result.draft_followup.generated_by == "template"
    assert "87" not in result.draft_followup.body  # invented figure thrown away
    assert any("draft failed validation" in n for n in result.ignored)


def test_draft_rephrase_used_when_clean(session, enable_llm, monkeypatch):
    clean_rephrase = (
        '{"subject": "Two ways to keep moving", "body": "Hi Maria, we can '
        'start with a phased rollout on your core team, or run a pilot first. '
        'Let me know which."}'
    )
    fake = Fake([
        '{"objections": ["budget_freeze"], "competitors": []}',
        clean_rephrase,
        "",  # reason (invalid)
    ])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    result = session.process_event(turn("Finance just froze new vendor spend."))
    assert result.draft_followup.generated_by == "groq"
    assert "core team" in result.draft_followup.body
    # Citations are still the real deals, untouched by the LLM.
    assert [c.id for c in result.draft_followup.based_on] == ["D007", "D009"]


# --------------------------------------------------------------------------
# DEGRADATION: API errors never raise
# --------------------------------------------------------------------------
def test_api_errors_never_raise_and_demo_still_tracks(session, enable_llm, monkeypatch):
    def boom():
        raise RuntimeError("network down")

    fake = Fake([boom, boom, boom, boom, boom, boom, boom, boom, boom, boom])
    monkeypatch.setattr(llm_module, "_client", lambda: fake)
    scores = []
    while True:
        event = session.next_turn()
        if event is None:
            break
        result = session.advance()
        scores.append(result.score)
        assert result.reason  # template reason still present
    assert scores == pytest.approx(
        [REFERENCE[i] for i in range(1, 7)], abs=TOLERANCE
    )
    assert session.draft is not None and session.draft.generated_by == "template"
    assert session.draft.marked_sent is True  # turn 6 sent it


# --------------------------------------------------------------------------
# THE GATE: works with the key removed
# --------------------------------------------------------------------------
def test_keyless_demo_never_touches_groq(session, monkeypatch):
    keyless = Settings()  # nothing set
    monkeypatch.setattr(llm_module, "settings", keyless)
    monkeypatch.setattr(pipeline_module, "settings", keyless)
    use = _ClientUse()
    monkeypatch.setattr(llm_module, "_client", use.get)

    scores = []
    while True:
        event = session.next_turn()
        if event is None:
            break
        scores.append(session.advance().score)

    assert use.uses == 0, "a keyless run must never construct a Groq client"
    assert scores == pytest.approx(
        [REFERENCE[i] for i in range(1, 7)], abs=TOLERANCE
    )