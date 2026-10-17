#!/usr/bin/env python3
"""points_indexer.py -- "Season 0" points indexer (formula per the tokenomics plan).

Dormant: runs only when launched explicitly; writes nothing on-chain; converts nothing. Points are
provisional, traced, with no guaranteed value (banner in every payload). A point is never declarative:
each point derives from an on-chain fact already verified by the protocol (settled job, posted verdict, slash).

Stateless design (= the "re-credit on vindication" Layer B is intrinsic): the indexer recomputes everything
from the current on-chain state at each refresh -- no local ledger. A job first in audit
(pending, not counted) then vindicated becomes `counted` again at the next recompute -> the honest party's
points reappear on their own, without correction logic. (Documented residual: a governance restitution
of a job left `clawed` on-chain is not visible at the job level -> manual handling, rare.)

Implemented formula:
  points(miner) = w_work · Σ fee of the final positive jobs it served (counted, strict-finality)
                + w_judge · number of verdicts consistent with the job's outcome (invalid->clawed / valid->vindicated)
                + w_use  · Σ `counted` fee of the jobs it paid as an external client (bucket)
                + w_avail · (dormant=0: no queryable presence history -- enable with the
                             pools/epochs query of a future regeneration)
                + w_val   · (dormant=0: per-validator VRF contribution not exposed -- same regeneration)
  × 0 if the miner carries >=1 SlashRecord on a `clawed` non-vindicated job   # audit gate (anti-farm)
  then cap per identity: share <= cap_bps of the total (excess truncated)      # anti multi-account

Usage:  python3 points_indexer.py --once          # computes and prints the JSON leaderboard
        python3 points_indexer.py --serve         # read-only HTTP facade :8091 (/points, /health)
        python3 points_indexer.py --selftest      # pure logic, offline
Weights/caps (public, frozen at the Phase D snapshot): DENDRA_POINTS_W_WORK (1.0) / W_JUDGE (200) / W_USE (0.5)
/ CAP_BPS (2000 = 20% per identity) / OWNER_CAP_BPS (2000 = 20% per operator group, anti-Sybil)
/ USE_SHARE_MAX_BPS (3000 = the use bucket weighs <= 30% of the total, anti-self-dealing) / REFRESH_S (60).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure core (offline selftest). Reuses the exporter's strict-finality classification.
# ---------------------------------------------------------------------------
R2_BUCKETS = ("counted", "pending", "clawed", "subsidized", "operator_client", "unpaid")


def r2_job_bucket(state, client, operators, excluded):
    """Exact copy of exporter.r2_job_bucket (canonical source)."""
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
    """Season 0 leaderboard (pure).
    jobs     = list_jobs_full(): [{id,state,miner_id,client,fee,slashes:[{miner_id,amount}]}]
    miners   = [{id, operator, stake, ...}] (registered_miners())
    verdicts = {job_id: {judge_id: "0"|"1"}} (on-chain verdicts of resolved audited jobs)
    Returns (sorted leaderboard, meta). Slash gate: a miner with >=1 SlashRecord on a clawed job -> 0.

    Two anti-Sybil bounds on top of the per-identity cap, which does not see groups:
      (i)  `use_share_max_bps`: the sum of use points (identities `client:*`) is capped to this share of
           the total. A client paying itself from a second address cannot be detected, so the total weight
           usage can carry is bounded instead (the client:* entries are scaled down proportionally);
      (ii) `owner_cap_bps`: the miners of the same `operator` (the existing on-chain link) share one group
           cap, so splitting one GPU into N identities does not multiply the share (prorated truncation).

    The caps bound the final share, via a closed-form formula:
      cap_abs(key) = p/(1-p) × Σ(points of the other keys)   [p = bps/10000]
    The naive `cap = p × total` is self-inclusive (the total contains the capped party's pre-cap points):
    a group with raw points 10× the rest lands at ~69% of the final distribution for a nominal 20% cap
    -- exactly the threat the owner-cap targets. With the closed-form bound, a single truncated dominant ends
    at exactly p of the final total (selftest "10× rest / cap 20%"). Simultaneous multi-dominants: each
    cap stays p/(1-p)×Σ(other raw) -- stricter than the self-inclusive form as soon as truncation occurs, without iteration
    (the "no iteration" spec choice; a strict fixed point would collapse a small leaderboard where k·p >= 1).
    Cascade: each stage applies to the points POST-previous-stage.
    Order: gate ×0 -> (i) use-share -> (ii) owner-cap -> per-identity cap.

    `jobs=None` (the listing could not be read) raises: a leaderboard computed over no job erases
    every miner's work and every slash gate at once, and reads exactly like a network with no work."""
    if jobs is None:
        raise ValueError("jobs not measured (list-job unreadable): no leaderboard over an unread listing")
    w = {"work": 1.0, "judge": 200.0, "use": 0.5, "avail": 0.0, "val": 0.0}
    w.update(weights or {})
    operators = {m.get("operator", "") for m in miners or [] if m.get("operator")}
    excluded = excluded or set()

    slashed_gate = set()   # miners to gate x0: SlashRecord on a clawed non-vindicated job (current state)
    pts = {}               # miner_id -> {work, judge, use, total}

    def _bucket_of(j):
        return r2_job_bucket(j.get("state", ""), j.get("client", ""), operators, excluded)

    for j in jobs:
        b = _bucket_of(j)
        mid = j.get("miner_id", "")
        fee = int(j.get("fee", 0) or 0)
        if b == "clawed":
            for s in j.get("slashes", []) or []:
                if int(s.get("amount", 0) or 0) > 0 and s.get("miner_id"):
                    slashed_gate.add(s["miner_id"])
            continue
        if b in ("counted", "subsidized", "operator_client") and mid:
            # Work: the miner served a job with a positive outcome. Weighted by fee. (A subsidized/
            # self-dealing job is still work served -- it's the usage dimension that requires external, not work.)
            d = pts.setdefault(mid, {"work": 0.0, "judge": 0.0, "use": 0.0})
            d["work"] += w["work"] * fee
        if b == "counted":
            # Usage: credited to the client address (prefix "client:", leaderboard separate from miners).
            # A client=operator never arrives here (operator_client bucket); nor does the subsidy
            # (subsidized). Usage measures external money entered with a positive outcome -- even if the job was
            # served by a miner later gated (the gate hits the dishonest server, not the honest payer).
            d = pts.setdefault("client:" + j.get("client", ""), {"work": 0.0, "judge": 0.0, "use": 0.0})
            d["use"] += w["use"] * fee

    # Audit: verdicts consistent with the job's on-chain outcome (the protocol decided):
    #   clawed job -> verdict "0" (invalid) = consistent; resolved job not clawed -> verdict "1" = consistent.
    issue = {}
    for j in jobs:
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

    # slash gate (x0), then totals
    rows = []
    for ident, d in pts.items():
        gated = ident in slashed_gate
        total = 0.0 if gated else (d["work"] + d["judge"] + d["use"])
        rows.append({"id": ident, "points": round(total, 3), "gated_slash": gated,
                     "detail": {k: round(v, 3) for k, v in d.items()}})
    tot = sum(r["points"] for r in rows)

    def _cap_abs(p_bps, others_sum):
        """Closed-form final-share bound: cap_abs = p/(1-p) × Σ(others). Final share of the truncated
        = cap/(cap+others) = p exactly (single dominant). p=10000 (neutral) is filtered upstream."""
        p = p_bps / 10000.0
        return (p / (1.0 - p)) * others_sum

    # (i) Use bucket cap: final share of Σ(client:*) <= use_share_max_bps.
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

    # (ii) per-operator cap (group of miners linked on-chain): final share of the group <= owner_cap.
    #      Closed-form against Σ(POST-(i) points outside the group); excess truncated prorated across members.
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
            owner_cap = _cap_abs(owner_cap_bps, cur_tot - gsum)  # others = everything except the group (client:* included)
            if gsum > owner_cap and gsum > 0:
                scale = owner_cap / gsum
                for r in members:
                    r["points"] = round(r["points"] * scale, 3)
                    r["owner_capped"] = key
                owner_capped += 1

    # per-identity cap: final share <= cap_bps (anti multi-account; excess truncated at the closed-form bound,
    # computed against the POST-(ii) points of the other identities -- single-shot: each cap reads the
    # entry points of the stage, the spec's cascade is between stages) -- the client:* stay subject to it.
    capped = 0
    if tot > 0 and cap_bps and cap_bps < 10000:
        stage_tot = sum(r["points"] for r in rows)  # post-stage-(ii), frozen for the whole stage
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
            "share_caps": "closed-form p/(1-p) x others: the final share is bounded",
            "dormant": {"avail": "no queryable presence history yet",
                        "val": "per-validator VRF contribution not exposed yet"}}
    return rows, meta


