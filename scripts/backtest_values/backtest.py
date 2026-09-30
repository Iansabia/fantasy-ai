"""Backtest: our ROS value vs FantasyCalc vs ESPN projections, as of after week 1 and week 2.

No leakage rules:
- ESPN side uses only actuals from weeks <= N and ESPN's weekly projection for week N+1
  (published before week N+1 kicked off).
- Hype uses nflverse rows with week <= N (hype.compute filters week < scoring_period) and the
  week N+1 nflverse injury report for current status.
- FC uses the daily redraft value on a date before week N+1 kicked off.
Not reconstructable (left neutral): news role/buzz multiplier, ownership trend, factor usage trend.
"""
import datetime as dt
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

from fantasy_ai import hype, usage_data
from fantasy_ai.models import LeagueState, Player, Team
from fantasy_ai.market import spearman

HERE = Path(__file__).resolve().parents[2] / "data" / "backtest_values"
K_ROS = 0.78          # ESPN ROS per game ~= 0.78 x next-week projection (calibrated on week 4 data)
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}
FC_DATES = {1: ("09/16/2026", "09/19/2026"), 2: ("09/23/2026", "09/26/2026")}
TOPK = {"QB": (12,), "TE": (12,), "RB": (24, 36), "WR": (24, 36)}
NF_STATUS = {"OUT": "OUT", "DOUBTFUL": "DOUBTFUL", "QUESTIONABLE": "QUESTIONABLE"}

weekly = json.loads((HERE / "espn_weekly.json").read_text())
pro = json.loads((HERE / "pro.json").read_text())
fc = json.loads((HERE / "fc_hist.json").read_text())
teams = {t["id"]: (t["abbrev"], t.get("byeWeek") or None) for t in pro["settings"]["proTeams"]}


def stat(pl, src, split, per):
    for s in pl.get("stats", []):
        if s["seasonId"] == 2026 and s["statSourceId"] == src and s["statSplitTypeId"] == split \
                and s["scoringPeriodId"] == per:
            return float(s.get("appliedTotal") or 0)
    return None


players = {}
for wk, raw in weekly.items():
    wk = int(wk)
    for x in raw["players"]:
        pl = x["player"]
        if pl.get("defaultPositionId") not in POS:
            continue
        d = players.setdefault(pl["id"], {"id": pl["id"], "name": pl["fullName"], "pos": POS[pl["defaultPositionId"]],
                                          "team": teams.get(pl.get("proTeamId"), ("", None))[0],
                                          "bye": teams.get(pl.get("proTeamId"), ("", None))[1],
                                          "proj": {}, "act": {}})
        pr = stat(pl, 1, 1, wk)
        if pr is not None:
            d["proj"][wk] = pr
        if wk <= 3:
            a = stat(pl, 0, 1, wk)
            d["act"][wk] = a     # None = no game logged (did not play / bye)

usage = usage_data.load(2026)


def fc_value(pid, date):
    rec = fc.get(str(pid))
    if not rec:
        return None
    for h in rec["hist"]:
        if h["date"] == date:
            return float(h["value"])
    return None


def build_snapshot(N, fc_date, hype_scale=1.0):
    """Rows for snapshot 'after week N' (decisions for week N+1)."""
    wk = N + 1
    plist = []
    for d in players.values():
        nxt = d["proj"].get(wk)
        fv = fc_value(d["id"], fc_date)
        if not nxt or not fv:
            continue
        rem = sum(1 for w in range(wk, 19) if w != d["bye"])
        to_date = sum((d["act"].get(w) or 0) for w in range(1, wk))
        p = Player(player_id=d["id"], name=d["name"], position=d["pos"], pro_team=d["team"], bye_week=d["bye"],
                   proj_week=nxt, proj_season=to_date + K_ROS * nxt * rem)
        nf_team = usage_data.nfl_team(d["team"])
        st = (usage.injuries.get((nf_team, wk), {}) or {}).get(usage_data.norm_name(d["name"]), "")
        p.injury_status = NF_STATUS.get(st, "ACTIVE")
        p._d, p._fc = d, fv
        plist.append(p)
    state = LeagueState(league_id=0, year=2026, my_team_id=1, scoring_period=wk, matchup_period=wk,
                        slot_counts={}, teams={1: Team(1, "all", plist)})
    base = {p.player_id: p.ros_value(wk) for p in plist}
    hype.apply(state, usage)
    rows = []
    for p in plist:
        d = p._d
        m = p.hype.ros_mult
        m_scaled = 1 + (m - 1) * hype_scale
        if N == 1:
            target = (d["act"].get(2) or 0) + (d["act"].get(3) or 0)
            games = sum(1 for w in (2, 3) if d["act"].get(w) is not None)
        else:
            target = d["act"].get(3) or 0
            games = 1 if d["act"].get(3) is not None else 0
        rows.append({"id": p.player_id, "name": p.name, "pos": p.position, "label": p.hype.label,
                     "mult": m, "espn_next": p.proj_week, "espn_x_hype": round(p.proj_week * m_scaled, 3), "ours_nohype": base[p.player_id],
                     "ours": round(base[p.player_id] * m_scaled, 2), "ours_raw_mult": round(base[p.player_id] * m, 2),
                     "fc": p._fc, "target": target, "games": games,
                     "weeks_left": max(1, base[p.player_id] / max(p.per_game(), 1e-9))})
    return rows


