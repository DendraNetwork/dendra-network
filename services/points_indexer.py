#!/usr/bin/env python3
"""points_indexer.py -- "Season 0" POINTS INDEXER (formula per the tokenomics plan).

DORMANT: runs only when launched explicitly; writes NOTHING on-chain; converts NOTHING. Points are
PROVISIONAL, traced, with no guaranteed value (banner in every payload). A point is NEVER declarative:
each point DERIVES from an on-chain fact already verified by the protocol (settled job, posted verdict, slash).

STATELESS DESIGN (= the "re-credit on vindication" Layer B is INTRINSIC): the indexer RECOMPUTES everything
from the CURRENT on-chain state at each refresh -- no local ledger. A job first in audit
(pending, not counted) then VINDICATED becomes `counted` again at the next recompute -> the honest party's
points reappear on their own, without correction logic. (Documented residual: a governance restitution
of a job left `clawed` on-chain is not visible at the job level -> manual handling, rare.)

Implemented formula:
  points(miner) = w_work · Σ fee of the FINAL POSITIVE jobs it served (counted, strict-finality)
                + w_judge · number of verdicts CONSISTENT with the job's outcome (invalid->clawed / valid->vindicated)
                + w_use  · Σ `counted` fee of the jobs it PAID as an external client (bucket)
                + w_avail · (DORMANT=0: no queryable presence history -- enable with the
                             pools/epochs query of a future regeneration)
                + w_val   · (DORMANT=0: per-validator VRF contribution not exposed -- same regeneration)
  × 0 if the miner carries >=1 SlashRecord on a `clawed` NON-vindicated job   # audit GATE (anti-farm)
  then CAP per identity: share <= cap_bps of the total (excess truncated)      # anti multi-account

Usage:  python3 points_indexer.py --once          # computes and prints the JSON leaderboard
        python3 points_indexer.py --serve         # read-only HTTP facade :8091 (/points, /health)
        python3 points_indexer.py --selftest      # pure logic, offline
Weights/caps (public, frozen at the Phase D snapshot): DENDRA_POINTS_W_WORK (1.0) / W_JUDGE (200) / W_USE (0.5)
/ CAP_BPS (2000 = 20% per identity) / OWNER_CAP_BPS (2000 = 20% per operator GROUP, anti-Sybil)
/ USE_SHARE_MAX_BPS (3000 = the USE bucket weighs <= 30% of the total, anti-self-dealing) / REFRESH_S (60).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# PURE core (offline selftest). Reuses the exporter's strict-finality classification.
# ---------------------------------------------------------------------------
R2_BUCKETS = ("counted", "pending", "clawed", "subsidized", "operator_client", "unpaid")


def r2_job_bucket(state, client, operators, excluded):
    """EXACT copy of exporter.r2_job_bucket (canonical source)."""
    st = state or ""
    if ("paid" not in st) and ("settled" not in st):
        return "unpaid"
    if "clawed" in st:
        return "clawed"
    if "disputed" in st and "resolved" not in st:
        return "pending"
    if client in excluded:
        return "subsidized"
    if client in operators:
        return "operator_client"
    return "counted"


def compute_points(jobs, miners, verdicts, weights=None, cap_bps=2000, excluded=None,
                   owner_cap_bps=2000, use_share_max_bps=3000):
    """Season 0 leaderboard (PURE).
    jobs     = list_jobs_full(): [{id,state,miner_id,client,fee,slashes:[{miner_id,amount}]}]
    miners   = [{id, operator, stake, ...}] (registered_miners())
    verdicts = {job_id: {judge_id: "0"|"1"}} (on-chain verdicts of RESOLVED audited jobs)
    Returns (sorted leaderboard, meta). Slash gate: a miner with >=1 SlashRecord on a clawed job -> 0.

    Two ANTI-SYBIL bounds on top of the per-identity cap (which did not see GROUPS):
      (i)  `use_share_max_bps`: the SUM of USE points (identities `client:*`) is capped to this share
           of the total -- client self-dealing (a 2nd address of the same human) is UNDETECTABLE (oracle), so we
           bound the GLOBAL WEIGHT that usage can carry (proportional scale-down of the client:*);
      (ii) `owner_cap_bps`: the MINERS of the same `operator` (the existing ON-CHAIN link) share ONE GROUP
           cap -- splitting one's GPU into N identities no longer multiplies the share (prorated truncation).

    The caps bound the FINAL share, via a CLOSED-FORM formula:
      cap_abs(key) = p/(1-p) × Σ(points of the OTHER keys)   [p = bps/10000]
    The naive `cap = p × total` is SELF-INCLUSIVE (the total contains the capped party's pre-cap points):
    a group with raw points 10× the rest lands at ~69% of the FINAL distribution for a nominal 20% cap
    -- exactly the threat the owner-cap targets. With the closed-form bound, a single truncated dominant ends
    at EXACTLY p of the final total (selftest "10× rest / cap 20%"). Simultaneous multi-dominants: each
    cap stays p/(1-p)×Σ(other raw) -- stricter than the self-inclusive form as soon as truncation occurs, without iteration
    (the "no iteration" spec choice; a strict fixed point would collapse a small leaderboard where k·p >= 1).
    CASCADE: each stage applies to the points POST-previous-stage.
    Order: gate ×0 -> (i) USE-share -> (ii) owner-cap -> per-identity cap."""
    w = {"work": 1.0, "judge": 200.0, "use": 0.5, "avail": 0.0, "val": 0.0}
    w.update(weights or {})
    operators = {m.get("operator", "") for m in miners or [] if m.get("operator")}
    excluded = excluded or set()

    slashed_gate = set()   # miners to gate x0: SlashRecord on a clawed NON-vindicated job (current state)
    pts = {}               # miner_id -> {work, judge, use, total}

    def _bucket_of(j):
        return r2_job_bucket(j.get("state", ""), j.get("client", ""), operators, excluded)

    for j in jobs or []:
        b = _bucket_of(j)
        mid = j.get("miner_id", "")
        fee = int(j.get("fee", 0) or 0)
        if b == "clawed":
            for s in j.get("slashes", []) or []:
                if int(s.get("amount", 0) or 0) > 0 and s.get("miner_id"):
                    slashed_gate.add(s["miner_id"])
            continue
        if b in ("counted", "subsidized", "operator_client") and mid:
            # WORK: the miner served a job with a positive outcome. Weighted by fee. (A subsidized/
            # self-dealing job is still WORK SERVED -- it's the USAGE dimension that requires external, not work.)
            d = pts.setdefault(mid, {"work": 0.0, "judge": 0.0, "use": 0.0})
            d["work"] += w["work"] * fee
        if b == "counted":
            # USAGE: credited to the CLIENT ADDRESS (prefix "client:", leaderboard separate from miners).
            # A client=operator NEVER arrives here (operator_client bucket); nor does the subsidy
            # (subsidized). Usage measures EXTERNAL MONEY entered with a positive outcome -- even if the job was
            # served by a miner later gated (the gate hits the dishonest server, not the honest payer).
            d = pts.setdefault("client:" + j.get("client", ""), {"work": 0.0, "judge": 0.0, "use": 0.0})
            d["use"] += w["use"] * fee

    # AUDIT: verdicts consistent with the job's on-chain OUTCOME (the protocol decided):
    #   clawed job -> verdict "0" (invalid) = consistent; resolved job NOT clawed -> verdict "1" = consistent.
    issue = {}
    for j in jobs or []:
        st = j.get("state", "") or ""
        if "disputed" in st and "resolved" in st:
            issue[j.get("id", "")] = "0" if "clawed" in st else "1"
    for jid, votes in (verdicts or {}).items():
        want = issue.get(jid)
        if want is None:
            continue
        for judge_id, v in (votes or {}).items():
            if (v or "").strip() == want:
                d = pts.setdefault(judge_id, {"work": 0.0, "judge": 0.0, "use": 0.0})
                d["judge"] += w["judge"]

    # slash GATE (x0), then totals
    rows = []
    for ident, d in pts.items():
        gated = ident in slashed_gate
        total = 0.0 if gated else (d["work"] + d["judge"] + d["use"])
        rows.append({"id": ident, "points": round(total, 3), "gated_slash": gated,
                     "detail": {k: round(v, 3) for k, v in d.items()}})
    tot = sum(r["points"] for r in rows)

    def _cap_abs(p_bps, others_sum):
        """CLOSED-FORM final-share bound: cap_abs = p/(1-p) × Σ(others). Final share of the truncated
        = cap/(cap+others) = p EXACTLY (single dominant). p=10000 (neutral) is filtered upstream."""
        p = p_bps / 10000.0
        return (p / (1.0 - p)) * others_sum

    # (i) USE BUCKET cap: FINAL share of Σ(client:*) <= use_share_max_bps.
    #     Closed-form against Σ(non-use); proportional scale-down of the client:*.
    use_scaled = False
    if tot > 0 and use_share_max_bps and use_share_max_bps < 10000:
        use_rows = [r for r in rows if r["id"].startswith("client:")]
        use_tot = sum(r["points"] for r in use_rows)
        non_use = tot - use_tot
        # non_use == 0 (pathological config w_work=0): no reference -> do not cap (meta flag).
        if non_use > 0:
            use_max = _cap_abs(use_share_max_bps, non_use)
            if use_tot > use_max and use_tot > 0:
                scale = use_max / use_tot
                for r in use_rows:
                    r["points"] = round(r["points"] * scale, 3)
                    r["use_scaled"] = True
                use_scaled = True

    # (ii) per-OPERATOR cap (group of miners linked on-chain): FINAL share of the group <= owner_cap.
    #      Closed-form against Σ(POST-(i) points outside the group); excess truncated PRORATED across members.
    #      A miner without an operator = singleton group.
    owner_capped = 0
    if tot > 0 and owner_cap_bps and owner_cap_bps < 10000:
        op_of = {m.get("id", ""): (m.get("operator", "") or "") for m in miners or []}
        groups = {}
        for r in rows:
            if r["id"].startswith("client:"):
                continue
            key = op_of.get(r["id"], "") or ("solo:" + r["id"])
            groups.setdefault(key, []).append(r)
        cur_tot = sum(r["points"] for r in rows)  # post-stage-(i)
        for key, members in sorted(groups.items()):
            gsum = sum(r["points"] for r in members)
            owner_cap = _cap_abs(owner_cap_bps, cur_tot - gsum)  # others = everything EXCEPT the group (client:* included)
            if gsum > owner_cap and gsum > 0:
                scale = owner_cap / gsum
                for r in members:
                    r["points"] = round(r["points"] * scale, 3)
                    r["owner_capped"] = key
                owner_capped += 1

    # per-identity CAP: FINAL share <= cap_bps (anti multi-account; excess TRUNCATED at the closed-form bound,
    # computed against the POST-(ii) points of the OTHER identities -- single-shot: each cap reads the
    # ENTRY points of the stage, the spec's cascade is BETWEEN stages) -- the client:* stay subject to it.
    capped = 0
    if tot > 0 and cap_bps and cap_bps < 10000:
        stage_tot = sum(r["points"] for r in rows)  # post-stage-(ii), FROZEN for the whole stage
        for r in rows:
            cap = _cap_abs(cap_bps, stage_tot - r["points"])
            if r["points"] > cap:
                r["points"] = round(cap, 3)
                r["capped"] = True
                capped += 1
    rows.sort(key=lambda r: (-r["points"], r["id"]))
    meta = {"weights": w, "cap_bps": cap_bps, "owner_cap_bps": owner_cap_bps,
            "use_share_max_bps": use_share_max_bps, "identities": len(rows),
            "gated_slash": sorted(slashed_gate), "capped": capped,
            "owner_groups_capped": owner_capped, "use_bucket_scaled": use_scaled,
            "share_caps": "closed-form p/(1-p) x others: the FINAL share is bounded",
            "dormant": {"avail": "no queryable presence history yet",
                        "val": "per-validator VRF contribution not exposed yet"}}
    return rows, meta


def _selftest():
    miners = [{"id": "m1", "operator": "opA"}, {"id": "m2", "operator": "opB"},
              {"id": "cheater", "operator": "opC"}, {"id": "juge1", "operator": "opJ"}]
    jobs = [
        {"id": "j1", "state": "open+paid+finalized", "miner_id": "m1", "client": "ext1", "fee": 1000, "slashes": []},
        {"id": "j2", "state": "open+paid+optimistic", "miner_id": "m1", "client": "ext1", "fee": 500, "slashes": []},
        {"id": "j3", "state": "open+paid+optimistic+disputed", "miner_id": "m2", "client": "ext1", "fee": 800, "slashes": []},          # pending -> 0 points (will re-credit if vindicated = Layer B)
        {"id": "j4", "state": "open+paid+optimistic+disputed+resolved", "miner_id": "m2", "client": "ext1", "fee": 700, "slashes": []},  # vindicated -> counts
        {"id": "j5", "state": "open+paid+optimistic+disputed+resolved+clawed", "miner_id": "cheater", "client": "ext1", "fee": 900,
         "slashes": [{"miner_id": "cheater", "amount": 320000}]},                                                                        # clawed -> gate
        {"id": "j6", "state": "open+paid+finalized", "miner_id": "cheater", "client": "ext1", "fee": 600, "slashes": []},               # even clean work: gate x0
        {"id": "j7", "state": "open+paid+finalized", "miner_id": "m1", "client": "bobaddr", "fee": 400, "slashes": []},                  # subsidized: work yes, usage no
        {"id": "j8", "state": "open+paid+finalized", "miner_id": "m1", "client": "opB", "fee": 300, "slashes": []},                      # client=operator: work yes, usage no
    ]
    verdicts = {"j5": {"juge1": "0", "m2": "1"},   # juge1 consistent (clawed->0), m2 inconsistent
                "j4": {"juge1": "1"}}              # consistent (vindicated->1)
    rows, meta = compute_points(jobs, miners, verdicts, cap_bps=10000, excluded={"bobaddr"},
                                owner_cap_bps=10000, use_share_max_bps=10000)  # neutral caps: historical cases intact
    by = {r["id"]: r for r in rows}
    ok = True

    def chk(label, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    chk("m1 work = 1000+500+400+300 (subsidized/operator client = work served)", by["m1"]["detail"]["work"] == 2200.0)
    chk("m2 = 700 (pending j3 NOT counted; vindicated j4 counted = Layer B)",
        by["m2"]["detail"]["work"] == 700.0)
    chk("cheater GATED x0 (slash on a clawed job) despite the clean j6", by["cheater"]["points"] == 0.0 and by["cheater"]["gated_slash"])
    chk("juge1 = 2 consistent verdicts x 200", by["juge1"]["detail"]["judge"] == 400.0)
    chk("m2 verdict j5='1' INCONSISTENT -> no judge points", by["m2"]["detail"]["judge"] == 0.0)
    # j1+j2+j4+j6: j6 counts TOO (the slash gate hits the cheating MINER, not the honest CLIENT who paid
    # a job with a positive outcome -- usage measures external money entered, not the server's virtue).
    chk("usage: client ext1 credited (counted only, j6 included)",
        by["client:ext1"]["detail"]["use"] == (1000 + 500 + 700 + 600) * 0.5)
    chk("usage: bobaddr (subsidized) and opB (operator) NOT credited",
        "client:bobaddr" not in by and "client:opB" not in by)
    # CAP: redo with a 20% cap -> nobody above 20% of the total
    rows2, _ = compute_points(jobs, miners, verdicts, cap_bps=2000, excluded={"bobaddr"},
                              owner_cap_bps=10000, use_share_max_bps=10000)
    tot2_pre = sum(r["points"] for r in rows)  # uncapped base (cap_bps=10000)
    chk("20% cap applied (max <= 20% of the uncapped total + tolerance)",
        max(r["points"] for r in rows2) <= 0.2 * tot2_pre + 1e-6)
    chk("leaderboard sorted descending", rows == sorted(rows, key=lambda r: (-r["points"], r["id"])))
    chk("meta exposes the dormant dimensions", "avail" in meta["dormant"] and "val" in meta["dormant"])

    # --- per-OPERATOR (group) cap + USE bucket cap ---
    miners6 = [{"id": "a1", "operator": "opX"}, {"id": "a2", "operator": "opX"}, {"id": "b1", "operator": "opY"}]
    jobs6 = [
        {"id": "k1", "state": "open+paid+finalized", "miner_id": "a1", "client": "extA", "fee": 4000, "slashes": []},
        {"id": "k2", "state": "open+paid+finalized", "miner_id": "a2", "client": "extA", "fee": 4000, "slashes": []},
        {"id": "k3", "state": "open+paid+finalized", "miner_id": "b1", "client": "extA", "fee": 2000, "slashes": []},
    ]
    # points: a1=4000, a2=4000, b1=2000, client:extA use=0.5×10000=5000 -> tot=15000.
    # closed-form bound: cap_abs(opX) = 0.5/(1-0.5) × (2000+5000) = 7000; group opX = 8000 > 7000
    # -> prorate ×0.875 -> a1=a2=3500; group FINAL share = 7000/14000 = 50% EXACT; opY intact.
    rows6, meta6 = compute_points(jobs6, miners6, {}, cap_bps=10000, owner_cap_bps=5000, use_share_max_bps=10000)
    by6 = {r["id"]: r for r in rows6}
    chk("owner-cap: group opX (2 miners, 8000) truncated prorated to 7000 (final share 50%)",
        by6["a1"]["points"] == 3500.0 and by6["a2"]["points"] == 3500.0)
    chk("owner-cap: singleton opY below the cap is untouched", by6["b1"]["points"] == 2000.0)
    chk("owner-cap: splitting does NOT increase the share (meta counts 1 capped group)",
        meta6["owner_groups_capped"] == 1)
    # use-share 20% (closed-form): use_max = 0.2/0.8 × 10000 (non-use) = 2500; USE bucket 5000 -> scale
    # to 2500 -> FINAL share = 2500/12500 = 20% EXACT; miners unchanged.
    rows7, meta7 = compute_points(jobs6, miners6, {}, cap_bps=10000, owner_cap_bps=10000, use_share_max_bps=2000)
    by7 = {r["id"]: r for r in rows7}
    chk("use-share: USE bucket capped at a FINAL share of 20% (2500)",
        by7["client:extA"]["points"] == 2500.0 and by7["a1"]["points"] == 4000.0)
    chk("use-share: meta reports it", meta7["use_bucket_scaled"] is True)

    # --- THE case the self-inclusive form gets wrong -- group raw points = 10× the rest,
    #     nominal 20% cap -> a naive `cap = p×total` leaves it ~69% of FINAL share;
    #     the closed-form bound must make the FINAL share <= 20% STRICT (and = 20% exactly, dominant truncated).
    minersF6 = [{"id": "g1", "operator": "opG"}, {"id": "g2", "operator": "opG"},
                {"id": "s1", "operator": "opS"}]
    jobsF6 = [
        {"id": "f1", "state": "open+paid+finalized", "miner_id": "g1", "client": "extF", "fee": 5000, "slashes": []},
        {"id": "f2", "state": "open+paid+finalized", "miner_id": "g2", "client": "extF", "fee": 5000, "slashes": []},
        {"id": "f3", "state": "open+paid+finalized", "miner_id": "s1", "client": "extF", "fee": 1000, "slashes": []},
    ]
    # w_use=0 to isolate the case "group 10000 vs rest 1000"; cap_abs(opG) = 0.2/0.8×1000 = 250.
    rowsF6, _mF6 = compute_points(jobsF6, minersF6, {}, weights={"use": 0.0},
                                  cap_bps=10000, owner_cap_bps=2000, use_share_max_bps=10000)
    byF6 = {r["id"]: r for r in rowsF6}
    totF6 = sum(r["points"] for r in rowsF6)
    shareF6 = (byF6["g1"]["points"] + byF6["g2"]["points"]) / totF6 if totF6 else 1.0
    chk("group raw points = 10x the rest, 20% cap -> FINAL share <= 20% STRICT (self-inclusive cap: ~69%)",
        shareF6 <= 0.20 + 1e-9)
    chk("single dominant truncated at the closed-form bound = final share EXACTLY 20%",
        abs(shareF6 - 0.20) <= 1e-9 and byF6["g1"]["points"] == 125.0 and byF6["s1"]["points"] == 1000.0)
    print("SELFTEST POINTS-INDEXER", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # BEFORE the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess

import client as dc

W = {"work": float(os.environ.get("DENDRA_POINTS_W_WORK", "1.0")),
     "judge": float(os.environ.get("DENDRA_POINTS_W_JUDGE", "200")),
     "use": float(os.environ.get("DENDRA_POINTS_W_USE", "0.5"))}
CAP_BPS = int(os.environ.get("DENDRA_POINTS_CAP_BPS", "2000"))
# GROUP anti-Sybil caps -- cf. compute_points (i)/(ii).
OWNER_CAP_BPS = int(os.environ.get("DENDRA_POINTS_OWNER_CAP_BPS", "2000"))
USE_SHARE_MAX_BPS = int(os.environ.get("DENDRA_POINTS_USE_SHARE_MAX_BPS", "3000"))
REFRESH_S = float(os.environ.get("DENDRA_POINTS_REFRESH_S", "60"))
EXCLUDED = {a.strip() for a in os.environ.get("DENDRA_R2_EXCLUDE_ADDRS", "").split(",") if a.strip()}


def _fetch_verdicts(jobs, miners):
    """On-chain verdicts of RESOLVED audited jobs: get-commit <jid>__verdict__<mid> (bounded best-effort)."""
    node = os.environ.get("DENDRA_NODE", "")
    node_arg = ["--node", node] if node else []
    out = {}
    mids = [m.get("id", "") for m in miners if m.get("id")]
    for j in jobs:
        st = j.get("state", "") or ""
        if not ("disputed" in st and "resolved" in st):
            continue
        jid = j.get("id", "")
        votes = {}
        for m in mids:
            if m == j.get("miner_id"):
                continue
            try:
                q = subprocess.run(["dendrad", "query", "jobs", "get-commit", f"{jid}__verdict__{m}",
                                    "-o", "json", *node_arg], capture_output=True, text=True, timeout=15).stdout
                c = json.loads(q).get("commit") or json.loads(q).get("Commit") or {}
                rc = (c.get("result_commit") or c.get("resultCommit") or "").strip()
                if rc in ("0", "1"):
                    votes[m] = rc
            except Exception:
                pass
        if votes:
            out[jid] = votes
    return out


def _fold_clients(rows: list) -> list:
    """Clients are counted, never named.

    Without this fold the ranking carries one row per PAYING CLIENT, keyed by its bech32 address
    (`client:dendra1...`), together with its consumption volume — on a port with no authentication and
    no route in Caddy, on a surface no public page announces. Miners have a reason to be named: they
    are paid, they are drawn, they are slashable, and their track record is the whole point. A client
    has none: it pays and leaves. Publishing what each address consumes serves nothing the protocol
    needs, and it is the kind of record that cannot be withdrawn once read.

    Hashing the address is NOT enough: the set of on-chain addresses is public and enumerable, so
    anyone can hash all of them and match. Only folding removes the information.

    The fold happens ON THE WAY OUT. The per-client rows still exist inside compute_points, because the
    usage cap (`use_share_max_bps`) scales each of them down proportionally — touching that would
    change the economics, not the privacy. Sum, count, and the cap all stay exact."""
    kept, use, n = [], 0.0, 0
    for r in rows:
        if str(r.get("id", "")).startswith("client:"):
            use += float(r.get("points", 0.0))
            n += 1
        else:
            kept.append(r)
    if n:
        kept.append({"id": "clients (aggregated)", "points": round(use, 4),
                     "detail": {"use": round(use, 4)}, "clients": n,
                     "note": "Per-client addresses are deliberately not published. Miners are named "
                             "because they are accountable; clients are not."})
        kept.sort(key=lambda r: (-r.get("points", 0.0), str(r.get("id", ""))))
    return kept


def snapshot():
    jobs = dc.list_jobs_full()
    miners = dc.registered_miners()
    verdicts = _fetch_verdicts(jobs, miners)
    rows, meta = compute_points(jobs, miners, verdicts, weights=W, cap_bps=CAP_BPS, excluded=EXCLUDED,
                                owner_cap_bps=OWNER_CAP_BPS, use_share_max_bps=USE_SHARE_MAX_BPS)
    return {
        "season": "0", "provisional": True, "generated_at": int(time.time()),
        "leaderboard": _fold_clients(rows), "meta": meta,
        "_provenance": {
            "claim": "Points DERIVED from the current on-chain state (full stateless recomputation). PROVISIONAL.",
            "note": "No conversion is promised (Phase C is not enabled). A late vindication makes the honest "
                    "party's points reappear at the next recomputation (Layer B is intrinsic). No anti-Sybil "
                    "measure is perfect: the caps, the slash gate and the GPU/bond cost make farming more "
                    "expensive than its gain.",
        },
    }


def main():
    if "--once" in sys.argv:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        return
    # --serve: read-only HTTP facade (the_proof.py pattern)
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    host = os.environ.get("DENDRA_POINTS_HOST", "127.0.0.1")
    port = int(os.environ.get("DENDRA_POINTS_PORT", "8091"))
    cors = os.environ.get("DENDRA_POINTS_CORS", "")
    cache = {"t": 0.0, "payload": b"{}"}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if cors:
                self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] in ("/points", "/"):
                if time.time() - cache["t"] >= REFRESH_S:
                    try:
                        cache["payload"] = json.dumps(snapshot(), ensure_ascii=False).encode()
                        cache["t"] = time.time()
                    except Exception as e:
                        sys.stderr.write(f"[points] refresh: {type(e).__name__}: {e}\n")
                self._send(200, cache["payload"])
            elif self.path == "/health":
                self._send(200, b'{"status":"ok"}')
            else:
                self._send(404, b'{"error":"not found"}')

        def log_message(self, *a):
            pass

    print(f"[points] Season 0 indexer (PROVISIONAL, dormant) on http://{host}:{port}/points "
          f"(weights {W}, cap {CAP_BPS} bps, refresh {REFRESH_S:g}s)")
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
