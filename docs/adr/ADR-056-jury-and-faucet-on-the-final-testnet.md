# ADR-056 — Jury and faucet on the final testnet: what an identity costs, what a farm of identities can do, and what mainnet changes

**Status:** Records four decisions of the project owner, 2026-10-08 (decisions 2 to 5), and what a fifth
does, which [ADR-047](ADR-047-final-testnet-season-reward-programme.md) records as its decision 19
(decision 1 here). The findings and the list under *Open for the mainnet genesis* are this record's
analysis: they decide nothing. The question of a payout cap per miner stays open
([ADR-054](ADR-054-one-identity-per-card.md), decision 14): nothing here decides one.
**Implementation:** Per decision. Decision 1 is a governance change of the running testnet, effective from
its adoption. Decision 2 is a setting of the faucet service, applied when it is redeployed with it.
Decision 3 ships with the next `kit_version` ([ADR-050](ADR-050-release-versioning.md)). Decision 4
belongs to the mainnet genesis ([ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md)).
Decision 5 is a non-action. The corrections the findings below name live in the season's and the
faucet's code, and in ADR-047.

## Context

- **On this testnet an identity costs `min_stake`, which the faucet grants.** One address registers one
  miner, under an id derived from it (`chain/x/jobs/types/miner_id.go::DeriveMinerID`). Registering
  needs a bond of at least `min_stake` from the signer's balance and nothing else, with no limit on how
  often (`chain/x/jobs/keeper/msg_server_miner.go::CreateMiner`): the faucet grants the funds, and DNDR
  already held fund a registration just as well. Transactions cost no gas, since the network's nodes run
  with a zero minimum gas price (`docker/entrypoint-chain.sh`, `docker/node-join.sh`). The bond is
  refunded at exit, less what was slashed (`chain/x/jobs/keeper/msg_server_miner.go::DeleteMiner`).
- **The work draw and the jury draw do not weigh stake the same way.** The work draw caps each identity's
  stake at `assignment_stake_cap_multiple` × `min_stake`
  (`chain/x/jobs/keeper/committee.go::capAssignmentWeights`). The jury draw weighs each candidate by
  its whole stake, gives each identity at most one seat, and seats every eligible miner when they are fewer
  than the seats (`chain/x/jobs/keeper/audit_committee.go::drawAuditCommittee`,
  `chain/x/jobs/keeper/audit_committee.go::drawMembersWithDomain`). A juror is eligible on one commit
  per `juror_freshness_blocks` (`chain/x/jobs/keeper/miner_vitality.go::eligibleAsJurorInWindow`) and
  a proof of presence at the draw (`chain/x/jobs/keeper/presence.go::minerPresentAt`); neither needs a
  model, and the chain records no judge role.
- **A verdict is a signed byte.** A commit needs the operator's signature and no model
  (`chain/x/jobs/keeper/msg_server_commit.go::CreateCommit`). A slash needs at least the jury floor of
  voters (`chain/x/jobs/keeper/antievasion.go::effectiveSlashFloor`), at least
  `chain/x/jobs/keeper/antievasion.go::auditRelativeBar` invalid votes, the larger of that floor and
  ⌈2/3 of the anchored seats still registered⌉, and a strict majority of the voters' stake; a vindication
  needs the same, valid (`chain/x/jobs/keeper/antievasion.go::auditSlashDecision`,
  `chain/x/jobs/keeper/antievasion.go::auditVindicateDecision`,
  `chain/x/jobs/keeper/antievasion.go::auditVerdictTally`). The tally records how many distinct
  operators voted, and gates nothing on it. An audit that concludes neither way is deferred, then unwound
  past `audit_unwind_blocks`: the client is refunded, the miner neither paid nor slashed
  ([ADR-044](ADR-044-le-silence-du-mineur-ne-se-punit-pas-il-se-rend-sans-valeur.md)).
- **The season pays per identity** ([ADR-047](ADR-047-final-testnet-season-reward-programme.md)): work,
  presence and verdicts consistent with the outcome. What a farm of identities can collect is bounded by
  the programme's caps, not by any cap per identity.

An adversarial review of the season's economics on 2026-10-08 found the five mechanisms below. Each is
written with its cost, its gain, what is corrected and where, and what stays open.

## Findings

### 1. A farm of identities captures the jury

