# ADR-046 — Relaunch as `dendra-testnet` on a single validator

**Status:** Accepted by the project owner, 2026-10-04.
**Implementation:** In progress. The chain changes it depends on are
[ADR-048](ADR-048-epoch-14-consensus-batch.md); the reward programme run on top of it is
[ADR-047](ADR-047-final-testnet-season-reward-programme.md). The genesis builder (`docker/entrypoint-chain.sh`)
writes every value of the table below, refuses to start without a chain id, and checks the 10 000 000
DNDR total in the built file. The network file (`deploy/launch/.env.public.example`, copied to the host
as `.env`) names the chain and the cold pockets, and the launcher refuses a public launch while a cold
address is missing or while the validator produces no decentralised seed.
The launcher builds both images on the operator machine, ships them with `docker save | docker load`,
refuses unless the host reports the same image IDs, and starts them without building. The chain image is
built in two stages, so the host receives the two binaries and the tools its entrypoint calls, not the Go
build environment. The faucet and gateway keys live in a separate keyring
on its own volume (`service-keys`), the only key material those two services mount; the validator's keys,
and the payout and generator keys, stay in the chain home. Every service of the host rotates its logs,
the auxiliary services have memory ceilings, gRPC listens on loopback only (Docker publishes ports past
ufw, so a closed port is a binding, not a firewall rule), and the chat interface is an opt-in compose
profile with no `chat.` site in the proxy.

## Context

Two halts, one cause.

- **2026-09-17, 21 h 12 min.** The second-largest validator lost its host without a clean shutdown. The
  remaining voting power was below two thirds, so no block could be committed until that host came back.
- **From 2026-09-19 09:07 UTC.** The host carrying the largest validator, the public API, the relay, the
  faucet and the gateway stopped answering on every port. The surviving validator held 37.5 % of the
  power and could not produce a block.

CometBFT commits a block only with strictly more than two thirds of the voting power. With two large
validators, the loss of **either** one halts the chain: two machines, two single points of failure.
Surviving the loss of one validator needs at least four, each below one third (n ≥ 3f + 1, the arithmetic
[ADR-041](ADR-041-repartition-du-pouvoir-de-consensus.md) already writes down). That target is not
reachable before the next launch, and every intermediate split keeps the property that one loss halts.

## Decision

Start a **new chain from a fresh genesis**. The previous chain is abandoned **without a snapshot**: its
balances, registrations and history are not carried over, and any public text promising that a
contributor record survives a reset is withdrawn in the same change.

| Setting | Value | Why |
|---|---|---|
| Chain id | `dendra-testnet` | A new name: a transaction signed for the old chain cannot replay here, and an old node that comes back announces another network |
| Validators | **One**, on the remaining host, reinstalled before launch | Nothing to wait for: the two-thirds rule cannot deadlock on a single signer. The chain stops with that host and resumes with it |
| Origin self-bond | **600 000 DNDR** (no longer a literal in the gentx) | Leaves room to fund three partners later without one of them ever becoming indispensable |
| Pockets | Supply unchanged at 10 000 000 DNDR. The validator pocket is split: a **hot validator key** (700 000) keeps the self-bond and a working margin, a **cold delegation pocket** (2 000 000) holds the rest for future validators. A **payout account** is funded once, at genesis, from the community pocket, with 15 000 DNDR, which covers the season cap of [ADR-047](ADR-047-final-testnet-season-reward-programme.md) (1 500 DNDR since its decision 15; `final_season_rules.py::RULES` is the bound that applies); the **request generator** of the programme gets 3 000 from the same pocket, an operating assumption: its fees are a fixed volume, `requests_per_day` × the days of the season (it ends at a fixed time, ADR-047 decision 17) × the fee each request escrows, at most `BASE_FEE` + `PER_TOKEN` × (prompt tokens + `OUT_ALLOW`) udndr (`final_season_generator.py`, `client.py::quick_metered`) | The key that chooses future validators must not live on the host it would compromise |
| Cold addresses | **Community, team and delegation** pockets are addresses generated offline; only the address enters the genesis | A compromised host must not give away the largest pockets |
| VRF contributor floor | `committee_min_vrf_contributors = 1` | With one validator, a floor of 2 defers every draw for ever. **Consequence, stated publicly: the operator alone produces the randomness of every draw** until independent validators join and governance raises the floor |
| Audit rate | `audit_sample_bps = 2000` at start, raised to `5000` by governance once four jurors vote | Below a functioning jury, an audited job can only be refunded; a lower rate pays more miners on the next block |
| Unwind | `audit_unwind_blocks` ≈ one day of blocks (counted in blocks; the day is an assumption at about 5 s per block) | A retention that no jury can judge goes back to the client instead of staying held for ever ([ADR-044](ADR-044-le-silence-du-mineur-ne-se-punit-pas-il-se-rend-sans-valeur.md), gesture 2, armed) |
| Juror freshness | `juror_freshness_blocks = 500000` | The value the previous chain voted after a shorter window dropped registered miners out of eligibility |
| Dispute bond | `dispute_bond = 1000000` udndr, equal to `min_stake` | Contesting a verdict and registering an identity cost the same |
| Block cadence | `timeout_commit = "5s"` written explicitly by every node builder | The parameters sized as "about a day" assume about 5 s per block; the cadence no longer rests on an SDK default |
| Gas | Minimum gas price stays zero | Every tool would otherwise have to pay fees; spam is bounded by the programme's caps |
| Miner stake, faucet | `min_stake` 1 DNDR; the faucet gives 10 DNDR per request | Unchanged |
| Presence | Availability windows armed for **measurement only**: `avail_epoch_blocks = 288`, `avail_deadline_blocks = 144`, no slash, no on-chain payout | Feeds the presence rule of ADR-048 (only present miners are drawn) and the presence reward of ADR-047, read from the transaction index (`final_season_chain.py::presence_windows`) and paid only on a day the identity also has verified work |
| Services on the host | Validator, faucet, relay, gateway | No chat interface and no dashboards on the machine that carries the only validator |

## Consequences

- **One host is the network.** An outage halts the chain until the host restarts; losing the host means a
  new reset. No standby machine and no external alerting are part of this launch.
- **The randomness is the operator's.** Committee draws are as fair as the operator is honest. This is said
  on every public surface that mentions the draw, and nowhere is the network described as decentralised.
- **ADR-041's target of four validators is suspended, not withdrawn.** The cold delegation pocket exists so
  that it can be resumed without moving the community's funds.
- The genesis pin (`docker/GENESIS_SHA256`) is rewritten by the launcher and republished in the same
  gesture as the genesis.
