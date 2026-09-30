"""Write side: build ESPN transaction payloads. Dry-run by default.

Sources (copies in refs/, fetched 2026-09-30):
- heyitaki/espn-fantasy-football-mcp src/espn/constants.ts (enums, transactionUrl) and
  src/espn/transactions.ts (lineupPayload, freeAgentPayload, waiverPayload, cancelWaiverPayload).
- jwulff/fantasy-sports docs/research/05-espn-write-surface.md: live-captured ROSTER/LINEUP
  request (section 2.1), required fields (2.2), atomic multi-item POST (3), current scoring
  period only (4), typed 409 rejections (5), read back before retry (8.1).
- garavitgabriel/espn-fantasy-claude-openclaw espn_api/baseball/league.py propose_trade /
  cancel_trade: TRADE_PROPOSAL item shape (jwulff section 10 cites it). Community-attested only.

Status of each shape:
  ROSTER/LINEUP   captured live on the wire (jwulff p3).
  FREEAGENT, WAIVER, CANCEL, TRADE_PROPOSAL   community code only, never confirmed by capture.

Safety:
- Nothing is sent unless send=True (CLI: --send).
- Every payload passes the own-team guard first, even in dry-run.
- memberId (the SWID) is optional per jwulff 2.2 and is left out, so dry-run output never
  contains a secret. Only the espn_s2 cookie (plus SWID cookie if set) goes on the wire.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx

from .config import Credentials, LeagueConfig
from .lineup import Move

# heyitaki constants.ts WRITES_BASE / transactionUrl(): trailing slash as the web client sends it.
WRITES_BASE = "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl"

# heyitaki constants.ts TRANSACTION_TYPE / EXECUTION_TYPE / ITEM_TYPE
TX_ROSTER, TX_FREEAGENT, TX_WAIVER, TX_TRADE_PROPOSAL = "ROSTER", "FREEAGENT", "WAIVER", "TRADE_PROPOSAL"
EXEC_EXECUTE, EXEC_CANCEL = "EXECUTE", "CANCEL"
ITEM_ADD, ITEM_DROP, ITEM_LINEUP, ITEM_TRADE = "ADD", "DROP", "LINEUP", "TRADE"

# jwulff 2.1 captured x-fantasy-platform: kona-PROD (heyitaki sends espn-fantasy-web);
# jwulff 2.2: both optional. We match the live capture.
WRITE_HEADERS = {
    "Content-Type": "application/json",
    "x-fantasy-source": "kona",
    "x-fantasy-platform": "kona-PROD",
}


def transaction_url(year: int, league_id: int) -> str:
    return f"{WRITES_BASE}/seasons/{year}/segments/0/leagues/{league_id}/transactions/"


# ---------------------------------------------------------------- payload builders

def _base(team_id: int, scoring_period: int, tx_type: str, execution: str = EXEC_EXECUTE) -> dict:
    # heyitaki transactions.ts basePayload(); memberId omitted (optional, jwulff 2.2).
    return {
        "isLeagueManager": False,
        "teamId": team_id,
        "type": tx_type,
        "scoringPeriodId": scoring_period,
        "executionType": execution,
    }


def lineup_payload(team_id: int, scoring_period: int, moves: Iterable[Move]) -> dict:
    """ROSTER + LINEUP items (jwulff 2.1, heyitaki lineupPayload). Only changed slots."""
    items = []
    for m in moves:
        if m.from_slot == m.to_slot:
            raise ValueError(f"{m.name}: from == to slot; ESPN rejects this as TRAN_ROSTER_SAME_SLOT")
        items.append({"playerId": m.player_id, "type": ITEM_LINEUP,
                      "fromLineupSlotId": m.from_slot, "toLineupSlotId": m.to_slot})
    if not items:
        raise ValueError("No lineup changes; ESPN has no no-op transaction.")
    return {**_base(team_id, scoring_period, TX_ROSTER), "items": items}


def free_agent_payload(team_id: int, scoring_period: int, add_id: int | None = None,
                       drop_id: int | None = None) -> dict:
    """FREEAGENT + ADD (toTeamId) / DROP (fromTeamId) (heyitaki freeAgentPayload)."""
    items = []
    if add_id is not None:
        items.append({"playerId": add_id, "type": ITEM_ADD, "toTeamId": team_id})
    if drop_id is not None:
        items.append({"playerId": drop_id, "type": ITEM_DROP, "fromTeamId": team_id})
    if not items:
        raise ValueError("Need a player to add or drop.")
    return {**_base(team_id, scoring_period, TX_FREEAGENT), "items": items}


def waiver_payload(team_id: int, scoring_period: int, add_id: int, drop_id: int | None = None,
                   bid: int = 0) -> dict:
    """WAIVER claim with bidAmount (heyitaki waiverPayload: freeAgent items, type WAIVER)."""
    if bid < 0:
        raise ValueError("bid must be >= 0")
    p = free_agent_payload(team_id, scoring_period, add_id, drop_id)
    return {**p, "type": TX_WAIVER, "bidAmount": int(bid)}


def cancel_waiver_payload(team_id: int, scoring_period: int, transaction_id: str) -> dict:
    """Withdraw a pending claim (heyitaki cancelWaiverPayload: no items key)."""
    return {**_base(team_id, scoring_period, TX_WAIVER, EXEC_CANCEL), "relatedTransactionId": transaction_id}


def trade_proposal_payload(team_id: int, partner_team_id: int, scoring_period: int,
                           give_ids: list[int], get_ids: list[int], comment: str = "",
                           expiration: str | None = None) -> dict:
    """TRADE_PROPOSAL (garavitgabriel propose_trade): one TRADE item per player with
    fromTeamId/toTeamId; expirationDate ISO-8601 UTC, default two days out."""
    if not give_ids or not get_ids:
        raise ValueError("A trade needs players on both sides.")
    items = [{"playerId": pid, "type": ITEM_TRADE, "fromTeamId": team_id, "toTeamId": partner_team_id} for pid in give_ids]
    items += [{"playerId": pid, "type": ITEM_TRADE, "fromTeamId": partner_team_id, "toTeamId": team_id} for pid in get_ids]
    if expiration is None:
        expiration = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {**_base(team_id, scoring_period, TX_TRADE_PROPOSAL), "items": items,
            "expirationDate": expiration, "comment": comment}


def cancel_trade_payload(team_id: int, scoring_period: int, transaction_id: str) -> dict:
    """Withdraw our own pending proposal (garavitgabriel cancel_trade: empty items)."""
    return {**_base(team_id, scoring_period, TX_TRADE_PROPOSAL, EXEC_CANCEL), "items": [],
            "relatedTransactionId": transaction_id}


# ---------------------------------------------------------------- own-team guard

class OwnTeamViolation(Exception):
    pass


def check_own_team(payload: dict, cfg: LeagueConfig) -> None:
    """Refuse anything that acts on a team other than the configured one.
    Needed because a commissioner's cookie can write other teams (jwulff 0, row 6; 6.2)."""
    own = cfg.team_id
    if not own:
        raise OwnTeamViolation(f"League '{cfg.name}' has no team_id configured; refusing to write.")
    if payload.get("teamId") != own:
        raise OwnTeamViolation(f"Payload teamId {payload.get('teamId')} is not your team ({own}) in '{cfg.name}'.")
    if payload.get("isLeagueManager"):
        raise OwnTeamViolation("League-manager writes are not allowed.")
    for it in payload.get("items", []):
        t = it.get("type")
        if t == ITEM_ADD and it.get("toTeamId") != own:
            raise OwnTeamViolation(f"ADD targets team {it.get('toTeamId')}, not yours ({own}).")
        if t == ITEM_DROP and it.get("fromTeamId") != own:
            raise OwnTeamViolation(f"DROP from team {it.get('fromTeamId')}, not yours ({own}).")
        if t == ITEM_TRADE and own not in (it.get("fromTeamId"), it.get("toTeamId")):
            raise OwnTeamViolation("Trade item does not involve your team.")
        if t == ITEM_TRADE and it.get("fromTeamId") == it.get("toTeamId"):
            raise OwnTeamViolation("Trade item from and to the same team.")


