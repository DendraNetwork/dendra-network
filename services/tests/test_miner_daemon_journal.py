# -*- coding: utf-8 -*-
"""The miner daemon's COMMITMENT JOURNAL — ADR-045, entry 17.

WHAT IT EXISTS TO HOLD. The loop skipped a job as soon as its RESPONSE appeared in the relay listing. But
the response is deposited BEFORE the commitment is anchored, and the anchoring is attempted three times:
if all three fail -- a misaligned identity, gas, a dropped RPC -- the GPU has run, the answer is sealed at
the relay, and NO commit exists. The next pass skipped the job, for good.

WHY NEITHER OF THE OTHER TWO OPTIONS, and this is what the file locks in:
  * "de-duplicate on the on-chain commit" would re-run INFERENCE on the next pass. An LLM does not answer
    twice the same, and the deposit is sealed to the CLIENT's key -- the daemon cannot read back what it
    stored in order to commit to THAT. Any recovery has to CARRY THE ORIGINAL COMMITMENT FORWARD.
  * "keep it in memory" does carry it forward, but does not survive a restart, which is precisely the case
    a daemon exists to survive.
=> The IRREPEATABLE step is made durable first; only the deposit and the anchoring stay retryable.

WHAT THIS BENCH DOES NOT COVER, AND IT SAYS SO. It exercises the JOURNAL and its contract, not the loop
that calls it: driving a job through the loop would need a relay, a chain and an inference engine. The
resumption path itself is verified by reading, and that limit is written here rather than hidden behind a
test name promising more. The HEARTBEAT section at the end does drive the loop through one pass, with
everything around it replaced: what it pins is the loop's own control flow, not a job.
"""
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The subject can be pointed at a MUTATED copy (DENDRA_DAEMON_FILE), so a harness can show that a case
# turns red when the property it pins is removed. By default, the shipped daemon.
if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    md = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = md
    _spec.loader.exec_module(md)
else:
    import miner as md  # noqa: E402

KEY = "job-42__m-abc"
COMMIT = "1.0,2.0,3.0"
PCOMMIT = "9.9,8.8"


def test_a_journalled_commitment_reads_back(tmp_path):
    md._journal_commitment(tmp_path, KEY, COMMIT, PCOMMIT)
    pending = md._pending_commitment(tmp_path, KEY)
    assert pending.get("commit") == COMMIT
    assert pending.get("pcommit") == PCOMMIT, (
        "the QUESTION's commitment travels with the answer's: without it the resumption would anchor a "
        "prompt commitment different from the one the juror will confront")


def test_it_survives_a_RESTART(tmp_path):
    """The whole point: the same directory, re-read by a fresh process, yields the same commitment."""
    md._journal_commitment(tmp_path, KEY, COMMIT, PCOMMIT)
    # No in-memory state is shared: the read goes through the disk, as it would after a restart.
    assert md._read_journal(tmp_path)[KEY]["commit"] == COMMIT


def test_it_is_forgotten_only_on_a_MEASUREMENT(tmp_path):
    md._journal_commitment(tmp_path, KEY, COMMIT, PCOMMIT)
    md._forget_commitment(tmp_path, KEY)
    assert md._pending_commitment(tmp_path, KEY) == {}, (
        "forgetting is called once the chain CARRIES the commitment, never on a hope")


def test_an_EMPTY_commitment_is_not_journalled(tmp_path):
    """An empty commit means the computation failed. Journalling it would promise the resumption something
    to anchor that does not exist -- a default written on the reassuring side."""
    md._journal_commitment(tmp_path, KEY, "", "")
    assert md._pending_commitment(tmp_path, KEY) == {}


def test_an_UNREADABLE_journal_degrades_instead_of_lying(tmp_path):
    """Three states, never two: a corrupt file is not "nothing pending", it is "I do not know". Both lead
    to the same behaviour -- the one from before the fix -- and that is the only acceptable fallback:
    degraded, never wrong."""
    io.open(Path(tmp_path) / md._JOURNAL, "w", encoding="utf-8").write("{this is not json")
    assert md._read_journal(tmp_path) == {}
    assert md._pending_commitment(tmp_path, KEY) == {}


