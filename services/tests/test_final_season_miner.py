"""The miner's Final Testnet Season command against the real service, over HTTP.

The command does two things: it reads the programme's status, and it declares where an identity's rewards
go, signed like a relay deposit at the height the service reports. What is faked, and why that does not
hollow the bench: the asymmetric SIGNATURE (benched on its own with a real binary) is replaced on both
sides by the digest of the canonical message (`relay_canon`, the bytes the real signature covers), and
the service's verifier recomputes that digest from what it RECEIVED and applies the replay guard's
freshness window. A declaration signed over another body, kind, key, miner or height is therefore refused
here as it would be by the real verifier. What is NOT faked: the routes, the body sent, the height read,
the evidence written, the refusals the service sends back, and the identity the command signs as.
"""
import importlib
import json
import os
import socket
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

# A height the service reports. Neither 0 nor the season's start, so a command that signs at a constant
# instead of the height it read cannot match it by chance.
HEIGHT = 4321
GOOD = cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes(range(20)), 8, 5))


def _headers(kind, key, body, mid, h):
    """What the bench's signer attaches: the digest of the exact message the real signature covers."""
    return {_rs.HEADER_MINER: mid, _rs.HEADER_HEIGHT: str(h),
            _rs.HEADER_SIG: relay_canon.empreinte(kind, key, body, mid, h).hex()}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DENDRA_FINAL_SEASON_DATA", str(tmp_path / "srv"))
    monkeypatch.delenv("DENDRA_FINAL_SEASON_URL", raising=False)
    import final_season_server as S
    importlib.reload(S)
    S.ST = S.State(str(tmp_path / "srv"), 1)
    S.ST.height = HEIGHT
    # A RUNNING season: the latest block's header time is read and lies before the end. Left unread,
    # the service refuses every declaration (425), which is right and is not what these cases measure.
    S.ST.latest_time = S.END - 10 * 86400
    S._RATE.clear()
    received, posts = [], []

    def fake_signed(self, kind, key, body):
        mid = self.headers.get(_rs.HEADER_MINER)
        try:
            h = int(self.headers.get(_rs.HEADER_HEIGHT))
        except (TypeError, ValueError):
            return None, "unsigned (test)"
        received.append((kind, key, body, mid, h))
        if self.headers.get(_rs.HEADER_SIG) != S._canon(kind, key, body, mid, h)[1].hex():
            return None, "SIGNATURE_INVALIDE (test)"
        named = key.rsplit("__", 1)[1] if "__" in key else None
        if named != mid:
            return None, "key/header mismatch (test)"
        if abs(h - S.ST.height) > S.ANTIREPLAY.fenetre:       # the replay guard's freshness, not a copy
            return None, "height out of the window (test)"
        return mid, "dendra1operator"

    monkeypatch.setattr(S.Handler, "_signed", fake_signed)
    orig_post = S.Handler.do_POST

    def counted_post(self):
        posts.append(self.path)
        return orig_post(self)

    monkeypatch.setattr(S.Handler, "do_POST", counted_post)
    httpd = S.ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/final-season/v1"
    import final_season_miner as M
    importlib.reload(M)
    # Hermetic: the identity the daemon resolved lives in the container's /data. A bench that read the
    # host's path would measure the machine it runs on.
    monkeypatch.setattr(M, "RESOLVED", str(tmp_path / "no-resolved-identity"))
    signed = []

    def sign(kind, key, body, mid, h):
        signed.append((kind, key, body, mid, h))
        return _headers(kind, key, body, mid, h)

    yield types.SimpleNamespace(S=S, M=M, base=base, sign=sign, signed=signed, received=received,
                                posts=posts, tmp=tmp_path)
    httpd.shutdown()


def payouts(S):
    return [r for r in S.ST.ev.read(S.ST.day_now()) if r["type"] == "payout"]


def closed_port() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/final-season/v1"


def outcome(M, argv):
    """The command's exit code, or 'raised' when it ends on an exception: neither is a success."""
    try:
        return M.main(argv)
    except Exception:  # noqa: BLE001
        return "raised"


