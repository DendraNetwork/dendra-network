# ADR-060 — A season reward for an active judge, read from the seat, the sealed reveal and the signed report, never from the verdict byte: amends ADR-038

**Status:** Decided by the owner; implemented in the season service (ADR-047 decision 24), in force from the
first day the owner fixes. Written 2026-10-15. Records the owner's decision of
2026-10-15 (0.5 DNDR per active judge per day, paid off chain by the Final Testnet Season, in the same
rule set as the stale-kit refusal, after an amendment of [ADR-038](ADR-038-juror-incentive.md)). The
condition under decision 2 is this record's proposal, adopted with it; the sections *Why it pays neither
a mute judge nor a blind one* and *What it still cannot prove* are analysis.
**Implementation:** A season rule, off chain: a new rule set appended to
`services/final_season_rules.py::RULE_HISTORY`, the one that also carries the stale-kit refusal of
[ADR-058](ADR-058-kit-v022-judge-by-default-signed-releases-and-stale-kit-refusal.md) decision 5, deployed
before the first day it applies to and recorded as a decision of
[ADR-047](ADR-047-final-testnet-season-reward-programme.md) when it ships. Not consensus-breaking: no chain
code moves, no juror is paid or penalised on chain, and the guards of
`chain/x/jobs/keeper/adr038_juror_incentive_test.go` stay as they are.

## Context

- **ADR-038 decided no incentive, for a reason that has not moved.** A verdict is a signed byte
  (`chain/x/jobs/keeper/msg_server_commit.go::CreateCommit` checks the signer and nothing about an
  inference); the tally reads one token as the accusation and every other string as a vindication
  (`chain/x/jobs/keeper/antievasion.go::auditVerdictTally`), so "present and undecided" and "did no
  work" collapse on the same free byte, which is a valid vote (ADR-038 §2). §3(d) rejected a payment per
  vote (it buys votes), per participation (participation and voting valid are the same observable) and
  conditioned on the majority (it punishes a juror for being right). §4 decided to add nothing, on chain,
  and to guard the absence (`adr038_juror_incentive_test.go::TestADR038NoJurorIsPaidNorSanctioned`). §5
  lists what must exist first, starting with an abstention the alphabet can express, a consensus change
  this record does not make.
- **The season already pays a verdict, off chain.** ADR-047 (*Rewards*, Juror) pays `juror_per_verdict`
  for a verdict of a drawn juror on the audit of a programme request, committed before the audit's
  resolution and consistent with its outcome (`services/final_season_facts.py::timely_verdicts`,
  `services/final_season_facts.py::audit_outcome`; the rate in
  `services/final_season_rules.py::RULES_17` and its successors, summed by
  `services/final_season_calc.py::gross_of`). An unwound audit pays no juror. ADR-056 finding 1
  names what that is: a juror who votes with the expected outcome without judging is paid the form
  ADR-038 §3(d) rejected for the chain. The season's answer is the cap and the publication of the rule,
  not a proof of judgement.
- **A juror under the kit's guard votes valid, invalid for an incoherent answer, or abstains**
  ([ADR-057](ADR-057-judge-anchor-check-and-single-judge-model.md) decision 3). While the guard holds,
  most concluded audits conclude for the miner; a verdict consistent with the outcome is then, most of
  the time, a valid vote. Which audits concluded and how is a reading
  (`https://testnet-proof.dendranetwork.com/proof`, `resolved_by_quorum` and `resolved_by_adjudication`).
