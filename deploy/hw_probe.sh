#!/usr/bin/env bash
# hw_probe.sh — detect a node's hardware, pick the model tier it can actually run, and emit a JSON
# capability report. Used by the join/miner kits to AUTO-SELECT and AUTO-INSTALL the right model, and
# by the network capacity aggregator to know what compute the network really has.
#
# Why a tier ladder: a model that does not fit in VRAM is silently offloaded to CPU by Ollama, which
# turns a 3-second inference into a multi-minute one and makes the miner miss its deadline
# (observed in production: mistral-nemo 8.7 GB on an 8 GB card -> 30%/70% GPU/CPU -> ReadTimeout).
# So we size the model to the MEASURED VRAM, with headroom for the KV cache / context window.
#
# Network model DIVERSITY (required by the audit committee: >=2 distinct judge models) is obtained
# WITHOUT forcing two models per node: each node deterministically picks one family among those that
# fit, hashed on its node id. Different nodes -> different families -> diversity emerges by itself.
#
# Usage:
#   bash deploy/hw_probe.sh                 # human report + JSON
#   bash deploy/hw_probe.sh --json <id>     # JSON only (machine-readable), id seeds the family pick
#   MODEL=$(bash deploy/hw_probe.sh --model juge1)   # just the chosen model tag
set -u

MODE="report"; NODE_ID=""
case "${1:-}" in
  --json)  MODE="json";  NODE_ID="${2:-}" ;;
  --model) MODE="model"; NODE_ID="${2:-}" ;;
  "") : ;;
  *) NODE_ID="$1" ;;
esac
# PRIVACY — these two values are PUBLISHED to the public capacity registry and rendered on the public
# explorer. A raw hostname ("DESKTOP-AB12CD") or a raw /etc/machine-id identifies the operator's
# personal machine to anyone loading the page. So we publish a STABLE PSEUDONYM derived by hash: it
# keeps every property we need (same box -> same id across restarts, one model per machine, dedup in
# the registry) while revealing nothing. An operator who WANTS to be identified sets DENDRA_NODE_ID.
_RAW_MACHINE="$(cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo machine)"
MACHINE_KEY="$(printf '%s' "$_RAW_MACHINE" | sha256sum | cut -c1-12)"   # pseudonym, never the raw id
_PSEUDO="dendra-$(printf '%s' "$_RAW_MACHINE" | sha256sum | cut -c1-8)"
[ -n "$NODE_ID" ] || NODE_ID="${DENDRA_NODE_ID:-$_PSEUDO}"

# GARDE ANTI-DÉ-ANONYMISATION. Le pseudonyme ci-dessus suffit à anonymiser, mais il ne protège que
# s'il n'est pas écrasé : un appelant qui passe `$(hostname)` en argument défait l'anonymisation par
# le haut, sans que la sonde puisse le savoir. On refuse donc
# désormais tout identifiant qui EST l'identité brute de la machine, quelle que soit sa provenance.
# Se nommer reste possible, mais il faut le vouloir explicitement (DENDRA_NODE_ID), pas l'hériter d'un
# `$(hostname)` oublié dans un script. Une garde de vie privée doit tenir même contre son propre code.
_HOSTNAME="$(hostname 2>/dev/null || echo '')"
if [ -z "${DENDRA_NODE_ID:-}" ] && [ -n "$NODE_ID" ]; then
  case "$NODE_ID" in
    "$_HOSTNAME"|"$_RAW_MACHINE")
      echo "[hw] identifiant '$NODE_ID' = identite BRUTE de la machine -> remplace par le pseudonyme '$_PSEUDO'." >&2
      echo "     (pour publier un nom choisi : DENDRA_NODE_ID=<nom> ; c'est un choix, jamais un defaut)" >&2
      NODE_ID="$_PSEUDO" ;;
  esac
fi

# ---------------------------------------------------------------- hardware detection
GPU_NAME=""; GPU_COUNT=0; VRAM_MB=0
if command -v nvidia-smi >/dev/null 2>&1; then
  # Sum VRAM across GPUs but keep the LARGEST single card as the usable budget: Ollama loads one model
  # on one device by default, so two 8 GB cards do NOT let you run a 12 GB model.
  while IFS=, read -r _name _mem; do
    _name="$(echo "$_name" | sed 's/^ *//;s/ *$//')"; _mem="$(echo "$_mem" | tr -dc '0-9')"
    [ -n "${_mem:-}" ] || continue
    GPU_COUNT=$((GPU_COUNT+1))
    [ -z "$GPU_NAME" ] && GPU_NAME="$_name"
    [ "$_mem" -gt "$VRAM_MB" ] && VRAM_MB="$_mem"
  done < <(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null)
