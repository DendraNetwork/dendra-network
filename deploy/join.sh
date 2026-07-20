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
while [ $# -gt 0 ]; do case "$1" in
  --validator) ROLE=validator; shift;;
  --miner) ROLE=miner; shift;;
  --judge) JUDGE=1; shift;;
  --id) MINER_ID="${2:-}"; shift 2;;
  -h|--help)
    sed -n '2,16p' "$0" 2>/dev/null || true
    echo "Modes: (default) miner | --judge miner+audit committee | --validator node+guided bond+VRF | --id NAME"
    exit 0;;
  *) echo "[join] unknown arg: $1 (see --help)"; exit 2;;
esac; done

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
REPO="$(cd "$SELF/.." 2>/dev/null && pwd)"
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
      # STATESYNC_* ajoutées ici DÉLIBÉRÉMENT : cette allow-list jette en silence toute clé inconnue
      # (c'est sa raison d'être, anti-injection). Sans cette ligne, l'opérateur publierait le point de
      # confiance, le joiner le téléchargerait, et il serait ignoré sans un mot — la chaîne de valeur
      # aurait 4 maillons corrects et 1 muet. Les valeurs restent soumises au filtre de caractères.
      CHAIN_ID|GENESIS_URL|GENESIS_SHA256|SEEDS|PERSISTENT_PEERS|DENDRA_NODE|DENDRA_RELAY|FAUCET|EXPLORER_URL|STATESYNC_RPC|STATESYNC_TRUST_HEIGHT|STATESYNC_TRUST_HASH)
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
export DENDRA_MODEL_ID="${DENDRA_MODEL_ID:-llama3.1:8b-instruct-q4_K_M}"
export DENDRA_EMBED_MODE=backend
export DENDRA_EMBED_API_MODEL="${DENDRA_EMBED_API_MODEL:-nomic-embed-text}"
[ -n "$MINER_ID" ] || MINER_ID="m-$(hostname 2>/dev/null || echo node)-$RANDOM"

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
  [ -n "$HW_MODEL" ] && export DENDRA_MODEL_ID="${DENDRA_MODEL_ID_EXPLICIT:-$HW_MODEL}"
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
    say "       (no-Docker fallback: deploy/rented-devnet/rented_miner.sh, see README)"
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
  # NVIDIA GPU (WARN, not FATAL — CPU works but is slow)
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    GPU_OK=1
    say "  [OK] GPU: $(nvidia-smi -L | head -1)"
    local vram
    vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
    if [ -n "$vram" ] && [ "$vram" -lt 6000 ] 2>/dev/null; then
      warn "VRAM ~${vram} MB: the pinned model (~5-6 GB) will spill -> floor throughput. >=8 GB recommended."
    fi
    if docker run --rm --gpus all ollama/ollama:latest nvidia-smi >/dev/null 2>&1; then
      TOOLKIT_OK=1; say "  [OK] nvidia-container-toolkit (GPU visible to Docker)"
    else
      warn "GPU seen by the host but NOT by Docker -> install nvidia-container-toolkit; otherwise CPU (slow)."
    fi
  else
    warn "no NVIDIA GPU detected -> CPU inference (slow, floor throughput). Ctrl-C to cancel (10 s)..."
    sleep 10
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

# ---------------------------------------------------------------- (b) GENESIS SHA (miner = informational)
verify_genesis_info(){
  [ -n "${GENESIS_URL:-}" ] && [ -n "${GENESIS_SHA256:-}" ] || return 0
  local tmp sha; tmp="$(mktemp)"
  if curl -fsSL "$GENESIS_URL" -o "$tmp" 2>/dev/null; then
    sha=$(sha256sum "$tmp" | cut -d' ' -f1)
    if [ "$sha" = "$GENESIS_SHA256" ]; then
      say "  [OK] genesis SHA256 VERIFIED (you are talking to the right network)"
    else
      die "published genesis SHA256 != expected ($GENESIS_SHA256) -> altered network / possible MITM. Copy the sha from the official channel."
    fi
  fi
  rm -f "$tmp"
}

