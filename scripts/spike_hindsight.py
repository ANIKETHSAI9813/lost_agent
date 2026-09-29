"""Step 3 spike: probe the real Hindsight API before designing around it.

SPEC 0 line 10: never guess method names or server behaviour. This script
answers three questions that decide the memory-layer design:

  1. Does `retain` return 1:1 with what we sent?
  2. Does `list_memories` return our metadata + tags intact, and does
     consolidation (fact_type='observation') create extra/missing rows?
  3. Can `recall` return all 30 deals, or is it truncated by max_tokens?

No key material is printed. Only status, counts and field names.
"""

from __future__ import annotations

import json
import sys
import traceback

from hindsight_client import Hindsight

from app.config import settings
from app.seed_loader import load_seed


def show(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> int:
    if not settings.hindsight_configured:
        print("Hindsight not configured; nothing to spike.")
        return 2

    print(f"base_url : {settings.hindsight_base_url}")
    print(f"bank_id  : {settings.hindsight_bank_id}")
    print(f"api_key  : [present, {len(settings.hindsight_api_key)} chars, not shown]")

    client = Hindsight(
        base_url=settings.hindsight_base_url,
        api_key=settings.hindsight_api_key,
        timeout=120.0,
        max_attempts=2,
    )

    show("1. server version / reachability")
    try:
        version = client.get_version()
        print("OK  get_version ->", json.dumps(version.to_dict(), default=str)[:300])
    except Exception as exc:  # noqa: BLE001
        print("FAIL get_version:", type(exc).__name__, str(exc)[:300])
        return 1

    seed = load_seed()
    probe_deal = seed.deals[0]
    print(f"\nprobe deal: {probe_deal.id} {probe_deal.company} outcome={probe_deal.outcome}")

    show("2. retain ONE probe deal (metadata + tags)")
    metadata = {
        "deal_id": probe_deal.id,
        "outcome": probe_deal.outcome,
        "objection": probe_deal.objections[0],
        "objection_stage": probe_deal.objection_stage,
        "competitors": ",".join(probe_deal.competitors),
        "rep": probe_deal.rep,
        "strategy": (
            probe_deal.winning_response.strategy if probe_deal.winning_response else ""
        ),
        "deal_size": str(probe_deal.deal_size),
        "source": "seed",
        "probe": "1",
    }
    tags = [
        f"deal:{probe_deal.id}",
        f"objection:{probe_deal.objections[0]}",
        f"outcome:{probe_deal.outcome}",
        "source:seed",
        "probe:1",
    ]
    content = (
        f"DEAL {probe_deal.id} | {probe_deal.company} | {probe_deal.outcome} | "
        f"objection {probe_deal.objections[0]} at {probe_deal.objection_stage} | "
        f"competitors {probe_deal.competitors} | rep {probe_deal.rep}"
    )
    try:
        resp = client.retain(
            bank_id=settings.hindsight_bank_id,
            content=content,
            metadata=metadata,
            tags=tags,
            context="spike probe",
        )
        print("OK  retain ->", resp.to_dict() if hasattr(resp, "to_dict") else resp)
    except Exception as exc:  # noqa: BLE001
        print("FAIL retain:", type(exc).__name__, str(exc)[:400])
        traceback.print_exc(limit=2)
        return 1

    show("3. list_memories: does our metadata + tags survive?")
    try:
        page = client.list_memories(bank_id=settings.hindsight_bank_id, limit=100, offset=0)
        print(f"total={page.total} returned={len(page.items)} limit={page.limit} offset={page.offset}")
        for item in page.items:
            print(f"\n  id          : {item.id}")
            print(f"  fact_type   : {item.fact_type}")
            print(f"  state       : {item.state}")
            print(f"  document_id : {item.document_id}")
            print(f"  tags        : {item.tags}")
            print(f"  metadata    : {json.dumps(item.metadata, default=str)[:400] if item.metadata else None}")
            print(f"  text        : {(item.text or '')[:160]!r}")
    except Exception as exc:  # noqa: BLE001
        print("FAIL list_memories:", type(exc).__name__, str(exc)[:400])
        traceback.print_exc(limit=2)
        return 1

    show("4. recall with tag filter (is it truncated?)")
    for max_tokens in (512, 4096, 32768):
        try:
            r = client.recall(
                bank_id=settings.hindsight_bank_id,
                query="lost deals with a budget freeze at Evaluation",
                tags=["probe:1"],
                tags_match="any_strict",
                max_tokens=max_tokens,
            )
            n = len(r.results or [])
            print(f"max_tokens={max_tokens:>6} -> {n} results")
            for item in (r.results or [])[:5]:
                print(f"    - {item.id[:8]} {getattr(item, 'fact_type', '?'):<12} {(item.text or '')[:90]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"max_tokens={max_tokens:>6} -> FAIL {type(exc).__name__}: {str(exc)[:200]}")

    show("5. reflect")
    try:
        ans = client.reflect(
            bank_id=settings.hindsight_bank_id,
            query="What happened in the past deals where a budget freeze appeared at Evaluation?",
            budget="low",
        )
        print("OK  reflect ->", (getattr(ans, "text", "") or "")[:600])
    except Exception as exc:  # noqa: BLE001
        print("FAIL reflect:", type(exc).__name__, str(exc)[:400])

    show("spike complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
