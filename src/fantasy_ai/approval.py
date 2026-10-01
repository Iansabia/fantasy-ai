"""Approval texts with pros and cons, built only from numbers the engine already computed.

Waiver / free-agent add:          Trade proposal:
  Add X, drop Y                     Give A, B | get C (Partner)
  Pros: (2-3)                       Pros: (2-3)
  Cons: (2-3)                       Cons: (2-3)
  Verdict: ...                      For them: ...
                                    Verdict: ...
notify.msg_pending adds the header and the "Reply 'approve N' or 'reject N'" line.
Bullets are picked in a fixed priority order; each text stays under MAX_CHARS.
"""
from __future__ import annotations

from .slots import IR, NON_STARTING

MAX_CHARS = 820          # + ~80 chars of header / reply line from msg_pending stays under ~900
SLOT = {"QB": 0, "RB": 2, "WR": 4, "TE": 6}


def _cap(lines: list[str], n: int = 3) -> list[str]:
    return [l for l in lines if l][:n]


def _count_after(roster, pos: str, out_ids: set, add: list) -> int:
    return sum(1 for p in roster if p.position == pos and p.player_id not in out_ids and not p.on_ir_slot) + \
        sum(1 for p in add if p.position == pos)


def _thin(state, out_ids: set, add: list, positions) -> list[str]:
    """'leaves 1 TE' when a position drops to its number of starting slots (no backup left)."""
    out = []
    for pos in sorted(set(positions)):
        if pos not in SLOT:
            continue
        slots = state.slot_counts.get(SLOT[pos], 0) + (state.slot_counts.get(7, 0) if pos == "QB" else 0)
        n = _count_after(state.roster, pos, out_ids, add)
        if n <= slots:
            out.append(f"leaves {n} {pos}" + (" (no backup)" if n == slots else " (a starting slot goes empty)"))
    return out


def _fixes(state, incoming) -> list[str]:
    from . import posneed
    out = []
    for p in incoming:
        if p.position not in SLOT:
            continue
        views = posneed.view(state, state.my_team_id, p.position)
        bad = [v for v in views if v.verdict == "real problem"]
        if bad and p.season_rate() > bad[-1].player.season_rate():
            v = bad[-1]
            out.append(f"fixes {p.position}: {v.player.name} is {p.position} {v.rank} of {v.of} ({'; '.join(v.why)})")
    return out


def _trend(p) -> str | None:
    t = getattr(p, "trend", None)
    return t.label if t is not None else None


def _ecr_text(p) -> str | None:
    rec = getattr(p, "ecr", None)
    return rec.get("ros_text") if rec and rec.get("ros") else None


def _norm_rank(p, state) -> float | None:
    """Expert ROS rank scaled by how many players start at his position in this league
    (1.0 = the last starter), so ranks compare across positions (RB45 vs TE13)."""
    from .vorp import starters_per_position
    rec = getattr(p, "ecr", None)
    if not rec or not rec.get("ros") or p.position not in SLOT:
        return None
    n = starters_per_position(state).get(p.position) or 1
    return rec["ros"] / n


def _expert_side(state, incoming, outgoing) -> tuple[str | None, str | None]:
    """(pro, con): an expert-rank line goes under Pros only when experts favor what we get,
    under Cons when they favor what we give up."""
    def best(ps):
        vals = [(r, p) for p in ps if (r := _norm_rank(p, state)) is not None]
        return min(vals, key=lambda x: x[0]) if vals else None
    bi, bo = best(incoming), best(outgoing)
    names = lambda ps: ", ".join(f"{p.name} {_ecr_text(p)}" for p in ps if _ecr_text(p))
    if bi and (not bo or bi[0] < bo[0]):
        if bo or bi[0] <= 1.0:
            return f"experts favor what we get: {names(incoming)} ROS" + (f" vs {names(outgoing)}" if bo else ""), None
        return None, None
    if bo and (not bi or bo[0] <= bi[0]):
        if bi or bo[0] <= 1.0:
            gone = bo[1]
            return None, (f"experts favor what we give up: {gone.name} is {_ecr_text(gone)} ROS"
                          + (f" vs {names(incoming)}" if bi else ""))
    return None, None


def _status_cons(players, state) -> list[str]:
    out = []
    wk = state.scoring_period
    for p in players:
        if not p.is_healthy():
            out.append(f"{p.name} is {p.game_status.lower().replace('_', ' ')}")
        if p.bye_week in (wk, wk + 1, wk + 2):
            out.append(f"{p.name} has a bye in week {p.bye_week}")
    return out


