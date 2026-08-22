#!/usr/bin/env python3
"""ADR-022 activation gate — the availability LIVENESS slash must NEVER false-slash an honest miner, and a
tumbling window must tolerate only a LOW chronic absence rate.

The keeper code is sound and dormant. This script CALIBRATES the genesis VALUES (avail_deadline_blocks,
avail_fail_k, avail_fail_window, avail_slash_bps, avail_slash_max) and produces a numeric artefact proving:

  (1) NO FALSE POSITIVES: an HONEST miner at the target uptime is essentially never slashed — the expected
      number of false slashes per year is negligible, and a TRANSIENT outage (< k epochs) is always survived.
  (2) BOUNDED CHRONIC TOLERANCE: a tumbling window lets a cheater stay absent for at most (k-1)/window of
      the epochs indefinitely (fail k-1 times, wait for the reset, repeat), so k and window are chosen to
      keep that fraction LOW.

Mechanics: at every epoch boundary, a bonded miner ABSENT from Available[E-1] — no VRF proof in time —
counts one failure; avail_fail_k or more failures within a TUMBLING window of avail_fail_window epochs
trigger a slash of min(stake*avail_slash_bps/10000, avail_slash_max), BURNED, and the counter resets.

FUNDAMENTAL TENSION: for any honest miss rate p_miss > 0 in continuous operation, a "k within W" threshold
eventually bites at the horizon. The slash is therefore SAFE only for HIGH-uptime miners with a GENEROUS
deadline (which drives p_miss down). A tumbling window additionally admits the "(k-1)/window" gaming above.
A sliding window (exact, 64-bit bitmask) is the hardening to apply if the live measurement of p_miss
requires it.
"""
import json, math, os, sys

# ---- network context (testnet) ----
BLOCK_TIME_S = float(os.environ.get("BLOCK_TIME_S", "1.0"))
SECONDS_YEAR = 365.0 * 24 * 3600

# ---- RECOMMENDED genesis configuration ----
AVAIL_EPOCH_BLOCKS    = int(os.environ.get("AVAIL_EPOCH_BLOCKS", "300"))   # 5 min per epoch at 1 s/block (matches emission epoch_blocks)
AVAIL_DEADLINE_BLOCKS = int(os.environ.get("AVAIL_DEADLINE_BLOCKS", "150"))# 2.5 min to answer = HALF an epoch (generous)
AVAIL_FAIL_K          = int(os.environ.get("AVAIL_FAIL_K", "4"))   # k=4 protects the CONSUMER-GPU miner at ~99 % uptime; a 99.9 % datacenter bar is rejected
AVAIL_FAIL_WINDOW     = int(os.environ.get("AVAIL_FAIL_WINDOW", "20"))
AVAIL_SLASH_BPS       = int(os.environ.get("AVAIL_SLASH_BPS", "500"))      # 5 %: liveness is not cheating -> lenient, deterrent without ruin
AVAIL_SLASH_MAX       = int(os.environ.get("AVAIL_SLASH_MAX", "0"))        # 0 = bounded by bps alone; >0 = absolute cap in udndr

EPOCH_S = AVAIL_EPOCH_BLOCKS * BLOCK_TIME_S


