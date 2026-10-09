# ADR-027 — Execution decisions

**Status:** Accepted. Settles five open decisions so that implementation can proceed without blocking.
**Implementation:** D1, D2 and D3 implemented as specified. D4 (a governed judge-model field in the
registry) exists as a proto field and a keeper getter but is **inert**: **no execution path reads it**,
and it must not be wired as defined — see [ADR-031](ADR-031-allow-list-modeles-juges.md). D5's
`hw_class` field is reserved in `x/modelregistry` and dormant.

## A structural finding that reduces new code

Reading the actual code: **committing an audit verdict requires NO new chain message.** A committee
member reuses `create-commit` with the key `"<jobId>__verdict__<minerId>"` and
`ResultCommit ∈ {"0","1"}`; `CreateCommit` extracts the miner id from the last `__`, so it accepts that
suffix, and the adjudication tally already counts those verdicts stake-weighted. Likewise, **discovery
of `+disputed` jobs reuses the existing `list-job` query**. **Only the reveal step requires anything
new**: a `reveal` namespace at the relay plus two off-chain worker processes.

## Decisions

**D1 — Index of `+disputed` jobs.** v1 is **`list-job` plus a worker-side `state` filter** (zero chain
code). A dedicated indexed query is only an optimisation at high volume, so it is deferred. *Reason:
do not regenerate the protobuf definitions for a need the existing query already covers.*

**D2 — Storage of batch items.** The N items live **off-chain**; only the **Merkle `dataset_root`** is
anchored on-chain. The audit committee retrieves the sampled items and their inclusion proofs through
the **same sealed channel as the reveal** in v1; a durable testnet moves to **content-addressed object
storage** referenced by the root. *Reason: the chain must never carry content, and the Merkle root is
enough to bind the sample to the batch that was paid.*

**D3 — Cross-batch deduplication.** v1 is **intra-batch deduplication only** (through the cosine
comparison with an inverted threshold). **Cross-batch** deduplication (an on-chain index of roots and
embeddings already paid for) is **deferred** on storage-cost grounds, and would need its own record if
a buyer required it. *Reason: avoid an unbounded on-chain index before the need is proven.*

**D4 — Judge model.** Recorded as a **governable registry field**, defaulting to a mid-size model, with
**all committee members using the same model**, on the assumption that different models would produce
inconsistent verdicts. *Reason given at the time: the judge is a security dependency, so it should be
versioned and consensual rather than each miner's choice.* **This decision was subsequently reversed by
measurement**: homogeneity is the hazard, because it makes judge errors correlated. The field stays
inert and the corrected reasoning is in [ADR-031](ADR-031-allow-list-modeles-juges.md).

**D5 — Datacenter TEE tier, timing.** **After** traction. The **cryptographic** confidentiality claim
is not the initial wedge; a **hardened Tier A** (OS confinement plus software attestation) is enough to
start. **But** reserve the **`hw_class`** field in the registry **immediately**, dormant, to avoid a
later protobuf regeneration. *Reason: decouple the premium claim (later) from schema preparation (now,
free).*

## Consequences

- Wiring the judge is delivered **mostly off-chain**: a reveal worker, a judge worker and a relay
  `reveal` namespace. What remains is **governance to activate** (`verification_mode=1`,
  `dispute_window>0`, `audit_sample_bps`) and **proving the slash live**.
- D4 implies a small registry addition (`audit_judge_model`, `hw_class`), grouped with the next
  protobuf regeneration.

## Links

[ADR-025](ADR-025-verification-optimiste-echantillonnage.md) ·
[ADR-026](ADR-026-llm-juge-onchain.md) · [ADR-031](ADR-031-allow-list-modeles-juges.md)
