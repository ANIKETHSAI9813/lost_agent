"""End-to-end smoke over a real uvicorn (Phase 8 final gate).

Spawns `uvicorn app.main:app` on port 8001 (keyless: the two API-key env vars
are scrubbed so the local fallback + templates are provably sufficient), plays
the six-turn call, closes the deal as lost, resets, replays, verifies the
post-close pattern fires as "7 of 11", then runs one pasted transcript via
`/api/transcript` to prove the Phase 7 path over HTTP. Exit 0 = everything
above reproduced; anything else exits non-zero with the failing line.

Run from the project root:
    python scripts/smoke_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parents[1]
REFERENCE = {1: 0.62, 2: 0.566, 3: 0.415, 4: 0.415, 5: 0.299, 6: 0.418}
TOLERANCE = 0.02
FAILURES = []

KEYLESS_ENV = dict(os.environ)
KEYLESS_ENV["MEMORY_BACKEND"] = "local"
KEYLESS_ENV["HINDSIGHT_API_KEY"] = ""
KEYLESS_ENV["GROQ_API_KEY"] = ""


def req(method: str, path: str, body=None) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(
        BASE + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        FAILURES.append(label)


def wait_until_healthy(proc) -> bool:
    for _ in range(60):
        if proc.poll() is not None:
            return False
        try:
            req("GET", "/api/health")
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    return False


def play_six(expect_reference: bool = True) -> list[dict]:
    turns = []
    for expected in range(1, 7):
        payload = req("POST", "/api/demo/step")
        assert payload is not None
        turns.append(payload)
        if expect_reference:
            check(
                f"turn {expected} score",
                abs(payload["score"] - REFERENCE[expected]) <= TOLERANCE,
                f"(got {payload['score']:.3f}, want {REFERENCE[expected]})",
            )
    return turns


def main() -> int:
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=KEYLESS_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_until_healthy(proc):
            log = proc.stdout.read().decode("utf-8", errors="replace")
            print(f"uvicorn did not come up:\n{log}")
            return 1

        print("demo: six-turn call (keyless)")
        turns = play_six()
        check(
            "turn 3 drafted a follow-up",
            turns[2].get("draft_followup") is not None,
        )
        check("turn 6 recovery credited", turns[5]["delta"] >= 5.0)

        print("close as lost")
        closed = req(
            "POST",
            "/api/deals/close",
            {"outcome": "lost", "stage": "Evaluation", "note": "smoke close"},
        )
        check("close 200 + autopsy", closed is not None and "autopsy" in closed)
        after = (closed or {}).get("pattern_after") or {}
        check("pattern now 7 lost of 11", after.get("lost") == 7 and after.get("total") == 11)

        print("reset + replay")
        req("POST", "/api/demo/reset")
        replay = play_six(expect_reference=False)
        reason = replay[2]["reason"]
        check("replay turn 3 warns '7 of 11'", "7 of 11" in reason, f"(reason: {reason})")
        # The point of the close-the-loop run: replaying AFTER a lost close has to
        # score HARDER (the pattern grew to 7-of-11), not reproduce the fresh seed.
        check(
            "replay drops harder than the fresh run",
            replay[2]["score"] < REFERENCE[3] - TOLERANCE,
            f"(got {replay[2]['score']:.3f}, fresh run {REFERENCE[3]})",
        )
        check("replay turn 6 still recovers", replay[5]["delta"] >= 5.0)

        print("transcript import over HTTP")
        imported = req(
            "POST",
            "/api/transcript",
            {
                "turns": [
                    {
                        "speaker": "Maria White (Prospect)",
                        "text": "Our security team wants another security review.",
                        "type": "call",
                    },
                    {
                        "speaker": "Maria White (Prospect)",
                        "text": "Also, Vantage quoted us a lower price.",
                        "type": "call",
                    },
                ]
            },
        )
        check("transcript 2 turns scored", bool(imported) and imported.get("turns_imported") == 2)
        signal_tags = imported["turns"][0]["result"]["signals"]["objections"] if imported else []
        check("security review caught", "security_review_stall" in signal_tags)

        if FAILURES:
            print("\nfailed:", ", ".join(FAILURES))
            return 1
        print("\nSMOKE_E2E_OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())