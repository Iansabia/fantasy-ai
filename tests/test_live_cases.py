"""The exact lineup cases seen in the real leagues (week 4, 2026), with mocked news both ways,
plus automatic IR handling."""
from conftest import b, make_state
from fantasy_ai.lineup import optimize
from fantasy_ai.news import adjust
from fantasy_ai.news.classify import Signal
from fantasy_ai.slots import BENCH, IR


def league_a():
    # League A (12-team): healthy Pittman 9.8 at WR vs QUESTIONABLE Mitchell 10.5 on the bench
    return make_state([
        b.entry(b.player(1, "Lamar Jackson", "QB", 21.1), 0),
        b.entry(b.player(2, "Bijan Robinson", "RB", 21.5), 2),
        b.entry(b.player(3, "Jaylen Warren", "RB", 16.7), 2),
        b.entry(b.player(4, "Davante Adams", "WR", 14.7), 4),
        b.entry(b.player(5, "Michael Pittman Jr.", "WR", 9.8), 4),
        b.entry(b.player(6, "Trey McBride", "TE", 17.4), 6),
        b.entry(b.player(7, "Jaylen Waddle", "WR", 11.9, injury="QUESTIONABLE"), 23),
        b.entry(b.player(8, "Texans D/ST", "D/ST", 4.9, injury="NORMAL"), 16),
        b.entry(b.player(9, "Jason Myers", "K", 9.8), 17),
        b.entry(b.player(10, "Adonai Mitchell", "WR", 10.5, injury="QUESTIONABLE"), 20),
        b.entry(b.player(11, "Kenny Gainwell", "RB", 8.1), 20),
    ])


def league_b():
    # League B (10-team): QUESTIONABLE Evans 11.7 at FLEX vs healthy McConkey 11.4 on the bench
    return make_state([
        b.entry(b.player(1, "QB One", "QB", 18.0), 0),
        b.entry(b.player(2, "RB One", "RB", 15.0), 2),
        b.entry(b.player(3, "RB Two", "RB", 13.0), 2),
        b.entry(b.player(4, "WR One", "WR", 16.0), 4),
        b.entry(b.player(5, "WR Two", "WR", 14.0), 4),
        b.entry(b.player(6, "TE One", "TE", 9.0), 6),
        b.entry(b.player(7, "Mike Evans", "WR", 11.7, injury="QUESTIONABLE"), 23),
        b.entry(b.player(8, "Some D/ST", "D/ST", 6.0), 16),
        b.entry(b.player(9, "Some K", "K", 8.0), 17),
        b.entry(b.player(10, "Ladd McConkey", "WR", 11.4), 20),
    ], slots=b.BE6_SLOTS)


def sig(pid, name, play, note, conf=0.9, status="questionable"):
    return Signal(pid, name, status, 0, note, 0.0, conf, "NBC Rotoworld", "https://example.com", "2026-10-02",
                  expected_to_play=play)


def moved(res):
    return {m.name: (m.from_slot, m.to_slot) for m in res.moves}


# ---- no news: modest default discount, and the reason says so

def test_league_a_no_news_keeps_healthy_pittman():
    res = optimize(league_a())
    assert "Adonai Mitchell" not in moved(res) and "Michael Pittman Jr." not in moved(res)
    assert res.target[7] == 23      # Waddle (Q, 11.9 -> 10.1) still beats Gainwell 8.1


def test_league_b_no_news_starts_healthy_mcconkey():
    res = optimize(league_b())
    m = moved(res)
    assert m["Mike Evans"] == (23, BENCH) and m["Ladd McConkey"] == (BENCH, 23)
    evans = next(x for x in res.moves if x.name == "Mike Evans")
    assert "QUESTIONABLE (ESPN)" in evans.reason and "no news found, default discount" in evans.reason


# ---- positive news: full practice, expected to play -> start him

def test_league_a_positive_news_starts_mitchell():
    s = league_a()
    adjust.apply(s, [sig(10, "Adonai Mitchell", "likely", "full practice Friday, expected to play")])
    res = optimize(s)
    m = moved(res)
    assert m["Adonai Mitchell"] == (BENCH, 4) and m["Michael Pittman Jr."] == (4, BENCH)
    r = next(x for x in res.moves if x.name == "Adonai Mitchell").reason
    assert "likely to play: full practice Friday, expected to play (NBC Rotoworld, 2026-10-02)" in r


def test_league_b_positive_news_keeps_evans():
    s = league_b()
    adjust.apply(s, [sig(7, "Mike Evans", "likely", "full practice, will play")])
    res = optimize(s)
    assert "Mike Evans" not in moved(res) and res.target[7] == 23


