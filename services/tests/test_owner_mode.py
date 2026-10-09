"""Owner mode (deploy/join.sh --owner): the miner's stake on a key that is not on the mining machine.

Pinned here, each half with its counterpart:
  * the daemon NEVER signs create-miner with this machine's key in owner mode: it simulates and prepares the
    registration from the OWNER's address, prints the three commands complete (the identifier derived from
    the owner, this machine's key as operator), and mines only once the chain records this machine's key as
    the operator -- naming both addresses when another one is recorded. Without the setting, it registers
    itself exactly as before;
  * the owner is decided BEFORE any rename, and the registry decides when the volume and the setting
    disagree; an unread registry never renames;
  * the season server accepts a payout declaration from the OWNER only when owner and operator differ, and
    from the one key as before when they coincide; a registry read with no creator attributes to nobody;
  * the registry cache reads creators from the same read as operators, and an absent creator is unknown,
    never the operator;
  * payout-prepare / payout-submit: the document the owner signs is the one a deposit signs, a REAL
    secp256k1 signature over it is accepted by the real server, and a file signed by another key, or over
    another height, is refused before anything is sent.

`dendrad` is a fake on a HERMETIC PATH that answers from the case's state and records every argv; the
signatures of the season are real (`cryptography`, the scheme `relay_carrier` verifies). The daemon and the
season server can be pointed at mutated copies with DENDRA_DAEMON_FILE and DENDRA_SEASON_SERVER_FILE.
"""
import base64
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import threading
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pytest  # noqa: E402


def _load(env, name):
    path = os.environ.get(env)
    if not path:
        return importlib.import_module(name)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


D = _load("DENDRA_DAEMON_FILE", "miner")

from modea import cosmos_addr, owner_tx, registry_cache, relay_canon, relay_carrier  # noqa: E402
from modea import relay_signature as rs  # noqa: E402
from modea.miner_id import miner_id_for_account  # noqa: E402


def _addr(first):
    return cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes(range(first, first + 20)), 8, 5))


COLD, HOT, OTHER = _addr(1), _addr(101), _addr(201)
OWNED_ID = miner_id_for_account(COLD)
SELF_ID = miner_id_for_account(HOT)
ENC = "ab" * 32

