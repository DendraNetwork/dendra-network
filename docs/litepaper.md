# Dendra — Litepaper

**Useful-work AI on a sovereign Cosmos L1.**
$DNDR · fixed supply · devnet / research preview · open source

> **Status note.** This document describes a **development network** (devnet / research preview), not a finished product. Sections marked *(roadmap)* are not yet implemented. $DNDR is a utility token; **there is no token sale**, and **testnet tokens carry no monetary value**. Verification has moved to an *optimistic* model (sampled audit + LLM-judge — ADR-025/026/028); any redundant-committee / cosine description below is the earlier design. **Every measurement quoted here is given with its scope**: the slash and clawback paths are covered by the chain's unit and integration tests, and the false-slash rate was measured on a **single machine** — the current network is not yet a multi-operator deployment. **And the mechanisms described below are the protocol, not a report on the running network**: the public endpoint's validator set is small and entirely project-operated — read its size from the chain, never from this page — and the chain refuses to open a job at all below its miner floor. That floor is `audit_min_quorum` + 1 registered miners eligible at the block, because the jury excludes the miner under audit; both numbers are queryable (`/dendra/jobs/v1/params` and `/dendra/jobs/v1/miner`) and **this page deliberately quotes neither**, since a count written into a document is wrong at the next registration. Historical note, superseded: an earlier chain ran three validators, the third an outside operator bonding 9 DNDR against the largest validator's 1 000 000. That state, the emission module's funding, and the queries that check both, are in §8. **The settlement path is armed from block 1** — miner, validator and whistleblower each have a flow, funded by client fees and the pre-allocated Reserve and never by inflation (§6 bis) — but read "paid" carefully: validators, treasury and the dev share receive coins at settlement, while the miner's entire net share is **held**, and on a job the audit lottery selects it cannot be released until a committee returns a verdict. No amount, rate or yield is offered here. **Contribution is recorded and nothing is promised**: see §6 ter.

---

## Abstract

Dendra is a Cosmos-SDK Layer-1 whose economic layer rewards **real machine-learning inference** performed on consumer GPUs, rather than hashing. A client submits an end-to-end-encrypted prompt; the chain escrows a fee, assigns a miner through an unpredictable decentralized-VRF seed, the miner runs the model locally and is paid optimistically, and a sampled fraction of jobs is re-checked by a fresh committee with an LLM-as-judge — cheating is slashed, all settled in a fixed-supply, zero-inflation token. Consensus remains CometBFT BFT (validators are distinct from miners); the novelty is the *useful-work market* layered on top.

