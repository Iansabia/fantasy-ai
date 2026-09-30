# fantasy-ai

An AI helper for ESPN fantasy football leagues. It sets your lineup, suggests waiver pickups,
checks trades, reads the news, and texts you a short weekly note.

The rule that shapes everything: **plain code makes every decision. The LLM only labels news
and writes explanation text, and it is never allowed to invent a number.**

## What it does

| Command | What happens |
|---|---|
| `fai leagues` | List configured leagues and whether cookies are found |
| `fai lineup <league> [--send]` | Best legal lineup plus IR moves, after checking news and factors. Prints each move with its reason and the exact ESPN request. Sends only with `--send` |
| `fai explain <league> <player>` | Full factor breakdown for one player: ESPN projection, news, status, matchup, Vegas, weather, usage, floor/ceiling |
| `fai waivers <league> [--pick N]` | Free agents ranked by value over your weakest player at the position. `--pick N` puts the add/drop in the approval queue |
| `fai trade <league> --give X --get Y [--propose]` | Rest-of-season value and starting-lineup impact for both teams. `--propose` queues a trade proposal |
| `fai pending` / `fai approve <id>` / `fai reject <id>` | The approval queue. `approve` re-reads the league and aborts if anything changed |
| `fai report [league] [--llm]` | Short weekly note per league |
| `fai autopilot [--send] [--waivers]` | Scheduler entry point: news, lineups, waiver ideas, texts |
| `fai poll-telegram` | Handles "approve 4" / "reject 4" replies sent to your Telegram bot |

Try it offline right now (no ESPN account needed):

```bash
fai lineup demo
fai waivers demo
fai trade demo --give "Backup Bo" --get "Rival RB1"
fai report demo
```

### How decisions are made

- **Lineup:** exact search over ESPN's own slot rules (QB, RB, WR, TE, FLEX, OP/superflex,
  D/ST, K, bench, IR) using each player's `eligibleSlots`, on each player's *worth* (below), not raw
  projection. Anyone on bye, OUT, IR or suspended is benched. Locked players never move. Only
  changed slots are sent, in one atomic request.
- **Questionable / doubtful:** news decides. "Full practice, expected to play" means little or no
  discount; "did not practice, game-time decision" means a big one; "ruled out" means zero. With no
  news found, a modest default applies (questionable counts 85%, doubtful 10%) and the reason says so.
- **IR:** an INJURY_RESERVE player moves into an open IR slot automatically. A player who is healthy
  but still in the IR slot is activated if there is a roster spot; if not, a drop is suggested and
  waits in the approval queue. IR moves go in a separate request after the lineup, because an IR
  move has not been captured on the wire yet and one request is all-or-nothing.
- **Your rules win.** Text the bot "start Mitchell this week", "always start Bijan", "never drop X".
  They land in `data/preferences.json`: forced starts fill a legal slot first (skipped with a text if
  the player is out or on bye), never-drop players are never offered in waivers or trades, and the
  move reason says "you asked". "Find me a trade for X" is picked up by the next autopilot run and
  any fair options go to the approval queue. Each run writes `data/snapshot.json` so the bot can
  answer "why did you bench Pittman?".
- **Worth = ESPN projection x news x status x factors.** Factors, each capped and tunable in
  `factors:`: opponent points allowed to the position (ESPN), home/away, Vegas implied team total and
  spread (The Odds API, optional `ODDS_API_KEY`), game script, usage trend over 3 weeks (nflverse via
  optional `nflreadpy`), wind and rain for outdoor games (Open-Meteo), a second projection
  (Sleeper, when available), and floor vs. ceiling when you are a big favorite or underdog. Every
  factor adds a one-line reason, and `fai explain` shows them all.
- **Waivers:** each add is scored on how much it improves your best starting lineup over the next
  3 weeks (FLEX counts, byes and future matchups included) plus rest-of-season value counted by how
  often he would start. ROS includes news, role changes, Sleeper buzz, a second projection, and a
  handcuff bump when the RB ahead of him is out. The drop can be any bench player, but never a
  locked, IR, IR-bound or never-drop player, and never one that breaks roster minimums or leaves a
  slot empty in a bye week. Waiver claims cost rolling priority, so they need a bigger gain than a
  free-agent add. K and D/ST streamers are flagged by matchup and Vegas. Each idea shows its top
  reasons; tune it under `waivers:`.
- **Waiver safety checks.** `fai waivers <league> --audit` lists every rostered player as droppable
  or not, with the exact reason (starter, locked, IR slot, IR-bound, never_drop, roster minimum, bye
  coverage). Every run also checks that each unprotected bench player was actually evaluated as a
  drop, that the lowest-value one was a candidate, and that no suggestion drops someone worth more
  than the add both rest-of-season and over 3 weeks (unless the add fills a starting slot the drop
  never would). A failed check blocks the suggestion and prints or texts "waiver check failed".
