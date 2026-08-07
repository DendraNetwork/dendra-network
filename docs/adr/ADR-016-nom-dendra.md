# ADR-016 — Project name: Dendra (DNDR)

**Status:** Accepted
**Implementation:** Implemented — the chain, the token denomination (`udndr`) and the address prefix
(`dendra`) all carry the name.

## Context

The L1 and the token carried a technical **codename** ("DEAI L1" / "DEAI"). A definitive **brand
name** was needed: evocative of AI, decentralised networking and privacy; distinctive; and
**uncrowded** in the crypto space.

## Decision

- **Blockchain: Dendra.**
- **Token: DNDR.**

### Meaning and branding

**Dendra** comes from **dendrite** (Greek *dendron*, "tree") — the **input branches of a neuron**. The
metaphor works twice:

- **AI**: the neural network, the intelligence.
- **Decentralised network**: a **branching structure that spreads** organically, like the mesh of
  miners and nodes as it grows ([ADR-013](ADR-013-mineur-exploite-un-node.md)).

Possible tagline: *"the network that branches"* — confidential AI on a spreading neural mesh.

### Why not the other candidates (collisions checked)

- **Obscura / Obscuro**: saturated in privacy-crypto (Obscura Privacy Layer, Obscuro to Ten, and
  others).
- **Inferon / IFR**: too close to **Inferium (IFR)**, an existing AI inference network — a head-on
  collision.
- Roots **infer-**, **tensor**, **obscur-**: too crowded (Inference Labs, Bittensor, Cortensor).
- **Dendra**: no major crypto project under that name. The ticker **DEN was already taken**, hence
  **DNDR**.

> Final clearance (`.xyz` / `.io` / `.ai` domains, social handles, trademark filing) to be completed
> before a public announcement; not a blocker for development.

## Consequences

- Progressive rename of the `DEAI` codename to `Dendra` across documentation and code.
- Tokenomics ticker: `DEAI` becomes **`DNDR`**.

## Links

[ADR-014](ADR-014-framework-l1-cosmos-souverain.md) · [ADR-015](ADR-015-tokenomics.md)
