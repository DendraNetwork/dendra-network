#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capacity_sign.py — sign one capacity report, and print the headers that carry the proof.

WHY IT EXISTS (ADR-045, entry 10). The `verified` block of `/capacity` used to be won by NAMING a staked
miner: `do_POST` carried no signature, the intersection was computed against `miner_ids` -- a field the
reporting node writes itself -- and the store key was `machine::node_id`, both ends chosen by the sender.
The served text meanwhile promised that inflating that block "requires staking first". The label was
right and the proof was missing; this is the proof.

WHAT IT PROVES, AND WHAT IT DOES NOT. It proves that the signer holds the key whose address the on-chain
registry records as the OPERATOR of the miner id being reported. It proves NOTHING about the hardware
figures in the body: those stay operator-declared, which is what `_provenance.claim` already says.

WHY IT IS A SEPARATE FILE RATHER THAN INLINE. The body must be signed EXACTLY as it will be sent -- the
signature covers a digest of those bytes, so a body re-serialised between signing and sending is a
different body. Reading the bytes on stdin and printing headers on stdout is the only shape that cannot
re-encode anything in between.

Usage (stdin = the exact JSON body that will be POSTed):
    printf '%s' "$JSON" | DENDRA_SIGN_KEY=<keyname> MINER_ID=<id> [DENDRA_NODE=tcp://host:26657] \
        python3 capacity_sign.py
Prints one `-H 'Header: value'` argument per line, ready for curl. Prints NOTHING and exits non-zero when
it cannot sign -- an unsigned report is still accepted, it simply cannot enter the verified block.
"""
import os
import sys

CAPACITY_KIND = "capacity"


def main() -> int:
    body = sys.stdin.buffer.read()
    if not body:
        print("capacity_sign: empty body on stdin", file=sys.stderr)
        return 2

    key_name = (os.environ.get("DENDRA_SIGN_KEY") or "").strip()
    miner_id = (os.environ.get("MINER_ID") or "").strip()
    if not key_name or not miner_id:
        print("capacity_sign: DENDRA_SIGN_KEY and MINER_ID are required", file=sys.stderr)
        return 2

    try:
        from modea import relay_signature as rs
    except Exception as e:  # pragma: no cover - depends on the deployment layout
        print("capacity_sign: modea.relay_signature unavailable (%s)" % e, file=sys.stderr)
        return 3

    node = (os.environ.get("DENDRA_NODE") or "").strip() or None
    backend = (os.environ.get("DENDRA_KEYRING_BACKEND") or "test").strip()
    keyring_dir = (os.environ.get("DENDRA_KEYRING_DIR") or "").strip() or None

    try:
        address = rs.address_from_key(key_name, keyring_backend=backend, keyring_dir=keyring_dir)
        number, sequence = rs.numero_et_sequence(address, node=node)
        # HEIGHT 0, AND THE REASON IS WRITTEN RATHER THAN THE FIELD BEING QUIETLY FILLED. The canonical
        # message carries a height, and the relay uses it for anti-replay. This endpoint does NOT: a
        # captured signed report can be re-posted, and re-posting it overwrites the same key with the
        # same content, which changes nothing. The escalation that DID matter -- one party holding
        # unbounded entries all naming one staked id -- is closed by the STORE KEY following the proof,
        # not by a nonce. Claiming replay protection here would be the kind of promise this record
        # exists to stop making.
        height = 0
        out_headers = rs.headers(CAPACITY_KIND, miner_id, body, miner_id, int(height),
                             key_name=key_name, address=address,
                             account_number=number, sequence=sequence,
                             keyring_backend=backend, keyring_dir=keyring_dir)
    except Exception as e:
        # DELIBERATELY SILENT ON STDOUT. A half-written header list would be worse than none: curl would
        # send a signature that cannot verify, and the operator would read a refusal where there is only
        # a missing key. The reason goes to stderr, the caller falls back to an unsigned POST.
        print("capacity_sign: cannot sign (%s)" % e, file=sys.stderr)
        return 4

    for nom, valeur in out_headers.items():
        print("-H")
        print("%s: %s" % (nom, valeur))
    return 0


if __name__ == "__main__":
    sys.exit(main())
