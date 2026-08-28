#!/usr/bin/env bash
# join.sh — join the Dendra testnet in ONE command (canonical path).
# Wraps the existing building blocks (docker/node-join.sh, deploy/testnet-miner, deploy/testnet-node,
# deploy/testnet/anchor_vrf_key.sh, miner.py) — it does NOT reimplement them.
#
#   MINER (default):
#     CONFIG_URL=<URL of network-info.txt> bash deploy/join.sh
#     # or explicit:
#     DENDRA_NODE=tcp://HOST:26657 DENDRA_RELAY=http://HOST:8645 FAUCET=http://HOST:4500 bash deploy/join.sh
#   MINER-JUDGE (audit committee):        ... bash deploy/join.sh --judge
#   VALIDATOR (sync + guided bond + VRF): CONFIG_URL=... bash deploy/join.sh --validator
#
# Canonical defaults (single source of truth — never the 'hash' backend on a real network):
#   DENDRA_MODEL_ID=llama3.1:8b-instruct-q4_K_M · DENDRA_EMBED_MODE=backend · DENDRA_EMBED_API_MODEL=nomic-embed-text
# Status: research / testnet. The validator bond is a DELIBERATE action (confirmation required, never silent).
set -u

ROLE=miner; JUDGE=0; MINER_ID="${MINER_ID:-}"
# OWN_NODE — a miner runs its own full node unless it explicitly opts out. See the long note in
# run_miner: the opt-out trades away the ability to verify the chain for itself, so it is a decision
# the operator makes on purpose, never a default it drifts into.
OWN_NODE=1
while [ $# -gt 0 ]; do case "$1" in
  --validator) ROLE=validator; shift;;
  --miner) ROLE=miner; shift;;
  --judge) JUDGE=1; shift;;
  --own-node) OWN_NODE=1; shift;;
  --remote-rpc|--no-node) OWN_NODE=0; shift;;
  --id) MINER_ID="${2:-}"; shift 2;;
  -h|--help)
    sed -n '2,16p' "$0" 2>/dev/null || true
    echo "Modes: (default) miner+own node | --judge miner+audit committee | --validator node+guided bond+VRF | --id NAME"
    echo "       --remote-rpc  read the chain from the operator's public RPC instead of running a node"
    exit 0;;
  *) echo "[join] unknown arg: $1 (see --help)"; exit 2;;
esac; done

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
REPO="$(cd "$SELF/.." 2>/dev/null && pwd)"
# REPOSITORY ROOT — validated by a MARKER, never inferred on its own.
# `$0` is "deploy/join.sh" only when the script is run as a FILE. Read from a PIPE
# (`tr -d '\r' < deploy/join.sh | bash -s -- --validator`, the form the runbooks recommend to strip
# CRLF), `$0` is "bash": `dirname` returns ".", SELF becomes the CURRENT directory, and the `..`
# climbs one level TOO FAR. The script would then look for `<parent>/deploy/testnet-node` — which does
# not exist — and die on "docker compose up failed", a message that says nothing about the real
# problem. So the root is verified by the presence of the kit, and searched upwards from the CWD when
# the computation is wrong. `DENDRA_REPO` still takes precedence.
if [ -n "${DENDRA_REPO:-}" ] && [ -d "$DENDRA_REPO/deploy/testnet-node" ]; then
  REPO="$DENDRA_REPO"
elif [ ! -d "$REPO/deploy/testnet-node" ]; then
  _d="$PWD"
  while [ "$_d" != "/" ] && [ -n "$_d" ]; do
    if [ -d "$_d/deploy/testnet-node" ]; then REPO="$_d"; break; fi
    _d="$(dirname "$_d")"
  done
fi
[ -d "$REPO/deploy/testnet-node" ] || { echo "[join] FATAL: repository root not found (try: export DENDRA_REPO=/path/to/Dendra)"; exit 1; }
MINER_KIT="$REPO/deploy/testnet-miner"
NODE_KIT="$REPO/deploy/testnet-node"

say(){ printf '%s\n' "$*"; }
die(){ printf '[join] FATAL: %s\n' "$*" >&2; exit 1; }
warn(){ printf '[join] WARN: %s\n' "$*"; }

# ---------------------------------------------------------------- (0) CONFIG
# CONFIG_URL -> network-info.txt is PARSED (never `source`d: we do NOT execute downloaded text). Only the
# EXPECTED keys are accepted, strict KEY=VALUE format, and each value is validated against a safe character
# set before assignment. This closes MITM command injection over a plaintext CONFIG_URL: a value is assigned
# literally (no eval), and any value containing shell metacharacters is rejected.
_load_config_url(){
  local url="$1" tmp; tmp="$(mktemp)"
  curl -fsSL "$url" -o "$tmp" || die "CONFIG_URL unreachable: $url"
  local k v line
  while IFS= read -r line; do
    case "$line" in \#*|"") continue;; esac
    k="${line%%=*}"; v="${line#*=}"
    case "$k" in
      # STATESYNC_* are listed here DELIBERATELY: this allow-list silently drops any unknown key (that
      # is its purpose, anti-injection). Without this line the operator would publish the trust point,
      # the joiner would download it, and it would be ignored without a word — four correct links in
      # the chain and one mute. The values remain subject to the character filter.
      # DENDRA_CAPACITY_URL is listed for the same reason as STATESYNC_*: the derivation below cannot
      # produce a usable value on this network, and without a way for the operator to publish the
      # right one the joiner has no remedy at all — an unknown key is dropped in silence.
      CHAIN_ID|GENESIS_URL|GENESIS_SHA256|SEEDS|PERSISTENT_PEERS|DENDRA_NODE|DENDRA_RELAY|FAUCET|EXPLORER_URL|STATESYNC_RPC|STATESYNC_TRUST_HEIGHT|STATESYNC_TRUST_HASH|DENDRA_CAPACITY_URL|CONSENSUS_EPOCH|CONSENSUS_EPOCH_PROVEN|KIT_VERSION)
        # value must be a plain URL / host:port / node-id / hex — no shell metacharacters, no spaces
        case "$v" in
          *[!A-Za-z0-9:/._@,-]*) warn "config: value for $k rejected (invalid characters) — ignored"; continue;;
        esac
        export "$k=$v";;
      *) : ;;  # unknown key = ignored (no execution of remote content)
    esac
  done < "$tmp"
  rm -f "$tmp"
  say "[join] config loaded from CONFIG_URL (${DENDRA_NODE:-?} / ${DENDRA_RELAY:-?} / ${FAUCET:-?})"
}
if [ -n "${CONFIG_URL:-}" ]; then
  _load_config_url "$CONFIG_URL"
elif [ -f "$MINER_KIT/.env" ] && [ "$ROLE" = miner ]; then
  say "[join] config from $MINER_KIT/.env"
elif [ -f "$NODE_KIT/.env" ] && [ "$ROLE" = validator ]; then
  say "[join] config from $NODE_KIT/.env"
fi

# Canonical defaults — DENDRA_EMBED_MODE=backend is HARDCODED for the network (the 'hash' bag-of-words
# measures WORDS, not meaning -> a mixed committee misverifies). Judge model is RAM-based (see below).
# THE OPERATOR'S EXPLICIT CHOICE, captured BEFORE any default is applied.
# Without this capture, `DENDRA_MODEL_ID=... bash join.sh` would be set here and OVERWRITTEN thirty
# lines below, WITHOUT A WORD: an operator who asks for a precise model — because the on-chain registry
# requires it, or to stay homogeneous with the rest of the network — would get something else and never
# find out. A script may propose a default; it must not cancel an instruction in silence.
DENDRA_MODEL_ID_EXPLICIT="${DENDRA_MODEL_ID:-}"
export DENDRA_MODEL_ID="${DENDRA_MODEL_ID:-llama3.1:8b-instruct-q4_K_M}"
export DENDRA_EMBED_MODE=backend
export DENDRA_EMBED_API_MODEL="${DENDRA_EMBED_API_MODEL:-nomic-embed-text}"
# STABLE IDENTITY ACROSS RUNS.
# A `$RANDOM` suffix would mint a NEW miner on every run: new key, new address, previous stake lost,
# and the on-chain registration to redo (from an empty account, hence impossible). The `miner-keys`
# volume is not enough to preserve the identity — MINER_ID is what designates it, which makes "do not
# delete this volume or you re-stake" misleading: simply re-running was enough to lose everything. The
# identity already written in the kit's .env is reused; `--id` still takes precedence.
# ⚠️ CARRY-OVER IS ONLY VALID WHILE THE CHAIN ACCEPTS CHOSEN IDENTIFIERS. From consensus epoch 3 the
# chain DERIVES the miner id from the signing address and refuses anything else, so an id inherited
# from a previous genesis is rejected at registration — and the daemon would keep retrying against a
# rule it cannot satisfy, which reads as "the network is broken" rather than "this id is stale".
# The kit therefore refuses the carry-over rather than handing the chain a value it will reject.
if [ -z "$MINER_ID" ] && [ -f "$MINER_KIT/.env" ]; then
  MINER_ID="$(grep -E '^MINER_ID=' "$MINER_KIT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | tr -d '[:space:]')"
  case "$MINER_ID" in
    dm1*) [ -n "$MINER_ID" ] && say "[join] miner identity REUSED from .env: $MINER_ID (stake preserved)" ;;
    "")   : ;;
    # ⛔ THIS BRANCH USED TO `exit 2`, AND IT LOCKED EVERY NEWCOMER OUT ON THEIR SECOND RUN — on an
    # identifier THIS SCRIPT had written on the first. It derives `m-<hash>` below and puts it in the
    # .env; the chain accepts only an id derived from the address; so the canonical public command
    # refused its own output, and the only way past was an env var named nowhere public.
    # The daemon now RESOLVES and PERSISTS the accepted identity in its own volume
    # (`miner.py`, `identite-resolue`), so a stale name here is no longer fatal: the next start
    # resumes the identity that actually holds the stake instead of minting a new one. What is left is
    # worth a WARNING, not a refusal — refusing was never the safe direction, it was the loud one.
    *)    say "  [i] '$MINER_ID' in $MINER_KIT/.env is not a derived identifier (dm1...)."
          say "      The chain derives the miner id from your address and refuses any other value, so"
          say "      this name is not what will be registered. The daemon resolves the right one at"
          say "      startup and REMEMBERS it, so your registration and stake survive restarts."
          say "      Nothing to do. If you want the file to match what runs, read the dm1... value"
          say "      from the miner's first log line and put it here."
          say "      NOTE: an identity from an EARLIER GENESIS carries no balance — a fresh genesis"
          say "      starts every account at zero, whatever the identifier says." ;;
  esac
fi
# IDENTITY DEFAULT — NEVER THE HOSTNAME. Whatever lands here is written into the ON-CHAIN miner
# registry: world-readable, and immutable, forever. A registry entry cannot be edited or withdrawn,
# so a hostname reaching this variable is the one leak in the whole kit that no later fix repairs.
# The pseudonym is derived from the machine id, exactly like deploy/hw_probe.sh:35 does: stable across
# restarts (so the stake and the track record survive), reproducible by its owner alone (run the same
# sha256 locally to find yourself in a list), and meaningless to anyone else.
# Naming yourself stays possible with --id: it becomes a DELIBERATE act, never something a script
# inherits from a forgotten $(hostname).
# ⚠️ THE BRACES ARE LOad-BEARING. `a || b || c | sha256sum` parses as `a || b || (c | sha256sum)`:
# the pipeline binds to the LAST alternative only, so a successful fallback would emit its value RAW.
# Grouping first, hashing after, is what makes every branch pseudonymous instead of just one.
PSEUDO_ID="m-$( { cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo machine; } | tr -d '\n' | head -c 64 | sha256sum | cut -c1-10)"
[ -n "$MINER_ID" ] || MINER_ID="$PSEUDO_ID"
# ANTI-DEANONYMIZATION GUARD — same rule, same floor and same remedy as deploy/hw_probe.sh:49-59.
# The test is INCLUSION, not equality, because callers build identifiers by concatenation: a name that
# merely CONTAINS the hostname leaks it just as completely as one that equals it.
# ⚠️ THE 4-CHARACTER FLOOR IS PART OF THE RULE, NOT A DETAIL OF THE OTHER FILE. Hostnames like `pc`,
# `vm` or `dev` are substrings of ordinary text and identify nobody, so without the floor the guard
# fires on identifiers that leak nothing — including the pseudonym it hands out. A guard copied without
# its floor is a different guard.
# The identifier is REPLACED, not refused. Refusing costs the operator their whole join over a name the
# script can simply fix, and the privacy outcome is identical. Publishing the machine name stays
# possible, as a deliberate act.
_HOSTNAME="$(hostname 2>/dev/null || echo '')"
[ "${#_HOSTNAME}" -ge 4 ] || _HOSTNAME='__too_short_to_identify__'
case "$MINER_ID" in
  *"$_HOSTNAME"*)
    if [ "${DENDRA_ACCEPT_PUBLIC_NAME:-0}" = "1" ]; then
      say "  [!] the identity '$MINER_ID' CONTAINS this machine's hostname, kept because"
      say "      DENDRA_ACCEPT_PUBLIC_NAME=1. It goes into the on-chain miner registry: world-readable,"
      say "      and never editable nor withdrawable."
    else
      say "  [!] the identity '$MINER_ID' CONTAINS this machine's hostname. It would be written on-chain,"
      say "      publicly and permanently -> REPLACED by the pseudonym '$PSEUDO_ID'."
      say "      To publish the machine name instead, set DENDRA_ACCEPT_PUBLIC_NAME=1: a choice, never"
      say "      something a script inherits from a forgotten hostname."
      MINER_ID="$PSEUDO_ID"
    fi ;;
esac

# Audit-committee HETEROGENEITY by config, RAM-based. A miner-judge picks its judge model by RAM:
# >=26 GB -> MoE qwen3:30b-a3b (most discriminating), otherwise mistral-nemo. NEVER qwen3:4b (removed:
# false-slashes in a distributed setting). Heterogeneity (>=2 distinct judge models on the network) closes
# correlated false-slash. Override explicitly with DENDRA_JUDGE_MODEL. The on-chain verdict CARRIES the model
# (DENDRA_JUDGE_MODEL_ID) so judge-model diversity is measurable.
# The single source of truth for BOTH the served model and the judge eligibility is deploy/hw_probe.sh:
# it measures the real VRAM/RAM, sizes the model to the card, and gates the judge seat behind an
# allow-list of validated judge models. A second, RAM-only heuristic lived here and diverged from it —
# it could seat a judge on a 4 GB card (RAM says yes, VRAM says no) and hand the miner a model bigger
# than its card. One probe, one answer.
HW_PROBE="$REPO/deploy/hw_probe.sh"
HW_JSON=""; HW_CAN_JUDGE="false"; HW_MODEL=""; HW_JUDGE_MODEL=""; HW_TIER=""
if [ -r "$HW_PROBE" ]; then
  HW_JSON="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --json 2>/dev/null || true)"
  HW_CAN_JUDGE="$(printf '%s' "$HW_JSON" | grep -o '"can_judge":[a-z]*' | cut -d: -f2)"; [ -n "$HW_CAN_JUDGE" ] || HW_CAN_JUDGE="false"
  HW_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"model":"[^"]*"' | cut -d'"' -f4)"
  HW_JUDGE_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"judge_model":"[^"]*"' | cut -d'"' -f4)"
  HW_TIER="$(printf '%s' "$HW_JSON" | grep -o '"tier":[0-9]*' | cut -d: -f2)"
  # The probe DECIDES only when the operator imposed nothing. And when it decides, it SAYS so: on a
  # large card it returns a top tier (qwen3:30b-a3b, ~19 GB) instead of the 8B announced at the top of
  # this file, which changes both what is downloaded AND the model served to the network. A choice of
  # that reach must not be discovered by reading a generated .env.
  if [ -n "${DENDRA_MODEL_ID_EXPLICIT:-}" ]; then
    export DENDRA_MODEL_ID="$DENDRA_MODEL_ID_EXPLICIT"
    [ -n "$HW_MODEL" ] && [ "$HW_MODEL" != "$DENDRA_MODEL_ID" ] && \
      say "  [i] model IMPOSED by you: $DENDRA_MODEL_ID (the probe would have picked $HW_MODEL for this hardware)"
  elif [ -n "$HW_MODEL" ]; then
    export DENDRA_MODEL_ID="$HW_MODEL"
    say "  [i] model CHOSEN BY THE PROBE: $HW_MODEL (tier ${HW_TIER:-?} from the measured VRAM)."
    say "      To impose your own:  DENDRA_MODEL_ID=<model> bash deploy/join.sh ..."
  fi
fi

_pick_judge_model(){
  [ -n "${DENDRA_JUDGE_MODEL:-}" ] && { echo "$DENDRA_JUDGE_MODEL"; return; }
  [ -n "$HW_JUDGE_MODEL" ] && { echo "$HW_JUDGE_MODEL"; return; }
  echo "mistral-nemo"
}

