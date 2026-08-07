"""Exercises `modea.relay_write` — what is measured here is the INTERACTIONS, not the parts.

Every building block already has its own suite. This file tests what none of them can test alone:
**the order of the checks**. Two properties are verified by direct observation of the calls:

  1. an invalid signature **never reaches** the replay guard — otherwise a third party with no key
     could poison the set of fingerprints and would hold an "already seen?" oracle;
  2. a foreign key (perfectly valid signature, but address != operator) **does not reach** the replay
     guard either — attribution refuses before any state is written.

Everything runs with REAL secp256k1 keys and the real modules, not doubles: a signature double would
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
    return m, rc.empreinte(kind, key, body, miner_id, height)


LYING_SEALED_HEIGHT = 42


def canon_lying(kind, key, body, miner_id, height):
    """Seals a height DIFFERENT from the one the caller announces.

    This is not a laboratory curiosity. Any future normalisation — U64 truncation, capping, conversion
    from a string — would produce exactly this divergence, and **silently**. It is the only instrument
    that can say *which of the two values* reaches the high-water mark: as long as they agree, the
    wiring is not measured, it is assumed.
    """
    h = LYING_SEALED_HEIGHT
    return (rc.canonical_message(kind, key, body, miner_id, h),
            rc.empreinte(kind, key, body, miner_id, h))


class AntireplaySpy(ar.Antireplay):
    """Counts the calls AND records the heights received: that is how an ORDER and a PROVENANCE are
    measured — not by re-reading the code."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = 0
        self.hauteurs = []

    def verifier(self, cle, height, empreinte, tete=None):
        self.calls += 1
        self.hauteurs.append(height)
        return super().verifier(cle, height, empreinte, tete=tete)