def test_the_write_is_ATOMIC_and_leaves_no_residue(tmp_path):
    """A half-written journal would be worse than none: the resumption would read a truncated commitment
    and anchor something other than what the client can open."""
    md._journal_commitment(tmp_path, KEY, COMMIT, PCOMMIT)
    leftovers = [p.name for p in Path(tmp_path).iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], "a surviving .tmp would signal a non-atomic replacement"
    # And the final file is complete JSON, not a prefix of one.
    with io.open(Path(tmp_path) / md._JOURNAL, encoding="utf-8") as f:
        assert json.load(f)[KEY]["commit"] == COMMIT


def test_several_jobs_coexist(tmp_path):
    """A miner serves several jobs: forgetting one must not carry the others away."""
    md._journal_commitment(tmp_path, "j1__m", "c1", "p1")
    md._journal_commitment(tmp_path, "j2__m", "c2", "p2")
    md._forget_commitment(tmp_path, "j1__m")
    assert md._pending_commitment(tmp_path, "j1__m") == {}
    assert md._pending_commitment(tmp_path, "j2__m").get("commit") == "c2"


# ══ THE HEARTBEAT (modea/heartbeat.py, miner._status_write) ════════════════════════════════════
# What it pins: the file is replaced atomically; the loop's pass ends with a write even when the pass
# raised; the presence field says "proven" only with the height `wait_tx` read, never before; a write
# that fails never stops the loop and is said once. The loop itself is driven through `main --once`,
# with everything around it replaced: the case is the loop's own control flow, not the chain.
import pytest  # noqa: E402

ADDR = "dendra1bench"


@pytest.fixture()
def status_file(tmp_path, monkeypatch):
    p = tmp_path / "status.json"
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(p))
    md._STATUS.clear()
    monkeypatch.setattr(md, "_STATUS_WARNED", False)
    return p


def _one_pass(tmp_path, monkeypatch, listing, miner=None):
    """main() through ONE pass of its loop, with the chain, the relay and the keys replaced (and the
    inference, by `miner`, when a job is in the queue)."""
    monkeypatch.setenv("DENDRA_CONFINE", "0")
    monkeypatch.setattr(sys, "argv", ["miner.py", "--id", "dm1bench", "--relay", "http://relay.invalid",
                                      "--keydir", str(tmp_path / "keys"), "--once"])
    monkeypatch.setattr(md, "keys_addr", lambda name, keydir=None: ADDR)
    monkeypatch.setattr(md, "align_identity", lambda name, addr, owner="": "dm1bench")
    monkeypatch.setattr(md.relay_signature, "address_from_key", lambda *a, **k: ADDR)
    monkeypatch.setattr(md.relay, "put", lambda *a, **k: True)
    # The loop asks for its own slice (relay_client.listing(base, suffix)); the stubs below answer the queue.
    monkeypatch.setattr(md.relay, "listing", lambda base, suffix=None: listing(base))
    monkeypatch.setattr(md, "model_weights_hash", lambda: "")
    monkeypatch.setattr(md, "vrf_identity", lambda keydir, mid: ("", ""))
    monkeypatch.setattr(md, "miner_operator", lambda mid: ADDR)
    # Registered: the registration is guarded by the registry (three states); stake_of, which read -1 both for a
    # failed query and for a stake proto3 omitted, is gone.
    monkeypatch.setattr(md, "registry_record",
                        lambda mid: (md.PRESENT, {"creator": ADDR, "operator": ADDR, "region": "eu", "stake": 5}))
    monkeypatch.setattr(md, "miner_vrf_onchain", lambda mid: "")
    monkeypatch.setattr(md, "pick_backend", lambda want: "mock")
    monkeypatch.setattr(md, "Miner", lambda *a, **k: miner if miner is not None else object())
    md.main()