# Judge-seat gate: an under-powered or unvalidated judge does not merely judge badly, it votes against
# answers that are correct but worded differently — and an unfair verdict costs an honest miner its
# stake. A node may MINE on any hardware; it may only JUDGE when the probe says so.
_assert_can_judge(){
  [ "$HW_CAN_JUDGE" = "true" ] && return 0
  warn "this machine is NOT eligible to judge (hardware tier ${HW_TIER:-?})."
  warn "It can MINE normally. Seating an under-powered judge penalises honest miners, so the role is refused."
  warn "Run 'bash deploy/hw_probe.sh' to see your tier and the model it selects."
  return 1
}

# ---------------------------------------------------------------- (a) PRE-FLIGHT (fail-fast + remediation)
GPU_OK=0; TOOLKIT_OK=0
check_prereqs(){
  say "== [join] pre-flight =="
  # Docker + compose v2
  if ! docker compose version >/dev/null 2>&1; then
    say "  [KO] Docker/Compose v2 missing -> https://docs.docker.com/engine/install/"
    # NO Docker-free fallback is shipped. This line used to name one; the file it named is not part of
    # the published repository, so the reader hit a dead end at the exact moment they had no working
    # path left. A remediation that points at something absent is worse than none: it costs a search
    # before the same conclusion. Docker Compose v2 is a hard prerequisite here, and that is said.
    say "       Docker Compose v2 is a REQUIREMENT of this kit: there is no Docker-free path in this repository."
    exit 1
  fi
  say "  [OK] docker compose"
  # Minimal config per role
  if [ "$ROLE" = miner ]; then
    { [ -n "${DENDRA_NODE:-}" ] && [ -n "${DENDRA_RELAY:-}" ] && [ -n "${FAUCET:-}" ]; } \
      || { [ -f "$MINER_KIT/.env" ]; } \
      || die "missing config: provide CONFIG_URL=<network-info.txt> (recommended) or fill $MINER_KIT/.env"
  else
    { [ -n "${GENESIS_URL:-}" ]; } || { [ -f "$NODE_KIT/.env" ]; } \
      || die "missing config: provide CONFIG_URL=<network-info.txt> (GENESIS_URL/SHA256/SEEDS) or fill $NODE_KIT/.env"
  fi
  # NVIDIA GPU (WARN, not FATAL — CPU works but is slow). THE WHOLE BLOCK IS A MINER CONCERN.
  # Inference is what needs a card, and only the miner serves a model: a validator syncs, signs blocks
  # and contributes to the seed, none of which touches a GPU. So on --validator there is no missing
  # hardware to report — and the CPU notice below ends with a ten-second window to cancel, which turns
  # a correct validator join into an invitation to abort it.
  if [ "$ROLE" = miner ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      GPU_OK=1
      say "  [OK] GPU: $(nvidia-smi -L | head -1)"
      local vram
      vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
      if [ -n "$vram" ] && [ "$vram" -lt 6000 ] 2>/dev/null; then
        warn "VRAM ~${vram} MB: the pinned model (~5-6 GB) will spill -> floor throughput. >=8 GB recommended."
      fi
      # `--entrypoint` is MANDATORY here: the ollama/ollama image sets ENTRYPOINT=/bin/ollama, so
      # `docker run ollama/ollama nvidia-smi` runs `ollama nvidia-smi` -> unknown command -> FAILURE,
      # WHATEVER the state of the toolkit. Without it the test can never succeed: the GPU override is
      # NEVER written and every miner using this kit infers on CPU while believing the opposite, since
      # `nvidia-smi` answers perfectly on the host. A detection that cannot be positive detects nothing —
      # it merely blames the user for a missing installation, every time.
      if docker run --rm --gpus all --entrypoint nvidia-smi ollama/ollama:latest -L >/dev/null 2>&1; then
        TOOLKIT_OK=1; say "  [OK] nvidia-container-toolkit (GPU visible to Docker)"
      else
        warn "GPU seen by the host but NOT by Docker -> install nvidia-container-toolkit; otherwise CPU (slow)."
        warn "  check by hand:  docker run --rm --gpus all --entrypoint nvidia-smi ollama/ollama:latest -L"
      fi
    else
      warn "no NVIDIA GPU detected -> CPU inference (slow, floor throughput). Ctrl-C to cancel (10 s)..."
      sleep 10
    fi
  else
    say "  [i] GPU: none required for this role. A validator serves no model — it syncs, signs blocks"
    say "      and feeds the committee seed, and none of that runs inference."
  fi
  # Disk
  local freeg
  freeg=$(df -BG --output=avail "$REPO" 2>/dev/null | tail -1 | tr -dc '0-9')
  [ -n "$freeg" ] && [ "$freeg" -lt 10 ] 2>/dev/null && warn "free disk ${freeg}G: model ~5-6 GB + chain state -> plan for >=10-15 GB."
  # Public RPC reachable (miner: required)
  if [ -n "${DENDRA_NODE:-}" ]; then
    local rpc_http="${DENDRA_NODE/tcp:\/\//http://}"
    if curl -fsS -m 8 "$rpc_http/status" >/dev/null 2>&1; then
      say "  [OK] public RPC reachable ($DENDRA_NODE)"
    else
      [ "$ROLE" = miner ] && die "public RPC unreachable ($DENDRA_NODE): operator down, wrong IP, or firewall. Check network-info.txt."
      warn "RPC $DENDRA_NODE unreachable (the node will use SEEDS to sync)."
    fi
  fi
}

# ---------------------------------------------------------------- (b) GENESIS SHA — anti-MITM, fail-closed
#
# ⚠️ THIS FUNCTION USED TO FAIL OPEN, IN SILENCE, AND THE CORRECT RULE WAS ALREADY WRITTEN NEXT DOOR.
# `docker/node-join.sh:62-76` states it in as many words: "An empty GENESIS_SHA256 no longer performs a
# silent SKIP (which would leave a public joiner unprotected)" — fail-closed by default, one NAMED opt-out
# for a trusted network. That rule was never ported here. This function instead did:
#     [ -n "$GENESIS_URL" ] && [ -n "$GENESIS_SHA256" ] || return 0
# so an absent hash returned SUCCESS without printing anything, and a failed download skipped the body
# just as quietly. GENESIS_SHA256 comes from the SAME network-info.txt an attacker would serve, so
# omitting one line disarmed the whole check — while the site, this kit's README and the launch
# announcement all told the operator it "fails closed". The operator saw no VERIFIED line, and nothing
# told them they should have.
#
# Scope, stated: a miner that runs its own node is protected twice (node-join.sh enforces this again
# inside the container). A miner that only runs the inference kit never reaches node-join.sh — for that
# operator THIS is the only check that the network they are about to serve is the one they think it is.
# ---------------------------------------------------------------- consensus epoch (fork, upstream)
# ⛔ THE GENESIS HASH PROVES WHICH HISTORY, NOT WHICH STATE MACHINE. This kit BUILDS the node from the
# tree you cloned, so a tree one consensus epoch away from the running network produces a binary that
# takes DIFFERENT state transitions on the same blocks. The failure is not immediate and does not look
# like a version problem: the node syncs normally until the first transaction that exercises the
# change, then stops on an AppHash mismatch that reads exactly like a slow disk or a bad peer.
# The chain cannot answer this on its own (`abci_info` carries no app_version here), so the network
# DECLARES its epoch in the same config this script already fetches, and we compare.
# THREE ANSWERS, never two: equal -> fine; different -> REFUSE; absent -> say what is NOT checked.
# Absent is the config of an operator who has not republished yet: refusing there would lock out every
# joiner on a network that is probably fine, so it warns and names the risk instead of pretending.
verify_consensus_epoch(){
  local mine theirs f="$REPO/docker/CONSENSUS_EPOCH"
  mine="$(head -1 "$f" 2>/dev/null | tr -dc 0-9)"
  theirs="$(printf '%s' "${CONSENSUS_EPOCH:-}" | tr -dc 0-9)"
  if [ -z "$mine" ]; then
    warn "docker/CONSENSUS_EPOCH unreadable in this tree -> cannot tell whether your build matches the network."
    return 0
  fi
  if [ -z "$theirs" ]; then
    warn "this network does not declare a consensus epoch -> NOT CHECKED whether your tree matches it."
    say  "       Your tree is at epoch $mine. If the operator is on a different one, your node will sync"
    say  "       normally and then stop on an AppHash mismatch at the first transaction that exercises"
    say  "       the difference — which looks like a network problem and is not one."
    say  "       Ask the operator to add CONSENSUS_EPOCH= to the file named by CONFIG_URL."
    return 0
  fi
  if [ "$mine" = "$theirs" ]; then
    # ⚠️ MATCHING THE DECLARATION IS NOT MATCHING THE CHAIN. The number in the config file is written
    # by the operator's publisher; until it also says WHERE it read it, an agreement here only proves
    # that two trees carry the same digit. `CONSENSUS_EPOCH_PROVEN=1` means the publisher took it from
    # the marker written after the chain was actually brought up; anything else means it took it from
    # the operator's tree, which can sit ahead of what is deployed.
    if [ "${CONSENSUS_EPOCH_PROVEN:-}" = "1" ]; then
      say "  [OK] consensus epoch $mine — your tree takes the same state transitions as this network,"
      say "       and the network declares this epoch from the chain it actually deployed."
    else
      say "  [OK] consensus epoch $mine — your tree matches what this network DECLARES."
      warn "the network does not certify where that number came from: it may be the operator's tree"
      say  "       rather than the chain that is running. If they differ, your node syncs and then halts"
      say  "       on an AppHash mismatch. Ask the operator to publish with CONSENSUS_EPOCH_PROVEN=1."
    fi
    return 0
  fi
  say "  [KO] CONSENSUS EPOCH MISMATCH: your tree is at $mine, this network runs $theirs."
  say "       A node built here would AGREE on the genesis and DISAGREE on the rules. It will sync"
  say "       until the first transaction that exercises the difference, then halt on an AppHash"
  say "       mismatch — a failure that looks like a slow peer and is not."
  say "       Ways out: check out the tree at the network's epoch, or wait for the operator to deploy"
  say "       yours. DENDRA_ALLOW_EPOCH_MISMATCH=1 forces it for a network you are testing against."
  if [ "${DENDRA_ALLOW_EPOCH_MISMATCH:-0}" = "1" ]; then
    warn "DENDRA_ALLOW_EPOCH_MISMATCH=1 -> joining anyway with a KNOWN state-machine difference."
    return 0
  fi
  die "consensus epoch mismatch ($mine vs $theirs) -> refusing to build a node that would fork."
}

# KIT VERSION — "is the code I run current?", which is NOT "does my binary agree on the rules?".
#
# THREE ANSWERS, and NONE of them refuses. A kit behind the network still joins: the defect it
# carries breaks THIS operator, not the chain. Refusing here would lock a joiner out over a script
# fix, and the epoch check above already exists for the case that must refuse. Two numbers, because
# one cannot serve both: merged, it would either refuse a joiner over a fix, or stay silent on a fork.
#
# WHY IT EXISTS. A shipped defect had no way to announce itself: the operator ran code that could not
# do its job, everything looked healthy, and nothing anywhere said "update". The epoch could not carry
# that signal, because nothing about consensus had changed.
verify_kit_version(){
  local mine theirs f="$REPO/docker/KIT_VERSION"
  mine="$(head -1 "$f" 2>/dev/null | tr -dc 0-9)"
  theirs="$(printf '%s' "${KIT_VERSION:-}" | tr -dc 0-9)"
  if [ -z "$mine" ]; then
    warn "docker/KIT_VERSION unreadable in this tree -> cannot tell whether your kit is current."
    return 0
  fi
  if [ -z "$theirs" ]; then
    say "  [i] this network does not declare a kit version -> NOT CHECKED whether yours is current."
    say "      Yours is $mine. Ask the operator to add KIT_VERSION= to the file named by CONFIG_URL."
    return 0
  fi
  if [ "$mine" -ge "$theirs" ] 2>/dev/null; then
    say "  [OK] kit version $mine — current with what this network publishes."
    return 0
  fi
  warn "KIT BEHIND: yours is $mine, this network publishes $theirs."
  say  "       Your node still joins. What you may be missing are fixes to the scripts and services"
  say  "       YOU run — and a defect there is silent by nature: the code does its job badly rather"
  say  "       than failing, so neither you nor the network can see it."
  say  "       Update:  git fetch origin && git reset --hard origin/main"
  say  "       (this repository is republished per release, so 'git pull' does not work), then re-run."
  return 0
}

verify_genesis_info(){
  # No GENESIS_URL is legitimate: the node kit may already carry a filled .env (see the config check
  # above), and node-join.sh enforces the hash from that file. Say so rather than skipping mutely.
  if [ -z "${GENESIS_URL:-}" ]; then
    # "THE NODE KIT ENFORCES ITS OWN" IS TRUE ONLY WHEN THERE IS A NODE KIT.
    # With --remote-rpc no container starts, node-join.sh is never reached, and the header of this
    # very function says so: "for that operator THIS is the only check". Returning 0 here therefore
    # announced a protection that does not exist in that mode — a miner pointed at an attacker's
    # DENDRA_NODE/RELAY/FAUCET passed every check, registered, staked, and served inference on a
    # chain whose identity was never confronted with anything. Fail CLOSED instead, with the way out
    # named.
    if [ "${OWN_NODE:-1}" = "0" ]; then
      if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
        warn "--remote-rpc with no GENESIS_URL + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> the chain identity is NOT verified (trusted network only)."
        return 0
      fi
      say "  [KO] --remote-rpc and no GENESIS_URL: NOTHING would check which chain this is."
      say "       Without a node kit there is no second line of defence — node-join.sh never runs in"
      say "       this mode, so this check is the only one, and it has nothing to check against."
      say "       Pass the operator's config (CONFIG_URL=<network-info.txt>), or set"
      say "       DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a network you already trust."
      die "--remote-rpc without GENESIS_URL -> refusing to serve a chain whose identity is unverified."
    fi
    say "  [i] no GENESIS_URL in this config -> genesis not checked here (the node kit enforces its own)"
    return 0
  fi
  if [ -z "${GENESIS_SHA256:-}" ]; then
    if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
      warn "GENESIS_SHA256 empty + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> anti-MITM verification DISABLED (trusted network only)."
      return 0
    fi
    say "  [KO] this config carries GENESIS_URL but NO GENESIS_SHA256."
    say "       A network-info.txt without the hash cannot be verified — and that is exactly what a"
    say "       forged one would look like. Obtain the sha INDEPENDENTLY — security@dendranetwork.com,"
    say "       or a node you already trust — and put it in GENESIS_SHA256,"
    say "       or set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a network you already trust."
    die "genesis SHA256 missing from the config (anti-MITM) -> refusing to join blind."
  fi
  local tmp sha; tmp="$(mktemp)"
  if curl -fsSL "$GENESIS_URL" -o "$tmp" 2>/dev/null; then
    sha=$(sha256sum "$tmp" | cut -d' ' -f1)
    rm -f "$tmp"
    if [ "$sha" = "$GENESIS_SHA256" ]; then
      say "  [OK] genesis matches the hash published with it."
      # ⛔ AND THAT IS ALL IT PROVES.
      # No wording here may claim network IDENTITY, because this check cannot establish it:
      # GENESIS_SHA256 arrives in the SAME network-info.txt as GENESIS_URL, over plain HTTP, from
      # the same host —
      # the block at the top of this function says so in as many words. Whoever can serve a forged
      # genesis can serve the matching hash beside it, and this check returns OK. What it DOES rule
      # out: a corrupted download, a config that drifted from the genesis it names, and an attacker
      # who alters only one of the two. That is worth having, and it is not identity.
      say "       It does NOT prove WHICH network this is: the hash travels in the same file as the"
      say "       URL, over plain HTTP. It rules out a corrupted download and a config that drifted"
      say "       from its genesis — not an operator who serves you both."
      say "       To settle identity you need this digest from somewhere else: ask for it at"
      say "       security@dendranetwork.com, or compare against a node you already trust."
    else
      die "published genesis SHA256 != expected ($GENESIS_SHA256) -> altered network / possible MITM. Get the sha from a SECOND source (security@dendranetwork.com, or a node you already trust) before doing anything else."
    fi
  else
    # Was a silent skip too: an unreachable genesis left the operator believing the check had passed.
    rm -f "$tmp"
    die "could not download the genesis at $GENESIS_URL -> the network identity cannot be verified. Check the URL, or the operator's host."
  fi
}

# ---------------------------------------------------------------- override GPU without editing YAML
# The override is a MACHINE ARTEFACT, never a repository file: it reserves an NVIDIA device, and on a
# CPU-only / AMD / no-nvidia-container-toolkit host, `docker compose up` fails on the very first start
# with "could not select device driver", without naming the cause. Refraining from writing it is not
# enough — it must be REMOVED when the GPU is absent, otherwise a file left behind by an equipped
# machine (or by a repository clone) condemns an operator who never had a GPU.
write_gpu_override(){
  if [ "$GPU_OK" != 1 ] || [ "$TOOLKIT_OK" != 1 ]; then
    if [ -f "$MINER_KIT/docker-compose.override.yml" ]; then
      rm -f "$MINER_KIT/docker-compose.override.yml"
      say "  [i] GPU override REMOVED: this kit starts on CPU (slow inference, but functional)."
      say "      Keeping it would make 'docker compose up' fail on 'could not select device driver'."
    fi
    return 0
  fi
  cat > "$MINER_KIT/docker-compose.override.yml" <<'EOF'
# Generated by deploy/join.sh: enables the NVIDIA GPU for Ollama WITHOUT editing docker-compose.yml.
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: all, capabilities: ["gpu"] }]
EOF
  say "  [OK] GPU enabled via docker-compose.override.yml (nothing to edit)"
}

