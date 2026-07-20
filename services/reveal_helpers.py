"""Reveal to the fresh committee for an AUDITED job (`+disputed`).

When an optimistic job (k=1) is drawn for audit, the chain moves it to `+disputed`.
The PRIMARY miner must then REVEAL (prompt + its answer) to the fresh committee for judging -- but
SEALED (X25519) to each member, never in cleartext (the relay is hostile). This module holds that
logic, imported by `miner.py` (RAM cache + reveal pass in the loop).

Confidentiality: cleartext is kept only IN MEMORY, bounded in time (TTL) and in count, zeroized on
purge. Never on disk. Revealing exposes content only for the ~10% audited (an accepted trade-off).

Crypto = real API of `modea.crypto` (ECDH X25519 + HKDF + AES-256-GCM), verified.
"""
from __future__ import annotations

import json
import re
import time

from modea import crypto
import relay_client as relay

# Derivation domain specific to reveal (independent of the client->miner channel).
REVEAL_INFO = b"dendra/reveal/v1"

# Defense in depth: a job_id comes from the CHAIN (potentially adversarial data)
# and ends up in `dendrad` ARGV and as a relay key. We accept only a flat
# identifier -- alphanum + . _ : -, NEVER starting with '-' (anti flag-injection),
# capped at 128. No shell involved (subprocess as a list), but we close the vector anyway.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def safe_job_id(s) -> bool:
    """True if `s` is a job_id safe to pass as argv / relay key (see _JOB_ID_RE)."""
    return isinstance(s, str) and bool(_JOB_ID_RE.match(s))


class JobCache:
    """Bounded IN-MEMORY cache `jobId -> (prompt, answer)` so we can reveal if the job is audited.
    Plaintext in RAM only (never disk), purged by TTL and bounded by `max_items`."""

    def __init__(self, max_items: int = 256, ttl: float = 3600.0):
        self._d: dict[str, list] = {}   # jobId -> [prompt, answer, ts]
        self.max_items = max_items
        self.ttl = ttl

    def put(self, job_id: str, prompt: str, answer: str) -> None:
        self._gc()
        self._d[job_id] = [prompt, answer, time.time()]
        while len(self._d) > self.max_items:
            oldest = min(self._d.items(), key=lambda kv: kv[1][2])[0]
            self._zap(oldest)

    def get(self, job_id: str):
        v = self._d.get(job_id)
        return (v[0], v[1]) if v else None

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, v in self._d.items() if now - v[2] > self.ttl]:
            self._zap(k)

    def _zap(self, k: str) -> None:
        v = self._d.pop(k, None)
        if v:   # best-effort zeroization of the cleartext
            try:
                v[0] = "\x00" * len(v[0])
                v[1] = "\x00" * len(v[1])
            except Exception:
                pass


def _seal_to(pub_hex: str, obj: dict) -> dict:
    """Seals `obj` (JSON) to an X25519 pub (hex 32B) with an ephemeral key -> forward secrecy.
    Returns the transportable dict {client_eph_pk, nonce, ct} (same fields as the req channel)."""
    eph_sk, eph_pk = crypto.gen_keypair()
    key = crypto.derive_session_key(eph_sk, bytes.fromhex(pub_hex), info=REVEAL_INFO)
    sealed = crypto.encrypt(key, json.dumps(obj).encode())
    crypto.zeroize(bytearray(key))
    return {"client_eph_pk": eph_pk.hex(), "nonce": sealed.nonce.hex(), "ct": sealed.ct.hex()}


def committee_pubs(relay_url: str, my_id: str, miners) -> dict[str, str]:
    """X25519 pubs of the OTHER miners (reveal targets).

    SOURCE OF TRUTH = the ON-CHAIN ANCHORED pub (`enc_pubkey` of the miner registry) — the very key the
    client already encrypts to, so a pub substituted at the relay is ineffective here too (it used to be
    the one hole left in the reveal path, while `client.submit_job` already refused the relay pub).

    It also removes a failure mode that puts an honest miner's stake at risk: the relay keeps `pub/<mid>` in MEMORY
    (relay.py STORE), so restarting the relay wiped every published key; miners were then
    structurally UNABLE to seal a reveal, and the committee charged them for that infrastructure fault.
    Reading the anchor makes the reveal path survive any relay restart.

    `miners` accepts registry records ({miner_id, enc_pubkey}) or a legacy list of ids. The volatile
    relay cache is kept ONLY as a fallback, for a miner that has not anchored a key yet.
    """
    pubs: dict[str, str] = {}
    for m in miners or []:
        if isinstance(m, dict):
            mid = m.get("miner_id") or m.get("id") or ""
            onchain = (m.get("enc_pubkey") or "").strip()
        else:
            mid, onchain = str(m or ""), ""
        if not mid or mid == my_id:
            continue
        if onchain:
            pubs[mid] = onchain          # anchored on-chain: authoritative, restart-proof
            continue
        r = relay.get(relay_url, "pub", mid)   # fallback: volatile relay cache
        if r and r.get("pub"):
            pubs[mid] = r["pub"]
    return pubs


def reveal_job(relay_url: str, job_id: str, prompt: str, answer: str, target_pubs: dict[str, str]) -> int:
    """Posts the sealed reveal (prompt+answer) to each committee member. Returns the number posted."""
    obj = {"prompt": prompt, "answer": answer}
    n = 0
    for mid, pub in target_pubs.items():
        if relay.put(relay_url, "reveal", f"{job_id}__{mid}", _seal_to(pub, obj)):
            n += 1
    return n


def open_reveal(relay_url: str, job_id: str, my_id: str, my_sk):
    """Committee side: fetches + decrypts the reveal addressed to me. None if missing/unreadable."""
    r = relay.get(relay_url, "reveal", f"{job_id}__{my_id}")
    if not r or "ct" not in r:
        return None
    try:
        key = crypto.derive_session_key(my_sk, bytes.fromhex(r["client_eph_pk"]), info=REVEAL_INFO)
        pt = crypto.decrypt(key, crypto.Sealed(bytes.fromhex(r["nonce"]), bytes.fromhex(r["ct"])))
        crypto.zeroize(bytearray(key))
        return json.loads(pt)
    except Exception:
        return None
