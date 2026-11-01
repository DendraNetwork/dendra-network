#!/usr/bin/env python3
"""Tests for c3_jury_forensics — no dependencies. `python3 test_c3_jury_forensics.py`

The core of this suite: the measurement must distinguish (a) missing verdicts from (b) late verdicts,
and above all refuse to decide when the data does not separate them. A measurement that always
answered "absent" would be one that cannot contradict anything.
"""
import sys

from c3_jury_forensics import (ABSENT, ALL_CLOSED, ANOMALY, LATE, UNDECIDED, analyse, bar_for,
                               classify, render)

OK = [0]
FAIL = []


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


def ev(t, h, **attrs):
    return {"type": t, "height": h, "attrs": {k: str(v) for k, v in attrs.items()}}


def job(jid, state="open+paid+optimistic+disputed", miner="m0", dh=1000):
    return {"job_id": jid, "state": state, "miner_id": miner, "dispute_height": str(dh)}


SEATS7 = ["j1", "j2", "j3", "j4", "j5", "g1", "g2"]

# --- bar: the exact rule of the chain ------------------------------------------------------------
chk("(0) bar = max(min_quorum, ceil(2/3 seats)): 7 seats & quorum 4 -> 5", bar_for(7, 4) == 5)
chk("(0b) 6 seats -> 4 (the governed quorum dominates)", bar_for(6, 4) == 4)
chk("(0c) 15 seats -> 10 (the relative bar dominates)", bar_for(15, 4) == 10)
chk("(0d) 0 seats -> None (undecidable, never 0)", bar_for(0, 4) is None)

# --- (1) flat voter counts across several attempts => ABSENT --------------------------------------
v, why = classify("j", 7, 3, [{"height": 1240, "voters": 3, "seats": 7},
                              {"height": 1480, "voters": 3, "seats": 7},
                              {"height": 1720, "voters": 3, "seats": 7}], 5)
chk("(1) flat voters 3/3/3 and nothing new -> VERDICTS_ABSENT", v == ABSENT, why)

# --- (2) rising voter counts => LATE ---------------------------------------------------------------
v, why = classify("j", 7, 4, [{"height": 1240, "voters": 2, "seats": 7},
                              {"height": 1480, "voters": 4, "seats": 7}], 5)
chk("(2) voters 2 -> 4 gives VERDICTS_LATE", v == LATE, why)

# --- (3) votes arrived since the last attempt => LATE, even on a short series ----------------------
v, why = classify("j", 7, 4, [{"height": 1240, "voters": 2, "seats": 7}], 5)
chk("(3) 4 votes now against 2 at the last attempt -> LATE", v == LATE, why)

# --- (4) enough votes and still open => ANOMALY (neither (a) nor (b)) ------------------------------
v, why = classify("j", 7, 5, [{"height": 1240, "voters": 5, "seats": 7}], 5)
chk("(4) votes >= bar and audit still open -> ANOMALY (the deferral should have resolved)", v == ANOMALY, why)

# --- (5) the case that matters: no series => undecidable, never "absent" ---------------------------
v, why = classify("j", 7, 2, [], 5)
chk("(5) no event series -> undecidable (do not conclude 'absent' by default)",
    v == UNDECIDED, why)
v, why = classify("j", 7, 2, [{"height": 1240, "voters": 2, "seats": 7}], 5)
chk("(5b) a single attempt -> undecidable (two deadlines minimum to distinguish)", v == UNDECIDED, why)
v, why = classify("j", 0, 0, [], None)
chk("(5c) unknown seat count -> undecidable", v == UNDECIDED, why)

