# ADR-052 — Installation: a state-sync trust point anchored in the clone, and a published node image applied to a fresh volume only

**Status:** Accepted by the project owner, 2026-10-08 — implementation in progress. Amended the same day by
an owner decision: `network-info.txt` is also served over HTTPS (*Amendment*, below).
**Implementation:** Decided, not implemented in the published kit; the work is in progress. The kit side
ships under the single `kit_version` bump shared by ADR-051 to ADR-055; the node image exists from the
release that follows it. Not consensus-breaking: no chain code moves, and snapshots and trust points are
node configuration.

## Context

- A joiner's own node starts from the genesis and replays every block, unless the configuration it loads
  carries a state-sync trust point: `STATESYNC_RPC`, `STATESYNC_TRUST_HEIGHT` and `STATESYNC_TRUST_HASH`,
  applied by `docker/node-join.sh` when the node is initialised and accepted from the network's
  `network-info.txt` by `deploy/join.sh::_load_config_url`. That file arrives over plain HTTP. The genesis
  is pinned in the clone for that very reason (`docker/GENESIS_SHA256`, checked by
  `deploy/join.sh::verify_genesis_info`); a trust hash served next to it lets whoever sits on the path
  choose the state the node starts from, around the pin.
- A node restored from a snapshot holds no history before it. Tools that rank from the transaction index
  refuse such a node (`services/final_season_chain.py::require_index_from`).
- The node image is built from the clone on every machine (`docker/Dockerfile.node`). Its header states that
  the image's binary is byte-for-byte the published release binary for the same tag and platform. The
  release workflow (`.github/workflows/release.yml`) builds the `dendrad` binaries in its `build` job and
  the miner image in its `miner-image` job, and no node image (what the workflow gains with this record's
  code is in [ADR-050](ADR-050-release-versioning.md), *Amendment*).
- The miner image already follows a pin model (`docker/MINER_IMAGE`, `deploy/join.sh::miner_image_pin`,
  `deploy/join.sh::miner_image_platform`): read from the clone, never from network-info; a digest in the
  official namespace, never a tag; started only on the engine's own platform; held only for the gates its
  `built-for` line names.
- The recreation guards of `deploy/join.sh::start_local_node` and of `deploy/bond_validator.sh` compare
  the running image with the image named by the shell's `DENDRA_NODE_IMAGE` or its default tag, not with
  the image compose resolves from the kit's `.env`.

## Decisions

1. **The trust anchor lives in the clone.** A new file, `docker/STATESYNC_RPC`, lists TLS RPC endpoints of
   the network, one per line, and nothing else: no height, no hash. It arrives with the repository over
   HTTPS git, like `docker/GENESIS_SHA256`.
2. **The trust point is derived at each join, never published.** `join.sh` reads the head over TLS from
   the listed endpoints, checks that each one serves the expected chain id, takes a height a named lag
   below the head, reads that block from each endpoint with the kit's JSON reader (never a text match),
   and requires the answers to agree. Three outcomes: agreement, and the node starts by state sync from
   that point; unreachable, and the node replays, which the join says; disagreement, and the join refuses.
   Nothing expires and nothing needs a timer.
3. **`STATESYNC_*` served by network-info are ignored**, and the join says so, as it already does for a
   miner image named there.
4. **One TLS endpoint is one witness: the TLS identity of the network's operator.** With a single endpoint
   the anchor reduces to that identity. It is accepted for the testnet and written in the kit's
   documentation. An independent witness needs a second TLS RPC, run by another validator and listed in the
   same file.
5. **State sync is the default for a miner's own node; a validator replays.** `--replay` refuses state
   sync. That a node restored by state sync extends its votes and proposes blocks is not measured; until a
   throwaway node shows it, `--validator` replays by default.
6. **The sync wait reads a restore as progress.** `deploy/join.sh::wait_for_sync` counts the snapshot
   restore lines of the node's log as progress, and `deploy/join.sh::sync_failure_report` names "no
   snapshot offered" and the `--replay` escape when the height stays at zero without one.
7. **A published node image for amd64 and arm64, built natively.** The release workflow gains a
   `node-image` job: one native build per architecture, arm64 on ARM runners, each pushed by digest, then
   merged into one manifest list. `docker/Dockerfile.node` stays the source of the image.
8. **A byte-equality gate.** The job extracts `dendrad` from each platform's image and compares its SHA-256
   with the binary the `build` job publishes for the same tag and platform; a difference stops the release.
   The header's statement becomes a measurement. A red here is a reproducibility finding, never a reason to
   drop the gate.
9. **`docker/NODE_IMAGE`, with the discipline of `docker/MINER_IMAGE`.** Read from the clone only; a
   digest, never a tag; a `built-for` line; empty when a release is tagged and before either gate moves;
   a `DENDRA_NODE_IMAGE` served by network-info is ignored. The tagging tool refuses a non-empty pin, as it
   does for the miner image ([ADR-050](ADR-050-release-versioning.md), decision 8).
