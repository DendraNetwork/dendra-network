#!/usr/bin/env python3
"""final_season_rank.py — compute, or recompute, the Final Testnet Season ranking of a day.

The programme's service runs this to publish each day; anyone can run it to check what was published:

    python3 final_season_rank.py --day 3 --evidence https://testnet-api.dendranetwork.com/final-season/v1/ \\
        --node tcp://<a node>:26657 --generator <generator address> --start <start height> \\
        --compare https://testnet-api.dendranetwork.com/final-season/v1/ranking/day-003.json

It reads the evidence logs of days 0..N, the chain (jobs, settlements, verdicts, availability proofs, the
miner registry) and the published rankings of the days before (for what the season has already paid), applies
`final_season_calc`, and with `--compare` says whether the published ranking is the same, byte for byte.

A day is ranked once it is final, never before: when the chain is past the day's last counted block by the
unwind bound, the audit deadline and a margin (`finality_blocks`, the finality rule of the day's set), every job
settled that day has its last state. Asked earlier, the script refuses.
The season ends at a fixed instant read from the block headers (`final_season_rules.RULES["end_time"]`): the
day that holds the end counts only the blocks before it, and a day after it is refused (`season_window`).

A juror is paid only for a verdict committed before its audit was resolved. The chain accepts a verdict commit
after the resolution, so each verdict is dated by its commit transaction and each audit by the block that
resolved it (final_season_facts.timely_verdicts); the ranking publishes, under `inputs.verdicts`, the verdicts
of the day's audited programme jobs committed in the day's window, with the heights that decided each one and
why one did not count.

Each day is ranked under the rules in force that day (`final_season_rules.rules_for_day`). Decision 18 pays the
work of an unwound audit -- answered, and not graded incoherent: its own answer's grade when it has one, else its
miner's day by the void rule -- from the first day it applies to, which the service fixed once when it started
and publishes in its status (`unwound_audit_work_from_day`); this script reads it there, or from
`--unwound-from`. A day under decision 18 carries its fingerprint and publishes, under
`inputs.unwound_audit_work`, the rule, that first day and every unwound programme job of the day, paid or not,
with why (final_season_facts.unwound_work). A day before it is recomputed under the rules before it, finality
included (`final_season_rules.finality_rule`): on a day with no concluded audit of a programme job, exactly as
it was published. One exception, said: on a day ranked by the service as it ran before decision 18 that holds a
concluded audit of a programme job, the verdicts are dated here (F3) and they were not there, so `--compare`
reports a difference, in `inputs.verdicts`.
The doubled rates (`final_season_rules.RULES_DOUBLED`) apply from one day on, fixed by the service the same way
and published in its status (`doubled_rates_from_day`); this script reads it there, or from `--doubled-from`.
Decision 23 (`final_season_rules.RULES_MORE_REQUESTS`, more requests a day at the doubled rates) applies from one
day on as well, published as `more_requests_from_day`; this script reads it there, or from `--more-requests-from`.
Decision 24 (`final_season_rules.RULES_KIT`: the judge presence reward, the kit floor
and the probation of new identities) applies from the day published as `kit_rules_from_day` (`--kit-rules-from`),
with the floor the set carries (`--kit-min`, else DENDRA_KIT_MIN, else the status's `rules.kit_min` or the published
ranking's `inputs.kit_floor.kit_min`). A day under it publishes, under `inputs.judge_presence`, `inputs.kit_floor`
and `inputs.probation`, every input the counts alone do not give, the relay's arrival times included: a reader
without the relay's store replays those from the published ranking (`--compare` without `--relay-store`).
The jury set (`final_season_rules.RULES_JURY`) applies from the day published as `jury_rules_from_day`
(`--jury-rules-from`), with the fault periods and the season payer the set carries (DENDRA_FINAL_SEASON_FAULT_PERIODS and
DENDRA_FINAL_SEASON_FAULT_PAYER, else the status's `rules`, else the published ranking's `inputs.jury`). A day under it
is ranked only with the reveal of every fault period begun by then, read from `--fault-reveal`
(DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR) or replayed from the published ranking, and checked here against the commitment,
the seed, the memo on chain and the requests the seed draws, never copied: a reveal that does not match, that leaves a
drawn request out, or no reveal, refuses the day. Under the set, a relay store that cannot be read defers the day.
The season cap stays one total over every day, whatever set each day was ranked under.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

import final_season_chain as C
import final_season_rules as R
from final_season_calc import compute_day, to_json
from final_season_facts import (CAPACITY, FAULT_ABSENT, NOT_DATED, REGISTRATION_NO_OPERATOR,
                                REGISTRATION_SEARCH_TOO_LONG, REVEAL_KIND, STORE_UNREAD, answered_jobs, audit_outcome,
                                audit_unwound, chain_day_facts, check_fault_reveal, contradicted_jurors, day_identities,
                                drawn_not_listed, fault_contradictions, fault_reveal_published, first_full_day,
                                judge_presence, jury_verdicts, kit_report, read_fault_reveal, reveal_arrivals,
                                reveal_arrivals_published, timely_verdicts, unwound_programme_jobs, unwound_work)
from final_season_rules import (FINALITY_17, day_bounds, end_epoch, fault_memo, fault_period_of, finality_rule,
                                pays_judge_presence, pays_jury_rule, pays_unwound_work, rules_for_day,
                                started_fault_periods, with_jury_rules)

FINALITY_MARGIN = 200
# What the arrival times under `inputs.judge_presence.reveals` are: the season
# service's reading of the relay's store on the network's host. No public route serves these times, so a reader
# without the store replays them from the ranking, and a replay copies them, it does not verify them. Published
# under a fixed key so that the ranking says so itself, rather than its reader having to know the convention.
REVEALS_SOURCE = ("relay store, read by the season service on the network's host; no public route serves these "
                  "times, and a reader without the store replays them from this ranking without verifying them")


def finality_blocks(params: dict, rules: dict) -> int:
    """Blocks past a day's last counted block after which every audit of a job settled that day has
    resolved: `audit_unwind_blocks` + `audit_resolve_timeout` + a margin, for a day ranked under the rules in force
    (`final_season_rules.FINALITY`). `rules` are the day's set (`rules_for_day`), required: a day ranked under
    RULES_17 was final at `audit_unwind_blocks` + the margin (`FINALITY_17`), as the service published every such
    day, and is recomputed at that same height -- with one rule for every day, a day published before decision 18
    would never recompute identically (its `finality_blocks` and the window of its verdicts would differ).

    Why both. The unwind bound is not checked at every block: it is checked when a deferred step comes due
    again (`audit_unwind.go::unwindStrandedRetention`, called from `resolveDisputedAudit`,
    `deferAuditForSmallJury` and `runOptimisticAudit`). A drawn audit's resolution comes due every
    `audit_resolve_timeout` blocks after its draw (a deferred draw comes due every
    `audit_sampling.go::auditDeferStride` blocks, a constant the margin covers). So the last audit of a day can resolve (by its jury or by the unwind) up to
    `audit_resolve_timeout` blocks past settlement + `audit_unwind_blocks`. With the bound plus a margin alone (200 < 240 on the public
    chain), a job settled in the day's last blocks, drawn late, could still be open at the final height: the
    ranking would pay it nothing, and a recompute after its resolution would differ.
    0 means a bound is not armed: no unwind, or no deadline at all (a drawn audit is then never tallied
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
    is final once the chain is `finality` blocks past its last counted block: past its full 17 280 blocks
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
            # (`final_season_server.State.day_closed`): `probe` is the season's last block iff the next one
            # (it exists: probe < current) is at or after the end.
            if C.block_time(rpc, probe + 1) >= end:
                return first, probe, probe
            raise ValueError(f"day {day} is not final yet: block {probe} is still before the season's end "
                             f"and the day runs to block {nominal_last} (chain at {current_height})")
        # The whole day is in the season. It is the last day if the next block is already past the end:
        # read, so a season that ends exactly on a day boundary still names its last block.
        return first, nominal_last, (nominal_last if C.block_time(rpc, nominal_last + 1) >= end else None)
    if C.block_time(rpc, first) >= end:
        raise ValueError(f"day {day} is after the season (its first block {first} is at or after the end)")
    last = C.season_end_height(rpc, end, first, probe)
    return first, last, last


def read_reveals(store_root, keys: list) -> tuple:
    """(state, arrivals) from the relay's store on this host: `final_season_facts.reveal_arrivals` for `keys`, and
    the state the ranking publishes, "read" when the store's reveal directory could be listed, STORE_UNREAD when
    it could not or no store is named. The state is read once, apart from the keys, so a day with no seat still
    says whether the store was there."""
    readable = False
    if store_root:
        try:
            os.listdir(os.path.join(store_root, REVEAL_KIND))
            readable = True
        except OSError:
            readable = False
    return ("read" if readable else STORE_UNREAD), reveal_arrivals(store_root if readable else "", keys)


def published_reveals(ranking: dict, keys: list) -> tuple:
    """(state, arrivals) replayed from a published ranking (`inputs.judge_presence`): what a reader without the
    relay's store hands `rank_day` to recompute the day as the service ranked it. The state is the published one,
    STORE_UNREAD when the ranking carries none."""
    jp = (ranking.get("inputs") or {}).get("judge_presence") or {}
    state = jp.get("relay_store")
    return (state if state in ("read", STORE_UNREAD) else STORE_UNREAD), reveal_arrivals_published(ranking, keys)


_NOT_GIVEN = object()


def _jury_from(jury_rules_from):
    """The first day of the jury set a caller of `rank_day` names. Not named: None only while this process knows no
    fault commitment at all, since the set does not exist without one and no day can be under it; once one is known,
    not naming the day is refused, never read as "no day"."""
    if jury_rules_from is not _NOT_GIVEN:
        return jury_rules_from
    if R.RULES_JURY is None and R.FAULT_PERIODS is None and R.FAULT_PAYER is None:
        return None
    raise TypeError("rank_day() needs jury_rules_from once this process names fault commitments: a day "
                    "or None")


def _unwind_resolution(rpc: str, jid: str):
    """(height, END_BLOCK) of the block whose end unwound the audit of `jid`, a job whose state says it was unwound
    (`final_season_facts.audit_unwound`). The chain emits `audit_retention_unwound` at every unwind, so an index that
    holds none for such a job has lost it: raised, and the day waits, never ranked with the job's verdicts unpaid."""
    h = C.audit_unwind_height(rpc, jid)
    if h is None:
        raise C.ChainUnreadable(f"the audit of {jid} is unwound but its {C.UNWIND_EVENT} event is not in this node's "
                                f"block index")
    return h, C.END_BLOCK


def checked_faults(day: int, rules: dict, period: dict, start: int, node: str, fault_reveal_dir, replay_from) -> dict:
    """The reveal of the fault period `period`, begun by `day`, checked: read from
    `fault_reveal_dir`, or replayed from the published ranking `replay_from` when no directory is named; the document
    and the seed against the commitment (`final_season_facts.check_fault_reveal`), and the commitment's memo on chain,
    from the season payer to itself, below the period's first block. Raises ValueError when the reveal is absent,
    unreadable or does not match, and when the memo is absent or not below that block: the day is not ranked, and is
    never read as a day without faults."""
    a, b, c = period["from_day"], period["to_day"], period["commitment"]
    if replay_from is not None and not fault_reveal_dir:
        state, doc, reveal = fault_reveal_published(replay_from, c)
        where = "the published ranking"
    else:
        state, doc, reveal = read_fault_reveal(fault_reveal_dir, c)
        where = fault_reveal_dir or "no directory named (DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR)"
    if state == FAULT_ABSENT:
        raise ValueError(f"day {day} comes in or after the period of controlled-fault requests of days {a} to {b} "
                         f"(commitment {c}): it is ranked only with the period's reveal, which is not in {where} yet")
    if state != "read":
        raise ValueError(f"day {day} comes in or after the period of controlled-fault requests of days {a} to {b} "
                         f"(commitment {c}): its reveal cannot be read in {where}, and an unread reveal is never no "
                         f"fault")
    out = check_fault_reveal(period, doc, reveal)
    fb = day_bounds(a, start)[0]
    memo_h = C.fault_commitment_height(node, rules["fault_payer"], fault_memo(c))
    if memo_h is None:
        raise ValueError(f"day {day}: no accepted transfer of 1 udndr from the season payer {rules['fault_payer']} to "
                         f"itself carries the memo {fault_memo(c)}: the commitment of days {a} to {b} is not on chain")
    if not memo_h < fb:
        raise ValueError(f"day {day}: the memo of the commitment of days {a} to {b} is at height {memo_h}, not below "
                         f"block {fb}, the first of day {a}: it was not committed before the period")
    out.update(memo_height=memo_h, first_block=fb, document_bytes=doc, period=dict(period), commitment=c)
    return out


def rank_day(day: int, records_upto: list, node: str, generator: str, start: int, paid_before: int,
             finality: int, current_height: int, epoch_len: int, *, unwound_from, doubled_from,
             more_requests_from, kit_rules_from, relay_store=None, replay_from=None, jury_rules_from=_NOT_GIVEN,
             fault_reveal_dir=None) -> dict:
    """The ranking of `day`. `unwound_from`: the first day decision 18 applies to, `doubled_from`: the first day
    of the doubled rates (`final_season_rules.RULES_DOUBLED`), `more_requests_from`: the first day of decision 23
    (`final_season_rules.RULES_MORE_REQUESTS`), `kit_rules_from`: the first day of decision 24
    (`final_season_rules.RULES_KIT`), each None when it applies to no day -- all four required, with no default: a
    default would choose, in silence, which work a day pays, at what rate and under which fingerprint.
    `paid_before` is what every earlier day paid, under whatever set: the season cap is one total.
    Under decision 24 the relay's reveal records are read for the judge presence reward: from the store
    at `relay_store` (a directory on the network's host, read-only; `read_reveals`), or, with `replay_from`, from
    the arrival times that published ranking carries (`published_reveals`), which is how a reader without the
    store replays the day. Neither named, or a store that cannot be read: the arrivals are unknown, no judge
    reward is paid that day, and the ranking says so (`inputs.judge_presence.relay_store`); the other rewards are
    ranked as usual.
    `jury_rules_from`: the first day of the jury set (`final_season_rules.RULES_JURY`), or None; required
    once this process names a fault commitment (`_jury_from`). Under it a verdict is paid whatever its value, on the
    audits concluded or unwound, after the reveal sealed to its juror (`final_season_facts.jury_verdicts`), a "1" on
    an incoherent or unrevealed fault takes the juror's verdicts and judge day away
    (`final_season_facts.fault_contradictions`), and the identities of every fault period begun by the day are removed
    before `final_season_calc.compute_day` and published; the day needs the reveal of each of those periods
    (`checked_faults`), read from `fault_reveal_dir` or replayed from `replay_from`, complete for the day's requests
    (`final_season_facts.drawn_not_listed`). The judge presence reward keeps decision 24's seats, on concluded audits
    only (R8 of 23/10). A relay store that cannot be read, or an unwound audit whose unwind the index does not give,
    defers the day: under this set either would leave verdicts unpaid in a day that is never recomputed."""
    rules = rules_for_day(day, unwound_from, doubled_from, more_requests_from, kit_rules_from)
    jury_rules_from = _jury_from(jury_rules_from)
    rules = with_jury_rules(day, rules, kit_rules_from, jury_rules_from)
    kit = pays_judge_presence(rules)
    jury = pays_jury_rule(rules)
    rpc = C.rpc_url(node)
    first, last, season_end = season_window(rpc, day, start, current_height, finality)
    # The searches below answer "nothing" on a node whose index does not reach this day: refused first.
    C.require_index_from(rpc, first)
    if kit:
        # The registrations of the season are searched from its first block (`final_season_facts.first_full_day`):
        # an index that starts later would read every identity registered before it as older than the season.
        C.require_index_from(rpc, start)
        # The signed reports reach the ranking through the `capacity` records the service filed while it watched
        # the registry (`final_season_facts.kit_report`). None at or before the day's last block means it was not
        # watching during the day: every identity would read no signed report and be payable 0, and a published
        # day is never recomputed. Refused, so the day is deferred and said, never published at zero.
        if not any(r.get("type") == CAPACITY and type(r.get("height")) is int and 0 <= r["height"] <= last
                   for r in records_upto):
            raise C.ChainUnreadable(f"day {day} is under the kit rule set but the evidence holds no signed capacity "
                                    f"report filed at or before its last block {last}: the registry was not watched "
                                    f"during the day, so every identity would be paid nothing, and a published day "
                                    f"is never recomputed. The day is not ranked")
    # A day under the jury set is ranked only with the reveal of every fault period begun by then, each checked
    # before anything else is read, so that the identities of a period are left out of the pay from its first day on.
    # A day that no period holds while a later one is to come would pay that period's identities, secret until its
    # reveal: refused (the service and the command that fix the set's first day refuse periods that leave such a day).
    period = fault_period_of(rules, day) if jury else None
    begun = started_fault_periods(rules, day) if jury else []
    if jury and period is None and len(begun) < len(rules["fault_periods"]):
        raise ValueError(f"day {day} is under the jury set and no fault period holds it while a later one is to come: "
                         f"that period's identities are secret until its reveal and would be paid on this day")
    checked = [checked_faults(day, rules, p, start, node, fault_reveal_dir, replay_from) for p in begun]
    jobs = C.jobs(node)
    settled = C.settle_heights(node)

    def juried(j):
        # The audits whose jurors a day reads: concluded, and under the jury set unwound too.
        return audit_outcome(j) is not None or (jury and audit_unwound(j))
    # Only the programme's own jobs pay a juror (final_season_facts): the juries of other audits are not read.
    audited = [j for j in jobs if j.get("client") == generator and juried(j)
               and first <= settled.get(j["id"], -1) <= last]
    # Decision 24 only: the programme audits of the days before, within the finality span, whose jury this day
    # may have drawn (a deferred draw) or whose resolution may fall within this day's blocks (the judge presence reward's
    # conditions one and four). Concluded ones only, under the jury set too: they are read for the judge presence
    # reward alone, whose seats the jury set does not change (R8 of 23/10). A day under an earlier set reads none of them
    # and keeps its bytes.
    earlier = [j for j in jobs if j.get("client") == generator and audit_outcome(j) is not None
               and first - finality <= settled.get(j["id"], -1) < first] if kit else []
    # The requests the reveals list that settled in this day's window, each a programme request of the identity
    # the reveal names, as the chain records it (a reveal that names another miner or another client is refused); the
    # faulted ones are the day's faults. Every request of those identities settled in the window that a seed draws
    # must be listed, faulted or served honestly: one left out refuses the day, never read as no fault.
    trap_ids = set()
    listed = {}
    for chk in checked:
        trap_ids |= set(chk["traps"])
        for jid, e in chk["faults"].items():
            if jid in listed:
                raise ValueError(f"day {day}: job {jid} is listed by the reveals of two fault periods")
            listed[jid] = dict(e, commitment=chk["commitment"])
    by_job = {j["id"]: j for j in jobs}
    day_listed, day_faults = {}, {}
    for jid, e in sorted(listed.items()):
        h = settled.get(jid)
        if h is None or not first <= h <= last:
            continue
        j = by_job.get(jid) or {}
        if j.get("client") != generator or j.get("miner_id") != e["miner_id"]:
            raise ValueError(f"day {day}: the reveal names job {jid} as a programme request of {e['miner_id']}, and "
                             f"the chain records client {j.get('client')!r} and miner {j.get('miner_id')!r}")
        day_listed[jid] = e
        if e["faulted"]:
            day_faults[jid] = e["class"]
    for chk in checked:
        missing = drawn_not_listed(chk, jobs, settled, generator, first, last)
        if missing:
            jid, mid, cls = missing[0]
            raise ValueError(f"day {day}: the reveal of days {chk['period']['from_day']} to {chk['period']['to_day']} "
                             f"leaves out {len(missing)} request(s) settled this day that its seed draws, the first "
                             f"{jid} of {mid} (class {cls}): every drawn request is listed, faulted or served honestly")
    committees, drawn_at = {}, {}
    for j in audited + earlier:
        drawn_at[j["id"]], members = C.audit_draw(rpc, j["id"])
        # An audit that concluded by quorum had a drawn jury: finding none means the index lost it, not
        # that nobody judged. Refused, never ranked with every verdict silently at zero.
        if not members and "quorum" in (j["state"] or ""):
            raise C.ChainUnreadable(f"the audit of {j['id']} concluded by quorum but its draw is not in "
                                    f"this node's block index")
        committees[j["id"]] = members
    commits_all = C.verdict_commits(node, audited + earlier, committees)
    day_ids = {j["id"] for j in audited}
    commits = {jid: v for jid, v in commits_all.items() if jid in day_ids}
    # F3 — a verdict counts only if it was committed before the audit was resolved. The chain accepts a verdict
    # after the resolution, when the outcome is public, and this day is ranked more than a day later: read
    # now, a drawn juror who never voted could post the winning vote in between and be paid. Each verdict is
    # dated by its commit transaction, each audit by the block that resolved it. The commits are searched in
    # the day's window, from the draw to the day's final block (`last` + `finality`, which this ranking already
    # requires the chain to have passed): fixed by the day, not by the moment it is ranked, so a recompute
    # finds the same proof (final_season_facts.timely_verdicts).
    window_end = last + finality
    # Under the jury set an unwound audit is resolved by the block of its `audit_retention_unwound` event, at the end
    # of that block; every other audit as before.
    juried_by_id = {j["id"]: j for j in audited + earlier}
    resolutions = {jid: (_unwind_resolution(rpc, jid) if jury and audit_outcome(juried_by_id[jid]) is None
                         else C.audit_resolution(node, rpc, jid)) for jid in commits_all}

    def dated(jid, m, c):
        # The search is by signer: a juror who anchors more commits in the window than the search reads (they
        # need no job, at zero gas) gets this verdict not dated, listed and not counted. Raised up to here, it
        # deferred the day, and every later day with it (they are ranked in order).
        if not c["creator"] or drawn_at[jid] is None:
            return None
        try:
            return C.commit_height(node, f"{jid}__verdict__{m}", c["creator"], drawn_at[jid], window_end)
        except C.SearchTooLong:
            return NOT_DATED

    committed = {jid: {m: dated(jid, m, c) for m, c in v.items()} for jid, v in commits_all.items()}
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
        # its miner -- added before the day's identities are built, so it also counts as the day's verified work
        # that presence needs, and the grading that voids a day's work applies to it as to the rest.
        unwound = unwound_programme_jobs(day, start, jobs, settled, generator, finality, current_height, rules,
                                         last_height=last)
        paid_unwound, unwound_proof = unwound_work(day, records_upto, unwound, answered)
        for m, n in paid_unwound.items():
            facts["work"][m] = facts["work"].get(m, 0) + n
    judge_inputs = None
    if kit:
        # The five conditions of the judge presence reward, each read from a record the judge did not write alone. The
        # verdicts of the earlier audits are dated like the day's own (F3) and never enter `inputs.verdicts`,
        # which keeps the day's audits only, as before.
        votes_earlier, _ = timely_verdicts({jid: v for jid, v in commits_all.items() if jid not in day_ids},
                                           resolutions, committed)
        all_votes = dict(votes)
        all_votes.update(votes_earlier)
        by_id = {j["id"]: j for j in audited + earlier}
        seats, primaries = {}, {}
        for jid in sorted(committees):
            # Seats on concluded audits only, under the jury set too (R8 of 23/10): an unwound audit pays its timely
            # verdicts under that set (`jury_verdicts`), never a judge day. Under decision 24 alone, every audit read
            # here is concluded, and the day keeps its bytes.
            if audit_outcome(by_id[jid]) is None:
                continue
            if drawn_at[jid] is not None and first <= drawn_at[jid] <= last:
                primaries[jid] = by_id[jid].get("miner_id", "") or ""
                seats[jid] = sorted(m for m in committees[jid] if m != primaries[jid])
        keys = [f"{jid}__{m}__{primaries[jid]}" for jid in sorted(seats) for m in seats[jid]]
        # Under the jury set the reveals sealed to every juror read on the day's own audits are read too: a verdict is
        # paid only after its reveal, and a "1" on an unrevealed fault contradicts only a juror that had none.
        day_primaries = {jid: by_id[jid].get("miner_id", "") or "" for jid in sorted(day_ids)} if jury else {}
        jury_keys = sorted({f"{jid}__{m}__{day_primaries[jid]}" for jid, p in verdict_proof.items()
                            for m in p["jurors"]}) if jury else []
        seat_keys = set(keys)
        store_state, arrivals = (read_reveals(relay_store, keys + [k for k in jury_keys if k not in seat_keys])
                                 if replay_from is None
                                 else published_reveals(replay_from, keys + [k for k in jury_keys if k not in seat_keys]))
        if jury and replay_from is None and store_state == STORE_UNREAD:
            # Under the jury set the reveal's arrival is a condition of every verdict's pay: a store that cannot be read
            # would rank the day with no juror paid, and a published day is never recomputed. The day waits.
            raise C.ChainUnreadable(f"day {day} is under the jury set and the relay's store cannot be read "
                                    f"({relay_store or 'no store named'}): no verdict could be paid, so the day waits")
        commit_times = {}
        for jid in seats:
            for m in seats[jid]:
                if m in (all_votes.get(jid) or {}):
                    h = committed[jid][m]
                    if h not in commit_times:
                        commit_times[h] = C.block_time(rpc, h)
        for jid in (votes if jury else {}):
            for m in votes[jid]:
                h = committed[jid][m]
                if h not in commit_times:
                    commit_times[h] = C.block_time(rpc, h)
        resolved_in_day = {jid: r[0] for jid, r in sorted(resolutions.items())
                           if r is not None and first <= r[0] <= last}
        if jury:
            # Every timely verdict after its reveal is paid, whatever it says, and only a
            # "1" on an incoherent or unrevealed fault contradicts; a contradicted juror loses the day's verdicts here
            # and its judge day below. Nothing else contradicts under this set.
            jury_counts, jury_proof = jury_verdicts(votes, day_primaries, arrivals, committed, commit_times)
            contradicted, fault_votes = fault_contradictions(day_faults, verdict_proof, arrivals, day_primaries)
            facts["verdicts"] = {m: n for m, n in jury_counts.items() if m not in contradicted}
        else:
            contradicted = contradicted_jurors(resolved_in_day, commits_all, by_id)
        seated = sorted({m for members in seats.values() for m in members})
        reports = {m: kit_report(records_upto, m, last) for m in seated}
        paid_judges, judge_proof = judge_presence(seats, primaries, arrivals, all_votes, committed, commit_times,
                                                 contradicted, reports, rules["kit_min"])
        facts["judge_active"] = {m: 1 for m in paid_judges}
        judge_inputs = {
            "rate_udndr": rules["judge_active_per_day"], "from_day": kit_rules_from,
            # The service's reading of the relay's store: "read", or STORE_UNREAD when the store was not there, in
            # which case every arrival below is unknown and no judge reward is paid this day.
            "relay_store": store_state,
            "audits": {jid: {"drawn_at": drawn_at[jid], "primary": primaries[jid], "seats": seats[jid],
                             "resolved_at": None if resolutions.get(jid) is None else resolutions[jid][0]}
                       for jid in seats},
            # The arrival time the relay recorded for each reveal sealed to a seated juror, by key: a reading no
            # public route serves, published here, and what a reader replays (`published_reveals`). The ranking
            # says so itself (`reveals_source`): a replay copies these times, it does not verify them.
            "reveals": {k: arrivals[k] for k in keys}, "reveals_source": REVEALS_SOURCE,
            "resolved_this_day": resolved_in_day, "contradicted": sorted(contradicted),
            "identities": judge_proof,
        }
    facts["presence"], proven_by = C.presence_proofs(node, first, last, epoch_len)
    # The default payout address: the operator that signed the identity's proofs that day, a fact the
    # chain keeps after the miner has left; the live registry only for an identity with no proof that day
    # (a newcomer still in its grace, or a juror only), which a recompute after its exit cannot match.
    default = {m["id"]: m["operator"] for m in miners}
    default.update(proven_by)
    rows = day_identities(day, records_upto, facts, default)
    # The controlled-fault identities of every period begun by this day are removed before the
    # calculation, so they take no part of the cap and no pro rata, and are published with their facts below.
    trap_rows = [i for i in rows if i.miner_id in trap_ids]
    all_work = sum(i.verified_requests for i in rows)
    rows = [i for i in rows if i.miner_id not in trap_ids]
    # The exclusion holds from a period's first day. An identity registered before it was ranked as any miner while it was
    # secret: its registration is published (`fault_inputs`), measured, never a refusal -- a registration is history,
    # and refusing the day would stop the season without undoing a payment.
    trap_regs = {mid: _registration(node, mid, default, commits_all, start, last) for mid in sorted(trap_ids)}
    kit_inputs = probation_inputs = None
    if kit:
        # The latest signed capacity report at or before the day's end decides whether the
        # identity is paid at all; the work plan's Q7: the first full day after the registration pays 60 percent.
        # The registration is searched by its signer, the identity's operator (`final_season_chain.miner_registration`):
        # the registry's operator, else the signer of the identity's proofs that day, else the signer of its verdict
        # commits. An identity with none has no address to search by, and one whose own registrations in the season
        # outnumber what the search reads has flooded it: either is not dated, payable 0 for the day, and the
        # ranking says which (`inputs.probation.not_dated`); the day is ranked without it, never deferred by it.
        operators = dict(default)
        for v in commits_all.values():
            for m, c in v.items():
                if c.get("creator"):
                    operators.setdefault(m, c["creator"])
        reports_of, regs_of, firsts, not_dated = {}, {}, {}, {}
        for i in rows:
            rep = kit_report(records_upto, i.miner_id, last)
            i.kit_version = None if rep is None else rep["kit_version"]
            op = operators.get(i.miner_id)
            if not op:
                reg, not_dated[i.miner_id] = NOT_DATED, REGISTRATION_NO_OPERATOR
            else:
                try:
                    reg = C.miner_registration(node, i.miner_id, op, start, last)
                except C.SearchTooLong:
                    reg, not_dated[i.miner_id] = NOT_DATED, REGISTRATION_SEARCH_TOO_LONG
            ffd = first_full_day(reg, start, rules)
            i.probation = ffd == day
            i.registration_not_dated = reg == NOT_DATED
            reports_of[i.miner_id], regs_of[i.miner_id], firsts[i.miner_id] = rep, reg, ffd
        kit_inputs = {"kit_min": rules["kit_min"], "reports": reports_of}
        probation_inputs = {"bps": rules["probation_first_day_bps"], "registered_at": regs_of,
                            "first_full_day": firsts, "not_dated": not_dated}
    out = to_json(compute_day(day, rows, paid_before, rules))
    out["inputs"] = {
        "evidence_sha256": hashlib.sha256(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records_upto).encode()).hexdigest(),
        "generator": generator, "start_height": start, "finality_blocks": finality,
        "avail_epoch_blocks": epoch_len, "paid_before_udndr": paid_before,
        # The blocks this day counted, and on the season's last day only, its last block. The weekly
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
        # On every day under decision 18, an unwound job or not: the rule the day applied, the first day it
        # applies to, and each unwound programme job of the day, paid or not, with why. A day before decision 18
        # has no such field, and keeps the bytes it was published with.
        out["inputs"]["unwound_audit_work"] = {"rule": rules["unwound_audit_work"], "from_day": unwound_from,
                                               "jobs": unwound_proof}
    if kit:
        # On every day under decision 24: the judge presence reward's inputs (per identity),
        # the kit floor with each ranked identity's report, and the probation with each identity's registration.
        # A day before decision 24 has none of the three, and keeps the bytes it was published with.
        out["inputs"]["judge_presence"] = judge_inputs
        out["inputs"]["kit_floor"] = kit_inputs
        out["inputs"]["probation"] = probation_inputs
    if jury:
        # On every day under the jury set: the rule, its keys (a payer without them rebuilds the set from
        # here, `final_season_payout`), each verdict of the day's audits with why it was paid or not, the arrival
        # times read for them, and the contradicted jurors. A day before the set has none of this.
        out["inputs"]["jury"] = {
            "rule": rules["juror_rule"], "from_day": jury_rules_from, "kit_min": rules["kit_min"],
            "fault_payer": rules["fault_payer"], "fault_periods": [dict(p) for p in rules["fault_periods"]],
            "verdicts": jury_proof, "reveals": {k: arrivals[k] for k in jury_keys}, "reveals_source": REVEALS_SOURCE,
            "contradicted": sorted(contradicted),
            "faults": None if not checked else fault_inputs(checked, period, day_listed, fault_votes, trap_rows,
                                                            all_work, day_ids, trap_regs),
        }
    return out


# Whether a controlled-fault identity was registered before its period's first block, when that cannot be told: not
# dated, or no registration in the season and no fact that day (registered before the season, or not yet).
REGISTRATION_UNKNOWN = "unknown"


def _registration(node: str, mid: str, default: dict, commits_all: dict, start: int, last: int) -> tuple:
    """(registration height | None | NOT_DATED, why not dated | None) of identity `mid`, searched by its operator as
    the probation does (`final_season_chain.miner_registration`): the registry's operator, else the signer of its
    proofs that day, else the signer of its verdict commits."""
    op = default.get(mid)
    if not op:
        op = next((c["creator"] for v in commits_all.values() for m, c in sorted(v.items())
                   if m == mid and c.get("creator")), None)
    if not op:
        return NOT_DATED, REGISTRATION_NO_OPERATOR
    try:
        return C.miner_registration(node, mid, op, start, last), None
    except C.SearchTooLong:
        return NOT_DATED, REGISTRATION_SEARCH_TOO_LONG


def fault_inputs(checked: list, period, day_listed: dict, fault_votes: dict, trap_rows: list, all_work: int,
                 day_ids: set, trap_regs: dict) -> dict:
    """What a day under the jury set publishes of its fault periods: the period that holds
    the day, or None; for every period begun by the day, its commitment's memo height, its document and its seed in
    plain text, and each of its identities with its facts of the day and its registration; the identities' share of the
    programme's verified work; each listed request settled that day with its question, its answer, whether it was
    faulted, the key of its anchor (`<job>__<miner>`, against which anyone recomputes the answer's embedding) and, for
    a fault, the jurors' votes; and per class what the jury did. Enough for a reader to check the day against the
    commitments and the chain (`final_season_facts.fault_reveal_published`)."""
    facts_of = {i.miner_id: i for i in trap_rows}
    periods = []
    for chk in checked:
        traps = {}
        for mid, addr in sorted(chk["traps"].items()):
            i = facts_of.get(mid)
            reg, why = trap_regs[mid]
            if reg == NOT_DATED or (reg is None and i is None):
                before = REGISTRATION_UNKNOWN
            else:
                # No registration in the season while it has facts that day: registered before the season.
                before = reg is None or reg < chk["first_block"]
            traps[mid] = {"payout_address": addr, "verified_requests": 0 if i is None else i.verified_requests,
                          "verdicts": 0 if i is None else i.verdicts, "presence": 0 if i is None else i.presence,
                          "ranked": i is not None, "registered_at": reg, "registered_before_period": before}
            if why is not None:
                traps[mid]["registration_not_dated"] = why
        periods.append({"period": dict(chk["period"]), "commitment_memo_height": chk["memo_height"],
                        "period_first_block": chk["first_block"], "document": chk["document_bytes"].decode("utf-8"),
                        "seed": chk["seed"].hex(), "traps": traps})
    jobs, by_class, honest = {}, {}, 0
    for jid in sorted(day_listed):
        e = day_listed[jid]
        if not e["faulted"]:
            # Drawn and served honestly: listed with its answer, which anyone compares to its anchor; no fault, so no
            # vote of it is read here (they are under `inputs.verdicts`).
            honest += 1
            jobs[jid] = dict(e, audited=jid in day_ids, anchor_key=f"{jid}__{e['miner_id']}", votes=None)
            continue
        votes = (fault_votes.get(jid) or {}).get("votes") or {}
        jobs[jid] = dict(e, audited=jid in day_ids, anchor_key=f"{jid}__{e['miner_id']}", votes=votes)
        c = by_class.setdefault(e["class"], {"faults": 0, "audited": 0, "votes_1": 0, "votes_0": 0,
                                             "contradicted_votes": 0})
        c["faults"] += 1
        c["audited"] += 1 if jid in day_ids else 0
        c["votes_1"] += sum(1 for v in votes.values() if v["vote"] == "1")
        c["votes_0"] += sum(1 for v in votes.values() if v["vote"] == "0")
        c["contradicted_votes"] += sum(1 for v in votes.values() if v["contradicted"])
    return {"period": None if period is None else dict(period), "periods": periods,
            "work": {"traps": sum(i.verified_requests for i in trap_rows), "all": all_work},
            "jobs": jobs, "by_class": by_class, "drawn_served_honestly": honest,
            # The service holds no embedding engine: the anchor check is the reader's (judge_worker.py::anchor_reading).
            "anchor_check": "not computed by the season service; recompute the answer's embedding against the "
                            "commit at anchor_key"}


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _records(base: str, day: int) -> list:
    """Every record of days 0..`day`, read exactly as the service reads its own files
    (`final_season_evidence.Evidence.read`): a line that is not JSON (the last line of a crash mid-write) is
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
    """The first day decision 18 applies to, as the programme's status publishes it. Three states: an int (the
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


# The key of the service's status that names the first day of the doubled rates (final_season_server._status).
DOUBLED_FROM_KEY = "doubled_rates_from_day"


def doubled_from_status(status: dict):
    """The first day of the doubled rates, as the programme's status publishes it: an int (the day), None when
    the status names no day (null: the service fixed none) or has no such key (a service that predates the
    doubled rates: it ranked no day under them), and a refusal for anything else."""
    if not isinstance(status, dict):
        raise ValueError("the programme's status is not an object")
    v = status.get(DOUBLED_FROM_KEY)
    if v is None:
        return None
    if type(v) is not int or v < 0:
        raise ValueError(f"the programme's status names {DOUBLED_FROM_KEY}={v!r}: not a day")
    return v


# The key of the service's status that names the first day of decision 23 (final_season_server._status).
MORE_REQUESTS_FROM_KEY = "more_requests_from_day"


def more_requests_from_status(status: dict):
    """The first day of decision 23 (`final_season_rules.RULES_MORE_REQUESTS`), as the programme's status publishes
    it, read like `doubled_from_status`: an int (the day), None when the status names no day (null: the service
    fixed none) or has no such key (a service that predates decision 23: it ranked no day under it), and a refusal
    for anything else."""
    if not isinstance(status, dict):
        raise ValueError("the programme's status is not an object")
    v = status.get(MORE_REQUESTS_FROM_KEY)
    if v is None:
        return None
    if type(v) is not int or v < 0:
        raise ValueError(f"the programme's status names {MORE_REQUESTS_FROM_KEY}={v!r}: not a day")
    return v


# The key of the service's status that names the first day of decision 24 (final_season_server._status).
KIT_RULES_FROM_KEY = "kit_rules_from_day"


def kit_rules_from_status(status: dict):
    """The first day of decision 24 (`final_season_rules.RULES_KIT`), as the programme's status publishes it,
    read like `doubled_from_status`: an int (the day), None when the status names no day (null) or has no such
    key (a service that predates decision 24), and a refusal for anything else."""
    if not isinstance(status, dict):
        raise ValueError("the programme's status is not an object")
    v = status.get(KIT_RULES_FROM_KEY)
    if v is None:
        return None
    if type(v) is not int or v < 0:
        raise ValueError(f"the programme's status names {KIT_RULES_FROM_KEY}={v!r}: not a day")
    return v


# The key of the service's status that names the first day of the jury set (final_season_server._status).
JURY_RULES_FROM_KEY = "jury_rules_from_day"


def jury_rules_from_status(status: dict):
    """The first day of the jury set, as the programme's status publishes it, read like
    `kit_rules_from_status`: an int, None when the status names no day (null) or has no such key (a service that
    predates the set), and a refusal for anything else."""
    if not isinstance(status, dict):
        raise ValueError("the programme's status is not an object")
    v = status.get(JURY_RULES_FROM_KEY)
    if v is None:
        return None
    if type(v) is not int or v < 0:
        raise ValueError(f"the programme's status names {JURY_RULES_FROM_KEY}={v!r}: not a day")
    return v


def kit_min_from_status(status: dict):
    """The kit floor the status's current rules carry (`rules.kit_min`), or None when the status's day is not
    under decision 24: a reader with no floor of its own takes it from here to know the set. Anything but a
    positive integer there is refused."""
    if not isinstance(status, dict):
        raise ValueError("the programme's status is not an object")
    rules = status.get("rules")
    if not isinstance(rules, dict) or "kit_min" not in rules:
        return None
    v = rules["kit_min"]
    if type(v) is not int or v < 1:
        raise ValueError(f"the programme's status names rules.kit_min={v!r}: not a kit number")
    return v


def _unwound_from_arg(s: str):
    """--unwound-from, --doubled-from, --more-requests-from and --kit-rules-from: a day, `none` (the set applies
    to no day), or `status` (read from the programme's status, the default -- argparse converts a string default
    through this too)."""
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
    ap.add_argument("--doubled-from", type=_unwound_from_arg, default="status",
                    help="first day of the doubled rates, or none (default: read from the programme's status)")
    ap.add_argument("--more-requests-from", type=_unwound_from_arg, default="status",
                    help="first day of decision 23 (more requests a day), or none (default: read from the "
                         "programme's status)")
    ap.add_argument("--kit-rules-from", type=_unwound_from_arg, default="status",
                    help="first day of decision 24 (the kit rule set: judge presence reward, kit floor, probation), "
                         "or none (default: read from the programme's status)")
    ap.add_argument("--kit-min", type=int, default=None,
                    help="the kit floor of decision 24 (default: DENDRA_KIT_MIN, else the status's rules.kit_min)")
    ap.add_argument("--relay-store", default=os.environ.get("DENDRA_FINAL_SEASON_RELAY_STORE", ""),
                    help="the relay's store directory, for the reveal arrival times (default: "
                         "DENDRA_FINAL_SEASON_RELAY_STORE; without it and with --compare, the published arrivals "
                         "are replayed; without either, no judge reward is computed and the ranking says so)")
    ap.add_argument("--jury-rules-from", type=_unwound_from_arg, default="status",
                    help="first day of the jury set, or none (default: read from the programme's status)")
    ap.add_argument("--fault-reveal", default=os.environ.get("DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR", ""),
                    help="the directory of the fault periods' reveals (default: DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR; "
                         "without it and with --compare, the published reveal is checked again; without either, a day "
                         "of a fault period is refused)")
    a = ap.parse_args(argv)
    rpc = C.rpc_url(a.node)
    rest = a.rest or rpc.rsplit(":", 1)[0] + ":1317"
    paid_before = 0
    for d in range(a.day):
        prev = json.loads(_fetch(f"{a.evidence.rstrip('/')}/ranking/day-{d:03d}.json"))
        paid_before += int(prev["total_paid_udndr"])
    params = C.jobs_params(rest)
    published = json.loads(_fetch(a.compare)) if a.compare else None
    try:
        unwound_from, doubled_from, more_requests_from = a.unwound_from, a.doubled_from, a.more_requests_from
        kit_rules_from, jury_rules_from, status = a.kit_rules_from, a.jury_rules_from, None
        if "status" in (unwound_from, doubled_from, more_requests_from, kit_rules_from):
            status = json.loads(_fetch(f"{a.evidence.rstrip('/')}/status"))
            if unwound_from == "status":
                unwound_from = unwound_from_status(status)
            if doubled_from == "status":
                doubled_from = doubled_from_status(status)
            if more_requests_from == "status":
                more_requests_from = more_requests_from_status(status)
            if kit_rules_from == "status":
                kit_rules_from = kit_rules_from_status(status)
        if jury_rules_from == "status":
            # The jury set follows decision 24: a day before decision 24's first day, or with no such day, is under
            # no jury set whatever the status says (`final_season_rules.with_jury_rules`), so the status is read for
            # it only when it can decide.
            if kit_rules_from is None or a.day < kit_rules_from:
                jury_rules_from = None
            else:
                status = status if status is not None else json.loads(_fetch(f"{a.evidence.rstrip('/')}/status"))
                jury_rules_from = jury_rules_from_status(status)
        if a.kit_min is not None and R.KIT_MIN is not None and a.kit_min != R.KIT_MIN:
            # Said, never ignored: an option that lost to the environment in silence would recompute the day under
            # another floor, another fingerprint, and report a difference with no cause.
            raise ValueError(f"--kit-min {a.kit_min} names another kit floor than the one this process already "
                             f"carries ({R.KIT_MIN}, from {R.KIT_MIN_ENV}): a day is ranked under one floor, which its "
                             f"fingerprint names. Unset {R.KIT_MIN_ENV}, or pass the same number")
        if kit_rules_from is not None and a.day >= kit_rules_from and R.KIT_MIN is None:
            # The set does not exist without its floor: the option, else the published ranking under comparison,
            # else the status's current rules. None of them: `rules_for_day` refuses, and says what to set.
            kit_min = a.kit_min
            if kit_min is None and published is not None:
                kit_min = ((published.get("inputs") or {}).get("kit_floor") or {}).get("kit_min")
            if kit_min is None and status is not None:
                kit_min = kit_min_from_status(status)
            if kit_min is not None:
                R.set_kit_min(kit_min)
        if jury_rules_from is not None and a.day >= jury_rules_from and R.RULES_JURY is None:
            # The jury set does not exist without its periods and its payer: the environment, else the published
            # ranking's `inputs.jury`, else the status's current rules, each taken only when the fingerprint beside it
            # binds them (`final_season_rules.jury_set_named`). None of them: `with_jury_rules` refuses, and says so.
            if published is not None:
                R.jury_set_named((published.get("inputs") or {}).get("jury"), published.get("rules_fingerprint"))
            if R.RULES_JURY is None and status is not None:
                R.jury_set_named(status.get("rules"), status.get("rules_fingerprint"))
        rules = rules_for_day(a.day, unwound_from, doubled_from, more_requests_from, kit_rules_from)
        rules = with_jury_rules(a.day, rules, kit_rules_from, jury_rules_from)
        # No store on this machine and a published ranking in hand: its arrival times are replayed, the way
        # a third party replays the day (`published_reveals`).
        replay = published if (pays_judge_presence(rules) and not a.relay_store and published is not None) else None
        out = rank_day(a.day, _records(a.evidence, a.day), a.node, a.generator, a.start, paid_before,
                       finality_blocks(params, rules), C.height(rpc), epoch_blocks(params),
                       unwound_from=unwound_from, doubled_from=doubled_from, more_requests_from=more_requests_from,
                       kit_rules_from=kit_rules_from, relay_store=a.relay_store, replay_from=replay,
                       jury_rules_from=jury_rules_from, fault_reveal_dir=a.fault_reveal)
    except ValueError as e:
        # Not final yet, or a day after the season's end: said, never ranked as a full day.
        print(f"REFUSED: {e}")
        return 2
    mine = json.dumps(out, sort_keys=True, indent=1)
    if a.compare:
        pub = json.dumps(published, sort_keys=True, indent=1)
        same = pub == mine
        print("identical to the published ranking" if same else "differs from the published ranking")
        if not same:
            sys.stdout.write(mine + "\n")
        return 0 if same else 1
    sys.stdout.write(mine + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
