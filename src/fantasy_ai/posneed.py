"""Position-need report: is this slot actually a problem?

For a team and position: its current starter(s) (best season lineup, dedicated slots), each with
the next 3 weeks (worth, opponent, ESPN matchup rank, bye), usage trend (trend.py), injury
status, rest-of-season value, and rank among all starters at that position in this league.
Verdict per starter:
  real problem  bottom 3 at the position, falling usage trend, injured, or on bye in 2 weeks
  strength      top 3 (and none of the above)
  fine          everything else
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .lineup import best_assignment
from .slots import NON_STARTING

SLOT = {"QB": 0, "RB": 2, "WR": 4, "TE": 6}
POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass
class StarterView:
    team_id: int
    pos: str
    player: object
    weeks: list = field(default_factory=list)        # (week, worth, opp, opp_rank, bye)
    rank: int = 0
    of: int = 0
    verdict: str = "fine"
    why: list = field(default_factory=list)


def _season(p) -> float:
    return p.season_rate()


def starters(team, state, pos: str) -> list:
    """His CURRENT starter(s): players in the position's dedicated lineup slot right now. Falls back
    to the best season lineup when the slot is empty (e.g. mid-week before lineups are set)."""
    now = [p for p in team.roster if p.lineup_slot == SLOT[pos] and p.position == pos]
    if now:
        return sorted(now, key=_season, reverse=True)
    starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}
    active = [p for p in team.roster if not p.on_ir_slot]
    _, assign = best_assignment(active, starting, lambda p, s: _season(p) + 1e-6)
    by_id = {p.player_id: p for p in active}
    return sorted([by_id[pid] for pid, slot in assign.items() if slot == SLOT[pos]], key=_season, reverse=True)


def _all_starters(state, pos: str) -> list:
    cache = getattr(state, "_posneed_cache", None)
    if cache is None:
        cache = state._posneed_cache = {}
    if pos not in cache:
        cache[pos] = sorted([p for t in state.teams.values() for p in starters(t, state, pos)], key=_season, reverse=True)
    return cache[pos]


def _opp(state, team: str, k: int):
    ctx = state.context
    if ctx is None:
        return None
    games = ctx.games if k == 0 else getattr(ctx, "future_games", {}).get(state.scoring_period + k, {})
    g = games.get(team)
    return g.opp if g else None


def view(state, team_id: int, pos: str) -> list[StarterView]:
    from .waivers import merged, week_worth
    cfg = merged(None)
    team = state.teams[team_id]
    league = _all_starters(state, pos)
    out = []
    wk = state.scoring_period
    for p in starters(team, state, pos):
        sv = StarterView(team_id, pos, p, rank=league.index(p) + 1 if p in league else len(league), of=len(league))
        for k in range(3):
            opp = _opp(state, p.pro_team, k)
            rank = state.context.def_rank.get((opp, pos)) if (state.context is not None and opp) else None
            sv.weeks.append((wk + k, week_worth(p, state, k, cfg), opp, rank, p.bye_week == wk + k))
        t = getattr(p, "trend", None)
        bad = []
        if sv.rank > sv.of - 3:
            bad.append(f"bottom 3 ({pos} {sv.rank} of {sv.of})")
        if t is not None and t.label == "downtrend":
            bad.append("usage falling")
        if not p.is_healthy():
            bad.append(p.game_status.lower().replace("_", " "))
        if p.bye_week in (wk, wk + 1):
            bad.append(f"bye week {p.bye_week}")
        if bad:
            sv.verdict, sv.why = "real problem", bad
        elif sv.rank <= 3:
            sv.verdict, sv.why = "strength", [f"top 3 ({pos} {sv.rank} of {sv.of})"]
        else:
            sv.verdict, sv.why = "fine", [f"{pos} {sv.rank} of {sv.of}"]
        out.append(sv)
    return out


def _wk(w) -> str:
    week, worth, opp, rank, bye = w
    if bye:
        return f"wk{week} BYE"
    where = f" vs {opp}" if opp else ""
    rk = ""
    if rank:
        rk = f" ({'tough' if rank <= 10 else 'soft' if rank >= 23 else 'avg'} {rank})"
    return f"wk{week} {worth:.1f}{where}{rk}"


def lines(state, team_id: int, pos: str, who: str = "ours", short: bool = False) -> list[str]:
    from .experts import note
    out = []
    for sv in view(state, team_id, pos):
        p = sv.player
        head = f"{pos} ({who}): {p.name}, {sv.verdict.upper()} ({'; '.join(sv.why)})"
        if short:
            out.append(head)
            continue
        t = getattr(p, "trend", None)
        out.append(head)
        out.append(f"    next 3: {', '.join(_wk(w) for w in sv.weeks)}")
        bits = [t.text if t is not None else "trend: n/a (QB, or under 3 healthy games)",
                p.game_status.lower().replace("_", " ") if not p.is_healthy() else "healthy",
                f"ROS {p.ros_value(state.scoring_period):.0f}"]
        ecr = note(p, state)
        if ecr:
            bits.append(ecr)
        out.append("    " + "; ".join(bits))
    return out


def summary(state, team_id: int, pos: str) -> str:
    """One short line for texts: 'TE fine (Loveland TE 6/10)'."""
    vs = view(state, team_id, pos)
    if not vs:
        return f"{pos}: no starter"
    worst = min(vs, key=lambda v: {"real problem": 0, "fine": 1, "strength": 2}[v.verdict])
    return f"{pos} {worst.verdict} ({worst.player.name} {pos} {worst.rank}/{worst.of})"
