# ADR-047 — Final Testnet Season reward programme

**Status:** Accepted by the project owner, 2026-10-04; amended by four owner decisions the same day, by
nine more on 2026-10-05, three on 2026-10-06, one on 2026-10-07, decision 17, which ends the season at
a fixed time, four on 2026-10-08, decisions 18 to 21, one on 2026-10-11, decision 22, which doubles the
rewards and the daily cap from the fifth season day, one on 2026-10-12, decision 23, which raises the
number of requests a day from the seventh season day, and one recorded on 2026-10-16, decision 24, which
ships the owner's decisions of 2026-10-15 as one rule set: the judge presence reward of
[ADR-060](ADR-060-judge-presence-reward-amends-adr-038.md), the stale-kit refusal of
[ADR-058](ADR-058-kit-v022-judge-by-default-signed-releases-and-stale-kit-refusal.md) decision 5 and the
probation of new identities, from a day the owner fixes with the announcement of `v0.2.2` (*Owner decisions*,
below). This text applies them all, each from the point its decision names.
**Implementation:** Runs on the chain of [ADR-046](ADR-046-single-validator-relaunch.md) and needs the
presence rule of [ADR-048](ADR-048-epoch-14-consensus-batch.md). The rates and caps live once per rule set, in
`services/final_season_rules.py`. An amendment that changes the rules is a new rule set,
never an edit of the earlier one: the sets published so far are `final_season_rules.py::RULE_HISTORY`, and
each day is ranked and paid under its own (`final_season_rules.py::rules_for_day`,
`final_season_payout.py::check_days`). The formula is `final_season_calc.py::gross_of` and
`final_season_calc.py::compute_day`, and `final_season_rank.py` recomputes any published day from the evidence logs
and the chain, presence included (`final_season_chain.py::presence_proofs`). The programme service
(`final_season_server.py`) takes the payout declarations, keeps the sample of answers to programme requests and
its grades in the evidence log, and publishes the daily rankings; it sets no test and holds no key that
moves funds. The generator (`final_season_generator.py`) sends the programme's requests and forwards each
answer to the service's token-protected internal route before it settles the job; it and the weekly
payment (`final_season_payout.py`) sign from a programme keyring that no internet-facing service mounts.

## Context

On the chain alone, a miner's income is hard to predict and slow to arrive: the fee is retained until an
audit lottery, the subsidy has to be claimed by hand, the availability payment is set by `avail_payout_bps`
(read it with `dendrad query jobs params`), and jurors are paid nothing. Nobody can tell a newcomer what a
given card earns in a day.

The Final Testnet Season answers that question with a formula published in advance. It pays for facts the
chain records, filtered by the programme's own published evidence (the answers it received and their
grades): a request of the programme that a miner served, that survived its audit (or, from decision 18,
whose audit no jury concluded) and whose answer reached the programme, on a day its work was not voided by
its grades (the void rule below); a verdict on one of
the programme's own requests that matched the outcome; and the availability windows a miner proved on a
day it also did that work.

## Decision

**Duration.** The Final Testnet Season ends on 7 November 2026 at 23:59 UTC (decision 17). It counts
every block timestamped before 8 November 2026, 00:00 UTC — the chain's own block times decide
(`final_season_rules.py::RULES`, `end_time`, exclusive). Season days are 17 280 blocks each, counted from
the season's first block (about a day at about 5 s per block, an assumption re-measured on the running
chain); the last one is cut at the end. How many days that makes follows from the chain's pace: it is
read from the chain once the end has passed, never written in advance. The chain, the programme and the
desktop application start together.

**No registration.** Anyone running a miner takes part. Every reward is computed per miner identity; the
caps below bound the programme as a whole.

**Rewards**

