# Running Dendra — node, miner, validator

This is the single entry point for joining a Dendra testnet. Pick your role, run one command.

> **Status: research / testnet.** `$DNDR` is a utility token — no sale, no monetary value, and the
> network is resettable. Nothing here handles real funds.

---

## Which role do I want?

| Role | You provide | You get | Command |
|------|-------------|---------|---------|
| **Miner** | A GPU (CPU works, slowly) | Serve encrypted inference locally, earn `DNDR` per honest verdict | `bash deploy/join.sh` |
| **Miner + Judge** | A GPU **+ ≥24 GB system RAM** (hardware-gated, see below) | Miner + a seat on the audit committee | `bash deploy/join.sh --judge` |
| **Node** | A machine that stays online | A synced full node / RPC / network peer | see [`testnet-node/`](testnet-node/) |
| **Validator** | A synced node + stake | Produce blocks, secure the chain, contribute to the VRF beacon | `bash deploy/join.sh --validator` |
| **Operator** | A server + public IP | Host the whole network for others to join | see [`launch/`](launch/) |

A **miner** serves GPU inference; a **validator** secures consensus. They are independent roles — you can run either or both.

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

Instead of `CONFIG_URL`, you can pass endpoints directly:

```bash
DENDRA_NODE=tcp://HOST:26657 DENDRA_RELAY=http://HOST:8645 FAUCET=http://HOST:4500 bash deploy/join.sh
```

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
- you have **≥ 24 GB of system RAM**, in which case the MoE judge runs **on CPU** (`qwen3:30b-a3b…` has ~3B
  *active* parameters, so it is usable off-GPU where a dense model of the same size would not be).

If neither holds, `--judge` is **downgraded to miner-only** with a warning — the node still mines normally.
Check your own machine any time:

```bash
bash deploy/hw_probe.sh          # human-readable: tier, chosen model, can_judge
bash deploy/hw_probe.sh --json   # machine-readable
```

> **Two Ollama instances when you mine *and* judge.** The miner uses the GPU instance (`:11434`); the CPU MoE
> judge gets its own (`:11435`, started with `CUDA_VISIBLE_DEVICES=""`). One shared instance serialises the two
> workloads and starves the miner — the launch kit does this for you.

---

## Becoming a validator (read this first)

Bonding a validator **locks up stake** — it is a deliberate action, so `join.sh --validator` **never bonds silently**. It syncs your node, then walks you through:

1. Create an operator key.
2. Fund the address (faucet, or an operator bootstrap transfer).
3. `create-validator` (SDK 0.50+ uses a `validator.json` file — see [`testnet-node/README.md`](testnet-node/README.md)).
4. **Anchor your VRF key** — required on a rewarded testnet, otherwise you don't contribute to the randomness beacon.

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

Open [`../wallet/web/index.html`](../wallet/web/index.html) in any browser to create or import an account, check your balance, and send `DNDR`. Keys stay in that browser tab. See [`../wallet/README.md`](../wallet/README.md).

---

## Hosting the network (operators)

To run the network others join, see [`launch/`](launch/): a one-command public launch that brings up the chain, relay, faucet, gateway, validators, and audit judges, then publishes the `network-info.txt` joiners consume.
