"""The faucet's DRIP: how much it sends, and in what order it refuses (faucet.py).

AN AMOUNT THAT FUNDS ONE IDENTITY. DENDRA_FAUCET_AMOUNT=min_stake sends the chain's `min_stake`, READ from the
chain: what one identity locks to register. The read is the shipped one (`dendrad query jobs params`, through
`faucet._run`, the only thing replaced here, with the chain's answers), and so is the transfer: the
bench reads the `bank send` the faucet built and checks it carries that amount. A min_stake that cannot be
read, or that reads 0 -- what an ABSENT field means, proto3 omitting a zero -- refuses the drip (503)
before any quota is spent: an amount is never guessed, and a drip of 0 is not a drip.

THE MOST SPECIFIC REFUSAL FIRST. An address in cooldown is told so, and an IP past its quota is told so, even
once the day's budget is gone: the miner daemon classifies the refusal by those words. And the order spends
nothing either way: the bench counts the distinct IPs it takes to spend the day, ceil(DAILY_CAP / IP_DAILY),
the same in both orders -- which is why the settings, not the order, decide it.

`DENDRA_MODEA_DIR` puts another copy of the faucet first on the path (the mutation bench
dendra/onchain-staging/dendra_saison_vague1_test.sh points it at a MUTATED copy and expects red).
"""
import importlib
import io
import json
import os
import sys
import threading
import types
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)

ADDR = "dendra1" + "q" * 38
MIN_STAKE = 1_000_000
# What dendrad writes for its OPERATOR: the node it reached, the keyring's home, the funding key. A refusal
# must carry none of it to the requester, and the log must carry all of it — both are asserted below on the
# SHIPPED handler, with these exact words standing for what a real dendrad prints.
OPERATOR_DETAIL = "Error: post failed: dial tcp chain:26657: connection refused (home /root/.dendra, key bob)"
LEAKS = ("chain:26657", "/root/.dendra", "key bob", "connection refused")


def leaked(reply):
    """The fragments of the operator's detail that reached the requester's JSON."""
    raw = json.dumps(reply)
    return [w for w in LEAKS if w in raw]


def _ok(stdout):
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


@pytest.fixture()
def fd(monkeypatch):
    if MODEA:
        while MODEA in sys.path:
            sys.path.remove(MODEA)
        sys.path.insert(0, MODEA)
    import faucet as F
    if MODEA:
        # Only for a copy under test: the module is shared with the other tests of a full run (miner
        # imports it), which a reload would hand fresh state they did not expect.
        importlib.reload(F)
    if MODEA and os.path.exists(os.path.join(MODEA, "faucet.py")):
        assert os.path.dirname(os.path.abspath(F.__file__)) == os.path.abspath(MODEA), F.__file__
    monkeypatch.setattr(F, "STATE_FILE", "")
    monkeypatch.setattr(F, "POW_BITS", 0)
    monkeypatch.setattr(F, "ADDR_COOLDOWN", 86400)
    monkeypatch.setattr(F, "IP_DAILY", 5)
    monkeypatch.setattr(F, "DAILY_CAP", 2000)
    monkeypatch.setattr(F, "_chain_id", lambda: "banc-1")
    F._addr_last.clear()
    F._ip_hits.clear()
    del F._global_hits[:]
    # The log's repeat bound is module state too: left filled, a detail logged by one test would be
    # held back in the next one, and an assertion on the log would measure the ORDER of the tests.
    if hasattr(F, "_log_last"):
        F._log_last.clear()
    chain = types.SimpleNamespace(params={"min_stake": str(MIN_STAKE)}, params_rc=0, sends=[], reads=0,
                                  send_rc=0, send_stderr="", send_raises=None)

    def run(cmd, t=60):
        if cmd[:4] == ["dendrad", "query", "jobs", "params"]:
            chain.reads += 1
            if chain.params_rc:
                return types.SimpleNamespace(returncode=chain.params_rc, stdout="", stderr=OPERATOR_DETAIL)
            return _ok(json.dumps({"params": chain.params}))
        if cmd[:4] == ["dendrad", "tx", "bank", "send"]:
            chain.sends.append(cmd)
            if chain.send_raises is not None:
                raise chain.send_raises
            if chain.send_rc:
                return types.SimpleNamespace(returncode=chain.send_rc, stdout="", stderr=chain.send_stderr)
            return _ok(json.dumps({"txhash": "AB" * 32}))
        if cmd[:3] == ["dendrad", "query", "tx"]:
            return _ok(json.dumps({"height": "9"}))          # code 0 is OMITTED, as the chain answers it
        raise AssertionError("unexpected command %r" % (cmd[:4],))

    monkeypatch.setattr(F, "_run", run)
    F._ms_cache.update(value=None, at=0.0)
    yield types.SimpleNamespace(F=F, chain=chain)
    # The anti-abuse state is the module's: left filled, it would refuse the next test file's drips.
    F._addr_last.clear()
    F._ip_hits.clear()
    del F._global_hits[:]
    F._ms_cache.update(value=None, at=0.0)
    if hasattr(F, "_log_last"):
        F._log_last.clear()


