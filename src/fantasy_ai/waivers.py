"""Waiver / free agent suggestions. Deterministic, every suggestion explained.

Player value (per candidate):
  ros      ESPN rest-of-season projection x role/news/buzz/usage (Player.ros_multiplier)
           x second projection (Sleeper) when available x handcuff bump
  next3    factor-adjusted worth for the next 3 weeks: this week's full worth, then per-game
           projection x future matchup (opponent points allowed), 0 on bye weeks
  handcuff an RB whose teammate ahead of him is OUT / IR gets a capped bump
  buzz     Sleeper trending adds, already capped inside ros_multiplier (news/adjust.py)

Gain of "add A, drop D" (points over the next 3 weeks):
  next3_weight x [best starting lineup with A minus without, summed over 3 weeks, FLEX counts]
  + ros_weight x (vorp(A) x start_share(A) - vorp(D) x start_share(D)) x 3 / weeks_left
  vorp = ros minus a replacement-level player at his position over the same weeks, floored at 0
  (vorp.py; replacement from the league's slots and team count), so a backup QB in a 1-QB league
  is not protected by his big raw point total. K / D/ST count 0 here (streamers handle them).
  start_share = share of the next 3 weeks he would start, at least 0.25 if he is useful depth
  (within starters + 1 backup at his position, +1 with FLEX/superflex), else at least 0.05.
  Filling an open roster spot is judged on the lineup gain alone.
Drops can be any bench player, not just the same position, but never: locked players, IR-slot
players, IR-eligible players headed to an open IR slot, never_drop players, a move that breaks
roster minimums (1 K, 1 D/ST, QBs: 1, or 2 with superflex) or leaves a starting slot empty in
any of the next 3 weeks (bye coverage).

Rolling waiver priority: a WAIVER claim spends our priority, a FREEAGENT add doesn't, so a
claim needs a bigger gain (min_gain_waiver) and the reason says so.
Streamers: K and D/ST free agents who beat our starter this week by matchup and Vegas.
Weights live in config `waivers:`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .models import SEASON_WEEKS, LeagueState, Player
from .slots import IR, NON_STARTING, slot_name

DEFAULTS = {
    "horizon": 3,
    "next3_weight": 1.0,
    "ros_weight": 1.0,
    "handcuff_bump": 0.15,
    "min_gain_free_agent": 3.0,
    "min_gain_waiver": 8.0,         # rolling priority: spend it only on a clear upgrade
    "rolling_priority": True,
    "stream_margin": 1.5,           # K / D/ST streamer must beat our starter by this much this week
    "candidates": 15,               # free agents evaluated in depth (best by quick value)
}
MIN_GAIN = DEFAULTS["min_gain_free_agent"]
DEPTH_SHARE = 0.25
SURPLUS_SHARE = 0.05   # beyond useful depth still has some value (trades, injuries), so a 115-ROS
                       # surplus WR is never treated as equal to a 9-ROS one


@dataclass
class WaiverSuggestion:
    add: Player
    drop: Player | None
    add_value: float                 # ros(add)
    drop_value: float                # ros(drop)
    gain: float                      # points over the horizon (see module doc)
    kind: str                        # "FREEAGENT" (instant) or "WAIVER" (claim); "STREAM" for streamers
    reason: str
    why: list[str] = field(default_factory=list)
    lineup_gain: float = 0.0
    threshold: float = 0.0
    considered: set[int] = field(default_factory=set)     # drops actually evaluated
    rejected: dict[int, str] = field(default_factory=dict)  # drops ruled out for this add, with reason
    next3_add: float = 0.0
    next3_drop: float = 0.0
    starts_add: int = 0                  # weeks in the horizon the add would start
    starts_drop: int = 0                 # weeks the drop would have started
    add_vorp: float | None = None        # rest-of-season value above replacement (cross-position)
    drop_vorp: float | None = None


def merged(cfg: dict | None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    out.update(cfg or {})
    return out


# ---------------------------------------------------------------- per-player value

def _teammate_out(p: Player, state: LeagueState) -> Player | None:
    """The higher-projected same-position teammate who is out (handcuff situation)."""
    if p.position != "RB" or not p.pro_team:
        return None
    everyone = [q for t in state.teams.values() for q in t.roster] + state.free_agents
    for q in everyone:
        if (q.player_id != p.player_id and q.pro_team == p.pro_team and q.position == p.position
                and q.per_game() > p.per_game() and (q.is_out() or q.news_out)):
            return q
    return None


def _second_proj_mult(p: Player) -> float:
    f = next((f for f in getattr(p, "factors", []) if f.name == "second projection"), None)
    return f.multiplier if f else 1.0


def ros(p: Player, state: LeagueState, cfg: dict) -> float:
    v = p.ros_value(state.scoring_period) * _second_proj_mult(p)
    if _teammate_out(p, state):
        v *= 1 + cfg["handcuff_bump"] / 2
    return round(v, 1)


def ros_vorp(p: Player, state: LeagueState, cfg: dict) -> float:
    """ros() minus a replacement player at his position over the same weeks, floored at 0."""
    from .vorp import POSITIONS, _weeks, replacement_levels
    if p.position not in POSITIONS:
        return 0.0
    repl = replacement_levels(state)[p.position] * _weeks(p, state.scoring_period)
    return round(max(0.0, ros(p, state, cfg) - repl), 1)


def week_worth(p: Player, state: LeagueState, k: int, cfg: dict) -> float:
    wk = state.scoring_period + k
    if p.bye_week == wk or p.on_ir_slot or p.ir_eligible():
        return 0.0
    bump = 1 + cfg["handcuff_bump"] if _teammate_out(p, state) else 1.0
    if k == 0:
        return round(p.lineup_value(wk) * bump, 2)
    ctx = state.context
    m = 1.0
    if ctx is not None:
        g = getattr(ctx, "future_games", {}).get(wk, {}).get(p.pro_team)
        if g:
            from .factors.score import matchup_multiplier
            m = matchup_multiplier(p.position, g.opp, ctx)
    back = 0.7 if (k == 1 and (p.is_out() or p.news_out)) else 1.0
    h = getattr(p, "hype", None)
    fut = h.future_mult if h is not None else 1.0          # fill-ins lose credit once the starter returns
    return round(p.per_game() * m * back * bump * fut, 2)


def _best(players: list[Player], state: LeagueState, k: int, cfg: dict, cache: dict) -> tuple[float, int, set[int]]:
    """Best starting lineup value for week offset k -> (points, slots filled, starter ids)."""
    from .lineup import best_assignment
    key = (k, frozenset(p.player_id for p in players))
    if key not in cache:
        starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}
        vals = {p.player_id: week_worth(p, state, k, cfg) for p in players}
        total, assign = best_assignment([p for p in players if vals[p.player_id] > 0], starting,
                                        lambda p, s: vals[p.player_id] + 1e-6)
        cache[key] = (round(total, 2), len(assign), set(assign))
    return cache[key]


# ---------------------------------------------------------------- drop candidates

def _minimum_ok(roster_after: list[Player], state: LeagueState) -> bool:
    pos = [p.position for p in roster_after if not p.on_ir_slot]
    need = {"K": 1 if 17 in state.slot_counts else 0, "D/ST": 1 if 16 in state.slot_counts else 0,
            "QB": 2 if state.slot_counts.get(7, 0) else 1}
    return all(pos.count(k) >= v for k, v in need.items())


def drop_status(state: LeagueState, prefs=None, cfg: dict | None = None,
                cache: dict | None = None) -> dict[int, str | None]:
    """player_id -> None if droppable, else the exact reason he is protected.
    Reasons: starter this week, locked, IR slot, IR-bound (open IR slot), never_drop (your rule),
    roster minimum, bye coverage (week N)."""
    cfg = merged(cfg)
    cache = {} if cache is None else cache
    H = cfg["horizon"]
    active = [p for p in state.roster if not p.on_ir_slot]
    ir_room = state.slot_counts.get(IR, 0) - sum(1 for p in state.roster if p.on_ir_slot)
    starters = _best(active, state, 0, cfg, cache)[2]
    base = [_best(active, state, k, cfg, cache) for k in range(H)]
    out: dict[int, str | None] = {}
    for p in state.roster:
        if p.on_ir_slot:
            out[p.player_id] = "IR slot"
        elif p.locked:
            out[p.player_id] = "locked (game started)"
        elif prefs is not None and prefs.keeps(p.name):
            out[p.player_id] = "never_drop (your rule)"
        elif p.ir_eligible() and ir_room > 0:
            ir_room -= 1
            out[p.player_id] = "IR-bound (open IR slot)"
        elif p.player_id in starters:
            out[p.player_id] = "starter this week"
        else:
            after = [q for q in active if q.player_id != p.player_id]
            if not _minimum_ok(after, state):
                out[p.player_id] = f"roster minimum (last {p.position})"
                continue
            short = [k for k in range(H) if _best(after, state, k, cfg, cache)[1] < base[k][1]]
            out[p.player_id] = f"bye coverage (week {state.scoring_period + short[0]})" if short else None
    return out


def droppable(state: LeagueState, prefs=None, cfg: dict | None = None, cache: dict | None = None) -> list[Player]:
    """Bench players with no protection reason. Roster-minimum / bye-coverage protections are
    re-checked per add (an add at the same position can cover), see evaluate_add."""
    st = drop_status(state, prefs, cfg, cache)
    soft = ("roster minimum", "bye coverage")
    return [p for p in state.roster if st[p.player_id] is None or str(st[p.player_id]).startswith(soft)]


SPECIAL = ("K", "D/ST")       # only one ever starts: never bench depth, only swapped for ours


def _useful_depth(p: Player, roster: list[Player], state: LeagueState, cfg: dict) -> bool:
    """Is p depth that can actually enter the lineup?
    RB / WR: starting slots + 1 backup (+1 more with FLEX).
    QB / TE: starting slots (+1 QB with superflex); a backup only while one of those starters is
    hurt or on bye within the horizon. A 2nd QB or TE otherwise never plays.
    K / D/ST: starting slots only; a second one never plays."""
    slot_for = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "K": 17, "D/ST": 16}
    slots = state.slot_counts.get(slot_for.get(p.position, -1), 0)
    if p.position == "QB":
        slots += state.slot_counts.get(7, 0)
    H = cfg["horizon"]
    value = lambda q: ros(q, state, cfg) + sum(week_worth(q, state, k, cfg) for k in range(H))
    same = sorted([q for q in roster if q.position == p.position and not q.on_ir_slot], key=value, reverse=True)
    rank = next((i for i, q in enumerate(same) if q.player_id == p.player_id), len(same))
    if p.position in ("RB", "WR"):
        need = slots + 1 + (1 if state.slot_counts.get(23, 0) else 0)
    elif p.position in ("QB", "TE"):
        starters = [q for q in same if q.player_id != p.player_id][:slots]
        wk = state.scoring_period
        gap = len(starters) < slots or any(
            not q.is_healthy() or q.ir_eligible() or (q.bye_week is not None and wk <= q.bye_week < wk + H)
            for q in starters)
        need = slots + (1 if gap else 0)
    else:
        need = slots
    return rank < need


# ---------------------------------------------------------------- evaluate one add

def evaluate_add(state: LeagueState, add: Player, prefs=None, cfg: dict | None = None,
                 cache: dict | None = None, only_drop: Player | None = None) -> WaiverSuggestion | None:
    """Best drop for this add (or no drop if there is an open roster spot). Returns the best
    option even if it is below the threshold (explain uses that); None if no legal drop.
    A K or D/ST add can only replace a K or D/ST of ours (never an extra bench spot).
    only_drop forces the drop (audit: best add for one roster spot)."""
    cfg = merged(cfg)
    cache = {} if cache is None else cache
    week, H = state.scoring_period, cfg["horizon"]
    weeks_left = max(1, SEASON_WEEKS - week + 1)
    active = [p for p in state.roster if not p.on_ir_slot]
    roster_limit = sum(c for s, c in state.slot_counts.items() if s != IR)
    base = [_best(active, state, k, cfg, cache) for k in range(H)]
    options: list[Player | None] = list(droppable(state, prefs, cfg, cache))
    if len(active) < roster_limit:
        options.append(None)
    if add.position in SPECIAL:
        options = [d for d in options if d is not None and d.position == add.position]
    if only_drop is not None:
        options = [d for d in options if d is not None and d.player_id == only_drop.player_id]
    best = None
    considered: set[int] = set()
    rejected: dict[int, str] = {}
    for d in options:
        after = [p for p in active if d is None or p.player_id != d.player_id] + [add]
        if not _minimum_ok(after, state):
            if d is not None:
                rejected[d.player_id] = "roster minimum"
            continue
        weeks = [_best(after, state, k, cfg, cache) for k in range(H)]
        if any(w[1] < b[1] for w, b in zip(weeks, base)):
            if d is not None:
                rejected[d.player_id] = "bye coverage"
            continue                           # would leave a starting slot empty (bye coverage)
        if d is not None:
            considered.add(d.player_id)
        lineup = round(sum(w[0] - b[0] for w, b in zip(weeks, base)), 2)
        r_add, r_drop = ros(add, state, cfg), (ros(d, state, cfg) if d else 0.0)
        v_add, v_drop = ros_vorp(add, state, cfg), (ros_vorp(d, state, cfg) if d else 0.0)
        starts = sum(1 for w in weeks if add.player_id in w[2])
        # ROS value counts by how often the player would start (depth floor 25%); filling an
        # open spot is judged on the lineup gain alone.
        floor_add = DEPTH_SHARE if _useful_depth(add, after, state, cfg) else SURPLUS_SHARE
        floor_drop = DEPTH_SHARE if (d is not None and _useful_depth(d, active, state, cfg)) else SURPLUS_SHARE
        share_add = max(floor_add, starts / H)
        share_drop = max(floor_drop, sum(1 for b in base if d is not None and d.player_id in b[2]) / H)
        ros_term = 0.0 if d is None else (v_add * share_add - v_drop * share_drop) * H / weeks_left
        gain = round(cfg["next3_weight"] * lineup + cfg["ros_weight"] * ros_term, 1)
        # ties go to dropping the lower rest-of-season player
        if best is None or gain > best[0] or (gain == best[0] and r_drop < best[4]):
            d_starts = sum(1 for b in base if d is not None and d.player_id in b[2])
            best = (gain, d, lineup, r_add, r_drop, starts, ros_term, d_starts, v_add, v_drop)
    if best is None:
        return None
    gain, d, lineup, r_add, r_drop, starts, ros_term, d_starts, v_add, v_drop = best
    next3_add = round(sum(week_worth(add, state, k, cfg) for k in range(H)), 1)
    next3_drop = round(sum(week_worth(d, state, k, cfg) for k in range(H)), 1) if d else 0.0
    claim = add.status == "WAIVERS"
    threshold = cfg["min_gain_waiver"] if (claim and cfg["rolling_priority"]) else cfg["min_gain_free_agent"]

    why: list[tuple[float, str]] = []
    if lineup:
        why.append((abs(lineup), f"lineup {lineup:+.1f} over next {H} wks (starts {starts} of {H})"))
    why.append((abs(ros_term), f"rest of season {r_add:.0f} vs {r_drop:.0f} ({v_add:.0f} vs {v_drop:.0f} above replacement)"
                if d else "fills an open roster spot"))
    mate = _teammate_out(add, state)
    if mate:
        why.append((cfg["handcuff_bump"] * 10, f"handcuff: {mate.name} is {mate.injury_status if mate.is_out() else 'reported out'}"))
    for r in add.reasons:
        if "Sleeper" in r or "role" in r:
            why.append((add.buzz * 5 + 1, r))
    ctx = state.context
    if ctx is not None and getattr(ctx, "future_games", None):
        from .factors.score import matchup_multiplier
        ms = [matchup_multiplier(add.position, g.opp, ctx) for wk, games in ctx.future_games.items()
              if (g := games.get(add.pro_team))]
        if ms and abs(sum(ms) / len(ms) - 1) >= 0.04:
            avg = sum(ms) / len(ms)
            why.append((abs(avg - 1) * 50, f"{'soft' if avg > 1 else 'tough'} matchups ahead (x{avg:.2f})"))
    if d is not None and (d.is_out() or d.bye_week in range(week, week + H)):
        why.append((1.0, f"{d.name} is {'out' if d.is_out() else 'on bye in week ' + str(d.bye_week)}"))
    why.sort(key=lambda x: x[0], reverse=True)
    lines = [w for _, w in why[:3]]

    drop_txt = f"drop {d.name} ({d.position})" if d else "no drop needed"
    reason = f"{add.name} ({add.position}, {'waivers' if claim else 'free agent'}): {drop_txt}, +{gain:.1f} pts over {H} wks."
    if claim and cfg["rolling_priority"]:
        reason += f" Costs waiver priority, so it needs +{threshold:.0f}."
    from .hype import line as hype_line
    hl = hype_line(add)
    if hl and getattr(add.hype, "label", "Steady") != "Steady":
        lines.insert(0, hl)
    if d is not None and getattr(d, "hype", None) is not None and d.hype.label in ("Fading", "Hype only", "Sell high"):
        lines.append(hype_line(d))
    from .ownership import own_note
    note = own_note(add)
    if note and len(lines) < 4:
        lines.append(note)
    if d is not None and r_drop > r_add and starts > 0 and d_starts == 0:
        lines.append(f"{d.name} has more raw value but would not start; {add.name} fills a starting slot")
    if d is not None and own_note(d) and "falling" in (own_note(d) or ""):
        lines.append(f"{d.name}: {own_note(d)}")
    return WaiverSuggestion(add, d, r_add, r_drop, gain, "WAIVER" if claim else "FREEAGENT", reason,
                            lines, lineup, threshold, considered=considered, rejected=rejected,
                            next3_add=next3_add, next3_drop=next3_drop, starts_add=starts, starts_drop=d_starts,
                            add_vorp=v_add, drop_vorp=v_drop if d else None)


# ---------------------------------------------------------------- ranking

def check_suggestion(state: LeagueState, s: WaiverSuggestion, status: dict[int, str | None], cfg: dict) -> list[str]:
    """Invariants. Any failure means the suggestion is not offered or queued."""
    fails = []
    by_id = {p.player_id: p for p in state.roster}
    free = [pid for pid, why in status.items() if why is None or str(why).startswith(("roster minimum", "bye coverage"))]
    for pid in free:
        if pid not in s.considered and pid not in s.rejected:
            fails.append(f"{by_id[pid].name} is droppable but was never evaluated for {s.add.name}")
    if free:
        low = min(free, key=lambda pid: ros_vorp(by_id[pid], state, cfg)
                  + sum(week_worth(by_id[pid], state, k, cfg) for k in range(cfg["horizon"])))
        if low not in s.considered and low not in s.rejected:
            fails.append(f"lowest-value droppable player {by_id[low].name} was not a candidate")
    if s.drop is not None:
        why = status.get(s.drop.player_id)
        if why is not None and not str(why).startswith(("roster minimum", "bye coverage")):
            fails.append(f"{s.drop.name} is protected ({why}) but was suggested as a drop")
        # Exception: the add starts for us and the drop would not start in any of the next weeks
        # (e.g. an RB filling an empty RB slot vs a 12th WR). Team value, not raw value.
        # Rest of season is compared above replacement, so positions compare fairly.
        fills_need = s.starts_add > 0 and s.starts_drop == 0
        dv = s.drop_vorp if s.drop_vorp is not None else s.drop_value
        av = s.add_vorp if s.add_vorp is not None else s.add_value
        if dv > av and s.next3_drop > s.next3_add and not fills_need:
            fails.append(f"{s.drop.name} is worth more than {s.add.name} both rest of season above replacement "
                         f"({dv:.0f} vs {av:.0f}) and over 3 weeks ({s.next3_drop:.1f} vs {s.next3_add:.1f})")
    return fails


def rank_with_checks(state: LeagueState, limit: int = 5, min_gain: float | None = None,
                     prefs=None, cfg: dict | None = None) -> tuple[list[WaiverSuggestion], list[str]]:
    """Like rank_free_agents, but also returns invariant failures ('waiver check failed: ...')."""
    cfg = merged(cfg)
    status = drop_status(state, prefs, cfg)
    suggestions = rank_free_agents(state, limit=limit * 2, min_gain=min_gain, prefs=prefs, cfg=cfg, _checked=False)
    ok, failures = [], []
    for s in suggestions:
        f = check_suggestion(state, s, status, cfg)
        if f:
            failures += [f"waiver check failed ({s.add.name}): {x}" for x in f]
        else:
            ok.append(s)
    return ok[:limit], failures


def rank_free_agents(state: LeagueState, limit: int = 5, min_gain: float | None = None,
                     prefs=None, cfg: dict | None = None, _checked: bool = True) -> list[WaiverSuggestion]:
    """Best add/drop pairs above their thresholds, best first, each drop used once.
    Suggestions that fail an invariant check are removed (see rank_with_checks)."""
    if _checked:
        return rank_with_checks(state, limit, min_gain, prefs, cfg)[0]
    cfg = merged(cfg)
    if min_gain is not None:
        cfg["min_gain_free_agent"] = min_gain
    cache: dict = {}
    quick = sorted(state.free_agents, key=lambda p: ros(p, state, cfg) + 3 * week_worth(p, state, 1, cfg), reverse=True)
    out = []
    for fa in quick[: cfg["candidates"]]:
        s = evaluate_add(state, fa, prefs, cfg, cache)
        if s and s.gain >= s.threshold:
            out.append(s)
    out.sort(key=lambda s: s.gain, reverse=True)
    open_spots = sum(c for s_, c in state.slot_counts.items() if s_ != IR) - sum(1 for p in state.roster if not p.on_ir_slot)
    used: set[int] = set()
    unique = []
    for s in out:
        if s.drop is None:
            if open_spots <= 0:
                continue
            open_spots -= 1            # each open roster spot is used once
        elif s.drop.player_id in used:
            continue
        else:
            used.add(s.drop.player_id)
        unique.append(s)
    return unique[:limit]


def swap_view(state: LeagueState, p: Player, cfg: dict) -> str:
    """explain line for a free-agent K / D/ST: they only ever replace our starter (streamer path)."""
    wk = state.scoring_period
    word = "kicker" if p.position == "K" else "defense"
    ours = [q for q in state.roster if q.position == p.position and not q.on_ir_slot]
    if not ours:
        return f"{word} swap: we have no {p.position}; projects {p.lineup_value(wk):.1f} this week"
    mine = max(ours, key=lambda q: q.lineup_value(wk))
    edge = p.lineup_value(wk) - mine.lineup_value(wk)
    verdict = "would be suggested" if edge >= cfg["stream_margin"] else "not suggested"
    return (f"{word} swap: projects {p.lineup_value(wk):.1f} vs your starter {mine.name} {mine.lineup_value(wk):.1f} "
            f"({edge:+.1f}; needs +{cfg['stream_margin']:.1f} to suggest, {verdict}). "
            f"A {p.position} is never added as an extra bench player.")


def streamers(state: LeagueState, prefs=None, cfg: dict | None = None) -> list[WaiverSuggestion]:
    """K and D/ST free agents who beat our starter this week (matchup, Vegas, weather)."""
    from .factors.score import top_reasons
    cfg = merged(cfg)
    wk = state.scoring_period
    out = []
    for pos in ("K", "D/ST"):
        ours = [p for p in state.roster if p.position == pos and not p.on_ir_slot]
        if not ours:
            continue
        mine_best = max(p.lineup_value(wk) for p in ours)
        droppable_ours = [p for p in ours if not p.locked and not (prefs and prefs.keeps(p.name))]
        fas = sorted([p for p in state.free_agents if p.position == pos], key=lambda p: p.lineup_value(wk), reverse=True)
        if not fas or not droppable_ours:
            continue
        fa = fas[0]
        edge = round(fa.lineup_value(wk) - mine_best, 1)
        if edge < cfg["stream_margin"]:
            continue
        drop = min(droppable_ours, key=lambda p: p.lineup_value(wk))
        claim = fa.status == "WAIVERS"
        why = top_reasons(fa, 3) or [f"projects {fa.lineup_value(wk):.1f} vs {mine_best:.1f}"]
        reason = (f"Stream {fa.name} ({pos}) over {drop.name} this week: {fa.lineup_value(wk):.1f} vs {mine_best:.1f} (+{edge})."
                  + (" On waivers: costs priority." if claim else ""))
        out.append(WaiverSuggestion(fa, drop, fa.lineup_value(wk), drop.lineup_value(wk), edge,
                                    "WAIVER" if claim else "FREEAGENT", reason, why, edge, cfg["stream_margin"]))
    return out


def best_add_for(state: LeagueState, drop: Player, prefs=None, cfg: dict | None = None,
                 skill_only: bool = True) -> WaiverSuggestion | None:
    """Best free agent to take this player's roster spot, even below the threshold (audit)."""
    cfg = merged(cfg)
    cache: dict = {}
    best = None
    for fa in state.free_agents:
        if skill_only and fa.position in SPECIAL:
            continue
        sg = evaluate_add(state, fa, prefs, cfg, cache, only_drop=drop)
        if sg is not None and (best is None or sg.gain > best.gain):
            best = sg
    return best


def audit(state: LeagueState, prefs=None, cfg: dict | None = None) -> list[dict]:
    """One row per rostered player: droppable yes/no + exact reason, ROS, 3-week worth, roster %."""
    from .ownership import own_note
    cfg = merged(cfg)
    status = drop_status(state, prefs, cfg)
    rows = []
    for p in state.roster:
        why = status[p.player_id]
        rows.append({
            "name": p.name, "pos": p.position, "slot": slot_name(p.lineup_slot),
            "droppable": why is None, "reason": why or "",
            "ros": ros(p, state, cfg), "vorp": ros_vorp(p, state, cfg),
            "next3": round(sum(week_worth(p, state, k, cfg) for k in range(cfg["horizon"])), 1),
            "owned": p.percent_owned, "trend": own_note(p) or "",
            "hype": p.hype.label if getattr(p, "hype", None) is not None else "",
        })
    rows.sort(key=lambda r: (not r["droppable"], r["vorp"] + r["next3"]))
    return rows
