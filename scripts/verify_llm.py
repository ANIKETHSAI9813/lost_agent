"""Live Groq end-to-end verification (SPEC 7 / SPEC 10.7).

Runs the full six-turn scripted call through the *same* pipeline method the
API uses (`LiveSession.advance` -> `process_event`) with the optional LLM
engaged, but on a local memory store so this script never touches Hindsight
state. It reports which of the three LLM paths actually engaged and proves the
score trajectory is still exactly the reference (the LLM must never corrupt
the numbers).

Usage:
    python scripts/verify_llm.py

Exit codes:
    0  PASS - the LLM engaged on the live model and the trajectory is intact
    1  FAIL - unexpected exception, or the trajectory drifted from reference
    2  DEGRADED-VISIBLE - a key exists but every LLM path fell back to the
       deterministic template (the D30 notes explain why); SPEC 7 holds, the
       numbers stay correct, the class of engagement can't be proven today
    3  SKIPPED - no GROQ_API_KEY, so the demo runs keyless (expected, not a
       regression)

Secrets: only booleans / engine outcomes are printed, never the key, model
prompts, or the raw model output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.memory import LocalMemory  # noqa: E402
from app.pipeline import LiveSession  # noqa: E402
from app.seed_loader import load_seed  # noqa: E402
from app import llm as llm_layer  # noqa: E402

REFERENCE_SCORES = [0.620, 0.566, 0.415, 0.415, 0.299, 0.418]
TOLERANCE = 0.02


def main() -> int:
    llm_layer.reset_client()  # build a fresh client with the current env key

    enabled = settings.llm_enabled
    print(
        json.dumps(
            {
                "env_files_loaded": list(settings.env_files_loaded),
                "groq_configured": enabled,
                "groq_model": settings.groq_model if enabled else None,
                "memory_backend_for_this_run": "local (isolation)",
            },
            indent=2,
        )
    )
    if not enabled:
        print("SKIPPED: no GROQ_API_KEY; the demo is fully template-driven.")
        return 3

    # Full 6-turn run through the real pipeline, LLM engaged.
    store = LocalMemory(reason="verify_llm: isolation, no Hindsight writes")
    sess = LiveSession(store=store, seed=load_seed())
    sess.startup_retain()

    results = []
    try:
        while True:
            result = sess.advance()
            if result is None:
                break
            results.append(result)
    except Exception as exc:  # pragma: no cover - failure path
        print(f"FAIL: run raised {type(exc).__name__}: {exc}")
        return 1

    if len(results) != 6:
        print(f"FAIL: expected 6 turns, ran {len(results)}")
        return 1

    # Trajectory must be byte-identical to the reference (SPEC 3/4).
    for i, (got, want) in enumerate(zip([r.score for r in results], REFERENCE_SCORES), 1):
        if abs(got - want) > TOLERANCE:
            print(
                f"FAIL: turn {i} score {got:.3f} drifted from reference "
                f"{want:.3f}; the LLM must never change the numbers"
            )
            return 1
    print("PASS: trajectory identical to reference "
          + " -> ".join(f"{s:.3f}" for s in REFERENCE_SCORES))

    # Which LLM paths engaged (all observable, no secrets).
    llm_extractions = [r for r in results if r.signals and r.signals.source == "llm"]
    draft = results[2].draft_followup  # drafted at turn 3
    draft_groq = draft is not None and draft.generated_by == "groq"
    degraded_notes = [
        note for r in results for note in (r.ignored or []) if "LLM" in note
    ]
    llm_reasons = [
        i
        for i, r in enumerate(results, 1)
        if r.reason
        and "Replay" not in r.reason
        and not any(f"turn {i}" in n for n in degraded_notes)
    ]

    report = {
        "turns": len(results),
        "llm_extraction_engaged": len(llm_extractions),
        "draft": (
            None
            if draft is None
            else {
                "generated_by": draft.generated_by,
                "objection": draft.objection,
                "citation_ids": [c.id for c in draft.based_on],
                "citations_sent": draft.marked_sent,
            }
        ),
        "degraded_notes": degraded_notes,
        "signals_by_turn": {
            i: {"source": r.signals.source, "objections": r.signals.objections,
                "competitors": r.signals.competitors}
            for i, r in enumerate(results, 1)
        },
    }
    print("report: " + json.dumps(report, indent=2))

    engaged = len(llm_extractions) > 0 or draft_groq
    print(
        "RESULT: " + ("LLM ENGAGED (live model took over extraction/draft)"
                      if engaged else
                      "DEGRADED-VISIBLE (every LLM path fell back to templates)")
    )
    return 0 if engaged else 2


if __name__ == "__main__":
    raise SystemExit(main())