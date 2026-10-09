# Dendra — Litepaper

**Useful-work AI on a sovereign Cosmos L1.**
$DNDR · fixed supply · public testnet (`dendra-testnet`) · open source

> **About this document.** It describes the Dendra protocol and the launch configuration of `dendra-testnet`, the public testnet. $DNDR is a utility token used inside the protocol to pay for inference and reward miners; **there is no token sale, presale or ICO**, and testnet DNDR is never sold. The Final Testnet Season, the points it pays and the mainnet that follows it are set out in §6 ter and the Disclaimer. Verification is *optimistic*: a job is paid at finality with no verdict unless a random sample selects it for re-checking by other miners, who grade the answer with a language model (*LLM-as-judge*; ADR-025/026/028); a selected job is paid only on an upheld verdict. Every parameter named here is queryable (`/dendra/jobs/v1/params`), so this page names parameters and read commands rather than live values. **The settlement path is armed from block 1** — miner, validator and whistleblower each have a flow, funded by client fees and the pre-allocated Reserve and never by inflation (§6 bis). No protocol amount, rate or yield is offered here; the Final Testnet Season is a separate, capped programme with its own published formula (§6 ter).

---

## Abstract

Dendra is a Cosmos-SDK Layer-1 whose economic layer pays for **real AI inference** on consumer hardware instead of hashing. Open-weight AI models, served by the GPUs people already own — and a machine joins with a GPU or with a CPU. A GPU (NVIDIA) mines: the kit's hardware probe (`deploy/hw_probe.sh`) picks the open model the card can serve, from its memory. A CPU judges, for now: the network's judge model, a mixture-of-experts, runs on the CPU and needs at least `MOE_CPU_MIN_RAM_MB` of system RAM (the floor `deploy/hw_probe.sh` applies), with or without a GPU (under Windows, the RAM of WSL 2); a juror is a registered miner, so a machine without a card also serves the requests the chain assigns it, with that same judge model on its CPU. No mining model runs on the CPU: without a usable NVIDIA card and below that floor, a machine has no role. A full node or a validator needs no GPU, and more roles and technologies for CPU-only machines are planned. A client sends a prompt to an OpenAI-compatible gateway; the chain escrows a fee and draws a *present* miner — one that recently proved it is online — from a verifiable random seed produced by the validators; the gateway seals (encrypts) the prompt to that miner's on-chain key; the miner answers on its own machine and is paid optimistically once the job is final. A random sample of jobs is re-checked by a fresh jury of other miners running an LLM-as-judge, and a cheat that the jury convicts is *slashed*: it loses part of its bond, the stake it locked to mine. Everything settles in a fixed-supply, zero-inflation token. Consensus is CometBFT, with validators distinct from miners; the novelty is the *useful-work market* layered on top.

The optimistic path (ADR-025/026/028) is armed from block 1 of the launch genesis, and the chain's own test suite exercises its slash, refund and unwind paths.

---

## 1. The problem

Two trends are unserved by existing chains:

1. **Wasteful security.** Proof-of-Work spends gigawatts on hashes whose only value is difficulty. The compute does nothing else.
2. **Centralized, opaque AI.** Inference is concentrated in a few clouds; users must trust both the operator's honesty *and* its handling of their data.

Dendra addresses both: the work the network pays for is **inference people actually want**, made **checkable** by sampled audits with stake at risk, on hardware people already own.

---

## 2. Architecture

```
Client ──prompt over HTTPS──► Gateway (OpenAI-compatible): reads it, runs the content filter
                                 │  opens the job on the Dendra chain (x/jobs): escrow fee
                                 │  validator VRF seed → stake-weighted present miner
                                 │  then seals the prompt to that miner's on-chain key
                                 ▼
                    Assigned miner (Ollama, its own GPU or CPU) ◄── sealed prompt, via the relay
                                 │  decrypts in RAM, answers
                                 │  anchors a commit: prompt hash + answer embedding, never the text
                                 │  payment retained, paid at finality
                                 ▼
                    VRF-sampled subset → fresh committee + LLM-judge → slash cheats
Client ◄── answer over HTTPS ◄── Gateway ◄── sealed answer, via the relay ◄── miner
```

