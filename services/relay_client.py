"""Minimal HTTP client for the relay (L4), standard library only (urllib). It carries JSON.

Keys are strings: "<jid>__<mid>" for req and res, "<mid>" for pub. Only ciphertext (req, res) or public
keys (pub) are sent, never plaintext.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import os

_TOKEN = os.environ.get("DENDRA_RELAY_TOKEN", "")

# An authentication refusal must be visible, once per route.
# Every function below swallows its exceptions and returns None, False or b"" on purpose: the relay is
# best effort and an outage must not kill the miner. Without a log line, a 401 would look like "no job
# available", and a miner without a token would wait for jobs forever with its GPU idle.
#
# A refusal speaks for its own route only. The relay decides access per route
# (`relay.py::_policy`): reads of ciphertext are open, since the bodies are sealed to the
# on-chain X25519 key, and only writes need a named writer. A miner with a wrong token still reads
# `GET /list` and `GET /req` and is refused only on `POST`, so "no job will be received" would be the
# wrong message; the real cost is that `res` and `reveal` are writes, so a job can be computed and
# never delivered.
#
# The flood bound is keyed on (method, route). A single global flag would let the first refusal,
# usually the cheap startup `POST pub`, mute a later refusal of `GET /list`, the one call that tells a
# miner a job is waiting (`miner.py::main` opens its loop with `relay.listing`).
_AUTH_WARNED = set()

# What depends on each route, so the consequence follows from what was refused. A route missing from
# this table gets no consequence at all (see below): an unexpected route must not inherit another
# route's cost, just as `_policy` gives an unknown route the strictest policy.
_COST = {
    ("GET", "list"): "this miner will not see the jobs waiting for it: `list` is the work queue and "
                     "nothing else carries that signal, so the daemon prints 'waiting for jobs' "
                     "while its GPU idles",
    ("GET", "req"): "this miner sees the job key but cannot pull the request body, so the job is "
                    "skipped as if it did not exist",
    ("POST", "res"): "a job can be computed and not delivered: the sealed answer stays on this "
                     "machine and the client is never served",
    ("POST", "reveal"): "the primary's obligation cannot be filed: a sampled job then has no artifact "
                        "to judge, so the client's fee stays held and this miner is neither paid nor "
                        "slashed",
    # These two say "if the relay holds no earlier copy" on purpose. A miner deposits both again at
    # every start with the same bytes (the `pub` body is constant and the attestation deterministic),
    # and the relay's anti-replay guard refuses the repeat with 401 and the reason `REPLAY` (digest already
    # accepted) while it still serves the earlier copy. A refused re-deposit can therefore cost
    # nothing, and the message must not assert a missing copy this client never checked.
    ("POST", "pub"): "this deposit did not land -- if the relay holds no earlier copy, the "
                     "client-side cross-check against it is unavailable; the key anchored on chain "
                     "is what peers use, so mining is not blocked",
    ("POST", "attest"): "this deposit did not land -- if the relay holds no earlier copy, this miner "
                        "stays out of its attested set, and where the gate is armed "
                        "(DENDRA_ATTEST_REQUIRE=1) it is assigned no confidential job; ordinary jobs "
                        "are unaffected",
}


def _relay_why(exc):
    """What the relay itself said, or "". Never raises, and the text is only ever quoted.

    Without it the message guesses: "DENDRA_RELAY_TOKEN is incorrect" is inferred from the token being
    set on this side, not read from anything. The relay also refuses writes for reasons unrelated to the
    token: a deposit whose digest it already accepted comes back with the reason `REPLAY` (digest already
    accepted), over a signature that verified and was attributed to this operator. A 401 that carries
    a reason is the one case where nothing has to be guessed.

    The text comes from a remote service, so it is bounded, flattened to one line and quoted: it is
    reported as what the relay said, never adopted as this client's verdict.
    """
    try:
        raw = exc.read(512)
    except Exception:                                    # noqa: BLE001 -- not a body, not a crash
        return ""
    try:
        said = json.loads(raw).get("why") or ""
    except Exception:                                    # noqa: BLE001
        return ""
    return " ".join(str(said).split())[:160]


def _note_auth_failure(exc, method, route):
    """Name the route that was refused, and only what that route carries.

    `method` and `route` are required: a default would let a call site ship without naming itself and
    print an anonymous refusal. There are four call sites, all in this file, and
    `test_relay_auth_message.py` drives each of them through a real refusal.
    """
    code = getattr(exc, "code", None)
    if code not in (401, 403):
        return
    key = (method, route)
    if key in _AUTH_WARNED:
        return
    _AUTH_WARNED.add(key)
    # The relay's own reason comes first, and the two are never merged into one sentence. When the
    # relay states a reason, the token line is only an observation about this side ("set" or "not
    # set"), since a token diagnosis can be wrong about what refused (a replay answers 401 and has
    # nothing to do with the token). Only when the relay says nothing does the message fall back on
    # the guess.
    said = _relay_why(exc)
    if said:
        cause = (f'. The relay\'s own reason: "{said}" -- read that before blaming the token '
                 f'(local state: DENDRA_RELAY_TOKEN is {"set" if _TOKEN else "not set"})')
    else:
        cause = (" (" + ("DENDRA_RELAY_TOKEN is unset" if not _TOKEN
                         else "DENDRA_RELAY_TOKEN is incorrect") + ")")
    cost = _COST.get(key)
    # No default consequence: an unlisted route gets its refusal named and nothing claimed about it.
    # A default is how a refused pubkey deposit would end up announcing a dead work queue.
    tail = f" -> {cost}." if cost else " -> what this costs is not established here."
    print(f"[relay] relay refused {method} {route} with {code}{cause}.{tail} "
          f"This refusal concerns {method} {route} and nothing else: the relay decides access per "
          f"route (relay.py::_policy), reads are open on a relay running that policy, and "
          f"a signature attributable to this miner's operator authorises the writes without any "
          f"shared token. Obtain the token from the relay operator, or fix signing, and restart.",
          flush=True)


def _hdrs(extra=None):
    h = dict(extra or {})
    if _TOKEN:
        h["X-Dendra-Token"] = _TOKEN   # shared relay authentication
    return h


# -- The kit version, in every deposit body (ADR-058, decision 5) -------------------------------------
# Read once, here, where every deposit passes: the relay refuses, from a published deadline, a signed deposit
# whose body declares a kit below the floor or declares none, and a kind whose call site forgot the field
# would be refused from that day. In the body and not in a header, because the signature covers the body
# (`relay_canon`), so nobody on the path can make the relay refuse an honest miner by rewriting a header.
# None when the kit's file cannot be read: the body then carries no field, and the daemon says so at start
# (miner.py reads the same file); a 0 written here would describe a kit older than every release.
# Every kind passes here, the gateway's `req` included: that body carries the field too, and the relay does
# not read it there (modea.relay_write.verify_write holds the operator's kinds to the floor, no other).
try:
    from modea import kit_version as _kit
    _KIT_VERSION = _kit.read_kit_version()
except Exception:  # noqa: BLE001 -- a module that cannot be imported leaves the body without the field
    _kit, _KIT_VERSION = None, None


def declaring(obj):
    """`obj` as this module posts it: with `kit_version` added when it is an object without one, otherwise as
    it is. Public, so a caller that compares the bytes it is about to send with bytes it read (the miner's
    self-test, check C6) compares what really leaves."""
    return _kit.with_kit_version(obj, _KIT_VERSION) if _kit is not None else obj


_declaring = declaring


def _url(base, kind, key):
    return f"{base.rstrip('/')}/{kind}/{key}"


# -- Signing, wired in one place ----------------------------------------------------------------
# Six call sites in four files deposit to the relay. Signing lives here, where every deposit passes,
# rather than at each call site, where one could be forgotten and fail as a refused write once
# enforcement is armed.
#
# Configured by environment, and off unless configured: a miner without a key keeps working as before.
# `DENDRA_SIGN_KEY` names the keyring entry; the rest is optional.
_SIGN_KEY = os.environ.get("DENDRA_SIGN_KEY", "").strip()
_SIGN_KEYRING_DIR = os.environ.get("DENDRA_KEYRING_DIR", "").strip() or None
_SIGN_NODE = os.environ.get("DENDRA_NODE", "").strip() or None
_SIGN_CACHE: dict = {}


def set_sign_key(name: str) -> None:
    """Re-point the signing identity after import, and drop what was cached about the old one.

    The name changes after import: `DENDRA_SIGN_KEY` defaults to `MINER_ID`, this module reads it once at
    import, and the daemon then renames the keyring entry to the derived `dm1...` identity. From then on the
    old name designates nothing, `address_from_key` fails and every deposit goes out unsigned, which goes
    unnoticed under `observe` and gets every write refused once `enforce` is armed.

    Exporting the variable again would not help, since the module already read it. The cache is cleared too,
    or the address resolved for the old name would keep being used."""
    global _SIGN_KEY
    _SIGN_KEY = (name or "").strip()
    _SIGN_CACHE.clear()
# The state of signing, in three values, and each change is logged once. "" means signing works (or was never
# tried); "absent" means the chain holds no account for the signing address yet (a first start, before the
# faucet's credit; it ends by itself); "failed" is anything else. A single "warned" flag would say the first
# failure and never say that signing came back.
_SIGN_STATE = ""


def _signature(kind, key, data, miner_id, height):
    """Headers for this deposit, or {} when signing is not configured. Never raises.

    A signing failure must not stop a miner that was working a second ago: the deposit goes out unsigned
    and is refused only if the relay is armed, a visible 401 rather than a crash. It is logged once per change
    of state, so that a fallback to unsigned is never silent, and neither is the recovery.

    An account that is not on chain yet is not a failure, and its line must not read like one: the chain
    answers "account ... not found: key not found" for an address no credit has reached, and the words "key
    not found" would make deploy/join.sh::wait_healthy declare a healthy first start unhealthy.
    """
    global _SIGN_STATE
    if not _SIGN_KEY:
        return {}
    try:
        from modea import relay_signature as _rs
        if "address" not in _SIGN_CACHE:
            _SIGN_CACHE["address"] = _rs.address_from_key(_SIGN_KEY, keyring_dir=_SIGN_KEYRING_DIR)
        address = _SIGN_CACHE["address"]
        # Read once, not per deposit. The relay rebuilds the document from the sequence carried in the
        # header, so writer and verifier agree whatever the chain has moved on to: the pair must be
        # consistent, not current, and re-reading it would cost a chain round trip per deposit.
        #
        # The signature still proves what it must: the digest of this exact write sits in the signed memo,
        # so the key holder signed this deposit and no other. Account number and sequence are padding in
        # that document, and the dedicated chain-id keeps it from ever being broadcast.
        if "account" not in _SIGN_CACHE:
            _SIGN_CACHE["account"] = _rs.account_number_and_sequence(address, node=_SIGN_NODE)
        an, seq = _SIGN_CACHE["account"]
        signed = _rs.headers(kind, key, data, miner_id or _SIGN_KEY, height,
                             key_name=_SIGN_KEY, address=address, account_number=an, sequence=seq,
                             keyring_dir=_SIGN_KEYRING_DIR)
    except Exception as e:  # noqa: BLE001
        try:
            from modea.relay_signature import AccountNotFound as _absent_cls
        except Exception:  # noqa: BLE001 -- a module that cannot be imported: the failure is a failure
            _absent_cls = ()
        absent = bool(_absent_cls) and isinstance(e, _absent_cls)
        state = "absent" if absent else "failed"
        if _SIGN_STATE != state:
            _SIGN_STATE = state
            if absent:
                # The chain's own wording is not quoted here (see the docstring): it names a key, and this
                # is about an account that does not exist yet.
                print(f"[relay] deposit sent unsigned for now: {e}. Nothing is wrong with this miner's keys; "
                      f"signing resumes by itself once the account exists, and a line says so.", flush=True)
            else:
                print(f"[relay] cannot sign deposits ({type(e).__name__}: {e}) -> sending them unsigned. "
                      f"They will be refused as soon as the relay requires signatures.", flush=True)
        return {}
    if _SIGN_STATE:
        print(f"[relay] deposits are signed again (signing key {_SIGN_KEY}): the earlier "
              f"{'missing account' if _SIGN_STATE == 'absent' else 'signing failure'} is over.", flush=True)
        _SIGN_STATE = ""
    return signed


def put_status(base, kind, key, obj, *, miner_id=None, height=0) -> str:
    """Deposit, with the one distinction a boolean cannot carry: "ok" | "exists" | "refused".

    A 409 does not mean the deposit failed. On the write-once kinds it means the key already holds a sealed
    artifact whose bytes differ from the ones offered, which is the normal answer on a retry, since every
    seal draws a fresh ephemeral key. Folded into False, a stored reveal would be counted as missing and a
    completed job as failed. The caller decides what "exists" means; this function keeps the information.
    """
    # The body is serialised once, then signed and sent: the signature covers a digest of these exact
    # bytes, and encoding it again in between could produce a different body and an unexplained refusal.
    data = json.dumps(_declaring(obj)).encode()
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

    Kept as it is on purpose: every existing caller reads it this way, and widening it would change what
    "it worked" means for all of them at once. A caller that needs to tell a 409 from a real refusal asks
    for it with `put_status`.
    """
    return put_status(base, kind, key, obj, miner_id=miner_id, height=height) == "ok"


