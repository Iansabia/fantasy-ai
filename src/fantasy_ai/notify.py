"""Notifications. Pluggable backends with one method: send(to, text).

Backends:
  console  default; prints (and records) messages. The only backend tests use.
  imessage macOS Messages.app via `osascript`.
  twilio   SMS via Twilio REST API with httpx (env TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM).
  telegram Bot API sendMessage (env TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Default in CI.

Phone numbers live only in .env (e.g. NOTIFY_TO_COUSIN); config names the env var.
Messages are split into chunks of about 300 characters.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

import httpx

from .config import ROOT
from .lineup import LineupResult
from .models import LeagueState
from .pending import PendingAction
from .slots import NON_STARTING, slot_name

MAX_CHARS = 300


class Backend(Protocol):
    def send(self, to: str, text: str) -> None: ...


class ConsoleBackend:
    def __init__(self, quiet: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.quiet = quiet

    def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))
        if not self.quiet:
            print(f"[text to {mask(to)}] {text}")


class IMessageBackend:
    max_chars = 1500

    def send(self, to: str, text: str) -> None:
        script = (
            'on run argv\n'
            '  tell application "Messages"\n'
            # some accounts (SMS relay, RCS) throw when their properties are read,
            # so a "whose" filter fails; walk them one at a time instead
            '    set svc to missing value\n'
            '    repeat with a in accounts\n'
            '      try\n'
            '        if service type of a is iMessage and enabled of a then\n'
            '          set svc to a\n'
            '          exit repeat\n'
            '        end if\n'
            '      end try\n'
            '    end repeat\n'
            '    if svc is missing value then error "no enabled iMessage account in Messages.app"\n'
            '    send (item 2 of argv) to participant (item 1 of argv) of svc\n'
            '  end tell\n'
            'end run'
        )
        # numbers and text go in as argv, never interpolated into the script
        subprocess.run(["osascript", "-e", script, to, text], check=True, capture_output=True, timeout=30)
        remember_sent(text)


SENT_LOG = "imessage_sent.json"


def _sent_path():
    return Path(os.environ.get("FAI_DATA_DIR") or ROOT / "data") / SENT_LOG


def remember_sent(text: str) -> None:
    """Texting your own number echoes the bot's message back as an incoming one, so the
    poller must know what the bot itself sent. Keeps the last 300 texts."""
    import json
    p = _sent_path()
    try:
        seen = json.loads(p.read_text())
    except (OSError, ValueError):
        seen = []
    seen = (seen + [text.strip()])[-300:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seen))


def was_sent_by_bot(text: str) -> bool:
    import json
    try:
        return text.strip() in set(json.loads(_sent_path().read_text()))
    except (OSError, ValueError):
        return False


class TwilioBackend:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.sid = os.environ.get("TWILIO_SID", "")
        self.token = os.environ.get("TWILIO_TOKEN", "")
        self.sender = os.environ.get("TWILIO_FROM", "")
        self.transport = transport
        if not (self.sid and self.token and self.sender):
            raise RuntimeError("Twilio needs TWILIO_SID, TWILIO_TOKEN and TWILIO_FROM in .env")

    def send(self, to: str, text: str) -> None:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        with httpx.Client(transport=self.transport, timeout=15) as c:
            r = c.post(url, auth=(self.sid, self.token), data={"From": self.sender, "To": to, "Body": text})
        if r.status_code >= 300:
            raise RuntimeError(f"Twilio error HTTP {r.status_code}")


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBackend:
    max_chars = 3500

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.transport = transport
        if not self.token:
            raise RuntimeError("Telegram needs TELEGRAM_BOT_TOKEN")

    def call(self, method: str, **params) -> dict:
        with httpx.Client(transport=self.transport, timeout=30) as c:
            r = c.post(TELEGRAM_API.format(token=self.token, method=method), json=params)
        if r.status_code >= 300:
            raise RuntimeError(f"Telegram error HTTP {r.status_code}")  # never echo the URL (it has the token)
        return r.json()

    def send(self, to: str, text: str) -> None:
        self.call("sendMessage", chat_id=to, text=text, disable_web_page_preview=True)


def mask(number: str) -> str:
    return ("***" + number[-2:]) if len(number) > 4 else "***"


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split into chunks <= limit, keeping whole lines (and blank-line sections) together."""
    if len(text) <= limit:
        return [text.strip()] if text.strip() else []
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                      # one very long line: break on words
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur.strip():
                chunks.append(cur.strip())
                cur = ""
            chunks.append(line[:cut].strip())
            line = line[cut:].strip()
        if len(cur) + len(line) + 1 > limit and cur.strip():
            chunks.append(cur.strip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    if len(chunks) > 1:
        chunks = [f"({i}/{len(chunks)})\n{c}" for i, c in enumerate(chunks, 1)]
    return chunks


class Notifier:
    """Sends to every recipient. A failed send (Messages.app error, Twilio down) never raises:
    it is printed and kept in .failures, so one bad text can't stop a run halfway (a lineup
    already saved, the next league never processed). Callers report .failures."""

    def __init__(self, backend: Backend, recipients: list[str]):
        self.backend, self.recipients = backend, recipients
        self.failures: list[str] = []

    def send(self, text: str) -> int:
        n = 0
        limit = getattr(self.backend, "max_chars", MAX_CHARS)
        if not self.recipients:
            self.failures.append("no recipients configured")
            print("WARNING: text not sent: no phone number configured for this owner")
            return 0
        for to in self.recipients:
            for chunk in split_text(text, limit):
                try:
                    self.backend.send(to, chunk)
                    n += 1
                except Exception as e:
                    why = f"text to {mask(to)} failed ({type(e).__name__})"
                    self.failures.append(why)
                    print(f"WARNING: {why}")
                    break                       # skip the rest of this text for this number
        return n


def make_notifier(notify_cfg: dict | None, owner: str) -> Notifier:
    """notify_cfg: {"backend": "console|imessage|twilio", "to": {"cousin": "NOTIFY_TO_COUSIN"}}"""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    cfg = notify_cfg or {}
    name = os.environ.get("FAI_NOTIFY_BACKEND") or cfg.get("backend", "console")
    backend: Backend = {"console": ConsoleBackend, "imessage": IMessageBackend, "twilio": TwilioBackend,
                        "telegram": TelegramBackend}[name]()
    if name == "telegram":
        return Notifier(backend, [os.environ.get("TELEGRAM_CHAT_ID", "")])
    to_map = cfg.get("to", {}) or {}
    env_name = to_map.get(owner, f"NOTIFY_TO_{owner.upper()}")
    raw = os.environ.get(env_name, "")
    recipients = [n.strip() for n in raw.split(",") if n.strip()]
    if not recipients and name == "console":
        recipients = [owner]
    return Notifier(backend, recipients)


# ---------------------------------------------------------------- message builders
# Texts are read on a phone: short header, blank line, one fact per line.

def _pts(x) -> str:
    return f"{x:.1f}" if isinstance(x, (int, float)) else str(x)


def msg_weekly(facts: dict) -> str:
    m = facts.get("matchup")
    lines = [f"🏈 {facts['league']} | Week {facts['week']}"]
    if m:
        lines.append(f"vs {m['opp_name']}: {_pts(m['my_points'])} - {_pts(m['opp_points'])}")
    lines.append("")
    if facts["lineup_moves"]:
        lines.append(f"Lineup: {_pts(facts['projected_after'])} proj ({facts['projected_gain']:+.1f} from {facts['move_count']} moves)")
    else:
        lines.append(f"Lineup: {_pts(facts['projected_after'])} proj, already set")
    if facts["injured_out"]:
        lines.append("")
        lines.append("Out:")
        lines += [f"• {n}".replace("INJURY_RESERVE", "IR") for n in facts["injured_out"]]
    if facts["waiver_ideas"]:
        w = facts["waiver_ideas"][0]
        lines.append("")
        lines.append("Top waiver idea:")
        lines.append(f"• Add {w['add']}")
        lines.append(f"• Drop {w['drop']}")
    return "\n".join(lines)


def _move_line(m) -> str:
    base = f"{m.name}: {slot_name(m.from_slot)} → {slot_name(m.to_slot)}"
    # drop the "started X at RB:" lead-in, the bullet already says who moved where
    why = re.sub(r"^(started|benched|moved)\s.*?:\s*", "", m.reason or "", flags=re.I)
    return f"• {base}" + (f"\n   {why[:1].upper() + why[1:]}" if why else "")


def msg_lineup_changes(label: str, res: LineupResult, sent: bool = True) -> str:
    lines = [f"🏈 {label} | {'Lineup set' if sent else 'Lineup plan (preview)'}",
             f"{_pts(res.before_points)} → {_pts(res.after_points)} proj ({res.gain:+.1f})", ""]
    ins = [m for m in res.moves if m.to_slot != 20]
    outs = [m for m in res.moves if m.to_slot == 20]
    if ins:
        lines.append("Starting:")
        lines += [_move_line(m) for m in ins]
    if outs:
        if ins:
            lines.append("")
        lines.append("Benched:")
        lines += [_move_line(m) for m in outs]
    ir = getattr(res, "ir_moves", None) or []
    if ir:
        lines.append("")
        lines.append("IR:")
        lines += [_move_line(m) for m in ir]
    if getattr(res, "ir_error", None):
        lines.append("")
        lines.append(f"⚠️ IR move not saved: {res.ir_error}")
    lines.append("")
    lines.append("✅ Already saved on ESPN. No reply needed." if sent
                 else "Preview only, nothing changed on ESPN.")
    return "\n".join(lines)


def msg_pending(a: PendingAction) -> str:
    label = {"WAIVER": "Waiver claim", "FREEAGENT": "Free agent add", "TRADE_PROPOSAL": "Trade proposal"}.get(a.kind, a.kind)
    return (f"❓ {label} #{a.id} | {a.league}\n\n"
            f"{a.summary}\n\n"
            f"Reply 'approve {a.id}' or 'reject {a.id}'")


def msg_pref_skipped(label: str, notes: list[str]) -> str:
    return f"⚠️ {label} | Couldn't follow your request\n\n" + "\n".join(f"• {n}" for n in notes)


def msg_paused(label: str, why: str) -> str:
    if "cap" in why and "cooldown" not in why and "pushed back" not in why:
        tail = "• No more lineup writes for this league in this run; the next scheduled run tries again"
    else:
        tail = "• No more ESPN writes for this league until the cooldown ends"
    return f"⏸️ {label} | Paused\n\n• {why}\n{tail}"


def msg_starter_injuries(label: str, state: LeagueState) -> str | None:
    hurt = [f"• {p.name} ({p.injury_status.replace('_', ' ').title()})" for p in state.roster
            if p.lineup_slot not in NON_STARTING and (p.injury_status or "ACTIVE") not in ("ACTIVE", "NORMAL")]
    if not hurt:
        return None
    return f"🚑 {label} | Injured starters\n\n" + "\n".join(hurt)


_REPLY = __import__("re").compile(r"^\s*/?(approve|reject)\s+#?(\d+)\s*$", 2)  # 2 = re.IGNORECASE


def parse_reply(text: str) -> tuple[str, str] | None:
    """'approve 4' / 'Reject #4' / '/approve 4' -> ("approve", "4"). Anything else -> None."""
    m = _REPLY.match(text or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def handle_reply(text: str, sender: str) -> None:
    """iMessage/SMS inbound replies are not wired up (Telegram is, see telegram_poll.py)."""
    raise NotImplementedError("Inbound SMS/iMessage replies are not wired up; use Telegram or fai approve <id>.")
