# ADR-031 — On-chain enforcement of judge-model heterogeneity

**Status:** **Rejected as specified.** The single-identifier form is refused outright; the allow-list
form is **deferred** behind a measurement. The property itself is held **off-chain**, and must be
described that way.
**Reverses:** [ADR-027](ADR-027-decisions-execution.md) D4 (a single judge model, named on-chain and identical for
every committee member).
**Depends on:** [ADR-026](ADR-026-llm-juge-onchain.md) (the judge decides the audited sample),
[ADR-032](ADR-032-comite-audit-ancre.md) (the voting set is drawn and anchored).
**Implementation:** Nothing to implement. The `audit_judge_model` field exists in
`chain/proto/dendra/modelregistry/v1/params.proto` with a keeper getter and a validator, and **no
execution path reads it** — the field is INERT and documented as such in the proto. No
`min_distinct_judge_models` parameter exists anywhere in the chain.

---

## 1. Context — a reserved slot and a claim that outran the code

[ADR-027](ADR-027-decisions-execution.md) D4 recorded that the judge model would be a governed
registry field, with **every member of an audit committee running the same model**, on the assumption
that different models would produce inconsistent verdicts. A field was reserved accordingly. Public
documentation went further and announced **on-chain enforcement of judge heterogeneity**, through a
parameter counting the distinct judge models that voted.

Two things were wrong with that. First, the reserved field enforces the **opposite** property from the
one being announced: a single canonical model is homogeneity, not heterogeneity. Second, neither
mechanism exists in consensus.

**Verified state of the code.**

- `audit_judge_model` is a field of the model-registry parameters, reachable through a keeper getter
  and covered by a validator and unit tests. **No production execution path reads it.** Nothing in the
  audit or adjudication path consults it; a juror running any model produces a verdict that the tally
  counts exactly the same way.
- `min_distinct_judge_models` **does not exist** — not as a parameter, not as a check, not as a metric.
- The heterogeneity that is actually in force comes from the launcher's hardware probe
  (`deploy/hw_probe.sh`, `_is_allowed_judge`), which refuses to seat any judge model outside a
  configured allow-list and above a capability floor. That is real, and it is **not part of
  consensus**.

## 2. Why pinning one judge model is a worse remedy than the disease

The original assumption was that a heterogeneous jury would produce inconsistent verdicts and that a
single model would make them consistent. Measurement reversed it.

**Homogeneity does not remove judge error; it CORRELATES it.** A distributed slash run with five real
judges, all running the same small fallback model, hard-slashed **three of five honest miners** at
80 % of stake. Each of those slashes carried at least four of five **"invalid" votes that agreed with
each other** on an answer that was correct. The five instances did not fail independently — they
failed *together*, on the same pair, which is the "nine judges, two effective votes" effect: a
committee of identical judges has a fraction of the effective independence its size suggests.

That matters because the honest-biased veto — the guard that is supposed to make a false slash
impossible unless a large majority agrees — **rests entirely on the assumption that judge errors are
independent**. A homogeneous committee violates that assumption. The veto then does not protect; it
merely requires the same systematic error to be repeated, which a single model obligingly supplies.

Two further findings from the same measurements:

- **A static bench does not predict distributed behaviour.** The same fallback model scored 4 % salad
  and 1 % false negatives on a fixed-pair bench of several hundred cases. It false-slashed honest
  miners in distributed conditions anyway. A bench score is therefore **not** evidence that a judge is
  safe to seat.
- **The false-slash rate of a single-model jury was measured between 1 % and 19 %, and a heterogeneous
  mix brings it to zero.** Heterogeneity is not a nicety; it is the mechanism that closes correlated
  false slashes.

**Conclusion: an on-chain parameter naming a single judge model would make correlated false slashes a
consensus rule.** It would take the exact failure that heterogeneity exists to close and
write it into the protocol, where it could no longer be fixed by an operator changing a configuration
value. The remedy would be worse than the disease.

## 3. Decision

1. **Do NOT wire `audit_judge_model`.** It stays **inert**, and the proto carries an explicit note
   saying so. An inert parameter is less harmful than a parameter that enforces the wrong property, but
   a parameter that *looks* armed and is not is a trap for the next reader — hence the note, and hence
   this record.
2. **Stop claiming on-chain enforcement of judge heterogeneity.** The property is real and it is held
   **by operator configuration**. Saying otherwise is a false statement about consensus, and a claimed
   guarantee that is not held is worse than an absent one.
