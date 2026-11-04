"""Final Testnet Season rules, in one place, as integers of udndr.

Every other piece of the programme imports these values: the calculation, the programme service, the
generator, the payout and the desktop application. A rate restated in a second file is a
second value free to drift away from the first, and the programme promises a formula anyone can rerun:
the formula has to exist once.

`fingerprint()` hashes the rules; every published ranking carries it, so a reader can tell which rules a
ranking was computed with and rerun it with the same ones.

An amendment is a new rule set, never an edit of the old one. This code names each amendment by its number in the
order the season adopted them (decision 15, 17, 18, 22, 23, 24), and each number from 17 on names a rule set here:
`RULES_17` are the rules as of decision 17, the season's rules until decision 18; `RULES` are the rules of decision
18 (the work of an unwound audit is paid, from the day `unwound_audit_work_from_day`); `RULES_DOUBLED` (decision 22)
are the same rules with twice the rewards and caps, from the day the service fixed for them; `RULES_MORE_REQUESTS` are
the doubled rules with a larger daily volume of requests (decision 23), from the day the service fixed for them;
`RULES_KIT` (decision 24) adds the judge presence reward, the stale-kit refusal
and the probation of new identities, from the day the service fixed for them, and carries the kit floor it refuses
below (`kit_min`), so that its fingerprint names the floor; `RULES_JURY` is RULES_KIT with a juror paid on
every timely verdict whatever its value, contradicted on controlled-fault requests only, and carries the periods of
those requests with their commitments, so that its fingerprint names them. A day is ranked under the set in force on that day
(`rules_for_day`), carries its fingerprint, and is paid under it (`final_season_payout.check_days` reads each
day's set from its fingerprint, `RULE_HISTORY`): a day already published is never recomputed under rules it was
not computed with -- its finality included (`finality_rule`).
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import os
import re

from modea import kit_floor

UDNDR = 1_000_000  # 1 DNDR

# The rules as of decision 17. Kept as they were published, never edited: their fingerprint is the one
# the service published before decision 18 (c591851f...), every day ranked before decision 18 took effect
# carries it, and the payment checks those days against these values.
RULES_17 = {
    "season": "final-testnet",
    # Counted in blocks, the unit the chain counts. A "day" is 17 280 blocks: about 24 h at about 5 s per
    # block, an assumption the chain does not enforce (the interval between blocks emerges from
    # timeout_commit and propagation). The programme pays per block-day, whatever its wall-clock length.
    "day_blocks": 17_280,
    # The season ends at a fixed instant, read from the block headers: it holds every block whose header time is before this instant, and no other —
    # "it ends on 7 November 2026 at 23:59 UTC". Days stay 17 280-block days from the start height; the
    # day that holds the season's last block is cut there. How many days that makes is a reading of the
    # chain (`season_end_height`, then `day_of_height`), never a rule: it moves with the block interval,
    # which the consensus does not fix.
    "end_time": "2026-11-08T00:00:00Z",
    # Rewards, all paid on facts the chain records (no test of the programme's own).
    # Presence is an availability window proven on chain (MsgProveAvailability), and it is paid only on a
    # day the identity also served at least one verified request: proving presence needs no model, the
    # work does (`final_season_calc.gross_of`).
    # Decision 15 divided every rate and cap by ten: one mainnet DNDR per point makes the season's budget the size of the mainnet conversion.
    "presence_per_window": 2_000,           # 0.002 DNDR per availability window proven on chain
    "presence_max_windows": 50,             # at most 50 windows a day
    "work_per_request": 50_000,             # 0.05 DNDR per verified programme request, the same for all
    "juror_per_verdict": 8_000,             # 0.008 DNDR per verdict consistent with the outcome
    # Caps, on the programme as a whole only. There is no cap per identity: the work an identity can get is its share of the chain's work draw over a fixed daily
    # volume, each identity's stake weighing up to `assignment_stake_cap_multiple` x `min_stake`
    # (`committee.go::capAssignmentWeights`), so a cap per identity would only pay an operator to split its
    # stake into identities that each stay under it.
    "cap_programme_day": 50 * UDNDR,        # 50 DNDR per day for the whole programme
    "cap_season": 1_500 * UDNDR,            # 1 500 DNDR over the season
    # Work volume sent by the project: a fixed number of requests a day, which the chain hands out among
    # its present miners, each identity's stake weighing up to the ceiling above.
    # A number per identity would make every identity added raise the programme's total of paid work.
    "requests_per_day": 600,
}

# Decision 18 ("pay the work delivered"). A programme job whose audit was unwound -- no jury
# could conclude it within `audit_unwind_blocks`, so the chain refunded its client and neither paid nor slashed
# its miner (`audit_unwind.go::unwindStrandedRetention`, state `+resolved+unwound`) -- is paid by the season as
# a verified request when both hold: its answer is in the programme's seal of the day it arrived
# (`final_season_facts.answered_jobs`) and the season's grading does not say incoherent. The job's own answer
# decides when it was sampled and graded: coherent is paid, incoherent never, whatever the rest of the day says.
# An answer that was not graded itself falls under the grading of its miner's day, by the rule that takes a
# day's work away (`final_season_facts._graded_out`): paid when that day has at least one grade and is not
# graded out. Not graded, or unreadable, is not paid. An audit concluded against the miner is never paid, and an
# unwound audit's verdicts pay no juror (no juror judged). The value names the rule;
# `final_season_facts.unwound_work` applies it, and `pays_unwound_work` refuses any other value.
UNWOUND_AUDIT_WORK = "paid_if_answered_and_graded_own_answer_first"
# When a day is final, a rule of the set too: it decides which jobs have their last state when the day is
# ranked, so which ones pay. RULES_17 do not name it, and every day ranked under them was final
# `audit_unwind_blocks` + 200 blocks past its last counted block (FINALITY_17: the service published them so,
# and a recompute must find the same window). From decision 18 on, one audit deadline more
# (`audit_resolve_timeout`): an audit drawn late can resolve up to that many blocks past the unwind bound
# (`final_season_rank.finality_blocks`).
FINALITY_17 = "audit_unwind_blocks+200"
FINALITY = "audit_unwind_blocks+audit_resolve_timeout+200"
RULES = dict(RULES_17, unwound_audit_work=UNWOUND_AUDIT_WORK, finality=FINALITY)
# Twice the rewards, the daily cap and the season cap, from one day to
# the end of the season. Every other rule is the one of RULES, decision 18 and its finality included. The season
# cap is a total over every day: a day under this set has a budget of what remains of 3 000 DNDR after all the
# earlier days paid, whatever set they were paid under (`final_season_calc.compute_day`).
RULES_DOUBLED = dict(RULES, presence_per_window=4_000, work_per_request=100_000, juror_per_verdict=16_000,
                     cap_programme_day=100 * UDNDR, cap_season=3_000 * UDNDR)
# More requests a day, from one day to the end of the season, at the rates and caps of
# RULES_DOUBLED. The generator is what acts on `requests_per_day` (`final_season_generator.main`); no amount of
# the calculation or of the payment reads it (`final_season_calc.gross_of`, `final_season_calc.compute_day`), so a
# day under this set pays what the same counts pay under RULES_DOUBLED, within the same daily and season caps.
RULES_MORE_REQUESTS = dict(RULES_DOUBLED, requests_per_day=900)
# The set of the judge presence reward, the kit floor (a kit below the floor is
# paid nothing) and the probation of new identities (the work plan's Q7), from one day to the end of the season.
# Three keys more than RULES_MORE_REQUESTS, nothing else changed:
#   judge_active_per_day     0.5 DNDR to an identity that was an active judge that day (the
#                            five conditions read in `final_season_facts.judge_presence`), once a day, within the
#                            same daily cap, season cap and pro rata as every other reward (`final_season_calc.gross_of`);
#   probation_first_day_bps  an identity earns this share of its gross on its first full season day after its
#                            registration (`final_season_facts.first_full_day`), 6 000 bps = 60 percent;
#   kit_min                  the kit number below which an identity is paid nothing for the day: its latest signed
#                            capacity report at or before the day's end declares a lower `kit_version`, or no
#                            signed report exists (`final_season_calc.kit_allows_pay`). The number is the kit
#                            number of the release the announcement names; the set carries
#                            it so that the fingerprint of a published day names the floor it was paid under.
# `kit_min` comes from the environment (DENDRA_KIT_MIN) the first time the service fixes the set's day, is kept
# with that day's record (`final_season_server.State`) and read back from it at every later start: the recorded
# value wins, and an environment that names another is refused, as a day once fixed is not moved. Without the
# number the set does not exist (RULES_KIT is None and RULE_HISTORY ends at RULES_MORE_REQUESTS): a reader that
# meets a day ranked under it recovers the floor from that day's published inputs (`final_season_payout`).
JUDGE_ACTIVE_PER_DAY = 500_000             # 0.5 DNDR per active judge per day (owner, 2026-10-15)
PROBATION_FIRST_DAY_BPS = 6_000            # 60 percent of the gross on the first full day (owner, 2026-10-15)
KIT_MIN_ENV = kit_floor.KIT_MIN_ENV


def kit_min_setting(raw) -> int | None:
    """The kit floor an environment names: None when it is empty or unset, the integer when it is one, and a
    refusal for anything else. Read by `modea.kit_floor.parse_kit_min`, as the relay and the faucet read it."""
    return kit_floor.parse_kit_min(raw)


def kit_rules(kit_min: int) -> dict:
    """RULES_KIT for the kit floor `kit_min`: RULES_MORE_REQUESTS with the three keys above."""
    if type(kit_min) is not int or kit_min < 1:
        raise ValueError(f"kit_min {kit_min!r} is not a kit number (a positive integer)")
    return dict(RULES_MORE_REQUESTS, judge_active_per_day=JUDGE_ACTIVE_PER_DAY,
                probation_first_day_bps=PROBATION_FIRST_DAY_BPS, kit_min=kit_min)


# The sets whose values this file fixes whatever the environment says. RULES_KIT joins them once its floor is known.
PUBLISHED_SETS = (RULES_17, RULES, RULES_DOUBLED, RULES_MORE_REQUESTS)
KIT_MIN = kit_min_setting(os.environ.get(KIT_MIN_ENV))
RULES_KIT = None if KIT_MIN is None else kit_rules(KIT_MIN)
# Every rule set the season has published, in the order they took effect. A day's set is read from its
# fingerprint, and a later day never goes back to an earlier set.
RULE_HISTORY = PUBLISHED_SETS + (() if RULES_KIT is None else (RULES_KIT,))

# The jury set. RULES_KIT and three keys more, nothing else changed:
#   juror_rule     JUROR_RULE: `juror_per_verdict` for each verdict of a drawn juror, other than the audited miner, on
#                  the audit of a programme request settled that day, concluded or unwound, committed before the
#                  audit's resolution (the `audit_retention_unwound` block for an unwound audit) and after the
#                  arrival of the reveal sealed to that juror; the vote's value is never read
#                  (`final_season_facts.jury_verdicts`). A juror that votes "1" on a controlled-fault request of
#                  a class in CONTRADICTING_CLASSES loses its verdicts and its judge day for that day
#                  (`final_season_facts.fault_contradictions`); nothing else contradicts a juror under this set.
#                  The judge presence reward keeps decision 24's seats, on concluded audits only: the jury set changes the
#                  pay of a verdict, not the seats of the judge presence reward (owner's answer R8 of 23/10, Q-a waits for V4).
#   fault_periods  [{"from_day", "to_day", "commitment"}]: the days of each period of controlled-fault requests,
#                  public, and the sha256 of its canonical document (`canonical_fault_document`), whose identities
#                  stay secret until the reveal. A day of a period is ranked only with the reveal that matches it,
#                  and a later day with the reveal of every period begun by then (`started_fault_periods`), so the
#                  identities of a period are left out of the pay on every day from its first.
#   fault_payer    the season payer, the account whose transfer of 1 udndr to itself carries each commitment in
#                  its memo (`fault_memo`), read on chain below the period's first block.
# The periods and the payer come from the environment (FAULT_PERIODS_ENV, FAULT_PAYER_ENV) the first time the service
# fixes the set's day, are kept with that day's record (`final_season_server.State`) and read back from it at every
# later start. Without them, or without the kit floor, the set does not exist (RULES_JURY is None): a reader that
# meets a day ranked under it rebuilds it from the day's published inputs (`jury_set_named`).
JUROR_RULE = "verdict_paid_whatever_the_vote_contradicted_on_incoherent_or_no_reveal_faults"
FAULT_CLASSES = ("incoherent_answer", "plausible_false_fact", "no_reveal")
# A plausible false fact contradicts nobody: an honest judge can read it as valid by the benefit of the doubt.
# It is measured and published.
CONTRADICTING_CLASSES = ("incoherent_answer", "no_reveal")
FAULT_DOC_VERSION = 1
FAULT_MEMO_PREFIX = "dendra-red-team-"
FAULT_PERIODS_ENV = "DENDRA_FINAL_SEASON_FAULT_PERIODS"
FAULT_PAYER_ENV = "DENDRA_FINAL_SEASON_FAULT_PAYER"
_PERIOD = re.compile(r"([0-9]{1,6})-([0-9]{1,6}):([0-9a-f]{64})\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PAYER = re.compile(r"dendra1[0-9a-z]{38,58}\Z")


def check_fault_periods(periods) -> tuple:
    """((from_day, to_day, commitment), ...) of `periods`, checked: at least one; each a pair of season days in order
    and a sha256 in lowercase hex; the periods in order and disjoint, each commitment once. Anything else raises: a
    set without a controlled-fault request would pay a blind valid vote in full, and two periods over one day would
    leave that day's identities ambiguous."""
    if not isinstance(periods, (list, tuple)) or not periods:
        raise ValueError(f"fault periods {periods!r}: at least one period is required")
    out, last, seen = [], -1, set()
    for p in periods:
        if isinstance(p, dict):
            if set(p) != {"from_day", "to_day", "commitment"}:
                raise ValueError(f"fault period {p!r} does not name from_day, to_day and commitment only")
            p = (p["from_day"], p["to_day"], p["commitment"])
        if not (isinstance(p, (list, tuple)) and len(p) == 3):
            raise ValueError(f"fault period {p!r} is not (from_day, to_day, commitment)")
        a, b, c = p
        if type(a) is not int or type(b) is not int or not 0 <= a <= b:
            raise ValueError(f"fault period {p!r}: the days are not two season days in order")
        if not isinstance(c, str) or not _HEX64.match(c):
            raise ValueError(f"fault period {p!r}: the commitment is not a sha256 in lowercase hex")
        if a <= last or c in seen:
            raise ValueError(f"fault period {p!r} overlaps or repeats an earlier one")
        out.append((a, b, c))
        last = b
        seen.add(c)
    return tuple(out)


