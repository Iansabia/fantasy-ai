"""Regression tests from the October 2026 bug audit. Each test failed before its fix."""
import datetime as dt
import json
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import b, make_state
from fantasy_ai import approval, chat, cli, imessage_poll, notify, waivers
from fantasy_ai.config import AppConfig, LeagueConfig
from fantasy_ai.espn_write import WriteRejected, WriteResult, free_agent_payload
from fantasy_ai.lineup import Move, optimize
from fantasy_ai.pacing import Pacer, PacingStop
from fantasy_ai.pending import PendingStore, checks_for_add_drop

COUSIN, IAN = "+15550001111", "+15552223333"
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture
def store(tmp_path):
    return PendingStore(tmp_path / "pending.json")


class FakeReader:
    def __init__(self, state):
        self.state = state

    def load(self, **k):
        return self.state


class RecordingWriter:
    def __init__(self, fail_ir=False, on_submit=None):
        self.sent, self.fail_ir, self.on_submit = [], fail_ir, on_submit

    def submit(self, payload, send=False):
        if self.on_submit:
            self.on_submit()
        if self.fail_ir and any(i.get("toLineupSlotId") == 21 or i.get("fromLineupSlotId") == 21
                                for i in payload.get("items", [])):
            raise WriteRejected(409, "X", "ESPN rejected the IR move")
        self.sent.append((payload, send))
        return WriteResult(send, "u", payload, {"status": "EXECUTED"}, "ok")


def two_owner_cfg():
    return AppConfig(
        leagues=[LeagueConfig("cpmclean", "cousin", 1, 23, 2026, "cousin", display_name="CP McLean"),
                 LeagueConfig("jville", "cousin", 2, 3, 2026, "cousin", display_name="JVille 2022"),
                 LeagueConfig("friendly", "ian", 3, 2, 2026, "ian", display_name="Friendly")],
        notify={"to": {"cousin": "NOTIFY_TO_COUSIN", "ian": "NOTIFY_TO_IAN"}})


@pytest.fixture
def owners(monkeypatch):
    monkeypatch.setenv("NOTIFY_TO_COUSIN", COUSIN)
    monkeypatch.setenv("NOTIFY_TO_IAN", IAN)


# ---------------------------------------------------------------- wrong league / team

def test_same_add_in_two_leagues_is_queued_in_both(tmp_path):
    """Player ids are NFL-wide, so the duplicate check must be per league."""
    store_path = tmp_path / "p.json"
    args = SimpleNamespace(store=str(store_path), notify=False)
    cfg = AppConfig([])
    a = LeagueConfig("jville", "cousin", 1, 3, 2026, "cousin")
    c = LeagueConfig("cpmclean", "cousin", 2, 3, 2026, "cousin")
    for lg in (a, c):
        p = free_agent_payload(3, 4, 4429059)
        assert cli._queue(cfg, lg, "FREEAGENT", "add Woody", p, checks_for_add_drop(4429059, None), args,
                          quiet_print=True) == 0
    assert sorted(x.league for x in PendingStore(store_path).open_actions()) == ["cpmclean", "jville"]


def test_cousin_cannot_approve_ians_league_by_text(store, tmp_path, owners):
    store.add("friendly", "TRADE_PROPOSAL", "give X get Y", {}, {})
    sent = []
    reply = chat.respond(COUSIN, "approve 1", two_owner_cfg(), store, lambda p: None,
                         lambda aid, c, s: sent.append(aid) or (0, "sent"), data_dir=tmp_path)
    assert sent == [] and store.get("1").status == "pending"
    assert "No pending action #1" in reply


def test_owner_can_still_approve_own_league_by_text(store, tmp_path, owners):
    store.add("cpmclean", "FREEAGENT", "add X", {}, {})
    sent = []
    chat.respond(COUSIN, "approve 1", two_owner_cfg(), store, lambda p: None,
                 lambda aid, c, s: sent.append(aid) or (0, "sent"), data_dir=tmp_path)
    assert sent == ["1"]


def test_player_on_two_rosters_asks_which_league(store, tmp_path):
    (tmp_path / "snapshot.json").write_text(json.dumps({"leagues": {
        "JVille 2022": {"week": 4, "roster": [{"name": "Lamar Jackson", "pos": "QB"}]},
        "CP McLean": {"week": 4, "roster": [{"name": "Lamar Jackson", "pos": "QB"}]}}}))
    llm = lambda p: json.dumps({"intent": "bench", "player": "Lamar", "confidence": 0.95})
    reply = chat.respond(COUSIN, "sit lamar this week", None, store, llm, lambda *a: (0, ""), data_dir=tmp_path)
    assert not (tmp_path / "preferences.json").exists()
    assert "JVille 2022" in reply and "CP McLean" in reply


