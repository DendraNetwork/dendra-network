"""Bench of the day ranking with the chain replaced: work counts the generator's jobs only, verdicts count
for DRAWN jurors on the generator's audited jobs only, presence is the availability windows proven on chain
during the day and is paid only on a day of verified work, an index that does not reach the day is refused,
a quorum audit of a programme job whose draw is missing is refused, a presence read that fails and a window
length of 0 are refused (never ranked as presence 0 for everyone), and the result is deterministic.

The season ends at an instant read from the block HEADERS: the fake node serves `/block?height=N` header
times, all long before the end unless a test places the end (`chain["cut"]`, the first block at or after
it). The day that holds the end counts only the blocks before it, is final from the season's last block
(not from its nominal end), and names that block; a day after the end is refused, never ranked empty."""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import final_season_chain as C  # noqa: E402
import final_season_rank as RK  # noqa: E402
from final_season_rules import RULES, day_bounds, end_epoch  # noqa: E402

GEN = "dendra1generator"
NODE = "tcp://n:26657"
RPC = C.rpc_url(NODE)
DAY = RULES["day_blocks"]
START = 1
FIRST, LAST = day_bounds(0, START)
EPOCH = 288
FINALITY = 100                       # what `rank` hands in
TIP = 10 * DAY                       # the chain's height in `rank`, unless a test says otherwise
END = end_epoch()
# A season that ends INSIDE day 0: CUT is the first block at or after the end, SEASON_END the last before it.
CUT = FIRST + 1000
SEASON_END = CUT - 1


def header_time(h, cut):
    """The header time the fake node serves for block `h`; `cut` is the first block at or after the end
    (None: every block of the bench is long before it). Around the end the times carry nanoseconds a text
    comparison would misplace: the season's last block reads 23:59:59.999999999, the first one out
    00:00:00.5, which sorts BEFORE '00:00:00Z' as text ('.' < 'Z'), so only a parsed time puts it out."""
    if cut is None:
        t, frac = END - 100 * 86400 + 5 * h, ""
    elif h < cut:
        t, frac = END - 5 * (cut - h) + 4, ".999999999"
    else:
        t, frac = END + 5 * (h - cut), ".5"
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + frac + "Z"
# The presence reader the module ships, kept before any fixture replaces it: the tests that drive it
# through `rank_day` replace only the command line underneath it (`final_season_chain._cli`).
REAL_PRESENCE = C.presence_proofs
# What a published ranking row carries (`final_season_calc.to_json`), no more and no less.
ROW_FIELDS = {"rank", "miner_id", "payout_address", "presence", "verified_requests", "verdicts",
              "gross_udndr", "payable_udndr", "paid_udndr", "reason"}