def fault_periods_setting(raw):
    """The fault periods an environment names, `<from>-<to>:<sha256>[,<from>-<to>:<sha256>]`: None when it is empty
    or unset, the checked periods when it is that form, and a refusal for anything else."""
    s = (raw or "").strip()
    if not s:
        return None
    periods = []
    for part in s.split(","):
        m = _PERIOD.match(part.strip())
        if not m:
            raise ValueError(f"{FAULT_PERIODS_ENV}={raw!r} is not <from>-<to>:<sha256>[,<from>-<to>:<sha256>]")
        periods.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    return check_fault_periods(periods)


def fault_payer_setting(raw):
    """The season payer an environment names: None when it is empty or unset, the address when it is one."""
    s = (raw or "").strip()
    if not s:
        return None
    if not _PAYER.match(s):
        raise ValueError(f"{FAULT_PAYER_ENV}={raw!r} is not an account address")
    return s


def jury_rules(kit_min: int, periods, payer: str) -> dict:
    """RULES_JURY for the kit floor `kit_min`, the fault periods `periods` and the season payer `payer`."""
    checked = check_fault_periods(periods)
    if not isinstance(payer, str) or not _PAYER.match(payer):
        raise ValueError(f"fault payer {payer!r} is not an account address")
    return dict(kit_rules(kit_min), juror_rule=JUROR_RULE, fault_payer=payer,
                fault_periods=[{"from_day": a, "to_day": b, "commitment": c} for a, b, c in checked])


