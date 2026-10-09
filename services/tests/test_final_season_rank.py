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
# `DENDRA_MODEA_DIR` puts another copy of the modules first on the path: the mutation bench
# (dendra/onchain-staging/dendra_saison_vague1_test.sh) points it at a MUTATED copy and expects red.
if os.environ.get("DENDRA_MODEA_DIR"):
    sys.path.insert(0, os.environ["DENDRA_MODEA_DIR"])

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
        {"id": "g1", "state": "open+paid+optimistic", "miner_id": "m1", "client": GEN, "slashed_primary": False,
         "audit_voters": 0},
        {"id": "x1", "state": "open+paid+optimistic", "miner_id": "m1", "client": "dendra1someone",
         "slashed_primary": False, "audit_voters": 0},
        {"id": "x2", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "miner_id": "m2",
         "client": GEN, "slashed_primary": False, "audit_voters": 4},
        {"id": "x3", "state": "open+paid+optimistic+disputed+resolved+clawed+quorum", "miner_id": "m2",
         "client": "dendra1someone", "slashed_primary": False, "audit_voters": 4},
    ]
    calls = {"index": [], "presence": [], "jobs": jobs, "tip": TIP, "cut": None,
             "settled": {"g1": 10, "x1": 11, "x2": 12, "x3": 13},
             # F3: the audit of x2 was drawn at block 20 and resolved at the END of block 260; its verdicts
             # were committed at block 100 unless a test dates one otherwise ((job, juror) -> height or None).
             "drawn": {"x2": 20, "x3": 21}, "resolved": {"x2": (260, "end_block")}, "committed": {},
             "height_reads": []}

    def settle_heights(node, sender=None, max_pages=2000):
        calls["sender"] = sender
        return dict(calls["settled"])

    def verdict_commits(node, audited, candidates):
        calls["candidates"] = candidates
        # every registered miner posted a matching "0", drawn or not, signed by its operator
        return {j["id"]: {m: {"vote": "0", "creator": "dendra1op" + m} for m in candidates.get(j["id"], [])}
                for j in audited}

    def audit_resolution(node, rpc, jid):
        calls.setdefault("resolution_reads", []).append(jid)
        return calls["resolved"].get(jid)

    def commit_height(node, key, creator, lo, hi):
        calls["height_reads"].append((key, creator, lo, hi))
        jid, _, juror = key.partition("__verdict__")
        h = calls["committed"].get((jid, juror), 100)
        return h if h is not None and lo <= h <= hi else None        # searched in [lo, hi] only

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
    monkeypatch.setattr(C, "verdict_commits", verdict_commits)
    monkeypatch.setattr(C, "audit_resolution", audit_resolution)
    monkeypatch.setattr(C, "commit_height", commit_height)
    calls["juries"] = {"x2": ["j1", "m2"], "x3": ["j1", "x"]}
    monkeypatch.setattr(C, "audit_draw", lambda rpc, jid: (calls["drawn"].get(jid), calls["juries"].get(jid, [])))
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


def rank(recs=None, epoch=EPOCH, height=TIP, day=0, unwound_from=0):
    # Decision 18 in force from day 0 unless a test says otherwise: what the redeployed service runs.
    return RK.rank_day(day, records() if recs is None else recs, NODE, GEN, START, 0, FINALITY, height, epoch,
                       unwound_from=unwound_from)


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
        RK.rank_day(0, records(), NODE, GEN, START, 0, 17_280, DAY + 5, EPOCH, unwound_from=0)
    assert chain["index"] == []                     # refused before any search of the day


def test_an_index_that_does_not_reach_the_day_is_refused(chain, monkeypatch):
    def short(rpc, h):
        raise C.ChainUnreadable("history starts later")
    monkeypatch.setattr(C, "require_index_from", short)
    with pytest.raises(C.ChainUnreadable):
        rank()


