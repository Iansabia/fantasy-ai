"""Rest-of-season value from a blend fitted on held-out seasons (scripts/backtest_sources.py).

2023-2025 backtest, Spearman with actual rest-of-season points from weeks 4/8/12 (each season
predicted by weights fitted on the other two): blend 0.574, FantasyPros ROS ECR 0.561, ESPN weekly
0.491, season-to-date PPG 0.482, our replayable ROS 0.481. So ROS value is the blend:
  points per remaining week = a + b1 x ESPN-scale projection + b2 x ECR rank mapped to points
                              + b3 x season-to-date PPG          (weights per position, ros_blend.json)
Live inputs: ESPN-scale projection = Player.per_game() / 0.82 (our per-game value back on the weekly
scale); ECR ROS rank mapped to the projection of the player at that rank in our pool; PPG from
nflverse games this season. The fit only covered experts' top players (QB32, RB60, WR80, TE32), so
anyone outside that range is capped at his old value (the intercept would otherwise lift scrubs).
Config `signals: {ros_blend: false}` turns it off.
"""
from __future__ import annotations

import json
from pathlib import Path

from .usage_data import norm_name

FIT = json.loads((Path(__file__).parent / "ros_blend.json").read_text())
TOP = {"QB": 32, "RB": 60, "WR": 80, "TE": 32}
WEEKLY_SCALE = 0.82


def apply(state, usage=None) -> int:
    wk = state.scoring_period
    pool: dict[str, list] = {}
    seen = set()
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if p.position in TOP and p.player_id not in seen:
            seen.add(p.player_id)
            pool.setdefault(p.position, []).append(p)
    n = 0
    for pos, ps in pool.items():
        espn = {p.player_id: p.per_game() / WEEKLY_SCALE for p in ps}
        scale = sorted(espn.values(), reverse=True) or [0.0]
        a, b1, b2, b3 = FIT["coef"][pos]
        for p in ps:
            rank = (getattr(p, "ecr", None) or {}).get("ros")
            ecr_pts = scale[min(rank, len(scale)) - 1] if rank else scale[-1]
            rows = [r for r in (usage.weekly.get(norm_name(p.name), []) if usage else []) if r["week"] < wk]
            ppg = sum(r["fpts"] for r in rows) / len(rows) if rows else espn[p.player_id]
            blend = max(0.0, a + b1 * espn[p.player_id] + b2 * ecr_pts + b3 * ppg)
            if not rank or rank > TOP[pos]:
                blend = min(blend, p.per_game() * p.ros_multiplier)      # outside the fitted range
            p.ros_blend_pg = round(blend, 3)
            n += 1
    return n
