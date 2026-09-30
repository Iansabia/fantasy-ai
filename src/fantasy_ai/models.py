"""Plain data classes shared by the reader, the decision modules and the writer."""
from __future__ import annotations

from dataclasses import dataclass, field

from .slots import HEALTHY_STATUSES, IR, IR_ELIGIBLE_STATUSES, OUT_STATUSES, STATUS_DISCOUNT, slot_name

SEASON_WEEKS = 17  # fantasy regular season + playoffs, used for rest-of-season math


@dataclass
class Player:
    player_id: int
    name: str
    position: str                 # QB, RB, WR, TE, K, D/ST
    pro_team: str = ""
    eligible_slots: list[int] = field(default_factory=list)
    lineup_slot: int = 20         # current ESPN lineupSlotId (20 = bench)
    proj_week: float = 0.0        # ESPN projection for the current scoring period
    proj_season: float = 0.0      # ESPN full-season projection (0 if unknown)
    injury_status: str = "ACTIVE"
    bye_week: int | None = None
    locked: bool = False          # playerPoolEntry.lineupLocked: game started, cannot move
    on_team_id: int = 0
    status: str = "ONTEAM"        # ONTEAM / FREEAGENT / WAIVERS
    percent_owned: float = 0.0
    # set by news/adjust.py; decisions read proj_week / ros_value, so they pick these up
    base_proj_week: float | None = None      # ESPN projection before news adjustment
    ros_multiplier: float = 1.0              # role/sentiment/buzz effect on rest-of-season value
    buzz: float = 0.0                        # 0..1 trending/news volume
    news_out: bool = False                   # confident fresh news says he will not play
    news_status: str | None = None           # status from news; if set, proj_week already includes it
    score_multiplier: float = 1.0            # product of bounded factors (factors/score.py)
    factors: list = field(default_factory=list)   # factors.score.Factor rows with reasons
    reasons: list[str] = field(default_factory=list)   # cited reasons for any adjustment

    @property
    def slot(self) -> str:
        return slot_name(self.lineup_slot)

    def on_bye(self, week: int) -> bool:
        return self.bye_week is not None and self.bye_week == week

    def is_out(self) -> bool:
        return (self.injury_status or "ACTIVE").upper() in OUT_STATUSES

    @property
    def game_status(self) -> str:
        return (self.injury_status or "ACTIVE").upper()

    def is_healthy(self) -> bool:
        return self.game_status in HEALTHY_STATUSES

    def ir_eligible(self) -> bool:
        return self.game_status in IR_ELIGIBLE_STATUSES and IR in (self.eligible_slots or [IR])

    def status_discount(self) -> float:
        """ESPN game-status discount, unless news already priced a status into proj_week."""
        if self.news_status is not None:
            return 1.0
        return STATUS_DISCOUNT.get(self.game_status, 1.0)

    def lineup_value(self, week: int) -> float:
        """Expected points used by the optimizer: 0 if he can't play, discounted if he may not."""
        if not self.available(week):
            return 0.0
        return round(self.proj_week * self.status_discount() * self.score_multiplier, 2)

    def available(self, week: int) -> bool:
        """Can this player score this week?"""
        return not self.on_bye(week) and not self.is_out() and not self.news_out

    def per_game(self) -> float:
        """Season projection spread per game; falls back to this week's (unadjusted) projection."""
        if self.proj_season > 0:
            return self.proj_season / (SEASON_WEEKS - 1)  # 16 games + 1 bye in 17 weeks
        return self.base_proj_week if self.base_proj_week is not None else self.proj_week

    def ros_value(self, current_week: int) -> float:
        """Rest-of-season projected points from the current week through week 17.
        OUT/IR players still carry value, but lose the current week."""
        weeks = max(0, SEASON_WEEKS - current_week + 1)
        if self.bye_week is not None and self.bye_week >= current_week:
            weeks -= 1
        if self.is_out() and weeks > 0:
            weeks -= 1
        return round(self.per_game() * weeks * self.ros_multiplier, 1)

    @property
    def on_ir_slot(self) -> bool:
        return self.lineup_slot == IR


@dataclass
class Team:
    team_id: int
    name: str
    roster: list[Player] = field(default_factory=list)


@dataclass
class LeagueState:
    league_id: int
    year: int
    my_team_id: int
    scoring_period: int                       # current ESPN scoringPeriodId (writes must use this)
    matchup_period: int
    slot_counts: dict[int, int]               # lineupSlotId -> count (settings.rosterSettings.lineupSlotCounts)
    teams: dict[int, Team] = field(default_factory=dict)
    free_agents: list[Player] = field(default_factory=list)
    matchup: dict | None = None
    transactions: list[dict] = field(default_factory=list)
    league_name: str = ""

    @property
    def my_team(self) -> Team:
        return self.teams[self.my_team_id]

    @property
    def roster(self) -> list[Player]:
        return self.my_team.roster

    def find_player(self, name_or_id: str) -> Player | None:
        """Look up any rostered player (all teams) by id or case-insensitive name."""
        everyone = [p for t in self.teams.values() for p in t.roster] + self.free_agents
        key = str(name_or_id).strip().lower()
        for p in everyone:
            if str(p.player_id) == key or p.name.lower() == key:
                return p
        matches = [p for p in everyone if key in p.name.lower()]
        return matches[0] if len(matches) == 1 else None
