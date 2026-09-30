"""Hype vs substance labels on fixture usage data (no network)."""
from conftest import b, make_state
from fantasy_ai import hype, waivers
from fantasy_ai.usage_data import UsageData, build, norm_name


def rows(team, pos, weeks):
    """weeks: list of (week, share, snap, targets, fpts, xfp)."""
    key = "target_share" if pos in ("WR", "TE") else "carry_share"
    out = []
    for w, share, snap, tg, fp, xf in weeks:
        r = {"week": w, "team": team, "pos": pos, "targets": tg, "target_share": 0.0, "carry_share": 0.0,
             "air_yards_share": 0.0, "wopr": 0.0, "carries": 0.0, "snap_pct": snap, "fpts": fp, "xfp": xf}
        r[key] = share
        out.append(r)
    return out


def state_with(*players, extra=()):
    entries = [b.entry(b.player(i + 1, n, pos, proj, pro=pro, injury=inj), 20)
               for i, (n, pos, proj, pro, inj) in enumerate(players)]
    s = make_state(entries, fas=[b.fa_row(b.player(100 + i, n, pos, 5, pro=pro, injury=inj))
                                 for i, (n, pos, pro, inj) in enumerate(extra)])
    return s


def label(s, name, usage):
    p = s.find_player(name)
    return hype.compute(p, usage, s)


def test_real_breakout_room_healthy():
    s = state_with(("Kalif Raymond", "WR", 10, 1, "ACTIVE"))
    u = UsageData(2026, {norm_name("Kalif Raymond"): rows("AAA", "WR", [
        (1, 0.08, 0.45, 2, 4, 5), (2, 0.22, 0.85, 8, 14, 13), (3, 0.26, 0.90, 10, 16, 15)])})
    h = label(s, "Kalif Raymond", u)
    assert h.label == "Real breakout" and h.substance >= 72
    assert any("targets 2 -> 9" in r for r in h.reasons) and any("healthy" in r for r in h.reasons)


def test_role_growing():
    s = state_with(("Grower", "WR", 10, 1, "ACTIVE"))
    u = UsageData(2026, {"grower": rows("AAA", "WR", [(1, 0.12, 0.60, 5, 8, 8), (2, 0.16, 0.66, 6, 9, 9), (3, 0.18, 0.74, 7, 10, 10)])})
    assert label(s, "Grower", u).label == "Role growing"


def test_fill_in_when_teammate_returns_next_week():
    s = state_with(("Fill Guy", "WR", 10, 1, "ACTIVE"), ("Star Wr", "WR", 16, 1, "QUESTIONABLE"))
    u = UsageData(2026, {
        "fill guy": rows("AAA", "WR", [(1, 0.10, 0.50, 4, 6, 6), (2, 0.25, 0.90, 9, 15, 14), (3, 0.24, 0.88, 9, 14, 14)]),
        "star wr": rows("AAA", "WR", [(1, 0.27, 0.92, 10, 18, 16)]),        # missed weeks 2 and 3
    }, injuries={("AAA", 2): {"star wr": "OUT"}, ("AAA", 3): {"star wr": "OUT"}})
    h = label(s, "Fill Guy", u)
    assert h.label == "Fill-in (Star Wr returns)" and h.future_mult == 0.60
    assert any("fill-in role" in r for r in h.reasons)


def test_teammate_on_ir_keeps_the_bump():
    s = state_with(("Fill Guy", "WR", 10, 1, "ACTIVE"), ("Star Wr", "WR", 16, 1, "INJURY_RESERVE"))
    u = UsageData(2026, {
        "fill guy": rows("AAA", "WR", [(1, 0.10, 0.50, 4, 6, 6), (2, 0.25, 0.90, 9, 15, 14), (3, 0.24, 0.88, 9, 14, 14)]),
        "star wr": rows("AAA", "WR", [(1, 0.27, 0.92, 10, 18, 16)]),
    })
    h = label(s, "Fill Guy", u)
    assert h.label in ("Real breakout", "Role growing") and any("should stick" in r for r in h.reasons)


