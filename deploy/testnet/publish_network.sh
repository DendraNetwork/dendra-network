#!/usr/bin/env bash
# publish_network.sh -- extracts and publishes the testnet network information so that third parties can
# join (full node or validator). Without it, `deploy/testnet-node` waits for a GENESIS_URL and SEEDS that
# nothing produces, which makes a multi-operator network impossible.
#
# Produces  deploy/testnet/published/{genesis.json, network-info.txt}  = chain-id + GENESIS_URL + SHA256
# + SEEDS (node-id@IP:26656). Joiners copy network-info.txt into deploy/testnet-node/.env.
#
# Run it where the stack runs (the VPS, or locally if the chain runs in docker):
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
NID=$($DC exec -T chain dendrad tendermint show-node-id </dev/null 2>/dev/null | tr -d '\r\n ')  # </dev/null: otherwise docker exec consumes the stdin of a piped script and the script ends silently with exit 0
[ -n "$NID" ] || { echo "[ERR] node-id not found - is the 'chain' service running? ($DC ps)"; exit 1; }
$DC exec -T chain cat /root/.dendra/config/genesis.json </dev/null > "$OUT/genesis.json" 2>/dev/null
[ -s "$OUT/genesis.json" ] || { echo "[ERR] genesis.json empty or unreadable"; exit 1; }

# The published chain id is the genesis's own, or nothing is published (ADR-048 item 8). A fallback name
# would announce an unreadable genesis under a previous chain's name, and every joiner would build a node
# for it.
CID=$(python3 -c "import json;print(json.load(open('$OUT/genesis.json'))['chain_id'])" 2>/dev/null | tr -dc 'A-Za-z0-9_.-')
[ -n "$CID" ] || { echo "[ERR] chain_id unreadable from the genesis: nothing is published under a guessed name"; exit 1; }
SHA=$(sha256sum "$OUT/genesis.json" | cut -d' ' -f1)
SEEDS="$NID@$HOST:26656"

# --- A second way into the network ---
# As `deploy/testnet-node/docker-compose.yml` explains, SEEDS and PERSISTENT_PEERS are comma-separated
# lists, so naming several hosts costs nothing, and it is the only thing that gives a joining node a second
# way into the network. Peer discovery does not repair a single entry on its own: measured on the
# operator's own node, `pex` asked for more addresses 118 times over a 3-hour window and the address book
# still held one. A node that reaches only one peer misses blocks whenever that link drops, and
# consecutive missed blocks jail a validator and slash it.
#
# The host is given, the node id is derived. A peer entry is `<node-id>@<host>:26656`. A retyped node id
# is a peer that resolves to nothing, and CometBFT does not complain: it never connects, which looks like
# a quiet network. So DENDRA_EXTRA_PEERS carries hosts only, and each id is read from that host's own RPC.
#
# A peer that cannot be confirmed is not published. Unreachable, or answering for another chain: both are
# reported here and dropped. A joiner handed a peer that never answers waits on it as long as it would wait
# on a real one, so a guess costs more than an absence.
EXTRA_RPC=""
for _h in $(printf '%s' "${DENDRA_EXTRA_PEERS:-}" | tr ',' ' '); do
  _st=$(curl -s -m 8 "http://$_h:26657/status" 2>/dev/null | tr -d ' \t')
  _id=$(printf '%s' "$_st" | grep -oE '"id":"[0-9a-f]{40}"' | head -1 | cut -d'"' -f4)
  _net=$(printf '%s' "$_st" | grep -oE '"network":"[^"]*"' | head -1 | cut -d'"' -f4)
  if [ -z "$_id" ]; then
    echo "[publish] extra peer $_h not published: its RPC returned no node id."
    echo "          Not knowing what that host is differs from knowing it is wrong, and neither one"
    echo "          may be handed to a joiner."
  elif [ "$_net" != "$CID" ]; then
    echo "[publish] extra peer $_h not published: it answers for chain '$_net', this network is '$CID'."
  else
    SEEDS="$SEEDS,$_id@$_h:26656"
    EXTRA_RPC="$EXTRA_RPC,http://$_h:26657"
    echo "[publish] extra peer confirmed: $_id@$_h:26656  (chain $_net, id derived from its own RPC)"
  fi
done

