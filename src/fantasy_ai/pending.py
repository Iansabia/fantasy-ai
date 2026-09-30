"""Approval queue. Trades and waiver/add-drop moves are never sent directly.

Flow: a command builds the payload, `queue()` stores it in data/pending.json with a short id,
an expiry and a list of preconditions. Nothing reaches ESPN until `fai approve <id>`, which
re-reads the league, re-checks every precondition, and aborts if anything changed.

Trade accept/counter is intentionally not implemented: approval-only, and the accept request
shape has never been captured (RESEARCH.md).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ROOT
from .models import LeagueState

DEFAULT_STORE = ROOT / "data" / "pending.json"
EXPIRY_HOURS = {"WAIVER": 24, "FREEAGENT": 12, "TRADE_PROPOSAL": 48}


@dataclass
class PendingAction:
    id: str
    league: str
    kind: str                 # WAIVER / FREEAGENT / TRADE_PROPOSAL
    summary: str
    payload: dict
    created: str
    expires: str
    checks: dict = field(default_factory=dict)
    status: str = "pending"   # pending / approved / rejected / expired / aborted / failed

    def expired(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        return now >= dt.datetime.fromisoformat(self.expires)


class PendingStore:
    def __init__(self, path: Path | str = DEFAULT_STORE):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "actions": []}
        return json.loads(self.path.read_text())

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def all(self) -> list[PendingAction]:
        return [PendingAction(**a) for a in self._load()["actions"]]

    def open_actions(self) -> list[PendingAction]:
        return [a for a in self.all() if a.status == "pending" and not a.expired()]

    def get(self, action_id: str) -> PendingAction:
        for a in self.all():
            if a.id == str(action_id):
                return a
        raise KeyError(f"No pending action #{action_id}")

    def add(self, league: str, kind: str, summary: str, payload: dict, checks: dict,
            now: dt.datetime | None = None) -> PendingAction:
        data = self._load()
        now = now or dt.datetime.now(dt.timezone.utc)
        a = PendingAction(
            id=str(data["next_id"]), league=league, kind=kind, summary=summary, payload=payload,
            created=now.isoformat(), expires=(now + dt.timedelta(hours=EXPIRY_HOURS.get(kind, 24))).isoformat(),
            checks=checks,
        )
        data["next_id"] += 1
        data["actions"].append(asdict(a))
        self._save(data)
        return a

    def set_status(self, action_id: str, status: str) -> None:
        data = self._load()
        for a in data["actions"]:
            if a["id"] == str(action_id):
                a["status"] = status
        self._save(data)


# ---------------------------------------------------------------- preconditions

def checks_for_add_drop(add_id: int | None, drop_id: int | None) -> dict:
    return {"unrostered": [add_id] if add_id else [], "mine": [drop_id] if drop_id else [],
            "unlocked": [drop_id] if drop_id else []}


def checks_for_trade(give_ids: list[int], get_ids: list[int], partner_team_id: int) -> dict:
    return {"mine": list(give_ids), "partner": {str(partner_team_id): list(get_ids)}}


def verify(checks: dict, state: LeagueState) -> list[str]:
    """Return a list of problems (empty = still valid). Uses a fresh LeagueState."""
    problems = []
    mine = {p.player_id: p for p in state.roster}
    rostered = {p.player_id: t.team_id for t in state.teams.values() for p in t.roster}
    for pid in checks.get("mine", []):
        if pid not in mine:
            problems.append(f"player {pid} is no longer on your roster")
    for pid in checks.get("unlocked", []):
        if pid in mine and mine[pid].locked:
            problems.append(f"{mine[pid].name} is locked (game started)")
    for pid in checks.get("unrostered", []):
        if pid in rostered:
            problems.append(f"player {pid} was already picked up by team {rostered[pid]}")
    for team_id, ids in checks.get("partner", {}).items():
        for pid in ids:
            if rostered.get(pid) != int(team_id):
                problems.append(f"player {pid} is no longer on team {team_id}")
    return problems
