"""D41: the extraction corpus is part of the suite.

`scripts/eval_extraction.py` is the standalone runner; this test imports its
`run_corpus` so `pytest` fails the build on any regression, then locks the
behaviors the demo trajectory depends on (turn 2 Acme, turn 3 budget freeze,
turn 5 champion_left over timing_slip, turn 6 recovery).
"""

from __future__ import annotations

import pytest

from app.extract import extract_turn
from app.seed_loader import load_seed
from scripts.eval_extraction import run_corpus


def _signals(seed, turn, draft_sent: bool = False):
    return extract_turn(
        text=turn.text,
        speaker=turn.speaker,
        event_type=turn.type,
        active_rep="Priya Nair",
        taxonomy=seed.scoring.objection_taxonomy,
        draft_exists=draft_sent,
        draft_marked_sent=draft_sent,
    )[0]


def test_every_extraction_case_passes() -> None:
    results = run_corpus()
    failures = [r for r in results if not r[2]]
    assert not failures, (
        "extraction regressions:\n" + "\n".join(
            f"  #{index:02d} [{case.get('kind')}] {case['text']}"
            f" expected={expected} got={got}"
            for index, case, _ok, expected, got, _i in failures
        )
    )
    assert len(results) >= 40, "corpus must stay >= 40 labelled cases"


def test_demo_turn_signals_still_fire_after_negation_lens() -> None:
    seed = load_seed()
    turns = seed.demo.turns

    # Turn 2: Acme competitor, no objection.
    signals = _signals(seed, turns[1])
    assert signals.competitors == ["Acme"]
    assert signals.objections == []

    # Turn 3: budget freeze fires; the "not sure how we move forward" ahead of
    # it must NOT be read as negating the freeze.
    signals = _signals(seed, turns[2])
    assert signals.objections == ["budget_freeze"]
    assert signals.competitors == []

    # Turn 4: rep turn yields nothing, so drafting stays visible.
    signals = _signals(seed, turns[3])
    assert signals.objections == []
    assert signals.competitors == []

    # Turn 5: longest phrase wins — champion_left over timing_slip.
    signals = _signals(seed, turns[4])
    assert signals.objections == ["champion_left"]

    # Turn 6: recovery fires on the positive email after a sent follow-up.
    signals = _signals(seed, turns[5], draft_sent=True)
    assert signals.recovery == ["positive_reply_after_followup"]
    assert signals.objections == []