FAULT_PERIODS = fault_periods_setting(os.environ.get(FAULT_PERIODS_ENV))
FAULT_PAYER = fault_payer_setting(os.environ.get(FAULT_PAYER_ENV))
RULES_JURY = (None if (RULES_KIT is None or FAULT_PERIODS is None or FAULT_PAYER is None)
              else jury_rules(KIT_MIN, FAULT_PERIODS, FAULT_PAYER))
RULE_HISTORY = RULE_HISTORY + (() if RULES_JURY is None else (RULES_JURY,))


def set_kit_min(kit_min: int) -> dict:
    """Fix the kit floor after import, from the record the service kept (`final_season_server.State`) or a
    published ranking (`final_season_payout`), and build RULES_KIT and RULE_HISTORY from it. Returns RULES_KIT.
    A floor already fixed to another number is refused: two floors would be two sets with two fingerprints in one
    process, and a day checked against the wrong one."""
    global KIT_MIN, RULES_KIT, RULE_HISTORY, RULES_JURY
    rules = kit_rules(kit_min)
    if KIT_MIN is not None and KIT_MIN != kit_min:
        raise ValueError(f"the kit floor is already fixed at {KIT_MIN}; {kit_min} names another one")
    KIT_MIN, RULES_KIT = kit_min, rules
    # The jury set carries the floor: with its periods and its payer named in the environment, it exists from now on.
    if RULES_JURY is None and FAULT_PERIODS is not None and FAULT_PAYER is not None:
        RULES_JURY = jury_rules(kit_min, FAULT_PERIODS, FAULT_PAYER)
    RULE_HISTORY = PUBLISHED_SETS + (RULES_KIT,) + (() if RULES_JURY is None else (RULES_JURY,))
    return RULES_KIT


