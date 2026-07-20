# Dendra — Containerization (Docker)

The whole network in containers. Services are grouped by **Compose profile** so a VPS operator, a solo
developer and a monitoring stack don't have to run the same things.

| Profile | Services | When |
|---|---|---|
| *(none — always up)* | `chain` · `faucet` · `relay` · `gateway` · `open-webui` · `caddy` | the core network + the chat front door |
| `public` | `proof` · `points` · `capacity` | a **public** deployment: the on-chain proof feed, the points ledger, the capacity registry |
| `monitoring` | `exporter` · `prometheus` · `grafana` | metrics (Prometheus/Grafana bind to **127.0.0.1** — reach them over an SSH tunnel, never publicly) |
| `local` | `ollama` · `miner1` | a **single-box dev run**. On a real VPS the miners are **external** — they join with `deploy/join.sh`. |

> **Note:** these images are not exercised by CI. If the `dendrad` build fails, check `./cmd/dendrad` in
> `chain/`. Validate on the host with `docker compose build`.

## Prerequisites

1. Docker + Compose v2 on the host.
2. **Chain source** in the build context: `chain/` (committed — it is the source of truth for the code).
3. (GPU, `local` profile only) `nvidia-container-toolkit` + uncomment the `deploy:` block of the `ollama`
   service. Otherwise Ollama runs on CPU (slow).

## Run

```bash
docker compose build chain                       # dendra/chain:latest (provides dendrad)
docker compose build                             # dendra/services:latest
docker compose up -d                             # core only
docker compose --profile public up -d            # + proof / points / capacity
docker compose --profile monitoring up -d        # + exporter / prometheus / grafana
docker compose --profile local up -d             # + a local ollama & miner (dev box)
```

**Ports.** Chat `:8080` · Gateway `:8651` · Relay `:8645` · Chain RPC `:26657`, REST `:1317`, gRPC `:9090` ·
Faucet `:4500` · Proof `:8090` · Points `:8091` · Capacity `:8092` · Caddy `:80/:443` ·
Grafana `127.0.0.1:3000` · Prometheus `127.0.0.1:9099`.

**Public hostnames** are terminated by `caddy` (automatic Let's Encrypt, config in `docker/Caddyfile`):
`chat.` → Open WebUI · `api.` → gateway (plus a keyless `/demo` path with server-side key injection) ·
`proof.` → the proof feed · `/rpc`, `/rest`, `/capacity` → chain & registry, CORS-enabled for the explorer.

First start on the `local` profile: pull a model —
`docker compose exec ollama ollama pull llama3.1:8b-instruct-q4_K_M`.
On a real deployment, model choice is **measured, not guessed**: `deploy/hw_probe.sh` sizes it to the VRAM.

## Image architecture

| Service | Image | Role |
|---|---|---|
| `chain` | `Dockerfile.chain` (golang + ignite) | Cosmos node, denom `udndr` (1 DNDR = 1e6 udndr), fixed supply 10M, zero mint |
| `faucet`/`relay`/`gateway`/`proof`/`points`/`capacity`/`exporter`/`miner1` | `Dockerfile.services` (python3.12 + dendrad) | Python services; reach the chain via `DENDRA_NODE=tcp://chain:26657` |
| `ollama` | `ollama/ollama` | LLM inference (GPU optional) |
| `caddy` | `caddy:2` | TLS reverse proxy (auto-HTTPS) |
| `open-webui`/`prometheus`/`grafana` | official images | chat UI + monitoring |

## Known limitations (to harden before prod)

- **Dev faucet**: unauthenticated (fine on a resettable testnet, not for prod).
- **`test` keyring** (plaintext keys) on the miners: replace with an encrypted keyring / HSM in prod.
- **Large `chain` image** (Go + ignite). Possible optimization: multi-stage build → standalone `dendrad`.
- **Content filtering is a deterministic regex floor only** (there is no LLM classifier in the stack). It is
  demonstration-grade, with high false-negatives — never describe it as a compliance guarantee.
- **Mining and judging on one box need two Ollama instances**: the GPU one (`:11434`) for mining, a
  CPU-only one (`:11435`, `CUDA_VISIBLE_DEVICES=""`) for the MoE judge. Sharing one instance serialises both
  workloads and starves the miner. The launch kit (`deploy/launch/`) sets this up; the `local` profile here
  is single-instance and is for mining only.
