"""Inbound approvals over Telegram. Reads getUpdates, accepts ONLY messages from the
configured TELEGRAM_CHAT_ID, parses 'approve 4' / 'reject 4', and runs the same approve
path as the CLI (fresh read + re-check before anything is sent)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import AppConfig, ROOT
from .notify import TelegramBackend, parse_reply
from .pending import PendingStore

OFFSET_FILE = ROOT / "data" / "telegram_offset.json"


def poll(cfg: AppConfig, store: PendingStore, backend: TelegramBackend | None = None,
         approve_fn=None, offset_file: Path = OFFSET_FILE) -> list[str]:
    """Returns short, non-identifying summaries for logs."""
    from .cli import approve as default_approve
    approve_fn = approve_fn or default_approve
    backend = backend or TelegramBackend()
    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")
    offset = json.loads(offset_file.read_text())["offset"] if offset_file.exists() else 0
    updates = backend.call("getUpdates", offset=offset, timeout=0).get("result", [])
    log = []
    for u in updates:
        offset = max(offset, int(u["update_id"]) + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat_id:
            log.append("ignored message from unknown chat")
            continue
        cmd = parse_reply(msg.get("text", ""))
        if not cmd:
            continue
        action, aid = cmd
        try:
            if action == "reject":
                from .cli import reject
                rc, reply = reject(aid, store)
            else:
                rc, reply = approve_fn(aid, cfg, store)
        except KeyError:
            rc, reply = 1, f"No pending action #{aid}."
        backend.send(chat_id, reply)
        log.append(f"{action} #{aid}: rc={rc}")
    offset_file.parent.mkdir(parents=True, exist_ok=True)
    offset_file.write_text(json.dumps({"offset": offset}))
    return log
