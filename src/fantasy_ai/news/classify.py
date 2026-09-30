"""LLM labels news items as structured signals. JSON only; never decisions.

Output per item (validated strictly, anything malformed is dropped):
  {"item": <index>, "player": <name>, "status_change": out|doubtful|questionable|ir|active|null,
   "expected_to_play": likely|uncertain|unlikely|null,
   "role_change": -1|0|1, "usage_note": str, "sentiment": -1..1, "confidence": 0..1}
source_url / source / published are copied from the item by code, not trusted from the LLM.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .sources import NewsItem

STATUSES = {"out", "doubtful", "questionable", "ir", "active"}
PLAY = {"likely", "uncertain", "unlikely"}
BATCH = 15


@dataclass
class Signal:
    player_id: int | None
    player_name: str
    status_change: str | None
    role_change: int
    usage_note: str
    sentiment: float
    confidence: float
    source: str
    source_url: str
    published: str
    expected_to_play: str | None = None


def _prompt(items: list[NewsItem]) -> str:
    rows = [{"item": i, "player": it.player_name, "source": it.source, "title": it.title,
             "text": it.summary[:500]} for i, it in enumerate(items)]
    return (
        "You label fantasy football news. For each item return one JSON object with keys: "
        "item (int, copy it), player (copy it), status_change (one of out, doubtful, questionable, "
        "ir, active, or null if the item says nothing about game status), expected_to_play (likely "
        "for full practice / expected to play, uncertain for limited or no practice / game-time "
        "decision, unlikely for trending toward sitting, or null if not covered), role_change (-1 lost "
        "role/snaps, 0 none, 1 gained role/snaps), usage_note (under 15 words, facts only), "
        "sentiment (-1 to 1 for this week's fantasy outlook), confidence (0 to 1 that the item "
        "really says this). Do not recommend starts or sits. Reply with ONLY a JSON array.\n\n"
        + json.dumps(rows)
    )


def _clamp(x, lo, hi, default):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


def parse_signals(text: str | None, items: list[NewsItem]) -> list[Signal]:
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    try:
        rows = json.loads(m.group(0) if m else text)
    except (json.JSONDecodeError, AttributeError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        idx = r.get("item")
        if not isinstance(idx, int) or not 0 <= idx < len(items):
            continue
        it = items[idx]
        status = r.get("status_change")
        status = str(status).lower() if status else None
        if status not in STATUSES:
            status = None
        play = r.get("expected_to_play")
        play = str(play).lower() if play else None
        if play not in PLAY:
            play = None
        role = r.get("role_change", 0)
        role = role if role in (-1, 0, 1) else 0
        out.append(Signal(
            player_id=it.player_id, player_name=it.player_name, status_change=status, role_change=role,
            usage_note=str(r.get("usage_note", ""))[:120],
            sentiment=_clamp(r.get("sentiment"), -1, 1, 0.0),
            confidence=_clamp(r.get("confidence"), 0, 1, 0.0),
            source=it.source, source_url=it.url, published=it.published, expected_to_play=play,
        ))
    return out


def classify(items: list[NewsItem], llm) -> list[Signal]:
    """llm: callable(prompt) -> str | None (fantasy_ai.llm.LLM in real runs, a fake in tests)."""
    signals: list[Signal] = []
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        signals += parse_signals(llm(_prompt(chunk)), chunk)
    return signals
