"""REVEAL MARKER (ADR-026, `reveal_marker.py`) — the Merkle tree a SLASH decision rests on.

This module answers one question: "did this primary reveal this job?" — and a "no" is paid in slashed
stake. Both directions are expensive:
  · a false NEGATIVE (a correct tree, a proof refused) slashes an HONEST primary;
  · a false POSITIVE (a proof accepted in error) lets a cheater produce a reveal it never made, which
    silently disarms the first stage of the anti-evasion path.
A Merkle tree is exactly the kind of code that looks right and is wrong on the edges — an ODD leaf
count, a single-leaf tree, an empty proof. All of them are exercised here.

What this file does NOT cover, stated rather than implied: the on-chain anchoring (`anchor_marker`), the
transport of the list by the relay, and the judge's decision (`marker_state`). This exercises the
primitive, not the pipeline built on it.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reveal_marker as rm


# ── (1) THE ROOT MUST DEPEND ON NEITHER ORDER NOR DUPLICATES ────────────────────────────────────
# Two primaries (or two runs of the same one) that revealed the SAME set must anchor the SAME root.
# Otherwise the root depends on arrival order, and no third party can recompute it.
@pytest.mark.parametrize("perm", [
    ["j1", "j2", "j3"],
    ["j3", "j1", "j2"],
    ["j2", "j3", "j1"],
    ["j3", "j2", "j1", "j2", "j3"],   # duplicates
])
def test_root_invariant_under_order_and_duplicates(perm):
    assert rm.merkle_root(perm) == rm.merkle_root(["j1", "j2", "j3"])


def test_root_empty_for_an_epoch_without_reveal():
    # An epoch with no reveal anchors NOTHING: an empty marker would suggest activity that never happened.
    assert rm.merkle_root([]) == ""
    assert rm.verify("", "j1", []) is False


# ── (2) EVERY LEAF PRESENT MUST BE PROVABLE — INCLUDING AT ODD SIZES ────────────────────────────
# The case that matters: at odd sizes `_levels` DUPLICATES the last node. That is the classic Merkle
# pitfall, and the only place an honest primary can be denied its proof. So this does not test "a case",
# it tests EVERY leaf for sizes 1 through 9.
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_every_leaf_proves_itself(n):
    ids = [f"job-{i:02d}" for i in range(n)]
    root = rm.merkle_root(ids)
    assert root, "a non-empty set must yield a non-empty root"
    for j in ids:
        prf = rm.proof(ids, j)
        assert prf is not None, f"{j} belongs to the tree: its proof cannot be None"
        assert rm.verify(root, j, prf) is True, (
            f"false NEGATIVE on {j} (n={n}): an HONEST primary would be slashed")


# ── (3) WHAT IS NOT IN THE TREE MUST NOT BE PROVABLE ────────────────────────────────────────────
def test_absent_job_has_no_proof():
    ids = ["a", "b", "c"]
    assert rm.proof(ids, "z") is None
    assert rm.verify(rm.merkle_root(ids), "z", []) is False


def test_proof_of_another_job_is_worthless():
    # Copying a neighbour's proof is the most obvious attack; it must fail.
    ids = ["a", "b", "c", "d"]
    root = rm.merkle_root(ids)
    prf_a = rm.proof(ids, "a")
    assert rm.verify(root, "b", prf_a) is False


def test_root_of_another_set_does_not_validate():
    ids = ["a", "b", "c"]
    other = ["x", "y", "z"]
    prf = rm.proof(ids, "a")
    assert rm.verify(rm.merkle_root(other), "a", prf) is False


# ── (4) FAIL CLOSED: an "almost correct" proof is worth NOTHING ─────────────────────────────────
# This function decides whether a "0" — and therefore a slash — is justified. It must never grant the
# benefit of the doubt: a malformed or truncated input is not a proof.
def test_missing_proof_refused():
    ids = ["a", "b", "c", "d"]
    assert rm.verify(rm.merkle_root(ids), "a", None) is False


def test_truncated_proof_refused():
    ids = ["a", "b", "c", "d"]
    root = rm.merkle_root(ids)
    prf = rm.proof(ids, "a")
    assert len(prf) >= 2
    assert rm.verify(root, "a", prf[:-1]) is False, "an incomplete proof must not pass"


def test_inverted_side_refused():
    # Swapping L/R changes the concatenation order: the recomputed root must differ.
    ids = ["a", "b", "c", "d"]
    root = rm.merkle_root(ids)
    prf = rm.proof(ids, "a")
    inverse = [("L" if s == "R" else "R", h) for s, h in prf]
    assert rm.verify(root, "a", inverse) is False


@pytest.mark.parametrize("bad", [
    [("L", "zz" * 32)],          # invalid hex
    [("L", "ab")],               # digest too short
    [("X", "00" * 32)],          # unknown side
    [("L", "00" * 33)],          # digest too long
    "not-a-list",                # unexpected type
])
def test_malformed_proof_refused(bad):
    ids = ["a", "b"]
    assert rm.verify(rm.merkle_root(ids), "a", bad) is False


# ── (5) DOMAIN SEPARATION: a digest computed elsewhere is not a leaf ────────────────────────────
# The 0x00 (leaf) / 0x01 (node) prefix plus the domain string stop any other protocol digest from being
# presented as a leaf of THIS tree. Without separation an internal node could be replayed as a leaf
# (second preimage), which would make the proof of a never-revealed job forgeable.
def test_the_leaf_is_not_a_bare_sha256():
    j = "job-42"
    bare = hashlib.sha256(j.encode()).digest()
    assert rm.leaf(j) != bare


def test_leaf_and_node_cannot_be_confused():
    a, b = rm.leaf("a"), rm.leaf("b")
    node = hashlib.sha256(rm.NODE_PREFIX + a + b).digest()
    leaf_of_the_same_content = hashlib.sha256(rm.LEAF_PREFIX + rm.DOMAIN + a + b).digest()
    assert node != leaf_of_the_same_content


def test_single_leaf_tree_the_root_IS_the_leaf_and_the_proof_is_empty():
    # An explicit edge case: here the empty proof is LEGITIMATE. That is why `verify` recomputes the root
    # instead of judging by the length of the proof.
    assert rm.merkle_root(["solo"]) == rm.leaf("solo").hex()
    assert rm.proof(["solo"], "solo") == []
    assert rm.verify(rm.merkle_root(["solo"]), "solo", []) is True
    # ...and that same empty proof must prove NOTHING against a larger tree.
    assert rm.verify(rm.merkle_root(["solo", "other"]), "solo", []) is False


# ── (6) EPOCHS: the partition that makes the quorum reachable ───────────────────────────────────
def test_epoch_of_is_an_integer_partition():
    assert rm.epoch_of(0, 600) == 0
    assert rm.epoch_of(599, 600) == 0
    assert rm.epoch_of(600, 600) == 1
    assert rm.epoch_of(1801, 600) == 3


def test_invalid_epoch_blocks_raises():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            rm.epoch_of(100, bad)


# The false slash by rounding, exercised explicitly: a job disputed JUST BEFORE an epoch boundary is
# revealed in the FOLLOWING epoch. Looking at a single epoch would report "marker absent" for a perfectly
# honest primary.
def test_coverage_crosses_the_epoch_boundary():
    eb = 600
    epochs = rm.covering_epochs(eb - 1, eb, span=2)     # last block of epoch 0
    assert rm.epoch_of(eb - 1, eb) in epochs
    assert rm.epoch_of(eb, eb) in epochs, (
        "the FOLLOWING epoch must be covered, otherwise an honest miner revealing just past the "
        "boundary is declared markerless")


def test_span_covers_at_least_one_epoch():
    # A degenerate span must not yield an EMPTY list: no epoch to look at means the marker is always
    # absent, which means a systematic slash.
    for span in (0, -3, 1):
        assert len(rm.covering_epochs(1234, 600, span=span)) >= 1


# ── (7) KEYS: their shape is stable, because they address on-chain state and the relay ──────────
def test_shape_of_the_keys():
    assert rm.marker_key(7, "m1") == "e7__reveals__m1"
    assert rm.list_key(7, "m1") == "markerlist__e7__m1"
    # Two distinct miners must NEVER share a key: one would overwrite the other's marker, and the second
    # would be declared markerless.
    assert rm.marker_key(7, "m1") != rm.marker_key(7, "m2")
    assert rm.marker_key(7, "m1") != rm.marker_key(8, "m1")
