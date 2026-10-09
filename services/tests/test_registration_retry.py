# -*- coding: utf-8 -*-
"""The miner daemon's REGISTRATION, replayed until the chain records the miner (one identity per card).

WHAT IT PINS, each with its case:
  * the faucet's refusal is CLASSED from the JSON `info` of its 429, compared with faucet's own
    REFUSAL_* constants: ip_quota, addr_cooldown, global_cap; a text that matches none is `unknown`, never
    `ip_quota` (that word stops deploy/join.sh from starting the next identities of a rig);
  * the registry decides, in three states, never stake_of: UNREAD -> zero faucet request and zero
    create-miner; PRESENT at stake 0 -> zero create-miner (a miner slashed to zero exists);
  * the loop REPLAYS the attempt at its cadence, and the miner registers once the faucet pays;
  * "ready" is printed only of a registered miner, and what a not-registered miner prints carries neither
    "ready" nor "create-miner" (deploy/join.sh::wait_healthy reads those words as a registration);
  * the heartbeat carries registration.state, which deploy/join.sh reads.
The faucet is a REAL local HTTP server answering like faucet.py (a 429 whose body names the cap);
the registry and the chain are replaced; the clock is injected. The subject can be pointed at a MUTATED copy
(DENDRA_DAEMON_FILE), so a harness can show each case turns red when its property is removed.
"""
import http.server
import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    md = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = md
    _spec.loader.exec_module(md)
else:
    import miner as md  # noqa: E402

import faucet as faucet_pow  # noqa: E402

ADDR = "dendra1" + "q" * 38


