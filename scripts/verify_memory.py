"""Print the memory-layer integrity report.

This is the Step 3 gate evidence and a useful command for the final report:

    .\\.venv\\Scripts\\python.exe scripts\\verify_memory.py

No key material is printed: only the backend in use, the visible fallback
flag, the reconstructed counts and the COUNTS VERIFIED / COUNT MISMATCH status
(SPEC 6).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory import (
    HindsightMemory,
    build_store,
    ingest_seed,
    verify_counts,
)
from app.seed_loader import load_seed


def main() -> int:
    seed = load_seed()
    store = build_store()

    print(f"backend        : {store.name}")
    print(f"bank_id        : {store.bank_id}")
    print(f"fallback       : {store.fallback}")
    if store.fallback:
        print(f"fallback reason: {store.fallback_reason}")
    if store.last_error:
        print(f"last error     : {store.last_error[:160]}")

    print(f"\nseed deals     : {len(seed.deals)}")
    print(f"ingested       : {ingest_seed(store, seed.deals)}")

    records = store.all_records()
    report = verify_counts(records, seed.scoring, seed.expected)

    print(f"\nreconstructed  : {report['deals']} deals")
    print(f"  lost         : {report['lost']}")
    print(f"  won          : {report['won']}")
    print(f"\nintegrity      : {report['integrity_status']}")
    if report["mismatches"]:
        print("mismatches:")
        for line in report["mismatches"]:
            print(f"  - {line}")

    print("\npatterns from memory:")
    expected_keys = {p.key: (p.total, p.lost, p.won) for p in seed.expected.patterns}
    for pattern in report["patterns"]:
        mark = " " if pattern.key in expected_keys else "+"
        want = expected_keys.get(pattern.key)
        suffix = f"   (expected {want[0]}/{want[1]}/{want[2]})" if want else ""
        print(
            f" {mark} {pattern.key:<52} "
            f"{pattern.total:>3}/{pattern.lost:>3}/{pattern.won:>3}{suffix}"
        )
    print("\n(+) not in expected_patterns.json; ground truth covers only the 5 above")

    if isinstance(store, HindsightMemory):
        units = sum(1 for _ in store._iter_units())
        print(
            f"\nraw memory units: {units} for {report['deals']} deals "
            f"(the server splits each retained deal into several units; "
            f"counts are deduped by deal id)"
        )

    store.close()
    return 0 if report["integrity_status"] == "COUNTS VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
