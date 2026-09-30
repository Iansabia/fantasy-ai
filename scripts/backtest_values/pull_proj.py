import json, sys
from pathlib import Path
from fantasy_ai.config import load_config, load_credentials
from fantasy_ai.espn_read import EspnReader
from fantasy_ai.pacing import Pacer
OUT = Path(sys.argv[1])
cfg = load_config(None); lg = cfg.get("friendly")
reader = EspnReader(lg, load_credentials(lg.auth_profile), pacer=Pacer(cfg.pacing))
year = lg.year
res = {}
for wk in (1, 2, 3, 4):
    filters = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
        "filterSlotIds": {"value": [0, 2, 4, 6, 23]},
        "limit": 450,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
    }}
    raw = reader._fetch(f"p{wk}", lambda: reader._request().league_get(
        params={"view": "kona_player_info", "scoringPeriodId": wk},
        headers=reader._headers({"x-fantasy-filter": json.dumps(filters)})))
    res[wk] = raw
    print(wk, len(raw.get("players", [])))
OUT.write_text(json.dumps(res))
