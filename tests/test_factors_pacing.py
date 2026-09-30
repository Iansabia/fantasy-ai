"""Factor score (offline fixtures) and anti-ban pacing (fake clock, no real sleeping)."""
import httpx
import pytest

from conftest import b, make_state
from fantasy_ai import cli
from fantasy_ai.config import Credentials, LeagueConfig
from fantasy_ai.espn_read import EspnReader
from fantasy_ai.espn_write import TransactionWriter, lineup_payload
from fantasy_ai.factors import providers, score
from fantasy_ai.factors.context import WeekContext
from fantasy_ai.lineup import Move, optimize
from fantasy_ai.pacing import Pacer, PacingStop

# pro teams: 1 AAA (home) vs 2 BBB; 3 CCC (home, outdoor BUF-like) vs 4 DDD
RAW_PRO = {"settings": {"proTeams": [
    {"id": 1, "abbrev": "KC", "location": "Kansas City", "name": "Chiefs",
     "proGamesByScoringPeriod": {"4": [{"homeProTeamId": 1, "awayProTeamId": 2, "date": 1791057600000}]}},
    {"id": 2, "abbrev": "DEN", "location": "Denver", "name": "Broncos",
     "proGamesByScoringPeriod": {"4": [{"homeProTeamId": 1, "awayProTeamId": 2, "date": 1791057600000}]}},
    {"id": 3, "abbrev": "BUF", "location": "Buffalo", "name": "Bills",
     "proGamesByScoringPeriod": {"4": [{"homeProTeamId": 3, "awayProTeamId": 4, "date": 1791057600000}]}},
    {"id": 4, "abbrev": "MIA", "location": "Miami", "name": "Dolphins",
     "proGamesByScoringPeriod": {"4": [{"homeProTeamId": 3, "awayProTeamId": 4, "date": 1791057600000}]}},
]}}
RAW_RATINGS = {"positionAgainstOpponent": {"positionalRatings": {
    "3": {"ratingsByOpponent": {"1": {"average": 20.0, "rank": 5}, "2": {"average": 40.0, "rank": 30},
                                "3": {"average": 30.0, "rank": 16}, "4": {"average": 30.0, "rank": 17}}},
}}}


def test_parse_games_and_ratings():
    games = providers.parse_pro_games(RAW_PRO, 4)
    assert games["KC"].opp == "DEN" and games["KC"].home and not games["DEN"].home
    avg, rank = providers.parse_positional_ratings(RAW_RATINGS, RAW_PRO)
    assert avg[("DEN", "WR")] == 40.0 and rank[("KC", "WR")] == 5


def test_parse_odds_implied_totals():
    events = [{"home_team": "Kansas City Chiefs", "away_team": "Denver Broncos", "bookmakers": [{"markets": [
        {"key": "spreads", "outcomes": [{"name": "Kansas City Chiefs", "point": -7.5}, {"name": "Denver Broncos", "point": 7.5}]},
        {"key": "totals", "outcomes": [{"name": "Over", "point": 47.0}, {"name": "Under", "point": 47.0}]}]}]}]
    v = providers.parse_odds(events, {"Kansas City Chiefs": "KC", "Denver Broncos": "DEN"})
    assert v["KC"] == (27.2, -7.5) and v["DEN"] == (19.8, 7.5)


def test_parse_weather_game_window():
    data = {"hourly": {"time": ["2026-10-04T17:00", "2026-10-04T18:00", "2026-10-04T19:00", "2026-10-04T20:00"],
                       "wind_speed_10m": [20, 22, 24, 5], "precipitation": [0, 2, 1, 0]}}
    assert providers.parse_weather(data, 1791133200000) == (22.0, 1.0)   # 17:00 UTC kickoff


def wr(pid, name, proj, pro):
    return b.entry(b.player(pid, name, "WR", proj, pro=pro), 20)


def ctx_for(state):
    ctx = WeekContext(week=4, games=providers.parse_pro_games(RAW_PRO, 4))
    ctx.def_vs_pos, ctx.def_rank = providers.parse_positional_ratings(RAW_RATINGS, RAW_PRO)
    return ctx


def test_matchup_and_vegas_and_weather_bounded_with_reasons():
    s = make_state([wr(1, "Kc Guy", 10, 1), wr(2, "Den Guy", 10, 2), wr(3, "Buf Guy", 10, 3)], raw_pro=RAW_PRO)
    ctx = ctx_for(s)
    ctx.vegas = {"KC": (27.2, -7.5), "DEN": (19.8, 7.5)}
    ctx.weather = {"BUF": (25.0, 2.0)}
    score.apply(s, ctx)
    kc, den, buf = (s.find_player(n) for n in ("Kc Guy", "Den Guy", "Buf Guy"))
    # KC WR faces DEN (allows 40 vs 30 avg) at home with a 27.2 total: boosted
    assert kc.score_multiplier > 1.1 and den.score_multiplier < 1.0
    names = {f.name for f in kc.factors}
    assert {"matchup", "home/away", "vegas", "game script"} <= names
    assert any("DEN allows 40.0 to WR" in f.reason for f in kc.factors)
    assert any(f.name == "weather" and f.multiplier < 1 for f in buf.factors)
    for p in (kc, den, buf):
        assert 0.70 <= p.score_multiplier <= 1.35
        for f in p.factors:
            assert f.reason


def test_total_cap_from_config():
    s = make_state([wr(1, "Kc Guy", 10, 1)], raw_pro=RAW_PRO)
    ctx = ctx_for(s)
    ctx.vegas = {"KC": (40.0, -14)}
    score.apply(s, ctx, {"total_cap": [0.9, 1.05]})
    assert s.find_player("Kc Guy").score_multiplier == 1.05


