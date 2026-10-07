# ADR-030 — Verifying and slashing an IMAGE generation response (non-deterministic)

**Status:** Proposed — **deferred**. Nothing is to be coded until the gating prototype below is green.
**Scope**: **TECHNICAL only**. No legal or compliance content.
**Implementation:** Not implemented. No image path exists in the chain code.

**Parent verification chain**: [ADR-020](ADR-020-tier-verification-s2.md) then
[ADR-025](ADR-025-verification-optimiste-echantillonnage.md) (optimistic, k=1, VRF audit, slash) then
[ADR-026](ADR-026-llm-juge-onchain.md) (language-model judge, coherence and same-fact) then
[ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) (anti-evasion).

---

## Context

The moat is **economic verifiability**: a dishonest miner gets **slashed**. For **TEXT** this works
because a verifiable *fact* exists. The mechanism: after k=1 payment, a **fresh VRF-drawn committee**
re-answers the same prompt on a sampled fraction of jobs, a **language-model judge** decides "same
fact?", and a divergence of fact produces a hard slash, with an honest-biased veto. The foundation is
**proven live**.

**IMAGE breaks that mechanism.** Diffusion is **stochastic**: two miners (or a judge re-rendering)
produce **different** images for the same prompt. There **is no single correct image**, so the
"re-answer and compare" core **does not transpose**. That is the reason for deferral.

**The deferral is corroborated by the state of the art in inference verification** (Equilibrium Labs,
*State of Verifiable Inference*, 2025): optimistic verification and random sampling **only work for
deterministic computation** (two nodes must be able to reach the same output); activation *hashing* is
**built for text LLMs**, and **extending hashing to diffusion models is listed as an OPEN problem**;
mixture-of-experts models already break hashing through non-deterministic routing. In other words,
**the text mechanism has NO "fact" equivalent on the image side**.

**Hard constraints**: **consumer** GPUs; **no consumer TEE**
([ADR-011](ADR-011-deux-modes-confidentialite.md)); fixed 10 M supply with zero minting; the moat is
**economic verifiability plus slashing**.

**The question of this record**: can an image response be **verified and slashed** — and if so, **what
is actually being proven**?

---

## Options analysed

For each: what it **PROVES**, what it **does NOT prove**, the **slash** it supports, **cost**, **attack
surface**, and **feasibility on consumer GPUs**.

### A1 — Deterministic reproduction

Pin the model, the seed, every parameter and the backend; the judge re-renders at the same seed and
compares (perceptual hash, SSIM, pixel).

- **Proves** (if reproducible): the miner executed **the pinned model** with the declared parameters —
  a strong equivalent of the text "same fact".
- **Does not prove**: anything beyond conformance to the pinned pipeline.
- **Slash**: the hardest possible (pixel or perceptual divergence implies cheating). This would be a
  genuine "same-fact" for images.
- **Cost**: one re-render per audited job, comparable to text. Reasonable.
- **Attack surface**: small **if** reproduction is exact. But if the comparison threshold must be
  **loosened** to tolerate numerical noise, a cheater can aim at that margin.
