"""Waiver safety audit + invariants, and roster % signal. All offline."""
import json

from conftest import b, make_state
from fantasy_ai import ownership, waivers
from fantasy_ai.factors import score
from fantasy_ai.factors.context import WeekContext
from fantasy_ai.prefs import Prefs

FRIENDLY_SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 7, "21": 1, "23": 2}


def friendly():
    """Real-shaped 'friendly' roster from the live Hunter bug (projections rounded)."""
    wr = lambda pid, n, proj, slot, season=None, pro=1: b.entry(b.player(pid, n, "WR", proj, season=season, pro=pro), slot)
    e = [b.entry(b.player(1, "Bijan Robinson", "RB", 23.8), 2), b.entry(b.player(2, "Ashton Jeanty", "RB", 16.1), 2),
         wr(3, "Rashee Rice", 13.3, 4), wr(4, "Zay Flowers", 18.8, 23), b.entry(b.player(5, "Colston Loveland", "TE", 8.4), 6),
         wr(6, "Terry McLaurin", 13.1, 4), wr(7, "Jameson Williams", 10.2, 20), b.entry(b.player(8, "Dak Prescott", "QB", 20.7), 0),
         wr(9, "Courtland Sutton", 9.6, 20, season=135), wr(10, "Travis Hunter", 0.8, 20, season=10),
         wr(11, "Matthew Golden", 11.7, 23), b.entry(b.player(12, "Dallas Goedert", "TE", 8.0, injury="DOUBTFUL"), 20),
         b.entry(b.player(13, "Matthew Stafford", "QB", 19.4), 20), b.entry(b.player(14, "Jason Myers", "K", 10.5), 17),
         b.entry(b.player(15, "Jared Goff", "QB", 19.7), 20), b.entry(b.player(16, "Chris Rodriguez Jr.", "RB", 4.7), 20),
         b.entry(b.player(17, "Ravens D/ST", "D/ST", 8.7), 16)]
    return make_state(e, slots=FRIENDLY_SLOTS,
                      fas=[b.fa_row(b.player(90, "Denzel Boston", "WR", 9, season=144), "FREEAGENT")])