def run_suite(verbose=True):
    greens = reds = 0

    def ok(name, cond, detail=""):
        nonlocal greens, reds
        if cond:
            greens += 1
            if verbose:
                print(f"  ✓ {name}")
        else:
            reds += 1
            if verbose:
                print(f"  ✗ {name} {detail}")

    def measure(name, f):
        """An EXCEPTION is a NAMED failure, not a suite that dies without a summary.

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
                print(f"  ✗ {name} — EXCEPTION {type(e).__name__}: {str(e)[:90]}")

    priv, pub = make_key()
    operator = ca.address_from_pubkey(pub, hrp="dendra")
    import json
    corps_reg = json.dumps({"miner": [{"miner_id": "m1", "operator": operator}]})
    registry = reg.RegistryCache(lambda: corps_reg, Clock(), expiry=300)
    registry.refresh()

    # The base kind is `reveal`, NOT `req`. A `req` deposit is attributed to the PAYING CLIENT and not
    # to the operator, so keeping `req` here would test the wrong authority in the eight sections about
    # check ordering. The `req` path has its own section (12).
    BASE = ("reveal", "job7__m1", b"charge utile", "m1", 1000)

    def signer(base=BASE, k=priv, c=canon):
        m, _ = c(*base)
        return k.sign(m, ec.ECDSA(hashes.SHA256()))

    def fresh():
        return AntireplaySpy(Clock(), retention=600, fenetre=200)

    def call(a, base=BASE, pk=pub, sig=None, tete=None, reg_=None, c=canon, **kw):
        return re_.verify_write(
            *base, pk, sig if sig is not None else signer(base, c=c),
            reg_ or registry, a, tete=tete,
            canon=c, verify_signature=verify_sig, read_height=rc.message_height,
            address_from_key=lambda p, h: ca.address_from_pubkey(p, hrp=h), **kw)

    # -- 1. the nominal path -------------------------------------------------------------------
    if verbose:
        print("1. nominal path: valid signature + operator key + fresh registry")
    a = fresh()
    r = call(a, tete=1000)
    ok("write accepted", r.accepte, f"({r.motif} {r.detail})")
    ok("the REGISTRY regime is reported", r.regime_registry == "FEES")
    ok("the REPLAY-GUARD regime is reported", r.regime_rejeu == ar.NOMINAL)
    ok("the attributed operator is reported", r.operator == operator)
    ok("`a_persister()` carries the four facts",
       set(r.a_persister()) == {"regime", "registry", "operator", "attribution"})

    # -- 2. ORDER: an invalid signature does NOT reach the replay guard -------------------------
    if verbose:
        print("2. invalid signature -> the replay guard is NEVER called (oracle + poisoning)")
    a2 = fresh()
    r2 = call(a2, sig=b"\x30\x44" + b"\x00" * 60)
    ok("refused as SIGNATURE_INVALIDE", r2.motif == re_.SIGNATURE_INVALIDE)
    ok("replay guard called 0 times", a2.calls == 0, f"(called {a2.calls} times)")
    ok("no fingerprint memorised", len(a2._vues) == 0)
    ok("no writer registered", len(a2._high) == 0)

    # -- 3. ORDER (continued): a foreign key does not reach the replay guard either -------------
    if verbose:
        print("3. foreign key (VALID signature) -> refused at attribution, before any state")
    other_priv, other_pub = make_key()
    a3 = fresh()
    r3 = call(a3, pk=other_pub, sig=signer(k=other_priv))
    ok("the signature was valid, and the write is still refused", r3.motif == re_.FOREIGN_KEY)
    ok("replay guard called 0 times", a3.calls == 0, f"(called {a3.calls} times)")
    ok("the expected operator is reported for the log", r3.operator == operator)

    # -- 4. registry: the two kinds of absence are not conflated --------------------------------
    if verbose:
        print("4. silent registry != unknown miner")
    empty = reg.RegistryCache(lambda: (_ for _ in ()).throw(OSError("link down")), Clock(), expiry=300)
    empty.refresh()
    a4 = fresh()
    r4 = call(a4, reg_=empty)
    ok("registry NEVER read -> REGISTRY_MUTE", r4.motif == re_.REGISTRY_MUTE)
    ok("and the replay guard is not reached", a4.calls == 0)
    other_reg = reg.RegistryCache(
        lambda: json.dumps({"miner": [{"miner_id": "AUTRE", "operator": operator}]}),
        Clock(), expiry=300)
    other_reg.refresh()
    r5 = call(fresh(), reg_=other_reg)
    ok("miner absent from a registry that WAS read -> MINEUR_INCONNU", r5.motif == re_.MINEUR_INCONNU)

    # -- 5. STALE registry: the write still goes through (never fail-closed) --------------------
    if verbose:
        print("5. STALE registry -> accepted, and the regime says so")
    h = Clock()
    per = reg.RegistryCache(lambda: corps_reg, h, expiry=60)
    per.refresh()
    h.t += 61
    r6 = call(fresh(), reg_=per, tete=1000)
    ok("accepted despite a stale registry", r6.accepte)
    ok("the STALE regime is reported, not masked", r6.regime_registry == "STALE")
    r7 = call(fresh(), reg_=per, tete=1000, require_fee_registry=True)
    ok("but refusable by explicit CONFIGURATION", not r7.accepte)

    # -- 6. replay, end to end -----------------------------------------------------------------
    if verbose:
        print("6. replay, end to end")
    a6 = fresh()
    ok("first write accepted", call(a6, tete=1000).accepte)
    rr = call(a6, tete=1000)
    ok("the EXACT replay is refused", (not rr.accepte) and rr.motif == ar.REJEU)
    ok("replay guard called twice (once per authenticated write)", a6.calls == 2)
    ok("unknown head: accepted in DEGRADED mode", call(fresh(), tete=None).regime_rejeu == ar.DEGRADE)

    # -- 7. shapes refused before any cryptography ----------------------------------------------
    if verbose:
        print("7. shapes refused before any cryptography")
    for name, kw in [("32-byte pubkey", {"pk": b"\x02" * 32}),
                    ("empty signature", {"sig": b""})]:
        a7 = fresh()
        r8 = call(a7, **kw)
        ok(f"{name} -> MAL_FORME, replay guard untouched",
           r8.motif == re_.MAL_FORME and a7.calls == 0)

    # -- 8. a body altered by ONE byte breaks everything ----------------------------------------
    if verbose:
        print("8. one byte of the body changes -> the signature no longer covers it")
    a8 = fresh()
    r9 = call(a8, base=("req", "job7__m1", b"charge utilE", "m1", 1000), sig=signer())
    ok("refused as SIGNATURE_INVALIDE", r9.motif == re_.SIGNATURE_INVALIDE)
    ok("replay guard untouched", a8.calls == 0)

    # -- 9. THE HEIGHT THAT REACHES THE HIGH-WATER IS THE ONE IN THE SIGNED MESSAGE --------------
    # The high-water mark anchors on the height CARRIED IN THE NOTE, never on the call sequence.
    # This section measures that wiring instead of assuming it.
    if verbose:
        print("9. the height handed to the replay guard is RE-READ from the signed bytes")
    a9 = fresh()
    r10 = call(a9, tete=1000)
    measure("the nominal path hands over a SEALED height (not a bare int)",
           lambda: isinstance(a9.hauteurs[0], ar.HauteurScellee))
    measure("and its value is the one in the message",
           lambda: int(a9.hauteurs[0]) == 1000 and r10.height == 1000)

    # The decisive case: the caller announces 1000, the signed message carries 42.
    a9b = fresh()
    r11 = call(a9b, c=canon_lying, tete=None)
    measure("the message carries 42 -> the replay guard receives 42, NOT the caller's 1000",
           lambda: int(a9b.hauteurs[0]) == LYING_SEALED_HEIGHT)
    measure("and the result logs that same height",
           lambda: r11.accepte and r11.height == LYING_SEALED_HEIGHT)
    measure("the call parameter (1000) left no trace at all",
           lambda: 1000 not in [int(x) for x in a9b.hauteurs])

    # A message whose height cannot be re-read -> a NAMED refusal, never a guessed height.
    a9c = fresh()
    r12 = re_.verify_write(
        *BASE, pub, signer(), registry, a9c, tete=None,
        canon=canon, verify_signature=verify_sig,
        read_height=lambda m: (_ for _ in ()).throw(ValueError("unknown shape")),
        address_from_key=lambda p, h: ca.address_from_pubkey(p, hrp=h))
    ok("unreadable height -> UNREADABLE_HEIGHT", r12.motif == re_.UNREADABLE_HEIGHT)
    ok("and the replay guard is not reached (nothing is written at an unknown height)",
       a9c.calls == 0)

    # -- 10. WHAT GETS PERSISTED: {mode, height} — and `null` is not `degraded` -----------------
    if verbose:
        print("10. regime persisted per entry: {mode, height}")
    p_nom = call(fresh(), tete=1000).a_persister()
    measure("mode `nominal` when the head is known", lambda: p_nom["regime"]["mode"] == "nominal")
    measure("height carries the sealed height", lambda: p_nom["regime"]["height"] == 1000)
    measure("height is a BARE int (the store does not depend on a Python type)",
           lambda: type(p_nom["regime"]["height"]) is int)
    p_deg = call(fresh(), tete=None).a_persister()
    measure("mode `degraded` when the head is unknown",
           lambda: p_deg["regime"]["mode"] == "degraded")
    measure("the block is serialisable as is", lambda: json.loads(json.dumps(p_nom)) == p_nom)

    # The property that matters: an entry refused BEFORE step 4 has NO mode at all.
    p_sig = call(fresh(), sig=b"\x30\x44" + b"\x00" * 60).a_persister()
    measure("refused before the replay guard -> mode `null`, NOT `degraded` (else it testifies falsely)",
           lambda: p_sig["regime"]["mode"] is None)
    measure("...and height `null` too: nothing was authenticated at that point",
           lambda: p_sig["regime"]["height"] is None)
    p_key = call(fresh(), pk=other_pub, sig=signer(k=other_priv)).a_persister()
    measure("refused at attribution: mode `null` but height KNOWN (signature already verified)",
           lambda: p_key["regime"]["mode"] is None and p_key["regime"]["height"] == 1000)

    # -- 11. THE COUNT PER MODE — a per-entry regime does not say HOW MANY ----------------------
    if verbose:
        print("11. summary: the COUNT per mode, and no exit path that escapes it")
    s = re_.Summary()
    a11 = fresh()
    call(a11, tete=1000, summary=s)                                  # accepted, nominal
    call(fresh(), base=("reveal", "j2", b"c2", "m1", 1000), tete=None, summary=s)  # accepted, degraded
    call(a11, tete=1000, summary=s)                                  # replay -> refused, NOMINAL
    call(fresh(), sig=b"\x30\x44" + b"\x00" * 60, summary=s)          # refused before step 4
    measure("accepted writes counted PER MODE",
           lambda: s.accepted["nominal"] == 1 and s.accepted["degraded"] == 1)
    measure("refusals counted per mode as well", lambda: s.refused["nominal"] == 1)
    measure("refusals raised before the replay guard land in `no_regime`, not in `degraded`",
           lambda: s.no_regime == 1 and s.refused["degraded"] == 0)
    measure("reasons are counted", lambda: s.reasons.get(re_.SIGNATURE_INVALIDE) == 1)
    measure("the total covers every entry", lambda: s.total == 4)
    lr = s.render()
    measure("summary line is stable and flat",
           lambda: all(x in lr for x in ("WRITE_SUMMARY", "acc_nominal=", "acc_degraded=",
                                         "ref_nominal=", "no_regime=", "reasons[")))
    if verbose:
        print(f"      {lr}")

    # The structural guarantee: counting lives in the wrapper, not in each `return`.
    s2 = re_.Summary()
    for kw in ({"pk": b"\x02" * 32}, {"sig": b""}, {"reg_": empty}, {"tete": 1000}):
        call(fresh(), summary=s2, **kw)
    ok("4 different exit paths, 4 entries counted — none escapes", s2.total == 4)

    # -- 12. a `req` is attributed to the PAYING CLIENT, never to an operator -------------------
    # The `req` deposit is bound to the job's paying client. The property that matters is not that a
    # client can write — it is that a PERFECTLY LEGITIMATE operator cannot.
    if verbose:
        print("12. the `req` belongs to the job's paying client")
    rj = _imp("job_registry")
    cli_priv, cli_pub = make_key()
    client_addr = ca.address_from_pubkey(cli_pub, hrp="dendra")
    ok("client and operator are two DIFFERENT keys (otherwise the test proves nothing)",
       client_addr != operator)
    corps_jobs = json.dumps({"job": [{"job_id": "job7", "client": client_addr},
                                     {"job_id": "vide", "client": ""}]})
    jobs = rj.JobRegistry(lambda: corps_jobs, Clock(), expiry=300)
    jobs.refresh()
    REQ = ("req", "job7__m1", b"charge utile", "m1", 1000)

    def call_req(a, base=REQ, pk=cli_pub, k=cli_priv, sig=None, tete=None, jobs_=jobs, **kw):
        return re_.verify_write(
            *base, pk, sig if sig is not None else signer(base, k=k),
            registry, a, tete=tete, canon=canon, verify_signature=verify_sig,
            read_height=rc.message_height, job_registry=jobs_, job_from_key=rj.job_from_key,
            address_from_key=lambda p, h: ca.address_from_pubkey(p, hrp=h), **kw)

    measure("the role is read from a TABLE, not from a scattered `if`",
           lambda: (re_.role_du_kind("req"), re_.role_du_kind("reveal"),
                    re_.role_du_kind("inconnu")) == ("client", "operator", "operator"))
    r12 = call_req(fresh(), tete=1000)
    ok("the paying client writes its own `req`", r12.accepte, f"({r12.motif} {r12.detail})")
    ok("attribution names both the role AND the address",
       r12.role == "client" and r12.client == client_addr and r12.operator is None)
    measure("`a_persister()` carries the attribution",
           lambda: r12.a_persister()["attribution"] == {"role": "client", "adresse": client_addr})

    # THE property of this binding.
    r13 = call_req(fresh(), pk=pub, k=priv, tete=1000)
    ok("the miner's OPERATOR — legitimate elsewhere — is REFUSED on a `req`",
       (not r13.accepte) and r13.motif == re_.CLIENT_ETRANGER)
    ok("and the reason differs from FOREIGN_KEY (two bindings, two investigations)",
       re_.CLIENT_ETRANGER != re_.FOREIGN_KEY)
    a13 = fresh(); call_req(a13, pk=pub, k=priv, tete=1000)
    ok("the replay guard is not reached by a foreign client", a13.calls == 0)

    # The fallback that does not exist: with no job registry wired, the write is REFUSED.
    # `measure` rather than `ok`: without the guard the call dereferences `None` and the suite DIES
    # without its summary line.
    measure("no job registry wired -> REFUSAL, never a fallback onto the operator",
           lambda: (lambda x: (not x.accepte) and x.motif == re_.JOB_REGISTRY_MUTE)(
               call_req(fresh(), jobs_=None, tete=1000)))
    mute = rj.JobRegistry(lambda: (_ for _ in ()).throw(OSError("link down")), Clock(), expiry=300)
    mute.refresh()
    ok("job list NEVER read -> JOB_REGISTRY_MUTE",
       call_req(fresh(), jobs_=mute).motif == re_.JOB_REGISTRY_MUTE)
    ok("job absent from a list that WAS read -> JOB_INCONNU (distinct from never read)",
       call_req(fresh(), base=("req", "job99__m1", b"c", "m1", 1000)).motif == re_.JOB_INCONNU)
    ok("a job with an EMPTY `client` is dropped at extraction, not stored empty",
       call_req(fresh(), base=("req", "vide__m1", b"c", "m1", 1000)).motif == re_.JOB_INCONNU)

    # The naming convention is VERIFIED, not merely split on.
    ok("key whose tail != miner_id -> MAL_FORME",
       call_req(fresh(), base=("req", "job7__AUTRE", b"c", "m1", 1000)).motif == re_.MAL_FORME)
    ok("key without a separator -> MAL_FORME",
       call_req(fresh(), base=("req", "job7", b"c", "m1", 1000)).motif == re_.MAL_FORME)
    measure("`job_from_key` splits on the LAST `__` (a jobId may contain one)",
           lambda: rj.job_from_key("a__b__m1", "m1") == "a__b")
    measure("and it RAISES when the tail lies", lambda: _raises_v(lambda: rj.job_from_key("j__x", "m1")))

    # -- 13. truncation is PROVEN by `pagination.next_key`, and a lookup by id bypasses it ------
    # Read at the source: `query_job.go` paginates (ListJob via query.CollectionPaginate) and exposes
    # `GetJob` by id; `query.proto` declares the route `/dendra/jobs/v1/job/{job_id}`. The risk of an
    # unpaginated read therefore lives in the consumer, not in the chain.
    if verbose:
        print("13. truncation PROVEN by `next_key`, and a lookup by id that bypasses it")
    measure("complete list -> liste_tronquee() false", lambda: jobs.liste_tronquee() is False)
    corps_page = json.dumps({"job": [{"job_id": "job7", "client": client_addr}],
                             "pagination": {"next_key": "c29tZS1jbGU="}})
    truncated = rj.JobRegistry(lambda: corps_page, Clock(), expiry=300)
    truncated.refresh()
    measure("non-empty `next_key` -> liste_tronquee() TRUE (proof, not suspicion)",
           lambda: truncated.liste_tronquee() is True)
    measure("the refusal reason SAYS that the list announced a continuation",
           lambda: "UNREAD CONTINUATION" in call_req(fresh(), base=("req", "absent__m1", b"c", "m1", 1000),
                                        jobs_=truncated).detail)
    # The lookup by id: a cache miss is no longer a verdict.
    calls_id = []

    def lecteur_job(jid):
        calls_id.append(jid)
        return json.dumps({"job": {"job_id": jid, "client": client_addr}}) if jid == "tard" else "{}"

    par_id = rj.JobRegistry(lambda: corps_jobs, Clock(), expiry=300, lecteur_job=lecteur_job)
    par_id.refresh()
    r15 = call_req(fresh(), base=("req", "tard__m1", b"c", "m1", 1000), jobs_=par_id, tete=1000)
    ok("job absent from the cache but resolved BY ITS ID -> accepted", r15.accepte,
       f"({r15.motif} {r15.detail})")
    ok("the lookup by id was attempted exactly once", calls_id == ["tard"])
    measure("a job unreachable even by id stays JOB_INCONNU",
           lambda: call_req(fresh(), base=("req", "never__m1", b"c", "m1", 1000),
                             jobs_=par_id).motif == re_.JOB_INCONNU)
    measure("an id reader that fails does not raise — it is COUNTED",
           lambda: (lambda rg: (rg.client("x"), rg.lookups_ko == 1)[1])(
               rj.JobRegistry(lambda: corps_jobs, Clock(), expiry=300,
                               lecteur_job=lambda j: (_ for _ in ()).throw(OSError("link down")))))
    measure("the job registry summary line is stable",
           lambda: all(x in par_id.resume() for x in
                       ("JOB_REGISTRY", "jobs=", "age=", "tronquee=", "lookups=", "par_id=")))

    return greens, reds


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