def test_status_is_what_the_service_publishes(world, capsys):
    W = world
    code, st = W.M.status(W.base)
    assert code == 200
    assert st["height"] == HEIGHT and st["rules_fingerprint"] == W.S.fingerprint()
    assert st["rules"] == json.loads(json.dumps(W.S.RULES))
    capsys.readouterr()
    assert W.M.main(["status", "--programme", W.base + "/"]) == 0     # a trailing slash is the same address
    assert json.loads(capsys.readouterr().out) == st
    assert W.posts == []                                              # reading the status writes nothing


def test_a_payout_is_signed_over_the_body_it_sends_at_the_height_the_service_reports(world):
    W = world
    code, r = W.M.declare_payout(W.base, "m1", GOOD, sign=W.sign)
    assert code == 200 and r == {"ok": True}, r
    [(kind, key, body, mid, h)] = W.signed
    assert (kind, key, mid, h) == ("fspay", "payout__m1", "m1", HEIGHT)
    assert json.loads(body) == {"address": GOOD, "miner_id": "m1"}
    assert W.received == [("fspay", "payout__m1", body, "m1", HEIGHT)]   # what was signed is what arrived
    [rec] = payouts(W.S)
    assert (rec["miner_id"], rec["address"], rec["height"]) == ("m1", GOOD, HEIGHT)
    # The height is READ at each declaration, not kept: once the chain has moved, the next one follows it.
    W.S.ST.height = HEIGHT + 777
    assert W.M.declare_payout(W.base, "m2", GOOD, sign=W.sign)[0] == 200
    assert W.signed[-1][4] == HEIGHT + 777


def test_the_bench_verifier_says_no_where_the_real_one_does(world):
    # A verifier that accepts everything would make every test above pass on a command that signs the
    # wrong thing. Each altered part of the signed message must be refused, and nothing recorded.
    W = world
    cases = {
        "another body": lambda kind, key, body, mid, h: _headers(kind, key, body + b" ", mid, h),
        "another kind": lambda kind, key, body, mid, h: _headers("s1other", key, body, mid, h),
        "another key": lambda kind, key, body, mid, h: _headers(kind, "payout__m9", body, mid, h),
        "another miner": lambda kind, key, body, mid, h: _headers(kind, "payout__m9", body, "m9", h),
        "a stale height": lambda kind, key, body, mid, h:
            _headers(kind, key, body, mid, h - W.S.ANTIREPLAY.fenetre - 1),
        "no height": lambda kind, key, body, mid, h: {_rs.HEADER_MINER: mid},
    }
    for why, sign in cases.items():
        code, r = W.M.declare_payout(W.base, "m1", GOOD, sign=sign)
        assert code == 401, (why, code, r)
    assert payouts(W.S) == []
    assert W.M.declare_payout(W.base, "m1", GOOD, sign=W.sign)[0] == 200   # and the honest one passes


def test_a_declaration_that_cannot_be_signed_is_not_sent(world, monkeypatch):
    W = world
    code, r = W.M.declare_payout(W.base, "m1", GOOD, sign=lambda *a: {})
    assert code != 200 and "error" in r
    assert W.posts == [] and payouts(W.S) == []
    # through the command: the relay's signer returns {} when it has no key, and that is a failure
    import relay_client
    monkeypatch.setattr(relay_client, "_signature", lambda *a: {})
    assert W.M.main(["payout", "--miner", "m1", "--address", GOOD, "--programme", W.base]) == 1
    assert W.posts == [] and payouts(W.S) == []


def test_a_refused_declaration_is_not_reported_as_done(world, monkeypatch, capsys):
    W = world
    import relay_client
    from final_season_address import module_address
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    argv = ["payout", "--miner", "m1", "--programme", W.base, "--address"]
    assert W.M.main(argv + [module_address("fee_collector")]) == 1       # unpayable: the service says why
    assert "error" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payouts(W.S) == []
    assert W.M.main(argv + [GOOD]) == 0
    assert W.M.main(argv + [GOOD]) == 1                                   # one declaration per period
    assert len(payouts(W.S)) == 1


