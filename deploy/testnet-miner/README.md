# Join the Dendra testnet — all-in-one miner on a GPU, or judge on the CPU

> **Simplest: the desktop application "Dendra"** (`deploy/app`, Linux first), **set up by `deploy/install.sh`** (Ubuntu, Debian, HiveOS; `deploy/install.ps1` on Windows, WSL 2): download the installer, read it, then run it with `--yes` (`-Yes` for `install.ps1`) — nothing happens without that flag, and it is never piped from `curl` into a shell. It prepares the host, runs `deploy/join.sh` itself and, on a graphical Linux session, adds the application to your menu.
> Docker in place? `CONFIG_URL=<network-info.txt> bash deploy/join.sh` (one command, sane defaults, self-diagnostics). This kit remains the **advanced/manual** path.
> A rented GPU pod (RunPod, Vast) runs one container and cannot run this kit: see [`../cloud/README.md`](../cloud/README.md). A HiveOS rig: [`../hiveos/README.md`](../hiveos/README.md).

Run your machine as a **miner** on the Dendra public research testnet, `dendra-testnet`: you receive encrypted
inference jobs, serve them **on your machine**, and are **paid** `DNDR` per job once it clears its audit checkpoint.
The [Final Testnet Season](#final-testnet-season) adds rewards on top, computed by its published formula.
No need to launch a chain —
this package only runs **your local engine (Ollama) + the miner client**, connected to the operator's
**public** endpoints.

**Join with a GPU or with a CPU.**
- **GPU (NVIDIA): mine** — the hardware probe picks the open model your card can serve.
- **CPU: judge, for now** — the network's judge model, a mixture-of-experts, runs on the CPU. It needs at
  least `MOE_CPU_MIN_RAM_MB` of system RAM (`bash deploy/hw_probe.sh --judge-floor-mb` prints it), with or
  without a GPU (under Windows, the RAM of WSL 2). A juror is a registered miner, so a machine without a card
  also serves the requests the chain assigns it, with that same judge model on its CPU. See
  [Judging on the CPU](#judging-on-the-cpu).
- **Full node or validator:** no GPU.

No mining model runs on the CPU: below `MOE_CPU_MIN_RAM_MB`, a machine without a usable NVIDIA card has no
role, and the installers refuse it before they change anything (`bash deploy/hw_probe.sh --role` says why).
The installers ask for the judge role by default; `deploy/install.sh` applies the judge's higher disk floor
(`MIN_DISK_GB_JUDGE`) only to a host that `deploy/hw_probe.sh --can-judge` says can judge. `--miner`
(`-Miner` on Windows) installs a miner only, on a card Docker can use. More roles and technologies for
CPU-only machines are planned.

> ⚠️ **Paid, then held.** At settlement `hold_bps` retains your net share — all of it at `10000` — together
> with the protocol's cut. What happens next is decided by the job's audit checkpoint, the draw of the
> following block, which selects a share `audit_sample_bps` of settled jobs for re-audit:
> - **not selected**: your share is released at that checkpoint;
> - **selected**: you are paid when the verdict upholds your answer, which needs `audit_min_quorum` drawn
>   jurors to vote, the miner under audit excluded; on a conviction you are slashed and the client is
>   refunded; if no jury concludes within `audit_unwind_blocks` blocks, the fee **and** the cut return to
>   the client. A selected job is never paid unverified and never held for ever. A settled job whose audit
>   draw waits for a seed unwinds the same way. That bound is also why a job opens while fewer than `audit_min_quorum` + 1
>   eligible miners are present, provided verification is optimistic (`verification_mode` = 1),
>   `audit_unwind_blocks` > 0 and `hold_bps` is `10000`
>   (`pool_freeze.go::enoughEligibleMinersToVerify`): its pool is frozen at birth, so if it is selected
>   it ends in that refund.
>
> Release and upheld verdict are both **finality**: it pays the retained cut to its owners and credits
> the work subsidy to you as a right, which the miner software claims by itself (`MsgClaimSubsidy`). A
> refund drops that right.
>
> The randomness of every assignment and audit draw comes from the VRF beacon. While
> `committee_min_vrf_contributors` is 1, one validator's VRF output carrying at least two thirds of the committing power is enough to seed a draw, and
> `dendra-testnet` launched from a genesis with a single validator, operated by the project: at launch, the
> project's validator alone produces that randomness. Raising `committee_min_vrf_contributors` takes independent validators and
> a governance vote. Read the parameters rather than this page:
> `curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/params` (or `dendrad query jobs params -o json`),
> the validator set at `curl -s http://testnet-api.dendranetwork.com:26657/validators` (or `dendrad query staking validators`),
> the beacon at `/dendra/jobs/v1/committee_seed_health`, and `vrf.contributors` against `vrf.min` at
> `https://testnet-proof.dendranetwork.com/proof`. Read [`../README.md`](../README.md) before you budget for this.

> **The model is LOCAL.** It is downloaded to **your** machine (Docker volume) and served by **your** GPU,
> or by your CPU on a machine without one.
> No central model serves the network: each prompt is sealed to the key of the miner that serves it,
> which decrypts it in its own memory.
>
> **Status: public research testnet, `dendra-testnet`.** `$DNDR` is a utility token used inside the
> protocol to pay for inference and reward miners; there is no token sale, presale or ICO. Testnet DNDR is
> never sold, Final Testnet Season rewards count as points (each testnet DNDR the season paid becomes one
> mainnet DNDR, credited in the mainnet genesis to the address that received it), mainnet launches when
> the season ends, and the chain can be reset: a reset erases balances; the season's
> published rankings are archived before any reset. On your machine, privacy is deterrence (sealed
> memory, confinement), not a cryptographic guarantee against a root miner: you decrypt the prompts you
> serve, and, as a juror, read the prompts and answers you judge. From kit v0.2.1 a miner seals the reveal
> of an audited job for the jurors the chain anchored for that audit only; a miner on a kit of an earlier
> release seals it for every other registered miner, yours included. A juror that registered no encryption
> key gets a copy sealed to the key the relay serves for it, which a relay that replaces that key can
> open. No slash punishes a leak.

## Prerequisites
- **Docker** + Docker Compose v2 (`docker compose version`).
- **An NVIDIA GPU** + `nvidia-container-toolkit`, given to the engine (see [NVIDIA GPU](#nvidia-gpu)). No mining
  model runs on the CPU: before it starts, the miner's container checks where its engine holds the served
  model (`serve_guard.py`) and refuses a mining model the CPU holds — the judge role on the CPU aside.
- **~6 GB of disk** for the model.
- **To judge (optional):** at least `MOE_CPU_MIN_RAM_MB` of system RAM, with or without a GPU, and room for the judge
  model's weights: the installers ask for the judge role by default, and `deploy/install.sh` applies the
  judge's disk floor (`MIN_DISK_GB_JUDGE`) when `deploy/hw_probe.sh --can-judge` says the host can judge;
  pass `--miner` (`-Miner` on Windows) to install a miner only. See [Judging on the CPU](#judging-on-the-cpu).
- The testnet's **public endpoints** (RPC / relay / faucet / `DENDRA_FINAL_SEASON_URL`) — from the operator's `network-info.txt`.
- The **cloned repository** (an image built from it compiles the chain binary `dendrad`). `deploy/join.sh` pulls a
  prebuilt miner image only when the clone's `docker/MINER_IMAGE` pins one by digest, starts it only when
  its platform is that of your Docker engine, and builds the image from the clone otherwise. By hand, copy
  that file's `DENDRA_MINER_IMAGE=` line into `.env`, run `docker compose pull miner`, and start it with
  `docker compose up -d --no-build` only if `docker image inspect --format '{{.Os}}/{{.Architecture}}'` on
  that image prints what `docker version --format '{{.Server.Os}}/{{.Server.Arch}}'` prints.

## 3 steps
```bash
cd deploy/testnet-miner
cp .env.example .env            # 1) copy the config
nano .env                       # 2) fill in DENDRA_NODE / DENDRA_RELAY / FAUCET + a unique MINER_ID, and add DENDRA_FINAL_SEASON_URL (see Final Testnet Season)
docker compose up -d --build    # 3) start (builds the image from this clone: compiles dendrad + pulls the model;
                                #    with DENDRA_MINER_IMAGE=<digest> in .env, use `up -d --no-build`: never --build with a digest)
                                #    Give the card to the engine first (NVIDIA GPU, below): a mining model the CPU
                                #    holds is refused at start (serve_guard.py: SERVE REFUSED in the miner's logs).
bash publish-capacity.sh        # 4) declare this machine to the network registry, signed, once the miner
                                #    logs `ready` (before the chain records it, nothing is sent: exit 2)
```
Follow: `docker compose logs -f miner`. You should see the faucet self-funding (it solves the faucet's
proof of work first, on a public network), the on-chain registration, then the job loop.

> **With your own node on this machine** (deploy/testnet-node, started first), the miner reads it on the
> node kit's `dendra-chain` Docker network rather than through the host — the node's RPC stays published on
> `127.0.0.1` only. Two lines of `.env` say so (both are in `.env.example`):
> `DENDRA_NODE=tcp://dendra-node:26657` (the node's compose project name) and
> `COMPOSE_FILE=docker-compose.yml:docker-compose.local-node.yml` (add `:docker-compose.override.yml` when
> that GPU file exists). `deploy/join.sh` writes both. Run compose **from this directory, without `-f`**:
> `-f docker-compose.yml` skips `COMPOSE_FILE`, and `up` then recreates the miner without the network that
> reaches its node.

> **`DENDRA_RELAY_TOKEN` is optional — a miner authenticates by signing.** The relay applies a policy
> **per route**, because the routes do not share a threat model:
>
> | Route | Needs |
> |---|---|
> | `GET pub/…` `req/…` `res/…` `reveal/…` — reading your work | **nothing.** Bodies are sealed to your **on-chain anchored** key, and the design assumes the relay is hostile, so a secret in front of ciphertext protected nothing. |
> | `POST pub/…` `res/…` `reveal/…` `attest/…` — everything a miner writes | **your signature**, made with your miner key. The kit wires `DENDRA_SIGN_KEY` for you. The shared token still works as a fallback for older relays. |
> | `GET list` — the queue that tells you a job awaits you | **nothing.** Gated, this is what leaves a joined miner registered and idle: no job, no error, nothing to search for. A secret here would hide only what the chain publishes to anyone anyway (`assigned_committee/<jobId>`). |
> | `POST req/…`, `POST list`, `GET stats` | the token — the gateway's route, and two surfaces nothing on the path to work depends on. |
>
> So leave `DENDRA_RELAY_TOKEN` empty. A `401` on a **read** means the operator runs an older relay
> build that gates every route behind a shared token — ask them to update rather than asking for a
> secret you would then have to keep.

> **Step 4 is not decoration, and it does not repeat itself.** The capacity registry is what the public
> `/network` page displays — and what the public **chat** reads before it will accept a prompt at all.
> With no report, `verified.live_nodes` is `0`, the chat closes its composer on purpose ("a page that
> cannot confirm a miner is serving must not imply one is"), and your miner answers nothing while looking
> perfectly healthy in its own logs. The failure is invisible from the operator's side: `0 nodes` reads
> like a young network, not like a missing publication.
>
> The probe must run **on the host** — the miner container has no GPU, so a report produced inside it
> would declare zero cards. And the registry ages a report out after 24 h and purges it after 7 days,
> so publish on a schedule rather than once:
> ```bash
> (crontab -l 2>/dev/null; echo "17 * * * * bash -lc 'bash $PWD/publish-capacity.sh' >> /tmp/dendra-capacity.log 2>&1") | crontab -
> ```
>
> **Keep the `-lc`, and keep the log.** cron runs with a minimal `PATH`, and the hardware probe resolves
> `nvidia-smi` through it — under WSL that binary lives in `/usr/lib/wsl/lib`, which a non-login shell does
> not carry. Without the login shell the probe finds no GPU, and instead of failing it publishes a
> **CPU-tier inventory advertising a smaller model than this miner actually serves**. The page then shows
> that as your own declaration, which is worse than showing nothing. The script refuses to publish
> when it can see the contradiction, but it can only see the cases it knows about.
>
> **It sends a SIGNED report of a REGISTERED miner, or nothing.** The report is signed with the miner's key
> (inside the miner container, where the keyring lives), and it is sent only once the chain records this
> identity with that key as its operator — read in the same place, before signing. An unsigned
> report, or one the registry cannot prove, would be listed beside the signed one of the same machine. Until
> the registration reads, each run sends nothing and says why: exit `2` when the chain does not record the
> miner (yet), `3` when the registration could not be read, `1` for a failure, `0` once published. The first
> report goes out at the first run after the registration; `deploy/join.sh` runs it at once when it sees the
> registration, and schedules this job either way.

## Final Testnet Season
The Final Testnet Season (ADR-047) ends on 7 November 2026 at 23:59 UTC: it counts every block
timestamped before 8 November 2026, 00:00 UTC — the chain's own block times decide. Season days are
17 280 blocks each; the last one is cut at the end. It is a reward programme on top of job pay: no
registration and no test, paid weekly in DNDR per miner identity and per programme day, by a published
formula applied to what the chain records and to the programme's published evidence — the jobs whose
answer it received, and the grades
(`final_season_calc.py::gross_of`, rates in `final_season_rules.py::RULES`):
- **work** — 0.05 DNDR per verified programme request (settled, past its audit, not refunded), the same
  rate for every identity; from the season day the season service names in
  `unwound_audit_work_from_day`, a programme request refunded because no audit of it concluded in time
  counts too, when its answer reached the programme and a grading clears it — its own answer graded
  coherent, or, not graded itself, its miner's grades that day not voiding the day's work; with no grade
  at all it is not paid (ADR-047, decision 18; `final_season_facts.py::unwound_grading`);
- **juror** — 0.008 DNDR per verdict consistent with the outcome, drawn jurors only, on audits of the
  programme's own requests; this kit posts verdicts only with `DENDRA_MINER_JUDGE=1`, on a machine that
  runs the judge model on its CPU ([Judging on the CPU](#judging-on-the-cpu));
- **presence** — 0.002 DNDR per availability window proven on chain (`MsgProveAvailability`, read from the
  transaction index by `final_season_chain.py::presence_proofs`), at most 50 a day (so at most 0.1 DNDR a
  day), paid **only on a day the identity also has at least one verified request**.

Caps, on the programme as a whole only: 50 DNDR a day (beyond it, every payable of the day is reduced by
the same ratio) and 1 500 DNDR over the season. There is no cap per identity: the work an identity gets
is the chain's stake-weighted draw over a fixed daily volume, so a cap per identity would only pay an
operator to split its stake into identities that each stay under it. There is no public-node reward. The
rules and the formula: https://dendranetwork.com/final-season/.
- **A fixed volume of work.** The programme sends `requests_per_day` requests a day
  (`final_season_rules.py::RULES`), whatever the number of miners; the chain hands them out among its
  **present** miners, drawn by stake. Each identity's stake weighs in the draw up to a ceiling,
  `assignment_stake_cap_multiple` × `min_stake` (`chain/x/jobs/keeper/committee.go::capAssignmentWeights`).
  The launch genesis sets the multiple to 100, the highest the chain accepts: up to 100 × `min_stake` an
  identity's weight is its stake, so splitting a stake into more identities buys no more work, and a larger
  stake draws a larger share of it; above the ceiling, splitting does buy more (several identities each at
  the ceiling weigh more than one). Read both on the chain: `dendrad query jobs params -o json` →
  `assignment_stake_cap_multiple`, `min_stake` (a multiple of 0, or absent from the output, selects the
  compiled default of 2; a `min_stake` of 0, or absent, sets no ceiling).
- **The miner does it by itself.** It serves programme requests like any other job, and proves
  availability windows on chain with its VRF key (`miner.py::prove_availability_once`). A miner
  with no VRF key proves no window: past the window it registered in and the next one, the chain no
  longer draws it for work (`presence.go::minerPresentAt`), and its log says so once an hour.
- **The work is checked.** After each day the programme service draws at most three of each identity's
  answers to programme requests, and a language model grades whether each is a coherent attempt
  (`final_season_grader.py`). A day whose graded answers are all incoherent — with at least two grades, or one
  when a single answer was sampled — loses its work reward, and with it its presence reward.
- **Only answered work counts.** The programme's request generator forwards each answer to its service before it
  settles the job (`final_season_generator.py::run_one`); the day's seal lists the job of every answer
  received, and a programme job counts as a verified request only if it is in that list
  (`final_season_facts.py::answered_jobs`). A miner can anchor a commit and anyone can settle a job, so a job
  can be paid on chain with no answer delivered: that job earns no season reward. Likewise, a job whose
  answer the programme did not record — the generator's forward failed, or the service was down — is
  paid on chain to the miner as usual but earns no season work reward. A day that received answers is
  ranked only once they are sealed.
- **`DENDRA_FINAL_SEASON_URL` is where the programme answers.** `deploy/join.sh` writes it into `.env` from
  `network-info.txt`; by hand, add the line `DENDRA_FINAL_SEASON_URL=<value from network-info.txt>` yourself.
  The payout declaration below and the Dendra application's season view read it. Rewards do not depend
  on it: an identity is ranked on what the chain records and on the programme's published evidence, and
  without a declaration its rewards go to the default address described under **Payout address** below.
- **Limits, stated.** A presence proof proves an online operator key and VRF key, not a model: that is
  why presence is paid only on a day with verified work. One card can serve the programme work of many
  identities. The volume is fixed, so a farm cannot raise the programme's total of paid work, and, up to
  the ceiling above, splitting a stake buys no more of it; above the ceiling, several identities each at
  the ceiling draw more of it than one. On either side of the ceiling, splitting also buys presence and
  jury seats. Each identity with at least one verified request that day earns its own presence reward,
  and a smaller identity draws a request, and so earns its presence, on fewer days. The audit draw
  (`chain/x/jobs/keeper/audit_committee.go::drawMembersWithDomain`) gives an identity at most one
  seat, draws by stake with no ceiling, and seats every eligible miner when they are fewer than its
  `audit_committee_draw_size` seats (0, or absent from the output, selects the compiled 15), so
  several identities can sit on one programme audit, each paid 0.008 DNDR per verdict consistent with
  the outcome (verdicts are posted only with `DENDRA_MINER_JUDGE=1`). What an identity costs is its registration and `min_stake`; the programme's
  50 DNDR a day, shared pro rata, and 1 500 DNDR over the season bound what is paid.
- **Payout address.** Rewards go to the address you declare (in the Dendra application, the **Where
  rewards go** card runs this command with the address you enter; by hand, from this directory, with your
  own address in place of `dendra1...` — the command finds your miner id by itself):
  `docker compose exec miner python3 final_season_miner.py payout --address dendra1...`
  (A miner in owner mode declares with its owner's key: see
  [Owner mode](#owner-mode-the-stake-on-a-key-that-is-not-on-this-machine).) Without a declaration, they go to the operator that signed the identity's availability proofs that day
  (`final_season_chain.py::presence_proofs`), else, for an identity with no proof that day (a juror only, for
  instance), to its operator in the miner registry. With no address known, the identity is still ranked,
  with a payable of 0 and the reason `no payout address known`. After the season's end the programme
  refuses new declarations: one made then would apply to no day.
- **What the rewards are.** They count as points: each testnet DNDR the season paid becomes one mainnet
  DNDR, credited in the mainnet genesis to the address that received it (at most 1 500 mainnet DNDR in
  all), and mainnet launches when the season ends; faucet grants, transfers, on-chain job pay and the subsidy
  are not points, and testnet DNDR is never sold. Each day is ranked once final and published with its
  evidence; anyone can recompute it with `final_season_rank.py`.

## Judging on the CPU
The network's judge model, a mixture-of-experts, runs on the CPU, in a second Ollama instance of its own
(`ollama-cpu`, CPU-only, in the `judge` profile), so a GPU keeps mining while the CPU judges. No judge is
seated on the GPU. It needs at least `MOE_CPU_MIN_RAM_MB` of system RAM (`bash deploy/hw_probe.sh
--judge-floor-mb` prints it), with or without a GPU; under Windows, the RAM
that counts is the WSL 2 VM's, about half the PC's by default, and `memory=` in the `[wsl2]` section of
`%UserProfile%\.wslconfig` raises it. Check the machine with `bash deploy/hw_probe.sh` from the repository
root. A juror is a registered miner: the chain draws jurors and work among the same registered, present
miners, so a machine that judges also serves its share of requests: on its card when it has one; without
one, with the judge model itself, on the CPU instance — never with a mining model.

From kit v0.2.1 the judge checks a reveal against the answer the miner anchored with the miner's own
embedding function, and a divergence it finds between the answer and its own reference answers is posted
as an abstention, not as an invalid vote, while `DENDRA_JUDGE_DIVERGENCE_SLASH` is unset or 0: the kit's
default, which the project lifts only after a dated measurement of a jury of the judge model alone
(ADR-057). An answer that is not a coherent attempt at the request is still voted invalid. Leave the
variable unset.

By hand: uncomment the audit-committee lines of `.env` (`DENDRA_MINER_JUDGE=1`,
`DENDRA_JUDGE_ENDPOINT=http://ollama-cpu:11434`), then `docker compose --profile judge up -d`. The canonical
path does it for you: `CONFIG_URL=<network-info.txt URL> bash deploy/join.sh --judge`.

## NVIDIA GPU
Uncomment the `deploy:` block of the `ollama` service in `docker-compose.yml` (requires
`nvidia-container-toolkit` installed on the host), then `docker compose up -d`. Without it, Ollama runs on the
**CPU**, and the miner's container refuses to start a mining model there (`serve_guard.py`).

The kit ships **without** that reservation on purpose: a GPU reservation on a host that has no NVIDIA
runtime makes `docker compose up` fail on `could not select device driver`, which names nothing. The
canonical path (`deploy/join.sh`) writes a `docker-compose.override.yml` when it detects a GPU *and* the
toolkit, and removes it otherwise — that file is a machine artifact and is git-ignored, so never copy it
between machines. When `.env` carries a `COMPOSE_FILE` line, compose no longer reads the override by
itself: it has to be listed in that line, which `join.sh` does.

## Several GPUs: one identity per card (`--gpus`)
```bash
bash deploy/join.sh --gpus all --plan                                  # what it would do; writes nothing, starts nothing
bash deploy/join.sh --gpus all --payout-address dendra1...             # one miner identity per NVIDIA card
bash deploy/join.sh --gpus 0,2 --payout-address dendra1...             # these cards only (indices from nvidia-smi -L)
```
Without `--gpus` the kit runs ONE identity on the machine, whatever its number of cards, exactly as before.
With it, each card becomes a miner identity of its own, a **slot**, bound to the card by its UUID: slot 0 is
this installation (project `dendra-miner`, this directory's `.env`), never renamed; each other card gets a
project `dendra-miner-g<k>` and machine files in `gpu/<k>/` (the env, which carries the relay token, and the
override that pins the card — never published, never copied between machines). Slots start one after the
other: slot 0 first, then each next one only once the RAM left, **measured** with slot 0 running, covers one
more (`DENDRA_RIG_RESERVE_MB` keeps room for the system), and once the previous one has registered.

What one more card is, said by its mechanism: an identity with **its own stake**; it shares, by stake, a
fixed daily volume of requests (`services/final_season_rules.py::RULES`); its presence is paid only
on a day it served a verified request; it sits on juries; it adds **no** work to the network; and the
identities of one operator may sit on the audit of that operator's own work (the draw excludes no operator).

What each identity costs:
- **one faucet drip and one stake.** The faucet caps drips per IP and per day (`DENDRA_FAUCET_IP_DAILY` in
  `faucet.py`): an identity it refuses retries by itself, and `join.sh` stops starting the next ones at
  the first such refusal and prints the command that resumes them (slots already running are left alone).
  The stake is the chain's `min_stake` (`dendrad query jobs params -o json`).
- **one keyring and one 24-word recovery phrase**, each written down on its own (the Dendra application
  shows them one identity at a time). The keyring passphrase is ONE file for the whole machine.
- **a payout address**: with two identities or more, `join.sh` refuses without one (`--payout-address`, or
  the one already in `.env`), and every identity declares the same. A hot key lost then costs its stake, not
  the season's pay. Owner mode (`--owner`) is refused with several cards: one owner address registers one miner.

**One CPU judge per machine.** With `--judge`, slot 0 hosts the judge's engine (`ollama-cpu`) and every other
identity votes through it, by the alias `dendra-judge-cpu` on a network of its own (`dendra-rig`). A machine
that cannot judge runs its identities without it, and `join.sh` says how many are then drawn as jurors and
never vote; `DENDRA_JUDGE_REQUIRED=1` makes that a refusal.

Every command for one identity goes through `slots.sh`, which hands compose that slot's project, env and
files — a slot's project named by hand with `-p` alone reads slot 0's `.env` and acts on slot 0's identity:
```bash
bash slots.sh list                          # every slot: number, project, state, card, identity
bash slots.sh run <k> logs -f miner         # any compose command, for slot k
bash slots.sh run <k> up -d --no-build
bash slots.sh pin <k>                       # does slot k's engine run on its own card?
bash miner_health.sh --slot <k>             # one identity; without --slot, every slot and one verdict
bash exit-miner.sh --slot <k> [--yes]       # leave the network with that identity only
bash encrypt-keys.sh --slot <k> | --all
```
Re-running `join.sh` without `--gpus` keeps the slots as they are. A card left out of a later `--gpus` list
is **retired**: its identity is stopped (never removed: its volume holds a staked key) and stays registered
— drawn as a juror while it is fresh, it never votes — until `bash exit-miner.sh --slot <k> --yes` takes it
off the network with its stake. A card that disappears is neither started nor re-bound in silence:
`--reassign <k>=<card index>` binds its identity to another card on purpose.

Not measured on real hardware here: whether, under WSL 2, a container sees only the card it was given. Each
slot is held to its card by `CUDA_VISIBLE_DEVICES` in every case, and the hourly self-test's pinning check
(H6) says whether that held — an engine that falls back to the CPU in silence is reported, never assumed.

## Health: is this miner doing its job?
```bash
bash miner_health.sh            # the report
bash miner_health.sh --json     # the same, as one JSON document (the Dendra application reads it)
bash miner_health.sh --quick    # without the model probe and the relay check
bash miner_health.sh --write    # the relay check also re-deposits the anchored key, signed
```
A miner can look healthy — containers up, logs scrolling — and earn nothing. This check asks, on this host,
whether the kit's containers run, whether the running image is the one this clone pins, and whether this
clone and that image are at the kit version and consensus epoch the network publishes (read again from the
`CONFIG_URL` that `join.sh` writes into `.env`). Then it runs `miner_selftest.py` **inside** the miner
container: the node answers and follows the chain, the miner is registered with the VRF key it holds, it
proved its presence in the current or previous window (the rule the chain applies to every draw), the relay
serves its work queue and its copy of the miner's encryption key does not differ from the chain's, the
model answers a short generation and an embedding through the calls a job makes, its capacity line is fresh
and attributed, and the daemon and its workers run. It writes nothing anywhere unless you pass `--write`:
then the relay check also re-deposits, **signed**, the key the chain anchors — never another one, and
nothing at all when it cannot sign. Latency is reported, not judged: this check reads no response budget
to judge it against. The self-test bounds itself inside the container, and runs one at a time. The host
needs Docker and nothing else.

Every check answers one of three words: **ok**, **ko**, or **not measured** — a reading that failed (a node
that did not answer, a document it could not read), never folded into ok. Exit codes: `0` every check ok,
`1` at least one ko, `2` no ko but something could not be measured (or nothing could be: a run that measured
nothing is never a 0).

`join.sh` schedules it hourly (crontab, `bash -lc`, minute 29, without `--write`) and runs it once at the
end of the join.
**`miner-health.ALERT` exists in this directory only while something is wrong** and holds the report, with
the time the problem was first seen; a run that could not measure everything writes a note there instead,
and never erases a standing alert. `miner-health.last.json` is rewritten on every run, with the schedule's
period (`schedule_period_s`, from `SCHEDULE_PERIOD_S` in `miner_health.sh`): the application shows the run
with its age, and a last run older than two periods means cron is not running (under WSL:
`sudo service cron start`). The image's own healthcheck reports only whether the daemon's loop is moving;
the verdict is this script's. `exit-miner.sh` records an exit in `miner-health.EXITED`, so a miner that left
the network on purpose is not reported while it stays stopped.

With one identity per card (`--gpus`), the hourly line stays ONE: it checks every active slot one after the
other, writes one `miner-health.ALERT` for the machine (a section per slot; the worst slot sets the exit
code) and one `miner-health.last.json` per slot (slot k's in `gpu/<k>/`). The clone's gates and the schedule
are checked once; each slot adds H6, whether its engine runs on its own card.

## Leaving the network
```bash
bash exit-miner.sh               # reads the miner and asks the chain; changes nothing
bash exit-miner.sh --yes         # drains, then leaves if the chain accepts (delete-miner), stake back
bash exit-miner.sh --yes --wait  # drains, asks the chain again once a minute until it accepts, leaves
bash exit-miner.sh --undrain     # gives the exit up: removes the drain, the miner takes work again
```
(Owner mode: the exit is signed by the owner key; see [Owner mode](#owner-mode-the-stake-on-a-key-that-is-not-on-this-machine).)
It reads the miner's identity from its own volume and its registration and stake from the chain, then
**simulates** the exit and prints the chain's answer. With `--yes` it first sets the **drain**: a file named
`drain` in the miner's key directory (next to `availability-last.json`, in its `miner-keys` volume). While
the file is there, a miner of this kit proves no presence and takes no new work, and keeps filing the
reveals its own audited jobs wait for; its heartbeat says `draining`. A draining miner that restarts (a
reboot, Docker's restart policy, a re-run of `join.sh`) registers nothing while the file is there, even
once the chain no longer lists it. Keep a draining miner running. Once
the chain accepts, the script stops the miner (a running miner registers again by itself), broadcasts
`delete-miner`, waits for its inclusion, checks that the miner is gone and that the remaining stake came
back to its address — what was slashed earlier is not refunded — and removes the drain. Every chain read
and the transaction run in a one-off container of the miner image, so the host needs Docker and nothing
else.

The chain **refuses** the exit while the miner holds an obligation — a fee retained on a job it served, a
seat on an audit committee that has not resolved yet, a job under dispute, or a job it answered that is
not settled yet (`chain/x/jobs/keeper/miner_vitality.go::hasOpenObligation`, called by
`DeleteMiner`). The script then prints the chain's own message and exits 2: a reading to repeat later,
not a failure. **The drain stays set after such a refusal**: the miner proves no presence and takes no
new work, so it earns no reward for either, and where the chain arms the availability slash it is exposed
to it (the script reads those parameters beside the refusal and says whether the slash is armed), until the
exit goes through or `--undrain` removes the drain. A refusal met after the
script stopped a running miner starts it again, draining: stopped, it could not file the reveal a job of
its own under audit may be waiting for. A miner that was already stopped when the script ran stays
stopped (save in owner mode with `--signed`, where the stop was the script's own, made by the run that
prepared the owner's transaction).

A miner without a presence proof is drawn onto no new jury while the chain measures availability
(`avail_epoch_blocks` above 0; `chain/x/jobs/keeper/presence.go::minerPresentAt`). A seat it already
holds keeps the exit refused while the miner stays eligible as a juror (`juror_freshness_blocks` after its
last commit) and until that audit resolves: by its jury, or unwound when its next deadline comes due —
up to `audit_unwind_blocks` + `audit_resolve_timeout` blocks after the job's settlement, a few more for a
deferred draw (`final_season_rank.py::finality_blocks` explains the sum). A fee retained on a drawn job
ends with its audit; on a job the draw skips, at finality. Read the parameters with
`dendrad query jobs params -o json`. A miner of an earlier kit does not know the drain:
the script says so when the heartbeat does not confirm it; stop such a miner instead, once it has filed the
reveals its own audited jobs wait for — a stopped miner files none, and such an audit can then conclude
against it, a slash. First read `bash deploy/testnet-miner/exit-miner.sh --help`, which gives that
procedure, then `docker compose -p dendra-miner stop`.

Starting the miner again — `docker compose up -d` here, or **Start** in the Dendra application — registers
it again and stakes again. The application shows the registration, the stake and the command; it has no
button for it, since the exit is irreversible and may have to wait.

With one identity per card, `--slot <k>` names the identity that leaves; `--yes` without it is refused (an
irreversible step is never taken on slot 0 by default), and a run without either simulates every slot. A
slot k that left is marked retired, so a plain re-run of `join.sh` leaves it stopped.

## Uninstall
```bash
bash ../uninstall.sh          # from this directory: measures, prints the plan, changes nothing
bash ../uninstall.sh --yes    # removes the kit; keeps the miner's keys
```
See [`../README.md`](../README.md#uninstalling) for what it removes, what it keeps (the `miner-keys`
volume among them, unless `--delete-keys`, which needs the verified backup the same run makes) and why it
refuses a validator.

## Keys: what is encrypted, and what to back up
The miner's keys live in the `miner-keys` Docker volume: its Cosmos keyring (`/data/keys/cosmos`, the key
that signs and holds the stake), its encryption key (`<id>.sk`), its VRF key (`<id>.vrf`) and its attestation
key (`<id>.attestkey`). Whether they are **encrypted** is read from the volume itself
(`modea/keyring.py::resolve`): a `keyring-file` keyring is encrypted, a `keyring-test` keyring is in clear.
- **A new installation is encrypted by default.** `deploy/join.sh` creates a random passphrase in
  `~/.config/dendra/miner-secrets/keyring-passphrase` (the directory 0700, the file 0600), outside the volume and
  outside this clone, and writes `DENDRA_SECRETS_DIR` into `.env`. The compose file mounts that directory
  read-only at `/run/dendra-secrets`; the miner hands the passphrase to `dendrad` on its standard input, never
  on a command line and never in a variable of the container. The key files are sealed with the same
  passphrase. `join.sh --plain-keys` keeps a new installation's keys in clear, on purpose. Another
  `DENDRA_SECRETS_DIR` must be a directory **of its own**: the kit sets it 0700, backs it up and removes the
  passphrase from it on `uninstall.sh --delete-keys`, so `join.sh`, `encrypt-keys.sh` and `uninstall.sh` refuse
  `$HOME`, a directory above it, `~/.config/dendra` and any directory that already holds other files
  (`passphrase-dir.sh`, the one check all three source).
- **An existing installation whose keys are in clear** is migrated by one command, and only by it:
  ```bash
  bash encrypt-keys.sh          # reads the keyring, prints what it would do, changes nothing
  bash encrypt-keys.sh --yes    # stops the miner, encrypts the keyring at the SAME address, starts it again
  ```
  It refuses while the key's 24-word recovery phrase has not been written down (see below): encrypted, the
  account depends on the passphrase file, and the phrase is the only way back if that file is lost.
- **The recovery phrase.** The miner keeps the 24 words of a key it creates in the volume (sealed with the
  passphrase when the keyring is encrypted). The Dendra application shows them, asks for three of them, and
  **I wrote it down — remove it from this machine** removes the file: from then on, your paper is its only
  copy. Nothing removes it before the three words match.
- **Back up BOTH**: the volume and the passphrase file. Without the passphrase, an encrypted keyring does not
  open, and the miner stops with that reason — it never creates a new key in its place. `bash ../uninstall.sh`
  backs both up (the passphrase in its own archive, mode 0600) and keeps both unless `--delete-keys`.
- **What this protects, and what it does not.** It protects the volume and every backup of it: a copy of the
  volume alone no longer holds a usable key. It does **not** protect a host that is compromised while the
  miner runs: the passphrase sits on the same machine, by design, so that the miner restarts unattended.
  Keep the backups of the volume and of the passphrase apart: together they are a key in clear.
- **Where the Final Testnet Season pays.** Without a declaration, it pays this machine's own key — which
  anyone who copies this machine's keys can spend. Declare an address whose key is **not** on this machine:
  `deploy/join.sh --payout-address dendra1...` (its checksum is verified before anything is written; the
  miner declares it once registered, and again only when it changes), or the application's **Where rewards
  go**. The application shows a banner until an address is declared.
- **The miner's self-test** lists these three choices — keys in clear, the phrase still on the machine, no
  payout address — as **advice**, with the command that changes each. Advice never changes its exit code
  and never raises the hourly alert. A keyring that does **not open** with its passphrase is not advice: the
  miner cannot sign, and check C3 is ko.
- `DENDRA_MINER_PASSPHRASE` is still read by a miner run **outside** this kit (a variable is readable with
  `docker inspect` and in `/proc/<pid>/environ`, and the miner says so); the compose file never forwards it.
- `delete-miner` (leaving the network, `exit-miner.sh`) and `update-miner` are signed by the key that
  registered the miner. `update-miner` must always carry the operator address: an empty one leaves the
  miner without an operator, and its commits, proofs and subsidy claims are refused until it is set again.

## Owner mode: the stake on a key that is not on this machine
An advanced option, for an operator with a second device that holds a key and runs `dendrad` (from the
release binaries) to sign offline:
```bash
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh --owner dendra1...   # the OWNER key's address
```
- **What the chain does with it.** A miner record carries two addresses
  (`chain/x/jobs/keeper/msg_server_miner.go::CreateMiner`). The **owner** (its creator) signs
  `create-miner`: the miner's identifier is derived from it, the stake is taken from it, it alone updates or
  deletes the miner, and the stake goes back to it on exit and on eviction. The **operator**, named by the
  owner, signs the commits, the availability proofs, the subsidy claims and the key rotations — and it is
  the address the chain pays. In owner mode this machine's key is the operator and the owner key never
  touches this machine. `chain/x/jobs/keeper/owner_operator_split_test.go` fixes both halves.
- **Registering.** The miner never sends `create-miner` in this mode. It asks the faucet for this machine's
  key (it needs an account the chain knows to sign anything) and for the owner (the stake), simulates the
  registration from the owner's address, writes the unsigned transaction in its volume, and prints three
  complete commands — copy it out, sign it on the owner's device (`dendrad tx sign ... --offline`, with the
  owner account's number and sequence read from the chain), broadcast it from this host:
  `docker compose -p dendra-miner logs miner | grep -A16 'OWNER MODE'`. It mines once the chain records this
  machine's key as the operator, and not before; if another operator is recorded, it prints the owner's
  `update-miner` instead (which always carries the operator).
- **Leaving.** `bash exit-miner.sh --yes` sets the drain, stops the miner once the chain would accept,
  simulates the exit from the owner's address,
  writes the owner's unsigned `delete-miner` next to the kit and prints the command that signs it;
  `bash exit-miner.sh --yes --signed delete-miner.signed.json` checks that the file is the owner's signed
  `delete-miner` for this miner, broadcasts it, and checks that the stake came back to the owner.
- **Where the season pays.** When owner and operator differ, the programme accepts a payout declaration
  signed by the OWNER only (`final_season_server.py`), so a copy of this machine's key cannot redirect it.
  Three steps, the middle one on the owner's device:
  ```bash
  docker compose -p dendra-miner exec -T miner python3 final_season_miner.py payout-prepare --address dendra1... > payout.json
  # on the owner's device: the `dendrad tx sign` command payout-prepare prints (offline)
  docker compose -p dendra-miner exec -T miner python3 final_season_miner.py payout-submit < payout.signed.json
  ```
  The signature is bound to the height the programme reported: submit it before the chain has moved past
  that height by more than the programme's replay window, or prepare it again. Without a declaration the
  season pays the operator, this machine's key.
- **What it protects, and what it does not.** It protects the **stake**: a copy of this machine's keys can
  neither withdraw it nor move the miner. It does **not** protect the **income**: payments, the subsidy and
  the season's default payout go to the operator, and a copy of this machine's key can spend them, rotate
  the miner's keys, and get the stake slashed by signing bad commits.
- **One owner address registers one miner**: the identifier is derived from it.
- **It starts a new identity.** A miner this machine already registered keeps working without it; to switch,
  leave with `exit-miner.sh` (the stake comes back to this machine's key) and join again with `--owner` from
  a new `miner-keys` volume. Every re-run of `join.sh` carries `DENDRA_MINER_OWNER` over; removing it from
  `.env` is a deliberate edit. When the setting names another owner than the one the chain records for the
  volume's miner, the miner stops at start and says how to leave first; when the setting is missing but the
  chain records this machine's key as the operator of a miner another key owns, it keeps that identity and
  says so.

## Honest notes
- **Persistent identity**: the miner's keys live in the `miner-keys` volume (and, encrypted, open with the
  passphrase file above). **Back both up** — losing them = starting over with a new identity (and
  re-staking), and the Final Testnet Season pays per identity.
- **Present, or not drawn.** Only miners that proved availability in the current or previous window are
  drawn for work or audit. Availability is measured on chain; what it pays is `avail_payout_bps`, and a
  slash (`avail_slash_bps`, capped by `avail_slash_max`) takes `avail_fail_k` failures within
  `avail_fail_window`, all in the same params. The Final Testnet Season counts the same proof — the
  windows proven on chain — and pays them only on a day of verified work.
- **`DENDRA_MODEL_ID` must match the on-chain registry.** If the operator enforces the model registry
  (`enforce_model_registry`), serving a different model = rejected commits / slash. Keep the provided value.
- **The embedder — keep the provided value** (`DENDRA_EMBED_MODE=backend` + `nomic-embed-text`, via
  Ollama). Your commit anchors an embedding of each answer, publicly readable, never the text. Under the
  optimistic mode the launch genesis arms (`verification_mode` = 1), correctness is decided by an audit
  jury's LLM-as-judge, not by comparing embeddings; the embedding cosine settles only the redundant mode
  (`verification_mode` = 0), where every miner of a committee must use the same embedder.
- **Build**: per-push CI does not build this image; the release workflow builds `Dockerfile.miner` and
  publishes it pinned by digest. A prebuilt image is used only when the clone's `docker/MINER_IMAGE` pins
  one and its platform is that of the Docker engine; otherwise the image is built from the clone. If a local `dendrad` build fails,
  check `./cmd/dendrad` in `chain` (see `docker/README.md`).
- **No inbound port required** on the miner side: it *reaches out* to the RPC/relay/faucet. The **operator** is
  the one exposing those ports.

## The canonical path
This kit is the manual route. The canonical one is `deploy/join.sh` — same result, plus pre-flight
checks, automatic GPU detection and a verified genesis SHA-256:
```bash
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh
```
It is **not** a Docker-free route: `join.sh` starts this very kit and refuses to run without Docker
Compose v2. No Docker-free miner is published in this repository — Docker is a hard prerequisite of
every path described here.
