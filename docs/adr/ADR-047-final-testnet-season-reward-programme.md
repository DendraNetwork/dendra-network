# ADR-047 — Final Testnet Season reward programme

**Status:** Accepted by the project owner, 2026-10-04; amended by four owner decisions the same day, by
nine more on 2026-10-05, three on 2026-10-06 and one on 2026-10-07, decision 17, which ends the season at
a fixed time (*Owner decisions*, below). This text already applies them all.
**Implementation:** Runs on the chain of [ADR-046](ADR-046-single-validator-relaunch.md) and needs the
presence rule of [ADR-048](ADR-048-epoch-14-consensus-batch.md). The rates and caps live once, in
`services/final_season_rules.py::RULES`; the formula is `final_season_calc.py::gross_of` and
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
grades): a request of the programme that a miner served, that survived its audit and whose answer reached
the programme, on a day its work was not voided by its grades (the void rule below); a verdict on one of
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
| Work | 0.05 DNDR per verified request, the same for every miner | The project sends a **fixed number of requests a day** (`requests_per_day`; on the season's last, shorter day, spread over the time left), which the chain hands out among its present miners, drawn by stake up to a ceiling per identity (decision 10). A request counts on the block it is settled on chain, which must be in the season (decision 17), once past its audit, neither refunded nor slashed, only on a job whose client is the programme's generator, and only if its answer reached the programme (below). It does not count on a day whose graded work answers are all incoherent, with at least min(2, answers sampled) grades (the void rule below) |
| Juror | 0.008 DNDR per verdict consistent with the outcome | The verdict is on chain, from a juror the chain drew, on the audit of a job whose client is the programme's generator |
| Presence | 0.002 DNDR per availability window proven on chain, at most 50 a day (0.1 DNDR a day), **only on a day with at least one verified request** | The miner's daemon answers each window's challenge with a VRF proof (`MsgProveAvailability`); the ranking reads the accepted proofs from the chain's transaction index. A proof needs an operator key and a VRF key, no model: the verified request of the same day is what shows a model answered |

There is no public-node reward, no model class and no exam (decisions 3 and 8 below).

**Work counts only if its answer reached the programme.** The generator forwards each answer to the
programme service before it settles the job (`client.py::quick_metered`, its `on_answer` hook;
`final_season_generator.py::run_one`). The service records the job id of every answer it receives, and the
day's seal lists them (the evidence record `work_sealed`, field `answered`); a programme job counts as a
verified request only if it is in that list (`final_season_facts.py::answered_jobs`,
`final_season_facts.py::chain_day_facts`). A miner can anchor a commit and anyone can settle a job —
settlement is permissionless — so a job can be paid on chain with no answer delivered; such a job is not
paid as work. The ranking of a day that received answers waits for their seal
(`final_season_server.py::rank_days`).

**Caps**

| Cap | Value |
|---|---|
| Programme, per day | 50 DNDR. Beyond it, every payable reward of that day is reduced by the same ratio (floor, in udndr), as announced in advance |
| Season | 1 500 DNDR over the season; a day's budget is the smaller of 50 DNDR and what remains of it |

There is no cap per identity (decision 11). The work an identity gets is the chain's stake-weighted draw
over a fixed daily volume (`requests_per_day`), so a cap per identity would only pay an operator to split
its stake into identities that each stay under it. Both caps live in `final_season_rules.py::RULES`
(`cap_programme_day`, `cap_season`).

**Formula.** With p = min(availability windows proven that day, 50), gross = 0.05 × verified requests
+ 0.008 × verdicts + (0.002 × p if verified requests ≥ 1, else 0), computed in integer udndr
(`final_season_calc.py::gross_of`). Payable = the gross, or 0 when no payout address is known. Paid = the
payable, unless the day's total payable exceeds the day's budget: then every payable of that day is
reduced by the same ratio, floor in udndr (`final_season_calc.py::compute_day`). For example, 50 windows and
20 verified requests give 0.1 + 1 = 1.1 DNDR gross, paid 1.1 DNDR unless the day's total exceeds the day's
budget. Each published row carries `presence`, `verified_requests`, `verdicts`, `gross_udndr`,
`payable_udndr`, `paid_udndr` and, in words, the `reason` it is paid less than its facts would give
(`final_season_calc.py::to_json`).

**Payout address.** A reward goes to the address declared with the miner's command line
(`final_season_miner.py payout`); else to the operator that signed the identity's availability proofs that
day, the creator of its latest accepted proof (`final_season_chain.py::presence_proofs`); else to the
registry's operator, for an identity with no proof that day (a juror only, for instance). An identity
with no address known is payable 0, its reason "no payout address known": a row the weekly payment could
not pay would stop the whole week. The application shows the recovery phrase at first launch.

**Payment.** Computed by a public script that anyone can rerun on chain data and the published evidence
logs; published daily as a ranking per identity, each row with its payout address; paid weekly in DNDR from
the payout account funded at genesis, every seven season days. The last payment covers the days left after
the last full week, up to the season's last day — the ranking that names the season's last block
(`inputs.season_end_height`) — once that day is final (`final_season_payout.py::plan_days`). Before
anything is planned, the payment checks every published day from the first to the end of that week
against the rules and pays nothing for the week if one breaks them
(`final_season_payout.py::check_days`): the rules fingerprint; each row's gross as the formula on the counts the
row publishes (`final_season_calc.py::gross_of` on its presence, verified requests and verdicts); its payable,
the gross if it has an address and 0 otherwise; 0 ≤ paid ≤ payable; the totals; the day's budget derived
from what the earlier days paid; the pro rata; and the season cap. These checks bound the loss, they do
not prove the facts: a compromised service can still publish false counts, and what it can misdirect is
bounded by the day's budget (50 DNDR) and the season cap, not by any per-identity cap.

**Points.** A participant's points are the programme rewards actually paid to it: the sum of `paid_udndr`
over the published rankings (decision 14), so work, juror and presence rewards after the day's pro rata.
Faucet grants, transfers between addresses, on-chain job payments and the chain subsidy never count. Points
convert **one for one** (decision 15): each testnet DNDR the season paid becomes one mainnet DNDR, credited in
the mainnet genesis to the address that received it ([ADR-049](ADR-049-testnet-mainnet-separation-and-transition.md)).
With the season cap of 1 500 DNDR, the conversion is at most 1 500 mainnet DNDR.

**After the season.** Mainnet launches after the Final Testnet Season (decision 13), and each point
becomes one mainnet DNDR (decision 15).

## Owner decisions

The project owner decided decisions 1 to 16 before the season started, and decision 17 on 2026-10-07,
once it had started.

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
   since decision 15).
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
   weight IS its stake, so below the ceiling splitting a stake buys no more work — the draw keeps the
   lowest hash-over-stake score, which favours a larger stake more than in proportion on a small pool,
   and less as the pool grows, so a split stake draws less. Above the ceiling, splitting does buy
   more: several identities each at the ceiling weigh more than one. Its cost, chosen: a miner with a
   larger stake draws a larger share of the work.
