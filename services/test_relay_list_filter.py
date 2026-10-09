#!/usr/bin/env python3
"""Bench for the FILTERED work queue (`GET /list?suffix=`), its ETag, and the client that reads it.

WHAT THIS BENCH HOLDS, IN TWO HALVES
  · the relay: `/list?suffix=<s>` returns every kind, each reduced to the keys ending with <s>, says
    which suffix it applied, and answers 304 while THAT slice is unchanged — not while the whole store
    is unchanged, or a miner would re-download its slice at every deposit of every other miner, and
    not while anything at all matches, or a miner would never see a new job. A parameter the route
    does not read is refused, never ignored. And the byte budget answers 507 over HTTP, at which point
    the shipped exporter, scraping this relay, reads a saturation equal to the fill.
  · the client (`relay_client.listing(base, suffix)`): against the real relay it reads the slice and
    revalidates it; against a relay that does not serve the filter it reads the FULL queue and keeps
    its own keys — the same jobs, never fewer — and says so ONCE. A relay that does not answer gets no
    second request in the same round.

⚠️ THE OLDER RELAY IS A FAKE, AND WHAT IT FAKES IS WRITTEN DOWN. Before this route existed,
`Handler._route` split the raw target on "/", so `list?suffix=x` was an unknown segment: the
strictest policy (401 with a token configured), else 404 `route`. The fake answers exactly that and
serves `/list` in full. It is the shape read in the relay's previous `_route` and `_politique`, not a
convenient one: a fake that answered 200 would make the fallback untestable.

Usage: python3 test_relay_list_filter.py    (exit 0 = green, 1 = red)
"""
import contextlib
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

N = KO = 0


def case(name, got, expected):
    global N, KO
    N += 1
    if got == expected:
        print(f"  OK  {name}")
    else:
        KO += 1
        print(f"  KO  {name}  (expected {expected!r}, got {got!r})")


# The CLIENT is imported first and with NO token: it stands where an outside operator stands. The relay
# below has a token configured, which this client never sends.
os.environ.pop("DENDRA_RELAY_TOKEN", None)
os.environ.pop("DENDRA_SIGN_KEY", None)
import relay_client as rc  # noqa: E402

TOKEN = "t" * 32
os.environ["DENDRA_RELAY_TOKEN"] = TOKEN
os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix="dendra-list-filter-")
os.environ["DENDRA_RELAY_RATE"] = "100000"
os.environ["DENDRA_RELAY_SIGN"] = "off"
os.environ.pop("DENDRA_RELAY_REGISTRY", None)
os.environ.pop("DENDRA_RELAY_KIND_BUDGET_MIB", None)
rs = importlib.reload(importlib.import_module("relay"))

A, B = "dm1alpha", "dm1bravo"
SA, SB = "__" + A, "__" + B


