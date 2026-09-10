#!/usr/bin/env python3
"""Tests for the reveal marker (tier 1). Run with `python3 test_reveal_marker.py`.

What these tests defend: **a membership proof must be impossible to forge**, because it is what
decides that a "0" - and therefore a slash - is justified. And **a root must not depend on arrival
order**, otherwise two honest judges compute different things and quorum is never reached.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reveal_marker as rm

OK = [0]
FAIL = []


def chk(name, cond, extra=""):
    if cond:
        OK[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {extra}")


JOBS = [f"job17849192{i:03d}" for i in range(7)]

# --- epochs: deterministic, and the boundary must not create a false "absent" --------------------
chk("(1) epoch_of splits by blocks", rm.epoch_of(0, 600) == 0 and rm.epoch_of(599, 600) == 0
    and rm.epoch_of(600, 600) == 1 and rm.epoch_of(15040, 600) == 25)
def _refuse_zero():
    try:
        rm.epoch_of(10, 0)
        return False
    except ValueError:
        return True


chk("(1b) epoch_blocks <= 0 refused (a silent division by zero would be worse)", _refuse_zero())
chk("(2) covering_epochs covers the following epoch - a job disputed just before the boundary is"
    " revealed AFTER it, and looking at only one epoch would produce a false 'marker absent'",
    rm.covering_epochs(599, 600) == [0, 1] and rm.covering_epochs(600, 600) == [1, 2])
chk("(2b) span is configurable, and never empty", rm.covering_epochs(600, 600, span=3) == [1, 2, 3]
    and rm.covering_epochs(600, 600, span=0) == [1])
chk("(3) the marker key is stable and readable",
    rm.marker_key(25, "miner-2") == "e25__reveals__miner-2")

# --- root: deterministic, independent of order ---------------------------------------------------
r1 = rm.merkle_root(JOBS)
r2 = rm.merkle_root(list(reversed(JOBS)))
r3 = rm.merkle_root(JOBS + JOBS)          # duplicates
chk("(4) the root does NOT depend on arrival order (otherwise judges diverge)", r1 == r2, f"{r1[:12]} vs {r2[:12]}")
chk("(4b) ...nor on duplicates", r1 == r3)
chk("(4c) an epoch with NO reveal anchors nothing (no misleading empty marker)",
    rm.merkle_root([]) == "")
chk("(4d) a different set gives a different root",
    rm.merkle_root(JOBS[:-1]) != r1)
chk("(4e) root = 32 bytes, hex-encoded", len(r1) == 64)

# --- proof: it proves, and only what it proves ---------------------------------------------------
allok = all(rm.verify(r1, j, rm.proof(JOBS, j)) for j in JOBS)
chk("(5) every revealed job has a VALID proof (7 leaves = an odd count at each level)", allok)
chk("(5b) a job that was NOT revealed has no proof", rm.proof(JOBS, "job-never-revealed") is None)
chk("(5c) ...and a proof borrowed from another job does not save it",
    not rm.verify(r1, "job-never-revealed", rm.proof(JOBS, JOBS[0])))
chk("(6) truncated proof -> REFUSED", not rm.verify(r1, JOBS[0], rm.proof(JOBS, JOBS[0])[:-1]))
chk("(6b) digest altered by one bit -> REFUSED",
    not rm.verify(r1, JOBS[0], [("R", "ff" + rm.proof(JOBS, JOBS[0])[0][1][2:])]
                  + rm.proof(JOBS, JOBS[0])[1:]))
chk("(6c) side inverted -> REFUSED",
    not rm.verify(r1, JOBS[0], [("L" if s == "R" else "R", h) for s, h in rm.proof(JOBS, JOBS[0])]))
chk("(6d) empty root / None proof -> REFUSED (fail closed, never the benefit of the doubt)",
    not rm.verify("", JOBS[0], rm.proof(JOBS, JOBS[0])) and not rm.verify(r1, JOBS[0], None))
chk("(6e) malformed proof (invalid hex, wrong length) -> REFUSED, and no exception",
    not rm.verify(r1, JOBS[0], [("R", "zz")]) and not rm.verify(r1, JOBS[0], [("R", "aabb")]))

# --- second preimage: an internal node must NOT be able to pass for a leaf -----------------------
# Without domain separation (0x00 for a leaf, 0x01 for a node), an attacker presents the
# CONCATENATION of two leaves as if it were a leaf and "proves" the membership of a job that was never
# revealed. This is the classic Merkle attack, and here it would produce a slash on an honest miner.
two = ["a", "b"]
root2 = rm.merkle_root(two)
leaf_a, leaf_b = rm.leaf("a"), rm.leaf("b")
import hashlib
forged_leaf_material = (leaf_a + leaf_b).hex()
chk("(7) an internal node cannot be presented as a leaf (second preimage closed)",
    not rm.verify(root2, forged_leaf_material, []),
    "the 0x00/0x01 domain separation is missing")
chk("(7b) the leaf and the node of the SAME content have different digests",
    rm.leaf("x") != hashlib.sha256(rm.DOMAIN + b"x").digest())

# --- (8) single-leaf tree: the proof is empty, but it must still be VERIFIED ---------------------
one = ["job-solo"]
chk("(8) single-leaf tree: an empty proof is VALID for the right job",
    rm.verify(rm.merkle_root(one), "job-solo", rm.proof(one, "job-solo")))
chk("(8b) ...and an empty proof is NOT valid for another job (the root is compared, not the length)",
    not rm.verify(rm.merkle_root(one), "other-job", []))

# --- (9) SWEEP over 1 to 40 leaves. Hand-picked cases miss the non-power-of-two sizes, and that is
# exactly where duplicating the last element can manufacture a false proof.
bad_sizes = []
for n in range(1, 41):
    js = [f"job{i:04d}" for i in range(n)]
    root = rm.merkle_root(js)
    if not all(rm.verify(root, j, rm.proof(js, j)) for j in js):
        bad_sizes.append(("member not proven", n))
    if rm.verify(root, "intruder", rm.proof(js, js[0]) or []):
        bad_sizes.append(("intruder accepted", n))
    if n > 1 and rm.verify(root, js[0], rm.proof(js, js[1])):
        bad_sizes.append(("cross proof accepted", n))
chk("(9) sweep over 1 to 40 leaves: every member proves, no intruder passes, no cross proof passes",
    not bad_sizes, str(bad_sizes[:4]))

print(f"\n{OK[0]} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
