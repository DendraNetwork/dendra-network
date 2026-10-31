"""The /demo block of docker/Caddyfile, run by a real Caddy against a local recorder.

/demo injects the project's key, so the gateway cannot tell who sent a request. The origin echo of that
block only decides whether a browser lets a page read the response; the 403 on a foreign Origin is what
stops a third-party page from making its visitors' browsers send paid requests. These cases extract the
shipped block from the file, change only its upstream address (to a recorder on loopback), run it, and
check what reaches the upstream. A case that removes the 403 line checks that this bench sees the hole, and
one that removes app. (the chat's origin) checks that it sees the chat cut off.

Caddy is found on PATH, or named by DENDRA_CADDY. Without it the cases are skipped (declared, not passed),
unless DENDRA_REQUIRE_CADDY=1: then a missing binary or file fails every case. Any other value than "", "0"
or "1" is an error.
"""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# DENDRA_MODEA_DIR points this bench at a copy of services; the Caddyfile is found from there or from DENDRA_REPO.
MODEA = Path(os.environ.get("DENDRA_MODEA_DIR") or Path(__file__).resolve().parents[1])
SITE = ("https://dendranetwork.com", "https://www.dendranetwork.com")
# The chat is served by app. and posts to /demo. The explorer (explorer.) only reads public routes, so /demo
# refuses it like any other origin.
APP = "https://app.dendranetwork.com"
ADMITTED = SITE + (APP,)
UPSTREAM = "gateway:8651"
KEY = "sk-bench-demo"


def _repo_root():
    """The tree that carries docker/Caddyfile. DENDRA_REPO names it when this directory runs away from it (the
    battery runs pytest from a copy of prototype/). A DENDRA_REPO without the file fails: a measurement was
    asked for and cannot be made."""
    named = os.environ.get("DENDRA_REPO") or ""
    if named:
        assert (Path(named) / "docker" / "Caddyfile").is_file(), "DENDRA_REPO=%s has no docker/Caddyfile" % named
        return Path(named)
    for d in [MODEA, *MODEA.parents]:
        if (d / "docker" / "Caddyfile").is_file():
            return d
    return None


def _caddy():
    named = os.environ.get("DENDRA_CADDY") or ""
    if named:
        assert os.access(named, os.X_OK), "DENDRA_CADDY=%s is not an executable" % named
        return named
    return shutil.which("caddy")


def _required():
    v = os.environ.get("DENDRA_REQUIRE_CADDY") or ""
    assert v in ("", "0", "1"), "DENDRA_REQUIRE_CADDY=%r: expected 0 or 1" % v
    return v == "1"


ROOT = _repo_root()
CADDY = _caddy()
MISSING = ROOT is None or CADDY is None
REQUIRED = _required()
pytestmark = pytest.mark.skipif(
    MISSING and not REQUIRED,
    reason="docker/Caddyfile or the caddy binary is not found: the /demo origin cases are not run "
           "(declared, not passed); DENDRA_REPO names the tree, DENDRA_CADDY the binary, "
           "DENDRA_REQUIRE_CADDY=1 turns this skip into a failure")


@pytest.fixture(autouse=True)
def _caddy_present():
    if MISSING:
        pytest.fail("DENDRA_REQUIRE_CADDY=1 but docker/Caddyfile or the caddy binary is not found")


