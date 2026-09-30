"""Value over replacement (VORP) for anything that compares players across positions.

Raw points overrate positions where a free replacement also scores a lot (QB in a 1-QB league).
Replacement level comes from the league itself, not from constants:
  starters league-wide per position = teams x dedicated slots (QB 0, RB 2, WR 4, TE 6),
  then each FLEX slot (23) goes to the best remaining RB/WR/TE and each superflex slot (7) to
  the best remaining QB/RB/WR/TE, by per-game season value.
  replacement per game = average of the next REPL_WINDOW players at the position after the
  starters (the best players you could expect to find on waivers or as a spare).
Pool: every rostered player plus the free agents we loaded, minus IR-reserve players.
Values: per-game season value = Player.per_game() x Player.ros_multiplier (same as trades).
"""
from __future__ import annotations

from .models import SEASON_WEEKS, LeagueState, Player

POSITIONS = ("QB", "RB", "WR", "TE")
DEDICATED = {"QB": 0, "RB": 2, "WR": 4, "TE": 6}
FLEX, SUPERFLEX = 23, 7
REPL_WINDOW = 3


def season_pg(p: Player) -> float:
    return p.per_game() * p.ros_multiplier


def starters_per_position(state: LeagueState, pool: list[Player] | None = None) -> dict[str, int]:
    """How many players at each position start across the league (FLEX shares by value)."""
    teams = max(1, len(state.teams))
    c = state.slot_counts
    n = {pos: teams * c.get(slot, 0) for pos, slot in DEDICATED.items()}
    pool = _pool(state) if pool is None else pool
    by_pos = {pos: sorted((season_pg(p) for p in pool if p.position == pos), reverse=True) for pos in POSITIONS}

    def fill(k: int, allowed: tuple[str, ...]) -> None:
        for _ in range(k):
            best = max(allowed, key=lambda pos: by_pos[pos][n[pos]] if n[pos] < len(by_pos[pos]) else -1)
            if n[best] >= len(by_pos[best]):
                return
            n[best] += 1

    fill(teams * c.get(FLEX, 0), ("RB", "WR", "TE"))
    fill(teams * c.get(SUPERFLEX, 0), ("QB", "RB", "WR", "TE"))
    return n


def _pool(state: LeagueState) -> list[Player]:
    seen, out = set(), []
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if p.player_id in seen or p.position not in POSITIONS or p.game_status == "INJURY_RESERVE":
            continue
        seen.add(p.player_id)
        out.append(p)
    return out


def replacement_levels(state: LeagueState) -> dict[str, float]:
    """Position -> replacement per-game value. Cached on the state until player values change."""
    pool = _pool(state)
    sig = (len(pool), round(sum(season_pg(p) for p in pool), 2), tuple(sorted(state.slot_counts.items())),
           len(state.teams))
    cached = getattr(state, "_vorp_cache", None)
    if cached and cached[0] == sig:
        return cached[1]
    n = starters_per_position(state, pool)
    out = {}
    for pos in POSITIONS:
        vals = sorted((season_pg(p) for p in pool if p.position == pos), reverse=True)
        window = vals[n[pos]:n[pos] + REPL_WINDOW] or vals[-1:] or [0.0]
        out[pos] = round(sum(window) / len(window), 2)
    state._vorp_cache = (sig, out)
    return out


def vorp_pg(p: Player, state: LeagueState) -> float:
    """Per-game value above replacement (negative below replacement; 0 for K / D/ST)."""
    if p.position not in POSITIONS:
        return 0.0
    return season_pg(p) - replacement_levels(state).get(p.position, 0.0)


def vorp_ros(p: Player, state: LeagueState) -> float:
    """Rest-of-season value above replacement, on the same weeks as Player.ros_value."""
    if p.position not in POSITIONS:
        return 0.0
    return round(vorp_pg(p, state) * _weeks(p, state.scoring_period), 1)


def _weeks(p: Player, wk: int) -> int:
    weeks = max(0, SEASON_WEEKS - wk + 1)
    if p.bye_week is not None and p.bye_week >= wk:
        weeks -= 1
    if p.is_out() and weeks > 0:
        weeks -= 1
    return weeks
