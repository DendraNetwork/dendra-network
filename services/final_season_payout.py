#!/usr/bin/env python3
"""final_season_payout.py — the Final Testnet Season's weekly payment (ADR-047), from the payout account.

    python3 final_season_payout.py --week 0 --rankings <dir or URL>                 # plan only: who gets what
    python3 final_season_payout.py --week 0 --rankings <dir or URL> --yes           # sign and broadcast

Week k pays the days 7k .. 7k+6, the last week stopping at the season's last day (the ranking that carries
`inputs.season_end_height`: the season ends at a fixed instant, so how many days it has is read, never set).
Each day must already be ranked: a week with a missing day is refused, never paid in part, because a later "top-up" would be a
second payment for the same week that nothing distinguishes from a double payment.

THE CHAIN IS THE LEDGER, PART BY PART
The week is split into deterministic parts of up to `BATCH` transfers. Every transaction carries the memo
`dendra-final-season-week-<k>-<plan>-part-<i>`, where <plan> is a digest of the week's payable list. Before
anything is signed, the chain is searched for the payout account's transactions carrying that week:
  - a part already on chain is never sent again;
  - parts that are missing are sent, so a run interrupted halfway is FINISHED by running it again;
  - a part on chain under a DIFFERENT plan digest stops everything: the rankings changed after a partial
    payment, and only a person can decide what is owed.
A local file could be lost or copied; the chain is the record both the payer and the paid can read. And
the node it is read from must keep the whole history (`require_index_from`): a node bootstrapped by state
sync answers "no earlier payment" for weeks it simply never saw.

AN UNPAYABLE ADDRESS IS SET ASIDE, NOT SENT
One module account in a batch fails the whole transaction in the block (see `final_season_address`). Such
addresses are listed, with their amounts, and left out of every part; nothing else is held up by them.

THE RANKINGS ARE CHECKED AGAINST THE RULES BEFORE ANYTHING IS PLANNED
They come from the internet-facing service, and this script holds the key that pays. Recomputing a day
needs the chain's full index and the evidence the same service serves, so it is not redone here; what IS
checked is everything the published figures must satisfy given the facts each row publishes
(`check_days`): the rules fingerprint, each row's gross as the formula on its own counts, its payable (the
gross, or 0 without an address), 0 <= paid <= payable, the totals, the day budget derived from what the
earlier days paid, the pro-rata, and the season cap. One day out of line refuses the whole week. A
compromised service can still publish false counts and misdirect what the day budget allows — these
checks bound the loss, they do not prove the facts — but it can no longer make this script pay past the
day's budget or past what the formula gives on the counts it published.
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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import final_season_chain as C  # noqa: E402
from final_season_address import payable_address  # noqa: E402
from final_season_calc import DayResult, Identity, Row, gross_of, week_totals  # noqa: E402
from final_season_rules import RULES, fingerprint  # noqa: E402

BATCH = 100
GAS_PER_SEND = 60_000
_MEMO = re.compile(r"^dendra-final-season-week-(\d+)-([0-9a-f]{12})-part-(\d+)$")


def plan_digest(payable: dict) -> str:
    return hashlib.sha256(json.dumps(sorted(payable.items()), separators=(",", ":")).encode()).hexdigest()[:12]


def memo(week: int, digest: str, part: int) -> str:
    return f"dendra-final-season-week-{week}-{digest}-part-{part}"


def week_days(week: int, last_day: int | None = None) -> list:
    """The days week `week` pays: 7k .. 7k+6, cut after the season's last day once it is known (the
    ranking of that day carries `inputs.season_end_height`). A week outside the season is REFUSED, never
    returned empty: an empty plan reads as "already paid in full" and would exit 0 having paid nothing."""
    if type(week) is not int or week < 0:
        raise SystemExit(f"week {week!r} is not a season week (weeks start at 0)")
    days = list(range(7 * week, 7 * week + 7))
    if last_day is not None:
        days = [d for d in days if d <= last_day]
        if not days:
            raise SystemExit(f"week {week} starts after the season's last day ({last_day}): there is nothing to pay")
    return days


def _read(src: str, name: str) -> dict:
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(f"{src.rstrip('/')}/{name}", timeout=30) as r:
            return json.loads(r.read())
    with open(os.path.join(src, name), encoding="utf-8") as f:
        return json.load(f)


def load_day_end(src: str, day: int) -> tuple[DayResult, int | None]:
    """A published ranking (`final_season_calc.to_json`) read back, with the season's last block when this
    day holds it (`inputs.season_end_height`, `final_season_rank.rank_day`). A missing field raises: never
    defaulted — a ranking that does not say whether it is the last day cannot be read as "not the last"."""
    d = _read(src, f"day-{day:03d}.json")
    rows = [Row(x["miner_id"], x["payout_address"], x["presence"], x["verified_requests"],
                x["verdicts"], x["gross_udndr"], x["payable_udndr"], x["paid_udndr"], x["reason"])
            for x in d["ranking"]]
    end, last = d["inputs"]["season_end_height"], d["inputs"]["last_height"]
    # Bounded from BELOW and tied to the day: the ranking that holds the end names it as its own last
    # counted block (`final_season_rank.rank_day`). A 0, or a height that is not this day's last block,
    # would end the season early and refuse every later week.
    if end is not None and (type(end) is not int or type(last) is not int or not 0 < end == last):
        raise ValueError(f"day {day}: season_end_height {end!r} is not this day's last block ({last!r})")
    return DayResult(d["day"], d["rules_fingerprint"], d["total_gross_udndr"], d["total_payable_udndr"],
                     d["day_budget_udndr"], d["total_paid_udndr"], d["pro_rata"][0], d["pro_rata"][1], rows), end


def load_day(src: str, day: int) -> DayResult:
    return load_day_end(src, day)[0]


def _whole(v) -> bool:
    return type(v) is int          # not a float, a string or a bool: a figure that pays is an integer


def check_days(days: list) -> str:
    """'' when the published days 0..N (in order) satisfy the rules, else the first violation, in words.
    Everything here follows from the figures themselves and the rules: no fact is trusted, none is needed."""
    r, fp, paid_before = RULES, fingerprint(), 0
    for n, d in enumerate(days):
        at = f"day {n}"
        if d.day != n:
            return f"{at}: the file says it is day {d.day!r}"
        if d.rules_fingerprint != fp:
            return f"{at}: ranked under rules {str(d.rules_fingerprint)[:12]}, these are {fp[:12]}"
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
            gross = gross_of(Identity(row.miner_id, *counts, row.payout_address), r)
            if row.gross != gross:
                return f"{at}: {row.miner_id!r} gross {row.gross}, but the formula on its counts gives {gross}"
            payable = gross if row.payout_address else 0
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


def plan(week: int, src: str) -> tuple[dict, dict]:
    """(payable, set_aside): {address: udndr} each. Refuses a week with a day not yet ranked, and a week
    whose rankings — or those of any earlier day, which set its budget — break the rules."""
    payable, aside, _ = plan_days(week, src)
    return payable, aside


def plan_days(week: int, src: str) -> tuple[dict, dict, list]:
    """`plan`, with the days the week pays. The days are read in order from day 0, and the reading stops
    at the season's last day (the ranking that carries `season_end_height`): the week that holds the end
    pays up to it, and cannot be planned before it is ranked; a week after it is refused."""
    week_days(week)                       # refuses a negative or malformed week before any read
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
    bad = check_days(loaded)
    if bad:
        raise SystemExit(f"REFUSED: {bad}. Week {week} is not paid; nothing is signed.")
    days = loaded[wd[0]:wd[-1] + 1]
    try:
        totals = week_totals(days)
    except ValueError as e:
        raise SystemExit(f"REFUSED: {e}. Week {week} is not paid; nothing is signed.") from e
    payable, aside = {}, {}
    for a, v in totals.items():
        (aside if payable_address(a) else payable)[a] = v
    return payable, aside, wd


def parts(payable: dict) -> list:
    items = sorted(payable.items())
    return [items[i:i + BATCH] for i in range(0, len(items), BATCH)]


def _cli(args, node, timeout=120):
    out = subprocess.run(["dendrad", *args, "--node", node], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def paid_parts(node: str, payer: str, week: int) -> dict:
    """{part: (plan digest, txhash)} of the week's parts already on chain, from the payout account."""
    q = f"message.sender='{payer}'"
    found, page = {}, 1
    while True:
        d = json.loads(_cli(["query", "txs", "--query", q, "--page", str(page), "--limit", "100", "-o", "json"], node))
        for t in d.get("txs") or []:
            if int(t.get("code", 0) or 0) != 0:
                continue
            m = _MEMO.match(((t.get("tx") or {}).get("body") or {}).get("memo", ""))
            if m and int(m.group(1)) == week:
                found[int(m.group(3))] = (m.group(2), t.get("txhash", ""))
        if page * 100 >= int(d.get("total_count", 0) or 0) or not d.get("txs"):
            return found
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Final Testnet Season weekly payment")
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--rankings", required=True, help="directory or URL holding day-NNN.json")
    ap.add_argument("--node", default=os.environ.get("DENDRA_NODE", "tcp://chain:26657"))
    ap.add_argument("--chain-id", default=os.environ.get("DENDRA_CHAIN_ID", ""))
    ap.add_argument("--start", type=int, default=int(os.environ.get("DENDRA_FINAL_SEASON_START_HEIGHT", "1") or 1))
    ap.add_argument("--key", default="payout")
    ap.add_argument("--home", default=os.environ.get("DENDRA_HOME", "/root/.dendra"))
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    payable, aside, wd = plan_days(a.week, a.rankings)
    digest = plan_digest(payable)
    print(f"week {a.week} (days {wd}), plan {digest}: {len(payable)} addresses, "
          f"{sum(payable.values())} udndr in {len(parts(payable))} part(s)")
    for addr, v in payable.items():
        print(f"  {addr}  {v}")
    for addr, v in aside.items():
        print(f"  SET ASIDE (unpayable: {payable_address(addr)})  {addr}  {v}")
    if not a.yes:
        print("plan only. Nothing signed. Add --yes to pay.")
        return 0
    if not a.chain_id:
        print("REFUSED: --chain-id (or DENDRA_CHAIN_ID) is required to sign; it is never defaulted.")
        return 2
    out = subprocess.run(["dendrad", "keys", "show", a.key, "-a", "--keyring-backend", "test", "--home", a.home],
                         capture_output=True, text=True, timeout=30)
    payer = out.stdout.strip()
    if out.returncode != 0 or not payer.startswith("dendra1"):
        print(f"REFUSED: key {a.key!r} not found in {a.home} ({out.stderr.strip()[:160]})")
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
        print(f"REFUSED: part(s) {which} of week {a.week} are on chain under another plan. The rankings "
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
