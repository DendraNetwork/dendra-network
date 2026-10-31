#!/usr/bin/env python3
"""final_season_payout.py — the Final Testnet Season's weekly payment, from the payout account.

    python3 final_season_payout.py --week 0 --rankings <dir or URL> --doubled-from-day 4         # plan only
    python3 final_season_payout.py --week 0 --rankings <dir or URL> --doubled-from-day 4 --yes   # sign and broadcast
    python3 final_season_payout.py --due --rankings <dir or URL> --doubled-from-day 4 [--yes]   # every week due
    python3 final_season_payout.py --season-list <file> --payer <address> --rankings <dir or URL> --doubled-from-day 4
    python3 final_season_payout.py --archive-rankings <empty dir> --rankings <dir or URL>
    python3 final_season_payout.py --plan-amendment N --days 16,17 --rankings <dir or URL> --evidence <dir or URL>
        --relay <URL> --decision TEXT --reason TEXT --out FILE --doubled-from-day 4                 # read only
    python3 final_season_payout.py --amendment N --rankings <dir or URL> --doubled-from-day 4 [--yes]

Week k pays the days 7k .. 7k+6, the last week stopping at the season's last day (the ranking that carries
`inputs.season_end_height`: the season ends at a fixed instant, so how many days it has is read, never set).
Each day must already be ranked: a week with a missing day is refused, never paid in part, because a later "top-up" would be a
second payment for the same week that nothing distinguishes from a double payment.

The chain is the ledger, part by part
The week is split into deterministic parts of up to `BATCH` transfers. Every transaction carries the memo
`dendra-final-season-week-<k>-<plan>-part-<i>`, where <plan> is a digest of the week's payable list. Before
anything is signed, the chain is searched for the payout account's transactions carrying that week:
  - a part already on chain is never sent again;
  - parts that are missing are sent, so a run interrupted halfway is finished by running it again;
  - a part on chain under a different plan digest stops everything: the rankings changed after a partial
    payment, and only a person can decide what is owed.
A local file could be lost or copied; the chain is the record both the payer and the paid can read. And
the node it is read from must keep the whole history (`require_index_from`): a node bootstrapped by state
sync answers "no earlier payment" for weeks it simply never saw.

The weeks that are due (--due)
The chain host's cron runs it (`dendra/onchain-staging/dendra_season_payout_cron.sh`). It walks the weeks from 0 on:
each week is planned as --week plans it (`plan_days`) and, with --yes, paid as --week pays it (`send_parts`), and a
week already paid in full is skipped. The walk ends at the first week with a day not published yet (`NotRankedYet`,
the normal wait) and at the first week after the season's last day (`PastSeasonEnd`). Anything else stops it, and no
later week is paid: a day that cannot be read (`DayUnreadable`: an unreachable service is not a day still to come),
a day not published in a week of which a part is already on chain, or in an earlier week (the rankings the payment
was planned from are gone), a day not published once the chain has passed the height where even the day after it
is final (`late_from`: a service that has stopped ranking is not a day still to come either), rankings out of the
rules (`PlanRefused`), parts on chain under another plan, a chain search or a part that fails. Whether day 0 is late
is not read: no earlier ranking says where it ends, and the walk prints that it does not know.
The key and the node's history are read once, before the first week (`_payer`), with or without --yes; without it
nothing is signed and the weeks that would be paid are printed. One line per week, then a summary line.
Exit status: 0 when every payable week is paid or was already paid; 2 when nothing was read on chain (no chain id to
sign with, the key not found, a node without the whole history), as --week answers them; 3 when a week is refused,
fails, or waits for a day that is late.
The outcomes are told apart by the exception's class, never by its words. Each class is a SystemExit carrying the
words --week has always printed, so --week stops on them as before, with the same message and exit status 1.

An unpayable address is set aside, not sent
One module account in a batch fails the whole transaction in the block (see `final_season_address`). Such
addresses are listed, with their amounts, and left out of every part; nothing else is held up by them.

The rankings are checked against the rules before anything is planned
They come from the internet-facing service, and this script holds the key that pays. Recomputing a day
needs the chain's full index and the evidence the same service serves, so it is not redone here; what is
checked is everything the published figures must satisfy given the facts each row publishes
(`check_days`): the rules fingerprint, each row's gross as the formula on its own counts, its payable (the
gross, or 0 without an address), 0 <= paid <= payable, the totals, the day budget derived from what the
earlier days paid, the pro-rata, and the season cap. One day out of line refuses the whole week. A
compromised service can still publish false counts and misdirect what the day budget allows — these
checks bound the loss, they do not prove the facts — but it can no longer make this script pay past the
day's budget or past what the formula gives on the counts it published.

The first day of the doubled rates is the payer's, not the service's
`--doubled-from-day` names the day decision 22 fixed (or `none`), and it is required: every day from it
on must be ranked under `final_season_rules.RULES_DOUBLED`, and no day before it (`check_doubled_from`). The
service publishes the day it fixed, but a compromised service could move it to pay twice the rates on days the
decision does not cover; the figures alone cannot tell, since each set is checked against its own formula.

The first day of decision 23 needs no argument
`final_season_rules.RULES_MORE_REQUESTS` is RULES_DOUBLED with another `requests_per_day`, and no figure this
script checks or pays reads that value: the gross is `final_season_calc.gross_of` on the row's counts (presence,
work and juror rates), the budget is `cap_programme_day` and `cap_season` (`check_days`). So the same counts pay
the same under either set, and a service that moved decision 23's first day could not make this script pay one
udndr more. What the payer still decides holds for it: a day under RULES_MORE_REQUESTS carries the doubled rates
and counts as a doubled day (`check_doubled_from`), and a day under RULES_DOUBLED after one under
RULES_MORE_REQUESTS is refused like any return to an earlier set (`check_days`). The command stays
`--week N --doubled-from-day 4` for every week, those after decision 23 included.

The set of decision 24 needs no argument either
`final_season_rules.RULES_KIT` carries the doubled rates too, so its days count as doubled days
(`check_doubled_from`), and a week may mix days under RULES_MORE_REQUESTS and days under it; a day under an
earlier set after one under it is refused like every return (`check_days`). What it adds, the ranking publishes
per row: `judge_active` (0 or 1), `probation`, `kit_version` and `registration_not_dated`, the inputs of the row's
gross and payable that the counts alone do not give, and the set's floor under `inputs.kit_floor.kit_min`. The set
does not exist without that floor (`final_season_rules.kit_rules`): this script takes it from the first day
published under the set, checks that the set it builds from it has the day's fingerprint, and holds every later
day to the same floor. A row that carries a judge day, a probation, a kit or an undated registration under an
earlier set is refused, like a fingerprint of no published set. The command stays `--week N --doubled-from-day 4`.

The jury set needs no argument either
`final_season_rules.RULES_JURY` is RULES_KIT with the juror rule, the fault periods and the season payer: its days count
as doubled days, and its rows are checked by the same formula (the controlled-fault identities are not rows: the
ranking removed them before the calculation). The set does not exist without its periods and its payer: this script
takes them, with the floor, from the first day published under the set (`inputs.jury`), and holds every later day to
them through the fingerprint (`final_season_rules.jury_set_named`).

The season's list of payments
`--season-list FILE` writes, once the season's last day is ranked, the list address -> paid_udndr of the whole
season: what the weekly payments sent to each address. It is built from the rankings, week by week as `plan_days`
plans them, and checked against the payout account's transfers on chain (`payout_transfers`): every part of every
week on chain under its week's plan, none twice, each part sending exactly its planned transfers (`season_list`).
Anything else refuses the list. The sum per address, and so the sum of the list, equals the sum sent because each
part matches its plan. An amount ranked to an unpayable address was never sent: it is listed separately under
`set_aside_udndr`. The file is the same bytes on every run (sorted keys, no time), with the SHA-256 of each ranking
it was read from; its own SHA-256 is printed and written next to it (`FILE.sha256`). A file already there with
other bytes is refused and left in place: the rankings or the payments changed, and a person decides.
`--archive-rankings DIR` copies every published ranking, byte for byte, into an empty directory with a
`SHA256SUMS` manifest (`archive_rankings`): the record to keep before any reset of the chain or the service.

Amendments: a payment decided after the rankings, for rows they did not pay
A week is never topped up. A payment decided afterwards is an amendment: one frozen file per amendment,
`final_season_amendments/amendment-NNN.json` beside this script (the services image copies the directory with it, and
the public mirror publishes it), holding its id, the decision it applies, the reason, the rows it pays
{day, miner_id, address, udndr, kit_proof: {job_id, kit_version}}, and under `set_apart` the rows it found and does
not pay (`amendment_problem`). Its digest is in every memo it pays under, so a paid file is never edited.
  --plan-amendment N (read only, never signs) writes such a file for the given days (`plan_amendment`). The rows are
  those ranked with exactly the reason `final_season_calc.NO_KIT_REPORT`, a gross above 0 and nothing paid; each needs
  a kit proof, one `work_answer` job of that identity that day in the published evidence whose deposit on the relay
  declares a kit at or above the day's floor (`_kit_proof`). A row without one is set apart, and so is a row whose
  reason names the missing report beside another one (a probation, no address): found, never paid. The evidence is
  read only once the files of days 0 to the day give the digest its ranking publishes (`_ranked_evidence`).
  --amendment N rereads the rankings and refuses a row that no longer matches them (amount, address, reason, nothing
  paid, day ranked), a day whose ranked payments plus its amendments pass `cap_programme_day`, and a season whose
  ranked payments plus every amendment pass `cap_season` (`amendment_refusal`). It refuses as well an amendment on
  chain that no frozen file names, or whose parts carry another digest than its file (`amendment_chain_refusal`): a
  file gone from the image would otherwise let its rows be frozen and paid again under another number. It then pays
  as a week is paid, part by part, under the memo `dendra-final-season-amend-<N>-<digest>-part-<i>` (`amend_memo`),
  where the digest is that of the file's content (`amendment_digest`): a part on chain is never sent again, and a part
  under another digest stops everything. Weeks and amendments each read their own memos only (`_MEMO`,
  `_AMEND_MEMO`). Run it by hand under the cron's lock, as a week by hand. Exit status: 0 paid, already paid or plan
  only; 2 nothing read on chain; 3 refused or failed.
  A week (--week, --due) is refused when a frozen amendment pays a row of one of its days that the rankings no longer
  give as that amendment's file says (`amended_rows_refusal`): a ranking that now pays such a row would pay it twice.
  The season's list adds every frozen amendment (`season_list`): each one on chain under its digest exactly, its
  amounts added per address, the season cap checked on the rankings plus the amendments. An amendment paid on chain
  that no file names refuses the list.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import final_season_chain as C  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_address import payable_address  # noqa: E402
from final_season_calc import (NO_KIT_REPORT, DayResult, Identity, Row, gross_of, kit_allows_pay,  # noqa: E402
                               registration_allows_pay, week_totals)
from final_season_rules import (RULES_DOUBLED, RULES_MORE_REQUESTS, fingerprint, pays_judge_presence,  # noqa: E402
                                rule_set_of)
from modea.relay_write import declared_kit_version  # noqa: E402

BATCH = 100
GAS_PER_SEND = 60_000
_MEMO = re.compile(r"^dendra-final-season-week-(\d+)-([0-9a-f]{12})-part-(\d+)$")
# The amendments (see the module's header): the directory the services image copies with this script, one file each.
AMENDMENTS_DIR = os.path.join(HERE, "final_season_amendments")
AMENDMENT_SCHEMA = 1
_AMEND_FILE = re.compile(r"^amendment-([0-9]{3})[.]json$")
_AMEND_MEMO = re.compile(r"^dendra-final-season-amend-([0-9]+)-([0-9a-f]{12})-part-([0-9]+)$")
# A job or miner id that goes into a relay path: no slash, no dot-dot segment, nothing to escape.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# Why a week cannot be planned, as a class: --due decides on the class (wait, stop, refuse), never on the words.
# Each is a SystemExit holding the message --week has always printed, so a caller of --week sees the same message
# and the same exit status (1) as before these classes existed.
class NotRankedYet(SystemExit):
    """A day the week needs is not published yet (HTTP 404, or no such file): the week is not payable yet."""

    def __init__(self, message: str, day: int):
        super().__init__(message)
        self.day = day


class DayUnreadable(SystemExit):
    """A day the week needs could not be read: the service did not answer, or its ranking lacks a field it must
    carry. Not a wait: a service that cannot be read is not a day still to come."""

    def __init__(self, message: str, day: int):
        super().__init__(message)
        self.day = day


class PastSeasonEnd(SystemExit):
    """The week starts after the season's last day: no week from it on has anything to pay."""

    def __init__(self, message: str, last_day: int):
        super().__init__(message)
        self.last_day = last_day


