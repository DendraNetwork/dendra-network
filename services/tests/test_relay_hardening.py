"""Tests d'intégration du relais durci (audit v5) : auth par token (MM-01) + rate-limit IP (MM-05).
Démarre un vrai ThreadingHTTPServer sur un port éphémère et fait de vraies requêtes HTTP."""
import importlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _boot(env):
    """(Re)charge relay avec un env donné et démarre le serveur sur un port éphémère."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import relay
    importlib.reload(relay)
    srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return srv, port


def _req(port, path, method="GET", headers=None, body=None):
    url = f"http://127.0.0.1:{port}/{path}"
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_token_auth_required_when_configured():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": "s3cret", "DENDRA_RELAY_RATE": "1000"})
    try:
        # POST /pub sans token -> 401
        code, _ = _req(port, "pub/m1", "POST", body=json.dumps({"pub": "ab"}).encode())
        assert code == 401, f"attendu 401 sans token, eu {code}"
        # avec le bon token -> 200
        code, _ = _req(port, "pub/m1", "POST", headers={"X-Dendra-Token": "s3cret"},
                       body=json.dumps({"pub": "ab"}).encode())
        assert code == 200, f"attendu 200 avec token, eu {code}"
        # /list sans token -> 401 (MM-03 : métadonnées gardées)
        code, _ = _req(port, "list")
        assert code == 401, f"attendu 401 sur /list sans token, eu {code}"
    finally:
        srv.shutdown()


def test_no_token_backward_compatible():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": None, "DENDRA_RELAY_RATE": "1000"})
    try:
        # pas de token configuré -> local OK sans en-tête (rétro-compat)
        code, _ = _req(port, "list")
        assert code == 200, f"attendu 200 en mode local, eu {code}"
    finally:
        srv.shutdown()


def test_rate_limit_blocks_flood():
    srv, port = _boot({"DENDRA_RELAY_TOKEN": None, "DENDRA_RELAY_RATE": "5", "DENDRA_RELAY_WINDOW": "100"})
    try:
        codes = [_req(port, "list")[0] for _ in range(12)]
        assert 429 in codes, f"attendu un 429 sous flood, eu {codes}"
        assert codes[:5] == [200] * 5, f"les 5 premières devraient passer, eu {codes[:5]}"
    finally:
        srv.shutdown()
