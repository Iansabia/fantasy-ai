"""Trade evaluator: rest-of-season value and starting-lineup impact for both sides.

Two numbers per side:
- ros_delta: ROS points received minus ROS points sent (raw talent swap).
- lineup_delta: change in the best weekly starting lineup (per-game season value) times the
  weeks left. This is what actually wins matchups: a bench player you receive adds little.
Verdict comes from lineup_delta for our side; the partner side is shown so the proposal is
realistic. Trades are only ever proposed, never accepted (trades_propose_only).
"""
from __future__ import annotations

from dataclasses import dataclass

from .lineup import season_lineup_value
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


@dataclass
class TradeEval:
    me: SideImpact
    partner: SideImpact
    verdict: str
    reason: str


def _side(state: LeagueState, team_id: int, sends: list[Player], receives: list[Player]) -> SideImpact:
    week = state.scoring_period
    team = state.teams[team_id]
    before = season_lineup_value(team.roster, state.slot_counts)
    out_ids = {p.player_id for p in sends}
    after_roster = [p for p in team.roster if p.player_id not in out_ids] + list(receives)
    after = season_lineup_value(after_roster, state.slot_counts)
    weeks_left = max(0, SEASON_WEEKS - week + 1)
    ros = sum(p.ros_value(week) for p in receives) - sum(p.ros_value(week) for p in sends)
    return SideImpact(team_id, team.name, sends, receives, round(ros, 1),
                      round((after - before) * weeks_left, 1), round(after - before, 1))


def evaluate_trade(state: LeagueState, give: list[Player], get: list[Player]) -> TradeEval:
    mine = {p.player_id for p in state.roster}
    bad_give = [p.name for p in give if p.player_id not in mine]
    if bad_give:
        raise ValueError(f"Not on your roster: {', '.join(bad_give)}")
    partners = {p.on_team_id for p in get}
    if len(partners) != 1 or state.my_team_id in partners or 0 in partners:
        raise ValueError("All players you get must come from one other team.")
    partner_id = partners.pop()

    me = _side(state, state.my_team_id, give, get)
    them = _side(state, partner_id, get, give)
    if me.lineup_delta > FAIR_BAND:
        verdict = "ACCEPTABLE" if them.lineup_delta >= -FAIR_BAND else "GOOD FOR US, HARD SELL"
    elif me.lineup_delta >= -FAIR_BAND:
        verdict = "ROUGHLY EVEN"
    else:
        verdict = "DECLINE"
    reason = (f"Our starting lineup changes by {me.lineup_delta:+.1f} points rest-of-season "
              f"(raw value {me.ros_delta:+.1f}); {them.team_name} changes by {them.lineup_delta:+.1f}.")
    return TradeEval(me, them, verdict, reason)