def test_the_heartbeat_is_replaced_atomically(status_file):
    assert md._status_write(phase="loop", loop_at=123) is True
    doc = json.loads(status_file.read_text())
    assert doc["loop_at"] == 123 and doc["phase"] == "loop" and isinstance(doc["written_at"], int)
    assert [p.name for p in status_file.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_a_value_json_cannot_hold_does_not_poison_the_next_writes(status_file):
    assert md._status_write(job={"why": object()}) is True
    assert md._status_write(loop_at=7) is True
    assert json.loads(status_file.read_text())["loop_at"] == 7


def test_loop_at_advances_even_when_the_pass_raises(status_file, tmp_path, monkeypatch):
    def down(base):
        raise RuntimeError("relay down")
    before = int(time.time())
    _one_pass(tmp_path, monkeypatch, down)
    doc = json.loads(status_file.read_text())
    assert doc["loop_error"]["what"] == "RuntimeError: relay down"
    assert doc["loop_at"] >= before and doc["phase"] == "loop" and doc["miner_id"] == "dm1bench"
    assert "relay_listing_ok_at" not in doc, "a listing that raised is not a queue that was read"


def test_a_queue_that_was_read_is_dated(status_file, tmp_path, monkeypatch):
    _one_pass(tmp_path, monkeypatch, lambda base: {"req": [], "res": [], "pub": []})
    doc = json.loads(status_file.read_text())
    assert isinstance(doc.get("relay_listing_ok_at"), int) and "loop_error" not in doc


def test_a_heartbeat_that_cannot_be_written_never_stops_the_loop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "no" / "such" / "dir" / "status.json"))
    md._STATUS.clear()
    monkeypatch.setattr(md, "_STATUS_WARNED", False)
    _one_pass(tmp_path, monkeypatch, lambda base: {"req": [], "res": [], "pub": []})
    out = capsys.readouterr().out
    assert out.count("heartbeat NOT written") == 1, "said once, never once per pass"


def _prove(monkeypatch, height):
    """prove_availability_once with a challenge, a proof and a transaction whose inclusion height is
    `height` (0: not included and accepted). Returns (what it returned, every heartbeat it wrote)."""
    writes = []
    real = md.hb.write_atomic

    def record(path, doc):
        writes.append(json.loads(json.dumps(doc)))
        real(path, doc)
    monkeypatch.setattr(md.hb, "write_atomic", record)
    monkeypatch.setattr(md, "query", lambda *a, **k: json.dumps({"challenge": "c" * 64}))

    class _Out:
        stdout = "ab" * 80
    monkeypatch.setattr(md.subprocess, "run", lambda *a, **k: _Out())
    monkeypatch.setattr(md, "tx_from", lambda *a, **k: "code: 0\ntxhash: " + "A" * 64)
    monkeypatch.setattr(md, "wait_tx_height", lambda o, timeout=24: height)
    monkeypatch.setattr(md, "_PROBE_STATE", {"failed_at": 0.0, "said_for": "", "bad_bound_said": False})
    return md.prove_availability_once("dm1bench", "a" * 128, "", probe=lambda: (md.PROBE_ANSWERED, "", 0.2)), writes


def test_presence_is_written_proven_only_with_the_height_wait_tx_read(status_file, monkeypatch):
    got, writes = _prove(monkeypatch, 0)
    assert got == "", "a refused proof leaves the old challenge, so the next tick retries"
    assert writes and writes[-1]["presence"]["result"] == "refused"
    assert not [w for w in writes if (w.get("presence") or {}).get("result") == "proven"], (
        "the heartbeat said 'proven' before the chain did")
    md._STATUS.clear()
    got, writes = _prove(monkeypatch, 4321)
    assert got == "c" * 64
    assert writes[-1]["presence"]["result"] == "proven" and writes[-1]["presence"]["height"] == 4321


def test_the_staleness_bound_is_derived_from_the_bounds_the_loop_applies(monkeypatch):
    monkeypatch.delenv("OLLAMA_TIMEOUT", raising=False)
    assert md.hb.max_age_s() == 2 * max(md.hb.DENDRAD_CALL_BOUND_S, 600)
    monkeypatch.setenv("OLLAMA_TIMEOUT", "900")
    assert md.hb.max_age_s() == 1800
    import inspect
    assert inspect.signature(md.run).parameters["t"].default == md.hb.DENDRAD_CALL_BOUND_S


# -- wait_tx_height, EXECUTED -------------------------------------------------------------------------
# The case above REPLACES wait_tx_height; these RUN it, with only `run` (the dendrad call) and the pause
# between polls replaced. An inclusion that FAILED is not a proof: it is the property that makes "proven"
# a reading, and the one a mock of the function can never see. wait_tx decides the anchoring of
# create-commit and create-miner too, from the same answer.
TXH = "AB" * 32
NL = chr(10)