3. **The correct on-chain primitive, if it is ever built, is an ALLOW-LIST, not an identifier**: a
   `repeated string` of admissible judge models, refusing anything below the capability floor, plus a
   **floor on the number of distinct models** among the verdicts counted. That form is **deferred**, for
   the reasons in §5.

## 4. What holds the property instead

- **Heterogeneity by operator configuration.** The launcher probe seats a judge only if the model is on
  the allow-list and the machine clears the capability floor, and it selects between a
  mixture-of-experts judge on CPU and a mid-size dense judge on GPU according to hardware. Operators
  running different hardware therefore run different judges, which is what decorrelates the errors. This
  was proven sufficient in the runs that took the false-slash rate to zero. **It is an operational
  requirement of the launch kit, not a consensus rule**, and it must be stated that way.
- **A posteriori observability.** Every commit carries a `model_id`, including a verdict commit, and it
  is persisted. The **diversity of an audit committee is therefore auditable after the fact** by reading
  the verdict commits of a job. That is a genuine transparency property and it costs nothing.
- **The hardware probe as the capability floor.** A judge that is too weak does not merely judge badly:
  it votes divergent on answers that are correct but worded differently, and an unfair verdict costs an
  honest miner its stake. The probe refuses to seat such a model at all.

## 5. Why the allow-list form is deferred rather than adopted

Three reasons, in order of weight.

- **A declared model identifier is not a proof of execution.** `model_id` on a commit is supplied by
  the committing operator. When registry enforcement is on, the chain binds that identifier to a
  registered artefact by comparing the commit's `weights_hash` against the hash anchored in the
  registry — a real binding, and an honest one, but it binds a **declaration to an artefact**, not an
  execution to a model. A juror can therefore declare a permitted judge model whatever it actually ran.
  A distinctness count built on that field counts **declarations**, and a single operator can satisfy a
  diversity floor by declaring several model identifiers. The floor would raise the cost of a
  homogeneous jury without closing it.
- **Registry enforcement is itself dormant in the shipped genesis.** The `enforce_model_registry`
  parameter is off, so even the declaration-to-artefact binding is not currently active. Building a
  diversity floor on top of an inactive binding would produce a guard whose green state means nothing —
  the failure mode this project refuses.
- **It costs a protobuf regeneration**, which is a heavy workflow to be grouped, and the property it
  would buy is already held by configuration at the scale being launched.

Adopting it later is cheap and non-breaking: a `min_distinct_judge_models` defaulting to zero preserves
current behaviour exactly, and the natural failure mode is **no hard slash** (fall back to the light
path) rather than a halt — visible degradation, never a stopped chain, which mirrors how the other
floors in this system behave.

## 6. Alternatives rejected

- **Name a single judge model on-chain (the original D4).** Rejected: it writes correlated false slashes
  into consensus (§2).
- **Wire a distinctness floor now, on the existing `model_id` field.** Rejected for the moment: it
  counts declarations, over an inactive binding (§5). It becomes worth building once registry
  enforcement is armed and a measurement shows configuration alone is insufficient.
- **Remove the `audit_judge_model` field immediately.** Deferred, not rejected: removing a proto field
  requires the same regeneration as replacing it, so the two decisions should be taken together. Until
  then the field stays inert **and documented as inert**.

## 7. Consequences

- **No code changes.** No new parameter, no new check, no regeneration triggered by this record.
- **The public claim is corrected**: judge heterogeneity is an operational requirement met by operator
  configuration and verifiable after the fact through verdict `model_id` values. Nothing in consensus
  constrains which model a juror runs.
- **Two resolutions stay open**, to be taken at the next protobuf regeneration: drop the field and own
  the off-chain allow-list, or replace it with an on-chain allow-list plus a distinctness floor, which
  moves the guard into consensus — and which only becomes meaningful once `enforce_model_registry` is
  armed.
- **Accepted residual risk**: an operator fleet that converges on one judge model re-creates the
  correlated false slash, and nothing on-chain prevents it. The mitigations are the launch-kit
  requirement, the capability floor in the probe, and the after-the-fact observability of committee
  diversity. This is a known, bounded and disclosed gap, not a solved problem.

## Links

[ADR-026](ADR-026-llm-juge-onchain.md) · [ADR-027](ADR-027-decisions-execution.md) ·
[ADR-032](ADR-032-comite-audit-ancre.md) · [ADR-004](ADR-004-determinisme-impose.md)