@pytest.fixture()
def chain(monkeypatch):
    jobs = [
        {"id": "g1", "state": "open+paid+optimistic", "miner_id": "m1", "client": GEN, "slashed_primary": False},
        {"id": "x1", "state": "open+paid+optimistic", "miner_id": "m1", "client": "dendra1someone",
         "slashed_primary": False},
        {"id": "x2", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "miner_id": "m2",
         "client": GEN, "slashed_primary": False},
        {"id": "x3", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "miner_id": "m2",
         "client": "dendra1someone", "slashed_primary": False},
    ]
    calls = {"index": [], "presence": [], "jobs": jobs, "tip": TIP, "cut": None,
             "settled": {"g1": 10, "x1": 11, "x2": 12, "x3": 13}}

    def settle_heights(node, sender=None, max_pages=2000):
        calls["sender"] = sender
        return dict(calls["settled"])

    def verdicts(node, audited, candidates):
        calls["candidates"] = candidates
        # every registered miner posted a matching "0", drawn or not
        return {j["id"]: {m: "0" for m in candidates.get(j["id"], [])} for j in audited}

    def presence_proofs(node, first, last, epoch_blocks):
        calls["presence"].append((node, first, last, epoch_blocks))
        # m1 served a programme request; j1 and x only judged; p only proved windows. Each proof was
        # signed by the operator the registry names, unless a test says otherwise.
        windows = calls.get("windows", {"m1": 3, "j1": 2, "x": 1, "p": 4})
        return windows, {m: calls.get("signers", {}).get(m, "dendra1op" + m) for m in windows}

    def no_network(*a, **k):
        # AssertionError, not ChainUnreadable: a read nobody replaced must fail a test that expects a
        # refusal, never pass it by refusing for the wrong reason
        raise AssertionError(f"unexpected network read {a[:1]}")

    def node_get(url, timeout=10.0):
        # The node's RPC: block headers and its status. A block above the tip does not exist yet, and one
        # below 1 never did: reading either is the code asking for something no node can answer.
        m = re.fullmatch(re.escape(RPC) + r"/block\?height=(\d+)", url)
        if m:
            h = int(m.group(1))
            if not 1 <= h <= calls["tip"]:
                raise AssertionError(f"block {h} read, the chain is at {calls['tip']}")
            return {"result": {"block": {"header": {"height": str(h), "time": header_time(h, calls["cut"])}}}}
        if url == RPC + "/status":
            return {"result": {"sync_info": {"latest_block_height": str(calls["tip"]), "earliest_block_height": "1",
                                             "latest_block_time": header_time(calls["tip"], calls["cut"])}}}
        return no_network(url)

    monkeypatch.setattr(C, "jobs", lambda node: calls["jobs"])
    calls["registry"] = ["m1", "m2", "j1", "x", "p"]
    monkeypatch.setattr(C, "miners",
                        lambda node: [{"id": m, "operator": "dendra1op" + m} for m in calls["registry"]])
    monkeypatch.setattr(C, "settle_heights", settle_heights)
    monkeypatch.setattr(C, "verdicts", verdicts)
    monkeypatch.setattr(C, "audit_committee",
                        lambda rpc, jid: {"x2": ["j1", "m2"], "x3": ["j1", "x"]}.get(jid, []))
    monkeypatch.setattr(C, "require_index_from", lambda rpc, h: calls["index"].append(h))
    monkeypatch.setattr(C, "presence_proofs", presence_proofs)
    monkeypatch.setattr(C, "_cli", no_network)
    monkeypatch.setattr(C, "_get", node_get)
    return calls


def records(coherent=True, answered=("g1",), payout=True):
    """The evidence of day 0: m1 declared a payout address and its one sampled programme answer was graded;
    the seal lists the programme jobs whose answer reached the programme. No record makes an identity
    rank: only the chain's facts do."""
    out = [{"type": "payout", "day": 0, "miner_id": "m1", "address": "dendra1payee", "height": 5}] if payout else []
    return out + [
        {"type": "work_answer", "day": 0, "miner_id": "m1", "job_id": "g1", "height": 10,
         "prompt": "p", "answer": "a"},
        {"type": "work_grade", "day": 0, "miner_id": "m1", "job_id": "g1", "coherent": coherent,
         "model": "judge"},
        {"type": "work_sealed", "day": 0, "answers": 1, "drawn": 1, "answered": {"m1": list(answered)}}]


def rank(recs=None, epoch=EPOCH, height=TIP, day=0):
    return RK.rank_day(day, records() if recs is None else recs, NODE, GEN, START, 0, FINALITY, height, epoch)


def rank_at(chain, height, day=0, recs=None):
    """`rank` with the chain at `height`: the fake node then serves no block above it."""
    chain["tip"] = height
    return rank(recs, height=height, day=day)


def avail_tx(h, mid, code=None, also=()):
    """An availability proof as the transaction index returns it, signed by the registry's operator."""
    t = {"height": str(h), "tx": {"body": {"messages": [
        {"@type": C.AVAIL_ACTION, "creator": "dendra1op" + mid, "miner_id": mid}, *also]}}}
    if code is not None:
        t["code"] = code
    return t


def serve_proofs(monkeypatch, txs):
    """The shipped presence reader over a fake index answering `txs`; returns the queries it was sent."""
    queries = []

    def cli(args, node, timeout=60):
        queries.append(args)
        return {"txs": txs, "total_count": str(len(txs))}
    monkeypatch.setattr(C, "presence_proofs", REAL_PRESENCE)
    monkeypatch.setattr(C, "_cli", cli)
    return queries


def by_id(out):
    return {r["miner_id"]: r for r in out["ranking"]}


def test_work_is_the_generators_and_verdicts_are_the_drawn_jurors(chain):
    rows = by_id(rank())
    assert chain["sender"] is None and chain["index"] == [FIRST]
    # only the drawn jury of a PROGRAMME job is ever read: x3, someone else's job, is not looked at
    assert chain["candidates"] == {"x2": ["j1", "m2"]}
    assert rows["m1"]["verified_requests"] == 1               # g1 only; x1 is not a programme request
    assert rows["j1"]["verdicts"] == 1                        # x2 only: drawn, matching, a programme job
    assert rows["x"]["verdicts"] == 0                         # drawn for x3 only, which pays no juror
    assert "m2" not in rows                                   # its job was clawed back; its own vote counts nothing