**Mechanism.** With every stake at `min_stake`, the stake majority is a head count. While the eligible
jurors are fewer than the seats (`audit_committee_draw_size`; absent or 0 selects the compiled size,
`chain/x/jobs/keeper/policy_params.go::effectiveAuditDrawSize`), nothing is drawn: every one of them
is seated (Context). With E honest eligible jurors and K identities of one farm, all seated, the farm holds
K of the K + E seats on every audit, and convicts when K is at least the jury floor and
K ≥ ⌈2(K + E)/3⌉, that is K ≥ 2E: it then holds the invalid votes the bar asks for, and the stake
majority. The same K vindicates the farm's own work. Above that size the seats are drawn, weighted by
stake.
**Cost.** K registrations at `min_stake`, funded by faucet grants or by DNDR already held, and no gas.
The farm's bonds are not at risk from its own convictions.
**Gain.** A conviction slashes the honest primary by `slash_leak_bps` of its stake
(`chain/x/jobs/keeper/antievasion.go::slashCheatedPrimary`) and the season does not pay the convicted
work; each convicting vote is a verdict consistent with the outcome, which the season pays; the farm's own
work can be vindicated whatever it was.
**Corrected.** Under decision 2 an identity the faucet funds costs a whole grant, and an IP address
obtains two grants a day. A verdict committed after the outcome is public is not paid (ADR-047,
*Consequences*).
**Open.** An identity still costs only `min_stake`, from a grant or from DNDR already held; a seat is still
one identity; nothing on chain links identities to an operator. A juror who votes with the expected outcome
without judging is still paid by the season, the form of payment
[ADR-038](ADR-038-incitation-des-jures.md) §3(d) rejected for the chain. Decision 1 does not touch any of
this (below). Lowering `slash_leak_bps` is set aside: the chain's validation bounds it from below in
optimistic mode, and it is the deterrent against a real cheat too. A structural remedy would be
consensus-breaking; what it could be is listed under *Open for the mainnet genesis*, undecided.

### 2. The audited share of the programme's work went unpaid

**Mechanism.** A settled job is drawn for audit with a probability of `audit_sample_bps` in 10 000
(`chain/x/jobs/keeper/audit_sampling.go::auditDraw`; the rate is one that
`chain/x/jobs/keeper/audit_sampling.go::effectiveAuditBps` can only raise). An audit with too few
eligible jurors defers (`chain/x/jobs/keeper/audit_jury_floor.go::deferAuditForSmallJury`) and
unwinds past `audit_unwind_blocks`. Under the rule set `RULES_17` the season never paid an unwound job
(`final_season_facts.py::job_is_final_positive`), and an unwind pays the miner nothing on chain. The miner
served the request, and nobody paid it.
**Cost.** The audited share of an honest miner's programme work. No attacker is needed.
**Corrected.** ADR-047, decision 18, from day 1: an unwound programme request is paid when its answer was
recorded and a grade clears it, its own answer graded coherent or, when its answer was not graded, a graded
day of its miner that is not voided (`final_season_facts.py::unwound_grading`).
**Open.** Day 0 stays ranked under `RULES_17` only if the service is deployed as decision 18 requires:
started with the new rules before day 0 is ranked and without `DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY`
set to 1, it fixes day 0 (ADR-047, decision 18). A request with no grade of its own and no graded day of
its miner is not paid, so a day the grader did not grade pays no unwound request. What decision 18 gives
up is finding 4.

### 3. The faucet's daily budget can be exhausted

**Mechanism.** The faucet refuses an address funded within `ADDR_COOLDOWN`, then a source IP address past
`IP_DAILY` grants in the day, then any grant past `DAILY_CAP` in the day, and records nothing before all
three pass (`services/faucet.py::_rate_ok`). The day's budget is therefore spent by
⌈`DAILY_CAP` / `IP_DAILY`⌉ distinct source IP addresses, whatever the order, each grant behind a proof of
work bound to its address.
**Cost.** That many IP addresses, and their proofs of work, once a day.
**Gain.** Nobody else obtains a grant for the rest of the day, so a newcomer holding no DNDR of its own
cannot register before the next day; and the grants fund identities (finding 1).
**Corrected.** Decision 2: one grant is `min_stake`, so the faucet's account funds one identity per grant,
and the quota per IP address is two a day, so more addresses are needed to spend a given `DAILY_CAP`.
**Open.** By construction, enough distinct addresses spend any daily cap; only the settings move the
number. `DAILY_CAP` is a setting of the faucet service (`DENDRA_FAUCET_DAILY_CAP`, read into
`services/faucet.py::DAILY_CAP`), and its value is not decided. A rig whose daemon funds
each identity from the faucet, with more cards than the quota behind one IP address, registers its further
identities on later days (ADR-054, decision 9).

### 4. A concentrated stake, or mute seats, block an audit; since decision 18 the season pays what they block

