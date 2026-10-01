"""Fit how much a usage trend predicts future fantasy points (2023-2025 nflverse, regular season).

Unit: player-game i (RB, WR, TE) with at least 2 earlier games and 2 recent games this season.
  recent  = his last 2 games (i-1, i); earlier = his games before that this season
  features (recent minus earlier, in share points, 0.10 = 10 points):
    d_primary  target share (WR/TE) or carry share (RB)
    d_snap     offense snap %
    d_rz       red-zone opportunity share (targets + carries inside the 20 / team's, from pbp)
  targets: next 3 games played and rest of season, PPR points per game relative to his
    season-to-date average:  y = future ppg / season-to-date ppg - 1   (clipped to [-1, 2])
Model per group (RB; WR/TE): y = a + b1 d_primary + b2 d_snap + b3 d_rz, OLS.
Held out by season: fit on two seasons, score on the third (skill = 1 - MSE / MSE of intercept only).
Then refit on all three. Writes src/fantasy_ai/trend_fit.json.

Run: .venv/bin/python scripts/fit_trend.py   (downloads nflverse pbp ~30 MB per season once, cached)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fantasy_ai.usage_data import norm_name  # noqa: E402

SEASONS = (2023, 2024, 2025)
GROUPS = {"RB": ("RB",), "WRTE": ("WR", "TE")}
FEATS = ("d_primary", "d_snap", "d_rz")
MIN_BASE_PPG = 4.0


def load(season: int):
    import nflreadpy as nfl
    from nflreadpy.config import update_config
    update_config(cache_mode="filesystem", cache_dir=(ROOT / "data" / "nflverse").resolve(),
                  cache_duration=60 * 86400)
    stats = [r for r in nfl.load_player_stats(season).to_dicts() if r.get("season_type", "REG") in ("REG", None)]
    snaps = nfl.load_snap_counts(season).to_dicts()
    pbp = nfl.load_pbp(season).select(["season_type", "week", "posteam", "yardline_100", "pass_attempt",
                                       "rush_attempt", "receiver_player_id", "rusher_player_id"]).to_dicts()
    return stats, snaps, pbp


def rows_for(season: int) -> list[dict]:
    stats, snaps, pbp = load(season)
    team_car = defaultdict(float)
    for r in stats:
        team_car[(r["team"], r["week"])] += float(r.get("carries") or 0)
    snap = {(norm_name(r["player"]), r["team"], r["week"]): float(r.get("offense_pct") or 0)
            for r in snaps if r.get("game_type", "REG") in ("REG", None)}
    rz_team, rz_pl = defaultdict(int), defaultdict(int)
    for p in pbp:
        if p.get("season_type") != "REG" or p.get("yardline_100") is None or p["yardline_100"] > 20:
            continue
        if not (p.get("pass_attempt") == 1 or p.get("rush_attempt") == 1):
            continue
        rz_team[(p["posteam"], p["week"])] += 1
        who = p.get("receiver_player_id") if p.get("pass_attempt") == 1 else p.get("rusher_player_id")
        if who:
            rz_pl[(who, p["week"])] += 1
    games = defaultdict(list)
    for r in stats:
        pos = r.get("position")
        if pos not in ("RB", "WR", "TE"):
            continue
        tk = (r["team"], r["week"])
        s = snap.get((norm_name(r.get("player_display_name", "")), r["team"], r["week"]))
        if s is None or s <= 0:
            continue
        games[r["player_id"]].append({
            "week": r["week"], "pos": pos,
            "primary": float(r.get("target_share") or 0) if pos != "RB" else
            (float(r.get("carries") or 0) / team_car[tk] if team_car[tk] else 0.0),
            "snap": s, "rz": rz_pl[(r["player_id"], r["week"])] / rz_team[tk] if rz_team[tk] else 0.0,
            "pts": float(r.get("fantasy_points_ppr") or 0)})
    out = []
    for pid, gs in games.items():
        gs.sort(key=lambda g: g["week"])
        for i in range(3, len(gs) - 1):
            earlier, recent = gs[:i - 1], gs[i - 1:i + 1]
            base = sum(g["pts"] for g in gs[:i + 1]) / (i + 1)
            if base < MIN_BASE_PPG:
                continue
            nxt = gs[i + 1:i + 4]
            rest = gs[i + 1:]
            m = lambda key, xs: sum(g[key] for g in xs) / len(xs)
            row = {"season": season, "pos": gs[i]["pos"],
                   "d_primary": m("primary", recent) - m("primary", earlier),
                   "d_snap": m("snap", recent) - m("snap", earlier),
                   "d_rz": m("rz", recent) - m("rz", earlier),
                   "y3": max(-1.0, min(2.0, m("pts", nxt) / base - 1)),
                   "yros": max(-1.0, min(2.0, m("pts", rest) / base - 1))}
            out.append(row)
    return out


def ols(rows, ycol):
    """Normal equations, intercept + FEATS. Returns coefficient list [a, b...]."""
    k = len(FEATS) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r in rows:
        x = [1.0] + [r[f] for f in FEATS]
        for a in range(k):
            xty[a] += x[a] * r[ycol]
            for c in range(k):
                xtx[a][c] += x[a] * x[c]
    m = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(k):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [a - f * b for a, b in zip(m[r], m[c])]
    return [m[i][k] / m[i][i] for i in range(k)]


def predict(coef, r):
    return coef[0] + sum(c * r[f] for c, f in zip(coef[1:], FEATS))


def skill(train, test, ycol):
    coef = ols(train, ycol)
    mean = sum(r[ycol] for r in train) / len(train)
    mse = sum((r[ycol] - predict(coef, r)) ** 2 for r in test) / len(test)
    mse0 = sum((r[ycol] - mean) ** 2 for r in test) / len(test)
    return 1 - mse / mse0


def bins(rows, ycol, feat="d_primary", edges=(-0.10, -0.05, -0.02, 0.02, 0.05, 0.10)):
    out = []
    lo = -9.0
    for hi in list(edges) + [9.0]:
        sel = [r[ycol] for r in rows if lo <= r[feat] < hi]
        if sel:
            out.append({"from": lo, "to": hi, "n": len(sel), "mean_y": round(sum(sel) / len(sel), 4)})
        lo = hi
    return out


def main():
    allrows = []
    for s in SEASONS:
        allrows += rows_for(s)
        print(s, "rows so far", len(allrows), flush=True)
    result = {"seasons": list(SEASONS), "features": list(FEATS), "groups": {}}
    for g, poss in GROUPS.items():
        rows = [r for r in allrows if r["pos"] in poss]
        entry = {"n": len(rows)}
        for ycol in ("y3", "yros"):
            entry[ycol] = {
                "coef": [round(c, 4) for c in ols(rows, ycol)],
                "holdout_skill": {str(s): round(skill([r for r in rows if r["season"] != s],
                                                      [r for r in rows if r["season"] == s], ycol), 4)
                                  for s in SEASONS},
                "bins_d_primary": bins(rows, ycol),
                "bins_d_snap": bins(rows, ycol, "d_snap", (-0.15, -0.08, -0.03, 0.03, 0.08, 0.15)),
            }
        result["groups"][g] = entry
        print(g, json.dumps(entry, indent=1))
    (ROOT / "src" / "fantasy_ai" / "trend_fit.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
