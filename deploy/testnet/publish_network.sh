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

# ── A SECOND WAY IN, AND THE KIT ALREADY CALLED ITS ABSENCE A DEFECT ────────────────────────────
# `deploy/testnet-node/docker-compose.yml` states it in full: SEEDS and PERSISTENT_PEERS are
# COMMA-SEPARATED LISTS, so naming several hosts costs nothing and is the only thing that gives a
# joining node a second way into the network. Peer discovery does not repair a single entry on its
# own -- measured on the operator's own node, `pex` asked for more addresses 118 times over a 3-hour
# window and the address book still held ONE. A node that reaches only one peer misses blocks
# whenever that link drops, and on this chain consecutive missed blocks jail a validator and slash it.
# Yet every key this script emitted named the SAME host, so the file that documents the danger was
# also the file that shipped it.
#
# THE HOST IS GIVEN, THE NODE ID IS DERIVED. A peer entry is `<node-id>@<host>:26656`. A node id that
# is retyped is a peer that resolves to nothing, and CometBFT does not complain -- it simply never
# connects, which looks exactly like a quiet network. So DENDRA_EXTRA_PEERS carries HOSTS only, and
# each id is read from that host's own RPC.
#
# A PEER THAT CANNOT BE CONFIRMED IS NOT PUBLISHED. Unreachable, or answering for another chain:
# both are said out loud here and dropped. A joiner handed a peer that never answers waits on it
# exactly as long as it would wait on a real one, so a guess costs more than an absence.
EXTRA_RPC=""
for _h in $(printf '%s' "${DENDRA_EXTRA_PEERS:-}" | tr ',' ' '); do
  _st=$(curl -s -m 8 "http://$_h:26657/status" 2>/dev/null | tr -d ' \t')
  _id=$(printf '%s' "$_st" | grep -oE '"id":"[0-9a-f]{40}"' | head -1 | cut -d'"' -f4)
  _net=$(printf '%s' "$_st" | grep -oE '"network":"[^"]*"' | head -1 | cut -d'"' -f4)
  if [ -z "$_id" ]; then
    echo "[publish] extra peer $_h NOT published: its RPC returned no node id."
    echo "          Not knowing what that host is differs from knowing it is wrong, and neither one"
    echo "          may be handed to a joiner."
  elif [ "$_net" != "$CID" ]; then
    echo "[publish] extra peer $_h NOT published: it answers for chain '$_net', this network is '$CID'."
  else
    SEEDS="$SEEDS,$_id@$_h:26656"
    EXTRA_RPC="$EXTRA_RPC,http://$_h:26657"
    echo "[publish] extra peer CONFIRMED: $_id@$_h:26656  (chain $_net, id derived from its own RPC)"
  fi
done

# STATE-SYNC TRUST POINT. On a chain that has undergone a consensus-breaking change, replaying the
# history makes a new node PANIC (the recomputed AppHash differs from the signed one). The joiner must
# therefore start from a snapshot, which requires a height and its hash, published by the operator.
# A RECENT but already committed height is used (h-10): too recent and the snapshot does not exist yet.
SS_H=$($DC exec -T chain dendrad status --node tcp://127.0.0.1:26657 </dev/null 2>/dev/null \
  | grep -oE '"latest_block_height":"[0-9]+"' | head -1 | grep -oE '[0-9]+')
SS_RPC=""; SS_HASH=""
# THE "20" THRESHOLD WAS A RETYPED NUMBER, UNRELATED TO WHAT MAKES STATE-SYNC POSSIBLE.
# A joiner using state-sync needs a SNAPSHOT to exist. The chain takes one every `snapshot-interval`
# blocks -- 500 here, set by docker/entrypoint-chain.sh -- so below height 500 there is NONE, and
# publishing the three values there advertises a path that cannot succeed.
# The interval is READ from the app.toml the chain applies; it is not retyped. That file decides, and
# a `sed` changing 500 to something else must not leave this script lying.
SS_INT=$($DC exec -T chain sh -c 'sed -n "s/^snapshot-interval[[:space:]]*=[[:space:]]*\([0-9]\{1,\}\).*/\1/p" /root/.dendra/config/app.toml | head -1' </dev/null 2>/dev/null | tr -dc 0-9)
if [ -z "$SS_INT" ] || [ "$SS_INT" = 0 ]; then
  echo "   [warn] snapshot-interval unreadable on the node -> STATESYNC_* left EMPTY."
  echo "          Not knowing whether a snapshot exists is not the same as one existing; a joiner told"
  echo "          to state-sync against a chain that has none waits for a peer that never answers."
  SS_H=""