def set_fault_commitments(periods, payer: str) -> dict:
    """Fix the fault periods and the season payer after import, from the record the service kept
    (`final_season_server.State`) or a published ranking (`jury_set_named`), and build RULES_JURY from them and the
    kit floor, which must be fixed first. Returns RULES_JURY. Values already fixed to others are refused: two jury sets
    in one process would be two fingerprints, and a day checked against the wrong one."""
    global FAULT_PERIODS, FAULT_PAYER, RULES_JURY, RULE_HISTORY
    if KIT_MIN is None:
        raise ValueError("the jury set carries the kit floor of decision 24, which is not fixed")
    checked = check_fault_periods(periods)
    rules = jury_rules(KIT_MIN, checked, payer)
    if (FAULT_PERIODS is not None and FAULT_PERIODS != checked) or (FAULT_PAYER is not None and FAULT_PAYER != payer):
        raise ValueError("the fault periods or the season payer are already fixed to other values")
    FAULT_PERIODS, FAULT_PAYER, RULES_JURY = checked, payer, rules
    RULE_HISTORY = PUBLISHED_SETS + (RULES_KIT, RULES_JURY)
    return RULES_JURY


def jury_set_named(rules, fp):
    """RULES_JURY when `rules` (a status's `rules`, or the keys a published day carries under `inputs.jury` with its
    kit floor) build the set whose fingerprint is `fp`; then the set is fixed in this process. None otherwise, and
    nothing is fixed: the only values a reader takes from the network are those the fingerprint binds."""
    if not isinstance(rules, dict) or not isinstance(fp, str):
        return None
    kit_min, periods, payer = rules.get("kit_min"), rules.get("fault_periods"), rules.get("fault_payer")
    try:
        built = jury_rules(kit_min, periods, payer)
    except (ValueError, TypeError):
        return None
    if fingerprint(built) != fp:
        return None
    if RULES_JURY is not None:
        return RULES_JURY if fingerprint(RULES_JURY) == fp else None
    if KIT_MIN is not None and KIT_MIN != kit_min:
        return None
    if (FAULT_PERIODS is not None and FAULT_PERIODS != check_fault_periods(periods)) or (
            FAULT_PAYER is not None and FAULT_PAYER != payer):
        return None
    if KIT_MIN is None:
        set_kit_min(kit_min)
    return set_fault_commitments(periods, payer)


