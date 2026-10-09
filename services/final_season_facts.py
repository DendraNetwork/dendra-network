"""Final Testnet Season facts: turn the evidence log and the chain's records into the `Identity` rows of
one day.

PURE, like the calculation: records in, rows out. The network reads live in `final_season_chain`; keeping them
out of here is what lets a reader rerun the day from saved inputs and get the same rows.

FROM THE CHAIN
  - verified requests: jobs whose CLIENT is the programme's generator (the only work this reward pays),
    settled during the day, and FINAL and POSITIVE: paid, never clawed back, unwound, refunded or expired,
    no slash against the miner, and when a dispute or an audit was opened, resolved. "Final" is a matter
    of time, not of state: a job drawn for an audit and deferred for want of a jury keeps its paid state
    until the unwind bound, then turns `+resolved+unwound`. So a day is counted only once its last
    settlement is older than that bound (`finality_height`), never earlier.
  - consistent verdicts: on a resolved audit of a job whose CLIENT is the programme's generator (the same
    restriction as the work reward), a juror the chain DREW for that audit whose vote matches the outcome
    (conviction -> "0", the miner cleared -> "1"), counted on the day the audited job was settled. A
    verdict from a miner who was not drawn earns nothing: the chain accepts the commit but its own tally
    ignores it. AND ONLY A VERDICT COMMITTED BEFORE THE AUDIT WAS RESOLVED (`timely_verdicts`): the chain
    accepts a verdict commit after the resolution too, when the outcome is public, and a day is ranked only
    once it is final, more than a day later -- a drawn juror who never voted could post the winning vote
    then and be paid for it. A verdict whose audit's resolution the index does not give is not counted
    either, and the published ranking says so, as for one whose commit cannot be dated (its juror's commits
    in the window outnumber what the search reads); one with no commit transaction in the day's window is
    neither counted nor listed (`timely_verdicts`).
  - presence: the availability windows each identity proved on chain during the day
    (`final_season_chain.presence_proofs`), counted once per window and per day.
  - UNDER DECISION 18 ONLY (`final_season_rules.pays_unwound_work`): the programme jobs settled during the day
    whose audit was UNWOUND -- no jury concluded it within the unwind bound, the chain refunded the client and
    neither paid nor slashed the miner (`+resolved+unwound`, `unwound_programme_jobs`). Each counts as one more
    verified request of its miner when its answer reached the programme AND the season's grading does not say
    incoherent -- its OWN answer's grade when it was sampled and graded, else the grading of its miner's day by
    the void rule (`unwound_work`); else it is not paid, and the published ranking says why, job by job.
FROM THE EVIDENCE LOG
  - which programme requests had their answer received (`answered_jobs`): only those count as work,
  - the payout address an identity declared (else its operator, read from the chain),
  - the grades of the sampled answers to programme requests: work is 0 for a day whose graded ones are
    all incoherent, and with it the presence of that day, which is paid only on a day of verified work.
"""
from __future__ import annotations

from final_season_calc import Identity
from final_season_rules import RULES, day_bounds

# A day's work is taken away only when every graded answer of the day was judged incoherent AND there are
# at least min(GRADES_TO_VOID, answers sampled) grades: an identity with one sampled answer can lose its
# day's work on one grade, one with more needs two.
GRADES_TO_VOID = 2

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
    `resolutions`: {job: (height, END_BLOCK | IN_TX) or None} for EVERY job of `commits`
                   (`final_season_chain.audit_resolution`);
    `committed`:   {job: {juror: height, None or NOT_DATED}} for EVERY verdict of `commits`: the block of its
                   accepted commit transaction, searched in the DAY'S WINDOW, from the draw to the day's final
                   block (`commit_height`, called by `final_season_rank.rank_day`); None when it is not there;
                   NOT_DATED when the juror's own commits in that window are more than the search reads.
    Indexed, never defaulted: a job or a verdict whose dates were not read is a KeyError, not a verdict.

    A verdict NOT_DATED is not counted and IS listed (`commit_not_dated`): its date is unknown, and unknown
    is never counted; and the juror chose it, by flooding the search with commits that need no job, so the
    day is ranked without it rather than not at all. Before this, the search raised, the day was deferred,
    and every later day with it: one juror stopped the season's rankings.

    A verdict counts only if its commit PRECEDES the resolution: in an earlier block, or -- the deadline's
    tally running after every transaction of its block -- in the block of an END_BLOCK resolution. Inside the
    block of an adjudication the order of transactions is not read, so only an earlier block counts there.
    Unknown is never counted: an audit whose resolution the index does not give counts none of its
    verdicts, and the proof says so for each. A verdict with no commit transaction in the day's window is not
    counted and NOT LISTED: it was anchored after the day became final (or before its jury was drawn), so it
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

    `verdicts`: {job_id: {juror_id: "0"|"1"}}, ALREADY limited to the jurors drawn for each audit and to the
    verdicts committed before its resolution (`timely_verdicts`).
    `answered`: `answered_jobs` of the evidence; a programme job outside it is not work.
    `last_height`: the day's last COUNTED block (`final_season_rank.season_window`): its full end, or the
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


