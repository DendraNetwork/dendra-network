# Mining on a rented GPU pod (RunPod, Vast)

A rented pod runs **one container** and gives you no Docker of your own, so the miner kit's compose
file (`deploy/testnet-miner`, three services) cannot run there. This folder describes the image made for
that case: **`dendra-miner-cloud`**, Ollama and the miner in one container, started by
[`docker/cloud-start.sh`](../../docker/cloud-start.sh) and built by
[`docker/Dockerfile.cloud`](../../docker/Dockerfile.cloud).

What the pod runs, and what it does not:

- **a miner only.** No judge: judging needs the RAM `deploy/hw_probe.sh` asks for (`MOE_CPU_MIN_RAM_MB`)
  and a second Ollama instance on the CPU, on a machine billed by the hour. Judging is for a machine you
  keep running (`deploy/install.sh`, `deploy/join.sh --judge`). Like any miner started without `--judge`,
  the pod can still be drawn into a jury — the chain draws from the miners it counts as alive, and has no
  judge capability of its own to tell them apart — and it does not vote there.
- **no node of its own.** It reads the chain from the network's public RPC, exactly as
  `deploy/join.sh --remote-rpc` does, and checks the network the same way (below).
- **no inbound port.** The miner only dials out — to the relay, the RPC and the faucet. Expose nothing.
- **linux/amd64 only**, like the miner image it is built on: an NVIDIA pod.

## The image

Use the value that [`docker/CLOUD_IMAGE`](../../docker/CLOUD_IMAGE) names on the main branch of this
repository — a **digest** (`ghcr.io/dendranetwork/dendra-miner-cloud@sha256:…`), never a tag: a tag can
be moved after the fact, a digest names the bytes a release built. When that file carries no value line,
no cloud image is published for the current tree.

## The settings (environment variables)

| Variable | | What it is |
|---|---|---|
| `DENDRA_PAYOUT_ADDRESS` | **required** | The **public** `dendra1…` address of a key that is **not** on the pod: where the Final Testnet Season pays this miner. Never a recovery phrase, never a private key — a value that looks like either is refused, and not printed. |
| `CONFIG_URL` | optional | The `network-info.txt` of the network to join. Empty: the public network's, as `deploy/install.sh` names it. |
| `DENDRA_WORKSPACE` | optional | Where the persistent volume is mounted. Default `/workspace`. |
| `DENDRA_MODEL_ID` | optional | Imposes the model. Empty: `deploy/hw_probe.sh` sizes it to the card, as on any miner. |
| `DENDRA_ACCEPT_EPHEMERAL=1` | opt-out | Start without a persistent volume, accepting that the miner's key, identity and stake are lost with the pod's disk. |
| `DENDRA_ALLOW_IDLE_GPUS=1` | opt-out | Start on a pod with more than one GPU. One miner serves one card, and the network pays a miner, not a card. |

## RunPod

- **Container image**: the digest above.
- **Volume**: a volume disk (or a network volume) **mounted at `/workspace`**. RunPod keeps that disk
  across a stop and a restart and deletes it with the pod; the container disk does not survive a stop.
- **Environment variables**: `DENDRA_PAYOUT_ADDRESS`, and whichever optional ones you need.
- **GPU count**: 1. **Exposed ports**: none.
- Leave the start command empty: the image's own entry point is the miner.

## Vast

- **Image**: the digest above. Vast offers several launch modes; the image's entry point runs in the
  **entrypoint** mode. In the SSH or Jupyter modes the provider may start its own process instead — then
  run `/opt/dendra/docker/cloud-start.sh` from the instance's on-start script, with the same variables.
  Check which one your template uses before relying on it. Started that way, the script is not the
  container's first process: when it stops, it stops the miner and Ollama, and **the instance keeps
  running, and billing**, until you stop it from Vast's console (the log says so).
