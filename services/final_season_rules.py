"""Final Testnet Season rules (ADR-047), in ONE place, as integers of udndr.

Every other piece of the programme imports these values: the calculation, the programme service, the
generator, the payout and the desktop application. A rate restated in a second file is a
second value free to drift away from the first, and the programme promises a formula anyone can rerun:
the formula has to exist once.

`fingerprint()` hashes the rules; every published ranking carries it, so a reader can tell which rules a
ranking was computed with and rerun it with the same ones.

AN AMENDMENT IS A NEW RULE SET, NEVER AN EDIT OF THE OLD ONE. `RULES_17` are the rules as of decision 17 of
ADR-047, the season's rules until decision 18; `RULES` are the rules in force. A day is ranked under the set in
force on that day (`rules_for_day`), carries ITS fingerprint, and is paid under it
(`final_season_payout.check_days` reads each day's set from its fingerprint, `RULE_HISTORY`): a day already
published is never recomputed under rules it was not computed with -- its finality included (`finality_rule`).
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import json

UDNDR = 1_000_000  # 1 DNDR

# The rules as of decision 17 (2026-10-07). KEPT AS THEY WERE PUBLISHED, NEVER EDITED: their fingerprint is the
# one the service published before decision 18 (c591851f..., measured on its status on 2026-10-08), every day
# ranked before decision 18 took effect carries it, and the payment checks those days against THESE values.
RULES_17 = {
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
    # 2026-10-05): the work an identity can get is its share of the chain's work draw over a fixed daily
    # volume, each identity's stake weighing up to `assignment_stake_cap_multiple` x `min_stake`
    # (`committee.go::capAssignmentWeights`), so a cap per identity would only pay an operator to split its
    # stake into identities that each stay under it.
    "cap_programme_day": 50 * UDNDR,        # 50 DNDR per day for the whole programme
    "cap_season": 1_500 * UDNDR,            # 1 500 DNDR over the season
    # Work volume sent by the project: a FIXED number of requests a day, which the chain hands out among
    # its present miners, each identity's stake weighing up to the ceiling above (ADR-047, Consequences).
    # A number per identity would make every identity added raise the programme's total of paid work.
    "requests_per_day": 600,
}

# DECISION 18 (owner, 2026-10-08: "pay the work delivered"). A programme job whose audit was UNWOUND -- no jury
# could conclude it within `audit_unwind_blocks`, so the chain refunded its client and neither paid nor slashed
# its miner (`audit_unwind.go::unwindStrandedRetention`, state `+resolved+unwound`) -- is paid by the season as
# a verified request when BOTH hold: its answer is in the programme's seal of the day it arrived
# (`final_season_facts.answered_jobs`) AND the season's grading does not say incoherent. The job's OWN answer
# decides when it was sampled and graded: coherent is paid, incoherent never, whatever the rest of the day says.
# An answer that was not graded itself falls under the grading of its miner's day, by the rule that takes a
# day's work away (`final_season_facts._graded_out`): paid when that day has at least one grade and is not
# graded out. Not graded, or unreadable, is not paid. An audit concluded against the miner is never paid, and an
# unwound audit's verdicts pay no juror (no juror judged). The value names the rule;
# `final_season_facts.unwound_work` applies it, and `pays_unwound_work` refuses any other value.
UNWOUND_AUDIT_WORK = "paid_if_answered_and_graded_own_answer_first"
# WHEN A DAY IS FINAL, a rule of the set too: it decides which jobs have their last state when the day is
# ranked, so which ones pay. RULES_17 do not name it, and every day ranked under them was final
# `audit_unwind_blocks` + 200 blocks past its last counted block (FINALITY_17: the service published them so,
# and a recompute must find the same window). From decision 18 on, one audit deadline more
# (`audit_resolve_timeout`): an audit drawn late can resolve up to that many blocks past the unwind bound
# (`final_season_rank.finality_blocks`).
FINALITY_17 = "audit_unwind_blocks+200"
FINALITY = "audit_unwind_blocks+audit_resolve_timeout+200"
RULES = dict(RULES_17, unwound_audit_work=UNWOUND_AUDIT_WORK, finality=FINALITY)
# Every rule set the season has published, in the order they took effect. A day's set is read from its
# fingerprint, and a later day never goes back to an earlier set.
RULE_HISTORY = (RULES_17, RULES)


def fingerprint(rules: dict | None = None) -> str:
    r = RULES if rules is None else rules
    return hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rule_set_of(fp) -> tuple:
    """(position in RULE_HISTORY, rules) of the rule set whose fingerprint is `fp`; (None, None) for a
    fingerprint of no published set -- never the current rules in its place."""
    for n, r in enumerate(RULE_HISTORY):
        if fingerprint(r) == fp:
            return n, r
    return None, None


def rules_for_day(day: int, unwound_from) -> dict:
    """The rules day `day` is ranked under. `unwound_from` is the first day decision 18 applies to, read where
    it was fixed (`final_season_server.State`, published in the service's status), or None when it applies to
    no day (a service that predates it). Days before it keep RULES_17: a published day is never recomputed
    under rules it was not computed with. Anything else is refused, never read as either."""
    if type(day) is not int or day < 0:
        raise ValueError(f"day {day!r} is not a season day")
    if unwound_from is None:
        return RULES_17
    if type(unwound_from) is not int or unwound_from < 0:
        raise ValueError(f"the first day of decision 18 {unwound_from!r} is neither a day nor None")
    return RULES if day >= unwound_from else RULES_17


def pays_unwound_work(rules: dict) -> bool:
    """Whether `rules` pay the work of an unwound audit (decision 18). Read from the rules themselves, so a
    ranking's own fingerprint says which way it went. A rule set that names it with another value raises:
    a rule this code does not know is applied neither way."""
    if "unwound_audit_work" not in rules:
        return False
    if rules["unwound_audit_work"] != UNWOUND_AUDIT_WORK:
        raise ValueError(f"unknown unwound_audit_work rule {rules['unwound_audit_work']!r}")
    return True


def finality_rule(rules: dict) -> str:
    """FINALITY or FINALITY_17: when a day ranked under `rules` is final. A set that does not name it is RULES_17,
    identified by its fingerprint and nothing else: any other set without it raises, and so does a value this code
    does not know -- a finality rule is never guessed, since it decides which jobs are paid."""
    if "finality" not in rules:
        if fingerprint(rules) != fingerprint(RULES_17):
            raise ValueError("a rule set that names no finality rule and is not RULES_17")
        return FINALITY_17
    if rules["finality"] != FINALITY:
        raise ValueError(f"unknown finality rule {rules['finality']!r}")
    return FINALITY


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