FAKE_DENDRAD = r'''#!/usr/bin/env python3
import json, os, sys
S = os.environ["FAKE_STATE"]
a = sys.argv[1:]
sys.stdin.read()
with open(os.path.join(S, "calls.jsonl"), "a") as f:
    f.write(json.dumps(a) + chr(10))
def load(n, d):
    p = os.path.join(S, n)
    return json.load(open(p)) if os.path.exists(p) else d
def save(n, v):
    json.dump(v, open(os.path.join(S, n), "w"))
def flag(n):
    return a[a.index(n) + 1] if n in a else ""
pos = a[a.index("--") + 1:] if "--" in a else []
keys = load("keys.json", {})
kdir = os.path.join(os.environ["FAKE_KDIR"], "keyring-test")
if a[:2] == ["keys", "show"]:
    if a[2] in keys:
        print(keys[a[2]]); sys.exit(0)
    print("Error: " + a[2] + " is not a valid name or address: key not found", file=sys.stderr); sys.exit(1)
if a[:2] == ["keys", "add"]:
    keys[a[2]] = os.environ["FAKE_HOT"]; save("keys.json", keys)
    os.makedirs(kdir, exist_ok=True); open(os.path.join(kdir, a[2] + ".info"), "w").write("x")
    print(json.dumps({"name": a[2], "type": "local", "address": keys[a[2]], "mnemonic": " ".join(["word"] * 24)}))
    sys.exit(0)
if a[:2] == ["keys", "rename"]:
    keys[a[3]] = keys.pop(a[2]); save("keys.json", keys)
    os.replace(os.path.join(kdir, a[2] + ".info"), os.path.join(kdir, a[3] + ".info")); sys.exit(0)
if a[:3] == ["query", "jobs", "get-miner"]:
    reg = load("registry.json", {})
    mid = pos[0] if pos else a[3]
    if mid not in reg:
        print("Error: rpc error: code = NotFound desc = rpc error: code = NotFound desc = not found: key not found", file=sys.stderr)
        sys.exit(1)
    if flag("--output") == "json":
        print(json.dumps({"miner": reg[mid]}))
    else:
        print("miner:" + chr(10) + "".join("  %s: %s%s" % (k, json.dumps(v), chr(10)) for k, v in reg[mid].items()))
    sys.exit(0)
if a[:2] == ["query", "tx"]:
    print("code: 0" + chr(10) + 'height: "5"'); sys.exit(0)
if a[:3] == ["query", "jobs", "params"]:
    print("params:" + chr(10) + '  min_stake: "1000"'); sys.exit(0)
if a[:3] == ["query", "bank", "balances"]:
    b = load("balances.json", {}).get(a[3], 0)
    print(json.dumps({"balances": [{"denom": "udndr", "amount": str(b)}] if b else [], "pagination": {}})); sys.exit(0)
if a[:3] == ["query", "auth", "account"]:
    if not load("balances.json", {}).get(a[3], 0):
        print("Error: rpc error: code = NotFound desc = account " + a[3] + " not found: key not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"account": {"type": "/cosmos.auth.v1beta1.BaseAccount",
                                  "value": {"address": a[3], "account_number": "7"}}}))
    sys.exit(0)
if a[:2] == ["tx", "jobs"]:
    sub = a[2]
    if "--dry-run" in a:
        print("gas estimate: 92393", file=sys.stderr); sys.exit(0)
    if "--generate-only" in a:
        msg = {"@type": "/dendra.jobs.v1.Msg" + "".join(w.capitalize() for w in sub.split("-")), "creator": flag("--from")}
        if sub == "create-miner":
            msg.update(miner_id=pos[0], operator=pos[1], region=pos[2], stake=pos[3], enc_pubkey=pos[4])
            if flag("--vrf-pubkey"):
                msg["vrf_pubkey"] = flag("--vrf-pubkey")
        elif sub == "update-miner":
            msg.update(miner_id=pos[0], operator=pos[1], region=pos[2], stake=pos[3])
        else:
            msg.update(miner_id=pos[0])
        print(json.dumps({"body": {"messages": [msg], "memo": ""},
                          "auth_info": {"signer_infos": [], "fee": {"gas_limit": flag("--gas")}}, "signatures": []}))
        sys.exit(0)
    with open(os.path.join(S, "SIGNED"), "a") as f:
        f.write(json.dumps(a) + chr(10))
    if sub == "create-miner":
        reg = load("registry.json", {})
        reg[pos[0]] = {"miner_id": pos[0], "creator": keys.get(flag("--from"), flag("--from")),
                       "operator": pos[1], "region": pos[2], "stake": pos[3]}
        save("registry.json", reg)
    print("code: 0" + chr(10) + "txhash: " + "AB" * 32); sys.exit(0)
print("unexpected " + " ".join(a), file=sys.stderr)
sys.exit(64)
'''