- **Hype vs substance.** `fai hype <league>` labels our roster and top free agents: Real breakout, Role
  growing, Fill-in (teammate returns), Hype only, Buy low, Sell high, Steady, Fading, with a
  substance score and a hype score (0-100) and reasons with numbers ("targets 2 -> 9 per game,
  target share 8% -> 24%, WR/TE room was healthy, so the role change is real"). Usage comes from
  nflverse via `nflreadpy` (weekly stats, snap %, ffverse expected points, injury reports, last
  season), cached in `data/nflverse` for a week. A spike that lined up with a teammate's absence
  is discounted if he is back; points far above expected volume read as "sell high". The label
  moves rest-of-season value and future-week worth in waivers and trades (fill-ins lose credit once
  the starter returns, hype-only players get little). Route participation and red-zone splits are
  not used (not available free for the current season without full play-by-play).
- **Roster % as a signal.** ESPN's percent rostered, weekly change and percent started are tracked in
  `data/ownership_history.json` (6 weeks). A capped factor lifts risers and fades fallers in waiver
  and trade value, reasons show "rostered 42% (+22% this week, +37% over 3 wks)", free agents spiking
  are marked "likely claimed soon if you wait", and players the league is dropping are flagged.
  Percent started adds a small consensus factor (at most 3%) to the weekly score.
- **Trades:** raw value swap plus the change in each team's best starting lineup.
- **Team-need trade ideas** (`fai trade-ideas <league>`): value to a team is what its best lineups are
  worth (next 3 weeks + rest of season + a small credit for first backups), computed on each team's
  own roster. So a 3rd QB in a 1-QB league is worth little to you and a lot to a team whose QB is
  weak or on bye. It searches 1-for-1, 2-for-1 and 2-for-2 deals with every team, keeps ones where
  your lineup clearly gains, theirs does not clearly lose, and market value is close to fair, and
  ranks them by your gain times acceptance odds. Each idea shows both lineup gains, FantasyCalc
  values, hype labels, why this partner, and a pitch line. `--propose N` queues one for approval.
- **Scouting** (`fai scout <league>`): every team's rank at QB/RB/WR/TE, holes, surplus, short-term
  needs (starters on bye or out soon) and record, plus "best partner for our QB surplus".
- **Market check** (`fai calc-compare <league>`): Spearman rank correlation between our
  rest-of-season values and FantasyCalc redraft values (overall and by position), and the biggest
  disagreements each way with our reasons. Below 0.6 prints a warning. FantasyCalc is a second
  opinion and a "market" column in trade output; it never replaces our decision value.
- **News and buzz** (`src/fantasy_ai/news/`): ESPN player news, NBC/Rotoworld, RotoBaller and
  FantasyPros RSS, Reddit r/fantasyfootball, and Sleeper trending adds/drops. The LLM turns each
  item into a small JSON label (status, role change, sentiment, confidence). Plain code turns
  labels into **bounded** changes: OUT means 0, questionable is a 15% discount (so a close,
  healthy backup gets the start), role change at most 25%, hype at most 15%, buzz at most 10% on
  rest-of-season value. Every change keeps a cited reason ("Q Star listed questionable (ESPN,
  2026-09-30)") that shows up in texts. X and Instagram have no free API and are not scraped;
  they are stubs that can read a list you provide.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config/leagues.example.yaml config/leagues.yaml   # gitignored
cp .env.example .env                                  # gitignored
.venv/bin/pytest
```

### Find your league_id and team_id

Open your team on the ESPN site. The URL looks like
`https://fantasy.espn.com/football/team?leagueId=123456&teamId=3&seasonId=2026`.
`leagueId` is the league_id, `teamId` is your team_id.

### Copy your ESPN cookies

Private leagues need two cookies from a logged-in browser:

1. Log in at espn.com and open your league.
2. Open DevTools (Cmd+Option+I on Mac), go to **Application > Cookies > https://www.espn.com**.
3. Copy the values of `espn_s2` (long string) and `SWID` (looks like `{XXXXXXXX-...}`).
4. Put them in `.env` under the auth profile for that league, for example
   `ESPN_S2_COUSIN=...` and `SWID_COUSIN={...}`.

Each league in `config/leagues.yaml` names an `auth_profile`; profile `cousin` reads
`ESPN_S2_COUSIN` / `SWID_COUSIN`. One cookie covers every league on that ESPN account. The
cookie expires every few months; an auth error means copy it again. Secrets are only read from
`.env` (or CI secrets) and are never printed.

## Safety model

- **Dry run by default.** Every write prints the exact URL and JSON first. Nothing is sent without
  `--send` (lineups) or `fai approve <id>` (everything else).
- **Own-team guard.** Every payload is checked before sending, dry runs included: the team id,
  every add/drop team id, and every trade item must match the team_id configured for that league.
  This matters because a commissioner's cookie can change other teams' rosters.
- **Approval queue.** Waiver claims, add/drops and trade proposals are never sent automatically.
  They wait in `data/pending.json` with an expiry. `approve` re-reads the league first and aborts
  if a player moved, got picked up, or locked, or if the week changed. Trade accept is not
  implemented at all.
- **No blind retries.** After a network error the roster is read back first. A lineup is only
  retried if the read-back proves nothing changed; a waiver claim is never retried automatically
  (that could double-spend).
- **ESPN errors in plain words.** Typed 409s (`TRAN_LINEUP_LOCKED`, `TRAN_ROSTER_SLOT_LIMIT_EXCEEDED`,
  ...) map to short messages.
- **Pacing (anti-ban).** Reads are cached per run (one fetch per view per league) with 1 to 4 s random
  gaps and a browser-like User-Agent; nothing runs in parallel. Scheduled runs start after a random 0
  to 10 min delay, with 20 to 90 s gaps between leagues and between separate writes. Writes are capped
  per run (3) and per league per day (6). On 401, 403, 429 or any unexpected error there is no retry:
  that league pauses for 24 h (`data/pacing.json`) and you get a text.
- **LLM guardrails.** Any LLM text containing a number that is not in the facts is thrown away
  and the template text is used. LLM calls per run are capped.

Request shapes follow
[heyitaki/espn-fantasy-football-mcp](https://github.com/heyitaki/espn-fantasy-football-mcp) (`src/espn/constants.ts`, `transactions.ts`)
and [jwulff/fantasy-sports](https://github.com/jwulff/fantasy-sports) `docs/research/05-espn-write-surface.md`,
which captured the lineup request live. The add/drop, waiver, cancel and trade shapes are
community-attested and not yet confirmed on the wire.

## Running it on a schedule

### GitHub Actions (public repo safe)

`.github/workflows/autopilot.yml` runs a full autopilot Thursday 2pm ET (news, factors, Vegas, IR,
waiver ideas), a recheck Saturday 2pm ET (lineups + IR only, fresh news and injuries, only changes
what changed), an optional Sunday 11:45am ET inactive check (off unless
`schedule.sunday_inactive_check: true`), waiver suggestions Tue/Wed, and reads Telegram approvals
every 30 minutes in season. Every scheduled run texts each owner a one-line heartbeat, even when
nothing changed ("Saturday recheck: no changes"). If Thursday's run is missed (laptop asleep, CI
outage), the next run before Sunday kickoff upgrades itself to a full run, and a catch-up job checks
every few hours. `fai status` shows the last successful run per league and warns about missed runs.

- Config and cookies come only from secrets: `LEAGUES_YAML` (the whole leagues.yaml),
  `ESPN_S2_<PROFILE>`, `SWID_<PROFILE>`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `ANTHROPIC_API_KEY`, `STATE_KEY` (make one with `fai state keygen`).
- Logs on a public repo are public, so every command runs with `--quiet`: only counts like
  `league 1: 2 moves, sent`. Names, players, payloads and ids go only to Telegram.
- State (approval queue, news cache) is encrypted with `STATE_KEY` before it goes into the
  Actions cache.
- The workflow stays in dry run until you set the repo variable `LIVE=true`, and lineups are only
  sent for leagues with `auto_lineup: true`.
- In CI the LLM is the Anthropic API (`claude-haiku-4-5-20251001` by default, `FAI_LLM_MODEL` to
  change); locally it uses `claude -p`.

### On a Mac

`deploy/launchd/*.plist.template` are launchd jobs for the same schedule (Thursday full, Saturday
recheck, Sunday inactives disabled, hourly catch-up that also runs at login, Tue/Wed waivers,
iMessage polling). They are templates: replace `__REPO__` and load them yourself. launchd runs a
missed calendar job once when the Mac wakes.

## Notifications

`notify.backend` in the config: `console` (default), `telegram`, `imessage` (macOS Messages via
`osascript`), or `twilio`. Phone numbers and tokens live only in `.env`. Messages are split into
about 300 characters.

## ESPN terms of service

ESPN has no official API. This tool uses the same private endpoints the ESPN website uses,
and Disney's terms of service ban automated access. The realistic risk is to your ESPN account;
no bans have been reported, but use it at your own risk. Keep writes to a handful per week, and
do not point it at leagues where you are not a member.

## License

MIT, see `LICENSE`.
