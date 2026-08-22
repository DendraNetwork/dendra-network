"""Simulated ledger — the stand-in for the "on-chain" side.

DEMONSTRATOR ONLY. The real on-chain state is the Cosmos chain (module x/jobs, through dendrad).
This ledger simulates it with no authentication on `save`/`settle`; it exists for off-chain demos and
must never be used as a source of truth or exposed in production.

ABSOLUTE RULE (ADR-011): no cleartext content ever goes on-chain. The ledger stores only COMMITMENTS
(hashes) and encrypted or opaque metadata. Anything resembling cleartext reaching it would be a
confidentiality bug, so `record` refuses large blobs and exposes hex digests only.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


def commit(data: bytes) -> str:
    """Commitment = SHA-256 hex; it reveals nothing about the content."""
    return hashlib.sha256(data).hexdigest()


@dataclass
class JobRecord:
    job_id: str
    miner_id: str
    client_commit: str      # hash of the encrypted request
    result_commit: str = ""  # hash of the encrypted result
    canary_commit: str = ""  # canary commitment (feeds slashing, ADR-012)
    ts: float = field(default_factory=time.time)


class Ledger:
    """Append-only registry. Holds ONLY hashes and ids — never content."""

    def __init__(self):
        self._records: dict[str, JobRecord] = {}

    def open_job(self, job_id: str, miner_id: str, client_commit: str, canary_commit: str = "") -> JobRecord:
        if job_id in self._records:
            raise ValueError(f"job {job_id} already recorded")
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
        """The ledger's public view: hashes only. It exists to demonstrate that no content is
        present, and is what the demo's self-check reads."""
        return [
            {"job_id": r.job_id, "miner_id": r.miner_id,
             "client_commit": r.client_commit, "result_commit": r.result_commit,
             "canary_commit": r.canary_commit, "ts": round(r.ts, 3)}
            for r in self._records.values()
        ]

    # --- persistence: commitments only, surviving a restart ---
    def record(self, job_id: str, miner_id: str, client_commit: str,
               result_commit: str = "", canary_commit: str = "") -> JobRecord:
        """Records or updates a job in one call; used by the coordinator at settlement."""
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
