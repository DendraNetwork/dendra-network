# Dendra — Containerization (Docker)

The whole network in containers. Services are grouped by **Compose profile** so a VPS operator, a solo
developer and a monitoring stack don't have to run the same things.

| Profile | Services | When |
|---|---|---|
| *(none — always up)* | `chain` · `faucet` · `relay` · `gateway` · `caddy` | the core network + its HTTPS front (`testnet-api.`, `testnet-proof.`) |
| `public` | `proof` · `points` · `capacity` · `final-season` · `final-season-generator` | a **public** deployment: the on-chain proof feed, a read-only index recomputed from on-chain jobs and verdicts (`points`, loopback only; it is not the Final Testnet Season programme, and nothing it computes counts toward the season's points, since on-chain job pay never does), the capacity registry, and the Final Testnet Season programme — its service and its request generator (see [Final Testnet Season](#final-testnet-season-public-profile)) |
| `monitoring` | `exporter` · `prometheus` · `grafana` | metrics (Prometheus/Grafana bind to **127.0.0.1** — reach them over an SSH tunnel, never publicly) |
| `local` | `ollama` · `model-init` · `miner1` | a **single-box dev run**, on an NVIDIA GPU. On a real VPS the miners are **external** — they join with `deploy/join.sh`. |
| `chat` | `open-webui` | a chat interface over the gateway. Not started on a validator host, and the bundled `Caddyfile` serves no site for it |

The faucet and the gateway mount the `service-keys` volume, a keyring holding only their own two keys
(`bob`, `gw`). The Final Testnet Season `payout` and `generator` keys live in a third keyring,
`programme-keys`, mounted only by `chain` (to create them at genesis; it already holds the validator's
keys) and by
`final-season-generator` (to sign, outbound only), never by the faucet, the gateway or the `final-season` service,
which answer the public. The validator's keys stay in `dendra-home`, which only `chain` mounts.

The `chain` service is a validator. The public testnet, `dendra-testnet`, launched from a genesis with a
single validator, operated by the project on one host: at launch it orders and finalises every block.
A validator holding a third or more of the voting power is a single point of failure: if it stops, the
chain stops, and at launch the project's validator holds all of it. Read the current set with
`dendrad query staking validators`. Its launch genesis sets `committee_min_vrf_contributors` to 1 (the
entrypoint default of `DENDRA_MIN_VRF_CONTRIB`); while it is 1, one validator's VRF output carrying at least two thirds of the committing power is enough to
seed a draw, so at launch the project's validator alone produces the randomness of every assignment and audit draw;
raising that floor takes independent validators and a governance vote. Read the live parameters with
`dendrad query jobs params -o json` or the REST path `/dendra/jobs/v1/params`. The chain's name comes
from `DENDRA_CHAIN_ID` in the network file: the entrypoint never defaults it, and it refuses a
`dendra-home` volume whose genesis names another chain instead of overwriting it.

> **Note:** these images are **not** exercised by CI — `ci.yml` builds and tests the Go and Python
> sources, never the containers. The release workflow builds exactly one of them, `Dockerfile.miner`,
> and publishes it pinned by digest (`miner-image.txt`). `deploy/join.sh` pulls a prebuilt miner image
> only when the clone's `docker/MINER_IMAGE` pins one, starts it only when its platform is that of the
> Docker engine, and builds the image from the clone otherwise; a
> `DENDRA_MINER_IMAGE` served by the network file is ignored. Nothing automatic guards
> `Dockerfile.chain`, `Dockerfile.node` or `Dockerfile.services` against a regression: validate on your
> host with `docker compose build`. If the `dendrad` build fails, check `./cmd/dendrad` in `chain/`.

## Prerequisites

1. Docker + Compose v2 on the host.
2. **Chain source** in the build context: `chain/` (committed — it is the source of truth for the code).
3. (`local` profile only) an NVIDIA GPU and `nvidia-container-toolkit` on the host: the profile gives the
   host's cards to `ollama`, and no mining model runs on the CPU. On a host without them, `up` stops at
   `ollama` on the device request.

## Run

```bash
docker compose build chain                       # dendra/chain:latest (provides dendrad)
docker compose build                             # dendra/services:latest
docker compose up -d                             # core only
docker compose --profile public up -d            # + proof / points / capacity / final-season / final-season-generator
docker compose --profile monitoring up -d        # + exporter / prometheus / grafana
docker compose --profile local up -d             # + a local ollama & miner (dev box)
docker compose --profile chat up -d              # + open-webui (never on a validator host)
```

### Stamp a version into the images

`dendrad` carries no version of its own: the string is injected at link time. Build without saying
which tree you are building and `dendrad version` prints an **empty line**, which tells an operator
nothing about what is running. The three images that compile the binary (`chain`, `node`, `miner`)
accept two build arguments:

```bash
docker build -f docker/Dockerfile.chain -t dendra/chain:latest \
  --build-arg VERSION="$(git describe --tags --match 'v[0-9]*' --always)" \
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

**Ports.** Gateway `127.0.0.1:8651` · Relay `:8645` · Chain RPC `:26657`, P2P `:26656`, REST `:1317`,
gRPC `:9090` · Faucet `:4500` · Proof `127.0.0.1:8090` · Points `127.0.0.1:8091` ·
Capacity `127.0.0.1:8092` · Final Testnet Season `:8093` (no published port: reached only through Caddy) ·
Caddy `:80/:443` · Chat `127.0.0.1:8080` (`chat` profile) · Exporter `:9101` (`monitoring` profile,
published on every interface) · Grafana `127.0.0.1:3000` · Prometheus `127.0.0.1:9099` ·
Ollama `:11434` (`local` profile).

**Public hostnames** are terminated by `caddy` (automatic Let's Encrypt, config in `docker/Caddyfile`),
and there are two: `testnet-api.` → gateway (plus a keyless `/demo` path with server-side key injection, the one
the website's `/chat` page calls) · `testnet-proof.` → the proof feed. Under `testnet-api.`, `/rpc`, `/rest`, `/capacity`
→ chain & registry, CORS-enabled for the explorer, and `/final-season/*` → the Final Testnet Season
service. No `chat.` site is served on the validator host.

The `local` profile serves the model named by `DENDRA_MODEL_ID` in the `.env`: `model-init` pulls it into
`ollama` before `miner1` starts, and stops, with `miner1` left unstarted, when the variable is empty or the pull
fails. The model is **measured, not guessed**: `deploy/hw_probe.sh` sizes it to the VRAM. The miner's entrypoint
asks the engine where the served model runs (`serve_guard.py`) and does not start a miner whose model the CPU
holds. A host without an NVIDIA GPU serves no mining model: it judges on the CPU, or has no role
(`deploy/hw_probe.sh --role`).

### Final Testnet Season (`public` profile)

The Final Testnet Season (ADR-047, rules at <https://dendranetwork.com/final-season/>) runs as two services
and one role run by hand, all from `dendra/services`:

- **`final-season`** answers through Caddy (`api.…/final-season/*`): the status, signed payout declarations, the
  evidence log, the grading sample and grades (grader token) and the daily rankings, which it computes from
  what the chain records — verified programme requests, verdicts of drawn jurors, availability windows
  proven on chain — and from its own published evidence (the jobs whose answer it received, the grades);
  a day that received answers is ranked only once they are sealed (`final_season_server.py::rank_days`). It
  sets no test. It holds **no key that moves funds** — only its draw key, on the `final-season-data` volume
  with the evidence — and publishes no port; its `/internal` route is never proxied, and refuses
  any call without `DENDRA_FINAL_SEASON_INTERNAL_TOKEN`. It refuses to start
  without `DENDRA_FINAL_SEASON_START_HEIGHT`, the season's first block, a published fact of the programme that is
  never guessed. Without `DENDRA_FINAL_SEASON_GENERATOR` (the generator's address, which the launcher adds to the
  `.env`) it ranks no day, since the work reward counts that account's jobs; without
  `DENDRA_FINAL_SEASON_GRADER_TOKEN` no work answer is graded by a model, and no day's work can be taken away for
  incoherent answers. Once the chain's latest block is at or after the season's end, it refuses new work
  answers and payout declarations (410).
- **`final-season-generator`** sends the programme's requests from the `generator` account — a fixed
  `requests_per_day` a day (`final_season_rules.py::RULES`), whatever the number of miners — and forwards each
  answer with its prompt to the service's internal route (`/internal/work_answer`, same token) before it
  settles the job (`final_season_generator.py::run_one`); once the
  day is over the service draws at most three per identity into the evidence for the grader. The service
  records the job of every answer received and the day's seal lists them: a programme job counts as work
  only if it is in that list (`final_season_facts.py::answered_jobs`), so a forward that fails is logged, the
  request is still settled, and it is not counted as work. Outbound only. It is an ordinary client of
  the chain: the chain draws the serving miner among the present ones, and at launch the project's
  validator alone produces the randomness of that draw (while `committee_min_vrf_contributors` is 1, one
  validator's VRF output carrying at least two thirds of the committing power suffices). It mounts
  `programme-keys`, and its own volume `final-season-generator` at `/data/final-season-generator`
  (`DENDRA_FINAL_SEASON_GENERATOR_STATE`): after each request it writes how many of the day's requests it has
  sent, keyed by the season's first block and the day (`final_season_generator.py::save_sent`), so a restart
  resumes from the last count written; a count it cannot write is logged as a warning, and a restart
  after it sends those requests again; a count it cannot read stops it (exit 2), since a count not known
  is not zero. A request counts on the block it settles, so it opens none in the last ten minutes before
  the season's end (`final_season_generator.py`, `STOP_MARGIN_S`), and on the last, shorter day it spreads
  that day's requests over the time left.
- **`final-season-payout`** is a role, not a service: the weekly payment, run from the generator's container
  because that is where the `payout` key is mounted.

```bash
docker compose --profile public run --rm final-season-generator final-season-payout --week N         # plan only: who gets what
docker compose --profile public run --rm final-season-generator final-season-payout --week N --yes   # sign and broadcast
```

Week *k* pays days 7k to 7k+6 from the rankings the `final-season` service publishes. The Final Testnet
Season ends on 7 November 2026 at 23:59 UTC: it counts every block timestamped before 8 November 2026,
00:00 UTC — the chain's own block times decide. Season days are 17 280 blocks each; the last one is cut at
the end, and its ranking names the season's last block (`inputs.season_end_height`). The last week stops
at that day: it pays the days left after the last full week, once that day is final, and a week after it
is refused (`final_season_payout.py::plan_days`). A week with an unranked day is refused, never paid in
part, and so is a week when any published day from the first to the last of that week breaks the rules
(each row's gross rerun from its published counts, payable, totals, daily budget, pro rata, season cap:
`final_season_payout.py::check_days`). The check cannot tell a false count from a true one: what a compromised
service could misdirect is bounded by the day's budget (50 DNDR) and the season cap (1 500 DNDR). Each transaction
carries a memo naming the week, a digest of its payable list and its part number, and the chain is
searched before anything is signed: a part found on
chain is never sent again, an interrupted run is finished by running it again, and a part found under a
different digest stops everything for a person to decide. The node it reads must keep the whole history.
If the genesis gave the payout account a cold address (`DENDRA_PAYOUT_ADDR`), its key is not on this host
and the role refuses to sign.

## Image architecture

| Service | Image | Role |
|---|---|---|
| `chain` | `Dockerfile.chain` (two stages: `go build` in golang, then only the binaries on debian-slim) | Cosmos node, denom `udndr` (1 DNDR = 1e6 udndr), zero mint: the supply starts at 10M and never rises |
| `faucet`/`relay`/`gateway`/`proof`/`points`/`capacity`/`final-season`/`final-season-generator`/`exporter`/`miner1` | `Dockerfile.services` (python3.12 + dendrad) | Python services; reach the chain via `DENDRA_NODE=tcp://chain:26657` |
| `final-season-payout` (role) | `Dockerfile.services`, run with `docker compose run` in the `final-season-generator` container | Final Testnet Season weekly payment from the `payout` key; plan only unless `--yes` |
| `ollama`/`model-init` | `ollama/ollama`, pinned by version and multi-platform digest (the miner kit's engine) | LLM inference on the host's NVIDIA GPU; `model-init` pulls `DENDRA_MODEL_ID` into it |
| `caddy` | `caddy:2` | TLS reverse proxy (auto-HTTPS) |
| `open-webui`/`prometheus`/`grafana` | official images | chat UI (`chat` profile) + monitoring |

## Known limitations (to harden before prod)

- **Faucet**: the anti-Sybil proof of work is **off by default** (`DENDRA_FAUCET_POW_BITS=0`), which is
  fine on a closed, resettable testnet. On a public deployment it is mandatory and enforced by the code,
  not by the operator's memory: with `DENDRA_PUBLIC=1` the daemon **refuses to boot** while the PoW is
  disabled. The launch kit sets both (`deploy/launch/.env.public.example`).
- **`test` keyring** (plaintext keys) on the miners and in `programme-keys` (the `generator` key, and the
  `payout` key unless the genesis gave it a cold address): replace with an encrypted keyring / HSM in prod.
- **Content filtering, as this kit ships it, is a deterministic regex floor only**: the optional classifier
  stage stays off while `DENDRA_GUARD_MODEL` is empty, and the kit leaves it empty. It is
  demonstration-grade, with high false-negatives — never describe it as a compliance guarantee.
- **Mining and judging on one box need two Ollama instances**: the main one (`:11434`, on the GPU when
  there is one) for mining, a CPU-only one (`:11435`, `CUDA_VISIBLE_DEVICES=""`) for the judge model, a
  mixture-of-experts that runs on the CPU with or without a GPU. No mining model runs on the CPU: without a
  GPU, the miner kit (`deploy/testnet-miner`) serves the judge model alone, on its CPU instance, and the
  miner image refuses at start a mining model the CPU holds (`serve_guard.py`). No judge is seated on the GPU. Sharing one instance serialises both workloads and starves the miner. The launch kit (`deploy/launch/`) sets this up; the `local` profile here
  is single-instance and is for mining only.
