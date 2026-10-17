# ADR-059 — Consensus epoch 15: jurors are declared judges that vote, a governed minimum kit version carried by the presence proof, and the proto debts settled in the same epoch

**Status:** Decided by the owner, not implemented in any release: the chain of the `v0.2.x` releases is
consensus epoch 14, and the code waits for `v0.3.0`. Written 2026-10-15; decisions 4, 8, 9 and 10 (the kit's watcher) and *What
this does not decide* amended 2026-10-16. Records the owner's decisions of
2026-10-12 (jurors are declared judges, on the testnet during the season; the chain is the fourth refusal
of a stale kit) and of 2026-10-15 (as soon as it is ready, with no deadline, developed in parallel with
kit `v0.2.2`; the proto debts and the "v3" fields in the same epoch). The sections *What the single
validator means* and *What this does not decide* are this record's analysis: they decide nothing.
**Implementation:** Consensus-breaking: `docker/CONSENSUS_EPOCH` moves from 14
to 15, the minor release number moves and the patch number returns to 0 ([ADR-050](ADR-050-release-versioning.md)
decision 2), so the release is `v0.3.0`. It reaches `dendra-testnet` by a governed upgrade
(`chain/app/upgrades.go::RegisterUpgradeHandlers`), rehearsed on a throwaway chain first
([ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md) decision 3). The deferral and unwind
semantics of `chain/x/jobs/keeper/audit_sampling.go::resolveDisputedAudit` and
`chain/x/jobs/keeper/audit_unwind.go::unwindStrandedRetention` are unchanged by this record, and
decision 8 says so.

## Context

- **Who is drawn as a juror today.** The jury of an audited job is drawn among registered miners of the
  frozen pool that are vital (a commit or a registration within `juror_freshness_blocks`,
  `chain/x/jobs/keeper/miner_vitality.go::eligibleAsJurorInWindow`) and present (a proof of
  availability in the current or the previous window when windows are armed,
  `chain/x/jobs/keeper/presence.go::minerPresentAt`), by stake, one seat per identity, up to
  `chain/x/jobs/keeper/audit_committee.go::auditCommitteeDrawSize` seats or the governed
  `audit_committee_draw_size` (`chain/x/jobs/keeper/policy_params.go::effectiveAuditDrawSize`); when
  the candidates are fewer than the seats, every one of them is seated
  (`chain/x/jobs/keeper/audit_committee.go::drawAuditMembers`). The chain records no judge role, and
  the comment above `auditCommitteeDrawSize` says why the draw is wide: nothing on chain distinguishes a
  miner that runs a judge from one that does not.
- **What a mute seat costs.** A conclusion needs `chain/x/jobs/keeper/antievasion.go::auditRelativeBar`
  votes in one direction, the larger of the jury floor and two thirds of the anchored seats still
  registered. A seat that never votes raises that bar and never helps reach it
  ([ADR-036](ADR-036-juror-vitality.md) §1 measured the form; [ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md)
  finding 4 the price). Below the bar an audit defers at each `audit_resolve_timeout`
  (`audit_sampling.go::resolveDisputedAudit`), and past `audit_unwind_blocks` it unwinds: the client is
  refunded, the miner neither paid nor slashed (`audit_unwind.go::unwindStrandedRetention`,
  [ADR-044](ADR-044-mute-miner-payoff-removed.md)). A jury smaller than the floor defers before any
  vote (`chain/x/jobs/keeper/audit_jury_floor.go::deferAuditForSmallJury`).
- **Vitality was built so that mining keeps a seat.** ADR-036 §3 refused an entry filter: a predicate
  that requires a recent verdict makes a new node ineligible for life. Its decision distinguishes
  "summoned and never voted" from "never summoned", and that distinction is the shape decision 2 below
  keeps.
- **A verdict is a signed byte.** `chain/x/jobs/keeper/msg_server_commit.go::CreateCommit` accepts
  a verdict commit from the operator of any registered miner; the tally counts those of the anchored
  jury (`chain/x/jobs/keeper/antievasion.go::auditVerdictTally`). Nothing here changes that:
  [ADR-038](ADR-038-juror-incentive.md) §2 holds, and this record pays nobody on chain.
- **The chain cannot refuse a kit.** `deploy/join.sh::verify_kit_version` warns; a kit of any release
  mines and sits on juries as long as its operator's key signs. [ADR-058](ADR-058-kit-v022-judge-by-default-signed-releases-and-stale-kit-refusal.md)
  decision 5 refuses a stale kit on the relay, the faucet and the season; the chain is the fourth
  refusal the owner asked for, and the only one that reaches the draw.
- **Three debts of the proto surface.** `rpc SettleJob` and its messages are still declared in
  `chain/proto/dendra/jobs/v1/tx.proto`, an authority-only vestige since epoch 14 (`docker/CONSENSUS_EPOCH`,
  item 3 of its epoch 14 note). `chain/x/emission/keeper/keeper.go::SecurityPool` is a telemetry
  accumulator of coins already paid, named like a claimable pool, which its own comment warns against.
  `docs/PLAN-REGEN-PROTO-V2.md` §5 lists fields waiting for the next regeneration: `avail_sample_bps` and
  `committee_min_vrf_power_bps` in the jobs `Params` and, as the bar in force, in the seed health
  query (its field 7, `contributor_power_bps`, already carries the latest contributor's share), and a `Pools` query of `x/emission`. A proto regeneration is one gesture (`make proto-gen`,
  `make proto-check`, with the toolchain `go.mod` pins), and the owner grouped all of it here.
- **The upgrade path exists and no plan has shipped in it yet.** On 2026-10-15
  `chain/app/upgrades.go::upgradePlans` is empty (a plan is a name in that list; the epoch 14 note
  of `docker/CONSENSUS_EPOCH` says why none is listed); the handler loop and the store loader are in
  place, and the file's header says what a plan does and does not do. Reading on a node:
  `dendrad query upgrade applied <name>` answers the height a named plan was applied at; while it never
  was, the height is absent, which reads as 0, not as an error (the SDK's `x/upgrade` keeper, `GetDoneHeight`, returns 0 with no error). ADR-049 decision 3 asks for one rehearsal on `dendra-testnet` during the season.
- **One validator produces the chain** ([ADR-046](ADR-046-single-validator-relaunch.md)). Reading:
  `curl -sf http://testnet-api.dendranetwork.com:26657/validators`, the length of `result.validators`.

## Decisions

1. **A judge is declared on chain.** A new message, `MsgDeclareJudge`, signed by the miner's operator
   like `MsgCreateCommit`, with the fields `miner_id` and `judge` (true or false). The keeper keeps, per
   miner, the declaration and its height (`MinerJudge`, `MinerJudgeDeclaredHeight`), the height of its
   last verdict commit (`MinerLastVerdictHeight`, written by `CreateCommit` when the key is a verdict
   key of an audit whose anchored jury names the miner), and a counter of draws it did not answer
   (`MinerMissedDraws`, decision 3). A miner declares `false` to leave the jury pool; exit and eviction
   clear all four. The declaration is free of gas like every transaction on this network, and nothing
   bonds it: the cost of a seat is still an identity (ADR-056 finding 1), and this record does not
   price it. The kit's judge switch sends the declaration (`deploy/testnet-miner/judge.sh on` declares
   true, `off` declares false, ADR-058 decision 2), and the miner daemon declares true at start when
   `DENDRA_MINER_JUDGE` is set and its judge worker holds the judge model.
2. **A juror is a declared judge that voted recently.** The jury draw (`drawAuditCommittee`) and the
   re-adjudication draw take as candidates the miners that pass today's filters (frozen pool, vitality,
   presence) and, in addition, are declared judges with a verdict within `judge_verdict_window_blocks`
   of the draw height, a new governed parameter of `Params` (0 selects a compiled default, as the four
   parameters of ADR-048 item 6 do; the compiled default is at least `juror_freshness_blocks`, and
   `chain/x/jobs/types/params.go::Validate` refuses a window below it when both are set). The
   window is counted in blocks, the unit the chain counts; a duration in days names its block interval
   assumption. The primary draw (`chain/x/jobs/keeper/committee.go::deriveCommitteeOrdered`) is
   unchanged: a judge is still drawn as a primary like any present miner, and answers with its model
   (ADR-056 decision 3).
