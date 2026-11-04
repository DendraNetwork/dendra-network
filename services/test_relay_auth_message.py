#!/usr/bin/env python3
"""Bench for the message a relay refusal prints: what it may claim, and what it may not.

The property under test: a refusal speaks for the route it was refused on, and for no other route.

This follows from how the relay decides access. `_policy` answers per route: reading ciphertext is
open (the bodies are sealed to the X25519 key anchored on chain), while writing miner-owned content
needs a named writer. A relay running that policy answers 200 on `GET /list` and `GET /pub/<mid>`,
404 on a `req|res|reveal` key it does not hold, and 401 on every write from a caller whose token is
wrong and whose deposit carries no attributable signature. One sentence cannot be true of all four
call sites.

A wrong sentence is costly. Ending a write refusal with "no job will be received" sends an operator
looking for a token for a work queue that is open, and leaves the real cost unnamed: `res` and
`reveal` are writes, so a job could be computed and never delivered.

The bench runs a relay-shaped HTTP server on loopback in that topology (reads open, writes 401) and
calls the shipped functions: `put_status`, `put`, `get`, `get_blob`, `listing`. Every string it judges
comes out of a real refusal through real urllib.

It clears `_AUTH_WARNED`, the flood bound, between cases and only there: otherwise it would measure
one case and inherit silence for the rest. Part 3 judges that same set.

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

# Set before importing: the module reads the token once, at import.
os.environ["DENDRA_RELAY_TOKEN"] = "a-token-that-is-not-the-relay-s"
os.environ.pop("DENDRA_SIGN_KEY", None)          # no signing: this bench is about the message
import relay_client as rc                        # noqa: E402

N = KO = 0


def case(name, got, expected):
    global N, KO
    N += 1
    if got == expected:
        print(f"  OK  {name}")
    else:
        KO += 1
        print(f"  KO  {name}  (expected {expected!r}, got {got!r})")


# -- A relay that refuses writes and serves reads: the deployed topology ---------------------------
# A fake answering 401 to everything could not tell the defect from the fix, since the claim under test
# is that a write refusal says nothing about reads. So the reads here really succeed, and part 0
# checks that they do.
QUEUE = {"pub": ["dm1a"], "req": ["job1__dm1a"], "res": [], "reveal": [], "attest": ["dm1a"]}


class FakeRelay(BaseHTTPRequestHandler):
    closed_for_reading = set()    # routes to also refuse on GET (a relay built before the policy)
    stated_reason = None          # what the relay states in its 401 body (`why`), or nothing
    stated_help = None            # where the relay says to read about the refusal (`help`), or nothing

    def _refusal(self):
        body = {"error": "unauthorized"}
        if FakeRelay.stated_reason is not None:
            body["why"] = FakeRelay.stated_reason
        if FakeRelay.stated_help is not None:
            body["help"] = FakeRelay.stated_help
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


print("== 0. Is the bench in the right topology? (otherwise nothing below means anything) ==")
val, out = drive(rc.listing, BASE)
case("listing() returns the real queue despite the wrong token", val, QUEUE)
case("...and prints nothing", out, "")
val, _ = drive(rc.get, BASE, "pub", "dm1a")
case("get(pub) succeeds despite the wrong token", val, {"ok": "pub"})

print()
print("== 1. A write refusal does not announce a dead work queue ==")
val, out = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("put_status(pub) returns 'refused'", val, "refused")
case("the message names the route", "POST pub" in out, True)
case("it says mining is not blocked", "mining is not blocked" in out.lower(), True)
# The core of this bench: on a write, that sentence is false.
case("it does not say 'no job will be received'", "no job will be received" in out, False)
case("it does not claim the miner is blind to its jobs", "will not see the jobs" in out, False)

# Nor may it assert an absence it has not read. The `pub` body is constant and the attestation
# deterministic, so a miner deposits identical bytes at every start, and the relay's anti-replay guard
# refuses the repeat with 401 while it still serves the earlier copy. "The relay copy is missing"
# would describe a state this client never read.
case("it does not declare the copy missing", "copy of the encryption key is missing" in out, False)
case("it conditions the cost on what it cannot know",
     "if the relay holds no earlier copy" in out, True)

val, out = drive(rc.put_status, BASE, "attest", "dm1a", {"a": 1})
case("POST attest names its route", "POST attest" in out, True)
case("...and bounds the damage to confidential jobs", "confidential" in out.lower(), True)
case("...without announcing a dead queue", "no job will be received" in out, False)

val, out = drive(rc.put_status, BASE, "reveal", "job1__dm1a", {"r": 1})
case("POST reveal names the held fee", "held" in out, True)

print()
print("== 2. The one route whose refusal does mean 'no job' ==")
FakeRelay.closed_for_reading = {"list"}
val, out = drive(rc.listing, BASE)
case("listing() refused still returns {} (unchanged)", val, {})
case("the message names GET list", "GET list" in out, True)
case("...and says the miner will not see its jobs", "will not see the jobs" in out, True)
FakeRelay.closed_for_reading = set()

print()
print("== 3. One refusal does not silence another ==")
# A single global flag would fail here without a sound. The startup `POST pub` is the cheapest refusal
# a miner can take; `GET /list` is the only call that tells it a job is waiting (`miner.py::main`
# opens its loop with `relay.listing`). With a global key the cheap one lands first and the important
# one never prints, so the flood bound is per (method, route).
rc._AUTH_WARNED.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    rc.put_status(BASE, "pub", "dm1a", {"pub": "x"})      # the cheap refusal, first
    FakeRelay.closed_for_reading = {"list"}
    rc.listing(BASE)                                       # the one that matters, after
FakeRelay.closed_for_reading = set()
both = buf.getvalue()
case("the write refusal is reported", "POST pub" in both, True)
case("the queue refusal is reported too", "GET list" in both, True)
case("two lines, not one", both.count("[relay] relay refused"), 2)

print()
print("== 4. The flood bound still holds: once per route, not once per call ==")
rc._AUTH_WARNED.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    for _ in range(5):
        rc.put_status(BASE, "pub", "dm1a", {"pub": "x"})
case("5 identical refusals -> 1 line", buf.getvalue().count("[relay] relay refused"), 1)

print()
print("== 5. All four call sites name themselves (none stays anonymous) ==")
FakeRelay.closed_for_reading = {"req", "res"}
_, s_get = drive(rc.get, BASE, "req", "job1__dm1a")
case("get() names GET req", "GET req" in s_get, True)
_, s_blob = drive(rc.get_blob, BASE, "res", "job1__dm1a")
case("get_blob() names GET res", "GET res" in s_blob, True)
FakeRelay.closed_for_reading = set()
_, s_put = drive(rc.put, BASE, "res", "job1__dm1a", {"r": 1})
case("put() (the boolean) names POST res", "POST res" in s_put, True)
case("POST res says computed but not delivered", "not delivered" in s_put, True)
_, s_list = drive(rc.listing, BASE)
case("listing() on success prints nothing", s_list, "")

print()
print("== 6. A route outside the table inherits no consequence ==")
# For the same reason `_policy` gives an unknown route the strictest policy: what is not known is not
# assumed. Here the strict form is to assert nothing at all.
_, s_unknown = drive(rc.put_status, BASE, "a-route-nobody-anticipated", "k", {"x": 1})
case("the unknown route is named", "POST a-route-nobody-anticipated" in s_unknown, True)
case("...and nothing is claimed about its cost", "not established" in s_unknown, True)
case("...least of all about the work queue", "no job will be received" in s_unknown, False)

print()
print("== 7. What is not an access refusal stays silent (401/403 only) ==")
_, s_404 = drive(rc.get, BASE, "absent-key", "k")
case("a 404 prints no access refusal", "[relay] relay refused" in s_404, False)
_, s_down = drive(rc.get, "http://127.0.0.1:9", "pub", "k")   # closed port -> transport failure
case("a transport failure prints no access refusal", "[relay] relay refused" in s_down, False)

print()
print("== 8. When the relay states why, quote it instead of guessing ==")
# "DENDRA_RELAY_TOKEN is incorrect" is inferred from this module's own variable being set; it reads
# nothing. A relay can refuse a write for reasons unrelated to the token: an anti-replay hit answers
# 401 over a signature that verified and was attributed, with the reason in `why`.
FakeRelay.stated_reason = "REPLAY (digest already accepted)"
_, s_why = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("the relay's reason is quoted", "REPLAY (digest already accepted)" in s_why, True)
case("...attributed to the relay, not adopted", "relay's own reason" in s_why, True)
case("...and the token is no longer given as the cause",
     "DENDRA_RELAY_TOKEN is incorrect" in s_why, False)
case("...local token state still stated, as an observation",
     "DENDRA_RELAY_TOKEN is set" in s_why, True)
case("the route is still named", "POST pub" in s_why, True)

# Remote text: bounded, flattened to one line, never adopted as this client's own verdict.
FakeRelay.stated_reason = ("x" * 400) + "\nsecond line"
_, s_long = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("an oversized reason is truncated", ("x" * 200) in s_long, False)
case("...and does not break the line", "second line" in s_long, False)
case("...the message still comes out", "POST pub" in s_long, True)

# A body that is not JSON, or carries no `why`, must break nothing: the inference resumes.
FakeRelay.stated_reason = None
_, s_mute = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("silent relay -> the inference takes its place back",
     "DENDRA_RELAY_TOKEN is incorrect" in s_mute, True)

print()
print("== 8b. When the relay says where to read about the refusal, show it, and only then ==")
HELP = "https://dendranetwork.com/docs/#relay-refusals"
FakeRelay.stated_help = HELP
_, s_help = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("the relay's help is shown, quoted", f'The relay points to "{HELP}"' in s_help, True)
case("...without a reason from the relay, the token inference stays", "DENDRA_RELAY_TOKEN is incorrect" in s_help, True)
case("...and the route is still named", "POST pub" in s_help, True)
case("...on one line", s_help.count(chr(10)), 1)

FakeRelay.stated_reason = "REPLAY (digest already accepted)"
_, s_both = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("with a reason and a help, both are shown", ("REPLAY (digest already accepted)" in s_both) and (HELP in s_both), True)
_, answer_why, _s = rc.put_answer(BASE, "pub", "dm1a", {"pub": "x"})
case("put_answer returns the reason alone, not the help", answer_why, "REPLAY (digest already accepted)")
FakeRelay.stated_reason = None

FakeRelay.closed_for_reading = {"list"}
_, s_list_help = drive(rc.listing, BASE)
case("a refused read shows the help too", HELP in s_list_help, True)
FakeRelay.closed_for_reading = set()

# No help, no line: nothing takes its place, whatever the shape of the field.
for label, value in (("absent", None), ("empty", ""), ("a number", 5), ("an object", {"a": 1}), ("a list", [HELP])):
    FakeRelay.stated_help = value
    _, s_none = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
    case(f"a help that is {label} prints no pointer", "The relay points to" in s_none, False)
    case(f"...and the refusal still comes out ({label})", "POST pub" in s_none, True)

# Remote text: bounded, flattened to one line.
FakeRelay.stated_help = ("h" * 400) + "\nsecond line"
_, s_long_help = drive(rc.put_status, BASE, "pub", "dm1a", {"pub": "x"})
case("an oversized help is truncated", ("h" * 250) in s_long_help, False)
case("...and does not break the line", "second line" in s_long_help, False)
case("...the message still comes out with what was kept", ("h" * 100) in s_long_help, True)
FakeRelay.stated_help = None

print()
print("== 9.A call site that does not name itself fails instead of printing anonymously ==")
# `method` and `route` are required on purpose: a fifth call site cannot be added without naming itself.
try:
    rc._note_auth_failure(Exception(), )   # deliberately incomplete call
    case("a call with no route raises TypeError", "no error", "TypeError")
except TypeError:
    case("a call with no route raises TypeError", "TypeError", "TypeError")

srv.shutdown()
print()
print(f"RELAY_AUTH_MESSAGE_SUMMARY cases={N} ko={KO}")
sys.exit(1 if KO else 0)