def put_answer(base, kind, key, obj, *, miner_id=None, height=0, require_signature=False) -> tuple:
    """(HTTP status, the relay's own stated reason, signed?) -- status None when nothing answered.

    For the one caller that has to tell refusals apart rather than count them: the miner's self-test
    (`miner_selftest.py`, check C6). A replay refusal (`REPLAY`, the digest was already accepted) comes
    after the relay verified the signature and attributed it to this miner's operator, so it proves the
    write path; any other 401 does not. `put_status` folds both into "refused", which is right for the
    daemon and blind for a test of the path. Signed exactly like every other deposit (`_signature`), on
    the bytes sent: the body is serialised once. `signed` says whether a signature went out at all: a
    relay that still accepts unsigned writes would otherwise make an unsigned deposit look like a proof.

    require_signature: when no signature can be made, nothing is sent and (None, why, False) comes back.
    The daemon's deposits fall back to unsigned on purpose (see `_signature`); a check that exists to
    test the signed path must not leave an unsigned write behind it."""
    data = json.dumps(_declaring(obj)).encode()
    sig = _signature(kind, key, data, miner_id, height)
    if require_signature and not sig:
        return None, "no signature could be made: nothing was sent", False
    req = urllib.request.Request(_url(base, kind, key), data=data, method="POST",
                                 headers=_hdrs({"Content-Type": "application/json", **sig}))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
            return r.status, "", bool(sig)
    except urllib.error.HTTPError as e:
        return e.code, _relay_why(e), bool(sig)
    except Exception as e:  # noqa: BLE001 -- nothing answered: the caller reports it, it never guesses
        return None, f"{type(e).__name__}: {e}", bool(sig)


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


