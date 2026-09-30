import pytest

from conftest import b, make_state
from fantasy_ai.trades import evaluate_trade
from fantasy_ai.waivers import rank_free_agents


def full_demo(demo_state):
    """Demo roster has one open spot; fill it so drops are required."""
    from fantasy_ai.espn_read import parse_player
    filler = parse_player(b.entry(b.player(999, "Filler Guy", "WR", 1.0, season=10, pro=5), 20), 2026, 4, {})
    filler.on_team_id = 1
    demo_state.my_team.roster.append(filler)
    return demo_state


def test_waiver_ranking_rules_and_why_lines(demo_state):
    sugg = rank_free_agents(demo_state, limit=5)
    assert sugg and [s.gain for s in sugg] == sorted([s.gain for s in sugg], reverse=True)
    assert sum(1 for s in sugg if s.drop is None) <= 1               # one open roster spot
    for s in sugg:
        assert s.gain >= s.threshold and s.why
        if s.kind == "WAIVER":
            assert s.threshold == 8.0 and "Costs waiver priority" in s.reason


def test_full_roster_drops_least_valuable_bench_player_any_position(demo_state):
    s = full_demo(demo_state)
    sugg = rank_free_agents(s, limit=5)
    assert sugg and all(x.drop is not None for x in sugg)
    top = sugg[0]
    assert top.drop.name in ("Filler Guy", "Flex Fred")       # surplus WRs, beyond useful depth
    assert top.add.position != "WR"                           # cross-position drop
    drops = [x.drop.name for x in sugg]
    assert len(drops) == len(set(drops))


def test_waivers_never_drop_locked_ir_or_starters():
    e = [b.entry(b.player(1, "Q", "QB", 20), 0), b.entry(b.player(2, "R1", "RB", 15), 2),
         b.entry(b.player(3, "R2", "RB", 14), 2), b.entry(b.player(4, "W1", "WR", 13), 4),
         b.entry(b.player(5, "W2", "WR", 12), 4), b.entry(b.player(6, "T", "TE", 9), 6),
         b.entry(b.player(7, "F", "WR", 11), 23), b.entry(b.player(8, "D", "D/ST", 6), 16),
         b.entry(b.player(9, "K", "K", 7), 17),
         b.entry(b.player(10, "Lock", "RB", 1, season=10), 20, locked=True),
         b.entry(b.player(11, "Irish", "RB", 0, season=5, injury="INJURY_RESERVE"), 21)]
    e += [b.entry(b.player(20 + i, f"Bench{i}", "WR", 2 + i), 20) for i in range(6)]
    fa = [b.fa_row(b.player(90, "FA Rb", "RB", 16, season=260))]
    sugg = rank_free_agents(make_state(e, fas=fa))
    assert sugg and sugg[0].drop.name == "Bench0"
    assert all(x.drop.name not in ("Lock", "Irish", "R1", "Q") for x in sugg)


def test_claim_needs_bigger_gain_than_free_agent(demo_state):
    from fantasy_ai.waivers import evaluate_add
    s = full_demo(demo_state)
    wonder = s.find_player("Waiver Wonder")
    w = evaluate_add(s, wonder)
    assert w.kind == "WAIVER" and w.threshold == 8.0
    wonder.status = "FREEAGENT"
    f = evaluate_add(s, wonder)
    assert f.threshold == 3.0 and f.gain == w.gain


def test_keeps_one_kicker_and_bye_coverage():
    from fantasy_ai.waivers import evaluate_add
    # only one K on the roster: a WR add must not drop him
    e = [b.entry(b.player(1, "Q", "QB", 20), 0), b.entry(b.player(9, "Only K", "K", 1, season=16), 20),
         b.entry(b.player(8, "D", "D/ST", 6), 16)]
    e += [b.entry(b.player(20 + i, f"W{i}", "WR", 10 + i), 4 if i < 2 else 20) for i in range(13)]
    s = make_state(e, fas=[b.fa_row(b.player(90, "FA Wr", "WR", 30, season=500))])
    w = evaluate_add(s, s.find_player("FA Wr"))
    assert w.drop.name != "Only K"


def test_handcuff_bump_when_starter_out():
    from fantasy_ai.waivers import week_worth, merged
    e = [b.entry(b.player(1, "Starter Rb", "RB", 18, pro=4, injury="OUT"), 2)]
    s = make_state(e, fas=[b.fa_row(b.player(90, "Backup Rb", "RB", 8, pro=4))])
    backup = s.find_player("Backup Rb")
    assert week_worth(backup, s, 0, merged(None)) == round(8 * 1.15, 2)


def test_streamer_flags_better_dst():
    from fantasy_ai.waivers import streamers
    e = [b.entry(b.player(1, "Our D", "D/ST", 4), 16), b.entry(b.player(2, "Q", "QB", 20), 0)]
    s = make_state(e, fas=[b.fa_row(b.player(90, "Stream D", "D/ST", 9))])
    out = streamers(s)
    assert out and out[0].add.name == "Stream D" and out[0].drop.name == "Our D" and "Stream" in out[0].reason


def test_trade_eval_both_sides(demo_state):
    s = demo_state
    give = [s.find_player("Backup Bo")]
    get = [s.find_player("Rival RB1")]
    ev = evaluate_trade(s, give, get)
    assert ev.me.lineup_delta > 0          # RB1 (17/g) starts over Ready Rex
    assert ev.partner.team_name == "Rival Squad"
    assert ev.partner.lineup_delta < 0     # backup QB doesn't start for them
    assert ev.verdict in ("ACCEPTABLE", "GOOD FOR US, HARD SELL")


def test_trade_rejects_players_not_mine(demo_state):
    s = demo_state
    with pytest.raises(ValueError):
        evaluate_trade(s, [s.find_player("Rival QB")], [s.find_player("Rival RB1")])


def test_fourth_qb_has_no_depth_value():
    from fantasy_ai.waivers import evaluate_add
    e = [b.entry(b.player(1, "Qb1", "QB", 22), 0), b.entry(b.player(2, "Qb2", "QB", 19), 20),
         b.entry(b.player(3, "Qb3", "QB", 15), 20), b.entry(b.player(4, "Rb Backup", "RB", 8), 20),
         b.entry(b.player(8, "D", "D/ST", 6), 16), b.entry(b.player(9, "K", "K", 7), 17)]
    e += [b.entry(b.player(20 + i, f"W{i}", "WR", 12 + i), 4 if i < 2 else 20) for i in range(10)]
    s = make_state(e, fas=[b.fa_row(b.player(90, "Qb Four", "QB", 14, season=300))])
    w = evaluate_add(s, s.find_player("Qb Four"))
    assert w is None or w.gain < w.threshold