*(The diagram shows the **optimistic** model — ADR-025/026/028 — **armed from block 1 in the launch genesis**. The relay carries ciphertext only; who can read a prompt is set out in §5.)*

- **Validators and miners:** validators order and finalise blocks with CometBFT consensus. Miners answer and judge requests; they never validate — **miners ≠ validators**, so GPU possession never secures consensus. The block interval emerges from `timeout_commit` and propagation rather than from a protocol constant, so this page counts durations in blocks.
- **Randomness:** random draws decide which miner serves a job, whether its answer is audited, and who sits on its jury. Each validator attaches to its block vote (a *vote extension*) the output of a verifiable random function (VRF), randomness anyone can check; the outputs are aggregated, with the VRF input bound to the block hash (anti-precompute). Under `committee_seed_source` = 1, a block's seed counts for job assignment and audit draws only when at least `committee_min_vrf_contributors` validators contributed to it, together carrying two thirds of the voting power that committed the block; §7 says what a draw does when its seed does not count. A jury that re-hears a dispute against a settled job uses that seed when it counts, and otherwise the hash of the block in which the dispute opens (`redo_committee.go::anchorRedoCommittee`). Read the validators with `curl -s http://testnet-api.dendranetwork.com:26657/validators` and the floor with `dendrad query jobs params -o json`.
- **Gateway:** an OpenAI-compatible endpoint (`/v1/chat/completions`); any existing client (e.g. Open WebUI) uses Dendra unchanged.
- **On-chain module `x/jobs`:** escrow, committee assignment, commit anchoring, settlement, slashing, pools.
- **Off-chain:** miners, set up by the installer — `deploy/install.sh` on Linux, `deploy/install.ps1` on Windows, each downloaded, read, then run with `--yes` (`-Yes` on Windows) rather than piped from `curl` into a shell — or by `deploy/join.sh`; the desktop application *Dendra* (`deploy/app`) then shows and drives the miner (health, start, stop) and shows the recovery phrase of each new key until three of its words are typed back; an encrypted relay bus; and a Prometheus/Grafana supervision stack.

---

## 3. Job lifecycle

1. **Open + escrow.** The gateway opens a job and the chain locks the fee in a module account. The gateway prices per token — `fee = base + per_token × (in + out)` — with a two-phase escrow that settles on the *effective* output.
2. **Assignment.** The chain draws one stake-weighted **primary** miner among **present** miners — those that proved availability in, or registered during, the current or the previous window (ADR-048). The draw is seeded by the validators' **ECVRF** beacon, bound to the block hash, so the requester cannot grind the job id to choose a complicit miner (anti-grinding). Miners rank by `hash(seed | jobId | minerId)`, **weighted by stake**, each weight capped at `assignment_stake_cap_multiple` × `min_stake` (`committee.go::capAssignmentWeights`): below that ceiling a miner's weight is its stake, and splitting the stake does not raise its share of the work; above it, splitting into identities that each still reach the ceiling does, while a split into identities below the ceiling can lower it; and at a multiple of 1 every miner bonded at `min_stake` weighs the same. Governance moves the multiple: read it on the chain (`GET /dendra/jobs/v1/params`) rather than here.
3. **Inference.** The miner decrypts the prompt in RAM, runs its model, returns the answer sealed to the gateway through the relay, and anchors a *commit* on chain: a salted hash of the prompt and an *embedding* of the answer — a list of numbers that summarises its meaning, publicly readable. The text itself is never written on chain.
4. **Settlement.** The chain records the job as answered. Under the launch genesis (`hold_bps` = 10000) the miner's net share and the protocol's cut stay held until the job's audit checkpoint, where they are released, judged or refunded (§4, §6 bis).

