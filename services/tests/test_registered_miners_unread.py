"""Bench of `client.registered_miners` when the registry cannot be read, and of what its callers do
with that.

THE DEFECT. An unreadable `list-miner` returned [] with a line on stderr. Every consumer turned it into a
MEASUREMENT: the exporter set the miner count to zero, `network_state` handed a registry of nobody to the
CLI, and `submit_job` found no anchored key for any committee member — which strict mode refused under
the wrong cause, and the legacy opt-out answered by sealing the prompt to the RELAY's key, the
substitutable one the anchored key exists to replace. An outage of the node, read the reassuring way.

WHAT IS ASSERTED. None, never [], when the list is unreadable (a failed read, a row that is not an
object, a number that is not a number, an id or key that is not text, an operator balance that could not
be read); an empty chain still reads [] — the zero of a repeated field is a reading. A count keeps the
LAST list read: `network_state` publishes it and says how old it is. A KEY is never taken from it:
`submit_job` seals to the keys read NOW — the registry, else each committee member's own record — because
an operator can rotate its key after any earlier reading (`rotate-miner-keys`), and a prompt sealed to
the replaced key is one its miner cannot open and the old key's holder can. Nothing read now, nothing
sealed.

The chain is replaced at TWO seams — `list_all` (the paginated reader the shipped registry calls) and
`read_json` (the reader of one record: a miner, a balance) — and everything above them is the shipped
code. The last case replaces nothing in the client at all: a fake `dendrad` on a hermetic PATH answers,
and the shipped readers run end to end.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client as dc  # noqa: E402

ROWS = [{"minerId": "dm1a", "operator": "dendra1opa", "stake": "1000000", "demand": "7",
         "encPubkey": "aa" * 32},
        {"minerId": "dm1b", "operator": "dendra1opb", "stake": "2000000", "encPubkey": "bb" * 32}]


@pytest.fixture()
def chain(monkeypatch):
    """The chain as the client reads it.
      rows      what `list-miner` answers: a list of rows, or None for a read that failed;
      records   what `get-miner <id>` answers NOW: mid -> record, or None for a read that failed;
      bank      what `bank balances <addr>` answers: addr -> document, or None for a read that failed
                (an address not named answers 5 udndr)."""
    state = types.SimpleNamespace(rows=list(ROWS), reads=0, record_reads=[],
                                  records={r["minerId"]: dict(r) for r in ROWS}, bank={})

    def list_all(subcmd, field, t=60, max_pages=400):
        assert (subcmd, field) == ("list-miner", "miner"), (subcmd, field)
        state.reads += 1
        return None if state.rows is None else [dict(r) if isinstance(r, dict) else r for r in state.rows]

    def read_json(cmd, t=60):
        if cmd[:4] == ["dendrad", "query", "jobs", "get-miner"]:
            state.record_reads.append(cmd[4])
            rec = state.records.get(cmd[4])
            return None if rec is None else {"miner": dict(rec)}
        if cmd[:4] == ["dendrad", "query", "bank", "balances"]:
            if cmd[4] in state.bank:
                return state.bank[cmd[4]]
            return {"balances": [{"denom": "udndr", "amount": "5"}]}
        raise AssertionError(f"a chain read this bench does not stand in for: {cmd}")

    monkeypatch.setattr(dc, "list_all", list_all)
    monkeypatch.setattr(dc, "read_json", read_json)
    monkeypatch.setattr(dc, "balance", lambda addr, denom="udndr": 5)   # the exporter's emission read
    monkeypatch.setattr(dc, "pools", lambda: {})
    monkeypatch.setattr(dc, "height", lambda: 42)
    monkeypatch.setattr(dc, "_MINERS_LAST", {"rows": None, "at": None})   # the SHIPPED initial state
    return state


# ── the reader ──────────────────────────────────────────────────────────────────────────────────────
def test_an_unreadable_list_is_none_never_an_empty_registry(chain):
    chain.rows = None
    assert dc.registered_miners() is None


def test_an_empty_chain_is_still_an_empty_list(chain):
    chain.rows = []
    assert dc.registered_miners() == []        # read, and empty: a measurement, not a failure


@pytest.mark.parametrize("bad", ["dm1a", None, 7, ["dm1a"]])
def test_a_row_that_is_not_an_object_makes_the_list_unreadable(chain, bad):
    chain.rows = [ROWS[0], bad]
    assert dc.registered_miners() is None      # never a smaller registry than the one listed


def test_a_stake_that_is_not_a_number_makes_the_list_unreadable(chain):
    chain.rows = [dict(ROWS[0], stake="lots")]
    assert dc.registered_miners() is None


@pytest.mark.parametrize("field,value", [("encPubkey", 7), ("operator", ["x"]), ("minerId", None)])
def test_an_id_or_key_that_is_not_text_makes_the_list_unreadable(chain, field, value):
    chain.rows = [dict(ROWS[0], **{field: value}), ROWS[1]]
    assert dc.registered_miners() is None


def test_a_read_list_is_returned_whole_and_remembered(chain):
    got = dc.registered_miners()
    assert [m["id"] for m in got] == ["dm1a", "dm1b"] and got[0]["stake"] == 1_000_000
    assert got[1]["demand"] == 0               # absent IS zero inside a list that was read
    assert got[0]["balance"] == 5
    last, age = dc.last_registered_miners()
    assert last == got and age == 0
    last[0]["id"] = "edited"                   # a caller editing its copy edits nobody else's
    assert dc.last_registered_miners()[0][0]["id"] == "dm1a"


def test_a_failed_read_does_not_overwrite_the_last_reading(chain):
    dc.registered_miners()
    chain.rows = None
    assert dc.registered_miners() is None
    last, _age = dc.last_registered_miners()
    assert [m["id"] for m in last] == ["dm1a", "dm1b"]


# ── the operator balances inside the registry ───────────────────────────────────────────────────────
def test_an_operator_balance_that_cannot_be_read_keeps_its_row_with_none(chain, capsys):
    """`balance` answers 0 for a failed bank query; a registry built on it would remember that 0 as a
    reading for as long as the fallback lasts. So the balance is None -- and ONLY the balance: the chain does
    not check that an `operator` is an address (msg_server_miner.go::CreateMiner), so one registration naming
    an operator the bank cannot query used to make the WHOLE registry unreadable for every reader, for as long
    as that miner stayed registered (relecture adverse du 2026-10-09, with the real dendrad)."""
    chain.bank["dendra1opb"] = None
    got = dc.registered_miners()
    assert [m["id"] for m in got] == ["dm1a", "dm1b"], "one unread balance made the registry unreadable"
    assert got[0]["balance"] == 5 and got[1]["balance"] is None, "an unread balance is None, never 0"
    last, _age = dc.last_registered_miners()
    assert last[1]["balance"] is None, "the fallback keeps it unread, never as a 0"
    assert "1 operator balance(s) of 2 could not be read" in capsys.readouterr().err


def test_an_operator_that_is_not_an_address_leaves_the_registry_readable(chain):
    chain.rows = [ROWS[0], dict(ROWS[1], operator="not-an-address")]
    chain.bank["not-an-address"] = None        # `dendrad query bank balances not-an-address` exits 1
    got = dc.registered_miners()
    assert got is not None and len(got) == 2 and got[1]["balance"] is None


def test_an_operator_with_no_coin_reads_zero(chain):
    chain.bank["dendra1opb"] = {}              # proto3 omits an empty `balances`: a reading of zero
    got = dc.registered_miners()
    assert got is not None and got[1]["balance"] == 0


@pytest.mark.parametrize("doc,want", [
    ({"balances": [{"denom": "udndr", "amount": "12"}]}, 12),
    ({"balances": [{"denom": "other", "amount": "9"}]}, 0),
    ({"balances": [{"denom": "udndr"}]}, 0),           # an amount at zero is omitted: absent IS 0
    ({}, 0),
    ({"balances": [{"denom": "udndr", "amount": "1e3"}]}, None),
    ({"balances": [{"denom": "udndr", "amount": True}]}, None),
    ({"balances": "lots"}, None),
    ({"balances": ["udndr"]}, None),
    (None, None),                                      # the query itself failed
])
def test_balance_read_has_three_states(chain, doc, want):
    chain.bank["dendra1x"] = doc
    assert dc.balance_read("dendra1x") == want


# ── network_state ───────────────────────────────────────────────────────────────────────────────────
def test_network_state_keeps_the_last_registry_and_says_it_is_old(chain, capsys):
    st = dc.network_state()
    assert st["miners_read_now"] is True and len(st["miners"]) == 2
    chain.rows = None
    st = dc.network_state()
    assert len(st["miners"]) == 2, "an outage of the node published a registry of zero miners"
    assert st["miners_read_now"] is False and isinstance(st["miners_age_s"], int)
    assert "the last one read" in capsys.readouterr().err


def test_network_state_with_no_reading_ever_says_none(chain):
    chain.rows = None
    st = dc.network_state()
    assert st["miners"] is None and st["miners_read_now"] is False and st["miners_age_s"] is None


@pytest.fixture()
def exporter(chain, monkeypatch):
    """The SHIPPED exporter module, fed by the SHIPPED network_state. Only prometheus_client and the chain
    reads it makes besides are replaced; the R2 block is not these benches' (its throttle is pushed out)."""
    import importlib

    class _Gauge(object):
        def __init__(self, *a, **k):
            self.v, self.children = None, {}

        def set(self, v):
            self.v = v

        def labels(self, **kw):
            return self.children.setdefault(tuple(sorted(kw.items())), _Gauge())

        def clear(self):
            self.children.clear()

    fake = types.ModuleType("prometheus_client")
    fake.Gauge, fake.start_http_server = _Gauge, (lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "prometheus_client", fake)
    monkeypatch.delitem(sys.modules, "exporter", raising=False)
    ex = importlib.import_module("exporter")
    try:
        monkeypatch.setattr(ex.dc, "committee_seed_health", lambda: None)
        monkeypatch.setattr(ex, "_R2_NEXT", float("inf"))
        yield ex
    finally:
        sys.modules.pop("exporter", None)


