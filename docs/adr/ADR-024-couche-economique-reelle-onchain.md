# ADR-024 — Real economic layer on-chain: deployment and verified invariants

**Status:** Accepted — deployed and verified live.
**Implementation:** Implemented. Closes the implementation half of
[ADR-023](ADR-023-emission-reserve-onchain.md) and makes miner bonds and slashing operate on real
coins. Where [ADR-021](ADR-021-tokenomics-v5-native.md) fixes the *model* and ADR-023 *specifies* the
module, this record states what **actually runs on the chain** and the **invariants proven on-chain
to the µDNDR**.

## Context

Before this batch, the v5 tokenomics lived in Python, the chain only settled escrow fees, and a miner's
"stake" was a **counter** — a slash removed nothing bankable. Two gaps: Reserve emission did not exist
on-chain, and bonds and slashes were counters.

## Decisions in force (what is deployed)

### 1. Real Reserve emission (`x/emission`, `BeginBlock`)

- **Funded at genesis**: the `x/emission` module account holds the **3.3 M DNDR Reserve** from block 0,
  declared in the genesis configuration as an address **without a key and with zero minting**. Total
  supply stays **fixed at 10 M**, verified against `q bank total`
  (`10 000 000 000 000 udndr`).
- **Per-epoch release** (`RunEpoch`, every `EpochBlocks`): the deterministic core `EpochRelease`
  (integer udndr and basis points, mirroring the reference simulation) releases a fixed fraction of the
  remainder — with the work flow **gated to zero** while non-recoverable demand is zero, so only
  availability and security are credited.

### 2. Honest routing of the flows

- **Security** goes to the fee collector and on to validators and delegators through `x/distribution`.
  **It is the only distributed flow.**
- **Availability and work** **stay** on the module account as counters, awaiting their dedicated
  payout ([ADR-022](ADR-022-proof-of-useful-availability.md) for availability). They are **not**
  claimed to be paid out to validators.
- **Best-effort**: an unfunded account produces a log and a deferral (counters and Reserve unchanged,
  **no halt**).

### 3. Miner bonds and slashes in real coins

- **`CreateMiner`** escrows the stake (signer account to the jobs module). It **fails** on insufficient
  funds, so **no miner exists without an effective bond**.
- **`DeleteMiner`** refunds the **remaining** bond (post-slash); slashed funds stay with the module
  (treasury).
- **`UpdateMiner`** **preserves** the bond (the stake field is ignored; changing it means delete then
  recreate).
- **Slashing** on the verification and settlement paths reduces a **real refundable balance** (coins
  already escrowed), so the effect is bankable rather than a counter.

### 4. Hardening: manual slashing is gated

- `SlashMiner` and `ReportDivergence` — where the **caller** supplies the evidence, which is therefore
  forgeable — are **reserved to the governance authority**. Otherwise a real bond becomes a **griefing
  target**: anyone could destroy an honest operator's bond. **Legitimate** slashing stays
  **automatic through the committee** (an on-chain tally, never the caller).

### 5. Real gentle burn (v5 deflation)

- At liquidation, `FeeBurnBps` (**5 %**, governable) of the escrow is **burned** before the remainder is
  distributed to the winners, so the model's "5 % burn" is no longer a **counter** but a **real
  destruction of coins** (global supply falls). Conservation holds: `burn + paid + surplus returned =
  job.Fee`. This also provides the **demand signal** (a supply decrease readable through `bank`) needed
  to wire the demand-gated work flow without cross-module plumbing.

## Invariants verified on-chain (live, exact to the µDNDR)

- **Fixed supply**: `q bank total` equals `10 000 000 000 000 udndr`, **unchanged** despite roughly one
  billion udndr moved — zero minting.
- **Emission coupled to coins**: the emission module balance equals the remaining Reserve when
  everything is paid out.
- **Routing**: emission module balance equals `Reserve + WorkPool + AvailPool` (only security leaves).
  Measured over six epochs: `2 304 022 956 102 == reserve_left 1 640 038 260 171 + Σavail
  663 984 695 931 == 3.3M − Σsecurity 995 977 043 898`.
- **Real bonds**: jobs module balance equals the sum of bonds (3 miners × 2000 = 6000 udndr).
- **Tests**: `go test ./x/emission/... ./x/jobs/...` green, including a bond escrow test covering
  insufficient funds refused, real escrow, an 80 % slash, refund of the remainder, and **rejection of
  an unauthorised slash**.

## Consequences

- The economic layer moves from **simulated to real**: the Reserve decreases, coins move, bonds are
  deposits, slashes bite — all without ever minting.
- The reference simulation remains the **specification and test oracle**.

## Out of scope (later iterations)

- **Demand-gated work flow**: measure non-recoverable demand on-chain (non-recoverable fees per epoch)
  to activate the work flow, currently frozen at zero.
- **Availability distribution**: pay `AvailPool` out to providers
  ([ADR-022](ADR-022-proof-of-useful-availability.md)).
- **APR floor and team vesting**, a stake-weighted committee, and making `ReportDivergence`
  permissionless behind on-chain evidence with a reporter bounty.