Settlement is **replay-safe**: a single shared predicate marks a job paid across every settlement path, so it is paid at most once.

The chain binary also carries a **redundant-committee** mode, its compiled default (`verification_mode` = 0): three miners answer each job and their anchored embeddings are clustered by an **integer cosine** computed in `big.Int` — no floating point, hence reproducible across validators. The launch genesis selects the optimistic mode (`verification_mode` = 1), in which a jury running a language-model judge grades the answer. Read the mode with `dendrad query jobs params -o json`.

---

## 4. Verification — optimistic, sampled, judged

A language model rarely answers twice in the same words, so answers cannot be compared byte for byte. Dendra pays optimistically and re-checks a random sample: settle fast, audit some, slash what a jury convicts (ADR-025/026/028).

1. **One miner, paid at finality.** The primary answers once, so a job costs one inference instead of the three a three-miner committee spends, and its latency is a single inference, which opens the door to streaming and large models. Its payment is retained at settlement and resolved at the audit checkpoint: a job the lottery skips is final there and pays the miner and the cut; a job the lottery takes waits for a verdict (§6 bis).
2. **VRF-sampled audit.** After the commit, at each block the VRF seed decides — via `H(seed ‖ jobId) mod 10000 < audit_sample_bps` — whether a job is audited (`bps` = basis points, hundredths of a percent). The seed is posted *after* the commit, so the miner cannot know in advance whether it will be checked. `audit_sample_bps` is a **governable** on-chain parameter: read it with `dendrad query jobs params -o json` (or `/dendra/jobs/v1/params`).
3. **Fresh jury + LLM-as-judge.** On an audited job, the primary **reveals** its answer to a fresh jury, drawn on chain by stake from the VRF seed among present, eligible miners in the pool frozen when the job opened (ADR-037), excluding the miner under audit, and anchored before anyone votes. The draw targets `audit_committee_draw_size` seats (0, or absent, selects the compiled 15). Each juror runs an **LLM-as-judge** and commits a **binary verdict** (`"1"` valid / `"0"` invalid); only a node started with `--judge` posts one. The kit runs the judge model the chain names (`audit_judge_model`), a mixture-of-experts, on the CPU, unless its operator overrides it with `DENDRA_JUDGE_MODEL`, and its hardware probe (`deploy/hw_probe.sh`) grants the judge role only to a machine with the system RAM that model needs and only to a model on its validated list. One shared model makes judge errors correlated; judge diversity on chain is listed as coming in §8. Against that correlation, from kit v0.2.1 the kit's judge posts an abstention, not an invalid verdict, when all it finds is a divergence between the answer and its own reference answers, until the project lifts that guard after a dated measurement; an answer that is not a coherent attempt at the request is still voted invalid. While the guard holds, a cheat whose answer stays coherent is not convicted by kit judges: its retained fee unwinds to the client, unless the jury upholds the answer — a kit judge votes valid when its reference answers agree with the answer, or when it proves the request ambiguous — and an upheld answer is paid (ADR-057).
4. **Two locks to convict (ADR-028).** A slash needs **two independent locks, both priced in capital**: "invalid" verdicts from at least **two thirds of the anchored seats** *and* a **strict majority of the voting stake**, with at least `audit_min_quorum` voters, in both code paths. The convicted primary loses `slash_leak_bps` of its stake (80 % in the launch genesis) and the retained payment is **refunded to the client**. The same bar with "valid" verdicts upholds the answer, and the miner is paid. No slash is applied below the verdict bar, and a concluded verdict is final: any remedy goes through governance.
5. **A missing reveal.** The reference juror software abstains when a reveal is missing — an absent reveal cannot be told apart from a relay outage — and a job with no verdict unwinds to its client after `audit_unwind_blocks` (ADR-044).
6. **The jury floor.** A verdict needs `audit_min_quorum` voters (0, or absent from the output, selects the compiled floor, `antievasion.go::effectiveSlashFloor`), and the jury excludes the miner under audit, so a jury takes `audit_min_quorum` + 1 present, eligible miners. A drawn job with no verdict within `audit_unwind_blocks` — no jury seated, or none concluding — is refunded in full to its client, the protocol's cut and deferred burn included; nobody is slashed, and a drawn job is never paid unverified.
7. **Nash-sized deterrence.** Where a jury can conclude, cheating is loss-making whenever `s·P > (1−s)·g` (audit rate `s`, slash `P`, cheat gain `g`); opening a job refuses any fee that would break the inequality at `min_stake`, taking the fee as the cheat's gain (`msg_server_open_job.go`).

