from conftest import b, make_state
from fantasy_ai.lineup import optimize
from fantasy_ai.slots import BENCH, IR


def by_name(state, name):
    return next(p for p in state.roster if p.name == name)


def test_demo_benches_bye_and_out_and_keeps_locked(demo_state):
    res = optimize(demo_state)
    t = res.target
    s = demo_state
    assert t[by_name(s, "Hurt Hal").player_id] == BENCH       # OUT
    assert t[by_name(s, "Bye Bill").player_id] == BENCH       # bye
    assert t[by_name(s, "Thursday Tank").player_id] == 2      # locked stays
    assert t[by_name(s, "Season Sid").player_id] == IR        # IR slot stays
    assert t[by_name(s, "Ready Rex").player_id] == 2
    assert t[by_name(s, "Bench Ben").player_id] == 4
    assert t[by_name(s, "Slot Sam").player_id] == 23          # FLEX
    assert res.before_points == 82.0 and res.after_points == 109.0 and res.gain == 27.0
    moved = {m.player_id for m in res.moves}
    assert by_name(s, "Thursday Tank").player_id not in moved
    assert all(m.from_slot != m.to_slot for m in res.moves)
    # vacates first
    firsts = [m.to_slot for m in res.moves]
    assert firsts == sorted(firsts, key=lambda x: 0 if x == BENCH else 1)


def test_already_optimal_no_moves():
    e = [b.entry(b.player(1, "Q", "QB", 20), 0), b.entry(b.player(2, "R1", "RB", 15), 2),
         b.entry(b.player(3, "R2", "RB", 14), 2), b.entry(b.player(4, "W1", "WR", 13), 4),
         b.entry(b.player(5, "W2", "WR", 12), 4), b.entry(b.player(6, "T", "TE", 9), 6),
         b.entry(b.player(7, "F", "RB", 11), 23), b.entry(b.player(8, "D", "D/ST", 6), 16),
         b.entry(b.player(9, "K", "K", 7), 17), b.entry(b.player(10, "Bn", "WR", 5), 20)]
    res = optimize(make_state(e))
    assert res.moves == [] and res.gain == 0


def test_flex_takes_best_leftover_te():
    e = [b.entry(b.player(1, "Q", "QB", 20), 0), b.entry(b.player(2, "R1", "RB", 5), 2),
         b.entry(b.player(3, "R2", "RB", 4), 2), b.entry(b.player(4, "W1", "WR", 13), 4),
         b.entry(b.player(5, "W2", "WR", 12), 4), b.entry(b.player(6, "T1", "TE", 9), 6),
         b.entry(b.player(7, "T2", "TE", 8), 20), b.entry(b.player(8, "R3", "RB", 3), 23),
         b.entry(b.player(9, "D", "D/ST", 6), 16), b.entry(b.player(10, "K", "K", 7), 17)]
    res = optimize(make_state(e))
    assert res.target[7] == 23 and res.target[8] == BENCH


def test_locked_bench_player_cannot_be_started():
    e = [b.entry(b.player(1, "Q", "QB", 10), 0), b.entry(b.player(2, "Q2", "QB", 30), 20, locked=True)]
    res = optimize(make_state(e))
    assert res.moves == []


def test_locked_out_starter_warns_and_stays():
    e = [b.entry(b.player(1, "Q", "QB", 10, injury="OUT"), 0, locked=True), b.entry(b.player(2, "Q2", "QB", 30), 20)]
    res = optimize(make_state(e))
    assert res.moves == [] and any("locked" in w for w in res.warnings)


def test_superflex_op_uses_second_qb():
    e = [b.entry(b.player(1, "Q1", "QB", 22), 0), b.entry(b.player(2, "Q2", "QB", 18), 20),
         b.entry(b.player(3, "R1", "RB", 10), 2), b.entry(b.player(4, "R2", "RB", 9), 2),
         b.entry(b.player(5, "W1", "WR", 12), 7)]
    res = optimize(make_state(e, slots=b.SUPERFLEX_SLOTS))
    assert res.target[2] == 7 and res.target[5] in (4, 23)


def test_questionable_still_starts_but_ir_status_benched():
    e = [b.entry(b.player(1, "Q", "QB", 20, injury="QUESTIONABLE"), 20),
         b.entry(b.player(2, "Q2", "QB", 10, injury="INJURY_RESERVE"), 0)]
    res = optimize(make_state(e))
    assert res.target[1] == 0 and res.target[2] == IR          # benched, then moved to the open IR slot
    assert [(m.player_id, m.from_slot, m.to_slot) for m in res.moves] == [(2, 0, BENCH), (1, BENCH, 0)]
    assert [(m.player_id, m.from_slot, m.to_slot) for m in res.ir_moves] == [(2, BENCH, IR)]


def test_empty_slot_left_empty_rather_than_starting_bye():
    e = [b.entry(b.player(1, "Q", "QB", 20, pro=2), 0)]   # pro team 2 on bye week 4
    res = optimize(make_state(e))
    assert res.target[1] == BENCH and res.after_points == 0


def test_swap_margin_needs_one_point_and_five_percent():
    from conftest import make_state
    from fantasy_ai.lineup import optimize
    # Dak 19.0 starting vs Goff 19.3 on the bench: +0.3 is noise, no swap
    e = [b.entry(b.player(1, "Dak", "QB", 19.0), 0), b.entry(b.player(2, "Goff", "QB", 19.3), 20)]
    assert optimize(make_state(e)).moves == []
    # +1.2 clears 1.0 but not 5% of 30 (1.5): still no swap
    e = [b.entry(b.player(1, "Star", "QB", 30.0), 0), b.entry(b.player(2, "Other", "QB", 31.2), 20)]
    assert optimize(make_state(e)).moves == []
    # +1.6 clears both: swap
    e = [b.entry(b.player(1, "Star", "QB", 30.0), 0), b.entry(b.player(2, "Other", "QB", 31.6), 20)]
    assert {m.name for m in optimize(make_state(e)).moves} == {"Star", "Other"}


def test_swap_margin_does_not_protect_injured_or_out_starters():
    from conftest import make_state
    from fantasy_ai.lineup import optimize
    # questionable starter (final-run discount 0.67): 15 -> 10.05 vs healthy 10.5 -> swap
    one_wr = {"4": 1, "20": 5, "21": 1}
    e = [b.entry(b.player(1, "Hurt", "WR", 15.0, injury="QUESTIONABLE"), 4), b.entry(b.player(2, "Fine", "WR", 10.5), 20)]
    assert {m.name for m in optimize(make_state(e, slots=one_wr)).moves} == {"Hurt", "Fine"}
    e = [b.entry(b.player(1, "Gone", "WR", 15.0, injury="OUT"), 4), b.entry(b.player(2, "Fine", "WR", 2.0), 20)]
    assert {m.name for m in optimize(make_state(e, slots=one_wr)).moves} == {"Gone", "Fine"}