class PlanRefused(SystemExit):
    """The rankings the week reads break the rules (`check_days`, `check_doubled_from`, `week_plan`): refused."""


def plan_digest(payable: dict) -> str:
    return hashlib.sha256(json.dumps(sorted(payable.items()), separators=(",", ":")).encode()).hexdigest()[:12]


def memo(week: int, digest: str, part: int) -> str:
    return f"dendra-final-season-week-{week}-{digest}-part-{part}"


def week_days(week: int, last_day: int | None = None) -> list:
    """The days week `week` pays: 7k .. 7k+6, cut after the season's last day once it is known (the
    ranking of that day carries `inputs.season_end_height`). A week outside the season is refused, never
    returned empty: an empty plan reads as "already paid in full" and would exit 0 having paid nothing."""
    if type(week) is not int or week < 0:
        raise SystemExit(f"week {week!r} is not a season week (weeks start at 0)")
    days = list(range(7 * week, 7 * week + 7))
    if last_day is not None:
        days = [d for d in days if d <= last_day]
        if not days:
            raise PastSeasonEnd(f"week {week} starts after the season's last day ({last_day}): there is nothing to pay",
                                last_day)
    return days


def _read_bytes(src: str, name: str) -> bytes:
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(f"{src.rstrip('/')}/{name}", timeout=30) as r:
            return r.read()
    with open(os.path.join(src, name), "rb") as f:
        return f.read()


def _read(src: str, name: str) -> dict:
    return json.loads(_read_bytes(src, name))


def ranking_bytes(src: str, day: int):
    """The published bytes of day `day`'s ranking, or None when the source says it does not exist (an HTTP 404,
    or no such file). Any other failure raises: an unreachable service is not a season without that day."""
    try:
        return _read_bytes(src, f"day-{day:03d}.json")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except FileNotFoundError:
        return None


def load_day_end(src: str, day: int) -> tuple[DayResult, int | None]:
    return parse_day(_read(src, f"day-{day:03d}.json"), day)


def parse_day(d: dict, day: int) -> tuple[DayResult, int | None]:
    """A published ranking (`final_season_calc.to_json`) read back, with the season's last block when this
    day holds it (`inputs.season_end_height`, `final_season_rank.rank_day`). A missing field raises: never
    defaulted — a ranking that does not say whether it is the last day cannot be read as "not the last"."""
    # The four fields of decision 24 are read when the row carries them; absent, they are the zero of their
    # family (no judge day, no probation, no kit read, a dated registration), which `check_days` accepts under an
    # earlier set only. A day that publishes a kit floor is ranked under the set that writes all four
    # (`final_season_calc.to_json`), and they decide its pay: there a missing field raises. A missing kit_version
    # would read as "no capacity report", the very rows an amendment pays.
    kit_day = isinstance(d["inputs"].get("kit_floor"), dict)
    rows = [Row(x["miner_id"], x["payout_address"], x["presence"], x["verified_requests"],
                x["verdicts"], x["gross_udndr"], x["payable_udndr"], x["paid_udndr"], x["reason"],
                *((x["judge_active"], x["probation"], x["kit_version"], x["registration_not_dated"]) if kit_day
                  else (x.get("judge_active", 0), x.get("probation", False), x.get("kit_version"),
                        x.get("registration_not_dated", False))))
            for x in d["ranking"]]
    end, last = d["inputs"]["season_end_height"], d["inputs"]["last_height"]
    # Bounded from below and tied to the day: the ranking that holds the end names it as its own last
    # counted block (`final_season_rank.rank_day`). A 0, or a height that is not this day's last block,
    # would end the season early and refuse every later week.
    if end is not None and (type(end) is not int or type(last) is not int or not 0 < end == last):
        raise ValueError(f"day {day}: season_end_height {end!r} is not this day's last block ({last!r})")
    # The floor of decision 24, when the day publishes one (`inputs.kit_floor.kit_min`): what names the day's set.
    kit_min = (d["inputs"].get("kit_floor") or {}).get("kit_min") if isinstance(d["inputs"].get("kit_floor"), dict) else None
    jury = d["inputs"].get("jury") if isinstance(d["inputs"].get("jury"), dict) else None
    return PublishedDay(d["day"], d["rules_fingerprint"], d["total_gross_udndr"], d["total_payable_udndr"],
                        d["day_budget_udndr"], d["total_paid_udndr"], d["pro_rata"][0], d["pro_rata"][1], rows,
                        kit_min, None if jury is None else {"kit_min": kit_min,
                                                            "fault_periods": jury.get("fault_periods"),
                                                            "fault_payer": jury.get("fault_payer")},
                        d["inputs"].get("evidence_sha256")), end


@dataclass
class PublishedDay(DayResult):
    """A published day as `parse_day` reads it back: a DayResult, and the keys of the jury set it publishes under
    `inputs.jury` with its kit floor, None on a day under an earlier set. `evidence_sha256` is the digest of the
    records the day was ranked from, as published; only an amendment's plan reads it, and refuses it absent."""
    jury: dict | None = None
    evidence_sha256: str | None = None


def load_day(src: str, day: int) -> DayResult:
    return load_day_end(src, day)[0]


def _whole(v) -> bool:
    return type(v) is int          # not a float, a string or a bool: a figure that pays is an integer


