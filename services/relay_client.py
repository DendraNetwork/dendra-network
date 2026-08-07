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

# AN AUTHENTICATION REFUSAL MUST BE VISIBLE — ONCE.
# Every function below swallows its exceptions and returns None/False/b"". That is deliberate: the
# relay is best effort and an outage must not kill the miner. But it also made a 401 INDISTINGUISHABLE
# from "no job available". A miner with no token would query a public relay running `auth=ON`, receive
# 401 on every call, and display "waiting for jobs" forever: GPU running, on-chain registration valid,
# zero work, no message. The first refusal is therefore logged — once only, so the logs are not
# flooded — instead of being silently discarded.
_AUTH_WARNED = False


def _note_auth_failure(exc):
    global _AUTH_WARNED
    if _AUTH_WARNED:
        return
    code = getattr(exc, "code", None)
    if code in (401, 403):
        _AUTH_WARNED = True
        why = "DENDRA_RELAY_TOKEN is unset" if not _TOKEN else "DENDRA_RELAY_TOKEN is incorrect"
        print(f"[relay] relay refused the request with {code} ({why}) -> no job will be received. "
              f"Obtain the token from the relay operator and restart.", flush=True)


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


def put(base, kind, key, obj, *, miner_id=None, height=0) -> bool:
    # The body is serialised ONCE and both signed and sent: the signature covers a digest of these
    # exact bytes, so re-encoding between the two would produce a different body and a refusal that
    # no log line explains.
    data = json.dumps(obj).encode()
    req = urllib.request.Request(_url(base, kind, key), data=data, method="POST",
                                 headers=_hdrs({"Content-Type": "application/json",
                                                **_signature(kind, key, data, miner_id, height)}))
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        _note_auth_failure(e)
        return False


def get(base, kind, key, retries=1):
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(_url(base, kind, key), headers=_hdrs())
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception as e:
            _note_auth_failure(e)
            time.sleep(0.3)
    return None


def get_blob(base, kind, key) -> bytes:
    """Raw stored bytes (used for confidentiality checks: this is all the relay can see)."""
    try:
        req = urllib.request.Request(_url(base, kind, key), headers=_hdrs())
        return urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        _note_auth_failure(e)
        return b""


def listing(base):
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/list", headers=_hdrs())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        # AN EMPTY DICT IS NOT "NOTHING TO DO". `/list` is the one route that stays behind the token,
        # so a miner without one takes a 401 here on every round. Returning {} silently made that
        # read as "no jobs" — the exact silence the rest of this module exists to prevent. Naming the
        # refusal is what the other three functions already do; this one must too.
        _note_auth_failure(e)
        return {}
