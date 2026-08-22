# ADR-026 — Language-model judge on-chain for the audited sample

**Status:** Adopted — the mechanism is coded and proven live in a distributed setting.
**Follows from** [ADR-025](ADR-025-verification-optimiste-echantillonnage.md) §3 (the judge is the
blocking dependency) and **corrects** [ADR-020](ADR-020-tier-verification-s2.md): cosine is not a
reliable judge, as measured.
**Implementation:** Implemented on-chain — `AdjudicateDispute` tallies binary verdicts, stake-weighted,
restricted to the anchored committee ([ADR-032](ADR-032-comite-audit-ancre.md)). The judge itself runs
**off-chain** in the miner services; which model a juror runs is **not enforced by consensus** (see
[ADR-031](ADR-031-allow-list-modeles-juges.md)).

> **Judge policy, revised on live distributed evidence.** A distributed slash run with five real
> judges showed that a small fallback model **false-slashes honest miners** in a distributed setting:
> three of five honest judges were hard-slashed at 80 %, each with at least four of five **correlated**
> "invalid" votes on a correct answer. **The N=5 veto does not protect**: a **homogeneous committee
> running one weak judge correlates its errors** — the five instances fail *together* on the same
> pair, the "nine judges, two effective votes" effect — which violates the veto's independence
> assumption. **A static bench of fixed pairs does not predict distributed behaviour**: the same model
> scored 4 % salad and 1 % false negatives on the bench. Consequences:
>
> - The small model is **withdrawn as an audit judge**, whether solo or in a homogeneous committee.
> - The primary judge is a mixture-of-experts model; the sub-26 GB fallback is a mid-size model with a
>   measured 0 % false-positive rate in distributed conditions.
> - **The veto does not license a weak judge**: it is only safe with a judge whose intrinsic
>   false-positive rate is low. Genuine decorrelation requires a **heterogeneous** committee (different
>   models) — see [ADR-031](ADR-031-allow-list-modeles-juges.md).
> - **Go/no-go for an incentivised network**: a distributed slash artefact with zero false slashes
>   **using the production judge**. A bench score must not be taken as evidence of distributed safety;
>   the bench has been shown insufficient.

---

## 1. Context — the measurement

For a single prompt, each candidate judge was measured on its ability to distinguish a TRUE answer
(reworded), a keyword SALAD, and a fluent FALSE FACT. Cosine threshold =
`semanticThresholdBps`/10000 = 0.70.

| Judge | true accepted | salad rejected | **false fact rejected** |
|---|---|---|---|
| Bag-of-words (lexical) | 2/6 | 0/6 | 1/6 |
| **Real embedder (`nomic-embed-text`)** | 5/6 | **0/6** | **0/6** (cosine up to 0.954) |
| Language-model judge, 8B | 6/6 | 4/6 | **6/6** |
| Language-model judge, 12B | 6/6 | 5/6 | **6/6** |

**Hard conclusion:** **embedding cosine is not a judge**, not even with a real embedder — the embedder
is insensitive to word order, so both a keyword salad and a false fact clear the threshold. **Only a
language-model judge decides the false fact**, which is the real attack (a fluent but wrong answer),
while still accepting honest rewordings. Salad (gibberish) is residual and decreases with model size.

**Decision: on the audited sample, the judge is a language-model judge, not the cosine.**

---

## 2. On-chain mechanism

The audit marks a sampled fraction of optimistic jobs `+disputed`. For those jobs only, resolution
changes:

