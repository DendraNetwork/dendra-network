"""Exercises `modea.registry_cache` with the clock and the network INJECTED, so nothing is approximated.

The module decides nothing, so what is tested is what it REPORTS — and above all the distinction the whole
design rests on: NEVER-READ is not STALE. Conflating the two starts a relay that attributes nothing and
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


class Horloge:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def avance(self, d):
        self.t += d


def lancer(verbeux=True):
    verts = rouges = 0

    def ok(name, cond, detail=""):
        nonlocal verts, rouges
        if cond:
            verts += 1
            if verbeux:
                print(f"  ✓ {name}")
        else:
            rouges += 1
            if verbeux:
                print(f"  ✗ {name} {detail}")

    def mesure(name, f):
        try:
            ok(name, f())
        except Exception as e:  # noqa: BLE001
            nonlocal rouges
            rouges += 1
            if verbeux:
                print(f"  ✗ {name} — EXCEPTION {type(e).__name__}: {str(e)[:90]}")

    # ── (1) the distinction the whole design rests on ───────────────────────────────────────────
    if verbeux:
        print("(1) NEVER-READ is not STALE (the distinction that prevents a mute guard)")
    h = Horloge()
    panne = RegistryCache(lambda: (_ for _ in ()).throw(OSError("connection refused")), h, expiry=60)
    ok("before any read -> NEVER-READ", panne.state() == NEVER_READ)
    ok("a failing read does not raise", panne.refresh() is False)
    ok("after a failure -> STILL NEVER-READ", panne.state() == NEVER_READ)
    ok("the failure is COUNTED and named",
       panne.lectures_ko == 1 and panne.derniere_erreur and "OSError" in panne.derniere_erreur)
    ok("age() is None when nothing was ever known", panne.age() is None)

    h2 = Horloge()
    bon = RegistryCache(lambda: BODY, h2, expiry=60)
    ok("a successful read -> FRESH", bon.refresh() and bon.state() == FEES)
    h2.avance(61)
    ok("61 s later (expiry 60) -> STALE", bon.state() == STALE)
    ok("STALE keeps the content: attribution is still possible", bon.size() == 2)
    ok("the two states are DISTINCT", NEVER_READ != STALE)

    # ── (2) an outage does not destroy what was already known ───────────────────────────────────
    if verbeux:
        print("(2) an outage does not destroy what was already known")
    state = {"ko": False}
    h3 = Horloge()

    def reader():
        if state["ko"]:
            raise OSError("network down")
        return BODY
    c = RegistryCache(reader, h3, expiry=60)
    c.refresh()
    state["ko"] = True
    ok("the refresh fails", c.refresh() is False)
    ok("the content is KEPT", c.size() == 2)
    ok("the state stays FRESH until the expiry is reached", c.state() == FEES)
    h3.avance(120)
    ok("then flips to STALE", c.state() == STALE)
    state["ko"] = False
    ok("recovery returns to FRESH", c.refresh() and c.state() == FEES)
    ok("the counters tell the story (2 ok, 1 failed)", c.lectures_ok == 2 and c.lectures_ko == 1)

    # ── (3) the attribution answer is UNAMBIGUOUS ───────────────────────────────────────────────
    if verbeux:
        print("(3) `operator()` returns the state alongside the answer, never a bare None")
    op, e = bon.operator("m1")
    ok("known miner -> address + state", op == "dendra13gaa2dd2hv4xhphfy6nc4pnyztmf2etuxl9ntm")
    op2, e2 = bon.operator("inconnu")
    ok("unknown miner -> (None, a readable state)", op2 is None and e2 in (FEES, STALE))
    op3, e3 = panne.operator("m1")
    ok("registry never read -> (None, NEVER-READ), not conflated with \"unknown\"",
       op3 is None and e3 == NEVER_READ)

    # ── (4) malformed bodies: counted, never swallowed, never fatal ─────────────────────────────
    if verbeux:
        print("(4) malformed bodies: the failure is COUNTED, never raised, never a false registry")
    for name, body in [
        ("invalid JSON", "{not json"),
        ("root is a list", "[]"),
        ("no miner list", json.dumps({"autre": 1})),
        ("EMPTY registry", json.dumps({"miner": []})),
        ("miners without `operator`", json.dumps({"miner": [{"miner_id": "m1"}]})),
        ("empty `operator`", json.dumps({"miner": [{"miner_id": "m1", "operator": ""}]})),
    ]:
        cc = RegistryCache(lambda body=body: body, Horloge(), expiry=60)
        mesure(f"{name} -> False, NEVER-READ, counted",
               lambda cc=cc: cc.refresh() is False and cc.state() == NEVER_READ
               and cc.lectures_ko == 1 and bool(cc.derniere_erreur))

    # One valid entry among broken ones: keep the valid pair, drop the rest.
    mixte = json.dumps({"miner": [{"miner_id": "m1"}, {"miner_id": "m2", "operator": "dendra1x"},
                                  {"not": "a miner"}, "not even an object"]})
    cm = RegistryCache(lambda: mixte, Horloge(), expiry=60)
    mesure("partially broken entries -> only complete pairs are kept",
           lambda: cm.refresh() and cm.size() == 1 and cm.operator("m2")[0] == "dendra1x"
           and cm.operator("m1")[0] is None)

    # ── (5) inconsistent construction is refused ────────────────────────────────────────────────
    if verbeux:
        print("(5) inconsistent construction -> a named ValueError")
    for name, f in [
        ("reader is not callable", lambda: RegistryCache("not a function", Horloge())),
        ("clock is not callable", lambda: RegistryCache(lambda: BODY, 42)),
        ("expiry is zero", lambda: RegistryCache(lambda: BODY, Horloge(), expiry=0)),
        ("expiry is negative", lambda: RegistryCache(lambda: BODY, Horloge(), expiry=-1)),
    ]:
        try:
            f()
            ok(name + " -> ValueError", False, "(accepted!)")
        except ValueError as e:
            ok(name + " -> ValueError", len(str(e)) > 8)

    # ── (6) the summary line, readable by a probe ───────────────────────────────────────────────
    if verbeux:
        print("(6) stable summary line")
    r = bon.resume()
    ok("carries state=, mineurs=, age=, ok=, ko=",
       all(x in r for x in ("state=", "mineurs=", "age=", "ok=", "ko=")))
    ok("a never-read registry shows age=- rather than age=0",
       "age=-" in panne.resume() and "state=NEVER_READ" in panne.resume())
    if verbeux:
        print(f"      {r}")
        print(f"      {panne.resume()}")

    return verts, rouges


def test_registry_cache():
    _, r = lancer(verbeux=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = lancer()
    print()
    print(f"RESUME_REGISTRY_CACHE verts={v} rouges={r}")
    sys.exit(1 if r else 0)