# ---------------------------------------------------------------- capacity publication
# `hw_probe.sh` produces the capacity JSON; without this step nobody PUBLISHES it, the `/capacity`
# registry never receives any data, and the public /network page shows zero everywhere for everyone. A
# running service, a page that displays it, and no producer: the failure is invisible because "0 nodes"
# is a perfectly plausible result on a young network. It is the opposite of an alarm that shouts — a
# display that lies by omission, without ever complaining.
# The PUBLIC capacity registry URL, derived ONCE and shared by everything that publishes.
#
# ⚠️ IT MUST BE DERIVED FROM THE SHELL `DENDRA_NODE`, NEVER FROM THE KIT'S .env. In the default setup this
# script writes a CONTAINER-LOCAL value into the kit (`DENDRA_NODE=tcp://host.docker.internal:26657`, see
# MINER_RPC below): correct for the miner container, useless as a public endpoint. The shell variable
# keeps the operator's public RPC throughout (comment at the MINER_RPC block). Two producers deriving the
# same way from two different inputs is exactly how the re-publisher ended up posting into the void while
# this script's own first publication looked green.
# ⛔ AND THE DERIVATION CANNOT WORK ON A BARE IP, WHICH IS WHAT THIS NETWORK PUBLISHES. `DENDRA_NODE`
# in the operator's network-info.txt is `tcp://<IP>:26657`, so the line below builds `https://<IP>/capacity`
# — and TLS on a bare IP presents a certificate issued for a NAME, so the handshake fails before any
# route is consulted. Measured: `https://80.71.235.130/capacity` returns 000 (TLS connect error) while
# `https://api.dendranetwork.com/capacity` returns 200 ON THE SAME ADDRESS. The consequence is not a
# noisy failure but a silent one: `publish_capacity` takes its else-branch, and `install_capacity_cron`
# — which lives ONLY in the success branch — is never reached. Every joiner therefore publishes once,
# fails, and never appears on /network. The page reads "few nodes", which is a plausible number for a
# young network, so nothing ever looks wrong.
# So: a host that is a bare IP yields NO url, and the caller says what to do rather than posting into
# the void. `DENDRA_CAPACITY_URL` (allow-listed above) is the operator's way to publish the real one.
capacity_url(){
  local u host
  u="${DENDRA_CAPACITY_URL:-}"
  if [ -z "$u" ] && [ -n "${DENDRA_NODE:-}" ]; then
    host="$(printf '%s' "$DENDRA_NODE" | sed -E 's#^[a-z]+://##; s#:[0-9]+$##; s#/.*$##')"
    case "$host" in
      ''|*[!0-9.]*) [ -n "$host" ] && u="https://$host/capacity" ;;
      *) u="" ;;   # bare IPv4 -> no url at all; see the block above for why
    esac
  fi
  printf '%s' "$u"
}

# The report EXPIRES. Publishing once at install time is not a configuration, it is a countdown: the
# registry ages a report out after 24 h, the node leaves `verified.live_nodes`, /network falls back to
# zero and the public chat closes its composer -- days later, for a cause nobody will connect to this
# install. So the repetition is set up HERE, where the operator is present, rather than described in a
# README that the canonical path exists to spare them from reading.
install_capacity_cron(){
  local script="$REPO/deploy/testnet-miner/publish-capacity.sh"
  local CAP_LOG="${DENDRA_CAPACITY_LOG:-$MINER_KIT/publish-capacity.log}"
  [ -f "$script" ] || return 0
  command -v crontab >/dev/null 2>&1 || {
    say "  [!] no crontab on this host -- schedule this yourself, or the node drops off /network in 24 h:"
    say "        bash $script"
    return 0
  }
  if crontab -l 2>/dev/null | grep -q 'publish-capacity\.sh'; then
    say "  [OK] capacity re-publication already scheduled (crontab)"
    return 0
  fi
  # ⚠️ `bash -lc`, NOT plain `bash`. cron runs with a MINIMAL PATH, and the hardware probe resolves
  # `nvidia-smi` through PATH — under WSL it lives in /usr/lib/wsl/lib, which a non-login shell does not
  # carry. Without the login shell the probe finds no GPU, publishes a CPU-tier inventory with a smaller
  # model than this miner serves, and the page shows that as the operator's own declaration.
  # stderr is kept, not discarded: a refusal that nobody can read is the same silence we are closing.
  if (crontab -l 2>/dev/null; printf "17 * * * * bash -lc 'bash %s' >> %s 2>&1\n" "$script" "$CAP_LOG") | crontab - 2>/dev/null; then
    say "  [OK] capacity re-publication scheduled (hourly, crontab) -- log: $CAP_LOG"
    say "       WITHOUT it the registry drops this node after 24 h and /network returns to zero."
  else
    say "  [!] could not write the crontab. Do it yourself, or the node drops off /network in 24 h"
    say "      (keep the -l: without a login shell the probe sees no GPU and publishes a false tier):"
    say "        (crontab -l 2>/dev/null; echo \"17 * * * * bash -lc 'bash $script' >> $CAP_LOG 2>&1\") | crontab -"
  fi
}

# ---------------------------------------------------------------- validator jail watch
# THE ONE THING A VALIDATOR OPERATOR CANNOT SEE FROM THE OUTSIDE IS THAT THEY ARE JAILED.
# The only health surface published to operators so far, `committee_seed_health`, returns a contributor
# count and the floor it must reach — and nothing about jails, slashes, or which validators are
# expected. A validator that drops out of the consensus set looks, through that endpoint, exactly like
# a vote-extension failure: the number falls, and the cause is not in the answer. Everything below
# exists so the cause has a name, on the operator's own machine, in one command.
VH_SCRIPT="$REPO/deploy/validator_health.sh"

# CRON: YES, HOURLY, AND SILENT WHILE HEALTHY — the reasoning, because the cadence is not obvious.
# The remedy for a jail is a SIGNED TRANSACTION, so no schedule can prevent one: by the time any check
# fires, the slash has already been taken (the chain applies it within the window that produced it).
# What a schedule buys is the END of the outage — the hours or days spent out of the set because nobody
# looked. That value is human-paced, so hourly is enough; a tighter cadence would poll a public
# endpoint harder without shortening a human reaction.
# The output policy matters more than the cadence: a log that grows every hour is one more surface
# nobody reads, which is the exact failure being closed here. So the check writes NOTHING while healthy
# and DELETES any previous alert — the file exists if and only if there is a problem right now, and it
# holds the whole current report. "Does this file exist" is a question that stays answerable at a
# glance, months later, by someone who has forgotten this install.
# Only the VALIDATOR role gets it: a miner has no validator to be jailed.
install_health_cron(){
  local alert="$NODE_KIT/validator-health.ALERT"
  local log_cmd="bash $VH_SCRIPT --cron $alert"
  [ -n "${DENDRA_NODE:-}" ] && log_cmd="DENDRA_NODE=$DENDRA_NODE $log_cmd"
  say ""
  say "  ALERT FILE : $alert"
  say "               It does NOT exist while everything is fine. If it appears, your validator has a"
  say "               problem and the file contains the full report. Nothing else to watch."
  command -v crontab >/dev/null 2>&1 || {
    say "  [!] no crontab on this host — schedule this yourself, or a jail can sit unnoticed for days:"
    say "        $log_cmd"
    return 0
  }
  if crontab -l 2>/dev/null | grep -q 'validator_health\.sh'; then
    say "  [OK] hourly jail watch already scheduled (crontab)"
    return 0
  fi
  # `bash -lc` for the same reason as the capacity cron: cron runs with a minimal PATH and this check
  # needs curl and python3/jq resolved the way the operator's shell resolves them.
  if (crontab -l 2>/dev/null; printf "43 * * * * bash -lc '%s'\n" "$log_cmd") | crontab - 2>/dev/null; then
    say "  [OK] hourly jail watch scheduled (crontab)"
  else
    say "  [!] could not write the crontab. Do it yourself, or a jail can sit unnoticed for days:"
    say "        (crontab -l 2>/dev/null; echo \"43 * * * * bash -lc '$log_cmd'\") | crontab -"
  fi
}

# The numbers an operator needs here — how many blocks may be missed, what that is in minutes, what the
# slash costs — are NOT written into this script. They are governed parameters: any copy of them would
# be true until the first parameter change and wrong afterwards, with nothing to signal the change. So
# the command is RUN, once, and its output is what the operator reads. One source, no copies.
# _jail_params_cmds — the two ways to READ the downtime parameters off the chain. Nothing is copied
# here: this function prints commands, never numbers. It exists because the numbers are governed and a
# copy of them would be true until the first parameter change and silently wrong afterwards.
# The REST guess is derived from DENDRA_NODE's host (the API listener is 1317 next to the RPC's 26657);
# it is a candidate, said as such, not an assertion about the operator's deployment.
_jail_params_cmds(){
  local host rest
  if [ -n "${DENDRA_REST:-}" ]; then rest="${DENDRA_REST%/}"
  elif [ -n "${DENDRA_NODE:-}" ]; then host="${DENDRA_NODE#*://}"; host="${host%%:*}"; rest="http://$host:1317"
  else rest=""; fi
  say "      docker compose -f $NODE_KIT/docker-compose.yml exec -T node dendrad query slashing params -o json"
  say "                                            # once your node is up — always available"
  [ -n "$rest" ] && say "      curl -s $rest/cosmos/slashing/v1beta1/params   # from any machine, IF the operator exposes the API"
}

validator_health_notice(){
  # THE PUBLIC REPOSITORY IS THE CASE THIS BRANCH IS FOR. A one-line "missing — no jail watch
  # installed" leaves the operator with the warning and no way to act on it, which is the same as
  # saying nothing. So the fallback hands over the chain reads the script would have made.
  [ -f "$VH_SCRIPT" ] || {
    warn "deploy/validator_health.sh is not in this checkout — no automatic jail watch can be installed."
    say  "      Read your downtime exposure off the chain yourself, and put a reminder in your own"
    say  "      monitoring: a jailed validator keeps running and stops earning, with no local symptom."
    _jail_params_cmds
    say  "      signed_blocks_window MINUS round(signed_blocks_window x min_signed_per_window) is how"
    say  "      many blocks you may miss inside one window; the one after that jails you. The product"
    say  "      ALONE is how many you must SIGN, which is a different number as soon as the ratio"
    say  "      leaves 0.5 — cosmos-sdk x/slashing/keeper/infractions.go, maxMissed := signedBlocksWindow"
    say  "      - MinSignedPerWindow(), jailing when MissedBlocksCounter > maxMissed."
    say  "      slash_fraction_downtime is what a jail costs; downtime_jail_duration is how long it lasts."
    return 0
  }
  say ""
  say "  =================================================================="
  say "  BEING JAILED IS THE DEFAULT OUTCOME OF UNPLANNED DOWNTIME."
  say "  Miss enough consecutive blocks and the chain jails your validator AND takes a percentage of"
  say "  your stake — any outage that outlasts the window is enough. Nothing warns you:"
  say "  your node keeps running, it simply stops being in the consensus set. The exact figures for"
  say "  THIS chain are printed below, read from the chain itself rather than repeated here:"
  say ""
  say "      bash $VH_SCRIPT                     # identifies your validator from this node"
  say "      bash $VH_SCRIPT dendravaloper1...   # or name it explicitly, from any machine"
  say "  =================================================================="
  # CRLF-safe invocation (the runbooks pipe scripts through `tr -d '\r'`), and DENDRA_HEALTH_CMD so the
  # commands it prints name the repository path rather than the temporary one it is running from.
  local _t; _t="$(mktemp)"
  tr -d '\r' < "$VH_SCRIPT" > "$_t" 2>/dev/null
  DENDRA_HEALTH_CMD="bash $VH_SCRIPT" bash "$_t" 2>&1 | sed 's/^/  | /'
  rm -f "$_t"
  say ""
  say "  A validator has not bonded yet at this point, so the report above says so and stops there."
  say "  Re-run it after create-validator: it then names your validator, your jail state and your margin."
  install_health_cron
}

publish_capacity(){
  [ -f "$HW_PROBE" ] || return 0
  local url json
  url="$(capacity_url)"
  if [ -z "$url" ]; then
    # Silence here is what kept every joiner off /network. Say it, and name the one fix.
    warn "capacity registry URL unknown -> this node will NOT appear on the /network page."
    say  "       Everything else works: this only affects the public capacity display."
    say  "       The address was not derivable because the network publishes a bare IP for the RPC, and"
    say  "       HTTPS on an IP cannot match a certificate issued for a name."
    say  "       Fix: ask the operator to add DENDRA_CAPACITY_URL=<https://name/capacity> to the file"
    say  "            named by CONFIG_URL, or export it yourself before running this script."
    return 0
  fi
  json="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --json "${MINER_ID:-node}" 2>/dev/null | tail -1)"
  case "$json" in '{'*) : ;; *) warn "hardware probe unreadable -> capacity not published"; return 0 ;; esac
  # `miner_ids` is the ON-CHAIN identity this machine actually carries. Without it the registry cannot
  # tell an anonymous declaration from an effectively staked node (see the `verified` subtotal), and the
  # displayed figure becomes whatever anyone claims.
  [ -n "${MINER_ID:-}" ] && json="$(printf '%s' "$json" | sed -E "s#\}\$#,\"miner_ids\":[\"$MINER_ID\"]}#")"
  if curl -fsS -m 10 -X POST -H 'Content-Type: application/json' -d "$json" "$url" >/dev/null 2>&1; then
    say "  [OK] capacity published -> $url (visible on the /network page)"
    say "       This report is valid for 24 h: the registry ages it out, and a node with no fresh report"
    say "       leaves /network AND closes the public chat, which reads the same figure."
    install_capacity_cron
  else
    warn "capacity NOT published ($url) -> /network will stay at zero for this node (the rest works)."
  fi
}

# ---------------------------------------------------------------- model-registry guard (best-effort)
warn_model_registry(){
  command -v dendrad >/dev/null 2>&1 || return 0
  # THE SWITCH BELONGS TO THE JOBS MODULE, NOT TO modelregistry. `enforce_model_registry` is field 14 of
  # dendra.jobs.v1.Params (chain/proto/dendra/jobs/v1/params.proto:63) and it is CreateCommit, in
  # x/jobs, that reads it. x/modelregistry carries the catalogue and audit_judge_model, nothing else.
  # Querying the wrong module answers without the field, the guard reads "not enforced", and the one
  # operator it exists for — the one whose commits are about to be refused for serving an unregistered
  # model — is the one told there is nothing to watch. A missing field and a disabled switch look
  # identical from outside; the module has to be the one that owns the parameter.
  local out enforce
  out=$(dendrad query jobs params -o json --node "${DENDRA_NODE:-tcp://127.0.0.1:26657}" 2>/dev/null) || return 0
  # READ THE FIELD BY ITS KEY — do not count occurrences of a pattern. Comparing a `grep -c` to
  # exactly 1 answers on the SHAPE of the text rather than on the value: an answer carrying the key
  # on two lines counts 2, which is neither 1 nor false, and reads as "not enforced". The wrong answer
  # here is SILENCE towards the one operator this warning exists for — the one whose commits are about
  # to be refused. proto3 omits a boolean worth false, so ABSENT and false are the same claim and earn
  # the same silence; only an explicit `true` speaks. No interpreter is assumed: this kit runs on
  # hosts that carry neither jq nor python3.
  enforce=$(printf '%s' "$out" | sed -nE 's/.*"enforce_model_registry"[[:space:]]*:[[:space:]]*(true|false).*/\1/p' | head -1)
  [ "$enforce" = "true" ] && say "  [i] model registry ENFORCED on-chain: keep DENDRA_MODEL_ID=$DENDRA_MODEL_ID (a different model = commits refused)."
}

