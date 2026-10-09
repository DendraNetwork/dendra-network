# -*- coding: utf-8 -*-
"""A miner's FIRST INSTALL, as the daemon prints it -- and what deploy/join.sh::wait_healthy reads in it.

WHAT IT PINS, each with its case:
  * the chain's answer for an address no credit has reached ("account ... not found: key not found") is a
    TRANSIENT state, CompteAbsent, told apart from a failure; relay_client says it once, without the chain's
    wording, and says again when signing comes back;
  * main() makes the two bootstrap deposits (pub, attest) AFTER the registration, so a relay that refuses
    unsigned writes accepts both -- driven end to end, with the REAL relay_client and the REAL
    relay_signature.numero_et_sequence against a fake `dendrad` that answers like the chain, and a fake relay
    in `enforce`; the log main() produced is written to $K1_LOG_OUT when set, for the shell bench that feeds it
    to the shipped wait_healthy;
  * the availability window: a challenge whose window closed is said in ONE line naming the next challenge's
    block, remembered in the key directory, and neither proven again nor said again after a restart;
  * the IDENTITY MISMATCH way out names the identity's own slot;
  * `python3 -m modea.keyring address` reads identity, address and keys at rest from the volume, not a log.
The subject can be pointed at MUTATED copies: DENDRA_DAEMON_FILE for the daemon; a copy of the whole package
for the rest (the shell bench dendra/onchain-staging/dendra_premiere_installation_test.sh does both).
"""
import http.server
import importlib.util
import json
import os
import stat
import sys
import threading
import types
from pathlib import Path

import pytest

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))

import modea.relay_signature as rs  # noqa: E402  (imported first: a mutated daemon copy finds it here)
from modea import keyring as kring  # noqa: E402
import relay_client as relay  # noqa: E402

if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    md = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = md
    _spec.loader.exec_module(md)
else:
    import miner as md  # noqa: E402

ADDR = "dendra1" + "q" * 38
NL = chr(10)

# What the chain answers for an address that has no account yet, as `dendrad query auth account` prints it.
ABSENT_ANSWER = ("Error: rpc error: code = NotFound desc = account " + ADDR + " not found: key not found")
# The words a log must never carry for a healthy first start (deploy/join.sh::WH_FAIL_RE, and the bare
# "key not found" that the old pattern held).
FAILURE_WORDS = ("key not found", "is not a valid name or address", "KEYS NOT OPENED", "IDENTITY MISMATCH",
                 "unauthorized", "ON-CHAIN REGISTRATION FAILED")


