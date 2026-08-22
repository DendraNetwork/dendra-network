#!/usr/bin/env python3
"""modea_attest.py -- prints the CONFINEMENT ATTESTATION of the Mode A miner client.

Measures: sha256 manifest of the code (modea/*.py) + actual confinement state (dumpable/no-new-privs/
core/mlockall). HONEST: SOFTWARE attestation (self-measurement) -> detects modified code or missing
confinement, but NOT a post-attestation memory patch (TOCTOU) nor a lying root operator.
The hardware root of trust (TEE/TPM) is not implemented.

Usage:
  python3 modea_attest.py                                   # confinement attestation (human-readable)
  python3 modea_attest.py --hash [--model-id ID]            # prints the MEASURED HASH (for the allow-list)
  python3 modea_attest.py --sign --keydir DIR --id MID      # full SIGNED attestation (JSON)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from modea import confine

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash", action="store_true", help="print only the measured hash")
    ap.add_argument("--sign", action="store_true", help="signed attestation (requires --keydir and --id)")
    ap.add_argument("--keydir", default="")
    ap.add_argument("--id", default="")
    ap.add_argument("--model-id", default="")
    a = ap.parse_args()

    if a.sign:
        if not (a.keydir and a.id):
            print("ERROR: --sign requires --keydir and --id", file=sys.stderr)
            sys.exit(2)
        sk, pub = confine.load_or_create_attest_key(a.keydir, a.id)
        att = confine.signed_attestation(sk, miner_id=a.id, model_id=a.model_id)
        print(json.dumps(att, indent=2, ensure_ascii=False))
        print(f"\n[attest] measured hash (allow-list) = {att['measured_hash']}", file=sys.stderr)
        print(f"[attest] signing Ed25519 pubkey    = {pub}", file=sys.stderr)
        sys.exit(0)

    if a.hash:
        h, _ = confine.measured_hash(model_id=a.model_id)
        print(h)
        sys.exit(0)

    att = confine.confinement_attestation()
    print(json.dumps(att, indent=2, ensure_ascii=False))
    c = att.get("confinement", {})
    strong = c.get("non_dumpable") and c.get("no_new_privs") and c.get("core_dumps_disabled")
    print("\n[attest] process confinement:",
          "STRONG (rootless maximum)" if strong else "PARTIAL (see platform)",
          "| residual root risk: YES (Mode A is deterrence, not cryptography)")