def test_a_declaration_outside_a_running_season_is_never_reported_as_done(world, monkeypatch, capsys):
    # The season ends at a header time, not after a count of days. Before the chain's time is read the
    # service cannot know which side of the end it stands on (425); once the latest block is at or past
    # the end, a declaration would apply to no day (410). The command must report both as failures, and
    # nothing may be recorded. Then, with the time read and before the end, the same declaration passes.
    W = world
    import relay_client
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    argv = ["payout", "--miner", "m1", "--address", GOOD, "--programme", W.base]
    for latest, http in ((None, 425), (W.S.END, 410), (W.S.END + 1, 410)):
        W.S.ST.latest_time = latest
        assert W.M.declare_payout(W.base, "m1", GOOD, sign=W.sign)[0] == http, latest
        capsys.readouterr()
        assert W.M.main(argv) == 1, latest
        assert "error" in json.loads(capsys.readouterr().out.strip().splitlines()[-1]), latest
        assert payouts(W.S) == [], latest
    W.S.ST.latest_time = W.S.END - 1                                      # the end is exclusive
    assert W.M.main(argv) == 0
    assert len(payouts(W.S)) == 1


def test_declarations_are_signed_with_the_resolved_identity(world, monkeypatch):
    # miner renames the key to the resolved dm1... id; DENDRA_SIGN_KEY still names the old 'm-...'
    # entry, which no longer exists. The command must sign as the resolved id and default --miner to it.
    W = world
    import relay_client
    pointed = []
    monkeypatch.setattr(relay_client, "set_sign_key", lambda name: pointed.append(name))
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    memo = W.tmp / "identite-resolue"
    memo.write_text("dm1abc\n", encoding="utf-8")
    monkeypatch.setattr(W.M, "RESOLVED", str(memo))
    assert W.M.main(["payout", "--address", GOOD.upper(), "--programme", W.base]) == 0
    assert pointed == ["dm1abc"] and [s[:2] for s in W.signed] == [("fspay", "payout__dm1abc")]
    [rec] = payouts(W.S)
    assert rec["miner_id"] == "dm1abc" and rec["address"] == GOOD          # stored lowercased
    # a memory that is not a resolved id is not adopted, and nothing is guessed in its place
    memo.write_text("m-0123abcd\n", encoding="utf-8")
    pointed.clear()
    assert W.M.main(["payout", "--address", GOOD, "--programme", W.base]) == 2
    assert pointed == [] and len(W.signed) == 1 and len(W.posts) == 1


def test_outside_the_container_the_named_miner_signs_with_the_start_up_key(world, monkeypatch):
    # No resolved identity means no rename happened: the key named at start-up is still the right one,
    # and re-pointing the signer at an empty name would leave the declaration unsigned.
    W = world
    import relay_client
    pointed = []
    monkeypatch.setattr(relay_client, "set_sign_key", lambda name: pointed.append(name))
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    assert W.M.main(["payout", "--address", GOOD, "--programme", W.base]) == 2   # and no id to declare for
    assert W.signed == [] and W.posts == []
    assert W.M.main(["payout", "--miner", "m1", "--address", GOOD, "--programme", W.base]) == 0
    assert pointed == [] and [s[:2] for s in W.signed] == [("fspay", "payout__m1")]


def test_the_programme_address_is_required_and_read_from_the_environment(world, monkeypatch):
    W = world
    assert W.M.main(["status"]) == 2                                      # no address: said, not guessed
    monkeypatch.setenv("DENDRA_FINAL_SEASON_URL", W.base + "/")
    importlib.reload(W.M)
    assert W.M.main(["status"]) == 0


def test_the_command_has_two_actions(world):
    W = world
    for action in ("node", "exam", "answer", "run"):
        with pytest.raises(SystemExit):
            W.M.main([action, "--programme", W.base])
    assert W.posts == []


def test_an_unreachable_programme_is_never_a_success(world, monkeypatch):
    W = world
    import relay_client
    monkeypatch.setattr(relay_client, "_signature", W.sign)
    gone = closed_port()
    assert outcome(W.M, ["status", "--programme", gone]) not in (0, None)
    assert outcome(W.M, ["payout", "--miner", "m1", "--address", GOOD, "--programme", gone]) not in (0, None)
    assert payouts(W.S) == []
