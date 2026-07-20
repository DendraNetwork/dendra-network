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


def _hdrs(extra=None):
    h = dict(extra or {})
    if _TOKEN:
        h["X-Dendra-Token"] = _TOKEN   # shared relay authentication
    return h


def _url(base, kind, key):
    return f"{base.rstrip('/')}/{kind}/{key}"


def put(base, kind, key, obj) -> bool:
    data = json.dumps(obj).encode()
    req = urllib.request.Request(_url(base, kind, key), data=data, method="POST",
                                 headers=_hdrs({"Content-Type": "application/json"}))
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def get(base, kind, key, retries=1):
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(_url(base, kind, key), headers=_hdrs())
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception:
            time.sleep(0.3)
    return None


def get_blob(base, kind, key) -> bytes:
    """Raw stored bytes (used for confidentiality checks: this is all the relay can see)."""
    try:
        req = urllib.request.Request(_url(base, kind, key), headers=_hdrs())
        return urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        return b""


def listing(base):
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/list", headers=_hdrs())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception:
        return {}
