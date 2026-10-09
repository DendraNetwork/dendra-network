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
unwind bound, the audit deadline and a margin (`finality_blocks`, the finality rule of the day's set), every job
settled that day has its last state. Asked earlier, the script refuses.
The season ends at a fixed instant read from the block headers (`final_season_rules.RULES["end_time"]`): the
day that holds the end counts only the blocks before it, and a day after it is refused (`season_window`).

A JUROR IS PAID ONLY FOR A VERDICT COMMITTED BEFORE ITS AUDIT WAS RESOLVED. The chain accepts a verdict commit
after the resolution, so each verdict is dated by its commit transaction and each audit by the block that
resolved it (final_season_facts.timely_verdicts); the ranking publishes, under `inputs.verdicts`, the verdicts
of the day's audited programme jobs committed in the day's window, with the heights that decided each one and
why one did not count.

EACH DAY IS RANKED UNDER THE RULES IN FORCE THAT DAY (`final_season_rules.rules_for_day`). Decision 18 pays the
work of an UNWOUND audit -- answered, and not graded incoherent: its own answer's grade when it has one, else its
miner's day by the void rule -- from the first day it applies to, which the service fixed once when it started
and publishes in its status (`unwound_audit_work_from_day`); this script reads it there, or from
`--unwound-from`. A day under decision 18 carries its fingerprint and publishes, under
`inputs.unwound_audit_work`, the rule, that first day and every unwound programme job of the day, paid or not,
with why (final_season_facts.unwound_work). A day before it is recomputed under the rules before it, finality
included (`final_season_rules.finality_rule`): on a day with no concluded audit of a programme job, exactly as
it was published. ONE EXCEPTION, SAID: on a day ranked by the service as it ran before decision 18 that holds a
concluded audit of a programme job, the verdicts are dated here (F3) and they were not there, so `--compare`
reports a difference, in `inputs.verdicts`.
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
from final_season_facts import (NOT_DATED, answered_jobs, audit_outcome, chain_day_facts, day_identities,
                                timely_verdicts, unwound_programme_jobs, unwound_work)
from final_season_rules import (FINALITY_17, day_bounds, end_epoch, finality_rule, pays_unwound_work,
                                rules_for_day)

FINALITY_MARGIN = 200


def finality_blocks(params: dict, rules: dict) -> int:
    """Blocks past a day's last counted block after which every audit of a job settled that day has
    RESOLVED: `audit_unwind_blocks` + `audit_resolve_timeout` + a margin, for a day ranked under the rules in force
    (`final_season_rules.FINALITY`). `rules` are the day's set (`rules_for_day`), required: a day ranked under
    RULES_17 was final at `audit_unwind_blocks` + the margin (`FINALITY_17`), as the service published every such
    day, and is recomputed at that same height -- with one rule for every day, a day published before decision 18
    would never recompute identically (its `finality_blocks` and the window of its verdicts would differ).

    WHY BOTH. The unwind bound is not checked at every block: it is checked when a deferred step comes due
    again (`audit_unwind.go::unwindStrandedRetention`, called from `resolveDisputedAudit`,
    `deferAuditForSmallJury` and `runOptimisticAudit`). A drawn audit's resolution comes due every
    `audit_resolve_timeout` blocks after its draw (a deferred draw comes due every
    `audit_sampling.go::auditDeferStride` blocks, a constant the margin covers). So the last audit of a day can resolve -- by its jury or by the unwind -- up to
    `audit_resolve_timeout` blocks past settlement + `audit_unwind_blocks`. With the bound plus a margin alone (200 < 240 on the public
    chain), a job settled in the day's last blocks, drawn late, could still be open at the final height: the
    ranking would pay it nothing, and a recompute after its resolution would differ.
    0 means a bound is NOT armed: no unwind, or no deadline at all (a drawn audit is then never tallied
    except by an adjudication), so no day can be called final and the script refuses instead of guessing."""
    rule = finality_rule(rules)
    unwind = C.param_int(params, "audit_unwind_blocks")
    if unwind <= 0:
        raise C.ChainUnreadable("audit_unwind_blocks is 0 on this chain: no day can be declared final")
    if rule == FINALITY_17:
        return unwind + FINALITY_MARGIN
    timeout = C.param_int(params, "audit_resolve_timeout")
    if timeout <= 0:
        raise C.ChainUnreadable("audit_resolve_timeout is 0 on this chain: a drawn audit has no deadline, so "
                                "no day can be declared final")
    return unwind + timeout + FINALITY_MARGIN


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
             finality: int, current_height: int, epoch_len: int, *, unwound_from) -> dict:
    """The ranking of `day`. `unwound_from`: the first day decision 18 applies to, or None when it applies to no
    day -- required, with no default: a default would choose, in silence, which work a day pays."""
    rules = rules_for_day(day, unwound_from)
    rpc = C.rpc_url(node)
    first, last, season_end = season_window(rpc, day, start, current_height, finality)
    # The searches below answer "nothing" on a node whose index does not reach this day: refused first.
    C.require_index_from(rpc, first)
    jobs = C.jobs(node)
    settled = C.settle_heights(node)
    # Only the programme's own jobs pay a juror (final_season_facts): the juries of other audits are not read.
    audited = [j for j in jobs if j.get("client") == generator and audit_outcome(j) is not None
               and first <= settled.get(j["id"], -1) <= last]
    committees, drawn_at = {}, {}
    for j in audited:
        drawn_at[j["id"]], members = C.audit_draw(rpc, j["id"])
        # An audit that concluded by quorum had a drawn jury: finding none means the index lost it, not
        # that nobody judged. Refused, never ranked with every verdict silently at zero.
        if not members and "quorum" in (j["state"] or ""):
            raise C.ChainUnreadable(f"the audit of {j['id']} concluded by quorum but its draw is not in "
                                    f"this node's block index")
        committees[j["id"]] = members
    commits = C.verdict_commits(node, audited, committees)
    # F3 — A VERDICT COUNTS ONLY IF IT WAS COMMITTED BEFORE THE AUDIT WAS RESOLVED. The chain accepts a verdict
    # after the resolution, when the outcome is public, and this day is ranked more than a day later: read
    # NOW, a drawn juror who never voted could post the winning vote in between and be paid. Each verdict is
    # dated by its commit transaction, each audit by the block that resolved it. The commits are searched in
    # the DAY'S WINDOW, from the draw to the day's final block (`last` + `finality`, which this ranking already
    # requires the chain to have passed): fixed by the day, not by the moment it is ranked, so a recompute
    # finds the same proof (final_season_facts.timely_verdicts).
    window_end = last + finality
    resolutions = {jid: C.audit_resolution(node, rpc, jid) for jid in commits}

    def dated(jid, m, c):
        # The search is by SIGNER: a juror who anchors more commits in the window than the search reads (they
        # need no job, at zero gas) gets THIS verdict not dated, listed and not counted. Raised up to here, it
        # deferred the day, and every later day with it (they are ranked in order).
        if not c["creator"] or drawn_at[jid] is None:
            return None
        try:
            return C.commit_height(node, f"{jid}__verdict__{m}", c["creator"], drawn_at[jid], window_end)
        except C.SearchTooLong:
            return NOT_DATED

    committed = {jid: {m: dated(jid, m, c) for m, c in v.items()} for jid, v in commits.items()}
    votes, verdict_proof = timely_verdicts(commits, resolutions, committed)
    voters = {j["id"]: j["audit_voters"] for j in audited}      # C.jobs reads it (proto3 zero included)
    for jid, p in verdict_proof.items():
        p["chain_counted_voters"] = voters[jid]
        p["searched_from"], p["searched_to"] = drawn_at[jid], window_end
    miners = C.miners(node)
    answered = answered_jobs(records_upto)
    facts = chain_day_facts(day, start, jobs, settled, votes, generator, finality, current_height,
                            answered, rules, last_height=last)
    unwound_proof = None
    if pays_unwound_work(rules):
        # Decision 18: an unwound audit's job, answered and not graded incoherent, is one more verified request of
        # its miner -- added BEFORE the day's identities are built, so it also counts as the day's verified work
        # that presence needs, and the grading that voids a day's work applies to it as to the rest.
        unwound = unwound_programme_jobs(day, start, jobs, settled, generator, finality, current_height, rules,
                                         last_height=last)
        paid_unwound, unwound_proof = unwound_work(day, records_upto, unwound, answered)
        for m, n in paid_unwound.items():
            facts["work"][m] = facts["work"].get(m, 0) + n
    facts["presence"], proven_by = C.presence_proofs(node, first, last, epoch_len)
    # The default payout address: the operator that signed the identity's proofs THAT DAY, a fact the
    # chain keeps after the miner has left; the live registry only for an identity with no proof that day
    # (a newcomer still in its grace, or a juror only), which a recompute after its exit cannot match.
    default = {m["id"]: m["operator"] for m in miners}
    default.update(proven_by)
    rows = day_identities(day, records_upto, facts, default)
    out = to_json(compute_day(day, rows, paid_before, rules))
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
    if verdict_proof:
        # Every drawn juror's verdict read for the day's programme audits, counted or not, with the heights
        # that decided it and the reason it was not counted (final_season_facts.timely_verdicts), beside the
        # number of votes the chain's own tally counted. Present only on a day that has one: a day without
        # an audited programme job keeps the shape it was published with.
        out["inputs"]["verdicts"] = verdict_proof
    if unwound_proof is not None:
        # On EVERY day under decision 18, an unwound job or not: the rule the day applied, the first day it
        # applies to, and each unwound programme job of the day, paid or not, with why. A day before decision 18
        # has no such field, and keeps the bytes it was published with.
        out["inputs"]["unwound_audit_work"] = {"rule": rules["unwound_audit_work"], "from_day": unwound_from,
                                               "jobs": unwound_proof}
    return out


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _records(base: str, day: int) -> list:
    """Every record of days 0..`day`, read EXACTLY as the service reads its own files
    (`final_season_evidence.Evidence.read`): a line that is not JSON -- the last line of a crash mid-write -- is
    skipped and counted in a `_unreadable_lines` record at the end of its day. The service ranks from that same
    list (its `evidence_sha256` covers it), and decision 18 reads it: an unreadable line may hold a seal or a
    grade. Read differently here, a recompute would refuse, or differ, on a day the service ranked."""
    out = []
    for d in range(day + 1):
        try:
            raw = _fetch(f"{base.rstrip('/')}/evidence/day-{d:03d}.jsonl")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue          # no record that day (nothing happened, or before the first answer)
            raise
        bad = 0
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                bad += 1
        if bad:
            out.append({"type": "_unreadable_lines", "count": bad, "day": d})
    return out


