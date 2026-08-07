#!/usr/bin/env python3
"""
c3_gate.py — honest C3 readiness report (decision support for a public announcement).

The script NEVER pronounces `GO`. It INFORMS the announcement decision, it does not take it: the C3 `GO`
is an irreversible governance decision (public reputation) taken by humans. A `GO` label that is not that
final GO would be FALSE TESTIMONY — a name that claims more than the reality it stands for. The highest
verdict is `ATTESTED_PENDING_SIGNOFF`: "everything is green and attested, ONLY the announcement decision
remains". No verdict exits 0 (a 0 reads as "go ahead" to an automation, i.e. the same false testimony at
the exit-code level).

Principle:
  - Criteria MEASURABLE on-chain: the report READS them and decides (PASS/FAIL).
  - Criteria that no counter can measure — ">=100 honest miners", ">=2 judge models including >=1 MoE",
    ">=2 genuinely DISTINCT operators" — are NEVER ticked from a counter: they are REQUIRES_ATTESTATION,
    resolved only by an EXPLICIT, NAMED and REFUTABLE operator attestation (--attest-*). The number of
    on-chain identities is not the number of distinct humans.
  - The preamble applies TO THE REPORT ITSELF: without `pockets_measured >= 3` (the codec omits zeros, so
    "absent" is indistinguishable from a binary predating the measurement) or with a missing query, the
    verdict is NO-GO, fail closed.
  - A failing measurable forces NO-GO even when all attestations are present: an attestation never covers
    a measurement.

Self-honesty: on an N=1 chain (contributors=1, resolved_by_quorum=0, distinct_voters<3) the verdict is NO-GO.

Usage:
  python3 c3_gate.py --node tcp://HOST:26657 [--min-quorum-resolved N] [--max-deferred M]
      [--attest-operators "opA=key,opB=key"] [--attest-models "qwen3:30b-a3b,mistral-nemo"]
      [--attest-honest "<n> honest, run <date>"]
  python3 c3_gate.py --from-json state.json            # evaluate a captured state (offline / test)
  python3 c3_gate.py --node ... --to-json state.json   # capture the on-chain state without deciding

Exit codes: 10 = NO-GO; 20 = REQUIRES_ATTESTATION; 30 = ATTESTED_PENDING_SIGNOFF; 2 = usage.
NEVER 0 for a verdict (see above).
"""
import argparse
import json
import re
import subprocess
import sys

NO_GO = "NO-GO"
NEEDS_ATTEST = "REQUIRES_ATTESTATION"
ATTESTED = "ATTESTED_PENDING_SIGNOFF"
# No verdict exits 0: the C3 `GO` cannot come out of a shell exit code, it is a governance decision.
EXIT = {ATTESTED: 30, NEEDS_ATTEST: 20, NO_GO: 10}

MOE_MARKERS = ("a3b", "30b")  # MoE judge-model markers (qwen3:30b-a3b...)


def _g(d, *keys, default=None):
    """Read a key tolerating both snake_case and camelCase (dendrad -o json = snake; REST = camel)."""
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


# --- NAMED and REFUTABLE attestations ---------------------------------------------------------------
# An unverifiable `--attest` boolean is the human version of "the measurement that cannot contradict".
# Naming what is attested is REQUIRED, so that a third party can refute it after the fact.

def _attest_operators(val):
    if not val:
        return False, "not provided"
    names = sorted(set(x.split("=")[0].strip() for x in val.split(",") if x.split("=")[0].strip()))
    if len(names) >= 2:
        return True, f"attested: {len(names)} NAMED operators ({', '.join(names)})"
    return False, f"provided but insufficient ({len(names)} operator named, need >=2 distinct)"


def _attest_models(val):
    if not val:
        return False, "not provided"
    items = sorted(set(x.strip() for x in val.split(",") if x.strip()))
    moe = any(any(m in it.lower() for m in MOE_MARKERS) for it in items)
    if len(items) >= 2 and moe:
        return True, f"attested: {len(items)} NAMED models ({', '.join(items)}), MoE present"
    if len(items) < 2:
        return False, f"provided but insufficient ({len(items)} model, need >=2)"
    return False, f"provided but NO MoE named among ({', '.join(items)}) — need >=1 marker in {MOE_MARKERS}"