def _fit(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    lines = text.split("\n")
    while len("\n".join(lines)) > MAX_CHARS and len(lines) > 3:
        # drop the longest bullet (never the first line or the verdict)
        idx = max(range(1, len(lines) - 1), key=lambda i: len(lines[i]) if lines[i].startswith("•") else -1)
        lines.pop(idx)
    return "\n".join(lines)[:MAX_CHARS]


def waiver_text(state, s) -> str:
    from .experts import caution, overrated
    add, d = s.add, s.drop
    pros = [f"lineup +{s.lineup_gain:.1f} pts over the next 3 wks (starts {s.starts_add} of 3)" if s.lineup_gain > 0 else None]
    if s.starts_add:
        pros += _fixes(state, [add])
    if _trend(add) == "uptrend":
        pros.append(f"{add.name} on an uptrend ({add.trend.next3 - 1:+.0%} next 3 wks)")
    ex_pro, ex_con = _expert_side(state, [add], [d] if d is not None else [])
    if ex_pro and not overrated(add, state):
        pros.append(ex_pro)
    if d is not None and s.add_value > s.drop_value:
        pros.append(f"rest of season {s.add_value:.0f} vs {s.drop_value:.0f}")
    cons = []
    if d is not None:
        cons += _thin(state, {d.player_id}, [add], [d.position])
        if ex_con:
            cons.append(f"dropping a {d.position} the experts rank {_ecr_text(d)} ROS ({add.name} is {_ecr_text(add) or 'unranked'})")
        if _trend(d) == "uptrend":
            cons.append(f"{d.name} is on an uptrend ({d.trend.next3 - 1:+.0%})")
    cons += _status_cons([add], state)
    w = caution(add, state)
    if w and w.startswith("CAUTION"):
        cons.append(f"experts disagree: {add.name} {_ecr_text(add)} ROS, we have him much higher")
    if s.kind == "WAIVER":
        cons.append("costs waiver priority")
    if d is not None and s.next3_drop > s.next3_add:
        cons.append(f"{d.name} projects more over 3 wks ({s.next3_drop:.1f} vs {s.next3_add:.1f})")
    head = f"Add {add.name} ({add.position}), drop {d.name} ({d.position})" if d else f"Add {add.name} ({add.position}), no drop"
    verdict = (f"Verdict: worth it, +{s.gain:.1f} pts over 3 wks (needs +{s.threshold:g})" if s.gain >= s.threshold
               else f"Verdict: below the bar, +{s.gain:.1f} (needs +{s.threshold:g})")
    lines = [head, "Pros:"] + [f"• {x}" for x in _cap(pros)] + ["Cons:"] + [f"• {x}" for x in (_cap(cons) or ["none found"])] + [verdict]
    return _fit("\n".join(lines))


def trade_text(state, idea) -> str:
    from .trend import next3
    give, get = idea.give, idea.get
    pros = [f"our lineup +{idea.our_gain:.1f} pts rest of season"]
    pros += _fixes(state, get)
    up = [p for p in get if _trend(p) == "uptrend"]
    if up:
        pros.append("buys " + ", ".join(f"{p.name} on an uptrend ({next3(p) - 1:+.0%})" for p in up))
    if idea.overpay is not None and idea.overpay <= 0:
        pros.append(f"FantasyCalc: we pay {idea.fc_give:.0f} for {idea.fc_get:.0f} ({idea.overpay_pct:+.1%})")
    ex_pro, ex_con = _expert_side(state, get, give)
    if ex_pro:
        pros.append(ex_pro)
    cons = [ex_con] if ex_con else []
    sold_up = [p for p in give if _trend(p) == "uptrend"]
    if sold_up:
        cons.append("sells " + ", ".join(f"{p.name} on an uptrend ({next3(p) - 1:+.0%})" for p in sold_up))
    cons += _thin(state, {p.player_id for p in give}, get, [p.position for p in give])
    if not idea.robust:
        cons.append("FRAGILE: verdict can flip if values move 5%")
    cons += _status_cons(get, state)
    cons += [w.replace("CAUTION: ", "experts disagree: ") for w in idea.cautions if w.startswith("CAUTION")]
    if idea.overpay is not None and idea.overpay_pct > 0.05:
        cons.append(f"overpay on FantasyCalc: {idea.overpay:+.0f} ({idea.overpay_pct:+.1%})")
    head = f"Give {', '.join(p.name for p in give)} | get {', '.join(p.name for p in get)} ({idea.partner})"
    them = f"For them: lineup {idea.their_gain:+.1f} ROS" + (f"; {idea.why_partner.split(';')[0]}" if idea.why_partner else "")
    verdict = ("Verdict: good for us and helps them" if idea.robust and not idea.hard_sell
               else "Verdict: good for us, close call for them" if not idea.hard_sell
               else "Verdict: hard sell for them")
    lines = [head, "Pros:"] + [f"• {x}" for x in _cap(pros)] + ["Cons:"] + \
            [f"• {x}" for x in (_cap(cons) or ["none found"])] + [them, verdict]
    return _fit("\n".join(lines))


def trade_eval_text(state, give, get, ev) -> str:
    """Same layout for a hand-picked `fai trade --propose` (no idea search behind it)."""
    from .trend import next3
    pros = [f"our lineup {ev.me.lineup_delta:+.1f} pts rest of season"] + _fixes(state, get)
    up = [p for p in get if _trend(p) == "uptrend"]
    if up:
        pros.append("buys " + ", ".join(f"{p.name} on an uptrend ({next3(p) - 1:+.0%})" for p in up))
    cons = []
    sold_up = [p for p in give if _trend(p) == "uptrend"]
    if sold_up:
        cons.append("sells " + ", ".join(f"{p.name} on an uptrend ({next3(p) - 1:+.0%})" for p in sold_up))
    cons += _thin(state, {p.player_id for p in give}, get, [p.position for p in give])
    cons += _status_cons(get, state)
    head = f"Give {', '.join(p.name for p in give)} | get {', '.join(p.name for p in get)} ({ev.partner.team_name})"
    lines = [head, "Pros:"] + [f"• {x}" for x in _cap(pros)] + ["Cons:"] + \
            [f"• {x}" for x in (_cap(cons) or ["none found"])] + \
            [f"For them: lineup {ev.partner.lineup_delta:+.1f} ROS", f"Verdict: {ev.verdict.lower()}"]
    return _fit("\n".join(lines))
