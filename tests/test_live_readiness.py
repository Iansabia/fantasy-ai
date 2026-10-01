"""Live-run edge cases from the trust audit: dry run == send, 401, IR shape, fill-ins, traded
players, OUT vs IR, and the expert-disagreement tripwire."""
import json

import httpx
import pytest

from conftest import b, make_state
from fantasy_ai import engine, experts, teamneeds, waivers
from fantasy_ai.config import Credentials
from fantasy_ai.espn_read import parse_player
from fantasy_ai.espn_write import TransactionWriter, lineup_payload
from fantasy_ai.lineup import Move, optimize
from fantasy_ai.pacing import PacingStop
from fantasy_ai.slots import BENCH, IR


def test_send_posts_exactly_the_dry_run_payload(cfg):
    res = optimize(make_state([b.entry(b.player(1, "Bad Qb", "QB", 10), 0, team_id=3),
                               b.entry(b.player(2, "Good Qb", "QB", 25), 20, team_id=3)], team_id=3))
    payload = lineup_payload(3, 4, res.moves)
    posted = {}

    def handler(request):
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "EXECUTED"})

    w = TransactionWriter(cfg, Credentials("s2", "{sw}"), readback=lambda: {1: 20, 2: 0},
                          transport=httpx.MockTransport(handler))
    dry = w.submit(payload)
    printed = json.loads(dry.message[dry.message.index("{"):])
    w.submit(payload, send=True)
    assert posted["body"] == printed == payload            # same moves, nothing extra
    assert sorted((i["playerId"], i["toLineupSlotId"]) for i in posted["body"]["items"]) == [(1, BENCH), (2, 0)]


