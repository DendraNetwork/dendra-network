#!/usr/bin/env python3
"""Stress-test MONTE-CARLO de la tokenomics v5 — sur le VRAI modele (`simulate_v5`).

POURQUOI CE FICHIER (audit 2026-06-10, constat TK-01) :
  L'ancien `_deprecated/spike_econ.py` testait la **v4** (inflationniste : `simulate_v4`,
  `infl 2 %`, offres a 196M-860M). Son verdict "[v4 ROBUSTE]" a ete recopie a tort dans
  `docs/TOKENOMICS_v5.md` / `ADR-021` comme "Monte-Carlo 4000 -> v5 ROBUSTE". OR `tokenomics_v5.py`
  est DETERMINISTE (une seule trajectoire) : v5 n'avait JAMAIS ete testee sous incertitude.
  Ce script repare ce trou : il TIRE les inconnues et mesure les invariants v5 sur N trajectoires.

CE QU'ON TIRE (sources d'incertitude reelles) :
  demande initiale, croissance (de la DECROISSANCE a forte hausse), prix du job, part mineur,
  taux de liberation Reserve, gate travail, part brulee.

CE QU'ON MESURE (par run, sur l'horizon) :
  supply_ok      offre <= 10 M chaque annee (plafond dur)
  reserve_ok     Reserve >= 0 ET monotone decroissante (jamais de mint)
  cliff_year     annee ou la Reserve securite se vide (<1% du genesis) -> securite = FRAIS SEULS
                 (ADR-029 ratifie : cap fixe / 0 mint, AUCUN plancher APR on-chain, pas d'auto-correction)
  apr_horizon    APR validateur a l'horizon (apres deplétion possible) : les frais soutiennent-ils la securite ?
  fee_funded     annee de bascule "finance par les frais" (ou jamais)
  deflation      offre finale < offre initiale

HONNETETE : ceci teste l'economie ON-MODEL. Il NE modelise PAS le farming du free-tier
(la Reserve paie le plein reward via `bob`, hors demand-gate — cf. audit PY-02/TK-09), ni le
grinding du defi de disponibilite (non specifie, TK-08), ni le vesting equipe (TK-09). Le verdict
ci-dessous porte donc sur la SOUTENABILITE MONETAIRE, pas sur la resistance a tous les abus.
"""
from __future__ import annotations

import argparse
import datetime
import random
import statistics
from tokenomics_v5 import V5Params, simulate_v5


def sample_params(rng: random.Random) -> V5Params:
    """Une trajectoire d'incertitude. Bornes larges et volontairement pessimistes en bas
    (job_growth < 1 = la demande RECULE) pour exercer les cas defavorables."""
    return V5Params(
        jobs_day_y1=int(rng.uniform(3_000, 120_000)),     # demande initiale tres incertaine
        job_growth=rng.uniform(0.85, 2.2),                # 0.85 = -15 %/an ... 2.2 = forte hausse (loin du plafond)
        jobs_day_max=int(rng.uniform(5_000_000, 200_000_000)),  # PLAFOND de marche incertain (demande saturante)
        avg_fee_dndr=rng.uniform(0.001, 0.02),            # prix d'un job (USD-cale, incertain)
        miner_fee_share=rng.uniform(0.75, 0.92),
        reserve_release_rate=rng.uniform(0.15, 0.30),     # vitesse de vidage de la Reserve
        work_gate_x=rng.uniform(1.0, 2.0),
        bme_burn_share=rng.uniform(0.02, 0.10),
    )


