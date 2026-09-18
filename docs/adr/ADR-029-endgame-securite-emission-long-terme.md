# ADR-029 — Security endgame and long-term emission policy

**Status:** Decided and ratified — **Option A, amended** (fixed 10 M cap and zero minting maintained;
no tail emission to be coded). Re-openable if the network reaches scale. Completes
[ADR-021](ADR-021-tokenomics-v5-native.md) and [ADR-023](ADR-023-emission-reserve-onchain.md).
**Implementation:** Implemented as a *non-action*: no tail emission exists in the code, and the
unconditional "5 % APR floor" claim is withdrawn. The three amendments below are documentation and
policy commitments, not code.

> **Correction (2026-07-31) — every duration in years below is a projection, not a measurement.**
> The Reserve's depletion horizon was expressed in years by converting epochs at an *assumed* block
> interval of 1 s (`chain/x/chaintime/chaintime.go`, where that value is documented as a
> **target**, not a guarantee — the consensus does not fix the block interval; it emerges from
> `timeout_commit` and propagation). The interval on the live network measures **~5.03 s**, so every
> year figure in this record is off by that factor. What the record decides is unaffected: the Reserve
> still depletes, security still ends up fee-funded, and no tail emission exists. Durations are now
> stated in **epochs** — the unit the chain counts — everywhere outside this historical text. Launch
> genesis: `reserve_release_bps = 2` per 86,400-block epoch, half-life **3,466–6,932 epochs**.

## Context — the uncovered bet

The current tokenomics is **verified sound on the monetary axis**: **fixed 10 M supply, zero minting**
(structural, not a slogan), emission as the release of a pre-allocated Reserve (3.3 M), a gentle 5 %
burn, demand-gated. That is excellent for scarcity, honesty and anti-bubble discipline.

**But the design makes an implicit, uncovered bet on long-term security:**

- The Reserve **depletes** (roughly 20 to 30 years at a release rate of 22 % of the remainder per year).
  *(See the correction note above: the yearly framing rests on an unmeasured block interval. The
  depletion itself, which is what this record decides on, is unaffected.)*
- The burn **shrinks** supply continuously, so the security budget, **denominated in tokens**,
  contracts.
- **After depletion, security depends 100 % on real fees.** If demand is durably weak at that point,
  staking APR collapses **with no lever** (no tail emission). This is Bitcoin's "security budget
  cliff", imported.
- The **5 % staking APR floor** is **announced but not implemented on-chain** — no mechanism raises
  emission if APR falls. That is an over-claim and must be corrected.
- The Monte-Carlo run reports "floor robust, 100 %", but that is a **horizon artefact**: ten years is
  too short to drain the Reserve. Under a 30-year stress the Reserve reaches zero around year 26 and
  APR goes to zero.

## Options

**A — Pure fixed Reserve, security from fees alone (status quo, owned).** Change nothing, and
**explicitly accept** that after roughly twenty years security holds only if fees fund it, then design
the fee market for that. *For*: maximum scarcity, the "10 M frozen" story intact, zero complexity.
*Against*: no cover at all if demand is weak at that horizon; the "5 % floor" claim must be withdrawn.

**B — Fixed Reserve plus a minimal tail emission activatable by governance (insurance, off by
default).** Keep 10 M fixed as the default and the story, but pre-code a **security tail emission**
triggerable only if security APR stays below a floor (3-5 %) over a sustained window, capped (0.5-1 %
per year), off by default, requiring a vote. This is the mechanism that would **actually implement** the
APR floor. *For*: preserves "zero minting today" (the tail is dormant, disclosed, never active without
a vote) while covering the cliff. *Against*: "fixed supply" becomes "fixed supply except an emergency
security vote", which must be communicated frankly — it is no longer an inviolable hard cap.

**C — Partial burn-and-mint for the work flow.** Mint to pay providers, burn the fees, decoupling the
security budget from a depleting Reserve. *For*: the most sustainable model for a perpetual work token.
*Against*: **breaks the "zero minting, 10 M frozen" identity**, a large rewrite, and it reintroduces
exactly the inflationary complexity that was eliminated. Too disruptive.

## Adversarial review and state of the art — why NOT Option B

Option B was red-teamed against its own initial recommendation, alongside a comparables survey. The
result: **B is the worst mechanism.**

**Red team of B (a dormant tail activatable by governance):**

- **"Zero minting" is binary; B makes it probabilistic.** The market prices dilution **from the moment
  the dormant tail is disclosed**, not from its activation — so the narrative cost is paid roughly
  twenty years before any hypothetical benefit. The one verified net advantage (a hard cap) evaporates
  on announcement day.
