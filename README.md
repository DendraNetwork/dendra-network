# Dendra ($DNDR)

**Open-weight AI models, served by the GPUs people already own — and checked on chain.**

**Join with a GPU or with a CPU.**

- **GPU (NVIDIA): mine** — the hardware probe picks the open model your card can serve.
- **CPU: judge, for now** — the network's judge model, a mixture-of-experts, runs on the CPU. It needs at least 26 000 MB of system RAM, with or without a GPU (under Windows, the RAM of WSL 2). A juror is a registered miner, so the machine also serves requests with a small model on its CPU.
- **Full node or validator:** no GPU.

More roles and technologies for CPU-only machines are planned.

Dendra is a sovereign Cosmos L1 whose useful work is **AI inference, not hashing**. A miner serves an open model sized to its own hardware — the kit's hardware probe, [`deploy/hw_probe.sh`](deploy/hw_probe.sh), picks it from the card's memory, or a small model for the CPU on a machine without an NVIDIA GPU — and earns `$DNDR` by answering real requests. A random sample of answers is re-checked by a jury of other miners, and a miner that jury convicts loses part of its stake. Prompts travel **encrypted from the gateway to the miner** ([who reads what](#confidentiality--who-reads-a-prompt)), and the gateway speaks the OpenAI chat-completions API, so an existing OpenAI client connects by pointing its base URL at it.

> **Status: public testnet, `dendra-testnet`.** `$DNDR` is a **utility token** used inside the protocol to pay for inference and reward miners — **there is no token sale, presale or ICO.** Testnet DNDR is never sold; the Final Testnet Season pays rewards in it by a published formula, and those rewards count as points that convert one for one: each testnet DNDR the season paid becomes one mainnet DNDR, credited in the mainnet genesis to the address that received it (at most 1 500 mainnet DNDR in total). Mainnet launches after the Final Testnet Season. The chain is resettable: a reset erases balances; the season's published rankings are archived before any reset. Beyond the Final Testnet Season ([ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md)), this repository makes no promise.

## Final Testnet Season

The Final Testnet Season is a reward programme with **no registration and no test**. It ends on
7 November 2026 at 23:59 UTC: it counts every block timestamped before 8 November 2026, 00:00 UTC — the
chain's own block times decide. Season days are 17 280 blocks each; the last one is cut at the end.
Rewards are earned per miner identity and per season day, paid every seven season days in testnet DNDR
(the last payment covers the days left after the last full week), within the caps below and by a
formula published in advance — rules at [dendranetwork.com/final-season](https://dendranetwork.com/final-season/), decision and
rationale in [ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md). Every reward is paid on what the
chain records and on the programme's published evidence (the jobs whose answer it received, and the
grades): work (0.05 DNDR per verified programme request — settled, past its audit, not refunded —
the same rate for every identity), jury duty (0.008 DNDR per verdict consistent with the outcome, drawn
jurors only, on audits of the programme's own requests) and presence (0.002 DNDR per availability window
proven on chain with `MsgProveAvailability`, up to 50 windows a day, so at most 0.1 DNDR a day, paid
**only on a day the identity has at least one verified request**). Daily reward = 0.05 × verified
requests + 0.008 × verdicts + (0.002 × min(windows, 50) if verified requests ≥ 1, else 0), in whole udndr
(`final_season_calc.py::gross_of`): the condition gates the presence term only, so verdicts are paid on a day
without a verified request. There is no public-node reward. The programme sends a fixed number of
requests a day (`requests_per_day`), which the chain hands out among its **present** miners, drawn by
stake. After each day, at most three answers to programme requests per identity are drawn and graded by
a language model; a day whose graded answers are all incoherent, with at least two grades (one when a
single answer was sampled), loses its work reward, and with it its presence reward. A programme request
counts as work only if its answer reached the programme: the generator forwards each answer to the
programme service before it settles the job, and the day's seal lists the jobs answered
([`final_season_facts.py::answered_jobs`](services/final_season_facts.py)). Caps, on the programme
as a whole only: 50 DNDR a day (beyond it, every payable of the day is reduced by the same ratio) and
1 500 DNDR over the season. There is no cap per identity: the work an identity gets is the chain's
stake-weighted draw over a fixed daily volume, so a cap per identity would only pay an operator to split
its stake into identities that each stay under it. Rewards go to the address an identity declares, else
to the operator that signed its availability proofs that day, else to its operator in the miner
registry; with no address known, its payable is 0. Each day is ranked once final and, if it received
answers, once they are sealed, and published with its evidence; anyone can recompute it with
[`final_season_rank.py`](services/final_season_rank.py). Rewards count as
points: a participant's points are the season rewards actually paid to it, and they convert one for one,
each testnet DNDR the season paid becoming one mainnet DNDR, credited in the mainnet genesis to the
address that received it ([ADR-049](docs/adr/ADR-049-testnet-mainnet-separation-and-transition.md)),
so at most 1 500 mainnet DNDR in total. Faucet grants, transfers, on-chain job pay and the work subsidy
are not points.

How the rules treat several identities: presence is paid only on a day of verified work, because a
presence proof shows an online operator key and VRF key, while only verified work shows a served model; and the programme's daily volume is fixed, so
adding identities does not raise its total of paid work. The work draw weighs each identity's stake up to a ceiling, `assignment_stake_cap_multiple` ×
`min_stake` ([`committee.go::capAssignmentWeights`](chain/x/jobs/keeper/committee.go)). The launch
genesis sets the multiple to 100, the highest the chain accepts: up to 100 × `min_stake` an identity's
weight is its stake, so splitting a stake into more identities buys no more work, and a larger stake
draws a larger share of it; above the ceiling, splitting does buy more, since several identities each at
the ceiling weigh more than one. Read the ceiling's two parameters on the chain:
`dendrad query jobs params -o json` → `assignment_stake_cap_multiple`, `min_stake` (a multiple of 0, or
absent from the output, selects the compiled default of 2; a `min_stake` of 0, or absent, sets no
ceiling). On either side of the ceiling, splitting also buys presence and jury seats. Each identity
with at least one verified request that day earns its own presence reward, and a smaller identity draws
a request, and so earns its presence, on fewer days. The audit draw
([`audit_committee.go::drawMembersWithDomain`](chain/x/jobs/keeper/audit_committee.go)) gives an
identity at most one seat, draws by stake with no ceiling, and seats every eligible miner when they are
fewer than its `audit_committee_draw_size` seats (0, or absent from the output, selects the compiled
15), so several identities can sit on one programme audit, each paid 0.008 DNDR per verdict consistent
with the outcome (only a miner configured to judge posts a verdict). What an identity costs is its registration and `min_stake`; the programme's
50 DNDR a day, shared pro rata, and 1 500 DNDR over the season bound what is paid.

Taking part means running a miner: [`deploy/install.sh`](deploy/install.sh) — download it, read it,
then run it with `--yes`; it is never piped from `curl` into a shell. On a graphical Linux session it
also adds the desktop application **Dendra** ([`deploy/app/`](deploy/app/)) to your menu,
which shows and drives the miner. [`deploy/join.sh`](deploy/join.sh) remains the manual path
([Run it](#run-it)).

---

## What Dendra is

A client sends a prompt over HTTPS to an OpenAI-compatible gateway (`/v1/chat/completions`), which runs a content filter and then **seals** (encrypts) the prompt to the serving miner's on-chain key; the relay carries ciphertext only. The miner decrypts it in memory and runs the inference **on its own machine**: on its GPU, with an open model sized to that card, or, without one, on its CPU with a small model. The work is **settled on a sovereign Cosmos chain** (payment, vesting, anti-Sybil) and **verified economically**: a random sample of jobs is re-checked by a jury of other miners, who read the prompt and the answer to judge them, and a dishonest miner that a concluding jury convicts is **slashed** (loses part of its bond). The chain stores no prompt or answer text: it stores a salted hash of the prompt, an *embedding* of the answer (a list of numbers that summarises its meaning, publicly readable), verdicts and counters.

## How it works (one paragraph)

`Chat client → OpenAI-compatible gateway → chain (escrow → VRF beacon → stake-weighted committee → local inference on a sealed prompt → on-chain settlement) → answer.` Verification is **optimistic**: one primary miner is paid (k=1), a fresh committee audits a VRF-sampled fraction of jobs, and a proven cheat is **hard-slashed**, which makes cheating negative-EV wherever an audit can conclude. The jury floor is a rule: `audit_min_quorum` + 1 present, eligible miners, since the jury excludes the miner under audit; below it, a drawn job is refunded to its client after `audit_unwind_blocks` and nobody is slashed. On a missing reveal the shipped juror (`judge_worker.py`, `NOREVEAL_ABSTAIN`) abstains, since it cannot be told apart from a relay outage; the audit unwinds after `audit_unwind_blocks` and the held fee returns to the client. The audit judge is an **LLM-as-judge** (an embedding cosine cannot judge correctness: it accepts both word-salads and fluent false facts). The audit committee is **drawn and anchored on-chain** at sampling time, stake-weighted, and **only its summoned members can vote**. A hard slash requires **at least two thirds of the anchored seats** to return "invalid" and a strict majority of the voting stake, with at least `audit_min_quorum` voters, so a minority verdict cannot slash an honest miner. With no anchored committee, no hard slash fires at all (fail-closed). The launch genesis **arms** this optimistic mode; the conservative redundant **k=3** mode remains available as a fallback. The randomness behind every assignment and audit draw comes from a **VRF seed**: each validator that anchored a VRF key contributes through its vote-extension, with a verifiable random function whose output anyone can check.

Those three properties are **pinned by a dedicated test suite**, [`chain/x/jobs/keeper/readme_public_claims_test.go`](chain/x/jobs/keeper/readme_public_claims_test.go), which exercises the anchored-committee rule, the two-thirds bar and the zero-seat (fail-closed) case under the **default** parameters — not under a launch-kit setting.

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
two thirds of the committing power. Read the floor with `dendrad query jobs params -o json` ->
`committee_min_vrf_contributors`.

**Two independent floors stand in the path of a job.** The first is applied when a job is opened:
`chain/x/jobs/keeper/pool_freeze.go::enoughEligibleMinersToVerify` counts the miners eligible **and
present** in the pool the job would freeze. Its full floor is `audit_min_quorum` + 1 (a quorum of 0, or
absent from the output, selects the compiled 4 through `effectiveSlashFloor`), because the jury
excludes the miner under audit, and it refuses a job below that floor unless the chain runs optimistic
verification (`verification_mode` 1) with `audit_unwind_blocks` above 0 and `hold_bps` at 10000. With
all three, the floor drops to one present miner: an audited job no jury can conclude then ends with its
client refunded after `audit_unwind_blocks`, so no selected job is paid unverified and nothing is held
for ever. The second floor is the seed floor above, applied to the audit draw:
`dendrad query jobs committee-seed-health -o json` returns `latest_contributors` and the floor side by
side; only a bonded validator moves it, never a miner registration. On a block below either bar no
sampled audit is drawn and the due jobs are deferred, so a proposer gains nothing by withholding the
seed - the chain logs each deferral - and a settled job whose audit draw waits for a seed is refunded to
its client after `audit_unwind_blocks`, exactly as one no jury can conclude.

**Live figures come from the chain**: `/dendra/jobs/v1/params`, `/dendra/jobs/v1/miner`,
`/dendra/jobs/v1/committee_seed_health`. When the open-job refusal fires, it names the shortfall in the
transaction's error.

**The protocol pays for work from block 1.** A miner that registers and serves a job is paid in `$DNDR`
by the protocol, out of the client fee and the pre-allocated Reserve — the mechanism, role by role, is
in [What the protocol pays](#what-the-protocol-pays). With `hold_bps` at 10000, as in the launch
genesis, settlement **retains the whole fee** — the miner's net share, the protocol's cut and the
deferred burn. Finality pays the miner and the validators/team/treasury cut; a retention that no jury
resolves within `audit_unwind_blocks` returns to the client, cut included. The mechanism is in
[Escrow](#escrow).

| Fact | Verify it yourself |
|---|---|
| **Supply starts at exactly 10,000,000 DNDR and can only fall.** `x/mint` is not in the binary, so no code path increases it; the soft burn on fees is the only thing that moves the total, and it moves it **down**. So the check is an **inequality, never an equality**: one denom, `0 < supply ≤ 10000000000000 udndr`, strictly below the genesis figure once any fee has burned, and never larger between two reads. Read it live. | `curl -s http://testnet-api.dendranetwork.com:1317/cosmos/bank/v1beta1/supply` — expect exactly one entry, `denom: udndr`. Compare its `amount` against the genesis total `10000000000000` (in `http://testnet-api.dendranetwork.com:8088/genesis.json`, `app_state.bank.supply`): it must be no larger, and strictly smaller once any fee has burned; the gap is the burn since genesis. Read it twice: it must never have grown. |
| **The served genesis matches the digest served beside it — and the one this repository pins.** The digest served beside the genesis catches a **truncated or corrupted download**. The pinned digest, [`docker/GENESIS_SHA256`](docker/GENESIS_SHA256), reaches you through the repository host instead, and `deploy/join.sh` refuses a served genesis that differs from it, so a forged genesis host is caught as well. | `curl -s -o g.json http://testnet-api.dendranetwork.com:8088/genesis.json && sha256sum g.json` — compare with `GENESIS_SHA256` in `http://testnet-api.dendranetwork.com:8088/network-info.txt` **and** with line 1 of `docker/GENESIS_SHA256` in your clone (`<chain_id>=<sha256>`); all three must agree. If the clone disagrees and the network was relaunched, `git fetch origin && git reset --hard origin/main` first. |
| **The active validator set is read from the chain.** Bonded validators that are not jailed order the blocks; those that anchored a VRF key also contribute to the randomness beacon. The staking registry can also list a **jailed** validator, which keeps its stake but produces no blocks and contributes nothing to the beacon, so the active set is the one to read. Jail for downtime costs stake, which is why [`deploy/README.md`](deploy/README.md#becoming-a-validator-read-this-first) states the cost of the role before you bond. | Active set: `curl -s http://testnet-api.dendranetwork.com:26657/validators` — read `result.total` and compare the `voting_power` values. Registry: `curl -s http://testnet-api.dendranetwork.com:1317/cosmos/staking/v1beta1/validators` — read `status` and `jailed` on each; a validator in the active set is `BOND_STATUS_BONDED` with `jailed: false`. |
| **A draw needs a validator VRF seed, counted block by block.** The launch genesis sets `committee_seed_source=1`, so a work assignment or a sampled audit draw happens only on a block where at least `committee_min_vrf_contributors` validators carried a VRF vote-extension ([`chain/x/jobs/keeper/audit_sampling.go`](chain/x/jobs/keeper/audit_sampling.go), the `committee_seed_source == 1` branch), and only if those contributors carry at least two thirds of the committing power. Contributors are counted **per block**, so this reads as a rate: a validator contributes on the blocks its vote-extension reaches the proposer, and a jailed validator does not contribute. Jobs that found no seed are re-queued for a later block (the chain logs each deferral and emits an `audit_draw_deferred` event), and one whose retention reaches `audit_unwind_blocks` is refunded to its client. Deferral is **fail-closed** throughout: no retained payment released to the miner, no slash fired. | `dendrad query jobs committee-seed-health -o json` — compare `latest_contributors` against `committee_min_vrf_contributors`; a draw runs only when the first reaches the second, **for that block**. `dendrad query jobs audit-deferred -o json` — `job_ids` lists the jobs the lottery selected whose jury was below the floor (`deferred_no_jury`); jobs deferred for want of a seed are not listed there, so read `dendrad query jobs held-summary -o json` (the retained total and the age of the oldest retention). `audits_opened` counts each job's first selection, whether deferred or anchored, plus human disputes. Without `dendrad`, the same figures are served over HTTP: `curl -s https://testnet-proof.dendranetwork.com/proof` |
| **The jury floor is a rule, not a headcount.** An audit's jurors are drawn from the miners **present** in the job's frozen pool (availability proved in the current or previous window), the jury excludes the miner under audit, and a verdict needs `audit_min_quorum` jurors to vote — so an audit can only conclude once **`audit_min_quorum` + 1 miners are eligible and present**. An audit that cannot conclude is refunded to its client after `audit_unwind_blocks`: fee and protocol cut return, and the miner is neither paid nor slashed. Read both numbers from the chain. | `dendrad query jobs list-miner -o json` — `pagination.total` is the registered count, a ceiling on the present one · `dendrad query jobs params -o json` — read `audit_min_quorum` and `audit_unwind_blocks`, and do the addition yourself · `dendrad query jobs held-summary -o json` shows what is retained and how long it has been. Over plain HTTP: `curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/miner` and `.../dendra/jobs/v1/params` |
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
   was asked about — compare `latest_contributors` against `committee_min_vrf_contributors`.
2. **`audit_min_quorum` + 1 present miners.** The jury is drawn from the present, eligible miners of the
   job's frozen pool, except the primary, and it is anchored only once it has at least
   `audit_min_quorum` members. Both outcomes are gated on the quorum: releasing a held fee requires
   `voters >= audit_min_quorum` and so does a slash
   ([`chain/x/jobs/keeper/antievasion.go`](chain/x/jobs/keeper/antievasion.go),
   `auditVindicateDecision` and `auditSlashDecision`). That many jurors, plus the primary they are
   judging, is the miner floor — read `audit_min_quorum` from `dendrad query jobs params -o json` and
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
- **Inference → payment ran end to end on a bench**: real jobs, settled on chain, with the supply
  decreasing by burn and never minting.
- **Audit → committee verdict → slash** is exercised by the chain's test suite, which covers the slash,
  refund and unwind paths, and ran on a bench with miners and judges side by side. On the public
  network, read the same paths live with `audit-deferred`, `held-summary` and
  `curl -s https://testnet-proof.dendranetwork.com/proof`.

A draw that read the **live** miner set would let an identity created after the seed becomes public grind its identifier into a committee — defeating stake-weighting, since hashing is free and stake is not. [ADR-037](docs/adr/ADR-037-gel-du-vivier-de-tirage.md) closes that by freezing the eligible set at job opening, and it is shipped: `OpenJob` writes the anchor unconditionally, both draws filter on it, and a missing anchor is an error rather than a permissive default. `dendra-testnet` runs it from its genesis.

## Run it

Two paths below: join the public network, or run a separate local stack of your own. They use the same
ports, so run one at a time.

### Join the public network (this is what you probably want)

**One command to join.** The config URL below is the live public network's, so these lines run as
written:

```bash
export CONFIG_URL=http://testnet-api.dendranetwork.com:8088/network-info.txt
bash deploy/join.sh              # miner (serves inference on the GPU, else on the CPU, slowly)
bash deploy/join.sh --validator  # full node + guided validator bond + VRF (no GPU)
bash deploy/join.sh --judge      # miner + audit committee (judges on the CPU: at least 26 000 MB of system RAM)
```

**On a bare machine** — no Docker yet, or Windows — `deploy/install.sh` (Ubuntu, Debian, HiveOS) and
`deploy/install.ps1` (Windows: WSL 2 + Ubuntu) prepare the host and then run the same `join.sh`.
Download the file, read it, run it; each prints its plan and changes nothing without `--yes` / `-Yes`.
On a graphical Linux session the installer also adds the desktop application **Dendra**
([`deploy/app/`](deploy/app/)) to your menu: it shows the miner's health, balance and season progress,
starts and stops the miner, and shows the recovery phrase of each new key until three of its words are
typed back. `join.sh` pulls a
prebuilt miner image only when the clone's `docker/MINER_IMAGE` pins one by digest, and builds the image
from the clone otherwise. A miner needs **no inbound port**: it dials out to the relay, the RPC and the faucet, so a
connection behind carrier-grade NAT qualifies.

### Or run a separate local stack of your own

This starts a **separate chain that is not connected to anything** — your own genesis, your own
validator, no peers. It is for reading the code with something running in front of you, not for
joining.

```bash
# `docker/Dockerfile.services` does `COPY --from=dendra/chain:latest`, so the chain image must exist
# BEFORE the services image is built. Compose does not order builds (`depends_on` orders start-up, not
# build), and a bare `up -d` on a clean machine fails with "pull access denied for dendra/chain" -- a
# message that blames a missing registry for a missing local build step.
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
rate or amount**; Final Testnet Season rewards — verdicts included — follow the formula published in
[ADR-047](docs/adr/ADR-047-final-testnet-season-reward-programme.md). Both are paid in testnet `$DNDR`. The
`--validator` path is also the one that touches the draw described above: it anchors a VRF key, and a
validator's VRF key counts toward `committee_min_vrf_contributors` on every block its vote-extension
reaches the proposer.

- **Full guide — every role (node / miner / validator / operator):** [`deploy/README.md`](deploy/README.md).
- **Wallet:** [dendranetwork.com/wallet](https://dendranetwork.com/wallet/) — create/import, check balance, send DNDR (testnet). Keys stay in the browser. The same page is in this repository ([`wallet/web/index.html`](wallet/web/index.html)) if you prefer to serve it yourself.
- **Advanced / manual node & miner kits:** [`deploy/testnet-node/`](deploy/testnet-node/), [`deploy/testnet-miner/`](deploy/testnet-miner/).
- **Operator (host the network):** [`deploy/launch/`](deploy/launch/) (one-command public launch) and `deploy/testnet/publish_network.sh`.

**Content filter.** The gateway screens every prompt with a content filter before sealing it to the miner: a CPU regex stage (patterns kept out of the repository), and an LLM classifier stage built into [`services/content_filter.py`](services/content_filter.py) that switches on with `DENDRA_GUARD_MODEL`.

## Tokenomics (fixed supply, zero mint)

- **Fixed supply: 10,000,000 DNDR at genesis, never more — the fee burn only lowers it. Zero inflation, zero mint.** Custom modules have no `Minter`; the standard `x/mint` module is **removed from the chain binary entirely** — fixed supply holds **by construction** (a fresh genesis boots to exactly 10,000,000 DNDR).
- Genesis allocation: community 33.82% / reserve 33% / validator delegation pocket 20% / validator 7% / team 5% / faucet float 1% / Final Testnet Season payout account 0.15% / request generator 0.03% — 10,000,000 DNDR exactly. Read it from the genesis the endpoint actually serves (`http://testnet-api.dendranetwork.com:8088/genesis.json`), or from the script that builds it, [`docker/entrypoint-chain.sh`](docker/entrypoint-chain.sh). The committed [`chain/config.optimistic.yml`](chain/config.optimistic.yml) carries the same allocation, but it is a **local** genesis for the replayable slash demonstration: several of its verification parameters differ from the public one on purpose, and its own header says which.
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
2. **The pre-allocated Reserve** — 33% of genesis, held by the `x/emission` module account from block 1
   and released in **decreasing slices** (a governable share of what *remains*, never of a fresh
   supply) every `epoch_blocks`: [`chain/x/emission/keeper/epoch.go`](chain/x/emission/keeper/epoch.go).

There is no third origin. `x/mint` is removed from the binary, so a reward cannot be minted even by
governance — pinned by [`chain/app/fixed_supply_test.go`](chain/app/fixed_supply_test.go).

**Miner** — three distinct flows:

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
  (`avail_epoch_blocks`) can run for measurement alone — they decide which miners are **present**, and
  only present miners are drawn for work or audit — while the on-chain payout flows only while
  `avail_payout_bps` is non-zero. Read both with `dendrad query jobs params -o json`.

**Validator** — transaction fees as on any Cosmos chain; a share of the protocol cut
(`validator_reward_bps`), retained per job and sent to `fee_collector` at finality; and the **security
flow** of each epoch, the one emission slice that leaves
the module account, sent to `fee_collector` and thus to validators and their delegators through
`x/distribution` ([`epoch.go`](chain/x/emission/keeper/epoch.go)).

**Whistleblower** — anyone may dispute a job by posting `dispute_bond`. An **upheld** dispute returns
the bond **and** pays a reward, bounded by the remaining Treasury (the `upheld` branch of
[`msg_server_adjudicate.go`](chain/x/jobs/keeper/msg_server_adjudicate.go)). An **unfounded**
dispute forfeits the bond to the Treasury: the reward is paid for by the people who guess wrong, which
is what keeps it from being free money.

**Juror** — paid by no path on chain ([ADR-038](docs/adr/ADR-038-incitation-des-jures.md)): a juror is
a miner, paid for the jobs it serves. Verdicts consistent with the outcome are rewarded by
the [Final Testnet Season](#final-testnet-season), off chain, by its published formula.

### Escrow

`hold_bps` is at its maximum (10000) in the launch genesis, which means the module **retains** the
whole fee at settlement — the miner's net payment, the protocol's cut and the deferred burn — rather
than handing anything over on the spot, and resolves it at the job's audit checkpoint: finality, the
draw of the next block.

**The order of the two checks decides what happens.** The VRF-seed test runs *before* the per-job
lottery and returns early below the contributor floor
([`audit_sampling.go`](chain/x/jobs/keeper/audit_sampling.go)): on a block without a seed, no job
reaches the draw at all and **every** due retention stays held — not a fraction of it. On a block with
one, the checkpoint resolves each job one of two ways:

- a job the audit lottery does **not** select is **released** at that checkpoint: the miner is paid and
  the cut goes to validators, team and treasury (same file, the `releaseHeld` branch). The selected
  share is `audit_sample_bps`, a governable parameter — read it with
  `dendrad query jobs params -o json` (or `/dendra/jobs/v1/params`);
- a job the lottery **does** select waits for its jury. An upheld verdict pays it; a conviction slashes
  the miner and refunds the client.

**Held is retained — not cancelled, not forfeited, and not held for ever.** A selected job waits for its
jury, and if none concludes within `audit_unwind_blocks` blocks, the retention returns to the client,
fee and cut together; the miner is neither paid nor penalised, and its stake stays intact
([`audit_unwind.go`](chain/x/jobs/keeper/audit_unwind.go), `unwindStrandedRetention`). A settled
job whose audit draw waits for a seed unwinds the same way. Releasing the payment because the audit
could not run would hand an abstaining validator the power to push jobs through unverified; refunding it
instead means nothing is held for ever and nothing unverified reaches the miner.

This is the protocol failing closed. A payment that cannot be verified is not released — and it is not
destroyed either: it goes back to the client who paid it. `held-summary` reports the age of the oldest
retention, so this stays checkable.

## Confidentiality — who reads a prompt

On the public chat and API path the prompt reaches the Dendra gateway over HTTPS. The gateway runs the content filter, then seals the prompt to the serving miner's on-chain key with X25519 + HKDF-SHA256 + AES-256-GCM ([`services/modea/crypto.py`](services/modea/crypto.py)), and decrypts the answer it returns to you over HTTPS.

- **The gateway** reads the prompt, filters it, and encrypts it to the serving miner.
- **The relay** carries ciphertext only.
- **The serving miner** decrypts the prompt in memory to answer it. The miner software confines its own process (no core dumps, no debugger attach from the same user, no privilege gain) where the host allows it. While the model answers, the miner software keeps the prompt out of crash dumps and blocks writing it to disk or sending it anywhere but the model, where the host allows it ([`services/modea/miner.py`](services/modea/miner.py), `handle_job`).
- **The jurors**, if the job is drawn for audit: the miner re-seals the prompt and the answer to them, and they read both to judge.
- **The chain** holds a salted hash of the prompt and an embedding of the answer, never the text.

Running the client or the gateway on your own machine keeps the plaintext off the Dendra gateway. As with any hosted model, keep secrets out of your prompts.

## Repository layout

```
chain/               Cosmos SDK 0.53.6 chain (source of truth) — x/{jobs,emission,modelregistry}, proto, genesis
services/            Reference off-chain stack: gateway, miner, relay, client, judge glue, regex content-filter, exporter, faucet
tokenomics/          Canonical economic model (tokenomics_v5)
deploy/              Node / validator / miner kits + one-command join.sh + public launch kit
docker/              Dockerfiles, entrypoints, monitoring provisioning
wallet/              Web wallet (single-file HTML) + docs
docs/litepaper.md    The litepaper
docs/adr/            Architecture Decision Records — the reasoning behind every protocol choice
```

## Verification & security model

Every mechanism below ships in this repository and is covered by its tests.

- **Optimistic verification + LLM-as-judge** (two-stage: coherence, then same-fact).
- **Anti-evasion**: a silent primary earns nothing — the shipped juror abstains on a missing reveal, so the held fee unwinds to the client after `audit_unwind_blocks` and the stake is untouched (ADR-044); fee-hold v2 ("never the bond" — clawbacks come from the withheld fee, never the security bond); pro-honest floor before any hard slash: "invalid" from **≥2/3 of the anchored committee seats** and a strict majority of the voting stake, with at least `audit_min_quorum` voters.
- Assignment via a **VRF beacon aggregated from validator vote-extensions** (see [The live network](#the-live-network)). Anti-Sybil via **non-recoverable demand**.

The reasoning behind each of these choices — including the ones that were tried and abandoned — is published in full: see [`docs/adr/`](docs/adr/README.md). The [litepaper](docs/litepaper.md) is the short form.

## Contributing & security

- Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Vulnerability disclosure: see [`SECURITY.md`](SECURITY.md) — please report privately, do not open a public issue for security bugs.

## License

[Apache-2.0](LICENSE). See [`NOTICE`](NOTICE).
