#!/usr/bin/env python3
"""Calibration ADR-022 SLIDING-WINDOW (lot scaling 2026-07-01) — recalibre k/W pour le bitmask glissant
qui REMPLACE le tumbling dans runAvailabilitySlash (availability.go).

POURQUOI RECALIBRER : le sliding évalue le quorum à CHAQUE époque (toutes les positions de fenêtre), là où
le tumbling n'évaluait qu'une fenêtre fixe par période W -> à (k, W) égaux, le faux-positif HONNÊTE MONTE
(~W/k fois plus de points d'évaluation). En échange : le gaming du reset disparaît (un chronique ne peut plus
caler (k-1) absences avant chaque frontière ni 2(k-1) consécutives à cheval), et la coupure continue survécue
reste k-1 époques SANS timing possible.

MÉTHODE (2 voies croisées) :
  (1) ANALYTIQUE (scan statistic, premier passage) : taux/époque ≈ p·C(W-1,k-1)·p^(k-1)·(1-p)^(W-k)
      (l'échec courant complète un k-cluster) -> espérance de faux-slash/an = taux × époques/an.
  (2) MONTE-CARLO de l'ALGO EXACT du keeper (bitmask 64 bits, masque fenêtre, reset après slash) — graine fixe,
      déterministe, valide l'analytique là où les événements sont observables (99 % / 95 % uptime).
  (3) ADVERSAIRE CHRONIQUE : le pattern optimal (k-1 absences consécutives par période W = densité (k-1)/W)
      ne doit JAMAIS slasher (vérifié en rejouant l'algo exact) — la tolérance chronique reste (k-1)/W,
      mais devient EXACTE (aucun burst 2(k-1), aucun timing de frontière).

RECOMMANDATION (défaut du script) : k=5, W=25 — à époque 300 blocs (~5 min) :
  chronique 16 % (≈ le 15 % validé internal audit au tumbling), coupure survécue 20 min (> 15 min tumbling k=4),
  bleed honnête @99 % uptime ≈ 0,5 %/an à slash 5 % (MEILLEUR que le tumbling k=4 ≈ 1,1 %/an).
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
EPY = SECONDS_YEAR / EPOCH_S  # époques/an (~105120 à 5 min)

UPTIMES = [0.9999, 0.999, 0.99, 0.95]


def fp_rate_per_epoch(k, w, p):
    """Approx premier-passage : P(échec à e ET k-1 échecs dans les W-1 époques précédentes)."""
    if k < 1 or w < k:
        return 0.0
    return p * math.comb(w - 1, k - 1) * p ** (k - 1) * (1 - p) ** (w - k)


def annual_analytic(k, w, p):
    return EPY * fp_rate_per_epoch(k, w, p)


def simulate_keeper(k, w, p, years, seed=20260701):
    """Rejoue l'ALGO EXACT du keeper : mask<<=1 ; |=1 si absent ; &= fenêtre ; popcount>=k -> slash + reset."""
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
    """Pattern chronique optimal (k-1 absences consécutives par période W) rejoué sur l'algo exact."""
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
        mc = simulate_keeper(k, w, p, MC_YEARS) if ana > 0.005 else None  # MC utile si événements observables
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

    # garde-fous (mêmes barres que le tumbling validé internal audit : hobbyiste ~99 % protégé, chronique ≤ 20 %)
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
            "mode": "SLIDING-WINDOW bitmask 64 bits (remplace tumbling ; lot scaling 2026-07-01)",
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
            "chronic_tolerance_le_20pct": ok_chronic,
            "chronic_adversary_never_slashed": ok_adv,
            "montecarlo_consistent_with_analytic": mc_consistent,
            "PASS": ok_fp and ok_chronic and ok_adv and mc_consistent,
        },
        "vs_tumbling_k4_w20": {
            "tumbling_bleed_99pct_per_year_pct": 1.1,
            "note": "le sliding k=5/W=25 protège MIEUX l'honnête (bleed moindre), survit 20 min (vs 15), "
                    "et supprime le gaming du reset (burst 2(k-1) et timing de frontière impossibles).",
        },
        "grid": grid,
        "notes": [
            "Tolérance chronique sliding = (k-1)/W EXACTE au sens « jamais slashé » (pattern optimal rejoué sur l'algo "
            "du keeper) ; toute densité supérieure FINIT slashée quelle que soit sa distribution. HONNÊTETÉ (red-team "
            "2026-07-02) : la dissuasion est ÉCONOMIQUE, pas absolue — un adversaire qui ENCAISSE des slashes "
            "récurrents (5 % du stake par cycle + absence jamais payée) peut soutenir plus ; c'est le burn qui borne.",
            "Le FP sliding évalue chaque époque -> à (k,W) égaux il est plus haut que le tumbling ; k=5/W=25 le ramène "
            "SOUS le tumbling k=4/W=20 tout en durcissant le chronique.",
            "W ≤ 64 (capacité bitmask) — borné par Params.Validate à l'armement.",
            "PREUVE LIVE requise inchangée (internal audit) : au soft-launch, couper un honnête < k époques -> 0 slash ; "
            "mesurer p_miss réel avant d'armer DENDRA_AVAIL_SLASH=1.",
            "VALEURS PENDING VALIDATION internal audit (le k=4/W=20 validé l'était pour le TUMBLING ; l'algo a changé).",
        ],
    }

    os.makedirs(os.path.dirname(art), exist_ok=True)
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("=" * 64)
    print(" CALIBRATION SLASH DISPO ADR-022 — SLIDING-WINDOW")
    print("=" * 64)
    print(f"  époque={AVAIL_EPOCH_BLOCKS} blocs (~{out['context']['epoch_minutes']} min) | "
          f"k={AVAIL_FAIL_K} / W={AVAIL_FAIL_WINDOW} | slash={AVAIL_SLASH_BPS}bps")
    print(f"  chronique EXACTE = {(AVAIL_FAIL_K-1)/AVAIL_FAIL_WINDOW*100:.1f}% | "
          f"coupure survécue = {burst_survived_min(AVAIL_FAIL_K):.0f} min | "
          f"adversaire chronique jamais slashé = {ok_adv}")
    for r in rec["honest_false_positive"]:
        mc = r["false_slashes_per_year_montecarlo"]
        print(f"  uptime {r['uptime']*100:6.2f}% -> analytique {r['false_slashes_per_year_analytic']:.5f}/an"
              + (f" | MC {mc:.4f}/an" if mc is not None else ""))
    print(f"  GARDE-FOUS : bleed@99%={hobby_bleed_pct:.2f}%/an (<5%={ok_fp}) ; chronique<=20%={ok_chronic} ; "
          f"MC~analytique={mc_consistent} ; PASS={out['guards']['PASS']}")
    print(f"  artefact : {art}")
    print("=" * 64)
    return 0 if out["guards"]["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
