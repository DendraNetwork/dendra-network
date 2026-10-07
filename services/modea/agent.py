"""Miner agent: registration plus the heartbeat loop towards the coordinator.

This is the miner-side node component of ADR-013: the miner declares itself to the coordinator and
maintains its liveness through regular beats. A miner that stops beating leaves the healthy set and
receives no more jobs. Only metadata is transmitted.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request


def _post(base: str, path: str, obj: dict) -> dict:
    body = json.dumps(obj).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def register(coord_base: str, *, miner_id: str, url: str, pubkey_hex: str,
            region: str, operator: str) -> None:
    _post(coord_base, "/register", {"miner_id": miner_id, "url": url, "pubkey_hex": pubkey_hex,
                                    "region": region, "operator": operator})


def settle(coord_base: str, *, job_id: str, miner_id: str, client_commit: str,
          result_commit: str, canary_commit: str = "") -> dict:
    """Settles a job: writes the commitments to the ledger and bumps reputation."""
    return _post(coord_base, "/settle", {"job_id": job_id, "miner_id": miner_id,
                                         "client_commit": client_commit,
                                         "result_commit": result_commit,
                                         "canary_commit": canary_commit})


class Heartbeat:
    """Background heartbeat loop (daemon thread)."""

    def __init__(self, coord_base: str, miner_id: str, interval: float = 5.0):
        self.coord_base = coord_base
        self.miner_id = miner_id
        self.interval = interval
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                _post(self.coord_base, "/heartbeat", {"miner_id": self.miner_id})
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self):
        # first beat sent immediately
        try:
            _post(self.coord_base, "/heartbeat", {"miner_id": self.miner_id})
        except Exception:
            pass
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
