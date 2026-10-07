#!/usr/bin/env python3
"""final_season_miner.py — the miner's side of the Final Testnet Season (ADR-047): where its rewards go.

    python3 final_season_miner.py payout  [--miner <id>] --address dendra1...
    python3 final_season_miner.py status

The programme sets the miner no test. Everything it pays is read from the chain — the programme's
requests the miner served and that were verified, its verdicts as a drawn juror, the availability windows
it proved (the daemon proves them on its own, `miner.prove_availability_once`) — so the only thing
a miner says to the programme is where its rewards go. Without a declaration they go to its operator.

The declaration is signed with the miner's operator key, exactly like a relay deposit
(`relay_client._signature`): the service attributes it to the operator the chain names for that miner.
Inside the miner's container the identity defaults to the one the daemon resolved
(`/data/keys/identite-resolue`, a `dm1...` id): the daemon RENAMES the keyring entry to it, so the name
`DENDRA_SIGN_KEY` was set to at start-up no longer exists, and signing must follow the rename.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROGRAMME = os.environ.get("DENDRA_FINAL_SEASON_URL", "").rstrip("/")
RESOLVED = "/data/keys/identite-resolue"     # written by miner once the identity is resolved


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None, timeout=20):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        # unreachable, timed out, or an answer that is not JSON (a proxy's page): said, never a traceback
        return 0, {"error": f"{url}: {type(e).__name__}: {e}"}


def _sign(kind: str, key: str, body: bytes, miner_id: str, height: int) -> dict:
    import relay_client
    return relay_client._signature(kind, key, body, miner_id, height)


def status(base: str) -> tuple:
    return _http("GET", f"{base}/status")


def declare_payout(base: str, miner_id: str, address: str, sign=_sign) -> tuple:
    """(http code, answer). Signed at the chain height the service reports, like a relay deposit."""
    body = json.dumps({"address": address, "miner_id": miner_id}, sort_keys=True, separators=(",", ":")).encode()
    code, st = status(base)
    h = st.get("height") if code == 200 else None
    if not isinstance(h, int) or h <= 0:
        # The signature commits to a height; one guessed here would be signed and refused, or worse,
        # accepted at a height the service never reported. Nothing is signed without the reading.
        return 0, {"error": f"the service's height is unknown (status {code}): nothing was signed"}
    headers = sign("fspay", f"payout__{miner_id}", body, miner_id, h)
    if not headers:
        return 0, {"error": "cannot sign (see the relay signing message above)"}
    return _http("POST", f"{base}/payout", body, headers)


def resolved_identity(path: str | None = None) -> str:
    """The `dm1...` identity the daemon resolved and renamed its key to, or '' (absent, unreadable, or not
    a resolved id — a memory, never a guess)."""
    try:
        with open(path or RESOLVED, encoding="utf-8") as f:
            ident = f.read().strip()
    except OSError:
        return ""
    return ident if ident.startswith("dm1") else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Final Testnet Season declarations for a miner identity")
    ap.add_argument("action", choices=("payout", "status"))
    ap.add_argument("--miner", default="", help=f"default: the identity resolved by the daemon ({RESOLVED})")
    ap.add_argument("--address", default="")
    ap.add_argument("--programme", default=PROGRAMME)
    a = ap.parse_args(argv)
    if not a.programme:
        print("DENDRA_FINAL_SEASON_URL is not set (the programme's address, e.g. https://testnet-api.dendranetwork.com/final-season/v1)")
        return 2
    base = a.programme.rstrip("/")
    if a.action == "status":
        code, st = status(base)
        print(json.dumps(st, indent=1))
        return 0 if code == 200 else 1
    resolved = resolved_identity()
    if resolved:
        # The keyring entry now carries this name: signing for the start-up name would sign for nothing.
        import relay_client
        relay_client.set_sign_key(resolved)
    mid = a.miner or resolved
    if not mid:
        print(f"--miner is required: no resolved identity in {RESOLVED}")
        return 2
    code, r = declare_payout(base, mid, a.address)
    print(json.dumps(r))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