def _wait(monkeypatch, answer, bcast=None, timeout=24):
    bcast = "code: 0" + NL + "txhash: " + TXH + NL if bcast is None else bcast
    calls = []

    def fake_run(c, t=None):
        calls.append(list(c))
        return answer
    monkeypatch.setattr(md, "run", fake_run)
    monkeypatch.setattr(md.time, "sleep", lambda s: None)
    return md.wait_tx_height(bcast, timeout=timeout), md.wait_tx(bcast, timeout=timeout), calls


def test_wait_tx_height_included_and_accepted_is_its_height(monkeypatch):
    h, ok, calls = _wait(monkeypatch, NL.join(["code: 0", 'height: "42"', "txhash: " + TXH]))
    assert (h, ok) == (42, True)
    assert calls and calls[0][:4] == ["dendrad", "query", "tx", TXH]


def test_wait_tx_height_included_and_FAILED_is_not_proven(monkeypatch):
    h, ok, _ = _wait(monkeypatch, NL.join(["code: 5", 'height: "42"', "raw_log: out of gas", "txhash: " + TXH]))
    assert (h, ok) == (0, False), "a transaction included with a non-zero code proved nothing"


def test_wait_tx_height_never_found_is_0_after_its_polls(monkeypatch):
    h, ok, calls = _wait(monkeypatch, "Error: tx (" + TXH + ") not found", timeout=3)
    assert (h, ok) == (0, False) and len(calls) == 2 * 3, "three polls for each of the two calls"


def test_wait_tx_height_refused_at_broadcast_asks_nothing(monkeypatch):
    h, ok, calls = _wait(monkeypatch, "unused", bcast=NL.join(["code: 13", "txhash: " + TXH, "raw_log: insufficient fee"]))
    assert (h, ok, calls) == (0, False, [])


# -- the code of a transaction response, in THREE states (the rule of the zero) ---------------------------
def test_tx_code_reads_a_number_an_omitted_zero_and_an_unknown():
    assert md._tx_code(NL.join(["code: 5", "txhash: " + TXH])) == 5
    assert md._tx_code(NL.join(["code: 0", "txhash: " + TXH])) == 0
    assert md._tx_code(NL.join(['height: "42"', "txhash: " + TXH])) == 0, (
        "a transaction response without its code line carries a zero proto3 omitted, not a failure")
    for unread in ("", "Error: rpc error: code = Unknown desc = failed to execute message", "Usage: dendrad tx"):
        assert md._tx_code(unread) is None, "no transaction response is an unknown, never a zero: %r" % unread


def test_wait_tx_height_included_with_its_code_omitted_is_its_height(monkeypatch):
    h, ok, _ = _wait(monkeypatch, NL.join(['height: "42"', "txhash: " + TXH]), bcast="txhash: " + TXH + NL)
    assert (h, ok) == (42, True), "proto3 omits a zero code at broadcast and at inclusion: both are acceptances"


# ══ THE COMMIT COUNTERS (miner._COMMITS) — what the HiveOS stats show as `ar` ═════════════════════
# The loop is driven through ONE pass of main() with a job in the queue, everything around it replaced: the
# relay, the inference, the chain's answers. What is pinned: each counter moves only on what it names --
# `anchored` on a commitment READ on the chain after this process broadcast it, `refused` on a non-zero code
# READ -- an unknown moves neither, and both are in the heartbeat from the first write.
JOB = "job1__dm1bench"
JCOMMIT = "0.5,-0.25,0.125"


@pytest.fixture()
def counters(status_file, monkeypatch):
    monkeypatch.setattr(md, "_COMMITS", {"commits_anchored": 0, "commits_refused": 0})
    monkeypatch.setattr(md, "_COMMITS_SENT", set())
    return status_file


class _Res:
    content_embed = JCOMMIT
    prompt_commit = "0.75,0.5"
    in_tok = 3
    out_tok = 5

    class sealed_result:
        nonce = bytes(24)
        ct = bytes(8)


class _FakeMiner:
    def handle_job(self, jid, eph, sealed, max_out=0):
        return _Res()


