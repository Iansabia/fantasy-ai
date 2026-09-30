"""ESPN id tables.

Slot ids match heyitaki/espn-fantasy-football-mcp src/espn/constants.ts (SLOT_BY_ID) and
espn-api football/constant.py POSITION_MAP (23 = RB/WR/TE flex, 7 = OP superflex,
20 = bench, 21 = IR). Confirmed on the wire by jwulff/fantasy-sports
docs/research/05-espn-write-surface.md section 2.1 (4 = WR, 20 = BE, 23 = RB/WR/TE, 2 = RB,
0 = QB, 6 = TE, 16 = D/ST, 17 = K, 21 = IR).
"""

SLOT_BY_ID: dict[int, str] = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP",
    8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S", 14: "DB", 15: "DP",
    16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE", 21: "IR", 22: "SLOT22",
    23: "RB/WR/TE", 24: "ER", 25: "Rookie",
}
SLOT_ID: dict[str, int] = {v: k for k, v in SLOT_BY_ID.items()}
SLOT_ID["FLEX"] = 23

BENCH = 20
IR = 21
NON_STARTING = {BENCH, IR}

# heyitaki constants.ts POSITION_BY_ID (player.defaultPositionId)
POSITION_BY_ID: dict[int, str] = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 9: "DT", 10: "DE",
    11: "LB", 12: "CB", 13: "S", 14: "HC", 16: "D/ST",
}

# Injury statuses that mean "will not play": always bench these.
OUT_STATUSES = {"OUT", "INJURY_RESERVE", "IR", "SUSPENSION"}


def slot_name(slot_id: int) -> str:
    return SLOT_BY_ID.get(slot_id, f"SLOT{slot_id}")
