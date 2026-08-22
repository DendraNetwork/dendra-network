# ADR-007 — Countering the verifier's dilemma

**Status:** Accepted
**Implementation:** Partially implemented, and by a different mechanism than the one recorded here.
Mandatory random re-verification exists as the VRF-sampled audit of
[ADR-025](ADR-025-verification-optimiste-echantillonnage.md). **Forced errors with a jackpot are not
implemented** — no jackpot reward path exists in the chain code.

## Context

The **verifier's dilemma**: if the network is mostly honest, verifying almost never pays, so nobody
verifies, so cheating goes unpunished in practice. The fraud proof
([ADR-003](ADR-003-fraud-proof-bisection.md)) is useless if nobody has an incentive to challenge.

## Decision

Two combined mechanisms (the TrueBit model):

- **Forced errors with a jackpot**: the protocol occasionally injects deliberate errors whose
  detection pays a **large prize**, guaranteeing a positive expected return to verification.
- **Mandatory, paid random re-verification**: some jobs are re-drawn for audit, with compensation.

## Consequences

- Makes verification economically rational even in an honest network.
- Costs to be calibrated (jackpot frequency, prize size, audit rate) alongside the other economic
  parameters.
