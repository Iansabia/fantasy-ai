"""Factor-based player score. ESPN projection is the base; bounded multipliers for matchup,
home/away, Vegas, game script, usage trend, weather, floor/ceiling and a second projection
source. News/status come from fantasy_ai.news and are already priced into proj_week.
Every factor has a weight and cap (config `factors:`) and a one-line reason."""