1. **Reveal for audit.** The judge needs the TEXT of the primary's answer, which is encrypted
   end-to-end towards the client. On an audited job the primary **reveals its answer to the fresh
   committee** (re-sealed to the fresh members' keys, or published at the relay under commitment).
   This is the confidentiality cost **already accepted** by ADR-025 §6: exposure moves from "3×
   always" to a small random fraction. Refusing to reveal inside the window is treated as an
   admission (slash).
2. **Verdict of the fresh committee.** Each fresh member (registered, outside the original committee,
   stake-weighted) runs the language-model judge and answers "does the primary's answer convey the
   same correct fact as mine?", then **commits a binary VERDICT** under the key
   `<jobId>__verdict__<minerId>` (`ResultCommit = "1"` valid, `"0"` invalid) instead of an embedding.
3. **Stake-weighted tally.** `AdjudicateDispute` reads the fresh verdicts and computes the **stake
   majority**: an invalid majority means a **hard slash** of the primary (`SlashLeakBps`) plus
   re-settlement; a valid majority means the primary is vindicated. It reuses the existing skeleton
   (exclusion of the original, restitution through `SlashRecords`, disputer reward).

## 3. Judge non-determinism (a critical point)

A language model is not bit-deterministic across machines, so two fresh members may diverge on a
borderline case. **The mitigation is exactly the one used for semantic verification**: the verdict is
**binary** (far more robust to noise than the raw output) **and** the decision takes the **stake
majority** of a fresh committee, not a single judge. Individual noise is absorbed by the vote, just as
inference non-determinism was absorbed by the cosine majority. Temperature 0 is recommended to reduce
variance.

## 4. Which model the judge runs

The identity of the judge model is a **security dependency**: it should be versioned and consensual,
not left to each miner. A registry field was reserved for this
([ADR-027](ADR-027-decisions-execution.md) D4) but it is **inert** — see
[ADR-031](ADR-031-allow-list-modeles-juges.md), which explains why pinning a **single** canonical
model is a worse remedy than the disease, and what holds the property instead. The extra cost is
acceptable in any case: the judge only runs on the audited sample.

## 5. Implementation increments (dormant behind `verification_mode`)

- **On-chain tally**: `AdjudicateDispute` extended so that when verdicts
  `<jobId>__verdict__<minerId>` exist from fresh, **registered**, non-original miners, the decision is
  a **stake-majority tally** instead of a cosine; otherwise it falls back to the cosine path for
  backwards compatibility. Covered by a dedicated test (cheater slashed at 80 %, honest miner
  vindicated).
- **Judge library**: a shared `llm_judge(ref, cand)` plus a pure, tested `parse_verdict` and a
  `verdict_commit` returning `"1"` / `"0"` (an unreadable answer maps to `"0"` out of caution).
  **Shared** by the calibration probe and the miner worker, so there is a single source.
- **Discovery and reveal**: the miner must spot the `+disputed` jobs where it sits on the **fresh**
  committee. Either a `pending-audit` query (listing `+disputed` and not yet resolved) or listening for
  the `audit_requested` event is required. On audit, the primary re-seals its answer and the prompt to
  each fresh member through the relay under a `reveal/<jobId>` key. Refusal inside the window yields a
  default verdict of `"0"` — the primary has to convince, consistent with "refusal is an admission".
- **Broader evaluation** of the judge (a varied set, not three cases) before activation.

## 6. Honest limitations

- The judge stays **probabilistic** (non-zero false positives and negatives), mitigated by: a fresh
  committee rather than a single judge, the challenge window, restitution through `SlashRecords`, and a
  **mandatory evaluation before activation**.
- Gibberish salad is not rejected 100 % of the time by small models (5-6 of 6); this improves with
  model size. It is a degenerate attack, less critical than the false fact, which is locked at 6 of 6.
- Reveal-for-audit **reduces confidentiality on the audited sample** — accepted (ADR-025 §6). Mode B
  (MPC) would remain the only strong confidentiality, and it is out of scope here.
- The **k=3 redundant** case (mode 0) keeps the cosine; replacing it with a language-model judge there
  is a separate project, not decided here.

## Links

[ADR-025](ADR-025-verification-optimiste-echantillonnage.md) ·
[ADR-020](ADR-020-tier-verification-s2.md) · [ADR-011](ADR-011-deux-modes-confidentialite.md) ·
[ADR-031](ADR-031-allow-list-modeles-juges.md) · [ADR-032](ADR-032-comite-audit-ancre.md)