def test_hype_only_spike_with_flat_usage():
    s = state_with(("Buzzy", "WR", 9, 1, "ACTIVE"))
    p = s.find_player("Buzzy")
    p.own_week, p.buzz = 25.0, 0.8
    u = UsageData(2026, {"buzzy": rows("AAA", "WR", [(1, 0.10, 0.60, 4, 6, 6), (2, 0.10, 0.61, 4, 6, 6), (3, 0.11, 0.60, 4, 7, 6)])})
    h = label(s, "Buzzy", u)
    assert h.label == "Hype only" and h.hype >= 60 and "usage is flat" in h.reasons[0]


def test_sell_high_td_driven():
    s = state_with(("Lucky", "WR", 10, 1, "ACTIVE"))
    u = UsageData(2026, {"lucky": rows("AAA", "WR", [(1, 0.14, 0.7, 5, 21, 8), (2, 0.14, 0.7, 5, 19, 8), (3, 0.15, 0.7, 5, 22, 9)])})
    assert label(s, "Lucky", u).label == "Sell high"


def test_buy_low_volume_without_points():
    s = state_with(("Unlucky", "RB", 12, 1, "ACTIVE"))
    u = UsageData(2026, {"unlucky": rows("AAA", "RB", [(1, 0.55, 0.7, 3, 8, 14), (2, 0.56, 0.7, 3, 7, 15), (3, 0.55, 0.7, 3, 9, 14)])})
    assert label(s, "Unlucky", u).label == "Buy low"


def test_fading_and_steady():
    s = state_with(("Fader", "WR", 10, 1, "ACTIVE"), ("Rock", "WR", 12, 1, "ACTIVE"))
    u = UsageData(2026, {
        "fader": rows("AAA", "WR", [(1, 0.24, 0.9, 9, 14, 13), (2, 0.12, 0.6, 4, 6, 6), (3, 0.08, 0.5, 3, 4, 5)]),
        "rock": rows("AAA", "WR", [(1, 0.20, 0.85, 8, 12, 12), (2, 0.20, 0.85, 8, 12, 12), (3, 0.21, 0.86, 8, 12, 12)])})
    assert label(s, "Fader", u).label == "Fading"
    assert label(s, "Rock", u).label == "Steady"


def test_labels_feed_waiver_value():
    s = state_with(("Fill Guy", "WR", 10, 3, "ACTIVE"), ("Star Wr", "WR", 16, 3, "ACTIVE"))
    cfg = waivers.merged(None)
    before = waivers.week_worth(s.find_player("Fill Guy"), s, 1, cfg)
    u = UsageData(2026, {
        "fill guy": rows("CCC", "WR", [(1, 0.10, 0.50, 4, 6, 6), (2, 0.25, 0.90, 9, 15, 14), (3, 0.24, 0.88, 9, 14, 14)]),
        "star wr": rows("CCC", "WR", [(1, 0.27, 0.92, 10, 18, 16)])})
    ros_before = s.find_player("Fill Guy").ros_value(4)
    hype.apply(s, u)
    p = s.find_player("Fill Guy")
    assert waivers.week_worth(p, s, 1, cfg) < before and p.ros_value(4) < ros_before


def test_no_data_is_steady_not_crash():
    s = state_with(("Rookie", "WR", 5, 1, "ACTIVE"))
    h = label(s, "Rookie", None)
    assert h.label == "Steady" and "no usage data yet" in h.reasons


def test_build_from_raw_nflverse_rows():
    stats = [{"season_type": "REG", "player_display_name": "Deebo Samuel Sr.", "team": "WAS", "week": 1,
              "position": "WR", "targets": 8, "target_share": 0.25, "air_yards_share": 0.2, "wopr": 0.515,
              "carries": 2, "fantasy_points_ppr": 15.0},
             {"season_type": "REG", "player_display_name": "Some Rb", "team": "WAS", "week": 1,
              "position": "RB", "targets": 1, "carries": 18, "fantasy_points_ppr": 10.0}]
    snaps = [{"player": "Deebo Samuel", "team": "WAS", "week": 1, "offense_pct": 0.8}]
    opp = [{"full_name": "Deebo Samuel", "posteam": "WAS", "week": 1, "total_fantasy_points_exp": 13.5}]
    u = build(2026, stats, snaps, opp, [], [])
    r = u.weekly["deebo samuel"][0]
    assert r["snap_pct"] == 0.8 and r["xfp"] == 13.5 and round(r["carry_share"], 2) == 0.1