def check_days(days: list) -> str:
    """'' when the published days 0..N (in order) satisfy the rules, else the first violation, in words.
    Everything here follows from the figures themselves and the rules: no fact is trusted, none is needed.

    Each day is checked, and paid, under the rules it was ranked with: its fingerprint names one of the season's
    published rule sets (`final_season_rules.RULE_HISTORY`), and the figures are checked against that set. A
    fingerprint of no published set is refused, never checked against the current rules in its place; and a day
    ranked under an earlier set than a day before it is refused too: an amendment takes effect from a day on,
    it is never undone for one day."""
    paid_before, prev = 0, 0
    for n, d in enumerate(days):
        at = f"day {n}"
        if d.day != n:
            return f"{at}: the file says it is day {d.day!r}"
        pos, r = _set_of_day(d)
        if r is None:
            known = ", ".join(fingerprint(x)[:12] for x in R.RULE_HISTORY)
            return f"{at}: ranked under rules {str(d.rules_fingerprint)[:12]}, which are none of the season's ({known})"
        if pos < prev:
            return (f"{at}: ranked under rules {str(d.rules_fingerprint)[:12]}, earlier than the rules of a day "
                    f"before it")
        prev = pos
        kit = pays_judge_presence(r)
        # The floor a kit day publishes is the one its set carries, or the day describes itself falsely.
        if kit and d.kit_min != r["kit_min"]:
            return f"{at}: publishes kit floor {d.kit_min!r}, but its rules carry {r['kit_min']}"
        if not kit and d.kit_min is not None:
            return f"{at}: publishes a kit floor under a set that has none"
        figures = (d.total_gross, d.total_payable, d.day_budget, d.total_paid, d.scale_num, d.scale_den)
        if not all(_whole(x) for x in figures):
            return f"{at}: a total is not an integer"
        seen = set()
        for row in d.rows:
            if not (isinstance(row.miner_id, str) and isinstance(row.payout_address, str)):
                return f"{at}: a row whose identity or address is not text"
            if row.miner_id in seen:
                return f"{at}: {row.miner_id!r} is ranked twice"
            seen.add(row.miner_id)
            counts = (row.presence, row.verified_requests, row.verdicts)
            if not all(_whole(x) and x >= 0 for x in counts):
                return f"{at}: {row.miner_id!r} has a count that is not a whole number >= 0"
            if not all(_whole(x) for x in (row.gross, row.payable, row.paid)):
                return f"{at}: {row.miner_id!r} has a non-integer amount"
            # Decision 24's fields: a whole 0 or 1, two bools (`is bool`: 1 == True in Python, and a 1 published
            # for a probation would pass as a true), a whole number or None; and only under its set. Under an
            # earlier set a row that carries any of them was not computed with that set's formula.
            if not (_whole(row.judge_active) and row.judge_active in (0, 1) and type(row.probation) is bool
                    and type(row.registration_not_dated) is bool
                    and (row.kit_version is None or (_whole(row.kit_version) and row.kit_version >= 0))):
                return (f"{at}: {row.miner_id!r} has a judge day, a probation, a kit or an undated registration that "
                        f"is not well formed")
            if not kit and (row.judge_active or row.probation or row.kit_version is not None
                            or row.registration_not_dated):
                return (f"{at}: {row.miner_id!r} carries a judge day, a probation, a kit or an undated registration "
                        f"under a set that has none")
            facts = Identity(row.miner_id, *counts, row.payout_address, judge_active=row.judge_active,
                             probation=row.probation, kit_version=row.kit_version,
                             registration_not_dated=row.registration_not_dated)
            gross = gross_of(facts, r)
            if row.gross != gross:
                return f"{at}: {row.miner_id!r} gross {row.gross}, but the formula on its counts gives {gross}"
            payable = gross if (row.payout_address and kit_allows_pay(facts, r)
                                and registration_allows_pay(facts, r)) else 0
            if row.payable != payable:
                return f"{at}: {row.miner_id!r} payable {row.payable}, it is {payable}"
            if not 0 <= row.paid <= row.payable:
                return f"{at}: {row.miner_id!r} paid {row.paid}, outside 0 <= paid <= payable {row.payable}"
        if (sum(x.paid for x in d.rows) != d.total_paid or sum(x.payable for x in d.rows) != d.total_payable
                or sum(x.gross for x in d.rows) != d.total_gross):
            return f"{at}: the rows do not add up to the published totals"
        budget = max(0, min(r["cap_programme_day"], r["cap_season"] - paid_before))
        if d.day_budget != budget:
            return f"{at}: budget {d.day_budget}, but the earlier days paid {paid_before}: it is {budget}"
        if not d.total_paid <= d.day_budget <= r["cap_programme_day"]:
            return f"{at}: paid {d.total_paid} against a budget of {d.day_budget}"
        scale = (budget, d.total_payable) if d.total_payable > budget else (1, 1)
        if (d.scale_num, d.scale_den) != scale or any(x.paid != x.payable * scale[0] // scale[1] for x in d.rows):
            return f"{at}: the pro-rata is not the one the budget gives"
        paid_before += d.total_paid
        if paid_before > r["cap_season"]:
            return f"{at}: the season has paid {paid_before}, over its cap of {r['cap_season']}"
    return ""


def _set_of_day(d: DayResult) -> tuple:
    """(position, rules) of the set a published day names (`final_season_rules.rule_set_of`). A fingerprint this
    process knows no set for, on a day that publishes a kit floor, is tried as the set of decision 24 with that
    floor (`final_season_rules.set_kit_min`): the floor is fixed for the process by the first such day, so every
    later day is held to it, and a day naming another floor stays unknown. (None, None) otherwise."""
    pos, r = rule_set_of(d.rules_fingerprint)
    if r is None and type(d.kit_min) is int and d.kit_min >= 1 and R.KIT_MIN is None:
        if fingerprint(R.kit_rules(d.kit_min)) == d.rules_fingerprint:
            R.set_kit_min(d.kit_min)
            pos, r = rule_set_of(d.rules_fingerprint)
    if r is None and getattr(d, "jury", None) is not None and R.RULES_JURY is None:
        # A day under the jury set names the set's keys beside its floor, bound by its fingerprint.
        if R.jury_set_named(d.jury, d.rules_fingerprint) is not None:
            pos, r = rule_set_of(d.rules_fingerprint)
    return pos, r


def doubled_sets() -> tuple:
    """The sets that carry the doubled rates: RULES_DOUBLED, RULES_MORE_REQUESTS (decision 23,
    which changes only `requests_per_day`), RULES_KIT (decision 24, which adds to them) once its floor is known, and
    RULES_JURY (which adds to RULES_KIT) once its keys are known."""
    return _sets_to_the_kit() + (() if R.RULES_JURY is None else (R.RULES_JURY,))


def _sets_to_the_kit() -> tuple:
    return (RULES_DOUBLED, RULES_MORE_REQUESTS) + (() if R.RULES_KIT is None else (R.RULES_KIT,))


def check_doubled_from(days: list, doubled_from) -> str:
    """'' when the days 0..N under the doubled rates (a set of `doubled_sets`) are exactly those from `doubled_from`
    on, else the first one out of line, in words. `doubled_from` is a day, or None for no doubled day; it has no
    default, and anything else raises. The fingerprints are read as `check_days` reads them, so run that first:
    it also refuses a day under RULES_DOUBLED after a day under RULES_MORE_REQUESTS."""
    if doubled_from is not None and (type(doubled_from) is not int or doubled_from < 0):
        raise ValueError(f"the first day of the doubled rates {doubled_from!r} is neither a day nor None")
    for n, d in enumerate(days):
        rules = _set_of_day(d)[1]
        doubled = any(rules is s for s in doubled_sets())
        if doubled != (doubled_from is not None and n >= doubled_from):
            return (f"day {n}: {'ranked' if doubled else 'not ranked'} under the doubled rates, but the payer names "
                    f"{'no doubled day' if doubled_from is None else f'day {doubled_from} as the first one'}")
    return ""


def plan(week: int, src: str, doubled_from) -> tuple[dict, dict]:
    """(payable, set_aside): {address: udndr} each. Refuses a week with a day not yet ranked, and a week
    whose rankings, or those of any earlier day (which set its budget), break the rules or put the doubled
    rates on other days than from `doubled_from` on (`check_doubled_from`)."""
    payable, aside, _ = plan_days(week, src, doubled_from)
    return payable, aside


def plan_days(week: int, src: str, doubled_from) -> tuple[dict, dict, list]:
    """`plan`, with the days the week pays. The days are read in order from day 0, and the reading stops
    at the season's last day (the ranking that carries `season_end_height`): the week that holds the end
    pays up to it, and cannot be planned before it is ranked; a week after it is refused."""
    week_days(week)                       # refuses a negative or malformed week before any read
    check_doubled_from([], doubled_from)  # refuses a malformed first doubled day before any read
    loaded, last_day = [], None
    for d in range(7 * week + 7):
        try:
            day, end = load_day_end(src, d)
        except Exception as e:  # noqa: BLE001
            # The same words for both, as --week has always printed them; the class tells a day the source says
            # does not exist (the test of `ranking_bytes`) from one it failed to give.
            cls = NotRankedYet if _not_published(e) else DayUnreadable
            raise cls(f"day {d} is not ranked yet or unreadable ({type(e).__name__}): week {week} "
                      f"is not paid in part", d)
        loaded.append(day)
        if end is not None:
            last_day = d
            break
    wd = week_days(week, last_day)
    bad = check_days(loaded) or check_doubled_from(loaded, doubled_from)
    if bad:
        raise PlanRefused(f"Refused: {bad}. Week {week} is not paid; nothing is signed.")
    try:
        payable, aside = week_plan(loaded[wd[0]:wd[-1] + 1])
        bad = amended_rows_refusal(load_amendments(AMENDMENTS_DIR), loaded, wd)
    except (ValueError, OSError) as e:
        raise PlanRefused(f"Refused: {e}. Week {week} is not paid; nothing is signed.") from e
    if bad:
        raise PlanRefused(f"Refused: {bad}. Week {week} is not paid; nothing is signed.")
    return payable, aside, wd


def _not_published(e: Exception) -> bool:
    """Whether a failed read says the ranking does not exist: an HTTP 404, or no such file (`ranking_bytes`)."""
    return isinstance(e, FileNotFoundError) or (isinstance(e, urllib.error.HTTPError) and e.code == 404)


def late_from(prev: dict, day: int) -> int:
    """The chain height from which day `day`, not published yet, is late, read from the published ranking of day
    `day` - 1: the height at which the day after it is final too. The service ranks the days in order, each once the
    chain is `inputs.finality_blocks` past its last block (`final_season_server.State.day_closed`, the blocks of a day
    from `inputs.start_height` by `final_season_rules.day_bounds`), and looks every 600 s (`rank_days`). The one day of
    blocks of margin covers that pace, a ranking deferred until the day's answers are sealed, and a finality that grows
    by less than a day from one rule set to the next. A day cut by the season's end is final earlier, never later.
    Raises ValueError when the ranking does not carry both figures as whole numbers above 0: lateness is not known."""
    inputs = prev.get("inputs") if isinstance(prev, dict) else None
    start = inputs.get("start_height") if isinstance(inputs, dict) else None
    finality = inputs.get("finality_blocks") if isinstance(inputs, dict) else None
    if not (type(start) is int and type(finality) is int and start > 0 and finality > 0):
        raise ValueError(f"the ranking of day {day - 1} does not carry inputs.start_height and inputs.finality_blocks "
                         f"as whole numbers above 0 ({start!r}, {finality!r})")
    return R.day_bounds(day + 1, start)[1] + finality


def week_plan(days: list) -> tuple[dict, dict]:
    """(payable, set_aside) of the checked days of one week: what each address earned over them
    (`final_season_calc.week_totals`), split on whether the address can receive a transfer. Raises ValueError
    on a row paid with no address."""
    payable, aside = {}, {}
    for a, v in week_totals(days).items():
        (aside if payable_address(a) else payable)[a] = v
    return payable, aside


def parts(payable: dict) -> list:
    items = sorted(payable.items())
    return [items[i:i + BATCH] for i in range(0, len(items), BATCH)]


def _cli(args, node, timeout=120):
    out = subprocess.run(["dendrad", *args, "--node", node], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def paid_parts(node: str, payer: str, week: int) -> dict:
    """{part: (plan digest, txhash)} of the week's parts already on chain, from the payout account.

    This list is what keeps a part from being paid twice, so it is complete or it is not returned: a search
    answer without its own count, or a page served empty before that count is reached, raises. Read as a total
    of 0, or as the end of the search, either would return the parts of the first pages only, and a part paid
    further down the account's history would be sent again."""
    found = {}
    for t in _payer_txs(node, payer):
        m = _MEMO.match(((t.get("tx") or {}).get("body") or {}).get("memo", ""))
        if m and int(m.group(1)) == week:
            found[int(m.group(3))] = (m.group(2), t.get("txhash", ""))
    return found


def _weeks_on_chain(node: str, payer: str) -> set:
    """The weeks of which the payout account has at least one part on chain (`memo`), from the same complete search."""
    weeks = set()
    for t in _payer_txs(node, payer):
        m = _MEMO.match(((t.get("tx") or {}).get("body") or {}).get("memo", ""))
        if m:
            weeks.add(int(m.group(1)))
    return weeks


def _payer_txs(node: str, payer: str):
    """Every transaction the payout account sent that the chain executed (code 0), page after page. The search
    is complete or it raises (see `paid_parts`): it is what both the payment and the season's list rely on."""
    q = f"message.sender='{payer}'"
    page, served = 1, 0
    while True:
        d = json.loads(_cli(["query", "txs", "--query", q, "--page", str(page), "--limit", "100", "-o", "json"], node))
        txs = d.get("txs") or []
        served += len(txs)
        for t in txs:
            if int(t.get("code", 0) or 0) != 0:
                continue
            yield t
        try:
            total = int(d["total_count"])
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError(f"the payout account's search answered without a total_count ({type(e).__name__}): "
                               f"the parts already paid cannot be known, nothing is sent") from e
        if served >= total:
            return
        if not txs:
            raise RuntimeError(f"the payout account's search served an empty page after {served} of the {total} "
                               f"transactions it counts: the parts already paid cannot be known, nothing is sent")
        page += 1


def decide(payable: dict, done: dict) -> tuple[str, list]:
    """('pay', [part numbers]) / ('done', []) / ('stop', [conflicting parts]). Pure."""
    return _decide(plan_digest(payable), len(parts(payable)), done)


def _decide(digest: str, count: int, done: dict) -> tuple[str, list]:
    """`decide` for `count` parts under `digest`, a week's plan or an amendment's file. Pure."""
    conflict = sorted(p for p, (dg, _h) in done.items() if dg != digest)
    if conflict:
        return "stop", conflict
    todo = [i for i in range(count) if i not in done]
    return ("pay", todo) if todo else ("done", [])


def send_part(chunk: list, week: int, digest: str, part: int, payer_key: str, payer: str, chain_id: str,
              node: str, home: str) -> str:
    return _send_tx(chunk, memo(week, digest, part), part, payer_key, payer, chain_id, node, home)


def _send_tx(chunk: list, memo_text: str, part: int, payer_key: str, payer: str, chain_id: str, node: str,
             home: str) -> str:
    """Signs and broadcasts one part, its transfers in `chunk` order under `memo_text`, and returns its txhash once
    it is in a block. Raises RuntimeError on a part refused, failed in its block, or not seen in one."""
    tx = {"body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend", "from_address": payer,
                                 "to_address": a, "amount": [{"denom": "udndr", "amount": str(v)}]}
                                for a, v in chunk],
                   "memo": memo_text, "timeout_height": "0",
                   "extension_options": [], "non_critical_extension_options": []},
          "auth_info": {"signer_infos": [], "fee": {"amount": [], "gas_limit": str(GAS_PER_SEND * len(chunk)),
                                                    "payer": "", "granter": ""}},
          "signatures": []}
    with tempfile.TemporaryDirectory() as td:
        raw, signed = os.path.join(td, "tx.json"), os.path.join(td, "signed.json")
        with open(raw, "w", encoding="utf-8") as f:
            json.dump(tx, f)
        _cli(["tx", "sign", raw, "--from", payer_key, "--chain-id", chain_id, "--keyring-backend", "test",
              "--home", home, "--output-document", signed], node)
        res = json.loads(_cli(["tx", "broadcast", signed, "-o", "json"], node))
    if int(res.get("code", 0) or 0) != 0:
        raise RuntimeError(f"part {part} refused: code={res.get('code')} {res.get('raw_log', '')[:200]}")
    h = res.get("txhash", "")
    for _ in range(60):
        time.sleep(2)
        try:
            d = json.loads(_cli(["query", "tx", h, "-o", "json"], node))
        except RuntimeError:
            continue                       # not in a block yet
        if int(d.get("code", 0) or 0) != 0:
            raise RuntimeError(f"part {part} failed in block: {d.get('raw_log', '')[:200]}")
        return h
    raise RuntimeError(f"part {part} ({h}) not seen in a block after 120 s: run again, it will be found "
                       f"on chain if it landed and skipped")


