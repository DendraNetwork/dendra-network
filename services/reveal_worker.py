#!/usr/bin/env python3
"""REVEAL (PRIMARY side), as a STANDALONE process (no change to miner).

Run by a miner ALONGSIDE `miner.py` (same --id/--keydir). Loop:
  1. discover via `list-job` the `+disputed` jobs where I am the primary (miner_id == me);
  2. fetch from the relay the sealed prompt (`req/<jid>__<me>`) AND my sealed answer (`res/<jid>__<me>`);
  3. RE-DECRYPT them with my X25519 key (same ECDH+AAD as handle_job: info = job_id) -- the cleartext
     is never re-read from disk, only re-derived in RAM long enough to re-seal;
  4. RE-SEAL (prompt + answer) to each committee member (pubs from the relay) and post `reveal/<jid>__<mid>`.

The committee (`judge_worker.py`) opens the reveal, recomputes, judges, and commits its verdict on-chain.
Confidentiality: revealing exposes content only for the ~10% AUDITED, sealed per member (an accepted trade-off).

Usage: python3 reveal_worker.py --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modea import crypto
from modea.crypto import Sealed
import relay_client as relay
import reveal_helpers as rv

NODE = os.environ.get("DENDRA_NODE", "")


def _node():
    return ["--node", NODE] if NODE else []


def run(c, t=120):
    r = subprocess.run(c, capture_output=True, text=True, timeout=t)
    return (r.stdout or "") + (r.stderr or "")


def list_jobs():
    out = run(["dendrad", "query", "jobs", "list-job", "--output", "json", *_node()])
    try:
        d = json.loads(out)
    except Exception:
        return []
    return [(j.get("job_id", ""), j.get("state", ""), j.get("miner_id", "")) for j in d.get("job", [])]


def list_miners():
    """FULL miner-registry records ({miner_id, enc_pubkey, ...}).

    We keep the WHOLE record because `enc_pubkey` is the on-chain ANCHORED reveal key = the root of
    trust. Discarding it (as this function used to) forced the reveal path onto the relay's volatile
    pub cache: a single relay restart then left honest miners unable to seal a reveal, and the
    committee charged them for it. The key was already in this very response — just thrown away."""
    out = run(["dendrad", "query", "jobs", "list-miner", "--output", "json", *_node()])
    try:
        d = json.loads(out)
    except Exception:
        return []
    return [m for m in d.get("miner", []) if m.get("miner_id")]


def list_miner_ids():
    """Legacy id-only view (kept for callers that do not need the anchored keys)."""
    return [m.get("miner_id", "") for m in list_miners()]


def is_disputed(state: str) -> bool:
    return "+disputed" in state and "+resolved" not in state


def _decrypt_from_relay(relay_url, kind, key, sk, eph_pk_hex, aad):
    """Re-decrypts an envelope {client_eph_pk?,nonce,ct} stored at the relay with MY X25519 key."""
    blob = relay.get(relay_url, kind, key)
    if not blob or "ct" not in blob:
        return None
    eph_hex = blob.get("client_eph_pk", eph_pk_hex)
    if not eph_hex:
        return None
    k = crypto.derive_session_key(sk, bytes.fromhex(eph_hex), info=aad)
    try:
        pt = crypto.decrypt(k, Sealed(bytes.fromhex(blob["nonce"]), bytes.fromhex(blob["ct"])), aad=aad)
        return pt.decode("utf-8", "replace"), eph_hex
    finally:
        crypto.zeroize(bytearray(k))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--relay", required=True)
    ap.add_argument("--keydir", required=True)
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    passphrase = os.environ.get("DENDRA_MINER_PASSPHRASE", "")
    skpath = Path(a.keydir) / f"{a.id}.sk"
    if not skpath.exists():
        print(f"[reveal] FATAL : clé X25519 {skpath} absente — lance d'abord miner.py --id {a.id}")
        sys.exit(3)
    sk = crypto.load_sk(str(skpath), passphrase)

    print(f"[reveal] worker révélation {a.id} prêt (relais {a.relay}) — révèle les jobs +disputed dont je suis primaire")
    done = set()
    while True:
        try:
            for job_id, state, primary in list_jobs():
                if not is_disputed(state) or primary != a.id or job_id in done:
                    continue
                if not rv.safe_job_id(job_id):   # job_id from on-chain -> relay key/aad
                    print(f"[reveal] {a.id} job_id NON CONFORME ignoré (défense) : {str(job_id)[:40]!r}")
                    done.add(job_id)
                    continue
                relay_key = f"{job_id}__{a.id}"   # relay storage key (req/res)
                aad = job_id.encode()  # handle_job: info/aad = BARE jobId (not the relay key) -> what the client sealed
                # prompt sealed by the CLIENT (req) -> client_eph_pk carried by the req envelope
                pr = _decrypt_from_relay(a.relay, "req", relay_key, sk, None, aad)
                if not pr:
                    continue
                prompt, client_eph = pr
                # my answer (res): sealed by ME with the SAME session key (same client eph + aad)
                rr = _decrypt_from_relay(a.relay, "res", relay_key, sk, client_eph, aad)
                if not rr:
                    continue
                answer, _ = rr
                pubs = rv.committee_pubs(a.relay, a.id, list_miners())
                if not pubs:
                    print(f"[reveal] {a.id} aucun pair pour révéler {job_id} "
                          f"(ni clé ancrée on-chain ni cache relais — registre des mineurs vide ?)")
                    continue
                n = rv.reveal_job(a.relay, job_id, prompt, answer, pubs)
                print(f"[reveal] {a.id} a révélé {job_id} à {n} membre(s) du comité")
                done.add(job_id)
        except Exception as e:
            print(f"[reveal] {a.id} boucle: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
