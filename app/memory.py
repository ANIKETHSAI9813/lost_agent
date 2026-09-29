"""Memory layer (SPEC 6).

Design is driven by what the real Hindsight API was observed to do in
`scripts/spike_hindsight.py` (client 0.10.1, server api_version 0.10.1):

  1. `retain` is ONE-to-MANY. The server LLM decomposes one retained item
     into several memory units, so one 30-deal ingest produces more than 30
     rows. Row count therefore can never be used as a deal count.
  2. `list_memories` is the only paginated, untruncated read path
     (`limit`/`offset`, returns `total`). Every memory unit carries back the
     `metadata` dict and `tags` list we sent, verbatim.
  3. `recall` has NO limit and NO pagination, only `max_tokens`, and it returns
     several near-duplicate facts for the same deal. Counts must never be
     computed from it (SPEC 6: "Counts must NOT be computed from a truncated
     list").
  4. `reflect` is a narrative generator, not a data source: in the spike it
     reported a close date of 2026-09-28 (the day of the run) for a deal whose
     real `closed_date` is different. It is display-only; computed counts win
     (SPEC 6).

So the contract is:

  * `all_records()` returns deduplicated `Deal` records reconstructed from
    memory metadata. Counts and patterns are computed from THESE, in code.
  * `recall_similar()` applies SPEC 5.3's deterministic filter in code over the
    same record set, so both backends return identical lists.
  * `reflect_narrative()` is optional, display-only, never used for numbers.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable

from .config import settings
from .models import Deal, ExpectedPatterns, Pattern, PatternExpectation, RecalledDeal, ScoringConfig, WinningResponse
from .seed_loader import (
    COMBINED_KEY,
    RECOVERY_SIGNAL,
    compute_pattern_counts,
    pattern_index,
)

log = logging.getLogger(__name__)

# Tag written on every ingest so records can be identified independently of
# however the server chose to split the text.
SCHEMA_TAG = "ingest:v1"

# SPEC 6: deals added through POST /api/retain are tagged source=live_added.
SOURCE_SEED = "seed"
SOURCE_LIVE = "live_added"


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MemoryRecord:
    """One deal as stored in memory."""

    deal: Deal
    source: str = SOURCE_SEED

    @property
    def id(self) -> str:
        return self.deal.id


# --------------------------------------------------------------------------
# Deterministic rendering + normalization (SPEC 6)
# --------------------------------------------------------------------------
def render_deal_text(deal: Deal, source: str = SOURCE_SEED) -> str:
    """Deterministic text rendering of a deal.

    The server rewrites retained text, so this string is for human/semantic
    readability only. Exact field values travel in `metadata`, which came back
    verbatim in the spike.
    """
    lines = [
        f"DEAL {deal.id} -- {deal.company} ({deal.industry})",
        f"Outcome: {deal.outcome}. Size: {deal.deal_size}.",
        f"Objection: {deal.objection} raised at the {deal.objection_stage} stage.",
        f"Stage lost at: {deal.stage_lost_at or 'n/a (won)'}. Closed: {deal.closed_date}.",
        f"Rep: {deal.rep}. Competitors: {', '.join(deal.competitors) or 'none'}.",
        f"Stakeholders in the room: {', '.join(deal.stakeholders_in_room) or 'none'}.",
        f"Summary: {deal.summary}",
    ]
    if deal.winning_response is not None:
        wr = deal.winning_response
        lines.append(
            f"Recovered with strategy '{wr.strategy}': {wr.text}"
        )
    else:
        lines.append("No winning response recorded; the deal was lost.")
    if deal.recovery_signals:
        lines.append(f"Recovery signals: {', '.join(deal.recovery_signals)}.")
    lines.append(f"Source: {source}.")
    return "\n".join(lines)


def _csv(values: Iterable[str]) -> str:
    return ",".join(v for v in values if v)


def deal_metadata(deal: Deal, source: str = SOURCE_SEED) -> dict[str, str]:
    """Normalized fields, all strings (the API types metadata as dict[str, str]).

    Every field that can affect a count, a pattern, a draft citation or the
    SPEC 5.3 ordering is included, so a full `Deal` can be rebuilt from memory
    without touching `seed/*.json`.
    """
    return {
        "schema": SCHEMA_TAG,
        "deal_id": deal.id,
        "company": deal.company,
        "industry": deal.industry,
        "outcome": deal.outcome,
        "objection": deal.objection or "",
        "objection_stage": deal.objection_stage,
        "stage_lost_at": deal.stage_lost_at or "",
        "closed_date": deal.closed_date,
        "rep": deal.rep,
        "competitors": _csv(deal.competitors),
        "stakeholders": _csv(deal.stakeholders_in_room),
        "deal_size": str(deal.deal_size),
        "summary": deal.summary,
        "recovery_signals": _csv(deal.recovery_signals),
        "winning_strategy": deal.winning_response.strategy if deal.winning_response else "",
        "winning_text": deal.winning_response.text if deal.winning_response else "",
        "source": source,
    }


def deal_tags(deal: Deal, source: str = SOURCE_SEED) -> list[str]:
    """Tags for recall filtering (SPEC 6: "so recall can filter on them")."""
    tags = [
        SCHEMA_TAG,
        f"deal:{deal.id}",
        f"outcome:{deal.outcome}",
        f"source:{source}",
    ]
    if deal.objection:
        tags.append(f"objection:{deal.objection}")
    tags.append(f"stage:{deal.objection_stage}")
    for comp in deal.competitors:
        tags.append(f"competitor:{comp}")
    for sig in deal.recovery_signals:
        tags.append(f"recovery:{sig}")
    return tags


def _split(value: str | None) -> list[str]:
    return [part for part in (value or "").split(",") if part]


def record_from_metadata(
    metadata: dict[str, Any] | None, fallback_text: str = ""
) -> MemoryRecord | None:
    """Rebuild a `MemoryRecord` from a memory unit's metadata.

    Returns None when the unit is not a deal record (e.g. a consolidated
    observation), so unrelated units never inflate the counts.
    """
    if not metadata:
        return None
    deal_id = str(metadata.get("deal_id") or "").strip()
    objection = str(metadata.get("objection") or "").strip()
    if not deal_id or not objection:
        return None

    source = str(metadata.get("source") or SOURCE_SEED)
    strategy = str(metadata.get("winning_strategy") or "").strip()
    winning_text = str(metadata.get("winning_text") or "").strip()

    try:
        deal_size = int(str(metadata.get("deal_size") or "0"))
    except ValueError:
        deal_size = 0

    deal = Deal(
        id=deal_id,
        company=str(metadata.get("company") or ""),
        industry=str(metadata.get("industry") or ""),
        deal_size=deal_size,
        outcome=str(metadata.get("outcome") or "lost"),
        objection_stage=str(metadata.get("objection_stage") or ""),
        stage_lost_at=str(metadata.get("stage_lost_at") or "") or None,
        closed_date=str(metadata.get("closed_date") or ""),
        rep=str(metadata.get("rep") or ""),
        competitors=_split(str(metadata.get("competitors") or "")),
        objections=[objection],
        stakeholders_in_room=_split(str(metadata.get("stakeholders") or "")),
        summary=str(metadata.get("summary") or fallback_text),
        recovery_signals=_split(str(metadata.get("recovery_signals") or "")),
        winning_response=(
            WinningResponse(
                objection=objection, strategy=strategy, text=winning_text
            )
            if strategy and winning_text
            else None
        ),
    )
    return MemoryRecord(deal=deal, source=source)


# --------------------------------------------------------------------------
# Deterministic similarity (SPEC 5.3)
# --------------------------------------------------------------------------
def rank_similar(
    records: Iterable[MemoryRecord],
    *,
    objection: str | None,
    competitors: list[str],
    stage: str,
    deal_size: int,
    active_rep: str,
    limit: int = 5,
) -> list[RecalledDeal]:
    """Same objection and/or competitor, same stage, then size proximity.

    Order is fully deterministic: descending score, then `closed_date`
    descending, then deal id ascending. Implemented in code rather than left to
    the backend so that switching MEMORY_BACKEND cannot change the list, and so
    that a new rep and a veteran see the same thing (SPEC 11).
    """
    wanted = {c for c in competitors if c}
    scored: list[tuple[float, str, str, MemoryRecord]] = []

    for record in records:
        deal = record.deal
        same_objection = bool(objection) and deal.objection == objection
        shared = wanted.intersection(deal.competitors)
        if not same_objection and not shared:
            continue  # not similar on either axis

        score = 0.0
        if same_objection:
            score += 3.0
        if shared:
            score += 2.0 + float(len(shared) - 1)
        if stage and deal.objection_stage == stage:
            score += 2.0
        if deal.deal_size > 0 and deal_size > 0:
            high = max(deal.deal_size, deal_size)
            low = min(deal.deal_size, deal_size)
            score += 2.0 * (low / high)

        scored.append((score, deal.closed_date, deal.id, record))

    scored.sort(key=lambda row: (-row[0], _date_rank(row[1]), row[2]))
    return [
        RecalledDeal(
            id=record.deal.id,
            company=record.deal.company,
            outcome=record.deal.outcome,
            rep=record.deal.rep,
            worked_by_you=record.deal.rep == active_rep,
            objection=record.deal.objection,
            objection_stage=record.deal.objection_stage,
            deal_size=record.deal.deal_size,
            closed_date=record.deal.closed_date,
        )
        for _score, _date, _id, record in scored[:limit]
    ]


def _date_rank(value: str) -> float:
    """Sort key giving `closed_date` DESCENDING order, undated deals last.

    A single ascending key cannot mix directions, so the digits are negated:
    "-20260101" sorts before "-20250101", i.e. the newer ISO date wins. Undated
    records get +inf so they fall to the end rather than the front.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return float("inf")
    return -float(digits)


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class MemoryStore(abc.ABC):
    """SPEC 6: interface with retain(), recall(), reflect()."""

    name: str = "abstract"
    bank_id: str = settings.hindsight_bank_id
    #: True whenever Hindsight was requested but is not actually serving reads.
    fallback: bool = True
    fallback_reason: str = ""
    last_error: str = ""

    @abc.abstractmethod
    def retain(self, deal: Deal, source: str = SOURCE_SEED) -> None:
        """Store one deal."""

    @abc.abstractmethod
    def all_records(self) -> list[MemoryRecord]:
        """Deduplicated deal records, reconstructed from memory. Never truncated."""

    @abc.abstractmethod
    def recall_similar(
        self,
        *,
        objection: str | None,
        competitors: list[str],
        stage: str,
        deal_size: int,
        active_rep: str,
        limit: int = 5,
    ) -> list[RecalledDeal]:
        """SPEC 5.3 similar past deals."""

    def reflect_narrative(self, query: str) -> str | None:
        """Optional, display-only narrative. Never a source of numbers."""
        return None

    def semantic_candidates(self, query: str, max_tokens: int = 2048) -> list[str]:
        """Deal ids surfaced by semantic recall. Informational only; defaults
        to no semantic arm so both backends stay comparable."""
        return []

    def forget_live_added(self) -> int:
        """Drop every `live_added` deal for a clean demo start.

        Returns the number of deal ids retired. The local backend removes the
        records; Hindsight invalidates its world/experience units and keeps an
        in-process filter for any derived observation that cannot be curated.
        """
        return 0

    def stale_deal_ids(self) -> set[str]:
        """Deal ids already present but stored under an older metadata schema.

        An id being present is not enough to skip a re-ingest: the spike wrote
        a reduced metadata set for D001, so a "does this id exist?" check would
        have kept the incomplete record forever.
        """
        return set()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Local fallback