- **The trigger is captured by its beneficiaries.** "APR below the floor" fires precisely when
  validators are underpaid, and it is those same validators (stake-weighted) who vote their own tap
  open — and they can **deliberately under-bond** to arm it. A structural conflict of interest.
- **"Capped and bounded" is not an invariant.** The same parameter-update message that arms the tail
  can raise its cap: a slippery slope in a bear market, exactly when defences are weakest.
- **It treats the symptom, not the cause.** The tail only fires if demand is durably weak — but
  printing 1 % per year of security in a token with no demand is security denominated in a depreciating
  asset.

**PoS state of the art:**

- **Cosmos Hub**: perpetual dynamic inflation of 7-10 %, targeting 67 % bonded; the "inflation to zero"
  proposal was **rejected**. Pure fixed supply is **against the grain** of the Cosmos framework.
- **Bitcoin**: no tail, so the "security budget cliff" is **unresolved** (fees are around 1.25 % of the
  block reward) — the trap that Option A imports.
- **Monero** (0.6 XMR per block, about 1 % per year), **Ethereum** ("minimum viable issuance" plus a
  burn), **Lattice** (about 0.065 % per year): the dominant prudent choice is a **small, perpetual,
  PREDICTABLE tail, decided at design time** — "because you cannot bet on a future fee market, and
  adding one after the fact is culturally and technically near impossible".

The two lessons appear contradictory: research says "a small predictable tail beats a pure cap", while
the red team says "a *conditional or discretionary* tail is capturable". **The only defensible tail
would be FIXED and PREDICTABLE (non-conditional), decided now** — which means frankly abandoning "zero
minting", not abandoning it quietly.

## Decision — Option A, amended

**Keep the fixed 10 M cap and zero minting (Option A), amended on three points**, because (1) the hard
cap is the **only verified differentiator** and the heart of the "honesty over hype" position; (2) a
*conditional* tail (B) is capturable, and a *perpetual tail now* (D) would sacrifice the differentiator
to cover a twenty-year risk; (3) the project is bounded by adoption — if external demand does not
arrive, it stops at the one-to-three-year horizon, long before the cliff, so over-engineering
twenty-year insurance now would be premature optimisation.

The three amendments:

1. **Withdraw the unconditional "5 % APR floor" claim** (an over-claim: it does not exist on-chain).
   Replace it with "security funded by the Reserve for roughly twenty years or more, then by fees; no
   guaranteed floor beyond that".
2. **Document the security cliff HONESTLY** (in the records and the litepaper). Do not hide it — that
   is precisely the position. Design the **fee market** as the priority for post-Reserve security.
3. **Pre-commit to a POLICY, not an on-chain lever**, to neutralise the "you cannot add it late"
   objection: IF, as Reserve depletion approaches, real fees are durably below validator cost, THEN
   governance will adopt a **minimal FIXED and predictable tail** (Monero or minimum-viable-issuance
   style, formula pre-specified), decided **with real data**. This is a rule disclosed in advance, not
   a dormant discretionary tap (unlike B) and not an active mint today (so "zero minting" holds).

## Consequences

- **Code**: NO tail is coded. The APR floor is removed or re-labelled in the model and everywhere it is
  displayed, and effort stays on the real incentive gaps (the availability pool of
  [ADR-022](ADR-022-proof-of-useful-availability.md), the runtime negative-expected-value guard). No
  minting is introduced.
- **Model**: extend the economic simulation to **30 years**, simulate A against a minimal tail to
  quantify the real margin, and correct the "floor robust, 100 %" label as a horizon artefact.
- **Public narrative**: the site and litepaper keep "fixed supply, zero minting" **but** add an honest
  line on the security endgame (fees after roughly twenty years, plus the pre-committed policy of point
  3). No unmet "5 % floor".
- **Reversibility**: if long-term security is valued above the differentiator, **Option D (a small
  perpetual tail decided now, Monero style, roughly 0.25-1 % per year)** is the only other defensible
  choice, to be adopted explicitly, never quietly. B (dormant and conditional) is **definitively
  rejected**.

## Recorded dissent

A security purist would choose **D** (a minimal perpetual tail from the start): the state of the art
leans that way, and "adding it late is near impossible". **A amended** is chosen because the project is
adoption-bounded — the existential risk is demand in years one to three, not security in year twenty —
and because the verified hard cap is the only differentiating asset today. **This is a brand-and-survival
versus long-term-security trade-off, owned as such, and to be re-opened if the network reaches sustained
scale.**
