"""The miner daemon keeps the recovery phrase of a key it creates, creates a key ONLY when it is absent, and
claims its subsidy by itself.

Pinned: the phrase is written only for a key CREATED here (never invented for an existing one), with mode
0600, and SEALED under the keyring's passphrase when the keyring is encrypted; `keys_addr` creates a key
only when dendrad says it is absent WITHOUT a passphrase refusal AND the disk holds no `<name>.info` -- a
missing or wrong passphrase raises and creates nothing; the claimable amount follows the chain's cap with
the rule of zero (absent = 0 inside an answer that was read, unreadable = no claim); nothing is sent below
the threshold; the payout address is declared once registered and again only when it changes.

`dendrad` is a fake on a HERMETIC PATH (that directory, with the interpreter its shebang names): it answers
from the case's state and records each call's argv and how many lines it was handed on stdin.
The subject can be pointed at a mutated copy with DENDRA_DAEMON_FILE."""
import importlib.util
import json
import os
import stat
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    D = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = D
    _spec.loader.exec_module(D)
else:
    import miner as D  # noqa: E402

from modea import crypto  # noqa: E402
from modea import keyring as K  # noqa: E402

MN = " ".join(["word"] * 23 + ["last"])
CREATED = ("some warning on stderr\n"
           + json.dumps({"name": "m1", "type": "local", "address": "dendra1abc", "mnemonic": MN}))
PASS = "p" * 64

FAKE_DENDRAD = r'''#!/usr/bin/env python3
import json, os, sys
S = os.environ["FAKE_STATE"]
a = sys.argv[1:]
given = sys.stdin.read()
with open(os.path.join(S, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": a, "stdin_lines": given.count(chr(10)), "stdin": given}) + chr(10))
mode = open(os.path.join(S, "mode")).read().strip()
name = a[2] if len(a) > 2 else ""
if a[:2] == ["keys", "show"]:
    if mode == "mute_after_add" and os.path.exists(os.path.join(S, "made")):
        print("Error: rpc error: the keyring did not answer", file=sys.stderr)
        sys.exit(1)
    if mode == "refused":
        print("EOF" + chr(10) + "EOF" + chr(10) + "EOF", file=sys.stderr)
        print(name + " is not a valid name or address: too many failed passphrase attempts", file=sys.stderr)
        sys.exit(1)
    if mode == "present" or os.path.exists(os.path.join(S, "made")):
        print("dendra1abc")
        sys.exit(0)
    print(name + " is not a valid name or address: decoding bech32 failed: invalid bech32 string length 2", file=sys.stderr)
    sys.exit(1)
if a[:2] == ["keys", "add"]:
    open(os.path.join(S, "made"), "w").write("1")
    print(json.dumps({"name": name, "type": "local", "address": "dendra1abc", "mnemonic": "%s"}))
    sys.exit(0)
print("unexpected " + " ".join(a), file=sys.stderr)
sys.exit(64)
''' % MN


class Bench:
    """A keyring directory, a passphrase file (absent unless asked), and the fake dendrad on PATH."""

    def __init__(self, tmp, monkeypatch, mode, backend=None, on_disk=(), passphrase=False):
        self.tmp, self.state = tmp, tmp / "state"
        self.state.mkdir()
        (self.state / "mode").write_text(mode)
        self.kdir = tmp / "kr"
        self.keydir = tmp / "keys"
        self.keydir.mkdir()
        if backend:
            d = self.kdir / K.DIRS[backend]
            d.mkdir(parents=True)
            if backend == K.FILE:
                (d / "keyhash").write_text("h")
            for n in on_disk:
                (d / f"{n}.info").write_text("x")
        self.pf = tmp / "keyring-passphrase"
        if passphrase:
            self.pf.write_text(PASS + "\n")
        b = tmp / "bin"
        b.mkdir()
        (b / "dendrad").write_text(FAKE_DENDRAD)
        os.chmod(b / "dendrad", 0o755)
        os.symlink(sys.executable, b / "python3")
        monkeypatch.setenv("PATH", str(b))
        monkeypatch.setenv("FAKE_STATE", str(self.state))
        monkeypatch.setenv("DENDRA_KEYRING_PASSPHRASE_FILE", str(self.pf))
        monkeypatch.setattr(D, "KEYRING_DIR", str(self.kdir))
        monkeypatch.setattr(D, "_KR", None)

    def calls(self):
        p = self.state / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def adds(self):
        return [c for c in self.calls() if c["argv"][:2] == ["keys", "add"]]