def test_a_quorum_audit_without_its_draw_is_refused(chain, monkeypatch):
    monkeypatch.setattr(C, "audit_draw", lambda rpc, jid: (None, []))
    with pytest.raises(C.ChainUnreadable):
        rank()
    # someone else's audit pays no juror: its draw is not read, so a missing one holds nothing up
    monkeypatch.setattr(C, "audit_draw", lambda rpc, jid: (20, ["j1", "m2"]) if jid == "x2" else (None, []))
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
        if url == "http://e/status":
            return b'{"unwound_audit_work_from_day": 0}'
        raise AssertionError(f"unexpected fetch {url}")
    monkeypatch.setattr(RK, "_fetch", fetch)
    monkeypatch.setattr(RK, "_records", lambda base, day: records())
    monkeypatch.setattr(C, "jobs_params", lambda rest: {"audit_unwind_blocks": "80", "audit_resolve_timeout": "20",
                                                        "avail_epoch_blocks": "288"})
    argv = ["--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start", str(START)]
    assert RK.main(["--day", "1", *argv]) == 2
    out = capsys.readouterr().out
    assert out.startswith("REFUSED:") and "after the season" in out
    # the day that holds the end is ranked by the same command, cut at the season's last block
    assert RK.main(["--day", "0", *argv]) == 0
    inputs = json.loads(capsys.readouterr().out)["inputs"]
    assert inputs["last_height"] == inputs["season_end_height"] == SEASON_END


def test_finality_refuses_an_unarmed_unwind():
    from final_season_rules import RULES_17
    for rules in (RULES, RULES_17):
        with pytest.raises(C.ChainUnreadable):
            RK.finality_blocks({"audit_unwind_blocks": "0", "audit_resolve_timeout": "240"}, rules)
    assert RK.finality_blocks({"audit_unwind_blocks": "17280", "audit_resolve_timeout": "240"}, RULES) \
        == 17280 + 240 + RK.FINALITY_MARGIN
    # the rules of the day are required: no default chooses, in silence, when a day is final
    with pytest.raises(TypeError):
        RK.finality_blocks({"audit_unwind_blocks": "17280", "audit_resolve_timeout": "240"})


def test_a_day_under_the_rules_before_decision_18_is_final_when_they_said_it_was():
    # The relecture's C3: the service ranked every day under RULES_17 with `audit_unwind_blocks` + 200 (the only
    # committed version publishing their fingerprint, 4b19b5a, returns `unwind + FINALITY_MARGIN`). On the public
    # chain (17 280 + 200) day 0, blocks 1..17 280, was final at 34 760 -- and a recompute with one deadline more
    # would publish `finality_blocks: 17720` beside a published 17480: never the same bytes.
    from final_season_rules import FINALITY, FINALITY_17, RULES_17
    p = {"audit_unwind_blocks": "17280", "audit_resolve_timeout": "240"}
    assert RK.finality_blocks(p, RULES_17) == 17280 + RK.FINALITY_MARGIN == 17480
    # the margin the rules NAME is the margin the code adds: one number, read in both places
    assert FINALITY_17.endswith("+%d" % RK.FINALITY_MARGIN) and FINALITY.endswith("+%d" % RK.FINALITY_MARGIN)
    assert LAST + RK.finality_blocks(p, RULES_17) == 34_760
    assert LAST + RK.finality_blocks(p, RULES) == 35_000
    # RULES_17 never read the deadline: a chain without one ranks their days as it always did
    assert RK.finality_blocks({"audit_unwind_blocks": "17280"}, RULES_17) == 17480
    # a set the season never published names no finality: refused, never given one
    with pytest.raises(ValueError):
        RK.finality_blocks(p, dict(RULES_17, cap_season=1))


def test_window_length_refuses_an_unarmed_window():
    with pytest.raises(C.ChainUnreadable):
        RK.epoch_blocks({"avail_epoch_blocks": "0"})
    # proto3 omits a zero field: absent IS 0, refused the same way, never given a default length
    with pytest.raises(C.ChainUnreadable):
        RK.epoch_blocks({})
    assert RK.epoch_blocks({"avail_epoch_blocks": "288"}) == 288


# --- F3: a verdict counts only if it was committed BEFORE the audit was resolved ------------------------------
# The chain accepts a verdict commit after the resolution, when the outcome is public, and a day is ranked only
# once it is final, more than a day later. Read as the chain holds it NOW, a drawn juror who never voted could
# post the winning vote in between and be paid. Each verdict is dated by its commit transaction; each audit by
# the block that resolved it; an unknown date is never counted, and the published ranking says why.

def test_a_verdict_committed_after_the_resolution_is_not_paid_and_the_ranking_says_why(chain):
    chain["committed"][("x2", "j1")] = 261                   # one block after the end of block 260
    out = rank()
    assert by_id(out)["j1"]["verdicts"] == 0
    p = out["inputs"]["verdicts"]["x2"]
    assert (p["resolved_at"], p["resolved_in"], p["chain_counted_voters"]) == (260, "end_block", 4)
    assert p["jurors"]["j1"] == {"vote": "0", "committed_at": 261, "counted": False,
                                 "why": "committed_after_resolution"}


