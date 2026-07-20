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
```bash
cd deploy/testnet-node
cp .env.example .env            # 1) copy the config
nano .env                       # 2) fill in CHAIN_ID / GENESIS_URL / SEEDS
docker compose up -d --build    # 3) start (first build is long: compiles dendrad, then syncs)
```
Follow the sync: `docker compose logs -f node` (watch the block height climb). Local RPC at
`http://localhost:26657/status`.

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

## Honest notes
- **`keyring-backend test`** = unencrypted keys on disk, fine for a testnet, **NOT for real value**.
- **Build not tested in Docker CI**: if the `dendrad` build fails, check
  `./cmd/dendrad` in `chain` (see `docker/README.md`).
- **Persistent state** in the `node-data` volume; deleting it = re-sync from scratch.
- A miner (serving GPU inference) ≠ a node: to mine, see `deploy/testnet-miner/`.
