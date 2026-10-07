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
that calls it: driving `daemon_loop` would need a relay, a chain and an inference engine. The resumption
path itself is verified by reading, and that limit is written here rather than hidden behind a test name
promising more.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
