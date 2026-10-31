# -*- coding: utf-8 -*-
"""Who a POST is charged to, and which signed report the registry keeps.

Behind Caddy every POST reaches the registry from the proxy's address. Keyed on the socket, the per-IP limit was
one bucket for the whole network: one sender looping on unsigned POSTs kept it full, every miner's signed report
got 429, and the season kept reading each identity's earlier kit number, or none.

What these cases hold, against the shipped `Handler.do_POST`, `_client_ip`, `older_than_stored` and
`cap_declared`:
  * an unsigned POST is charged to the real client: the last X-Forwarded-For hop when DENDRA_TRUST_PROXY is on,
    the socket peer otherwise, so a forwarded header from a peer nobody trusts changes nothing;
  * a signed report draws from its proven identity's own allowance, so 31 unsigned POSTs from one peer leave a
    signed POST from that same peer its 200; a signature that proves nothing is charged to the IP;
  * a signed report older than the one stored for that identity (by the height, then the account sequence, both
    inside the signature) is refused and changes nothing; an equal or newer one is stored;
  * the declared entries are capped, the oldest leave first, and a signed entry is never evicted.
The signature itself is benched with real keys in dendra/onchain-staging/dendra_preuve_capacite_paire.py; here a
header stands for the proof, so the HTTP path is what is held. The subject can be pointed at a mutated copy
(DENDRA_CAPACITY_SERVER_FILE), so a harness can show each case turns red.
"""
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("DENDRA_CAPACITY_SERVER_FILE"):
    _spec = importlib.util.spec_from_file_location("capacity_server", os.environ["DENDRA_CAPACITY_SERVER_FILE"])
    cs = importlib.util.module_from_spec(_spec)
    sys.modules["capacity_server"] = cs
    _spec.loader.exec_module(cs)
else:
    import capacity_server as cs  # noqa: E402

MINER_A = "dm1ratesignedaaaaaaaaaaaa"
MINER_B = "dm1ratesignedbbbbbbbbbbbb"
PROXY = "192.0.2.7"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "DB", tmp_path / "capacity.json")
    monkeypatch.setattr(cs, "_RATE", {})
    monkeypatch.setattr(cs, "_RATE_SIGNED", {}, raising=False)
    monkeypatch.setattr(cs, "TRUST_PROXY", False, raising=False)
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: {MINER_A, MINER_B})
    monkeypatch.setattr(cs, "_proven_miner_of", lambda h, b: (h.get("X-Bench-Proof") or "").strip())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _post(base, body, headers=None):
    req = urllib.request.Request(base + "/capacity", data=json.dumps(body).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _unsigned(i, **extra):
    return dict({"node_id": "n-flood-%d" % i, "machine": "flood%04d" % i, "backend": "cpu", "tier": 0}, **extra)


def _signed_headers(mid, height=0, seq=1):
    """The headers of a report proven to come from `mid`, signed at (`height`, `seq`). The bench proof header
    stands for the signature; the height and sequence travel in the headers the real signer writes."""
    return {"X-Bench-Proof": mid, "X-Dendra-Miner": mid, "X-Dendra-Height": str(height), "X-Dendra-Seq": str(seq)}


def _signed_body(mid, kit):
    return {"node_id": "n-" + mid[-4:], "machine": "box" + mid[-4:], "miner_ids": [mid], "backend": "gpu",
            "tier": 3, "kit_version": kit, "judge_role": "active"}


def _stored(base_db):
    return json.loads(Path(base_db).read_text("utf-8"))


# -- 1. unsigned traffic cannot starve a signed report -----------------------------------------------------
def test_31_unsigned_posts_from_one_peer_then_a_signed_post_gets_200(server):
    codes = [_post(server, _unsigned(i)) for i in range(cs.RATE_N + 1)]
    assert codes[:cs.RATE_N] == [200] * cs.RATE_N and codes[cs.RATE_N] == 429, codes
    assert _post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, seq=7)) == 200
    assert _stored(cs.DB)[cs.KEY_PROVEN + MINER_A]["kit_version"] == 4


def test_the_signed_allowance_is_per_identity_and_order(server):
    codes = [_post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, seq=7))
             for _ in range(cs.RATE_SIGNED_N + 1)]
    assert codes[:cs.RATE_SIGNED_N] == [200] * cs.RATE_SIGNED_N and codes[-1] == 429, codes
    # Another identity, and a newer report of the same one, each have their own allowance: replaying a captured
    # report uses up only that report's.
    assert _post(server, _signed_body(MINER_B, 4), _signed_headers(MINER_B, seq=3)) == 200
    assert _post(server, _signed_body(MINER_A, 5), _signed_headers(MINER_A, seq=8)) == 200


def test_a_signature_that_proves_nothing_is_charged_to_the_ip(server):
    # Every signature header but a proof that holds: the deposit is stored as declared, and charged to the IP.
    bad = {"X-Dendra-Miner": MINER_A, "X-Dendra-Height": "0", "X-Dendra-Seq": "1", "X-Dendra-Sig": "AAAA"}
    codes = [_post(server, _unsigned(i), bad) for i in range(cs.RATE_N + 1)]
    assert codes[:cs.RATE_N] == [200] * cs.RATE_N and codes[cs.RATE_N] == 429, codes
    assert _post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, seq=7)) == 200


