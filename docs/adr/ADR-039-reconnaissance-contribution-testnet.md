# ADR-039 — Recording testnet contribution, with a view to recognising it at mainnet

**Status:** Decided by the project owner. Supersedes the "no reward programme of any kind" framing that
[ADR-021](ADR-021-tokenomics-v5-native.md) and the public texts carried until now.
**Implementation:** Texts corrected. The durable snapshot, the payee journal and the frozen index are
**decided, not implemented** — each is listed below with what breaks if it ships late.

> **What this record decides is an INTENTION, not an entitlement.** It commits the project to recording
> contribution and to publishing that record durably. It does not commit it to launching a mainnet, to
> distributing anything, or to any amount, rate, ratio, date or eligibility rule. Those words are chosen
> and they are the decision, not a hedge around it.

> **Correction (2026-08-01) — the derived-identifier rule guarded ONE of the registry's TWO doors.**
> `CreateMiner` refused a chosen `miner_id`, while `InitGenesis` wrote `MinerMap` straight to the store
> and `GenesisState.Validate` checked nothing but duplicate keys. A genesis — hand-written, or carried
> over from an earlier chain — could therefore re-register `m-pc-01`, a hostname, or any string the
> message path exists to refuse: the same impersonation, the same permanent public leak, the same
> re-entry under a new name. A rule enforced on one of two paths is not a property of the registry.
>
> The check now lives in one function called from both doors, and from `InitGenesis` **in particular**:
> `InitChain` calls that, never `ValidateGenesis`, so a guard placed only in validation is one an
> operator skips by not running a command. It is proven in both directions and against three sabotages
> (disarming the import door, tolerating an absent creator, neutralising the comparison), each of which
> reddens a different, named set of cases.
>
> **Two consequences, stated rather than discovered.** A genesis exported by a pre-ADR-039 binary can no
> longer be imported — deliberately, since its identifiers are precisely what this record removes. And
> "one address registers exactly one miner" now holds on the genesis path too, for free: two entries
> sharing a creator derive the same identifier, which the duplicate check already refuses.
>
> It cost nothing to add because the launched genesis carries an **empty** `MinerMap`, measured on the
> live chain. The same change once operators have registered would have been a migration with real
> holders behind it — the window for making a guarantee total is before anyone relies on it.

## Context

The chain already records everything that matters: a job, its settlement, the verdicts on it and any
slash are written into the block that carries them. A stateless index recomputes a view of that record
from public state alone — it stores nothing, so there is no second ledger to trust, corrupt or lose.

Three facts make that insufficient on its own.

**A reset erases the record along with the balances.** The public texts say a reset wipes every balance;
they do not say it also wipes the only trace of who did the work. Nothing survives today.

**The payee disappears while the work does not.** `Job.Remove` has no caller outside tests, so job
history accumulates permanently. `Miner.Remove` has two — eviction for inactivity
(`x/jobs/keeper/miner_vitality.go`) and voluntary exit (`x/jobs/keeper/msg_server_miner.go`) — and the
miner record carries the `operator` address. A snapshot taken at reset would know that a given miner id
served N jobs and would not know whom to credit. The binding survives only inside the original
`MsgCreateMiner` transaction, i.e. in the blocks, and `MsgUpdateMiner` can change it: it is a time
series, not a constant.

**The public texts asserted the opposite, and one assertion was false on its own terms.** They stated
that no points programme "appears anywhere in this code" while `points_indexer.py` shipped in the public
repository under the title "Season 0" POINTS INDEXER. That claim was false independently of this
decision, and `CONTRIBUTING.md` went further: it *instructed* contributors not to document one.

## Decision

**1. The durable record is a triplet, not a leaderboard.** At named block heights the project publishes
(a) the block archive, (b) the state export, (c) the exact binary that produced both — each with its
SHA-256, in a tagged release, with a small manifest **committed** to the public repository so it survives
in every clone. The export alone would be theatre: nothing lets a reader recompute it without the blocks
and the binary, and `app_version` comes from build flags, so the export hash is not reproducible across
binaries.

The manifest carries `chain_id`, height H, the app-hash **read from the header of block H+1** (the app
hash of the state after H appears in the *next* header — an off-by-one here silently anchors nothing),
the validator and signature counts, and the limitation in plain words.

**The word is NON-REPUDIATION, never "verifiable by a third party".** An app-hash signed by a single
validator operated by the project is signed by the project. The snapshot is a dated commitment that
cannot be withdrawn afterwards; it is not evidence against its author. A reader who wants more must
replay the blocks with the published binary and compare the digests themselves. Saying otherwise would
be the same class of overclaim this project spent a release removing.

**2. What is recognised is the on-chain fact, not a score.** Jobs served, fees settled, verdicts
consistent with the outcome, participation in consensus — monotone quantities that a newcomer's arrival
does not move. A score moves under its holder: measured on the live chain, the sole registered miner
scores 80 % with all three anti-sybil caps already firing, and drops to **zero** when the only client
address is excluded, because the denominator starves. No ranking is published until the index counts
only **audited** jobs, stops scoring the raw `fee` (a number the payer writes), filters the judge term by
the **anchored** committee, and groups by `creator` (an authenticated signer) rather than `operator` (a
mutable string).

**3. The texts are corrected before any operator is recruited.** Nobody plugs in a GPU under a framing
that denies what the project is preparing. Those who believe it would under-participate; those who guess
otherwise would gain an edge. This ordering is the substance of the decision, not its packaging.

## Consequences

- A reset without a snapshot destroys work that belongs to other people. The reset script must **refuse**
  to run until a snapshot is written and checked — a warning in a runbook is not enough for an
  irreversible, silent action.
- The payee journal (`miner_id → operator → creator`, append-only) must exist **before the first miner
  eviction**, not at reset time. After an eviction the binding is only recoverable from the blocks.
- Pruning must be pinned. The re-export window closes at roughly 362 880 versions, and the failure would
  surface long after the cause.
- The index is renamed away from "Season 0": a season name implies a schedule and a successor, and it
  outlives every disclaimer placed next to it.
- The public record now states that a miner carrying a retained fee can neither exit nor be evicted, and
  that its bond stays locked until audits can conclude. That is fail-closed behaving correctly, but it
  binds an operator's capital and must be read before staking, not discovered afterwards.

## Alternatives rejected

**Keep the "no reward programme" framing and record nothing.** Rejected by the owner. It also required
maintaining a claim that was already false.

**Promise a distribution with a formula and a date.** Rejected: the formula is measurably broken at the
current network size, no mainnet is decided, and a promise of that shape changes what is being offered
in ways that belong to a professional's judgement, not an engineering record.

**Publish a live leaderboard now.** Rejected: with one participant the ranking is a portrait of the
project itself, and a published score creates an expectation that a stateless recomputation can revoke
at any time — a late clawback removes points that a reader already believed were theirs.