def test_the_exporter_keeps_its_miner_count_through_an_unread_registry(chain, exporter):
    exporter.refresh()
    assert exporter.g_miners.v == 2
    chain.rows = None
    exporter.refresh()
    assert exporter.g_miners.v == 2, "the exporter published zero miners on an unread registry"


def test_the_exporter_with_no_registry_ever_read_refreshes_the_rest(chain, exporter, monkeypatch, capsys):
    """`miners` None (never read in this process): `len(None)` used to raise at every refresh, so the anti-
    grinding gauge after it was never evaluated and `dendra_chain_up` never set."""
    chain.rows = None
    monkeypatch.setattr(exporter.dc, "committee_seed_health",
                        lambda: {"source": 1, "has_seed": False, "contributors": 0, "min": 2, "power_bps": 0})
    walks = []
    monkeypatch.setattr(exporter.dc, "list_jobs_full", lambda: walks.append(1) or [])
    monkeypatch.setattr(exporter, "_R2_NEXT", 0.0)      # R2 due: it must still wait for the operators
    exporter.refresh()
    assert exporter.g_miners.v is None and exporter.g_total.v is None and exporter.g_r.v is None
    assert exporter.g_height.v == 42 and exporter.g_up.v == 1
    assert exporter.g_grind.v == 1, "the alarm after the registry gauges was evaluated"
    assert walks == [] and exporter._R2_NEXT == 0.0, "R2 over no known operator would count self-dealing"
    assert "has never been read" in capsys.readouterr().out


