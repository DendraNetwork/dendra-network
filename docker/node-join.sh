#!/bin/sh
# Joins a public Dendra testnet as a full node (syncs from the seeds and the public genesis). Idempotent:
# init once, then dendrad start. To become a validator, see deploy/testnet-node/README.md (the manual
# create-validator step, a stake bond: a deliberate operator action, never automatic).
set -e
HOME_DIR="${DENDRA_HOME:-/root/.dendra}"
# The chain id is read, never defaulted (ADR-048 item 8). A default such as `dendra`, the name of a
# previous chain, would init a home under a name the network no longer runs; join.sh writes the id into
# the kit .env from the network file.
CHAIN_ID="${CHAIN_ID:-}"
[ -n "$CHAIN_ID" ] || { echo "[node] FATAL: CHAIN_ID is not set in the kit .env (join.sh writes it from network-info.txt)."; exit 1; }
MONIKER="${MONIKER:-dendra-node}"
: "${GENESIS_URL:?Set GENESIS_URL to the public genesis.json provided by the operator}"
SEEDS="${SEEDS:-}"                       # node_id@host:26656 (comma-separated)
PERSISTENT_PEERS="${PERSISTENT_PEERS:-}" # same format (persistent peers)
GENESIS_SHA256="${GENESIS_SHA256:-}"     # expected sha256 (see network-info.txt) -> verifies the downloaded genesis (anti-MITM)
CFG="$HOME_DIR/config"

# ---------------------------------------------------------------------------------------------------
# Fork check, on every start, not only the first one.
# After an operator-side genesis reset, a node left alone keeps running on the old chain. The symptoms
# are misleading: `catching_up: false` (it is not catching up, it simply has no peers left talking to
# it), a height frozen well above the real network, and a consistent validator set that belongs to a
# dead fork. Every on-chain read served through such a node is silently wrong, and every tx is rejected.
# Running this check only when genesis.json is absent would mean the operator-supplied SHA256 is never
# compared again after the first boot.
if [ -f "$CFG/genesis.json" ] && [ -n "$GENESIS_SHA256" ]; then
  LOCAL_SHA=$(sha256sum "$CFG/genesis.json" | cut -d' ' -f1)
  if [ "$LOCAL_SHA" != "$GENESIS_SHA256" ]; then
    echo "[node] ====================================================================" >&2
    echo "[node] The local genesis is not the network genesis." >&2
    echo "[node]   local   : $LOCAL_SHA" >&2
    echo "[node]   network : $GENESIS_SHA256" >&2
    echo "[node] Two possible causes: (a) the operator reset the network (new genesis) and your local" >&2
    echo "[node] state belongs to the old chain; (b) a MITM on the genesis." >&2
    echo "[node] In both cases, starting would be worse than stopping: the node would answer queries" >&2
    echo "[node] with the state of a dead chain, without ever signalling it." >&2
    if [ "${DENDRA_AUTO_RESET_ON_GENESIS_CHANGE:-0}" = "1" ]; then
      echo "[node] DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 -> purging local state and resyncing from scratch." >&2
      # The reset keeps the keys, but it is not harmless. `unsafe-reset-all` also resets
      # `priv_validator_state.json`, the file that stops this validator from signing a height it has
      # already signed. On a bonded node that erases the protection against double signing, and a
      # double signature is tombstoned: permanent, with no unjail. This flag is read from the kit .env
      # on every boot, so it is a standing policy, not a one-off action.
      # Neither the keyring nor the validator key decides the risk: a remote signer or a non-test
      # keyring backend leaves no keyring directory, and `dendrad init` writes priv_validator_key.json
      # on every home, validator or not. What creates the risk is having signed, which is recorded in
      # data/priv_validator_state.json. Three answers, and the two that are not "never signed" both
      # refuse: a state that cannot be read does not count as zero.
      _PVS="$HOME_DIR/data/priv_validator_state.json"
      if [ -s "$_PVS" ]; then
        _H="$(sed -n 's/.*"height"[[:space:]]*:[[:space:]]*"\{0,1\}\([0-9]\{1,\}\).*/\1/p' "$_PVS" | head -1)"
        [ -n "$_H" ] || _H=unknown
      else
        _H=unknown   # absent or empty: it should exist since `dendrad init` writes it
      fi
      if [ "$_H" = unknown ] || [ "$_H" -gt 0 ] 2>/dev/null; then
        echo "[node] Refusing the automatic reset: this validator key has already signed (height" >&2
        echo "[node] ${_H}). unsafe-reset-all would also reset priv_validator_state.json, the file" >&2
        echo "[node] that stops this validator from signing a height it has already signed -- and a" >&2
        echo "[node] double signature is tombstoned: permanent, with no unjail." >&2
        echo "[node] Back the keys up, then reset by hand." >&2
        echo "[node] DENDRA_AUTO_RESET_ON_GENESIS_CHANGE is a standing policy, not a one-off." >&2
        exit 1
      fi
      echo "[node] (the keyring keys are kept; priv_validator_state.json is reset, which is acceptable" >&2
      echo "[node]  here because this key has signed nothing (height 0), so it cannot double-sign)" >&2
      dendrad comet unsafe-reset-all --home "$HOME_DIR" >/dev/null 2>&1 \
        || dendrad tendermint unsafe-reset-all --home "$HOME_DIR" >/dev/null 2>&1 \
        || rm -rf "$HOME_DIR/data"
      rm -f "$CFG/genesis.json"
    else
      echo "[node] aborting. To start fresh on the current network:" >&2
      echo "[node]   docker compose -f deploy/testnet-node/docker-compose.yml down -v" >&2
      # The keys cannot be re-created for a bonded validator: this volume holds keyring-test/, the operator
      # key of this identity, and an undelegation is a transaction signed by that key, so a bonded stake
      # whose key is gone cannot be recovered by anyone.
      echo "[node]   then run deploy/join.sh again." >&2
      echo "[node]   -v deletes the volume, which holds priv_validator_key.json, vrf_key and keyring-test/." >&2
      echo "[node]     If this node has bonded stake, that keyring is the only thing that can undelegate it:" >&2
      echo "[node]     copy the keys out before using -v. They cannot be re-created, only restored." >&2
      echo "[node] Or, keeping the keys: DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 on the next boot." >&2
      echo "[node] ====================================================================" >&2
      exit 1
    fi
  else
    echo "[node] local genesis == network genesis (sha256 verified on every boot)."
  fi
