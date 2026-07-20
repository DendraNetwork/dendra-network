"""Agent mineur : enregistrement + boucle de heartbeat vers le coordinateur.

C'est la partie "node" de l'ADR-013 cote mineur : le mineur se declare au coordinateur et
maintient sa liveness par des battements reguliers (sinon il sort du set sain -> plus de jobs).
Ne transmet que des metadonnees.
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
    """Reglement d'un job : ecrit les engagements au ledger + bump reputation."""
    return _post(coord_base, "/settle", {"job_id": job_id, "miner_id": miner_id,
                                         "client_commit": client_commit,
                                         "result_commit": result_commit,
                                         "canary_commit": canary_commit})


class Heartbeat:
    """Boucle de heartbeat en arriere-plan (thread daemon)."""

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
        # premier battement immediat
        try:
            _post(self.coord_base, "/heartbeat", {"miner_id": self.miner_id})
        except Exception:
            pass
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