def test_phrase_of_a_created_key_is_kept_private(tmp_path):
    assert D.keep_recovery_phrase(str(tmp_path), CREATED) is True
    p = tmp_path / D.RECOVERY_FILE
    d = json.loads(p.read_text())
    assert d["mnemonic"] == MN and d["address"] == "dendra1abc"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_with_an_encrypted_keyring_the_phrase_is_NEVER_written_in_clear(tmp_path):
    # With an encrypted keyring the phrase is sealed under the same passphrase, written straight into its
    # envelope: no clear copy, not even a temporary one, ever reaches the disk.
    seen = []
    real = crypto.write_private

    def spy(path, data):
        seen.append(bytes(data))
        return real(path, data)
    crypto.write_private = spy
    try:
        assert D.keep_recovery_phrase(str(tmp_path), CREATED, seal_with=PASS) is True
    finally:
        crypto.write_private = real
    p = tmp_path / D.RECOVERY_FILE
    raw = p.read_bytes()
    assert raw.startswith(crypto.MAGIC) and b"word" not in raw
    assert seen and all(b"word" not in s for s in seen), "a clear copy was handed to the disk"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # ... and the application opens it, inside the container, with the keyring's passphrase
    assert K.read_recovery(str(p), PASS)["mnemonic"] == MN
    assert K.recovery_head(str(p), PASS) == {"present": True, "address": "dendra1abc", "words": 24, "sealed": True}
    assert K.recovery_head(str(p), "")["present"] is None, "sealed and no passphrase: not read, never absent"


def test_no_phrase_no_file(tmp_path):
    assert D.keep_recovery_phrase(str(tmp_path), "Error: key already exists") is False
    assert D.keep_recovery_phrase(str(tmp_path), json.dumps({"mnemonic": "too short"})) is False
    assert D.keep_recovery_phrase("", CREATED) is False
    assert not (tmp_path / D.RECOVERY_FILE).exists()


def test_existing_key_writes_nothing(tmp_path, monkeypatch):
    b = Bench(tmp_path, monkeypatch, "present", backend=K.TEST, on_disk=("m1",))
    assert D.keys_addr("m1", str(b.keydir)) == "dendra1abc"
    assert b.adds() == []
    assert not (b.keydir / D.RECOVERY_FILE).exists()


def test_created_key_keeps_its_phrase(tmp_path, monkeypatch):
    # A key that is really absent -- dendrad says so without a passphrase refusal, and the disk agrees --
    # is created, and its phrase kept.
    b = Bench(tmp_path, monkeypatch, "absent")
    assert D.keys_addr("m1", str(b.keydir)) == "dendra1abc"
    assert len(b.adds()) == 1 and "--output" in b.adds()[0]["argv"]
    assert (b.keydir / D.RECOVERY_FILE).exists()


def test_a_REFUSED_passphrase_never_creates_a_key(tmp_path, monkeypatch):
    # dendrad answers a missing or wrong passphrase with "not a valid name or address": the words of an
    # absent key. Creating one would be a new identity, in silence, with the stake left under the old one.
    import pytest
    b = Bench(tmp_path, monkeypatch, "refused", backend=K.FILE, on_disk=("m1",), passphrase=True)
    with pytest.raises(K.KeyringError) as e:
        D.keys_addr("m1", str(b.keydir))
    assert "passphrase is missing or wrong" in str(e.value)
    assert b.adds() == []
    # the passphrase went on STDIN, one line per prompt, and never into argv
    show = [c for c in b.calls() if c["argv"][:2] == ["keys", "show"]]
    assert show and show[0]["stdin"] == (PASS + "\n") * K.STDIN_COPIES
    assert all(PASS not in " ".join(c["argv"]) for c in b.calls())
    assert ["--keyring-backend", "file"] == show[0]["argv"][show[0]["argv"].index("--keyring-backend"):][:2]