- **Storage**: mount a volume and point `DENDRA_WORKSPACE` at it, or `/workspace` if that is where it is
  mounted. The pod refuses to start on a directory that is not a mounted volume, or that is mounted on a
  filesystem that does not outlive the pod (`tmpfs`, `ramfs`, the container's own `overlay`), unless
  `DENDRA_ACCEPT_EPHEMERAL=1`.
- **One GPU**, no open port.

## What the pod does when it starts

Every refusal comes **before** anything starts, so a refused pod downloads nothing and registers nothing:

1. the payout address is checked with the miner's own rule (`final_season_address.py::payable_address`):
   an address one character off, or a module account, is refused rather than paid to nobody; on a
   restart, an address that is one of the keys the pod's own keyring holds is refused too;
2. the volume must be a mounted one, on a filesystem that outlives the pod (or the opt-out set);
3. one GPU (or the opt-out set);
4. the network is checked by `deploy/join.sh`'s own functions, sourced from the image: the genesis the
   network serves must be the one `docker/GENESIS_SHA256` names in the image, its consensus epoch must
   match, and a kit older than the network's is said, with the update path of a pod. These are the
   checks `join.sh --remote-rpc` makes, from the same code. They prove which genesis this is; they do
   not prove who runs the RPC you read — that is what running your own node buys, and a pod does not.

Then it starts Ollama **on 127.0.0.1**, downloads the model and the embedder into the volume (once: the
volume keeps them; the pod is billed while it downloads — the size of each model is written next to its
tag in `deploy/testnet-miner/docker-compose.yml`), and starts the miner. The miner funds itself at the
faucet, registers, and **declares the payout address with a lock**. Until the miner's record shows that
declaration, the pod's log repeats **`payout NOT declared`** with the reason it knows of; a declaration
the programme accepted **without** the lock is reported as **`NOT locked`**, again at each check, while the
miner asks for the lock again. `LOCKED` is what the programme named by the network's settings answered: when
those settings are read over plain HTTP, nothing authenticates which programme that was, and the
programme's published evidence (`evidence/day-<N>.jsonl`) is where a filed lock can be checked.

If Ollama or the miner stops, everything the image started stops, and so does its entry point (exit code
1): run as the container's entry point, that ends the pod, so that it is not billed while serving nothing.
Started from an on-start script instead (Vast, above), it does not end the instance. Lines
`DENDRA_CLOUD_STATE=…` and `DENDRA_CLOUD_PAYOUT=…` say the same in a form a script reads.

Exit codes: `0` stopped on request · `1` a process died or a step failed · `2` refused, reason named ·
`3` something needed to decide could not be read.

## The keys on a machine you rent

The key that **signs** the miner's work lives on the pod's volume, in clear, and **the host can read it**.
Treat it as disposable: it holds the stake the faucet gave it, nothing else, and nothing else should be
sent to it. What the season **pays** goes to the payout address, whose key is not on the pod.

The **lock** is what keeps a copy of the pod's key from changing that address afterwards: once the
programme has filed a locked declaration for this miner, it refuses any other address for it. Two
consequences, both deliberate:

- **check the address before the first start.** A locked address cannot be changed, by you either. The
  pod's own key is never locked: the miner refuses to, and the pod refuses to start again with it;
- the lock protects the declaration the pod makes **first**. A host acting with the pod's key before the
  miner has registered and declared could lock its own address — the same host could also take the stake.
  Rent from a provider you are willing to trust with a disposable key.

## Updating

The pod compares its kit with the network's **when it starts** (step 4 above), and only then: a pod that
was started before the network published a newer kit keeps running without saying so. To compare while it
runs, read the network's `KIT_VERSION` in the `network-info.txt` the pod was given, and the pod's own in a
terminal of the pod: `cat /opt/dendra/docker/KIT_VERSION`. To update, redeploy the pod with the digest
`docker/CLOUD_IMAGE` names; the volume keeps the miner's identity, stake and models.

A pod runs no hourly health check, and its container healthcheck (`miner_selftest.py --heartbeat`) says
only that the miner's loop is alive. The self-test can be run by hand in a terminal of the pod,
`python3 /app/miner_selftest.py --quick`, as a reading: it changes nothing, and it does not promise a
reading of every check on a pod, which runs no compose kit. A pod does not judge: each jury seat the chain
draws it into gives no verdict.

## Leaving the network, and ending a rental

`deploy/testnet-miner/exit-miner.sh` drives the compose kit, which a pod does not run, and stopping the
miner inside a pod stops the pod: leaving the network from a pod is **not automated** in this image. A pod
ended without leaving keeps its registration on chain, with the stake on the pod's key. Two windows of the
chain apply, and they are not the same one: after `juror_freshness_blocks` without a commit the chain stops
drawing the miner into juries, and only after `miner_prune_blocks` — which cannot be shorter — does it
evict it (`miner_vitality.go::pruneInactiveMiners`), and never while the miner still carries an open
obligation. On eviction the stake is refunded to the key that registered the miner: the pod's key, which
lives on the pod's volume — a volume deleted with the rental takes that key with it.

## The faucet

The miner funds its registration at the network's faucet, which pays a limited number of times per IP
address. On a host whose pods share one public address, the faucet can refuse a new pod: the miner's log
names the faucet's answer, and the pod's log keeps saying `payout NOT declared` while it waits.
