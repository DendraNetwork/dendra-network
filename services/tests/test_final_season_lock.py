"""The payout address LOCK of the Final Testnet Season, against the real service, over HTTP.

A miner on a rented machine keeps its signing key where the host can read it. The lock is what keeps the
host from redirecting the season's rewards with that key: an identity that filed a locked declaration has
any OTHER address refused (409) from then on, and its own address answered 200 without a new record.
Without the flag, nothing changes.

AND THE LOCK ITSELF IS GUARDED. Accepted at any time, it let a thief of a self-registered miner's key lock
HIS address for good. It is accepted only as an identity's FIRST declaration, or signed by the miner's
creator while another key operates it (owner mode); a lock asked for after a declaration without it is
refused (409, "lock_refused") and files nothing, before and after a restart. Both halves are held here:
what must pass (first declaration, owner mode) and what must refuse (a later lock from the machine's key,
a registry that names no owner).

Faked, as in test_final_season_miner.py and for the same reason: the asymmetric SIGNATURE is replaced on
both sides by the digest of the canonical message, which the service's verifier recomputes from what it
RECEIVED -- so a lock that is not inside the signed body is refused here as it would be by the real
verifier. NOT faked: the route, the body, the evidence written, the restart that rebuilds the lock from it,
the command and the daemon's handling of the answer.

`DENDRA_MODEA_DIR` puts another copy of the service first on the path: the mutation bench
(dendra/onchain-staging/dendra_verrou_paie_test.sh) points it at a MUTATED copy and expects red.
"""
import importlib
import json
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)

import pytest  # noqa: E402

from modea import cosmos_addr, relay_canon  # noqa: E402
from modea import relay_signature as _rs  # noqa: E402

HEIGHT = 4321
COLD = cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes(range(20)), 8, 5))
THIEF = cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes(range(1, 21)), 8, 5))


def _modea_first():
    if MODEA:
        while MODEA in sys.path:
            sys.path.remove(MODEA)
        sys.path.insert(0, MODEA)


def _headers(kind, key, body, mid, h):
    return {_rs.HEADER_MINER: mid, _rs.HEADER_HEIGHT: str(h),
            _rs.HEADER_SIG: relay_canon.empreinte(kind, key, body, mid, h).hex()}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_DATA", str(tmp_path / "srv"))
    monkeypatch.delenv("DENDRA_FINAL_SEASON_URL", raising=False)
    monkeypatch.delenv("DENDRA_PAYOUT_LOCK", raising=False)
    # A reload FINDS the module again on the path: a test before this one has put the delivered directory in
    # front (each module inserts its own), so the copy under test goes back first BEFORE the reload too, or a
    # parametrized case after the first would measure the delivered file.
    _modea_first()
    import final_season_server as S
    importlib.reload(S)
    if MODEA and os.path.exists(os.path.join(MODEA, "final_season_server.py")):
        # The copy under test is the one on the path first, never the delivered file by accident.
        assert os.path.dirname(os.path.abspath(S.__file__)) == os.path.abspath(MODEA), S.__file__
    S.ST = S.State(str(tmp_path / "srv"), 1)
    S.ST.height = HEIGHT
    S.ST.latest_time = S.END - 10 * 86400
    S._RATE.clear()
    # The registry as the chain names it: a self-registered miner by default (one key, creator and operator).
    # `signer` is the address a verified declaration is attributed to (the creator for this kind of write).
    reg = types.SimpleNamespace(operator="dendra1operator", creator="dendra1operator", signer="dendra1operator")

    class FakeRegistry:
        def operator(self, miner_id):
            return reg.operator, "fresh"

        def creator(self, miner_id):
            return reg.creator, "fresh"

    monkeypatch.setattr(S, "REGISTRY", FakeRegistry())

    def fake_signed(self, kind, key, body):
        mid = self.headers.get(_rs.HEADER_MINER)
        try:
            h = int(self.headers.get(_rs.HEADER_HEIGHT))
        except (TypeError, ValueError):
            return None, "unsigned (test)"
        if self.headers.get(_rs.HEADER_SIG) != S._canon(kind, key, body, mid, h)[1].hex():
            return None, "SIGNATURE_INVALIDE (test)"
        named = key.rsplit("__", 1)[1] if "__" in key else None
        if named != mid:
            return None, "key/header mismatch (test)"
        return mid, reg.signer

    monkeypatch.setattr(S.Handler, "_signed", fake_signed)
    httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/final-season/v1"
    # The service puts ITS directory first on the path when it is imported: the copy under test goes back
    # in front, or a mutated command would be measured through the delivered one.
    _modea_first()
    import final_season_miner as M
    importlib.reload(M)
    if MODEA and os.path.exists(os.path.join(MODEA, "final_season_miner.py")):
        assert os.path.dirname(os.path.abspath(M.__file__)) == os.path.abspath(MODEA), M.__file__
    monkeypatch.setattr(M, "RESOLVED", str(tmp_path / "no-resolved-identity"))
    signed = []

    def sign(kind, key, body, mid, h):
        signed.append((kind, key, body, mid, h))
        return _headers(kind, key, body, mid, h)

    yield types.SimpleNamespace(S=S, M=M, base=base, sign=sign, signed=signed, tmp=tmp_path, reg=reg)
    httpd.shutdown()


