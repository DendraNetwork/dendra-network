"""Final Testnet Season calculation: the pure core. No network, no clock, no file: facts in,
ranking out.

Anyone can rerun it. The inputs are the facts of one day, gathered by `final_season_facts` from the chain and
from the programme's published evidence; this module only applies the published formula to them, in
integer udndr, deterministically. The same facts always give the same ranking, byte for byte.

The formula, per identity and per day, with the rates and caps of the day's rule set (`final_season_rules`;
the figures below are those of RULES, and RULES_DOUBLED doubles every rate and both caps)
    presence = availability windows the identity proved on chain that day, at most 50
    gross    = 0.05 x verified_requests + 0.008 x verdicts
               + 0.002 x presence, only if verified_requests >= 1
               + 0.5 x judge_active (0 or 1) under RULES_KIT (the five conditions read in
                 `final_season_facts.judge_presence`, never the verdict byte)
               x 60 percent under RULES_KIT on the identity's first full season day after its registration
                 (`probation`, `final_season_facts.first_full_day`), floor
               in integer udndr
    payable  = gross, or 0 when no payout address is known (a row the weekly payment could not pay
               would stop the whole week), or 0 under RULES_KIT when the identity's latest signed capacity
               report at or before the day's end declares a kit below the set's `kit_min`, or no signed report
               exists (`kit_allows_pay`), or 0 under RULES_KIT when the identity's
               registration could not be dated (`registration_allows_pay`)
    paid     = payable, unless the day's total payable exceeds min(50 DNDR, what remains of the
               1 500 DNDR season budget after every earlier day, whatever its set): then every payable of
               that day is reduced by the same ratio (floor, in udndr)
    There is no cap per identity: the work an identity gets is its share of the chain's work draw over a
    fixed daily volume, each identity's stake weighing up to `assignment_stake_cap_multiple` x `min_stake`
    (`committee.go::capAssignmentWeights`), and a cap per identity would pay an operator to split its stake.

Why presence needs work. Proving an availability window takes an operator key and a VRF key, no model:
a machine without a card proves it as well as a miner. A day with at least one verified request is a day
a model answered; presence counts on those days only.

Why the pro rata comes last. It is the only rule that depends on everybody else: it shares the day's
budget among the rows that can be paid, so a row without an address takes no share from the others.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from final_season_rules import RULES, fingerprint, pays_judge_presence, rule_set_of

# The reason a row carries when the kit floor refuses it for want of any signed capacity report. The catch-up
# payment (`final_season_payout.plan_amendment`) selects its rows by these exact words, so both read this name.
NO_KIT_REPORT = "no signed capacity report at or before the day's end"


@dataclass
class Identity:
    """The facts of one miner identity for one day. Every count is a measured count: absent means 0."""
    miner_id: str
    presence: int = 0                # distinct availability windows proven on chain that day
    # programme requests served, settled, past their audit, not refunded -- and, from decision 18, those whose
    # audit was unwound (no jury concluded) when answered and not graded incoherent (`final_season_facts.unwound_work`)
    verified_requests: int = 0
    verdicts: int = 0                # juror verdicts on programme jobs, consistent with the outcome
    payout_address: str = ""         # where the reward goes (the operator's choice, else its address)
    # Decision 24 only (RULES_KIT); the three stay at their zero under every earlier set, and a set without the
    # keys refuses a row that carries them (`gross_of`, `final_season_payout.check_days`).
    judge_active: int = 0            # 1 when the identity was an active judge that day (`final_season_facts.judge_presence`)
    probation: bool = False          # the identity's first full season day after its registration
    # The kit its latest signed capacity report at or before the day's end declares (`final_season_facts.kit_report`);
    # None when no signed report exists, or the report carries no number. Read, never defaulted: on the refusal
    # of a stale kit, absent is not a current kit.
    kit_version: int | None = None
    # True when the identity's registration could not be dated (`final_season_facts.REGISTRATION_SEARCH_TOO_LONG`,
    # `REGISTRATION_NO_OPERATOR`): neither before the season nor on a first full day, so the day is not paid.
    registration_not_dated: bool = False


@dataclass
class Row:
    miner_id: str
    payout_address: str
    presence: int
    verified_requests: int
    verdicts: int
    gross: int                       # the formula on the facts
    payable: int                     # gross, or 0 when no payout address is known, or the kit is below the floor
    paid: int                        # after the day's pro-rata
    reason: str = ""                 # why paid < what the facts alone would give, in words
    judge_active: int = 0            # decision 24: the four fields of Identity, published under RULES_KIT only
    probation: bool = False
    kit_version: int | None = None
    registration_not_dated: bool = False


@dataclass
class DayResult:
    day: int
    rules_fingerprint: str
    total_gross: int
    total_payable: int
    day_budget: int
    total_paid: int
    scale_num: int                   # paid = payable x scale_num // scale_den
    scale_den: int
    rows: list = field(default_factory=list)
    # The kit floor the day was ranked under (decision 24), as a published ranking carries it
    # (`inputs.kit_floor.kit_min`, read back by `final_season_payout.load_day_end`); None for every earlier set.
    kit_min: int | None = None


def presence_paid(i: Identity, rules: dict | None = None) -> int:
    """The windows paid: the proven ones, at most `presence_max_windows`, and none on a day without verified
    work."""
    r = RULES if rules is None else rules
    if int(i.verified_requests) < 1:
        return 0
    return max(0, min(int(i.presence), r["presence_max_windows"]))


def gross_of(i: Identity, rules: dict | None = None) -> int:
    """The formula on the facts of `i`. Under RULES_KIT (decision 24) the judge presence reward is added and the
    probation share applied last, on the whole gross (the work plan's Q7: 60 percent of the season pay). A row
    that carries a judge day or a probation under a set without those keys is refused: it was computed under
    rules the set does not have."""
    r = RULES if rules is None else rules
    judge_active = int(i.judge_active)
    if judge_active not in (0, 1):
        raise ValueError(f"{i.miner_id}: judge_active {i.judge_active!r} is neither 0 nor 1")
    # `is bool`, not `in (True, False)`: 1 == True in Python, and a row published with a 1 would pass as a true.
    if type(i.probation) is not bool or type(i.registration_not_dated) is not bool:
        raise ValueError(f"{i.miner_id}: probation {i.probation!r} or registration_not_dated "
                         f"{i.registration_not_dated!r} is neither true nor false")
    g = (presence_paid(i, r) * r["presence_per_window"]
         + max(0, int(i.verified_requests)) * r["work_per_request"]
         + max(0, int(i.verdicts)) * r["juror_per_verdict"])
    if pays_judge_presence(r):
        g += judge_active * r["judge_active_per_day"]
        if i.probation:
            g = g * r["probation_first_day_bps"] // 10_000
    elif judge_active or i.probation or i.registration_not_dated:
        raise ValueError(f"{i.miner_id}: a judge day, a probation or an undated registration under a rule set that "
                         f"has none of them")
    return g


def kit_allows_pay(i: Identity, rules: dict | None = None) -> bool:
    """Whether the identity's kit lets the day be paid. Under a set without a floor, always.
    Under RULES_KIT, only a kit number read from a signed report at or before the day's end, at or above the
    floor: None (no signed report, or a report with no number) is not a current kit, and a number that is not a
    whole number is refused rather than compared."""
    r = RULES if rules is None else rules
    if not pays_judge_presence(r):
        return True
    kv = i.kit_version
    if kv is None:
        return False
    if type(kv) is not int or kv < 0:
        raise ValueError(f"{i.miner_id}: kit_version {kv!r} is neither a whole number nor None")
    return kv >= r["kit_min"]


def registration_allows_pay(i: Identity, rules: dict | None = None) -> bool:
    """Whether the identity's registration lets the day be paid (decision 24, the probation's reading). Under
    RULES_KIT an identity whose registration could not be dated (`registration_not_dated`) is paid nothing for the
    day: its first full day is unknown, and unknown is neither before the season nor today. Under a set without
    the probation, always; a row that carries the flag under such a set is refused by `gross_of`."""
    r = RULES if rules is None else rules
    return not (pays_judge_presence(r) and i.registration_not_dated is True)


def compute_day(day: int, identities: list, paid_before: int, rules: dict | None = None) -> DayResult:
    """Ranking of one day. `paid_before` = what the season has already paid on the days before this one.
    `rules`: the set in force that day (`final_season_rules.rules_for_day`); the ranking carries its fingerprint."""
    r = RULES if rules is None else rules
    seen = set()
    rows = []
    for i in identities:
        if i.miner_id in seen:
            raise ValueError(f"identity {i.miner_id!r} appears twice in the facts of day {day}")
        seen.add(i.miner_id)
        g = gross_of(i, r)
        why = []
        if int(i.presence) > 0 and int(i.verified_requests) < 1:
            why.append("presence not paid: no verified request counted that day")
        if pays_judge_presence(r) and i.probation and g:
            why.append(f"probation: first full day after registration, {r['probation_first_day_bps'] // 100} percent "
                       f"of the gross")
        kit_ok, reg_ok = kit_allows_pay(i, r), registration_allows_pay(i, r)
        payable = g if (i.payout_address and kit_ok and reg_ok) else 0
        if g and not i.payout_address:
            why.append("no payout address known")
        if g and not kit_ok:
            why.append(NO_KIT_REPORT if i.kit_version is None
                       else f"kit {i.kit_version} below the floor {r['kit_min']}: not paid")
        if g and not reg_ok:
            why.append("registration not datable: not paid")
        rows.append(Row(i.miner_id, i.payout_address or "", int(i.presence), int(i.verified_requests),
                        int(i.verdicts), g, payable, 0, "; ".join(why), int(i.judge_active), bool(i.probation),
                        i.kit_version, bool(i.registration_not_dated)))

    total_payable = sum(x.payable for x in rows)
    budget = max(0, min(r["cap_programme_day"], r["cap_season"] - int(paid_before)))
    if total_payable > budget:
        num, den = budget, total_payable
    else:
        num, den = 1, 1
    for row in rows:
        row.paid = row.payable * num // den
        if row.payable and row.paid < row.payable:
            row.reason = (row.reason + "; " if row.reason else "") + "daily budget shared pro rata"
    rows.sort(key=lambda x: (-x.paid, x.miner_id))
    return DayResult(day, fingerprint(r), sum(x.gross for x in rows), total_payable, budget,
                     sum(x.paid for x in rows), num, den, rows)


def week_totals(days: list) -> dict:
    """Sum of what each payout address earned over the given days (the weekly payment)."""
    out = {}
    for d in days:
        for row in d.rows:
            if row.paid > 0:
                if not row.payout_address:
                    raise ValueError(f"day {d.day}: {row.miner_id} is paid {row.paid} with no payout address")
                out[row.payout_address] = out.get(row.payout_address, 0) + row.paid
    return dict(sorted(out.items()))


def publishes_kit_fields(rules_fingerprint: str) -> bool:
    """Whether a ranking under `rules_fingerprint` carries the four row fields of decision 24. Read from the
    set the fingerprint names: a day under an earlier set keeps the bytes it was published with, whatever the
    rows hold, and a fingerprint of no known set publishes the earlier shape."""
    rules = rule_set_of(rules_fingerprint)[1]
    return rules is not None and pays_judge_presence(rules)


def to_json(d: DayResult) -> dict:
    """The published ranking of a day. `final_season_payout.load_day` reads its figures back, and refuses a day
    whose figures break the rules before paying anything. Under RULES_KIT each row also carries `judge_active`,
    `probation`, `kit_version` and `registration_not_dated`, the inputs of its gross and payable that the counts
    alone do not give."""
    kit = publishes_kit_fields(d.rules_fingerprint)
    rows = []
    for n, x in enumerate(d.rows):
        row = {
            "rank": n + 1, "miner_id": x.miner_id, "payout_address": x.payout_address,
            "presence": x.presence, "verified_requests": x.verified_requests, "verdicts": x.verdicts,
            "gross_udndr": x.gross, "payable_udndr": x.payable, "paid_udndr": x.paid, "reason": x.reason,
        }
        if kit:
            row.update(judge_active=x.judge_active, probation=x.probation, kit_version=x.kit_version,
                       registration_not_dated=x.registration_not_dated)
        rows.append(row)
    return {
        "season": RULES["season"], "day": d.day, "rules_fingerprint": d.rules_fingerprint,
        "total_gross_udndr": d.total_gross, "total_payable_udndr": d.total_payable,
        "day_budget_udndr": d.day_budget, "total_paid_udndr": d.total_paid,
        "pro_rata": [d.scale_num, d.scale_den],
        "ranking": rows,
    }
