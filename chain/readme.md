# chain

The source of the Dendra chain: the application (`app/`), the commands (`cmd/dendrad`, `cmd/dendra-vrf`),
the modules (`x/`) and their wire format (`proto/`).

## Build

```
go build -trimpath -o dendrad ./cmd/dendrad
```

The protocol buffer code under `x/*/types` is generated from `proto/` by `make proto-gen`;
`make proto-check` regenerates it in a staging directory and compares it with the committed files.
`config.yml` holds the settings of a local development chain.

## Release
Releases are tags `vMAJOR.MINOR.PATCH` of the public repository, and their numbers follow the two gates
of `VERSION`: a change of `consensus_epoch` raises the minor number, a change of `kit_version` alone
the patch number, and `v1.0.0` is the mainnet genesis. `CHANGELOG.md` at the repository root lists each release.
Pushing the tag is the whole of a release: the release workflow verifies the tagged tree, builds static
`dendrad` binaries and the miner image, and creates the release with them.

### Install
Each release carries static `dendrad` binaries for Linux and macOS, their checksums, and the steps to
rebuild them byte for byte from the tag. To run a node or a miner, use `deploy/join.sh`, or
`deploy/install.sh` on a bare machine, from the repository root.
