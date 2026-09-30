"""Team-need trade engine and scouting.

Value to a team = what its best lineups are worth, not the sum of its players:
  team_value(roster) = best lineup over the next 3 weeks (factor-adjusted worth, byes)
                     + best lineup by rest-of-season per-game value x remaining weeks
                     + small depth credit for the first backups (1 QB, 2 RB, 2 WR, 1 TE;
                       +1 QB in superflex), counted ABOVE REPLACEMENT (vorp.py), so a backup QB
                       in a 1-QB league adds ~nothing: any team can find a similar QB on waivers
A trade's gain for a side is team_value(after) - team_value(before), computed on THAT team's
roster, so surplus (a 3rd QB in a 1-QB league) is worth little to us and a lot to a team whose
QB is hurt or on bye.

Scouting profile per team: positional strength rank vs the league, holes, surplus (startable
elsewhere but beyond their own need), short-term desperation (starters on bye / out in the next
2 weeks), and record.

Trade ideas: 1-for-1, 2-for-1 and 2-for-2 with every team, kept when our gain is clearly
positive, their gain is not clearly negative, and market value (FantasyCalc, else our ROS) is
close to fair (FantasyCalc; else our value above replacement). Ranked by our gain x acceptance
(their gain + need match + fairness).
Ideas are only ever queued for approval on request; nothing is proposed automatically.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .lineup import best_assignment
from .market import fc_value
from .models import SEASON_WEEKS, LeagueState, Player
from .slots import NON_STARTING
from .vorp import vorp_pg, vorp_ros
from .waivers import merged as waiver_cfg, week_worth

POSITIONS = ("QB", "RB", "WR", "TE")
H = 3
DEPTH_CREDIT = 0.15


def _need(state: LeagueState) -> dict[str, float]:
    c = state.slot_counts
    flex = c.get(23, 0)
    return {"QB": c.get(0, 0) + c.get(7, 0), "RB": c.get(2, 0) + flex / 2, "WR": c.get(4, 0) + flex / 2,
            "TE": c.get(6, 0)}


def _backups(state: LeagueState) -> dict[str, int]:
    return {"QB": 1 + (1 if state.slot_counts.get(7) else 0), "RB": 2, "WR": 2, "TE": 1}


def season_value(p: Player) -> float:
    return p.per_game() * p.ros_multiplier


def team_value(roster: list[Player], state: LeagueState, cache: dict | None = None) -> float:
    cache = {} if cache is None else cache
    key = frozenset(p.player_id for p in roster)
    if key in cache:
        return cache[key]
    cfg = waiver_cfg(None)
    starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}
    active = [p for p in roster if not p.on_ir_slot]
    near = 0.0
    for k in range(H):
        vals = {p.player_id: week_worth(p, state, k, cfg) for p in active}
        t, _ = best_assignment([p for p in active if vals[p.player_id] > 0], starting,
                               lambda p, s: vals[p.player_id] + 1e-6)
        near += t
    weeks_after = max(0, SEASON_WEEKS - state.scoring_period + 1 - H)
    t, assign = best_assignment(active, starting, lambda p, s: season_value(p) + 1e-6)
    far = t * weeks_after
    depth = 0.0
    starters = set(assign)
    for pos, n in _backups(state).items():
        bench = sorted([p for p in active if p.position == pos and p.player_id not in starters],
                       key=season_value, reverse=True)[:n]
        depth += sum(max(0.0, vorp_pg(p, state)) for p in bench) * DEPTH_CREDIT \
            * max(1, SEASON_WEEKS - state.scoring_period + 1)
    v = round(near + far + depth, 1)
    cache[key] = v
    return v


# ---------------------------------------------------------------- scouting

@dataclass
class Profile:
    team_id: int
    name: str
    record: str
    strength: dict[str, float]
    rank: dict[str, int]
    holes: list[str]
    surplus: list[Player]
    desperation: list[str]
    injured_starters: list[str] = field(default_factory=list)


def profiles(state: LeagueState) -> dict[int, Profile]:
    need = _need(state)
    wk = state.scoring_period
    strength: dict[int, dict[str, float]] = {}
    tops: dict[int, dict[str, list[Player]]] = {}
    for tid, t in state.teams.items():
        strength[tid], tops[tid] = {}, {}
        for pos in POSITIONS:
            n = max(1, round(need[pos] + 0.49))
            best = sorted([p for p in t.roster if p.position == pos and not p.on_ir_slot and not p.ir_eligible()],
                          key=season_value, reverse=True)
            tops[tid][pos] = best
            strength[tid][pos] = round(sum(season_value(p) for p in best[:n]), 1)
    n_teams = len(state.teams)
    # startable elsewhere: at least the median starter at the position across the league
    med = {}
    for pos in POSITIONS:
        n = max(1, round(need[pos] + 0.49))
        vals = sorted(season_value(p) for tid in tops for p in tops[tid][pos][:n])
        med[pos] = vals[len(vals) // 2] if vals else 0
    out = {}
    for tid, t in state.teams.items():
        rank = {pos: 1 + sum(1 for o in strength if strength[o][pos] > strength[tid][pos]) for pos in POSITIONS}
        holes = [pos for pos in POSITIONS if rank[pos] > n_teams * 2 / 3]
        # surplus: not in their best lineup, beyond the first backup(s) at the position,
        # and good enough to start for someone else
        starting = {sl: c for sl, c in state.slot_counts.items() if sl not in NON_STARTING}
        _, assign = best_assignment([p for p in t.roster if not p.on_ir_slot], starting,
                                    lambda p, sl: season_value(p) + 1e-6)
        backups = {"QB": 1 + (1 if state.slot_counts.get(7) else 0), "RB": 1, "WR": 1, "TE": 1}
        surplus = []
        for pos in POSITIONS:
            bench = [p for p in tops[tid][pos] if p.player_id not in assign]
            surplus += [p for p in bench[backups[pos]:] if season_value(p) >= med[pos] * 0.8]
        surplus.sort(key=lambda p: vorp_pg(p, state), reverse=True)     # most tradeable value first
        desperate = []
        for pos in POSITIONS:
            n = max(1, round(need[pos] + 0.49))
            for p in tops[tid][pos][:n]:
                for k in range(0, 2):
                    if p.bye_week == wk + k:
                        desperate.append(f"{pos} {p.name} on bye week {wk + k}")
                if p.is_out() or p.news_out:
                    desperate.append(f"{pos} {p.name} {p.game_status.lower()}")
        wins, losses = getattr(t, "wins", 0), getattr(t, "losses", 0)
        out[tid] = Profile(tid, t.name.strip(), f"{wins}-{losses}", strength[tid], rank, holes, surplus, desperate)
    return out


def describe(pf: Profile, n_teams: int) -> str:
    ranks = ", ".join(f"{pos} {pf.rank[pos]}/{n_teams}" for pos in POSITIONS)
    bits = [f"{pf.name} ({pf.record}): {ranks}"]
    if pf.holes:
        bits.append("holes: " + ", ".join(pf.holes))
    if pf.surplus:
        bits.append("surplus: " + ", ".join(f"{p.name} ({p.position})" for p in pf.surplus[:4]))
    if pf.desperation:
        bits.append("short-term need: " + "; ".join(pf.desperation[:3]))
    return " | ".join(bits)


def strength_summary(pf: Profile, n_teams: int) -> list[str]:
    out = []
    for pos in POSITIONS:
        r = pf.rank[pos]
        word = "strong" if r <= max(1, n_teams / 3) else ("weak" if r > n_teams * 2 / 3 else "average")
        if any(p.position == pos for p in pf.surplus):
            word = "surplus" if r <= n_teams / 2 else word + ", some surplus"
        out.append(f"{pos}: {word}, rank {r} of {n_teams}")
    return out


def surplus_matches(state: LeagueState, prof: dict[int, Profile]) -> list[str]:
    """'Best partner for our QB surplus: Team X (9th of 10 at QB; QB on bye week 5)'."""
    me = prof[state.my_team_id]
    n = len(state.teams)
    lines = []
    for pos in sorted({p.position for p in me.surplus}):
        cands = sorted([pf for tid, pf in prof.items() if tid != state.my_team_id],
                       key=lambda pf: (-(pos in pf.holes), -sum(pos in d for d in pf.desperation), -pf.rank[pos]))
        if cands:
            b = cands[0]
            why = [f"{b.rank[pos]} of {n} at {pos}"] + [d for d in b.desperation if d.startswith(pos)][:1]
            lines.append(f"Best partner for our {pos} surplus: {b.name} ({'; '.join(why)})")
    return lines


# ---------------------------------------------------------------- trade ideas

@dataclass
class Idea:
    partner_id: int
    partner: str
    give: list[Player]
    get: list[Player]
    our_gain: float
    their_gain: float
    fc_give: float | None
    fc_get: float | None
    fairness: float
    accept: float
    score: float
    why_partner: str
    pitch: str


def _market(ps: list[Player], state) -> float | None:
    vals = [fc_value(p) for p in ps]
    return sum(vals) if all(v is not None for v in vals) and vals else None


def _fair(give, get, state) -> tuple[float, float | None, float | None]:
    fg, ft = _market(give, state), _market(get, state)
    if fg and ft:
        return min(fg, ft) / max(fg, ft), fg, ft
    a = sum(max(0.0, vorp_ros(p, state)) for p in give)
    b = sum(max(0.0, vorp_ros(p, state)) for p in get)
    return (min(a, b) / max(a, b) if max(a, b) > 0 else 0.0), fg, ft


def trade_ideas(state: LeagueState, prefs=None, top: int = 8, min_gain: float = 5.0,
                max_their_loss: float = 3.0, min_fair: float = 0.75, pool: int = 5) -> list[Idea]:
    cache: dict = {}
    prof = profiles(state)
    me = state.my_team
    n = len(state.teams)
    base_me = team_value(me.roster, state, cache)
    protected = (lambda p: prefs is not None and (prefs.keeps(p.name) or prefs.no_trade(p.name)))
    mine = [p for p in me.roster if not p.on_ir_slot and not p.locked and not protected(p)
            and p.position in POSITIONS]

    def marginal_loss(p):   # how much we lose without him (cheap first pass)
        return base_me - team_value([q for q in me.roster if q.player_id != p.player_id], state, cache)

    give_pool = sorted(mine, key=lambda p: (marginal_loss(p) - 0.02 * (fc_value(p) or max(0.0, vorp_ros(p, state)))))[:pool]
    ideas: list[Idea] = []
    for tid, t in state.teams.items():
        if tid == state.my_team_id:
            continue
        pf = prof[tid]
        base_them = team_value(t.roster, state, cache)
        theirs = [p for p in t.roster if not p.on_ir_slot and not p.locked and p.position in POSITIONS]

        def our_add(p):
            return team_value(me.roster + [p], state, cache) - base_me

        get_pool = sorted(theirs, key=our_add, reverse=True)[:pool]
        packages = [([g], [r]) for g in give_pool for r in get_pool]
        packages += [(list(g2), [r]) for g2 in itertools.combinations(give_pool, 2) for r in get_pool]
        packages += [(list(g2), list(r2)) for g2 in itertools.combinations(give_pool, 2)
                     for r2 in itertools.combinations(get_pool, 2)]
        for give, get in packages:
            g_ids, r_ids = {p.player_id for p in give}, {p.player_id for p in get}
            ours_after = [p for p in me.roster if p.player_id not in g_ids] + get
            theirs_after = [p for p in t.roster if p.player_id not in r_ids] + give
            if len(ours_after) > len(me.roster) and _over_limit(ours_after, state):
                continue       # 1-for-2 into a full roster would need a drop; keep ideas simple
            our_gain = round(team_value(ours_after, state, cache) - base_me, 1)
            if our_gain < min_gain:
                continue
            their_gain = round(team_value(theirs_after, state, cache) - base_them, 1)
            if their_gain < -max_their_loss:
                continue
            fair, fg, ft = _fair(give, get, state)
            if fair < min_fair:
                continue
            need_match = 1.0 if any(p.position in pf.holes or any(d.startswith(p.position) for d in pf.desperation)
                                    for p in give) else 0.0
            accept = round(0.4 * max(0.0, min(1.0, (their_gain + max_their_loss) / 15))
                           + 0.3 * need_match + 0.3 * fair, 2)
            score = round(our_gain * (0.3 + 0.7 * accept), 1)
            why = _why_partner(pf, give, get, n)
            ideas.append(Idea(tid, t.name, give, get, our_gain, their_gain, fg, ft, round(fair, 2),
                              accept, score, why, _pitch(pf, give, get, their_gain, n)))
    ideas.sort(key=lambda i: i.score, reverse=True)
    out, used = [], set()
    for i in ideas:                      # avoid near-duplicate ideas around the same players
        key = (i.partner_id, frozenset(p.player_id for p in i.get))
        if key in used:
            continue
        used.add(key)
        out.append(i)
    return out[:top]


def _over_limit(roster, state) -> bool:
    from .slots import IR
    limit = sum(c for s, c in state.slot_counts.items() if s != IR)
    return sum(1 for p in roster if not p.on_ir_slot) > limit


def _why_partner(pf: Profile, give, get, n) -> str:
    bits = []
    for p in give:
        bits.append(f"{pf.name} is {pf.rank[p.position]} of {n} at {p.position}")
        d = [x for x in pf.desperation if x.startswith(p.position)]
        if d:
            bits.append(d[0])
    for p in get:
        if p in pf.surplus:
            bits.append(f"{p.name} is surplus for them")
    seen, out = set(), []
    for b in bits:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return "; ".join(out[:3])


def _pitch(pf: Profile, give, get, their_gain, n) -> str:
    """One or two clean sentences: the ask, then the reason it helps them."""
    g = " and ".join(p.name for p in give)
    r = " and ".join(p.name for p in get)
    ask = f"Hey {pf.name}, would you trade {r} for {g}?"
    helps = their_gain > 1
    hole = next((p for p in give if p.position in pf.holes), None)
    desp = next((d for p in give for d in pf.desperation if d.startswith(p.position)), None)
    if hole is not None:
        why = f"You're {pf.rank[hole.position]} of {n} at {hole.position}, so {hole.name} would start for you"
    elif desp:
        pos, rest = desp.split(" ", 1)
        why = f"It covers {pos} for you with {rest}"
    elif helps:
        return f"{ask} It upgrades your starting lineup."
    else:
        return ask
    return f"{ask} {why}{' and it upgrades your lineup' if helps else ''}."