# --- (6) end to end: cause (a), and it names the silent jurors -------------------------------------
cap_a = {
    "jobs": [job(f"p{i}") for i in range(3)],
    "params": {"params": {"audit_min_quorum": "4", "audit_resolve_timeout": "240"}},
    "committees": {},   # assigned-committee is the inference committee: no longer used here
    "verdicts": {f"p{i}": ["j1", "j2", "j3"] for i in range(3)},
    "events": [e for i in range(3) for e in
               (ev("audit_requested", 1200 + i, job_id=f"p{i}", committee=",".join(SEATS7)),
                ev("audit_resolve_deferred", 1240 + i, job_id=f"p{i}", voters=3, seats=7),
                ev("audit_resolve_deferred", 1480 + i, job_id=f"p{i}", voters=3, seats=7))],
    "events_scanned": 6, "height": 2000,
}
a = analyse(cap_a)
chk("(6) 3 flat audits -> global VERDICTS_ABSENT", a["global"]["verdict"] == ABSENT)
chk("(6b) jurors that never voted are named in the action",
    all(m in a["global"]["action"] for m in ("j4", "j5", "g1", "g2")), a["global"]["action"])
chk("(6c) per-juror matrix: j1 votes 3/3, g2 votes 0/3",
    {x["miner"]: (x["summoned"], x["voted"]) for x in a["jurors"]}["j1"] == (3, 3)
    and {x["miner"]: (x["summoned"], x["voted"]) for x in a["jurors"]}["g2"] == (3, 0))
chk("(6d) the action states explicitly that widening the window repairs nothing",
    "repair nothing" in a["global"]["action"])

# --- (7) end to end: cause (b) -> the action names audit_resolve_timeout ---------------------------
cap_b = dict(cap_a)
cap_b["verdicts"] = {f"p{i}": ["j1", "j2", "j3", "j4"] for i in range(3)}
cap_b["events"] = [e for i in range(3) for e in
                   (ev("audit_requested", 1200 + i, job_id=f"p{i}", committee=",".join(SEATS7)),
                    ev("audit_resolve_deferred", 1240 + i, job_id=f"p{i}", voters=1, seats=7),
                    ev("audit_resolve_deferred", 1480 + i, job_id=f"p{i}", voters=3, seats=7))]
b = analyse(cap_b)
chk("(7) rising voter counts -> global VERDICTS_LATE", b["global"]["verdict"] == LATE)
chk("(7b) the action names audit_resolve_timeout and the value it read",
    "audit_resolve_timeout" in b["global"]["action"] and "240" in b["global"]["action"])
chk("(7c) and recalls that widening it does not accept fewer verdicts",
    "no fewer verdicts" in b["global"]["action"])

# --- (8) unreadable events (None) -> undecidable + "change nothing" -------------------------------
cap_n = dict(cap_a)
cap_n["events"] = None
n = analyse(cap_n)
chk("(8) events=None -> undecidable (never a default conclusion)",
    n["global"]["verdict"] == UNDECIDED)
chk("(8b) and the action says change nothing", "Change nothing" in n["global"]["action"])

# --- (9) an off-seat vote does not count (invariant: voting requires having been drawn) -----------
cap_o = dict(cap_a)
cap_o["verdicts"] = {f"p{i}": ["j1", "j2", "j3", "intruder"] for i in range(3)}
o = analyse(cap_o)
chk("(9) a voter outside the anchored committee is ignored in the count",
    len(o["per_job"]["p0"]["voted"]) == 3 and o["per_job"]["p0"]["votes_off_seat_ignored"] == ["intruder"])

# --- (10) ANOMALY takes precedence over everything else -------------------------------------------
cap_x = dict(cap_a)
cap_x["verdicts"] = {"p0": SEATS7[:6], "p1": ["j1", "j2", "j3"], "p2": ["j1", "j2", "j3"]}
cap_x["events"] = [ev("audit_requested", 1200, job_id="p0", committee=",".join(SEATS7)),
                   ev("audit_resolve_deferred", 1240, job_id="p0", voters=6, seats=7),
                   ev("audit_resolve_deferred", 1480, job_id="p0", voters=6, seats=7)] + [
                  e for i in (1, 2) for e in
                  (ev("audit_requested", 1200 + i, job_id=f"p{i}", committee=",".join(SEATS7)),
                   ev("audit_resolve_deferred", 1240 + i, job_id=f"p{i}", voters=3, seats=7),
                   ev("audit_resolve_deferred", 1480 + i, job_id=f"p{i}", voters=3, seats=7))]
x = analyse(cap_x)
chk("(10) one above-quorum audit still open forces the global verdict to ANOMALY",
    x["global"]["verdict"] == ANOMALY)
