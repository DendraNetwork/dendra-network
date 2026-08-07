# ADR-009 — Anti-collusion: VRF assignment and slashing parameters

**Status:** Accepted
**Implementation:** Partially implemented. Committee assignment is VRF-drawn and stake-weighted
(`chain/x/jobs/keeper/committee.go`), and the drawing pool is frozen per job
([ADR-037](ADR-037-gel-du-vivier-de-tirage.md)). The "stake at least as large as the maximum gain
from cheating" bound is **not enforced on-chain**: no guard compares a job fee against the primary's
stake at open time.

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