# ---------------------------------------------------------------- errors

# jwulff 5.1: observed typed rejections, mapped to plain language.
ERROR_MESSAGES = {
    "TRAN_LINEUP_LOCKED": "A player in this move is locked because his game has started. Nothing was changed.",
    "TRAN_ROSTER_SLOT_LIMIT_EXCEEDED": "Too many players in one slot. Nothing was changed; re-read the roster and re-plan.",
    "TRAN_ROSTER_SAME_SLOT": "A player is already in that slot (roster changed since we read it). Nothing was changed.",
    "TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT": "Lineups can only be set for the current week. Nothing was changed.",
    "AUTH_MISSING_CREDENTIALS": "ESPN refused the cookies. Check espn_s2 in .env (it may have expired) and that this account is in the league.",
    "HTTP_METHOD_NOT_SUPPORTED": "Wrong HTTP method for the write endpoint (bug in fantasy_ai).",
}


class WriteRejected(Exception):
    def __init__(self, status: int, espn_type: str | None, message: str):
        super().__init__(message)
        self.status, self.espn_type, self.message = status, espn_type, message


def explain_error(status: int, body: dict | None) -> WriteRejected:
    details = (body or {}).get("details") or []
    etype = details[0].get("type") if details else None
    espn_msg = details[0].get("message") if details else "; ".join((body or {}).get("messages", []))
    if etype in ERROR_MESSAGES:
        return WriteRejected(status, etype, ERROR_MESSAGES[etype])
    if status == 400 and not details:
        return WriteRejected(status, None, "ESPN said 'Invalid Input' with no reason: the request shape is probably out of date. Do not retry; update fantasy_ai.")
    if status == 409:
        return WriteRejected(status, etype, f"ESPN rejected the move ({etype}): {espn_msg}. Nothing was changed.")
    return WriteRejected(status, etype, f"ESPN returned HTTP {status} ({etype}): {espn_msg}")


