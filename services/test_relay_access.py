#!/usr/bin/env python3
"""Bench for the relay's write policy: who may write, and what may be destroyed.

It holds two halves, and both must hold:
  · what must open: reading ciphertext with no secret at all. A service-wide token would answer 401
    on every route to an operator whose hardware and genesis digest `join.sh` has just verified,
    because `DENDRA_RELAY_TOKEN` is a shared secret that `network-info.txt` does not carry and no
    self-service procedure issues.
  · what must stay closed: writing. A deposit key names a miner (`res/<jid>__<mid>`), and the sealed
    artifact filed under it is the only thing an audit can conclude on.

The bench does not keep its own copy of the policy table, since a copy only confirms itself: Part 1
imports and calls the server's `_policy`. A table also cannot ask who signed, so Part 2 runs a real
relay over a loopback socket with real secp256k1 signatures, the only way to ask whether a signature
names anybody.

Usage: python3 test_relay_access.py
"""
import base64
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

# The subject is whatever `relay` and `cli` resolve to on this path, and nothing else in the
# bench opens a file by its own location. Point DENDRA_MODEA at a mutated copy of the directory to bench it.
sys.path.insert(0, os.environ.get("DENDRA_MODEA") or os.path.dirname(os.path.abspath(__file__)))

N = KO = 0


def case(name, got, expected):
    global N, KO
    N += 1
    if got == expected:
        print(f"  OK  {name}")
    else:
        KO += 1
        print(f"  KO  {name}  (expected {expected!r}, got {got!r})")


# == Part 1 — the policy table, read from the server itself =======================================
os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix="dendra-access-")
os.environ.pop("DENDRA_RELAY_TOKEN", None)
os.environ.pop("DENDRA_RELAY_REGISTRY", None)
os.environ["DENDRA_RELAY_SIGN"] = "off"
relay = importlib.import_module("relay")
H = relay.Handler
OPEN, TOKEN, SIGNED_OR_TOKEN = H.POLICY_OPEN, H.POLICY_TOKEN, H.POLICY_SIGNED_OR_TOKEN


def policy(parts, method):
    """The server's own function, not a copy of it. `_policy` reads only class attributes, so the
    class itself serves as `self` — no socket, no request, nothing to imitate."""
    return H._policy(H, parts, method)


J = "job1785643115494__dm1teqlpyx9sctv4d954eejvz"

print("== What must open: a joiner pulls its own work with no secret at all ==")
case("GET req/<jid>__<mid>    -> open", policy(["req", J], "GET"), OPEN)
case("GET res/<jid>__<mid>    -> open", policy(["res", J], "GET"), OPEN)
case("GET reveal/<jid>__<mid> -> open", policy(["reveal", J], "GET"), OPEN)
case("GET pub/<mid>           -> open", policy(["pub", "dm1teq"], "GET"), OPEN)

print()
print("== Writing miner-owned content: a signature, or the token as the transition path ==")
for k in ("pub", "res", "reveal", "attest"):
    case(f"POST {k:<6} -> signed-or-token", policy([k, J], "POST"), SIGNED_OR_TOKEN)

print()
print("== `req`: the gateway writes it with the token, a client with its own wallet signs it (C37) ==")
case("POST req -> signed-or-token", policy(["req", J], "POST"), SIGNED_OR_TOKEN)
case("POST /req with no identifier -> token (an unknown route)", policy(["req"], "POST"), TOKEN)
case("POST /req/a/b (3 segments)   -> token (an unknown route)", policy(["req", "a", "b"], "POST"), TOKEN)

print()
print("== The work queue is open; writing it is not ==")
# `GET /list` is the only route that tells a miner a job is waiting for it (`miner.py::main`
# opens its loop with it). Behind a token, an outside operator gets 401 on every round, the client
# returns `{}`, and the daemon waits for jobs forever.
# Opening it reveals nothing new: the chain already publishes who processes what, to anyone and with
# no secret (`/dendra/jobs/v1/job` carries job_id and miner_id, and
# `/dendra/jobs/v1/assigned_committee/<jobId>` the drawn members).
case("GET  list  -> OPEN (the work queue)", policy(["list"], "GET"), OPEN)
case("POST list  -> token (reading it is not writing it)", policy(["list"], "POST"), TOKEN)
case("GET  stats -> token (nothing depends on it)", policy(["stats"], "GET"), TOKEN)


class _Target:
    """What `Handler._route` reads: the request target. Its method is the server's, called as is."""

    def __init__(self, path):
        self.path = path


def route(path):
    t = _Target(path)
    return H._route(t), t._query


print()
print("== The filtered queue is the same route under the same policy ==")
# The query string is split off before the policy is decided. Left in the segment, `list?suffix=` is
# an unknown route and takes the strictest policy: the filter would be closed where the queue is open.
_p, _q = route("/list?suffix=__dm1teqlpyx9sctv4d954eejvz")
case("GET  list?suffix= -> OPEN, like the queue it filters", policy(_p, "GET"), OPEN)
case("POST list?suffix= -> token", policy(_p, "POST"), TOKEN)
case("...and the query is set aside, not lost", _q, "suffix=__dm1teqlpyx9sctv4d954eejvz")
case("GET  stats?x=1    -> token (a query opens nothing)", policy(route("/stats?x=1")[0], "GET"), TOKEN)
case("GET  admin?x=1    -> token (an unknown route stays unknown)", policy(route("/admin?x=1")[0], "GET"),
     TOKEN)

print()
print("== An unknown route takes the strictest policy ==")
case("GET  /admin                  -> token", policy(["admin"], "GET"), TOKEN)
case("POST /pub with no identifier -> token", policy(["pub"], "POST"), TOKEN)
case("GET  /req/a/b (3 segments)   -> token", policy(["req", "a", "b"], "GET"), TOKEN)

# == Part 2 — a real relay, real signatures, and the question the table could not ask =============
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
except ImportError as exc:                       # noqa: BLE001
    print(f"\nRELAY_ACCESS_SUMMARY cases={N} ko={N} "
          f"(cryptography missing: {exc} - the half that matters cannot run, so this bench is red: "
          f"it does not report green for what it did not run.)")
    sys.exit(1)

from modea import registry_cache, relay_canon, relay_carrier  # noqa: E402
from modea import relay_signature as rs  # noqa: E402
from modea.cosmos_addr import address_from_pubkey  # noqa: E402

ACCT, SEQ = "10", "160"
MINER, VICTIM = "dm1honest", "dm1victim"


def identity():
    sk = ec.generate_private_key(ec.SECP256K1())
    pub = sk.public_key().public_bytes(encoding=serialization.Encoding.X962,
                                       format=serialization.PublicFormat.CompressedPoint)
    return sk, pub, address_from_pubkey(pub)


def signed_headers(sk, pub, addr, *, kind, key, body, miner, height):
    """The headers an honest writer sends. Built here rather than through `dendrad`, which needs a
    binary and a funded account; `tests/test_relay_carrier_verifier.py` is what ties this
    reconstruction to a real binary signature."""
    message = relay_canon.canonical_message(kind, key, body, miner, height)
    doc = relay_carrier.document_amino(hashlib.sha256(message).digest(), addr, ACCT, SEQ)
    r, s = utils.decode_dss_signature(sk.sign(doc, ec.ECDSA(hashes.SHA256())))
    return {rs.HEADER_MINER: miner, rs.HEADER_HEIGHT: str(height),
            rs.HEADER_PUBKEY: base64.b64encode(pub).decode(),
            rs.HEADER_SIG: base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode(),
            rs.HEADER_ACCT: ACCT, rs.HEADER_SEQ: SEQ}