def fake_dendrad(tmp_path, monkeypatch):
    """A `dendrad` first on PATH: `query auth account` answers like the chain -- no account until the state
    file exists, then an account WITHOUT `sequence` (proto3 omits it at zero). `keys show` answers the name
    listed in the keys file. Anything else is refused loudly."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    state = tmp_path / "funded"
    src = NL.join([
        "#!" + sys.executable,
        "import json, os, sys",
        "args = sys.argv[1:]",
        "state = os.environ['BANC_CHAIN_STATE']",
        "if args[:3] == ['query', 'auth', 'account']:",
        "    if os.environ.get('BANC_NODE_DOWN'):",
        "        sys.stderr.write('Error: post failed: dial tcp 127.0.0.1:9: connect: connection refused' + chr(10))",
        "        sys.exit(1)",
        "    if not os.path.exists(state):",
        "        sys.stderr.write(os.environ['BANC_ABSENT_ANSWER'] + chr(10))",
        "        sys.exit(1)",
        "    print(json.dumps({'account': {'@type': '/cosmos.auth.v1beta1.BaseAccount', 'address': args[3],",
        "                                  'account_number': '12'}}))",
        "    sys.exit(0)",
        "if args[:2] == ['keys', 'show']:",
        "    keys = dict(l.split('=', 1) for l in open(os.environ['BANC_KEYS']).read().split() if '=' in l)",
        "    if args[2] in keys:",
        "        print(keys[args[2]])",
        "        sys.exit(0)",
        "    sys.stderr.write('Error: ' + args[2] + ' is not a valid name or address: key not found' + chr(10))",
        "    sys.exit(1)",
        "sys.stderr.write('fake dendrad: unexpected ' + ' '.join(args) + chr(10))",
        "sys.exit(64)",
        "",
    ])
    p = bindir / "dendrad"
    p.write_text(src, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    keys = tmp_path / "keys.txt"
    keys.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("BANC_CHAIN_STATE", str(state))
    monkeypatch.setenv("BANC_ABSENT_ANSWER", ABSENT_ANSWER)
    monkeypatch.setenv("BANC_KEYS", str(keys))
    return state, keys


class Relay:
    """A relay in `enforce`: a write without a signature is refused 401 with its reason, a signed one is
    accepted; GET /list answers the work queue (empty). Every write is recorded with whether it was signed."""

    def __init__(self):
        self.writes = []
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
                self._send(200, {"req": [], "res": [], "pub": [], "attest": []} if self.path == "/list" else {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(n)
                kind = self.path.strip("/").split("/")[0]
                signed = bool(self.headers.get(rs.HEADER_SIG))
                outer.writes.append((kind, signed))
                if not signed:
                    self._send(401, {"why": "unsigned write refused (enforce)"})
                else:
                    self._send(200, {"ok": True})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class Faucet:
    """A faucet that PAYS: GET / announces no proof of work, every POST is accepted."""

    def __init__(self, on_pay):
        outer = self
        self.posts = 0

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
                self.rfile.read(n)
                outer.posts += 1
                on_pay()
                self._send(200, {"ok": True})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _signed_headers(*a, **k):
    # The one link no unit test reaches is `dendrad tx sign` (relay_signature's own docstring says so): a
    # signature is made here only once the account was READ, which is what this file measures.
    return {rs.HEADER_SIG: "banc-signature", rs.HEADER_MINER: "dm1bench"}


@pytest.fixture
def fresh_signing(monkeypatch):
    monkeypatch.setattr(relay, "_SIGN_STATE", "")
    relay._SIGN_CACHE.clear()
    monkeypatch.setattr(rs, "address_from_key", lambda *a, **k: ADDR)
    monkeypatch.setattr(rs, "headers", _signed_headers)
    yield
    relay._SIGN_CACHE.clear()


# ── the account that does not exist YET ──────────────────────────────────────────────────────────────
def test_the_chain_answer_for_a_missing_account_is_transient_not_a_failure(tmp_path, monkeypatch):
    state, _ = fake_dendrad(tmp_path, monkeypatch)
    with pytest.raises(rs.CompteAbsent) as e:
        rs.numero_et_sequence(ADDR)
    assert "key not found" not in str(e.value) and "not a valid name" not in str(e.value), str(e.value)
    assert "key not found" in e.value.texte, "the chain's own words are kept, whole, for whoever needs them"
    state.write_text("1")
    assert rs.numero_et_sequence(ADDR) == ("12", "0"), "sequence omitted (proto3): read 0"
    # THE THIRD STATE: a node that does not answer is a failure, never "no account".
    monkeypatch.setenv("BANC_NODE_DOWN", "1")
    with pytest.raises(rs.SignatureIndisponible) as e2:
        rs.numero_et_sequence(ADDR)
    assert not isinstance(e2.value, rs.CompteAbsent)


def test_signing_says_the_missing_account_once_without_the_chain_wording_then_says_it_is_back(
        tmp_path, monkeypatch, capsys, fresh_signing):
    state, _ = fake_dendrad(tmp_path, monkeypatch)
    relay.set_sign_key("dm1bench")
    assert relay._signature("pub", "dm1bench", b"{}", None, 0) == {}
    assert relay._signature("attest", "dm1bench", b"{}", None, 0) == {}
    first = capsys.readouterr().out
    assert first.count("[relay]") == 1, "said ONCE while the state does not change: " + first
    assert "UNSIGNED" in first and "resumes by itself" in first
    for w in FAILURE_WORDS:
        assert w not in first, (w, first)
    state.write_text("1")
    assert relay._signature("pub", "dm1bench", b"{}", None, 0) == _signed_headers()
    back = capsys.readouterr().out
    assert "SIGNED again" in back, "a recovery must be said, or the last word stays with a failure that is over"
    relay._signature("pub", "dm1bench", b"{}", None, 0)
    assert capsys.readouterr().out == "", "said once per change of state"


# ── the first install, end to end ─────────────────────────────────────────────────────────────────────
class Stop(Exception):
    pass


def test_a_first_install_deposits_after_the_credit_and_its_log_reads_healthy(tmp_path, monkeypatch, capsys,
                                                                              fresh_signing):
    state, _ = fake_dendrad(tmp_path, monkeypatch)
    status = tmp_path / "status.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(status))
    monkeypatch.setenv("DENDRA_CONFINE", "0")
    monkeypatch.delenv("DENDRA_SLOT", raising=False)
    md._STATUS.clear()
    chain = {"present": False}

    def record(mid):
        if chain["present"]:
            return md.PRESENT, {"creator": ADDR, "operator": ADDR, "region": "eu", "stake": 1_000_000}
        return md.ABSENT, {}

    def tx_from(frm, sub, *pos, flags=()):
        if sub == "create-miner" and state.exists():
            chain["present"] = True
            return "ok"
        return ""
    monkeypatch.setattr(md, "registry_record", record)
    monkeypatch.setattr(md, "tx_from", tx_from)
    monkeypatch.setattr(md, "wait_tx", lambda out, timeout=24: out)
    monkeypatch.setattr(md, "bal_token", lambda addr: 10_000_000 if state.exists() else 0)
    monkeypatch.setattr(md, "chain_min_stake", lambda default=50000: 1_000_000)
    monkeypatch.setattr(md, "_registration_stake", lambda: 1_000_000)
    clock = {"t": 1_000_000.0, "sleeps": 0}

    def sleep(s):
        clock["t"] += s
        clock["sleeps"] += 1
        if clock["sleeps"] > 60:
            raise Stop()
    monkeypatch.setattr(md, "time", types.SimpleNamespace(time=lambda: clock["t"], sleep=sleep))
    rel = Relay()
    fau = Faucet(lambda: state.write_text("1"))
    monkeypatch.setattr(sys, "argv", ["miner.py", "--id", "dm1bench", "--relay", rel.url,
                                      "--keydir", str(tmp_path / "keys"), "--faucet", fau.url, "--poll", "3"])
    monkeypatch.setattr(md, "keys_addr", lambda name, keydir=None: ADDR)
    monkeypatch.setattr(md, "align_identity", lambda name, addr, owner="": "dm1bench")
    monkeypatch.setattr(md, "decide_owner", lambda mid, addr, configured=None, read=None: "")
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
        rel.close()
        fau.close()
    out = capsys.readouterr().out
    if os.environ.get("K1_LOG_OUT"):
        Path(os.environ["K1_LOG_OUT"]).write_text(out, encoding="utf-8")
    ready = [l for l in out.splitlines() if l.startswith("[daemon] miner dm1bench ready ")]
    assert len(ready) == 1, out[-2000:]
    kinds = {k: s for k, s in rel.writes}
    assert kinds.get("pub") is True and kinds.get("attest") is True, (
        "both bootstrap deposits SIGNED, so accepted by a relay that refuses unsigned writes: " + repr(rel.writes))
    assert all(s for _, s in rel.writes), "no deposit left unsigned: " + repr(rel.writes)
    for w in FAILURE_WORDS:
        assert w not in out, (w, out[-1500:])
    assert json.loads(status.read_text())["address"] == ADDR, "the address is in the heartbeat, not only a log"


# ── the availability window ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("epoch, eb, deadline, height, want", [
    (10, 100, 20, 1015, ("open", 1100)),
    (10, 100, 20, 1020, ("open", 1100)),
    (10, 100, 20, 1021, ("closed", 1100)),
    (10, 100, 0, 1099, ("open", 1100)),          # 0 is a reading: no deadline, the whole epoch counts
    (10, 100, None, 1050, ("unknown", 1100)),    # deadline not read: the chain decides
    (10, 100, 20, None, ("unknown", 1100)),      # height not read
    (10, 100, 20, 1100, ("stale", 1100)),        # the height is in the next epoch: read again
    (0, 100, 20, 5, ("open", 100)),              # epoch 0 (proto3 omits it) is the first epoch
    (3, 0, 20, 5, ("unknown", None)),            # availability off
])
def test_the_availability_window(epoch, eb, deadline, height, want):
    assert md.avail_window(epoch, eb, deadline, height) == want


def _avail(monkeypatch, keydir, height, refusal="", deadline=20, on_chain=True,
           probe=lambda: (md.PROBE_ANSWERED, "", 0.4)):
    sent = []
    # A fresh backoff for each call: a probe that did not answer in another test must not delay this one.
    monkeypatch.setattr(md, "_PROBE_STATE", {"failed_at": 0.0, "said_for": "", "bad_bound_said": False})
    # Whether the node shows the proof's transaction (miner._tx_on_chain): False is a chain rescued by
    # export, which keeps the challenge and not the transactions.
    monkeypatch.setattr(md, "_tx_on_chain", lambda th: bool(on_chain and th == "A" * 64))
    monkeypatch.setattr(md, "query", lambda sub, *p, flags=(): json.dumps(
        {"params": {"avail_deadline_blocks": str(deadline)}} if sub == "params" else
        {"challenge": "c" * 64, "epoch": "10", "avail_epoch_blocks": "100"}))
    monkeypatch.setattr(md, "_chain_height", lambda: height)

    class _Out:
        stdout = "ab" * 80
        stderr = ""
    monkeypatch.setattr(md.subprocess, "run", lambda *a, **k: _Out())

    def tx(*a, **k):
        sent.append(a)
        return ("code: 18\nraw_log: " + refusal) if refusal else ("code: 0\ntxhash: " + "A" * 64)
    monkeypatch.setattr(md, "tx_from", tx)
    monkeypatch.setattr(md, "wait_tx_height", lambda o, timeout=24: 0 if refusal else 4321)
    return md.prove_availability_once("dm1bench", "a" * 128, "", keydir=str(keydir), probe=probe), sent


def test_a_closed_window_is_said_once_with_the_next_challenge_and_survives_a_restart(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    got, sent = _avail(monkeypatch, tmp_path, 1050)
    out = capsys.readouterr().out
    assert got == "c" * 64 and sent == [], "a closed window sends nothing"
    assert out.count("availability window CLOSED") == 1 and "next challenge at block 1100" in out, out
    assert "REFUSED" not in out
    rec = json.loads((tmp_path / md.AVAIL_RECORD).read_text())
    assert rec["state"] == "window_closed" and rec["challenge"] == "c" * 64
    # A RESTART: a new process (last_chal empty) reads the record, sends nothing, says nothing.
    got, sent = _avail(monkeypatch, tmp_path, 1050)
    assert got == "c" * 64 and sent == [] and capsys.readouterr().out == ""


def test_the_chains_too_late_closes_the_window_when_the_height_was_not_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    got, sent = _avail(monkeypatch, tmp_path, None,
                       refusal="availability proof too late (past avail_deadline_blocks within the epoch)")
    out = capsys.readouterr().out
    assert got == "c" * 64 and len(sent) == 1
    assert out.count("availability window CLOSED") == 1 and "next challenge at block 1100" in out, out
    assert "will retry" not in out


def test_a_proven_challenge_is_remembered_and_not_proven_again_after_a_restart(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    got, sent = _avail(monkeypatch, tmp_path, 1010)
    assert got == "c" * 64 and len(sent) == 1
    rec = json.loads((tmp_path / md.AVAIL_RECORD).read_text())
    assert rec["state"] == "proven" and rec["txhash"] == "A" * 64, "the proof's transaction is kept with it"
    capsys.readouterr()
    got, sent = _avail(monkeypatch, tmp_path, 1010)
    assert got == "c" * 64 and sent == [] and capsys.readouterr().out == ""


def test_a_proven_record_whose_transaction_this_chain_does_not_show_is_proven_again(tmp_path, monkeypatch,
                                                                                     capsys):
    # A RESCUE BY EXPORT (same chain-id, same numbering): the challenge is carried over, the proofs are not. The
    # record says "proven" for that very challenge; the chain shows no such transaction -> proven again.
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    got, sent = _avail(monkeypatch, tmp_path, 1010)
    assert len(sent) == 1
    capsys.readouterr()
    got, sent = _avail(monkeypatch, tmp_path, 1010, on_chain=False)
    assert got == "c" * 64 and len(sent) == 1, "the proof is sent again on a chain that does not show it"
    assert "PROVEN" in capsys.readouterr().out
    # A record written before the hash was kept (no txhash): not trusted either, proven once more.
    (tmp_path / md.AVAIL_RECORD).write_text(json.dumps({"challenge": "c" * 64, "state": "proven", "epoch": 10}))
    got, sent = _avail(monkeypatch, tmp_path, 1010)
    assert len(sent) == 1


def test_another_refusal_is_still_retried(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    got, sent = _avail(monkeypatch, tmp_path, 1010, refusal="invalid VRF proof")
    assert got == "" and "will retry" in capsys.readouterr().out
    assert not (tmp_path / md.AVAIL_RECORD).exists()


# ── presence is proven by a model that ANSWERS (miner.presence_probe) ──────────────────────────
class _Engine:
    """A backend whose answer the test chooses; it records what it was asked."""

    def __init__(self, answer="ready", exc=None):
        self.answer, self.exc, self.calls = answer, exc, []

    def generate(self, prompt, max_out=0, temperature=None, timeout_s=None):
        self.calls.append((prompt, max_out, timeout_s))
        if self.exc is not None:
            raise self.exc
        return self.answer


class _HttpError(Exception):
    def __init__(self, code):
        super().__init__("HTTP %d" % code)
        self.response = types.SimpleNamespace(status_code=code)


def test_the_probe_answered_is_the_only_state_that_proves(monkeypatch):
    monkeypatch.delenv("DENDRA_PRESENCE_PROBE_S", raising=False)
    e = _Engine("ready")
    assert md.presence_probe(e)[:2] == (md.PROBE_ANSWERED, "")
    assert e.calls == [(md.PRESENCE_PROBE_PROMPT, md.PRESENCE_PROBE_MAX_OUT, md.PRESENCE_PROBE_S_DEFAULT)], \
        "the served model is asked once, a short answer, under the probe's own bound"
    assert md.presence_probe(_Engine("   "))[0] == md.PROBE_SILENT, "an empty answer is a measured no"
    assert md.presence_probe(_Engine(None))[0] == md.PROBE_SILENT
    st, why, _ = md.presence_probe(_Engine(exc=_HttpError(404)))
    assert st == md.PROBE_SILENT and "HTTP 404" in why, "the engine without the model is a measured no"
    st, why, _ = md.presence_probe(_Engine(exc=TimeoutError("read timed out")))
    assert st == md.PROBE_UNMEASURED and "TimeoutError" in why, "no answer in time is not an answer"
    st, why, _ = md.presence_probe(_Engine(exc=ConnectionError("refused")))
    assert st == md.PROBE_UNMEASURED


def test_the_engine_call_carries_the_probe_bound_and_keeps_its_own_otherwise(monkeypatch):
    from modea import inference
    seen = []

    class _R:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": "ready", "prompt_eval_count": 3, "eval_count": 1}

    def post(url, json=None, timeout=None, **k):  # noqa: A002 -- the keyword requests uses
        seen.append(timeout)
        return _R()
    monkeypatch.setattr(inference, "requests", types.SimpleNamespace(post=post))
    monkeypatch.setenv("OLLAMA_TIMEOUT", "600")
    b = inference.OllamaBackend(model="m", endpoint="http://e")
    assert b.generate("x", timeout_s=42) == "ready"
    b.generate("x")
    o = inference.OpenAIBackend(model="m", endpoint="http://e")
    seen_o = len(seen)

    class _RO(_R):
        def json(self):
            return {"choices": [{"message": {"content": "ready"}}], "usage": {}}
    monkeypatch.setattr(inference, "requests", types.SimpleNamespace(post=lambda url, **k: (seen.append(k.get("timeout")), _RO())[1]))
    o.generate("x", timeout_s=7)
    o.generate("x")
    assert seen[:seen_o] == [42, 600] and seen[seen_o:] == [7, 600], seen
    assert inference.MockBackend().generate("x", timeout_s=1)


def test_an_answer_past_the_bound_is_not_an_answer():
    ticks = iter([100.0, 100.0 + 31.0])
    st, why, secs = md.presence_probe(_Engine("ready"), bound_s=30, clock=lambda: next(ticks))
    assert (st, secs) == (md.PROBE_UNMEASURED, 31.0) and "past the 30 s bound" in why


def test_the_probe_bound_is_read_and_a_bad_value_is_said_once(monkeypatch, capsys):
    monkeypatch.setattr(md, "_PROBE_STATE", {"failed_at": 0.0, "said_for": "", "bad_bound_said": False})
    monkeypatch.delenv("DENDRA_PRESENCE_PROBE_S", raising=False)
    assert md.presence_probe_bound_s() == md.PRESENCE_PROBE_S_DEFAULT
    monkeypatch.setenv("DENDRA_PRESENCE_PROBE_S", "45")
    assert md.presence_probe_bound_s() == 45.0
    for bad in ("abc", "-3", "0"):
        monkeypatch.setenv("DENDRA_PRESENCE_PROBE_S", bad)
        assert md.presence_probe_bound_s() == md.PRESENCE_PROBE_S_DEFAULT
    assert capsys.readouterr().out.count("is not a positive number of seconds") == 1


def test_no_proof_is_sent_when_the_served_model_does_not_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    for st, why in ((md.PROBE_SILENT, "the served model returned an empty answer"),
                    (md.PROBE_UNMEASURED, "no answer within 120 s (ReadTimeout)")):
        got, sent = _avail(monkeypatch, tmp_path, 1010, probe=lambda st=st, why=why: (st, why, 3.0))
        assert got == "" and sent == [], "a model absent or mute proves no presence: nothing is sent"
        p = json.loads((tmp_path / "status.json").read_text())["presence"]
        assert (p["result"], p["probe"], p["why"]) == ("model did not answer", st, why)
        assert not (tmp_path / md.AVAIL_RECORD).exists(), "the challenge stays unsettled, the next tick asks again"
    out = capsys.readouterr().out
    assert "availability NOT proven" in out and "PROVEN (VRF)" not in out


def test_without_a_probe_nothing_is_sent_and_a_probe_that_raises_is_not_an_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    got, sent = _avail(monkeypatch, tmp_path, 1010, probe=None)
    assert sent == [] and json.loads((tmp_path / "status.json").read_text())["presence"]["probe"] == md.PROBE_UNMEASURED

    def boom():
        raise RuntimeError("bug")
    got, sent = _avail(monkeypatch, tmp_path, 1010, probe=boom)
    assert sent == []
    got, sent = _avail(monkeypatch, tmp_path, 1010, probe=lambda: ("yes", "", 0.1))
    assert sent == [], "a state that is not one of the three is not an answer"


def test_a_probe_that_did_not_answer_waits_before_the_next_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    now = [1000.0]
    monkeypatch.setattr(md.time, "time", lambda: now[0])
    asked = []

    def probe_silent():
        asked.append("silent")
        return md.PROBE_SILENT, "the served model returned an empty answer", 0.5
    # The first call sets the fakes (chain, transaction) and leaves the backoff of a probe that did not answer.
    got, sent = _avail(monkeypatch, tmp_path, 1010, probe=probe_silent)
    assert sent == [] and asked == ["silent"]

    def probe_ok():
        asked.append("ok")
        return md.PROBE_ANSWERED, "", 0.4
    now[0] += md.PRESENCE_PROBE_RETRY_S - 1
    got = md.prove_availability_once("dm1bench", "a" * 128, "", keydir=str(tmp_path), probe=probe_ok)
    assert asked == ["silent"] and got == "" and sent == [], "within the wait, the engine is not asked again"
    now[0] += 2
    got = md.prove_availability_once("dm1bench", "a" * 128, "", keydir=str(tmp_path), probe=probe_ok)
    assert asked == ["silent", "ok"] and got == "c" * 64 and len(sent) == 1, "past the wait, it answers and proves"
    out = capsys.readouterr().out
    assert out.count("availability NOT proven") == 1 and "answered a test request in 0.4 s" in out


def test_the_daemon_loop_hands_the_served_backend_to_the_probe():
    # FORM, declared as such: the loop is not driven here (main() needs the whole chain). The proof call of the
    # loop must carry the probe of the backend the miner serves with, or every proof would go out unprobed.
    import inspect
    src = inspect.getsource(md.main)
    assert "probe=lambda: presence_probe(miner.backend)" in src


# ── the IDENTITY MISMATCH way out names the identity's own slot ──────────────────────────────────────
def test_the_identity_mismatch_way_out_names_the_slot():
    s0 = md.identity_mismatch_text("dm1a", "dendra1op", "dendra1me", "0", creator="dendra1me", region="eu")
    assert "dendra-miner_miner-keys" in s0
    assert md.identity_mismatch_text("dm1a", "dendra1op", "dendra1me", None, creator="dendra1me",
                                     region="eu") == s0, "no slot = the kit's own"
    s2 = md.identity_mismatch_text("dm1a", "dendra1op", "dendra1me", "2")
    assert "dendra-miner-g2_miner-keys" in s2 and "dendra-miner_miner-keys" not in s2
    sx = md.identity_mismatch_text("dm1a", "dendra1op", "dendra1me", "02")
    assert "miner-g" not in sx and "is not known here" in sx and "slots.sh list" in sx
    for t in (s0, s2, sx):
        assert t.startswith("[daemon] IDENTITY MISMATCH: 'dm1a'") and "restore the original key of dendra1op" in t
        # A NEW IDENTIFIER IS NO WAY OUT (the chain derives it from the key): never offered as one.
        assert "--id <new-id>" not in t and "MINER_ID=<new-id>" not in t and "A NEW identifier is NOT a way out" in t
        assert "dendrad tx jobs update-miner dm1a dendra1me " in t
    assert "update-miner dm1a dendra1me eu 0" in s0 and "This machine can sign it" not in s2
    assert "That creator is THIS machine's key" in s0, "the creator read on chain is this machine's key: said"


def test_the_identity_mismatch_is_in_the_heartbeat_and_clears(tmp_path, monkeypatch, capsys):
    status = tmp_path / "status.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(status))
    md._STATUS.clear()
    chain = {"op": "dendra1other"}
    monkeypatch.setattr(md, "miner_operator", lambda mid: chain["op"])
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.PRESENT, {"creator": ADDR, "operator": chain["op"],
                                                                          "region": "eu", "stake": 1}))
    st = md.check_operator("dm1bench", ADDR, "0")
    out = capsys.readouterr().out
    assert st == "mismatch" and out.count("IDENTITY MISMATCH") == 1
    hb = json.loads(status.read_text())
    assert hb["identity_mismatch"] == {"operator": "dendra1other", "address": ADDR}, hb
    if os.environ.get("K1_HB_OUT"):
        Path(os.environ["K1_HB_OUT"]).write_text(status.read_text(), encoding="utf-8")
    # Read again while it holds: nothing said twice.
    assert md.check_operator("dm1bench", ADDR, "0", st) == "mismatch" and capsys.readouterr().out == ""
    # Not read (the registry does not answer): nothing claimed, the state said stays.
    chain["op"] = ""
    assert md.check_operator("dm1bench", ADDR, "0", "mismatch") == "mismatch"
    assert json.loads(status.read_text())["identity_mismatch"]["operator"] == "dendra1other"
    # Fixed on chain (update-miner): cleared from the heartbeat, and said.
    chain["op"] = ADDR
    assert md.check_operator("dm1bench", ADDR, "0", "mismatch") == "ok"
    assert json.loads(status.read_text())["identity_mismatch"] is None
    said = capsys.readouterr().out
    assert "records this machine's key" in said
    for w in FAILURE_WORDS:
        assert w not in said, (w, said)


def test_the_lines_of_a_miner_not_registered_yet_carry_no_registration_word(tmp_path, monkeypatch, capsys):
    """The lines the daemon prints while it is NOT registered -- the stake warning of _registration_stake, the
    deferred line, the next-attempt line, and a failed attempt followed by the chain's own usage text -- written
    to $K1_PENDING_OUT / $K1_FAILED_OUT for the shell bench, which feeds them to the SHIPPED wait_healthy: its
    registration words are join.sh's, so the judgement lives there; this test only produces the lines."""
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    monkeypatch.setattr(md, "chain_min_stake", lambda default=50000: 5_000_000)
    monkeypatch.setenv("DENDRA_MINER_STAKE", "1000")
    assert md._registration_stake() == 5_000_000
    print(md.REGISTRATION_DEFERRED_LINE.format(reason="ip_quota"), flush=True)
    print(md.not_registered_line("dm1bench", "chain_refused", 600), flush=True)
    pending = capsys.readouterr().out
    assert "create-miner would be REJECTED" in pending, "the warning names create-miner: the case the bench needs"
    # A failed attempt: no account funded, the chain answers with its usage text, which names create-miner.
    usage = ("Error: accepts 5 arg(s), received 4" + NL + "Usage:" + NL +
             "  dendrad tx jobs create-miner [miner_id] [operator] [region] [stake] [enc_pubkey] [flags]")
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.ABSENT, {}))
    monkeypatch.setattr(md, "faucet_fund_classified", lambda url, addr: (True, "", ""))
    monkeypatch.setattr(md, "bal_token", lambda addr: 1)
    monkeypatch.setattr(md, "tx_from", lambda *a, **k: usage)
    monkeypatch.setattr(md, "wait_tx", lambda out, timeout=24: out)
    monkeypatch.setattr(md.time, "sleep", lambda s: None)
    a = types.SimpleNamespace(id="dm1bench", faucet="http://f", keydir=str(tmp_path))
    assert md.register_attempt(a, ADDR, "ab" * 32, "cd" * 32) == (md.REFUSED, "chain_refused")
    failed = capsys.readouterr().out
    assert "ON-CHAIN REGISTRATION FAILED" in failed and "create-miner [miner_id]" in failed
    if os.environ.get("K1_PENDING_OUT"):
        Path(os.environ["K1_PENDING_OUT"]).write_text(pending, encoding="utf-8")
    if os.environ.get("K1_FAILED_OUT"):
        Path(os.environ["K1_FAILED_OUT"]).write_text(failed, encoding="utf-8")