def test_a_verdict_committed_before_the_resolution_is_paid_and_recorded(chain):
    out = rank()
    assert by_id(out)["j1"]["verdicts"] == 1
    assert out["inputs"]["verdicts"]["x2"]["jurors"]["j1"] == {"vote": "0", "committed_at": 100, "counted": True}
    # dated in the day's window -- from the draw to the day's final block -- by the signer the commit names
    assert ("x2__verdict__j1", "dendra1opj1", 20, LAST + FINALITY) in chain["height_reads"]
    p = out["inputs"]["verdicts"]["x2"]
    assert (p["searched_from"], p["searched_to"]) == (20, LAST + FINALITY)


def test_in_the_resolution_block_an_end_block_tally_counts_the_verdict_and_an_adjudication_does_not(chain):
    chain["committed"][("x2", "j1")] = 260
    assert by_id(rank())["j1"]["verdicts"] == 1              # the tally ran after every transaction of 260
    chain["resolved"]["x2"] = (260, "transaction")
    out = rank()
    assert by_id(out)["j1"]["verdicts"] == 0                 # the order inside the block is not read
    assert out["inputs"]["verdicts"]["x2"]["jurors"]["j1"]["why"] == "committed_after_resolution"


def test_a_resolution_the_index_does_not_give_pays_no_verdict_and_says_so(chain):
    chain["resolved"]["x2"] = None
    out = rank()
    assert by_id(out)["j1"]["verdicts"] == 0
    p = out["inputs"]["verdicts"]["x2"]
    assert p["resolved_at"] is None and p["jurors"]["j1"]["why"] == "resolution_not_indexed"


def test_a_verdict_without_a_commit_in_the_days_window_is_neither_paid_nor_listed(chain):
    for h in (None, 19, LAST + FINALITY + 1):        # not in the index, before the draw, after the day's end
        chain["committed"][("x2", "j1")] = h
        out = rank()
        assert by_id(out)["j1"]["verdicts"] == 0, h
        assert "j1" not in out["inputs"]["verdicts"]["x2"]["jurors"], h


def test_the_proof_is_the_same_whenever_the_day_is_ranked(chain):
    # The window is the day's, not the moment's: a verdict committed after the day became final is in no
    # recompute, so the published proof and a recompute months later are the same bytes.
    chain["committed"][("x2", "j1")] = LAST + FINALITY + 50
    first = json.dumps(rank_at(chain, TIP), sort_keys=True)
    assert json.dumps(rank_at(chain, TIP + 5000), sort_keys=True) == first


def _flooded_by(chain, monkeypatch, juror, exc):
    real = C.commit_height

    def commit_height(node, key, creator, lo, hi):
        if key == f"x2__verdict__{juror}":
            raise exc
        return real(node, key, creator, lo, hi)
    monkeypatch.setattr(C, "commit_height", commit_height)


def test_a_juror_who_floods_the_commit_search_is_not_dated_and_the_day_is_still_ranked(chain, monkeypatch):
    # The relecture's replay: one juror's commits in the window outnumber what the search reads. Before, the
    # search raised, the day was deferred -- and every later day with it, ranked in order. Now THAT verdict is
    # listed, not dated and not counted, and the day is ranked.
    _flooded_by(chain, monkeypatch, "j1", C.SearchTooLong("message.sender='dendra1opj1'", 20001, 200))
    out = rank()
    assert by_id(out)["j1"]["verdicts"] == 0
    assert out["inputs"]["verdicts"]["x2"]["jurors"]["j1"] == {"vote": "0", "committed_at": None,
                                                               "counted": False, "why": "commit_not_dated"}
    assert by_id(out)["m1"]["verified_requests"] == 1        # the rest of the day is ranked as before


def test_a_commit_search_that_fails_still_defers_the_ranking(chain, monkeypatch):
    # A node that does not answer is not a juror who flooded the index: unknown is never "not dated".
    _flooded_by(chain, monkeypatch, "j1", C.ChainUnreadable("query txs: rc=1"))
    with pytest.raises(C.ChainUnreadable) as e:
        rank()
    assert not isinstance(e.value, C.SearchTooLong)