3. **A judge drawn without voting loses its eligibility until it votes again.** Counted once per audit,
   never per deferral: at each of the four points where an audit leaves the disputed state, a real
   resolution of `resolveDisputedAudit`, `chain/x/jobs/keeper/msg_server_adjudicate.go::AdjudicateDispute`,
   `unwindStrandedRetention`, and the authority's `chain/x/jobs/keeper/msg_server_dispute.go::ResolveDispute`
   (every point that calls `chain/x/jobs/keeper/miner_vitality.go::clearAuditCommittee`), the keeper
   walks the anchored jury in its anchored order before it purges it and, for each seat with no verdict
   commit on that job, increments `MinerMissedDraws`. A verdict commit from an anchored juror resets its
   counter to 0 and writes `MinerLastVerdictHeight`; this holds for a commit made while the audit is
   open and its jury anchored, since `CreateCommit` reads the membership from the anchored jury and that
   jury is purged at the conclusion. A verdict posted after the conclusion is accepted as today
   (`CreateCommit` has no guard on a resolved job) and moves neither value, so a late blind vote on a
   dead audit buys nothing. A judge whose counter reaches `judge_missed_draws_max`, a new governed
   parameter (0 selects a compiled default), is not a candidate until its next verdict; a judge whose
   last verdict is older than the window is not a candidate either. Two reasons, one state: both end at
   the next verdict committed on an audit the judge is still anchored on. The ADR-036 invariant holds: a
   seat already anchored keeps its place in that audit's denominator, and nothing lowers a bar in flight.
