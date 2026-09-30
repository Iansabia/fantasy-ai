"""Market value benchmark from FantasyCalc (public JSON API, redraft values).

GET https://api.fantasycalc.com/values/current?isDynasty=false&numQbs=<1|2>&numTeams=<n>&ppr=<0|0.5|1>
Each row: player {name, position, maybeTeam, espnId, ...}, value, overallRank, positionRank,
trend30Day. Cached daily. Players map by ESPN id first, then name (suffixes ignored) + team +
position. FC is a second opinion ("market value") for trades and a sanity check on our values;
it never replaces our decision value.
"""
from __future__ import annotations

from dataclasses import dataclass

from .news.cache import PoliteClient
from .usage_data import norm_name

FC_URL = "https://api.fantasycalc.com/values/current"
LOW_CORRELATION = 0.6


def fetch(client: PoliteClient, num_teams: int, num_qbs: int = 1, ppr: float = 1) -> list[dict]:
    try:
        return client.get(FC_URL, ttl=86400, params={"isDynasty": "false", "numQbs": num_qbs,
                                                     "numTeams": num_teams, "ppr": ppr}) or []
    except Exception:
        return []


def attach(state, rows: list[dict]) -> int:
    """Set p.market_value (and p.market_rank) on every player we can match. Returns matches."""
    by_espn, by_name = {}, {}
    for r in rows:
        pl = r.get("player") or {}
        rec = (float(r.get("value") or 0), int(r.get("overallRank") or 0), int(r.get("trend30Day") or 0))
        if pl.get("espnId"):
            by_espn[str(pl["espnId"])] = rec
        by_name[(norm_name(pl.get("name", "")), pl.get("position"), pl.get("maybeTeam"))] = rec
        by_name.setdefault((norm_name(pl.get("name", "")), pl.get("position"), None), rec)
    n = 0
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        rec = by_espn.get(str(p.player_id)) or by_name.get((norm_name(p.name), p.position, p.pro_team)) \
            or by_name.get((norm_name(p.name), p.position, None))
        if rec:
            p.market_value, p.market_rank, p.market_trend = rec
            n += 1
    return n


def fc_value(p) -> float | None:
    return getattr(p, "market_value", None)


# ---------------------------------------------------------------- comparison

def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 3) if va and vb else None


@dataclass
class Compare:
    overall: float | None
    by_position: dict[str, float | None]
    n: int
    ours_higher: list[tuple]     # (player, our rank, fc rank)
    fc_higher: list[tuple]


def compare(state, top: int = 10) -> Compare:
    wk = state.scoring_period
    players = [p for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents
               if fc_value(p) is not None and p.position not in ("K", "D/ST")]
    ours = [p.ros_value(wk) for p in players]
    fcs = [fc_value(p) for p in players]
    by_pos = {}
    for pos in ("QB", "RB", "WR", "TE"):
        idx = [i for i, p in enumerate(players) if p.position == pos]
        by_pos[pos] = spearman([ours[i] for i in idx], [fcs[i] for i in idx])
    r_ours = _ranks([-x for x in ours])      # 1 = best
    r_fc = _ranks([-x for x in fcs])
    diffs = sorted(range(len(players)), key=lambda i: r_fc[i] - r_ours[i])
    ours_higher = [(players[i], int(r_ours[i]), int(r_fc[i])) for i in diffs[::-1][:top] if r_fc[i] > r_ours[i]]
    fc_higher = [(players[i], int(r_ours[i]), int(r_fc[i])) for i in diffs[:top] if r_fc[i] < r_ours[i]]
    return Compare(spearman(ours, fcs), by_pos, len(players), ours_higher, fc_higher)


def reasons_for(p, state) -> str:
    """Why our value differs: hype label, injury, bye, notable factors."""
    from .factors.score import top_reasons
    bits = []
    h = getattr(p, "hype", None)
    if h is not None and h.label != "Steady":
        bits.append(h.label.lower())
    if not p.is_healthy():
        bits.append(p.game_status.lower())
    if p.bye_week and state.scoring_period <= p.bye_week <= state.scoring_period + 2:
        bits.append(f"bye week {p.bye_week}")
    if p.ros_multiplier and abs(p.ros_multiplier - 1) >= 0.05:
        bits.append(f"ROS x{p.ros_multiplier:.2f} (role/buzz/usage)")
    bits += top_reasons(p, 1)
    return "; ".join(bits) or "projection difference"
