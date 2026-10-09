"""Integration tests for the hardening of the OpenAI-compatible gateway.

The real gateway is started on an ephemeral port, and the four properties that keep it safe to expose are
checked end to end: a Bearer token is mandatory (401 without, 200 with), /health stays public so a probe
does not need a credential, the request body is capped (413), and CORS is closed by default and limited
to exactly one configured origin when set. Each of these fails open if it regresses, which is why they
are asserted against a live server rather than against the handler in isolation.
"""
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
        assert _req(port, "/health")[0] == 200                                    # health stays public
        assert _req(port, "/v1/models")[0] == 401                                 # no Bearer -> 401
        assert _req(port, "/v1/models", headers={"Authorization": "Bearer sk-test"})[0] == 200
        assert _req(port, "/v1/models", headers={"Authorization": "Bearer WRONG"})[0] == 401
        assert _req(port, "/v1/chat/completions", "POST", body=b"{}")[0] == 401    # POST without Bearer -> 401
        big = b'{"messages":"' + b"a" * 300 + b'"}'
        code, _ = _req(port, "/v1/chat/completions", "POST",
                       headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"}, body=big)
        assert code == 413, f"a body over MAX_BODY must return 413, got {code}"
    finally:
        srv.shutdown()


def test_cors_off_by_default():
    srv, port = _boot({"DENDRA_API_KEY": None, "DENDRA_CORS_ORIGIN": None, "DENDRA_MAX_BODY": None})
    try:
        _, hdrs = _req(port, "/health")
        assert "Access-Control-Allow-Origin" not in hdrs, "no CORS header may be emitted by default"
    finally:
        srv.shutdown()


def test_cors_restricted_when_set():
    srv, port = _boot({"DENDRA_API_KEY": None, "DENDRA_CORS_ORIGIN": "http://localhost:8080"})
    try:
        _, hdrs = _req(port, "/health")
        assert hdrs.get("Access-Control-Allow-Origin") == "http://localhost:8080", \
            "CORS must be limited to exactly the configured origin"
    finally:
        srv.shutdown()
