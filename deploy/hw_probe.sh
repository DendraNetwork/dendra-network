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

# ANTI-DEANONYMIZATION GUARD. The pseudonym above is enough to anonymize, but it only protects while
# it is not overwritten: a caller passing `$(hostname)` as an argument defeats the anonymization from
# above, and the probe cannot tell. So any identifier that IS the raw identity of the machine is
# refused, whatever its origin. Naming yourself remains possible, but it must be deliberate
# (DENDRA_NODE_ID), not inherited from a `$(hostname)` forgotten in a script. A privacy guard must
# hold even against its own callers.
# ⚠️ THE TEST IS INCLUSION, NOT EQUALITY. Equality was the original form and it did not hold against
# the caller it was written for: `deploy/join.sh` passed `m-$(hostname)-$RANDOM`, which CONTAINS the
# hostname without ever equalling it, so the guard let it through and the machine name went on-chain.
# A guard whose own comment promises it "must hold even against its own callers" has to match the way
# callers actually build strings: by concatenation.
# The 4-character floor keeps it from biting on hostnames like `pc`, `vm` or `node`, which are common
# substrings and identify nobody.
_HOSTNAME="$(hostname 2>/dev/null || echo '')"
[ "${#_HOSTNAME}" -ge 4 ] || _HOSTNAME='__too_short_to_identify__'
if [ -z "${DENDRA_NODE_ID:-}" ] && [ -n "$NODE_ID" ]; then
  case "$NODE_ID" in
    *"$_HOSTNAME"*|*"$_RAW_MACHINE"*)
      echo "[hw] id '$NODE_ID' is the RAW identity of the machine -> replaced by the pseudonym '$_PSEUDO'." >&2
      echo "     (to publish a chosen name: DENDRA_NODE_ID=<name>; it is a choice, never a default)" >&2
      NODE_ID="$_PSEUDO" ;;
  esac
fi

# ---------------------------------------------------------------- hardware detection
GPU_NAME=""; GPU_COUNT=0; VRAM_MB=0; VRAM_FREE_MB=""
if command -v nvidia-smi >/dev/null 2>&1; then
  # Sum VRAM across GPUs but keep the LARGEST single card as the usable budget: Ollama loads one model
  # on one device by default, so two 8 GB cards do NOT let you run a 12 GB model.
  # `memory.total` IS THE SIZE OF THE CARD, NOT WHAT IS AVAILABLE ON IT. A desktop session, a browser
  # with hardware acceleration, or another model already resident all take VRAM that this figure keeps
  # counting as ours. The tier is still decided on the TOTAL -- it describes what the card can hold, and
  # a machine that will run headless should not be downgraded because someone ran the probe from their
  # desktop -- but the FREE figure is read too, so the gap can be stated instead of discovered when the
  # model refuses to load. Reported, never a verdict: this probe measures capacity, it does not book it.
  while IFS=, read -r _name _mem _free; do
    _name="$(echo "$_name" | sed 's/^ *//;s/ *$//')"; _mem="$(echo "$_mem" | tr -dc '0-9')"
    _free="$(echo "$_free" | tr -dc '0-9')"
    [ -n "${_mem:-}" ] || continue
    GPU_COUNT=$((GPU_COUNT+1))
    [ -z "$GPU_NAME" ] && GPU_NAME="$_name"
    if [ "$_mem" -gt "$VRAM_MB" ]; then VRAM_MB="$_mem"; VRAM_FREE_MB="${_free:-0}"; fi
  done < <(nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits 2>/dev/null)
