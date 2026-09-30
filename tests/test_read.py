from conftest import b
from fantasy_ai.espn_read import parse_league


def test_parse_roster_fields(demo_state):
    s = demo_state
    assert s.scoring_period == 4 and s.year == 2026
    assert s.slot_counts == {0: 1, 2: 2, 4: 2, 6: 1, 23: 1, 16: 1, 17: 1, 20: 7, 21: 1}
    tank = next(p for p in s.roster if p.name == "Thursday Tank")
    assert tank.locked and tank.lineup_slot == 2 and tank.proj_week == 14.0
    bill = next(p for p in s.roster if p.name == "Bye Bill")
    assert bill.bye_week == 4 and bill.on_bye(4) and bill.pro_team == "BBB"
    hal = next(p for p in s.roster if p.name == "Hurt Hal")
    assert hal.is_out() and not hal.available(4)
    assert s.matchup["opp_name"] == "Rival Squad"
    assert [t["type"] for t in s.transactions] == ["WAIVER"]  # ROSTER shuffles filtered out
    assert {p.status for p in s.free_agents} == {"FREEAGENT", "WAIVERS"}


def test_be6_slot_config():
    s = parse_league(b.league([(23, "Cousin", b.demo_team1())], b.BE6_SLOTS), b.pro_teams(), 23)
    assert s.slot_counts[20] == 6 and s.my_team_id == 23
