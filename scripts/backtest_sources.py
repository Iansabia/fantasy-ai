"""Multi-source backtest, 2023-2025 regular seasons (weeks 2-17), PPR.

Sources (weekly):
  espn       ESPN's weekly projection (JVille league history, statSourceId 1)
  sleeper    Sleeper's weekly projection (public API, PPR)
  ecr        FantasyPros weekly expert consensus rank (nflverse load_ff_rankings archive, Friday
             scrape), mapped to points: ECR order with ESPN's point scale for that week+position
  baseline   season-to-date PPR points per game (nflverse)
  ours       replay of our weekly worth: ESPN x Sleeper blend (weight 0.75, cap 15%) x home/away
             x Vegas implied total x game script x wind (nflverse closing lines + weather), capped
             [0.70, 1.35]. NOT replayable: news/injury adjustments, % started, floor/ceiling,
             questionable deferral (no history), matchup factor is off.
  blend      linear blend of espn, sleeper, ecr, baseline, weights fitted on the OTHER two seasons
FantasyCalc has no values before 2026 (its history endpoint starts 2026-01-01), so it is not here.
Universe: QB/RB/WR/TE player-weeks with ESPN >= 3, all sources present, the player had a stat
line that week, and the game was on Sunday or Monday (Friday ECR scrapes come after Thursday games).

ROS (from weeks 4, 8, 12; target = total PPR points from that week through 17):
  ecr_ros    FantasyPros ROS consensus rank (Friday before the week)
  espn_wk    ESPN's weekly projection that week (ESPN season projections are not kept historically)
  baseline   season-to-date PPG
  ours_part  0.82 x ESPN weekly x fitted usage trend (ROS); the ESPN season-rate half of our live
             value is missing historically. Trend weights were fitted on these same seasons
             (in-sample, slightly optimistic)
  blend_ros  linear blend of espn_wk, ecr_ros (as points), baseline, fitted on the other seasons
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fantasy_ai.market import spearman  # noqa: E402
from fantasy_ai.usage_data import norm_name  # noqa: E402

DATA = ROOT / "data" / "backtest_sources"
SEASONS = (2023, 2024, 2025)
WEEK1_SUNDAY = {2023: dt.date(2023, 9, 10), 2024: dt.date(2024, 9, 8), 2025: dt.date(2025, 9, 7)}
POSITIONS = ("QB", "RB", "WR", "TE")
WEEKLY_PAGES = {"/nfl/rankings/qb.php": "QB", "/nfl/rankings/ppr-rb.php": "RB",
                "/nfl/rankings/ppr-wr.php": "WR", "/nfl/rankings/ppr-te.php": "TE"}
ROS_PAGES = {"/nfl/rankings/ros-qb.php": "QB", "/nfl/rankings/ros-ppr-rb.php": "RB",
             "/nfl/rankings/ros-ppr-wr.php": "WR", "/nfl/rankings/ros-ppr-te.php": "TE"}
ROS_TOP = {"QB": 32, "RB": 60, "WR": 80, "TE": 32}
NFV = {"WSH": "WAS", "LAR": "LA", "JAC": "JAX"}


def nfl_setup():
    import nflreadpy as nfl
    from nflreadpy.config import update_config
    update_config(cache_mode="filesystem", cache_dir=(ROOT / "data" / "nflverse").resolve(),
                  cache_duration=60 * 86400)
    return nfl


def week_of(season: int, date_str: str) -> int:
    d = dt.date.fromisoformat(date_str[:10])
    # a Friday (or earlier) scrape is for the coming Sunday's week
    return (d - (WEEK1_SUNDAY[season] - dt.timedelta(days=6))).days // 7 + 1


def load_ecr():
    import polars as pl
    nfl = nfl_setup()
    df = nfl.load_ff_rankings("all").with_columns(pl.col("scrape_date").cast(pl.Utf8).alias("d"))
    weekly, ros = {}, {}
    for season in SEASONS:
        lo, hi = f"{season}-08-20", f"{season + 1}-01-15"
        sub = df.filter((pl.col("d") >= lo) & (pl.col("d") < hi) & pl.col("fp_page").is_in(
            list(WEEKLY_PAGES) + list(ROS_PAGES))).select(["fp_page", "mergename", "ecr", "d"]).to_dicts()
        first = {}                                         # earliest scrape per (page, week)
        for r in sub:
            k = (r["fp_page"], week_of(season, r["d"]))
            first[k] = min(first.get(k, r["d"]), r["d"])
        for r in sub:
            wk = week_of(season, r["d"])
            if r["d"] != first[(r["fp_page"], wk)] or r["ecr"] is None:
                continue
            if r["fp_page"] in WEEKLY_PAGES:
                weekly[(season, wk, WEEKLY_PAGES[r["fp_page"]], norm_name(r["mergename"]))] = float(r["ecr"])
            else:
                ros[(season, wk, ROS_PAGES[r["fp_page"]], norm_name(r["mergename"]))] = float(r["ecr"])
    return weekly, ros


def load_season(season: int):
    nfl = nfl_setup()
    stats = [r for r in nfl.load_player_stats(season).to_dicts() if r.get("season_type", "REG") in ("REG", None)]
    sched = [g for g in nfl.load_schedules(season).to_dicts() if g.get("game_type") == "REG"]
    return stats, sched


def game_info(sched):
    """(team, week) -> dict(home, implied total, spread from team view (+ = underdog), wind, sun/mon)."""
    out = {}
    for g in sched:
        wd = dt.date.fromisoformat(g["gameday"]).weekday()
        total, spread = g.get("total_line"), g.get("spread_line")       # spread_line: home favored by
        roof = (g.get("roof") or "").lower()
        wind = g.get("wind") if roof in ("outdoors", "open") else 0
        for team, home in ((g["home_team"], True), (g["away_team"], False)):
            tt = ts = None
            if total is not None and spread is not None:
                tt = total / 2 + (spread / 2 if home else -spread / 2)
                ts = -spread if home else spread
            out[(team, g["week"])] = {"home": home, "tt": tt, "ts": ts, "wind": wind or 0, "sunmon": wd in (6, 0)}
    return out


def ours_mult(pos, espn, sleeper, gi):
    clamp = lambda x, c: max(-c, min(c, x))
    m = 1 + clamp((sleeper / espn - 1) * 0.75, 0.15) if sleeper is not None and espn > 0 else 1.0
    m *= 1.015 if gi["home"] else 0.985
    if gi["tt"] is not None:
        m *= 1 + clamp((gi["tt"] - 22.5) / 22.5 * 0.4, 0.10)
        if gi["ts"] is not None and abs(gi["ts"]) >= 7 and pos in ("RB", "WR", "TE"):
            dog = gi["ts"] > 0
            m *= (0.96 if dog else 1.04) if pos == "RB" else (1.03 if dog else 0.99)
    if pos in ("QB", "WR", "TE") and gi["wind"] and gi["wind"] >= 15:
        m *= 1 - min(0.08, (gi["wind"] - 15 + 3) * 0.01)
    return max(0.70, min(1.35, m))


def build_rows():
    ecr_w, ecr_r = load_ecr()
    weekly_rows, ros_rows = [], []
    for season in SEASONS:
        espn = json.loads((DATA / f"espn_{season}.json").read_text())
        slp = json.loads((DATA / f"sleeper_{season}.json").read_text())
        stats, sched = load_season(season)
        gi = game_info(sched)
        played = {}
        team_of = {}
        for r in stats:
            if r.get("position") in POSITIONS:
                k = (norm_name(r["player_display_name"]), r["position"])
                played[(k, r["week"])] = float(r.get("fantasy_points_ppr") or 0)
                team_of[(k, r["week"])] = r["team"]
        ppg_before = {}
        by_player = defaultdict(list)
        for (k, wk), pts in played.items():
            by_player[k].append((wk, pts))
        for k, games in by_player.items():
            games.sort()
            for wk in range(2, 19):
                prior = [p for w, p in games if w < wk]
                if prior:
                    ppg_before[(k, wk)] = sum(prior) / len(prior)
        sl = {}
        for wk, rows in slp.items():
            for r in rows:
                sl[(int(wk), norm_name(r["name"]), r["pos"])] = float(r["pts"])
        # weekly
        for wk_s, rows in espn.items():
            wk = int(wk_s)
            if wk < 2:
                continue
            for r in rows:
                k = (norm_name(r["name"]), r["pos"])
                e = r["proj"]
                if e is None or e < 3 or (k, wk) not in played:
                    continue
                team = team_of[(k, wk)]
                g = gi.get((team, wk))
                s = sl.get((wk, k[0], k[1]))
                ec = ecr_w.get((season, wk, k[1], k[0]))
                b = ppg_before.get((k, wk))
                if g is None or not g["sunmon"] or s is None or ec is None or b is None:
                    continue
                weekly_rows.append({"season": season, "wk": wk, "pos": k[1], "name": k[0], "actual": played[(k, wk)],
                                    "espn": e, "sleeper": s, "ecr_rank": ec, "baseline": b,
                                    "ours": e * ours_mult(k[1], e, s, g)})
        # ROS
        for start in (4, 8, 12):
            rows = espn.get(str(start), [])
            for r in rows:
                k = (norm_name(r["name"]), r["pos"])
                rank = ecr_r.get((season, start, k[1], k[0]))
                if rank is None or rank > ROS_TOP[k[1]] or r["proj"] is None:
                    continue
                actual = sum(played.get((k, w), 0.0) for w in range(start, 18))
                ros_rows.append({"season": season, "start": start, "pos": k[1], "name": k[0], "actual": actual,
                                 "ecr_rank": rank, "espn_wk": r["proj"], "baseline": ppg_before.get((k, start), 0.0)})
    return weekly_rows, ros_rows


def map_rank_to_points(rows, rank_key, scale_key, group_keys, out_key):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[g] for g in group_keys)].append(r)
    for rs in groups.values():
        scale = sorted((r[scale_key] for r in rs), reverse=True)
        for i, r in enumerate(sorted(rs, key=lambda r: r[rank_key])):
            r[out_key] = scale[i]


def ols(rows, feats, y="actual"):
    k = len(feats) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r in rows:
        x = [1.0] + [r[f] for f in feats]
        for a in range(k):
            xty[a] += x[a] * r[y]
            for c in range(k):
                xtx[a][c] += x[a] * x[c]
    m = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for c in range(k):
        piv = max(range(c, k), key=lambda q: abs(m[q][c]))
        m[c], m[piv] = m[piv], m[c]
        for q in range(k):
            if q != c:
                f = m[q][c] / m[c][c]
                m[q] = [a - f * b for a, b in zip(m[q], m[c])]
    return [m[i][k] / m[i][i] for i in range(k)]


def heldout_blend(rows, feats, out_key, by_pos=True):
    """Fit on the other seasons (per position), predict this season. Returns {season: coefs}."""
    coefs = {}
    for season in SEASONS:
        train = [r for r in rows if r["season"] != season]
        for pos in (POSITIONS if by_pos else (None,)):
            tr = [r for r in train if pos is None or r["pos"] == pos]
            c = ols(tr, feats)
            coefs[(season, pos)] = c
            for r in rows:
                if r["season"] == season and (pos is None or r["pos"] == pos):
                    r[out_key] = c[0] + sum(ci * r[f] for ci, f in zip(c[1:], feats))
    return coefs


def pair_acc(rs, key):
    win = tot = 0.0
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            a, b = rs[i], rs[j]
            if a["actual"] == b["actual"]:
                continue
            tot += 1
            d = a[key] - b[key]
            if d == 0:
                win += 0.5
            elif (d > 0) == (a["actual"] > b["actual"]):
                win += 1
    return win / tot if tot else None


def weekly_board(rows, keys):
    out = {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["season"], r["wk"], r["pos"])].append(r)
    for key in keys:
        res = {"MAE": sum(abs(r[key] - r["actual"]) for r in rows) / len(rows)}
        for pos in POSITIONS + ("ALL",):
            sp, pa = [], []
            for (s, w, p), rs in groups.items():
                if pos != "ALL" and p != pos:
                    continue
                if len(rs) >= 5:
                    v = spearman([r[key] for r in rs], [r["actual"] for r in rs])
                    if v is not None:
                        sp.append(v)
                    a = pair_acc(rs, key)
                    if a is not None:
                        pa.append(a)
            res[f"rho_{pos}"] = sum(sp) / len(sp) if sp else None
            res[f"pair_{pos}"] = sum(pa) / len(pa) if pa else None
        out[key] = res
    return out


def ros_board(rows, keys):
    out = {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["season"], r["start"], r["pos"])].append(r)
    for key in keys:
        res = {}
        for start in (4, 8, 12, "ALL"):
            vals = []
            for (s, st, p), rs in groups.items():
                if start != "ALL" and st != start:
                    continue
                v = spearman([r[key] for r in rs], [r["actual"] for r in rs])
                if v is not None:
                    vals.append(v)
            res[start] = sum(vals) / len(vals) if vals else None
        out[key] = res
    return out


def add_trend(ros_rows):
    """ours_part = 0.82 x ESPN weekly x fitted ROS trend multiplier (usage through week-1)."""
    from fantasy_ai import trend, usage_data
    from fantasy_ai.models import Player
    nfl = nfl_setup()
    for season in SEASONS:
        stats = nfl.load_player_stats(season).to_dicts()
        snaps = nfl.load_snap_counts(season).to_dicts()
        try:
            inj = nfl.load_injuries(season).to_dicts()
        except Exception:
            inj = []
        pbp = nfl.load_pbp(season).select(["season_type", "week", "posteam", "yardline_100", "pass_attempt",
                                           "rush_attempt", "receiver_player_id", "rusher_player_id"]).to_dicts()
        u = usage_data.build(season, stats, snaps, [], inj, [], pbp)
        for r in ros_rows:
            if r["season"] != season:
                continue
            p = Player(0, r["name"], r["pos"])
            t = trend.compute(p, u, r["start"])
            r["ours_part"] = 0.82 * r["espn_wk"] * (t.ros if t else 1.0)


def fmt(v):
    return f"{v:.3f}" if v is not None else "  n/a"


def main():
    weekly, ros = build_rows()
    map_rank_to_points(weekly, "ecr_rank", "espn", ("season", "wk", "pos"), "ecr")
    coefs = heldout_blend(weekly, ["espn", "sleeper", "ecr", "baseline"], "blend")
    heldout_blend(weekly, ["ours", "ecr"], "blend_ours_ecr")
    keys = ["ours", "espn", "sleeper", "ecr", "baseline", "blend", "blend_ours_ecr"]
    board = weekly_board(weekly, keys)
    print(f"WEEKLY  n={len(weekly)} player-weeks; by season " + str({s: sum(1 for r in weekly if r['season'] == s) for s in SEASONS}))
    print(f"{'source':16} {'MAE':>6} " + " ".join(f"{'rho_' + p:>8}" for p in POSITIONS + ('ALL',))
          + " " + " ".join(f"{'pair_' + p:>9}" for p in POSITIONS + ('ALL',)))
    for k in sorted(keys, key=lambda k: -board[k]["pair_ALL"]):
        b = board[k]
        print(f"{k:16} {b['MAE']:6.2f} " + " ".join(f"{fmt(b['rho_' + p]):>8}" for p in POSITIONS + ('ALL',))
              + " " + " ".join(f"{fmt(b['pair_' + p]):>9}" for p in POSITIONS + ('ALL',)))
    print("\nblend weights by held-out season / position (intercept, espn, sleeper, ecr, baseline):")
    for (s, p), c in sorted(coefs.items()):
        print(f"  test {s} {p}: " + ", ".join(f"{x:+.3f}" for x in c))
    # per-season pair accuracy for top sources
    print("\nper-season start/sit accuracy (ALL positions):")
    for s in SEASONS:
        sb = weekly_board([r for r in weekly if r["season"] == s], keys)
        print(f"  {s}: " + ", ".join(f"{k} {sb[k]['pair_ALL']:.3f}" for k in keys))
    # ROS
    map_rank_to_points(ros, "ecr_rank", "espn_wk", ("season", "start", "pos"), "ecr_ros_pts")
    for r in ros:
        r["ecr_ros"] = -r["ecr_rank"]
    add_trend(ros)
    for r in ros:                     # per remaining calendar week, so one model serves every start week
        r["actual_raw"], r["actual"] = r["actual"], r["actual"] / (18 - r["start"])
    rcoefs = heldout_blend(ros, ["espn_wk", "ecr_ros_pts", "baseline"], "blend_ros")
    pooled = {pos: ols([r for r in ros if r["pos"] == pos], ["espn_wk", "ecr_ros_pts", "baseline"]) for pos in POSITIONS}
    rkeys = ["ecr_ros", "espn_wk", "ours_part", "baseline", "blend_ros"]
    rb = ros_board(ros, rkeys)
    print(f"\nROS  n={len(ros)} player-starts; Spearman with actual points from that week to 17 (avg over season x position)")
    print(f"{'source':12} " + " ".join(f"{'from wk ' + str(s):>10}" for s in (4, 8, 12)) + f" {'all':>8}")
    for k in sorted(rkeys, key=lambda k: -rb[k]["ALL"]):
        print(f"{k:12} " + " ".join(f"{fmt(rb[k][s]):>10}" for s in (4, 8, 12)) + f" {fmt(rb[k]['ALL']):>8}")
    print("\nROS blend weights (intercept, espn_wk, ecr_ros_pts, baseline), per remaining week:")
    for (se, po), c in sorted(rcoefs.items()):
        print(f"  test {se} {po}: " + ", ".join(f"{x:+.3f}" for x in c))
    print("  pooled (used live): " + "; ".join(f"{po} " + ", ".join(f"{x:+.3f}" for x in c) for po, c in pooled.items()))
    (ROOT / "src" / "fantasy_ai" / "ros_blend.json").write_text(json.dumps({
        "features": ["espn_wk", "ecr_ros_pts", "baseline"], "target": "PPR points per remaining week",
        "coef": pooled, "heldout_spearman": {k: rb[k]["ALL"] for k in rkeys},
        "seasons": list(SEASONS)}, indent=1))
    json.dump({"weekly": board, "ros": {k: {str(a): b for a, b in v.items()} for k, v in rb.items()},
               "blend_coefs": {f"{s}|{p}": c for (s, p), c in coefs.items()}},
              open(DATA / "leaderboard.json", "w"), indent=1)


if __name__ == "__main__":
    main()
