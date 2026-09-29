"""Proof of the integration path (Phase 7): POST a JSON transcript to
`/api/transcript` over plain HTTP, exactly what a Gong/Zoom/SFDC export hook
would do. Uses only the standard library.

Usage:
    python scripts/sample_webhook.py [base_url]

Defaults to http://127.0.0.1:8000 (the local uvicorn from `scripts/run` or the
Start menu). Exit code is 0 when the server replies and the transcript runs
were scored; anything else prints the error and exits non-zero.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# The shape a Gong/Zoom export maps to: {"turns": [{"speaker","text","type","timestamp"}]}.
TRANSCRIPT = {
    "turns": [
        {
            "speaker": "Maria White (Prospect)",
            "text": "The walkthrough looked good, but our security team wants "
            "another security review before anything moves forward.",
            "type": "call",
            "timestamp": "2026-09-29T14:02:00",
        },
        {
            "speaker": "Maria White (Prospect)",
            "text": "Also worth being straight with you: Vantage quoted us a "
            "lower price for something very similar.",
            "type": "call",
            "timestamp": "2026-09-29T14:06:00",
        },
        {
            "speaker": "Priya Nair (Rep)",
            "text": "That's fair. Let me send the security docs we used at two "
            "customers this quarter and a cost plan.",
            "type": "call",
            "timestamp": "2026-09-29T14:09:00",
        },
        {
            "speaker": "Maria White (Prospect)",
            "text": "If you can get the security packet to us by Friday, I can "
            "make time to walk through it with our team.",
            "type": "email",
            "timestamp": "2026-09-29T14:30:00",
        },
    ]
}


def main() -> int:
    request = urllib.request.Request(
        BASE_URL.rstrip("/") + "/api/transcript",
        data=json.dumps(TRANSCRIPT).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach {BASE_URL}: {exc.reason}")
        return 2

    print(f"POST /api/transcript -> {len(payload['turns'])} turns scored")
    for turn in payload["turns"]:
        result = turn["result"]
        delta = result["delta"]
        arrow = "▼" if delta < 0 else "▲" if delta > 0 else "•"
        signals = result["signals"]
        tags = signals["objections"] + [f"vs {c}" for c in signals["competitors"]]
        print(
            f"  {arrow} {result['score'] * 100:5.1f}% ({delta:+5.1f} pts) "
            f"{', '.join(tags) or 'no signal'} — {result['reason'][:80]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())