4. **Trial seats, and the way back when no audit is open.** A judge declared within
   `judge_verdict_window_blocks` of the draw height is a candidate without a verdict, as
   `eligibleAsJurorInWindow` gives a new registration the vitality window before its first commit. The
   window is the trial; a new judge that misses `judge_missed_draws_max` draws inside it drops out like
   any other. No separate "trial seat" state exists: the declaration height is the only new date.
   - *The way back.* A judge out of the pool with no open audit it is anchored on has nothing to vote on:
     its concluded audits are purged (decision 3) and it is not drawn. It returns by declaring again:
     `MsgDeclareJudge` with `judge` true, from a declared or an undeclared state, rewrites
     `MinerJudgeDeclaredHeight` to the current height and resets `MinerMissedDraws` to 0, which reopens
     the trial window from that height. This is the one path for a judge whose window ran out on a
     quiet network, or that was never drawn inside it, or that came back after an outage once its audits
     had concluded; without it the exclusion is for life, which ADR-036 §3 refuses.
   - *What the reset costs, and why it is accepted.* An identity that never votes can hold at most
     `judge_missed_draws_max` concluded draws per declaration, then declare again: the counter moves at a
     conclusion, so the seats such an identity holds at once are those of the audits open before its
     draws conclude, and the cap bounds the concluded ones per declaration, not the open ones. The mute
     seat is bounded, not removed. The alternative, a counter that survives the declaration, is
     the exclusion for life. The bound is the governed parameter, and each declaration is a signed
     transaction with an event, so the count of declarations per identity is a reading
     (`dendrad query txs --events 'message.action=/dendra.jobs.v1.MsgDeclareJudge'`, filtered on
     `miner_id`), never a guess; the season's judge reward of ADR-060 reads a dated verdict after a
     reveal and pays nothing for a seat, so the loop earns nothing off chain either. Every other state
     of this record stays: a seat already anchored keeps its place in that audit's denominator.
5. **The jury size is the smaller of the draw size and the eligible judges.** This is the draw's
   existing behaviour (`drawAuditMembers` seats every candidate when there are fewer than `k`) applied to
   the new candidate set; `audit_committee_draw_size` and its compiled default stay what they are, and
   the safety net that raises the size to `audit_min_quorum` stays. Below the jury floor the audit
   defers as today (`deferAuditForSmallJury`); a network with fewer eligible judges than the floor
   concludes no audit, which is the state it is in today with mute seats, now visible in the committee's
   length. Reading: `GET /dendra/jobs/v1/audit_committee/{job_id}`, the length of `members`, on an
   audit drawn after the upgrade.
