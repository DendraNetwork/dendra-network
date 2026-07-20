"""Client reseau Mode A : parle au serveur mineur en HTTP.

Le prompt est chiffre AVANT de partir sur le fil ; on expose aussi les **octets reels de la
requete reseau** pour pouvoir prouver qu'aucun plaintext n'y figure.
Zero dependance (urllib stdlib).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from .client import Client
from .crypto import Sealed


@dataclass
class NetResult:
    output: str
    request_bytes: bytes      # ce qui est REELLEMENT parti sur le reseau
    client_commit: str
    result_commit: str


class NetClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = Client()

    def fetch_pubkey(self) -> tuple[str, bytes]:
        with urllib.request.urlopen(f"{self.base_url}/pubkey", timeout=30) as r:
            d = json.loads(r.read().decode())
        return d["miner_id"], bytes.fromhex(d["pubkey_hex"])

    def submit(self, job_id: str, prompt: str) -> NetResult:
        _, miner_pub = self.fetch_pubkey()
        sub, key = self._client.submit(job_id, miner_pub, prompt)
        body = json.dumps({
            "job_id": job_id,
            "client_eph_pk": sub.client_eph_pk.hex(),
            "nonce": sub.sealed_prompt.nonce.hex(),
            "ct": sub.sealed_prompt.ct.hex(),
        }).encode()
        req = urllib.request.Request(f"{self.base_url}/job", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode())
        sealed = Sealed(nonce=bytes.fromhex(resp["nonce"]), ct=bytes.fromhex(resp["ct"]))
        out = self._client.open_result(job_id, key, sealed)
        return NetResult(output=out, request_bytes=body,
                         client_commit=sub.client_commit, result_commit=resp["result_commit"])
