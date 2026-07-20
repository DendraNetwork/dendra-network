# Join the Dendra testnet — all-in-one GPU miner

> **Simplest: `CONFIG_URL=<network-info.txt> bash deploy/join.sh`** (one command, sane defaults, self-diagnostics). This kit remains the **advanced/manual** path.

Run your GPU as a **miner** on a Dendra testnet: you receive encrypted inference jobs, serve them
**on your machine**, and earn `DNDR` on each honest verdict. No need to launch a chain — this
package only runs **your local engine (Ollama) + the miner client**, connected to the operator's **public**
endpoints.

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
nano .env                       # 2) fill in DENDRA_NODE / DENDRA_RELAY / FAUCET + a unique MINER_ID
docker compose up -d --build    # 3) start (first build is long: compiles dendrad + pulls the model)
```
Follow: `docker compose logs -f miner`. You should see the faucet self-funding, the on-chain
registration, then the job loop.

## NVIDIA GPU
Uncomment the `deploy:` block of the `ollama` service in `docker-compose.yml` (requires
`nvidia-container-toolkit` installed on the host), then `docker compose up -d`. Without it, Ollama runs on **CPU**.

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

## Docker-free alternative (CLI)
The CANONICAL path is `deploy/join.sh` (preflight, auto GPU, verified SHA):
```bash
bash deploy/join.sh --miner
```