class World:
    """A keyring directory, a key directory, the fake dendrad on a hermetic PATH, and the chain's state."""

    def __init__(self, tmp, monkeypatch, *, owner=COLD, registry=None, balances=None):
        self.tmp, self.state = tmp, tmp / "state"
        self.state.mkdir()
        self.kdir, self.keydir = tmp / "kr", tmp / "keys"
        self.kdir.mkdir()
        self.keydir.mkdir()
        (self.state / "registry.json").write_text(json.dumps(registry or {}))
        (self.state / "balances.json").write_text(json.dumps(balances or {}))
        b = tmp / "bin"
        b.mkdir()
        (b / "dendrad").write_text(FAKE_DENDRAD)
        os.chmod(b / "dendrad", 0o755)
        os.symlink(sys.executable, b / "python3")
        monkeypatch.setenv("PATH", str(b))
        monkeypatch.setenv("FAKE_STATE", str(self.state))
        monkeypatch.setenv("FAKE_KDIR", str(self.kdir))
        monkeypatch.setenv("FAKE_HOT", HOT)
        monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp / "status.json"))
        monkeypatch.setenv("DENDRA_CONFINE", "0")
        monkeypatch.setattr(D, "KEYRING_DIR", str(self.kdir))
        monkeypatch.setattr(D, "_KR", None)
        monkeypatch.setattr(D, "OWNER", owner)
        monkeypatch.setattr(D, "NODE", "tcp://chain:26657")
        monkeypatch.setattr(D, "PAYOUT_ADDRESS", "")
        monkeypatch.setattr(D._cid, "chain_id", lambda *a, **k: "c2banc-1")
        monkeypatch.setattr(D, "time", types.SimpleNamespace(time=__import__("time").time, sleep=lambda s: None))
        self.faucet = []

        def faucet(url, who):
            self.faucet.append(who)
            bal = json.loads((self.state / "balances.json").read_text())
            bal[who] = bal.get(who, 0) + 10_000_000
            (self.state / "balances.json").write_text(json.dumps(bal))
            return True, ""
        monkeypatch.setattr(D, "faucet_fund", faucet)
        monkeypatch.setattr(D.relay, "put", lambda *a, **k: True)
        monkeypatch.setattr(D.relay, "set_sign_key", lambda name: None)
        monkeypatch.setattr(D.relay, "listing", lambda *a, **k: {})
        monkeypatch.setattr(D.relay_signature, "address_from_key", lambda *a, **k: HOT)
        monkeypatch.setattr(D, "vrf_identity", lambda keydir, mid: ("", ""))
        monkeypatch.setattr(D, "model_weights_hash", lambda: "")
        monkeypatch.setattr(D, "pick_backend", lambda want: "mock")
        self.mined = []
        world = self

        class Miner:
            def __init__(self, mid, **kw):
                world.mined.append(mid)
        monkeypatch.setattr(D, "Miner", Miner)

    def calls(self):
        p = self.state / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def signed(self):
        p = self.state / "SIGNED"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def keys(self):
        return json.loads((self.state / "keys.json").read_text())

    def run(self, name="m-0123abcd"):
        sys_argv = ["miner.py", "--id", name, "--relay", "http://relay.invalid", "--keydir", str(self.keydir),
                    "--faucet", "http://faucet.invalid", "--backend", "mock", "--once"]
        old = sys.argv
        sys.argv = sys_argv
        try:
            D.main()
        finally:
            sys.argv = old


