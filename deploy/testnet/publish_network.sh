#!/usr/bin/env bash
# publish_network.sh — EXTRACTS and PUBLISHES the testnet network information so that THIRD PARTIES can
# JOIN (full node / validator). Without it, `deploy/testnet-node` waits for a GENESIS_URL/SEEDS that
# nothing produces, which makes a multi-operator network impossible and the real distributed throughput
# unmeasurable.
#
# Produces  deploy/testnet/published/{genesis.json, network-info.txt}  = chain-id + GENESIS_URL + SHA256
# + SEEDS (node-id@IP:26656). Joiners copy network-info.txt into deploy/testnet-node/.env.
#
# RUN IT WHERE THE STACK RUNS (the VPS, or locally if the chain runs in docker):
#   tr -d '\r' < deploy/testnet/publish_network.sh | bash -s -- <PUBLIC_IP> [GENESIS_BASE_URL]
# Idempotent (it re-extracts on every call). The node-id and genesis come from the `chain` container.
set -u
HOST="${1:?Usage: ... | bash -s -- <PUBLIC_IP_or_HOST> [GENESIS_BASE_URL]}"
BASE_URL="${2:-http://$HOST:8088}"   # URL where genesis.json will be hosted (default: a local http.server on :8088)
# No hard-coded personal path: that leaks an identity into a public repository. DENDRA_REPO wins;
# otherwise walk up from the cwd to the repository/stack root (docker-compose.yml + deploy/).
_find_repo(){ local d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "[ERR] repository root not found: export DENDRA_REPO=/path/to/repo"; exit 1; }
OUT="$REPO/deploy/testnet/published"
DC="${DENDRA_COMPOSE:-docker compose}"
command -v docker >/dev/null 2>&1 || { echo "[ERR] docker not found (run this script on the stack host)"; exit 1; }
mkdir -p "$OUT"

echo "## 1) node-id + genesis from the 'chain' container"
NID=$($DC exec -T chain dendrad tendermint show-node-id </dev/null 2>/dev/null | tr -d '\r\n ')  # </dev/null: otherwise docker exec EATS the stdin of a piped script and the script ends silently with exit 0
[ -n "$NID" ] || { echo "[ERR] node-id not found - is the 'chain' service running? ($DC ps)"; exit 1; }
$DC exec -T chain cat /root/.dendra/config/genesis.json </dev/null > "$OUT/genesis.json" 2>/dev/null
[ -s "$OUT/genesis.json" ] || { echo "[ERR] genesis.json empty or unreadable"; exit 1; }

CID=$(python3 -c "import json;print(json.load(open('$OUT/genesis.json')).get('chain_id','dendra'))" 2>/dev/null || echo dendra)
SHA=$(sha256sum "$OUT/genesis.json" | cut -d' ' -f1)
SEEDS="$NID@$HOST:26656"

# STATE-SYNC TRUST POINT. On a chain that has undergone a consensus-breaking change, replaying the
# history makes a new node PANIC (the recomputed AppHash differs from the signed one). The joiner must
# therefore start from a snapshot, which requires a height and its hash, published by the operator.
# A RECENT but already committed height is used (h-10): too recent and the snapshot does not exist yet.
SS_H=$($DC exec -T chain dendrad status --node tcp://127.0.0.1:26657 </dev/null 2>/dev/null \
  | grep -oE '"latest_block_height":"[0-9]+"' | head -1 | grep -oE '[0-9]+')
SS_RPC=""; SS_HASH=""
if [ -n "$SS_H" ] && [ "$SS_H" -gt 20 ] 2>/dev/null; then
  SS_H=$((SS_H - 10))
  # Query the CometBFT RPC DIRECTLY rather than the CLI: `dendrad query block` has changed signature
  # across SDK versions (--type=height, positional, absent) and returned empty output WITHOUT an error —
  # the trust point was then published without its hash, hence unusable, silently. The `/block?height=`
  # endpoint has been stable since Tendermint and does not depend on the CLI version.
  # `tr -d ' \t'` BEFORE the grep: indented JSON writes `"hash": "…"` with a space after the colon. The
  # pattern is always the same: the regex is written against the compact JSON one has in mind, the
  # service returns indented JSON, and the extraction returns empty WITHOUT an error.
  SS_HASH=$($DC exec -T chain curl -s "http://127.0.0.1:26657/block?height=$SS_H" </dev/null 2>/dev/null \
    | tr -d ' \t' | grep -oE '"hash":"[0-9A-Fa-f]{64}"' | head -1 | cut -d'"' -f4)
  [ -n "$SS_HASH" ] && SS_RPC="http://$HOST:26657,http://$HOST:26657"   # 2 entries = CometBFT requirement
