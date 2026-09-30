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
