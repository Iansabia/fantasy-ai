"""Run schedule, run log, heartbeat and missed-run detection.

Schedule (Eastern time, all leagues):
  Thursday 2:00pm   full run: news, factors, Vegas, IR, waiver ideas, texts
  Saturday 2:00pm   recheck: lineups + IR only, fresh news/injuries, only change what changed
  Sunday 11:45am    inactive check (optional, OFF unless schedule.sunday_inactive_check: true)
NFL week window = Tuesday 00:00 ET to the next Tuesday. If this week's Thursday run is missing
(laptop asleep, CI outage) and Sunday's 1:00pm ET kickoff has not passed, any autopilot run
upgrades itself to a full run.

data/runs.json: {league: {"full": iso, "recheck": iso, "inactives": iso, "last": {...}}}
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .prefs import _load, _save, data_dir

ET = ZoneInfo("America/New_York")
SLOTS = [("full", 3, 14, 0), ("recheck", 5, 14, 0), ("inactives", 6, 11, 45)]   # (mode, weekday Mon=0, h, m)


def _week_start(now: dt.datetime) -> dt.datetime:
    now = now.astimezone(ET)
    days_back = (now.weekday() - 1) % 7            # Tuesday = 1
    start = (now - dt.timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def slot_time(now: dt.datetime, weekday: int, hour: int, minute: int) -> dt.datetime:
    start = _week_start(now)
    offset = (weekday - 1) % 7
    return (start + dt.timedelta(days=offset)).replace(hour=hour, minute=minute)


def kickoff(now: dt.datetime) -> dt.datetime:
    return slot_time(now, 6, 13, 0)                 # Sunday 1:00pm ET


def last_slot(now: dt.datetime, sunday_enabled: bool = False) -> tuple[str, dt.datetime] | None:
    """Most recent scheduled slot at or before now in this NFL week."""
    best = None
    for mode, wd, h, m in SLOTS:
        if mode == "inactives" and not sunday_enabled:
            continue
        t = slot_time(now, wd, h, m)
        if t <= now.astimezone(ET) and (best is None or t > best[1]):
            best = (mode, t)
    return best


def runs(directory=None) -> dict:
    return _load((directory or data_dir()) / "runs.json", {})


def record(league: str, mode: str, summary: dict, now: dt.datetime | None = None, directory=None) -> None:
    path = (directory or data_dir()) / "runs.json"
    data = _load(path, {})
    now = now or dt.datetime.now(dt.timezone.utc)
    entry = data.setdefault(league, {})
    entry[mode] = now.isoformat()
    entry["last"] = {"mode": mode, "at": now.isoformat(), **summary}
    _save(path, data)


def _ran_since(entry: dict, modes: tuple[str, ...], since: dt.datetime) -> bool:
    for m in modes:
        t = entry.get(m)
        if t and dt.datetime.fromisoformat(t) >= since:
            return True
    return False


def missed_full(league: str, now: dt.datetime, data: dict | None = None) -> bool:
    """This week's Thursday full run is due, did not happen, and kickoff has not passed."""
    data = runs() if data is None else data
    thu = slot_time(now, 3, 14, 0)
    if not (thu <= now.astimezone(ET) < kickoff(now)):
        return False
    return not _ran_since(data.get(league, {}), ("full",), thu)


def status_lines(leagues: list[str], now: dt.datetime, sunday_enabled: bool = False, data: dict | None = None) -> list[str]:
    data = runs() if data is None else data
    out = []
    slot = last_slot(now, sunday_enabled)
    for lg in leagues:
        e = data.get(lg, {})
        last = e.get("last")
        when = dt.datetime.fromisoformat(last["at"]).astimezone(ET).strftime("%a %b %d %I:%M%p ET") if last else "never"
        line = f"{lg}: last run {when}" + (f" ({last['mode']}, {last.get('moves', 0)} moves)" if last else "")
        if slot:
            mode, t = slot
            ok = _ran_since(e, ("full",) if mode == "full" else ("full", mode), t)
            if not ok:
                line += f" | WARNING: missed the {mode} run due {t.strftime('%a %I:%M%p ET')}"
        if missed_full(lg, now, data):
            line += " | Thursday run missing and kickoff not passed: next autopilot runs a full catch-up"
        out.append(line)
    return out


def heartbeat(label: str, mode: str, moves: int, queued: int, sent: bool, error: str | None = None) -> str:
    name = {"full": "Thursday run", "recheck": "Saturday recheck", "inactives": "Sunday inactive check",
            "waivers": "Waiver check"}.get(mode, mode)
    if error:
        return f"⚠️ {label} | {name}\n\n• problem: {error}"
    if moves == 0 and mode != "full":
        body = "recheck: no changes"
    else:
        body = (f"{moves} lineup move{'s' if moves != 1 else ''} {'set' if sent else 'planned (dry run)'}"
                + (f", {queued} waiting for your OK" if queued else ""))
    return f"✅ {label} | {name}\n\n• {body}"
