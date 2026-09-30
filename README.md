# fantasy-ai

An AI helper for ESPN fantasy football leagues. It sets your lineup, suggests waiver pickups,
checks trades, reads the news, and texts you a short weekly note.

The rule that shapes everything: **plain code makes every decision. The LLM only labels news
and writes explanation text, and it is never allowed to invent a number.**

## What it does

| Command | What happens |
|---|---|
| `fai leagues` | List configured leagues and whether cookies are found |
| `fai lineup <league> [--send]` | Best legal lineup by projection. Prints the moves and the exact ESPN request. Sends only with `--send` |
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
  D/ST, K, bench, IR) using each player's `eligibleSlots`. Anyone on bye, OUT, IR or suspended is
  benched. Locked players (game started) never move. Only changed slots are sent, in one request.
- **Waivers:** rest-of-season projected points vs. the weakest droppable player at the same
  position. Locked and IR-slot players are never suggested as drops.
- **Trades:** raw value swap plus the change in each team's best starting lineup.
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
- **LLM guardrails.** Any LLM text containing a number that is not in the facts is thrown away
  and the template text is used. LLM calls per run are capped.

Request shapes follow
[heyitaki/espn-fantasy-football-mcp](https://github.com/heyitaki/espn-fantasy-football-mcp) (`src/espn/constants.ts`, `transactions.ts`)
and [jwulff/fantasy-sports](https://github.com/jwulff/fantasy-sports) `docs/research/05-espn-write-surface.md`,
which captured the lineup request live. The add/drop, waiver, cancel and trade shapes are
community-attested and not yet confirmed on the wire.

## Running it on a schedule

### GitHub Actions (public repo safe)

`.github/workflows/autopilot.yml` runs lineups about an hour before each lock (Thu, Sun early,
Sun late, Sun night, Mon), waiver suggestions Tue/Wed, and reads Telegram approvals every 30
minutes in season.

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

`deploy/launchd/*.plist.template` are launchd jobs for the same schedule. They are templates:
replace `__REPO__` and load them yourself.

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
