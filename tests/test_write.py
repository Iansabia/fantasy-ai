"""Payload shapes vs the reference repos, own-team guard, dry-run, error mapping.
No test ever reaches ESPN: the conftest socket guard fails any real connection, and
sends go through httpx.MockTransport."""
import httpx
import pytest

from fantasy_ai.espn_write import (ERROR_MESSAGES, OwnTeamViolation, TransactionWriter, WriteRejected,
                                   cancel_trade_payload, cancel_waiver_payload, check_own_team,
                                   free_agent_payload, lineup_payload, trade_proposal_payload,
                                   transaction_url, waiver_payload)
from fantasy_ai.config import Credentials
from fantasy_ai.lineup import Move


def test_url_matches_heyitaki_transactionUrl():
    assert transaction_url(2026, 11111111) == (
        "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/11111111/transactions/")


def test_lineup_payload_matches_jwulff_capture():
    # jwulff 05-espn-write-surface.md 2.1 (p3), minus the optional memberId (2.2)
    p = lineup_payload(1, 1, [Move(9000013, "a", 4, 20), Move(9000012, "b", 20, 4)])
    assert p == {
        "isLeagueManager": False, "teamId": 1, "type": "ROSTER", "scoringPeriodId": 1,
        "executionType": "EXECUTE",
        "items": [
            {"playerId": 9000013, "type": "LINEUP", "fromLineupSlotId": 4, "toLineupSlotId": 20},
            {"playerId": 9000012, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 4},
        ],
    }
    assert "memberId" not in p


def test_lineup_payload_refuses_noop():
    with pytest.raises(ValueError):
        lineup_payload(1, 1, [Move(1, "a", 2, 2)])
    with pytest.raises(ValueError):
        lineup_payload(1, 1, [])


def test_free_agent_payload_matches_heyitaki():
    p = free_agent_payload(3, 4, add_id=11, drop_id=22)
    assert p["type"] == "FREEAGENT" and p["executionType"] == "EXECUTE"
    assert p["items"] == [{"playerId": 11, "type": "ADD", "toTeamId": 3},
                          {"playerId": 22, "type": "DROP", "fromTeamId": 3}]


def test_waiver_payload_matches_heyitaki_and_defaults_bid_zero():
    p = waiver_payload(3, 4, add_id=11, drop_id=22)
    assert p["type"] == "WAIVER" and p["bidAmount"] == 0
    assert p["items"][0] == {"playerId": 11, "type": "ADD", "toTeamId": 3}
    assert waiver_payload(3, 4, 11, bid=7)["bidAmount"] == 7


def test_cancel_payloads():
    c = cancel_waiver_payload(3, 4, "abc")
    assert c["type"] == "WAIVER" and c["executionType"] == "CANCEL" and c["relatedTransactionId"] == "abc"
    assert "items" not in c   # heyitaki cancelWaiverPayload has no items
    t = cancel_trade_payload(3, 4, "xyz")
    assert t["type"] == "TRADE_PROPOSAL" and t["executionType"] == "CANCEL" and t["items"] == []


def test_trade_proposal_matches_garavitgabriel():
    p = trade_proposal_payload(3, 7, 4, [1, 2], [9], expiration="2026-10-02T00:00:00.000Z")
    assert p["type"] == "TRADE_PROPOSAL" and p["teamId"] == 3
    assert p["items"] == [
        {"playerId": 1, "type": "TRADE", "fromTeamId": 3, "toTeamId": 7},
        {"playerId": 2, "type": "TRADE", "fromTeamId": 3, "toTeamId": 7},
        {"playerId": 9, "type": "TRADE", "fromTeamId": 7, "toTeamId": 3},
    ]
    assert p["expirationDate"] == "2026-10-02T00:00:00.000Z" and p["comment"] == ""


# ---- own-team guard

def test_guard_accepts_own_team(cfg):
    check_own_team(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), cfg)
    check_own_team(free_agent_payload(3, 4, 1, 2), cfg)
    check_own_team(trade_proposal_payload(3, 7, 4, [1], [2]), cfg)


