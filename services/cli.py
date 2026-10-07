#!/usr/bin/env python3
"""Dendra CLI -- drive the local network from the terminal.

  state                     network state (miners, balances, pools, height)
  submit "<prompt>"         submit an inference request: escrow -> committee -> inference -> verdict -> payment
  job <jid>                 job state (who answered / anchored commits)

Variables: DENDRA_RELAY (default http://127.0.0.1:8645).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import client as dc

RELAY = os.environ.get("DENDRA_RELAY", "http://127.0.0.1:8645")


def cmd_state(_a):
    st = dc.network_state()
    print(f"== Dendra network ==  (block height {st['height']})\n")
    print(f"Registered miners: {len(st['miners'])}")
    for m in st["miners"]:
        print(f"  - {m['id']:18} bond={m['stake']:>6}  balance={m['balance']/1_000_000:>11.4f} DNDR   {m['operator'][:20]}...")
    if st["pools"]:
        print("\nPools:", "  ".join(f"{k}={v}" for k, v in st["pools"].items()))


def cmd_submit(a):
    print(f"[submit] \"{a.prompt}\"   fee={a.fee}  reward={a.reward}  committee={a.k}  relay={RELAY}\n")
    r = dc.quick(a.prompt, a.fee, a.reward, RELAY, client=a.client, k=a.k, timeout=a.timeout)
    if "error" in r:
        print("ERROR:", r["error"]); sys.exit(1)
    print(f"job {r['jid']}  | beacon {r['beacon']}  | committee {r['committee']}")
    for mid, ans in r["results"].items():
        print(f"   {mid:18} -> {'(pending / no answer)' if not ans else repr(ans[:90])}")
    print("\n================ ANSWER (committee majority) ================")
    print(r["answer"] or "(no answer received within the deadline)")
    print("=============================================================")
    print(f"on-chain settlement (semantic: pays same-meaning, slashes the outlier): {r['settle']}")


def cmd_job(a):
    state = dc.run(["dendrad", "query", "jobs", "get-job", a.jid])
    print(state.strip() or "(job not found)")
    beacon = dc.get_beacon(a.jid)
    ids = [m["id"] for m in dc.registered_miners()]
    comm = dc.committee(f"{beacon}|{a.jid}", ids) if beacon else []
    print(f"\ncommittee (beacon {beacon}): {comm}")
    for mid in comm:
        c = dc.query("get-commit", f"{a.jid}__{mid}")
        anchored = "resultCommit" in c or "result_commit" in c
        print(f"  {mid:18} commit anchored: {anchored}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state")
    s = sub.add_parser("submit")
    s.add_argument("prompt")
    s.add_argument("--fee", type=int, default=4500)     # udndr: 0.0045 DNDR / job
    s.add_argument("--reward", type=int, default=1500)  # udndr: 0.0015 DNDR / miner
    s.add_argument("--client", default="alice")
    s.add_argument("--k", type=int, default=3)
    s.add_argument("--timeout", type=int, default=240)
    j = sub.add_parser("job")
    j.add_argument("jid")
    a = ap.parse_args()
    {"state": cmd_state, "submit": cmd_submit, "job": cmd_job}[a.cmd](a)


if __name__ == "__main__":
    main()