# ---------------------------------------------------------------- metrics

def ranks_desc(xs):
    order = sorted(range(len(xs)), key=lambda i: -xs[i])
    r = [0] * len(xs)
    for k, i in enumerate(order):
        r[i] = k + 1
    return r


def topk_hit(rows, key, pos, k):
    rs = [r for r in rows if r["pos"] == pos]
    if len(rs) < k * 1.5:
        return None
    pred = set(r["id"] for r in sorted(rs, key=lambda r: -r[key])[:k])
    act = set(r["id"] for r in sorted(rs, key=lambda r: -r["target"])[:k])
    return len(pred & act) / k


def disagreement(rows, a, b, positions=("QB", "RB", "WR", "TE"), min_gap_pts=0.0):
    """Pairs (same position) where a and b order players oppositely: how often each was right."""
    wa = wb = 0
    for pos in positions:
        rs = [r for r in rows if r["pos"] == pos]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                x, y = rs[i], rs[j]
                da, db = x[a] - y[a], x[b] - y[b]
                dt_ = x["target"] - y["target"]
                if da * db >= 0 or abs(dt_) <= min_gap_pts:
                    continue
                if da * dt_ > 0:
                    wa += 1
                else:
                    wb += 1
    return wa, wb


def quantile_map(rows, src, ref):
    """Map src (e.g. FC value) onto the ref distribution (ESPN weekly points) by rank, per position."""
    out = {}
    for pos in POS.values():
        rs = [r for r in rows if r["pos"] == pos]
        refs = sorted((r[ref] for r in rs), reverse=True)
        for k, r in enumerate(sorted(rs, key=lambda r: -r[src])):
            out[r["id"]] = refs[k]
    return out


def mae(rows, pred_key_fn, N):
    denom = 2 if N == 1 else 1
    errs = [abs(pred_key_fn(r) - r["target"] / denom) for r in rows]
    return sum(errs) / len(errs)


PREDS = [("ours", "Ours (with hype)"), ("ours_nohype", "Ours, hype off"), ("espn_x_hype", "ESPN proj x hype"),
         ("espn_next", "ESPN next-week proj"),
         ("fc", "FantasyCalc")]


