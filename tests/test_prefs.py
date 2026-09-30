"""Owner preferences (data/preferences.json), snapshot for chat, and trade_idea requests."""
import json

from conftest import b, make_state
from fantasy_ai import cli
from fantasy_ai.lineup import optimize
from fantasy_ai.prefs import Prefs, load_prefs, take_requests, trade_ideas, write_snapshot
from fantasy_ai.slots import BENCH
from fantasy_ai.trades import evaluate_trade
from fantasy_ai.waivers import rank_free_agents

import pytest


def write_prefs(d, data):
    d.mkdir(parents=True, exist_ok=True)
    (d / "preferences.json").write_text(json.dumps(data))


def two_wrs():
    return make_state([b.entry(b.player(1, "Big Wr", "WR", 15), 4), b.entry(b.player(2, "Mid Wr", "WR", 12), 4),
                       b.entry(b.player(3, "Adonai Mitchell", "WR", 9), 20),
                       b.entry(b.player(4, "Hurt Wr", "WR", 20, injury="OUT"), 20)],
                      slots={"4": 2, "20": 5, "21": 1})


def test_this_week_only_valid_in_override_week(isolated_data_dir):
    write_prefs(isolated_data_dir, {"Demo League": {"start_this_week": ["Adonai Mitchell"], "override_week": 3}})
    assert not load_prefs(4, "Demo League").starts("Adonai Mitchell")          # stale week ignored
    write_prefs(isolated_data_dir, {"Demo League": {"start_this_week": ["Adonai Mitchell"], "override_week": 4}})
    assert load_prefs(4, "demo league").starts("adonai mitchell")


def test_forced_start_beats_higher_score_with_reason(isolated_data_dir):
    write_prefs(isolated_data_dir, {"Demo League": {"start_this_week": ["Adonai Mitchell"], "override_week": 4}})
    res = optimize(two_wrs(), prefs=load_prefs(4, "Demo League"))
    assert res.target[3] == 4 and res.target[2] == BENCH
    m = next(m for m in res.moves if m.player_id == 3)
    assert "you asked" in m.reason


def test_forced_start_out_is_skipped_with_warning():
    p = Prefs(force_start={"hurt wr"})
    res = optimize(two_wrs(), prefs=p)
    assert res.target[4] == BENCH and any("can't play" in w for w in res.pref_skipped)


def test_always_start_and_bench_this_week_override(isolated_data_dir):
    write_prefs(isolated_data_dir, {"Demo League": {"always_start": ["Adonai Mitchell"],
                                                    "bench_this_week": ["Adonai Mitchell"], "override_week": 4}})
    res = optimize(two_wrs(), prefs=load_prefs(4, "Demo League"))
    assert res.target[3] == BENCH                  # this week's bench wins over always_start


def test_never_start_and_bench_reason():
    res = optimize(two_wrs(), prefs=Prefs(force_bench={"big wr"}))
    assert res.target[1] == BENCH
    assert next(m for m in res.moves if m.player_id == 1).reason == "benched Big Wr: you asked"


def test_never_drop_respected_by_waivers_trades_and_ideas(demo_state):
    keep = Prefs(never_drop={"tiny tim", "backup bo"})
    assert "Tiny Tim" not in [s.drop.name for s in rank_free_agents(demo_state, prefs=keep)]
    with pytest.raises(ValueError, match="never"):
        evaluate_trade(demo_state, [demo_state.find_player("Backup Bo")], [demo_state.find_player("Rival RB1")], prefs=keep)
    assert trade_ideas(demo_state, demo_state.find_player("Backup Bo"), keep) == []
    for give, get, ev in trade_ideas(demo_state, demo_state.find_player("Rival RB1"), keep):
        assert all(not keep.keeps(p.name) for p in give)


def test_snapshot_written_by_lineup_command(isolated_data_dir):
    assert cli.main(["--config", "config/leagues.example.yaml", "lineup", "demo"]) == 0
    snap = json.loads((isolated_data_dir / "snapshot.json").read_text())
    entry = snap["leagues"]["demo"]
    assert entry["week"] == 4 and entry["last_moves"]
    row = next(r for r in entry["roster"] if r["name"] == "Hurt Hal")
    assert set(row) >= {"name", "pos", "slot", "proj", "adj", "status", "reasons"} and row["adj"] == 0


def test_trade_idea_request_is_queued_and_cleared(isolated_data_dir, tmp_path, monkeypatch):
    from fantasy_ai import prefs as prefs_mod

    def one_idea(state, player, prefs, limit=2):
        give, get = [state.find_player("Backup Bo")], [player]
        return [(give, get, evaluate_trade(state, give, get))]
    monkeypatch.setattr(prefs_mod, "trade_ideas", one_idea)
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "requests.json").write_text(json.dumps([
        {"type": "trade_idea", "player": "Rival RB1", "league": "demo"},
        {"type": "trade_idea", "player": "X", "league": "some other league"}]))
    store = tmp_path / "p.json"
    rc = cli.main(["--quiet", "--config", "config/leagues.example.yaml", "--store", str(store),
                   "autopilot", "--demo", "--no-news"])
    assert rc == 0
    acts = json.loads(store.read_text())["actions"]
    trades = [a for a in acts if a["kind"] == "TRADE_PROPOSAL"]
    assert trades and all(i["type"] == "TRADE" for a in trades for i in a["payload"]["items"])
    left = json.loads((isolated_data_dir / "requests.json").read_text())
    assert left == [{"type": "trade_idea", "player": "X", "league": "some other league"}]


def test_take_requests_only_matching_league(isolated_data_dir):
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "requests.json").write_text(json.dumps([{"type": "trade_idea", "player": "A", "league": "L1"}]))
    assert take_requests("trade_idea", "L2") == []
    assert len(take_requests("trade_idea", "l1")) == 1 and take_requests("trade_idea", "L1") == []


def test_trade_ideas_finds_fair_upgrade():
    # we have two good QBs and a weak RB2; they have two good RBs and a weak QB
    me = [b.entry(b.player(1, "Qb A", "QB", 22), 0), b.entry(b.player(2, "Qb B", "QB", 20), 20),
          b.entry(b.player(3, "Rb A", "RB", 15), 2), b.entry(b.player(4, "Rb Weak", "RB", 4), 2)]
    them = [b.entry(b.player(11, "Their Qb", "QB", 8), 0, team_id=2), b.entry(b.player(12, "Their Rb1", "RB", 16), 2, team_id=2),
            b.entry(b.player(13, "Their Rb2", "RB", 15), 2, team_id=2), b.entry(b.player(14, "Their Rb3", "RB", 14), 20, team_id=2)]
    s = make_state(me, slots={"0": 1, "2": 2, "20": 5, "21": 1}, extra_teams=[(2, "Them", them)])
    ideas = trade_ideas(s, s.find_player("Qb B"), Prefs())
    assert ideas and ideas[0][0][0].name == "Qb B"
    assert ideas[0][2].me.lineup_delta > 0 and ideas[0][2].partner.lineup_delta >= -3
