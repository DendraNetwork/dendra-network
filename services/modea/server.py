"""Serveur mineur HTTP (transport reseau reel).

⚠️ AUDIT PY-13 / CR-02 : ce serveur (et son endpoint /mpc_linear) est un DEMONSTRATEUR, PAS le chemin
de prod. Le chemin de prod = passerelle OpenAI -> client -> dendrad (chaine). /mpc_linear
accepte une part SANS auth ni engagement -> ne JAMAIS l'exposer en prod tel quel.

Expose le mineur sur le reseau, mais ne recoit/renvoie que du **chiffre** :
  GET  /pubkey  -> {miner_id, pubkey_hex}        (cle d'identite attestee)
  POST /job     -> {job_id, client_eph_pk, nonce, ct}
                -> {result_commit, nonce, ct}     (sortie re-chiffree)

Aucun contenu n'est journalise (log_message neutralise). Le clair n'existe qu'en RAM dans
handle_job (qui zeroise). Zero dependance (stdlib http.server).
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import mpc
from .crypto import Sealed
from .miner import Miner


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/pubkey":
            m: Miner = self.server.miner  # type: ignore[attr-defined]
            self._send(200, {"miner_id": m.miner_id, "pubkey_hex": m.pub.hex()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode()) if n else {}
        m: Miner = self.server.miner  # type: ignore[attr-defined]

        if self.path == "/job":  # Mode A : inference confidentielle chiffree
            sealed = Sealed(nonce=bytes.fromhex(req["nonce"]), ct=bytes.fromhex(req["ct"]))
            try:
                res = m.handle_job(req["job_id"], bytes.fromhex(req["client_eph_pk"]), sealed)
            except Exception as e:  # ne jamais divulguer de contenu dans l'erreur
                self._send(500, {"error": type(e).__name__})
                return
            self._send(200, {"result_commit": res.result_commit,
                             "nonce": res.sealed_result.nonce.hex(),
                             "ct": res.sealed_result.ct.hex()})
            return

        if self.path == "/mpc_linear":  # Mode B : calcul local sur UNE part (entree jamais vue)
            try:
                result = mpc.linear_local(req["weight"], req["share"])
            except Exception as e:
                self._send(500, {"error": type(e).__name__})
                return
            self._send(200, {"result": result})
            return

        self._send(404, {"error": "not found"})

    def log_message(self, *args):  # neutralise les logs d'acces (pas de fuite de metadata)
        pass


def make_server(miner: Miner, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.miner = miner  # type: ignore[attr-defined]
    return srv


def serve(miner: Miner, host: str = "127.0.0.1", port: int = 8745):
    srv = make_server(miner, host, port)
    print(f"[miner] {miner.miner_id} en ecoute sur http://{host}:{port} (backend={miner.backend.name})")
    srv.serve_forever()
