# ADR-019 — Inference-to-chain bridge (an economy driven by confidential inference)

**Status:** Accepted
**Implementation:** Implemented in the prototype (`services/modea/chain_bridge.py`), which was
the rehearsal for the Go keeper API. On the live chain the same loop is served by `x/jobs` and
`x/emission`; the Python bridge is no longer the production path.

## Context

The Mode A prototype ([ADR-011](ADR-011-deux-modes-confidentialite.md)) ran **confidential inference**
(end-to-end encryption, locked memory, canaries) while writing its **commitments** to a local ledger —
but with **no economy**: no escrow, no payment, no real slashing. The state machine
([ADR-018](ADR-018-reglement-onchain-v4.md)) carried the economy — but **no inference**. The two had
to be **wired together** to obtain an end-to-end thread.

## Decision

A **bridge** advances **two complementary layers in lockstep** around a job:

| Layer | Role | Content |
|---|---|---|
| Ledger (ADR-011) | **Confidentiality** | commitments (hashes) only — **never** content |
| Chain (ADR-018) | **Economy** | escrow, 85 % payment with vesting, non-recoverable demand, gated subsidy, slashing |

**Job cycle:**

1. Optionally, a watermarked **canary** is committed on-chain and inserted into the prompt.
2. The **client encrypts** with an ephemeral key, producing a request commitment.
3. **`open_job`**: on-chain escrow — it sees **only commitments** (ADR-011).
4. **Confidential inference** on the miner side (locked memory, nothing logged).
5. **`settle_job`**: pays the miner (85 %, of which 30 % vests), records the **non-recoverable
   demand** (ADR-017), splits the cut (validators, team, treasury).
6. **Ledger record**: commitment of the result (proof that no content was stored).
7. The **client opens** the result and strips the marker.

Plus `onboard_miner` (on-chain bond), `claim_subsidy` (gated subsidy after the epoch closes), and
`report_leak` (a marker reappears, producing an **objective on-chain slash**, ADR-012).

## Consequences

- An **end-to-end thread demonstrated** by integration tests: confidential inference, on-chain
  payment, anti-Sybil subsidy, leak, slash — with an **automatic confidentiality check** verifying
  that the sensitive data appears nowhere on-chain or in the ledger.
- The bridge is **pure and in-process** (testable without a network); the coordinator's HTTP service
  can call the same primitives from its settlement endpoint.
- This is the **second lock** before Cosmos ([ADR-014](ADR-014-framework-l1-cosmos-souverain.md)): the
  keeper API (`open_job` / `settle_job` / `claim_work_subsidy`) is proven end to end, which makes the
  port from the Python state machine to Go `x/` modules mechanical. The remaining lock is determinism
  ([ADR-OPEN](ADR-OPEN-strategie-determinisme.md)).

## Honest limitations

- The bridge imports the chain package by relative path (prototype); in production, miner and chain
  are **separate processes** communicating through signed transactions.
- Ledger and chain are two in-memory objects here; on the real L1 the "commitment ledger" **is** the
  chain state (a single register).

## Links

[ADR-011](ADR-011-deux-modes-confidentialite.md) · [ADR-018](ADR-018-reglement-onchain-v4.md) ·
[ADR-017](ADR-017-anti-sybil-demande.md) · [ADR-012](ADR-012-sanctions-fuites.md)
