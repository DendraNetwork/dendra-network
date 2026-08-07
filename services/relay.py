#!/usr/bin/env python3
"""UNTRUSTED network relay (L4): carries ENCRYPTED messages and PUBLIC keys over HTTP
between the client and the miners. It NEVER sees cleartext (only ciphertext + pubs).

Endpoints (JSON body):
  POST /pub/<mid>            {"pub": hex}                 -- a miner publishes its public key
  GET  /pub/<mid>            -> {"pub": hex}              -- the client fetches it
  POST /req/<jid>__<mid>     {"client_eph_pk","nonce","ct"} (hex) -- sealed request client->miner
  GET  /req/<jid>__<mid>     -> same
  POST /res/<jid>__<mid>     {"nonce","ct"} (hex)          -- sealed response miner->client
  GET  /res/<jid>__<mid>     -> same
  GET  /list                 -> {kind: [keys...]}          -- debug (keys only; GUARDED by token)

PERSISTENT storage (`relay_store.DiskStore`): RAM is only a hot cache, the disk is the truth.
Usage: python3 relay.py [port]   (default 8645, on 127.0.0.1).

PERSISTENCE IS NOT OPTIONAL. The sealed reveal is the ONLY artifact that makes an audit judgeable
(the chain keeps nothing but hashes of it). With an in-memory store, a single relay restart makes
EVERY open audit PERMANENTLY unjudgeable: the fee stays held forever, the client is neither served
nor refunded, the primary is neither paid nor slashed — the promise "held is not lost" becomes false
the moment the process restarts. Hence: write on deposit, reload at startup, and REFUSE TO START if
the store is not writable, because starting without persistence would recreate the hole while
suggesting otherwise. `DENDRA_RELAY_STORE` chooses the LOCATION, never the existence (invariant: no
environment variable ever disables a guard).

Confidentiality: END-TO-END guaranteed by Mode A. The relay is assumed hostile and only sees
ciphertext. BUT the miner pub is no longer the root of trust: the client encrypts to the ON-CHAIN
ANCHORED X25519 pub (signed by the miner's Cosmos key), so a pub substitution at the relay is
ineffective (the relay's /pub is now only a cache/transport).

Hardening:
  * Global LOCK -> ThreadingHTTPServer + shared state without data race (safe /list iteration).
  * Store BOUNDED per type (MAX_ENTRIES) -> no memory leak (FIFO eviction).
  * Body size cap (MAX_BODY) -> no unbounded allocation (local anti-DoS).
  * AUTH via shared token (DENDRA_RELAY_TOKEN) -> require the X-Dendra-Token header on ALL
    requests once a token is configured (local default = empty = no auth, backward-compatible).
  * RATE-LIMIT per IP (sliding window) -> a spammer can no longer evict (FIFO) legitimate
    entries nor saturate the relay.
  * /list (metadata: who processes what) GUARDED behind the token (otherwise network mapping).
  Multi-machine: DENDRA_RELAY_TOKEN=<secret> on all nodes + WireGuard/SSH tunnel for transport
  (the token authenticates; WireGuard encrypts/authenticates the channel). See the multi-machine
  deployment guide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_store  # noqa: E402  (sys.path must be set before this import)
from modea import (cosmos_addr, job_registry, registry_cache,  # noqa: E402  (same)
                   relay_antireplay, relay_canon, relay_carrier, relay_write)
from modea import relay_signature as _rs  # noqa: E402  (same)
from modea.exposition import (DrapeauInvalide, ecoute_locale,  # noqa: E402  (same)
                              public_actif)

MAX_BODY = 1 << 20        # 1 MiB per request (well above a sealed prompt/response)
# MAX_ENTRIES is NOT an eviction trigger: it is a memory guard rail. Eviction BY COUNT is blind — it
# ignores that a reveal may still serve an OPEN audit, so a few hundred jobs of recent traffic (about
# 6.6 envelopes per job) are enough to destroy the evidence of an older audit. Persistence alone would
# then be defeated by ordinary usage, with no attacker involved.
MAX_ENTRIES = 4000        # per kind: a CEILING, not an eviction policy (see RETENTION)
# The only eviction allowed is BY AGE. An entry is discardable only once it has outlived the window in
# which an audit could still need it. 7 days is the SAME window as `miner_prune_blocks`, on which the
# release gate already alarms for held funds: one horizon to keep in mind, and it is the one that
# bounds the deferral loop (the deferral bound MUST stay under this horizon).
RETENTION_FLOOR = 86400            # COMPILED floor: 24 h
RETENTION_DEFAULT = 604800         # 7 days
# The environment may LENGTHEN the retention, never shorten it below the floor: otherwise
# `DENDRA_RELAY_RETENTION=1` would become a switch that disables the guard.
try:
    _RET = float(os.environ.get("DENDRA_RELAY_RETENTION", "") or RETENTION_DEFAULT)
except ValueError:
    _RET = RETENTION_DEFAULT
RETENTION = max(RETENTION_FLOOR, _RET)
TOKEN = os.environ.get("DENDRA_RELAY_TOKEN", "")          # shared auth (empty = OFF, local)
# ROTATION WITHOUT DOWNTIME. The token being SHARED, changing it cuts off every client that has not
# migrated yet, so rotation becomes an event one keeps postponing — and a secret one postpones
# changing is a secret one never changes. `DENDRA_RELAY_TOKEN_PREV` makes the PREVIOUS token
# acceptable during the switchover. Two guard rails, because a badly bounded transition mechanism is
# worse than none: (1) EMPTY means strictly no effect (an empty variable opens NOTHING); (2) every use
# of the old token is COUNTED and logged, which gives the only signal that matters — "N clients still
# to migrate", hence "the old token can be closed".
TOKEN_PREV = os.environ.get("DENDRA_RELAY_TOKEN_PREV", "")
_PREV_HITS = [0]
RATE_MAX = int(os.environ.get("DENDRA_RELAY_RATE", "120"))  # max req / window / IP
RATE_WINDOW = float(os.environ.get("DENDRA_RELAY_WINDOW", "10"))  # seconds
# PUBLIC EXPOSURE. Read once, through the SHARED reader (`modea.exposition`): when three services of
# the stack each had their own, `DENDRA_PUBLIC=true` armed some refusals and not others. An unexpected
# value is never guessed — it is recorded here and refused at startup by `main()`, never folded back
# onto the permissive default.
PUBLIC = False
PUBLIC_ERR = ""
try:
    PUBLIC = public_actif(os.environ.get("DENDRA_PUBLIC"))
except DrapeauInvalide as _e:
    PUBLIC_ERR = str(_e)
# ATTESTATION gate for confidential jobs. When armed, the relay REFUSES a req/<jid>__<mid> until the
# miner <mid> has deposited a valid SIGNED attestation (POST /attest/<mid>) whose measured hash is in
# the allow-list DENDRA_ATTEST_ALLOW (comma-separated sha256 = recognized binaries/configs).
# HONEST: deterrence (the miner must sign a recognized measured client), NOT a proof of execution.
#
# ⛔ PUBLIC EXPOSURE DOES NOT IMPLY ATTESTATION, AND THE TWO MUST NOT BE TIED TOGETHER.
# `ATTEST_REQUIRE` reads its own variable and nothing else: derived from `PUBLIC`, it would force the
# gate on for any open network. An allow-list pins SPECIFIC measured digests, so an open network would
# have to publish the exact build every third-party miner must run — which excludes precisely the
# independent operators the network exists to attract. ADR-011 states the consumer tier as HARDENED
# DETERRENCE, NOT a cryptographic proof; forcing the gate contradicts the decision it claims to
# implement. The knob is therefore explicit: attestation is required when, and only when, someone asks
# for it. Two properties hold regardless, and they are the ones that matter:
#   · asking for the gate WITHOUT an allow-list is still refused at startup — arming a gate with
#     nothing to check against assigns NO job at all, and failing loudly at boot beats a live network
#     answering silent 403s;
#   · a relay that does NOT enforce attestation says so LOUDLY at startup (see the banner below).
# ⚠️ AND THE COUNTERPART IS BINDING: a guard that is not APPLIED is not ANNOUNCED. No public text may
# describe attestation as active while this is off. That rule is what makes turning it off honest
# rather than convenient.
ATTEST_ALLOW = {h.strip() for h in os.environ.get("DENDRA_ATTEST_ALLOW", "").split(",") if h.strip()}
ATTEST_REQUIRE = os.environ.get("DENDRA_ATTEST_REQUIRE", "0") == "1"

# ── SIGNED WRITES (ADR-039 applied to the relay) ───────────────────────────────────────────────
# The shared token says SOMEONE authorised is writing; it never says WHO. Since the deposit key
# encodes the miner (`req/<jid>__<mid>`), any token holder can write under any other operator's key.
# That is invisible today — one participant, one token — and it becomes real the day operators arrive,
# which is exactly what the network is recruiting for.
#
# THREE STATES, AND THE DEFAULT IS `off` ON PURPOSE. The verification chain (canonical message →
# signature → attribution → replay) is implemented and unit-tested, but the one link that produces a
# signature runs `dendrad`, which cannot be exercised where this code was written. Arming a path whose
# first real execution is also its first test would refuse every deposit, and the symptom — an empty
# relay — is indistinguishable from a quiet network.
#
#   off      nothing changes. Unsigned writes accepted, as before.
#   observe  signed writes are VERIFIED, ATTRIBUTED and counted; unsigned ones still pass, and so do
#            signed ones that no registry can attribute — on the token. This is the state that turns
#            "it should work" into a number, on real traffic, before anything is refused.
#   enforce  a write that is neither attributed nor carrying the token is REFUSED (401).
#
# THE MODE IS NOT WHAT DECIDES WHETHER A SIGNATURE MEANS ANYTHING — the registry is. In every mode, a
# signature that cannot be attributed to the operator the chain records for `<mid>` authorises exactly
# as much as no signature at all: the deposit falls back to the token. See ATTRIBUTION below.
#
# The order matters more than the switch: run `modea.relay_signature.autotest()` once, watch
# `observe` count valid writes, then arm. Anyone who jumps straight to `enforce` is testing in
# production on the one path whose failure mode is silence.
SIGN_MODE = (os.environ.get("DENDRA_RELAY_SIGN", "off") or "off").strip().lower()
if SIGN_MODE not in ("off", "observe", "enforce"):
    raise SystemExit(f"DENDRA_RELAY_SIGN={SIGN_MODE!r} — expected off | observe | enforce. "
                     f"Refusing to start rather than guess: a typo must not silently mean 'off'.")

# ── ATTRIBUTION — A SIGNATURE THAT NAMES NOBODY AUTHORISES NOBODY ─────────────────────────────────
# Verifying a signature and ATTRIBUTING a write are two different operations, and only the second one
# says WHO wrote. Rebuilding the signed document from the address DERIVED FROM THE PRESENTED KEY makes
# every well-formed key verify for every deposit key: the check answers "someone holds a private key",
# which is a sentence with no subject. The subject comes from the registry, and from nowhere else —
# `address(pubkey)` must equal the operator the chain records for the miner `<mid>` the deposit key
# names (or, for `req`, the client the chain records as paying for `<jid>`).
#
# DENDRA_RELAY_REGISTRY IS THEREFORE NOT AN OPTION OF THE SIGNATURE, IT IS ITS SUBJECT. Unset, a
# signature stands for nothing on its own and every deposit falls back to the shared token — the state
# announced at startup, because a guard whose arming is unknown gets described as armed.
REGISTRY_URL = os.environ.get("DENDRA_RELAY_REGISTRY", "").strip().rstrip("/")
REGISTRY_TIMEOUT = 3.0                       # a request thread must not hang on a dead REST gateway
REGISTRY_EXPIRY = 300.0                      # beyond this the cache is STALE, which does not refuse
# Floor between two refresh ATTEMPTS. Without it, an unreachable gateway costs REGISTRY_TIMEOUT on
# every single write, and a guard whose cost grows with traffic becomes the reason someone removes it.
REGISTRY_RETRY = 30.0


def _read_rest(path: str) -> bytes:
    with urllib.request.urlopen(REGISTRY_URL + path, timeout=REGISTRY_TIMEOUT) as r:
        return r.read()


class _UnconfiguredRegistry:
    """The registry that is NOT configured. It answers NEVER_READ, which `relay_write` turns into a
    refusal — never into an acceptance.

    A stub is the natural place to put a convenient default, and that is exactly why this one carries
    none: a guard that cannot identify must refuse. Returning anything else here would reopen the hole
    one layer lower, where no test looks."""

    def operator(self, miner_id):
        return None, registry_cache.NEVER_READ


REGISTRY = _UnconfiguredRegistry()
JOBS = None
if REGISTRY_URL:
    REGISTRY = registry_cache.RegistryCache(
        lambda: _read_rest("/dendra/jobs/v1/miner?pagination.limit=1000"), time.time,
        expiry=REGISTRY_EXPIRY)
    JOBS = job_registry.JobRegistry(
        lambda: _read_rest("/dendra/jobs/v1/job?pagination.limit=1000"), time.time,
        expiry=REGISTRY_EXPIRY,
        lecteur_job=lambda jid: _read_rest("/dendra/jobs/v1/job/" + urllib.parse.quote(jid, safe="")))
# `retention` is NOT a free setting: a digest forgotten before its entry is evicted reopens exactly
# the replay window this object closes. It is read from RETENTION rather than restated.
ANTIREPLAY = relay_antireplay.Antireplay(time.time, retention=RETENTION)
_REG_LOCK = threading.Lock()
_REG_ATTEMPT = {"miner": 0.0, "job": 0.0}
# Reasons that do NOT establish a valid signature. Everything else means the signature verified and
# the ATTRIBUTION is what refused — two failures an operator must not confuse: one is a broken client,
# the other is a write under someone else's name.
_BEFORE_SIGNATURE = (relay_write.MAL_FORME, relay_write.SIGNATURE_INVALIDE,
                     relay_write.UNREADABLE_HEIGHT)
# Named like the reasons `relay_write` returns, because it lands in the same counter and the same
# refusal body: an operator reading `KEY_MISMATCH` next to `FOREIGN_KEY` must not have to work out
# which module spoke.
KEY_MISMATCH = "KEY_MISMATCH"
EMPTY_ATTRIBUTION = "EMPTY_ATTRIBUTION"
SIGN_STATS = {"signed_ok": 0, "signed_ko": 0, "unsigned": 0,
              # `signed_ok` keeps its meaning — the SIGNATURE verified — because a counter that
              # changes meaning silently invalidates every reading of it made before. What the
              # signature now has to earn is counted next to it.
              "attributed": 0, "unattributed": 0}
SIGN_REFUSALS: dict = {}
# The KEY names the miner a deposit is FILED UNDER; the header names the miner it CLAIMS to be, and
# the registry is read against the header. Nothing ties the two: both enter the canonical message, so
# a signature is perfectly valid for a header of A over a key of B. A REGISTERED operator could
# therefore deposit under another miner's key, be correctly attributed to ITSELF, and take the key its
# owner needs — write-once then locks that owner out of its own artifact for good. This is where the
# two names are confronted, in the relay, because the shape of a deposit key is route knowledge.
KEY_IS_THE_MINER = ("pub", "attest")     # `pub/<mid>`, `attest/<mid>`: the key IS the identifier


def _miner_named_by_key(kind, key):
    """The miner the KEY files this deposit under, or None when the key names none.

    None is a REFUSAL upstream, never a pass: a key whose shape says nothing about its owner cannot be
    confronted with the writer, and a binding that cannot be enforced must not be reported as
    enforced."""
    if kind in KEY_IS_THE_MINER:
        return key or None
    return key.rsplit("__", 1)[1] if isinstance(key, str) and "__" in key else None


def _refresh_cache(cache, name):
    """Best-effort refresh. `refresh()` NEVER raises; a failed read is a fact the cache counts."""
    if cache is None or not REGISTRY_URL or cache.state() == registry_cache.FEES:
        return
    now = time.monotonic()
    with _REG_LOCK:
        if now - _REG_ATTEMPT[name] < REGISTRY_RETRY:
            return
        _REG_ATTEMPT[name] = now
    cache.refresh()


def _canon(kind, key, body, miner_id, height):
    """`relay_write` wants the message AND its digest; `relay_canon` exposes them separately and
    recomputes the message for the second. One call, one message: the digest must be the digest OF
    THE BYTES that were verified, not of a second construction that happens to agree today."""
    message = relay_canon.canonical_message(kind, key, body, miner_id, height)
    return message, hashlib.sha256(message).digest()


LOCK = threading.Lock()
STORE = {"pub": OrderedDict(), "req": OrderedDict(), "res": OrderedDict(),
         "reveal": OrderedDict(),    # SEALED reveal primary->committee on an audited job
         "attest": OrderedDict()}    # signed software attestation of the miner (key = <mid>)
# ── WRITE-ONCE ───────────────────────────────────────────────────────────────────────────────────
# `_put` OVERWRITES, and three of the five kinds hold a per-job sealed artifact that exists in ONE
# copy: the sealed request (`req`), the sealed response (`res`) and the sealed reveal (`reveal`). The
# chain keeps only hashes of them, so replacing one destroys the evidence an audit concludes on — the
# held fee then never resolves, and the symptom is indistinguishable from a quiet network.
#
# `pub` and `attest` are DELIBERATELY absent. They carry an IDENTITY, not evidence: a miner rotates
# its X25519 key and republishes a measured client after every upgrade. Write-once there would freeze
# a miner on its first key and make an upgrade permanently unattestable — a guard that breaks the
# thing it protects.
#
# NOBODY REPLACES, NOT EVEN THE AUTHOR. A first draft let an ATTRIBUTED author replace its own
# artifact, to spare a worker whose `200` was lost on the way back. It is a worse rule: it hands the
# primary a way to swap the very bytes a jury is judging, and it makes the guard depend on an
# ownership that `relay_store` does not persist — so the same request would be refused or accepted
# depending on whether the relay had restarted. A guard whose answer moves with the uptime is not a
# guard. The retry is covered by the case below instead, which costs nothing.
WRITE_ONCE = ("req", "res", "reveal")


class Conflict(RuntimeError):
    """A DIFFERENT content offered for a key that already holds a sealed artifact.

    Byte-identical is not a conflict: a retry that re-sends what is already stored destroys nothing,
    and refusing it would strand a worker whose first `200` was lost on the way back."""
# ── AGGREGATE COUNTERS — OBSERVABILITY WITHOUT THE PATHS ─────────────────────────────────────────
# The access log is SILENT by design (see `log_message`): the path of a request IS an identifier
# (`<kind>/<jobId>__<minerId>`). But "no paths" does not mean "blind" — GRANULARITY decides, not the
# principle. Without these counters, a store-filling attack in progress (a third party saturating the
# store so that legitimate deposits are REFUSED) is strictly invisible: the network goes dark and
# nothing says so. Only numbers live here — no jobId, no minerId, no key.
#
# WHY THEY ARE COUNTED IN `_send` AND NOWHERE ELSE. A counter placed branch by branch is forgotten on
# the FIRST branch added — and the refusal just written is the one that would stop being visible.
# Every relay response goes through `_send`, so a new refusal code is counted WITHOUT ANYONE THINKING
# ABOUT IT, and it appears under its own name (`http_418` if a 418 is ever introduced). A guard placed
# at a single point cannot miss a variant.
CPT = {"evictions_age": 0}
CPT_LOCK = threading.Lock()


def _cpt(name, n=1):
    with CPT_LOCK:
        CPT[name] = CPT.get(name, 0) + n


def _stats():
    """AGGREGATE snapshot of the relay. Nothing here may ever carry a key: the `kind` names are fixed
    protocol CATEGORIES (pub/req/res/reveal/attest), not identifiers."""
    with LOCK:
        occupation = {k: len(v) for k, v in STORE.items()}
    with CPT_LOCK:
        compteurs = dict(CPT)
    return {
        "compteurs": compteurs,
        "occupation": occupation,
        "plafond_par_type": MAX_ENTRIES,
        "retention_s": RETENTION,
        "ancien_jeton_utilise": _PREV_HITS[0],
        # SIGNED WRITES — the mode is published next to its counters ON PURPOSE. `signed_ok: 0` means
        # two opposite things depending on it: "nobody signs yet" under `off`, or "nothing is getting
        # through" under `enforce`. A counter without its regime is a number that can be read the
        # reassuring way, which is how a disarmed guard passes for a quiet network.
        #
        # `registry` sits in the same block for the same reason, one level deeper: `signed_ok` high
        # and `attributed` at zero is not "signatures are working", it is "signatures name nobody and
        # every write is riding on the token". Published apart, those two numbers get read apart.
        "signature": {"mode": SIGN_MODE, **SIGN_STATS,
                      "registry": REGISTRY.state() if REGISTRY_URL else "NOT_CONFIGURED",
                      "jobs": JOBS.state() if JOBS is not None else "NOT_CONFIGURED",
                      "refusals": dict(SIGN_REFUSALS)},
        "rate_max": RATE_MAX,
        "rate_window_s": RATE_WINDOW,
        "ips_suivies": len(RATE),
        "uptime_s": round(time.time() - _T0, 1),
    }


_T0 = time.time()
RATE = {}                 # ip -> deque[timestamps] (bounded by window)
RATE_LOCK = threading.Lock()

# --- PERSISTENCE --------------------------------------------------------------------------------
# The LOCATION is configurable, the EXISTENCE is not: no value of DENDRA_RELAY_STORE makes the relay
# volatile. Default = `relay-store/` next to the script.
STORE_DIR = (os.environ.get("DENDRA_RELAY_STORE", "").strip()
             or os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay-store"))
TS = {k: OrderedDict() for k in STORE}   # kind -> key -> deposit time (eviction is BY AGE)
DISK = None          # relay_store.DiskStore — the disk is authoritative, RAM is only a hot cache
BOOT_ERR = ""        # non-empty => the store is unusable => the relay refuses to serve
BOOT_STATS = {}


def _boot_store():
    """Open the store and RELOAD the RAM cache from disk. Returns {kind: n_reloaded}.

    FIFO order is rebuilt from each record's persisted rank: after a restart, eviction is still
    "oldest first", as if the process had never been interrupted. Raises `StoreUnavailable` if the
    store is not writable — never a silent in-memory fallback, which would be the original hole with
    added confidence.
    """
    global DISK
    DISK = relay_store.DiskStore(STORE_DIR, list(STORE.keys()), MAX_ENTRIES)
    loaded = DISK.load(retention=RETENTION)     # purge BY AGE at startup, never by count
    stats = {}
    with LOCK:
        for kind, items in loaded.items():
            d, t = STORE[kind], TS[kind]
            d.clear()
            t.clear()
            for key, data, _n, ts in items:  # items already sorted from oldest to newest
                d[key] = data
                t[key] = ts
            stats[kind] = len(d)
    return stats


try:
    BOOT_STATS = _boot_store()
except relay_store.StoreUnavailable as _e:
    BOOT_ERR = str(_e)


def _attest_ok(mid: str) -> bool:
    """Is a miner attested to receive a confidential job? Verifies the signature of the deposited
    attestation + the membership of the measured hash in the allow-list. Best-effort: if the
    `cryptography`/`modea` lib is not importable on the relay side, we DO NOT ALLOW (fail-closed when
    the gate is active). Without a configured allow-list, the gate cannot tell a good client ->
    we also refuse (inconsistent config = we close)."""
    if not ATTEST_ALLOW:
        return False
    raw = _get("attest", mid)
    if not raw:
        return False
    try:
        att = json.loads(raw)
    except Exception:
        return False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from modea import confine
    except Exception:
        return False
    # `expected_pubkey` IS LEFT OUT, AND THAT IS THE HONEST FORM OF THIS CHECK.
    # `verify_attestation` compares `expected_pubkey` against `att["attest_pubkey"]`. Handing it
    # `att["attest_pubkey"]` compares a value to itself: the branch is always taken, it can refuse
    # nothing, and it reads — in the source and in the review — like a binding to an identity. The
    # relay has no source for the attestation key: the registry it now reads holds Cosmos OPERATOR
    # addresses (secp256k1), whereas an attestation is signed by an Ed25519 key that appears nowhere
    # on chain. There is nothing to compare against, so nothing is compared, and what remains is what
    # is actually proven: a valid signature, a measure that matches its hash, and a hash on the
    # allow-list. A control that cannot fail is worse than an absent one — it is an absent one that
    # gets counted.
    ok, _ = confine.verify_attestation(att, allowed_hashes=ATTEST_ALLOW, expected_pubkey=None)
    return ok and att.get("miner_id") == mid


def _put(kind: str, key: str, data: bytes) -> None:
    """Deposit: DISK FIRST, then RAM. The order matters.

    If the disk write fails, NOTHING is put in RAM and the exception propagates (503 on the HTTP
    side): a deposit accepted but not persisted would be exactly the original defect, with a lying
    "ok" returned to the miner that would believe its reveal safe.

    The write-once guard lives HERE rather than in the HTTP layer: this is the only function that
    writes, so a caller added later inherits the guard instead of having to remember it.
    """
    if DISK is None:
        raise relay_store.StoreUnavailable(BOOT_ERR or "store not opened")
    now = time.time()
    evicted = []
    with LOCK:
        d, t = STORE[kind], TS[kind]
        # (1) EVICTION BY AGE, and by age only. `t` is ordered by insertion, hence by date: stop at
        # the first entry that is still young.
        while d:
            k0 = next(iter(d))
            if now - t.get(k0, now) <= RETENTION:
                break
            d.pop(k0, None)
            t.pop(k0, None)
            evicted.append(k0)
        # (2) CEILING: when it is reached while EVERYTHING is still young, REFUSE the write. Destroying
        # evidence that is still useful in order to make room is the very defect this repairs: a
        # refused deposit is recoverable (the worker retries), erased evidence is not.
        if key not in d and len(d) >= MAX_ENTRIES:
            full = True
        else:
            full = False
        stored = d.get(key)
    if evicted:
        _cpt("evictions_age", len(evicted))
    for k in evicted:
        DISK.remove(kind, k)
    if full:
        raise relay_store.StoreFull(
            f"{kind}: {MAX_ENTRIES} entries and none has outlived the retention window "
            f"({RETENTION:g}s) - deposit REFUSED rather than erasing evidence that is still useful")
    # (3) WRITE-ONCE on the kinds that hold a sealed artifact. Two outcomes, and the first is what
    # keeps this from stranding an honest worker:
    #   · identical bytes -> nothing to do, and nothing destroyed. A worker whose `200` was lost on
    #     the way back re-sends exactly what is stored; refusing that would freeze the job it exists
    #     to complete, and accepting it changes not one byte on disk.
    #   · different bytes -> REFUSED, whoever is writing. The chain keeps only a hash of this
    #     artifact: replace it and the audit has nothing left to conclude on.
    if kind in WRITE_ONCE and stored is not None:
        if stored == data:
            return
        raise Conflict(
            f"{kind}/{key}: this key already holds a sealed artifact and the deposit carries "
            f"different bytes. REFUSED rather than replaced - the chain keeps only a hash of it, so "
            f"an audit that loses it can never conclude and its fee stays held for good.")
    DISK.write(kind, key, data, ts=now)
    with LOCK:
        d, t = STORE[kind], TS[kind]
        if key in d:
            d.move_to_end(key)
            t.pop(key, None)
        d[key] = data
        t[key] = now


def _get(kind: str, key: str):
    with LOCK:
        return STORE[kind].get(key)


def _snapshot_keys():
    with LOCK:
        return {k: list(v.keys()) for k, v in STORE.items()}   # copy under lock -> safe iteration


def _rate_ok(ip: str) -> bool:
    """Sliding window per IP. True if the request is allowed."""
    now = time.monotonic()
    with RATE_LOCK:
        dq = RATE.get(ip)
        if dq is None:
            dq = deque()
            RATE[ip] = dq
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_MAX:
            return False
        dq.append(now)
        # opportunistic purge of inactive IPs (bounds the counter's memory)
        if len(RATE) > 8192:
            for k in [k for k, v in RATE.items() if not v or now - v[-1] > RATE_WINDOW][:4096]:
                RATE.pop(k, None)
        return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b""):
        _cpt("http_%d" % code)          # see the COUNTERS block: a single point, no branch forgotten
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self):
        return [p for p in self.path.strip("/").split("/") if p != ""]

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _authed(self) -> bool:
        if not TOKEN:
            return True   # no token configured -> local mode, no auth (backward-compatible)
        got = self.headers.get("X-Dendra-Token", "")
        # CONSTANT-TIME comparison (== leaks the length of the correct prefix)
        cur = hmac.compare_digest(got, TOKEN)
        if not TOKEN_PREV:
            return cur
        # BOTH are ALWAYS evaluated (no short circuit): returning earlier on the current token would
        # make the response time depend on which token was presented.
        prev = hmac.compare_digest(got, TOKEN_PREV)
        if prev and not cur:
            _PREV_HITS[0] += 1
            if _PREV_HITS[0] in (1, 10, 100) or _PREV_HITS[0] % 500 == 0:
                print(f"[relay] PREVIOUS token still in use ({_PREV_HITS[0]} requests) from "
                      f"{self._client_ip()} - this client is NOT migrated. Do not remove "
                      f"DENDRA_RELAY_TOKEN_PREV while this counter keeps rising.", flush=True)
        return cur or prev

    # ⛔ ACCESS POLICY IS DECIDED PER ROUTE. ONE POLICY FOR THE WHOLE SERVICE MAKES THIS RELAY
    # UNJOINABLE AND PROTECTS THE WRONG THING.
    #
    # `DENDRA_RELAY_TOKEN` is a SHARED secret that `network-info.txt` does not carry and that no
    # self-service procedure issues. Demanded on every route, it gates a network described as
    # permissionless on a secret handed out by hand, one operator at a time: a joiner passes the
    # hardware and genesis-digest checks of `join.sh`, then takes 401 on every relay call.
    #
    # The reflex fix — publish the token — is wrong, and `_put` says why. A POST to
    # `res/<jid>__<mid>` lands on the ONLY artifact that makes an audit judgeable (see the module
    # docstring): destroy it and the fee stays held forever, the miner neither paid nor slashed.
    # `_put` now refuses a second, DIFFERENT content on that key (409) — so the destruction is no
    # longer one anonymous POST away. That closes the ERASURE; it does not make the route safe to
    # open, because a first write on a key nobody has used yet still needs a writer with a name.
    #
    # ⚠️ WRITE-ONCE AND ATTRIBUTION ARE NOT INTERCHANGEABLE, AND NEITHER SUBSTITUTES FOR THE OTHER:
    # write-once protects an artifact that EXISTS, attribution decides who may create one. A relay
    # with only the first lets a stranger occupy every key before its owner does; a relay with only
    # the second lets a token holder overwrite what it did not write.
    #
    # But the routes do not share a threat model, so they must not share a policy:
    #   · READING content is already assumed hostile — the docstring says "the relay is assumed
    #     hostile", bodies are sealed to the ON-CHAIN ANCHORED X25519 key, and a substitution at the
    #     relay is ineffective. A token in front of ciphertext protects nothing the design relies on,
    #     and it is exactly what blocks a joiner from pulling its own work. -> OPEN.
    #   · WRITING miner-owned content (`pub`, `res`) needs the writer to be THAT miner — which a
    #     shared secret cannot express and a signature can. `relay_client` already signs these
    #     deposits. -> SIGNATURE, or the token as the transition path.
    #   · WRITING `req` is the gateway's job and the gateway does not sign yet. -> TOKEN.
    #   · `list` / `stats` map who processes what. That is the network-mapping surface the token is
    #     really there for. -> TOKEN.
    #
    # Why not simply set DENDRA_RELAY_SIGN=enforce: it is GLOBAL, so it refuses the gateway's unsigned
    # `req` deposits and stops the network. The mode is not the problem; applying one mode to routes
    # with different threats is.
    OUVERT, JETON, SIGNE_OU_JETON = "open", "token", "signed-or-token"

    def _politique(self, parts, methode):
        """Access policy for this route. Unknown routes get the STRICTEST one: a path nobody
        anticipated must not inherit the permissions of the most open class."""
        if parts and parts[0] in ("list", "stats"):
            return self.JETON
        if len(parts) == 2 and parts[0] in STORE:
            if methode == "GET":
                return self.OUVERT
            # THE CRITERION IS WHO WRITES, not which routes happen to be listed here.
            # Four kinds are written by the MINER — `pub`, `res`, `reveal`, `attest` (deposited by
            # reveal_helpers.py, reveal_worker.py and miner.py). For those, a signature names
            # WHICH miner wrote, which a shared secret cannot express, so it stands on its own.
            # `reveal` matters most: it is the primary's obligation, and a sampled job with no reveal
            # has no artifact to judge, so its held fee never resolves. Gating it on a secret the
            # joiner has no way to obtain would strand the payment it exists to release.
            # `req` keeps the token: it is the gateway's route, and the gateway does not sign yet.
            return self.SIGNE_OU_JETON if parts[0] in ("pub", "res", "reveal", "attest") else self.JETON
        return self.JETON

    def _guard(self, politique=None) -> bool:
        """Rate-limit + auth. Returns False (and has already responded) if rejected.

        The rate limit applies to EVERY route including the open ones: opening a route removes the
        need for a secret, never the need for a bound."""
        if not _rate_ok(self._client_ip()):
            self._send(429, b'{"error":"rate limited"}')
            return False
        if politique == self.OUVERT:
            return True
        if not self._authed():
            self._send(401, b'{"error":"unauthorized (X-Dendra-Token)"}')
            return False
        return True

    def _verify_write_request(self, kind, key, body):
        """(address|None, reason). The address is the one THE CHAIN names for this deposit key.

        THE FOUR STAGES ARE NOT OPTIONAL AND THEIR ORDER IS THE GUARD — shape, signature, attribution,
        replay — so they are not re-implemented here: `modea.relay_write.verify_write` composes them
        and is tested as a composition. A third implementation of the same chain is a third place for
        one stage to go missing, which is how stages (3) and (4) came to exist without ever running.

        WHY THE VERIFICATION IS NOT DONE IN `off`: it costs a signature check (~475 us) on input that
        nothing yet requires, and — more to the point — a guard that runs while unable to refuse
        trains everyone to read its counters as decoration. `observe` exists precisely so that the
        counters mean something before they can block.
        """
        if SIGN_MODE == "off":
            return None, "off"
        missing = [h for h in _rs.HEADERS if not self.headers.get(h)]
        if missing:
            SIGN_STATS["unsigned"] += 1
            # NAMED, not "invalid signature": an operator whose client is simply too old must read
            # WHICH field is absent, not go hunting through its key material.
            return None, "unsigned (missing: " + ", ".join(missing) + ")"
        try:
            pub = base64.b64decode(self.headers.get(_rs.HEADER_PUBKEY), validate=True)
            sig = base64.b64decode(self.headers.get(_rs.HEADER_SIG), validate=True)
            miner = self.headers.get(_rs.HEADER_MINER)
            height = int(self.headers.get(_rs.HEADER_HEIGHT))
            verif = relay_carrier.verifier(self.headers.get(_rs.HEADER_ACCT),
                                           self.headers.get(_rs.HEADER_SEQ))
        except Exception as e:  # noqa: BLE001 — malformed input is a refusal, never an outage
            SIGN_STATS["signed_ko"] += 1
            return None, f"malformed ({type(e).__name__})"
        # STAGE (1) CONTINUED — the two names this deposit carries must be ONE name. Checked here and
        # not inside `relay_write`: the convention `<jid>__<mid>` is the shape of a RELAY ROUTE, and a
        # module that verifies signatures has no business knowing how this service names its keys.
        target = _miner_named_by_key(kind, key)
        if target is None or target != miner:
            SIGN_STATS["signed_ko"] += 1
            SIGN_REFUSALS[KEY_MISMATCH] = SIGN_REFUSALS.get(KEY_MISMATCH, 0) + 1
            return None, (f"{KEY_MISMATCH} (the key files this deposit under {target!r}, the header "
                          f"announces {miner!r})")
        _refresh_cache(REGISTRY if REGISTRY_URL else None, "miner")
        _refresh_cache(JOBS, "job")
        # `tete=None`: the relay has no chain head, so the replay guard runs in its DEGRADED regime —
        # high-water plus digest, without the freshness window. That is a deliberate acceptance stated
        # in `relay_antireplay`: a halted chain must not destroy audit evidence.
        r = relay_write.verify_write(
            kind, key, body, miner, height, pub, sig, REGISTRY, ANTIREPLAY, None,
            canon=_canon, verify_signature=verif,
            address_from_key=cosmos_addr.address_from_pubkey,
            read_height=relay_canon.message_height,
            job_registry=JOBS, job_from_key=job_registry.job_from_key)
        if r.accepte:
            address = r.attribue_a()
            SIGN_STATS["signed_ok"] += 1
            if not address:
                # AN ACCEPTANCE WITHOUT AN ADDRESS IS NOT AN ATTRIBUTION, and the empty string is the
                # shape that would slip through: it is falsy, it is not None, and `address != None`
                # would read it as a name. The caller must never receive a subject it cannot print.
                SIGN_STATS["unattributed"] += 1
                SIGN_REFUSALS[EMPTY_ATTRIBUTION] = SIGN_REFUSALS.get(EMPTY_ATTRIBUTION, 0) + 1
                return None, f"{EMPTY_ATTRIBUTION} (accepted with no address for role {r.role!r})"
            SIGN_STATS["attributed"] += 1
            return address, "signed"
        SIGN_REFUSALS[r.motif] = SIGN_REFUSALS.get(r.motif, 0) + 1
        if r.motif in _BEFORE_SIGNATURE:
            SIGN_STATS["signed_ko"] += 1
        else:
            # The signature VERIFIED; it is the binding that refused. Counting this as a bad signature
            # would send an operator to check its key material over a write filed under a name that is
            # not its own.
            SIGN_STATS["signed_ok"] += 1
            SIGN_STATS["unattributed"] += 1
        return None, f"{r.motif} ({r.detail[:120]})"

    def do_POST(self):
        parts = self._route()
        pol = self._politique(parts, "POST")
        # A SIGNED write needs no shared secret; that is the whole point of the signature. So the
        # token check is deferred for `signed-or-token` until we know whether the deposit is signed —
        # it cannot run first, because running it first is what makes a token mandatory.
        if not self._guard(self.OUVERT if pol == self.SIGNE_OU_JETON else pol):
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        # BODY CAP. `Content-Length` is supplied by the CLIENT: comparing it against the UPPER bound
        # only lets NEGATIVE values through, and `read(-1)` reads until EOF — an unbounded allocation
        # despite the guard. It must be bounded on BOTH sides.
        if n < 0 or n > MAX_BODY:
            self._send(413, b'{"error":"body too large"}')
            return
        data = self.rfile.read(n) if n else b""
        if len(parts) == 2 and parts[0] in STORE:
            # attestation gate on the DEPOSIT of a confidential job (req/<jid>__<mid>).
            if ATTEST_REQUIRE and parts[0] == "req":
                mid = parts[1].split("__", 1)[1] if "__" in parts[1] else ""
                if not (mid and _attest_ok(mid)):
                    self._send(403, b'{"error":"miner not attested (DENDRA_ATTEST_REQUIRE)"}')
                    return
            # SIGNATURE BEFORE THE WRITE. `_put` touches the disk and the replay set; running it on
            # a deposit that will be refused would let an unauthenticated caller fill both.
            _writer, _sig_why = self._verify_write_request(parts[0], parts[1], data)
            if pol == self.SIGNE_OU_JETON:
                # AN ATTRIBUTED WRITE IS STRICTLY STRONGER THAN THE SHARED TOKEN, so it stands alone.
                # The strength is in the ATTRIBUTION, not in the signature: a signature proves that
                # someone holds a private key, and the verifier rebuilds the signed document from the
                # address derived from THAT KEY, so any well-formed key verifies for any deposit key.
                # What names a writer is the comparison against the registry — `address(pubkey)` equal
                # to the operator the chain records for `<mid>` (or the client that pays for `<jid>`).
                # `_writer` is that address and nothing else; a valid signature that the registry
                # cannot attribute leaves it None and buys no more than an unsigned deposit does.
                # The token is kept as the FALLBACK, not as the requirement: an operator running an
                # older client that cannot sign yet must not be cut off by a hardening it never heard
                # about. A joiner needs no secret once its signature can be attributed — which is the
                # defect being closed, and it is closed by the registry, not by the signature.
                #
                # THE FALLBACK IS "A TOKEN EXISTS AND WAS PRESENTED", WHICH `_authed()` ALONE CANNOT
                # SAY: it answers True when no token is CONFIGURED. That answer is right for a route
                # whose only guard is the token — there is nothing to check against — and wrong here,
                # where it makes the fallback swallow the requirement it is a fallback FOR. Written
                # `not self._authed()`, the refusal below cannot fire on a relay that has no token,
                # so `enforce` refuses nothing on exactly the deployments that dropped the token to
                # open the routes: the switch reports armed and is inert.
                # The regime still decides WHETHER to refuse. `off` and `observe` must keep letting
                # an unsigned deposit through — that is what they are for, and the public relay runs
                # `observe` today — so they keep reading the token guard alone.
                if SIGN_MODE == "enforce":
                    _token_fallback = bool(TOKEN) and self._authed()
                else:
                    _token_fallback = self._authed()
                if not _writer and not _token_fallback:
                    self._send(401, json.dumps({
                        "error": "unauthorized",
                        "why": _sig_why,
                        "how": "sign this deposit with the key of the operator this miner is "
                               "registered under, or send X-Dendra-Token",
                    }).encode())
                    return
            # `pol == JETON` REFUSES NOTHING ON THE SIGNATURE, AND THAT IS THE PER-ROUTE RULE APPLIED
            # WHERE IT WAS STILL MISSING. `_guard` has already required the token on this route, which
            # is its policy. Refusing here as well made `enforce` a GLOBAL switch that also refused the
            # gateway's unsigned `req` deposits — the documented reason nobody could arm it. The
            # verification still RUNS: it counts, and `req` is where the client-authority path is
            # exercised on real traffic before anything depends on it.
            try:
                _put(parts[0], parts[1], data)
            except Conflict as e:
                # 409: the key already holds a sealed artifact and this deposit carries different
                # bytes. Refusing is the only outcome that keeps the audit judgeable.
                self._send(409, json.dumps({"error": str(e)}).encode())
                return
            except relay_store.StoreFull as e:
                # 507: the store is full of evidence that is STILL USEFUL. Refuse rather than erase
                # any of it — a refused deposit is retried, destroyed evidence is final.
                self._send(507, json.dumps({"error": str(e)}).encode())
                return
            except (relay_store.StoreUnavailable, OSError) as e:
                # REFUSE the deposit rather than return an "ok" the disk does not guarantee.
                self._send(503, json.dumps({"error": f"store unavailable: {type(e).__name__}"}).encode())
                return
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"route"}')

    def do_GET(self):
        parts = self._route()
        if not self._guard(self._politique(parts, "GET")):
            return
        if parts == ["stats"]:
            # GUARDED by `_guard` like everything else: counters map nothing, but no extra public
            # surface is opened just "because it is harmless".
            self._send(200, json.dumps(_stats()).encode())
            return
        if parts == ["list"]:
            # /list exposes metadata (who processes what). If a token is configured, it IS
            # required by _guard; in local mode without a token, /list stays open (debug).
            self._send(200, json.dumps(_snapshot_keys()).encode())
            return
        if len(parts) == 2 and parts[0] in STORE:
            d = _get(parts[0], parts[1])
            self._send(404, b'{"error":"not found"}') if d is None else self._send(200, d)
        else:
            self._send(404, b'{"error":"route"}')

    # THIS SILENCE IS A GUARD, NOT AN OVERSIGHT — DO NOT "FIX" IT.
    #
    # WHY. An HTTP access log writes the request PATH. In this relay the path IS the identifier:
    # `<kind>/<jobId>__<minerId>`. Re-enabling `log_message` does not "make the relay observable" — it
    # MANUFACTURES the very leak the security checks look for, and a PERSISTENT one (log rotation,
    # shipping to a collector), so worse than the leak feared in the first place. The silence is
    # consistent with the invariant that no plaintext ever appears on-chain, at the relay, or in logs.
    #
    # WHAT THIS DOES NOT MEAN: that the relay must stay BLIND. Missing observability is a real gap (it
    # blocks the detection of a store-filling attack in progress), and it is filled by AGGREGATE
    # COUNTERS — refusals by reason, evictions per minute, store occupancy, latency — none of which
    # needs a `jobId`. GRANULARITY decides, not the principle: numbers yes, paths never. That lives in
    # `exporter.py`, not here.
    #
    # A STATIC check verifies this override and a test locks it (`tests/test_relay_hardening.py`).
    # Both go red if anyone removes this no-op.
    def log_message(self, *a):
        pass  # silent BY DESIGN — read the block above before touching this line


def verdict_exposition(token: str, token_prev: str, host: str, public: bool):
    """Is the relay allowed to start? Returns ("REFUS"|"AVERTISSEMENT"|"OK", message).

    PURE OUT OF NECESSITY, NOT TASTE. The relay test bench instantiates `ThreadingHTTPServer` directly
    and never calls `main()`: an exposure decision that lives inside `main()` is structurally
    unreachable by any test. Without `os.environ` or `sys.exit`, this one is testable — a guard no test
    CAN reach is not a weak guard, it is a guard one only believes in.

    TWO CRITERIA, TWO NATURES.
    · The LISTEN ADDRESS is a FACT: non-local without a token means REFUSAL, whether `DENDRA_PUBLIC` is
      set or not, and identically to the gateway. The relay carries the sealed envelopes; an anonymous
      write overwrites audit evidence, which is irreversible.
    · `DENDRA_PUBLIC` is a DECLARATION: it adds requirements on the STRENGTH of the secret, which an
      address cannot measure.

    The 16-character floor applies to BOTH tokens. `_authed()` also accepts
    `DENDRA_RELAY_TOKEN_PREV` whatever its length: a weak previous token is a back door open for the
    whole rotation window, not a transition. An EMPTY previous token stays without effect — that is
    guard rail (1) of the rotation."""
    if public:
        if not token:
            return ("REFUS", "[relay] REFUSED: DENDRA_PUBLIC=1 without DENDRA_RELAY_TOKEN "
                             "(auth required in public).")
        if len(token) < 16:
            return ("REFUS", "[relay] REFUSED: DENDRA_PUBLIC=1 with a DENDRA_RELAY_TOKEN that is too "
                             "short (<16 chars). Generate `openssl rand -hex 24`.")
        if token_prev and len(token_prev) < 16:
            return ("REFUS", "[relay] REFUSED: DENDRA_PUBLIC=1 with a DENDRA_RELAY_TOKEN_PREV that is "
                             "too short (<16 chars). The PREVIOUS token is ACCEPTED by _authed(): a "
                             "weak one is a public back door for the whole rotation window.")
        return ("OK", "")
    # NON-LOCAL BIND WITHOUT A TOKEN: REFUSAL, whether `DENDRA_PUBLIC` is set or not — the exact
    # counterpart of guard (1) of the gateway. The bind is a FACT, `DENDRA_PUBLIC` a DECLARATION, and
    # `docker-compose.yml` already publishes the relay port on the host: a start without an environment
    # file would therefore offer a relay open to ANONYMOUS WRITES. Writing to this relay means
    # OVERWRITING sealed envelopes — destroying the evidence that makes an audit judgeable. Two
    # services of the same stack cannot hold two opposite policies on the same exposure.
    if not ecoute_locale(host) and not token:
        return ("REFUS", f"[relay] REFUSED: non-local bind ({host or '0.0.0.0'}) without "
                         "DENDRA_RELAY_TOKEN -> anonymous writes could OVERWRITE sealed reveals. "
                         "Set DENDRA_RELAY_TOKEN=`openssl rand -hex 24` (and the same value on the "
                         "gateway/miners), or bind DENDRA_RELAY_HOST=127.0.0.1.")
    return ("OK", "")


def verdict_attestation(public: bool, require: bool, allow_list):
    """Is the attestation gate usable? Returns ("REFUS"|"OK", message).

    PURE, like `verdict_exposition`, and for the same reason: the bench never calls `main()`.

    The refusal is about ASKING FOR THE GATE WITHOUT ANYTHING TO CHECK AGAINST, not about being
    public. `_attest_ok` refuses every miner when the allow-list is empty, so requiring attestation
    with no allow-list assigns NO job at all. The two tenable outcomes are "gate armed and usable" or
    "refusal at startup" — never "gate armed that shuts the network down without saying so".

    Being PUBLIC does NOT arm the gate on its own: an allow-list pins specific measured digests, so
    forcing it on an open network would exclude the independent operators that network needs. What
    public exposure does instead is make the state VISIBLE — the caller prints an unmissable banner
    when the relay serves without attestation.
    """
    if require and not allow_list:
        return ("REFUS", "[relay] REFUSED: DENDRA_ATTEST_REQUIRE=1 with an EMPTY DENDRA_ATTEST_ALLOW. "
                         "The gate refuses every miner without an allow-list, which would assign NO "
                         "job at all. Set DENDRA_ATTEST_ALLOW=<sha256[,sha256...]> (see "
                         "modea_attest.py), or leave DENDRA_ATTEST_REQUIRE unset.")
    if public and not require:
        # Not a failure — a STATE, and one that must not be discoverable only by reading the source.
        return ("OK", "[relay] NOTICE: serving PUBLIC with attestation NOT enforced "
                      "(DENDRA_ATTEST_REQUIRE unset). Miners are not required to present a signed "
                      "measured client. Do not describe attestation as active anywhere public while "
                      "this is the case.")
    return ("OK", "")


def verdict_signature(sign_mode: str, registry_url: str, token: str):
    """Can a signed write ever be ATTRIBUTED here? Returns ("REFUS"|"OK", message).

    PURE, like the two verdicts above, and for the same reason: the bench never calls `main()`.

    THE REFUSAL IS ABOUT A RELAY THAT COULD ACCEPT NOTHING, not about being strict. Under `enforce`,
    a deposit passes on an attributed signature or on the token, and on nothing else. Without a
    registry no signature can be attributed; without a token there is no fallback either — so every
    write would be refused and the symptom, an empty relay, is indistinguishable from a quiet network.
    That is the same shape as arming the attestation gate with an empty allow-list, and it gets the
    same answer: fail at boot, where it can be read.

    THE NOTICE IS THE OTHER HALF, AND IT IS THE ONE THAT WILL ACTUALLY BE PRINTED. A relay with no
    registry still verifies signatures and still counts them; what it cannot do is attribute one. The
    consequence is concrete and belongs on the console rather than in a source file: a joiner holding
    no token cannot write, however correctly it signs.
    """
    if sign_mode == "off":
        return ("OK", "")
    if not registry_url:
        if sign_mode == "enforce" and not token:
            return ("REFUS",
                    "[relay] REFUSED: DENDRA_RELAY_SIGN=enforce without DENDRA_RELAY_REGISTRY and "
                    "without DENDRA_RELAY_TOKEN. A signature can only be attributed against the "
                    "registry, and there is no token to fall back on: EVERY write would be refused. "
                    "Set DENDRA_RELAY_REGISTRY=http://<node>:1317, or keep DENDRA_RELAY_TOKEN.")
        return ("OK",
                "[relay] NOTICE: DENDRA_RELAY_REGISTRY is unset -> a signature is VERIFIED but "
                "ATTRIBUTED TO NOBODY, so it authorises no write on its own and every deposit falls "
                "back to DENDRA_RELAY_TOKEN. A joiner that holds no token cannot write, however "
                "correctly it signs. Set DENDRA_RELAY_REGISTRY=http://<node>:1317 to let a signed "
                "deposit stand alone.")
    return ("OK", "")


def main():
    # FAIL CLOSED on persistence, BEFORE any other guard: a relay serving without a store keeps the
    # audit evidence in the RAM of a process. Refuse, name the path and the system error, and invent no
    # fallback.
    if BOOT_ERR:
        print(f"[relay] REFUSED: store not writable -> {BOOT_ERR}\n"
              f"[relay] The relay CANNOT start without persistence: the sealed reveal is the only\n"
              f"[relay] artifact that makes an audit judgeable, and losing it freezes the held fee for good.\n"
              f"[relay] Fix the permissions, or pick another location with DENDRA_RELAY_STORE=<dir>.",
              file=sys.stderr)
        sys.exit(2)
    # An unrecognized value of `DENDRA_PUBLIC` is a REFUSAL, never a fallback to "not public": a typo
    # would otherwise make the exposure silent, with the guards disarmed.
    if PUBLIC_ERR:
        print(f"[relay] REFUSED: {PUBLIC_ERR}", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8645
    # Multi-machine: DENDRA_RELAY_HOST=0.0.0.0 to accept REMOTE miners (default 127.0.0.1).
    # BEFORE exposing on 0.0.0.0: set DENDRA_RELAY_TOKEN (auth) AND a WireGuard/SSH tunnel (transport).
    host = os.environ.get("DENDRA_RELAY_HOST", "127.0.0.1")
    auth = "ON" if TOKEN else "OFF(local)"
    for verdict, motif in (verdict_exposition(TOKEN, TOKEN_PREV, host, PUBLIC),
                           verdict_attestation(PUBLIC, ATTEST_REQUIRE, ATTEST_ALLOW),
                           verdict_signature(SIGN_MODE, REGISTRY_URL, TOKEN)):
        if verdict == "REFUS":
            print(motif, file=sys.stderr)
            sys.exit(2)
        # ⛔ AN "OK" THAT CARRIES A MESSAGE IS STILL SOMETHING TO SAY. The branch keys on the PRESENCE
        # of a message, not on the verdict class: restricting it to REFUS and AVERTISSEMENT accepts a
        # verdict of ("OK", notice) and DROPS its text — including the notice announcing that
        # attestation is not enforced. Such a gap is invisible from the unit tests: they call the pure
        # function and assert on its return value, which proves the message EXISTS, never that anyone
        # PRINTS it.
        elif motif:
            print(motif, file=sys.stderr)
    srv = ThreadingHTTPServer((host, port), Handler)
    n = sum(BOOT_STATS.values())
    corrupt = getattr(DISK, "last_load_corrupt", 0)
    expired = getattr(DISK, "last_load_expired", 0)
    print(f"[relay] store {STORE_DIR} - {n} record(s) reloaded "
          f"({' '.join(f'{k}={v}' for k, v in sorted(BOOT_STATS.items()) if v)or 'empty'})"
          + (f"  {corrupt} unreadable IGNORED" if corrupt else "")
          + (f"  {expired} expired purged" if expired else ""))
    print(f"[relay] eviction BY AGE only - retention {RETENTION:g}s "
          f"({RETENTION/86400:.1f} d); ceiling {MAX_ENTRIES}/kind is a guard rail, "
          f"beyond it deposits are REFUSED (507) instead of erasing evidence")
    # The state of the attestation gate is ANNOUNCED: a guard whose arming is unknown gets described as
    # armed, and that is the sentence one eventually publishes.
    print(f"[relay] attestation {'REQUIRED' if ATTEST_REQUIRE else 'NOT required'} - "
          f"{len(ATTEST_ALLOW)} recognized digest(s) (deterrence: a signed measured client, "
          f"NOT a proof of execution)")
    # The SUBJECT of the signature is announced next to its mode, because "signatures are on" and
    # "signatures name someone" are two different statements and only the second one guards anything.
    print(f"[relay] signed writes {SIGN_MODE} - attribution registry "
          f"{REGISTRY_URL or 'NOT configured (a signature attributes to NOBODY)'} - "
          f"write-once on {'/'.join(WRITE_ONCE)} (a second, DIFFERENT content is refused 409)")
    print(f"[relay] listening on http://{host}:{port}  auth={auth}  rate={RATE_MAX}/{RATE_WINDOW:g}s/IP  (encrypted transport)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
