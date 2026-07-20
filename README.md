# Dendra ($DNDR)

**Confidential, verifiable AI inference — paid on consumer GPUs, settled on a sovereign L1.**

> **Status: research / devnet.** Nothing here is production. `$DNDR` is a **utility token** used for staking, fees and rewards — **there is no token sale and the testnet token has no monetary value** (the network is resettable). Dendra is **not** trying to beat frontier models on quality; it offers **privacy + on-chain verifiability + censorship-minimal (best-effort, not a compliance guarantee)** inference on consumer hardware. No financial promises are made anywhere in this repository.

---

## What Dendra is

A client sends an **end-to-end encrypted** prompt; a miner runs the inference **on its own consumer GPU** without the plaintext ever leaking on-chain or to the relay; the work is **settled on a sovereign Cosmos chain** (payment, vesting, anti-Sybil) and **verified economically** — a dishonest miner is **slashed**. The content never touches the chain (only hashes, embeddings, verdicts and counters do).

The "useful work" of the network is **inference**, not hashing.

## How it works (one paragraph)

`Chat client → OpenAI-compatible gateway → chain (escrow → VRF beacon → stake-weighted committee → confidential GPU inference → on-chain settlement) → answer.` Verification is **optimistic**: one primary miner is paid (k=1), a fresh committee audits a VRF-sampled fraction of jobs, and a proven cheat is **hard-slashed** (cheating is negative-EV — a Nash equilibrium). The audit judge is an **LLM-as-judge** (the embedding cosine is dead as a correctness judge — it accepts both word-salads and fluent false facts). The audit committee is **drawn and anchored on-chain** at sampling time, stake-weighted, and **only its summoned members can vote**. A hard slash requires **at least two thirds of the anchored seats** to return "invalid", so a minority verdict cannot slash an honest miner. With no anchored committee, no hard slash fires at all (fail-closed). The launch genesis **arms** this optimistic mode; the conservative redundant **k=3** mode remains available as a fallback. The full loop — inference → payment → VRF audit → committee verdict → slash of a cheat, honest miners untouched — runs end-to-end on the public testnet.

## Confidentiality — honest two-tier model

- **Consumer GPU tier:** end-to-end X25519 + HKDF + AES-256-GCM (nothing in plaintext on-chain or at the relay). The runtime guarantee is **hardened deterrence** (OS confinement, software attestation, sealed memory, slashing) — **not a cryptographic guarantee.**
- **Datacenter tier (opt-in):** real hardware **TEE** (Hopper/Blackwell). There is **no hardware TEE on consumer GPUs in 2026**, so the cryptographic guarantee is offered only on this tier (**planned, opt-in — no datacenter tier is deployed yet**).

We state this plainly rather than overselling.

## Tokenomics (fixed supply, zero mint)

- **Fixed supply: 10,000,000 DNDR. Zero inflation, zero mint.** Custom modules have no `Minter`; the standard `x/mint` module is **removed from the chain binary entirely** — fixed supply holds **by construction** (a fresh genesis boots to exactly 10,000,000 DNDR).
- Genesis allocation: community 34% / reserve 33% / validator-treasury 27% / team 5% / faucet float 1% — 10,000,000 DNDR exactly. Every figure is readable from the launch genesis itself.
- **Emission = release of the pre-allocated Reserve** (never minting), across three flows: work (demand-gated 1.5×), availability, security.
- Soft burn ~5%, deferred to finality. Protocol takes 15% of a job (miner keeps 85%).
- **Anti-bubble intent (honest):** emission should not outrun real external demand. The network exports a settlement ratio `R = on-chain settled demand ÷ emission released` as a **proxy**. This proxy is **farmable** — an operator can self-settle through a separate client address — so a high `R` is **not** proof of external traction, and a sustained low `R` is a warning signal rather than an automatic stop. Making `R` a Sybil-resistant measure of genuine external demand is open work.

## Run it

The whole stack runs with Docker:

```bash
docker compose up -d            # chain + relay + faucet + gateway + content guard + chat UI
```

**One command to join** (points at the public network via a shared config URL):

```bash
CONFIG_URL=<network-info.txt> bash deploy/join.sh              # miner (serve GPU inference for rewards)
CONFIG_URL=<network-info.txt> bash deploy/join.sh --validator  # full node + guided validator bond + VRF
CONFIG_URL=<network-info.txt> bash deploy/join.sh --judge      # miner + audit committee
```

- **Full guide — every role (node / miner / validator / operator):** [`deploy/README.md`](deploy/README.md).
- **Wallet:** open [`wallet/web/index.html`](wallet/web/index.html) in any browser — create/import, check balance, send DNDR (testnet).
- **Advanced / manual node & miner kits:** [`deploy/testnet-node/`](deploy/testnet-node/), [`deploy/testnet-miner/`](deploy/testnet-miner/).
- **Operator (host the network):** [`deploy/launch/`](deploy/launch/) (one-command public launch) and `deploy/testnet/publish_network.sh`.

Moderation ships a best-effort CPU **regex floor** (demonstration-grade, high false-negatives, patterns kept out-of-repo, includes a CSAM pattern), not a politeness filter. In the shipped public configuration it is the **only** content filter: the optional LLM classifier stage exists in the code but stays **off unless `DENDRA_GUARD_MODEL` is set**, and the public launch config leaves it unset. The stage is available, not unreachable. **Do not rely on Dendra for legal-content compliance.**

## Repository layout

```
chain/          Cosmos SDK 0.53.6 chain (source of truth) — x/{jobs,emission,modelregistry}, proto, genesis
services/   Reference off-chain stack: gateway, miner, relay, client, judge glue, regex content-filter, exporter, faucet
tokenomics/ Canonical economic model (tokenomics_v5)
deploy/             Node / validator / miner kits + one-command join.sh + public launch kit
docker/             Dockerfiles, entrypoints, monitoring provisioning
wallet/             Web wallet (single-file HTML) + docs
docs/litepaper.md   The litepaper
```

## Verification & security model

- **Optimistic verification + LLM-as-judge** (two-stage: coherence, then same-fact).
- **Anti-evasion**: a silent primary is slashed via the committee; fee-hold v2 ("never the bond" — clawbacks come from the withheld fee, never the security bond); pro-honest floor of **≥2/3 of the anchored committee seats** before any hard slash.
- Assignment via a **decentralized VRF beacon** (vote-extensions). Anti-Sybil via **non-recoverable demand**.

The full Architecture Decision Records are available on request; the [litepaper](docs/litepaper.md) summarizes the design.

## Contributing & security

- Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Vulnerability disclosure: see [`SECURITY.md`](SECURITY.md) — please report privately, do not open a public issue for security bugs.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
