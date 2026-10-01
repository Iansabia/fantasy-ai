"""nflverse usage data via nflreadpy, cached under data/nflverse (weekly refresh).

Sources (all free, nflverse / ffverse):
  load_player_stats(season)            targets, target_share, air_yards_share, wopr, carries, PPR points
  load_snap_counts(season)             offense snap %
  load_ff_opportunity(season)          expected fantasy points (xFP) from ffverse's volume model
  load_injuries(season)                weekly report_status / practice_status
  load_player_stats(prev, "reg")       last season's per-game baseline
  load_pbp(season)                     red-zone opportunity share (targets + carries inside the 20)
Not available for the current season: route participation (nflverse participation stops at 2025).

Everything is turned into plain dicts (UsageData) so the hype model runs on fixtures in tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .prefs import data_dir

ESPN_TO_NFLVERSE = {"WSH": "WAS", "LAR": "LA"}
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str) -> str:
    toks = re.sub(r"[^a-z0-9 ]", "", (name or "").lower().replace(".", "").replace("'", "")).split()
    return " ".join(t for t in toks if t not in SUFFIX)


def nfl_team(espn_abbrev: str) -> str:
    return ESPN_TO_NFLVERSE.get(espn_abbrev, espn_abbrev)


@dataclass
class UsageData:
    season: int
    weekly: dict[str, list[dict]] = field(default_factory=dict)        # norm name -> rows by week
    last_season: dict[str, dict] = field(default_factory=dict)         # norm name -> per-game baseline
    injuries: dict[tuple[str, int], dict[str, str]] = field(default_factory=dict)  # (team, week) -> name -> status


def load(season: int, cache_dir: Path | None = None) -> UsageData | None:
    """Download (or read cache) and build UsageData. None if nflreadpy is missing or offline."""
    try:
        import nflreadpy as nfl
        from nflreadpy.config import update_config
        update_config(cache_mode="filesystem", cache_dir=(cache_dir or data_dir() / "nflverse").resolve(),
                      cache_duration=7 * 86400, user_agent="fantasy-ai (personal, low volume) nflreadpy")
        stats = nfl.load_player_stats(season).to_dicts()
        snaps = nfl.load_snap_counts(season).to_dicts()
    except Exception:
        return None
    try:
        opp = nfl.load_ff_opportunity(season).to_dicts()
    except Exception:
        opp = []
    try:
        inj = nfl.load_injuries(season).to_dicts()
    except Exception:
        inj = []
    try:
        prev = nfl.load_player_stats(season - 1, summary_level="reg").to_dicts()
    except Exception:
        prev = []
    try:
        pbp = nfl.load_pbp(season).select(["season_type", "week", "posteam", "yardline_100", "pass_attempt",
                                           "rush_attempt", "receiver_player_id", "rusher_player_id"]).to_dicts()
    except Exception:
        pbp = []
    return build(season, stats, snaps, opp, inj, prev, pbp)


def red_zone(pbp: list[dict]) -> tuple[dict, dict]:
    """(team, week) -> red-zone pass+rush plays; (player_id, week) -> his targets+carries there."""
    team, pl = {}, {}
    for p in pbp or []:
        if p.get("season_type") not in ("REG", None) or p.get("yardline_100") is None or p["yardline_100"] > 20:
            continue
        if not (p.get("pass_attempt") == 1 or p.get("rush_attempt") == 1):
            continue
        team[(p["posteam"], p["week"])] = team.get((p["posteam"], p["week"]), 0) + 1
        who = p.get("receiver_player_id") if p.get("pass_attempt") == 1 else p.get("rusher_player_id")
        if who:
            pl[(who, p["week"])] = pl.get((who, p["week"]), 0) + 1
    return team, pl


def build(season: int, stats: list[dict], snaps: list[dict], opp: list[dict], inj: list[dict],
          prev: list[dict], pbp: list[dict] | None = None) -> UsageData:
    """Pure: raw nflverse rows -> UsageData. Carry share is computed from team carries."""
    rz_team, rz_pl = red_zone(pbp or [])
    team_carries: dict[tuple[str, int], float] = {}
    for r in stats:
        if r.get("season_type", "REG") != "REG":
            continue
        k = (r.get("team"), r.get("week"))
        team_carries[k] = team_carries.get(k, 0) + float(r.get("carries") or 0)
    snap = {(norm_name(r.get("player", "")), r.get("team"), r.get("week")): float(r.get("offense_pct") or 0)
            for r in snaps}
    xfp = {(norm_name(r.get("full_name", "")), r.get("posteam"), r.get("week")): float(r.get("total_fantasy_points_exp") or 0)
           for r in opp}
    weekly: dict[str, list[dict]] = {}
    for r in stats:
        if r.get("season_type", "REG") != "REG":
            continue
        name, team, wk = norm_name(r.get("player_display_name", "")), r.get("team"), r.get("week")
        tc = team_carries.get((team, wk)) or 0
        weekly.setdefault(name, []).append({
            "week": wk, "team": team, "pos": r.get("position"),
            "targets": float(r.get("targets") or 0),
            "target_share": float(r.get("target_share") or 0),
            "air_yards_share": float(r.get("air_yards_share") or 0),
            "wopr": float(r.get("wopr") or 0),
            "carries": float(r.get("carries") or 0),
            "carry_share": (float(r.get("carries") or 0) / tc) if tc else 0.0,
            "snap_pct": snap.get((name, team, wk)),
            "rz_share": (rz_pl.get((r.get("player_id"), wk), 0) / rz_team[(team, wk)]) if rz_team.get((team, wk)) else 0.0,
            "fpts": float(r.get("fantasy_points_ppr") or 0),
            "xfp": xfp.get((name, team, wk)),
        })
    for rows in weekly.values():
        rows.sort(key=lambda x: x["week"])
    last = {}
    for r in prev:
        g = float(r.get("games") or 0)
        if g <= 0:
            continue
        last[norm_name(r.get("player_display_name", ""))] = {
            "target_share": float(r.get("target_share") or 0),
            "wopr": float(r.get("wopr") or 0),
            "carries_pg": float(r.get("carries") or 0) / g,
            "ppg": float(r.get("fantasy_points_ppr") or 0) / g,
        }
    injuries: dict[tuple[str, int], dict[str, str]] = {}
    for r in inj:
        injuries.setdefault((r.get("team"), r.get("week")), {})[norm_name(r.get("full_name", ""))] = \
            (r.get("report_status") or "").upper()
    return UsageData(season, weekly, last, injuries)