# -- the season's list of payments and the archive of the rankings ----------------------
_SEND = "/cosmos.bank.v1beta1.MsgSend"


def _udndr_send(msg, payer: str):
    """[to, amount] of a plain transfer of udndr from `payer`, else None."""
    if not isinstance(msg, dict) or msg.get("@type") != _SEND or msg.get("from_address") != payer:
        return None
    coins, to = msg.get("amount"), msg.get("to_address")
    if not (isinstance(coins, list) and len(coins) == 1 and isinstance(coins[0], dict)
            and coins[0].get("denom") == "udndr" and isinstance(to, str) and to):
        return None
    a = coins[0].get("amount")
    if not (type(a) is str and a.isascii() and a.isdigit() and int(a) > 0):
        return None
    return [to, int(a)]


def payout_transfers(node: str, payer: str) -> list:
    """[{week, plan, part, txhash, sends: [[address, udndr], ...]}] of every executed transaction of the payout
    account whose memo names a season week (`memo`), its transfers in message order. A season transaction holding
    anything but plain udndr transfers from the payer raises: what the season sent could not be counted."""
    return [{"week": n, "plan": dg, "part": i, "txhash": h, "sends": s}
            for n, dg, i, h, s in _memo_transfers(node, payer, _MEMO, "week")]


def amendment_transfers(node: str, payer: str) -> list:
    """[{amendment, plan, part, txhash, sends}] of every executed transaction of the payout account whose memo names
    an amendment (`amend_memo`), `plan` being the file's digest; read and refused as `payout_transfers` reads weeks."""
    return [{"amendment": n, "plan": dg, "part": i, "txhash": h, "sends": s}
            for n, dg, i, h, s in _memo_transfers(node, payer, _AMEND_MEMO, "amendment")]


def _memo_transfers(node: str, payer: str, pattern, kind: str) -> list:
    """[(number, digest, part, txhash, sends)] of the payer's executed transactions whose memo `pattern` matches."""
    out = []
    for t in _payer_txs(node, payer):
        body = (t.get("tx") or {}).get("body") or {}
        m = pattern.match(body.get("memo", ""))
        if not m:
            continue
        msgs = body.get("messages")
        sends = [_udndr_send(x, payer) for x in msgs] if isinstance(msgs, list) and msgs else [None]
        if any(s is None for s in sends):
            raise RuntimeError(f"transaction {t.get('txhash', '')} carries the memo of {kind} {m.group(1)} part "
                               f"{m.group(3)} but not only udndr transfers from {payer}: what the season sent "
                               f"cannot be counted, no list is written")
        out.append((int(m.group(1)), m.group(2), int(m.group(3)), t.get("txhash", ""), sends))
    return out


def season_days(src: str) -> tuple[list, dict, int]:
    """(days, {file name: sha256 of its bytes}, season_end_height): every ranking from day 0 to the one that
    carries the season's last block. Refused while that day is not ranked: a list covers the whole season or
    nothing."""
    days, digests = [], {}
    while True:
        n = len(days)
        raw = ranking_bytes(src, n)
        if raw is None:
            raise SystemExit(f"Refused: day {n} is not ranked, and no ranking before it carries the season's last "
                             f"block: the season is not over, or its last day is not final yet. No list is written.")
        day, end = parse_day(json.loads(raw), n)
        if day.day != n:
            raise ValueError(f"day-{n:03d}.json says it is day {day.day!r}")
        days.append(day)
        digests[f"day-{n:03d}.json"] = hashlib.sha256(raw).hexdigest()
        if end is not None:
            return days, digests, end


def season_list(days: list, digests: dict, end_height: int, transfers: list, doubled_from, payer: str,
                amendments: list, amended: list) -> dict:
    """The season's list (see the module's header). Raises ValueError on the first thing out of line. Pure: the
    rankings (`season_days`), the transfers (`payout_transfers`), the frozen amendments (`load_amendments`) and
    their transfers (`amendment_transfers`) are read by the caller.

    Each week is planned from the rankings as `plan_days` plans it, and its payment is read on chain: every part
    under the week's plan digest, each part once, each part's transfers exactly its planned ones. That per-part
    check is what makes the list equal to what was sent. The two sum checks at the end restate it: no input
    reaches them while `parts` splits a week's plan without loss and `week_plan` splits each day's rows into
    payable and set aside without loss. They catch a change to either helper that loses an amount.
    Each amendment is checked the same way against its file's digest, after `amendment_refusal` on the season's
    days, and its amounts are added per address."""
    bad = check_days(days) or check_doubled_from(days, doubled_from)
    if bad:
        raise ValueError(bad)
    last = len(days) - 1
    weeks = last // 7 + 1
    on_chain = {}
    for t in transfers:
        if not 0 <= t["week"] < weeks:
            raise ValueError(f"transaction {t['txhash']} pays week {t['week']}, which is not a week of the season "
                             f"(0 to {weeks - 1})")
        prior = on_chain.setdefault(t["week"], {}).get(t["part"])
        if prior is not None:
            raise ValueError(f"week {t['week']} part {t['part']} is on chain twice ({prior['txhash']} and "
                             f"{t['txhash']}): it was paid twice")
        on_chain[t["week"]][t["part"]] = t
    listed, aside, payments = {}, {}, []
    for k in range(weeks):
        wd = week_days(k, last)
        payable, week_aside = week_plan(days[wd[0]:wd[-1] + 1])
        digest, chunks, found = plan_digest(payable), parts(payable), on_chain.get(k, {})
        other = sorted(p for p, t in found.items() if t["plan"] != digest)
        if other:
            raise ValueError(f"week {k}: part(s) {other} are on chain under another plan than the rankings give "
                             f"({digest}): the rankings changed after a payment, and a person decides what is owed")
        missing = [i for i in range(len(chunks)) if i not in found]
        extra = sorted(p for p in found if p >= len(chunks))
        if missing or extra:
            raise ValueError(f"week {k} is not paid as planned: part(s) {missing} of {len(chunks)} missing, part(s) "
                             f"{extra} beyond the plan")
        for i, chunk in enumerate(chunks):
            if found[i]["sends"] != [[a, v] for a, v in chunk]:
                raise ValueError(f"week {k} part {i} ({found[i]['txhash']}) did not send the transfers its plan lists")
        for a, v in payable.items():
            listed[a] = listed.get(a, 0) + v
        for a, v in week_aside.items():
            aside[a] = aside.get(a, 0) + v
        payments.append({"week": k, "plan": digest, "txs": [found[i]["txhash"] for i in range(len(chunks))]})
    amended_total, amendment_payments = _amendments_paid(days, amendments, amended, listed)
    sent = {}
    for t in list(transfers) + list(amended):
        for a, v in t["sends"]:
            sent[a] = sent.get(a, 0) + v
    total, total_sent = sum(listed.values()), sum(sent.values())
    if total != total_sent or sent != listed:
        diff = sorted(a for a in set(sent) | set(listed) if sent.get(a, 0) != listed.get(a, 0))
        raise ValueError(f"the list sums to {total} udndr and the payout account sent {total_sent}; first address "
                         f"that differs: {diff[0] if diff else 'none'}")
    ranked, weekly = sum(d.total_paid for d in days), total - amended_total
    if weekly + sum(aside.values()) != ranked:
        raise ValueError(f"the weeks ({weekly}) and the amounts set aside ({sum(aside.values())}) do not add up to "
                         f"what the rankings paid ({ranked})")
    return {"schema": 2, "payer": payer, "season_end_height": end_height, "last_day": last,
            "paid_udndr": dict(sorted(listed.items())), "total_paid_udndr": total,
            "set_aside_udndr": dict(sorted(aside.items())), "total_set_aside_udndr": sum(aside.values()),
            "rankings_total_paid_udndr": ranked, "rankings_sha256": dict(sorted(digests.items())),
            "payments": payments, "amendments": amendment_payments, "total_amended_udndr": amended_total}


