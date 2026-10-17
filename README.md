# Dendra ($DNDR)

**Open-weight AI models, served by the GPUs people already own — and checked on chain.**

**Join with a GPU or with a CPU.**

- **GPU (NVIDIA): mine.** The hardware probe picks the open model your card can serve.
- **CPU: judge, for now.** The network's judge model, a mixture-of-experts, runs on the CPU. It needs at least `MOE_CPU_MIN_RAM_MB` of system RAM (the floor [`deploy/hw_probe.sh`](deploy/hw_probe.sh) applies; `bash deploy/hw_probe.sh --judge-floor-mb` prints it), with or without a GPU (under Windows, the RAM of WSL 2). A juror is a registered miner, so a machine without a card also serves the requests the chain assigns it, with that same judge model on its CPU. No mining model runs on the CPU: below that floor, a machine without a usable NVIDIA card has no role.
- **Full node or validator:** no GPU.

More roles and technologies for CPU-only machines are planned.

Dendra is a sovereign Layer-1 whose useful work is **AI inference**. A miner serves an open model sized to its own hardware and earns `$DNDR` by answering real requests. The kit's hardware probe, [`deploy/hw_probe.sh`](deploy/hw_probe.sh), picks that model from the card's memory. A machine without an NVIDIA GPU serves no mining model: it judges on its CPU, or has no role. A random sample of answers is re-checked by a jury of other miners, and a miner that jury convicts loses part of its stake. Prompts travel **encrypted from the gateway to the miner** ([who reads what](#confidentiality--who-reads-a-prompt)), and the gateway serves the chat-completions API (`/v1/chat/completions`), so an existing chat-completions client connects by pointing its base URL at it.

> **Status: public testnet, `dendra-testnet`.** `$DNDR` is a **utility token** used inside the protocol to pay for inference and reward miners. **There is no token sale, presale or ICO.** Testnet DNDR is never sold. The Final Testnet Season pays rewards in it by a published formula. Those rewards count as points that convert one for one: each testnet DNDR the season paid becomes one mainnet DNDR, credited in the mainnet genesis to the address that received it (at most 3 000 mainnet DNDR in total). Mainnet launches after the Final Testnet Season. The chain is resettable: a reset erases balances; the season's published rankings are archived before any reset. Beyond the Final Testnet Season ([ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md)), this repository makes no promise.

## Final Testnet Season

The Final Testnet Season is a reward programme with **no registration and no test**. It ends on
7 November 2026 at 23:59 UTC: it counts every block timestamped before 8 November 2026, 00:00 UTC, by the
chain's own block times. Season days are 17 280 blocks each; the last one is cut at the end.
Rewards are earned per miner identity and per season day, and paid every seven season days in testnet
DNDR (the last payment covers the days left after the last full week), within the caps below and by a
formula published in advance. The rules are at [dendranetwork.com/final-season](https://dendranetwork.com/final-season/);
the decision and its rationale are in [ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md).
Every reward is paid on what the chain records and on the programme's published evidence (the jobs whose
answer it received, and the grades). Three rewards make up a day, and a fourth from a season day the
season service fixes (below):

- **Work: 0.10 DNDR per verified programme request** (0.05 on season days 0 to 3), the same rate for every identity. A verified
  request is settled, past its audit and not refunded. From the season day the season service names in
  `unwound_audit_work_from_day`, a request refunded because no audit of it concluded in time also counts
  when its answer reached the programme and a grading clears it. A grading clears it when its own answer
  was graded coherent or, if it was not graded itself, when its miner's grades that day do not void the
  day's work. With no grade at all it is not paid (ADR-047, decision 18;
  `final_season_facts.py::unwound_grading`).
- **Jury duty: 0.016 DNDR per verdict consistent with the outcome** (0.008 on days 0 to 3), for drawn jurors only, on audits of
  the programme's own requests.
- **Presence: 0.004 DNDR per availability window** (0.002 on days 0 to 3) proven on chain with `MsgProveAvailability`, up to 50
  windows a day, so at most 0.2 DNDR a day, paid **only on a day the identity has at least one verified
  request**.
- **Judge presence: 0.5 DNDR per active judge per day**, from the season day of the kit rule set below,
  inside the same daily cap as every other reward, paid in testnet DNDR and counted as points like the
  others. On a season day an identity is an active judge when all five hold: it is seated on the jury,
  drawn that day, of at least one audit that reached an outcome, of a programme request settled that day
  or within the finality span before it; the relay holds a reveal sealed to it for
  that audit, stored before its verdict; it committed a verdict on that audit after that reveal arrived
  and before the audit concluded; no verdict it posted on a programme audit concluded that day contradicts how
  that audit ended; and its latest signed capacity report at or before the day's end declares the judge role `active` on
  a kit at or above the kit floor. A verdict is a signed byte; apart from the contradiction check, these conditions do not
  read its value, so they prove neither that a model ran nor that the reveal was opened. The reward pays for being seated, answering
  in time and not being contradicted, and a mute seat earns nothing from it
  ([ADR-060](docs/adr/ADR-060-judge-presence-reward-amends-adr-038.md);
  `final_season_facts.py::judge_presence`).

The kit rule set applies from the one season day the season service publishes in its status as
`kit_rules_from_day` (counted from 0, null until the service fixes it); that day is the first season day
that begins at or after `KIT_DEADLINE`, and the service publishes it once it fixes it. From that day it pays the judge presence reward and applies two more
rules, and a ranked day is never recomputed under it. The kit floor: an identity whose latest signed
capacity report, at or before the day's end, declares a kit below `kit_min`, or that has no such
report, is paid nothing for the day. The floor is the kit number of the announced release. The
probation: an identity earns 60 percent of its season pay on its first full season day after its
registration (`final_season_rules.py::kit_rules`; the floor in `final_season_calc.py::kit_allows_pay`, the
probation in `final_season_calc.py::gross_of` and `final_season_facts.py::first_full_day`). An identity whose
registration cannot be dated is paid nothing under this rule set either
(`final_season_calc.py::registration_allows_pay`). Read
`KIT_MIN` and `KIT_DEADLINE` with `curl -sf https://testnet-api.dendranetwork.com/network-info.txt`,
and `kit_rules_from_day`, then `rules.kit_min` from that day, with
`curl -s https://testnet-api.dendranetwork.com/final-season/v1/status`.

Daily reward = 0.10 × verified requests + 0.016 × verdicts + (0.004 × min(windows, 50) if verified
requests ≥ 1, else 0) + 0.5 if an active judge (from the day of the kit rule set), in whole udndr
(`final_season_calc.py::gross_of`); on a probation day the whole is paid at 60 percent. The first three rates are those from the fifth season day (day 4 counted from 0); the first four days pay half of each of them and of the daily cap, and no judge presence reward. A ranked day is never recomputed. The condition on verified requests gates the
presence term only, so verdicts and the judge presence reward are paid on a day without a verified request. There is no public-node
reward. The programme sends a fixed number of
requests a day (`requests_per_day`), which the chain hands out among its **present** miners, drawn by
stake. After each day, at most three answers to programme requests per identity are drawn and graded by
a language model. A day whose graded answers are all incoherent, with at least two grades (one when a
single answer was sampled), loses its work reward, and with it its presence reward. A programme request
counts as work only if its answer reached the programme: the generator forwards each answer to the
programme service before it settles the job, and the day's seal lists the jobs answered
([`final_season_facts.py::answered_jobs`](services/final_season_facts.py)). Caps, on the programme
as a whole only: 100 DNDR a day (beyond it, every payable of the day is reduced by the same ratio) and
3 000 DNDR over the season. There is no cap per identity. The work an identity gets is its share of the
chain's work draw over a fixed daily volume (each identity's stake weighing up to the ceiling below), so a
cap per identity would only pay an operator to split its stake into identities that each stay under it. Rewards go to the address an identity declares, else
to the operator that signed its availability proofs that day, else to its operator in the miner
registry; with no address known, its payable is 0. Each day is ranked once final and, if it received
answers, once they are sealed, and published with its evidence; anyone can recompute it with
[`final_season_rank.py`](services/final_season_rank.py). Rewards count as
points: a participant's points are the season rewards actually paid to it. They convert one for one:
each testnet DNDR the season paid becomes one mainnet DNDR, credited in the mainnet genesis to the
address that received it ([ADR-049](docs/adr/ADR-049-testnet-mainnet-separation-and-transition.md)),
so at most 3 000 mainnet DNDR in total. Faucet grants, transfers, on-chain job pay and the work subsidy
are not points.

