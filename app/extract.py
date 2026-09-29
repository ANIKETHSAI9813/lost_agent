"""EXTRACT (SPEC 5.2): turn text -> {objections[], competitors[], recovery[]}.

Rules, in the order they matter:

  * Only PROSPECT turns are read. A turn whose speaker contains "(Rep)" or
    matches the active rep yields zero signals, which is what makes turn 4
    ("options that work within a freeze") produce nothing.
  * The keyword lists are SPEC 5.2's, verbatim. Unknown tags are rejected
    against `objection_taxonomy` and `KNOWN_COMPETITORS`.
  * At most one objection per turn, because SPEC 3 establishes that every deal
    in this domain has exactly one objection. Turn 5 is the case that forces
    this: it contains both "moving over to" (champion_left) and "next quarter"
    (timing_slip), and `demo_call.json` expects champion_left alone. The most
    specific match wins (longest matched phrase, then taxonomy order), and the
    discarded candidates are reported in `ignored` rather than silently dropped.
    The score never sees the discarded tag.
  * Recovery fires only when a draft exists AND was marked sent AND this is a
    prospect email with positive intent.
"""

from __future__ import annotations

import re
from typing import Sequence

from .config import KNOWN_COMPETITORS
from .models import EventType, ExtractedSignals

# SPEC 5.2 keyword lists, verbatim.
OBJECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "budget_freeze": ("freeze", "hold on spend", "no budget"),
    "champion_left": ("moving over to", "moving to another team", "leaving", "new role"),
    "no_exec_sponsor": ("exec sponsor", "haven't looped in"),
    "competitor_pricing": ("cheaper", "lower price", "discount"),
    "security_review_stall": ("security review", "SOC 2"),
    "legal_redlines": ("redlines", "indemnity", "MSA"),
    "timing_slip": ("next quarter", "push"),
    "integration_concern": ("integration", "SSO"),
}

# SPEC 5.2 recovery trigger phrases.
POSITIVE_INTENT = ("thanks for sending", "can we set up", "take this to")

# D41: a keyword inside a negated clause is NOT a signal. `no` alone is never
# a negator (the phrase "no budget" must still fire, as must "we have no exec
# sponsor"); we use negators that cannot be part of the positive phrase, e.g.
# "not", "never", "won't", "no longer". The count of words looked back is
# capped (MAX_NEG_LOOKBACK) and a clause boundary (punctuation, or "but" /
# "however" / "although") stops the lookback.
NEGATORS = frozenset(
    {
        "not", "never", "won't", "wont", "can't", "cannot",
        "isn't", "aren't", "wasn't", "weren't", "doesn't", "don't", "didn't",
        "hasn't", "haven't", "no longer", "not at all",
    }
)
CLAUSE_BREAKS = frozenset({",", ";", ":", ".", "!", "?", "…", "—", "but", "however", "although", "though"})
LOOKBACK_TOKENS = 5


def _is_negated(text: str, start: int) -> bool:
    """True when a negator appears within `LOOKBACK_TOKENS` words before `start`
    without crossing a clause boundary. Negators are matched as whole words
    (single- and two-token), so "to" inside a bigger word can never bite."""
    before = text[:start]
    words = [w for w in re.split(r"\s+", before) if w]
    if not words:
        return False
    tail = words[-LOOKBACK_TOKENS:]
    index = len(tail) - 1
    while index >= 0:
        token = tail[index].strip("(),.!?;:'\"")
        if token.lower() in CLAUSE_BREAKS:
            return False
        if token.lower() in NEGATORS:
            return True
        if index >= 1:
            one = tail[index - 1].strip("(),.!?;:'\"")
            pair = f"{one.lower()} {token.lower()}"
            if pair in NEGATORS:
                return True
        index -= 1
    return False


def _unnegated_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    """(start, end) spans of `phrase` that are NOT inside a negated clause."""
    out: list[tuple[int, int]] = []
    for match in re.finditer(re.escape(phrase), text, re.IGNORECASE):
        if not _is_negated(text, match.start()):
            out.append((match.start(), match.end()))
    return out