def test_finality_covers_an_audit_resolved_one_deadline_past_the_unwind_bound():
    # The last audit of a day resolves, at the latest, at the first resolution attempt at or after settlement
    # + audit_unwind_blocks, and attempts come every audit_resolve_timeout blocks: up to that many past the bound.
    p = {"audit_unwind_blocks": "17280", "audit_resolve_timeout": "240"}
    settled_last = 1000
    latest_resolution = settled_last + 17280 + 240 - 1
    assert settled_last + RK.finality_blocks(p, RULES) > latest_resolution
    assert RK.finality_blocks(p, RULES) == 17280 + 240 + RK.FINALITY_MARGIN
    # proto3 omits a zero: an absent timeout IS 0, and a chain without a deadline has no final day
    for unarmed in ({"audit_unwind_blocks": "17280"}, {"audit_unwind_blocks": "17280", "audit_resolve_timeout": "0"}):
        with pytest.raises(C.ChainUnreadable, match="audit_resolve_timeout"):
            RK.finality_blocks(unarmed, RULES)


def test_a_resolution_read_that_fails_defers_the_ranking(chain, monkeypatch):
    def unreadable(node, rpc, jid):
        raise C.ChainUnreadable("block_search: connection refused")
    monkeypatch.setattr(C, "audit_resolution", unreadable)
    with pytest.raises(C.ChainUnreadable):
        rank()


def test_only_the_programmes_audits_are_dated(chain):
    rank()
    assert chain["resolution_reads"] == ["x2"]               # x3 is someone else's job: never read
    assert all(k.startswith("x2__verdict__") for k, *_ in chain["height_reads"])


def test_a_day_without_an_audited_programme_job_keeps_its_published_shape(chain):
    chain["jobs"] = [j for j in chain["jobs"] if j["id"] != "x2"]
    assert "verdicts" not in rank()["inputs"]


# --- Decision 18: the work of an UNWOUND audit, answered and graded coherent, is paid --------------------------
# No jury concluded the audit within audit_unwind_blocks: the chain refunded the client and neither paid nor
# slashed the miner (`+resolved+unwound`). The season pays the request as verified work when its answer reached
# the programme and its miner's day is graded coherent, from the first day decision 18 applies to.
UNWOUND = "open+paid+optimistic+disputed+resolved+unwound"


def _unwound(chain, jid="u1", mid="m1", state=UNWOUND, at=14, slashed=False):
    chain["jobs"].append({"id": jid, "state": state, "miner_id": mid, "client": GEN, "slashed_primary": slashed,
                          "audit_voters": 0})
    chain["settled"][jid] = at


def test_decision_18_pays_an_unwound_job_answered_and_graded_coherent_as_verified_work(chain):
    from final_season_rules import UNWOUND_AUDIT_WORK, fingerprint
    _unwound(chain)
    out = rank(records(answered=("g1", "u1")))
    m1 = by_id(out)["m1"]
    assert m1["verified_requests"] == 2                       # g1, and u1 whose audit no jury concluded
    assert m1["gross_udndr"] == 3 * RULES["presence_per_window"] + 2 * RULES["work_per_request"]
    assert out["rules_fingerprint"] == fingerprint(RULES)
    assert out["inputs"]["unwound_audit_work"] == {
        "rule": UNWOUND_AUDIT_WORK, "from_day": 0,
        "jobs": {"u1": {"miner_id": "m1", "answered": True, "graded": "day_coherent", "paid": True}}}


def test_an_unwound_job_unanswered_or_graded_out_is_not_paid_and_the_ranking_says_why(chain):
    _unwound(chain)
    out = rank(records(answered=("g1",)))
    assert by_id(out)["m1"]["verified_requests"] == 1
    assert out["inputs"]["unwound_audit_work"]["jobs"]["u1"]["why"] == "not_answered"
    # m1's one sampled answer graded incoherent: the day's work is voided, the unwound job with it
    out = rank(records(coherent=False, answered=("g1", "u1")))
    assert by_id(out)["m1"]["verified_requests"] == 0 and by_id(out)["m1"]["gross_udndr"] == 0
    assert out["inputs"]["unwound_audit_work"]["jobs"]["u1"]["why"] == "day_graded_out"


