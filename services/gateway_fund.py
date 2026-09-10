#!/usr/bin/env python3
"""Funds the gateway subsidy account (gw) from the faucet at startup.

The gw key is created by the chain in the shared keyring (volume /root/.dendra). We fetch its address,
call the faucet until the balance reaches >= MIN, then hand back control.

EXIT STATUS IS PART OF THE CONTRACT, and it used to be a lie. `docker/entrypoint-services.sh` wraps
this script in `until python3 gateway_fund.py; do … ALERT … done`, whose comment promises "this loop
RETRIES, then SHOUTS". Nothing here ever exited non-zero — `main()` returned None on all three of its
paths and was called bare — so `until` was satisfied on the FIRST pass, the retry loop never ran once
and the ALERT was structurally unreachable. Funding now returns a status: 0 funded, 1 not funded.
Non-fatal stays true where it belongs — in the CALLER, which breaks out and starts the gateway anyway,
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

# THE SOLVER IS THE FAUCET'S OWN. `faucet.solve_pow` says it in its docstring — "every faucet
# requester calls THIS, never a copy" — and importing the module is side-effect free: the server lives
# behind `if __name__ == "__main__"`. `miner.py` already imports it the same way. A second
# implementation of a proof-of-work is a second thing to keep in agreement with the server.
import faucet as faucet_pow

NODE = os.environ.get("DENDRA_NODE", "tcp://chain:26657")
HOME = os.environ.get("DENDRA_HOME", "/root/.dendra")
KEY = os.environ.get("DENDRA_SUBSIDY_CLIENT", "gw")
FAUCET = os.environ.get("DENDRA_FAUCET", "http://faucet:4500")
MIN = int(os.environ.get("DENDRA_GW_MIN_BAL", "100000"))
POW_MAX_S = float(os.environ.get("DENDRA_FAUCET_POW_MAX_S", "300"))

# A subprocess with no timeout can hang forever, and these two run BEFORE `exec python3
# gateway.py`: the gateway would never start, and the container would look merely slow. The
# neighbouring service in this same directory already carries the right shape (`faucet._run`
# takes t=60).
CMD_TIMEOUT_S = 60


def addr():
    """Address of the subsidy key, or "" when it cannot be read."""
    try:
        r = subprocess.run(
            ["dendrad", "keys", "show", KEY, "-a", "--keyring-backend", "test", "--home", HOME],
            capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
        return r.stdout.strip()
    except Exception as e:
        print("[gateway_fund] keys show failed: %s" % str(e)[:120], flush=True)
        return ""


def bal(a):
    """Total balance in udndr, or None when it could NOT be measured.

    THREE STATES, NEVER TWO. This returned 0 on any exception, so an unreachable node, a timeout or a
    malformed answer all read as "the account is empty" — the reading that triggers a funding attempt
    and, worse, the reading that would let a caller believe it had measured something. None says the
    measurement failed; the caller decides what to do with that, and it is not the same decision.
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
    """Difficulty the faucet ANNOUNCES, or None when the probe fails.

    None is not 0: `0` means "the faucet declares the proof-of-work disarmed", None means "unknown".
    Posting without a token while believing the contract was honoured is exactly the confusion that
    left this account unfunded.
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
        # THE REFUSAL BODY IS READ, NOT DISCARDED: the faucet names the cause and, on a proof-of-work
        # refusal, the difficulty it expects. Throwing it away is what made this failure
        # undiagnosable from the gateway's logs.
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

    THE BODY USED TO CARRY ONLY THE ADDRESS. `faucet` refuses any request without a valid token
    as soon as POW_BITS > 0, and the public deployment arms exactly that
    (`deploy/launch/.env.public.example`, DENDRA_FAUCET_POW_BITS=20 — and the live faucet answers
    pow_bits=20). So under the documented public configuration this account could never be funded by
    this path, and the caller's alert could never fire to say so.
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
    # The server names a difficulty that was not honoured — the GET probe was unavailable, or the
    # difficulty rose between the probe and the request. One retry, with the token it asks for.
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
        print("[gateway_fund] no address for %s (shared keyring missing?)" % KEY, flush=True)
        return 1
    print("[gateway_fund] %s=%s" % (KEY, a), flush=True)
    for _ in range(40):
        b = bal(a)
        if b is None:
            # NOT a zero balance: the chain could not be read. Saying so is the whole point.
            print("[gateway_fund] balance NOT MEASURED (node unreachable?) — retrying", flush=True)
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
