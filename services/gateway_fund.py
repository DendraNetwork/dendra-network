#!/usr/bin/env python3
"""Funds the gateway subsidy account (gw) from the faucet at startup.

The gw key is created by the chain in the shared keyring (volume /root/.dendra).
We fetch its address, call the faucet until the balance reaches >= MIN, then hand back control.
Best-effort: if the faucet is not ready, we retry; we never fail the gateway startup.

Env: DENDRA_SUBSIDY_CLIENT=gw, DENDRA_NODE, DENDRA_HOME, DENDRA_FAUCET, DENDRA_GW_MIN_BAL.
"""
import json
import os
import subprocess
import time
import urllib.request

NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
HOME = os.environ.get("DENDRA_HOME", "/root/.dendra")
KEY = os.environ.get("DENDRA_SUBSIDY_CLIENT", "gw")
FAUCET = os.environ.get("DENDRA_FAUCET", "http://faucet:4500")
MIN = int(os.environ.get("DENDRA_GW_MIN_BAL", "100000"))


def addr():
    r = subprocess.run(["dendrad", "keys", "show", KEY, "-a", "--keyring-backend", "test", "--home", HOME],
                       capture_output=True, text=True)
    return r.stdout.strip()


def bal(a):
    r = subprocess.run(["dendrad", "query", "bank", "balances", a, "--node", NODE, "--output", "json"],
                       capture_output=True, text=True)
    try:
        return sum(int(x["amount"]) for x in json.loads(r.stdout).get("balances", []))
    except Exception:
        return 0


def main():
    a = addr()
    if not a:
        print("[gateway_fund] pas d'adresse pour %s (keyring partage absent ?)" % KEY, flush=True)
        return
    print("[gateway_fund] %s=%s" % (KEY, a), flush=True)
    for _ in range(40):
        b = bal(a)
        if b >= MIN:
            print("[gateway_fund] solde=%d udndr OK" % b, flush=True)
            return
        try:
            req = urllib.request.Request(FAUCET, data=json.dumps({"address": a}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            print("[gateway_fund] faucet pas pret, reessai (%s)" % str(e)[:80], flush=True)
        time.sleep(3)
    print("[gateway_fund] WARN: %s non finance apres 40 essais" % KEY, flush=True)


if __name__ == "__main__":
    main()
