#!/usr/bin/env python3
"""Bench for the message a relay refusal prints -- what it may claim, and what it may NOT.

THE PROPERTY UNDER TEST, IN ONE LINE: a refusal speaks for the route it was refused on, and for no
other route.

That is not a stylistic preference, it follows from how the relay decides access. `_politique`
answers PER ROUTE: reading ciphertext is open (bodies are sealed to the X25519 key anchored on chain,
so a secret in front of them protects nothing the design relies on), while writing miner-owned
content needs a writer with a name. A relay running that policy answers 200 on `GET /list`, 200 on
`GET /pub/<mid>`, 404 on a `req|res|reveal` key it does not hold -- and 401 on every write, to a
caller whose token is wrong and whose deposit carries no attributable signature. A single sentence
covering all four call sites therefore cannot be true of all of them.

WHAT A WRONG SENTENCE COSTS. Ending a write refusal with "no job will be received" sends an operator
hunting a token for a work queue nobody closed, and leaves the real cost of a wrong token unnamed:
`res` and `reveal` are writes, so a job could be COMPUTED and never DELIVERED. A message that
announces a wider outage than it measured is not silence, it is confidently wrong -- and unlike
silence, it stops the search.

WHAT THIS BENCH DOES THAT A TABLE COULD NOT. It runs a REAL relay-shaped HTTP server on loopback, in
the topology described above (reads open, writes 401), and calls the SHIPPED functions --
`put_status`, `put`, `get`, `get_blob`, `listing`. It does not re-declare the message beside the
module and then question the copy: every string it judges came out of a real refusal travelling
through real urllib.

⚠️ IT REACHES INTO `_AUTH_WARNED` BETWEEN CASES, AND ONLY THERE. That set is the flood bound; a bench
that could not clear it would measure one case and inherit silence for the rest. Clearing it is
stated here rather than hidden, because the SAME set is what part 3 exists to judge.

Usage: python3 test_relay_auth_message.py    (exit 0 = green)
"""
import io
import json
import os
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set BEFORE importing: the module reads the token once, at import.
os.environ["DENDRA_RELAY_TOKEN"] = "a-token-that-is-not-the-relay-s"
os.environ.pop("DENDRA_SIGN_KEY", None)          # no signing: this bench is about the message
import relay_client as rc                        # noqa: E402

N = KO = 0


def case(nom, got, expected):
    global N, KO
    N += 1
    if got == expected:
        print(f"  OK  {nom}")
    else:
        KO += 1
        print(f"  KO  {nom}  (expected {expected!r}, got {got!r})")


# ── A RELAY THAT REFUSES WRITES AND SERVES READS: the deployed topology, not a convenient one ────
# A fake answering 401 to EVERYTHING could not tell the defect from the fix, because the claim under
# test is precisely that a write refusal says nothing about reads. So the reads here must really
# succeed -- and part 0 checks that they do, or the rest of this file is theatre.
QUEUE = {"pub": ["dm1a"], "req": ["job1__dm1a"], "res": [], "reveal": [], "attest": ["dm1a"]}


class FakeRelay(BaseHTTPRequestHandler):
    closed_for_reading = set()    # routes to also refuse on GET (a relay built before the policy)
    stated_reason = None          # what the relay states in its 401 body (`why`), or nothing

    def _refusal(self):
        body = {"error": "unauthorized"}
        if FakeRelay.stated_reason is not None:
            body["why"] = FakeRelay.stated_reason
        return json.dumps(body).encode()

    def _rep(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _parts(self):
        return [p for p in self.path.strip("/").split("/") if p]

    def do_GET(self):
        p = self._parts()
        route = p[0] if p else ""
        if route in FakeRelay.closed_for_reading:
            return self._rep(401, self._refusal())
        if route == "list":
            return self._rep(200, json.dumps(QUEUE).encode())
        if route == "absent-key":
            return self._rep(404, b'{"error":"absent"}')
        return self._rep(200, json.dumps({"ok": route}).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(n)
        # Every write is refused: that is what a wrong token produces on `signed-or-token` routes
        # when no attributable signature is attached (relay.py::do_POST).
        self._rep(401, self._refusal())

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), FakeRelay)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def drive(fn, *a, **kw):
    """Run a shipped call and return (return value, what it printed). Nothing is replayed."""
    rc._AUTH_WARNED.clear()
    buf = io.StringIO()
    with redirect_stdout(buf):
        val = fn(*a, **kw)
    return val, buf.getvalue()


