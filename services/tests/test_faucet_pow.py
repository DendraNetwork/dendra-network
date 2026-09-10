"""The faucet proof of work: the token produced by the CLIENT is accepted by the server's VERIFIER.

The public faucet refuses any request without `pow` (DENDRA_FAUCET_POW_BITS > 0). A third-party miner
that does not solve that PoW gets a 400, then tries to register on-chain from an empty account and will
never receive a job. These tests hold both ends of the contract together - same digest, same separator,
same encoding - and require a refusal to be NAMED rather than swallowed.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import faucet
import miner

ADDR = "dendra1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
BITS = 12  # ~4096 attempts: enough to prove the contract, short enough for a test


def test_the_client_token_passes_the_server_verifier():
    """The same code on both sides: `solve_pow` feeds the service's REAL `_pow_ok`."""
    nonce = faucet.solve_pow(ADDR, BITS, deadline_s=60)
    assert nonce, "the solver must return a nonce within the time bound"
    old = faucet.POW_BITS
    faucet.POW_BITS = BITS
    try:
        assert faucet._pow_ok(ADDR, nonce) is True
        # The token is bound TO THE ADDRESS: reusing it elsewhere is worthless (anti-Sybil).
        assert faucet._pow_ok(ADDR[:-1] + "z", nonce) is False
        assert faucet._pow_ok(ADDR, "") is False
    finally:
        faucet.POW_BITS = old


def test_zero_difficulty_disarms_the_pow():
    assert faucet.pow_ok(ADDR, "", 0) is True
    assert faucet.solve_pow(ADDR, 0) == ""


def test_leading_bits_counted_exactly():
    assert faucet.leading_zero_bits(b"\x00\x00\xff") == 16
    assert faucet.leading_zero_bits(b"\x01") == 7
    assert faucet.leading_zero_bits(b"\x00\x00") == 16
    assert faucet.leading_zero_bits(b"\x80") == 0


class _Faucet(BaseHTTPRequestHandler):
    """The faucet reduced to its CONTRACT: announce the difficulty on GET, apply the real verifier on
    POST.

    Funding itself - signing a transaction - is out of scope here. What is proven is that a request
    produced by the client clears the guard that refused every earlier one.
    """
    bits = BITS
    received = []

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"status": "ok", "pow_bits": self.bits})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        _Faucet.received.append(body)
        addr = str(body.get("address", ""))
        if not faucet._RE_ADDR.match(addr):
            return self._send(400, {"error": "invalid address"})
        if not faucet.pow_ok(addr, body.get("pow", ""), self.bits):
            return self._send(400, {"error": "PoW required or invalid", "pow_bits": self.bits})
        self._send(200, {"ok": True, "info": "txhash"})

    def log_message(self, *a):
        return


def _server(bits, announce=True):
    _Faucet.bits = bits
    _Faucet.received = []
    handler = _Faucet
    if not announce:
        class _Silent(_Faucet):
            def do_GET(self):
                self._send(200, {"status": "ok"})   # older faucet: does not announce its difficulty
        handler = _Silent
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def test_the_miner_funds_its_address_on_a_pow_faucet():
    srv, url = _server(BITS)
    try:
        ok, why = miner.faucet_fund(url, ADDR)
    finally:
        srv.shutdown()
    assert ok is True, why
    assert _Faucet.received and _Faucet.received[-1].get("pow"), "the request must carry a PoW token"


def test_retry_when_the_faucet_does_not_announce_its_difficulty():
    """The difficulty named in the refusal is HONOURED: exactly one retry, not a silent give-up."""
    srv, url = _server(BITS, announce=False)
    try:
        ok, why = miner.faucet_fund(url, ADDR)
    finally:
        srv.shutdown()
    assert ok is True, why
    assert len(_Faucet.received) == 2, "one request without a token, then ONE retry with the required token"
    assert not _Faucet.received[0].get("pow") and _Faucet.received[1].get("pow")


def test_a_refusal_is_named_not_swallowed():
    """The body of the refusal reaches the caller: a silent `False` leaves the operator with no
    diagnosis."""
    srv, url = _server(BITS)
    try:
        ok, why = miner.faucet_fund(url, "dendra1trop-court")
    finally:
        srv.shutdown()
    assert ok is False
    assert "400" in why and "invalid address" in why, why


def test_end_to_end_against_the_REAL_service():
    """The miner funds its address through the faucet's REAL handler: address guard, PoW, quotas.

    Only the transfer itself is neutralized, because it signs a transaction through dendrad.
    Everything that used to refuse the miner's request is production code here, not a reduction
    written for the test.
    """
    old_bits, old_state, old_fund = faucet.POW_BITS, faucet.STATE_FILE, faucet.fund
    faucet.POW_BITS = BITS
    faucet.STATE_FILE = ""          # no state on disk
    faucet.fund = lambda addr: (True, "TXHASH")
    faucet._addr_last.pop(ADDR, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), faucet.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    try:
        ok, why = miner.faucet_fund(url, ADDR)
    finally:
        srv.shutdown()
        faucet.POW_BITS, faucet.STATE_FILE = old_bits, old_state
        faucet.fund = old_fund
        faucet._addr_last.pop(ADDR, None)
    assert ok is True, why


def test_an_unreachable_faucet_returns_the_cause():
    ok, why = miner.faucet_fund("http://127.0.0.1:1/", ADDR)
    assert ok is False and why, "an unreachable faucet must return the cause, not a bare False"


def test_an_out_of_reach_pow_returns_a_bounded_failure():
    """An unreachable difficulty returns a NAMED failure, never an endless loop at start-up."""
    old = miner.POW_MAX_S
    miner.POW_MAX_S = 0.2
    srv, url = _server(64)
    try:
        ok, why = miner.faucet_fund(url, ADDR)
    finally:
        miner.POW_MAX_S = old
        srv.shutdown()
    assert ok is False
    assert "PoW" in why and "64 bits" in why, why