def report(N, fc_date, rows, out):
    out.append(f"\n=== Snapshot after week {N} (FC date {fc_date}); target = actual PPR "
               f"{'weeks 2-3' if N == 1 else 'week 3'}; n={len(rows)} ===")
    counts = defaultdict(int)
    for r in rows:
        counts[r["pos"]] += 1
    out.append("players by position: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    hdr = f"{'predictor':22} " + " ".join(f"{p:>6}" for p in ("QB", "RB", "WR", "TE")) + f" {'avg':>6}"
    out.append("Spearman vs actual points:\n" + hdr)
    for key, label in PREDS:
        vals = []
        for pos in ("QB", "RB", "WR", "TE"):
            rs = [r for r in rows if r["pos"] == pos]
            vals.append(spearman([r[key] for r in rs], [r["target"] for r in rs]))
        avg = sum(v for v in vals if v is not None) / len([v for v in vals if v is not None])
        out.append(f"{label:22} " + " ".join(f"{(v if v is not None else float('nan')):6.3f}" for v in vals) + f" {avg:6.3f}")
    out.append("Top-K hit rate (share of predicted top-K that finished top-K):")
    for key, label in PREDS:
        bits = []
        for pos, ks in TOPK.items():
            for k in ks:
                h = topk_hit(rows, key, pos, k)
                bits.append(f"{pos}{k} {h:.2f}" if h is not None else f"{pos}{k} n/a")
        out.append(f"  {label:22} " + "  ".join(bits))
    fc_pts = quantile_map(rows, "fc", "espn_next")
    denom = 2 if N == 1 else 1
    out.append("MAE, points per game (FC mapped onto ESPN's point scale by position rank):")
    out.append(f"  {'ESPN next-week proj':22} {mae(rows, lambda r: r['espn_next'], N):.2f}")
    out.append(f"  {'Ours (per game)':22} {mae(rows, lambda r: r['ours'] / r['weeks_left'], N):.2f}")
    out.append(f"  {'Ours hype off':22} {mae(rows, lambda r: r['ours_nohype'] / r['weeks_left'], N):.2f}")
    out.append(f"  {'FantasyCalc (mapped)':22} {mae(rows, lambda r: fc_pts[r['id']], N):.2f}")
    out.append("Disagreement test (same-position pairs ordered oppositely; wins for first vs second):")
    for a, b in (("ours", "fc"), ("ours_nohype", "fc"), ("espn_next", "fc"), ("ours", "ours_nohype"),
                 ("ours", "espn_next"), ("espn_x_hype", "espn_next"), ("espn_x_hype", "fc")):
        wa, wb = disagreement(rows, a, b)
        tot = wa + wb
        wa3, wb3 = disagreement(rows, a, b, min_gap_pts=3.0)
        out.append(f"  {a:12} vs {b:12}: {wa:5d} vs {wb:5d} pairs  ({(wa / tot if tot else 0):.1%} first)"
                   f"   | actual gap > 3 pts: {wa3} vs {wb3} ({(wa3 / (wa3 + wb3) if wa3 + wb3 else 0):.1%})")
    # label check
    out.append("Hype label check (actual per game minus ESPN next-week projection):")
    groups = defaultdict(list)
    for r in rows:
        if r["games"]:
            groups[r["label"].split(" (")[0]].append(r["target"] / denom - r["espn_next"])
    for lab, xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.append(f"  {lab:14} n={len(xs):3d}  mean residual {sum(xs) / len(xs):+5.2f}")


def boot(rows, N, reps=1000, seed=7):
    """Bootstrap players (within position): mean-over-positions Spearman diff ours - fc, and win share."""
    rng = random.Random(seed)
    by_pos = defaultdict(list)
    for r in rows:
        by_pos[r["pos"]].append(r)
    diffs, shares, diffs_h = [], [], []
    for _ in range(reps):
        sample = []
        for pos, rs in by_pos.items():
            s = [dict(rng.choice(rs)) for _ in rs]
            for k, r in enumerate(s):
                r["id"] = f"{r['id']}_{k}"
            sample += s
        d, dh = [], []
        for pos in ("QB", "RB", "WR", "TE"):
            rs = [r for r in sample if r["pos"] == pos]
            a = spearman([r["ours"] for r in rs], [r["target"] for r in rs])
            b = spearman([r["fc"] for r in rs], [r["target"] for r in rs])
            c = spearman([r["ours_nohype"] for r in rs], [r["target"] for r in rs])
            if a is not None and b is not None and c is not None:
                d.append(a - b)
                dh.append(a - c)
        diffs.append(sum(d) / len(d))
        diffs_h.append(sum(dh) / len(dh))
        wa, wb = disagreement(sample, "ours", "fc")
        shares.append(wa / (wa + wb) if wa + wb else 0.5)
    q = lambda xs: (sorted(xs)[int(0.05 * len(xs))], sorted(xs)[int(0.95 * len(xs))])
    return q(diffs), q(shares), q(diffs_h)


if __name__ == "__main__":
    out = []
    snaps = {}
    for N in (1, 2):
        for fc_date in FC_DATES[N]:
            rows = build_snapshot(N, fc_date)
            snaps[(N, fc_date)] = rows
            report(N, fc_date, rows, out)
        rows = snaps[(N, FC_DATES[N][0])]
        (d_lo, d_hi), (s_lo, s_hi), (h_lo, h_hi) = boot(rows, N, reps=300)
        out.append(f"Bootstrap 90% CI (week {N}, FC {FC_DATES[N][0]}): Spearman ours-FC [{d_lo:+.3f}, {d_hi:+.3f}];"
                   f" ours win share in disagreements [{s_lo:.1%}, {s_hi:.1%}]; ours-with-hype minus hype-off"
                   f" [{h_lo:+.3f}, {h_hi:+.3f}]")
        for scale in (0.0, 0.5, 1.0):
            rs = build_snapshot(N, FC_DATES[N][0], hype_scale=scale)
            vals = []
            for pos in ("QB", "RB", "WR", "TE"):
                x = [r for r in rs if r["pos"] == pos]
                vals.append(spearman([r["ours"] for r in x], [r["target"] for r in x]))
            wa, wb = disagreement(rs, "ours", "fc")
            out.append(f"  hype scale {scale:.1f}: mean Spearman {sum(vals) / 4:.4f}; vs FC disagreements {wa}-{wb}")
    Path(HERE / "backtest_out.txt").write_text("\n".join(out))
    json.dump({f"{k[0]}|{k[1]}": v for k, v in snaps.items()}, open(HERE / "snap_rows.json", "w"))
    print("\n".join(out))