| Reward | Rate | What proves it |
|---|---|---|
| Work | 0.10 DNDR per verified request (0.05 on days 0 to 3), the same for every miner | The project sends a **fixed number of answered requests a day** (`requests_per_day`, raised from the seventh season day by decision 23; a request whose answer does not reach the programme is replaced, below; on the season's last, shorter day, spread over the time left), which the chain hands out among its present miners, drawn by stake up to a ceiling per identity (decisions 10 and 19). A request counts on the block it is settled on chain, which must be in the season (decision 17), once past its audit, neither refunded nor slashed (or, from the first day decision 18 applies to, unwound by the chain with no verdict against it and graded as decision 18 requires), only on a job whose client is the programme's generator, and only if its answer reached the programme (below). It does not count on a day whose graded work answers are all incoherent, with at least min(2, answers sampled) grades (the void rule below) |
| Juror | 0.016 DNDR per verdict consistent with the outcome (0.008 on days 0 to 3) | The verdict is on chain, from a juror the chain drew, on the audit of a job whose client is the programme's generator, and its commit transaction precedes the audit's resolution (*Consequences*). An unwound audit pays no juror |
| Presence | 0.004 DNDR per availability window proven on chain, at most 50 a day (0.2 DNDR a day), **only on a day with at least one verified request** (0.002 and 0.1 DNDR on days 0 to 3) | The miner's daemon answers each window's challenge with a VRF proof (`MsgProveAvailability`); the ranking reads the accepted proofs from the chain's transaction index. A proof needs an operator key and a VRF key, no model: the verified request of the same day is what shows a model answered |

The rates above are those of decision 22, in force from the fifth season day. Days 0 to 3 pay half of each rate
and of the daily cap, and a published day is never recomputed under another rule set (*A ranking carries
its rules*, below).

There is no public-node reward, no model class and no exam (decisions 3 and 8 below).

**Work counts only if its answer reached the programme.** The generator forwards each answer to the
programme service before it settles the job (`client.py::quick_metered`, its `on_answer` hook;
`final_season_generator.py::run_one`). The service records the job id of every answer it receives, and the
day's seal lists them (the evidence record `work_sealed`, field `answered`); a programme job counts as a
verified request only if it is in that list (`final_season_facts.py::answered_jobs`,
`final_season_facts.py::chain_day_facts`). Settlement is permissionless: a miner can anchor a commit and
anyone can settle the job, so a job can be paid on chain with no answer delivered. Such a job is not
paid as work. The ranking of a day that received answers waits for their seal
(`final_season_server.py::rank_days`). The generator counts the day's number in answers: a request drawn on
an identity that does not answer is replaced by the next one, at most
`final_season_generator.py::ATTEMPTS_PER_REQUEST` × `requests_per_day` requests are sent a day, a forward
whose reply was lost is sent again, and the service answers a job it already holds as recorded
(`final_season_generator.py::report_work`).

**Caps**

| Cap | Value |
|---|---|
| Programme, per day | 100 DNDR from the fifth season day (50 DNDR before). Beyond it, every payable reward of that day is reduced by the same ratio (floor, in udndr), as announced in advance |
| Season | 3 000 DNDR over the season (1 500 DNDR before decision 22); a day's budget is the smaller of the programme's cap per day and what remains of the season cap, counting what every earlier day was paid |

There is no cap per identity (decision 11). The reason the owner gave then rests on the work draw weighing
stake; decision 19 changes that draw, and presence and jury seats were never bounded by it
(*Consequences*). Whether a payout cap per miner should follow is a question the owner left open on
2026-10-08. Both caps live in each rule set of `final_season_rules.py` (`cap_programme_day`, `cap_season`).

**Formula.** From the fifth season day (days 0 to 3 use half of each rate), with p = min(availability windows
proven that day, 50), gross = 0.10 × verified requests
+ 0.016 × verdicts + (0.004 × p if verified requests ≥ 1, else 0), computed in integer udndr
(`final_season_calc.py::gross_of`). Payable = the gross, or 0 when no payout address is known. Paid = the
payable, unless the day's total payable exceeds the day's budget: then every payable of that day is
reduced by the same ratio, floor in udndr (`final_season_calc.py::compute_day`). For example, 50 windows and
20 verified requests give 0.2 + 2 = 2.2 DNDR gross, paid 2.2 DNDR unless the day's total exceeds the day's
budget. Each published row carries `presence`, `verified_requests`, `verdicts`, `gross_udndr`,
`payable_udndr`, `paid_udndr` and, in words, the `reason` it is paid less than its facts would give
(`final_season_calc.py::to_json`).

**Payout address.** A reward goes to the address declared with the miner's command line
(`final_season_miner.py payout`); else to the operator that signed the identity's availability proofs that
day, the creator of its latest accepted proof (`final_season_chain.py::presence_proofs`); else to the
registry's operator, for an identity with no proof that day (a juror only, for instance). An identity
with no address known is payable 0, its reason "no payout address known": a row the weekly payment could
not pay would stop the whole week. The application shows the recovery phrase at first launch.
When the Creator and the Operator of an identity differ (owner mode,
[ADR-051](ADR-051-miner-keys.md)), only a declaration signed by the Creator is accepted. A declaration may
lock its address (`"lock": true` in the signed body, [ADR-053](ADR-053-cloud-and-hiveos-paths.md)): once
locked, another address is refused and the same one accepted, the lock survives a restart of the service,
and it cannot be lifted. A lock is accepted only as the identity's first declaration, or from its Creator in
owner mode when every earlier declaration of the identity was filed in owner mode too; otherwise it is
refused (409, `lock_refused`) and nothing is filed (`final_season_server.py::_payout`). Each payout record
says whether it was filed in owner mode, so that an `update-miner` sent today cannot rewrite the past; a
record without that mark, including every record filed before the mark existed, is not owner mode.

**Payment.** Computed by a public script that anyone can rerun on chain data and the published evidence
logs; published daily as a ranking per identity, each row with its payout address; paid weekly in DNDR from
the payout account funded at genesis, every seven season days. The last payment covers the days left after
the last full week, up to the season's last day, once that day is final
(`final_season_payout.py::plan_days`); that day is the ranking that names the season's last block
(`inputs.season_end_height`). Before
anything is planned, the payment checks every published day from the first to the end of that week
against the rules and pays nothing for the week if one breaks them
(`final_season_payout.py::check_days`): the rules fingerprint; each row's gross as the formula on the counts the
row publishes (`final_season_calc.py::gross_of` on its presence, verified requests and verdicts); its payable,
the gross if it has an address and 0 otherwise; 0 ≤ paid ≤ payable; the totals; the day's budget derived
from what the earlier days paid; the pro rata; and the season cap. These checks bound the loss, they do
not prove the facts: a compromised service can still publish false counts, and what it can misdirect is
bounded by the day's budget (`cap_programme_day`) and the season cap, not by any per-identity cap.

**Points.** A participant's points are the programme rewards actually paid to it: the sum of `paid_udndr`
over the published rankings (decision 14), so work, juror and presence rewards after the day's pro rata.
Faucet grants, transfers between addresses, on-chain job payments and the chain subsidy never count. Points
convert **one for one** (decision 15): each testnet DNDR the season paid becomes one mainnet DNDR, credited in
the mainnet genesis to the address that received it ([ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md)).
With the season cap of 3 000 DNDR (decision 22; 1 500 DNDR before it), the conversion is at most 3 000
mainnet DNDR.

**After the season.** Mainnet launches after the Final Testnet Season (decision 13), and each point
becomes one mainnet DNDR (decision 15).

## Owner decisions

The project owner decided decisions 1 to 16 before the season started, decision 17 on 2026-10-07, once
it had started, decisions 18 to 21 on 2026-10-08, decision 22 on 2026-10-11 and decision 23 on 2026-10-12.

**2026-10-04.** The review of the first implementation found four ways to be paid for something the
programme does not check.

1. *Superseded by decisions 5 and 6.* The proven windows of the programme's own question scaled the work
   and juror rewards, so that a farm of identities each proving one window would not collect them in full.
2. **The juror reward counts only audits of the programme's own requests.** Anyone can open jobs as a
   client, pay them to himself, and sit among the jurors drawn for their audits: the programme would then
   pay verdicts on audits its participants made for themselves. Only jobs whose client is the generator
   are counted, the same restriction the work reward has (`final_season_facts.py::chain_day_facts`).
3. **The public-node reward is removed.** The probe checked that a declared URL returned the network's
   `app_hash` at a given height, which a proxy in front of somebody else's node, or of a public RPC,
   answers as well as a node does; and a moniker is a free string. The reward paid for a URL, not for a
   node.
4. **Work answers are graded.** Below the jury floor no audit concludes, so the programme's work would be
   paid on requests nobody ever judged. The generator forwards each answer to a programme request, with
   its prompt, to the service's internal route (`/internal/work_answer`, the `X-Final-Season-Internal` token);
   once the day is over the service draws at most `final_season_server.py::WORK_SAMPLE` of them per identity,
   with its secret, into the evidence; the grader asks of each whether it is a coherent attempt to answer
   the request; and an identity whose graded work answers of a day are all incoherent gets no work reward
   that day, under the void rule below. (The same decision capped the model class at 2; decision 8
   removes the class.)

**2026-10-05.** Measured on a real card (*What was measured*, below), the programme's own tests proved
less than they were written for, and the owner removed them: no reward rests on an artificial test.
Decisions 10 and 11 set the ceiling of the stake-weighted work draw and the caps that follow from it;
decisions 12 and 13 name the programme and settle what follows it.

5. **The simultaneous question is removed.** Each availability window, the service sent every identity a
   personal story-writing prompt with a deadline, meant to show that a model answered for that identity.
   The topics were a public list and the identity's code word a name placed in the story, so stories
   written in advance answered it with no generation at all, and passed both the check and the grader;
   and one card answered for several identities at once. The question, its check, its grading and the
   per-address grouping it fed are gone from the rules, the service, the miner and the application.
6. **Presence is read from the chain, and paid only on a day of verified work.** The chain already
   records each availability window a miner proves, and draws only present miners for work (ADR-048).
   A proof needs no model, so it is paid only on a day the identity also served at least one verified
   request. The owner chose this knowing that it pays presence per identity: a farm that splits its stake
   into many identities, each with verified work that day, collects one presence reward per identity.
7. **A fixed volume of requests.** The generator sends `requests_per_day` requests a day (on the season's
   last, shorter day, spread over the time left: decision 17), set from the budget, instead of six per
   active identity: counted per identity, every identity a farm adds would have
   raised the programme's total of paid work. A fixed volume is shared by the chain's draw, weighted by
   stake — each identity's weight capped (*Consequences*).
8. **One rate for every miner, no exam.** The class exam measured a declared capability: a program that
   reads its task families solves them without a model, and a miner can route the exam to a model it does
   not run. The chain records no model a miner could be held to. Every verified request pays the same rate (0.05 DNDR
   since decision 15, 0.10 from the fifth season day by decision 22).
9. **No per-address cap.** It was fed by the addresses the question's answers came from; with the question
   gone it has no source.
10. **The work draw is weighted by stake, with the per-identity ceiling set as high as the chain
   accepts.** The chain draws the work among present miners, weighing each identity's stake up to
   a ceiling, `assignment_stake_cap_multiple` × `min_stake`
   (`chain/x/jobs/keeper/committee.go::capAssignmentWeights`).
   At the compiled multiple of 2, a stake split into many identities at the ceiling drew far more work
   than the same stake in one identity, and decision 7 alone did not prevent it. The launch genesis sets
   the multiple to 100 (`docker/entrypoint-chain.sh`), the highest the chain's parameter validation
   accepts (`chain/x/jobs/types/params.go::validateEpoch14`): up to 100 × `min_stake` an identity's
   weight equals its stake, so below the ceiling splitting a stake buys no more work — the draw keeps the
   lowest hash-over-stake score, which favours a larger stake more than in proportion on a small pool,
   and less as the pool grows, so a split stake draws less. Above the ceiling, splitting does buy
   more: several identities each at the ceiling weigh more than one. Its cost, chosen: a miner with a
   larger stake draws a larger share of the work.
11. **No cap per identity.** Asked whether to remove the cap per identity and keep only the programme's
   caps (then 500 DNDR a day, shared pro rata beyond it, and 15 000 DNDR over the season, divided by ten by
   decision 15), the owner removed
   it. The work an identity gets is the chain's stake-weighted draw over a fixed daily volume
   (`requests_per_day`, decisions 7 and 10), so a cap per identity would only pay an operator to split its
   stake into identities that each stay under it. `final_season_rules.py::RULES` carries `cap_programme_day`
   and `cap_season`, and nothing per identity.
12. **The programme is named the Final Testnet Season.** The owner asked that the programme no longer be
   called Season 1; asked for its English name and how far the renaming reaches (the texts only, what a
   reader sees, or the technical identifiers too), the owner chose "Final Testnet Season" and the
   technical identifiers too. Its technical identifiers carry the name: the modules `services/final_season_*.py`,
   the environment variables `DENDRA_FINAL_SEASON_*`, the services `final-season` and
   `final-season-generator` and their volumes, the route `/final-season/v1`, the data under
   `/data/final-season`, the header `X-Final-Season-Internal` and the page `/final-season/`.
13. **Mainnet launches after this season.** The owner stated that mainnet launches after this season.
   Asked what the texts should then say, given that they made mainnet conditional (an external security
   audit first, a network run by several operators first, no date), the owner chose: mainnet launches
   after the Final Testnet Season, with none of these conditions placed before it. The season's points
   then converted pro rata into 1 % of the mainnet supply; decision 15 replaced that rule.

**2026-10-06.**

14. **Points are the amount actually paid.** The owner was asked which figure of a ranking counts as
   points: the gross the formula gives, the payable amount (the gross when a payout address is known), or
   the amount actually paid after the day's pro rata. The owner chose the amount paid (`paid_udndr`). Points stay
   inside the programme's budget and its season cap, and a day shared pro rata counts what it paid.
15. **One mainnet DNDR per point, and a season budget of 1 500 DNDR.** The owner judged the rates too
   high for what a request is worth: one card could take about 100 of the programme's daily requests,
   and 0.5 DNDR each would pay it about 50 DNDR a day. Shown that the daily volume is shared by every
   present miner and that the caps bound the whole programme, the owner set the conversion to one mainnet
   DNDR per point, in place of 1 % of the mainnet supply shared pro rata, and the season's budget to
   1 500 DNDR. Every rate and cap is divided by ten, which keeps their ratios: 0.05 DNDR per verified
   request, 0.008 per verdict, 0.002 per presence window, 50 DNDR a day, 1 500 for the season. The daily
   volume of requests does not change. For scale: a mainnet job pays its miner 80 % of its fee, about
   0.007 DNDR at the fee the generator pays (`final_season_generator.py`), so 0.05 DNDR per request is
   still a launch bonus. Decision 22 doubles every rate and the daily cap from the fifth season day and sets the
   season budget to 3 000 DNDR.
16. **How the conversion is delivered.** Credited in the mainnet genesis, never by a later transfer or a
   claim: see [ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md).

**2026-10-07.**

17. **The season ends at a fixed time: 7 November 2026, 23:59 UTC.** It no longer lasts a fixed number
   of days counted in blocks. It counts the work and presence of every block timestamped before 8 November
   2026, 00:00 UTC, and of no later block (`final_season_rules.py::RULES`, `end_time`, exclusive;
   `final_season_rules.py::end_epoch`); the audit of a job settled in the season, and its drawn jurors'
   verdicts, still count when they conclude after the end. The time written in the signed block headers
   decides: BFT time, which on a chain with a single validator is that validator's clock. The season's
   last block is the last one
   whose header time is before that instant, found by a search over the header times that every node
   repeats identically (`final_season_chain.py::season_end_height`). Season days stay 17 280-block days counted
   from the season's first block; the day that holds the season's last block is cut there and counts only
   the blocks up to it (`final_season_rank.py::season_window`). How many days that makes follows from the
   chain's pace, which the consensus does not fix: it is read from the chain once the end has passed, and
   written nowhere in advance. A request counts on the block it settles, so the generator opens none in
   the last ten minutes before the end (`final_season_generator.py`, `STOP_MARGIN_S`), and on the last,
   shorter day it spreads that day's requests over the time left. After the end the programme service
   refuses new answers and new payout declarations (`final_season_server.py::_work_answer`,
   `final_season_server.py::_payout`): an answer filed then would join the last day's sample, and the
   addresses the season pays are those declared before its end. The ranking of the season's last day names its last block
   (`inputs.season_end_height`); the weekly payment stops at that day, so the last payment covers the days
   left after the last full week, once that last day is final (`final_season_payout.py::plan_days`). The
   rates, the caps, the daily volume, the weekly payment and the conversion do not change. The rules do,
   and so does their fingerprint: the payment refuses a day ranked under the earlier rules (*A ranking
   carries its rules*, below).

**2026-10-08.**

18. **The work of an unwound audit is paid.** The owner's words: "pay the work delivered". A programme
   request whose audit was drawn and unwound (no jury concluded within `audit_unwind_blocks`; the chain
   refunded the client and neither paid nor slashed the miner,
   `chain/x/jobs/keeper/audit_unwind.go::unwindStrandedRetention`, state `+resolved+unwound`, with or
   without `+disputed`) is paid by the season as a verified request when both hold: (a) its answer is in the
   requests whose answer the programme recorded in its day seal (`work_sealed.answered`,
   `final_season_facts.py::answered_jobs`); (b) the season's grading does not say incoherent: if its own
   answer was sampled and graded, that grade decides (coherent paid, incoherent never, whatever the rest of
   the day says); otherwise its miner's day decides, by the rule that takes a day's work away
   (`final_season_facts.py::_graded_out`): paid if the day has at least one grade and is not graded out
   (`final_season_facts.py::unwound_grading`). Never paid: an audit concluded against the miner (claw-back
   or slash); a request with no recorded answer; a request neither graded itself nor on a graded day; a
   request whose deciding grade may sit on an unreadable evidence line. Every unwound programme request of a
   day is listed under `inputs.unwound_audit_work` of its ranking (the rule, the first day, and per request
   `answered`, `graded` (one of `own_answer_coherent`, `own_answer_incoherent`, `day_coherent`,
   `day_not_graded_out`, `day_graded_out`, `not_graded`, `grading_unreadable`), `paid`, `why`). An unwound
   audit's verdicts pay no juror. Presence still needs one verified request a day; a request paid under this
   decision is one.

   **A new rule set, from one day.** `RULES_17` stay as published; decision 18 is the set `RULES`, two keys
   more and no rate, cap, volume or end changed: `unwound_audit_work`, and `finality`: a day is final
   `audit_unwind_blocks` + `audit_resolve_timeout` + 200 blocks past its last counted block (days under
   `RULES_17` stay final at `audit_unwind_blocks` + 200, as they were published, so they recompute
   identically; a day ranked before this decision that holds a concluded programme audit recomputes with
   dated verdicts, `inputs.verdicts`). The owner set it to apply from day 1: day 0 stays ranked under
   `RULES_17`, which pay no unwound request. The service fixes that first day once
   (`final_season_server.py::_unwound_from`): the day `DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY`
   names, else the first day not ranked yet, and never a day already ranked, since applying the rule there
   would rewrite a published ranking. Without the parameter, a first start after day 1 is ranked fixes the
   first day that is not; with the parameter naming a day already ranked, the first start stops the service
   with a fatal error that names the cause rather than move the day. Day 1 is therefore a condition of the service's deployment:
   started with these rules before day 0 is ranked, and without the parameter, it fixes day 0. The deployment
   passes `DENDRA_FINAL_SEASON_UNWOUND_WORK_FROM_DAY=1`, which the environment of the `final-season` service
   in `docker-compose.yml` names, and starts the service before day 1 is ranked
   (`GET /final-season/v1/ranking/day-001.json` still answers 404); later restarts read the recorded day and
   accept the same parameter. The day is recorded once, never rewritten, and published in the service's status as
   `unwound_audit_work_from_day`: read it there (`GET /final-season/v1/status`). Each ranking carries the
   fingerprint of its set; the payment checks each day against its own.

   **What this gives up.** [ADR-044](ADR-044-mute-miner-payoff-removed.md)
   holds that a jury's silence yields zero, never a free pass, and that abstaining gains no way to push a
   job through unaudited. On chain this still holds; at the season level it no longer does: an unwound
   request is paid as verified. Provoking an unwind costs silent jury seats: silence is not penalised (an
   owner decision recorded in [ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md)), a juror stays
   drawable on one commit per `juror_freshness_blocks` and a proof of presence each availability window,
   neither of which needs a model, an unwind slashes nobody, and enough silent drawn jurors block every
   conclusion, since a slash and a vindication each need
   `chain/x/jobs/keeper/antievasion.go::auditRelativeBar` votes in their direction: the larger of the
   jury floor (`chain/x/jobs/keeper/antievasion.go::effectiveSlashFloor`) and ⌈2/3 of the anchored
   seats⌉. A juror whose stake outweighs the other voters' stakes together blocks both outcomes too, since
   each also needs a strict majority of the voters' stake (ADR-056). The gain is
   `work_per_request` per request, including on requests a jury would have convicted. The only remaining
   check is the grading (its own answer when sampled, `final_season_server.py::WORK_SAMPLE` per identity per
   day, else its miner's day), which judges a coherent attempt and does not detect a substituted model; the
   exposure is bounded by `requests_per_day`, `cap_programme_day` and `cap_season`. A day the grader did not
   grade pays no unwound request, while its ordinary work stays paid: what this decision pays depends on the
   grader running.
19. **The work draw weighs every identity alike: `assignment_stake_cap_multiple` = 1 for the testnet.** The
   owner's words: "let us limit the stake to 1 for the testnet". It reverses the cost decision 10 chose. At a
   multiple of 1, `chain/x/jobs/keeper/committee.go::capAssignmentWeights` caps each identity's weight
   in the work draw at `min_stake`, so every present identity bonded at `min_stake` or above weighs the
   same, and a larger stake draws no larger share
   (`chain/x/jobs/keeper/stake_cap_multiple_one_test.go::TestStakeCapMultipleOne_EqualisesTheWorkDraw`).
   It is a change by governance, not by genesis: the owner submitted it on 2026-10-08 as proposal 1, on the
   expedited track. `MsgUpdateParams` replaces the whole parameter set
   (`chain/x/jobs/keeper/msg_update_params.go::UpdateParams`), so the proposal carries every parameter
   as read from the chain when it was built, with this one changed. If it passes, it executes at the end of
   the block in which its vote ends and applies to the work committees drawn after it; a committee already
   anchored keeps its draw (`chain/x/jobs/keeper/work_committee.go::anchorWorkCommittee`). Read whether
   it passed, and what the chain applies: `GET /cosmos/gov/v1/proposals/1` (`status`) and
   `GET /dendra/jobs/v1/params` (`assignment_stake_cap_multiple`; absent or 0 selects the compiled multiple
   of 2). A chain started again from the launch genesis would carry 100 again (`docker/entrypoint-chain.sh`
   writes it): the decision takes effect through the vote, not through the genesis. What it does not do,
   namely bound how many identities an
   operator registers or touch the jury draw, and what it does to a slash, is in
   [ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md).
20. **One faucet grant funds one identity.** The owner set the faucet's grant to the chain's `min_stake`,
   read from the chain (`DENDRA_FAUCET_AMOUNT=min_stake`, `faucet.py::drip_amount`), lowered its quota
   per source IP address to two grants a day, raised to ten on 2026-10-12 ([ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md),
   decision 2) (`DENDRA_FAUCET_IP_DAILY`, read into
   `faucet.py::IP_DAILY`), and kept its proof of work (`faucet.py::POW_BITS`). A fixed grant
   above `min_stake` funds several identities, since one address registers one miner and a transfer costs
   no gas. These are settings of the faucet service and apply when it is redeployed with them; its probe
   (`GET /` on the faucet) says the grant and where it comes from (`amount`, `amount_source`), not the quota.
   A `min_stake` that cannot be read, or reads 0, refuses the grant before any quota is spent. Faucet grants
   still never count toward points. Its limits: ADR-056.
21. **A machine without a GPU joins as a judge or not at all, from the next kit version.** A machine
   without a GPU and with less system RAM than `deploy/hw_probe.sh::MOE_CPU_MIN_RAM_MB` does not join; one
   with enough joins as a judge and answers the requests the chain draws for it with the judge's model; no
   mining model runs on a CPU. It ships with the next `kit_version` ([ADR-050](ADR-050-release-versioning.md))
   and binds a machine through its kit, not through the chain: a kit of an earlier release gives a machine
   without a GPU a small model to mine with on its CPU (`deploy/hw_probe.sh` as tagged `v0.2.0`), and keeps
   doing so until its operator updates, since a kit behind the network is told, never refused
   (`deploy/join.sh::verify_kit_version`). Why, and what it changes for the jury: ADR-056.

**2026-10-11.**

22. **The rewards double from the fifth season day, the programme's daily cap with them, and the season cap becomes
   3 000 DNDR.** The owner's words, translated: "for the next week, double the rewards directly on the
   testnet"; then "everything, and the day's cap until the end of the testnet"; then "the season cap adapted to
   the increase". Asked from which day, the owner chose "Day 4, today": the current day when the
   choice was made, not yet ranked, so no published ranking changes. Day 4, counted from 0 as the ranking files are
   (`ranking/day-004.json`), starts at block 69 121: the season's first block (1, `start_height` in the
   service's status) plus 4 × 17 280 blocks. From that day to the season's end:

   | Rule | Days 0 to 3 | From day 4 |
   |---|---|---|
   | `work_per_request` | 0.05 DNDR | 0.10 DNDR |
   | `juror_per_verdict` | 0.008 DNDR | 0.016 DNDR |
   | `presence_per_window` | 0.002 DNDR | 0.004 DNDR |
   | `cap_programme_day` | 50 DNDR | 100 DNDR |
   | `cap_season` | 1 500 DNDR | 3 000 DNDR |

   Nothing else changes. `presence_max_windows` stays 50, so presence is at most 0.2 DNDR a day for an
   identity; the ratios between the three rewards, `requests_per_day`, `day_blocks`, `end_time`, the formula,
   the void rule, decision 18 (`unwound_audit_work` and `finality`), the payout address, the weekly payment, the
   points and their one-for-one conversion are those of the sets before.

   **A new rule set, from one day.** `RULES_17` and `RULES` (decision 18) stay as published. `RULES_DOUBLED`
   joins `RULE_HISTORY`: `RULES` with the five values above and nothing else changed
   (`final_season_rules.py::RULES_DOUBLED`). A ranking carries the fingerprint of its set,
   `final_season_rules.py::rules_for_day` gives each day its set, and the payment checks each day against its own
   (*A ranking carries its rules*, below): a published day is never recomputed under another set, and a day of
   an earlier set cannot be paid at the new rates. The service fixes the first day under the new set once
   (`final_season_server.py::State`): the day `DENDRA_FINAL_SEASON_DOUBLED_FROM_DAY` names, kept in
   `doubled-rates.json` and read back at every later start. Unlike decision 18, the parameter has no fallback,
   and it is required until that record exists: a day number, or `none` for no doubled day. Absent or empty,
   the service stops: read as "no day", it would rank the next day at the old rates, and a published day is
   never ranked again. A parameter that names a day already ranked, a day before decision 18's, another day than
   the one kept (`none` included), or anything else stops the service with a fatal error. The payment does not
   take the day from the service: the payer names it (`final_season_payout.py --doubled-from-day`), and a week
   whose days under the doubled rates are not exactly those from that day on is refused
   (`final_season_payout.py::check_doubled_from`). Day 4 is therefore a
   condition of the deployment: the `final-season` service of `docker-compose.yml` receives
   `DENDRA_FINAL_SEASON_DOUBLED_FROM_DAY=4`, and has to be restarted with it before day 4 is ranked
   (`GET /final-season/v1/ranking/day-004.json` still answers 404). The day is published in the service's
   status as `doubled_rates_from_day` (null when there is none), next to `rules`, the rules of the current day:
   read it there (`GET /final-season/v1/status`).

   **The caps.** A day's budget is the smaller of its set's `cap_programme_day` and what remains of its set's
   `cap_season` after everything the earlier days were paid, whatever set they were paid under
   (`final_season_calc.py::compute_day`). The total paid therefore cannot pass 3 000 DNDR, and ADR-049 reads
   its conversion maximum from this cap. The payout account of ADR-046 is funded above that bound.

   **What this costs.** Decision 15 divided the rates by ten because one card could take a large share of the
   daily requests. The fixed volume still bounds the work: at most `requests_per_day` × 0.10 DNDR a day. Presence
   and jury seats are paid per identity and are not bounded by that volume (*Consequences*), so what bounds a
   farm of identities is the daily cap, doubled with the rates: an operator that adds identities takes a larger
   share of a day's budget, and beyond the cap every payable of the day is cut by the same ratio. Registering
   an identity costs `min_stake`, which the faucet grants (decision 20).

**2026-10-12.**

23. **More requests a day from day 6: `requests_per_day` becomes 900, at the same rates and caps.** The owner
   asked, translated: "can we raise the number of requests sent per day? 600 seems low, doesn't it?". Offered a
   volume, a first day and a go-ahead, the owner chose "900 (Recommended)", "Day 6" and "Yes, launch". Day 6,
   counted from 0 as the ranking files are (`ranking/day-006.json`), is "Season day 7" in the site's banner and
   starts at block 103 681: the season's first block (1, `start_height` in the service's status) plus 6 × 17 280
   blocks. On 2026-10-12 the service's status named day 5 as the current day (`day` in
   `GET /final-season/v1/status`), so no published ranking changes.

   **Why.** Under the doubled rates of decision 22, a day whose requests are all verified pays
   `requests_per_day` × `work_per_request` of work: 600 × 0.10 = 60 DNDR, against a daily cap
   (`cap_programme_day`) of 100 DNDR. At 900, the same day pays 90 DNDR of work before presence and juror rewards:
   the day's budget is used without lowering any rate. Beyond the cap, the pro rata applies as before.

   **A new rule set, from one day.** `RULES_17`, `RULES` and `RULES_DOUBLED` stay as published.
   `RULES_MORE_REQUESTS` joins `RULE_HISTORY`: `RULES_DOUBLED` with `requests_per_day` 900 and nothing else changed
   (`final_season_rules.py::RULES_MORE_REQUESTS`), and `final_season_rules.py::rules_for_day` takes its first day
   as a fourth argument, required like the others. The service fixes that day once, from
   `DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY`, under the contract of decision 22
   (`final_season_server.py::State`, its `_first_day_of`): kept in `more-requests.json` and read back at every later
   start, required until that record exists (a day number, or `none`, which is never recorded and is therefore
   required at every start), never a day already ranked, never before the first doubled day, refused when no day is
   doubled, never moved once recorded; and every published ranking is checked against the three first days before
   anything is written. The day is published in the status as `more_requests_from_day` (null when there is none),
   where `final_season_rank.py` reads it (or from
   `--more-requests-from`). Day 6 is therefore a condition of the deployment: the `final-season` service of
   `docker-compose.yml` receives `DENDRA_FINAL_SEASON_MORE_REQUESTS_FROM_DAY=6` before day 6 is ranked.

   **The generator takes each day's number from that day's set.** No figure of a ranking reads `requests_per_day`;
   the generator is what acts on it. It takes the number of the status's day from its own copy of the set the
   status's `rules_fingerprint` names, never from a number the network sends, and sends nothing while that set is
   unknown (`final_season_generator.py::day_quota`, `final_season_generator.py::main`); its bound on what a day
   sends, `ATTEMPTS_PER_REQUEST` × that number, follows it. A restart during day 6 resumes the day's count
   (`final_season_generator.py::load_sent`) and spreads what is left of the day's number over what is left of the day
   (`final_season_generator.py::spacing_s`).

   **The payment takes no new argument.** No amount the payment checks or pays reads `requests_per_day`: a row's
   gross is `final_season_calc.py::gross_of` on its counts, and a day's budget reads `cap_programme_day` and
   `cap_season` (`final_season_payout.py::check_days`). The same counts therefore pay the same under
   `RULES_DOUBLED` and `RULES_MORE_REQUESTS`, and a service that moved the first day of decision 23 could not make
   the payer pay more. The weekly command stays `--doubled-from-day 4`: a day under the new set carries the doubled
   rates and counts as a doubled day (`final_season_payout.py::check_doubled_from`), and a day under `RULES_DOUBLED`
   after one under the new set is refused like any return to an earlier set (`final_season_payout.py::check_days`).

   **What bounds it.** `cap_programme_day` and `cap_season` do not change (100 and 3 000 DNDR): a day's budget is
   still the smaller of the daily cap and what remains of the season cap (`final_season_calc.py::compute_day`), so
   the total paid still cannot pass 3 000 DNDR. The graded sample is per identity and per day: at most
   `final_season_server.py::WORK_SAMPLE` answers of an identity's day enter the evidence, drawn once the day is over
   (`final_season_server.py::seal_work_day`), whatever the volume, so the grader's work follows the number of
   identities, not the number of requests. Audits are drawn job by job at the chain's rate
   (`chain/x/jobs/keeper/audit_sampling.go::effectiveAuditBps`: `audit_sample_bps`, raised by
   `audit_probation_jobs` or `audit_adaptive` when either is set), so more requests draw proportionally more audits
   and juror verdicts, all paid within the same daily cap (read the parameters with
   `dendrad query jobs params -o json`). The generator still sends one request at a time (*Known limits*
   (e)), at most `ATTEMPTS_PER_REQUEST` × `requests_per_day` of them a day, and each escrows at most `BASE_FEE` +
   `PER_TOKEN` × (prompt tokens + `OUT_ALLOW`) udndr from the generator's account. That account is funded once, at
   genesis (`docker/entrypoint-chain.sh::GENR_AMT`, 3 000 DNDR); no code ties its balance to `requests_per_day`.
   Measured on 2026-10-12 at block 95 039, during day 5: the account held 2 980 627 020 udndr at account
   sequence 6 062 (`GET /rest/cosmos/bank/v1beta1/balances/<address>` and
   `GET /rest/cosmos/auth/v1beta1/accounts/<address>` on the public API, the address being `inputs.generator` of a
   published ranking).

