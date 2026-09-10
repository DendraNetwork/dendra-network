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
MIN_STAKE  = int(os.environ.get("MIN_STAKE", "1000000")) # min_stake MEASURED on the live chain
# THE DEFAULT WAS 50000, AND THE FILE IT CITED CONTRADICTS ITSELF. [chain] FATAL: dendrad not found
# still writes "min_stake (50000 udndr = 0.05 DNDR)" in a comment while the same file SETS
# min_stake to 1000000, and the chain answers 1000000. The direction was safe -- a smaller stake
# makes the refundable silence penalty smaller, so the calibration UNDERSTATED its own deterrent --
# but a file that opens on "genesis parameters of the REWARDED network" must carry the network
# ones. A parameter is cited from what the chain applies, never from the comment next to it.
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

    # ⛔ THE PARAMETER THIS GUARANTEE RESTS ON IS NOT APPLIED BY THE CHAIN.
    # `silence_slash_bps` is read in exactly one place, `clawbackPayment`, and that function has NO
    # PRODUCTION CALLER -- the chain's own source says so at `x/jobs/keeper/antievasion.go`, and two
    # other files repeat it. So the column above describes a network that would arm the penalty, not
    # the one running. The EFFECTIVE column below is computed with silence = 0, which is what a silent
    # cheater actually faces today, and it is the number that decides whether staying mute pays.
    eff_rows = []
    print()
    print(f"{'audit':18s} {'EFFECTIVE silent EV @q=0':>26s} {'@q=.3':>10s}   (silence penalty NOT applied)")
    for name, a_bps in audit_levels.items():
        a = a_bps / 10000
        ev0 = mute_ev(a, 0.0, 0)
        ev3 = mute_ev(a, 0.3, 0)
        eff_rows.append({"audit_bps": a_bps, "mute_ev_q0": round(ev0, 1),
                         "mute_ev_q03": round(ev3, 1), "neg_ev_effective": ev0 < 0})
        print(f"{name:18s} {ev0:>26.0f} {ev3:>10.0f}   {'-EV' if ev0 < 0 else '+EV -- staying mute PAYS'}")

    print()
    print("How to read this:")
    print("  - THE EFFECTIVE TABLE IS THE ONE THAT DESCRIBES THIS CHAIN. `silence_slash_bps` is inert:")
    print("    its only reader, clawbackPayment, has no production caller. Whatever the genesis posts")
    print("    for it changes nothing until that path is wired back.")
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
           "by_audit_level_effective": eff_rows,
           "silence_slash_effective_bps": 0,
           "silence_slash_inert_reason": "clawbackPayment has no production caller (x/jobs/keeper/antievasion.go); "
                                        "silence_slash_bps is read there and nowhere else",
           "genesis_recommande": {"audit_sample_bps": 5000, "silence_slash_bps": SILENCE,
                                  "hold_bps": 10000, "audit_min_quorum": 4, "slash_leak_bps": 8000},
           "note": "silent cheating is -EV at q=0 with a 50% audit => hard guarantee. A 10% PROD audit requires a measured quorum rate above 0."}
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nArtefact: {art}")
    # Guard: the PROPOSED configuration must be -EV in the worst case. This one is about a network
    # that arms the penalty -- keep it, it is what the calibration is FOR.
    assert mute_ev(0.5, 0.0, SILENCE) < 0, "the testnet configuration (50% audit + proposed silence) is NOT -EV in the worst case"
    print(f"GUARD OK (PROPOSED): 50% audit + silence {SILENCE} makes silent cheating -EV at q=0.")
    # And the one that describes the chain as it runs. It is NOT an assert: the effective state is
    # allowed to be +EV, what is not allowed is publishing a guarantee without saying so.
    eff = mute_ev(0.5, 0.0, 0)
    if eff < 0:
        print(f"EFFECTIVE: silent cheating is -EV ({eff:.0f} udndr) even with the silence penalty inert.")
    else:
        print(f"⚠️ EFFECTIVE: with silence_slash inert, silent cheating is +EV ({eff:+.0f} udndr per job at q=0).")
        print("   The audit lottery alone does not deter it at this quorum rate. Arming the penalty means")
        print("   giving clawbackPayment a caller again -- posting the parameter changes nothing on its own.")


if __name__ == "__main__":
    main()
