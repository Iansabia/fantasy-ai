"""Lineup optimizer. Plain code, exact answer.

Rules:
- Starting slots come from settings.rosterSettings.lineupSlotCounts (everything except BE/IR).
- A player may fill a slot only if the slot id is in his eligibleSlots (ESPN's own list,
  so FLEX 23 = RB/WR/TE and OP 7 = QB/RB/WR/TE come for free).
- Anyone on bye, OUT, IR-status or suspended is benched.
- Locked players (game started) never move; a locked starter keeps his slot.
- Players sitting in the IR slot stay there.
- Output: only the slots that change (ESPN rejects from == to items, see
  jwulff 05-espn-write-surface.md section 3), vacates listed before fills.

Search: dynamic programming over "how many of each slot type are filled". This is exact
(unlike greedy fill order when RB/WR and WR/TE style flex slots overlap) and tiny: a normal
roster has at most a few thousand states.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import LeagueState, Player
from .slots import BENCH, IR, NON_STARTING, slot_name

KEEP_BONUS = 1e-3  # tie-break: prefer leaving a player where he already is
FORCE_BONUS = 1000.0  # "you asked" starts fill a legal slot before anything else
FLEX_SLOTS = {3, 5, 7, 23}  # RB/WR, WR/TE, OP, RB/WR/TE
DISPLAY_ORDER = [0, 2, 4, 6, 3, 5, 23, 7, 16, 17, 20, 21]


@dataclass
class Move:
    player_id: int
    name: str
    from_slot: int
    to_slot: int
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.name}: {slot_name(self.from_slot)} -> {slot_name(self.to_slot)}"


@dataclass
class LineupResult:
    moves: list[Move]
    before_points: float                   # injury-adjusted expected points (Player.lineup_value)
    after_points: float
    target: dict[int, int]                 # playerId -> slot after the lineup moves
    warnings: list[str] = field(default_factory=list)
    # IR handling. Sent as a SEPARATE ROSTER transaction after the lineup one: a BE->IR / IR->BE
    # LINEUP item has not been captured on the wire (jwulff 05 section 8 item 9), and one POST is
    # atomic, so a rejected IR item must not take the lineup moves down with it.
    ir_moves: list[Move] = field(default_factory=list)
    ir_drop: Player | None = None          # roster full: suggested drop so ir_activate can come off IR
    ir_activate: Player | None = None
    pref_skipped: list[str] = field(default_factory=list)   # owner requests we could not honor

    @property
    def gain(self) -> float:
        return round(self.after_points - self.before_points, 2)


def starting_points(roster: list[Player], slots: dict[int, int], week: int) -> float:
    by_id = {p.player_id: p for p in roster}
    return round(sum(by_id[pid].lineup_value(week) for pid, s in slots.items() if s not in NON_STARTING), 2)


def best_assignment(candidates: list[Player], open_slots: dict[int, int],
                    value) -> tuple[float, dict[int, int]]:
    """Max total value assignment of candidates into open slot counts.
    value(player, slot_id) -> float. Returns (total, {playerId: slot_id})."""
    slot_types = sorted(open_slots)
    caps = tuple(open_slots[s] for s in slot_types)
    # dp: filled-count tuple -> (total, assignment tuple of (pid, slot))
    dp: dict[tuple, tuple[float, tuple]] = {tuple(0 for _ in caps): (0.0, ())}
    for p in candidates:
        options = [i for i, s in enumerate(slot_types) if s in p.eligible_slots]
        if not options:
            continue
        nxt = dict(dp)
        for state, (total, assign) in dp.items():
            for i in options:
                if state[i] >= caps[i]:
                    continue
                v = value(p, slot_types[i])
                if v <= 0:
                    continue
                ns = state[:i] + (state[i] + 1,) + state[i + 1:]
                cand = (total + v, assign + ((p.player_id, slot_types[i]),))
                if ns not in nxt or cand[0] > nxt[ns][0]:
                    nxt[ns] = cand
        dp = nxt
    total, assign = max(dp.values(), key=lambda x: x[0])
    return total, dict(assign)


def optimize(state: LeagueState, roster: list[Player] | None = None, prefs=None) -> LineupResult:
    """prefs: fantasy_ai.prefs.Prefs (owner's start/bench/never rules) or None."""
    roster = roster if roster is not None else state.roster
    week = state.scoring_period
    warnings: list[str] = []
    pref_skipped: list[str] = []
    starting = {s: c for s, c in state.slot_counts.items() if s not in NON_STARTING}

    # Locked starters keep their slot and use up that slot. IR-slot players stay for the lineup
    # step (IR changes are planned separately below).
    open_slots = dict(starting)
    fixed: dict[int, int] = {}
    for p in roster:
        if p.locked or p.lineup_slot == IR:
            fixed[p.player_id] = p.lineup_slot
            if p.lineup_slot in open_slots:
                open_slots[p.lineup_slot] -= 1
            if p.locked and p.lineup_slot not in NON_STARTING and not p.available(week):
                warnings.append(f"{p.name} is locked in {p.slot} but cannot score (bye/OUT); nothing to do.")
    open_slots = {s: c for s, c in open_slots.items() if c > 0}

    candidates = [p for p in roster if p.player_id not in fixed and p.available(week)]
    if prefs is not None:
        for p in roster:
            if prefs.starts(p.name) and not p.available(week):
                why = "bye week" if p.on_bye(week) else f"{p.injury_status}" if p.is_out() else "reported out"
                pref_skipped.append(f"You asked to start {p.name}, but he can't play ({why}); started the best available instead.")
            elif prefs.starts(p.name) and p.player_id in fixed and p.lineup_slot in NON_STARTING:
                pref_skipped.append(f"You asked to start {p.name}, but he is locked on the bench.")
            if prefs.benches(p.name) and p.player_id in fixed and p.lineup_slot not in NON_STARTING:
                pref_skipped.append(f"You asked to bench {p.name}, but he is already locked in.")
        candidates = [p for p in candidates if not prefs.benches(p.name)]

    def value(p: Player, slot: int) -> float:
        ev = p.lineup_value(week)            # questionable/doubtful discount lives here
        v = ev + 1e-6                        # >0 so a healthy 0-projection player can still fill a hole
        if prefs is not None and prefs.starts(p.name):
            v += FORCE_BONUS
        if p.lineup_slot == slot:
            v += KEEP_BONUS
        if slot not in FLEX_SLOTS:
            v += 1e-5 * ev                   # on ties, put the better player in the dedicated slot
        return v

    _, assign = best_assignment(candidates, open_slots, value)

    target: dict[int, int] = {}
    for p in roster:
        if p.player_id in fixed:
            target[p.player_id] = fixed[p.player_id]
        else:
            target[p.player_id] = assign.get(p.player_id, BENCH)

    moves = [Move(p.player_id, p.name, p.lineup_slot, target[p.player_id])
             for p in roster if target[p.player_id] != p.lineup_slot]
    moves.sort(key=lambda m: 0 if m.to_slot == BENCH else 1)  # vacate before fill
    by_id = {p.player_id: p for p in roster}
    for m in moves:
        p = by_id[m.player_id]
        m.reason = move_reason(p, m, week)
        if prefs is not None and m.to_slot not in NON_STARTING and prefs.starts(p.name):
            m.reason = f"started {p.name} at {slot_name(m.to_slot)}: you asked (worth {p.lineup_value(week):.1f})"
        elif prefs is not None and m.to_slot == BENCH and prefs.benches(p.name):
            m.reason = f"benched {p.name}: you asked"
    if prefs is not None:
        for p in roster:
            if prefs.starts(p.name) and p.available(week) and target.get(p.player_id) in NON_STARTING \
                    and p.player_id not in fixed:
                pref_skipped.append(f"You asked to start {p.name}, but no legal slot was left.")
    warnings.extend(pref_skipped)

    current = {p.player_id: p.lineup_slot for p in roster}
    res = LineupResult(
        moves=moves,
        before_points=starting_points(roster, current, week),
        after_points=starting_points(roster, target, week),
        target=target,
        warnings=warnings,
    )
    res.pref_skipped = pref_skipped
    plan_ir(state, roster, res, prefs)
    return res


def plan_ir(state: LeagueState, roster: list[Player], res: LineupResult, prefs=None) -> None:
    """Fill open IR slots with IR-eligible players, and activate IR-slot players who are no
    longer IR-eligible (ESPN treats them as an illegal IR stash). Activation needs a roster
    spot; if there is none, suggest a drop for the approval queue instead of acting."""
    from .vorp import vorp_ros
    ir_cap = state.slot_counts.get(IR, 0)
    in_ir = [p for p in roster if p.lineup_slot == IR]
    roster_limit = sum(c for s, c in state.slot_counts.items() if s != IR)
    active_count = len(roster) - len(in_ir)
    to_ir = sorted([p for p in roster if p.lineup_slot != IR and p.ir_eligible() and not p.locked],
                   key=lambda p: vorp_ros(p, state), reverse=True)
    stale = [p for p in in_ir if not p.ir_eligible() and not p.locked]

    def into_ir(p: Player) -> Move:
        m = Move(p.player_id, p.name, res.target[p.player_id], IR,
                 f"moved {p.name} to IR: {p.injury_status} (ESPN), frees a bench spot")
        res.target[p.player_id] = IR
        return m

    def out_of_ir(p: Player) -> Move:
        m = Move(p.player_id, p.name, IR, BENCH, f"activated {p.name} from IR: now listed {p.injury_status} (ESPN)")
        res.target[p.player_id] = BENCH
        return m

    free = max(0, ir_cap - len(in_ir))
    for p in to_ir[:free]:
        res.ir_moves.append(into_ir(p))
        active_count -= 1
    waiting = to_ir[free:]

    for p in stale:
        if waiting:                               # swap: net roster count unchanged
            res.ir_moves.append(out_of_ir(p))
            res.ir_moves.append(into_ir(waiting.pop(0)))
        elif active_count < roster_limit:
            res.ir_moves.append(out_of_ir(p))
            active_count += 1
        else:
            droppable = [q for q in roster if res.target.get(q.player_id) == BENCH and not q.locked
                         and q.player_id != p.player_id and not (prefs and prefs.keeps(q.name))]
            if droppable:
                res.ir_drop = min(droppable, key=lambda q: vorp_ros(q, state))   # cross-position: above replacement
                res.ir_activate = p
                res.warnings.append(f"{p.name} is healthy but stuck in the IR slot and the roster is full; "
                                    f"suggested drop {res.ir_drop.name} (needs approval).")
            break
    for p in waiting:
        res.warnings.append(f"{p.name} is IR-eligible but no IR slot is free.")


def move_reason(p: Player, m: Move, week: int) -> str:
    """One line, facts only, for texts: why this player moved. Every line names its source."""
    from .factors.score import top_reasons
    extra = list(p.reasons[:1]) + top_reasons(p)
    news = ("; " + "; ".join(extra)) if extra else ""
    disc = p.status_discount()
    if disc < 1 and p.news_status is None:
        news += "; no news found, default discount"
    if m.to_slot == BENCH:
        if p.on_bye(week):
            return f"benched {p.name}: bye week"
        if p.is_out():
            return f"benched {p.name}: {p.injury_status} (ESPN)"
        if p.news_out:
            return f"benched {p.name}{news}"
        if disc < 1:
            return (f"benched {p.name}: {p.game_status} (ESPN), {p.proj_week:.1f} proj counts as "
                    f"{p.lineup_value(week):.1f}{news}")
        esp = p.base_proj_week if p.base_proj_week is not None else p.proj_week
        return f"benched {p.name}: worth {p.lineup_value(week):.1f} (ESPN proj {esp:.1f}), others higher{news}"
    esp = p.base_proj_week if p.base_proj_week is not None else p.proj_week
    tag = f", {p.game_status} but still ahead" if disc < 1 else ""
    return f"started {p.name} at {slot_name(m.to_slot)}: worth {p.lineup_value(week):.1f} (ESPN proj {esp:.1f}){tag}{news}"


def season_lineup_value(roster: list[Player], slot_counts: dict[int, int]) -> float:
    """Best starting lineup by per-game season value (ignores this week's bye/injury).
    Used by trades.py to measure starting-lineup impact."""
    starting = {s: c for s, c in slot_counts.items() if s not in NON_STARTING}
    total, _ = best_assignment(roster, starting, lambda p, s: p.per_game())
    return round(total, 2)
