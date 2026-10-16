"""The miner declares its kit to the faucet (ADR-058, decision 5).

From DENDRA_KIT_DEADLINE the faucet refuses a request whose body declares `kit_version` below
DENDRA_KIT_MIN, or declares none (faucet.py::H.do_POST), before the proof of work. The miner's
request is written by miner.py::_faucet_post, so the field has to leave from there under the name
the faucet reads. These tests read the body as the server receives it, and drive the faucet's real
handler with its floor armed: a request without the field is refused, one carrying the kit's own number
is served.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import faucet
import miner
from modea import kit_version as kitv

ADDR = "dendra1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"


class _Capture(BaseHTTPRequestHandler):
    """A faucet that keeps the bodies it receives and serves everything: what is measured here is what
    the miner sends, not what the faucet does with it."""
    received = []

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"status": "ok", "pow_bits": 0})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        _Capture.received.append(body)
        self._send(200, {"ok": True, "info": "txhash"})

    def log_message(self, *a):
        return


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def _kit_file(tmp_path, monkeypatch, text):
    p = tmp_path / "KIT_VERSION"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv(kitv.KIT_VERSION_FILE_ENV, str(p))
    return p


def test_the_request_declares_the_kit_under_the_field_the_faucet_reads(tmp_path, monkeypatch):
    _kit_file(tmp_path, monkeypatch, "7\n")
    _Capture.received = []
    srv, url = _serve(_Capture)
    try:
        ok, why = miner.faucet_fund(url, ADDR)
    finally:
        srv.shutdown()
    assert ok is True, why
    body = _Capture.received[-1]
    assert body.get(faucet.KIT_VERSION_FIELD) == 7, body
    # The same bytes, read by the faucet's own reader: one name on both sides of the contract.
    assert faucet.declared_kit_version(body) == 7


def test_an_unreadable_kit_file_sends_no_field_rather_than_a_zero(tmp_path, monkeypatch):
    monkeypatch.setenv(kitv.KIT_VERSION_FILE_ENV, str(tmp_path / "missing"))
    _Capture.received = []
    srv, url = _serve(_Capture)
    try:
        miner.faucet_fund(url, ADDR)
    finally:
        srv.shutdown()
    body = _Capture.received[-1]
    assert faucet.KIT_VERSION_FIELD not in body, body
    assert faucet.declared_kit_version(body) is None


def test_the_real_faucet_with_its_floor_armed_serves_the_kit_and_refuses_the_stale_one(tmp_path, monkeypatch):
    """The faucet's real handler, floor armed at 7 since an instant in the past: the kit at 7 is served,
    the kit at 3 is told it is stale. Only the transfer is neutralized (it signs through dendrad)."""
    saved = (faucet.POW_BITS, faucet.STATE_FILE, faucet.fund,
             faucet.KIT_FLOOR, faucet.KIT_DEADLINE_TS)
    faucet.POW_BITS = 0
    faucet.STATE_FILE = ""
    faucet.fund = lambda addr, *amount: (True, "TXHASH")
    faucet.KIT_FLOOR, faucet.KIT_DEADLINE_TS = 7, 0.0
    faucet._addr_last.pop(ADDR, None)
    srv, url = _serve(faucet.H)
    try:
        assert faucet.kit_floor_now() == 7
        _kit_file(tmp_path, monkeypatch, "3\n")
        ok, why = miner.faucet_fund(url, ADDR)
        assert ok is False and "403" in why and "stale kit" in why, why
        _kit_file(tmp_path, monkeypatch, "7\n")
        ok, why = miner.faucet_fund(url, ADDR)
        assert ok is True, why
    finally:
        srv.shutdown()
        (faucet.POW_BITS, faucet.STATE_FILE, faucet.fund,
         faucet.KIT_FLOOR, faucet.KIT_DEADLINE_TS) = saved
        faucet._addr_last.pop(ADDR, None)


def test_a_stale_kit_refusal_is_classed_by_the_floor_field_not_by_its_text(tmp_path, monkeypatch):
    """The word deploy/join.sh reads (`deferred:stale_kit`) comes from the floor field of the real faucet's 403,
    read as an integer; a 403 that carries no floor stays an HTTP code, and the word is a known class."""
    saved = (faucet.POW_BITS, faucet.STATE_FILE, faucet.fund,
             faucet.KIT_FLOOR, faucet.KIT_DEADLINE_TS)
    faucet.POW_BITS = 0
    faucet.STATE_FILE = ""
    faucet.fund = lambda addr, *amount: (True, "TXHASH")
    faucet.KIT_FLOOR, faucet.KIT_DEADLINE_TS = 7, 0.0
    faucet._addr_last.pop(ADDR, None)
    srv, url = _serve(faucet.H)
    try:
        _kit_file(tmp_path, monkeypatch, "3\n")
        ok, why, reason = miner.faucet_fund_classified(url, ADDR)
        assert ok is False and reason == "stale_kit", (why, reason)
        assert reason in miner.FAUCET_REASONS
    finally:
        srv.shutdown()
        (faucet.POW_BITS, faucet.STATE_FILE, faucet.fund,
         faucet.KIT_FLOOR, faucet.KIT_DEADLINE_TS) = saved
        faucet._addr_last.pop(ADDR, None)
    # The same code without the field, and the field as text or as a bool: none of them is the floor.
    assert miner.faucet_reason(403, json.dumps({"error": "stale kit"})) == "http_403"
    assert miner.faucet_reason(403, json.dumps({faucet.KIT_MIN_FIELD: "7"})) == "http_403"
    assert miner.faucet_reason(403, json.dumps({faucet.KIT_MIN_FIELD: True})) == "http_403"
    assert miner.faucet_reason(403, json.dumps({faucet.KIT_MIN_FIELD: 7})) == "stale_kit"