def test_usage_trend_moves_weekly_and_ros():
    s = make_state([wr(1, "Rising Star", 10, 1)])
    ctx = WeekContext(week=4, usage={"rising star": {"snap_share": [0.5, 0.6, 0.8], "target_share": [0.1, 0.15, 0.25]}})
    before = s.find_player("Rising Star").ros_value(4)
    score.apply(s, ctx)
    p = s.find_player("Rising Star")
    assert p.score_multiplier > 1 and p.ros_value(4) > before
    assert any("usage up" in f.reason for f in p.factors)


def test_factors_change_the_lineup_and_reasons_cite_them():
    # two WRs with equal ESPN projections; matchup decides
    s = make_state([b.entry(b.player(1, "Q", "QB", 20), 0), b.entry(b.player(2, "Den Guy", "WR", 10, pro=2), 4),
                    b.entry(b.player(3, "Kc Guy", "WR", 10, pro=1), 20)],
                   slots={"0": 1, "4": 1, "20": 5, "21": 1}, raw_pro=RAW_PRO)
    score.apply(s, ctx_for(s))
    res = optimize(s)
    start = next(m for m in res.moves if m.name == "Kc Guy")
    assert start.to_slot == 4 and "matchup" in start.reason


def test_ceiling_when_big_underdog():
    s = make_state([wr(1, "Boom", 10, 1)])
    ctx = WeekContext(week=4, our_edge=-0.2)
    score.apply(s, ctx)
    f = next(f for f in s.find_player("Boom").factors if f.name == "floor/ceiling")
    assert f.multiplier > 1 and "underdogs" in f.reason


def test_explain_command_on_fixture(capsys):
    rc = cli.main(["--config", "config/leagues.example.yaml", "explain", "demo", "Quinn Arm"])
    out = capsys.readouterr().out
    assert rc == 0 and "ESPN projection" in out and "worth this week" in out


# ---- pacing

class Clock:
    def __init__(self):
        self.t, self.slept = 1_790_000_000.0, []

    def sleep(self, s):
        self.slept.append(round(s, 1))
        self.t += s

    def now(self):
        return self.t


def pacer(tmp_path, **cfg):
    c = Clock()
    return Pacer(cfg, tmp_path / "pacing.json", sleep=c.sleep, rand=lambda a, b: b, clock=c.now), c


def test_jitter_and_gaps(tmp_path):
    p, c = pacer(tmp_path)
    p.start_jitter()
    p.between_leagues()            # first league: no gap
    p.between_leagues()
    p.before_read()                # first read: no gap
    p.before_read()
    assert c.slept == [600, 90, 4]


def test_write_caps_per_run_and_day(tmp_path):
    p, c = pacer(tmp_path, writes_per_run=2, writes_per_league_per_day=3)
    for _ in range(2):
        p.check_write("a")
        p.record_write("a")
    assert c.slept == [90]                     # gap only between writes
    with pytest.raises(PacingStop, match="this run"):
        p.check_write("a")
    p2, _ = pacer(tmp_path, writes_per_run=5, writes_per_league_per_day=3)
    p2.check_write("a")
    p2.record_write("a")
    with pytest.raises(PacingStop, match="daily"):
        p2.check_write("a")


def test_cooldown_blocks_until_expiry(tmp_path):
    p, c = pacer(tmp_path, cooldown_hours=24)
    p.cooldown("a", "HTTP 429")
    with pytest.raises(PacingStop, match="cooldown"):
        p.check_write("a")
    c.t += 25 * 3600
    p.check_write("a")


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_writer_pushback_sets_cooldown_no_retry(tmp_path, status):
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        return httpx.Response(status, json={"details": [{"type": "X", "message": "no"}]})
    cfg = LeagueConfig("league-a", "cousin", 11111111, 3, 2026, "cousin")
    p, _ = pacer(tmp_path)
    w = TransactionWriter(cfg, Credentials("s2", "sw"), readback=lambda: {}, transport=httpx.MockTransport(handler), pacer=p)
    with pytest.raises(PacingStop, match="pushed back"):
        w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    assert calls["n"] == 1 and p.in_cooldown("league-a")
    with pytest.raises(PacingStop):             # next attempt blocked before any request
        w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    assert calls["n"] == 1


def test_409_does_not_cool_down(tmp_path):
    cfg = LeagueConfig("league-a", "cousin", 11111111, 3, 2026, "cousin")
    p, _ = pacer(tmp_path)
    t = httpx.MockTransport(lambda r: httpx.Response(409, json={"details": [{"type": "TRAN_LINEUP_LOCKED", "message": "x"}]}))
    w = TransactionWriter(cfg, Credentials("s2", "sw"), readback=lambda: {}, transport=t, pacer=p)
    from fantasy_ai.espn_write import WriteRejected
    with pytest.raises(WriteRejected):
        w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    assert not p.in_cooldown("league-a")


def test_reader_fetches_each_view_once_per_run(tmp_path):
    cfg = LeagueConfig("league-a", "cousin", 11111111, 3, 2026, "cousin")
    p, c = pacer(tmp_path)
    r = EspnReader(cfg, Credentials("s2", "sw"), pacer=p)
    calls = []
    r._fetch("league", lambda: calls.append(1) or {"x": 1})
    r._fetch("league", lambda: calls.append(1) or {"x": 1})
    r._fetch("pro", lambda: calls.append(2) or {})
    assert calls == [1, 2] and c.slept == [4]          # one gap, before the 2nd real read
    r._fetch("league", lambda: calls.append(3) or {}, fresh=True)   # read-back after a write is fresh
    assert calls == [1, 2, 3]