def payouts(S):
    return [r for d in S.ST.ev.days() for r in S.ST.ev.read(d) if r.get("type") == "payout"]


def declare(W, mid, addr, lock=False):
    return W.M.declare_payout(W.base, mid, addr, sign=W.sign, lock=lock)


def test_a_locked_declaration_is_filed_with_its_lock_and_another_address_is_refused(world):
    W = world
    code, r = declare(W, "m1", COLD, lock=True)
    assert code == 200 and r == {"ok": True, "locked": True}, r
    [rec] = payouts(W.S)
    assert rec["address"] == COLD and rec["lock"] is True
    # the hourly limit is not what refuses here: even once the period has passed, another address is 409
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    for lock in (True, False):
        code, r = declare(W, "m1", THIEF, lock=lock)
        assert code == 409 and r.get("locked") is True and "locked" in r["error"], (lock, code, r)
    assert [p["address"] for p in payouts(W.S)] == [COLD]           # nothing of the refused ones is filed


def test_the_locked_address_again_is_200_and_files_nothing(world):
    W = world
    assert declare(W, "m1", COLD, lock=True)[0] == 200
    # immediately, inside the hourly period: the address it already holds is not a new declaration
    for lock in (True, False):
        code, r = declare(W, "m1", COLD.upper(), lock=lock)
        assert code == 200 and r == {"ok": True, "locked": True, "unchanged": True}, (lock, code, r)
    assert len(payouts(W.S)) == 1
    assert W.S.ST.declared[("payout", "m1")] == HEIGHT              # the allowance was not spent again


def test_without_the_flag_nothing_changes(world):
    W = world
    code, r = declare(W, "m1", COLD)
    assert code == 200 and r == {"ok": True}, r
    assert declare(W, "m1", THIEF)[0] == 429                       # one declaration per period, as before
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    assert declare(W, "m1", THIEF)[0] == 200                       # and an unlocked address still changes
    assert [p["address"] for p in payouts(W.S)] == [COLD, THIEF]
    assert all("lock" not in p for p in payouts(W.S))               # the record keeps its earlier shape
    assert "m1" not in W.S.ST.locked


def test_a_lock_after_a_declaration_without_it_is_refused_for_good_and_files_nothing(world):
    # The thief of a self-registered miner's key, whose owner declared without the lock, must not be able to
    # lock HIS address for good: the lock is refused, inside the hourly period (409, not 429: final) and after.
    W = world
    assert declare(W, "m1", COLD)[0] == 200                         # the owner's declaration, no lock
    for _ in range(2):
        code, r = declare(W, "m1", THIEF, lock=True)
        assert code == 409 and r.get("lock_refused") is True and "FIRST declaration" in r["error"], (code, r)
        assert r.get("locked") is not True                          # never mistaken for "another address is locked"
        W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    code, r = declare(W, "m1", COLD, lock=True)                      # the owner's own address: refused the same way
    assert code == 409 and r.get("lock_refused") is True, (code, r)
    assert [p["address"] for p in payouts(W.S)] == [COLD] and all("lock" not in p for p in payouts(W.S))
    assert "m1" not in W.S.ST.locked
    # Nothing is locked, so the owner can still change the address without the lock, as before.
    assert declare(W, "m1", THIEF)[0] == 200


def test_the_first_declaration_rule_survives_a_restart(world):
    W = world
    assert declare(W, "m1", COLD)[0] == 200
    again = W.S.State(W.S.ST.data, 1)
    again.height = HEIGHT + 10 * W.S.DECLARE_EVERY_BLOCKS
    again.latest_time = W.S.END - 10 * 86400
    W.S.ST = again
    code, r = declare(W, "m1", THIEF, lock=True)
    assert code == 409 and r.get("lock_refused") is True, (code, r)
    assert again.locked == {} and [p["address"] for p in payouts(W.S)] == [COLD]