# State-sync trust point. On a chain that has undergone a consensus-breaking change, replaying the history
# makes a new node panic (the recomputed AppHash differs from the signed one). The joiner must therefore
# start from a snapshot, which requires a height and its hash, published by the operator.
# A recent but already committed height is used: too recent and the snapshot does not exist yet.
SS_H=$($DC exec -T chain dendrad status --node tcp://127.0.0.1:26657 </dev/null 2>/dev/null \
  | grep -oE '"latest_block_height":"[0-9]+"' | head -1 | grep -oE '[0-9]+')
SS_RPC=""; SS_HASH=""
# A joiner using state sync needs a snapshot to exist. The chain takes one every `snapshot-interval`
# blocks (500 here, set by docker/entrypoint-chain.sh), so below that height there is none, and publishing
# the three values would advertise a path that cannot succeed.
# The interval is read from the app.toml the chain applies, not retyped, so a changed value cannot leave
# this script wrong.
SS_INT=$($DC exec -T chain sh -c 'sed -n "s/^snapshot-interval[[:space:]]*=[[:space:]]*\([0-9]\{1,\}\).*/\1/p" /root/.dendra/config/app.toml | head -1' </dev/null 2>/dev/null | tr -dc 0-9)
if [ -z "$SS_INT" ] || [ "$SS_INT" = 0 ]; then
  echo "   [warn] snapshot-interval unreadable on the node -> STATESYNC_* left empty."
  echo "          Not knowing whether a snapshot exists is not the same as one existing; a joiner told"
  echo "          to state-sync against a chain that has none waits for a peer that never answers."
  SS_H=""
elif [ -n "$SS_H" ] && [ "$SS_H" -ge "$SS_INT" ] 2>/dev/null; then
  # Align on the last multiple of the interval: a height a snapshot is known to cover, and one that
  # stays inside the retained window (`snapshot-keep-recent`).
  SS_H=$(( (SS_H / SS_INT) * SS_INT ))
  # Query the CometBFT RPC directly rather than the CLI: `dendrad query block` has changed signature
  # across SDK versions (--type=height, positional, absent) and returned empty output without an error,
  # so the trust point was published without its hash. The `/block?height=` endpoint has been stable for
  # a long time and does not depend on the CLI version.
  # `tr -d ' \t'` before the grep: indented JSON writes `"hash": "..."` with a space after the colon, and
  # a regex written for compact JSON would silently return nothing.
  SS_HASH=$($DC exec -T chain curl -s "http://127.0.0.1:26657/block?height=$SS_H" </dev/null 2>/dev/null \
    | tr -d ' \t' | grep -oE '"hash":"[0-9A-Fa-f]{64}"' | head -1 | cut -d'"' -f4)
  # CometBFT wants two rpc servers. With a confirmed second host it gets two distinct ones; with none
  # it gets the same host twice, which satisfies the format and buys no redundancy at all, so that
  # case is reported rather than left to look like a pair.
  if [ -n "$SS_HASH" ] && [ -n "$EXTRA_RPC" ]; then
    SS_RPC="http://$HOST:26657$EXTRA_RPC"
  elif [ -n "$SS_HASH" ]; then
    SS_RPC="http://$HOST:26657,http://$HOST:26657"
    echo "   [info] STATESYNC_RPC names the same host twice: CometBFT requires two servers and only"
    echo "          one is configured. Two names for one machine is a single point of failure written"
    echo "          twice. Pass DENDRA_EXTRA_PEERS=<host> to publish a real second one."
  fi
else
  # The post-reset case, reported explicitly. A relaunched chain restarts at height 1: until it passes
  # `snapshot-interval`, no snapshot exists and state sync cannot succeed. Publishing nothing is the
  # correct behaviour here, and the message says so.
  echo "   [info] chain at height ${SS_H:-?} < snapshot-interval ($SS_INT): no snapshot exists yet,"
  echo "          so STATESYNC_* are published empty. Re-run this script once the chain passes $SS_INT"
  echo "          (~$(( SS_INT * 5 / 60 )) min at the measured ~5 s per block) to publish a usable trust point."
  SS_H=""
fi
# The 3 values only mean something together. A height without a hash produces a config.toml that
# CometBFT refuses at startup, so publishing nothing is better than a truncated trust point.
if [ -z "$SS_HASH" ] || [ -z "$SS_RPC" ]; then SS_H=""; SS_HASH=""; SS_RPC=""; fi