def listing(base, suffix=None):
    """The work queue, {kind: [keys...]}, or {} when it could not be read.

    `suffix` (a miner passes "__<its id>"): only the keys ending with it, asked of the relay as
    `/list?suffix=` and revalidated with its ETag, so an unchanged slice costs a 304 and no body. A
    relay that does not serve the filter is read in full and filtered here (the same keys, more bytes),
    and that is logged once (see the filtered work queue below). Without `suffix`, the full queue."""
    if suffix:
        return _listing_slice(base, suffix)
    return _listing_full(base)


def _listing_full(base):
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/list", headers=_hdrs())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        # An empty dict is not "nothing to do", so the refusal is named, as the other three functions do.
        # `GET /list` is an open route (see relay.py), but an older relay may still gate it, and a
        # 401 answered by silence would look like an idle network. This is the one route whose refusal
        # does mean "no job will be received".
        _note_auth_failure(e, "GET", "list")
        return {}


# -- The filtered work queue ----------------------------------------------------------------------
# A miner reads the queue every few seconds and keeps only the keys ending with its own id, so a full read
# carries every key of the network to every miner, at a cost that grows with the relay's store.
# `/list?suffix=` (relay.py, the filtered work queue) answers the slice alone, and its ETag turns an
# unchanged slice into a 304 with no body.
#
# A relay that does not serve the filter must cost bytes, never jobs. An older relay answers the filtered
# request 404 (no token configured) or 401 (token configured: an unknown route takes the strictest policy),
# and that 401 says nothing about the queue, which is open. So the full `/list` is read and filtered here,
# the switch is logged once, and the filtered request is asked again every LIST_RETRY_S, so that a relay
# updated under a running miner is picked up without a restart.
# Only an answer that declines the route falls back for LIST_RETRY_S. A relay that does not answer, or
# rate-limits, gets nothing more this round: a second request would double the load at the worst moment,
# and the next round retries anyway. A relay that answers 5xx or an unreadable body gets the full read once,
# without being marked, since its `/list` may still work.
#
# The slice is filtered here again whatever the relay answered: a 200 that does not echo the suffix
# (`X-Dendra-List-Suffix`) did not apply it (a proxy that drops the query string would serve the full
# queue), and the jobs a miner sees are decided by the same `endswith` as always.
LIST_RETRY_S = 600.0
LIST_SUFFIX_HEADER = "X-Dendra-List-Suffix"      # relay.py::LIST_SUFFIX_HEADER, same bytes
_SLICE = {"key": None, "etag": "", "doc": None, "fallback": "", "retry_at": 0.0}


