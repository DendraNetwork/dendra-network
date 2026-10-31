"""Exercises `modea.relay_write` — what is measured here is the interactions, not the parts.

Every building block already has its own suite. This file tests what none of them can test alone:
the order of the checks. Two properties are verified by direct observation of the calls:

  1. an invalid signature never reaches the replay guard — otherwise a third party with no key
     could poison the set of fingerprints and would hold an "already seen?" oracle;
  2. a foreign key (perfectly valid signature, but address != operator) does not reach the replay
     guard either — attribution refuses before any state is written.

Everything runs with real secp256k1 keys and the real modules, not doubles: a signature double would
prove that the wiring compiles, not that it holds.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _imp(name):
    try:
        return __import__(f"modea.{name}", fromlist=["*"])
    except ImportError:
        return __import__(name)


ca = _imp("cosmos_addr")
rc = _imp("relay_canon")
ar = _imp("relay_antireplay")
re_ = _imp("relay_write")
reg = _imp("registry_cache")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_key():
    p = ec.generate_private_key(ec.SECP256K1())
    pub = p.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return p, pub


def verify_sig(pubkey, message, signature):
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), pubkey).verify(
            signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:  # noqa: BLE001
        return False


def canon(kind, key, body, miner_id, height):
    m = rc.canonical_message(kind, key, body, miner_id, height)
    return m, rc.digest(kind, key, body, miner_id, height)


LYING_SEALED_HEIGHT = 42


def canon_lying(kind, key, body, miner_id, height):
    """Seals a height different from the one the caller announces.

    This is not a laboratory curiosity. Any future normalisation — U64 truncation, capping, conversion
    from a string — would produce exactly this divergence, and silently. It is the only instrument
    that can say *which of the two values* reaches the high-water mark: as long as they agree, nothing
    measures the wiring.
    """
    h = LYING_SEALED_HEIGHT
    return (rc.canonical_message(kind, key, body, miner_id, h),
            rc.digest(kind, key, body, miner_id, h))


class AntireplaySpy(ar.Antireplay):
    """Counts the calls and records the heights received: that is how an order and a provenance are
    measured — not by re-reading the code."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = 0
        self.heights = []

    def verify(self, key, height, digest, head=None):
        self.calls += 1
        self.heights.append(height)
        return super().verify(key, height, digest, head=head)