class _Fake(object):
    """What `H.do_POST` / `H.do_GET` read; the METHODS are the shipped ones."""

    def __init__(self, F, body, ip):
        b = json.dumps(body).encode()
        self.headers = {"Content-Length": str(len(b))}
        self.rfile = io.BytesIO(b)
        self.client_address = (ip, 0)
        self.out = []
        self.do_POST = types.MethodType(F.H.do_POST, self)
        self.do_GET = types.MethodType(F.H.do_GET, self)

    def _send(self, code, obj):
        self.out.append((code, obj))


def post(W, addr=ADDR, ip="1.2.3.4"):
    h = _Fake(W.F, {"address": addr}, ip)
    h.do_POST()
    return h.out[0]


def get(W):
    h = _Fake(W.F, {}, "1.2.3.4")
    h.do_GET()
    return h.out[0]


def amount_sent(cmd):
    return cmd[6]


# ── the amount ───────────────────────────────────────────────────────────────────────────────────────────
def test_min_stake_mode_sends_the_chains_min_stake_and_says_so(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "AMOUNT", "min_stake")
    code, r = post(fd)
    assert code == 200 and r["ok"] is True, (code, r)
    assert [amount_sent(c) for c in fd.chain.sends] == ["%dudndr" % MIN_STAKE]
    code, g = get(fd)
    assert g["amount"] == "%dudndr" % MIN_STAKE and g["amount_source"] == fd.F.MIN_STAKE_SOURCE, g


def test_min_stake_follows_the_chain_and_is_read_again_after_its_lifetime(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "AMOUNT", "min_stake")
    assert fd.F.drip_amount(now=1000.0)[0] == MIN_STAKE
    fd.chain.params["min_stake"] = "2500000"                     # a vote changed it
    assert fd.F.drip_amount(now=1000.0 + fd.F.MIN_STAKE_TTL_S - 1)[0] == MIN_STAKE   # cached
    assert fd.F.drip_amount(now=1000.0 + fd.F.MIN_STAKE_TTL_S + 1)[0] == 2_500_000
    assert fd.chain.reads == 2


@pytest.mark.parametrize("params,why", [
    ({}, "reads 0"),                                    # proto3 omitted it: absent IS 0, and 0 is no drip
    ({"min_stake": "0"}, "reads 0"),
    ({"min_stake": "lots"}, "not a whole number"),
])
def test_a_min_stake_of_zero_absent_or_malformed_refuses_and_spends_no_quota(fd, monkeypatch, capsys, params,
                                                                            why):
    monkeypatch.setattr(fd.F, "AMOUNT", "min_stake")
    fd.chain.params = params
    code, r = post(fd)
    assert code == 503 and r["info"] == fd.F.CAUSE_MIN_STAKE_UNUSABLE and fd.chain.sends == [], (code, r)
    assert why in capsys.readouterr().out, "the operator's detail did not reach the log"
    assert not fd.F._addr_last and not fd.F._ip_hits and not fd.F._global_hits     # nothing spent
    code, g = get(fd)
    assert g["amount"] is None and g["amount_unknown"], g


def test_a_chain_that_does_not_answer_refuses_and_spends_no_quota(fd, monkeypatch, capsys):
    monkeypatch.setattr(fd.F, "AMOUNT", "min_stake")
    fd.chain.params_rc = 1
    code, r = post(fd)
    assert code == 503 and r["info"] == fd.F.CAUSE_MIN_STAKE_UNREAD and fd.chain.sends == [], (code, r)
    # R3: dendrad's stderr is the OPERATOR's — it names the node and the keyring. The public 503 carries a
    # cause, the log carries the detail.
    assert leaked(r) == [], "the public 503 carries dendrad's output: %s" % leaked(r)
    log = capsys.readouterr().out
    assert "rc=1" in log and "chain:26657" in log, log
    assert not fd.F._addr_last and not fd.F._global_hits
    fd.chain.params_rc = 0                                       # the chain answers again: the drip goes
    assert post(fd)[0] == 200 and amount_sent(fd.chain.sends[0]) == "%dudndr" % MIN_STAKE


