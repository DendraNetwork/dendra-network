# ADR-020 — On-chain verification tier (tiered S2)

> **Unresolved tension.** S2 assumes a committee **re-verifies** the output, and therefore sees the
> content — which **contradicts** the confidentiality of
> [ADR-011](ADR-011-deux-modes-confidentialite.md) for the private modes. Two possible resolutions:
> either **Mode A** gives up verification by re-execution (attestation, reputation, canary), or a
> **non-confidential public tier** coexists. As written, S2 applies to the non-private flow, and the
> embedding-to-content binding for the private modes is still to be designed.

**Status:** Accepted, scope to be narrowed (see box above). **Partly superseded** by
[ADR-025](ADR-025-verification-optimiste-echantillonnage.md) — re-verification survives but stops
being performed on every job by a k=3 committee — and by
[ADR-026](ADR-026-llm-juge-onchain.md), which replaces cosine with a language-model judge on the
audited sample.
**Implementation:** Superseded in place. There is no `x/verify` module in the chain; semantic
verification lives in `x/jobs` (`chain/x/jobs/keeper/msg_server_verify_semantic.go`). The
`JackpotReward` mechanism below is **not implemented**.

## Context

Determinism was settled as **tiered S2** ([ADR-OPEN](ADR-OPEN-strategie-determinisme.md)). LLM
inference is not globally byte-reproducible (0.95 intra-machine), but the non-determinism is
**targeted**: **structured output is byte-perfect**, while **free-form diverges late while preserving
meaning**. An on-chain module has to verify a job accordingly.

## Decision

An **optimistic plus challenge** verification tier, with **two modes** depending on the job category:

- **`hash`** (code, factual, structured — measured byte-stable): verification by **hash comparison**.
  A verifier committee (VRF-assigned, [ADR-009](ADR-009-anti-collusion-vrf.md)) re-executes and
  submits its `result_commit`; the **honest majority** is the canonical answer, and a miner who
  disagrees is **divergent**.
- **`semantic`** (reasoning, free-form): verifiers attest an **embedding cosine**
  ([ADR-008](ADR-008-comparaison-semantique.md)) between the miner's output and the reference. A
  median **at or above the threshold** (0.90) means the divergence **preserves meaning** (no fault);
  **below** the threshold means a wrong answer.

**Resolution** after the challenge window: proven divergence leads to a **slash** of the miner
([ADR-009](ADR-009-anti-collusion-vrf.md)) plus a **jackpot** for the honest verifiers
([ADR-007](ADR-007-anti-dilemme-verificateur.md)) — a protocol bounty that breaks the verifier's
dilemma and funds random re-verification. Otherwise the job is **confirmed**.

> Escalation: a `hash` dispute not settled by the majority escalates to the **bisection fraud proof**
> ([ADR-003](ADR-003-fraud-proof-bisection.md)); high-value jobs may require **S3 or zkML** (opt-in).
> The chain only ever sees **commitments and attested cosines**, never content
> ([ADR-011](ADR-011-deux-modes-confidentialite.md)).

### Mechanism (deterministic keeper)

`OpenVerification(jobID, mode, minerID, minerCommit)`, then `SubmitHashVote` or
`SubmitSemanticVote` within the window, then `Resolve` after it:

- `hash`: `canonical = majority(commits)`; `minerWrong = minerCommit != canonical`.
- `semantic`: `minerWrong = median(cosine) < threshold`.
- if `minerWrong`: `Slash(minerID, MinerSlashBps)` plus `payJackpot(honest)`.

Map iterations are **sorted** before any effect, so the result is deterministic.

### Parameters (governable)

| Parameter | Default | Role |
|---|---|---|
| `ChallengeWindowBlocks` | 50 | optimistic window before finalisation |
| `SemanticThresholdBps` | 9000 | cosine at or above 0.90 counts as meaning-preserving |
| `MinerSlashBps` | 5000 | 50 % of stake slashed on proven divergence |
| `JackpotReward` | 300 | bounty split among honest verifiers |

## Consequences

- The determinism verdict becomes operational: `x/jobs` handles the **economy** (escrow, payment,
  demand) and the verification path handles **correctness** (who is right, who is slashed).
- Verification is integrated with the real miner keeper: slashing hits the real stake.
- The verification path depends on the jobs keeper through a narrow `Stakes` interface (slash plus
  mint), which keeps the coupling clean.

## Honest limitations

- **Embeddings and cosines** are computed **off-chain** (oracle or committee) and **attested**; the
  chain only compares against a threshold. Collusion resistance rests on VRF assignment
  ([ADR-009](ADR-009-anti-collusion-vrf.md)) plus random re-verification
  ([ADR-007](ADR-007-anti-dilemme-verificateur.md)), both of which need hardening.
- **Bisection** ([ADR-003](ADR-003-fraud-proof-bisection.md)) is not implemented in the keeper; v1 is
  honest-majority, which is sufficient for the verifiable tier.

## Links

[ADR-OPEN](ADR-OPEN-strategie-determinisme.md) · [ADR-003](ADR-003-fraud-proof-bisection.md) ·
[ADR-005](ADR-005-architecture-2-tiers.md) · [ADR-007](ADR-007-anti-dilemme-verificateur.md) ·
[ADR-008](ADR-008-comparaison-semantique.md) · [ADR-009](ADR-009-anti-collusion-vrf.md) ·
[ADR-018](ADR-018-reglement-onchain-v4.md)
