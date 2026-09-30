"""Forward value tracker: snapshots of our values vs FantasyCalc, scored on real points (no network)."""
import datetime as dt
import json

from conftest import b, make_state
from fantasy_ai import value_track as vt
from fantasy_ai.hype import HypeResult


def _state():
    entries = [b.entry(b.player(1, "Alpha Back", "RB", 15, season=240), 20),
               b.entry(b.player(2, "Beta Back", "RB", 12, season=200), 20),
               b.entry(b.player(3, "Gamma Wide", "WR", 14, season=220), 20),
               b.entry(b.player(4, "Kicker Guy", "K", 8, season=130), 20)]
    s = make_state(entries, fas=[b.fa_row(b.player(10, "Delta Back", "RB", 6, season=90))], week=4)
    for p, fc, label, mult in ((1, 5000, "Fading", 0.9), (2, 6000, "Steady", 1.0),
                               (3, 7000, "Steady", 1.0), (10, None, "Steady", 1.0)):
        pl = s.find_player(str(p))
        pl.market_value = fc
        pl.hype = HypeResult(50, 20, label, [], mult, mult)
        pl.ros_multiplier = mult
    return s


def test_snapshot_rows_and_hype_backed_out():
    snap = vt.snapshot(_state(), "friendly", now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    assert snap["week"] == 4 and snap["season"] == 2026 and snap["league"] == "friendly"
    by = {r["name"]: r for r in snap["players"]}
    assert "Kicker Guy" not in by                      # K and D/ST are not tracked
    a = by["Alpha Back"]
    assert a["hype"] == "Fading" and a["hype_mult"] == 0.9
    assert abs(a["ours_nohype"] - a["ours"] / 0.9) < 0.11
    assert by["Delta Back"]["fc"] is None and by["Beta Back"]["fc"] == 6000


def test_save_keeps_first_snapshot_unless_forced(tmp_path):
    snap = vt.snapshot(_state(), "friendly")
    path, written = vt.save(snap, tmp_path)
    assert written and path.name == "friendly_2026_wk04.json"
    snap2 = dict(snap, taken_at="later")
    _, written2 = vt.save(snap2, tmp_path)
    assert not written2 and json.loads(path.read_text())["taken_at"] == snap["taken_at"]
    _, written3 = vt.save(snap2, tmp_path, force=True)
    assert written3 and json.loads(path.read_text())["taken_at"] == "later"
    assert [s["week"] for s in vt.load_all("friendly", 2026, tmp_path)] == [4]
    assert vt.load_all("other", 2026, tmp_path) == []


def _snap(players, week=4):
    return {"league": "x", "season": 2026, "week": week, "players": players}


def _row(i, name, pos, ours, fc, nohype=None, espn=None):
    return {"id": i, "name": name, "pos": pos, "ours": ours, "ours_nohype": nohype if nohype is not None else ours,
            "espn_pg": espn if espn is not None else ours / 10, "fc": fc}


def test_disagreement_counts_who_was_right():
    # ours says A > B > C; FC says C > B > A; actual A > C > B
    rows = [dict(_row(1, "A", "RB", 30, 1), actual=20), dict(_row(2, "B", "RB", 20, 2), actual=5),
            dict(_row(3, "C", "RB", 10, 3), actual=10)]
    # every pair disagrees: (A,B) ours right, (A,C) ours right, (B,C) FC right
    assert vt.disagreement(rows, "ours", "fc") == (2, 1)
    # identical rankings never disagree
    assert vt.disagreement(rows, "ours", "ours_nohype") == (0, 0)


def test_disagreement_is_within_position_only():
    rows = [dict(_row(1, "Q", "QB", 30, 1), actual=30), dict(_row(2, "R", "RB", 10, 9), actual=5)]
    assert vt.disagreement(rows, "ours", "fc") == (0, 0)


def test_score_snapshot_uses_weeks_from_snapshot_on_and_counts_missed_games_as_zero():
    players = [_row(i, f"Back {i}", "RB", 100 - i, 1000 - i) for i in range(1, 6)]
    players.append(_row(9, "No Market", "RB", 500, None))           # no FC value: excluded
    actuals = {("back 1", "RB"): {3: 99.0, 4: 20.0, 5: 20.0},        # week 3 is before the snapshot
               ("back 2", "RB"): {4: 15.0, 5: 15.0},
               ("back 3", "RB"): {4: 10.0},                          # missed week 5 -> 0
               ("back 4", "RB"): {4: 4.0, 5: 4.0},
               ("back 5", "RB"): {4: 1.0, 5: 1.0},
               ("no market", "RB"): {4: 50.0}}
    sc = vt.score_snapshot(_snap(players), actuals)
    assert sc.week == 4 and sc.through == 5 and sc.n == 5
    assert sc.spearman["ours"]["RB"] == 1.0 and sc.spearman["fc"]["RB"] == 1.0
    assert sc.spearman["ours"]["QB"] is None
    one = vt.score_snapshot(_snap(players), actuals, horizon=1)
    assert one.through == 4


def test_score_snapshot_none_before_games():
    assert vt.score_snapshot(_snap([_row(1, "A", "RB", 1, 1)], week=6), {("a", "RB"): {4: 1.0}}) is None


def test_format_scores_runs_and_pools():
    players = [_row(i, f"W {i}", "WR", 50 - i, 100 + i) for i in range(1, 8)]
    actuals = {(f"w {i}", "WR"): {4: float(i % 3), 5: float(i)} for i in range(1, 8)}
    scores = [vt.score_snapshot(_snap(players, 4), actuals), vt.score_snapshot(_snap(players, 5), actuals)]
    text = "\n".join(vt.format_scores(scores))
    assert "Snapshot week 4" in text and "Snapshot week 5" in text and "pooled" in text
    assert "FantasyCalc" in text


def test_actuals_from_usage():
    from fantasy_ai.usage_data import UsageData
    u = UsageData(2026, {"a guy": [{"week": 1, "pos": "WR", "fpts": 12.5}, {"week": 2, "pos": "WR", "fpts": 3.0}]})
    acts = vt.actuals_from_usage(u)
    assert acts == {("a guy", "WR"): {1: 12.5, 2: 3.0}} and vt.last_week(acts) == 2


def test_cli_parser_has_commands():
    from fantasy_ai.cli import build_parser
    a = build_parser().parse_args(["track-values", "friendly", "--force"])
    assert a.cmd == "track-values" and a.force
    a = build_parser().parse_args(["score-values", "friendly", "--horizon", "2"])
    assert a.cmd == "score-values" and a.horizon == 2
