# Dendra ($DNDR)

**Confidential, verifiable AI inference — paid on consumer GPUs, settled on a sovereign L1.**

> **Status: research / devnet.** Nothing here is production. `$DNDR` is a **utility token** used for staking, fees and rewards — **there is no token sale, and the testnet token has no monetary value** (the network is resettable).
>
> Dendra does **not** claim to match frontier models on answer quality; it offers **privacy + on-chain verifiability + censorship-minimal (best-effort, not a compliance guarantee)** inference on consumer hardware. No financial promise is made anywhere in this repository.

---

## What Dendra is

*(Protocol. What the code does when miners and a decentralized seed are present — see [What is actually running today](#what-is-actually-running-today) for what is present today.)*

A client sends an **end-to-end encrypted** prompt; a miner runs the inference **on its own consumer GPU** without the plaintext ever leaking on-chain or to the relay; the work is **settled on a sovereign Cosmos chain** (payment, vesting, anti-Sybil) and **verified economically** — a dishonest miner is **slashed**. The content never touches the chain (only hashes, embeddings, verdicts and counters do).

The "useful work" of the network is **inference**, not hashing.

## How it works (one paragraph)

*(Protocol.)*

`Chat client → OpenAI-compatible gateway → chain (escrow → VRF beacon → stake-weighted committee → confidential GPU inference → on-chain settlement) → answer.` Verification is **optimistic**: one primary miner is paid (k=1), a fresh committee audits a VRF-sampled fraction of jobs, and a proven cheat is **hard-slashed** (cheating is negative-EV — a Nash equilibrium). The audit judge is an **LLM-as-judge** (the embedding cosine is dead as a correctness judge — it accepts both word-salads and fluent false facts). The audit committee is **drawn and anchored on-chain** at sampling time, stake-weighted, and **only its summoned members can vote**. A hard slash requires **at least two thirds of the anchored seats** to return "invalid", so a minority verdict cannot slash an honest miner. With no anchored committee, no hard slash fires at all (fail-closed). The launch genesis **arms** this optimistic mode; the conservative redundant **k=3** mode remains available as a fallback.

Those three properties are stated as **properties, not as prose**: they are pinned by a dedicated test suite, [`chain/x/jobs/keeper/readme_public_claims_test.go`](chain/x/jobs/keeper/readme_public_claims_test.go), which exercises the anchored-committee rule, the two-thirds bar and the zero-seat (fail-closed) case under the **default** parameters — not under a launch-kit setting.

---

## What is actually running today

The two sections above describe the **protocol**: what the code does once the conditions it needs are
met. This section describes the **network**: what is actually running. The two are not the same, and
what separates them is operators, not code.

**What is open today is a public devnet endpoint**: it produces blocks, answers a queryable API, and
publishes a genesis anyone can verify against this source tree. It serves inference but does **not**
currently **verify** it: the audit draw is deferred below its contributor floor, so no job is checked
and no miner payment is released.

**It does pay for work.** The settlement path is armed from block 1: a miner that registers and serves
a job is paid in `$DNDR` by the protocol, out of the client fee and the pre-allocated Reserve — the
mechanism, role by role, is in [What the protocol pays](#what-the-protocol-pays). What does **not**
read "paid" carefully on this
network: the protocol **distributes and records a debt**, it does not yet pay the miner. Validators,
treasury and the dev share receive coins at settlement; the miner's entire net share is **held** —
retained, not cancelled — and cannot be released while the audit draw is below its contributor floor.

**Contribution is recorded, and nothing is promised.** Every job, settlement, verdict and slash is
written into the block that carries it, and the project publishes state snapshots at named block
heights so that record survives a reset. If a mainnet is launched, the project intends that record to
count among the inputs to how an allocation is decided. It does **not** promise that a mainnet will
exist, that any distribution will happen, or any amount, rate, ratio, date, eligibility or reservation
— and arriving early carries no advantage of its own. There is no enrolment, no participant list and
nothing to claim: being recorded is a property of the chain, not an account opened in your name.
[`services/points_indexer.py`](services/points_indexer.py) computes a provisional view of that record
from public state; its formula is not final and can change or be withdrawn, and its internal
"Season 0" title is a legacy name being retired — it implies a schedule and a successor, and neither
exists. The token itself has **no monetary value**, is not sold, and
the chain can be reset without notice — any balance can vanish.

| Fact | Verify it yourself |
|---|---|
| **Supply starts at exactly 10,000,000 DNDR and can only fall.** `x/mint` is not in the binary, so no code path increases it; the soft burn on fees is the only thing that moves the total, and it moves it **down**. So the check is an **inequality, never an equality**: one denom, `0 < supply ≤ 10000000000000 udndr`, strictly below the genesis figure once any fee has burned, and never larger between two reads. A number written here would be stale by the next block — read it. | `curl -s http://api.dendranetwork.com:1317/cosmos/bank/v1beta1/supply` — expect exactly one entry, `denom: udndr`. Compare its `amount` against the genesis total `10000000000000` (in `http://api.dendranetwork.com:8088/genesis.json`, `app_state.bank.supply`): it must be **smaller**, and the gap is the burn since genesis. Read it twice: it must never have grown. |
| **The genesis served is the genesis announced.** Its SHA-256 is published beside it, so a joiner can refuse a substituted file. | `curl -s -o g.json http://api.dendranetwork.com:8088/genesis.json && sha256sum g.json` — compare with `GENESIS_SHA256` in `http://api.dendranetwork.com:8088/network-info.txt` |
| **Fault tolerance is zero, and a registry count is not a validator set.** Outside operators have bonded here, so the staking registry lists more validators than are actually securing the chain: a validator that is **jailed** sits in the registry with its stake, produces no blocks and contributes nothing to the randomness beacon. Only the **active** set matters, and losing it stops the chain. This is not a hypothetical on this network — validators have been jailed for downtime and lost stake for it, which is why [`deploy/README.md`](deploy/README.md#becoming-a-validator-read-this-first) states the cost of the role before you bond. | Active set: `curl -s http://api.dendranetwork.com:26657/validators` — read `result.total` and compare the `voting_power` values. Registry: `curl -s http://api.dendranetwork.com:1317/cosmos/staking/v1beta1/validators` — read `status` and `jailed` on each. **The two numbers differ, and the difference is the point**; anything not `BOND_STATUS_BONDED` with `jailed: false` is securing nothing. |
| **A draw needs a decentralized seed, and the seed is a per-block rate — not a switch that stays on.** The genesis sets `committee_seed_source=1`, so a draw happens only on a block where at least `committee_min_vrf_contributors` validators carried a VRF vote-extension ([`chain/x/jobs/keeper/audit_sampling.go`](chain/x/jobs/keeper/audit_sampling.go), the `committee_seed_source == 1` branch). Contributors are counted **per block, not once and for all**, so this reads as a rate: a validator on a home connection contributes only on the blocks its extension reaches the proposer, and a validator that gets **jailed contributes nothing at all**. Audits opened during past windows sit on the deferred backlog. Deferral is **fail-closed** throughout: no payment released on a selected job, no slash fired. | `dendrad query jobs committee-seed-health -o json` — compare `latest_contributors` against `committee_min_vrf_contributors`; a draw runs only when the first reaches the second, **for that block**. `dendrad query jobs audit-deferred -o json` — `job_ids` is the deferred backlog (`deferred_no_jury`), `audits_opened` counts audits ever opened. No `dendrad` yet? The same figures are served over HTTP: `curl -s https://proof.dendranetwork.com/proof` |
| **Miners are registered and have served work**, so inference happens — but none of it is verified, and no miner is paid. The bar is a **rule, not a headcount**: the jury is every registered miner *except* the primary under audit, and a verdict needs `audit_min_quorum` jurors to vote, so an audit can only conclude once **`audit_min_quorum` + 1 miners are registered**. Below that an audit opens and never resolves, and the draw defers with `deferred_no_jury` — the same backlog as the row above. Read both numbers rather than trusting a count printed here. | `dendrad query jobs list-miner -o json` — `pagination.total` is the registered count · `dendrad query jobs params -o json` — read `audit_min_quorum` and do the addition yourself · `dendrad query jobs held-summary -o json` shows what is retained and how long it has been. Over plain HTTP: `curl -s http://api.dendranetwork.com:1317/dendra/jobs/v1/miner` and `.../dendra/jobs/v1/params` |
| ⚠️ **A miner with a retained fee cannot exit and cannot be evicted.** A held payment counts as an open obligation, and the same guard that refuses a voluntary exit also blocks eviction ([`chain/x/jobs/keeper/msg_server_miner.go`](chain/x/jobs/keeper/msg_server_miner.go), the open-obligations branch). Its bond stays locked until audits can conclude, which needs enough miners for a jury. This is fail-closed working as designed, and it is stated here because it binds capital: **do not stake more than you are willing to leave in place**, and note that a reset erases both the bond and the debt. | `dendrad query jobs held-summary -o json` |
| **The token has no monetary value** and the chain can be reset without notice. | — |

**These queries return empty, and empty is the answer.** At launch `audit-deferred` returns `{}` and
`list-miner` an object with no entries. That is the expected reading of "nothing has happened yet", not
a broken endpoint: protobuf omits fields at their zero value, so an absent field and a field at zero are
indistinguishable in the JSON. A field that is missing here means zero — it does not mean the query
failed. A query that actually fails returns an error, and that is a different thing.

**This state clears itself, and it is meant to.** The deferral is a threshold being unmet, not a
failure. Two distinct thresholds have to be crossed, and a second validator only crosses the first:

1. **Enough VRF contributors on the same block.** `register-validator-vrf-key` from a bonded validator
   makes it a contributor, and the `committee_min_vrf_contributors` floor has to be met **on the block
   the draw is attempted** — it is a rate, not a latch. Two things un-cross it after it has been
   crossed: a home connection whose vote-extension misses the proposer on most blocks, and **jail**,
   which stops the contribution outright. `committee-seed-health` prints the count for the block it was
   asked about, so it is the only honest way to read this — compare `latest_contributors` against
   `committee_min_vrf_contributors` yourself.
2. **`audit_min_quorum` + 1 registered miners.** The jury is every registered miner except the primary,
   so a *committee* exists from two miners onward; what a committee needs in order to **conclude** is
   different and stricter. Both outcomes are gated on the quorum: releasing a held fee requires
   `voters >= audit_min_quorum` and so does a slash
   ([`chain/x/jobs/keeper/antievasion.go`](chain/x/jobs/keeper/antievasion.go),
   `auditVindicateDecision` and `auditSlashDecision`). That many jurors, plus the primary they are
   judging, is the registered-miner floor — read `audit_min_quorum` from
   `dendrad query jobs params -o json` and add one, rather than trusting a figure typed here. Below that
   floor an audit opens and simply never resolves, and the draw defers with `deferred_no_jury`.
   ⚠️ **And registered is not the same as voting**: only a node started with `--judge` posts a verdict,
   so a network at the floor made of silent miners still returns zero votes.

Both are required before a *selected* job's held fee settles. Neither needs a genesis change or a
redeployment. And "held" is falsifiable rather than promised: `dendrad query jobs held-summary -o json`
returns the retained total, the number of retentions and the **age of the oldest one**, so a claim that
retention is not confiscation can be checked instead of believed.

Re-run the queries above rather than trusting this paragraph — the chain, not this README, is the
current state.

## What is proven, and where

The two halves of the loop are **not proven at the same level**, and neither was proven on the endpoint
that is open today. Three levels of evidence are used below, and each claim carries its own.

- **Inference → payment: demonstrated on a project-operated bench.** Real jobs, settled on-chain, supply decreasing by burn and never minting. **Perimeter of that measurement:** a **two-validator set, both validators operated by the project**, with CPU miners and judges hosted alongside. It demonstrates that the settlement path works end to end. It does **not** demonstrate a network of independent operators under distinct administration — nothing in this repository calls any deployment distributed or decentralized. And it is **not** the public endpoint. What separates them is that the bench hosted **several miners and judges** alongside, which is what let a jury form and an audit conclude there; the public endpoint is below that floor, so it cannot. For what the endpoint is running right now, **run the queries in the table above** — this README deliberately states no live count anywhere, because a count written into a document is wrong the day the network changes and nothing in the document notices.
- **Audit → committee verdict → slash: bench only.** The pro-honest floor holds under test, with **no honest miner penalized** across that run — but the run is a **single-machine bench**, not a public network of independent judges. Treat it as a design demonstration, not as field evidence. On the public endpoint this path has never run: draws now open, and every one of them stops for want of a jury.
- **The invariants themselves: pinned by the test suite**, independently of any deployment. Fixed supply, settle-once, bonds untouched outside a slash, the two-thirds anchored-seat bar. These hold in the binary whether or not anyone is running it.

A draw that read the **live** miner set would let an identity created after the seed becomes public grind its identifier into a committee — defeating stake-weighting, since hashing is free and stake is not. [ADR-037](docs/adr/ADR-037-gel-du-vivier-de-tirage.md) closes that by freezing the eligible set at job opening, and it is shipped: `OpenJob` writes the anchor unconditionally, both draws filter on it, and a missing anchor is an error rather than a permissive default. It was consensus-breaking, so it took a fresh genesis — the one this endpoint runs. **It is not observable from outside**: no genesis field and no route expose the anchor, so this is a claim about the code in this repository, not one a query can settle for you.

## Run it

The whole stack runs with Docker:

```bash
docker compose up -d            # chain + relay + faucet + gateway (content filter inlined) + chat UI + reverse proxy
```

**One command to join.** The config URL below is the live public network's, so these lines run as
written:

```bash
export CONFIG_URL=http://api.dendranetwork.com:8088/network-info.txt
bash deploy/join.sh              # miner (serve GPU inference)
bash deploy/join.sh --validator  # full node + guided validator bond + VRF
bash deploy/join.sh --judge      # miner + audit committee
```

Each of these roles is **paid by the protocol** for the work it does — miner, validator and judge alike,
per [What the protocol pays](#what-the-protocol-pays). It pays in testnet `$DNDR`, which has **no
monetary value**, is not sold, and can be wiped by a chain reset; **no yield, rate or amount is offered
or implied**, and the flow depends entirely on real traffic. The `--validator` path is also the one that
changes the network state described above: it anchors a VRF key, and the second such key lifts the
contributor floor that currently defers every audit draw — and with it, the retained earnings of every
miner.

- **Full guide — every role (node / miner / validator / operator):** [`deploy/README.md`](deploy/README.md).
- **Wallet:** [dendranetwork.com/wallet](https://dendranetwork.com/wallet/) — create/import, check balance, send DNDR (testnet). Keys stay in the browser. The same page is in this repository ([`wallet/web/index.html`](wallet/web/index.html)) if you would rather serve it yourself rather than trust a host.
- **Advanced / manual node & miner kits:** [`deploy/testnet-node/`](deploy/testnet-node/), [`deploy/testnet-miner/`](deploy/testnet-miner/).
- **Operator (host the network):** [`deploy/launch/`](deploy/launch/) (one-command public launch) and `deploy/testnet/publish_network.sh`.

Moderation ships a best-effort CPU **regex floor** (demonstration-grade, high false-negatives, patterns kept out-of-repo, includes a CSAM pattern), not a politeness filter. In the shipped public configuration it is the **only** content filter: the optional LLM classifier stage exists in the code but stays **off unless `DENDRA_GUARD_MODEL` is set** ([`services/content_filter.py`](services/content_filter.py)), and the public launch config leaves it unset. The stage is available, not unreachable. **This is not a claim that illegal content is filtered out — do not rely on Dendra for legal-content compliance.**

## Tokenomics (fixed supply, zero mint)

- **Fixed supply: 10,000,000 DNDR. Zero inflation, zero mint.** Custom modules have no `Minter`; the standard `x/mint` module is **removed from the chain binary entirely** — fixed supply holds **by construction** (a fresh genesis boots to exactly 10,000,000 DNDR).
- Genesis allocation: community 34% / reserve 33% / validator-treasury 27% / team 5% / faucet float 1% — 10,000,000 DNDR exactly. Read it from the genesis the endpoint actually serves (`http://api.dendranetwork.com:8088/genesis.json`), or from the script that builds it, [`docker/entrypoint-chain.sh`](docker/entrypoint-chain.sh). The committed [`chain/config.optimistic.yml`](chain/config.optimistic.yml) carries the same allocation, but it is a **local** genesis for the replayable slash demonstration: several of its verification parameters differ from the public one on purpose, and its own header says which.
- **Emission = release of the pre-allocated Reserve** (never minting), across three flows: work (demand-gated 1.5×), availability, security. Which role receives which flow, and against which file, is set out in [What the protocol pays](#what-the-protocol-pays).
- Soft burn 5% of fees (`fee_burn_bps = 500`), deferred to finality. Protocol takes 15% of a job (`protocol_fee_bps = 1500`); the miner keeps 85%.
- **Anti-bubble intent (honest):** emission should not outrun real external demand. The network exports a settlement ratio `R = on-chain settled demand ÷ emission released` as a **proxy**. This proxy is **farmable** — an operator can self-settle through a separate client address — so a high `R` is **not** proof of external traction, and a sustained low `R` is a warning signal rather than an automatic stop. Making `R` a Sybil-resistant measure of genuine external demand is open work.

## What the protocol pays

*(Protocol. Everything below is a **parameter** of the running chain, readable with
`dendrad query jobs params -o json` and `dendrad query emission params -o json`. No amount, no rate and
no yield is stated here, and none should be derived: what a participant receives depends on real
traffic and on what is left in the Reserve. The token has no monetary value.)*

The money has exactly two origins, and **neither is inflation**:

1. **The client fee**, escrowed when a job opens.
2. **The pre-allocated Reserve** — 33% of genesis, held by the `x/emission` module account from block 1
   and released in **decreasing slices** (a governable share of what *remains*, never of a fresh
   supply) every `epoch_blocks`: [`chain/x/emission/keeper/epoch.go`](chain/x/emission/keeper/epoch.go).

There is no third origin. `x/mint` is removed from the binary, so a reward cannot be minted even by
governance — pinned by [`chain/app/fixed_supply_test.go`](chain/app/fixed_supply_test.go).

**Miner** — three distinct flows:

- **Per job served.** At settlement the fee is split into soft burn (`fee_burn_bps`), protocol cut
  (`protocol_fee_bps`), and **the rest to the miner** (the `minerNet` term in
  [`msg_server_settle_semantic.go`](chain/x/jobs/keeper/msg_server_settle_semantic.go)).
- **Work subsidy**, claimed from the emission `WorkPool` and **capped by demand** at
  `work_gate_bps × miner.Demand`, so it cannot be farmed without settled traffic that actually burned
  fees ([`msg_server_claim_subsidy.go`](chain/x/jobs/keeper/msg_server_claim_subsidy.go)).
- **Availability**, paid pro rata to bond from the `AvailPool` to miners that answer the availability
  challenge ([`availability.go`](chain/x/jobs/keeper/availability.go)). This flow is parameter-gated
  by `avail_payout_bps` and `avail_epoch_blocks`, and both are **at zero** on the public endpoint today
  — read them with `dendrad query jobs params -o json` rather than assuming the flow is live.

**Validator** — transaction fees as on any Cosmos chain; a share of the protocol cut
(`validator_reward_bps`); and the **security flow** of each epoch, the one emission slice that leaves
the module account, sent to `fee_collector` and thus to validators and their delegators through
`x/distribution` ([`epoch.go`](chain/x/emission/keeper/epoch.go)).

**Whistleblower** — anyone may dispute a job by posting `dispute_bond`. An **upheld** dispute returns
the bond **and** pays a reward, bounded by the remaining Treasury (the `upheld` branch of
[`msg_server_adjudicate.go`](chain/x/jobs/keeper/msg_server_adjudicate.go)). An **unfounded**
dispute forfeits the bond to the Treasury: the reward is paid for by the people who guess wrong, which
is what keeps it from being free money.

### Escrow — a property, not a caveat

`hold_bps` is at its maximum in the launch genesis, which means a miner's net payment is **retained at
settlement** rather than handed over on the spot, and resolved at the job's audit checkpoint.

**On the public endpoint no miner has yet been paid**, and the order of the two checks is why. The
decentralized-seed test runs *before* the per-job lottery and returns early below the contributor floor
([`audit_sampling.go`](chain/x/jobs/keeper/audit_sampling.go)): on a block without a seed, no job
reaches the draw at all and **the whole** miner share is held — not a fraction of it. On a block with
one, the checkpoint resolves each job one of two ways:

- a job the audit lottery does **not** select is **released to the miner** at that checkpoint (same
  file, the `releaseHeld` branch). The live chain runs `audit_sample_bps = 5000`, so that is roughly
  **half** of jobs — read the value with `dendrad query jobs params -o json` rather than trusting this
  sentence, since the parameter is governable;
- a job the lottery **does** select waits for the audit committee's verdict.

**Held is retained — not cancelled, not forfeited, not quietly expired.** The deferral carries no
expiry and no attempt counter, and that is deliberate: releasing a payment because the audit could not
run would hand an abstaining validator the power to push jobs through unverified. It drains in two
stages. On blocks where a decentralized seed exists, jobs the lottery does **not** select finalise and
release. What stays held is what the lottery **does** select: those wait until enough miners are
registered for a jury to reach `audit_min_quorum`, since the jury excludes the primary — subtract the
registered count from `audit_min_quorum + 1` and that is the gap, both numbers being readable with
`dendrad query jobs params -o json` and `dendrad query jobs list-miner -o json`. That is not a code
change; it is operators arriving.

This is the protocol failing closed. A payment that cannot be verified is not released — and it is not
destroyed either. `held-summary` reports the age of the oldest retention precisely so that the second
half of that sentence stays checkable.

## Confidentiality — honest two-tier model

- **Consumer GPU tier:** end-to-end X25519 + HKDF-SHA256 + AES-256-GCM ([`services/modea/crypto.py`](services/modea/crypto.py)) — nothing in plaintext on-chain or at the relay. The runtime guarantee is **hardened deterrence** (process confinement, sealed memory, slashing) — **not a cryptographic guarantee.**
- **Software attestation is implemented but not enforced, and must not be counted as a protection.** The gate exists in the relay ([`services/relay.py`](services/relay.py)) and arms **only** on `DENDRA_ATTEST_REQUIRE=1` — a variable the deployment kit sets nowhere, so the public relay serves without it and says so at startup. This is deliberate, not an oversight: an attestation allow-list pins **specific** measured digests, so requiring it on an open network would force every third-party miner onto one published build, excluding exactly the independent operators the network needs. [ADR-011](docs/adr/ADR-011-deux-modes-confidentialite.md) states the consumer tier as hardened deterrence, not a cryptographic proof.
- **Datacenter tier (opt-in):** real hardware **TEE** (Hopper/Blackwell). There is **no hardware TEE on consumer GPUs**, so the cryptographic guarantee is offered only on this tier (**planned, opt-in — no datacenter tier is deployed yet**).

Stated plainly rather than oversold: the consumer tier raises the cost of extraction, it does not make extraction impossible.

## Repository layout

```
chain/               Cosmos SDK 0.53.6 chain (source of truth) — x/{jobs,emission,modelregistry}, proto, genesis
services/            Reference off-chain stack: gateway, miner, relay, client, judge glue, regex content-filter, exporter, faucet
tokenomics/          Canonical economic model (tokenomics_v5)
deploy/              Node / validator / miner kits + one-command join.sh + public launch kit
docker/              Dockerfiles, entrypoints, monitoring provisioning
wallet/              Web wallet (single-file HTML) + docs
docs/litepaper.md    The litepaper
docs/adr/            Architecture Decision Records — the reasoning behind every protocol choice
```

## Verification & security model

*(Protocol. Every mechanism below is in the binary and covered by tests; none of them is currently exercised by the public endpoint, which draws no committee — see [What is actually running today](#what-is-actually-running-today).)*

- **Optimistic verification + LLM-as-judge** (two-stage: coherence, then same-fact).
- **Anti-evasion**: a silent primary is slashed via the committee; fee-hold v2 ("never the bond" — clawbacks come from the withheld fee, never the security bond); pro-honest floor of **≥2/3 of the anchored committee seats** before any hard slash.
- Assignment via a **decentralized VRF beacon** (vote-extensions). Anti-Sybil via **non-recoverable demand**.

The reasoning behind each of these choices — including the ones that were tried and abandoned — is published in full: see [`docs/adr/`](docs/adr/README.md). The [litepaper](docs/litepaper.md) is the short form.

## Contributing & security

- Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Vulnerability disclosure: see [`SECURITY.md`](SECURITY.md) — please report privately, do not open a public issue for security bugs.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
