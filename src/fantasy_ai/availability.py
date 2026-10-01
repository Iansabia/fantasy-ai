"""How likely is an injured player to play, and should we decide now or wait?

1) Play probability by practice report (used only on the FINAL run before a player's game).

Measured on 2023-2025 regular seasons, QB/RB/WR/TE, nflverse load_injuries (final weekly report:
game status + last practice) joined to load_snap_counts (played = any offense / special teams
snap). Raw rates, then divided by 0.93 because players with NO game status and a full final
practice only "played" 93% of the time in the join (name-matching misses and healthy scratches),
so the raw join undercounts everyone by about that much:

  final game status / last practice      n     raw   adjusted (used)
  Questionable, full                   300    72%    0.77
  Questionable, limited                737    61%    0.66
  Questionable, did not practice       194    47%    0.51
  Questionable, any                   1256    62%    0.67
  no status, limited                   348    94%    0.99 (capped 0.98)
  no status, did not practice          204    72%    0.77
  Doubtful, any                        150     1%    0.02
  Out                                 1013     0%    0.00

Midweek (Wednesday / Thursday) reports are not in nflverse, so those rows are derived, not
measured: a player limited on Wednesday ends the week either with no status (plays ~98%), as
Questionable (~66%) or Out; most midweek "limited" tags are maintenance or early-week
caution, so the midweek numbers sit between the two final rows and get closer to the final ones
as the week goes on. Retune these when we log midweek reports.

2) Defer, decide, or treat as active (gametime.py uses these):
  - a later scheduled run happens before his kickoff -> DEFER: no injury haircut now; the final
    run decides (we can still swap him then if he is ruled out).
  - inactives are out (90 min before kickoff) and he is not ruled out -> ACTIVE: full value.
  - otherwise this is the final run -> use the probability above.
"""
from __future__ import annotations

import datetime as dt

# (day, practice) -> P(plays). day: wed / thu / fri ("fri" = the final report for a Sunday game).
PRACTICE_PLAY = {
    ("wed", "full"): 0.97, ("wed", "limited"): 0.85, ("wed", "dnp"): 0.65,
    ("thu", "full"): 0.97, ("thu", "limited"): 0.80, ("thu", "dnp"): 0.55,
    ("fri", "full"): 0.97, ("fri", "limited"): 0.75, ("fri", "dnp"): 0.40,
}
# Final game status (with last practice when known). Measured, see table above.
FINAL_PLAY = {
    ("QUESTIONABLE", "full"): 0.77, ("QUESTIONABLE", "limited"): 0.66, ("QUESTIONABLE", "dnp"): 0.51,
    ("QUESTIONABLE", None): 0.67,
    ("DOUBTFUL", None): 0.02, ("OUT", None): 0.0,
}
DAYS = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
INACTIVES_LEAD = dt.timedelta(minutes=90)      # NFL inactives are announced 90 min before kickoff


def practice_day(day: str | None, published: str | None) -> str | None:
    """'wed' / 'thu' / 'fri' from the item, else from its publish date. Sat/Sun/Mon count as the
    final report ('fri'); Tuesday as Wednesday (first report of the week)."""
    d = (day or "").lower()[:3] or None
    if d is None and published:
        try:
            d = DAYS[dt.datetime.fromisoformat(published.replace("Z", "+00:00")).weekday()]
        except ValueError:
            d = None
    if d is None:
        return None
    return {"tue": "wed", "sat": "fri", "sun": "fri", "mon": "fri"}.get(d, d)


def play_probability(game_status: str | None, practice: str | None = None, day: str | None = None) -> float | None:
    """P(plays) from what we know. None when we know nothing useful."""
    st = (game_status or "").upper()
    if st in ("OUT", "INJURY_RESERVE", "IR", "SUSPENSION"):
        return 0.0
    if st == "DOUBTFUL":
        return FINAL_PLAY[("DOUBTFUL", None)]
    if st == "QUESTIONABLE" and (day in (None, "fri")):
        return FINAL_PLAY.get((st, practice), FINAL_PLAY[(st, None)])
    if practice and day in ("wed", "thu", "fri"):
        return PRACTICE_PLAY[(day, practice)]
    if st == "QUESTIONABLE":
        return FINAL_PLAY[(st, None)]
    return None
