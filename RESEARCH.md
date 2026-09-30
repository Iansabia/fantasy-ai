# AI fantasy football manager: research (2026-09-30)

All leagues are on ESPN. ESPN has no official API; everything is reverse-engineered.

## What ESPN allows (unofficial)
- **Reads:** `lm-api-reads.fantasy.espn.com`. The Python package `espn-api` (cwendt94/espn-api, 981 stars, MIT, active) reads everything. It has **no write support.**
- **Writes:** POST `https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{yr}/segments/0/leagues/{id}/transactions/`
  - Set lineup: `ROSTER` + `LINEUP` items. Tested live 9/12/26. Atomic. Current week only.
  - Free agent add/drop: `FREEAGENT` + `ADD`/`DROP`. Several repos use the same shape.
  - Waiver claim / FAAB bid: `WAIVER` + `bidAmount`. Cancel a claim: `CANCEL`.
  - Propose trade: `TRADE_PROPOSAL`. Known from code only.
  - Accept trade: not recorded anywhere yet.
- **Auth:** the `espn_s2` cookie is enough. One cookie covers every league on that account; configure each league as league_id + team_id. The cookie expires; a 401 means refresh it.
- **Danger:** a commissioner's cookie could change other teams. Hard-guard to our own team only.
- **Errors:** typed 409s, e.g. `TRAN_LINEUP_LOCKED`, `TRAN_ROSTER_SLOT_LIMIT_EXCEEDED`.

## Existing projects (all tiny, weeks old)
| Repo | Writes | Notes |
|---|---|---|
| heyitaki/espn-fantasy-football-mcp (TS, MIT) | lineup, add/drop, waivers (no trades) | MCP server, dryRun + read-only switch, 1 league per instance |
| elcachorrohumano/fantasy-agent (Py, **no license**) | lineup, waivers, trade proposals | Closest to our goal. Multi-league yaml. Math engine decides, Claude explains. Need author permission to fork. |
| Zinkelburger/Fantasy-Football-Tool (AGPL) | ESPN roster moves | Good weekly data (Vegas, injuries) |
| jwulff/fantasy-sports (MIT) | reads only | Best docs on the ESPN write surface |
| dtcarls/fantasy_football_chat_bot (307 stars) | reads only | Discord/GroupMe/Slack bot, good model for text insights |

## Data sources
- Free: ESPN projections (from reads), nflreadpy (nflverse), Sleeper projections, The Odds API free tier.
- Paid: FantasyPros consensus.

## Bar to beat
FantasyPros Auto-Pilot (paid) already auto-sets ESPN lineups. ESPN Quick Lineup is a manual tap in the app.

## ToS
Disney ToS bans automated access. Realistic risk is the ESPN account, no reported bans. Keep writes to a handful per week.

## Recommended build
1. **Data:** espn-api + nflreadpy + Vegas lines + ESPN projections, one config entry per league.
2. **Decisions (plain code, not the LLM):** best lineup under slot rules, bye/Out swaps, waiver and trade value over replacement.
3. **LLM:** Claude writes a short weekly note per league (Discord or text).
4. **Writes:** our own client posting only the changed slots (request shapes from heyitaki `src/espn/constants.ts`).
   - Own-team guard.
   - Read the roster back before retrying (avoid double FAAB).
   - Lineups can be automatic: Tuesday, then ~30 min before each game window.
   - Waivers need approval. Trades are proposed only, never accepted automatically.

Fast start: run heyitaki's MCP server (one per league) in Claude Code for 2 to 3 weeks while capturing real add/drop and trade requests from the browser.