# ---------------------------------------------------------------- healthcheck + status
wait_healthy(){
  local kit="$1" svc="$2" rpc_http="${DENDRA_NODE:-}"; rpc_http="${rpc_http/tcp:\/\//http://}"
  say "== [join] healthcheck (bounded ~180 s) =="
  local h0 h1
  h0=$(curl -fsS -m 8 "$rpc_http/status" 2>/dev/null | grep -oE '"latest_block_height": *"[0-9]+"' | tr -dc '0-9')
  sleep 15
  h1=$(curl -fsS -m 8 "$rpc_http/status" 2>/dev/null | grep -oE '"latest_block_height": *"[0-9]+"' | tr -dc '0-9')
  if [ -n "$h0" ] && [ -n "$h1" ] && [ "$h1" -gt "$h0" ] 2>/dev/null; then
    say "  [OK] chain live (height $h0 -> $h1)"
  else
    warn "height not climbing (RPC stuck? consensus?) -> diagnostics below."
  fi
  # THIS CHECK MUST NOT INVERT THE SIGNAL.
  # Searching the logs for "register|create-miner|commit" is not enough: the miner's FAILURE message
  # says "the chain will REFUSE every commit (unauthorized)" — it CONTAINS the word "commit", so the
  # worst failure reads as "[OK] daemon activity detected". And in the nominal case on a quiet network
  # the miner only prints "waiting for jobs": no match, hence a mere warning. Healthy -> lukewarm,
  # broken -> green. Exactly backwards.
  # So KNOWN FAILURE signatures are searched FIRST and stop the loop. Only then is POSITIVE evidence of
  # life looked for.
  local i reg=0 lg=""
  for i in $(seq 1 12); do
    lg="$(docker compose -f "$kit/docker-compose.yml" logs "$svc" 2>/dev/null)"
    # (a) known silent failures: never let them pass for activity
    if printf '%s' "$lg" | grep -qE "REFUS 401|unauthorized|ECHEC INSCRIPTION|ON-CHAIN REGISTRATION FAILED|INCOHERENCE D.IDENTITE|IDENTITY MISMATCH|key not found"; then
      say "  [!] FAILURE DETECTED in the logs of $svc:"
      printf '%s' "$lg" | grep -E "REFUS 401|unauthorized|ECHEC INSCRIPTION|ON-CHAIN REGISTRATION FAILED|INCOHERENCE D.IDENTITE|IDENTITY MISMATCH|key not found" | tail -3 | sed 's/^/      /'
      say "      This node would appear to mine while being MUTE to the network. Not declared OK."
      return 1
    fi
    # (b) POSITIVE evidence: an actual registration, not a word inside an error sentence
    if printf '%s' "$lg" | grep -qE "create-miner|mineur .* pret|miner .* ready|registered on-chain"; then reg=1; break; fi
    sleep 10
  done
  [ "$reg" = 1 ] && say "  [OK] miner alive and registered (positive evidence, not a keyword match)" \
                 || warn "no positive evidence in ~2 min (see logs below; zero traffic = normal: 'waiting for jobs')."
}

print_status_miner(){
  cat <<EOF
============================================================
  Dendra — MINER '$MINER_ID' started
  Served model  : $DENDRA_MODEL_ID  (downloaded and served LOCALLY, on YOUR machine)
  Verification  : semantic embeddings ($DENDRA_EMBED_API_MODEL) — never 'hash' on a real network
  Chain (RPC)   : ${DENDRA_NODE:-?}
  Logs          : docker compose -f $MINER_KIT/docker-compose.yml logs -f miner
  Identity      : Docker volume 'miner-keys' = YOUR miner identity -> do NOT delete it (else re-stake)
  Faucet        : ${FAUCET:-?} (the miner self-funds; if PoW/cap blocks it -> ask on the testnet channel)
  If 0 jobs     : below the miner floor this is NOT a quiet network, it is an ON-CHAIN REFUSAL.
                  OpenJob refuses to create any job while fewer than audit_min_quorum + 1 miners are
                  eligible AT THAT BLOCK, because a job the jury could never conclude would freeze its
                  fee forever. So the first arrivals see ZERO jobs, not "few" — and the count has to be
                  reached by miners running AT THE SAME TIME, not accumulated over days: a miner that
                  has never committed stops being eligible juror_freshness_blocks after it registered,
                  and no job can open to give it the commit that would refresh it. Read both numbers
                  from the chain rather than from here — they are governed and this line is not:
                    dendrad query jobs params -o json     # audit_min_quorum, juror_freshness_blocks
                  If commits are REFUSED instead: your model is not the one the on-chain registry lists.
  Network state : bash $REPO/deploy/validator_health.sh
                  Section 4 answers the one network fact that decides whether YOUR work gets paid: when
                  the committee seed sits below its floor, no audit can conclude and verified work stays
                  unpaid for everyone. It names the cause instead of leaving a falling number to guess at.
============================================================
EOF
}

# ---------------------------------------------------------------- roles
run_miner(){
  say "== [join] role: MINER${JUDGE:+ (+judge)} =="
  verify_genesis_info
  verify_consensus_epoch
  verify_kit_version
  write_gpu_override
  # The kit .env is written from the loaded config (an existing, already-filled .env is kept when no
  # explicit config is provided).
  # If this is a miner-JUDGE, set the judge model (RAM-based heterogeneity) and carry it into the verdict.
  JUDGE_MODEL=""; JUDGE_MODEL_OVERRIDE=""
  if [ "$JUDGE" = 1 ]; then
    # HARDWARE GATE before anything else: an under-powered judge penalises honest miners, so the seat is
    # refused rather than degraded. The node still joins and MINES normally.
    if ! _assert_can_judge; then
      # DECLINING THE JUDGE ROLE DOES NOT DECLINE THE SEAT. The chain draws its jurors among LIVE miners
      # (eligibility = a recent commit, x/jobs/keeper/miner_vitality.go), not among those who agreed to
      # judge. A node that mines without judging is therefore a MUTE SEAT: it raises the bar — 2/3 of
      # the anchored seats — for everyone, and never votes. The arithmetic is direct: at 7 seats the bar
      # is 5, and every mute seat brings the audit closer to being unresolvable.
      # The default stays PERMISSIVE (a public joiner must be able to mine without judging), but the
      # refusal is BLOCKING when the operator declares they came FOR the committee:
      # DENDRA_JUDGE_REQUIRED=1.
      if [ "${DENDRA_JUDGE_REQUIRED:-0}" = "1" ]; then
        die "--judge requested but this hardware cannot judge (deploy/hw_probe.sh: can_judge=false). Joining anyway would occupy a MUTE jury seat and raise the quorum bar for the whole network. Refused on purpose (DENDRA_JUDGE_REQUIRED=1)."
      fi
      JUDGE=0
      warn "--judge ignored: joining as a MINER only -- BUT the chain still draws this miner into audit committees (juror eligibility = a recent commit, not a declared role). A mining-only node is a MUTE SEAT that raises the 2/3 bar for everyone. Re-run on eligible hardware, or set DENDRA_JUDGE_REQUIRED=1 to make this fatal."
    else
      JUDGE_MODEL="$(_pick_judge_model)"
      case "$JUDGE_MODEL" in
        *qwen3:4b*) die "qwen3:4b is FORBIDDEN as a judge (unfair verdicts in a distributed setting). Set DENDRA_JUDGE_MODEL=mistral-nemo.";;
      esac
      # JUDGE-MODEL PRECEDENCE (judge_worker.py::resolve_judge_model):
      #   1. --model-id            <- DENDRA_JUDGE_MODEL_OVERRIDE, written here
      #   2. the ON-CHAIN pin      <- modelregistry.audit_judge_model
      #   3. DENDRA_JUDGE_MODEL_ID <- fallback when the chain pins nothing
      # A value written only at rank 3 DECIDES nothing as soon as the chain pins a model: the operator
      # believes they chose, the committee judges with something else, and the kit downloads 19 GB of a
      # model that will never be used. The operator's EXPLICIT choice is therefore carried to rank 1; a
      # model inferred from the hardware stays at rank 3, where the on-chain pin dominates it (that is
      # the intended homogeneity of the committee).
      if [ -n "${DENDRA_JUDGE_MODEL:-}" ]; then
        JUDGE_MODEL_OVERRIDE="$JUDGE_MODEL"
        say "  [i] JUDGE model = $JUDGE_MODEL — IMPOSED by you: it wins over the on-chain pin."
      else
        JUDGE_MODEL_OVERRIDE=""
        say "  [i] JUDGE model = $JUDGE_MODEL (inferred by deploy/hw_probe.sh). The on-chain pin"
        say "      (modelregistry.audit_judge_model) WINS if it exists; to impose your own,"
        say "      re-run with DENDRA_JUDGE_MODEL=<model>."
      fi
      say "  [i] served model = $DENDRA_MODEL_ID (sized to the measured VRAM). Committee diversity is an announcement gate."
    fi
  fi
  # THE KIT MUST NOT BE BORN DEAF. If `DENDRA_RELAY_TOKEN` never reaches the generated .env, compose
  # sets it EMPTY and relay_client takes a 401 on every request: the machine registers on-chain, takes a
  # jury seat, raises the 2/3 bar for everyone... and NEVER receives a job. Seen from the network it
  # looks like a quiet miner, and the defect sits on the OFFICIAL onboarding path, so every new operator
  # reproduces it.
  # FAIL CLOSED: if the relay REQUIRES a token and none is available, refuse to generate a kit that
  # cannot work. An installation failure is cheaper than a mute node discovered a day later by reading
  # container logs.
  # Testing the PRESENCE of a secret proves nothing about its VALIDITY: a wrong, truncated or expired
  # token would pass a presence check and land on the same silent 401. So the relay is ALWAYS probed,
  # WITH the token when there is one. The header is `X-Dendra-Token` (services/relay.py::_guard), not
  # `Authorization: Bearer`.
  # Existing .env: the token and the judge role are CARRIED OVER when they are not supplied again. A
  # re-run without DENDRA_RELAY_TOKEN would erase the kit's token and make the machine deaf; a re-run
  # without --judge would demote a judge to a MUTE SEAT. Both silently, on a machine that worked.
  _ENV="$MINER_KIT/.env"
  if [ -f "$_ENV" ]; then
    if [ -z "${DENDRA_RELAY_TOKEN:-}" ]; then
      _old="$(sed -n 's/^DENDRA_RELAY_TOKEN=//p' "$_ENV" | head -1)"
      [ -n "$_old" ] && { DENDRA_RELAY_TOKEN="$_old"; say "  [i] relay token CARRIED OVER from the existing .env (not supplied on the command line)"; }
    fi
    # Same rewrite, same hazard: the .env is regenerated from scratch on every run, so a knob set once
    # and not repeated on the next command line would be silently reset to its default. The stake and
    # the PoW time bound are carried over for exactly the reason the token is.
    for _k in DENDRA_MINER_STAKE DENDRA_FAUCET_POW_MAX_S; do
      if [ -z "$(eval printf '%s' "\${$_k:-}")" ]; then
        _old="$(sed -n "s/^$_k=//p" "$_ENV" | head -1)"
        [ -n "$_old" ] && { export "$_k=$_old"; say "  [i] $_k CARRIED OVER from the existing .env ($_old)"; }
      fi
    done
    if [ "$JUDGE" != "1" ] && [ "$(sed -n 's/^DENDRA_MINER_JUDGE=//p' "$_ENV" | head -1)" = "1" ]; then
      say "  [!] This node was a JUDGE and you are re-running WITHOUT --judge: it would become a MUTE SEAT"
      say "      (drawn by the chain, never voting, the 2/3 bar rises for the whole network)."
      say "      Re-run with --judge, or set DENDRA_ACCEPT_MUTE_SEAT=1 if this is deliberate."
      [ "${DENDRA_ACCEPT_MUTE_SEAT:-0}" = "1" ] || exit 2
    fi
  fi
  if [ -n "${DENDRA_RELAY:-}" ]; then
    # TWO probes, because a joiner needs two different questions answered and one does not imply
    # the other. A relay can answer your content reads perfectly and still never hand you a job.
    #   · GET pub/<id>  — "does this relay talk to me at all?"  404 is a PASS: the relay answered and
    #     simply holds nothing under that key, the expected state for a miner that has not worked yet.
    #   · GET list      — "will it ever hand me work?"  This is the WORK QUEUE, and `relay.listing()`
    #     is the miner's ONLY discovery call: behind a token, a machine registers, takes a jury seat
    #     and waits forever with no error to look for. It is not network-mapping metadata — the chain
    #     already publishes the same job-to-miner assignments to anyone who asks.
    # Only 401/403 is a refusal, on either. Neither probe may send a token: the question is what an
    # ANONYMOUS miner is served, and an operator's own secret would answer a different one.
    _probe="${MINER_ID:-preflight}"
    # NO TOKEN HERE — and this line used to send one, two lines under the comment forbidding it.
    # An operator carrying `DENDRA_RELAY_TOKEN` got 404 from a relay that gates /pub behind the
    # token, and the probe then concluded "no shared secret needed to read". That is the wall that
    # is invisible from the inside: the holder of the secret certifies an open door that a joiner
    # without it finds shut. The question is what an ANONYMOUS miner is served, so the probe has to
    # be anonymous — exactly like the queue probe below, which already was.
    _code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' \
             "${DENDRA_RELAY%/}/pub/$_probe" 2>/dev/null || echo 000)"
    case "$_code" in
      200|404) say "  [OK] relay $DENDRA_RELAY answers this node (HTTP $_code) — no shared secret needed to read." ;;
      401|403)
        say "  [!] The relay $DENDRA_RELAY REFUSES even a read (HTTP $_code)."
        say "      This miner would register on-chain, take a jury seat, and NEVER receive a job —"
        say "      one silent 401 per request. Stopping here is cheaper."
        say "      A current relay serves reads to anyone: getting this means the operator is running"
        say "      an older build that gates every route behind a shared token. Ask them to update,"
        say "      or pass one:  DENDRA_RELAY_TOKEN=<token> bash deploy/join.sh ..."
        exit 2 ;;
      000) say "  [!] relay $DENDRA_RELAY UNREACHABLE — there is no proof this node will be served."
           say "      This is not a green light: check the IP and the firewall before relying on this machine." ;;
      *)   say "  [i] relay $DENDRA_RELAY: HTTP $_code (neither served nor refused — keep an eye on it)" ;;
    esac

    # THE WORK QUEUE ITSELF. Probed WITHOUT the token even when one is set: the question a joiner
    # needs answered is whether an ANONYMOUS miner is served, and sending an operator's secret here
    # answers a different one — which is how a closed queue stays invisible to whoever holds it.
    _lcode="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "${DENDRA_RELAY%/}/list" 2>/dev/null || echo 000)"
    case "$_lcode" in
      200) say "  [OK] the work queue (GET list) is open — this relay can hand this miner a job." ;;
      401|403)
        say "  [!] The relay $DENDRA_RELAY REFUSES the WORK QUEUE (GET list -> HTTP $_lcode)."
        say "      Reads of your own content may work, but this is the only route by which a miner"
        say "      learns a job awaits it. This machine would register on-chain, take a jury seat,"
        say "      and wait forever WITHOUT a single error to look for. Stopping here is cheaper."
        say "      A current relay serves this queue to anyone. Ask the operator to update."
        exit 2 ;;
      000) say "  [!] GET list UNREACHABLE — no proof this miner would ever be handed work." ;;
      *)   say "  [i] GET list: HTTP $_lcode (neither served nor refused — keep an eye on it)" ;;
    esac
  fi
  # ── THE MINER'S OWN NODE — the default, and the reason it is the default ──────────────────────
  # A miner needs a view of the chain: which jobs are open, what the seed is, whether its commit landed.
  # Pointing every miner at the operator's public RPC makes that operator a single point of failure (its
  # endpoint goes down, every miner goes blind at once), a point of censorship, and a party that has to
  # be TRUSTED about the state of a chain whose whole purpose is not to require trust.
  # A miner already runs a machine 24/7 and already needs the chain, so it is the natural node operator.
  # `DENDRA_NODE` keeps pointing at the operator's public RPC throughout this script — it is what the
  # relay host is derived from, and what state-sync bootstraps against. What changes is the value handed
  # to the CONTAINER, and only that.
  local MINER_RPC="${DENDRA_NODE:-}"
  if [ "$OWN_NODE" = 1 ]; then
    say "== [join] this miner will run its OWN node (recommended). Use --remote-rpc to depend on the operator's instead. =="
    start_local_node
    MINER_RPC="tcp://host.docker.internal:26657"
    say "  [OK] miner will read the chain from its own node ($MINER_RPC)"
  else
    say "  [!] --remote-rpc: this miner depends on ${DENDRA_NODE:-?}. If that endpoint stops, this miner"
    say "      goes blind, and it cannot verify for itself what it is told about the chain."
  fi
  if [ -n "${DENDRA_NODE:-}" ] && [ -n "${DENDRA_RELAY:-}" ] && [ -n "${FAUCET:-}" ]; then
    cat > "$MINER_KIT/.env" <<EOF