fi
RAM_MB="$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')"; RAM_MB="${RAM_MB:-0}"
CPU_CORES="$(nproc 2>/dev/null || echo 1)"
CPU_MODEL="$(awk -F: '/model name/{gsub(/^ +/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null || echo unknown)"

# Usable budget in MB. GPU: keep 15% headroom for the KV cache/context (a model at 100% of VRAM
# thrashes). No GPU: fall back to RAM with a much harsher cap — CPU inference is slow, stay small.
if [ "$VRAM_MB" -gt 0 ]; then
  BUDGET_MB=$(( VRAM_MB * 85 / 100 )); BACKEND="gpu"
else
  BUDGET_MB=$(( RAM_MB * 40 / 100 )); BACKEND="cpu"
  [ "$BUDGET_MB" -gt 4096 ] && BUDGET_MB=4096      # never pick a big model for CPU-only inference
fi

# ---------------------------------------------------------------- model ladder
# tier|min_budget_MB|comma-separated candidates (DISTINCT families -> network diversity)
# Sizes are the q4_K_M on-disk/VRAM footprint; keep this table in sync with what the miners pull.
LADDER="
5|19500|qwen3:30b-a3b-instruct-2507-q4_K_M
4|11000|qwen3:14b,mistral-nemo
3|9200|mistral-nemo,qwen2.5:7b,gemma2:9b
2|6000|llama3.1:8b-instruct-q4_K_M,qwen2.5:7b
1|2500|llama3.2:3b,qwen2.5:3b
0|0|llama3.2:1b
"
TIER=0; CANDIDATES="llama3.2:1b"
while IFS='|' read -r _t _min _models; do
  [ -n "${_t:-}" ] || continue
  if [ "$BUDGET_MB" -ge "$_min" ]; then TIER="$_t"; CANDIDATES="$_models"; break; fi
done < <(printf '%s\n' "$LADDER" | grep -v '^$')