- **Consumer-GPU feasibility — THE blocker.** Bit-for-bit reproducibility of diffusion across
  different GPUs and drivers is **notoriously false**. Sources: HuggingFace Diffusers, *Reproducibility*
  — one must **not** expect full reproducibility across different hardware (GPU matmuls are
  non-deterministic and diffusion performs many of them); measured, **A10G, A40 and A100 give DIFFERENT
  results at the same seed**; non-deterministic CUDA/cuDNN kernels mean **no complete numerical
  determinism** even with a fixed seed and deterministic flags set. Corollary: the comparison must fall
  back to **perceptual hash or SSIM**, whose thresholds **overlap** between "same source" and
  "different sources", making a false-positive/false-negative trade-off unavoidable (Snibgo/ImageMagick;
  USENIX Security '22 on the fragility of perceptual hashing). **On a heterogeneous consumer fleet, A1
  fails**: either honest miners are false-slashed (threshold too tight) or cheaters walk (threshold too
  loose). **A1 is viable ONLY on a pinned, homogeneous backend** — an unrealistic assumption here.

### A2 — Prompt-adherence committee

A vision or multimodal judge answers "does the image faithfully render the prompt? is this real
generated content, not noise, not a cache hit, not off-topic?"; the committee votes; slashing applies to
garbage, off-prompt output and absence of real work.

- **Proves**: **real, relevant effort** was expended (generated content, coherent with the prompt). It
  is the **image mirror of the coherence and relevance stage of the text judge**
  ([ADR-026](ADR-026-llm-juge-onchain.md)).
- **Does NOT prove**: that it is the **optimal or correct** image (there is no image ground truth);
  **nor** that the **pinned model** ran (a miner could serve *another* model that renders the prompt
  just as well). A **weaker** proof than A1.
- **Slash**: catches **gross fraud** — noise, cached or replayed images, off-topic output, output
  stolen elsewhere, blatant under-effort. It does **not** catch a miner running a cheaper, weaker model
  that is nonetheless on-prompt.
- **Cost**: one vision-judge pass per audited job (cheaper than a re-render). Compatible with the
  existing VRF sampling.
- **Attack surface**: (a) **judge subjectivity**, hence a risk of false-slashing an honest miner on an
  ambiguous prompt (the **same** trap as text, addressed by the judge self-consistency
  layer); (b) an attacker generates the **cheapest** image
  that clears the threshold; (c) generic catch-all responses. Network precedent: **Bittensor image
  subnets** validate and **score** image miners by **sampling plus control servers**, **never by exact
  recompute** — structurally a verification of **effort and quality**, not of fact.
- **Consumer-GPU feasibility**: **good.** A multimodal judge runs on the same fleet as the current text
  judge. **This is the realistic option.**

### A3 — CLIP alignment (image-text embedding) plus a threshold

- **Proves**: image-to-prompt correlation above a threshold. **Nearly deterministic, very cheap.**
- **Does not prove**: that the pinned model ran; and it is **highly gameable**. CLIP is an **imperfect
  proxy**: reward hacking is documented (arXiv 2601.03468 — optimising a CLIP score degrades the real
  objective); it is **biased** (it penalises detail-rich images and favours simple "faithful" ones —
  arXiv 2507.19002); and an adversary can craft an **out-of-distribution** image that **scores high**
  without being a good image.
- **Slash**: too fragile for a **hard** slash on its own (false positives on legitimately rich content;
  circumventable). Usable only as a **cheap pre-filter or signal**, never as the final judge. It is
  **the text cosine, replayed for images** — and cosine is dead as a judge
  ([ADR-026](ADR-026-llm-juge-onchain.md)).
- **Cost**: negligible (a single CLIP forward pass).
- **Attack surface**: **wide** (adversarial, cache, off-model that scores high).
- **Feasibility**: trivial, but **insufficient alone**.

### A4 — Hybrid (A1 where determinism holds, otherwise fall back to the A2 committee)

- **Proves**: a HARD "same fact" (A1) **when** miner and judge share a pinned reproducible backend;
  otherwise it degrades to **effort and relevance** (A2).
- **Does not prove**: the hard fact on the **heterogeneous** fleet (the dominant case), so in practice
  **A4 reduces to A2** until cross-GPU reproduction is demonstrated.
- **Slash**: hard under a pinned backend, effort-level elsewhere. **High calibration complexity** (two
  regimes, two thresholds, plus detection of which regime applies).
- **Cost and surface**: the sum of A1 and A2; risk of **A1 false positives** if the "deterministic"
  regime is mis-detected.
- **Feasibility**: depends **entirely** on a conclusive A1 prototype. **Premature** before A1 is
  proven.

### Out of scope, noted for the record

- **zkML**: cryptographic proof of execution. **Impractical for diffusion** — zkML frameworks (EZKL,
  Lagrange DeepProve and others) today prove **LLMs** (attention feasible since 2025, under about 15
  minutes per proof at sequence length 2048), while **diffusion remains research** (ICME, *Definitive
  Guide to ZKML 2025*). Proving cost is **out of reach** for a consumer GPU.
- **TEE**: hardware attestation. **No consumer-GPU TEE exists**
  ([ADR-011](ADR-011-deux-modes-confidentialite.md)). Reserved for a datacenter tier, not the target
  fleet.

---

## Decision (recommendation)

**The honest finding, and the core of this record: images can only be verified WEAKLY.** Gross fraud
can be caught (noise, cache or replay, off-topic, stolen output, under-effort) through a
**prompt-adherence committee (A2)**. It is **NOT possible to prove "this is the right or best
image"** — there is **no image ground truth**, and **deterministic cross-GPU reproduction (A1) is not
reliable** on consumer hardware (shown false by the state of the art). An image tier would therefore
have a **thinner moat** than text: **"verified effort", not "verified fact".** That must be stated
**explicitly** and never sold as the equivalent of text.

**Recommendation: offer images as a "verified effort" tier, EXPLICITLY distinct from the "verified
fact" of text, GATED behind A2 plus calibration.** Reject "stay deferred indefinitely" **only if** A2
passes calibration; otherwise stay deferred (fallback to the status quo). Reject A1 and A4 as the
primary mechanism until cross-GPU reproduction is **proven** on the real fleet.

**Retained architecture (A2, aligned with what exists):**

1. **Multimodal judge in a VRF committee** (the same sampled fraction as text), with a **three-stage**
   verdict mirroring [ADR-026](ADR-026-llm-juge-onchain.md): (i) **real generated content**
   (anti-noise, anti-cache), otherwise a hard slash; (ii) **prompt adherence**, where coherent
   off-topic output yields an **abstention**, never "invalid" (the relevance guard); (iii) **judge
   self-consistency at K=3** to kill false slashes on ambiguous prompts **before** any bond moves.
2. **A3/CLIP as a cheap pre-filter** (a signal, never the final slash).
3. **The honest-biased veto retained** (an honest miner mis-judged by a minority is never slashed).
4. **A1 held in reserve**, to be reactivated **if** a prototype proves cross-GPU perceptual
   reproduction under a pinned backend (which would upgrade images to "verified fact").

**To prototype FIRST, before any image opening:**

- **The go/no-go**: a hardened A2 bench — multimodal judge, a **varied image bank** (clean, noise,
  cache replay, off-topic, off-model but on-prompt, ambiguous prompts) — measuring the **honest
  false-slash rate** and the **fraud detection rate**. **Gate**: false-slash rate of zero on honest
  cases, maximum invalid votes on honest cases within the veto margin, and detection at or above the
  threshold on gross fraud cases.
- **An A1 feasibility probe, in parallel and informative**: the same prompt, seed and parameters on
  **at least two distinct consumer GPUs**, measuring **honest-to-honest** perceptual distance against
  **honest-to-cheater**. **If the distributions separate cleanly, A1 and A4 become viable again**;
  otherwise A1 stays dead.

**Pre-committed GO / NO-GO criteria:**

- **GO for an image tier (verified effort)**: the A2 bench green (zero false-slash rate plus fraud
  detection at or above threshold) **AND** UI and positioning that explicitly display **"verified
  effort is not verified fact"** **AND** the non-technical gates satisfied (outside the scope of this
  technical record).
