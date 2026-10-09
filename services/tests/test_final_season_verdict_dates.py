"""F3: the dates that decide whether a juror's verdict counts, read by the SHIPPED readers.

The chain accepts a verdict commit after the audit resolved (`msg_server_commit.go::CreateCommit` checks
neither an open audit nor the jury), so the programme dates each verdict itself: the block that DREW the jury
(`audit_requested`), the block that RESOLVED the audit (`audit_expired` at the end of a block, or an accepted
MsgAdjudicateDispute naming the job), and the block of the accepted MsgCreateCommit that anchored the verdict,
signed by the juror's operator. Only the network is replaced here: the node's RPC (`final_season_chain._get`)
and the transaction index (`final_season_chain._cli`), answering as the chain answers -- an accepted
transaction carries no `code` (proto3 omits a zero). The decision itself, `final_season_facts.timely_verdicts`,
is pure and benched on its own below.

`DENDRA_MODEA_DIR` puts another copy of the modules first on the path: the mutation bench
(dendra/onchain-staging/dendra_saison_vague1_test.sh) points it at a MUTATED copy and expects red.
"""
import importlib
import json
import os
import re
import sys
import types
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEA = os.environ.get("DENDRA_MODEA_DIR", "")
if MODEA:
    sys.path.insert(0, MODEA)

RPC = "http://n:26657"
NODE = "tcp://n:26657"
J1 = "dendra1" + "a" * 38
J2 = "dendra1" + "b" * 38


@pytest.fixture()
def mods():
    if MODEA:
        while MODEA in sys.path:
            sys.path.remove(MODEA)
        sys.path.insert(0, MODEA)
    import final_season_chain as C
    import final_season_facts as F
    if MODEA:
        importlib.reload(C)
        importlib.reload(F)
        for m in (C, F):
            if os.path.exists(os.path.join(MODEA, os.path.basename(m.__file__))):
                assert os.path.dirname(os.path.abspath(m.__file__)) == os.path.abspath(MODEA), m.__file__
    return types.SimpleNamespace(C=C, F=F)


class Node:
    """The node's RPC: `block_search` over END-OF-BLOCK events, `block_results` per height."""

    def __init__(self, events):
        self.events = events          # {height: [(type, {attrs})]}
        self.reads = []

    def get(self, url, timeout=10.0):
        self.reads.append(url)
        m = re.fullmatch(re.escape(RPC) + r"/block_search\?query=([^&]+)&per_page=100&page=(\d+)&order_by=%22asc%22", url)
        if m:
            q = urllib.parse.unquote(m.group(1))
            mm = re.fullmatch(r'"(\w+)\.job_id=\'([^\']+)\'"', q)
            assert mm, q
            typ, jid = mm.groups()
            hs = sorted(h for h, evs in self.events.items()
                        if any(t == typ and a.get("job_id") == jid for t, a in evs))
            page = int(m.group(2))
            chunk = hs[(page - 1) * 100: page * 100]
            return {"result": {"blocks": [{"block": {"header": {"height": str(h)}}} for h in chunk],
                               "total_count": str(len(hs))}}
        m = re.fullmatch(re.escape(RPC) + r"/block_results\?height=(\d+)", url)
        if m:
            evs = self.events.get(int(m.group(1)), [])
            return {"result": {"finalize_block_events": [
                {"type": t, "attributes": [{"key": k, "value": v} for k, v in a.items()]} for t, a in evs]}}
        raise AssertionError("unexpected read " + url)


class Index:
    """The transaction index, as `dendrad query txs --query ...` answers it."""

    def __init__(self, txs):
        self.txs = txs                # [(height, code or None, [messages], {event type: {attrs}})]
        self.queries = []

    def cli(self, args, node, timeout=60):
        assert args[:3] == ["query", "txs", "--query"], args
        q = args[3]
        self.queries.append(q)
        conds = re.findall(r"([\w.]+)(>=|<=|=)'?([^' ]+)'?", q)
        out = []
        for h, code, msgs, events in self.txs:
            ok = True
            for k, op, v in conds:
                if k == "tx.height":
                    ok &= (h >= int(v)) if op == ">=" else (h <= int(v))
                elif k == "message.action":
                    ok &= any(m.get("@type") == v for m in msgs)
                elif k == "message.sender":
                    ok &= any(m.get("creator") == v for m in msgs)
                else:
                    typ, attr = k.split(".", 1)
                    ok &= events.get(typ, {}).get(attr) == v
            if ok:
                t = {"height": str(h), "tx": {"body": {"messages": msgs}}}
                if code is not None:
                    t["code"] = code
                out.append(t)
        page = int(args[args.index("--page") + 1])
        return {"txs": out[(page - 1) * 100: page * 100], "total_count": str(len(out))}