fi
RAM_MB="$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')"; RAM_MB="${RAM_MB:-0}"
# FREE DISK, ON THE PATH THAT WILL ACTUALLY HOLD THE WEIGHTS. This probe selects models whose q4_K_M
# footprint runs from ~1 GB at tier 0 to ~19 GB at tier 5, and it used to select them without ever
# looking at whether the box could store one. The pull then fails long after the operator has been told
# the machine qualifies -- the worst moment to learn it. Docker keeps images and volumes under its own
# root, which is where the model lands, so that is the filesystem measured; the home directory is the
# fallback when docker is not installed yet. `?` when it cannot be read: unknown is not "enough".
DISK_PATH="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || printf '')"
[ -n "${DISK_PATH:-}" ] && [ -d "$DISK_PATH" ] || DISK_PATH="${HOME:-/}"
DISK_FREE_MB="$(df -Pm "$DISK_PATH" 2>/dev/null | awk 'NR==2{print $4}')"; DISK_FREE_MB="${DISK_FREE_MB:-}"
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
TIER=0; CANDIDATES="llama3.2:1b"; TIER_MIN_MB=0
while IFS='|' read -r _t _min _models; do
  [ -n "${_t:-}" ] || continue
  # TIER_MIN_MB is what the tier REQUIRES; BUDGET_MB is the envelope this machine happens to have.
  # They are different numbers, and only the first answers "will the model fit". Kept here, where
  # the ladder is read, so it cannot drift from the row that granted the tier.
  if [ "$BUDGET_MB" -ge "$_min" ]; then TIER="$_t"; CANDIDATES="$_models"; TIER_MIN_MB="$_min"; break; fi
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
# The allow-list is a SAFETY gate, so the environment may only NARROW it. DENDRA_JUDGE_ALLOWLIST is
# INTERSECTED with the hard-coded list: an operator can forbid a validated model on their own box, and
# can never seat a model the project has not validated. A gate that an env var can widen is not a gate
# -- one exported variable would otherwise install an unfair judge on hardware that passes every other
# check, which is exactly what the un-lowerable MoE RAM floor below refuses on the memory side.
# An intersection that comes out EMPTY seats NO judge: an unusable list fails closed, it never falls
# back to the defaults it was asked to restrict.
_JUDGE_ALLOW_HARD="mistral-nemo,qwen3:30b-a3b-instruct-2507-q4_K_M"
JUDGE_ALLOW="$_JUDGE_ALLOW_HARD"
if [ -n "${DENDRA_JUDGE_ALLOWLIST:-}" ]; then
  JUDGE_ALLOW=""; _rest="${DENDRA_JUDGE_ALLOWLIST},"
  while [ -n "$_rest" ]; do
    _one="${_rest%%,*}"; _rest="${_rest#*,}"
    [ -n "$_one" ] || continue
    case ",$_JUDGE_ALLOW_HARD," in
      *",$_one,"*) JUDGE_ALLOW="${JUDGE_ALLOW:+$JUDGE_ALLOW,}$_one" ;;
    esac
  done