10. **The pin applies to a fresh volume only.** When the node's data volume is new and the epoch in the
    `built-for` line equals `docker/CONSENSUS_EPOCH`: pull, check that the image is present, check its
    platform, start without building, and record the reference in the node kit's `.env`. Otherwise build
    from the clone and say why. An existing node keeps its binary: changing the binary of a running
    consensus node is an upgrade, a separate gesture.
11. **Compose resolves the image.** Both recreation guards read the image compose resolves for the kit
    (`docker compose config --images`), not the shell's variable; otherwise the pin breaks a restart and a
    bond.
12. **The network side comes first, without a bump.** Before the kit changes, the network's node is checked
    for snapshots (`dendrad snapshots list`), a throwaway node is synced by state sync to measure its
    catch-up and its votes, and the publication of `network-info.txt` gets a timer that keeps the
    environment of its first run. Kits already deployed benefit from it at once.

## Set aside

- **Building the node image from the downloaded release binary:** `dendra-vrf` is not a release asset, the
  assets are attached only by the `publish` job after the images, and the build would depend on the
  network. The image keeps its Dockerfile, and the gate proves the equality.
- **A bare binary mounted into a pinned base image:** two checksums to pin (`dendrad` and `dendra-vrf`)
  and a second distribution model next to the miner's.
- **Deriving the TLS host from `DENDRA_NODE`:** that value arrives over HTTP; an attacker would name its
  own domain, with a valid certificate.
- **A static trust point, published alone:** it expires with the trust period, needs a timer, and stays
  unauthenticated. It remains a stopgap for deployed kits (decision 12), not the target.
- **Applying the pin to an existing node:** it would change a running consensus binary, which the
  recreation guard rightly refuses.
- **QEMU emulation for arm64, or amd64 only:** an emulated Go build is slow, and amd64 only leaves ARM
  joiners building from the clone. Both stay fallbacks if ARM runners are unavailable.

## Consequences

- The node image exists from the next release on. Until the republication that pins the node and miner
  images, every joiner builds both from the clone, which takes longer than a pulled miner image does. One
  `kit_version` bump and one release for ADR-051 to ADR-055 make that window happen once.
- The container package of the node image must be public, or every pull fails and falls back to the
  build.
- Compose cannot tag a reference by digest: the documentation no longer prescribes `up -d --build` for a
  pinned node, and the kit never builds with a digest in the environment.
- Whether CometBFT's light-client provider accepts an HTTPS endpoint with a path, behind a reverse proxy,
  is an assumption; the fallback is a dedicated TLS route without a path.
- A node restored by state sync has no history. Ranking and evidence tools keep reading a node with the
  full history, which `require_index_from` already enforces.
- The equality gate can turn red on a toolchain difference between the image build and the `build` job;
  that is the gate doing its work.

## Version

Decisions 1 to 11 change what an operator runs (`join.sh`, `bond_validator.sh`, two new anchor files):
they ship under the one `kit_version` bump shared by ADR-051 to ADR-055, in one release whose number
[ADR-050](ADR-050-release-versioning.md) derives. That bump empties `docker/MINER_IMAGE` and creates
`docker/NODE_IMAGE` empty; both are pinned by the republication that follows the release.
`consensus_epoch` does not move. Decision 12 is network-side.

## Amendment, 2026-10-08: `network-info.txt` over HTTPS

The owner decided on 2026-10-08 that `network-info.txt` is served over HTTPS as well as over plain HTTP;
the TLS route is in `docker/Caddyfile`. Fetched over HTTPS, the file is authenticated as coming from the
network operator's host, which closes, for a joiner who uses that address, the path attack this record's
context describes. It changes none of the decisions above: the trust anchor stays in the clone (decisions
1 to 3), the node and miner image pins are still read from the clone only (decision 9), and one TLS
identity is still one witness (decision 4): HTTPS on the operator's host authenticates the operator, not
the network.

## Pointers to add at merge

The code below is not in the published tree yet; each item is cited by file and symbol once it lands.

- `docker/STATESYNC_RPC` and `docker/NODE_IMAGE`.
- The trust-point derivation and the `--replay` flag in `deploy/join.sh`, and the state-sync branch of the
  sync wait and of its failure report.
- The image pin and platform check generalised to the node in `deploy/join.sh`, and the compose
  resolution in the recreation guards of `deploy/join.sh` and `deploy/bond_validator.sh`.
- The `node-image` job and its equality step in `.github/workflows/release.yml`.
- The tagging tool's refusal of a non-empty node pin, and the publication's pin check extended to
  `docker/NODE_IMAGE`.