def test_owner_mode_may_lock_after_a_declaration_without_it(world):
    # Owner mode: the creator signs, another key operates the miner and cannot declare at all. A lock from the
    # creator is accepted even after an earlier declaration without it.
    W = world
    W.reg.operator, W.reg.creator, W.reg.signer = "dendra1hotkey", "dendra1owner", "dendra1owner"
    assert declare(W, "m1", THIEF)[0] == 200
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    code, r = declare(W, "m1", COLD, lock=True)
    assert code == 200 and r == {"ok": True, "locked": True}, (code, r)
    assert W.S.ST.locked == {"m1": COLD}
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    assert declare(W, "m1", THIEF)[0] == 409


def _thief_flips_to_owner_mode(W, own_key="dendra1operator"):
    # The thief holds the self-registered miner's one key, which is its CREATOR: `update-miner` (the creator
    # alone may change the operator, msg_server_miner.go::UpdateMiner) points the operator at another key of
    # his, and the miner reads as owner mode, with the declaration attributed to the creator he holds.
    W.reg.operator, W.reg.creator, W.reg.signer = "dendra1thiefsecondkey", own_key, own_key


def test_a_thief_who_switches_the_miner_to_owner_mode_is_still_refused_the_lock(world):
    # The relecture's replay: owner mode read at the time of the lock alone is something the thief can MAKE.
    W = world
    assert declare(W, "m1", COLD)[0] == 200                         # the owner, self-registered, no lock
    [rec] = payouts(W.S)
    assert "owner" not in rec                                        # filed from a key that also operates it
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    _thief_flips_to_owner_mode(W)
    code, r = declare(W, "m1", THIEF, lock=True)
    assert code == 409 and r.get("lock_refused") is True and "FIRST declaration" in r["error"], (code, r)
    assert "m1" not in W.S.ST.locked and [p["address"] for p in payouts(W.S)] == [COLD]
    # And once the thief has set the operator back, the owner still changes the address, unlocked, as before.
    W.reg.operator = W.reg.creator
    assert declare(W, "m1", COLD)[0] == 200


def test_the_thiefs_switch_is_refused_after_a_restart_too(world):
    W = world
    assert declare(W, "m1", COLD)[0] == 200
    again = W.S.State(W.S.ST.data, 1)
    again.height = HEIGHT + 10 * W.S.DECLARE_EVERY_BLOCKS
    again.latest_time = W.S.END - 10 * 86400
    W.S.ST = again
    _thief_flips_to_owner_mode(W)
    code, r = declare(W, "m1", THIEF, lock=True)
    assert code == 409 and r.get("lock_refused") is True, (code, r)
    assert again.locked == {} and [p["address"] for p in payouts(W.S)] == [COLD]


def test_records_say_owner_mode_only_when_it_holds(world):
    W = world
    assert declare(W, "m1", COLD)[0] == 200                          # self-registered: one key in both places
    W.reg.operator, W.reg.creator, W.reg.signer = "dendra1hotkey", "dendra1owner", "dendra1owner"
    assert declare(W, "m2", COLD)[0] == 200                          # owner mode, signed by the creator
    by = {p["miner_id"]: p for p in payouts(W.S)}
    assert "owner" not in by["m1"] and by["m2"]["owner"] is True, by


def test_an_owner_who_always_declared_as_one_may_lock_after_a_restart(world):
    # The half that must PASS: owner mode now AND in every earlier record, rebuilt from the log.
    W = world
    W.reg.operator, W.reg.creator, W.reg.signer = "dendra1hotkey", "dendra1owner", "dendra1owner"
    assert declare(W, "m1", THIEF)[0] == 200
    again = W.S.State(W.S.ST.data, 1)
    again.height = HEIGHT + 10 * W.S.DECLARE_EVERY_BLOCKS
    again.latest_time = W.S.END - 10 * 86400
    W.S.ST = again
    code, r = declare(W, "m1", COLD, lock=True)
    assert code == 200 and r == {"ok": True, "locked": True}, (code, r)
    assert again.locked == {"m1": COLD}


