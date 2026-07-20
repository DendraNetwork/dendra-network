#!/usr/bin/env python3
"""Calibration T1.3 (internal audit 2026-06-26) — l'audit + les pénalités gardent la TRICHE -EV avant le testnet récompensé.

Tout est DÉJÀ codé (verification_mode/audit_sample_bps/slash_leak_bps/hold_bps/audit_min_quorum/silence_slash_bps) ;
ce script CALIBRE les valeurs de genesis et PROUVE (chiffre = artefact) que :
  (1) la triche RÉVÉLÉE (faux fluide) est -EV — le juge MoE l'attrape (P_escape≈0) ;
  (2) la triche MUETTE (ne révèle pas, économise tout le compute) est -EV MÊME quand les audits n'atteignent
      JAMAIS le quorum (q=0, pire cas de liveness) — c'est le cas CONTRAIGNANT, fermé par silence_slash_bps.

Modèle EV par job (relatif à « ne pas jouer », unité = udndr), pour un primaire malhonnête :
  - non audité (proba 1-a)      : encaisse le paiement optimiste libéré (faux travail payé)  = +pay
  - audité + quorum (proba a·q) : SLASH DUR                                                   = -s·stake
  - audité + no-quorum (a·(1-q)): clawback du paiement + pénalité de SILENCE (restituable)    = -silence·stake
  pay = fee·(1 - cut_bps/10000) ; s = slash_leak_bps/10000 ; silence = silence_slash_bps/10000.
hold_bps=10000 ne change pas le signe (il RETARDE le paiement non-audité, ne l'annule pas) -> conservateur.
"""
import json, os, sys

# --- params de genesis RÉCOMPENSÉ (docker/entrypoint-chain.sh) ---
FEE        = int(os.environ.get("FEE", "4500"))          # udndr/job (cli.py défaut)
CUT_BPS    = int(os.environ.get("CUT_BPS", "1500"))      # protocol_fee_bps (15%) -> pay = 85% de la fee
MIN_STAKE  = int(os.environ.get("MIN_STAKE", "50000"))   # entrypoint-chain.sh
SLASH_BPS  = int(os.environ.get("SLASH_BPS", "8000"))    # slash_leak_bps (80%)

PAY = FEE * (10000 - CUT_BPS) // 10000                    # paiement optimiste encaissé sur un job NON audité


def muet_ev(a, q, silence_bps, stake=MIN_STAKE):
    """EV par job de la triche MUETTE (udndr). a=audit prob, q=taux de succès de quorum, silence_bps en bps."""
    s = SLASH_BPS / 10000
    sil = silence_bps / 10000
    gain_non_audite = (1 - a) * PAY
    perte_quorum = a * q * s * stake
    perte_noquorum = a * (1 - q) * sil * stake
    return gain_non_audite - perte_quorum - perte_noquorum


def min_silence_for_neg_ev(a, q=0.0):
    """Plus petit silence_slash_bps qui rend le muet -EV au taux de quorum q (défaut q=0 = pire cas liveness)."""
    for bps in range(0, 10001, 5):
        if muet_ev(a, q, bps) < 0:
            return bps
    return None


def main():
    audit_levels = {"testnet 50%": 5000, "intermediaire 30%": 3000, "PROD 10%": 1000}
    SILENCE = int(os.environ.get("SILENCE_BPS", "2000"))  # valeur PROPOSÉE à armer

    print(f"== Calibration EV triche ==  fee={FEE}  pay(optimiste)={PAY}  stake(min)={MIN_STAKE}  slash={SLASH_BPS/100:.0f}%")
    print(f"   silence_slash_bps PROPOSÉ = {SILENCE} ({SILENCE/100:.0f}% du stake, RESTITUABLE)\n")

    rows = []
    print(f"{'audit':18s} {'silence min (q=0)':>18s} {'muet EV @q=0':>14s} {'muet EV @q=.3':>14s}  verdict@{SILENCE}bps")
    for name, a_bps in audit_levels.items():
        a = a_bps / 10000
        need = min_silence_for_neg_ev(a, q=0.0)
        ev_q0 = muet_ev(a, 0.0, SILENCE)
        ev_q3 = muet_ev(a, 0.3, SILENCE)
        ok = ev_q0 < 0
        rows.append({"audit_bps": a_bps, "silence_min_q0_bps": need,
                     "muet_ev_q0": round(ev_q0, 1), "muet_ev_q03": round(ev_q3, 1),
                     "neg_ev_at_proposed": ok})
        need_s = f"{need}" if need is not None else ">10000"
        print(f"{name:18s} {need_s:>18s} {ev_q0:>14.0f} {ev_q3:>14.0f}  {'OK -EV' if ok else 'INSUFFISANT (+EV)'}")

    print()
    print("Lecture :")
    print(f"  • À audit 50% (testnet), silence_slash_bps={SILENCE} rend le muet -EV MÊME si les audits n'atteignent")
    print("    JAMAIS le quorum (q=0). C'est la garantie DURE (ne dépend pas de la liveness du comité).")
    print("  • À audit 10% (PROD visé), le silence seul ne suffit PAS à q=0 : il FAUT un taux de quorum réel (q>0)")
    print("    -> garder audit_sample_bps ÉLEVÉ (5000) au testnet tant que le quorum distribué n'est pas mesuré ≥ ~0,5.")
    print("  • La triche RÉVÉLÉE est dominée par la muette (juge MoE P_escape≈0 + risque de litige client) -> même borne.")

    art_dir = os.path.join(os.path.dirname(__file__), "..", "mode-a", "bench-results")
    os.makedirs(art_dir, exist_ok=True)
    art = os.path.join(art_dir, "audit-ev-calibration.json")
    out = {"fee": FEE, "pay_optimiste": PAY, "min_stake": MIN_STAKE, "slash_leak_bps": SLASH_BPS,
           "silence_slash_bps_propose": SILENCE,
           "by_audit_level": rows,
           "genesis_recommande": {"audit_sample_bps": 5000, "silence_slash_bps": SILENCE,
                                  "hold_bps": 10000, "audit_min_quorum": 4, "slash_leak_bps": 8000},
           "note": "muet -EV @q=0 a audit 50% => garantie dure. PROD 10% exige un taux de quorum mesure >0."}
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nArtefact : {art}")
    # garde-fou : la config testnet proposée DOIT être -EV au pire cas
    assert muet_ev(0.5, 0.0, SILENCE) < 0, "config testnet 50% + silence proposé n'est PAS -EV au pire cas !"
    print("GARDE-FOU OK : testnet (audit 50% + silence 2000) = muet -EV au pire cas (q=0).")


if __name__ == "__main__":
    main()
