#!/usr/bin/env python3
"""Tests of c3_jury_model — `python3 test_c3_jury_model.py`

The central case is (3): adding ONE perfect juror changes NOTHING, because the added seat raises the
relative bar at the same time. That is the counter-intuitive result the model exists to establish; if
it disappeared, the model would serve no purpose.
"""
import sys

from c3_jury_model import bar_for, p_at_least, poisson_binomial, rates_from_forensics, scenarios

OK = [0]
FAIL = []


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


def close(a, b, eps=1e-9):
    return abs(a - b) < eps


# --- (1) Poisson binomial: reduces to the binomial when every rate is equal -----------------------
d = poisson_binomial([0.5] * 4)
chk("(1) probabilities sum to 1", close(sum(d), 1.0))
chk("(1b) P(exactly 2 of 4 at p=0.5) = 6/16", close(d[2], 0.375))
chk("(1c) heterogeneous: p=0 and p=1 -> exactly 1 success, with certainty",
    close(poisson_binomial([0.0, 1.0])[1], 1.0))
chk("(1d) P(>=0) = 1 and P(> n) = 0", close(p_at_least([0.3, 0.3], 0), 1.0)
    and close(p_at_least([0.3, 0.3], 3), 0.0))

# --- (2) the bar is the chain's bar ---------------------------------------------------------------
chk("(2) 7 seats, quorum 4 -> bar 5", bar_for(7, 4) == 5)
chk("(2b) 8 seats -> bar 6 (ceil(2/3 x 8) exceeds the quorum)", bar_for(8, 4) == 6)

# --- (3) ADDING ONE PERFECT JUROR CHANGES NOTHING -------------------------------------------------
s = scenarios([4 / 7] * 7, 4, add_max=2)
add0 = s["levier_ajouter_des_jures"][0]
add1 = s["levier_ajouter_des_jures"][1]
add2 = s["levier_ajouter_des_jures"][2]
chk("(3) +1 PERFECT juror: the bar goes from 5 to 6 and P(resolved) is UNCHANGED",
    add1["bar"] == add0["bar"] + 1 and close(add1["p_resolve"], add0["p_resolve"]),
    f"{add0['p_resolve']:.4f} -> {add1['p_resolve']:.4f}")
chk("(3b) it takes +2 for the number to move at all", add2["p_resolve"] > add0["p_resolve"] + 0.2)

# --- (4) making jurors vote beats adding jurors, on the same figures -------------------------------
best_add = max(s["levier_ajouter_des_jures"], key=lambda r: r["p_resolve"])["p_resolve"]
lift80 = [r for r in s["levier_faire_voter"] if r["target_rate"] == 0.8][0]
chk("(4) lifting the rates to 80 % (zero extra machines) beats +2 perfect jurors",
    lift80["p_resolve"] > best_add, f"{lift80['p_resolve']:.3f} vs {best_add:.3f}")
chk("(4b) the 'make them vote' lever does NOT move the bar", lift80["bar"] == add0["bar"])

# --- (5) the model carries its assumptions (a figure without assumptions is a false witness) ------
chk("(5) the 3 assumptions are returned alongside the result", len(s["hypotheses"]) == 3)
chk("(5b) including independence, marked FALSE when machines are shared",
    any("independence" in h and "FALSE" in h for h in s["hypotheses"]))

# --- (6) MEASURED rates read from the forensics report, with a warning on thin samples ------------
rep = {"jurors": [{"miner": "a", "convoqué": 11, "a_voté": 7, "taux": 0.636},
                  {"miner": "b", "convoqué": 3, "a_voté": 0, "taux": 0.0}],
       "context": {"audit_min_quorum": 4}}
rates, ids, warn = rates_from_forensics(rep)
chk("(6) rates and identities extracted from the report", rates == [0.636, 0.0] and ids == ["a", "b"])
chk("(6b) a juror seen fewer than 5 times is flagged as noisy", warn and "b" in warn[0])
chk("(6c) report without a matrix -> None plus a reason", rates_from_forensics({})[0] is None)

# --- (7) degenerate case: no juror votes -> P = 0, never a division by zero ------------------------
z = scenarios([0.0] * 7, 4, add_max=1)
chk("(7) all silent -> P(resolved) = 0", close(z["base"]["p_resolve"], 0.0))
chk("(7b) and +1 perfect juror is still not enough (bar 6)",
    close(z["levier_ajouter_des_jures"][1]["p_resolve"], 0.0))

print(f"\n{OK[0]} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
