# ADR-013 — Every miner runs a full/relay node, not a validator

**Status:** Accepted. Stays decoupled from [ADR-001](ADR-001-consensus-tendermint.md). In force as the kit's
default only: `deploy/join.sh` starts the miner's own node unless `--remote-rpc` is given, and the
rented-pod image of [ADR-053](ADR-053-cloud-and-hiveos-paths.md) runs none. The chain does not check that a
registered miner runs a node; a check would be a new consensus rule, and none is decided. The relay and
coordination mesh and the co-location of Mode B groups are not built; the latter follows Mode B of
[ADR-011](ADR-011-two-confidentiality-modes.md). Classified on 2026-10-16.
**Implementation:** Off chain, partial: `deploy/join.sh::start_local_node` starts the node (`OWN_NODE`), and
`--remote-rpc` turns it off. No on-chain obligation.

## Context

Product objective: a resilient, decentralised network that **reinforces itself as it grows** (every new
miner should **add infrastructure** as well as consume jobs), and avoid depending on a handful of
central gateways or relays.

The tempting shortcut, "every miner is a validator", is **impossible and undesirable**: the BFT
validator set is **bounded** (CometBFT around 100-150; O(n²) communication). Thousands of miners
cannot all produce blocks. Mining and block production are therefore **never** coupled.

## Decision

The **miner client embeds and requires a light full/relay node**. To **receive jobs and be paid**,
every miner must:

1. Run a **light full node** (state-sync, pruned, not archive) that stays **synchronised**: it reads
   **job assignments**, posts **commitments**, and **verifies state without a trusted third party**.
2. Take part in the **relay and coordination mesh**: P2P gossip, **discovery**, **caching of public
   data** (model manifests and weights), **co-location of Mode B groups** (low RTT).
3. Answer **liveness and relay probes**; **reputation** and **job routing** depend on **node health**.

This node is **not a validator**: block production remains an **opt-in subset**, sufficiently staked,
inside the bounded CometBFT set.

## Consequences

- **Self-reinforcing network effect**: every miner adds **inference capacity plus infrastructure**
  (data availability, relaying, coordination). The network **grows with its miners**.
- **Partially resolves the Mode B RTT bottleneck**: more nodes means a **denser regional mesh**,
  making it easier to find **three co-located low-RTT miners**.
- **Decentralisation and anti-chokepoint**: consistent with the permissionless gateway
  ([ADR-010](ADR-010-permissionless-gateway.md)).
- **Cost and barrier to entry**: a node needs disk, bandwidth and uptime. **Mitigations**: a **light**
  node (state-sync, pruned), a **one-click bundle** (miner client plus node), and modest hardware
  requirements separate from the GPU requirements for inference.
- **Uptime**: required **while actively mining**; uptime feeds **reputation**, but a miner that shuts
  down is **not** slashed the way a validator would be (intermittent consumer hardware is not a
  consensus fault).
- **Sybil**: a node is not sufficient to prevent it, so a **stake or bond is always required**
  ([ADR-012](ADR-012-leak-sanctions.md), [ADR-009](ADR-009-anti-collusion-vrf.md)).
- **Decoupling maintained** ([ADR-001](ADR-001-consensus-tendermint.md)): a miner node is not a
  validator, and consensus security stays independent of miner count or power.

## Three node types

| Node | Role | Who | Mandatory? |
|---|---|---|---|
| **Validator** | produces and orders blocks (PoS) | bounded opt-in set, high stake | no (opt-in) |
| **Miner node (full/relay)** | chain sync, gossip, relay, coordination, miner client | **every miner** | **yes** |
| **Dedicated relay** | relay and cache without mining | optional, separately compensated | no |

## Open items

- Minimum hardware requirements for the miner node (pruned disk, bandwidth, target uptime).
- A mechanism to prove "I really do run a synced relaying node" (probes, service challenges).
- Reputation weighting: node uptime versus inference quality.

## Links

[ADR-001](ADR-001-consensus-tendermint.md) · [ADR-010](ADR-010-permissionless-gateway.md) ·
[ADR-009](ADR-009-anti-collusion-vrf.md) · [ADR-011](ADR-011-two-confidentiality-modes.md)
