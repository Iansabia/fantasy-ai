"""Regenerate tests/fixtures/demo/*.json:  .venv/bin/python tests/fixtures/build_demo.py"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import builders as b  # noqa: E402

out = HERE / "demo"
out.mkdir(exist_ok=True)
for name, data in {"league.json": b.demo_league(), "pro_teams.json": b.pro_teams(),
                   "free_agents.json": b.demo_free_agents(), "transactions.json": b.demo_transactions()}.items():
    (out / name).write_text(json.dumps(data, indent=1))
print("wrote", out)
