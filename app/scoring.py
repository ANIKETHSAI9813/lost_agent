"""Scoring engine (SPEC 4, section 5.5).

Every constant is read from `seed/scoring_config.json`; none are hardcoded
(SPEC 4 line 39). The one addition is `clip`, defined here as a named constant
(CLIP_MIN / CLIP_MAX in `app.config`) and documented in the README, which SPEC
4 line 47 explicitly allows and requires.

The reference trajectory in SPEC 3 is reproduced exactly by this arithmetic;
`tests/test_scoring.py` asserts it turn by turn.

Sequencing rules implemented here, from SPEC 4 and DECISIONS.md:
  * D2  the combined `competitor:Acme+objection:no_exec_sponsor` signal carries
    weight 0.8 (no such key exists in scoring_config.json).
  * D3  one resolved pattern feeds both the score and the fatal check, so the
    reason line and the score can never disagree.
  * D5  signals are applied once, in arrival order, and are never merged
    retroactively -- applying budget_freeze at T3 then champion_left at T5
    leaves two separate history entries.
  * D8  the confidence shown in the UI is the minimum across the signals that
    moved the score, not an average.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Sequence

from .config import CLIP_MAX, CLIP_MIN
from .models import Pattern, ScoreHistoryEntry, ScoringConfig
from .seed_loader import (
    COMBINED_KEY,
    RECOVERY_SIGNAL,
    Pattern as SeedPattern,
    compute_pattern_counts,
    pattern_index,
    resolve_objection_pattern,
)
from .memory import MemoryRecord

Direction = Literal["risk", "recovery"]

# D2: `scoring_config.json` has no weight for the combined pattern, so 0.8 is a
# documented choice: heavier than a bare competitor (0.12) and close to
# no_exec_sponsor on its own (0.8).
COMBINED_WEIGHT = 0.8

# Human labels for reason lines. These are display strings only; no number,
# deal id or rep name is ever invented here (SPEC 0 line 6).
OBJECTION_LABELS: dict[str, str] = {
    "budget_freeze": "budget freeze",
    "champion_left": "champion leaving",
    "no_exec_sponsor": "no executive sponsor",
    "competitor_pricing": "competitor pricing",
    "security_review_stall": "security review stall",
    "legal_redlines": "legal redlines",
    "timing_slip": "timing slip",
    "integration_concern": "integration concern",
}


# --------------------------------------------------------------------------
# math helpers
# --------------------------------------------------------------------------
def logit(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"logit undefined at p={p!r}")
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    """Numerically stable logistic function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def clip_ratio(x: float) -> float:
    """SPEC 4: `clip(x) = min(max(x, 0.05), 0.95)`.

    Documented addition. Without it, a 5-lost/0-won Vantage pattern gives
    ln(inf) and the score becomes NaN (SPEC 3 edge case).
    """
    return min(max(x, CLIP_MIN), CLIP_MAX)


def confidence_for(total: int, config: ScoringConfig) -> float:
    """SPEC 4: `confidence = min(1, total / confidence_full_at_n)`."""
    if total <= 0:
        return 0.0
    return min(1.0, total / config.confidence_full_at_n)


def weight_for(signal_key: str, config: ScoringConfig) -> float:
    """Look the weight up, falling back to the D2 combined weight."""
    if signal_key in config.signal_weights:
        return float(config.signal_weights[signal_key])
    if signal_key == COMBINED_KEY:
        return COMBINED_WEIGHT
    # A bare competitor signal uses the `competitor` weight (SPEC 4 line 49).
    if signal_key.startswith("competitor:"):
        return float(config.signal_weights.get("competitor", 0.0))
    if signal_key.startswith("recovery:"):
        return float(config.signal_weights.get(f"recovery:{RECOVERY_SIGNAL}", 0.0))
    if signal_key.startswith("objection:"):
        tag = signal_key.split(":", 1)[1].split("@", 1)[0]
        return float(config.signal_weights.get(f"objection:{tag}", 0.0))
    return 0.0


# --------------------------------------------------------------------------
# pattern description
# --------------------------------------------------------------------------
def describe_pattern(pattern: Pattern, *, direction: Direction = "risk") -> str:
    """One line of grounded prose. All numbers are interpolated verbatim."""
    key = pattern.key
    scope = "" if pattern.stage_scope == "stage" else " (all stages)"

    if key == COMBINED_KEY:
        what = "Acme plus no executive sponsor"
        tail = f"past Acme+no_exec_sponsor deals{scope}"
    elif key.startswith("recovery:"):
        tag = key.split("@", 1)[1] if "@" in key else ""
        what = "a positive reply to the follow-up"
        tail = f"past {tag} deals that got a positive reply"
    elif key.startswith("objection:"):
        remainder = key.split(":", 1)[1]
        tag = remainder.split("@", 1)[0]
        stage = remainder.split("@", 1)[1] if "@" in remainder else None
        what = OBJECTION_LABELS.get(tag, tag)
        tail = (
            f"past {tag} deals at {stage}"
            if stage
            else f"past {tag} deals{scope}"
        )
    elif key.startswith("competitor:"):
        name = key.split(":", 1)[1]
        what = f"{name} as a competitor"
        tail = f"past {name} deals{scope}"
    else:  # pragma: no cover - every key shape is covered above
        what, tail = key, f"past {key} deals{scope}"

    if direction == "recovery":
        return (
            f"Positive reply after the follow-up: {pattern.won} of {pattern.total} "
            f"{tail} were won."
        )
    return (
        f"{what} mentioned, and {pattern.lost} of {pattern.total} {tail} were lost."
    )


