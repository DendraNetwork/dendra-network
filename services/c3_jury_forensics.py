#!/usr/bin/env python3
"""
c3_jury_forensics.py — are the missing verdicts ABSENT or LATE?

On audits that stay OPEN, two causes call for OPPOSITE remedies.
  (a) the verdicts NEVER exist (worker stopped, model missing, miner silent) -> an OPERATIONS
      problem; lengthening the window would repair nothing.
  (b) the verdicts arrive AFTER the deadline (GPU/judge latency) -> the window is too short;
      lengthening `audit_resolve_timeout` weakens NOTHING (it waits longer for a REAL verdict, it
      does not accept fewer of them).
Confusing the two lowers security in order to fix a latency problem.

THIS MEASUREMENT IS NOT RECONSTRUCTED: THE CHAIN ALREADY MAKES IT. On every resolution retry,
`resolveDisputedAudit` emits `audit_resolve_deferred{job_id, voters, seats}` (audit_sampling.go). Each
open audit therefore carries a TIME SERIES of "how many jurors had voted at that moment". That is
exactly the discriminant required, written by the chain itself:
  - `voters` RISING from one retry to the next -> verdicts ARE arriving, late          => cause (b)
  - `voters` FLAT across several retries       -> nobody new is voting                 => cause (a)
  - `voted_now > voters` of the LAST retry     -> votes have arrived since             => (b) active
  - `voted_now >= bar` while the job is still open -> ANOMALY (the deferral should have resolved it
    on the next retry): neither (a) nor (b), a defect to investigate.

SOURCE: only the interfaces SERVED BY THE NODE (REST :1317 plus CometBFT RPC :26657), never the local
CLI. A stale `dendrad` fabricates "missing queries" and drops the fields it does not know about; the
measurement then describes the age of the WORKSTATION while claiming to describe the CHAIN.

The report MUST be able to say "I cannot decide": with no event series, it does NOT conclude
"absent" — that would be a measurement able to return only one answer.

Usage:
  python3 c3_jury_forensics.py --host api.dendranetwork.com [--out artifact.json] [--json]
  python3 c3_jury_forensics.py --from-json capture.json      # offline replay
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

ABSENTS, LATE, ANOMALY, UNDECIDED = "VERDICTS_ABSENT", "VERDICTS_LATE", "ANOMALY", "UNDECIDABLE"
# "0 open audit" on a chain THAT HAS AUDITED is the TARGET state, not a failed measurement. Reporting
# it as UNDECIDABLE (exit 3) makes the best possible state read as a measurement failure. A diagnostic
# tool must be able to say "there is nothing left to diagnose", and distinguish that from "I could see
# nothing".
ALL_CLOSED = "ALL_CLOSED"
# A flat series that JUMPS is an intervention, not latency. Confusing the two recommends "lengthen the
# window" where somebody has just repaired a worker — the exact opposite remedy.
DISCONTINU = "DISCONTINUITY"
EV_DEFER_RESOLVE = "audit_resolve_deferred"
EV_DEFER_DRAW = "audit_draw_deferred"
EV_REQUESTED = "audit_requested"   # carries the ANCHORED JURY (committee=...) — the authority


def _g(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def tokens(job):
    return (_g(job, "state", "State", default="") or "").split("+")


def job_id_of(job):
    return str(_g(job, "job_id", "jobId", "id", default="") or "")


def bar_for(seats, audit_min_quorum):
    """The chain's EXACT bar: max(audit_min_quorum, ceil(2/3 x anchored seats)).

    Copied from `auditSlashDecision` (antievasion.go) — and this is the ONLY place where this file
    reproduces a chain rule. It is here because the report must STATE how far from the bar each audit
    sits; every decision remains the one the chain wrote.
    """
    if seats <= 0:
        return None
    return max(_int(audit_min_quorum), -(-2 * seats // 3))


def classify(job_id, seats, voted_now, series, bar):
    """PURE. `series` = [{height, voters, seats}] sorted by ascending height.

    `voted_now` and `bar` MUST come from the chain's own figures whenever those exist. Feeding this
    function a client-side count (2 votes over a "committee" of 3) while the chain wrote
    `voters=4, seats=7` means the client is holding a model of the protocol: it read the INFERENCE
    committee (`assigned-committee`, members[0] = the primary) instead of the anchored AUDIT JURY.
    """
    if bar is None:
        return UNDECIDED, "anchored seats unknown — nothing to compare"
    if voted_now >= bar:
        return ANOMALY, (f"{voted_now} votes >= bar {bar} and the audit is STILL open: the deferral "
                         "should have resolved it on the next retry. Neither (a) nor (b) — a defect to investigate")
    if not series:
        return UNDECIDED, ("no `audit_resolve_deferred` event found for this job: either the audit has "
                           "not reached its first deadline yet, or the scanned block window does not "
                           "cover it. \"Absent\" is NOT concluded for lack of a series")
    last = series[-1]
    lv = _int(_g(last, "voters"))
    if voted_now > lv:
        return LATE, (f"{voted_now} votes today against {lv} at the last retry (h={_g(last,'height')}) "
                      "— verdicts arrived AFTER the deadline: the window is too short")
    if len(series) >= 2:
        vs = [_int(_g(p, "voters")) for p in series]
        # A REPAIR LOOKS LIKE LATENCY, and the remedy it suggests is the opposite of the right one. A
        # series FLAT across several deadlines that then JUMPS is not "late verdicts": it is a
        # DISCONTINUITY — something changed in between (worker restarted, juror repaired, machine
        # back). After three silent seats are fixed by hand, a latency reading would conclude
        # "lengthen `audit_resolve_timeout`", which would repair STRICTLY nothing. The remedy for
        # latency and the remedy for an operational outage are opposites: they must be distinguished,
        # not averaged.
        if len(vs) >= 3 and len(set(vs[:-1])) == 1 and vs[-1] > vs[-2]:
            return DISCONTINU, (f"voters FLAT at {vs[0]} across {len(vs)-1} deadlines then a JUMP to {vs[-1]}: "
                                "something CHANGED in between (restart, repair, juror back). "
                                "This is NOT latency — lengthening the window would repair nothing. "
                                "Re-measure after stabilization before arbitrating any threshold")
        if vs[-1] > vs[0]:
            return LATE, (f"voters {vs[0]} -> {vs[-1]} across {len(series)} retries: the verdicts do arrive, "
                          "too late for each deadline taken in isolation")
        if len(set(vs)) == 1:
            return ABSENTS, (f"voters FLAT at {vs[0]} across {len(series)} retries and nothing new since: "
                             f"{bar - voted_now} verdict(s) missing that NEVER come")
        return UNDECIDED, f"non-monotonic series {vs} — neither clear progress nor stagnation"
    return UNDECIDED, (f"a single retry observed (voters={lv}): at least 2 deadlines are needed to tell "
                       "\"never comes\" from \"comes late\"")


def analyse(cap):
    """PURE. cap = {jobs, params, committees:{jid:[seats]}, verdicts:{jid:[minerIds]},
    events:[{type,height,attrs}], height}. Returns the structured report."""
    jobs = cap.get("jobs")
    if jobs is None:
        return {"_measured": False, "_why": "job list missing: NOTHING is measurable."}
    params = (cap.get("params") or {}).get("params") or cap.get("params") or {}
    amq = _int(_g(params, "audit_min_quorum", "auditMinQuorum"))
    timeout = _int(_g(params, "audit_resolve_timeout", "auditResolveTimeout"))
    committees = cap.get("committees") or {}
    verdicts = cap.get("verdicts") or {}
    events = cap.get("events")
    scanned = cap.get("events_scanned")

    # per-job series, built from the event the CHAIN emits on every retry
    series = {}
    draw_deferred = {}
    anchored = {}   # ANCHORED jury, read from audit_requested
    for e in (events or []):
        jid = str(_g(e.get("attrs") or {}, "job_id", default="") or "")
        if not jid:
            continue
        if e.get("type") == EV_DEFER_RESOLVE:
            series.setdefault(jid, []).append({"height": _int(e.get("height")),
                                               "voters": _int(_g(e["attrs"], "voters")),
                                               "seats": _int(_g(e["attrs"], "seats"))})
        elif e.get("type") == EV_DEFER_DRAW:
            draw_deferred.setdefault(jid, []).append(_int(e.get("height")))
        elif e.get("type") == EV_REQUESTED:
            anchored[jid] = [m for m in str(_g(e["attrs"], "committee", default="") or "").split(",") if m]
    for v in series.values():
        v.sort(key=lambda x: x["height"])

    pending, per_job = [], {}
    juror_seen, juror_voted = {}, {}
    for j in jobs:
        tk = tokens(j)
        if "disputed" not in tk or "resolved" in tk:
            continue
        jid = job_id_of(j)
        pending.append(jid)
        sr = series.get(jid, [])
        # AUTHORITY: the ANCHORED jury comes from `audit_requested`, and the seat count from the
        # deferral event. `assigned-committee` is the INFERENCE committee — using it here yields
        # "2 votes over 3 seats" where the chain wrote "4 voters over 7 seats".
        seats = list(anchored.get(jid) or [])
        seats_chain = _int(_g(sr[-1], "seats")) if sr else (len(seats) or 0)
        voters_chain = _int(_g(sr[-1], "voters")) if sr else None
        voters_ids = sorted(set(verdicts.get(jid) or []))
        voted_seats = [m for m in voters_ids if m in seats] if seats else []
        off_seat = [m for m in voters_ids if seats and m not in seats]
        b = bar_for(seats_chain, amq)
        # TWO WITNESSES: the client-side commit count against the chain's counter. The chain is
        # authoritative; a divergence is INFORMATION (the client model is wrong), never a display
        # detail.
        n_client = len(voted_seats) if seats else None
        n_used = voters_chain if voters_chain is not None else (n_client or 0)
        diverge = (voters_chain is not None and n_client is not None and n_client != voters_chain)
        verdict, why = classify(jid, seats_chain, n_used, sr, b)
        if diverge:
            why += (f" DIVERGENCE: {n_client} vote(s) counted from the commits, the chain writes "
                    f"{voters_chain} — the client model is the wrong one, the verdict follows the chain")
        # (the per-juror count is not done here: it would cover only OPEN audits — see the loop over
        #  "ALL anchored audits" below, and the defect it repairs)
        per_job[jid] = {
            "primary": str(_g(j, "miner_id", "minerId", default="") or ""),
            "dispute_height": _int(_g(j, "dispute_height", "disputeHeight")),
            "anchored_seats": seats_chain, "seats": seats,
            "voted": voted_seats, "missing": [m for m in seats if m not in voted_seats],
            "votes_off_seat_ignored": off_seat,
            "voters_chain": voters_chain, "voters_client": n_client, "witnesses_agree": not diverge,
            "bar": b, "gap_to_bar": None if b is None else max(0, b - n_used),
            "deferral_series": series.get(jid, []),
            "draw_deferred_heights": draw_deferred.get(jid, []),
            "verdict": verdict, "why": why,
        }

    # --- per-JUROR aggregation, over ALL ANCHORED AUDITS (not only the open ones) ------------------
    # Counting only OPEN audits has a perverse consequence: a successful repair erases the proof that
    # it succeeded. The audits close, the matrix empties, and a juror that was just brought back to
    # life becomes indistinguishable from one that never existed — an empty matrix while 8 jurors had
    # voted across 38 audits. The count therefore runs over `anchored` (every `audit_requested`
    # observed): participation history survives closure, which is precisely what is worth measuring
    # about a juror.
    for jid_all, seats_all in anchored.items():
        voted_all = set(verdicts.get(jid_all) or [])
        for m in seats_all:
            juror_seen[m] = juror_seen.get(m, 0) + 1
            if m in voted_all:
                juror_voted[m] = juror_voted.get(m, 0) + 1
    jurors = []
    # The keys below are the REPORT FORMAT, not prose: `c3_jury_model.py --from-forensics` reads them
    # back, and so does every report already written to disk. Renaming them is a format break, and a
    # silent one: the reader defaults a missing rate to 0, so an old report would come back as a jury
    # that never votes instead of failing loudly.
    for m in sorted(juror_seen):
        seen, voted = juror_seen[m], juror_voted.get(m, 0)
        jurors.append({"miner": m, "convoqué": seen, "a_voté": voted,
                       "taux": round(voted / seen, 3) if seen else None,
                       "silencieux_total": voted == 0})

    counts = {}
    for v in per_job.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1

    # --- GLOBAL VERDICT, with a NAMED criterion and the right not to decide -----------------------
    decided = counts.get(ABSENTS, 0) + counts.get(LATE, 0)
    if events is None:
        glob, action = UNDECIDED, ("the chain events could not be read (RPC block_search silent): "
                                   "without them, \"absent\" would be a default conclusion. "
                                   "CHANGE NOTHING — restore RPC access first.")
    elif not per_job:
        audited = len({(e.get("attrs") or {}).get("job_id") for e in (events or [])
                       if (e.get("attrs") or {}).get("job_id")})
        if audited:
            glob, action = ALL_CLOSED, (f"no open audit: the {audited} audits seen in the chain events are "
                                        "ALL resolved. This is the target state — there is no silent seat "
                                        "left to diagnose, and nothing to correct here.")
        else:
            glob, action = UNDECIDED, ("no open audit AND no audit event read: the chain has nothing to "
                                       "show. This is NOT a success, it is an absence of measurement.")
    elif counts.get(ANOMALY, 0):
        glob, action = ANOMALY, (f"{counts[ANOMALY]} audit(s) have enough votes AND remain open: this is "
                                 "not a participation problem, it is a defect in the resolution path. "
                                 "Investigate BEFORE arbitrating any threshold.")
    elif decided == 0:
        glob, action = UNDECIDED, ("no audit provides 2 or more observed deadlines: the measurement is "
                                   "not conclusive yet. Re-run later — CHANGE NOTHING.")
    elif counts.get(LATE, 0) > counts.get(ABSENTS, 0):
        glob, action = LATE, ("the verdicts DO ARRIVE, after the deadline => cause (b): "
                              f"`audit_resolve_timeout` (={timeout} blocks) is too short. "
                              "Lengthening it accepts NO fewer verdicts, it waits longer for them.")
    elif counts.get(ABSENTS, 0) > counts.get(LATE, 0):
        silent = [j["miner"] for j in jurors if j["silencieux_total"]]
        glob, action = ABSENTS, ("the verdicts NEVER COME => cause (a): an OPERATIONS problem. "
                                 + (f"Jurors that NEVER vote: {', '.join(silent)}. " if silent else
                                    "No juror is fully silent: participation is partial across the board. ")
                                 + "Lengthening the window would repair NOTHING.")
    else:
        glob, action = UNDECIDED, (f"as many \"absent\" audits as \"late\" ones ({counts.get(ABSENTS,0)} "
                                   f"vs {counts.get(LATE,0)}): both causes coexist, no single fix follows. "
                                   "CHANGE NOTHING without more data.")

    return {"_measured": True,
            "context": {"height": cap.get("height"), "audit_min_quorum": amq,
                        "audit_resolve_timeout": timeout, "events_scanned": scanned,
                        "pending_audits": len(per_job)},
            "global": {"verdict": glob, "action": action, "counts": counts},
            "jurors": jurors, "per_job": per_job}


def compare(before, after):
    """Are the audits closing again? Compares two reports (before / after a repair).

    Returns facts, not a judgement: closed audits, new audits, and the per-juror participation DELTA.
    A juror moving from 0 % to above 0 % is the proof that the repair took hold.
    """
    if not (before.get("_measured") and after.get("_measured")):
        return {"_measured": False, "_why": "one of the two reports measured nothing: comparison impossible."}
    pb, pa = before.get("per_job") or {}, after.get("per_job") or {}
    closed = sorted(set(pb) - set(pa))
    new = sorted(set(pa) - set(pb))
    still = sorted(set(pb) & set(pa))
    rb = {j["miner"]: j for j in (before.get("jurors") or [])}
    ra = {j["miner"]: j for j in (after.get("jurors") or [])}
    deltas = []
    for m in sorted(set(rb) | set(ra)):
        b = (rb.get(m) or {}).get("taux")
        a = (ra.get(m) or {}).get("taux")
        deltas.append({"miner": m, "taux_avant": b, "taux_apres": a,
                       "reveil": (b in (0, 0.0) or b is None) and isinstance(a, (int, float)) and a > 0})
    woke = [d["miner"] for d in deltas if d["reveil"]]
    return {"_measured": True,
            "audits_clos": closed, "audits_encore_ouverts": still, "audits_nouveaux": new,
            "jurors_delta": deltas, "jures_reveilles": woke,
            "verdict": ("REPAIR CONFIRMED" if closed and woke else
                        "NOTHING MOVED" if not closed and not woke else
                        "PARTIAL — read line by line"),
            "why": (f"{len(closed)} audit(s) closed since the reference measurement; "
                    f"juror(s) revived: {', '.join(woke) or 'none'}; "
                    f"{len(still)} still open.")}


# ------------------------------------------------------------------ I/O: the NODE only
def _get(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _rest_all(rest, path, field, max_pages=400):
    out, key, n = [], None, 0
    while n < max_pages:
        u = f"{rest}/dendra/jobs/v1/{path}?pagination.limit=500" + (f"&pagination.key={urllib.parse.quote(key)}" if key else "")
        d = _get(u, 120)
        if d is None:
            return None
        out.extend(_g(d, field, default=[]) or [])
        key = _g(d.get("pagination") or {}, "next_key", "nextKey")
        n += 1
        if not key:
            return out
    return None


def scan_events(rpc, types=(EV_DEFER_RESOLVE, EV_DEFER_DRAW, EV_REQUESTED)):
    """Finds the blocks carrying the events, then READS their attributes (block_results).

    `block_search` returns heights, not attributes: two calls are required. When block_search fails
    this returns None, never [] — an empty list would read as "nothing happened" when it means
    "I could not look".
    """
    heights, scanned = set(), 0
    for t in types:
        page, total = 1, None
        while True:
            q = urllib.parse.quote(f'"{t}.job_id EXISTS"')
            d = _get(f"{rpc}/block_search?query={q}&per_page=100&page={page}&order_by=%22asc%22", 60)
            if d is None or "result" not in d:
                return None, 0
            r = d["result"]
            total = _int(r.get("total_count"))
            for b in (r.get("blocks") or []):
                heights.add(_int(((b.get("block") or {}).get("header") or {}).get("height")))
            if page * 100 >= total or not (r.get("blocks") or []):
                break
            page += 1
    evs = []
    for h in sorted(heights):
        d = _get(f"{rpc}/block_results?height={h}", 60)
        if d is None:
            continue
        scanned += 1
        for e in ((d.get("result") or {}).get("finalize_block_events") or []):
            if e.get("type") in types:
                attrs = {a.get("key"): a.get("value") for a in (e.get("attributes") or [])}
                evs.append({"type": e.get("type"), "height": h, "attrs": attrs})
    return evs, scanned


def gather(host):
    rest, rpc = f"http://{host}:1317", f"http://{host}:26657"
    jobs = _rest_all(rest, "job", "job")
    params = _get(f"{rest}/dendra/jobs/v1/params")
    commits = _rest_all(rest, "commit", "commit") or []
    verdicts = {}
    for c in commits:
        key = str(_g(c, "job_id", "jobId", default="") or "")
        if "__verdict__" in key:
            jid, _, mid = key.partition("__verdict__")
            if str(_g(c, "result_commit", "resultCommit", default="") or "").strip() in ("0", "1"):
                verdicts.setdefault(jid, []).append(mid)
    # `assigned-committee` is NOT the audit jury (it is the inference committee, members[0] = the
    # primary). The anchored jury is read from the `audit_requested` event, so it is not queried here.
    committees = {}
    st = _get(f"{rpc}/status", 20)
    height = _int((((st or {}).get("result") or {}).get("sync_info") or {}).get("latest_block_height"))
    events, scanned = scan_events(rpc)
    return {"jobs": jobs, "params": params, "verdicts": verdicts, "committees": committees,
            "events": events, "events_scanned": scanned, "height": height,
            "_source": f"node {host} (REST 1317 + RPC 26657)"}


def render(a):
    if not a.get("_measured"):
        return f"MEASUREMENT IMPOSSIBLE: {a.get('_why')}"
    c, g = a["context"], a["global"]
    L = ["=" * 78,
         "  VERDICTS ABSENT or LATE? (read from the event the CHAIN emits)", "=" * 78,
         f"  height={c['height']}  open audits={c['pending_audits']}  "
         f"audit_min_quorum={c['audit_min_quorum']}  audit_resolve_timeout={c['audit_resolve_timeout']}",
         f"  event blocks read: {c['events_scanned']}", "",
         "-- PER JUROR (who is summoned, who votes)",
         f"   {'miner':<16}{'summoned':>10}{'voted':>9}{'rate':>8}"]
    for j in a["jurors"]:
        flag = "   <- NEVER" if j["silencieux_total"] else ""
        L.append(f"   {j['miner']:<16}{j['convoqué']:>10}{j['a_voté']:>9}{j['taux']:>8}{flag}")
    if not a["jurors"]:
        # The matrix covers ALL anchored audits: if it is empty, no `audit_requested` was read, which
        # is an absence of MEASUREMENT, not an absence of participation.
        L.append("   (empty — NO `audit_requested` event read: this is an absence of MEASUREMENT, "
                 "not \"no juror votes\". Check RPC access before concluding anything.)")
    else:
        L.append("   (over ALL anchored audits observed, closed ones included — a successful repair "
                 "must not erase the proof that it succeeded)")
    L += ["", "-- PER OPEN AUDIT"]
    for jid, v in list(a["per_job"].items())[:40]:
        s = v["deferral_series"]
        vs = "->".join(str(p["voters"]) for p in s) if s else "none"
        L.append(f"   {jid:<26} {v['verdict']:<19} votes={len(v['voted'])}/{v['anchored_seats']} "
                 f"bar={v['bar']} voter_series={vs}")
        if v["missing"]:
            L.append(f"        missing: {', '.join(v['missing'])}")
    if len(a["per_job"]) > 40:
        L.append(f"   ... +{len(a['per_job'])-40} audits (see the JSON artifact)")
    L += ["", "-" * 78, f"  SOURCE  : {a.get('source', 'unknown')}",
          f"  VERDICT : {g['verdict']}   {g['counts']}",
          f"  ACTION  : {g['action']}", "-" * 78]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verdicts absent vs late (source: the NODE).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--host", help="node host (REST :1317 + RPC :26657)")
    src.add_argument("--from-json", help="replay a capture")
    src.add_argument("--compare", nargs=2, metavar=("BEFORE.json", "AFTER.json"),
                     help="compare two REPORTS — did the audits close again?")
    ap.add_argument("--to-json", help="with --host: write the raw capture and exit")
    ap.add_argument("--out", help="write the JSON report")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.compare:
        b = json.load(open(a.compare[0], encoding="utf-8"))
        c = json.load(open(a.compare[1], encoding="utf-8"))
        d = compare(b, c)
        print(json.dumps(d, ensure_ascii=False, indent=2) if a.json else
              ("COMPARISON IMPOSSIBLE: " + str(d.get("_why")) if not d["_measured"] else
               "\n".join(["=" * 78, "  DID THE AUDITS CLOSE AGAIN?", "=" * 78,
                          f"  closed: {len(d['audits_clos'])}   still open: {len(d['audits_encore_ouverts'])}"
                          f"   new: {len(d['audits_nouveaux'])}", "",
                          "  rate per juror (before -> after):"] +
                         [f"     {x['miner']:<16} {x['taux_avant']} -> {x['taux_apres']}"
                          + ("   <- REVIVED" if x["reveil"] else "") for x in d["jurors_delta"]] +
                         ["", "-" * 78, f"  VERDICT : {d['verdict']}", f"  {d['why']}", "-" * 78])))
        return 0 if d.get("_measured") else 3
    if a.from_json:
        cap = json.load(open(a.from_json, encoding="utf-8"))
    else:
        cap = gather(a.host)
        if a.to_json:
            json.dump(cap, open(a.to_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"capture -> {a.to_json}")
            return 0
    rep = analyse(cap)
    rep["source"] = cap.get("_source", "unknown")
    if a.out:
        json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render(rep))
    if a.out:
        print(f"\n  artifact -> {a.out}")
    # Exit 0 ONLY if the measurement decided; 3 means undecidable (automation must not read
    # "no problem" where nothing could be concluded).
    return 0 if rep.get("_measured") and rep["global"]["verdict"] != UNDECIDED else 3


if __name__ == "__main__":
    sys.exit(main())