- **What the chain, the relay and the registry record about a seat.** The anchored jury of an audit,
  from the block event that drew it (`services/final_season_chain.py::audit_committee`); each
  verdict commit of a seated juror, dated by its accepted transaction
  (`services/final_season_chain.py::verdict_commits`); on the relay, one sealed reveal per
  anchored juror, under the key `<job>__<juror>__<primary>`, sealed to that juror's registered
  encryption key (`services/reveal_helpers.py::reveal_job`, ADR-057 decision 4), stored with its
  arrival time and write-once (`services/relay_store.py`, the record's field `t`;
  `services/relay.py::WRITE_ONCE`); in the capacity registry, a signed report per
  identity whose `judge_role` says `active`, `mute`, `off` or `unknown`, read inside the miner's
  container and covered by the operator's signature when there is one
  (`services/capacity_server.py::JUDGE_ROLES`, `services/capacity_server.py::_proven_miner_of`),
  and, from kit `v0.2.2`, its `kit_version` (ADR-058 decision 3).
- **Why the owner wants a presence reward at all.** From `v0.2.2` the judge is on by default and costs
  each machine disk and CPU time per audited job (ADR-058 decision 1); on the epoch of
  [ADR-059](ADR-059-consensus-epoch-15-jurors-are-declared-judges.md) only declared judges that vote are
  drawn. A per-verdict reward pays a judge in proportion to how often it is drawn, which on a small
  network is every audit and on a large one a few; the owner chose a flat daily amount for being there
  and judging, with a condition that is not the verdict byte.

## Decisions

1. **0.5 DNDR per active judge per day, paid by the season off chain** (owner, 2026-10-15). A new key
   of the rule set, `judge_active_per_day`, added to the identity's gross next to work, presence and
   verdicts (`final_season_calc.py::gross_of`), within the same daily cap and pro rata as every other
   reward and within the season cap, after the payout-address rule and before the daily pro rata
   (ADR-047, *Order of the rules*). It is paid in the rule set that carries the stale-kit refusal of ADR-058
   decision 5 and the probation of the plan's Q7, from the first day that rule set applies to, and to
   no earlier day. Points are the amount paid (ADR-047 decision 14), so this reward converts like the
   others.
