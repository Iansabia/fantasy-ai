"""`fai` command line.

  fai leagues
  fai lineup <league> [--send] [--notify]          (lineup + IR moves; news and factors first)
  fai explain <league> <player>                     (full factor breakdown for one player)
  fai waivers <league> [--pick N] [--bid B] [--notify]       (--pick queues an approval)
  fai trade <league> --give X --get Y [--propose] [--notify]  (--propose queues an approval)
  fai pending | fai approve <id> | fai reject <id>
  fai track-values <league> [--force]                 (weekly snapshot: our values vs FantasyCalc)
  fai score-values <league> [--horizon N]             (score saved snapshots against real points)
  fai report [league] [--llm] [--notify]
  fai autopilot [--send] [--waivers] [--jitter] [--notify]
  fai poll-telegram
  fai poll-imessage
  fai state keygen|pack|unpack

Common flags: --no-news (skip the news layer), --no-factors (skip matchup/Vegas/weather/usage).
Nothing is ever texted unless --notify is given; FAI_NOTIFY_BACKEND=console forces console.

--quiet (or FAI_QUIET=1): stdout shows only counts like "league 1: 2 moves, sent".
No names, players, payloads, ids or phone numbers. Use it in public CI logs.

Lineups: dry-run unless --send (own team only). IR moves go in a separate request after the
lineup request. Waivers, add/drops and trades: never sent directly. They go to the approval
queue and are sent only by `fai approve <id>`, after a fresh read confirms nothing changed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys

from . import engine, insights, notify
from .config import AppConfig, LeagueConfig, load_config, load_credentials
from .espn_read import EspnReader
from .espn_write import (OwnTeamViolation, TransactionWriter, WriteRejected, check_own_team,
                         describe_dry_run, free_agent_payload, lineup_payload,
                         trade_proposal_payload, transaction_url, waiver_payload)
from .lineup import DISPLAY_ORDER, LineupResult, optimize
from .pacing import Pacer, PacingStop
from . import prefs as prefs_mod
from .pending import PendingStore, checks_for_add_drop, checks_for_trade, verify
from .trades import evaluate_trade
from .waivers import rank_free_agents

SUMMARY: list[str] = []   # the only lines printed in --quiet mode (no identifying details)
_PACER: Pacer | None = None


def summary(line: str) -> None:
    SUMMARY.append(line)


def _pacer(cfg: AppConfig) -> Pacer:
    global _PACER
    if _PACER is None:
        _PACER = Pacer(cfg.pacing)
    return _PACER


def _setup(cfg: AppConfig, name: str) -> tuple[LeagueConfig, EspnReader, TransactionWriter]:
    lg = cfg.get(name)
    if not lg.fixture_dir and (not lg.league_id or not lg.team_id):
        raise SystemExit(f"League '{lg.name}' has no league_id/team_id yet. Edit {cfg.source}.")
    creds = None if lg.fixture_dir else load_credentials(lg.auth_profile)
    if not lg.fixture_dir and creds is None:
        # private leagues need cookies; don't make pointless unauthenticated calls to ESPN
        raise SystemExit(f"No cookies for '{lg.name}'. Add ESPN_S2_{lg.auth_profile.upper()} and "
                         f"SWID_{lg.auth_profile.upper()} to .env (see README).")
    pacer = None if lg.fixture_dir else _pacer(cfg)
    reader = EspnReader(lg, creds, pacer=pacer)
    writer = TransactionWriter(lg, creds, readback=reader.roster_slots, pacer=pacer)
    return lg, reader, writer


def _store(args) -> PendingStore:
    return PendingStore(args.store) if getattr(args, "store", None) else PendingStore()


def _notifier(cfg: AppConfig, lg: LeagueConfig, args) -> notify.Notifier | None:
    return notify.make_notifier(cfg.notify, lg.owner) if getattr(args, "notify", False) else None


def _prepare(cfg: AppConfig, state, reader, args, research=None) -> dict:
    return engine.prepare(state, reader, cfg.factors, news=not getattr(args, "no_news", False),
                          factors=not getattr(args, "no_factors", False), research=research,
                          hype_cfg=getattr(cfg, "hype", None))


def _submit(writer: TransactionWriter, payload: dict, send: bool, label: str = "") -> int:
    try:
        res = writer.submit(payload, send=send)
    except OwnTeamViolation as e:
        print(f"REFUSED (own-team guard): {e}")
        return 2
    except PacingStop as e:
        print(f"STOPPED (pacing): {e}")
        return 4
    except WriteRejected as e:
        print(f"ESPN rejected the {label or 'write'}: {e.message}")
        return 3
    if label:
        print(f"[{label}]")
    print(res.message)
    return 0


def _queue(cfg: AppConfig, lg: LeagueConfig, kind: str, summary_text: str, payload: dict, checks: dict,
           args, quiet_print: bool = False) -> int:
    try:
        check_own_team(payload, lg)
    except OwnTeamViolation as e:
        print(f"REFUSED (own-team guard): {e}")
        return 2
    store = _store(args)
    sig = _pending_signature(payload)
    if any(_pending_signature(a.payload) == sig for a in store.open_actions()):
        print("Same action is already waiting for approval.")
        return 0
    if not quiet_print:
        print(describe_dry_run(transaction_url(lg.year, lg.league_id), payload))
    a = store.add(lg.name, kind, summary_text, payload, checks)
    print(f"\nQueued as #{a.id} (expires {a.expires[:16]} UTC). Send it with: fai approve {a.id}")
    n = _notifier(cfg, lg, args)
    if n:
        n.send(notify.msg_pending(a))
    return 0


def _prefs(lg: LeagueConfig, state):
    return prefs_mod.load_prefs(state.scoring_period, lg.label, lg.name)


def _snapshot(lg: LeagueConfig, state, res) -> None:
    try:
        prefs_mod.write_snapshot(lg.label, state, res)
    except OSError as e:
        print(f"Note: could not write snapshot ({e})")


def _queue_trade_ideas(cfg: AppConfig, lg: LeagueConfig, state, prefs, args) -> int:
    """Handle data/requests.json trade_idea items for this league: queue proposals for approval."""
    queued = 0
    n = _notifier(cfg, lg, args)
    for req in prefs_mod.take_requests("trade_idea", lg.label, lg.name):
        player = state.find_player(req.get("player") or "")
        if not player:
            if n:
                n.send(f"🔎 {lg.label} | Trade ideas\n\n• Couldn't find {req.get('player')} in the league")
            continue
        ideas = prefs_mod.trade_ideas(state, player, prefs)
        if not ideas and n:
            n.send(f"🔎 {lg.label} | Trade ideas\n\n• No fair trade found for {player.name} that helps your lineup")
        for give, get, ev in ideas:
            g_ids, r_ids = [p.player_id for p in give], [p.player_id for p in get]
            payload = trade_proposal_payload(lg.team_id, ev.partner.team_id, state.scoring_period, g_ids, r_ids)
            text = (f"give {', '.join(p.name for p in give)} get {', '.join(p.name for p in get)} "
                    f"({ev.partner.team_name}), {ev.me.per_week:+.1f} pts/wk.")
            if _queue(cfg, lg, "TRADE_PROPOSAL", text, payload, checks_for_trade(g_ids, r_ids, ev.partner.team_id),
                      args, quiet_print=True) == 0:
                queued += 1
    return queued


def _pending_signature(payload: dict) -> str:
    return f"{payload.get('type')}:" + ",".join(f"{i.get('type')}{i.get('playerId')}" for i in payload.get("items", []))


def _apply_lineup(cfg: AppConfig, lg: LeagueConfig, writer: TransactionWriter, state, res: LineupResult,
                  send: bool, args) -> tuple[int, str]:
    """Send (or dry-run) the lineup request, then the IR request, then queue an IR drop if needed.
    Returns (rc, status word)."""
    rc, status = 0, "no changes"
    if res.moves:
        rc = _submit(writer, lineup_payload(lg.team_id, state.scoring_period, res.moves), send, "lineup")
        status = ("sent" if send else "dry-run") if rc == 0 else f"failed (rc {rc})"
    if res.ir_moves and rc == 0:
        print("\nIR moves go in a separate request after the lineup one. Note: an IR slot move has not "
              "been captured on the wire yet (jwulff 05 section 8 item 9); if ESPN rejects it, the lineup is unaffected.")
        rc_ir = _submit(writer, lineup_payload(lg.team_id, state.scoring_period, res.ir_moves), send, "IR")
        rc = rc or rc_ir
        if status == "no changes":
            status = ("sent" if send else "dry-run") if rc_ir == 0 else f"failed (rc {rc_ir})"
    if res.ir_drop and res.ir_activate:
        payload = free_agent_payload(lg.team_id, state.scoring_period, drop_id=res.ir_drop.player_id)
        _queue(cfg, lg, "FREEAGENT",
               f"drop {res.ir_drop.name} so {res.ir_activate.name} can come off IR (roster is full).",
               payload, checks_for_add_drop(None, res.ir_drop.player_id), args)
    return rc, status


# ---------------------------------------------------------------- commands

def cmd_leagues(cfg: AppConfig, args) -> int:
    print(f"Config: {cfg.source or 'LEAGUES_YAML'}")
    for lg in cfg.leagues:
        mode = "offline fixture" if lg.fixture_dir else (
            "cookies OK" if load_credentials(lg.auth_profile) else f"no cookies (ESPN_S2_{lg.auth_profile.upper()})")
        print(f"  {lg.name:10} {lg.label:28} league {lg.league_id:>10} team {lg.team_id:>3} "
              f"owner {lg.owner:7} {mode}")
    s = cfg.settings
    print(f"Settings: auto_lineup={s.auto_lineup} waivers_need_approval={s.waivers_need_approval} "
          f"trades_propose_only={s.trades_propose_only} notify={cfg.notify.get('backend', 'console')}")
    return 0


def _print_roster(state) -> None:
    wk = state.scoring_period
    order = lambda p: (DISPLAY_ORDER.index(p.lineup_slot) if p.lineup_slot in DISPLAY_ORDER else 99, -p.proj_week)
    print(f"  {'slot':9} {'player':24} {'pos':4} {'ESPN':>5} {'worth':>6}  flags")
    for p in sorted(state.roster, key=order):
        esp = p.base_proj_week if p.base_proj_week is not None else p.proj_week
        flags = " ".join(f for f in [
            "LOCKED" if p.locked else "", "BYE" if p.on_bye(wk) else "",
            p.game_status if not p.is_healthy() else "", "NEWS" if p.reasons else ""] if f)
        print(f"  {p.slot:9} {p.name:24} {p.position:4} {esp:5.1f} {p.lineup_value(wk):6.1f}  {flags}")


def cmd_lineup(cfg: AppConfig, args) -> int:
    lg, reader, writer = _setup(cfg, args.league)
    state = reader.load()
    info = _prepare(cfg, state, reader, args)
    res = optimize(state, prefs=_prefs(lg, state))
    _snapshot(lg, state, res)
    print(f"{lg.label}: {state.my_team.name}, week {state.scoring_period}")
    print(f"Inputs: ESPN projections"
          + (f", news for {info['news_players']} players" if info["news"] else ", no news (--no-news or offline)")
          + (", " + ", ".join(info["factor_sources"]) if info["factor_sources"] else ""))
    _print_roster(state)
    for w in res.warnings:
        print(f"Note: {w}")
    if not res.moves and not res.ir_moves and not res.ir_drop:
        print(f"Lineup already optimal ({res.after_points} expected points).")
        n = _notifier(cfg, lg, args)
        if n and res.pref_skipped:
            n.send(notify.msg_pref_skipped(lg.label, res.pref_skipped))
        return 0
    if res.moves:
        print(f"\nMoves (expected {res.before_points} -> {res.after_points}, gain {res.gain:+.2f}):")
        for m in res.moves:
            print(f"  {m}\n      {m.reason}")
    if res.ir_moves:
        print("\nIR moves:")
        for m in res.ir_moves:
            print(f"  {m}\n      {m.reason}")
    print()
    rc, _ = _apply_lineup(cfg, lg, writer, state, res, args.send, args)
    n = _notifier(cfg, lg, args)
    if n and args.send and rc == 0 and (res.moves or res.ir_moves):
        n.send(notify.msg_lineup_changes(lg.label, res))
    if n and res.pref_skipped:
        n.send(notify.msg_pref_skipped(lg.label, res.pref_skipped))
    return rc


def cmd_explain(cfg: AppConfig, args) -> int:
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load(free_agents=True)
    target = state.find_player(args.player)
    if not target:
        raise SystemExit(f"Player not found (or ambiguous): {args.player}")
    info = _prepare(cfg, state, reader, args, research=[target])
    p, wk = target, state.scoring_period
    esp = p.base_proj_week if p.base_proj_week is not None else p.proj_week
    print(f"{p.name} ({p.position}, {p.pro_team or '?'}) in {lg.label}, week {wk}")
    print(f"  ESPN projection        {esp:6.2f}")
    if p.base_proj_week is not None and p.proj_week != p.base_proj_week:
        print(f"  news adjustment        x{p.proj_week / p.base_proj_week:.3f} -> {p.proj_week:.2f}")
    for r in p.reasons:
        print(f"      {r}")
    disc = p.status_discount()
    if disc != 1:
        print(f"  status discount        x{disc:.2f}  ({p.game_status} on ESPN, no news found: default)")
    for f in p.factors:
        print(f"  {f.name:22} x{f.multiplier:.3f}  {f.reason}")
    print(f"  factor product         x{p.score_multiplier:.3f} (capped {cfg.factors.get('total_cap', [0.70, 1.35])})")
    if not p.available(wk):
        print("  cannot play this week (bye / OUT / reported out)")
    print(f"  = worth this week      {p.lineup_value(wk):6.2f}")
    print(f"  rest-of-season value   {p.ros_value(wk):6.1f} (x{p.ros_multiplier:.3f} from role/buzz/usage)")
    if getattr(p, "hype", None) is not None:
        print(f"  hype vs substance      {p.hype.text}, ROS x{p.hype.ros_mult:.2f}, future weeks x{p.hype.future_mult:.2f}")
        for r in p.hype.reasons:
            print(f"      - {r}")
    if p.status in ("FREEAGENT", "WAIVERS"):
        from .waivers import evaluate_add
        w = evaluate_add(state, p, _prefs(lg, state), cfg.waivers)
        if w is None:
            print("  waiver view: no legal drop (roster minimums / bye coverage)")
        else:
            verdict = "worth it" if w.gain >= w.threshold else f"below the +{w.threshold:.0f} bar"
            print(f"  waiver view: {w.reason} ({verdict})")
            for line in w.why:
                print(f"      - {line}")
    print(f"Inputs used: ESPN" + (", news" if info["news"] else "") +
          ("" if not info["factor_sources"] else ", " + ", ".join(info["factor_sources"])))
    return 0


def cmd_waivers(cfg: AppConfig, args) -> int:
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load(free_agents=True)
    from .news.pipeline import players_to_research
    _prepare(cfg, state, reader, args, research=players_to_research(state))
    from .waivers import audit, rank_with_checks
    pr = _prefs(lg, state)
    sugg, failures = rank_with_checks(state, limit=args.limit, prefs=pr, cfg=cfg.waivers)
    if args.audit:
        print(f"{lg.label}: roster audit, week {state.scoring_period}")
        print(f"  {'player':24} {'pos':4} {'slot':9} {'drop?':6} {'ROS':>6} {'3wk':>5} {'own%':>5}  {'hype label':24} reason / trend")
        for r in audit(state, pr, cfg.waivers):
            print(f"  {r['name']:24} {r['pos']:4} {r['slot']:9} {'yes' if r['droppable'] else 'no':6} "
                  f"{r['ros']:6.1f} {r['next3']:5.1f} {r['owned']:5.0f}  {r['hype'][:24]:24} {r['reason'] or '-'}"
                  + (f" | {r['trend']}" if r['trend'] else ""))
        print()
    for f in failures:
        print(f"WARNING: {f}")
    print(f"{lg.label}: waiver ideas, week {state.scoring_period} (approval required)")
    if not sugg:
        print("  No free agent improves your team by enough to matter (claims need more, they cost priority).")
    for i, s in enumerate(sugg, 1):
        print(f"  {i}. [{s.kind}] {s.reason}")
        for w in s.why:
            print(f"       - {w}")
    from .waivers import streamers
    streams = streamers(state, prefs=_prefs(lg, state), cfg=cfg.waivers)
    if streams:
        print("\nStreamers for this week (K / D/ST):")
        for s in streams:
            print(f"  - {s.reason}")
            for w in s.why:
                print(f"       - {w}")
    if args.pick is None:
        if sugg:
            print("\nRe-run with --pick N to queue one for approval.")
        return 0
    if not 1 <= args.pick <= len(sugg):
        raise SystemExit(f"--pick must be between 1 and {len(sugg)}")
    s = sugg[args.pick - 1]
    drop_id = s.drop.player_id if s.drop else None
    if s.kind == "WAIVER":
        bid = args.bid if lg.faab else 0   # no-FAAB leagues always send bidAmount 0
        payload = waiver_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id, bid=bid)
    else:
        payload = free_agent_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id)
    summary_text = f"add {s.add.name}, drop {s.drop.name if s.drop else 'nobody'}, +{s.gain:.1f} pts over 3 wks. " + "; ".join(s.why[:2])
    print()
    return _queue(cfg, lg, s.kind, summary_text, payload, checks_for_add_drop(s.add.player_id, drop_id), args)


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
    _prepare(cfg, state, reader, args, research=give + get)
    try:
        ev = evaluate_trade(state, give, get, prefs=_prefs(lg, state))
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"{lg.label}: trade check, week {state.scoring_period}")
    from .market import fc_value
    fc = lambda ps: (f"{sum(fc_value(p) for p in ps):.0f}" if ps and all(fc_value(p) for p in ps) else "n/a")
    for side in (ev.me, ev.partner):
        print(f"  {side.team_name}: sends {', '.join(p.name for p in side.sends)}; "
              f"gets {', '.join(p.name for p in side.receives)}; ROS value {side.ros_delta:+.1f}, "
              f"starting lineup {side.lineup_delta:+.1f} ({side.per_week:+.1f}/wk); "
              f"market (FantasyCalc) sends {fc(side.sends)} / gets {fc(side.receives)}")
    from .hype import line as hype_line
    for p in give + get:
        if hype_line(p):
            print(f"    {hype_line(p)}")
        for r in p.reasons[:2]:
            print(f"    {r}")
    print(f"Verdict: {ev.verdict}. {ev.reason}")
    if not args.propose:
        return 0
    give_ids, get_ids = [p.player_id for p in give], [p.player_id for p in get]
    payload = trade_proposal_payload(lg.team_id, ev.partner.team_id, state.scoring_period, give_ids, get_ids)
    summary_text = (f"give {', '.join(p.name for p in give)} get {', '.join(p.name for p in get)} "
                    f"({ev.partner.team_name}), {ev.me.per_week:+.1f} pts/wk.")
    print("\nTrades are proposed only after your approval, never accepted automatically.")
    return _queue(cfg, lg, "TRADE_PROPOSAL", summary_text, payload,
                  checks_for_trade(give_ids, get_ids, ev.partner.team_id), args)


def cmd_hype(cfg: AppConfig, args) -> int:
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load(free_agents=True)
    from .news.pipeline import players_to_research
    info = _prepare(cfg, state, reader, args, research=players_to_research(state))
    if not info.get("hype"):
        print("Note: nflverse usage data unavailable (offline or nflreadpy missing); labels use hype only.")
    def show(title, players):
        print(title)
        print(f"  {'player':24} {'pos':4} {'label':30} {'sub':>3} {'hype':>4}  why")
        for p in players:
            h = p.hype
            print(f"  {p.name:24} {p.position:4} {h.label:30} {h.substance:3d} {h.hype:4d}  {'; '.join(h.reasons[:3])}")
    show(f"{lg.label}: hype vs substance, week {state.scoring_period}\nOur roster:", sorted(state.roster, key=lambda p: -p.hype.substance))
    fas = sorted(state.free_agents, key=lambda p: -p.percent_owned)[: args.limit]
    show("\nTop free agents (by roster %):", fas)
    return 0


def cmd_scout(cfg: AppConfig, args) -> int:
    from . import teamneeds
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load()
    _prepare(cfg, state, reader, args, research=[])
    prof = teamneeds.profiles(state)
    n = len(state.teams)
    me = prof[state.my_team_id]
    print(f"{lg.label}: scouting, week {state.scoring_period}")
    print("Our team:")
    for line in teamneeds.strength_summary(me, n):
        print(f"  {line}")
    if me.surplus:
        print("  our surplus: " + ", ".join(f"{p.name} ({p.position})" for p in me.surplus))
    for line in teamneeds.surplus_matches(state, prof):
        print(f"  {line}")
    print("\nOther teams:")
    for tid, pf in sorted(prof.items(), key=lambda x: x[1].name):
        if tid != state.my_team_id:
            print(f"  {teamneeds.describe(pf, n)}")
    return 0


def cmd_trade_ideas(cfg: AppConfig, args) -> int:
    from . import teamneeds
    from .hype import line as hype_line
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load()
    _prepare(cfg, state, reader, args, research=[])
    pr = _prefs(lg, state)
    prof = teamneeds.profiles(state)
    n = len(state.teams)
    print(f"{lg.label}: trade ideas, week {state.scoring_period} (nothing is proposed without your approval)")
    for line in teamneeds.strength_summary(prof[state.my_team_id], n):
        print(f"  {line}")
    ideas = teamneeds.trade_ideas(state, pr, top=args.limit)
    if not ideas:
        print("  No trade clearly helps us while staying fair and good for the other team.")
        return 0
    fmt = lambda v: f"{v:.0f}" if v else "n/a"
    for i, idea in enumerate(ideas, 1):
        print(f"\n  {i}. with {idea.partner}: give {', '.join(p.name for p in idea.give)} | "
              f"get {', '.join(p.name for p in idea.get)}")
        print(f"     our lineup {idea.our_gain:+.1f} pts ROS, theirs {idea.their_gain:+.1f}; market (FantasyCalc) "
              f"{fmt(idea.fc_give)} for {fmt(idea.fc_get)}, fairness {idea.fairness:.0%}, acceptance {idea.accept:.0%}")
        print(f"     why this partner: {idea.why_partner}")
        for p in idea.give + idea.get:
            if hype_line(p) and p.hype.label != "Steady":
                print(f"     {hype_line(p)}")
        print(f"     pitch: \"{idea.pitch}\"")
    if args.propose:
        if not 1 <= args.propose <= len(ideas):
            raise SystemExit(f"--propose must be between 1 and {len(ideas)}")
        idea = ideas[args.propose - 1]
        g, r = [p.player_id for p in idea.give], [p.player_id for p in idea.get]
        payload = trade_proposal_payload(lg.team_id, idea.partner_id, state.scoring_period, g, r)
        text = (f"give {', '.join(p.name for p in idea.give)} get {', '.join(p.name for p in idea.get)} "
                f"({idea.partner}), lineup {idea.our_gain:+.1f} ROS. Pitch: {idea.pitch}")
        print()
        return _queue(cfg, lg, "TRADE_PROPOSAL", text, payload, checks_for_trade(g, r, idea.partner_id), args)
    print("\nRe-run with --propose N to queue one for your approval (fai approve sends it).")
    return 0


def cmd_calc_compare(cfg: AppConfig, args) -> int:
    from . import market
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load(free_agents=True)
    info = _prepare(cfg, state, reader, args, research=[])
    if not info.get("market"):
        print("FantasyCalc values unavailable (offline or API change).")
        return 1
    c = market.compare(state, top=args.limit)
    print(f"{lg.label}: our rest-of-season value vs FantasyCalc market value ({c.n} players matched)")
    print(f"  Spearman rank correlation: overall {c.overall}; " +
          ", ".join(f"{k} {v}" for k, v in c.by_position.items()))
    if c.overall is not None and c.overall < market.LOW_CORRELATION:
        print(f"  WARNING: correlation below {market.LOW_CORRELATION}; investigate our values before trusting trade math.")
    for title, rows in (("We value higher than the market", c.ours_higher), ("Market values higher than us", c.fc_higher)):
        print(f"\n  {title}:")
        for p, ro, rf in rows:
            print(f"    {p.name:24} {p.position:3} our rank {ro:3d}, FC rank {rf:3d}  ({market.reasons_for(p, state)})")
    return 0


def cmd_track_values(cfg: AppConfig, args) -> int:
    from . import value_track
    lg, reader, _ = _setup(cfg, args.league)
    state = reader.load(free_agents=True)
    info = _prepare(cfg, state, reader, args, research=[])
    if not info.get("market"):
        print("Note: FantasyCalc values unavailable; snapshot saved without market values.")
    snap = value_track.snapshot(state, lg.name)
    path, written = value_track.save(snap, force=args.force)
    n_fc = sum(1 for r in snap["players"] if r["fc"] is not None)
    if written:
        print(f"{lg.label}: saved week {snap['week']} snapshot ({len(snap['players'])} players, {n_fc} with FC value) to {path}")
    else:
        print(f"{lg.label}: week {snap['week']} snapshot already exists ({path}); use --force to replace it")
    summary(f"track-values: week {snap['week']} {'saved' if written else 'kept'}")
    return 0


def cmd_score_values(cfg: AppConfig, args) -> int:
    from . import usage_data, value_track
    lg = cfg.get(args.league)
    snaps = value_track.load_all(lg.name, lg.year)
    if not snaps:
        print(f"No snapshots for {lg.label} yet. Run `fai track-values {lg.name}` before each week's games.")
        return 1
    usage = usage_data.load(lg.year)
    if usage is None:
        print("nflverse data unavailable (offline or nflreadpy missing); cannot score.")
        return 1
    actuals = value_track.actuals_from_usage(usage)
    scores = [s for s in (value_track.score_snapshot(sn, actuals, horizon=args.horizon) for sn in snaps) if s]
    if not scores:
        print(f"Snapshots exist (weeks {', '.join(str(s['week']) for s in snaps)}) but no games have been played since.")
        return 0
    print(f"{lg.label}: our values vs FantasyCalc, scored on real PPR points (nflverse)")
    for line in value_track.format_scores(scores):
        print(line)
    return 0


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
    except PacingStop as e:
        return 4, f"#{a.id} not sent yet: {e}. It stays in the queue."
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
        _prepare(cfg, state, reader, args)
        pr = _prefs(lg, state)
        res = optimize(state, prefs=pr)
        _snapshot(lg, state, res)
        facts = insights.build_facts(lg.label, state, res, rank_free_agents(state, limit=3, prefs=pr, cfg=cfg.waivers))
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


def cmd_autopilot(cfg: AppConfig, args) -> int:
    """Scheduler entry point. Modes (schedule.py): full (Thursday: news, factors, IR, waiver ideas),
    recheck (Saturday: lineups + IR only), inactives (Sunday, only if enabled), catchup (runs a
    full run only if this week's Thursday run was missed and kickoff has not passed).
    Lineups and IR: automatic (sent only with --send AND auto_lineup: true). Waivers/add-drops:
    queued for approval only. With --notify every league gets a heartbeat text, even with no changes."""
    import datetime as _dt
    from . import schedule
    store = _store(args)
    pacer = _pacer(cfg)
    base_mode = args.mode
    if base_mode == "inactives" and not cfg.schedule.get("sunday_inactive_check", False):
        summary("inactive check is disabled (schedule.sunday_inactive_check: false)")
        return 0
    now = _dt.datetime.now(_dt.timezone.utc)
    leagues = [l for l in cfg.leagues if (l.fixture_dir is not None) == bool(args.demo)]
    if base_mode == "catchup" and not any(schedule.missed_full(l.label, now) for l in leagues):
        summary("catchup: nothing missed")
        return 0
    if args.jitter:
        pacer.start_jitter()
    rc_all = 0
    for i, lg in enumerate(leagues, 1):
        if not lg.fixture_dir and (not lg.league_id or not lg.team_id or not load_credentials(lg.auth_profile)):
            summary(f"league {i}: skipped (not configured or no cookies)")
            continue
        mode = base_mode
        if mode != "full" and schedule.missed_full(lg.label, now):
            mode = "full"                       # Thursday run was missed: catch up before kickoff
            summary(f"league {i}: missed Thursday run, running full catch-up")
        elif mode == "catchup":
            continue
        full = mode == "full"
        n = notify.make_notifier(cfg.notify, lg.owner) if args.notify else None
        if not lg.fixture_dir:
            if pacer.in_cooldown(lg.name):
                summary(f"league {i}: paused (cooldown)")
                continue
            pacer.between_leagues()
        try:
            _, reader, writer = _setup(cfg, lg.name)
            state = reader.load(free_agents=full, transactions=full)
            _prepare(cfg, state, reader, args)
            pr = _prefs(lg, state)
            res = optimize(state, prefs=pr)
            _snapshot(lg, state, res)
            if full and not lg.fixture_dir:
                try:                            # forward test of our values vs FantasyCalc (never fatal)
                    from . import value_track
                    value_track.save(value_track.snapshot(state, lg.name))
                except Exception as e:
                    print(f"{lg.label}: value snapshot skipped ({type(e).__name__})")
            send = bool(args.send and cfg.settings.auto_lineup)
            rc, status = _apply_lineup(cfg, lg, writer, state, res, send, args)
            rc_all = rc_all or rc
            if rc == 4 and n:
                n.send(notify.msg_paused(lg.label, "ESPN pushed back or a write cap was hit"))
            if n and rc == 0 and (res.moves or res.ir_moves):
                n.send(notify.msg_lineup_changes(lg.label, res, sent=send))
            if n and res.pref_skipped:
                n.send(notify.msg_pref_skipped(lg.label, res.pref_skipped))
            hurt = notify.msg_starter_injuries(lg.label, state)
            if n and hurt:
                n.send(hurt)
            queued = _queue_trade_ideas(cfg, lg, state, pr, args) if full else 0
            if full or args.waivers:
                from .waivers import rank_with_checks
                existing = {_pending_signature(a.payload) for a in store.open_actions()}
                ideas, failures = rank_with_checks(state, limit=2, prefs=pr, cfg=cfg.waivers)
                for f in failures:
                    print(f"{lg.label}: {f}")
                if failures:
                    summary(f"league {i}: waiver check failed ({len(failures)})")
                    if n:
                        n.send(f"⚠️ {lg.label} | Waiver check failed\n\n" + "\n".join(f"• {f}" for f in failures[:5]))
                for s in ideas:
                    drop_id = s.drop.player_id if s.drop else None
                    payload = (waiver_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id, bid=0)
                               if s.kind == "WAIVER" else
                               free_agent_payload(lg.team_id, state.scoring_period, s.add.player_id, drop_id))
                    if _pending_signature(payload) in existing:
                        continue
                    check_own_team(payload, lg)
                    why = f" ({s.add.reasons[0]})" if s.add.reasons else ""
                    a = store.add(lg.name, s.kind, f"add {s.add.name}, drop {s.drop.name if s.drop else 'nobody'}, "
                                  f"+{s.gain:.1f} pts over 3 wks{why}.", payload, checks_for_add_drop(s.add.player_id, drop_id))
                    queued += 1
                    if n:
                        n.send(notify.msg_pending(a))
            moves = len(res.moves) + len(res.ir_moves)
            summary(f"league {i}: {mode}: {moves} moves, {status}; {queued} queued for approval")
            if rc == 0:
                schedule.record(lg.label, mode, {"moves": moves, "queued": queued, "sent": send})
            if n:
                beat = "waivers" if (mode == "recheck" and args.waivers) else mode
                n.send(schedule.heartbeat(lg.label, beat, moves, queued, send and rc == 0,
                                          None if rc in (0,) else f"exit code {rc}"))
        except PacingStop as e:
            rc_all = rc_all or 4
            summary(f"league {i}: paused (ESPN pushed back)")
            print(f"{lg.label}: {e}")
            if n:
                n.send(notify.msg_paused(lg.label, str(e)))
        except Exception as e:   # one league failing never stops the others
            rc_all = rc_all or 1
            summary(f"league {i}: error ({type(e).__name__})")
            print(f"{lg.label}: error: {e}")
            if n:
                n.send(schedule.heartbeat(lg.label, mode, 0, 0, False, type(e).__name__))
    return rc_all


def cmd_status(cfg: AppConfig, args) -> int:
    import datetime as _dt
    from . import schedule
    now = _dt.datetime.now(_dt.timezone.utc)
    labels = [l.label for l in cfg.leagues if not l.fixture_dir]
    print(f"Now: {now.astimezone(schedule.ET).strftime('%a %b %d %I:%M%p ET')}")
    for line in schedule.status_lines(labels, now, cfg.schedule.get("sunday_inactive_check", False)):
        print(f"  {line}")
    pacer = _pacer(cfg)
    for l in cfg.leagues:
        cd = pacer.in_cooldown(l.name)
        if cd:
            print(f"  {l.label}: ESPN cooldown until {cd}")
    return 0


def cmd_poll_telegram(cfg: AppConfig, args) -> int:
    from .telegram_poll import poll
    for line in poll(cfg, _store(args)):
        summary(line)
    return 0


def cmd_poll_imessage(cfg: AppConfig, args) -> int:
    from .imessage_poll import poll
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

    def evidence(p):
        p.add_argument("--no-news", action="store_true", help="skip the news/buzz layer")
        p.add_argument("--no-factors", action="store_true", help="skip matchup/Vegas/weather/usage factors")

    sub.add_parser("leagues", help="list configured leagues")
    p = sub.add_parser("lineup", help="optimize lineup + IR (dry-run unless --send)")
    p.add_argument("league")
    p.add_argument("--send", action="store_true", help="really submit to ESPN (own team only)")
    p.add_argument("--notify", action="store_true")
    evidence(p)
    p = sub.add_parser("explain", help="factor breakdown for one player")
    p.add_argument("league")
    p.add_argument("player")
    evidence(p)
    p = sub.add_parser("waivers", help="rank free agents vs your weakest players")
    p.add_argument("league")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--pick", type=int, help="queue suggestion N for approval")
    p.add_argument("--bid", type=int, default=0, help="FAAB bid (ignored in no-FAAB leagues)")
    p.add_argument("--audit", action="store_true", help="show every player: droppable or why not")
    p.add_argument("--notify", action="store_true")
    evidence(p)
    p = sub.add_parser("trade", help="evaluate a trade")
    p.add_argument("league")
    p.add_argument("--give", action="append", required=True)
    p.add_argument("--get", action="append", required=True)
    p.add_argument("--propose", action="store_true", help="queue a TRADE_PROPOSAL for approval")
    p.add_argument("--notify", action="store_true")
    evidence(p)
    p = sub.add_parser("hype", help="hype vs substance labels for our roster and top free agents")
    p.add_argument("league")
    p.add_argument("--limit", type=int, default=15)
    evidence(p)
    p = sub.add_parser("scout", help="profile every team: strengths, holes, surplus, short-term needs")
    p.add_argument("league")
    evidence(p)
    p = sub.add_parser("trade-ideas", help="team-need trade ideas (queued only with --propose N)")
    p.add_argument("league")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--propose", type=int, help="queue idea N for approval")
    p.add_argument("--notify", action="store_true")
    evidence(p)
    p = sub.add_parser("calc-compare", help="compare our values with FantasyCalc market values")
    p.add_argument("league")
    p.add_argument("--limit", type=int, default=10)
    evidence(p)
    p = sub.add_parser("track-values", help="save this week's snapshot of our values vs FantasyCalc")
    p.add_argument("league")
    p.add_argument("--force", action="store_true", help="replace this week's snapshot if it exists")
    evidence(p)
    p = sub.add_parser("score-values", help="score saved value snapshots against real PPR points")
    p.add_argument("league")
    p.add_argument("--horizon", type=int, help="score only the first N weeks after each snapshot")
    sub.add_parser("pending", help="list queued actions")
    p = sub.add_parser("approve", help="re-check and send a queued action")
    p.add_argument("id")
    p = sub.add_parser("reject", help="drop a queued action")
    p.add_argument("id")
    p = sub.add_parser("report", help="weekly note per league")
    p.add_argument("league", nargs="?")
    p.add_argument("--llm", action="store_true", help="rewrite with an LLM (numbers checked)")
    p.add_argument("--notify", action="store_true")
    evidence(p)
    p = sub.add_parser("autopilot", help="scheduled run: news, lineups + IR, waiver approvals, texts")
    p.add_argument("--send", action="store_true", help="really set lineups (needs auto_lineup: true)")
    p.add_argument("--waivers", action="store_true", help="also queue top waiver ideas for approval")
    p.add_argument("--jitter", action="store_true", help="random 0-10 min start delay (scheduled runs)")
    p.add_argument("--mode", choices=["full", "recheck", "inactives", "catchup"], default="full",
                   help="full (Thu), recheck (Sat), inactives (Sun, if enabled), catchup (only if Thu was missed)")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--demo", action="store_true", help="run on fixture leagues only")
    evidence(p)
    sub.add_parser("status", help="last successful run per league; warns about missed scheduled runs")
    sub.add_parser("poll-telegram", help="process 'approve N' / 'reject N' replies")
    sub.add_parser("poll-imessage", help="process 'approve N' / 'reject N' iMessage replies (macOS)")
    p = sub.add_parser("state", help="encrypted CI state")
    p.add_argument("action", choices=["keygen", "pack", "unpack"])
    return ap


def main(argv: list[str] | None = None) -> int:
    global _PACER
    args = build_parser().parse_args(argv)
    quiet = args.quiet or os.environ.get("FAI_QUIET") == "1"
    SUMMARY.clear()
    _PACER = None
    handler = {"leagues": cmd_leagues, "lineup": cmd_lineup, "explain": cmd_explain, "waivers": cmd_waivers,
               "trade": cmd_trade, "hype": cmd_hype, "scout": cmd_scout, "trade-ideas": cmd_trade_ideas,
               "calc-compare": cmd_calc_compare,
               "track-values": cmd_track_values, "score-values": cmd_score_values, "pending": cmd_pending, "approve": cmd_approve,
               "reject": cmd_reject, "report": cmd_report, "autopilot": cmd_autopilot,
               "poll-telegram": cmd_poll_telegram, "status": cmd_status, "poll-imessage": cmd_poll_imessage, "state": cmd_state}[args.cmd]
    if not quiet:
        cfg = load_config(args.config)
        try:
            rc = handler(cfg, args)
        except KeyError as e:
            print(e.args[0] if e.args else e)
            rc = 1
        except PacingStop as e:
            print(f"STOPPED (pacing): {e}")
            rc = 4
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
