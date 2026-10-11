#!/usr/bin/env bash
# hw_probe.sh -- detect a node's hardware, pick the model tier it can actually run, and emit a JSON
# capability report. Used by the join and miner kits to select and install the right model, and by the
# network capacity aggregator to know what compute the network really has.
#
# Why a tier ladder: a model that does not fit in VRAM is silently offloaded to the CPU by Ollama, which
# turns a 3-second inference into a multi-minute one and makes the miner miss its deadline
# (observed in production: mistral-nemo 8.7 GB on an 8 GB card -> 30%/70% GPU/CPU -> ReadTimeout).
# So the model is sized to the measured VRAM, with headroom for the KV cache and context window.
#
# Model diversity across the network (the audit committee needs at least 2 distinct judge models) does
# not require two models per node: each node deterministically picks one family among those that fit,
# hashed on its machine. Different machines pick different families, so diversity emerges on its own.
#
# Usage:
#   bash deploy/hw_probe.sh                 # human report + JSON
#   bash deploy/hw_probe.sh --json <id>     # JSON only (machine-readable), id seeds the family pick
#   MODEL=$(bash deploy/hw_probe.sh --model judge1)   # just the chosen model tag
#   bash deploy/hw_probe.sh --can-judge     # one word: true | false | unknown
#   bash deploy/hw_probe.sh --list-gpus     # one line per NVIDIA card, for a shell (see below)
#   bash deploy/hw_probe.sh --gpu GPU-<uuid> --json <id>   # the report of a single card (one identity per card)
#   bash deploy/hw_probe.sh --judge-floor-mb               # the judge's RAM floor, after its hard clamp
#   bash deploy/hw_probe.sh --role [--no-gpu]              # the role of this machine on the testnet (below)
#   bash deploy/hw_probe.sh --judge-allowed <model>        # one word: true | false -- is <model> on the judge
#                                                          # allow-list below (the hard list, narrowed by
#                                                          # DENDRA_JUDGE_ALLOWLIST)? A reading of this file's list,
#                                                          # asked by deploy/join.sh of a DENDRA_JUDGE_MODEL
#
# --role is the one decision every installer applies (deploy/join.sh, deploy/install.sh and through it
# deploy/install.ps1 and the HiveOS package, docker/cloud-start.sh). Line 1 is one word, the lines after it say
# why, in words an operator can act on:
#   miner    a usable NVIDIA card: the machine mines on the card, as it always did (exit 0)
#   judge    no usable card and at least MOE_CPU_MIN_RAM_MB of system RAM: the machine judges on the CPU, and
#            answers the requests the chain assigns it with that same judge model, on the kit's CPU instance.
#            No mining model is pulled (exit 0)
#   refused  no usable card, and the RAM below MOE_CPU_MIN_RAM_MB (or the judge model off this box's allow-list):
#            the testnet runs no mining model on the CPU, so this machine has no role (exit 0: a reading)
#   unknown  no usable card, and the system RAM could not be read: nothing was decided, and an unread gate is
#            never a pass (exit 3)
# --no-gpu decides as if this host had no usable card: the caller measured that its engine cannot reach the card
# the host lists (deploy/join.sh: Docker without the NVIDIA container toolkit).
#
# --can-judge answers one question, for a caller that has to decide before anything is installed
# (deploy/install.sh sizes its disk floor on it): does this box clear the judge gate below? Three
# answers, never two: `true` and `false` are readings and exit 0; `unknown` exits 3 and means the RAM
# could not be read, so nothing was decided. A caller must not fold `unknown` into either reading.
#
# --list-gpus prints one line per card, in a form a shell splits without parsing JSON:
#   <index>|<uuid>|<vram_total_mb>|<vram_free_mb>|<tier>|<model>|<pci_bus_id>|<name>
# The name comes last because it may hold spaces. tier and model come from the same ladder and the same
# family pick (hashed on the machine) as the report below: one function, called by both paths. A field
# that could not be read is `?`, never 0. Three states, never two: no nvidia-smi on this host -> no line,
# exit 0 (a reading: this kit has no NVIDIA card to use); nvidia-smi present and failing -> no line,
# exit 3 (unknown); otherwise one line per card, exit 0. "No devices were found" is nvidia-smi's own
# reading of zero cards, and is read as such.
# --gpu <uuid> keeps only that card: its name, its VRAM, gpu_count 1. A UUID that is not a card of this
# host is refused (exit 2, nothing on stdout), never replaced by the largest card or by the CPU tier,
# since the identity it was asked for runs on that card and on no other. A failing nvidia-smi exits 3.
# The UUID never appears in the JSON: it is a unique hardware identifier, and the JSON is published.
# --no-docker skips the one docker call (the disk Docker stores the weights on), which is then
# reported unread; deploy/join.sh --plan uses it so a plan touches no engine.
set -u