11. **No cap per identity.** Asked whether to remove the cap per identity and keep only the programme's
   caps — then 500 DNDR a day, shared pro rata beyond it, and 15 000 DNDR over the season, divided by ten by
   decision 15 — the owner removed
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

14. **Points are the amount actually paid.** Asked which figure of a ranking counts as points — the
   gross the formula gives, the payable amount (the gross when a payout address is known), or the amount
   actually paid after the day's pro rata — the owner chose the amount paid (`paid_udndr`). Points stay
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
   still a launch bonus.
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

**The void rule.** A day's work is taken away only when every graded work answer of the day was judged
incoherent and there are at least min(2, answers sampled) grades (`final_season_facts.py::_graded_out`,
`GRADES_TO_VOID`); its presence goes with it, being paid only on a day of verified work. No grade never
voids anything.

## What was measured

Measured in October 2026, before the season, on one RTX 5060 Ti (8 GB) with `llama3.1:8b-instruct-q4_K_M`
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
  `assignment_stake_cap_multiple` × `min_stake` per identity (decision 10): below that ceiling a farm's
  share of the work follows its stake, and splitting it does not raise it; above it, splitting the stake
  into identities each at the ceiling does. One card can serve the work of several identities. Below the
  ceiling, what splitting still buys is presence and jury seats. Presence: each identity with at least one
  verified request that day is paid its own windows (decision 6), and a smaller identity draws a request
  on fewer days. Jury seats: the audit draw gives each identity at most one seat, weighs each candidate by
  its whole stake with no ceiling, and takes every eligible miner when they are fewer than the seats
  (`chain/x/jobs/keeper/audit_committee.go::drawMembersWithDomain`), so an operator's identities can
  each hold a seat on the same audit of a programme request, each seat paid 0.008 DNDR per verdict
  consistent with the outcome; only a node started with `--judge` posts a verdict. What an identity costs
  is its registration and `min_stake`; no cap per identity bounds what it is paid, the programme's daily
  budget and season cap bound what the programme pays (decision 11). Read the multiple and the stake
  floor on the chain: `dendrad query jobs params -o json` → `assignment_stake_cap_multiple`, `min_stake`
  (a multiple of 0, or absent from the output, selects the compiled multiple of 2; a `min_stake` of 0, or
  absent, sets no ceiling at all).