class Registry:
    """The miner registry the relay attributes against. Injected, so the bench needs no chain."""

    def __init__(self, table, state=registry_cache.FEES):
        self._table, self._state = table, state

    def operator(self, miner_id):
        return self._table.get(miner_id), self._state


class MuteRegistry:
    """No read has ever succeeded — nothing can be attributed to anyone. It is a state, not an error,
    and it is the one a signed write must be refused on: a guard that cannot identify must refuse."""

    def operator(self, miner_id):
        return None, registry_cache.NEVER_READ


class Relay:
    """A real relay on an ephemeral port, in a chosen regime, with an injected registry."""

    def __init__(self, mode, registry, token="", kit_min="", kit_deadline="", registry_url="",
                 jobs=None, price="", job_docs=None, committees=None, module_path=None):
        os.environ["DENDRA_RELAY_SIGN"] = mode
        os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix=f"dendra-access-{mode}-")
        if token:
            os.environ["DENDRA_RELAY_TOKEN"] = token
        else:
            os.environ.pop("DENDRA_RELAY_TOKEN", None)
        # The kit floor and the request price are read from the environment at import, like the deployed
        # relay reads them; each variable is set here or absent, never left over from an earlier instance.
        for name, value in (("DENDRA_KIT_MIN", kit_min), ("DENDRA_KIT_DEADLINE", kit_deadline),
                            ("DENDRA_REQUEST_PRICE_UDNDR", price)):
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        self.token = token
        self.token_uses = 0
        self.last_refusal = None            # the JSON body of the last refusal, for the cases that read a field
        if module_path:
            # A variant of the shipped file (a mutant), imported under its own name.
            spec = importlib.util.spec_from_file_location(
                "relay_variant_" + os.path.basename(module_path)[:-3], module_path)
            self.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.mod)
        else:
            self.mod = importlib.reload(relay)
        self.mod.REGISTRY = registry
        # The job registry (`job_id -> paying client`) and the reader of one job by id, both injected so
        # that the bench reads no chain. Left as the module set them when not given.
        if jobs is not None:
            self.mod.JOBS = jobs
        if job_docs is not None:
            self.mod.JOB_DOC_READER = job_docs
        if committees is not None:
            self.mod.COMMITTEE_READER = committees
        # `_refresh_cache` reads nothing without a registry URL. The bench's registry is injected and reads
        # no network, so the URL only says "a registry is wired", which is the deployed relay's state.
        if registry_url:
            self.mod.REGISTRY_URL = registry_url
        self.srv = self.mod.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _call(self, method, path, data=None, hdrs=None, with_token=False):
        return self._call_why(method, path, data, hdrs, with_token)[0]

    def _call_why(self, method, path, data=None, hdrs=None, with_token=False):
        """(status, the relay's `why` on a refusal, "" otherwise)."""
        h = dict(hdrs or {})
        if with_token and self.token:
            h["X-Dendra-Token"] = self.token
            self.token_uses += 1
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/{path}", data=data,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
                return r.status, ""
        except urllib.error.HTTPError as e:
            raw = e.read()
            self.last_refusal = None
            try:
                doc = json.loads(raw)
                self.last_refusal = doc if isinstance(doc, dict) else None
                why = doc.get("why", "")
            except Exception:  # noqa: BLE001 -- a refusal without a JSON body has no stated reason
                why = ""
            return e.code, why if isinstance(why, str) else ""

    def post(self, kind, key, body, hdrs=None, with_token=False):
        return self._call("POST", f"{kind}/{key}", body, hdrs, with_token)

    def post_why(self, kind, key, body, hdrs=None, with_token=False):
        return self._call_why("POST", f"{kind}/{key}", body, hdrs, with_token)

    def get(self, kind, key):
        return self._call("GET", f"{kind}/{key}")

    def body(self, kind, key):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{kind}/{key}", timeout=5) as r:
            return r.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


SEALED = b'{"nonce":"aa","ct":"the sealed reveal of the honest miner"}'
FORGED = b'{"nonce":"bb","ct":"overwritten by a stranger"}'
KEY = f"job42__{VICTIM}"

sk_v, pub_v, addr_v = identity()          # the victim: registered operator of dm1victim
sk_x, pub_x, addr_x = identity()          # a stranger: a perfectly valid key, registered nowhere
sk_m, pub_m, addr_m = identity()          # another registered miner, with its own operator address
REGISTERED = Registry({VICTIM: addr_v, MINER: addr_m})

print()
print("== Green: the joiner's path and the legitimate deposit keep working ==")
with Relay("enforce", REGISTERED) as r:
    case("the registered operator deposits its own reveal -> 200",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=100)), 200)
    case("a joiner reads ciphertext with no secret at all -> 200", r.get("reveal", KEY), 200)
    case("...and reads exactly the bytes deposited", r.body("reveal", KEY), SEALED)

    print()
    print("== (a) A stranger signs its own deposit over someone else's reveal ==")
    # A valid signature made with a key the registry knows nothing about, over the victim's deposit
    # key. Without attribution this would answer 200 and destroy the bytes above for good.
    case("a foreign key signing for dm1victim -> refused (401)",
         r.post("reveal", KEY, FORGED,
                signed_headers(sk_x, pub_x, addr_x, kind="reveal", key=KEY, body=FORGED,
                               miner=VICTIM, height=101)), 401)
    case("...and the sealed reveal is untouched", r.body("reveal", KEY), SEALED)
    # The same attack from a miner the registry does know: it is correctly attributed to itself, so
    # attribution alone says yes. What refuses is the deposit key naming another miner.
    K2 = f"job42__{VICTIM}"
    case("a registered miner depositing under another miner's key -> refused (401)",
         r.post("reveal", K2, FORGED,
                signed_headers(sk_m, pub_m, addr_m, kind="reveal", key=K2, body=FORGED,
                               miner=MINER, height=101)), 401)
    case("...and the sealed reveal is still untouched", r.body("reveal", KEY), SEALED)

    print()
    print("== (b) Replay of a valid signed deposit ==")
    K3, BODY3 = f"job43__{VICTIM}", b'{"nonce":"cc","ct":"second job"}'
    h3 = signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=K3, body=BODY3,
                        miner=VICTIM, height=102)
    case("the honest deposit passes once -> 200", r.post("reveal", K3, BODY3, h3), 200)
    # Same bytes, same signature, replayed. The digest has been seen, so the replay guard refuses it
    # before the store is touched — which is also what stops a captured deposit being re-injected.
    case("the exact same signed deposit, replayed -> refused (401)",
         r.post("reveal", K3, BODY3, h3), 401)

    print()
    print("== (c) A second, different content on an existing sealed artifact ==")
    # Signed by the legitimate operator, attributed, replay-clean: every authorisation question
    # answers yes, and it is still refused. Write-once is not an authorisation question.
    OTHER = b'{"nonce":"dd","ct":"a second answer for the same job"}'
    case("the registered operator replaces its own reveal -> 409",
         r.post("reveal", KEY, OTHER,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=OTHER,
                               miner=VICTIM, height=103)), 409)
    case("...and the first artifact is still the one stored", r.body("reveal", KEY), SEALED)
    # A retry that re-sends what is already stored destroys nothing and must not strand the worker.
    case("re-sending the identical bytes -> 200 (nothing to destroy)",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=104)), 200)

print()
print("== (d) The registry is silent: a signature that names nobody authorises nobody ==")
with Relay("enforce", MuteRegistry()) as r:
    case("a perfectly valid signature, registry never read -> refused (401)",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=100)), 401)

print()
print("== The token remains the fallback, and the only one while the registry is silent ==")
with Relay("enforce", MuteRegistry(), token="t" * 32) as r:
    case("unattributable signature + token -> 200",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=100), with_token=True), 200)
    K4 = f"job99__{VICTIM}"
    case("unattributable signature without the token -> 401",
         r.post("reveal", K4, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=K4, body=SEALED,
                               miner=VICTIM, height=100)), 401)
    case("a joiner still reads with no secret -> 200", r.get("reveal", KEY), 200)

