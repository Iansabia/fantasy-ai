"""Hype vs substance: is a player's rise real usage, a temporary fill-in role, or just buzz?

Deterministic. The LLM only labels news upstream (news/); this module reads numbers.

Substance (0-100) from nflverse usage (usage_data.py):
  primary share = target share (WR/TE) or carry share (RB), last 2 games vs season baseline
  (earlier games, else last season), snap % change, volume level (expected points per game),
  consecutive weekly increases, and role signals from news ("role up/down").
Vacated opportunity: if a same-position teammate was out in the spike games, check his status
now. Back (ACTIVE/QUESTIONABLE/PROBABLE) = "Fill-in", jump discounted 75%. Out long term (IR) =
keep most of it. Nobody out = the room was healthy, so the role change is real.
Production vs volume: points far above expected (xFP) = "Sell high"; strong volume with points
lagging = "Buy low".
Hype (0-100): roster % change and trend, Sleeper trending / news volume (Player.buzz).

Labels: Real breakout, Role growing, Fill-in (X returns), Hype only, Buy low, Sell high, Steady, Fading.
Each label maps to a bounded multiplier on rest-of-season value and on future-week worth
(waivers, trades); config `hype:` can override the table.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .usage_data import UsageData, norm_name, nfl_team

MULTS = {   # label -> (ros multiplier, future-week multiplier)
    "Real breakout": (1.15, 1.15), "Role growing": (1.08, 1.08), "Fill-in": (0.85, 0.60),
    "Hype only": (0.90, 0.95), "Buy low": (1.05, 1.05), "Sell high": (0.93, 0.95),
    "Steady": (1.0, 1.0), "Fading": (0.90, 0.92),
}
RECEIVERS, BACKS = ("WR", "TE"), ("RB",)
BACK_STATUSES = {"ACTIVE", "NORMAL", "QUESTIONABLE", "PROBABLE", "DAY_TO_DAY", ""}


@dataclass
class HypeResult:
    substance: int
    hype: int
    label: str
    reasons: list[str] = field(default_factory=list)
    ros_mult: float = 1.0
    future_mult: float = 1.0

    @property
    def text(self) -> str:
        return f"{self.label} (substance {self.substance}, hype {self.hype})"


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _pct(x):
    return f"{x * 100:.0f}%"


def _status_now(name: str, state) -> str | None:
    key = norm_name(name)
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if norm_name(p.name) == key:
            return (p.injury_status or "ACTIVE").upper()
    return None


def _vacated(p, games_recent: list[dict], usage: UsageData, primary: str, state):
    """The most important same-group teammate missing in the spike games, and his status now."""
    group = RECEIVERS if p.position in RECEIVERS else BACKS
    team = games_recent[-1]["team"] if games_recent else nfl_team(p.pro_team)
    spike_weeks = {g["week"] for g in games_recent}
    me = norm_name(p.name)
    best = None
    for name, rows in usage.weekly.items():
        if name == me:
            continue
        mine = [r for r in rows if r["team"] == team and r["pos"] in group]
        if not mine:
            continue
        earlier = [r[primary] for r in mine if r["week"] < min(spike_weeks)]
        base = _mean(earlier) or 0
        played = {r["week"] for r in mine}
        reported_out = any(usage.injuries.get((team, w), {}).get(name) == "OUT" for w in spike_weeks)
        missed = bool(spike_weeks - played) and base >= 0.12
        if (reported_out or missed) and base >= 0.08 and (best is None or base > best[1]):
            best = (name, base)
    if best is None:
        return None
    status = _status_now(best[0], state)
    return best[0], best[1], status


def compute(p, usage: UsageData | None, state) -> HypeResult:
    wk = state.scoring_period
    own_w = getattr(p, "own_week", None) or 0.0
    own_3 = getattr(p, "own_3wk", None) or 0.0
    hype = int(_clamp(15 + _clamp(own_w * 2, -10, 40) + 40 * (p.buzz or 0) + (10 if own_3 >= 15 else 0), 0, 100))
    hype_bits = []
    if own_w:
        hype_bits.append(f"roster% {own_w:+.0f} this week")
    for r in p.reasons:
        if "Sleeper" in r:
            hype_bits.append(r.split(" (")[0].replace(f"{p.name} ", ""))
            break

    rows = (usage.weekly.get(norm_name(p.name), []) if usage else [])
    games = [r for r in rows if r["week"] < wk]
    if p.position not in RECEIVERS + BACKS or not games:
        label = "Fading" if own_3 <= -15 else ("Hype only" if hype >= 65 else "Steady")
        reasons = (["no usage data yet"] if p.position in RECEIVERS + BACKS else []) + hype_bits[:1]
        ros, fut = MULTS[label]
        return HypeResult(40 if not games else 50, hype, label, reasons, ros, fut)

    primary = "target_share" if p.position in RECEIVERS else "carry_share"
    recent = games[-2:]
    base_games = games[:-2]
    recent_v = _mean([g[primary] for g in recent])
    base_v = _mean([g[primary] for g in base_games])
    base_src = "earlier games"
    if base_v is None:
        last = usage.last_season.get(norm_name(p.name)) if usage else None
        if last:
            base_v = last["target_share"] if primary == "target_share" else min(1.0, last["carries_pg"] / 26)
            base_src = "last season"
    jump = (recent_v - base_v) if base_v is not None else 0.0
    snap_r, snap_b = _mean([g["snap_pct"] for g in recent]), _mean([g["snap_pct"] for g in base_games])
    snap_jump = (snap_r - snap_b) if (snap_r is not None and snap_b is not None) else 0.0
    streak = 0
    for a, b in zip(games[::-1][1:], games[::-1]):
        if b[primary] > a[primary]:
            streak += 1
        else:
            break
    xfp_pg = _mean([g["xfp"] for g in games[-3:]])
    fpts_pg = _mean([g["fpts"] for g in games[-3:]])

    reasons: list[str] = []
    label_metric = "target share" if primary == "target_share" else "carry share"
    if base_v is not None and abs(jump) >= 0.03:
        reasons.append(f"{label_metric} {_pct(base_v)} -> {_pct(recent_v)} (last {len(recent)} vs {base_src})")
    if abs(snap_jump) >= 0.08:
        reasons.append(f"snaps {_pct(snap_b)} -> {_pct(snap_r)}")
    if primary == "target_share":
        t0, t1 = (_mean([g['targets'] for g in base_games]), _mean([g['targets'] for g in recent]))
        if t0 is not None and abs(t1 - t0) >= 2:
            reasons.insert(0, f"targets {t0:.0f} -> {t1:.0f} per game")

    sub_jump = _clamp(jump * 250, -40, 40) + _clamp(snap_jump * 60, -15, 15)
    vac = _vacated(p, recent, usage, primary, state) if jump > 0.05 else None
    fill_in = False
    usage_reasons, reasons = reasons, []          # context reasons below get a guaranteed spot
    if vac:
        who, who_share, status = vac
        who_disp = who.title()
        if status is None:
            sub_jump *= 0.5
            reasons.append(f"{who_disp} ({_pct(who_share)} share) missed those games; status unknown")
        elif status in BACK_STATUSES:
            sub_jump *= 0.25
            fill_in = True
            reasons.append(f"{who_disp} was out and is {status.lower()} now: fill-in role")
        else:
            sub_jump *= 0.8
            reasons.append(f"{who_disp} is {status.replace('_', ' ').lower()}: the extra work should stick")
    elif jump > 0.05:
        reasons.append(f"{'WR/TE room' if primary == 'target_share' else 'backfield'} was healthy, so the role change is real")

    role = 0
    for r in p.reasons:
        if " role up" in r:
            role, _ = 8, reasons.append(r)
        elif " role down" in r:
            role, _ = -8, reasons.append(r)
    vol = 0
    if xfp_pg is not None:
        vol = 10 if xfp_pg >= 12 else (-10 if xfp_pg <= 4 else 0)
    substance = int(_clamp(50 + sub_jump + vol + role + (5 if streak >= 2 and jump > 0 else 0), 0, 100))

    sell = buy = False
    if xfp_pg is not None and fpts_pg is not None and xfp_pg > 0:
        if fpts_pg - xfp_pg >= 5 and fpts_pg / xfp_pg >= 1.4:
            sell = True
            reasons.append(f"{fpts_pg:.1f} pts vs {xfp_pg:.1f} expected per game: TD or big-play driven")
        elif xfp_pg >= 10 and fpts_pg / xfp_pg <= 0.75:
            buy = True
            reasons.append(f"{xfp_pg:.1f} expected pts per game but only {fpts_pg:.1f} scored: volume is there")

    injured = p.is_out() or p.ir_eligible() or p.news_out
    still_big = recent_v is not None and recent_v >= (0.50 if primary == "carry_share" else 0.20)
    if injured and jump < 0:
        reasons.insert(0, f"{p.game_status.lower().replace('_', ' ')}: usage drop reflects the injury")
    if fill_in:
        label = "Fill-in"
    elif sell and substance < 70:
        label = "Sell high"
    elif substance >= 72 and jump >= 0.08:
        label = "Real breakout"
    elif substance >= 60 and jump > 0.03:
        label = "Role growing"
    elif injured and jump < 0:
        label = "Steady"                     # injured: a usage drop says nothing about his role
    elif (substance <= 38 or own_3 <= -15) and jump < -0.05 and not still_big:
        label = "Fading"                     # falling usage beats a volume-based buy-low call
    elif buy:
        label = "Buy low"
    elif hype >= 60 and substance < 58:
        label = "Hype only"
        reasons.insert(0, "usage is flat" if abs(jump) < 0.03 else "usage does not back it up")
    elif (substance <= 38 or own_3 <= -15) and not still_big:
        label = "Fading"
    else:
        label = "Steady"
    if label == "Fill-in":
        who = vac[0].title()
        label_text = f"Fill-in ({who} returns)"
    else:
        label_text = label
    ros, fut = MULTS[label]
    if label == "Hype only":
        reasons += hype_bits[:1]
    final = usage_reasons[:2] + reasons[:1] + usage_reasons[2:] + reasons[1:]
    if label == "Hype only":
        final = reasons[:1] + usage_reasons[:1] + reasons[1:]
    return HypeResult(substance, hype, label_text, final[:3], ros, fut)


def apply(state, usage: UsageData | None, overrides: dict | None = None) -> None:
    """Attach p.hype to every player and fold the label multiplier into ros_multiplier."""
    if overrides:
        for k, v in overrides.items():
            if k in MULTS and isinstance(v, (list, tuple)) and len(v) == 2:
                MULTS[k] = (float(v[0]), float(v[1]))
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        h = compute(p, usage, state)
        p.hype = h
        p.ros_multiplier = round(p.ros_multiplier * h.ros_mult, 3)


def line(p) -> str | None:
    """'Kalif Raymond: role growing, targets 3 -> 7 per game; WR/TE room was healthy'."""
    h = getattr(p, "hype", None)
    if h is None:
        return None
    return f"{p.name}: {h.label.lower()}" + (f", {'; '.join(h.reasons[:2])}" if h.reasons else "")
