# Security Policy

> **Status: research / devnet.** Dendra is experimental software. The testnet is resettable and its token has **no monetary value**. Do not put real value at risk.

> **What the public endpoint runs.** The network has been reset onto a fresh genesis more than once: every stake, balance and registration from a previous chain is gone each time, and this page names no date because a date here is a claim that ages. Its validators are **all operated by the project** — so fault tolerance is zero whatever their number, and stopping any one of them stops block production — and the registered miners stand below the floor the chain enforces, so nothing is served and **no job is verified on that endpoint today**. Whether the decentralized seed meets its contributor floor is **a reading, not a property**: `dendrad query jobs committee-seed-health -o json` returns `latest_contributors` next to `committee_min_vrf_contributors`, and below that floor the beacon falls back to its legacy source, which means the anti-grinding property is **not** in force. Counts printed here go stale; the queries that decide are in the [`README`](README.md), and they are what you should trust. The rule they gate is unchanged: the jury is every registered miner except the one under audit, and a verdict requires `audit_min_quorum` (**4**) jurors to vote — five registered miners in all. A report about the verification path is therefore a report about the **code**, not an observation of the network; that is expected and does not need reporting, and the queries that show it are in the [`README`](README.md). A way to make an audit **conclude** without a jury, to open a draw **below** the seed floor, or to release a held fee without a verdict, is very much in scope.

## Reporting a vulnerability

**Please report security issues privately. Do NOT open a public GitHub issue for a vulnerability.**

- Email: **security@dendranetwork.com**
- Include: a description, reproduction steps, affected component (chain module, gateway, miner, relay, judge), and impact.
- We aim to acknowledge within a few days. Coordinated disclosure is appreciated; we will credit reporters who wish to be credited.

## Scope

In scope: the chain (`chain/`), the off-chain reference stack (`services/`), the deployment kits (`deploy/`, `docker/`).

Particularly valuable reports:
- Ways to **mint or destroy** supply (the invariant is a fixed 10,000,000 DNDR, zero mint).
- **Double-settlement**, escrow imbalance, or paying a job twice.
- **Slashing an honest miner** (false positive), or letting a prover **escape** a deserved slash.
- Leaking **plaintext** on-chain, at the relay, or in logs (only hashes/embeddings/verdicts/counters should ever appear).
- Bypassing the **regex floor** — not by finding content it misses (it misses a great deal by construction, and this project makes no claim that illegal content is filtered out), but by defeating the mechanism itself: making a request skip the filter stage, or disabling it remotely.
- Faucet drain / Sybil beyond the documented rate limits.

## Out of scope

- The resettable testnet's token having no value.
- The consumer-GPU tier providing **hardened deterrence, not a cryptographic guarantee** (this is by design and documented).
- Denial of service against a single self-hosted node.

## Honest posture

We do **not** claim a cryptographic confidentiality guarantee on consumer GPUs, nor that Dendra outperforms frontier models. Reports premised on a guarantee we never made are not vulnerabilities — but reports showing we **fail a guarantee we did make** are very welcome.