def _only(doc, suffix):
    """`doc` reduced to the keys ending with `suffix`, or None when `doc` is not a work queue.

    The relay names every kind even when empty, so a queue that was read is a non-empty dict of lists.
    Anything else means no queue was read: None, never {}."""
    if not isinstance(doc, dict) or not doc:
        return None
    out = {}
    for kind, keys in doc.items():
        if not isinstance(keys, list):
            return None
        out[kind] = [k for k in keys if isinstance(k, str) and k.endswith(suffix)]
    return out


def _ask_slice(base, suffix):
    """(state, queue or None, why) for one filtered request. States:
      ok          the relay applied the filter (200 echoing it, or 304 on the slice held);
      unfiltered  a 200 without the echo: the queue in it is filtered here, the route is not served;
      declined    the relay answered that it does not serve this route (400/401/403/404/405);
      broken      it answered 5xx, or a body that is not a work queue: read /list once, mark nothing;
      silent      no answer, a 429, or a 304 with nothing held: nothing more this round."""
    url = f"{_SLICE['key'][0]}/list?suffix={urllib.parse.quote(suffix, safe='')}"
    h = _hdrs()
    if _SLICE["etag"] and _SLICE["doc"] is not None:
        h["If-None-Match"] = _SLICE["etag"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=10) as r:
            raw, echoed, etag = r.read(), r.headers.get(LIST_SUFFIX_HEADER), r.headers.get("ETag") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            if _SLICE["doc"] is not None:
                return "ok", {k: list(v) for k, v in _SLICE["doc"].items()}, ""
            return "silent", None, "HTTP 304 with no slice held"
        if e.code in (400, 401, 403, 404, 405):
            return "declined", None, f"HTTP {e.code}"
        if e.code >= 500:
            return "broken", None, f"HTTP {e.code}"
        return "silent", None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 -- no answer at all: the round is lost, the miner is not
        return "silent", None, type(e).__name__
    try:
        doc = json.loads(raw)
    except ValueError:
        return "broken", None, "a body that is not JSON"
    only = _only(doc, suffix)
    if only is None:
        return "broken", None, "a body that is not a work queue"
    if echoed != suffix:
        _SLICE.update(etag="", doc=None)
        return "unfiltered", only, "a 200 that did not echo the filter"
    _SLICE.update(etag=etag, doc={k: list(v) for k, v in only.items()})
    return "ok", only, ""