def is_fatal(pattern: Pattern, config: ScoringConfig) -> bool:
    """SPEC 5.7 draft trigger.

    `loss_ratio >= fatal_loss_ratio` AND `total >= min_deals_for_fatal_pattern`.
    Both use >=, so budget_freeze at 6/10 = 0.6 is fatal. Competitor-only
    signals never trigger a draft; that check lives in the pipeline.
    """
    return (
        pattern.loss_ratio >= config.fatal_loss_ratio
        and pattern.total >= config.min_deals_for_fatal_pattern
    )


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
@dataclass
class ScoreUpdate:
    """Result of applying one signal."""

    score_before: float
    score_after: float
    delta_pts: float
    signal_key: str
    direction: Direction
    pattern: Pattern
    confidence: float
    applied: bool
    ignored_reason: str = ""


@dataclass
class ScoringEngine:
    """Live-deal score state. Carries across events, never recomputed (SPEC 4)."""

    config: ScoringConfig
    records: Sequence[MemoryRecord] = field(default_factory=list)
    score: float = 0.0
    applied_signals: set[str] = field(default_factory=set)
    history: list[ScoreHistoryEntry] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history and self.score == 0.0:
            self.reset()

    # -- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        self.score = float(self.config.prior_win_probability)
        self.applied_signals = set()
        self.history = []
        self.ignored = []

    @property
    def confidence(self) -> float:
        """D8: the UI shows the weakest signal that moved the score."""
        if not self.history:
            return 0.0
        return min(entry.confidence for entry in self.history)

    def replace_records(self, records: Sequence[MemoryRecord]) -> None:
        """Refresh the data source, e.g. after POST /api/retain."""
        self.records = records

    # -- pattern resolution ----------------------------------------------
    def pattern_for(self, key: str, live_stage: str) -> Pattern | None:
        """Resolve a signal key to computed stats from the memory records."""
        deals = [r.deal for r in self.records]
        if key == COMBINED_KEY:
            scoped = [
                d
                for d in deals
                if "Acme" in d.competitors and "no_exec_sponsor" in d.objections
            ]
            if not scoped:
                return None
            return _to_pattern(
                pattern_index(compute_pattern_counts(scoped, self.config))[COMBINED_KEY]
            )
        if key.startswith("recovery:"):
            tag = key.split("@", 1)[1] if "@" in key else ""
            scoped = [
                d
                for d in deals
                if RECOVERY_SIGNAL in d.recovery_signals and tag in d.objections
            ]
            if not scoped:
                return None
            return _to_pattern(
                pattern_index(compute_pattern_counts(scoped, self.config))[key]
            )
        if key.startswith("competitor:"):
            name = key.split(":", 1)[1]
            scoped = [d for d in deals if name in d.competitors]
            if not scoped:
                return None
            return _to_pattern(
                pattern_index(compute_pattern_counts(scoped, self.config))[key]
            )
        if key.startswith("objection:"):
            remainder = key.split(":", 1)[1]
            tag = remainder.split("@", 1)[0]
            resolved = resolve_objection_pattern(
                deals, self.config, tag, live_stage
            )
            return _to_pattern(resolved) if resolved is not None else None
        return None

    # -- the update itself ------------------------------------------------
    def apply(
        self,
        signal_key: str,
        pattern: Pattern,
        *,
        direction: Direction = "risk",
        event_id: str = "",
        timestamp: str = "",
    ) -> ScoreUpdate:
        """Apply one signal. Idempotent per key (SPEC 4 line 51)."""
        if signal_key in self.applied_signals:
            reason = f"signal already applied for this deal: {signal_key}"
            self.ignored.append(reason)
            return ScoreUpdate(
                score_before=self.score,
                score_after=self.score,
                delta_pts=0.0,
                signal_key=signal_key,
                direction=direction,
                pattern=pattern,
                confidence=0.0,
                applied=False,
                ignored_reason=reason,
            )

        if pattern.total == 0:
            reason = f"no team history for {signal_key}"
            self.ignored.append(reason)
            return ScoreUpdate(
                score_before=self.score,
                score_after=self.score,
                delta_pts=0.0,
                signal_key=signal_key,
                direction=direction,
                pattern=pattern,
                confidence=0.0,
                applied=False,
                ignored_reason=reason,
            )

        confidence = confidence_for(pattern.total, self.config)
        weight = weight_for(signal_key, self.config)

        if direction == "risk":
            ratio = clip_ratio(pattern.loss_ratio)
        else:
            ratio = clip_ratio(pattern.won / pattern.total if pattern.total else 0.0)

        delta = self.config.logit_scale * logit(ratio) * weight * confidence
        delta = max(0.0, delta)  # SPEC 4: risk never raises, recovery never lowers

        before = self.score
        if direction == "risk":
            after = sigmoid(logit(before) - delta)
        else:
            after = sigmoid(logit(before) + delta)

        after = min(max(after, self.config.clamp.min), self.config.clamp.max)

        self.score = after
        self.applied_signals.add(signal_key)
        self.history.append(
            ScoreHistoryEntry(
                event_id=event_id,
                score_before=before,
                score_after=after,
                delta_pts=(after - before) * 100.0,
                signal_key=signal_key,
                pattern_text=describe_pattern(pattern, direction=direction),
                confidence=confidence,
                timestamp=timestamp,
            )
        )
        return ScoreUpdate(
            score_before=before,
            score_after=after,
            delta_pts=(after - before) * 100.0,
            signal_key=signal_key,
            direction=direction,
            pattern=pattern,
            confidence=confidence,
            applied=True,
        )


def _to_pattern(pattern: SeedPattern | None) -> Pattern | None:
    if pattern is None:
        return None
    return Pattern(
        key=pattern.key,
        total=pattern.total,
        lost=pattern.lost,
        won=pattern.won,
        loss_ratio=pattern.loss_ratio,
        stage_scope=pattern.stage_scope,
        display_text=pattern.display_text,
    )
