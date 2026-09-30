import datetime as dt
import json

import httpx
import pytest

from conftest import b
from fantasy_ai import cli, insights, notify
from fantasy_ai.config import AppConfig, Credentials, LeagueConfig
from fantasy_ai.espn_read import EspnReader
from fantasy_ai.espn_write import TransactionWriter, free_agent_payload, trade_proposal_payload
from fantasy_ai.lineup import optimize
from fantasy_ai.pending import PendingStore, checks_for_add_drop, checks_for_trade
from fantasy_ai.waivers import rank_free_agents


class FakeReader:
    def __init__(self, state):
        self.state = state

    def load(self, **k):
        return self.state


class RecordingWriter:
    def __init__(self):
        self.sent = []

    def submit(self, payload, send=False):
        self.sent.append((payload, send))
        from fantasy_ai.espn_write import WriteResult
        return WriteResult(send, "u", payload, {"status": "PENDING"}, "ok")


@pytest.fixture
def store(tmp_path):
    return PendingStore(tmp_path / "pending.json")


def test_queue_and_approve_sends_only_after_recheck(store, demo_state):
    a = store.add("demo", "FREEAGENT", "add X", free_agent_payload(1, 4, 903, 304), checks_for_add_drop(903, 304))
    assert a.id == "1" and store.open_actions()[0].id == "1"
    w = RecordingWriter()
    rc, msg = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 0 and w.sent == [(a.payload, True)]
    assert store.get("1").status == "approved"
    rc, _ = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), w)   # not twice
    assert rc == 1 and len(w.sent) == 1


def test_approve_aborts_when_player_gone(store, demo_state):
    # Flex Fred (304) no longer on our roster -> abort, nothing sent
    demo_state.my_team.roster = [p for p in demo_state.roster if p.player_id != 304]
    store.add("demo", "FREEAGENT", "add X", free_agent_payload(1, 4, 903, 304), checks_for_add_drop(903, 304))
    w = RecordingWriter()
    rc, msg = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 1 and "aborted" in msg and w.sent == []


def test_approve_aborts_when_add_target_taken(store, demo_state):
    store.add("demo", "FREEAGENT", "add", free_agent_payload(1, 4, 311, None), checks_for_add_drop(311, None))
    w = RecordingWriter()
    rc, msg = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), w)   # 311 is on team 2
    assert rc == 1 and "picked up" in msg and w.sent == []


def test_approve_aborts_when_drop_locked(store, demo_state):
    store.add("demo", "FREEAGENT", "x", free_agent_payload(1, 4, 903, 201), checks_for_add_drop(903, 201))
    rc, msg = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), RecordingWriter())
    assert rc == 1 and "locked" in msg


def test_approve_trade_aborts_if_partner_traded_player(store, demo_state):
    p = trade_proposal_payload(1, 2, 4, [102], [211])
    store.add("demo", "TRADE_PROPOSAL", "t", p, checks_for_trade([102], [211], 2))
    demo_state.teams[2].roster = [x for x in demo_state.teams[2].roster if x.player_id != 211]
    w = RecordingWriter()
    rc, msg = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 1 and w.sent == []


