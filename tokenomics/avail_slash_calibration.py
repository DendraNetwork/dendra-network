#!/usr/bin/env python3
"""Calibration ADR-022 v1 (gate d'ACTIVATION internal audit 2026-06-27) — le slash de LIVENESS de disponibilité ne
faux-slashe JAMAIS un honnête, et la fenêtre tumbling ne tolère qu'une FAIBLE absence chronique.

Le code keeper est validé (SAIN, dormant). Ce script CALIBRE les VALEURS de genesis (avail_deadline_blocks,
avail_fail_k, avail_fail_window, avail_slash_bps, avail_slash_max) et PROUVE (chiffre = artefact) :

  (1) ANTI-FAUX-POSITIF : un mineur HONNÊTE (uptime cible) n'est ~jamais slashé — l'espérance de faux-slash/an est
      négligeable, et une coupure TRANSITOIRE (< k époques) est toujours survécue.
  (2) TOLÉRANCE CHRONIQUE bornée : la fenêtre tumbling laisse un tricheur s'absenter au plus (k-1)/window des
      époques indéfiniment (échouer k-1, attendre le reset, recommencer) -> on choisit k/window pour la garder BASSE.

Mécanique (rappel) : à chaque frontière d'époque, un mineur bondé ABSENT de Available[E-1] (= pas de preuve VRF à
temps) compte 1 échec ; >= avail_fail_k échecs dans une fenêtre TUMBLING de avail_fail_window époques -> slash
min(stake*avail_slash_bps/10000, avail_slash_max), BRÛLÉ, compteur reset.

TENSION FONDAMENTALE (honnête) : pour un taux d'absence honnête p_miss > 0 en opération continue, tout seuil
"k dans W" finit par mordre à l'horizon. Le slash n'est donc SÛR que pour des mineurs à HAUT uptime + une échéance
GÉNÉREUSE (p_miss -> bas). La fenêtre tumbling a EN PLUS le gaming "(k-1)/window" (limite connue, cf. internal audit #2).
Le sliding-window (bitmask 64 bits, exact) est le durcissement si la mesure live de p_miss l'exige.
"""
import json, math, os, sys

# ---- contexte réseau (testnet) ----
BLOCK_TIME_S = float(os.environ.get("BLOCK_TIME_S", "1.0"))
SECONDS_YEAR = 365.0 * 24 * 3600

# ---- config genesis RECOMMANDÉE (à valider par internal audit) ----
AVAIL_EPOCH_BLOCKS    = int(os.environ.get("AVAIL_EPOCH_BLOCKS", "300"))   # 5 min/époque @1s (= emission epoch_blocks)
AVAIL_DEADLINE_BLOCKS = int(os.environ.get("AVAIL_DEADLINE_BLOCKS", "150"))# 2,5 min pour répondre = MOITIÉ d'époque (généreux)
AVAIL_FAIL_K          = int(os.environ.get("AVAIL_FAIL_K", "4"))   # internal audit 2026-06-27 : 3->4 = protège le mineur GRAND-PUBLIC ~99% uptime (datacenter 99,9% rejeté)
AVAIL_FAIL_WINDOW     = int(os.environ.get("AVAIL_FAIL_WINDOW", "20"))
AVAIL_SLASH_BPS       = int(os.environ.get("AVAIL_SLASH_BPS", "500"))      # 5 % : liveness != triche -> lénient, dissuasif sans détruire
AVAIL_SLASH_MAX       = int(os.environ.get("AVAIL_SLASH_MAX", "0"))        # 0 = borné par bps seul ; >0 = plafond absolu udndr

EPOCH_S = AVAIL_EPOCH_BLOCKS * BLOCK_TIME_S


def binom_sf(k, n, p):
    """P(X >= k) pour X ~ Binomiale(n, p) (somme exacte, stdlib)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def fp_window(k, w, p_miss):
    """Proba qu'un HONNÊTE (miss/époque = p_miss) atteigne >= k échecs dans une fenêtre de w époques."""
    return binom_sf(k, w, p_miss)


def annual_expected_slashes(k, w, p_miss):
    """Espérance de faux-slash/an (tumbling : au plus 1 slash par fenêtre, puis reset)."""
    windows_per_year = SECONDS_YEAR / (w * EPOCH_S)
    return windows_per_year * fp_window(k, w, p_miss)


def chronic_tolerance(k, w):
    """Fraction d'absence qu'un tricheur peut maintenir INDÉFINIMENT sans slash (gaming du reset tumbling)."""
    return (k - 1) / w


def transient_survived_min(k):
    """Coupure CONTINUE (en minutes) toujours survécue (k-1 époques consécutives, sans autre miss dans la fenêtre)."""
    return (k - 1) * EPOCH_S / 60.0


