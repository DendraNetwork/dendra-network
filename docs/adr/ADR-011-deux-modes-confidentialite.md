# ADR-011 — Two confidentiality modes (fast Standard / asynchronous Confidential MPC)

**Status:** Accepted. Settles [ADR-OPEN-PRIVACY](ADR-OPEN-PRIVACY-confidentialite.md).
**Implementation:** Mode A is implemented in the reference services (`services/`:
end-to-end encryption between client and miner, sealed client, no content on-chain). **Mode B (MPC)
is decided, not implemented** — no multi-party computation path exists in the code.

## Context

Firm product requirements:

1. Content (prompts, outputs) is **never public and never stored in the clear on-chain**.
2. User identity is **anonymised**.
3. **Consumer GPUs** must be able to mine (non-negotiable constraint).

"Hidden even from the miner", consumer GPUs and low latency do not coexist with today's technology
(see [ADR-OPEN-PRIVACY](ADR-OPEN-PRIVACY-confidentialite.md): TEEs mean datacenter hardware, zkML and
FHE are far too slow). The resolution is to offer **two modes**, chosen by the user, per job.

## Decision

### Mode A — "Standard" (fast, interactive)

- **One consumer-GPU miner**, normal latency (real-time chat).
- Content **encrypted end-to-end between client and miner**; **nothing in the clear on-chain or
  public** (only commitments and ciphertexts on-chain, replacing the publication model of
  [ADR-006](ADR-006-data-availability.md) for this mode). Identity is **anonymised**.
- The miner does hold the plaintext in RAM **for the duration of the computation**. The guarantee is
  **deterrence, not cryptography**: **sealed** miner client plus **software attestation** of the
  binary, **logging forbidden**, **ephemeral** processing, **stake and slashing** on proven leaks,
  and trap probes.
- Correctness verification: **best-effort plus reputation plus random audits** (no public
  re-execution — [ADR-003](ADR-003-fraud-proof-bisection.md) does not apply here).
- **Threat model**: protects against the public, the chain, and a network observer. **Does not
  protect** against a malicious miner that exfiltrates.

### Mode B — "Confidential" (asynchronous, private even from miners)

- **Three non-colluding consumer-GPU miners**, VRF-drawn across distinct domains and operators, in
  **honest-majority MPC (2-of-3)**.
- The client **secret-shares** the prompt; each share in isolation is noise. Miners compute in MPC
  (PUMA / PermLLM / TruncFormer style) without ever seeing plaintext. **Only the client
  reconstructs** the output.
- **Asynchronous** latency (seconds per token), **high bandwidth** between miners.
- **Cryptographic** guarantee of both confidentiality and correctness as long as **at most 1 of the 3
  miners** cheats or colludes (honest majority). Nothing in the clear on-chain or public; identity
  anonymised.
- **Threat model**: protects against the public, the chain, the network **and the miners**, except
  **collusion of all three** on a given job.

### Common to both modes

- **On-chain: commitments and ciphertexts only, never content.**
- **No public re-execution**; verification is by software attestation (A) or honest-majority MPC (B),
  not by [ADR-003](ADR-003-fraud-proof-bisection.md).
- **Anonymity**: stealth or single-use addresses per job, payments through a shielded pool, network
  relaying (mixnet), a **log-free gateway** with padding
  ([ADR-010](ADR-010-gateway-permissionless.md)).
- **Consumer GPUs** in both modes.

### Explicitly rejected anti-pattern

**Split inference** — sending miners raw intermediate activations — is **forbidden**. Published
attacks (ActInv, SipIt) reconstruct the prompt from activations even with added noise or
sparsification. Confidentiality against the miner must go through **secret sharing (MPC)**, never
through mere layer splitting.

## Consequences and residual risks

- **Mode A**: trust in deterrence, not cryptography, against the miner. This must be stated plainly
  to the user.
- **Mode B**: rests on the **non-collusion** of three parties, which makes **anti-collusion
  critical** (VRF, operator diversity, stake, reputation,
  [ADR-009](ADR-009-anti-collusion-vrf.md)). Costs: asynchronous latency and bandwidth; mature MPC
  protocols target models around 7B.
- **Determinism** ([ADR-OPEN](ADR-OPEN-strategie-determinisme.md)) becomes **secondary**: it stays
  relevant only if a non-confidential publicly verifiable tier is also maintained.
- Reconfigures [ADR-005](ADR-005-architecture-2-tiers.md): the "tiers" become the confidentiality
  **modes** A and B, possibly alongside an optional public tier.

## Open items

- Choice of a concrete MPC protocol (3-party replicated secret sharing, malicious-secure) and of the
  model class (7B and below first).
- Exact anonymity mechanics (stealth address scheme, shielded pool).
- Software attestation of the miner client in Mode A: technology and registry entry in
  `x/modelregistry`.

## Links

[ADR-OPEN-PRIVACY](ADR-OPEN-PRIVACY-confidentialite.md) ·
[ADR-005](ADR-005-architecture-2-tiers.md) · [ADR-006](ADR-006-data-availability.md) ·
[ADR-009](ADR-009-anti-collusion-vrf.md) · [ADR-010](ADR-010-gateway-permissionless.md)