How the rules treat several identities: presence is paid only on a day of verified work, because a
presence proof shows an online operator key and VRF key, while only verified work shows a served model. The programme's daily volume is fixed, so
adding identities does not raise its total of paid work. The work draw weighs each identity's stake up to a ceiling, `assignment_stake_cap_multiple` ×
`min_stake` ([`committee.go::capAssignmentWeights`](chain/x/jobs/keeper/committee.go)); stake above
the ceiling weighs nothing in that draw. Below the ceiling an identity's weight is its stake, so its share
of the work follows its stake and splitting the stake does not raise it. Above the ceiling, splitting the
stake into identities that each still reach the ceiling does raise it, since each of them weighs as much
as the one clipped identity; a split into identities below the ceiling can lower it. At a multiple of 1
every identity bonded at `min_stake` or more sits at the ceiling: each weighs the same, a larger stake
draws no larger share, and splitting a stake into more identities bonded at `min_stake` draws a larger
share of the work. Governance moves the multiple, so which case applies is a reading of the chain,
not of this page. Read `assignment_stake_cap_multiple` and `min_stake` from `GET /dendra/jobs/v1/params`
or `dendrad query jobs params -o json` (a multiple of 0, or absent from the output, selects the
compiled default of 2; a `min_stake` of 0, or absent, sets no ceiling). At any multiple, splitting also
buys presence and jury seats. Each identity with at least one verified request that day earns its own
presence reward, and an identity that weighs less in the work draw draws a request, and so earns its
presence, on fewer days. The audit draw
([`audit_committee.go::drawMembersWithDomain`](chain/x/jobs/keeper/audit_committee.go)) gives an
identity at most one seat, draws by stake with no ceiling, and seats every eligible miner when they are
fewer than its `audit_committee_draw_size` seats (0, or absent from the output, selects the compiled
15). Several identities can therefore sit on one programme audit, each paid `juror_per_verdict` (0.016 DNDR from the fifth season day) per verdict consistent
with the outcome (only a miner configured to judge posts a verdict). What an identity costs is its registration and `min_stake`; the programme's
100 DNDR a day, shared pro rata, and 3 000 DNDR over the season bound what is paid.

