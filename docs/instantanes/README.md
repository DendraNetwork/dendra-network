# Snapshot manifests

Each file here anchors one snapshot of the chain state to a named block height, and each is a few
kilobytes. It is the only piece of the snapshot triplet that lives **inside** the repository: the block
archive and the binary are release assets, too large to commit, and a release asset does not survive a
clone. Whoever clones this repository gets the manifests.

A manifest records the chain id, the height H, the app hash **read from the header of block H+1** (the
app hash of the state after H appears in the *next* header — anchoring to H itself anchors nothing),
the digests of the three pieces, the validator and signature counts, and the build version the export
came from.

**What a manifest establishes is non-repudiation, not third-party verification.** This network runs a
single validator operated by the project, so the app hash it anchors carries a single signature — the
project's own. A manifest is a dated commitment its author can no longer withdraw. It is not evidence
against that author. Verifying independently means replaying the blocks with the published binary and
comparing the digests yourself.

Produced and checked by `dendra_snapshot.sh`; the reset procedure refuses to run without a valid one.

## Which manifest matters

`MANIFEST-dendra-h24206.txt` is the one that counts: it is the state of the first public chain at the
moment it was destroyed, on 2026-08-01 — 6 jobs, 1 miner, 2 verdicts, 33,936 udndr of retained miner
fees. Nothing of that chain survives anywhere else. The reset refused to run until this snapshot was
produced and checked.

`MANIFEST-dendra-h23516.txt` is a dry run of the same procedure, kept because a procedure nobody has
rehearsed is a procedure nobody can trust. It anchors a state that was later superseded, not destroyed.