# Generated by deploy/join.sh $(date -u +%FT%TZ)
DENDRA_NODE=$MINER_RPC
# PUBLIC capacity registry. It is written here EXPLICITLY because DENDRA_NODE above is container-local in
# the default setup: anything re-deriving the registry from it would target host.docker.internal and post
# into the void, which is invisible until the node silently drops off /network 24 h later.
DENDRA_CAPACITY_URL=$(capacity_url)
DENDRA_RELAY=$DENDRA_RELAY
DENDRA_RELAY_TOKEN=${DENDRA_RELAY_TOKEN:-}
FAUCET=$FAUCET
DENDRA_MODEL_ID=$DENDRA_MODEL_ID
DENDRA_EMBED_MODE=$DENDRA_EMBED_MODE
DENDRA_EMBED_API_MODEL=$DENDRA_EMBED_API_MODEL
MINER_ID=$MINER_ID
DENDRA_MINER_JUDGE=$JUDGE
# TWO KNOBS THAT ONLY EXIST IF THEY ARE WRITTEN HERE. compose forwards to the container ONLY the
# variables its environment: block names, and it reads their values from THIS file — not from the
# environment of the shell that ran join.sh. So "DENDRA_MINER_STAKE=60000 bash deploy/join.sh" used to
# be dropped in silence: the operator believed they had set a registration stake, the container never
# saw the variable, and the only trace was a create-miner that behaved differently from what was asked.
# Empty stays the recommended value for the stake (the miner then reads min_stake FROM THE CHAIN, a
# governed parameter); what changes is that an explicit choice is no longer cancelled mutely.
DENDRA_MINER_STAKE=${DENDRA_MINER_STAKE:-}
DENDRA_FAUCET_POW_MAX_S=${DENDRA_FAUCET_POW_MAX_S:-}
# Rank 3 (fallback when the chain pins no model) AND the declaration carried by the on-chain verdict:
# it is what makes judge-model diversity MEASURABLE.
DENDRA_JUDGE_MODEL_ID=$JUDGE_MODEL
# Rank 1: filled ONLY on an explicit choice (DENDRA_JUDGE_MODEL), empty otherwise.
DENDRA_JUDGE_MODEL_OVERRIDE=$JUDGE_MODEL_OVERRIDE
$(if [ "$JUDGE" = 1 ]; then
    # TWO ENGINES, NOT ONE. Leaving this variable EMPTY makes the judge run on the MINER's Ollama: the
    # judge model (MoE ~19 GB) evicts the mining one, the miner exits on a FATAL Ollama ReadTimeout,
    # and every assigned job stays open — the machine looks alive and serves nothing. ollama-cpu is
    # the kit's dedicated CPU instance.
    # (No backticks: this comment lives inside an UNQUOTED heredoc, where bash executes them.)
    echo "DENDRA_JUDGE_ENDPOINT=http://ollama-cpu:11434"
  fi)
EOF
    # The .env carries the RELAY TOKEN. Default permissions can leave it readable AND writable by every
    # user of the machine. A secrets file is closed at creation time, never hardened afterwards.
    chmod 600 "$MINER_KIT/.env" 2>/dev/null || true
    say "  [OK] $MINER_KIT/.env written"
  fi
  warn_model_registry
  # The judge lives in PROFILED services (`judge`). Without this flag, docker compose starts NEITHER
  # `ollama-cpu` NOR its model download: `DENDRA_JUDGE_ENDPOINT` would point at a non-existent host and
  # every verdict would fail — silently, from the miner's point of view.
  COMPOSE_PROFILES_ARG=""
  [ "$JUDGE" = 1 ] && COMPOSE_PROFILES_ARG="--profile judge"
  ( cd "$MINER_KIT" && docker compose $COMPOSE_PROFILES_ARG up -d --build ) || die "docker compose up failed (see output above)"
  publish_capacity
  wait_healthy "$MINER_KIT" miner
  print_status_miner
  [ "$JUDGE" = 1 ] && say "  [i] JUDGE mode: reveal_worker+judge_worker start once the miner key is ready (logs: grep judge)."
}

# ================================================================ SYNC WATCH
# WHY THIS IS A PROGRESS WATCH AND NOT A TIMEOUT.
#
# A FIXED DEADLINE ON A SYNC IS A WRONG ANSWER WAITING TO BE PRINTED. Replaying a chain from block 1
# takes as long as the chain is long: on a host applying ~23 blocks per second, ~92 000 blocks need
# about an hour, and that number grows every day the network runs. Any wall-clock limit is therefore
# certain to fire on a node that is working — and the operator, told to "check SEEDS/network", goes
# looking at the one thing that is not the cause.
#
# Three defects, and the number is the least interesting one.
#
#  (a) A DURATION CANNOT TELL "SLOW" FROM "DEAD". The chain only grows, hosts differ by an order of
#      magnitude in disk throughput, and any constant is wrong for somebody — today, or in a month when
#      the chain is twice as long. What separates the two cases is PROGRESSION. So the bound below is on
#      TIME WITHOUT A NEW BLOCK, never on total time: a sync that crawls is waited on for as long as it
#      crawls FORWARD, and one that has stopped is called out in minutes rather than in half an hour.
#
#  (b) THE MESSAGE NAMED A CAUSE THE MEASUREMENT CONTRADICTS. "check SEEDS/network" sends the operator
#      to inspect peers and firewall, neither of which was involved, while the one fact that mattered
#      was never said: the CONTAINER keeps syncing after this script returns. Someone who has just
#      waited an hour and reads "FATAL" concludes they lost the hour. So the failure path reports what
#      was OBSERVED — height reached, network head, rate this node actually sustained, how long since
#      the height last moved — and says explicitly that nothing is lost.
#
#  (c) A TEXTUAL PREDICATE ON JSON CANNOT HAVE THREE ANSWERS. The old line grepped the status output for
#      `"catching_up": *(true|false)`. A failed `docker exec`, a node not yet listening, a status
#      document in a shape it did not expect, and an encoder that OMITS a boolean worth false all
#      produce the same empty string. Two questions — "is it caught up" and "could I read the answer" —
#      collapsed into one variable. The reader below returns `unreadable` as a value of its own and
#      never lets it count as caught up.

# ---- reading a status document --------------------------------------------------------------------
# A real parser is preferred and a by-key text read is the documented FALLBACK, announced when it is
# used. This kit does not require an interpreter (see warn_model_registry: it runs on hosts that carry
# neither jq nor python3), so demanding one here would turn a sync wait into a hard prerequisite.
# The FALLBACK still answers three states, because what makes a probe unreadable is decided on the
# DOCUMENT (is this a status document at all), not on whether one field could be extracted from it.
_SYNC_JSON=""

# _status_fields RAW -> "<height> <true|false|absent>", or the single word NOSTATUS.
# Extraction only. It decides nothing: `absent` is reported as `absent` and the caller applies the
# proto3 rule, so the place where "omitted" becomes "false" is one line, in one function, below.
_status_fields(){
  local raw="$1"
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf 'NOSTATUS\n'; return 0;; esac
  case "$_SYNC_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import json,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("NOSTATUS"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
s=None
if isinstance(d,dict):
    for k in ("sync_info","SyncInfo"):
        if isinstance(d.get(k),dict): s=d[k]; break
if s is None: print("NOSTATUS"); raise SystemExit(0)
try: h=int(str(s.get("latest_block_height",0)))
except Exception: h=0
if "catching_up" in s:
    v=s["catching_up"]; c="true" if (v is True or v=="true") else "false"
else:
    c="absent"
print("%d %s"%(h,c))
' 2>/dev/null
      ;;
    jq)
      # `.catching_up // "absent"` WOULD BE WRONG and is the reason `has` is used instead: jq treats
      # `false` as empty for the alternative operator, so a node that has caught up would be reported
      # as a node whose field is missing.
      printf '%s' "$raw" | jq -r '
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | ($d.sync_info // $d.SyncInfo) as $s
        | if ($s|type)!="object" then "NOSTATUS"
          else ((($s.latest_block_height // 0)|tostring) + " "
                + (if ($s|has("catching_up")) then ($s.catching_up|tostring) else "absent" end))
          end' 2>/dev/null
      ;;
    *)
      # Fallback: fields read BY THEIR KEY (never a count of occurrences, never a match on a whole
      # `"key": value` pair). The document is qualified FIRST — no sync_info, no status.
      case "$raw" in *'"sync_info"'*|*'"SyncInfo"'*) : ;; *) printf 'NOSTATUS\n'; return 0;; esac
      local h c
      h="$(printf '%s' "$raw" | sed -nE 's/.*"latest_block_height"[[:space:]]*:[[:space:]]*"?([0-9]+)"?.*/\1/p' | head -1)"
      c="$(printf '%s' "$raw" | sed -nE 's/.*"catching_up"[[:space:]]*:[[:space:]]*"?(true|false)"?.*/\1/p' | head -1)"
      printf '%s %s\n' "${h:-0}" "${c:-absent}"
      ;;
  esac
}

# ---- THE READER IS CONFRONTED BEFORE IT IS TRUSTED -------------------------------------------------
# A reader is picked here from what the host happens to carry, so at most one of the three branches
# above ever runs on any given machine — and the branch that runs on somebody else's machine is the one
# that was never executed on ours. A wrong option, a jq expression that does not compile, a python that
# is really python2: each of those returns NOTHING, which this loop would read as "unreadable" forever
# and turn into a 15-minute wait ending in a false report. So the selected reader is made to answer a
# document whose correct answer is known, and a reader that fails its own fixture is not used.
# Fixture A is the RPC envelope (tests the `result` unwrap) and carries catching_up TRUE.
# Fixture B is the bare `dendrad status` shape and carries catching_up FALSE — the case a jq
# alternative operator silently swallows, which is exactly the defect that would ship unnoticed.
_SYNC_FIX_A='{"jsonrpc":"2.0","id":-1,"result":{"node_info":{"network":"dendra"},"sync_info":{"latest_block_hash":"AB12","latest_block_height":"55882","catching_up":true}}}'
_SYNC_FIX_B='{"node_info":{"network":"dendra"},"sync_info":{"latest_block_height":"92405","earliest_block_height":"1","catching_up":false},"validator_info":{"voting_power":"0"}}'
_sync_reader_ok(){
  [ "$(_status_fields "$_SYNC_FIX_A")" = "55882 true" ] || return 1
  [ "$(_status_fields "$_SYNC_FIX_B")" = "92405 false" ] || return 1
}
# ── THE AGE OF THE LAST BLOCK — THE ONLY VALUE THAT TELLS A LIVE CHAIN FROM A STOPPED ONE ───────────
# `catching_up=false` does not say the chain is advancing: it says this node has applied everything
# it was offered. A network that stopped seven hours ago answers exactly like a healthy one, and the
# node really is perfectly synchronised — with a dead chain. The height gives nothing away either:
# it is high, it is simply motionless. The one quantity that separates the two is the block
# TIMESTAMP, and nothing here read it (zero occurrences).
#
# This reader returns that timestamp in EPOCH SECONDS, never an age: the age is computed by the
# caller through `_sync_now`, the clock already indirected for the bench. A reader calling its own
# clock would be untestable, and three branches with three clocks would be three defects to hunt.
#
# ⚠️ AND IT CAN RETURN `?`. Converting an ISO date to epoch in portable shell is not a given:
# `date -d` is a GNU extension. A host without python3, without jq and without GNU date cannot date
# the block — we return `?` then, which the caller DISPLAYS as unknown and NEVER reads as fresh.
# Same rule as everywhere here: a field absent means zero, while not having been able to find out is
# a distinct sentinel.
# ⛔ And that `?` must NOT disqualify the `_status_fields` reader: dating the block is a supplement,
# reading the height is the service. So this function has its own probe, apart from `_sync_reader_ok`.
_status_block_epoch(){
  local raw="$1" ts
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf '?\n'; return 0;; esac
  case "$_SYNC_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import calendar,json,re,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("?"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
s=None
if isinstance(d,dict):
    for k in ("sync_info","SyncInfo"):
        if isinstance(d.get(k),dict): s=d[k]; break
if s is None: print("?"); raise SystemExit(0)
t=s.get("latest_block_time")
if not isinstance(t,str) or not t: print("?"); raise SystemExit(0)
m=re.match(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})", t)
if not m: print("?"); raise SystemExit(0)
print(calendar.timegm(tuple(int(x) for x in m.groups())+(0,0,0)))
' 2>/dev/null || printf '?\n'
      ;;
    jq)
      # The fractional seconds are STRIPPED before conversion: `fromdateiso8601` rejects
      # `…:26.772062712Z` and would raise, i.e. yield `?`, on a perfectly readable document.
      printf '%s' "$raw" | jq -r '
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | ($d.sync_info // $d.SyncInfo) as $s
        | if ($s|type)!="object" or (($s.latest_block_time // "")|type)!="string"
             or ($s.latest_block_time // "")=="" then "?"
          else (try (($s.latest_block_time|sub("\.[0-9]+";"")|sub("[+-][0-9]{2}:[0-9]{2}$";"Z")
                     |(if endswith("Z") then . else .+"Z" end))|fromdateiso8601|tostring)
                catch "?")
          end' 2>/dev/null || printf '?\n'
      ;;
    *)
      # Fallback: the string is extracted BY ITS KEY, then handed to `date`. If that `date` is not
      # GNU it rejects `-d` and we return `?` — an admission, not an approximation.
      case "$raw" in *'"latest_block_time"'*) : ;; *) printf '?\n'; return 0;; esac
      ts="${raw#*\"latest_block_time\"}"; ts="${ts#*:}"; ts="${ts#*\"}"; ts="${ts%%\"*}"
      case "$ts" in [0-9][0-9][0-9][0-9]-*) : ;; *) printf '?\n'; return 0;; esac
      date -u -d "$ts" +%s 2>/dev/null || printf '?\n'
      ;;
  esac
}

# The same gesture as `_sync_reader_ok`, on a document whose answer is known: 2020-01-01T00:00:00Z is
# 1577836800. It is NOT called to pick the reader — it is called to learn whether dating is
# available at all, and to SAY SO when it is not.
_BLOCK_TIME_FIX='{"result":{"sync_info":{"latest_block_height":"7","latest_block_time":"2020-01-01T00:00:00.123456789Z","catching_up":false}}}'
_block_time_available(){ [ "$(_status_block_epoch "$_BLOCK_TIME_FIX")" = "1577836800" ]; }

# Past this, a last block stops being proof of life. The value is an ORDER OF MAGNITUDE owned as
# such, not a derivation: the interval between blocks is fixed by no consensus parameter (it emerges
# from `timeout_commit` and from propagation), so no number on the chain honestly turns into this
# threshold. 300 s lets a hiccup, a restart or a slightly skewed clock through, and does not let a
# stop through. ⚠️ The comparison brings THE CLOCK OF THE JOINING MACHINE into the verdict: a host
# with a wrong date sees an old block that is not old. That is why this finding WARNS and blocks
# nothing — the node itself is genuinely up to date.
: "${DENDRA_STALE_BLOCK_SECS:=300}"

_SYNC_READER_PICKED=0
_sync_pick_reader(){
  [ "$_SYNC_READER_PICKED" = 1 ] && return 0
  _SYNC_READER_PICKED=1
  local c
  for c in python3 jq __by_key__; do
    case "$c" in
      python3|jq) command -v "$c" >/dev/null 2>&1 || continue; _SYNC_JSON="$c";;
      *) _SYNC_JSON="";;
    esac
    if _sync_reader_ok; then
      [ -n "$_SYNC_JSON" ] || warn "neither python3 nor jq on this host: the sync check reads the status fields BY KEY from the text. It still tells 'caught up' from 'still syncing' from 'unreadable' — install jq if you want the document parsed."
      return 0
    fi
    warn "the '$c' status reader failed its own fixture (a document whose answer is known) -> trying the next reader."
  done
  _SYNC_JSON=""
  warn "no working status reader on this host. The sync wait will report 'unreadable' rather than guess — install jq or python3."
  return 1
}

