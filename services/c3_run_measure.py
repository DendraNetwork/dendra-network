#!/usr/bin/env python3
"""
c3_run_measure.py — MEASUREMENT of the C3 load run (the TRIPLET), separated from traffic submission.

Why this module exists as a module. When the measurement lives inside a heredoc of the validation
script, it goes wrong in three ways that share one property: it cannot contradict.

  (1) Reading a job identifier with `j.get("id", j.get("jobId", ""))`. The chain returns `job_id`
      (proto3 -> snake_case, cf. job.pb.go: `json:"job_id,omitempty"`), so the id is ALWAYS "", every
      `""__verdict__<miner>` lookup misses, the observed judges and models stay empty, and
      `hard_slash_at_quorum_rate` comes out at 0.0 BY CONSTRUCTION. The first member of the triplet —
      the one that authorizes the announcement — is then a MANUFACTURED zero.
  (2) Deciding the quorum locally (`inv >= 4`). The chain decides
      `need = max(audit_min_quorum, ceil(2/3 x anchored seats))` (auditSlashDecision, antievasion.go).
      At 7 seats the chain requires 5: a client holding a MODEL OF THE PROTOCOL is a model that
      diverges.
  (3) Assuming "every miner in the run is honest". False as soon as a cheater is permanently
      registered: a LEGITIMATE slash of the cheater reads as a "false slash of an honest miner", and
      the measurement counts a SUCCESS as a FAILURE.

Principles:
  - NEVER recompute a decision the chain has already taken. A hard slash at the quorum is written by
    the chain: state tokens `resolved+clawed+quorum` (audit_sampling.go) plus `slash_records`. Read
    both sources and CROSS-CHECK them (two independent witnesses, never one).
  - EXACT state tokens via `split("+")` (never `in`, which matches `quorum` inside `noquorum`).
  - Honest miners and cheaters are measured SEPARATELY. The triplet speaks only of honest miners; the
    cheater's slashes are the POSITIVE evidence the release gate expects (at least one cheater slash).
  - Anything that could not be measured is marked `measured: false` with its reason. An unmeasured
    member of the triplet is not a zero.

Usage:
  python3 c3_run_measure.py --node tcp://HOST:26657 --cheaters <miner-id> --out artifact.json
  python3 c3_run_measure.py --from-json run_state.json --cheaters <miner-id>   # offline replay
"""
import argparse
import json
import subprocess
import sys

# Tokens written by the chain (audit_sampling.go). Do not derive them: copy them.
T_DISPUTED, T_RESOLVED, T_QUORUM = "disputed", "resolved", "quorum"
T_CLAWED, T_VINDICATED, T_ADJUDICATED = "clawed", "vindicated", "adjudicated"


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


def job_id_of(job):
    """The identifier of a job as the CHAIN names it.

    `job_id` first: that is what `dendrad -o json` returns (proto3 OrigName). `jobId` is the
    REST/camelCase form. `id` last is the NORMALIZED form of `client.list_jobs_full()`. The
    order matters: a normalized dict carries all of them, the chain carries only one. Omitting
    `job_id` is defect (1) above.
    """
    return str(_g(job, "job_id", "jobId", "id", default="") or "")


def tokens(job):
    return (_g(job, "state", "State", default="") or "").split("+")