*(Honest note: the optimistic path — k=1 + sampled audit + LLM-judge, ADR-025/026/028 — is coded and tested; the chain binary defaults to the earlier redundant-committee path (§3–4, kept for reference), and the **launch genesis arms optimistic mode from block 1**. What is measured, and where: the slash, clawback and silence paths are exercised by the chain's own test suite, and the false-slash rate was measured on a **single machine** running every identity — a bench, not a deployment. Pool payouts are unit-tested and observed on that bench. No slash has yet been artifacted on a network of independent operators; producing that artifact is the next milestone, not a past result.)*

---

## 1. The problem

Two trends are unserved by existing chains:

1. **Wasteful security.** Proof-of-Work spends gigawatts on hashes whose only value is difficulty. The compute does nothing else.
2. **Centralized, opaque AI.** Inference is concentrated in a few clouds; users must trust both the operator's honesty *and* its handling of their data.

Dendra addresses both: the work the network pays for is **inference people actually want**, done **privately** and **verifiably**, on hardware people already own.

---

## 2. Architecture

```
Client ──enc prompt──► OpenAI-compatible Gateway ──► Dendra chain (x/jobs)
                                                       │  escrow fee
                                                       │  decentralized-VRF seed → stake-weighted miner
                                                       ▼
                                        Assigned GPU miner (Ollama, consumer GPU)
                                                       │  anchored result commit, paid optimistically
                                                       ▼
                                        VRF-sampled subset → fresh committee + LLM-judge → slash cheats
                                                       │
Client ◄────────────────── real answer ◄──────────────┘
```

*(Diagram shows the **optimistic** model — ADR-025/026/028 — **armed from block 1 in the launch genesis**. The chain binary's default remains the earlier redundant-committee path described in §3–4, kept for reference.)*

- **Consensus:** CometBFT BFT. The block interval is **not** a protocol constant — it emerges from `timeout_commit` and propagation; **measured at about 5 s** on the public endpoint, on a chain that has since been replaced — so read durations from the chain in blocks, never from a seconds figure on this page. **Miners ≠ validators** — GPU possession never secures consensus.
- **Gateway:** an OpenAI-compatible endpoint (`/v1/chat/completions`); any existing client (e.g. Open WebUI) uses Dendra unchanged.
- **On-chain module `x/jobs`:** escrow, committee assignment, commit anchoring, settlement, slashing, pools.
- **Off-chain:** miners (`miner`), an encrypted relay bus, a Prometheus/Grafana supervision stack.

---

## 3. Job lifecycle (redundant mode — the binary's Go default, NOT what the network runs)

> This is the **redundant-committee** path, i.e. what the chain binary does when `verification_mode=0` — its **Go default**. It is **not** what the public network runs: the launch genesis sets `verification_mode=1`, and the live chain answers `1` when queried. The **optimistic** model that supersedes it as the project's thesis is described in §4 bis.

1. **Open + escrow.** The gateway opens a job and locks the fee in a module account. Pricing is pay-per-token: `fee = base + per_token × (in + out)`, with a two-phase escrow that settles on the *effective* output.
2. **Assignment.** `open-job` fixes a seed derived from unpredictable block data (a decentralized **ECVRF** beacon aggregated via vote-extensions, bound to the block hash); the committee of size *k* = 3 is the set of miners whose `hash(seed | jobId | minerId)` ranks lowest, **weighted by stake**. The requester cannot grind the job id to choose a complicit committee (anti-grinding), and splitting stake across identities does not increase influence (anti-Sybil).
3. **Inference.** Committee miners decrypt the prompt in RAM, run the LLM, and anchor a *commit* (hash + embedding) on-chain. Plaintext never leaves the miner.
4. **Settlement.** The chain reads the anchored commits of the assigned committee, computes the honest majority, pays it from escrow, slashes divergent miners' real bonded stake, and returns the answer to the client.

Settlement is **replay-safe**: a single shared predicate marks a job paid across every settlement path, so it can never be paid twice.

---

## 4. Verification — semantic, not exact (redundant mode)

LLM output is non-deterministic, so byte-equality verification fails. In redundant mode, Dendra clusters the committee's anchored **embeddings** by an **integer cosine** computed in `big.Int` (no floating point, hence reproducible across validators). Two honest answers with the same meaning stay close and survive; an outlier that diverges beyond a governed threshold is slashed. This tolerates legitimate variation while still punishing wrong or lazy work.

> **Measured limit.** Cosine-of-embeddings is **not a reliable judge of correctness**, even with a real embedder: an order-insensitive embedder (`nomic-embed-text`) accepts both keyword-salad and fluent-but-false answers above the threshold. This is what motivates the optimistic model below — the cosine is retired *as a judge*.

## 4 bis. Verification — optimistic (the thesis; armed at launch)

The project has **pivoted** (ADR-025/026/028) to an **optimistic** model, gated by a `verification_mode` parameter. The chain binary **defaults to `0` (redundant)**; the **launch genesis sets it to `1` from block 1**. In optimistic mode:

1. **k = 1, paid optimistically.** A single stake-weighted **primary** miner is drawn by the existing committee selector, answers, anchors its commit, and is paid — *provisionally* on auditable jobs (ADR-028). Under the launch genesis (`hold_bps` at maximum) the miner's net is **retained at settlement** and resolved at the audit checkpoint, where a job the lottery skips is released to it and a job the lottery takes waits for a verdict; see §6 bis. Cost falls from ~3× to ~1×; latency is one inference, which unblocks streaming and large models.
2. **VRF-sampled audit.** After the commit, at each block the decentralized VRF seed decides — via `H(seed ‖ jobId) mod 10000 < audit_sample_bps` — whether a job is audited. Because the seed is posted *after* the commit, the miner cannot know in advance if it will be checked. `audit_sample_bps` is an on-chain, **governable** parameter: it is set high on the current testnet (5000 = 50%, to gather evidence fast) and is meant to fall toward ~10% as the network matures. That rate governs which jobs are *selected*; whether a selected audit *opens* is a separate condition, and on the current endpoint it is not met (§8).
3. **Fresh committee + LLM-as-judge.** On an audited job, the primary **reveals** its answer to a fresh committee (excluding the original miner, stake-weighted); each member runs an **LLM-as-judge** and commits a **binary verdict** (`"1"` valid / `"0"` invalid). Judge-model **heterogeneity is a design requirement** — a single shared model makes judge errors *correlated*, which is what produced false slashes under measurement. It is **configured by operators and enforced by the launcher's hardware probe; consensus does not check which model a juror ran**, so it is a convention the network relies on, not a guarantee it enforces. Moving an admissible-model allow-list on-chain is open work. A slash then requires two-thirds of the anchored seats *and* a stake majority (see below).
4. **Hard slash, clawback, appeal (ADR-028).** Proven divergence → the provisional payment is **clawed back** and the primary's stake **slashed** (`SlashLeakBps`, 80%). A miner that **stays silent** is clawed back and slashed too (closing the silence-evasion gap). No slash is applied below the verdict bar (anti false-positive). A late-reveal **appeal window** is implemented but **dormant in the shipped genesis** (`appeal_window = 0`): until it is armed, an honest miner that was merely offline recovers through governance, not automatically.
5. **Nash-sized.** Cheating is loss-making whenever `s·P > (1−s)·g` (audit rate `s`, slash `P`, cheat gain `g`); job opening is capped so `fee ≤ stake/nashFactor`.

The elegance: auditing only a *fraction* of jobs makes a **costly LLM-judge affordable** — the budget that the always-on k=3 path could never fund — so detection quality rises *and* total cost falls at the same time. Deterrence does not need every job checked; it needs the *expected* cost of cheating to stay negative.

*(Honest status. The optimistic path and its off-chain reveal/judge glue are **coded and covered by the chain's test suite**. The economic outcome was measured on a **bench where a single machine ran every identity**: 2×N=100 audited jobs with a heterogeneous committee gave a **0.0 % false-slash rate on honest miners**, while a revealing cheater lost 84–99 % of its stake and a silent one ~80 %. **That is a bench figure, not a network figure** — a single machine is not a multi-operator deployment, and no slash has yet been artifacted on one. The structural property, however, is enforced by the binary: a hard slash requires **two independent locks, both priced in capital** — "invalid" verdicts from at least **two-thirds of the ANCHORED committee seats**, *and* a **strict majority of the voting stake**, in both code paths. The committee is **drawn on-chain from the VRF seed and anchored before anyone can vote**, and the draw targets **15 seats**, so the bar rises with the size of the eligible pool: influencing a verdict costs capital, not identities.)*

---

## 5. Confidentiality — two modes, stated honestly

- **Mode A — Standard (default).** One GPU miner; the prompt is end-to-end encrypted (X25519 ECDH + AES-256-GCM). Nothing in clear at the relay or on-chain. **Honest limit:** the miner decrypts in RAM to compute, so privacy on the consumer tier is **hardened deterrence, not cryptography** — sealed memory (`mlock`), egress and disk guards, and slashing raise the cost of extraction; none of them prevent a determined host operator. **Remote attestation is implemented but off by default** (`DENDRA_ATTEST_REQUIRE=0`, and the deployment kit does not set it), so it must not be counted as an active protection on the current network.
- **Mode B — Confidential (opt-in, research).** 3-party MPC; private as long as at most one of three nodes colludes, so the miner never sees plaintext. **Honest limit:** a research spike, not production — residential RTT (~50 ms) makes it slow, and secure non-linearities (softmax/GeLU/LayerNorm) are modeled but not implemented.
- **Hardware-isolated (TEE) tier.** *(roadmap)* Datacenter, opt-in. **It does not exist today** and nothing in the consumer path should be read as a TEE claim.

These limits are published rather than papered over with a guarantee the protocol does not have. In the same spirit: content moderation is a **deterministic regex floor** with acknowledged false negatives — the protocol does **not** claim that illegal content is filtered out.

---

## 6. Tokenomics — $DNDR

A **fixed-supply utility token**: the medium for paying for inference and rewarding miners.

| Property | Value |
|---|---|
| Max supply | **10,000,000 DNDR** (hard cap, **zero inflation, zero mint**) |
| Base unit | `udndr` — 1 DNDR = 1,000,000 udndr |
| Genesis allocation | Community 34% · Reserve 33% · Validator treasury 27% · Team 5% · Faucet float 1% — 10,000,000 DNDR exactly, readable from the launch genesis (`chain/config.optimistic.yml`) |
| Emission | Release of the pre-allocated **Reserve** only — a geometric, decreasing share of what remains. **No minting, at any rate.** |
| Release rate | A **governable on-chain parameter** (`reserve_release_bps` per `epoch_blocks`), distinct from the binary's compiled defaults. The launch genesis ships `reserve_release_bps = 2` per **86,400-block epoch** — a **long-lifetime** calibration whose half-life is **6,932 epochs** on an idle chain and **3,466 epochs** under saturated demand, leaving 93–96 % of the Reserve after 365 epochs. Those figures are stated in **epochs**, the unit the chain actually counts; they are deliberately **not** converted into years, because the wall-clock length of an epoch depends on the block interval, which the consensus does not fix (it emerges from `timeout_commit` and propagation, and is measurable at any time from two block headers). Read the live value with `dendrad query emission params -o json`. **This is the drain rate of a pool, not a yield**: it says how fast the Reserve empties, never what a participant receives. |
| Emission flows | work (demand-gated 1.5×) · availability (slashable challenge) · security |
| Burn | 5% of fees (`fee_burn_bps = 500`) |
| Protocol cut | 15% of a job (split: validators 50% / dev 20% / treasury 30%) |

### 6 bis. Who gets paid, for what, and out of which pocket

The protocol **pays for work**, from block 1 of the launch genesis. Two pockets fund every flow, and neither is inflation: the **client fee** escrowed at job opening, and the **pre-allocated Reserve** released in decreasing slices (`x/emission/keeper/epoch.go`) — a mechanism that is implemented and tested, and whose module account the boot **refuses to start without** — the 3.3 M are credited to it in the genesis and the node verifies the balance before serving, so the release path is not blocked by an empty pocket (§8 gives the query). There is no third pocket — `x/mint` is absent from the binary, so no reward path can mint (`chain/app/fixed_supply_test.go`).

| Role | What it is paid | Implemented in |
|---|---|---|
| **Miner** | The job fee less soft burn and protocol cut; plus a **work subsidy** capped at `work_gate_bps × demand`; plus **availability**, pro rata to bond | `x/jobs/keeper/msg_server_settle_semantic.go` · `msg_server_claim_subsidy.go` · `availability.go` |
| **Validator** | Transaction fees; a share of the protocol cut (`validator_reward_bps`); and the epoch **security** slice, routed to `fee_collector` and on to validators and delegators through `x/distribution` — the only emission flow that leaves the module account | `x/emission/keeper/epoch.go` |
| **Whistleblower** | On an upheld dispute: the `dispute_bond` back **plus** a reward, bounded by the remaining Treasury. On an unfounded one, the bond is forfeited to the Treasury | `x/jobs/keeper/msg_server_adjudicate.go` |

**No amount, rate or yield is stated here or anywhere else, and none can be derived.** Every quantity above is a governable on-chain parameter, and what any participant actually receives depends on real traffic and on what remains in the Reserve. Read the live values with `dendrad query jobs params -o json` and `dendrad query emission params -o json`.

**Escrow, as a property.** `hold_bps` sits at its maximum in the launch genesis, so a miner's net payment is **retained at settlement** and resolved at the job's audit checkpoint. A job the audit lottery does not select is **released to the miner** there (`audit_sampling.go`, the `releaseHeld` branch) — at the genesis `audit_sample_bps = 5000` that is about half of jobs, a governable figure to be read from the chain rather than from this page. A job it does select waits for the audit layer. ⚠️ **That whole sentence assumes a lottery ran at all.** Below the seed's contributor floor the pass defers *before* reaching the lottery, so there is neither a selected job nor an unselected one, and no fee is released either way — the release branch is simply not reached. Both floors are readings, not properties: `dendrad query jobs committee-seed-health -o json` for the seed, `list-miner` plus `params` for the jury. Until both are cleared, every fee stays **held**.

Held means **retained, not cancelled and not expired**. The deferral has no timeout and no attempt counter, by design: releasing a payment because the audit could not run would let an abstaining validator push jobs through unverified, and cancelling it would punish a miner for someone else's absence. The backlog drains in two stages, and **neither is open while its floor is unmet**. The first needs the seed's contributor floor: below it the audit pass defers before the lottery, so nothing finalises and nothing releases — not even a job the lottery would not have selected. Only a **bonded validator** anchoring a VRF key raises that reading; registering as a miner does not. The second stage is what the lottery selected, and it needs a jury: an audit resolves — in either direction — only when at least `audit_min_quorum` jurors vote, and the jury excludes the miner being judged, so the floor is that parameter plus one. Read both from the chain and do the addition yourself. Operators arriving, not a patch. `dendrad query jobs held-summary -o json` reports the retained total and the **age of the oldest retention**, which is what makes "retained, not lost" a falsifiable claim rather than a reassurance.

**Key properties.**
- **No protocol minting.** All rewards come from releasing the pre-allocated Reserve; the chain does not mint new tokens for them. The protocol's **custom modules hold no `Minter` permission**, and the standard `x/mint` module is **removed from the chain binary entirely** — there is no minting module to configure, so fixed supply holds **by construction**, verified at runtime: a fresh genesis boots to a total supply of **exactly 10,000,000 DNDR** in a single denom. Neutralising `x/mint` through `inflation=0` genesis parameters would leave a lever a governance proposal could pull; de-registering the module removes the lever.
- **Demand-gated work flow.** The work subsidy is bounded at `1.5×` a non-recoverable demand counter (fees burned/spent). Self-dealing — a miner paying its own jobs through a separate address — is possible, but kept **economically -EV** (the cut + burn paid exceed the subsidy unlocked), so it cannot drain emission. That counter is a settlement-volume proxy, **not** a measure of external traction.
- **Real skin in the game.** Miner bonds are escrowed real coins; slashing destroys real value; the 5% burn really reduces supply.
- **Validated (supply).** The bound is *structural*, not statistical, so no simulation is cited for it: emission only releases the pre-allocated Reserve, and no code path can create a coin. `chain/app/fixed_supply_test.go` locks three independent facts — no minting module is wired into the app, the exact per-module permission set is pinned (only IBC `transfer` carries `Minter`, and only for foreign denominations, never native `udndr`), and both committed genesis files total exactly 10,000,000 DNDR.
- **Security endgame (stated honestly).** A fixed supply means a finite security budget. Under the **launch genesis** release setting the Reserve has a half-life of **3,466 to 6,932 epochs** depending on demand — stated in epochs on purpose, since converting it to years would require assuming a block interval the consensus does not guarantee. It is **a model output, not a commitment**, bracketed by a lifetime test rather than promised, and a governable parameter can shorten or lengthen it at any time. Once the Reserve is depleted, security relies on real transaction fees: **there is no on-chain guaranteed yield floor of any kind**, and none is promised. If fees prove insufficient at that horizon, governance may adopt a minimal, predictable tail-emission — a policy decided *then* with real data, never a dormant discretionary lever. The trade-off is named here rather than left for a reader to discover.

---

## 7. Security model (what to trust, and what not to)

- **Trust the BFT consensus** for ordering/finality (standard CometBFT assumptions).
- **Trust the economic verdict** for inference correctness only *up to* the two locks of §4 bis: two-thirds of the anchored audit seats plus a strict majority of the voting stake. The VRF seed makes assignment unpredictable; stake-weighting makes Sybil splitting pointless; real bonds make lying costly. It is a deterrent priced in capital, not a proof of correctness.
- **Do not** treat Mode-A confidentiality as cryptographic — it is deterrence, and remote attestation is off by default (§5).
- **Do not** treat the judge-model requirement as a protocol guarantee. Heterogeneity is an operator-side convention; **consensus does not check which model a juror ran**, and the on-chain `audit_judge_model` parameter is **inert** — no execution path reads it.
- **Do not** treat this as audited-for-production. It has gone through repeated internal review cycles but **no external audit**. Some components remain modeled or partial: **Mode-B MPC**; the on-chain model registry is implemented and *can* be enforced, but enforcement is **off by default** and off on the current network, so the served model is **not** pinned by the chain today.
- **What the binary does enforce**, and what the tests cover: fixed supply (`x/mint` de-registered; a fresh genesis boots to exactly 10,000,000 DNDR in a single denom), a job settling at most once, bonds never touched outside a slash, immutable anchored commits, and the two-lock slash bar of §4 bis in both code paths.
- **Decentralized randomness is implemented**: per-validator VRF aggregated via ABCI++ vote-extensions, consumed by committee selection, with the VRF input **bound to the block hash** (anti-precompute). It has been **demonstrated across two physical machines**. Note the honest consequence, which is the state of the public endpoint today: below `committee_min_vrf_contributors` the seed is not accepted as decentralized. For ordinary assignment the chain then falls back to a legacy seed with anti-grinding **inactive**; for the **audit draw**, under `committee_seed_source=1`, there is no fallback at all — the draw is **deferred**, the fee stays held, and **no job is verified**. The contributor count is exposed on-chain (`committee-seed-health`) and the deferred backlog is queryable (`audit-deferred`), so anyone can check which regime a given draw ran under, or whether it ran.
- **Treat "real on-chain economy" as proven on a devnet bench**, not in production and not across independent operators.

Summaries of the internal review cycles are available on request, covering what each cycle found and what remains open. That transparency is part of the security posture.

---

## 8. Status & roadmap

Two things are described in this section and they must not be read as one. **What has been exercised**
is a project-operated bench. **What is publicly reachable** is a devnet endpoint, and it is a strict
subset of the bench.

**Exercised on a devnet bench:** end-to-end real inference, the full on-chain economy in real coins (emission / bonds / slash / burn / stake-weighted committees), E2E encryption, replay-safe settlement, least-privilege permissions, a **real RFC-9381 ECVRF** (verified on-chain for availability proofs and committee seeding), **2-validator consensus demonstrated across two physical machines** (decentralized VRF, two contributors per block under strict signatures), **on-chain miner-key anchoring** (anti-MITM) with key rotation, and secure-by-default settings — all covered by the test suite and committed. Optimistic verification + LLM-as-judge (ADR-025/026/028) is coded, tested, and armed from block 1 in the launch genesis.

**Publicly reachable today:** an **open devnet endpoint** — blocks, a queryable API, a published and verifiable genesis, and a deployment kit a third party can run. The chain has been destroyed and relaunched more than once, onto a corrected genesis — the emission Reserve sits in the module account the release path actually draws from — and to ship consensus changes that cannot be applied node by node. Its validators are **all operated by the project**, so their number is a count and not a decentralisation, and stopping any one of them stops block production whatever that number is. Two consequences follow mechanically, and both are queryable rather than asserted. **No job can be created** while fewer than `audit_min_quorum` + 1 miners are eligible at the block — a job whose jury could never reach quorum would freeze its fee forever, so the chain refuses it outright and names the shortfall in the error. And **no committee is drawn** while `latest_contributors` stays below `committee_min_vrf_contributors`: the beacon then runs on its legacy fallback and the anti-grinding property is **not** in force, which the node states in its own logs at every block. Read both readings from the chain, never from this page.

**A third condition, cleared at the genesis of every relaunch since.** The Reserve described in §6 used to exist as an *allocation* only: the 3.3 M DNDR sat in an ordinary genesis account while the account the release path draws from — the emission **module account** — was empty, so at each epoch the module computed a release, attempted the security transfer, failed, and **skipped that epoch without loss**. A transfer could not fix it — a third of the supply cannot be moved into a module account by transaction — so it was fixed where it is decided, in the genesis, and the node now **refuses to boot** on a Reserve counter that no module balance backs. Both halves stay queryable rather than asserted: `dendrad query auth module-account emission` gives the address, `dendrad query bank balances <that address>` gives what it holds, and the genesis `app_state.bank.balances` says what was credited. ⚠️ Funded is not paid: emission still releases only at an epoch boundary, and the miner's net share stays **held** until an audit can conclude.

**It is not an unpaid endpoint, either — but "paid" needs reading.** The settlement path is armed from block 1: a settled job splits the client fee, and validators, treasury and the dev share receive coins immediately. The **miner's entire net share is held**, not paid: with `hold_bps = 10000` the whole of it is retained until an audit can conclude, and no audit can conclude while the draw sits below its contributor floor. So the protocol **distributes and records a debt**; it does not yet pay the miner. Retained is neither cancelled nor expired, and `dendrad query jobs held-summary -o json` returns the total, the count and the age of the oldest.

⚠️ **And a retained fee locks the miner in.** A held payment counts as an open obligation, and the guard that refuses a voluntary exit also blocks eviction: a miner in that state cannot leave and cannot be removed, and its bond stays locked until audits can conclude — which needs enough registered miners to form a jury. This is fail-closed working as designed, and it is written here because it binds capital: do not stake more than you are willing to leave in place, and note that a reset erases both the bond and the debt.

**§6 ter — Contribution is recorded, and nothing is promised.** Every job, settlement, verdict and slash is written into the block that carries it, and the project publishes state snapshots at named block heights — block archive, state export and the exact binary, each with its digest — so that record survives a reset. If a mainnet is launched, the project **intends** that record to count among the inputs to how an allocation is decided. It does **not** promise that a mainnet will exist, that any distribution will happen, or any amount, rate, ratio, date, eligibility or reservation; arriving early carries no advantage of its own, there is no enrolment and nothing to claim. The published index is a provisional reading of public state, not a ledger of entitlements, and its formula can change or be withdrawn. One limit stated plainly: every validator on this network is operated by the project, so a snapshot's signatures all come from one party — it is a **dated commitment the project cannot withdraw**, not evidence against it. Verifying it independently means replaying the blocks with the published binary. Testnet `$DNDR` still carries no monetary value, is not sold, and a reset can wipe any balance.

**Scope, stated plainly.** Everything in the bench paragraph was exercised with a **small number of identities, most of them co-resident on one machine**. That is a bench. Until the same behaviour is reproduced by **independent operators on separate hardware**, the network is not described here as distributed or decentralized — those words are reserved for a real multi-operator deployment.

**Roadmap:**

1. **Artifact the optimistic slash & pool payouts across independent operators** — the critical economic-security milestone, and the one that turns the bench figures of §4 bis into network evidence.
2. **Maturation** — *shipped:* governable emission params, an on-chain model registry that can be enforced (off by default), real work/availability distribution, verifiable VRF for availability & committee seed, 2-validator consensus, secure-by-default settings. *Next:* public explorer + CI.
3. **Multi-operator public network** — multi-machine, cross-hardware, community node operators. Protocol payment for work is not part of this milestone: it is live already (§6). What this milestone adds is enough independent operators for the audit layer to draw, which is what settles the retained share of those payments. Any reward beyond protocol payment would come from the pre-allocated community share; **no new token is ever created**, and testnet tokens carry no monetary value.
4. **Hardening + external audit** — an on-chain allow-list for judge models, Mode-B decision, remote attestation enabled by default, third-party audit.
5. **Mainnet** — only after the above. No date is promised.

---

## Disclaimer

Dendra is an open-source **research preview running on a development network**. **$DNDR is a utility token** used inside the protocol; it is **not** a security or an investment, there is **no token sale, presale, or ICO**, **testnet tokens carry no monetary value**, and nothing in this document is financial, legal, or investment advice. No yield, return, or price outcome is offered or implied. The software is provided "as is", has **not** undergone an external security audit, and must not be used to handle real value. Default-mode confidentiality is hardened deterrence, not a cryptographic guarantee, and content moderation is a regex floor with known false negatives — the protocol does not claim that illegal content is filtered. Read the source and do your own research.
