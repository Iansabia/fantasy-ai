"""Waiver / free agent ranking: value over the weakest rostered player at the position.

Value = rest-of-season projected points (Player.ros_value). A suggestion pairs a free agent
with the weakest droppable player at the same position. Locked players and players in the
IR slot are never suggested as drops (ESPN rejects dropping a locked player, and IR spots do
not cost a bench spot).
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import LeagueState, Player

MIN_GAIN = 3.0          # ignore upgrades smaller than this many ROS points
POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")


@dataclass
class WaiverSuggestion:
    add: Player
    drop: Player | None
    add_value: float
    drop_value: float
    gain: float
    kind: str               # "FREEAGENT" (instant) or "WAIVER" (claim)
    reason: str


def weakest_by_position(state: LeagueState) -> dict[str, Player]:
    week = state.scoring_period
    out: dict[str, Player] = {}
    for p in state.roster:
        if p.locked or p.on_ir_slot:
            continue
        cur = out.get(p.position)
        if cur is None or p.ros_value(week) < cur.ros_value(week):
            out[p.position] = p
    return out


def rank_free_agents(state: LeagueState, limit: int = 5, min_gain: float = MIN_GAIN) -> list[WaiverSuggestion]:
    week = state.scoring_period
    weakest = weakest_by_position(state)
    suggestions: list[WaiverSuggestion] = []
    for fa in state.free_agents:
        drop = weakest.get(fa.position)
        if drop is None:
            continue
        add_v, drop_v = fa.ros_value(week), drop.ros_value(week)
        gain = round(add_v - drop_v, 1)
        if gain < min_gain:
            continue
        why = f"{fa.name} ({fa.position}) projects {add_v:.1f} rest-of-season vs {drop.name} {drop_v:.1f}, +{gain:.1f}."
        if drop.is_out():
            why += f" {drop.name} is {drop.injury_status}."
        if fa.status == "WAIVERS":
            why += " On waivers: needs a claim."
        suggestions.append(WaiverSuggestion(fa, drop, add_v, drop_v, gain, "WAIVER" if fa.status == "WAIVERS" else "FREEAGENT", why))
    suggestions.sort(key=lambda s: s.gain, reverse=True)

    # Don't suggest dropping the same player twice.
    used: set[int] = set()
    unique = []
    for s in suggestions:
        if s.drop and s.drop.player_id in used:
            continue
        if s.drop:
            used.add(s.drop.player_id)
        unique.append(s)
    return unique[:limit]