6. **The two-thirds bar does not move.** `auditRelativeBar` is unchanged in text and in callers:
   the larger of `effectiveSlashFloor` and two thirds of the anchored seats still registered, and the
   stake majority of `auditSlashDecision` and `auditVindicateDecision` stand. With a jury of declared,
   voting judges the same bar is reached by fewer seats that each vote; nothing is lowered.
7. **A governed minimum kit version, carried by the presence proof, so that a kit below it is neither
   drawn nor paid on chain.** `MsgProveAvailability` gains a field `kit_version` (the integer of
   `docker/KIT_VERSION`), and `Params` a field `min_kit_version` (0: dormant, every proof accepted as
   today). `chain/x/jobs/keeper/msg_server_prove_availability.go::ProveAvailability` refuses a proof
   whose `kit_version` is below `min_kit_version` when that parameter is above 0; a proof without the
   field declares 0, which is below any armed minimum. A refused proof writes no presence, so with
   availability windows armed (`avail_epoch_blocks` above 0, read with `GET /dendra/jobs/v1/params`)
   the miner is not present at the next window (`minerPresentAt`), hence drawn neither as primary nor
   as juror (ADR-048 item 5) and paid no availability reward (`chain/x/jobs/keeper/availability.go::payAvailability`).
   Work already assigned pays as it would have. With windows disarmed the parameter refuses proofs and
   filters nothing, which the parameter's comment says. The value is governed; the first value is the
   kit number of `v0.3.0` or a later release, set by a vote the owner announces ahead, with the notice
   the announcement names as for `KIT_DEADLINE` (ADR-058 decision 5), never in the upgrade handler.
   - *Declared, not proven.* `kit_version` is an integer the client writes into the proof and the
     operator's key signs: the signature proves who wrote it, not that it is true. An honest kit older
     than this record sends no field, reads 0 and is refused once the minimum is armed; a client built
     to lie declares what it likes, and nothing in this decision tells. The target is the honest, old
     kit, as for the three service-side refusals (ADR-058, *Declared versus proven*).
8. **The deferral and the unwind are unchanged.** `resolveDisputedAudit` keeps its branches: below the
   bar it defers, with the appeal deadline first when `allowAppeal` and `appeal_window` say so, and
   mutates the job on a real resolution only; `unwindStrandedRetention` keeps its order of checks, its
   dormancy at `audit_unwind_blocks` 0 and its refund of the client with the miner neither paid nor
   slashed. Decision 3 adds a read of the anchored jury and a counter write at the points where those
   functions already conclude; it moves no funds and changes no state those functions already write.
   The pinning tests of ADR-044 and ADR-048 stay green with their assertions unchanged, which is the bench
   of this decision; the draw fixtures of `chain/x/jobs/keeper/epoch14_test.go` and the shared miner
   fixtures declare their miners as judges, since under decision 2 a draw seats nobody otherwise.
9. **The proto debts, in the same regeneration.** `rpc SettleJob`, `MsgSettleJob` and
   `MsgSettleJobResponse` are removed from `chain/proto/dendra/jobs/v1/tx.proto` and the handler
   with them; the retired message is recorded in `chain/app/params_inert_test.go` next to the
   retired fields, so that neither name nor number comes back. `SecurityPool` is renamed
   `SecurityDistributedTotal` in `chain/x/emission/keeper/keeper.go`, in the genesis state and in
   the query that exposes it; whether the store key's bytes change is measured on the throwaway chain,
   and if they do the upgrade handler copies the item under the new key and deletes the old one. The
   "v3" fields of `docs/PLAN-REGEN-PROTO-V2.md` §5 land dormant: `avail_sample_bps` (0: every bonded
   miner must prove, today's behaviour) and `committee_min_vrf_power_bps` (0: the compiled threshold of
   `decentralized_seed.go` stays) in the jobs `Params`, the bar in force as `committee_min_vrf_power_bps`
   (field 8) in `QueryCommitteeSeedHealthResponse`, whose field 7 `contributor_power_bps` already carries
   the latest contributor's share, and a `Pools` query of `x/emission` with the two paid-total
   counters it needs. Each new parameter is 0 at the upgrade and arms by governance only; each is read
   by code in the same change, so that none is inert in the sense of ADR-038 §1(b).