**2026-10-16.**

24. **One rule set for the judge presence reward, the kit floor and the probation, from a day fixed with the
   announcement of `v0.2.2`.** It ships three decisions the owner took on 2026-10-15 (the work plan's questions
   Q2, Q3, Q7 and Q9): `KIT_MIN` is the kit number of `v0.2.2` (Q2, ADR-058 decision 5); an active judge earns
   0.5 DNDR a day, paid by the season off chain, on a condition that is not the verdict byte (Q3, ADR-060); a new
   identity earns 60 percent of its season pay during its first full day (Q7); and no cap per miner is added
   (Q9). ADR-058 decision 5 and ADR-060 decision 1 both ask that the three travel in one set, recorded here when
   it ships. `audit_probation_jobs` above 0, the chain's half of the probation, is a parameter the owner votes
   (`dendrad query jobs params -o json`), not a rule of this programme.

   **The set.** `RULES_KIT` joins `RULE_HISTORY` after `RULES_MORE_REQUESTS`: that set with three keys more and
   nothing else changed (`final_season_rules.py::kit_rules`): `judge_active_per_day` = 500 000 udndr,
   `probation_first_day_bps` = 6 000, and `kit_min`, the floor itself, so that the fingerprint of a day ranked
   under the set names the floor it was paid under. The floor is not written in the code: the service reads it
   from `DENDRA_KIT_MIN` at the start that fixes the set's day, records it with that day (`kit-rules.json` in its
   data, `final_season_server.py::State._kit_rules_from`), and at every later start the recorded floor wins (an
   environment that names another stops the service; an absent one reads the record). For `kit_min` = 3, the
   number `docker/KIT_VERSION` takes at `v0.2.2` from its 2 at `v0.2.1`, the fingerprint is
   `e297f5cabbfa7a28711e64ce89373bf15fadf72da3f01dca1290ece6ff2d438a`
   (`services/tests/test_final_season_kit_rules.py::test_the_published_sets_are_unchanged_and_the_kit_set_adds_three_keys_and_carries_its_floor`
   computes it); another floor is another fingerprint, and the one in force is read at
   `GET /final-season/v1/status` (`rules_fingerprint`, `rules.kit_min`). The rates, the caps, the daily volume,
   decision 18, the finality, the void rule, the payout address and the weekly payment are those of decision 23.

   **The judge presence reward** (ADR-060 decision 2, read as written). On a season day an identity is active
   when all five hold, each read from a record it did not write alone: it is named in the anchored jury of a
   programme audit whose jury was drawn within the day's blocks (`final_season_chain.py::audit_draw`); the relay
   holds a reveal sealed to it under `<job>__<identity>__<primary>`, whose arrival time the service reads in the
   relay's store, mounted read-only (`final_season_facts.py::reveal_arrivals`, the record's field `t` of
   `relay_store.py::write`); it committed a verdict on that audit counted as timely
   (`final_season_facts.py::timely_verdicts`, unchanged) whose block time is later than that arrival; no verdict
   of its own on a programme audit resolved within the day's blocks is inconsistent with the outcome
   (`final_season_facts.py::contradicted_jurors`), the audits of the days before being read for their resolution
   up to the finality span; and its latest signed capacity report at or before the day's end declares
   `judge_role` `active` on a kit at or above `kit_min`. The five are `final_season_facts.py::judge_presence`;
   the reward is one term of `final_season_calc.py::gross_of` (`judge_active`, 0 or 1), within the same daily cap,
   season cap and pro rata as every other reward, after the payout-address rule and before the pro rata, and paid
   once a day whatever the number of audits. An audit with no indexed resolution (an unwound one included)
   satisfies nobody; a day without a programme audit pays nobody; a store the service cannot read pays no judge
   that day and the ranking says so (`inputs.judge_presence.relay_store` = `?`), while every other reward is
   ranked as usual. The ranking publishes, under `inputs.judge_presence`, each seated identity's conditions and,
   for a paid one, the job id, the reveal key, the arrival time, the commit's height and block time and the
   height of the report it read (`final_season_rank.py::rank_day`); a reader without the store replays those
   arrival times from the published ranking (`final_season_rank.py --compare` without `--relay-store`). The
   ranking says itself what those times are, under the fixed key `inputs.judge_presence.reveals_source`
   (`final_season_rank.py::REVEALS_SOURCE`): the service's reading of the relay's store, which a replay copies
   and does not verify, as ADR-060 decision 2 asks.

   **The capacity reports.** The registry keeps each identity's latest report only, and a day is ranked more
   than a day after it ends, so the service files in its evidence, as `capacity` records, each signed report's
   kit number and judge role whenever one of them changes, with the chain height it read them at, read once per
   pass (`final_season_server.py::watch_capacity`, from the registry's store mounted read-only), and the ranking
   reads the latest record at or before the day's last counted block (`final_season_facts.py::kit_report`). A
   report that stopped coming is still the latest; an unreadable registry is reported and retried, and the start that
   fixes the set's day refuses one it cannot read (ADR-058, *Consequences*). A day under the set whose evidence
   holds no `capacity` record at or before its last block is not ranked (`final_season_rank.py::rank_day` raises,
   the service defers and says so): the registry was not watched during that day, every identity would read no
   signed report and be payable 0, and a published day is never recomputed. For the same reason the set's first
   day must not have begun at the start that fixes it (below).

   **The kit floor** (ADR-058 decision 5). From the set's first day an identity whose latest signed report at or
   before the day's end declares `kit_version` below `kit_min`, or that has no signed report, or whose report
   carries no number, is payable 0 for the day, its gross published as the formula gives it and its reason
   naming the floor (`final_season_calc.py::kit_allows_pay`); the identity takes no share of the day's budget.
   Each row publishes the kit it was read with (`kit_version`, null when none), and `inputs.kit_floor` the floor
   and each ranked identity's report.

   **The probation** (Q7). An identity's first full season day is the first day whose first block comes after
   the block of its first accepted registration in the season (`final_season_chain.py::miner_registration`,
   searched in the index from the season's first block to the day's last, by the identity's own signer: the
   miner id is derived from the operator's address, ADR-039, so the signer of its `MsgCreateMiner` is its
   operator; `final_season_facts.py::first_full_day`): registered in the last block of day N, its first full day
   is N + 1; registered in the first block of day N + 1, it is N + 2. The first, not the latest: an identity that
   leaves and registers again under the same derived id is the same identity, and its first full day does not
   move with the re-registration (otherwise a delete and a create at the start of each day would be paid in full
   every day, `msg_server_miner.go::DeleteMiner` refunding the bond at once). On that day, and that day only,
   its gross is 60 percent of the formula, the judge reward included, floor (`final_season_calc.py::gross_of`,
   after every other term). An identity registered before the season, or whose registration the index does not
   hold, has day 0 as its first full day, so the probation never touches a published day. A registration that
   cannot be dated, because the identity's own registrations in the season outnumber what the search reads
   (`final_season_chain.py::SearchTooLong`, the identity's doing) or because no operator address is known to
   search by (an identity gone from the registry with no proof and no verdict that day), is neither before the
   season nor today: the identity is payable 0 for that day with its reason, the day is ranked without it
   (`final_season_calc.py::registration_allows_pay`; before, one such search stopped every ranking), and the
   ranking names the cause under `inputs.probation.not_dated`. Each row publishes `probation` and
   `registration_not_dated`, and `inputs.probation` the registration height and the first full day of each
   ranked identity. No cap per identity is added (Q9).

   **A new rule set, from one day.** The four sets before stay as published (their fingerprints are asserted by
   the bench above, so a change to any of them is a red bench, not a silent recompute). The service fixes the
   first day once, from `DENDRA_FINAL_SEASON_KIT_RULES_FROM_DAY`, under the contract of decision 23
   (`final_season_server.py::State._first_day_of`): required until the record exists (a day number, or `none`),
   never a day already ranked, never before decision 23's day nor without one, never moved once recorded; and
   with a day, `DENDRA_KIT_MIN`, `DENDRA_FINAL_SEASON_CAPACITY_DB` and `DENDRA_FINAL_SEASON_RELAY_STORE` are
   required (`docker-compose.yml`, service `final-season`, mounts `relay-store` and `capacity-db` read-only). One
   rule more than decision 23, because this set reads a registry as the days go by: the day must not have begun
   at the start that fixes it (`final_season_server.py::State._kit_rules_from` reads the chain's height, refuses
   a day at or before the current one, and records the height in `kit-rules.json` as `fixed_at_height`; a chain
   it cannot read fixes nothing). The day is published as `kit_rules_from_day`, where `final_season_rank.py`
   reads it (or `--kit-rules-from`). The day itself is the owner's: the first season day that begins at or after
   `KIT_DEADLINE` (ADR-058 decision 5), named with the announcement; this record writes no date.

   **The payment takes no new argument.** `RULES_KIT` carries the doubled rates, so its days count as doubled
   days and a week may mix days of decision 23 and days of this set under `--week N --doubled-from-day 4`
   (`final_season_payout.py::check_doubled_from`); a day under an earlier set after one under this set is
   refused like every return (`final_season_payout.py::check_days`). What the counts alone do not give, the row
   publishes (`judge_active`, `probation`, `kit_version`, `registration_not_dated`), and the payer recomputes
   each row's gross and payable from them, the judge term inside the same pro rata as the rest; a row that
   carries one of the four under an earlier set is refused, and so is a flag published as a number (1 is not
   `true`). The payer needs no floor of its own: it takes it from `inputs.kit_floor.kit_min` of the first day
   published under the set, checks that the set built from it has the day's fingerprint, and holds every later
   day to it (`final_season_payout.py::_set_of_day`).

   **The faucet** (ADR-058 decision 5). From `DENDRA_KIT_DEADLINE`, with `DENDRA_KIT_MIN`, a grant request whose
   JSON body declares `kit_version` below the floor, or declares none, is refused with 403 and a message naming
   the update, before the proof of work, the amount and the quotas (`faucet.py::H.do_POST`, before
   `_rate_ok`); refusals are counted in the log. Both variables unset, the floor is dormant; one without the
   other refuses to boot (`faucet.py::kit_policy_from_env`). The two variables reach the daemon only if
   the service's line in `docker-compose.yml` names them (compose passes nothing it does not name; the bench
   `dendra_saison_vague1_test.sh` checks the line against what the daemon reads). The field is the one the
   relay's signed deposits carry (`modea/relay_write.py::KIT_VERSION_FIELD`); the kit writes it when it asks for
   a grant, and until the kit that joins does, the floor stays dormant on the faucet: armed before, it would
   refuse every joining kit, honest ones first.

   **What this does not prove.** What ADR-060 lists: that a model ran, that the reveal was read, that the relay's
   clock and the validator's agree, that a declared `active` is true. A kit number is declared, not proven
   (ADR-058, *Declared versus proven*), and it persists: a signed report that declares a kit at the floor once,
   and never again, is the identity's latest report on every later day, and pays as such; what a day reads is
   `declared`, never `current`. The reveal records the judge reward reads live in the relay's store for its
   retention (`relay.py`, `RETENTION`, compiled default seven days, never below one day; the compose
   names no variable for it, so the deployed relay keeps the default); a reveal evicted before the day is ranked
   reads as no reveal, not as unknown, so a retention set shorter than a day's ranking delay (its last block plus
   the finality span) would pay no judge for that day and the ranking would say `read`. The judge reward's seats
   are read among the programme audits settled within the finality span before the day; an audit of a job
   settled earlier whose jury were drawn that day is not read, and the ranking's `inputs.judge_presence.audits`
   lists what was.