def binom_sf(k, n, p):
    """P(X >= k) for X ~ Binomial(n, p) (exact sum, stdlib only)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def fp_window(k, w, p_miss):
    """Probability that an HONEST miner (miss rate per epoch = p_miss) reaches k or more failures within a
    window of w epochs."""
    return binom_sf(k, w, p_miss)


def annual_expected_slashes(k, w, p_miss):
    """Expected false slashes per year (tumbling: at most one slash per window, then reset)."""
    windows_per_year = SECONDS_YEAR / (w * EPOCH_S)
    return windows_per_year * fp_window(k, w, p_miss)


def chronic_tolerance(k, w):
    """Absence fraction a cheater can sustain INDEFINITELY without a slash (gaming the tumbling reset)."""
    return (k - 1) / w


def transient_survived_min(k):
    """CONTINUOUS outage, in minutes, that is always survived: k-1 consecutive epochs with no other miss
    in the window."""
    return (k - 1) * EPOCH_S / 60.0


UPTIMES = [0.9999, 0.999, 0.99, 0.95]  # p_miss = 1 - uptime (with a generous deadline, miss ~= downtime per epoch)


def report(k, w):
    rows = []
    for up in UPTIMES:
        p = 1 - up
        rows.append({
            "uptime": up,
            "p_miss_per_epoch": round(p, 5),
            "fp_per_window": fp_window(k, w, p),
            "expected_false_slashes_per_year": round(annual_expected_slashes(k, w, p), 4),
        })
    return {
        "k": k, "window": w,
        "chronic_absence_tolerated": round(chronic_tolerance(k, w), 4),
        "transient_outage_survived_min": round(transient_survived_min(k), 1),
        "honest_false_positive": rows,
    }


def main():
    art = os.environ.get("ART", os.path.join(os.path.dirname(__file__),
                                             "..", "mode-a", "bench-results", "avail-slash-calibration.json"))
    # 1) (k, window) grid — look for chronic tolerance <= 10 % AND ~0 false slashes/year at the target uptime
    grid = []
    for w in (10, 12, 16, 20, 24, 30, 40):
        for k in (2, 3, 4, 5):
            if k >= w:
                continue
            esl_999 = annual_expected_slashes(k, w, 0.001)   # target uptime 99.9 %
            grid.append({
                "k": k, "window": w,
                "chronic_tol": round(chronic_tolerance(k, w), 4),
                "false_slashes_per_year_at_99_9": round(esl_999, 4),
                "transient_survived_min": round(transient_survived_min(k), 1),
            })

    # 2) RECOMMENDATION = the retained configuration (script defaults), detailed at every uptime
    rec = report(AVAIL_FAIL_K, AVAIL_FAIL_WINDOW)

    # Internal guard rail: protect the CONSUMER-GPU miner at ~99 % uptime. An honest miner's annual bleed
    # (false slashes per year times slash size) must stay LOW, and chronic tolerance must stay bounded.
    hobby = next(r for r in rec["honest_false_positive"] if r["uptime"] == 0.99)
    hobby_bleed_pct = hobby["expected_false_slashes_per_year"] * (AVAIL_SLASH_BPS / 10000) * 100
    ok_fp = hobby_bleed_pct < 5.0          # under 5 %/year of bleed for an honest miner at 99 %
    ok_chronic = rec["chronic_absence_tolerated"] <= 0.20

    out = {
        "context": {
            "block_time_s": BLOCK_TIME_S,
            "avail_epoch_blocks": AVAIL_EPOCH_BLOCKS,
            "epoch_minutes": round(EPOCH_S / 60.0, 2),
        },
        "recommended_genesis_values": {
            "avail_epoch_blocks": AVAIL_EPOCH_BLOCKS,
            "avail_deadline_blocks": AVAIL_DEADLINE_BLOCKS,
            "avail_deadline_minutes": round(AVAIL_DEADLINE_BLOCKS * BLOCK_TIME_S / 60.0, 2),
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
            "PASS": ok_fp and ok_chronic,
        },
        "design_uptime_bar": "~99% (CONSUMER GPU: k=4 protects the hobbyist, honest bleed under 5%/year at 99% uptime). "
                             "99.9% (datacenter) is NOT required. The real p_miss must be CONFIRMED at soft launch before arming.",
        "grid": grid,
        "notes": [
            "A GENEROUS deadline (half an epoch) means a miner only misses when it is genuinely DOWN for more "
            "than 2.5 min; honest p_miss is approximately downtime per epoch.",
            "Chronic tolerance = (k-1)/window under a tumbling window. A 64-bit sliding-window bitmask is the "
            "exact hardening if the live p_miss measurement justifies it; it removes the reset gaming.",
            "avail_slash_bps is lenient (5%): liveness is NOT cheating, and a residual false slash costs 5%, not "
            "ruin. Proceeds are BURNED, so no beneficiary has an incentive to over-slash.",
            "LIVE PROOF required: at soft launch, arm these values, take an honest miner offline briefly "
            "(fewer than k epochs), verify that NO slash fires, and measure the real p_miss.",
        ],
    }

    os.makedirs(os.path.dirname(art), exist_ok=True)
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("=" * 64)
    print(" ADR-022 AVAILABILITY SLASH CALIBRATION")
    print("=" * 64)
    rg = out["recommended_genesis_values"]
    print(f"  epoch={AVAIL_EPOCH_BLOCKS} blocks (~{out['context']['epoch_minutes']} min) | "
          f"deadline={rg['avail_deadline_blocks']} blocks (~{rg['avail_deadline_minutes']} min, generous)")
    print(f"  k={rg['avail_fail_k']} / window={rg['avail_fail_window']}  | "
          f"slash={rg['avail_slash_bps']}bps (max={rg['avail_slash_max']})")
    print(f"  -> chronic absence tolerated = {rec['chronic_absence_tolerated']*100:.1f}%  | "
          f"transient outage survived = {rec['transient_outage_survived_min']:.0f} min")
    print("  HONEST false slashes per year:")
    for r in rec["honest_false_positive"]:
        print(f"     uptime {r['uptime']*100:6.2f}%  (p_miss={r['p_miss_per_epoch']}) -> "
              f"{r['expected_false_slashes_per_year']:.4f} slash/year")
    print(f"  GUARD RAIL: honest bleed at 99% = {hobby_bleed_pct:.2f}%/year (<5% = {ok_fp}) ; "
          f"chronic {rec['chronic_absence_tolerated']*100:.0f}% (<=20% = {ok_chronic}) ; PASS = {out['guards']['PASS']}")
    print(f"  artefact: {art}")
    print("=" * 64)
    return 0 if out["guards"]["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