10. **The migration.** One named plan in `upgradePlans`, applied by a `MsgSoftwareUpgrade` proposal at
    a height the owner chooses after the rehearsal. The handler runs `RunMigrations`; the jobs module's
    `ConsensusVersion` (`chain/x/jobs/module/module.go`) moves so that its migration runs, and the
    migration writes the new parameters at 0 and no other state: the new maps start empty, and a miner
    with no declaration is not a judge. No module is added or removed, so the store loader lists no
    store; the renamed emission item is the one possible exception, decided by the measurement of
    decision 9. The epoch number in `docker/CONSENSUS_EPOCH` moves to 15 with a note in its format; the
    kit of the same tree is `v0.3.0` (ADR-050 decision 2), carries the declaration in its daemon and its
    switch (decision 1) and the `kit_version` in its proofs (decision 7). `deploy/join.sh::verify_consensus_epoch`
    refuses a kit of another epoch until the node it starts answers `dendrad query upgrade applied
    <plan>` with a height (`--after-upgrade <plan>`). The automatic update of ADR-058 decision 3 stages a
    release of a higher epoch, verified, and the kit's watcher (`deploy/testnet-miner/node_upgrade_watch.sh`)
    moves the clone and the node image only when the chain halts at the plan height. A kit without the
    watcher (HiveOS with the automatic update off, a kit without its own node) moves by hand at the
    announced height, or by a new installation (amended 2026-10-16).
11. **Rehearsal before the network.** The upgrade is run first on a throwaway chain started from the
    testnet's genesis and a recent exported state, with a node on the old binary stopping at the
    planned height and restarting on the new one, until `last_block_app_hash` of `/abci_info` at the
    node agrees with the header at h+1 (the repository's fork guard bench, kept outside the public tree, three
    answers, `unknown` never read as agreement), `make proto-check` is green,
    the repository's jury floor guard is green from its root, and the
    benches of decisions 2 to 8 pass with their mutations. Then the same proposal on `dendra-testnet`,
    which is the rehearsal ADR-049 decision 3 asks for before the mainnet. The upgrade starts as soon
    as that is green, with no deadline (owner, 2026-10-15); a frozen week before the season's end is a
    proposal of the plan, not a decision of this record.

## What the single validator means

With one validator, one host carries the chain and the season. A handler that panics at the upgrade
height stops block production until the owner repairs it; a wrong migration that the rehearsal did not
catch forks nothing, since there is nobody to fork from, and corrupts the one state there is. Hence the
order: volumes backed up before the proposal (the chain's data volume and the validator's keys),
rehearsal on a throwaway chain with the real exported state, an adversarial pass with one reader per
lens (correctness, security, determinism, migration), and a final verification at the highest effort
before the proposal is submitted. The owner alone votes the proposal, which is the state ADR-046
accepted. The season's pay reads the chain; a halt stops the season day's blocks and not its rules, and
a day is ranked only once final.

## Consequences

- From the upgrade height every jury seat belongs to a judge that declared and votes. The bar is the
  same fraction of fewer seats, and a seat that stops voting leaves the pool after
  `judge_missed_draws_max` draws it did not answer, or when its last verdict ages past the window, and
  comes back by a verdict on an audit it is still anchored on or by a new declaration (decisions 3 and
  4). A network with fewer declared judges than the jury floor concludes no audit, as today, and its
  committees say so by their length.
- A miner that does not run a judge is no longer a juror and loses nothing else: its work draw, its
  presence and its pay are unchanged, and the seat it held and never filled is no longer counted
  against anyone.
- The season's juror reward (ADR-047, *Rewards*) and the judge presence reward of ADR-060 read the
  anchored jury as before; with judges only in the jury, "drawn" and "judge" coincide, which ADR-060
  notes as a condition it can then read on chain.
- A kit that did not stage `v0.3.0` stops at the upgrade height and is refused by `join.sh` until it
  moves; a kit of `v0.2.2` or later stages it by its daily update and its watcher moves it at the halt.
  The announcement of `v0.3.0` says what to do for the others, with the notice it names, as for `KIT_MIN`.
