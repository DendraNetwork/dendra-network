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
    ignores it.
  - presence: the availability windows each identity proved on chain during the day
    (`final_season_chain.presence_proofs`), counted once per window and per day.
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


def chain_day_facts(day: int, start_height: int, jobs: list, settle_heights: dict, verdicts: dict,
                    generator: str, finality_height: int, current_height: int, answered: set,
                    rules: dict | None = None, *, last_height: int) -> dict:
    """{"work": {miner_id: n}, "verdicts": {miner_id: n}} for one day, or raise if the day is not final.

    `verdicts`: {job_id: {juror_id: "0"|"1"}}, ALREADY limited to the jurors drawn for each audit.
    `answered`: `answered_jobs` of the evidence; a programme job outside it is not work.
    `last_height`: the day's last COUNTED block (`final_season_rank.season_window`): its full end, or the
    season's last block on the day the season ends. Required, with no default: a default would be the full
    day, and settlements after the season's end would be paid.
    """
    r = RULES if rules is None else rules
    first, nominal_last = day_bounds(day, start_height, r)
    if not first <= last_height <= nominal_last:
        raise ValueError(f"day {day}: last counted block {last_height} outside the day [{first}, {nominal_last}]")
    last = last_height
    if current_height < last + finality_height:
        raise ValueError(f"day {day} is not final before height {last + finality_height} "
                         f"(chain at {current_height})")
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


def _graded_out(grades: list, gradable: int) -> bool:
    """Every grade incoherent, and at least min(GRADES_TO_VOID, gradable) of them — never on no grade."""
    return len(grades) >= max(1, min(GRADES_TO_VOID, gradable)) and not any(grades)


def day_identities(day: int, records_upto: list, chain: dict, default_address: dict) -> list:
    """`records_upto`: every evidence record of days <= `day`, in file order.
    `chain`: {"work": {miner_id: n}, "verdicts": {miner_id: n}, "presence": {miner_id: windows}} for the day.
    `default_address`: {miner_id: operator} from the chain, used when the identity declared no address."""
    payout, work_sampled, work_grades = {}, {}, {}
    for rec in records_upto:
        t, d, mid = rec.get("type"), rec.get("day"), rec.get("miner_id", "")
        if t == "payout":
            payout[mid] = rec.get("address", "")
        if d != day:
            continue
        if t == "work_answer":
            work_sampled.setdefault(mid, set()).add(rec.get("job_id", ""))
        elif t == "work_grade":
            work_grades.setdefault(mid, []).append(bool(rec.get("coherent")))
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
