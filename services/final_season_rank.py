#!/usr/bin/env python3
"""final_season_rank.py — compute, or recompute, the Final Testnet Season ranking of a day (ADR-047).

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
Decision 24 (`final_season_rules.RULES_KIT`: the judge presence reward of ADR-060, the kit floor of ADR-058 decision
5 and the probation of new identities) applies from the day published as `kit_rules_from_day` (`--kit-rules-from`),
with the floor the set carries (`--kit-min`, else DENDRA_KIT_MIN, else the status's `rules.kit_min` or the published
ranking's `inputs.kit_floor.kit_min`). A day under it publishes, under `inputs.judge_presence`, `inputs.kit_floor`
and `inputs.probation`, every input the counts alone do not give, the relay's arrival times included: a reader
without the relay's store replays those from the published ranking (`--compare` without `--relay-store`).
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
from final_season_facts import (CAPACITY, NOT_DATED, REGISTRATION_NO_OPERATOR, REGISTRATION_SEARCH_TOO_LONG,
                                REVEAL_KIND, STORE_UNREAD, answered_jobs, audit_outcome, chain_day_facts,
                                contradicted_jurors, day_identities, first_full_day, judge_presence, kit_report,
                                reveal_arrivals, reveal_arrivals_published, timely_verdicts,
                                unwound_programme_jobs, unwound_work)
from final_season_rules import (FINALITY_17, day_bounds, end_epoch, finality_rule, pays_judge_presence,
                                pays_unwound_work, rules_for_day)

FINALITY_MARGIN = 200
# What the arrival times under `inputs.judge_presence.reveals` are (ADR-060 decision 2, last sentence): the season
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


def rank_day(day: int, records_upto: list, node: str, generator: str, start: int, paid_before: int,
             finality: int, current_height: int, epoch_len: int, *, unwound_from, doubled_from,
             more_requests_from, kit_rules_from, relay_store=None, replay_from=None) -> dict:
    """The ranking of `day`. `unwound_from`: the first day decision 18 applies to, `doubled_from`: the first day
    of the doubled rates (`final_season_rules.RULES_DOUBLED`), `more_requests_from`: the first day of decision 23
    (`final_season_rules.RULES_MORE_REQUESTS`), `kit_rules_from`: the first day of decision 24
    (`final_season_rules.RULES_KIT`), each None when it applies to no day -- all four required, with no default: a
    default would choose, in silence, which work a day pays, at what rate and under which fingerprint.
    `paid_before` is what every earlier day paid, under whatever set: the season cap is one total.
    Under decision 24 the relay's reveal records are read for the judge presence reward (ADR-060): from the store
    at `relay_store` (a directory on the network's host, read-only; `read_reveals`), or, with `replay_from`, from
    the arrival times that published ranking carries (`published_reveals`), which is how a reader without the
    store replays the day. Neither named, or a store that cannot be read: the arrivals are unknown, no judge
    reward is paid that day, and the ranking says so (`inputs.judge_presence.relay_store`); the other rewards are
    ranked as usual."""
    rules = rules_for_day(day, unwound_from, doubled_from, more_requests_from, kit_rules_from)
    kit = pays_judge_presence(rules)
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
    jobs = C.jobs(node)
    settled = C.settle_heights(node)
    # Only the programme's own jobs pay a juror (final_season_facts): the juries of other audits are not read.
    audited = [j for j in jobs if j.get("client") == generator and audit_outcome(j) is not None
               and first <= settled.get(j["id"], -1) <= last]
    # Decision 24 only: the programme audits of the days before, within the finality span, whose jury this day
    # may have drawn (a deferred draw) or whose resolution may fall within this day's blocks (ADR-060 decision 2,
    # conditions one and four). A day under an earlier set reads none of them and keeps its bytes.
    earlier = [j for j in jobs if j.get("client") == generator and audit_outcome(j) is not None
               and first - finality <= settled.get(j["id"], -1) < first] if kit else []
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
    resolutions = {jid: C.audit_resolution(node, rpc, jid) for jid in commits_all}

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
        # ADR-060 decision 2, the five conditions, each read from a record the judge did not write alone. The
        # verdicts of the earlier audits are dated like the day's own (F3) and never enter `inputs.verdicts`,
        # which keeps the day's audits only, as before.
        votes_earlier, _ = timely_verdicts({jid: v for jid, v in commits_all.items() if jid not in day_ids},
                                           resolutions, committed)
        all_votes = dict(votes)
        all_votes.update(votes_earlier)
        by_id = {j["id"]: j for j in audited + earlier}
        seats, primaries = {}, {}
        for jid in sorted(committees):
            if drawn_at[jid] is not None and first <= drawn_at[jid] <= last:
                primaries[jid] = by_id[jid].get("miner_id", "") or ""
                seats[jid] = sorted(m for m in committees[jid] if m != primaries[jid])
        keys = [f"{jid}__{m}__{primaries[jid]}" for jid in sorted(seats) for m in seats[jid]]
        store_state, arrivals = (read_reveals(relay_store, keys) if replay_from is None
                                 else published_reveals(replay_from, keys))
        commit_times = {}
        for jid in seats:
            for m in seats[jid]:
                if m in (all_votes.get(jid) or {}):
                    h = committed[jid][m]
                    if h not in commit_times:
                        commit_times[h] = C.block_time(rpc, h)
        resolved_in_day = {jid: r[0] for jid, r in sorted(resolutions.items())
                           if r is not None and first <= r[0] <= last}
        contradicted = contradicted_jurors(resolved_in_day, commits_all, by_id)
        seated = sorted({m for members in seats.values() for m in members})
        reports = {m: kit_report(records_upto, m, last) for m in seated}
        paid_judges, judge_proof = judge_presence(seats, primaries, arrivals, all_votes, committed, commit_times,
                                                 contradicted, reports, rules["kit_min"])
        facts["judge_active"] = {m: 1 for m in paid_judges}
        judge_inputs = {
            "rate_udndr": rules["judge_active_per_day"], "from_day": kit_rules_from,
            # The service's reading of the relay's store: "read", or STORE_UNREAD when the store was not there, in
            # which case every arrival below is unknown and no judge reward is paid this day (ADR-060).
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
    kit_inputs = probation_inputs = None
    if kit:
        # ADR-058 decision 5: the latest signed capacity report at or before the day's end decides whether the
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
        # On every day under decision 24: the judge presence reward's inputs (ADR-060 decision 2, per identity),
        # the kit floor with each ranked identity's report, and the probation with each identity's registration.
        # A day before decision 24 has none of the three, and keeps the bytes it was published with.
        out["inputs"]["judge_presence"] = judge_inputs
        out["inputs"]["kit_floor"] = kit_inputs
        out["inputs"]["probation"] = probation_inputs
    return out


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
        kit_rules_from, status = a.kit_rules_from, None
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
        rules = rules_for_day(a.day, unwound_from, doubled_from, more_requests_from, kit_rules_from)
        # No store on this machine and a published ranking in hand: its arrival times are replayed, as ADR-060
        # says a third party replays the day (`published_reveals`).
        replay = published if (pays_judge_presence(rules) and not a.relay_store and published is not None) else None
        out = rank_day(a.day, _records(a.evidence, a.day), a.node, a.generator, a.start, paid_before,
                       finality_blocks(params, rules), C.height(rpc), epoch_blocks(params),
                       unwound_from=unwound_from, doubled_from=doubled_from, more_requests_from=more_requests_from,
                       kit_rules_from=kit_rules_from, relay_store=a.relay_store, replay_from=replay)
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