def test_a_record_without_the_owner_field_is_not_owner_mode(world):
    # A record filed before the field existed (or by hand) says nothing of the mode: unknown is never owner
    # mode, so an owner-mode lock after it is refused. And a field that is not the boolean true is not owner mode.
    W = world
    for i, extra in enumerate(({}, {"owner": "true"}, {"owner": 1})):
        mid = f"old{i}"
        W.S.ST.ev.append(0, {"type": "payout", "miner_id": mid, "address": THIEF, "height": HEIGHT - 2000, **extra})
    again = W.S.State(W.S.ST.data, 1)
    again.height, again.latest_time = HEIGHT, W.S.END - 10 * 86400
    W.S.ST = again
    W.reg.operator, W.reg.creator, W.reg.signer = "dendra1hotkey", "dendra1owner", "dendra1owner"
    for i in range(3):
        code, r = declare(W, f"old{i}", COLD, lock=True)
        assert code == 409 and r.get("lock_refused") is True, (i, code, r)
    assert again.locked == {}


@pytest.mark.parametrize("operator,creator,signer", [
    (None, None, "dendra1owner"),                   # the registry read names nobody
    ("dendra1hotkey", None, "dendra1owner"),        # it names no creator
    ("dendra1owner", "dendra1owner", "dendra1owner"),  # one key in both places: self-registered
    ("dendra1hotkey", "dendra1owner", "dendra1hotkey"),  # attributed to the operator, not the creator
])
def test_owner_mode_is_never_assumed(world, operator, creator, signer):
    W = world
    W.reg.operator, W.reg.creator, W.reg.signer = operator, creator, signer
    W.S.ST.declared[("payout", "m1")] = HEIGHT - 2 * W.S.DECLARE_EVERY_BLOCKS     # an earlier declaration
    code, r = declare(W, "m1", COLD, lock=True)
    assert code == 409 and r.get("lock_refused") is True, (operator, creator, signer, code, r)
    assert "m1" not in W.S.ST.locked


def test_the_lock_is_per_identity(world):
    W = world
    assert declare(W, "m1", COLD, lock=True)[0] == 200
    assert declare(W, "m2", THIEF)[0] == 200


def test_the_lock_survives_a_restart(world):
    W = world
    assert declare(W, "m1", COLD, lock=True)[0] == 200
    again = W.S.State(W.S.ST.data, 1)
    assert again.locked == {"m1": COLD}
    again.height = HEIGHT + 10 * W.S.DECLARE_EVERY_BLOCKS
    again.latest_time = W.S.END - 10 * 86400
    W.S.ST = again
    code, r = declare(W, "m1", THIEF)
    assert code == 409, (code, r)
    assert declare(W, "m1", COLD)[0] == 200


def test_the_lock_must_be_a_real_boolean(world):
    W = world
    for bad in ("true", 1, None, "yes"):
        body = json.dumps({"address": COLD, "lock": bad, "miner_id": "m1"}, sort_keys=True,
                          separators=(",", ":")).encode()
        code, r = W.M._http("POST", W.base + "/payout", body, W.sign("fspay", "payout__m1", body, "m1", HEIGHT))
        assert code == 400 and "lock" in r.get("error", ""), (bad, code, r)
    assert payouts(W.S) == [] and "m1" not in W.S.ST.locked


def test_the_lock_is_inside_the_signed_body(world):
    W = world
    plain = W.M._payout_body("m1", COLD)
    locked = W.M._payout_body("m1", COLD, lock=True)
    assert json.loads(locked) == {"address": COLD, "lock": True, "miner_id": "m1"}
    # a signature over the body WITHOUT the lock does not carry a request that adds it
    code, r = W.M._http("POST", W.base + "/payout", locked, W.sign("fspay", "payout__m1", plain, "m1", HEIGHT))
    assert code == 401, (code, r)
    assert payouts(W.S) == [] and "m1" not in W.S.ST.locked


def test_the_command_locks_only_when_asked(world, monkeypatch):
    W = world
    import relay_client
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    argv = ["payout", "--miner", "m1", "--address", COLD, "--programme", W.base]
    assert W.M.main(argv) == 0
    assert "lock" not in json.loads(W.signed[-1][2])
    # A lock is granted on an identity's first declaration: m2's, with --lock.
    assert W.M.main(["payout", "--miner", "m2", "--address", COLD, "--programme", W.base, "--lock"]) == 0
    assert json.loads(W.signed[-1][2])["lock"] is True
    assert W.S.ST.locked == {"m2": COLD}
    # And m1, which declared without it, is refused the lock by the service: the command says it failed.
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    assert W.M.main(argv + ["--lock"]) == 1 and "m1" not in W.S.ST.locked


