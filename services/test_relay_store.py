#!/usr/bin/env python3
"""Relay PERSISTENCE tests — `python3 test_relay_store.py`

What these tests defend: a sealed reveal must survive a relay restart. It is the only artifact that
makes an audit judgeable; losing it freezes the held fee indefinitely, since a sub-quorum audit defers
without bound. Cases (8) and (9) are the ones that turn red on a relay whose store is volatile or
whose eviction is count-based.

No external dependency, no network, no subprocess: a restart is simulated with `importlib.reload`,
which resets module state exactly as a fresh process would.
"""
import base64
import importlib
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import relay_store
from relay_store import DiskStore, StoreUnavailable

OK = [0]
FAIL = []


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


KINDS = ["pub", "req", "res", "reveal", "attest"]
TMP = tempfile.mkdtemp(prefix="dendra-store-")

# THIS TEST MUST WRITE NOTHING INTO THE REPOSITORY. Two mechanisms would make it do so: (a) the default
# store resolves next to the SCRIPT -> the test runs against a COPY of the module placed in the
# temporary directory; (b) relative values resolve from the CWD -> the test moves there first. A test
# that dirties the working tree lies about its cost.
MOD = os.path.join(TMP, "mod")
os.makedirs(MOD, exist_ok=True)
for f in ("relay.py", "relay_store.py"):
    shutil.copy2(os.path.join(HERE, f), os.path.join(MOD, f))
os.chdir(TMP)

# --- (1) basic round trip: what is written reads back byte for byte -------------------------------
s = DiskStore(os.path.join(TMP, "a"), KINDS, max_entries=10)
s.write("reveal", "job1__juge5", b'{"ct":"deadbeef"}')
back = {k: v for k, v, _n, _t in DiskStore(os.path.join(TMP, "a"), KINDS, 10).load()["reveal"]}
chk("(1) a stored reveal reads back after REOPENING the store",
    back.get("job1__juge5") == b'{"ct":"deadbeef"}', str(back)[:80])

# --- (2) FIFO order survives a restart (otherwise eviction would hit arbitrary entries) -----------
s2 = DiskStore(os.path.join(TMP, "b"), KINDS, max_entries=10)
for i in range(5):
    s2.write("req", f"k{i}", bytes([i]))
order = [k for k, _v, _n, _t in DiskStore(os.path.join(TMP, "b"), KINDS, 10).load()["req"]]
chk("(2) FIFO order is rebuilt from oldest to newest",
    order == ["k0", "k1", "k2", "k3", "k4"], str(order))

# --- (3) LOADING discards nothing on account of COUNT ---------------------------------------------
# A `load()` that keeps "the max_entries most RECENT" entries and deletes the rest lets a restart
# destroy the evidence of an old audit as soon as recent traffic outnumbers it. That is blind
# eviction, disguised as a memory bound.
s3 = DiskStore(os.path.join(TMP, "c"), KINDS, max_entries=1000)
for i in range(12):
    s3.write("res", f"k{i}", b"x")
small = DiskStore(os.path.join(TMP, "c"), KINDS, max_entries=5)
kept = [k for k, _v, _n, _t in small.load()["res"]]
chk("(3) on load, NO entry is discarded on account of count (only AGE evicts)",
    len(kept) == 12, f"{len(kept)} reloaded out of 12 — count-based truncation still present")
chk("(3b) and the disk keeps them all", small.count()["res"] == 12, str(small.count()))
old = DiskStore(os.path.join(TMP, "c"), KINDS, max_entries=1000)
kept_age = old.load(retention=1.0, now=time.time() + 10_000)["res"]
chk("(3c) while an exceeded RETENTION does purge (age-based eviction at startup)",
    kept_age == [] and old.last_load_expired == 12 and old.count()["res"] == 0,
    f"{len(kept_age)} remaining, {old.last_load_expired} purged")

# --- (4) the key comes from the NETWORK: it must never reach the filesystem ------------------------
# The escape, not a literal accent: the BYTES are what this case tests (a non-ASCII key must
# never reach the filesystem), and the escape keeps them identical while the file stays pure ASCII.
HOSTILE = ["../../../etc/passwd", "..", ".", "a/b/c", "%2e%2e%2fetc", "\x00nul", "\u00e9" * 50, "k" * 4000]
s4 = DiskStore(os.path.join(TMP, "d"), KINDS, max_entries=100)
for i, k in enumerate(HOSTILE):
    s4.write("reveal", k, f"v{i}".encode())
got = {k: v for k, v, _n, _t in DiskStore(os.path.join(TMP, "d"), KINDS, 100).load()["reveal"]}
chk("(4) every hostile key round-trips correctly",
    all(got.get(k) == f"v{i}".encode() for i, k in enumerate(HOSTILE)), str(len(got)))