def commit_msg(key, creator, vote="1"):
    return {"@type": "/dendra.jobs.v1.MsgCreateCommit", "creator": creator, "job_id": key,
            "prompt_commit": "", "result_commit": vote, "kind": "verdict"}


def adjudicate_msg(jid):
    return {"@type": "/dendra.jobs.v1.MsgAdjudicateDispute", "creator": J2, "job_id": jid}


# ── the draw ────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_draw_is_read_from_its_block_and_confirmed_in_its_results(mods, monkeypatch):
    node = Node({20: [("audit_requested", {"job_id": "x2", "committee": "m1, m2,m3"}),
                      ("audit_requested", {"job_id": "x9", "committee": "z"})],
                 30: [("audit_draw_deferred", {"job_id": "x2"})]})
    monkeypatch.setattr(mods.C, "_get", node.get)
    assert mods.C.audit_draw(RPC, "x2") == (20, ["m1", "m2", "m3"])
    assert mods.C.audit_committee(RPC, "x2") == ["m1", "m2", "m3"]
    assert mods.C.audit_draw(RPC, "x5") == (None, [])


def test_every_page_of_a_block_search_is_read(mods, monkeypatch):
    node = Node({h: [("audit_expired", {"job_id": "x2"})] for h in range(1000, 1150)})
    monkeypatch.setattr(mods.C, "_get", node.get)
    assert [h for h, _ in mods.C._end_block_events(RPC, "audit_expired", "x2")] == list(range(1000, 1150))
    assert sum("block_search" in u for u in node.reads) == 2


def test_a_block_search_without_its_count_is_refused(mods, monkeypatch):
    monkeypatch.setattr(mods.C, "_get", lambda url, timeout=10.0: {"result": {"blocks": []}})
    with pytest.raises(mods.C.ChainUnreadable, match="total_count"):
        mods.C.audit_draw(RPC, "x2")


