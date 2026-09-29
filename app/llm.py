"""Optional Groq layer (SPEC 7, SPEC 10.7).

The LLM only phrases. All numbers, deal ids, reps and winning-response content
are computed in code and passed in; every LLM output is validated against them.
Any API error, retry failure, invalid JSON or validation failure degrades to the
deterministic template path and never raises to a caller. With no `GROQ_API_KEY`
set, none of this code runs at all and the demo is fully template-driven
(SPEC 12 / SPEC 10.7 gate).
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from .config import KNOWN_COMPETITORS, settings
from .draft import DraftFollowup, validate_draft
from .models import ExtractedSignals  # noqa: F401  (re-exported for tests)

# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
# The cached client instance and the factory function below share the
# `_client` name only in intent: `_client_cache` holds the built instance,
# `_client()` builds/returns it. (The original code reused one name for both,
# so `reset_client()` set the *function* to None and the LLM never worked
# live -- D32.)
_client_cache = None


def _client():
    """Lazily built Groq client. None when disabled or unbuildable."""
    global _client_cache
    if _client_cache is None and settings.llm_enabled:
        try:
            import groq  # imported only when a key exists

            _client_cache = groq.Groq(
                api_key=settings.groq_api_key, base_url=settings.groq_base_url
            )
        except Exception:
            _client_cache = None
    return _client_cache


def reset_client() -> None:
    """Tests replace the injected client; production never needs this."""
    global _client_cache
    _client_cache = None


def _chat(prompt: str, *, json_mode: bool = True) -> str | None:
    """One call, with a single retry (SPEC 7). None on any failure.

    A JSON-object response shape is requested when `json_mode`; callers still
    parse defensively because the model may ignore it.
    """
    client = _client()
    if client is None:
        return None
    messages = [
        {
            "role": "system",
            "content": (
                "You are part of a sales-deal memory system. You only rephrase "
                "or structure facts you are given; you never invent numbers, "
                "deal ids, dates, prices or names."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    for attempt in (1, 2):  # retry once (SPEC 7)
        try:
            kwargs: dict[str, Any] = {
                "model": settings.groq_model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 500,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except Exception:
            if attempt == 2:
                return None
    return None  # pragma: no cover - unreachable, kept for clarity


# --------------------------------------------------------------------------
# EXTRACT (SPEC 5.2): strict JSON, validated against the taxonomy
# --------------------------------------------------------------------------
EXTRACT_SYSTEM = (
    "Extract sales signals from the buyer's message. Reply with STRICT JSON "
    'only, exactly this shape: {{"objections": [...], "competitors": [...]}}.\n'
    "- objections tags must come from the taxonomy below.\n"
    "- competitor names must be from: {competitors}.\n"
    "- Simply naming a competitor IS the cue for that competitor.\n"
    "- This domain records exactly one objection per deal: pick the single "
    "strongest objection, never two.\n"
    "- If neither list applies, return empty lists.\n"
    "Taxonomy: {taxonomy}"
)


def candidate_signals(
    *, text: str, taxonomy: Sequence[str]
) -> tuple[tuple[str | None, list[str]] | None, str]:
    """Ask the LLM for objection/competitor candidates.

    Returns `(candidates, note)`. `candidates` is `(objection, competitors)`
    when the LLM answered with valid, taxonomically-clean tags, else `None`,
    which tells the caller to use the keyword path. The note explains any
    degradation so it is VISIBLE, and candidates never include an unknown tag.
    """
    try:
        raw = _chat(
            EXTRACT_SYSTEM.format(
                taxonomy=", ".join(taxonomy),
                competitors=", ".join(KNOWN_COMPETITORS),
            )
            + "\n\nMessage:\n"
            + text
        )
    except Exception:
        return None, "LLM extraction failed; keyword fallback used"
    if not raw:
        return None, "LLM extraction returned nothing; keyword fallback used"

    try:
        payload = json.loads(raw)
        objection_tags = payload.get("objections") or []
        competitor_tags = payload.get("competitors") or []
        if not isinstance(objection_tags, list) or not isinstance(
            competitor_tags, list
        ):
            raise ValueError("unexpected shape")
    except Exception:
        return None, "LLM extraction was not strict JSON; keyword fallback used"

    unknown = [tag for tag in objection_tags if tag not in taxonomy]
    known = [tag for tag in objection_tags if tag in taxonomy]
    if unknown:
        # SPEC 5.2: reject unknown tags. All-unknown means the answer is
        # useless; a mix is still suspect, so treat it as a failure.
        return None, (
            f"LLM extraction rejected unknown objection tag(s): "
            f"{', '.join(sorted(set(unknown)))}; keyword fallback used"
        )

    competitors = [c for c in competitor_tags if c in KNOWN_COMPETITORS]
    discarded = [c for c in competitor_tags if c not in KNOWN_COMPETITORS]
    note = (
        f"LLM discarded unknown competitor(s): {', '.join(discarded)}"
        if discarded
        else ""
    )
    # At most one objection (the domain models exactly one per deal).
    objection = known[0] if known else None
    if len(known) > 1:
        note = (
            note + f"; kept only '{objection}' across {len(known)} objection candidates"
        ).strip()
    return (objection, competitors), note


# --------------------------------------------------------------------------
# EXPLAIN (SPEC 5.6 + SPEC 7): may rephrase, must keep the exact counts
# --------------------------------------------------------------------------
REASON_RULES = (
    "Rephrase this one-line sales warning in fresh words. Hard rules:\n"
    '- Keep the exact count phrase "{lost} of {total}" unchanged.\n'
    '- Keep the point drop/recovery number "{pts}" unchanged.\n'
    "- Do not add any fact, number, deal id or date that is not already in the "
    "line.\n"
    "- Reply with one sentence only."
)


def phrase_reason(*, template_reason: str, pattern_text: str) -> str | None:
    """LLM rephrase of the reason line, validated to keep the exact numbers.

    `pattern_text` is the computed line carrying the truthful counts (e.g.
    "5 of 6 past champion_left deals at Evaluation were lost."); the rephrase
    must keep that "N of M" phrase and the pts figure, or it is discarded and
    the template reason is used.
    """
    numbers = re.findall(r"\d+", pattern_text)
    if len(numbers) < 2:
        return None
    lost, total = numbers[0], numbers[1]
    pts_hit = re.search(r"([0-9]+) pts", template_reason)
    pivot = pts_hit.group(1) if pts_hit else ""
    prompt = (
        REASON_RULES.format(lost=lost, total=total, pts=pivot)
        + "\n\nLine to rephrase:\n"
        + template_reason
    )
    try:
        out = _chat(prompt, json_mode=False)
    except Exception:
        return None
    if not out:
        return None
    out = out.strip().strip('"').strip()
    if f"{lost} of {total}" not in out:
        return None
    if pivot and f"{pivot} pts" not in out:
        return None
    if len(out) > 240:
        return None
    return out


# --------------------------------------------------------------------------
# DRAFT (SPEC 5.7 + SPEC 7): may rephrase, may cite only passed ids
# --------------------------------------------------------------------------
DRAFT_RULES = (
    "You are sales rep {rep} ({voice}) writing a follow-up email to {contact} "
    "of {company}.\n"
    "Reword this draft in {voice} voice. Rules:\n"
    "- Keep the structure and meaning; if it offers two options, keep two.\n"
    "- Cite ONLY these deal ids: {ids}.\n"
    "- Use ONLY facts and figures present in CONTEXT. Never introduce a price, "
    "percentage, figure, date or name that is absent from CONTEXT.\n"
    '- Reply with STRICT JSON: {{"subject": "...", "body": "..."}}, using \\n '
    "for line breaks."
)


def rephrase_draft(
    *,
    draft: DraftFollowup,
    allowed_ids: Sequence[str],
    context_text: str,
) -> DraftFollowup | None:
    """LLM rephrase of a draft body. Validated; None means keep the template.

    Validation (SPEC 7): every cited id must be in `allowed_ids` and every
    digit in the subject/body must occur in `context_text`. On any failure the
    caller keeps the deterministic template, so the UI never shows an invented
    figure.
    """
    prompt = (
        DRAFT_RULES.format(
            rep="Priya Nair",
            voice="Priya",
            contact="Maria Torres",
            company="Northwind Logistics",
            ids=", ".join(allowed_ids),
        )
        + "\n\nCurrent draft:\n"
        + draft.body
        + "\n\nCONTEXT:\n"
        + context_text
    )
    try:
        raw = _chat(prompt, json_mode=True)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        body = str(payload.get("body", "")).strip()
        subject = str(payload.get("subject", "")).strip()
        if not body or not subject:
            raise ValueError("missing fields")
    except Exception:
        return None

    candidate = draft.model_copy(update={"subject": subject, "body": body})
    ok, _ = validate_draft(
        candidate,
        allowed_ids=allowed_ids,
        context_text=context_text,
    )
    if not ok:
        return None
    return candidate.model_copy(update={"generated_by": "groq"})