# -- the daemon: DENDRA_PAYOUT_LOCK=1 is what docker/cloud-start.sh sets --------------------------------
def _daemon(monkeypatch, answers):
    _modea_first()
    import miner as D
    import final_season_miner as fsm
    from modea import keyring as K
    if MODEA and os.path.exists(os.path.join(MODEA, "miner.py")):
        assert os.path.dirname(os.path.abspath(D.__file__)) == os.path.abspath(MODEA), D.__file__
    calls = []

    def fake_declare(base, mid, address, sign=None, **kw):
        calls.append((mid, address, kw))
        return answers.pop(0)
    monkeypatch.setattr(fsm, "declare_payout", fake_declare)
    monkeypatch.setattr(D, "PAYOUT_ADDRESS", COLD)
    monkeypatch.setattr(D, "PROGRAMME", "http://programme.example/final-season/v1")
    return D, K, calls


def test_the_daemon_locks_when_the_pod_asks_and_records_what_the_programme_answered(tmp_path, monkeypatch):
    D, K, calls = _daemon(monkeypatch, [(200, {"ok": True, "locked": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert settled and calls == [("dm1abc", COLD, {"lock": True})] and "LOCKED" in msg, (calls, msg)
    rec = K.payout_record(str(tmp_path))
    assert rec["address"] == COLD and rec["locked"] is True


def test_the_daemon_without_the_setting_calls_as_before(tmp_path, monkeypatch):
    D, K, calls = _daemon(monkeypatch, [(200, {"ok": True})])
    monkeypatch.delenv("DENDRA_PAYOUT_LOCK", raising=False)
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert settled and calls == [("dm1abc", COLD, {})] and "LOCKED" not in msg
    assert "locked" not in K.payout_record(str(tmp_path))


def test_a_409_is_final_said_once_and_records_nothing(tmp_path, monkeypatch):
    D, K, calls = _daemon(monkeypatch, [(409, {"error": "the payout address of dm1abc is locked", "locked": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert settled is True and "REFUSED for good" in msg and len(calls) == 1
    assert K.payout_record(str(tmp_path)) is None


def test_a_refused_lock_is_final_said_as_such_and_keeps_the_record(tmp_path, monkeypatch):
    # The programme refuses the lock because this identity declared before without it: final, not retried, said
    # in its own words (not "another address is locked"), and the earlier record is left as it is.
    _record(str(tmp_path), miner_id="dm1abc", address=COLD, setting=COLD, source="daemon")
    D, K, calls = _daemon(monkeypatch, [(409, {"error": "cannot be locked: a lock is accepted only as an "
                                                        "identity's FIRST declaration", "lock_refused": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert settled is True and len(calls) == 1 and calls[0][2] == {"lock": True}, (calls, msg)
    assert "WITH THE LOCK" in msg and "first declaration" in msg and "holds a LOCKED" not in msg, msg
    rec = K.payout_record(str(tmp_path))
    assert rec["address"] == COLD and "locked" not in rec, rec
    # The refusal itself is recorded, apart, as the programme answered it: the pod's watch reads it.
    assert _refusal(str(tmp_path)) == {"miner_id": "dm1abc", "address": COLD, "refused": "lock_refused"}


def _refusal(keydir):
    import final_season_miner as fsm
    p = os.path.join(keydir, fsm.LOCK_REFUSED)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    d.pop("at", None)
    return d


def test_a_refused_lock_on_a_volume_without_a_record_is_recorded_and_files_nothing(tmp_path, monkeypatch):
    # The pod's case the watch could not see: no record on this volume (the identity declared elsewhere
    # before), the lock refused. Nothing is declared, and the refusal is what the volume now says.
    D, K, calls = _daemon(monkeypatch, [(409, {"error": "cannot be locked", "lock_refused": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert settled is True and len(calls) == 1 and "REFUSED for good" in msg, (calls, msg)
    assert K.payout_record(str(tmp_path)) is None
    assert _refusal(str(tmp_path)) == {"miner_id": "dm1abc", "address": COLD, "refused": "lock_refused"}


def test_another_address_locked_is_not_recorded_as_a_refused_lock(tmp_path, monkeypatch):
    D, K, calls = _daemon(monkeypatch, [(409, {"error": "the payout address of dm1abc is locked", "locked": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert _refusal(str(tmp_path)) is None


# -- a lock asked for and not obtained: the answer of a programme deployed before the lock existed ----------
def _record(keydir, **doc):
    with open(os.path.join(keydir, "payout-declared.json"), "w", encoding="utf-8", newline="") as f:
        json.dump(doc, f)


def test_a_lock_the_programme_does_not_confirm_is_not_settled_and_not_recorded_locked(tmp_path, monkeypatch):
    # The service before the lock answers {"ok": true} and ignores the key: the address is filed, unlocked.
    D, K, calls = _daemon(monkeypatch, [(200, {"ok": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert calls == [("dm1abc", COLD, {"lock": True})], calls
    assert settled is False and "WITHOUT the lock" in msg and "LOCKED" not in msg, msg
    rec = K.payout_record(str(tmp_path))
    assert rec["address"] == COLD and "locked" not in rec, rec       # what was answered, never what was asked


def test_an_unlocked_record_is_declared_again_with_the_lock(tmp_path, monkeypatch):
    # A volume that carries an earlier, unlocked declaration of the SAME address (a rig run without the lock,
    # or the answer above): with the lock asked for, it is not settled, and the next call asks again.
    _record(str(tmp_path), miner_id="dm1abc", address=COLD, setting=COLD, source="daemon")
    D, K, calls = _daemon(monkeypatch, [(200, {"ok": True, "locked": True})])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine")
    assert calls == [("dm1abc", COLD, {"lock": True})] and settled and "LOCKED" in msg, (calls, msg)
    assert K.payout_record(str(tmp_path))["locked"] is True


def test_without_the_lock_an_unlocked_record_stays_settled(tmp_path, monkeypatch):
    _record(str(tmp_path), miner_id="dm1abc", address=COLD, setting=COLD, source="daemon")
    D, K, calls = _daemon(monkeypatch, [])
    monkeypatch.delenv("DENDRA_PAYOUT_LOCK", raising=False)
    assert D.maybe_declare_payout(str(tmp_path), "dm1abc", "dendra1machine") == (True, "") and calls == []


def test_the_lock_is_never_asked_for_the_machines_own_key(tmp_path, monkeypatch):
    D, K, calls = _daemon(monkeypatch, [])
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    settled, msg = D.maybe_declare_payout(str(tmp_path), "dm1abc", COLD.upper())
    assert calls == [] and settled is True and "OWN key" in msg and "NOT locked" in msg, (calls, msg)
    assert K.payout_record(str(tmp_path)) is None


def _real_daemon(W, monkeypatch):
    import relay_client
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    _modea_first()
    import miner as D
    monkeypatch.setattr(D, "PAYOUT_ADDRESS", COLD)
    monkeypatch.setattr(D, "PROGRAMME", W.base)
    monkeypatch.setenv("DENDRA_PAYOUT_LOCK", "1")
    return D


def test_against_the_real_service_an_earlier_unlocked_declaration_is_not_locked(world, monkeypatch):
    # End to end: the record and the service both hold an UNLOCKED declaration of COLD (a volume from a rig
    # run without the lock). The daemon, asked to lock, declares COLD again with the lock; the service refuses
    # it (the lock is for an identity's FIRST declaration), the daemon says so once and settles, and nothing
    # is locked: the record stays unlocked, as the pod's watch then says.
    W = world
    assert declare(W, "dm1abc", COLD)[0] == 200
    _record(str(W.tmp), miner_id="dm1abc", address=COLD, setting=COLD, source="daemon")
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    D = _real_daemon(W, monkeypatch)
    settled, msg = D.maybe_declare_payout(str(W.tmp), "dm1abc", "dendra1machine")
    assert settled and "WITH THE LOCK" in msg, msg
    assert W.S.ST.locked == {} and [p["address"] for p in payouts(W.S)] == [COLD]
    import final_season_miner as fsm  # noqa: F401  (the record is the keyring's, read as the pod reads it)
    from modea import keyring as K
    assert "locked" not in K.payout_record(str(W.tmp))


def test_against_the_real_service_the_pods_first_declaration_is_locked(world, monkeypatch):
    # The pod's path: a fresh identity, no record, no declaration on the service. The daemon's first
    # declaration carries the lock and is locked; the host's address is refused from then on.
    W = world
    D = _real_daemon(W, monkeypatch)
    settled, msg = D.maybe_declare_payout(str(W.tmp), "dm1abc", "dendra1machine")
    assert settled and "LOCKED" in msg, msg
    assert W.S.ST.locked == {"dm1abc": COLD.lower()}
    from modea import keyring as K
    assert K.payout_record(str(W.tmp))["locked"] is True
    W.S.ST.height += W.S.DECLARE_EVERY_BLOCKS
    assert declare(W, "dm1abc", THIEF)[0] == 409 and declare(W, "dm1abc", THIEF, lock=True)[0] == 409
