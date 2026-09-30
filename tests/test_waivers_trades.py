import pytest

from conftest import b, make_state
from fantasy_ai.trades import evaluate_trade
from fantasy_ai.waivers import rank_free_agents


def test_waiver_ranking_order_and_reason(demo_state):
    sugg = rank_free_agents(demo_state, limit=5)
    names = [(s.add.name, s.drop.name) for s in sugg]
    assert names[0] == ("Waiver Wonder", "Tiny Tim")
    assert sugg[0].kind == "WAIVER" and "needs a claim" in sugg[0].reason
    assert all(s.gain >= 3.0 for s in sugg)
    assert [s.gain for s in sugg] == sorted([s.gain for s in sugg], reverse=True)
    assert "Street Runner" not in [s.add.name for s in sugg]   # worse than every RB we have


def test_waivers_never_drop_locked_or_ir():
    e = [b.entry(b.player(1, "Lock", "RB", 1, season=10), 2, locked=True),
         b.entry(b.player(2, "Irish", "RB", 0, season=5, injury="INJURY_RESERVE"), 21),
         b.entry(b.player(3, "Ok", "RB", 8), 2)]
    fa = [b.fa_row(b.player(9, "FA", "RB", 10))]
    sugg = rank_free_agents(make_state(e, fas=fa))
    assert [s.drop.name for s in sugg] == ["Ok"]


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
