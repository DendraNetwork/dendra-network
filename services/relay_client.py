"""Minimal HTTP client for the relay (L4) -- stdlib only (urllib). Carries JSON.

The keys passed in are already strings: for req/res we use "<jid>__<mid>", for pub "<mid>".
We only send CIPHERTEXT (req/res) or PUBLIC keys (pub): never plaintext.
"""
from __future__ import annotations

import json
import time
import urllib.request
import os

_TOKEN = os.environ.get("DENDRA_RELAY_TOKEN", "")

# AN AUTHENTICATION REFUSAL MUST BE VISIBLE — ONCE PER ROUTE.
# Every function below swallows its exceptions and returns None/False/b"". That is deliberate: the
# relay is best effort and an outage must not kill the miner. But it also made a 401 INDISTINGUISHABLE
# from "no job available". A miner with no token would query a public relay running `auth=ON`, receive
# 401 on every call, and display "waiting for jobs" forever: GPU running, on-chain registration valid,
# zero work, no message. The refusal is therefore logged instead of being silently discarded.
#
# ⛔ BUT A REFUSAL SPEAKS FOR ITS OWN ROUTE, AND FOR NO OTHER. One sentence cannot serve four call
# sites, because the relay does not decide access for the service — it decides PER ROUTE
# (`relay.py::_politique`). Reads of ciphertext are open, since a token in front of bodies
# sealed to the on-chain X25519 key protects nothing the design relies on; only writes need a writer
# with a name. On such a relay a miner with a wrong token reads `GET /list` and `GET /req` normally
# and is refused only on `POST`. Ending those refusals with "no job will be received" therefore sends
# an operator hunting a token for a queue nobody closed, and leaves the real cost of a wrong token
# unnamed: `res` and `reveal` are writes, so a job can be COMPUTED and never DELIVERED.
#
# ⛔ AND THE FLOOD BOUND MUST BE KEYED PER ROUTE, IN THE ONE DIRECTION THAT HIDES THINGS. A single
# global flag lets the FIRST refusal mute every later one — and the first a miner takes is the
# startup `POST pub`, the cheapest of them. A relay that also closed `GET /list`, the only call that
# tells a miner a job waits for it (`miner.py::main` opens its loop with `relay.listing`),
# would then be refused IN SILENCE behind a message about a pubkey deposit: the least informative
# refusal muting the most informative. The bound stays — the logs must not flood — but it is keyed
# on (method, route), so each distinct refusal is said once and no route speaks for another.
_AUTH_WARNED = set()

# What DEPENDS on each route, so the consequence is DERIVED from what was refused rather than
# retyped. Absent from this table = we do not know what it costs, and we say nothing (see below):
# a route nobody anticipated must not inherit another route's consequence, exactly as `_politique`
# gives an unknown route the strictest policy rather than the most open one.
_COST = {
    ("GET", "list"): "this miner will NOT see the jobs waiting for it: `list` is the work queue and "
                     "nothing else carries that signal, so the daemon prints 'waiting for jobs' "
                     "while its GPU idles",
    ("GET", "req"): "this miner sees the job key but cannot pull the request body, so the job is "
                    "skipped as if it did not exist",
    ("POST", "res"): "a job can be COMPUTED and not DELIVERED: the sealed answer stays on this "
                     "machine and the client is never served",
    ("POST", "reveal"): "the primary's obligation cannot be filed: a sampled job then has no artifact "
                        "to judge, so the client's fee stays HELD and this miner is neither paid nor "
                        "slashed",
    # ⚠️ THESE TWO SAY "IF THE RELAY HOLDS NO EARLIER COPY", AND THE HEDGE IS EARNED, NOT TIMID.
    # A miner re-deposits both of these at every start, with the SAME bytes: the `pub` body is a
    # constant and the attestation is deterministic. The relay's anti-replay guard then refuses the
    # deposit with 401 — `{"why": "REJEU (digest already accepted)"}` — while serving the very
    # artifact it is refusing. So a refused re-deposit can cost EXACTLY NOTHING, and a message
    # asserting a missing copy would announce an absence this client never checked. Naming the cost
    # is the point of this table; asserting a state nobody read is the defect it exists to stop.
    ("POST", "pub"): "this deposit did not land -- if the relay holds no earlier copy, the "
                     "client-side cross-check against it is unavailable; the key anchored ON CHAIN "
                     "is what peers use, so mining is not blocked",
    ("POST", "attest"): "this deposit did not land -- if the relay holds no earlier copy, this miner "
                        "stays out of its attested set, and where the gate is armed "
                        "(DENDRA_ATTEST_REQUIRE=1) it is assigned no CONFIDENTIAL job; ordinary jobs "
                        "are unaffected",
}