# ---------------------------------------------------------------- override GPU without editing YAML
write_gpu_override(){
  [ "$GPU_OK" = 1 ] && [ "$TOOLKIT_OK" = 1 ] || return 0
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

# ---------------------------------------------------------------- model-registry guard (best-effort)
warn_model_registry(){
  command -v dendrad >/dev/null 2>&1 || return 0
  local out enforce
  out=$(dendrad query modelregistry params -o json --node "${DENDRA_NODE:-tcp://127.0.0.1:26657}" 2>/dev/null) || return 0
  enforce=$(printf '%s' "$out" | grep -c '"enforce_model_registry": *true' || true)
  [ "$enforce" = "1" ] && say "  [i] model registry ENFORCED on-chain: keep DENDRA_MODEL_ID=$DENDRA_MODEL_ID (a different model = commits refused)."
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
  local i reg=0
  for i in $(seq 1 12); do
    if docker compose -f "$kit/docker-compose.yml" logs "$svc" 2>/dev/null | grep -qE "register|create-miner|commit"; then reg=1; break; fi
    sleep 10
  done
  [ "$reg" = 1 ] && say "  [OK] daemon activity detected (register/commit in the logs)" \
                 || warn "no visible activity in ~2 min (see logs below; zero traffic = normal: 'waiting for jobs')."
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
  If 0 jobs     : normal on a quiet network. If commits REFUSED: your model != on-chain registry.
============================================================
EOF
}

# ---------------------------------------------------------------- roles
run_miner(){
  say "== [join] role: MINER${JUDGE:+ (+judge)} =="
  verify_genesis_info
  write_gpu_override
  # The kit .env is written from the loaded config (an existing, already-filled .env is kept when no
  # explicit config is provided).
  # If this is a miner-JUDGE, set the judge model (RAM-based heterogeneity) and carry it into the verdict.
  JUDGE_MODEL=""
  if [ "$JUDGE" = 1 ]; then
    # HARDWARE GATE before anything else: an under-powered judge penalises honest miners, so the seat is
    # refused rather than degraded. The node still joins and MINES normally.
    if ! _assert_can_judge; then
      JUDGE=0
      warn "--judge ignored: joining as a MINER only. Re-run on eligible hardware to join the committee."
    else
      JUDGE_MODEL="$(_pick_judge_model)"
      case "$JUDGE_MODEL" in
        *qwen3:4b*) die "qwen3:4b is FORBIDDEN as a judge (unfair verdicts in a distributed setting). Set DENDRA_JUDGE_MODEL=mistral-nemo.";;
      esac
      say "  [i] JUDGE model = $JUDGE_MODEL (from deploy/hw_probe.sh; override DENDRA_JUDGE_MODEL)."
      say "  [i] served model = $DENDRA_MODEL_ID (sized to the measured VRAM). Committee diversity is an announcement gate."
    fi
  fi
  if [ -n "${DENDRA_NODE:-}" ] && [ -n "${DENDRA_RELAY:-}" ] && [ -n "${FAUCET:-}" ]; then
    cat > "$MINER_KIT/.env" <<EOF
# Generated by deploy/join.sh $(date -u +%FT%TZ)
DENDRA_NODE=$DENDRA_NODE
DENDRA_RELAY=$DENDRA_RELAY
FAUCET=$FAUCET
DENDRA_MODEL_ID=$DENDRA_MODEL_ID
DENDRA_EMBED_MODE=$DENDRA_EMBED_MODE
DENDRA_EMBED_API_MODEL=$DENDRA_EMBED_API_MODEL
MINER_ID=$MINER_ID
DENDRA_MINER_JUDGE=$JUDGE
DENDRA_JUDGE_MODEL=$JUDGE_MODEL
DENDRA_JUDGE_MODEL_ID=$JUDGE_MODEL
EOF
    say "  [OK] $MINER_KIT/.env written"
  fi
  warn_model_registry
  ( cd "$MINER_KIT" && docker compose up -d --build ) || die "docker compose up failed (see output above)"
  wait_healthy "$MINER_KIT" miner
  print_status_miner
  [ "$JUDGE" = 1 ] && say "  [i] JUDGE mode: reveal_worker+judge_worker start once the miner key is ready (logs: grep judge)."
}