def _demo_block():
    """The shipped `handle_path /demo/* { ... }` block, verbatim, found by brace depth."""
    lines = (ROOT / "docker" / "Caddyfile").read_text(encoding="utf-8").split("\n")
    starts = [i for i, l in enumerate(lines) if l.strip() == "handle_path /demo/* {"]
    assert len(starts) == 1, "expected one /demo block, found %d" % len(starts)
    depth, out = 0, []
    for l in lines[starts[0]:]:
        code = l.split("#", 1)[0]
        depth += code.count("{") - code.count("}")
        out.append(l)
        if depth == 0:
            return "\n".join(out)
    raise AssertionError("the /demo block never closes")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Recorder(BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.server.hits.append((self.command, self.path, self.headers.get("Authorization")))
        b = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    do_GET = do_POST = do_OPTIONS = _any

    def log_message(self, *a):
        pass


@pytest.fixture
def demo(tmp_path):
    """Yields a function that starts Caddy on the given block and returns (port, upstream hits)."""
    started = []

    def start(block):
        up = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        up.hits = []
        threading.Thread(target=up.serve_forever, daemon=True).start()
        assert block.count(UPSTREAM) == 1, "the block must name its upstream once"
        block = block.replace(UPSTREAM, "127.0.0.1:%d" % up.server_address[1])
        port = _free_port()
        cfg = tmp_path / ("bench-%d.caddyfile" % len(started))
        cfg.write_text("{\n\tadmin off\n\tauto_https off\n\tpersist_config off\n}\n"
                       "http://127.0.0.1:%d {\n%s\n}\n" % (port, block), encoding="utf-8")
        env = dict(os.environ, DENDRA_API_KEY=KEY, HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path / "data"),
                   XDG_CONFIG_HOME=str(tmp_path / "config"))
        proc = subprocess.Popen([CADDY, "run", "--adapter", "caddyfile", "--config", str(cfg)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        started.append((proc, up))
        for _ in range(100):
            if proc.poll() is not None:
                raise AssertionError("caddy exited: " + proc.stdout.read().decode("utf-8", "replace")[-800:])
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return port, up.hits
            except OSError:
                time.sleep(0.1)
        raise AssertionError("caddy did not listen on %d" % port)

    yield start
    for proc, up in started:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        up.shutdown()


def _req(port, method, path, origin=None, content_type=None, extra=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict(extra or {})
    body = None
    if origin is not None:
        h["Origin"] = origin
    if method == "POST":
        body = b'{"messages":[{"role":"user","content":"hi"}]}'
        h["Content-Type"] = content_type or "application/json"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs


def _req_origins(port, origins):
    """A JSON POST carrying one Origin header per entry. A browser sends one; a script can send several."""
    body = b'{"messages":[{"role":"user","content":"hi"}]}'
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("POST", "/demo/v1/chat/completions")
    for o in origins:
        c.putheader("Origin", o)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs


# The test compares the value byte for byte: another case or a trailing slash is not an admitted origin, and a
# matcher that folds either one would admit a value no browser sends for the site.
FOREIGN = ("https://evil.example", "null", "https://dendranetwork.com.evil.example", "http://dendranetwork.com",
           "https://dendranetwork.com:8443", "https://sub.dendranetwork.com", "https://explorer.dendranetwork.com",
           "http://app.dendranetwork.com", "https://app.dendranetwork.com.evil.example", "https://www.app.dendranetwork.com",
           "https://app.dendranetwork.com:8443", "HTTPS://APP.DENDRANETWORK.COM", "https://App.dendranetwork.com",
           APP + "/")


def test_a_foreign_origin_is_refused_before_the_gateway(demo):
    port, hits = demo(_demo_block())
    for o in FOREIGN:
        for method, path, ct in (("POST", "/demo/v1/chat/completions", "text/plain"),
                                 ("POST", "/demo/v1/chat/completions", "application/json"),
                                 ("GET", "/demo/v1/models", None)):
            code, h = _req(port, method, path, origin=o, content_type=ct)
            assert code == 403, "%s %s from %s: want 403, got %d" % (method, path, o, code)
            assert "access-control-allow-origin" not in h, "a refused origin got a CORS header: %r" % o
        code, h = _req(port, "OPTIONS", "/demo/v1/chat/completions", origin=o,
                       extra={"Access-Control-Request-Method": "POST",
                              "Access-Control-Request-Headers": "content-type"})
        assert code == 403, "preflight from %s: want 403, got %d" % (o, code)
        assert "access-control-allow-origin" not in h, "a refused preflight got a CORS header: %r" % o
    assert hits == [], "a foreign origin reached the upstream: %r" % hits


def test_several_origin_headers_are_refused_even_with_the_site_among_them(demo):
    # Caddy joins repeated headers with a comma; a test that accepts when any one value matches would let
    # the site's origin carry a foreign one through, with the project's key injected.
    port, hits = demo(_demo_block())
    for origins in ([SITE[0], "https://evil.example"], ["https://evil.example", SITE[0]], [SITE[0], SITE[1]],
                    [SITE[0], SITE[0]], [SITE[0] + "," + "https://evil.example"], [APP, "https://evil.example"],
                    [APP, SITE[0]]):
        code, h = _req_origins(port, origins)
        assert code == 403, "Origin headers %r: want 403, got %d" % (origins, code)
        assert "access-control-allow-origin" not in h, "refused origins got a CORS header: %r" % origins
    assert hits == [], "several Origin headers reached the upstream: %r" % hits


def _admitted_failures(port, origin):
    """What an admitted origin does not get: a POST answered 200 with its origin echoed and Vary, and a preflight
    answered 204 with its origin. Empty when the origin is served as the chat needs."""
    bad = []
    code, h = _req(port, "POST", "/demo/v1/chat/completions", origin=origin)
    if code != 200 or h.get("access-control-allow-origin") != origin or "Origin" not in h.get("vary", ""):
        bad.append("POST from %s: %d, allow-origin %r, vary %r" % (origin, code, h.get("access-control-allow-origin"),
                                                                 h.get("vary")))
    code, h = _req(port, "OPTIONS", "/demo/v1/chat/completions", origin=origin,
                   extra={"Access-Control-Request-Method": "POST"})
    if code != 204 or h.get("access-control-allow-origin") != origin:
        bad.append("preflight from %s: %d, allow-origin %r" % (origin, code, h.get("access-control-allow-origin")))
    return bad


def test_the_admitted_origins_and_a_request_without_origin_pass(demo):
    port, hits = demo(_demo_block())
    for o in ADMITTED:
        assert _admitted_failures(port, o) == [], o
    # No Origin: curl or a server-side client. It passes and is bounded by the gateway's quotas.
    code, h = _req(port, "POST", "/demo/v1/chat/completions")
    assert code == 200 and "access-control-allow-origin" not in h, (code, h)
    # Preflights are answered by Caddy; the upstream sees only the POSTs, path stripped, key injected.
    assert hits == [("POST", "/v1/chat/completions", "Bearer " + KEY)] * (len(ADMITTED) + 1), hits


APP_ITEM = ", '" + APP + "'"


@pytest.mark.parametrize("matcher", ["@foreign_origin", "@site_origin", "both"])
def test_without_app_in_the_shipped_block_this_bench_sees_the_chat_cut_off(demo, matcher):
    # Removing app. from the refusal list refuses the chat with 403; removing it from the echo list lets the
    # request through without the header a browser needs. Either way the admitted check must fail.
    block = _demo_block()
    lines = block.split(chr(10))
    where =[i for i, l in enumerate(lines) if APP_ITEM in l]
    assert len(where) == 2, "app. must be named on exactly two lines of the shipped block, found %d" % len(where)
    refusal = [i for i in where if lines[i].strip().startswith("not expression ")]
    echo = [i for i in where if lines[i].strip().startswith("@site_origin ")]
    assert len(refusal) == 1 and len(echo) == 1, (refusal, echo)
    for i in {"@foreign_origin": refusal, "@site_origin": echo, "both": where}[matcher]:
        lines[i] = lines[i].replace(APP_ITEM, "")
    port, _ = demo(chr(10).join(lines))
    assert _admitted_failures(port, APP) != [], "the mutant without app. in %s still serves the chat" % matcher
    for o in SITE:
        assert _admitted_failures(port, o) == [], "the mutant must change app. only: %s" % o


def test_without_the_403_line_this_bench_sees_the_hole(demo):
    block = _demo_block()
    line = "respond @foreign_origin 403"
    assert block.count(line) == 1, "the shipped block has no single 403 line"
    mutant = "\n".join(l for l in block.split("\n") if l.strip() != line)
    port, hits = demo(mutant)
    code, h = _req(port, "POST", "/demo/v1/chat/completions", origin="https://evil.example",
                   content_type="text/plain")
    assert code == 200 and "access-control-allow-origin" not in h, (code, h)
    assert len(hits) == 1, "the mutant must let the foreign request reach the upstream: %r" % hits


def test_a_per_value_origin_test_would_let_two_origins_through(demo):
    # The form this block replaced: header_regexp accepts when any one value matches. This bench must see it.
    block = _demo_block()
    shipped = [l for l in block.split("\n") if l.strip().startswith("not expression ")]
    assert len(shipped) == 1, "the shipped block has no single joined-value origin test"
    mutant = block.replace(shipped[0], "\t\tnot header_regexp Origin ^https://(www[.])?dendranetwork[.]com$")
    port, hits = demo(mutant)
    code, h = _req_origins(port, [SITE[0], "https://evil.example"])
    assert code == 200, "the mutant must let two Origin headers through, got %d" % code
    assert hits == [("POST", "/v1/chat/completions", "Bearer " + KEY)], hits


def test_the_whole_shipped_file_validates(tmp_path):
    env = dict(os.environ, DENDRA_API_KEY=KEY, HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path / "data"),
               XDG_CONFIG_HOME=str(tmp_path / "config"))
    r = subprocess.run([CADDY, "validate", "--adapter", "caddyfile", "--config", str(ROOT / "docker" / "Caddyfile")],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]