- **NO-GO, stay deferred**: the bench leaves an irreducible non-zero false-slash rate, **or** the tier
  cannot be honestly distinguished from verified fact.
- **Upgrade to "verified fact" for images (A1/A4)**: **only** if the probe proves cross-GPU perceptual
  separation. Not required for opening; it is a later hardening.

---

## Consequences

**Positive**

- Unblocks an **honest** image roadmap aligned with the existing moat (it reuses VRF sampling, the
  committee, the veto, and the relevance and self-consistency guards).
- Catches **gross** image fraud, which is the bulk of the economic abuse (cache, noise, off-topic,
  output theft).
- **Frames the discourse**: "verified effort", owned as such, with no over-promise of parity with text.

**Negative and risks**

- **A thinner moat** on images: a miner serving a **cheaper but on-prompt model** is **not** caught by
  A2. To be accepted and documented.
- **False slashes** are possible on ambiguous prompts (the same trap as text), so the outcome
  **depends** on self-consistency calibration and the veto. That is the hard suspensive condition.
- **Judge cost**: one multimodal pass per audited job, to be measured on the bench.
- **A1 remains open**: "verified fact for images" is **not** delivered and depends on a cross-GPU
  reproduction proof that has not been obtained.

**Neutral and follow-ups**

- Changes **nothing** in the chain code until the bench is green; images stay deferred.
- Reuses the off-chain judge scaffolding; the multimodal judge plugs into it.
- To be re-evaluated if practical diffusion zkML **or** a consumer TEE appears — neither is expected in
  the near term.

---

## Sources

- Equilibrium Labs — *State of Verifiable Inference & Future Directions*:
  https://equilibrium.co/writing/state-of-verifiable-inference (optimistic verification and sampling are
  **deterministic-only**; hashing is built for LLMs and **extension to diffusion is an open problem**;
  Bittensor image subnets use sampling plus control servers).
- HuggingFace Diffusers — *Reproducibility*:
  https://huggingface.co/docs/diffusers/en/using-diffusers/reusing_seeds (**no** full reproducibility
  across hardware; A10G, A40 and A100 diverge at the same seed).
- Ingonyama — *Solving Reproducibility Challenges in Deep Learning and LLMs*:
  https://www.ingonyama.com/post/solving-reproducibility-challenges-in-deep-learning-and-llms-our-journey
  (CUDA/cuDNN non-determinism; numerical determinism not guaranteed).
- *Understanding Reward Hacking in Text-to-Image RL* — arXiv 2601.03468:
  https://arxiv.org/html/2601.03468v1 (CLIP as a reward is **gameable**).
- *Enhancing Reward Models for High-quality Image Generation* — arXiv 2507.19002:
  https://arxiv.org/html/2507.19002v1 (CLIP-score bias).
- Snibgo / ImageMagick — *Perceptual hash thresholds*: https://im.snibgo.com/phashthresh.htm; USENIX
  Security '22 — *Robustness of perceptual hashing*:
  https://www.usenix.org/system/files/sec22summer_jain.pdf (false-positive/false-negative trade-off,
  threshold fragility).
- ICME Labs — *The Definitive Guide to ZKML (2025)*: https://blog.icme.io/the-definitive-guide-to-zkml-2025/
  (LLMs provable; **diffusion still research**).
- Bittensor docs — *Anatomy of an incentive mechanism*:
  https://docs.learnbittensor.org/learn/anatomy-of-incentive-mechanism (image miners scored by
  validators, no exact recompute).

---

*Internal links: [ADR-025](ADR-025-verification-optimiste-echantillonnage.md) ·
[ADR-026](ADR-026-llm-juge-onchain.md) · [ADR-028](ADR-028-anti-evasion-paiement-provisoire.md) ·
[ADR-011](ADR-011-deux-modes-confidentialite.md).*