def test_401_on_write_is_a_clear_stop_without_cooldown(cfg):
    w = TransactionWriter(cfg, Credentials("SECRETVAL", "SWIDVAL"), transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(PacingStop) as e:
        w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    msg = str(e.value)
    assert "expired or wrong" in msg and "ESPN_S2_COUSIN" in msg and "no cooldown" in msg
    assert "SECRETVAL" not in msg and "SWIDVAL" not in msg
    assert w.pacer.in_cooldown(cfg.name) is None


def test_401_on_read_is_a_clear_stop_without_cooldown(cfg, tmp_path):
    from espn_api.requests.espn_requests import ESPNAccessDenied
    from fantasy_ai.espn_read import EspnReader
    from fantasy_ai.pacing import Pacer
    pacer = Pacer(path=tmp_path / "p.json", sleep=lambda s: None, rand=lambda a, b: 0.0)
    r = EspnReader(cfg, Credentials("s2", "sw"), pacer=pacer)

    def boom():
        raise ESPNAccessDenied("League cannot be accessed with the provided credentials")
    with pytest.raises(PacingStop) as e:
        r._fetch("league", boom)
    assert "ESPN refused access (401)" in str(e.value) and pacer.in_cooldown(cfg.name) is None


def test_ir_payload_matches_executed_espn_ir_moves():
    # shape of the IR moves ESPN executed in our leagues' mTransactions2 logs:
    # type ROSTER, executionType EXECUTE, item LINEUP 20 -> 21
    p = lineup_payload(3, 4, [Move(7, "Hurt", BENCH, IR)])
    assert p["type"] == "ROSTER" and p["executionType"] == "EXECUTE"
    assert p["items"] == [{"playerId": 7, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 21}]


def test_out_is_not_ir_eligible_but_injury_reserve_is():
    s = make_state([b.entry(b.player(1, "Q", "QB", 20), 0),
                    b.entry(b.player(2, "Out Guy", "RB", 0, injury="OUT"), 20),
                    b.entry(b.player(3, "Ir Guy", "RB", 0, injury="INJURY_RESERVE"), 20)])
    res = optimize(s)
    assert [(m.player_id, m.to_slot) for m in res.ir_moves] == [(3, IR)]


def test_traded_player_gets_new_team_bye_on_next_read():
    pros = {1: {"abbrev": "AAA", "bye": 5}, 3: {"abbrev": "CCC", "bye": 6}}
    before = parse_player(b.entry(b.player(9, "Moved", "WR", 10, pro=1), 20), 2026, 4, pros)
    after = parse_player(b.entry(b.player(9, "Moved", "WR", 10, pro=3), 20), 2026, 4, pros)
    assert (before.pro_team, before.bye_week) == ("AAA", 5) and (after.pro_team, after.bye_week) == ("CCC", 6)


def test_fill_in_week_does_not_inflate_rest_of_season():
    # starter doubtful this week; backup projects 12.7 this week but only 96 for the season
    s = make_state([b.entry(b.player(1, "Star Rb", "RB", 0, season=212, injury="DOUBTFUL", pro=3), 20),
                    b.entry(b.player(2, "Backup Rb", "RB", 12.7, season=96, pro=3), 2)])
    back = s.find_player("Backup Rb")
    inflated = back.per_game()
    assert engine.mark_fill_ins(s) == ["Backup Rb"]
    assert back.per_game() == 96 / 17 < inflated


def test_weekly_half_capped_at_1_5x_season_rate():
    from fantasy_ai.models import Player
    p = Player(1, "Spike", "WR", proj_week=20.0, proj_season=85.0)     # season rate 5.0, weekly 16.4
    assert abs(p.per_game() - (0.5 * 7.5 + 0.5 * 5.0)) < 1e-9


def _ecr_state():
    s = make_state([b.entry(b.player(1, "Mine Rb", "RB", 5, season=60), 20),
                    b.entry(b.player(2, "Starter", "RB", 15), 2), b.entry(b.player(3, "Starter2", "RB", 14), 2),
                    b.entry(b.player(4, "K", "K", 8), 17), b.entry(b.player(5, "D", "D/ST", 6), 16),
                    b.entry(b.player(6, "Q", "QB", 18), 0)],
                   fas=[b.fa_row(b.player(90, "Hot Add", "RB", 14, season=230))])
    for i in range(30):          # pool depth so ranks mean something
        s.free_agents.append(parse_player(b.fa_row(b.player(200 + i, f"Filler {i}", "RB", 1, season=10)), 2026, 4, {}))
    return s


def test_tripwire_on_waiver_add_we_rank_20_spots_above_experts():
    s = _ecr_state()
    s.find_player("Hot Add").ecr = {"ros": 52, "ros_text": "RB52"}
    sg = waivers.evaluate_add(s, s.find_player("Hot Add"), cfg={"min_gain_free_agent": 3.0})
    assert sg.threshold == 4.5 and sg.why[0].startswith("CAUTION: we rank Hot Add RB2, experts RB52") and sg.why[0].endswith("needs +4.5")
    s2 = _ecr_state()
    s2.find_player("Hot Add").ecr = {"ros": 3, "ros_text": "RB3"}
    assert waivers.evaluate_add(s2, s2.find_player("Hot Add"), cfg={"min_gain_free_agent": 3.0}).threshold == 3.0


def test_tripwire_marks_trade_targets():
    from test_trade_engine import league
    from fantasy_ai.prefs import Prefs
    s = league()
    for t in s.teams.values():
        for p in t.roster:
            p.ecr = {"ros": 60, "ros_text": f"{p.position}60"}
    ideas = teamneeds.trade_ideas(s, Prefs(), top=5)
    assert ideas and all(i.cautions and i.cautions[0].startswith("CAUTION") for i in ideas)


def test_ir_stash_valued_from_expert_rank_minus_games_out():
    s = make_state([b.entry(b.player(1, "Rb1", "RB", 20, season=320, pro=4), 2),
                    b.entry(b.player(2, "Rb2", "RB", 15, season=240, pro=4), 2),
                    b.entry(b.player(3, "Rb3", "RB", 10, season=160, pro=4), 20),
                    b.entry(b.player(4, "Stash", "RB", 0, season=0, injury="INJURY_RESERVE", pro=4), 20)])
    stash = s.find_player("Stash")
    assert stash.ros_value(4) == 0
    stash.ecr = {"ros": 2, "ros_text": "RB2"}
    assert engine.value_ir_stashes(s) == ["Stash"]
    rb2 = s.find_player("Rb2")
    weeks = 17 - 4 + 1 - 4                                         # bye week 7 is still ahead
    assert abs(stash.ros_value(4) - round(rb2.per_game() * (weeks - 1), 1)) < 0.6
    assert "IR stash valued from expert rank RB2" in stash.reasons[-1]
    no_ecr = make_state([b.entry(b.player(5, "Other", "RB", 0, season=0, injury="INJURY_RESERVE"), 20)])
    assert engine.value_ir_stashes(no_ecr) == []
