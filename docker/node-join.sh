#!/bin/sh
# Joins a PUBLIC Dendra testnet as a full NODE (syncs from the seeds + public genesis). Idempotent:
# init once, then dendrad start. To BECOME A VALIDATOR: see deploy/testnet-node/README.md (the manual
# create-validator step = stake bond, a deliberate operator action, never automatic).
set -e
HOME_DIR="${DENDRA_HOME:-/root/.dendra}"
CHAIN_ID="${CHAIN_ID:-dendra}"   # same chain-id as the operator side; the downloaded genesis is authoritative.
MONIKER="${MONIKER:-dendra-node}"
: "${GENESIS_URL:?Set GENESIS_URL to the public genesis.json provided by the operator}"
SEEDS="${SEEDS:-}"                       # node_id@host:26656 (comma-separated)
PERSISTENT_PEERS="${PERSISTENT_PEERS:-}" # same format (persistent peers)
GENESIS_SHA256="${GENESIS_SHA256:-}"     # expected sha256 (see network-info.txt) -> verifies the downloaded genesis (anti-MITM)
CFG="$HOME_DIR/config"

# ---------------------------------------------------------------------------------------------------
# FORK CHECK — on EVERY start, not only the first one.
# After an operator-side genesis reset, an uncontrolled node keeps running on the OLD chain. The
# symptoms are misleading: `catching_up: false` (true — it is not catching up, it simply has no peers
# left talking to it), a height FROZEN well above the real network, and a consistent validator set
# that belongs to a dead fork. Every on-chain read served through such a node is wrong without saying
# so, and every tx is rejected.
# Running this check only when genesis.json is absent would mean the operator-supplied SHA256 is never
# compared again after the first boot: the anti-MITM guard itself would only cover the first start.
if [ -f "$CFG/genesis.json" ] && [ -n "$GENESIS_SHA256" ]; then
  LOCAL_SHA=$(sha256sum "$CFG/genesis.json" | cut -d' ' -f1)
  if [ "$LOCAL_SHA" != "$GENESIS_SHA256" ]; then
    echo "[node] ====================================================================" >&2
    echo "[node] THE LOCAL GENESIS IS NOT THE NETWORK GENESIS." >&2
    echo "[node]   local   : $LOCAL_SHA" >&2
    echo "[node]   network : $GENESIS_SHA256" >&2
    echo "[node] Two possible causes: (a) the operator RESET the network (new genesis) and your local" >&2
    echo "[node] state belongs to the old chain; (b) a MITM on the genesis." >&2
    echo "[node] In BOTH cases, starting would be worse than stopping: the node would answer queries" >&2
    echo "[node] with the state of a dead chain, without ever signalling it." >&2
    if [ "${DENDRA_AUTO_RESET_ON_GENESIS_CHANGE:-0}" = "1" ]; then
      echo "[node] DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 -> purging local state and resyncing from scratch." >&2
      echo "[node] (keyring KEYS are preserved: only the chain state is erased)" >&2
      dendrad comet unsafe-reset-all --home "$HOME_DIR" >/dev/null 2>&1 \
        || dendrad tendermint unsafe-reset-all --home "$HOME_DIR" >/dev/null 2>&1 \
        || rm -rf "$HOME_DIR/data"
      rm -f "$CFG/genesis.json"
    else
      echo "[node] ABORTING. To start fresh on the current network:" >&2
      echo "[node]   docker compose -f deploy/testnet-node/docker-compose.yml down -v" >&2
      echo "[node]   then run deploy/join.sh again (-v discards the volume keys: re-create them)" >&2
      echo "[node] Or, keeping the keys: DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 on the next boot." >&2
      echo "[node] ====================================================================" >&2
      exit 1
    fi
  else
    echo "[node] local genesis == network genesis (sha256 verified on every boot)."
  fi
fi

