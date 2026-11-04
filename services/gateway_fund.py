#!/usr/bin/env python3
"""Funds the gateway subsidy account (gw) from the faucet at startup.

The gw key is created by the chain at genesis, and the gateway signs with it from its own keyring
(DENDRA_HOME, the gateway-keys volume in docker-compose.yml), where docker/entrypoint-services.sh
adopts it once before this script runs. This script reads its address, calls the faucet until the
balance reaches MIN, then returns.

The exit status is part of the contract: 0 funded, 1 not funded. `docker/entrypoint-services.sh` runs
this script in an `until` loop that retries and then raises an alert, and that loop only works if a
failure exits non-zero. The failure stays non-fatal in the caller, which starts the gateway anyway,
because the paid tier does not depend on this account.

Env: DENDRA_SUBSIDY_CLIENT=gw, DENDRA_NODE, DENDRA_HOME, DENDRA_FAUCET, DENDRA_GW_MIN_BAL,
     DENDRA_FAUCET_POW_MAX_S.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The solver is the faucet's own (`faucet.solve_pow`), imported the same way `miner.py`
# does; the import has no side effect because the server sits behind `if __name__ == "__main__"`. A
# second implementation would be one more thing to keep in agreement with the server.
import faucet as faucet_pow

NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
HOME = os.environ.get("DENDRA_HOME", "/root/.dendra")
KEY = os.environ.get("DENDRA_SUBSIDY_CLIENT", "gw")
FAUCET = os.environ.get("DENDRA_FAUCET", "http://faucet:4500")
MIN = int(os.environ.get("DENDRA_GW_MIN_BAL", "100000"))
POW_MAX_S = float(os.environ.get("DENDRA_FAUCET_POW_MAX_S", "300"))

# These subprocesses run before the gateway starts, so one without a timeout could keep the gateway
# from ever starting while the container only looks slow. Same bound as `faucet._run`.
CMD_TIMEOUT_S = 60


def addr():
    """Address of the subsidy key, or "" when it cannot be read."""
    try:
        r = subprocess.run(
            ["dendrad", "keys", "show", KEY, "-a", "--keyring-backend", "test", "--home", HOME],
            capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print("[gateway_fund] keys show failed: %s" % str(e)[:120], flush=True)
        return ""


def bal(a):
    """Total balance in udndr, or None when it could not be measured.

    An unreachable node, a timeout or a malformed answer returns None, never 0: an empty account and a
    failed measurement call for different decisions, and the caller makes them.
    """
    try:
        r = subprocess.run(
            ["dendrad", "query", "bank", "balances", a, "--node", NODE, "--output", "json"],
            capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
        if r.returncode != 0:
            return None
        return sum(int(x["amount"]) for x in json.loads(r.stdout).get("balances", []))
    except Exception:
        return None


def pow_bits():
    """Difficulty the faucet announces, or None when the probe fails.

    None is not 0: `0` means the faucet declares the proof-of-work disarmed, None means unknown.
    """
    try:
        with urllib.request.urlopen(FAUCET, timeout=15) as r:
            return int(json.loads(r.read()).get("pow_bits", 0))
    except Exception:
        return None


def ask(a, nonce):
    """POST the request. Returns (ok, detail, required_bits) — required_bits from the refusal body."""
    body = {"address": a}
    if nonce:
        body["pow"] = nonce
    req = urllib.request.Request(FAUCET, data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True, "", 0
    except urllib.error.HTTPError as e:
        # The refusal body names the cause and, on a proof-of-work refusal, the difficulty the faucet
        # expects. It goes into the gateway's log.
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        want = 0
        try:
            want = int(json.loads(raw).get("pow_bits", 0))
        except Exception:
            pass
        return False, "HTTP %s %s" % (e.code, raw), want
    except Exception as e:
        return False, str(e)[:160], 0


def request_drip(a):
    """One funding attempt, honouring the proof-of-work contract. Returns (ok, detail).

    `faucet` refuses any request without a valid token as soon as POW_BITS > 0, which the public
    deployment sets (`deploy/launch/.env.public.example`, DENDRA_FAUCET_POW_BITS).
    """
    bits = pow_bits()
    nonce = ""
    if bits:
        nonce = faucet_pow.solve_pow(a, bits, deadline_s=POW_MAX_S)
        if not nonce:
            return False, "proof-of-work unsolved within %.0f s (%d bits)" % (POW_MAX_S, bits)
    ok, detail, want = ask(a, nonce)
    if ok:
        return True, ""
    # The server asked for a difficulty we did not meet: the GET probe failed, or the difficulty rose
    # between the probe and the request. Retry once with the token it asks for.
    if want > 0 and not nonce:
        nonce = faucet_pow.solve_pow(a, want, deadline_s=POW_MAX_S)
        if not nonce:
            return False, "proof-of-work unsolved within %.0f s (%d bits)" % (POW_MAX_S, want)
        ok, detail, _ = ask(a, nonce)
        if ok:
            return True, ""
    return False, detail


def main():
    a = addr()
    if not a:
        print("[gateway_fund] no address for %s (not in the keyring at %s?)" % (KEY, HOME), flush=True)
        return 1
    print("[gateway_fund] %s=%s" % (KEY, a), flush=True)
    for _ in range(40):
        b = bal(a)
        if b is None:
            # The chain could not be read; this is not a zero balance.
            print("[gateway_fund] balance not measured (node unreachable?) — retrying", flush=True)
        elif b >= MIN:
            print("[gateway_fund] balance=%d udndr OK" % b, flush=True)
            return 0
        else:
            ok, detail = request_drip(a)
            if not ok:
                print("[gateway_fund] faucet refused: %s" % detail, flush=True)
        time.sleep(3)
    print("[gateway_fund] WARN: %s not funded after 40 attempts" % KEY, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