run_validator(){
  say "== [join] role: VALIDATOR (sync -> CONFIRMED bond -> VRF anchoring) =="
  [ -n "${GENESIS_URL:-}" ] || die "GENESIS_URL required (network-info.txt via CONFIG_URL)."
  # This .env is REWRITTEN from scratch on every run. Anything a later step added to it would be lost —
  # in particular DENDRA_VRF_KEY_FILE, which bond_validator.sh writes so the node SIGNS its vote
  # extensions. Losing it does not break anything visibly: the node still runs, still syncs, still looks
  # healthy — it just silently stops contributing to the committee seed. So we carry it over.
  local _keep_vrf=""
  [ -f "$NODE_KIT/.env" ] && _keep_vrf="$(grep -E '^DENDRA_VRF_KEY_FILE=' "$NODE_KIT/.env" 2>/dev/null | tail -1)"
  cat > "$NODE_KIT/.env" <<EOF
# Generated by deploy/join.sh $(date -u +%FT%TZ)
CHAIN_ID=${CHAIN_ID:-dendra}
GENESIS_URL=$GENESIS_URL
GENESIS_SHA256=${GENESIS_SHA256:-}
SEEDS=${SEEDS:-}
PERSISTENT_PEERS=${PERSISTENT_PEERS:-${SEEDS:-}}
MONIKER=${MONIKER:-dendra-$(hostname 2>/dev/null || echo val)}
STATESYNC_RPC=${STATESYNC_RPC:-}
STATESYNC_TRUST_HEIGHT=${STATESYNC_TRUST_HEIGHT:-}
STATESYNC_TRUST_HASH=${STATESYNC_TRUST_HASH:-}
EOF
  [ -n "$_keep_vrf" ] && { printf '%s\n' "$_keep_vrf" >> "$NODE_KIT/.env"; say "  [i] VRF key setting preserved across the rewrite ($_keep_vrf)"; }
  say "  [OK] $NODE_KIT/.env written (SHA256 checked by node-join.sh at boot, FATAL on mismatch)"
  ( cd "$NODE_KIT" && docker compose up -d --build ) || die "docker compose up failed"
  say "  [..] syncing (catching_up) — polling every 15 s (bounded 30 min)"
  local i cu
  for i in $(seq 1 120); do
    cu=$(docker compose -f "$NODE_KIT/docker-compose.yml" exec -T node dendrad status 2>/dev/null | grep -oE '"catching_up": *(true|false)' | grep -oE 'true|false')
    [ "$cu" = "false" ] && break
    sleep 15
  done
  [ "$cu" = "false" ] || die "still catching_up after 30 min — check SEEDS/network then retry."
  say "  [OK] node synced."
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
    4. Anchor your VRF key (REQUIRED on a rewarded testnet, otherwise you do not contribute to the seed) —
       DOCKER node: pipe the script INTO the container (it has dendrad + dendra-vrf + the keyring):
         tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | docker compose -f deploy/testnet-node/docker-compose.yml \
           exec -T -e HOME_DIR=/root/.dendra -e CHAIN_ID=dendra -e NODE=tcp://localhost:26657 -e VAL_KEY=validator node bash -s
    5. Restart the node with DENDRA_VRF_KEY_FILE=<home>/config/vrf_key
    6. Verify: dendrad query jobs committee-seed-health   (contributors should rise)
  ------------------------------------------------------------------
EOF
}

check_prereqs
case "$ROLE" in
  miner) run_miner;;
  validator) run_validator;;
esac
