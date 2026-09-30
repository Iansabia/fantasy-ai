"""Which hype labels cause good vs bad reorderings? For pairs where hype flips the order
(ours vs ours_nohype), attribute to the label of the player whose multiplier moved, count wins."""
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[2] / "data" / "backtest_values"
snaps = json.loads((HERE / "snap_rows.json").read_text())
for key in ("1|09/16/2026", "2|09/23/2026"):
    rows = snaps[key]
    res = defaultdict(lambda: [0, 0])
    for pos in ("QB", "RB", "WR", "TE"):
        rs = [r for r in rows if r["pos"] == pos]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                x, y = rs[i], rs[j]
                da, db, dt = x["ours"] - y["ours"], x["ours_nohype"] - y["ours_nohype"], x["target"] - y["target"]
                if da * db >= 0 or dt == 0:
                    continue
                # label responsible: the one with multiplier further from 1
                resp = x if abs(x["mult"] - 1) >= abs(y["mult"] - 1) else y
                lab = resp["label"].split(" (")[0]
                res[lab][0 if da * dt > 0 else 1] += 1
    print(key, {k: f"{v[0]}-{v[1]} ({v[0] / (sum(v)):.0%} hype right)" for k, v in res.items()})
    fad = [r for r in rows if r["label"] == "Fading"]
    print("  Fading players:", ", ".join(f"{r['name']} ({r['pos']})" for r in fad[:30]))