**Mechanism.** A conclusion needs `auditRelativeBar` votes in its direction and a strict majority of the
voters' stake. A juror whose stake outweighs the other voters' stakes together decides the stake majority
alone: voting against the majority of heads, it blocks the slash and the vindication alike, a veto. The
jury draw weighs the whole stake, so a large stake is also drawn more often. Mute seats block with no stake
at all: when fewer seats than the bar can still vote, nothing concludes. A seat stays drawable on a commit
per `juror_freshness_blocks` and a proof of presence, and silence is not penalised (decision 5). The audit
then defers and unwinds.
**Cost.** A seat, and nothing else; for the veto, a stake above the other voters' together, which on this
testnet is DNDR gathered into one identity, faucet grants among them.
**Gain.** On chain, none: an unwind pays the miner nothing (ADR-044). At the season level, from decision 18
of ADR-047, `work_per_request` on the unwound request when its answer was recorded and a grade clears it
(finding 2), including a request a jury would have convicted; and its miner escapes the slash.
**Corrected.** On chain, nothing. At the season level, the grading (the request's own grade first, else its
miner's day) and the programme's caps, which ADR-047 decision 18 names as the price it accepts.
**Open.** All of it on chain. What could remedy it is listed under *Open for the mainnet genesis*,
undecided.

### 5. Requests nobody answers

**Mechanism.** An identity can be registered and present with no model behind it, since presence needs
keys and no model (ADR-047). The work draw cannot tell, and draws it as the primary of programme requests:
no answer comes, and the job stays open until its escrow returns to the generator at expiry, after
`job_expiry_blocks` (`chain/x/jobs/keeper/policy_params.go::effectiveJobExpiry`; absent or 0 selects
the compiled `chain/x/jobs/keeper/job_expiry.go::jobExpiryBlocks`). Counted as one of the day's
requests, each such request took a share of the fixed daily volume from the miners that answer.
**Cost.** An identity and its proofs of presence; no model.
**Gain.** No pay: an unanswered request earns no work reward. It takes work from others, and on a draw
that weighs every identity alike (decision 1) each such identity takes its share of the draws.
**Corrected.** In the generator (ADR-047, *Work counts only if its answer reached the programme*): the
day's number counts answered requests, an unanswered request is replaced, and at most
`final_season_generator.py::ATTEMPTS_PER_REQUEST` × `requests_per_day` requests are sent a day.
**Open.** Time. Requests go one at a time, and an unanswered one holds the generator for the answer's
wait, the commit wait and a settlement that fails; enough such identities leave a day below its number,
which the generator's log says. A shorter wait or a second generator account would change that; neither is
decided.

## Decisions

1. **What `assignment_stake_cap_multiple` = 1 does, and what it does not** (ADR-047, decision 19, from its
   adoption by governance; read it with `GET /dendra/jobs/v1/params`).
   - *The work draw.* Every present identity bonded at `min_stake` or above weighs `min_stake`, so a larger
     stake no longer draws a larger share of the work
     (`chain/x/jobs/keeper/stake_cap_multiple_one_test.go::TestStakeCapMultipleOne_EqualisesTheWorkDraw`).
     It removes the stake that buys work; it does not bound how many identities an operator registers, and
     splitting becomes the form that always buys more work. Nothing on chain bounds that: a registration
     needs `min_stake` and nothing else (Context), and a transfer costs no gas, so DNDR from any source fund
     identities: earlier grants, a bond refunded at exit, job pay. The faucet's limits (decision 2) bound
     only the identities its own grants fund.
   - *The jury.* Nothing changes. The multiple applies to the work draw only; the jury draw and the tally
     keep the whole stake (Context). Findings 1 and 4 stand as they were, and the form decision 1 rewards
     for work, many identities at `min_stake`, is the form that holds seats.
   - *The slash.* The cap applies to the stake left after a slash, and nothing excludes a miner below
     `min_stake` from the work draw (`chain/x/jobs/keeper/committee.go::deriveCommitteeOrdered` filters
     on the frozen pool, vitality and presence only), while a bond cannot be topped up
     (`chain/x/jobs/keeper/msg_server_miner.go::UpdateMiner` refuses a stake change). So a slash of
     `slash_leak_bps` leaves unchanged the work share of a miner whose remaining stake is still at least
     `min_stake`; only a miner whose stake falls below `min_stake` draws less. A slash still takes the bond;
     it no longer costs work share to a miner that bonded enough above the floor.
2. **One faucet grant funds one identity** (owner, 2026-10-08; ADR-047, decision 20). The grant is the
   chain's `min_stake`, read from the chain (`DENDRA_FAUCET_AMOUNT=min_stake`,
   `services/faucet.py::drip_amount`); a `min_stake` that cannot be read, or reads 0, refuses
   the grant before any quota is spent. The quota per source IP address is two grants a day
   (`DENDRA_FAUCET_IP_DAILY`, read into `services/faucet.py::IP_DAILY`). The proof of work
   stays (`DENDRA_FAUCET_POW_BITS`, read into `services/faucet.py::POW_BITS`; 0 turns it
   off). It is bound to the receiving address (`services/faucet.py::pow_digest`) and the
   faucet keeps no spent nonce (`services/faucet.py::_pow_ok`): its cost falls on each new
   address, not on a repeated grant to the same one, which `ADDR_COOLDOWN` bounds
   (`services/faucet.py::_rate_ok`), and it does not limit what one grant funds. The
   faucet's probe says the grant and its source, not the quota.