The elegance: auditing only a *fraction* of jobs makes a **costly LLM-judge affordable** — a budget an always-on three-miner committee could never fund — so detection quality rises *and* total cost falls at the same time. Deterrence does not need every job checked: it needs cheating to lose money on average.

---

## 5. Who can read a prompt

On the public chat and API path:

- **the Dendra gateway** — it receives the prompt over HTTPS, runs the content filter, then seals (encrypts) it to the serving miner's on-chain key (X25519 ECDH + AES-256-GCM); it also decrypts the answer it returns to you over HTTPS;
- **not the relay** — it carries ciphertext only;
- **the serving miner** — it decrypts the prompt in memory to answer;
- **the jurors, if the job is drawn for audit** — the miner re-seals the prompt and the answer to each of them, and they read both to judge. From kit v0.2.1 the miner seals this reveal for the jurors the chain anchored for that audit and for no other miner; a miner running a kit of an earlier release seals it for every other registered miner with an encryption key (ADR-057, decision 4). Each copy is sealed to the encryption key the juror registered on chain; for a juror that registered none, to the key the relay serves for it, which a relay that replaces that key can open;
- **not the chain** — it stores a salted hash of the prompt and an embedding of the answer (a list of numbers that summarises its meaning, publicly readable), never the text.

Running the client or the gateway on your own machine keeps the plaintext off the Dendra gateway; the serving miner, and the jurors if the job is drawn, still read it. While the model answers, the miner software keeps the prompt out of crash dumps and blocks writing it to disk or sending it anywhere but the model, where the host allows it. The gateway's code is open source, and its content filter matches fixed text patterns. Do not put secrets in a prompt.

---

## 6. Tokenomics — $DNDR

A **fixed-supply utility token**: the medium for paying for inference and rewarding miners.

| Property | Value |
|---|---|
| Max supply | **10,000,000 DNDR** (hard cap, **zero inflation, zero mint**) |
| Base unit | `udndr` — 1 DNDR = 1,000,000 udndr |
| Genesis allocation | Community 33.82% · Reserve 33% · Validator delegation pocket 20% · Validator 7% · Team 5% · Faucet float 1% · Final Testnet Season payout 0.15% · Request generator 0.03% — 10,000,000 DNDR exactly, readable from the published genesis |
| Emission | Release of the pre-allocated **Reserve** only — a geometric, decreasing share of what remains. **No minting, at any rate.** |
| Release rate | A **governable on-chain parameter** (`reserve_release_bps` per `epoch_blocks`), distinct from the binary's compiled defaults. The launch genesis ships `reserve_release_bps = 2` per **86,400-block epoch** — a **long-lifetime** calibration whose half-life is **6,932 epochs** on an idle chain and **3,466 epochs** under saturated demand, leaving 93–96 % of the Reserve after 365 epochs. Those figures are stated in **epochs**, the unit the chain counts, since an epoch's length in time depends on the block interval (it emerges from `timeout_commit` and propagation, and is measurable at any time from two block headers). Read the live value with `dendrad query emission params -o json`. **This is the drain rate of a pool, not a yield**: it says how fast the Reserve empties, not what a participant receives. |
| Emission flows | work (demand-gated 1.5×) · availability (paid from its pool only when `avail_payout_bps` > 0; the launch genesis sets it to 0, so availability windows are measured, neither paid nor slashed) · security |
| Burn | 5% of fees (`fee_burn_bps = 500`), deferred until the job is final and returned to the client on a refund |
| Protocol cut | 15% of a job (split: validators 50% / team 20% / treasury 30%), retained per job and paid at its finality |

