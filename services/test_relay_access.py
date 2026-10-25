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

    def __init__(self, mode, registry, token="", kit_min="", kit_deadline="", registry_url=""):
        os.environ["DENDRA_RELAY_SIGN"] = mode
        os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix=f"dendra-access-{mode}-")
        if token:
            os.environ["DENDRA_RELAY_TOKEN"] = token
        else:
            os.environ.pop("DENDRA_RELAY_TOKEN", None)
        # The kit floor is read from the environment at import, like the deployed relay reads it; the two
        # variables are set here or absent, never left over from an earlier instance.
        for name, value in (("DENDRA_KIT_MIN", kit_min), ("DENDRA_KIT_DEADLINE", kit_deadline)):
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        self.token = token
        self.mod = importlib.reload(relay)
        self.mod.REGISTRY = registry
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
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/{path}", data=data,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
                return r.status, ""
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                why = json.loads(raw).get("why", "")
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
    case("reads stay open under the armed floor: GET pub/<mid> -> 200", r.get("pub", VICTIM), 200)
    case("...GET /list -> 200", r._call("GET", "list"), 200)
    case("the refusals are counted by code", r.mod.SIGN_REFUSALS.get("STALE_KIT"), 2)
    case("...and under their own counter, not as unattributed writes",
         (r.mod.SIGN_STATS["stale_kit"], r.mod.SIGN_STATS["unattributed"]), (2, 0))

print()
print(f"RELAY_ACCESS_SUMMARY cases={N} ko={KO} untested=0")
sys.exit(1 if KO else 0)
