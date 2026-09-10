# ADR-018 — On-chain settlement v4 (cut split, miner vesting, BME buyback)

> **SUPERSEDED by [ADR-021](ADR-021-tokenomics-v5-native.md)**, which replaces the v4 monetary model
> with v5 (fixed supply, zero minting, Reserve emission). The settlement split described here is the
> v4 arrangement; the live settlement path follows
> [ADR-024](ADR-024-couche-economique-reelle-onchain.md). Kept for the record.

**Status:** Superseded by ADR-021
**Implementation:** Prototype only. The split, vesting tranches and `buyback_burn` were implemented in
the Python state machine. On the chain, `MinerVestBps` exists in
`chain/x/jobs/types/params.go` and is read by `msg_server_settle_job.go`, a handler that is now
**authority-gated and has no production caller**
([ADR-033](ADR-033-comite-readjudication-ancre.md)). **No `buyback_burn` exists on-chain.**

## Context

The state machine settled jobs in **v2**: burn 50 % of the protocol share, then split the rest 50/50
between validators and treasury. The adopted tokenomics became **v4**
([ADR-015](ADR-015-tokenomics.md)): a three-way cut split, **vesting** of the miner share, and
**BME by buy-and-burn** rather than by burning the cut. Settlement had to be aligned with v4 and with
the anti-Sybil gate ([ADR-017](ADR-017-anti-sybil-demande.md)).

## Decision

`settle_job` applies the v4 distribution, entirely in integers and deterministic:

**Miner share = 85 % of the fee**, of which **30 % vests** (locked in tranches, released after
`miner_vest_blocks` via `release_vesting`); the remainder is **immediately liquid**. This is the
anti-dump measure.

**Protocol share (cut = 15 %)** splits as:

| Beneficiary | Share of the cut | Share of the job |
|---|---|---|
| Validators (`reward_pool`) | 50 % | 7.5 % |
| **Team / founders** (`_team`) | 20 % | 3 % |
| Treasury | 30 % | 4.5 % |

**BME becomes `buyback_burn`**: the burn is **no longer taken from the cut**. The protocol
**buys back and burns** from the **treasury**, at a cadence and amount decided by governance. This
**decouples** value accrual (the burn) from **role compensation** (the cut split). `fee_burn_bps = 0`
in v4 (retained and governable: setting it above zero re-attaches a direct burn to the cut).

**NON-RECOVERABLE demand** (the anti-Sybil gate, ADR-017) = **burn + treasury + team** of a job — the
shares a client-to-miner loop cannot refund to itself. The validator share is excluded out of caution.
Self-dealing (client linked to the miner) does not count.

### Parameters (governable)

| Parameter | Default | Role |
|---|---|---|
| `protocol_fee_bps` | 1500 | protocol share (cut) = 15 % |
| `validator_reward_bps` | 5000 | 50 % of the cut to validators |
| `team_fee_bps` | 2000 | 20 % of the cut to the team |
| `miner_vest_bps` | 3000 | 30 % of the miner share locked |
| `miner_vest_blocks` | 200 | release delay of one tranche |
| `fee_burn_bps` | 0 | v4: BME through `buyback_burn`, not a cut burn |

### New primitives

- `release_vesting(miner)` and `locked_balance(miner)` — release of matured tranches.
- `buyback_burn(amount)` — buy-and-burn bounded by the treasury (BME).
- `add_work_emission` and `claim_work_subsidy` — the gated work subsidy (ADR-017).

## Consequences

- Chain and tokenomics **speak the same language** (miner 85 % with vesting, cut 50/20/30, BME
  buyback, demand gate on the non-recoverable part).
- The demonstration flow walks the **complete v4 economic loop**: job, settlement, gated subsidy,
  vesting release, BME.
- Tests updated to v4 amounts, with cases for vesting, team share and buyback.

## Honest limitations

- The macro tokenomics' **"BME at 30 % of fees"** is a **buyback target reached over time** (funded by
  the treasury and accumulated fees), **not a per-job burn**: `buyback_burn` is bounded by the
  available treasury. Cadence and amount are **governance** decisions.
- Vesting is modelled as **block-maturity tranches** (prototype); a real Cosmos module would use a
  continuous release schedule.

## Links

[ADR-015](ADR-015-tokenomics.md) · [ADR-017](ADR-017-anti-sybil-demande.md) ·
[ADR-013](ADR-013-mineur-exploite-un-node.md) · [ADR-024](ADR-024-couche-economique-reelle-onchain.md)