### 6 bis. Who gets paid, for what, and out of which pocket

The protocol **pays for work**, from block 1 of the launch genesis. Two pockets fund every flow, and neither is inflation: the **client fee** escrowed at job opening, and the **pre-allocated Reserve** released in decreasing slices (`x/emission/keeper/epoch.go`). There is no third pocket — `x/mint` is absent from the binary, so no reward path can mint (`chain/app/fixed_supply_test.go`).

| Role | What it is paid | Implemented in |
|---|---|---|
| **Miner** | The job fee less burn and protocol cut, released at the job's finality; plus a **work subsidy** capped at `work_gate_bps × demand`, credited as a right at finality and claimed with `MsgClaimSubsidy` (the miner software claims it by itself); plus **availability**, pro rata to bond, only when `avail_payout_bps` > 0 (0 in the launch genesis) | `x/jobs/keeper/msg_server_settle_semantic.go` · `msg_server_claim_subsidy.go` · `availability.go` |
| **Validator** | Transaction fees; a share of the protocol cut (`validator_reward_bps`), retained per job and sent at its finality to `fee_collector`; and the epoch **security** slice, routed to `fee_collector` and on to validators and delegators through `x/distribution` — the only emission flow that leaves the module account | `x/emission/keeper/epoch.go` |
| **Whistleblower** | On an upheld dispute: the `dispute_bond` back **plus** a reward, bounded by the remaining Treasury. On an unfounded one, the bond is forfeited to the Treasury | `x/jobs/keeper/msg_server_adjudicate.go` |

The split above says how a client's fee divides, not what any participant receives: that depends on real traffic and on what remains in the Reserve, and every quantity above is a governable on-chain parameter. The Final Testnet Season publishes its own capped formula (§6 ter, ADR-047). Read the live values with `dendrad query jobs params -o json` and `dendrad query emission params -o json`.

**Escrow, as a property.** `hold_bps` = 10000 in the launch genesis, so at settlement the miner's net share **and** the protocol's cut are **retained** under the job's id, next to the deferred burn, and resolved at the job's audit checkpoint: the draw that follows its settlement. Per 100 paid by a client, 80 go to the miner, 7.5 to validators, 3 to the team, 4.5 to the treasury, and 5 are burned. A job the audit lottery does not select is **final** there: the miner's share is released (`antievasion.go::releaseHeld`), the burn executed, the cut paid — the validators' share to `fee_collector`, the team's to the governed `team_address`, the treasury's into its pool (`protocol_cut.go::payRetainedCut`) — and the work subsidy right credited. The selected share is `audit_sample_bps`, a governable figure read from the chain rather than from this page. A selected job is paid the same way on a verdict that upholds the miner's work; on a conviction the miner is slashed and the client refunded; and if no jury concludes within `audit_unwind_blocks` blocks, the whole fee — cut and deferred burn included — goes back to the client, the miner neither paid nor penalised (`audit_unwind.go::unwindStrandedRetention`). A settled job whose audit draw waits for a seed unwinds the same way (ADR-048). No selected job is paid unverified, and nothing is held for ever; an unselected job is paid without a verdict by design, and the deterrent is that the miner cannot know in advance whether the draw will select it.

Held means **retained until the audit resolves or `audit_unwind_blocks` elapses**, whichever comes first. `dendrad query jobs held-summary -o json` reports the retained total and the **age of the oldest retention**, so anyone can check that bound.

**The Reserve is funded where the release path draws from it.** The 3.3 M DNDR of the Reserve are credited in the genesis to the emission **module account**, the account the release path draws from, and the node **refuses to boot** on a Reserve counter that no module balance backs. Both halves are queryable: `dendrad query auth module-account emission` gives the address, `dendrad query bank balances <that address>` gives what it holds, and the genesis `app_state.bank.balances` says what was credited. Emission releases at epoch boundaries, while each job's payments follow its own lifecycle, above.