# --------------------------------------------------------------------------
class LocalMemory(MemoryStore):
    """In-process deterministic store. SPEC 6: fallback only, and VISIBLE.

    It performs no network calls and needs no credentials, so the whole demo
    works with no API keys (SPEC 12).
    """

    name = "local"
    bank_id = "(in-process)"

    def __init__(self, reason: str = "") -> None:
        self.fallback = True
        self.fallback_reason = reason or "MEMORY_BACKEND=local"
        self.last_error = ""
        self._records: dict[str, MemoryRecord] = {}

    def retain(self, deal: Deal, source: str = SOURCE_SEED) -> None:
        self._records[deal.id] = MemoryRecord(deal=deal, source=source)

    def all_records(self) -> list[MemoryRecord]:
        return [self._records[k] for k in sorted(self._records)]

    def recall_similar(
        self,
        *,
        objection: str | None,
        competitors: list[str],
        stage: str,
        deal_size: int,
        active_rep: str,
        limit: int = 5,
    ) -> list[RecalledDeal]:
        return rank_similar(
            self.all_records(),
            objection=objection,
            competitors=competitors,
            stage=stage,
            deal_size=deal_size,
            active_rep=active_rep,
            limit=limit,
        )

    def reflect_narrative(self, query: str) -> str | None:
        return None  # no LLM narrative without a backend; counts stand alone

    def forget_live_added(self) -> int:
        retired = [k for k, r in self._records.items() if r.source == SOURCE_LIVE]
        for key in retired:
            del self._records[key]
        return len(retired)


