"""Expert consensus rankings (FantasyPros ECR, PPR): this week by position and rest of season.

Source: the public rankings pages embed the table as `var ecrData = {...}` JSON:
  https://www.fantasypros.com/nfl/rankings/ros-ppr-overall.php       rest of season (pos_rank WR18...)
  https://www.fantasypros.com/nfl/rankings/{qb,ppr-rb,ppr-wr,ppr-te}.php   this week
Pages are allowed by robots.txt (only /api/, /json/, /ajax/ ... are disallowed). Five pages,
fetched through PoliteClient and cached 12 hours. FantasyPros publishes only this week and rest of
season (no week+1 / week+2 ECR), so "next few weeks" = this week + ROS.

Information only: there is no free ECR history to backtest a blend with, so ECR never changes our
values. It is shown next to our rank, with a flag when we are more than DISAGREE spots apart.
"""
from __future__ import annotations

import json
import re

from .usage_data import norm_name

BASE = "https://www.fantasypros.com/nfl/rankings/"
PAGES = {"ros": "ros-ppr-overall.php", "QB": "qb.php", "RB": "ppr-rb.php", "WR": "ppr-wr.php", "TE": "ppr-te.php"}
TTL = 12 * 3600
DISAGREE = 10
TRIPWIRE = 20          # waiver adds / trade targets: we rank him 20+ spots above the experts -> low confidence
TEAM_FIX = {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"}


def parse_page(html: str) -> list[dict]:
    m = re.search(r"var ecrData = (\{.*?\});\s*\n", html or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1)).get("players", [])
    except ValueError:
        return []


def _pos_num(pos_rank: str | None) -> tuple[str, int] | None:
    m = re.match(r"([A-Z/]+?)(\d+)$", pos_rank or "")
    return (m.group(1), int(m.group(2))) if m else None


def build(pages: dict[str, list[dict]]) -> dict[tuple[str, str], dict]:
    """(norm name, position) -> {"ros": 18, "week": 12, "ros_text": "WR18", "week_text": "WR12", "team": "SEA"}."""
    out: dict[tuple[str, str], dict] = {}
    for key, rows in pages.items():
        for r in rows:
            pr = _pos_num(r.get("pos_rank"))
            if not pr:
                continue
            pos, num = pr
            if pos == "DST":
                pos = "D/ST"
            rec = out.setdefault((norm_name(r.get("player_name", "")), pos), {"team": r.get("player_team_id")})
            slot = "ros" if key == "ros" else "week"
            rec[slot], rec[f"{slot}_text"] = num, f"{pos}{num}"
    return out


def fetch(client) -> dict[tuple[str, str], dict]:
    pages = {}
    for key, page in PAGES.items():
        try:
            html = client.get(BASE + page, ttl=TTL, as_json=False)
        except Exception:
            continue
        pages[key] = parse_page(html)
    return build(pages)


def attach(state, table: dict) -> int:
    n = 0
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        rec = table.get((norm_name(p.name), p.position))
        if rec:
            p.ecr = rec
            n += 1
    return n


def our_pos_ranks(state, fresh: bool = False) -> dict[int, int]:
    """Our rest-of-season rank within position over every player we loaded (rosters + FAs).
    Cached on the state (values are fixed once engine.prepare has run)."""
    if not fresh and getattr(state, "_our_ranks", None) is not None:
        return state._our_ranks
    wk = state.scoring_period
    seen, by_pos = set(), {}
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if p.player_id in seen or p.position not in ("QB", "RB", "WR", "TE"):
            continue
        seen.add(p.player_id)
        by_pos.setdefault(p.position, []).append(p)
    out = {}
    for pos, ps in by_pos.items():
        for i, p in enumerate(sorted(ps, key=lambda q: q.ros_value(wk), reverse=True), 1):
            out[p.player_id] = i
    state._our_ranks = out
    return out


def caution(p, state) -> str | None:
    """Plain warning when we are 20+ spots off the experts on this player (either way)."""
    rec = getattr(p, "ecr", None)
    ours = our_pos_ranks(state).get(p.player_id)
    if not rec or not rec.get("ros") or ours is None:
        return None
    gap = rec["ros"] - ours
    if abs(gap) <= TRIPWIRE:
        return None
    if gap > 0:
        return (f"CAUTION: we rank {p.name} {p.position}{ours}, experts {rec['ros_text']} ROS "
                f"({gap} spots lower): low confidence, we may be overrating him")
    return f"NOTE: experts rank {p.name} {rec['ros_text']} ROS, we have {p.position}{ours} ({-gap} spots higher than us)"


def overrated(p, state) -> bool:
    """We rank him 20+ spots above the experts."""
    rec = getattr(p, "ecr", None)
    ours = our_pos_ranks(state).get(p.player_id)
    return bool(rec and rec.get("ros") and ours is not None and rec["ros"] - ours > TRIPWIRE)


def note(p, state, ranks: dict[int, int] | None = None) -> str | None:
    """'ECR WR18 ROS, WR12 this week; ours WR14' (+ a flag when we disagree by more than 10)."""
    rec = getattr(p, "ecr", None)
    ranks = ranks if ranks is not None else our_pos_ranks(state)
    ours = ranks.get(p.player_id)
    if not rec and ours is None:
        return None
    bits = []
    if rec and rec.get("ros"):
        bits.append(f"ECR {rec['ros_text']} ROS")
    if rec and rec.get("week"):
        bits.append(f"{rec['week_text']} this week")
    txt = ", ".join(bits) if bits else "no ECR"
    if ours is not None:
        txt += f"; ours {p.position}{ours}"
        if rec and rec.get("ros") and abs(rec["ros"] - ours) > DISAGREE:
            txt += f" (BIG DISAGREEMENT: {'we are higher' if ours < rec['ros'] else 'experts are higher'})"
    return txt


def norm_rank(p, state) -> float | None:
    """Expert ROS rank scaled by how many players start at his position in this league
    (1.0 = the last starter), so ranks compare across positions."""
    from .vorp import starters_per_position
    rec = getattr(p, "ecr", None)
    if not rec or not rec.get("ros") or p.position not in ("QB", "RB", "WR", "TE"):
        return None
    return rec["ros"] / (starters_per_position(state).get(p.position) or 1)


def favors(a, b, state) -> bool:
    """Do the experts rank a ahead of b for the rest of the season (cross-position safe)?"""
    ra, rb = norm_rank(a, state), norm_rank(b, state)
    return ra is not None and rb is not None and ra < rb