def test_a_key_on_disk_that_dendrad_does_not_read_is_never_recreated(tmp_path, monkeypatch):
    # dendrad says "absent" with no passphrase refusal, but the disk holds <name>.info: the two readings
    # disagree, so the key is UNREADABLE -- never absent, never recreated.
    import pytest
    b = Bench(tmp_path, monkeypatch, "absent", backend=K.TEST, on_disk=("m1",))
    with pytest.raises(K.KeyringUnreadable):
        D.keys_addr("m1", str(b.keydir))
    assert b.adds() == []


def test_an_encrypted_keyring_without_its_passphrase_is_never_read_as_test(tmp_path, monkeypatch):
    # No passphrase file and keyring-file holds keys: a named refusal, before dendrad is even called. A
    # fallback to the test keyring would find no key there and create one.
    import pytest
    b = Bench(tmp_path, monkeypatch, "absent", backend=K.FILE, on_disk=("m1",), passphrase=False)
    with pytest.raises(K.PassphraseMissing):
        D.keys_addr("m1", str(b.keydir))
    assert b.calls() == []


def test_an_earlier_unconfirmed_phrase_blocks_a_creation_and_is_kept(tmp_path, monkeypatch):
    # The phrase file has ONE name. A key created while an earlier key's phrase is still kept -- never
    # confirmed as written down -- would replace the only copy of words whose key may hold the stake.
    import pytest
    b = Bench(tmp_path, monkeypatch, "absent")
    kept = b.keydir / D.RECOVERY_FILE
    kept.write_text(json.dumps({"address": "dendra1earlier", "name": "dm1old", "mnemonic": " ".join(["old"] * 24)}))
    before = kept.read_bytes()
    with pytest.raises(K.RecoveryPending) as e:
        D.keys_addr("m1", str(b.keydir))
    assert "dendra1earlier" in str(e.value) and "No new key was created" in str(e.value)
    assert b.adds() == [], "no key was created"
    assert kept.read_bytes() == before, "the earlier phrase is untouched"


def test_keeping_a_phrase_never_replaces_an_existing_file(tmp_path):
    # The last line of the same rule, under keys_addr: the file is linked into place, and a link refuses an
    # existing name.
    kept = tmp_path / D.RECOVERY_FILE
    kept.write_text(json.dumps({"address": "dendra1earlier", "mnemonic": " ".join(["old"] * 24)}))
    before = kept.read_bytes()
    assert D.keep_recovery_phrase(str(tmp_path), CREATED) is False
    assert kept.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [D.RECOVERY_FILE], "no temporary file is left behind"


def test_the_words_of_a_created_key_never_reach_the_stop_message_or_the_heartbeat(tmp_path, monkeypatch, capsys):
    # `keys add` succeeds and prints the JSON whose LAST field is the mnemonic; the read-back then fails. The
    # message that stops the daemon goes to `docker logs`, the application's log view and the heartbeat: it
    # must carry no word of the phrase.
    import pytest
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    b = Bench(tmp_path, monkeypatch, "mute_after_add")
    with pytest.raises(K.KeyringError) as e:
        D.keys_addr("m1", str(b.keydir))
    assert len(b.adds()) == 1
    assert "word word" not in str(e.value) and "mnemonic" not in str(e.value)
    with pytest.raises(SystemExit):
        D._stop_on_keyring(e.value)
    printed = capsys.readouterr().out
    assert "KEYS NOT OPENED" in printed and "word word" not in printed and "mnemonic" not in printed
    hb = (tmp_path / "status.json").read_text()
    assert "keys_error" in hb and "word word" not in hb and "mnemonic" not in hb