elif [ -f "$CFG/genesis.json" ]; then
  # Without a sha there is nothing to compare, and staying silent would read as an all-clear.
  # Further down, this file refuses an empty value on the first boot and names the opt-out; a node whose
  # sha went missing afterwards would otherwise keep restarting on an unchecked genesis with a normal
  # startup log.
  echo "[node] GENESIS_SHA256 is empty: the boot-time fork check is disabled for this start." >&2
  echo "[node]   Nothing here compared the local genesis to the network's, so a node left on a dead" >&2
  echo "[node]   fork would start silently and answer queries with its state." >&2
  if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" != "1" ]; then
    echo "[node]   Set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 to accept that deliberately, or provide" >&2
    echo "[node]   GENESIS_SHA256 (deploy/join.sh reads it from network-info.txt for you)." >&2
    exit 1
  fi
  echo "[node]   DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> accepted deliberately, continuing." >&2
fi

if [ ! -f "$CFG/genesis.json" ]; then
  echo "[node] init $MONIKER (chain-id $CHAIN_ID)"
  dendrad init "$MONIKER" --chain-id "$CHAIN_ID" --home "$HOME_DIR" >/dev/null 2>&1 || true
  echo "[node] downloading public genesis: $GENESIS_URL"
  # The file is verified before it is put in place, and that order is the guarantee. Writing it to its
  # final path first would defeat the refusal below: it exits 1, but the file would stay in the volume,
  # the container restarts (`restart: unless-stopped`), genesis.json then exists, this block is skipped
  # (and the boot-time check too when the sha is empty), and the node syncs on an unchecked genesis.
  # So the file is downloaded beside it, verified, and only then moved into place.
  TMPG="$CFG/.genesis.download"
  curl -fsSL "$GENESIS_URL" -o "$TMPG"
  SHA=$(sha256sum "$TMPG" | cut -d' ' -f1)
  echo "[node] genesis sha256: $SHA"
  # Anti-MITM: verification is mandatory by default (fail-closed). An empty GENESIS_SHA256 is not silently
  # skipped, which would leave a public joiner unprotected; the explicit opt-out is for a trusted network only.
  # From here on, every error exit must remove the file, or the next boot would skip this check.
  trap 'rm -f "$TMPG"' EXIT
  if [ -z "$GENESIS_SHA256" ]; then
    if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
      echo "[node] WARNING: GENESIS_SHA256 empty + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> anti-MITM verification disabled (trusted networks only)."
    else
      echo "[node] FATAL: GENESIS_SHA256 is required (anti-MITM). Copy the sha from network-info.txt into GENESIS_SHA256," >&2
      echo "       or set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a trusted network. Aborting." >&2
      exit 1
    fi
  elif [ "$GENESIS_SHA256" != "$SHA" ]; then
    echo "[node] FATAL: genesis sha256 != expected ($GENESIS_SHA256) -> genesis tampered / possible MITM, aborting" >&2; exit 1
  else
    echo "[node] genesis sha256 verified (matches the expected value, anti-MITM OK)."
  fi
  # Installed only now: every error exit above leaves the volume without genesis.json, so the next boot
  # goes through this check again instead of skipping it. The trap is cleared here, otherwise it would
  # delete the file that was just installed.
  mv -f "$TMPG" "$CFG/genesis.json"
  trap - EXIT
  [ -n "$SEEDS" ] && sed -i "s|^seeds = .*|seeds = \"$SEEDS\"|" "$CFG/config.toml"
  [ -n "$PERSISTENT_PEERS" ] && sed -i "s|^persistent_peers = .*|persistent_peers = \"$PERSISTENT_PEERS\"|" "$CFG/config.toml"
  # RPC reachable from the container (P2P 26656 already listens on 0.0.0.0)
  sed -i 's|^laddr = "tcp://127.0.0.1:26657"|laddr = "tcp://0.0.0.0:26657"|' "$CFG/config.toml"

  # State sync is a correction here, not a convenience.
  # The current binary writes `audit_committee/*` (ADR-032) where an older one did not. Replaying the
  # history from genesis therefore recomputes an AppHash different from the one already signed, at
  # heights before the upgrade where an audit had been drawn, and the replay panics. No height guard
  # fixes this: the history really contains both behaviours. State-syncing at a height after the
  # upgrade means the joiner never replays those blocks.
  # Off by default (empty STATESYNC_RPC) so that a fresh network without snapshots still works.
  # A trust point belongs to one genesis. STATESYNC_GENESIS_SHA256 names the genesis it was derived under
  # (deploy/join.sh writes it beside the three values), and it is applied only when that is the genesis
  # verified above. This block also runs when DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 recreates the home after
  # a network reset: without the binding, the trust point of the previous network, still in the kit .env,
  # would be written into the new home's config.
  _ss_apply=0
  if [ -n "${STATESYNC_RPC:-}" ] && [ -n "${STATESYNC_TRUST_HEIGHT:-}" ] && [ -n "${STATESYNC_TRUST_HASH:-}" ]; then
    if [ -n "$GENESIS_SHA256" ] && [ "${STATESYNC_GENESIS_SHA256:-}" = "$GENESIS_SHA256" ]; then
      _ss_apply=1
    else
      echo "[node] a state-sync trust point is set, but it is bound to genesis '${STATESYNC_GENESIS_SHA256:-<none>}'"
      echo "[node]   and this home is created for '${GENESIS_SHA256:-<none>}' -> not applied (STATESYNC_GENESIS_SHA256)."
    fi
  fi
  if [ "$_ss_apply" = 1 ]; then
    echo "[node] state sync active (trust_height=$STATESYNC_TRUST_HEIGHT): the history is not replayed."
    # CometBFT requires at least two rpc_servers: it verifies the trusted block against two sources and
    # refuses to start with a single one ("at least two rpc_servers entries is required"). Operators
    # often publish only one. Repeating the same address is the documented workaround: it adds no
    # source independence, but state sync works instead of the node refusing to start.
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
    echo "       On a network that went through a consensus-breaking change, the replay panics."
    echo "       deploy/join.sh derives STATESYNC_RPC / _TRUST_HEIGHT / _TRUST_HASH for a new node, over TLS,"
    echo "       from the servers docker/STATESYNC_RPC names in the clone (--replay asks for this replay)."
  fi
