"""Fitted usage trend, expert ranks (ECR), position-need report, and how trades/waivers use them."""
import json

from conftest import b, make_state
from fantasy_ai import experts, posneed, trend, waivers
from fantasy_ai.trades import evaluate_trade
from fantasy_ai.usage_data import UsageData


def rows(pos, weeks):
    """weeks: (week, primary share, snap, rz, fpts)."""
    key = "carry_share" if pos == "RB" else "target_share"
    out = []
    for w, share, snap, rz, fp in weeks:
        r = {"week": w, "team": "AAA", "pos": pos, "targets": 5.0, "target_share": 0.0, "carry_share": 0.0,
             "air_yards_share": 0.0, "wopr": 0.0, "carries": 0.0, "snap_pct": snap, "rz_share": rz,
             "fpts": fp, "xfp": fp}
        r[key] = share
        out.append(r)
    return out


def test_fit_file_has_positive_holdout_skill():
    fit = trend.FIT["groups"]
    for g in ("RB", "WRTE"):
        for y in ("y3", "yros"):
            assert len(fit[g][y]["coef"]) == 4
            assert all(v > 0 for v in fit[g][y]["holdout_skill"].values())     # beats "no trend" every season


def test_wr_target_share_jump_gives_fitted_uptrend():
    s = make_state([b.entry(b.player(1, "Rising Wr", "WR", 12), 4)])
    u = UsageData(2026, {"rising wr": rows("WR", [(1, 0.15, 0.8, 0.1, 10), (2, 0.15, 0.8, 0.1, 10),
                                                  (3, 0.25, 0.8, 0.1, 14), (4, 0.25, 0.8, 0.1, 15)])})
    t = trend.compute(s.find_player("Rising Wr"), u, 5)
    c3 = trend.FIT["groups"]["WRTE"]["y3"]["coef"][1]
    assert abs(t.next3 - (1 + c3 * 0.10)) < 0.002 and t.label == "uptrend"
    assert "target share 15%->25%" in t.text


def test_trend_scaled_with_one_earlier_game_clipped_and_skipped_for_qb():
    s = make_state([b.entry(b.player(1, "Wr", "WR", 12), 4), b.entry(b.player(2, "Qb", "QB", 20), 0),
                    b.entry(b.player(3, "Rb", "RB", 12), 2)])
    u = UsageData(2026, {
        "wr": rows("WR", [(1, 0.15, 0.8, 0.1, 10), (2, 0.25, 0.8, 0.1, 14), (3, 0.25, 0.8, 0.1, 15)]),
        "rb": rows("RB", [(1, 0.2, 0.2, 0.0, 5), (2, 0.2, 0.2, 0.0, 5), (3, 0.9, 1.0, 0.9, 25), (4, 0.9, 1.0, 0.9, 25)])})
    full = 1 + trend.FIT["groups"]["WRTE"]["y3"]["coef"][1] * 0.10
    t = trend.compute(s.find_player("Wr"), u, 4)
    assert abs((t.next3 - 1) - (full - 1) / 2) < 0.002                      # 1 earlier game: half weight
    assert trend.compute(s.find_player("Rb"), u, 5).next3 == 1 + trend.CLIP   # huge jump: clipped
    assert trend.compute(s.find_player("Qb"), u, 5) is None


def test_apply_moves_ros_and_future_weeks_not_this_week():
    s = make_state([b.entry(b.player(1, "Rising Wr", "WR", 12, pro=4), 4)])          # bye week 7
    p = s.find_player("Rising Wr")
    cfg = waivers.merged(None)
    ros0, wk0, wk1 = p.ros_value(4), waivers.week_worth(p, s, 0, cfg), waivers.week_worth(p, s, 1, cfg)
    u = UsageData(2026, {"rising wr": rows("WR", [(1, 0.15, 0.8, 0.1, 10), (2, 0.15, 0.8, 0.1, 10),
                                                  (3, 0.25, 0.8, 0.1, 14)])})
    trend.apply(s, u)
    assert p.ros_value(4) > ros0 and waivers.week_worth(p, s, 1, cfg) > wk1
    assert waivers.week_worth(p, s, 0, cfg) == wk0                         # weekly lineup untouched