def test_a_new_miner_with_a_passphrase_file_is_born_encrypted(tmp_path, monkeypatch):
    # Nothing on the disk yet, the passphrase file is there: the keyring is `file`, the key is created
    # with the passphrase on stdin, twice (dendrad asks to re-enter it on a keyring that has none yet),
    # and its recovery phrase is sealed.
    b = Bench(tmp_path, monkeypatch, "absent", passphrase=True)
    assert D.keys_addr("m1", str(b.keydir)) == "dendra1abc"
    add = b.adds()[0]
    assert add["argv"][add["argv"].index("--keyring-backend") + 1] == "file" and add["stdin_lines"] == 2
    assert (b.keydir / D.RECOVERY_FILE).read_bytes().startswith(crypto.MAGIC)


FAKE_VRF = r'''#!/usr/bin/env python3
import os, sys
if sys.argv[1:2] == ["keygen"]:
    print("a" * 128 + chr(9) + "b" * 64); sys.exit(0)
if sys.argv[1:2] == ["pubkey"] and os.environ.get("DENDRA_VRF_SK") == "a" * 128:
    print("b" * 64); sys.exit(0)
sys.exit(1)
'''


def test_the_vrf_secret_is_born_sealed_0600_and_a_clear_one_is_sealed_in_place(tmp_path, monkeypatch):
    # It was written under the process umask and chmod'ed afterwards: readable by others for an instant,
    # and in clear whatever the keyring. With an encrypted keyring it is sealed, 0600 from its creation.
    b = Bench(tmp_path, monkeypatch, "absent", passphrase=True)
    (b.tmp / "bin" / "dendra-vrf").write_text(FAKE_VRF)
    os.chmod(b.tmp / "bin" / "dendra-vrf", 0o755)
    monkeypatch.setattr(D, "_vrf_bin", lambda: str(b.tmp / "bin" / "dendra-vrf"))
    old = os.umask(0o022)
    try:
        assert D.vrf_identity(str(b.keydir), "dm1abc") == ("a" * 128, "b" * 64)
    finally:
        os.umask(old)
    v = b.keydir / "dm1abc.vrf"
    assert v.read_bytes().startswith(crypto.MAGIC) and b"aaaa" not in v.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(v).st_mode) == 0o600
    assert D.vrf_identity(str(b.keydir), "dm1abc") == ("a" * 128, "b" * 64), "read back through the envelope"
    # a secret left in clear by an older daemon is sealed at the next start, same key
    v.write_text("a" * 128)
    assert D.vrf_identity(str(b.keydir), "dm1abc") == ("a" * 128, "b" * 64)
    assert v.read_bytes().startswith(crypto.MAGIC)
    # sealed, and the passphrase gone: a NAMED failure, never a VRF key silently treated as missing
    import pytest
    b.pf.unlink()
    monkeypatch.setattr(D, "_KR", None)
    (b.kdir / "keyring-file").mkdir(parents=True, exist_ok=True)
    (b.kdir / "keyring-file" / "keyhash").write_text("h")
    with pytest.raises(crypto.KeyEnvelopeError):
        D.vrf_identity(str(b.keydir), "dm1abc")


def _q(miner, params):
    def query(sub, *pos, flags=()):
        if sub == "get-miner":
            return miner if isinstance(miner, str) else json.dumps(miner)
        return params if isinstance(params, str) else json.dumps(params)
    return query


def test_claimable_follows_the_chain_cap(monkeypatch):
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "1000000", "subsidy_claimed": "100000"}},
                                       {"params": {"work_gate_bps": "5000"}}))
    assert D.claimable_subsidy("m1") == 400000


def test_absent_fields_are_zero_inside_a_read_answer(monkeypatch):
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "1000000"}}, {"params": {}}))
    assert D.claimable_subsidy("m1") == 0


