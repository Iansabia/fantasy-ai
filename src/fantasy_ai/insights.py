"""Turn decisions into a facts dict and a short weekly note.

The template note is built only from the facts dict. The optional LLM rewrite (`claude -p`)
gets the facts as JSON and must not add numbers: every number in its output has to already
appear in the facts, or we fall back to the template text.
"""
from __future__ import annotations

import json
import re

from .lineup import LineupResult
from .models import LeagueState
from .waivers import WaiverSuggestion


def build_facts(label: str, state: LeagueState, lineup: LineupResult,
                waivers: list[WaiverSuggestion] | None = None) -> dict:
    week = state.scoring_period
    out_players = [f"{p.name} ({p.injury_status})" for p in state.roster if p.is_out()]
    bye_players = [p.name for p in state.roster if p.on_bye(week)]
    return {
        "league": label,
        "team": state.my_team.name,
        "week": week,
        "matchup": state.matchup,
        "lineup_moves": [m.reason or str(m) for m in lineup.moves + lineup.ir_moves],
        "move_count": len(lineup.moves) + len(lineup.ir_moves),
        "projected_before": lineup.before_points,
        "projected_after": lineup.after_points,
        "projected_gain": lineup.gain,
        "lineup_warnings": lineup.warnings,
        "injured_out": out_players,
        "on_bye": bye_players,
        "waiver_ideas": [
            {"add": w.add.name, "drop": w.drop.name if w.drop else None, "gain": w.gain, "kind": w.kind, "reason": w.reason}
            for w in (waivers or [])
        ],
        "recent_transactions": [
            f"{t['team']}: {t['type']} {t['status']}" for t in state.transactions
            if not str(t.get("status", "")).startswith("FAILED")][:5],
    }


def template_note(f: dict) -> str:
    lines = [f"{f['league']} ({f['team']}), week {f['week']}"]
    m = f.get("matchup")
    if m:
        lines.append(f"Matchup vs {m['opp_name']}: {m['my_points']} to {m['opp_points']} so far.")
    if f["lineup_moves"]:
        lines.append(f"Lineup: {f['move_count']} change(s), projected {f['projected_before']} -> "
                     f"{f['projected_after']} ({f['projected_gain']:+.2f}).")
        lines += [f"  - {mv}" for mv in f["lineup_moves"]]
    else:
        lines.append(f"Lineup is already optimal at {f['projected_after']} projected points.")
    if f["injured_out"]:
        lines.append("Out: " + ", ".join(f["injured_out"]) + ".")
    if f["on_bye"]:
        lines.append("On bye: " + ", ".join(f["on_bye"]) + ".")
    for w in f["lineup_warnings"]:
        lines.append(f"Note: {w}")
    if f["waiver_ideas"]:
        lines.append("Waiver ideas (need your OK):")
        lines += [f"  - {w['reason']}" for w in f["waiver_ideas"][:3]]
    if f["recent_transactions"]:
        lines.append("League activity: " + "; ".join(f["recent_transactions"]) + ".")
    return "\n".join(lines)


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.lstrip("-").rstrip("0").rstrip(".") or "0" for n in _NUM.findall(text)}


def numbers_are_grounded(text: str, facts: dict) -> bool:
    allowed = _numbers(json.dumps(facts))
    return _numbers(text) <= allowed


def llm_note(facts: dict, llm=None) -> tuple[str, bool]:
    """Rewrite the note with an LLM. Returns (text, used_llm). Falls back to the template
    if no LLM is available or the text contains any number not in the facts."""
    from .llm import LLM
    fallback = template_note(facts)
    llm = llm or LLM()
    prompt = (
        "Write a short, friendly weekly fantasy football note (under 120 words, plain text, "
        "no em dashes, no markdown). Use ONLY these facts. Do not add, change, or compute any "
        "number; only repeat numbers exactly as given. Decisions are already made; just explain them.\n\n"
        f"FACTS:\n{json.dumps(facts, indent=1)}"
    )
    text = llm(prompt)
    if not text or not numbers_are_grounded(text, facts):
        return fallback, False
    return text, True
