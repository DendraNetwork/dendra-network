#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capacity_sign.py — sign one capacity report, and print the headers that carry the proof.

Why it exists. Entering the `verified` block of `/capacity` must take a signature by
the operator of a staked miner. Without one, any sender could name a staked miner in `miner_ids`, a field
the reporting node writes itself, and be counted.

What it proves, and what it does not. It proves that the signer holds the key whose address the on-chain
registry records as the operator of the miner id being reported. It proves nothing about the hardware
figures in the body: those stay operator-declared, which is what `_provenance.claim` already says.

Why it is a separate file rather than inline. The body must be signed exactly as it will be sent -- the
signature covers a digest of those bytes, so a body re-serialised between signing and sending is a
different body. Reading the bytes on stdin and printing headers on stdout is the only shape that cannot
re-encode anything in between.

Usage (stdin = the exact JSON body that will be POSTed):
    printf '%s' "$JSON" | DENDRA_SIGN_KEY=<keyname> MINER_ID=<id> [DENDRA_NODE=tcp://host:26657] \
        python3 capacity_sign.py
Prints one `-H 'Header: value'` argument per line, ready for curl. Prints nothing and exits non-zero when
it cannot sign -- an unsigned report is still accepted, it simply cannot enter the verified block.
"""
import os
import sys

CAPACITY_KIND = "capacity"


def signed_headers(body: bytes, key_name: str, miner_id: str, node=None, keyring_dir=None) -> dict:
    """{header: value} proving `body` was signed by `key_name`, for `miner_id`. Raises when it cannot sign.
    One signer for the two senders: this file's command line (publish-capacity.sh) and capacity_sender.py, which
    calls it in the miner container."""
    from modea import relay_signature as rs
    # No backend is named here: relay_signature reads it from the state of the disk (modea/keyring.py) and
    # hands the passphrase of an encrypted keyring to dendrad on stdin. A backend taken from a variable
    # with a default of `test` signed nothing the day the keyring was encrypted.
    backend = None
    address = rs.address_from_key(key_name, keyring_backend=backend, keyring_dir=keyring_dir)
    number, sequence = rs.account_number_and_sequence(address, node=node)
    # Height 0, and the reason is written rather than the field being quietly filled. The canonical
    # message carries a height, and the relay uses it for anti-replay. The registry orders the signed
    # reports of one identity by this height, then by the account sequence the signature also covers,
    # and refuses one older than the report it holds (capacity_server.py::older_than_stored). With
    # height 0 the sequence alone orders them: a captured report is refused once the operator has sent
    # a transaction since, and accepted while the sequence has not moved. The escalation of one party
    # holding unbounded entries all naming one staked id is closed by the store key following the proof.
    height = 0
    return rs.headers(CAPACITY_KIND, miner_id, body, miner_id, int(height),
                      key_name=key_name, address=address,
                      account_number=number, sequence=sequence,
                      keyring_backend=backend, keyring_dir=keyring_dir)


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
    keyring_dir = (os.environ.get("DENDRA_KEYRING_DIR") or "").strip() or None

    try:
        out_headers = signed_headers(body, key_name, miner_id, node=node, keyring_dir=keyring_dir)
    except Exception as e:
        # Deliberately silent on stdout. A half-written header list would be worse than none: curl would
        # send a signature that cannot verify, and the operator would read a refusal where there is only
        # a missing key. The reason goes to stderr, the caller falls back to an unsigned POST.
        print("capacity_sign: cannot sign (%s)" % e, file=sys.stderr)
        return 4

    for name, value in out_headers.items():
        print("-H")
        print("%s: %s" % (name, value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