# ---------------------------------------------------------------- approval reply parser

def test_llm_cannot_pick_an_id_the_owner_never_typed(store, tmp_path):
    """'Approve tf out of em' names no id. Even a confident LLM guess must be confirmed first."""
    a = store.add("demo", "FREEAGENT", "add Woody", {}, {})
    sent = []
    llm = lambda p: json.dumps({"intent": "approve", "action_id": a.id, "confidence": 0.97})
    reply = chat.respond(COUSIN, "Approve tf out of em", None, store, llm,
                         lambda aid, c, s: sent.append(aid) or (0, "sent"), data_dir=tmp_path)
    assert sent == [] and "Reply yes or no" in reply


def test_explicit_multi_replies_are_parsed_without_the_llm(store, tmp_path):
    a, b_ = store.add("demo", "FREEAGENT", "a", {}, {}), store.add("demo", "FREEAGENT", "b", {}, {})
    boom = lambda p: (_ for _ in ()).throw(AssertionError("LLM called"))
    sent = []
    reply = chat.respond(COUSIN, f"{a.id} yes {b_.id} no", None, store, boom,
                         lambda aid, c, s: sent.append(aid) or (0, f"#{aid} sent."), data_dir=tmp_path)
    assert sent == [a.id] and store.get(b_.id).status == "rejected"
    assert f"#{a.id} sent." in reply and f"#{b_.id} rejected" in reply
    assert chat.parse_multi("approve 4 and 5") == [("approve", "4"), ("approve", "5")]
    assert chat.parse_multi("approve all") is None and chat.parse_multi("yes") is None
    assert chat.parse_multi("4 yes 5") is None              # a bare number with no verb: not explicit


def test_reject_after_approve_does_not_claim_nothing_was_sent(store, tmp_path):
    a = store.add("demo", "FREEAGENT", "add Woody", {}, {})
    store.set_status(a.id, "approved")
    reply = chat.respond(COUSIN, f"reject {a.id}", None, store, lambda p: None, lambda *x: (0, ""),
                         data_dir=tmp_path)
    assert store.get(a.id).status == "approved"
    assert "already approved" in reply and "Nothing sent" not in reply


def test_cli_reject_refuses_a_sent_action(store, capsys):
    a = store.add("demo", "FREEAGENT", "x", {}, {})
    store.set_status(a.id, "approved")
    rc = cli.cmd_reject(AppConfig([]), SimpleNamespace(id=a.id, store=str(store.path)))
    assert rc == 1 and store.get(a.id).status == "approved"
    assert "Nothing was sent" not in capsys.readouterr().out


# ---------------------------------------------------------------- iMessage poller

def _apple(t):
    return int((t - APPLE_EPOCH).total_seconds() * 1e9)