def test_an_unwound_job_whose_own_answer_is_graded_incoherent_is_not_paid_on_a_coherent_day(chain):
    # The relecture's C1, through the ranking: g1 graded coherent keeps m1's ordinary work, u1's OWN answer is
    # graded incoherent -- u1 is not paid, and the published proof says its own answer decided.
    _unwound(chain)
    recs = records(answered=("g1", "u1")) + [
        {"type": "work_answer", "day": 0, "miner_id": "m1", "job_id": "u1", "height": 14, "prompt": "q", "answer": "b"},
        {"type": "work_grade", "day": 0, "miner_id": "m1", "job_id": "u1", "coherent": False, "model": "judge"}]
    out = rank(recs)
    assert by_id(out)["m1"]["verified_requests"] == 1                      # g1 only
    assert out["inputs"]["unwound_audit_work"]["jobs"]["u1"] == {
        "miner_id": "m1", "answered": True, "graded": "own_answer_incoherent", "paid": False,
        "why": "own_answer_incoherent"}


def test_an_unwound_job_of_a_miner_never_graded_that_day_is_not_paid(chain):
    # m2 has no grade on day 0: its ordinary work would stand (no grade never voids), its unwound job does not
    _unwound(chain, "u2", mid="m2")
    out = rank(records(answered=("g1", "u2")))
    assert by_id(out).get("m2", {}).get("verified_requests", 0) == 0
    assert out["inputs"]["unwound_audit_work"]["jobs"]["u2"] == {
        "miner_id": "m2", "answered": True, "graded": "not_graded", "paid": False, "why": "not_graded"}


def test_the_work_of_an_unwound_job_alone_is_a_day_of_verified_work_for_presence(chain):
    # p only proved windows: with one unwound job, answered and graded coherent, its presence is paid too
    _unwound(chain, "u3", mid="p")
    recs = records(answered=("g1", "u3")) + [
        {"type": "work_answer", "day": 0, "miner_id": "p", "job_id": "u3", "height": 14, "prompt": "q", "answer": "b"},
        {"type": "work_grade", "day": 0, "miner_id": "p", "job_id": "u3", "coherent": True, "model": "judge"}]
    p = by_id(rank(recs))["p"]
    assert p["verified_requests"] == 1 and p["presence"] == 4
    assert p["gross_udndr"] == 4 * RULES["presence_per_window"] + RULES["work_per_request"]


def test_a_convicted_job_is_never_paid_and_an_unwound_audit_pays_no_juror(chain):
    _unwound(chain, "c1", state="open+paid+optimistic+disputed+resolved+clawed+quorum")
    chain["juries"]["c1"], chain["drawn"]["c1"], chain["resolved"]["c1"] = ["j1"], 20, (260, "end_block")
    _unwound(chain, "u1")
    out = rank(records(answered=("g1", "c1", "u1")))
    assert by_id(out)["m1"]["verified_requests"] == 2         # g1 and u1; never c1, convicted
    assert "c1" not in out["inputs"]["unwound_audit_work"]["jobs"]
    # the unwound audit concluded nothing: its jury is not even read, and no verdict on it is paid
    assert "u1" not in chain["candidates"] and "u1" not in out["inputs"]["verdicts"]
    _unwound(chain, "u9", state=UNWOUND, slashed=True)
    out = rank(records(answered=("g1", "u1", "u9")))
    assert out["inputs"]["unwound_audit_work"]["jobs"]["u9"]["why"] == "convicted"
    assert by_id(out)["m1"]["verified_requests"] == 2


def test_a_day_before_decision_18_is_ranked_exactly_as_it_was_published(chain):
    from final_season_rules import RULES_17, fingerprint
    _unwound(chain)
    recs = records(answered=("g1", "u1"))
    before = rank(recs, unwound_from=1)
    assert by_id(before)["m1"]["verified_requests"] == 1      # the rules before decision 18: unwound unpaid
    assert before["rules_fingerprint"] == fingerprint(RULES_17)
    assert "unwound_audit_work" not in before["inputs"]
    # the same bytes as a service that never had decision 18
    assert json.dumps(before, sort_keys=True) == json.dumps(rank(recs, unwound_from=None), sort_keys=True)
    # and the same bytes as if the unwound job did not exist: the rules before decision 18 never looked at it
    chain["jobs"] = [j for j in chain["jobs"] if j["id"] != "u1"]
    del chain["settled"]["u1"]
    assert json.dumps(before, sort_keys=True) == json.dumps(rank(recs, unwound_from=1), sort_keys=True)
    # and the next day is under decision 18, carrying its fingerprint and its (empty) record
    after = rank(recs, day=1, unwound_from=1)
    assert after["rules_fingerprint"] == fingerprint(RULES)
    assert after["inputs"]["unwound_audit_work"] == {"rule": RULES["unwound_audit_work"], "from_day": 1, "jobs": {}}


