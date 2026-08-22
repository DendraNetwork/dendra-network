# ADR-041 — A validator above two thirds silences every other validator

**Status:** Decided by the project owner. Enacted on-chain on **2026-08-11** by two delegations
(`MsgDelegate`, executed at heights **171109** and **171112**), moving the incumbent from **99.999 %**
to **62.50 %** of bonded power. **Not consensus-breaking** — stake moved, no state-machine change, no
parameter change.
> ⚠️ **That split no longer holds, and the record is kept as decided.** Re-measured **2026-08-21**: the second
> validator has been lost, so a single validator carries **100 %** of the voting power and the VRF contributor
> count is back to **1** against a floor of 2. The decision itself is unchanged — the target is now **two**
> partners rather than one, so that losing either still leaves a way back.

> ⚠️ **The chain this record describes no longer exists.** It was destroyed and relaunched on a fresh
> genesis on **2026-08-16**, which erased every delegation and left the network on a single validator with
> the seed back below its contributor floor. The decision was **re-enacted the same day**: a second
> validator was bonded at **600 000 DNDR** against the incumbent's 1 000 000, giving **62.50 / 37.50** and
> `contributors = 2` again — but with **two** validators where this record asks for three, so losing
> either one now stops the chain. The percentages below describe the chain of 2026-08-11; the ratio
> happens to match, the third partner does not exist yet.

> **This record exists because the defect was invisible from every indicator the chain publishes.** Two
> validators ran for days reporting perfect health — synced, never jailed, never absent, zero errors —
> while contributing nothing to consensus and nothing to the committee seed. Eight hypotheses were
> tested and eliminated before the real one was even considered, because the real one is not a fault.

## Decision

**No validator may hold two thirds or more of bonded power on a network that expects other validators
to participate.** On this chain the incumbent held 99.999 %; power was redistributed so that it holds
62.50 % and two partners hold 18.75 % each.

**The redistribution goes to TWO partners, not one.** With a single partner at 37.5 %, that partner's
failure stops the chain — and **a stopped chain cannot be repaired by transaction**, since undoing a
delegation requires a block. With two partners at 18.75 %, the incumbent plus *either one* reaches
81.25 %: a single failure leaves the chain producing, so the reverse gesture stays available. That
property, not the percentage, is what the split buys.

## What was measured

One continuous window of 210 blocks, cut at the height where the second delegation executed. Same
binary, same machines, same peers, same minute — the only variable that moved is the power split.

| | before (126 blocks, incumbent 99.999 %) | after (84 blocks, incumbent 62.50 %) |
|---|---|---|
| incumbent, `Commit` | 100.0 % | 100.0 % |
| validator A, `Commit` | **21.4 %** (99 `nil`) | **98.8 %** (1 `nil`) |
| validator B, `Commit` | **10.3 %** (113 `nil`) | **96.4 %** (3 `nil`) |
| `Absent`, any validator | **0** | **0** |
| commits at `round 0` | 126 / 126 | 84 / 84 |
| seed contributors = 3 | never observed | ~97 % of heights |
| block interval | 5.02 s | 5.23 s |

## Why a supermajority holder silences the others

A precommit carries a block only if the validator **has** that block. The sequence, with one holder
above two thirds:

1. The holder proposes, then prevotes. Its prevote **alone** is a supermajority, so the polka exists.
2. A vote message is a few hundred bytes; a block travels as gossiped parts. The prevote therefore
   reaches the other validators **before the block does**.
3. They reach the precommit step holding a polka for a block they have not finished receiving. A
   validator cannot sign what it does not have, so it precommits `nil`.
4. The holder commits on its own precommit. The round closes at `round 0`, on time, every time.

Nothing here is a fault, which is why nothing reports one. Remove the holder's ability to conclude
alone and the polka can no longer form ahead of the block: the measured rates go from 21.4 % / 10.3 %
to 98.8 % / 96.4 %, in a single block.

## Why no indicator could show it

**`nil` is not `Absent`, and the slashing module only counts `Absent`.** `x/slashing/keeper/infractions.go`
computes `missed := signed == comet.BlockIDFlagAbsent`; CometBFT's `BlockIDFlag` is `1 = Absent`,
`2 = Commit`, `3 = Nil`. A validator precommitting `nil` on every block is **present**: it signs the
round, its vote is included in the commit, `missed_blocks_counter` never moves, it is never jailed, and
`status` stays `BONDED`. Measured here: **zero `Absent` across 630 precommits** from two validators that
were contributing almost nothing.

An operator sees a healthy node because the node *is* healthy. The failure is not in any node.

## What it unblocks, and what it does not

**It unblocks the decentralized VRF seed.** A vote extension rides only on a `Commit` precommit, so a
`nil` takes the validator's VRF contribution with it. `committee_min_vrf_contributors` (2) was
therefore unreachable four heights out of five, and every draw fell back to the legacy path with
anti-grinding inactive. Measured: contributor count 1 on 82.1 % of heights before, 3 on ~97 % after.

