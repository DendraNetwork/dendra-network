"""Final Testnet Season calculation (ADR-047): the PURE core. No network, no clock, no file: facts in,
ranking out.

Anyone can rerun it. The inputs are the facts of one day, gathered by `final_season_facts` from the chain and
from the programme's published evidence; this module only applies the published formula to them, in
integer udndr, deterministically. The same facts always give the same ranking, byte for byte.

THE FORMULA, PER IDENTITY AND PER DAY
    presence = availability windows the identity proved ON CHAIN that day, at most 50
    gross    = 0.05 x verified_requests + 0.008 x verdicts
               + 0.002 x presence, ONLY if verified_requests >= 1
               in integer udndr
    payable  = gross, or 0 when no payout address is known (a row the weekly payment could not pay
               would stop the whole week)
    paid     = payable, unless the day's total payable exceeds min(50 DNDR, what remains of the
               1 500 DNDR season budget): then every payable of that day is reduced by the same ratio
               (floor, in udndr)
    There is no cap per identity: the work an identity gets is its share of the chain's work draw over a
    fixed daily volume, each identity's stake weighing up to `assignment_stake_cap_multiple` x `min_stake`
    (`committee.go::capAssignmentWeights`), and a cap per identity would pay an operator to split its stake.

WHY PRESENCE NEEDS WORK. Proving an availability window takes an operator key and a VRF key, no model:
a machine without a card proves it as well as a miner. A day with at least one verified request is a day
a model answered; presence counts on those days only.

WHY THE PRO RATA COMES LAST. It is the only rule that depends on everybody else: it shares the day's
budget among the rows that can be paid, so a row without an address takes no share from the others.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from final_season_rules import RULES, fingerprint


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


@dataclass
class Row:
    miner_id: str
    payout_address: str
    presence: int
    verified_requests: int
    verdicts: int
    gross: int                       # the formula on the facts
    payable: int                     # gross, or 0 when no payout address is known
    paid: int                        # after the day's pro-rata
    reason: str = ""                 # why paid < what the facts alone would give, in words


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


def presence_paid(i: Identity, rules: dict | None = None) -> int:
    """The windows paid: the proven ones, at most `presence_max_windows`, and none on a day without verified
    work."""
    r = RULES if rules is None else rules
    if int(i.verified_requests) < 1:
        return 0
    return max(0, min(int(i.presence), r["presence_max_windows"]))


def gross_of(i: Identity, rules: dict | None = None) -> int:
    r = RULES if rules is None else rules
    return (presence_paid(i, r) * r["presence_per_window"]
            + max(0, int(i.verified_requests)) * r["work_per_request"]
            + max(0, int(i.verdicts)) * r["juror_per_verdict"])


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
        payable = g if i.payout_address else 0
        if g and not i.payout_address:
            why.append("no payout address known")
        rows.append(Row(i.miner_id, i.payout_address or "", int(i.presence), int(i.verified_requests),
                        int(i.verdicts), g, payable, 0, "; ".join(why)))

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


def to_json(d: DayResult) -> dict:
    """The published ranking of a day. `final_season_payout.load_day` reads its figures back, and refuses a day
    whose figures break the rules before paying anything."""
    return {
        "season": RULES["season"], "day": d.day, "rules_fingerprint": d.rules_fingerprint,
        "total_gross_udndr": d.total_gross, "total_payable_udndr": d.total_payable,
        "day_budget_udndr": d.day_budget, "total_paid_udndr": d.total_paid,
        "pro_rata": [d.scale_num, d.scale_den],
        "ranking": [{
            "rank": n + 1, "miner_id": x.miner_id, "payout_address": x.payout_address,
            "presence": x.presence, "verified_requests": x.verified_requests, "verdicts": x.verdicts,
            "gross_udndr": x.gross, "payable_udndr": x.payable, "paid_udndr": x.paid, "reason": x.reason,
        } for n, x in enumerate(d.rows)],
    }