def _amendments_paid(days: list, amendments: list, amended: list, listed: dict) -> tuple[int, list]:
    """(udndr the amendments paid, one entry per amendment) for `season_list`, adding each amendment's amounts to
    `listed` per address. Every frozen amendment must still match the season's rankings (`amendment_refusal`) and be
    on chain under its digest, each part once and exactly as its file lists it; an amendment on chain that no file
    names is refused. Raises ValueError on the first thing out of line."""
    on_chain = {}
    for t in amended:
        prior = on_chain.setdefault(t["amendment"], {}).get(t["part"])
        if prior is not None:
            raise ValueError(f"amendment {t['amendment']} part {t['part']} is on chain twice ({prior['txhash']} and "
                             f"{t['txhash']}): it was paid twice")
        on_chain[t["amendment"]][t["part"]] = t
    stray = sorted(set(on_chain) - {n for n, _doc, _dg in amendments})
    if stray:
        raise ValueError(f"amendment(s) {stray} are paid on chain, and no frozen amendment file names them")
    total, entries = 0, []
    for n, doc, dg in amendments:
        bad = amendment_refusal(n, amendments, days)
        if bad:
            raise ValueError(bad)
        payable = amendment_payable(doc)
        chunks, found = parts(payable), on_chain.get(n, {})
        other = sorted(p for p, t in found.items() if t["plan"] != dg)
        if other:
            raise ValueError(f"amendment {n}: part(s) {other} are on chain under another digest than its file gives "
                             f"({dg}): the file changed after a payment, and a person decides what is owed")
        missing = [i for i in range(len(chunks)) if i not in found]
        extra = sorted(p for p in found if p >= len(chunks))
        if missing or extra:
            raise ValueError(f"amendment {n} is not paid as frozen: part(s) {missing} of {len(chunks)} missing, "
                             f"part(s) {extra} beyond it")
        for i, chunk in enumerate(chunks):
            if found[i]["sends"] != [[a, v] for a, v in chunk]:
                raise ValueError(f"amendment {n} part {i} ({found[i]['txhash']}) did not send the transfers its file "
                                 f"lists")
        for a, v in payable.items():
            listed[a] = listed.get(a, 0) + v
        total += sum(payable.values())
        entries.append({"id": n, "plan": dg, "decision": doc["decision"], "total_udndr": sum(payable.values()),
                        "txs": [found[i]["txhash"] for i in range(len(chunks))]})
    return total, entries


def stable_bytes(doc: dict) -> bytes:
    """The one serialisation of a list: sorted keys, ASCII, one trailing newline, nothing that depends on the run."""
    return (json.dumps(doc, sort_keys=True, indent=1) + chr(10)).encode("ascii")


def write_stable(path: str, data: bytes) -> tuple[str, bool]:
    """(sha256 of `data`, whether it was written): `data` at `path`, and its digest at `path`.sha256 in the form
    sha256sum reads. A file already at `path` with other bytes raises and is kept: the same run gives the same
    bytes, so other bytes mean the rankings or the payments changed, and a person decides."""
    digest = hashlib.sha256(data).hexdigest()
    try:
        with open(path, "rb") as f:
            old = f.read()
    except FileNotFoundError:
        old = None
    if old is not None and old != data:
        raise FileExistsError(f"{path} already holds other bytes (sha256 {hashlib.sha256(old).hexdigest()}, this "
                              f"run gives {digest}): it is not replaced")
    if old is None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    with open(path + ".sha256", "wb") as f:
        f.write(f"{digest}  {os.path.basename(path)}{chr(10)}".encode("ascii"))
    return digest, old is None


def archive_rankings(src: str, dest: str) -> tuple[int, int | None, str]:
    """(rankings copied, season_end_height or None, sha256 of the manifest): every published ranking from day 0
    on, copied byte for byte into `dest` (a directory that does not exist yet, or is empty) with `SHA256SUMS`, one
    line "<sha256>  <name>" per file. It stops at the first day not published (`ranking_bytes`); a source that
    cannot be read raises. Each copy is read back and compared before the manifest is written."""
    if os.path.isdir(dest) and os.listdir(dest):
        raise FileExistsError(f"{dest} is not empty: an archive is written once, into an empty directory")
    files, end = [], None
    while True:
        n = len(files)
        raw = ranking_bytes(src, n)
        if raw is None:
            break
        doc = json.loads(raw)
        if doc["day"] != n:
            raise ValueError(f"day-{n:03d}.json says it is day {doc['day']!r}")
        if doc["inputs"]["season_end_height"] is not None:
            end = doc["inputs"]["season_end_height"]
        files.append((f"day-{n:03d}.json", raw))
    if not files:
        raise ValueError(f"no ranking of day 0 at {src}: nothing to archive")
    os.makedirs(dest, exist_ok=True)
    for name, raw in files:
        with open(os.path.join(dest, name), "wb") as f:
            f.write(raw)
        with open(os.path.join(dest, name), "rb") as f:
            if f.read() != raw:
                raise OSError(f"{name} reads back other bytes than were written")
    manifest = "".join(f"{hashlib.sha256(raw).hexdigest()}  {name}{chr(10)}" for name, raw in files).encode("ascii")
    with open(os.path.join(dest, "SHA256SUMS"), "wb") as f:
        f.write(manifest)
    return len(files), end, hashlib.sha256(manifest).hexdigest()


# -- amendments (see the module's header) ------------------------------------------------------------------------
_AMEND_KEYS = {"schema", "id", "decision", "reason", "rows", "set_apart"}
_ROW_KEYS = {"day", "miner_id", "address", "udndr", "kit_proof"}
_PROOF_KEYS = {"job_id", "kit_version"}
_APART_KEYS = {"day", "miner_id", "address", "udndr", "why"}


def amend_memo(n: int, digest: str, part: int) -> str:
    return f"dendra-final-season-amend-{n}-{digest}-part-{part}"


def amendment_digest(doc: dict) -> str:
    """The digest an amendment's parts carry: of the file's content as `stable_bytes` writes it, which are the bytes
    `--plan-amendment` writes. Taken from the content, so a checkout that rewrote the line endings names the same
    amendment."""
    return hashlib.sha256(stable_bytes(doc)).hexdigest()[:12]


def amendment_payable(doc: dict) -> dict:
    """{address: udndr} an amendment pays, summed per address, sorted."""
    out = {}
    for row in doc["rows"]:
        out[row["address"]] = out.get(row["address"], 0) + row["udndr"]
    return dict(sorted(out.items()))


def _text(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _row_problem(row, keys: set) -> str:
    """'' when a row of an amendment carries exactly `keys`, each of its type, else what is wrong."""
    if not isinstance(row, dict) or set(row) != keys:
        return f"a row whose keys are not {sorted(keys)}"
    if not (_whole(row["day"]) and row["day"] >= 0):
        return f"a row whose day {row['day']!r} is not a day"
    if not _text(row["miner_id"]):
        return f"a row whose miner_id {row['miner_id']!r} is not an identity"
    if not (_whole(row["udndr"]) and row["udndr"] > 0):
        return f"{row['miner_id']}: udndr {row['udndr']!r} is not a whole number above 0"
    if not isinstance(row["address"], str):
        return f"{row['miner_id']}: an address that is not text"
    return ""


def amendment_problem(doc, n: int) -> str:
    """'' when `doc` is a well formed amendment `n`, else the first thing out of line, in words. Every key is
    required and no other is allowed; the rows paid, then the rows set apart, are sorted by (day, miner_id) with no
    pair twice; a paid row names a payable address and a kit proof (a job id and a whole kit number)."""
    if not isinstance(doc, dict) or set(doc) != _AMEND_KEYS:
        return f"an amendment carries exactly the keys {sorted(_AMEND_KEYS)}"
    if not (_whole(doc["schema"]) and doc["schema"] == AMENDMENT_SCHEMA):
        return f"schema {doc['schema']!r}, this script reads {AMENDMENT_SCHEMA}"
    if not (_whole(doc["id"]) and doc["id"] == n and 1 <= n <= 999):
        return f"id {doc['id']!r}, the file is amendment {n}"
    if not (_text(doc["decision"]) and _text(doc["reason"])):
        return "the decision and the reason are required, as text"
    rows, apart = doc["rows"], doc["set_apart"]
    if not (isinstance(rows, list) and rows and isinstance(apart, list)):
        return "rows is a list of at least one row, set_apart a list"
    seen = set()
    for keys, listed in ((_ROW_KEYS, rows), (_APART_KEYS, apart)):
        last = None
        for row in listed:
            bad = _row_problem(row, keys)
            if bad:
                return bad
            key = (row["day"], row["miner_id"])
            if key in seen or (last is not None and key <= last):
                return f"day {key[0]}: {key[1]} is listed twice or out of order"
            seen.add(key)
            last = key
    for row in rows:
        if not _SAFE_ID.match(row["miner_id"]):
            return f"day {row['day']}: the identity {row['miner_id']!r} cannot name a relay deposit"
        unpayable = payable_address(row["address"])
        if unpayable:
            return f"day {row['day']}: {row['miner_id']} is paid to {row['address']!r}: {unpayable}"
        proof = row["kit_proof"]
        if not (isinstance(proof, dict) and set(proof) == _PROOF_KEYS and isinstance(proof["job_id"], str)
                and _SAFE_ID.match(proof["job_id"]) and _whole(proof["kit_version"]) and proof["kit_version"] >= 0):
            return f"day {row['day']}: {row['miner_id']} has no well formed kit_proof {{job_id, kit_version}}"
    if not all(_text(row["why"]) for row in apart):
        return "a row set apart says why, as text"
    return ""


def _unique_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"the key {k!r} appears twice in one object")
        out[k] = v
    return out