def fingerprint(rules: dict | None = None) -> str:
    # No argument: the fingerprint of RULES (decision 18), not of the set a given day is ranked under.
    r = RULES if rules is None else rules
    return hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rule_set_of(fp) -> tuple:
    """(position in RULE_HISTORY, rules) of the rule set whose fingerprint is `fp`; (None, None) for a
    fingerprint of no published set -- never the current rules in its place."""
    for n, r in enumerate(RULE_HISTORY):
        if fingerprint(r) == fp:
            return n, r
    return None, None


def rules_for_day(day: int, unwound_from, doubled_from, more_requests_from, kit_rules_from) -> dict:
    """The rules day `day` is ranked under. `unwound_from` is the first day decision 18 applies to,
    `doubled_from` the first day of RULES_DOUBLED, `more_requests_from` the first day of RULES_MORE_REQUESTS and
    `kit_rules_from` the first day of RULES_KIT (decision 24), each read where it was fixed
    (`final_season_server.State`, published in the service's status); None when the set applies to no day (a
    service that predates it). Days before `unwound_from` keep RULES_17, days from `doubled_from` on take
    RULES_DOUBLED, days from `more_requests_from` on take RULES_MORE_REQUESTS, days from `kit_rules_from` on take
    RULES_KIT: a published day is never recomputed under rules it was not computed with. All four are required,
    with no default. A value that is neither a day nor None is refused, and so is a set without the one before it
    or before its first day: the sets take effect in the order of RULE_HISTORY. A day under RULES_KIT with no kit
    floor fixed (RULES_KIT is None) is refused too: the set does not exist without its floor."""
    if type(day) is not int or day < 0:
        raise ValueError(f"day {day!r} is not a season day")
    if unwound_from is not None and (type(unwound_from) is not int or unwound_from < 0):
        raise ValueError(f"the first day of decision 18 {unwound_from!r} is neither a day nor None")
    if doubled_from is not None:
        if type(doubled_from) is not int or doubled_from < 0:
            raise ValueError(f"the first day of the doubled rates {doubled_from!r} is neither a day nor None")
        if unwound_from is None or doubled_from < unwound_from:
            raise ValueError(f"the doubled rates from day {doubled_from} would take effect before decision 18 "
                             f"(from day {unwound_from!r})")
    if more_requests_from is not None:
        if type(more_requests_from) is not int or more_requests_from < 0:
            raise ValueError(f"the first day of decision 23 (more requests a day) {more_requests_from!r} is neither "
                             f"a day nor None")
        if doubled_from is None or more_requests_from < doubled_from:
            raise ValueError(f"decision 23 (more requests a day) from day {more_requests_from} would take effect "
                             f"before the doubled rates (from day {doubled_from!r})")
    if kit_rules_from is not None:
        if type(kit_rules_from) is not int or kit_rules_from < 0:
            raise ValueError(f"the first day of decision 24 (the kit rule set) {kit_rules_from!r} is neither a day "
                             f"nor None")
        if more_requests_from is None or kit_rules_from < more_requests_from:
            raise ValueError(f"decision 24 (the kit rule set) from day {kit_rules_from} would take effect before "
                             f"decision 23 (from day {more_requests_from!r})")
        if day >= kit_rules_from:
            if RULES_KIT is None:
                raise ValueError(f"day {day} is under decision 24 (the kit rule set) but no kit floor is fixed: "
                                 f"set {KIT_MIN_ENV}, or read the floor from the day's published inputs")
            return RULES_KIT
    if more_requests_from is not None and day >= more_requests_from:
        return RULES_MORE_REQUESTS
    if doubled_from is not None and day >= doubled_from:
        return RULES_DOUBLED
    if unwound_from is None:
        return RULES_17
    return RULES if day >= unwound_from else RULES_17