**Retention and exit.** A held payment counts as an open obligation: a miner with an open retention can neither leave nor be removed. Each retention ends — released, judged, or unwound to its client — within about `audit_unwind_blocks` of settlement, so the bond is free once the miner stops taking work and that bound has passed. Plan the bond for that window.

**Key properties.**
- **No protocol minting.** All rewards come from releasing the pre-allocated Reserve; the chain does not mint new tokens for them. The protocol's **custom modules hold no `Minter` permission**, and the standard `x/mint` module is **removed from the chain binary entirely** — there is no minting module to configure, so fixed supply holds **by construction**, verified at runtime: a fresh genesis boots to a total supply of **exactly 10,000,000 DNDR** in a single denom. De-registering the module removes the lever that `inflation=0` genesis parameters would have left for a governance proposal to pull.
- **Demand-gated work flow.** The work subsidy is bounded at `1.5×` a demand counter fed by the team and treasury shares of each paid job's fee. Self-dealing by an outside washer — a miner paying its own jobs through a separate address — is kept **economically -EV**: what it cannot recover (the team and treasury shares, plus the burn) exceeds the subsidy it unlocks, and parameter validation enforces that inequality, counting the validators' share as recoverable by a staking washer (`chain/x/jobs/types/params_invariant_test.go`).
- **Real skin in the game.** Miner bonds are escrowed real coins; slashing destroys real value; the 5% burn really reduces supply.
- **Validated (supply).** The bound is *structural*, not statistical: emission only releases the pre-allocated Reserve, and no code path can create a coin. `chain/app/fixed_supply_test.go` locks three independent facts — no minting module is wired into the app, the exact per-module permission set is pinned (only IBC `transfer` carries `Minter`, and only for foreign denominations, never native `udndr`), and both committed genesis files total exactly 10,000,000 DNDR.
- **Long-run security.** A fixed supply means a finite security budget, released on a schedule anyone can read. Under the **launch genesis** release setting the Reserve has a half-life of **3,466 to 6,932 epochs** depending on demand, and the rate is a governable parameter. Validators earn transaction fees, a share of each paid job's protocol cut and the Reserve's security slice; as the Reserve drains, fees and the cut carry the security budget.

### 6 ter. A fresh start, and the Final Testnet Season

**A fresh start.** `dendra-testnet` starts from a fresh genesis: no balance, registration, job or history moves over from an earlier chain. The chain is resettable, and a reset erases balances; the season's published rankings are archived before any reset.

**The Final Testnet Season (ADR-047)** is a reward programme run on top of the chain, with no registration and no test: anyone running a miner takes part. It ends on 7 November 2026 at 23:59 UTC: it counts every block timestamped before 8 November 2026, 00:00 UTC — the chain's own block times decide. Season days are 17 280 blocks each, counted from the season's first block; the last one is cut at the end, and the season's last ranking names its last block (`inputs.season_end_height`). A request counts on the block that settles it, so the programme opens none in the last ten minutes; after the end, the programme service refuses new answers and payout-address declarations. It pays per miner identity and per season day, every seven season days (the last payment covers the days left after the last full week, once the last day is final), in DNDR, by a formula published in advance and applied to what the chain records and to the programme's published evidence (the jobs whose answer it received, and the grades):