def _attest_honest(val):
    if not val:
        return False, "not provided"
    m = re.search(r"\d+", val)
    if not m:
        return False, f"provided but WITHOUT a verifiable count ('{val}')"
    n = int(m.group())
    if n >= 100:
        return True, f"attested: {n} honest miners declared ('{val}')"
    return False, f"provided but < 100 ({n})"


# ------------------------------------------------------------------ partition

def classify_jobs(jobs, deferred_ids):
    """Partition DISPUTED jobs into MUTUALLY EXCLUSIVE classes, using exact state tokens.

    The state is a '+'-delimited string (job_state.go). It is split on '+' and compared as EXACT TOKENS:
    that is what defeats the `quorum` in `noquorum` trap (a Contains('quorum') would also match
    'noquorum'). `+noquorum` is no longer written at all: a sub-quorum audit is deferred instead.
    """
    deferred_ids = set(deferred_ids or [])
    part = {"resolved_by_quorum": 0, "vindicated": 0, "resolved_by_adjudication": 0,
            "resolved_other": 0, "deferred_no_jury": 0, "pending": 0, "disputed_total": 0}
    resolved_jobs = []
    for j in jobs or []:
        state = _g(j, "state", "State", default="") or ""
        toks = state.split("+")
        if "disputed" not in toks:
            continue
        part["disputed_total"] += 1
        jid = str(_g(j, "id", "jobId", "job_id", default="") or "")
        if "resolved" not in toks:
            if jid and jid in deferred_ids:
                part["deferred_no_jury"] += 1
            else:
                part["pending"] += 1
            continue
        resolved_jobs.append(j)
        if "adjudicated" in toks:
            part["resolved_by_adjudication"] += 1
        elif "quorum" in toks:  # token exact -> ne matche PAS 'noquorum'
            part["resolved_by_quorum"] += 1
            if "vindicated" in toks:
                part["vindicated"] += 1
        else:
            part["resolved_other"] += 1  # human dispute, innocence by default
    return part, resolved_jobs


def operator_share_bps(job):
    """Largest operator share = audit_max_operator_votes / audit_voters, in bps (0..10000)."""
    voters = _int(_g(job, "audit_voters", "auditVoters"))
    mx = _int(_g(job, "audit_max_operator_votes", "auditMaxOperatorVotes"))
    if voters <= 0:
        return None  # no data -> undecidable (counts as not measured)
    return mx * 10000 // voters


# ------------------------------------------------------------------ evaluation

