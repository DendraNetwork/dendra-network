# ADR-012 — Sanctions for leaks and confidentiality violations

**Status:** Accepted
**Implementation:** Decided, not implemented. Miner bonds are real escrowed coins and slashing is
real (`chain/x/jobs/keeper/`), but **no canary mechanism, no attestation check and no leak-report
path exist on-chain**. The graded scale below describes an intended design, not shipped behaviour.

## Context

Mode A rests partly on **deterrence** ([ADR-011](ADR-011-deux-modes-confidentialite.md)). Without a credible punishment system, deterrence is empty.
But — the same lesson as for computation fraud
([ADR-002](ADR-002-abandon-levenshtein.md), [ADR-003](ADR-003-fraud-proof-bisection.md)) — slashing
must rest on **objective proof**, otherwise honest miners get punished.

## Decision

Introduce **graded slashing for confidentiality violations**, resting on **objective evidence** and
funded by a mandatory **bonded stake** from every miner.

1. **Bonded stake**: every miner (Mode A and Mode B) posts a stake **at least equal to the exposed
   value**. No stake, no jobs.

2. **Objective proof of a leak** (the core):
   - **Traceable canaries**: watermarked prompts and responses carrying a unique marker are injected
     at random into a fraction of jobs. The canary-to-miner link is committed on-chain. If a canary
     **reappears** (public dataset, another model's output, a marketplace, a reported leak), anyone
     can **prove on-chain** which miner served that job, triggering automatic slashing. This is the
     analogue of the forced errors with a jackpot in
     [ADR-007](ADR-007-anti-dilemme-verificateur.md).
   - **Attestation failure**: miner client hash different from the registered sealed binary is an
     **automatic** fault.
   - **Report with cryptographic proof** (signed job receipt plus a match against the leaked
     content).

3. **Graded scale**:
   - **Technical or minor** (expired attestation, missed audit): small slash plus **temporary
     suspension**.
   - **Proven leak**: **major slash** (a large fraction up to the whole stake).
   - **Repeat offence or mass exfiltration**: **full slash plus permanent ban** (denylist of operator
     keys and hardware fingerprints).

4. **Distribution of the slashed stake**: part **burned**, part to the **whistleblower bounty**, part
   to a **victim compensation pool**.

5. **Long-tail liability**: a leak may surface **long after** the job. Therefore **unbonding strictly
   longer than the claim window** (anti slash-and-run,
   [ADR-009](ADR-009-anti-collusion-vrf.md)), and a **long claim window** during which part of the
   stake — or an insurance escrow — stays slashable even after unbonding has begun.

6. **Abuse-resistance of the punishment system**:
   - False accusations barred: **cryptographic proof required** (signed receipt plus canary); a
     **complainant bond** against spam; **slashing of the false accuser**.
   - Anti-Sybil: stake plus identity and operator diversity.

7. **Reputation**: a cumulative non-financial score influencing **job routing** and the **required
   stake**.

8. **Arbitration**: automatic when the evidence is objective (attestation, on-chain verified canary);
   **committee or governance** for contested cases, with the accused given a right of reply.

## Consequences

- Mode A deterrence becomes **credible and quantified** (stake plus scale).
- Heavy dependency on the **canary** mechanism: its quality determines real detectability.
- Parameters to calibrate (stake size, canary rate, windows, distribution), alongside the global
  economic parameters.
- Applies to **Mode B** as well: proven collusion among MPC miners is a slashable fault of the same
  nature.

## Links

[ADR-011](ADR-011-deux-modes-confidentialite.md) · [ADR-007](ADR-007-anti-dilemme-verificateur.md) ·
[ADR-009](ADR-009-anti-collusion-vrf.md)