MODE="report"; NODE_ID=""; GPU_UUID=""; NO_DOCKER=0; NO_GPU=0; JA_TAG=""
# Several flags may be combined (`--gpu <uuid> --json <id>`). An <id> follows --json or --model only when
# it does not itself start with `--`: the forms callers already use (`--json`, `--json <id>`,
# `--model <id>`, `--can-judge`, a bare id, no argument) keep their meaning.
while [ $# -gt 0 ]; do
  case "$1" in
    --json|--model)
      MODE="${1#--}"; shift
      if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then NODE_ID="$1"; shift; fi ;;
    --can-judge) MODE="can-judge"; shift ;;
    --list-gpus) MODE="list-gpus"; shift ;;
    --judge-floor-mb) MODE="judge-floor-mb"; shift ;;
    --role) MODE="role"; shift ;;
    --judge-allowed)
      MODE="judge-allowed"; JA_TAG="${2:-}"
      [ -n "$JA_TAG" ] || { echo "[hw] --judge-allowed needs a model tag" >&2; exit 2; }
      shift 2 ;;
    --no-gpu) NO_GPU=1; shift ;;
    --no-docker) NO_DOCKER=1; shift ;;
    --gpu)
      GPU_UUID="${2:-}"
      [ -n "$GPU_UUID" ] || { echo "[hw] --gpu needs the UUID of a card (GPU-...); nvidia-smi -L lists them" >&2; exit 2; }
      shift 2 ;;
    --*) echo "[hw] unknown option: $1" >&2; exit 2 ;;
    *) NODE_ID="$1"; shift ;;
  esac
done
# The one form a card UUID takes here. MIG instances (MIG-...) are refused: one identity per physical card.
GPU_UUID_RE='^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
if [ -n "$GPU_UUID" ] && ! printf '%s\n' "$GPU_UUID" | grep -Eq "$GPU_UUID_RE"; then
  echo "[hw] --gpu '$GPU_UUID' is not the UUID of a card (GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx, lower-case hex)" >&2
  exit 2
fi
# A card asked for and "no usable card" in one call contradict each other: refused, never one of the two picked.
if [ -n "$GPU_UUID" ] && [ "$NO_GPU" = 1 ]; then
  echo "[hw] --gpu and --no-gpu together: one names a card to report, the other says no card is usable" >&2
  exit 2
fi
# Privacy: these two values are published to the public capacity registry and shown on the public
# explorer. A raw hostname ("DESKTOP-AB12CD") or a raw /etc/machine-id identifies the operator's
# personal machine to anyone loading the page. So a stable pseudonym derived by hash is published
# instead: it keeps every property needed (same box -> same id across restarts, one model per machine,
# deduplication in the registry) while revealing nothing. An operator who wants to be identified sets
# DENDRA_NODE_ID.
_RAW_MACHINE="$(cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo machine)"
MACHINE_KEY="$(printf '%s' "$_RAW_MACHINE" | sha256sum | cut -c1-12)"   # pseudonym, never the raw id
_PSEUDO="dendra-$(printf '%s' "$_RAW_MACHINE" | sha256sum | cut -c1-8)"
[ -n "$NODE_ID" ] || NODE_ID="${DENDRA_NODE_ID:-$_PSEUDO}"

# De-anonymisation guard. The pseudonym above only protects while it is not overwritten: a caller passing
# `$(hostname)` as an argument would defeat it, and the probe cannot tell. So any identifier that contains
# the raw identity of the machine is refused, whatever its origin. Naming yourself remains possible, but it
# must be deliberate (DENDRA_NODE_ID), not inherited from a `$(hostname)` forgotten in a script.
# The test is containment, not equality: `deploy/join.sh` once passed `m-$(hostname)-$RANDOM`, which
# contains the hostname without equalling it, and the machine name went on chain.
# The 4-character floor keeps it from matching hostnames like `pc`, `vm` or `node`, which are common
# substrings and identify nobody.
_HOSTNAME="$(hostname 2>/dev/null || echo '')"
[ "${#_HOSTNAME}" -ge 4 ] || _HOSTNAME='__too_short_to_identify__'
if [ -z "${DENDRA_NODE_ID:-}" ] && [ -n "$NODE_ID" ]; then
  case "$NODE_ID" in
    *"$_HOSTNAME"*|*"$_RAW_MACHINE"*)
      echo "[hw] id '$NODE_ID' is the raw identity of the machine -> replaced by the pseudonym '$_PSEUDO'." >&2
      echo "     (to publish a chosen name: DENDRA_NODE_ID=<name>; it is a choice, never a default)" >&2
      NODE_ID="$_PSEUDO" ;;
  esac
fi

