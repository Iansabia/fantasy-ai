"""Team-need trade engine, scouting, FantasyCalc benchmark. Offline fixtures."""
from conftest import b, make_state
from fantasy_ai import market, teamneeds
from fantasy_ai.prefs import Prefs

SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 6, "21": 1}


def e(pid, name, pos, proj, slot, team=1, pro=None):
    return b.entry(b.player(pid, name, pos, proj, pro=pro or (pid % 6) + 1), slot, team_id=team)


def league():
    """We have 3 good QBs (Dak/Goff/Stafford style) and a weak RB2; Team B has a weak QB and
    3 good RBs; Team C and D are balanced."""
    us = [e(1, "Dak Qb", "QB", 21, 0), e(2, "Goff Qb", "QB", 19.5, 20), e(3, "Stafford Qb", "QB", 19, 20),
          e(4, "Rb One", "RB", 16, 2), e(5, "Rb Weak", "RB", 5, 2), e(6, "Wr One", "WR", 14, 4),
          e(7, "Wr Two", "WR", 13, 4), e(8, "Wr Flex", "WR", 12, 23), e(9, "Te One", "TE", 9, 6),
          e(10, "Our D", "D/ST", 7, 16), e(11, "Our K", "K", 8, 17), e(12, "Wr Bench", "WR", 6, 20)]
    tb = [e(21, "Bad Qb", "QB", 9, 0, 2), e(22, "B Rb1", "RB", 17, 2, 2), e(23, "B Rb2", "RB", 15, 2, 2),
          e(24, "B Rb3", "RB", 14, 20, 2), e(31, "B Rb4", "RB", 13, 20, 2), e(32, "B Rb5", "RB", 12.5, 20, 2),
          e(25, "B Wr1", "WR", 13, 4, 2), e(26, "B Wr2", "WR", 12, 4, 2),
          e(27, "B Wr3", "WR", 15, 23, 2), e(28, "B Te", "TE", 8, 6, 2), e(29, "B D", "D/ST", 6, 16, 2),
          e(30, "B K", "K", 7, 17, 2)]
    def balanced(t, base):
        return [e(base + 1, f"T{t} Qb", "QB", 18, 0, t), e(base + 2, f"T{t} Rb1", "RB", 14, 2, t),
                e(base + 3, f"T{t} Rb2", "RB", 11, 2, t), e(base + 4, f"T{t} Wr1", "WR", 14, 4, t),
                e(base + 5, f"T{t} Wr2", "WR", 12, 4, t), e(base + 6, f"T{t} Wr3", "WR", 10, 23, t),
                e(base + 7, f"T{t} Te", "TE", 8, 6, t), e(base + 8, f"T{t} D", "D/ST", 6, 16, t),
                e(base + 9, f"T{t} K", "K", 7, 17, t)]
    s = make_state(us, slots=SLOTS, extra_teams=[(2, "Team B", tb), (3, "Team C", balanced(3, 40)),
                                                 (4, "Team D", balanced(4, 60))])
    # market values: QBs are cheap in 1-QB redraft, RBs expensive
    fc = {"Goff Qb": 3000, "Stafford Qb": 2900, "Dak Qb": 4000, "B Rb3": 3100, "B Rb2": 3600, "B Rb1": 5000,
          "B Rb4": 2900, "B Rb5": 2800, "Rb Weak": 400, "Wr Bench": 500}
    for t in s.teams.values():
        for p in t.roster:
            p.market_value = fc.get(p.name, 2000.0)
    return s


def test_team_value_sees_qb3_as_surplus():
    s = league()
    base = teamneeds.team_value(s.my_team.roster, s)
    without_qb3 = teamneeds.team_value([p for p in s.my_team.roster if p.name != "Stafford Qb"], s)
    without_rb1 = teamneeds.team_value([p for p in s.my_team.roster if p.name != "Rb One"], s)
    assert base - without_qb3 < 0.2 * (base - without_rb1)


