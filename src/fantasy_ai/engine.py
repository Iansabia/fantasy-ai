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


def mark_fill_ins(state: LeagueState) -> list[str]:
    """Flag players whose THIS-week projection is a fill-in role: a same-team, same-position
    teammate with a bigger season projection is out, doubtful or on IR this week. Their weekly
    number then stays out of rest-of-season value (Player.per_game)."""
    everyone = {p.player_id: p for t in state.teams.values() for p in t.roster}
    everyone.update({p.player_id: p for p in state.free_agents})
    by_group: dict[tuple[str, str], list[Player]] = {}
    for p in everyone.values():
        if p.pro_team and p.position in ("QB", "RB", "WR", "TE"):
            by_group.setdefault((p.pro_team, p.position), []).append(p)
    notes = []
    for ps in by_group.values():
        hurt = [q for q in ps if q.is_out() or q.news_out or q.game_status == "DOUBTFUL" or q.ir_eligible()]
        for p in ps:
            if any(q is not p and q.proj_season > p.proj_season for q in hurt):
                p.fill_in_week = True
                notes.append(p.name)
    return notes


IR_MIN_GAMES = 4      # NFL injured reserve: at least 4 games out


def value_ir_stashes(state: LeagueState) -> list[str]:
    """IR / OUT players ESPN projects at 0 for the season get a value from the expert ROS rank:
    the per-game value of the player at that positional rank in our pool, minus the games he
    will miss (at least 4 on injured reserve, 1 if just out). Without ECR they stay at 0."""
    everyone = {p.player_id: p for t in state.teams.values() for p in t.roster}
    everyone.update({p.player_id: p for p in state.free_agents})
    pool: dict[str, list[Player]] = {}
    for p in everyone.values():
        if p.position in ("QB", "RB", "WR", "TE") and p.proj_season > 0:
            pool.setdefault(p.position, []).append(p)
    for ps in pool.values():
        ps.sort(key=lambda q: q.per_game() * q.ros_multiplier, reverse=True)
    notes = []
    for p in everyone.values():
        rank = (getattr(p, "ecr", None) or {}).get("ros")
        if p.proj_season > 0 or not rank or p.position not in pool or not (p.is_out() or p.ir_eligible()):
            continue
        ps = pool[p.position]
        ref = ps[min(rank, len(ps)) - 1]
        p.proj_season = round(ref.per_game() * ref.ros_multiplier * 17, 1)
        p.weeks_out = IR_MIN_GAMES if p.game_status == "INJURY_RESERVE" else 1
        p.reasons.append(f"{p.name}: IR stash valued from expert rank {p.ecr.get('ros_text')} "
                         f"(like {ref.name}), minus {p.weeks_out} game{'s' if p.weeks_out > 1 else ''} out")
        notes.append(p.name)
    return notes


def prepare(state: LeagueState, reader, factor_cfg: dict | None = None, *, news: bool = True,
            factors: bool = True, research: list[Player] | None = None, llm=None, client=None,
            hype_cfg: dict | None = None, schedule_cfg: dict | None = None,
            signals_cfg: dict | None = None) -> dict:
    """Mutates state. Returns a small dict describing which inputs were used (for explain)."""
    info = {"news": False, "news_players": 0, "factor_sources": []}
    sig = signals_cfg or {}
    from . import ownership as _own
    from .news import adjust as _adj
    _adj.BUZZ_ROS_ENABLED = bool(sig.get("buzz_ros", False))
    _own.OWNERSHIP_ROS_ENABLED = bool(sig.get("ownership_ros", False))
    offline = reader.cfg.fixture_dir is not None
    if news and not offline:
        from .llm import LLM
        from .news.pipeline import enrich
        players = research if research is not None else lineup_research_set(state)
        enrich(state, llm or LLM(), client=client, players=players)
        info.update(news=True, news_players=len(players))
    info["fill_ins"] = mark_fill_ins(state)
    from . import ownership
    try:
        ownership.apply(state, ownership.record(state))
        info["ownership"] = True
    except OSError:
        pass
    if not offline:
        from . import market
        from .news.cache import PoliteClient
        rows = market.fetch(client or PoliteClient(), len(state.teams),
                            2 if state.slot_counts.get(7) else 1, getattr(reader.cfg, "ppr", 1.0))
        info["market"] = market.attach(state, rows)
        from . import experts
        try:
            info["experts"] = experts.attach(state, experts.fetch(client or PoliteClient()))
        except Exception:
            info["experts"] = 0
    from . import hype, usage_data
    usage = None if offline else usage_data.load(state.year)
    hype.apply(state, usage, hype_cfg)
    info["hype"] = usage is not None
    from . import trend
    info["trend"] = trend.apply(state, usage)
    info["ir_stashes"] = value_ir_stashes(state)
    from . import gametime
    info["gametime"] = gametime.apply(state, reader.raw_pro_teams(), schedule_cfg)
    if factors:
        ctx = providers.build_context(state, reader.raw_pro_teams(),
                                      reader.raw_positional_ratings(state.scoring_period),
                                      client=client, online=not offline)
        score.apply(state, ctx, factor_cfg)
        info["factor_sources"] = ctx.sources
    return info
