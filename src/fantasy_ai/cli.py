"""`fai` command line.

  fai leagues
  fai lineup <league> [--send] [--notify]
  fai waivers <league> [--pick N] [--bid B] [--notify]       (--pick queues an approval)
  fai trade <league> --give X --get Y [--propose] [--notify]  (--propose queues an approval)
  fai pending | fai approve <id> | fai reject <id>
  fai report [league] [--llm] [--notify]
  fai autopilot [--send] [--waivers] [--no-news] [--notify]
  fai poll-telegram
  fai state keygen|pack|unpack

--quiet (or FAI_QUIET=1): stdout shows only counts like "league 1: 2 moves, sent".
No names, players, payloads, ids or phone numbers. Use it in public CI logs.

Lineups: dry-run unless --send (own team only).
Waivers, add/drops and trades: never sent directly. They go to the approval queue and are
sent only by `fai approve <id>`, after a fresh read confirms nothing changed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys

from . import insights, notify
from .config import AppConfig, LeagueConfig, load_config, load_credentials
from .espn_read import EspnReader
from .espn_write import (OwnTeamViolation, TransactionWriter, WriteRejected, check_own_team,
                         describe_dry_run, free_agent_payload, lineup_payload,
                         trade_proposal_payload, transaction_url, waiver_payload)
from .lineup import DISPLAY_ORDER, optimize
from .pending import PendingStore, checks_for_add_drop, checks_for_trade, verify
from .trades import evaluate_trade
from .waivers import rank_free_agents


SUMMARY: list[str] = []   # the only lines printed in --quiet mode (no identifying details)


def summary(line: str) -> None:
    SUMMARY.append(line)


def _setup(cfg: AppConfig, name: str) -> tuple[LeagueConfig, EspnReader, TransactionWriter]:
    lg = cfg.get(name)
    if not lg.fixture_dir and (not lg.league_id or not lg.team_id):
        raise SystemExit(f"League '{lg.name}' has no league_id/team_id yet. Edit {cfg.source}.")
    creds = None if lg.fixture_dir else load_credentials(lg.auth_profile)
    if not lg.fixture_dir and creds is None:
        # private leagues need cookies; don't make pointless unauthenticated calls to ESPN
        raise SystemExit(f"No cookies for '{lg.name}'. Add ESPN_S2_{lg.auth_profile.upper()} and "
                         f"SWID_{lg.auth_profile.upper()} to .env (see README).")
    reader = EspnReader(lg, creds)
    writer = TransactionWriter(lg, creds, readback=reader.roster_slots)
    return lg, reader, writer


def _store(args) -> PendingStore:
    return PendingStore(args.store) if getattr(args, "store", None) else PendingStore()


def _notifier(cfg: AppConfig, lg: LeagueConfig, args) -> notify.Notifier | None:
    return notify.make_notifier(cfg.notify, lg.owner) if getattr(args, "notify", False) else None


def _submit(writer: TransactionWriter, payload: dict, send: bool) -> int:
    try:
        res = writer.submit(payload, send=send)
    except OwnTeamViolation as e:
        print(f"REFUSED (own-team guard): {e}")
        return 2
    except WriteRejected as e:
        print(f"ESPN rejected the write: {e.message}")
        return 3
    print(res.message)
    return 0


def _queue(cfg: AppConfig, lg: LeagueConfig, kind: str, summary: str, payload: dict, checks: dict, args) -> int:
    try:
        check_own_team(payload, lg)
    except OwnTeamViolation as e:
        print(f"REFUSED (own-team guard): {e}")
        return 2
    print(describe_dry_run(transaction_url(lg.year, lg.league_id), payload))
    a = _store(args).add(lg.name, kind, summary, payload, checks)
    print(f"\nQueued as #{a.id} (expires {a.expires[:16]} UTC). Send it with: fai approve {a.id}")
    n = _notifier(cfg, lg, args)
    if n:
        n.send(notify.msg_pending(a))
    return 0


def cmd_leagues(cfg: AppConfig, args) -> int:
    print(f"Config: {cfg.source}")
    for lg in cfg.leagues:
        mode = "offline fixture" if lg.fixture_dir else (
            "cookies OK" if load_credentials(lg.auth_profile) else f"no cookies (ESPN_S2_{lg.auth_profile.upper()})")
        print(f"  {lg.name:10} {lg.label:28} league {lg.league_id:>10} team {lg.team_id:>3} "
              f"owner {lg.owner:7} {mode}")
    s = cfg.settings
    print(f"Settings: auto_lineup={s.auto_lineup} waivers_need_approval={s.waivers_need_approval} "
          f"trades_propose_only={s.trades_propose_only} notify={cfg.notify.get('backend', 'console')}")
    return 0


def cmd_lineup(cfg: AppConfig, args) -> int:
    lg, reader, writer = _setup(cfg, args.league)
    state = reader.load()
    res = optimize(state)
    print(f"{lg.label}: {state.my_team.name}, week {state.scoring_period}")
    order = lambda p: (DISPLAY_ORDER.index(p.lineup_slot) if p.lineup_slot in DISPLAY_ORDER else 99, -p.proj_week)
    for p in sorted(state.roster, key=order):
        flags = " ".join(f for f in [
            "LOCKED" if p.locked else "", "BYE" if p.on_bye(state.scoring_period) else "",
            p.injury_status if p.injury_status not in ("ACTIVE", "", None) else ""] if f)
        print(f"  {p.slot:9} {p.name:24} {p.position:4} {p.proj_week:6.1f}  {flags}")
    for w in res.warnings:
        print(f"Note: {w}")
    if not res.moves:
        print(f"Lineup already optimal ({res.after_points} projected).")
        return 0
    print(f"\nMoves ({res.before_points} -> {res.after_points}, gain {res.gain:+.2f}):")
    for m in res.moves:
        print(f"  {m}")
    print()
    rc = _submit(writer, lineup_payload(lg.team_id, state.scoring_period, res.moves), args.send)
    n = _notifier(cfg, lg, args)
    if n and args.send and rc == 0:
        n.send(notify.msg_lineup_changes(lg.label, res))
    return rc


def cmd_waivers(cfg: AppConfig, args) -> int:
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load(free_agents=True)
    sugg = rank_free_agents(state, limit=args.limit)
    print(f"{lg.label}: waiver ideas, week {state.scoring_period} (approval required)")
    if not sugg:
        print("  No free agent beats your weakest player by enough to matter.")
        return 0
    for i, s in enumerate(sugg, 1):
        print(f"  {i}. add {s.add.name}, drop {s.drop.name if s.drop else '-'} [{s.kind}] {s.reason}")
    if args.pick is None:
        print("\nRe-run with --pick N to queue one for approval.")
        return 0
    s = sugg[args.pick - 1]
    drop_id = s.drop.player_id if s.drop else None
    if s.kind == "WAIVER":
        bid = args.bid if lg.faab else 0   # no-FAAB leagues always send bidAmount 0
        payload = waiver_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id, bid=bid)
    else:
        payload = free_agent_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id)
    summary = f"add {s.add.name}, drop {s.drop.name if s.drop else 'nobody'}, +{s.gain:.1f} ROS pts."
    print()
    return _queue(cfg, lg, s.kind, summary, payload, checks_for_add_drop(s.add.player_id, drop_id), args)


def cmd_trade(cfg: AppConfig, args) -> int:
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load()
    give, get = [], []
    for names, bucket in ((args.give, give), (args.get, get)):
        for n in names:
            p = state.find_player(n)
            if not p:
                raise SystemExit(f"Player not found (or ambiguous): {n}")
            bucket.append(p)
    try:
        ev = evaluate_trade(state, give, get)
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"{lg.label}: trade check, week {state.scoring_period}")
    for side in (ev.me, ev.partner):
        print(f"  {side.team_name}: sends {', '.join(p.name for p in side.sends)}; "
              f"gets {', '.join(p.name for p in side.receives)}; ROS value {side.ros_delta:+.1f}, "
              f"starting lineup {side.lineup_delta:+.1f} ({side.per_week:+.1f}/wk)")
    print(f"Verdict: {ev.verdict}. {ev.reason}")
    if not args.propose:
        return 0
    give_ids, get_ids = [p.player_id for p in give], [p.player_id for p in get]
    payload = trade_proposal_payload(lg.team_id, ev.partner.team_id, state.scoring_period, give_ids, get_ids)
    summary = (f"give {', '.join(p.name for p in give)} get {', '.join(p.name for p in get)} "
               f"({ev.partner.team_name}), {ev.me.per_week:+.1f} pts/wk.")
    print("\nTrades are proposed only after your approval, never accepted automatically.")
    return _queue(cfg, lg, "TRADE_PROPOSAL", summary, payload,
                  checks_for_trade(give_ids, get_ids, ev.partner.team_id), args)


def cmd_pending(cfg: AppConfig, args) -> int:
    acts = _store(args).open_actions()
    if not acts:
        print("No pending actions.")
    for a in acts:
        print(f"  #{a.id} [{a.kind}] {a.league}: {a.summary} (expires {a.expires[:16]} UTC)")
    return 0


def cmd_reject(cfg: AppConfig, args) -> int:
    store = _store(args)
    store.get(args.id)
    store.set_status(args.id, "rejected")
    print(f"#{args.id} rejected. Nothing was sent.")
    return 0


def approve(action_id: str, cfg: AppConfig, store: PendingStore,
            reader: EspnReader | None = None, writer: TransactionWriter | None = None) -> tuple[int, str]:
    """Re-read, re-check, then send one queued action. Returns (exit code, message)."""
    a = store.get(action_id)
    if a.status != "pending":
        return 1, f"#{a.id} is {a.status}; nothing sent."
    if a.expired():
        store.set_status(a.id, "expired")
        return 1, f"#{a.id} expired; nothing sent. Re-run the command to queue a fresh one."
    if reader is None or writer is None:
        _, reader, writer = _setup(cfg, a.league)
    state = reader.load()
    if state.scoring_period != a.payload.get("scoringPeriodId"):
        store.set_status(a.id, "aborted")
        return 1, f"#{a.id} aborted: the week changed since it was queued. Nothing sent."
    problems = verify(a.checks, state)
    if problems:
        store.set_status(a.id, "aborted")
        return 1, f"#{a.id} aborted, the league changed: " + "; ".join(problems) + ". Nothing sent."
    try:
        res = writer.submit(a.payload, send=True)
    except OwnTeamViolation as e:
        store.set_status(a.id, "aborted")
        return 2, f"REFUSED (own-team guard): {e}"
    except WriteRejected as e:
        store.set_status(a.id, "failed")
        return 3, f"ESPN rejected #{a.id}: {e.message}"
    store.set_status(a.id, "approved")
    return 0, f"#{a.id} sent. {res.message}"


def cmd_approve(cfg: AppConfig, args) -> int:
    rc, msg = approve(args.id, cfg, _store(args))
    print(msg)
    return rc


def cmd_report(cfg: AppConfig, args) -> int:
    leagues = [cfg.get(args.league)] if args.league else [
        l for l in cfg.leagues if l.fixture_dir or (l.league_id and l.team_id)]
    for lg in leagues:
        if not lg.fixture_dir and not load_credentials(lg.auth_profile) and not args.league:
            print(f"{lg.label}: skipped, no cookies for profile '{lg.auth_profile}'.\n")
            continue
        _, reader, _ = _setup(cfg, lg.name)
        state = reader.load(free_agents=True, transactions=True)
        facts = insights.build_facts(lg.label, state, optimize(state), rank_free_agents(state, limit=3))
        text, used = insights.llm_note(facts) if args.llm else (insights.template_note(facts), False)
        print(text)
        if args.llm and not used:
            print("(LLM unavailable or added numbers; showing template text.)")
        n = _notifier(cfg, lg, args)
        if n:
            n.send(notify.msg_weekly(facts))
            hurt = notify.msg_starter_injuries(lg.label, state)
            if hurt:
                n.send(hurt)
            for a in _store(args).open_actions():
                if a.league == lg.name:
                    n.send(notify.msg_pending(a))
        print()
    return 0


def _pending_signature(payload: dict) -> str:
    return f"{payload.get('type')}:" + ",".join(f"{i.get('type')}{i.get('playerId')}" for i in payload.get("items", []))


def cmd_autopilot(cfg: AppConfig, args) -> int:
    """Scheduler entry point. Lineups: automatic (sent only with --send AND auto_lineup: true).
    Waivers/add-drops: queued for approval only. Texts go to the league owner."""
    from .llm import LLM
    from .news.pipeline import enrich
    store = _store(args)
    llm = LLM()
    rc_all = 0
    leagues = [l for l in cfg.leagues if (l.fixture_dir is not None) == bool(args.demo)]
    for i, lg in enumerate(leagues, 1):
        if not lg.fixture_dir and (not lg.league_id or not lg.team_id or not load_credentials(lg.auth_profile)):
            summary(f"league {i}: skipped (not configured or no cookies)")
            continue
        try:
            _, reader, writer = _setup(cfg, lg.name)
            state = reader.load(free_agents=True, transactions=True)
            if not args.no_news:
                enrich(state, llm)
            n = notify.make_notifier(cfg.notify, lg.owner) if args.notify else None
            res = optimize(state)
            status = "no changes"
            if res.moves:
                send = bool(args.send and cfg.settings.auto_lineup)
                rc = _submit(writer, lineup_payload(lg.team_id, state.scoring_period, res.moves), send)
                rc_all = rc_all or rc
                status = ("sent" if send else "dry-run") if rc == 0 else f"failed (rc {rc})"
                if n and rc == 0:
                    n.send(notify.msg_lineup_changes(lg.label, res, sent=send))
            hurt = notify.msg_starter_injuries(lg.label, state)
            if n and hurt:
                n.send(hurt)
            queued = 0
            if args.waivers:
                existing = {_pending_signature(a.payload) for a in store.open_actions()}
                for s in rank_free_agents(state, limit=2):
                    drop_id = s.drop.player_id if s.drop else None
                    payload = (waiver_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id, bid=0)
                               if s.kind == "WAIVER" else
                               free_agent_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id))
                    if _pending_signature(payload) in existing:
                        continue
                    check_own_team(payload, lg)
                    why = f" ({s.add.reasons[0]})" if s.add.reasons else ""
                    a = store.add(lg.name, s.kind, f"add {s.add.name}, drop {s.drop.name if s.drop else 'nobody'}, "
                                  f"+{s.gain:.1f} ROS pts{why}.", payload, checks_for_add_drop(s.add.player_id, drop_id))
                    queued += 1
                    if n:
                        n.send(notify.msg_pending(a))
            summary(f"league {i}: {len(res.moves)} moves, {status}; {queued} queued for approval")
        except Exception as e:   # one league failing never stops the others
            rc_all = rc_all or 1
            summary(f"league {i}: error ({type(e).__name__})")
            print(f"{lg.label}: error: {e}")
    return rc_all


def cmd_poll_telegram(cfg: AppConfig, args) -> int:
    from .telegram_poll import poll
    for line in poll(cfg, _store(args)):
        summary(line)
    return 0


def cmd_state(cfg: AppConfig, args) -> int:
    from . import state
    if args.action == "keygen":
        print(state.keygen())   # printed once for the user to store as a secret; never used in CI logs
        return 0
    if args.action == "pack":
        summary(f"state packed ({state.pack()} bytes)")
    else:
        summary("state restored" if state.unpack() else "no previous state")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fai", description="AI fantasy football manager for ESPN.")
    ap.add_argument("--config", help="path to leagues.yaml")
    ap.add_argument("--store", help="pending-actions JSON (default data/pending.json)")
    ap.add_argument("--quiet", action="store_true", help="CI-safe output: counts only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("leagues", help="list configured leagues")
    p = sub.add_parser("lineup", help="optimize lineup (dry-run unless --send)")
    p.add_argument("league")
    p.add_argument("--send", action="store_true", help="really submit to ESPN (own team only)")
    p.add_argument("--notify", action="store_true")
    p = sub.add_parser("waivers", help="rank free agents vs your weakest players")
    p.add_argument("league")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--pick", type=int, help="queue suggestion N for approval")
    p.add_argument("--bid", type=int, default=0, help="FAAB bid (ignored in no-FAAB leagues)")
    p.add_argument("--notify", action="store_true")
    p = sub.add_parser("trade", help="evaluate a trade")
    p.add_argument("league")
    p.add_argument("--give", action="append", required=True)
    p.add_argument("--get", action="append", required=True)
    p.add_argument("--propose", action="store_true", help="queue a TRADE_PROPOSAL for approval")
    p.add_argument("--notify", action="store_true")
    sub.add_parser("pending", help="list queued actions")
    p = sub.add_parser("approve", help="re-check and send a queued action")
    p.add_argument("id")
    p = sub.add_parser("reject", help="drop a queued action")
    p.add_argument("id")
    p = sub.add_parser("report", help="weekly note per league")
    p.add_argument("league", nargs="?")
    p.add_argument("--llm", action="store_true", help="rewrite with `claude -p` (numbers checked)")
    p.add_argument("--notify", action="store_true")
    p = sub.add_parser("autopilot", help="scheduled run: lineups, news, waiver approvals, texts")
    p.add_argument("--send", action="store_true", help="really set lineups (needs auto_lineup: true)")
    p.add_argument("--waivers", action="store_true", help="also queue top waiver ideas for approval")
    p.add_argument("--no-news", action="store_true", help="skip the news/buzz layer")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--demo", action="store_true", help="run on fixture leagues only")
    sub.add_parser("poll-telegram", help="process 'approve N' / 'reject N' replies")
    p = sub.add_parser("state", help="encrypted CI state")
    p.add_argument("action", choices=["keygen", "pack", "unpack"])
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.quiet or os.environ.get("FAI_QUIET") == "1"
    SUMMARY.clear()
    handler = {"leagues": cmd_leagues, "lineup": cmd_lineup, "waivers": cmd_waivers,
               "trade": cmd_trade, "pending": cmd_pending, "approve": cmd_approve,
               "reject": cmd_reject, "report": cmd_report, "autopilot": cmd_autopilot,
               "poll-telegram": cmd_poll_telegram, "state": cmd_state}[args.cmd]
    if not quiet:
        cfg = load_config(args.config)
        try:
            rc = handler(cfg, args)
        except KeyError as e:
            print(e.args[0] if e.args else e)
            rc = 1
        for line in SUMMARY:
            print(line)
        return rc
    # Quiet: swallow ALL detail output and errors; print only the non-identifying summary.
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            rc = handler(load_config(args.config), args)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        rc = 1
        SUMMARY.append(f"{args.cmd}: error ({type(e).__name__})")
    for line in SUMMARY or [f"{args.cmd}: done"]:
        print(line)
    print(f"{args.cmd}: exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
