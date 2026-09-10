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

import hashlib
import json
import sys
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


def query_all(subcmd, field, node_args, runner, t=90, max_pages=400):
    """COMPLETE listing of a paginated `query jobs <subcmd>`. Returns `None` when unreadable, never [].

    WHY THIS FUNCTION EXISTS, AND WHY IT LIVES HERE RATHER THAN BEING COPIED INTO EACH WORKER.
    `dendrad query jobs list-job` WITHOUT pagination stops at 100 entries: `ListJob` calls
    `query.CollectionPaginate` (query_job.go) and the SDK applies `DefaultLimit = 100` when the request
    carries Limit=0. Past the 100th job, the judge and reveal workers would stop seeing part of the
    audits — SILENTLY, and on the reassuring side: truncation can only produce GOOD news (fewer
    disputes, fewer pending audits), so the failure mode is indistinguishable from a healthy network.
    `client.list_all` already did this work; copying it into the workers would have created a
    third source of truth that drifts. It therefore lives in the module BOTH workers import.

    AND IT RETURNS `None`, NOT `[]`. Returning `[]` on unreadable output turns "I could not read" into
    "nothing to judge" for a `for job in list_jobs()` loop, and the worker sleeps believing it has
    worked. Not knowing is not the same as knowing there is nothing.
    """
    out, key, pages = [], None, 0
    while pages < max_pages:
        cmd = ["dendrad", "query", "jobs", subcmd, "--page-limit", "500", "--output", "json", *node_args]
        if key:
            cmd += ["--page-key", key]
        try:
            d = json.loads(runner(cmd, t=t) if _accepts_t(runner) else runner(cmd))
        except Exception:
            return None
        if not isinstance(d, dict) or not d:
            return None
        out.extend(d.get(field) or d.get(field.capitalize()) or [])
        pg = d.get("pagination") or {}
        key = pg.get("next_key") or pg.get("nextKey")
        pages += 1
        if not key:
            return out
    sys.stderr.write(f"[helpers] {subcmd}: >{max_pages} pages -> INCOMPLETE read, refusing to return a prefix\n")
    return None


def _accepts_t(fn):
    """The workers' `run()` does not have the same signature everywhere: inspect it, do not assume."""
    try:
        import inspect
        return "t" in inspect.signature(fn).parameters
    except Exception:
        return False


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


# The two primitives live in `modea.crypto`, where BOTH the miner (which computes the commitment
# while the plaintext is still inside its locked buffer) and this module can reach them without one
# importing the other. Re-exported here because the reveal contract is what carries the salt.
prompt_salt = crypto.prompt_salt
prompt_commitment = crypto.prompt_commitment


def reveal_job(relay_url: str, job_id: str, prompt: str, answer: str, target_pubs: dict[str, str],
               author_id: str, psalt: str = "") -> int:
    """Posts the sealed reveal (prompt+answer) to each committee member. Returns the number DELIVERED.

    DELIVERED, NOT NEWLY WRITTEN -- and the difference decides whether a completed job looks failed.
    Every seal draws a fresh ephemeral key (`_seal_to`), so re-sealing the same content produces
    different bytes; the relay's write-once guard then answers 409 for a juror whose reveal is ALREADY
    stored. Counting that as a failure means a retry after a lost response -- or simply a second run of
    this worker -- reports fewer deliveries than there are readable reveals, and the caller concludes
    the reveal did not land while the audit can read it perfectly well.

    A juror whose slot already holds a sealed artifact HAS something to judge, which is what this
    count exists to measure. What it does not prove is WHO sealed it: the relay cannot yet establish
    that the writer is the job's primary, so a squatted slot would also count here. That gap is real
    and belongs to the relay, not to this counter -- and it is not an argument for going back to
    reporting stored reveals as missing.
    """
    # `psalt` travels WITH the reveal: it is what lets a juror recompute the commitment the primary
    # anchored. Absent -- an older primary -- the juror cannot verify the question, and must abstain
    # rather than judge a prompt nobody vouched for.
    obj = {"prompt": prompt, "answer": answer}
    if psalt:
        obj["psalt"] = psalt
    n = 0
    for mid, pub in target_pubs.items():
        # ADR-045 (14) : LE DERNIER SEGMENT EST L AUTEUR, POUR TOUS LES GENRES SANS EXCEPTION.
        # The key was `<jid>__<recipient>`: SIGNED by the primary, KEYED by its reader. But the relay
        # attributes a deposit by taking the segment after the last `__` -- right for `res`, `pub` and
        # `attest`, wrong for a reveal. Measured under `enforce`: the primary signing ITS OWN reveal got
        # 401, the recipient signing the same deposit got 200.
        # Putting the author last makes the invariant UNIFORM instead of asking the relay for a
        # per-kind special case: a rule with no exception cannot forget one.
        key_ = f"{job_id}__{mid}__{author_id}"
        st = relay.put_status(relay_url, "reveal", key_, _seal_to(pub, obj))
        if st in ("ok", "exists"):
            n += 1
    return n


def open_reveal(relay_url: str, job_id: str, my_id: str, my_sk, author_id: str):
    """Committee side: fetches + decrypts the reveal addressed to me. None if missing/unreadable."""
    # The reader names the AUTHOR it expects, and that is a property rather than a constraint: a reveal
    # posted by anyone else no longer occupies the same key, so a squatter can no longer fill the slot
    # the primary was meant to fill.
    r = relay.get(relay_url, "reveal", f"{job_id}__{my_id}__{author_id}")
    if not r or "ct" not in r:
        return None
    try:
        key = crypto.derive_session_key(my_sk, bytes.fromhex(r["client_eph_pk"]), info=REVEAL_INFO)
        pt = crypto.decrypt(key, crypto.Sealed(bytes.fromhex(r["nonce"]), bytes.fromhex(r["ct"])))
        crypto.zeroize(bytearray(key))
        return json.loads(pt)
    except Exception:
        return None
