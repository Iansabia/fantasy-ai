"""Team-need trade engine and scouting.

Value to a team = what its best lineups are worth, not the sum of its players:
  team_value(roster) = best lineup over the next 3 weeks (factor-adjusted worth, byes)
                     + best lineup by rest-of-season per-game value x remaining weeks
                     + small depth credit for the first backups (1 QB, 2 RB, 2 WR, 1 TE;
                       +1 QB in superflex), counted ABOVE REPLACEMENT (vorp.py), so a backup QB
                       in a 1-QB league adds ~nothing: any team can find a similar QB on waivers
Every lineup slot can also be filled by a virtual replacement-level player (the waiver wire), so
an injured or weak starter counts at replacement, never zero.
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
    return p.season_rate()


ELIGIBLE = {"QB": [0, 7], "RB": [2, 3, 23, 7], "WR": [3, 4, 5, 23, 7], "TE": [5, 6, 23, 7]}


def _replacements(state: LeagueState) -> list[Player]:
    """Virtual waiver-wire players at replacement level, one per slot a position can fill.
    Any team can plug a hole (injured or empty starter) with one of these, so a trade only
    'helps' a lineup by what it adds ABOVE replacement, not by filling a zero."""
    from .vorp import replacement_levels
    repl = replacement_levels(state)
    c = state.slot_counts
    counts = {"QB": c.get(0, 0) + c.get(7, 0), "RB": c.get(2, 0) + c.get(23, 0),
              "WR": c.get(4, 0) + c.get(23, 0), "TE": c.get(6, 0)}
    out = []
    for pos, n in counts.items():
        for i in range(n):
            v = Player(player_id=-(900000 + 100 * POSITIONS.index(pos) + i), name=f"replacement {pos}", position=pos,
                       eligible_slots=ELIGIBLE[pos])
            v._repl = repl[pos]
            out.append(v)
    return out


def team_value(roster: list[Player], state: LeagueState, cache: dict | None = None) -> float:
    cache = {} if cache is None else cache
    key = frozenset(p.player_id for p in roster)
    if key in cache:
        return cache[key]
    cfg = waiver_cfg(None)
    starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}
    active = [p for p in roster if not p.on_ir_slot]
    if "_repl" not in cache:
        cache["_repl"] = _replacements(state)
    virt_all = cache["_repl"]

    def needed(vals: dict) -> list[Player]:
        """Only as many replacements per position as real players above replacement leave open
        (keeps the lineup search small; extra ones could never be picked)."""
        out, by_pos = [], {}
        for v in virt_all:
            by_pos.setdefault(v.position, []).append(v)
        for pos, vs in by_pos.items():
            good = sum(1 for p in active if p.position == pos and vals.get(p.player_id, 0) > vs[0]._repl)
            out += vs[:max(0, len(vs) - good)]
        return out

    near = 0.0
    for k in range(H):
        vals = {p.player_id: week_worth(p, state, k, cfg) for p in active}
        virt = needed(vals)
        vals.update({v.player_id: v._repl for v in virt})
        t, _ = best_assignment([p for p in active if vals[p.player_id] > 0] + virt, starting,
                               lambda p, s: vals[p.player_id] + 1e-6)
        near += t
    weeks_after = max(0, SEASON_WEEKS - state.scoring_period + 1 - H)
    virt = needed({p.player_id: season_value(p) for p in active})
    t, assign = best_assignment(active + virt, starting,
                                lambda p, s: (getattr(p, "_repl", None) or season_value(p)) + 1e-6)
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


def roster_limit(state: LeagueState) -> int:
    from .slots import IR
    return sum(c for s, c in state.slot_counts.items() if s != IR)


def forced_drops(roster: list[Player], state: LeagueState) -> list[Player]:
    """Players a team must cut when a trade puts it over the roster limit: its lowest-value bench
    players (value above replacement per game; a 2nd K / D/ST is worth 0), never breaking roster
    minimums (1 K, 1 D/ST, 1 QB or 2 with superflex). Received players can be cut too."""
    active = [p for p in roster if not p.on_ir_slot]
    extra = len(active) - roster_limit(state)
    if extra <= 0:
        return []
    starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}
    _, assign = best_assignment(active, starting, lambda p, s: season_value(p) + 1e-6)
    mins = {"QB": 1 + (1 if state.slot_counts.get(7) else 0), "K": 1 if state.slot_counts.get(17) else 0,
            "D/ST": 1 if state.slot_counts.get(16) else 0}
    count = {pos: sum(1 for p in active if p.position == pos) for pos in mins}
    out = []
    for p in sorted([p for p in active if p.player_id not in assign], key=lambda p: vorp_pg(p, state)):
        if len(out) >= extra:
            break
        if p.position in mins and count[p.position] <= mins[p.position]:
            continue
        if p.position in count:
            count[p.position] -= 1
        out.append(p)
    return out


def lineup_change(state: LeagueState, team_id: int, sends: list[Player], receives: list[Player],
                  cache: dict | None = None) -> tuple[float, list[Player]]:
    """THE lineup delta for one side of a trade, used by both `fai trade` and trade ideas:
    team_value(after, with forced drops if over the roster limit) - team_value(before).
    Returns (delta in ROS lineup points, players they would have to drop)."""
    team = state.teams[team_id]
    out = {p.player_id for p in sends}
    after = [p for p in team.roster if p.player_id not in out] + list(receives)
    drops = forced_drops(after, state)
    if drops:
        cut = {p.player_id for p in drops}
        after = [p for p in after if p.player_id not in cut]
    return round(team_value(after, state, cache) - team_value(team.roster, state, cache), 1), drops


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
    give_cost: dict[int, float] = field(default_factory=dict)   # lineup points each give costs us (get included)
    price: float = 1.0                  # 1.0 inside the fair band, lower when we overpay / underpay a lot
    sweetened: "Idea | None" = None     # easier-to-accept version when this one is a hard sell
    their_drops: list = field(default_factory=list)   # players they must cut to fit the roster limit
    cautions: list = field(default_factory=list)      # expert-disagreement warnings on players we get
    sensitivity: str = ""                             # does the verdict hold if values move +-5%?
    robust: bool = True

    @property
    def overpay(self) -> float | None:
        """Market points we give beyond what we get (negative = we underpay)."""
        return round(self.fc_give - self.fc_get, 0) if self.fc_give and self.fc_get else None

    @property
    def overpay_pct(self) -> float | None:
        return round(self.fc_give / self.fc_get - 1, 3) if self.fc_give and self.fc_get else None

    @property
    def hard_sell(self) -> bool:
        return self.their_gain < -HARD_SELL


# Package search tuning. We want the cheapest package that still works: players who cost our
# lineup little (backups the incoming player makes redundant) at a market price close to even.
FAIR_BAND = (0.90, 1.05)      # give/get market ratio with no price penalty
OVERPAY_PENALTY = 1.5         # score x (1 - 1.5 x overpay beyond 5%): 20% over -> x0.78, 35% over -> x0.55
UNDERPAY_PENALTY = 0.75       # lowballing is less bad for us, but hurts acceptance
HARD_SELL = 3.0               # partner's lineup loses more than this -> "hard sell"
CLEAR_EDGE = 10.0             # ROS lineup points that count as a clear win for us on an even-market deal
MIN_THEIR_GAIN = 6.0          # partner's lineup must gain a meaningful amount (Ian, trust audit: +6 ROS)
SENSITIVITY = 0.05            # each idea is re-checked with both sides' values moved +-5%
MAX_OVERPAY = 1.20            # default view never pays 20%+ over market (lopsided view may)
MAX_THEIR_LOSS = 20.0         # never suggest trades that gut the partner's lineup
MAX_GIVE = 3
LOW_CONFIDENCE_SCORE = 0.75   # target we rank 20+ spots above the experts
PER_TARGET = 20               # packages per target evaluated in full (best by quick cost first)


def _market(ps: list[Player], state) -> float | None:
    vals = [fc_value(p) for p in ps]
    return sum(vals) if all(v is not None for v in vals) and vals else None


def _ratio(give, get, state) -> tuple[float, float | None, float | None]:
    """give/get market ratio (FantasyCalc; else our value above replacement), with the FC sums."""
    fg, ft = _market(give, state), _market(get, state)
    if fg and ft:
        return fg / ft, fg, ft
    a = sum(max(0.0, vorp_ros(p, state)) for p in give)
    b = sum(max(0.0, vorp_ros(p, state)) for p in get)
    return (a / b if b > 0 else 0.0), fg, ft


def _fair(give, get, state) -> tuple[float, float | None, float | None]:
    r, fg, ft = _ratio(give, get, state)
    return (min(r, 1 / r) if r > 0 else 0.0), fg, ft


def price_factor(ratio: float) -> float:
    lo, hi = FAIR_BAND
    return max(0.0, 1 - OVERPAY_PENALTY * max(0.0, ratio - hi) - UNDERPAY_PENALTY * max(0.0, lo - ratio))


def wins_both_ways(our_gain: float, their_gain: float, ratio: float, pf: "Profile", give, min_gain: float) -> bool:
    """Default rule (Ian): the partner's starting lineup must improve and one of the players they
    get must fill a weak position of theirs (scouting hole or a starter out / on bye soon), while
    we still win: our gain clears min_gain and beats theirs, or market value is about even (90-110%)
    and our gain is a clear edge on its own (CLEAR_EDGE ROS points)."""
    helps_them = their_gain >= MIN_THEIR_GAIN
    fills_need = any(p.position in pf.holes or any(d.startswith(p.position) for d in pf.desperation) for p in give)
    we_win = our_gain >= min_gain and (our_gain > their_gain
                                       or (FAIR_BAND[0] <= ratio <= 1.10 and our_gain >= CLEAR_EDGE))
    return helps_them and fills_need and we_win


def trade_ideas(state: LeagueState, prefs=None, top: int = 8, min_gain: float = 5.0,
                max_their_loss: float = HARD_SELL, min_fair: float = 0.75, pool: int = 5,
                include_lopsided: bool = False) -> list[Idea]:
    """For each target (1 or 2 of their players), search give packages of 1-3 of ours and keep
    the cheapest that works: score = our gain x price (market ratio near even) x acceptance.
    Default: only trades that improve the partner's lineup at a position they need while we
    still win (wins_both_ways). include_lopsided also keeps trades that hurt them (hard sells) or
    miss their needs; a hard sell then carries the best easier package as `sweetened`, so the
    extra market cost is visible."""
    cache: dict = {}
    prof = profiles(state)
    me = state.my_team
    n = len(state.teams)
    base_me = team_value(me.roster, state, cache)
    protected = (lambda p: prefs is not None and (prefs.keeps(p.name) or prefs.no_trade(p.name)))
    mine = [p for p in me.roster if not p.on_ir_slot and not p.locked and not protected(p)
            and p.position in POSITIONS]
    groups: list[list[Idea]] = []
    for tid, t in state.teams.items():
        if tid == state.my_team_id:
            continue
        pf = prof[tid]
        base_them = team_value(t.roster, state, cache)
        theirs = [p for p in t.roster if not p.on_ir_slot and not p.locked and p.position in POSITIONS]

        def our_add(p):
            return team_value(me.roster + [p], state, cache) - base_me

        get_pool = sorted(theirs, key=our_add, reverse=True)[:pool]
        need_pos = set(pf.holes) | {d.split(" ", 1)[0] for d in pf.desperation}
        targets = [[r] for r in get_pool] + [list(r2) for r2 in itertools.combinations(get_pool, 2)]
        for get in targets:
            r_ids = {p.player_id for p in get}
            with_get = me.roster + get
            v_with = team_value(with_get, state, cache)
            cost = {p.player_id: round(v_with - team_value([q for q in with_get if q.player_id != p.player_id],
                                                           state, cache), 1) for p in mine}
            quick = []
            for k in range(1, MAX_GIVE + 1):
                for give in itertools.combinations(mine, k):
                    if len(get) - k > 0 and _over_limit(
                            [p for p in me.roster if p not in give] + get, state):
                        continue      # receiving more than we send into a full roster needs a drop
                    ratio, fg, ft = _ratio(give, get, state)
                    if ratio <= 0 or min(ratio, 1 / ratio) < min_fair:
                        continue
                    if not include_lopsided and ratio >= MAX_OVERPAY:
                        continue
                    est = sum(cost[p.player_id] for p in give)
                    helps = sum(max(0.0, vorp_ros(p, state)) for p in give if p.position in need_pos)
                    quick.append((est + 40 * (1 - price_factor(ratio)), give, ratio, fg, ft, helps))
            # two shortlists: cheapest for us, and cheap-for-us packages that fill THEIR needs
            # (a package can only pass Ian's rule if it helps them, so search there too)
            by_cost = sorted(quick, key=lambda x: x[0])[:PER_TARGET]
            by_need = sorted(quick, key=lambda x: x[0] - 0.5 * x[5])[:PER_TARGET]
            seen_pk, quick = set(), []
            for x in by_cost + by_need:
                k = frozenset(p.player_id for p in x[1])
                if k not in seen_pk:
                    seen_pk.add(k)
                    quick.append(x)
            group = []
            for _, give, ratio, fg, ft, _h in quick:
                our_gain, _ = lineup_change(state, state.my_team_id, list(give), get, cache)
                if our_gain < min_gain:
                    continue
                their_gain, their_drops = lineup_change(state, tid, get, list(give), cache)
                if their_gain < -MAX_THEIR_LOSS:
                    continue
                if not include_lopsided and not wins_both_ways(our_gain, their_gain, ratio, pf, give, min_gain):
                    continue
                need_match = 1.0 if any(p.position in pf.holes or any(d.startswith(p.position) for d in pf.desperation)
                                        for p in give) else 0.0
                accept = round(0.4 * max(0.0, min(1.0, (their_gain + max_their_loss) / 15))
                               + 0.3 * need_match + 0.3 * min(1.0, ratio), 2)
                price = price_factor(ratio)
                score = round(our_gain * price * (0.4 + 0.6 * accept), 1)
                from .experts import caution, overrated
                warns = [w for w in (caution(p, state) for p in get) if w]
                if any(overrated(p, state) for p in get):
                    score = round(score * LOW_CONFIDENCE_SCORE, 1)       # we may be overrating the target
                group.append(Idea(tid, t.name.strip(), list(give), get, our_gain, their_gain, fg, ft,
                                  round(min(ratio, 1 / ratio), 2), accept, score, _why_partner(pf, give, get, n),
                                  _pitch(pf, give, get, their_gain, n),
                                  give_cost={p.player_id: cost[p.player_id] for p in give}, price=round(price, 2),
                                  their_drops=their_drops, cautions=warns))
            if group:
                groups.append(group)
    out = []
    for group in groups:
        group.sort(key=lambda i: i.score, reverse=True)
        best = group[0]
        if best.hard_sell:
            easier = [i for i in group if not i.hard_sell]
            if easier:
                best.sweetened = easier[0]
        out.append(best)
    out.sort(key=lambda i: i.score, reverse=True)
    out = out[:top]
    for i in out:
        _sensitivity(state, i, prof[i.partner_id], min_gain, include_lopsided)
    return out


def _sensitivity(state: LeagueState, idea: Idea, pf: Profile, min_gain: float, lopsided: bool) -> None:
    """Re-run both sides with the traded players' values moved +-SENSITIVITY in the worst
    direction for each team (ours: what we give +5%, what we get -5%; theirs: the reverse)."""
    def run(give_f: float, get_f: float) -> tuple[float, float]:
        moved = [(p, give_f) for p in idea.give] + [(p, get_f) for p in idea.get]
        saved = [(p, p.proj_week, p.base_proj_week, p.proj_season) for p, _ in moved]
        for p, f in moved:
            p.proj_week, p.proj_season = p.proj_week * f, p.proj_season * f
            if p.base_proj_week is not None:
                p.base_proj_week *= f
        if hasattr(state, "_vorp_cache"):
            del state._vorp_cache
        try:
            ours, _ = lineup_change(state, state.my_team_id, idea.give, idea.get, {})
            theirs, _ = lineup_change(state, idea.partner_id, idea.get, idea.give, {})
        finally:
            for p, pw, bpw, ps in saved:
                p.proj_week, p.base_proj_week, p.proj_season = pw, bpw, ps
            if hasattr(state, "_vorp_cache"):
                del state._vorp_cache
        return ours, theirs

    up, down = 1 + SENSITIVITY, 1 - SENSITIVITY
    ours_worst, theirs_when_ours_worst = run(up, down)
    ours_when_theirs_worst, theirs_worst = run(down, up)
    ratio = (idea.fc_give / idea.fc_get) if idea.fc_give and idea.fc_get else 1.0
    ok = lopsided or (wins_both_ways(ours_worst, theirs_when_ours_worst, ratio, pf, idea.give, min_gain)
                      and wins_both_ways(ours_when_theirs_worst, theirs_worst, ratio, pf, idea.give, min_gain))
    idea.robust = ok and ours_worst > 0
    rng = f"ours {ours_worst:+.1f} to {ours_when_theirs_worst:+.1f}, theirs {theirs_worst:+.1f} to {theirs_when_ours_worst:+.1f}"
    idea.sensitivity = (f"holds if values move +-5% ({rng})" if idea.robust
                        else f"FRAGILE: the verdict can flip if values move +-5% ({rng})")


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
    """Lead with what the trade fixes for them, then the ask. One or two sentences."""
    g, r = _names(give), _names(get)
    ask = f"Would you trade {r} for {g}?"
    hole = next((p for p in give if p.position in pf.holes), None)
    desp = next(((p, d) for p in give for d in pf.desperation if d.startswith(p.position)), None)
    if hole is not None:
        return (f"Hey {pf.name}, you're {pf.rank[hole.position]} of {n} at {hole.position}, "
                f"and {hole.name} would start for you. {ask}")
    if desp is not None:
        p, d = desp
        return f"Hey {pf.name}, {p.name} covers {p.position} for you with {d.split(' ', 1)[1]}. {ask}"
    if their_gain > 1:
        return f"Hey {pf.name}, this upgrades your starting lineup. {ask}"
    return f"Hey {pf.name}, {ask[0].lower()}{ask[1:]}"


def _names(ps) -> str:
    names = [p.name for p in ps]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