# --------------------------------------------------------------------------
# Hindsight
# --------------------------------------------------------------------------
def _drive(coro: Any) -> Any:
    """Run a coroutine on the CURRENT thread's cached event loop.

    `hindsight_client._run_async` does exactly this, but the app needs the
    same guarantee for its own asyncio-driven calls (e.g. the curated invalidation
    in `forget_live_added`): `asyncio.run()` spawns a brand-new loop, and aiohttp
    refuses to reuse a connector created under a different loop.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class _ThreadLocalHindsight:
    """Give every OS thread its own Hindsight client.

    The generated client shares ONE aiohttp connector, which is bound to the
    event loop of whichever thread created it. Uvicorn dispatches sync
    endpoints to its threadpool, and each pool thread has its own loop, so a
    client shared across threads fails with aiohttp's ``RuntimeError: Timeout
    context manager should be used inside a task`` on anything but the thread
    that created it. One client per thread keeps every call on the loop the
    connector belongs to; the thread-local loop is cached by
    ``set_event_loop``, so all calls in a thread stay on the SAME loop.
    """

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._created: list[Any] = []
        self._local = threading.local()

    def _get(self) -> Any:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._factory()
            self._local.client = client
            with self._lock:
                self._created.append(client)
        return client

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._get(), name)

    def close(self) -> None:
        with self._lock:
            created = list(self._created)
            self._created.clear()
        for client in created:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


class HindsightMemory(MemoryStore):
    """Real Hindsight backend.

    Writes go through `retain_batch`; reads for counting go through paginated
    `list_memories` and are deduplicated by `metadata.deal_id`, because the
    server stores one retained item as several memory units.
    """

    name = "hindsight"
    fallback = False

    PAGE_SIZE = 100

    def __init__(self, client: Any, bank_id: str) -> None:
        self._client = client
        self.bank_id = bank_id
        self.fallback_reason = ""
        self.last_error = ""
        self._page_calls = 0
        # Deal ids this process has explicitly retired (clean demo start).
        # Kept in-process because observations derived from a retired deal
        # cannot be curated server-side and would otherwise resurrect it.
        self._retired_ids: set[str] = set()

    # -- lifecycle -------------------------------------------------------
    def ping(self) -> bool:
        try:
            self._client.get_version()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # -- writes ----------------------------------------------------------
    def retain(self, deal: Deal, source: str = SOURCE_SEED) -> None:
        self.retain_many([deal], source=source)

    def retain_many(self, deals: list[Deal], source: str = SOURCE_SEED) -> None:
        if not deals:
            return
        items = [
            {
                "content": render_deal_text(deal, source),
                "metadata": deal_metadata(deal, source),
                "tags": deal_tags(deal, source),
                "context": f"Past {deal.outcome} deal {deal.id} ({source})",
            }
            for deal in deals
        ]
        try:
            self._client.retain_batch(
                bank_id=self.bank_id, items=items, retain_async=False
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise

    # -- reads -----------------------------------------------------------
    def _iter_units(self) -> Iterable[Any]:
        offset = 0
        total: int | None = None
        while True:
            page = self._client.list_memories(
                bank_id=self.bank_id, limit=self.PAGE_SIZE, offset=offset
            )
            self._page_calls += 1
            total = int(getattr(page, "total", 0) or 0)
            items = list(getattr(page, "items", []) or [])
            if not items:
                return
            for item in items:
                yield item
            offset += len(items)
            if total and offset >= total:
                return

    def all_records(self) -> list[MemoryRecord]:
        """Reconstruct every stored deal, one record per deal id.

        `state == 'valid'` skips units the server invalidated; consolidation
        (`fact_type == 'observation'`) is handled by preferring whichever unit
        for a deal id carries the most metadata, since consolidation can
        produce an observation with partial metadata.
        """
        best: dict[str, MemoryRecord] = {}
        completeness: dict[str, int] = {}
        for item in self._iter_units():
            if str(getattr(item, "state", "valid") or "valid") != "valid":
                continue
            metadata = getattr(item, "metadata", None)
            record = record_from_metadata(metadata, getattr(item, "text", "") or "")
            if record is None:
                continue
            if record.id in self._retired_ids:
                continue
            filled = sum(1 for v in (metadata or {}).values() if str(v).strip())
            if record.id not in best or filled > completeness[record.id]:
                best[record.id] = record
                completeness[record.id] = filled
        return [best[k] for k in sorted(best)]

    def recall_similar(
        self,
        *,
        objection: str | None,
        competitors: list[str],
        stage: str,
        deal_size: int,
        active_rep: str,
        limit: int = 5,
    ) -> list[RecalledDeal]:
        """Deterministic SPEC 5.3 ranking over the exact record set.

        Hindsight's semantic `recall` is deliberately NOT used to build this
        list: it has no limit or pagination and returns several near-duplicate
        facts per deal (measured in the spike), so it cannot produce a stable
        top-5. Semantic search is still exercised for the display-only
        reflection narrative.
        """
        return rank_similar(
            self.all_records(),
            objection=objection,
            competitors=competitors,
            stage=stage,
            deal_size=deal_size,
            active_rep=active_rep,
            limit=limit,
        )

    def semantic_candidates(self, query: str, max_tokens: int = 2048) -> list[str]:
        """Deal ids surfaced by semantic recall. Informational only."""
        try:
            response = self._client.recall(
                bank_id=self.bank_id,
                query=query,
                tags=[SCHEMA_TAG],
                tags_match="any_strict",
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        found: list[str] = []
        for result in getattr(response, "results", []) or []:
            metadata = getattr(result, "metadata", None) or {}
            deal_id = str(metadata.get("deal_id") or "").strip()
            if deal_id and deal_id not in found:
                found.append(deal_id)
        return found

    def stale_deal_ids(self) -> set[str]:
        """Ids whose stored metadata predates the current schema tag."""
        stale: set[str] = set()
        for item in self._iter_units():
            metadata = getattr(item, "metadata", None) or {}
            deal_id = str(metadata.get("deal_id") or "").strip()
            if deal_id and str(metadata.get("schema") or "") != SCHEMA_TAG:
                stale.add(deal_id)
        return stale

    def reflect_narrative(self, query: str) -> str | None:
        try:
            answer = self._client.reflect(
                bank_id=self.bank_id, query=query, budget="low"
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("hindsight reflect failed: %s", self.last_error)
            return None
        return getattr(answer, "text", None) or None

    def forget_live_added(self) -> int:
        """Retire every `live_added` deal so a fresh run starts from the seed.

        World/experience units are invalidated server-side via the curated
        PATCH endpoint (`state="invalidated"`); derived observations cannot be
        curated, so their deal ids are retired in-process as a second gate.
        Values are never echoed; failures keep the in-process filter so a deal
        still retires even when the server call fails.
        """
        retired: set[str] = set()
        update_error = False
        for item in self._iter_units():
            if str(getattr(item, "state", "valid") or "valid") != "valid":
                continue
            metadata = getattr(item, "metadata", None) or {}
            if str(metadata.get("source") or "") != SOURCE_LIVE:
                continue
            deal_id = str(metadata.get("deal_id") or "").strip()
            unit_id = str(getattr(item, "id", "") or "").strip()
            fact_type = str(getattr(item, "fact_type", "") or "").lower()
            if unit_id and fact_type in ("world", "experience"):
                try:
                    from hindsight_client_api.models import UpdateMemoryRequest

                    coro = self._client.memory.update_memory(
                        bank_id=self.bank_id,
                        memory_id=unit_id,
                        update_memory_request=UpdateMemoryRequest(
                            state="invalidated", reason="reset_live"
                        ),
                    )
                    # Driven on the current thread's cached loop: the aiohttp
                    # connector this thread's client uses lives on that loop.
                    _drive(coro)
                except Exception as exc:  # noqa: BLE001
                    update_error = True
                    self.last_error = f"{type(exc).__name__}: {exc}"
                if deal_id:
                    retired.add(deal_id)
            elif fact_type == "observation" and deal_id:
                retired.add(deal_id)
            elif deal_id:
                # Unknown fact type: still retire so the memory story stays clean.
                retired.add(deal_id)
        self._retired_ids.update(retired)
        if update_error:
            log.warning("hindsight forget_live_added partial failure; in-process filter active")
        return len(retired)


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
def build_store(
    *, force_local: bool = False, allow_network: bool = True
) -> MemoryStore:
    """Return the configured store, falling back visibly (SPEC 6).

    The badge must never claim Hindsight unless a real call succeeded, so the
    client is pinged here and any failure becomes a LocalMemory with a reason.
    """
    if force_local:
        return LocalMemory("forced local (testing)")

    if settings.memory_backend != "hindsight":
        return LocalMemory(f"MEMORY_BACKEND={settings.memory_backend}")

    if not settings.hindsight_configured:
        return LocalMemory(
            "HINDSIGHT_API_KEY and/or HINDSIGHT_BASE_URL not set"
        )

    if not allow_network:
        return LocalMemory("network disabled (testing)")

    try:
        from hindsight_client import Hindsight
    except ImportError as exc:
        return LocalMemory(f"hindsight-client not installed ({exc})")

    try:
        client = _ThreadLocalHindsight(
            lambda: Hindsight(
                base_url=settings.hindsight_base_url,
                api_key=settings.hindsight_api_key,
                timeout=120.0,
                max_attempts=2,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return LocalMemory(f"could not build Hindsight client: {type(exc).__name__}")

    store = HindsightMemory(client, settings.hindsight_bank_id)
    if not store.ping():
        reason = f"Hindsight unreachable: {store.last_error[:200]}"
        store.close()
        return LocalMemory(reason)
    return store


# --------------------------------------------------------------------------
# Integrity (SPEC 6)
# --------------------------------------------------------------------------
def verify_counts(
    records: list[MemoryRecord], scoring: ScoringConfig, expected: ExpectedPatterns
) -> dict[str, Any]:
    """Reconstruct counts from memory and compare with expected_patterns.json.

    SPEC 6: show `COUNTS VERIFIED` or `COUNT MISMATCH`, and never silently
    continue on a mismatch.
    """
    deals = [r.deal for r in records]
    computed = pattern_index(compute_pattern_counts(deals, scoring))
    lost = sum(1 for d in deals if d.is_lost)
    won = len(deals) - lost
    mismatches: list[str] = []

    if len(deals) != expected.totals.deals:
        mismatches.append(
            f"total deals: expected {expected.totals.deals}, found {len(deals)}"
        )
    if lost != expected.totals.lost:
        mismatches.append(
            f"lost deals: expected {expected.totals.lost}, found {lost}"
        )
    if won != expected.totals.won:
        mismatches.append(f"won deals: expected {expected.totals.won}, found {won}")

    for want in expected.patterns:
        got = computed.get(want.key)
        if got is None:
            mismatches.append(
                f"{want.key}: missing from memory "
                f"(expected {want.total}/{want.lost}/{want.won})"
            )
            continue
        if (got.total, got.lost, got.won) != (want.total, want.lost, want.won):
            mismatches.append(
                f"{want.key}: expected {want.total}/{want.lost}/{want.won}, "
                f"found {got.total}/{got.lost}/{got.won}"
            )

    return {
        "integrity_status": "COUNT MISMATCH" if mismatches else "COUNTS VERIFIED",
        "deals": len(deals),
        "lost": lost,
        "won": won,
        "patterns": [
            PatternExpectation(key=p.key, total=p.total, lost=p.lost, won=p.won)
            for p in sorted(computed.values(), key=lambda p: p.key)
        ],
        "mismatches": mismatches,
    }


# --------------------------------------------------------------------------
# Ingest (SPEC 5.1)
# --------------------------------------------------------------------------
def ingest_seed(store: MemoryStore, deals: list[Deal], batch_size: int = 10) -> int:
    """Ingest the seed deals, idempotently. Returns the record count.

    A deal is retained when it is missing from memory OR present under an older
    metadata schema, so a partial earlier ingest gets completed rather than
    trusted. Records are deduplicated by deal id on read, so re-retaining is
    safe.
    """
    existing = {r.id for r in store.all_records()}
    stale = store.stale_deal_ids()
    todo = [d for d in deals if d.id not in existing or d.id in stale]

    if not todo:
        return len(existing)

    log.info(
        "ingesting %d deal(s) into %s (%d already current, %d stale schema)",
        len(todo),
        store.name,
        len(existing) - len(stale & existing),
        len(stale & existing),
    )
    if isinstance(store, HindsightMemory):
        for start in range(0, len(todo), batch_size):
            store.retain_many(todo[start : start + batch_size], source=SOURCE_SEED)
    else:
        for deal in todo:
            store.retain(deal, source=SOURCE_SEED)
    return len(store.all_records())
