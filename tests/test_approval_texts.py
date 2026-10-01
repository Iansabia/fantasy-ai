"""Approval texts: pros / cons from engine facts, readable on a phone, under ~900 characters."""
from conftest import b, make_state
from fantasy_ai import approval, notify, teamneeds, waivers
from fantasy_ai.pending import PendingAction
from fantasy_ai.prefs import Prefs


def two_te_league():
    e = [b.entry(b.player(1, "Q", "QB", 20), 0), b.entry(b.player(2, "Rb One", "RB", 16), 2),
         b.entry(b.player(3, "Rb Two", "RB", 4), 2), b.entry(b.player(4, "Wr One", "WR", 14), 4),
         b.entry(b.player(5, "Wr Two", "WR", 12), 4), b.entry(b.player(6, "Schultz Te", "TE", 9), 6),
         b.entry(b.player(7, "Pitts Te", "TE", 7), 20), b.entry(b.player(8, "Flex", "WR", 10), 23),
         b.entry(b.player(9, "D", "D/ST", 6), 16), b.entry(b.player(10, "K", "K", 8), 17)]
    e += [b.entry(b.player(20 + i, f"Bench Wr{i}", "WR", 6 + i), 20) for i in range(6)]
    return make_state(e, fas=[b.fa_row(b.player(90, "Woody Rb", "RB", 13, season=210, pro=3))])


def render(kind, summary):
    return notify.msg_pending(PendingAction(id="7", league="CP", kind=kind, summary=summary, payload={},
                                            created="", expires=""))


def test_waiver_text_has_pros_cons_thin_te_and_reply_line():
    s = two_te_league()
    sg = waivers.evaluate_add(s, s.find_player("Woody Rb"), cfg={"min_gain_free_agent": 0})
    sg.drop = s.find_player("Pitts Te")       # the case Ian asked about: dropping our only backup TE
    text = render("FREEAGENT", approval.waiver_text(s, sg))
    assert text.splitlines()[2].startswith("Add Woody Rb (RB), drop Pitts Te (TE)")
    assert "Pros:\n• " in text and "Cons:\n• " in text and "leaves 1 TE (no backup)" in text
    assert "Verdict:" in text and text.endswith("Reply 'approve 7' or 'reject 7'")
    assert len(text) <= 900


def test_trade_text_has_their_side_and_fits():
    from test_trade_engine import league
    s = league()
    idea = teamneeds.trade_ideas(s, Prefs(), top=1)[0]
    text = render("TRADE_PROPOSAL", approval.trade_text(s, idea))
    assert "Give " in text and " | get " in text and "For them: lineup +" in text
    assert "Pros:" in text and "Cons:" in text and "Verdict:" in text and len(text) <= 900


def test_long_texts_are_trimmed_but_keep_head_and_verdict():
    long = "Add X\nPros:\n" + "\n".join(f"• {'x' * 200}" for _ in range(3)) + "\nCons:\n" + \
        "\n".join(f"• {'y' * 200}" for _ in range(3)) + "\nVerdict: worth it"
    out = approval._fit(long)
    assert len(out) <= approval.MAX_CHARS and out.startswith("Add X") and out.endswith("Verdict: worth it")


def test_expert_rank_line_goes_under_cons_when_experts_favor_the_drop():
    s = two_te_league()
    sg = waivers.evaluate_add(s, s.find_player("Woody Rb"), cfg={"min_gain_free_agent": 0})
    sg.drop = s.find_player("Pitts Te")
    s.find_player("Woody Rb").ecr = {"ros": 45, "ros_text": "RB45"}
    s.find_player("Pitts Te").ecr = {"ros": 3, "ros_text": "TE3"}
    text = approval.waiver_text(s, sg)
    pros, cons = text.split("Cons:")
    assert "TE3" not in pros and "dropping a TE the experts rank TE3 ROS (Woody Rb is RB45)" in cons
    s.find_player("Woody Rb").ecr = {"ros": 2, "ros_text": "RB2"}
    s.find_player("Pitts Te").ecr = {"ros": 30, "ros_text": "TE30"}
    text = approval.waiver_text(s, sg)
    pros, cons = text.split("Cons:")
    assert "experts favor what we get: Woody Rb RB2" in pros and "experts" not in cons


def _numbers(text):
    import re
    gain = re.search(r"gain ([+-]\d+\.\d) pts over 3 wks", text)
    verdict = re.search(r"Verdict: .*?([+-]\d+\.\d) pts over 3 wks|Verdict: below the bar, ([+-]\d+\.\d)", text)
    return gain and gain.group(1), verdict and (verdict.group(1) or verdict.group(2))


def test_verdict_number_always_equals_the_gain_shown_in_pros():
    s = two_te_league()
    for drop in ("Pitts Te", "Bench Wr0", "Bench Wr5"):
        sg = waivers.evaluate_add(s, s.find_player("Woody Rb"), cfg={"min_gain_free_agent": 0})
        g, v = _numbers(approval.waiver_text(s, sg))
        assert g is not None and g == v
        assert abs(sg.gain - round(sg.lineup_gain + sg.ros_term, 1)) <= 0.11


def test_experts_ranking_the_drop_higher_cancels_our_ros_edge():
    s = two_te_league()
    add, drop = s.find_player("Woody Rb"), s.find_player("Pitts Te")
    add.proj_season, drop.proj_season = 300, 60          # our ROS strongly favors the add
    base = waivers.evaluate_add(s, add, cfg={"min_gain_free_agent": 0})
    add.ecr, drop.ecr = {"ros": 45, "ros_text": "RB45"}, {"ros": 3, "ros_text": "TE3"}
    vetoed = waivers.evaluate_add(s, add, cfg={"min_gain_free_agent": 0})
    if base.drop is not None and base.drop.player_id == drop.player_id and base.ros_term > 0:
        assert vetoed.drop is None or vetoed.drop.player_id != drop.player_id or vetoed.ros_term == 0
    assert waivers.favors(drop, add, s) and not waivers.favors(add, drop, s)