# Deterministic family pick, hashed on the MACHINE — NOT on the identity. Several judge identities
# sharing one GPU MUST run the SAME model: otherwise Ollama juggles several families on one card and
# everything thrashes: co-resident identities on two families exceed the card, Ollama offloads to CPU,
# inference misses its deadline and jobs never settle. Diversity therefore comes from DIFFERENT machines,
# which is exactly what the audit committee needs, and it stays stable across restarts.
_n=$(printf '%s' "$CANDIDATES" | awk -F, '{print NF}')
_h=$(printf '%s' "$MACHINE_KEY" | sha256sum | tr -dc '0-9' | cut -c1-6)
_idx=$(( (10#${_h:-0} % _n) + 1 ))
MODEL="$(printf '%s' "$CANDIDATES" | cut -d, -f"$_idx")"

# JUDGE-ROLE GATE.
# A judge that is too weak does not merely judge badly: it votes DIVERGENT on answers that are correct
# but worded differently, and an unfair verdict costs an honest miner its stake. The project already
# bans qwen3:4b as a judge for exactly that reason; anything below the mistral-nemo class is weaker
# still. Hence: a node may MINE at any tier, but may only JUDGE from tier 3 up. An under-powered judge
# is the fastest way to damage an honest network.
# The judge gate must bind the MODEL, not merely the tier. Tier 3 also contains qwen2.5:7b and
# gemma2:9b, which were NEVER validated as judges (modea/judge.py validates mistral-nemo and the MoE,
# and bans qwen3:4b for unfair verdicts). Since the family pick is a hash, a tier-only check would seat
# an unvalidated judge roughly two times out of three. Allow-list, therefore: an unproven judge model
# never sits, whatever the hardware.
JUDGE_ALLOW="${DENDRA_JUDGE_ALLOWLIST:-mistral-nemo,qwen3:30b-a3b-instruct-2507-q4_K_M}"
_is_allowed_judge(){ case ",$JUDGE_ALLOW," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
CAN_JUDGE=false
[ "$TIER" -ge 3 ] && _is_allowed_judge "$MODEL" && CAN_JUDGE=true

# EXCEPTION — MoE ON PURE CPU. A Mixture-of-Experts activates only a fraction of its weights per token
# (qwen3:30b-a3b = ~3B ACTIVE), so it runs at usable speed on CPU where a DENSE model of the same file
# size would be hopeless. This is the project's ORIGINAL validated judge configuration (MoE q4 on CPU,
# GPU left free for mining, ~10.6 s/case). The generic CPU cap above exists to stop DENSE models from
# crawling — applying it to a MoE would wrongly lock out the ONLY way to bring the audit layer back
# without buying hardware. Requires enough RAM to hold the weights; run ONE such judge per box.
MOE_CPU_MODEL="${DENDRA_MOE_CPU_MODEL:-qwen3:30b-a3b-instruct-2507-q4_K_M}"
MOE_CPU_MIN_RAM_MB="${DENDRA_MOE_CPU_MIN_RAM_MB:-26000}"   # ~18.6 GB weights + OS/mining headroom
# HARD FLOOR, not overridable downwards: without it `DENDRA_MOE_CPU_MIN_RAM_MB=0` (plus a tweaked
# MOE_CPU_MODEL) would turn ANY box into a judge — including with a model far too small to judge fairly.
# An env var must never be able to disable a safety gate; it may only make it stricter.
[ "${MOE_CPU_MIN_RAM_MB:-0}" -ge 24000 ] 2>/dev/null || MOE_CPU_MIN_RAM_MB=24000
# NOTE: this sets the JUDGE model only. `model` (serving/mining) stays the GPU-sized pick — the box
# mines fast on the card AND judges on the CPU at the same time; conflating the two would drag mining
# down to CPU speed and re-create the timeout problem we just fixed.
MOE_CPU=0
JUDGE_MODEL="$MODEL"
if [ "$CAN_JUDGE" != "true" ] && [ "${RAM_MB:-0}" -ge "$MOE_CPU_MIN_RAM_MB" ] && _is_allowed_judge "$MOE_CPU_MODEL"; then
  MOE_CPU=1; CAN_JUDGE=true
  JUDGE_MODEL="$MOE_CPU_MODEL"
fi

# ---------------------------------------------------------------- output
json(){
  printf '{"node_id":"%s","machine":"%s","backend":"%s","gpu":"%s","gpu_count":%d,"vram_mb":%d,"ram_mb":%d,' \
    "$NODE_ID" "$MACHINE_KEY" "$BACKEND" "$GPU_NAME" "$GPU_COUNT" "$VRAM_MB" "$RAM_MB"
  printf '"cpu_cores":%d,"cpu":"%s","budget_mb":%d,"tier":%d,"model":"%s","judge_model":"%s",' \
    "$CPU_CORES" "$CPU_MODEL" "$BUDGET_MB" "$TIER" "$MODEL" "$JUDGE_MODEL"
  printf '"can_judge":%s,"moe_cpu":%d,"candidates":"%s"}\n' "$CAN_JUDGE" "$MOE_CPU" "$CANDIDATES"
}

case "$MODE" in
  json)  json ;;
  model) printf '%s\n' "$MODEL" ;;
  *)
    echo "== [hw] node '$NODE_ID' =="
    if [ "$BACKEND" = "gpu" ]; then
      echo "  GPU        : $GPU_NAME  (x$GPU_COUNT, largest card ${VRAM_MB} MB VRAM)"
    else
      echo "  GPU        : none detected -> CPU inference (slow; small model enforced)"
    fi
    echo "  RAM / CPU  : ${RAM_MB} MB / ${CPU_CORES} cores ($CPU_MODEL)"
    echo "  Budget     : ${BUDGET_MB} MB usable for the model (headroom kept for the KV cache)"
    echo "  Tier       : $TIER   candidates: $CANDIDATES"
    echo "  -> model   : $MODEL   (deterministic pick from the MACHINE = one model per card, diversity across machines)"
    if [ "$MOE_CPU" = "1" ]; then
      echo "  -> role    : MINE (GPU, $MODEL) + JUDGE (CPU MoE, $JUDGE_MODEL)"
      echo "               MoE = few ACTIVE params -> usable on CPU while the GPU keeps mining."
      echo "               Run ONE such judge on this box (MOE_COUNT=1); needs ~19 GB RAM for the weights."
    elif [ "$CAN_JUDGE" = "true" ]; then
      echo "  -> role    : MINE + JUDGE (tier >= 3: this box can host a trustworthy judge)"
    else
      echo "  -> role    : MINE ONLY — too small to JUDGE (tier < 3). A weak judge slashes HONEST miners;"
      echo "               run the audit committee on a machine with >= 12 GB VRAM (>= 24 GB for the MoE judge)."
    fi
    echo
    json
    ;;
esac
