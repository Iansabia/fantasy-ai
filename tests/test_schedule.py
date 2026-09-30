"""Thursday full / Saturday recheck / optional Sunday inactives, heartbeat, missed-run catch-up."""
import datetime as dt

from fantasy_ai import cli, schedule

ET = schedule.ET


def et(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ET)


def test_slots_for_the_week():
    now = et(2026, 10, 2, 9)                     # Friday
    assert schedule.slot_time(now, 3, 14, 0) == et(2026, 10, 1, 14)
    assert schedule.kickoff(now) == et(2026, 10, 4, 13)
    assert schedule.last_slot(et(2026, 10, 3, 15)) == ("recheck", et(2026, 10, 3, 14))
    assert schedule.last_slot(et(2026, 10, 4, 12)) == ("recheck", et(2026, 10, 3, 14))
    assert schedule.last_slot(et(2026, 10, 4, 12), sunday_enabled=True) == ("inactives", et(2026, 10, 4, 11, 45))
    assert schedule.last_slot(et(2026, 9, 30, 12)) is None      # Wednesday: nothing yet this NFL week


def test_missed_thursday_detection():
    assert schedule.missed_full("L", et(2026, 10, 2, 10), {}) is True
    ran = {"L": {"full": et(2026, 10, 1, 14, 10).isoformat()}}
    assert schedule.missed_full("L", et(2026, 10, 2, 10), ran) is False
    assert schedule.missed_full("L", et(2026, 10, 4, 14), {}) is False      # kickoff passed
    assert schedule.missed_full("L", et(2026, 10, 1, 13), {}) is False      # not due yet
    last_week = {"L": {"full": et(2026, 9, 24, 14).isoformat()}}
    assert schedule.missed_full("L", et(2026, 10, 3, 9), last_week) is True


def test_status_warns_on_missed_run(isolated_data_dir):
    schedule.record("L", "full", {"moves": 2}, now=et(2026, 10, 1, 14, 5))
    lines = schedule.status_lines(["L", "M"], et(2026, 10, 3, 16))
    assert "missed the recheck run" in lines[0]
    assert "never" in lines[1] and "WARNING" in lines[1]


def test_heartbeat_texts():
    assert "recheck: no changes" in schedule.heartbeat("L", "recheck", 0, 0, False)
    t = schedule.heartbeat("L", "full", 2, 1, True)
    assert t.startswith("✅ L | Thursday run") and "2 lineup moves set, 1 waiting for your OK" in t
    assert "problem" in schedule.heartbeat("L", "full", 0, 0, False, "HTTPError")


def test_autopilot_recheck_heartbeat_and_run_log(tmp_path, capsys, monkeypatch, isolated_data_dir):
    monkeypatch.setattr(schedule, "missed_full", lambda *a, **k: False)
    rc = cli.main(["--config", "config/leagues.example.yaml", "--store", str(tmp_path / "p.json"),
                   "autopilot", "--demo", "--no-news", "--mode", "recheck", "--notify"])
    out = capsys.readouterr().out
    assert rc == 0 and "demo | Saturday recheck" in out and "Waiver" not in out
    assert schedule.runs()["demo"]["last"]["mode"] == "recheck"


def test_autopilot_upgrades_to_full_when_thursday_missed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(schedule, "missed_full", lambda *a, **k: True)
    cli.main(["--config", "config/leagues.example.yaml", "--store", str(tmp_path / "p.json"),
              "autopilot", "--demo", "--no-news", "--mode", "recheck"])
    assert "missed Thursday run, running full catch-up" in capsys.readouterr().out


def test_inactive_check_disabled_by_default(capsys):
    assert cli.main(["--config", "config/leagues.example.yaml", "autopilot", "--demo", "--mode", "inactives"]) == 0
    assert "disabled" in capsys.readouterr().out


def test_catchup_does_nothing_when_nothing_missed(capsys, monkeypatch):
    monkeypatch.setattr(schedule, "missed_full", lambda *a, **k: False)
    assert cli.main(["--config", "config/leagues.example.yaml", "autopilot", "--demo", "--mode", "catchup"]) == 0
    assert "nothing missed" in capsys.readouterr().out
