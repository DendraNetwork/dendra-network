<!-- DOWNTIME-POLICY: transition record — this file cites the before AND the after on purpose, so its
     literals are not claims about what the chain serves today. The marker on the first line is what
     exempts the page from the freshness check that otherwise reads a downtime literal as a claim about
     the current parameters, and that check would be right to refuse them here. The exemption is
     whole-file and deliberate: everything below is about the change. Read the values the chain serves
     today from the chain itself, never from this page. -->

# ADR-040 — Consensus downtime: widen the signing window, do not soften the jail

**Status:** Decided by the project owner. Enacted on-chain as **governance proposal 1**
(`/cosmos.slashing.v1beta1.MsgUpdateParams`), in its voting period until **2026-08-10T18:52:03Z**.
**Implementation:** the three genesis paths of this repository already write these values; the proposal
aligns the **running** chain with them. **Not consensus-breaking** — a parameter change, not a
state-machine change.

> **This record exists because the policy changed before it was written down.** No ADR covered consensus
> downtime: `docs/adr/` contained no occurrence of `signed_blocks_window`, `downtime` or `jail`, and
> [ADR-009](ADR-009-anti-collusion-vrf.md) carries "slashing parameters" in its title while deciding
> something else entirely — the **miner's bond** against slash-and-run. Two neighbouring subjects, one
> vocabulary, no decision trace for the one that jails validators.

## Decision

| parameter | before | after | moved |
|---|---|---|---|
| `signed_blocks_window` | 100 | **10000** | yes |
| `min_signed_per_window` | 0.5 | **0.85** | yes |
| `slash_fraction_downtime` | 0.01 | **0.0005** | yes |
| `downtime_jail_duration` | 600s | 600s | no |
| `slash_fraction_double_sign` | 0.05 | 0.05 | **no — deliberately** |

## Why

**State the threshold in the unit the chain counts.** `x/slashing/keeper/infractions.go` computes
`maxMissed := signedBlocksWindow - MinSignedPerWindow()` and jails when
`MissedBlocksCounter > maxMissed`; `x/slashing/keeper/params.go` makes `MinSignedPerWindow()` the
**product** `min_signed_per_window × signed_blocks_window`, rounded. So `maxMissed` goes from
`100 − 50 = 50` to `10000 − 8500 = 1500`: the jail moves from the **51st** missed block to the
**1501st**. At the block interval observed on this chain, about **5.03 s**, that is roughly 4 min 17 s
of continuous absence today against roughly **2 h 06** afterwards. The interval is named in the same
sentence on purpose: the consensus does not fix it, it emerges from `timeout_commit` and propagation.

**The jail is the deterrent; the burn is the signal.** Removal from the consensus set until a manual
unjail is what actually costs an operator — it stops rewards and requires a human to notice. Lowering
the burn 20-fold does not weaken that; it stops a home connection from paying a validator-grade fine
for a router reboot. Two non-datacentre validators of this network had already been jailed and slashed
under the old figures, which is the evidence that the setting, not the operators, was wrong.

**Double-signing is untouched, and that is the whole shape of the decision.** Downtime is a failure to
participate; equivocation is an attack on safety. Softening both together would have said they are the
same fault. They are not, and 5% stays.

**Why 0.85 rather than keeping 0.5 at a wider window.** At `0.5 / 10000`, `maxMissed` would be 5000 —
about 7 hours — which stops distinguishing an outage from an abandonment. 0.85 buys a tolerance that
covers a reboot, a power cut or an ISP incident without covering a node that has simply been left off.

## Two paths, one policy — and two encodings for the same value

A new network never receives the proposal; it receives the genesis. The values above are therefore held
identical in `docker/entrypoint-chain.sh:415-419`, `chain/config.yml:67-73` and
`chain/config.optimistic.yml:89-95`, so a fresh chain and this one converge rather than drift.

⚠️ **The same number is written two different ways, and mixing them fails in opposite directions.** In a
genesis file a `LegacyDec` is a **decimal string** (`"0.850000000000000000"`); in a proposal it is a
proto **bytes** field, so it travels as base64 of the 10^18-scaled ASCII (`ODUwMDAwMDAwMDAwMDAwMDAw`).
The genesis path builds its strings by integer arithmetic (`_dec()`, `docker/entrypoint-chain.sh:474`)
precisely to avoid a float artefact, and rejects a decimal pasted into a `DENDRA_SLASH_*` basis-point
variable with a fatal error — so that inversion fails closed. The proposal path has no such guard: a
decimal string submitted there is refused by the SDK with `invalid value for bytes field`.

## Consequence operators must be told, because it is not in any doc

`AfterValidatorBonded` (`x/slashing/keeper/hooks.go`) resets `signingInfo.StartHeight` to the current
height, and the jail condition requires `height > StartHeight + signedBlocksWindow`. **A validator is
therefore immune to a downtime jail for one full window after it bonds or unjails** — about 8 minutes
under the old window, close to 14 hours under the new one. On jail, the counter and the missed-block
bitmap are cleared, so a re-bonded validator always starts from a clean slate.

## What this record does NOT decide

It does not state any tolerance as a duration without naming the interval assumption alongside it, and
it does not make the block interval a governed quantity — it is not one. Any surface that quotes a
number here must derive it from `dendrad query slashing params`, as `deploy/validator_health.sh` does,
rather than copy it: these five values are governable and every copy of them is true only until the
next vote.