# ── what a failed transfer tells the requester ─────────────────────────────────────────────────────────────
def test_a_transfer_dendrad_refuses_is_a_502_with_a_cause_and_the_detail_in_the_log(fd, capsys):
    fd.chain.send_rc, fd.chain.send_stderr = 1, OPERATOR_DETAIL
    code, r = post(fd)
    assert (code, r["ok"], r["info"]) == (502, False, fd.F.TRANSFER_NOT_SENT), (code, r)
    assert leaked(r) == [], "the public 502 carries dendrad's output: %s" % leaked(r)
    assert "/root/.dendra" in capsys.readouterr().out
    assert not fd.F._addr_last, "the address's cooldown was spent on a transfer that did not happen"


def test_a_transfer_that_raises_is_a_502_with_a_cause_and_the_detail_in_the_log(fd, capsys):
    import subprocess as _sp
    # TimeoutExpired carries the whole argv in its text: the funding key and the node, here.
    fd.chain.send_raises = _sp.TimeoutExpired(["dendrad", "tx", "bank", "send", "bob", "--node", "tcp://chain:26657",
                                               "--home", "/root/.dendra"], 60)
    code, r = post(fd)
    assert (code, r["ok"]) == (502, False) and r["info"].startswith("transfer failed"), (code, r)
    assert leaked(r) == [], "the public 502 carries the exception's text: %s" % leaked(r)
    log = capsys.readouterr().out
    assert "TimeoutExpired" in log and "chain:26657" in log, log


def test_the_log_says_a_repeated_detail_once_then_counts_it(fd, capsys):
    F = fd.F
    assert F._log_detail("w", "same words", now=1000.0) is True
    assert F._log_detail("w", "same words", now=1001.0) is False           # a public endpoint does not
    assert F._log_detail("w", "same words", now=1002.0) is False           # decide how fast the log fills
    assert F._log_detail("w", "other words", now=1003.0) is True           # a CHANGE is said at once
    assert F._log_detail("w", "other words", now=1004.0) is False
    assert F._log_detail("w", "other words", now=1004.0 + F.LOG_REPEAT_S) is True
    out = capsys.readouterr().out
    assert out.count("same words") == 1 and "(and 1 more like it)" in out, out


def test_the_logged_detail_is_one_line_and_bounded(fd, capsys):
    """A subprocess's output spans lines nobody chose. Logged as is, its second line would read as a line
    the faucet wrote — a drip it never sent, say. One line per detail, bounded."""
    nl = chr(10)
    forged = "Error: rpc down" + nl + "[faucet] drip ok: sent to dendra1someone" + nl + "x" * 1000
    assert fd.F._log_detail("t", forged, now=5000.0) is True
    out = capsys.readouterr().out
    assert out.count(nl) == 1 and out.startswith("[faucet] t: Error: rpc down [faucet] drip ok"), out[:200]
    assert len(out) < 500, len(out)


def test_the_health_probe_never_queries_the_chain(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "AMOUNT", "min_stake")
    code, g = get(fd)
    assert code == 200 and g["amount"] is None and fd.chain.reads == 0 and g["pow_bits"] == 0, g


def test_the_probe_still_says_the_amount_once_it_is_older_than_its_lifetime(fd, monkeypatch):
    # The relecture: five minutes without a drip and the probe answered null, "not read from the chain yet",
    # about a value it HAD read; a check of the probe (the VPS gesture) went red on a healthy faucet. It says
    # the amount last read and its age; the next drip reads it again.
    import time as _t
    monkeypatch.setattr(fd.F, "AMOUNT", "min_stake")
    assert fd.F.drip_amount()[0] == MIN_STAKE and fd.chain.reads == 1
    fd.F._ms_cache["at"] = _t.monotonic() - fd.F.MIN_STAKE_TTL_S - 600          # read 15 minutes ago
    code, g = get(fd)
    assert code == 200 and g["amount"] == "%dudndr" % MIN_STAKE, g
    assert fd.F.MIN_STAKE_TTL_S + 600 <= g["amount_read_s_ago"] <= fd.F.MIN_STAKE_TTL_S + 660, g
    assert fd.chain.reads == 1                                                  # the probe read nothing
    fd.chain.params["min_stake"] = "3000000"                                    # a vote changed it since
    assert post(fd)[0] == 200 and amount_sent(fd.chain.sends[-1]) == "3000000udndr"
    assert fd.chain.reads == 2 and get(fd)[1]["amount_read_s_ago"] <= 60


