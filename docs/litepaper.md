# Dendra — Litepaper

**Useful-work AI on a sovereign Cosmos L1.**
$DNDR · fixed supply · devnet / research preview · open source

> **Status note.** This document describes a working **development network**, not a finished product. Sections marked *(roadmap)* are not yet implemented. $DNDR is a utility token; there is no token sale. **Verification has moved to an *optimistic* model (sampled audit + LLM-judge — ADR-025/026/028); any redundant-committee / cosine description below is the earlier design. Hard slashing is *proven live* on the launch-genesis stack (rented GPU box + VPS chain), with zero honest false-slash; pool payouts are *observed live and unit-tested*, not yet independently artifacted.** See the disclaimer at the end.

---

## Abstract

Dendra is a Cosmos-SDK Layer-1 whose economic layer rewards **real machine-learning inference** performed on consumer GPUs, rather than hashing. A client submits an end-to-end-encrypted prompt; the chain escrows a fee, assigns a miner through an unpredictable decentralized-VRF seed, the miner runs the model locally and is paid optimistically, and a sampled fraction of jobs is re-checked by a fresh committee with an LLM-as-judge — cheating is slashed, all settled in a fixed-supply, zero-inflation token. Consensus remains CometBFT BFT (validators are distinct from miners); the novelty is the *useful-work market* layered on top.

*(Honest note: the optimistic path — k=1 + sampled audit + LLM-judge, ADR-025/026/028 — is coded and tested; the chain binary defaults to the earlier redundant-committee path (§3–4, kept for reference), and the **public launch genesis arms optimistic mode from block 1**. Hard slashing is proven live end-to-end (both cheating modes, zero honest false-slash) on the launch-genesis stack — a rented GPU box + a VPS chain; pool payouts are observed live and unit-tested, not yet independently artifacted.)*

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