def test_the_first_day_of_decision_18_is_required_and_has_no_default(chain):
    with pytest.raises(TypeError):
        RK.rank_day(0, records(), NODE, GEN, START, 0, FINALITY, TIP, EPOCH)
    for bad in (-1, "0", 0.0, True):
        with pytest.raises(ValueError):
            rank(unwound_from=bad)


def test_a_day_with_an_unwound_job_recomputes_identically(chain):
    _unwound(chain)
    _unwound(chain, "u2", mid="m2")
    recs = records(answered=("g1", "u1", "u2"))
    first = json.dumps(rank_at(chain, TIP, recs=recs), sort_keys=True)
    assert json.dumps(rank_at(chain, TIP + 5000, recs=recs), sort_keys=True) == first


def _cli_fetch(status):
    def fetch(url):
        if url == "http://e/status":
            if status is None:
                raise AssertionError("the status was read although the day was given")
            return json.dumps(status).encode()
        raise AssertionError(f"unexpected fetch {url}")
    return fetch


@pytest.mark.parametrize("status,argv,code,fp_set", [
    ({"unwound_audit_work_from_day": 0}, [], 0, "RULES"),
    ({"unwound_audit_work_from_day": 1}, [], 0, "RULES_17"),
    ({}, [], 0, "RULES_17"),                                   # a service that predates decision 18
    ({"unwound_audit_work_from_day": None}, [], 2, None),      # a service that does not know: refused
    ({"unwound_audit_work_from_day": "0"}, [], 2, None),
    ({"unwound_audit_work_from_day": -1}, [], 2, None),
    (None, ["--unwound-from", "none"], 0, "RULES_17"),
    (None, ["--unwound-from", "0"], 0, "RULES"),
])
def test_the_command_line_reads_the_first_day_of_decision_18_from_the_status(chain, monkeypatch, capsys, status,
                                                                             argv, code, fp_set):
    import final_season_rules as R
    _unwound(chain)
    monkeypatch.setattr(RK, "_fetch", _cli_fetch(status))
    monkeypatch.setattr(RK, "_records", lambda base, day: records(answered=("g1", "u1")))
    monkeypatch.setattr(C, "jobs_params", lambda rest: {"audit_unwind_blocks": "80", "audit_resolve_timeout": "20",
                                                        "avail_epoch_blocks": "288"})
    rc = RK.main(["--day", "0", "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start",
                  str(START), *argv])
    out = capsys.readouterr().out
    assert rc == code, out
    if code == 2:
        assert out.startswith("REFUSED:") and "unwound_audit_work_from_day" in out
        return
    got = json.loads(out)
    assert got["rules_fingerprint"] == R.fingerprint(getattr(R, fp_set))
    assert by_id(got)["m1"]["verified_requests"] == (2 if fp_set == "RULES" else 1)
    # and final when the day's own rules say: unwind + deadline + 200 under decision 18, unwind + 200 before it
    assert got["inputs"]["finality_blocks"] == (80 + 20 + 200 if fp_set == "RULES" else 80 + 200)


def test_the_command_line_reads_the_evidence_as_the_service_reads_it(tmp_path, monkeypatch):
    # A torn last line (a crash mid-write) is skipped and counted, exactly as final_season_evidence.Evidence
    # reads it: the service ranks from that list, and decision 18 reads the `_unreadable_lines` record.
    from final_season_evidence import Evidence
    ev = Evidence(str(tmp_path))
    ev.append(0, {"type": "payout", "miner_id": "m1", "address": "dendra1a", "height": 3})
    with open(ev.path(0), "a", encoding="utf-8") as f:
        f.write('{"type": "work_grade", "miner_id": "m1", "coh')
    ev.append(1, {"type": "payout", "miner_id": "m2", "address": "dendra1b", "height": 4})

    def fetch(url):
        name = url.rsplit("/", 1)[1]
        p = os.path.join(str(tmp_path), name)
        if not os.path.exists(p):
            raise RK.urllib.error.HTTPError(url, 404, "not published", None, None)
        with open(p, "rb") as f:
            return f.read()
    monkeypatch.setattr(RK, "_fetch", fetch)
    got = RK._records("http://e/", 2)
    assert got == ev.read(0) + ev.read(1)
    assert {"type": "_unreadable_lines", "count": 1, "day": 0} in got
