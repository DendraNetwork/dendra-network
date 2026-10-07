"""Final Testnet Season rules (ADR-047), in ONE place, as integers of udndr.

Every other piece of the programme imports these values: the calculation, the programme service, the
generator, the payout and the desktop application. A rate restated in a second file is a
second value free to drift away from the first, and the programme promises a formula anyone can rerun:
the formula has to exist once.

`fingerprint()` hashes the rules; every published ranking carries it, so a reader can tell which rules a
ranking was computed with and rerun it with the same ones.
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import json

UDNDR = 1_000_000  # 1 DNDR

RULES = {
    "season": "final-testnet",
    # Counted in BLOCKS, the unit the chain counts. A "day" is 17 280 blocks: about 24 h at about 5 s per
    # block, an assumption the chain does not enforce (the interval between blocks emerges from
    # timeout_commit and propagation). The programme pays per block-day, whatever its wall-clock length.
    "day_blocks": 17_280,
    # THE SEASON ENDS AT A FIXED INSTANT, read from the block headers (owner's decision of 2026-10-07,
    # ADR-047 decision 17): it holds every block whose header time is BEFORE this instant, and no other —
    # "it ends on 7 November 2026 at 23:59 UTC". Days stay 17 280-block days from the start height; the
    # day that holds the season's last block is cut there. How many days that makes is a READING of the
    # chain (`season_end_height`, then `day_of_height`), never a rule: it moves with the block interval,
    # which the consensus does not fix. A count written here was "30" until the end became a date.
    "end_time": "2026-11-08T00:00:00Z",
    # Rewards, all paid on facts the chain records (owner's decisions of 2026-10-05: no artificial test).
    # Presence is an availability window proven ON CHAIN (MsgProveAvailability), and it is paid only on a
    # day the identity also served at least one verified request: proving presence needs no model, the
    # work does (`final_season_calc.gross_of`).
    # The owner's decision of 2026-10-06 divided every rate and cap by ten (ADR-047 decision 15): one
    # mainnet DNDR per point makes the season's budget the size of the mainnet conversion.
    "presence_per_window": 2_000,           # 0.002 DNDR per availability window proven on chain
    "presence_max_windows": 50,             # at most 50 windows a day
    "work_per_request": 50_000,             # 0.05 DNDR per verified programme request, the same for all
    "juror_per_verdict": 8_000,             # 0.008 DNDR per verdict consistent with the outcome
    # Caps, on the programme as a whole only. There is no cap per identity (owner's decision of
    # 2026-10-05): the work an identity can get is set by the chain's stake-weighted draw over a fixed
    # daily volume, so a cap per identity would only pay an operator to split its stake into identities
    # that each stay under it.
    "cap_programme_day": 50 * UDNDR,        # 50 DNDR per day for the whole programme
    "cap_season": 1_500 * UDNDR,            # 1 500 DNDR over the season
    # Work volume sent by the project: a FIXED number of requests a day, which the chain hands out among
    # its present miners, drawn by stake (each identity's weight capped: ADR-047, Consequences). A number
    # per identity would make every identity added raise the programme's total of paid work.
    "requests_per_day": 600,
}


def fingerprint(rules: dict | None = None) -> str:
    r = RULES if rules is None else rules
    return hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def day_of_height(height: int, start_height: int, rules: dict | None = None) -> int:
    """Day index (0-based) of a block height, or -1 before the season starts."""
    r = RULES if rules is None else rules
    if height < start_height:
        return -1
    return (height - start_height) // r["day_blocks"]


def end_epoch(rules: dict | None = None) -> int:
    """The season's end, in whole UTC seconds: a block belongs to the season iff its header time is
    strictly earlier. The rule is a whole second, so comparing the header time floored to the second is
    exact (floor(t) < E  <=>  t < E for an integer E)."""
    r = RULES if rules is None else rules
    t = datetime.datetime.strptime(r["end_time"], "%Y-%m-%dT%H:%M:%SZ")
    return calendar.timegm(t.timetuple())


def day_bounds(day: int, start_height: int, rules: dict | None = None,
               end_height: int | None = None) -> tuple[int, int]:
    """First and last block height (inclusive) of a day.

    `end_height` is the season's last block (`final_season_chain.season_end_height`) once the chain has
    passed the end: it cuts the day that holds it, and a day that starts after it is NOT a season day —
    refused, never returned as an empty or inverted window, which every search would read as "nothing
    happened that day". None means the end is not known YET: the full 17 280-block day, which only a
    caller that has checked the day lies wholly before the end (`final_season_rank.season_window`) may use."""
    r = RULES if rules is None else rules
    first = start_height + day * r["day_blocks"]
    last = first + r["day_blocks"] - 1
    if end_height is not None:
        if first > end_height:
            raise ValueError(f"day {day} starts at block {first}, after the season's last block {end_height}")
        last = min(last, end_height)
    return first, last