inside = os.listdir(os.path.join(TMP, "d", "reveal"))
chk("(4b) no filename echoes the key: they are ALL digests",
    all(n.endswith(".rec") and len(n) == 64 + 4 and all(c in "0123456789abcdef" for c in n[:64])
        for n in inside), str(inside)[:120])
    # The root contains ONLY the kind directories, and each kind ONLY .rec files: a key such as
    # "../../../etc/passwd" therefore created no entry anywhere else. Testing for the existence of
    # /etc/passwd itself would prove nothing — that file always exists.
chk("(4c) nothing was written OUTSIDE the store root (no traversal)",
    sorted(os.listdir(os.path.join(TMP, "d"))) == sorted(KINDS)
    and all(n.endswith(".rec") for k in KINDS for n in os.listdir(os.path.join(TMP, "d", k))),
    str(sorted(os.listdir(os.path.join(TMP, "d")))))

# --- (5) unusable store => FATAL, never a silent in-memory fallback -------------------------------
blocker = os.path.join(TMP, "not-a-dir")
with open(blocker, "wb") as f:
    f.write("this is a file, not a directory".encode())
try:
    DiskStore(os.path.join(blocker, "sub"), KINDS)
    chk("(5) a non-writable store raises StoreUnavailable (fail closed)", False, "no exception")
except StoreUnavailable as e:
    chk("(5) a non-writable store raises StoreUnavailable (fail closed)", True)
    chk("(5b) and the error NAMES the offending path", "not-a-dir" in str(e), str(e)[:80])

# --- (6) a corrupt record is IGNORED and COUNTED, never guessed -----------------------------------
s6 = DiskStore(os.path.join(TMP, "e"), KINDS, max_entries=100)
s6.write("reveal", "healthy", b"intact")
s6.write("reveal", "broken", b"does not matter")
victim = os.path.join(TMP, "e", "reveal", relay_store._fname("reveal", "broken"))
with open(victim, "wb") as f:
    f.write(b'{"k": "broken", "n": 1, "b": "NOT-BASE64-{{{')
s6b = DiskStore(os.path.join(TMP, "e"), KINDS, max_entries=100)
res6 = {k: v for k, v, _n, _t in s6b.load()["reveal"]}
chk("(6) the healthy record loads despite a corrupt neighbour", res6.get("healthy") == b"intact")
chk("(6b) the corrupt one is NOT loaded and it is COUNTED (content is never guessed)",
    "broken" not in res6 and s6b.last_load_corrupt == 1, str(s6b.last_load_corrupt))

# --- (7) deletion ----------------------------------------------------------------------------------
s7 = DiskStore(os.path.join(TMP, "f"), KINDS, max_entries=100)
s7.write("pub", "m1", b"aa")
s7.remove("pub", "m1")
chk("(7) remove() erases the record, and a second remove does not raise",
    s7.count()["pub"] == 0 and s7.remove("pub", "m1") is None)

# --- (8) the reveal survives a relay RESTART -------------------------------------------------------
# A relay without `_boot_store` starts with an empty STORE, and this case fails: it is the executed
# proof that a restart would make open audits unjudgeable.
os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv")
sys.path.insert(0, MOD)          # the COPY: its default store lands in TMP, not in the repository
import relay  # noqa: E402
relay = importlib.reload(relay)
chk("(7b) the test works on a COPY of the module (nothing is written into the repository)",
    os.path.dirname(os.path.abspath(relay.__file__)) == MOD, relay.__file__)
if not hasattr(relay, "_boot_store"):
    chk("(8) the relay RELOADS its reveals at startup", False,
        "relay does not expose _boot_store: volatile store")
    chk("(8b) the deposit is written to DISK, not only to RAM", False, "no store")
    chk("(9) FIFO eviction also erases the on-disk record", False, "no store")
    chk("(10) without a writable store, the relay REFUSES to serve", False, "no store")