fi

# Peers are configuration, not init state, so they are re-applied on every start.
# The `sed -i` calls in the init block run only when the home is created, so an operator who corrects
# SEEDS in the .env and restarts would change nothing: the node starts, announces "syncing from seeds",
# and has nobody to dial.
if [ -f "$CFG/config.toml" ]; then
  [ -n "${SEEDS:-}" ] && sed -i "s|^seeds = .*|seeds = \"$SEEDS\"|" "$CFG/config.toml"
  [ -n "${PERSISTENT_PEERS:-}" ] && sed -i "s|^persistent_peers = .*|persistent_peers = \"$PERSISTENT_PEERS\"|" "$CFG/config.toml"
  _s="$(sed -n 's|^seeds = "\(.*\)"|\1|p' "$CFG/config.toml" | head -1)"
  _p="$(sed -n 's|^persistent_peers = "\(.*\)"|\1|p' "$CFG/config.toml" | head -1)"
  if [ -z "$_s" ] && [ -z "$_p" ]; then
    echo "[node] WARNING: neither seeds nor persistent_peers is set in config.toml."
    echo "[node]          This node has nobody to dial and will not sync. Set SEEDS in the .env"
    echo "[node]          (from network-info.txt) and restart."
  fi
  # Block cadence, written explicitly (ADR-046) as on the genesis node: the SDK otherwise substitutes
  # its own 5 s for the CometBFT default, and a validator joining later must not run a different
  # cadence by omission. Read back, because a sed that matched nothing fails silently.
  # cadence: live
  sed -i 's|^timeout_commit = .*|timeout_commit = "5s"|' "$CFG/config.toml"
  grep -q '^timeout_commit = "5s"' "$CFG/config.toml" || { echo "[node] FATAL: timeout_commit could not be written to $CFG/config.toml"; exit 1; }
