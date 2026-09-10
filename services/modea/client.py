"""Mode A client.

Generates an EPHEMERAL key per job (forward secrecy), encrypts the prompt to the miner's attested
identity key, submits it, then decrypts the answer. It keeps no state between jobs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import crypto
from .crypto import Sealed


@dataclass
class Submission:
    job_id: str
    client_eph_pk: bytes
    sealed_prompt: Sealed
    client_commit: str        # hash of the encrypted request (the on-chain commitment)


class Client:
    def submit(self, job_id: str, miner_pub: bytes, prompt: str) -> tuple[Submission, bytes]:
        """Returns (submission, session_key); the key stays client-side to decrypt the answer."""
        aad = job_id.encode()
        eph_sk, eph_pk = crypto.gen_keypair()             # EPHEMERAL
        key = crypto.derive_session_key(eph_sk, miner_pub, info=aad)
        sealed = crypto.encrypt(key, prompt.encode("utf-8"), aad=aad)
        commit = hashlib.sha256(sealed.nonce + sealed.ct).hexdigest()
        sub = Submission(job_id=job_id, client_eph_pk=eph_pk, sealed_prompt=sealed,
                         client_commit=commit)
        return sub, key

    def open_result(self, job_id: str, key: bytes, sealed_result: Sealed) -> str:
        return crypto.decrypt(key, sealed_result, aad=job_id.encode()).decode("utf-8")
