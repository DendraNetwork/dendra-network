# ADR-053 — Cloud pods and HiveOS rigs: a miner-only pod paid to a cold address, and a HiveOS package that asks for consent and invents no hashrate

**Status:** Accepted by the project owner, 2026-10-08 — implementation in progress.
**Implementation:** Decided, not implemented; the work is in progress. The cloud image, its entrypoint and
the HiveOS package are new artifacts; the daemon's status file and the payout lock change what an operator
runs and ship under the single `kit_version` bump shared by ADR-051 to ADR-055. The lock on the season
service is network-side. Not consensus-breaking.

## Context

- A rented GPU pod runs one container from an image, offers no Docker inside it, and stores data on a disk
  its host can read; its storage may not survive a restart. The kit assumes a Docker host and runs the
  miner, its Ollama, and optionally a node and a judge, as compose services.
- A pod's signing key lives on a machine someone else controls. The season pays the address an identity
  declared, else its operator (`services/final_season_facts.py::day_identities`), and accepts a
  new declaration from the identity's key once per
  `services/final_season_server.py::DECLARE_EVERY_BLOCKS`: a host that reads the key can declare
  its own address.
- HiveOS runs a miner package as root from a flight sheet, shows its hashrate, and can restart a miner whose
  hashrate falls. It installs a package by downloading the archive its URL names.
- The faucet limits its grants per source IP address (`services/faucet.py::IP_DAILY`); pods
  behind a shared address can stay unfunded.
- A devnet bench for rented machines exists outside the published tree: it bypasses the joining checks and
  pipes an installer into a shell.

## Decisions

### Cloud pods

1. **A pod is a miner, never a judge.** A pod that dies would leave a mute jury seat, and a judge needs the
   RAM `deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB` names, which a rented host shares badly.
2. **One container image, `dendra-miner-cloud`.** An Ollama base pinned by digest, Python and the miner's
   dependencies, and `dendrad`, `dendra-vrf` and the miner's code copied from the miner image built in the
   same release job: the same bytes, no second build. It carries `deploy/` and the gate files of the
   tagged tree. Its entrypoint starts Ollama on loopback, pulls the model the probe chooses and the
   embedder, then starts the miner on a remote RPC. No inbound port, no tunnel, no token in the image.
3. **A cold payout address is mandatory.** The entrypoint refuses to start without
   `DENDRA_PAYOUT_ADDRESS`, checked by `services/final_season_address.py::payable_address`: the
   owner's public address, whose key never reaches the pod. No phrase and no importable key is accepted.
   Once the identity is resolved the entrypoint declares the address, retries until the programme accepts
   or refuses it, records the answer, and prints "payout NOT declared" until then.
4. **The payout address can be locked on the season service.** A signed declaration may carry a `lock`
   flag. Once an identity's address is locked, a different address is refused and the same one accepted,
   and the lock survives a restart of the service. A lock is accepted only as the identity's first
   declaration, or from a Creator that has always declared in owner mode
   ([ADR-047](ADR-047-final-testnet-season-reward-programme.md), *Payout address*). So it protects the pay
   only if the pod's own locked declaration is the identity's first: a host that reads the key and
   declares first, locked, fixes its own address for good, and one that declares first without the lock
   leaves the identity unlockable. The pod therefore declares, with the lock, as soon as its miner is
   registered, and records a refused lock as final. The cost is accepted: a wrong address, once locked,
   cannot be corrected, so the address is checked before the pod starts.
5. **A persistent volume is required.** Without a persistent workspace the entrypoint refuses, unless the
   operator sets the named opt-out `DENDRA_ACCEPT_EPHEMERAL=1`: without one, the identity and its bond die
   at the first restart. The keys and the models live on that volume.
6. **One card per pod**, until per-card identities ship in the pod
   ([ADR-054](ADR-054-one-identity-per-card.md)). A pod with more cards is refused unless the operator
   accepts idle cards through a named opt-out.
7. **The same network checks as a join, from one implementation.** The entrypoint sources `deploy/join.sh`
   in its library mode (`DENDRA_JOIN_LIB`) and runs its three checks, each with the answers it gives a join
   on `--remote-rpc`. `deploy/join.sh::verify_genesis_info` refuses a genesis it cannot download or that
   differs from the hash served with it; and, unless `DENDRA_ALLOW_UNVERIFIED_GENESIS=1`, a genesis it has
   nothing to check against or that differs from the clone's `docker/GENESIS_SHA256`.
   `deploy/join.sh::verify_consensus_epoch` refuses only an epoch the network declares and the tree does
   not carry, unless `DENDRA_ALLOW_EPOCH_MISMATCH=1`; an epoch the network does not declare, or a
   `docker/CONSENSUS_EPOCH` it cannot read, passes with a warning.
   `deploy/join.sh::verify_kit_version` never refuses: a kit behind the network still runs, and is told.
   Its update advice names the image digest to redeploy.
8. **Supervision.** The entrypoint waits on its processes and exits non-zero as soon as one dies, so that
   the platform restarts the pod.
9. **`docker/CLOUD_IMAGE` pins the cloud image** with the discipline of `docker/MINER_IMAGE`: a digest,
   a `built-for` line, empty when a release is tagged.