# ---------------------------------------------------------------- hardware detection
GPU_NAME=""; GPU_COUNT=0; VRAM_MB=0; VRAM_FREE_MB=""
# The cards, one row each, read once: index, UUID, PCI bus id, total and free memory, and the name last (a
# name may hold a comma, and nothing after it has to be split). The PCI bus id is read because the engine
# uses it: measured on ollama 0.32.1, its "inference compute" line names a card by index and pci_id, never
# by UUID, so the per-card pinning check (deploy/testnet-miner/slots.sh) needs it.
# GPU_SMI: absent (no nvidia-smi: a reading, no card this kit can use) | none (nvidia-smi's own "No
# devices were found") | read | failed (present and failing: unknown, never zero cards).
GPU_SMI=absent; GPU_ROWS=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_ROWS="$(nvidia-smi --query-gpu=index,uuid,pci.bus_id,memory.total,memory.free,name --format=csv,noheader,nounits 2>/dev/null)"
  _smi_rc=$?
  if [ "$_smi_rc" = 0 ]; then
    GPU_SMI=read
  elif nvidia-smi -L 2>&1 | grep -q 'No devices were found'; then
    GPU_SMI=none; GPU_ROWS=""
  else
    GPU_SMI=failed
  fi
fi
# --gpu on a host whose cards cannot be read: unknown, and nothing is printed, since a report of another card
# or of the CPU would be published as the report of the identity that runs on the requested card. The same
# goes for a card that is listed but whose memory is not (`[N/A]`): its row would be skipped below, and the
# report would fall to the CPU tier, published as the reading of that card.
if [ -n "$GPU_UUID" ] && [ "$MODE" != "list-gpus" ] && [ "$MODE" != "judge-floor-mb" ]; then
  case "$GPU_SMI" in
    failed) echo "[hw] nvidia-smi is present and failing: the card $GPU_UUID cannot be read (unknown, nothing reported)" >&2; exit 3 ;;
    read) _gm="$(printf '%s\n' "$GPU_ROWS" | awk -F, -v u="$GPU_UUID" '{g=$2; gsub(/ /,"",g); if (g==u) { f=1; m=$4; gsub(/ /,"",m); if (m ~ /^[0-9]+$/) r=1 } } END{print (!f) ? "absent" : (r ? "read" : "unread")}')"
      case "$_gm" in
        read) : ;;
        unread) echo "[hw] --gpu $GPU_UUID: nvidia-smi lists this card but not its memory: unknown, nothing reported" >&2; exit 3 ;;
        *) echo "[hw] --gpu $GPU_UUID is not a card of this host (nvidia-smi -L lists them): nothing reported" >&2; exit 2 ;;
      esac ;;
    *) echo "[hw] --gpu $GPU_UUID: this host has no NVIDIA card nvidia-smi can list: nothing reported" >&2; exit 2 ;;
  esac
fi
if [ "$GPU_SMI" = read ]; then
  # Count the GPUs but keep the largest single card as the usable budget: Ollama loads one model on one
  # device by default, so two 8 GB cards do not let you run a 12 GB model.
  # `memory.total` is the size of the card, not what is available on it. A desktop session, a browser with
  # hardware acceleration, or another resident model all take VRAM that this figure still counts. The tier
  # is decided on the total, since it describes what the card can hold and a machine that will run headless
  # should not be downgraded because the probe ran from a desktop. The free figure is read too, so the gap
  # can be reported before the model fails to load. It is reported, never a verdict: this probe measures
  # capacity, it does not reserve it.
  # With --gpu, only that card's row is kept: its name, its VRAM, and a count of one.
  while IFS=, read -r _idx _uuid _pci _mem _free _name; do
    _name="$(echo "$_name" | sed 's/^ *//;s/ *$//')"; _mem="$(echo "$_mem" | tr -dc '0-9')"
    _free="$(echo "$_free" | tr -dc '0-9')"; _uuid="$(echo "$_uuid" | tr -d ' ')"
    [ -n "${_mem:-}" ] || continue
    [ -z "$GPU_UUID" ] || [ "$_uuid" = "$GPU_UUID" ] || continue
    GPU_COUNT=$((GPU_COUNT+1))
    [ -z "$GPU_NAME" ] && GPU_NAME="$_name"
    if [ "$_mem" -gt "$VRAM_MB" ]; then VRAM_MB="$_mem"; VRAM_FREE_MB="${_free:-0}"; fi
  done < <(printf '%s\n' "$GPU_ROWS")
