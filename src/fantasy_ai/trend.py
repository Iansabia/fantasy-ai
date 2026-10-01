"""Usage-trend momentum with data-fitted weights (scripts/fit_trend.py -> trend_fit.json).

Feature (RB / WR / TE, injury-limited games left out): his last 2 games minus his earlier games
this season, in share points (0.10 = 10 points):
  d_primary  target share (WR/TE) or carry share (RB)
  d_snap     offense snap %
  d_rz       red-zone opportunity share
Fitted on 2023-25 (6,749 player-games, held out by season): predicted change in PPR points per
game over the next 3 games and rest of season, relative to his season-to-date average:
  WR/TE  next 3: +10 pt target share -> +5.5%, +10 pt snaps -> +3.0%, red zone ~0
         ROS:    +10 pt target share -> +3.2%, +10 pt snaps -> +3.2%, +10 pt red zone -> +1.7%
  RB     next 3: +10 pt snaps -> +7.1%, carry share ~0 once snaps are known, red zone +0.8%
         ROS:    +10 pt snaps -> +6.0%, red zone +1.4%
Out-of-sample it explains 1-5% of the variance in every held-out season: real but small, so the
effect stays small. ESPN's week-4 projections did not already move with this feature (slope
-0.4 on 2026 data), so it is applied on top of ESPN at full weight.
With fewer than 2 earlier games the effect is scaled by n_earlier / 2; it is clipped to +-15%
(the range of the fitted bins).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .usage_data import UsageData, norm_name

FIT = json.loads((Path(__file__).parent / "trend_fit.json").read_text())
CLIP = 0.15
UP, DOWN = 0.05, -0.05          # next-3 effect that counts as an uptrend / downtrend
HARD_UP = 0.08                  # "trending up hard": waivers need a clear win to drop him


@dataclass
class Trend:
    d_primary: float
    d_snap: float
    d_rz: float
    next3: float          # multiplier on future-week worth (1.05 = +5%)
    ros: float            # multiplier on rest-of-season value
    n_earlier: int
    primary_name: str
    recent: dict
    earlier: dict

    @property
    def label(self) -> str:
        e = self.next3 - 1
        return "uptrend" if e >= UP else ("downtrend" if e <= DOWN else "flat")

    @property
    def text(self) -> str:
        pct = lambda x: f"{x * 100:.0f}%"
        bits = [f"{self.primary_name} {pct(self.earlier['primary'])}->{pct(self.recent['primary'])}",
                f"snaps {pct(self.earlier['snap'])}->{pct(self.recent['snap'])}"]
        if abs(self.d_rz) >= 0.05:
            bits.append(f"red zone {pct(self.earlier['rz'])}->{pct(self.recent['rz'])}")
        return f"{self.label} ({self.next3 - 1:+.0%} next 3 wks): " + ", ".join(bits)


def _effect(coef: list[float], d: tuple[float, float, float]) -> float:
    return sum(c * x for c, x in zip(coef[1:], d))       # intercept left out: no trend = no change


def compute(p, usage: UsageData | None, week: int) -> Trend | None:
    if p.position not in ("RB", "WR", "TE") or usage is None:
        return None
    from .hype import injury_filtered
    rows = [r for r in usage.weekly.get(norm_name(p.name), []) if r["week"] < week and r.get("snap_pct") is not None]
    rows, _ = injury_filtered(rows, norm_name(p.name), usage)
    if len(rows) < 3:
        return None
    prim = "carry_share" if p.position == "RB" else "target_share"
    recent, earlier = rows[-2:], rows[:-2]
    m = lambda key, xs: sum(float(r.get(key) or 0) for r in xs) / len(xs)
    rv = {"primary": m(prim, recent), "snap": m("snap_pct", recent), "rz": m("rz_share", recent)}
    ev = {"primary": m(prim, earlier), "snap": m("snap_pct", earlier), "rz": m("rz_share", earlier)}
    d = (rv["primary"] - ev["primary"], rv["snap"] - ev["snap"], rv["rz"] - ev["rz"])
    g = FIT["groups"]["RB" if p.position == "RB" else "WRTE"]
    scale = min(1.0, len(earlier) / 2)
    e3 = max(-CLIP, min(CLIP, _effect(g["y3"]["coef"], d) * scale))
    er = max(-CLIP, min(CLIP, _effect(g["yros"]["coef"], d) * scale))
    return Trend(d[0], d[1], d[2], round(1 + e3, 3), round(1 + er, 3), len(earlier),
                 "carry share" if prim == "carry_share" else "target share", rv, ev)


def apply(state, usage: UsageData | None) -> int:
    """Attach p.trend and fold the fitted ROS effect into ros_multiplier. Returns players changed."""
    n = 0
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        t = compute(p, usage, state.scoring_period)
        p.trend = t
        if t is not None:
            p.ros_multiplier = round(p.ros_multiplier * t.ros, 3)
            n += 1
    return n


def next3(p) -> float:
    t = getattr(p, "trend", None)
    return t.next3 if t is not None else 1.0


def label(p) -> str | None:
    t = getattr(p, "trend", None)
    return t.label if t is not None else None


def _count(n: int) -> str:
    return {1: "a player", 2: "two players", 3: "three players"}.get(n, f"{n} players")


def trade_note(sends, receives) -> str | None:
    """'sells two players on an uptrend (A, B); buys a player on a downtrend (C)'."""
    bits = []
    for verb, ps in (("sells", sends), ("buys", receives)):
        for lab in ("uptrend", "downtrend"):
            hit = [p for p in ps if label(p) == lab]
            if hit:
                bits.append(f"{verb} {_count(len(hit))} on an {lab} ({', '.join(f'{p.name} {next3(p) - 1:+.0%}' for p in hit)})"
                            if lab == "uptrend" else
                            f"{verb} {_count(len(hit))} on a {lab} ({', '.join(f'{p.name} {next3(p) - 1:+.0%}' for p in hit)})")
    return "; ".join(bits) or None