def test_presence_is_the_days_windows_and_is_paid_only_with_work(chain):
    rows = by_id(rank())
    # one read, over the day's own blocks, with the window length handed in
    assert chain["presence"] == [(NODE, FIRST, LAST, EPOCH)]
    assert rows["m1"]["presence"] == 3 and rows["m1"]["payout_address"] == "dendra1payee"
    assert rows["m1"]["gross_udndr"] == 3 * RULES["presence_per_window"] + RULES["work_per_request"]
    # j1 judged and proved windows but served no programme request: its presence is shown, not paid
    assert rows["j1"]["presence"] == 2 and rows["j1"]["verified_requests"] == 0
    assert rows["j1"]["gross_udndr"] == RULES["juror_per_verdict"] and rows["j1"]["reason"]
    # p only proved windows: ranked with nothing to pay, at its operator's address
    assert rows["p"]["presence"] == 4 and rows["p"]["gross_udndr"] == 0 and rows["p"]["paid_udndr"] == 0
    assert rows["p"]["payout_address"] == "dendra1opp"


def test_a_ranking_row_carries_the_published_fields_only(chain):
    out = rank()
    assert out["ranking"]
    for r in out["ranking"]:
        assert set(r) == ROW_FIELDS, sorted(set(r) ^ ROW_FIELDS)


def test_a_day_whose_work_is_graded_out_loses_its_presence_too(chain):
    rows = by_id(rank(records(coherent=False)))
    assert rows["m1"]["verified_requests"] == 0 and rows["m1"]["presence"] == 3
    assert rows["m1"]["gross_udndr"] == 0 and rows["m1"]["paid_udndr"] == 0


def test_not_final_is_refused(chain):
    chain["tip"] = DAY + 5
    with pytest.raises(ValueError, match="not final"):
        RK.rank_day(0, records(), NODE, GEN, START, 0, 17_280, DAY + 5, EPOCH)
    assert chain["index"] == []                     # refused before any search of the day


def test_an_index_that_does_not_reach_the_day_is_refused(chain, monkeypatch):
    def short(rpc, h):
        raise C.ChainUnreadable("history starts later")
    monkeypatch.setattr(C, "require_index_from", short)
    with pytest.raises(C.ChainUnreadable):
        rank()


def test_a_quorum_audit_without_its_draw_is_refused(chain, monkeypatch):
    monkeypatch.setattr(C, "audit_committee", lambda rpc, jid: [])
    with pytest.raises(C.ChainUnreadable):
        rank()
    # someone else's audit pays no juror: its draw is not read, so a missing one holds nothing up
    monkeypatch.setattr(C, "audit_committee", lambda rpc, jid: ["j1", "m2"] if jid == "x2" else [])
    assert rank()["ranking"]


def test_a_presence_read_that_fails_is_refused(chain, monkeypatch):
    def unreadable(node, first, last, epoch_blocks):
        raise C.ChainUnreadable("query txs: rc=1")
    monkeypatch.setattr(C, "presence_proofs", unreadable)
    with pytest.raises(C.ChainUnreadable):
        rank()
    # the same through the shipped reader, with its command line failing underneath it
    monkeypatch.setattr(C, "presence_proofs", REAL_PRESENCE)

    def failing_cli(args, node, timeout=60):
        raise C.ChainUnreadable("query txs: rc=1 connection refused")
    monkeypatch.setattr(C, "_cli", failing_cli)
    with pytest.raises(C.ChainUnreadable):
        rank()


def test_a_window_length_of_zero_is_refused_before_any_read(chain, monkeypatch):
    # through the shipped reader; the fixture's command line raises AssertionError, so a reader that
    # queried anyway would fail this test instead of passing it
    monkeypatch.setattr(C, "presence_proofs", REAL_PRESENCE)
    with pytest.raises(C.ChainUnreadable):
        rank(epoch=0)


