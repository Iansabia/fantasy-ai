One-off backtest (2026 weeks 1-3): our ROS value vs FantasyCalc vs ESPN projections.
Data lives in data/backtest_values/ (gitignored). Run from repo root:
  .venv/bin/python scripts/backtest_values/pull_proj.py data/backtest_values/espn_weekly.json   (ESPN reads, weeks 1-4)
  (cd data/backtest_values && python3 ../../scripts/backtest_values/fc_hist.py)             (FC /trades/historical/<id>, ~1 req/s)
  .venv/bin/python scripts/backtest_values/backtest.py
  .venv/bin/python scripts/backtest_values/labels.py
FC history endpoint: https://api.fantasycalc.com/trades/historical/<fcId>?isDynasty=false&numQbs=1
(daily generic redraft value, not the 10-team league-adjusted value).