**The void rule.** A day's work is taken away only when every graded work answer of the day was judged
incoherent and there are at least min(2, answers sampled) grades (`final_season_facts.py::_graded_out`,
`GRADES_TO_VOID`); its presence goes with it, being paid only on a day of verified work. No grade never
voids anything.

## What was measured

Measured in October 2026, before the season, on one 8 GB consumer GPU with `llama3.1:8b-instruct-q4_K_M`
(the miner's default), `llama3.2:3b` and `qwen2.5:1.5b`, through Ollama with the miner's own generation
options. These measurements are why decisions 5 and 8 were taken, and the reason the miner keeps its model
loaded.

- **One card, several identities.** Generating the window answers concurrently, the 8B finished four
  inside the deadline, the 3B eight in less than half of it: the window bounded the identities of a card by
  its speed and its model, never to one.
- **Prepared answers.** The grader judged coherent every honest answer it was shown, and the same stories
  would have read as coherent had they been written the day before. Nothing in the check or the grader
  could tell an answer written at window time from one written in advance.
- **The exam.** With room to reason, the 8B reached class 2 in all three end-to-end attempts and the 3B in
  one of four; estimated from the measured rates, the 3B reached it in about one attempt in seven. The
  separation it measured was real but was measured on the miner's word: nothing tied the exam to the model
  that serves the work.
- **Loading a model.** Loading the 8B and getting its first reply took 69 s while the disk was busy. The
  miner's Ollama keeps its model loaded for an hour (`deploy/testnet-miner/docker-compose.yml`), so a
  request the chain draws for it is not served behind a reload.
- **The work grade, after decisions 5 to 11.** On the same card, with the grader's
  default model (`llama3.1:8b-instruct-q4_K_M`) on 40 requests composed by
  `final_season_generator.py::make_prompt`, answers served with the miner's options
  (`final_season_grader_measure.py` replays it): the 8B's answers were graded coherent 40 times out of 40,
  the 3B's 40/40 and the 1.5B's 38/40. Random words, the answer to another request, one sentence
  repeated, the request echoed back and a polite refusal were each graded coherent 0 times out of 40, so a
  day served that way is voided. The first quarter of an honest answer passed 20 times out of 40; no 8B
  answer reached the generator's output cap (the longest was 459 tokens of 768). The grade does not tell a
  small model from the 8B, as decision 8 already says.

## Consequences

- **What a farm can and cannot do.** The volume of paid work is fixed, and the work draw weighs stake up to
  `assignment_stake_cap_multiple` × `min_stake` per identity (decisions 10 and 19): below that ceiling a
  farm's share of the work follows its stake, and splitting it does not raise it; above it, splitting the
  stake into identities each at the ceiling does. At a multiple of 1 every identity bonded at `min_stake`
  is at the ceiling, so the work is shared per present identity and splitting a stake always buys more of
  it. One card can serve the work of several identities. Splitting also buys presence and jury seats, at
  any multiple. Presence: each identity with at least one
  verified request that day is paid its own windows (decision 6), and a smaller identity draws a request
  on fewer days. Jury seats: the audit draw gives each identity at most one seat, weighs each candidate by
  its whole stake with no ceiling, and takes every eligible miner when they are fewer than the seats
  (`chain/x/jobs/keeper/audit_committee.go::drawMembersWithDomain`), so an operator's identities can
  each hold a seat on the same audit of a programme request, each seat paid `juror_per_verdict` (0.016 DNDR from the fifth season day) per verdict
  consistent with the outcome; only a node started with `--judge` posts a verdict. What an identity costs
  is its registration and `min_stake`, which on this testnet the faucet grants (decision 20); no cap per
  identity bounds what it is paid, the programme's daily budget and season cap bound what the programme
  pays (decision 11). What a farm of identities can do to the jury is in
  [ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md). Read the multiple and the stake
  floor on the chain: `assignment_stake_cap_multiple` and `min_stake` in `dendrad query jobs params -o json`
  (a multiple of 0, or absent from the output, selects the compiled multiple of 2; a `min_stake` of 0, or
  absent, sets no ceiling at all).
- **The budget does not grow with the network.** The volume of requests is fixed and the daily cap shared
  pro rata: the work paid in a day does not grow with the number of identities, what all of them are paid
  never exceeds the daily cap, and a large network reduces every reward equally once that cap is reached.
  The same fixed volume is shared among fewer miners on a smaller network, so each of them draws a larger
  share of it.
- **The programme lives off chain.** Its rules, script, ranking and payouts are public; the chain is the
  record of what each identity did, not the payer. Nothing here changes consensus.
- **What the checks prove, and what they do not.** That a programme request was served and either survived
  its audit or, from decision 18, had its audit unwound with no verdict against it (the chain); that its
  answer reached the programme (the day's seal); that a sample of the answers is a coherent attempt (a
  language model run where a GPU is, `final_season_grader.py`, which reads the first 8 000 characters of
  each; a grade the model cannot give clearly is not counted); that an operator key
  and a VRF key answered each window's challenge (the chain).
  Not which model answered, nor on which machine.
- **Presence is read from the transaction index.** The chain purges its record of a window's present miners
  at the next window and keeps only the last window per miner; the accepted `MsgProveAvailability`
  transactions are what lasts. The ranking reads them for the day's heights, keeps only messages of that
  type, and counts each window once (height ÷ `avail_epoch_blocks`). A node whose index does not reach back
  to the day is refused (`final_season_chain.py::require_index_from`). A chain with `avail_epoch_blocks = 0`
  is refused too, rather than read as no presence for anyone: no proof can be made on it, and every miner
  would count as present.
- **A miner without a VRF key proves no window.** Once the grace the chain gives a new registration is
  over (the window it registered in and the next), the chain draws it for no work at all; its daemon
  says so once an hour.
- **Sampled work answers are public.** Each sampled work answer enters the evidence log with its prompt,
  and the evidence log is downloadable by anyone. The programme's prompts are its own, so no client's text
  is exposed, but a miner's sampled answers are.
- **Jurors are paid only for audits they were drawn for, on the programme's own requests, and only for a
  verdict committed before the audit was resolved.** The chain accepts a verdict commit from any registered
  miner, even once the outcome is public (`chain/x/jobs/keeper/msg_server_commit.go::CreateCommit`), and
  ignores it in its own tally unless the miner was drawn; the programme reads the drawn jury from the block
  event that drew it, only for jobs whose client is the generator, and dates each verdict by its accepted
  `MsgCreateCommit` transaction in the transaction index. A verdict counts only if that transaction
  precedes the audit's resolution: an earlier block, or the same block when the deadline resolved the audit
  at the end of that block; for a resolution by adjudication, an earlier block only
  (`final_season_facts.py::timely_verdicts`). An audit whose resolution the index does not give counts none
  of its verdicts, and a verdict whose juror committed more in the day's window than the search reads is
  listed as not dated and not counted. The record is published under `inputs.verdicts` of the day's
  ranking. A verdict is a signed byte with no proof of judgement: a juror who votes with the expected
  outcome without judging is still paid (ADR-056).
- **Order of the rules.** The payout address comes first: a row with no address known is payable 0 and
  takes no share of the day's budget. The daily pro rata comes next, and it is the only rule that depends
  on everybody else. There is no cap per identity between the two (decision 11). Changing the order changes who is
  paid, so the order is part of the rule.
- **A day is ranked only once final.** A request counts when its last state is known. When that is, is a
  rule of the day's set (`final_season_rules.py::finality_rule`): a day under `RULES_17` is final
  `audit_unwind_blocks` + 200 blocks past its last counted block, a day under `RULES` (decision 18)
  `audit_unwind_blocks` + `audit_resolve_timeout` + 200, because the unwind is checked only when a deferred
  step comes due again, up to `audit_resolve_timeout` blocks later (`final_season_rank.py::finality_blocks`).
  The parameters are read from the chain, a value of 0 for one the rule reads refuses the day, and the
  script refuses to rank a day earlier. The season's last day is final that much after the season's last
  block, not after its full 17 280 blocks, and a day after the end is refused
  (`final_season_rank.py::season_window`). The service takes no grade for a day already ranked.
- **A restart of the generator resumes the day's count.** The generator writes how many of the day's
  requests it has sent, and how many were answered, after each one (`final_season_generator.py::save_sent`,
  `final_season_generator.py::load_sent`),
  keyed by the season's first block and the day, in the `final-season-generator` volume mounted at
  `/data/final-season-generator` (`DENDRA_FINAL_SEASON_GENERATOR_STATE`). A restart resumes from the last count written;
  a count it could not write is reported in its log, and a restart after it would send those requests
  again. A count it cannot read stops it with exit 2 and a message, because an unknown count is not zero.
- **No network address is collected.** No reward depends on where a miner connects from.
- **A ranking carries its rules.** Every published day carries the fingerprint of the rule set it was
  computed with. An amendment that changes the rules is a new set, never an edit
  (`final_season_rules.py::RULE_HISTORY`): the payment checks each day against the set its fingerprint
  names, refuses a fingerprint of no published set rather than paying it under rules it was not computed
  with, and refuses a day ranked under an earlier set than a day before it
  (`final_season_payout.py::check_days`).
- **Known limits.** (a) The chain accepts several proofs in one availability window, and a window is
  counted on the day of each proof's height: a window whose proofs fall on both sides of a day boundary
  is counted on both days, at most one window per boundary. (b) The ranking reads `avail_epoch_blocks` and
  the parameters its finality rule names from the chain's params when it ranks a day, so a parameter change
  during the season applies to the days ranked after it. (c) A last line of the evidence log torn by a
  crash mid-write is skipped and reported by a record of type `_unreadable_lines` with its count, and the
  next record is written on a line of its own (`final_season_evidence.py`). (d) A programme job whose answer
  the programme did not record (every forward was lost, or the service failed while the request was in
  flight) is paid on chain to its miner as usual (`client.py::quick_metered` settles whatever the
  forward raised) but earns no work reward, and the miner loses its presence too on a day none of its
  answers was recorded. A forward lost every time is counted in the day's number, since the programme may
  hold it. During an outage the generator sends nothing: it reads the service's status before each request
  (`final_season_generator.py::main`). (e) Time bounds the generator, not its count: requests go one at a
  time, and a request no one answers holds the generator for the answer's wait, the commit wait and a
  settlement that fails. Identities without a model, drawn often enough, leave a day with fewer answered
  requests than its number; the generator's log says by how much
  ([ADR-056](ADR-056-jury-and-faucet-on-the-final-testnet.md)).
