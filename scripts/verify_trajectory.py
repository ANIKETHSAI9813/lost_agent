"""Print the reference score trajectory (SPEC 3 / SPEC 10.4 gate evidence).

    .\\.venv\\Scripts\\python.exe scripts\\verify_trajectory.py

Replays the demo's six turns through the real scoring engine and prints the
score, the delta in points, the signal and the reason line, alongside the
reference values from SPEC 3. Exits non-zero if any level is outside +/-0.02.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory import LocalMemory  # noqa: E402
from app.scoring import ScoringEngine, describe_pattern
from app.seed_loader import RECOVERY_SIGNAL, load_seed

# SPEC 3 reference trajectory, and the delta notes in demo_call.json.
REFERENCE = {0: 0.62, 1: 0.62, 2: 0.566, 3: 0.415, 4: 0.415, 5: 0.299, 6: 0.418}
REFERENCE_DELTA = {2: -5.4, 3: -15.1, 4: 0.0, 5: -11.6, 6: 11.9}
NOTE = {
    0: "prior",
    1: "neutral, no signal",
    2: "competitor Acme",
    3: "budget freeze, fatal -> draft",
    4: "rep turn, no signal",
    5: "champion leaving, fatal -> heads-up",
    6: "positive reply after follow-up",
}
TOLERANCE = 0.02


def main() -> int:
    seed = load_seed()
    store = LocalMemory()
    for deal in seed.deals:
        store.retain(deal)
    engine = ScoringEngine(config=seed.scoring, records=store.all_records())

    plan = {
        2: ("competitor:Acme", "risk"),
        3: ("objection:budget_freeze", "risk"),
        5: ("objection:champion_left", "risk"),
        6: (f"recovery:{RECOVERY_SIGNAL}@budget_freeze", "recovery"),
    }

    print(f"{'turn':<5}{'score':>8}{'ref':>8}{'delta pts':>11}{'ref':>8}  note")
    print("-" * 78)

    scores = {0: engine.score}
    rows = [(0, engine.score, 0.0, "")]
    last = 0
    for turn, (key, direction) in sorted(plan.items()):
        for quiet in range(last + 1, turn):
            scores[quiet] = engine.score
            rows.append((quiet, engine.score, 0.0, ""))
        pattern = engine.pattern_for(key, "Evaluation")
        before = engine.score
        update = engine.apply(key, pattern, direction=direction, event_id=f"t{turn}")
        scores[turn] = engine.score
        rows.append((turn, engine.score, (engine.score - before) * 100.0, key))
        last = turn
    for quiet in range(last + 1, 7):
        scores[quiet] = engine.score
        rows.append((quiet, engine.score, 0.0, ""))

    failures = []
    for turn, score, delta_pts, key in rows:
        ref = REFERENCE[turn]
        ref_delta = REFERENCE_DELTA.get(turn, 0.0)
        flag = "" if abs(score - ref) <= TOLERANCE else "  <-- OUT OF TOLERANCE"
        if flag:
            failures.append((turn, score, ref))
        print(
            f"{turn:<5}{score:>8.3f}{ref:>8.3f}{delta_pts:>11.1f}{ref_delta:>8.1f}  "
            f"{NOTE[turn]}{flag}"
        )

    print("\npatterns that fired (numbers interpolated verbatim):")
    for entry in engine.history:
        print(f"  {entry.signal_key}")
        print(f"    {entry.pattern_text}")
        print(
            f"    confidence {entry.confidence:.2f}"
            f"   {entry.score_before:.3f} -> {entry.score_after:.3f}"
        )

    if failures:
        print("\nFAILED: levels outside +/-0.02")
        for turn, got, want in failures:
            print(f"  turn {turn}: got {got:.4f}, want {want:.3f}")
        return 1

    print("\nOK: reference trajectory reproduced within +/-0.02 on every level")
    return 0


if __name__ == "__main__":
    sys.exit(main())