chk("(10b) and the action says to investigate before any threshold change",
    "Investigate before arbitrating" in x["global"]["action"])

# --- (11) missing jobs -> _measured False, no invented report --------------------------------------
chk("(11) jobs=None -> _measured False", analyse({"jobs": None})["_measured"] is False)

# --- (12) a resolved job is not an open audit ------------------------------------------------------
cap_r = dict(cap_a)
cap_r["jobs"] = [job("r0", "open+paid+optimistic+disputed+resolved+vindicated+quorum")]
chk("(12) resolved jobs are excluded from the analysis", analyse(cap_r)["context"]["pending_audits"] == 0)

# --- (13) the chain's witness prevails, and a divergence is reported -------------------------------
# The defect this pins: the tool counted 2 votes (on the wrong committee) while the chain wrote
# voters=4/seats=7, and it displayed both without flinching.
cap_d = {
    "jobs": [job("d0")],
    "params": {"params": {"audit_min_quorum": "4", "audit_resolve_timeout": "240"}},
    "committees": {},                      # assigned-committee is no longer used
    "verdicts": {"d0": ["j1", "j2"]},      # 2 commits readable on the client side
    "events": [ev("audit_requested", 5000, job_id="d0", committee=",".join(SEATS7)),
               ev("audit_resolve_deferred", 5469, job_id="d0", voters=4, seats=7),
               ev("audit_resolve_deferred", 5709, job_id="d0", voters=4, seats=7)],
    "events_scanned": 3, "height": 6000,
}
d = analyse(cap_d)
v = d["per_job"]["d0"]
chk("(13) the anchored jury is read from audit_requested (7 seats), not from assigned-committee",
    v["anchored_seats"] == 7 and len(v["seats"]) == 7, str(v["anchored_seats"]))
chk("(13b) the bar follows the chain's seats: max(4, ceil(2/3*7)) = 5", v["bar"] == 5)
chk("(13c) the client(2) vs chain(4) divergence is reported",
    v["witnesses_agree"] is False and "; divergence:" in v["why"], v["why"][-90:])
chk("(13d) the verdict follows the chain (4 voters, flat) -> ABSENT, gap to the bar = 1",
    v["verdict"] == ABSENT and v["gap_to_bar"] == 1, f"{v['verdict']} gap={v['gap_to_bar']}")
chk("(13e) and when both witnesses agree, no noise",
    analyse({**cap_d, "verdicts": {"d0": SEATS7[:4]}})["per_job"]["d0"]["witnesses_agree"] is True)

# --- (14) 6 seats: the bar falls back to the governed quorum (4), 4 voters reach it in number ------
cap_6 = {**cap_d,
         "events": [ev("audit_requested", 5000, job_id="d0", committee=",".join(SEATS7[:6])),
                    ev("audit_resolve_deferred", 5469, job_id="d0", voters=4, seats=6),
                    ev("audit_resolve_deferred", 5709, job_id="d0", voters=4, seats=6)]}
c6 = analyse(cap_6)["per_job"]["d0"]
chk("(14) 6 seats -> bar 4; 4 voters reach it in number -> ANOMALY (the 4 must be on the same side)",
    c6["bar"] == 4 and c6["verdict"] == ANOMALY, f"{c6['bar']} {c6['verdict']}")

# --- (15) before/after comparison of a repair -----------------------------------------------------
from c3_jury_forensics import compare
_before = {"_measured": True, "per_job": {"a": {}, "b": {}, "c": {}},
           "jurors": [{"miner": "judge0", "rate": 0.0}, {"miner": "judge1", "rate": 0.76}]}
_after = {"_measured": True, "per_job": {"c": {}},
          "jurors": [{"miner": "judge0", "rate": 0.9}, {"miner": "judge1", "rate": 0.78}]}
d = compare(_before, _after)
chk("(15) 2 closed audits + a juror moving from 0% to 90% -> repair confirmed",
    d["verdict"] == "repair confirmed" and len(d["audits_closed"]) == 2
    and d["jurors_revived"] == ["judge0"], str(d.get("verdict")))
chk("(15b) nothing closed and nobody woken up -> nothing moved",
    compare(_before, _before)["verdict"] == "nothing moved")