- `min_kit_version` armed makes an honest kit below it absent on chain (decision 7, *Declared, not
  proven*); the three service-side refusals of ADR-058 decision 5 stay in place, since the chain filters
  presence and not deposits, grants or pay.
- A declaration is as cheap as an identity, so a farm of identities can declare them all judges and
  still vote with the free byte (ADR-038 §2); the jury of judges removes the mute seat, not the capture
  ADR-056 finding 1 describes. What that costs is unchanged and still written there.
- The removal of `rpc SettleJob` breaks any client that still calls it; the only caller was the
  authority path the epoch 14 note calls a vestige, and the services are grepped for it before the
  regeneration.

## What this does not decide

- Whether a job's admission counts declared judges. `chain/x/jobs/keeper/pool_freeze.go::enoughEligibleMinersToVerify`
  still counts vital, present miners, so a job can open on a pool without a judge and defer until one is
  drawn (decision 5). Counting judges there is a separate decision of the owner.

- The values of `judge_verdict_window_blocks`, `judge_missed_draws_max` and `min_kit_version`: governed,
  set by proposals the owner announces; their compiled defaults are chosen when the code is written and
  named in the parameters' comments with the block interval assumption, never in this record.
- The upgrade height and date (owner, after the rehearsal; no deadline).
- A proof of judging on chain: a verdict stays a byte, and the jury of judges does not make the free
  byte dearer (ADR-038 §5 step 1 is still the prerequisite of any on-chain incentive; the season-side
  reward is ADR-060).
- A hash of the answer in the commit, which would bind a reveal to the delivered answer
  ([ADR-057](ADR-057-judge-anchor-check-and-single-judge-model.md), *What the anchor check does not
  prove*): not in this epoch.
- Several judge models per jury (ADR-057, *Open*).
- The mainnet's parameters and genesis (ADR-049, *Open*); if the upgrade has not run on the testnet by
  the season's end, this record's content enters the mainnet genesis instead, with a governed upgrade
  still rehearsed on the testnet before (ADR-049 decision 3), which is the plan's fallback and not a
  decision here.
- A second validator: none for now, on the testnet or at the mainnet launch (owner, 2026-10-15).

## Pointers to add at merge

The code below is not in the tree yet; each item is cited by file and symbol once it lands.

- `MsgDeclareJudge` in `chain/proto/dendra/jobs/v1/tx.proto` and its handler under
  `chain/x/jobs/keeper/`; the four maps in `chain/x/jobs/keeper/keeper.go` and their export in
  the genesis state.
- The candidate filter in `chain/x/jobs/keeper/audit_committee.go` and in the re-adjudication draw;
  the counter walk at the four conclusion points (every caller of `clearAuditCommittee`); the reset in
  `chain/x/jobs/keeper/msg_server_commit.go` and in the declaration handler.
- `judge_verdict_window_blocks`, `judge_missed_draws_max`, `min_kit_version`, `avail_sample_bps` and
  `committee_min_vrf_power_bps` in `chain/proto/dendra/jobs/v1/params.proto`, their readers in
  `chain/x/jobs/keeper/policy_params.go` and the invariant in `chain/x/jobs/types/params.go`.
- `kit_version` in `MsgProveAvailability` and the refusal in `chain/x/jobs/keeper/msg_server_prove_availability.go`.
- The plan name in `chain/app/upgrades.go`, the migration of `x/jobs`, the epoch note in
  `docker/CONSENSUS_EPOCH`, and the benches: declared judge drawn, mute judge dropped and returned by a
  verdict, a verdict posted after the conclusion moves neither the counter nor the last-verdict height,
  an excluded judge whose audits are all concluded returns by declaring again, a judge whose window ran
  out with no open audit returns the same way, trial window, jury length equal to the eligible judges,
  bar unchanged, refused proof, and the pinning tests of ADR-044 and ADR-048 unmodified.
- The declaration in `services/miner.py` and `deploy/testnet-miner/judge.sh`; the
  `kit_version` in the daemon's availability proof.

## Version

`consensus_epoch` moves from 14 to 15 and the release is `v0.3.0` ([ADR-050](ADR-050-release-versioning.md)
decision 2). The kit of that tree carries the declaration and the proof field. No mainnet gate moves:
`v1.0.0` stays the mainnet genesis.
