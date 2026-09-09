# Dendra — Containerization (Docker)

The whole network in containers. Services are grouped by **Compose profile** so a VPS operator, a solo
developer and a monitoring stack don't have to run the same things.

| Profile | Services | When |
|---|---|---|
| *(none — always up)* | `chain` · `faucet` · `relay` · `gateway` · `open-webui` · `caddy` | the core network + the chat front door |
| `public` | `proof` · `points` · `capacity` | a **public** deployment: the on-chain proof feed, the points ledger, the capacity registry |
| `monitoring` | `exporter` · `prometheus` · `grafana` | metrics (Prometheus/Grafana bind to **127.0.0.1** — reach them over an SSH tunnel, never publicly) |
| `local` | `ollama` · `miner1` | a **single-box dev run**. On a real VPS the miners are **external** — they join with `deploy/join.sh`. |

> **Note:** these images are **not** exercised by CI — the published workflow builds and tests the Go
> and Python sources, never the containers, so nothing here is guarded against a Dockerfile regression.
> The four Dockerfiles (`chain`, `node`, `miner`, `services`) have been built from this tree by hand and
> all four succeeded; that is a point measurement on one host, not a standing guarantee. Validate on
> yours with `docker compose build`. If the `dendrad` build fails, check `./cmd/dendrad` in `chain/`.

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

### Stamp a version into the images

`dendrad` carries no version of its own: the string is injected at link time. Build without saying
which tree you are building and `dendrad version` prints an **empty line**, which tells an operator
nothing about what is running. The three images that compile the binary (`chain`, `node`, `miner`)
accept two build arguments:

```bash
docker build -f docker/Dockerfile.chain -t dendra/chain:latest \
  --build-arg VERSION="$(git describe --tags --always)" \
  --build-arg COMMIT="$(git rev-parse HEAD)" .

docker run --rm --entrypoint dendrad dendra/chain:latest version          # the injected version
docker run --rm --entrypoint dendrad dendra/chain:latest version --long   # + commit and go version
```

Left unset they default to `dev` / `unknown` — honest for a working build, and still not empty.
`dendra/services` does not compile anything; it takes `dendrad` from `dendra/chain`, so it reports
whatever that image was stamped with.

The build flags are the ones the release workflow uses (`-trimpath`, `-buildvcs=false`,
`CGO_ENABLED=0`, version injected through `-ldflags -X`). That is deliberate: with the same tag and
platform the binary inside the image is byte-for-byte the published release binary, so its SHA-256
can be checked against `SHA256SUMS.binaries` of the release. Removing any of those flags — or writing
the version into a tracked file instead of injecting it — silently breaks that property.

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
| `chain` | `Dockerfile.chain` (golang, `go build ./cmd/dendrad`) | Cosmos node, denom `udndr` (1 DNDR = 1e6 udndr), fixed supply 10M, zero mint |
| `faucet`/`relay`/`gateway`/`proof`/`points`/`capacity`/`exporter`/`miner1` | `Dockerfile.services` (python3.12 + dendrad) | Python services; reach the chain via `DENDRA_NODE=tcp://chain:26657` |
| `ollama` | `ollama/ollama` | LLM inference (GPU optional) |
| `caddy` | `caddy:2` | TLS reverse proxy (auto-HTTPS) |
| `open-webui`/`prometheus`/`grafana` | official images | chat UI + monitoring |

## Known limitations (to harden before prod)

- **Faucet**: the anti-Sybil proof of work is **off by default** (`DENDRA_FAUCET_POW_BITS=0`), which is
  fine on a closed, resettable testnet. On a public deployment it is mandatory and enforced by the code,
  not by the operator's memory: with `DENDRA_PUBLIC=1` the daemon **refuses to boot** while the PoW is
  disabled. The launch kit sets both (`deploy/launch/.env.public.example`).
- **`test` keyring** (plaintext keys) on the miners: replace with an encrypted keyring / HSM in prod.
- **Large `chain` image** (the full Go toolchain is kept in the final layer). Possible optimization:
  multi-stage build → a standalone `dendrad`, as `Dockerfile.node` already does.
- **Content filtering is a deterministic regex floor only** (there is no LLM classifier in the stack). It is
  demonstration-grade, with high false-negatives — never describe it as a compliance guarantee.
- **Mining and judging on one box need two Ollama instances**: the GPU one (`:11434`) for mining, a
  CPU-only one (`:11435`, `CUDA_VISIBLE_DEVICES=""`) for the MoE judge. Sharing one instance serialises both
  workloads and starves the miner. The launch kit (`deploy/launch/`) sets this up; the `local` profile here
  is single-instance and is for mining only.