# _sync_state RAW -> "<state> <height> <catching_up>"
#   state  : synced | syncing | unreadable   <- THREE answers. `unreadable` is a sentinel of its own.
#   height : an integer inside a document that parsed; `?` when there is no document to read.
#
# THE ZERO RULE, APPLIED TWICE AND IN OPPOSITE DIRECTIONS:
#   - Inside a document that PARSED, an omitted boolean is the zero of its type, i.e. false. Absent and
#     false are the same claim and get the same treatment.
#   - But `catching_up` false is NOT sufficient, because `latest_block_height` is a uint and gets
#     omitted at zero too. A node that has applied nothing reports, by omission alone, "height 0, not
#     catching up" — which read literally announces a finished sync at block zero. So the claim is
#     BOUNDED FROM BELOW: caught up requires 0 < height, not merely "not catching up".
_sync_state(){
  local f h c state
  f="$(_status_fields "$1")"
  case "$f" in ''|NOSTATUS*) printf 'unreadable ? ?\n'; return 0;; esac
  h="${f%% *}"; c="${f##* }"
  case "$h" in ''|*[!0-9]*) printf 'unreadable ? ?\n'; return 0;; esac
  case "$c" in true|false|absent) : ;; *) printf 'unreadable ? ?\n'; return 0;; esac
  if [ "$c" != "true" ] && [ "$h" -gt 0 ]; then state=synced; else state=syncing; fi
  printf '%s %s %s\n' "$state" "$h" "$c"
}

# ---- probes ----------------------------------------------------------------------------------------
# stderr is CAPTURED, not discarded: some SDK builds print `status` on stderr, and `2>/dev/null` on a
# probe turns "the answer went to the other stream" into "the node never answered". Everything before
# the first brace is dropped by the reader, so a docker warning in front of the document is harmless.
_probe_node_status(){ docker compose -f "$NODE_KIT/docker-compose.yml" exec -T node dendrad status 2>&1; }

# The network head, from the operator's public RPC. Best effort by design: it decorates the report and
# never decides anything, so `?` — not a number, not a zero — is what an unanswered RPC yields.
_probe_net_head(){
  local rpc raw line
  rpc="${DENDRA_NODE:-}"; [ -n "$rpc" ] || { printf '?'; return 0; }
  rpc="${rpc/tcp:\/\//http://}"
  raw="$(curl -fsS -m 8 "$rpc/status" 2>/dev/null)" || { printf '?'; return 0; }
  line="$(_sync_state "$raw")"
  case "$line" in unreadable*) printf '?';; *) line="${line#* }"; printf '%s' "${line%% *}";; esac
}

# THE BENCH HAS TO DRIVE THE SHIPPED LOOP, NOT A COPY OF IT — a bench that re-implements the decision
# proves things about the copy. So the probe, the head and the clock are indirected. The indirection is
# honoured ONLY under DENDRA_SELFTEST=1, and none of these names is in the CONFIG_URL allow-list, so no
# downloaded network-info.txt can reach them.
_sync_probe(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_PROBE:-}" ]; then "$DENDRA_SELFTEST_PROBE"; return 0; fi
  _probe_node_status
}
_sync_head(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_HEAD:-}" ]; then printf '%s' "$DENDRA_SELFTEST_HEAD"; return 0; fi
  _probe_net_head
}
_sync_now(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_CLOCK:-}" ]; then
    cat "$DENDRA_SELFTEST_CLOCK" 2>/dev/null || printf '0'
    return 0
  fi
  date +%s
}
_sync_nap(){ [ "${DENDRA_SELFTEST:-0}" = "1" ] && return 0; sleep "$1"; }

# ---- THE FORK DETECTOR -----------------------------------------------------------------------------
# A node can be up, peered, and NEVER able to follow this chain. When the binary it was built from
# applies a block to a different result than the network did, CometBFT rejects that block, drops the
# peer, reconnects, and starts over -- forever. The height freezes. From the inside this is
# indistinguishable from a slow disk or a slow link, so a sync wait that reads only height and
# catching_up CANNOT tell the two apart, and reports the reassuring one.
# A single transaction is enough to cause it: one the chain refuses and a stale binary accepts leaves
# the two sides holding different state from that block onward.
#
# THE OBVIOUS COMPARISON IS A FALSE NEGATIVE, AND THAT IS THE WHOLE DIFFICULTY.
# Confronting the block headers this node already HOLDS against the network's would always agree: a node
# stops at the last block it ACCEPTED, so every header it owns predates the disagreement. The value that
# differs is the state computed AFTER that block -- which this node publishes in `latest_app_hash` and
# which the network publishes in the header of the NEXT block. So the confrontation is asymmetric on
# purpose:
#     local  /status            -> sync_info.latest_app_hash   (state after height h, as WE computed it)
#     public /block?height=h+1  -> block.header.app_hash        (state after height h, as THEY published)
#
# THREE ANSWERS, NEVER TWO. `unknown` is a verdict of its own and is never folded into `agree`: an
# unread side costs nothing on a path that is already failing, while a guess costs an operator a bond
# placed on a fork.

# _apphash RAW -> the application state hash carried by a status OR a block document, uppercased, or
#                 NOHASH. One reader for both shapes, because each document carries exactly one of the
#                 two fields -- two readers would be two things to confront instead of one.
_FORK_JSON=""
_apphash(){
  local raw v; raw="$1"
  # SAME PROLOGUE AS `_status_fields`, AND FOR THE SAME REASON. Both read the output of the SAME probe,
  # and `docker compose` writes warnings ("the attribute `version` is obsolete") to that stream ahead of
  # the document. A reader without this line returns NOHASH on a perfectly good status, which this file
  # then reads as "no hash" -- i.e. `unknown` -- on every host whose compose is chatty. Measured: the
  # clean document answers, the same document behind one warning line did not.
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf 'NOHASH\n'; return 0;; esac
  case "$_FORK_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import json,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("NOHASH"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
v=""
if isinstance(d,dict):
    s=d.get("sync_info") or d.get("SyncInfo")
    if isinstance(s,dict) and isinstance(s.get("latest_app_hash"),str): v=s["latest_app_hash"]
    if not v:
        b=d.get("block")
        if isinstance(b,dict):
            h=b.get("header")
            if isinstance(h,dict) and isinstance(h.get("app_hash"),str): v=h["app_hash"]
print(v.strip().upper() if v.strip() else "NOHASH")
' 2>/dev/null
      ;;
    jq)
      printf '%s' "$raw" | jq -r '
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | ((($d.sync_info // $d.SyncInfo // {}).latest_app_hash) // ($d.block.header.app_hash) // "") as $v
        | if ($v|type)=="string" and ($v|length)>0 then ($v|ascii_upcase) else "NOHASH" end' 2>/dev/null
      ;;
    *)
      # Fields read BY THEIR KEY, never by counting occurrences and never by matching a whole pair.
      v="$(printf '%s' "$raw" | sed -nE 's/.*"latest_app_hash"[[:space:]]*:[[:space:]]*"([0-9A-Fa-f]+)".*/\1/p' | head -1)"
      [ -n "$v" ] || v="$(printf '%s' "$raw" | sed -nE 's/.*"app_hash"[[:space:]]*:[[:space:]]*"([0-9A-Fa-f]+)".*/\1/p' | head -1)"
      if [ -n "$v" ]; then printf '%s' "$v" | tr 'a-f' 'A-F'; printf '\n'; else printf 'NOHASH\n'; fi
      ;;
  esac
}

# THE READER IS CONFRONTED BEFORE IT IS TRUSTED, and the THIRD fixture is the one that matters.
# A document carrying NEITHER field must answer NOHASH. A reader that returned the empty string there
# would make two unreadable sides compare EQUAL -- a fork reported as agreement, on the reassuring side,
# indistinguishable from a healthy node. Fixture S is lowercase on purpose: nothing guarantees the two
# sources agree on case, and a case-sensitive comparison on a hex string invents forks that do not
# exist -- the mirror image of the defect this whole block exists to prevent.
_FORK_FIX_S='{"jsonrpc":"2.0","id":-1,"result":{"sync_info":{"latest_block_height":"10","latest_app_hash":"0164de52dc759ca7","catching_up":true}}}'
_FORK_FIX_B='{"jsonrpc":"2.0","id":-1,"result":{"block":{"header":{"height":"11","app_hash":"F6C36C046170960C"},"data":{"txs":[]}}}}'
_FORK_FIX_N='{"jsonrpc":"2.0","id":-1,"result":{"node_info":{"network":"dendra"}}}'
_fork_reader_ok(){
  [ "$(_apphash "$_FORK_FIX_S")" = "0164DE52DC759CA7" ] || return 1
  [ "$(_apphash "$_FORK_FIX_B")" = "F6C36C046170960C" ] || return 1
  [ "$(_apphash "$_FORK_FIX_N")" = "NOHASH" ] || return 1
}
_FORK_READER_PICKED=0
_FORK_READER_RC=1
_fork_pick_reader(){
  [ "$_FORK_READER_PICKED" = 1 ] && return "$_FORK_READER_RC"
  local c
  _FORK_READER_PICKED=1
  for c in python3 jq __by_key__; do
    case "$c" in
      python3|jq) command -v "$c" >/dev/null 2>&1 || continue; _FORK_JSON="$c";;
      *) _FORK_JSON="";;
    esac
    if _fork_reader_ok; then _FORK_READER_RC=0; return 0; fi
    warn "the '$c' app-hash reader failed its own fixture (a document whose answer is known) -> trying the next reader."
  done
  _FORK_JSON=""; _FORK_READER_RC=1
  warn "no app-hash reader on this host answered its fixtures: a frozen height will be reported as 'unknown', never as 'in agreement'."
  return 1
}

# Same indirection as the sync loop, same reason, same guard: honoured ONLY under DENDRA_SELFTEST=1, and
# none of these names is in the CONFIG_URL allow-list, so no downloaded network-info.txt reaches them.
_fork_probe_block(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_BLOCK:-}" ]; then "$DENDRA_SELFTEST_BLOCK" "$1"; return $?; fi
  local rpc; rpc="${DENDRA_NODE:-}"; [ -n "$rpc" ] || return 1
  rpc="${rpc/tcp:\/\//http://}"
  curl -fsS -m 10 "$rpc/block?height=$1" 2>/dev/null
}
_fork_logs(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_LOGS:-}" ]; then "$DENDRA_SELFTEST_LOGS"; return 0; fi
  docker compose -f "$NODE_KIT/docker-compose.yml" logs --tail 200 node 2>/dev/null
}

FORK_STATE=unknown; FORK_WHY=""; FORK_LOCAL=""; FORK_NET=""; FORK_LOG=""; _FORK_SEEN_H=""
# fork_verdict HEIGHT -> sets FORK_STATE to fork | agree | unknown. It always returns 0: it NAMES a
# failure, it does not decide whether one happened.
fork_verdict(){
  local h hdoc doc fields next raw a b
  FORK_STATE=unknown; FORK_WHY=""; FORK_LOCAL=""; FORK_NET=""; FORK_LOG=""
  h="$1"
  case "$h" in ''|*[!0-9]*) FORK_WHY="the local height is not a number, so there is nothing to confront"; return 0;; esac
  [ "$h" -gt 0 ] || { FORK_WHY="this node has applied no block yet, so there is nothing to confront"; return 0; }
  _fork_pick_reader || { FORK_WHY="no reader on this host answered a document whose answer is known"; return 0; }
  # ONE DOCUMENT, ONE HEIGHT, ONE HASH -- AND THE HEIGHT COMES FROM THE DOCUMENT, NEVER FROM THE CALLER.
  # The height the loop holds was read on an earlier pass. Pairing it with a hash read now compares the
  # state after height h+1 against the block published for height h, and prints FORK with total
  # confidence on a node that is merely making progress. The two values have to come out of the SAME
  # read or they describe two different instants; the argument is kept only to refuse height 0.
  doc="$(_sync_probe)"
  a="$(_apphash "$doc")"
  case "$a" in ''|NOHASH) FORK_WHY="this node's own status carries no application hash"; return 0;; esac
  fields="$(_status_fields "$doc")"
  case "$fields" in ''|NOSTATUS*) FORK_WHY="the status carries a hash but no readable height: the pair cannot be trusted, so nothing is concluded"; return 0;; esac
  hdoc="${fields%% *}"
  case "$hdoc" in ''|*[!0-9]*) FORK_WHY="the status carries a hash but an unreadable height, so nothing is concluded"; return 0;; esac
  [ "$hdoc" -gt 0 ] || { FORK_WHY="this node has applied no block yet, so there is nothing to confront"; return 0; }
  h="$hdoc"
  next=$(( hdoc + 1 ))
  raw="$(_fork_probe_block "$next")" || raw=""
  [ -n "$raw" ] || { FORK_WHY="block $next was not served, so the network's answer is unknown -- which is NOT an agreement"; return 0; }
  b="$(_apphash "$raw")"
  case "$b" in ''|NOHASH) FORK_WHY="block $next as served carries no application hash"; return 0;; esac
  FORK_LOCAL="$a"; FORK_NET="$b"
  if [ "$a" = "$b" ]; then
    FORK_STATE=agree
    FORK_WHY="both sides hold $a after height $h: this node is behind, nothing more"
  else
    FORK_STATE=fork
    FORK_WHY="after height $h this node computes $a while the network published $b"
    # Corroboration, never the measurement: the node usually says it itself, but a phrasing that
    # changes with the CometBFT release must never be what decides.
    FORK_LOG="$(_fork_logs | grep -m1 -F 'wrong Block.Header.AppHash' || true)"
  fi
  return 0
}

# ---- tunables --------------------------------------------------------------------------------------
# Named in the unit each one actually counts, and overridable — because the right value depends on a
# disk, not on an opinion held here.
#   STALL : how long the height may fail to increase before this script stops waiting. It is NOT a
#           budget for the sync; the sync may take all day as long as it advances.
#   MAX   : an absolute ceiling so an unattended run cannot loop forever. Deliberately far above any
#           plausible replay (24 h) — it is a stop, not a verdict.
DENDRA_SYNC_STALL_SECS="${DENDRA_SYNC_STALL_SECS:-900}"
DENDRA_SYNC_MAX_SECS="${DENDRA_SYNC_MAX_SECS:-86400}"
DENDRA_SYNC_POLL_SECS="${DENDRA_SYNC_POLL_SECS:-15}"
DENDRA_SYNC_REPORT_SECS="${DENDRA_SYNC_REPORT_SECS:-120}"

# Observed rate, carried as hundredths of a block per second so the whole loop stays in integers.
SYNC_H="?"; SYNC_HEAD="?"; SYNC_RATE_CBS=""; SYNC_STALL_FOR=0; SYNC_ELAPSED=0; SYNC_WHY=""; SYNC_CU="?"

_fmt_rate(){
  case "${1:-}" in
    ''|*[!0-9]*) printf 'not measurable yet';;
    *) printf '%d.%02d blocks/s' $(( $1 / 100 )) $(( $1 % 100 ));;
  esac
}

# announce_sync_plan — said BEFORE the wait, because the cost of waiting is only bearable when it was
# announced. THE ROOT CAUSE OF THE WAIT IS NAMED HERE: with no state-sync trust point published, the
# node replays every block from the genesis.
announce_sync_plan(){
  local head est
  _sync_pick_reader || true
  head="$(_sync_head)"
  say ""
  if [ -z "${STATESYNC_RPC:-}" ] || [ -z "${STATESYNC_TRUST_HEIGHT:-}" ] || [ -z "${STATESYNC_TRUST_HASH:-}" ]; then
    say "  [i] NO STATE-SYNC TRUST POINT in the configuration you loaded (STATESYNC_RPC,"
    say "      STATESYNC_TRUST_HEIGHT and STATESYNC_TRUST_HASH are empty in network-info.txt)."
    say "      This node therefore REPLAYS THE CHAIN FROM BLOCK 1. That is the whole of the wait below;"
    say "      it is not a fault of your machine or of your network."
    case "$head" in
      ''|*[!0-9]*)
        say "      The public RPC did not answer, so the size of the replay cannot be stated here." ;;
      *)
        est=$(( head * 100 / 2307 / 60 ))
        say "      Network head right now: $head blocks. AT 23.07 BLOCKS APPLIED PER SECOND — a rate"
        say "      measured on 2026-08-07 over 90 s on one host, and the only figure that exists before"
        say "      YOUR node has applied anything — that is roughly ${est} min of replay. Your disk and"
        say "      your peers decide the real number; it is printed below, from your own node." ;;
    esac
    say "      Ask the network operator to publish the three STATESYNC_* fields: with a trust point a"
    say "      new node starts from a snapshot and this wait disappears for everyone after you."
  else
    say "  [i] state-sync trust point present (height ${STATESYNC_TRUST_HEIGHT}): the node starts from a"
    say "      snapshot instead of replaying the chain."
  fi
  say "  [i] EXPECT A FLOOD OF RED LINES IN THE NODE LOGS WHILE IT REPLAYS:"
  say "      'ERR ... SECURITY: decentralized VRF seed UNDER-DECENTRALIZED ... LEGACY fallback'."
  say "      That line is emitted by the chain's jobs keeper (decentralized seed) for a block whose"
  say "      committee draw had too few anchored VRF contributors — including the CURRENT block: the"
  say "      keeper computes that seed at every EndBlock, so the line does NOT stop when your replay"
  say "      catches up. It stops when enough validators have anchored a VRF key, not when you sync."
  say ""
}

