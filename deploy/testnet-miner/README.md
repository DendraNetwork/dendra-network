# Join the Dendra testnet — all-in-one GPU miner

> **Simplest: `CONFIG_URL=<network-info.txt> bash deploy/join.sh`** (one command, sane defaults, self-diagnostics). This kit remains the **advanced/manual** path.

Run your GPU as a **miner** on a Dendra testnet: you receive encrypted inference jobs, serve them
**on your machine**, and are **paid** `DNDR` on each honest verdict. No need to launch a chain — this
package only runs **your local engine (Ollama) + the miner client**, connected to the operator's **public**
endpoints.

> ⚠️ **Paid, then held.** On the public network `hold_bps` reads `10000`: **100 % of your net share is
> retained at settlement** and released only at the job's audit checkpoint — which cannot run until the
> beacon reaches **2 VRF contributors**, and a contributor is a **validator**, not a miner. Mining does
> not unlock mining pay; a second validator does. Read [`../README.md`](../README.md) before you budget
> for this, and check `vrf.contributors` against `vrf.min` at `https://proof.dendranetwork.com/proof`.

> **The model is LOCAL.** It is downloaded to **your** machine (Docker volume) and served by **your** GPU.
> Nobody connects to a central model: this is the very condition for Dendra's decentralization,
> privacy (E2E) and verification.
>
> **Status: research / testnet.** `$DNDR` = utility token, no sale. Privacy Mode A = deterrence
> (sealed memory + slashing), not a cryptographic guarantee against a root miner.

## Prerequisites
- **Docker** + Docker Compose v2 (`docker compose version`).
- **NVIDIA GPU recommended** + `nvidia-container-toolkit` (otherwise it runs on CPU, slow). See step 2.
- **~6 GB of disk** for the model.
- The testnet's **public endpoints** (RPC / relay / faucet) — from the operator.
- The **cloned repository** (the first build compiles the chain binary `dendrad`).

## 3 steps
```bash
cd deploy/testnet-miner
cp .env.example .env            # 1) copy the config
nano .env                       # 2) fill in DENDRA_NODE / DENDRA_RELAY / FAUCET / DENDRA_RELAY_TOKEN + a unique MINER_ID
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
> | `POST req/…`, `GET list` `stats` | the token — the gateway's route, and the network-mapping surface. |
>
> So leave `DENDRA_RELAY_TOKEN` empty. A `401` on a **read** means the operator runs an older relay
> build that gates every route behind a shared token — ask them to update rather than asking for a
> secret you would then have to keep.

> **Step 4 is not decoration, and it does not repeat itself.** The capacity registry is what the public
> `/network` page displays — and what the public **chat** reads before it will accept a prompt at all.
> With no report, `verified.live_nodes` is `0`, the chat closes its composer on purpose ("a page that
> cannot confirm a miner is serving must not invite one"), and your miner answers nothing while looking
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
> that as your own declaration, which is worse than showing nothing. The script now refuses to publish
> when it can see the contradiction, but it can only see the cases it knows about.

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
  starting over with a new identity (and re-staking).
- **`DENDRA_MODEL_ID` must match the on-chain registry.** If the operator enforces the model registry
  (`enforce_model_registry`), serving a different model = rejected commits / slash. Keep the provided value.
- **Real SEMANTIC verification**: this package embeds via Ollama (`DENDRA_EMBED_MODE=backend` +
  `nomic-embed-text`) → verification measures **meaning**, not just shared words (≠ the lexical `hash`
  embedder of the local devnet). All miners in a committee must use the **same** embedder (keep the value).
  The cosine threshold still needs **cross-hardware calibration** before a large network (embeddings are stable, but measure it).
- **Build not tested in Docker CI**: if the `dendrad` build fails,
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