def test_a_fixed_amount_is_sent_as_set_without_reading_the_chain(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "AMOUNT", "10000000")
    assert post(fd)[0] == 200
    assert amount_sent(fd.chain.sends[0]) == "10000000udndr" and fd.chain.reads == 0
    code, g = get(fd)
    assert g["amount"] == "10000000udndr" and g["amount_source"] == "DENDRA_FAUCET_AMOUNT"


@pytest.mark.parametrize("setting", ["0", "-5", "abc", "", "1e6", chr(0x661) + chr(0x660)])   # last: non-ASCII digits
def test_an_unusable_amount_setting_is_named_and_refused(fd, setting):
    assert fd.F.amount_setting_error(setting)
    assert fd.F.amount_setting_error("min_stake") == "" and fd.F.amount_setting_error("1") == ""


def test_fund_refuses_a_non_positive_amount(fd):
    for bad in (0, -1, True):
        ok, why = fd.F.fund(ADDR, bad)
        assert ok is False and "not a positive" in why
    assert fd.chain.sends == []


# ── the order of the refusals ─────────────────────────────────────────────────────────────────────────────
def test_an_address_in_cooldown_is_told_so_even_once_the_day_is_spent(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "DAILY_CAP", 1)
    assert post(fd, ADDR, "1.1.1.1")[0] == 200                   # the day's only drip
    code, r = post(fd, ADDR, "1.1.1.1")
    assert (code, r["info"]) == (429, fd.F.REFUSAL_ADDR_COOLDOWN), r


def test_an_ip_past_its_quota_is_told_so_even_once_the_day_is_spent(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "DAILY_CAP", 2)
    monkeypatch.setattr(fd.F, "IP_DAILY", 2)
    assert post(fd, "dendra1" + "a" * 38, "2.2.2.2")[0] == 200
    assert post(fd, "dendra1" + "b" * 38, "2.2.2.2")[0] == 200
    code, r = post(fd, "dendra1" + "c" * 38, "2.2.2.2")
    assert (code, r["info"]) == (429, fd.F.REFUSAL_IP_QUOTA), r
    code, r = post(fd, "dendra1" + "d" * 38, "3.3.3.3")         # a fresh IP: the day's budget
    assert (code, r["info"]) == (429, fd.F.REFUSAL_GLOBAL_CAP), r


def test_a_refused_request_spends_nothing(fd, monkeypatch):
    monkeypatch.setattr(fd.F, "IP_DAILY", 1)
    assert post(fd, "dendra1" + "a" * 38, "4.4.4.4")[0] == 200
    before = (dict(fd.F._addr_last), {k: list(v) for k, v in fd.F._ip_hits.items()}, list(fd.F._global_hits))
    for _ in range(5):
        assert post(fd, "dendra1" + "b" * 38, "4.4.4.4")[0] == 429
    assert (dict(fd.F._addr_last), {k: list(v) for k, v in fd.F._ip_hits.items()}, list(fd.F._global_hits)) == before


def _ips_that_spend_the_day(F):
    """Distinct IPs, each asking for one drip more than its quota, that obtain a drip before the day's cap
    refuses the first request."""
    spenders, n = 0, 0
    while True:
        ip = "10.0.%d.%d" % (n // 250, n % 250)
        n += 1
        got = 0
        for i in range(F.IP_DAILY + 1):
            ok, why = F._rate_ok("dendra1%038x" % (n * 1000 + i), ip)
            if ok:
                got += 1
            elif why == F.REFUSAL_GLOBAL_CAP:
                return spenders + (1 if got else 0)
        spenders += 1 if got else 0


def test_the_days_budget_is_spent_by_ceil_daily_cap_over_ip_daily_ips(fd, monkeypatch):
    # What the order does not change, measured: the number of distinct IPs that spend the day. Only the
    # settings move it (see the module's header and faucet._rate_ok).
    for cap, per_ip in ((2000, 5), (10, 3), (7, 7), (20000, 3)):
        monkeypatch.setattr(fd.F, "DAILY_CAP", cap)
        monkeypatch.setattr(fd.F, "IP_DAILY", per_ip)
        fd.F._addr_last.clear()
        fd.F._ip_hits.clear()
        del fd.F._global_hits[:]
        assert _ips_that_spend_the_day(fd.F) == -(-cap // per_ip), (cap, per_ip)
