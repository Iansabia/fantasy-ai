"""Inbound approvals over iMessage (macOS only). Reads ~/Library/Messages/chat.db read-only,
accepts ONLY incoming messages from the phone numbers configured under notify.to, parses
'approve 4' / 'reject 4', and runs the same approve path as the CLI (fresh read + re-check
before anything is sent). Replies go back over iMessage.

Needs Full Disk Access for whatever runs it (Terminal / VS Code, or the launchd python).
The first run only records where the chat log ends, so old messages are never acted on.
Messages older than MAX_AGE (a backlog from while access was missing, or the Mac was asleep)
are skipped, never executed: the sender gets one note asking to resend.
Each message moves the marker forward BEFORE it is acted on (at most once): if a reply fails
to send, or something crashes, the next poll does not run the same command or text again."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

from .config import AppConfig, ROOT
from .notify import IMessageBackend, mask, parse_reply, was_sent_by_bot
from .chat import respond
from .pending import PendingStore


def _default_llm():
    from .llm import LLM
    return LLM(max_calls=10)

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
MAX_AGE = dt.timedelta(hours=3)       # older texts are not acted on (pending actions expire in 12-48 h)
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


class ChatDbUnavailable(RuntimeError):
    """chat.db can't be read (no Full Disk Access yet)."""
STATE_FILE = ROOT / "data" / "imessage_last.json"


def _digits(number: str) -> str:
    d = "".join(ch for ch in number if ch.isdigit())
    return d[-10:]  # compare US numbers without the country code


def allowed_senders(cfg: AppConfig) -> set[str]:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    to_map = (cfg.notify or {}).get("to", {}) or {}
    out = set()
    for env_name in to_map.values():
        for n in os.environ.get(env_name, "").split(","):
            if n.strip():
                out.add(_digits(n))
    return out


def decode_attributed_body(blob: bytes | None) -> str:
    """Newer macOS leaves message.text NULL and stores an NSAttributedString typedstream.
    The plain string sits right after the NSString class name, behind a '+' marker and a length."""
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    j = blob.find(b"\x01+", i)
    if j < 0:
        return ""
    j += 2
    n = blob[j]
    if n == 0x81:
        n, j = int.from_bytes(blob[j + 1:j + 3], "little"), j + 3
    elif n == 0x82:
        n, j = int.from_bytes(blob[j + 1:j + 5], "little"), j + 5
    else:
        j += 1
    return blob[j:j + n].decode("utf-8", "ignore")


def apple_time(raw) -> dt.datetime | None:
    """message.date: seconds (old macOS) or nanoseconds (High Sierra on) since 2001-01-01 UTC."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return APPLE_EPOCH + dt.timedelta(seconds=v / 1e9 if v > 1e11 else v)


def read_new(db: Path, after_rowid: int) -> list[tuple[int, str, str, dt.datetime | None]]:
    """(rowid, sender handle, text, sent at) for incoming messages after after_rowid."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT m.ROWID, h.id, m.text, m.attributedBody, m.date FROM message m "
            "JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE m.is_from_me = 0 AND m.ROWID > ? ORDER BY m.ROWID", (after_rowid,)).fetchall()
    finally:
        con.close()
    return [(r[0], r[1] or "", r[2] or decode_attributed_body(r[3]), apple_time(r[4])) for r in rows]


def max_rowid(db: Path) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
    finally:
        con.close()


def poll(cfg: AppConfig, store: PendingStore, backend=None, approve_fn=None,
         db: Path = CHAT_DB, state_file: Path = STATE_FILE, llm=None,
         now: dt.datetime | None = None) -> list[str]:
    """Returns short, non-identifying summaries for logs."""
    from .cli import approve as default_approve
    approve_fn = approve_fn or default_approve
    backend = backend or IMessageBackend()
    try:
        if not state_file.exists():
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"rowid": max_rowid(db)}))
            return ["first run: marked the end of the chat log, nothing processed"]
        last = json.loads(state_file.read_text())["rowid"]
        rows = read_new(db, last)
    except sqlite3.OperationalError as e:
        # Only a permission problem means "no Full Disk Access". A locked database or a schema
        # change is a different failure and must not hide behind the once-a-day FDA note.
        code = getattr(e, "sqlite_errorcode", None)
        if code not in (3, 14, 23) and not any(k in str(e).lower() for k in       # PERM, CANTOPEN, AUTH
                                               ("unable to open", "authoriz", "not permitted")):
            raise
        raise ChatDbUnavailable("Can't read Messages (chat.db). Give Full Disk Access to the app "
                           "running this: System Settings > Privacy & Security > Full Disk Access.") from e
    allowed = allowed_senders(cfg)
    llm = llm or _default_llm()
    now = now or dt.datetime.now(dt.timezone.utc)
    log: list[str] = []
    stale: dict[str, int] = {}          # sender -> old commands skipped
    skipped = 0
    for rowid, sender, text, sent_at in rows:
        last = max(last, rowid)
        state_file.write_text(json.dumps({"rowid": last}))   # at most once, even if this one crashes
        if not text.strip() or was_sent_by_bot(text):   # skip echoes of the bot's own texts
            continue
        if _digits(sender) not in allowed:
            if parse_reply(text):
                log.append("ignored command from unknown sender")
            continue
        if sent_at is None or now - sent_at > MAX_AGE:
            skipped += 1
            if _looks_like_command(text):
                stale[sender] = stale.get(sender, 0) + 1
            continue
        try:
            # every message from the owner is read and understood, not just "approve N"
            reply = respond(sender, text, cfg, store, llm, approve_fn, data_dir=state_file.parent,
                            sent_at=sent_at)
        except Exception as e:
            log.append(f"failed to handle a message from {mask(sender)} ({type(e).__name__})")
            reply = ("Something went wrong handling that text. Check what's pending before sending it "
                     "again (it won't be retried on its own).")
        if reply:
            try:
                backend.send(sender, reply)
                log.append(f"replied to {mask(sender)}")
            except Exception as e:
                log.append(f"reply to {mask(sender)} failed ({type(e).__name__})")
    if skipped:
        log.append(f"skipped {skipped} old message{'s' if skipped != 1 else ''} (older than "
                   f"{int(MAX_AGE.total_seconds() // 3600)} h); nothing in them was acted on")
    for sender, n in stale.items():
        note = (f"I couldn't read texts for a while, so I skipped {n} older "
                f"repl{'ies' if n != 1 else 'y'} and did nothing with them. "
                "If you still want something done, resend it now (for example 'approve 4').")
        try:
            backend.send(sender, note)
        except Exception as e:
            log.append(f"stale-message note to {mask(sender)} failed ({type(e).__name__})")
    return log


def _looks_like_command(text: str) -> bool:
    import re
    return bool(parse_reply(text)) or re.search(r"\b(approve|reject|yes|yep|yeah|no|do it|go ahead|start|bench|drop|trade)\b",
                                                text or "", re.I) is not None
