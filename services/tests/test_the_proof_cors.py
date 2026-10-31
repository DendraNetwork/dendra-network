"""Bench of The Proof's CORS answer: which origin a browser page may read the feed from.

The feed is read by pages on four origins: the site's two hosts (the bare domain and www.), the chat (app.) and
the explorer (explorer.). Every case drives the shipped `Handler` on a loopback port, and the last tests read the
default that docker-compose.yml ships, so a default that leaves out one reader fails here and not only in a browser.
"""
import importlib
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

# DENDRA_MODEA_DIR points this bench at a copy of the modules (a mutation bench); DENDRA_REPO names the tree that
# carries docker-compose.yml when the tests run away from it (the battery runs pytest from a copy of prototype/).
MODEA_DIR = os.environ.get("DENDRA_MODEA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isfile(os.path.join(MODEA_DIR, "the_proof.py")):
    raise FileNotFoundError(f"{MODEA_DIR}: the_proof.py is not there, so this bench has no subject")
sys.path.insert(0, MODEA_DIR)
REPO = os.environ.get("DENDRA_REPO") or os.path.dirname(os.path.dirname(MODEA_DIR))

SITE = ("https://dendranetwork.com", "https://www.dendranetwork.com")
# Every origin whose pages read the feed: the site's two hosts, the chat (app.) and the explorer (explorer.).
READERS = SITE + ("https://app.dendranetwork.com", "https://explorer.dendranetwork.com")
OUTSIDE = ("https://evil.example", "https://dendranetwork.com.evil.example", "https://sub.dendranetwork.com",
           "https://www.app.dendranetwork.com", "https://www.explorer.dendranetwork.com", "http://app.dendranetwork.com",
           "http://explorer.dendranetwork.com", "http://dendranetwork.com", "https://app.dendranetwork.com:8443",
           "https://explorer.dendranetwork.com.evil.example", "null",
           # Compared byte for byte, as Caddy does on /demo: another case or a trailing slash is not a reader.
           "HTTPS://APP.DENDRANETWORK.COM", "https://App.dendranetwork.com", "https://app.dendranetwork.com/")


@pytest.fixture()
def tp():
    return importlib.import_module("the_proof")


def _headers(tp, monkeypatch, raw, origin):
    monkeypatch.setattr(tp, "CORS_ORIGINS", tp.cors_origins(raw))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), tp.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/health")
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers
    finally:
        srv.shutdown()
        srv.server_close()


def test_no_setting_sends_no_header(tp, monkeypatch):
    code, h = _headers(tp, monkeypatch, "", SITE[0])
    assert code == 200
    assert h.get("Access-Control-Allow-Origin") is None
    assert h.get("Vary") is None


@pytest.mark.parametrize("origin", READERS)
def test_each_listed_origin_gets_its_own_origin_back(tp, monkeypatch, origin):
    _, h = _headers(tp, monkeypatch, ",".join(READERS), origin)
    assert h.get("Access-Control-Allow-Origin") == origin
    assert h.get("Vary") == "Origin"


@pytest.mark.parametrize("origin", OUTSIDE + (None,))
def test_an_origin_outside_the_list_gets_no_header(tp, monkeypatch, origin):
    _, h = _headers(tp, monkeypatch, ",".join(READERS), origin)
    assert h.get("Access-Control-Allow-Origin") is None
    assert h.get("Vary") == "Origin"


def test_a_single_origin_setting_keeps_working(tp, monkeypatch):
    _, h = _headers(tp, monkeypatch, "https://dendranetwork.com", "https://dendranetwork.com")
    assert h.get("Access-Control-Allow-Origin") == "https://dendranetwork.com"


def test_a_star_setting_answers_star_without_vary(tp, monkeypatch):
    _, h = _headers(tp, monkeypatch, "*", "https://anything.example")
    assert h.get("Access-Control-Allow-Origin") == "*"
    assert h.get("Vary") is None


def test_spaces_and_empty_items_are_dropped(tp):
    assert tp.cors_origins(" https://a.example , ,https://b.example,") == ("https://a.example", "https://b.example")
    assert tp.cors_origins(None) == ()


# Run in a child process: the module reads DENDRA_PROOF_CORS once, when it is loaded, and the cases above
# replace the parsed value. This one proves the variable itself reaches the server.
_CHILD = """
import sys, threading, urllib.request
from http.server import ThreadingHTTPServer
import the_proof as tp
srv = ThreadingHTTPServer(("127.0.0.1", 0), tp.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/health" % srv.server_address[1]
with urllib.request.urlopen(urllib.request.Request(url, headers={"Origin": sys.argv[1]}), timeout=5) as r:
    print("allow=%s" % r.headers.get("Access-Control-Allow-Origin"))
srv.shutdown()
"""


def _child_allow(env_value, origin):
    env = {k: v for k, v in os.environ.items() if k != "DENDRA_PROOF_CORS"}
    if env_value is not None:
        env["DENDRA_PROOF_CORS"] = env_value
    env["PYTHONPATH"] = os.pathsep.join(p for p in (MODEA_DIR, env.get("PYTHONPATH")) if p)
    out = subprocess.run([sys.executable, "-c", _CHILD, origin], cwd=MODEA_DIR, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("allow=")]
    assert len(lines) == 1, out.stdout[-2000:]
    return lines[0][len("allow="):]


def test_the_variable_is_read_when_the_module_loads():
    assert _child_allow(",".join(SITE), SITE[1]) == SITE[1]
    assert _child_allow(None, SITE[1]) == "None"


def _compose_default():
    with open(os.path.join(REPO, "docker-compose.yml"), encoding="utf-8") as f:
        text = f.read()
    marker = 'DENDRA_PROOF_CORS: "${DENDRA_PROOF_CORS:-'
    assert text.count(marker) == 1, f"expected one DENDRA_PROOF_CORS default in docker-compose.yml, found {text.count(marker)}"
    return text.split(marker, 1)[1].split("}", 1)[0]


def _default_failures(tp, default):
    """What a DENDRA_PROOF_CORS value gets wrong: a reader whose origin is not echoed, or an outside origin that
    gets a header. Empty when the value serves exactly the readers."""
    allowed = tp.cors_origins(default)
    bad = [f"{o} not echoed" for o in READERS if tp.cors_allow(allowed, o) != o]
    return bad + [f"{o} answered {tp.cors_allow(allowed, o)!r}" for o in OUTSIDE if tp.cors_allow(allowed, o) is not None]


def test_the_compose_default_lets_every_reader_read_the_feed(tp):
    assert _default_failures(tp, _compose_default()) == []


@pytest.mark.parametrize("dropped", READERS)
def test_a_compose_default_without_one_reader_is_seen(tp, dropped):
    # The shipped default with one origin taken out: the check above must see the reader it leaves out.
    shipped = tp.cors_origins(_compose_default())
    assert dropped in shipped
    mutant = ",".join(o for o in shipped if o != dropped)
    assert _default_failures(tp, mutant) == [f"{dropped} not echoed"]