def evaluate(state, cfg):
    """PURE: takes the parsed on-chain state plus the config, returns the structured report. No I/O."""
    crit = []

    def add(key, kind, status, observed, need="", detail=""):
        crit.append({"key": key, "kind": kind, "status": status,
                     "observed": observed, "need": need, "detail": detail})

    # --- PREAMBLE: the report must PROVE it measured, otherwise NO-GO -----------------------------
    missing = [q for q in ("seed_health", "audit_deferred", "held_summary", "params", "jobs")
               if state.get(q) is None]
    held = state.get("held_summary") or {}
    pockets = _g(held, "pockets_measured", "pocketsMeasured")
    preamble_ok = True
    if missing:
        preamble_ok = False
        add("preambule_queries", "preamble", "FAIL", f"missing: {','.join(missing)}",
            "every query answered", "without the data, a zero read would be indistinguishable from an absent field")
    if pockets is None or _int(pockets) < 3:
        preamble_ok = False
        add("pockets_measured", "preamble", "FAIL",
            "absent" if pockets is None else str(pockets), ">= 3",
            "sentinel absent/insufficient -> the binary does not PROVE it measured the zeros")
    else:
        add("pockets_measured", "preamble", "PASS", str(pockets), ">= 3",
            "sentinel present -> a zero pocket is a MEASURED zero, not an omitted field")

    seed = state.get("seed_health") or {}
    deferred = state.get("audit_deferred") or {}
    params = (state.get("params") or {}).get("params") or state.get("params") or {}
    jobs = state.get("jobs") or []
    deferred_ids = _g(deferred, "job_ids", "jobIds", default=[]) or []
    audits_opened = _int(_g(deferred, "audits_opened", "auditsOpened"))

    part, resolved_jobs = classify_jobs(jobs, deferred_ids)

    # --- MEASURABLE on-chain (the report decides) --------------------------------------------------
    contributors = _int(_g(seed, "latest_contributors", "latestContributors"))
    add("contributors", "measurable", "PASS" if contributors >= cfg["min_contributors"] else "FAIL",
        str(contributors), f">= {cfg['min_contributors']}",
        "decentralized VRF seed: below 2, no adversarial audit is possible (the network serves without verifying)")

    rbq = part["resolved_by_quorum"]
    add("resolved_by_quorum", "measurable", "PASS" if rbq >= cfg["min_quorum_resolved"] else "FAIL",
        str(rbq), f">= {cfg['min_quorum_resolved']}", "audits ACTUALLY closed by a quorum of seats")

    add("vindicated", "measurable", "PASS" if part["vindicated"] > 0 else "FAIL",
        str(part["vindicated"]), "> 0", "at least one honest primary cleared by quorum (the path works)")

    dnj = part["deferred_no_jury"]
    add("deferred_no_jury", "measurable", "PASS" if dnj <= cfg["max_deferred"] else "FAIL",
        str(dnj), f"<= {cfg['max_deferred']}", "a backlog of jury-less audits is a network that does not resolve (strict)")

    if resolved_jobs:
        dv_min = min(_int(_g(j, "audit_distinct_voters", "auditDistinctVoters")) for j in resolved_jobs)
        add("distinct_voters_min", "measurable", "PASS" if dv_min >= cfg["distinct_voters_min"] else "FAIL",
            str(dv_min), f">= {cfg['distinct_voters_min']}", "REAL plurality of voters (not a quorum in form only)")
    else:
        add("distinct_voters_min", "measurable", "FAIL", "n/a (0 resolved job)",
            f">= {cfg['distinct_voters_min']}", "no resolved audit -> plurality not measurable")

    shares = [s for s in (operator_share_bps(j) for j in resolved_jobs) if s is not None]
    if shares:
        mshare = max(shares)
        add("max_operator_share_bps", "measurable", "PASS" if mshare < 5000 else "FAIL",
            str(mshare), "< 5000", "no single operator held a MAJORITY of the votes in an audit")
    else:
        add("max_operator_share_bps", "measurable", "FAIL", "n/a (0 resolved job with voters)",
            "< 5000", "no resolved audit with voters -> majority share not measurable")

    # --- jobs_unresolved: the third member of the triplet ------------------------------------------
    # Readiness requires a 0/0/0 triplet, and `jobs_unresolved = 0` is part of it. Watching only
    # `deferred_no_jury` (audits WITHOUT a jury) lets audits WITH a jury that never conclude slip
    # through: the gate would report "every measurable OK" on a chain where 20 audits out of 38 stayed
    # open, with growing held funds and locked jurors. An open audit that never closes is literally
    # "the network has not finished verifying" — the very thing C3 must exclude.
    unres = part["pending"]
    add("jobs_unresolved", "measurable", "PASS" if unres <= cfg.get("max_unresolved", 0) else "FAIL",
        str(unres), f"<= {cfg.get('max_unresolved', 0)}",
        "OPEN audits that never close (jury seated but under the bar) — third member of the triplet; "
        "distinct from deferred_no_jury, which only counts audits WITHOUT a jury")

    # --- ALARM (never blocking) -------------------------------------------------------------------
    # `miner_prune_blocks` is DORMANT by default (0 = the chain falls back on the compiled constant in
    # miner_vitality.go). A `prune > 0 and ...` test can therefore NEVER fire at 0, while DISPLAYING
    # "OK" next to "expected <= 0" — a dead alarm presenting itself as a healthy one (a guard that
    # cannot bite plus a label that lies). Apply the SAME fallback as the chain and NAME the source of
    # the threshold.
    PRUNE_COMPILED = 604800  # miner_vitality.go: minerPruneBlocks (~7 days at 1 block/s)
    prune_param = _int(_g(params, "miner_prune_blocks", "minerPruneBlocks"))
    prune_eff = prune_param if prune_param > 0 else PRUNE_COMPILED
    src_prune = "governed" if prune_param > 0 else f"compiled constant ({PRUNE_COMPILED})"
    oldest_held = _int(_g(held, "oldest_age_blocks", "oldestAgeBlocks"))
    oldest_lock = _int(_g(held, "oldest_lock_age_blocks", "oldestLockAgeBlocks"))
    warn = oldest_held > prune_eff or oldest_lock > prune_eff
    add("held_lock_age", "warning", "WARN" if warn else "OK",
        f"held={oldest_held} lock={oldest_lock} threshold={prune_eff} [{src_prune}]",
        f"<= {prune_eff} (alarm, not a block)",
        "held funds older than the eviction window raise an alarm to clear (it does not block the "
        "gate). The threshold is NEVER 0: at 0 the alarm would be mute by construction.")

    # --- NOT MEASURABLE -> NAMED/REFUTABLE attestation, never a PASS derived from a counter --------
    for key, (ok, shown), detail in (
        ("distinct_operators_ge_2", _attest_operators(cfg.get("attest_operators")),
         "on-chain identities are not distinct human operators: NAME them (--attest-operators \"opA=key,opB=key\")"),
        ("judge_models_ge_2_incl_moe", _attest_models(cfg.get("attest_models")),
         "a declared model_id is not a physically distinct model; >=2 models including >=1 MoE, to be NAMED"),
        ("honest_miners_ge_100", _attest_honest(cfg.get("attest_honest")),
         "registered is not honest; >=100 honest miners in the run, to be DECLARED with a verifiable count"),
    ):
        add(key, "attestation", "ATTESTED" if ok else NEEDS_ATTEST, shown,
            "NAMED operator attestation (refutable)", detail)

    # --- VERDICT (the script NEVER pronounces `GO`) ------------------------------------------------
    measurable = [c for c in crit if c["kind"] == "measurable"]
    attest = [c for c in crit if c["kind"] == "attestation"]
    meas_fail = [c for c in measurable if c["status"] == "FAIL"]

    if not preamble_ok:
        verdict = NO_GO
        why = "PREAMBLE failed: the report cannot PROVE that it measured (see preamble)."
    elif meas_fail:
        verdict = NO_GO
        why = "MEASURABLE criteria failed: " + ", ".join(c["key"] for c in meas_fail)
    elif any(c["status"] == NEEDS_ATTEST for c in attest):
        verdict = NEEDS_ATTEST
        why = "measurables OK; named attestation missing/insufficient: " + \
              ", ".join(c["key"] for c in attest if c["status"] == NEEDS_ATTEST)
    else:
        verdict = ATTESTED
        why = ("every measurable PASSes and the 3 NAMED attestations are provided — ONLY the "
               "ANNOUNCEMENT DECISION remains, and it is a human one. The script does NOT pronounce `GO`.")

    return {
        "verdict": verdict, "why": why,
        "partition": part, "audits_opened": audits_opened,
        "partition_sum_vs_opened": {
            "sum_classes": part["resolved_by_quorum"] + part["resolved_by_adjudication"]
                           + part["resolved_other"] + part["deferred_no_jury"] + part["pending"],
            "audits_opened": audits_opened,
            # The right-hand witness only exists if the query answered. Without it, `audits_opened`
            # defaults to 0 and the comparison would shout "divergence" where there is nothing to
            # compare: an UNMEASURED zero treated as a measured one — the exact fault the
            # `pockets_measured` sentinel closes one level above.
            "comparable": state.get("audit_deferred") is not None},
        "criteria": crit,
        "config": cfg,
        # PROVENANCE is part of the verdict: a NO-GO read through a stale client does not say the same
        # thing as a NO-GO read through the node's own gateway. Never return one for the other.
        "source": state.get("_source", "unknown"),
    }