print("== 0. IS THE BENCH IN THE RIGHT TOPOLOGY? (otherwise everything below is theatre) ==")
val, out = drive(rc.listing, BASE)
case("listing() returns the real queue despite the wrong token", val, QUEUE)
case("...and prints NOTHING", out, "")
val, _ = drive(rc.get, BASE, "pub", "dm1a")
case("get(pub) succeeds despite the wrong token", val, {"ok": "pub"})

print()
print("== 1. A WRITE REFUSAL MAY NOT ANNOUNCE A DEAD WORK QUEUE ==")
val, out = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("put_status(pub) returns 'refused'", val, "refused")
case("the message NAMES the route", "POST pub" in out, True)
case("it says mining is not blocked", "mining is not blocked" in out.lower(), True)
# ⛔ THE HEART OF THIS BENCH: on a write, that sentence is false.
case("it does NOT say 'no job will be received'", "no job will be received" in out, False)
case("it does NOT claim the miner is blind to its jobs", "will NOT see the jobs" in out, False)

# ⛔ NOR MAY IT ASSERT AN ABSENCE IT HAS NOT READ. The `pub` body is a constant and the attestation
# is deterministic, so a miner re-deposits byte-identical content at every start. The relay's
# anti-replay guard refuses that with 401 -- while serving the very artifact it is refusing. A
# refused re-deposit can therefore cost nothing at all, and "the relay copy is missing" would
# describe a state this client never read.
case("it does NOT declare the copy missing", "copy of the encryption key is missing" in out, False)
case("it conditions the cost on what it cannot know",
     "if the relay holds no earlier copy" in out, True)

val, out = drive(rc.put_status, BASE, "attest", "dm1a", {"a": 1})
case("POST attest names its route", "POST attest" in out, True)
case("...and bounds the damage to CONFIDENTIAL jobs", "confidential" in out.lower(), True)
case("...without announcing a dead queue", "no job will be received" in out, False)

val, out = drive(rc.put_status, BASE, "reveal", "job1__dm1a", {"r": 1})
case("POST reveal names the HELD fee", "HELD" in out, True)

print()
print("== 2. THE ONE ROUTE WHOSE REFUSAL REALLY DOES MEAN 'no job' ==")
FakeRelay.closed_for_reading = {"list"}
val, out = drive(rc.listing, BASE)
case("listing() refused still returns {} (unchanged)", val, {})
case("the message names GET list", "GET list" in out, True)
case("...and says the miner will not see its jobs", "will NOT see the jobs" in out, True)
FakeRelay.closed_for_reading = set()

print()
print("== 3. ONE REFUSAL MAY NOT MUTE ANOTHER ==")
# ⛔ A SINGLE GLOBAL FLAG WOULD FAIL HERE, AND IT WOULD FAIL SILENTLY. The startup `POST pub` is the
# cheapest refusal a miner can take; `GET /list` is the only call that tells it a job is waiting
# (`miner.py::main` opens its loop with `relay.listing`). Keyed globally, the cheap one lands
# first and the expensive one never prints -- the least informative refusal muting the most
# informative. The flood bound must be per (method, route), not per service.
rc._AUTH_WARNED.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    rc.put_status(BASE, "pub", "dm1a", {"pub": "x"})      # the cheap refusal, first
    FakeRelay.closed_for_reading = {"list"}
    rc.listing(BASE)                                       # the one that matters, after
FakeRelay.closed_for_reading = set()
both = buf.getvalue()
case("the write refusal is said", "POST pub" in both, True)
case("the QUEUE refusal is said too", "GET list" in both, True)
case("two lines, not one", both.count("[relay] relay refused"), 2)

print()
print("== 4. THE FLOOD BOUND STILL HOLDS: once PER ROUTE, not once per call ==")
rc._AUTH_WARNED.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    for _ in range(5):
        rc.put_status(BASE, "pub", "dm1a", {"pub": "x"})
case("5 identical refusals -> 1 line", buf.getvalue().count("[relay] relay refused"), 1)