chk("(15c) an unmeasured report -> comparison refused, not a false pass",
    compare({"_measured": False}, _after)["_measured"] is False)

# --- (16) 0 open audit: the target state must not read as a measurement failure --------------------
# The defect this pins: with every audit closed, the tool returned undecidable and exit code 3, i.e.
# the best possible state was reported as "I could not conclude". A tool that cannot say "there is
# nothing left to diagnose" pushes the reader to hunt a failure that no longer exists.
cap_closed = {"height": 15040, "params": {"audit_min_quorum": 4, "audit_resolve_timeout": 240},
            "jobs": [job("z0", state="open+paid+optimistic+adjudicated")],
            "events": [ev("audit_requested", 5000, job_id="z0", committee=",".join(SEATS7)),
                       ev("audit_resolve_deferred", 5100, job_id="z0", voters=5, seats=7)]}
a16 = analyse(cap_closed)
chk("(16) 0 open audit but audits observed -> ALL_CLOSED (the target state, not an undecidable)",
    a16["global"]["verdict"] == ALL_CLOSED, str(a16["global"]["verdict"]))
chk("(16b) and the count of observed audits is named in the action",
    "1 audits" in a16["global"]["action"], a16["global"]["action"][:60])
cap_empty = {"height": 15040, "params": {"audit_min_quorum": 4, "audit_resolve_timeout": 240},
            "jobs": [], "events": []}
chk("(16c) 0 open audit and no event -> undecidable (absence of measurement, not a success)",
    analyse(cap_empty)["global"]["verdict"] == UNDECIDED)
# (16d) The per-juror matrix covers all anchored audits, not only the open ones. Otherwise a
# successful repair erases the evidence that it succeeded — the audits close, the matrix empties, and
# a woken juror becomes indistinguishable from a juror that never existed.
chk("(16d) closed audit: the juror's participation survives the closure (the repair stays provable)",
    len(a16["jurors"]) == len(SEATS7) and all(j["summoned"] == 1 for j in a16["jurors"]),
    f"{len(a16['jurors'])} jurors, {len(SEATS7)} expected")
chk("(16d2) ...and the rendering states what the matrix covers",
    "closed ones included" in render(a16))
chk("(16d3) a truly empty matrix (no audit_requested) is reported as an absence of measurement",
    "absence of measurement" in render(analyse(cap_empty)))
chk("(16e) unreadable events -> still undecidable, never ALL_CLOSED (fail closed holds)",
    analyse({"height": 1, "params": {"audit_min_quorum": 4, "audit_resolve_timeout": 240},
             "jobs": [], "events": None})["global"]["verdict"] == UNDECIDED)

# --- (17) a repair is not latency -----------------------------------------------------------------
# The defect this pins: after three silent seats are repaired by hand, the series goes from flat to
# rising and the tool concludes "widen audit_resolve_timeout" — the exact opposite of the remedy that
# just worked. Latency and an operational outage have opposite remedies: distinguishing them is the
# minimum, averaging them is harmful.
from c3_jury_forensics import DISCONTINUITY
_e = lambda v: {"voters": str(v), "seats": "7"}
chk("(17) flat then a jump -> DISCONTINUITY (something changed, this is not latency)",
    classify("j", 7, 4, [_e(2), _e(2), _e(2), _e(4)], 5)[0] == DISCONTINUITY)
chk("(17b) ...and the reason explicitly refuses the wrong remedy",
    "lengthening the window would repair nothing" in classify("j", 7, 4, [_e(2), _e(2), _e(2), _e(4)], 5)[1])
chk("(17c) a steady rise -> LATE (real latency is still diagnosed)",
    classify("j", 7, 4, [_e(2), _e(3), _e(4)], 5)[0] == LATE)
chk("(17d) flat without a jump -> ABSENT (unchanged)",
    classify("j", 7, 4, [_e(4), _e(4), _e(4)], 5)[0] == ABSENT)
chk("(17e) only 2 points: not enough history to claim a discontinuity",
    classify("j", 7, 3, [_e(2), _e(3)], 5)[0] == LATE)

print(f"\n{OK[0]} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