# ── DECISION 18: the work of an unwound audit (final_season_rules.UNWOUND_AUDIT_WORK) ─────────────────────────
# Why an unwound programme job is not paid as work, as the published ranking names it (`unwound_work`). The
# first three are read from the chain's job, the others from the evidence log.
CONVICTED = "convicted"                    # a claw-back or a slash of its own miner on the job: never paid
UNEXPECTED_STATE = "unexpected_state"      # a state no unwind writes (never paid, never resolved, or a verdict)
NO_PRIMARY = "no_primary"                  # the job names no miner: nobody to credit
NOT_ANSWERED = "not_answered"              # its answer is in no seal of the evidence: it never reached the programme
ANSWER_UNREADABLE = "answer_unreadable"    # not in a seal, but a line of the evidence is unreadable: it may be there
# The grading that decided, as the published ranking names it (`unwound_work`, "graded"). The job's OWN answer
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
    never as a substring of another one. `slashed_primary` is INDEXED, never defaulted: on the criterion that
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
    """{job_id: {"miner_id": miner, "state": job_unwound(job)}} for every job whose CLIENT is the programme's
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
    """({miner_id: job ids sampled}, {miner_id: [coherent, ...]}) of `day`'s work answers and grades."""
    sampled, grades = {}, {}
    for rec in records_upto:
        if rec.get("day") != day:
            continue
        t, mid = rec.get("type"), rec.get("miner_id", "")
        if t == "work_answer":
            sampled.setdefault(mid, set()).add(rec.get("job_id", ""))
        elif t == "work_grade":
            grades.setdefault(mid, []).append(bool(rec.get("coherent")))
    return sampled, grades


def _own_grades(records_upto: list) -> dict:
    """{(job_id, miner_id): [coherent, ...]}: the grades of each sampled answer, whatever the day they are filed
    under (the service files a grade under the day its answer was sealed, `final_season_server._grading_result`).
    Only a literal `true` is coherent."""
    out = {}
    for rec in records_upto:
        if rec.get("type") == "work_grade":
            out.setdefault((rec.get("job_id", ""), rec.get("miner_id", "")), []).append(rec.get("coherent") is True)
    return out


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
    names above. ITS OWN ANSWER FIRST: a job sampled and graded is paid only if every grade of it says coherent,
    whatever the rest of its miner's day says -- a coherent grade of another answer says nothing of this one.
    Else its miner's day, by the void rule that takes a day's work away (`_graded_out`), and at least one grade.

    THREE STATES, NEVER TWO. An evidence line that could not be read (`_unreadable_lines`) may hold the grade that
    decides: with no grade of its own found, a torn line on the day its answer was sealed (any day when that seal
    is not found) makes the grading unreadable -- before any day-level reading, since the hidden line may be its
    own incoherent grade; and with no coherent grade of the day, a torn line of THAT day does too."""
    return _grading(jid, mid, day, _grading_reads(day, records_upto))


def _grading_reads(day: int, records_upto: list) -> tuple:
    """What `unwound_grading` reads, read once for every unwound job of the day."""
    torn = {r.get("day") for r in records_upto if r.get("type") == "_unreadable_lines"}
    return (_own_grades(records_upto), _sealed_on(records_upto), torn) + _work_grades(day, records_upto)


def _grading(jid: str, mid: str, day: int, reads: tuple) -> str:
    own_grades, sealed_on, torn, sampled, grades = reads
    own = own_grades.get((jid, mid), [])
    if own:
        return OWN_COHERENT if all(own) else OWN_INCOHERENT
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
           "why": <reason, only when not paid>}} for EVERY unwound programme job of the day, paid or not: the
           published ranking's record of the rule.

    A job is paid when the chain says unwound with no verdict against it (`job_unwound` == ''), its answer is in
    a seal (`answered`) AND its grading is one that pays (`unwound_grading`: its own answer graded coherent, or,
    not graded itself, its miner's day graded and not graded out). THREE STATES, NEVER TWO: an evidence line that
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
    """Every grade incoherent, and at least min(GRADES_TO_VOID, gradable) of them — never on no grade."""
    return len(grades) >= max(1, min(GRADES_TO_VOID, gradable)) and not any(grades)


def day_identities(day: int, records_upto: list, chain: dict, default_address: dict) -> list:
    """`records_upto`: every evidence record of days <= `day`, in file order.
    `chain`: {"work": {miner_id: n}, "verdicts": {miner_id: n}, "presence": {miner_id: windows}} for the day.
    Under decision 18 `work` already holds the unwound jobs `unwound_work` pays (`final_season_rank.rank_day`).
    `default_address`: {miner_id: operator} from the chain, used when the identity declared no address."""
    payout = {}
    for rec in records_upto:
        if rec.get("type") == "payout":
            payout[rec.get("miner_id", "")] = rec.get("address", "")
    work_sampled, work_grades = _work_grades(day, records_upto)
    # Indexed, never defaulted: a family of facts that was not read is not a family of zeros.
    work_of, verdicts_of, presence_of = chain["work"], chain["verdicts"], chain["presence"]
    out = []
    for mid in sorted(set(work_of) | set(verdicts_of) | set(presence_of)):
        work = work_of.get(mid, 0)
        if _graded_out(work_grades.get(mid, []), len(work_sampled.get(mid, set()))):
            work = 0
        out.append(Identity(mid, presence=int(presence_of.get(mid, 0)), verified_requests=work,
                            verdicts=verdicts_of.get(mid, 0),
                            payout_address=payout.get(mid) or default_address.get(mid, "")))
    return out
