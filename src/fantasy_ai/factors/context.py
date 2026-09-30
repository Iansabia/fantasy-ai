"""Week context: the raw inputs factors read. Every field is optional; a missing input just
means that factor is skipped (with no reason line)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProGame:
    team: str
    opp: str
    home: bool
    kickoff_ms: int
    home_team: str


@dataclass
class WeekContext:
    week: int
    games: dict[str, ProGame] = field(default_factory=dict)              # team abbrev -> game
    future_games: dict[int, dict[str, ProGame]] = field(default_factory=dict)  # week -> team -> game
    def_vs_pos: dict[tuple[str, str], float] = field(default_factory=dict)  # (opp abbrev, pos) -> avg pts allowed
    def_rank: dict[tuple[str, str], int] = field(default_factory=dict)      # (opp abbrev, pos) -> ESPN rank
    vegas: dict[str, tuple[float, float]] = field(default_factory=dict)  # team -> (implied total, spread; + = underdog)
    weather: dict[str, tuple[float, float]] = field(default_factory=dict)  # home team -> (wind mph, precip mm/h)
    usage: dict[str, dict[str, list[float]]] = field(default_factory=dict)  # norm name -> metric -> last weeks
    second_proj: dict[int, float] = field(default_factory=dict)          # player id -> other source's proj
    our_edge: float | None = None                                        # our proj / opp proj - 1
    sources: list[str] = field(default_factory=list)                     # which inputs loaded (for explain)
