"""Questionable starters: defer to the final pre-game run, calibrated play odds, Sunday inactives."""
import datetime as dt
import json

from conftest import b, make_state
from fantasy_ai import availability, cli, engine, gametime, schedule
from fantasy_ai.config import LeagueConfig
from fantasy_ai.espn_read import EspnReader
from fantasy_ai.lineup import optimize
from fantasy_ai.news import adjust
from fantasy_ai.news.classify import Signal
from fantasy_ai.slots import BENCH

ET = schedule.ET
SUN_ON = {"sunday_inactive_check": True}


def et(*a):
    return dt.datetime(*a, tzinfo=ET)


SUN_1PM = et(2026, 10, 4, 13, 0)
SUN_425 = et(2026, 10, 4, 16, 25)
THU_815 = et(2026, 10, 1, 20, 15)
MON_815 = et(2026, 10, 5, 20, 15)


def test_decide_defers_while_a_later_run_comes_before_kickoff():
    wed = et(2026, 9, 30, 15)
    assert gametime.decide(SUN_1PM, wed, SUN_ON) == ("defer", et(2026, 10, 1, 14, 0))       # Thursday run
    sat = et(2026, 10, 3, 14, 5)
    assert gametime.decide(SUN_1PM, sat, SUN_ON) == ("defer", et(2026, 10, 4, 11, 45))      # Sunday run
    assert gametime.decide(SUN_1PM, sat, {}) == ("final", None)                             # no Sunday run


def test_decide_final_and_active_on_sunday():
    sun = et(2026, 10, 4, 11, 50)
    assert gametime.decide(SUN_1PM, sun, SUN_ON) == ("active", None)       # inactives are out
    assert gametime.decide(SUN_425, sun, SUN_ON) == ("final", None)        # 4:25 inactives not out yet
    assert gametime.decide(THU_815, et(2026, 10, 1, 14, 2), SUN_ON) == ("final", None)    # TNF at the Thu run
    assert gametime.decide(MON_815, et(2026, 10, 3, 14, 5), SUN_ON)[0] == "defer"
    assert gametime.decide(MON_815, et(2026, 10, 4, 11, 50), SUN_ON) == ("final", None)
    assert gametime.decide(None, sun, SUN_ON) == ("final", None)            # unknown kickoff: no deferral


def test_run_times_come_from_config():
    slots = schedule.run_slots({"sunday_inactive_check": True, "runs": {"inactives": "sun 11:35"}})
    assert ("inactives", 6, 11, 35) in slots and ("full", 3, 14, 0) in slots
    assert all(m != "inactives" for m, *_ in schedule.run_slots({}))


def test_play_probability_calibration():
    assert availability.play_probability("QUESTIONABLE", "limited", "wed") == 0.85
    assert availability.play_probability("QUESTIONABLE", "limited", "fri") == 0.66   # measured, final report
    assert availability.play_probability("QUESTIONABLE", None, None) == 0.67
    assert availability.play_probability("ACTIVE", "dnp", "thu") == 0.55
    assert availability.play_probability("DOUBTFUL") == 0.02 and availability.play_probability("OUT") == 0.0
    assert availability.practice_day(None, "2026-09-30T18:00:00+00:00") == "wed"
    assert availability.practice_day("Saturday", None) == "fri"


def lamar_league():
    return make_state([
        b.entry(b.player(1, "Lamar Jackson", "QB", 21.14, injury="QUESTIONABLE", pro=1), 0),
        b.entry(b.player(2, "Backup Qb", "QB", 15.0, pro=3), 20),
        b.entry(b.player(3, "Rb A", "RB", 15, pro=3), 2), b.entry(b.player(4, "Rb B", "RB", 12, pro=3), 2),
        b.entry(b.player(5, "Wr A", "WR", 14, pro=3), 4), b.entry(b.player(6, "Wr B", "WR", 12, pro=3), 4),
        b.entry(b.player(7, "Te A", "TE", 9, pro=3), 6), b.entry(b.player(8, "Flex", "WR", 10, pro=3), 23),
        b.entry(b.player(9, "D", "D/ST", 6, pro=3), 16), b.entry(b.player(10, "K", "K", 8, pro=3), 17),
    ])


def raw_pro(kick):
    ms = int(kick.timestamp() * 1000)
    teams = []
    for i, a, bye in b.PRO_TEAMS:
        g = [{"homeProTeamId": 1, "awayProTeamId": 2, "date": ms}] if i in (1, 2) else \
            [{"homeProTeamId": 3, "awayProTeamId": 4, "date": ms}] if i in (3, 4) else []
        teams.append({"id": i, "abbrev": a, "byeWeek": bye, "proGamesByScoringPeriod": {"4": g}})
    return {"settings": {"proTeams": teams}}