def _db(path, rows):
    """rows: (rowid, text, handle_id, datetime)"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    con.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, "
                "is_from_me INTEGER, handle_id INTEGER, date INTEGER)")
    con.executemany("INSERT INTO handle VALUES (?, ?)", [(1, COUSIN)])
    con.executemany("INSERT INTO message VALUES (?,?,?,?,?,?)",
                    [(r, txt, None, 0, h, _apple(t)) for r, txt, h, t in rows])
    con.commit()
    con.close()


class Cfg:
    notify = {"to": {"cousin": "NOTIFY_TO_COUSIN"}}


class Backend:
    def __init__(self, fail_on=()):
        self.sent, self.fail_on, self.calls = [], set(fail_on), 0

    def send(self, to, text):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("osascript failed")
        self.sent.append((to, text))


def test_poll_failure_midway_never_resends_earlier_replies(store, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_TO_COUSIN", COUSIN)
    now = dt.datetime.now(dt.timezone.utc)
    a, b_ = store.add("demo", "FREEAGENT", "a", {}, {}), store.add("demo", "FREEAGENT", "b", {}, {})
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    st.write_text(json.dumps({"rowid": 0}))
    _db(db, [(1, f"reject {a.id}", 1, now), (2, f"reject {b_.id}", 1, now)])
    be = Backend(fail_on={2})                       # the 2nd reply fails to send
    log = imessage_poll.poll(Cfg(), store, be, lambda *x: (0, ""), db, st, llm=lambda p: None)
    assert any("failed" in line for line in log)
    imessage_poll.poll(Cfg(), store, be, lambda *x: (0, ""), db, st, llm=lambda p: None)
    assert [t for _, t in be.sent].count(f"#{a.id} rejected. Nothing sent.") == 1
    assert json.loads(st.read_text())["rowid"] == 2


def test_backlog_after_access_returns_is_not_acted_on(store, tmp_path, monkeypatch):
    """Old replies (e.g. while Full Disk Access was missing) are skipped, never executed."""
    monkeypatch.setenv("NOTIFY_TO_COUSIN", COUSIN)
    now = dt.datetime.now(dt.timezone.utc)
    a = store.add("demo", "FREEAGENT", "a", {}, {})
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    st.write_text(json.dumps({"rowid": 0}))
    _db(db, [(1, f"approve {a.id}", 1, now - dt.timedelta(hours=30)), (2, "lol", 1, now - dt.timedelta(hours=29))])
    calls, be = [], Backend()
    log = imessage_poll.poll(Cfg(), store, be, lambda aid, c, s: calls.append(aid) or (0, "sent"), db, st,
                             llm=lambda p: (_ for _ in ()).throw(AssertionError("LLM called on stale text")))
    assert calls == [] and store.get(a.id).status == "pending"
    assert json.loads(st.read_text())["rowid"] == 2
    assert len(be.sent) == 1 and "resend" in be.sent[0][1].lower()     # one heads-up, not one per message
    assert any("skipped 2 old" in line for line in log)


def test_yes_sent_before_the_question_does_not_approve(store, tmp_path, monkeypatch):
    """Both texts arrive in one poll: the 'yes' was written before the bot asked anything."""
    monkeypatch.setenv("NOTIFY_TO_COUSIN", COUSIN)
    now = dt.datetime.now(dt.timezone.utc)
    store.add("demo", "TRADE_PROPOSAL", "give A get B", {}, {})
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    st.write_text(json.dumps({"rowid": 0}))
    _db(db, [(1, "do the trade", 1, now - dt.timedelta(minutes=3)), (2, "yes", 1, now - dt.timedelta(minutes=2))])
    labels = iter(['{"intent": "approve", "confidence": 0.9}', '{"intent": "yes", "confidence": 0.9}'])
    calls = []
    imessage_poll.poll(Cfg(), store, Backend(), lambda aid, c, s: calls.append(aid) or (0, "sent"), db, st,
                       llm=lambda p: next(labels))
    assert calls == []


def test_poll_without_disk_access_texts_ian_once_a_day(monkeypatch):
    sent = []

    class Rec:
        def send(self, text):
            sent.append(text)
            return 1
    monkeypatch.setattr(notify, "make_notifier", lambda cfg, owner: sent.append(owner) or Rec())

    def no_access(*a, **k):
        raise imessage_poll.ChatDbUnavailable("no")
    monkeypatch.setattr(imessage_poll, "poll", no_access)
    for _ in range(3):
        assert cli.main(["--quiet", "--config", "config/leagues.example.yaml", "poll-imessage"]) == 0
    assert sent[0] == "ian" and len(sent) == 2 and "Full Disk Access" in sent[1]


# ---------------------------------------------------------------- notifications that fail

class Boom:
    def send(self, to, text):
        raise RuntimeError("osascript: Messages got an error")


def test_notifier_failure_is_reported_not_raised():
    n = notify.Notifier(Boom(), ["+1", "+2"])
    assert n.send("hi") == 0
    assert len(n.failures) == 2


def test_text_failure_does_not_stop_the_other_leagues(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "leagues.yaml"
    cfg_path.write_text(
        "leagues:\n"
        "  - {name: demo, owner: ian, league_id: 99, team_id: 1, year: 2026, auth_profile: ian, fixture: tests/fixtures/demo}\n"
        "  - {name: demo2, owner: ian, league_id: 98, team_id: 1, year: 2026, auth_profile: ian, fixture: tests/fixtures/demo}\n")
    monkeypatch.setattr(notify, "make_notifier", lambda cfg, owner: notify.Notifier(Boom(), ["+1"]))
    rc = cli.main(["--config", str(cfg_path), "--store", str(tmp_path / "p.json"),
                   "autopilot", "--demo", "--no-news", "--notify"])
    out = capsys.readouterr().out
    assert "demo: full:" in out and "demo2: full:" in out
    assert "failed to send" in out and rc != 0


# ---------------------------------------------------------------- partial failures

def test_lineup_text_still_sent_when_only_the_ir_move_fails(demo_state, monkeypatch, tmp_path):
    res = optimize(demo_state)
    hal = demo_state.find_player("Hurt Hal")
    res.ir_moves = [Move(hal.player_id, hal.name, 20, 21, "moved Hurt Hal to IR")]
    lg = LeagueConfig("demo", "ian", 99, 1, 2026, "ian", fixture="tests/fixtures/demo")
    args = SimpleNamespace(store=str(tmp_path / "p.json"), notify=False)
    rc, status, lineup_ok = cli._apply_lineup(AppConfig([lg]), lg, RecordingWriter(fail_ir=True),
                                              demo_state, res, True, args)
    assert rc == 3 and lineup_ok and status == "sent"
    text = notify.msg_lineup_changes("demo", res)
    assert "Hurt Hal: BE → IR" not in text and "IR move not saved" in text


# ---------------------------------------------------------------- stale / duplicate approvals

def test_approve_is_idempotent_even_when_two_pollers_race(store, demo_state):
    a = store.add("demo", "FREEAGENT", "add X", free_agent_payload(1, 4, 903, 304), checks_for_add_drop(903, 304))
    inner = []
    w = RecordingWriter(on_submit=lambda: inner.append(
        cli.approve(a.id, AppConfig([]), store, FakeReader(demo_state), RecordingWriter())))
    rc, msg = cli.approve(a.id, AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 0 and len(w.sent) == 1
    assert inner[0][0] == 1 and "already" in inner[0][1]
    assert store.get(a.id).status == "approved"


def test_pacing_stop_puts_the_action_back_in_the_queue(store, demo_state):
    a = store.add("demo", "FREEAGENT", "add X", free_agent_payload(1, 4, 903, 304), checks_for_add_drop(903, 304))

    class Capped:
        def submit(self, payload, send=False):
            raise PacingStop("daily write cap reached")
    rc, msg = cli.approve(a.id, AppConfig([]), store, FakeReader(demo_state), Capped())
    assert rc == 4 and store.get(a.id).status == "pending"


def test_no_drop_add_aborts_when_the_open_spot_is_gone(store, demo_state):
    extra = demo_state.free_agents[1]
    extra.lineup_slot = 20
    demo_state.my_team.roster.append(extra)          # roster is now full
    a = store.add("demo", "FREEAGENT", "add Meh", free_agent_payload(1, 4, 903), checks_for_add_drop(903, None))
    w = RecordingWriter()
    rc, msg = cli.approve(a.id, AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 1 and w.sent == [] and "roster is full" in msg


# ---------------------------------------------------------------- write caps

def test_one_league_using_the_run_cap_does_not_block_the_next_league(tmp_path):
    p = Pacer({"writes_per_run": 2, "write_gap_s": [0, 0]}, path=tmp_path / "pacing.json",
              sleep=lambda s: None, rand=lambda a, b: 0.0)
    p.record_write("jville")
    p.record_write("jville")
    with pytest.raises(PacingStop):
        p.check_write("jville")
    p.check_write("cpmclean")                         # its own lineup still goes through


def test_paused_text_does_not_mention_a_cooldown_for_a_write_cap():
    assert "cooldown" not in notify.msg_paused("JVille", "daily write cap reached (6 per league)")


# ---------------------------------------------------------------- text vs decision numbers

def test_rest_of_season_pro_only_when_it_counted_in_the_gain():
    from test_approval_texts import two_te_league
    s = two_te_league()
    sg = waivers.evaluate_add(s, s.find_player("Woody Rb"), cfg={"min_gain_free_agent": 0})
    sg.drop = s.find_player("Pitts Te")
    sg.add_value, sg.drop_value, sg.add_vorp, sg.drop_vorp, sg.ros_term = 95, 8, 0, 0, 0.0
    pros = approval.waiver_text(s, sg).split("Cons:")[0]
    assert "95 vs 8" not in pros
    sg.add_vorp, sg.drop_vorp, sg.ros_term = 40, 10, 2.0
    pros = approval.waiver_text(s, sg).split("Cons:")[0]
    assert "40 vs 10 above replacement" in pros


def test_gain_parts_add_up_to_the_gain_shown():
    from test_approval_texts import two_te_league
    s = two_te_league()
    sg = waivers.evaluate_add(s, s.find_player("Woody Rb"), cfg={"min_gain_free_agent": 0})
    sg.gain, sg.lineup_gain, sg.ros_term = 4.0, 1.45, 2.54        # shown alone: 1.4 + 2.5 = 3.9
    text = approval.waiver_text(s, sg)
    import re
    m = re.search(r"gain \+(\d+\.\d) pts over 3 wks: lineup \+(\d+\.\d).*?rest-of-season value \+(\d+\.\d)", text)
    assert m and round(float(m.group(2)) + float(m.group(3)), 1) == float(m.group(1))


def test_readback_failure_after_a_successful_post_still_counts_as_sent(cfg):
    import httpx
    from fantasy_ai.config import Credentials
    from fantasy_ai.espn_write import TransactionWriter

    def readback():
        raise PacingStop("ESPN pushed back (HTTP 503), paused for 24 h")
    w = TransactionWriter(cfg, Credentials("s2", "sw"), readback=readback,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "EXECUTED"})))
    res = w.submit(free_agent_payload(3, 4, 903), send=True)
    assert res.sent and "read-back failed" in res.message


def test_a_locked_or_changed_chat_db_is_not_reported_as_missing_disk_access(store, tmp_path):
    db, st = tmp_path / "chat.db", tmp_path / "last.json"
    st.write_text(json.dumps({"rowid": 0}))
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY)")      # schema without the columns we read
    con.commit()
    con.close()
    with pytest.raises(sqlite3.OperationalError):
        imessage_poll.poll(Cfg(), store, Backend(), lambda *a: (0, ""), db, st, llm=lambda p: None)
    with pytest.raises(imessage_poll.ChatDbUnavailable):
        imessage_poll.poll(Cfg(), store, Backend(), lambda *a: (0, ""), tmp_path / "missing" / "chat.db", st,
                           llm=lambda p: None)


def test_autopilot_does_not_requeue_an_idea_the_owner_rejected_this_week(tmp_path, capsys):
    store_path = tmp_path / "p.json"
    argv = ["--config", "config/leagues.example.yaml", "--store", str(store_path),
            "autopilot", "--demo", "--no-news", "--waivers"]
    cli.main(argv)
    store = PendingStore(store_path)
    first = store.open_actions()
    assert first
    for a in first:
        cli.reject(a.id, store)
    cli.main(argv)
    assert store.open_actions() == []


def test_errors_in_the_cousins_league_also_reach_ian(tmp_path, monkeypatch):
    cfg_path = tmp_path / "leagues.yaml"
    cfg_path.write_text(
        "leagues:\n"
        "  - {name: demo, owner: cousin, league_id: 99, team_id: 1, year: 2026, auth_profile: cousin, fixture: tests/fixtures/demo}\n")
    sent = []

    class Rec:
        failures = []

        def __init__(self, owner):
            self.owner = owner

        def send(self, text):
            sent.append((self.owner, text))
            return 1
    monkeypatch.setattr(notify, "make_notifier", lambda cfg, owner: Rec(owner))

    def boom(*a, **k):
        raise RuntimeError("optimizer blew up")
    monkeypatch.setattr(cli, "optimize", boom)
    rc = cli.main(["--config", str(cfg_path), "--store", str(tmp_path / "p.json"),
                   "autopilot", "--demo", "--no-news", "--notify"])
    assert rc == 1
    assert any(o == "cousin" for o, _ in sent)
    assert any(o == "ian" and "optimizer blew up" in t for o, t in sent)


def test_approve_aborts_when_the_add_is_already_on_our_roster(store, demo_state):
    """The Woody Marks case: if ESPN already shows him on our team, approving sends nothing."""
    ours = demo_state.roster[0].player_id
    a = store.add("demo", "FREEAGENT", "add", free_agent_payload(1, 4, ours), checks_for_add_drop(ours, None))
    w = RecordingWriter()
    rc, msg = cli.approve(a.id, AppConfig([]), store, FakeReader(demo_state), w)
    assert rc == 1 and w.sent == [] and "already on your roster" in msg
    rc, msg = cli.approve(a.id, AppConfig([]), store, FakeReader(demo_state), w)     # and again: still nothing
    assert rc == 1 and w.sent == []


def test_trade_idea_request_cannot_target_another_owners_league(store, tmp_path, owners):
    llm = lambda p: json.dumps({"intent": "trade_idea", "player": "Nobody Known", "league": "friendly",
                                "confidence": 0.9})
    chat.respond(COUSIN, "find me a trade for nobody", two_owner_cfg(), store, llm, lambda *a: (0, ""),
                 data_dir=tmp_path)
    assert json.loads((tmp_path / "requests.json").read_text())[0]["league"] is None