def load_amendments(directory: str) -> list:
    """[(id, doc, digest)] of every frozen amendment in `directory`, by id. A directory that does not exist holds
    none: the image carries it once a first amendment is frozen, and an amendment paid on chain that no file names
    refuses the season's list. Anything else in the directory raises ValueError, since a misnamed amendment would be
    left out of the list; so does a file that is not a well formed amendment (`amendment_problem`)."""
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        path = os.path.join(directory, name)
        m = _AMEND_FILE.match(name)
        if not m or not os.path.isfile(path):
            raise ValueError(f"{path} is not an amendment file (amendment-NNN.json), and nothing there is skipped")
        with open(path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"), object_pairs_hook=_unique_keys)
        n = int(m.group(1))
        bad = amendment_problem(doc, n)
        if bad:
            raise ValueError(f"{name}: {bad}")
        out.append((n, doc, amendment_digest(doc)))
    return out


def amendment_refusal(n: int, amendments: list, days: list) -> str:
    """'' when amendment `n`, one of `amendments` [(id, doc, digest)], can be paid on the published days `days` (from
    day 0, already through `check_days`), else the first thing out of line, in words.

    Each row must still match its day's ranking: the day ranked, the identity ranked with exactly the reason
    `final_season_calc.NO_KIT_REPORT`, nothing payable or paid, a gross equal to the row's udndr and the same payout
    address; its kit proof must reach the day's floor. No (day, miner_id) is in two amendments. Each of its days, with
    every amendment of that day, stays within the day's `cap_programme_day`, and the published days with every
    amendment stay within `cap_season` of the last day's set."""
    docs = {k: doc for k, doc, _dg in amendments}
    if n not in docs:
        return f"amendment {n} is not among the frozen amendments"
    owner, per_day, added = {}, {}, 0
    for k, doc, _dg in amendments:
        for row in doc["rows"]:
            key = (row["day"], row["miner_id"])
            if key in owner:
                return (f"day {key[0]}: {key[1]} is in amendment {owner[key]} and in amendment {k}: it would be "
                        f"paid twice")
            owner[key] = k
            per_day[row["day"]] = per_day.get(row["day"], 0) + row["udndr"]
            added += row["udndr"]
    for row in docs[n]["rows"]:
        bad = _row_refusal(n, row, days)
        if bad:
            return bad
    for d in sorted({row["day"] for row in docs[n]["rows"]}):
        cap = _set_of_day(days[d])[1]["cap_programme_day"]
        if days[d].total_paid + per_day[d] > cap:
            return (f"day {d}: the ranking paid {days[d].total_paid} and the amendments add {per_day[d]}, over the "
                    f"day's cap of {cap}")
    ranked_total = sum(x.total_paid for x in days)
    cap_season = _set_of_day(days[-1])[1]["cap_season"]
    if ranked_total + added > cap_season:
        return (f"the rankings paid {ranked_total} and the amendments add {added}, over the season's cap of "
                f"{cap_season}")
    return ""


_PROBATION_NOTE = re.compile(r"^probation: first full day after registration, \d+ percent of the gross$")


def _refused_for_the_report_alone(x) -> bool:
    """Whether the floor's missing report is the only thing that kept row `x` unpaid. The probation's note may stand
    beside it on a probation row: `final_season_calc.gross_of` already cut that gross to the probation's share, so
    the gross is what the day would have paid. Any other reason keeps the row apart."""
    if x.kit_version is not None:
        return False
    if x.reason == NO_KIT_REPORT:
        return True
    parts = x.reason.split("; ")
    return (x.probation is True and len(parts) == 2 and parts[1] == NO_KIT_REPORT
            and _PROBATION_NOTE.match(parts[0]) is not None)


def _row_refusal(n: int, row: dict, days: list) -> str:
    """'' when a row of amendment `n` still matches its day's ranking (`amendment_refusal`), else why not."""
    at = f"amendment {n}, day {row['day']}, {row['miner_id']}"
    if row["day"] >= len(days):
        return f"{at}: the day is not ranked"
    day = days[row["day"]]
    ranked = [x for x in day.rows if x.miner_id == row["miner_id"]]
    if len(ranked) != 1:
        return f"{at}: the identity is not in the day's ranking"
    x = ranked[0]
    if not _refused_for_the_report_alone(x):
        return f"{at}: the ranking now gives the reason {x.reason!r} and the kit {x.kit_version!r}"
    if x.payable != 0 or x.paid != 0:
        return f"{at}: the ranking now makes it payable {x.payable}, paid {x.paid}"
    if x.gross != row["udndr"]:
        return f"{at}: {row['udndr']} udndr, but the ranking's gross is {x.gross}"
    if x.payout_address != row["address"]:
        return f"{at}: paid to {row['address']}, but the ranking names {x.payout_address!r}"
    if not (_whole(day.kit_min) and row["kit_proof"]["kit_version"] >= day.kit_min):
        return f"{at}: kit proof {row['kit_proof']['kit_version']}, the day's floor is {day.kit_min!r}"
    return ""


def amended_rows_refusal(amendments: list, days: list, wd: list) -> str:
    """'' when every row of a frozen amendment on one of the week's days `wd` still matches the ranking (`_row_refusal`)
    in `days` (from day 0, checked), else why not. The week pays what the rankings pay: a ranking that now pays a row
    an amendment pays, or gives it otherwise than its file, would have it paid twice, or on figures no longer
    published, so the week is not paid and a person decides."""
    for k, doc, _dg in amendments:
        for row in doc["rows"]:
            if row["day"] in wd:
                bad = _row_refusal(k, row, days)
                if bad:
                    return f"{bad}; a week paying that day could pay the row twice"
    return ""


def amendment_chain_refusal(n: int, amendments: list, on_chain: dict) -> str:
    """'' when every amendment the payout account paid (`on_chain`, {id: {part: (digest, txhash)}} from
    `amendment_parts`) is a frozen file of `amendments` and, for every id but `n` (which `_decide` reads), each of its
    parts carries that file's digest; else why not. A file missing from the image, or edited after it was paid, would
    let its rows be frozen again in another amendment, and that one would pay them a second time."""
    files = {k: dg for k, _doc, dg in amendments}
    stray = sorted(k for k in on_chain if k not in files)
    if stray:
        return (f"amendment(s) {stray} are paid on chain, and no frozen amendment file names them: the directory is "
                f"not the one they were paid from")
    for k in sorted(on_chain):
        other = sorted(p for p, (dg, _h) in on_chain[k].items() if dg != files[k])
        if k != n and other:
            return (f"amendment {k}: part(s) {other} are on chain under another digest than its file gives "
                    f"({files[k]}): the file changed after a payment")
    return ""


def amendment_parts(transfers: list) -> dict:
    """{id: {part: (digest, txhash)}} of the amendments' transactions (`amendment_transfers`)."""
    out = {}
    for t in transfers:
        out.setdefault(t["amendment"], {})[t["part"]] = (t["plan"], t["txhash"])
    return out


def _amendment_days(src: str, need: int, doubled_from) -> list:
    """The published days from day 0 on, up to the first one not published or the one that holds the season's end,
    checked as a week's are (`check_days`, `check_doubled_from`). Days 0 to `need` must all be there: an amendment
    pays rows of ranked days only, and the budget and the season cap read every published day. Raises the classes
    `plan_days` raises."""
    read = []
    while True:
        at = len(read)
        try:
            day, end = load_day_end(src, at)
        except Exception as e:  # noqa: BLE001
            if _not_published(e) and at > need:
                break
            cls = NotRankedYet if _not_published(e) else DayUnreadable
            raise cls(f"day {at} is not ranked yet or unreadable ({type(e).__name__}): no amendment is planned "
                      f"or paid on day {need}", at)
        read.append(day)
        if end is not None:
            break
    if len(read) <= need:
        raise PastSeasonEnd(f"day {need} is after the season's last day ({len(read) - 1})", len(read) - 1)
    refusal = check_days(read) or check_doubled_from(read, doubled_from)
    if refusal:
        raise PlanRefused(f"Refused: {refusal}. No amendment is planned or paid.")
    return read


def _evidence_file(evidence: str, day: int):
    """The records of one published evidence file (`day-NNN.jsonl`) as the service reads its own
    (`final_season_evidence.Evidence.read`, the same text-mode line split), or None when the source says it does not
    exist. Any other failure raises."""
    try:
        raw = _read_bytes(evidence, f"day-{day:03d}.jsonl")
    except Exception as e:  # noqa: BLE001
        if _not_published(e):
            return None
        raise
    out, bad = [], 0
    for line in io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            bad += 1
    if bad:
        out.append({"type": "_unreadable_lines", "count": bad, "day": day})
    return out


def _ranked_evidence(evidence: str, day: PublishedDay) -> list:
    """The records of day `day` from the published evidence, once the files of days 0 to `day` give the digest the
    ranking publishes (`inputs.evidence_sha256`, computed by `final_season_rank.rank_day` over the records the service
    read, `final_season_server.ranking_inputs`). Raises PlanRefused when the ranking carries no digest, when the day's
    own file is missing, or when the files differ from what was ranked: a proof is only read from that evidence."""
    want = day.evidence_sha256
    if not (isinstance(want, str) and re.fullmatch(r"[0-9a-f]{64}", want)):
        raise PlanRefused(f"Refused: day {day.day} publishes no evidence digest ({want!r}); its evidence cannot be "
                          f"tied to its ranking. No amendment is written.")
    upto, own = [], None
    for d in range(day.day + 1):
        recs = _evidence_file(evidence, d)
        upto.extend(recs or [])
        if d == day.day:
            own = recs
    got = hashlib.sha256("".join(json.dumps(r, sort_keys=True) + chr(10) for r in upto).encode()).hexdigest()
    if own is None or got != want:
        raise PlanRefused(f"Refused: the evidence of days 0 to {day.day} at {evidence} gives the digest {got}, the "
                          f"ranking of day {day.day} was computed from {want}"
                          + ("" if own is not None else f" (day-{day.day:03d}.jsonl is missing)")
                          + ". No amendment is written.")
    return own


def _answered_jobs(records: list, day: int) -> dict:
    """{miner_id: [job_id, ...]} of the `work_answer` records filed under day `day` among `records` (the day's
    evidence file, `_ranked_evidence`). A line that did not parse holds no proof."""
    out = {}
    for rec in records:
        if not (isinstance(rec, dict) and rec.get("type") == "work_answer" and _whole(rec.get("day"))
                and rec.get("day") == day):
            continue
        mid, job = rec.get("miner_id"), rec.get("job_id")
        if isinstance(mid, str) and isinstance(job, str) and _SAFE_ID.match(job):
            out.setdefault(mid, set()).add(job)
    return {m: sorted(v) for m, v in out.items()}


def _kit_proof(relay: str, miner_id: str, jobs: list, kit_min: int):
    """{job_id, kit_version} of the first of `jobs` (in id order) whose answer deposit on the relay
    (`res/<job_id>__<miner_id>`) declares a kit at or above `kit_min` (`modea.relay_write.declared_kit_version`, the
    field the relay itself reads), or None. A deposit the relay does not hold proves nothing and the next job is
    read; a relay that does not answer raises, since an unread deposit is not a missing proof."""
    for job in jobs:
        try:
            body = _read_bytes(f"{relay.rstrip('/')}/res", f"{job}__{miner_id}")
        except Exception as e:  # noqa: BLE001
            if _not_published(e):
                continue
            raise
        kv = declared_kit_version(body)
        if kv is not None and kv >= kit_min:
            return {"job_id": job, "kit_version": kv}
    return None


def plan_amendment(n: int, wanted: list, rankings: str, evidence: str, relay: str, decision: str, reason: str,
                   doubled_from, frozen: list) -> dict:
    """Amendment `n` for the days `wanted`, read only (see the module's header): never signs, never writes. `frozen`
    is what `load_amendments` read, so a row already in a frozen amendment and the budgets they take are counted.
    Raises the classes `plan_days` raises on the rankings, PlanRefused on an amendment out of line, and what a read
    of the evidence or the relay raises."""
    if not (_whole(n) and 1 <= n <= 999):
        raise PlanRefused(f"Refused: amendment {n!r} is not a number from 1 to 999.")
    if any(k == n for k, _doc, _dg in frozen):
        raise PlanRefused(f"Refused: amendment {n} is already frozen; a frozen amendment is never planned again.")
    if not (wanted and all(_whole(d) and d >= 0 for d in wanted) and list(wanted) == sorted(set(wanted))):
        raise PlanRefused(f"Refused: the days {wanted!r} are not a list of distinct days in order.")
    days = _amendment_days(rankings, wanted[-1], doubled_from)
    rows, apart = [], []
    for d in wanted:
        day = days[d]
        if not _whole(day.kit_min):
            raise PlanRefused(f"Refused: day {d} is ranked under a set without a kit floor, and no row of it is "
                              f"refused for want of a capacity report.")
        found = sorted((x for x in day.rows if _refused_for_the_report_alone(x) and x.gross > 0 and x.paid == 0),
                       key=lambda x: x.miner_id)
        # The missing report beside another reason than the probation's note: listed apart with the ranking's words,
        # so no row the floor refused goes unseen.
        for x in day.rows:
            if (not _refused_for_the_report_alone(x) and NO_KIT_REPORT in x.reason.split("; ") and x.gross > 0
                    and x.paid == 0):
                apart.append({"day": d, "miner_id": x.miner_id, "address": x.payout_address, "udndr": x.gross,
                              "why": f"the ranking gives another reason beside the missing report: {x.reason}"})
        jobs = _answered_jobs(_ranked_evidence(evidence, day), d) if found else {}
        for x in found:
            row = {"day": d, "miner_id": x.miner_id, "address": x.payout_address, "udndr": x.gross}
            unpayable = payable_address(x.payout_address)
            named = _SAFE_ID.match(x.miner_id) is not None
            proof = (_kit_proof(relay, x.miner_id, jobs.get(x.miner_id, []), day.kit_min)
                     if named and not unpayable else None)
            if unpayable:
                apart.append(dict(row, why=f"unpayable address: {unpayable}"))
            elif not named:
                apart.append(dict(row, why="an identity that cannot name a relay deposit"))
            elif proof is None:
                apart.append(dict(row, why=f"no work_answer job of the day whose relay deposit declares kit_version "
                                           f"{day.kit_min} or above"))
            else:
                rows.append(dict(row, kit_proof=proof))
    apart.sort(key=lambda x: (x["day"], x["miner_id"]))
    doc = {"schema": AMENDMENT_SCHEMA, "id": n, "decision": decision, "reason": reason, "rows": rows,
           "set_apart": apart}
    bad = amendment_problem(doc, n) or amendment_refusal(n, list(frozen) + [(n, doc, amendment_digest(doc))], days)
    if bad:
        raise PlanRefused(f"Refused: {bad}. No amendment is written.")
    return doc


def _print_amendment(n: int, digest: str, doc: dict) -> None:
    payable = amendment_payable(doc)
    print(f"amendment {n}, digest {digest}: {len(doc['rows'])} row(s), {sum(payable.values())} udndr to "
          f"{len(payable)} address(es) in {len(parts(payable))} part(s); {len(doc['set_apart'])} row(s) set apart")
    for row in doc["rows"]:
        print(f"  day {row['day']}  {row['miner_id']}  {row['address']}  {row['udndr']}  proof "
              f"{row['kit_proof']['job_id']} kit {row['kit_proof']['kit_version']}")
    for row in doc["set_apart"]:
        print(f"  set apart: day {row['day']}  {row['miner_id']}  {row['address']}  {row['udndr']}  ({row['why']})")


def _plan_amendment_main(a) -> int:
    given = {"--days": a.days, "--evidence": a.evidence, "--relay": a.relay, "--decision": a.decision,
             "--reason": a.reason, "--out": a.out}
    missing = [k for k, v in given.items() if not v]
    if missing:
        print(f"Refused: {', '.join(missing)} required to plan an amendment; none is defaulted.")
        return 2
    try:
        doc = plan_amendment(a.plan_amendment, a.days, a.rankings, a.evidence, a.relay, a.decision, a.reason,
                             a.doubled_from_day, load_amendments(AMENDMENTS_DIR))
        digest = amendment_digest(doc)
        _print_amendment(a.plan_amendment, digest, doc)
        sha, written = write_stable(a.out, stable_bytes(doc))
    except SystemExit as e:
        print(e)
        return 3
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Refused: {type(e).__name__}: {e}. No amendment is written.")
        return 3
    print(f"plan only, nothing signed. sha256 {sha}  {a.out}" + ("" if written else " (unchanged)"))
    print(f"to freeze it, commit it as {os.path.join(AMENDMENTS_DIR, f'amendment-{a.plan_amendment:03d}.json')}")
    return 0


def _amendment_main(a) -> int:
    """--amendment N (see the module's header)."""
    n = a.amendment
    try:
        frozen = load_amendments(AMENDMENTS_DIR)
        doc = {k: d for k, d, _dg in frozen}.get(n)
        if doc is None:
            print(f"Refused: no frozen amendment {n} in {AMENDMENTS_DIR}. Nothing is signed.")
            return 3
        days = _amendment_days(a.rankings, max(row["day"] for row in doc["rows"]), a.doubled_from_day)
    except SystemExit as e:
        print(e)
        return 3
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Refused: {type(e).__name__}: {e}. Nothing is signed.")
        return 3
    bad = amendment_refusal(n, frozen, days)
    if bad:
        print(f"Refused: {bad}. Amendment {n} is not paid; nothing is signed.")
        return 3
    digest, payable = amendment_digest(doc), amendment_payable(doc)
    _print_amendment(n, digest, doc)
    if a.yes and _chain_id_refusal(a):
        return 2
    payer, rc = _payer(a)
    if rc:
        return rc
    chunks = parts(payable)
    try:
        on_chain = amendment_parts(amendment_transfers(a.node, payer))
        bad = amendment_chain_refusal(n, frozen, on_chain)
        if bad:
            print(f"Refused: {bad}. Amendment {n} is not paid; nothing is signed.")
            return 3
        verdict, which = _decide(digest, len(chunks), on_chain.get(n, {}))
        if verdict == "stop":
            print(f"Refused: part(s) {which} of amendment {n} are on chain under another digest. The file changed "
                  f"after a partial payment; decide by hand what is owed before paying anything.")
            return 3
        if verdict == "done":
            print(f"amendment {n} is already paid in full under digest {digest}: nothing sent.")
            return 0
        if not a.yes:
            print(f"plan only, nothing signed: part(s) {which} would be sent. Add --yes to pay.")
            return 0
        for i in which:
            h = _send_tx(chunks[i], amend_memo(n, digest, i), i, a.key, payer, a.chain_id, a.node, a.home)
            print(f"  part {i}: {len(chunks[i])} transfers, tx {h}", flush=True)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
        # A part that landed before the failure is found on chain by the next run.
        print(f"amendment {n}: failed: {type(e).__name__}: {e}")
        return 3
    print(f"amendment {n} paid, part(s) {which} sent.")
    return 0


def _days_arg(s: str) -> list:
    """--days: day numbers, comma separated, distinct and in order."""
    items = s.split(",")
    if not all(x and all(c in "0123456789" for c in x) for x in items):
        raise argparse.ArgumentTypeError("day numbers, comma separated")
    days = [int(x) for x in items]
    if days != sorted(set(days)):
        raise argparse.ArgumentTypeError("distinct days, in order")
    return days


_NOT_GIVEN = object()


def _season_list_main(a) -> int:
    if not (a.payer.startswith("dendra1") and not payable_address(a.payer)):
        print(f"Refused: --payer {a.payer!r} is not an account address; it is never defaulted.")
        return 2
    try:
        days, digests, end = season_days(a.rankings)
        amendments = load_amendments(AMENDMENTS_DIR)
        C.require_index_from(C.rpc_url(a.node), a.start)
        doc = season_list(days, digests, end, payout_transfers(a.node, a.payer), a.doubled_from_day, a.payer,
                          amendments, amendment_transfers(a.node, a.payer))
    except (C.ChainUnreadable, RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
        print(f"Refused: {e}. No list is written.")
        return 3
    try:
        digest, written = write_stable(a.season_list, stable_bytes(doc))
    except OSError as e:
        print(f"Refused: {e}")
        return 3
    print(f"season list: days 0 to {doc['last_day']} (last block {end}), {len(doc['paid_udndr'])} addresses, "
          f"{doc['total_paid_udndr']} udndr, equal to what the payout account sent, {doc['total_amended_udndr']} "
          f"of them by {len(doc['amendments'])} amendment(s); "
          f"{doc['total_set_aside_udndr']} udndr ranked to unpayable addresses, never sent")
    print(f"sha256 {digest}  {a.season_list}" + ("" if written else " (unchanged)"))
    return 0


def _archive_main(a) -> int:
    try:
        n, end, digest = archive_rankings(a.rankings, a.archive_rankings)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Refused: {e}. Nothing archived.")
        return 3
    print(f"archived {n} ranking(s), days 0 to {n - 1}, into {a.archive_rankings}; "
          + (f"the last carries the season's last block {end}" if end is not None
             else "none carries the season's last block: the season was not over or its last day not ranked"))
    print(f"sha256 {digest}  SHA256SUMS")
    return 0


def _doubled_from_arg(s: str):
    """--doubled-from-day: a day number, or `none` for no doubled day."""
    if s == "none":
        return None
    if not (s and all(c in "0123456789" for c in s)):
        raise argparse.ArgumentTypeError("a day number, or none")
    return int(s)


def _print_plan(week: int, wd: list, digest: str, payable: dict, aside: dict) -> None:
    print(f"week {week} (days {wd}), plan {digest}: {len(payable)} addresses, "
          f"{sum(payable.values())} udndr in {len(parts(payable))} part(s)")
    for addr, v in payable.items():
        print(f"  {addr}  {v}")
    for addr, v in aside.items():
        print(f"  set aside (unpayable: {payable_address(addr)})  {addr}  {v}")


def _chain_id_refusal(a) -> int:
    """2, after saying why, when no chain id is given to sign with (it is never defaulted); else 0."""
    if a.chain_id:
        return 0
    print("Refused: --chain-id (or DENDRA_CHAIN_ID) is required to sign; it is never defaulted.")
    return 2


def _payer(a) -> tuple[str, int]:
    """(the payout account's address, 0), or ('', 2) after saying why: the key is not in the keyring, or the node
    does not keep the whole history, where a search for the parts already paid would find nothing and read as
    "nothing paid yet". --week and --due both read it here, once, before any payment."""
    out = subprocess.run(["dendrad", "keys", "show", a.key, "-a", "--keyring-backend", "test", "--home", a.home],
                         capture_output=True, text=True, timeout=30)
    payer = out.stdout.strip()
    if out.returncode != 0 or not payer.startswith("dendra1"):
        print(f"Refused: key {a.key!r} not found in {a.home} ({out.stderr.strip()[:160]})")
        return "", 2
    try:
        C.require_index_from(C.rpc_url(a.node), a.start)
    except C.ChainUnreadable as e:
        print(f"Refused: {e}")
        return "", 2
    return payer, 0


def send_parts(payable: dict, which: list, week: int, digest: str, payer: str, a) -> None:
    """Sends the parts `which` of the week's plan, one transaction each, in order, and prints each one's txhash.
    Raises RuntimeError on a part refused or not seen in a block; a later run finds on chain the parts that landed."""
    chunks = parts(payable)
    for i in which:
        h = send_part(chunks[i], week, digest, i, a.key, payer, a.chain_id, a.node, a.home)
        print(f"  part {i}: {len(chunks[i])} transfers, tx {h}", flush=True)


def _due_main(a) -> int:
    """--due (see the module's header): every week payable now and not paid yet, from week 0 on."""
    try:
        sys.stdout.reconfigure(line_buffering=True)   # the cron log gets each line as it happens, not at the exit
    except (AttributeError, ValueError):
        pass
    if a.yes and _chain_id_refusal(a):
        print("summary: not run, nothing read and nothing paid (exit 2)")
        return 2
    payer, rc = _payer(a)
    if rc:
        print(f"summary: not run, nothing read on chain and nothing paid (exit {rc})")
        return rc
    paid, already, due = [], [], []
    week, rc, end = 0, 0, ""
    while True:
        try:
            payable, aside, wd = plan_days(week, a.rankings, a.doubled_from_day)
        except NotRankedYet as e:
            # A part is sent only once every day of its week is published. A part on chain for this week or a later
            # one means rankings it was paid from have gone (a purged service): not a wait, or the cron would wait
            # in silence from then on.
            try:
                later = sorted(w for w in _weeks_on_chain(a.node, payer) if w >= week)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as x:
                print(f"week {week}: failed: {type(x).__name__}: {x}")
                rc, end = 3, f"week {week} failed, no later week paid"
                break
            if later:
                print(f"week {week}: refused: day {e.day} is not published, yet week(s) {later} are paid on chain: the "
                      f"rankings those payments were planned from are gone, and a person decides what to do")
                rc, end = 3, f"week {week} refused, no later week paid"
                break
            # Nor is a day the service is late to rank: a service that stopped ranking would leave the cron waiting
            # in silence, every week unpaid. Day 0 has no earlier ranking to say where it ends.
            if e.day == 0:
                print(f"week {week}: not payable yet: day 0 is not ranked; no earlier ranking says where it ends, so "
                      f"whether it is late is not known")
                end = f"week {week} waits for day 0"
                break
            try:
                late = late_from(_read(a.rankings, f"day-{e.day - 1:03d}.json"), e.day)
                now = C.height(C.rpc_url(a.node))
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as x:
                print(f"week {week}: failed: whether day {e.day} is late cannot be read: {type(x).__name__}: {x}")
                rc, end = 3, f"week {week} failed, no later week paid"
                break
            if now >= late:
                print(f"week {week}: refused: day {e.day} is late: it is not ranked, and the chain is at block {now}, "
                      f"at or past block {late}, where even the day after it is final. Read the final-season "
                      f"service's log")
                rc, end = 3, f"week {week} waits for day {e.day}, which is late"
                break
            print(f"week {week}: not payable yet: day {e.day} is not ranked (late from block {late}, the chain is "
                  f"at {now})")
            end = f"week {week} waits for day {e.day}"
            break
        except PastSeasonEnd as e:
            print(f"week {week}: starts after the season's last day ({e.last_day}): no week left to pay")
            end = f"the season has no week after week {week - 1}"
            break
        except DayUnreadable as e:
            print(f"week {week}: refused: day {e.day} cannot be read, and an unreadable day is not a day still to "
                  f"come ({e})")
            rc, end = 3, f"week {week} refused, no later week paid"
            break
        except PlanRefused as e:
            print(f"week {week}: {e}")
            rc, end = 3, f"week {week} refused, no later week paid"
            break
        digest = plan_digest(payable)
        head = f"week {week} (days {wd[0]} to {wd[-1]}), plan {digest}"
        try:
            verdict, which = decide(payable, paid_parts(a.node, payer, week))
            if verdict == "stop":
                print(f"week {week}: refused: part(s) {which} are on chain under another plan. The rankings changed "
                      f"after a partial payment; decide by hand what is owed before paying anything.")
                rc, end = 3, f"week {week} refused, no later week paid"
                break
            if verdict == "done":
                print(f"{head}: already paid in full, nothing sent")
                already.append(week)
            elif not a.yes:
                _print_plan(week, wd, digest, payable, aside)
                print(f"{head}: due, plan only: part(s) {which} would be sent")
                due.append(week)
            else:
                _print_plan(week, wd, digest, payable, aside)
                send_parts(payable, which, week, digest, payer, a)
                print(f"{head}: paid, part(s) {which} sent")
                paid.append(week)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
            # The chain search or a part failed. A part that landed before the failure is found on chain next run.
            print(f"week {week}: failed: {type(e).__name__}: {e}")
            rc, end = 3, f"week {week} failed, no later week paid"
            break
        week += 1
    if a.yes:
        print(f"summary: weeks paid now {paid}, already paid {already}; {end} (exit {rc})")
    else:
        print(f"summary, plan only, nothing signed: weeks due {due}, already paid {already}; {end} (exit {rc})")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Final Testnet Season weekly payment")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--week", type=int)
    mode.add_argument("--due", action="store_true",
                      help="every week payable now and not paid yet, from week 0 on (with --yes, pay them)")
    mode.add_argument("--season-list", metavar="FILE",
                      help="write the season's list address -> paid_udndr, checked against the payments on chain")
    mode.add_argument("--archive-rankings", metavar="DIR", help="copy every published ranking into an empty DIR")
    mode.add_argument("--plan-amendment", metavar="N", type=int,
                      help="write amendment N for --days to --out, read only (see the module's header)")
    mode.add_argument("--amendment", metavar="N", type=int,
                      help="pay the frozen amendment N (with --yes), or say what would be sent")
    ap.add_argument("--days", type=_days_arg, help="--plan-amendment: the days, comma separated")
    ap.add_argument("--evidence", help="--plan-amendment: directory or URL holding day-NNN.jsonl")
    ap.add_argument("--relay", help="--plan-amendment: the relay's URL, whose res/<job>__<miner> deposits are read")
    ap.add_argument("--decision", help="--plan-amendment: the decision the amendment applies")
    ap.add_argument("--reason", help="--plan-amendment: why the amendment pays")
    ap.add_argument("--out", help="--plan-amendment: the file to write")
    ap.add_argument("--payer", default="", help="the payout account's address (--season-list)")
    ap.add_argument("--rankings", required=True, help="directory or URL holding day-NNN.json")
    ap.add_argument("--node", default=os.environ.get("DENDRA_NODE", "tcp://chain:26657"))
    ap.add_argument("--chain-id", default=os.environ.get("DENDRA_CHAIN_ID", ""))
    ap.add_argument("--start", type=int, default=int(os.environ.get("DENDRA_FINAL_SEASON_START_HEIGHT", "1") or 1))
    ap.add_argument("--key", default="payout")
    ap.add_argument("--home", default=os.environ.get("DENDRA_HOME", "/root/.dendra"))
    ap.add_argument("--doubled-from-day", type=_doubled_from_arg, default=_NOT_GIVEN,
                    help="first day of the doubled rates as decision 22 fixed it, or none (required to pay "
                         "a week or write the season's list)")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.archive_rankings is not None:
        return _archive_main(a)
    if a.doubled_from_day is _NOT_GIVEN:
        ap.error("--doubled-from-day is required: a day number, or none")
    if a.season_list is not None:
        return _season_list_main(a)
    if a.plan_amendment is not None:
        return _plan_amendment_main(a)
    if a.amendment is not None:
        return _amendment_main(a)
    if a.due:
        return _due_main(a)
    payable, aside, wd = plan_days(a.week, a.rankings, a.doubled_from_day)
    digest = plan_digest(payable)
    _print_plan(a.week, wd, digest, payable, aside)
    if not a.yes:
        print("plan only. Nothing signed. Add --yes to pay.")
        return 0
    rc = _chain_id_refusal(a)
    if rc:
        return rc
    payer, rc = _payer(a)
    if rc:
        return rc
    verdict, which = decide(payable, paid_parts(a.node, payer, a.week))
    if verdict == "done":
        print(f"week {a.week} is already paid in full under plan {digest}: nothing sent.")
        return 0
    if verdict == "stop":
        print(f"Refused: part(s) {which} of week {a.week} are on chain under another plan. The rankings "
              f"changed after a partial payment; decide by hand what is owed before paying anything.")
        return 3
    send_parts(payable, which, a.week, digest, payer, a)
    print(f"week {a.week} paid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