2. **"Active", on a season day, is all of the following, each read from a record the judge did not
   write alone:**
   - *Seated.* The identity is named in the anchored jury of at least one audit of a programme request
     (a job whose client is the generator) whose jury was drawn on that day
     (`final_season_chain.py::audit_committee`).
   - *Given something to judge.* For that audit, the relay holds a reveal sealed to that identity, under
     the key `<job>__<identity>__<primary>` (`reveal_helpers.py::reveal_job`), stored before the verdict
     below. The primary writes that record, not the judge, and the relay refuses to replace it.
   - *It answered after its reveal arrived.* The identity committed a verdict on that audit, as a drawn
     juror, before the audit's resolution (`final_season_facts.py::timely_verdicts`, unchanged), and the
     block time of its commit transaction is later than the arrival time the relay recorded for the
     reveal sealed to it. The verdict's value is not read here. A verdict committed before its reveal
     existed, or on an audit whose reveal never reached that juror, counts for nothing: nobody can have
     judged what was not there. The arrival time is the field `t` of the relay's store record
     (`services/relay_store.py::write`); no public route of the relay serves it
     (`services/relay.py::do_GET` returns the sealed bytes, `/list` the keys), so the
     season service reads it from the relay's store on the network's host, where both run, and the
     ranking publishes it. This record adds no route to the relay.
   - *Not contradicted that day.* No verdict of that identity, on a programme audit whose resolution
     falls on that season day (the resolution height `final_season_chain.py::audit_resolution` reads,
     within the day's blocks), is inconsistent with the audit's outcome
     (`final_season_facts.py::audit_outcome`). The scope is the day of the conclusion, not every audit
     concluded so far: a conviction concluded on day N voids the judge reward of day N for the jurors it
     contradicts, whatever day their verdicts were cast, and a clean day N+1 is paid. An unconcluded or
     unwound audit contradicts nothing; the per-verdict reward already paid nothing for a contradicted
     verdict.
   - *Declared as a judge, under signature.* The identity's latest signed capacity report at or before
     the day's end declares `judge_role` `active` and a `kit_version` at or above `KIT_MIN`
     (ADR-058 decision 5); `mute`, `off`, `unknown`, an unsigned report, no report or no field pays
     nothing. Absent is unknown, never active (`capacity_server.py`, the comment above `JUDGE_ROLES`).
   The identity is paid once for the day whatever the number of audits it satisfies this on. The
   ranking publishes, per paid identity, the job id of one audit that satisfied the first four
   conditions, the reveal key, the arrival time the relay recorded, the height and block time of the
   commit, and the height of the report it read. A third party replays from that what is public: the
   seat and the commit on the chain, the key's existence on the relay's `/list` while the record lives,
   the report in the registry. The arrival time is the service's reading of the relay's store, published
   and not replayable from outside, and the ranking says so in its inputs. On the epoch of ADR-059,
   "seated" and "declared judge on chain" coincide; the report's role is still read, since it names the
   kit and the worker state the chain does not know.
3. **Nothing on chain pays or penalises a juror, and the season pays a dated participation.** ADR-038
   §4 stands for the chain and its three guards stay armed; `silence_slash_bps` stays inert and is
   marked as having no effect in the public texts (owner, 2026-10-15), without a vote. This record
   amends §3(d) in one respect only, and names what it pays: a participation, dated after the reveal,
   on a seat the identity did not draw itself. That is a per-participation payment, the form §3(d)
   rejects for the chain, since a juror that waits for its reveal and posts a byte without reading it
   satisfies the condition; what the owner accepted for the season is that form made flat, capped,
   revocable with the rule set and published with its inputs, which the chain cannot make it. The
   residue is written below (*a judge that posts valid without reading*, its second form), not removed.
   ADR-038 §5 is unchanged: an on-chain incentive still waits for an expressible abstention.
4. **The public texts change with the rule set.** `public-assets/README.md` (*Juror*), the season page
   and the announcement of `v0.2.2` say what is paid, on what condition, and that a verdict is a signed
   byte the condition does not read; they never say that a judge is paid for judging well.

## Why it pays neither a mute judge nor a blind one

- *A mute judge* is seated, receives its reveal and commits nothing: condition three fails, and the day
  pays nothing. A judge that is never seated on a day, because no programme audit drew it, is not paid
  that day either; on a day with no concluded or opened programme audit nobody earns this reward, which
  the ranking's inputs show.
- *A judge that posts valid without reading* has two forms. The one that posts as soon as it is drawn,
  before the primary's reveal reaches it, fails condition three by its own timestamp: the chain dates
  its commit, the relay dates the reveal, and the order is public. The one that waits for the reveal and
  then posts valid without running a model passes conditions one to three, like a judge that judged:
  the key it waits for is on `/list`, an open route (`relay.py::_snapshot_keys`), and nothing
  requires the reveal to be opened. It is caught only by condition four, on the day a programme audit it
  sat on convicts, and under the guard of ADR-057 decision 3 that happens on an incoherent answer only.
  That is the residue of ADR-038 §2, and this record does not remove it (decision 3 says so); what it
  does is make the reward flat and daily, so that the blind form earns the same 0.5 DNDR as the honest
  one and nothing more for a second, a tenth or a hundredth blind vote. The per-verdict reward of
  ADR-047 is where more votes earn more, and it is unchanged by this record.
- *A farm of declared identities* earns this reward once per identity per day, as ADR-056 finding 1
  says of every per-identity reward; the cost of an identity and the programme's caps bound it, and no
  cap per identity is added (owner, 2026-10-15). The condition adds nothing to the farm's cost beyond
  the identity and the wait for its reveal key: an identity that registered no encryption key still
  receives a copy sealed to the key the relay serves (ADR-057 decision 4), and a juror can post after
  the reveal's arrival without opening it. What the farm loses is the day of each identity contradicted
  by a conviction concluded that day.

## What it still cannot prove

- That a model ran. The condition proves a sealed artifact existed for the juror, that the juror
  answered after it, and that nothing concluded contradicted it. It does not open the juror's machine;
  ADR-035 is still the only route to a checkable judgement, and it is deferred.
- That the reveal was read. A juror can post after the reveal's arrival without opening it. The relay
  records that the reveal exists, not that it was fetched by its recipient; adding a fetch record would
  make the relay's reads attributable, which the per-route policy keeps open and anonymous, and this
  record does not change that.
- That the clocks agree. The relay's arrival time is its host's clock; the block time is the validator's
  clock (ADR-047 decision 17). Both run on the operator's hosts today; a skew between them moves
  condition three by that skew, in one direction only, and the ranking's inputs print both times.
- That a declared `active` role is true: it is a signed statement of the kit, and a modified client
  signs what it likes (ADR-058, *Declared versus proven*). The role gates the reward; the seat, the
  reveal and the dated verdict are what the identity cannot write alone.
- Whether 0.5 DNDR is the right size. ADR-038 §5 step 3 asked for data before sizing; the owner sized it
  on 2026-10-15 within the season's caps, and the ranking publishes what it pays, so the measurement
  exists from the first day the rule set applies to.

## Consequences

- A capable machine that keeps its judge on, opens its reveals and posts in time earns 0.5 DNDR a day
  from the first day of the rule set, within the caps; a mute one earns nothing from this reward and
  nothing from the per-verdict reward either.
- The season reads the relay's records for the first time. The relay's reveal store is already the
  evidence an audit concludes on (`relay.py`, its persistence requirement); the ranking's reader
  takes the record's key and its field `t` from the relay's store on the network's host
  (`relay_store.py::write`), read-only, and nothing else; the relay's routes and their per-route policy
  do not change, and the access bench `services/test_relay_access.py` is the proof of that.
- A day's judge reward is final with the day's ranking, like every other reward (ADR-047, *A day is
  ranked only once final*); a conviction concluded after the day's ranking does not reopen it.
- The announcement of `v0.2.2` names the amount, the condition in plain words, and the first day; the
  rule set's fingerprint is readable at `https://testnet-api.dendranetwork.com/final-season/v1/status`
  (`rules_fingerprint`) from that day on.
- When ADR-059 is live, a declared judge that stops voting leaves the jury pool and so stops being seated:
  this reward then stops by itself, with no rule to add.

## What this does not decide

- The first day of the rule set and `KIT_DEADLINE`: the owner's, with the announcement.
- The probation of new identities (ADR-058, *What this does not decide*), in the same rule set.
- An on-chain juror incentive: ADR-038 §5 unchanged.
- A per-verdict reward other than ADR-047's; the rates and caps of ADR-047 decision 22 are untouched.
- A reward for a judge on a day with no programme audit, or a reward computed from the kit's own
  judge log: the log is the judge's word.

## Pointers to add at merge

The code below is not in the tree yet; each item is cited by file and symbol once it lands.

- `judge_active_per_day` in the rule set at the end of `services/final_season_rules.py::RULE_HISTORY`
  and its term in `services/final_season_calc.py::gross_of`.
- The reader of the relay's reveal records and of the capacity registry's signed reports, and the five
  conditions, in `services/final_season_facts.py`; the published inputs in the day's ranking
  (`services/final_season_rank.py`).
- The bench: a seated judge with a reveal and a later verdict is paid; a verdict dated before its reveal
  is not; a mute seat is not; a contradicted verdict voids the day the conviction concludes on, and the
  next clean day is paid; `mute`, `off`, `unknown` and an absent report pay nothing; a day without a
  programme audit pays nobody; days ranked before the rule set replay unchanged
  (`services/final_season_payout.py::check_days`).

## Version

A season rule set, deployed on the network's host without a release number; no `kit_version` or
`consensus_epoch` change. Recorded as a decision of ADR-047 when it ships.
