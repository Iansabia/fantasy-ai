"""CI safety: quiet output, Telegram approvals, encrypted state, autopilot. All offline."""
import json

import httpx
import pytest

from fantasy_ai import cli, notify, state
from fantasy_ai.config import AppConfig, load_config
from fantasy_ai.espn_write import free_agent_payload
from fantasy_ai.pending import PendingStore, checks_for_add_drop
from fantasy_ai.telegram_poll import poll

DEMO_NAMES = ["Quinn Arm", "Ready Rex", "Hurt Hal", "Waiver Wonder", "My Team", "Rival", "playerId", "11111111"]


def test_quiet_autopilot_prints_counts_only(tmp_path, capsys):
    rc = cli.main(["--quiet", "--config", "config/leagues.example.yaml", "--store", str(tmp_path / "p.json"),
                   "autopilot", "--demo", "--no-news", "--waivers", "--notify"])
    out = capsys.readouterr()
    text = out.out + out.err
    import re
    assert rc == 0 and re.search(r"demo: full: 6 moves, dry-run; [12] queued for approval", text), text
    for secret in DEMO_NAMES + ["DRY RUN", "{"]:
        assert secret not in text


def test_quiet_hides_error_details(capsys):
    rc = cli.main(["--quiet", "--config", "config/leagues.example.yaml", "lineup", "no-such-league"])
    text = capsys.readouterr().out
    assert rc == 1 and "no-such-league" not in text


def test_leagues_yaml_env(monkeypatch):
    monkeypatch.setenv("LEAGUES_YAML", "leagues:\n - {name: x, league_id: 1, team_id: 2, year: 2026, auth_profile: cousin}\n")
    cfg = load_config()
    assert cfg.leagues[0].team_id == 2 and cfg.source is None


def test_parse_reply():
    assert notify.parse_reply("approve 4") == ("approve", "4")
    assert notify.parse_reply(" Reject #12 ") == ("reject", "12")
    assert notify.parse_reply("/approve 3") == ("approve", "3")
    assert notify.parse_reply("approve everything") is None
    assert notify.parse_reply("approve 4; reject 5") is None


class FakeTelegram:
    def __init__(self, updates):
        self.updates, self.sent = updates, []

    def call(self, method, **p):
        assert method == "getUpdates"
        return {"result": [u for u in self.updates if u["update_id"] >= p["offset"]]}

    def send(self, to, text):
        self.sent.append((to, text))


def test_telegram_poll_only_trusts_configured_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    store = PendingStore(tmp_path / "p.json")
    store.add("demo", "FREEAGENT", "x", free_agent_payload(1, 4, 903, 304), checks_for_add_drop(903, 304))
    store.add("demo", "FREEAGENT", "y", free_agent_payload(1, 4, 904, 601), checks_for_add_drop(904, 601))
    tg = FakeTelegram([
        {"update_id": 10, "message": {"chat": {"id": 999}, "text": "approve 1"}},   # stranger
        {"update_id": 11, "message": {"chat": {"id": 111}, "text": "approve 1"}},
        {"update_id": 12, "message": {"chat": {"id": 111}, "text": "reject 2"}},
        {"update_id": 13, "message": {"chat": {"id": 111}, "text": "hello"}},
    ])
    approved = []

    def fake_approve(aid, cfg, st):
        approved.append(aid)
        return 0, f"#{aid} sent."

    log = poll(AppConfig([]), store, tg, fake_approve, tmp_path / "off.json")
    assert approved == ["1"]                      # stranger's message ignored
    assert store.get("2").status == "rejected"
    assert [t for _, t in tg.sent] == ["#1 sent.", "#2 rejected. Nothing sent."]
    assert json.loads((tmp_path / "off.json").read_text())["offset"] == 14
    assert "ignored message from unknown chat" in log
    # re-poll: nothing processed twice
    poll(AppConfig([]), store, tg, fake_approve, tmp_path / "off.json")
    assert approved == ["1"]


def test_telegram_backend_mock(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"ok": True})
    notify.TelegramBackend(transport=httpx.MockTransport(handler)).send("111", "hi")
    assert seen["url"].endswith("/bot123:abc/sendMessage") and seen["body"]["chat_id"] == "111"


def test_state_roundtrip_encrypted(tmp_path):
    pytest.importorskip("cryptography")
    key = state.keygen()
    data = tmp_path / "data"
    data.mkdir()
    (data / "pending.json").write_text('{"secret": "Waiver Wonder"}')
    out = tmp_path / "s.enc"
    state.pack(data, out, key)
    assert b"Waiver Wonder" not in out.read_bytes()
    (data / "pending.json").unlink()
    assert state.unpack(out, data, key)
    assert "Waiver Wonder" in (data / "pending.json").read_text()
    with pytest.raises(Exception):
        state.unpack(out, data, state.keygen())   # wrong key fails
