#!/usr/bin/env python3
"""Bench for the relay's write policy — who may write, and what may be destroyed.

WHAT THIS BENCH FORBIDS, IN TWO HALVES THAT MUST BOTH HOLD
  · what must OPEN — reading ciphertext with NO secret at all. A service-wide token requirement
    answers 401 on every route to an operator who has just had its hardware and the genesis digest
    verified by `join.sh`, because `DENDRA_RELAY_TOKEN` is a SHARED secret that `network-info.txt`
    does not carry and no self-service procedure issues. Do not rebuild that wall.
  · what must STAY CLOSED — writing. A deposit key names a miner (`res/<jid>__<mid>`), and the sealed
    artifact filed under it is the only thing an audit can conclude on.

⚠️ WHY THIS BENCH IS NO LONGER A TABLE. Its first version scored 23/0 while a stranger could destroy
any miner's sealed reveal. It could not have caught it: it re-declared the server's policy table
beside the server, then asked its questions of the copy. Two defects in one shape —
  (1) a copy CONFIRMS itself. `_politique` is now IMPORTED and called; there is no second table.
  (2) a table could not express the property that mattered. "signed" was a boolean in the model, so
      "signed BY WHOM" had no way to be asked, and the answer the server gave — anyone — was outside
      the vocabulary of the test. Part 2 therefore runs a REAL relay over a loopback socket with REAL
      secp256k1 signatures: the only way to ask whether a signature names anybody.

Usage: python3 test_relay_access.py
"""
import base64
import hashlib
import importlib
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


# ══ PART 1 — THE POLICY TABLE, READ FROM THE SERVER ITSELF ═══════════════════════════════════════
os.environ["DENDRA_RELAY_STORE"] = tempfile.mkdtemp(prefix="dendra-access-")
os.environ.pop("DENDRA_RELAY_TOKEN", None)
os.environ.pop("DENDRA_RELAY_REGISTRY", None)
os.environ["DENDRA_RELAY_SIGN"] = "off"
relay = importlib.import_module("relay")
H = relay.Handler
OPEN, TOKEN, SIGNED_OR_TOKEN = H.OUVERT, H.JETON, H.SIGNE_OU_JETON


def policy(parts, method):
    """The SERVER's own function, not a copy of it. `_politique` reads only class attributes, so the
    class itself serves as `self` — no socket, no request, nothing to imitate."""
    return H._politique(H, parts, method)


J = "job1785643115494__dm1teqlpyx9sctv4d954eejvz"

print("== WHAT MUST OPEN: a joiner pulls its own work with no secret at all ==")
case("GET req/<jid>__<mid>    -> open", policy(["req", J], "GET"), OPEN)
case("GET res/<jid>__<mid>    -> open", policy(["res", J], "GET"), OPEN)
case("GET reveal/<jid>__<mid> -> open", policy(["reveal", J], "GET"), OPEN)
case("GET pub/<mid>           -> open", policy(["pub", "dm1teq"], "GET"), OPEN)

print()
print("== WRITING MINER-OWNED CONTENT: a signature, or the token as the transition path ==")
for k in ("pub", "res", "reveal", "attest"):
    case(f"POST {k:<6} -> signed-or-token", policy([k, J], "POST"), SIGNED_OR_TOKEN)

print()
print("== THE GATEWAY: `req` is not signed yet, so it keeps the token ==")
case("POST req -> token", policy(["req", J], "POST"), TOKEN)

print()
print("== NETWORK MAPPING STAYS CLOSED (what the token really protected) ==")
case("GET list  -> token", policy(["list"], "GET"), TOKEN)
case("GET stats -> token", policy(["stats"], "GET"), TOKEN)

print()
print("== AN UNKNOWN ROUTE TAKES THE STRICTEST POLICY ==")
case("GET  /admin                  -> token", policy(["admin"], "GET"), TOKEN)
case("POST /pub with no identifier -> token", policy(["pub"], "POST"), TOKEN)
case("GET  /req/a/b (3 segments)   -> token", policy(["req", "a", "b"], "GET"), TOKEN)