print()
print("== `enforce` does not cut off the gateway (otherwise it could never be armed) ==")
with Relay("enforce", REGISTERED, token="t" * 32) as r:
    RJ = f"job77__{MINER}"
    case("POST req unsigned with the token -> 200",
         r.post("req", RJ, b'{"ct":"x"}', with_token=True), 200)
    case("POST req without the token -> 401", r.post("req", RJ, b'{"ct":"x"}'), 401)
    case("a second, different sealed request on the same key -> 409",
         r.post("req", RJ, b'{"ct":"y"}', with_token=True), 409)

print()
print("== Identity kinds stay replaceable (a rotated key must not be frozen) ==")
with Relay("enforce", REGISTERED, token="t" * 32) as r:
    case("POST pub once -> 200", r.post("pub", MINER, b'{"pub":"01"}', with_token=True), 200)
    case("POST pub again with a new key -> 200",
         r.post("pub", MINER, b'{"pub":"02"}', with_token=True), 200)
    case("POST attest again after an upgrade -> 200",
         r.post("attest", MINER, b'{"measured_hash":"02"}', with_token=True), 200)

print()
print("== `off` and `observe` still let an unsigned deposit through (nothing is armed by surprise) ==")
for mode in ("off", "observe"):
    with Relay(mode, MuteRegistry()) as r:
        case(f"{mode}: unsigned deposit, no token, no registry -> 200",
             r.post("res", f"jobA__{MINER}", b'{"ct":"z"}'), 200)

print()
print("== The configuration actually deployed: `observe` with a token ==")
# The same attack applies under `observe`: a self-signed deposit must not count as "signed" and skip the
# token. A mode is not a defence; it only decides whether an unsigned write is refused.
with Relay("observe", REGISTERED, token="t" * 32) as r:
    case("a stranger's self-signed deposit, no token -> 401",
         r.post("reveal", KEY, FORGED,
                signed_headers(sk_x, pub_x, addr_x, kind="reveal", key=KEY, body=FORGED,
                               miner=VICTIM, height=200)), 401)
    case("the registered operator, no token -> 200",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=200)), 200)
    case("an older client that cannot sign, with the token -> 200",
         r.post("res", f"jobB__{MINER}", b'{"ct":"z"}', with_token=True), 200)
    case("...and the same client without the token -> 401",
         r.post("res", f"jobC__{MINER}", b'{"ct":"z"}'), 401)

print()
print("== The joiner's actual loop, against a real relay with a token configured ==")
# Part 1 exercises `_policy`, a pure function, and says nothing about whether `do_GET` honours its
# answer: a handler that guarded the route separately would keep every table case green. So this block
# speaks HTTP, with a token configured on the relay and never sent by the caller, the position of an
# outside operator.
with Relay("observe", REGISTERED, token="t" * 32) as r:
    case("GET /list with no token -> 200 (a miner can see its work)",
         r._call("GET", "list"), 200)
    # A 200 alone would also be returned by an empty stub. Read the body and require the queue's
    # shape: the point is that the miner sees its work, not that the route answers.
    with urllib.request.urlopen(f"http://127.0.0.1:{r.port}/list", timeout=5) as _r:
        _body = _r.read().decode()
    case("...and the body really carries the queue", 200 if _body.startswith("{") and '"res"' in _body else 0, 200)
    case("POST /list with no token -> 401 (reading is not writing)",
         r._call("POST", "list", b"{}"), 401)
    case("GET /stats with no token -> 401 (nothing depends on it)",
         r._call("GET", "stats"), 401)
    # The filtered queue, over HTTP, from the outside operator's position. Both halves again: the
    # slice opens exactly like the queue, and a query string opens nothing that was closed. Two keys
    # are filed first, one per miner: a slice measured over an empty store would prove nothing.
    r.post("res", f"jobL__{MINER}", b'{"ct":"l"}', with_token=True)
    r.post("res", f"jobL__{VICTIM}", b'{"ct":"v"}', with_token=True)
    case("GET /list?suffix=__<miner> with no token -> 200",
         r._call("GET", f"list?suffix=__{MINER}"), 200)
    with urllib.request.urlopen(f"http://127.0.0.1:{r.port}/list?suffix=__{MINER}", timeout=5) as _r:
        _slice = json.loads(_r.read())
    _keys = [k for v in _slice.values() for k in v]
    case("...and it carries this miner's keys and no one else's",
         200 if _keys and all(k.endswith(f"__{MINER}") for k in _keys) else 0, 200)
    case("POST /list?suffix= with no token -> 401", r._call("POST", f"list?suffix=__{MINER}", b"{}"), 401)
    case("GET /stats?x=1 with no token -> 401 (a query opens nothing)", r._call("GET", "stats?x=1"), 401)
    case("POST res/<key>?x=1 with the token -> 400 (a write reads no parameter)",
         r._call("POST", f"res/jobQ__{MINER}?x=1", b'{"ct":"q"}', with_token=True), 400)
    case("...and nothing was filed under the key", r.get("res", f"jobQ__{MINER}"), 404)


# == Part 3 — the registry re-read on an unknown miner (8), the kit floor (5), and the ======
# == plaintext that no public route returns (S6) ==================================================
class LiveRegistry(Registry):
    """A registry whose chain moves: `pending` holds the miners registered since the last read, and a
    `refresh()` is the read that learns them. `reads` counts what the relay asked of the REST gateway."""

    def __init__(self, table, pending=None):
        super().__init__(table, registry_cache.FEES)
        self.pending = dict(pending or {})
        self.reads = 0

    def state(self):
        return self._state

    def refresh(self):
        self.reads += 1
        self._table.update(self.pending)
        self.pending = {}
        return True


LATE, GHOST = "dm1late", "dm1ghost"
sk_l, pub_l, addr_l = identity()          # registered after the relay's last read
sk_g, pub_g, addr_g = identity()          # a key the chain never records

print()
print("== (R1) A miner registered after the last read is accepted at its first deposit ==")
live = LiveRegistry({VICTIM: addr_v, MINER: addr_m}, pending={LATE: addr_l})
with Relay("enforce", live, registry_url="http://registry.bench.invalid") as r:
    case("the fresh copy does not know dm1late yet", live._table.get(LATE), None)
    case("its first signed pub deposit -> 200 (one re-read, then the same verdict again)",
         r.post("pub", LATE, b'{"pub":"01"}',
                signed_headers(sk_l, pub_l, addr_l, kind="pub", key=LATE, body=b'{"pub":"01"}',
                               miner=LATE, height=300)), 200)
    case("...and the registry was read exactly once for it", live.reads, 1)
    print()
    print("== (R1) An identifier the chain does not know stays refused, and the re-read is bounded ==")
    st, why = r.post_why("pub", GHOST, b'{"pub":"02"}',
                         signed_headers(sk_g, pub_g, addr_g, kind="pub", key=GHOST, body=b'{"pub":"02"}',
                                        miner=GHOST, height=300))
    case("a valid signature under an identifier absent from the chain -> 401", st, 401)
    case("...refused as UNKNOWN_MINER", why.split(" ")[0] if why else why, "UNKNOWN_MINER")
    case("...and no second read within REGISTRY_RETRY of the first (the gateway is not hammered)",
         live.reads, 1)
    # REGISTRY_RETRY seconds later (the attempt clock is the state the floor reads; it is set back, as a
    # clock would be advanced), the unknown identifier earns one re-read, and is refused again.
    r.mod._REG_ATTEMPT["miner"] = 0.0
    case("after REGISTRY_RETRY the unknown identifier earns one re-read and is still refused (401)",
         r.post("pub", GHOST, b'{"pub":"03"}',
                signed_headers(sk_g, pub_g, addr_g, kind="pub", key=GHOST, body=b'{"pub":"03"}',
                               miner=GHOST, height=301)), 401)
    case("...the registry was read a second time", live.reads, 2)
    case("a second unknown identifier right after -> 401 with no further read",
         r.post("pub", "dm1ghost2", b'{"pub":"04"}',
                signed_headers(sk_g, pub_g, addr_g, kind="pub", key="dm1ghost2", body=b'{"pub":"04"}',
                               miner="dm1ghost2", height=301)), 401)
    case("...bounded by REGISTRY_RETRY", live.reads, 2)
    case("the counter names the re-reads", r.mod.COUNTERS.get("registry_reread_unknown_miner"), 2)
    case("nothing was filed under the unknown identifiers", r.get("pub", GHOST), 404)