fi
# COHERENCE: the 3 values only mean something TOGETHER. A height without a hash produces a config.toml
# that CometBFT refuses at startup, so publishing NOTHING is preferable to a truncated trust point.
if [ -z "$SS_HASH" ] || [ -z "$SS_RPC" ]; then SS_H=""; SS_HASH=""; SS_RPC=""; fi

cat > "$OUT/network-info.txt" <<EOF
# Dendra testnet - PUBLIC NETWORK INFO. Each joiner pastes this into deploy/testnet-node/.env.
CHAIN_ID=$CID
GENESIS_URL=$BASE_URL/genesis.json
GENESIS_SHA256=$SHA
SEEDS=$SEEDS
PERSISTENT_PEERS=$SEEDS
DENDRA_NODE=tcp://$HOST:26657
DENDRA_RELAY=http://$HOST:8645
FAUCET=http://$HOST:4500
# --- state-sync: speeds up the synchronization of a new node (it starts from a snapshot instead of
# replaying the whole chain). On a chain RESTARTED FROM ZERO these 3 values may stay EMPTY: replaying
# from genesis is safe, the history having been produced by a single binary. They become MANDATORY the
# day the history carries a consensus-breaking change - without them the joiner replays, reaches the
# height of the change, and PANICS.
STATESYNC_RPC=$SS_RPC
STATESYNC_TRUST_HEIGHT=$SS_H
STATESYNC_TRUST_HASH=$SS_HASH
EOF
if [ -z "$SS_HASH" ]; then
  # The severity of a missing trust point depends on the history, not on the tool. On a chain whose
  # HISTORY contains a consensus-breaking change, replaying from genesis panics, so state-sync is the
  # ONLY way to join and the absence is blocking. On a chain RESTARTED FROM ZERO the history is produced
  # by a single binary: there is no height at which behaviour changes, replay is safe again, and
  # state-sync is only a convenience. Printing the blocking wording in both cases would forbid inviting
  # anyone onto a perfectly healthy chain - a guard that outlived the condition justifying it and that
  # blocks the nominal case instead of protecting it.
  echo "[publish] state-sync trust point UNAVAILABLE (no usable snapshot)."
  echo "          On a chain RESTARTED FROM ZERO this is NOT blocking - a joiner replays from genesis"
  echo "          safely (history produced by a single binary). State-sync then only adds"
  echo "          synchronization SPEED."
  echo "          On a chain whose history carries a consensus-breaking change this IS blocking: the"
  echo "          joiner would panic - invite nobody while these 3 values are empty."
else
  echo "[publish] state-sync trust point published: height $SS_H"
fi

echo "## 2) PUBLISHED -> $OUT/"
cat "$OUT/network-info.txt"
echo "------------------------------------------------------------"
echo "## 3) HOST the genesis at GENESIS_URL. The simplest way:"
echo "      cd $OUT && nohup python3 -m http.server 8088 >/tmp/dendra-genesis-http.log 2>&1 &"
echo "   (or commit deploy/testnet/published/genesis.json, or use a CDN/S3). Open port 8088 and P2P 26656."
echo "## 4) A joiner, the simplest path:  CONFIG_URL=$BASE_URL/network-info.txt bash deploy/join.sh"
echo "   (network-info.txt now carries all 7 keys: chain/genesis/sha/seeds + NODE/RELAY/FAUCET -> one URL is enough.)"
echo "   Host network-info.txt NEXT TO the genesis (same http.server). Manual/advanced path: see the kit READMEs."