def run_one(p: V5Params, years: int) -> dict:
    rows = simulate_v5(p, years)
    res = [r["reserve_left"] for r in rows]
    sup = [r["supply"] for r in rows]
    # CLIFF de sécurité (ADR-029 : pas de plancher on-chain) : année où la Réserve passe sous 1% du genesis.
    cliff_thresh = (res[0] if res else 0) * 0.01
    cliff_year = next((r["year"] for r in rows if r["reserve_left"] <= cliff_thresh), None)
    return {
        "supply_ok": all(s <= p.max_supply for s in sup),
        "reserve_nonneg": all(x >= 0 for x in res),
        "reserve_monotone": all(res[i] >= res[i + 1] - 1 for i in range(len(res) - 1)),
        "fee_year": next((r["year"] for r in rows if r["fee_funded"]), None),
        "deflation": sup[-1] < sup[0],
        "reserve_left": res[-1],
        "supply_left": sup[-1],
        "min_apr": min(r["val_apr_pct"] for r in rows),
        "apr_horizon": rows[-1]["val_apr_pct"] if rows else 0.0,  # APR à l'horizon (après déplétion possible)
        "cliff_year": cliff_year,
    }


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def main():
    ap = argparse.ArgumentParser(description="Monte-Carlo tokenomics v5")
    ap.add_argument("-n", "--runs", type=int, default=4000)
    ap.add_argument("-y", "--years", type=int, default=30)  # T3.6 : 30 ans (10 ans = trop court, masquait le cliff de Réserve)
    ap.add_argument("-s", "--seed", type=int, default=12345)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    runs = [run_one(sample_params(rng), a.years) for _ in range(a.runs)]
    n = len(runs)
    supply_ok = sum(r["supply_ok"] for r in runs)
    reserve_ok = sum(r["reserve_nonneg"] and r["reserve_monotone"] for r in runs)
    defl = sum(r["deflation"] for r in runs)
    fee_ever = [r["fee_year"] for r in runs if r["fee_year"] is not None]
    res_left = [r["reserve_left"] for r in runs]
    min_aprs = [r["min_apr"] for r in runs]
    horizon_aprs = [r["apr_horizon"] for r in runs]
    cliffs = [r["cliff_year"] for r in runs if r["cliff_year"] is not None]

    stamp = datetime.date.today().isoformat()
    print(f"=== MONTE-CARLO TOKENOMICS v5 (simulate_v5) — {n} runs x {a.years} ans — seed {a.seed} — {stamp} ===\n")
    print(f"  offre <= 10 M (plafond dur) .............. {pct(supply_ok, n):6.1f}%  [{supply_ok}/{n}]")
    print(f"  Reserve >= 0 & decroissante ............. {pct(reserve_ok, n):6.1f}%  [{reserve_ok}/{n}]")
    print(f"  deflationniste (offre finale < genesis) . {pct(defl, n):6.1f}%  [{defl}/{n}]")
    print(f"  devient 'finance par les frais' un jour . {pct(len(fee_ever), n):6.1f}%  [{len(fee_ever)}/{n}]")
    if fee_ever:
        print(f"      -> annee de bascule : mediane {int(statistics.median(fee_ever))}, "
              f"min {min(fee_ever)}, max {max(fee_ever)}")
    # CLIFF de securite (ADR-029 : PAS de plancher on-chain) — quand la Reserve securite se vide
    print(f"  Reserve securite videe (<1% genesis) .... {pct(len(cliffs), n):6.1f}% des runs sur {a.years} ans")
    if cliffs:
        print(f"      -> annee du CLIFF : mediane {int(statistics.median(cliffs))}, "
              f"min {min(cliffs)}, max {max(cliffs)}  (apres : securite = FRAIS SEULS)")
    # NB : depuis la demande SATURANTE (plafond de marché), le HAUT de l'APR horizon est PHYSIQUE (réseau saturé au
    # plafond = beaucoup de fees vs bond borné = APR élevé mais fini), plus l'artefact de compounding géométrique.
    # Pour la SÉCURITÉ après le cliff, le chiffre pertinent reste le BAS de distribution (p05/min : demande faible).
    print(f"  APR validateur a l'HORIZON ({a.years} ans) ... p05 {sorted(horizon_aprs)[n//20]:.2f}% , "
          f"min {min(horizon_aprs):.2f}%  (bas = securite si demande faible ; mediane {statistics.median(horizon_aprs):.0f}% = reseau sature au plafond, eleve mais physique)")
    print(f"  APR validateur minimal (toute annee) .... mediane {statistics.median(min_aprs):.2f}% , "
          f"p05 {sorted(min_aprs)[n//20]:.2f}% , min {min(min_aprs):.2f}%")
    print(f"  Reserve restante a {a.years} ans ............. mediane {statistics.median(res_left)/1e6:.2f}M , "
          f"min {min(res_left)/1e6:.2f}M , max {max(res_left)/1e6:.2f}M")

    # --- VERDICT honnete (ADR-029 ratifie : cap fixe 10M / 0 mint, AUCUN plancher APR on-chain) ---
    hard = pct(supply_ok, n) >= 99.9 and pct(reserve_ok, n) >= 99.9
    cliff_share = pct(len(cliffs), n)
    horizon_p05 = sorted(horizon_aprs)[n // 20]
    print("\n>>> VERDICT (run ci-dessus) :")
    print(f"    Invariants DURS (offre<=10M, Reserve>=0/decroissante) : "
          f"{'TENUS 100%' if hard else 'VIOLES — investiguer'}")
    print(f"    Securite long terme (PAS de plancher, ADR-029) : la Reserve securite se vide dans "
          f"{cliff_share:.0f}% des runs sur {a.years} ans -> ensuite securite = FRAIS SEULS, SANS auto-correction.")
    print(f"    APR validateur a l'horizon, BAS de distribution (= test securite) : p05 {horizon_p05:.2f}% , "
          f"min {min(horizon_aprs):.2f}% -> en demande durablement faible apres le cliff, l'APR tombe ~0 et n'est PAS")
    print("    rattrape (cliff assume, ADR-029). Le haut de distribution (forte demande) n'est PAS un risque securite.")
    print("    NB : ne couvre pas free-tier/dispo/vesting (cf. en-tete). Mesure = soutenabilite MONETAIRE, pas anti-abus.")
    print("    CAVEAT dispo (internal audit 2026-06-26) : cout de disponibilite OPTIMISTE (avail_pass_rate=0.95 = PLACEHOLDER")
    print("    hors-modele) tant qu'ADR-022 n'est pas implemente -> SOUS-MODELISE le grinding. Le vrai taux d'echec +")
    print("    slash (AvailSlashBps...) sera modelise avec le plein ADR-022 (apres regen proto). Verdict securite (bas")
    print("    de distribution) inchange : le cout dispo ne le pilote pas.")


if __name__ == "__main__":
    main()