def with_jury_rules(day: int, rules: dict, kit_rules_from, jury_rules_from) -> dict:
    """The set of day `day` once the jury set is read: RULES_JURY from `jury_rules_from` on, else `rules`,
    the answer of `rules_for_day` for the same day. `jury_rules_from` is the set's first day as the service fixed it, or
    None when it applies to no day. Never before the first day of decision 24, nor without it: the sets take effect in
    the order of RULE_HISTORY. A day under the set with no fault commitment fixed (RULES_JURY is None) is refused: the
    set does not exist without them."""
    if jury_rules_from is None:
        return rules
    if type(jury_rules_from) is not int or jury_rules_from < 0:
        raise ValueError(f"the first day of the jury set {jury_rules_from!r} is neither a day nor None")
    if kit_rules_from is None or jury_rules_from < kit_rules_from:
        raise ValueError(f"the jury set from day {jury_rules_from} would take effect before decision 24 "
                         f"(from day {kit_rules_from!r})")
    if day < jury_rules_from:
        return rules
    if RULES_JURY is None:
        raise ValueError(f"day {day} is under the jury set but no fault commitment is fixed: set "
                         f"{FAULT_PERIODS_ENV} and {FAULT_PAYER_ENV}, or read them from the day's published inputs")
    return RULES_JURY