def test_scout_profiles_and_surplus_match():
    s = league()
    prof = teamneeds.profiles(s)
    me, tb = prof[1], prof[2]
    assert me.rank["QB"] == 1 and any(p.name == "Stafford Qb" for p in me.surplus)
    assert "QB" in tb.holes and any(p.name == "B Rb5" for p in tb.surplus)
    lines = teamneeds.surplus_matches(s, prof)
    assert any("QB surplus: Team B" in l for l in lines)
    summary = teamneeds.strength_summary(me, len(s.teams))
    assert summary[0].startswith("QB: surplus, rank 1 of 4")


def test_trade_ideas_move_qb_surplus_to_team_with_qb_hole():
    s = league()
    ideas = teamneeds.trade_ideas(s, Prefs())
    assert ideas
    top = ideas[0]
    assert top.partner == "Team B"
    assert any(p.position == "QB" for p in top.give) and any(p.position == "RB" for p in top.get)
    assert top.our_gain > 5 and top.their_gain > 0 and top.fairness >= 0.75
    assert "Team B is 4 of 4 at QB" in top.why_partner and "you're 4 of 4 at QB" in top.pitch


def test_never_trade_is_respected():
    s = league()
    prefs = Prefs(never_trade={"goff qb", "stafford qb"})
    for idea in teamneeds.trade_ideas(s, prefs, top=20):
        assert not any(p.name in ("Goff Qb", "Stafford Qb") for p in idea.give)


def test_fc_attach_by_espn_id_then_name_and_suffix():
    s = league()
    for t in s.teams.values():
        for p in t.roster:
            p.market_value = None
    rows = [{"player": {"name": "Goff QB", "position": "QB", "maybeTeam": "XXX", "espnId": "2"}, "value": 3000, "overallRank": 90},
            {"player": {"name": "B Rb1 Jr.", "position": "RB", "maybeTeam": None}, "value": 5000, "overallRank": 5}]
    assert market.attach(s, rows) == 2
    assert s.find_player("Goff Qb").market_value == 3000 and s.find_player("B Rb1").market_value == 5000


def test_spearman_and_compare():
    assert market.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert market.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    s = league()
    c = market.compare(s, top=3)
    assert c.n > 10 and c.overall is not None and set(c.by_position) == {"QB", "RB", "WR", "TE"}
    assert len(c.ours_higher) <= 3 and len(c.fc_higher) <= 3


# ---- value over replacement

def test_replacement_levels_follow_league_slots():
    from fantasy_ai import vorp
    s = league()
    n = vorp.starters_per_position(s)
    assert n["QB"] == 4 and n["TE"] == 4 and n["RB"] + n["WR"] == 4 * 5      # 2 RB + 2 WR + 1 FLEX per team
    lv = vorp.replacement_levels(s)
    assert set(lv) == {"QB", "RB", "WR", "TE"}
    s.slot_counts[23] = 2                                                     # a 2-FLEX league starts one more
    n2 = vorp.starters_per_position(s)
    assert n2["RB"] + n2["WR"] + n2["TE"] == n["RB"] + n["WR"] + n["TE"] + 4


def test_backup_qb_is_not_a_top_asset_by_vorp():
    from fantasy_ai import vorp
    s = league()
    goff, rb1 = s.find_player("Goff Qb"), s.find_player("B Rb1")
    assert goff.ros_value(4) > rb1.ros_value(4)             # raw points say QB
    assert vorp.vorp_ros(goff, s) < vorp.vorp_ros(rb1, s)   # above replacement says RB


def test_spare_qb_depth_credit_counts_only_above_replacement():
    from fantasy_ai import vorp
    s = league()
    tc, spare = s.teams[3], s.find_player("T4 Qb")          # same level as their starter: pure depth
    gain = teamneeds.team_value(tc.roster + [spare], s) - teamneeds.team_value(tc.roster, s)
    raw_credit = teamneeds.DEPTH_CREDIT * teamneeds.season_value(spare) * 14
    assert 0 <= gain < 0.5 * raw_credit
    assert abs(gain - teamneeds.DEPTH_CREDIT * vorp.vorp_pg(spare, s) * 14) < 1