sync_progress_line(){
  local h="$1" head="$2" pct="" eta="" left
  local r; r="$(_fmt_rate "$SYNC_RATE_CBS")"
  case "$head" in
    ''|*[!0-9]*) head="?";;
    *) [ "$head" -gt 0 ] && pct=" ($(( h * 100 / head )) %)";;
  esac
  case "$SYNC_RATE_CBS" in
    ''|*[!0-9]*) : ;;
    0) : ;;
    *) if [ "$head" != "?" ] && [ "$head" -gt "$h" ]; then
         left=$(( head - h ))
         eta=" -> ~$(( left * 100 / SYNC_RATE_CBS / 60 )) min left IF this node holds the rate it has held so far"
       fi ;;
  esac
  say "  [..] height $h / network head $head$pct · $r sustained by this node$eta"
}

sync_failure_report(){
  say ""
  say "  ------------------------------------------------------------------"
  if [ "${FORK_STATE:-unknown}" = "fork" ]; then
    say "  STOP -- THIS NODE IS NOT BEHIND. IT DISAGREES WITH THE CHAIN."
    say ""
    say "  After applying height $SYNC_H the two sides do not hold the same application state:"
    say "    this node computed    : $FORK_LOCAL"
    say "    the network published : $FORK_NET"
    [ -n "$FORK_LOG" ] && say "    the node says so itself: $FORK_LOG"
    say ""
    say "  RE-RUNNING THIS COMMAND WILL NOT HELP, AND NEITHER WILL WAITING. A node that computes a"
    say "  different state rejects the block, drops the peer, reconnects, and starts over, forever."
    say "  DO NOT BOND ON THIS NODE: a bond placed here is placed on a fork, not on this chain."
    say ""
    say "  What it means in practice: the binary this node runs was NOT built from the source this chain"
    say "  runs. The two can be out of step in EITHER direction, and the remedy is not the same:"
    say "    · the public mirror is BEHIND the chain -> rebuilding from it changes nothing;"
    say "    · the public mirror is AHEAD of the chain (a consensus change is published but the"
    say "      operator has not restarted the network on it yet) -> rebuilding from it REPRODUCES"
    say "      this fork, every time."
    say "  A blind 'reset --hard origin/main' was prescribed here and is WRONG in both cases: on a"
    say "  fresh clone it is a no-op that changes nothing, and on a modified clone it destroys work"
    say "  without fixing anything. It also invited an endless loop -- rebuild, fork, wipe, repeat."
    say ""
    say "  ASK THE OPERATOR WHICH COMMIT THE CHAIN IS RUNNING before rebuilding anything. Until that"
    say "  answer matches your source, no rebuild can help. This node's data must go regardless: it"
    say "  was written by the wrong state machine, and keeping it keeps the disagreement."
    say ""
    say "  ⚠ BACK THE KEYS UP BEFORE THE NEXT LINE, AND READ WHY."
    say "  \`down -v\` DELETES THE VOLUME, and this volume is not only chain data: it is the node home."
    say "  It holds config/priv_validator_key.json, config/vrf_key, and keyring-test/ -- the OPERATOR key"
    say "  that controls any stake this validator has bonded. Chain data can be downloaded again from"
    say "  any peer; that key cannot. Losing it does NOT merely mean re-registering: an undelegation is"
    say "  a transaction SIGNED BY THAT KEY, so a bonded stake whose key is gone can never be recovered"
    say "  by anyone, including us. Nothing on this network can undo it."
    say "  Copy them out first -- nothing is printed to the screen, the files land beside you:"
    say "    ssh <this host> \"docker run --rm -v \$(docker volume inspect ${DENDRA_PROJECT:-dendra-node}_node-data -f '{{.Name}}'):/d -w /d busybox tar cz config/priv_validator_key.json config/node_key.json config/vrf_key keyring-test\" > dendra-node-keys.tgz"
    say "    tar tzf dendra-node-keys.tgz     # must list priv_validator_key.json before you continue"
    say ""
    say "  Then, and only then:"
    say "    docker compose -f $NODE_KIT/docker-compose.yml down -v"
    say "  DO NOT re-run this command until your SOURCE has changed. Wiping the volume and rejoining"
    say "  with the SAME binary reproduces the same fork and destroys the volume again on every pass."
    say "  Once the source matches what the operator names, rebuild, then re-run and restore the keys."
    say "  ------------------------------------------------------------------"
    say ""
    return 0
  fi
  say "  THIS SCRIPT STOPPED WAITING. THE NODE DID NOT STOP SYNCING."
  say "  The container is still up and still working. The chain data already downloaded lives in the"
  say "  Docker volume, so nothing here is lost and a re-run resumes where this left off."
  say ""
  say "  WHAT WAS OBSERVED — this is a report, not a diagnosis of your network:"
  say "    height reached       : $SYNC_H"
  say "    network head (RPC)   : $SYNC_HEAD   ('?' = the public RPC did not answer, so it is unknown)"
  say "    catching_up field    : $SYNC_CU   ('?' = the status document could not be read at all)"
  say "    rate this node held  : $(_fmt_rate "$SYNC_RATE_CBS")"
  say "    height last moved    : $SYNC_STALL_FOR s ago"
  say "    age of that block    : $SYNC_BLOCK_AGE s   ('?' = the timestamp could not be read; a LARGE"
  say "                           number means the CHAIN itself stopped producing, which is not a"
  say "                           problem with this node and no re-run will fix it)"
  say "    total wait           : $SYNC_ELAPSED s"
  say "    why it stopped       : $SYNC_WHY"
  say "    state confrontation  : $FORK_STATE   ($FORK_WHY)"
  say ""
  say "  Look for yourself, in this order:"
  say "    docker compose -f $NODE_KIT/docker-compose.yml exec -T node dendrad status"
  say "    docker compose -f $NODE_KIT/docker-compose.yml logs --tail 80 node"
  say "  Then simply re-run this command: it picks the database up where it is."
  say "  ------------------------------------------------------------------"
  say ""
}

# wait_for_sync — returns 0 when the node has caught up, 1 when this script stopped waiting.
# _set_block_age DOC -- refreshes SYNC_BLOCK_AGE from the status document of THIS pass.
#
# Recomputed every pass, not only on the happy exit, because the report printed when the script GIVES
# UP needs it just as much: "the height has not moved for 900 s" and "the last block this node holds
# is eight hours old" are the same fact seen twice, and only the second one names a chain that stopped.
#
# ⛔ AND IT IS DELIBERATELY NOT KEPT LIKE THE HEIGHT IS. An unreadable probe leaves the height, the
# stall clock and the rate untouched, because a height is a monotone fact that stays true. An age is
# not: it is a snapshot, and carrying the previous pass's value forward would print, next to a probe
# that answered nothing, an age that came from somewhere else. So an unreadable document yields `?`.
_set_block_age(){
  local stamp
  stamp="$(_status_block_epoch "$1")"
  case "$stamp" in
    ''|*[!0-9]*) SYNC_BLOCK_AGE="?" ;;
    *)           SYNC_BLOCK_AGE=$(( $(_sync_now) - stamp )) ;;
  esac
}

# _caught_up_line HEIGHT HEAD DOC -- the end-of-sync announcement, IN ONE PLACE ONLY.
# It was written twice, at both exits of the `synced` branch. Two copies of a concluding sentence
# is a sentence that gets half-corrected: the second one keeps the older truth.
#
# It carries THE AGE OF THE LAST BLOCK, and that is its whole purpose. A line reading "caught up at
# height 20583" is true of a node up to date with a chain that has produced nothing for hours, and
# the joiner reading it concludes the network works. Three possible announcements, never two:
#   age readable and short -> the age is given, catching up is good news;
#   age readable and long  -> the age is given AND a warning says the CHAIN is not advancing. Nothing
#                             blocks: the node has finished its work, there is nothing more to wait
#                             for, and looping the joiner on a network outage would leave them with
#                             no diagnosis. They are TOLD instead, which is what was missing;
#   age unreadable         -> the age is announced unknown. Never omitted, never shown as fresh.
_caught_up_line(){
  local h="$1" head="$2" doc="$3" age
  # The age was measured by `_set_block_age` on this same pass, from this same document. Measuring it
  # again here would read the clock a second time and print an age that is not the one the loop just
  # judged on -- two numbers for one fact.
  _set_block_age "$doc"
  age="$SYNC_BLOCK_AGE"
  case "$age" in
    '?') say "  [OK] node caught up at height $h (network head ${head}); age of the last block: UNKNOWN on this host — no python3, no jq, and a non-GNU date, so the script cannot tell a live chain from a stopped one." ;;
    *)
      if [ "$age" -ge "$DENDRA_STALE_BLOCK_SECS" ]; then
        say "  [OK] node caught up at height $h (network head ${head})."
        warn "THE NODE IS UP TO DATE BUT THE CHAIN IS NOT PRODUCING: the last block is ${age}s old (over ${DENDRA_STALE_BLOCK_SECS}s). Being caught up says this node applied everything offered to it, never that anything is being offered. Check the validators before concluding the network works — or check this machine's clock, which is the other way this number gets large."
      else
        say "  [OK] node caught up at height $h (network head ${head}); last block ${age}s old."
      fi
      ;;
  esac
}

# It never exits on its own: the caller owns the fatal, and the bench can run the real loop.
wait_for_sync(){
  local t0 tnow line state h cu first_h first_t last_h stall_since head_at last_report gap cu_confrontable doc
  _sync_pick_reader || true
  t0="$(_sync_now)"
  first_h=-1; first_t="$t0"; last_h=-1; stall_since="$t0"; head_at=0; last_report="$t0"
  SYNC_H="?"; SYNC_HEAD="?"; SYNC_RATE_CBS=""; SYNC_STALL_FOR=0; SYNC_ELAPSED=0; SYNC_WHY=""; SYNC_CU="?"
  # SYNC_BLOCK_AGE is NOT reset here, and that is a decision rather than an omission: `_set_block_age`
  # runs on every pass of the loop below, including the very first, so nothing from an earlier call can
  # survive into this one. A reset here would be a line that can never change an outcome — and a guard
  # that cannot fail is a guard that lies about being tested. The protection lives where it bites.
  # Reset with the rest, and for the same reason: a verdict left over from an earlier call would be
  # reported as the outcome of this one. `unknown` is the state to start from, never `agree`.
  FORK_STATE=unknown; FORK_WHY=""; FORK_LOCAL=""; FORK_NET=""; FORK_LOG=""; _FORK_SEEN_H=""
  say "  [..] waiting for the node to catch up. This script waits AS LONG AS THE HEIGHT KEEPS CLIMBING;"
  say "       it gives up only if no new block is applied for $(( DENDRA_SYNC_STALL_SECS / 60 )) min."
  while :; do
    tnow="$(_sync_now)"; SYNC_ELAPSED=$(( tnow - t0 ))
    # The document is kept: the timestamp is read from THE SAME status as the height. Probing the node
    # again would give two snapshots, and an age that does not match the height announced next to it.
    doc="$(_sync_probe)"
    line="$(_sync_state "$doc")"
    state="${line%% *}"; line="${line#* }"; h="${line%% *}"; cu="${line##* }"
    _set_block_age "$doc"
    SYNC_CU="$cu"
    # Re-derived on EVERY pass. A reason left over from an earlier iteration would be reported as the
    # cause of a stop that happened for a different reason two hours later.
    SYNC_WHY=""

    # `unreadable` deliberately does NOT touch the height, the stall clock or the rate: an unreadable
    # probe is an absence of measurement, so nothing here may move on the strength of it. The stall
    # clock keeps running, which is exactly right — a probe that stays unreadable is a stop.
    if [ "$state" != unreadable ]; then
      SYNC_H="$h"
      [ "$first_h" -lt 0 ] && [ "$h" -gt 0 ] && { first_h="$h"; first_t="$tnow"; }
      if [ "$h" -gt "$last_h" ]; then last_h="$h"; stall_since="$tnow"; fi
    fi
    SYNC_STALL_FOR=$(( tnow - stall_since ))

    # The only rate this script is entitled to quote: the one THIS node produced, over THIS run.
    if [ "$first_h" -ge 0 ] && [ "$last_h" -gt "$first_h" ] && [ $(( tnow - first_t )) -ge 30 ]; then
      SYNC_RATE_CBS=$(( ( last_h - first_h ) * 100 / ( tnow - first_t ) ))
    fi

    if [ $(( tnow - head_at )) -ge "$DENDRA_SYNC_REPORT_SECS" ] || [ "$head_at" = 0 ]; then
      SYNC_HEAD="$(_sync_head)"; head_at="$tnow"
    fi

    if [ "$state" = synced ]; then
      # ONE AMBIGUITY IS LEFT AND IT IS NAMED. `catching_up` reported as absent is read as false by the
      # proto3 rule, which is correct for an encoder that omits zeros — but it is an INFERENCE, not an
      # answer. So in that single case the claim is confronted with the head of the network before it is
      # believed. An explicit `false` is the node's own answer and is taken as such.
      # The head is tested for being A NUMBER rather than for differing from the sentinel, because an
      # arithmetic expression on anything else prints a bash error and evaluates to 0 — a gap of zero,
      # i.e. the answer this branch exists to question, arrived at by accident.
      # AND THE CONFRONTATION IS A BONUS, NOT A CONDITION: when the head cannot be read the omission is
      # accepted, because the proto3 reading is correct on its own and because an operator who joins on
      # SEEDS alone has no public RPC to confront anything with. Requiring a head here would hang that
      # operator forever on a check that is only ever a second opinion.
      gap=0
      case "$SYNC_HEAD" in ''|*[!0-9]*) cu_confrontable=0;; *) cu_confrontable=1; gap=$(( SYNC_HEAD - h ));; esac
      if [ "$cu" = absent ] && [ "$cu_confrontable" = 1 ]; then
        if [ "$gap" -gt 100 ]; then
          SYNC_WHY="the node omits catching_up (read as 'not catching up') but sits $gap blocks under the network head — the claim is not believed on its own"
          warn "status says caught up by OMISSION at height $h while the public RPC is at $SYNC_HEAD -> still waiting."
        else
          _caught_up_line "$h" "$SYNC_HEAD" "$doc"
          return 0
        fi
      else
        _caught_up_line "$h" "$SYNC_HEAD" "$doc"
        return 0
      fi
    fi

    # A FROZEN HEIGHT IS NOT A DIAGNOSIS, AND 900 s IS A LONG TIME TO BE TOLD NOTHING.
    # The confrontation runs as soon as the height has been still for one report interval, ONCE per
    # distinct height: a node that is merely behind pays one status read and one block read per height
    # it rests on, which is nothing, while a node that disagrees is told so in two minutes instead of
    # fifteen. And once the two states differ there is nothing left to wait for, so this stops waiting.
    if [ "$SYNC_STALL_FOR" -ge "$DENDRA_SYNC_REPORT_SECS" ] && [ "$_FORK_SEEN_H" != "$h" ]; then
      _FORK_SEEN_H="$h"
      fork_verdict "$h"
      if [ "$FORK_STATE" = fork ]; then
        SYNC_WHY="CONSENSUS FORK -- $FORK_WHY"
        return 1
      fi
    fi

    if [ "$SYNC_STALL_FOR" -ge "$DENDRA_SYNC_STALL_SECS" ]; then
      [ -n "$SYNC_WHY" ] || SYNC_WHY="no new block applied for ${SYNC_STALL_FOR} s (limit ${DENDRA_SYNC_STALL_SECS} s, DENDRA_SYNC_STALL_SECS)"
      [ "$state" = unreadable ] && SYNC_WHY="the status document could not be read for ${SYNC_STALL_FOR} s (limit ${DENDRA_SYNC_STALL_SECS} s) — this is 'no answer', not 'not caught up'"
      return 1
    fi
    if [ "$SYNC_ELAPSED" -ge "$DENDRA_SYNC_MAX_SECS" ]; then
      SYNC_WHY="absolute ceiling reached (${DENDRA_SYNC_MAX_SECS} s, DENDRA_SYNC_MAX_SECS) while the height was still climbing — raise it and re-run, nothing is wrong"
      return 1
    fi

    if [ $(( tnow - last_report )) -ge "$DENDRA_SYNC_REPORT_SECS" ]; then
      last_report="$tnow"
      if [ "$state" = unreadable ]; then
        warn "status unreadable (${SYNC_STALL_FOR} s without a readable answer) — still trying, the container is up."
      else
        sync_progress_line "$h" "$SYNC_HEAD"
      fi
    fi
    _sync_nap "$DENDRA_SYNC_POLL_SECS"
  done
}