elif [ -n "$SS_H" ] && [ "$SS_H" -ge "$SS_INT" ] 2>/dev/null; then
  # Align on the last multiple of the interval: a height we KNOW a snapshot covers, and one that
  # stays inside the retained window (`snapshot-keep-recent`).
  SS_H=$(( (SS_H / SS_INT) * SS_INT ))
  # Query the CometBFT RPC DIRECTLY rather than the CLI: `dendrad query block` has changed signature
  # across SDK versions (--type=height, positional, absent) and returned empty output WITHOUT an error —
  # the trust point was then published without its hash, hence unusable, silently. The `/block?height=`
  # endpoint has been stable since Tendermint and does not depend on the CLI version.
  # `tr -d ' \t'` BEFORE the grep: indented JSON writes `"hash": "…"` with a space after the colon. The
  # pattern is always the same: the regex is written against the compact JSON one has in mind, the
  # service returns indented JSON, and the extraction returns empty WITHOUT an error.
  SS_HASH=$($DC exec -T chain curl -s "http://127.0.0.1:26657/block?height=$SS_H" </dev/null 2>/dev/null \
    | tr -d ' \t' | grep -oE '"hash":"[0-9A-Fa-f]{64}"' | head -1 | cut -d'"' -f4)
  # CometBFT wants TWO rpc servers. With a confirmed second host it gets two DISTINCT ones; with none
  # it gets the same host twice, which satisfies the FORMAT and buys no redundancy at all -- so that
  # case is said rather than left to look like a pair.
  if [ -n "$SS_HASH" ] && [ -n "$EXTRA_RPC" ]; then
    SS_RPC="http://$HOST:26657$EXTRA_RPC"
  elif [ -n "$SS_HASH" ]; then
    SS_RPC="http://$HOST:26657,http://$HOST:26657"
    echo "   [info] STATESYNC_RPC names the SAME host twice: CometBFT requires two servers and only"
    echo "          one is configured. Two names for one machine is a single point of failure written"
    echo "          twice. Pass DENDRA_EXTRA_PEERS=<host> to publish a real second one."
  fi
else
  # THE POST-RESET CASE, AND IT HAS TO SAY SO. A relaunched chain restarts at height 1: until it
  # passes `snapshot-interval`, no snapshot exists and state-sync cannot succeed. Silence here would
  # read as "the script forgot"; it is on the contrary the only correct behaviour.
  echo "   [info] chain at height ${SS_H:-?} < snapshot-interval ($SS_INT): NO snapshot exists yet,"
  echo "          so STATESYNC_* are published EMPTY. Re-run this script once the chain passes $SS_INT"
  echo "          (~$(( SS_INT * 5 / 60 )) min at the measured ~5 s per block) to publish a usable trust point."
  SS_H=""
fi
# COHERENCE: the 3 values only mean something TOGETHER. A height without a hash produces a config.toml
# that CometBFT refuses at startup, so publishing NOTHING is preferable to a truncated trust point.
if [ -z "$SS_HASH" ] || [ -z "$SS_RPC" ]; then SS_H=""; SS_HASH=""; SS_RPC=""; fi

# The epoch is READ from the file that carries it, never retyped here: two copies of a number
# that must agree is how they stop agreeing.
# ⛔ THE EPOCH A JOINER MUST MATCH IS THE ONE THE RUNNING CHAIN WAS BUILT FROM, NOT THE ONE SITTING
# IN THIS TREE. `deploy/launch/launch_public.sh` states the distinction and acts on it: the repository
# carries the epoch of the CODE, while `<stack>/.consensus_epoch` carries the epoch of the chain that
# is actually RUNNING, written after every successful `up`. The moment the tree moves ahead of the
# deployed chain, publishing the tree's number declares an epoch nobody deployed: the joiner builds a
# binary that matches the DECLARATION, its guard prints [OK], and the two machines disagree at the
# first transaction that exercises the change. Both sides of that comparison would be the same
# declaration copied twice -- which is exactly the shape of guard this repository exists to refuse.
# The capacity endpoint is served on 127.0.0.1:8092 behind a reverse proxy, so its public URL cannot
# be derived from an IP: `deploy/join.sh` builds `https://<host>/capacity` only when DENDRA_NODE names
# a HOST, and refuses to guess from a bare IPv4. A network published by IP therefore leaves every
# joiner invisible in the capacity report unless this key is present. Adding it to the served file by
# hand does not survive: this script rewrites that file on every run. So it is emitted here, from the
# environment, where a decision made once keeps applying.
if [ -z "${DENDRA_CAPACITY_URL:-}" ]; then
  echo "[publish] WARNING: DENDRA_CAPACITY_URL is not set, so the published file will not carry it."
  echo "          A joiner behind this network then reports no capacity and never appears in the"
  echo "          public report. Re-run with DENDRA_CAPACITY_URL=https://<public-host>/capacity."
