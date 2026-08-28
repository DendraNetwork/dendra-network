#!/usr/bin/env python3
"""Tests of the marker anchoring (PRIMARY side). `python3 test_reveal_worker_marker.py`

No chain, no relay, no `dendrad`: `anchor_marker` is extracted from the module and given doubles for
`relay.put` and `tx_from`. That is deliberate — these tests must run everywhere, including where there
is neither a node nor a GPU, otherwise they would never be executed.

What they defend, in order of importance:
  - a root whose list could not be published is NEVER anchored (an immutable, forever unverifiable
    marker would leave the judges stuck on `None` for that epoch);
  - an epoch without any reveal anchors NOTHING (an empty marker would suggest activity);
  - an already anchored commit is a SUCCESS, not an error (commits are immutable);
  - a genuine failure surfaces the RAW REASON from the chain, never an invented message.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reveal_marker as rmk

OK = [0]
FAIL = []


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


# --- extract anchor_marker WITHOUT importing the module (which requires modea/cryptography) --------
src = open(os.path.join(HERE, "reveal_worker.py"), encoding="utf-8").read()
keep = [n for n in ast.parse(src).body
        if isinstance(n, ast.FunctionDef) and n.name in ("anchor_marker", "_tx_ok")]
calls = {"put": [], "tx": []}


class FakeRelay:
    put_ok = True

    @staticmethod
    def put(url, kind, key, obj):
        calls["put"].append((kind, key, obj))
        return FakeRelay.put_ok


def fake_tx(frm, *a):
    calls["tx"].append((frm,) + a)
    return fake_tx.out


fake_tx.out = "code: 0\ntxhash: " + "A" * 64
ns = {"rmk": rmk, "relay": FakeRelay, "tx_from": fake_tx, "re": re}
exec(compile(ast.Module(body=keep, type_ignores=[]), "rw", "exec"), ns)
anchor_marker = ns["anchor_marker"]

# --- (1) nominal case ------------------------------------------------------------------------------
ok, why = anchor_marker("minerB", "http://r", 25, ["jobB", "jobA", "jobA"])
kind, key, payload = calls["put"][0]
chk("(1) nominal anchoring succeeded", ok, why)
chk("(1b) the list goes to the relay under a deterministic key",
    key == "markerlist__e25__minerB", key)
chk("(1c) the list is SORTED and DEDUPLICATED (otherwise the root would depend on arrival order)",
    payload["jobs"] == ["jobA", "jobB"], str(payload["jobs"]))
chk("(1d) the tx reuses create-commit (no new proto, no consensus break)",
    calls["tx"][0][1] == "create-commit" and calls["tx"][0][2] == "e25__reveals__minerB",
    str(calls["tx"][0][1:3]))
chk("(1e) the PUBLISHED root is exactly the ANCHORED root",
    payload["root"] == rmk.merkle_root(["jobA", "jobB"]) == calls["tx"][0][3])
chk("(1f) the marker also carries the NUMBER of jobs (a coarse check readable on-chain)",
    calls["tx"][0][4] == "2" and calls["tx"][0][5] == rmk.MARKER_KIND)

# --- (2) empty epoch: no marker at all ---------------------------------------------------------------
ok2, why2 = anchor_marker("m", "http://r", 26, [])
chk("(2) epoch without a reveal -> NO marker (no misleading empty marker)",
    not ok2 and "nothing to anchor" in why2)

# --- (3) order of operations: list first, root second ------------------------------------------------
calls["put"].clear(); calls["tx"].clear()
FakeRelay.put_ok = False
ok3, why3 = anchor_marker("m", "http://r", 27, ["j1"])
chk("(3) list REFUSED by the relay -> NO root anchored",
    not ok3 and not calls["tx"],
    "an immutable root without its list would be unverifiable FOR LIFE: the judges would stay on None")

# --- (4) idempotence: an already anchored commit is a success ----------------------------------------
FakeRelay.put_ok = True
fake_tx.out = "code: 18\nraw_log: 'commit already exists'"
ok4, why4 = anchor_marker("m", "http://r", 28, ["j1"])
chk("(4) commit already anchored -> SUCCESS (commits are immutable)", ok4, why4)

# --- (5) genuine failure: the raw reason, never an invented message ----------------------------------
fake_tx.out = "code: 5\nraw_log: insufficient funds"
ok5, why5 = anchor_marker("m", "http://r", 29, ["j1"])
chk("(5) genuine failure -> surfaces the RAW REASON from the chain", not ok5 and "insufficient funds" in why5,
    why5[:80])
fake_tx.out = ""
ok6, why6 = anchor_marker("m", "http://r", 30, ["j1"])
chk("(5b) empty output -> explicit failure, never a false success", not ok6, why6[:60])

print(f"\n{OK[0]} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