# The epoch is read from the file that carries it, never retyped here.
# The epoch a joiner must match is the one the running chain was built from, not the one in this tree.
# `deploy/launch/launch_public.sh` makes the same distinction: the repository carries the epoch of the
# code, while `<stack>/.consensus_epoch` carries the epoch of the chain actually running, written after
# every successful `up`. Once the tree moves ahead of the deployed chain, publishing the tree's number
# would declare an epoch nobody deployed: the joiner builds a binary that matches the declaration, its
# check passes, and the two machines disagree at the first transaction that exercises the change.
# The capacity endpoint is served on 127.0.0.1:8092 behind a reverse proxy, so its public URL cannot be
# derived from an IP: `deploy/join.sh` builds `https://<host>/capacity` only when DENDRA_NODE names a host,
# and refuses to guess from a bare IPv4. A network published by IP therefore leaves every joiner out of the
# capacity report unless this key is present. This script rewrites the served file on every run, so the key
# is emitted here, from the environment, rather than added by hand.
if [ -z "${DENDRA_CAPACITY_URL:-}" ]; then
  echo "[publish] WARNING: DENDRA_CAPACITY_URL is not set, so the published file will not carry it."
  echo "          A joiner behind this network then reports no capacity and never appears in the"
  echo "          public report. Re-run with DENDRA_CAPACITY_URL=https://<public-host>/capacity."
fi