# ══ PART 2 — A REAL RELAY, REAL SIGNATURES, AND THE QUESTION THE TABLE COULD NOT ASK ═════════════
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
except ImportError as exc:                       # noqa: BLE001
    print(f"\nBANC_RELAY_ACCESS_RESUME cas={N} ko={N} "
          f"(cryptography missing: {exc} - the half that matters CANNOT run, so this bench is RED. "
          f"A bench that reports green for what it did not run is worse than no bench.)")
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
    reconstruction to a REAL binary signature."""
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
    """No read has ever succeeded — NOTHING can be attributed to anyone. It is a STATE, not an error,
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
sk_m, pub_m, addr_m = identity()          # another REGISTERED miner, with its own operator address
REGISTERED = Registry({VICTIM: addr_v, MINER: addr_m})

print()
print("== GREEN: the joiner's path and the legitimate deposit must keep working ==")
with Relay("enforce", REGISTERED) as r:
    case("the registered operator deposits its own reveal -> 200",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=100)), 200)
    case("a joiner READS ciphertext with no secret at all -> 200", r.get("reveal", KEY), 200)
    case("...and reads exactly the bytes deposited", r.body("reveal", KEY), SEALED)

    print()
    print("== (a) A STRANGER SELF-SIGNS OVER SOMEONE ELSE'S REVEAL ==")
    # The whole finding in one request: a valid signature made with a key the registry knows nothing
    # about, over the victim's deposit key. Before attribution was wired this answered 200 and the
    # bytes above were gone for good.
    case("a foreign key signing for dm1victim -> REFUSED (401)",
         r.post("reveal", KEY, FORGED,
                signed_headers(sk_x, pub_x, addr_x, kind="reveal", key=KEY, body=FORGED,
                               miner=VICTIM, height=101)), 401)
    case("...and the sealed reveal is UNTOUCHED", r.body("reveal", KEY), SEALED)
    # The same attack from a miner the registry DOES know: it is correctly attributed to ITSELF, so
    # attribution alone says yes. What refuses is the deposit key naming another miner.
    K2 = f"job42__{VICTIM}"
    case("a REGISTERED miner depositing under another miner's key -> REFUSED (401)",
         r.post("reveal", K2, FORGED,
                signed_headers(sk_m, pub_m, addr_m, kind="reveal", key=K2, body=FORGED,
                               miner=MINER, height=101)), 401)
    case("...and the sealed reveal is STILL untouched", r.body("reveal", KEY), SEALED)

    print()
    print("== (b) REPLAY OF A VALID SIGNED DEPOSIT ==")
    K3, BODY3 = f"job43__{VICTIM}", b'{"nonce":"cc","ct":"second job"}'
    h3 = signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=K3, body=BODY3,
                        miner=VICTIM, height=102)
    case("the honest deposit passes once -> 200", r.post("reveal", K3, BODY3, h3), 200)
    # Same bytes, same signature, replayed. The digest has been seen, so the replay guard refuses it
    # BEFORE the store is touched — which is also what stops a captured deposit being re-injected.
    case("the exact same signed deposit, replayed -> REFUSED (401)",
         r.post("reveal", K3, BODY3, h3), 401)

    print()
    print("== (c) A SECOND, DIFFERENT CONTENT ON AN EXISTING SEALED ARTIFACT ==")
    # Signed by the LEGITIMATE operator, attributed, replay-clean: every authorisation question
    # answers yes, and it is still refused. Write-once is not an authorisation question.
    OTHER = b'{"nonce":"dd","ct":"a second answer for the same job"}'
    case("the registered operator replaces its OWN reveal -> 409",
         r.post("reveal", KEY, OTHER,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=OTHER,
                               miner=VICTIM, height=103)), 409)
    case("...and the first artifact is still the one stored", r.body("reveal", KEY), SEALED)
    # A retry that re-sends what is already stored destroys nothing and must not strand the worker.
    case("re-sending the IDENTICAL bytes -> 200 (nothing to destroy)",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=104)), 200)

