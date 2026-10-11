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
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
print("== The gateway: `req` is not signed yet, so it keeps the token ==")
case("POST req -> token", policy(["req", J], "POST"), TOKEN)

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

    def __init__(self, mode, registry, token=""):
        os.environ["DENDRA_RELAY_SIGN"] = mode
        os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix=f"dendra-access-{mode}-")
        if token:
            os.environ["DENDRA_RELAY_TOKEN"] = token
        else:
            os.environ.pop("DENDRA_RELAY_TOKEN", None)
        self.token = token
        self.mod = importlib.reload(relay)
        self.mod.REGISTRY = registry
        self.srv = self.mod.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _call(self, method, path, data=None, hdrs=None, with_token=False):
        h = dict(hdrs or {})
        if with_token and self.token:
            h["X-Dendra-Token"] = self.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/{path}", data=data,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.read()
            return e.code

    def post(self, kind, key, body, hdrs=None, with_token=False):
        return self._call("POST", f"{kind}/{key}", body, hdrs, with_token)

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

print()
print(f"RELAY_ACCESS_SUMMARY cases={N} ko={KO} untested=0")
sys.exit(1 if KO else 0)