# ---- negative news: no practice / game-time decision -> bench him

def test_league_a_negative_news_keeps_mitchell_benched():
    s = league_a()
    adjust.apply(s, [sig(10, "Adonai Mitchell", "uncertain", "did not practice, game-time decision")])
    res = optimize(s)
    assert "Adonai Mitchell" not in moved(res)
    assert s.find_player("Adonai Mitchell").lineup_value(4) < 7.0


def test_league_b_negative_news_benches_evans_with_cited_reason():
    s = league_b()
    adjust.apply(s, [sig(7, "Mike Evans", "uncertain", "did not practice, game-time decision")])
    res = optimize(s)
    evans = next(x for x in res.moves if x.name == "Mike Evans")
    assert evans.to_slot == BENCH and "(NBC Rotoworld, 2026-10-02)" in evans.reason
    assert "default discount" not in evans.reason


def test_ruled_out_news_is_zero():
    s = league_b()
    adjust.apply(s, [sig(7, "Mike Evans", None, "ruled out", conf=0.95, status="out")])
    assert s.find_player("Mike Evans").lineup_value(4) == 0


def test_doubtful_default_is_near_zero():
    s = make_state([b.entry(b.player(1, "D", "WR", 20, injury="DOUBTFUL"), 4),
                    b.entry(b.player(2, "H", "WR", 5), 20)])
    assert s.find_player("D").lineup_value(4) <= 2.0
    assert optimize(s).target[2] == 4


def test_normal_status_is_healthy():
    s = league_a()
    assert s.find_player("Texans D/ST").is_healthy()


# ---- IR

def test_ir_eligible_bench_player_moves_to_open_ir_in_separate_request():
    extra = b.entry(b.player(12, "Jonah Coleman", "RB", 0.0, injury="INJURY_RESERVE"), 20)
    s = make_state([b.entry(b.player(1, "Q", "QB", 20), 0), extra])
    res = optimize(s)
    assert res.moves == [] and [(m.player_id, m.from_slot, m.to_slot) for m in res.ir_moves] == [(12, BENCH, IR)]
    assert "INJURY_RESERVE (ESPN)" in res.ir_moves[0].reason


def test_healthy_player_activated_from_ir_when_room():
    s = make_state([b.entry(b.player(1, "Q", "QB", 20), 0),
                    b.entry(b.player(2, "Back", "RB", 9.0), 21)])       # ACTIVE but sitting in IR
    res = optimize(s)
    assert [(m.player_id, m.from_slot, m.to_slot) for m in res.ir_moves] == [(2, IR, BENCH)]


def test_activation_swaps_with_new_ir_player():
    s = make_state([b.entry(b.player(1, "Q", "QB", 20), 0),
                    b.entry(b.player(2, "Back", "RB", 9.0), 21),
                    b.entry(b.player(3, "Hurt", "RB", 0.0, injury="INJURY_RESERVE"), 20)])
    res = optimize(s)
    got = [(m.player_id, m.from_slot, m.to_slot) for m in res.ir_moves]
    assert (2, IR, BENCH) in got and (3, BENCH, IR) in got


def test_activation_with_full_roster_suggests_drop():
    # BE7 league: 9 starters + 7 bench = 16 active spots. Fill all 16, plus a healthy IR player.
    entries = [b.entry(b.player(100 + i, f"P{i}", pos, 5 + i), slot) for i, (pos, slot) in enumerate(
        [("QB", 0), ("RB", 2), ("RB", 2), ("WR", 4), ("WR", 4), ("TE", 6), ("WR", 23), ("D/ST", 16), ("K", 17)]
        + [("WR", 20)] * 7)]
    entries.append(b.entry(b.player(200, "Healed", "RB", 12.0), 21))
    res = optimize(make_state(entries))
    assert res.ir_moves == [] and res.ir_activate.name == "Healed"
    assert res.ir_drop.name == "P3"          # weakest player on the bench after the lineup is set
    assert any("roster is full" in w for w in res.warnings)


def test_waivers_do_not_drop_ir_eligible_player_headed_to_ir():
    from fantasy_ai.waivers import rank_free_agents
    s = make_state([b.entry(b.player(1, "Healthy RB", "RB", 8), 2),
                    b.entry(b.player(2, "Jonah Coleman", "RB", 0.0, season=0, injury="INJURY_RESERVE"), 20)],
                   fas=[b.fa_row(b.player(9, "Jaylen Wright", "RB", 7, season=150))])
    drops = [w.drop.name for w in rank_free_agents(s)]
    assert "Jonah Coleman" not in drops and drops == ["Healthy RB"]
