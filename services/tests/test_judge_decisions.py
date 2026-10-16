"""The judge's decisions (ADR-026, `judge_worker.py`) — the functions that decide a slash.

================================================================================================
Why this bench exists. `judge_worker.py` is the module that posts verdicts: one "0" too many slashes
an honest primary, one "0" too few lets a cheater cash in. The five functions covered here are pure
or have injected dependencies (`get_commit`, `get_list`), so they can be exercised without a chain,
without a relay and without an LLM. The rest of the module (I/O, the loop, the dendrad calls) is not
covered, and that is stated rather than implied.

The invariant that governs the whole file:
    an infrastructure failure is never billed to the honest party.
Every test below therefore asks, for a case of absence: is it attributable to the primary, or to an
incident that hits everyone? If the code cannot tell, it must abstain.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import judge_worker as jw
import reveal_marker as rmk


# -- fixture helpers: an in-memory chain and relay ------------------------------------------------
def fake_readers(commits=None, lists=None):
    """Returns (get_commit, get_list) reading two dicts. This is the injection the module provides
    explicitly, so that the decision is testable without a chain and without a relay."""
    commits = commits or {}
    lists = lists or {}
    return (lambda k: commits.get(k, "")), (lambda k: lists.get(k))


EB = 600          # blocks per epoch
DH = 1234         # dispute height -> epoch 2, coverage [2, 3]
PRIM = "mPrim"


def anchor(primary, epoch, jobs):
    """A coherent anchored marker: on-chain root = merkle(jobs), relay list = jobs."""
    return ({rmk.marker_key(epoch, primary): rmk.merkle_root(jobs)},
            {rmk.list_key(epoch, primary): {"jobs": jobs}})


# === (1) stage 1 — `marker_state`: True / False / None, and the None matters most ================

def test_job_present_in_an_anchored_and_verified_marker():
    c, l = anchor(PRIM, 2, ["jA", "jB"])
    gc, gl = fake_readers(c, l)
    state, reason = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is True
    assert "root verified" in reason


def test_no_anchored_marker_makes_the_zero_justified():
    # The only case where "0" is justified without further evidence: anchoring does not go through the
    # relay, so its absence cannot be attributed to a relay outage.
    gc, gl = fake_readers({}, {})
    state, reason = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is False
    assert "no marker anchored" in reason


def test_markers_anchored_but_not_this_job():
    c, l = anchor(PRIM, 2, ["jB", "jC"])
    gc, gl = fake_readers(c, l)
    state, reason = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is False
    assert "it revealed for others" in reason


# The two "undecidable" cases — this is where the invariant is decided. A root is anchored (so the
# primary did its on-chain work) but the off-chain list is missing or inconsistent. Concluding "0" here
# would bill a relay outage to the primary.
def test_root_anchored_but_list_not_found_UNDECIDABLE():
    c, _ = anchor(PRIM, 2, ["jA"])
    gc, gl = fake_readers(c, {})            # no list at the relay
    state, reason = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is None, "a relay outage must never become a slash"
    assert "stage 2" in reason


def test_a_list_that_does_not_match_the_root_UNDECIDABLE():
    c, _ = anchor(PRIM, 2, ["jA", "jB"])
    _, l_lying = anchor(PRIM, 2, ["jX", "jY"])     # list inconsistent with the anchored root
    gc, gl = fake_readers(c, l_lying)
    state, reason = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is None
    assert "does not match" in reason


@pytest.mark.parametrize("payload", [None, {}, {"jobs": None}, {"jobs": "jA"}, "not-a-dict", 42])
def test_a_malformed_relay_payload_UNDECIDABLE(payload):
    # Fail closed toward abstention, never toward a slash: unreadable data accuses nobody.
    c, _ = anchor(PRIM, 2, ["jA"])
    gc, gl = fake_readers(c, {rmk.list_key(2, PRIM): payload})
    state, _ = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is None


# The epoch boundary, on the judge side this time: a job disputed at the last block of an epoch is
# revealed in the next one. Looking at a single epoch declares "nothing anchored" for an honest primary.
def test_the_marker_of_the_NEXT_epoch_is_found():
    dh = EB - 1                                    # last block of epoch 0
    c, l = anchor(PRIM, 1, ["jA"])                  # anchored in epoch 1, not in epoch 0
    gc, gl = fake_readers(c, l)
    state, _ = jw.marker_state("jA", PRIM, dh, get_commit=gc, get_list=gl, epoch_blocks=EB, span=2)
    assert state is True, "an honest primary revealing just past the boundary would be declared markerless"


def test_span_1_no_longer_sees_the_next_epoch():
    # Documents the sensitivity to the parameter: `span` is what must cover the audit window.
    dh = EB - 1
    c, l = anchor(PRIM, 1, ["jA"])
    gc, gl = fake_readers(c, l)
    state, _ = jw.marker_state("jA", PRIM, dh, get_commit=gc, get_list=gl, epoch_blocks=EB, span=1)
    assert state is False


def test_a_root_anchored_in_UPPERCASE_stays_recognised():
    # The comparison is normalized to lowercase: a different case must not manufacture a mismatch
    # (hence an abstention) on a perfectly valid marker.
    jobs = ["jA", "jB"]
    c = {rmk.marker_key(2, PRIM): rmk.merkle_root(jobs).upper()}
    l = {rmk.list_key(2, PRIM): {"jobs": jobs}}
    gc, gl = fake_readers(c, l)
    state, _ = jw.marker_state("jA", PRIM, DH, get_commit=gc, get_list=gl, epoch_blocks=EB)
    assert state is True


# === (2) `primary_has_marker` — the deployment guard =============================================

def test_primary_has_marker_true_if_a_single_epoch_is_anchored():
    c, _ = anchor(PRIM, 3, ["jZ"])
    gc, _gl = fake_readers(c, {})
    assert jw.primary_has_marker(PRIM, DH, get_commit=gc, epoch_blocks=EB) is True


def test_primary_has_marker_false_if_nothing():
    gc, _ = fake_readers({}, {})
    assert jw.primary_has_marker(PRIM, DH, get_commit=gc, epoch_blocks=EB) is False


def test_primary_has_marker_ignores_an_EMPTY_root():
    # An entry that exists but is empty is not a marker: otherwise a blank commit would exonerate a
    # silent primary.
    gc, _ = fake_readers({rmk.marker_key(2, PRIM): "   "}, {})
    assert jw.primary_has_marker(PRIM, DH, get_commit=gc, epoch_blocks=EB) is False


# (3) `marker_stage_usable` — stage 1 arms itself, and that is a guard against a mass slash.
# On deployment day, no primary has anchored yet: "marker absent" would be true for everyone and the
# committee would vote "0" against honest miners. A universal absence is correlated, hence not
# attributable to an individual — the stage-2 reasoning, applied to stage 1 itself.
def test_stage1_unavailable_while_nobody_anchors():
    assert jw.marker_stage_usable(0) is False


@pytest.mark.parametrize("n", [1, 2, 7])
def test_stage1_arms_at_the_first_anchorer(n):
    assert jw.marker_stage_usable(n) is True


# === (4) `noreveal_verdict` — the decision, in two stages ========================================

def test_stage1_a_missing_marker_gives_ZERO_without_further_evidence():
    v, reason = jw.noreveal_verdict("jA", PRIM, marker=False, others_ok=0, others_missing=0,
                                   my_missing_for_primary=0)
    assert v == "0"
    assert "stage 1" in reason


# The case that protects the honest primary: everyone is silent, so it is the infrastructure, not it.
def test_stage2_CORRELATED_gives_ABSTENTION():
    v, reason = jw.noreveal_verdict("jA", PRIM, marker=None, others_ok=0, others_missing=4,
                                   my_missing_for_primary=3)
    assert v == "abstention"
    assert "stage 2: correlated" in reason


def test_stage2_ISOLATED_gives_ZERO():
    v, reason = jw.noreveal_verdict("jA", PRIM, marker=None, others_ok=3, others_missing=0,
                                   my_missing_for_primary=2)
    assert v == "0"
    assert "stage 2: isolated" in reason


@pytest.mark.parametrize("ok,miss", [(0, 0), (1, 0), (0, 1)])
def test_too_few_witnesses_ABSTENTION(ok, miss):
    # With a single witness, "isolated" and "correlated" are indistinguishable. Refusing to decide
    # without data is not decorative caution: it is the only correct answer.
    v, reason = jw.noreveal_verdict("jA", PRIM, marker=None, others_ok=ok, others_missing=miss,
                                   my_missing_for_primary=5, min_others=2)
    assert v == "abstention"
    assert "undecidable" in reason


def test_a_PRESENT_marker_is_not_enough_to_clear():
    # The marker is posted by the party under scrutiny (self-issued evidence): a cheater posts it
    # without ever publishing. `marker=True` must therefore not short-circuit stage 2.
    v, reason = jw.noreveal_verdict("jA", PRIM, marker=True, others_ok=3, others_missing=0,
                                   my_missing_for_primary=2)
    assert v == "0", "self-issued evidence must not be enough to close the case"
    assert "stage 2: isolated" in reason


def test_nobody_is_missing_anything_ABSTENTION():
    v, _ = jw.noreveal_verdict("jA", PRIM, marker=None, others_ok=5, others_missing=0,
                               my_missing_for_primary=0)
    assert v == "abstention"


def test_the_verdict_is_never_a_ONE():
    # This function never vindicates: it returns "0" or "abstention". A "1" coming out of here would
    # mean that a missing reveal counts as proof of good faith.
    for m in (True, False, None):
        for ok in range(4):
            for miss in range(4):
                v, _ = jw.noreveal_verdict("jA", PRIM, marker=m, others_ok=ok, others_missing=miss,
                                           my_missing_for_primary=miss)
                assert v in ("0", "abstention")


# === (5) `noreveal_action` — abstention is a state, not a verdict ================================

def test_during_the_grace_period_we_wait():
    a, d = jw.noreveal_action(now=100.0, first_seen=0.0, misses=1, grace=3)
    assert (a, d) == ("wait", 0.0)


def test_after_the_grace_period_we_RETRY_with_backoff():
    a, d = jw.noreveal_action(now=100.0, first_seen=0.0, misses=3, grace=3,
                              backoff_base=30.0, backoff_max=600.0)
    assert a == "retry"
    assert 0 < d <= 600.0


def test_the_backoff_grows_then_caps():
    ds = [jw.noreveal_action(now=1e6, first_seen=0.0, misses=3 + k, grace=3,
                             backoff_base=30.0, backoff_max=600.0)[1] for k in range(8)]
    assert ds == sorted(ds), "the backoff must grow"
    assert max(ds) <= 600.0, "and stay bounded"


# `2 ** (misses - grace)` raises OverflowError beyond roughly a thousand attempts. Since "retry without
# a horizon" is the nominal mode, a hundred thousand attempts is not a theoretical figure.
@pytest.mark.parametrize("misses", [1_000, 100_000, 10_000_000])
def test_no_overflow_even_after_millions_of_attempts(misses):
    a, d = jw.noreveal_action(now=1e9, first_seen=0.0, misses=misses, grace=3,
                              backoff_base=30.0, backoff_max=600.0)
    assert a == "retry"
    assert d == 600.0


def test_we_NEVER_give_up_by_default():
    # `abandon_after=0` means the chain decides when an audit is closed. A worker that gives up must
    # never decide the fate of an audit.
    a, _ = jw.noreveal_action(now=1e12, first_seen=0.0, misses=10_000, grace=3)
    assert a == "retry"


def test_we_give_up_only_if_the_operator_set_a_horizon():
    a, _ = jw.noreveal_action(now=1000.0, first_seen=0.0, misses=10, grace=3, abandon_after=500.0)
    assert a == "give_up"
    a2, _ = jw.noreveal_action(now=400.0, first_seen=0.0, misses=10, grace=3, abandon_after=500.0)
    assert a2 == "retry"


# === (6) `tx_reason` — the named extractor that replaced a positional truncation =================
# `raw_log` arrives after `events:` (thousands of characters), so a `[:400]` prefix drops the reason
# every time, turning already-closed disputes into false "real failure" reports. The lesson: a named
# extractor, never a positional truncation.
def test_raw_log_found_even_far_after_events():
    text = ("code: 0\n" + "events:\n" + ("- attribute: x\n" * 4000) +
             "raw_log: 'already resolved'\nother: 1\n")
    code, raw = jw.tx_reason(text)
    assert code == 0
    assert raw == "already resolved", "the reason must not depend on its position in the output"


def test_a_non_zero_code_is_extracted():
    code, raw = jw.tx_reason("code: 18\nraw_log: 'window still open'\n")
    assert code == 18 and "window" in raw


def test_empty_or_none_text_does_not_raise():
    for t in (None, "", "nothing at all"):
        code, raw = jw.tx_reason(t)
        assert code is None and raw == ""


# === (7) `is_disputed` — the entry predicate of the whole loop ===================================
@pytest.mark.parametrize("state,expected", [
    ("open+paid+optimistic+disputed", True),
    ("open+paid+optimistic+disputed+resolved", False),   # already settled: do not touch it again
    ("open+paid+optimistic", False),
    ("", False),
])
def test_is_disputed(state, expected):
    assert jw.is_disputed(state) is expected
