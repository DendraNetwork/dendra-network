# Running Dendra — node, miner, validator

This is the single entry point for joining a Dendra testnet. Pick your role, run one command.

> **Status: research / testnet.** `$DNDR` is a utility token — no sale, no monetary value, and the
> network is resettable. Nothing here handles real funds.

---

## Which role do I want?

| Role | You provide | You get | Command |
|------|-------------|---------|---------|
| **Validator** | A small **genuinely** always-on server + stake. **No GPU.** ⚠️ Downtime is *slashed*, not merely unproductive — [read the cost first](#-the-role-costs-money-when-your-machine-goes-quiet--read-this-before-you-bond) | Produce blocks, and **contribute to the VRF beacon** — see the note below: this is the role the network is short of | `bash deploy/join.sh --validator` |
| **Miner** | A GPU (CPU works, slowly) | Serve encrypted inference locally and be **paid** per honest verdict — *paid, then held*: read the note below before you budget for it — **and your own full node** | `bash deploy/join.sh` |
| **Miner + Judge** | A GPU tier ≥3 **OR** ≥26 GB system RAM (MoE judge on CPU, no GPU needed) | Miner + a seat on the audit committee | `bash deploy/join.sh --judge` |
| **Node only** | A machine that stays online | A synced full node / RPC / network peer | see [`testnet-node/`](testnet-node/) |
| **Operator** | A server + public IP | Host the whole network for others to join | see [`launch/`](launch/) |

> ### ⚠️ Read this before you pick a role — what your pay does today
>
> The protocol **does** pay for work served, from block 1. It then **retains all of it**: `hold_bps`
> reads `10000` on this chain, so **100 % of a miner's net share is held at settlement**, dated and
> queryable (`dendrad query jobs held-summary -o json`), and released at the job's audit checkpoint.
>
> That checkpoint cannot happen yet, and **two** floors stand in the way. They are independent: clearing
> the first does not clear the second, and only the second gates pay.
>
> **The seed floor is a per-block rate, not a switch.** An audit committee is drawn only on a block
> where the randomness beacon carries at least `committee_min_vrf_contributors` contributors, a
> contributor being a **validator** posting a VRF vote-extension — *not* a miner. A validator on a home
> connection contributes only on the blocks its extension reaches the proposer, and **a jailed validator
> contributes nothing at all** (see [the cost of the validator role](#becoming-a-validator-read-this-first)),
> so a floor that was met last week can be unmet today.
>
> **The jury floor is what actually blocks pay.** A drawn committee returns a verdict only when
> `audit_min_quorum` of its jurors vote, and the jury is every registered miner *except* the one under
> audit — so the network needs **`audit_min_quorum` + 1 registered miners** before any audit can
> conclude. Worse, only a node started with `--judge` posts verdicts at all: miners at the floor that
> are all silent still produce zero votes. **Check the live numbers before you decide** rather than
> trusting a count written on this page:
>
> ```bash
> curl -s https://proof.dendranetwork.com/proof                               # jobs, held, audits
> curl -s http://api.dendranetwork.com:1317/dendra/jobs/v1/params             # audit_min_quorum, hold_bps
> curl -s http://api.dendranetwork.com:1317/dendra/jobs/v1/miner              # pagination.total = miners
> curl -s http://api.dendranetwork.com:1317/dendra/jobs/v1/committee_seed_health
> ```
>
> **Both roles can lose stake, for different reasons.** A miner's wrong answer caught at audit costs
> `slash_leak_bps` of its bond (**80 %** on this chain — `dendra/jobs/v1/params`). A validator loses
> `slash_fraction_downtime` of its bond simply for **being offline too long**, and gets jailed with it:
> that one is the trap operators actually fall into, and it has already fired on this network —
> [read it before you bond](#-the-role-costs-money-when-your-machine-goes-quiet--read-this-before-you-bond).
>
> Testnet `$DNDR` has **no monetary value**, is never sold, and a reset erases every balance. There is
> no points programme, no season and no airdrop.

A **miner** serves GPU inference; a **validator** secures consensus. They are independent roles — you can run either or both.

### Every miner runs its own node

`bash deploy/join.sh` starts **two** things: a full node, and the miner that reads it. You do not have
to ask for it and you do not have to configure it — the node syncs first, then the miner is pointed at
`tcp://host.docker.internal:26657`.

> ### ⏱️ Budget for the first sync — it replays the whole chain
>
> State-sync is only used when the operator publishes the three `STATESYNC_*` fields in
> `network-info.txt`. **Check before you start**, because when they are empty your node does **not**
> snapshot — it replays every block from genesis:
>
> ```bash
> curl -s http://api.dendranetwork.com:8088/network-info.txt | grep STATESYNC
> ```
>
> Empty values (`STATESYNC_RPC=` with nothing after it) mean a **full replay**. That is safe — it is
> the honest way to verify the history yourself — but it takes **tens of minutes and gets longer every
> day the chain runs**, so estimate it rather than guessing: divide the network head by the rate your
> own node is catching up at.
>
> ```bash
> curl -s http://api.dendranetwork.com:26657/status | grep -o '"latest_block_height":"[0-9]*"'   # target
> docker compose -f deploy/testnet-node/docker-compose.yml logs --tail 5 node                    # your height
> ```
>
> **`catching_up: true` is not an error.** Leave it running. If a helper script gives up before the
> replay finishes, that is the *script* abandoning, not your node — the node keeps syncing, and you can
> watch it climb. Judge by whether your height is still rising, not by how long it has taken.
>
> **A wall of `ERR ... decentralized VRF seed UNDER-DECENTRALIZED ... LEGACY fallback` during the
> replay is expected** — one line per block, thousands of them. You are re-executing historical blocks
> that were produced below the VRF contributor floor; it says nothing about your node's health.

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

## Prerequisites

- **Docker** with Compose v2 — check with `docker compose version`.
- **The cloned repository.** The first start compiles the chain binary (`dendrad`), so you need the source, not just the script.
- **The network's connection info** — a short `network-info.txt` (RPC, relay, faucet, genesis + its SHA-256) published by the operator. You pass its URL as `CONFIG_URL`.
- **Miners:** an NVIDIA GPU + [`nvidia-container-toolkit`](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) is strongly recommended (CPU inference is very slow), plus ~6 GB of disk for the model.

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
operator's public RPC and run no node of your own, add `--remote-rpc` and `GENESIS_URL` becomes
unnecessary:

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
| `POST pub/…` `res/…` — writing your key and your sealed answer | **a signature**, made with your miner key. `relay_client` already produces it and the kit wires `DENDRA_SIGN_KEY` from your `MINER_ID`. The shared token still works as a fallback for older clients. |
| `POST req/…` | the token — this is the gateway's route, and the gateway does not sign yet. |
| `GET list` `stats` | the token. This is the network-mapping surface, and it is the one thing the token genuinely protects. |

So the canonical command carries no secret:

```bash
CONFIG_URL=<network-info.txt URL> bash deploy/join.sh
```

A signature is **stronger** than a shared token: it names *which* miner wrote, cannot be
passed to a third party, and — unlike a shared secret — stops a token holder from overwriting
someone else's sealed reveal. If you still get `401` on a read, the operator is running a relay
build that predates per-route policy; `DENDRA_RELAY_TOKEN=<token>` stays accepted as a fallback.

### No NVIDIA GPU?

The kit runs on CPU — slow, but correct — and `join.sh` handles it: the GPU reservation lives in a
generated `docker-compose.override.yml` that is **written only when a GPU *and*
`nvidia-container-toolkit` are both detected, and removed otherwise**. That file is a machine artifact
(git-ignored); if you copy a kit between machines, let `join.sh` rewrite it rather than carrying it over —
a stale override fails at `docker compose up` with `could not select device driver`.

### The faucet asks for a proof of work

On a public network the faucet requires a proof of work bound to your address (anti-Sybil). The miner
reads the required difficulty from the faucet and solves it at startup — **nothing to configure**. It
prints its progress and, if the difficulty is out of reach, says so instead of failing mutely
(`DENDRA_FAUCET_POW_MAX_S`, default 300 s).

Follow your miner afterwards with:

```bash
docker compose -f deploy/testnet-miner/docker-compose.yml logs -f miner
```

Seeing **0 jobs** on a quiet network is normal — the miner is idle, waiting for traffic.

---

## Manual path (full control)

The one-command script wraps these kits; use them directly if you prefer to edit the config yourself.

- **Miner:** [`testnet-miner/`](testnet-miner/) — copy `.env.example` → `.env`, fill in the endpoints, `docker compose up -d --build`.
- **Node / validator:** [`testnet-node/`](testnet-node/) — sync from the public genesis + seeds, then bond a validator manually.

Both READMEs cover the details (GPU toggle, validator bond via the SDK 0.50+ `validator.json`, VRF anchoring).

---

## Judging (why `--judge` can be refused)

A judge's verdict can **slash** a miner's stake. An under-powered judge model produces wrong verdicts, and a
wrong verdict penalises an **honest** miner — so the role is gated on measured hardware, not on intent.

`join.sh --judge` calls `deploy/hw_probe.sh` and refuses the committee seat unless **either**:

- your GPU reaches **tier ≥ 3** *and* the model it sizes to is on the judge allow-list (`mistral-nemo`,
  `qwen3:30b-a3b-instruct-2507-q4_K_M`) — override with `DENDRA_JUDGE_ALLOWLIST`; **or**
- you have **≥ 26 GB of system RAM**, in which case the MoE judge runs **on CPU** (`qwen3:30b-a3b…` has ~3B
  *active* parameters, so it is usable off-GPU where a dense model of the same size would not be).

If neither holds, `--judge` is **downgraded to miner-only** with a warning — the node still mines normally.
Check your own machine any time:

```bash
bash deploy/hw_probe.sh          # human-readable: tier, chosen model, can_judge
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
writes rank 1 and therefore genuinely overrides the pin; without it, `join.sh` picks a model from
`hw_probe.sh` at rank 3 and says so. The container start-up resolves the **effective** model and warns
when it differs from the one the kit downloaded — an unresolvable model means a silent, non-voting seat.

> **Two Ollama instances when you mine *and* judge.** The miner uses the GPU instance (`:11434`); the CPU MoE
> judge gets its own (`:11435`, started with `CUDA_VISIBLE_DEVICES=""`). One shared instance serialises the two
> workloads and starves the miner — the launch kit does this for you.

---

## Becoming a validator (read this first)

### ⚠️ The role costs money when your machine goes quiet — read this before you bond

A validator is not a "leave it running and collect" role. **Going offline is a punishable event on this
chain, not merely an unproductive one.** This is standard Cosmos `x/slashing` behaviour, it is armed
here, and it has already fired on this network — so it is stated before the instructions rather than
after the bill.

**Every number below is a chain parameter. Read them yourself rather than trusting this page:**

```bash
curl -s http://api.dendranetwork.com:1317/cosmos/slashing/v1beta1/params
curl -s http://api.dendranetwork.com:1317/cosmos/staking/v1beta1/params      # unbonding_time
```

**What gets you jailed.** The chain keeps a rolling window of the last `signed_blocks_window` blocks
and requires you to have signed at least `min_signed_per_window` of them. Miss more than the
complement, and the jail fires on the very next miss — no warning, no grace period, no notification.
Work it out from the two parameters:

> maximum blocks you may miss in the window = `signed_blocks_window` × (1 − `min_signed_per_window`)

⚠️ **That window is far shorter in wall-clock time than operators expect.** Convert it yourself, and
name the assumption when you do: the chain counts **blocks**, not minutes, and the consensus does not
fix the interval between them — it emerges from `timeout_commit` and propagation. **At the block
interval this chain has been producing recently, the allowance works out to only a few minutes of
continuous downtime.** Measure the interval before relying on any figure:

```bash
# two readings, ~2 min apart: (height2 - height1) blocks between the two block timestamps
curl -s http://api.dendranetwork.com:26657/status | grep -o '"latest_block_[ht][^,]*'
```

A reboot, a package upgrade, a home internet blip, a full disk, or a container restart that takes
slightly too long is enough. **This is the single most common way a testnet validator loses stake.**

**What it costs.**

- **`slash_fraction_downtime` of your bonded stake is burned.** Irreversible: slashed tokens do not
  come back, not on unjail, not ever. It hits your delegators' stake too, not only your own.
- **You leave the active set.** No blocks, no rewards, and — the part that matters on *this* network —
  **your VRF vote-extension stops counting toward `committee_min_vrf_contributors`**. A jailed
  validator contributes exactly nothing to the randomness beacon, which is the floor gating every audit
  draw. The bond stays locked while contributing nothing.
- **Double-signing is a different and much worse event**: `slash_fraction_double_sign` is several times
  the downtime fraction, and it **tombstones** the key — permanent, `unjail` is refused forever. The way
  people cause it is running the same `priv_validator_key.json` in two places at once. **Never start a
  second node with a copy of that key**, not even to "migrate".
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
  --chain-id dendra --node tcp://localhost:26657 --gas-prices 0udndr -y
```

Then confirm you are actually back — a successful transaction is not proof of a bonded validator:

```bash
curl -s http://api.dendranetwork.com:26657/validators          # your address must appear here
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
curl -s http://api.dendranetwork.com:1317/cosmos/staking/v1beta1/validators   # status + jailed per validator
curl -s http://api.dendranetwork.com:1317/cosmos/slashing/v1beta1/signing_infos
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
4. **Anchor your VRF key** — without it your node produces blocks but contributes **nothing** to the randomness beacon, and the beacon is what gates every audit draw. On this network that step is the binding constraint: see the note at the top of this file.

Steps 1-4 are fiddly to assemble by hand (a `validator.json` to author, a script to pipe into the
container, an env var to set before restarting). `bond_validator.sh` does them in one confirmed command
**without removing the deliberation** — it computes an amount that keeps every validator under 2/3,
prints the plan, and changes nothing until you pass `--yes-bond`:

```bash
bash deploy/bond_validator.sh                        # shows the plan, bonds nothing
bash deploy/bond_validator.sh --yes-bond             # bonds + anchors the VRF key + verifies
```

> **Anchor the VRF key, or the bond is half-useless.** A validator that is bonded but whose VRF key is
> not anchored **does not contribute to the randomness beacon**: the committee seed stays as centralised
> as it was, and `committee-seed-health` keeps reporting a low `contributors` count. The script does the
> anchoring and the restart-with-the-key for you, because doing it by hand is exactly the step people skip.

> **Keys.** The kits use the `test` keyring backend: keys are stored **unencrypted** on disk. That is fine for a resettable testnet, **never for real value**. Your miner identity lives in the `miner-keys` Docker volume — back it up; deleting it means re-staking under a new identity.

---

## Wallet

Hosted at [dendranetwork.com/wallet](https://dendranetwork.com/wallet/): create or import an account, check your balance, send `DNDR`. Keys stay in that browser tab and never leave it.

The same page ships here as [`../wallet/web/index.html`](../wallet/web/index.html) — open it locally if you would rather not depend on a host for a page that touches keys. See [`../wallet/README.md`](../wallet/README.md).

---

## Hosting the network (operators)

To run the network others join, see [`launch/`](launch/): a one-command public launch that brings up the chain, relay, faucet, gateway, validators, and audit judges, then publishes the `network-info.txt` joiners consume.

```bash
tr -d '\r' < deploy/launch/launch_env_check.sh | bash -s -- --init   # once: generate ~/.dendra-launch.env
tr -d '\r' < deploy/launch/launch_env_check.sh | bash                # gate: must print GREEN
export SSHPASS='<VPS root password>'
tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
```

### `DENDRA_FRESH=1` — the variable that starts a network from zero

`launch_public.sh` **reuses** an existing chain volume by default. Two situations require the opposite,
and both are governed by one variable that lives nowhere in a config file:

- **First public launch:** without it, the network boots on top of whatever test data the volume holds —
  Season-0 points included.
- **After a consensus-breaking change:** the running history can no longer be replayed by the new binary.
  Guard `3c` compares `docker/CONSENSUS_EPOCH` with the epoch recorded on the host and **refuses to
  deploy** rather than reproducing an `AppHash` panic. It names `DENDRA_FRESH=1` as the way out.

```bash
export DENDRA_FRESH=1     # EXPORT, not an inline prefix: in `VAR=1 tr … | bash`, the assignment
                          # applies to `tr` alone — `bash` never sees it, and the purge silently
                          # does not happen.
tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
```

It **purges the volumes**: the chain restarts from a fresh genesis, balances and points return to their
genesis values, and every joiner must re-sync from the newly published genesis. That is the intent — it
is also irreversible, which is why it is a command-line variable and never a default.

Other operator variables read only from the environment: `SSHPASS` (mandatory, never committed),
`DENDRA_REPO` (repository root when it cannot be inferred), `DENDRA_LAUNCH_ENV` (path of the secrets
file, default `~/.dendra-launch.env`), `DENDRA_GW_MIN_UDNDR` / `DENDRA_GW_TOPUP_UDNDR` (gateway subsidy
thresholds, step `7b`). Everything else belongs in `~/.dendra-launch.env` — see
[`launch/.env.public.example`](launch/.env.public.example).
