"""Inputs for the factor score. Each provider is optional and fails soft (returns empty).

Live status: parse functions are tested on fixtures. The network fetches for Odds API,
Open-Meteo, Sleeper projections and nflreadpy have not been run from this code yet.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from ..models import LeagueState
from ..news.cache import PoliteClient
from .context import ProGame, WeekContext
from .stadiums import STADIUMS

ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
SLEEPER_PROJ = "https://api.sleeper.app/projections/nfl/{season}/{week}"   # undocumented [unverified]
POS_ID = {"QB": "1", "RB": "2", "WR": "3", "TE": "4", "K": "5", "D/ST": "16"}


# ---------------------------------------------------------------- ESPN (already-read payloads)

def pro_team_maps(raw_pro: dict) -> tuple[dict[int, str], dict[str, str]]:
    """(proTeamId -> abbrev, 'Kansas City Chiefs' -> 'KC')."""
    ids, names = {}, {}
    for t in (raw_pro or {}).get("settings", {}).get("proTeams", []):
        ids[int(t["id"])] = t.get("abbrev", "")
        full = f"{t.get('location', '')} {t.get('name', '')}".strip()
        if full:
            names[full] = t.get("abbrev", "")
    return ids, names


def parse_pro_games(raw_pro: dict, week: int) -> dict[str, ProGame]:
    ids, _ = pro_team_maps(raw_pro)
    games: dict[str, ProGame] = {}
    for t in (raw_pro or {}).get("settings", {}).get("proTeams", []):
        for g in (t.get("proGamesByScoringPeriod") or {}).get(str(week), []):
            home, away = ids.get(g["homeProTeamId"], ""), ids.get(g["awayProTeamId"], "")
            if not home or not away:
                continue
            games[home] = ProGame(home, away, True, int(g.get("date", 0)), home)
            games[away] = ProGame(away, home, False, int(g.get("date", 0)), home)
    return games


def parse_positional_ratings(raw: dict, raw_pro: dict) -> tuple[dict, dict]:
    """ESPN view mPositionalRatings (same one espn-api reads):
    positionAgainstOpponent.positionalRatings[defaultPositionId].ratingsByOpponent[proTeamId]
    = {"average": pts allowed, "rank": n}. Returns (avg map, rank map) keyed (opp abbrev, pos)."""
    ids, _ = pro_team_maps(raw_pro)
    pos_by_id = {v: k for k, v in POS_ID.items()}
    avg, rank = {}, {}
    ratings = (raw or {}).get("positionAgainstOpponent", {}).get("positionalRatings", {})
    for pos_id, block in ratings.items():
        pos = pos_by_id.get(str(pos_id))
        if not pos:
            continue
        for team_id, r in (block.get("ratingsByOpponent") or {}).items():
            abbrev = ids.get(int(team_id))
            if not abbrev:
                continue
            if r.get("average") is not None:
                avg[(abbrev, pos)] = float(r["average"])
            if r.get("rank") is not None:
                rank[(abbrev, pos)] = int(r["rank"])
    return avg, rank


# ---------------------------------------------------------------- Vegas (The Odds API, optional key)

def parse_odds(events: list, name_to_abbrev: dict[str, str]) -> dict[str, tuple[float, float]]:
    """-> team abbrev: (implied team total, spread from that team's view; + = underdog)."""
    out = {}
    for ev in events or []:
        home, away = ev.get("home_team"), ev.get("away_team")
        spreads, total = {}, None
        for bk in ev.get("bookmakers", [])[:1]:          # first book is enough
            for mk in bk.get("markets", []):
                if mk.get("key") == "spreads":
                    spreads = {o["name"]: float(o["point"]) for o in mk.get("outcomes", []) if "point" in o}
                if mk.get("key") == "totals":
                    over = [o for o in mk.get("outcomes", []) if o.get("name") == "Over"]
                    total = float(over[0]["point"]) if over else None
        if total is None or home not in spreads or away not in spreads:
            continue
        for team in (home, away):
            ab = name_to_abbrev.get(team)
            if ab:
                s = spreads[team]
                out[ab] = (round(total / 2 - s / 2, 1), s)
    return out


def fetch_vegas(client: PoliteClient, name_to_abbrev: dict[str, str]) -> dict[str, tuple[float, float]]:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        return {}
    try:
        events = client.get(ODDS_URL, ttl=6 * 3600, params={"apiKey": key, "regions": "us",
                                                          "markets": "spreads,totals", "oddsFormat": "american"})
    except Exception:
        return {}
    return parse_odds(events, name_to_abbrev)


# ---------------------------------------------------------------- weather (Open-Meteo, no key)

def parse_weather(data: dict, kickoff_ms: int) -> tuple[float, float] | None:
    """Average wind (mph) and precipitation (mm/h) over the 3 game hours."""
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return None
    start = dt.datetime.fromtimestamp(kickoff_ms / 1000, dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    want = {(start + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(3)}
    idx = [i for i, t in enumerate(times) if t in want]
    if not idx:
        return None
    wind = sum(h["wind_speed_10m"][i] for i in idx) / len(idx)
    rain = sum(h["precipitation"][i] for i in idx) / len(idx)
    return round(wind, 1), round(rain, 2)


def fetch_weather(client: PoliteClient, games: dict[str, ProGame], teams: set[str]) -> dict[str, tuple[float, float]]:
    out = {}
    homes = {games[t].home_team: games[t].kickoff_ms for t in teams if t in games}
    for home, kickoff in homes.items():
        lat, lon, dome = STADIUMS.get(home, (0, 0, True))
        if dome or not kickoff:
            continue
        day = dt.datetime.fromtimestamp(kickoff / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        try:
            data = client.get(OPEN_METEO, ttl=3 * 3600, params={
                "latitude": lat, "longitude": lon, "hourly": "wind_speed_10m,precipitation",
                "wind_speed_unit": "mph", "timezone": "UTC", "start_date": day, "end_date": day})
        except Exception:
            continue
        w = parse_weather(data, kickoff)
        if w:
            out[home] = w
    return out


# ---------------------------------------------------------------- usage trend (nflreadpy, optional)

def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.lower().replace(".", "")).strip()


def load_usage(season: int, week: int, weeks: int = 3) -> dict[str, dict[str, list[float]]]:
    """Last `weeks` weeks of usage per player from nflverse via nflreadpy (optional dependency).
    Column names follow nflverse player_stats / snap_counts; any mismatch returns {}."""
    try:
        import nflreadpy as nfl
        stats = nfl.load_player_stats([season]).to_dicts()
        snaps = nfl.load_snap_counts([season]).to_dicts()
    except Exception:
        return {}
    lo = week - weeks
    out: dict[str, dict[str, list[float]]] = {}
    for r in sorted(stats, key=lambda r: r.get("week", 0)):
        if not (lo <= r.get("week", 0) < week):
            continue
        d = out.setdefault(_norm(r.get("player_display_name") or r.get("player_name") or ""), {})
        d.setdefault("target_share", []).append(float(r.get("target_share") or 0))
        d.setdefault("carries", []).append(float(r.get("carries") or 0))
    for r in sorted(snaps, key=lambda r: r.get("week", 0)):
        if not (lo <= r.get("week", 0) < week):
            continue
        d = out.setdefault(_norm(r.get("player") or ""), {})
        d.setdefault("snap_share", []).append(float(r.get("offense_pct") or 0))
    return out


# ---------------------------------------------------------------- second projection (Sleeper, optional)

def parse_sleeper_proj(rows: list, state: LeagueState) -> dict[int, float]:
    everyone = {(_norm(p.name), p.position): p for t in state.teams.values() for p in t.roster}
    everyone.update({(_norm(p.name), p.position): p for p in state.free_agents})
    out = {}
    for r in rows or []:
        pl = r.get("player") or {}
        key = (_norm(f"{pl.get('first_name', '')} {pl.get('last_name', '')}"), pl.get("position", ""))
        p = everyone.get(key)
        pts = (r.get("stats") or {}).get("pts_ppr")
        if p and pts is not None:
            out[p.player_id] = float(pts)
    return out


def fetch_sleeper_proj(client: PoliteClient, state: LeagueState) -> dict[int, float]:
    try:
        rows = client.get(SLEEPER_PROJ.format(season=state.year, week=state.scoring_period), ttl=6 * 3600,
                          params={"season_type": "regular", "position[]": ["QB", "RB", "WR", "TE", "K"]})
    except Exception:
        return {}
    return parse_sleeper_proj(rows, state)


# ---------------------------------------------------------------- assemble

def build_context(state: LeagueState, raw_pro: dict, raw_ratings: dict | None = None,
                  client: PoliteClient | None = None, online: bool = True) -> WeekContext:
    ctx = WeekContext(week=state.scoring_period)
    ctx.games = parse_pro_games(raw_pro, state.scoring_period)
    ctx.future_games = {w: parse_pro_games(raw_pro, w) for w in range(state.scoring_period + 1, state.scoring_period + 3)}
    if ctx.games:
        ctx.sources.append("ESPN schedule")
    if raw_ratings:
        ctx.def_vs_pos, ctx.def_rank = parse_positional_ratings(raw_ratings, raw_pro)
        if ctx.def_vs_pos or ctx.def_rank:
            ctx.sources.append("ESPN points allowed by position")
    if online:
        client = client or PoliteClient()
        _, names = pro_team_maps(raw_pro)
        ctx.vegas = fetch_vegas(client, names)
        teams = {p.pro_team for t in state.teams.values() for p in t.roster} | {p.pro_team for p in state.free_agents}
        ctx.weather = fetch_weather(client, ctx.games, teams)
        ctx.usage = load_usage(state.year, state.scoring_period)
        ctx.second_proj = fetch_sleeper_proj(client, state)
        for name, got in (("Vegas", ctx.vegas), ("Open-Meteo", ctx.weather), ("nflverse usage", ctx.usage),
                          ("Sleeper projections", ctx.second_proj)):
            if got:
                ctx.sources.append(name)
    return ctx
