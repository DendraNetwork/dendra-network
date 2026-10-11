#!/usr/bin/env python3
"""Prometheus exporter for Dendra: exposes the network state (miners, stake, balances, pools,
block height, jobs) as Prometheus metrics on /metrics, for visualization in Grafana.

Queries the chain via client (thus via `dendrad`). Refresh loop every 5 s.
Usage: python3 exporter.py    (port 9101 by default; DENDRA_EXPORTER_PORT to change)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure core of the hardened metric "external final fees / released emission".
# Hardens R (dendra_r_settlement, farmable) on 3 on-chain measurable axes:
#   (1) Strict-finality: only jobs settled in a final positive state count -- a clawed job
#       (`+clawed`, payment reclaimed) never counts; a job in unresolved audit (`+disputed` without
#       `+resolved`) is pending (excluded while undecided); a vindicated job counts again.
#       (Markers = on-chain predicates in job_state.go: paid/settled, disputed, resolved, clawed.)
#   (2) External client: excludes jobs whose client is the operator of any registered miner,
#       whoever it is (the on-chain Demand guard only excludes its own operator).
#   (3) Non-subsidy: excludes jobs paid by subsidy accounts (free-tier settled by the
#       Reserve = emission disguised as "demand" -- the main hole in R, which counted them).
# Honesty: R2 is not a proof of traction. A "2-account" Sybil (a fresh client funded off-chain by an
# operator) stays undetectable on-chain (oracle problem). But this wash costs non-recoverable burn+cut
# and stays -EV for the subsidy (pinned by chain/x/jobs/types/params_invariant_test.go). The real
# traction metric = monetized non-miner revenue. R2 = the best on-chain-measurable alert signal.
# Pure functions (no network/chain/prometheus dependency) -> `--selftest` offline.
# ---------------------------------------------------------------------------
R2_BUCKETS = ("counted", "pending", "clawed", "subsidized", "operator_client", "unpaid")


def r2_job_bucket(state, client, operators, excluded):
    """Classifies one job for R2 (pure). Precedence: unpaid > clawed > pending > subsidized > operator_client
    > counted. `clawed` takes precedence over any settled job (even resolved: the payment was reclaimed); a
    `+disputed+resolved` job not clawed = vindicated -> counts. `operators`/`excluded` = sets of addresses."""
    st = state or ""
    if ("paid" not in st) and ("settled" not in st):
        return "unpaid"            # never settled: the fee did not leave escrow toward a miner
    if "clawed" in st:
        return "clawed"            # strict-finality: payment reclaimed -> never counts
    if "disputed" in st and "resolved" not in st:
        return "pending"           # audit in progress: not final (counts again if vindicated)
    if client in excluded:
        return "subsidized"        # paid by the Reserve/free-tier: emission, not demand
    if client in operators:
        return "operator_client"   # direct-operator self-dealing (any registered operator)
    return "counted"


def compute_r2(jobs, operators, excluded, emitted_u):
    """(fees_udndr, r2, buckets): Σ fees of `counted` jobs / emission out of the module (udndr). Pure.

    `jobs=None` (the listing could not be read) raises: computed over no job, R2 is 0, and 0 under
    the threshold raises `dendra_r2_stop_breach` -- an outage of the node read as a collapse of demand."""
    if jobs is None:
        raise ValueError("jobs not measured (list-job unreadable): R2 is not computed over an unread listing")
    buckets = {b: 0 for b in R2_BUCKETS}
    fees_u = 0
    for j in jobs:
        b = r2_job_bucket(j.get("state", ""), j.get("client", ""), operators, excluded)
        buckets[b] += 1
        if b == "counted":
            fees_u += int(j.get("fee", 0) or 0)
    r2 = (fees_u / emitted_u) if emitted_u > 0 else 0.0
    return fees_u, r2, buckets


def _selftest():
    """Verifies the pure R2 logic offline (no chain, no prometheus_client)."""
    ops, exc = {"opA"}, {"bobaddr"}
    cases = [  # (state, client, expected bucket)
        ("open", "ext1", "unpaid"),                                        # never settled
        ("open+paid+finalized", "ext1", "counted"),                        # k=3 final
        ("open+paid+optimistic", "ext1", "counted"),                       # optimistic, unaudited = final
        ("open+paid+optimistic+disputed", "ext1", "pending"),              # audit in progress
        ("open+paid+optimistic+disputed+resolved", "ext1", "counted"),     # vindicated
        ("open+paid+optimistic+disputed+resolved+clawed", "ext1", "clawed"),  # reclaimed -> never counted
        ("open+paid+optimistic+disputed+resolved+clawed", "bobaddr", "clawed"),  # clawed takes precedence over subsidy
        ("open+paid+finalized", "bobaddr", "subsidized"),                  # free-tier excluded
        ("open+paid+finalized", "opA", "operator_client"),                 # direct self-dealing excluded
        ("settled", "ext2", "counted"),                                    # settle_pay/settle_job path
    ]
    ok = True
    for st, cl, want in cases:
        got = r2_job_bucket(st, cl, ops, exc)
        good = got == want
        ok = ok and good
        print(f"  [{'OK' if good else 'FAIL'}] {st!r:62} client={cl:8} -> {got} (expected {want})")
    jobs = [{"state": st, "client": cl, "fee": 100} for st, cl, _ in cases]
    fees, r2, buckets = compute_r2(jobs, ops, exc, emitted_u=1000)
    good = fees == 400 and abs(r2 - 0.4) < 1e-9 and buckets["counted"] == 4 and buckets["clawed"] == 2
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] compute_r2: fees={fees} (expected 400) r2={r2} (expected 0.4) buckets={buckets}")
    fees0, r20, _ = compute_r2(jobs, ops, exc, emitted_u=0)
    good = r20 == 0.0 and fees0 == 400
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] zero emission -> r2=0.0 (no division), fees untouched")
    try:
        compute_r2(None, ops, exc, emitted_u=1000)
        good = False
    except ValueError:
        good = True
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] jobs=None (unread listing) raises -- never an R2 of 0 that trips the stop")
    fe, re_, be = compute_r2([], ops, exc, emitted_u=1000)
    good = fe == 0 and re_ == 0.0 and sum(be.values()) == 0
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] an empty listing that was read is a measurement: R2 = 0 over zero jobs")
    print("selftest R2", "GREEN" if ok else "RED")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # Before the heavy imports (client / prometheus may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client as dc

try:
    from prometheus_client import start_http_server, Gauge
except Exception:
    print("ERROR: 'prometheus_client' is required  ->  pip install --user --break-system-packages prometheus_client")
    sys.exit(1)

PORT = int(os.environ.get("DENDRA_EXPORTER_PORT", "9101"))
MAX_LABELED = int(os.environ.get("DENDRA_EXPORTER_MAX_MINERS", "200"))  # bounds the cardinality

g_height = Gauge("dendra_block_height", "Block height of the chain")
g_miners = Gauge("dendra_miners_total", "Number of registered miners")
g_stake = Gauge("dendra_miner_stake", "Stake (bond) of a miner", ["miner"])
g_bal = Gauge("dendra_miner_balance_token", "Token balance of a miner (earnings)", ["miner"])
g_treasury = Gauge("dendra_pool_treasury", "Treasury (accumulated slashes)")
g_total = Gauge("dendra_miners_total_balance_token", "Sum of the miner balances read (distributed tokens); "
                "dendra_miners_balance_unread says how many it leaves out")
g_bal_unread = Gauge("dendra_miners_balance_unread", "Registered miners whose operator balance could not be read "
                     "at the last registry reading: not counted in dendra_miners_total_balance_token, never as 0")
g_up = Gauge("dendra_chain_up", "1 if the chain answers")
# Settlement-to-emission ratio. Farmable proxy, not a proof of external traction:
# Demand is credited only with the guard `job.Client != miner.Operator` (settle_semantic.go) -> an operator can
# wash via a Sybil client (2nd address != operator) and inflate R with no external buyer. Do not use it
# as an automatic stop rule while the anti-Sybil / strict-finality (v2) is not hardened; R<1 = alert signal.
g_demand = Gauge("dendra_demand_total_dndr", "Settlement volume credited as Demand (sum of miner.Demand). Proxy: does not distinguish an external buyer from an operator-controlled Sybil client. DNDR")
g_released = Gauge("dendra_emission_released_dndr", "Emission released from the Reserve (reserve_init - emission module balance), DNDR")
g_r = Gauge("dendra_r_settlement", "R = settlement volume (Demand) / released emission. Farmable proxy (wash through a Sybil client != operator) -> alert signal when sustained below 1, not a proof of external traction nor an automatic stop")
g_grind = Gauge("dendra_committee_grinding_inactive", "VRF bootstrap: 1 IFF committee_seed_source=1 and (no recent decentralized VRF seed or contributors < floor) -> anti-grinding inactive (legacy fallback, committee drawn from a non-decentralized seed). RED = precondition for opening the rewarded testnet (a validator must anchor its VRF key). 0 = OK, or legacy source (committee_seed_source=0)")
# -- Relay: aggregate observability ----------------------------------------------------------------
# The relay does not log its requests, and that is a guard: the path carries the identifier. But "no
# paths" does not mean "blind". The real exposure is saturation:
# a third party that fills the store makes legitimate deposits be refused (507), and the network goes
# dark for a day to a week without anything saying so. These metrics are the missing signal — only
# numbers, no `jobId`, no `minerId` (the relay itself guarantees this and its test bench requires it).
RELAY_BASE = os.environ.get("DENDRA_RELAY_BASE", "http://127.0.0.1:8645")
RELAY_TOKEN = os.environ.get("DENDRA_RELAY_TOKEN", "")
g_relay_up = Gauge("dendra_relay_up", "1 if the relay /stats endpoint answers (0 = relay silent or token refused: "
                                      "in both cases relay observability is absent, which is not 'all is well')")
g_relay_occ = Gauge("dendra_relay_store_entries", "Store entries per type (protocol category, never a key)", ["kind"])
g_relay_cap = Gauge("dendra_relay_store_cap", "Cap per type: beyond it a deposit is refused (507), never overwritten")
g_relay_sat = Gauge("dendra_relay_store_saturation", "Saturation signal: maximum occupancy / cap, across all types. "
                                                     "Close to 1 means legitimate deposits are about to be refused")
g_relay_http = Gauge("dendra_relay_http_responses", "Relay responses since startup, per code", ["code"])
g_relay_evic = Gauge("dendra_relay_evictions_age", "Entries evicted by age (retention) since startup")
g_relay_prev = Gauge("dendra_relay_prev_token_uses", "Uses of the previous token (rotation): while this rises, a client is not migrated")
g_relay_ips = Gauge("dendra_relay_rate_ips", "IPs tracked by the rate limiter (memory bound)")


def refresh_relay():
    """Scrapes the relay /stats endpoint. Never raises: an unreachable relay sets `up=0` and leaves
    the other metrics untouched — an exporter that dies on the relay would blind the chain too."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(RELAY_BASE.rstrip("/") + "/stats")
    if RELAY_TOKEN:
        req.add_header("X-Dendra-Token", RELAY_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            st = json.loads(r.read().decode())
    except Exception as e:
        g_relay_up.set(0)
        print(f"[exporter] relay /stats unavailable ({type(e).__name__}) -> relay observability absent")
        return
    g_relay_up.set(1)
    occ = st.get("occupation", {}) or {}
    cap = int(st.get("legacy_entry_cap", 0) or 0)
    g_relay_occ.clear()
    for kind, n in occ.items():
        g_relay_occ.labels(kind=str(kind)).set(n)
    g_relay_cap.set(cap)
    # Saturation is measured on the fullest type: an average would dilute exactly the type an
    # attacker targets. The maximum is what decides the first refusal.
    g_relay_sat.set((max(occ.values()) / cap) if (occ and cap > 0) else 0)
    g_relay_http.clear()
    for name, n in (st.get("counters", {}) or {}).items():
        if name.startswith("http_"):
            g_relay_http.labels(code=name[5:]).set(n)
    g_relay_evic.set((st.get("counters", {}) or {}).get("evictions_age", 0))
    g_relay_prev.set(st.get("prev_token_uses", 0))
    g_relay_ips.set(st.get("tracked_ips", 0))


EMISSION_ADDR = os.environ.get("DENDRA_EMISSION_ADDR", "dendra1q6tym5h6cd3cxcqua9eflkf5rct8c6rsn92wrf")  # emission module account (genesis)
RESERVE_INIT = int(os.environ.get("DENDRA_RESERVE_INIT_UDNDR", "3300000000000"))  # 3.3 M DNDR placed in the module at genesis

# Hardened metrics (cf. pure core at the top of the file). Honest labels:
g_r2_fees = Gauge("dendra_r2_external_final_fees_dndr",
                  "R2 numerator: sum of the fees of jobs with positive finality (paid/settled, never clawed, "
                  "not disputed-pending) paid by an external client (neither the operator of a registered "
                  "miner nor a subsidy/free-tier account). DNDR")
g_r2 = Gauge("dendra_r2_external",
             "R2 = external final fees / emission that left the emission module. Hardened versus "
             "dendra_r_settlement (strict finality + exclusion of direct self-dealing + exclusion of the "
             "subsidy) but not a proof of traction: a two-account Sybil funded off-chain stays undetectable "
             "(oracle problem) - it pays burn+cut and stays -EV (params_invariant_test.go). Real traction is "
             "monetized revenue")
g_r2_stop = Gauge("dendra_r2_stop_breach",
                  "1 when R2 < the pre-committed stop threshold (DENDRA_R2_STOP_THRESHOLD, default 1.0) while "
                  "the emission paid out exceeds the significance floor (DENDRA_R2_MIN_EMISSION_DNDR, default "
                  "100 DNDR). Anti-bubble rule: a sustained breach (14 days on the incentivized testnet) means "
                  "rewards are frozen by governance (the R2 metric note of the development repository) - an alert "
                  "signal, not an auto-stop")
g_r2_jobs = Gauge("dendra_r2_jobs",
                  "Job count per R2 class (transparency of the numerator): counted / pending (audit in "
                  "progress) / clawed (payment reclaimed) / subsidized (free-tier) / operator_client (direct "
                  "self-dealing) / unpaid", ["bucket"])
g_r2_conf = Gauge("dendra_r2_exclusions_unresolved",
                  "1 when no subsidy account could be resolved (neither DENDRA_R2_EXCLUDE_ADDRS nor the "
                  "keyring): if a free-tier is running, R2 then over-counts the subsidy as demand -> set "
                  "DENDRA_R2_EXCLUDE_ADDRS explicitly. 0 when at least one exclusion is active")
# Public launch: the live triplet (never a rate alone) and judge-model diversity.
# In production there is no ground truth about who is honest, so this exports the raw material (hard
# slashes at quorum, stake seizures, unresolved audit jobs, veto margins, judge-model diversity) and
# leaves the reading to the launch runbook. At launch the network is presumed honest, so any hard slash
# is an incident to investigate before the announcement.
g_l_hard = Gauge("dendra_launch_hard_slash_jobs_total",
                 "Audit jobs resolved with the quorum of 'invalid' verdicts reached (>=4/5) = hard slash. "
                 "At launch, on a network presumed honest, this must stay 0 - any increment is an incident")
g_l_slashrec = Gauge("dendra_launch_slash_records_total",
                     "Cumulative number of SlashRecords carried by jobs (stake seizures, hard or light clawback)")
g_l_slashamt = Gauge("dendra_launch_slash_amount_total_dndr",
                     "Cumulative amount of the SlashRecords (DNDR) - the raw bleed observable on-chain")
g_l_unres = Gauge("dendra_launch_audit_unresolved",
                  "Disputed jobs not resolved (snapshot). Persistently >0 means silent judges or a timeout that "
                  "is too short (calibrate DENDRA_AUDIT_RESOLVE_TIMEOUT above the p95 of the 5th verdict)")
g_l_maxinv = Gauge("dendra_launch_veto_margin_max_invalid",
                   "Maximum number of 'invalid' verdicts observed on a job resolved without quorum (current "
                   "window): the distance to a slash (quorum=4). >=3 is one flip away from a hard slash -> "
                   "investigate the prompt or the judge")
g_l_models = Gauge("dendra_launch_judge_models_distinct",
                   "Number of distinct judge models observed in the on-chain verdicts (Commit.ModelId). "
                   "Must be >=2 at all times (heterogeneity by configuration)")
g_l_model_v = Gauge("dendra_launch_judge_verdicts", "On-chain verdicts observed per judge model", ["model"])
_L_VERDICT_CACHE: dict = {}   # jid -> {"votes": {mid: "0"/"1"}, "models": set, "resolved": bool} (resolved = immutable)
_L_SCAN_MAX = int(os.environ.get("DENDRA_LAUNCH_SCAN_MAX_JOBS", "50"))  # bounds the get-commit cost per cycle

R2_STOP_THRESHOLD = float(os.environ.get("DENDRA_R2_STOP_THRESHOLD", "1.0"))
R2_MIN_EMISSION_U = int(float(os.environ.get("DENDRA_R2_MIN_EMISSION_DNDR", "100")) * 1e6)
R2_REFRESH_S = float(os.environ.get("DENDRA_R2_REFRESH_S", "30"))  # list-job = full walk -> not every 5 s
# exclusions are re-resolved periodically (not only at boot) -> a
# rotation of the subsidy account after startup no longer silently over-counts the free-tier.
R2_EXCLUDE_REFRESH_S = float(os.environ.get("DENDRA_R2_EXCLUDE_REFRESH_S", "600"))
_R2_EXCLUDED: set = set()
_R2_NEXT = 0.0
_R2_EXCL_NEXT = 0.0


def _resolve_excluded():
    """Subsidy accounts to exclude from the R2 numerator. Sources: (1) DENDRA_R2_EXCLUDE_ADDRS (comma-
    separated addresses -- the reliable path in a deployment without a local keyring); (2) best-effort, the
    keyring resolution of the names DENDRA_R2_EXCLUDE_KEYS (default = DENDRA_SUBSIDY_CLIENT, 'bob' = the Reserve
    that settles the free-tier, cf. gateway.py). Silent keyring failure -> g_r2_conf signals it."""
    addrs = {a.strip() for a in os.environ.get("DENDRA_R2_EXCLUDE_ADDRS", "").split(",") if a.strip()}
    names = os.environ.get("DENDRA_R2_EXCLUDE_KEYS", os.environ.get("DENDRA_SUBSIDY_CLIENT", "bob"))
    for name in (n.strip() for n in names.split(",") if n.strip()):
        try:
            out = subprocess.run(["dendrad", "keys", "show", name, "-a", "--keyring-backend", "test"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out.startswith("dendra1") and " " not in out:
                addrs.add(out)
        except Exception:
            pass  # keyring absent (observation node) -> DENDRA_R2_EXCLUDE_ADDRS is authoritative
    return addrs


def refresh():
    st = dc.network_state()
    # `miners` is None when no registry was ever read in this process (client.network_state):
    # nothing is known about the miners, which is not zero miners. The gauges built on them keep their
    # value, R2 and the launch gauges (which need the operators) wait below; the rest still refreshes,
    # the anti-grinding alarm first among them.
    miners = st.get("miners")
    g_up.set(1 if (st.get("height", 0) or miners) else 0)
    g_height.set(st.get("height", 0))
    if miners is None:
        print("[exporter] the miner registry has never been read -> miner, R, R2 and launch gauges not "
              "updated (nothing measured yet)")
    else:
        g_miners.set(len(miners))
        # A balance not read is not zero (client.registered_miners keeps its row with `balance` None):
        # the total sums the balances read, and how many it leaves out is published beside it.
        read_bal = [m["balance"] for m in miners if isinstance(m.get("balance"), int)]
        g_bal_unread.set(len(miners) - len(read_bal))
        total = sum(read_bal)
        # purge old series then re-label only the top-N by stake -> bounded cardinality
        # (miner_id are created by third parties via create-miner; without a bound, explosion in an incentive testnet).
        g_stake.clear()
        g_bal.clear()
        for m in sorted(miners, key=lambda x: x.get("stake", 0), reverse=True)[:MAX_LABELED]:
            g_stake.labels(miner=m["id"]).set(m.get("stake", 0))
            if isinstance(m.get("balance"), int):
                g_bal.labels(miner=m["id"]).set(m["balance"] / 1e6)  # udndr -> DNDR; unread: no series
        g_total.set(total / 1e6)  # udndr -> DNDR (sum of the balances read)
    g_treasury.set(st.get("pools", {}).get("treasury", 0))
    released_u = RESERVE_INIT - dc.balance(EMISSION_ADDR)  # released = what left the emission module
    if released_u < 0:
        released_u = 0
    g_released.set(released_u / 1e6)
    if miners is not None:
        # settlement/emission ratio (farmable proxy, cf. header): Demand = cut-portion credited (client != operator).
        demand_u = sum(int(m.get("demand", 0) or 0) for m in miners)
        g_demand.set(demand_u / 1e6)
        g_r.set((demand_u / released_u) if released_u > 0 else 0.0)
    # VRF bootstrap: anti-grinding metric (makes the (b)/(c) fallback operable -> red panel).
    try:
        csh = dc.committee_seed_health()
        # A failed query sets nothing (the gauge keeps its last measured value): it is not a reading.
        # The seed is refused under either bar: the head count, or the contributors' share of the
        # commit power (decentralized_seed.go::MinVrfContributorPowerBps, 6667 bps).
        if csh is not None:
            grind = 1 if (csh["source"] == 1 and (not csh["has_seed"] or csh["contributors"] < csh["min"]
                                                   or csh["power_bps"] < 6667)) else 0
            g_grind.set(grind)
    except Exception:
        pass  # the grinding metric must not break the rest of the exporter
    # dedicated throttle (list-job = full walk of all jobs -> not on every 5 s refresh).
    global _R2_NEXT, _R2_EXCLUDED, _R2_EXCL_NEXT
    # R2 excludes the operators' own jobs: with no registry ever read it cannot, and an R2 over no
    # operators would count self-dealing as external demand. It waits, its throttle untouched.
    if miners is not None and time.time() >= _R2_NEXT:
        _R2_NEXT = time.time() + R2_REFRESH_S
        if time.time() >= _R2_EXCL_NEXT:  # periodic re-resolution (rotation of the subsidy account)
            _R2_EXCL_NEXT = time.time() + R2_EXCLUDE_REFRESH_S
            try:
                fresh = _resolve_excluded()
                if fresh:  # Never degrade to empty on a transient keyring failure
                    _R2_EXCLUDED = fresh
            except Exception:
                pass
        try:
            jobs = dc.list_jobs_full()
            if jobs is None:
                # Not measured: the R2 and launch gauges keep their last measured value, the same rule
                # as the seed-health gauge above. Zeros here would raise `dendra_r2_stop_breach` on an
                # outage of the node.
                print("[exporter] R2: list-job unreadable or incomplete -> R2 and launch gauges not updated "
                      "(they keep their last measured value)")
                return
            operators = {m.get("operator", "") for m in miners if m.get("operator")}
            fees_u, r2, buckets = compute_r2(jobs, operators, _R2_EXCLUDED, released_u)
            g_r2_fees.set(fees_u / 1e6)
            g_r2.set(r2)
            for b in R2_BUCKETS:
                g_r2_jobs.labels(bucket=b).set(buckets[b])
            # breach only beyond the significance floor (a near-zero emission would make
            # R2 insignificant -> no false red at the bootstrap of a fresh testnet).
            g_r2_stop.set(1 if (released_u >= R2_MIN_EMISSION_U and r2 < R2_STOP_THRESHOLD) else 0)
            g_r2_conf.set(0 if _R2_EXCLUDED else 1)
            _refresh_launch(jobs, [m.get("id", "") for m in miners if m.get("id")])
        except Exception as e:
            print(f"[exporter] R2: {type(e).__name__}: {e}")  # R2 never breaks the rest of the exporter


def _verdicts_of(jid, mids):
    """On-chain verdicts of a job (get-commit <jid>__verdict__<mid>). Bounded: called at most for
    _L_SCAN_MAX jobs/cycle, and resolved jobs are cached (immutable -> never re-queried)."""
    votes, models = {}, set()
    for m in mids:
        try:
            q = subprocess.run(["dendrad", "query", "jobs", "get-commit", f"{jid}__verdict__{m}",
                                "-o", "json", *dc._node()],
                               capture_output=True, text=True, timeout=10).stdout
            c = (json.loads(q).get("commit") or json.loads(q).get("Commit") or {}) if q.strip() else {}
            rc = (c.get("result_commit") or c.get("resultCommit") or "").strip()
            if rc in ("0", "1"):
                votes[m] = rc
                mdl = (c.get("model_id") or c.get("modelId") or "").strip()
                if mdl:
                    models.add(mdl)
        except Exception:
            pass
    return votes, models


def _refresh_launch(jobs, mids):
    """live triplet + judge diversity. Never breaks the rest (best-effort)."""
    try:
        hard = unres = recs = 0
        amt_u = 0
        max_inv_noquorum = 0
        all_models: set = set()
        model_counts: dict = {}
        disputed = [j for j in jobs if "disputed" in (j.get("state", "") or "")]
        # slash records: all jobs (hard + light clawback) -- the raw bleed.
        for j in jobs:
            for s in j.get("slashes", []) or []:
                a = int(s.get("amount", 0) or 0)
                if a > 0:
                    recs += 1
                    amt_u += a
        # verdicts: resolved jobs cached; scan bounded to the most recent for the pending ones.
        scan = disputed[-_L_SCAN_MAX:]
        for j in scan:
            jid = j.get("id", "")
            st = j.get("state", "") or ""
            resolved = "resolved" in st
            ent = _L_VERDICT_CACHE.get(jid)
            if not ent or (not ent["resolved"]):
                votes, models = _verdicts_of(jid, mids)
                ent = {"votes": votes, "models": models, "resolved": resolved}
                _L_VERDICT_CACHE[jid] = ent
            inv = sum(1 for v in ent["votes"].values() if v == "0")
            all_models |= ent["models"]
            for mdl in ent["models"]:
                model_counts[mdl] = model_counts.get(mdl, 0) + 1
            if resolved and inv >= 4:
                hard += 1
            elif resolved and inv > max_inv_noquorum:
                max_inv_noquorum = inv
            if not resolved:
                unres += 1
        g_l_hard.set(hard)
        g_l_slashrec.set(recs)
        g_l_slashamt.set(amt_u / 1e6)
        g_l_unres.set(unres)
        g_l_maxinv.set(max_inv_noquorum)
        g_l_models.set(len(all_models))
        g_l_model_v.clear()
        for mdl, n in sorted(model_counts.items())[:10]:  # bounded cardinality
            g_l_model_v.labels(model=mdl).set(n)
        if len(_L_VERDICT_CACHE) > 2000:  # memory bound (the oldest resolved ones drop out)
            for k in list(_L_VERDICT_CACHE)[:1000]:
                _L_VERDICT_CACHE.pop(k, None)
    except Exception as e:
        print(f"[exporter] launch: {type(e).__name__}: {e}")


def main():
    global _R2_EXCLUDED
    _R2_EXCLUDED = _resolve_excluded()  # subsidy accounts excluded from the numerator (once at boot)
    start_http_server(PORT, addr="127.0.0.1")
    print(f"[exporter] Dendra metrics on http://127.0.0.1:{PORT}/metrics")
    print(f"[exporter] R2: stop threshold {R2_STOP_THRESHOLD} (floor {R2_MIN_EMISSION_U/1e6:g} DNDR emitted); "
          f"subsidy exclusions = {sorted(_R2_EXCLUDED) if _R2_EXCLUDED else 'none (set DENDRA_R2_EXCLUDE_ADDRS if a free-tier runs)'}")
    while True:
        try:
            refresh()
        except Exception as e:
            g_up.set(0)
            print(f"[exporter] error: {type(e).__name__}: {e}")
        # Separate call, outside the chain try block: inside the same block, a silent relay would
        # skip the next `refresh()` and set `dendra_chain_up=0` — the chain would be blamed for a
        # relay outage. Two subjects, two verdicts.
        refresh_relay()
        time.sleep(5)


if __name__ == "__main__":
    main()