def test_the_exporter_sums_the_balances_read_and_counts_the_others(chain, exporter):
    chain.bank["dendra1opb"] = None
    exporter.refresh()
    assert exporter.g_miners.v == 2 and exporter.g_bal_unread.v == 1
    assert exporter.g_total.v == 5 / 1e6, "the total is the balances READ, never an unread one as 0"
    labelled = {k[0][1]: g.v for k, g in exporter.g_bal.children.items()}
    assert labelled == {"dm1a": 5 / 1e6}, "an unread balance publishes no series rather than a 0"
    chain.bank.pop("dendra1opb")
    exporter.refresh()
    assert exporter.g_bal_unread.v == 0 and exporter.g_total.v == 10 / 1e6


# ── the CLI and the leaderboard ─────────────────────────────────────────────────────────────────────
def test_the_cli_says_an_unread_registry_and_an_unread_balance(chain, capsys):
    import cli
    chain.bank["dendra1opb"] = None
    cli.cmd_state(None)
    out = capsys.readouterr().out
    assert "Registered miners: 2" in out and "balance=          ? DNDR" in out
    dc._MINERS_LAST.update(rows=None, at=None)
    chain.rows = None
    cli.cmd_state(None)
    assert "Registered miners: ? (the registry could not be read)" in capsys.readouterr().out