def test_the_shipped_presence_reader_counts_accepted_windows_once(chain, monkeypatch):
    proof = avail_tx
    txs = [
        proof(FIRST + 5, "m1"),                     # accepted: proto3 omits a code of 0
        proof(FIRST + 6, "m1", code=0),             # the same window again: counted once
        proof(FIRST + EPOCH, "m1"),                 # the next window
        proof(LAST + 1, "m1"),                      # the next day's block: not this day's presence
        proof(FIRST + 7, "p", code=5),              # refused by the chain: proves nothing
        proof(FIRST + 8, "j1", also=({"@type": "/dendra.jobs.v1.MsgCommitVerdict", "miner_id": "x"},)),
    ]
    queries = serve_proofs(monkeypatch, txs)
    rows = by_id(rank())
    assert queries and all(q[:2] == ["query", "txs"] for q in queries)
    assert rows["m1"]["presence"] == 2
    assert rows["j1"]["presence"] == 1
    assert "p" not in rows                          # its only proof was refused, and it has no work or verdict
    assert "x" not in rows                          # named only by another message of the same transaction


def test_a_programme_job_without_its_answer_in_the_seal_earns_nothing(chain):
    # g1 is paid and settled on the chain, but the seal does not list it: its answer never reached the
    # programme, so it is not work, and m1's presence goes unpaid with it.
    rows = by_id(rank(records(answered=())))
    assert rows["m1"]["verified_requests"] == 0 and rows["m1"]["presence"] == 3
    assert rows["m1"]["gross_udndr"] == 0
    assert by_id(rank())["m1"]["verified_requests"] == 1


def test_the_default_address_is_the_signer_of_the_days_proofs(chain):
    # m1 declared nothing; it proved its windows from an operator the registry no longer names (it left,
    # or changed hands since): the day is paid where its proofs were signed, which a recompute after its
    # exit still finds.
    chain["signers"] = {"m1": "dendra1signer"}
    assert by_id(rank(records(payout=False)))["m1"]["payout_address"] == "dendra1signer"
    chain["registry"] = ["m2", "j1", "x", "p"]
    assert by_id(rank(records(payout=False)))["m1"]["payout_address"] == "dendra1signer"
    # a declaration still prevails over the proofs
    assert by_id(rank())["m1"]["payout_address"] == "dendra1payee"
    # an identity with no proof that day (j1, a juror only) falls back on the registry's operator
    chain["windows"] = {"m1": 3}
    rows = by_id(rank(records(payout=False)))
    assert rows["j1"]["presence"] == 0 and rows["j1"]["payout_address"] == "dendra1opj1"


def test_same_inputs_same_ranking(chain):
    a = rank()
    b = rank()
    assert a == b and a["inputs"]["evidence_sha256"]


def test_the_window_length_is_recorded_in_the_inputs(chain):
    # a length other than the fixture's: one written in the module instead of the one handed in would show
    out = rank(epoch=144)
    assert out["inputs"]["avail_epoch_blocks"] == 144
    assert chain["presence"] == [(NODE, FIRST, LAST, 144)]


def test_an_ordinary_day_counts_its_full_blocks_and_names_no_season_end(chain):
    # every header long before the end: the whole day, and no season's last block on it
    inputs = rank()["inputs"]
    assert inputs["last_height"] == LAST and inputs["season_end_height"] is None


def test_the_day_that_holds_the_end_counts_only_the_blocks_before_it(chain, monkeypatch):
    chain["cut"] = CUT
    # g1 settled ON the season's last block: counted. g2, m1's programme job too, answered and sealed,
    # settled on the first block at the end: inside the nominal day, outside the season. x2, the programme
    # audit, settled after the end: its jury is not even read, and it pays no verdict.
    chain["jobs"].append({"id": "g2", "state": "open+paid+optimistic", "miner_id": "m1", "client": GEN,
                          "slashed_primary": False})
    chain["settled"] = {"g1": SEASON_END, "g2": CUT, "x1": 11, "x2": CUT + 3, "x3": 13}
    queries = serve_proofs(monkeypatch, [
        avail_tx(FIRST + 5, "m1"),                  # window 0
        avail_tx(SEASON_END, "m1"),                 # the season's last block, another window: counted
        avail_tx(CUT + EPOCH, "m1"),                # a third window, after the end: not presence
        avail_tx(CUT, "p"),                         # p's only proof, on the first block at the end
    ])
    out = rank(records(answered=("g1", "g2")))
    assert out["inputs"]["last_height"] == SEASON_END
    assert out["inputs"]["season_end_height"] == SEASON_END
    rows = by_id(out)
    assert rows["m1"]["verified_requests"] == 1 and rows["m1"]["presence"] == 2
    assert chain["candidates"] == {}                # x2's jury not read: settled after the end
    assert "j1" not in rows and "p" not in rows     # their only facts of the day came after the end
    # the index is asked for the season's blocks only, never up to the nominal end of the day
    assert queries and all(f"tx.height<={SEASON_END}" in q[3] for q in queries)
    assert chain["index"] == [FIRST]