def row(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_friendly_hunter_case_passes_checks_and_drops_hunter():
    s = friendly()
    sugg, fails = waivers.rank_with_checks(s, cfg={"min_gain_free_agent": 0})
    assert fails == [] and sugg and sugg[0].drop.name == "Travis Hunter"
    rows = waivers.audit(s)
    assert row(rows, "Travis Hunter")["droppable"] and row(rows, "Travis Hunter")["reason"] == ""
    assert row(rows, "Bijan Robinson")["reason"] == "starter this week"


def test_invariant_catches_a_silently_skipped_drop(monkeypatch):
    """If a bug hid Hunter from the drop list, the check must fail and nothing is suggested."""
    s = friendly()
    real = waivers.droppable
    monkeypatch.setattr(waivers, "droppable",
                        lambda st, *a, **k: [p for p in real(st, *a, **k) if p.name != "Travis Hunter"])
    sugg, fails = waivers.rank_with_checks(s, cfg={"min_gain_free_agent": 0})
    assert sugg == [] and any("Travis Hunter is droppable but was never evaluated" in f for f in fails)


def test_invariant_blocks_dropping_a_better_player():
    s = friendly()
    sug = waivers.evaluate_add(s, s.find_player("Denzel Boston"))
    stafford = s.find_player("Matthew Stafford")
    sug.drop, sug.drop_value, sug.next3_drop, sug.starts_drop = stafford, 999, 99, 1
    fails = waivers.check_suggestion(s, sug, waivers.drop_status(s), waivers.merged(None))
    assert any("worth more than" in f for f in fails)


def test_audit_reasons_edge_cases():
    # 3 QBs in a 1-QB league; K on bye (only K); TE2 covering TE1's bye; IR-bound RB; never_drop; locked
    e = [b.entry(b.player(1, "Qb1", "QB", 22), 0), b.entry(b.player(2, "Qb2", "QB", 18), 20),
         b.entry(b.player(3, "Qb3", "QB", 15), 20),
         b.entry(b.player(4, "Only K", "K", 8, pro=2), 17),              # pro 2 = bye week 4
         b.entry(b.player(5, "Te1", "TE", 10, pro=1), 6),                 # pro 1 = bye week 5
         b.entry(b.player(6, "Te2", "TE", 5, pro=3), 20),
         b.entry(b.player(7, "Hurt Rb", "RB", 0, injury="INJURY_RESERVE"), 20),
         b.entry(b.player(8, "Keeper", "WR", 3), 20),
         b.entry(b.player(9, "Locked Wr", "WR", 4), 20, locked=True),
         b.entry(b.player(10, "Dst", "D/ST", 6), 16),
         b.entry(b.player(11, "Wr1", "WR", 14), 4), b.entry(b.player(12, "Wr2", "WR", 13), 4),
         b.entry(b.player(13, "Wr3", "WR", 12), 23), b.entry(b.player(14, "Rb1", "RB", 15), 2),
         b.entry(b.player(15, "Rb2", "RB", 14), 2)]
    rows = waivers.audit(make_state(e), Prefs(never_drop={"keeper"}))
    assert row(rows, "Qb1")["reason"] == "starter this week"
    assert row(rows, "Qb3")["droppable"]
    assert row(rows, "Only K")["reason"] == "roster minimum (last K)"
    assert row(rows, "Te2")["reason"] == "bye coverage (week 5)"
    assert row(rows, "Hurt Rb")["reason"] == "IR-bound (open IR slot)"
    assert row(rows, "Keeper")["reason"] == "never_drop (your rule)"
    assert row(rows, "Locked Wr")["reason"] == "locked (game started)"


def test_every_unprotected_bench_player_is_evaluated_for_every_add():
    s = friendly()
    status = waivers.drop_status(s)
    free = {pid for pid, why in status.items() if why is None}
    for fa in s.free_agents:
        w = waivers.evaluate_add(s, fa)
        assert free <= (w.considered | set(w.rejected))


# ---- roster %

def test_ownership_history_trend_and_notes(isolated_data_dir):
    s = friendly()
    boston = s.find_player("Denzel Boston")
    hunter = s.find_player("Travis Hunter")
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "ownership_history.json").write_text(json.dumps({"weeks": {
        "2026-01": {"90": 5.0, "10": 60.0}, "2026-03": {"90": 20.0, "10": 50.0}}}))
    boston.percent_owned, boston.percent_change = 42.0, None
    hunter.percent_owned, hunter.percent_change = 35.0, None
    before = boston.ros_value(4)
    hist = ownership.record(s)
    ownership.apply(s, hist)
    assert (boston.own_week, boston.own_3wk) == (22.0, 37.0)
    assert (hunter.own_week, hunter.own_3wk) == (-15.0, -25.0)
    assert before < boston.ros_value(4) <= before * 1.08 + 0.1
    assert ownership.own_note(boston) == "rostered 42% (+22% this week, +37% over 3 wks), hot: likely claimed soon if you wait"
    assert "falling" in ownership.own_note(hunter)
    assert "2026-04" in json.loads((isolated_data_dir / "ownership_history.json").read_text())["weeks"]


def test_history_keeps_six_weeks(isolated_data_dir):
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "ownership_history.json").write_text(json.dumps(
        {"weeks": {f"2026-{w:02d}": {} for w in range(1, 10)}}))
    s = friendly()
    hist = ownership.record(s)
    assert len(hist["weeks"]) == 6 and "2026-01" not in hist["weeks"]


def test_espn_percent_change_preferred_and_consensus_factor():
    s = friendly()
    p = s.find_player("Matthew Golden")
    p.percent_change, p.percent_started = -6.0, 95.0
    w, _ = ownership.trend(p, {"weeks": {}}, 2026, 4)
    assert w == -6.0
    score.apply(s, WeekContext(week=4))
    f = next(f for f in p.factors if f.name == "consensus")
    assert 1 < f.multiplier <= 1.03 and "95%" in f.reason
