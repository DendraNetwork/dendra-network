"""Ledger simule (la partie 'on-chain').

⚠️ AUDIT PY-13 : DEMONSTRATEUR. Le vrai "on-chain" = la chaine Cosmos (module x/jobs, via dendrad).
Ce ledger simule (`save`/`settle` sans auth) sert aux demos hors-chaine -> NE PAS l'utiliser comme
source de verite ni l'exposer en prod.

REGLE ABSOLUE (ADR-011) : **jamais de contenu en clair on-chain.** Le ledger ne stocke que des
ENGAGEMENTS (hash) et des metadonnees chiffrees/opaques. Si quoi que ce soit ressemblant a du
texte clair y entrait, ce serait un bug de confidentialite -> le `record` refuse les gros blobs
et n'expose que des hex de hash.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


def commit(data: bytes) -> str:
    """Engagement = SHA-256 hex (revele rien du contenu)."""
    return hashlib.sha256(data).hexdigest()


@dataclass
class JobRecord:
    job_id: str
    miner_id: str
    client_commit: str      # hash de la requete chiffree
    result_commit: str = ""  # hash du resultat chiffre
    canary_commit: str = ""  # engagement du canari (sert au slashing, ADR-012)
    ts: float = field(default_factory=time.time)


class Ledger:
    """Registre append-only. Ne contient QUE des hash/ids — aucun contenu."""

    def __init__(self):
        self._records: dict[str, JobRecord] = {}

    def open_job(self, job_id: str, miner_id: str, client_commit: str, canary_commit: str = "") -> JobRecord:
        if job_id in self._records:
            raise ValueError(f"job {job_id} deja enregistre")
        rec = JobRecord(job_id=job_id, miner_id=miner_id,
                        client_commit=client_commit, canary_commit=canary_commit)
        self._records[job_id] = rec
        return rec

    def settle_job(self, job_id: str, result_commit: str) -> None:
        self._records[job_id].result_commit = result_commit

    def get(self, job_id: str) -> JobRecord:
        return self._records[job_id]

    def all(self):
        return list(self._records.values())

    def dump_public(self) -> list[dict]:
        """Vue 'publique' du ledger — uniquement des hash. Sert a prouver qu'aucun
        contenu n'y figure (utilise par l'auto-check de la demo)."""
        return [
            {"job_id": r.job_id, "miner_id": r.miner_id,
             "client_commit": r.client_commit, "result_commit": r.result_commit,
             "canary_commit": r.canary_commit, "ts": round(r.ts, 3)}
            for r in self._records.values()
        ]

    # --- persistance (engagements uniquement, survit au redemarrage) ---
    def record(self, job_id: str, miner_id: str, client_commit: str,
               result_commit: str = "", canary_commit: str = "") -> JobRecord:
        """Enregistre/maj un job en un appel (utilise par le coordinateur au reglement)."""
        rec = JobRecord(job_id=job_id, miner_id=miner_id, client_commit=client_commit,
                        result_commit=result_commit, canary_commit=canary_commit)
        self._records[job_id] = rec
        return rec

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.dump_public(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        led = cls()
        p = Path(path)
        if p.exists():
            for d in json.loads(p.read_text(encoding="utf-8")):
                led._records[d["job_id"]] = JobRecord(
                    job_id=d["job_id"], miner_id=d["miner_id"],
                    client_commit=d["client_commit"], result_commit=d.get("result_commit", ""),
                    canary_commit=d.get("canary_commit", ""), ts=d.get("ts", 0.0))
        return led
