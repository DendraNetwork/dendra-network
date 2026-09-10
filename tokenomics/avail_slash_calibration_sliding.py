#!/usr/bin/env python3
"""ADR-022 SLIDING-WINDOW calibration — recalibrates k/W for the sliding bitmask that REPLACES the
tumbling window in runAvailabilitySlash (availability.go).

WHY RECALIBRATE: the sliding window evaluates the quorum at EVERY epoch, over all window positions,
where the tumbling window evaluated a single fixed window per period W. At equal (k, W) the HONEST
false-positive rate therefore RISES, by roughly W/k times more evaluation points. In exchange, reset
gaming disappears — a chronic offender can no longer place k-1 absences before every boundary, nor
2(k-1) consecutive ones straddling a boundary — and the continuous outage survived stays k-1 epochs
with no timing to exploit.

METHOD (two crossed routes plus an adversary):
  (1) ANALYTIC (scan statistic, first passage): rate per epoch ~= p·C(W-1,k-1)·p^(k-1)·(1-p)^(W-k),
      the current failure completing a k-cluster -> expected false slashes per year = rate x epochs/year.
  (2) MONTE CARLO over the keeper's EXACT algorithm (64-bit bitmask, window mask, reset after a slash),
      with a fixed seed, deterministic, validating the analytic route where events are observable
      (99 % and 95 % uptime).
  (3) CHRONIC ADVERSARY: the optimal pattern (k-1 consecutive absences per period W, i.e. density
      (k-1)/W) must NEVER slash, verified by replaying the exact algorithm. The chronic tolerance stays
      (k-1)/W but becomes EXACT: no 2(k-1) burst, no boundary timing.

RECOMMENDATION (the script's default): k=5, W=25 — with a 300-block epoch (~5 min):
  chronic tolerance 16 %, outage survived 20 min (against 15 min for a tumbling k=4), honest bleed at
  99 % uptime about 0.5 %/yr at a 5 % slash, which is better than the tumbling k=4 figure of ~1.1 %/yr.
"""
import json, math, os, random, sys

BLOCK_TIME_S = float(os.environ.get("BLOCK_TIME_S", "1.0"))
SECONDS_YEAR = 365.0 * 24 * 3600
AVAIL_EPOCH_BLOCKS = int(os.environ.get("AVAIL_EPOCH_BLOCKS", "300"))
AVAIL_DEADLINE_BLOCKS = int(os.environ.get("AVAIL_DEADLINE_BLOCKS", "150"))
AVAIL_FAIL_K = int(os.environ.get("AVAIL_FAIL_K", "5"))
AVAIL_FAIL_WINDOW = int(os.environ.get("AVAIL_FAIL_WINDOW", "25"))
AVAIL_SLASH_BPS = int(os.environ.get("AVAIL_SLASH_BPS", "500"))
AVAIL_SLASH_MAX = int(os.environ.get("AVAIL_SLASH_MAX", "0"))
MC_YEARS = int(os.environ.get("MC_YEARS", "400"))

EPOCH_S = AVAIL_EPOCH_BLOCKS * BLOCK_TIME_S
EPY = SECONDS_YEAR / EPOCH_S  # epochs per year (~105120 at 5 min)

UPTIMES = [0.9999, 0.999, 0.99, 0.95]


def fp_rate_per_epoch(k, w, p):
    """First-passage approximation: P(failure at e AND k-1 failures in the preceding W-1 epochs)."""
    if k < 1 or w < k:
        return 0.0
    return p * math.comb(w - 1, k - 1) * p ** (k - 1) * (1 - p) ** (w - k)


def annual_analytic(k, w, p):
    return EPY * fp_rate_per_epoch(k, w, p)