# ------------------------------------------------------------------ I/O (dendrad)

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


def rpc_all(node, cmd, field, timeout=180, max_pages=200):
    """COMPLETE list, page by page. Returns None if any page fails (fail closed).

    Without paging, `dendrad query jobs list-job` stops at 100 entries: `query.CollectionPaginate`
    receives a PageRequest with Limit=0 and the SDK applies `DefaultLimit = 100`
    (cosmos-sdk/types/query/pagination.go). A C3 run over 150 jobs would show only 100 — silently, and
    ALWAYS in the reassuring direction (fewer jobs read = fewer disputes, fewer deferrals, fewer false
    slashes). A truncation that can only produce good news is the most dangerous form of the
    measurement that cannot contradict.
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
    return None  # page bound reached: do not claim to have read everything


def gather(node):
    """Query the chain. A failing query stays None -> the preamble returns NO-GO (fail closed)."""
    seed = _rpc(node, ["committee-seed-health"])
    deferred = _rpc(node, ["audit-deferred"])
    held = _rpc(node, ["held-summary"])
    params = _rpc(node, ["params"])
    jobs = rpc_all(node, "list-job", "job")
    miners = rpc_all(node, "list-miner", "miner", timeout=60)
    return {"seed_health": seed, "audit_deferred": deferred, "held_summary": held,
            "params": params, "jobs": jobs, "miners": miners, "_source": "cli"}


# --- ALTERNATE SOURCE: the NODE's own REST gateway (:1317) -----------------------------------------
# A workstation's `dendrad` CLI decodes responses with ITS protos and exposes only ITS subcommands. A
# client lagging behind the node therefore produces exactly the picture of a node that lags: subcommands
# reported "absent", recent fields read as 0 — and the chain gets blamed for a workstation defect. REST
# is served BY THE NODE: it is a witness whose version is the chain's, not the workstation's. Same case
# convention (snake_case), same uint64-as-string encoding.

def _rest(base, path, timeout=20):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/dendra/jobs/v1/{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _rest_all(base, path, field, max_pages=400):
    out, key, pages = [], None, 0
    while pages < max_pages:
        q = f"{path}?pagination.limit=500" + (f"&pagination.key={key}" if key else "")
        d = _rest(base, q, timeout=120)
        if d is None:
            return None
        out.extend(_g(d, field, field.capitalize(), default=[]) or [])
        key = _g(d.get("pagination") or {}, "next_key", "nextKey")
        pages += 1
        if not key:
            return out
    return None


def gather_rest(base):
    """Same state, read through the NODE's REST gateway — independent of the local CLI version."""
    return {"seed_health": _rest(base, "committee_seed_health"),
            "audit_deferred": _rest(base, "audit_deferred"),
            "held_summary": _rest(base, "held_summary"),
            "params": _rest(base, "params"),
            "jobs": _rest_all(base, "job", "job"),
            "miners": _rest_all(base, "miner", "miner"),
            "_source": f"rest {base}"}


