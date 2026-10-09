# Running Dendra — node, miner, validator

This is the single entry point for joining a Dendra testnet. Pick your role, run one command.

> **Status: public research testnet, `dendra-testnet`.** `$DNDR` is a utility token used inside the
> protocol to pay for inference and reward miners; there is no token sale, presale or ICO. Testnet DNDR
> is never sold. The [Final Testnet Season](https://dendranetwork.com/final-season/) pays rewards in it by
> a published formula, and those rewards count as points: each testnet DNDR the season paid becomes one
> mainnet DNDR, credited in the mainnet genesis to the address that received it; mainnet launches when
> the season ends. The chain is resettable: a reset erases balances; the
> season's published rankings are archived before any reset.

---

## Which role do I want?

| Role | You provide | You get | Command |
|------|-------------|---------|---------|
| **Validator** | A small **genuinely** always-on server + stake. **No GPU.** ⚠️ Downtime is *slashed*, not merely unproductive — [read the cost first](#-the-role-costs-money-when-your-machine-goes-quiet--read-this-before-you-bond) | Produce blocks, and **contribute to the VRF beacon** — see the note below. ⚠️ **Which role the network is short of is a STATE, not a property of the role**: read it from the two counters below (`latest_contributors` against `committee_min_vrf_contributors`, and the registered-miner total — an upper bound, since only present miners are drawn — against `audit_min_quorum` + 1) rather than from this table, which cannot follow a bond | `bash deploy/join.sh --validator` |
| **Miner** | An NVIDIA GPU (no mining model runs on the CPU: a machine without a usable card judges, or has no role, as `deploy/hw_probe.sh --role` decides) | Serve encrypted inference locally and be **paid** per job at its audit checkpoint — *paid, then held*: read the note below before you budget for it — plus [Final Testnet Season](https://dendranetwork.com/final-season/) rewards, **and your own full node** | On a bare Linux host, download and read [`install.sh`](install.sh), then `bash install.sh --yes` (it asks for the judge role by default; the judge's higher disk floor, `MIN_DISK_GB_JUDGE` in `install.sh`, applies only to a host that `deploy/hw_probe.sh --can-judge` says can judge, and a host it says cannot keeps the miner floor; downloaded alone, it decides the role with the probe of the clone it places, before it installs Docker; on a machine with a card, the judge seat is downgraded to miner-only at join time when the RAM does not qualify; `--miner` installs a miner only, on a usable card) and, on a graphical session, adds the **Dendra** desktop application to the menu. Manual path: `bash deploy/join.sh` |
| **Miner + Judge** | At least `MOE_CPU_MIN_RAM_MB` of system RAM (the floor `deploy/hw_probe.sh` applies; `bash deploy/hw_probe.sh --judge-floor-mb` prints it), with or without a GPU (under Windows, the RAM of WSL 2): the network's judge model, a mixture-of-experts, runs on the CPU. A box with a usable card mines on it; a box without one serves the requests the chain assigns it with that same judge model, on its CPU instance — never a mining model | Miner + a seat on the audit committee, and Final Testnet Season juror rewards for each verdict, given when drawn on an audit of a programme request, that is consistent with the job's outcome | `bash deploy/join.sh --judge` |
| **Node only** | A machine that stays online | A synced full node / RPC / network peer | see [`testnet-node/`](testnet-node/) |
| **Operator** | A server + public IP | Host the whole network for others to join | see [`launch/`](launch/) |

> ### ⚠️ Read this before you pick a role — what your pay does
>
> The protocol **does** pay for work served, from block 1, and it **retains that pay first**:
> `hold_bps` is `10000` at genesis (the launcher refuses a lower value), so at settlement **the miner's
> whole net share and the protocol's cut are held**, dated and queryable
> (`dendrad query jobs held-summary -o json`). The audit lottery then draws a share of settled jobs,
> `audit_sample_bps`, for re-audit — read it with `dendrad query jobs params -o json` or
> `/dendra/jobs/v1/params` rather than from any page — and each job ends in exactly one of four ways:
>
> - **not selected** — released at finality, which is the next block's draw: the miner is paid, the
>   protocol's cut goes to the validators (through the fee collector), the team address and the
>   treasury, and its burned share is burned;
> - **selected, verdict upheld** — paid the same way once the jury concludes;
> - **selected, miner convicted** — the miner is slashed and the client refunded;
> - **selected, no jury concludes within `audit_unwind_blocks`** (counted in blocks; about a day at
>   roughly five seconds per block) — the fee **and** the cut return to the client.
>
> A selected job is never paid without a verdict, and no job is held for ever; a job deferred for want
> of a seed unwinds the same way. At finality the work subsidy is credited as a right that the miner claims
> (`MsgClaimSubsidy`); the miner software sends that claim by itself.
>
> Each job's audit checkpoint is gated by **two** independent floors: the seed floor decides whether a
> draw can happen at all, the jury floor whether a selected job can be judged. Clearing the first does
> not clear the second. **Which side of either one this network sits on is a reading, not a property of this
> page** - the commands below return it.
>
> **The seed floor is a per-block rate, not a switch.** The audit lottery and every committee draw
> happen only on a block where the randomness beacon carries at least `committee_min_vrf_contributors`
> contributors, a contributor being a **validator** posting a VRF vote-extension — *not* a miner.
> `dendra-testnet` launched from **a genesis with a single validator, operated by the project**, with that floor set to 1 at
> genesis: at launch that validator holds all the voting power and orders and finalises every block, so at launch
> consensus tolerates no fault (if that validator's host stops, the chain stops). While
> `committee_min_vrf_contributors` is 1, one validator's VRF output carrying at least two thirds of the committing power is enough to seed a draw, so at launch **the
> project's validator alone produces the randomness of every assignment and audit draw**. Read the present set
> rather than this page: `curl -s http://testnet-api.dendranetwork.com:26657/validators` (or
> `dendrad query staking validators`), and the floor with `dendrad query jobs params` → `committee_min_vrf_contributors`.
> The floor rises only when independent validators join and a governance vote raises it.
> A validator contributes only on the blocks its extension reaches the proposer, and **a jailed validator
> contributes nothing at all** (see [the cost of the validator role](#becoming-a-validator-read-this-first)),
> so the floor can be met on one block and missed on the next.
>
> **The jury floor decides whether a selected job can be paid.** A drawn committee returns a verdict
> only when `audit_min_quorum` of its drawn jurors vote, and the jury is drawn from the **present**
> miners — those that proved availability in the current or the previous window — *except* the one under
> audit. Below `audit_min_quorum` + 1 present eligible miners a selected job therefore gets no verdict,
> and its retention returns to the client after `audit_unwind_blocks`. A job still **opens** below that
> floor in optimistic verification mode (`verification_mode` = 1) when `audit_unwind_blocks` > 0 and
> `hold_bps` = 10000 (`pool_freeze.go::enoughEligibleMinersToVerify`); then one present eligible miner
> is enough to serve it: the refund is the client's price for opening early.
> Worse, only a node started with `--judge` posts verdicts at all: miners at the floor that are all
> silent still produce zero votes. **Check the live numbers before you decide** rather than trusting a
> count written on this page:
>
> ```bash
> curl -s https://testnet-proof.dendranetwork.com/proof                               # jobs, held, audits
> curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/params             # audit_min_quorum, hold_bps, audit_sample_bps, audit_unwind_blocks
> curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/miner              # pagination.total = registered miners (an upper bound on the present ones)
> curl -s http://testnet-api.dendranetwork.com:1317/dendra/jobs/v1/committee_seed_health
> ```
>
> **Both roles can lose stake, for different reasons.** A miner's wrong answer caught at audit costs
> `slash_leak_bps` of its bond (`8000`, i.e. **80 %**, at genesis — read it from `/dendra/jobs/v1/params`).
> A validator loses `slash_fraction_downtime` of its bond simply for **being offline too long**, and gets
> jailed with it: that one is the trap operators actually fall into, and it fired on the previous chain,
> whose history this chain does not carry —
> [read it before you bond](#-the-role-costs-money-when-your-machine-goes-quiet--read-this-before-you-bond).
>
> Testnet `$DNDR` is never sold, and a reset erases every balance; the published rankings of the
> **[Final Testnet Season](https://dendranetwork.com/final-season/)** are archived before any reset. The
> season ([ADR-047](../docs/adr/ADR-047-final-testnet-season-reward-programme.md)) pays DNDR weekly, per miner identity and
> with no registration, by a published formula — work per verified programme request, juror rewards per
> verdict consistent with the outcome, and presence per availability window proven on chain, paid only on
> a day with verified work, within the programme's caps of 50 DNDR a day and 1 500 DNDR over the season;
> there is no public-node reward — and anyone can recompute a published day with
> `final_season_rank.py`. Its rewards count as points: each testnet DNDR the season paid becomes one
> mainnet DNDR, credited in the mainnet genesis to the address that received it, and mainnet launches
> when the season ends. Faucet grants, transfers, on-chain job pay and the work
> subsidy are not points.

**Join with a GPU or with a CPU.** A GPU (NVIDIA) mines: the hardware probe picks the open model your card
can serve. A CPU judges, for now: the network's judge model, a mixture-of-experts, runs on the CPU and needs
at least `MOE_CPU_MIN_RAM_MB` of system RAM (`bash deploy/hw_probe.sh --judge-floor-mb` prints it), with or
without a GPU (under Windows, the RAM of WSL 2). A juror is a registered miner, so a machine without a card
also serves the requests the chain assigns it, with that same judge model on its CPU: no mining model runs on
a CPU. Below that floor, a machine without a usable card has no role. A full node or a validator needs no
GPU. More roles and technologies for CPU-only machines are planned.

A **miner** serves inference; a **validator** secures consensus. They are independent roles — you can run either or both.

### Every miner runs its own node

`bash deploy/join.sh` starts **two** things: a full node, and the miner that reads it. You do not have
to ask for it and you do not have to configure it — the node syncs first, then the miner is pointed at
`tcp://dendra-node:26657`: the node's name (its compose project, `DENDRA_PROJECT`) on the `dendra-chain`
Docker network that the node kit owns. The miner joins that network through
`testnet-miner/docker-compose.local-node.yml`, which `join.sh` enables with a `COMPOSE_FILE` line in the
miner kit's `.env` (listing the GPU override too when there is one). The two containers talk directly,
so the node's RPC stays published on `127.0.0.1` only (`DENDRA_RPC_BIND` in `testnet-node/.env`; set it
to `0.0.0.0` only for a node that serves a public RPC). Start the miner from its directory without `-f`
(`cd deploy/testnet-miner && docker compose up -d`): `-f docker-compose.yml` skips that `COMPOSE_FILE`
line and recreates the miner without its network. A miner kit generated by an older `join.sh` still names
`host.docker.internal`, which a loopback-bound RPC cuts off: re-run `join.sh` to rewrite it. Without
`CONFIG_URL` in the shell it reads the one the kit's `.env` names; with none there either, it rewrites the
kit's route to the node in place (`CONFIG_URL=<network-info URL> bash deploy/join.sh` also refreshes the
rest of the kit).

> ### ⏱️ The first sync: state sync for a miner's new node, a replay otherwise
>
> `deploy/join.sh` configures a miner's NEW node to state-sync: it starts from a snapshot when a peer
> offers one, and otherwise stays at height 0 (join.sh names that case). It derives the light-client trust point at
> every join, over TLS, from the servers `docker/STATESYNC_RPC` names in your clone, and requires every
> one of them to serve the same block; it never reads a trust point from `network-info.txt`, which travels
> over plain HTTP. While the snapshot is restored the node's height reads 0, then jumps to the snapshot's
> height: join.sh counts the restore lines of the node's log as progress meanwhile.
>
> A validator's node **replays** every block from the genesis unless `--statesync` is given, and
> `--replay` replays for any role. A replay is also what happens when those servers do not answer (join.sh
> says so), and a node whose peers offer no snapshot stays at height 0 until you remove its volume and
> re-join with `--replay` (join.sh names that case). A replay is safe — it is the honest way to verify the
> history yourself — but its length **grows with every block the chain adds**, so estimate it rather than
> guessing: divide the network head by the rate your own node is catching up at.
>
> ```bash
> curl -s http://testnet-api.dendranetwork.com:26657/status | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['sync_info']['latest_block_height'])"   # target
> docker compose -f deploy/testnet-node/docker-compose.yml logs --tail 5 node                    # your height
> ```
>
> **`catching_up: true` is not an error.** Leave it running. If a helper script gives up before the
> replay finishes, that is the *script* abandoning, not your node — the node keeps syncing, and you can
> watch it climb. Judge by whether your height is still rising, not by how long it has taken.
>
> **`ERR SECURITY: ... VRF seed ... -> LEGACY fallback` lines during the replay describe the blocks you
> are re-executing, not your node.** The chain logs one at every block that carried no randomness beacon
> meeting its floors — at least `committee_min_vrf_contributors` contributors, holding two thirds of the
> commit power — for instance blocks produced before the validator's VRF key was anchored. They say
> nothing about your node's health. Once your node has caught up, the same line at new heights describes
> the live beacon, which `/dendra/jobs/v1/committee_seed_health` reports directly.

This is deliberate. A miner needs to see the chain: which jobs are open, what the committee seed is,
whether its commit landed. A miner reading someone else's RPC has to **trust** what it is told, **goes
blind** the moment that endpoint stops, and can be shown a filtered view. You already keep this machine
running — it is the natural place for a node, and it costs disk and bandwidth, not attention.

If the machine genuinely cannot host one, opt out explicitly:

```bash
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh --remote-rpc
```

That reads the chain from the operator's public RPC, as before. It works — it just makes this miner
depend on one machine it does not control.

---

## Who runs what — read this before you browse the tree

This repository ships **both sides** of the network on purpose: you can read the code that judges you.
The consequence is that half of what you see is not yours to run. None of it is dangerous to you — the
operator scripts act on the operator's own machines — but knowing which half is which saves a wrong turn.

| Path | Who runs it | Why it is here |
|------|-------------|----------------|
| `deploy/join.sh` · `deploy/hw_probe.sh` | **You** | The entry point. Everything else is optional. |
| `deploy/install.sh` · `deploy/install.ps1` | **You**, on a bare machine | Prepare the host (Docker, Compose v2, NVIDIA toolkit, the clone; on Windows WSL 2 first), then run `join.sh`; on a Linux graphical session `install.sh` also adds the Dendra application to the menu. Plan first, nothing without `--yes`. |
| `deploy/app/` | **You**, on a Linux desktop | The **Dendra** desktop application: your miner's health, balance and Final Testnet Season progress, start and stop, and its recovery phrase shown once at first launch. It drives the kit `install.sh` and `join.sh` set up; it installs and joins nothing by itself. Add it to the menu by hand with `bash deploy/app/install_app.sh`. |
| `deploy/testnet-miner/` · `deploy/testnet-node/` | **You** | The stacks `join.sh` starts on your machine. |
| `deploy/cloud/` · `deploy/hiveos/` | **You**, on a rented GPU pod or a HiveOS rig | A pod runs one container: the single-container image and its settings are in [`deploy/cloud/README.md`](cloud/README.md). A HiveOS rig takes the package each release publishes: [`deploy/hiveos/README.md`](hiveos/README.md). |
| `deploy/bond_validator.sh` · `validator_health.sh` · `node_reachability.sh` | **You**, as a validator | Bond deliberately, then read what your node actually contributes. |
| `deploy/testnet/` · `deploy/launch/` | **The network operator** | Launching, resetting and publishing the public network. Here so the launch is auditable — not so you run it. |
| `services/` — miner, judge and reveal workers, `capacity_sign.py` | **You** | Started inside your miner container. |
| `services/` — relay, capacity server, faucet, gateway, Final Testnet Season service | **The network operator** | Runs on the operator's host; you reach it over HTTP. |
| `chain/` | **Everyone** | The consensus binary. Your node builds it from this source. |

### What version am I running?

The root [`VERSION`](../VERSION) file answers it without asking anyone:

- **`kit_version`** — the operator surface in the table above. Behind the network's value is a
  **warning**: your kit still joins, it is simply missing fixes. Update when convenient.
- **`consensus_epoch`** — the state machine. A different value is a **fork**, not a lag: your binary
  would compute a different result for the same block. `join.sh` refuses, and it is right to.
- **`source_commit`** — the exact tree this release was published from.

Two numbers rather than one, because a script fix must not lock you out, and a consensus change must.

---

## Prerequisites

- **Docker** with Compose v2 — check with `docker compose version`.
- **The cloned repository.** The first start compiles the chain binary (`dendrad`) from it, so you need the source, not just the script. `join.sh` pulls a prebuilt miner image only when your clone's `docker/MINER_IMAGE` pins one by digest, and starts it only when its platform is that of your Docker engine; it builds the miner image from the clone otherwise (a `DENDRA_MINER_IMAGE` served by `network-info.txt` is ignored); the full node a miner runs by default is still built from your clone.
- **The network's connection info** — a short `network-info.txt` (RPC, relay, faucet, genesis + its SHA-256) published by the operator. You pass its URL as `CONFIG_URL`.
- **Miners:** an NVIDIA GPU + [`nvidia-container-toolkit`](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), plus ~6 GB of disk for the model. No mining model runs on the CPU: the miner's container checks where its engine holds the served model before it starts (`serve_guard.py`), and refuses a mining model held by the CPU.
- **Judges:** at least `MOE_CPU_MIN_RAM_MB` of system RAM (`bash deploy/hw_probe.sh --judge-floor-mb` prints it), with or without a GPU, since the judge model runs on the CPU; the installers ask for the judge role by default, and `install.sh` applies the judge's higher disk floor (`MIN_DISK_GB_JUDGE`) only to a host that `deploy/hw_probe.sh --can-judge` says can judge (`install.ps1` checks the miner floor on the Windows drive and leaves that decision to `install.sh`, inside the distribution); pass `--miner` (`-Miner` on Windows) to install a miner only. Under Windows the RAM that counts is the WSL 2 VM's, about half the PC's by default; `memory=` in the `[wsl2]` section of `%UserProfile%\.wslconfig` raises it.
- **Bare machine?** Two bootstrap files do the three lines above for you and then run `join.sh`: `deploy/install.sh` (Ubuntu, Debian, HiveOS — Docker, Compose v2, the NVIDIA toolkit, the `docker` group, the clone) and `deploy/install.ps1` (Windows — WSL 2 + Ubuntu, systemd, a logon task that keeps the WSL VM alive, then `install.sh` inside it). Both print their plan and change nothing without `--yes` / `-Yes`; both refuse a host under their disk and memory floors up front. Download, read, run — they are not meant to be piped from a URL.
- **No inbound port for a miner.** It dials out to the relay, the RPC and the faucet; nothing listens. A connection behind carrier-grade NAT qualifies. A validator is different: 26656 is meant to be reachable, and `deploy/node_reachability.sh` measures whether it is.

---

## One command (recommended)

`join.sh` is the canonical path: it runs pre-flight checks, **verifies the genesis SHA-256** (so you can't be pointed at a forged network), enables your GPU automatically, starts the stack, and prints a health summary.

```bash
# Miner (default)
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh

# Miner that also joins the audit committee (hardware-gated — see "Judging" below)
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh --judge

# Validator: sync, then a guided (never silent) stake bond + VRF anchoring
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh --validator
```

Instead of `CONFIG_URL`, you can pass endpoints directly — but **this form also requires
`GENESIS_URL`**, and omitting it fails late rather than early:

```bash
DENDRA_NODE=tcp://HOST:26657 DENDRA_RELAY=http://HOST:8645 FAUCET=http://HOST:4500 \
GENESIS_URL=http://HOST:8088/genesis.json bash deploy/join.sh
```

`GENESIS_URL` is only filled in automatically when you use `CONFIG_URL`. Without it the pre-flight
passes, the genesis check downgrades to a notice, and the run dies much later at
`GENESIS_URL required to run a node` — because a miner starts **its own full node by default**, and
the explicit-endpoint form names no genesis source. If you would rather read the chain from the
operator's public RPC and run no node of your own, add `--remote-rpc`. ⛔ **It does not make
`GENESIS_URL` optional — it makes it the ONLY check there is.** In that mode no node container
starts, so the kit's own genesis enforcement never runs; the pre-flight therefore fails CLOSED
and tells you the two ways out (pass `CONFIG_URL`, or set `DENDRA_ALLOW_UNVERIFIED_GENESIS=1`
for a network you already trust). Supply one of them alongside:

```bash
DENDRA_NODE=tcp://HOST:26657 DENDRA_RELAY=http://HOST:8645 FAUCET=http://HOST:4500 \
bash deploy/join.sh --remote-rpc
```

`CONFIG_URL` remains the recommended form precisely because it cannot be missing a field.

### The relay token (`DENDRA_RELAY_TOKEN`) — **a miner does not need one**

No shared secret is issued to joiners, and `network-info.txt` carries none. A relay that demanded a
token on *every* route would therefore stop a joiner dead: hardware and genesis verified, then `401`
on every relay route, with no self-service way to obtain the secret.

**A miner authenticates by signing, not by sharing a secret.** The relay applies a policy per
route rather than one policy to everything:

| Route | Needs |
|---|---|
| `GET pub/…` `req/…` `res/…` — reading your work | **nothing.** Bodies are sealed to the miner's **on-chain anchored** key, and the relay is assumed hostile by design, so a token in front of ciphertext protects nothing. |
| `POST pub/…` `res/…` — writing your key and your sealed answer | **a signature**, made with your miner key. `relay_client` already produces it and the kit derives it from the identity the miner actually resolved on-chain, at each publication. The shared token still works as a fallback for older clients. |
| `POST req/…` | the token — this is the gateway's route, and the gateway does not sign. |
| `GET list` — the work queue | **nothing.** This is the one route by which a miner learns a job awaits it: gate it and a machine registers, takes a jury seat, and waits forever with no error to show. The "mapping" a secret here would protect is published anonymously by the chain itself — `assigned_committee/<jobId>` and `job` answer with the very `jobId`/`minerId` pairs these keys are made of. It would cost a joiner everything and an attacker nothing. |
| `POST list`, `GET stats` | the token. Neither is on the path to receiving work. |

So the canonical command carries no secret:

```bash
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh
```

A signature is **stronger** than a shared token: it names *which* miner wrote, cannot be
passed to a third party, and — unlike a shared secret — stops a token holder from overwriting
someone else's sealed reveal. If you still get `401` on a read, the operator is running a relay
build that predates per-route policy; `DENDRA_RELAY_TOKEN=<token>` stays accepted as a fallback.

### No NVIDIA GPU?

A machine without one runs no mining model: the testnet runs none on the CPU. With at least
`MOE_CPU_MIN_RAM_MB` of system RAM (`bash deploy/hw_probe.sh --judge-floor-mb` prints it) it **judges**: the
network's judge model, a mixture-of-experts, runs on the CPU (`join.sh` implies `--judge`), and since a juror
is a registered miner, the machine also serves the requests the chain assigns it with that same judge model,
on its CPU instance. Under that floor it has **no role**, and the installers refuse it before they change
anything, saying what is missing (`bash deploy/hw_probe.sh --role` gives the same answer). The judge's disk
floor (`MIN_DISK_GB_JUDGE` in `install.sh`) applies only when `deploy/hw_probe.sh --can-judge` says the host
can judge. `--miner` (`-Miner` on Windows) installs a miner only, on a machine whose card Docker can use, and
is refused without one. More roles and technologies for CPU-only machines are planned.

`join.sh` decides where the engine runs: the GPU reservation lives in a generated
`docker-compose.override.yml` that is **written only when a GPU *and* `nvidia-container-toolkit` are both
detected**; on a machine judging on the CPU it is replaced by the override that puts the miner on the CPU
instance (`ollama-cpu`) with the judge model. The miner's container then checks, before it starts, where its
engine holds the served model (`serve_guard.py`, from `docker/entrypoint-services.sh`): a mining model held
by the CPU is refused, whichever way the container was started. The override is a machine artifact
(git-ignored); if you copy a kit between machines, let `join.sh` rewrite it rather than carrying it over —
a stale override fails at `docker compose up` with `could not select device driver`.

### Several NVIDIA cards?

Without `--gpus` the kit runs one miner identity whatever the number of cards; `bash deploy/join.sh --gpus
all --plan` shows what one identity per card would be (each with its own stake, faucet drip and 24-word
phrase, one shared CPU judge per machine) and writes nothing. Details, and what one more card does and does
not add: [`testnet-miner/README.md`](testnet-miner/README.md#several-gpus-one-identity-per-card---gpus).

### The faucet asks for a proof of work

On a public network the faucet requires a proof of work bound to your address (anti-Sybil). The miner
reads the required difficulty from the faucet and solves it at startup — **nothing to configure**. It
prints its progress and, if the difficulty is out of reach, says so instead of failing mutely
(`DENDRA_FAUCET_POW_MAX_S`, default 300 s).

Follow your miner afterwards with:

```bash
docker compose -p dendra-miner logs -f miner
```

Seeing **0 jobs** on a quiet network is normal — the miner is idle, waiting for traffic.

### Is my miner doing its job?

```bash
bash deploy/testnet-miner/miner_health.sh     # exit 0 every check ok, 1 a problem, 2 not everything measured
```

It checks, on this host, the kit's containers, the image this clone pins and the versions the network
publishes; then, inside the miner container, the node and its sync, the registration and VRF key, the
presence the chain counts, the relay's work queue and a signed write, the model, the capacity line and
the processes. A check it cannot read is **not measured**, never "ok". `join.sh` runs it at the end of the
join and schedules it hourly: `deploy/testnet-miner/miner-health.ALERT` exists while a check fails (an
alert) or could not be measured (a note), and the Dendra application shows the last run with its age. Details:
[`testnet-miner/README.md`](testnet-miner/README.md#health-is-this-miner-doing-its-job).

### Your miner's keys, and where the season pays

A new miner's keyring is **encrypted** by default: `join.sh` creates a random passphrase in
`~/.config/dendra/miner-secrets/keyring-passphrase` (outside the volume and outside the clone), the kit mounts
it read-only into the miner, and the miner hands it to `dendrad` on its standard input. `--plain-keys` keeps a
new miner's keys in clear, on purpose. A miner whose keys are already in clear is encrypted at the same
address by `bash deploy/testnet-miner/encrypt-keys.sh` (a plan without `--yes`; it refuses until the key's
24-word recovery phrase is written down). **Back up both** the `miner-keys` volume and the passphrase file:
an encrypted keyring does not open without its passphrase, and the miner then stops with that reason instead
of making a new key. This protects the volume and its backups, **not** a host compromised while the miner
runs — the passphrase is on the same machine so that the miner restarts unattended.

`--payout-address dendra1...` names where the Final Testnet Season pays this miner: its checksum is verified
before anything changes, and the miner declares it once registered. Without it the season pays this machine's
own key, and the Dendra application says so until an address is declared. Details:
[`testnet-miner/README.md`](testnet-miner/README.md#keys-what-is-encrypted-and-what-to-back-up).

`--owner dendra1...` (advanced) keeps the **stake** off this machine: the named key — on a second device that
runs `dendrad` from the release binaries — registers the miner and holds its stake, and this machine's key
only operates it. The miner never sends `create-miner` then: it prints the registration its owner signs
offline, and mines once the chain records this machine's key as the operator; `delete-miner` and
`update-miner` are signed by the owner too. It protects the stake, **not** the income: payments, the subsidy
and the season's default payout go to the operator, this machine's key. One owner address registers one
miner. Details: [`testnet-miner/README.md`](testnet-miner/README.md#owner-mode-the-stake-on-a-key-that-is-not-on-this-machine).

### Leaving the network

```bash
bash deploy/testnet-miner/exit-miner.sh          # reads the miner and asks the chain; changes nothing
bash deploy/testnet-miner/exit-miner.sh --yes    # stops the miner, deregisters it, gets the stake back
```

It reads your miner's identity and stake, then **simulates** `delete-miner` and prints what the chain
answers. With `--yes` it stops the miner first (a running miner registers again by itself), broadcasts
the transaction, waits for it to be included, and checks that the miner is gone and that the remaining
stake came back to its address (what was slashed earlier is not refunded). The chain **refuses** while the
miner holds an obligation — a fee retained on a job it served, a seat on an audit committee that has not
resolved yet (a miner stays eligible for one during `juror_freshness_blocks` after its last commit), or a
job under dispute: the script prints the chain's own message and exits 2. That is a reading to repeat
later, not a failure. Starting the miner again afterwards registers it again, and stakes again. The
Dendra application shows the registration, the stake and this command; it has no button for it. In owner
mode the exit is the owner's to sign: `--yes` prepares it and prints the sign command, and
`--yes --signed <file>` sends the signed file and checks that the stake came back to the owner.

### Uninstalling

```bash
bash deploy/uninstall.sh                  # measures what the kit set up here, prints the plan
bash deploy/uninstall.sh --yes            # removes it; keeps the miner's keys
```

It removes the containers and images of this clone's kits (`docker compose -p <project> down --rmi all`,
never `-v`), the model volumes, the node's chain data when the node is measured as **not** a validator,
this clone's crontab lines (every other line is kept), the miner self-test's files and the application. Before anything, it backs up
the miner's and the node's keys into `$HOME/dendra-backup-YYYYMMDDTHHMMSSZ`, named after the UTC time of the run (readable by you only, outside the clone)
and verifies the archives — the keyring passphrase included, in its own archive. It **keeps** the miner's
keys volume and the passphrase file (the address holds the stake and the season payments; `--delete-keys`
removes both, only after that verified backup -- the passphrase FILE, then its directory only if nothing else
is in it, and it refuses a passphrase kept in `$HOME` or in a directory that holds other files), the clone, and what was
installed for the whole system (Docker, the NVIDIA toolkit, the apt repositories, the `docker` group) — it
prints the command for each. It refuses a bonded validator (leave the validator set first), and a running
node whose voting power it cannot read; `--keep-node` removes the miner side only. On Windows,
`install.ps1 -Uninstall` runs it inside the distribution and then removes the logon task; it never
unregisters the distribution, which would delete its volumes, keys included.

---

## Manual path (full control)

The one-command script wraps these kits; use them directly if you prefer to edit the config yourself.

- **Miner:** [`testnet-miner/`](testnet-miner/) — copy `.env.example` → `.env`, fill in the endpoints, `docker compose up -d --build` from that directory. With your own node on the same machine, the two lines `.env.example` describes (`DENDRA_NODE` by the node's name, and `COMPOSE_FILE`) attach the miner to it.
- **Node / validator:** [`testnet-node/`](testnet-node/) — sync from the public genesis + seeds, then bond a validator manually.

Both READMEs cover the details (GPU toggle, validator bond via the SDK 0.50+ `validator.json`, VRF anchoring).

---

## Judging (why `--judge` can be refused)

A judge's verdict can **slash** a miner's stake. An under-powered judge model produces wrong verdicts, and a
wrong verdict penalises an **honest** miner — so the role is gated on measured hardware, not on intent.

**Judging runs on the CPU, for now.** The network's judge model, the mixture-of-experts
`qwen3:30b-a3b-instruct-2507-q4_K_M`, has ~3B *active* parameters, so it runs at usable speed on the CPU
where a dense model of the same size would not. No judge is seated on the GPU: the kit serves every
verdict from its CPU instance, with the model the chain names, unless you override it (see
[Which judge model actually runs](#which-judge-model-actually-runs)). `join.sh --judge` calls
`deploy/hw_probe.sh` and refuses the committee seat unless **both** hold:

- the machine has **at least `MOE_CPU_MIN_RAM_MB` of system RAM** (the probe's floor, printed by
  `bash deploy/hw_probe.sh --judge-floor-mb`; the environment may raise it, never lower it past a hard
  floor), **with or without a GPU**. Under Windows
  this is the RAM of the WSL 2 VM, about half the PC's by default; `memory=` in the `[wsl2]` section of
  `%UserProfile%\.wslconfig` raises it;
- that model is on the judge allow-list. `DENDRA_JUDGE_ALLOWLIST` does **not** widen it: it is
  INTERSECTED with the project's list, so it can only forbid a validated model on your own box, never
  seat one the project has not validated.

A juror is a **registered miner**: the chain draws jurors among the registered, present miners, weighted
by stake, and draws work among the same miners, so there is no judge-only mode. A machine with a GPU and
that much RAM mines on the GPU and judges on the CPU at the same time; a machine without a GPU judges on
the CPU and also serves the requests the chain assigns it with that same judge model. `deploy/install.sh` also
applies a higher disk floor to the judge role (`MIN_DISK_GB_JUDGE`), since it pulls the judge model — and
only on a host that `deploy/hw_probe.sh --can-judge` says can judge; `deploy/install.ps1` leaves that
decision to it, inside the distribution.

If either probe condition (the RAM, a validated model) fails at join time on a machine with a usable card,
`--judge` is **downgraded to miner-only** with a warning — the node still mines on the card. Without a usable
card there is no miner role to fall back to: the machine is refused. Check your own machine any time:

```bash
bash deploy/hw_probe.sh          # human-readable: tier, chosen model, role (mine, or mine + judge)
bash deploy/hw_probe.sh --json   # machine-readable
```

### Which judge model actually runs

Four settings can name a judge model, and only one of them wins. `judge_worker.py::resolve_judge_model`
applies this precedence, highest first:

| Rank | Source | Set by |
|------|--------|--------|
| 1 | `--model-id` | `DENDRA_JUDGE_MODEL_OVERRIDE` in the kit `.env` |
| 2 | **on-chain pin** `modelregistry.audit_judge_model` | the network, for the whole committee |
| 3 | `DENDRA_JUDGE_MODEL_ID` | the kit `.env` — fallback when the chain pins nothing |
| 4 | built-in default (`qwen3:30b-a3b-instruct-2507-q4_K_M`) | the code |

So a value set at rank 3 **decides nothing** as soon as the network pins a model — it only declares, in
your on-chain verdicts, which model you claim to run (that declaration is what makes committee-model
diversity measurable, so keep it truthful). `DENDRA_JUDGE_MODEL=<model> bash deploy/join.sh --judge`
writes rank 1 and therefore genuinely overrides the pin — only with a model on the judge allow-list
(`bash deploy/hw_probe.sh --judge-allowed <model>` answers `true` or `false`; `join.sh` refuses any other,
before anything is written); without it, `join.sh` picks a model from `hw_probe.sh` at rank 3 and says so. The container start-up resolves the **effective** model and warns
when it differs from the one the kit downloaded — an unresolvable model means a silent, non-voting seat.

> **Two Ollama instances when you mine *and* judge.** The miner uses the kit's main instance (`ollama`, on
> the GPU when there is one); the judge gets its own CPU-only instance (`ollama-cpu`, started with
> `CUDA_VISIBLE_DEVICES=""`), which `join.sh --judge` starts through the `judge` profile. Without a GPU,
> the CPU instance holds the judge model alone and the miner answers its requests there, so no second model
> competes for the CPU; `ollama` starts and stays empty. With a GPU, one shared instance would serialise the
> two workloads and starve the miner.

---

## Becoming a validator (read this first)

### ⚠️ The role costs money when your machine goes quiet — read this before you bond

A validator is not a "leave it running and collect" role. **Going offline is a punishable event on this
chain, not merely an unproductive one.** This is standard Cosmos `x/slashing` behaviour, it is armed
here, and it fired on the previous chain, whose history this chain does not carry — so it is stated
before the instructions rather than after the bill.

**Every number below is a chain parameter. Read them yourself rather than trusting this page:**

```bash
curl -s http://testnet-api.dendranetwork.com:1317/cosmos/slashing/v1beta1/params
curl -s http://testnet-api.dendranetwork.com:1317/cosmos/staking/v1beta1/params      # unbonding_time
```

**What gets you jailed.** The chain keeps a rolling window of the last `signed_blocks_window` blocks
and requires you to have signed at least `min_signed_per_window` of them. Miss more than the
complement, and the jail fires on the very next miss — no warning, no grace period, no notification.
Work it out from the two parameters:

> maximum blocks you may miss in the window = `signed_blocks_window` × (1 − `min_signed_per_window`)

⚠️ **That allowance is a count of blocks, and it buys far less real time than operators assume.** Both
parameters are governable, so any figure copied onto a page like this one stops being true the moment a
proposal changes them — derive it, never recite it. And the chain counts **blocks**, not minutes: the
consensus does not fix the interval between them, it emerges from `timeout_commit` and propagation. If
you need wall-clock time, measure the interval yourself and name that measurement in the same sentence
as whatever duration you derive from it:

```bash
# two readings, ~2 min apart: (height2 - height1) blocks between the two block timestamps
curl -s http://testnet-api.dendranetwork.com:26657/status | python3 -c "import json,sys; s=json.load(sys.stdin)['result']['sync_info']; print(s['latest_block_height'], s['latest_block_time'])"
```

`deploy/validator_health.sh` already does this arithmetic against the live chain — it reads the window
and the ratio, derives the allowance, and tells you how much of it your node has already spent. Trust
it over any number written on this page (described under *How to know before it costs you*, below).

A reboot, a package upgrade, a home internet blip, a full disk, or a container restart that drags all
spend that same allowance, and none of them announces itself as dangerous. Nothing warns you while the
counter climbs, and nothing tells you when it tips: the jail *is* the notification. **This is the
single most common way a testnet validator loses stake.**

**What it costs.**

- **`slash_fraction_downtime` of your bonded stake is burned.** Irreversible: slashed tokens do not
  come back, not on unjail, not ever. It hits your delegators' stake too, not only your own.
- **You leave the active set.** No blocks, no rewards, and — the part that matters on *this* network —
  **your VRF vote-extension stops counting toward `committee_min_vrf_contributors`**. A jailed
  validator contributes exactly nothing to the randomness beacon, which is the floor gating every
  assignment and audit draw. The bond stays locked while contributing nothing.
- **Double-signing is a different and much worse event**: `slash_fraction_double_sign` is the heavier of
  the two fractions — compare them in the same params output above — and it **tombstones** the key:
  permanent, `unjail` is refused forever. The way people cause it is running the same
  `priv_validator_key.json` in two places at once. **Never start a second node with a copy of that
  key**, not even to "migrate".
- **Unbonding takes `unbonding_time`** (read it from the staking params above — it is measured in
  *weeks*, not minutes). Your stake is neither productive nor withdrawable during it.

**How to get out of jail.** It is **not automatic**. After `downtime_jail_duration` has elapsed you must
send the transaction yourself; before that it is rejected:

```bash
# inside your node container, with your operator key.
# --keyring-backend test is NOT optional: the node's client.toml defaults to the `os` backend, while
# the kit creates the validator key in the `test` one. Without the flag this returns "key not found",
# which reads like a lost key and is only a wrong backend.
# Point --node at a node that is CAUGHT UP: a lagging one simulates against older state and can refuse
# an unjail whose jail period has in fact expired.
dendrad tx slashing unjail --from <your-key> --keyring-backend test \
  --chain-id "$(sed -n 's/^CHAIN_ID=//p' deploy/testnet-node/.env)" --node tcp://localhost:26657 --gas-prices 0udndr -y
```

Then confirm you are actually back — a successful transaction is not proof of a bonded validator:

```bash
curl -s http://testnet-api.dendranetwork.com:26657/validators          # your address must appear here
```

**How to know before it costs you.** `deploy/validator_health.sh` is the read-only answer to "am I
jailed, was I slashed, and how much margin is left before the next jail". It never signs and never
broadcasts anything:

```bash
bash deploy/validator_health.sh          # jail state, slash detection, margin before the next jail
```

It derives the slash rather than guessing it — `tokens` and `delegator_shares` diverge only downwards
and only through a slash, so the gap between them *is* what the slash cost. You can read the same facts
by hand, and a jailed validator is visible from outside:

```bash
curl -s http://testnet-api.dendranetwork.com:1317/cosmos/staking/v1beta1/validators   # status + jailed per validator
curl -s http://testnet-api.dendranetwork.com:1317/cosmos/slashing/v1beta1/signing_infos
```

In `signing_infos`, `jailed_until` in the future means you are still serving the jail period;
`jailed_until` in the **past** while you are still absent from `/validators` means the period is over
and **nobody has sent the unjail transaction** — the chain will not do it for you. `tombstoned: true`
means the key is finished.

> **Read the zero correctly.** protobuf omits a field at its zero value, so `jailed` **absent is not
> `jailed: false`** — and a query that failed returns neither. Never conclude "not jailed" from a
> missing field or an empty response; the reassuring answer is exactly the one that must not be guessed.

Bonding a validator **locks up stake** — it is a deliberate action, so `join.sh --validator` **never bonds silently**. It syncs your node, then walks you through:

1. Create an operator key.
2. Fund the address (faucet, or an operator bootstrap transfer).
3. `create-validator` (SDK 0.50+ uses a `validator.json` file — see [`testnet-node/README.md`](testnet-node/README.md)).
4. **Anchor your VRF key** — without an anchored key your validator produces blocks but adds **nothing** to the randomness beacon, which gates every assignment and audit draw. The beacon's floor is `committee_min_vrf_contributors`, read with `dendrad query jobs params -o json`. `dendra-testnet` launched from a genesis with a single validator, operated by the project; while that floor is 1, one validator's VRF output carrying at least two thirds of the committing power is enough to seed a draw, so at launch the project's validator alone produces the randomness of every draw, and only a governance vote raises that floor.

Steps 1-4 are fiddly to assemble by hand (a `validator.json` to author, a script to pipe into the
container, an env var to set before restarting). `bond_validator.sh` does them in one confirmed command
**without removing the deliberation** — it computes an amount that keeps every validator under 2/3,
prints the plan, and changes nothing until you pass `--yes-bond`:

```bash
bash deploy/bond_validator.sh                        # shows the plan, bonds nothing
bash deploy/bond_validator.sh --yes-bond             # bonds + anchors the VRF key + verifies
```

It reads the node's `earliest_block_height` and the genesis `initial_height` first: a node that holds no
block below its snapshot (a miner's node configured to state-sync, or a validator's joined with
`--statesync`) is refused unless `--accept-statesynced-node` is given, as is a node whose history cannot
be read. A validator's node joined by `join.sh --validator` replays from the genesis by default.

> **Anchor the VRF key, or the bond is half-useless.** A validator that is bonded but whose VRF key is
> not anchored **does not contribute to the randomness beacon**: the committee seed stays as centralised
> as it was, and `committee-seed-health` keeps reporting a low `contributors` count. The script does the
> anchoring and the restart-with-the-key for you, because doing it by hand is exactly the step people skip.

> **Keys.** The node kit and the validator commands above use the `test` keyring backend: those keys are stored **unencrypted** on disk. That is fine for a resettable testnet, **never for real value**. A new miner's keyring is encrypted by default (see [Your miner's keys](#your-miners-keys-and-where-the-season-pays)); a miner whose keys are still in clear is encrypted by `deploy/testnet-miner/encrypt-keys.sh`. Your miner identity lives in the `miner-keys` Docker volume, and an encrypted one opens only with its passphrase file — back up both (the Dendra application shows the key's recovery phrase until three of its words are typed back, then removes it from the machine); losing them means re-staking under a new identity, and the Final Testnet Season counts rewards per identity.

---

## Wallet

Hosted at [dendranetwork.com/wallet](https://dendranetwork.com/wallet/): create or import an account, check your balance, send `DNDR`. Keys stay in that browser tab and never leave it.

The same page ships here as [`../wallet/web/index.html`](../wallet/web/index.html) — open it locally if you would rather not depend on a host for a page that touches keys. See [`../wallet/README.md`](../wallet/README.md).

---

## Hosting the network (operators)

To run the network others join, see [`launch/`](launch/): a one-command public launch that brings up the chain from a genesis with a single validator, the relay, faucet and gateway, the proof page and the Final Testnet Season service, publishes the `network-info.txt` joiners consume, then prints the follow-up steps (audit judges among them).

```bash
tr -d '\r' < deploy/launch/launch_env_check.sh | bash -s -- --init   # once: generate ~/.dendra-launch.env
tr -d '\r' < deploy/launch/launch_env_check.sh | bash                # gate: must print GREEN
# SSH key by default; to log in by password instead (it then replaces the key): export SSHPASS='<VPS root password>'
tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
```

### `DENDRA_FRESH=1` — the variable that starts a network from zero

`launch_public.sh` **reuses** an existing chain volume by default. Two situations require the opposite,
and both are governed by one variable that lives nowhere in a config file:

- **First public launch:** without it, the network boots on top of whatever test data the volume holds —
  test balances, registrations and jobs included.
- **After a consensus-breaking change:** the running history can no longer be replayed by the new binary.
  Guard `3c` compares `docker/CONSENSUS_EPOCH` with the epoch recorded on the host and **refuses to
  deploy** rather than reproducing an `AppHash` panic. It names `DENDRA_FRESH=1` as the way out.

```bash
export DENDRA_FRESH=1     # EXPORT, not an inline prefix: in `VAR=1 tr … | bash`, the assignment
                          # applies to `tr` alone — `bash` never sees it, and the purge silently
                          # does not happen.
tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
```

It **purges the volumes**: the chain restarts from a fresh genesis, balances return to their genesis
values, and every joiner must re-sync from the newly published genesis. That is the intent — it is also
irreversible, which is why it is a command-line variable and never a default. The purge runs
`docker compose --profile public … down -v`, which also removes the Final Testnet Season service's
volume (`final-season-data`: the programme's draw key `secret.bin`, the evidence log and the rankings it
serves) and the generator's volume (`final-season-generator`: its count of the day's requests, keyed by
the season's first block and the day). A reset erases balances; the season's published rankings are
archived before any reset: before the purge, step `3b` stops the season's services and copies `final-season-data` off the
host to your machine, under
`DENDRA_FINAL_SEASON_ARCHIVE_DIR` (default `~/dendra-final-season-archives`), mode 600 since it holds the draw key. It
purges nothing unless the copy is a readable archive holding at least one file, and it refuses as well
when it cannot tell whether the volume exists. The new volume starts empty: serving the archived
rankings again after the reset is a step of yours, not of the launcher.

Other operator variables read only from the environment: `SSHPASS` (only when the host does not accept
your SSH key; never committed), `DENDRA_REPO` (repository root when it cannot be inferred),
`DENDRA_LAUNCH_ENV` (path of the secrets file, default `~/.dendra-launch.env`), `DENDRA_GW_MIN_UDNDR` /
`DENDRA_GW_TOPUP_UDNDR` (gateway subsidy thresholds, step `7b`). Everything else belongs in `~/.dendra-launch.env` — see
[`launch/.env.public.example`](launch/.env.public.example).