def _trended(p, mult):
    p.trend = trend.Trend(0.1, 0.0, 0.0, mult, 1.0, 2, "target share", {"primary": 0.25, "snap": 0.8, "rz": 0.1},
                          {"primary": 0.15, "snap": 0.8, "rz": 0.1})


def test_trade_verdict_mentions_trends():
    s = make_state([b.entry(b.player(1, "Up One", "WR", 12), 4), b.entry(b.player(2, "Up Two", "WR", 11), 4)],
                   extra_teams=[(2, "B", [b.entry(b.player(10, "Down Guy", "RB", 15, pro=3), 2, team_id=2)])])
    _trended(s.find_player("Up One"), 1.08)
    _trended(s.find_player("Up Two"), 1.06)
    _trended(s.find_player("Down Guy"), 0.92)
    ev = evaluate_trade(s, [s.find_player("Up One"), s.find_player("Up Two")], [s.find_player("Down Guy")])
    assert "sells two players on an uptrend" in ev.reason and "buys a player on a downtrend" in ev.reason


def test_waivers_never_drop_a_hard_uptrend_without_a_clear_win():
    from test_waiver_safety import friendly
    s = friendly()
    sug = waivers.evaluate_add(s, s.find_player("Denzel Boston"))
    hunter = s.find_player("Travis Hunter")
    _trended(hunter, 1.10)
    sug.drop, sug.gain, sug.threshold = hunter, sug.threshold + 1, sug.threshold
    fails = waivers.check_suggestion(s, sug, waivers.drop_status(s), waivers.merged(None))
    assert any("trending up hard" in f for f in fails)


ECR_HTML = 'x\nvar ecrData = ' + json.dumps({"players": [
    {"player_name": "Rising Wr", "player_team_id": "AAA", "pos_rank": "WR30"},
    {"player_name": "Some Te", "player_team_id": "AAA", "pos_rank": "TE4"}]}) + ';\nvar y = 1;\n'


def test_experts_parse_attach_and_flag_disagreement():
    rows_ = experts.parse_page(ECR_HTML)
    table = experts.build({"ros": rows_, "WR": [{"player_name": "Rising Wr", "pos_rank": "WR12"}]})
    assert table[("rising wr", "WR")]["ros_text"] == "WR30" and table[("rising wr", "WR")]["week"] == 12
    s = make_state([b.entry(b.player(1, "Rising Wr", "WR", 22), 4), b.entry(b.player(2, "Other Wr", "WR", 5), 4)])
    assert experts.attach(s, table) == 1
    text = experts.note(s.find_player("Rising Wr"), s)
    assert text.startswith("ECR WR30 ROS, WR12 this week; ours WR1") and "BIG DISAGREEMENT: we are higher" in text
    assert experts.parse_page("<html>nothing</html>") == []


def test_position_need_verdicts():
    def team(tid, te_proj, inj="ACTIVE"):
        return [b.entry(b.player(tid * 10 + 1, f"T{tid} Qb", "QB", 18, pro=3), 0, team_id=tid),
                b.entry(b.player(tid * 10 + 2, f"T{tid} Te", "TE", te_proj, pro=3, injury=inj), 6, team_id=tid)]
    s = make_state(team(1, 4), extra_teams=[(t, f"Team {t}", team(t, 4 + t)) for t in range(2, 9)])
    mine = posneed.view(s, 1, "TE")[0]
    assert mine.verdict == "real problem" and mine.rank == 8 and mine.of == 8
    top = posneed.view(s, 8, "TE")[0]
    assert top.verdict == "strength" and top.rank == 1
    mid = posneed.view(s, 4, "TE")[0]
    assert mid.verdict == "fine"
    assert posneed.summary(s, 1, "TE") == "TE real problem (T1 Te TE 8/8)"
    lines = posneed.lines(s, 1, "TE")
    assert lines[0].startswith("TE (ours): T1 Te, REAL PROBLEM") and "next 3:" in lines[1]