def test_expired_action_not_sent(store, demo_state):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
    store.add("demo", "WAIVER", "x", free_agent_payload(1, 4, 903), checks_for_add_drop(903, None), now=old)
    assert store.open_actions() == []
    w = RecordingWriter()
    rc, msg = cli.approve("1", AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 1 and "expired" in msg and w.sent == []


def test_approve_on_fixture_league_refuses_real_send(store, demo_state, tmp_path):
    lg = LeagueConfig("demo", "ian", 99, 1, 2026, "ian", fixture="tests/fixtures/demo")
    store.add("demo", "FREEAGENT", "x", free_agent_payload(1, 4, 903, 304), checks_for_add_drop(903, 304))
    writer = TransactionWriter(lg, Credentials("s2", "sw"), transport=httpx.MockTransport(lambda r: 1 / 0))
    rc, msg = cli.approve("1", AppConfig([lg]), store, FakeReader(demo_state), writer)
    assert rc == 2 and "REFUSED" in msg


def test_cli_waiver_pick_queues_not_sends(tmp_path, capsys):
    store = tmp_path / "p.json"
    rc = cli.main(["--config", "config/leagues.example.yaml", "--store", str(store), "waivers", "demo", "--pick", "1"])
    out = capsys.readouterr().out
    assert rc == 0 and "DRY RUN" in out and "Queued as #1" in out
    data = json.loads(store.read_text())
    assert data["actions"][0]["payload"]["bidAmount"] == 0 and data["actions"][0]["kind"] == "WAIVER"


def test_cli_trade_propose_queues(tmp_path, capsys):
    store = tmp_path / "p.json"
    rc = cli.main(["--config", "config/leagues.example.yaml", "--store", str(store), "trade", "demo",
                   "--give", "Backup Bo", "--get", "Rival RB1", "--propose"])
    assert rc == 0 and "Queued as #1" in capsys.readouterr().out
    assert json.loads(store.read_text())["actions"][0]["payload"]["type"] == "TRADE_PROPOSAL"


# ---- notify (console backend only)

def test_split_text_under_limit():
    text = " ".join(["word"] * 200)
    chunks = notify.split_text(text, 300)
    assert len(chunks) > 1 and all(len(c) <= 310 for c in chunks) and chunks[0].startswith("(1/")
    assert notify.split_text("short") == ["short"]


def test_console_notifier_and_messages(demo_state):
    be = notify.ConsoleBackend(quiet=True)
    n = notify.Notifier(be, ["+15550000000"])
    res = optimize(demo_state)
    n.send(notify.msg_lineup_changes("demo", res))
    facts = insights.build_facts("demo", demo_state, res, rank_free_agents(demo_state))
    n.send(notify.msg_weekly(facts))
    assert len(be.sent) >= 2 and all(len(t) <= 310 for _, t in be.sent)
    assert "demo | Injured starters" in notify.msg_starter_injuries("demo", demo_state)
    assert notify.mask("+15551234567") == "***67"


def test_pending_message_format(store):
    a = store.add("league-a", "TRADE_PROPOSAL", "give X get Y, +6.2 pts/wk.", {}, {})
    assert notify.msg_pending(a) == "\u2753 Trade proposal #1 | league-a\n\ngive X get Y, +6.2 pts/wk.\n\nReply 'approve 1' or 'reject 1'"


def test_reply_stub():
    with pytest.raises(NotImplementedError):
        notify.handle_reply("approve 1", "+1")


# ---- insights

def test_template_note_uses_facts(demo_state):
    facts = insights.build_facts("demo", demo_state, optimize(demo_state), rank_free_agents(demo_state))
    note = insights.template_note(facts)
    assert "82.0 -> 109.0" in note and "Hurt Hal" in note and insights.numbers_are_grounded(note, facts)


def test_llm_note_falls_back_when_numbers_invented(demo_state, monkeypatch):
    facts = insights.build_facts("demo", demo_state, optimize(demo_state))

    text, used = insights.llm_note(facts, llm=lambda p: "You will score 999 points this week!")
    assert not used and text == insights.template_note(facts)
    text, used = insights.llm_note(facts, llm=lambda p: "Big week: projected 109.0 after moving Ready Rex in.")
    assert used and "109.0" in text
    text, used = insights.llm_note(facts, llm=lambda p: None)   # no LLM available
    assert not used


def test_llm_call_cap(monkeypatch):
    from fantasy_ai import llm as llm_mod
    monkeypatch.setattr(llm_mod.shutil, "which", lambda x: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    l = llm_mod.LLM(max_calls=1)
    assert l("x") is None and l.calls == 1 and l("y") is None and l.calls == 1
