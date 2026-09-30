"""Deterministic, bounded adjustments from signals. Every change carries a cited reason.

Weekly projection:
  adjusted = base * status_factor * (1 + role) * (1 + sentiment)
    status  out / ir -> 0 (and the player is benched), doubtful -> 0.1, questionable -> 0.85
            (same numbers as slots.STATUS_DISCOUNT; a news status replaces ESPN's so it never counts twice)
    role    +-25% max, scaled by confidence
    sentiment/hype  +-15% max, scaled by confidence
  The questionable discount means a healthy backup within ~15% gets the start.
Rest-of-season value (waivers, trades):
  ros_multiplier = (1 + role/2) * (1 + buzz_bonus), buzz bonus capped at +10% (drops: -10%).
Only status signals with confidence >= 0.7 can mark a player out.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import LeagueState, Player
from ..slots import STATUS_DISCOUNT
from .classify import Signal
from .sources import Buzz

STATUS_FACTOR = {"out": 0.0, "ir": 0.0, "doubtful": 0.1, "questionable": 0.85, "active": 1.0}
POSSIBLY_OUT = 0.4   # "out" reported with low confidence
# expected_to_play target factors. Blended with the ESPN default by confidence:
#   factor = espn_default + (target - espn_default) * confidence
PLAY_FACTOR = {"likely": 1.0, "uncertain": 0.6, "unlikely": 0.2}
ROLE_CAP, SENT_CAP, BUZZ_CAP = 0.25, 0.15, 0.10
OUT_CONFIDENCE = 0.7


@dataclass
class Adjustment:
    player_id: int
    multiplier: float = 1.0
    news_out: bool = False
    news_status: str | None = None
    ros_multiplier: float = 1.0
    buzz: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _short_date(published: str) -> str:
    return published[:10] if published else "recent"


def _cite(sig: Signal) -> str:
    return f"{sig.source}, {_short_date(sig.published)}"


def buzz_score(b: Buzz | None, news_count: int) -> float:
    """0..1. Sleeper top-10 add ~0.8+, plus 0.05 per news item, capped at 1."""
    score = 0.0
    if b and b.add_rank:
        score += max(0.0, 1.0 - (b.add_rank - 1) / 50)
    score += min(0.3, 0.05 * news_count)
    return round(min(1.0, score), 2)


def compute(player: Player, signals: list[Signal], buzz: Buzz | None = None,
            espn_status: str | None = None) -> Adjustment:
    adj = Adjustment(player.player_id)
    sigs = sorted(signals, key=lambda s: s.published or "", reverse=True)

    # Game status: the newest confident status/practice signal wins. With no such signal the
    # proj is left alone and Player.status_discount applies ESPN's default discount instead.
    base = STATUS_DISCOUNT.get(player.game_status, 1.0)
    status_sig = next((s for s in sigs if (s.status_change or s.expected_to_play) and s.confidence >= 0.5), None)
    status_factor = 1.0
    if status_sig:
        st, play = status_sig.status_change, status_sig.expected_to_play
        note = f": {status_sig.usage_note}" if status_sig.usage_note else ""
        if st in ("out", "ir") and status_sig.confidence < OUT_CONFIDENCE:
            status_factor = POSSIBLY_OUT
            adj.news_status = "possibly out"
            adj.reasons.append(f"{player.name} possibly out{note} ({_cite(status_sig)}), not confident enough to bench outright")
        elif st in ("out", "ir"):
            status_factor = 0.0
            adj.news_out = True
            adj.news_status = st
            adj.reasons.append(f"{player.name} reported {st.upper()}{note} ({_cite(status_sig)})")
        else:
            target, label = ((PLAY_FACTOR[play], f"{play} to play") if play else (STATUS_FACTOR[st], f"listed {st}"))
            status_factor = round(base + (target - base) * status_sig.confidence, 3)
            adj.news_status = label
            if status_factor != 1.0 or base != 1.0:
                adj.reasons.append(f"{player.name} {label}{note} ({_cite(status_sig)}), counts {status_factor:.0%}")

    role = sum(s.role_change * s.confidence for s in sigs) * ROLE_CAP
    role = max(-ROLE_CAP, min(ROLE_CAP, role))
    if abs(role) >= 0.05:
        best = max(sigs, key=lambda s: abs(s.role_change) * s.confidence)
        adj.reasons.append(f"{player.name} role {'up' if role > 0 else 'down'}: {best.usage_note or 'usage change'} ({_cite(best)})")

    if sigs:
        sent = sum(s.sentiment * s.confidence for s in sigs) / len(sigs) * SENT_CAP
    else:
        sent = 0.0
    sent = max(-SENT_CAP, min(SENT_CAP, sent))

    adj.multiplier = round(status_factor * (1 + role) * (1 + sent), 3)

    adj.buzz = buzz_score(buzz, len(sigs))
    buzz_bonus = adj.buzz * BUZZ_CAP
    if buzz and buzz.drop_rank and not buzz.add_rank:
        buzz_bonus = -max(0.0, 1.0 - (buzz.drop_rank - 1) / 50) * BUZZ_CAP
        adj.reasons.append(f"{player.name} trending in drops on Sleeper (#{buzz.drop_rank})")
    elif buzz and buzz.add_rank and buzz.add_rank <= 25:
        adj.reasons.append(f"{player.name} #{buzz.add_rank} most-added on Sleeper ({buzz.add_count} adds)")
    adj.ros_multiplier = round((1 + role / 2) * (1 + max(-BUZZ_CAP, min(BUZZ_CAP, buzz_bonus))), 3)
    return adj


def apply(state: LeagueState, signals: list[Signal], buzz: dict[int, Buzz] | None = None) -> dict[int, Adjustment]:
    """Apply adjustments to every player in the state (all rosters + free agents), in place.
    Decisions downstream read proj_week / ros_value / available(), so they pick this up."""
    buzz = buzz or {}
    by_player: dict[int, list[Signal]] = {}
    for s in signals:
        if s.player_id is not None:
            by_player.setdefault(s.player_id, []).append(s)
    everyone = [p for t in state.teams.values() for p in t.roster] + state.free_agents
    out = {}
    for p in everyone:
        sigs, bz = by_player.get(p.player_id, []), buzz.get(p.player_id)
        if not sigs and not bz:
            continue
        a = compute(p, sigs, bz)
        if p.base_proj_week is None:
            p.base_proj_week = p.proj_week
        p.proj_week = round(p.base_proj_week * a.multiplier, 2)
        p.news_out = a.news_out
        p.news_status = a.news_status
        p.ros_multiplier = a.ros_multiplier
        p.buzz = a.buzz
        p.reasons = a.reasons
        out[p.player_id] = a
    return out
