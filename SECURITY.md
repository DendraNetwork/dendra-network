# Security Policy

> **Status: research / devnet.** Dendra is experimental software. The testnet is resettable and its token has **no monetary value**. Do not put real value at risk.

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
- Bypassing the **regex floor** so that illegal content reaches a public endpoint.
- Faucet drain / Sybil beyond the documented rate limits.

## Out of scope

- The resettable testnet's token having no value.
- The consumer-GPU tier providing **hardened deterrence, not a cryptographic guarantee** (this is by design and documented).
- Denial of service against a single self-hosted node.

## Honest posture

We do **not** claim a cryptographic confidentiality guarantee on consumer GPUs, nor that Dendra outperforms frontier models. Reports premised on a guarantee we never made are not vulnerabilities — but reports showing we **fail a guarantee we did make** are very welcome.