@pytest.mark.parametrize("payload", [
    lineup_payload(5, 4, [Move(1, "a", 2, 20)]),                       # another team's lineup
    free_agent_payload(5, 4, 1, 2),                                     # another team's add/drop
    {**free_agent_payload(3, 4, 1, 2), "items": [{"playerId": 2, "type": "DROP", "fromTeamId": 5}]},
    {**free_agent_payload(3, 4, 1), "items": [{"playerId": 1, "type": "ADD", "toTeamId": 5}]},
    trade_proposal_payload(5, 7, 4, [1], [2]),
    {**trade_proposal_payload(3, 7, 4, [1], [2]), "items": [{"playerId": 1, "type": "TRADE", "fromTeamId": 6, "toTeamId": 7}]},
    {**lineup_payload(3, 4, [Move(1, "a", 2, 20)]), "isLeagueManager": True},
])
def test_guard_refuses_other_teams(cfg, payload):
    with pytest.raises(OwnTeamViolation):
        check_own_team(payload, cfg)


def test_guard_refuses_unconfigured_team(cfg):
    cfg.team_id = 0
    with pytest.raises(OwnTeamViolation):
        check_own_team(lineup_payload(0, 4, [Move(1, "a", 2, 20)]), cfg)


def _explode(request):
    raise AssertionError("network used")


def test_dry_run_sends_nothing_and_prints_payload(cfg):
    w = TransactionWriter(cfg, Credentials("s2secret", "{swid}"), transport=httpx.MockTransport(_explode))
    res = w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]))
    assert res.sent is False and "DRY RUN" in res.message and '"toLineupSlotId": 20' in res.message
    assert "s2secret" not in res.message and "{swid}" not in res.message


def test_send_to_other_team_refused_before_network(cfg):
    w = TransactionWriter(cfg, Credentials("s2", "sw"), transport=httpx.MockTransport(_explode))
    with pytest.raises(OwnTeamViolation):
        w.submit(lineup_payload(4, 4, [Move(1, "a", 2, 20)]), send=True)


def test_credentials_repr_hides_secrets():
    assert "abc" not in repr(Credentials("abc", "def")) and "def" not in str(Credentials("abc", "def"))


# ---- error mapping with a mock transport (not ESPN)

@pytest.mark.parametrize("etype", ["TRAN_LINEUP_LOCKED", "TRAN_ROSTER_SLOT_LIMIT_EXCEEDED",
                                   "TRAN_ROSTER_SAME_SLOT", "TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT"])
def test_409_mapped_to_plain_message(cfg, etype):
    body = {"messages": ["x"], "details": [{"type": etype, "message": "x"}]}
    t = httpx.MockTransport(lambda r: httpx.Response(409, json=body))
    w = TransactionWriter(cfg, Credentials("s2", "sw"), readback=lambda: {}, transport=t)
    with pytest.raises(WriteRejected) as e:
        w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    assert e.value.message == ERROR_MESSAGES[etype] and e.value.espn_type == etype


def test_400_without_details_is_schema_drift(cfg):
    t = httpx.MockTransport(lambda r: httpx.Response(400, json={"messages": ["Invalid Input."]}))
    w = TransactionWriter(cfg, Credentials("s2", "sw"), transport=t)
    with pytest.raises(WriteRejected, match="out of date"):
        w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)


def test_timeout_reads_back_before_retry(cfg):
    calls = {"post": 0, "readback": 0}

    def handler(request):
        calls["post"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    def readback():
        calls["readback"] += 1
        return {1: 20}   # the move did land

    w = TransactionWriter(cfg, Credentials("s2", "sw"), readback=readback, transport=httpx.MockTransport(handler))
    res = w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    assert calls == {"post": 1, "readback": 1} and "went through" in res.message


def test_timeout_on_waiver_never_blind_retries(cfg):
    calls = {"post": 0}

    def handler(request):
        calls["post"] += 1
        raise httpx.ConnectTimeout("slow", request=request)

    w = TransactionWriter(cfg, Credentials("s2", "sw"), readback=lambda: {}, transport=httpx.MockTransport(handler))
    with pytest.raises(WriteRejected, match="not retrying|do not resend"):
        w.submit(waiver_payload(3, 4, 11, 22), send=True)
    assert calls["post"] == 1


def test_success_verified_by_readback(cfg):
    seen = {}

    def handler(request):
        seen["cookie"] = request.headers.get("cookie", "")
        seen["platform"] = request.headers.get("x-fantasy-platform")
        return httpx.Response(200, json={"status": "EXECUTED"})

    w = TransactionWriter(cfg, Credentials("s2val", ""), readback=lambda: {1: 20}, transport=httpx.MockTransport(handler))
    res = w.submit(lineup_payload(3, 4, [Move(1, "a", 2, 20)]), send=True)
    assert res.sent and "confirms" in res.message
    assert "espn_s2=s2val" in seen["cookie"] and seen["platform"] == "kona-PROD"
