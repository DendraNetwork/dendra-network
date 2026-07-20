"""Tests d'intégration du durcissement de la passerelle OpenAI->Dendra (audit PY-01/11 ; étape 2 plan).
Démarre la vraie passerelle sur un port éphémère et vérifie : Bearer obligatoire (401/200), health public,
cap de corps (413), et CORS restreint (vide par défaut, origine précise sinon)."""
import importlib
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _boot(env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import gateway
    importlib.reload(gateway)
    srv = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return srv, port


def _req(port, path, method="GET", headers=None, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def test_bearer_required_and_caps():
    srv, port = _boot({"DENDRA_API_KEY": "sk-test", "DENDRA_MAX_BODY": "100", "DENDRA_CORS_ORIGIN": None})
    try:
        assert _req(port, "/health")[0] == 200                                    # health public
        assert _req(port, "/v1/models")[0] == 401                                 # sans Bearer -> 401
        assert _req(port, "/v1/models", headers={"Authorization": "Bearer sk-test"})[0] == 200
        assert _req(port, "/v1/models", headers={"Authorization": "Bearer WRONG"})[0] == 401
        assert _req(port, "/v1/chat/completions", "POST", body=b"{}")[0] == 401    # POST sans Bearer -> 401
        big = b'{"messages":"' + b"a" * 300 + b'"}'
        code, _ = _req(port, "/v1/chat/completions", "POST",
                       headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"}, body=big)
        assert code == 413, f"corps > MAX_BODY doit donner 413, eu {code}"
    finally:
        srv.shutdown()


def test_cors_off_by_default():
    srv, port = _boot({"DENDRA_API_KEY": None, "DENDRA_CORS_ORIGIN": None, "DENDRA_MAX_BODY": None})
    try:
        _, hdrs = _req(port, "/health")
        assert "Access-Control-Allow-Origin" not in hdrs, "aucun CORS par défaut"
    finally:
        srv.shutdown()


def test_cors_restricted_when_set():
    srv, port = _boot({"DENDRA_API_KEY": None, "DENDRA_CORS_ORIGIN": "http://localhost:8080"})
    try:
        _, hdrs = _req(port, "/health")
        assert hdrs.get("Access-Control-Allow-Origin") == "http://localhost:8080", "CORS limité à l'origine configurée"
    finally:
        srv.shutdown()
