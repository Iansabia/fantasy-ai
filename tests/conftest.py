import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import builders as b  # noqa: E402

from fantasy_ai.config import LeagueConfig  # noqa: E402
from fantasy_ai.espn_read import parse_league  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real socket connection in a test is a bug."""
    import socket

    def guard(*a, **k):
        raise RuntimeError("network access attempted in a test")
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def demo_state():
    return parse_league(b.demo_league(), b.pro_teams(), 1, raw_fa=b.demo_free_agents(),
                        raw_tx=b.demo_transactions())


@pytest.fixture
def cfg(tmp_path):
    return LeagueConfig(name="league-a", owner="cousin", league_id=11111111, team_id=3,
                        year=2026, auth_profile="cousin")


def make_state(entries, slots=None, fas=None, team_id=1, week=4, extra_teams=()):
    teams = [(team_id, "Mine", entries), *extra_teams]
    return parse_league(b.league(teams, slots or b.BE7_SLOTS, week=week), b.pro_teams(), team_id,
                        raw_fa={"players": fas or []})
