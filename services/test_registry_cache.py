"""Exercises `modea.registry_cache` with the clock and the network injected, so nothing is approximated.

The module decides nothing, so what is tested is what it reports — and above all the distinction the whole
design rests on: never-read is not STALE. Conflating the two starts a relay that attributes nothing and
that nothing distinguishes from a healthy relay.

The reference JSON body is not invented: it is the shape the network gateway actually returns for
`/dendra/jobs/v1/miner`, fields included.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from modea.registry_cache import FEES, NEVER_READ, STALE, RegistryCache  # type: ignore
except ImportError:
    from registry_cache import FEES, NEVER_READ, STALE, RegistryCache  # type: ignore

# The gateway's real response shape, observed rather than assumed.
BODY = json.dumps({"miner": [
    {"creator": "dendra1c", "demand": "0", "enc_pubkey": "AA", "miner_id": "m1",
     "operator": "dendra13gaa2dd2hv4xhphfy6nc4pnyztmf2etuxl9ntm", "region": "eu",
     "stake": "1000", "subsidy_claimed": False, "vrf_pubkey": "BB"},
    {"creator": "dendra1c", "demand": "0", "enc_pubkey": "AA", "miner_id": "m2",
     "operator": "dendra1qjr85xgm4ceuksaf7wjnadlracsz609s25h0ss", "region": "eu",
     "stake": "1000", "subsidy_claimed": False, "vrf_pubkey": "BB"},
]})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


def run(verbose=True):
    passed = failed = 0

    def ok(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  OK   {name}")
        else:
            failed += 1
            if verbose:
                print(f"  FAIL {name} {detail}")

    def measure(name, f):
        try:
            ok(name, f())
        except Exception as e:  # noqa: BLE001
            nonlocal failed
            failed += 1
            if verbose:
                print("  FAIL", f"{name}: exception {type(e).__name__}: {str(e)[:90]}")

    # -- (1) the distinction the whole design rests on -------------------------------------------
    if verbose:
        print("(1) never-read is not STALE (the distinction that prevents a mute guard)")
    h = Clock()
    failing = RegistryCache(lambda: (_ for _ in ()).throw(OSError("connection refused")), h, expiry=60)
    ok("before any read -> never-read", failing.state() == NEVER_READ)
    ok("a failing read does not raise", failing.refresh() is False)
    ok("after a failure -> still never-read", failing.state() == NEVER_READ)
    ok("the failure is counted and named",
       failing.reads_failed == 1 and failing.last_error and "OSError" in failing.last_error)
    ok("age() is None when nothing was ever known", failing.age() is None)

    h2 = Clock()
    good = RegistryCache(lambda: BODY, h2, expiry=60)
    ok("a successful read -> fresh", good.refresh() and good.state() == FEES)
    h2.advance(61)
    ok("61 s later (expiry 60) -> STALE", good.state() == STALE)
    ok("STALE keeps the content: attribution is still possible", good.size() == 2)
    ok("the two states are distinct", NEVER_READ != STALE)

    # -- (2) an outage does not destroy what was already known -----------------------------------
    if verbose:
        print("(2) an outage does not destroy what was already known")
    state = {"ko": False}
    h3 = Clock()

    def reader():
        if state["ko"]:
            raise OSError("network down")
        return BODY
    c = RegistryCache(reader, h3, expiry=60)
    c.refresh()
    state["ko"] = True
    ok("the refresh fails", c.refresh() is False)
    ok("the content is kept", c.size() == 2)
    ok("the state stays fresh until the expiry is reached", c.state() == FEES)
    h3.advance(120)
    ok("then flips to STALE", c.state() == STALE)
    state["ko"] = False
    ok("recovery returns to fresh", c.refresh() and c.state() == FEES)
    ok("the counters tell the story (2 ok, 1 failed)", c.reads_ok == 2 and c.reads_failed == 1)

    # -- (3) the attribution answer is unambiguous -----------------------------------------------
    if verbose:
        print("(3) `operator()` returns the state alongside the answer, never a bare None")
    op, e = good.operator("m1")
    ok("known miner -> address + state", op == "dendra13gaa2dd2hv4xhphfy6nc4pnyztmf2etuxl9ntm")
    op2, e2 = good.operator("unknown")
    ok("unknown miner -> (None, a readable state)", op2 is None and e2 in (FEES, STALE))
    op3, e3 = failing.operator("m1")
    ok("registry never read -> (None, never-read), not conflated with \"unknown\"",
       op3 is None and e3 == NEVER_READ)

    # -- (4) malformed bodies: counted, never swallowed, never fatal -----------------------------
    if verbose:
        print("(4) malformed bodies: the failure is counted, never raised, never a false registry")
    for name, body in [
        ("invalid JSON", "{not json"),
        ("root is a list", "[]"),
        ("no miner list", json.dumps({"other": 1})),
        ("empty registry", json.dumps({"miner": []})),
        ("miners without `operator`", json.dumps({"miner": [{"miner_id": "m1"}]})),
        ("empty `operator`", json.dumps({"miner": [{"miner_id": "m1", "operator": ""}]})),
    ]:
        cc = RegistryCache(lambda body=body: body, Clock(), expiry=60)
        measure(f"{name} -> False, never-read, counted",
               lambda cc=cc: cc.refresh() is False and cc.state() == NEVER_READ
               and cc.reads_failed == 1 and bool(cc.last_error))

    # One valid entry among broken ones: keep the valid pair, drop the rest.
    mixed = json.dumps({"miner": [{"miner_id": "m1"}, {"miner_id": "m2", "operator": "dendra1x"},
                                  {"not": "a miner"}, "not even an object"]})
    cm = RegistryCache(lambda: mixed, Clock(), expiry=60)
    measure("partially broken entries -> only complete pairs are kept",
           lambda: cm.refresh() and cm.size() == 1 and cm.operator("m2")[0] == "dendra1x"
           and cm.operator("m1")[0] is None)

    # -- (5) inconsistent construction is refused ------------------------------------------------
    if verbose:
        print("(5) inconsistent construction -> a named ValueError")
    for name, f in [
        ("reader is not callable", lambda: RegistryCache("not a function", Clock())),
        ("clock is not callable", lambda: RegistryCache(lambda: BODY, 42)),
        ("expiry is zero", lambda: RegistryCache(lambda: BODY, Clock(), expiry=0)),
        ("expiry is negative", lambda: RegistryCache(lambda: BODY, Clock(), expiry=-1)),
    ]:
        try:
            f()
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError as e:
            ok(name + " -> ValueError", len(str(e)) > 8)

    # -- (6) the summary line, readable by a probe -----------------------------------------------
    if verbose:
        print("(6) stable summary line")
    r = good.summary()
    ok("carries state=, miners=, age=, ok=, failed=",
       all(x in r for x in ("state=", "miners=", "age=", "ok=", "failed=")))
    ok("a never-read registry shows age=- rather than age=0",
       "age=-" in failing.summary() and "state=NEVER_READ" in failing.summary())
    if verbose:
        print(f"      {r}")
        print(f"      {failing.summary()}")

    return passed, failed


def test_registry_cache():
    _, r = run(verbose=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = run()
    print()
    print(f"REGISTRY_CACHE_SUMMARY passed={v} failed={r}")
    sys.exit(1 if r else 0)
