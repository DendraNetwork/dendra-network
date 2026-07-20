#!/usr/bin/env python3
"""Tokenomics DENDRA v5 — modele NATIF (pense pour un reseau d'inference utile, PAS pour Cosmos).

Idee directrice (ce qui change vs un PoS Cosmos classique) :
  * AUCUNE inflation perpetuelle, AUCUN mint. Offre PLAFONNEE a 10 000 000 DNDR.
  * Les recompenses sortent d'une RESERVE PRE-ALLOUEE (33 % du genesis) qui se VIDE selon une
    courbe DECROISSANTE -> on ne cree jamais de token, on LIBERE du pre-alloue.
    => offre toujours <= 10 M ; les BURN la font passer SOUS le genesis (deflationniste reel).
  * 3 flux de recompense, tous verifies/bornes (anti-farming) :
      - TRAVAIL   (par job, demande-gated : subvention <= 1.5x la demande non-recuperable)
      - DISPONIBILITE (defi aleatoire toutes les 4h, verifie ; borne par le pool ; slash si echec)
      - SECURITE  (validateurs : budget Reserve DECROISSANT -> bascule vers les FRAIS quand l'usage monte)
  * Le DEV touche 5 % au genesis + une part GOUVERNABLE du revenu d'INFERENCE (pas une taxe sur chaque tx).
  * Quand la Reserve est epuisee -> economie 100 % financee par les FRAIS (securite incluse). Plus d'emission.

Tout est illustratif/gouvernable. L'effet "prix" suppose une demande reelle (non garantie).
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class V5Params:
    ticker: str = "DNDR"
    max_supply: int = 10_000_000                 # PLAFOND DUR (jamais depasse)
    alloc: dict = field(default_factory=lambda: {  # genesis (bps) -- somme 10000
        "communaute": 3500, "reserve": 3300, "tresorerie": 2700, "equipe": 500})
    # --- emission depuis la RESERVE (decroissante, bornee, NON inflationniste) ---
    reserve_release_rate: float = 0.22           # liberation annuelle = 22 % de la Reserve RESTANTE (decay)
    emit_split: dict = field(default_factory=lambda: {  # repartition de la liberation Reserve
        "work": 0.50, "availability": 0.20, "security": 0.30})
    # --- TRAVAIL (par job) ---
    jobs_day_y1: int = 50_000
    job_growth: float = 1.8                        # croissance ANNUELLE quand on est loin du plafond (geometrique tot)
    jobs_day_max: int = 50_000_000                 # PLAFOND de marche : demande SATURANTE (logistique) — au-dela, la
    #   croissance retombe vers 1 (anti-compounding non-physique a 30 ans ; internal audit 2026-06-26). Borne le HAUT, ne
    #   touche PAS le bas (declin g<1 preserve) -> le verdict securite (pilote par la demande faible) est inchange.
    avg_fee_dndr: float = 0.005                   # prix moyen d'un job (le user paie)
    miner_fee_share: float = 0.85                 # 85 % au mineur, 15 % protocole
    work_gate_x: float = 1.5                      # subvention travail <= 1.5x la demande NON-RECUPERABLE
    # --- DISPONIBILITE (defi 4h) : bornee par le pool ; pas de farming au-dela ---
    avail_pass_rate: float = 0.95                 # taux de reussite moyen au defi (le reste = slash, hors modele)
    # --- PROTOCOLE (15 % du job) ---
    protocol_split: dict = field(default_factory=lambda: {  # du cut protocole (15 %)
        "validators": 0.50, "dev": 0.20, "treasury": 0.30})
    # --- SECURITE / staking ---
    target_bonded: float = 0.40                   # cible bondee (securite = fees + Reserve, pas inflation)
    min_security_apr: float = 0.0                 # T1.4/ADR-029 (ratifie) : AUCUN plancher APR on-chain (cap fixe 10M
    #   / 0 mint, pas de tail). Securite = Reserve (~>=20 ans) PUIS frais seuls, SANS auto-correction. Defaut 0 =
    #   realite on-chain (pas de ponction-plancher simulee). >0 = knob de SCENARIO hypothetique, NON implemente.
    # --- DEFLATION ---
    bme_burn_share: float = 0.05                  # 5 % des frais brules (BME) -> deflation DOUCE :
    #   DNDR est un token de PAIEMENT (forte velocite) -> on veut qu'il reste LIQUIDE/depensable,
    #   pas un actif thesaurise. 5 % => offre ~8,1 M a 10 ans, rarete ~x1,23 (apprecie sans s'emballer).
    #   (30 % ecrasait l'offre a 2,3 M = hyper-deflation -> thesaurisation -> mort du marche d'inference.)


def simulate_v5(p: V5Params, years: int = 10) -> list[dict]:
    supply = float(p.max_supply)                         # ne fait que DESCENDRE (burns)
    reserve = p.max_supply * p.alloc["reserve"] / 10000  # 3,3 M : source unique d'emission
    treasury = p.max_supply * p.alloc["tresorerie"] / 10000
    circ = supply - reserve - treasury                   # circulant initial (communaute + equipe)
    bonded_ratio = 0.20
    burned_cum = 0.0
    dev_cum = 0.0
    jobs_day = float(p.jobs_day_y1)
    rows = []
    for y in range(years):
        # 1) LIBERATION Reserve (decroissante) -> 3 flux
        release = reserve * p.reserve_release_rate
        em_work_pool = release * p.emit_split["work"]
        em_avail = release * p.emit_split["availability"]
        em_sec = release * p.emit_split["security"]

        # 2) FRAIS d'usage (revenu reel, paye par les users -> recircule, ne cree rien)
        # REALISME : le prix d'un job est cale en USD. Si le token s'apprecie (rarete^), il faut
        # MOINS de DNDR par job -> les frais en DNDR sont amortis par la rarete (anti-emballement).
        scarcity_now = p.max_supply / supply if supply else 1.0
        eff_fee = p.avg_fee_dndr / max(1.0, scarcity_now)
        fee_rev = jobs_day * 365 * eff_fee
        miner_fee = fee_rev * p.miner_fee_share
        protocol = fee_rev - miner_fee
        val_fee = protocol * p.protocol_split["validators"]
        dev_fee = protocol * p.protocol_split["dev"]
        treas_fee = protocol * p.protocol_split["treasury"]
        # demande NON-RECUPERABLE (ce qu'un mineur ne peut pas se rembourser) = burn + treasury + dev
        bme_burn = fee_rev * p.bme_burn_share
        nonrec_demand = bme_burn + treas_fee + dev_fee

        # 3) TRAVAIL : subvention demande-gated (l'inutilise RESTE en Reserve -> anti-farming + duree)
        work_sub = min(em_work_pool, p.work_gate_x * nonrec_demand)
        released = work_sub + em_avail + em_sec          # ce qui sort vraiment de la Reserve cette annee
        reserve -= released
        circ += released                                  # la Reserve liberee devient circulante

        # 4) SECURITE / APR (plancher)
        bonded = supply * bonded_ratio
        sec_income = em_sec + val_fee
        apr = sec_income / bonded if bonded else 0.0
        if p.min_security_apr > 0 and bonded and apr < p.min_security_apr:
            extra = min(p.min_security_apr * bonded - sec_income, reserve)  # borne par la Reserve
            reserve -= extra; circ += extra; sec_income += extra; apr = sec_income / bonded
        # le stake monte si l'APR est attractif (>6 %)
        bonded_ratio = max(0.15, min(0.70, bonded_ratio + (0.04 if apr > 0.06 else -0.03)))

        # 5) BURN (BME) + treasury
        bme_burn = min(bme_burn, supply * 0.5)
        supply -= bme_burn; circ -= bme_burn
        burned_cum += bme_burn
        treasury += treas_fee
        dev_cum += dev_fee

        # 6) revenus mineurs (frais + subvention travail + disponibilite)
        miner_total = miner_fee + work_sub + em_avail
        scarcity = p.max_supply / supply if supply else 0.0
        defl = bme_burn / (supply + bme_burn) if supply else 0.0
        rows.append({
            "year": y + 1,
            "supply": round(supply),
            "reserve_left": round(reserve),
            "scarcity_x": round(scarcity, 3),
            "miner_income": round(miner_total),
            "work_subsidy": round(work_sub),
            "avail_reward": round(em_avail),
            "val_apr_pct": round(apr * 100, 2),
            "real_apr_pct": round((apr + defl) * 100, 2),
            "bonded_pct": round(bonded_ratio * 100, 1),
            "fee_rev": round(fee_rev),
            "burned_cumul": round(burned_cum),
            "dev_fee_cumul": round(dev_cum),
            "treasury": round(treasury),
            "fee_funded": fee_rev > released,   # l'usage finance-t-il plus que l'emission Reserve ?
        })
        # demande SATURANTE (logistique vers le plafond de marche) : jobs_day << K -> ~geometrique (job_growth) ;
        # jobs_day -> K -> croissance -> 1 (sature) ; job_growth<1 (declin) PRESERVE (le bas de distribution, qui
        # pilote le verdict securite, ne change pas). Remplace le geometrique pur, non-physique a 30 ans.
        K = float(p.jobs_day_max)
        jobs_day = max(0.0, jobs_day * (1 + (p.job_growth - 1) * (1 - jobs_day / K)))
        jobs_day = min(jobs_day, K)
    return rows


def make_v5() -> V5Params:
    return V5Params()


def _fmt(x):
    return f"{x/1e6:.2f}M" if abs(x) >= 1e6 else (f"{x/1e3:.0f}k" if abs(x) >= 1e3 else f"{x:.3f}")


def main():
    p = make_v5()
    rows = simulate_v5(p, 10)
    print("=== TOKENOMICS DENDRA v5 — offre FIXE 10 M, emission depuis la Reserve (zero inflation) ===\n")
    print(f"{'An':>2} {'Offre':>8} {'Reserve':>8} {'Rarete':>7} {'Mineur/an':>10} {'APR':>6} {'APRr':>6} {'Bonde':>6} {'Frais':>8} {'Burn∑':>8} {'FeeFin?':>7}")
    print("-" * 92)
    for r in rows:
        print(f"{r['year']:>2} {_fmt(r['supply']):>8} {_fmt(r['reserve_left']):>8} {r['scarcity_x']:>6}x "
              f"{_fmt(r['miner_income']):>10} {r['val_apr_pct']:>5}% {r['real_apr_pct']:>5}% {r['bonded_pct']:>5}% "
              f"{_fmt(r['fee_rev']):>8} {_fmt(r['burned_cumul']):>8} {'oui' if r['fee_funded'] else 'non':>7}")
    last = rows[-1]
    dep = next((r["year"] for r in rows if r["reserve_left"] < p.max_supply * 0.0033 * 100 * 0.05), None)
    print(f"\n>>> 10 ans : offre {_fmt(rows[0]['supply'])} -> {_fmt(last['supply'])} (rarete x{last['scarcity_x']}), "
          f"Reserve restante {_fmt(last['reserve_left'])}, dev cumule {_fmt(last['dev_fee_cumul'])} DNDR")
    print(f">>> Bascule 'finance par les frais' : annee {next((r['year'] for r in rows if r['fee_funded']), '—')}")
    print(f">>> Offre TOUJOURS <= 10 M : {'OK' if all(r['supply'] <= p.max_supply for r in rows) else 'ECHEC'} ; "
          f"deflationniste : {'OK' if last['supply'] < rows[0]['supply'] else 'non'}")


if __name__ == "__main__":
    main()