- **The budget does not grow with the network.** The volume of requests is fixed and the daily cap shared
  pro rata: the work paid in a day does not grow with the number of identities, what all of them are paid
  never exceeds the daily cap, and a large network reduces every reward equally once that cap is reached.
  The same fixed volume is shared among fewer miners on a smaller network, so each of them draws a larger
  share of it.
- **The programme lives off chain.** Its rules, script, ranking and payouts are public; the chain is the
  record of what each identity did, not the payer. Nothing here changes consensus.
- **What the checks prove, and what they do not.** That a programme request was served and survived its
  audit (the chain); that its answer reached the programme (the day's seal); that a sample of the answers
  is a coherent attempt (a language model run where a GPU is, `final_season_grader.py`, which reads the first
  8 000 characters of each; a grade the model cannot give clearly is not counted); that an operator key
  and a VRF key answered each window's challenge (the chain).
  Not which model answered, nor on which machine.
- **Presence is read from the transaction index.** The chain purges its record of a window's present miners
  at the next window and keeps only the last window per miner; the accepted `MsgProveAvailability`
  transactions are what lasts. The ranking reads them for the day's heights, keeps only messages of that
  type, and counts each window once (height ÷ `avail_epoch_blocks`). A node whose index does not reach back
  to the day is refused (`final_season_chain.py::require_index_from`), and a chain with `avail_epoch_blocks = 0`
  — where no proof can be made, and every miner counts as present — is refused rather than read as no
  presence for anyone.
- **A miner without a VRF key proves no window.** Once the grace the chain gives a new registration is
  over (the window it registered in and the next), the chain draws it for no work at all; its daemon
  says so once an hour.
- **Sampled work answers are public.** Each sampled work answer enters the evidence log with its prompt,
  and the evidence log is downloadable by anyone. The programme's prompts are its own, so no client's text
  is exposed, but a miner's sampled answers are.
- **Jurors are paid only for audits they were drawn for, on the programme's own requests.** The chain
  accepts a verdict commit from any registered miner and ignores it in its own tally; the programme reads
  the drawn jury from the block event that drew it, and only for jobs whose client is the generator. A
  drawn juror who stays silent and votes after the outcome is known is still counted: the chain records
  no height on a commit.
- **Order of the rules.** The payout address first — a row with no address known is payable 0 and takes
  no share of the day's budget — then the daily pro rata, which is the only rule that depends on everybody
  else. There is no cap per identity between the two (decision 11). Changing the order changes who is
  paid, so the order is part of the rule.
- **A day is ranked only once final.** A request counts when its last state is known, which is after the
  unwind bound of the chain; a day's ranking is published that much later, and the script refuses to rank
  a day earlier. The season's last day is final that much after the season's last block, not after its
  full 17 280 blocks, and a day after the end is refused (`final_season_rank.py::season_window`). The
  service takes no grade for a day already ranked.
- **A restart of the generator resumes the day's count.** The generator writes how many of the day's
  requests it has sent after each one (`final_season_generator.py::save_sent`, `final_season_generator.py::load_sent`),
  keyed by the season's first block and the day, in the `final-season-generator` volume mounted at
  `/data/final-season-generator` (`DENDRA_FINAL_SEASON_GENERATOR_STATE`). A restart resumes from the last count written;
  a count it could not write is said in its log, and a restart after it would send those requests again.
  A count it cannot read stops it (exit 2), said: a count not known is not zero.
- **No network address is collected.** No reward depends on where a miner connects from.
- **A ranking carries its rules.** Every published day carries the fingerprint of the rules it was computed
  with; an amendment changes that fingerprint, so a day ranked under other rules is refused by the payment
  rather than paid under rules it was not computed with.
- **Known limits.** (a) The chain accepts several proofs in one availability window, and a window is
  counted on the day of each proof's height: a window whose proofs fall on both sides of a day boundary
  is counted on both days, at most one window per boundary. (b) The ranking reads `avail_epoch_blocks` and
  the finality (`audit_unwind_blocks` plus a margin) from the chain's params when it ranks a day, so a
  parameter change during the season applies to the days ranked after it. (c) A last line of the evidence
  log torn by a crash mid-write is skipped and said — a record of type `_unreadable_lines` with its count —
  and the next record is written on a line of its own (`final_season_evidence.py`). (d) A programme job whose
  answer the programme did not record — the generator's forward failed, or the service failed while the
  request was in flight — is paid on chain to its miner as usual (`client.py::quick_metered`
  settles whatever the forward raised) but earns no work reward, and the miner loses its presence too on
  a day none of its answers was recorded. During an outage the generator sends nothing: it reads the
  service's status before each request (`final_season_generator.py::main`).
