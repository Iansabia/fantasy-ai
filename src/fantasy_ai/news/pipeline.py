"""Glue: fetch news + buzz for the players that matter, classify, apply adjustments."""
from __future__ import annotations

from ..models import LeagueState
from . import adjust
from .cache import PoliteClient
from .classify import classify
from .sources import SleeperTrending, default_sources

MAX_FA = 20   # research the top free agents too, so waivers see buzz


def players_to_research(state: LeagueState) -> list:
    fas = sorted(state.free_agents, key=lambda p: p.percent_owned, reverse=True)[:MAX_FA]
    return list(state.roster) + fas


def enrich(state: LeagueState, llm, client: PoliteClient | None = None, sources=None,
           trending: SleeperTrending | None = None, players: list | None = None) -> dict[int, adjust.Adjustment]:
    """Mutates state in place. Returns the adjustments (with reasons) by player id.
    `players` limits which players get per-player news (default: roster + top free agents)."""
    client = client or PoliteClient()
    players = players if players is not None else players_to_research(state)
    items = []
    for src in (sources if sources is not None else default_sources(client)):
        try:
            items += src.fetch(players)
        except Exception:
            continue   # one broken feed never stops a run
    signals = classify(items, llm) if items else []
    everyone = [p for t in state.teams.values() for p in t.roster] + state.free_agents
    buzz = (trending or SleeperTrending(client)).buzz(everyone)
    return adjust.apply(state, signals, buzz)
