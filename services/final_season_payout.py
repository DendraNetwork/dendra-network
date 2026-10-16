#!/usr/bin/env python3
"""final_season_payout.py — the Final Testnet Season's weekly payment (ADR-047), from the payout account.

    python3 final_season_payout.py --week 0 --rankings <dir or URL> --doubled-from-day 4         # plan only
    python3 final_season_payout.py --week 0 --rankings <dir or URL> --doubled-from-day 4 --yes   # sign and broadcast
    python3 final_season_payout.py --season-list <file> --payer <address> --rankings <dir or URL> --doubled-from-day 4
    python3 final_season_payout.py --archive-rankings <empty dir> --rankings <dir or URL>

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
`--doubled-from-day` names the day ADR-047 decision 22 fixed (or `none`), and it is required: every day from it
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

The season's list of payments (ADR-049 decision 2)
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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import final_season_chain as C  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_address import payable_address  # noqa: E402
from final_season_calc import (DayResult, Identity, Row, gross_of, kit_allows_pay, registration_allows_pay,  # noqa: E402
                               week_totals)
from final_season_rules import (RULES_DOUBLED, RULES_MORE_REQUESTS, fingerprint, pays_judge_presence,  # noqa: E402
                                rule_set_of)

BATCH = 100
GAS_PER_SEND = 60_000
_MEMO = re.compile(r"^dendra-final-season-week-(\d+)-([0-9a-f]{12})-part-(\d+)$")


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
            raise SystemExit(f"week {week} starts after the season's last day ({last_day}): there is nothing to pay")
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
    # earlier set only.
    rows = [Row(x["miner_id"], x["payout_address"], x["presence"], x["verified_requests"],
                x["verdicts"], x["gross_udndr"], x["payable_udndr"], x["paid_udndr"], x["reason"],
                x.get("judge_active", 0), x.get("probation", False), x.get("kit_version"),
                x.get("registration_not_dated", False))
            for x in d["ranking"]]
    end, last = d["inputs"]["season_end_height"], d["inputs"]["last_height"]
    # Bounded from below and tied to the day: the ranking that holds the end names it as its own last
    # counted block (`final_season_rank.rank_day`). A 0, or a height that is not this day's last block,
    # would end the season early and refuse every later week.
    if end is not None and (type(end) is not int or type(last) is not int or not 0 < end == last):
        raise ValueError(f"day {day}: season_end_height {end!r} is not this day's last block ({last!r})")
    # The floor of decision 24, when the day publishes one (`inputs.kit_floor.kit_min`): what names the day's set.
    kit_min = (d["inputs"].get("kit_floor") or {}).get("kit_min") if isinstance(d["inputs"].get("kit_floor"), dict) else None
    return DayResult(d["day"], d["rules_fingerprint"], d["total_gross_udndr"], d["total_payable_udndr"],
                     d["day_budget_udndr"], d["total_paid_udndr"], d["pro_rata"][0], d["pro_rata"][1], rows,
                     kit_min), end


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
    return pos, r


def doubled_sets() -> tuple:
    """The sets that carry the doubled rates (ADR-047 decision 22): RULES_DOUBLED, RULES_MORE_REQUESTS (decision 23,
    which changes only `requests_per_day`) and RULES_KIT (decision 24, which adds to them) once its floor is known."""
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
            raise SystemExit(f"day {d} is not ranked yet or unreadable ({type(e).__name__}): week {week} "
                             f"is not paid in part")
        loaded.append(day)
        if end is not None:
            last_day = d
            break
    wd = week_days(week, last_day)
    bad = check_days(loaded) or check_doubled_from(loaded, doubled_from)
    if bad:
        raise SystemExit(f"Refused: {bad}. Week {week} is not paid; nothing is signed.")
    try:
        payable, aside = week_plan(loaded[wd[0]:wd[-1] + 1])
    except ValueError as e:
        raise SystemExit(f"Refused: {e}. Week {week} is not paid; nothing is signed.") from e
    return payable, aside, wd


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
    digest = plan_digest(payable)
    conflict = sorted(p for p, (dg, _h) in done.items() if dg != digest)
    if conflict:
        return "stop", conflict
    todo = [i for i in range(len(parts(payable))) if i not in done]
    return ("pay", todo) if todo else ("done", [])


def send_part(chunk: list, week: int, digest: str, part: int, payer_key: str, payer: str, chain_id: str,
              node: str, home: str) -> str:
    tx = {"body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend", "from_address": payer,
                                 "to_address": a, "amount": [{"denom": "udndr", "amount": str(v)}]}
                                for a, v in chunk],
                   "memo": memo(week, digest, part), "timeout_height": "0",
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


# -- the season's list of payments (ADR-049 decision 2) and the archive of the rankings ----------------------
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
    out = []
    for t in _payer_txs(node, payer):
        body = (t.get("tx") or {}).get("body") or {}
        m = _MEMO.match(body.get("memo", ""))
        if not m:
            continue
        msgs = body.get("messages")
        sends = [_udndr_send(x, payer) for x in msgs] if isinstance(msgs, list) and msgs else [None]
        if any(s is None for s in sends):
            raise RuntimeError(f"transaction {t.get('txhash', '')} carries the memo of week {m.group(1)} part "
                               f"{m.group(3)} but not only udndr transfers from {payer}: what the season sent "
                               f"cannot be counted, no list is written")
        out.append({"week": int(m.group(1)), "plan": m.group(2), "part": int(m.group(3)),
                    "txhash": t.get("txhash", ""), "sends": sends})
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


def season_list(days: list, digests: dict, end_height: int, transfers: list, doubled_from, payer: str) -> dict:
    """The season's list (see the module's header). Raises ValueError on the first thing out of line. Pure: the
    rankings (`season_days`) and the transfers (`payout_transfers`) are read by the caller.

    Each week is planned from the rankings as `plan_days` plans it, and its payment is read on chain: every part
    under the week's plan digest, each part once, each part's transfers exactly its planned ones. That per-part
    check is what makes the list equal to what was sent. The two sum checks at the end restate it: no input
    reaches them while `parts` splits a week's plan without loss and `week_plan` splits each day's rows into
    payable and set aside without loss. They catch a change to either helper that loses an amount."""
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
    sent = {}
    for t in transfers:
        for a, v in t["sends"]:
            sent[a] = sent.get(a, 0) + v
    total, total_sent = sum(listed.values()), sum(sent.values())
    if total != total_sent or sent != listed:
        diff = sorted(a for a in set(sent) | set(listed) if sent.get(a, 0) != listed.get(a, 0))
        raise ValueError(f"the list sums to {total} udndr and the payout account sent {total_sent}; first address "
                         f"that differs: {diff[0] if diff else 'none'}")
    ranked = sum(d.total_paid for d in days)
    if total + sum(aside.values()) != ranked:
        raise ValueError(f"the list ({total}) and the amounts set aside ({sum(aside.values())}) do not add up to what "
                         f"the rankings paid ({ranked})")
    return {"schema": 1, "payer": payer, "season_end_height": end_height, "last_day": last,
            "paid_udndr": dict(sorted(listed.items())), "total_paid_udndr": total,
            "set_aside_udndr": dict(sorted(aside.items())), "total_set_aside_udndr": sum(aside.values()),
            "rankings_total_paid_udndr": ranked, "rankings_sha256": dict(sorted(digests.items())),
            "payments": payments}


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


_NOT_GIVEN = object()


def _season_list_main(a) -> int:
    if not (a.payer.startswith("dendra1") and not payable_address(a.payer)):
        print(f"Refused: --payer {a.payer!r} is not an account address; it is never defaulted.")
        return 2
    try:
        days, digests, end = season_days(a.rankings)
        C.require_index_from(C.rpc_url(a.node), a.start)
        doc = season_list(days, digests, end, payout_transfers(a.node, a.payer), a.doubled_from_day, a.payer)
    except (C.ChainUnreadable, RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
        print(f"Refused: {e}. No list is written.")
        return 3
    try:
        digest, written = write_stable(a.season_list, stable_bytes(doc))
    except OSError as e:
        print(f"Refused: {e}")
        return 3
    print(f"season list: days 0 to {doc['last_day']} (last block {end}), {len(doc['paid_udndr'])} addresses, "
          f"{doc['total_paid_udndr']} udndr, equal to what the payout account sent; "
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Final Testnet Season weekly payment")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--week", type=int)
    mode.add_argument("--season-list", metavar="FILE",
                      help="write the season's list address -> paid_udndr, checked against the payments on chain")
    mode.add_argument("--archive-rankings", metavar="DIR", help="copy every published ranking into an empty DIR")
    ap.add_argument("--payer", default="", help="the payout account's address (--season-list)")
    ap.add_argument("--rankings", required=True, help="directory or URL holding day-NNN.json")
    ap.add_argument("--node", default=os.environ.get("DENDRA_NODE", "tcp://chain:26657"))
    ap.add_argument("--chain-id", default=os.environ.get("DENDRA_CHAIN_ID", ""))
    ap.add_argument("--start", type=int, default=int(os.environ.get("DENDRA_FINAL_SEASON_START_HEIGHT", "1") or 1))
    ap.add_argument("--key", default="payout")
    ap.add_argument("--home", default=os.environ.get("DENDRA_HOME", "/root/.dendra"))
    ap.add_argument("--doubled-from-day", type=_doubled_from_arg, default=_NOT_GIVEN,
                    help="first day of the doubled rates as ADR-047 decision 22 fixed it, or none (required to pay "
                         "a week or write the season's list)")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.archive_rankings is not None:
        return _archive_main(a)
    if a.doubled_from_day is _NOT_GIVEN:
        ap.error("--doubled-from-day is required: a day number, or none")
    if a.season_list is not None:
        return _season_list_main(a)
    payable, aside, wd = plan_days(a.week, a.rankings, a.doubled_from_day)
    digest = plan_digest(payable)
    print(f"week {a.week} (days {wd}), plan {digest}: {len(payable)} addresses, "
          f"{sum(payable.values())} udndr in {len(parts(payable))} part(s)")
    for addr, v in payable.items():
        print(f"  {addr}  {v}")
    for addr, v in aside.items():
        print(f"  set aside (unpayable: {payable_address(addr)})  {addr}  {v}")
    if not a.yes:
        print("plan only. Nothing signed. Add --yes to pay.")
        return 0
    if not a.chain_id:
        print("Refused: --chain-id (or DENDRA_CHAIN_ID) is required to sign; it is never defaulted.")
        return 2
    out = subprocess.run(["dendrad", "keys", "show", a.key, "-a", "--keyring-backend", "test", "--home", a.home],
                         capture_output=True, text=True, timeout=30)
    payer = out.stdout.strip()
    if out.returncode != 0 or not payer.startswith("dendra1"):
        print(f"Refused: key {a.key!r} not found in {a.home} ({out.stderr.strip()[:160]})")
        return 2
    try:
        C.require_index_from(C.rpc_url(a.node), a.start)
    except C.ChainUnreadable as e:
        print(f"REFUSED: {e}")
        return 2
    verdict, which = decide(payable, paid_parts(a.node, payer, a.week))
    if verdict == "done":
        print(f"week {a.week} is already paid in full under plan {digest}: nothing sent.")
        return 0
    if verdict == "stop":
        print(f"Refused: part(s) {which} of week {a.week} are on chain under another plan. The rankings "
              f"changed after a partial payment; decide by hand what is owed before paying anything.")
        return 3
    chunks = parts(payable)
    for i in which:
        h = send_part(chunks[i], a.week, digest, i, a.key, payer, a.chain_id, a.node, a.home)
        print(f"  part {i}: {len(chunks[i])} transfers, tx {h}", flush=True)
    print(f"week {a.week} paid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
