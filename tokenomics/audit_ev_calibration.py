#!/usr/bin/env python3
"""Calibration: the audit and the penalties must keep cheating at negative EV before the rewarded testnet.

The mechanisms already exist (verification_mode, audit_sample_bps, slash_leak_bps, hold_bps,
audit_min_quorum, silence_slash_bps). This script calibrates their genesis values and writes a numeric
artefact showing that:
  (1) revealed cheating (a plausible fake answer) is -EV, because the MoE judge catches it (P_escape near 0);
  (2) silent cheating (never reveal, save all the compute) is -EV even when audits never reach quorum
      (q=0, the worst liveness case). This is the binding case, and silence_slash_bps closes it.

Per-job EV model for a dishonest primary, relative to not playing, in udndr:
  - not audited (prob 1-a)        : collects the released optimistic payment for fake work = +pay
  - audited + quorum (prob a*q)   : hard slash                                             = -s*stake
  - audited + no quorum (a*(1-q)) : payment clawback plus the refundable silence penalty   = -silence*stake
  pay = fee*(1 - cut_bps/10000); s = slash_leak_bps/10000; silence = silence_slash_bps/10000.
hold_bps=10000 does not change the sign: it delays the unaudited payment without cancelling it, so the
model is conservative.
"""
import json, os, sys

# --- genesis parameters of the rewarded network (docker/entrypoint-chain.sh) ---
FEE        = int(os.environ.get("FEE", "4500"))          # udndr per job (cli.py default)
CUT_BPS    = int(os.environ.get("CUT_BPS", "1500"))      # protocol_fee_bps (15%) -> pay = 85% of the fee
MIN_STAKE  = int(os.environ.get("MIN_STAKE", "1000000")) # min_stake as measured on the live chain
# The default is the value docker/entrypoint-chain.sh sets and the chain answers, never a figure copied
# from a comment: a parameter is taken from what the chain applies.
SLASH_BPS  = int(os.environ.get("SLASH_BPS", "8000"))    # slash_leak_bps (80%)

PAY = FEE * (10000 - CUT_BPS) // 10000                    # optimistic payment collected on a job that is not audited


def mute_ev(a, q, silence_bps, stake=MIN_STAKE):
    """Per-job EV of silent cheating (udndr). a = audit probability, q = quorum success rate, silence_bps in bps."""
    s = SLASH_BPS / 10000
    sil = silence_bps / 10000
    unaudited_gain = (1 - a) * PAY
    quorum_loss = a * q * s * stake
    noquorum_loss = a * (1 - q) * sil * stake
    return unaudited_gain - quorum_loss - noquorum_loss


def min_silence_for_neg_ev(a, q=0.0):
    """Smallest silence_slash_bps that makes silent cheating -EV at quorum rate q (default q=0, worst liveness case)."""
    for bps in range(0, 10001, 5):
        if mute_ev(a, q, bps) < 0:
            return bps
    return None


def main():
    audit_levels = {"testnet 50%": 5000, "intermediate 30%": 3000, "PROD 10%": 1000}
    SILENCE = int(os.environ.get("SILENCE_BPS", "2000"))  # proposed value to arm

    print(f"== Cheating EV calibration ==  fee={FEE}  pay(optimistic)={PAY}  stake(min)={MIN_STAKE}  slash={SLASH_BPS/100:.0f}%")
    print(f"   proposed silence_slash_bps = {SILENCE} ({SILENCE/100:.0f}% of the stake, refundable)\n")

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
        print(f"{name:18s} {need_s:>18s} {ev_q0:>14.0f} {ev_q3:>15.0f}  {'OK -EV' if ok else 'insufficient (+EV)'}")

    # The chain does not apply the parameter the table above rests on: `silence_slash_bps` is read only
    # by `clawbackPayment`, which has no production caller (x/jobs/keeper/antievasion.go). The table
    # below uses silence = 0, which is what a silent cheater faces on the running chain.
    eff_rows = []
    print()
    print(f"{'audit':18s} {'effective silent EV @q=0':>26s} {'@q=.3':>10s}   (silence penalty not applied)")
    for name, a_bps in audit_levels.items():
        a = a_bps / 10000
        ev0 = mute_ev(a, 0.0, 0)
        ev3 = mute_ev(a, 0.3, 0)
        eff_rows.append({"audit_bps": a_bps, "mute_ev_q0": round(ev0, 1),
                         "mute_ev_q03": round(ev3, 1), "neg_ev_effective": ev0 < 0})
        print(f"{name:18s} {ev0:>26.0f} {ev3:>10.0f}   {'-EV' if ev0 < 0 else '+EV: staying mute pays'}")

    print()
    print("How to read this:")
    print("  - The effective table describes this chain. `silence_slash_bps` is inert: its only reader,")
    print("    clawbackPayment, has no production caller, so whatever the genesis sets for it changes")
    print("    nothing until that path is wired back.")
    print(f"  - At 50% audit (testnet), silence_slash_bps={SILENCE} makes silent cheating -EV even if audits")
    print("    never reach quorum (q=0). That guarantee does not depend on committee liveness.")
    print("  - At 10% audit (the production target), the silence penalty alone is not enough at q=0: a real")
    print("    quorum rate (q>0) is required, so keep audit_sample_bps at 5000 on the testnet until the")
    print("    distributed quorum rate is measured at about 0.5 or above.")
    print("  - Revealed cheating is dominated by silent cheating (MoE judge with P_escape near 0, plus the risk")
    print("    of a client dispute), so the same bound applies.")

    art_dir = os.path.join(os.path.dirname(__file__), "..", "services", "bench-results")
    os.makedirs(art_dir, exist_ok=True)
    art = os.path.join(art_dir, "audit-ev-calibration.json")
    out = {"fee": FEE, "pay_optimistic": PAY, "min_stake": MIN_STAKE, "slash_leak_bps": SLASH_BPS,
           "silence_slash_bps_proposed": SILENCE,
           "by_audit_level": rows,
           "by_audit_level_effective": eff_rows,
           "silence_slash_effective_bps": 0,
           "silence_slash_inert_reason": "clawbackPayment has no production caller (x/jobs/keeper/antievasion.go); "
                                        "silence_slash_bps is read there and nowhere else",
           "genesis_recommended": {"audit_sample_bps": 5000, "silence_slash_bps": SILENCE,
                                  "hold_bps": 10000, "audit_min_quorum": 4, "slash_leak_bps": 8000},
           "note": "silent cheating is -EV at q=0 with a 50% audit => hard guarantee. A 10% production audit requires a measured quorum rate above 0."}
    with open(art, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nArtefact: {art}")
    # The proposed configuration, for a network that arms the penalty, must be -EV in the worst case.
    assert mute_ev(0.5, 0.0, SILENCE) < 0, "the testnet configuration (50% audit + proposed silence) is not -EV in the worst case"
    print(f"OK (proposed configuration): 50% audit + silence {SILENCE} makes silent cheating -EV at q=0.")
    # The running chain is reported, not asserted: it may be +EV, but the output must say so.
    eff = mute_ev(0.5, 0.0, 0)
    if eff < 0:
        print(f"Effective: silent cheating is -EV ({eff:.0f} udndr) even with the silence penalty inert.")
    else:
        print(f"WARNING: in the effective configuration, with silence_slash inert, silent cheating is +EV ({eff:+.0f} udndr per job at q=0).")
        print("   The audit lottery alone does not deter it at this quorum rate. Arming the penalty means")
        print("   giving clawbackPayment a caller again; setting the parameter changes nothing on its own.")


if __name__ == "__main__":
    main()