@pytest.mark.parametrize("jid", ["x2' OR 'a'='a", "x 2", "", "x\"2"])
def test_a_job_id_that_would_change_the_query_is_never_searched(mods, monkeypatch, jid):
    monkeypatch.setattr(mods.C, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched")))
    with pytest.raises(mods.C.ChainUnreadable, match="cannot be searched"):
        mods.C.audit_draw(RPC, jid)


# ── the resolution ──────────────────────────────────────────────────────────────────────────────────────────
def test_an_end_of_block_resolution_is_read_from_audit_expired(mods, monkeypatch):
    monkeypatch.setattr(mods.C, "_get", Node({260: [("audit_expired", {"job_id": "x2"})]}).get)
    monkeypatch.setattr(mods.C, "_cli", Index([]).cli)
    assert mods.C.audit_resolution(NODE, RPC, "x2") == (260, mods.C.END_BLOCK)


def test_an_adjudication_is_read_from_its_accepted_transaction(mods, monkeypatch):
    monkeypatch.setattr(mods.C, "_get", Node({}).get)
    idx = Index([
        (150, 5, [adjudicate_msg("x2")], {"redo_participation": {"job_id": "x2"}}),        # refused: not it
        (170, None, [adjudicate_msg("x2")], {"redo_participation": {"job_id": "x2"}}),     # accepted, code omitted
        (180, None, [adjudicate_msg("x7")], {"redo_participation": {"job_id": "x7"}}),     # another job
    ])
    monkeypatch.setattr(mods.C, "_cli", idx.cli)
    assert mods.C.audit_resolution(NODE, RPC, "x2") == (170, mods.C.IN_TX)
    assert "redo_participation.job_id='x2'" in idx.queries[0]


def test_no_resolution_in_the_index_is_none_and_a_failed_search_raises(mods, monkeypatch):
    monkeypatch.setattr(mods.C, "_get", Node({}).get)
    monkeypatch.setattr(mods.C, "_cli", Index([]).cli)
    assert mods.C.audit_resolution(NODE, RPC, "x2") is None

    def down(args, node, timeout=60):
        raise mods.C.ChainUnreadable("query txs: rc=1")
    monkeypatch.setattr(mods.C, "_cli", down)
    with pytest.raises(mods.C.ChainUnreadable):
        mods.C.audit_resolution(NODE, RPC, "x2")


# ── the commit ──────────────────────────────────────────────────────────────────────────────────────────────
def test_a_verdict_is_dated_by_its_accepted_commit_signed_by_its_juror(mods, monkeypatch):
    key = "x2__verdict__m1"
    idx = Index([
        (90, 18, [commit_msg(key, J1)], {}),                          # refused by the chain: not it
        (95, None, [commit_msg("x2__verdict__m9", J1)], {}),          # another key, same signer
        (98, None, [commit_msg(key, J2)], {}),                        # same key, another signer
        # found by the signer's search through ANOTHER message of the same transaction: the key's message is
        # not J1's, so it does not date J1's verdict
        (99, None, [commit_msg("x2__verdict__m8", J1), commit_msg(key, J2)], {}),
        (300, None, [commit_msg(key, J1)], {}),                       # THE commit
    ])
    monkeypatch.setattr(mods.C, "_cli", idx.cli)
    assert mods.C.commit_height(NODE, key, J1, 20, 5000) == 300
    q = idx.queries[-1]
    assert "message.action='/dendra.jobs.v1.MsgCreateCommit'" in q and f"message.sender='{J1}'" in q
    assert "tx.height>=20" in q and "tx.height<=5000" in q
    assert mods.C.commit_height(NODE, key, J1, 20, 299) is None     # outside the window: not found there


def test_a_juror_who_floods_the_search_is_answered_search_too_long_after_one_page(mods, monkeypatch):
    # The relecture's replay: more accepted commits by this signer in the window than the reader reads (they
    # need no job: an epoch's reveal marker, at zero gas). The answer is the index's own count, read on the
    # FIRST page: one query, then SearchTooLong -- a ChainUnreadable for any caller that does not name it.
    pages = []

    def flooded(args, node, timeout=60):
        page = int(args[args.index("--page") + 1])
        pages.append(page)
        txs = [{"height": str(100 + page), "tx": {"body": {"messages": [
            commit_msg(f"e{page * 100 + i}__reveals__dm1x", J1)]}}} for i in range(100)]
        return {"total_count": "20001", "txs": txs}
    monkeypatch.setattr(mods.C, "_cli", flooded)
    with pytest.raises(mods.C.SearchTooLong) as e:
        mods.C.commit_height(NODE, "x2__verdict__m1", J1, 20, 40000)
    assert pages == [1] and e.value.total == 20001 and isinstance(e.value, mods.C.ChainUnreadable)


def test_a_search_that_fits_its_pages_is_read_to_its_end(mods, monkeypatch):
    # The bound is the count, never a page that happens to be full: exactly max_pages x 100 is read whole.
    key = "x2__verdict__m1"
    idx = Index([(100 + i, None, [commit_msg(f"e{i}__reveals__dm1x", J1)], {}) for i in range(299)]
                + [(5000, None, [commit_msg(key, J1)], {})])
    monkeypatch.setattr(mods.C, "_cli", idx.cli)
    assert mods.C._accepted_txs(NODE, f"message.sender='{J1}'", max_pages=3) and len(idx.queries) == 3
    with pytest.raises(mods.C.SearchTooLong):
        mods.C._accepted_txs(NODE, f"message.sender='{J1}'", max_pages=2)


def test_a_commit_signer_that_is_not_an_address_is_never_searched(mods, monkeypatch):
    monkeypatch.setattr(mods.C, "_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched")))
    for bad in ("", "dendra1' OR 1=1", "cosmos1abc", None):
        with pytest.raises(mods.C.ChainUnreadable):
            mods.C.commit_height(NODE, "x2__verdict__m1", bad, 1, 10)


