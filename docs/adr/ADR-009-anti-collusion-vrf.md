# ADR-009 — Anti-collusion: VRF assignment and slashing parameters

**Status:** Accepted, in force in forms other than the ones written below. Committees are drawn by VRF over a pool
frozen per job ([ADR-037](ADR-037-drawing-pool-freeze.md)). The work draw weighs stake up to a ceiling of
`assignment_stake_cap_multiple` x `min_stake` ([ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md));
the audit jury keeps raw stake weighting. The
stake-versus-gain bound is the expected-value guard of
[ADR-025](ADR-025-optimistic-verification-by-sampling.md) section 2.5, checked when a job opens, against
`min_stake`, in optimistic mode. Slash-and-run is closed by holding the bond while the miner carries an
obligation. The values of `min_stake`, `audit_sample_bps` and `slash_leak_bps` at the mainnet genesis are
among the genesis parameters [ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md) leaves open.
Classified on 2026-10-16.
**Implementation:** Implemented. `chain/x/jobs/keeper/committee.go::deriveCommitteeOrdered` draws the
work committee through `selectCommitteeOrdered` over `capAssignmentWeights`;
`chain/x/jobs/keeper/audit_committee.go::drawAuditCommittee` draws the audit jury. `chain/x/jobs/keeper/msg_server_open_job.go::OpenJob` refuses a fee unless `audit_sample_bps`
x `slash_leak_bps` x `min_stake` exceeds (10000 - `audit_sample_bps`) x 10000 x fee, when
`verification_mode` is 1. `chain/x/jobs/keeper/msg_server_miner.go::DeleteMiner` refuses, and
`chain/x/jobs/keeper/miner_vitality.go::pruneInactiveMiners` skips, a miner for which
`chain/x/jobs/keeper/miner_vitality.go::hasOpenObligation` holds. Read the parameters with `dendrad
query jobs params -o json`. Corrected on 2026-10-16: before that date this line said that no guard compared a job fee with a
stake.

## Context

Two risks: **collusion or cartel** (a miner picks its own verifiers, or peers coordinate) and
**slash-and-run** (cheat, then withdraw the stake before the sanction lands).

## Decision

- **Jobs and verifiers are assigned by VRF** (verifiable random function), so nobody chooses who
  verifies what.
- **Unbonding strictly longer than the challenge window**, so a stake cannot be withdrawn before the
  period in which it can be slashed has elapsed.
- **Stake at least the maximum gain from cheating**, so cheating is never profitable in expectation.

## Consequences

- Closes both the cartel and the slash-and-run vectors.
- Parameters (stake size, unbonding duration, window) remain to be calibrated.
- The VRF must be fed by an on-chain randomness source that cannot be manipulated.
