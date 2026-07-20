"""Tests du simulateur de tokenomics."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokenomics import (TokenomicsParams, simulate, genesis_allocation, inflation_rate,
                        make_optimized, make_v3, simulate_v3, make_v4, simulate_v4)


def test_allocation_sums_to_100():
    p = TokenomicsParams()
    assert sum(p.alloc.values()) == 10000
    alloc = genesis_allocation(p)
    assert sum(alloc.values()) == p.genesis_supply  # arrondis exacts ici


def test_inflation_decays_to_tail():
    p = TokenomicsParams()
    assert inflation_rate(p, 0) == p.inflation_initial
    assert inflation_rate(p, 100) == p.inflation_tail        # plancher atteint
    # strictement decroissante jusqu'au tail
    assert inflation_rate(p, 1) < inflation_rate(p, 0)


def test_supply_grows_with_emission():
    p = TokenomicsParams()
    rows = simulate(p, years=5)
    supplies = [r["supply"] for r in rows]
    assert supplies == sorted(supplies)                      # croissante
    assert rows[0]["supply"] > p.genesis_supply


def test_apr_decreases_over_time_ceteris_paribus():
    # avec usage constant, l'APR baisse quand l'emission decroit
    p = TokenomicsParams(job_growth_per_year=1.0)
    rows = simulate(p, years=6)
    aprs = [r["staking_apr_pct"] for r in rows]
    assert aprs[0] > aprs[-1]


def test_no_burn_treasury_grows():
    p = TokenomicsParams()
    rows = simulate(p, years=5)
    treasuries = [r["treasury"] for r in rows]
    assert treasuries == sorted(treasuries)                  # tresorerie monotone (pas de burn)


def test_miner_gets_majority_of_fee():
    p = TokenomicsParams()
    r = simulate(p, years=1)[0]
    assert r["miner_revenue"] > 0.8 * r["fee_revenue"]       # ~85% au mineur


def test_training_rewards_funded_from_reserve():
    p = TokenomicsParams()
    rows = simulate(p, years=10)
    assert rows[0]["training_reward"] > 0                     # l'entrainement est paye
    # la reserve decroit (financement de l'entrainement)
    reserves = [r["reserve_remaining"] for r in rows]
    assert reserves == sorted(reserves, reverse=True)
    # total verse <= dotation de la Reserve (15%)
    total_train = sum(r["training_reward"] for r in rows)
    assert total_train <= p.genesis_supply * p.alloc["reserve_recompenses"] / 10000


def test_burn_option_reduces_treasury():
    base = simulate(TokenomicsParams(), years=5)[-1]
    with_burn = simulate(TokenomicsParams(fee_burn_bps=3000), years=5)[-1]
    assert with_burn["burned_cumul"] > 0                      # burn actif
    assert base["burned_cumul"] == 0                          # modele d'origine = pas de burn
    assert with_burn["treasury"] < base["treasury"]           # moins vers la tresorerie


def test_v1_default_emission_all_to_validators():
    # v1 (defaut) : emission 100% validateurs -> pas de subvention mineur
    r = simulate(TokenomicsParams(), years=1)[0]
    assert r["miner_total"] == r["miner_revenue"]
    assert r["node_reward"] == 0


def test_optimized_alloc_and_split_valid():
    p = make_optimized()
    assert sum(p.alloc.values()) == 10000
    assert abs(sum(p.emission_split.values()) - 1.0) < 1e-9
    assert p.alloc["equipe"] + p.alloc["investisseurs"] < 3300   # moins d'insiders que v1 (3300)


def test_optimized_subsidizes_miners_and_training():
    v1 = simulate(TokenomicsParams(), years=1)[0]
    v2 = simulate(make_optimized(), years=1)[0]
    # cote offre subventionne par l'emission
    assert v2["miner_total"] > v2["miner_revenue"]
    assert v2["miner_total"] > v1["miner_total"]
    # entrainement = Reserve + emission -> superieur a la v1 (Reserve seule)
    assert v2["training_reward"] > v1["training_reward"]
    # burn plus fort
    v1b = simulate(TokenomicsParams(), years=5)[-1]
    v2b = simulate(make_optimized(), years=5)[-1]
    assert v2b["burned_cumul"] > v1b["burned_cumul"]


# ---------------- v3 (corrige l'audit) ----------------

def test_v3_alloc_and_split_valid():
    p = make_v3()
    assert sum(p.alloc.values()) == 10000
    assert abs(sum(p.emission_split.values()) - 1.0) < 1e-9


def test_v3_demand_gate_caps_emission_when_low_demand():
    # demande tres faible -> la subvention 'work' est plafonnee tot
    p = make_v3()
    p.jobs_per_day_y1 = 100      # quasi pas de demande
    rows = simulate_v3(p, years=3)
    assert any(r["work_emission_capped"] for r in rows)


def test_v3_bme_burn_active():
    rows = simulate_v3(make_v3(), years=5)
    assert rows[-1]["burned_cumul"] > 0


def test_v3_miner_vesting_locks_part():
    rows = simulate_v3(make_v3(), years=3)
    r = rows[0]
    assert r["miner_circulating"] < r["miner_total"]      # une part est verrouillee
    assert rows[-1]["locked_cumul"] > 0


def test_v3_dynamic_inflation_grows_bonded():
    rows = simulate_v3(make_v3(), years=10)
    # le ratio bonde monte vers la cible au fil du temps
    assert rows[-1]["bonded_ratio_pct"] > rows[0]["bonded_ratio_pct"]


# ---------------- v4 (cycles deflationnistes) ----------------

def test_v4_burn_event_every_two_years():
    rows = simulate_v4(make_v4(), years=6)
    events = [r["year"] for r in rows if r["event"]]
    assert events == [2, 4, 6]                        # gros burn tous les 2 ans
    assert all(rows[y - 1]["event_burn"] > 0 for y in events)


def test_v4_scarcity_increases():
    rows = simulate_v4(make_v4(), years=6)
    assert rows[-1]["scarcity_x"] > rows[0]["scarcity_x"]      # rarete monte
    assert rows[-1]["supply"] < rows[0]["supply"] or rows[-1]["scarcity_x"] > 1.0


def test_v4_halving_reduces_inflation():
    rows = simulate_v4(make_v4(), years=6)
    # l'inflation est divisee ~par 2 a chaque cycle
    assert rows[0]["inflation_pct"] > rows[2]["inflation_pct"] > rows[4]["inflation_pct"]


def test_v4_real_apr_above_nominal():
    rows = simulate_v4(make_v4(), years=6)
    # le rendement reel (avec deflation) depasse l'APR nominal
    assert all(r["real_apr_pct"] >= r["staking_apr_pct"] for r in rows)
    assert rows[-1]["real_apr_pct"] > rows[-1]["staking_apr_pct"]


def test_v4_alloc_no_investors_team_regrouped():
    # allocation revisee : investisseurs supprimes, equipe+fondateurs regroupes a 5%
    p = make_v4()
    assert sum(p.alloc.values()) == 10000
    assert "investisseurs" not in p.alloc
    assert p.alloc["equipe_fondation"] == 500          # 5%
    assert p.team_fee_share_bps == 2000                # equipe payee aussi via les frais


def test_v4_gate_on_nonrecoverable_raised():
    # ADR-017 : gate remonte a 1,5x (sur demande non-recuperable, < break-even 2,0)
    assert make_v4().demand_gate_multiple == 1.5


def test_v4_team_fee_accrues_from_fees():
    rows = simulate_v4(make_v4(), years=6)
    assert rows[-1]["team_fee_cumul"] > 0              # flux equipe alimente par les frais