*(Diagram shows the **optimistic** model — ADR-025/026/028 — **armed from block 1 in the public launch genesis**. The chain binary's default remains the earlier redundant-committee path described in §3–4, kept for reference.)*

- **Consensus:** CometBFT BFT, ~1 s blocks. **Miners ≠ validators** — GPU possession never secures consensus.
- **Gateway:** an OpenAI-compatible endpoint (`/v1/chat/completions`); any existing client (e.g. Open WebUI) uses Dendra unchanged.
- **On-chain module `x/jobs`:** escrow, committee assignment, commit anchoring, settlement, slashing, pools.
- **Off-chain:** miners (`miner`), an encrypted relay bus, a Prometheus/Grafana supervision stack.

---

## 3. Job lifecycle (redundant mode — current live default)

> This is the **redundant-committee** path that runs by default today (`verification_mode=0`). The **optimistic** model that supersedes it as the project's thesis is described in §4 bis.

1. **Open + escrow.** The gateway opens a job and locks the fee in a module account. Pricing is pay-per-token: `fee = base + per_token × (in + out)`, with a two-phase escrow that settles on the *effective* output.
2. **Assignment.** `open-job` fixes a seed derived from unpredictable block data (a decentralized **ECVRF** beacon aggregated via vote-extensions, bound to the block hash); the committee of size *k* = 3 is the set of miners whose `hash(seed | jobId | minerId)` ranks lowest, **weighted by stake**. The requester cannot grind the job id to choose a complicit committee (anti-grinding), and splitting stake across identities does not increase influence (anti-Sybil).
3. **Inference.** Committee miners decrypt the prompt in RAM, run the LLM, and anchor a *commit* (hash + embedding) on-chain. Plaintext never leaves the miner.
4. **Settlement.** The chain reads the anchored commits of the assigned committee, computes the honest majority, pays it from escrow, slashes divergent miners' real bonded stake, and returns the answer to the client.

Settlement is **replay-safe**: a single shared predicate marks a job paid across every settlement path, so it can never be paid twice.

---

## 4. Verification — semantic, not exact (redundant mode)

LLM output is non-deterministic, so byte-equality verification fails. In redundant mode, Dendra clusters the committee's anchored **embeddings** by an **integer cosine** computed in `big.Int` (no floating point, hence reproducible across validators). Two honest answers with the same meaning stay close and survive; an outlier that diverges beyond a governed threshold is slashed. This tolerates legitimate variation while still punishing wrong or lazy work.

> **Measured limit (2026-06-15).** Cosine-of-embeddings is **not a reliable judge of correctness**, even with a real embedder: an order-insensitive embedder (`nomic-embed-text`) accepts both keyword-salad and fluent-but-false answers above the threshold. This is what motivates the optimistic pivot below — the cosine is retired *as a judge*.

## 4 bis. Verification — optimistic (the thesis; armed at launch)

The project has **pivoted** (ADR-025/026/028) to an **optimistic** model, gated by a `verification_mode` parameter. The chain binary **defaults to `0` (redundant)**; the **public launch genesis sets it to `1` from block 1**, now that the judge path is proven live. In optimistic mode:

1. **k = 1, paid optimistically.** A single stake-weighted **primary** miner is drawn by the existing committee selector, answers, anchors its commit, and is paid — *provisionally* on auditable jobs (ADR-028). Cost falls from ~3× to ~1×; latency is one inference, which unblocks streaming and large models.
2. **VRF-sampled audit.** After the commit, at each block the decentralized VRF seed decides — via `H(seed ‖ jobId) mod 10000 < audit_sample_bps` — whether a job is audited. Because the seed is posted *after* the commit, the miner cannot know in advance if it will be checked. `audit_sample_bps` is an on-chain, **governable** parameter: it is set high on the current testnet (5000 = 50%, to gather evidence fast) and is meant to fall toward ~10% as the network matures.
3. **Fresh committee + LLM-as-judge.** On an audited job, the primary **reveals** its answer to a fresh committee (excluding the original miner, stake-weighted); each member runs an **LLM-as-judge** and commits a **binary verdict** (`"1"` valid / `"0"` invalid). The network deliberately requires **heterogeneous judge models** — a single shared model makes judge errors *correlated*, which is what produced false slashes in our own measurements. The admissible model set is enforced **off-chain by operators today, not by consensus**; moving it on-chain is open work. A slash then requires two-thirds of the anchored seats *and* a stake majority (see below).
4. **Hard slash, clawback, appeal (ADR-028).** Proven divergence → the provisional payment is **clawed back** and the primary's stake **slashed** (`SlashLeakBps`, 80%). A miner that **stays silent** is clawed back and slashed too (closing the silence-evasion gap), while an honest miner that was merely offline can **reveal late within an appeal window** and recover. No slash is applied below a verdict quorum (anti false-positive).
5. **Nash-sized.** Cheating is loss-making whenever `s·P > (1−s)·g` (audit rate `s`, slash `P`, cheat gain `g`); job opening is capped so `fee ≤ stake/nashFactor`.

The elegance: auditing only a *fraction* of jobs makes a **costly LLM-judge affordable** — the budget that the always-on k=3 path could never fund — so detection quality rises *and* total cost falls at the same time. Deterrence does not need every job checked; it needs the *expected* cost of cheating to stay negative.

*(Honest status: the optimistic path and its off-chain reveal/judge glue are **coded and tested**, and the on-chain **slash is proven live** (a revealing cheater loses ~99% of stake, a silent one ~80%, zero honest false-slash). The **public launch genesis arms optimistic mode from block 1**. A hard slash requires **two independent locks, both priced in capital**: "invalid" verdicts from at least **two-thirds of the anchored committee seats**, *and* a **strict majority of the voting stake**. Committee members are **drawn on-chain by VRF and anchored before they can vote** — so buying influence over a verdict costs capital, not identities.)*

---

## 5. Confidentiality — two modes, stated honestly

- **Mode A — Standard (default).** One GPU miner; the prompt is end-to-end encrypted (X25519 ECDH + AES-256-GCM). Nothing in clear at the relay or on-chain. **Honest limit:** the miner decrypts in RAM to compute, so privacy is by **deterrence** — sealed memory (`mlock`), egress/disk guards, attestation, and slashing — *not* a cryptographic guarantee against a determined host operator.
- **Mode B — Confidential (opt-in, research).** 3-party MPC; private as long as at most one of three nodes colludes, so the miner never sees plaintext. **Honest limit:** a research spike, not production — residential RTT (~50 ms) makes it slow, and secure non-linearities (softmax/GeLU/LayerNorm) are modeled but not implemented.

We publish these limits rather than claim a guarantee we don't have.

---

## 6. Tokenomics — $DNDR

A **fixed-supply utility token**: the medium for paying for inference and rewarding miners.

| Property | Value |
|---|---|
| Max supply | **10,000,000 DNDR** (hard cap, **zero inflation, zero mint**) |
| Base unit | `udndr` — 1 DNDR = 1,000,000 udndr |
| Genesis allocation | Community 35% · Reserve 33% · Treasury 27% · Team 5% |
| Emission | Release of the pre-allocated **Reserve**, decreasing 22%/yr of the remainder |
| Emission flows | work (demand-gated 1.5×) · availability (4h slashable challenge) · security |
| Burn | 5% of fees (soft deflation → ~8.1M at 10y) |
| Protocol cut | 15% of a job (split: validators 50% / dev 20% / treasury 30%) |

**Key properties.**
- **No protocol minting.** All rewards come from releasing the pre-allocated Reserve; the chain does not mint new tokens for them. The protocol's **custom modules hold no `Minter` permission**, and the standard `x/mint` module is **removed from the chain binary entirely** — there is no minting module to configure, so fixed supply holds **by construction**, verified at runtime: a fresh genesis boots to a total supply of **exactly 10,000,000 DNDR** in a single denom. *(Earlier drafts neutralised `x/mint` via `inflation=0` genesis params; it has since been de-registered outright.)*
- **Demand-gated work flow.** The work subsidy is bounded at `1.5×` a non-recoverable demand counter (fees burned/spent). Self-dealing — a miner paying its own jobs through a separate address — is possible, but kept **economically -EV** (the cut + burn paid exceed the subsidy unlocked), so it cannot drain emission. That counter is a settlement-volume proxy, **not** a measure of external traction.
- **Real skin in the game.** Miner bonds are escrowed real coins; slashing destroys real value; the 5% burn really reduces supply.
- **Validated (supply).** A 4,000-run Monte-Carlo keeps supply ≤ 10M in 100% of runs — and this is *structural*, not statistical: emission only releases the pre-allocated Reserve, the protocol never mints.
- **Security endgame (stated honestly).** A fixed supply means a finite security budget. The Reserve funds validators for ~20+ years; beyond depletion, security relies on real transaction fees — **there is no on-chain guaranteed APR floor** (a model figure we do *not* enforce on-chain). If fees prove insufficient at that horizon, governance may adopt a minimal, predictable tail-emission (Monero/Ethereum-style) — a pre-disclosed policy decided *then* with real data, never a dormant discretionary lever. We name the trade-off rather than hide it.

---

## 7. Security model (what to trust, and what not to)

- **Trust the BFT consensus** for ordering/finality (standard CometBFT assumptions).
- **Trust the economic verdict** for inference correctness *up to an honest majority of the assigned committee's stake*. The beacon makes assignment unpredictable; stake-weighting makes Sybil splitting pointless; real bonds make lying costly.
- **Do not** treat Mode-A confidentiality as cryptographic — it is deterrence.
- **Do not** treat this as audited-for-production. It has had **eight internal review cycles** (which drove it to "claims match the code"), but **no external audit**. Some components remain modeled or partial: **Mode-B MPC**. The **optimistic verification + LLM-as-judge** is coded, tested, and **armed from block 1 in the public launch genesis**, with the hard slash **proven live** (§4 bis). **Decentralized randomness is implemented**: per-validator VRF aggregated via ABCI++ vote-extensions, consumed by committee selection, with the VRF input **bound to the block hash** (anti-precompute) — **demonstrated across two physical machines** (real network, strict BFT signatures). OS confinement is implemented as **deterrence**, not cryptographic.
- **Hard slashing is proven live** on the launch-genesis stack (rented GPU box + VPS chain: both cheating modes, zero honest false-slash); **pool payouts** are observed live and unit-tested, not yet independently artifacted. Treat "real on-chain economy" as proven-in-devnet, not production-audited.

Internal audit summaries (across eight review cycles) are available on request, covering what each cycle found and what remains open. We consider that transparency part of the security posture.

---

## 8. Status & roadmap

**Live on a local devnet:** end-to-end real inference, the full on-chain economy in real coins (emission / bonds / slash / burn / stake-weighted committees — *hard slashing is proven live on the launch-genesis stack (rented GPU box + VPS chain), zero honest false-slash; pool payouts are observed live and unit-tested*), E2E encryption, replay-safe settlement, least-privilege permissions, a **real RFC-9381 ECVRF** (verified on-chain for availability proofs and committee seeding), a **2-validator consensus demonstrated across two physical machines** (decentralized VRF, `contributors=2` per block under strict signatures), **on-chain miner-key anchoring** (anti-MITM) with key rotation, and **secure-by-default** settings — all unit-tested and committed. **Optimistic verification + LLM-as-judge (ADR-025/026/028) is coded, tested, and armed from block 1 in the public launch genesis.**

**Roadmap:**

1. **Artifact the optimistic slash & pool payouts on a broader multi-operator network** — the critical economic-security milestone. The slash is already **proven live** on the launch-genesis stack (rented GPU box + VPS chain, both cheating modes, zero honest false-slash); next is independent multi-machine reproduction and pool-payout artifacts.
2. **Maturation** — *shipped:* governable emission params, on-chain model registry (enforceable), real work/availability distribution, verifiable VRF for availability & committee seed, 2-validator consensus, secure-by-default settings. *Next:* public explorer + CI.
3. **Public incentivized testnet** — multi-machine, cross-hardware, community node operators.
4. **Hardening + external audit** — flip on optimistic verification with the LLM-as-judge wired live, Mode-B decision, remote attestation, third-party audit. (Decentralized VRF via vote-extensions, block-hash binding (VE-01), and the **2-machine test**: all **done**.)
5. **Mainnet** — only after the above. No date is promised.

---

## Disclaimer

Dendra is an open-source **research preview running on a development network**. **$DNDR is a utility token** used inside the protocol; it is **not** a security or an investment, there is **no token sale, presale, or ICO**, and nothing in this document is financial, legal, or investment advice. The software is provided "as is", has **not** undergone an external security audit, and must not be used to handle real value. Default-mode confidentiality is best-effort deterrence, not a cryptographic guarantee. Read the source and do your own research.