TREE_EPOCH="$(head -1 "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
# The kit version of this tree: compared with the release's below, never published (see "Kit version").
KIT_VERSION_TREE="$(head -1 "$REPO/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)"
RUN_EPOCH="$(head -1 "$REPO/.consensus_epoch" 2>/dev/null | tr -dc 0-9)"
[ -n "$TREE_EPOCH" ] || { echo "[publish] FATAL: docker/CONSENSUS_EPOCH unreadable -- a joiner could not
  tell whether its tree matches this network, and publishing an empty value would say 'unknown'
  in a field meant to say 'this one'." >&2; exit 1; }
EPOCH_PROVEN=0
if [ -n "$RUN_EPOCH" ]; then
  if [ "$RUN_EPOCH" != "$TREE_EPOCH" ]; then
    echo "[publish] FATAL: this tree is at consensus epoch $TREE_EPOCH but the chain that is running was
  deployed at epoch $RUN_EPOCH. Publishing either number misleads a joiner: $TREE_EPOCH describes code
  nobody deployed, $RUN_EPOCH describes a chain this tree can no longer reproduce. Redeploy the stack
  (deploy/launch/launch_public.sh, which records the epoch after a successful up), or decide the
  compatibility explicitly and write it into $REPO/.consensus_epoch before publishing." >&2; exit 1
  fi
  EPOCH="$RUN_EPOCH"; EPOCH_PROVEN=1
else
  # No marker: this stack was never brought up by the launcher that records one. The tree's number is
  # the best available, and it is not a proof, so it is published with a warning.
  EPOCH="$TREE_EPOCH"
  echo "[publish] WARNING: no .consensus_epoch marker beside the stack -- publishing the tree epoch"
  echo "          ($EPOCH), which says what this code is, not what the running chain was built from."
  echo "          A joiner comparing it gets a match on a declaration nobody measured."
fi

# --- Kit version: it follows the release, not this tree ---
# The operator surface, published so a joiner can be told it is behind (deploy/join.sh::verify_kit_version).
# A joiner updates by cloning a release (deploy/install.sh --ref, the HiveOS package) or the default branch,
# so the number must name a kit that exists there. It is read from docker/KIT_VERSION at the latest release
# tag of the repository joiners clone: DENDRA_REPO_URL, the variable deploy/install.sh clones from. The tree
# this script runs from is a copy kept on the stack host; its number describes that copy and is only
# compared. Re-running this script after a release is what makes KIT_VERSION follow it.
# A release's number is published only once the default branch pins a miner image built for it, as
# deploy/join.sh::miner_image_pin judges a pin (sourced from this stack's kit, never retyped here). Before
# that, every kit told it is behind would rebuild the miner image from source on its own machine.
# DENDRA_KIT_WITHOUT_IMAGE_REASON="<why>" publishes it anyway, and the reason is printed with it.
# Three answers, never two: a number read from the release with its image pinned -> published. No release
# yet, or anything unreadable (no git, a silent remote, an absent field, an unloadable join.sh) -> not
# published, and reported: a joiner is then told its kit version is not checked. An empty KIT_VERSION= would
# say "unknown" where the key is meant to name a version, so the key is left out.
KIT_URL="${DENDRA_REPO_URL:-https://github.com/DendraNetwork/dendra-network.git}"
KIT_VERSION_PUB=""; KIT_TAG=""; KIT_WHY=""
_git_net(){ if command -v timeout >/dev/null 2>&1; then GIT_TERMINAL_PROMPT=0 timeout 60 git "$@"
            else GIT_TERMINAL_PROMPT=0 git "$@"; fi; }
_kit_from_release(){
  local tmp lsr rc k pin st mk f
  command -v git >/dev/null 2>&1 || { KIT_WHY="git is not installed on this host"; return 0; }
  lsr="$(_git_net ls-remote --tags "$KIT_URL" 2>/dev/null)"; rc=$?
  [ "$rc" = 0 ] || { KIT_WHY="$KIT_URL did not answer (git ls-remote exit $rc)"; return 0; }
  KIT_TAG="$(printf '%s\n' "$lsr" | awk 'NF >= 2 { print $2 }' | sed -n 's#^refs/tags/##p' | grep -v '[{][}]$' \
    | grep -E '^v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$' | grep -vE '^v0[.]1[.]' | sort -V | tail -1)"
  [ -n "$KIT_TAG" ] || { KIT_WHY="$KIT_URL carries no release tag yet"; return 0; }
  tmp="$(mktemp -d)" || { KIT_WHY="no temporary directory to read $KIT_TAG into"; return 0; }
  # --git-dir everywhere: `git -C` on a directory that is not a repository climbs to the parent one.
  k=""
  if git init -q "$tmp" 2>/dev/null \
     && _git_net --git-dir="$tmp/.git" fetch -q --no-tags "$KIT_URL" "refs/tags/$KIT_TAG" 2>/dev/null; then
    k="$(git --git-dir="$tmp/.git" show FETCH_HEAD:docker/KIT_VERSION 2>/dev/null | head -1 | tr -dc 0-9)"
  fi
  if [ -z "$k" ]; then rm -rf "$tmp"; KIT_WHY="docker/KIT_VERSION could not be read at $KIT_TAG"; return 0; fi
  # The default branch's pin, judged by deploy/join.sh::miner_image_pin of this stack's tree, applied to the
  # default branch's files. A stack whose join.sh differs from the default branch's may reach a different
  # verdict than a joiner's own join.sh would; the stack's tree is the one to keep current.
  pin=""
  if _git_net --git-dir="$tmp/.git" fetch -q --no-tags "$KIT_URL" "refs/heads/main" 2>/dev/null; then
    mkdir -p "$tmp/main/docker" "$tmp/main/deploy/testnet-node"
    for f in MINER_IMAGE KIT_VERSION CONSENSUS_EPOCH; do
      git --git-dir="$tmp/.git" show "FETCH_HEAD:docker/$f" > "$tmp/main/docker/$f" 2>/dev/null || rm -f "$tmp/main/docker/$f"
    done
    pin="$(set --; export DENDRA_JOIN_LIB=1 DENDRA_REPO="$tmp/main"; cd "$tmp/main" 2>/dev/null || exit 9
      { . "$REPO/deploy/join.sh" >/dev/null 2>&1 || exit 9; }; REPO="$tmp/main"
      command -v miner_image_pin >/dev/null 2>&1 || exit 9
      miner_image_pin >/dev/null 2>&1
      printf '%s|%s|%s' "$MINER_IMAGE_STATE" "$(head -1 "$tmp/main/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)" \
        "${MINER_IMAGE_PIN:-$MINER_IMAGE_WHY}")"
  fi
  rm -rf "$tmp"
  st="${pin%%|*}"; mk="${pin#*|}"; mk="${mk%%|*}"
  if [ "$st" = pinned ] && [ "$mk" = "$k" ]; then
    KIT_VERSION_PUB="$k"
  elif [ -n "${DENDRA_KIT_WITHOUT_IMAGE_REASON:-}" ]; then
    KIT_VERSION_PUB="$k"
    echo "[publish] KIT_VERSION=$k published without a miner image built for it, on the stated reason:"
    echo "          $DENDRA_KIT_WITHOUT_IMAGE_REASON"
  elif [ -z "$pin" ]; then
    KIT_WHY="the miner image pin of the default branch could not be judged (main unreadable, or $REPO/deploy/join.sh not loadable)"
  else
    KIT_WHY="the default branch pins no miner image built for kit $k (${pin#*|*|})"
  fi
}
_kit_from_release
# The key a previous run published, read before this run rewrites the file. Between a release's tag and the
# pin of its miner image the key is left out by design, so a re-run inside that window removes the number the
# previous run published, and joiners are told their kit version is not checked. The removal is printed
# here, where it happens.
KIT_VERSION_PREV="$(grep -m1 -E '^KIT_VERSION=[0-9]+' "$OUT/network-info.txt" 2>/dev/null | cut -d= -f2 | tr -dc 0-9)"
if [ -n "$KIT_VERSION_PUB" ]; then
  echo "[publish] KIT_VERSION=$KIT_VERSION_PUB, read at $KIT_TAG, the latest release of $KIT_URL."
  [ "${KIT_VERSION_TREE:-?}" = "$KIT_VERSION_PUB" ] || echo "          This tree carries kit ${KIT_VERSION_TREE:-?}; joiners are compared with the release's."