fi
# --no-gpu: the caller measured that its engine cannot reach the card this host lists. The card stays named (the
# role's reason says which one is unusable), and none counts: the machine is treated as one without a card.
GPU_SEEN="$GPU_NAME"
if [ "$NO_GPU" = 1 ]; then GPU_COUNT=0; VRAM_MB=0; VRAM_FREE_MB=""; GPU_NAME=""; fi
# System RAM. An unreadable value stays unread: this figure gates the judge seat below, and falling back to 0
# would report a box whose memory could not be read as one that cannot judge. Empty means unread: the judge
# gate then decides nothing (--can-judge says `unknown`), the JSON says null, and only the CPU model size
# below takes the smallest tier, which is the safe direction for a size and never a verdict on the gate.
# LC_ALL=C: procps-ng translates the row label ("Mem:" reads "Speicher:" in German), and a label this line
# cannot find would leave the RAM unread on a host that has all the memory the judge needs.
RAM_MB="$(LC_ALL=C free -m 2>/dev/null | awk '/^Mem:/{print $2; exit}')"
case "$RAM_MB" in ''|*[!0-9]*) RAM_MB="" ;; esac
# Free disk, on the path that will actually hold the weights. This probe selects models whose q4_K_M
# footprint runs from ~1 GB at tier 0 to ~19 GB at tier 5; without this check the pull fails long after the
# operator has been told the machine qualifies. Docker keeps images and volumes under its own root, which is
# where the model lands, so that is the filesystem measured; the home directory is the fallback when docker
# is not installed yet. `?` when it cannot be read: unknown is not "enough".
# --no-docker, and the modes that print no disk figure, ask the engine nothing: the figure is then unread
# (null in the JSON, `?` in the report), never measured somewhere else under the same name.
if [ "$NO_DOCKER" = 1 ] || [ "$MODE" = "list-gpus" ] || [ "$MODE" = "judge-floor-mb" ] || [ "$MODE" = "role" ] || [ "$MODE" = "judge-allowed" ]; then
  DISK_PATH="(not measured: no docker call was made)"; DISK_FREE_MB=""
else
DISK_PATH="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || printf '')"
[ -n "${DISK_PATH:-}" ] && [ -d "$DISK_PATH" ] || DISK_PATH="${HOME:-/}"
DISK_FREE_MB="$(df -Pm "$DISK_PATH" 2>/dev/null | awk 'NR==2{print $4}')"; DISK_FREE_MB="${DISK_FREE_MB:-}"
fi
CPU_CORES="$(nproc 2>/dev/null || echo 1)"
CPU_MODEL="$(awk -F: '/model name/{gsub(/^ +/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null || echo unknown)"

# Usable budget in MB. GPU: keep 15% headroom for the KV cache and context (a model at 100% of VRAM thrashes).
# No GPU: fall back to RAM with a much lower cap, since CPU inference is slow. (On the testnet no machine mines
# on the CPU any more, see the role below: these CPU rows only fill `tier` and `budget_mb`.)
if [ "$VRAM_MB" -gt 0 ]; then
  BUDGET_MB=$(( VRAM_MB * 85 / 100 )); BACKEND="gpu"
else
  BUDGET_MB=$(( ${RAM_MB:-0} * 40 / 100 )); BACKEND="cpu"
  [ "$BUDGET_MB" -gt 4096 ] && BUDGET_MB=4096      # never pick a big model for CPU-only inference
fi

# ---------------------------------------------------------------- model ladder
# tier|min_budget_MB|comma-separated candidates (distinct families -> network diversity)
# Sizes are the q4_K_M on-disk/VRAM footprint; keep this table in sync with what the miners pull.
LADDER="
5|19500|qwen3:30b-a3b-instruct-2507-q4_K_M
4|11000|qwen3:14b,mistral-nemo
3|9200|mistral-nemo,qwen2.5:7b,gemma2:9b
2|6000|llama3.1:8b-instruct-q4_K_M,qwen2.5:7b
1|2500|llama3.2:3b,qwen2.5:3b
0|0|llama3.2:1b
"
# _tier_for <budget_mb> -> sets TIER, CANDIDATES, TIER_MIN_MB from the ladder. One function, called for the
# machine below and for each card by --list-gpus, so the per-card tier cannot drift from the one the report
# publishes.
_tier_for(){
  local _t _min _models
  TIER=0; CANDIDATES="llama3.2:1b"; TIER_MIN_MB=0
  while IFS='|' read -r _t _min _models; do
    [ -n "${_t:-}" ] || continue
    # TIER_MIN_MB is what the tier requires; BUDGET_MB is the envelope this machine happens to have.
    # Only the first answers "will the model fit". Set here, where the ladder is read, so it cannot
    # drift from the row that granted the tier.
    if [ "$1" -ge "$_min" ]; then TIER="$_t"; CANDIDATES="$_models"; TIER_MIN_MB="$_min"; break; fi
  done < <(printf '%s\n' "$LADDER" | grep -v '^$')
}