class Unmeasured(RuntimeError):
    """A read the leaderboard depends on could not be made. Its message is served in the public
    payload, so it names what was not read, never a host, a path or a command line."""


NOT_YET_BUILT = "first refresh not completed"


def new_cache():
    """The served state before any refresh has completed. Its payload is marked, never `{}`: the
    refresh lock does not make a request wait, so a request arriving during the first walk is served
    this payload, and an empty object has no `refresh` field at all. Same shape as a refresh that
    failed with nothing to keep; `attempted_at` is null because no attempt has completed."""
    payload = {"refresh": {"current": False, "attempted_at": None, "failing_since": None,
                           "reason": NOT_YET_BUILT, "snapshot_generated_at": None}}
    return {"t": 0.0, "payload": json.dumps(payload, ensure_ascii=False).encode(), "doc": None,
            "failing_since": None}


def publish(cache, now, build):
    """One refresh of the served leaderboard; True when it was rebuilt. Pure apart from `build()`.
    Same contract as `the_proof.publish`.

    On success the payload is rebuilt and marked current. On any failure the last leaderboard built
    is kept and served, and it says so: `refresh.current` is false, with the time the failures began
    and their reason. Rebuilding from what could not be read would erase every miner's work at once;
    serving the old leaderboard without a mark would present it as fresh. Nothing in a leaderboard is
    computed against the clock of the build apart from `generated_at`, which dates it, so a kept one
    is served whole.

    The reason is public: an `Unmeasured` message is written to be served; any other failure serves
    its type only, its message going to the operator's log."""
    try:
        doc = build()
        payload = dict(doc)
        payload["refresh"] = {"current": True, "attempted_at": int(now), "failing_since": None,
                              "reason": None, "snapshot_generated_at": doc.get("generated_at")}
        body = json.dumps(payload, ensure_ascii=False).encode()
    except Unmeasured as e:
        sys.stderr.write(f"[points] refresh: {e}\n")
        reason = str(e)
    except Exception as e:  # noqa: BLE001 -- the facade never breaks: it serves the last leaderboard
        sys.stderr.write(f"[points] refresh: {type(e).__name__}: {e}\n")
        reason = f"refresh failed ({type(e).__name__})"
    else:
        cache["doc"] = doc
        cache["failing_since"] = None
        cache["payload"] = body
        return True
    if cache.get("failing_since") is None:
        cache["failing_since"] = int(now)
    kept = cache.get("doc")
    payload = dict(kept) if kept is not None else {}
    payload["refresh"] = {"current": False, "attempted_at": int(now),
                          "failing_since": cache["failing_since"], "reason": reason,
                          "snapshot_generated_at": None if kept is None else kept.get("generated_at")}
    cache["payload"] = json.dumps(payload, ensure_ascii=False).encode()
    return False