### HiveOS rigs

10. **A HiveOS package.** The flight sheet's wallet field is the payout address, its pool URL an optional
    configuration URL, and its extra configuration an allow-list (`YES=1`, `ROLE=miner|judge`,
    `LIGHT=1`). A value carrying a quote, a `$` or a space is refused; an unknown key is ignored and named.
11. **Consent is `YES=1`.** Without it, the package runs the installer's plan (`install.sh --check`) and
    waits; with it, `install.sh --yes` with the chosen role, then the payout declaration, then it follows
    the miner's logs. A stop from HiveOS stops the compose project by name (`-p`): otherwise the containers
    keep running after "stop miner".
12. **The release tag is baked into the package.** Each version of the package clones the tag it names,
    never `main`, and never pulls at start; an update is a new package URL.
13. **Honest statistics.** The package reports a hashrate of zero and, as accepted and rejected shares, the
    commits the daemon anchored and the commits refused, read from the daemon's local status file
    ([ADR-055](ADR-055-miner-lifecycle.md)). When that file cannot be read, the shares field is left out,
    unknown, never reported as zero. HiveOS's hashrate watchdog has to be disabled for the rig; the
    documentation says so.
14. **The archive and its checksum are release assets**, attached by the release workflow, the only creator
    of a release ([ADR-050](ADR-050-release-versioning.md)).
15. **The devnet bench for rented machines stays unpublished**; the published guides point to the cloud
    documentation.

## Set aside

- **Publishing the devnet bench for rented machines:** it skips the checks of `join.sh` and pipes an
  installer into a shell.
- **Docker inside the pod, to reuse the compose files:** pods do not offer nested Docker.
- **Copying the genesis and epoch checks into the entrypoint:** a second implementation, bound to drift.
- **A tunnel and a relay token baked into the image:** the miner only connects outward, and a shared token
  contradicts the relay's per-route access policy.
- **A reward address carried by `create-miner` on chain:** consensus-breaking, for a protection the
  season service's lock gives during the season; it is decided for the mainnet genesis in
  [ADR-051](ADR-051-miner-keys.md), decision 11.
- **`git pull` at each HiveOS start:** an update with neither consent nor a pin.
- **Tokens per second, or requests, in the hashrate field:** an invented hashrate.
- **A second compilation of `dendrad` in the cloud image:** its build flags could drift from the release's.

## Consequences

- The rented host can read the hot key: it can withdraw the pod's testnet funds, sign false commits and
  have the pod's own bond slashed, and, unless the pod's locked declaration came first, redirect the pay.
  An encrypted keyring ([ADR-051](ADR-051-miner-keys.md)) does not help on a host that also holds the
  passphrase.
- A pod that dies before its first declaration sends its pay to a key that is gone; hence the refusal
  without an address, the retries, and the message.
- Pods behind one IP address reach the faucet's quota per source IP address, which the owner lowered on
  2026-10-08 ([ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md)); the message names that cause.
- On a remote RPC the pod believes the network's operator about the chain's state; the genesis check
  proves a genesis, not a network.
- Sourcing `join.sh` runs its top-level code, so a change there reaches the pod; the pod's tests source the
  real file rather than a copy.
- On Vast, an image's entrypoint may not run in the SSH or Jupyter launch modes; this is to be checked on a
  real pod, with an on-start script as the fallback.
- On HiveOS a flash drive below the installer's disk floor (`deploy/install.sh::MIN_DISK_GB`) is refused,
  and moving Docker's data root is out of scope. HiveOS runs the downloaded archive as root without a
  checksum of its own: integrity rests on HTTPS and on the published checksum.
- The miner image is built for linux/amd64 only (the `miner-image` job of `.github/workflows/release.yml`
  runs on an amd64 runner and names no other platform), and the cloud image, built on it (decision 2),
  inherits that. A pod on an arm64 host cannot run them, whatever its GPU.
- Pulling the image and the model is billed time; the documentation says so without quoting sizes.

## Version

The cloud image, its entrypoint and the HiveOS package are new artifacts that no deployed kit is behind.
The daemon's status file, the `lock` flag of the payout command and the update advice `join.sh` can print
for a pod change what an operator runs: they ship under the one `kit_version` bump shared by ADR-051 to
ADR-055, in one release whose number [ADR-050](ADR-050-release-versioning.md) derives. The cloud image is
pinned after that release, with the miner image. The season service's lock is a redeployment, not a bump.
`consensus_epoch` does not move.

## Pointers to add at merge

The code below is not in the published tree yet; each item is cited by file and symbol once it lands.

- `docker/Dockerfile.cloud`, the cloud entrypoint under `docker/`, and `docker/CLOUD_IMAGE`.
- The HiveOS package under `deploy/hiveos/` (manifest, configuration, run and statistics scripts) and its
  build script.
- The cloud and HiveOS documentation under `deploy/cloud/` and `deploy/hiveos/`.
- The `lock` flag in `services/final_season_server.py` and in the payout command of
  `services/final_season_miner.py`.
- The cloud image step and the package job in `.github/workflows/release.yml`.