Taking part means running a miner with [`deploy/install.sh`](deploy/install.sh): download it, read it,
then run it with `--yes`. It is never piped from `curl` into a shell. On a graphical Linux session it
also adds the desktop application **Dendra** ([`deploy/app/`](deploy/app/)) to your menu,
which shows and drives the miner. [`deploy/join.sh`](deploy/join.sh) remains the manual path
([Run it](#run-it)).

---

## What Dendra is

A client sends a prompt over HTTPS to a gateway that serves the chat-completions API (`/v1/chat/completions`), which runs a content filter and then **seals** (encrypts) the prompt to the serving miner's on-chain key. The relay carries ciphertext only. The miner decrypts it in memory and runs the inference **on its own machine**, on its GPU, with an open model sized to that card. A machine without a GPU judges on its CPU, and answers the requests the chain assigns it with the judge model, never with a mining model. The work is **settled on a sovereign Cosmos chain** (payment, vesting, anti-Sybil) and **verified economically**. A random sample of jobs is re-checked by a jury of other miners, who read the prompt and the answer to judge them, and a dishonest miner that a concluding jury convicts is **slashed** (loses part of its bond). The chain stores no prompt or answer text: it stores a salted hash of the prompt, an *embedding* of the answer (a list of numbers that summarises its meaning, publicly readable), verdicts and counters.

## How it works (one paragraph)

A request goes from the chat client to the chat-completions gateway, which opens a job on the chain (escrow, VRF beacon, stake-weighted committee). The miner answers the sealed prompt on its own machine, the chain settles the job, and the answer returns through the relay and the gateway. Verification is **optimistic**: one primary miner is paid (k=1), a fresh committee audits a VRF-sampled fraction of jobs, and a proven cheat is **hard-slashed**, which makes cheating negative-EV wherever an audit can conclude. The jury floor is a rule: `audit_min_quorum` + 1 present, eligible miners, since the jury excludes the miner under audit; below it, a drawn job is refunded to its client after `audit_unwind_blocks` and nobody is slashed. On a missing reveal the shipped juror (`judge_worker.py`, `NOREVEAL_ABSTAIN`) abstains, since it cannot be told apart from a relay outage; the audit unwinds after `audit_unwind_blocks` and the held fee returns to the client. The audit judge is an **LLM-as-judge** (an embedding cosine cannot judge correctness: it accepts both word-salads and fluent false facts). The audit committee is **drawn and anchored on-chain** at sampling time, stake-weighted, and **only its summoned members can vote**. A hard slash requires **at least two thirds of the anchored seats** to return "invalid" and a strict majority of the voting stake, with at least `audit_min_quorum` voters, so a minority verdict cannot slash an honest miner. With no anchored committee, no hard slash fires at all (fail-closed). The launch genesis **arms** this optimistic mode; the conservative redundant **k=3** mode remains available as a fallback. The randomness behind every assignment and audit draw comes from a **VRF seed**: each validator that anchored a VRF key contributes through its vote-extension, with a verifiable random function whose output anyone can check.

Those three properties are **pinned by a dedicated test suite**, [`chain/x/jobs/keeper/readme_public_claims_test.go`](chain/x/jobs/keeper/readme_public_claims_test.go), which exercises the anchored-committee rule, the two-thirds bar and the zero-seat (fail-closed) case under the **default** parameters, not under a launch-kit setting.

---

## The live network

**`dendra-testnet` runs the protocol in public.** It started from a fresh genesis, with no snapshot of
the earlier chain `dendra`: it produces blocks, answers a queryable API, and publishes a genesis anyone can
verify against this source tree. Its activity lives on the chain, one query away:

```
curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/job
curl -s "http://testnet-api.dendranetwork.com:26657/tx_search?query=%22message.module%3D%27jobs%27%22&per_page=1"
```

**Validators order the blocks and seed the draws.** Validators order and finalise blocks with CometBFT
consensus; read the set with `curl -s http://testnet-api.dendranetwork.com:26657/validators`. A bonded
validator that anchors a VRF key (`tx jobs register-validator-vrf-key`) contributes to the randomness of
every work assignment and every audit draw through its vote-extension. A draw's seed counts only when at
least `committee_min_vrf_contributors` validators contributed on that block, together carrying at least
two thirds of the committing power. Read the floor as `committee_min_vrf_contributors` in
`dendrad query jobs params -o json`.

**Two independent floors stand in the path of a job.** The first is applied when a job is opened:
`chain/x/jobs/keeper/pool_freeze.go::enoughEligibleMinersToVerify` counts the miners eligible **and
present** in the pool the job would freeze. Its full floor is `audit_min_quorum` + 1 (a quorum of 0, or
absent from the output, selects the compiled 4 through `effectiveSlashFloor`), because the jury
excludes the miner under audit. It refuses a job below that floor unless the chain runs optimistic
verification (`verification_mode` 1) with `audit_unwind_blocks` above 0 and `hold_bps` at 10000. With
all three, the floor drops to one present miner: an audited job no jury can conclude then ends with its
client refunded after `audit_unwind_blocks`, so no selected job is paid unverified and nothing is held
for ever. The second floor is the seed floor above, applied to the audit draw:
`dendrad query jobs committee-seed-health -o json` returns `latest_contributors` and the floor side by
side; only a bonded validator moves it, never a miner registration. On a block below either bar no
sampled audit is drawn and the due jobs are deferred (the chain logs each deferral), so a proposer gains
nothing by withholding the seed. A settled job whose audit draw waits for a seed is refunded to its
client after `audit_unwind_blocks`, exactly as one no jury can conclude.

**Live figures come from the chain**: `/dendra/jobs/v1/params`, `/dendra/jobs/v1/miner`,
`/dendra/jobs/v1/committee_seed_health`. When the open-job refusal fires, it names the shortfall in the
transaction's error.

**The protocol pays for work from block 1.** A miner that registers and serves a job is paid in `$DNDR`
by the protocol, out of the client fee and the pre-allocated Reserve. The mechanism, role by role, is
in [What the protocol pays](#what-the-protocol-pays). With `hold_bps` at 10000, as in the launch
genesis, settlement **retains the whole fee**: the miner's net share, the protocol's cut and the
deferred burn. Finality pays the miner and the validators/team/treasury cut; a retention that no jury
resolves within `audit_unwind_blocks` returns to the client, cut included. The mechanism is in
[Escrow](#escrow).

| Fact | Verify it yourself |
|---|---|
| **Supply starts at exactly 10,000,000 DNDR and can only fall.** `x/mint` is not in the binary, so no code path increases it; the soft burn on fees is the only thing that moves the total, and it moves it **down**. So the check is an **inequality, never an equality**: one denom, `0 < supply ≤ 10000000000000 udndr`, strictly below the genesis figure once any fee has burned, and never larger between two reads. Read it live. | `curl -s http://testnet-api.dendranetwork.com:1317/cosmos/bank/v1beta1/supply` should return exactly one entry, `denom: udndr`. Compare its `amount` against the genesis total `10000000000000` (in `https://testnet-api.dendranetwork.com/genesis.json`, `app_state.bank.supply`): it must be no larger, and strictly smaller once any fee has burned; the gap is the burn since genesis. Read it twice: it must never have grown. |
| **The served genesis matches the digest served beside it, and the one this repository pins.** The digest served beside the genesis catches a **truncated or corrupted download**. The pinned digest, [`docker/GENESIS_SHA256`](docker/GENESIS_SHA256), reaches you through the repository host instead, and `deploy/join.sh` refuses a served genesis that differs from it, so a forged genesis host is caught as well. | `curl -s -o g.json https://testnet-api.dendranetwork.com/genesis.json && sha256sum g.json`, then compare with `GENESIS_SHA256` in `https://testnet-api.dendranetwork.com/network-info.txt` **and** with line 1 of `docker/GENESIS_SHA256` in your clone (`<chain_id>=<sha256>`); all three must agree. If the clone disagrees and the network was relaunched, update the clone first: `git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main` (the update `deploy/join.sh` prints). |
| **The active validator set is read from the chain.** Bonded validators that are not jailed order the blocks; those that anchored a VRF key also contribute to the randomness beacon. The staking registry can also list a **jailed** validator, which keeps its stake but produces no blocks and contributes nothing to the beacon, so the active set is the one to read. Jail for downtime costs stake, which is why [`deploy/README.md`](deploy/README.md#becoming-a-validator-read-this-first) states the cost of the role before you bond. | Active set: `curl -s http://testnet-api.dendranetwork.com:26657/validators`, then read `result.total` and compare the `voting_power` values. Registry: `curl -s http://testnet-api.dendranetwork.com:1317/cosmos/staking/v1beta1/validators`, then read `status` and `jailed` on each; a validator in the active set is `BOND_STATUS_BONDED` with `jailed: false`. |
| **A draw needs a validator VRF seed, counted block by block.** The launch genesis sets `committee_seed_source=1`, so a work assignment or a sampled audit draw happens only on a block where at least `committee_min_vrf_contributors` validators carried a VRF vote-extension ([`chain/x/jobs/keeper/audit_sampling.go`](chain/x/jobs/keeper/audit_sampling.go), the `committee_seed_source == 1` branch), and only if those contributors carry at least two thirds of the committing power. Contributors are counted **per block**, so this reads as a rate: a validator contributes on the blocks its vote-extension reaches the proposer, and a jailed validator does not contribute. Jobs that found no seed are re-queued for a later block (the chain logs each deferral and emits an `audit_draw_deferred` event), and one whose retention reaches `audit_unwind_blocks` is refunded to its client. Deferral is **fail-closed** throughout: no retained payment released to the miner, no slash fired. | `dendrad query jobs committee-seed-health -o json`: compare `latest_contributors` against `committee_min_vrf_contributors`; a draw runs only when the first reaches the second, **for that block**. In `dendrad query jobs audit-deferred -o json`, `job_ids` lists the jobs the lottery selected whose jury was below the floor (`deferred_no_jury`); jobs deferred for want of a seed are not listed there, so read `dendrad query jobs held-summary -o json` (the retained total and the age of the oldest retention). `audits_opened` counts each job's first selection, whether deferred or anchored, plus human disputes. Without `dendrad`, the same figures are served over HTTP: `curl -s https://testnet-proof.dendranetwork.com/proof` |
| **The jury floor is set by a parameter.** An audit's jurors are drawn from the miners **present** in the job's frozen pool (availability proved in the current or previous window), the jury excludes the miner under audit, and a verdict needs votes at the jury bar (the larger of `audit_min_quorum` and two thirds of the anchored seats, `chain/x/jobs/keeper/antievasion.go::auditRelativeBar`), so an audit can only conclude once **at least `audit_min_quorum` + 1 miners are eligible and present**. An audit that cannot conclude is refunded to its client after `audit_unwind_blocks`: fee and protocol cut return, and the miner is neither paid nor slashed. Read both numbers from the chain. | `dendrad query jobs list-miner -o json`: `pagination.total` is the registered count, a ceiling on the present one · `dendrad query jobs params -o json`: read `audit_min_quorum` and `audit_unwind_blocks`, and do the addition yourself · `dendrad query jobs held-summary -o json` shows what is retained and for how long. Over plain HTTP: `curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/miner` and `.../dendra/jobs/v1/params` |
| **A miner's bond stays in place while it has a retained fee.** A held payment counts as an open obligation, and the same guard that refuses a voluntary exit also blocks eviction ([`chain/x/jobs/keeper/msg_server_miner.go`](chain/x/jobs/keeper/msg_server_miner.go), the open-obligations branch). The bond stays locked until that audit concludes, or until `audit_unwind_blocks` elapses and the fee returns to the client. Stake an amount you are happy to leave in place while audits resolve; a reset erases both the bond and the debt. | `dendrad query jobs held-summary -o json` |

**An empty answer is an answer.** On a fresh genesis `audit-deferred` returns `{}` and `list-miner` an
object with no entries. Protobuf omits fields at their zero value, so a field missing from the JSON
means zero; a query that fails returns an error instead.

**A deferral ends by itself.** It is a threshold being unmet, not a failure, and two distinct thresholds
gate a *selected* job's verdict:

1. **Enough VRF contributors on the same block.** `register-validator-vrf-key` from a bonded validator
   makes it a contributor, and the `committee_min_vrf_contributors` floor has to be met **on the block
   the draw is attempted**. A validator contributes on the blocks its vote-extension reaches the
   proposer, and jail stops the contribution. `committee-seed-health` prints the count for the block it
   was asked about: compare `latest_contributors` against `committee_min_vrf_contributors`.
2. **`audit_min_quorum` + 1 present miners.** The jury is drawn from the present, eligible miners of the
   job's frozen pool, except the primary, and it is anchored only once it has at least
   `audit_min_quorum` members. Both outcomes are gated on the jury bar, the larger of `audit_min_quorum` and two thirds of the anchored seats, rounded up (`auditRelativeBar`): releasing a held fee requires
   valid votes at that bar, and a slash needs invalid votes at it
   ([`chain/x/jobs/keeper/antievasion.go`](chain/x/jobs/keeper/antievasion.go),
   `auditVindicateDecision` and `auditSlashDecision`). At least `audit_min_quorum` jurors, plus the primary they are
   judging, is the miner floor: read `audit_min_quorum` from `dendrad query jobs params -o json` and
   add one. Below that floor no committee is anchored: the selected job is marked, its draw defers with
   `deferred_no_jury` (listed by `audit-deferred`), and once its retention reaches `audit_unwind_blocks`
   the client is refunded. Jurors vote from nodes started with `--judge`: only such a node posts a
   verdict.

Both are required before a *selected* job's held fee is paid. Neither needs a genesis change or a
redeployment, and neither is waited on for ever: a retention that no jury concludes within
`audit_unwind_blocks` returns to the client, fee and cut together. Retention is checkable:
`dendrad query jobs held-summary -o json` returns the retained total, the number of retentions and the
**age of the oldest one**.

The chain, not this page, is the live state: re-run the queries above to read it.

## Tested end to end

- **The invariants hold in the binary itself and are pinned by the test suite**: fixed supply,
  settle-once, bonds untouched outside a slash, the two-thirds anchored-seat bar.
- **Inference to payment ran end to end on a bench**: real jobs, settled on chain, with the supply
  decreasing by burn and never minting.
- **Audit, committee verdict and slash** are exercised by the chain's test suite, which covers the slash,
  refund and unwind paths, and ran on a bench with miners and judges side by side. On the public
  network, read the same paths live with `audit-deferred`, `held-summary` and
  `curl -s https://testnet-proof.dendranetwork.com/proof`.

A draw that read the **live** miner set would let an identity created after the seed becomes public grind its identifier into a committee, which defeats stake-weighting, since hashing is free and stake is not. [ADR-037](docs/adr/ADR-037-drawing-pool-freeze.md) closes that by freezing the eligible set at job opening, and it is shipped: `OpenJob` writes the anchor unconditionally, both draws filter on it, and a missing anchor is an error rather than a permissive default. `dendra-testnet` runs it from its genesis.

## Send a request

A request from outside the project reaches the public network through the Dendra gateway, and today
through nothing else. The gateway serves the chat-completions API (`/v1/chat/completions`): it opens the
job on chain, pays its fee from a testnet account of its own, seals the prompt to the serving miner and
returns the answer. Handing a sealed prompt to a miner is a write on the relay's `req` route, which takes
the relay's token ([`relay.py`](services/relay.py), `Handler._policy`). The
project's own clients hold that token: the gateway, and the Final Testnet Season's request generator,
which opens its own jobs and reads their answers itself
([`final_season_generator.py`](services/final_season_generator.py); `docker-compose.yml`,
services `gateway` and `final-season-generator`). The token is not distributed: the network's description
file, `network-info.txt`, does not carry it. A program outside the project can open a job on chain and
cannot hand it to a miner. Such a job is never served, and its fee goes back to the account that opened
it once it expires ([`job_expiry.go`](chain/x/jobs/keeper/job_expiry.go), `expireUnservedJob`).

What a first client can do today:

- send requests to `/demo/v1`, with no key, as below;
- ask the project for the key of `/v1`, for example to call the gateway from a web page of another
  origin (on `/demo` the proxy lets a browser read the answer only from the project's own site, and on
  `/v1` from any origin, [`docker/Caddyfile`](docker/Caddyfile)). The gateway checks a
  single key, `DENDRA_API_KEY` ([`gateway.py`](services/gateway.py),
  `_check_auth`), the one the proxy adds on `/demo`, so a key handed out is that key: same free tier,
  same quotas;
- run a stack of its own ([Run it](#run-it)) and call that stack's gateway,
  `http://127.0.0.1:8651/v1`, with a key it chooses. In that stack the gateway listens beyond loopback
  inside Docker (`DENDRA_GW_HOST: "0.0.0.0"` in `docker-compose.yml`), so it refuses to start without
  `DENDRA_API_KEY` in the `.env` (`gateway.py`, `main`) and answers a chat request that lacks
  `Authorization: Bearer <key>` with 401. That stack is a separate chain: its requests are served only by
  miners attached to it, such as `miner1` of the `local` profile, and never reach the public network.

| Base URL | Key | Without a key |
|---|---|---|
| `https://testnet-api.dendranetwork.com/demo/v1` | none | the path to use |
| `https://testnet-api.dendranetwork.com/v1` | `Authorization: Bearer <key>` | `401 {"error": "unauthorized"}` |

On `/demo/v1` the reverse proxy adds the gateway's key to the request
([`docker/Caddyfile`](docker/Caddyfile)); no key for `/v1` is published. The gateway serves one model,
`dendra-network` (`curl -s https://testnet-api.dendranetwork.com/demo/v1/models`); the `model` field is
accepted and does not route.

**The trial credit is the gateway's free tier.** You need no key, wallet, DNDR or sign-up. The gateway
pays each job's fee from its own subsidy account and bounds that spending with two daily token
quotas, one for all callers together (`DENDRA_FREE_TOK_DAY`) and one per caller IP address
(`DENDRA_FREE_TOK_IP`), counted on prompt and answer tokens
([`gateway.py`](services/gateway.py), `quota_reserve`). A request reserves its
prompt estimate plus the output cap (`DENDRA_OUT_ALLOW`) before the job, and the count is corrected once
the answer is back. A quota that is reached answers HTTP 429 with `"code": "quota_exhausted"` until the
next day of the gateway's clock. No endpoint reports what is left of a quota.

**A first request**, runnable as written:

```bash
curl -s --max-time 600 https://testnet-api.dendranetwork.com/demo/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"dendra-network","messages":[{"role":"user","content":"Capital of Australia? Answer in one word."}]}'
```

Keep a client timeout of ten minutes, as above. Before it replies, the gateway sends the transaction that
opens the job, waits up to `DENDRA_TIMEOUT` seconds for the miner's answer
([`gateway.py`](services/gateway.py), `TIMEOUT`, 240 by default), then waits for
the miner's commit on chain before it settles, up to `commit_wait` seconds more
([`client.py`](services/client.py), `settle_when_ready`, 180 by default). A
shorter client timeout fails on the client side only: the gateway still carries the job through.
Three examples are in [`examples/`](examples/): [`first_request.sh`](examples/first_request.sh)
(curl), [`first_request.py`](examples/first_request.py) (Python standard library) and
[`read_job.py`](examples/read_job.py), which reads a job back from the chain. The first two send
`Authorization: Bearer $DENDRA_API_KEY` when that variable is set, and take another gateway from
`DENDRA_GATEWAY_URL`.

**What the answer carries.** A chat-completions response (`choices[0].message.content`, `usage`) and a
`dendra` object ([`gateway.py`](services/gateway.py), `_dendra_meta`):

- `job_id`, the job on chain; `committee`, the miners the chain drew for it; `miner_id`, the miner
  that served it when the committee has a single member, and `null` otherwise; `beacon`, the public
  seed of that draw;
- `settled`: whether the chain accepted the settlement, `true`, `false`, or `null` when the gateway
  could not tell;
- `audit_state`: always `"pending"`, a constant the gateway writes; the outcome is read on chain;
- `escrow_udndr`, the fee the gateway escrowed, and `cost_udndr`, the gateway's own metering of the
  request. Under optimistic settlement the chain splits the whole escrowed fee
  ([`msg_server_settle_semantic.go`](chain/x/jobs/keeper/msg_server_settle_semantic.go),
  `settleOptimistic`), so the on-chain amount is the job's `fee`;
- `verify`: `proof_endpoint` and `query` (`dendrad query jobs get-job <job_id>`). The proof endpoint may
  be the relative path `/proof`: resolve it against `https://testnet-proof.dendranetwork.com`.

`usage` is the gateway's estimate, about four characters per token. With `"stream": true` the `dendra`
object arrives in the last chunk before `data: [DONE]`. A failed request answers an HTTP error
([`gateway.py`](services/gateway.py), `Handler.do_POST`). A job that failed
carries an `error` object whose `code` names the cause: `quota_exhausted` (429), `upstream_error` or
`no_result` (502), `no_committee_response` (504). A prompt or an answer the content filter refuses gets
403 with `"code": "illegal_content"` beside a plain `"error"` string. The other refusals (400, 401, 404,
413) carry a plain `"error"` string and no code.

**Find the job on chain** with `python3 examples/read_job.py <job_id>`, or with the chain's REST API,
which needs no key. These two read the most recent job the chain lists, as written:

```bash
curl -s "https://testnet-api.dendranetwork.com/rest/dendra/jobs/v1/job?pagination.limit=1&pagination.reverse=true"
python3 examples/read_job.py --latest
```

For your own job, put its `job_id` (and its `miner_id`) in these paths:

| GET under `https://testnet-api.dendranetwork.com/rest/dendra/jobs/v1` | What it says |
|---|---|
| `/job/<job_id>` | `client` (the gateway's account, which paid), `miner_id`, `fee` (escrowed, in udndr) and `state` |
| `/assigned_committee/<job_id>` | the drawn order; the first member is the primary, the miner that optimistic settlement pays |
| `/commit/<job_id>__<miner_id>` | what the miner committed: `prompt_commit` (a salted hash of the prompt), `result_commit` (an embedding of the answer), `model_id` and `weights_hash` |
| `/audit_committee/<job_id>` | the jury anchored for an audit; `404` with "no jury anchored" when no audit has opened on the job |

The chain appends markers to `state`. `+paid+optimistic`: settled. `+disputed`: an audit is open, drawn
by the lottery or by a dispute. `+resolved+vindicated+quorum`: the jury upheld the answer.
`+resolved+clawed+quorum`: the jury convicted the miner, which is slashed, and the fee goes back to the
client. `+resolved+unwound`: no verdict within `audit_unwind_blocks`, fee returned, nobody slashed.
`+expired+refunded`: no miner served the job. A settled job without `+disputed` was either passed over
by the audit lottery or is still waiting for its draw: `/audit_deferred` lists the drawn jobs waiting for
a jury, and The Proof (`https://testnet-proof.dendranetwork.com/proof`) lists recent audits in
`recent_audits`. A field missing from any of these answers holds zero.

**What verification covers.** A share `audit_sample_bps` of settled jobs is drawn for audit, and the
drawn job's jury, other miners running the judge model, reads the prompt and the answer and votes
([Verification & security model](#verification--security-model)). It checks the miner's answer to the
prompt the gateway sealed. A job the lottery passes over is not judged, and a drawn job whose jury
cannot conclude within `audit_unwind_blocks` is refunded with no verdict. The chain stores no prompt or
answer text, and the gateway builds the response you receive, `dendra` object included: reading the
job shows that a job was opened, served by the named miner, committed and settled, and leaves open
whether the text you received is the one the miner committed. The Proof's `origin` object counts jobs
by who opened them: `season_generator` (the Final Testnet Season's own requests, the only ones the
season pays work for), `chat_gateway` (this gateway) and `other_clients`.

## Run it

Two paths below: join the public network, or run a separate local stack of your own. They use the same
ports, so run one at a time.

### Join the public network (this is what you probably want)

**One command to join.** The config URL below is the live public network's, so these lines run as
written:

```bash
export CONFIG_URL=https://testnet-api.dendranetwork.com/network-info.txt
bash deploy/join.sh              # miner on a usable NVIDIA GPU; without one, a judge on the CPU, or refused (deploy/hw_probe.sh --role)
bash deploy/join.sh --validator  # full node + guided validator bond + VRF (no GPU)
bash deploy/join.sh --judge      # miner + audit committee (the judge runs on the CPU: MOE_CPU_MIN_RAM_MB of system RAM, deploy/hw_probe.sh)
```

**On a bare machine** (no Docker yet, or Windows), `deploy/install.sh` (Ubuntu, Debian, HiveOS) and
`deploy/install.ps1` (Windows: WSL 2 + Ubuntu) prepare the host and then run the same `join.sh`.
Download the file, read it, run it; each prints its plan and changes nothing without `--yes` / `-Yes`.
On a graphical Linux session the installer also adds the desktop application **Dendra**
([`deploy/app/`](deploy/app/)) to your menu: it shows the miner's health, balance and season progress,
starts and stops the miner, and shows the recovery phrase of each new key until three of its words are
typed back. `join.sh` pulls a
prebuilt miner image only when the clone's `docker/MINER_IMAGE` pins one by digest, starts it only when its
platform is that of your Docker engine, and builds the image from the clone otherwise. A miner needs **no inbound port**: it dials out to the relay, the RPC and the faucet, so a
connection behind carrier-grade NAT qualifies.

### Or run a separate local stack of your own

This starts a **separate chain that is not connected to anything**: your own genesis, your own
validator, no peers. It is for reading the code with something running in front of you, not for
joining.

```bash
# `docker/Dockerfile.services` does `COPY --from=dendra/chain:latest`, so the chain image must exist
# before the services image is built. Compose does not order builds (`depends_on` orders start-up),
# and a bare `up -d` on a clean machine fails with "pull access denied for dendra/chain", which points
# at a registry when the missing piece is a local build step.
docker compose build chain      # dendra/chain:latest (provides dendrad)
docker compose build relay      # dendra/services:latest, which copies dendrad out of the image above
docker compose up -d            # chain + relay + faucet + gateway (content filter inlined) + reverse proxy; the chat UI is opt-in (--profile chat)
```

> **Run one or the other.** This stack binds `26657` and `26656`, the same ports the joining kit needs.
> It takes its chain id from `DENDRA_CHAIN_ID` and refuses to start without one; give it a name other
> than the public network's (`dendra-testnet`), whose history it does not share.
> The two run under different Docker project names, so the kit's "a node is already running" check
> does not see this one and `deploy/join.sh` stops on a port already in use. If you started this stack
> and then want to join, `docker compose down` first.

The miner and validator roles are **paid by the protocol** for the work they do, per
[What the protocol pays](#what-the-protocol-pays); a judge is a miner, paid for the jobs it serves, and
the protocol pays nothing for a verdict itself. Protocol pay follows real traffic and **offers no yield,
rate or amount**; Final Testnet Season rewards, verdicts included, follow the formula published in
[ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md). Both are paid in testnet `$DNDR`. The
`--validator` path is also the one that touches the draw described above: it anchors a VRF key, and a
validator's VRF key counts toward `committee_min_vrf_contributors` on every block its vote-extension
reaches the proposer.

- **Full guide for every role (node, miner, validator, operator):** [`deploy/README.md`](deploy/README.md).
- **Wallet:** [dendranetwork.com/wallet](https://dendranetwork.com/wallet/): create or import a wallet, check a balance, send DNDR (testnet). Keys stay in the browser. The same page is in this repository ([`wallet/web/index.html`](wallet/web/index.html)) if you prefer to serve it yourself.
- **Advanced / manual node & miner kits:** [`deploy/testnet-node/`](deploy/testnet-node/), [`deploy/testnet-miner/`](deploy/testnet-miner/).
- **Operator (host the network):** [`deploy/launch/`](deploy/launch/) (one-command public launch) and `deploy/testnet/publish_network.sh`.

**Content filter.** The gateway screens every prompt with a content filter before sealing it to the miner: a CPU regex stage (patterns kept out of the repository), and an LLM classifier stage built into [`services/content_filter.py`](services/content_filter.py) that switches on with `DENDRA_GUARD_MODEL`.

## Tokenomics (fixed supply, zero mint)

- **Fixed supply: 10,000,000 DNDR at genesis, never more; the fee burn only lowers it. Zero inflation, zero mint.** Custom modules have no `Minter`, and the standard `x/mint` module is **removed from the chain binary entirely**, so fixed supply holds **by construction** (a fresh genesis boots to exactly 10,000,000 DNDR).
- Genesis allocation: community 33.82% / reserve 33% / validator delegation pocket 20% / validator 7% / team 5% / faucet float 1% / Final Testnet Season payout account 0.15% / request generator 0.03%, for 10,000,000 DNDR exactly. Read it from the genesis the endpoint actually serves (`http://testnet-api.dendranetwork.com:8088/genesis.json`), or from the script that builds it, [`docker/entrypoint-chain.sh`](docker/entrypoint-chain.sh). The committed [`chain/config.optimistic.yml`](chain/config.optimistic.yml) carries the same allocation, but it is a **local** genesis for the replayable slash demonstration: several of its verification parameters differ from the public one on purpose, and its own header says which.
- **Emission = release of the pre-allocated Reserve** (never minting), across three flows: work (demand-gated 1.5×), availability, security. Which role receives which flow, and against which file, is set out in [What the protocol pays](#what-the-protocol-pays).
- The protocol takes `protocol_fee_bps` and burns `fee_burn_bps` of each fee, and the miner keeps the remainder (the `minerNet` term in [`msg_server_settle_semantic.go`](chain/x/jobs/keeper/msg_server_settle_semantic.go)). With the launch genesis values (`protocol_fee_bps = 1500`, `fee_burn_bps = 500`), per 100 paid by a client: 80 to the miner, 7.5 to validators, 3 to the team, 4.5 to the treasury, 5 burned. The cut is retained per job and paid at finality, and the burn is deferred to finality; both return to the client with a refunded job.
- **The work flow follows demand:** the work subsidy is capped by settled traffic that burned fees (see [What the protocol pays](#what-the-protocol-pays)); the availability and security slices are released from the Reserve each epoch as a share of what remains ([`emission.go::EpochRelease`](chain/x/emission/keeper/emission.go)). The exporter publishes `dendra_r_settlement`, the ratio of on-chain settled demand to emission released.

## What the protocol pays

*(Everything below is a **parameter** of the running chain, readable with
`dendrad query jobs params -o json` and `dendrad query emission params -o json`. No amount, no rate and
no yield is stated here: what a participant receives depends on real traffic and on what is left in the
Reserve. Final Testnet Season rewards are a separate programme with a published formula, set out in
[Final Testnet Season](#final-testnet-season).)*

The money has exactly two origins, and **neither is inflation**:

1. **The client fee**, escrowed when a job opens.
2. **The pre-allocated Reserve**: 33% of genesis, held by the `x/emission` module account from block 1
   and released in **decreasing slices** (a governable share of what *remains*, never of a fresh
   supply) every `epoch_blocks`: [`chain/x/emission/keeper/epoch.go`](chain/x/emission/keeper/epoch.go).

There is no third origin. `x/mint` is removed from the binary, so a reward cannot be minted even by
governance. [`chain/app/fixed_supply_test.go`](chain/app/fixed_supply_test.go) pins this.

**Miner.** Three distinct flows:

- **Per job served.** At settlement the fee is split into soft burn (`fee_burn_bps`), protocol cut
  (`protocol_fee_bps`), and **the rest to the miner** (the `minerNet` term in
  [`msg_server_settle_semantic.go`](chain/x/jobs/keeper/msg_server_settle_semantic.go)); under
  `hold_bps` that share is retained and paid at finality (see [Escrow](#escrow)).
- **Work subsidy**, drawn from the emission `WorkPool` and **capped by demand** at
  `work_gate_bps × miner.Demand`, so it cannot be farmed without settled traffic that actually burned
  fees ([`msg_server_claim_subsidy.go`](chain/x/jobs/keeper/msg_server_claim_subsidy.go)). A job's
  right to it is credited at finality, dropped if the job is refunded, and claimed by the miner with
  `MsgClaimSubsidy`; the miner software claims it by itself.
- **Availability**, paid pro rata to bond from the `AvailPool` to miners that answer the availability
  challenge ([`availability.go`](chain/x/jobs/keeper/availability.go)). Availability windows
  (`avail_epoch_blocks`) can run for measurement alone: they decide which miners are **present**, and
  only present miners are drawn for work or audit. The on-chain payout flows only while
  `avail_payout_bps` is non-zero. Read both with `dendrad query jobs params -o json`.

**Validator.** Three sources: the transaction fees; a share of the protocol cut
(`validator_reward_bps`), retained per job and sent to `fee_collector` at finality; and the **security
flow** of each epoch, the one emission slice that leaves
the module account, sent to `fee_collector` and thus to validators and their delegators through
`x/distribution` ([`epoch.go`](chain/x/emission/keeper/epoch.go)).

**Whistleblower.** Anyone may dispute a job by posting `dispute_bond`. An **upheld** dispute returns
the bond **and** pays a reward, bounded by the remaining Treasury (the `upheld` branch of
[`msg_server_adjudicate.go`](chain/x/jobs/keeper/msg_server_adjudicate.go)). An **unfounded**
dispute forfeits the bond to the Treasury: the reward is paid for by the people who guess wrong, which
is what keeps it from being free money.

**Juror.** No path on chain pays a juror ([ADR-038](docs/adr/ADR-038-juror-incentive.md), amended for
the season by [ADR-060](docs/adr/ADR-060-judge-presence-reward-amends-adr-038.md)): a juror is a miner,
paid for the jobs it serves. Off chain, the [Final Testnet Season](#final-testnet-season) pays verdicts
consistent with the outcome by its published formula and, from the season day it publishes in
`kit_rules_from_day`, 0.5 DNDR a day to a judge that meets the five conditions listed there. A verdict is a signed byte; apart from the contradiction check, those conditions do not read
its value.

### Escrow

`hold_bps` is at its maximum (10000) in the launch genesis. The module therefore **retains** the
whole fee at settlement (the miner's net payment, the protocol's cut and the deferred burn) rather
than handing anything over on the spot, and resolves it at the job's audit checkpoint: finality, the
draw of the next block.

**The order of the two checks decides what happens.** The VRF-seed test runs *before* the per-job
lottery and returns early below the contributor floor
([`audit_sampling.go`](chain/x/jobs/keeper/audit_sampling.go)): on a block without a seed, no job
reaches the draw at all and **every** due retention stays held, not a fraction of it. On a block with
one, the checkpoint resolves each job one of two ways:

- a job the audit lottery does **not** select is **released** at that checkpoint: the miner is paid and
  the cut goes to validators, team and treasury (same file, the `releaseHeld` branch). The selected
  share is `audit_sample_bps`, a governable parameter; read it with
  `dendrad query jobs params -o json` (or `/dendra/jobs/v1/params`);
- a job the lottery **does** select waits for its jury. An upheld verdict pays it; a conviction slashes
  the miner and refunds the client.

**Held is retained: not cancelled, not forfeited, and not held for ever.** A selected job waits for its
jury, and if none concludes within `audit_unwind_blocks` blocks, the retention returns to the client,
fee and cut together. The miner is neither paid nor penalised, and its stake stays intact
([`audit_unwind.go`](chain/x/jobs/keeper/audit_unwind.go), `unwindStrandedRetention`). A settled
job whose audit draw waits for a seed unwinds the same way. Releasing the payment because the audit
could not run would hand an abstaining validator the power to push jobs through unverified; refunding it
instead means nothing is held for ever and nothing unverified reaches the miner.

This is the protocol failing closed. A payment that cannot be verified is not released, and it is not
destroyed either: it goes back to the client who paid it. `held-summary` reports the age of the oldest
retention, so this stays checkable.

## Confidentiality — who reads a prompt

On the public chat and API path the prompt reaches the Dendra gateway over HTTPS. The gateway runs the content filter, then seals the prompt to the serving miner's on-chain key with X25519 + HKDF-SHA256 + AES-256-GCM ([`services/modea/crypto.py`](services/modea/crypto.py)), and decrypts the answer it returns to you over HTTPS.

- **The gateway** reads the prompt, filters it, and encrypts it to the serving miner.
- **The relay** carries ciphertext only.
- **The serving miner** decrypts the prompt in memory to answer it. The miner software confines its own process (no core dumps, no debugger attach from the same user, no privilege gain) where the host allows it. While the model answers, the miner software keeps the prompt out of crash dumps and blocks writing it to disk or sending it anywhere but the model, where the host allows it ([`services/modea/miner.py`](services/modea/miner.py), `handle_job`).
- **The jurors**, if the job is drawn for audit: the miner re-seals the prompt and the answer to them, and they read both to judge. From kit v0.2.1 the miner seals this reveal for the jurors the chain anchored for that audit and for no other miner. A miner running a kit of an earlier release seals it for every other registered miner with an encryption key ([ADR-057](docs/adr/ADR-057-judge-anchor-check-and-single-judge-model.md), decision 4). Each copy is sealed to the encryption key the juror registered on chain. For a juror that registered none, it is sealed to the key the relay serves for it, which a relay that replaces that key can open.
- **The chain** holds a salted hash of the prompt and an embedding of the answer, never the text.

On the public network a prompt from outside the project goes through the Dendra gateway: handing a prompt to a miner takes the relay's token, which is not distributed ([Send a request](#send-a-request)). A stack you run yourself ([Run it](#run-it)) keeps the plaintext off the Dendra gateway, on a separate chain served by miners you attach to it. As with any hosted model, keep secrets out of your prompts.

## Repository layout

```
chain/               Cosmos SDK 0.53.6 chain (source of truth): x/{jobs,emission,modelregistry}, proto, genesis
services/            Reference off-chain stack: gateway, miner, relay, client, judge glue, regex content-filter, exporter, faucet
tokenomics/          Canonical economic model (tokenomics_v5)
deploy/              Node / validator / miner kits + one-command join.sh + public launch kit
docker/              Dockerfiles, entrypoints, monitoring provisioning
wallet/              Web wallet (single-file HTML) + docs
docs/litepaper.md    The litepaper
docs/adr/            Architecture Decision Records: the reasoning behind every protocol choice
```

## Verification & security model

Every mechanism below ships in this repository and is covered by its tests.

- **Optimistic verification + LLM-as-judge** (two-stage: coherence, then same-fact).
- **Anti-evasion**: a silent primary earns nothing. The shipped juror abstains on a missing reveal, so the held fee unwinds to the client after `audit_unwind_blocks` and the stake is untouched (ADR-044). The parameter `silence_slash_bps` has no effect: the only code that reads it, `chain/x/jobs/keeper/antievasion.go::clawbackPayment`, has no production caller.
- **Fee-hold v2** ("never the bond"): clawbacks come from the withheld fee, never from the security bond.
- **Pro-honest floor** before any hard slash: "invalid" from **≥2/3 of the anchored committee seats** and a strict majority of the voting stake; the bar is the larger of that and `audit_min_quorum` jurors (`antievasion.go::auditRelativeBar`).
- Assignment via a **VRF beacon aggregated from validator vote-extensions** (see [The live network](#the-live-network)). Anti-Sybil via **non-recoverable demand**.

The reasoning behind each of these choices, including the ones that were tried and abandoned, is published in full: see [`docs/adr/`](docs/adr/README.md). The [litepaper](docs/litepaper.md) is the short form.

## Contributing & security

- Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Vulnerability disclosure: see [`SECURITY.md`](SECURITY.md). Please report privately and do not open a public issue for security bugs.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
