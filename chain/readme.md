# dendra
**dendra** is a blockchain built using Cosmos SDK and Tendermint and created with [Ignite CLI](https://ignite.com/cli).

## Get started

```
ignite chain serve
```

`serve` command installs dependencies, builds, initializes, and starts your blockchain in development.

### Configure

Your blockchain in development can be configured with `config.yml`. To learn more, see the [Ignite CLI docs](https://docs.ignite.com).

### Web Frontend

Additionally, Ignite CLI offers a frontend scaffolding feature (based on Vue) to help you quickly build a web frontend for your blockchain:

Use: `ignite scaffold vue`
This command can be run within your scaffolded blockchain project.


For more information see the [monorepo for Ignite front-end development](https://github.com/ignite/web).

## Release
Releases are tags `vMAJOR.MINOR.PATCH` of the public repository, and their numbers follow the two gates
of `VERSION`: see `CHANGELOG.md` at the repository root and `docs/adr/ADR-050-release-versioning.md`.
Pushing the tag is the whole of a release: the release workflow verifies the tagged tree, builds static
`dendrad` binaries and the miner image, and creates the release with them.

### Install
Each release carries static `dendrad` binaries for Linux and macOS, their checksums, and the steps to
rebuild them byte for byte from the tag. To run a node or a miner, use `deploy/join.sh`, or
`deploy/install.sh` on a bare machine, from the repository root.

## Learn more

- [Ignite CLI](https://ignite.com/cli)
- [Tutorials](https://docs.ignite.com/guide)
- [Ignite CLI docs](https://docs.ignite.com)
- [Cosmos SDK docs](https://docs.cosmos.network)
- [Developer Chat](https://discord.com/invite/ignitecli)