def _listing_slice(base, suffix):
    key = (base.rstrip("/"), suffix)
    if _SLICE["key"] != key:
        _SLICE.update(key=key, etag="", doc=None, fallback="", retry_at=0.0)
    now = time.monotonic()
    if now >= _SLICE["retry_at"]:
        state, doc, why = _ask_slice(base, suffix)
        if state == "ok":
            if _SLICE["fallback"]:
                print(f"[relay] the relay serves the filtered work queue again ({key[0]}/list?suffix=): "
                      f"back to reading this miner's keys only.", flush=True)
                _SLICE["fallback"] = ""
            return doc
        if state == "silent":
            return {}
        if state in ("declined", "unfiltered"):
            _SLICE["retry_at"] = now + LIST_RETRY_S
            if _SLICE["fallback"] != why:
                _SLICE["fallback"] = why
                print(f"[relay] the relay did not serve the filtered work queue "
                      f"(GET {key[0]}/list?suffix=, {why}) -> this miner reads the full queue and keeps "
                      f"its own keys: the same jobs, but every key of the network crosses the wire on "
                      f"every round. A relay that serves /list?suffix= restores the filtered read; it "
                      f"is asked again every {LIST_RETRY_S:g} s.", flush=True)
            if state == "unfiltered":
                return doc
    only = _only(_listing_full(base), suffix)
    return only if only is not None else {}