def _job_pass(tmp_path, monkeypatch, broadcasts, inclusions, on_chain_after=None, on_chain_before=False,
              pending=False):
    """One pass of main() with JOB in the queue. `broadcasts`: the outputs of the successive create-commit
    calls (the last one repeats); `inclusions`: what successive `dendrad query tx` calls answer (the last one
    repeats); the commitment is on the chain before any broadcast when `on_chain_before`, and from the
    broadcast number `on_chain_after` (1-based) on otherwise. With `pending`, the job's commitment is
    journalled and its response already at the relay: the RESUMPTION path. Returns the number of
    create-commit calls."""
    (tmp_path / "keys").mkdir(exist_ok=True)
    sent, polled = [], []

    def fake_tx_from(frm, sub, *pos, flags=()):
        assert sub == "create-commit"
        sent.append(pos)
        return broadcasts[min(len(sent), len(broadcasts)) - 1]

    def fake_query(sub, *pos, flags=()):
        assert sub == "get-commit"
        on = on_chain_before or (on_chain_after is not None and len(sent) >= on_chain_after)
        return ("commit: " + JCOMMIT) if on else "commit: null"

    def fake_run(c, t=None, stdin=""):
        assert c[:3] == ["dendrad", "query", "tx"], c
        polled.append(c)
        return inclusions[min(len(polled), len(inclusions)) - 1]

    if pending:
        md._journal_commitment(tmp_path / "keys", JOB, JCOMMIT, "0.75,0.5")
    listing = {"req": [JOB], "res": [JOB] if pending else [], "pub": []}
    monkeypatch.setattr(md, "tx_from", fake_tx_from)
    monkeypatch.setattr(md, "query", fake_query)
    monkeypatch.setattr(md, "run", fake_run)
    monkeypatch.setattr(md, "bal_token", lambda addr: 0)
    monkeypatch.setattr(md.relay, "get", lambda *a, **k: {"client_eph_pk": "00" * 32, "nonce": "00" * 24, "ct": "00" * 8})
    monkeypatch.setattr(md.time, "sleep", lambda s: None)
    _one_pass(tmp_path, monkeypatch, lambda base: listing, miner=_FakeMiner())
    return len(sent)


def _ar(status_file):
    doc = json.loads(status_file.read_text())
    return [doc.get("commits_anchored"), doc.get("commits_refused")]


OK_BCAST = "code: 0" + NL + "txhash: " + TXH + NL
INCLUDED = NL.join(["code: 0", 'height: "42"', "txhash: " + TXH])


def test_the_counters_are_in_the_first_heartbeat_at_zero(counters, tmp_path, monkeypatch):
    _one_pass(tmp_path, monkeypatch, lambda base: {"req": [], "res": [], "pub": []})
    assert _ar(counters) == [0, 0], "present from the first write: a zero here is a reading, nothing was counted"


def test_an_anchored_commit_counts_once_as_anchored(counters, tmp_path, monkeypatch):
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED], on_chain_after=1)
    assert n == 1 and _ar(counters) == [1, 0]
    assert json.loads(counters.read_text())["job"]["result"] == "anchored"


def test_a_code_read_non_zero_counts_refused_once_per_transaction(counters, tmp_path, monkeypatch):
    refused = NL.join(["code: 13", "txhash: " + TXH, "raw_log: insufficient fee"])
    n = _job_pass(tmp_path, monkeypatch, [refused], ["unused"])
    assert n == 3 and _ar(counters) == [0, 3], "three transactions, each answered with a non-zero code"


def test_an_unknown_fate_counts_in_neither(counters, tmp_path, monkeypatch):
    # No transaction response: the node refused while estimating gas, or did not answer. Nothing was read.
    n = _job_pass(tmp_path, monkeypatch, ["Error: rpc error: code = Unknown desc = failed to execute message"], ["unused"])
    assert n == 3 and _ar(counters) == [0, 0], "an unread code is not a refusal (the rule of the zero, three states)"


def test_a_transaction_never_found_counts_in_neither(counters, tmp_path, monkeypatch):
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], ["Error: tx (" + TXH + ") not found"])
    assert n == 3 and _ar(counters) == [0, 0]


def test_an_inclusion_that_failed_then_an_anchoring_counts_one_of_each(counters, tmp_path, monkeypatch):
    failed = NL.join(["code: 5", 'height: "42"', "raw_log: out of gas", "txhash: " + TXH])
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [failed, INCLUDED], on_chain_after=2)
    assert n == 2 and _ar(counters) == [1, 1], "one transaction included and refused, then one anchored"


