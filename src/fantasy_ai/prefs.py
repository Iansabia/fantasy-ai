"""Consumes the files chat.py writes (data/preferences.json, requests.json) and writes
data/snapshot.json for chat answers.

preferences.json, per league label:
  start_this_week / bench_this_week  only while override_week == current week
  always_start   start whenever he can play, even over a higher score
  never_start    never start (a this-week start request overrides it for that week)
  never_drop     never suggested as a drop or as a trade piece
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT


def data_dir() -> Path:
    return Path(os.environ.get("FAI_DATA_DIR") or ROOT / "data")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(".", "").split())


@dataclass
class Prefs:
    force_start: set[str] = field(default_factory=set)   # normalized names
    force_bench: set[str] = field(default_factory=set)
    never_drop: set[str] = field(default_factory=set)
    asked_this_week: set[str] = field(default_factory=set)   # start/bench_this_week (for wording)

    def starts(self, name: str) -> bool:
        return _norm(name) in self.force_start

    def benches(self, name: str) -> bool:
        return _norm(name) in self.force_bench

    def keeps(self, name: str) -> bool:
        return _norm(name) in self.never_drop


def _league_block(prefs: dict, *labels: str) -> dict:
    keys = {k.lower(): k for k in prefs}
    for lab in labels:
        if lab and lab.lower() in keys:
            return prefs[keys[lab.lower()]]
    return {}


def load_prefs(week: int, *labels: str, directory: Path | None = None) -> Prefs:
    block = _league_block(_load((directory or data_dir()) / "preferences.json", {}), *labels)
    this_week = block.get("override_week") == week
    start_wk = {_norm(n) for n in block.get("start_this_week", [])} if this_week else set()
    bench_wk = {_norm(n) for n in block.get("bench_this_week", [])} if this_week else set()
    always = {_norm(n) for n in block.get("always_start", [])}
    never = {_norm(n) for n in block.get("never_start", [])}
    return Prefs(
        force_start=(start_wk | (always - bench_wk)),
        force_bench=(bench_wk | (never - start_wk)),
        never_drop={_norm(n) for n in block.get("never_drop", [])},
        asked_this_week=start_wk | bench_wk,
    )


# ---------------------------------------------------------------- snapshot for chat

def write_snapshot(label: str, state, res=None, directory: Path | None = None) -> None:
    from .factors.score import top_reasons
    path = (directory or data_dir()) / "snapshot.json"
    snap = _load(path, {})
    wk = state.scoring_period
    roster = []
    for p in state.roster:
        roster.append({
            "name": p.name, "pos": p.position, "slot": p.slot,
            "proj": p.base_proj_week if p.base_proj_week is not None else p.proj_week,
            "adj": p.lineup_value(wk), "status": p.injury_status,
            "reasons": list(p.reasons) + top_reasons(p, 3),
        })
    entry = {"week": wk, "roster": roster, "updated": dt.datetime.now(dt.timezone.utc).isoformat()}
    if res is not None:
        entry["last_moves"] = [m.reason or str(m) for m in res.moves + res.ir_moves]
        entry["warnings"] = list(res.warnings)
    snap.setdefault("leagues", {})[label] = entry
    _save(path, snap)


# ---------------------------------------------------------------- requests (trade ideas)

def take_requests(kind: str, *labels: str, directory: Path | None = None) -> list[dict]:
    """Pop the requests of `kind` for this league from requests.json (others are kept)."""
    path = (directory or data_dir()) / "requests.json"
    reqs = _load(path, [])
    wanted = {l.lower() for l in labels if l}
    mine = [r for r in reqs if r.get("type") == kind and str(r.get("league") or "").lower() in wanted]
    if mine:
        _save(path, [r for r in reqs if r not in mine])
    return mine


def trade_ideas(state, player, prefs: Prefs, limit: int = 2):
    """1-for-1 trade options involving `player`. Returns [(give, get, TradeEval)] best first.
    Only options that help our starting lineup and do not gut the partner's are kept."""
    from .trades import FAIR_BAND, evaluate_trade
    mine = {p.player_id for p in state.roster}
    options = []
    if player.player_id in mine:
        if prefs.keeps(player.name):
            return []
        for t in state.teams.values():
            if t.team_id == state.my_team_id:
                continue
            for q in t.roster:
                if q.on_ir_slot:
                    continue
                ev = evaluate_trade(state, [player], [q])
                if ev.me.lineup_delta > FAIR_BAND and ev.partner.lineup_delta >= -FAIR_BAND:
                    options.append(([player], [q], ev))
    else:
        for g in state.roster:
            if g.on_ir_slot or prefs.keeps(g.name):
                continue
            ev = evaluate_trade(state, [g], [player])
            if ev.me.lineup_delta > FAIR_BAND and ev.partner.lineup_delta >= -FAIR_BAND:
                options.append(([g], [player], ev))
    options.sort(key=lambda o: o[2].me.lineup_delta, reverse=True)
    return options[:limit]
