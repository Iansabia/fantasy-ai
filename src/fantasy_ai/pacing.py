"""Anti-ban pacing for every ESPN read and write.

- One atomic ROSTER request per lineup change (the ESPN web app does the same).
- Scheduled runs start after a random 0-10 min jitter.
- Random 20-90 s gaps between leagues and between separate write requests.
- Reads: cached per run by the reader (one fetch per view per league), 1-4 s random gaps.
- Write caps: per league per day (default 6) and per run (default 3). Hitting one stops writes.
- On 401/403/429 or any unexpected non-409 error: no retries, stop that league, and set a
  cooldown (default 24 h) stored in data/pacing.json.
- One browser-like User-Agent; requests are strictly sequential.
Tests inject a fake sleep/clock so nothing really waits.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from pathlib import Path

from .config import ROOT

STATE_PATH = ROOT / "data" / "pacing.json"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
# Headers the fantasy web app sends (jwulff 05 section 2.1; heyitaki WRITE_HEADERS).
ESPN_HEADERS = {"User-Agent": BROWSER_UA, "x-fantasy-source": "kona", "x-fantasy-platform": "kona-PROD",
                "Accept": "application/json"}

DEFAULTS = {"jitter_max_s": 600, "league_gap_s": [20, 90], "write_gap_s": [20, 90], "read_gap_s": [1, 4],
            "writes_per_league_per_day": 6, "writes_per_run": 3, "cooldown_hours": 24}


class PacingStop(Exception):
    """Writes (or the whole league) must stop for now. The message is safe to show."""


class Pacer:
    def __init__(self, cfg: dict | None = None, path: Path | str = STATE_PATH,
                 sleep=time.sleep, rand=random.uniform, clock=time.time):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.path, self.sleep, self.rand, self.clock = Path(path), sleep, rand, clock
        self.run_writes = 0
        self._reads = 0
        self._writes_this_run: dict[str, int] = {}
        self._league_started = False

    # ---- persistent state (write counts per day, cooldowns)
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"writes": {}, "cooldown_until": {}}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=1))

    def _today(self) -> str:
        return dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).strftime("%Y-%m-%d")

    # ---- waits
    def _wait(self, key: str) -> float:
        lo, hi = self.cfg[key]
        s = self.rand(lo, hi)
        self.sleep(s)
        return s

    def start_jitter(self) -> float:
        s = self.rand(0, self.cfg["jitter_max_s"])
        self.sleep(s)
        return s

    def between_leagues(self) -> None:
        if self._league_started:
            self._wait("league_gap_s")
        self._league_started = True

    def before_read(self) -> None:
        if self._reads:
            self._wait("read_gap_s")
        self._reads += 1

    # ---- writes
    def in_cooldown(self, league: str) -> str | None:
        until = self._load()["cooldown_until"].get(league)
        if until and self.clock() < until:
            return dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return None

    def check_write(self, league: str) -> None:
        cd = self.in_cooldown(league)
        if cd:
            raise PacingStop(f"paused after ESPN pushed back; cooldown until {cd}")
        if self.run_writes >= self.cfg["writes_per_run"]:
            raise PacingStop(f"write cap for this run reached ({self.cfg['writes_per_run']})")
        today = self._load()["writes"].get(league, {}).get(self._today(), 0)
        if today >= self.cfg["writes_per_league_per_day"]:
            raise PacingStop(f"daily write cap reached ({self.cfg['writes_per_league_per_day']} per league)")
        if self.run_writes:
            self._wait("write_gap_s")

    def record_write(self, league: str) -> None:
        self.run_writes += 1
        d = self._load()
        day = d["writes"].setdefault(league, {})
        day[self._today()] = day.get(self._today(), 0) + 1
        d["writes"][league] = {k: v for k, v in day.items() if k >= self._today()}   # keep today only
        self._save(d)

    def cooldown(self, league: str, why: str) -> str:
        d = self._load()
        until = self.clock() + self.cfg["cooldown_hours"] * 3600
        d["cooldown_until"][league] = until
        self._save(d)
        return f"ESPN pushed back ({why}), paused for {self.cfg['cooldown_hours']} h"


class NoPacer(Pacer):
    """Offline fixtures and tests: no waits, no caps, no state file."""

    def __init__(self):
        super().__init__(sleep=lambda s: None, rand=lambda a, b: 0.0)

    def _load(self) -> dict:
        return {"writes": {}, "cooldown_until": {}}

    def _save(self, d: dict) -> None:
        pass
