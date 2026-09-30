"""Deterministic factor score. Each factor returns a bounded multiplier and a reason.

weekly value = proj_week (ESPN, news already applied) x status discount x product(factors),
with the factor product clamped to total_cap. Usage trend also feeds rest-of-season value.
Weights and caps come from config `factors:` (defaults below).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from ..models import LeagueState, Player
from ..slots import NON_STARTING
from .context import WeekContext
from .stadiums import STADIUMS

DEFAULTS = {
    "second_projection": {"weight": 0.5, "cap": 0.15},   # blend toward another source's projection
    "matchup": {"weight": 0.5, "cap": 0.12},             # opponent points allowed to the position
    "home": {"cap": 0.015},
    "vegas": {"weight": 0.4, "cap": 0.10, "baseline": 22.5},
    "script": {"cap": 0.04, "spread": 7.0},
    "usage": {"weight": 0.15, "cap": 0.08},
    "weather": {"wind_mph": 15.0, "rain_mm": 1.0, "pass_cap": 0.08, "kick_cap": 0.15},
    "ceiling": {"weight": 0.15, "cap": 0.04, "edge": 0.10},
    "total_cap": [0.70, 1.35],
}
VOLATILITY = {"WR": 1.0, "TE": 0.9, "D/ST": 0.8, "RB": 0.75, "QB": 0.6, "K": 0.5}
PASSING = {"QB", "WR", "TE"}


@dataclass
class Factor:
    name: str
    multiplier: float
    reason: str


def merged_config(user: dict | None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    for k, v in (user or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _clamp(x: float, cap: float) -> float:
    return max(-cap, min(cap, x))


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.lower().replace(".", "")).strip()


def score_factors(p: Player, ctx: WeekContext, cfg: dict) -> list[Factor]:
    out: list[Factor] = []
    game = ctx.games.get(p.pro_team)

    c = cfg["second_projection"]
    other = ctx.second_proj.get(p.player_id)
    if other is not None and p.proj_week > 0:
        m = 1 + _clamp((other / p.proj_week - 1) * c["weight"], c["cap"])
        out.append(Factor("second projection", m, f"Sleeper projects {other:.1f} vs ESPN {p.proj_week:.1f}"))

    if game:
        c = cfg["matchup"]
        allowed = ctx.def_vs_pos.get((game.opp, p.position))
        pos_vals = [v for (t, pos), v in ctx.def_vs_pos.items() if pos == p.position]
        rank = ctx.def_rank.get((game.opp, p.position))
        if allowed is not None and pos_vals:
            mean = sum(pos_vals) / len(pos_vals)
            m = 1 + _clamp((allowed / mean - 1) * c["weight"], c["cap"]) if mean > 0 else 1.0
            rk = f" ({_ordinal(rank)} vs {p.position})" if rank else ""
            what = "soft" if m > 1 else "tough"
            out.append(Factor("matchup", m, f"{what} matchup: {game.opp} allows {allowed:.1f} to {p.position} vs {mean:.1f} avg{rk}"))

        c = cfg["home"]
        out.append(Factor("home/away", 1 + (c["cap"] if game.home else -c["cap"]),
                          f"{'home vs' if game.home else 'at'} {game.opp}"))

    team_line = ctx.vegas.get(p.pro_team)
    if team_line:
        tt, spread = team_line
        c = cfg["vegas"]
        if p.position == "D/ST" and game and game.opp in ctx.vegas:
            opp_tt = ctx.vegas[game.opp][0]
            m = 1 + _clamp((c["baseline"] - opp_tt) / c["baseline"] * c["weight"] * 1.25, c["cap"])
            out.append(Factor("vegas", m, f"opponent implied total {opp_tt:.1f}"))
        else:
            m = 1 + _clamp((tt - c["baseline"]) / c["baseline"] * c["weight"], c["cap"])
            out.append(Factor("vegas", m, f"team implied total {tt:.1f}"))
        s = cfg["script"]
        if abs(spread) >= s["spread"] and p.position in ("RB", "WR", "TE"):
            dog = spread > 0
            if p.position == "RB":
                m = 1 - s["cap"] if dog else 1 + s["cap"]
            else:
                m = 1 + s["cap"] * 0.75 if dog else 1 - s["cap"] * 0.5
            out.append(Factor("game script", m, f"{'underdog' if dog else 'favorite'} by {abs(spread):.1f}"))

    u = ctx.usage.get(_norm(p.name))
    if u:
        c = cfg["usage"]
        keys = {"RB": ["snap_share", "carries"], "WR": ["snap_share", "target_share"],
                "TE": ["snap_share", "target_share"]}.get(p.position, [])
        trends = []
        for k in keys:
            series = [x for x in u.get(k, []) if x is not None]
            if len(series) >= 2:
                trends.append((series[-1] - series[0]) / max(abs(series[0]), 0.05))
        if trends:
            t = sum(trends) / len(trends)
            m = 1 + _clamp(t * c["weight"], c["cap"])
            if abs(m - 1) >= 0.005:
                out.append(Factor("usage trend", m, f"usage {'up' if t > 0 else 'down'} over last {len(u.get(keys[0], []))} weeks"))

    if game:
        w = ctx.weather.get(game.home_team)
        lat_lon = STADIUMS.get(game.home_team)
        if w and lat_lon and not lat_lon[2]:
            wind, rain = w
            c = cfg["weather"]
            hit = 0.0
            if p.position in PASSING:
                if wind >= c["wind_mph"]:
                    hit += min(c["pass_cap"], (wind - c["wind_mph"] + 3) * 0.01)
                if rain >= c["rain_mm"]:
                    hit += 0.03
                hit = min(hit, c["pass_cap"])
            elif p.position == "K":
                if wind >= c["wind_mph"] - 5:
                    hit += min(c["kick_cap"], (wind - c["wind_mph"] + 5) * 0.015)
                if rain >= c["rain_mm"]:
                    hit += 0.03
                hit = min(hit, c["kick_cap"])
            if hit > 0:
                out.append(Factor("weather", 1 - hit, f"weather at {game.home_team}: wind {wind:.0f} mph, rain {rain:.1f} mm/h"))

    if ctx.our_edge is not None:
        c = cfg["ceiling"]
        vol = VOLATILITY.get(p.position, 0.75) - 0.75
        if abs(ctx.our_edge) >= c["edge"] and vol != 0:
            sign = 1 if ctx.our_edge < 0 else -1          # underdog -> ceiling, favorite -> floor
            m = 1 + _clamp(sign * vol * c["weight"], c["cap"])
            pref = "ceiling" if ctx.our_edge < 0 else "floor"
            out.append(Factor("floor/ceiling", m,
                              f"we are {abs(ctx.our_edge):.0%} projected {'underdogs' if ctx.our_edge < 0 else 'favorites'}, favoring {pref}"))
    return out


def matchup_multiplier(position: str, opp: str, ctx: WeekContext, cfg: dict | None = None) -> float:
    """Opponent points allowed to the position vs league average, capped. Used for future weeks."""
    c = merged_config(cfg)["matchup"]
    allowed = ctx.def_vs_pos.get((opp, position))
    vals = [v for (t, pos), v in ctx.def_vs_pos.items() if pos == position]
    if allowed is None or not vals:
        return 1.0
    mean = sum(vals) / len(vals)
    return 1 + _clamp((allowed / mean - 1) * c["weight"], c["cap"]) if mean > 0 else 1.0


def factor_product(factors: list[Factor], cfg: dict) -> float:
    lo, hi = cfg["total_cap"]
    prod = 1.0
    for f in factors:
        prod *= f.multiplier
    return round(max(lo, min(hi, prod)), 3)


def our_edge(state: LeagueState) -> float | None:
    """Our best lineup vs the opponent's, on current projections."""
    from ..lineup import best_assignment
    m = state.matchup
    if not m or m.get("opp_team_id") not in state.teams:
        return None
    starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}
    wk = state.scoring_period
    val = lambda p, s: p.lineup_value(wk) + 1e-6
    ours, _ = best_assignment(state.roster, starting, val)
    theirs, _ = best_assignment(state.teams[m["opp_team_id"]].roster, starting, val)
    return round(ours / theirs - 1, 3) if theirs > 0 else None


def apply(state: LeagueState, ctx: WeekContext, user_cfg: dict | None = None) -> None:
    """Set score_multiplier / factors on every player (in place). Usage trend also moves ROS."""
    cfg = merged_config(user_cfg)
    state.context = ctx
    if ctx.our_edge is None:
        ctx.our_edge = our_edge(state)
    everyone = [p for t in state.teams.values() for p in t.roster] + state.free_agents
    for p in everyone:
        fs = score_factors(p, ctx, cfg)
        p.factors = fs
        p.score_multiplier = factor_product(fs, cfg)
        usage = next((f for f in fs if f.name == "usage trend"), None)
        if usage:
            p.ros_multiplier = round(p.ros_multiplier * usage.multiplier, 3)


def top_reasons(p: Player, n: int = 2) -> list[str]:
    fs = sorted([f for f in getattr(p, "factors", []) if f.name != "home/away"],
                key=lambda f: abs(f.multiplier - 1), reverse=True)
    return [f.reason for f in fs[:n] if abs(f.multiplier - 1) >= 0.02]