def _relay_why(exc):
    """What the RELAY itself said, or "" -- never raises, never trusted as more than a quotation.

    ⛔ WITHOUT THIS, THE MESSAGE GUESSES. "DENDRA_RELAY_TOKEN is incorrect" is inferred from the
    token being non-empty on OUR side; it is not a reading of anything. The relay refuses writes for
    reasons that have nothing to do with the token -- a deposit whose digest it has already accepted
    comes back `{"why": "REJEU (digest already accepted)"}`, over a signature that VERIFIED and was
    attributed to this operator. Both faults can hold at once, and then the inference names a real
    defect that is simply not the one refusing this write. A 401 carrying a stated reason is the one
    case where nothing has to be speculated, and the body was being discarded.

    The text comes from a remote service, so it is bounded, flattened to one line and QUOTED: it is
    reported as something the relay said, never adopted as our own verdict.
    """
    try:
        brut = exc.read(512)
    except Exception:                                    # noqa: BLE001 -- not a body, not a crash
        return ""
    try:
        dit = json.loads(brut).get("why") or ""
    except Exception:                                    # noqa: BLE001
        return ""
    return " ".join(str(dit).split())[:160]


def _note_auth_failure(exc, methode, route):
    """Name the route that was refused, and only what THAT route carries.

    `methode` and `route` are REQUIRED, and deliberately so: a default would let a call site ship
    without naming itself and print an anonymous refusal again — the defect this function was
    rewritten to remove. There are four call sites, all in this file, and `test_relay_auth_message.py`
    drives every one of them through a real refusal.
    """
    code = getattr(exc, "code", None)
    if code not in (401, 403):
        return
    cle = (methode, route)
    if cle in _AUTH_WARNED:
        return
    _AUTH_WARNED.add(cle)
    # THE RELAY'S OWN REASON OUTRANKS OUR GUESS, and the two are never merged into one sentence.
    # When the relay states a reason, the token line becomes an OBSERVATION about our side ("set" /
    # "not set"), not a diagnosis -- because it can be flatly wrong about what refused (a replay
    # answers 401 and has nothing to do with the token). Only when the relay says nothing do we fall
    # back on the guess, and then it is the only thing we have.
    dit = _relay_why(exc)
    if dit:
        cause = (f'. The RELAY\'S OWN reason: "{dit}" -- read that before blaming the token '
                 f'(local state: DENDRA_RELAY_TOKEN is {"set" if _TOKEN else "NOT set"})')
    else:
        cause = (" (" + ("DENDRA_RELAY_TOKEN is unset" if not _TOKEN
                         else "DENDRA_RELAY_TOKEN is incorrect") + ")")
    cout = _COST.get(cle)
    # NO DEFAULT ON THE CONSEQUENCE. An unlisted route gets its refusal named and nothing claimed
    # about it. Inventing a cost here is how a refused pubkey deposit ends up announcing a dead work
    # queue: the fallback, not the route, is what makes the claim.
    suite = f" -> {cout}." if cout else " -> what this costs is not established here."
    print(f"[relay] relay refused {methode} {route} with {code}{cause}.{suite} "
          f"THIS REFUSAL CONCERNS {methode} {route} AND NOTHING ELSE: the relay decides access per "
          f"route (relay.py::_politique), reads are open on a relay running that policy, and "
          f"a signature attributable to this miner's operator authorises the writes without any "
          f"shared token. Obtain the token from the relay operator, or fix signing, and restart.",
          flush=True)


def _hdrs(extra=None):
    h = dict(extra or {})
    if _TOKEN:
        h["X-Dendra-Token"] = _TOKEN   # shared relay authentication
    return h


def _url(base, kind, key):
    return f"{base.rstrip('/')}/{kind}/{key}"


# ── SIGNING, WIRED IN ONE PLACE ────────────────────────────────────────────────────────────────
# Six call sites deposit to the relay, in four files. Teaching each of them to sign would mean six
# chances to forget one, and the one forgotten would fail the day enforcement is armed — as a refused
# write, which this client turns into a bare False. So the knowledge lives HERE, where every deposit
# already passes.
#
# Configured by environment, and OFF unless configured: a miner that has no key must keep working
# exactly as before. `DENDRA_SIGN_KEY` names the keyring entry; the rest is optional.
_SIGN_KEY = os.environ.get("DENDRA_SIGN_KEY", "").strip()
_SIGN_KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "").strip() or None
_SIGN_NODE = os.environ.get("DENDRA_NODE", "").strip() or None
_SIGN_CACHE: dict = {}


def set_sign_key(name: str) -> None:
    """Re-point the signing identity AFTER import, and drop what was cached about the old one.

    THIS EXISTS BECAUSE THE NAME CHANGES UNDER US. `DENDRA_SIGN_KEY` defaults to `MINER_ID`, this
    module reads it once at import, and the daemon then RENAMES the keyring entry to the derived
    `dm1...` identity. From that moment the name held here designates nothing: `address_from_key`
    fails, no signature is attached, and every deposit goes out unsigned. Nothing says so while the
    relay runs in `observe` -- the day `enforce` is armed, the miner's writes are refused and no log
    line on either side explains why.

    Exporting the variable again would not help: the module already read it. The cache must go too,
    or the address resolved for the OLD name would keep being signed for."""
    global _SIGN_KEY
    _SIGN_KEY = (name or "").strip()
    _SIGN_CACHE.clear()
_SIGN_WARNED = False