The two seed bars now close together rather than fight: `MinVrfContributorPowerBps` = 6667
(`x/jobs/keeper/decentralized_seed.go`) requires contributors to weigh at least two thirds of the
commit, and below a supermajority **a block cannot be committed at all** without two thirds of `Commit`
precommits. Every committed block therefore carries enough extensions by construction.

**It does not unblock miner payment.** Concluding an audit needs `audit_min_quorum` voters with the
primary excluded from the jury, and only a node started with `--judge` posts a verdict. That floor is
independent of this record and is untouched by it.

## What it costs, measured rather than assumed

**5.02 s → 5.23 s per block**, about 4 %. `round 0` on 100 % of blocks **in both regimes**: the
consensus never had to retry, before or after. The cost of a proposer that must wait for somebody is
two tenths of a second per block.

Fault tolerance is genuinely reduced: before, the incumbent alone produced blocks; now it needs one of
two partners. That is the intended direction — "nobody produces alone" is the property being bought —
and the two-partner split is what keeps the price payable.

## Consequences

- `deploy/bond_validator.sh` already refused any bond leaving a validator at or above two thirds. Its
  `--accept-supermajority` escape hatch justified itself on the grounds that the newcomer would
  "contribute ONLY to the VRF seed". **That rationale was false** and is corrected in the same pass:
  under a supermajority the newcomer contributes to the seed on 10–21 % of heights, not on all of them.
  The flag remains, because a desktop validator that sleeps is a real constraint — but it now states
  what it actually costs.
- A genesis that hands one key the whole stake does not merely centralise the chain in principle; it
  makes every later validator decorative, silently, and the effect survives every health check.
- Any claim that a network "has N validators" must cite the **`Commit` rate**, not the bonded count.
  Two of the four validators on this chain were bonded, unjailed, and contributing nothing.

---

## Addendum (2026-08-16) — the seed-suppression threshold is ⌈1/3⌉, not two thirds

**The decision above stands and is not amended.** What follows adds a *second, stricter* target, because
the property it protects is a different one and the two thresholds do not follow from each other.

**Two independent bars gate the decentralized seed** ([`chain/x/jobs/keeper/decentralized_seed.go`](../../chain/x/jobs/keeper/decentralized_seed.go),
`committeeBaseSeedSourced`): a **head count**, `committee_min_vrf_contributors` — governed, `2` on this
chain — and a **power share**, `MinVrfContributorPowerBps` — a Go constant, `6667`, that no parameter
reaches. Below either bar the seed falls back to `AppHash(H-1):h` and the caller is told the seed is not
decentralized, so **no committee is drawn at all**.

**Hence the threshold.** A validator of weight `p` forces that fallback simply by withholding its vote
extension, whenever the power left contributing drops below the bar:

> `100 % − p < 66.67 %`  ⟺  **`p > 33.33 %`**

Withholding is not a fault anyone can distinguish from a slow link, it leaves the validator `BONDED`,
unjailed and never `Absent` — the same blind spot this ADR was written about — and it can be done
**per block**, so suppression is selective.

**What that means for the topologies this ADR discusses:**

| Bonded power | Who can suppress every draw by staying silent |
|---|---|
| 1 validator at 100 % | it does not need to: the head-count bar already blocks every draw |
| 2 validators, 62.5 / 37.5 | **both of them** — 62.5 % and 37.5 % are each below 66.67 % |
| 3 validators, 62.5 / 18.75 / 18.75 (**the target set above**) | the incumbent, alone. The two small partners lose the veto: 81.25 % still clears the bar |
| 3 validators at ⌈1/3⌉ each | nobody — but it is knife-edge: the remaining share equals the bar exactly |
| 4 validators at 25 % each | nobody, and the set still draws with **one** silent validator |

**Corollary, and it is the part that changes a plan: with two validators the veto cannot be removed at
all.** One of the two necessarily holds at least half, hence more than a third. Bonding a second
validator is still the right next step — it lifts `contributors` from 1 to 2 and arms anti-grinding —
but it buys the head-count bar, never this one.

**The cost to the attacker is not symmetric.** The code accepts this deliberately: drawing nothing is
safer than rejecting the block, since rejection in a loop halts the chain for a reason the attacker
chooses, and "the omission then stops paying, since it no longer moves any draw". That deterrent bites a
validator **who is also a miner**. For a validator that mines nothing, silence costs nothing at all.

**Target.** Beyond the rule above, aim for **no validator above ⌈1/3⌉ of bonded power**, which in
practice means **four or more** validators — three at exactly one third sits on the boundary, where a
single rounding or a stake change re-opens the veto. Until then, state plainly that any single validator
above a third can suppress committee draws at will; it is not a defect to be fixed by a parameter, since
`MinVrfContributorPowerBps` is a constant and lowering it would weaken the BFT bar it exists to enforce.

**How to read the current state.** `dendrad query jobs committee-seed-health -o json` reports
`latest_contributors` against `committee_min_vrf_contributors`; the power share is not exposed by any
query, so the only way to observe the second bar is the node's own
`SECURITY: decentralized VRF seed UNDER-WEIGHTED` log line.