# Deterministic family pick, hashed on the machine, not on the identity. Several identities sharing one GPU
# must run the same model: two families on one card exceed it, Ollama offloads to the CPU, inference misses
# its deadline and jobs never settle. Diversity therefore comes from different machines, which is what the
# audit committee needs, and it stays stable across restarts. Every card of this machine uses the same hash:
# two cards of one tier run one model, kept once in a shared model store.
_model_for(){ # _model_for <candidates> -> sets MODEL (and nothing else: its counters are local)
  local _n _h _pick
  _n=$(printf '%s' "$1" | awk -F, '{print NF}')
  _h=$(printf '%s' "$MACHINE_KEY" | sha256sum | tr -dc '0-9' | cut -c1-6)
  _pick=$(( (10#${_h:-0} % _n) + 1 ))
  MODEL="$(printf '%s' "$1" | cut -d, -f"$_pick")"
}
_tier_for "$BUDGET_MB"
_model_for "$CANDIDATES"

# Judge role gate.
# A judge that is too weak votes "divergent" on answers that are correct but worded differently, and an
# unfair verdict costs an honest miner its stake. qwen3:4b is banned as a judge for that reason, and anything
# below the mistral-nemo class is weaker still. So a node may mine at any tier, but judges only with an
# allow-listed model, on the CPU (the MoE below, gated on system RAM).
# The gate binds the model: modea/judge.py validates mistral-nemo and the MoE, and bans qwen3:4b for unfair
# verdicts, so an unproven judge model never sits, whatever the hardware.
# The allow-list is a safety gate, so the environment may only narrow it: DENDRA_JUDGE_ALLOWLIST is
# intersected with the hard-coded list. An operator can forbid a validated model on their own box, but can
# never seat a model the project has not validated; otherwise one exported variable would install an unfair
# judge on hardware that passes every other check (the MoE RAM floor below refuses the same on the memory side).
# An empty intersection seats no judge: an unusable list fails closed and never falls back to the defaults
# it was meant to restrict.
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

# No judge is seated on the GPU. A judge seated there from tier 3 up with the card's own model
# (mistral-nemo) could never vote, for three reasons found in the code that applies them:
#   - deploy/join.sh points every judge at the kit's CPU instance (DENDRA_JUDGE_ENDPOINT=ollama-cpu);
#   - the judge model a juror runs is the one the network pins on chain, read first
#     (judge_worker.py::resolve_judge_model, modelregistry `audit_judge_model`): the MoE below;
#   - the kit downloads the model this file names, not the pinned one.
# A GPU-tier box therefore pulled mistral-nemo into the CPU instance, waited 30 minutes for the pinned MoE
# that never came, and sat on juries without voting. Judging is the CPU path below, with or without a card:
# the pinned MoE on the CPU, gated on system RAM. `judge_backend` stays in the JSON ("cpu" or "none")
# because the capacity registry reads it.
CAN_JUDGE=false
JUDGE_BACKEND="none"
# Declared rather than inferred from an absence: the site-truth guard (development repository) reads this line,
# and refuses any page that announces a VRAM floor for judging while it says 0.
JUDGE_ON_GPU=0

# The judge: the MoE on the CPU. A mixture-of-experts activates only a fraction of its weights per token
# (qwen3:30b-a3b = ~3B active), so it runs at usable speed on the CPU where a dense model of the same file
# size would not. This is the project's original validated judge configuration (MoE q4 on CPU, GPU left
# free for mining, ~10.6 s per case). The generic CPU cap above exists to keep dense models from crawling;
# applying it to a MoE would lock out the only way to judge without buying hardware. It needs enough RAM to
# hold the weights; run one such judge per box.
MOE_CPU_MODEL="${DENDRA_MOE_CPU_MODEL:-qwen3:30b-a3b-instruct-2507-q4_K_M}"
MOE_CPU_MIN_RAM_MB="${DENDRA_MOE_CPU_MIN_RAM_MB:-26000}"   # ~18.6 GB weights + OS/mining headroom
# Hard floor that cannot be lowered: without it `DENDRA_MOE_CPU_MIN_RAM_MB=0` (plus a changed MOE_CPU_MODEL)
# would turn any box into a judge, including with a model far too small to judge fairly. An environment
# variable may raise the default, or lower it down to this floor, never past it.
[ "${MOE_CPU_MIN_RAM_MB:-0}" -ge 24000 ] 2>/dev/null || MOE_CPU_MIN_RAM_MB=24000
# This sets the judge model only. On a box with a card, `model` (serving and mining) stays the GPU-sized pick:
# the box mines on the card and judges on the CPU at the same time. Mixing the two would drag mining down to
# CPU speed and bring back the timeouts. Without a card, see the role below.
MOE_CPU=0
JUDGE_MODEL="$MODEL"
if [ "$CAN_JUDGE" != "true" ] && [ -n "$RAM_MB" ] && [ "$RAM_MB" -ge "$MOE_CPU_MIN_RAM_MB" ] && _is_allowed_judge "$MOE_CPU_MODEL"; then
  MOE_CPU=1; CAN_JUDGE=true; JUDGE_BACKEND="cpu"
  JUDGE_MODEL="$MOE_CPU_MODEL"
fi

# ---------------------------------------------------------------- the role of this machine on the testnet
# No mining model on the CPU. A machine without a usable card does not mine with the small model its RAM allows
# (the CPU rows of the ladder above): on the testnet such a machine is a judge when it clears the judge gate just
# above, and is refused when it does not. The chain draws a judge like any present miner, including as the
# primary of a request (chain/x/jobs/keeper/committee.go::deriveCommitteeOrdered draws on vitality and
# presence, with no role), so it answers those requests with the same judge model on the same CPU instance
# rather than leaving them open. An unread RAM decides nothing. A machine with a usable card is unchanged.
# ROLE_WHY says why in words an operator can act on; every installer prints it (--role).
ROLE_WHY=""
if [ "$VRAM_MB" -gt 0 ]; then
  ROLE=miner
  ROLE_WHY="a usable NVIDIA card ($GPU_NAME, $VRAM_MB MB of VRAM): this machine mines on the card, with $MODEL."
else
  # Why no card is usable, in the words the operator can act on.
  case "$NO_GPU:$GPU_SMI" in
    1:*) _nocard="the NVIDIA card this host lists (${GPU_SEEN:-?}) is not reachable by the container engine (measured by the caller: with the NVIDIA container toolkit installed, the card mines)" ;;
    0:failed) _nocard="nvidia-smi is installed but fails, so no card can be read here (when nvidia-smi answers, a card mines)" ;;
    *) _nocard="no NVIDIA card this kit can use" ;;
  esac
  if [ -z "$RAM_MB" ]; then
    ROLE=unknown; MODEL=""; CANDIDATES=""
    ROLE_WHY="$_nocard, and the system RAM could not be read (free -m): whether this machine reaches MOE_CPU_MIN_RAM_MB (${MOE_CPU_MIN_RAM_MB} MB) is unknown, and an unread gate is not a pass. Nothing was decided."
  elif [ "$CAN_JUDGE" = true ]; then
    ROLE=judge; MODEL="$JUDGE_MODEL"; CANDIDATES="$JUDGE_MODEL"
    ROLE_WHY="$_nocard, and ${RAM_MB} MB of system RAM, at or above MOE_CPU_MIN_RAM_MB (${MOE_CPU_MIN_RAM_MB} MB): this machine judges on the CPU with $JUDGE_MODEL, and answers the requests the chain assigns it with that same model, on the kit's CPU instance. No mining model is pulled."
  else
    ROLE=refused; MODEL=""; CANDIDATES=""
    if [ "$RAM_MB" -ge "$MOE_CPU_MIN_RAM_MB" ]; then
      _short="the judge model $MOE_CPU_MODEL is off this box's judge allow-list (DENDRA_JUDGE_ALLOWLIST leaves: ${JUDGE_ALLOW:-nothing})"
    else
      _short="${RAM_MB} MB of system RAM is below MOE_CPU_MIN_RAM_MB (${MOE_CPU_MIN_RAM_MB} MB, deploy/hw_probe.sh), the judge's floor on the CPU"
    fi
    ROLE_WHY="$_nocard, and $_short. The testnet runs no mining model on the CPU, so this machine has no role and nothing is installed. What works: a machine with an NVIDIA card (it mines on the card), or one with at least ${MOE_CPU_MIN_RAM_MB} MB of system RAM (it joins as a judge)."
    [ -n "${WSL_DISTRO_NAME:-}" ] && ROLE_WHY="$ROLE_WHY Under WSL 2 this is the RAM of the WSL VM, not the PC's: raise [wsl2] memory= in the .wslconfig of your Windows profile."
  fi