print()
print("== (R1) Without a registry URL nothing is re-read: an unconfigured registry stays what it is ==")
cold = LiveRegistry({VICTIM: addr_v}, pending={LATE: addr_l})
with Relay("enforce", cold) as r:
    case("dm1late -> 401 (no gateway to ask)",
         r.post("pub", LATE, b'{"pub":"01"}',
                signed_headers(sk_l, pub_l, addr_l, kind="pub", key=LATE, body=b'{"pub":"01"}',
                               miner=LATE, height=300)), 401)
    case("...and the bench registry was never read", cold.reads, 0)

print()
print("== (S6) No public route returns a prompt or an answer in plaintext ==")
# The client seals before it deposits (modea.client.Client.submit), and the relay serves the bytes it was
# given: what this block measures is that the deployed path, sealed by the shipped client code and read
# back through every open route, never shows the prompt or the answer. The relay has no key and no route
# takes one; a plaintext on a public route could only come from a client that did not seal.
from modea import crypto  # noqa: E402
from modea.client import Client  # noqa: E402
import reveal_helpers  # noqa: E402

PROMPT = "the secret prompt 7f3a9c of this bench"
ANSWER = "the secret answer 2b8e1d of this bench"
m_sk, m_pk = crypto.gen_keypair()                       # the miner's X25519 identity
sub, session = Client().submit("jobS6", m_pk, PROMPT)
req_body = json.dumps({"client_eph_pk": sub.client_eph_pk.hex(), "nonce": sub.sealed_prompt.nonce.hex(),
                       "ct": sub.sealed_prompt.ct.hex()}).encode()
sealed_answer = crypto.encrypt(session, ANSWER.encode("utf-8"), aad=b"jobS6")
res_body = json.dumps({"nonce": sealed_answer.nonce.hex(), "ct": sealed_answer.ct.hex(),
                       "in_tok": 3, "out_tok": 4, "kit_version": 2}).encode()
