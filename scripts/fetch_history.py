"""Fetch past-season weekly projections for the multi-source backtest (read only, cached).

ESPN: JVille's league (exists since 2022) for seasons 2023-2025, view kona_player_info per week;
      keeps each player's weekly projection (statSourceId 1) and actual (statSourceId 0).
Sleeper: public projections endpoint per season/week (PPR points only kept).
Output: data/backtest_sources/espn_<yr>.json, sleeper_<yr>.json
"""
import dataclasses
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fantasy_ai.config import load_config, load_credentials  # noqa: E402
from fantasy_ai.espn_read import EspnReader  # noqa: E402
from fantasy_ai.pacing import Pacer  # noqa: E402

OUT = ROOT / "data" / "backtest_sources"
OUT.mkdir(parents=True, exist_ok=True)
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}
WEEKS = range(1, 18)


def espn(yr: int):
    path = OUT / f"espn_{yr}.json"
    if path.exists():
        return
    cfg = load_config(None)
    lg = dataclasses.replace(cfg.get("jville"), year=yr)
    r = EspnReader(lg, load_credentials(lg.auth_profile), pacer=Pacer(cfg.pacing))
    out = {}
    for wk in WEEKS:
        flt = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                           "filterSlotIds": {"value": [0, 2, 4, 6]}, "limit": 400,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        raw = r._fetch(f"w{wk}", lambda: r._request().league_get(
            params={"view": "kona_player_info", "scoringPeriodId": wk},
            headers=r._headers({"x-fantasy-filter": json.dumps(flt)})))
        rows = []
        for x in raw.get("players", []):
            pl = x["player"]
            pos = POS.get(pl.get("defaultPositionId"))
            if not pos:
                continue
            proj = act = None
            for s in pl.get("stats", []):
                if s.get("seasonId") == yr and s.get("statSplitTypeId") == 1 and s.get("scoringPeriodId") == wk:
                    if s.get("statSourceId") == 1:
                        proj = float(s.get("appliedTotal") or 0)
                    elif s.get("statSourceId") == 0:
                        act = float(s.get("appliedTotal") or 0)
            rows.append({"name": pl["fullName"], "pos": pos, "proj": proj, "act": act})
        out[wk] = rows
        print(yr, wk, len(rows), flush=True)
    path.write_text(json.dumps(out))


def sleeper(yr: int):
    path = OUT / f"sleeper_{yr}.json"
    if path.exists():
        return
    out = {}
    with httpx.Client(timeout=30, headers={"User-Agent": "fantasy-ai/0.1 (personal, low volume)"}) as c:
        for wk in WEEKS:
            r = c.get(f"https://api.sleeper.app/projections/nfl/{yr}/{wk}",
                      params={"season_type": "regular", "position[]": ["QB", "RB", "WR", "TE"]})
            r.raise_for_status()
            out[wk] = [{"name": f"{x['player'].get('first_name', '')} {x['player'].get('last_name', '')}",
                        "pos": x["player"].get("position"), "team": x.get("team"),
                        "pts": (x.get("stats") or {}).get("pts_ppr")} for x in r.json()
                       if (x.get("stats") or {}).get("pts_ppr") is not None]
            print("sleeper", yr, wk, len(out[wk]), flush=True)
            time.sleep(2)
    path.write_text(json.dumps(out))


if __name__ == "__main__":
    for yr in (2023, 2024, 2025):
        sleeper(yr)
        espn(yr)