else:
    SEALED = json.dumps({"client_eph_pk": "aa" * 32, "nonce": "bb" * 12, "ct": "cc" * 64}).encode()
    relay._put("reveal", "job1784919287094__juge5", SEALED)
    relay._put("pub", "juge5", b'{"pub":"dd"}')
    relay = importlib.reload(relay)          # the restart
    chk("(8) the reveal survives a relay restart",
        relay._get("reveal", "job1784919287094__juge5") == SEALED)
    chk("(8b) and so do the other kinds (pub/req/res/attest)",
        relay._get("pub", "juge5") == b'{"pub":"dd"}')

    # --- (9) BLIND EVICTION UNDOES PERSISTENCE ----------------------------------------------------
    # COUNT-based eviction destroys a reveal that still serves an OPEN audit as soon as MAX_ENTRIES
    # newer deposits arrive. At roughly 6.6 envelopes per job, the store holds about 600 jobs: past
    # that, the evidence of an old audit is erased by ordinary traffic, with no attacker involved.
    # Persistence on disk is worthless if usage silently deletes what it stored.
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv2")
    relay = importlib.reload(relay)
    relay.MAX_ENTRIES = 3
    PROOF = b'{"ct":"proof-audit-open"}'
    relay._put("reveal", "job-audit-OPEN", PROOF)
    refus = None
    for i in range(5):
        try:
            relay._put("reveal", f"trafic{i}", bytes([i]))
        except relay_store.StoreFull as e:
            refus = str(e)
            break
    chk("(9) the evidence of an open audit SURVIVES recent traffic (no count-based eviction)",
        relay._get("reveal", "job-audit-OPEN") == PROOF,
        "destroyed by newer deposits")
    chk("(9b) and it is the NEW deposit that is refused (507), not the evidence that is erased",
        refus is not None and "REFUSED" in refus, str(refus)[:90])
    relay = importlib.reload(relay)
    relay.MAX_ENTRIES = 3
    chk("(9c) the evidence is still there after a RESTART (disk and RAM agree)",
        relay._get("reveal", "job-audit-OPEN") == PROOF)

    # --- (9d) AGE-based eviction does work — otherwise the store would grow without bound ---------
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv3")
    relay = importlib.reload(relay)
    relay.MAX_ENTRIES = 3
    relay.RETENTION = 1000.0
    vieux = time.time() - 5000                     # well beyond the retention window
    for i in range(3):
        relay._put("reveal", f"vieux{i}", b"x")
        relay.TS["reveal"][f"vieux{i}"] = vieux
    relay._put("reveal", "neuf", b"y")      # must PASS: the 3 old entries have expired
    chk("(9d) EXPIRED entries are evicted and make room (AGE-based eviction)",
        relay._get("reveal", "neuf") == b"y"
        and all(relay._get("reveal", f"vieux{i}") is None for i in range(3)),
        str(sorted(relay.STORE["reveal"])))
    chk("(9e) and they are ALSO erased from disk (no unbounded disk usage)",
        relay.DISK.count()["reveal"] == 1, str(relay.DISK.count()))

    # --- (9f) the retention cannot be SHORTENED through the environment ---------------------------
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv4")
    os.environ["DENDRA_RELAY_RETENTION"] = "1"
    m = importlib.reload(relay)
    chk("(9f) DENDRA_RELAY_RETENTION=1 does NOT go below the compiled floor",
        m.RETENTION >= m.RETENTION_FLOOR, f"RETENTION={m.RETENTION}")
    os.environ["DENDRA_RELAY_RETENTION"] = str(30 * 86400)
    m = importlib.reload(relay)
    chk("(9g) but it can be LENGTHENED (the only direction that protects the evidence)",
        m.RETENTION == 30 * 86400, f"RETENTION={m.RETENTION}")
    os.environ.pop("DENDRA_RELAY_RETENTION", None)
    relay = importlib.reload(relay)

    # --- (10) broken store => the relay refuses to serve, with no memory fallback -----------------
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(blocker, "sub")
    relay = importlib.reload(relay)
    refused = bool(relay.BOOT_ERR) and relay.DISK is None
    try:
        relay._put("reveal", "x", b"y")
        accepted = True
    except StoreUnavailable:
        accepted = False
    chk("(10) without a writable store, the relay REFUSES to serve (no silent memory fallback)",
        refused and not accepted, f"BOOT_ERR={relay.BOOT_ERR!r} accepted={accepted}")

    # --- (11) no environment variable can make the relay volatile ---------------------------------
    volatile = []
    for val in ("", "0", "off", "none", "false", ":memory:"):
        os.environ["DENDRA_RELAY_STORE"] = val
        try:
            m = importlib.reload(relay)
            if m.DISK is None and not m.BOOT_ERR:
                volatile.append(val)            # would serve WITHOUT persistence: forbidden
        except StoreUnavailable:
            pass
    chk("(11) no value of DENDRA_RELAY_STORE disables persistence",
        not volatile, f"volatile values: {volatile}")
    # And the proof that this test stays CLEAN: everything it created lives under TMP.
    dirt = [p for p in ("0", "off", "none", "false", ":memory:", "relay-store")
            if os.path.exists(os.path.join(HERE, p))]
    chk("(12) the test created NOTHING in the repository directory", not dirt, f"leftovers: {dirt}")

print(f"\n{OK[0]} green, {len(FAIL)} red")
if FAIL:
    print("RED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