# ------------------------------------------------------------------ rendering

def render(report):
    C = report["criteria"]
    lines = []
    lines.append("=" * 78)
    lines.append("  C3 REPORT — ANNOUNCEMENT DECISION SUPPORT (on-chain read; never pronounces GO)")
    lines.append("=" * 78)
    for kind, title in (("preamble", "PREAMBLE (proof of measurement)"),
                        ("measurable", "MEASURABLE on-chain (the report decides)"),
                        ("warning", "ALARM (never blocking)"),
                        ("attestation", "NOT MEASURABLE — NAMED operator attestation required")):
        rows = [c for c in C if c["kind"] == kind]
        if not rows:
            continue
        lines.append("")
        lines.append(f"-- {title}")
        for c in rows:
            mark = {"PASS": "[x] PASS", "FAIL": "[!] FAIL", "OK": "[.] OK", "WARN": "[!] WARN",
                    "ATTESTED": "[x] ATTESTED", NEEDS_ATTEST: "[?] TO ATTEST"}[c["status"]]
            lines.append(f"   {mark:<14} {c['key']}")
            lines.append(f"       observed={c['observed']}  expected={c['need']}")
            if c["detail"]:
                lines.append(f"       -> {c['detail']}")
    p = report["partition"]
    lines.append("")
    lines.append("-- PARTITION of disputed jobs (exact state tokens; checked against audits_opened)")
    lines.append(f"   quorum={p['resolved_by_quorum']} (vindicated={p['vindicated']}) "
                 f"adjudication={p['resolved_by_adjudication']} other={p['resolved_other']} "
                 f"deferred={p['deferred_no_jury']} pending={p['pending']} | disputed={p['disputed_total']}")
    s = report["partition_sum_vs_opened"]
    if not s.get("comparable", True):
        lines.append(f"   sum_classes={s['sum_classes']}  audits_opened=NOT MEASURED "
                     "(query `audit-deferred` did not answer — nothing to compare, not a divergence)")
    else:
        flag = "" if s["sum_classes"] == s["audits_opened"] else "  ! divergence (investigate)"
        lines.append(f"   sum_classes={s['sum_classes']}  audits_opened={s['audits_opened']}{flag}")
    lines.append("")
    lines.append("-- WHAT THIS GATE DOES NOT MEASURE (and which is part of the triplet)")
    lines.append("   `hard_slash_at_quorum_rate` and the false-slash rate on honest miners require")
    lines.append("   knowing WHO is declared a cheater: a legitimate slash of a cheater is not a false")
    lines.append("   slash. This gate reads the whole chain history without roles, so it cannot decide")
    lines.append("   them. They are produced by `c3_run_measure.py --cheaters \"...\"` over a dated run.")
    lines.append("")
    lines.append("-" * 78)
    lines.append(f"  SOURCE  : {report.get('source', 'unknown')}")
    lines.append(f"  VERDICT : {report['verdict']}")
    lines.append(f"  REASON  : {report['why']}")
    lines.append("  (the script NEVER pronounces `GO` — ceiling = ATTESTED_PENDING_SIGNOFF; the C3 GO is")
    lines.append("   a human governance decision, never an output of this script.)")
    lines.append("-" * 78)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Honest C3 report — never pronounces GO.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--node", help="tcp://HOST:26657 — query the chain through the LOCAL dendrad CLI")
    src.add_argument("--rest", metavar="http://HOST:1317",
                     help="the NODE's REST gateway — use it when the local CLI lags behind the chain "
                          "(its subcommands and protos are the workstation's, not the chain's)")
    src.add_argument("--from-json", help="evaluate a captured state (offline / test)")
    ap.add_argument("--to-json", help="with --node: write the gathered state and exit (no verdict)")
    ap.add_argument("--min-quorum-resolved", type=int, default=1,
                    help="floor for resolved_by_quorum (default 1; calibrate on real data)")
    ap.add_argument("--max-deferred", type=int, default=0, help="tolerated deferred_no_jury (STRICT, default 0)")
    ap.add_argument("--max-unresolved", type=int, default=0,
                    help="tolerated jobs_unresolved (third member of the triplet; STRICT, default 0)")
    ap.add_argument("--min-contributors", type=int, default=2, help="floor for contributors (default 2)")
    ap.add_argument("--distinct-voters-min", type=int, default=3, help="floor for distinct_voters (default 3)")
    ap.add_argument("--attest-operators", metavar='"opA=key,opB=key"', default="",
                    help="ATTEST, BY NAMING THEM, >=2 genuinely distinct operators (refutable afterwards)")
    ap.add_argument("--attest-models", metavar='"m1,m2(MoE)"', default="",
                    help="ATTEST >=2 NAMED judge models including >=1 MoE (markers a3b/30b)")
    ap.add_argument("--attest-honest", metavar='"<n> honest, run <date>"', default="",
                    help="DECLARE the number (>=100) of honest miners in the run")
    ap.add_argument("--json", action="store_true", help="raw JSON output instead of the text rendering")
    a = ap.parse_args(argv)

    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = gather_rest(a.rest) if a.rest else gather(a.node)
        if a.to_json:
            with open(a.to_json, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            print(f"state written -> {a.to_json}")
            return 0  # a capture, not a gate verdict

    cfg = {"min_quorum_resolved": a.min_quorum_resolved, "max_deferred": a.max_deferred,
           "max_unresolved": a.max_unresolved,
           "min_contributors": a.min_contributors, "distinct_voters_min": a.distinct_voters_min,
           "attest_operators": a.attest_operators, "attest_models": a.attest_models,
           "attest_honest": a.attest_honest}
    report = evaluate(state, cfg)
    print(json.dumps(report, ensure_ascii=False, indent=2) if a.json else render(report))
    return EXIT[report["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
