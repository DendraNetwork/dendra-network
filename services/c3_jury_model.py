#!/usr/bin/env python3
"""
c3_jury_model.py — which LEVER closes open audits: ADDING jurors, or getting the jurors already
seated to VOTE?

Why this file exists. The reflex answer is "we need more machines". It is wrong here, and the
arithmetic says so: the chain's bar is RELATIVE (`max(audit_min_quorum, ⌈2/3 x seats⌉)`, see
`auditSlashDecision`). Adding a juror adds a SEAT, and therefore raises the bar at the same time. One
more perfect juror moves almost nothing; what moves everything is the VOTING RATE of the seats
already anchored. This script quantifies both, from the rates MEASURED by `c3_jury_forensics.py`,
never from an assumed rate.

Model: exact Poisson binomial (each juror carries its own measured rate, not an average), computed by
dynamic programming. No random draw, no sampling: the result is reproducible.

WHAT THE MODEL ASSUMES, AND WHERE IT CAN BE WRONG (read before deciding):
  - INDEPENDENCE of votes across jurors. False if two "operators" are the same machine or the same
    GPU: one outage silences them together, and the real risk is then worse than this computation.
  - STATIONARITY: the rate measured yesterday predicts tomorrow. False just after a restart.
  - the committee draw is assumed to take every eligible juror; in practice it draws `K` seats among
    the eligible ones (here, the observed seats), which the script takes as an input.
These three caveats are PRINTED alongside the result: a number without its assumptions is a false
witness.

Usage:
  python3 c3_jury_model.py --from-forensics p9-jury.json          # MEASURED rates (recommended)
  python3 c3_jury_model.py --rates 0.6,0.6,0.6,0.6,0.6,0.6,0.6 --min-quorum 4
"""
import argparse
import json
import math
import sys