3. **A machine without a GPU joins as a judge or not at all** (owner, 2026-10-08, for the next kit version;
   ADR-047, decision 21). Without a GPU and below `deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB` of system RAM, a
   machine does not join. With enough RAM it joins as a judge, and answers the requests the chain draws for
   it with the judge's model. No mining model runs on a CPU. The kit's judge already runs on the CPU, gated
   by that RAM and not by a GPU (`deploy/hw_probe.sh::JUDGE_ON_GPU`; ADR-054, *Context*); a machine that
   joins this way brings a judge behind its seat rather than a mute one, and serves its work with the model
   it has already loaded.
4. **`qwen3:14b` enters the model registry at the mainnet genesis only** (owner, 2026-10-08). It is
   registered in `x/modelregistry` when the mainnet genesis is written, and not on the testnet.
5. **A mute juror is not penalised, for now** (owner, 2026-10-08). The owner's reason: judging needs the
   system RAM `deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB` names, not a GPU. What the chain adds to it: it draws
   every eligible, present miner as a juror and records no judge role (Context), so a penalty for silence
   would fall alike on a miner whose machine cannot judge and on one that chose not to. ADR-038 stands
   there too (§3(c), §4). Its price is finding 4: a mute seat costs nothing.

## Open for the mainnet genesis

Not decided. The only mainnet item decided here is the owner's decision 4. What follows is what findings 1
and 4 point to, for the mainnet's parameters, which ADR-049 leaves open.

- *No faucet.* The mainnet genesis builder that ADR-049 lists as a work item carries no faucet account; an
  identity's bond would then be capital, which pricing the jury in stake assumes.
- *A juror role the chain can check.* Findings 1 and 4 come from seats that cost one identity each and need
  no judge behind them. A role an operator merely declares would cost as little as a seat does now; a role
  proven on chain would not. The chain draws jurors on vitality and presence alone. How a role could be
  proven is not designed; it would be consensus-breaking and enter at a genesis. ADR-038 §5 lists what
  must exist before any juror incentive, starting with an abstention a juror can express.
- *Model tiers.* Whether the mainnet's registry groups its models by tier, and how a tier would be proven
  and priced, is open.

## Set aside

- **Lowering `slash_leak_bps` against jury capture:** bounded from below in optimistic mode, and it weakens
  the deterrent against a real cheat as much as it softens a farm's conviction.
- **A penalty for silent jurors on this testnet:** decision 5.
- **Excluding a farm's identities from one another's juries:** the chain has no link between an identity
  and its operator (ADR-054, decision 13).

## Consequences

- On this testnet the jury is priced in identities, and identities in `min_stake`, which the faucet
  grants: the protection a jury gives is bounded by what an identity costs, not by the stake. Public texts
  have to say so, naming the mechanism and the parameters to read, never on which side of a threshold the
  network stands.
- Decision 18 of ADR-047 pays work that no jury judged; findings 1 and 4 are the reason its price is
  written next to it.
- A rig behind one IP address obtains at most the faucet's quota of grants a day, hence at most that many
  identities funded by the faucet; identities funded from DNDR already held are bounded by nothing on
  chain (decision 1).
- Decision 3 binds a machine through its kit, not through the chain, which cannot tell a CPU from a GPU. A
  kit of an earlier release gives a machine without a GPU a small model to mine with on its CPU
  (`deploy/hw_probe.sh` as tagged `v0.2.0`), and a kit behind the network is told, never refused
  (`deploy/join.sh::verify_kit_version`): decision 3 reaches a machine when its operator updates.

## Version

Decision 1 is a parameter change by governance: no kit or epoch change. Decision 2 is a redeployment of
the faucet service: no bump. Decision 3 changes what an operator runs and ships under a `kit_version` bump
whose release number [ADR-050](ADR-050-release-versioning.md) derives. Decision 4 belongs to the mainnet
genesis. `consensus_epoch` of the testnet does not move.
