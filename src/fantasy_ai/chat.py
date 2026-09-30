"""Understands free-text replies ("yeah do the Puka trade", "start Mitchell this week",
"never drop Bijan", "why'd you bench Pittman?") instead of only 'approve 4'.

Same rule as the rest of the bot: the LLM only LABELS the message (JSON intent). Plain code
decides what happens, and anything that changes ESPN still goes through the approve path
(fresh read + re-check). When the LLM isn't sure, or no action id was named, the bot asks
"Approve #4 (...)? Reply yes or no" and waits instead of guessing.

Files (all under data/, gitignored):
  chat_state.json   what the bot is waiting on a yes/no for, per sender
  preferences.json  standing rules + this-week overrides the lineup optimizer honors
  requests.json     things to look into on the next run (e.g. trade ideas for a player)
  snapshot.json     latest rosters written by lineup/autopilot runs (context for answers)
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from pathlib import Path

from .config import ROOT
from .pending import PendingStore

DATA = ROOT / "data"
AWAIT_HOURS = 2
ACT_CONFIDENCE = 0.85

INTENTS = {"approve", "reject", "yes", "no", "question", "start", "bench",
           "always_start", "never_drop", "never_start", "clear_rule", "trade_idea", "other"}


# ---------------------------------------------------------------- small json store

def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- understanding

def build_context(store: PendingStore, data_dir: Path = DATA) -> dict:
    snap = _load(data_dir / "snapshot.json", {})
    return {
        "pending": [{"id": a.id, "kind": a.kind, "league": a.league, "summary": a.summary}
                    for a in store.open_actions()],
        "leagues": snap.get("leagues", {}),   # label -> {"week": n, "roster": [{"name","pos","slot","proj","status"}]}
    }


def _prompt(text: str, ctx: dict, awaiting: dict | None) -> str:
    rosters = {lg: [p["name"] for p in v.get("roster", [])] for lg, v in ctx["leagues"].items()}
    return (
        "You label a text message sent to a fantasy football bot by the team owner. "
        "Output ONLY one JSON object, no prose.\n"
        'Schema: {"intent": one of ' + json.dumps(sorted(INTENTS)) + ', "action_id": string or null, '
        '"player": string or null, "league": string or null, "confidence": 0..1}\n'
        "Meanings: approve/reject = a pending action; yes/no = answering the bot's last question; "
        "start/bench = this week only; always_start/never_drop/never_start = standing rules; "
        "clear_rule = remove a rule for a player; trade_idea = wants trade options involving a player; "
        "question = asking why/what/who; other = chit-chat or unclear.\n"
        "Use exact player names from the rosters when possible. Do not invent action ids.\n"
        f"Pending actions: {json.dumps(ctx['pending'])}\n"
        f"Rosters: {json.dumps(rosters)}\n"
        f"Bot is waiting on: {json.dumps(awaiting)}\n"
        f"Message: {json.dumps(text)}"
    )


def parse_intent(raw: str | None) -> dict:
    out = {"intent": "other", "action_id": None, "player": None, "league": None, "confidence": 0.0}
    if not raw:
        return out
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, AttributeError):
        return out
    if not isinstance(d, dict):
        return out
    intent = str(d.get("intent", "other")).lower()
    out["intent"] = intent if intent in INTENTS else "other"
    for k in ("action_id", "player", "league"):
        v = d.get(k)
        out[k] = str(v).lstrip("#") if v not in (None, "", "null") else None
    try:
        out["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        pass
    return out


def match_player(name: str | None, ctx: dict, league: str | None = None) -> tuple[str, str] | None:
    """Fuzzy-match a name the owner typed ("mitchell", "AD") to (league, exact roster name)."""
    if not name:
        return None
    pool = [(lg, p["name"]) for lg, v in ctx["leagues"].items()
            if not league or lg.lower().startswith(league.lower()) for p in v.get("roster", [])]
    low = name.lower().strip()
    hits = [x for x in pool if x[1].lower() == low] or \
           [x for x in pool if low in x[1].lower().split() or low in x[1].lower()]
    if not hits:
        close = difflib.get_close_matches(low, [x[1].lower() for x in pool], n=1, cutoff=0.75)
        hits = [x for x in pool if close and x[1].lower() == close[0]]
    return hits[0] if len({h[1] for h in hits}) == 1 else None


# ---------------------------------------------------------------- acting

def _set_rule(data_dir: Path, league: str, rule: str, player: str, week: int | None = None) -> None:
    prefs = _load(data_dir / "preferences.json", {})
    lg = prefs.setdefault(league, {})
    for key in ("always_start", "never_drop", "never_start", "start_this_week", "bench_this_week"):
        lst = lg.setdefault(key, [])
        if player in lst and (key.endswith("this_week") and rule.endswith("this_week") or key == rule or rule == "clear"):
            lst.remove(player)
    if rule != "clear":
        lg[rule].append(player)
        if week is not None:
            lg["override_week"] = week
    _save(data_dir / "preferences.json", prefs)


def _do(action: str, aid: str, cfg, store: PendingStore, approve_fn) -> str:
    try:
        if action == "reject":
            store.get(aid)
            store.set_status(aid, "rejected")
            return f"#{aid} rejected. Nothing sent."
        return approve_fn(aid, cfg, store)[1]
    except KeyError:
        return f"No pending action #{aid}."


def respond(sender: str, text: str, cfg, store: PendingStore, llm, approve_fn,
            answer_fn=None, data_dir: Path = DATA) -> str | None:
    """Returns the reply to text back, or None to stay quiet."""
    from .notify import parse_reply
    state = _load(data_dir / "chat_state.json", {})
    awaiting = state.get(sender)
    if awaiting and _now() - dt.datetime.fromisoformat(awaiting["asked"]) > dt.timedelta(hours=AWAIT_HOURS):
        awaiting = None

    def ask(action: str, aid: str) -> str:
        a = store.get(aid)
        state[sender] = {"action": action, "id": aid, "asked": _now().isoformat()}
        _save(data_dir / "chat_state.json", state)
        return f"Just to check: {action} #{aid}?\n{a.summary}\n\nReply yes or no."

    def clear_wait():
        state.pop(sender, None)
        _save(data_dir / "chat_state.json", state)

    fast = parse_reply(text)                      # exact "approve 4" needs no LLM
    if fast:
        clear_wait()
        return _do(fast[0], fast[1], cfg, store, approve_fn)

    ctx = build_context(store, data_dir)
    it = parse_intent(llm(_prompt(text, ctx, awaiting)))
    intent, conf = it["intent"], it["confidence"]
    open_ids = [p["id"] for p in ctx["pending"]]

    if intent in ("yes", "no"):
        if not awaiting:
            return "Nothing is waiting on a yes or no right now." if intent == "yes" else None
        clear_wait()
        if intent == "no":
            return f"OK, left #{awaiting['id']} alone. Nothing sent."
        return _do(awaiting["action"], awaiting["id"], cfg, store, approve_fn)

    if intent in ("approve", "reject"):
        aid = it["action_id"] if it["action_id"] in open_ids else (open_ids[0] if len(open_ids) == 1 else None)
        if not aid:
            if not open_ids:
                return "There's nothing waiting for approval right now."
            listing = "\n".join(f"• #{p['id']}: {p['summary']}" for p in ctx["pending"])
            return f"Which one?\n\n{listing}\n\nReply 'approve N' or 'reject N'."
        if it["action_id"] == aid and conf >= ACT_CONFIDENCE:
            clear_wait()
            return _do(intent, aid, cfg, store, approve_fn)
        return ask(intent, aid)                   # guessed the id or not confident: confirm first

    if intent in ("start", "bench", "always_start", "never_drop", "never_start", "clear_rule"):
        hit = match_player(it["player"], ctx, it["league"])
        if not hit:
            typed = f" by \"{it['player']}\"" if it["player"] else ""
            return f"I couldn't tell which player you meant{typed}. Can you send the full name?"
        league, name = hit
        week = ctx["leagues"].get(league, {}).get("week")
        rule = {"start": "start_this_week", "bench": "bench_this_week", "clear_rule": "clear"}.get(intent, intent)
        _set_rule(data_dir, league, rule, name, week if intent in ("start", "bench") else None)
        said = {"start": f"{name} will start in {league} this week.",
                "bench": f"{name} will sit in {league} this week.",
                "always_start": f"{name} will always start in {league} when healthy.",
                "never_drop": f"{name} will never be suggested as a drop in {league}.",
                "never_start": f"{name} won't be started in {league}.",
                "clear_rule": f"Cleared the rules for {name} in {league}."}[intent]
        return f"Got it. {said} The next lineup run will use this."

    if intent == "trade_idea":
        hit = match_player(it["player"], ctx, it["league"]) if it["player"] else None
        reqs = _load(data_dir / "requests.json", [])
        reqs.append({"type": "trade_idea", "player": hit[1] if hit else it["player"],
                     "league": hit[0] if hit else it["league"], "from": sender, "at": _now().isoformat()})
        _save(data_dir / "requests.json", reqs)
        who = hit[1] if hit else (it["player"] or "your team")
        return f"On it. I'll look at trade options for {who} and text you. Nothing gets proposed without your OK."

    if intent == "question":
        return answer_fn(text, ctx) if answer_fn else answer(text, ctx, llm)

    return None                                   # chit-chat: stay quiet


def answer(question: str, ctx: dict, llm) -> str:
    """Short answer grounded only in the context we pass; never invents numbers."""
    reply = llm(
        "You are a fantasy football bot texting the team owner. Answer in 1 to 4 short lines, "
        "plain words, no markdown headers. Use ONLY these facts; if they don't cover it, say you "
        "don't have that yet.\n"
        f"Facts: {json.dumps(ctx)}\nQuestion: {question}")
    return reply or "I don't have an answer for that yet. Try again after the next lineup run."
