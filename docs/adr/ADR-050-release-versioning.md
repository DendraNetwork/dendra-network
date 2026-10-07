# ADR-050 — Release numbers follow the two gates, and the release workflow is the only creator

**Status:** Accepted by the project owner, 2026-10-07, first release `v0.2.0` included. The `v0.1` tags
and releases were withdrawn by the owner the same day.
**Implementation:** The numbering rule, the single creator and the change log are implemented in the
release tooling and in `.github/workflows/release.yml`; the first release of the new line is `v0.2.0`.

## Context

A clone carries two numbers in `VERSION`, and they are the only two that tell an operator to act:

- `consensus_epoch` — the state machine. A node on another epoch forks, and `deploy/join.sh` refuses it.
- `kit_version` — the scripts and services an operator runs. A kit one version behind still joins, and
  `deploy/join.sh` warns.

The release tags of the `v0.1` line followed neither. Measured on 2026-09-10: four numbers in 4 h 58, and
over five releases neither gate moved; only the source commit did, which `VERSION` itself declares as
provenance. A series that moves while no gate moves teaches its reader that the numbers mean nothing.

The releases also shipped nothing. Measured on 2026-10-07, on the public repository: the release workflow
ran five times (`v0.1.3` to `v0.1.7`) and failed five times at its last step, *Create the release*, while
the verification and the four builds passed every time. The maintainers' tagging step had already created
the release, so the workflow's own creation collided with it. The two releases still listed carried no
file: the reproducible `dendrad` binaries and their checksums, which the workflow exists to publish, never
reached a release.

The `v0.1` tags were withdrawn on 2026-10-07, on both repositories, with the releases. Their names
designated trees of the previous testnet, and a clone that kept one keeps it: measured with a fresh root
commit reusing a withdrawn name, `git fetch` returns 0 and says nothing, and the clone's tag still points
at the old tree, so `git checkout` of that name gives the old code.

## Decisions

1. **Format `vMAJOR.MINOR.PATCH`**, without prefix variants, suffixes or leading zeros.
2. **The number follows the gates, and is derived rather than typed.** From the last release published on
   the public repository:
   - `consensus_epoch` moved: the minor number moves and the patch number returns to 0;
   - `kit_version` moved alone: the patch number moves;
   - neither moved: no number. The repository is republished without a release. A number is still
     possible on an explicit, written reason, which the release's section of `CHANGELOG.md` states; it
     moves the patch number;
   - a gate that moves backwards is refused: the published tree would be older than the last release.

   The tagging tool computes the number itself and refuses any other, except the next major version,
   which is declared explicitly. With no release published, the number is the first one of the line
   (decision 4) and no other.
3. **`v1.0.0` is the mainnet genesis** (ADR-049). The rule after mainnet, where every change to the state
   machine is a governed upgrade, is set with the mainnet.
4. **The `v0.1` line is retired; a published name is never reused.** The first release of the new line is
   `v0.2.0`: `dendra-testnet` was launched on consensus epoch 14 (2026-10-07, `docker/CONSENSUS_EPOCH`),
   a new network, which under decision 2 is a new minor number in any case.
5. **The release workflow is the only creator of a release.** Pushing the tag is the whole of the
   maintainers' part. The workflow verifies the tagged tree, builds the binaries and the miner image, and
   then creates the release with its files. A re-run after a partial failure completes the existing
   release instead of failing on it.
6. **The notes are read from the tagged tree**, by `.github/release_notes.sh`: the two gates from
   `VERSION`, the summary of the changes from the tag's section of `CHANGELOG.md`. A missing field or
   section stops the release. The tagging tool runs the same script on the published tree before it
   tags, so a missing section is refused before a tag exists. A relative link of the section is
   rewritten to the same file at the tag, since the release page would not resolve it.
7. **`CHANGELOG.md` is the comparison between two releases.** The repository is republished as a single
   root commit, so `git log` between two tags says nothing; each release has a section there, written for
   an operator: what changed, and what to do.
8. **A tagged tree is certified and pins no miner image.** The tagging tool refuses a published tree
   whose build seal declares uncommitted sources, and one whose `docker/MINER_IMAGE` names an image: that
   image was built from another tree. A release's own image is pinned after its run, by a republication,
   with the line `# built-for:` that names the gates it was built for. Joiners clone `main`, not the tag,
   so the publication itself refuses a pin whose gates differ from the tree being published; the pin is
   emptied before either gate moves.
9. **Binaries are named after release tags only.** The launcher names the chain binary with
   `git describe --match 'v[0-9]*'`; without the pattern, the nearest tag of any kind in the maintainers'
   repository named the binary that the network runs.

## Consequences

- A release is rare, and its number says what an operator has to do: a new minor number means a new
  binary, a new patch number means an updated kit, or an exceptional release whose section of
  `CHANGELOG.md` says why.
- Each release carries its `dendrad` binaries, their checksums, and the miner image digest. The digest
  reaches joiners once it is committed to `docker/MINER_IMAGE` and the repository is republished, which
  is a republication, not a release. The container package has to be public for the pull to succeed.
- The workflow fails at verification, before the platform builds and the image, when the notes cannot
  be written.
- Between a release and the republication that pins its image, a joiner builds the miner image from the
  clone, as `deploy/join.sh` already does when no image is pinned.
- What changed between two releases is read in `CHANGELOG.md`, not in the history, until the public
  repository keeps its history (from mainnet on, ADR-049).
