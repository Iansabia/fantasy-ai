"""Read side. espn-api handles the HTTP GETs and cookies; we parse the raw JSON ourselves.

Why raw JSON: espn-api's Player object drops the numeric lineupSlotId and the
playerPoolEntry.lineupLocked flag, and the optimizer and the writer need both exactly.
Parsing raw payloads also means the same parser runs on offline fixtures.

Raw views used (names from heyitaki constants.ts VIEW):
  mTeam, mRoster, mSettings, mMatchup   -> league payload
  proTeamSchedules_wl                   -> NFL bye weeks (season-level, public)
  kona_player_info + x-fantasy-filter   -> free agents / waivers
  mTransactions2                        -> recent transactions
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Credentials, LeagueConfig
from .models import LeagueState, Player, Team
from .slots import POSITION_BY_ID

LEAGUE_VIEWS = ["mTeam", "mRoster", "mSettings", "mMatchup"]


# ---------------------------------------------------------------- parsing (pure)

def parse_pro_teams(raw_pro: dict) -> dict[int, dict]:
    """proTeamId -> {"abbrev", "bye"} from the proTeamSchedules_wl view."""
    out: dict[int, dict] = {}
    for t in (raw_pro or {}).get("settings", {}).get("proTeams", []):
        out[int(t["id"])] = {"abbrev": t.get("abbrev", ""), "bye": t.get("byeWeek") or None}
    return out


def _projection(stats: list[dict], year: int, split: int, period: int) -> float:
    # statSourceId 1 = projected; statSplitTypeId 1 = one week, 0 = season
    for s in stats or []:
        if (s.get("seasonId") == year and s.get("statSourceId") == 1
                and s.get("statSplitTypeId") == split and s.get("scoringPeriodId") == period):
            return round(float(s.get("appliedTotal", 0.0)), 2)
    return 0.0


def parse_player(entry: dict, year: int, week: int, pro_teams: dict[int, dict]) -> Player:
    """Parse a roster entry (teams[].roster.entries[]) or a free-agent row (players[])."""
    pool = entry.get("playerPoolEntry") or {}
    p = pool.get("player") or entry.get("player") or {}
    pro_id = int(p.get("proTeamId", 0) or 0)
    pro = pro_teams.get(pro_id, {})
    pos_id = int(p.get("defaultPositionId", 0) or 0)
    stats = p.get("stats", [])
    return Player(
        player_id=int(p.get("id", entry.get("playerId", entry.get("id", 0)))),
        name=p.get("fullName", "Unknown"),
        position=POSITION_BY_ID.get(pos_id, f"POS{pos_id}"),
        pro_team=pro.get("abbrev", ""),
        eligible_slots=[int(s) for s in p.get("eligibleSlots", [])],
        lineup_slot=int(entry.get("lineupSlotId", 20)),
        proj_week=_projection(stats, year, 1, week),
        proj_season=_projection(stats, year, 0, 0),
        injury_status=(p.get("injuryStatus") or entry.get("injuryStatus") or "ACTIVE"),
        bye_week=pro.get("bye"),
        locked=bool(pool.get("lineupLocked", entry.get("lineupLocked", False))),
        on_team_id=int(pool.get("onTeamId", entry.get("onTeamId", 0)) or 0),
        status=entry.get("status", "ONTEAM"),
        percent_owned=round(float((p.get("ownership") or {}).get("percentOwned", 0.0)), 1),
    )


def _team_name(t: dict) -> str:
    if t.get("name"):
        return t["name"]
    return f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t.get('id')}"


def parse_matchup(raw_league: dict, team_id: int, period: int, names: dict[int, str]) -> dict | None:
    for m in raw_league.get("schedule", []):
        if m.get("matchupPeriodId") != period:
            continue
        home, away = m.get("home") or {}, m.get("away") or {}
        if team_id not in (home.get("teamId"), away.get("teamId")):
            continue
        me, opp = (home, away) if home.get("teamId") == team_id else (away, home)
        return {
            "matchup_period": period,
            "my_points": round(float(me.get("totalPoints", 0.0)), 2),
            "opp_team_id": opp.get("teamId"),
            "opp_name": names.get(opp.get("teamId"), "BYE"),
            "opp_points": round(float(opp.get("totalPoints", 0.0)), 2),
        }
    return None


def parse_transactions(raw_tx: dict | None, names: dict[int, str]) -> list[dict]:
    out = []
    for t in (raw_tx or {}).get("transactions", []):
        if t.get("type") == "ROSTER":
            continue  # lineup shuffles are noise in a weekly report
        out.append({
            "id": t.get("id"),
            "type": t.get("type"),
            "status": t.get("status"),
            "team": names.get(t.get("teamId"), str(t.get("teamId"))),
            "team_id": t.get("teamId"),
            "bid": t.get("bidAmount", 0),
            "items": [{"type": i.get("type"), "player_id": i.get("playerId")} for i in t.get("items", [])],
        })
    return out


def parse_league(raw_league: dict, raw_pro: dict, my_team_id: int,
                 raw_fa: dict | None = None, raw_tx: dict | None = None) -> LeagueState:
    year = int(raw_league.get("seasonId"))
    week = int(raw_league.get("scoringPeriodId") or raw_league.get("status", {}).get("latestScoringPeriod", 1))
    mperiod = int(raw_league.get("status", {}).get("currentMatchupPeriod", week))
    pro_teams = parse_pro_teams(raw_pro)
    counts = raw_league.get("settings", {}).get("rosterSettings", {}).get("lineupSlotCounts", {})
    slot_counts = {int(k): int(v) for k, v in counts.items() if int(v) > 0}

    teams: dict[int, Team] = {}
    for t in raw_league.get("teams", []):
        roster = [parse_player(e, year, week, pro_teams) for e in (t.get("roster") or {}).get("entries", [])]
        for p in roster:
            p.on_team_id = int(t["id"])
        teams[int(t["id"])] = Team(team_id=int(t["id"]), name=_team_name(t), roster=roster)
    if my_team_id not in teams:
        raise ValueError(f"team_id {my_team_id} not found in league {raw_league.get('id')}")
    names = {tid: tm.name for tid, tm in teams.items()}

    fas = [parse_player(e, year, week, pro_teams) for e in (raw_fa or {}).get("players", [])]
    fas = [p for p in fas if p.status in ("FREEAGENT", "WAIVERS")]

    return LeagueState(
        league_id=int(raw_league.get("id")), year=year, my_team_id=my_team_id,
        scoring_period=week, matchup_period=mperiod, slot_counts=slot_counts, teams=teams,
        free_agents=fas, matchup=parse_matchup(raw_league, my_team_id, mperiod, names),
        transactions=parse_transactions(raw_tx, names),
        league_name=raw_league.get("settings", {}).get("name", ""),
    )


# ---------------------------------------------------------------- fetching

class EspnReader:
    """Loads a league either from a fixture dir (offline) or from ESPN via espn-api.

    Fixture dir layout: league.json, pro_teams.json, free_agents.json, transactions.json
    (same raw shapes ESPN returns).
    """

    def __init__(self, cfg: LeagueConfig, creds: Credentials | None = None):
        self.cfg = cfg
        self.creds = creds
        self._req = None

    # -- network (untested until real league ids arrive) --
    def _request(self):
        if self._req is None:
            from espn_api.requests.espn_requests import EspnFantasyRequests
            cookies = self.creds.cookies() if self.creds else None
            self._req = EspnFantasyRequests(sport="nfl", year=self.cfg.year,
                                            league_id=self.cfg.league_id, cookies=cookies)
        return self._req

    def _fixture(self, name: str) -> dict | None:
        path: Path = self.cfg.fixture_dir / name  # type: ignore[operator]
        return json.loads(path.read_text()) if path.exists() else None

    def raw_league(self) -> dict:
        if self.cfg.fixture_dir:
            return self._fixture("league.json") or {}
        return self._request().league_get(params={"view": LEAGUE_VIEWS})

    def raw_pro_teams(self) -> dict:
        if self.cfg.fixture_dir:
            return self._fixture("pro_teams.json") or {}
        return self._request().get_pro_schedule()

    def raw_free_agents(self, week: int, size: int = 50) -> dict:
        if self.cfg.fixture_dir:
            return self._fixture("free_agents.json") or {}
        # same filter shape espn-api League.free_agents() sends
        filters = {"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": size,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }}
        return self._request().league_get(
            params={"view": "kona_player_info", "scoringPeriodId": week},
            headers={"x-fantasy-filter": json.dumps(filters)},
        )

    def raw_transactions(self, week: int) -> dict:
        if self.cfg.fixture_dir:
            return self._fixture("transactions.json") or {}
        return self._request().league_get(params={"view": "mTransactions2", "scoringPeriodId": week})

    def load(self, free_agents: bool = False, transactions: bool = False) -> LeagueState:
        """Minimal reads: league + pro schedule, plus FA / transactions only when asked."""
        raw = self.raw_league()
        week = int(raw.get("scoringPeriodId", 1))
        return parse_league(
            raw, self.raw_pro_teams(), self.cfg.team_id,
            raw_fa=self.raw_free_agents(week) if free_agents else None,
            raw_tx=self.raw_transactions(week) if transactions else None,
        )

    def roster_slots(self) -> dict[int, int]:
        """{playerId: lineupSlotId} for my team, used for read-back after a write."""
        state = parse_league(self.raw_league(), {}, self.cfg.team_id)
        return {p.player_id: p.lineup_slot for p in state.roster}