def measure(state, cheaters=(), honest_declared=None):
    """PURE. `state` is an on-chain capture; returns the structured artifact. No I/O.

    state = {jobs, miners_before, miners_after, verdicts:{job_id:{miner:{vote,model}}}, params,
             audit_deferred, seats:{job_id:[anchored members]}}
    """
    cheaters = set(cheaters or ())
    jobs = state.get("jobs")
    params = (state.get("params") or {}).get("params") or state.get("params") or {}
    verdicts = state.get("verdicts") or {}
    seats_by_job = state.get("seats") or {}
    before = state.get("miners_before") or {}
    after = state.get("miners_after") or {}
    defr = state.get("audit_deferred")

    notes = []
    if jobs is None:
        return {"_measured": False,
                "_why": "list-job missing: NOTHING is measured (neither the triplet nor the cheater evidence)."}

    owner = {}          # job_id -> primary miner
    disputed, resolved = [], []
    for j in jobs:
        jid, tk = job_id_of(j), tokens(j)
        owner[jid] = str(_g(j, "miner_id", "minerId", default="") or "")
        if T_DISPUTED in tk:
            disputed.append(j)
            if T_RESOLVED in tk:
                resolved.append(j)

    if disputed and not any(job_id_of(j) for j in disputed):
        notes.append("NO disputed job carries a readable identifier - the capture is suspect "
                     "(this is exactly defect (1): do NOT read it as \"nothing to report\").")

    def split_by_role(items, key):
        h = [x for x in items if key(x) not in cheaters]
        c = [x for x in items if key(x) in cheaters]
        return h, c

    # --- HARD SLASHES AT THE QUORUM, as the CHAIN wrote them (witness 1: state tokens) -------------
    hard = [j for j in resolved if T_CLAWED in tokens(j) and T_QUORUM in tokens(j)]
    hard_h, hard_c = split_by_role(hard, lambda j: owner.get(job_id_of(j), ""))
    res_h, res_c = split_by_role(resolved, lambda j: owner.get(job_id_of(j), ""))

    # --- SLASHES ACTUALLY APPLIED (witness 2: slash_records, INDEPENDENT of the tokens) ------------
    slashed_udndr, slashed_jobs = {}, {}
    for j in jobs:
        for r in (_g(j, "slash_records", "slashRecords", default=[]) or []):
            mid = str(_g(r, "miner_id", "minerId", default="") or "?")
            slashed_udndr[mid] = slashed_udndr.get(mid, 0) + _int(_g(r, "amount"))
            slashed_jobs.setdefault(mid, []).append(job_id_of(j))
    honest_slashed = {m: v for m, v in slashed_udndr.items() if m not in cheaters}

    # --- CROSS-CHECK OF THE TWO WITNESSES. They measure the same thing by two paths; when they
    # disagree, no side is picked silently — the disagreement is REPORTED, because a disagreement
    # between witnesses is information, not a display detail.
    hard_owners = sorted({owner.get(job_id_of(j), "") for j in hard})
    rec_owners = sorted(slashed_udndr)
    cross_ok = set(hard_owners) <= set(rec_owners) or not hard_owners
    if not cross_ok:
        notes.append(f"WITNESSES DIVERGE: slashed by state tokens={hard_owners} but "
                     f"slash_records={rec_owners}. Investigate before making any claim.")

    # --- stake deltas: witness 3, WEAK (a miner can re-bond) -> never decisive ---------------------
    deltas = {m: after.get(m, 0) - before.get(m, 0) for m in sorted(set(before) | set(after))} \
        if (before and after) else {}
    bled_delta = {m: d for m, d in deltas.items() if d < 0 and m not in cheaters}

    # --- VETO MARGINS — OBSERVATION, never a decision ----------------------------------------------
    # The raw INPUTS are exposed (anchored seats, "invalid" votes, governed quorum) and the DERIVED
    # threshold is shown as such. The decision remains the one the chain wrote: that is what keeps this
    # file from becoming a model of the protocol that diverges (defect (2)).
    amq = _int(_g(params, "audit_min_quorum", "auditMinQuorum"))
    margins = {}
    for j in disputed:
        jid = job_id_of(j)
        v = verdicts.get(jid) or {}
        inv = sum(1 for x in v.values() if str(_g(x, "vote", default="")) == "0")
        seats = len(seats_by_job.get(jid) or []) or _int(_g(j, "audit_voters", "auditVoters"))
        derived = max(amq, -(-2 * seats // 3)) if seats else None
        margins[jid] = {
            "primary": owner.get(jid, ""), "state": (_g(j, "state", default="") or ""),
            "votes_read": len(v), "invalid_votes": inv,
            "anchored_seats": seats or None,
            "audit_min_quorum_param": amq,
            "need_seats_DERIVED_informational": derived,
            "chain_wrote_hard_slash": T_CLAWED in tokens(j) and T_QUORUM in tokens(j),
        }
    models = sorted({str(_g(x, "model", default="") or "")
                     for v in verdicts.values() for x in v.values() if _g(x, "model")})
    judges = sorted({m for v in verdicts.values() for m in v})

    n_res_h = len(res_h)
    rate_measured = n_res_h > 0
    rate = (len(hard_h) / n_res_h) if rate_measured else None

    unresolved = len(disputed) - len(resolved)

    return {
        "_measured": True,
        "_notes": notes,
        "roles": {"cheaters_declared": sorted(cheaters),
                  "honest_declared": honest_declared,
                  "why": "slashing a cheater is a SUCCESS, not a false slash: the two populations "
                         "cannot share a counter."},
        "counts": {"jobs_total": len(jobs), "disputed": len(disputed), "resolved": len(resolved),
                   "resolved_honest": n_res_h, "resolved_cheater": len(res_c),
                   "audits_opened_onchain": None if defr is None
                   else _int(_g(defr, "audits_opened", "auditsOpened")),
                   "deferred_no_jury": None if defr is None
                   else len(_g(defr, "job_ids", "jobIds", default=[]) or [])},
        # THE TRIPLET — honest miners ONLY (never a rate on its own).
        "triplet": {
            "hard_slash_at_quorum_rate": {
                "value": rate, "measured": rate_measured,
                "why": "no resolved audit of an honest miner -> nothing to divide (a 0 here would be fabricated)"
                       if not rate_measured else
                       f"{len(hard_h)} hard slash(es) at the quorum out of {n_res_h} resolved honest audits",
                "source": "state tokens written by the chain (resolved+clawed+quorum)"},
            "honnetes_faux_slash": {
                "value": len(honest_slashed), "measured": True,
                "detail_udndr": honest_slashed, "jobs": {m: slashed_jobs[m] for m in honest_slashed},
                "source": "on-chain slash_records (exact, attributable); stake deltas as a control"},
            "jobs_unresolved": {
                "value": unresolved, "measured": True,
                "source": "jobs carrying +disputed without a +resolved token (exact tokens)"},
        },
        # POSITIVE EVIDENCE expected by the release gate (at least one cheater slash).
        "cheater_evidence": {
            "resolved_audits": len(res_c),
            "hard_slash_at_quorum": len(hard_c),
            "slashed_udndr": {m: v for m, v in slashed_udndr.items() if m in cheaters},
            "jobs": {m: slashed_jobs[m] for m in slashed_jobs if m in cheaters}},
        "cross_check": {"owners_by_state_tokens": hard_owners,
                        "miners_in_slash_records": rec_owners, "consistent": cross_ok},
        "stake_deltas_weak_witness": {"deltas": deltas, "honest_negative": bled_delta,
                                      "caveat": "a miner can re-bond: this witness settles NOTHING"},
        "judge_diversity": {"models_observed": models, "judges_observed": judges,
                            "note": "empty means the verdicts were not read (check the job "
                                    "identifiers), NOT that there was no judge"},
        "veto_margins_observational": margins,
    }


# ------------------------------------------------------------------ I/O
def _rpc(node, args, timeout=60):
    try:
        r = subprocess.run(["dendrad", "query", "jobs", *args, "--node", node, "-o", "json"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def rpc_all(node, cmd, field, timeout=180, max_pages=400):
    """COMPLETE listing, page by page (None if a page fails: fail closed).

    `list-job` / `list-commit` without pagination stop at 100 entries (SDK `DefaultLimit`). A run of
    150 jobs would show only 100 of them — always in the reassuring direction.
    """
    out, key, pages = [], None, 0
    while pages < max_pages:
        args = [cmd, "--page-limit", "500"] + (["--page-key", key] if key else [])
        d = _rpc(node, args, timeout=timeout)
        if d is None:
            return None
        out.extend(_g(d, field, field.capitalize(), default=[]) or [])
        key = _g(d.get("pagination") or {}, "next_key", "nextKey")
        pages += 1
        if not key:
            return out
    return None


def gather_run(node, miners_before=None):
    jobs = rpc_all(node, "list-job", "job")
    ml = rpc_all(node, "list-miner", "miner", timeout=60)
    miners = {} if ml is None else {str(_g(m, "miner_id", "minerId", default="")): _int(_g(m, "stake"))
                                    for m in ml}
    # A SINGLE query for ALL verdicts. One `get-commit` per job per seat is O(jobs x 15) subprocesses
    # AND sees only the seats: a vote posted by a NON-convened miner stays invisible, which is exactly
    # what must remain observable.
    commits = rpc_all(node, "list-commit", "commit") or []
    by_job = {}
    for c in commits:
        key = str(_g(c, "job_id", "jobId", default="") or "")
        if "__verdict__" not in key:
            continue
        jid, _, mid = key.partition("__verdict__")
        vote = str(_g(c, "result_commit", "resultCommit", default="") or "").strip()
        if vote in ("0", "1"):
            by_job.setdefault(jid, {})[mid] = {
                "vote": vote, "model": str(_g(c, "model_id", "modelId", default="") or "").strip()}
    verdicts, seats = {}, {}
    for j in (jobs or []):
        if T_DISPUTED not in tokens(j):
            continue
        jid = job_id_of(j)
        if not jid:
            continue
        ac = _rpc(node, ["assigned-committee", jid], timeout=30)
        seats[jid] = list(_g(ac, "members", default=[]) or []) if ac else []
        verdicts[jid] = by_job.get(jid, {})
    return {"jobs": jobs, "miners_before": miners_before or {}, "miners_after": miners,
            "verdicts": verdicts, "seats": seats,
            "params": _rpc(node, ["params"]), "audit_deferred": _rpc(node, ["audit-deferred"])}


def render(a):
    if not a.get("_measured"):
        return f"MEASUREMENT IMPOSSIBLE: {a.get('_why')}"
    t, c, n = a["triplet"], a["counts"], a["cheater_evidence"]
    L = ["=" * 78, "  C3 RUN MEASUREMENT - triplet (honest miners) + cheater evidence", "=" * 78,
         f"  jobs={c['jobs_total']} disputed={c['disputed']} resolved={c['resolved']} "
         f"(honest={c['resolved_honest']} cheater={c['resolved_cheater']}) "
         f"audits_opened={c['audits_opened_onchain']} deferred={c['deferred_no_jury']}", "",
         "-- TRIPLET (honest miners only)"]
    r = t["hard_slash_at_quorum_rate"]
    L.append(f"   hard_slash_at_quorum_rate = {'NOT MEASURED' if not r['measured'] else r['value']}"
             f"   -> {r['why']}")
    L.append(f"   honnetes_faux_slash       = {t['honnetes_faux_slash']['value']}"
             f"   {t['honnetes_faux_slash']['detail_udndr'] or ''}")
    L.append(f"   jobs_unresolved           = {t['jobs_unresolved']['value']}")
    L += ["", "-- CHEATER EVIDENCE (the release gate expects this to be POSITIVE)",
          f"   resolved audits={n['resolved_audits']}  hard slash at quorum={n['hard_slash_at_quorum']}"
          f"  total slashed={n['slashed_udndr']}", "",
          "-- JUDGE DIVERSITY (empty = not read, NOT \"none\")",
          f"   models={a['judge_diversity']['models_observed']}  "
          f"judges={a['judge_diversity']['judges_observed']}"]
    if not a["cross_check"]["consistent"] or a["_notes"]:
        L += ["", "-- TO INVESTIGATE"] + [f"   {x}" for x in a["_notes"]]
        if not a["cross_check"]["consistent"]:
            L.append(f"   witnesses diverge: {a['cross_check']}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="C3 run measurement (honest triplet + cheater evidence).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--node")
    src.add_argument("--from-json")
    ap.add_argument("--cheaters", default="", help="comma-separated miner ids DECLARED as cheaters")
    ap.add_argument("--honest-declared", default="", help="what the operator attests about the honest miners")
    ap.add_argument("--before-json", help="snapshot of the stakes BEFORE the run (weak witness)")
    ap.add_argument("--out", help="write the JSON artifact")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.from_json:
        state = json.load(open(a.from_json, encoding="utf-8"))
    else:
        before = json.load(open(a.before_json, encoding="utf-8")) if a.before_json else {}
        state = gather_run(a.node, before)
    art = measure(state, [x.strip() for x in a.cheaters.split(",") if x.strip()],
                  a.honest_declared or None)
    if a.out:
        json.dump(art, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(art, ensure_ascii=False, indent=2) if a.json else render(art))
    if a.out:
        print(f"\n  artifact -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
