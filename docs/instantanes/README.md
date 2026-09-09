# Snapshot manifests

Each file here anchors one snapshot of the chain state to a named block height, and each is a few
kilobytes. It is the only piece of the snapshot triplet that lives **inside** the repository: the block
archive and the binary are release assets, too large to commit, and a release asset does not survive a
clone. Whoever clones this repository gets the manifests.

A manifest records the chain id, the height H, the app hash **read from the header of block H+1** (the
app hash of the state after H appears in the *next* header — anchoring to H itself anchors nothing),
the digests of the three pieces, the validator and signature counts, and the build version the export
came from.

**What a manifest establishes is non-repudiation, not third-party verification.** The validator set is
small, entirely project-operated, and it CHANGES — the chain was relaunched on 2026-08-16 and its set was
rebuilt the same day. So do not read a count from this page: every manifest records `validators_at_H` and
how many of them signed, **for the height it anchors**, which is the only height those numbers speak for.

⚠️ **In manifests produced before 2026-08-16 the NON-REPUDIATION sentence says "a single validator"
regardless of what `validators_at_H` records** — three of the five in this directory contradict themselves
that way, `h208153`, `h23686` and `h28153` all recording `2`. The FIELD is authoritative; the sentence was
boilerplate until the producer was changed to derive it. They are not corrected in place on purpose: a
manifest is a sealed artifact whose other lines are digests, and hand-editing one would destroy exactly
the property it exists to carry. Signatures from a set the project controls do not
make an app hash independently attested. A manifest is a dated commitment its author can no longer
withdraw; it is not evidence against that author. Verifying independently means replaying the blocks
with the published binary and comparing the digests yourself.

**A manifest names WHICH chain it describes by `genesis_time`, and nothing else does.** `chain_id` is
reused verbatim across resets and heights restart from 1, so two manifests from two different networks
can carry the same id and overlapping heights — that is not hypothetical here: `h23516` and `h24206`
below are from a chain destroyed on 2026-08-01, and they straddle `h23686`, taken from a chain born on
2026-08-14. **Every manifest in this directory predates the field, `h23686` included** — it was produced hours
before the generator learned to record it. Date them by `anchor_captured_utc` and `block_H_time`, never
by their height. Only manifests produced after that change carry `genesis_time`, and a sealed manifest
is never edited afterwards: half its fields are digests, so a hand-added line would break the very
non-repudiation the file exists for.

Produced and checked by `dendra_snapshot.sh`; the reset procedure refuses to run without a valid one.

## Which manifest matters

Read this list by date, not by height — the heights overlap because the chains do not.

`MANIFEST-dendra-h24206.txt` (2026-08-01) is the state of the first public chain at the moment it was
destroyed — 6 jobs, 1 miner, 2 verdicts, 33,936 udndr of retained miner fees. Nothing of that chain
survives anywhere else. The reset refused to run until this snapshot was produced and checked.

`MANIFEST-dendra-h23516.txt` (2026-08-01) is a dry run of the same procedure, kept because a procedure
nobody has rehearsed is a procedure nobody can trust. It anchors a state that was later superseded, not
destroyed.

`MANIFEST-dendra-h208153.txt` (block time 2026-08-13T22:23:25Z, `validators_at_H = 2`) is the last state
of the chain that ran until the **2026-08-14** relaunch. It carries the highest number in this directory
and belongs to the oldest of the three chains — which is exactly why this list is ordered by date.

`MANIFEST-dendra-h23686.txt` (2026-08-15) anchors the chain born on 2026-08-14, taken during a **first,
interrupted** attempt at that chain's reset. Its app hash was cross-checked two ways — `block_results(23686)` against the header of block
23687 — and confronted against the live chain before the destruction. It records a chain that held
**nothing**: 0 jobs, 1 miner, 0 commits, 0 verdicts, 0 udndr retained. A snapshot of an empty state is
still worth taking; what it establishes is that nothing was lost, which is a claim no one could make
afterwards.

`MANIFEST-dendra-h28153.txt` (block time 2026-08-15T20:16:17Z, `validators_at_H = 2`) is what that same
chain held **immediately before the 2026-08-16 relaunch** — the snapshot that actually closes it. It and
`h23686` are not alternatives: the earlier one belongs to the attempt that was interrupted.