else
  echo "[publish] KIT_VERSION not published: $KIT_WHY."
  echo "          Joiners are told their kit version is not checked. Re-run this script once the release is"
  echo "          tagged and the default branch pins its miner image; KIT_VERSION then follows it."
  if [ -n "$KIT_VERSION_PREV" ]; then
    echo "[publish] WARNING: the network-info this run replaces carried KIT_VERSION=$KIT_VERSION_PREV; this run drops it."
    echo "          Between a release tag and the pin of its miner image this is the expected cost of a re-run;"
    echo "          once the default branch pins that image, re-run and KIT_VERSION is published at the release's number."
  fi
fi
# No backtick inside this heredoc. Its delimiter cannot be quoted (the values come from variables), so
# the shell performs every substitution it finds: a backtick would run what it surrounds and paste the
# output into the published file, which a joiner sourcing it as .env would then trip on. The twin bench
# checks it.
cat > "$OUT/network-info.txt" <<EOF
# Dendra testnet - public network info. Each joiner pastes this into deploy/testnet-node/.env.
CHAIN_ID=$CID
# Consensus epoch of the running network. The joining kit builds the node from the tree it
# cloned, so a tree one epoch away from this network produces a binary that takes different
# state transitions: a fork that stays dormant until the first transaction that exercises the
# change, and then looks like a sync failure. The chain itself cannot answer this (abci_info
# carries no app_version here), so the network declares it, next to everything else a joiner
# already fetches, and deploy/join.sh compares it with docker/CONSENSUS_EPOCH in its tree.
CONSENSUS_EPOCH=$EPOCH
CONSENSUS_EPOCH_PROVEN=$EPOCH_PROVEN
${KIT_VERSION_PUB:+KIT_VERSION=$KIT_VERSION_PUB}
GENESIS_URL=$BASE_URL/genesis.json
GENESIS_SHA256=$SHA
SEEDS=$SEEDS
PERSISTENT_PEERS=$SEEDS
DENDRA_NODE=tcp://$HOST:26657
DENDRA_RELAY=http://$HOST:8645
FAUCET=http://$HOST:4500
# --- state sync, for a node kit configured by hand. deploy/join.sh ignores these three keys: it derives
# the trust point over TLS from docker/STATESYNC_RPC in its clone, since a trust point read from this
# plain-HTTP file is only as trustworthy as the path it came over. Left empty, a node replays from
# genesis, which is safe while a single binary produced the whole history; after a consensus-breaking
# change, a replaying node panics at the height of that change.
STATESYNC_RPC=$SS_RPC
STATESYNC_TRUST_HEIGHT=$SS_H
STATESYNC_TRUST_HASH=$SS_HASH
EOF
# Written after the heredoc, never interpolated into it: a backslash-n inside a variable is not a
# newline to a heredoc; it comes out literally and joins two keys on the same line.
[ -n "${DENDRA_CAPACITY_URL:-}" ] && printf 'DENDRA_CAPACITY_URL=%s\n' "$DENDRA_CAPACITY_URL" >> "$OUT/network-info.txt"
# Final Testnet Season (ADR-047): where the programme answers a miner's tools (status, payout
# declaration, the Dendra application's Final Testnet Season view); rewards are ranked on chain facts.
# HTTPS through Caddy, so it needs a name the certificate holds: derived from HOST when HOST is a name,
# never from an IP (no certificate matches an IP, and a miner would fail TLS silently). An IP-only
# publication says so instead.
S1_URL="${DENDRA_FINAL_SEASON_URL:-}"
case "$HOST" in *[!0-9.]*) [ -n "$S1_URL" ] || S1_URL="https://$HOST/final-season/v1" ;; esac
# The miner image is no longer published here, even pinned by digest. This file is served over plain
# HTTP, and the image is code a joiner pulls and runs next to its miner keys: whoever can rewrite the file
# in transit would choose it, and an attacker's digest is just as well-formed. The pin lives in
# docker/MINER_IMAGE of the tree a joiner clones (HTTPS git); deploy/join.sh reads it from there and
# ignores this key. An operator who still sets the variable is told so.
[ -n "${DENDRA_MINER_IMAGE:-}" ] && echo "  [i] DENDRA_MINER_IMAGE is set but not published: the miner image is pinned by docker/MINER_IMAGE in the published tree"
if [ -n "$S1_URL" ]; then
  printf 'DENDRA_FINAL_SEASON_URL=%s\n' "$S1_URL" >> "$OUT/network-info.txt"