def test_unreadable_is_none_and_claims_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "query", _q("Error: connection refused", {"params": {"work_gate_bps": "5000"}}))
    monkeypatch.setattr(D, "tx_from", lambda *a, **k: sent.append(a) or "")
    assert D.claimable_subsidy("m1") is None
    assert "not read" in D.maybe_claim_subsidy("m1") and not sent


def test_below_threshold_nothing_is_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "10000"}}, {"params": {"work_gate_bps": "5000"}}))
    monkeypatch.setattr(D, "tx_from", lambda *a, **k: sent.append(a) or "")
    assert "below" in D.maybe_claim_subsidy("m1") and not sent


def test_above_threshold_one_claim(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "query", _q({"miner": {"demand": "2000000"}}, {"params": {"work_gate_bps": "5000"}}))
    monkeypatch.setattr(D, "tx_from", lambda *a, **k: sent.append(a) or "{}")
    monkeypatch.setattr(D, "wait_tx", lambda o, timeout=24: True)
    out = D.maybe_claim_subsidy("m1")
    assert sent == [("m1", "claim-subsidy", "m1")] and "confirmed" in out


# ── the Final Testnet Season payout address (DENDRA_PAYOUT_ADDRESS) ──────────────────────────────────
def _cold():
    from modea import cosmos_addr as ca
    return ca.bech32_encode("dendra", ca._convertbits(bytes(range(20)), 8, 5))


def _payout_bench(monkeypatch, addr, answers):
    import final_season_miner as fsm
    sent = []

    def declare(base, mid, address, sign=None):
        sent.append((base, mid, address))
        return answers.pop(0) if answers else (200, {"ok": True})
    monkeypatch.setattr(fsm, "declare_payout", declare)
    monkeypatch.setattr(D, "PAYOUT_ADDRESS", addr)
    monkeypatch.setattr(D, "PROGRAMME", "http://programme.example/final-season/v1")
    return sent


def test_the_payout_address_is_declared_once_then_only_when_it_changes(tmp_path, monkeypatch):
    cold = _cold()
    sent = _payout_bench(monkeypatch, cold, [])
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert settled and sent == [("http://programme.example/final-season/v1", "dm1abc", cold)] and "declared" in msg
    rec = K.payout_record(str(tmp_path))
    assert rec["address"] == cold and rec["setting"] == cold and rec["source"] == "daemon"
    # the same setting again: nothing is sent (the programme takes one declaration per 720 blocks)
    assert D.maybe_declare_payout(str(tmp_path), "dm1abc")[0] and len(sent) == 1
    # a declaration made since from the application keeps the setting: not declared over it
    import final_season_miner as fsm
    fsm.record_declaration(str(tmp_path), "dm1abc", "dendra1fromtheapp", source="command")
    assert D.maybe_declare_payout(str(tmp_path), "dm1abc")[0] and len(sent) == 1
    # the setting CHANGES: declared again
    from modea import cosmos_addr as ca
    other = ca.bech32_encode("dendra", ca._convertbits(bytes(range(1, 21)), 8, 5))
    monkeypatch.setattr(D, "PAYOUT_ADDRESS", other)
    assert D.maybe_declare_payout(str(tmp_path), "dm1abc")[0] and sent[-1][2] == other and len(sent) == 2


def test_an_address_one_character_off_is_never_declared(tmp_path, monkeypatch):
    cold = _cold()
    off = cold[:-1] + ("q" if cold[-1] != "q" else "p")
    sent = _payout_bench(monkeypatch, off, [])
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc")
    assert sent == [] and "NOT declared" in msg and K.payout_record(str(tmp_path)) is None


def test_a_refused_declaration_is_retried_and_records_nothing(tmp_path, monkeypatch):
    sent = _payout_bench(monkeypatch, _cold(), [(0, {"error": "the service's height is unknown"})])
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc")
    assert settled is False and len(sent) == 1 and "retried" in msg
    assert K.payout_record(str(tmp_path)) is None
    assert D.maybe_declare_payout(str(tmp_path), "dm1abc")[0] is True and len(sent) == 2
