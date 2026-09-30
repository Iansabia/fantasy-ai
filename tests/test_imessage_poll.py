import json
import sqlite3

import pytest

from fantasy_ai import imessage_poll
from fantasy_ai.pending import PendingStore


@pytest.fixture
def store(tmp_path):
    return PendingStore(tmp_path / "pending.json")


def _db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    con.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, "
                "is_from_me INTEGER, handle_id INTEGER)")
    con.executemany("INSERT INTO handle VALUES (?, ?)", [(1, "+15550001111"), (2, "+15559998888")])
    con.executemany("INSERT INTO message (ROWID, text, attributedBody, is_from_me, handle_id) VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


class FakeBackend:
    def __init__(self):
        self.sent = []

    def send(self, to, text):
        self.sent.append((to, text))


class Cfg:
    notify = {"to": {"cousin": "NOTIFY_TO_COUSIN"}}


def test_first_run_marks_end_and_processes_nothing(tmp_path, monkeypatch, store):
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    _db(db, [(1, "approve 1", None, 0, 1)])
    calls = []
    log = imessage_poll.poll(Cfg(), store, FakeBackend(), lambda *a: calls.append(a) or (0, "ok"), db, st)
    assert calls == [] and json.loads(st.read_text())["rowid"] == 1 and "first run" in log[0]


def test_only_allowed_sender_and_incoming(tmp_path, monkeypatch, store):
    monkeypatch.setenv("NOTIFY_TO_COUSIN", "+1 (555) 000-1111")
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    st.write_text(json.dumps({"rowid": 0}))
    a = store.add("league-a", "TRADE_PROPOSAL", "give X get Y", {}, {})
    _db(db, [
        (1, f"approve {a.id}", None, 0, 2),   # stranger: ignored
        (2, f"approve {a.id}", None, 1, 1),   # our own outgoing text: ignored
        (3, "nice", None, 0, 1),              # not a command
        (4, f"Approve #{a.id}", None, 0, 1),  # cousin: runs
    ])
    calls, be = [], FakeBackend()
    log = imessage_poll.poll(Cfg(), store, be, lambda aid, c, s: calls.append(aid) or (0, "sent"), db, st,
                             llm=lambda p: '{"intent": "other"}')
    assert calls == [a.id]
    assert be.sent == [("+15550001111", "sent")]
    assert "ignored command from unknown sender" in log
    assert json.loads(st.read_text())["rowid"] == 4


def test_reject_and_attributed_body(tmp_path, monkeypatch, store):
    monkeypatch.setenv("NOTIFY_TO_COUSIN", "+15550001111")
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    st.write_text(json.dumps({"rowid": 0}))
    a = store.add("league-a", "WAIVER", "add X drop Y", {}, {})
    text = f"reject {a.id}".encode()
    blob = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + bytes([len(text)]) + text + b"\x86\x84"
    assert imessage_poll.decode_attributed_body(blob) == f"reject {a.id}"
    _db(db, [(1, None, blob, 0, 1)])
    be = FakeBackend()
    imessage_poll.poll(Cfg(), store, be, lambda *a: (0, "x"), db, st, llm=lambda p: None)
    assert store.get(a.id).status == "rejected" and "rejected" in be.sent[0][1]