else
  echo "  [!] network-info carries no DENDRA_FINAL_SEASON_URL: '$HOST' is an IP. Miners joining from this file"
  echo "      cannot declare a Final Testnet Season payout address. Publish with the certified name, or set DENDRA_FINAL_SEASON_URL."
fi
if [ -z "$SS_HASH" ]; then
  # The severity of a missing trust point depends on the history. On a chain whose history contains a
  # consensus-breaking change, replaying from genesis panics, so state sync is the only way to join and the
  # absence is blocking. On a chain restarted from zero a single binary produced the history, replay is
  # safe, and state sync is only a convenience; a blocking message there would discourage joining a
  # healthy chain.
  echo "[publish] state-sync trust point unavailable (no usable snapshot)."
  echo "          On a chain restarted from zero this is not blocking - a joiner replays from genesis"
  echo "          safely (history produced by a single binary). State-sync then only adds"
  echo "          synchronization speed."
  echo "          On a chain whose history carries a consensus-breaking change this is blocking: the"
  echo "          joiner would panic - invite nobody while these 3 values are empty."
else
  echo "[publish] state-sync trust point published: height $SS_H"
fi

# The public surface guard judges the file before it is served, when this tree carries it (a checkout of the
# private repository; a host with only the kit has no guard and says so). The personal category is left
# out: the file names the project's own servers, by IP when HOST is one.
SPG="$REPO/dendra/onchain-staging/dendra_surface_publique_garde.py"
if [ -r "$SPG" ] && command -v python3 >/dev/null 2>&1; then
  SPOUT="$(python3 "$SPG" --repo "$REPO" --notes "$OUT/network-info.txt" --categories ai,watermark,names,style 2>&1)"; SPRC=$?
  if [ "$SPRC" != 0 ]; then
    mv -f "$OUT/network-info.txt" "$OUT/network-info.txt.refused"
    echo "[ERR] the public surface guard refuses network-info.txt (code $SPRC); it was moved to network-info.txt.refused:"
    printf '%s\n' "$SPOUT" | grep -E '^  \[|^PUBLIC_SURFACE_SUMMARY' | head -12 | sed 's/^/      /'
    exit 1
  fi
  echo "[publish] public surface guard: $(printf '%s\n' "$SPOUT" | grep -E '^PUBLIC_SURFACE_SUMMARY' | tail -1)"
else
  echo "  [!] public surface guard not found on this host ($SPG): network-info.txt is not judged here."
fi

echo "## 2) Published -> $OUT/"
cat "$OUT/network-info.txt"
echo "------------------------------------------------------------"
echo "## 3) Host the genesis at GENESIS_URL. The simplest way:"
echo "      cd $OUT && nohup python3 -m http.server 8088 >/tmp/dendra-genesis-http.log 2>&1 &"
echo "   (or commit deploy/testnet/published/genesis.json, or use a CDN/S3). Open port 8088 and P2P 26656."
echo "## 4) A joiner, the simplest path:  CONFIG_URL=$BASE_URL/network-info.txt bash deploy/join.sh"
echo "   (network-info.txt carries the chain id, the genesis URL and digest, the seeds, and the node, relay and faucet endpoints, so one URL is enough.)"
echo "   Host network-info.txt next to the genesis (same http.server). Manual/advanced path: see the kit READMEs."
