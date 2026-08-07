# ADR-025 — Optimistic verification by VRF sampling (k=1, sampled audit, hard slash)

**Status:** Accepted — implemented and active. `verification_mode=1` is set in the shipped genesis
(`chain/config.optimistic.yml`).
**Extends and partly supersedes:** [ADR-020](ADR-020-tier-verification-s2.md) — re-verification
survives but stops being performed **on every job by a k=3 committee** and becomes a **sampled
audit**. Builds on [ADR-022](ADR-022-proof-of-useful-availability.md),
[ADR-007](ADR-007-anti-dilemme-verificateur.md), [ADR-009](ADR-009-anti-collusion-vrf.md),
[ADR-003](ADR-003-fraud-proof-bisection.md).
**Corrected by:** [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) closes an evasion path in the
liveness rule below; [ADR-032](ADR-032-comite-audit-ancre.md) and
[ADR-033](ADR-033-comite-readjudication-ancre.md) authenticate the voting sets; the judge is specified
by [ADR-026](ADR-026-llm-juge-onchain.md).

> **Consistency notes that prevail over the body below.** (1) The audit verdict key is
> `<jobId>__verdict__<minerId>`, a **binary** commit `"0"` / `"1"` (see
> [ADR-026](ADR-026-llm-juge-onchain.md)); the `__redo__` key mentioned further down applies only to
> the re-adjudication committee. (2) `verification_mode` is an **integer**: **0** = redundant
> (default), **1** = optimistic; the words "redundant" and "optimistic" are only labels. (3) The
> retained judge is a **language-model judge** — cosine is not sufficient even with a real embedder.

---

## 1. Context and problem

Verification was a **k=3 redundancy**: the assigned committee drew three stake-weighted miners, all
three computed, and settlement required a **complete committee** before paying the majority and
slashing the outliers.

Stated plainly: three miners to answer one question is slow, expensive, and blocks several
evolutions.

Diagnosis:

- **Cost is roughly 3×** raw inference (three miners pay for the compute behind one answer).
- **Latency**: at least three concordant commits are needed, so there is no **streaming**, and a
  **large model** (70-90B) is penalised threefold.
- **It blocks evolutions**: because a single miner can never settle a job alone, streaming, large
  models and GPU pooling all stay constrained.
- **But it cannot simply be removed.** Dropping k=3 with nothing in its place removes the honesty
  guarantee, and the thesis collapses with it.

A survey of the alternatives (PoSP/Hyperbolic, opML, zkML, TEE, Yuma) settled the question: the
**only** model compatible with consumer GPUs, a cost near 1×, and the already-adopted optimistic
thesis is **optimistic verification by random sampling plus a hard slash** — a Nash equilibrium,
"Proof of Sampling", arXiv 2405.00295.

**The decisive asset: the chain already holds, dormant, all the machinery required.** This is not a
rewrite but a **promotion** of already-reviewed code:

| Existing building block | Location | State at the time |
|---|---|---|
| **Permissionless bonded** challenge of a settled verdict | `msg_server_dispute.go` (`DisputeVerdict`) | dormant (`dispute_window==0`) |
| **Trustless re-adjudication by a fresh committee** (excludes the original committee, stake-weighted, **reverses erroneous slashes**, **rewards the whistleblower**) | `msg_server_adjudicate.go` (`AdjudicateDispute`) | dormant |
| Recording of slashes, enabling **restitution** | `SlashRecords` | active |
| **Decentralised VRF beacon** (aggregated vote extensions), unpredictable | `decentralized_seed.go`, `abci.go` | active when `committee_seed_source=1` |
| Stake-weighted selection with **parametrisable k** | `committee.go` (`assignedCommittee(ctx, jobId, size)`) | active (`size` already an argument) |
| Real slash in coins (80 % of stake) | `SlashLeakBps=8000`, `params.go` | active |