fi

# ---------------------------------------------------------------- output
json(){
  # `ram_mb` is null when unread, like the two figures on the next line: never a 0 nobody measured.
  printf '{"node_id":"%s","machine":"%s","backend":"%s","gpu":"%s","gpu_count":%d,"vram_mb":%d,"ram_mb":%s,' \
    "$NODE_ID" "$MACHINE_KEY" "$BACKEND" "$GPU_NAME" "$GPU_COUNT" "$VRAM_MB" "${RAM_MB:-null}"
  printf '"vram_free_mb":%s,"disk_free_mb":%s,' "${VRAM_FREE_MB:-null}" "${DISK_FREE_MB:-null}"
  # `tier_min_mb` travels with `budget_mb`: a consumer that has only the envelope cannot tell
  # whether a model fits, and would have to re-derive the ladder.
  printf '"cpu_cores":%d,"cpu":"%s","budget_mb":%d,"tier_min_mb":%d,"tier":%d,"model":"%s","judge_model":"%s",' \
    "$CPU_CORES" "$CPU_MODEL" "$BUDGET_MB" "$TIER_MIN_MB" "$TIER" "$MODEL" "$JUDGE_MODEL"
  # `can_judge` is null on an unread RAM, as --can-judge says unknown: the gate decided nothing, and a
  # consumer must not read a decision into it. A capacity registry that only counts judges reads null as
  # "not a judge", which is the safe direction; a gate reads it as unknown.
  # `role` is the decision --role prints, null when it is unknown (the same reading as `can_judge` above).
  # `model` is what the machine serves: the card's pick, or the judge model on the CPU, or "" with no role.
  printf '"can_judge":%s,"moe_cpu":%d,"judge_backend":"%s","role":%s,"candidates":"%s"}\n' \
    "$( [ -n "$RAM_MB" ] && printf '%s' "$CAN_JUDGE" || printf null)" "$MOE_CPU" "$JUDGE_BACKEND" \
    "$( [ "$ROLE" = unknown ] && printf null || printf '"%s"' "$ROLE")" "$CANDIDATES"
}