# The key of the service's status that names the first day decision 18 applies to (final_season_server._status).
UNWOUND_FROM_KEY = "unwound_audit_work_from_day"


def unwound_from_status(status: dict):
    """The first day decision 18 applies to, as the programme's status publishes it. THREE STATES: an int (the
    day), None when the status has no such key (a service that predates decision 18: it ranked every day under
    the rules before it), and a refusal for anything else -- a null or a malformed value is a service that does
    not know, never "no decision 18"."""
    if not isinstance(status, dict):
        raise ValueError("the programme's status is not an object")
    if UNWOUND_FROM_KEY not in status:
        return None
    v = status[UNWOUND_FROM_KEY]
    if type(v) is not int or v < 0:
        raise ValueError(f"the programme's status names {UNWOUND_FROM_KEY}={v!r}: not a day")
    return v


def _unwound_from_arg(s: str):
    """--unwound-from: a day, `none` (decision 18 applies to no day), or `status` (read from the programme's
    status, the default -- argparse converts a string default through this too)."""
    if s == "status":
        return s
    if s == "none":
        return None
    if not (s and all(c in "0123456789" for c in s)):
        raise argparse.ArgumentTypeError("a day number, or none")
    return int(s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", type=int, required=True)
    ap.add_argument("--evidence", required=True, help="base URL of the programme (…/final-season/v1/)")
    ap.add_argument("--node", default=C.default_node())
    ap.add_argument("--rest", default="", help="REST of the same node, for the params (default: derived)")
    ap.add_argument("--generator", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--compare", default="", help="URL of the published ranking of that day")
    ap.add_argument("--unwound-from", type=_unwound_from_arg, default="status",
                    help="first day decision 18 applies to, or none (default: read from the programme's status)")
    a = ap.parse_args(argv)
    rpc = C.rpc_url(a.node)
    rest = a.rest or rpc.rsplit(":", 1)[0] + ":1317"
    paid_before = 0
    for d in range(a.day):
        prev = json.loads(_fetch(f"{a.evidence.rstrip('/')}/ranking/day-{d:03d}.json"))
        paid_before += int(prev["total_paid_udndr"])
    params = C.jobs_params(rest)
    try:
        unwound_from = a.unwound_from
        if unwound_from == "status":
            unwound_from = unwound_from_status(json.loads(_fetch(f"{a.evidence.rstrip('/')}/status")))
        out = rank_day(a.day, _records(a.evidence, a.day), a.node, a.generator, a.start, paid_before,
                       finality_blocks(params, rules_for_day(a.day, unwound_from)), C.height(rpc),
                       epoch_blocks(params), unwound_from=unwound_from)
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