- **Work** — 0.05 DNDR per verified programme request, the same rate for every identity. A request counts once it is settled on chain, past its audit, and not refunded, and its answer reached the programme: the generator forwards each answer to the programme service before it settles the job, and the day's seal lists the jobs answered (`final_season_facts.py::answered_jobs`). From the season day the season service names in `unwound_audit_work_from_day`, a programme request refunded because no audit of it concluded in time counts too, when its answer reached the programme and a grading clears it: its own grade first, otherwise its miner's grades that day; with no grade at all it is not paid (ADR-047, decision 18; `final_season_facts.py::unwound_work`). The programme sends a fixed number of requests a day (`requests_per_day`), which the chain hands out among its **present** miners. Each identity's stake weighs in the draw up to a ceiling, `assignment_stake_cap_multiple` × `min_stake` (`chain/x/jobs/keeper/committee.go::capAssignmentWeights`): below the ceiling an identity's share of the work follows its stake and splitting the stake does not raise it; above it, splitting into identities that each still reach the ceiling does, while a split into identities below the ceiling can lower it; at a multiple of 1 every identity bonded at `min_stake` weighs the same, a larger stake draws no larger share, and splitting a stake into more identities bonded at `min_stake` draws a larger share of the work. Governance moves the multiple, so which case applies is a reading of the chain: `GET /dendra/jobs/v1/params`, or `dendrad query jobs params -o json` → `assignment_stake_cap_multiple`, `min_stake` (a multiple of 0, or absent from the output, selects the compiled default of 2; a `min_stake` of 0, or absent, sets no ceiling).
- **Juror** — 0.008 DNDR per verdict consistent with the outcome, for drawn jurors only, on audits of the programme's own requests.
- **Presence** — 0.002 DNDR per availability window proven on chain (`MsgProveAvailability`, read from the transaction index), at most 50 a day (at most 0.1 DNDR a day), paid **only on a day the identity has at least one verified request**.

Daily reward = 0.05 × verified requests + 0.008 × verdicts + (0.002 × min(windows, 50) if verified requests ≥ 1, else 0), in whole udndr (`final_season_calc.py::gross_of`). There is no public-node reward. After each day the programme draws at most three answers to programme requests per identity, and a language model grades whether each is a coherent attempt; an identity whose graded answers of a day are all incoherent, with at least two grades (one when a single answer was sampled), loses that day's work reward, and with it its presence reward. Caps apply to the programme as a whole: 50 DNDR a day, every payable of a day being reduced by the same ratio beyond it, and 1 500 DNDR over the season; the work an identity gets is its share of the chain's work draw over a fixed daily volume. Rewards go to the address an identity declares, else to the operator that signed its availability proofs that day, else to its operator in the miner registry; with no address known, its payable is 0. Each day is ranked once final and, if it received answers, once they are sealed, and published with its evidence, and anyone can recompute it with `final_season_rank.py`. Payments come from a payout account funded once at genesis from the community pocket; the programme lives off chain, and nothing in it changes consensus.

The rewards the season actually pays, after the day's pro rata, count as **points**, and points convert **one for one**: each testnet DNDR the season paid becomes one mainnet DNDR, credited in the mainnet genesis to the address that received it (ADR-049), so at most 1 500 mainnet DNDR in all. **Mainnet launches after the Final Testnet Season.** Faucet grants, transfers, on-chain job pay and the work subsidy are not points. Rules: [dendranetwork.com/final-season](https://dendranetwork.com/final-season/).

---

## 7. Security model