case "$MODE" in
  json)  json ;;
  model)
    # The model this machine serves. A machine with no role serves none: nothing is printed, and the exit
    # code says which case it is (2 refused, 3 unknown). An empty line is never a model.
    case "$ROLE" in
      refused) echo "[hw] no model: $ROLE_WHY" >&2; exit 2 ;;
      unknown) echo "[hw] no model: $ROLE_WHY" >&2; exit 3 ;;
    esac
    printf '%s\n' "$MODEL" ;;
  role)
    # Line 1: the decision, one word. Then why. A reading exits 0 (miner, judge, refused); unknown exits 3.
    printf '%s\n%s\n' "$ROLE" "$ROLE_WHY"
    [ "$ROLE" = unknown ] && exit 3
    exit 0 ;;
  judge-allowed)
    # A reading of the allow-list above, the one that seats a judge here: true or false, exit 0 both ways. A
    # caller that is handed a model by an operator (deploy/join.sh, DENDRA_JUDGE_MODEL) asks it here rather than
    # keep a copy of the list, which would drift from the one this file applies.
    if _is_allowed_judge "$JA_TAG"; then echo true; else echo false; fi ;;
  judge-floor-mb)
    # The floor after its hard clamp: the number a caller reserves for the judge is derived here, where the
    # gate applies it, and never retyped elsewhere.
    printf '%s\n' "$MOE_CPU_MIN_RAM_MB" ;;
  list-gpus)
    case "$GPU_SMI" in
      failed) echo "[hw] nvidia-smi is present and failing: the cards of this host are unknown (not zero)" >&2; exit 3 ;;
      absent|none) exit 0 ;;
    esac
    while IFS= read -r _row; do
      # A blank line is no card. Any other line is one row: a line nvidia-smi printed instead of a card (a
      # lost card's "Unable to determine the device handle for GPU0000:03:00.0: Unknown Error") is a card
      # whose fields are unread, `?`. It is never skipped, since the host does not have fewer cards because
      # one could not be read. The index is the field as nvidia-smi wrote it, or `?`: digits picked out of
      # that message would be an index nobody wrote.
      case "$_row" in *[![:space:]]*) : ;; *) continue ;; esac
      IFS=, read -r _ci _uuid _pci _mem _free _name <<< "$_row"
      _ci="$(echo "$_ci" | tr -d ' ')"; case "$_ci" in ""|*[!0-9]*) _ci="" ;; esac
      _uuid="$(echo "$_uuid" | tr -d ' ')"; _pci="$(echo "$_pci" | tr -d ' ')"
      _mem="$(echo "$_mem" | tr -dc '0-9')"; _free="$(echo "$_free" | tr -dc '0-9')"
      _name="$(echo "$_name" | sed 's/^ *//;s/ *$//')"
      printf '%s\n' "$_uuid" | grep -Eq "$GPU_UUID_RE" || _uuid="?"
      if [ -n "$_mem" ]; then
        _tier_for $(( _mem * 85 / 100 )); _model_for "$CANDIDATES"; _t="$TIER"; _m="$MODEL"
      else
        _t="?"; _m="?"
      fi
      printf '%s|%s|%s|%s|%s|%s|%s|%s\n' "${_ci:-?}" "$_uuid" "${_mem:-?}" "${_free:-?}" "$_t" "$_m" "${_pci:-?}" "${_name:-?}"
    done < <(printf '%s\n' "$GPU_ROWS")
    ;;
  can-judge)
    # The gate above is the only decision: this mode prints it and does not re-derive it. An unread RAM
    # left CAN_JUDGE false without deciding anything, so it is handled here first.
    if [ -z "$RAM_MB" ]; then printf 'unknown\n'; exit 3; fi
    printf '%s\n' "$CAN_JUDGE" ;;
  *)
    echo "== [hw] node '$NODE_ID' =="
    if [ "$BACKEND" = "gpu" ]; then
      echo "  GPU        : $GPU_NAME  (x$GPU_COUNT, largest card ${VRAM_MB} MB VRAM total)"
      # The tier is decided on the total and the gap is reported. A card already holding a desktop
      # session has less than its size available, and the model may then fail to load. The tier stays
      # on the total on purpose, since a box that will run headless must not be downgraded because the
      # probe ran from a desktop, so the gap is reported instead.
      if [ -n "${VRAM_FREE_MB:-}" ] && [ "${VRAM_FREE_MB:-0}" -gt 0 ] 2>/dev/null; then
        # Compare with what the tier requires, not with the envelope it was granted on. BUDGET_MB is
        # 85 % of the whole card; a tier is granted when that budget clears the ladder row's minimum,
        # and the model is sized for the minimum. Warning on `free < BUDGET_MB` would fire on every
        # desktop whose display holds a few hundred MB, predicting a failed load that will not happen.
        if [ "$VRAM_FREE_MB" -lt "$TIER_MIN_MB" ] 2>/dev/null; then
          echo "               [!] only ${VRAM_FREE_MB} MB of it is free right now, under the ${TIER_MIN_MB} MB this"
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
      echo "  GPU        : none usable ($_nocard)"
      echo "               -> no mining model on the CPU on the testnet: the role below decides"
    fi
    echo "  RAM / CPU  : ${RAM_MB:-?} MB / ${CPU_CORES} cores ($CPU_MODEL)$( [ -n "$RAM_MB" ] || printf '  (RAM unread: the judge gate decides nothing)' )"
    # Disk: the weights have to land somewhere, and this probe picks models from ~1 GB to ~19 GB.
    # `?` is printed when the path cannot be read: unknown is not enough.
    if [ -n "${DISK_FREE_MB:-}" ]; then
      echo "  Disk free  : ${DISK_FREE_MB} MB on $DISK_PATH (where the model weights land)"
      if [ "$DISK_FREE_MB" -lt 25000 ] 2>/dev/null; then
        echo "               [!] a tier-4/5 model is 11 to 19 GB on disk, plus room to pull it. Below"
        echo "                   ~25 GB free it is the pull that fails, long after this line said yes."
      fi
    else
      echo "  Disk free  : ? (unreadable on $DISK_PATH -- unknown, which is not the same as enough)"
    fi
    if [ "$ROLE" = miner ]; then
      echo "  Budget     : ${BUDGET_MB} MB usable for the model (headroom kept for the KV cache)"
      echo "  Tier       : $TIER   candidates: $CANDIDATES"
      echo "  -> model   : $MODEL   (deterministic pick from the machine: one model per card, diversity across machines)"
    elif [ "$ROLE" = judge ]; then
      echo "  -> model   : $MODEL   (the judge model, served on the CPU instance: no mining model is pulled)"
    else
      echo "  -> model   : none"
    fi
    if [ "$ROLE" = judge ]; then
      # Without a card the box judges, and the chain draws it like any present miner, including as the
      # primary of a request: it answers with the judge model, on the same CPU instance.
      echo "  -> role    : judge (CPU, $JUDGE_MODEL) -- no usable GPU"
      echo "               It judges on the CPU (a mixture-of-experts: few active params), and answers the requests"
      echo "               the chain assigns it with that same model, on the same CPU instance. No mining model."
      echo "               Run one such judge on this box; the ${MOE_CPU_MIN_RAM_MB} MB RAM floor (MOE_CPU_MIN_RAM_MB) it"
      echo "               cleared is what holds the weights alongside the OS."
    elif [ "$ROLE" = refused ] || [ "$ROLE" = unknown ]; then
      echo "  -> role    : $( [ "$ROLE" = refused ] && printf 'none (refused)' || printf 'unknown (nothing decided)' )"
      printf '               %s\n' "$ROLE_WHY"
    elif [ "$MOE_CPU" = "1" ]; then
      echo "  -> role    : mine (GPU, $MODEL) + judge (CPU MoE, $JUDGE_MODEL)"
      echo "               MoE: few active params, so it runs on the CPU while the GPU keeps mining."
      echo "               Run one such judge on this box (MOE_COUNT=1); the ${MOE_CPU_MIN_RAM_MB} MB RAM floor it just"
      echo "               cleared is what holds the weights alongside the OS and the mining process."
    else
      echo "  -> role    : mine only (GPU) — this box does not clear the judge gate. Judging runs the network's"
      echo "               judge model, the mixture-of-experts $MOE_CPU_MODEL, on the CPU,"
      echo "               with or without a GPU. It needs >= ${MOE_CPU_MIN_RAM_MB} MB of system RAM (this box: ${RAM_MB:-?} MB)"
      echo "               and that model on this box's judge allow-list. No judge is seated on the GPU:"
      echo "               the kit serves verdicts from its CPU instance, with the model the chain pins."
      [ "${WSL_DISTRO_NAME:-}" ] && echo "               Under WSL 2 this is the RAM of the WSL VM, not the PC's: %UserProfile%\\.wslconfig, [wsl2] memory=."
    fi
    echo
    json
    ;;
esac