# start_local_node — writes the node kit's .env, brings the node up, and waits until it has caught up.
#
# Shared by the VALIDATOR role and by the MINER role: a miner that runs its own node uses exactly the
# same node, configured exactly the same way. Duplicating the sequence would let the two copies drift,
# and the miner's copy — the one almost every operator runs — would be the one nobody re-reads.
start_local_node(){
  # THE NODE KIT'S .env IS AUTHORITATIVE, SO IT IS READ BEFORE ANYTHING IS REQUIRED.
  # `check_prereqs` accepts the mere EXISTENCE of `$NODE_KIT/.env` as valid configuration, and
  # `verify_genesis_info` states that "the node kit enforces its own". Both hold only if the values in
  # that file actually reach this function. They do not arrive on their own: the invocation form this
  # script prints in its own help (`DENDRA_NODE=... DENDRA_RELAY=... FAUCET=... bash deploy/join.sh`),
  # like any plain re-run without CONFIG_URL, carries no GENESIS_URL in the shell. Such a run clears
  # the pre-flight, the hardware probe and the relay probe, and reaches this point with the setting
  # sitting in the neighbouring file rather than in the environment. Reading the kit here is what makes
  # the pre-flight's verdict true where it is used.
  # ORDER MATTERS: the rewrite further down overwrites this file with the shell's values, so the
  # carry-over has to happen BEFORE it. Otherwise a re-run erases not only GENESIS_URL but
  # GENESIS_SHA256 and SEEDS — the digest that protects against a substituted genesis, and the peers
  # without which nothing ever syncs.
  if [ -f "$NODE_KIT/.env" ]; then
    for _k in CHAIN_ID GENESIS_URL GENESIS_SHA256 SEEDS PERSISTENT_PEERS; do
      if [ -z "$(eval printf '%s' "\${$_k:-}")" ]; then
        _v="$(sed -n "s/^$_k=//p" "$NODE_KIT/.env" 2>/dev/null | tail -1 | tr -d '\r')"
        [ -n "$_v" ] && { export "$_k=$_v"; say "  [i] $_k CARRIED OVER from $NODE_KIT/.env"; }
      fi
    done
  fi
  [ -n "${GENESIS_URL:-}" ] || die "GENESIS_URL required to run a node. Either pass CONFIG_URL=<network-info.txt>, or run with --remote-rpc to read the chain from the operator's public RPC instead of starting one."
  # This .env is REWRITTEN from scratch on every run. Anything a later step added to it would be lost —
  # in particular DENDRA_VRF_KEY_FILE, which bond_validator.sh writes so the node SIGNS its vote
  # extensions. Losing it does not break anything visibly: the node still runs, still syncs, still looks
  # healthy — it just silently stops contributing to the committee seed. So we carry it over.
  # ⛔ AND DENDRA_VRF_KEY_FILE IS NOT THE ONLY ONE. Everything the operator or another script of this
  # kit puts here is a DECISION, and rewriting the file from scratch silently revokes all of them:
  # DENDRA_ADOPT_EXISTING_HOME (the only way past the preflight guard — erase it and the very next
  # `compose up` exits 78 and this script dies on "docker compose up failed", the empty message its
  # own header condemns), DENDRA_NODE_IMAGE (pinning the image a running node is on), DENDRA_PROJECT
  # and the two port overrides (what keeps a second node off the first one's containers). So the rule
  # is inverted: this template owns the keys it writes, and EVERY OTHER DENDRA_* line already present
  # is carried over verbatim. A key this script does not know about is a decision it must not undo.
  local _keep=""
  if [ -f "$NODE_KIT/.env" ]; then
    _keep="$(grep -E '^DENDRA_[A-Z0-9_]+=' "$NODE_KIT/.env" 2>/dev/null \
              | grep -vE '^DENDRA_(NODE|RELAY|API|FAUCET)=' || true)"
  fi
  cat > "$NODE_KIT/.env" <<EOF
# Generated by deploy/join.sh $(date -u +%FT%TZ)
CHAIN_ID=${CHAIN_ID:-dendra}
GENESIS_URL=$GENESIS_URL
GENESIS_SHA256=${GENESIS_SHA256:-}
SEEDS=${SEEDS:-}
PERSISTENT_PEERS=${PERSISTENT_PEERS:-${SEEDS:-}}
# The moniker is NOT a display field: it travels inside the CometBFT NodeInfo, is exchanged with every
# peer, and is returned by /net_info for anyone who asks. A moniker built from the hostname therefore
# broadcast the operator's machine name to the whole peer-to-peer network. Same pseudonym as the miner
# identity, and for the same reason. It is frozen when the node is initialised: an existing node keeps
# the old one.
# NO BACKTICKS AND NO COMMAND SUBSTITUTION IN THESE COMMENTS. This heredoc is UNQUOTED (<<EOF) because
# the values below must expand — so bash also expands, inside the comment lines, both a backtick pair
# and a dollar followed by a parenthesis, and runs whatever they contain. Marking an identifier as code
# that way here therefore EXECUTES it: the operator gets "command not found" on a join that is
# otherwise fine, and the quoted text is blanked out of the generated file. The delimiter cannot be
# quoted (<<'EOF') without freezing the values, so the rule falls on the TEXT: name the two dangerous
# forms in words, and never write either of them here.
# ⚠️ THIS PARAGRAPH WAS ITSELF THE BUG. It spelled the dollar-parenthesis form out literally as an
# example, so bash ran it: every joiner saw "join.sh: line <this heredoc>: ...: command not found"
# right after the genesis check passed, and the warning reached the generated file with the example
# blanked out. A rule written in the syntax it forbids is executed, not read.
MONIKER=${MONIKER:-dendra-$( { cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo val; } | tr -d '\n' | head -c 64 | sha256sum | cut -c1-8)}
STATESYNC_RPC=${STATESYNC_RPC:-}
STATESYNC_TRUST_HEIGHT=${STATESYNC_TRUST_HEIGHT:-}
STATESYNC_TRUST_HASH=${STATESYNC_TRUST_HASH:-}
EOF
  if [ -n "$_keep" ]; then
    printf '%s\n' "$_keep" >> "$NODE_KIT/.env"
    # Named one by one: a carried-over decision the operator has forgotten about is as surprising as
    # one that was silently dropped. The count is printed so a rewrite that carries NOTHING is visible.
    say "  [i] $(printf '%s\n' "$_keep" | grep -c .) operator setting(s) carried across the rewrite:"
    printf '%s\n' "$_keep" | sed 's/^/        /'
  fi
  # This file describes the validator identity and designates its VRF key (DENDRA_VRF_KEY_FILE): it is
  # closed AT CREATION, like the miner kit's. Default permissions leave it readable by every account on
  # the machine — and a sensitive configuration file is never hardened after the fact.
  chmod 600 "$NODE_KIT/.env" 2>/dev/null || true
  say "  [OK] $NODE_KIT/.env written (SHA256 checked by node-join.sh at boot, FATAL on mismatch)"
  # ⛔ `--build` REBUILDS AND RE-TAGS the image this compose names, and the node service carries both
  # `build:` and `image:`. On a machine that already runs a Dendra node, that moves the tag its
  # container will restart onto — and `up -d` then recreates it on a binary nobody chose. A different
  # binary computes state differently, so a node producing blocks on a live chain forks or halts.
  # The running container is pinned to an image ID, so it is untouched until something RECREATES it;
  # this is that something. Refuse rather than warn: at this point the operator is joining, not
  # upgrading, and the two are not the same gesture.
  local _run_img="" _tag_img="" _img_ref
  _img_ref="$(( cd "$NODE_KIT" && grep -oE '^\s*image:\s*\S+' docker-compose.yml | head -1 | awk '{print $2}' ) 2>/dev/null || true)"
  _img_ref="${DENDRA_NODE_IMAGE:-dendra/node:latest}"
  _run_img="$(docker inspect "$( ( cd "$NODE_KIT" && docker compose ps -q node ) 2>/dev/null | head -1)" --format '{{.Image}}' 2>/dev/null || true)"
  _tag_img="$(docker image inspect "$_img_ref" --format '{{.Id}}' 2>/dev/null || true)"
  # ⛔ THREE ANSWERS, NOT TWO. An EMPTY `_tag_img` does not mean "nothing to refuse", it means
  # "nobody can say what compose would resolve this image to": the image is not present locally, so an
  # `up`/`recreate` will PULL it, and that is precisely the case where the consensus binary can change
  # under a running node. Treating that emptiness as a pass reads an unknown as reassurance, on the
  # criterion that decides whether a recreate can fork the node. On a security criterion an unknown is
  # a refusal. (The sibling script takes the same line: `bond_validator.sh` refuses an image it cannot
  # resolve.)
  if [ -n "$_run_img" ] && [ -z "$_tag_img" ]; then
    die "a node is already running here, and the image $_img_ref is NOT present locally, so nothing can
   say what a recreate would resolve it to. Pull or build it first (docker pull $_img_ref), or pin the
   running image with DENDRA_NODE_IMAGE=<its id> in $NODE_KIT/.env — joining must never recreate a
   running node on an image nobody has checked."
  fi
  if [ -n "$_run_img" ] && [ -n "$_tag_img" ] && [ "$_run_img" != "$_tag_img" ]; then
    die "a node is already running here on image ...${_run_img: -12}, while $_img_ref now points at ...${_tag_img: -12}.
   Rebuilding would recreate that node on a DIFFERENT consensus binary and can fork it off the chain.
   Either pin the running image (DENDRA_NODE_IMAGE=<its id> in $NODE_KIT/.env) and re-run, or start a
   SEPARATE node: set DENDRA_PROJECT, DENDRA_P2P_PORT, DENDRA_RPC_PORT and DENDRA_NODE_IMAGE to values
   of your own — see deploy/testnet-node/README.md. Joining must never recreate a node you did not
   mean to touch."
  fi
  ( cd "$NODE_KIT" && docker compose up -d --build ) || die "docker compose up failed"
  announce_sync_plan
  if ! wait_for_sync; then
    sync_failure_report
    # THE FATAL SENTENCE CARRIES THE MEASUREMENT, NOT A HYPOTHESIS. Whatever a reader keeps from this
    # run, it is this line — so it names the height, the head, the rate and the fact that the node
    # itself is unaffected, and it points at nothing it has not observed.
    if [ "${FORK_STATE:-unknown}" = "fork" ]; then
      die "CONSENSUS FORK at height $SYNC_H: this node computes $FORK_LOCAL where the network published $FORK_NET. This is not a slow sync and re-running will not fix it -- the binary is not built from the source this chain runs. Do NOT bond on this node."
    fi
    die "stopped waiting for the sync at height $SYNC_H of a network head at $SYNC_HEAD ($(_fmt_rate "$SYNC_RATE_CBS") sustained, height last moved ${SYNC_STALL_FOR} s ago, waited ${SYNC_ELAPSED} s): $SYNC_WHY. The node container is STILL RUNNING and still syncing — see the report above; re-run this command to pick it up."
  fi
}

run_validator(){
  say "== [join] role: VALIDATOR (sync -> CONFIRMED bond -> VRF anchoring) =="
  # THE ANTI-MITM GATE BELONGS ON THIS ROLE ABOVE ALL OTHERS, hence the call before any node starts.
  # `verify_genesis_info` confronts the genesis actually served with the digest announced out of band.
  # This role is the one that locks up capital and signs blocks, so it has the most to lose from a
  # substituted genesis: a deceived miner serves work against the wrong chain and wastes its own
  # compute, while a deceived validator signs that chain with its stake bonded ON it — and the bond
  # lives on the fork, not on the chain it meant to join.
  verify_genesis_info
  verify_consensus_epoch
  verify_kit_version
  # WHAT THIS RUN COSTS, SAID BEFORE IT COSTS IT.
  # The bond instructions and the jail figures both live at the END of this function, i.e. behind the
  # sync wait — which, with no state-sync trust point published, is tens of minutes. An operator who
  # walks away during that wait has been told nothing about the risk they are about to take on. This
  # block is a forward reference, not a second copy: it names the exposure and hands over the chain
  # reads, and the figures themselves are still printed once, at the end, by validator_health_notice.
  say ""
  say "  WHAT THIS COMMAND IS ABOUT TO DO, IN ORDER:"
  say "    1. start a full node and WAIT until it has caught up (the long step — see the note below);"
  say "    2. print the bond instructions. join.sh BONDS NOTHING on its own, ever;"
  say "    3. print your jail exposure read from the chain, and schedule an hourly watch."
  say ""
  say "  READ THIS BEFORE STEP 2 RATHER THAN AFTER IT:"
  say "  ONCE BONDED, UNPLANNED DOWNTIME JAILS YOUR VALIDATOR AND TAKES A PERCENTAGE OF YOUR"
  say "  STAKE. Any outage that outlasts the window is enough, and nothing warns you — your node keeps"
  say "  running, it simply stops being in the consensus set. How many blocks you may miss, for how"
  say "  long you stay jailed and what fraction is taken are GOVERNED PARAMETERS of this chain: no"
  say "  figure for them is written into this script, and none is repeated here. Read them yourself,"
  say "  now or at the end of this run:"
  _jail_params_cmds
  say ""
  start_local_node
  cat <<'EOF'
  ------------------------------------------------------------------
  BECOMING A VALIDATOR = LOCKING UP STAKE (deliberate action).
  join.sh does NOT bond silently.

  ONE COMMAND (recommended) — still deliberate: it prints the plan and bonds NOTHING until you
  confirm, computes an amount that keeps every validator under 2/3, then anchors the VRF key and
  restarts the node with it (the step everyone skips, without which you do not feed the seed):

      bash deploy/bond_validator.sh              # shows the plan, changes nothing
      bash deploy/bond_validator.sh --yes-bond   # does it, end to end, then verifies

  Or the same thing by hand:
    1. Create your operator key (keyring 'test' = UNENCRYPTED, fine for devnet, not for real value):
         docker compose -f deploy/testnet-node/docker-compose.yml exec node \
           dendrad keys add validator --keyring-backend test
    2. Fund the address: external joiner -> faucet/channel; OPERATOR bootstrap -> send from the genesis
       node's 'validator' key (keep stake distribution <2/3: the faucet is not enough).
    3. Bond (create-validator) — SDK 0.50+ = validator.json file, see deploy/testnet-node/README.md.
    4. Anchor your VRF key. WITHOUT IT your node produces blocks and contributes NOTHING to the seed,
       and the seed gates every audit draw — so nothing gets verified and no held payment is released —
       DOCKER node: pipe the script INTO the container (it has dendrad + dendra-vrf + the keyring):
         tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | docker compose -f deploy/testnet-node/docker-compose.yml \
           exec -T -e HOME_DIR=/root/.dendra -e CHAIN_ID=dendra -e NODE=tcp://localhost:26657 -e VAL_KEY=validator node bash -s
    5. Tell the node where the key lives, then restart it. The variable is read from the node kit's
       env file, so the line goes THERE — exporting it in your shell does not reach the container.
       And the restart must FORCE a recreate: docker compose up -d recreates a container only when the
       configuration CHANGED, while join.sh preserves this very line across its rewrites, so an
       identical line reads as "no change", the node keeps running, and it never loads the key. The
       result is the failure step 4 warns about — anchored on-chain, zero signed vote extensions —
       while every command on screen prints success:
         echo 'DENDRA_VRF_KEY_FILE=/root/.dendra/config/vrf_key' >> deploy/testnet-node/.env
         docker compose -f deploy/testnet-node/docker-compose.yml up -d --force-recreate
    6. Verify (contributors should rise). dendrad lives INSIDE the container, like in steps 1 and 4:
         docker compose -f deploy/testnet-node/docker-compose.yml exec -T node \
           dendrad query jobs committee-seed-health
       That command answers ONE question — is the seed being fed — and it cannot tell a jailed
       validator from a broken vote extension. For that, see the jail watch printed below.
    7. Check whether anyone can REACH you. This is not a formality and it is not about your node's
       health: a node nobody can dial has ONE path for every consensus message, so a single missed
       block proposal is not recoverable and the round is played without it. Measured on this network,
       five nodes out of five were in that state, each dialling out to the same single host, and no
       indicator anywhere said so — because from the inside such a node looks perfectly healthy.
         bash deploy/node_reachability.sh
       It tells you which of the two fixes you need, in which order, and it refuses to guess: it
       cannot dial you from outside, so it reports what did arrive and says as much.
  ------------------------------------------------------------------
EOF
  validator_health_notice
}

# LIBRARY MODE — the ONLY reason it exists: a bench that copies the sync loop into itself proves things
# about the copy. `DENDRA_JOIN_LIB=1 . deploy/join.sh` loads the functions and stops before any role
# runs, so the bench drives the code that actually ships. Nothing else in this file reads this variable,
# and it is not in the CONFIG_URL allow-list.
case "${DENDRA_JOIN_LIB:-0}" in 1) return 0 2>/dev/null || exit 0;; esac

check_prereqs
case "$ROLE" in
  miner) run_miner;;
  validator) run_validator;;
esac