class Faucet:
    """A local faucet: GET / announces no PoW; each POST takes the next scripted answer (code, body)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.posts = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

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
                n = int(self.headers.get("Content-Length", "0"))
                outer.posts.append(json.loads(self.rfile.read(n) or b"{}"))
                code, body = outer.answers.pop(0) if outer.answers else (200, {"ok": True})
                self._send(code, body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def ip_quota():
    return 429, {"ok": False, "error": "rate limited", "info": faucet_pow.REFUSAL_IP_QUOTA, "address": ADDR}


# ── the class of a refusal ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("answer, word", [
    (ip_quota(), "ip_quota"),
    ((429, {"info": faucet_pow.REFUSAL_ADDR_COOLDOWN}), "addr_cooldown"),
    ((429, {"info": faucet_pow.REFUSAL_GLOBAL_CAP}), "global_cap"),
    ((429, {"info": "too many requests from this IP"}), "unknown"),
    ((429, {"error": "rate limited"}), "unknown"),
    ((400, {"error": "PoW required or invalid", "pow_bits": 0}), "pow"),
    ((502, {"ok": False, "info": "transfer failed"}), "http_502"),
])
def test_a_faucet_refusal_is_classed_from_its_parsed_info(answer, word):
    f = Faucet([answer])
    try:
        ok, detail, bits, reason = md._faucet_post(f.url, ADDR, "")
    finally:
        f.close()
    assert ok is False and reason == word, (answer, reason)


def test_an_unknown_info_is_never_read_as_the_ip_quota():
    assert md.faucet_reason(429, json.dumps({"info": "daily quota"})) == "unknown"
    assert md.faucet_reason(429, "not json at all") == "unknown"


def test_no_answer_at_all_is_transport():
    ok, detail, reason = md.faucet_fund_classified("http://127.0.0.1:1/", ADDR)
    assert ok is False and reason == "transport"


# ── one attempt, guarded by the registry ──────────────────────────────────────────────────────────────
class Chain:
    """The registry and create-miner: ABSENT until a create-miner follows a PAID drip, then PRESENT."""

    def __init__(self, monkeypatch, states=None, funded_registers=True):
        self.states = list(states or [])
        self.creates = 0
        self.funded = False
        self.present = False
        self.funded_registers = funded_registers
        monkeypatch.setattr(md, "registry_record", self.record)
        monkeypatch.setattr(md, "tx_from", self.tx_from)
        monkeypatch.setattr(md, "wait_tx", lambda out, timeout=24: out)
        monkeypatch.setattr(md, "bal_token", lambda addr: 10_000_000 if self.funded else 0)
        monkeypatch.setattr(md, "chain_min_stake", lambda default=50000: 1_000_000)
        monkeypatch.setattr(md, "_registration_stake", lambda: 1_000_000)
        monkeypatch.setattr(md, "time", types.SimpleNamespace(time=lambda: 1_000_000.0, sleep=lambda s: None))

    def record(self, mid):
        if self.states:
            return self.states.pop(0)
        if self.present:
            return md.PRESENT, {"creator": ADDR, "operator": ADDR, "region": "eu", "stake": 1_000_000}
        return md.ABSENT, {}

    def tx_from(self, frm, sub, *pos, flags=()):
        if sub == "create-miner":
            self.creates += 1
            if self.funded and self.funded_registers:
                self.present = True
                return "ok"
        return ""


def args(faucet_url, keydir):
    return types.SimpleNamespace(id="dm1bench", faucet=faucet_url, keydir=str(keydir), relay="http://relay.invalid",
                                 once=True, poll=3.0)


def test_an_unread_registry_sends_nothing(monkeypatch, tmp_path):
    ch = Chain(monkeypatch, states=[(md.UNREAD, {"why": "node down"})])
    f = Faucet([])
    try:
        st, why = md.register_attempt(args(f.url, tmp_path), ADDR, "pub", "vpk")
    finally:
        f.close()
    assert st == md.UNREAD
    assert f.posts == [] and ch.creates == 0, "a node that does not answer is never 'not registered'"


def test_a_miner_slashed_to_zero_exists_and_is_not_registered_again(monkeypatch, tmp_path):
    ch = Chain(monkeypatch, states=[(md.PRESENT, {"creator": ADDR, "operator": ADDR, "region": "eu", "stake": 0})])
    f = Faucet([])
    try:
        st, why = md.register_attempt(args(f.url, tmp_path), ADDR, "pub", "vpk")
    finally:
        f.close()
    assert st == md.REGISTERED and f.posts == [] and ch.creates == 0


def test_an_ip_quota_refusal_defers_with_its_word_and_says_it_once(monkeypatch, tmp_path, capsys):
    ch = Chain(monkeypatch)
    f = Faucet([ip_quota()])
    try:
        st, why = md.register_attempt(args(f.url, tmp_path), ADDR, "pub", "vpk")
    finally:
        f.close()
    out = capsys.readouterr().out
    assert (st, why) == (md.DEFERRED, "ip_quota")
    assert md.REGISTRATION_DEFERRED_LINE.format(reason="ip_quota") in out


def test_what_a_not_registered_miner_prints_never_reads_as_a_registration():
    for reason in ("ip_quota", "addr_cooldown", "global_cap", "pow", "unknown", "registry_unread", "chain_refused"):
        line = md.not_registered_line("dm1bench", reason, 600)
        deferred = md.REGISTRATION_DEFERRED_LINE.format(reason=reason)
        for text in (line, deferred):
            assert "ready" not in text and "create-miner" not in text, text


def test_the_heartbeat_carries_the_registration_state(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(status))
    md._STATUS.clear()
    Chain(monkeypatch)
    f = Faucet([ip_quota()])
    try:
        st, why, retry_at = md.registration_step(args(f.url, tmp_path), ADDR, "pub", "vpk", now=1_000_000.0)
    finally:
        f.close()
    reg = json.loads(status.read_text())["registration"]
    assert reg == {"state": "deferred", "reason": "ip_quota", "retry_at": int(1_000_000 + md.REGISTER_RETRY_S)}


def test_an_address_in_cooldown_waits_longer_than_the_cadence():
    assert md.registration_retry_delay(md.DEFERRED, "addr_cooldown") > md.registration_retry_delay(md.DEFERRED, "ip_quota")


# ── the loop: the attempt is REPLAYED at its cadence, and "ready" waits for the registry ──────────────
class Stop(Exception):
    pass


def test_the_loop_replays_the_registration_and_says_ready_only_once_registered(monkeypatch, tmp_path, capsys):
    status = tmp_path / "status.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(status))
    monkeypatch.setenv("DENDRA_CONFINE", "0")
    md._STATUS.clear()
    ch = Chain(monkeypatch)
    f = Faucet([ip_quota()])           # refused once, then paid

    def paid_after_refusal(faucet, addr):
        r = md._faucet_post(faucet, addr, "")
        if r[0]:
            ch.funded = True
        return r[0], r[1], r[3]
    monkeypatch.setattr(md, "faucet_fund_classified", paid_after_refusal)
    clock = {"t": 1_000_000.0, "sleeps": 0, "ready_at": None}

    def sleep(s):
        clock["t"] += s
        clock["sleeps"] += 1
        if clock["sleeps"] > 400:
            raise Stop()

    monkeypatch.setattr(md, "time", types.SimpleNamespace(time=lambda: clock["t"], sleep=sleep))
    monkeypatch.setattr(md, "REGISTER_RETRY_S", 30.0)
    monkeypatch.setattr(sys, "argv", ["miner.py", "--id", "dm1bench", "--relay", "http://relay.invalid",
                                      "--keydir", str(tmp_path / "keys"), "--faucet", f.url, "--poll", "3"])
    monkeypatch.setattr(md, "keys_addr", lambda name, keydir=None: ADDR)
    monkeypatch.setattr(md, "align_identity", lambda name, addr, owner="": "dm1bench")
    monkeypatch.setattr(md, "decide_owner", lambda mid, addr, configured=None, read=None: "")
    monkeypatch.setattr(md.relay_signature, "address_from_key", lambda *a, **k: ADDR)
    monkeypatch.setattr(md.relay, "put", lambda *a, **k: True)
    monkeypatch.setattr(md.relay, "set_sign_key", lambda name: None)
    monkeypatch.setattr(md.relay, "listing", lambda *a, **k: {})
    monkeypatch.setattr(md, "model_weights_hash", lambda: "")
    monkeypatch.setattr(md, "vrf_identity", lambda keydir, mid: ("", ""))
    monkeypatch.setattr(md, "miner_operator", lambda mid: "")
    monkeypatch.setattr(md, "miner_vrf_onchain", lambda mid: "")
    monkeypatch.setattr(md, "pick_backend", lambda want: "mock")
    monkeypatch.setattr(md, "Miner", lambda *a, **k: object())
    monkeypatch.setattr(md, "PAYOUT_ADDRESS", "")
    try:
        with pytest.raises(Stop):
            md.main()
    finally:
        f.close()
    out = capsys.readouterr().out
    lines = out.splitlines()
    ready = [i for i, l in enumerate(lines) if " ready " in l and l.startswith("[daemon] miner dm1bench")]
    deferred = [i for i, l in enumerate(lines) if "is NOT registered yet (ip_quota)" in l]
    assert deferred, out[-2000:]
    assert len(f.posts) == 2, "one refused drip, then one paid at the next due attempt"
    assert len(ready) == 1 and ready[0] > deferred[0], "ready only after the registration, and once"
    assert ch.present
    assert json.loads(status.read_text())["registration"]["state"] == "registered"