def test_pitch_is_clean_sentences():
    for i in teamneeds.trade_ideas(league(), Prefs()):
        assert "? and" not in i.pitch and i.pitch.count("?") == 1 and i.pitch.endswith((".", "?"))


# ---- cheapest fair package, both teams must gain (Ian's rule)

def kelce_league():
    """Kelce case: our TE is weak and Goedert is a doubtful backup TE; Williams is a bench WR,
    Stafford a spare QB. Bread-style partner is fine at WR, has Kelce plus a solid TE2."""
    us = [e(1, "Dak Qb", "QB", 20, 0), e(2, "Stafford Qb", "QB", 18, 20), e(3, "Rb A", "RB", 18, 2),
          e(4, "Rb B", "RB", 15, 2), e(5, "Wr A", "WR", 16, 4), e(6, "Wr B", "WR", 14, 4),
          e(7, "Wr C", "WR", 13, 23), e(8, "Jameson Williams", "WR", 11, 20), e(9, "Loveland Te", "TE", 7, 6),
          e(10, "Dallas Goedert", "TE", 5, 20), e(11, "Our D", "D/ST", 7, 16), e(12, "Our K", "K", 8, 17)]
    them = [e(21, "Weak Qb", "QB", 13, 0, 2), e(22, "C Rb1", "RB", 15, 2, 2), e(23, "C Rb2", "RB", 12, 2, 2),
            e(24, "C Wr1", "WR", 11, 4, 2), e(25, "C Wr2", "WR", 9, 4, 2), e(26, "C Wr3", "WR", 8, 23, 2),
            e(27, "Travis Kelce", "TE", 13, 6, 2), e(28, "C Te2", "TE", 10, 20, 2),
            e(29, "C D", "D/ST", 6, 16, 2), e(30, "C K", "K", 7, 17, 2)]
    def balanced(t, base):
        return [e(base + 1, f"T{t} Qb", "QB", 19, 0, t), e(base + 2, f"T{t} Rb1", "RB", 15, 2, t),
                e(base + 3, f"T{t} Rb2", "RB", 13, 2, t), e(base + 4, f"T{t} Wr1", "WR", 15, 4, t),
                e(base + 5, f"T{t} Wr2", "WR", 13, 4, t), e(base + 6, f"T{t} Wr3", "WR", 12, 23, t),
                e(base + 7, f"T{t} Te", "TE", 9, 6, t), e(base + 8, f"T{t} D", "D/ST", 6, 16, t),
                e(base + 9, f"T{t} K", "K", 7, 17, t)]
    s = make_state(us, slots=SLOTS, extra_teams=[(2, "Bread", them), (3, "Team C", balanced(3, 40)),
                                                 (4, "Team D", balanced(4, 60))])
    fc = {"Travis Kelce": 1631, "Jameson Williams": 1236, "Stafford Qb": 979, "Dallas Goedert": 369,
          "Dak Qb": 3000, "Loveland Te": 1100}
    for t in s.teams.values():
        for p in t.roster:
            p.market_value = fc.get(p.name, 2500.0)
    return s


def _ids(ps):
    return {p.name for p in ps}


def test_default_ideas_help_both_lineups_and_hit_a_need():
    for s in (league(), kelce_league()):
        prof = teamneeds.profiles(s)
        for i in teamneeds.trade_ideas(s, Prefs(), top=20):
            assert i.their_gain >= teamneeds.MIN_THEIR_GAIN > 0 and i.our_gain > 0
            assert i.our_gain > i.their_gain or (0.9 <= i.fc_give / i.fc_get <= 1.10
                                                 and i.our_gain >= teamneeds.CLEAR_EDGE)
            pf = prof[i.partner_id]
            assert any(p.position in pf.holes or any(d.startswith(p.position) for d in pf.desperation) for p in i.give)
            assert not i.hard_sell