# -- 2. the client behind the proxy ---------------------------------------------------------------------------
def test_a_forwarded_header_from_an_untrusted_peer_is_ignored(monkeypatch):
    monkeypatch.setattr(cs, "TRUST_PROXY", False, raising=False)
    assert cs._client_ip({"X-Forwarded-For": "198.51.100.7"}, PROXY) == PROXY


def test_behind_the_trusted_proxy_the_last_hop_is_the_client(monkeypatch):
    monkeypatch.setattr(cs, "TRUST_PROXY", True, raising=False)
    # The first hop is whatever the client sent; the proxy appended the last one.
    assert cs._client_ip({"X-Forwarded-For": "203.0.113.9, 198.51.100.7"}, PROXY) == "198.51.100.7"
    assert cs._client_ip({}, PROXY) == PROXY


def test_over_http_an_untrusted_forwarded_header_does_not_open_a_new_bucket(server):
    codes = [_post(server, _unsigned(i), {"X-Forwarded-For": "198.51.100.%d" % i}) for i in range(cs.RATE_N + 1)]
    assert codes[cs.RATE_N] == 429, codes


def test_over_http_behind_the_trusted_proxy_each_client_has_its_own_bucket(server, monkeypatch):
    monkeypatch.setattr(cs, "TRUST_PROXY", True, raising=False)
    flood = {"X-Forwarded-For": "198.51.100.7"}
    codes = [_post(server, _unsigned(i), flood) for i in range(cs.RATE_N + 1)]
    assert codes[cs.RATE_N] == 429, codes
    assert _post(server, _unsigned(99), {"X-Forwarded-For": "198.51.100.8"}) == 200


# -- 3. a replayed older signed report ---------------------------------------------------------------------
def test_a_replayed_older_signed_report_is_refused(server):
    assert _post(server, _signed_body(MINER_A, 3), _signed_headers(MINER_A, seq=5)) == 200
    assert _post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, seq=7)) == 200
    # The earlier report, posted again with its own signed sequence: refused, and the store keeps kit 4.
    assert _post(server, _signed_body(MINER_A, 3), _signed_headers(MINER_A, seq=5)) == 409
    assert _stored(cs.DB)[cs.KEY_PROVEN + MINER_A]["kit_version"] == 4
    # The same order again passes (the same report twice changes nothing), and a newer one is stored.
    assert _post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, seq=7)) == 200
    assert _post(server, _signed_body(MINER_A, 5), _signed_headers(MINER_A, seq=8)) == 200
    assert _stored(cs.DB)[cs.KEY_PROVEN + MINER_A]["kit_version"] == 5


def test_the_signed_height_comes_before_the_sequence(server):
    assert _post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, height=120, seq=2)) == 200
    assert _post(server, _signed_body(MINER_A, 3), _signed_headers(MINER_A, height=0, seq=9)) == 409
    assert _stored(cs.DB)[cs.KEY_PROVEN + MINER_A]["kit_version"] == 4


def test_a_stale_stored_report_takes_any_signed_order():
    # After a chain reset the heights and sequences start again: a stale record no longer bounds them.
    now = time.time()
    prev = {"_proven": MINER_A, "_signed_height": 0, "_signed_sequence": 50, "ts": int(now)}
    assert cs.older_than_stored(prev, MINER_A, (0, 3), now) is True
    prev["ts"] = int(now) - cs.STALE_S - 60
    assert cs.older_than_stored(prev, MINER_A, (0, 3), now) is False
    # A record stored before the order existed bounds nothing, and another identity's record never does.
    assert cs.older_than_stored({"_proven": MINER_A, "ts": int(now)}, MINER_A, (0, 3), now) is False
    assert cs.older_than_stored(dict(prev, _proven=MINER_B, ts=int(now)), MINER_A, (0, 3), now) is False


def test_a_proven_report_without_a_readable_order_is_refused(server):
    headers = dict(_signed_headers(MINER_A), **{"X-Dendra-Seq": "seven"})
    assert _post(server, _signed_body(MINER_A, 4), headers) == 400


# -- 4. the declared entries are capped ---------------------------------------------------------------------
def test_the_oldest_declared_entries_leave_first_and_signed_ones_stay(server, monkeypatch):
    monkeypatch.setattr(cs, "DECLARED_MAX", 3, raising=False)
    assert _post(server, _signed_body(MINER_A, 4), _signed_headers(MINER_A, seq=7)) == 200
    for i in range(5):
        assert _post(server, _unsigned(i)) == 200
        time.sleep(1.05)             # `ts` is in whole seconds: one per second orders the entries
    store = _stored(cs.DB)
    declared = sorted(k for k in store if k.startswith(cs.KEY_DECLARED))
    assert declared == sorted(cs.KEY_DECLARED + "flood%04d::n-flood-%d" % (i, i) for i in (2, 3, 4)), declared
    assert cs.KEY_PROVEN + MINER_A in store
