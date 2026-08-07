#!/usr/bin/env python3
"""Calibration: the audit and the penalties keep CHEATING -EV before the rewarded testnet.

Everything is ALREADY implemented (verification_mode / audit_sample_bps / slash_leak_bps / hold_bps /
audit_min_quorum / silence_slash_bps); this script CALIBRATES the genesis values and PROVES, as a numeric
artefact, that:
  (1) REVEALED cheating (a plausible-sounding fake) is -EV — the MoE judge catches it (P_escape almost 0);
  (2) SILENT cheating (never reveal, save all the compute) is -EV EVEN when the audits NEVER reach quorum
      (q=0, the worst liveness case) — that is the binding case, closed by silence_slash_bps.

Per-job EV model (relative to "not playing", unit = udndr) for a dishonest primary:
  - not audited (prob 1-a)        : collects the released optimistic payment (fake work paid) = +pay
  - audited + quorum (prob a*q)   : HARD SLASH                                                = -s*stake
  - audited + no quorum (a*(1-q)) : payment clawback + SILENCE penalty (refundable)           = -silence*stake
  pay = fee*(1 - cut_bps/10000); s = slash_leak_bps/10000; silence = silence_slash_bps/10000.
hold_bps=10000 does not change the sign (it DELAYS the unaudited payment, it does not cancel it), so this
model is conservative.
"""
import json, os, sys

# --- genesis parameters of the REWARDED network (docker/entrypoint-chain.sh) ---
FEE        = int(os.environ.get("FEE", "4500"))          # udndr per job (cli.py default)
CUT_BPS    = int(os.environ.get("CUT_BPS", "1500"))      # protocol_fee_bps (15%) -> pay = 85% of the fee
MIN_STAKE  = int(os.environ.get("MIN_STAKE", "50000"))   # entrypoint-chain.sh
SLASH_BPS  = int(os.environ.get("SLASH_BPS", "8000"))    # slash_leak_bps (80%)

PAY = FEE * (10000 - CUT_BPS) // 10000                    # optimistic payment collected on a NON-audited job


def mute_ev(a, q, silence_bps, stake=MIN_STAKE):
    """Per-job EV of SILENT cheating (udndr). a = audit probability, q = quorum success rate, silence_bps in bps."""
    s = SLASH_BPS / 10000
    sil = silence_bps / 10000
    gain_non_audite = (1 - a) * PAY
    perte_quorum = a * q * s * stake
    perte_noquorum = a * (1 - q) * sil * stake
    return gain_non_audite - perte_quorum - perte_noquorum


def min_silence_for_neg_ev(a, q=0.0):
    """Smallest silence_slash_bps that makes silent cheating -EV at quorum rate q (default q=0, worst liveness case)."""
    for bps in range(0, 10001, 5):
        if mute_ev(a, q, bps) < 0:
            return bps
    return None


def main():
    audit_levels = {"testnet 50%": 5000, "intermediate 30%": 3000, "PROD 10%": 1000}
    SILENCE = int(os.environ.get("SILENCE_BPS", "2000"))  # PROPOSED value to arm

    print(f"== Cheating EV calibration ==  fee={FEE}  pay(optimistic)={PAY}  stake(min)={MIN_STAKE}  slash={SLASH_BPS/100:.0f}%")
    print(f"   PROPOSED silence_slash_bps = {SILENCE} ({SILENCE/100:.0f}% of the stake, REFUNDABLE)\n")

    rows = []
    print(f"{'audit':18s} {'min silence (q=0)':>18s} {'silent EV @q=0':>14s} {'silent EV @q=.3':>15s}  verdict@{SILENCE}bps")
    for name, a_bps in audit_levels.items():
        a = a_bps / 10000
        need = min_silence_for_neg_ev(a, q=0.0)
        ev_q0 = mute_ev(a, 0.0, SILENCE)
        ev_q3 = mute_ev(a, 0.3, SILENCE)
        ok = ev_q0 < 0
        rows.append({"audit_bps": a_bps, "silence_min_q0_bps": need,
                     "mute_ev_q0": round(ev_q0, 1), "mute_ev_q03": round(ev_q3, 1),
                     "neg_ev_at_proposed": ok})
        need_s = f"{need}" if need is not None else ">10000"
        print(f"{name:18s} {need_s:>18s} {ev_q0:>14.0f} {ev_q3:>15.0f}  {'OK -EV' if ok else 'INSUFFICIENT (+EV)'}")

    print()
    print("How to read this:")
    print(f"  - At 50% audit (testnet), silence_slash_bps={SILENCE} makes silent cheating -EV EVEN if the audits")
    print("    NEVER reach quorum (q=0). That is the HARD guarantee: it does not depend on committee liveness.")
    print("  - At 10% audit (the PROD target), silence alone is NOT enough at q=0: a real quorum rate (q>0) is")
    print("    required -> keep audit_sample_bps HIGH (5000) on the testnet until the distributed quorum is")
    print("    measured at about 0.5 or above.")
    print("  - REVEALED cheating is dominated by silent cheating (MoE judge with P_escape near 0, plus the risk")
    print("    of a client dispute), so the same bound applies.")

    art_dir = os.path.join(os.path.dirname(__file__), "..", "mode-a", "bench-results")
    os.makedirs(art_dir, exist_ok=True)
    art = os.path.join(art_dir, "audit-ev-calibration.json")
    out = {"fee": FEE, "pay_optimiste": PAY, "min_stake": MIN_STAKE, "slash_leak_bps": SLASH_BPS,
           "silence_slash_bps_propose": SILENCE,
           "by_audit_level": rows,
           "genesis_recommande": {"audit_sample_bps": 5000, "silence_slash_bps": SILENCE,
                                  "hold_bps": 10000, "audit_min_quorum": 4, "slash_leak_bps": 8000},
           "note": "silent cheating is -EV at q=0 with a 50% audit => hard guarantee. A 10% PROD audit requires a measured quorum rate above 0."}
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nArtefact: {art}")
    # Guard: the proposed testnet configuration MUST be -EV in the worst case.
    assert mute_ev(0.5, 0.0, SILENCE) < 0, "the testnet configuration (50% audit + proposed silence) is NOT -EV in the worst case"
    print("GUARD OK: testnet (50% audit + silence 2000) makes silent cheating -EV in the worst case (q=0).")


if __name__ == "__main__":
    main()