# ── the daemon: owner mode never registers itself ─────────────────────────────────────────────────────
def test_owner_mode_never_signs_create_miner_and_prints_the_three_commands(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.run()
    out = capsys.readouterr().out
    # NOTHING was signed or sent with this machine's key: every create-miner was a simulation or unsigned.
    assert w.signed() == []
    cms = [c for c in w.calls() if c[:3] == ["tx", "jobs", "create-miner"]]
    assert cms and all(("--dry-run" in c) or ("--generate-only" in c) for c in cms)
    assert all(c[c.index("--from") + 1] == COLD for c in cms), "every create-miner is from the OWNER's address"
    # the identity is the OWNER's, and the key that holds this machine's address is named after it
    assert w.keys() == {OWNED_ID: HOT}
    assert (w.keydir / "identite-resolue").read_text().strip() == OWNED_ID
    # the unsigned registration, in the volume: owner signs, this machine operates
    tx = json.loads((w.keydir / D.OWNER_TX_FILE["create-miner"]).read_text())
    [msg] = tx["body"]["messages"]
    assert (msg["@type"], msg["creator"], msg["miner_id"], msg["operator"]) == \
        ("/dendra.jobs.v1.MsgCreateMiner", COLD, OWNED_ID, HOT)
    # the gas is the SIMULATION's estimate times the kit's margin, never a constant and never --gas auto
    from modea import dendrad_argv as da
    assert tx["auth_info"]["fee"]["gas_limit"] == str(int(92393 * float(da.GAS_ADJUSTMENT)) + 1)
    # the three commands, complete: copy out, sign offline with the account read from the chain, broadcast
    assert f"docker compose -p dendra-miner exec -T miner cat {w.keydir}/owner-create-miner.json > create-miner.json" in out
    sign = owner_tx.shell(owner_tx.sign_argv("create-miner.json", COLD, "c2banc-1", "7", "0", "create-miner.signed.json"))
    assert sign in out and "--offline" in sign
    assert "dendrad tx broadcast - --node tcp://chain:26657 < create-miner.signed.json" in out
    assert "<" + "owner" not in out and "dendra1..." not in out, "a printed command carries no placeholder"
    # funds: this machine's key (an account to sign with) and the owner (the stake), once each
    assert sorted(w.faucet) == sorted([HOT, COLD])
    # and nothing was mined
    assert w.mined == [] and "nothing was mined" in out


def test_owner_mode_refuses_to_mine_while_another_operator_is_recorded(tmp_path, monkeypatch, capsys):
    reg = {OWNED_ID: {"miner_id": OWNED_ID, "creator": COLD, "operator": OTHER, "region": "eu", "stake": "1000000"}}
    w = World(tmp_path, monkeypatch, registry=reg, balances={COLD: 5, HOT: 5})
    w.run()
    out = capsys.readouterr().out
    assert OTHER in out and HOT in out, "both addresses are named"
    assert w.mined == [] and w.signed() == []
    tx = json.loads((w.keydir / D.OWNER_TX_FILE["update-miner"]).read_text())
    [msg] = tx["body"]["messages"]
    assert (msg["@type"], msg["creator"], msg["operator"], msg["miner_id"]) == \
        ("/dendra.jobs.v1.MsgUpdateMiner", COLD, HOT, OWNED_ID), "update-miner always carries the operator"


def test_owner_mode_mines_once_the_chain_names_this_key_the_operator(tmp_path, monkeypatch, capsys):
    reg = {OWNED_ID: {"miner_id": OWNED_ID, "creator": COLD, "operator": HOT, "region": "eu", "stake": "1000000"}}
    w = World(tmp_path, monkeypatch, registry=reg, balances={COLD: 5, HOT: 5})
    w.run()
    out = capsys.readouterr().out
    assert w.mined == [OWNED_ID]
    assert f"is registered by its owner {COLD} and operated by this machine" in out
    assert w.signed() == [] and not [c for c in w.calls() if c[:3] == ["tx", "jobs", "create-miner"]]


def test_an_owner_mode_miner_slashed_to_zero_never_registers_itself(tmp_path, monkeypatch, capsys):
    # proto3 omits a zero stake: the record carries no `stake` at all, the normal form of a total slash.
    reg = {OWNED_ID: {"miner_id": OWNED_ID, "creator": COLD, "operator": HOT, "region": "eu"}}
    w = World(tmp_path, monkeypatch, registry=reg, balances={COLD: 5, HOT: 5})
    w.run()
    assert w.signed() == [], "the bond and the identity are the owner's: this machine never sends create-miner"
    assert w.mined == [OWNED_ID]


def test_an_owner_mode_miner_never_reaches_self_registration_even_on_an_absent_record(tmp_path, monkeypatch, capsys):
    # The owner branch is what keeps an owner-mode miner away from register_attempt. A slashed miner is
    # PRESENT, and register_attempt sends nothing for a present record -- so that case cannot tell the branch
    # from its absence. An ABSENT record after owner_wait (a registry read that lags the owner's broadcast)
    # can: register_attempt would fund this machine's key and sign create-miner with it.
    w = World(tmp_path, monkeypatch, registry={}, balances={COLD: 5, HOT: 5})
    monkeypatch.setattr(D, "owner_wait", lambda *a, **k: True)
    called = []
    real = D.register_attempt
    monkeypatch.setattr(D, "register_attempt", lambda *a, **k: (called.append(a), real(*a, **k))[1])
    w.run()
    assert called == [], "owner mode never calls register_attempt"
    assert w.signed() == [], "the bond and the identity are the owner's: this machine never sends create-miner"


def test_without_an_owner_the_miner_registers_itself_as_before(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, owner="")
    w.run()
    [cm] = [c for c in w.signed() if c[:3] == ["tx", "jobs", "create-miner"]]
    assert cm[cm.index("--from") + 1] == SELF_ID and cm[cm.index("--") + 1:][:2] == [SELF_ID, HOT]
    assert w.keys() == {SELF_ID: HOT}


# ── the daemon: who the owner is, decided before any rename ───────────────────────────────────────────
def _never(mid):
    raise AssertionError("the registry must not be read here")


def test_decide_owner_from_the_setting(monkeypatch):
    assert D.decide_owner("m-0123abcd", HOT, configured=COLD, read=_never) == COLD
    assert D.decide_owner(OWNED_ID, HOT, configured=COLD.upper(), read=_never) == COLD
    assert D.decide_owner("m-0123abcd", HOT, configured=HOT, read=_never) == "", "its own key is no owner"
    assert D.decide_owner(SELF_ID, HOT, configured="", read=_never) == ""
    off = COLD[:-1] + ("q" if COLD[-1] != "q" else "p")
    with pytest.raises(D.OwnerModeError, match="not an owner address"):
        D.decide_owner("m-0123abcd", HOT, configured=off, read=_never)


def test_decide_owner_lets_the_registry_settle_a_disagreement(monkeypatch):
    present = lambda creator, op: (lambda mid: (D.PRESENT, {"creator": creator, "operator": op, "region": "eu", "stake": 1}))
    # the volume holds a miner THIS machine registered: owner mode would be another identity -- refused
    with pytest.raises(D.OwnerModeError, match="exit-miner.sh"):
        D.decide_owner(SELF_ID, HOT, configured=COLD, read=present(HOT, HOT))
    # never registered: the identity can still follow the owner
    assert D.decide_owner(SELF_ID, HOT, configured=COLD, read=lambda mid: (D.ABSENT, {})) == COLD
    # unread: nothing is decided
    with pytest.raises(D.OwnerModeError, match="cannot be read"):
        D.decide_owner(SELF_ID, HOT, configured=COLD, read=lambda mid: (D.UNREAD, {"why": "node down"}))
    # the setting is missing, the chain says another key owns this machine's miner: kept
    assert D.decide_owner(OWNED_ID, HOT, configured="", read=present(COLD, HOT)) == COLD
    # the chain says another operator: not an owner-mode miner of this machine
    assert D.decide_owner(OWNED_ID, HOT, configured="", read=present(COLD, OTHER)) == ""
    with pytest.raises(D.OwnerModeError, match="cannot be read"):
        D.decide_owner(OWNED_ID, HOT, configured="", read=lambda mid: (D.UNREAD, {"why": "node down"}))
    # the setting is missing and the chain does NOT register the volume's identity, which is not this
    # machine's: refused -- renaming it would register the owner's identity with the hot key
    with pytest.raises(D.OwnerModeError, match="DENDRA_MINER_OWNER") as e:
        D.decide_owner(OWNED_ID, HOT, configured="", read=lambda mid: (D.ABSENT, {}))
    assert "identite-resolue" in str(e.value) and "--owner" in str(e.value)


def test_a_lost_setting_never_registers_an_unregistered_owned_identity_with_the_hot_key(tmp_path, monkeypatch, capsys):
    # The path to it: uninstall (the volume is kept), then a join without --owner. The registry is EMPTY --
    # the owner had not signed yet, or left with exit-miner.sh -- and the volume resumes the owner's identity.
    w = World(tmp_path, monkeypatch, owner="", balances={})
    (w.state / "keys.json").write_text(json.dumps({OWNED_ID: HOT}))
    (w.kdir / "keyring-test").mkdir()
    (w.kdir / "keyring-test" / f"{OWNED_ID}.info").write_text("x")
    (w.keydir / "identite-resolue").write_text(OWNED_ID + "\n")
    with pytest.raises(SystemExit) as e:
        w.run()
    out = capsys.readouterr().out
    assert e.value.code == 5 and "OWNER MODE NOT STARTED" in out
    assert w.keys() == {OWNED_ID: HOT}, "the key keeps the owner's identity's name: nothing was renamed"
    assert (w.keydir / "identite-resolue").read_text().strip() == OWNED_ID
    assert w.signed() == [] and w.faucet == [] and w.mined == []
    assert not [c for c in w.calls() if c[:3] == ["tx", "jobs", "create-miner"]]


def test_an_account_the_chain_answers_without_number_or_sequence_is_zero_zero():
    # THE ZERO RULE, on the owner's account: proto3 omits zeros, so the first account of a genesis carries no
    # `account_number` and a fresh one no `sequence` (measured on a throwaway chain). Inside an answer that
    # was read and names THIS address, absent is 0 -- never a KeyError, never "unreadable".
    def answer(body):
        return lambda argv, timeout=120: (0, json.dumps(body), "")
    base = {"@type": "/cosmos.auth.v1beta1.BaseAccount", "address": COLD}
    assert owner_tx.account(COLD, run=answer({"account": base})) == ("0", "0")
    assert owner_tx.account(COLD, run=answer({"account": dict(base, sequence="3")})) == ("0", "3")
    assert owner_tx.account(COLD, run=answer({"account": dict(base, account_number="12")})) == ("12", "0")
    # ... but an answer that names ANOTHER address is not this account's record
    with pytest.raises(owner_tx.OwnerTxError, match="not that account"):
        owner_tx.account(COLD, run=answer({"account": dict(base, address=HOT)}))


def test_a_lost_setting_never_renames_an_owned_identity(tmp_path, monkeypatch, capsys):
    reg = {OWNED_ID: {"miner_id": OWNED_ID, "creator": COLD, "operator": HOT, "region": "eu", "stake": "1000000"}}
    w = World(tmp_path, monkeypatch, owner="", registry=reg, balances={COLD: 5, HOT: 5})
    (w.state / "keys.json").write_text(json.dumps({OWNED_ID: HOT}))
    (w.kdir / "keyring-test").mkdir()
    (w.kdir / "keyring-test" / f"{OWNED_ID}.info").write_text("x")
    (w.keydir / "identite-resolue").write_text(OWNED_ID + "\n")
    w.run()
    out = capsys.readouterr().out
    assert w.keys() == {OWNED_ID: HOT}, "the key keeps the owned identity's name"
    assert "OWNER MODE, READ FROM THE CHAIN" in out and w.mined == [OWNED_ID] and w.signed() == []


def test_payout_in_owner_mode_is_never_signed_by_this_machine(tmp_path, monkeypatch):
    import final_season_miner as fsm
    monkeypatch.setattr(D, "PAYOUT_ADDRESS", OTHER)
    monkeypatch.setattr(D, "PROGRAMME", "http://programme.invalid/final-season/v1")
    monkeypatch.setattr(fsm, "declare_payout", lambda *a, **k: (_ for _ in ()).throw(AssertionError("signed here")))
    settled, msg = D.maybe_declare_payout(str(tmp_path), OWNED_ID, HOT, owner=COLD)
    assert settled and "payout-prepare" in msg and "payout-submit" in msg and COLD in msg


# ── the registry cache: creators from the same read ────────────────────────────────────────────────────
def test_the_cache_reads_creators_and_an_absent_one_is_unknown():
    body = {"miner": [{"miner_id": "a", "operator": HOT, "creator": COLD},
                      {"miner_id": "b", "operator": OTHER}]}
    c = registry_cache.RegistryCache(lambda: json.dumps(body), lambda: 100.0)
    assert c.refresh()
    assert c.operator("a") == (HOT, "FEES") and c.creator("a") == (COLD, "FEES")
    assert c.operator("b") == (OTHER, "FEES") and c.creator("b") == (None, "FEES"), "never the operator"
    from modea.job_registry import JobRegistry
    j = JobRegistry(lambda: json.dumps({"job": [{"job_id": "j1", "client": HOT}]}), lambda: 1.0)
    assert j.refresh() and j.client("j1")[0] == HOT and j.creator("j1")[0] is None


# ── the season server: the owner declares ──────────────────────────────────────────────────────────────
@pytest.fixture()
def season(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_DATA", str(tmp_path / "srv"))
    monkeypatch.setenv("DENDRA_FINAL_SEASON_START_HEIGHT", "1")
    S = _load("DENDRA_SEASON_SERVER_FILE", "final_season_server")
    if not os.environ.get("DENDRA_SEASON_SERVER_FILE"):
        importlib.reload(S)
    reg = {}

    class Reg:
        def operator(self, mid):
            return (reg.get(mid, {}).get("operator"), "FEES")

        def creator(self, mid):
            return (reg.get(mid, {}).get("creator"), "FEES")

    S.REGISTRY = Reg()
    S.ST = S.State(str(tmp_path / "srv"), 1)
    S.ST.height = 1000
    S.ST.latest_time = S.END - 10 * 86400
    S._RATE.clear()
    httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/final-season/v1"
    yield types.SimpleNamespace(S=S, reg=reg, base=base, tmp=tmp_path)
    httpd.shutdown()


class Key:
    """An ephemeral secp256k1 key that signs exactly what `dendrad tx sign --sign-mode amino-json` signs."""

    def __init__(self):
        pytest.importorskip("cryptography")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        self.sk = ec.generate_private_key(ec.SECP256K1())
        self.pub = self.sk.public_key().public_bytes(encoding=serialization.Encoding.X962,
                                                     format=serialization.PublicFormat.CompressedPoint)
        self.address = cosmos_addr.address_from_pubkey(self.pub)

    def sign_document(self, doc: dict, an, seq) -> dict:
        """The JSON `dendrad tx sign` writes for `doc` (relay_signature.document)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        amino = relay_carrier.document_amino(bytes.fromhex(doc["body"]["memo"]), self.address, an, seq)
        r, s = utils.decode_dss_signature(self.sk.sign(amino, ec.ECDSA(hashes.SHA256())))
        signed = json.loads(json.dumps(doc))
        signed["auth_info"]["signer_infos"] = [{
            "public_key": {"@type": "/cosmos.crypto.secp256k1.PubKey", "key": base64.b64encode(self.pub).decode()},
            "mode_info": {"single": {"mode": "SIGN_MODE_LEGACY_AMINO_JSON"}}, "sequence": str(seq)}]
        signed["signatures"] = [base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()]
        return signed

    def signer(self, an="7", seq="0"):
        """A `sign` callable for final_season_miner.declare_payout, signing with THIS key."""
        def sign(kind, key, body, mid, h):
            doc = rs.document(kind, key, body, mid, h, self.address)
            return rs.headers_from_signed(self.sign_document(doc, an, seq), mid, h, an, seq)
        return sign


def _payouts(S):
    return [r for r in S.ST.ev.read(S.ST.day_now()) if r["type"] == "payout"]


def test_one_key_as_owner_and_operator_declares_as_before(season):
    import final_season_miner as M
    k = Key()
    mid = miner_id_for_account(k.address)
    season.reg[mid] = {"operator": k.address, "creator": k.address}
    code, r = M.declare_payout(season.base, mid, OTHER, sign=k.signer())
    assert code == 200, r
    assert [p["address"] for p in _payouts(season.S)] == [OTHER]


def test_in_owner_mode_only_the_owner_declares(season):
    import final_season_miner as M
    owner, hot = Key(), Key()
    mid = miner_id_for_account(owner.address)
    season.reg[mid] = {"operator": hot.address, "creator": owner.address}
    code, r = M.declare_payout(season.base, mid, OTHER, sign=hot.signer())
    assert code == 401 and owner.address in r["error"] and "OWNER" in r["error"], r
    assert _payouts(season.S) == []
    code, r = M.declare_payout(season.base, mid, OTHER, sign=owner.signer())
    assert code == 200, r
    assert [p["address"] for p in _payouts(season.S)] == [OTHER]


def test_a_registry_read_with_no_creator_attributes_to_nobody(season):
    import final_season_miner as M
    k = Key()
    mid = miner_id_for_account(k.address)
    season.reg[mid] = {"operator": k.address}
    code, r = M.declare_payout(season.base, mid, OTHER, sign=k.signer())
    assert code == 401 and "no owner" in r["error"], r
    assert _payouts(season.S) == []


# ── payout-prepare / payout-submit: signed on the owner's machine ──────────────────────────────────────
def test_prepare_then_submit_with_a_real_owner_signature(season, tmp_path, monkeypatch):
    import final_season_miner as M
    owner, hot = Key(), Key()
    mid = miner_id_for_account(owner.address)
    season.reg[mid] = {"operator": hot.address, "creator": owner.address}
    keydir = tmp_path / "keys"
    keydir.mkdir()
    monkeypatch.setenv("DENDRA_PAYOUT_ADDRESS", OTHER)
    rc, doc, sign = M.prepare_payout(season.base, mid, OTHER, owner.address, str(keydir),
                                     numero=lambda addr, node=None: ("7", "0"))
    assert rc == 0, doc
    assert doc == rs.document("fspay", f"payout__{mid}", M._payout_body(mid, OTHER.lower()), mid, 1000, owner.address)
    assert sign == rs.sign_argv("payout.json", owner.address, "7", "0") + ["--output-document", "payout.signed.json"]
    assert "--offline" in sign and relay_carrier.DOMAINE_CHAIN_ID in sign and "amino-json" in sign
    # a file signed by ANOTHER key is refused before anything is sent
    rc, r = M.submit_payout(json.dumps(hot.sign_document(doc, "7", "0")), str(keydir))
    assert rc == 2 and "not by the owner" in r["error"] and _payouts(season.S) == []
    # the owner's signature over the prepared document: accepted by the real server
    rc, r = M.submit_payout(json.dumps(owner.sign_document(doc, "7", "0")), str(keydir))
    assert rc == 0, r
    [p] = _payouts(season.S)
    assert (p["miner_id"], p["address"]) == (mid, OTHER.lower())
    rec = json.loads((keydir / "payout-declared.json").read_text())
    assert (rec["miner_id"], rec["address"], rec["source"], rec["setting"]) == (mid, OTHER.lower(), "owner", OTHER.lower())
    assert not (keydir / M.PENDING).exists()


def test_submit_refuses_a_signature_over_another_declaration(season, tmp_path):
    import final_season_miner as M
    owner = Key()
    mid = miner_id_for_account(owner.address)
    season.reg[mid] = {"operator": HOT, "creator": owner.address}
    keydir = tmp_path / "keys"
    keydir.mkdir()
    rc, doc, _ = M.prepare_payout(season.base, mid, OTHER, owner.address, str(keydir),
                                  numero=lambda addr, node=None: ("7", "0"))
    assert rc == 0
    signed = owner.sign_document(doc, "7", "1")          # another sequence than the one prepared
    rc, r = M.submit_payout(json.dumps(signed), str(keydir))
    assert rc == 2 and "does not cover" in r["error"] and _payouts(season.S) == []


def test_prepare_refuses_an_owner_that_did_not_register_this_miner(season, tmp_path):
    import final_season_miner as M
    keydir = tmp_path / "keys"
    keydir.mkdir()
    rc, r, _ = M.prepare_payout(season.base, OWNED_ID, OTHER, HOT, str(keydir), numero=lambda a, node=None: ("7", "0"))
    assert rc == 2 and "was not registered by" in r["error"]
    off = COLD[:-1] + ("q" if COLD[-1] != "q" else "p")
    rc, r, _ = M.prepare_payout(season.base, OWNED_ID, OTHER, off, str(keydir), numero=lambda a, node=None: ("7", "0"))
    assert rc == 2 and "--owner" in r["error"]
    assert list(keydir.iterdir()) == [], "nothing is prepared for a refused owner"
