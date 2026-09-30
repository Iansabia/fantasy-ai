"""Roster % as a signal. ESPN returns ownership.percentOwned / percentChange / percentStarted
for every player; Sleeper trending adds/drops come through news/adjust.py.

- data/ownership_history.json: {"weeks": {"<season>-<week>": {player_id: percent_owned}}},
  written every run, last 6 weeks kept. Gives this week's change and the 3-week trend.
- Capped factor on rest-of-season value (waivers, trades): rising fast = bump, falling = fade.
- Flags: our bench players whose roster % is collapsing ("falling"), and free agents spiking
  ("hot: likely claimed soon").
- percentStarted feeds a small consensus factor in the weekly score (factors/score.py).
"""
from __future__ import annotations

from .models import LeagueState, Player
from .prefs import _load, _save, data_dir

KEEP_WEEKS = 6
OWN_CAP = 0.08            # max +-8% on rest-of-season value
HOT_WEEK = 10.0           # +10 points of roster % in a week = hot
FALLING_WEEK, FALLING_3WK = -8.0, -15.0


def _key(year: int, week: int) -> str:
    return f"{year}-{week:02d}"


def record(state: LeagueState, directory=None) -> dict:
    """Store this week's roster % for every player we saw. Returns the full history."""
    path = (directory or data_dir()) / "ownership_history.json"
    hist = _load(path, {"weeks": {}})
    wk = hist["weeks"].setdefault(_key(state.year, state.scoring_period), {})
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        if p.percent_owned >= 0:
            wk[str(p.player_id)] = p.percent_owned
    keys = sorted(hist["weeks"])[-KEEP_WEEKS:]
    hist["weeks"] = {k: hist["weeks"][k] for k in keys}
    _save(path, hist)
    return hist


def trend(p: Player, hist: dict, year: int, week: int) -> tuple[float | None, float | None]:
    """(change this week, change over 3 weeks) in roster-% points."""
    weeks = hist.get("weeks", {})
    now = p.percent_owned
    wk_chg = p.percent_change if p.percent_change is not None else None
    if wk_chg is None:
        prev = weeks.get(_key(year, week - 1), {}).get(str(p.player_id))
        wk_chg = round(now - prev, 1) if prev is not None else None
    old = weeks.get(_key(year, week - 3), {}).get(str(p.player_id))
    three = round(now - old, 1) if old is not None else None
    return wk_chg, three


def apply(state: LeagueState, hist: dict) -> None:
    """Set own_week / own_3wk on every player and fold a capped factor into ros_multiplier."""
    for p in [q for t in state.teams.values() for q in t.roster] + state.free_agents:
        w, t3 = trend(p, hist, state.year, state.scoring_period)
        p.own_week, p.own_3wk = w, t3
        score = 0.004 * (w or 0) + 0.002 * (t3 or 0)
        mult = 1 + max(-OWN_CAP, min(OWN_CAP, score))
        if abs(mult - 1) >= 0.005:
            p.ros_multiplier = round(p.ros_multiplier * mult, 3)


def own_note(p: Player) -> str | None:
    """'rostered 42% (-6% this week, -15% over 3 wks)', plus hot / falling tags."""
    if p.percent_owned is None or p.percent_owned < 0:
        return None
    w, t3 = getattr(p, "own_week", None), getattr(p, "own_3wk", None)
    bits = []
    if w is not None:
        bits.append(f"{w:+.0f}% this week")
    if t3 is not None:
        bits.append(f"{t3:+.0f}% over 3 wks")
    note = f"rostered {p.percent_owned:.0f}%" + (f" ({', '.join(bits)})" if bits else "")
    if p.status in ("FREEAGENT", "WAIVERS") and w is not None and w >= HOT_WEEK:
        note += ", hot: likely claimed soon if you wait"
    elif p.status == "ONTEAM" and ((w is not None and w <= FALLING_WEEK) or (t3 is not None and t3 <= FALLING_3WK)):
        note += ", falling: the league is dropping him"
    return note if (bits or p.percent_owned) else None