def test_the_cut_day_is_final_from_the_seasons_last_block_not_from_its_nominal_end(chain):
    chain["cut"] = CUT
    # ranked once the chain is the finality bound past the end, long before the nominal day is
    out = rank_at(chain, CUT + FINALITY)
    assert out["inputs"]["last_height"] == out["inputs"]["season_end_height"] == SEASON_END
    assert CUT + FINALITY < LAST + FINALITY
    # and refused before: the chain has passed the end, but not by the bound: never ranked short
    for h in (CUT, SEASON_END + FINALITY - 1):
        chain["index"] = []
        with pytest.raises(ValueError, match="not final"):
            rank_at(chain, h)
        assert chain["index"] == []


def test_a_day_after_the_end_is_refused(chain):
    # the end inside day 0, inside day 1, then exactly on day 1's first block (whose header IS the end)
    for cut, day in ((CUT, 1), (CUT, 5), (LAST + 1 + DAY // 2, 2), (LAST + 1, 1)):
        chain["cut"] = cut
        assert day_bounds(day, START)[0] >= cut
        with pytest.raises(ValueError, match="after the season"):
            rank(day=day)
    # refused before any search: never ranked as an empty day, which every search would confirm
    assert chain["index"] == [] and chain["presence"] == []


def test_a_day_whose_last_block_is_the_last_before_the_end_carries_it(chain):
    # the end falls exactly on day 1's first block: day 0 is whole, and it is the season's last day
    chain["cut"] = LAST + 1
    inputs = rank()["inputs"]
    assert inputs["last_height"] == LAST and inputs["season_end_height"] == LAST
    assert chain["presence"] == [(NODE, FIRST, LAST, EPOCH)]
    with pytest.raises(ValueError, match="after the season"):
        rank(day=1)
    # one block later: day 0 is whole but NOT the last day; day 1 holds the season's single last block
    chain["cut"] = LAST + 2
    assert rank()["inputs"]["season_end_height"] is None
    inputs = rank(day=1)["inputs"]
    assert inputs["last_height"] == inputs["season_end_height"] == LAST + 1
    assert chain["presence"][-1] == (NODE, LAST + 1, LAST + 1, EPOCH)


def test_the_command_line_refuses_a_day_after_the_end(chain, monkeypatch, capsys):
    chain["cut"] = CUT

    def fetch(url):
        if url.endswith("/ranking/day-000.json"):
            return b'{"total_paid_udndr": "0"}'
        raise AssertionError(f"unexpected fetch {url}")
    monkeypatch.setattr(RK, "_fetch", fetch)
    monkeypatch.setattr(RK, "_records", lambda base, day: records())
    monkeypatch.setattr(C, "jobs_params", lambda rest: {"audit_unwind_blocks": "100", "avail_epoch_blocks": "288"})
    argv = ["--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start", str(START)]
    assert RK.main(["--day", "1", *argv]) == 2
    out = capsys.readouterr().out
    assert out.startswith("REFUSED:") and "after the season" in out
    # the day that holds the end is ranked by the same command, cut at the season's last block
    assert RK.main(["--day", "0", *argv]) == 0
    inputs = json.loads(capsys.readouterr().out)["inputs"]
    assert inputs["last_height"] == inputs["season_end_height"] == SEASON_END


def test_finality_refuses_an_unarmed_unwind():
    with pytest.raises(C.ChainUnreadable):
        RK.finality_blocks({"audit_unwind_blocks": "0"})
    assert RK.finality_blocks({"audit_unwind_blocks": "17280"}) == 17280 + RK.FINALITY_MARGIN


def test_window_length_refuses_an_unarmed_window():
    with pytest.raises(C.ChainUnreadable):
        RK.epoch_blocks({"avail_epoch_blocks": "0"})
    # proto3 omits a zero field: absent IS 0, refused the same way, never given a default length
    with pytest.raises(C.ChainUnreadable):
        RK.epoch_blocks({})
    assert RK.epoch_blocks({"avail_epoch_blocks": "288"}) == 288
