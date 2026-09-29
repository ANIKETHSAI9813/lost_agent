"""D41: score extraction correctness against a hand-labelled corpus.

Loads `tests/data/extraction_cases.json` and runs every case through the same
`extract_turn` the pipeline uses, comparing objections / competitors / recovery
against the expected labels. Exit 0 when every case passes, 1 otherwise
(the testing suite imports `run_corpus` and asserts the same).

Run from the project root:
    .venv/Scripts/python.exe scripts/eval_extraction.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "data" / "extraction_cases.json"

sys.path.insert(0, str(ROOT))

from app.extract import extract_turn  # noqa: E402
from app.seed_loader import load_seed  # noqa: E402

DRAFT_FLAG = {
    "sent": (True, True),
    "exists": (True, False),
    "none": (False, False),
}


def _normalise(value) -> str | None:
    return value if value is None else str(value)


def run_case(seed, case: dict) -> tuple[bool, dict, dict, list[str]]:
    draft_exists, drawer_marked = DRAFT_FLAG.get(case.get("draft"), (False, False))
    signals, ignored = extract_turn(
        text=case["text"],
        speaker=case["speaker"],
        event_type=case["event_type"],
        active_rep="Priya Nair",
        taxonomy=seed.scoring.objection_taxonomy,
        draft_exists=draft_exists,
        draft_marked_sent=drawer_marked,
    )
    expected = {
        "objections": case.get("objection"),
        "competitors": set(case.get("competitors") or []),
        "recovery": case.get("recovery") or [],
    }
    got = {
        "objections": signals.objections[0] if signals.objections else None,
        "competitors": set(signals.competitors),
        "recovery": signals.recovery,
    }
    ok = (
        _normalise(got["objections"]) == _normalise(expected["objections"])
        and got["competitors"] == expected["competitors"]
        and got["recovery"] == expected["recovery"]
    )
    return ok, expected, got, ignored


def run_corpus(corpus_path: Path = CORPUS) -> list[tuple[int, dict, bool, dict, dict, list[str]]]:
    seed = load_seed()
    cases = json.loads(corpus_path.read_text(encoding="utf-8"))["cases"]
    results: list[tuple[int, dict, bool, dict, dict, list[str]]] = []
    for index, case in enumerate(cases, start=1):
        ok, expected, got, ignored = run_case(seed, case)
        results.append((index, case, ok, expected, got, ignored))
    return results


def main() -> int:
    results = run_corpus()
    failures = [r for r in results if not r[2]]
    for index, case, _ok, expected, got, ignored in results:
        mark = "PASS" if _ok else "FAIL"
        print(f"{mark:4s} #{index:02d} [{case.get('kind')}] {case['text'][:64]}")
        if not _ok:
            print(f"      expected obj={expected['objections']} "
                  f"comp={sorted(expected['competitors'])} rec={expected['recovery']}")
            print(f"      got      obj={got['objections']} "
                  f"comp={sorted(got['competitors'])} rec={got['recovery']}")
            print(f"      ignored  {ignored}")
    print(f"\n{len(results) - len(failures)}/{len(results)} extraction cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())