def test_the_leaderboard_is_not_computed_over_an_unread_registry(chain, monkeypatch):
    import points_indexer as pi
    monkeypatch.setattr(pi.dc, "list_jobs_full", lambda: [])
    chain.rows = None
    with pytest.raises(pi.Unmeasured):
        pi.snapshot()


# ── submit_job: the anchored keys ───────────────────────────────────────────────────────────────────
def _seams(monkeypatch):
    """`submit_job` up to the seal: open-job accepted, committee [dm1a], relay and sealing recorded."""
    rec = types.SimpleNamespace(sealed_to=[], relay_pub_reads=[], deposits=[])
    monkeypatch.setattr(dc, "tx_from", lambda *a, **k: "")
    monkeypatch.setattr(dc, "wait_tx_reason", lambda out, timeout=24: (True, ""))
    monkeypatch.setattr(dc, "assigned_committee", lambda jid, k=None, **kw: ["dm1a"])
    monkeypatch.setattr(dc, "get_beacon", lambda jid: "")

    class _Client(object):
        def submit(self, jid, pub, prompt):
            rec.sealed_to.append(pub.hex())
            sub = types.SimpleNamespace(client_eph_pk=bytes([1]), sealed_prompt=types.SimpleNamespace(
                nonce=bytes([2]), ct=bytes([3])))
            return sub, b"key"

    monkeypatch.setattr(dc, "Client", _Client)
    monkeypatch.setattr(dc.relay, "get", lambda base, kind, key, retries=1: rec.relay_pub_reads.append(key)
                        or {"pub": "cc" * 32})
    monkeypatch.setattr(dc.relay, "put_status", lambda base, kind, key, obj, **k: rec.deposits.append(key)
                        or "ok")
    return rec


@pytest.fixture()
def job(chain, monkeypatch):
    return _seams(monkeypatch)


def _submit():
    return dc.submit_job("Explain tides.", 500, "http://relay", client="gen", k=1, jid="job1")


def test_submit_on_a_registry_read_now_is_unchanged(chain, job):
    r = _submit()
    assert "error" not in r and job.sealed_to == ["aa" * 32] and job.deposits == ["job1__dm1a"], (r, job)
    assert chain.record_reads == [], "the registry was read: no member record is needed"


@pytest.mark.parametrize("optout", ["1", "0"])
def test_nothing_read_now_seals_nothing(chain, job, monkeypatch, optout):
    """The registry and the member's record both unreadable: refused, in strict mode AND under the
    legacy opt-out — an unknown key is not an absent one, and a reading from before is not used."""
    monkeypatch.setenv("DENDRA_REQUIRE_ONCHAIN_PUB", optout)
    dc.registered_miners()                     # an earlier reading exists: it must NOT be used
    chain.rows = None
    chain.records["dm1a"] = None
    r = _submit()
    assert "could be read" in r.get("error", "") and "NOT sealed" in r["error"], r
    assert job.sealed_to == [] and job.deposits == [] and job.relay_pub_reads == []