def _signature(kind, key, data, miner_id, height):
    """Headers for this deposit, or {} when signing is not configured. NEVER raises.

    A signing failure must not kill a miner that was working a second ago: the deposit goes out
    unsigned and is refused only if the relay is armed — which is a visible 401, not a crash. But it
    is said ONCE, because a silent fallback to unsigned is how an operator ends up armed and mute.
    """
    global _SIGN_WARNED
    if not _SIGN_KEY:
        return {}
    try:
        from modea import relay_signature as _rs
        if "adresse" not in _SIGN_CACHE:
            _SIGN_CACHE["adresse"] = _rs.address_from_key(_SIGN_KEY, keyring_dir=_SIGN_KEYRING_DIR)
        adresse = _SIGN_CACHE["adresse"]
        # READ ONCE, NOT PER DEPOSIT. A first draft re-read the account on every write, on the
        # assumption that a stale sequence would break verification. It would not: the relay rebuilds
        # the document from the SEQUENCE CARRIED IN THE HEADER, so writer and verifier agree by
        # construction whatever the chain has moved on to. What the pair must be is CONSISTENT, not
        # current — and re-reading it bought a chain round-trip per deposit for a property that was
        # already free.
        #
        # The signature still proves what it must: the digest of this exact write sits in the signed
        # memo, so the key holder signed THIS deposit and no other. Account number and sequence are
        # padding in that document, and the dedicated chain-id is what keeps it unbroadcastable.
        if "compte" not in _SIGN_CACHE:
            _SIGN_CACHE["compte"] = _rs.numero_et_sequence(adresse, node=_SIGN_NODE)
        an, seq = _SIGN_CACHE["compte"]
        return _rs.headers(kind, key, data, miner_id or _SIGN_KEY, height,
                           key_name=_SIGN_KEY, adresse=adresse, account_number=an, sequence=seq,
                           keyring_dir=_SIGN_KEYRING_DIR)
    except Exception as e:  # noqa: BLE001
        if not _SIGN_WARNED:
            _SIGN_WARNED = True
            print(f"[relay] cannot sign deposits ({type(e).__name__}: {e}) -> sending them UNSIGNED. "
                  f"They will be refused as soon as the relay requires signatures.", flush=True)
        return {}


def put_status(base, kind, key, obj, *, miner_id=None, height=0) -> str:
    """Deposit, with the ONE distinction a boolean cannot carry: "ok" | "exists" | "refused".

    A 409 does NOT mean the deposit failed. On the write-once kinds it means the key already holds a
    sealed artifact whose bytes differ from the ones just offered -- and on a RETRY that is the normal
    answer, because every seal draws a fresh ephemeral key, so re-sealing the same content never
    reproduces the same bytes. Collapsing that into False makes a caller count a reveal that IS stored
    as missing, and conclude a completed job failed. The caller decides what "exists" means for it;
    this function's job is to stop destroying the information.
    """
    # The body is serialised ONCE and both signed and sent: the signature covers a digest of these
    # exact bytes, so re-encoding between the two would produce a different body and a refusal that
    # no log line explains.
    data = json.dumps(obj).encode()
    req = urllib.request.Request(_url(base, kind, key), data=data, method="POST",
                                 headers=_hdrs({"Content-Type": "application/json",
                                                **_signature(kind, key, data, miner_id, height)}))
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return "ok"
    except Exception as e:
        _note_auth_failure(e, "POST", kind)
        if getattr(e, "code", None) == 409:
            return "exists"
        return "refused"


def put(base, kind, key, obj, *, miner_id=None, height=0) -> bool:
    """Back-compatible boolean: True only on a fresh accepted deposit.

    Kept as it was on purpose -- every existing caller reads it this way, and widening it here would
    silently change what "it worked" means for all of them at once. A caller that needs to tell a
    409 from a real refusal asks for it explicitly, with `put_status`.
    """
    return put_status(base, kind, key, obj, miner_id=miner_id, height=height) == "ok"


def get(base, kind, key, retries=1):
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(_url(base, kind, key), headers=_hdrs())
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception as e:
            _note_auth_failure(e, "GET", kind)
            time.sleep(0.3)
    return None


def get_blob(base, kind, key) -> bytes:
    """Raw stored bytes (used for confidentiality checks: this is all the relay can see)."""
    try:
        req = urllib.request.Request(_url(base, kind, key), headers=_hdrs())
        return urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        _note_auth_failure(e, "GET", kind)
        return b""


def listing(base):
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/list", headers=_hdrs())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        # AN EMPTY DICT IS NOT "NOTHING TO DO", and naming the refusal is what the other three
        # functions already do. Returning {} silently would read as "no jobs" — the exact silence
        # the rest of this module exists to prevent.
        # `GET /list` is an OPEN route (see relay.py): a miner needs no secret to learn that
        # work awaits it. But an older relay build may still gate it, and against one of those a
        # miner takes a 401 here on every round, forever. A 401 answered by silence is
        # indistinguishable from an idle network, so this handler must name what refused.
        # THIS IS THE ONE ROUTE WHOSE REFUSAL REALLY DOES MEAN "no job will be received" — and under
        # the old single global flag it was the one that could be muted by any earlier refusal.
        _note_auth_failure(e, "GET", "list")
        return {}