fi
# An empty tag is refused explicitly: with an empty allow-list the substring test would otherwise match
# ",," and report the empty model as allowed.
_is_allowed_judge(){ [ -n "${1:-}" ] || return 1; case ",$JUDGE_ALLOW," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# The tier is sized on VRAM, but a GPU-served judge still needs system RAM: the weights are staged
# through it, and the runtime, the context and the OS live there. A card that clears tier 3 on a box
# with almost no RAM stalls mid-verdict, and a judge that misses its deadline is as costly to an honest
# miner as one that judges badly. So the GPU branch carries its own RAM floor, hard like the MoE one:
# DENDRA_JUDGE_MIN_RAM_MB may raise it, never lower it.
JUDGE_MIN_RAM_MB="${DENDRA_JUDGE_MIN_RAM_MB:-8000}"
[ "${JUDGE_MIN_RAM_MB:-0}" -ge 8000 ] 2>/dev/null || JUDGE_MIN_RAM_MB=8000
# THE TIER THE GATE BINDS, NAMED ONCE — and the VRAM floor DERIVED from it, never retyped beside it.
# The operator message at the end of this file has to tell a reader what hardware seats a judge, and
# a number typed into a message is a copy: it agrees with the rule on the day it is written and drifts
# at the first edit of either. The ladder is compared against the BUDGET, not against the card, so the
# floor is read back through the same 15% headroom taken above — the smallest VRAM whose 85% still
# reaches that budget.
JUDGE_MIN_TIER=3
_JUDGE_MIN_BUDGET_MB="$(printf '%s\n' "$LADDER" | awk -F'|' -v t="$JUDGE_MIN_TIER" '$1==t{print $2; exit}')"
_JUDGE_MIN_VRAM_MB=$(( (${_JUDGE_MIN_BUDGET_MB:-0} * 100 + 84) / 85 ))
# WHICH backend serves the verdicts is recorded, not inferred. Eligibility won on the card and
# eligibility won on RAM have different failure modes and different floors; `can_judge` alone tells no
# operator, and no capacity aggregator, which resource is the one under pressure on that box.
CAN_JUDGE=false
JUDGE_BACKEND="none"
if [ "$TIER" -ge "$JUDGE_MIN_TIER" ] && _is_allowed_judge "$MODEL" && [ "${RAM_MB:-0}" -ge "$JUDGE_MIN_RAM_MB" ]; then
  CAN_JUDGE=true; JUDGE_BACKEND="gpu"
fi

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
  MOE_CPU=1; CAN_JUDGE=true; JUDGE_BACKEND="cpu"
  JUDGE_MODEL="$MOE_CPU_MODEL"
fi

# ---------------------------------------------------------------- output
json(){
  printf '{"node_id":"%s","machine":"%s","backend":"%s","gpu":"%s","gpu_count":%d,"vram_mb":%d,"ram_mb":%d,' \
    "$NODE_ID" "$MACHINE_KEY" "$BACKEND" "$GPU_NAME" "$GPU_COUNT" "$VRAM_MB" "$RAM_MB"
  printf '"vram_free_mb":%s,"disk_free_mb":%s,' "${VRAM_FREE_MB:-null}" "${DISK_FREE_MB:-null}"
  # `tier_min_mb` travels with `budget_mb`: a consumer that has only the envelope cannot tell
  # whether a model fits, and would have to re-derive the ladder — which is how two answers to
  # one question start.
  printf '"cpu_cores":%d,"cpu":"%s","budget_mb":%d,"tier_min_mb":%d,"tier":%d,"model":"%s","judge_model":"%s",' \
    "$CPU_CORES" "$CPU_MODEL" "$BUDGET_MB" "$TIER_MIN_MB" "$TIER" "$MODEL" "$JUDGE_MODEL"
  printf '"can_judge":%s,"moe_cpu":%d,"judge_backend":"%s","candidates":"%s"}\n' \
    "$CAN_JUDGE" "$MOE_CPU" "$JUDGE_BACKEND" "$CANDIDATES"
}

case "$MODE" in
  json)  json ;;
  model) printf '%s\n' "$MODEL" ;;
  *)
    echo "== [hw] node '$NODE_ID' =="
    if [ "$BACKEND" = "gpu" ]; then
      echo "  GPU        : $GPU_NAME  (x$GPU_COUNT, largest card ${VRAM_MB} MB VRAM total)"
      # THE TIER IS DECIDED ON THE TOTAL, THE GAP IS REPORTED. A card that is already holding a
      # desktop session has less than its size available, and the model then fails to load long
      # after this probe said the box qualifies. The tier stays on the total on purpose -- a box
      # that will run headless must not be downgraded because the probe was run from a desktop --
      # so the gap is stated instead of being discovered.
      if [ -n "${VRAM_FREE_MB:-}" ] && [ "${VRAM_FREE_MB:-0}" -gt 0 ] 2>/dev/null; then
        # COMPARE AGAINST WHAT THE TIER REQUIRES, NOT AGAINST THE ENVELOPE IT WAS GRANTED ON.
        # BUDGET_MB is 85 % of the whole card; a tier is granted when that budget CLEARS the ladder
        # row's minimum, and the model is sized for the minimum. Warning on `free < BUDGET_MB` fires on
        # every desktop whose display holds a few hundred MB and tells the operator the load will fail
        # when it will not — the kind of wrong number that makes someone buy a bigger card.
        if [ "$VRAM_FREE_MB" -lt "$TIER_MIN_MB" ] 2>/dev/null; then
          echo "               [!] only ${VRAM_FREE_MB} MB of it is FREE right now, under the ${TIER_MIN_MB} MB this"
          echo "                   tier model needs. As things stand it will not fit: free the card"
          echo "                   (display, another model) or expect the load to fail."
        elif [ "$VRAM_FREE_MB" -lt "$BUDGET_MB" ] 2>/dev/null; then
          echo "               ${VRAM_FREE_MB} MB free right now: above the ${TIER_MIN_MB} MB this tier model"
          echo "               needs, below the ${BUDGET_MB} MB envelope the tier was granted on. It should"
          echo "               load; leave the card to it while it serves."
        else
          echo "               ${VRAM_FREE_MB} MB free right now (clears the ${BUDGET_MB} MB envelope)."
        fi
      fi
    else
      echo "  GPU        : none detected -> CPU inference (slow; small model enforced)"
    fi
    echo "  RAM / CPU  : ${RAM_MB} MB / ${CPU_CORES} cores ($CPU_MODEL)"
    # DISK. The weights have to land somewhere, and this probe picks models from ~1 GB to ~19 GB
    # without ever having looked. `?` is printed when the path cannot be read: unknown is NOT enough.
    if [ -n "${DISK_FREE_MB:-}" ]; then
      echo "  Disk free  : ${DISK_FREE_MB} MB on $DISK_PATH (where the model weights land)"
      if [ "$DISK_FREE_MB" -lt 25000 ] 2>/dev/null; then
        echo "               [!] a tier-4/5 model is 11 to 19 GB on disk, plus room to pull it. Below"
        echo "                   ~25 GB free it is the PULL that fails, long after this line said yes."
      fi
    else
      echo "  Disk free  : ? (unreadable on $DISK_PATH -- unknown, which is NOT the same as enough)"
    fi
    echo "  Budget     : ${BUDGET_MB} MB usable for the model (headroom kept for the KV cache)"
    echo "  Tier       : $TIER   candidates: $CANDIDATES"
    echo "  -> model   : $MODEL   (deterministic pick from the MACHINE = one model per card, diversity across machines)"
    if [ "$MOE_CPU" = "1" ]; then
      echo "  -> role    : MINE (GPU, $MODEL) + JUDGE (CPU MoE, $JUDGE_MODEL)"
      echo "               MoE = few ACTIVE params -> usable on CPU while the GPU keeps mining."
      echo "               Run ONE such judge on this box (MOE_COUNT=1); the ${MOE_CPU_MIN_RAM_MB} MB RAM floor it just"
      echo "               cleared is what holds the weights alongside the OS and the mining process."
    elif [ "$CAN_JUDGE" = "true" ]; then
      echo "  -> role    : MINE + JUDGE (tier >= 3, judge served on the GPU with $JUDGE_MODEL)"
      echo "               RAM floor of ${JUDGE_MIN_RAM_MB} MB cleared: the card holds the weights, the box stages them."
    else
      echo "  -> role    : MINE ONLY — this box does not clear the JUDGE gate (tier, model or RAM). A weak"
      echo "               or RAM-starved judge slashes HONEST miners. What the gate above ACTUALLY applies,"
      echo "               all three at once, on the GPU path:"
      echo "                 · tier >= $JUDGE_MIN_TIER, i.e. a budget of ${_JUDGE_MIN_BUDGET_MB} MB, i.e. >= ${_JUDGE_MIN_VRAM_MB} MB of VRAM on the"
      echo "                   largest single card (the budget keeps 15% back for the KV cache);"
      echo "                 · >= ${JUDGE_MIN_RAM_MB} MB of system RAM;"
      echo "                 · a model from the judge allow-list ($JUDGE_ALLOW)."
      echo "               The family is picked by hash among the whole tier, so clearing the VRAM floor does"
      echo "               not on its own seat a judge here — more VRAM raises the odds, it does not decide."
      echo "               Without any card: >= ${MOE_CPU_MIN_RAM_MB} MB of RAM seats the CPU MoE judge instead."
    fi
    echo
    json
    ;;
esac