def test_the_verdict_commits_carry_their_signer(mods, monkeypatch):
    def run(cmd, capture_output=True, text=True, timeout=30):
        key = cmd[4]
        if key == "x2__verdict__m1":
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"commit": {"creator": J1, "job_id": key, "result_commit": "1"}}))
        return types.SimpleNamespace(returncode=1, stdout="", stderr="rpc error: code = NotFound desc = not found")
    monkeypatch.setattr(mods.C.subprocess, "run", run)
    got = mods.C.verdict_commits(NODE, [{"id": "x2", "miner_id": "p"}], {"x2": ["p", "m1", "m2"]})
    assert got == {"x2": {"m1": {"vote": "1", "creator": J1}}}
    assert mods.C.verdicts(NODE, [{"id": "x2", "miner_id": "p"}], {"x2": ["m1"]}) == {"x2": {"m1": "1"}}


# ── the decision (pure) ────────────────────────────────────────────────────────────────────────────────────
def _commits(*jurors):
    return {"x2": {m: {"vote": "1", "creator": "dendra1op" + m} for m in jurors}}


def test_only_a_verdict_committed_before_the_resolution_counts(mods):
    F = mods.F
    counted, proof = F.timely_verdicts(_commits("a", "b", "c"), {"x2": (260, F.END_BLOCK)},
                                       {"x2": {"a": 259, "b": 260, "c": 261}})
    assert counted == {"x2": {"a": "1", "b": "1"}}
    assert proof["x2"]["jurors"]["c"] == {"vote": "1", "committed_at": 261, "counted": False,
                                          "why": F.AFTER_RESOLUTION}
    counted, _ = F.timely_verdicts(_commits("a", "b"), {"x2": (260, F.IN_TX)}, {"x2": {"a": 259, "b": 260}})
    assert counted == {"x2": {"a": "1"}}                 # inside an adjudication's block: not known to count


def test_an_unknown_date_is_never_counted(mods):
    F = mods.F
    counted, proof = F.timely_verdicts(_commits("a"), {"x2": None}, {"x2": {"a": 10}})
    assert counted == {} and proof["x2"]["jurors"]["a"]["why"] == F.RESOLUTION_NOT_INDEXED
    # no commit transaction in the day's window: not counted, and not listed (a recompute must find the same)
    counted, proof = F.timely_verdicts(_commits("a", "b"), {"x2": (260, F.END_BLOCK)}, {"x2": {"a": None, "b": 10}})
    assert counted == {"x2": {"b": "1"}} and "a" not in proof["x2"]["jurors"]


def test_a_commit_not_dated_is_listed_and_never_counted(mods):
    F = mods.F
    counted, proof = F.timely_verdicts(_commits("a", "b"), {"x2": (260, F.END_BLOCK)},
                                       {"x2": {"a": F.NOT_DATED, "b": 10}})
    assert counted == {"x2": {"b": "1"}}
    assert proof["x2"]["jurors"]["a"] == {"vote": "1", "committed_at": None, "counted": False,
                                          "why": F.COMMIT_NOT_DATED}
    # Three states, and only three: anything else is not a date.
    for bad in ("12", True, 1.5):
        with pytest.raises(ValueError):
            F.timely_verdicts(_commits("a"), {"x2": (260, F.END_BLOCK)}, {"x2": {"a": bad}})


def test_dates_that_were_not_read_are_an_error_never_a_verdict(mods):
    F = mods.F
    with pytest.raises(KeyError):
        F.timely_verdicts(_commits("a"), {}, {"x2": {"a": 10}})
    with pytest.raises(KeyError):
        F.timely_verdicts(_commits("a"), {"x2": (260, F.END_BLOCK)}, {"x2": {}})
    with pytest.raises(ValueError):
        F.timely_verdicts(_commits("a"), {"x2": (260, "somewhere")}, {"x2": {"a": 10}})
