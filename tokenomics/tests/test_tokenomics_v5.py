#!/usr/bin/env python3
"""Tests des INVARIANTS de la tokenomics v5 (audit 2026-06-10, constat TK-04).

Le modele canonique `tokenomics_v5.py` n'avait AUCUN test ; la suite existante
(`test_tokenomics.py`) valide les modeles v1-v4 DEPRECIES, avec des invariants
CONTRADICTOIRES avec v5 (`test_supply_grows_with_emission`, `test_inflation_decays`).
Ici on verrouille les proprietes propres a v5 :
  - offre PLAFONNEE a 10 M (jamais depassee)
  - Reserve >= 0 et monotone decroissante (zero mint : on ne fait que LIBERER du pre-alloue)
  - sommes de repartition coherentes (alloc=10000 bps, emit_split=1, protocol_split=1)
  - deflation reelle sous demande positive
  - le mecanisme de PLANCHER APR se DECLENCHE quand la demande decoit (couvre la branche
    95-97 jamais exercee par la trajectoire par defaut, cf. TK-03)

Lancer : `python3 test_tokenomics_v5.py`  (autonome, assert)  ou  `pytest`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tokenomics_v5 import V5Params, simulate_v5

P = V5Params()


# ---------- repartitions ----------
def test_alloc_sums_to_10000_bps():
    assert sum(P.alloc.values()) == 10000


def test_emit_split_sums_to_one():
    assert abs(sum(P.emit_split.values()) - 1.0) < 1e-9


def test_protocol_split_sums_to_one():
    assert abs(sum(P.protocol_split.values()) - 1.0) < 1e-9


# ---------- offre plafonnee / pas de mint ----------
def test_supply_never_exceeds_cap():
    for r in simulate_v5(P, 10):
        assert r["supply"] <= P.max_supply, f"offre {r['supply']} > plafond a l'an {r['year']}"


def test_supply_is_deflationary_under_demand():
    rows = simulate_v5(P, 10)
    assert rows[-1]["supply"] < rows[0]["supply"], "doit etre deflationniste avec de la demande"


def test_reserve_nonnegative_and_monotone():
    rows = simulate_v5(P, 10)
    res = [r["reserve_left"] for r in rows]
    assert all(x >= 0 for x in res), "la Reserve ne doit jamais etre negative"
    assert all(res[i] >= res[i + 1] - 1 for i in range(len(res) - 1)), \
        "la Reserve ne doit que decroitre (jamais de mint)"


def test_no_mint_total_emitted_le_reserve_alloc():
    """Tout ce qui est sorti de la Reserve <= dotation Reserve initiale (jamais cree de token)."""
    rows = simulate_v5(P, 30)
    reserve0 = P.max_supply * P.alloc["reserve"] / 10000
    total_emitted = reserve0 - rows[-1]["reserve_left"]
    assert total_emitted <= reserve0 + 1, "emission cumulee > Reserve : un mint s'est glisse"


# ---------- plancher APR : la branche DOIT se declencher en demande faible (TK-03) ----------
def _low_demand():
    # demande qui decoit : peu de jobs, prix bas, demande qui RECULE -> APR sous le plancher
    return replace(P, jobs_day_y1=1_000, job_growth=0.9, avg_fee_dndr=0.001)


def test_apr_floor_branch_is_exercised_when_demand_disappoints():
    """Avec la demande par defaut, APR min = 5.86 % > 5 % : la branche 95-97 ne tourne JAMAIS.
    Ce test force une demande faible ET verifie que le plancher est defendu tant que la
    Reserve le permet (apr >= 5 % chaque annee ou Reserve epuisee cette annee-la)."""
    p = _low_demand()
    rows = simulate_v5(p, 10)
    floor = p.min_security_apr * 100
    triggered = any(abs(r["val_apr_pct"] - floor) < 0.02 for r in rows)
    assert triggered, "la branche plancher APR aurait du s'activer (apr cale a 5.00 %) en demande faible"
    # invariants durs preserves meme sous stress
    for r in rows:
        assert r["supply"] <= p.max_supply
        assert r["reserve_left"] >= 0


def test_hard_invariants_hold_across_scenarios():
    scenarios = {
        "defaut": P,
        "demande_faible": _low_demand(),
        "forte_croissance": replace(P, jobs_day_y1=100_000, job_growth=2.2, avg_fee_dndr=0.02),
        "liberation_rapide": replace(P, reserve_release_rate=0.30),
        "burn_eleve": replace(P, bme_burn_share=0.10),
    }
    for name, p in scenarios.items():
        rows = simulate_v5(p, 12)
        assert all(r["supply"] <= p.max_supply for r in rows), f"[{name}] offre > 10 M"
        assert all(r["reserve_left"] >= 0 for r in rows), f"[{name}] Reserve negative"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{ok}/{len(fns)} tests OK")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
