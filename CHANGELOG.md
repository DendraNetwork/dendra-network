# Changelog

Each release of this repository is a tag `vMAJOR.MINOR.PATCH` with a section below, and its GitHub
release carries that section. The repository is republished as a single commit, so `git log` cannot
compare two releases: this file does.

## How the version number moves

The number follows the two gates of `VERSION` ([ADR-050](docs/adr/ADR-050-release-versioning.md)):

- **`consensus_epoch` moved**: the state machine changed, and a node left on the previous binary forks.
  The **minor** number moves and the patch number returns to 0.
- **`kit_version` moved alone**: the scripts and services an operator runs changed; a kit one version
  behind still joins and is told to update. The **patch** number moves.
- **Neither moved**: documentation, the website, or services only the network runs. No number: the
  repository is republished without a release, save an exceptional release whose section says why; it
  moves the patch number.

`v1.0.0` is the mainnet genesis. The earlier `v0.1` line designated trees of the previous testnet and was
withdrawn; its names are not reused.

## v0.2.0

The `dendra-testnet` network, at kit version 1 and consensus epoch 14.

- A new chain from a fresh genesis. Nothing carries over from the previous testnet: balances,
  registrations and stakes start empty. A miner key survives in its Docker volume and registers again.
- Consensus epoch 14: its state-machine changes are listed in `docker/CONSENSUS_EPOCH`.
- The Final Testnet Season pays miners until 7 November 2026, 23:59 UTC, read on block timestamps
  ([ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md)).
- `deploy/install.sh` (Linux) and `deploy/install.ps1` (Windows) prepare a bare machine and run
  `deploy/join.sh`; on a Linux desktop, `install.sh` also adds the Dendra application.
- Judging runs on the CPU: the judge seat needs at least 26 000 MB of system RAM, with or without a GPU,
  and runs the judge model the network pins; no judge is seated on the GPU.
- Release files: static `dendrad` binaries for Linux and macOS (amd64, arm64) with their checksums, and
  the digest of the prebuilt miner image (`miner-image.txt`).

What to do: join with `deploy/install.sh`, or with `deploy/join.sh` on a host that already runs Docker.
A clone of the previous testnet is brought to this tree by downloading `deploy/install.sh` again and
running it: it moves the existing clone in place, which `git pull` cannot do.
