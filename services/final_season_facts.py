"""Final Testnet Season facts: turn the evidence log and the chain's records into the `Identity` rows of
one day.

Pure, like the calculation: records in, rows out. The network reads live in `final_season_chain`; keeping them
out of here is what lets a reader rerun the day from saved inputs and get the same rows.

From the chain
  - verified requests: jobs whose client is the programme's generator (the only work this reward pays),
    settled during the day, and final and positive: paid, never clawed back, unwound, refunded or expired,
    no slash against the miner, and when a dispute or an audit was opened, resolved. "Final" is a matter
    of time, not of state: a job drawn for an audit and deferred for want of a jury keeps its paid state
    until the unwind bound, then turns `+resolved+unwound`. So a day is counted only once its last
    settlement is older than that bound (`finality_height`), never earlier.
  - consistent verdicts: on a resolved audit of a job whose client is the programme's generator (the same
    restriction as the work reward), a juror the chain drew for that audit whose vote matches the outcome
    (conviction -> "0", the miner cleared -> "1"), counted on the day the audited job was settled. A
    verdict from a miner who was not drawn earns nothing: the chain accepts the commit but its own tally
    ignores it. And only a verdict committed before the audit was resolved (`timely_verdicts`): the chain
    accepts a verdict commit after the resolution too, when the outcome is public, and a day is ranked only
    once it is final, more than a day later -- a drawn juror who never voted could post the winning vote
    then and be paid for it. A verdict whose audit's resolution the index does not give is not counted
    either, and the published ranking says so, as for one whose commit cannot be dated (its juror's commits
    in the window outnumber what the search reads); one with no commit transaction in the day's window is
    neither counted nor listed (`timely_verdicts`).
  - presence: the availability windows each identity proved on chain during the day
    (`final_season_chain.presence_proofs`), counted once per window and per day.
  - Under decision 18 only (`final_season_rules.pays_unwound_work`): the programme jobs settled during the day
    whose audit was unwound -- no jury concluded it within the unwind bound, the chain refunded the client and
    neither paid nor slashed the miner (`+resolved+unwound`, `unwound_programme_jobs`). Each counts as one more
    verified request of its miner when its answer reached the programme and the season's grading does not say
    incoherent -- its own answer's grade when it was sampled and graded, else the grading of its miner's day by
    the void rule (`unwound_work`); else it is not paid, and the published ranking says why, job by job.
From the evidence log
  - which programme requests had their answer received (`answered_jobs`): only those count as work,
  - the payout address an identity declared (else its operator, read from the chain),
  - the grades of the sampled answers to programme requests: work is 0 for a day whose graded ones are
    all incoherent, and with it the presence of that day, which is paid only on a day of verified work.
    Several graders may grade one answer (each grade names its grader, `grader_of`); the answer is read as
    incoherent only when every grade of it says so (`answer_incoherent`): a disagreement favours the miner.
  - under decision 24 (`final_season_rules.RULES_KIT`): the signed capacity reports the service filed from the
    registry (`kit_report`: the kit number and the judge role an identity declared, at or before the day's end).
From the relay's store, under decision 24 only
  - the arrival time of each reveal sealed to a seated juror (`reveal_arrivals`), one of the five conditions of the
    judge presence reward (`judge_presence`). The only reader of this module that opens a file;
    it is read-only, and an absent store reads as unknown, which pays nothing and is reported as unknown.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from final_season_calc import Identity
from final_season_rules import (CONTRADICTING_CLASSES, FAULT_CLASSES, FAULT_DOC_VERSION, RULES, canonical_fault_document,
                                day_bounds, day_of_height, fault_draw)

# A day's work is taken away only when every graded answer of the day was judged incoherent and there are
# at least min(GRADES_TO_VOID, answers sampled) graded answers: an identity with one sampled answer can lose
# its day's work on one graded answer, one with more needs two. An answer graded by several graders counts
# once, with its combined grade (`answer_incoherent`).
GRADES_TO_VOID = 2

# Who posted a grade. The service names the grader on each grade from the token it was posted with
# (final_season_server.Handler._grader); a grade with no `grader` field was filed before graders had
# names, when one grader alone posted them, and is read as that first grader's.
FIRST_GRADER = "gpu"


def grader_of(rec: dict) -> str:
    """The name of the grader that posted grade `rec`: its `grader` field, else FIRST_GRADER."""
    return rec.get("grader") or FIRST_GRADER


def answer_incoherent(by_grader: dict) -> bool:
    """The combined grade of one answer, from {grader: [coherent, ...]}: incoherent only when every grade of
    it, from every grader that graded it, says incoherent. One coherent grade keeps the answer coherent: when
    two graders disagree the doubt favours the miner (owner's decision of 2026-10-15, question 6 of
    the work plan). No grade at all is not incoherent."""
    return bool(by_grader) and not any(g for grades in by_grader.values() for g in grades)


def _answer_grades(records: list) -> dict:
    """{(job_id, miner_id): {grader: [coherent, ...]}}: every grade of every sampled answer in `records`, in
    file order. Only a literal `true` is coherent."""
    out = {}
    for rec in records:
        if rec.get("type") == "work_grade":
            key = (rec.get("job_id", ""), rec.get("miner_id", ""))
            out.setdefault(key, {}).setdefault(grader_of(rec), []).append(rec.get("coherent") is True)
    return out

_BAD_MARKERS = ("clawed", "unwound", "refunded", "expired")


def job_is_final_positive(job: dict) -> bool:
    s = job.get("state", "") or ""
    if "+paid" not in s or any(m in s for m in _BAD_MARKERS) or job.get("slashed_primary"):
        return False
    if "+disputed" in s and "resolved" not in s:
        return False                                  # an audit or a dispute still open
    return True


def audit_outcome(job: dict):
    """"0" (the miner was convicted), "1" (cleared) or None (no concluded audit)."""
    s = job.get("state", "") or ""
    if "+disputed" not in s or "resolved" not in s or "unwound" in s:
        return None
    return "0" if ("clawed" in s or job.get("slashed_primary")) else "1"


def answered_jobs(records_upto: list) -> set:
    """Job ids whose answer reached the programme, from the day seals of the evidence log. A programme
    request counts as work only if it is in here: a miner can anchor a commit, and anyone can settle the
    job, without any answer having been given; such a job would be paid and never sampled."""
    out = set()
    for rec in records_upto:
        if rec.get("type") == "work_sealed":
            for jids in (rec.get("answered") or {}).values():
                out.update(jids)
    return out


# Why a drawn juror's verdict is not counted (`timely_verdicts`), as the published ranking names it.
AFTER_RESOLUTION = "committed_after_resolution"   # anchored once the outcome was public: not in the tally
RESOLUTION_NOT_INDEXED = "resolution_not_indexed"  # no audit_expired event and no adjudication in the index
COMMIT_NOT_DATED = "commit_not_dated"              # the juror's commits in the window outnumber what is read
END_BLOCK, IN_TX = "end_block", "transaction"      # final_season_chain.END_BLOCK / IN_TX
# The value `committed` carries for a verdict whose commit could not be dated (`timely_verdicts`): distinct
# from None (searched, and not there) and from a height (searched, and found). Three states, never two.
NOT_DATED = "not_dated"


def timely_verdicts(commits: dict, resolutions: dict, committed: dict) -> tuple:
    """(counted, proof): the drawn jurors' verdicts the chain's own tally could have read, and the record of
    the day's verdicts, counted or not, for the published ranking.

    `commits`:     {job: {juror: {"vote": "0"|"1", "creator": signer}}}, drawn jurors only
                   (`final_season_chain.verdict_commits`);
    `resolutions`: {job: (height, END_BLOCK | IN_TX) or None} for every job of `commits`
                   (`final_season_chain.audit_resolution`);
    `committed`:   {job: {juror: height, None or NOT_DATED}} for every verdict of `commits`: the block of its
                   accepted commit transaction, searched in the day's window, from the draw to the day's final
                   block (`commit_height`, called by `final_season_rank.rank_day`); None when it is not there;
                   NOT_DATED when the juror's own commits in that window are more than the search reads.
    Indexed, never defaulted: a job or a verdict whose dates were not read is a KeyError, not a verdict.

    A verdict NOT_DATED is not counted and is listed (`commit_not_dated`): its date is unknown, and unknown
    is never counted; and the juror chose it, by flooding the search with commits that need no job, so the
    day is ranked without it rather than not at all. Before this, the search raised, the day was deferred,
    and every later day with it: one juror stopped the season's rankings.

    A verdict counts only if its commit precedes the resolution: in an earlier block, or -- the deadline's
    tally running after every transaction of its block -- in the block of an END_BLOCK resolution. Inside the
    block of an adjudication the order of transactions is not read, so only an earlier block counts there.
    Unknown is never counted: an audit whose resolution the index does not give counts none of its
    verdicts, and the proof says so for each. A verdict with no commit transaction in the day's window is not
    counted and not listed: it was anchored after the day became final (or before its jury was drawn), so it
    could not have counted, and listing it would make the same day's proof depend on when it is recomputed --
    a ranking must recompute identically. The window is published beside it (`final_season_rank`)."""
    counted, proof = {}, {}
    for job in sorted(commits):
        res = resolutions[job]
        if res is not None:
            rh, where = res
            if where not in (END_BLOCK, IN_TX) or not isinstance(rh, int):
                raise ValueError(f"{job}: resolution {res!r} is not (height, {END_BLOCK!r} | {IN_TX!r})")
        jurors = {}
        for juror in sorted(commits[job]):
            vote = commits[job][juror]["vote"]
            ch = committed[job][juror]
            if ch is None:
                continue          # no commit transaction in the day's window: not this day's record (above)
            if ch == NOT_DATED:
                jurors[juror] = {"vote": vote, "committed_at": None, "counted": False, "why": COMMIT_NOT_DATED}
                continue
            if not isinstance(ch, int) or isinstance(ch, bool):
                raise ValueError(f"{job}/{juror}: commit height {ch!r} is neither a height, None nor {NOT_DATED!r}")
            entry = {"vote": vote, "committed_at": ch}
            if res is None:
                why = RESOLUTION_NOT_INDEXED
            elif ch < rh or (where == END_BLOCK and ch == rh):
                why = ""
            else:
                why = AFTER_RESOLUTION
            entry["counted"] = not why
            if why:
                entry["why"] = why
            else:
                counted.setdefault(job, {})[juror] = vote
            jurors[juror] = entry
        proof[job] = {"resolved_at": None if res is None else res[0],
                      "resolved_in": None if res is None else res[1], "jurors": jurors}
    return counted, proof


def _counted_window(day: int, start_height: int, finality_height: int, current_height: int, rules,
                    last_height: int) -> tuple:
    """(first, last) blocks the day counts, or raise: a last counted block outside the day, or a day not final."""
    r = RULES if rules is None else rules
    first, nominal_last = day_bounds(day, start_height, r)
    if not first <= last_height <= nominal_last:
        raise ValueError(f"day {day}: last counted block {last_height} outside the day [{first}, {nominal_last}]")
    last = last_height
    if current_height < last + finality_height:
        raise ValueError(f"day {day} is not final before height {last + finality_height} "
                         f"(chain at {current_height})")
    return first, last


def chain_day_facts(day: int, start_height: int, jobs: list, settle_heights: dict, verdicts: dict,
                    generator: str, finality_height: int, current_height: int, answered: set,
                    rules: dict | None = None, *, last_height: int) -> dict:
    """{"work": {miner_id: n}, "verdicts": {miner_id: n}} for one day, or raise if the day is not final.

    `verdicts`: {job_id: {juror_id: "0"|"1"}}, already limited to the jurors drawn for each audit and to the
    verdicts committed before its resolution (`timely_verdicts`).
    `answered`: `answered_jobs` of the evidence; a programme job outside it is not work.
    `last_height`: the day's last counted block (`final_season_rank.season_window`): its full end, or the
    season's last block on the day the season ends. Required, with no default: a default would be the full
    day, and settlements after the season's end would be paid.
    """
    first, last = _counted_window(day, start_height, finality_height, current_height, rules, last_height)
    work, judged = {}, {}
    for j in jobs:
        jid = j.get("id", "")
        h = settle_heights.get(jid)
        if h is None or not (first <= h <= last):
            continue
        if j.get("client") != generator:
            continue                       # neither work nor verdicts: only the programme's jobs pay
        if job_is_final_positive(j) and jid in answered:
            mid = j.get("miner_id", "")
            if mid:
                work[mid] = work.get(mid, 0) + 1
        outcome = audit_outcome(j)
        if outcome is None:
            continue
        for juror, vote in (verdicts.get(jid) or {}).items():
            if vote == outcome and juror != j.get("miner_id"):
                judged[juror] = judged.get(juror, 0) + 1
    return {"work": work, "verdicts": judged}


# -- Decision 18: the work of an unwound audit (final_season_rules.UNWOUND_AUDIT_WORK) -------------------------
# Why an unwound programme job is not paid as work, as the published ranking names it (`unwound_work`). The
# first three are read from the chain's job, the others from the evidence log.
CONVICTED = "convicted"                    # a claw-back or a slash of its own miner on the job: never paid
UNEXPECTED_STATE = "unexpected_state"      # a state no unwind writes (never paid, never resolved, or a verdict)
NO_PRIMARY = "no_primary"                  # the job names no miner: nobody to credit
NOT_ANSWERED = "not_answered"              # its answer is in no seal of the evidence: it never reached the programme
ANSWER_UNREADABLE = "answer_unreadable"    # not in a seal, but a line of the evidence is unreadable: it may be there
# The grading that decided, as the published ranking names it (`unwound_work`, "graded"). The job's own answer
# first, when it was sampled and graded; else its miner's day, by the void rule (`_graded_out`). Each name says
# what was read, never more: "day_not_graded_out" is a day whose grades are all incoherent but fewer than the void
# rule needs -- the day keeps its work, and nothing in it was judged coherent.
OWN_COHERENT = "own_answer_coherent"        # its own answer graded coherent: paid
OWN_INCOHERENT = "own_answer_incoherent"    # its own answer graded incoherent: never paid, whatever the day says
DAY_COHERENT = "day_coherent"               # not graded itself; a grade of its miner's day says coherent: paid
DAY_NOT_GRADED_OUT = "day_not_graded_out"   # not graded itself; graded, none coherent, below the void count: paid
DAY_GRADED_OUT = "day_graded_out"           # not graded itself; its miner's day is graded out: not paid
NOT_GRADED = "not_graded"                   # neither its answer nor its miner's day was graded: not paid
GRADING_UNREADABLE = "grading_unreadable"   # an unreadable evidence line may hold the grade that decides: not paid
_PAID_GRADINGS = frozenset({OWN_COHERENT, DAY_COHERENT, DAY_NOT_GRADED_OUT})
# What no unwind writes beside `+resolved+unwound`: the outcome of a concluded audit, an adjudication, an expiry.
_NOT_AN_UNWIND = frozenset({"vindicated", "adjudicated", "quorum", "expired", "refunded"})


def job_unwound(job: dict):
    """None when the job's audit was not unwound (`job_is_final_positive` and `audit_outcome` decide it).
    Otherwise '' for an unwound audit no verdict went against -- the work decision 18 pays, subject to its answer
    and its grades -- or why it cannot be paid: CONVICTED, UNEXPECTED_STATE, NO_PRIMARY.

    The chain writes `+resolved+unwound` when no audit could conclude within `audit_unwind_blocks`
    (`audit_unwind.go::unwindStrandedRetention`): with `+disputed` when a jury was drawn and stayed below quorum,
    without it when the jury pool was too small or no decentralised seed existed. Read as markers of the state,
    never as a substring of another one. `slashed_primary` is indexed, never defaulted: on the criterion that
    keeps a convicted miner unpaid, a job whose slash was not read is not a job without one."""
    tokens = set((job.get("state", "") or "").split("+"))
    if "unwound" not in tokens:
        return None
    slashed = job["slashed_primary"]
    if slashed is True or "clawed" in tokens:
        return CONVICTED
    if slashed is not False:
        raise ValueError(f"job {job.get('id')!r}: slashed_primary {slashed!r} is neither true nor false")
    if not {"paid", "resolved"} <= tokens or tokens & _NOT_AN_UNWIND:
        return UNEXPECTED_STATE
    if not job.get("miner_id"):
        return NO_PRIMARY
    return ""


def unwound_programme_jobs(day: int, start_height: int, jobs: list, settle_heights: dict, generator: str,
                           finality_height: int, current_height: int, rules: dict | None = None, *,
                           last_height: int) -> dict:
    """{job_id: {"miner_id": miner, "state": job_unwound(job)}} for every job whose client is the programme's
    generator, settled in the day's counted window (the window and the finality of `chain_day_facts`, refused the
    same way), whose audit was unwound. Its answer and its grades are read by `unwound_work`."""
    first, last = _counted_window(day, start_height, finality_height, current_height, rules, last_height)
    out = {}
    for j in jobs:
        jid = j.get("id", "")
        h = settle_heights.get(jid)
        if h is None or not (first <= h <= last) or j.get("client") != generator:
            continue
        why = job_unwound(j)
        if why is not None:
            out[jid] = {"miner_id": j.get("miner_id", "") or "", "state": why}
    return out


def _work_grades(day: int, records_upto: list) -> tuple:
    """({miner_id: job ids sampled}, {miner_id: [coherent, ...]}) of `day`'s work answers and grades: one entry
    per graded answer, its combined grade (`answer_incoherent`), whatever the number of graders that graded it.
    With one grader per answer this is one entry per grade record, as the days before the second grader were
    ranked."""
    sampled, grades = {}, {}
    day_records = [rec for rec in records_upto if rec.get("day") == day]
    for rec in day_records:
        if rec.get("type") == "work_answer":
            sampled.setdefault(rec.get("miner_id", ""), set()).add(rec.get("job_id", ""))
    for (_jid, mid), by_grader in _answer_grades(day_records).items():
        grades.setdefault(mid, []).append(not answer_incoherent(by_grader))
    return sampled, grades


def _own_grades(records_upto: list) -> dict:
    """{(job_id, miner_id): {grader: [coherent, ...]}}: the grades of each sampled answer, whatever the day they
    are filed under (the service files a grade under the day its answer was sealed,
    `final_season_server._grading_result`). Only a literal `true` is coherent."""
    return _answer_grades(records_upto)


def _sealed_on(records_upto: list) -> dict:
    """{job_id: day of the seal that lists it}: the day its answer arrived, which is the day its grade is filed
    under."""
    out = {}
    for rec in records_upto:
        if rec.get("type") == "work_sealed":
            for jids in (rec.get("answered") or {}).values():
                for jid in jids:
                    out.setdefault(jid, rec.get("day"))
    return out


def unwound_grading(jid: str, mid: str, day: int, records_upto: list) -> str:
    """The grading that decides whether unwound job `jid` of miner `mid`, counted on `day`, is paid: one of the
    names above. Its own answer first: a job sampled and graded is not paid when every grade of it says
    incoherent (`answer_incoherent`), whatever the rest of its miner's day says -- a coherent grade of another
    answer says nothing of this one; and paid when one of its own grades says coherent. Else its miner's day,
    by the void rule that takes a day's work away (`_graded_out`), and at least one grade.

    Three states, never two. An evidence line that could not be read (`_unreadable_lines`) may hold the grade that
    decides: with no grade of its own found, a torn line on the day its answer was sealed (any day when that seal
    is not found) makes the grading unreadable -- before any day-level reading, since the hidden line may be its
    own incoherent grade; and with no coherent grade of the day, a torn line of that day does too."""
    return _grading(jid, mid, day, _grading_reads(day, records_upto))


def _grading_reads(day: int, records_upto: list) -> tuple:
    """What `unwound_grading` reads, read once for every unwound job of the day."""
    torn = {r.get("day") for r in records_upto if r.get("type") == "_unreadable_lines"}
    return (_own_grades(records_upto), _sealed_on(records_upto), torn) + _work_grades(day, records_upto)


def _grading(jid: str, mid: str, day: int, reads: tuple) -> str:
    own_grades, sealed_on, torn, sampled, grades = reads
    own = own_grades.get((jid, mid), {})
    if own:
        return OWN_INCOHERENT if answer_incoherent(own) else OWN_COHERENT
    sealed = sealed_on.get(jid)
    if (sealed in torn) if sealed is not None else bool(torn):
        return GRADING_UNREADABLE
    g = grades.get(mid, [])
    if any(g):
        return DAY_COHERENT
    if day in torn:
        return GRADING_UNREADABLE
    if not g:
        return NOT_GRADED
    if _graded_out(g, len(sampled.get(mid, set()))):
        return DAY_GRADED_OUT
    return DAY_NOT_GRADED_OUT


def unwound_work(day: int, records_upto: list, unwound: dict, answered: set) -> tuple:
    """(paid, proof) under decision 18. `unwound`: `unwound_programme_jobs` of the day; `answered`:
    `answered_jobs` of the same records.

    paid:  {miner_id: n} -- the unwound jobs counted as verified requests of their miner.
    proof: {job_id: {"miner_id", "answered": true | false | null, "graded": <`unwound_grading`>, "paid": bool,
           "why": <reason, only when not paid>}} for every unwound programme job of the day, paid or not: the
           published ranking's record of the rule.

    A job is paid when the chain says unwound with no verdict against it (`job_unwound` == ''), its answer is in
    a seal (`answered`) and its grading is one that pays (`unwound_grading`: its own answer graded coherent, or,
    not graded itself, its miner's day graded and not graded out). Three states, never two: an evidence line that
    could not be read (`_unreadable_lines`, final_season_evidence.Evidence.read) may hold a seal or a grade, so a
    job not found in a seal is `null`, not `false`, when one exists, and its grading is unreadable when the hidden
    line may be the grade that decides. Unknown is never paid, and said."""
    torn_any = any(r.get("type") == "_unreadable_lines" for r in records_upto)
    reads = _grading_reads(day, records_upto)
    paid, proof = {}, {}
    for jid in sorted(unwound):
        mid, why = unwound[jid]["miner_id"], unwound[jid]["state"]
        graded = _grading(jid, mid, day, reads)
        ans = True if jid in answered else (None if torn_any else False)
        if not why:
            if ans is None:
                why = ANSWER_UNREADABLE
            elif ans is False:
                why = NOT_ANSWERED
            elif graded not in _PAID_GRADINGS:
                why = graded
        entry = {"miner_id": mid, "answered": ans, "graded": graded, "paid": not why}
        if why:
            entry["why"] = why
        else:
            paid[mid] = paid.get(mid, 0) + 1
        proof[jid] = entry
    return paid, proof


def _graded_out(grades: list, gradable: int) -> bool:
    """Every graded answer incoherent, and at least min(GRADES_TO_VOID, gradable) of them, never on no grade.
    `grades` holds one combined grade per graded answer (`_work_grades`)."""
    return len(grades) >= max(1, min(GRADES_TO_VOID, gradable)) and not any(grades)


def day_identities(day: int, records_upto: list, chain: dict, default_address: dict) -> list:
    """`records_upto`: every evidence record of days <= `day`, in file order.
    `chain`: {"work": {miner_id: n}, "verdicts": {miner_id: n}, "presence": {miner_id: windows}} for the day, and
    under decision 24 `"judge_active": {miner_id: 1}` (`judge_presence`), a fourth family whose identities rank
    like the others. Under decision 18 `work` already holds the unwound jobs `unwound_work` pays
    (`final_season_rank.rank_day`).
    `default_address`: {miner_id: operator} from the chain, used when the identity declared no address."""
    payout = {}
    for rec in records_upto:
        if rec.get("type") == "payout":
            payout[rec.get("miner_id", "")] = rec.get("address", "")
    work_sampled, work_grades = _work_grades(day, records_upto)
    # Indexed, never defaulted: a family of facts that was not read is not a family of zeros. The judge family
    # exists under decision 24 only; a day before it hands none, and no key is read for it then.
    work_of, verdicts_of, presence_of = chain["work"], chain["verdicts"], chain["presence"]
    judge_of = chain["judge_active"] if "judge_active" in chain else {}
    out = []
    for mid in sorted(set(work_of) | set(verdicts_of) | set(presence_of) | set(judge_of)):
        work = work_of.get(mid, 0)
        if _graded_out(work_grades.get(mid, []), len(work_sampled.get(mid, set()))):
            work = 0
        out.append(Identity(mid, presence=int(presence_of.get(mid, 0)), verified_requests=work,
                            verdicts=verdicts_of.get(mid, 0),
                            payout_address=payout.get(mid) or default_address.get(mid, ""),
                            judge_active=int(judge_of.get(mid, 0))))
    return out


# -- Decision 24: the kit floor, the judge presence reward and the probation (final_season_rules.RULES_KIT) ------
# The capacity registry's signed reports, as the service files them in the evidence (`final_season_server.watch_capacity`):
# one record per identity each time its signed report's kit number or judge role changes, with the chain height at
# which the service read the registry. The ranking reads the latest record at or before the day's last counted
# block: the registry itself keeps only each identity's latest report, so a reading made when the day is ranked,
# more than a day after it ended, would be a report of a later day.
CAPACITY = "capacity"


def kit_report(records_upto: list, miner_id: str, last_height: int):
    """The latest signed capacity report of `miner_id` the service filed at or before block `last_height`:
    {"kit_version": int | None, "judge_role": str | None, "height": int}, or None when there is none. The number
    and the role are read from the record, never defaulted: a record without a readable number reads None, which
    `final_season_calc.kit_allows_pay` refuses, and a role that is not a string reads None, which is not
    `active`. Records are kept in file order; the one with the greatest height at or before the bound wins, the
    later-filed one on equal heights."""
    best = None
    for rec in records_upto:
        if rec.get("type") != CAPACITY or rec.get("miner_id") != miner_id:
            continue
        h = rec.get("height")
        if type(h) is not int or h < 0 or h > last_height:
            continue
        if best is None or h >= best["height"]:
            kv, role = rec.get("kit_version"), rec.get("judge_role")
            best = {"kit_version": kv if (type(kv) is int and kv >= 0) else None,
                    "judge_role": role if isinstance(role, str) else None, "height": h}
    return best


# Why an identity's registration could not be dated (`final_season_rank.rank_day`), as the published ranking names
# it under `inputs.probation.not_dated`. Such an identity is payable 0 for the day (`final_season_calc.compute_day`,
# `registration_not_dated`): unknown is neither "before the season" (paid in full) nor "its first full day" (paid
# 60 percent), and the first cause is the identity's own doing, as a juror who floods the commit search is not paid
# for the verdict it hid.
REGISTRATION_SEARCH_TOO_LONG = "search_too_long"   # its own registrations in the season outnumber what is read
REGISTRATION_NO_OPERATOR = "no_operator"           # no operator address known to search its registrations by


def first_full_day(registered_at, start_height: int, rules: dict | None = None):
    """The first season day that is a full day after the identity's registration: the first day whose first
    block comes after the registration's block. `registered_at` is the height of the identity's first accepted
    registration in the season (`final_season_chain.miner_registration`), None when the chain's index holds none
    at or before the day (the identity was registered before the season, and day 0 is its first full day), or
    NOT_DATED when the registration could not be dated: then there is no first full day, None, and the identity
    is not paid that day (the reasons above). A height before the season's first block reads as None does.
    Registered in the last block of day N, the first full day is N + 1; registered in the first block of day
    N + 1, it is N + 2, since day N + 1 then holds the registration's own block."""
    if registered_at is None:
        return 0
    if registered_at == NOT_DATED:
        return None
    if type(registered_at) is not int or registered_at < 0:
        raise ValueError(f"registration height {registered_at!r} is neither a height, None nor {NOT_DATED!r}")
    return max(0, day_of_height(registered_at, start_height, rules) + 1)


# The relay's store of sealed reveals (`relay_store.DiskStore`): kind `reveal`, one record per key
# `<job>__<juror>__<primary>` (`reveal_worker.py`), its arrival time in the field `t` (`relay_store.DiskStore.write`).
# Read from the store's directory on the network's host, read-only; no route of the relay serves the time.
REVEAL_KIND = "reveal"
# What the reader answers when the store, or a record, could not be read: distinct from None (read, and absent)
# and from a time (read, and found). Three answers, never two: unknown pays no judge reward, and is reported.
STORE_UNREAD = "?"


def reveal_arrivals(store_root, keys: list) -> dict:
    """{key: arrival time (float, UTC seconds) | None | STORE_UNREAD} for every key of `keys`. The whole answer is
    STORE_UNREAD for every key when `store_root` is empty or its `reveal` directory cannot be listed: an absent
    store is not a store with no reveals. A record that is missing is None; one that cannot be read or whose
    stored key is not the one asked for is STORE_UNREAD for that key alone."""
    if not store_root:
        return {k: STORE_UNREAD for k in keys}
    import relay_store                      # the file name of a record is the store's: one definition, imported
    d = os.path.join(store_root, REVEAL_KIND)
    try:
        names = set(os.listdir(d))
    except OSError:
        return {k: STORE_UNREAD for k in keys}
    out = {}
    for k in keys:
        name = relay_store._fname(REVEAL_KIND, k)
        if name not in names:
            out[k] = None
            continue
        try:
            with open(os.path.join(d, name), "rb") as f:
                rec = json.loads(f.read())
            t = rec["t"]
            if rec.get("k") != k or isinstance(t, bool) or not isinstance(t, (int, float)):
                raise ValueError("record of another key, or no time")
            out[k] = float(t)
        except (OSError, ValueError, KeyError, TypeError):
            out[k] = STORE_UNREAD
    return out


def reveal_arrivals_published(ranking: dict, keys: list) -> dict:
    """The arrival times a published ranking carries (`inputs.judge_presence.reveals`), in the shape of
    `reveal_arrivals`: how a reader without the relay's store replays the day the service ranked. A key the
    ranking does not name is STORE_UNREAD, never None. A day under the jury set also publishes the arrivals of
    its verdicts' reveals under `inputs.jury.reveals`, read here the same way."""
    pub = ((ranking.get("inputs") or {}).get("judge_presence") or {}).get("reveals")
    if not isinstance(pub, dict):
        return {k: STORE_UNREAD for k in keys}
    jury_pub = ((ranking.get("inputs") or {}).get("jury") or {}).get("reveals")
    if isinstance(jury_pub, dict):
        pub = dict(jury_pub, **pub)
    out = {}
    for k in keys:
        if k not in pub:
            out[k] = STORE_UNREAD
            continue
        t = pub[k]
        out[k] = STORE_UNREAD if t == STORE_UNREAD else (None if t is None else float(t))
    return out


# Why a seated identity was not paid the judge reward, as the published ranking names it.
JUDGE_NO_REVEAL = "no_reveal_sealed_to_this_juror"       # condition two: no record under its key
JUDGE_STORE_UNREAD = "relay_store_unread"                # the store, or the record, could not be read: unknown
JUDGE_NO_TIMELY_VERDICT = "no_timely_verdict"            # condition three: no verdict counted before the resolution
JUDGE_VERDICT_BEFORE_REVEAL = "verdict_before_reveal"    # condition three: committed before the reveal arrived
JUDGE_CONTRADICTED = "contradicted_by_a_conviction_concluded_this_day"   # condition four
JUDGE_NO_SIGNED_REPORT = "no_signed_report"              # condition five: no signed report at or before the day's end
JUDGE_ROLE_NOT_ACTIVE = "judge_role_not_active"          # condition five: mute, off, unknown or unreadable
JUDGE_KIT_BELOW_MIN = "kit_below_min"                    # condition five: a kit below the floor, or no number
JUDGE_ACTIVE_ROLE = "active"                             # capacity_server.JUDGE_ROLES, the one word that pays


def judge_presence(seats: dict, primaries: dict, arrivals: dict, votes: dict, committed: dict, commit_times: dict,
                   contradicted: set, reports: dict, kit_min: int) -> tuple:
    """(paid, proof) of the judge presence reward of one day, pure.

    seats:        {job: [juror, ...]} the anchored juries of the programme audits drawn on the day (condition one,
                  `final_season_chain.audit_draw` within the day's blocks), the primary left out;
    primaries:    {job: miner_id} the audited miner, who seals the reveals (`reveal_worker.py`);
    arrivals:     `reveal_arrivals` for every key `<job>__<juror>__<primary>` of `seats`: the time the relay recorded
                  the reveal sealed to that juror (condition two), None or STORE_UNREAD;
    votes:        {job: {juror: vote}} the verdicts counted as timely (`timely_verdicts`, condition three's first
                  half), for the same jobs;
    committed:    {job: {juror: height}} the block of each counted verdict's commit transaction;
    commit_times: {height: header time (UTC seconds)} of every height in `committed`
                  (`final_season_chain.block_time`); indexed, never defaulted;
    contradicted: the identities with a verdict inconsistent with the outcome of a programme audit resolved within
                  the day's blocks (condition four, `contradicted_jurors`);
    reports:      {juror: `kit_report` or None} the latest signed capacity report at or before the day's end;
    kit_min:      the set's floor.

    paid:  {juror: evidence}: the job id of one audit that met conditions one to three, the reveal key, the arrival
           time, the commit's height and block time, and the report's height, as the judge presence reward asks the ranking to publish;
    proof: {juror: {"paid": bool, "why": <name above, when not paid>, "audits": {job: "ok" | <name>}, ...evidence}}
           for every seated identity. An identity is paid once, whatever the number of audits it satisfies.
    A verdict counts for condition three only when its block time is strictly later than the arrival time; an
    unread arrival is unknown, which never pays. Unknown is reported under its own name, never folded into absent."""
    paid, proof = {}, {}
    for juror in sorted({m for members in seats.values() for m in members}):
        audits, evidence, best_why = {}, None, None
        for job in sorted(seats):
            if juror not in seats[job]:
                continue
            key = f"{job}__{juror}__{primaries[job]}"
            t = arrivals[key]
            if t == STORE_UNREAD:
                why = JUDGE_STORE_UNREAD
            elif t is None:
                why = JUDGE_NO_REVEAL
            elif juror not in (votes.get(job) or {}):
                why = JUDGE_NO_TIMELY_VERDICT
            else:
                h = committed[job][juror]
                bt = commit_times[h]
                why = "" if bt > t else JUDGE_VERDICT_BEFORE_REVEAL
            audits[job] = why or "ok"
            if not why and evidence is None:
                evidence = {"job": job, "reveal_key": key, "reveal_arrival": t, "commit_height": h,
                            "commit_time": bt}
            if why and best_why is None:
                best_why = why
        rep = reports.get(juror)
        entry = {"audits": audits}
        if evidence is None:
            why = best_why or JUDGE_NO_TIMELY_VERDICT
        elif juror in contradicted:
            why = JUDGE_CONTRADICTED
        elif rep is None:
            why = JUDGE_NO_SIGNED_REPORT
        elif rep["judge_role"] != JUDGE_ACTIVE_ROLE:
            why = JUDGE_ROLE_NOT_ACTIVE
        elif rep["kit_version"] is None or rep["kit_version"] < kit_min:
            why = JUDGE_KIT_BELOW_MIN
        else:
            why = ""
        if evidence is not None:
            entry.update(evidence)
        if rep is not None:
            entry["report_height"] = rep["height"]
            entry["judge_role"], entry["kit_version"] = rep["judge_role"], rep["kit_version"]
        entry["paid"] = not why
        if why:
            entry["why"] = why
        else:
            paid[juror] = dict(evidence, report_height=rep["height"])
        proof[juror] = entry
    return paid, proof


def contradicted_jurors(resolved: dict, commits: dict, jobs_by_id: dict) -> set:
    """The identities with a verdict inconsistent with the outcome of an audit of `resolved` (the judge presence reward's
    condition four). `resolved`: {job: resolution height} the programme audits whose resolution falls within the
    day's blocks (`final_season_chain.audit_resolution`, filtered by the caller); `commits`: {job: {juror: {"vote":
    "0" | "1", ...}}} the drawn jurors' verdicts on them (`final_season_chain.verdict_commits`); `jobs_by_id`: the
    chain's jobs, for the outcome (`audit_outcome`). A verdict's date is not read here: the day of the conclusion
    is what voids, whatever day the verdict was cast. An audit with no concluded outcome contradicts nothing."""
    out = set()
    for job in resolved:
        outcome = audit_outcome(jobs_by_id[job])
        if outcome is None:
            continue
        for juror, c in (commits.get(job) or {}).items():
            if c["vote"] != outcome:
                out.add(juror)
    return out


# -- the jury set (final_season_rules.RULES_JURY) -------------------------------------------------------------
def audit_unwound(job: dict) -> bool:
    """Whether the chain unwound the job's audit (`+resolved+unwound`, `audit_unwind.go::unwindStrandedRetention`),
    read as markers of the state. Under the jury set such an audit pays its timely verdicts like a concluded one."""
    return {"resolved", "unwound"} <= set((job.get("state", "") or "").split("+"))


JURY_OK = "ok"


def jury_verdicts(votes: dict, primaries: dict, arrivals: dict, committed: dict, commit_times: dict) -> tuple:
    """(counts, proof) of the verdicts the jury set pays, pure.

    votes:        {job: {juror: vote}} the drawn jurors' verdicts counted as timely (`timely_verdicts`) on the audits of
                  the programme requests settled that day, concluded or unwound;
    primaries:    {job: miner_id} the audited miner, who never earns a verdict on its own job;
    arrivals:     `reveal_arrivals` for every key `<job>__<juror>__<primary>` of `votes`;
    committed:    {job: {juror: height}} the block of each verdict's commit transaction;
    commit_times: {height: header time} of those blocks, indexed, never defaulted.

    counts: {juror: n} the verdicts committed strictly after the arrival of the reveal sealed to that juror; proof:
    {job: {juror: "ok" | why}}. The vote's value is never read: a verdict is paid whatever it says, and an unread
    arrival is unknown, which never pays."""
    counts, proof = {}, {}
    for job in sorted(votes):
        primary, per = primaries[job], {}
        for juror in sorted(votes[job]):
            if juror == primary:
                continue
            t = arrivals[f"{job}__{juror}__{primary}"]
            if t is None:
                why = JUDGE_NO_REVEAL
            elif t == STORE_UNREAD:
                why = JUDGE_STORE_UNREAD
            else:
                why = "" if commit_times[committed[job][juror]] > t else JUDGE_VERDICT_BEFORE_REVEAL
            per[juror] = why or JURY_OK
            if not why:
                counts[juror] = counts.get(juror, 0) + 1
        proof[job] = per
    return counts, proof


# Why a "1" on a controlled-fault request does not contradict its juror, as the ranking names it.
FAULT_CLASS_CONTRADICTS_NOBODY = "class_contradicts_nobody"      # a plausible false fact: measured, never a sanction
FAULT_REVEAL_ARRIVED = "reveal_arrived_for_this_juror"           # a no-reveal fault whose reveal the relay holds
FAULT_ARRIVAL_UNREAD = "relay_store_unread"                      # unknown, which never sanctions


def fault_contradictions(faults: dict, verdict_proof: dict, arrivals: dict, primaries: dict) -> tuple:
    """(contradicted, proof) of one day under the jury set, pure.

    faults:        {job: class} the controlled-fault requests settled that day, from the checked reveals: the entries
                   listed as faulted only, since a drawn request served honestly is no fault;
    verdict_proof: the day's `timely_verdicts` proof, every drawn juror's verdict read in the day's window;
    arrivals:      `reveal_arrivals` for every key `<job>__<juror>__<primary>` of those verdicts;
    primaries:     {job: miner_id}.

    A juror is contradicted when it voted "1" on a fault of a class in `final_season_rules.CONTRADICTING_CLASSES`:
    an incoherent answer, or no reveal, when the relay holds no reveal sealed to that juror. A "0" is the right
    answer on a fault and contradicts nobody, and so does any vote on a request that is not a fault: under this set
    nothing else contradicts. proof: {job: {"class", "votes": {juror: {"vote", "contradicted", "why"?}}}}."""
    out, proof = set(), {}
    for job in sorted(faults):
        cls = faults[job]
        jurors = (verdict_proof.get(job) or {}).get("jurors") or {}
        per = {}
        for juror in sorted(jurors):
            vote = jurors[juror]["vote"]
            entry = {"vote": vote, "contradicted": False}
            if vote == "1":
                if cls not in CONTRADICTING_CLASSES:
                    entry["why"] = FAULT_CLASS_CONTRADICTS_NOBODY
                elif cls == "no_reveal" and arrivals[f"{job}__{juror}__{primaries[job]}"] is not None:
                    t = arrivals[f"{job}__{juror}__{primaries[job]}"]
                    entry["why"] = FAULT_ARRIVAL_UNREAD if t == STORE_UNREAD else FAULT_REVEAL_ARRIVED
                else:
                    entry["contradicted"] = True
                    out.add(juror)
            per[juror] = entry
        proof[job] = {"class": cls, "votes": per}
    return out, proof


# The reveal of a period of controlled-fault requests, as the season service reads it from the
# directory DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR names: two files per period, named by its commitment.
FAULT_DOC_FILE = "{}.commitment.json"      # the canonical document's exact bytes, whose sha256 is the commitment
FAULT_REVEAL_FILE = "{}.reveal.json"       # {"version", "commitment", "seed", "faults": [{"job_id", ...}]}
FAULT_DOC_KEYS = frozenset({"classes", "fault_bps", "from_day", "nonce", "seed_sha256", "to_day", "traps", "version"})
FAULT_REVEAL_KEYS = frozenset({"version", "commitment", "seed", "faults"})
# One entry per drawn request (`final_season_rules.fault_draw`), "faulted" true when the launcher served the drawn
# fault, false when it served the request honestly (a client it could not read): never left out, so that a fault is
# not withdrawn after the votes are known (`drawn_not_listed`).
FAULT_ENTRY_KEYS = frozenset({"job_id", "miner_id", "class", "question", "answer", "faulted"})
FAULT_ABSENT = "absent"
_FAULT_MINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")      # final_season_server.MINER_ID
_FAULT_JOB = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FAULT_ADDRESS = re.compile(r"dendra1[0-9a-z]{38,58}\Z")
_FAULT_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def read_fault_reveal(reveal_dir, commitment: str) -> tuple:
    """(state, document bytes, reveal) of the period whose commitment is `commitment`. Three states: "read", with
    both; FAULT_ABSENT when the directory lists and either file is not there; STORE_UNREAD when no directory is
    named, it cannot be listed, or a file cannot be read or is not JSON. An absent reveal is never a reveal without
    faults: the caller refuses the day on anything but "read"."""
    if not reveal_dir:
        return STORE_UNREAD, None, None
    try:
        names = set(os.listdir(reveal_dir))
    except OSError:
        return STORE_UNREAD, None, None
    doc_name, rev_name = FAULT_DOC_FILE.format(commitment), FAULT_REVEAL_FILE.format(commitment)
    if doc_name not in names or rev_name not in names:
        return FAULT_ABSENT, None, None
    try:
        with open(os.path.join(reveal_dir, doc_name), "rb") as f:
            doc = f.read()
        with open(os.path.join(reveal_dir, rev_name), "rb") as f:
            reveal = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return STORE_UNREAD, None, None
    return "read", doc, reveal


def fault_reveal_published(ranking: dict, commitment: str) -> tuple:
    """(state, document bytes, reveal) of the period whose commitment is `commitment`, replayed from a published
    ranking (`inputs.jury.faults.periods` and `.jobs`): its document, its seed and the requests of the day listed under
    it, in the shape of `read_fault_reveal`, so a reader without the service's directory checks the day against the
    same commitment and the same chain. STORE_UNREAD when the ranking carries no such period."""
    faults = ((ranking.get("inputs") or {}).get("jury") or {}).get("faults")
    periods = faults.get("periods") if isinstance(faults, dict) else None
    mine = [p for p in periods if isinstance(p, dict) and isinstance(p.get("period"), dict)
            and p["period"].get("commitment") == commitment] if isinstance(periods, list) else []
    if len(mine) != 1 or not isinstance(mine[0].get("document"), str):
        return STORE_UNREAD, None, None
    jobs = faults.get("jobs") if isinstance(faults.get("jobs"), dict) else {}
    entries = [{k: e.get(k) for k in FAULT_ENTRY_KEYS} for _jid, e in sorted(jobs.items())
               if isinstance(e, dict) and e.get("commitment") == commitment]
    return "read", mine[0]["document"].encode("utf-8"), {"version": FAULT_DOC_VERSION, "commitment": commitment,
                                                         "seed": mine[0].get("seed"), "faults": entries}


def drawn_not_listed(checked: dict, jobs: list, settle_heights: dict, generator: str, first: int, last: int) -> list:
    """[(job_id, miner_id, class)] of the programme requests settled within blocks `first`..`last` of the identities
    of one checked reveal (`check_fault_reveal`) that its seed draws (`final_season_rules.fault_draw`) and that the
    reveal does not list, pure. Empty when the reveal is complete for those blocks; the caller refuses the day
    otherwise, since a drawn request left out of a reveal would be a fault withdrawn after the votes are known."""
    doc, out = checked["document"], []
    for j in sorted(jobs, key=lambda x: x["id"]):
        h = settle_heights.get(j["id"])
        if j.get("client") != generator or j.get("miner_id") not in checked["traps"] or h is None or not first <= h <= last:
            continue
        cls = fault_draw(checked["seed"], j["id"], doc["fault_bps"], doc["classes"])
        if cls is not None and j["id"] not in checked["faults"]:
            out.append((j["id"], j["miner_id"], cls))
    return out


def check_fault_reveal(period: dict, doc_bytes: bytes, reveal) -> dict:
    """The reveal of `period`, checked against its commitment, pure: {"document", "seed" (bytes), "traps":
    {miner_id: payout_address}, "faults": {job_id: entry}}. Raises ValueError on the first thing that does not match:
    the document's sha256 is not the commitment, the document is not canonical or not of the period, the seed's
    sha256 is not the one committed, or a revealed fault names an identity the document does not, a class it does not
    commit, or a class the seed does not draw for that job (`final_season_rules.fault_draw`)."""
    c = period["commitment"]
    if not isinstance(doc_bytes, bytes) or hashlib.sha256(doc_bytes).hexdigest() != c:
        raise ValueError(f"the revealed document's sha256 is not the commitment {c} of days {period['from_day']} to "
                         f"{period['to_day']}")
    try:
        doc = json.loads(doc_bytes.decode("utf-8"))
    except ValueError as e:
        raise ValueError(f"the revealed document of {c} is not JSON: {e}") from e
    if not isinstance(doc, dict) or set(doc) != FAULT_DOC_KEYS or canonical_fault_document(doc) != doc_bytes:
        raise ValueError(f"the revealed document of {c} is not the canonical document with keys {sorted(FAULT_DOC_KEYS)}")
    if doc["version"] != FAULT_DOC_VERSION or type(doc["version"]) is not int:
        raise ValueError(f"the revealed document of {c} is version {doc['version']!r}")
    if (doc["from_day"], doc["to_day"]) != (period["from_day"], period["to_day"]) or type(doc["from_day"]) is not int \
            or type(doc["to_day"]) is not int:
        raise ValueError(f"the revealed document of {c} names days {doc['from_day']!r} to {doc['to_day']!r}, and the "
                         f"rule set days {period['from_day']} to {period['to_day']}")
    bps, classes = doc["fault_bps"], doc["classes"]
    if type(bps) is not int or not 0 < bps <= 10_000:
        raise ValueError(f"the revealed document of {c} names fault_bps {bps!r}")
    if (not isinstance(classes, list) or not classes or len(set(classes)) != len(classes)
            or classes != [k for k in FAULT_CLASSES if k in classes]):
        raise ValueError(f"the revealed document of {c} names classes {classes!r}, not a list of {list(FAULT_CLASSES)} "
                         f"in that order")
    for k in ("seed_sha256", "nonce"):
        if not isinstance(doc[k], str) or not _FAULT_HEX64.match(doc[k]):
            raise ValueError(f"the revealed document of {c} names {k} {doc[k]!r}")
    traps, prev = {}, ""
    if not isinstance(doc["traps"], list) or not doc["traps"]:
        raise ValueError(f"the revealed document of {c} names no identity")
    for t in doc["traps"]:
        if (not isinstance(t, dict) or set(t) != {"miner_id", "payout_address"} or not isinstance(t["miner_id"], str)
                or not _FAULT_MINER.match(t["miner_id"]) or not isinstance(t["payout_address"], str)
                or not _FAULT_ADDRESS.match(t["payout_address"]) or t["miner_id"] <= prev):
            raise ValueError(f"the revealed document of {c} names identity {t!r}: not a miner id and a payout address, "
                             f"in order, once")
        traps[t["miner_id"]], prev = t["payout_address"], t["miner_id"]
    if not isinstance(reveal, dict) or set(reveal) != FAULT_REVEAL_KEYS:
        raise ValueError(f"the reveal of {c} does not carry exactly {sorted(FAULT_REVEAL_KEYS)}")
    if reveal["version"] != FAULT_DOC_VERSION or reveal["commitment"] != c:
        raise ValueError(f"the reveal names commitment {reveal['commitment']!r} (version {reveal['version']!r}), not {c}")
    seed = reveal["seed"]
    if not isinstance(seed, str) or not _FAULT_HEX64.match(seed) or \
            hashlib.sha256(bytes.fromhex(seed)).hexdigest() != doc["seed_sha256"]:
        raise ValueError(f"the revealed seed of {c} is not the one whose sha256 the document commits")
    seed_bytes = bytes.fromhex(seed)
    if not isinstance(reveal["faults"], list):
        raise ValueError(f"the reveal of {c} carries no list of faults")
    faults = {}
    for e in reveal["faults"]:
        if (not isinstance(e, dict) or set(e) != FAULT_ENTRY_KEYS or not isinstance(e["job_id"], str)
                or not _FAULT_JOB.match(e["job_id"]) or not isinstance(e["question"], str)
                or not isinstance(e["answer"], str) or type(e["faulted"]) is not bool or e["job_id"] in faults):
            raise ValueError(f"the reveal of {c} carries an entry that is not one job id, its identity, class, question, "
                             f"answer and whether it was faulted: {str(e)[:120]}")
        if e["miner_id"] not in traps:
            raise ValueError(f"the reveal of {c} names job {e['job_id']} of {e['miner_id']!r}, an identity the "
                             f"document does not commit")
        if e["class"] not in classes:
            raise ValueError(f"the reveal of {c} names class {e['class']!r} for job {e['job_id']}, a class the "
                             f"document does not commit")
        drawn = fault_draw(seed_bytes, e["job_id"], bps, classes)
        if drawn != e["class"]:
            raise ValueError(f"the reveal of {c} names class {e['class']!r} for job {e['job_id']}, and the seed draws "
                             f"{drawn!r} for it")
        faults[e["job_id"]] = dict(e)
    return {"document": doc, "seed": seed_bytes, "traps": traps, "faults": faults}