def test_an_omitted_zero_code_is_an_acceptance_never_a_refusal(counters, tmp_path, monkeypatch):
    n = _job_pass(tmp_path, monkeypatch, ["txhash: " + TXH + NL], [NL.join(['height: "42"', "txhash: " + TXH])],
                  on_chain_after=1)
    assert n == 1 and _ar(counters) == [1, 0]


def test_a_commitment_already_on_chain_is_not_this_process_s_anchoring(counters, tmp_path, monkeypatch):
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED], on_chain_before=True)
    assert n == 0 and _ar(counters) == [0, 0], "nothing was broadcast here: the count is this process's"


def test_a_resumed_anchoring_counts_as_anchored(counters, tmp_path, monkeypatch):
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED], on_chain_after=1, pending=True)
    assert n == 1 and _ar(counters) == [1, 0]
    assert json.loads(counters.read_text())["job"]["result"] == "anchored (resumed from the journal)"


def test_a_resumed_refusal_counts_refused_and_keeps_the_commitment(counters, tmp_path, monkeypatch):
    refused = NL.join(["code: 7", "txhash: " + TXH, "raw_log: unauthorized"])
    n = _job_pass(tmp_path, monkeypatch, [refused], ["unused"], pending=True)
    assert n == 1 and _ar(counters) == [0, 1], "one attempt per pass on the resumption path, refused with a code"
    assert md._pending_commitment(tmp_path / "keys", JOB).get("commit") == JCOMMIT


def test_a_resumed_job_found_anchored_without_a_broadcast_here_is_not_counted(counters, tmp_path, monkeypatch):
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED], on_chain_before=True, pending=True)
    assert n == 0 and _ar(counters) == [0, 0]


def test_counting_survives_a_heartbeat_that_cannot_be_written(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "no" / "such" / "dir" / "status.json"))
    md._STATUS.clear()
    monkeypatch.setattr(md, "_STATUS_WARNED", False)
    monkeypatch.setattr(md, "_COMMITS", {"commits_anchored": 0, "commits_refused": 0})
    monkeypatch.setattr(md, "_COMMITS_SENT", set())
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED], on_chain_after=1)
    assert n == 1 and md._COMMITS == {"commits_anchored": 1, "commits_refused": 0}
    assert capsys.readouterr().out.count("heartbeat NOT written") == 1


# ══ HOW LONG THE ANSWER TOOK (miner: `gen_s` in the heartbeat's job, and on the anchored line) ════════
# The client waits for an answer a bounded time, and a judge on the CPU answers with a model whose speed the RAM
# floor does not measure. The generation's duration is therefore READ on every job: the inference alone, from a
# clock the test moves, never the pass around it.
class _SlowMiner:
    clock = [1000.0]

    def handle_job(self, jid, eph, sealed, max_out=0):
        _SlowMiner.clock[0] += 237.4      # the inference, and nothing else, takes this long
        return _Res()


def test_an_anchored_job_records_how_long_its_answer_took(counters, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(md.time, "monotonic", lambda: _SlowMiner.clock[0])
    monkeypatch.setitem(globals(), "_FakeMiner", _SlowMiner)
    n = _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED], on_chain_after=1)
    job = json.loads(counters.read_text())["job"]
    assert n == 1 and job["result"] == "anchored"
    assert job.get("gen_s") == 237.4, "the generation's duration, measured on every job"
    assert job.get("out_tok") == _Res.out_tok
    assert "answered in 237.4 s, 5 tokens" in capsys.readouterr().out


def test_a_failed_inference_records_how_long_it_ran(counters, tmp_path, monkeypatch):
    class _Failing:
        def handle_job(self, jid, eph, sealed, max_out=0):
            _SlowMiner.clock[0] += 600.0  # an engine that never answered: OLLAMA_TIMEOUT's bound
            raise TimeoutError("read timed out")
    monkeypatch.setattr(md.time, "monotonic", lambda: _SlowMiner.clock[0])
    monkeypatch.setitem(globals(), "_FakeMiner", _Failing)
    _job_pass(tmp_path, monkeypatch, [OK_BCAST], [INCLUDED])
    job = json.loads(counters.read_text())["job"]
    assert job["result"] == "inference failed" and job.get("gen_s") == 600.0
