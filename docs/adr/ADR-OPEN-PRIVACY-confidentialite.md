# ADR-OPEN-PRIVACY — User confidentiality and anonymity

**Status:** **SETTLED — see [ADR-011](ADR-011-deux-modes-confidentialite.md)** (two modes: fast Standard
and asynchronous Confidential MPC). This document remains the underlying justification.
**Implementation:** See [ADR-011](ADR-011-deux-modes-confidentialite.md) for what is and is not built.

## Context

The question: *do the end users of inference — the people submitting prompts — have their data secured
and anonymous?*

Given the architecture as recorded here: **no — pseudonymous yes, confidential no.** That is not an
oversight but a direct consequence of the verification model.

## The underlying problem: public verifiability against confidentiality

- Tier A (verifiable) proves fraud by **re-execution**
  ([ADR-003](ADR-003-fraud-proof-bisection.md)).
- **Data availability** ([ADR-006](ADR-006-data-availability.md)) requires publishing prompt **and**
  output on IPFS, retrievable throughout the challenge window, so that any verifier can replay the
  computation.
- **Consequence: the content of requests and responses is public.** That is the price of a cheap fraud
  proof.

It is therefore **not** possible, simultaneously and cheaply, to (a) verify by public re-execution and
(b) keep the data secret. Lifting that tension requires expensive cryptography (zkML) or a change of
trust model (TEE).

## Identity

Client-side signing with the user's keys ([ADR-010](ADR-010-gateway-permissionless.md)), with no account
and no KYC, gives **pseudonymity** (an address, not a name). But:

- pseudonymous is not anonymous: an address's activity is public and correlatable (chain analysis);
- if the **content** of a prompt reveals the identity, pseudonymity falls, because the content is public.

## Options (to be arbitrated; all beyond v1)

| Option | Confidentiality | Cost | Trust model | Note |
|---|---|---|---|---|
| **Status quo** (public Tier A) | none (content public) | low | trustless | suited to publicly verifiable computation |
| **zkML** (S4, [ADR-OPEN](ADR-OPEN-strategie-determinisme.md)) | strong (input and output hidden, cryptographic proof) | very high | trustless | high-value jobs, opt-in, roadmap |
| **TEE / confidential computing** | strong (the miner never sees plaintext) | medium | trust in the hardware vendor | attestation to be integrated into `x/modelregistry` |
| **Encrypted Tier B** (reputation, no publication) | partial | low | trust and reputation | loses byte-exact verifiability ([ADR-005](ADR-005-architecture-2-tiers.md)) |
| **Encrypted DA with keys to the verifiers** | partial | medium | verifier committee | breaks permissionless challenge |

## Decisions to take

1. Is content confidentiality a **product requirement** or an **opt-in**?
2. If required: which mechanism (zkML or TEE) and for which tiers or jobs?
3. Target level of anonymity: simple pseudonymity, or anti-correlation measures (mixing, single-use
   addresses)?

> Until this is settled, users must be told clearly that on Tier A, **prompts and outputs are public**
> and identity is **pseudonymous, not anonymous**.

## Links

[ADR-003](ADR-003-fraud-proof-bisection.md) · [ADR-006](ADR-006-data-availability.md) ·
[ADR-005](ADR-005-architecture-2-tiers.md) · [ADR-010](ADR-010-gateway-permissionless.md) ·
[ADR-OPEN](ADR-OPEN-strategie-determinisme.md) · [ADR-011](ADR-011-deux-modes-confidentialite.md)
