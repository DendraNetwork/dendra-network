#!/usr/bin/env python3
"""Modele de cout HONNETE : $/M tokens de SORTIE, Dendra vs incumbents (reponse audit A->Z, P1).

NE modelise PAS le tarif on-chain en udndr (pas de prix de marche -> non significatif). Modelise le COUT REEL
DE RESSOURCE (electricite + amortissement GPU) pour produire un token de sortie, x la redondance k=3, vs le
prix catalogue d'un appel API incumbent. C'est la comparaison economiquement pertinente que l'audit demande.

Tout est transparent + ajustable (hypotheses en bas). Aucune donnee proprietaire ; prix incumbents = juin 2026
(cf docs/COUT-HONNETE.md pour les sources datees).
"""
from dataclasses import dataclass

SEC_PER_YEAR = 365 * 24 * 3600


@dataclass
class GPU:
    name: str
    tok_per_s: float   # debit generation Llama 3.1 8B q4 (tokens de SORTIE/s)
    watts: float       # consommation sous charge
    price_usd: float   # prix d'achat (amorti sur la vie utile)


def single_miner_usd_per_mtok(g: GPU, elec_usd_kwh: float, amort_years: float, util: float) -> dict:
    """Cout d'UN mineur pour 1 M tokens de sortie : electricite + amortissement."""
    elec = (g.watts / 1000 * elec_usd_kwh) / (g.tok_per_s * 3600)          # $/token
    life_tokens = amort_years * SEC_PER_YEAR * util * g.tok_per_s          # tokens sur la vie utile
    amort = g.price_usd / life_tokens                                      # $/token
    return {"elec": elec * 1e6, "amort": amort * 1e6, "total": (elec + amort) * 1e6}


def dendra_usd_per_mtok(single_total: float, k: int, miner_share: float, miner_margin: float) -> float:
    """Prix client : k mineurs CALCULENT (redondance) -> cout reseau = k x single ; + marge mineur ;
    + part protocole (le mineur touche miner_share de la fee -> le client paie /miner_share)."""
    net = single_total * k * (1 + miner_margin)
    return net / miner_share


# Prix incumbents -- $/M tokens de SORTIE (juin 2026 ; sources dans docs/COUT-HONNETE.md)
INCUMBENTS = {
    "Groq Llama 3.1 8B": 0.08,
    "Together Llama 3.1 8B": 0.18,
    "OpenAI GPT-4o-mini": 0.60,
}


def main():
    elec, amort_years, util = 0.20, 3.0, 0.40
    k, miner_share, miner_margin = 3, 0.85, 0.20
    gpus = [GPU("RTX 4090", 120, 400, 1800), GPU("RTX 3070", 45, 200, 500)]
    print(f"Hypotheses : elec ${elec}/kWh | amortissement {amort_years} ans @ {util:.0%} utilisation | "
          f"k={k} (redondance) | part mineur {miner_share:.0%} | marge mineur {miner_margin:.0%}\n")
    worst, best = 0.0, 1e9
    for g in gpus:
        c = single_miner_usd_per_mtok(g, elec, amort_years, util)
        d = dendra_usd_per_mtok(c["total"], k, miner_share, miner_margin)
        print(f"{g.name:9s}: 1 mineur ${c['total']:.2f}/M (elec ${c['elec']:.2f} + amort ${c['amort']:.2f})"
              f"  ->  Dendra ${d:.2f}/M sortie")
        for name, p in INCUMBENTS.items():
            mult = d / p
            worst, best = max(worst, mult), min(best, mult)
            print(f"           vs {name:22s} ${p:.2f}/M  ->  {mult:4.0f}x plus cher")
        print()
    print(f"=> Dendra est structurellement {best:.0f}x a {worst:.0f}x plus cher au token de sortie.")
    print("   Conclusion : le wedge NE PEUT PAS etre le prix. Cf docs/COUT-HONNETE.md (implication strategique).")


if __name__ == "__main__":
    main()