def test_a_key_rotated_since_the_last_reading_is_never_sealed_to(chain, job, capsys):
    """The case the stale reading got wrong: dm1a anchored aa..aa when the registry was last read, its
    operator rotated to dd..dd since, and the registry cannot be read now. The prompt goes to dd..dd."""
    dc.registered_miners()                     # the earlier reading: dm1a -> aa..aa
    chain.records["dm1a"] = dict(ROWS[0], encPubkey="dd" * 32)    # rotate-miner-keys, since
    chain.rows = None                          # ...and the registry read fails right now
    r = _submit()
    assert "error" not in r, r
    assert job.sealed_to == ["dd" * 32], "sealed to a key the chain no longer anchors"
    assert job.deposits == ["job1__dm1a"] and chain.record_reads == ["dm1a"]
    assert "its own on-chain record" in capsys.readouterr().out


def test_a_member_absent_from_the_last_reading_is_read_from_its_record(chain, job):
    chain.rows = [ROWS[1]]                     # the last reading predates dm1a
    dc.registered_miners()
    chain.rows = None
    r = _submit()
    assert "error" not in r and job.sealed_to == ["aa" * 32], (r, job)


def test_under_the_optout_a_key_anchored_since_is_used_not_the_relays(chain, job, monkeypatch):
    """The legacy opt-out falls back on the RELAY's key for a miner with none anchored. A reading from
    before dm1a anchored said "none"; it has anchored since: its anchored key, read now, is the one."""
    monkeypatch.setenv("DENDRA_REQUIRE_ONCHAIN_PUB", "0")
    chain.rows = [dict(ROWS[0], encPubkey=""), ROWS[1]]
    dc.registered_miners()                     # last reading: dm1a has no key
    chain.records["dm1a"] = dict(ROWS[0], encPubkey="ee" * 32)
    chain.rows = None
    r = _submit()
    assert "error" not in r and job.sealed_to == ["ee" * 32], (r, job)


def test_a_record_read_now_with_no_key_is_refused_in_strict_mode(chain, job):
    chain.rows = None
    rec = dict(ROWS[0])
    del rec["encPubkey"]                       # proto3 omits an empty key: absent IS "" (none anchored)
    chain.records["dm1a"] = rec
    r = _submit()
    assert "no on-chain anchored pubkey" in r.get("error", "") and job.sealed_to == [], r


def test_a_record_that_names_another_miner_is_not_read(chain, job):
    chain.rows = None
    chain.records["dm1a"] = dict(ROWS[1])      # an answer about dm1b, under dm1a's query
    r = _submit()
    assert "NOT sealed" in r.get("error", "") and job.sealed_to == [], r


def test_a_record_in_snake_case_is_read_too(chain, job):
    chain.rows = None
    chain.records["dm1a"] = {"miner_id": "dm1a", "operator": "dendra1opa", "enc_pubkey": "ab" * 32}
    r = _submit()
    assert "error" not in r and job.sealed_to == ["ab" * 32], (r, job)


def test_the_shipped_readers_against_a_fake_dendrad_on_a_hermetic_path(tmp_path, monkeypatch):
    """No reader of the client is replaced: `list_all`, `read_json`, `registered_miners` and
    `anchored_enc_pubkey` run as shipped, against a `dendrad` that fails `list-miner` and answers
    `get-miner dm1a` with a rotated key. The PATH holds that fake and nothing else, so no real binary
    can answer in its place."""
    nl = chr(10)
    b = tmp_path / "bin"
    b.mkdir()
    fake = b / "dendrad"
    rec = '{"miner":{"miner_id":"dm1a","operator":"dendra1opa","enc_pubkey":"' + "dd" * 32 + '"}}'
    fake.write_text("#!/bin/sh" + nl
                    + 'case "$1 $2 $3 $4" in' + nl
                    + '  "query jobs get-miner dm1a") printf %s ' + "'" + rec + "'" + '; exit 0 ;;' + nl
                    + "esac" + nl
                    + "exit 1" + nl, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(b))
    monkeypatch.setattr(dc, "NODE", "")
    monkeypatch.setattr(dc, "_MINERS_LAST", {"rows": [dict(ROWS[0], id="dm1a", enc_pubkey="aa" * 32)],
                                             "at": 0.0})
    job = _seams(monkeypatch)
    assert dc.registered_miners() is None      # list-miner fails through the shipped reader
    r = _submit()
    assert "error" not in r, r
    assert job.sealed_to == ["dd" * 32] and job.deposits == ["job1__dm1a"], job