def is_prospect_turn(speaker: str, active_rep: str) -> bool:
    """False for the rep's own turns, which must never produce a signal."""
    lowered = speaker.lower()
    if "(rep)" in lowered:
        return False
    if active_rep and active_rep.lower() in lowered:
        return False
    return True


def _matched_phrases(text: str, phrases: Sequence[str]) -> list[str]:
    return [phrase for phrase in phrases if _unnegated_spans(text, phrase)]


def find_competitors(text: str) -> list[str]:
    """D6 / D41: a competitor name appearing (and not negated) is the cue."""
    return [name for name in KNOWN_COMPETITORS if _unnegated_spans(text, name)]


def _select_objection(
    text: str, taxonomy: Sequence[str]
) -> tuple[str | None, list[str], list[str]]:
    """Return (chosen_tag, all_matched_tags, ignored_tags).

    The most specific match wins: longest keyword phrase matched, then the
    taxonomy order as a stable tiebreak.
    """
    best: tuple[int, int, str] | None = None
    matched: list[str] = []
    for order, tag in enumerate(taxonomy):
        phrases = OBJECTION_KEYWORDS.get(tag, ())
        hits = _matched_phrases(text, phrases)
        if not hits:
            continue
        matched.append(tag)
        longest = max(len(hit) for hit in hits)
        candidate = (longest, -order, tag)
        if best is None or candidate > best:
            best = candidate

    if best is None:
        return None, [], []
    chosen = best[2]
    return chosen, matched, [tag for tag in matched if tag != chosen]


def extract_turn(
    *,
    text: str,
    speaker: str,
    event_type: EventType,
    active_rep: str,
    taxonomy: Sequence[str],
    draft_exists: bool = False,
    draft_marked_sent: bool = False,
    candidates: tuple[str | None, list[str]] | None = None,
    source: str = "keyword",
) -> tuple[ExtractedSignals, list[str]]:
    """Extract one turn's signals. Returns (signals, ignored_reasons).

    `candidates` supplies objection/competitor candidates already produced by
    the LLM layer (SPEC 7). When given, keyword matching is skipped but every
    gate below still runs, so an unknown tag or a rep turn is rejected exactly
    as it would be on the keyword path. `source` becomes the signal's `source`
    label. When `candidates` is None the keyword lists of SPEC 5.2 are used.
    """
    ignored: list[str] = []

    if not is_prospect_turn(speaker, active_rep):
        ignored.append(
            f"skipped non-prospect turn (speaker: {speaker}); a rep turn yields no signal"
        )
        return (
            ExtractedSignals(objections=[], competitors=[], recovery=[], source="none"),
            ignored,
        )

    text = text.strip()

    if candidates is None:
        chosen, matched, discarded = _select_objection(text, taxonomy)
        for tag in discarded:
            ignored.append(
                f"kept '{chosen}' over '{tag}': SPEC 3 models exactly one objection per deal"
            )
        competitors = [name for name in find_competitors(text) if name in KNOWN_COMPETITORS]
    else:
        chosen, competitors = candidates
        competitors = [name for name in competitors if name in KNOWN_COMPETITORS]

    # Guard against any tag outside the taxonomy reaching the scorer.
    if chosen is not None and chosen not in taxonomy:
        ignored.append(f"rejected unknown objection tag: {chosen}")
        chosen = None

    recovery: list[str] = []
    if event_type == "email":
        if not draft_exists:
            ignored.append("recovery needs a drafted follow-up to exist first")
        elif not draft_marked_sent:
            ignored.append("recovery needs the follow-up to be marked sent first")
        elif any(phrase in text.lower() for phrase in POSITIVE_INTENT):
            recovery.append("positive_reply_after_followup")
        else:
            ignored.append("no positive-intent phrasing found in the email")
    else:
        ignored.append("recovery only fires on a prospect email")

    signals = ExtractedSignals(
        objections=[chosen] if chosen else [],
        competitors=competitors,
        recovery=recovery,
        source=source,
    )
    return signals, ignored


def numbers_in(text: str) -> list[str]:
    """Every digit sequence in a string (SPEC 7 draft validation)."""
    return re.findall(r"\d+", text)