In other words, the "sampled audit" is **a `DisputeVerdict` that the chain opens by itself, with
probability `s`, instead of waiting for a vigilant human**; `AdjudicateDispute` then resolves it. And
that is **exactly the pattern of [ADR-022](ADR-022-proof-of-useful-availability.md)** ("the challenge
IS a real sampled job, indistinguishable, VRF-drawn, verified through the same semantic path"),
**generalised from *availability* to *correctness***.

---

## 2. Decision

Adopt an **optimistic** verification mode, governed by a new `verification_mode` parameter (**default
= redundant = current behaviour unchanged**; the switch is a governance act, taken once the judge is
ready, see §3).

In optimistic mode:

1. **k = 1.** A **primary** miner is drawn by `assignedCommittee(ctx, jobId, 1)`. It computes, anchors
   its commit, **and is paid immediately** on that single commit (optimistic payment, natural
   streaming, large models at 1×).

2. **Sampled audit, VRF-drawn *after* the commit.** At each block, for every job settled in optimistic
   mode and **not yet audited**:

   ```
   audit  ⇔  H( decentralizedSeed(h_settlement) ‖ jobId )  mod 10000  <  audit_sample_bps
   ```

   The seed is the **decentralised VRF beacon** posted at a height **later than the commit**, so the
   miner **cannot know, at the time it answers, whether it will be audited** — it committed *before*
   the draw. This is the anti-grinding condition of ADR-022, reused.

3. **If audited, the chain disputes its own settlement.** The job is marked `+disputed` (through the
   `DisputeVerdict` machinery, with a module account as disputer) and an `audit_requested` event is
   emitted. A **fresh committee** recomputes off-chain and re-commits; **`AdjudicateDispute`** decides,
   stake-weighted, excluding the primary:
   - proven divergence: **hard slash** of the primary (`SlashLeakBps` = 80 % of stake) plus
     re-settlement;
   - agreement: primary **vindicated**, nothing moves.

4. **Permissionless challenge stays open.** Independently of the protocol draw, **anyone** can still
   challenge a job through `DisputeVerdict` (with an anti-grief bond). The VRF audit guarantees
   detection **even if nobody is watching**; the open challenge adds the vigilance of interested
   parties.

5. **Slash sized for the Nash equilibrium.** Cheating (returning a lazy or false result) is negative
   expected value if and only if:

   ```
   s · P  >  (1 − s) · g
   ```

   where `s` is the audit rate, `P` the loss if caught (the slash), and `g` the gain from cheating
   (compute saved, at most the fee). With `P` = 80 % of stake and `s` = 0.10:

   ```
   0.8 · stake · 0.10  >  0.9 · g    ⇒    stake  >  ~11 · g
   ```

   Hence a **hard guard at job opening**: refuse (or cap) if `job.Fee > primary_stake / nashFactor`,
   with `nashFactor` derived from `s` and `SlashLeakBps`. It is the only new economic constraint, and
   it is **verifiable on-chain**.

---

## 3. The judge — a blocking dependency, to be settled *before* switching

Sampling is worthless if the judge cannot tell a real result from a false one. The judge at the time
of writing was a cosine over **bag-of-words** vectors. **A lazy result containing the right keywords
passes the threshold.** As long as that holds, the audit catches roughly zero cheating, and
**sampling is cosmetic.**

That is therefore the absolute prerequisite of this record:

- **Minimum viable**: a real semantic embedder (sentence-transformers class, 384 dimensions — already
  provided for, with signed components supported).
- **The better option, *unlocked* by going optimistic**: a **language-model judge** on the audited
  sample. Because only a sample is audited, an expensive judge per audited job becomes affordable —
  something k=3-on-every-job could never finance. **Total cost falls and detection quality rises at
  the same time.** That is the core of the design: the optimistic mode frees the budget that plugs the
  judge gap.

**Activation rule:** `verification_mode=1` must not be switched on until the test "lazy result
rejected, genuinely different true result accepted" is green with the real judge.

---

## 4. Parameters (new and reused)

| Parameter | Role | Proposed default |
|---|---|---|
| `verification_mode` (**new**) | `0` = redundant (current) · `1` = optimistic | **0** (dormant, no change) |
| `audit_sample_bps` (**new**) | audit probability (`1000` = one tenth) | `0` in mode 0; `1000` in optimistic |
| `dispute_window` (existing) | re-commit delay for the fresh committee | `> 0` in optimistic |
| `dispute_bond` (existing) | anti-grief bond on human challenges | reused (protocol audit: bond 0) |
| `min_stake` (existing) | stake floor (Nash sizing) | raise to at least `nashFactor · fee_max` |
| `slash_leak_bps` (existing = 8000) | slash severity (80 %) | **unchanged** |

The pattern "parameter defaults to 0, therefore dormant, therefore end-to-end and devnet strictly
unchanged" is the one already used for `dispute_window`, `committee_seed_source` and
`avail_epoch_blocks`. It is respected here.

---

## 5. Refinements (optional, behind parameters)

- **Adaptive sampling**: let `audit_sample_bps` grow with the **value** of the job (a large fee means a
  higher audit probability). This maintains the Nash inequality on large jobs without over-taxing
  small ones.
- **Reputation and probation, against Sybils and for cold start**: a **new** miner is audited at
  **100 %** (`audit_sample_bps=10000`) over its first `N` jobs, decaying towards the floor as a clean
  audit history accumulates. This settles (a) the **cold start** problem (why trust a stranger) and
  (b) **taxes every Sybil identity** with the cost of probation. Reputation remains a **complement**
  (modulating `s`), **never** the guarantee on its own — the lesson being that reputation alone is not
  trustless.

---

## 6. Consequences

**Positive**

- **Cost falls from 3× to about 1.1×**; **latency equals one inference**, which unlocks streaming and
  large models.
- **Massively reuses dormant, already-reviewed code** (`DisputeVerdict`, `AdjudicateDispute`,
  `SlashRecords`, the decentralised beacon), so little new code and an attack surface already
  examined.
- **Stays trustless**: the fresh committee on the sample is non-colluding (it excludes the original
  and is stake-weighted), and permissionless challenge stays open permanently.
- **The confidentiality side effect of ADR-020 is attenuated**: re-verification, which exposes content
  to a committee, no longer happens on **every** job but only on a randomly sampled fraction, so
  exposure drops from "3× always" to "about 1.1× and random".

**Negative, accepted**

- The per-job guarantee becomes **probabilistic and economic** rather than deterministic per job. That
  is **the optimistic thesis**, explicitly chosen.
- It depends on **three** conditions: a **good judge** (otherwise false negatives, §3, blocking),
  **correct stake sizing** (otherwise the Nash inequality breaks, §2.5), and an **unpredictable
  beacon** (available when `committee_seed_source=1`).
- Risk of a **false positive** (an honest miner slashed by a noisy judge), mitigated by: a tolerant
  cosine threshold, a **fresh committee** rather than a single judge, the challenge window, and the
  **already-coded restitution** through `SlashRecords` (an upheld adjudication re-credits the stake).

---

## 7. Alternatives rejected

- **Keeping k=N**: prohibitive cost and latency, blocks evolutions — it is the problem being solved.
- **opML / bisection** ([ADR-003](ADR-003-fraud-proof-bisection.md)): requires a cross-hardware
  determinism that LLM inference does not have (0.88 on the hard cases), so it is unsuited to sampling
  LLM output. **Kept** for a future deterministic greedy-decode tier
  ([ADR-035](ADR-035-verification-deterministe.md)).
- **zkML**: proof cost per inference is prohibitive (days or weeks for a 70B model) — a distant
  horizon.
- **TEE**: requires datacenter hardware, which **breaks the consumer-GPU thesis**; deferred.
- **Reputation alone (Yuma style)**: not trustless, tends towards centralisation — **kept as a
  complement** (probation, §5), not as the guarantee.

---

## 8. Honest limitations

- Audit unpredictability **depends on the decentralised beacon being active**
  (`committee_seed_source=1` plus vote extensions). On a permissive single-machine devnet where the
  seed falls back to a legacy source, the draw is less unpredictable — a two-machine bench is the
  right test rig. The consequences of that fallback for pool eligibility are analysed in
  [ADR-037](ADR-037-gel-du-vivier-de-tirage.md).
- `ReportDivergence` remains **governance-gated** (the caller supplies the evidence, which is
  forgeable): the protocol audit **does not** go through it. It goes through the fresh committee of
  `AdjudicateDispute`, which is the legitimate on-chain evidence.

## Links

[ADR-020](ADR-020-tier-verification-s2.md) · [ADR-022](ADR-022-proof-of-useful-availability.md) ·
[ADR-026](ADR-026-llm-juge-onchain.md) · [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) ·
[ADR-032](ADR-032-comite-audit-ancre.md) · [ADR-033](ADR-033-comite-readjudication-ancre.md) ·
[ADR-007](ADR-007-anti-dilemme-verificateur.md) · [ADR-009](ADR-009-anti-collusion-vrf.md) ·
[ADR-003](ADR-003-fraud-proof-bisection.md)
