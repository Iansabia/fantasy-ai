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
import subprocess
from typing import Protocol

import httpx

from .config import ROOT
from .lineup import LineupResult
from .models import LeagueState
from .pending import PendingAction
from .slots import NON_STARTING

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
    def send(self, to: str, text: str) -> None:
        script = (
            'on run argv\n'
            '  tell application "Messages"\n'
            '    set svc to 1st account whose service type = iMessage\n'
            '    send (item 2 of argv) to participant (item 1 of argv) of svc\n'
            '  end tell\n'
            'end run'
        )
        # numbers and text go in as argv, never interpolated into the script
        subprocess.run(["osascript", "-e", script, to, text], check=True, capture_output=True, timeout=30)


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
    """Split on line breaks, then words, so each chunk is <= limit."""
    chunks, cur = [], ""
    for word in text.replace("\n", " \n ").split(" "):
        if word == "\n":
            if len(cur) >= limit * 0.6:
                chunks.append(cur.strip())
                cur = ""
            else:
                cur += "\n"
            continue
        while len(word) > limit:
            chunks.append(word[:limit])
            word = word[limit:]
        if len(cur) + len(word) + 1 > limit:
            chunks.append(cur.strip())
            cur = ""
        cur += (" " if cur and not cur.endswith("\n") else "") + word
    if cur.strip():
        chunks.append(cur.strip())
    chunks = [c for c in chunks if c]
    if len(chunks) > 1:
        chunks = [f"({i}/{len(chunks)}) {c}"[:limit + 8] for i, c in enumerate(chunks, 1)]
    return chunks


class Notifier:
    def __init__(self, backend: Backend, recipients: list[str]):
        self.backend, self.recipients = backend, recipients

    def send(self, text: str) -> int:
        n = 0
        for to in self.recipients:
            for chunk in split_text(text):
                self.backend.send(to, chunk)
                n += 1
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

def msg_weekly(facts: dict) -> str:
    m = facts.get("matchup")
    parts = [f"{facts['league']} wk {facts['week']}:"]
    if m:
        parts.append(f"vs {m['opp_name']} {m['my_points']}-{m['opp_points']}.")
    parts.append(f"Lineup {facts['projected_after']} proj" + (f" ({facts['projected_gain']:+.1f} after {facts['move_count']} moves)." if facts["lineup_moves"] else ", already optimal."))
    if facts["injured_out"]:
        parts.append("Out: " + ", ".join(facts["injured_out"]) + ".")
    if facts["waiver_ideas"]:
        w = facts["waiver_ideas"][0]
        parts.append(f"Top waiver idea: add {w['add']}, drop {w['drop']}.")
    return " ".join(parts)


def msg_lineup_changes(label: str, res: LineupResult, sent: bool = True) -> str:
    head = f"{label}: lineup {'set' if sent else 'plan (dry run)'}, {res.before_points} -> {res.after_points} proj ({res.gain:+.1f})."
    lines = [m.reason or str(m) for m in res.moves if m.to_slot != 20] + \
            [m.reason or str(m) for m in res.moves if m.to_slot == 20]
    return head + "\n" + "\n".join(f"- {l}" for l in lines)


def msg_pending(a: PendingAction) -> str:
    label = {"WAIVER": "Waiver claim", "FREEAGENT": "Free agent add", "TRADE_PROPOSAL": "Trade proposal"}.get(a.kind, a.kind)
    return f"{label} #{a.id} ({a.league}): {a.summary} Reply 'approve {a.id}' or run fai approve {a.id}"


def msg_starter_injuries(label: str, state: LeagueState) -> str | None:
    hurt = [f"{p.name} ({p.injury_status})" for p in state.roster
            if p.lineup_slot not in NON_STARTING and (p.injury_status or "ACTIVE") not in ("ACTIVE", "NORMAL")]
    if not hurt:
        return None
    return f"{label}: starters with injury tags: " + ", ".join(hurt) + "."


_REPLY = __import__("re").compile(r"^\s*/?(approve|reject)\s+#?(\d+)\s*$", 2)  # 2 = re.IGNORECASE


def parse_reply(text: str) -> tuple[str, str] | None:
    """'approve 4' / 'Reject #4' / '/approve 4' -> ("approve", "4"). Anything else -> None."""
    m = _REPLY.match(text or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def handle_reply(text: str, sender: str) -> None:
    """iMessage/SMS inbound replies are not wired up (Telegram is, see telegram_poll.py)."""
    raise NotImplementedError("Inbound SMS/iMessage replies are not wired up; use Telegram or fai approve <id>.")