fi

# Being reachable is not the same as publishing the port. A node advertises `tcp://0.0.0.0:26656` unless
# an external address is given; that means "I listen on every local interface", which nobody outside can
# dial. Measured on this network: every node dialled out to the same host and accepted nobody, so the
# topology was a star with no lateral link, while each operator saw a healthy node. A node with one peer
# has one source for every consensus message: a missed proposal has nowhere else to come from.
# Three states, and the empty one is the default because it is correct behind NAT: a wildcard is honest,
# a wrong address is not. Peers store what you advertise and re-gossip it, so a private or loopback
# address does not just fail for you, it pollutes other operators' address books.
# Carrier-grade NAT is invisible from the inside. Mobile, fibre and satellite ISPs hand out 100.64.0.0/10
# (RFC 6598, "shared address space"): `ip addr` shows an address that looks routable and is not RFC 1918,
# but it is shared between many subscribers, so nobody outside can dial it and no port forwarding on the
# customer's router changes that. Advertising it does the same damage as advertising 10.x.
# The range is 100.64 through 100.127 only: 100.63.x and 100.128.x are ordinary public addresses, so a
# plain `100.*` would refuse operators who are reachable.
_EA="${DENDRA_EXTERNAL_ADDRESS:-}"
P2P_ARGS=""
if [ -n "$_EA" ]; then
  case "$_EA" in
    *:*) : ;;
    *) echo "[node] FATAL: DENDRA_EXTERNAL_ADDRESS='$_EA' must be host:port (the port peers dial)."; exit 1 ;;
  esac
  _EH="${_EA%:*}"; _EP="${_EA##*:}"
  case "$_EP" in
    ''|*[!0-9]*)
      echo "[node] FATAL: DENDRA_EXTERNAL_ADDRESS='$_EA' has no numeric port."; exit 1 ;;
  esac
  case "$_EH" in
    ''|0.0.0.0|127.*|localhost|10.*|192.168.*|169.254.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*|'::'|'[::]'|'[::1]')
      echo "[node] FATAL: DENDRA_EXTERNAL_ADDRESS='$_EA' is a wildcard, a loopback, a private or a"
      echo "[node]        carrier-NAT (100.64.0.0/10) address, so peers cannot dial it."
      echo "[node]        Peers store and re-gossip what you advertise, so this is worse than advertising"
      echo "[node]        nothing. Leave it empty unless the port is genuinely reachable from outside."
      exit 1 ;;
  esac
  P2P_ARGS="--p2p.external-address $_EA"
  echo "[node] advertising $_EA to peers — they can dial this node."
else
  echo "[node] no external address set: this node dials out but accepts nobody, so it is not a"
  echo "[node]        redundancy option for anyone else. Set DENDRA_EXTERNAL_ADDRESS once the P2P"
  echo "[node]        port is forwarded."
fi

echo "[node] starting (syncing from seeds)..."
# The REST API is on because the desktop application reads the chain through it (127.0.0.1:1317 on the
# host). Without these flags the SDK leaves it disabled. It listens on every interface inside the
# container only: what reaches the host is decided by the port mapping in
# deploy/testnet-node/docker-compose.yml, which publishes it on loopback. It answers queries and relays
# transactions that are already signed; it holds no key.
# Unquoted on purpose: empty must expand to no argument, not to an empty one.
# shellcheck disable=SC2086
exec dendrad start --home "$HOME_DIR" --minimum-gas-prices "0udndr" --api.enable --api.address tcp://0.0.0.0:1317 $P2P_ARGS
