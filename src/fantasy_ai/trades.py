"""Trade evaluator: rest-of-season value and starting-lineup impact for both sides.

Two numbers per side:
- ros_delta: ROS value above replacement received minus sent (vorp.py), so positions compare
  fairly (a backup QB in a 1-QB league is worth little even with big raw points).
- lineup_delta: teamneeds.lineup_change, the same number trade ideas use: best lineups over the
  next 3 weeks (byes, injuries) + rest-of-season lineup + depth above replacement, after any
  forced drops when a side goes over the roster limit. A bench player you receive adds little.
Verdict comes from lineup_delta for our side; the partner side is shown so the proposal is
realistic. Trades are only ever proposed, never accepted (trades_propose_only).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import SEASON_WEEKS, LeagueState, Player

FAIR_BAND = 3.0  # lineup points ROS; inside this band a trade is "roughly even"


@dataclass
class SideImpact:
    team_id: int
    team_name: str
    sends: list[Player]
    receives: list[Player]
    ros_delta: float
    lineup_delta: float
    per_week: float = 0.0     # starting-lineup change per week
    drops: list = field(default_factory=list)   # forced cuts to fit the roster limit


@dataclass
class TradeEval:
    me: SideImpact
    partner: SideImpact
    verdict: str
    reason: str


def _side(state: LeagueState, team_id: int, sends: list[Player], receives: list[Player],
          cache: dict | None = None) -> SideImpact:
    from .teamneeds import lineup_change
    from .vorp import vorp_ros
    week = state.scoring_period
    team = state.teams[team_id]
    delta, drops = lineup_change(state, team_id, sends, receives, cache)
    weeks_left = max(1, SEASON_WEEKS - week + 1)
    ros = sum(max(0.0, vorp_ros(p, state)) for p in receives) - sum(max(0.0, vorp_ros(p, state)) for p in sends)
    return SideImpact(team.team_id, team.name.strip(), sends, receives, round(ros, 1), delta,
                      round(delta / weeks_left, 1), drops)


def evaluate_trade(state: LeagueState, give: list[Player], get: list[Player], prefs=None) -> TradeEval:
    if prefs is not None:
        kept = [p.name for p in give if prefs.keeps(p.name) or getattr(prefs, "no_trade", lambda n: False)(p.name)]
        if kept:
            raise ValueError(f"Marked never drop / never trade: {', '.join(kept)}")
    mine = {p.player_id for p in state.roster}
    bad_give = [p.name for p in give if p.player_id not in mine]
    if bad_give:
        raise ValueError(f"Not on your roster: {', '.join(bad_give)}")
    partners = {p.on_team_id for p in get}
    if len(partners) != 1 or state.my_team_id in partners or 0 in partners:
        raise ValueError("All players you get must come from one other team.")
    partner_id = partners.pop()

    cache: dict = {}
    me = _side(state, state.my_team_id, give, get, cache)
    them = _side(state, partner_id, get, give, cache)
    if me.lineup_delta > FAIR_BAND:
        verdict = "ACCEPTABLE" if them.lineup_delta >= -FAIR_BAND else "GOOD FOR US, HARD SELL"
    elif me.lineup_delta >= -FAIR_BAND:
        verdict = "ROUGHLY EVEN"
    else:
        verdict = "DECLINE"
    reason = (f"Our starting lineup changes by {me.lineup_delta:+.1f} points rest-of-season "
              f"(value above replacement {me.ros_delta:+.1f}); {them.team_name} changes by {them.lineup_delta:+.1f}.")
    from .trend import trade_note
    tn = trade_note(give, get)
    if tn:
        reason += f" This trade {tn}."
    return TradeEval(me, them, verdict, reason)