def _selftest():
    miners = [{"id": "m1", "operator": "opA"}, {"id": "m2", "operator": "opB"},
              {"id": "cheater", "operator": "opC"}, {"id": "judge1", "operator": "opJ"}]
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
    verdicts = {"j5": {"judge1": "0", "m2": "1"},   # judge1 consistent (clawed->0), m2 inconsistent
                "j4": {"judge1": "1"}}              # consistent (vindicated->1)
    rows, meta = compute_points(jobs, miners, verdicts, cap_bps=10000, excluded={"bobaddr"},
                                owner_cap_bps=10000, use_share_max_bps=10000)  # neutral caps: historical cases intact
    by = {r["id"]: r for r in rows}
    ok = True

    def chk(label, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    chk("m1 work = 1000+500+400+300 (subsidized/operator client = work served)", by["m1"]["detail"]["work"] == 2200.0)
    chk("m2 = 700 (pending j3 not counted; vindicated j4 counted = Layer B)",
        by["m2"]["detail"]["work"] == 700.0)
    chk("cheater gated x0 (slash on a clawed job) despite the clean j6", by["cheater"]["points"] == 0.0 and by["cheater"]["gated_slash"])
    chk("judge1 = 2 consistent verdicts x 200", by["judge1"]["detail"]["judge"] == 400.0)
    chk("m2 verdict j5='1' inconsistent -> no judge points", by["m2"]["detail"]["judge"] == 0.0)
    # j1+j2+j4+j6: j6 counts too (the slash gate hits the cheating miner, not the honest client who paid
    # a job with a positive outcome -- usage measures external money entered, not the server's virtue).
    chk("usage: client ext1 credited (counted only, j6 included)",
        by["client:ext1"]["detail"]["use"] == (1000 + 500 + 700 + 600) * 0.5)
    chk("usage: bobaddr (subsidized) and opB (operator) not credited",
        "client:bobaddr" not in by and "client:opB" not in by)
    # Cap: redo with a 20% cap -> nobody above 20% of the total
    rows2, _ = compute_points(jobs, miners, verdicts, cap_bps=2000, excluded={"bobaddr"},
                              owner_cap_bps=10000, use_share_max_bps=10000)
    tot2_pre = sum(r["points"] for r in rows)  # uncapped base (cap_bps=10000)
    chk("20% cap applied (max <= 20% of the uncapped total + tolerance)",
        max(r["points"] for r in rows2) <= 0.2 * tot2_pre + 1e-6)
    chk("leaderboard sorted descending", rows == sorted(rows, key=lambda r: (-r["points"], r["id"])))
    chk("meta exposes the dormant dimensions", "avail" in meta["dormant"] and "val" in meta["dormant"])

    # --- per-operator (group) cap + use bucket cap ---
    miners6 = [{"id": "a1", "operator": "opX"}, {"id": "a2", "operator": "opX"}, {"id": "b1", "operator": "opY"}]
    jobs6 = [
        {"id": "k1", "state": "open+paid+finalized", "miner_id": "a1", "client": "extA", "fee": 4000, "slashes": []},
        {"id": "k2", "state": "open+paid+finalized", "miner_id": "a2", "client": "extA", "fee": 4000, "slashes": []},
        {"id": "k3", "state": "open+paid+finalized", "miner_id": "b1", "client": "extA", "fee": 2000, "slashes": []},
    ]
    # points: a1=4000, a2=4000, b1=2000, client:extA use=0.5×10000=5000 -> tot=15000.
    # closed-form bound: cap_abs(opX) = 0.5/(1-0.5) × (2000+5000) = 7000; group opX = 8000 > 7000
    # -> prorate ×0.875 -> a1=a2=3500; group final share = 7000/14000 = 50% exact; opY intact.
    rows6, meta6 = compute_points(jobs6, miners6, {}, cap_bps=10000, owner_cap_bps=5000, use_share_max_bps=10000)
    by6 = {r["id"]: r for r in rows6}
    chk("owner-cap: group opX (2 miners, 8000) truncated prorated to 7000 (final share 50%)",
        by6["a1"]["points"] == 3500.0 and by6["a2"]["points"] == 3500.0)
    chk("owner-cap: singleton opY below the cap is untouched", by6["b1"]["points"] == 2000.0)
    chk("owner-cap: splitting does not increase the share (meta counts 1 capped group)",
        meta6["owner_groups_capped"] == 1)
    # use-share 20% (closed-form): use_max = 0.2/0.8 × 10000 (non-use) = 2500; use bucket 5000 -> scale
    # to 2500 -> final share = 2500/12500 = 20% exact; miners unchanged.
    rows7, meta7 = compute_points(jobs6, miners6, {}, cap_bps=10000, owner_cap_bps=10000, use_share_max_bps=2000)
    by7 = {r["id"]: r for r in rows7}
    chk("use-share: use bucket capped at a final share of 20% (2500)",
        by7["client:extA"]["points"] == 2500.0 and by7["a1"]["points"] == 4000.0)
    chk("use-share: meta reports it", meta7["use_bucket_scaled"] is True)

    # --- The case the self-inclusive form gets wrong -- group raw points = 10× the rest,
    #     nominal 20% cap -> a naive `cap = p×total` leaves it ~69% of final share;
    #     the closed-form bound must make the final share <= 20% strict (and = 20% exactly, dominant truncated).
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
    chk("group raw points = 10x the rest, 20% cap -> final share <= 20% strict (self-inclusive cap: ~69%)",
        shareF6 <= 0.20 + 1e-9)
    chk("single dominant truncated at the closed-form bound = final share exactly 20%",
        abs(shareF6 - 0.20) <= 1e-9 and byF6["g1"]["points"] == 125.0 and byF6["s1"]["points"] == 1000.0)
    try:
        compute_points(None, minersF6, {})
        unread_raises = False
    except ValueError:
        unread_raises = True
    chk("jobs=None (unread listing) raises -- never a leaderboard that erased everyone's work",
        unread_raises)

    # --- the served leaderboard across an outage of the node ---
    def _built():
        return {"season": "0", "generated_at": 100, "leaderboard": [{"id": "m1", "points": 7.0}]}

    def _unread():
        raise Unmeasured("list-job unreadable or incomplete: no leaderboard is computed (not measured)")

    served = new_cache()
    unbuilt = json.loads(served["payload"])
    chk("before the first refresh completes the payload is marked (current=false), never {}",
        unbuilt["refresh"]["current"] is False and unbuilt["refresh"]["reason"] == NOT_YET_BUILT
        and "leaderboard" not in unbuilt)
    publish(served, 1000, _built)
    publish(served, 1060, _unread)
    publish(served, 1120, _unread)
    kept = json.loads(served["payload"])
    chk("an unread listing keeps the last leaderboard and says so (current=false, reason, start of the outage)",
        kept["leaderboard"] == [{"id": "m1", "points": 7.0}] and kept["refresh"]["current"] is False
        and kept["refresh"]["failing_since"] == 1060 and kept["refresh"]["attempted_at"] == 1120
        and kept["refresh"]["snapshot_generated_at"] == 100 and "not measured" in kept["refresh"]["reason"])

    def _boom():
        raise OSError("connection refused by node.example:26657")

    publish(served, 1180, _boom)
    chk("an unexpected failure serves its type only, never the message (hosts, paths, commands)",
        json.loads(served["payload"])["refresh"]["reason"] == "refresh failed (OSError)")
    print("points-indexer selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if "--selftest" in sys.argv:  # Before the heavy imports (client may be absent in a bare test environment)
    raise SystemExit(_selftest())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import client as dc

W = {"work": float(os.environ.get("DENDRA_POINTS_W_WORK", "1.0")),
     "judge": float(os.environ.get("DENDRA_POINTS_W_JUDGE", "200")),
     "use": float(os.environ.get("DENDRA_POINTS_W_USE", "0.5"))}
CAP_BPS = int(os.environ.get("DENDRA_POINTS_CAP_BPS", "2000"))
# Group anti-Sybil caps -- cf. compute_points (i)/(ii).
OWNER_CAP_BPS = int(os.environ.get("DENDRA_POINTS_OWNER_CAP_BPS", "2000"))
USE_SHARE_MAX_BPS = int(os.environ.get("DENDRA_POINTS_USE_SHARE_MAX_BPS", "3000"))
REFRESH_S = float(os.environ.get("DENDRA_POINTS_REFRESH_S", "60"))
EXCLUDED = {a.strip() for a in os.environ.get("DENDRA_R2_EXCLUDE_ADDRS", "").split(",") if a.strip()}


def _fetch_verdicts(jobs, miners):
    """On-chain verdicts of resolved audited jobs: get-commit <jid>__verdict__<mid> (bounded best-effort)."""
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

    Without this fold the ranking carries one row per paying client, keyed by its bech32 address
    (`client:dendra1...`), together with its consumption volume — on an endpoint with no authentication,
    meant to be served through the public reverse proxy. Miners have a reason to be named: they
    are paid, they are drawn, they are slashable, and their track record is the whole point. A client
    has none: it pays and leaves. Publishing what each address consumes serves nothing the protocol
    needs, and it is the kind of record that cannot be withdrawn once read.

    Hashing the address is not enough: the set of on-chain addresses is public and enumerable, so
    anyone can hash all of them and match. Only folding removes the information.

    The fold happens on the way out. The per-client rows still exist inside compute_points, because the
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
    if jobs is None:
        # Not measured: `--serve` keeps the payload it last built, marks it (`publish`) and logs this;
        # `--once` fails with it. A leaderboard over an unread listing would erase every miner's work.
        raise Unmeasured("list-job unreadable or incomplete: no leaderboard is computed (not measured)")
    miners = dc.registered_miners()
    if miners is None:
        # Same rule as the job listing: without the registry, the operators are unknown, so the client points
        # (operators excluded) and the per-operator cap cannot be computed. Not measured, never a guess.
        raise Unmeasured("list-miner unreadable or incomplete: no leaderboard is computed (not measured)")
    verdicts = _fetch_verdicts(jobs, miners)
    rows, meta = compute_points(jobs, miners, verdicts, weights=W, cap_bps=CAP_BPS, excluded=EXCLUDED,
                                owner_cap_bps=OWNER_CAP_BPS, use_share_max_bps=USE_SHARE_MAX_BPS)
    return {
        "season": "0", "provisional": True, "generated_at": int(time.time()),
        "leaderboard": _fold_clients(rows), "meta": meta,
        "_provenance": {
            "claim": "Points derived from the current on-chain state (full stateless recomputation). Provisional.",
            "note": "No conversion is promised (Phase C is not enabled). A late vindication makes the honest "
                    "party's points reappear at the next recomputation (Layer B is intrinsic). No anti-Sybil "
                    "measure is perfect: the caps, the slash gate and the GPU/bond cost make farming more "
                    "expensive than its gain.",
        },
    }


# --serve: read-only HTTP facade (the_proof.py pattern). `t` is the last attempt, failed or not: a
# read that keeps failing is retried at the refresh pace, never on every request -- each attempt walks
# the whole job listing, and /points is public behind the reverse proxy.
CORS = os.environ.get("DENDRA_POINTS_CORS", "")
_CACHE = new_cache()
_REFRESH_LOCK = threading.Lock()


def _refresh():
    # One refresh at a time: a request arriving while one runs is served what is published, instead
    # of starting a second walk of the job listing in parallel.
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        now = time.time()
        if now - _CACHE["t"] < REFRESH_S:
            return
        _CACHE["t"] = now
        publish(_CACHE, now, snapshot)
    finally:
        _REFRESH_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if CORS:
            self.send_header("Access-Control-Allow-Origin", CORS)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/points", "/"):
            _refresh()
            self._send(200, _CACHE["payload"])
        elif self.path == "/health":
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *a):
        pass


def main():
    if "--once" in sys.argv:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        return
    host = os.environ.get("DENDRA_POINTS_HOST", "127.0.0.1")
    port = int(os.environ.get("DENDRA_POINTS_PORT", "8091"))
    print(f"[points] Season 0 indexer (provisional, dormant) on http://{host}:{port}/points "
          f"(weights {W}, cap {CAP_BPS} bps, refresh {REFRESH_S:g}s)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
