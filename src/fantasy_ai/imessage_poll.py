"""Inbound approvals over iMessage (macOS only). Reads ~/Library/Messages/chat.db read-only,
accepts ONLY incoming messages from the phone numbers configured under notify.to, parses
'approve 4' / 'reject 4', and runs the same approve path as the CLI (fresh read + re-check
before anything is sent). Replies go back over iMessage.

Needs Full Disk Access for whatever runs it (Terminal / VS Code, or the launchd python).
The first run only records where the chat log ends, so old messages are never acted on."""
from __future__ import annotations

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


def read_new(db: Path, after_rowid: int) -> list[tuple[int, str, str]]:
    """(rowid, sender handle, text) for incoming messages after after_rowid."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT m.ROWID, h.id, m.text, m.attributedBody FROM message m "
            "JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE m.is_from_me = 0 AND m.ROWID > ? ORDER BY m.ROWID", (after_rowid,)).fetchall()
    finally:
        con.close()
    return [(r[0], r[1] or "", r[2] or decode_attributed_body(r[3])) for r in rows]


def max_rowid(db: Path) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
    finally:
        con.close()


def poll(cfg: AppConfig, store: PendingStore, backend=None, approve_fn=None,
         db: Path = CHAT_DB, state_file: Path = STATE_FILE, llm=None) -> list[str]:
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
        raise RuntimeError("Can't read Messages (chat.db). Give Full Disk Access to the app "
                           "running this: System Settings > Privacy & Security > Full Disk Access.") from e
    allowed = allowed_senders(cfg)
    llm = llm or _default_llm()
    log = []
    for rowid, sender, text in rows:
        last = max(last, rowid)
        if not text.strip() or was_sent_by_bot(text):   # skip echoes of the bot's own texts
            continue
        if _digits(sender) not in allowed:
            if parse_reply(text):
                log.append("ignored command from unknown sender")
            continue
        # every message from the owner is read and understood, not just "approve N"
        reply = respond(sender, text, cfg, store, llm, approve_fn, data_dir=state_file.parent)
        if reply:
            backend.send(sender, reply)
            log.append(f"replied to {mask(sender)}")
    state_file.write_text(json.dumps({"rowid": last}))
    return log
