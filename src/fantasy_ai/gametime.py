"""Questionable starters: decide now, or wait for a later run?

A questionable player whose game kicks off AFTER our next scheduled run is not benched for
injury risk alone: we can still swap him on that later run if he is ruled out, so the injury
haircut is taken back out for now and the reason says "decision deferred". Only the final run
before his game applies the play probability (availability.py). On a run after inactives (90
min before kickoff) a questionable player who was not ruled out counts as active.

Run times come from schedule.run_slots (config `schedule:`), kickoffs from ESPN's schedule.
FAI_NOW (ISO time) overrides the clock for dry runs and tests.
"""
from __future__ import annotations

import datetime as dt
import os

from .availability import INACTIVES_LEAD
from .schedule import ET, upcoming_runs

MARGIN = dt.timedelta(minutes=15)       # a later run must finish this long before kickoff
NOT_DEFERRABLE = ("possibly out", "out", "ir", "listed doubtful", "listed out")


def now() -> dt.datetime:
    raw = os.environ.get("FAI_NOW")
    if raw:
        t = dt.datetime.fromisoformat(raw)
        return t if t.tzinfo else t.replace(tzinfo=ET)
    return dt.datetime.now(dt.timezone.utc)


def kickoffs(raw_pro: dict, week: int) -> dict[str, dt.datetime]:
    from .factors.providers import parse_pro_games
    out = {}
    for team, g in parse_pro_games(raw_pro, week).items():
        if g.kickoff_ms:
            out[team] = dt.datetime.fromtimestamp(g.kickoff_ms / 1000, dt.timezone.utc)
    return out


def _uncertain(p) -> bool:
    if p.is_out() or p.news_out or p.game_status == "DOUBTFUL":
        return False
    if (p.news_status or "") in NOT_DEFERRABLE:
        return False
    return p.game_status == "QUESTIONABLE" or (p.news_status is not None and p.news_status_factor < 1)


def decide(kick: dt.datetime | None, at: dt.datetime, sched: dict | None) -> tuple[str, dt.datetime | None]:
    """('defer', next run) | ('active', None) | ('final', None)."""
    if kick is None or at >= kick:
        return "final", None
    for _, t in upcoming_runs(at, sched):
        if t <= kick - MARGIN:
            return "defer", t
        break
    if at >= kick - INACTIVES_LEAD:
        return "active", None
    return "final", None


def _restore(p, label: str, reason: str) -> None:
    if p.news_status_factor and 0 < p.news_status_factor < 1:
        p.proj_week = round(p.proj_week / p.news_status_factor, 2)
        p.news_status_factor = 1.0
    if p.base_proj_week is None:
        p.base_proj_week = p.proj_week
    p.news_status = label                 # status_discount() is 1.0 once a news status is set
    p.reasons.insert(0, reason)


def apply(state, raw_pro: dict, sched: dict | None = None, at: dt.datetime | None = None) -> list[str]:
    """Mutates questionable players in state. Returns one note per player changed."""
    at = at or now()
    kicks = kickoffs(raw_pro, state.scoring_period)
    notes = []
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if not _uncertain(p):
            continue
        what, nxt = decide(kicks.get(p.pro_team), at, sched)
        if what == "defer":
            p.deferred_until = nxt
            when = nxt.astimezone(ET).strftime("%a %-I:%M%p ET")
            _restore(p, "questionable, decision deferred",
                     f"{p.name} questionable, decision deferred to the final pre-game run ({when}); no injury haircut yet")
            notes.append(f"{p.name}: deferred to {when}")
        elif what == "active":
            _restore(p, "active (not ruled out at inactives)",
                     f"{p.name} not ruled out at inactives (90 min before kickoff): counts as active")
            notes.append(f"{p.name}: active after inactives")
    return notes