# ---------------------------------------------------------------- send / dry-run

@dataclass
class WriteResult:
    sent: bool
    url: str
    payload: dict
    response: dict | None = None
    message: str = ""


def describe_dry_run(url: str, payload: dict) -> str:
    return f"DRY RUN (nothing sent). Would POST to:\n  {url}\nwith JSON body:\n{json.dumps(payload, indent=2)}"


class TransactionWriter:
    """send=False (default) only prints. send=True posts ONCE, then verifies by reading back.

    readback: returns {playerId: lineupSlotId} for our team. After a timeout it tells us whether
    the write landed (jwulff 8.1: a timed-out write is not a failed write). There are no
    automatic retries at all: if the read-back shows nothing changed, the next scheduled run
    tries again. 401/403/429 or any unexpected non-409 status sets a pacing cooldown.
    """

    def __init__(self, cfg: LeagueConfig, creds: Credentials | None,
                 readback: Callable[[], dict[int, int]] | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 15.0, pacer=None):
        from .pacing import NoPacer, Pacer
        self.cfg, self.creds, self.readback = cfg, creds, readback
        self.transport, self.timeout = transport, timeout
        self.pacer = pacer or (NoPacer() if cfg.fixture_dir or transport is not None else Pacer())

    def submit(self, payload: dict, send: bool = False) -> WriteResult:
        check_own_team(payload, self.cfg)          # always, dry-run included
        url = transaction_url(self.cfg.year, self.cfg.league_id)
        if not send:
            return WriteResult(False, url, payload, message=describe_dry_run(url, payload))
        if self.cfg.fixture_dir:
            raise OwnTeamViolation("This league is an offline fixture; refusing to send.")
        if not self.creds:
            raise WriteRejected(0, None, f"No cookies for profile '{self.cfg.auth_profile}'. Add ESPN_S2_{self.cfg.auth_profile.upper()} to .env.")
        self.pacer.check_write(self.cfg.name)      # cooldown, per-run and per-day caps, write gap
        try:
            return self._post_with_readback(url, payload)
        except WriteRejected as e:
            if e.status in (401, 403, 429) or (e.status >= 300 and e.status not in (400, 409)):
                from .pacing import PacingStop
                raise PacingStop(self.pacer.cooldown(self.cfg.name, f"HTTP {e.status}")) from e
            raise

    def _post(self, url: str, payload: dict) -> dict:
        with httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False,
                          cookies=self.creds.cookies()) as client:  # no redirects: never leak the cookie to another host
            from .pacing import BROWSER_UA
            r = client.post(url, json=payload, headers={**WRITE_HEADERS, "User-Agent": BROWSER_UA})
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            raise explain_error(r.status_code, body)
        return body or {}

    def _applied(self, payload: dict) -> bool | None:
        """Did the payload take effect? None = cannot tell from the roster."""
        if not self.readback:
            return None
        now = self.readback()
        items = payload.get("items", [])
        if payload["type"] == TX_ROSTER:
            return all(now.get(i["playerId"]) == i["toLineupSlotId"] for i in items)
        if payload["type"] == TX_FREEAGENT:
            adds = [i["playerId"] for i in items if i["type"] == ITEM_ADD]
            drops = [i["playerId"] for i in items if i["type"] == ITEM_DROP]
            return all(a in now for a in adds) and all(d not in now for d in drops)
        return None  # WAIVER / TRADE land as pending, not on the roster

    def _post_with_readback(self, url: str, payload: dict) -> WriteResult:
        try:
            resp = self._post(url, payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # Unknown state: read the roster back. Never resend here.
            applied = self._applied(payload)
            self.pacer.record_write(self.cfg.name)
            if applied:
                return WriteResult(True, url, payload, None, "Network error, but the roster shows the change went through.")
            if applied is False:
                raise WriteRejected(0, None, f"Network error ({type(e).__name__}); read-back shows nothing changed. "
                                             "Not retrying; the next scheduled run will try again.") from e
            raise WriteRejected(0, None, f"Network error ({type(e).__name__}) and the result is unknown. "
                                         "Check the league's transactions page; do not resend blindly.") from e
        self.pacer.record_write(self.cfg.name)
        status = resp.get("status", "")
        applied = self._applied(payload)
        msg = f"ESPN status: {status or 'unknown'}."
        if applied is False:
            msg += " WARNING: read-back does not match the request; check the roster on ESPN."
        elif applied:
            msg += " Read-back confirms the change."
        return WriteResult(True, url, payload, resp, msg)