if [ ! -f "$CFG/genesis.json" ]; then
  echo "[node] init $MONIKER (chain-id $CHAIN_ID)"
  dendrad init "$MONIKER" --chain-id "$CHAIN_ID" --home "$HOME_DIR" >/dev/null 2>&1 || true
  echo "[node] downloading public genesis: $GENESIS_URL"
  curl -fsSL "$GENESIS_URL" -o "$CFG/genesis.json"
  SHA=$(sha256sum "$CFG/genesis.json" | cut -d' ' -f1)
  echo "[node] genesis sha256: $SHA"
  # Anti-MITM: verification is MANDATORY by default (fail-closed). An empty GENESIS_SHA256 no longer performs a
  # silent SKIP (which would leave a public joiner unprotected). EXPLICIT opt-out for a trusted network only.
  if [ -z "$GENESIS_SHA256" ]; then
    if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
      echo "[node] WARNING: GENESIS_SHA256 empty + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> anti-MITM verification DISABLED (trusted only)."
    else
      echo "[node] FATAL: GENESIS_SHA256 is required (anti-MITM). Copy the sha from network-info.txt into GENESIS_SHA256," >&2
      echo "       or set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a TRUSTED network. ABORTING." >&2
      exit 1
    fi
  elif [ "$GENESIS_SHA256" != "$SHA" ]; then
    echo "[node] FATAL: genesis sha256 != expected ($GENESIS_SHA256) -> genesis tampered / possible MITM, ABORTING" >&2; exit 1
  else
    echo "[node] genesis sha256 VERIFIED (= expected, anti-MITM OK)."
  fi
  [ -n "$SEEDS" ] && sed -i "s|^seeds = .*|seeds = \"$SEEDS\"|" "$CFG/config.toml"
  [ -n "$PERSISTENT_PEERS" ] && sed -i "s|^persistent_peers = .*|persistent_peers = \"$PERSISTENT_PEERS\"|" "$CFG/config.toml"
  # RPC reachable from the container (P2P 26656 already listens on 0.0.0.0)
  sed -i 's|^laddr = "tcp://127.0.0.1:26657"|laddr = "tcp://0.0.0.0:26657"|' "$CFG/config.toml"

  # STATE-SYNC — not a convenience optimization here, a correction.
  # The current binary writes `audit_committee/*` (ADR-032) where an older one did not. Replaying the
  # history from genesis therefore recomputes an AppHash different from the one already signed, at
  # heights preceding the upgrade where an audit had been drawn -> panic during replay. No height
  # guard fixes this: the history genuinely contains both behaviours.
  # State-syncing at a height AFTER the upgrade means the joiner never replays those blocks.
  # DISABLED by default (empty STATESYNC_RPC) so a fresh network without snapshots still works; the
  # operator's network-info.txt supplies the values when they are needed.
  if [ -n "${STATESYNC_RPC:-}" ] && [ -n "${STATESYNC_TRUST_HEIGHT:-}" ] && [ -n "${STATESYNC_TRUST_HASH:-}" ]; then
    echo "[node] state-sync ACTIVE (trust_height=$STATESYNC_TRUST_HEIGHT) — history is NOT replayed."
    # CometBFT REQUIRES AT LEAST TWO rpc_servers: it verifies the trusted block against two sources and
    # refuses to start with a single one ("at least two rpc_servers entries is required"). Operators
    # often publish only one. Duplicating the same address is the documented workaround: it buys no
    # source independence, but it yields a state-sync that WORKS instead of a node that refuses to
    # start for no visible reason. Without this line the whole state-sync path of the kit is dead.
    _ss_rpc="$STATESYNC_RPC"
    case "$_ss_rpc" in *,*) : ;; *) _ss_rpc="$_ss_rpc,$_ss_rpc" ;; esac
    sed -i "/^\[statesync\]/,/^\[/{
      s|^enable = .*|enable = true|
      s|^rpc_servers = .*|rpc_servers = \"$_ss_rpc\"|
      s|^trust_height = .*|trust_height = $STATESYNC_TRUST_HEIGHT|
      s|^trust_hash = .*|trust_hash = \"$STATESYNC_TRUST_HASH\"|
    }" "$CFG/config.toml"
  else
    echo "[node] state-sync inactive -> replaying from genesis."
    echo "       On a network that went through a consensus-breaking change, the replay PANICS."
    echo "       Ask the operator for STATESYNC_RPC / _TRUST_HEIGHT / _TRUST_HASH if they publish them."
  fi
fi

# PEERS ARE CONFIGURATION, NOT INIT STATE — so they are re-applied on EVERY start.
# The `sed -i` calls in the init block run only when the home is created. Applying peers there alone
# means an operator who corrects SEEDS in the .env and restarts changes nothing: config.toml keeps
# whatever it holds, or nothing at all. The node then starts, announces "syncing from seeds", and has
# nobody to dial — a failure that reads like a network problem on the joiner's side.
if [ -f "$CFG/config.toml" ]; then
  [ -n "${SEEDS:-}" ] && sed -i "s|^seeds = .*|seeds = \"$SEEDS\"|" "$CFG/config.toml"
  [ -n "${PERSISTENT_PEERS:-}" ] && sed -i "s|^persistent_peers = .*|persistent_peers = \"$PERSISTENT_PEERS\"|" "$CFG/config.toml"
  _s="$(sed -n 's|^seeds = "\(.*\)"|\1|p' "$CFG/config.toml" | head -1)"
  _p="$(sed -n 's|^persistent_peers = "\(.*\)"|\1|p' "$CFG/config.toml" | head -1)"
  if [ -z "$_s" ] && [ -z "$_p" ]; then
    echo "[node] WARNING: neither seeds nor persistent_peers is set in config.toml."
    echo "[node]          This node has NOBODY to dial and will not sync. Set SEEDS in the .env"
    echo "[node]          (from network-info.txt) and restart."
  fi
fi

echo "[node] starting (syncing from seeds)..."
exec dendrad start --home "$HOME_DIR" --minimum-gas-prices "0udndr"