fi

TREE_EPOCH="$(head -1 "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
# KIT VERSION -- the operator surface, published so a joiner can be TOLD it is behind. Unlike the
# epoch, an unreadable value here is not fatal: the field is advisory, and omitting it costs a
# joiner a warning, not a fork. Publishing an empty one would say "unknown" where "none" is meant,
# so it is left OUT of the file entirely rather than emitted blank.
KIT_VERSION_TREE="$(head -1 "$REPO/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)"
RUN_EPOCH="$(head -1 "$REPO/.consensus_epoch" 2>/dev/null | tr -dc 0-9)"
[ -n "$TREE_EPOCH" ] || { echo "[publish] FATAL: docker/CONSENSUS_EPOCH unreadable -- a joiner could not
  tell whether its tree matches this network, and publishing an empty value would say 'unknown'
  in a field meant to say 'this one'." >&2; exit 1; }
EPOCH_PROVEN=0
if [ -n "$RUN_EPOCH" ]; then
  if [ "$RUN_EPOCH" != "$TREE_EPOCH" ]; then
    echo "[publish] FATAL: this tree is at consensus epoch $TREE_EPOCH but the chain that is RUNNING was
  deployed at epoch $RUN_EPOCH. Publishing either number misleads a joiner: $TREE_EPOCH describes code
  nobody deployed, $RUN_EPOCH describes a chain this tree can no longer reproduce. Redeploy the stack
  (deploy/launch/launch_public.sh, which records the epoch after a successful up), or decide the
  compatibility explicitly and write it into $REPO/.consensus_epoch before publishing." >&2; exit 1
  fi
  EPOCH="$RUN_EPOCH"; EPOCH_PROVEN=1
else
  # No marker: this stack was never brought up by the launcher that records one. The tree's number is
  # the best available, and it is NOT a proof -- so it is published as such rather than silently.
  EPOCH="$TREE_EPOCH"
  echo "[publish] WARNING: no .consensus_epoch marker beside the stack -- publishing the TREE epoch"
  echo "          ($EPOCH), which says what this code is, not what the running chain was built from."
  echo "          A joiner comparing it gets a match on a declaration nobody measured."
fi
# ⛔ NO BACKTICK INSIDE THIS HEREDOC. Its delimiter is not quoted -- it cannot be, the values come
# from variables -- so the shell performs every substitution it finds. A decorative backtick therefore
# RUNS what it surrounds and pastes the output into the published file, between the keys. A joiner
# sourcing that file as .env then breaks on those lines. The text carries none; the twin bench checks it.
cat > "$OUT/network-info.txt" <<EOF
# Dendra testnet - PUBLIC NETWORK INFO. Each joiner pastes this into deploy/testnet-node/.env.
CHAIN_ID=$CID
# CONSENSUS EPOCH OF THE RUNNING NETWORK. The joining kit BUILDS the node from the tree it
# cloned, so a tree one epoch away from this network produces a binary that takes different
# state transitions -- a fork that stays dormant until the first transaction that exercises the
# change, and then looks like a sync failure. The chain itself cannot answer this: abci_info
# carries no app_version here. So the network DECLARES it, next to everything else a joiner
# already fetches, and deploy/join.sh compares it with docker/CONSENSUS_EPOCH in its tree.
CONSENSUS_EPOCH=$EPOCH
CONSENSUS_EPOCH_PROVEN=$EPOCH_PROVEN
${KIT_VERSION_TREE:+KIT_VERSION=$KIT_VERSION_TREE}
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
# Written AFTER the heredoc, never interpolated into it: a backslash-n inside a variable is not a
# newline to a heredoc -- it comes out literally and glues two keys onto the same line.
[ -n "${DENDRA_CAPACITY_URL:-}" ] && printf 'DENDRA_CAPACITY_URL=%s\n' "$DENDRA_CAPACITY_URL" >> "$OUT/network-info.txt"
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