def simulate_keeper(k, w, p, years, seed=20260701):
    """Replays the keeper's EXACT algorithm: mask <<= 1; |= 1 if absent; &= window; popcount >= k -> slash + reset."""
    rng = random.Random(seed)
    n = int(years * EPY)
    winmask = (1 << w) - 1 if w < 64 else (1 << 64) - 1
    mask, slashes = 0, 0
    for _ in range(n):
        mask = (mask << 1)
        if rng.random() < p:
            mask |= 1
        mask &= winmask
        if bin(mask).count("1") >= k:
            slashes += 1
            mask = 0
    return slashes / years


def chronic_adversary_never_slashed(k, w):
    """The optimal chronic pattern (k-1 consecutive absences per period W), replayed on the exact algorithm."""
    winmask = (1 << w) - 1
    mask = 0
    for e in range(w * 400):
        mask = (mask << 1) & winmask
        if e % w < (k - 1):
            mask |= 1
        if bin(mask).count("1") >= k:
            return False
    return True


def burst_survived_min(k):
    return (k - 1) * EPOCH_S / 60.0


def analyze(k, w):
    rows = []
    for up in UPTIMES:
        p = 1 - up
        ana = annual_analytic(k, w, p)
        mc = simulate_keeper(k, w, p, MC_YEARS) if ana > 0.005 else None  # Monte Carlo is useful only when events are observable
        rows.append({
            "uptime": up,
            "p_miss_per_epoch": round(p, 5),
            "false_slashes_per_year_analytic": round(ana, 5),
            "false_slashes_per_year_montecarlo": (round(mc, 4) if mc is not None else None),
        })
    return {
        "k": k, "window": w,
        "chronic_absence_tolerated_exact": round((k - 1) / w, 4),
        "burst_outage_survived_min": round(burst_survived_min(k), 1),
        "chronic_adversary_never_slashed": chronic_adversary_never_slashed(k, w),
        "honest_false_positive": rows,
    }


