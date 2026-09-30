"""nflverse usage data via nflreadpy, cached under data/nflverse (weekly refresh).

Sources (all free, nflverse / ffverse):
  load_player_stats(season)            targets, target_share, air_yards_share, wopr, carries, PPR points
  load_snap_counts(season)             offense snap %
  load_ff_opportunity(season)          expected fantasy points (xFP) from ffverse's volume model
  load_injuries(season)                weekly report_status / practice_status
  load_player_stats(prev, "reg")       last season's per-game baseline
Not available for the current season: route participation (nflverse participation stops at 2025),
red-zone / goal-line splits (would need full play-by-play; skipped to keep runs light).

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
    return build(season, stats, snaps, opp, inj, prev)


def build(season: int, stats: list[dict], snaps: list[dict], opp: list[dict], inj: list[dict],
          prev: list[dict]) -> UsageData:
    """Pure: raw nflverse rows -> UsageData. Carry share is computed from team carries."""
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
