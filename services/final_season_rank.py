#!/usr/bin/env python3
"""final_season_rank.py — compute, or RECOMPUTE, the Final Testnet Season ranking of a day (ADR-047).

The programme's service runs this to publish each day; anyone can run it to check what was published:

    python3 final_season_rank.py --day 3 --evidence https://testnet-api.dendranetwork.com/final-season/v1/ \\
        --node tcp://<a node>:26657 --generator <generator address> --start <start height> \\
        --compare https://testnet-api.dendranetwork.com/final-season/v1/ranking/day-003.json

It reads the evidence logs of days 0..N, the chain (jobs, settlements, verdicts, availability proofs, the
miner registry) and the published rankings of the days before (for what the season has already paid), applies
`final_season_calc`, and with `--compare` says whether the published ranking is the same, byte for byte.

A DAY IS RANKED ONCE IT IS FINAL, NEVER BEFORE: when the chain is past the day's last counted block by the
unwind bound plus a margin, every job settled that day has its last state. Asked earlier, the script refuses.
The season ends at a fixed instant read from the block headers (`final_season_rules.RULES["end_time"]`): the
day that holds the end counts only the blocks before it, and a day after it is refused (`season_window`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request

import final_season_chain as C
from final_season_calc import compute_day, to_json
from final_season_facts import answered_jobs, audit_outcome, chain_day_facts, day_identities
from final_season_rules import day_bounds, end_epoch

FINALITY_MARGIN = 200


def finality_blocks(params: dict) -> int:
    """The unwind bound of the chain plus a margin. 0 means the bound is NOT armed: then a deferred
    audit never resolves and no day can be called final, so the script refuses instead of guessing."""
    unwind = C.param_int(params, "audit_unwind_blocks")
    if unwind <= 0:
        raise C.ChainUnreadable("audit_unwind_blocks is 0 on this chain: no day can be declared final")
    return unwind + FINALITY_MARGIN


def epoch_blocks(params: dict) -> int:
    """The availability window length. 0 means the chain refuses every availability proof: presence
    cannot be read, and is refused rather than read as zero for everyone (`final_season_chain.presence_proofs`)."""
    eb = C.param_int(params, "avail_epoch_blocks")
    if eb <= 0:
        raise C.ChainUnreadable("avail_epoch_blocks is 0 on this chain: no presence can be proven or read")
    return eb


def season_window(rpc: str, day: int, start: int, current_height: int, finality: int,
                  end: int | None = None) -> tuple[int, int, int | None]:
    """(first, last, season_end): the blocks of `day` that the season counts, and the season's last block
    when this day holds it (None otherwise). Raises when the day is not final yet, or lies after the end.

    The season ends at a fixed instant read from the block headers (`final_season_rules.end_epoch`). A day
    is final once the chain is `finality` blocks past its LAST COUNTED block: past its full 17 280 blocks
    when the end comes later, past the season's last block when the end falls inside it — never past the
    full nominal day then, or the last day would wait for blocks that are not in the season.
    Read from the chain, so a recompute on any node of the network finds the same window."""
    end = end_epoch() if end is None else end
    first, nominal_last = day_bounds(day, start)
    probe = min(nominal_last, current_height - finality)
    if probe < first:
        raise ValueError(f"day {day} is not final yet (chain at {current_height})")
    if C.block_time(rpc, probe) < end:
        if probe < nominal_last:
            # Final exactly `finality` blocks past the season's last block, as the service closes it
            # (`final_season_server.State.day_closed`): `probe` IS the season's last block iff the next one
            # (it exists: probe < current) is at or after the end.
            if C.block_time(rpc, probe + 1) >= end:
                return first, probe, probe
            raise ValueError(f"day {day} is not final yet: block {probe} is still before the season's end "
                             f"and the day runs to block {nominal_last} (chain at {current_height})")
        # The whole day is in the season. It is the LAST day if the next block is already past the end:
        # read, so a season that ends exactly on a day boundary still names its last block.
        return first, nominal_last, (nominal_last if C.block_time(rpc, nominal_last + 1) >= end else None)
    if C.block_time(rpc, first) >= end:
        raise ValueError(f"day {day} is after the season (its first block {first} is at or after the end)")
    last = C.season_end_height(rpc, end, first, probe)
    return first, last, last


def rank_day(day: int, records_upto: list, node: str, generator: str, start: int, paid_before: int,
             finality: int, current_height: int, epoch_len: int) -> dict:
    rpc = C.rpc_url(node)
    first, last, season_end = season_window(rpc, day, start, current_height, finality)
    # The searches below answer "nothing" on a node whose index does not reach this day: refused first.
    C.require_index_from(rpc, first)
    jobs = C.jobs(node)
    settled = C.settle_heights(node)
    # Only the programme's own jobs pay a juror (final_season_facts): the juries of other audits are not read.
    audited = [j for j in jobs if j.get("client") == generator and audit_outcome(j) is not None
               and first <= settled.get(j["id"], -1) <= last]
    committees = {}
    for j in audited:
        members = C.audit_committee(rpc, j["id"])
        # An audit that concluded by quorum had a drawn jury: finding none means the index lost it, not
        # that nobody judged. Refused, never ranked with every verdict silently at zero.
        if not members and "quorum" in (j["state"] or ""):
            raise C.ChainUnreadable(f"the audit of {j['id']} concluded by quorum but its draw is not in "
                                    f"this node's block index")
        committees[j["id"]] = members
    votes = C.verdicts(node, audited, committees)
    miners = C.miners(node)
    facts = chain_day_facts(day, start, jobs, settled, votes, generator, finality, current_height,
                            answered_jobs(records_upto), last_height=last)
    facts["presence"], proven_by = C.presence_proofs(node, first, last, epoch_len)
    # The default payout address: the operator that signed the identity's proofs THAT DAY, a fact the
    # chain keeps after the miner has left; the live registry only for an identity with no proof that day
    # (a newcomer still in its grace, or a juror only), which a recompute after its exit cannot match.
    default = {m["id"]: m["operator"] for m in miners}
    default.update(proven_by)
    rows = day_identities(day, records_upto, facts, default)
    out = to_json(compute_day(day, rows, paid_before))
    out["inputs"] = {
        "evidence_sha256": hashlib.sha256(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records_upto).encode()).hexdigest(),
        "generator": generator, "start_height": start, "finality_blocks": finality,
        "avail_epoch_blocks": epoch_len, "paid_before_udndr": paid_before,
        # The blocks this day counted, and — on the season's last day only — its last block. The weekly
        # payment stops at the day that carries it (`final_season_payout.plan`); a recompute derives both
        # from its own node and compares, it never copies them from here.
        "last_height": last, "season_end_height": season_end,
    }
    return out


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _records(base: str, day: int) -> list:
    out = []
    for d in range(day + 1):
        try:
            raw = _fetch(f"{base.rstrip('/')}/evidence/day-{d:03d}.jsonl")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue          # no record that day (nothing happened, or before the first answer)
            raise
        out += [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", type=int, required=True)
    ap.add_argument("--evidence", required=True, help="base URL of the programme (…/final-season/v1/)")
    ap.add_argument("--node", default=C.default_node())
    ap.add_argument("--rest", default="", help="REST of the same node, for the params (default: derived)")
    ap.add_argument("--generator", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--compare", default="", help="URL of the published ranking of that day")
    a = ap.parse_args(argv)
    rpc = C.rpc_url(a.node)
    rest = a.rest or rpc.rsplit(":", 1)[0] + ":1317"
    paid_before = 0
    for d in range(a.day):
        prev = json.loads(_fetch(f"{a.evidence.rstrip('/')}/ranking/day-{d:03d}.json"))
        paid_before += int(prev["total_paid_udndr"])
    params = C.jobs_params(rest)
    try:
        out = rank_day(a.day, _records(a.evidence, a.day), a.node, a.generator, a.start, paid_before,
                       finality_blocks(params), C.height(rpc), epoch_blocks(params))
    except ValueError as e:
        # Not final yet, or a day after the season's end: said, never ranked as a full day.
        print(f"REFUSED: {e}")
        return 2
    mine = json.dumps(out, sort_keys=True, indent=1)
    if a.compare:
        pub = json.dumps(json.loads(_fetch(a.compare)), sort_keys=True, indent=1)
        same = pub == mine
        print("IDENTICAL to the published ranking" if same else "DIFFERS from the published ranking")
        if not same:
            sys.stdout.write(mine + "\n")
        return 0 if same else 1
    sys.stdout.write(mine + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
