# Join the Dendra testnet — all-in-one miner, GPU or CPU

> **Simplest: the desktop application "Dendra"** (`deploy/app`, Linux first), **set up by `deploy/install.sh`** (Ubuntu, Debian, HiveOS; `deploy/install.ps1` on Windows, WSL 2): download the installer, read it, then run it with `--yes` (`-Yes` for `install.ps1`) — nothing happens without that flag, and it is never piped from `curl` into a shell. It prepares the host, runs `deploy/join.sh` itself and, on a graphical Linux session, adds the application to your menu.
> Docker in place? `CONFIG_URL=<network-info.txt> bash deploy/join.sh` (one command, sane defaults, self-diagnostics). This kit remains the **advanced/manual** path.

Run your machine as a **miner** on the Dendra public research testnet, `dendra-testnet`: you receive encrypted
inference jobs, serve them **on your machine**, and are **paid** `DNDR` per job once it clears its audit checkpoint.
The [Final Testnet Season](#final-testnet-season) adds rewards on top, computed by its published formula.
No need to launch a chain —
this package only runs **your local engine (Ollama) + the miner client**, connected to the operator's
**public** endpoints.

**Join with a GPU or with a CPU.**
- **GPU (NVIDIA): mine** — the hardware probe picks the open model your card can serve.
- **CPU: judge, for now** — the network's judge model, a mixture-of-experts, runs on the CPU. It needs at
  least 26 000 MB of system RAM, with or without a GPU (under Windows, the RAM of WSL 2). A juror is a
  registered miner, so the machine also serves requests with a small model on its CPU. See
  [Judging on the CPU](#judging-on-the-cpu).
- **Full node or validator:** no GPU.

Under 26 000 MB of RAM, a machine without a GPU still mines on its CPU, slowly, with a small model. The
installers ask for the judge role by default and refuse it under 32 GB of free disk; pass `--miner`
(`-Miner` on Windows) to install a miner only. More roles and technologies for CPU-only machines are
planned.

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
> serve, and, as a juror, read the prompts and answers you judge. No slash punishes a leak.

## Prerequisites
- **Docker** + Docker Compose v2 (`docker compose version`).
- **NVIDIA GPU recommended** + `nvidia-container-toolkit` (otherwise it runs on CPU, slow). See [NVIDIA GPU](#nvidia-gpu).
- **~6 GB of disk** for the model.
- **To judge (optional):** at least 26 000 MB of system RAM, with or without a GPU, and room for the judge
  model's weights: the installers ask for the judge role by default and refuse it under 32 GB of free disk; pass
  `--miner` (`-Miner` on Windows) to install a miner only. See [Judging on the CPU](#judging-on-the-cpu).
- The testnet's **public endpoints** (RPC / relay / faucet / `DENDRA_FINAL_SEASON_URL`) — from the operator's `network-info.txt`.
- The **cloned repository** (the first build compiles the chain binary `dendrad`). `deploy/join.sh` pulls a
  prebuilt miner image only when the clone's `docker/MINER_IMAGE` pins one by digest, and builds the image
  from the clone otherwise; by hand, copy that file's `DENDRA_MINER_IMAGE=` line into `.env` and run
  `docker compose pull miner && docker compose up -d --no-build`.

## 3 steps
```bash
cd deploy/testnet-miner
cp .env.example .env            # 1) copy the config
nano .env                       # 2) fill in DENDRA_NODE / DENDRA_RELAY / FAUCET + a unique MINER_ID, and add DENDRA_FINAL_SEASON_URL (see Final Testnet Season)
docker compose up -d --build    # 3) start (first build is long: compiles dendrad + pulls the model)
bash publish-capacity.sh        # 4) declare this machine to the network registry
```
Follow: `docker compose logs -f miner`. You should see the faucet self-funding (it solves the faucet's
proof of work first, on a public network), the on-chain registration, then the job loop.

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

## Final Testnet Season
The Final Testnet Season (ADR-047) ends on 7 November 2026 at 23:59 UTC: it counts every block
timestamped before 8 November 2026, 00:00 UTC — the chain's own block times decide. Season days are
17 280 blocks each; the last one is cut at the end. It is a reward programme on top of job pay: no
registration and no test, paid weekly in DNDR per miner identity and per programme day, by a published
formula applied to what the chain records and to the programme's published evidence — the jobs whose
answer it received, and the grades
(`final_season_calc.py::gross_of`, rates in `final_season_rules.py::RULES`):
- **work** — 0.05 DNDR per verified programme request (settled, past its audit, not refunded), the same
  rate for every identity;
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
  Without a declaration, they go to the operator that signed the identity's availability proofs that day
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
seated on the GPU. It needs at least 26 000 MB of system RAM, with or without a GPU; under Windows, the RAM
that counts is the WSL 2 VM's, about half the PC's by default, and `memory=` in the `[wsl2]` section of
`%UserProfile%\.wslconfig` raises it. Check the machine with `bash deploy/hw_probe.sh` from the repository
root. A juror is a registered miner: the chain draws jurors and work among the same registered, present
miners, so a machine that judges also serves its share of requests — on its CPU, with a small model,
when it has no GPU.

By hand: uncomment the audit-committee lines of `.env` (`DENDRA_MINER_JUDGE=1`,
`DENDRA_JUDGE_ENDPOINT=http://ollama-cpu:11434`), then `docker compose --profile judge up -d`. The canonical
path does it for you: `CONFIG_URL=<network-info.txt URL> bash deploy/join.sh --judge`.

## NVIDIA GPU
Uncomment the `deploy:` block of the `ollama` service in `docker-compose.yml` (requires
`nvidia-container-toolkit` installed on the host), then `docker compose up -d`. Without it, Ollama runs on **CPU**.

The kit ships **without** that reservation on purpose: a GPU reservation on a host that has no NVIDIA
runtime makes `docker compose up` fail on `could not select device driver`, which names nothing. The
canonical path (`deploy/join.sh`) writes a `docker-compose.override.yml` when it detects a GPU *and* the
toolkit, and removes it otherwise — that file is a machine artifact and is git-ignored, so never copy it
between machines.

## Honest notes
- **Persistent identity**: the miner's key lives in the `miner-keys` volume. **Back it up** — losing it =
  starting over with a new identity (and re-staking), and the Final Testnet Season pays per identity.
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
  one; otherwise the image is built from the clone. If a local `dendrad` build fails,
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