def rules_for_day_with_jury(day: int, unwound_from, doubled_from, more_requests_from, kit_rules_from,
                            jury_rules_from) -> dict:
    """`rules_for_day`, then `with_jury_rules`: the set of `day` from every first day the service fixed, all required."""
    return with_jury_rules(day, rules_for_day(day, unwound_from, doubled_from, more_requests_from, kit_rules_from),
                           kit_rules_from, jury_rules_from)


def pays_jury_rule(rules: dict) -> bool:
    """Whether `rules` carry the jury rule. Its three keys travel together, on top of decision 24's: a set
    that names one without the others, or a juror rule this code does not know, raises."""
    keys = ("juror_rule", "fault_periods", "fault_payer")
    present = [k for k in keys if k in rules]
    if not present:
        return False
    if len(present) != len(keys) or not pays_judge_presence(rules):
        raise ValueError(f"a rule set with {present} and not the rest of the jury set's keys")
    if rules["juror_rule"] != JUROR_RULE:
        raise ValueError(f"unknown juror rule {rules['juror_rule']!r}")
    return True


def fault_period_of(rules: dict, day: int):
    """The fault period of the jury set `rules` that holds `day`, as {"from_day", "to_day", "commitment"}, or None
    when no period holds it: a day the set declares without controlled-fault requests."""
    for p in rules["fault_periods"]:
        if p["from_day"] <= day <= p["to_day"]:
            return p
    return None


def started_fault_periods(rules: dict, day: int) -> list:
    """The fault periods of the jury set `rules` whose first day is `day` or earlier, in order: the periods whose
    identities a ranking of `day` leaves out of the pay, and whose reveals it needs for that."""
    return [p for p in rules["fault_periods"] if p["from_day"] <= day]


def fault_memo(commitment: str) -> str:
    """The memo of the transfer that carries a commitment, in the payout memos' form."""
    if not isinstance(commitment, str) or not _HEX64.match(commitment):
        raise ValueError(f"commitment {commitment!r} is not a sha256 in lowercase hex")
    return FAULT_MEMO_PREFIX + commitment


def canonical_fault_document(doc: dict) -> bytes:
    """The bytes whose sha256 is a commitment: sorted keys, no space in the separators, UTF-8, no final newline."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fault_draw(seed: bytes, job_id: str, fault_bps: int, classes) -> str | None:
    """The class drawn for a programme request served by a controlled-fault identity, or None when it is served
    honestly: d = sha256(seed || job_id as UTF-8); faulted when the first 8 bytes of d, big-endian, modulo 10 000 are
    below `fault_bps`; the class is `classes`[the next 8 bytes, big-endian, modulo the number of classes]. The
    launcher draws with this, and the ranking checks the reveal against it both ways: each listed request must be
    drawn in its class, and each drawn request must be listed, faulted or served honestly
    (`final_season_facts.drawn_not_listed`). A fault can then be neither added nor withdrawn once the votes are
    known; a drawn request listed as served honestly carries its answer, which anyone compares to its anchor."""
    d = hashlib.sha256(bytes(seed) + job_id.encode("utf-8")).digest()
    if int.from_bytes(d[0:8], "big") % 10_000 >= fault_bps:
        return None
    return classes[int.from_bytes(d[8:16], "big") % len(classes)]


def pays_judge_presence(rules: dict) -> bool:
    """Whether `rules` carry the judge presence reward, the kit floor and the probation (decision 24). The three
    travel together: a set that names one of the three keys without the others raises, since the gross and the
    payable of a row would then follow a set this code does not know."""
    keys = ("judge_active_per_day", "probation_first_day_bps", "kit_min")
    present = [k for k in keys if k in rules]
    if not present:
        return False
    if len(present) != len(keys):
        raise ValueError(f"a rule set with {present} and not the rest of decision 24's keys")
    return True


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
    passed the end: it cuts the day that holds it, and a day that starts after it is not a season day —
    refused, never returned as an empty or inverted window, which every search would read as "nothing
    happened that day". None means the end is not known yet: the full 17 280-block day, which only a
    caller that has checked the day lies wholly before the end (`final_season_rank.season_window`) may use."""
    r = RULES if rules is None else rules
    first = start_height + day * r["day_blocks"]
    last = first + r["day_blocks"] - 1
    if end_height is not None:
        if first > end_height:
            raise ValueError(f"day {day} starts at block {first}, after the season's last block {end_height}")
        last = min(last, end_height)
    return first, last