UPTIMES = [0.9999, 0.999, 0.99, 0.95]  # p_miss = 1 - uptime (échéance généreuse -> miss ~= downtime/époque)


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
    # 1) grille de (k, window) — on cherche chronique <= 10% ET ~0 faux-slash/an à l'uptime CIBLE (99,9%)
    grid = []
    for w in (10, 12, 16, 20, 24, 30, 40):
        for k in (2, 3, 4, 5):
            if k >= w:
                continue
            esl_999 = annual_expected_slashes(k, w, 0.001)   # uptime cible 99,9%
            grid.append({
                "k": k, "window": w,
                "chronic_tol": round(chronic_tolerance(k, w), 4),
                "false_slashes_per_year_at_99_9": round(esl_999, 4),
                "transient_survived_min": round(transient_survived_min(k), 1),
            })

    # 2) RECOMMANDATION = la config retenue (valeurs par défaut du script), détaillée à tous les uptimes
    rec = report(AVAIL_FAIL_K, AVAIL_FAIL_WINDOW)

    # garde-fou interne (internal audit 2026-06-27) : protéger le mineur GRAND-PUBLIC ~99 % uptime -> le BLEED annuel d'un
    # honnête (faux-slash/an × taille du slash) doit rester FAIBLE ; chronique tolérée bornée (v1 <= 20 %).
    hobby = next(r for r in rec["honest_false_positive"] if r["uptime"] == 0.99)
    hobby_bleed_pct = hobby["expected_false_slashes_per_year"] * (AVAIL_SLASH_BPS / 10000) * 100
    ok_fp = hobby_bleed_pct < 5.0          # < 5 %/an de bleed pour un honnête à 99 %
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
            "chronic_tolerance_le_20pct": ok_chronic,
            "PASS": ok_fp and ok_chronic,
        },
        "design_uptime_bar": "~99% (GPU GRAND-PUBLIC : k=4 protège le hobbyiste, bleed honnête <5%/an à 99% uptime). "
                             "99,9% (datacenter) N'EST PAS exigé. p_miss réel à CONFIRMER au soft-launch avant d'armer.",
        "grid": grid,
        "notes": [
            "Échéance GÉNÉREUSE (moitié d'époque) -> un mineur ne rate que s'il est réellement DOWN > 2,5 min ; "
            "p_miss honnête ~= downtime/époque.",
            "Tolérance chronique = (k-1)/window : tumbling. Bitmask sliding-window 64 bits = durcissement exact si "
            "la mesure live de p_miss le justifie (élimine le gaming du reset).",
            "avail_slash_bps lénient (5%) : la liveness n'est PAS de la triche ; un faux-slash résiduel coûte 5%, "
            "pas une ruine. BRÛLÉ (pas de bénéficiaire -> pas d'incitation à sur-slasher).",
            "PREUVE LIVE requise (internal audit) : au soft-launch, armer ces valeurs, couper un mineur honnête briévement "
            "(< k époques) -> vérifier AUCUN slash ; mesurer p_miss réel.",
        ],
    }

    os.makedirs(os.path.dirname(art), exist_ok=True)
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("=" * 64)
    print(" CALIBRATION SLASH DISPO ADR-022 v1")
    print("=" * 64)
    rg = out["recommended_genesis_values"]
    print(f"  époque={AVAIL_EPOCH_BLOCKS} blocs (~{out['context']['epoch_minutes']} min) | "
          f"échéance={rg['avail_deadline_blocks']} blocs (~{rg['avail_deadline_minutes']} min, généreuse)")
    print(f"  k={rg['avail_fail_k']} / window={rg['avail_fail_window']}  | "
          f"slash={rg['avail_slash_bps']}bps (max={rg['avail_slash_max']})")
    print(f"  -> absence chronique tolérée = {rec['chronic_absence_tolerated']*100:.1f}%  | "
          f"coupure transitoire survécue = {rec['transient_outage_survived_min']:.0f} min")
    print("  faux-slash HONNÊTE / an :")
    for r in rec["honest_false_positive"]:
        print(f"     uptime {r['uptime']*100:6.2f}%  (p_miss={r['p_miss_per_epoch']}) -> "
              f"{r['expected_false_slashes_per_year']:.4f} slash/an")
    print(f"  GARDE-FOU : bleed honnête @99% = {hobby_bleed_pct:.2f}%/an (<5% = {ok_fp}) ; "
          f"chronique {rec['chronic_absence_tolerated']*100:.0f}% (<=20% = {ok_chronic}) ; PASS = {out['guards']['PASS']}")
    print(f"  artefact : {art}")
    print("=" * 64)
    return 0 if out["guards"]["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
