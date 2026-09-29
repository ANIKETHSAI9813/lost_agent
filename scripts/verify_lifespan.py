"""Run the app lifespan inside a real running event loop.

This is the exact shape that broke: `hindsight_client._run_async` calls
`loop.run_until_complete()`, so any Hindsight call made from a coroutine that
owns the loop raises "This event loop is already running". Offline it used to
fall back to local, which looked fine (same 30/22/8 counts) and hid the bug.

Usage:
    python scripts/verify_lifespan.py

Exits 0 only when the live backend is actually in use, never the local fallback.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402


async def main() -> int:
    # Inspect the shared store *inside* the lifespan context, before shutdown
    # clears the module global in its finally block.
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))

        import app.main as main_module
        from app.pipeline import LiveSession

        store = main_module._store
        if store is None:
            print("FAIL: lifespan did not install the shared store")
            return 1
        seed = main_module._startup_seed
        if seed is None:
            print("FAIL: lifespan did not load the seed")
            return 1
        # The stats probe reads every record, so it must run on a worker thread
        # exactly like the startup path (`_build_store_and_ingest`), never on
        # the loop thread.
        import anyio  # noqa: PLC0415

        stats = await anyio.to_thread.run_sync(
            lambda: LiveSession(store=store, seed=seed).memory_stats()
        )
        result = await report(stats)

    await asyncio.sleep(0)  # let aiohttp's close task finish before teardown
    return result


async def report(stats) -> int:
    print(
        json.dumps(
            {
                "backend": stats.backend,
                "fallback": stats.fallback,
                "fallback_reason": stats.fallback_reason,
                "integrity_status": stats.integrity_status,
                "deals": stats.deals,
                "lost": stats.lost,
                "won": stats.won,
            },
            indent=2,
        )
    )

    if "event loop is already running" in (stats.fallback_reason or ""):
        print("FAIL: Hindsight was called from the event loop thread")
        return 1
    if stats.fallback:
        print(f"NOTE: running on the local fallback ({stats.fallback_reason})")
        return 2
    print("PASS: lifespan started on the live backend, no event loop error")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