print()
print("== 5. ALL FOUR CALL SITES NAME THEMSELVES (none stays anonymous) ==")
FakeRelay.closed_for_reading = {"req", "res"}
_, s_get = drive(rc.get, BASE, "req", "job1__dm1a")
case("get() names GET req", "GET req" in s_get, True)
_, s_blob = drive(rc.get_blob, BASE, "res", "job1__dm1a")
case("get_blob() names GET res", "GET res" in s_blob, True)
FakeRelay.closed_for_reading = set()
_, s_put = drive(rc.put, BASE, "res", "job1__dm1a", {"r": 1})
case("put() (the boolean) names POST res", "POST res" in s_put, True)
case("POST res says COMPUTED but not DELIVERED", "not DELIVERED" in s_put, True)
_, s_list = drive(rc.listing, BASE)
case("listing() on success prints nothing", s_list, "")

print()
print("== 6. A ROUTE OUTSIDE THE TABLE INHERITS NO CONSEQUENCE ==")
# Same reason `_politique` gives an unknown route the STRICTEST policy: what is not known is not
# assumed. Here the strict form is to assert nothing at all.
_, s_unknown = drive(rc.put_status, BASE, "a-route-nobody-anticipated", "k", {"x": 1})
case("the unknown route is NAMED", "POST a-route-nobody-anticipated" in s_unknown, True)
case("...and nothing is claimed about its cost", "not established" in s_unknown, True)
case("...least of all about the work queue", "no job will be received" in s_unknown, False)

print()
print("== 7. WHAT IS NOT AN ACCESS REFUSAL STAYS SILENT (401/403 only) ==")
_, s_404 = drive(rc.get, BASE, "absent-key", "k")
case("a 404 prints no access refusal", "[relay] relay refused" in s_404, False)
_, s_down = drive(rc.get, "http://127.0.0.1:9", "pub", "k")   # closed port -> transport failure
case("a transport failure prints no access refusal", "[relay] relay refused" in s_down, False)

print()
print("== 8. WHEN THE RELAY STATES WHY, QUOTE IT INSTEAD OF GUESSING ==")
# "DENDRA_RELAY_TOKEN is incorrect" is inferred from this module's own variable being non-empty; it
# reads nothing. A relay refuses a write for reasons that have no connection to the token at all --
# an anti-replay hit answers 401 over a signature that verified and was attributed. The body carries
# the reason in `why`, and it was being discarded.
FakeRelay.stated_reason = "REJEU (digest already accepted)"
_, s_why = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("the relay's reason is QUOTED", "REJEU (digest already accepted)" in s_why, True)
case("...attributed to the relay, not adopted", "RELAY'S OWN reason" in s_why, True)
case("...and the token is no longer given as THE cause",
     "DENDRA_RELAY_TOKEN is incorrect" in s_why, False)
case("...local token state still stated, as an observation",
     "DENDRA_RELAY_TOKEN is set" in s_why, True)
case("the route is still named", "POST pub" in s_why, True)

# Remote text: bounded, flattened to one line, never adopted as this client's own verdict.
FakeRelay.stated_reason = ("x" * 400) + "\nSECOND LINE"
_, s_long = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("an oversized reason is TRUNCATED", ("x" * 200) in s_long, False)
case("...and does not break the line", "SECOND LINE" in s_long, False)
case("...the message still comes out", "POST pub" in s_long, True)

# A body that is not JSON, or carries no `why`, must break nothing: the inference resumes.
FakeRelay.stated_reason = None
_, s_mute = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("silent relay -> the inference takes its place back",
     "DENDRA_RELAY_TOKEN is incorrect" in s_mute, True)

print()
print("== 9. A CALL SITE THAT DOES NOT NAME ITSELF MUST FAIL, NOT PRINT ANONYMOUSLY ==")
# `methode` and `route` are REQUIRED on purpose: that is what stops a fifth call site from being
# added without naming itself, and reopening the exact defect this file closes.
try:
    rc._note_auth_failure(Exception(), )   # deliberately incomplete call
    case("a call with no route raises TypeError", "no error", "TypeError")
except TypeError:
    case("a call with no route raises TypeError", "TypeError", "TypeError")

srv.shutdown()
print()
print(f"BANC_RELAY_AUTH_MESSAGE_RESUME cas={N} ko={KO}")
sys.exit(1 if KO else 0)