# The reveal: the operator seals the prompt and the answer to one juror's X25519 key with the shipped sealer
# (reveal_helpers._seal_to, the same call reveal_job makes), so what the reveal route serves is measured too.
j_sk, j_pk = crypto.gen_keypair()                       # the juror's X25519 identity
reveal_body = json.dumps(reveal_helpers._seal_to(j_pk.hex(), {"prompt": PROMPT, "answer": ANSWER})).encode()
KS6 = f"jobS6__{VICTIM}"
with Relay("observe", REGISTERED, token="t" * 32) as r:
    case("the sealed request is deposited (gateway, token) -> 200", r.post("req", KS6, req_body, with_token=True), 200)
    case("the sealed answer is deposited by the registered operator -> 200",
         r.post("res", KS6, res_body,
                signed_headers(sk_v, pub_v, addr_v, kind="res", key=KS6, body=res_body,
                               miner=VICTIM, height=400)), 200)
    case("the sealed reveal is deposited by the registered operator -> 200",
         r.post("reveal", KS6, reveal_body,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KS6, body=reveal_body,
                               miner=VICTIM, height=401)), 200)
    leaked = []
    for route in (f"req/{KS6}", f"res/{KS6}", f"reveal/{KS6}", f"pub/{VICTIM}", f"attest/{VICTIM}",
                  "list", f"list?suffix=__{VICTIM}"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{r.port}/{route}", timeout=5) as _r:
                text = _r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
        if PROMPT in text or ANSWER in text:
            leaked.append(route)
    case("no open route carries the prompt or the answer in plaintext", leaked, [])
    case("...and the sealed request read back is exactly what was deposited", r.body("req", KS6), req_body)
    case("...the client can still open the answer it reads back",
         Client().open_result("jobS6", session, crypto.Sealed(
             bytes.fromhex(json.loads(r.body("res", KS6))["nonce"]),
             bytes.fromhex(json.loads(r.body("res", KS6))["ct"]))), ANSWER)
    rv = json.loads(r.body("reveal", KS6))
    case("...the reveal read back is the sealed shape and nothing else", sorted(rv), ["client_eph_pk", "ct", "nonce"])
    jk = crypto.derive_session_key(j_sk, bytes.fromhex(rv["client_eph_pk"]), info=reveal_helpers.REVEAL_INFO)
    case("...and the juror alone opens it to the prompt and the answer",
         json.loads(crypto.decrypt(jk, crypto.Sealed(bytes.fromhex(rv["nonce"]), bytes.fromhex(rv["ct"])))),
         {"prompt": PROMPT, "answer": ANSWER})
    case("GET /stats without the token -> 401 (the counters are not a public route either)",
         r._call("GET", "stats"), 401)

print()
print("== (D1) The kit floor: dormant with nothing set, announced before the deadline, armed after ==")
rsv = relay  # the module as last reloaded: the pure functions are the same in every instance
case("both unset -> dormant", rsv.kit_policy_from_env("", "")[:2], (None, None))
case("...and without error", rsv.kit_policy_from_env("", "")[2], None)
case("KIT_MIN alone -> a configuration error, never a floor without a date",
     rsv.kit_policy_from_env("3", "")[2] is not None, True)
case("KIT_DEADLINE alone -> a configuration error", rsv.kit_policy_from_env("", "2026-10-23T23:59:00Z")[2] is not None, True)
case("a KIT_MIN that is not an integer -> error", rsv.kit_policy_from_env("v0.2.2", "2026-10-23T23:59:00Z")[2] is not None, True)
case("KIT_MIN=0 -> error (publish_network.sh publishes [1-9][0-9]* and a floor of 0 refuses nothing)",
     rsv.kit_policy_from_env("0", "2026-10-23T23:59:00Z")[2] is not None, True)
case("a Unicode digit (superscript two) -> the named error, not a traceback",
     rsv.kit_policy_from_env(chr(178), "2026-10-23T23:59:00Z")[2] is not None, True)
case("a leading zero -> error", rsv.kit_policy_from_env("03", "2026-10-23T23:59:00Z")[2] is not None, True)
case("ten digits -> error, the form the faucet and the season read too (modea/kit_floor.py)",
     rsv.kit_policy_from_env("1234567890", "2026-10-23T23:59:00Z")[2] is not None, True)
case("a deadline with one-digit fields -> error", rsv.kit_policy_from_env("3", "2026-1-2T3:4:5Z")[2] is not None, True)
case("a deadline without the UTC marker -> error", rsv.kit_policy_from_env("3", "2026-10-23 23:59")[2] is not None, True)
case("a usable pair -> the floor and the instant", rsv.kit_policy_from_env("3", "1970-01-01T00:00:10Z")[:2], (3, 10.0))
case("a policy in error refuses to start", rsv.verdict_kit_policy("half set", None, None)[0], "REFUSE")
case("a dormant policy starts and says so", rsv.verdict_kit_policy(None, None, None)[0], "OK")

BODY_OLD = b'{"pub":"01","kit_version":2}'
BODY_CUR = b'{"pub":"01","kit_version":3}'
BODY_NEW = b'{"pub":"01","kit_version":9}'
BODY_MUTE = b'{"pub":"01"}'


def pub_from(body, height):
    return signed_headers(sk_v, pub_v, addr_v, kind="pub", key=VICTIM, body=body, miner=VICTIM, height=height)


with Relay("enforce", REGISTERED, kit_min="3", kit_deadline="2999-01-01T00:00:00Z") as r:
    case("before the deadline: a body without kit_version is served (200)",
         r.post("pub", VICTIM, BODY_MUTE, pub_from(BODY_MUTE, 500)), 200)
    case("before the deadline: a kit below the floor is served (200)",
         r.post("pub", VICTIM, BODY_OLD, pub_from(BODY_OLD, 501)), 200)
    case("before the deadline the floor applies to nothing", r.mod.kit_floor_now(), None)
    case("...and the module says the pair is usable", r.mod.KIT_POLICY_ERROR, None)

with Relay("enforce", REGISTERED, kit_min="3", kit_deadline="2020-01-01T00:00:00Z") as r:
    case("the floor is armed at this instant", r.mod.kit_floor_now(), 3)
    st, why = r.post_why("pub", VICTIM, BODY_MUTE, pub_from(BODY_MUTE, 600))
    case("armed: a signed body without kit_version -> 401", st, 401)
    case("...named STALE_KIT", why.split(" ")[0] if why else why, "STALE_KIT")
    st, why = r.post_why("pub", VICTIM, BODY_OLD, pub_from(BODY_OLD, 601))
    case("armed: kit_version 2 under a floor of 3 -> 401 STALE_KIT", (st, why.split(" ")[0] if why else why),
         (401, "STALE_KIT"))
    # C14: the refusal points to the page that settles it, in a field of its own. `why` keeps starting with the
    # reason word, which the miner side and the self-test read; the link is never part of it.
    case("...the refusal carries help, the update page", (r.last_refusal or {}).get("help"), r.mod.relay_write.HELP_UPDATE_KIT)
    case("...and why does not carry the link", "http" in (r.last_refusal or {}).get("why", "http"), False)
    case("armed: kit_version 3 -> 200", r.post("pub", VICTIM, BODY_CUR, pub_from(BODY_CUR, 602)), 200)
    case("armed: a kit ahead of the floor -> 200", r.post("pub", VICTIM, BODY_NEW, pub_from(BODY_NEW, 603)), 200)
    # The version is read from the signed body only. A header that claims an old kit changes nothing,
    # since nothing on the path can rewrite what the signature covers.
    case("a forged header claiming kit 0 over a current signed body -> 200 (the body decides)",
         r.post("pub", VICTIM, BODY_CUR, {**pub_from(BODY_CUR, 604), "X-Dendra-Kit-Version": "0"}), 200)
    # A body rewritten on the path to carry a lower kit_version breaks the signature: INVALID_SIGNATURE,
    # not a refusal that reads as the miner's age.
    st, why = r.post_why("pub", VICTIM, BODY_OLD, pub_from(BODY_CUR, 605))
    case("a body rewritten in transit to a lower kit version -> 401, and not as STALE_KIT",
         (st, why.split(" ")[0] if why else why), (401, "INVALID_SIGNATURE"))
    case("...and it points to the relay refusals section", (r.last_refusal or {}).get("help"),
         r.mod.HELP_RELAY_REFUSALS)
    st, why = r.post_why("pub", VICTIM, BODY_CUR)
    case("an unsigned write without the token -> 401 'unsigned', with the same section",
         (st, why.split(" ")[0] if why else why, (r.last_refusal or {}).get("help")),
         (401, "unsigned", r.mod.HELP_RELAY_REFUSALS))
    case("reads stay open under the armed floor: GET pub/<mid> -> 200", r.get("pub", VICTIM), 200)
    case("...GET /list -> 200", r._call("GET", "list"), 200)
    case("the refusals are counted by code", r.mod.SIGN_REFUSALS.get("STALE_KIT"), 2)
    case("...and under their own counter, not as unattributed writes",
         (r.mod.SIGN_STATS["stale_kit"], r.mod.SIGN_STATS["unattributed"]), (2, 0))


# == Part 4 — C37: the second way in. A client with its own wallet signs and pays for its own request =====
# The client opens the job on chain (the fee goes to escrow), then deposits the sealed request on `req`
# signed with the key that paid. Two halves again, and both are held: the signature alone is accepted, the
# token alone is accepted, and nothing else is. The price (`DENDRA_REQUEST_PRICE_UDNDR`) is compared with
# the fee the chain holds for the job; unset, the signature path refuses and the token path is unchanged.
print()
print("== (C37) The request price and the escrowed fee are read, never assumed ==")
case("price unset -> (None, None): the signed path stays closed", rsv.request_price_from_env(""), (None, None))
case("price 1000 -> usable", rsv.request_price_from_env("1000"), (1000, None))
for bad in ("0", "01", "1.5", "-3", "1e3", chr(178), " 1000", "1000 ", " ", "+5"):
    case(f"price {bad!r} -> a configuration error", rsv.request_price_from_env(bad)[1] is not None, True)
case("a price in error refuses to start", rsv.verdict_request_price("bad", None)[0], "REFUSE")
case("an unset price starts and says so", rsv.verdict_request_price(None, None)[0], "OK")


def job_doc(jid, **fields):
    return json.dumps({"job": {"job_id": jid, **fields}}).encode()


case("fee read from the job read by id", rsv.job_fee_from_doc(job_doc("j1", fee="8470"), "j1"), 8470)
case("fee absent -> 0 (proto3 omits a zero), which no price accepts", rsv.job_fee_from_doc(job_doc("j1"), "j1"), 0)
case("a document about another job -> nothing read", rsv.job_fee_from_doc(job_doc("j2", fee="9"), "j1"), None)
case("a fee that is not an unsigned integer -> nothing read",
     [rsv.job_fee_from_doc(job_doc("j1", fee=v), "j1") for v in ("-1", "1.5", True, None, "")], [None] * 5)
case("no job object -> nothing read", rsv.job_fee_from_doc(b'{"job": null}', "j1"), None)
case("state read from the job read by id", rsv.job_state_from_doc(job_doc("j1", state="open"), "j1"), "open")
case("a settled state is read as it is, never shortened",
     rsv.job_state_from_doc(job_doc("j1", state="open+paid+optimistic"), "j1"), "open+paid+optimistic")
case("state absent -> '' (proto3 omits an empty string), which is not the open state",
     rsv.job_state_from_doc(job_doc("j1"), "j1"), "")
case("a state that is not a string -> nothing read",
     [rsv.job_state_from_doc(job_doc("j1", state=v), "j1") for v in (7, None, True, ["open"])], [None] * 4)
case("the state of another job -> nothing read", rsv.job_state_from_doc(job_doc("j2", state="open"), "j1"), None)
case("committee read from assigned_committee, primary first",
     rsv.committee_from_doc(b'{"members":["dm1a","dm1b","dm1c"]}'), ["dm1a", "dm1b", "dm1c"])
case("members absent -> [] (proto3 omits an empty list), which names no miner", rsv.committee_from_doc(b"{}"), [])
case("members that are not a list of non-empty strings -> nothing read",
     [rsv.committee_from_doc(json.dumps({"members": v}).encode()) for v in ("dm1a", [1], [""], None)], [None] * 4)
case("a document that is not an object -> nothing read", rsv.committee_from_doc(b"[]"), None)


class Jobs:
    """The job registry a `req` is attributed against: `job_id -> paying client`. Injected, no chain."""

    def __init__(self, table):
        self._table = table

    def client(self, job_id):
        return self._table.get(job_id), registry_cache.FEES

    def list_truncated(self):
        return False


sk_c, pub_c, addr_c = identity()          # a client with its own wallet: it opened and paid for the jobs below
PRICE = "1000"
FEES_BY_JOB = {"jobPaid1": "1000", "jobPaid2": "1000", "jobPaid3": "4000", "jobPaid4": "1000",
               "jobPaid5": "1000", "jobLow": "999", "jobZero": None, "jobDark": "unreadable",
               "jobFlaky": "1000", "jobNoMembers": "1000", "jobNoSeed": "1000", "jobAttest": "1000",
               "jobSettled": "1000", "jobRefunded": "1000", "jobNoState": "1000", "jobBadState": "1000"}
# The state each job reads, the shapes the chain writes (`msg_server_open_job.go` writes "open", every later
# transition appends a marker). A job not named here is open. `jobNoState` omits `state` (proto3, empty
# string), `jobBadState` carries a state that is not a string.
STATE_ABSENT = object()
STATE_BY_JOB = {"jobSettled": "open+paid+optimistic", "jobRefunded": "open+expired+refunded",
                "jobNoState": STATE_ABSENT, "jobBadState": 7}
CLIENT_JOBS = Jobs({j: addr_c for j in FEES_BY_JOB})
# The committee every job of the bench was assigned, the shape `/dendra/jobs/v1/assigned_committee` returns.
COMMITTEE = [MINER, VICTIM, "dm1third"]
# Reads of the fee that fail before one succeeds, per job: the transient outage of the REST gateway.
# Reset by `c37_scenarios`, which runs once per relay variant.
FLAKY = {}


def read_job(jid):
    """`/dendra/jobs/v1/job/{jid}` as the chain answers it. `jobZero` carries no `fee`, the form proto3
    gives a zero; `jobDark` is a read that fails, `jobFlaky` one that fails once."""
    fee = FEES_BY_JOB[jid]
    if fee == "unreadable":
        raise OSError("REST gateway unreachable")
    if FLAKY.get(jid, 0) > 0:
        FLAKY[jid] -= 1
        raise OSError("REST gateway unreachable, once")
    fields = {"client": addr_c}
    if fee is not None:
        fields["fee"] = fee
    state = STATE_BY_JOB.get(jid, "open")
    if state is not STATE_ABSENT:
        fields["state"] = state
    return job_doc(jid, **fields)


def read_committee(jid):
    """`/dendra/jobs/v1/assigned_committee/{jid}`. `jobNoMembers` omits `members` (proto3, empty list);
    `jobNoSeed` is the chain refusing while the seed is unrevealed, a read that fails."""
    if jid == "jobNoSeed":
        raise OSError("seed not revealed")
    if jid == "jobNoMembers":
        return b"{}"
    return json.dumps({"members": COMMITTEE}).encode()


REQ_BODY = b'{"client_eph_pk":"aa","nonce":"bb","ct":"the sealed prompt"}'
_HEIGHT = [0]


def client_signed(key, *, sk=sk_c, pub=pub_c, addr=addr_c, miner=MINER, body=REQ_BODY):
    """The headers the client sends: signed with its own wallet key, at a height that only rises."""
    _HEIGHT[0] += 1
    return signed_headers(sk, pub, addr, kind="req", key=key, body=body, miner=miner, height=_HEIGHT[0])


def first(why):
    return why.split(" ")[0] if why else why


def c37_scenarios(module_path=None):
    """Every C37 outcome, as `{name: (status, refusal code)}`, against the shipped relay or a variant."""
    out = {}
    FLAKY.clear()
    FLAKY["jobFlaky"] = 1
    with Relay("enforce", REGISTERED, token="t" * 32, jobs=CLIENT_JOBS, price=PRICE, job_docs=read_job,
               committees=read_committee, module_path=module_path) as r:
        k1, k2, k3 = f"jobPaid1__{MINER}", f"jobPaid2__{MINER}", f"jobPaid3__{MINER}"
        h1 = client_signed(k1)
        st, why = r.post_why("req", k1, REQ_BODY, h1)
        out["signed"] = (st, first(why))
        out["read_back"] = (r.get("req", k1), "")
        st, why = r.post_why("req", k1, REQ_BODY, h1)
        out["exact_replay"] = (st, first(why))
        # One paid job, keys filed under miners the chain never assigned to it: each is refused, so a
        # single payment cannot fill the `req` budget.
        codes = set()
        for i in range(3):
            k = f"jobPaid5__dm1invented{i}"
            st, why = r.post_why("req", k, REQ_BODY, client_signed(k, miner=f"dm1invented{i}"))
            codes.add((st, first(why)))
        out["invented_miner"] = codes.pop() if len(codes) == 1 else (0, repr(sorted(codes)))
        out["invented_miner_stored"] = (r.get("req", "jobPaid5__dm1invented0"), "")
        k = "jobPaid5__dm1third"
        st, why = r.post_why("req", k, REQ_BODY, client_signed(k, miner="dm1third"))
        out["other_member"] = (st, first(why))
        for name, jid in (("committee_absent", "jobNoMembers"), ("committee_unreadable", "jobNoSeed")):
            k = f"{jid}__{MINER}"
            st, why = r.post_why("req", k, REQ_BODY, client_signed(k))
            out[name] = (st, first(why))
        # A fee read that fails once: the refusal records nothing, so the same signed message, sent again
        # unchanged as `client.py::submit_job` does, is accepted.
        kf = f"jobFlaky__{MINER}"
        hf = client_signed(kf)
        st, why = r.post_why("req", kf, REQ_BODY, hf)
        out["flaky_first"] = (st, first(why))
        st, why = r.post_why("req", kf, REQ_BODY, hf)
        out["flaky_retry_same_message"] = (st, first(why))
        out["flaky_stored"] = (r.get("req", kf), "")
        # The attestation gate, once required, refuses before the digest is recorded as well.
        r.mod.ATTEST_REQUIRE = True
        ka = f"jobAttest__{MINER}"
        ha = client_signed(ka)
        st, why = r.post_why("req", ka, REQ_BODY, ha)
        out["not_attested"] = (st, first(why))
        r.mod.ATTEST_REQUIRE = False
        st, why = r.post_why("req", ka, REQ_BODY, ha)
        out["attest_lifted_same_message"] = (st, first(why))
        st, why = r.post_why("req", k2, REQ_BODY, with_token=True)
        out["token"] = (st, first(why))
        st, why = r.post_why("req", k3, REQ_BODY)
        out["neither"] = (st, first(why))
        st, why = r.post_why("req", k3, REQ_BODY, client_signed(k3, sk=sk_x, pub=pub_x, addr=addr_x))
        out["stranger_key"] = (st, first(why))
        st, why = r.post_why("req", k3, REQ_BODY, client_signed(k3, sk=sk_m, pub=pub_m, addr=addr_m))
        out["operator_key"] = (st, first(why))
        st, why = r.post_why("req", k3, REQ_BODY, client_signed(k3, miner=VICTIM))
        out["header_names_another_miner"] = (st, first(why))
        for name, jid in (("underpaid", "jobLow"), ("fee_absent", "jobZero"), ("fee_unreadable", "jobDark")):
            k = f"{jid}__{MINER}"
            st, why = r.post_why("req", k, REQ_BODY, client_signed(k))
            out[name] = (st, first(why))
        k = f"jobLow__{VICTIM}"
        st, why = r.post_why("req", k, REQ_BODY, client_signed(k, miner=VICTIM), with_token=True)
        out["underpaid_with_token"] = (st, first(why))
        # A job that no longer waits for its request: its fee still covers the price, so only the state refuses.
        for name, jid in (("settled", "jobSettled"), ("refunded", "jobRefunded"), ("state_absent", "jobNoState"),
                          ("state_unreadable", "jobBadState")):
            k = f"{jid}__{MINER}"
            st, why = r.post_why("req", k, REQ_BODY, client_signed(k))
            out[name] = (st, first(why))
        out["settled_stored"] = (r.get("req", f"jobSettled__{MINER}"), "")
        k = f"jobSettled__{VICTIM}"
        st, why = r.post_why("req", k, REQ_BODY, client_signed(k, miner=VICTIM), with_token=True)
        out["settled_with_token"] = (st, first(why))
        st, why = r._call_why("POST", "req", REQ_BODY, client_signed(k1))
        out["route_req_one_segment"] = (st, "")
        st, why = r._call_why("POST", f"req/jobPaid4/{MINER}", REQ_BODY, client_signed(k1))
        out["route_req_three_segments"] = (st, "")
        st, why = r._call_why("POST", "admin", REQ_BODY, client_signed(k1))
        out["route_unknown"] = (st, "")
        out["refusals"] = (0, ",".join(sorted(r.mod.SIGN_REFUSALS)))
        s = r.mod.SIGN_STATS
        out["counters"] = (s["attributed"], f"request_refused={s['request_refused']} "
                                            f"unattributed={s['unattributed']}")
    with Relay("enforce", REGISTERED, token="t" * 32, jobs=CLIENT_JOBS, price="", job_docs=read_job,
               committees=read_committee, module_path=module_path) as r:
        k4 = f"jobPaid4__{MINER}"
        st, why = r.post_why("req", k4, REQ_BODY, client_signed(k4))
        out["price_unset_signed"] = (st, first(why))
        st, why = r.post_why("req", k4, REQ_BODY, with_token=True)
        out["price_unset_token"] = (st, first(why))
    # `enforce` on a relay with no token configured: `req` keeps the reading it had as a token route,
    # `_authed()`, which a relay without a token answers True. The other signed kinds refuse there.
    with Relay("enforce", REGISTERED, token="", jobs=CLIENT_JOBS, price=PRICE, job_docs=read_job,
               committees=read_committee, module_path=module_path) as r:
        st, why = r.post_why("req", f"jobPaid4__{MINER}", REQ_BODY)
        out["no_token_configured_unsigned_req"] = (st, first(why))
        st, why = r.post_why("res", f"jobPaid4__{MINER}", REQ_BODY)
        out["no_token_configured_unsigned_res"] = (st, first(why))
    return out


EXPECTED = {
    "signed": (200, ""), "read_back": (200, ""), "token": (200, ""), "neither": (401, "unsigned"),
    "stranger_key": (401, "FOREIGN_CLIENT"), "operator_key": (401, "FOREIGN_CLIENT"),
    "header_names_another_miner": (401, "KEY_MISMATCH"),
    "underpaid": (401, "UNDERPAID"), "fee_absent": (401, "UNDERPAID"), "fee_unreadable": (401, "FEE_UNREADABLE"),
    "underpaid_with_token": (200, ""),
    "route_req_one_segment": (401, ""), "route_req_three_segments": (401, ""), "route_unknown": (401, ""),
    "exact_replay": (401, "REPLAY"),
    "invented_miner": (401, "UNASSIGNED_MINER"), "invented_miner_stored": (404, ""),
    "other_member": (200, ""),
    "committee_absent": (401, "UNASSIGNED_MINER"), "committee_unreadable": (401, "COMMITTEE_UNREADABLE"),
    "flaky_first": (401, "FEE_UNREADABLE"), "flaky_retry_same_message": (200, ""), "flaky_stored": (200, ""),
    "not_attested": (401, "NOT_ATTESTED"), "attest_lifted_same_message": (200, ""),
    "settled": (401, "JOB_NOT_OPEN"), "refunded": (401, "JOB_NOT_OPEN"), "state_absent": (401, "JOB_NOT_OPEN"),
    "state_unreadable": (401, "JOB_STATE_UNREADABLE"), "settled_stored": (404, ""),
    "settled_with_token": (200, ""),
    "refusals": (0, "COMMITTEE_UNREADABLE,FEE_UNREADABLE,FOREIGN_CLIENT,JOB_NOT_OPEN,JOB_STATE_UNREADABLE,"
                    "KEY_MISMATCH,NOT_ATTESTED,REPLAY,UNASSIGNED_MINER,UNDERPAID"),
    "counters": (4, "request_refused=16 unattributed=3"),
    "price_unset_signed": (401, "PRICE_UNSET"), "price_unset_token": (200, ""),
    "no_token_configured_unsigned_req": (200, ""), "no_token_configured_unsigned_res": (401, "unsigned"),
}
SAY = {
    "signed": "the payer's signature alone, no token -> 200",
    "read_back": "...and the sealed request reads back with no secret -> 200",
    "token": "the token alone, unsigned (the gateway's path) -> 200",
    "neither": "neither a signature nor the token -> 401",
    "stranger_key": "a valid signature by a key that paid for nothing -> 401 FOREIGN_CLIENT",
    "operator_key": "a registered operator's key, not the payer -> 401 FOREIGN_CLIENT",
    "header_names_another_miner": "the payer's signature, header naming another miner than the key -> 401 KEY_MISMATCH",
    "underpaid": "the payer signs a job that escrowed below the price -> 401 UNDERPAID",
    "fee_absent": "a job with no fee field (proto3 zero) -> 401 UNDERPAID",
    "fee_unreadable": "a job whose fee cannot be read -> 401 FEE_UNREADABLE",
    "underpaid_with_token": "the same underpaid request with the token -> 200 (the token path is unchanged)",
    "route_req_one_segment": "POST /req with no key, signed -> 401 (an unknown route is strict)",
    "route_req_three_segments": "POST /req/a/b, signed -> 401 (an unknown route is strict)",
    "route_unknown": "POST /admin, signed -> 401 (an unknown route is strict)",
    "exact_replay": "the accepted signed message sent again -> 401 REPLAY",
    "invented_miner": "one paid job, keys under miners outside its committee -> 401 UNASSIGNED_MINER each",
    "invented_miner_stored": "...and nothing is stored under them -> 404",
    "other_member": "the same paid job under another member of its committee -> 200",
    "committee_absent": "a committee with no members (proto3 omits the list) -> 401 UNASSIGNED_MINER",
    "committee_unreadable": "a committee that cannot be read -> 401 COMMITTEE_UNREADABLE",
    "flaky_first": "a fee read that fails once -> 401 FEE_UNREADABLE",
    "flaky_retry_same_message": "...the same signed message sent again unchanged -> 200 (no digest recorded)",
    "flaky_stored": "...and the request is stored -> 200",
    "not_attested": "attestation required, miner not attested -> 401 NOT_ATTESTED",
    "attest_lifted_same_message": "...requirement lifted, the same signed message -> 200 (no digest recorded)",
    "settled": "the payer signs a paid job whose state carries +paid -> 401 JOB_NOT_OPEN",
    "refunded": "the payer signs a job expired and refunded -> 401 JOB_NOT_OPEN",
    "state_absent": "a job with no state field (proto3 empty string) -> 401 JOB_NOT_OPEN",
    "state_unreadable": "a job whose state is not a string -> 401 JOB_STATE_UNREADABLE",
    "settled_stored": "...and nothing is stored under the settled job -> 404",
    "settled_with_token": "the same settled job with the token -> 200 (the token path is unchanged)",
    "refusals": "the refusals are counted under their own codes",
    "counters": "`attributed` counts accepted writes only; refusals on price or committee are `request_refused`",
    "price_unset_signed": "price unset: the payer's signature alone -> 401 PRICE_UNSET",
    "price_unset_token": "price unset: the token alone -> 200 (as before C37)",
    "no_token_configured_unsigned_req": "enforce with no token configured: unsigned req -> 200 (as before C37)",
    "no_token_configured_unsigned_res": "...while an unsigned res is refused there -> 401",
}

print()
print("== (C37) Both halves against the shipped relay ==")
SHIPPED_C37 = c37_scenarios()
for name, want in EXPECTED.items():
    case(SAY[name], SHIPPED_C37.get(name), want)

print()
print("== (C37) The joiner's path, with no secret of the operator ==")
# The relay holds the operator's token; the client never sends it. It reads the queue, deposits its
# signed request, reads it back; the miner's registered operator answers with its own signature; the
# client reads the sealed answer. `token_uses` counts every call that carried the token.
with Relay("enforce", REGISTERED, token="t" * 32, jobs=CLIENT_JOBS, price=PRICE, job_docs=read_job,
           committees=read_committee) as r:
    kj = f"jobPaid3__{VICTIM}"
    case("GET /list -> 200", r._call("GET", "list"), 200)
    case("the client deposits its signed request -> 200", r.post("req", kj, REQ_BODY, client_signed(kj, miner=VICTIM)), 200)
    case("the miner sees it in its slice of the queue",
         kj in json.loads(urllib.request.urlopen(f"http://127.0.0.1:{r.port}/list?suffix=__{VICTIM}",
                                                 timeout=5).read()).get("req", []), True)
    ANS = b'{"nonce":"ee","ct":"the sealed answer"}'
    case("the registered operator deposits the sealed answer -> 200",
         r.post("res", kj, ANS, signed_headers(sk_v, pub_v, addr_v, kind="res", key=kj, body=ANS,
                                               miner=VICTIM, height=900)), 200)
    case("the client reads the sealed answer with no secret", r.body("res", kj), ANS)
    case("...and no call on this path carried the operator's token", r.token_uses, 0)

print()
print("== (C37) Mutants: each half removed from the shipped file, and the bench must see it ==")
# Each mutant rewrites one line of the shipped relay.py. A rewrite that does not apply exactly once
# is itself a failure: a mutant that changed nothing would be reported caught by a bench that saw no change.
case("the shipped relay keeps one replay store, the one the price mutant below bypasses",
     type(relay.ANTIREPLAY).__name__, "Antireplay")
SHIPPED_SRC = open(relay.__spec__.origin, encoding="utf-8").read()
MUTANTS = (
    ("the signature half: `req` left out of the signed kinds",
     'SIGNED_KINDS = ("pub", "res", "reveal", "attest", "req")',
     'SIGNED_KINDS = ("pub", "res", "reveal", "attest")', "signed"),
    ("the token half: no token fallback", "_token_fallback = self._authed()",
     "_token_fallback = False", "token"),
    ("the price: the paid check skipped",
     'antireplay = (PaidBeforeReplay(lambda: self._request_refusal(key), ANTIREPLAY)\n                      if kind == "req" else ANTIREPLAY)',
     "antireplay = ANTIREPLAY", "underpaid"),
    ("the strict default: an unknown route opened", "        return self.POLICY_TOKEN\n\n    def _guard",
     "        return self.POLICY_OPEN\n\n    def _guard", "route_unknown"),
    ("the token path of `req` read like the other signed kinds under enforce",
     'if SIGN_MODE == "enforce" and parts[0] != "req":', 'if SIGN_MODE == "enforce":',
     "no_token_configured_unsigned_req"),
    ("the committee: a key under any miner accepted", "        if mid not in members:\n",
     "        if False:\n", "invented_miner"),
    ("the order: the digest recorded before the price and the committee are checked",
     "        refused = self._refusal()\n        if refused is not None:\n            reason, detail = refused\n"
     "            return relay_antireplay.Verdict(False, None, reason, detail)\n"
     "        return self._antireplay.verify(writing_key, height, digest, head=head)\n",
     "        v = self._antireplay.verify(writing_key, height, digest, head=head)\n"
     "        refused = self._refusal()\n        if v.accepted and refused is not None:\n            reason, detail = refused\n"
     "            return relay_antireplay.Verdict(False, None, reason, detail)\n"
     "        return v\n",
     "flaky_retry_same_message"),
    ("the state: never compared", "        if state != JOB_OPEN_STATE:\n", "        if False:\n", "settled"),
    ("the state: a textual predicate that finds the word", "if state != JOB_OPEN_STATE:",
     "if JOB_OPEN_STATE not in state:", "settled"),
    ("the state: the proto3 zero read as open", '        return ""\n    state = job["state"]',
     '        return "open"\n    state = job["state"]', "state_absent"),
    ("the state: an unreadable state read as open", "    return state if isinstance(state, str) else None",
     "    return state if isinstance(state, str) else JOB_OPEN_STATE", "state_unreadable"),
    ("the state: the unreadable branch removed", "        if state is None:\n            return JOB_STATE_UNREADABLE",
     "        if False:\n            return JOB_STATE_UNREADABLE", "state_unreadable"),
)
_mdir = tempfile.mkdtemp(prefix="dendra-access-mutants-")
for i, (label, old, new, caught_by) in enumerate(MUTANTS):
    case(f"mutant {i + 1} applies to the shipped text exactly once ({label})", SHIPPED_SRC.count(old), 1)
    if SHIPPED_SRC.count(old) != 1:
        continue
    path = os.path.join(_mdir, f"relay_mutant{i + 1}.py")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(SHIPPED_SRC.replace(old, new))
    got = c37_scenarios(path)
    case(f"mutant {i + 1} is caught by `{caught_by}`", got.get(caught_by) != EXPECTED[caught_by], True)

print()
print("== (C37) The reference client seals to the committee the chain's mode calls for, unless --k says otherwise ==")
# The integrator quickstart (section 6) runs `cli.py submit` without --k. A fixed 3 there would seal
# the prompt to two miners an optimistic chain never pays; unset, client.quick reads the mode
# (`_submit_committee_k`). The shipped main() runs with quick replaced by a recorder.
import contextlib  # noqa: E402
import io  # noqa: E402
import cli  # noqa: E402

CLI_NOT_CALLED = "quick not called"


def cli_k(module, argv):
    seen = {}

    def recorder(*args, **kwargs):
        seen["k"] = kwargs["k"]
        return {"error": "stopped by the bench"}
    saved_quick, saved_argv = module.dc.quick, sys.argv
    module.dc.quick, sys.argv = recorder, ["cli.py"] + argv
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            module.main()
    except SystemExit:
        pass
    finally:
        module.dc.quick, sys.argv = saved_quick, saved_argv
    return seen.get("k", CLI_NOT_CALLED)


_doc_argv = ["submit", "Capital of Australia? Answer in one word.", "--client", "mykey", "--fee", "8280"]
case("cli submit without --k -> k left to the chain's mode (None)", cli_k(cli, _doc_argv), None)
case("cli submit --k 3 -> 3, as asked", cli_k(cli, _doc_argv + ["--k", "3"]), 3)
_cli_src = open(cli.__spec__.origin, encoding="utf-8").read()
_cli_old = '    s.add_argument("--k", type=int, default=None)'
case("mutant: the CLI default applies to the shipped text exactly once", _cli_src.count(_cli_old), 1)
if _cli_src.count(_cli_old) == 1:
    _cli_path = os.path.join(_mdir, "cli_mutant.py")
    with open(_cli_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(_cli_src.replace(_cli_old, '    s.add_argument("--k", type=int, default=3)'))
    _spec = importlib.util.spec_from_file_location("cli_mutant", _cli_path)
    _cli_mut = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_cli_mut)
    case("mutant: a fixed default of 3 is caught", cli_k(_cli_mut, _doc_argv) is None, False)

print()
print(f"RELAY_ACCESS_SUMMARY cases={N} ko={KO} untested=0")
sys.exit(1 if KO else 0)