print()
print("== (d) THE REGISTRY IS MUTE: a signature that names nobody authorises nobody ==")
with Relay("enforce", MuteRegistry()) as r:
    case("a PERFECTLY VALID signature, registry never read -> REFUSED (401)",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=100)), 401)

print()
print("== THE TOKEN REMAINS THE FALLBACK, AND WHEN THE REGISTRY IS MUTE IT IS THE ONLY ONE ==")
with Relay("enforce", MuteRegistry(), token="t" * 32) as r:
    case("unattributable signature + token -> 200",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=100), with_token=True), 200)
    K4 = f"job99__{VICTIM}"
    case("unattributable signature WITHOUT the token -> 401",
         r.post("reveal", K4, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=K4, body=SEALED,
                               miner=VICTIM, height=100)), 401)
    case("a joiner still READS with no secret -> 200", r.get("reveal", KEY), 200)

print()
print("== THE GATEWAY IS NOT CUT OFF BY `enforce` (the reason it could never be armed) ==")
with Relay("enforce", REGISTERED, token="t" * 32) as r:
    RJ = f"job77__{MINER}"
    case("POST req UNSIGNED with the token -> 200",
         r.post("req", RJ, b'{"ct":"x"}', with_token=True), 200)
    case("POST req without the token -> 401", r.post("req", RJ, b'{"ct":"x"}'), 401)
    case("a second, DIFFERENT sealed request on the same key -> 409",
         r.post("req", RJ, b'{"ct":"y"}', with_token=True), 409)

print()
print("== IDENTITY KINDS STAY REPLACEABLE (a rotated key must not be frozen) ==")
with Relay("enforce", REGISTERED, token="t" * 32) as r:
    case("POST pub once -> 200", r.post("pub", MINER, b'{"pub":"01"}', with_token=True), 200)
    case("POST pub again with a NEW key -> 200",
         r.post("pub", MINER, b'{"pub":"02"}', with_token=True), 200)
    case("POST attest again after an upgrade -> 200",
         r.post("attest", MINER, b'{"measured_hash":"02"}', with_token=True), 200)

print()
print("== `off` AND `observe` STILL LET AN UNSIGNED DEPOSIT THROUGH (nothing is armed by surprise) ==")
for mode in ("off", "observe"):
    with Relay(mode, MuteRegistry()) as r:
        case(f"{mode}: unsigned deposit, no token, no registry -> 200",
             r.post("res", f"jobA__{MINER}", b'{"ct":"z"}'), 200)

print()
print("== THE CONFIGURATION ACTUALLY DEPLOYED: `observe` + a token ==")
# The finding was NOT confined to `enforce`. Under `observe` a self-signed deposit used to be read as
# "signed", which skipped the token entirely — so the relay running in production was open to the same
# stranger. A mode is not a defence; it only decides whether an UNSIGNED write is refused.
with Relay("observe", REGISTERED, token="t" * 32) as r:
    case("a stranger's self-signed deposit, no token -> 401",
         r.post("reveal", KEY, FORGED,
                signed_headers(sk_x, pub_x, addr_x, kind="reveal", key=KEY, body=FORGED,
                               miner=VICTIM, height=200)), 401)
    case("the registered operator, no token -> 200",
         r.post("reveal", KEY, SEALED,
                signed_headers(sk_v, pub_v, addr_v, kind="reveal", key=KEY, body=SEALED,
                               miner=VICTIM, height=200)), 200)
    case("an older client that cannot sign, WITH the token -> 200",
         r.post("res", f"jobB__{MINER}", b'{"ct":"z"}', with_token=True), 200)
    case("...and the same client WITHOUT the token -> 401",
         r.post("res", f"jobC__{MINER}", b'{"ct":"z"}'), 401)

print()
print(f"BANC_RELAY_ACCESS_RESUME cas={N} ko={KO}")
sys.exit(1 if KO else 0)
