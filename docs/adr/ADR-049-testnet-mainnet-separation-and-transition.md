# ADR-049 — Separating testnet from mainnet, and the transition to mainnet

**Status:** Accepted by the project owner, 2026-10-06 (decisions 1 to 4). The items under *Open* are not
decided.
**Implementation:** Decision 1 is applied to the kit, the site and the published texts. The work items
below are not implemented.

## Context

The repository and its kit describe exactly one network:

- the chain id is written once, in the network file (`deploy/launch/.env.public.example`), and every
  service refuses to start without it;
- the genesis pin `docker/GENESIS_SHA256` holds one `<chain_id>=<sha256>` line, which `join.sh` reads with
  `head -1` and the launcher rewrites at a reset;
- `docker/CONSENSUS_EPOCH` holds one epoch, and `join.sh::verify_consensus_epoch` refuses a network at another
  epoch;
- the kit directories are named `deploy/testnet`, `deploy/testnet-node` and `deploy/testnet-miner`, and no
  script takes a network flag;
- the installers default to the network file served by the testnet host;
- the bech32 prefix (`dendra`, compiled in `chain/app/app.go`), the coin type (118) and the denom (`udndr`)
  are the same on every network, so one key gives the same address on a testnet and on a mainnet built from
  this code;
- the public repository is published as a single root commit on `main`; its releases were numbered
  `v0.1.x`, a line since withdrawn ([ADR-050](ADR-050-release-versioning.md)).

Mainnet launches after the Final Testnet Season ([ADR-047](ADR-047-final-testnet-season-reward-programme.md),
decision 13). A mainnet is never reset: every change to its state machine after launch is a governed
upgrade (`x/upgrade`, `chain/app/upgrades.go`), not a fresh genesis.

## Decisions

1. **Separate hostnames from the testnet's launch.** The testnet is served at
   `testnet-api.dendranetwork.com` and `testnet-proof.dendranetwork.com`; `api.` and `proof.` are kept for
   mainnet. The testnet's DNS records do not exist yet, so this costs nothing now, while moving a running
   testnet later would break the configuration of every participant.
2. **Season points are credited in the mainnet genesis.** One mainnet DNDR per point (ADR-047, decision 15):
   each address is credited with exactly the testnet DNDR the season's payments sent to it. Before launch,
   the list of addresses and amounts is published with its hash, and a verification period follows; anyone
   can recompute it from the published rankings and from the payout transfers on chain. There is no claim
   module and no later transfer. A participant keeps the key that received the season's payments: the same
   key controls the same address on mainnet.
3. **The testnet stops when mainnet launches.** There is no public staging network after launch. A governed
   upgrade has never been run on a live chain of this project, so one is rehearsed on `dendra-testnet` during
   the season; after launch, upgrades are rehearsed on private networks before they reach mainnet.
4. **Mainnet starts on the project's validator**, as the testnet does, and opens progressively: independent
   validators join, and governance raises `committee_min_vrf_contributors` so the work and audit draws are
   seeded by several of them.

## Transition

1. During the season: the work items below, and the rehearsal of a governed upgrade on `dendra-testnet`.
2. End of the season: the last payment; the list of addresses and amounts, with its hash, in the repository;
   the verification period.
3. Mainnet genesis: chain id, allocation (the season's conversion included, at most 1 500 DNDR), parameters,
   the project's validator.
4. Launch: the genesis and its pin published, release `v1.0.0` with its binaries; the site reads mainnet;
   the testnet stops.

## Work items

- Per-network declarations instead of one-network files: a directory per chain id (genesis pin, consensus
  epoch, network file URL), a genesis pin keyed by chain id instead of its first line, and a `--network`
  option in `install.sh`, `install.ps1` and `join.sh`.
- Kit directory names that do not name a network, and a miner kit whose compose project and identity volume
  can be told apart per network.
- A mainnet genesis builder, without the testnet's faucet, request generator and season payout accounts.
- The tool that turns the season's payments into the list of decision 2.
- The release pipeline: decided in [ADR-050](ADR-050-release-versioning.md) — the release workflow is
  the only creator and attaches the reproducible `dendrad` binaries and their checksums, and the numbers
  follow the two gates of `VERSION`. Still open: from mainnet on, the public repository keeps its history
  so that operators can read what changed between two releases.

## Open

- The mainnet chain id (proposed: `dendra-1`).
- The mainnet genesis allocation, including the pocket the season's conversion is taken from.
- The mainnet parameter values; by default, the testnet's launch values.
- The price of a request on the mainnet gateway (`DENDRA_FEE`, `DENDRA_PER_TOKEN`), which the chain does not
  set.
- The length of the verification period.
- A ranked amount the payment set aside, for an address it could not pay: decision 2 credits what the
  payments sent, so such an amount is credited only if it is paid before the list is drawn up.

## Consequences

- One key, one address on both networks: a season participant who loses the key that received the payments
  loses the mainnet credit too. The texts tell participants to keep it.
- The testnet's endpoints and every text that names them move to the `testnet-` hostnames before the testnet
  launches.
- After launch, nothing is tested in public before it reaches mainnet; the private rehearsal is the only
  stage.
