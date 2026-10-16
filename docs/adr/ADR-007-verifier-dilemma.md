# ADR-007 — Countering the verifier's dilemma

> **The jackpot is withdrawn on chain (classified 2026-10-16).** The forced errors exist here to pay a
> jackpot to the verifiers who catch them, and a payment to jurors is what
> [ADR-038](ADR-038-juror-incentive.md) section 4 declines on chain, with its absence pinned by
> `chain/x/jobs/keeper/adr038_juror_incentive_test.go`. A juror reward on chain first needs the
> prerequisites of ADR-038 section 5. The season's off-chain reward to active judges is
> [ADR-060](ADR-060-judge-presence-reward-amends-adr-038.md).

**Status:** Accepted for mandatory random re-verification, in force as the VRF-sampled audit of
[ADR-025](ADR-025-optimistic-verification-by-sampling.md) at the rate `audit_sample_bps` (`dendrad query
jobs params -o json`). The forced errors with a jackpot are withdrawn on chain (banner above).
**Implementation:** Partial. The sampled audit is implemented
(`chain/x/jobs/keeper/audit_sampling.go::runOptimisticAudit`); the chain code has no forced-error or
jackpot path.

## Context

The **verifier's dilemma**: if the network is mostly honest, verifying almost never pays, so nobody
verifies, so cheating goes unpunished in practice. The fraud proof
([ADR-003](ADR-003-fraud-proof-bisection.md)) is useless if nobody has an incentive to challenge.

## Decision

Two combined mechanisms, the standard answers to the verifier's dilemma:

- **Forced errors with a jackpot**: the protocol occasionally injects deliberate errors whose
  detection pays a **large prize**, guaranteeing a positive expected return to verification.
- **Mandatory, paid random re-verification**: some jobs are re-drawn for audit, with compensation.

## Consequences

- Makes verification economically rational even in an honest network.
- Costs to be calibrated (jackpot frequency, prize size, audit rate) alongside the other economic
  parameters.