def limited_wed(conf=0.9):
    return Signal(1, "Lamar Jackson", "questionable", 0, "Limited in Wednesday practice with back injury",
                  0.0, conf, "RotoBaller", "https://example.com", "2026-09-30T17:00:00+00:00",
                  expected_to_play="uncertain", practice="limited", practice_day="wed")


def test_lamar_limited_wednesday_is_deferred_not_benched():
    s = lamar_league()
    adjust.apply(s, [limited_wed()])
    p = s.find_player("Lamar Jackson")
    assert 0.80 <= p.news_status_factor <= 0.90 and "plays ~85% historically" in p.reasons[0]
    notes = gametime.apply(s, raw_pro(SUN_1PM), SUN_ON, at=et(2026, 9, 30, 15))
    assert notes and p.proj_week == 21.14 and p.lineup_value(4) == 21.14
    assert "decision deferred to the final pre-game run (Thu 2:00PM ET)" in p.reasons[0]
    assert optimize(s).target[1] == 0                                   # Lamar keeps the QB spot


def test_final_run_applies_play_probability_and_sunday_active_counts_full():
    # Saturday run with no Sunday check = final for a Sunday game: the calibrated haircut stays
    s = lamar_league()
    adjust.apply(s, [limited_wed()])
    gametime.apply(s, raw_pro(SUN_1PM), {}, at=et(2026, 10, 3, 14, 5))
    assert s.find_player("Lamar Jackson").lineup_value(4) < 21.14
    # Sunday 11:50 after inactives, not ruled out: full value
    s2 = make_state([b.entry(b.player(1, "Lamar Jackson", "QB", 21.14, injury="QUESTIONABLE", pro=1), 0)])
    gametime.apply(s2, raw_pro(SUN_1PM), SUN_ON, at=et(2026, 10, 4, 11, 50))
    assert s2.find_player("Lamar Jackson").lineup_value(4) == 21.14


def test_sunday_inactives_autopilot_end_to_end(tmp_path, monkeypatch, capsys):
    """Sunday 11:50 ET: OUT starter swapped for the best backup, questionable-but-active starts."""
    entries = [
        b.entry(b.player(1, "Q Star", "QB", 20.0, injury="QUESTIONABLE", pro=1), 20),   # active after inactives
        b.entry(b.player(2, "Healthy Qb", "QB", 16.0, pro=3), 0),
        b.entry(b.player(3, "Ruled Out Rb", "RB", 15, injury="OUT", pro=3), 2),
        b.entry(b.player(4, "Rb B", "RB", 12, pro=3), 2), b.entry(b.player(5, "Rb Bench", "RB", 9, pro=3), 20),
        b.entry(b.player(6, "Wr A", "WR", 14, pro=3), 4), b.entry(b.player(7, "Wr B", "WR", 12, pro=3), 4),
        b.entry(b.player(8, "Te A", "TE", 9, pro=3), 6), b.entry(b.player(9, "Flex", "WR", 10, pro=3), 23),
        b.entry(b.player(10, "D", "D/ST", 6, pro=3), 16), b.entry(b.player(11, "K", "K", 8, pro=3), 17)]
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "league.json").write_text(json.dumps(b.league([(1, "Mine", entries)])))
    (fx / "pro_teams.json").write_text(json.dumps(raw_pro(SUN_1PM)))
    cfg = tmp_path / "leagues.yaml"
    cfg.write_text(f"leagues:\n  - {{name: sun, owner: ian, league_id: 99, team_id: 1, year: 2026, "
                   f"auth_profile: ian, fixture: {fx}}}\nsettings: {{auto_lineup: false}}\n"
                   f"notify: {{backend: console}}\nschedule: {{sunday_inactive_check: true}}\n")
    monkeypatch.setenv("FAI_NOW", "2026-10-04T11:50:00-04:00")
    lg = LeagueConfig(name="sun", owner="ian", league_id=99, team_id=1, year=2026, auth_profile="ian", fixture=str(fx))
    reader = EspnReader(lg)
    state = reader.load()
    engine.prepare(state, reader, news=False, schedule_cfg=SUN_ON)
    res = optimize(state)
    assert res.target[1] == 0 and res.target[2] == BENCH            # Q Star active -> starts over 16.0
    assert res.target[3] == BENCH and res.target[5] == 2            # OUT RB swapped for the bench RB
    rc = cli.main(["--config", str(cfg), "--store", str(tmp_path / "p.json"), "autopilot", "--demo",
                   "--mode", "inactives", "--no-news"])
    out = capsys.readouterr().out
    assert rc == 0 and "inactives: 4 moves, dry-run" in out
