#!/usr/bin/env python3
"""REVEAL MARKER — stage 1 of the two-stage silence test. PURE module.

WHAT IT CLOSES. "Missing reveal" covers two different worlds: a primary that did nothing, and an
infrastructure outage. Stage 1 makes the first case decidable WITHOUT going through the relay: the
primary anchors ON-CHAIN a marker of the jobs it has revealed. A missing anchor cannot be blamed on a
relay outage, because the anchoring does not go through the relay.

WHY IT IS BATCHED. One marker per EPOCH and per MINER, rooting every job revealed within the window:
the cost drops from one tx per job (which would double the traffic) to one tx per epoch per miner,
which is negligible. The discriminating property is unchanged; only the granularity differs.

WHY A ROOT AND NOT THE LIST. The list of jobs in an epoch is unbounded, so writing it on-chain would
make the cost grow with traffic — exactly what batching avoids. A Merkle ROOT is anchored instead (32
bytes, fixed size) and the list is served off-chain. The consequence is deliberate: if the list is
unavailable, the judge CANNOT conclude on stage 1 — it returns `marker=None` and STAGE 2
(correlation) decides. An outage therefore never becomes a slash: that is the exact composition of
the two stages, not a bypass.

CONFIDENTIALITY. The leaves are `sha256` hashes of job identifiers, data that is already public
on-chain. No content, no prompt, no answer. The marker therefore reveals nothing beyond what the
chain already shows.

TREE SECURITY. Leaves and internal nodes are DOMAIN-SEPARATED (prefixes 0x00 / 0x01). Without that,
an attacker can present an internal node as if it were a leaf — the classic Merkle second-preimage
attack — and "prove" membership of a job that was never revealed. Covered by tests.
"""
from __future__ import annotations

import hashlib

DOMAIN = b"dendra/revealmark/v1"
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EPOCH_BLOCKS_DEFAULT = 600          # ~10 min at 1 s/block: one tx per 10 min per miner at worst
MARKER_KIND = "revealmark"


def _h(*parts: bytes) -> bytes:
    d = hashlib.sha256()
    for p in parts:
        d.update(p)
    return d.digest()


def leaf(job_id: str) -> bytes:
    """Leaf = H(0x00 ‖ domain ‖ job_id). The domain separator prevents a digest computed elsewhere in
    the protocol from being reused as if it were a leaf of this tree."""
    return _h(LEAF_PREFIX, DOMAIN, job_id.encode("utf-8"))


def epoch_of(height: int, epoch_blocks: int = EPOCH_BLOCKS_DEFAULT) -> int:
    """Epoch of a block height. DETERMINISTIC and readable by everyone: that is what makes stage 1
    deterministic, and therefore the quorum reachable, since every judge reads the same chain and
    reaches the same conclusion. A split based on the workers' local clocks would not be."""
    if epoch_blocks <= 0:
        raise ValueError("epoch_blocks must be > 0")
    return int(height) // int(epoch_blocks)


def covering_epochs(dispute_height: int, epoch_blocks: int = EPOCH_BLOCKS_DEFAULT, span: int = 2):
    """Epochs in which the reveal of a job disputed at `dispute_height` may legitimately fall: the
    epoch of the dispute itself, and the following ones for as long as the audit window runs.

    ⚠️ Why more than one: a job disputed just before an epoch boundary would be revealed in the NEXT
    epoch. Looking at a single epoch would declare "marker missing" for a perfectly honest primary —
    a false slash produced by rounding. `span` must cover the audit window.
    """
    e = epoch_of(dispute_height, epoch_blocks)
    return [e + i for i in range(max(1, int(span)))]


def marker_key(epoch: int, miner_id: str) -> str:
    """Key of the on-chain commit. Same shape as a verdict (`<jobId>__verdict__<minerId>`): it REUSES
    `create-commit`, so no new proto message, no consensus break, and it deploys without a regenesis.
    An anchored commit is IMMUTABLE: a marker cannot be rewritten after the fact."""
    return f"e{int(epoch)}__reveals__{miner_id}"


def list_key(epoch: int, miner_id: str) -> str:
    """RELAY key of a marker's leaf list. The root is on-chain (fixed size), the list is off-chain —
    otherwise the cost would grow with traffic, which batching exists to prevent. The relay is
    PERSISTENT, so this list survives a restart; and if it is missing anyway, the judge returns
    `marker=None` and stage 2 decides. An outage therefore cannot turn into a slash."""
    return f"markerlist__e{int(epoch)}__{miner_id}"


def _levels(job_ids):
    """Tree levels, bottom up. The jobs are DEDUPLICATED and SORTED: two primaries (or two runs) that
    revealed the same set produce the SAME root, whatever the arrival order. Otherwise the root would
    depend on scheduling and could not be verified."""
    leaves = [leaf(j) for j in sorted(set(job_ids))]
    if not leaves:
        return [[]]
    levels = [leaves]
    cur = leaves
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur), 2):
            a = cur[i]
            b = cur[i + 1] if i + 1 < len(cur) else cur[i]   # odd count: the last node is duplicated
            nxt.append(_h(NODE_PREFIX, a, b))
        levels.append(nxt)
        cur = nxt
    return levels


def merkle_root(job_ids) -> str:
    """Hex root (64 characters), or "" when there is no job. An epoch without a reveal anchors
    NOTHING: an empty marker would suggest activity that did not happen."""
    lv = _levels(job_ids)
    return lv[-1][0].hex() if lv[-1] else ""


def proof(job_ids, job_id: str):
    """Membership proof: a list of (side, digest_hex) from the bottom up. [] when absent — and [] is
    also the valid proof for a single-leaf tree, which is why the verification below compares the
    recomputed root and never the proof's length alone."""
    uniq = sorted(set(job_ids))
    if job_id not in uniq:
        return None
    idx = uniq.index(job_id)
    out = []
    for lvl in _levels(uniq)[:-1]:
        sib = idx ^ 1
        if sib >= len(lvl):
            sib = idx                       # last element, duplicated
        out.append(("R" if sib > idx else "L", lvl[sib].hex()))
        idx //= 2
    return out


def verify(root_hex: str, job_id: str, prf) -> bool:
    """Does the job belong to the tree rooted at `root_hex`? Refuses anything that is not proven.

    Fail closed: an empty root, a missing proof or a malformed digest all yield False. An
    "almost correct" proof is worth nothing: this function decides whether a "0" verdict is
    justified, and it must never give the benefit of the doubt.
    """
    if not root_hex or prf is None:
        return False
    try:
        cur = leaf(job_id)
        for side, hexval in prf:
            sib = bytes.fromhex(hexval)
            if len(sib) != 32 or side not in ("L", "R"):
                return False
            cur = _h(NODE_PREFIX, sib, cur) if side == "L" else _h(NODE_PREFIX, cur, sib)
        return cur.hex() == root_hex.lower()
    except (ValueError, TypeError):
        return False
