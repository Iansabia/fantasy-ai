"""Run the evidence layers before any decision: news signals, then factors.

Used by `fai lineup`, `waivers`, `trade`, `explain`, `report` and `autopilot`, so every path
decides on the same numbers. Offline fixture leagues skip network layers (factors still use
fixture positional ratings if present).
"""
from __future__ import annotations

from .factors import providers, score
from .models import LeagueState, Player
from .slots import NON_STARTING

CLOSE_BENCH = 0.65   # research bench players projecting at least 65% of a comparable starter


def lineup_research_set(state: LeagueState) -> list[Player]:
    """Starters, anyone with an injury tag, and bench players close to a starter they could replace."""
    starters = [p for p in state.roster if p.lineup_slot not in NON_STARTING]
    out = {p.player_id: p for p in starters}
    for p in state.roster:
        if not p.is_healthy():
            out[p.player_id] = p
            continue
        if p.lineup_slot in NON_STARTING:
            rivals = [s for s in starters if set(s.eligible_slots) & set(p.eligible_slots) - NON_STARTING]
            if rivals and p.proj_week >= CLOSE_BENCH * min(r.proj_week for r in rivals):
                out[p.player_id] = p
    return list(out.values())


def prepare(state: LeagueState, reader, factor_cfg: dict | None = None, *, news: bool = True,
            factors: bool = True, research: list[Player] | None = None, llm=None, client=None,
            hype_cfg: dict | None = None) -> dict:
    """Mutates state. Returns a small dict describing which inputs were used (for explain)."""
    info = {"news": False, "news_players": 0, "factor_sources": []}
    offline = reader.cfg.fixture_dir is not None
    if news and not offline:
        from .llm import LLM
        from .news.pipeline import enrich
        players = research if research is not None else lineup_research_set(state)
        enrich(state, llm or LLM(), client=client, players=players)
        info.update(news=True, news_players=len(players))
    from . import ownership
    try:
        ownership.apply(state, ownership.record(state))
        info["ownership"] = True
    except OSError:
        pass
    from . import hype, usage_data
    usage = None if offline else usage_data.load(state.year)
    hype.apply(state, usage, hype_cfg)
    info["hype"] = usage is not None
    if factors:
        ctx = providers.build_context(state, reader.raw_pro_teams(),
                                      reader.raw_positional_ratings(state.scoring_period),
                                      client=client, online=not offline)
        score.apply(state, ctx, factor_cfg)
        info["factor_sources"] = ctx.sources
    return info