| Property | Mechanism |
|---|---|
| Block order and finality | CometBFT consensus among the validators (§2). |
| Randomness of draws | Validators' VRF outputs, attached to their block votes (vote extensions) and aggregated, with the VRF input bound to the block hash. A block's seed counts only with at least `committee_min_vrf_contributors` contributors carrying two thirds of the committing power (`decentralized_seed.go::committeeBaseSeedSourced`). Under `committee_seed_source` = 1 and the deferred reveal that optimistic mode uses (`committee_reveal_delay` > 0), a block whose seed does not count assigns no job and draws no sampled audit; both wait for a later block, and a retention still waiting after `audit_unwind_blocks` unwinds to its client. A jury that re-hears a dispute against a settled job is drawn from the hash of the block in which the dispute opens when the seed does not count (`redo_committee.go::anchorRedoCommittee`). The contributor count is exposed on chain (`committee-seed-health`). |
| Answer checking | A sampled audit, concluded by a jury at the two-lock bar of §4 — two thirds of the anchored seats and a strict majority of the voting stake — with the miner's stake at risk. Splitting a stake across identities adds nothing to the stake lock, which sums the voters' stake, but it does buy seats: the jury draw gives each identity at most one seat, weighs each candidate by its stake, and seats every eligible miner when they number no more than the seats (`audit_committee.go::drawMembersWithDomain`). The seat lock and the `audit_min_quorum` voter floor count identities, so several identities can hold several seats on the same audit. |
| Jury pool | Frozen at the job's opening height: an identity registered later cannot be drawn for that job. |
| Prompt confidentiality | Encryption from the gateway to the miner's on-chain key, re-sealed by the miner to each juror when the job is drawn for audit (from kit v0.2.1, to the anchored jurors only; a miner on a kit of an earlier release seals it for every other registered miner); while the model answers, the miner software keeps the prompt out of crash dumps and blocks writing it to disk or sending it anywhere but the model, where the host allows it; a hash of the prompt and an embedding of the answer on chain, not their text (§5). |
| Settlement | A job settles at most once, through a single shared predicate across every settlement path; anchored commits are immutable. |
| Parameter changes | A governance vote, weighted by bonded stake. |
| Supply | `x/mint` absent from the binary; a fresh genesis boots to exactly 10,000,000 DNDR in a single denom (`chain/app/fixed_supply_test.go`). |
| Code | Open source; the chain's test suite covers the slash, refund and unwind paths. |

---

## 8. Status & roadmap

- **Built — the protocol.** Escrow; stake-weighted draws from a real RFC-9381 ECVRF, verified on chain for availability proofs and committee seeding; on-chain miner-key anchoring with key rotation; optimistic verification with a language-model judge; settlement that pays a job at most once; Reserve release, stakes, burn and slashing — all covered by the chain's test suite. A bench also ran real inference from request to settled payment, with real balances.
- **Public testnet — `dendra-testnet` and the Final Testnet Season.** Blocks from a published, verifiable genesis, a queryable API at `testnet-api.dendranetwork.com`, and a deployment kit anyone can run (§2). Read every parameter with `dendrad query jobs params -o json` or `/dendra/jobs/v1/params`.
- **Next — mainnet**, which launches after the Final Testnet Season (§6 ter).
- **Roadmap — by status.** Integrated: held fees and random audits, AI juries and slashing, encryption to the miner, availability proofs, validator joining, an OpenAI-compatible API and chat, a model sized to the miner's GPU, judging on the CPU, the miner app and installers, a fixed supply. Ready to switch on: availability rewards, the attestation gate, the model registry, randomness from several validators. In development: a confidential MPC mode. Coming: judge diversity on chain, verification by recomputation, image verification (verified effort), multi-GPU rigs, attestation on chain, per-request model routing, a data-center enclave tier, a signed Windows miner app, and more roles and technologies for CPU-only machines. The full board: [dendranetwork.com/roadmap](https://dendranetwork.com/roadmap/).

---

## Disclaimer

Dendra is open-source software running on a **public testnet**, `dendra-testnet`. **$DNDR is a utility token** used inside the protocol to pay for inference and reward miners; it is **not** a security or an investment, there is **no token sale, presale, or ICO**, and nothing in this document is financial, legal, or investment advice. The network documented here is a public testnet: its DNDR is **never sold**, the Final Testnet Season pays rewards in it by a published formula, and those rewards count as points that convert one for one into mainnet DNDR, credited in the mainnet genesis to the address that received them, at most 1 500 mainnet DNDR in all; mainnet launches after the Final Testnet Season. The chain is resettable: a reset erases balances; the season's published rankings are archived before any reset. Beyond that published formula of the Final Testnet Season, no yield, return, or price outcome is offered or implied. The gateway applies a regex prefilter with known false negatives; it is not a guarantee that unlawful content is blocked, and users are solely responsible for submitted content. The software is provided "as is", under the Apache-2.0 licence. Read the source and do your own research.
