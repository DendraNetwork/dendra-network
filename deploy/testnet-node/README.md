# Join the Dendra testnet — full node (and, optionally, validator)

> **Simplest: `CONFIG_URL=<network-info.txt> bash deploy/join.sh`** (one command, sane defaults, self-diagnostics). This kit remains the **advanced/manual** path.

Run a **node** that syncs the Dendra chain from the public seeds. Useful for: your own RPC,
serving as a network peer, or **becoming a validator**. This package does **not** launch its own chain — it joins
the operator's (public genesis + seeds).

> **Status: research / testnet.** `$DNDR` = utility token, no sale. Resettable network, tokens have no value.

## Prerequisites
- **Docker** + Docker Compose v2.
- The operator's network info: **CHAIN_ID**, **GENESIS_URL** (the public genesis.json), **SEEDS**.
- The **cloned repository** (the first build compiles the `dendrad` binary).
- Disk depending on chain size; ports **26656** (P2P) open inbound = a bonus for the network.

## 3 steps — run the node

> **Does this machine already run a Dendra node?** Then read
> [If this machine already runs a Dendra node](#if-this-machine-already-runs-a-dendra-node) FIRST.
> Compose keys a project by **name**, not by directory: a second copy of this repository resolves to
> the same project and the same data volume, so `up` there **recreates the node you already have**
> instead of adding one. Check in one second, from anywhere:
> ```bash
> docker compose ls --all | grep dendra-node   # a line here = this machine already has that project
> ```

```bash
cd deploy/testnet-node
cp .env.example .env            # 1) copy the config
nano .env                       # 2) fill in CHAIN_ID / GENESIS_URL / SEEDS
docker compose up -d --build    # 3) start (first build is long: compiles dendrad, then syncs)
```
RPC at `http://localhost:26657/status` (or `DENDRA_RPC_PORT` if you changed it).

> ⚠️ **On a VPS, close that RPC.** This line used to call it "local RPC". It is not: the kit publishes
> 26657 on **every interface**, so on a rented machine it is an unauthenticated endpoint open to the
> internet — anyone can submit transactions, read your peer topology and run expensive queries. It does
> not expose your signing key (that is a file on disk), but there is no reason to leave it open.
> ```bash
> echo 'DENDRA_RPC_BIND=127.0.0.1' >> .env && docker compose up -d
> ```
> **The cost, and it depends on where your miner looks.** Check before you close anything:
> ```bash
> docker exec dendra-miner-miner-1 printenv DENDRA_NODE
> ```
> · `tcp://host.docker.internal:26657` — the **default**: your miner uses your own node, through the
>   Docker bridge gateway (measured: `172.17.0.1`), **not** through loopback. Binding to `127.0.0.1`
>   disconnects it. Leave the RPC open on that machine, or move the miner off it.
> · anything else (`--remote-rpc`) — your miner reads someone else's node, so closing yours costs you
>   nothing.
>
> ⛔ **And if you are the operator whose RPC other people's miners point at, this advice does not apply
> to you.** Closing the public endpoint that `--remote-rpc` miners were handed takes every one of them
> blind at once, and they have no way to tell you. Move them to their own nodes first, then close it.
> `bash deploy/node_reachability.sh` reports what this node currently exposes.

### Following the sync — and the wall of red you will see

`docker compose logs -f node` shows the height climbing. It also prints, during the whole catch-up, a
**`ERR SECURITY: ... decentralized VRF seed UNDER-DECENTRALIZED ... -> LEGACY fallback`** line at a
rate of roughly one per block — measured on a joiner replaying this chain on 2026-08-07: **29 048
such lines in 14 minutes**. It looks like the network is broken. It is not, and here is the honest
version:

- The line is emitted by `chain/x/jobs/keeper/decentralized_seed.go`, in the branch that compares
  the height's VRF contributor count against `CommitteeMinVrfContributors`, at the moment a committee
  is drawn. A replaying node **re-executes the historical blocks**, so it re-emits the alert once per
  historical draw. Seeing it during a replay says something about the chain's past, not about your
  node. (Cited by the name of the parameter, not by a line number: line numbers drift.)
- What it means is real, not cosmetic: for those draws the anti-grinding property was inactive and
  the committee was drawn from the legacy fallback. The chain does not halt (`decentralized_seed.go`
  returns a non-empty fallback on purpose) — it tells you loudly instead of degrading in silence.
- **Do not expect it to stop when you finish syncing.** It stops when enough validators have anchored
  a VRF key to meet the floor, which is a property of the *network*, not of your node. Read the floor
  rather than trusting this sentence — it is a governed parameter and this document is not its source:
  ```bash
  docker compose exec -T node dendrad query jobs params -o json
  ```
- To watch the sync without the noise:
  ```bash
  docker compose logs -f node | grep -v "UNDER-DECENTRALIZED"
  ```

## If this machine already runs a Dendra node

**The problem, measured.** From a fresh clone of this repository in `/tmp` on 2026-08-07,
`docker compose ps` listed the **running container of the machine's existing node** — a node that
clone had never started. `docker compose config` resolved `name: dendra-node`, the same project as
the existing one. Compose identifies a project by its **name**, and the name was hard-coded, so two
unrelated source trees mapped onto the same containers and the same `dendra-node_node-data` volume.
Running `up -d --build` from the second tree does not create a second node: it **recreates the first
one**, from different sources, on top of its chain state and its validator key.

**The guard.** This kit now refuses that by default. A `preflight` service runs before the node,
mounts the data volume **read-only** — it can refuse, it cannot damage — and stops everything if the
volume already holds a chain home that this copy of the kit was not told to use. Measured on this
compose file:

| situation | result |
|---|---|
| volume empty (new node) | `no chain home in volume … -> new node, continuing.`, node starts, `up` returns 0 |
| volume already holds a chain home | `REFUSING TO START`, node **never started**, `up` returns **1** |
| same, with `DENDRA_ADOPT_EXISTING_HOME=1` | `reusing the existing chain home (moniker …)`, node starts |

The discriminator is `.env`, which lives in **your copy** of the kit and is gitignored — a second
clone gets a fresh `.env` from `.env.example`, does not carry the flag, and is refused. An unset flag
counts as `0`: absence is a refusal, never a pass.

**If it is your own node** (you are seeing this once, after updating the kit), add one line to
`deploy/testnet-node/.env` and re-run:
```bash
echo 'DENDRA_ADOPT_EXISTING_HOME=1' >> deploy/testnet-node/.env
docker compose -f deploy/testnet-node/docker-compose.yml up -d
```

**If you want a genuinely separate node** — to try the kit without touching the one you have — give it
its own project, its own ports and its own image tag:
```bash
cat >> .env <<'EOF'
DENDRA_PROJECT=dendra-try
DENDRA_P2P_PORT=36656
DENDRA_RPC_PORT=36657
DENDRA_NODE_IMAGE=dendra/node:try
EOF
docker compose up -d --build
```
All four default to the previous literals (`dendra-node`, `26656`, `26657`, `dendra/node:latest`), so
an operator who sets nothing sees no change. `COMPOSE_PROJECT_NAME` still overrides the project name
if you already use it. What the override buys you, and what it costs:

- A distinct project gets a **distinct volume** (`dendra-try_node-data`), so the second node syncs
  **from scratch** — the whole chain, block by block. On a host applying on the order of 20 blocks per
  second that is about an hour per 90 000 blocks, and it gets longer every day the network runs. Read
  the rate off your own run rather than quoting one: `join.sh` prints the rate it is actually
  sustaining, and that is the only number that predicts your wait.
- `DENDRA_NODE_IMAGE` is not decoration. Both nodes build from the same Dockerfile, so without it the
  second build **retags `dendra/node:latest`** — the tag the first node's container restarts onto.
  This already happened on this machine: the live node's container references image
  `sha256:790dee8c7616…`, and `docker image inspect` on that id answers **`No such image`**, while
  `dendra/node:latest` now points at `sha256:4233913532d8…`. The image the running node was created
  from no longer exists; a rebuild by anyone else decided what it would come back as.
- Changing `DENDRA_P2P_PORT` changes only what is **published on the host**. The node still advertises
  the port from `external_address`, which this kit does not manage, so a second node on a different
  P2P port is reachable by you and invisible to the network until you set that field.

**Two caveats, both measured, neither of which the guard can fix:**

1. **`--force-recreate` destroys before it asks.** Compose recreates the dependent container *before*
   running the guard. Measured on this file: with a node running, `up -d --force-recreate` from a
   foreign tree left the node **recreated and in `created` state — stopped, and not restarted**
   (plain `up -d` left it `running` and untouched). That is still better than the alternative it
   replaces — a foreign binary writing to your chain state — but on a **bonded** validator it is
   downtime, and downtime is metered: see [Availability and peer
   redundancy](#availability-and-peer-redundancy--read-this-before-you-bond-stake). Recover with
   `docker compose -p dendra-node start node`, then unjail.
   `deploy/join.sh` uses `--force-recreate`, so this is the path a joiner actually takes.
2. **The build happens before the guard runs.** Compose builds every service before it starts any of
   them, so a refused `up -d --build` has already rebuilt (and retagged) the image. Set
   `DENDRA_NODE_IMAGE` *before* you experiment, not after.

## Become a VALIDATOR (manual step — stake bond)
The node must be **synced** (`catching_up: false` in `/status`). Becoming a validator **locks up stake**
→ it is a **deliberate action**, never automatic. In summary:

```bash
# 1) an operator key (keep the mnemonic somewhere safe)
docker compose exec node dendrad keys add validator --keyring-backend test

# 2) fund it: external joiner -> testnet faucet/channel; OPERATOR bootstrap -> send from the genesis node's
#    'validator' key (the faucet is not enough for a bond that keeps stake distribution <2/3)
# 3) create the validator — SDK 0.50+: via a JSON FILE (the --amount/--pubkey/... flags were REMOVED):
docker compose exec -T node sh -c 'PK=$(dendrad tendermint show-validator); printf "{\"pubkey\": %s, \"amount\": \"<AMOUNT>udndr\", \"moniker\": \"<MONIKER>\", \"commission-rate\": \"0.10\", \"commission-max-rate\": \"0.20\", \"commission-max-change-rate\": \"0.01\", \"min-self-delegation\": \"1\"}" "$PK" > /tmp/validator.json; dendrad tx staking create-validator /tmp/validator.json --from validator --keyring-backend test --chain-id "$CHAIN_ID" --yes'
```
(Amount: for an OPERATOR validator, aim for a distribution where no single validator holds >2/3 of total stake. Gas: min-gas-prices=0 on this testnet.)

## Availability and peer redundancy — read this BEFORE you bond stake

Bonding turns a sync problem into a **money** problem. A full node that falls behind just catches up;
a **validator** that falls behind gets jailed and slashed.

**What the chain demands — read it here, not from this page.** The parameters that decide your
exposure are **governed**: they change by vote, and any copy of them written into a document is true
only until the next one. This section therefore gives you the rule and the command, and deliberately
quotes no figure you could act on without checking.

```bash
docker compose exec -T node dendrad query slashing params -o json
```

The same four values are served over REST at `/cosmos/slashing/v1beta1/params`, if the operator
exposes the API. The rule that turns them into a budget lives in the Cosmos SDK, in
`x/slashing/keeper/infractions.go`:

    maxMissed = signed_blocks_window - round(signed_blocks_window x min_signed_per_window)

and the chain jails as soon as `missed_blocks_counter` goes **above** `maxMissed` — so the block that
jails you is the one after it. ⚠️ The product **alone** is how many blocks you must SIGN, not how many
you may miss; the two coincide only while `min_signed_per_window` is exactly 0.5, and reading it as
the allowance overstates the allowance the moment that ratio moves. `slash_fraction_downtime` is what
the jail burns, `downtime_jail_duration` is how long you stay out, and
`slash_fraction_double_sign` is the one no restart ever forgives — that one tombstones you.

**Do not do this arithmetic by hand.** From the repository root, `deploy/validator_health.sh` reads
the four parameters off the chain, derives the budget, and reports how much of it you have already
spent — including the case, invisible from your own logs, where you are already jailed:

```bash
bash deploy/validator_health.sh
```

Turning a budget in blocks into one in minutes needs the block interval, and **the consensus does not
fix it**: it emerges from `timeout_commit` and propagation. Measured over 850 blocks on 2026-08-07
(heights 91034→91884, chain timestamps 23:44:45Z → 00:56:01Z, 4275.535 s) it was **5.0300 s per
block** — an observation, not a constant, and worth redoing before you lean on it.

Whatever that budget works out to on the day you read this, two things do not change: **nothing warns
you**, and the cost is automatic.

- **A validator is not a desktop.** A machine that sleeps, reboots for updates, or gets its Docker
  daemon restarted will be jailed. Measured on the operator's own node: it was stopped at height
  91034 (23:44:45Z) and resumed at 00:51Z on the same height — **66 minutes down, ~787 blocks at the
  observed interval**, well past the allowance in force at the time, and it was jailed for it. Whether
  that same outage would cost you today is a question only the current parameters answer. In the same
  3-hour log window the process caught
  `SIGTERM` **67 times** (`ExitCode=0`, `OOMKilled=false`): it was being stopped from outside, not
  crashing. `restart: unless-stopped` brought it back every time and it was jailed anyway, because
  what the chain counts is the blocks you did not sign, not whether your container recovered.
- **Resource starvation was NOT the cause here, and probably is not yours either.** Same node, same
  machine, `docker stats --no-stream`: the node used **197.4 MiB of 31.29 GiB RAM and 10.07% CPU** on
  a 24-core host, sharing it with two Ollama servers and a miner; `df` showed **678 G free** on the
  chain volume for a 1.1 G data directory. Measure before blaming the hardware.

**Peer redundancy is the part the kit cannot fix for you.**

- The operator's `network-info.txt` currently names **the same single host** in `SEEDS` and in
  `PERSISTENT_PEERS`. Two fields, one machine, one point of failure.
- Peer discovery does not rescue you. `pex = true` is already set in the shipped config. On the
  operator's node, over a 3-hour window, PEX logged `We need more addresses. Sending pexRequest` and
  then `No addresses to dial. Falling back to seeds` **118 times**, and `config/addrbook.json` still
  contained **exactly one address**. In 186 `Ensure peers` samples the node had **1** outbound peer
  in 118 of them and **0** in the other 68.
- The reason is structural: at the time of measurement the public seed node reported `n_peers=1`, and
  that single peer was the operator's own machine. **The network was two nodes joined by one TCP
  link.** There was no third node for PEX to discover, so every joiner necessarily depended on the
  same host, and any hiccup on it put every validator on the clock toward the jail threshold.
  Counting connections is not counting hosts: a later sample showed `n_peers=2`, but both entries
  carried the **same remote IP** and the same moniker — one machine holding two connections, not a
  second independent operator. Check `remote_ip`, not the count.
- Nodes behind home NAT make this worse: with `external_address = ""` the node advertises itself as
  `0.0.0.0:26656`, which nobody can dial, so it can accept no inbound peers and never becomes a
  redundancy option for anyone else. If you can forward port **26656**, set `external_address` in
  `config.toml` to your public `host:port` — the kit's entrypoint does not manage that field, so it
  is an edit inside the volume, and it only helps the network once the port is genuinely reachable.

**So, concretely, before you bond:**

1. Ask the operator for **more than one** seed/persistent peer and put the full comma-separated list
   in `SEEDS` and `PERSISTENT_PEERS`. `node-join.sh` writes each string straight into `config.toml`,
   so a list needs no other change.
2. Run the node on something that stays up — a VPS, not a workstation. Disable sleep and unattended
   reboots.
3. If you cannot guarantee that, **run a full node and do not bond.** An unbonded node that goes
   offline costs nobody anything.

**What to watch** (every minute is reasonable; set the alert well below the budget
`deploy/validator_health.sh` derives for you, not at some figure copied from here):

```bash
# Am I keeping up? catching_up must be false and the height must climb.
curl -s localhost:26657/status | jq '.result.sync_info | {latest_block_height, latest_block_time, catching_up}'

# How many peers do I actually have? 1 means no redundancy; 0 means you are already missing blocks.
curl -s localhost:26657/net_info | jq '.result.n_peers'

# Am I in the consensus set, and am I missing blocks?
docker compose exec -T node dendrad query slashing signing-info "$(docker compose exec -T node dendrad tendermint show-validator)"
```

Read the last command with care: proto3 omits zero-valued fields, so a **missing**
`missed_blocks_counter` or `tombstoned` means *zero* / *false*, while a **failed query** means you
learned nothing at all. Do not let a failed query read as healthy.

> **Applying a change from this kit recreates your container.** These files are the kit, not your
> running node — editing them changes nothing until you re-run `docker compose up -d` in this
> directory, and that **recreates** the container (chain state survives in the named `node-data`
> volume, keys included). The recreation costs a short outage. If you are already bonded, that outage
> counts against your missed-block budget: unjail *after* the restart, not before.
>
> **And it can change the binary under you.** The recreated container starts from the *tag*
> `DENDRA_NODE_IMAGE` (default `dendra/node:latest`), not from the image the old container was built
> from — and a tag is mutable. Measured on the operator's own machine on 2026-08-07: the running
> container references `sha256:790dee8c7616…`, `docker image inspect` on that id answers **`No such
> image`**, and `dendra/node:latest` now resolves to `sha256:4233913532d8…`. Something rebuilt the tag
> while the node was up; the recreation would have silently adopted it. On a live chain a validator
> running a **different binary** from its peers recomputes a **different app hash** and diverges from
> consensus. Before recreating a bonded node, know which image the tag points at:
> ```bash
> docker inspect -f '{{.Image}}' dendra-node-node-1     # what is running now
> docker image inspect dendra/node:latest --format '{{.Id}}'   # what it would come back as
> ```
>
> **Recreation also throws away your diagnostic history.** The `docker logs` journal belongs to the
> *container*, not to the volume: recreating it starts an empty json-file log, and everything the old
> container recorded — the jail you are trying to explain, the peer churn before it — is gone. If the
> log is why you are looking, save it first: `docker compose logs --no-color node > node-$(date
> +%F).log`.

## Honest notes
- **`keyring-backend test`** = unencrypted keys on disk, fine for a testnet, **NOT for real value**.
- **Build not tested in Docker CI**: if the `dendrad` build fails, check
  `./cmd/dendrad` in `chain` (see `docker/README.md`).
- **Persistent state** in the `node-data` volume; deleting it = re-sync from scratch.
- **`docker compose ps` now shows two services.** `preflight` is the guard described above; it is
  expected to sit at `exited(0)` next to a running `node`. `exited(78)` means it refused — read its
  output with `docker compose logs preflight`. It is a one-shot check, not a daemon, and it holds no
  state: nothing breaks if you never look at it.
- A miner (serving GPU inference) ≠ a node: to mine, see `deploy/testnet-miner/`.
