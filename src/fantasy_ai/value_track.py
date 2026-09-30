"""Forward test: which ranking predicts real fantasy points better, ours or the market's?

`fai track-values <league>` saves one snapshot per league per week to data/value_history/:
every rostered player plus the top free agents with
  ours          rest-of-season value (Player.ros_value, all multipliers)
  ours_nohype   the same value with the hype label multiplier taken back out
  espn_week     ESPN's projection for the upcoming week (before our news adjustment)
  espn_pg       ESPN season projection per game (Player.per_game)
  fc            FantasyCalc redraft market value (None if FC has no row)
  hype label and multiplier
The first snapshot of a week is kept (earliest = no peeking at games); --force replaces it.

`fai score-values <league>` scores every saved snapshot against PPR points actually scored from
the snapshot week on (nflverse fantasy_points_ppr, so no ESPN reads). Per position: Spearman,
top-K hit rate, and a disagreement test: for same-position pairs where two rankings order the
players oppositely, how often each side was right. Everything here is pure except load/save.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from .market import spearman
from .prefs import data_dir
from .usage_data import norm_name

POSITIONS = ("QB", "RB", "WR", "TE")
TOPK = {"QB": (12,), "TE": (12,), "RB": (24, 36), "WR": (24, 36)}
PREDICTORS = ("ours", "ours_nohype", "espn_pg", "fc")
LABELS = {"ours": "ours", "ours_nohype": "ours, hype off", "espn_pg": "ESPN per game", "fc": "FantasyCalc"}
PAIRS = (("ours", "fc"), ("ours_nohype", "fc"), ("espn_pg", "fc"), ("ours", "ours_nohype"))


def history_dir(directory: Path | None = None) -> Path:
    return (directory or data_dir()) / "value_history"


# ---------------------------------------------------------------- snapshot

def snapshot(state, league: str, now: dt.datetime | None = None) -> dict:
    wk = state.scoring_period
    rows = []
    seen = set()
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if p.position not in POSITIONS or p.player_id in seen:
            continue
        seen.add(p.player_id)
        h = getattr(p, "hype", None)
        mult = h.ros_mult if h is not None and h.ros_mult else 1.0
        ours = p.ros_value(wk)
        rows.append({
            "id": p.player_id, "name": p.name, "pos": p.position, "team": p.pro_team,
            "status": p.status, "injury": p.game_status,
            "ours": ours, "ours_nohype": round(ours / mult, 1),
            "espn_week": p.base_proj_week if p.base_proj_week is not None else p.proj_week,
            "espn_pg": round(p.per_game(), 2),
            "fc": getattr(p, "market_value", None), "fc_rank": getattr(p, "market_rank", None),
            "hype": h.label if h is not None else None, "hype_mult": mult,
        })
    return {"league": league, "season": state.year, "week": wk,
            "taken_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
            "players": rows}


def snapshot_path(league: str, season: int, week: int, directory: Path | None = None) -> Path:
    return history_dir(directory) / f"{league}_{season}_wk{week:02d}.json"


def save(snap: dict, directory: Path | None = None, force: bool = False) -> tuple[Path, bool]:
    """Write the snapshot. Returns (path, written). Keeps an existing file unless force."""
    path = snapshot_path(snap["league"], snap["season"], snap["week"], directory)
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=1))
    tmp.replace(path)
    return path, True


def load_all(league: str, season: int | None = None, directory: Path | None = None) -> list[dict]:
    out = []
    for f in sorted(history_dir(directory).glob(f"{league}_*_wk*.json")):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if season is None or s.get("season") == season:
            out.append(s)
    return sorted(out, key=lambda s: (s["season"], s["week"]))


# ---------------------------------------------------------------- actuals

def actuals_from_usage(usage) -> dict[tuple[str, str], dict[int, float]]:
    """(norm name, position) -> {week: PPR points} from usage_data.UsageData.weekly."""
    out: dict[tuple[str, str], dict[int, float]] = {}
    for name, rows in (usage.weekly if usage else {}).items():
        for r in rows:
            out.setdefault((name, r.get("pos")), {})[int(r["week"])] = float(r.get("fpts") or 0)
    return out


def last_week(actuals: dict) -> int:
    return max((w for weeks in actuals.values() for w in weeks), default=0)


# ---------------------------------------------------------------- scoring

def _topk_hit(rows: list[dict], key: str, k: int) -> float | None:
    if len(rows) < int(k * 1.5):
        return None
    pred = {r["id"] for r in sorted(rows, key=lambda r: -r[key])[:k]}
    act = {r["id"] for r in sorted(rows, key=lambda r: -r["actual"])[:k]}
    return round(len(pred & act) / k, 3)


def disagreement(rows: list[dict], a: str, b: str) -> tuple[int, int]:
    """Same-position pairs where a and b disagree on the order; (a right, b right). Actual ties skipped."""
    wa = wb = 0
    for pos in POSITIONS:
        rs = [r for r in rows if r["pos"] == pos and r.get(a) is not None and r.get(b) is not None]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                x, y = rs[i], rs[j]
                da, db, dt_ = x[a] - y[a], x[b] - y[b], x["actual"] - y["actual"]
                if da * db >= 0 or dt_ == 0:
                    continue
                if da * dt_ > 0:
                    wa += 1
                else:
                    wb += 1
    return wa, wb


@dataclass
class Score:
    week: int
    through: int
    n: int
    spearman: dict[str, dict[str, float | None]] = field(default_factory=dict)   # predictor -> pos -> rho
    topk: dict[str, dict[str, float | None]] = field(default_factory=dict)       # predictor -> "RB24" -> hit
    pairs: dict[str, tuple[int, int]] = field(default_factory=dict)              # "ours|fc" -> (wins a, wins b)

    def mean_spearman(self, pred: str) -> float | None:
        vals = [v for v in self.spearman.get(pred, {}).values() if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None


def score_snapshot(snap: dict, actuals: dict, through: int | None = None,
                   horizon: int | None = None) -> Score | None:
    """Score one snapshot. Target = PPR points from snapshot week to `through` (or week + horizon - 1).
    Players who did not play count 0 (missed games are part of what a value must predict).
    Only players with a FantasyCalc value are used, so every predictor ranks the same pool."""
    start = snap["week"]
    end = through if through is not None else last_week(actuals)
    if horizon:
        end = min(end, start + horizon - 1)
    if end < start:
        return None
    rows = []
    for r in snap["players"]:
        if r.get("fc") is None or r["pos"] not in POSITIONS:
            continue
        weeks = actuals.get((norm_name(r["name"]), r["pos"]), {})
        rows.append({**r, "actual": sum(weeks.get(w, 0.0) for w in range(start, end + 1))})
    if not rows:
        return None
    sc = Score(week=start, through=end, n=len(rows))
    for pred in PREDICTORS:
        sc.spearman[pred] = {}
        sc.topk[pred] = {}
        for pos in POSITIONS:
            rs = [r for r in rows if r["pos"] == pos and r.get(pred) is not None]
            sc.spearman[pred][pos] = spearman([r[pred] for r in rs], [r["actual"] for r in rs])
            for k in TOPK[pos]:
                sc.topk[pred][f"{pos}{k}"] = _topk_hit(rs, pred, k)
    for a, b in PAIRS:
        sc.pairs[f"{a}|{b}"] = disagreement(rows, a, b)
    return sc


def format_scores(scores: list[Score]) -> list[str]:
    lines = []
    tot = {k: [0, 0] for k in (f"{a}|{b}" for a, b in PAIRS)}
    for sc in scores:
        lines.append(f"\nSnapshot week {sc.week}, scored on weeks {sc.week}-{sc.through} ({sc.n} players with an FC value)")
        lines.append(f"  {'Spearman vs points':20} " + " ".join(f"{p:>6}" for p in POSITIONS) + f" {'mean':>6}")
        for pred in PREDICTORS:
            vals = [sc.spearman[pred][p] for p in POSITIONS]
            lines.append(f"  {LABELS[pred]:20} " + " ".join(f"{v:6.3f}" if v is not None else "   n/a" for v in vals)
                         + f" {sc.mean_spearman(pred) if sc.mean_spearman(pred) is not None else float('nan'):6.3f}")
        lines.append("  top-K hit rate:")
        for pred in PREDICTORS:
            bits = [f"{k} {v:.2f}" if v is not None else f"{k} n/a" for k, v in sc.topk[pred].items()]
            lines.append(f"    {LABELS[pred]:18} " + "  ".join(bits))
        lines.append("  disagreements (same-position pairs ordered oppositely; who was right):")
        for key, (wa, wb) in sc.pairs.items():
            a, b = key.split("|")
            share = f"{wa / (wa + wb):.0%}" if wa + wb else "n/a"
            lines.append(f"    {LABELS[a]:15} {wa:5d}  vs  {LABELS[b]:15} {wb:5d}   ({share} for {LABELS[a]})")
            tot[key][0] += wa
            tot[key][1] += wb
    if len(scores) > 1:
        lines.append("\nAll snapshots pooled (overlapping weeks, so not independent):")
        for key, (wa, wb) in tot.items():
            a, b = key.split("|")
            share = f"{wa / (wa + wb):.0%}" if wa + wb else "n/a"
            lines.append(f"    {LABELS[a]:15} {wa:5d}  vs  {LABELS[b]:15} {wb:5d}   ({share} for {LABELS[a]})")
    if scores:
        lines.append("\nSmall samples: a few weeks of points are noisy. Treat gaps under ~0.05 Spearman "
                     "or ~55/45 in disagreements as a tie.")
    return lines