def bar_for(seats, audit_min_quorum):
    """The chain's EXACT bar: max(audit_min_quorum, ⌈2/3 x seats⌉) — see antievasion.go."""
    if seats <= 0:
        return None
    return max(int(audit_min_quorum), -(-2 * seats // 3))


def poisson_binomial(ps):
    """EXACT distribution of the number of successes for heterogeneous probabilities (DP, O(n^2))."""
    dist = [1.0]
    for p in ps:
        p = min(max(float(p), 0.0), 1.0)
        nxt = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nxt[k] += v * (1 - p)
            nxt[k + 1] += v * p
        dist = nxt
    return dist


def p_at_least(ps, k):
    if k <= 0:
        return 1.0
    d = poisson_binomial(ps)
    if k >= len(d):
        return 0.0
    return sum(d[k:])


def scenarios(rates, min_quorum, add_max=6, targets=(0.7, 0.8, 0.9, 1.0)):
    """Returns both levers, quantified on the SAME basis: P(bar reached) for one audit."""
    rates = [float(r) for r in rates]
    n = len(rates)
    base_bar = bar_for(n, min_quorum)
    base = {"seats": n, "bar": base_bar, "p_resolve": p_at_least(rates, base_bar)}

    add = []
    for k in range(0, add_max + 1):
        ps = rates + [1.0] * k          # ADDED jurors assumed PERFECT (optimistic upper bound)
        b = bar_for(n + k, min_quorum)
        add.append({"added_perfect_jurors": k, "seats": n + k, "bar": b,
                    "p_resolve": p_at_least(ps, b)})

    lift = []
    for t in targets:
        ps = [max(r, t) for r in rates]  # lift the laggards to the target rate, seats unchanged
        lift.append({"target_rate": t, "seats": n, "bar": base_bar,
                     "p_resolve": p_at_least(ps, base_bar),
                     "jurors_to_fix": sum(1 for r in rates if r < t)})
    return {"base": base, "levier_ajouter_des_jures": add, "levier_faire_voter": lift,
            "rates_used": rates,
            "hypotheses": [
                "independence of votes across jurors — FALSE if two identities share a machine or a "
                "GPU (one outage silences them together: the real risk is then WORSE than this model)",
                "stationarity: the measured rate predicts the future rate — false just after a restart",
                "the anchored seats are the observed ones; the real draw may pick others"]}


def rates_from_forensics(rep):
    """Extracts the per-juror MEASURED rates from the forensics report. -> (rates, ids, warnings)."""
    warn = []
    jurors = rep.get("jurors") or []
    if not jurors:
        return None, [], ["forensics report without a per-juror matrix: nothing to model"]
    thin = [j["miner"] for j in jurors if (j.get("convoqué") or 0) < 5]
    if thin:
        warn.append(f"rates estimated on fewer than 5 summonses for: {', '.join(thin)} — very noisy")
    rates = [(j.get("taux") if j.get("taux") is not None else 0.0) for j in jurors]
    return rates, [j["miner"] for j in jurors], warn


def render(s, ids=None, warn=()):
    b = s["base"]
    L = ["=" * 78, "  WHICH LEVER CLOSES AUDITS? (RELATIVE bar: adding a juror also raises the bar)",
         "=" * 78,
         f"  measured base: {b['seats']} seats, bar {b['bar']}, "
         f"P(audit resolved) = {b['p_resolve']:.1%}"]
    if ids:
        L.append("  rate per juror: " + ", ".join(f"{i}={r:.0%}" for i, r in zip(ids, s["rates_used"])))
    L += ["", "-- LEVER 1: ADD jurors (assumed PERFECT = optimistic upper bound)",
          f"   {'added':>8}{'seats':>8}{'bar':>7}{'P(resolved)':>12}"]
    for r in s["levier_ajouter_des_jures"]:
        L.append(f"   {r['added_perfect_jurors']:>8}{r['seats']:>8}{r['bar']:>7}{r['p_resolve']:>11.1%}")
    L += ["", "-- LEVER 2: get the already anchored seats to VOTE (no machine added)",
          f"   {'target':>8}{'to fix':>12}{'bar':>7}{'P(resolved)':>12}"]
    for r in s["levier_faire_voter"]:
        L.append(f"   {r['target_rate']:>8.0%}{r['jurors_to_fix']:>12}{r['bar']:>7}{r['p_resolve']:>11.1%}")
    best_add = max(s["levier_ajouter_des_jures"], key=lambda r: r["p_resolve"])
    best_lift = max(s["levier_faire_voter"], key=lambda r: r["p_resolve"])
    L += ["", "-" * 78,
          f"  Ceiling of the \"add jurors\" lever: {best_add['p_resolve']:.1%} "
          f"(+{best_add['added_perfect_jurors']} perfect jurors)",
          f"  Reachable by getting seats to vote: {best_lift['p_resolve']:.1%} "
          f"(target rate {best_lift['target_rate']:.0%}, {best_lift['jurors_to_fix']} juror(s) to fix)"]
    L.append("  -> " + ("ADDING machines is the wrong lever here: the bar rises with the seats."
                        if best_lift["p_resolve"] > best_add["p_resolve"] + 0.02 else
                        "both levers are comparable on these numbers — do not decide on this computation alone."))
    L += ["", "-- ASSUMPTIONS (a number without its assumptions is a false witness)"]
    L += [f"   - {h}" for h in s["hypotheses"]] + [f"   [!] {w}" for w in warn]
    L.append("-" * 78)
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add jurors or make them vote? (relative bar)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-forensics", help="JSON report from c3_jury_forensics.py (MEASURED rates)")
    src.add_argument("--rates", help="comma-separated per-juror rates (exploration)")
    ap.add_argument("--min-quorum", type=int, default=None, help="audit_min_quorum (otherwise read from the report)")
    ap.add_argument("--add-max", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    ids, warn = None, []
    if a.from_forensics:
        rep = json.load(open(a.from_forensics, encoding="utf-8"))
        rates, ids, warn = rates_from_forensics(rep)
        if rates is None:
            print("MODEL IMPOSSIBLE: " + "; ".join(warn))
            return 3
        mq = a.min_quorum if a.min_quorum is not None else (rep.get("context") or {}).get("audit_min_quorum", 0)
    else:
        rates = [float(x) for x in a.rates.split(",") if x.strip()]
        mq = a.min_quorum if a.min_quorum is not None else 0
    s = scenarios(rates, mq, a.add_max)
    print(json.dumps({**s, "jurors": ids, "warnings": warn}, ensure_ascii=False, indent=2)
          if a.json else render(s, ids, warn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