def run_suite(verbose=True):
    greens = reds = 0

    def ok(name, cond, detail=""):
        nonlocal greens, reds
        if cond:
            greens += 1
            if verbose:
                print(f"  OK   {name}")
        else:
            reds += 1
            if verbose:
                print(f"  FAIL {name} {detail}")

    def measure(name, f):
        """An exception is a named failure, not a suite that dies without a summary.

        Sabotaging the persisted format raises a `KeyError`, and the suite then stops **without
        printing its summary line**. A failure that is not counted looks far too much like a pass —
        especially from a runner that only reads the final line.
        """
        try:
            ok(name, f())
        except Exception as e:  # noqa: BLE001
            nonlocal reds
            reds += 1
            if verbose:
                print("  FAIL", f"{name}: exception {type(e).__name__}: {str(e)[:90]}")

    priv, pub = make_key()
    operator = ca.address_from_pubkey(pub, hrp="dendra")
    import json
    reg_body = json.dumps({"miner": [{"miner_id": "m1", "operator": operator}]})
    registry = reg.RegistryCache(lambda: reg_body, Clock(), expiry=300)
    registry.refresh()

    # The base kind is `reveal`, not `req`. A `req` deposit is attributed to the paying client and not
    # to the operator, so keeping `req` here would test the wrong authority in the eight sections about
    # check ordering. The `req` path has its own section (12).
    BASE = ("reveal", "job7__m1", b"payload", "m1", 1000)

    def signer(base=BASE, k=priv, c=canon):
        m, _ = c(*base)
        return k.sign(m, ec.ECDSA(hashes.SHA256()))

    def fresh():
        return AntireplaySpy(Clock(), retention=600, window=200)

    def call(a, base=BASE, pk=pub, sig=None, head=None, reg_=None, c=canon, **kw):
        return re_.verify_write(
            *base, pk, sig if sig is not None else signer(base, c=c),
            reg_ or registry, a, head=head,
            canon=c, verify_signature=verify_sig, read_height=rc.message_height,
            address_from_key=lambda p, h: ca.address_from_pubkey(p, hrp=h), **kw)

    # -- 1. the nominal path -------------------------------------------------------------------
    if verbose:
        print("1. nominal path: valid signature + operator key + fresh registry")
    a = fresh()
    r = call(a, head=1000)
    ok("write accepted", r.accepted, f"({r.reason} {r.detail})")
    ok("the registry regime is reported", r.regime_registry == "FEES")
    ok("the replay-guard regime is reported", r.replay_regime == ar.NOMINAL)
    ok("the attributed operator is reported", r.operator == operator)
    ok("`to_persist()` carries the four facts",
       set(r.to_persist()) == {"regime", "registry", "operator", "attribution"})

    # -- 2. Order: an invalid signature does not reach the replay guard -------------------------
    if verbose:
        print("2. invalid signature -> the replay guard is never called (oracle + poisoning)")
    a2 = fresh()
    r2 = call(a2, sig=b"\x30\x44" + b"\x00" * 60)
    ok("refused as INVALID_SIGNATURE", r2.reason == re_.INVALID_SIGNATURE)
    ok("replay guard called 0 times", a2.calls == 0, f"(called {a2.calls} times)")
    ok("no fingerprint memorised", len(a2._seen) == 0)
    ok("no writer registered", len(a2._high) == 0)

    # -- 3. Order (continued): a foreign key does not reach the replay guard either -------------
    if verbose:
        print("3. foreign key (valid signature) -> refused at attribution, before any state")
    other_priv, other_pub = make_key()
    a3 = fresh()
    r3 = call(a3, pk=other_pub, sig=signer(k=other_priv))
    ok("the signature was valid, and the write is still refused", r3.reason == re_.FOREIGN_KEY)
    ok("replay guard called 0 times", a3.calls == 0, f"(called {a3.calls} times)")
    ok("the expected operator is reported for the log", r3.operator == operator)

    # -- 4. registry: the two kinds of absence are not conflated --------------------------------
    if verbose:
        print("4. silent registry != unknown miner")
    empty = reg.RegistryCache(lambda: (_ for _ in ()).throw(OSError("link down")), Clock(), expiry=300)
    empty.refresh()
    a4 = fresh()
    r4 = call(a4, reg_=empty)
    ok("registry never read -> REGISTRY_MUTE", r4.reason == re_.REGISTRY_MUTE)
    ok("and the replay guard is not reached", a4.calls == 0)
    other_reg = reg.RegistryCache(
        lambda: json.dumps({"miner": [{"miner_id": "OTHER", "operator": operator}]}),
        Clock(), expiry=300)
    other_reg.refresh()
    r5 = call(fresh(), reg_=other_reg)
    ok("miner absent from a registry that was read -> UNKNOWN_MINER", r5.reason == re_.UNKNOWN_MINER)

    # -- 5. Stale registry: the write still goes through (never fail-closed) --------------------
    if verbose:
        print("5. STALE registry -> accepted, and the regime says so")
    h = Clock()
    per = reg.RegistryCache(lambda: reg_body, h, expiry=60)
    per.refresh()
    h.t += 61
    r6 = call(fresh(), reg_=per, head=1000)
    ok("accepted despite a stale registry", r6.accepted)
    ok("the STALE regime is reported, not masked", r6.regime_registry == "STALE")
    r7 = call(fresh(), reg_=per, head=1000, require_fee_registry=True)
    ok("but refusable by explicit configuration", not r7.accepted)

    # -- 6. replay, end to end -----------------------------------------------------------------
    if verbose:
        print("6. replay, end to end")
    a6 = fresh()
    ok("first write accepted", call(a6, head=1000).accepted)
    rr = call(a6, head=1000)
    ok("the exact replay is refused", (not rr.accepted) and rr.reason == ar.REPLAY)
    ok("replay guard called twice (once per authenticated write)", a6.calls == 2)
    ok("unknown head: accepted in DEGRADED mode", call(fresh(), head=None).replay_regime == ar.DEGRADED)

    # -- 7. shapes refused before any cryptography ----------------------------------------------
    if verbose:
        print("7. shapes refused before any cryptography")
    for name, kw in [("32-byte pubkey", {"pk": b"\x02" * 32}),
                    ("empty signature", {"sig": b""})]:
        a7 = fresh()
        r8 = call(a7, **kw)
        ok(f"{name} -> MALFORMED, replay guard untouched",
           r8.reason == re_.MALFORMED and a7.calls == 0)

    # -- 8. a body altered by one byte breaks everything ----------------------------------------
    if verbose:
        print("8. one byte of the body changes -> the signature no longer covers it")
    a8 = fresh()
    r9 = call(a8, base=("req", "job7__m1", b"payloaD", "m1", 1000), sig=signer())
    ok("refused as INVALID_SIGNATURE", r9.reason == re_.INVALID_SIGNATURE)
    ok("replay guard untouched", a8.calls == 0)

    # -- 9. The height that reaches the high-water is the one in the signed message --------------
    # The high-water mark anchors on the height carried in the note, never on the call sequence.
    # This section measures that wiring instead of assuming it.
    if verbose:
        print("9. the height handed to the replay guard is re-read from the signed bytes")
    a9 = fresh()
    r10 = call(a9, head=1000)
    measure("the nominal path hands over a sealed height (not a bare int)",
           lambda: isinstance(a9.heights[0], ar.SealedHeight))
    measure("and its value is the one in the message",
           lambda: int(a9.heights[0]) == 1000 and r10.height == 1000)

    # The decisive case: the caller announces 1000, the signed message carries 42.
    a9b = fresh()
    r11 = call(a9b, c=canon_lying, head=None)
    measure("the message carries 42 -> the replay guard receives 42, not the caller's 1000",
           lambda: int(a9b.heights[0]) == LYING_SEALED_HEIGHT)
    measure("and the result logs that same height",
           lambda: r11.accepted and r11.height == LYING_SEALED_HEIGHT)
    measure("the call parameter (1000) left no trace at all",
           lambda: 1000 not in [int(x) for x in a9b.heights])

    # A message whose height cannot be re-read -> a named refusal, never a guessed height.
    a9c = fresh()
    r12 = re_.verify_write(
        *BASE, pub, signer(), registry, a9c, head=None,
        canon=canon, verify_signature=verify_sig,
        read_height=lambda m: (_ for _ in ()).throw(ValueError("unknown shape")),
        address_from_key=lambda p, h: ca.address_from_pubkey(p, hrp=h))
    ok("unreadable height -> UNREADABLE_HEIGHT", r12.reason == re_.UNREADABLE_HEIGHT)
    ok("and the replay guard is not reached (nothing is written at an unknown height)",
       a9c.calls == 0)

    # -- 10. What gets persisted: {mode, height} — and `null` is not `degraded` -----------------
    if verbose:
        print("10. regime persisted per entry: {mode, height}")
    p_nominal = call(fresh(), head=1000).to_persist()
    measure("mode `nominal` when the head is known", lambda: p_nominal["regime"]["mode"] == "nominal")
    measure("height carries the sealed height", lambda: p_nominal["regime"]["height"] == 1000)
    measure("height is a bare int (the store does not depend on a Python type)",
           lambda: type(p_nominal["regime"]["height"]) is int)
    p_deg = call(fresh(), head=None).to_persist()
    measure("mode `degraded` when the head is unknown",
           lambda: p_deg["regime"]["mode"] == "degraded")
    measure("the block is serialisable as is", lambda: json.loads(json.dumps(p_nominal)) == p_nominal)

    # The property that matters: an entry refused before step 4 has no mode at all.
    p_sig = call(fresh(), sig=b"\x30\x44" + b"\x00" * 60).to_persist()
    measure("refused before the replay guard -> mode `null`, not `degraded` (else it testifies falsely)",
           lambda: p_sig["regime"]["mode"] is None)
    measure("...and height `null` too: nothing was authenticated at that point",
           lambda: p_sig["regime"]["height"] is None)
    p_key = call(fresh(), pk=other_pub, sig=signer(k=other_priv)).to_persist()
    measure("refused at attribution: mode `null` but height known (signature already verified)",
           lambda: p_key["regime"]["mode"] is None and p_key["regime"]["height"] == 1000)

    # -- 11. The count per mode — a per-entry regime does not say how many ----------------------
    if verbose:
        print("11. summary: the count per mode, and no exit path that escapes it")
    s = re_.Summary()
    a11 = fresh()
    call(a11, head=1000, summary=s)                                  # accepted, nominal
    call(fresh(), base=("reveal", "j2", b"c2", "m1", 1000), head=None, summary=s)  # accepted, degraded
    call(a11, head=1000, summary=s)                                  # replay -> refused, NOMINAL
    call(fresh(), sig=b"\x30\x44" + b"\x00" * 60, summary=s)          # refused before step 4
    measure("accepted writes counted per mode",
           lambda: s.accepted["nominal"] == 1 and s.accepted["degraded"] == 1)
    measure("refusals counted per mode as well", lambda: s.refused["nominal"] == 1)
    measure("refusals raised before the replay guard land in `no_regime`, not in `degraded`",
           lambda: s.no_regime == 1 and s.refused["degraded"] == 0)
    measure("reasons are counted", lambda: s.reasons.get(re_.INVALID_SIGNATURE) == 1)
    measure("the total covers every entry", lambda: s.total == 4)
    lr = s.render()
    measure("summary line is stable and flat",
           lambda: all(x in lr for x in ("WRITE_SUMMARY", "acc_nominal=", "acc_degraded=",
                                         "ref_nominal=", "no_regime=", "reasons[")))
    if verbose:
        print(f"      {lr}")

    # The structural guarantee: counting lives in the wrapper, not in each `return`.
    s2 = re_.Summary()
    for kw in ({"pk": b"\x02" * 32}, {"sig": b""}, {"reg_": empty}, {"head": 1000}):
        call(fresh(), summary=s2, **kw)
    ok("4 different exit paths, 4 entries counted — none escapes", s2.total == 4)

    # -- 12. a `req` is attributed to the paying client, never to an operator -------------------
    # The `req` deposit is bound to the job's paying client. The property that matters is not that a
    # client can write — it is that a perfectly legitimate operator cannot.
    if verbose:
        print("12. the `req` belongs to the job's paying client")
    rj = _imp("job_registry")
    cli_priv, cli_pub = make_key()
    client_addr = ca.address_from_pubkey(cli_pub, hrp="dendra")
    ok("client and operator are two different keys (otherwise the test proves nothing)",
       client_addr != operator)
    jobs_body = json.dumps({"job": [{"job_id": "job7", "client": client_addr},
                                     {"job_id": "empty", "client": ""}]})
    jobs = rj.JobRegistry(lambda: jobs_body, Clock(), expiry=300)
    jobs.refresh()
    REQ = ("req", "job7__m1", b"payload", "m1", 1000)

    def call_req(a, base=REQ, pk=cli_pub, k=cli_priv, sig=None, head=None, jobs_=jobs, **kw):
        return re_.verify_write(
            *base, pk, sig if sig is not None else signer(base, k=k),
            registry, a, head=head, canon=canon, verify_signature=verify_sig,
            read_height=rc.message_height, job_registry=jobs_, job_from_key=rj.job_from_key,
            address_from_key=lambda p, h: ca.address_from_pubkey(p, hrp=h), **kw)

    measure("the role is read from a table, not from a scattered `if`",
           lambda: (re_.role_of_kind("req"), re_.role_of_kind("reveal"),
                    re_.role_of_kind("unknown")) == ("client", "operator", "operator"))
    r12 = call_req(fresh(), head=1000)
    ok("the paying client writes its own `req`", r12.accepted, f"({r12.reason} {r12.detail})")
    ok("attribution names both the role and the address",
       r12.role == "client" and r12.client == client_addr and r12.operator is None)
    measure("`to_persist()` carries the attribution",
           lambda: r12.to_persist()["attribution"] == {"role": "client", "address": client_addr})

    # The property of this binding.
    r13 = call_req(fresh(), pk=pub, k=priv, head=1000)
    ok("the miner's operator, legitimate elsewhere, is refused on a `req`",
       (not r13.accepted) and r13.reason == re_.FOREIGN_CLIENT)
    ok("and the reason differs from FOREIGN_KEY (two bindings, two investigations)",
       re_.FOREIGN_CLIENT != re_.FOREIGN_KEY)
    a13 = fresh(); call_req(a13, pk=pub, k=priv, head=1000)
    ok("the replay guard is not reached by a foreign client", a13.calls == 0)

    # The fallback that does not exist: with no job registry wired, the write is refused.
    # `measure` rather than `ok`: without the guard the call dereferences `None` and the suite dies
    # without its summary line.
    measure("no job registry wired -> refusal, never a fallback onto the operator",
           lambda: (lambda x: (not x.accepted) and x.reason == re_.JOB_REGISTRY_MUTE)(
               call_req(fresh(), jobs_=None, head=1000)))
    mute = rj.JobRegistry(lambda: (_ for _ in ()).throw(OSError("link down")), Clock(), expiry=300)
    mute.refresh()
    ok("job list never read -> JOB_REGISTRY_MUTE",
       call_req(fresh(), jobs_=mute).reason == re_.JOB_REGISTRY_MUTE)
    ok("job absent from a list that was read -> UNKNOWN_JOB (distinct from never read)",
       call_req(fresh(), base=("req", "job99__m1", b"c", "m1", 1000)).reason == re_.UNKNOWN_JOB)
    ok("a job with an empty `client` is dropped at extraction, not stored empty",
       call_req(fresh(), base=("req", "empty__m1", b"c", "m1", 1000)).reason == re_.UNKNOWN_JOB)

    # The naming convention is verified, not merely split on.
    ok("key whose tail != miner_id -> MALFORMED",
       call_req(fresh(), base=("req", "job7__OTHER", b"c", "m1", 1000)).reason == re_.MALFORMED)
    ok("key without a separator -> MALFORMED",
       call_req(fresh(), base=("req", "job7", b"c", "m1", 1000)).reason == re_.MALFORMED)
    measure("`job_from_key` splits on the last `__` (a jobId may contain one)",
           lambda: rj.job_from_key("a__b__m1", "m1") == "a__b")
    measure("and it raises when the tail lies", lambda: _raises_v(lambda: rj.job_from_key("j__x", "m1")))

    # -- 13. truncation is proven by `pagination.next_key`, and a lookup by id bypasses it ------
    # Read at the source: `query_job.go` paginates (ListJob via query.CollectionPaginate) and exposes
    # `GetJob` by id; `query.proto` declares the route `/dendra/jobs/v1/job/{job_id}`. The risk of an
    # unpaginated read therefore lives in the consumer, not in the chain.
    if verbose:
        print("13. truncation proven by `next_key`, and a lookup by id that bypasses it")
    measure("complete list -> list_truncated() false", lambda: jobs.list_truncated() is False)
    page_body = json.dumps({"job": [{"job_id": "job7", "client": client_addr}],
                            "pagination": {"next_key": "c29tZS1jbGU="}})
    truncated = rj.JobRegistry(lambda: page_body, Clock(), expiry=300)
    truncated.refresh()
    measure("non-empty `next_key` -> list_truncated() true (proof, not suspicion)",
           lambda: truncated.list_truncated() is True)
    measure("the refusal reason says that the list announced a continuation",
           lambda: "unread continuation" in call_req(fresh(), base=("req", "absent__m1", b"c", "m1", 1000),
                                        jobs_=truncated).detail)
    measure("a list read as complete says so, which is the answer the next two cases must never fake",
           lambda: "was complete" in call_req(fresh(), base=("req", "absent__m1", b"c", "m1", 1000)).detail)
    # `[]` and not a string: it is the falsy wrong type that a truthiness test would fold into "absent".
    bad_page = rj.JobRegistry(lambda: json.dumps({"job": [{"job_id": "job7", "client": client_addr}],
                                                  "pagination": []}), Clock(), expiry=300)
    measure("a `pagination` that is not an object fails the read -- never a list announced complete",
           lambda: bad_page.refresh() is False and bad_page.state() == "NEVER_READ")

    # No default on the truncation. A registry that cannot answer the question must not be read as a
    # complete list: the call is direct, so a missing method is an error, never "no continuation".
    class _NoTruncationAnswer:
        def client(self, job_id):
            return None, "FEES"

    class _NonBoolAnswer(_NoTruncationAnswer):
        def list_truncated(self):
            return None

    measure("a job registry without list_truncated() raises -- never a list read 'complete'",
           lambda: _raises_any(lambda: call_req(fresh(), base=("req", "absent__m1", b"c", "m1", 1000),
                                                jobs_=_NoTruncationAnswer()), AttributeError))
    measure("list_truncated() answering None raises -- a falsy answer is not a 'no'",
           lambda: _raises_any(lambda: call_req(fresh(), base=("req", "absent__m1", b"c", "m1", 1000),
                                                jobs_=_NonBoolAnswer()), TypeError))
    # The lookup by id: a cache miss is no longer a verdict.
    calls_id = []

    def job_reader(jid):
        calls_id.append(jid)
        return json.dumps({"job": {"job_id": jid, "client": client_addr}}) if jid == "late" else "{}"

    by_id = rj.JobRegistry(lambda: jobs_body, Clock(), expiry=300, job_reader=job_reader)
    by_id.refresh()
    r15 = call_req(fresh(), base=("req", "late__m1", b"c", "m1", 1000), jobs_=by_id, head=1000)
    ok("job absent from the cache but resolved by its ID -> accepted", r15.accepted,
       f"({r15.reason} {r15.detail})")
    ok("the lookup by id was attempted exactly once", calls_id == ["late"])
    measure("a job unreachable even by id stays UNKNOWN_JOB",
           lambda: call_req(fresh(), base=("req", "never__m1", b"c", "m1", 1000),
                             jobs_=by_id).reason == re_.UNKNOWN_JOB)
    measure("an id reader that fails does not raise — it is counted",
           lambda: (lambda rg: (rg.client("x"), rg.lookups_failed == 1)[1])(
               rj.JobRegistry(lambda: jobs_body, Clock(), expiry=300,
                               job_reader=lambda j: (_ for _ in ()).throw(OSError("link down")))))
    measure("the job registry summary line is stable",
           lambda: all(x in by_id.summary() for x in
                       ("JOB_REGISTRY", "jobs=", "age=", "truncated=", "lookups=", "by_id=")))
    measure("the summary says `truncated=yes` on a list that announced a continuation",
           lambda: "truncated=yes" in truncated.summary() and "truncated=no" in jobs.summary())

    # -- 14. the kit floor: read from the signed body, after the signature ----
    # and the attribution, before the replay guard; dormant with no floor; the gateway's `req` is not
    # held to it; a body that declares nothing is refused by name once the floor is armed.
    if verbose:
        print("14. the kit floor: STALE_KIT from the signed body, dormant without a floor")

    def body(v):
        return json.dumps({"nonce": "aa", "ct": "bb", "kit_version": v}).encode()

    old = ("reveal", "job14__m1", body(1), "m1", 1000)
    cur = ("reveal", "job14__m1", body(2), "m1", 1000)
    mute = ("reveal", "job14__m1", b'{"nonce":"aa","ct":"bb"}', "m1", 1000)
    ok("dormant (no floor): a body without kit_version is accepted",
       call(fresh(), base=mute, head=1000).accepted)
    ok("dormant: a body below any future floor is accepted", call(fresh(), base=old, head=1000).accepted)
    a14 = fresh()
    r14 = call(a14, base=old, head=1000, kit_floor=2)
    ok("floor 2: kit_version 1 -> STALE_KIT", r14.reason == re_.STALE_KIT, f"({r14.reason})")
    ok("...the writer is still attributed in the refusal (the log names who is behind)", r14.operator == operator)
    ok("...and the replay guard was not reached", a14.calls == 0, f"(called {a14.calls} times)")
    a14b = fresh()
    r14b = call(a14b, base=mute, head=1000, kit_floor=2)
    ok("floor 2: no kit_version in the body -> STALE_KIT, never read as current",
       r14b.reason == re_.STALE_KIT, f"({r14b.reason})")
    ok("...nothing written either", a14b.calls == 0)
    ok("floor 2: a body that is not JSON declares nothing -> STALE_KIT",
       call(fresh(), base=BASE, head=1000, kit_floor=2).reason == re_.STALE_KIT)
    ok("floor 2: kit_version 2 -> accepted", call(fresh(), base=cur, head=1000, kit_floor=2).accepted)
    ok("floor 2: a kit ahead of the floor -> accepted",
       call(fresh(), base=("reveal", "job14__m1", body(9), "m1", 1000), head=1000, kit_floor=2).accepted)
    ok("an invalid signature over a stale body is INVALID_SIGNATURE, not STALE_KIT (the floor reads "
       "signed bytes only)",
       call(fresh(), base=old, sig=b"\x30\x44" + b"\x00" * 60, kit_floor=2).reason == re_.INVALID_SIGNATURE)
    ok("a foreign key over a stale body is FOREIGN_KEY (attribution first)",
       call(fresh(), base=old, pk=other_pub, sig=signer(old, k=other_priv), kit_floor=2).reason
       == re_.FOREIGN_KEY)
    r14r = call_req(fresh(), head=1000, kit_floor=2)
    ok("the gateway's `req` carries no kit and is not held to the floor", r14r.accepted,
       f"({r14r.reason} {r14r.detail})")
    for raw, why in ((b'{"kit_version": true}', "a boolean"), (b'{"kit_version": "2"}', "a string"),
                     (b'{"kit_version": -1}', "a negative"), (b'[2]', "a list")):
        ok(f"declared_kit_version: {why} declares nothing", re_.declared_kit_version(raw) is None)
    ok("declared_kit_version reads the integer", re_.declared_kit_version(body(7)) == 7)

    return greens, reds


def _raises_any(f, kind):
    try:
        f()
        return False
    except kind:
        return True
    except Exception:  # noqa: BLE001
        return False


def _raises_v(f):
    try:
        f()
        return False
    except ValueError:
        return True
    except Exception:  # noqa: BLE001
        return False


def test_relay_write():
    _, r = run_suite(verbose=False)
    assert r == 0, f"{r} check(s) failed — run this file directly for the detail"


if __name__ == "__main__":
    v, r = run_suite()
    print()
    print(f"SUMMARY_WRITE green={v} red={r}")
    sys.exit(1 if r else 0)
