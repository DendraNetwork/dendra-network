# ADR-017 — Anti-Sybil: subsidy gated on NON-RECOVERABLE demand

**Status:** Accepted
**Implementation:** Implemented. `work_gate_bps` and `demand_client_cap` are chain parameters
(`chain/x/jobs/types/params.go`, `chain/x/emission/types/params.go`); the work pool is capped
by measured demand in `chain/x/emission/keeper/epoch.go` and claimed through
`chain/x/jobs/keeper/msg_server_claim_subsidy.go`.

## Context

"Work" emission (inference, training, node operation) is **capped by a demand gate**: work is only
subsidised if **real demand** exists, otherwise a miner collects free inflation, which is farming. The
economic spike exposed the flaw:

> An attacker who is **both client and miner** opens jobs **to itself**. As a miner it recovers 85 %
> of its own fees, and the "demand" it manufactures **unlocks emission**. At a gate of **2×**,
> self-dealing returned **+94 %**.

The spike's emergency fix was to drop the gate to **0.20**, below the 0.30 break-even. Safe, but it
**starves genuine miners**. A **structural** mechanism is needed to raise the gate **without
reopening the hole** — the subject of this record.

## Decision

The subsidy is **gated on NON-RECOVERABLE demand**, not on gross revenue.

A job's **non-recoverable demand** is the share of fees that the client-to-miner loop **cannot refund
to itself**: **burn (BME) plus treasury share**. The validator share is **excluded out of caution**,
in case the attacker also validates. The miner share (85 %) is **recoverable** and therefore does not
count.

Three layers of defence:

1. **Excluding trivial self-dealing.** A job whose `client` is **linked** to the miner (equal to its
   `operator` or its `funder`) **does not count** towards demand.
2. **Economic gate on the non-recoverable part.** A miner's subsidy cap over an epoch is
   `work_gate × Σ non_recoverable_demand`. The subsidy is a **fraction of a value the attacker has
   genuinely lost**, so the margin stays negative as long as
   `work_gate < 1 / inference_share`.
3. **Per-distinct-client cap** (`demand_client_cap`). A single payer cannot unlock a
   disproportionate emission, so scaling the attack requires **funding N identities**, each paying a
   real non-recoverable amount — **linear cost, zero margin** (layer 2).

### Why this permits raising the gate

Per unit of fee `F`, with the attacker acting as client and miner but **not** validator:

- **Cost** = the protocol share lost = `protocol` (about 0.15·F), unrecoverable.
- **Gain** = subsidy captured as a miner = `gate × non_recoverable × inference_share`.

Gating on the **gross** amount (`non_recoverable = F`) puts break-even at
`gate = protocol / inference_share ≈ 0.30`. Gating on the **non-recoverable** part
(`≈ protocol·F`) makes the gain `gate × protocol·F × inference_share`, so:

```
break-even  gate* = 1 / inference_share ≈ 1 / 0.5 = 2.0
```

The safe gate therefore moves from **0.30 to about 2.0**. The retained value is
**work_gate = 1.5** (a margin below break-even): self-dealing is **net negative**, and **genuine
miners earn more** than they would at a gate of 0.20.

## On-chain mechanism (deterministic keeper)

- The counting **epoch** is `epoch_blocks` blocks long.
- At `settle_job`: compute `non_recoverable = burn + treasury`; if the client is **not linked** to the
  miner, add it to `demand[(epoch, miner)][client]`, **capped** at `demand_client_cap`.
- `work_subsidy_cap(miner, epoch) = work_gate_bps/10000 × Σ_client counted_demand`.
- `claim_work_subsidy(miner, epoch)`, after the epoch closes, pays
  `min(cap − already_paid, work_pool)` out of the **work emission pool**.

Self-dealing yields **zero** counted demand, hence **zero** subsidy — the attacker paid the protocol
share for nothing.

### Parameters (governable)

| Parameter | Default | Role |
|---|---|---|
| `work_gate_bps` | 15000 (1.5×) | cap = gate × non-recoverable demand |
| `demand_client_cap` | 100 | maximum demand counted per distinct client, per epoch, per miner |
| `epoch_blocks` | 100 | length of the counting epoch |

## Consequences

- The demand gate **can be raised safely** (0.20 to 1.5), making **mining more profitable** without
  reopening self-dealing. The macro simulator gates on the non-recoverable part accordingly.
- `settle_job` records demand; new flows `add_work_emission` and `claim_work_subsidy`.
- Governance: `work_gate`, `demand_client_cap` and `epoch_blocks` are adjustable by vote.

## Honest limitations

- Detecting a client-to-miner **link** is **heuristic**; distinct Sybil identities evade it. This is
  **not** the primary defence: the **real backstop is economic** (layer 2), which depends on no
  identity detection at all.
- The **validator share** is excluded from "non-recoverable" out of caution (attacker-as-validator).
  Proving validator/miner independence would raise the safe gate further.
- A **proof of user uniqueness** (personhood, staked reputation) stays **optional**: it would tighten
  `demand_client_cap`, but it is not required for safety.

## Links

[ADR-015](ADR-015-tokenomics.md) · [ADR-013](ADR-013-mineur-exploite-un-node.md) ·
[ADR-021](ADR-021-tokenomics-v5-native.md)
