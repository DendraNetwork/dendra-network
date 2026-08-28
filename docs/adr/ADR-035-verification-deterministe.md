# ADR-035 — DETERMINISTIC verification by recomputation (exact path, no judge)

**Status:** **Proposed — DEFERRED.** Nothing to be coded until the measurement in §5 exists. Recorded so
that the reasoning is not lost.
**Origin**: reading of a third-party decentralised-training protocol whose verification is a
deterministic bit-exact spot-recompute. Transferring that idea to this system is the subject here.
**Complements (does not replace):** [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) (optimistic
audit), [ADR-032](ADR-032-comite-audit-ancre.md), [ADR-034](ADR-034-vitalite-du-mineur.md).
**Implementation:** Not implemented, deliberately.

---

## 1. The observation that motivates this record

**The current audit answers a harder question than necessary.**

- What must be caught: *"did the miner actually execute the model it declares?"*
- What the judge committee actually decides: *"is this answer semantically correct?"*

The second question is **structurally harder**: two honest miners produce two different, correct texts.
That is where everything else comes from — the finding that cosine is dead as a judge
([ADR-026](ADR-026-llm-juge-onchain.md)), the requirement for model heterogeneity
([ADR-031](ADR-031-allow-list-modeles-juges.md)), the false-slash rates measured between 1 % and 19 %
for a single-model jury, and the whole apparatus of quorum and juror participation.

**A miner that answers well but differently is not a cheater. A miner that does not run the declared
model is.** The second is verifiable **exactly**.

## 2. The principle

Inference in **greedy decoding, temperature 0, fixed seed**, on **the same execution class**, is
**deterministic**: same prompt, same model, same quantization, same backend gives **the same bits**. An
auditor re-runs and **compares hashes**.

**Consequences if it holds:**

- **No judge** on that path, hence no false slash is *possible* (the verifier cannot be wrong).
- **No committee, no quorum, no vote**, so the entire juror-participation problem **disappears on that
  path**.
- **Verifiability by a THIRD PARTY.** Today the proof is "our committee judged" — an outsider must
  either believe us or run a judge. With exact recomputation, **anyone downloads the model, re-runs the
  prompt, and compares** — without us. That is the property most aligned with what the product claims:
  moving from "trust our committee" to "verify it yourself".

## 3. The blocker, named

Bit-exactness requires **an identical execution class**: GPU class, CUDA and backend version, build,
quantization, batch configuration, KV cache state. But the promise is **"any consumer GPU"** — so **it
cannot hold across the whole fleet**.

**And the trap not to miss**: replacing bit-exactness with a **tolerance** lands *exactly* back on "what
deviation constitutes cheating?", which is the problem that killed the cosine. **Either it is exact, or
it is of no interest.** No middle ground.

## 4. The retained form: a TIER, never a replacement

1. The miner **declares its execution class**. Model identifier and weights hash **already exist** in
   commits — half the plumbing is there; backend, quantization and GPU class are missing.
2. A job's audit is drawn **among miners of the SAME declared class**, giving **exact** verification by
   hash comparison.
3. **No peer of the same class available** means **falling back to the current judge committee**,
   unchanged.

The judge path therefore remains the general route; the deterministic path is a **stronger** route where
it is available.

## 5. The measurement that decides — and it is free

**Open question, unsettled: how far does determinism hold across hardware?** (Only within a GPU class?
Between two consecutive generations? Across backend versions?)

A third-party protocol is looking for **exactly that number**, calling for community cross-GPU
determinism testing on consumer cards. The same cards are available here and the same result is needed:
following their test costs nothing and yields the datum that decides whether this record is viable or
dead.

**Nothing is coded before that measurement.**

## 6. Source and scope

The full survey context — what the third-party protocol is, what is **rejected** from their approach (a
communication registry, a sovereignty claim), the strategic note on the "small local model" bet, and the
signal that would trigger a re-evaluation — is held with the technology survey. This record carries
only the decision.

One element is worth repeating here, because it has already applied twice (self-issued vitality,
self-issued revelation marker):

> **The watched party never supplies the evidence that exonerates it.**
