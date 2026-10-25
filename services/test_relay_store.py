#!/usr/bin/env python3
"""Relay persistence tests — `python3 test_relay_store.py`

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
UNTESTED = [0]


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


KINDS = ["pub", "req", "res", "reveal", "attest"]
TMP = tempfile.mkdtemp(prefix="dendra-store-")

# This test must write nothing into the repository. Two mechanisms would make it do so: (a) the default
# store resolves next to the script -> the test runs against a copy of the module placed in the
# temporary directory; (b) relative values resolve from the CWD -> the test moves there first. A test
# that dirties the working tree lies about its cost.
MOD = os.path.join(TMP, "mod")
os.makedirs(MOD, exist_ok=True)
for f in ("relay.py", "relay_store.py"):
    shutil.copy2(os.path.join(HERE, f), os.path.join(MOD, f))
os.chdir(TMP)

# --- (1) basic round trip: what is written reads back byte for byte -------------------------------
s = DiskStore(os.path.join(TMP, "a"), KINDS)
s.write("reveal", "job1__judge5", b'{"ct":"deadbeef"}')
back = {k: v for k, v, _n, _t in DiskStore(os.path.join(TMP, "a"), KINDS).load()["reveal"]}
chk("(1) a stored reveal reads back after reopening the store",
    back.get("job1__judge5") == b'{"ct":"deadbeef"}', str(back)[:80])

# --- (2) FIFO order survives a restart (otherwise eviction would hit arbitrary entries) -----------
s2 = DiskStore(os.path.join(TMP, "b"), KINDS)
for i in range(5):
    s2.write("req", f"k{i}", bytes([i]))
order = [k for k, _v, _n, _t in DiskStore(os.path.join(TMP, "b"), KINDS).load()["req"]]
chk("(2) FIFO order is rebuilt from oldest to newest",
    order == ["k0", "k1", "k2", "k3", "k4"], str(order))

# --- (3) loading discards nothing on account of count ---------------------------------------------
# A `load()` that keeps "the N most recent" entries and deletes the rest lets a restart destroy the
# evidence of an old audit as soon as recent traffic outnumbers it. That is blind eviction, disguised
# as a memory bound. The store takes no ceiling at all now: the bound lives in the relay's `_put`.
s3 = DiskStore(os.path.join(TMP, "c"), KINDS)
for i in range(12):
    s3.write("res", f"k{i}", b"x")
small = DiskStore(os.path.join(TMP, "c"), KINDS)
kept = [k for k, _v, _n, _t in small.load()["res"]]
chk("(3) on load, no entry is discarded on account of count (only age evicts)",
    len(kept) == 12, f"{len(kept)} reloaded out of 12 — count-based truncation still present")
chk("(3b) and the disk keeps them all", small.count()["res"] == 12, str(small.count()))
old = DiskStore(os.path.join(TMP, "c"), KINDS)
kept_age = old.load(retention=1.0, now=time.time() + 10_000)["res"]
chk("(3c) while an exceeded RETENTION does purge (age-based eviction at startup)",
    kept_age == [] and old.last_load_expired == 12 and old.count()["res"] == 0,
    f"{len(kept_age)} remaining, {old.last_load_expired} purged")

# --- (4) the key comes from the network: it must never reach the filesystem ------------------------
# The escape, not a literal accent: the bytes are what this case tests (a non-ASCII key must
# never reach the filesystem), and the escape keeps them identical while the file stays pure ASCII.
HOSTILE = ["../../../etc/passwd", "..", ".", "a/b/c", "%2e%2e%2fetc", "\x00nul", "\u00e9" * 50, "k" * 4000]
s4 = DiskStore(os.path.join(TMP, "d"), KINDS)
for i, k in enumerate(HOSTILE):
    s4.write("reveal", k, f"v{i}".encode())
got = {k: v for k, v, _n, _t in DiskStore(os.path.join(TMP, "d"), KINDS).load()["reveal"]}
chk("(4) every hostile key round-trips correctly",
    all(got.get(k) == f"v{i}".encode() for i, k in enumerate(HOSTILE)), str(len(got)))
inside = os.listdir(os.path.join(TMP, "d", "reveal"))
chk("(4b) no filename echoes the key: they are all digests",
    all(n.endswith(".rec") and len(n) == 64 + 4 and all(c in "0123456789abcdef" for c in n[:64])
        for n in inside), str(inside)[:120])
    # The root contains only the kind directories, and each kind only .rec files: a key such as
    # "../../../etc/passwd" therefore created no entry anywhere else. Testing for the existence of
    # /etc/passwd itself would prove nothing — that file always exists.
chk("(4c) nothing was written outside the store root (no traversal)",
    sorted(os.listdir(os.path.join(TMP, "d"))) == sorted(KINDS)
    and all(n.endswith(".rec") for k in KINDS for n in os.listdir(os.path.join(TMP, "d", k))),
    str(sorted(os.listdir(os.path.join(TMP, "d")))))

# --- (5) unusable store => fatal, never a silent in-memory fallback -------------------------------
blocker = os.path.join(TMP, "not-a-dir")
with open(blocker, "wb") as f:
    f.write("this is a file, not a directory".encode())
try:
    DiskStore(os.path.join(blocker, "sub"), KINDS)
    chk("(5) a non-writable store raises StoreUnavailable (fail closed)", False, "no exception")
except StoreUnavailable as e:
    chk("(5) a non-writable store raises StoreUnavailable (fail closed)", True)
    chk("(5b) and the error names the offending path", "not-a-dir" in str(e), str(e)[:80])

# --- (6) a corrupt record is ignored and counted, never guessed -----------------------------------
s6 = DiskStore(os.path.join(TMP, "e"), KINDS)
s6.write("reveal", "healthy", b"intact")
s6.write("reveal", "broken", b"does not matter")
victim = os.path.join(TMP, "e", "reveal", relay_store._fname("reveal", "broken"))
with open(victim, "wb") as f:
    f.write(b'{"k": "broken", "n": 1, "b": "NOT-BASE64-{{{')
s6b = DiskStore(os.path.join(TMP, "e"), KINDS)
res6 = {k: v for k, v, _n, _t in s6b.load()["reveal"]}
chk("(6) the healthy record loads despite a corrupt neighbour", res6.get("healthy") == b"intact")
chk("(6b) the corrupt one is not loaded and it is counted (content is never guessed)",
    "broken" not in res6 and s6b.last_load_corrupt == 1, str(s6b.last_load_corrupt))

# --- (7) deletion ----------------------------------------------------------------------------------
s7 = DiskStore(os.path.join(TMP, "f"), KINDS)
s7.write("pub", "m1", b"aa")
s7.remove("pub", "m1")
chk("(7) remove() erases the record, and a second remove does not raise",
    s7.count()["pub"] == 0 and s7.remove("pub", "m1") is None)

# --- (8) the reveal survives a relay restart -------------------------------------------------------
# A relay without `_boot_store` starts with an empty STORE, and this case fails: it is the executed
# proof that a restart would make open audits unjudgeable.
os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv")
sys.path.insert(0, MOD)          # the copy: its default store lands in TMP, not in the repository
import relay  # noqa: E402
relay = importlib.reload(relay)
chk("(7b) the test works on a copy of the module (nothing is written into the repository)",
    os.path.dirname(os.path.abspath(relay.__file__)) == MOD, relay.__file__)
if not hasattr(relay, "_boot_store"):
    chk("(8) the relay reloads its reveals at startup", False,
        "relay does not expose _boot_store: volatile store")
    chk("(8b) the deposit is written to disk, not only to RAM", False, "no store")
    chk("(9) FIFO eviction also erases the on-disk record", False, "no store")
    chk("(10) without a writable store, the relay refuses to serve", False, "no store")
else:
    SEALED = json.dumps({"client_eph_pk": "aa" * 32, "nonce": "bb" * 12, "ct": "cc" * 64}).encode()
    relay._put("reveal", "job1784919287094__judge5", SEALED)
    relay._put("pub", "judge5", b'{"pub":"dd"}')
    relay = importlib.reload(relay)          # the restart
    chk("(8) the reveal survives a relay restart",
        relay._get("reveal", "job1784919287094__judge5") == SEALED)
    chk("(8b) and so do the other kinds (pub/req/res/attest)",
        relay._get("pub", "judge5") == b'{"pub":"dd"}')

    # --- (9) blind eviction undoes persistence ----------------------------------------------------
    # count- or size-based eviction destroys a reveal that still serves an open audit as soon as
    # enough newer deposits arrive: the evidence of an old audit is erased by ordinary traffic, with
    # no attacker involved. Persistence on disk is worthless if usage silently deletes what it stored.
    # The budget is set to hold the proof and exactly two traffic entries, in bytes (`_cost`).
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv2")
    relay = importlib.reload(relay)
    PROOF = b'{"ct":"proof-audit-open"}'
    ONE = relay._cost("traffic0", b"\x00")
    relay.KIND_BUDGET = relay._cost("job-audit-OPEN", PROOF) + 2 * ONE
    relay._put("reveal", "job-audit-OPEN", PROOF)
    refusal, admitted = None, 0
    for i in range(5):
        try:
            relay._put("reveal", f"traffic{i}", bytes([i]))
            admitted += 1
        except relay_store.StoreFull as e:
            refusal = str(e)
            break
    chk("(9) the evidence of an open audit survives recent traffic (no eviction by count or size)",
        relay._get("reveal", "job-audit-OPEN") == PROOF,
        "destroyed by newer deposits")
    chk("(9b) and it is the new deposit that is refused (507), not the evidence that is erased",
        refusal is not None and "refused rather than erasing" in refusal and admitted == 2, f"admitted={admitted} {str(refusal)[:90]}")
    relay = importlib.reload(relay)
    chk("(9c) the evidence is still there after a restart (disk and RAM agree)",
        relay._get("reveal", "job-audit-OPEN") == PROOF)

    # --- (9d) age-based eviction does work — otherwise the store would grow without bound ---------
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv3")
    relay = importlib.reload(relay)
    relay.KIND_BUDGET = 3 * relay._cost("old0", b"x")
    relay.RETENTION = 1000.0
    stale_ts = time.time() - 5000                     # well beyond the retention window
    for i in range(3):
        relay._put("reveal", f"old{i}", b"x")
        relay.TS["reveal"][f"old{i}"] = stale_ts
    relay._put("reveal", "fresh", b"y")      # must pass: the 3 old entries have expired
    chk("(9d) expired entries are evicted and make room (age-based eviction)",
        relay._get("reveal", "fresh") == b"y"
        and all(relay._get("reveal", f"old{i}") is None for i in range(3)),
        str(sorted(relay.STORE["reveal"])))
    chk("(9e) and they are also erased from disk (no unbounded disk usage)",
        relay.DISK.count()["reveal"] == 1, str(relay.DISK.count()))
    chk("(9e2) and their bytes are given back: the budget holds what the store holds, nothing more",
        relay.USED["reveal"] == relay._cost("fresh", b"y"),
        f"USED={relay.USED['reveal']} expected {relay._cost('fresh', b'y')}")

    # --- (13) the unit is the byte, not the entry -------------------------------------------------
    # The old ceiling counted entries: 4000 requests of ~330 bytes filled it while weighing 1.3 MB,
    # and 4000 bodies of MAX_BODY weighed 4 GiB. A budget in bytes admits many small entries and
    # refuses one large entry the count would have let through.
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv5")
    relay = importlib.reload(relay)
    SMALL = b"s" * 100
    relay.KIND_BUDGET = 8 * relay._cost("small0", SMALL)
    for i in range(6):
        relay._put("req", f"small{i}", SMALL)
    big_refused = False
    try:
        relay._put("req", "big", b"B" * (3 * relay._cost("small0", SMALL)))
    except relay_store.StoreFull:
        big_refused = True
    still = True
    try:
        relay._put("req", "small6", SMALL)
    except relay_store.StoreFull:
        still = False
    chk("(13) a large deposit is refused by bytes while the store holds few entries",
        big_refused and relay._get("req", "big") is None, f"refused={big_refused}")
    chk("(13b) ...and a small one still fits: the refusal was about weight, not count", still)
    chk("(13c) the total charged is the sum of the entries' costs, each with its fixed overhead",
        relay.USED["req"] == 7 * relay._cost("small0", SMALL)
        and relay.ENTRY_OVERHEAD > 0, f"USED={relay.USED['req']}")
    # (13d) Without the fixed charge, empty deposits weigh nothing. A budget of fifty overheads must
    # stop fifty-odd one-byte entries, not admit the two hundred offered — otherwise the budget bounds
    # bytes of payload and leaves the objects holding them unbounded.
    relay.KIND_BUDGET = relay.USED["req"] + 50 * relay.ENTRY_OVERHEAD
    tiny_ok = 0
    for i in range(200):
        try:
            relay._put("req", f"t{i}", b"t")
            tiny_ok += 1
        except relay_store.StoreFull:
            break
    chk("(13d) one-byte deposits are charged their overhead: fifty overheads admit fewer than fifty",
        0 < tiny_ok < 50, f"admitted {tiny_ok} of 200")

    # --- (14) the total is rebuilt from the records at restart ------------------------------------
    expect = sum(relay._cost(k, v) for k, v in relay.STORE["req"].items())
    relay = importlib.reload(relay)
    chk("(14) after a restart the bytes charged equal the records reloaded (never a stored counter)",
        relay.USED["req"] == expect, f"USED={relay.USED['req']} expected {expect}")

    # --- (15) a replacement is charged its difference; an identical retry is charged nothing ------
    relay._put("pub", "m1", b"p" * 300)
    relay._put("pub", "m1", b"p" * 50)
    chk("(15) a rotated `pub` costs what it holds now, not both versions",
        relay.USED["pub"] == relay._cost("m1", b"p" * 50), str(relay.USED["pub"]))
    before = relay.USED["req"]
    relay._put("req", "small0", SMALL)          # a worker's retry: same bytes, write-once kind
    chk("(15b) an identical retry on a write-once key is free (nothing stored twice)",
        relay.USED["req"] == before, f"{before} -> {relay.USED['req']}")

    # --- (16) the 409 comes before the 507 --------------------------------------------------------
    # A deposit that can never be accepted must say so, not ask its sender to retry later.
    relay.KIND_BUDGET = relay.USED["req"]          # full to the byte
    try:
        relay._put("req", "small0", SMALL + b"different")
        got16 = "accepted"
    except relay.Conflict:
        got16 = "409"
    except relay_store.StoreFull:
        got16 = "507"
    chk("(16) different bytes on an existing sealed key in a full store -> 409, not 507",
        got16 == "409", got16)

    # --- (17) the budget can be raised by the environment, never lowered ---------------------------
    floor = relay.KIND_BUDGET_FLOOR
    cases17 = {"": (floor, False), "1": (floor, True), "1024": (1024 << 20, False),
               "abc": (floor, True), "nan": (floor, True), "inf": (floor, True), "-5": (floor, True),
               "0": (floor, True)}
    bad17 = []
    for raw, (want, noted) in cases17.items():
        got_b, note = relay._read_kind_budget(raw)
        if got_b != want or bool(note) != noted:
            bad17.append((raw, got_b, note))
    chk("(17) DENDRA_RELAY_KIND_BUDGET_MIB raises the budget, and every value that would not raise it "
        "keeps the floor and is reported", not bad17, str(bad17)[:200])
    os.environ["DENDRA_RELAY_KIND_BUDGET_MIB"] = "1"
    m = importlib.reload(relay)
    chk("(17b) ...through the module as loaded: a budget below the floor does not take effect",
        m.KIND_BUDGET == m.KIND_BUDGET_FLOOR and "floor" in m.KIND_BUDGET_NOTE, m.KIND_BUDGET_NOTE)
    os.environ.pop("DENDRA_RELAY_KIND_BUDGET_MIB", None)
    relay = importlib.reload(relay)

    # --- (18) the alarm at 80%: listed in /stats, said once in the log, taken back under 75% ----
    import contextlib
    import io as _io
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv6")
    relay = importlib.reload(relay)
    UNIT = relay._cost("a0", b"z")
    relay.KIND_BUDGET = 10 * UNIT
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(7):
            relay._put("res", f"a{i}", b"z")
        st7 = relay._stats()
        relay._put("res", "a7", b"z")       # 80%: the alarm
        relay._put("res", "a8", b"z")       # 90%: already said
        st9 = relay._stats()
    said = buf.getvalue()
    chk("(18) under the threshold, no kind is in alarm", st7["alarm"] == [] and st7["fill"]["res"] == 0.7,
        str((st7["alarm"], st7["fill"]["res"])))
    chk("(18b) at 80% the kind is listed in /stats `alarm`, and fill_max says how full",
        st9["alarm"] == ["res"] and st9["fill_max"] == 0.9 and st9["bytes"]["res"] == 9 * UNIT,
        str((st9["alarm"], st9["fill_max"])))
    chk("(18c) and the log says it once, naming the category and no key",
        said.count("store alarm: kind res") == 1 and "a7" not in said and "a8" not in said, said[:200])
    relay.RETENTION = 1000.0
    for i in range(4):
        relay.TS["res"][f"a{i}"] = time.time() - 5000
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        relay._put("res", "b0", b"z")       # evicts 4, adds 1: 6 of 10 -> under 75%
    chk("(18d) once back under 75%, the log takes the alarm back",
        "alarm over: kind res" in buf.getvalue() and relay._stats()["alarm"] == [],
        buf.getvalue()[:160])
    chk("(18e) /stats publishes the count bound as `entries_max_per_kind` (budget // overhead)",
        st9["entries_max_per_kind"] == st9["budget_bytes_per_kind"] // st9["entry_overhead_bytes"] > 0,
        str(st9.get("entries_max_per_kind")))

    # (18e2) `legacy_entry_cap` is read as a count ceiling by every exporter already deployed:
    # saturation = max(occupation) / legacy_entry_cap. Kept constant, that ratio understated the fill
    # by the overhead's share of an entry's cost. Its value must keep that ratio at the fill of the
    # fullest kind, never under it, never over 1 while the budget holds — including when the most
    # numerous kind is not the fullest one, which is the usual case (requests are small and many).
    def _legacy(st):
        return max(st["occupation"].values()) / st["legacy_entry_cap"]
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv6b")
    relay = importlib.reload(relay)
    st0 = relay._stats()
    chk("(18e2) an empty store: `legacy_entry_cap` is the count bound, the legacy ratio is 0",
        st0["legacy_entry_cap"] == st0["entries_max_per_kind"] and _legacy(st0) == 0, str(st0["legacy_entry_cap"]))
    relay.KIND_BUDGET = 40 * relay._cost("s00", b"q" * 10)
    with contextlib.redirect_stdout(_io.StringIO()):
        for i in range(30):
            relay._put("req", "s%02d" % i, b"q" * 10)       # many small: the most numerous
        for i in range(6):
            relay._put("res", "b%d" % i, b"Q" * 5000)       # few large: the fullest
    stm = relay._stats()
    chk("(18e3) mixed kinds: the legacy ratio equals the fullest kind's fill (never less, never above 1)",
        stm["fill_max"] == stm["fill"]["res"] > stm["fill"]["req"]
        and stm["fill_max"] <= _legacy(stm) <= 1 and _legacy(stm) - stm["fill_max"] < 0.05,
        "fill=%s legacy=%.4f" % (stm["fill"], _legacy(stm)))

    # (18f) The eviction itself can end an alarm, and that is logged even when the deposit is then refused.
    # Eviction runs before the write-once check: a deposit refused 409 has still given bytes back, and
    # a log whose last word is an alarm that is over keeps an operator looking for a filler.
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv6c")
    relay = importlib.reload(relay)
    UNIT = relay._cost("c0", b"z")
    relay.KIND_BUDGET = 10 * UNIT
    relay.RETENTION = 1000.0
    with contextlib.redirect_stdout(_io.StringIO()):
        for i in range(9):
            relay._put("res", f"c{i}", b"z")                 # 90%: the alarm, said
    for i in range(5):
        relay.TS["res"][f"c{i}"] = time.time() - 5000       # five of them have expired
    buf = _io.StringIO()
    got18f = "accepted"
    with contextlib.redirect_stdout(buf):
        try:
            relay._put("res", "c8", b"different")          # evicts five, then 409
        except relay.Conflict:
            got18f = "409"
    chk("(18f) evictions that bring a kind under 75% end its alarm in the log, deposit refused or not",
        got18f == "409" and "alarm over: kind res" in buf.getvalue(), f"{got18f} {buf.getvalue()[:120]!r}")

    # --- (19) a store reloaded above its budget discards nothing and refuses the next deposit -------
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv7")
    relay = importlib.reload(relay)
    for i in range(5):
        relay._put("reveal", f"r{i}", b"R" * 200)
    relay = importlib.reload(relay)   # the restart: 5 records reloaded
    # The budget is set under what was reloaded. The environment cannot do that (it only raises), but
    # a disk can hold more than a budget: a store carried over from another relay, or a hand copy.
    relay.KIND_BUDGET = 2 * relay._cost("r0", b"R" * 200)
    kept19 = all(relay._get("reveal", f"r{i}") == b"R" * 200 for i in range(5))
    try:
        relay._put("reveal", "r5", b"R")
        got19 = "accepted"
    except relay_store.StoreFull:
        got19 = "507"
    chk("(19) a reload over the budget keeps every record, and the next new deposit is refused",
        kept19 and got19 == "507", f"kept={kept19} next={got19}")
    chk("(19b) ...and /stats says the kind is in alarm (fill above 1 is reported, not clipped)",
        relay._stats()["alarm"] == ["reveal"] and relay._stats()["fill"]["reveal"] > 1,
        str(relay._stats()["fill"]))
    _st19 = relay._stats()
    chk("(19b2) ...and the legacy ratio says so too: max(occupation) / legacy_entry_cap is above 1",
        max(_st19["occupation"].values()) / _st19["legacy_entry_cap"] > 1, str(_st19["legacy_entry_cap"]))
    # A budget smaller than one entry: the count that would make the ratio true rounds to zero, and its
    # readers treat a zero cap as "no saturation" (`cap > 0` or 0). Never under 1.
    relay.KIND_BUDGET = relay._cost("r0", b"R" * 200) // 3
    _st19 = relay._stats()
    chk("(19b3) a budget under one entry: legacy_entry_cap stays at 1, the legacy ratio far above 1",
        _st19["legacy_entry_cap"] == 1 and max(_st19["occupation"].values()) / _st19["legacy_entry_cap"] > 1,
        str(_st19["legacy_entry_cap"]))

    # --- (19c) a store reloaded above the alarm says so at startup, and only there --------------------
    # main() prints `_startup_alarm_lines()`; `_boot_store` marks the kind as said, so the first deposit
    # after the restart does not say it a second time. The boot is re-run with a budget set below what
    # the disk holds, the only way to reach the alarm without filling 64 MiB.
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv7b")
    relay = importlib.reload(relay)
    with contextlib.redirect_stdout(_io.StringIO()):
        for i in range(9):
            relay._put("reveal", f"v{i}", b"V" * 50)
    relay.KIND_BUDGET = 10 * relay._cost("v0", b"V" * 50)
    relay._boot_store()                     # the restart, against that budget
    lines19 = relay._startup_alarm_lines()
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        relay._put("reveal", "v9", b"V" * 50)              # 100%: still the same alarm
    chk("(19c) a kind reloaded at 90% is named by the startup banner",
        lines19 == ["[relay] store alarm at startup: kind reveal reloaded at 90% of its budget"], str(lines19))
    chk("(19d) ...and the first deposit after the restart does not say it a second time",
        "store alarm" not in buf.getvalue(), buf.getvalue()[:160])

    # --- (20) the budget against the memory the process may use -----------------------------------
    # Every kind full must fit the container's memory limit, or the relay is killed before it refuses
    # anything, and killed again at each reload. The compiled floor must fit the limit the stack ships
    # (docker-compose.yml, the relay service's `mem_limit`), read here rather than retyped.
    vbm = relay.verdict_budget_memory
    kinds = len(relay.STORE)
    floor = relay.KIND_BUDGET_FLOOR
    shipped = None
    # The development tree keeps this bench two levels under the root, the published one a single
    # level: the root compose file is looked for at both, and nowhere else.
    for _compose in (os.path.join(HERE, "..", "..", "docker-compose.yml"),
                     os.path.join(HERE, "..", "docker-compose.yml")):
        try:
            _txt = open(_compose, encoding="utf-8").read()
            _blk = _txt.split(chr(10) + "  relay:" + chr(10), 1)[1].split(chr(10) + chr(10), 1)[0]
        except (OSError, IndexError):
            continue
        _lim = [l.split(":", 1)[1].strip() for l in _blk.split(chr(10)) if l.strip().startswith("mem_limit:")]
        if len(_lim) == 1 and _lim[0][:-1].isdigit() and _lim[0][-1] in "mg":
            shipped = int(_lim[0][:-1]) << (20 if _lim[0][-1] == "m" else 30)
        break
    if shipped is None:
        # Not found is not green: the case is declared untested, and counted as such below.
        print("  not tested (20) the shipped relay mem_limit could not be read from docker-compose.yml")
        UNTESTED[0] += 1
    else:
        chk("(20) the compiled floor, every kind full, fits the relay's shipped memory limit",
            vbm(floor, kinds, shipped) == ("OK", ""), str(vbm(floor, kinds, shipped)))
    chk("(20b) a raised budget that does not fit is reported at startup",
        vbm(1 << 30, kinds, 512 << 20)[0] == "WARNING"
        and "killed" in vbm(1 << 30, kinds, 512 << 20)[1])
    chk("(20c) an unreadable limit is reported, never read as 'no limit'",
        vbm(floor, kinds, "unknown")[0] == "OK" and "could not be read" in vbm(floor, kinds, "unknown")[1])
    chk("(20d) no limit at all -> nothing to compare, nothing said", vbm(1 << 40, kinds, None) == ("OK", ""))
    _lim_now = relay._cgroup_memory_limit()
    chk("(20e) the cgroup reader answers a number, None or 'unknown' (three states)",
        _lim_now is None or _lim_now == "unknown" or (isinstance(_lim_now, int) and _lim_now > 0),
        repr(_lim_now))

    # (20f) The reader itself, on fabricated trees: the process's own cgroup first, then the root,
    # then cgroup v1 — and a file that says nothing readable is "unknown", never "no limit".
    def _tree(name, files):
        r = os.path.join(TMP, "cg-" + name)
        for rel, txt in files.items():
            p = os.path.join(r, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(txt)
        return relay._cgroup_memory_limit(r)
    _cg = {
        "own": _tree("own", {"proc/self/cgroup": "0::/docker/abc" + chr(10),
                             "sys/fs/cgroup/docker/abc/memory.max": "536870912" + chr(10),
                             "sys/fs/cgroup/memory.max": "max" + chr(10)}),
        "own-max": _tree("own-max", {"proc/self/cgroup": "0::/init.scope" + chr(10),
                                     "sys/fs/cgroup/init.scope/memory.max": "max" + chr(10)}),
        "root": _tree("root", {"proc/self/cgroup": "0::/" + chr(10),
                               "sys/fs/cgroup/memory.max": "268435456" + chr(10)}),
        "v1": _tree("v1", {"sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712" + chr(10)}),
        "garbage": _tree("garbage", {"sys/fs/cgroup/memory.max": "lots" + chr(10)}),
        "none": _tree("none", {}),
    }
    chk("(20f) own cgroup 512 MiB, own 'max', container root 256 MiB, v1 unlimited, garbage, nothing",
        _cg == {"own": 512 << 20, "own-max": None, "root": 256 << 20, "v1": None,
                "garbage": "unknown", "none": "unknown"}, str(_cg))

    # --- (9f) the retention cannot be shortened through the environment ---------------------------
    os.environ["DENDRA_RELAY_STORE"] = os.path.join(TMP, "srv4")
    os.environ["DENDRA_RELAY_RETENTION"] = "1"
    m = importlib.reload(relay)
    chk("(9f) DENDRA_RELAY_RETENTION=1 does not go below the compiled floor",
        m.RETENTION >= m.RETENTION_FLOOR, f"RETENTION={m.RETENTION}")
    os.environ["DENDRA_RELAY_RETENTION"] = str(30 * 86400)
    m = importlib.reload(relay)
    chk("(9g) but it can be lengthened (the only direction that protects the evidence)",
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
    chk("(10) without a writable store, the relay refuses to serve (no silent memory fallback)",
        refused and not accepted, f"BOOT_ERR={relay.BOOT_ERR!r} accepted={accepted}")

    # --- (11) no environment variable can make the relay volatile ---------------------------------
    volatile = []
    for val in ("", "0", "off", "none", "false", ":memory:"):
        os.environ["DENDRA_RELAY_STORE"] = val
        try:
            m = importlib.reload(relay)
            if m.DISK is None and not m.BOOT_ERR:
                volatile.append(val)            # would serve without persistence: forbidden
        except StoreUnavailable:
            pass
    chk("(11) no value of DENDRA_RELAY_STORE disables persistence",
        not volatile, f"volatile values: {volatile}")
    # And the proof that this test stays clean: everything it created lives under TMP.
    dirt = [p for p in ("0", "off", "none", "false", ":memory:", "relay-store")
            if os.path.exists(os.path.join(HERE, p))]
    chk("(12) the test created nothing in the repository directory", not dirt, f"leftovers: {dirt}")

print(f"\n{OK[0]} green, {len(FAIL)} red")
# Every case above runs the shipped `_put`, `_boot_store` and `_stats` on a copy of the module; the
# only one that can be declared untested is (20), when the shipped compose file is not where this
# bench looks (a published tree without it). The HTTP side of the budget (507, the filtered /list) is
# test_relay_list_filter.py's.
print(f"RELAY_STORE_SUMMARY green={OK[0]} red={len(FAIL)} untested={UNTESTED[0]}")
if FAIL:
    print("RED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
