"""Build raw ESPN-shaped JSON for tests and the offline demo league.

Shapes follow what the ESPN read API returns for views mTeam/mRoster/mSettings/mMatchup,
kona_player_info, mTransactions2 and proTeamSchedules_wl. All names and ids are made up.
"""
from __future__ import annotations

YEAR = 2026
POS_ID = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "D/ST": 16}
ELIGIBLE = {  # ESPN eligibleSlots per position (0 QB, 2 RB, 3 RB/WR, 4 WR, 5 WR/TE, 6 TE, 7 OP, 16 D/ST, 17 K, 20 BE, 21 IR, 23 FLEX)
    "QB": [0, 7, 20, 21],
    "RB": [2, 3, 23, 7, 20, 21],
    "WR": [3, 4, 5, 23, 7, 20, 21],
    "TE": [5, 6, 23, 7, 20, 21],
    "D/ST": [16, 20, 21],
    "K": [17, 20, 21],
}
# 12-team style settings: QB1 RB2 WR2 TE1 FLEX1 D/ST1 K1 BE7 IR1
BE7_SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 7, "21": 1}
# 10-team style: same starters, BE6
BE6_SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 6, "21": 1}
# generic superflex league for OP tests
SUPERFLEX_SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "7": 1, "16": 1, "17": 1, "20": 6, "21": 1}

PRO_TEAMS = [  # id, abbrev, bye
    (1, "AAA", 5), (2, "BBB", 4), (3, "CCC", 6), (4, "DDD", 7), (5, "EEE", 9), (6, "FFF", 10),
]


def player(pid, name, pos, proj, season=None, pro=1, injury="ACTIVE", status=None, owned=50.0):
    season = proj * 16 if season is None else season
    p = {
        "id": pid, "fullName": name, "defaultPositionId": POS_ID[pos],
        "eligibleSlots": ELIGIBLE[pos], "proTeamId": pro, "injuryStatus": injury,
        "injured": injury != "ACTIVE", "ownership": {"percentOwned": owned},
        "stats": [
            {"seasonId": YEAR, "statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": 4, "appliedTotal": proj},
            {"seasonId": YEAR, "statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0, "appliedTotal": season},
        ],
    }
    return p


def entry(p, slot, locked=False, team_id=1):
    return {"playerId": p["id"], "lineupSlotId": slot, "injuryStatus": p["injuryStatus"],
            "playerPoolEntry": {"id": p["id"], "onTeamId": team_id, "lineupLocked": locked, "player": p}}


def fa_row(p, status="FREEAGENT"):
    return {"id": p["id"], "status": status, "onTeamId": 0, "player": p}


def league(teams, slot_counts=BE7_SLOTS, week=4, league_id=99):
    return {
        "id": league_id, "seasonId": YEAR, "scoringPeriodId": week,
        "status": {"currentMatchupPeriod": week, "latestScoringPeriod": week},
        "settings": {"name": "Demo League", "rosterSettings": {"lineupSlotCounts": slot_counts}},
        "teams": [{"id": tid, "name": name, "roster": {"entries": entries}} for tid, name, entries in teams],
        "schedule": [{"matchupPeriodId": week, "home": {"teamId": 1, "totalPoints": 31.4},
                      "away": {"teamId": 2, "totalPoints": 18.2}}],
    }


def pro_teams():
    return {"settings": {"proTeams": [{"id": i, "abbrev": a, "byeWeek": b} for i, a, b in PRO_TEAMS]}}


# ---- the demo league (team 1 = us, week 4; pro team 2 "BBB" is on bye week 4)

def demo_team1():
    return [
        entry(player(101, "Quinn Arm", "QB", 20.0), 0),
        entry(player(102, "Backup Bo", "QB", 15.0), 20),
        entry(player(201, "Thursday Tank", "RB", 14.0, pro=3), 2, locked=True),   # played Thursday
        entry(player(202, "Hurt Hal", "RB", 10.0, injury="OUT"), 2),              # OUT in a starting slot
        entry(player(203, "Ready Rex", "RB", 12.0, pro=4), 20),
        entry(player(204, "Depth Dan", "RB", 9.0, pro=5), 20),
        entry(player(205, "Season Sid", "RB", 0.0, season=150, injury="INJURY_RESERVE"), 21),
        entry(player(301, "Wide Will", "WR", 16.0), 4),
        entry(player(302, "Bye Bill", "WR", 15.0, pro=2), 4),                     # on bye
        entry(player(303, "Bench Ben", "WR", 13.0, pro=3), 20),
        entry(player(304, "Flex Fred", "WR", 8.0, pro=4), 23),
        entry(player(305, "Slot Sam", "WR", 10.0, pro=5), 20),
        entry(player(401, "Tight Ted", "TE", 9.0), 6),
        entry(player(402, "Tiny Tim", "TE", 4.0, season=40, pro=6), 20),
        entry(player(501, "Iron Wall", "D/ST", 7.0, pro=6), 16),
        entry(player(601, "Boot Kicks", "K", 8.0, pro=3), 17),
    ]


def demo_team2():
    return [entry(p, s, team_id=2) for p, s in [
        (player(111, "Rival QB", "QB", 18.0, pro=4), 0),
        (player(211, "Rival RB1", "RB", 17.0, pro=5), 2),
        (player(212, "Rival RB2", "RB", 11.0, pro=6), 2),
        (player(213, "Rival RB3", "RB", 7.0, pro=1), 20),
        (player(311, "Rival WR1", "WR", 18.0, pro=6), 4),
        (player(312, "Rival WR2", "WR", 12.0, pro=1), 4),
        (player(313, "Rival WR3", "WR", 11.5, pro=3), 23),
        (player(314, "Rival WR4", "WR", 9.0, pro=4), 20),
        (player(411, "Rival TE", "TE", 10.0, pro=5), 6),
        (player(511, "Rival D", "D/ST", 6.0, pro=1), 16),
        (player(611, "Rival K", "K", 7.5, pro=4), 17),
    ]]


def demo_league():
    return league([(1, "My Team", demo_team1()), (2, "Rival Squad", demo_team2())])


def demo_free_agents():
    return {"players": [
        fa_row(player(901, "Waiver Wonder", "TE", 8.0, season=120, pro=4, owned=30.0), "WAIVERS"),
        fa_row(player(902, "Street Runner", "RB", 6.0, season=70, pro=6, owned=12.0)),
        fa_row(player(903, "Meh Receiver", "WR", 7.0, season=150, pro=5, owned=8.0)),  # worse than Flex Fred
        fa_row(player(904, "Big Leg", "K", 9.5, season=150, pro=1, owned=40.0)),   # bench K: never an add
        fa_row(player(905, "Rising Rb", "RB", 13.0, season=210, pro=3, owned=25.0)),  # real upgrade at RB
    ]}


def demo_transactions():
    return {"transactions": [
        {"id": "t1", "type": "WAIVER", "status": "EXECUTED", "teamId": 2, "bidAmount": 0,
         "items": [{"type": "ADD", "playerId": 314}, {"type": "DROP", "playerId": 999}]},
        {"id": "t2", "type": "ROSTER", "status": "EXECUTED", "teamId": 1, "items": []},
    ]}