# ── the address, read from the keyring, never from a log ─────────────────────────────────────────────
def test_the_keyring_reports_address_and_keys_at_rest(tmp_path, monkeypatch, capsys):
    _, keys = fake_dendrad(tmp_path, monkeypatch)
    keydir = tmp_path / "volume"
    cosmos = keydir / "cosmos"
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(cosmos))
    (keydir).mkdir()
    # No identity yet: nothing to read an address for -- 3, and the keys at rest said ("none").
    assert kring.address_report(str(keydir)) == 3
    out = capsys.readouterr().out
    assert "at_rest=none" in out and "address=" + NL in out + NL
    (keydir / "identite-resolue").write_text("dm1bench" + NL)
    (cosmos / "keyring-test").mkdir(parents=True)
    (cosmos / "keyring-test" / "dm1bench.info").write_text("x")
    keys.write_text("dm1bench=" + ADDR + NL)
    assert kring.address_report(str(keydir)) == 0
    out = capsys.readouterr().out.splitlines()
    assert "identity=dm1bench" in out and "address=" + ADDR in out and "at_rest=clear" in out, out
    # The keyring the DISK holds decides, never a setting: a passphrase file next to a `test` keyring leaves
    # the keys in clear, and says so.
    pp = tmp_path / "passphrase"
    pp.write_text("a-passphrase-long-enough")
    monkeypatch.setenv("DENDRA_KEYRING_PASSPHRASE_FILE", str(pp))
    kring.address_report(str(keydir))
    assert "at_rest=clear" in capsys.readouterr().out.splitlines()
    # Encrypted on the disk: encrypted.
    (cosmos / "keyring-test" / "dm1bench.info").rename(tmp_path / "moved.info")
    (cosmos / "keyring-file").mkdir()
    (cosmos / "keyring-file" / "dm1bench.info").write_text("x")
    kring.address_report(str(keydir))
    assert "at_rest=encrypted" in capsys.readouterr().out.splitlines()