def test_kelce_case_williams_goedert_not_suggested_because_it_hurts_them():
    s = kelce_league()
    kelce = [i for i in teamneeds.trade_ideas(s, Prefs(), top=50) if _ids(i.get) == {"Travis Kelce"}]
    assert all(_ids(i.give) != {"Jameson Williams", "Dallas Goedert"} for i in kelce)
    lop = [i for i in teamneeds.trade_ideas(s, Prefs(), top=50, include_lopsided=True)
           if _ids(i.get) == {"Travis Kelce"}]
    assert lop, "the one-sided view still finds a Kelce deal"


def test_cheapest_package_preferred_when_both_work():
    assert teamneeds.price_factor(1.0) == 1.0 and teamneeds.price_factor(0.95) == 1.0
    assert teamneeds.price_factor(1.25) < 0.75 < teamneeds.price_factor(1.10)
    s = league()
    for i in teamneeds.trade_ideas(s, Prefs(), top=20):
        assert i.fc_give / i.fc_get < teamneeds.MAX_OVERPAY   # never 20%+ over market by default
        assert set(i.give_cost) == {p.player_id for p in i.give}


def test_hard_sell_carries_sweetened_alternative():
    s = kelce_league()
    for i in teamneeds.trade_ideas(s, Prefs(), top=50, include_lopsided=True):
        if i.hard_sell and i.sweetened is not None:
            assert not i.sweetened.hard_sell and _ids(i.sweetened.get) == _ids(i.get)


def test_dst_negative_ids_do_not_confuse_replacement_players():
    s = league()
    for t in s.teams.values():
        for p in t.roster:
            if p.position == "D/ST":
                p.player_id = -16000 - t.team_id          # ESPN D/ST ids are negative
    assert teamneeds.team_value(s.my_team.roster, s) > 0


# ---- one lineup-delta function for trade ideas and `fai trade`

def test_trade_ideas_partner_delta_equals_evaluate_trade():
    from fantasy_ai.trades import evaluate_trade
    for s in (league(), kelce_league()):
        for i in teamneeds.trade_ideas(s, Prefs(), top=6, include_lopsided=True):
            ev = evaluate_trade(s, i.give, i.get)
            assert ev.me.lineup_delta == i.our_gain and ev.partner.lineup_delta == i.their_gain


def test_forced_drops_counted_when_partner_goes_over_roster_limit():
    s = kelce_league()
    bread = s.teams[2]
    from fantasy_ai.espn_read import parse_player
    fill = [parse_player(b.entry(b.player(300 + k, f"Filler {k}", "WR", 1.0, season=10), 20, team_id=2), 2026, 4, {})
            for k in range(teamneeds.roster_limit(s) - len(bread.roster))]
    for f in fill:
        f.on_team_id = 2
    bread.roster.extend(fill)                                    # Bread is now exactly at the limit
    give = [s.find_player(n) for n in ("Jameson Williams", "Dallas Goedert", "Stafford Qb")]
    delta, drops = teamneeds.lineup_change(s, 2, [s.find_player("Travis Kelce")], give)
    assert len(drops) == 2 and all(d.position != "K" for d in drops)
    assert not {d.name for d in drops} & {"Weak Qb"}             # last QB is never cut


def test_replacement_floor_injured_starter_counts_as_waiver_level_not_zero():
    s = league()
    tc = s.teams[3]
    wr1 = s.find_player("T3 Wr1")
    base = teamneeds.team_value(tc.roster, s)
    wr1.injury_status = "OUT"
    hurt = teamneeds.team_value(tc.roster, s, {})
    assert hurt < base                                           # losing a starter this week still costs
    # but only his value above a replacement WR this week, not his whole week
    assert base - hurt < wr1.proj_week