def serve(handler_cls, threaded=True):
    srv = (ThreadingHTTPServer if threaded else HTTPServer)(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call(base, method, path, data=None, headers=None):
    """(status, body bytes, headers) — any status, never raises on an HTTP code."""
    req = urllib.request.Request(base + "/" + path, data=data, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def deposit(base, kind, key, body=b'{"ct":"x"}'):
    return call(base, "POST", f"{kind}/{key}", body, {"X-Dendra-Token": TOKEN})[0]


def quiet(fn, *a, **k):
    """(result, what it printed)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **k)
    return out, buf.getvalue()


# ══ PART 1 — THE RELAY ═══════════════════════════════════════════════════════════════════════════
srv, BASE = serve(rs.Handler)
print("== the filtered queue, from a real relay, read with no secret ==")
for kind, key in (("req", f"job1{SA}"), ("req", f"job2{SA}"), ("req", f"job3{SB}"),
                  ("res", f"job1{SA}"), ("reveal", f"job9__{B}{SA}"), ("reveal", f"job9__{A}{SB}")):
    deposit(BASE, kind, key)
code, body, hdr = call(BASE, "GET", f"list?suffix={SA}")
doc = json.loads(body) if code == 200 else {}
case("GET /list?suffix=__A with no token -> 200", code, 200)
case("every kind is named, even empty (a READ queue is never {})", sorted(doc), sorted(rs.STORE))
case("req holds A's keys only, in deposit order", doc.get("req"), [f"job1{SA}", f"job2{SA}"])
case("res and reveal too; a reveal is filed under its AUTHOR, the last segment of its key",
     (doc.get("res"), doc.get("reveal")), ([f"job1{SA}"], [f"job9__{B}{SA}"]))
case("the relay says which suffix it applied", hdr.get(rs.LIST_SUFFIX_HEADER), SA)
tag = hdr.get("ETag") or ""
case("...and gives the slice a strong validator", tag.startswith('"') and len(tag) > 10, True)

code, body, _ = call(BASE, "GET", f"list?suffix={SA}", headers={"If-None-Match": tag})
case("the same slice revalidated -> 304 with no body", (code, body), (304, b""))
deposit(BASE, "req", f"job4{SB}")
code, _, _ = call(BASE, "GET", f"list?suffix={SA}", headers={"If-None-Match": tag})
case("ANOTHER miner's new job leaves A's slice unchanged -> still 304", code, 304)
code, _, _ = call(BASE, "GET", f"list?suffix={SA}", headers={"If-None-Match": "W/" + tag})
case("a weak form of the same tag compares on the tag -> 304", code, 304)
deposit(BASE, "req", f"job5{SA}")
code, body, hdr = call(BASE, "GET", f"list?suffix={SA}", headers={"If-None-Match": tag})
case("A's own new job -> 200, and it is in the body",
     (code, f"job5{SA}" in json.loads(body).get("req", []) if code == 200 else None), (200, True))
case("...under a NEW validator", (hdr.get("ETag") or "") != tag, True)
code, _, _ = call(BASE, "GET", f"list?suffix={SA}", headers={"If-None-Match": "*"})
case("`If-None-Match: *` is not honoured (it would freeze a changing slice) -> 200", code, 200)

code, body, hdr = call(BASE, "GET", "list")
full = json.loads(body) if code == 200 else {}
case("GET /list unfiltered is unchanged: every key of every miner",
     sorted(full.get("req", [])), sorted([f"job1{SA}", f"job2{SA}", f"job3{SB}", f"job4{SB}", f"job5{SA}"]))
case("...with no suffix echoed", hdr.get(rs.LIST_SUFFIX_HEADER), None)
code, _, _ = call(BASE, "GET", "list", headers={"If-None-Match": hdr.get("ETag") or "-"})
case("...and revalidable too -> 304", code, 304)

print()
print("== a parameter the route does not read is REFUSED, never ignored ==")
for q, why in (("suffix=", "empty"), (f"suffix={SA}&suffix={SB}", "twice"), ("foo=1", "unknown"),
               ("suffix=" + "a" * 129, "too long"), ("suffix=a%2Fb", "a slash"),
               (f"suffix={SA}&foo=1", "known plus unknown")):
    case(f"GET /list?{q[:24]}... ({why}) -> 400", call(BASE, "GET", f"list?{q}")[0], 400)
case("GET /req/<key>?x=1 -> 400 (was a lookup of the key `<key>?x=1`)",
     call(BASE, "GET", f"req/job1{SA}?x=1")[0], 400)
case("GET /req/<key> without a query is unchanged -> 200", call(BASE, "GET", f"req/job1{SA}")[0], 200)

print()
print("== the byte budget, over HTTP ==")
rs.KIND_BUDGET = rs.USED["req"] + rs._cost(f"job6{SA}", b'{"ct":"x"}')
case("a deposit that fits the budget to the byte -> 200", deposit(BASE, "req", f"job6{SA}"), 200)
code, body, _ = call(BASE, "POST", f"req/job7{SA}", b'{"ct":"x"}', {"X-Dendra-Token": TOKEN})
case("one byte-worth more -> 507, and the refusal says why",
     (code, b"REFUSED" in body), (507, True))
code, body, _ = call(BASE, "GET", "stats", headers={"X-Dendra-Token": TOKEN})
st = json.loads(body) if code == 200 else {}
case("/stats names the full kind in `alarm` (the 80 % signal)", "req" in st.get("alarm", []), True)
case("...and the exporter's field is still there and non-zero (a missing cap reads as zero saturation)",
     st.get("plafond_par_type", 0) > 0, True)
case("...and no key in /stats", any(k in body.decode() for k in (A, B)), False)
rs.KIND_BUDGET = rs.KIND_BUDGET_FLOOR

print()
print("== the saturation the SHIPPED exporter reads from this relay, at the first refusal ==")
# Every exporter already deployed computes saturation as max(occupation) / plafond_par_type and alarms
# near 1. With entries the size of real responses, a constant count bound there read about a third at
# the moment deposits were refused. The exporter below is the shipped module, unpatched; only its
# metrics library is replaced, so its numbers can be read back.
import types as _types  # noqa: E402


class _Gauge(object):
    def __init__(self, *a, **k):
        self.v, self.children = None, {}

    def set(self, v):
        self.v = v

    def labels(self, **kw):
        return self.children.setdefault(tuple(sorted(kw.items())), _Gauge())

    def clear(self):
        self.children.clear()


_prom = _types.ModuleType("prometheus_client")
_prom.Gauge, _prom.start_http_server = _Gauge, (lambda *a, **k: None)
sys.modules["prometheus_client"] = _prom
import exporter as ex  # noqa: E402
ex.RELAY_BASE, ex.RELAY_TOKEN = BASE, TOKEN
BIG = b'{"ct":"' + b"a" * 1990 + b'"}'           # the size of a sealed response
for i in range(3):
    deposit(BASE, "res", f"jobR{i}{SA}", BIG)
rs.KIND_BUDGET = rs.USED["res"] + rs._cost(f"jobR3{SA}", BIG)
code_fit, code_over = deposit(BASE, "res", f"jobR3{SA}", BIG), deposit(BASE, "res", f"jobR4{SA}", BIG)
code, body, _ = call(BASE, "GET", "stats", headers={"X-Dendra-Token": TOKEN})
st = json.loads(body) if code == 200 else {}
ex.refresh_relay()
sat = ex.g_relay_sat.v
case("the kind fills to its budget, then refuses", (code_fit, code_over, st.get("alarm")), (200, 507, ["res"]))
case("the exporter scraped it", ex.g_relay_up.v, 1)
case("...and its saturation is the fill of the fullest kind, never less, never above 1",
     isinstance(sat, float) and st.get("fill_max", 2) <= sat <= 1 and sat >= 0.99, True)
case("...although the most NUMEROUS kind is another one (the ratio's numerator is a count)",
     max(st.get("occupation", {"x": 0}), key=lambda k: st["occupation"][k]) != "res", True)
rs.KIND_BUDGET = rs.KIND_BUDGET_FLOOR

# ══ PART 2 — THE CLIENT ══════════════════════════════════════════════════════════════════════════
print()
print("== relay_client.listing(base, suffix) against the real relay ==")
case("the client and the relay name the echo header with the same bytes",
     rc.LIST_SUFFIX_HEADER, rs.LIST_SUFFIX_HEADER)
got, said = quiet(rc.listing, BASE, SA)
case("the slice, read with no token", sorted(got.get("req", [])),
     sorted([f"job1{SA}", f"job2{SA}", f"job5{SA}", f"job6{SA}"]))
with rs.CPT_LOCK:
    before304 = rs.CPT.get("http_304", 0)
got2, said2 = quiet(rc.listing, BASE, SA)
with rs.CPT_LOCK:
    after304 = rs.CPT.get("http_304", 0)
case("the second read REVALIDATES (the relay answered 304) and returns the same slice",
     (after304 - before304, got2), (1, got))
case("nothing was printed on the nominal path", said + said2, "")
got2["req"].append("job999" + SA)        # a caller that edits the slice it was handed...
got3, _ = quiet(rc.listing, BASE, SA)
case("...edits its own copy: the next 304 hands back the slice the relay validated, not the edit",
     got3, got)
case("listing(base) with no suffix is the full queue, as before",
     sorted(rc.listing(BASE).get("req", [])), sorted(full.get("req", []) + [f"job6{SA}"]))
srv.shutdown()
srv.server_close()


class Fake(BaseHTTPRequestHandler):
    """A relay of a chosen vintage. `mode`:
      old       the relay before this route: /list in full, /list?... -> 404, or 401 if `token`
      new       serves the slice and echoes it
      noecho    answers /list?suffix= with the FULL queue and no echo (a proxy dropped the query)
      broken    /list?suffix= -> 500, /list in full
      ratelim   /list?suffix= -> 429, /list in full
    `hits` counts requests per (path-without-query, has-query)."""
    mode, token, hits = "old", False, {}
    QUEUE = {"pub": [A, B], "req": [f"j1{SA}", f"j2{SB}"], "res": [f"j1{SB}"], "reveal": [],
             "attest": []}

    def _rep(self, code, obj=None, hdrs=None):
        b = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (hdrs or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, q = self.path.partition("?")
        Fake.hits[(path, bool(q))] = Fake.hits.get((path, bool(q)), 0) + 1
        if path == "/list" and not q:
            return self._rep(200, Fake.QUEUE)
        if path == "/list" and q:
            if Fake.mode == "old":
                return self._rep(401 if Fake.token else 404,
                                 {"error": "unauthorized (X-Dendra-Token)" if Fake.token else "route"})
            if Fake.mode == "noecho":
                return self._rep(200, Fake.QUEUE)
            if Fake.mode == "broken":
                return self._rep(500, {"error": "boom"})
            if Fake.mode == "ratelim":
                return self._rep(429, {"error": "rate limited"})
            s = q.split("=", 1)[1]
            return self._rep(200, {k: [x for x in v if x.endswith(s)] for k, v in Fake.QUEUE.items()},
                             {rc.LIST_SUFFIX_HEADER: s, "ETag": '"fake-1"'})
        return self._rep(404, {"error": "route"})

    def log_message(self, *a):
        pass


def fresh(mode, token=False):
    Fake.mode, Fake.token, Fake.hits = mode, token, {}
    return serve(Fake, threaded=False)


MINE = {"pub": [], "req": [f"j1{SA}"], "res": [], "reveal": [], "attest": []}

print()
print("== an OLDER relay (404 on the unknown route): the full queue, filtered here, said once ==")
fsrv, FB = fresh("old")
got, said1 = quiet(rc.listing, FB, SA)
case("the same jobs as a filtering relay would give", got, MINE)
case("the switch is SAID, naming what refused", "did not serve the filtered work queue" in said1
     and "HTTP 404" in said1, True)
got, said = quiet(rc.listing, FB, SA)
case("the next round: same jobs, nothing said again", (got, said), (MINE, ""))
case("...and the filtered route was NOT asked again within the retry window",
     Fake.hits.get(("/list", True)), 1)
case("no auth refusal is reported for the queue (the queue was never refused)",
     "relay refused GET list" in said1 + said, False)
rc._SLICE["retry_at"] = 0.0             # the retry window, elapsed: the filter is asked again...
got, said = quiet(rc.listing, FB, SA)
case("past the window the filter is asked again, still declined: same jobs, and NOT said again "
     "(the state did not change)", (got, said, Fake.hits.get(("/list", True))), (MINE, "", 2))
print("   -- the relay is updated under the running miner --")
Fake.mode = "new"
rc._SLICE["retry_at"] = 0.0             # the retry window, elapsed (LIST_RETRY_S is ten minutes)
got, said = quiet(rc.listing, FB, SA)
case("past the window the filter is asked again and used", got, MINE)
case("...and the return is said", "serves the filtered work queue again" in said, True)
fsrv.shutdown()
fsrv.server_close()

print()
print("== an OLDER relay WITH a token: its 401 on the filter is not a refusal of the queue ==")
fsrv, FB = fresh("old", token=True)
got, said = quiet(rc.listing, FB, SA)
case("the queue is read in full and filtered", got, MINE)
case("the 401 is named as the filter's, not the queue's", ("HTTP 401" in said,
     "relay refused GET list" in said), (True, False))
fsrv.shutdown()
fsrv.server_close()

print()
print("== a 200 that does not echo the filter (a proxy dropped the query): filtered HERE ==")
fsrv, FB = fresh("noecho")
got, said = quiet(rc.listing, FB, SA)
case("only this miner's keys, from the full answer", got, MINE)
case("one request, no second read of /list", (Fake.hits.get(("/list", True)), Fake.hits.get(("/list", False))),
     (1, None))
case("...and it is said", "did not echo the filter" in said, True)
fsrv.shutdown()
fsrv.server_close()


class Shifty(BaseHTTPRequestHandler):
    """A relay whose filter a proxy starts dropping: with `filtering` it serves the slice and echoes it,
    without it the FULL queue whatever the query. Both answers carry the ETag of their own bytes and
    honour If-None-Match, as the real relay does; `inm` records the validator each request carried."""
    filtering, queue, inm = True, {}, []

    def do_GET(self):
        path, _, q = self.path.partition("?")
        Shifty.inm.append(self.headers.get("If-None-Match"))
        doc, hdrs = Shifty.queue, {}
        if Shifty.filtering and q:
            s = q.split("=", 1)[1]
            doc, hdrs = {k: [x for x in v if x.endswith(s)] for k, v in Shifty.queue.items()}, {rc.LIST_SUFFIX_HEADER: s}
        b = json.dumps(doc).encode()
        hdrs["ETag"] = '"' + hashlib.sha256(b).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == hdrs["ETag"]:
            self.send_response(304)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


print()
print("== a validator held from before a proxy dropped the filter is not sent through it ==")
EMPTY = {"pub": [], "req": [], "res": [], "reveal": [], "attest": []}
Shifty.filtering, Shifty.inm = True, []
Shifty.queue = dict(EMPTY, req=[f"j1{SA}", f"j2{SB}"])
ssrv, SBASE = serve(Shifty, threaded=False)
quiet(rc.listing, SBASE, SA)                     # the relay filters: the slice and its validator held
Shifty.filtering = False                         # ...then a proxy starts dropping the query string
got, said = quiet(rc.listing, SBASE, SA)
case("the unfiltered answer is filtered here and said", (got.get("req"), "did not echo the filter" in said),
     ([f"j1{SA}"], True))
Shifty.queue = dict(EMPTY, req=[f"j1{SA}"])      # B's job is gone: the FULL queue is now byte for byte
rc._SLICE["retry_at"] = 0.0                      # the slice held from before the proxy
got, said = quiet(rc.listing, SBASE, SA)
case("past the window no stale validator is sent, so a full queue that equals the old slice is not "
     "taken for the filter's return", (Shifty.inm[-1], "serves the filtered work queue again" in said, got.get("req")),
     (None, False, [f"j1{SA}"]))
ssrv.shutdown()
ssrv.server_close()

print()
print("== a relay that answers 5xx on the filter: /list read ONCE, nothing marked ==")
fsrv, FB = fresh("broken")
got, said = quiet(rc.listing, FB, SA)
case("the jobs come from the full queue", got, MINE)
quiet(rc.listing, FB, SA)
case("the filter is asked again on the next round (a 500 is not 'not served')",
     Fake.hits.get(("/list", True)), 2)
case("nothing claims the route is unserved", "did not serve" in said, False)
fsrv.shutdown()
fsrv.server_close()

print()
print("== a relay that rate-limits or does not answer: nothing more this round ==")
fsrv, FB = fresh("ratelim")
got, _ = quiet(rc.listing, FB, SA)
case("429 -> {} and NO second request (it would double the load that caused it)",
     (got, Fake.hits.get(("/list", False))), ({}, None))
fsrv.shutdown()
fsrv.server_close()
got, said = quiet(rc.listing, FB, SA)          # the port is closed now: nothing answers
case("no answer -> {} (the daemon's own 'not read'), nothing claimed", (got, "did not serve" in said),
     ({}, False))

print()
print(f"BANC_RELAY_LIST_RESUME cas={N} ko={KO} non_eprouves=0")
sys.exit(1 if KO else 0)