def main():
    art = os.environ.get("ART", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "..", "mode-a", "bench-results",
                                             "avail-slash-calibration-sliding.json"))
    grid = []
    for w in (15, 20, 25, 30, 40, 60):
        for k in (3, 4, 5, 6, 7):
            if k >= w or w > 64:
                continue
            grid.append({
                "k": k, "window": w,
                "chronic_tol": round((k - 1) / w, 4),
                "burst_survived_min": round(burst_survived_min(k), 1),
                "false_slashes_per_year_at_99": round(annual_analytic(k, w, 0.01), 4),
                "false_slashes_per_year_at_99_9": round(annual_analytic(k, w, 0.001), 6),
            })

    rec = analyze(AVAIL_FAIL_K, AVAIL_FAIL_WINDOW)

    # Guard rails, at the same bars as the validated tumbling window: a hobbyist at ~99 % uptime stays
    # protected, and the chronic tolerance stays at or below 20 %.
    hobby = next(r for r in rec["honest_false_positive"] if r["uptime"] == 0.99)
    hobby_bleed_pct = hobby["false_slashes_per_year_analytic"] * (AVAIL_SLASH_BPS / 10000) * 100
    mc99 = hobby["false_slashes_per_year_montecarlo"]
    ana99 = hobby["false_slashes_per_year_analytic"]
    mc_consistent = (mc99 is None) or (ana99 == 0) or (0.25 <= (mc99 / ana99 if ana99 else 1) <= 4.0)
    ok_fp = hobby_bleed_pct < 5.0
    ok_chronic = rec["chronic_absence_tolerated_exact"] <= 0.20
    ok_adv = rec["chronic_adversary_never_slashed"]

    out = {
        "context": {
            "mode": "SLIDING-WINDOW, 64-bit bitmask (replaces the tumbling window)",
            "block_time_s": BLOCK_TIME_S,
            "avail_epoch_blocks": AVAIL_EPOCH_BLOCKS,
            "epoch_minutes": round(EPOCH_S / 60.0, 2),
            "mc_years": MC_YEARS,
        },
        "recommended_genesis_values": {
            "avail_epoch_blocks": AVAIL_EPOCH_BLOCKS,
            "avail_deadline_blocks": AVAIL_DEADLINE_BLOCKS,
            "avail_fail_k": AVAIL_FAIL_K,
            "avail_fail_window": AVAIL_FAIL_WINDOW,
            "avail_slash_bps": AVAIL_SLASH_BPS,
            "avail_slash_max": AVAIL_SLASH_MAX,
        },
        "recommended_analysis": rec,
        "guards": {
            "hobbyist_99pct_annual_bleed_pct": round(hobby_bleed_pct, 2),
            "hobbyist_bleed_under_5pct_per_year": ok_fp,
            "chronic_tolerance_max_20pct": ok_chronic,
            "chronic_adversary_never_slashed": ok_adv,
            "montecarlo_consistent_with_analytic": mc_consistent,
            "PASS": ok_fp and ok_chronic and ok_adv and mc_consistent,
        },
        "vs_tumbling_k4_w20": {
            "tumbling_bleed_99pct_per_year_pct": 1.1,
            "note": "sliding k=5/W=25 protects the honest miner BETTER (lower bleed), survives 20 min "
                    "instead of 15, and removes reset gaming: the 2(k-1) burst and boundary timing "
                    "both become impossible.",
        },
        "grid": grid,
        "notes": [
            "The sliding chronic tolerance (k-1)/W is EXACT in the sense of 'never slashed' (the optimal pattern "
            "replayed on the keeper's algorithm); any higher density ENDS UP slashed whatever its distribution. "
            "Stated honestly: the deterrence is ECONOMIC, not absolute — an adversary willing to ABSORB recurring "
            "slashes (5 % of stake per cycle plus absence never being paid) can sustain more; the burn is what "
            "bounds it.",
            "The sliding false-positive rate is evaluated at every epoch, so at equal (k,W) it is higher than the "
            "tumbling one; k=5/W=25 brings it BELOW tumbling k=4/W=20 while tightening the chronic case.",
            "W <= 64, the bitmask capacity, bounded by Params.Validate when the mechanism is armed.",
            "A LIVE PROOF is still required: at soft launch, cut an honest miner for fewer than k epochs and observe "
            "zero slash; measure the real p_miss before arming DENDRA_AVAIL_SLASH=1.",
            "These values are pending validation: the validated k=4/W=20 pair was validated for the TUMBLING window, "
            "and the algorithm has changed.",
        ],
    }

    os.makedirs(os.path.dirname(art), exist_ok=True)
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("=" * 64)
    print(" ADR-022 AVAILABILITY SLASH CALIBRATION — SLIDING WINDOW")
    print("=" * 64)
    print(f"  epoch={AVAIL_EPOCH_BLOCKS} blocks (~{out['context']['epoch_minutes']} min) | "
          f"k={AVAIL_FAIL_K} / W={AVAIL_FAIL_WINDOW} | slash={AVAIL_SLASH_BPS}bps")
    print(f"  exact chronic tolerance = {(AVAIL_FAIL_K-1)/AVAIL_FAIL_WINDOW*100:.1f}% | "
          f"outage survived = {burst_survived_min(AVAIL_FAIL_K):.0f} min | "
          f"chronic adversary never slashed = {ok_adv}")
    for r in rec["honest_false_positive"]:
        mc = r["false_slashes_per_year_montecarlo"]
        print(f"  uptime {r['uptime']*100:6.2f}% -> analytic {r['false_slashes_per_year_analytic']:.5f}/yr"
              + (f" | MC {mc:.4f}/yr" if mc is not None else ""))
    print(f"  GUARD RAILS: bleed@99%={hobby_bleed_pct:.2f}%/yr (<5%={ok_fp}) ; chronic<=20%={ok_chronic} ; "
          f"MC~analytic={mc_consistent} ; PASS={out['guards']['PASS']}")
    print(f"  artifact: {art}")
    print("=" * 64)
    return 0 if out["guards"]["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
