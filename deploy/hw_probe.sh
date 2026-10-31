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
#   bash deploy/hw_probe.sh --judge-survey [--json]        # what this machine can give the judge, measured, and the
#                                                          # mode it gets (resident | on-demand | impossible | unknown):
#                                                          # key=value lines (exit 0; 3 = unknown, 2 = a setting refused),
#                                                          # or one flat JSON object; see "the judge survey" below
#   bash deploy/hw_probe.sh --judge-cpu                    # the judge's share of the cores alone (threads and quota,
#                                                          # key=value lines), for a caller whose survey gave no reading
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

MODE="report"; NODE_ID=""; GPU_UUID=""; NO_DOCKER=0; NO_GPU=0; JA_TAG=""; SURVEY_JSON=0
# Several flags may be combined (`--gpu <uuid> --json <id>`). An <id> follows --json or --model only when
# it does not itself start with `--`: the forms callers already use (`--json`, `--json <id>`,
# `--model <id>`, `--can-judge`, a bare id, no argument) keep their meaning.
while [ $# -gt 0 ]; do
  case "$1" in
    --json|--model)
      # --json after --judge-survey asks for the survey as JSON; it is not the capacity report.
      if [ "$1" = --json ] && [ "$MODE" = judge-survey ]; then SURVEY_JSON=1; shift; continue; fi
      MODE="${1#--}"; shift
      if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then NODE_ID="$1"; shift; fi ;;
    --can-judge) MODE="can-judge"; shift ;;
    --judge-survey) [ "$MODE" = json ] && SURVEY_JSON=1; MODE="judge-survey"; shift ;;
    --judge-cpu) MODE="judge-cpu"; shift ;;
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
if [ "$NO_DOCKER" = 1 ] || [ "$MODE" = "list-gpus" ] || [ "$MODE" = "judge-floor-mb" ] || [ "$MODE" = "role" ] || [ "$MODE" = "judge-allowed" ] || [ "$MODE" = "judge-survey" ] || [ "$MODE" = "judge-cpu" ]; then
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

# ---------------------------------------------------------------- the judge survey (--judge-survey)
# What this machine can give the judge, measured before its mode is chosen. The floor above (MOE_CPU_MIN_RAM_MB)
# decides whether the machine may judge; this survey decides how, above that floor, and never moves the floor:
#   resident    the judge model stays loaded between audits;
#   on-demand   it is loaded for an audit and unloaded after, behind the judge's check before each load;
#   impossible  even at the quietest moment observed, the model would take the file cache the system is using;
#   unknown     MemTotal, MemAvailable, Active(file) or the swap figures could not be read: nothing was decided (exit 3).
# Three forms of one decision. `key=value` lines, one key per line, for a shell: deploy/join.sh and judge.sh read
# them by exact key, since no JSON parser is guaranteed on the host. The same keys in one flat JSON object with
# --json, for a reader that parses. And a readable last line, `human=`. A value that was not read is `?` (null in
# the JSON), never 0; 0 stays a reading (no swap, the judge engine not running). The lines `env.<KEY>=<value>` are
# what deploy/join.sh copies into the kit's .env as they stand. None of this enters the JSON of `--json` alone: that
# one is published to the capacity registry, and the processes of a desktop have no place there.
# The kernel's files are read under two roots a bench can point at a fabricated host (DENDRA_SURVEY_PROC_ROOT,
# DENDRA_SURVEY_SYS_ROOT; the real /proc and /sys otherwise). The kit's directories, the judge model and the
# recorded mode come from the caller (DENDRA_SURVEY_MINER_KIT, DENDRA_SURVEY_NODE_KIT, DENDRA_SURVEY_REPO,
# DENDRA_SURVEY_JUDGE_MODEL, DENDRA_SURVEY_RECORDED_MODE): join.sh pipes this file into bash, so it cannot find them.
SV_PROC="${DENDRA_SURVEY_PROC_ROOT:-/proc}"
SV_SYS="${DENDRA_SURVEY_SYS_ROOT:-/sys}"
SV_MINER_KIT="${DENDRA_SURVEY_MINER_KIT:-}"
SV_NODE_KIT="${DENDRA_SURVEY_NODE_KIT:-}"
SV_REPO="${DENDRA_SURVEY_REPO:-}"
SV_JMODEL="${DENDRA_SURVEY_JUDGE_MODEL:-$MOE_CPU_MODEL}"
SV_RECORDED="${DENDRA_SURVEY_RECORDED_MODE:-}"
# The parallel slots of the judge engine (what the caller writes as OLLAMA_NUM_PARALLEL on ollama-cpu): each slot holds
# its own KV cache. A caller that writes none leaves the engine's own default, one slot.
SV_ENGINE_PARALLEL_DEFAULT=1
SV_PARALLEL_RAW="${DENDRA_SURVEY_NUM_PARALLEL:-$SV_ENGINE_PARALLEL_DEFAULT}"
# How long the survey watches: DENDRA_SURVEY_SAMPLES readings DENDRA_SURVEY_INTERVAL_S apart. Thirty one-second
# readings are three windows of the kernel's shortest memory pressure average (avg10): a chosen observation time, not a
# reserve, and settable. The longer history comes from the counters the kernel keeps (avg300, pswpout, swap in use).
SV_SAMPLES="${DENDRA_SURVEY_SAMPLES:-30}"
SV_INTERVAL="${DENDRA_SURVEY_INTERVAL_S:-1}"
# The share of the physical cores the judge may use: 75 % unless the operator sets DENDRA_JUDGE_CPU_PCT, accepted
# from 75 to 85. These three figures are the project's decision for the judge (it froze desktops when it took every
# core), not a measure; a value outside them is refused rather than clamped, so the operator sees what was refused.
SV_CPU_PCT_DEFAULT=75; SV_CPU_PCT_MIN=75; SV_CPU_PCT_MAX=85
# The calls one audit makes to the judge model, at most: the steps of judge_worker.py::decide_verdict that generate
# (coherence, relevance, same fact, references, multiplicity). It bounds the time a verdict takes on a card machine.
SV_JUDGE_CALLS_PER_AUDIT=5
# Docker refuses a memory limit below 6 MiB ("Minimum memory limit allowed is 6MB"): a room under it writes no limit.
SV_DOCKER_MIN_MEM_MB=6

_sv_is_uint(){ case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }
# _sv_dec <value> -> the value as a decimal number, leading zeros dropped; nothing (exit 1) when it is not made of
# digits only or has more than nine of them. bash reads 075 as octal and 080 not at all, and wraps a number past 2^63:
# a setting is normalised here before any comparison.
_sv_dec(){ case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; [ "${#1}" -le 9 ] || return 1; printf '%d' "$((10#$1))"; }
_sv_refuse(){ echo "[hw] --judge-survey: $*. Nothing was measured, nothing was changed." >&2; exit 2; }
# _sv_gb <MiB> -> GiB with one decimal, or `?` for a value that was not read.
_sv_gb(){ case "${1:-}" in ''|'?') printf '?' ;; *) awk -v m="$1" 'BEGIN { printf "%.1f", m / 1024 }' ;; esac; }
# _sv_clean <text> -> the text on one line, without the two characters a JSON string or a key=value line cannot carry raw.
_sv_clean(){ printf '%s' "$1" | tr -d '"\\' | tr '\n\r\t' '   '; }

# _sv_meminfo -> "MemTotal MemAvailable MemFree Active(file) SwapTotal SwapFree" in MiB, `?` for a field not read.
_sv_meminfo(){
  [ -r "$SV_PROC/meminfo" ] || { echo '? ? ? ? ? ?'; return 0; }
  awk 'BEGIN { n = split("MemTotal MemAvailable MemFree Active(file) SwapTotal SwapFree", k, " ") }
       { f = $1; sub(/:$/, "", f); if ($2 ~ /^[0-9]+$/) v[f] = int($2 / 1024) }
       END { for (i = 1; i <= n; i++) printf "%s%s", (i > 1 ? " " : ""), ((k[i] in v) ? v[k[i]] : "?"); print "" }' "$SV_PROC/meminfo"
}
# _sv_field <file> <first word> -> the second field of the first line that starts with that word, digits only, or `?`.
_sv_field(){
  [ -r "$1" ] || { echo '?'; return 0; }
  awk -v k="$2" '$1 == k && $2 ~ /^[0-9]+$/ { print $2; f = 1; exit } END { if (!f) print "?" }' "$1"
}
# _sv_psi -> "some_avg10 some_avg300 full_avg10 full_avg300" of /proc/pressure/memory, `?` each when not read: a
# kernel without pressure accounting (or booted with psi=0) has no file, and that is never "no pressure".
_sv_psi(){
  [ -r "$SV_PROC/pressure/memory" ] || { echo '? ? ? ?'; return 0; }
  awk '{ for (i = 2; i <= NF; i++) { split($i, a, "="); if (a[2] ~ /^[0-9]+(\.[0-9]+)?$/) v[$1 "_" a[1]] = a[2] } }
       END { split("some_avg10 some_avg300 full_avg10 full_avg300", k, " ")
             for (i = 1; i <= 4; i++) printf "%s%s", (i > 1 ? " " : ""), ((k[i] in v) ? v[k[i]] : "?"); print "" }' "$SV_PROC/pressure/memory"
}
# _sv_swap_kind -> what backs the swap: zram, partition, file (several joined by +), none, or `?`.
_sv_swap_kind(){
  [ -r "$SV_PROC/swaps" ] || { echo '?'; return 0; }
  awk 'NR > 1 && NF >= 2 { t = (index($1, "/dev/zram") == 1) ? "zram" : $2; if (!(t in s)) { s[t] = 1; o = o (o == "" ? "" : "+") t } }
       END { print (o == "" ? "none" : o) }' "$SV_PROC/swaps"
}
# _sv_physical_cores -> distinct (package, core) pairs of the topology, else of /proc/cpuinfo; `?` when neither names
# one. The logical count is not the physical one: on a machine with two threads per core the judge would take every core.
_sv_physical_cores(){
  local n d
  n="$(for d in "$SV_SYS"/devices/system/cpu/cpu[0-9]*/topology; do
         [ -r "$d/physical_package_id" ] && [ -r "$d/core_id" ] && printf '%s:%s\n' "$(cat "$d/physical_package_id")" "$(cat "$d/core_id")"
       done 2>/dev/null | sort -u | awk 'END { print NR }')"
  if ! _sv_is_uint "$n" || [ "$n" = 0 ]; then
    n="$( [ -r "$SV_PROC/cpuinfo" ] && awk -F: '$1 ~ /^physical id/ { p = $2 } $1 ~ /^core id/ { print p ":" $2 }' "$SV_PROC/cpuinfo" | sort -u | awk 'END { print NR }')"
  fi
  if _sv_is_uint "$n" && [ "$n" -gt 0 ]; then echo "$n"; else echo '?'; fi
}
# _sv_logical_cpus -> the processors online (/sys/devices/system/cpu/online, "0-23" or "0,2-5"); `nproc` reads the
# affinity of this process, which is not the machine.
_sv_logical_cpus(){
  local s
  s="$(cat "$SV_SYS/devices/system/cpu/online" 2>/dev/null | tr -d ' \n')"
  case "$s" in ''|*[!0-9,-]*) echo '?'; return 0 ;; esac
  printf '%s' "$s" | awk -F, '{ for (i = 1; i <= NF; i++) { n = split($i, r, "-"); if (n == 2) c += r[2] - r[1] + 1; else if (n == 1 && r[1] != "") c += 1 } } END { print (c > 0) ? c : "?" }'
}
# _sv_mem_to_mib <docker size> -> MiB rounded up, or `?` for a form it does not know (the reading of
# deploy/join.sh::_mem_to_mib, repeated here because this file runs alone).
_sv_mem_to_mib(){
  printf '%s\n' "$1" | awk '{ v = $1
    if (!match(v, /^[0-9]+(\.[0-9]+)?/)) { print "?"; exit }
    n = substr(v, 1, RLENGTH) + 0; u = substr(v, RLENGTH + 1)
    if (u == "B") m = n / 1048576; else if (u == "KiB" || u == "kB" || u == "KB") m = n / 1024
    else if (u == "MiB" || u == "MB") m = n; else if (u == "GiB" || u == "GB") m = n * 1024
    else if (u == "TiB" || u == "TB") m = n * 1048576; else { print "?"; exit }
    printf "%d\n", (m == int(m)) ? m : int(m) + 1 }'
}

# _sv_host_kind -> SV_KIND (wsl | desktop | server | ?) and SV_TARGET (systemctl get-default, or ?). WSL first, by the
# rule of deploy/join.sh::check_prereqs (/proc/version names microsoft): Windows runs beside it, so it counts as a
# desktop and is told apart. Then a graphical session (loginctl), else a server. No loginctl answer: unread.
_sv_host_kind(){
  local ids id t
  SV_KIND='?'
  SV_TARGET="$(systemctl get-default 2>/dev/null | tr -d '\r')"; [ -n "$SV_TARGET" ] || SV_TARGET='?'
  [ -r "$SV_PROC/version" ] || return 0
  if grep -qi microsoft "$SV_PROC/version" 2>/dev/null; then SV_KIND=wsl; return 0; fi
  command -v loginctl >/dev/null 2>&1 || return 0
  ids="$(loginctl list-sessions --no-legend 2>/dev/null)" || return 0
  SV_KIND=server
  for id in $(printf '%s\n' "$ids" | awk 'NF { print $1 }'); do
    t="$(loginctl show-session "$id" -p Type --value 2>/dev/null)"
    case "$t" in x11|wayland) SV_KIND=desktop; return 0 ;; esac
  done
}

# _sv_windows -> the Windows side of a WSL VM, read once by interop (the container has none, so only this survey
# sees it): total and free physical memory, page file in use, and the [wsl2] section of the profile's .wslconfig.
# Only the classes of `Get-CimInstance` are asked: the performance counters are translated with the language of Windows.
SV_WIN_TOTAL='?'; SV_WIN_AVAIL='?'; SV_WIN_PF='?'; SV_WSLC_MEM='?'; SV_WSLC_SWAP='?'; SV_WSLC_CPU='?'
_sv_windows(){
  local mnt ps1 out tk fk pf up drv rest f
  mnt="${DENDRA_WSL_HOST_MOUNT:-/mnt/c}"
  ps1="$mnt/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
  [ -x "$ps1" ] || return 0
  out="$("$ps1" -NoProfile -NonInteractive -Command '$o = Get-CimInstance Win32_OperatingSystem; $p = (Get-CimInstance Win32_PageFileUsage | Measure-Object -Property CurrentUsage -Sum).Sum; Write-Output ("{0}|{1}|{2}|{3}" -f $o.TotalVisibleMemorySize, $o.FreePhysicalMemory, $p, $env:UserProfile)' 2>/dev/null | tr -d '\r' | awk 'NF' | tail -1)"
  IFS='|' read -r tk fk pf up <<< "$out"
  _sv_is_uint "$tk" && SV_WIN_TOTAL=$((tk / 1024))
  _sv_is_uint "$fk" && SV_WIN_AVAIL=$((fk / 1024))
  _sv_is_uint "$pf" && SV_WIN_PF="$pf"
  # The profile's path (a drive letter, a colon, then its folders) -> the same folders under the mount of that drive,
  # which sits next to DENDRA_WSL_HOST_MOUNT.
  case "$up" in [A-Za-z]:*) : ;; *) return 0 ;; esac
  drv="$(printf '%s' "${up%%:*}" | tr 'A-Z' 'a-z')"; rest="$(printf '%s' "${up#*:}" | tr '\\' '/')"
  f="${mnt%/*}/$drv$rest/.wslconfig"
  if [ ! -e "$f" ]; then SV_WSLC_MEM=absent; SV_WSLC_SWAP=absent; SV_WSLC_CPU=absent; return 0; fi
  [ -r "$f" ] || return 0
  SV_WSLC_MEM=default; SV_WSLC_SWAP=default; SV_WSLC_CPU=default
  eval "$(tr -d '\r' < "$f" | awk -F= '
    /^[[:space:]]*\[/ { s = tolower($0); gsub(/[[:space:]]/, "", s); w = (s == "[wsl2]"); next }
    w && NF >= 2 { k = tolower($1); gsub(/[[:space:]]/, "", k); v = $2; gsub(/[^A-Za-z0-9.]/, "", v)
      if (v != "" && k == "memory") print "SV_WSLC_MEM=" v
      if (v != "" && k == "swap") print "SV_WSLC_SWAP=" v
      if (v != "" && k == "processors") print "SV_WSLC_CPU=" v }')"
}

# _sv_dendra -> what this clone's containers hold (deploy/join.sh::measure_slot_footprint reads them the same way):
# SV_JUDGE_MB, the judge engine (ollama-cpu, 0 when it does not run: a reading), SV_DENDRA_MB, the others; and the
# containers the later reads go through. A container is this clone's when its compose working directory is the miner
# kit, a slot of it, or the node kit (the rule of deploy/testnet-miner/judge.sh::node_cid): another clone on the same
# machine is not counted. docker stats printing 0B for a running container is a tool that does not measure: `?`.
SV_JUDGE_MB='?'; SV_DENDRA_MB='?'; SV_DOCKER_WHY=''; SV_JC=''; SV_MC=''; SV_NC=''
_sv_dendra(){
  local ids id line wd svc proj mk nk ours="" c mu mb tot=0
  if [ -z "$SV_MINER_KIT" ]; then SV_DOCKER_WHY="the kit's directory was not given (DENDRA_SURVEY_MINER_KIT)"; return 0; fi
  if ! command -v docker >/dev/null 2>&1; then SV_DOCKER_WHY="docker is not installed"; return 0; fi
  ids="$(docker ps -q --filter label=com.docker.compose.oneoff=False 2>/dev/null)" || { SV_DOCKER_WHY="docker ps did not answer"; return 0; }
  mk="$(cd "$SV_MINER_KIT" 2>/dev/null && pwd -P)"; nk=""; [ -n "$SV_NODE_KIT" ] && nk="$(cd "$SV_NODE_KIT" 2>/dev/null && pwd -P)"
  for id in $ids; do
    line="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project"}}' "$id" 2>/dev/null | tr -d '\r')" \
      || { SV_DOCKER_WHY="docker inspect did not answer for a running container"; return 0; }
    IFS='|' read -r wd svc proj <<< "$line"
    [ -n "$wd" ] || continue
    case "$wd" in
      "$SV_MINER_KIT"|"$SV_MINER_KIT"/gpu/*) : ;;
      "$mk"|"$mk"/gpu/*) [ -n "$mk" ] || continue ;;
      "$SV_NODE_KIT") [ -n "$SV_NODE_KIT" ] || continue; [ "$svc" = node ] && SV_NC="$id" ;;
      "$nk") [ -n "$nk" ] || continue; [ "$svc" = node ] && SV_NC="$id" ;;
      *) continue ;;
    esac
    ours="$ours $id"
    [ "$svc" = ollama-cpu ] && SV_JC="$id"
    # The miner of slot 0 reaches the judge engine by its service name; a slot k's miner (project -g<k>) does not.
    [ "$svc" = miner ] && case "$proj" in *-g[0-9]*) : ;; *) SV_MC="$id" ;; esac
  done
  if [ -z "$ours" ]; then SV_JUDGE_MB=0; SV_DENDRA_MB=0; return 0; fi
  SV_JUDGE_MB=0; SV_DENDRA_MB=0
  # shellcheck disable=SC2086
  line="$(docker stats --no-stream --format '{{.Container}}|{{.MemUsage}}' $ours 2>/dev/null)" \
    || { SV_JUDGE_MB='?'; SV_DENDRA_MB='?'; SV_DOCKER_WHY="docker stats did not answer"; return 0; }
  while IFS='|' read -r c mu; do
    [ -n "$c" ] || continue
    mb="$(_sv_mem_to_mib "${mu%% /*}")"
    if [ "$mb" = '?' ] || [ "$mb" -lt 1 ]; then
      SV_JUDGE_MB='?'; SV_DENDRA_MB='?'
      SV_DOCKER_WHY="docker stats reports ${mu:-nothing} for a running container: it does not measure memory on this host"; return 0
    fi
    if [ -n "$SV_JC" ] && [ "$c" = "$SV_JC" ]; then SV_JUDGE_MB="$mb"; else tot=$((tot + mb)); fi
  done <<< "$line"
  SV_DENDRA_MB="$tot"
}

# _sv_engine -> from the judge engine, through the python3 of this kit's miner container (the engine is reached by
# its service name there; JSON parsed, never searched): SV_MODEL_MB, the size of the judge model on disk (/api/tags),
# SV_RESIDENT, whether it is loaded now (/api/ps: yes | no | ?), and the four figures of its metadata that size its KV
# cache (/api/show, model_info under the model's architecture: block_count, attention.head_count_kv,
# attention.key_length, attention.value_length; `?` for one that is absent or not a single whole number). The engine not
# running is a reading: nothing is loaded, and the size on disk is unread.
SV_MODEL_MB='?'; SV_RESIDENT='?'; SV_KV_BLOCKS='?'; SV_KV_HEADS='?'; SV_KV_KLEN='?'; SV_KV_VLEN='?'
_sv_engine(){
  local out
  if [ "$SV_JUDGE_MB" = 0 ] && [ -z "$SV_JC" ]; then SV_RESIDENT=no; return 0; fi
  [ -n "$SV_JC" ] && [ -n "$SV_MC" ] || return 0
  out="$(docker exec "$SV_MC" python3 -c 'import json, sys, urllib.request
base, want = sys.argv[1].rstrip("/"), sys.argv[2]
alts = {want, want + ":latest"} if ":" not in want else {want}
def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.load(r)
def mine(doc):
    ms = doc.get("models", []) if isinstance(doc, dict) else None
    if not isinstance(ms, list):
        return None
    for m in ms:
        if isinstance(m, dict) and {str(m.get("name") or ""), str(m.get("model") or "")} & alts:
            return m
    return {}
try:
    t = mine(get("/api/tags"))
except Exception:
    t = None
try:
    p = mine(get("/api/ps"))
except Exception:
    p = None
size = t.get("size") if isinstance(t, dict) else None
mb = str(-(-int(size) // 1048576)) if isinstance(size, int) and size > 0 else "?"
st = "?" if p is None else ("yes" if p else "no")
kv = ["?"] * 4
if isinstance(t, dict) and t:
    try:
        body = json.dumps({"model": str(t.get("name") or t.get("model"))}).encode()
        req = urllib.request.Request(base + "/api/show", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            mi = json.load(r).get("model_info")
        arch = mi.get("general.architecture") if isinstance(mi, dict) else None
        if isinstance(arch, str) and arch:
            for i, k in enumerate(("block_count", "attention.head_count_kv", "attention.key_length", "attention.value_length")):
                v = mi.get(arch + "." + k)
                if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 1000000:
                    kv[i] = str(v)
    except Exception:
        pass
print("|".join([mb, st] + kv))' http://ollama-cpu:11434 "$SV_JMODEL" 2>/dev/null | tail -1)"
  case "$out" in *'|'*) : ;; *) return 0 ;; esac
  IFS='|' read -r SV_MODEL_MB SV_RESIDENT SV_KV_BLOCKS SV_KV_HEADS SV_KV_KLEN SV_KV_VLEN <<< "$out"
  _sv_is_uint "$SV_MODEL_MB" || SV_MODEL_MB='?'
  case "$SV_RESIDENT" in yes|no) : ;; *) SV_RESIDENT='?' ;; esac
  for out in SV_KV_BLOCKS SV_KV_HEADS SV_KV_KLEN SV_KV_VLEN; do _sv_is_uint "${!out}" || printf -v "$out" '%s' '?'; done
}

# _sv_context -> SV_CTX, the context the judge engine opens every model with: OLLAMA_CONTEXT_LENGTH of the kit's
# compose file (set on ollama-cpu there, never by a caller), `?` when the file or a whole number in it is not read.
SV_CTX='?'
_sv_context(){
  [ -n "$SV_MINER_KIT" ] && [ -r "$SV_MINER_KIT/docker-compose.yml" ] || return 0
  SV_CTX="$(awk '$1 == "OLLAMA_CONTEXT_LENGTH:" { v = $2; gsub(/"/, "", v); print v; exit }' "$SV_MINER_KIT/docker-compose.yml" | tr -d '\r')"
  _sv_is_uint "$SV_CTX" && [ "$SV_CTX" -gt 0 ] || SV_CTX='?'
}

# _sv_declared -> SV_DECL, the eight figures DECLARED_JUDGE_MODELS of modea/memwatch.py declares for the judge model (a
# line `"<model>": (file, block_count, head_count_kv, key_length, value_length, context, loaded with 1 slot, with 2)`,
# MiB and counts, measured by the engine bench), space-separated; nothing when the model has no line or the file cannot
# be read. The judge's watch reads the same line, so the two count the same figures.
SV_DECL=''
_sv_declared(){
  local f x
  SV_DECL=''
  f="$(_sv_memwatch_file)"
  [ -n "$f" ] || return 0
  SV_DECL="$(awk -v m="$SV_JMODEL" 'BEGIN { q = sprintf("%c", 34) }
    $1 == q m q ":" { s = $0; sub(/^[^(]*[(]/, "", s); sub(/[)].*$/, "", s); gsub(/[ ,]+/, " ", s); sub(/^ /, "", s); sub(/ $/, "", s); print s; exit }' "$f")"
  # shellcheck disable=SC2086
  set -- $SV_DECL
  [ "$#" = 8 ] || { SV_DECL=''; return 0; }
  for x in "$@"; do _sv_is_uint "$x" || { SV_DECL=''; return 0; }; done
  return 0
}

# _sv_memwatch_file -> the judge's reader in the miner container (services/modea/memwatch.py) in the repository
# the caller names (DENDRA_SURVEY_REPO), or nothing when it cannot be read. The survey reads two of its figures there
# rather than keep a second copy: the shortest period of the judge's watch, and the figures declared for the judge model.
_sv_memwatch_file(){
  local f="$SV_REPO/services/modea/memwatch.py"
  [ -n "$SV_REPO" ] && [ -r "$f" ] && printf '%s' "$f"
  return 0
}

# The judge's run-time settings, read in the miner container by modea/memwatch.py::settings, are checked here by the
# same rules or narrower ones, so that a value that reader would refuse stops the installation (exit 2) instead of
# keeping the judge's check before each load closed in the container. A keep-alive is a number of seconds or numbers
# with a unit (h, m, s, ms); the resident one may be negative (kept loaded without limit), the on-demand one is above 0.
# The pressure threshold is above 0 and at most 100, the promotion delay above 0. The watch's period is at least the
# reader's own bound, PERIOD_S_MIN, read in that file (_sv_period_min): a period this file accepted below it would close
# the judge's check in the container for good. A period set while that bound cannot be read is refused, never passed.
SV_NUM_RE='^[0-9]+([.][0-9]+)?$'
SV_DUR_RE='^[+-]?([0-9]+([.][0-9]+)?|([0-9]+([.][0-9]+)?(h|m|s|ms))+)$'
# _sv_period_min -> SV_PERIOD_MIN, PERIOD_S_MIN of modea/memwatch.py (a line `PERIOD_S_MIN = 2.0`), or `?` when that
# file or that line cannot be read.
SV_PERIOD_MIN='?'
_sv_period_min(){
  local f
  SV_PERIOD_MIN='?'
  f="$(_sv_memwatch_file)"
  [ -n "$f" ] || return 0
  SV_PERIOD_MIN="$(awk '$1 == "PERIOD_S_MIN" && $2 == "=" { print $3; exit }' "$f")"
  [[ $SV_PERIOD_MIN =~ $SV_NUM_RE ]] || SV_PERIOD_MIN='?'
  return 0
}
_sv_check_runtime(){
  local v
  v="${DENDRA_JUDGE_KEEPALIVE:-}"
  [ -z "$v" ] || [[ $v =~ $SV_DUR_RE ]] \
    || _sv_refuse "DENDRA_JUDGE_KEEPALIVE=$v is not a duration (seconds, or numbers with h, m, s or ms such as 1h or 1h30m; negative keeps the model loaded)"
  v="${DENDRA_JUDGE_ONDEMAND_KEEPALIVE:-}"
  [ -z "$v" ] || { [[ $v =~ $SV_DUR_RE ]] && [ "${v#-}" = "$v" ] && [ -n "$(printf '%s' "$v" | tr -cd '1-9')" ]; } \
    || _sv_refuse "DENDRA_JUDGE_ONDEMAND_KEEPALIVE=$v is not a duration above 0 (seconds, or numbers with h, m, s or ms such as 600s or 10m)"
  v="${DENDRA_MEMWATCH_PERIOD_S:-}"
  if [ -n "$v" ]; then
    _sv_period_min
    [ "$SV_PERIOD_MIN" != '?' ] \
      || _sv_refuse "DENDRA_MEMWATCH_PERIOD_S=$v cannot be checked: the shortest period the judge's watch takes (PERIOD_S_MIN in services/modea/memwatch.py) could not be read in the repository the caller names (DENDRA_SURVEY_REPO=${SV_REPO:-unset})"
    { [[ $v =~ $SV_NUM_RE ]] && awk -v x="$v" -v m="$SV_PERIOD_MIN" 'BEGIN { exit !(x + 0 >= m + 0) }'; } \
      || _sv_refuse "DENDRA_MEMWATCH_PERIOD_S=$v is not a number of seconds of at least $SV_PERIOD_MIN (PERIOD_S_MIN of the judge's watch, services/modea/memwatch.py)"
  fi
  v="${DENDRA_MEMWATCH_PSI_SOME_PCT:-}"
  [ -z "$v" ] || { [[ $v =~ $SV_NUM_RE ]] && awk -v x="$v" 'BEGIN { exit !(x + 0 > 0 && x + 0 <= 100) }'; } \
    || _sv_refuse "DENDRA_MEMWATCH_PSI_SOME_PCT=$v is not a share above 0 and at most 100"
  v="${DENDRA_MEMWATCH_PROMOTE_AFTER_S:-}"
  [ -z "$v" ] || { [[ $v =~ $SV_NUM_RE ]] && awk -v x="$v" 'BEGIN { exit !(x + 0 > 0) }'; } \
    || _sv_refuse "DENDRA_MEMWATCH_PROMOTE_AFTER_S=$v is not a number of seconds above 0"
  return 0
}

# _sv_cpu_pct -> SV_PCT, the operator's share of the physical cores (DENDRA_JUDGE_CPU_PCT, read in base 10), or a
# refusal (exit 2) outside the bounds above.
SV_PCT=''
_sv_cpu_pct(){
  local raw="${DENDRA_JUDGE_CPU_PCT:-$SV_CPU_PCT_DEFAULT}" cpu_pct
  cpu_pct="$(_sv_dec "$raw")" || _sv_refuse "DENDRA_JUDGE_CPU_PCT=$raw is not a whole number (% of the physical cores the judge may use, ${SV_CPU_PCT_MIN} to ${SV_CPU_PCT_MAX}; empty is ${SV_CPU_PCT_DEFAULT})"
  [ "$cpu_pct" -ge "$SV_CPU_PCT_MIN" ] && [ "$SV_CPU_PCT_MAX" -ge "$cpu_pct" ] \
    || _sv_refuse "DENDRA_JUDGE_CPU_PCT=$raw is outside ${SV_CPU_PCT_MIN} to ${SV_CPU_PCT_MAX} (% of the physical cores the judge may use; empty is ${SV_CPU_PCT_DEFAULT})"
  SV_PCT="$cpu_pct"
}
# _sv_cpu_share <share> -> SV_PHYS, SV_LOG, and the judge's threads and quota: max(1, floor(physical cores x share /
# 100)); without a physical count the quota falls back to the logical processors and no thread count is written (the
# engine then detects, and only the quota bounds).
SV_PHYS='?'; SV_LOG='?'; SV_NTHR='?'; SV_CPUS='?'
_sv_cpu_share(){
  SV_PHYS="$(_sv_physical_cores)"; SV_LOG="$(_sv_logical_cpus)"; SV_NTHR='?'; SV_CPUS='?'
  if _sv_is_uint "$SV_PHYS"; then SV_NTHR=$(( SV_PHYS * $1 / 100 )); [ "$SV_NTHR" -ge 1 ] || SV_NTHR=1; SV_CPUS="$SV_NTHR"
  elif _sv_is_uint "$SV_LOG"; then SV_CPUS=$(( SV_LOG * $1 / 100 )); [ "$SV_CPUS" -ge 1 ] || SV_CPUS=1; fi
}
# judge_cpu (--judge-cpu) -> the survey's lines of the judge's share of the cores, and nothing else: deploy/join.sh asks
# them when the survey gave no reading, since the judge's CPU limits do not depend on the memory measure.
judge_cpu(){
  _sv_cpu_pct
  _sv_cpu_share "$SV_PCT"
  printf '%s=%s\n' cpu_pct "$SV_PCT" cpu_pct_min "$SV_CPU_PCT_MIN" cpu_pct_max "$SV_CPU_PCT_MAX" physical_cores "$SV_PHYS" logical_cpus "$SV_LOG" judge_num_thread "$SV_NTHR" judge_cpus "$SV_CPUS"
  printf 'env.DENDRA_JUDGE_NUM_THREAD=%s\n' "$( _sv_is_uint "$SV_NTHR" && printf '%s' "$SV_NTHR")"
  exit 0
}

# _sv_disk -> SV_DISK_MB_S: one GiB of the judge's largest blob read straight from the disk (iflag=direct, around the
# page cache) inside the judge engine, the real path of the weights (a Docker volume; under WSL, the VM's disk). Only
# when the model is there: a first installation writes no test file on the operator's disk, and the first load
# measures the real time instead (the engine's load_duration).
SV_DISK_MB_S='?'
_sv_disk(){
  local out
  [ -n "$SV_JC" ] && _sv_is_uint "$SV_MODEL_MB" || return 0
  out="$(docker exec "$SV_JC" sh -c 'f=$(ls -S /root/.ollama/models/blobs 2>/dev/null | head -1); [ -n "$f" ] || exit 1; LC_ALL=C dd if=/root/.ollama/models/blobs/$f of=/dev/null bs=4M count=256 iflag=direct 2>&1 | tail -1' 2>/dev/null)" || return 0
  SV_DISK_MB_S="$(printf '%s\n' "$out" | awk '$2 == "bytes" && $1 ~ /^[0-9]+$/ { for (i = 3; i < NF; i++) if ($(i + 1) == "s," && $i ~ /^[0-9]+(\.[0-9]+)?$/ && $i > 0) { printf "%d\n", $1 / 1048576 / $i; f = 1; exit } } END { if (!f) print "?" }')"
  _sv_is_uint "$SV_DISK_MB_S" && [ "$SV_DISK_MB_S" -gt 0 ] || SV_DISK_MB_S='?'
}

# _sv_src_default <file under the repository> <name> -> the default a reader of the container applies, read in its
# source (a line `name = 120` or `name = 120.0`), so that no second copy of it lives here; `?` when it cannot be read.
_sv_src_default(){
  local v
  [ -n "$SV_REPO" ] && [ -r "$SV_REPO/$1" ] || { echo '?'; return 0; }
  v="$(awk -v k="$2" '$1 == k && $2 == "=" { v = $3; sub(/\.0+$/, "", v); print v; exit }' "$SV_REPO/$1")"
  if _sv_is_uint "$v"; then echo "$v"; else echo '?'; fi
}

# _sv_audit_window -> SV_WINDOW_S, the time a verdict has. Machine with a card: audit_resolve_timeout blocks (jobs
# params, read by this machine's node; proto3 omits it at 0, and 0 is no deadline: `none`) at the block interval
# measured over the last 100 blocks, which varies. Machine without a card: the bound of a presence probe
# (DENDRA_PRESENCE_PROBE_S, else the daemon's default), since an unloaded model must come back within it.
SV_WINDOW_S='?'; SV_JUDGE_TIME_S='?'
_sv_audit_window(){
  local params out t
  if [ "$VRAM_MB" -le 0 ]; then
    t="${DENDRA_PRESENCE_PROBE_S:-}"; t="${t%%.*}"
    _sv_is_uint "$t" || t="$(_sv_src_default services/miner.py PRESENCE_PROBE_S_DEFAULT)"
    SV_WINDOW_S="$t"; return 0
  fi
  t="${DENDRA_JUDGE_TIMEOUT_S:-}"; t="${t%%.*}"
  _sv_is_uint "$t" || t="$(_sv_src_default services/modea/judge.py JUDGE_TIMEOUT_DEFAULT_S)"
  _sv_is_uint "$t" && SV_JUDGE_TIME_S=$((t * SV_JUDGE_CALLS_PER_AUDIT))
  [ -n "$SV_NC" ] && [ -n "$SV_MC" ] || return 0
  params="$(docker exec "$SV_NC" dendrad query jobs params --output json 2>/dev/null)" || return 0
  out="$(printf '%s' "$params" | docker exec -i "$SV_MC" python3 -c 'import datetime, json, os, sys, urllib.request
try:
    d = json.load(sys.stdin)
    p = d.get("params", d)
    t = int(p.get("audit_resolve_timeout") or 0)
except Exception:
    print("?"); raise SystemExit
if t == 0:
    print("none"); raise SystemExit
base = os.environ.get("DENDRA_NODE", "").replace("tcp://", "http://").rstrip("/")
def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.load(r)
def at(h):
    s = get("/block?height=%d" % h)["result"]["block"]["header"]["time"]
    main, _, frac = s.rstrip("Z").partition(".")
    return datetime.datetime.fromisoformat(main + "." + (frac + "000000")[:6] + "+00:00").timestamp()
try:
    h = int(get("/status")["result"]["sync_info"]["latest_block_height"])
    if h <= 100:
        raise ValueError
    step = (at(h) - at(h - 100)) / 100
    if step <= 0:
        raise ValueError
    print(int(t * step))
except Exception:
    print("?")' 2>/dev/null | tail -1)"
  case "$out" in none) SV_WINDOW_S=none ;; *) _sv_is_uint "$out" && SV_WINDOW_S="$out" ;; esac
}

judge_survey(){
  local i m a amin="" amax="" wmax="" free_now total="" stot="" sfree="" unread="" p0 p1 pin
  local need e src j h q b quiet busy cpu_pct nthr cpus lim swl why mode envmode kv par capw dkv dfile buf
  # The operator's settings are checked before anything is measured: a refused value changes nothing. Each is read in
  # base 10 (_sv_dec).
  SV_SAMPLES="$(_sv_dec "$SV_SAMPLES")" && [ "$SV_SAMPLES" -ge 1 ] || _sv_refuse "DENDRA_SURVEY_SAMPLES=${DENDRA_SURVEY_SAMPLES:-} is not a count of readings (1 or more)"
  SV_INTERVAL="$(_sv_dec "$SV_INTERVAL")" || _sv_refuse "DENDRA_SURVEY_INTERVAL_S=${DENDRA_SURVEY_INTERVAL_S:-} is not a whole number of seconds"
  _sv_cpu_pct; cpu_pct="$SV_PCT"
  # DENDRA_JUDGE_HEADROOM_MB: room the operator adds on top of the judge's footprint. Absent, nothing is added: the
  # zero value of the setting, not a figure chosen here. At most this machine's memory (MemTotal): more is no headroom
  # any machine has, and a sum past it would only be read as a mode nobody measured.
  h="$(_sv_dec "${DENDRA_JUDGE_HEADROOM_MB:-0}")" || _sv_refuse "DENDRA_JUDGE_HEADROOM_MB=${DENDRA_JUDGE_HEADROOM_MB:-} is not a whole number of MiB (nine digits at most)"
  # shellcheck disable=SC2046
  set -- $(_sv_meminfo)
  ! _sv_is_uint "$1" || [ "$h" -le "$1" ] || _sv_refuse "DENDRA_JUDGE_HEADROOM_MB=${DENDRA_JUDGE_HEADROOM_MB:-} is more than the memory of this machine (MemTotal $1 MiB)"
  par="$(_sv_dec "$SV_PARALLEL_RAW")" && [ "$par" -ge 1 ] || _sv_refuse "DENDRA_SURVEY_NUM_PARALLEL=$SV_PARALLEL_RAW is not a count of the judge engine's slots (1 or more)"
  # DENDRA_JUDGE_MODE_CHOICE may only lower the mode (resident to on-demand), never raise it, as the environment may
  # only narrow the judge allow-list above. `resident` is accepted and changes nothing: the measure still decides.
  case "${DENDRA_JUDGE_MODE_CHOICE:-}" in ''|on-demand|resident) : ;;
    *) _sv_refuse "DENDRA_JUDGE_MODE_CHOICE=${DENDRA_JUDGE_MODE_CHOICE} is neither on-demand nor resident (empty: the measure decides)" ;; esac
  _sv_check_runtime

  _sv_host_kind
  [ "$SV_KIND" = wsl ] && _sv_windows
  # The samples. Under WSL each MemAvailable is bounded by what Windows can still give the VM: the VM does not always
  # hand its free pages back, and the ones it handed back return only while Windows still has them.
  p0="$(_sv_field "$SV_PROC/vmstat" pswpout)"
  i=0
  while [ "$i" -lt "$SV_SAMPLES" ]; do
    # shellcheck disable=SC2046
    set -- $(_sv_meminfo)
    total="$1"; a="$2"; free_now="$3"; m="$4"; stot="$5"; sfree="$6"
    if _sv_is_uint "$a" && [ "$SV_KIND" = wsl ] && _sv_is_uint "$SV_WIN_AVAIL" && _sv_is_uint "$free_now"; then
      pin=$(( free_now < SV_WIN_AVAIL ? free_now : SV_WIN_AVAIL ))
      [ $((a - free_now + pin)) -lt "$a" ] && a=$((a - free_now + pin))
    fi
    if _sv_is_uint "$a"; then
      { [ -z "$amin" ] || [ "$a" -lt "$amin" ]; } && amin="$a"
      { [ -z "$amax" ] || [ "$a" -gt "$amax" ]; } && amax="$a"
    else unread="$unread MemAvailable"; fi
    if _sv_is_uint "$m"; then { [ -z "$wmax" ] || [ "$m" -gt "$wmax" ]; } && wmax="$m"; else unread="$unread Active(file)"; fi
    # Under WSL, MemFree bounds the room above: without it the figure would claim what Windows may not give back.
    [ "$SV_KIND" = wsl ] && ! _sv_is_uint "$free_now" && unread="$unread MemFree"
    i=$((i + 1))
    [ "$i" -lt "$SV_SAMPLES" ] && sleep "$SV_INTERVAL"
  done
  p1="$(_sv_field "$SV_PROC/vmstat" pswpout)"
  _sv_is_uint "$total" || unread="$unread MemTotal"
  { _sv_is_uint "$stot" && _sv_is_uint "$sfree"; } || unread="$unread SwapTotal/SwapFree"
  # One unread sample makes the figure unread: the extremes of a series with a hole in it are not the extremes.
  case "$unread" in *MemAvailable*) amin='?'; amax='?' ;; esac
  case "$unread" in *Active*) wmax='?' ;; esac
  [ -n "$amin" ] || { amin='?'; amax='?'; }
  [ -n "$wmax" ] || wmax='?'

  declare -gA SV=()
  SV[survey_schema]=1
  SV[host_kind]="$SV_KIND"; SV[systemd_default_target]="$(_sv_clean "$SV_TARGET")"
  SV[mem_total_mb]="${total:-?}"; _sv_is_uint "${SV[mem_total_mb]}" || SV[mem_total_mb]='?'
  SV[mem_available_min_mb]="$amin"; SV[mem_available_max_mb]="$amax"; SV[active_file_mb]="$wmax"
  if _sv_is_uint "$stot" && _sv_is_uint "$sfree"; then SV[swap_total_mb]="$stot"; SV[swap_used_mb]=$((stot - sfree)); else SV[swap_total_mb]='?'; SV[swap_used_mb]='?'; fi
  SV[swap_kind]="$(_sv_swap_kind)"
  SV[swap_out_pages_since_boot]="$p1"
  SV[swap_out_pages_window]='?'; _sv_is_uint "$p0" && _sv_is_uint "$p1" && SV[swap_out_pages_window]=$((p1 - p0))
  SV[uptime_s]="$( [ -r "$SV_PROC/uptime" ] && awk '$1 ~ /^[0-9]/ { print int($1); f = 1 } END { if (!f) print "?" }' "$SV_PROC/uptime" || echo '?')"
  # shellcheck disable=SC2046
  set -- $(_sv_psi)
  SV[psi_some_avg10]="$1"; SV[psi_some_avg300]="$2"; SV[psi_full_avg10]="$3"; SV[psi_full_avg300]="$4"
  SV[swappiness]="$(cat "$SV_PROC/sys/vm/swappiness" 2>/dev/null | tr -d ' \n')"; _sv_is_uint "${SV[swappiness]}" || SV[swappiness]='?'
  m="$(ps -eo rss=,comm= --sort=-rss 2>/dev/null | head -5)" || m=""
  SV[top_processes]="$( [ -n "$m" ] && printf '%s\n' "$m" | awk '$1 ~ /^[0-9]+$/ { c = $2; gsub(/[^A-Za-z0-9._-]/, "_", c); printf "%s%s:%d", (n++ ? "," : ""), c, int($1 / 1024) } END { if (!n) printf "?" }' || printf '?')"
  _sv_dendra
  SV[dendra_containers_mb]="$SV_DENDRA_MB"; SV[judge_engine_mb]="$SV_JUDGE_MB"
  _sv_engine
  SV[judge_resident]="$SV_RESIDENT"; SV[model_file_mb]="$SV_MODEL_MB"
  _sv_context
  _sv_cpu_share "$cpu_pct"
  SV[physical_cores]="$SV_PHYS"; SV[logical_cpus]="$SV_LOG"
  m="$(docker info --format '{{.MemoryLimit}}|{{.SwapLimit}}|{{.CPUCfsQuota}}|{{.CPUShares}}|{{.CgroupVersion}}' 2>/dev/null | tr -d '\r' | tail -1)" || m=""
  IFS='|' read -r a b q i j <<< "$m"
  for e in a b q i; do case "${!e}" in true|false) : ;; *) printf -v "$e" '%s' '?' ;; esac; done
  _sv_is_uint "$j" || j='?'
  SV[docker_memory_limit]="$a"; SV[docker_swap_limit]="$b"; SV[docker_cpu_quota]="$q"; SV[docker_cpu_shares]="$i"; SV[cgroup_version]="$j"
  _sv_disk
  SV[disk_read_mb_s]="$SV_DISK_MB_S"
  SV[load_estimate_s]='?'
  _sv_is_uint "$SV_MODEL_MB" && _sv_is_uint "$SV_DISK_MB_S" && SV[load_estimate_s]=$(( (SV_MODEL_MB + SV_DISK_MB_S - 1) / SV_DISK_MB_S ))
  SV[windows_total_mb]="$SV_WIN_TOTAL"; SV[windows_available_mb]="$SV_WIN_AVAIL"; SV[windows_pagefile_used_mb]="$SV_WIN_PF"
  SV[wslconfig_memory]="$SV_WSLC_MEM"; SV[wslconfig_swap]="$SV_WSLC_SWAP"; SV[wslconfig_processors]="$SV_WSLC_CPU"

  # E, the judge's footprint in memory, by preference:
  #   resident  what the engine holds now, when the model is loaded (measured on this machine);
  #   file+kv   the size of the model file the engine lists, plus its KV cache: block_count x head_count_kv x
  #             (key_length + value_length) of its metadata, times the context of the judge engine (SV_CTX), times its
  #             parallel slots, times 2 bytes per value (an f16 cache), rounded up to the MiB. modea/memwatch.py
  #             computes the same formula from the same metadata;
  #   declared  the same sum made from the figures DECLARED_JUDGE_MODELS of modea/memwatch.py declares for that model
  #             (its file and the metadata that size its cache, measured by the engine bench), when the engine gave
  #             neither (a first installation runs before the pull, and judge.sh off stops the engine); an estimate of
  #             the same kind as file+kv, never called a measure. A model without a declared line has none, and a file
  #             the engine lists at another size than the declared one is another file.
  # After a load, the judge's check before each load measures the footprint again and that measure takes precedence.
  # The engine's own buffers come on top of an estimate: the loaded size the engine bench measured less the declared file
  # and its cache, known only for the declared context and one or two slots (`?` elsewhere).
  e='?'; src='?'; kv='?'; dkv='?'; dfile=''; buf='?'
  if _sv_is_uint "$SV_KV_BLOCKS" && _sv_is_uint "$SV_KV_HEADS" && _sv_is_uint "$SV_KV_KLEN" && _sv_is_uint "$SV_KV_VLEN" && _sv_is_uint "$SV_CTX"; then
    kv=$(( (SV_KV_BLOCKS * SV_KV_HEADS * (SV_KV_KLEN + SV_KV_VLEN) * SV_CTX * par * 2 + 1048575) / 1048576 ))
  fi
  _sv_declared
  if [ -n "$SV_DECL" ] && _sv_is_uint "$SV_CTX"; then
    # shellcheck disable=SC2086
    set -- $SV_DECL
    dfile="$1"; dkv=$(( ($2 * $3 * ($4 + $5) * SV_CTX * par * 2 + 1048575) / 1048576 ))
    if [ "$SV_CTX" = "$6" ]; then
      case "$par" in 1) buf=$(( $7 - $1 - dkv )) ;; 2) buf=$(( $8 - $1 - dkv )) ;; esac
    fi
  fi
  SV[judge_context]="$SV_CTX"; SV[judge_num_parallel]="$par"; SV[judge_kv_mb]="$kv"; SV[headroom_mb]="$h"
  if [ "$SV_RESIDENT" = yes ] && _sv_is_uint "$SV_JUDGE_MB" && [ "$SV_JUDGE_MB" -gt 0 ]; then e="$SV_JUDGE_MB"; src=resident
  elif _sv_is_uint "$SV_MODEL_MB" && _sv_is_uint "$kv"; then e=$(( SV_MODEL_MB + kv )); src=file+kv
  elif _sv_is_uint "$dkv" && { ! _sv_is_uint "$SV_MODEL_MB" || [ "$SV_MODEL_MB" = "$dfile" ]; }; then e=$(( dfile + dkv )); src=declared
  fi
  # The buffers belong to the declared file: an estimate of another file, or a measured footprint, takes none.
  case "$src" in file+kv) [ "$SV_MODEL_MB" = "$dfile" ] || buf='?' ;; declared) : ;; *) buf='?' ;; esac
  _sv_is_uint "$buf" || buf='?'
  SV[judge_footprint_mb]="$e"; SV[judge_footprint_source]="$src"; SV[judge_buffers_mb]="$buf"

  # Threads and CPU quota (_sv_cpu_share above).
  SV[cpu_pct]="$cpu_pct"; SV[cpu_pct_min]="$SV_CPU_PCT_MIN"; SV[cpu_pct_max]="$SV_CPU_PCT_MAX"; nthr="$SV_NTHR"; cpus="$SV_CPUS"
  SV[judge_num_thread]="$nthr"; SV[judge_cpus]="$cpus"

  # The rooms (MiB): what the judge can hold without taking the file cache the system uses now.
  #   quiet = MemAvailable max + judge engine - Active(file)                 (the quietest moment observed)
  #   busy  = MemAvailable min + judge engine - Active(file) - swap in use   (the busiest moment, less the shortage
  #                                                                           the swap already attests)
  # MemAvailable already leaves out what the containers hold: Dendra is never taken out twice. The judge engine is added
  # back because it is the judge itself that holds that place.
  j="$SV_JUDGE_MB"; quiet='?'; busy='?'
  if [ -z "$unread" ] && _sv_is_uint "$j"; then
    quiet=$(( amax + j - wmax )); busy=$(( amin + j - wmax - ${SV[swap_used_mb]} ))
  fi
  SV[room_quiet_mb]="$quiet"; SV[room_busy_mb]="$busy"

  why=""
  if [ -n "$unread" ]; then
    mode=unknown
    why="$(printf '%s' "$unread" | awk '{ for (i = 1; i <= NF; i++) if (!s[$i]++) o = o (o == "" ? "" : ", ") $i } END { print o }') could not be read in $SV_PROC/meminfo: nothing was decided, and an unread measure is not a pass"
  elif ! _sv_is_uint "$j"; then
    case "$SV_RECORDED" in
      resident|on-demand) mode="$SV_RECORDED"; why="the judge engine's share could not be told apart ($SV_DOCKER_WHY): the recorded mode is kept, not lowered on a missing figure" ;;
      *) mode=on-demand; why="the judge engine's share could not be told apart ($SV_DOCKER_WHY): on-demand, and the judge's check before each load decides" ;;
    esac
  elif ! _sv_is_uint "$e"; then
    mode=on-demand; why="the judge's footprint is not known (no figure for $SV_JMODEL): on-demand, and the judge's check before each load measures it"
  else
    need=$(( e + h ))
    if [ "$quiet" -lt "$need" ]; then
      mode=impossible
      why="at the quietest moment observed, MemAvailable ${amax} + judge engine ${j} - file cache ${wmax} = ${quiet} MiB, below the judge's ${e} MiB (${src}) + headroom ${h} MiB"
    elif [ "$busy" -ge "$need" ] && { [ "$SV_KIND" != wsl ] || _sv_is_uint "$SV_WIN_AVAIL"; } && [ "${DENDRA_JUDGE_MODE_CHOICE:-}" != on-demand ]; then
      mode=resident
      why="at the busiest moment observed, MemAvailable ${amin} + judge engine ${j} - file cache ${wmax} - swap in use ${SV[swap_used_mb]} = ${busy} MiB, at or above the judge's ${e} MiB (${src}) + headroom ${h} MiB"
    else
      mode=on-demand
      if [ "$busy" -lt "$need" ]; then
        why="at the busiest moment observed, MemAvailable ${amin} + judge engine ${j} - file cache ${wmax} - swap in use ${SV[swap_used_mb]} = ${busy} MiB, below the judge's ${e} MiB (${src}) + headroom ${h} MiB; at the quietest, ${quiet} MiB"
      elif [ "$SV_KIND" = wsl ] && ! _sv_is_uint "$SV_WIN_AVAIL"; then
        why="the Windows side of this WSL VM could not be read (interop): what Windows can still give the VM is not known, so the model is not kept loaded"
      else
        why="DENDRA_JUDGE_MODE_CHOICE=on-demand: the operator's choice (the measure would allow resident, ${busy} MiB at the busiest moment)"
      fi
      # An unloaded model must come back in time. Measured disk and known window: a load that cannot fit, with no room
      # to stay loaded either, is impossible. Not established: said, and the first load measures it.
      _sv_audit_window
      b=""
      if _sv_is_uint "${SV[load_estimate_s]}"; then
        if [ "$VRAM_MB" -le 0 ]; then b="${SV[load_estimate_s]}"
        elif _sv_is_uint "$SV_JUDGE_TIME_S"; then b=$(( ${SV[load_estimate_s]} + SV_JUDGE_TIME_S )); fi
      fi
      if [ -n "$b" ] && _sv_is_uint "$SV_WINDOW_S"; then
        if [ "$b" -ge "$SV_WINDOW_S" ] && [ "$busy" -lt "$need" ]; then
          mode=impossible; why="$why; a load takes about ${SV[load_estimate_s]} s, and with the judging it does not fit the ${SV_WINDOW_S} s a verdict has: the model can neither stay loaded nor come back in time"
        fi
      elif ! _sv_is_uint "${SV[load_estimate_s]}"; then
        why="$why; load time not established (the model is not on this disk yet, or its read was not measured)"
      elif [ "$SV_WINDOW_S" != none ]; then
        why="$why; whether a load fits the time a verdict has was not established (that time could not be read)"
      fi
    fi
  fi
  SV[audit_window_s]="$SV_WINDOW_S"; SV[judge_time_s]="$SV_JUDGE_TIME_S"
  # The memory limit of the judge engine is the quiet room: the most the machine can give the judge without its file
  # cache, at the quietest moment, the line past which only the judge stops. It is written only where that room is known
  # to hold what the model takes once loaded, plus the headroom: a footprint measured on this machine (resident), or an
  # estimate (file+kv, declared) plus the engine's own buffers measured for that model, context and slots (judge_buffers_mb
  # above). Those buffers were measured on one machine with the engine the kit pins; that they are the same on this one is
  # an inference (they depend on the engine, the model, the context and the slots, not on the host). Elsewhere no limit is
  # written, `none`: a limit under what the model takes stops every load the judge's check lets through, and a limit under
  # the footprint keeps that check closed for good. Without one, the check decides on fresh readings, and the kernel stops
  # the judge first when the whole machine runs short (oom_score_adj 1000 in the override). No swap for the container when
  # the kernel accounts it per container.
  lim='?'
  case "$quiet" in ''|'?') : ;;
    *) lim=none
       if _sv_is_uint "$e" && [ "$quiet" -ge "$SV_DOCKER_MIN_MEM_MB" ]; then
         if [ "$src" = resident ] && [ "$quiet" -ge $(( e + h )) ]; then lim="$quiet"
         elif [ "$buf" != '?' ] && [ "$quiet" -ge $(( e + buf + h )) ]; then lim="$quiet"; fi
       fi ;;
  esac
  case "${SV[docker_swap_limit]}" in true) swl=yes ;; false) swl=no ;; *) swl='?' ;; esac
  SV[judge_mem_limit_mb]="$lim"; SV[judge_mem_swap_limit]="$swl"
  SV[judge_mode]="$mode"; SV[judge_mode_why]="$(_sv_clean "$why")"
  # The ceiling the kit writes: resident only when measured so; on-demand otherwise (impossible keeps a seat, if one is
  # kept, on-demand, with no memory limit: the check before each load then abstains while the room is not there, and
  # opens when it comes back).
  envmode=on-demand; [ "$mode" = resident ] && envmode=resident
  SV[env.DENDRA_JUDGE_MODE]="$envmode"
  SV[env.DENDRA_JUDGE_HOST_KIND]="$( [ "$SV_KIND" = '?' ] || printf '%s' "$SV_KIND")"
  SV[env.DENDRA_JUDGE_NUM_THREAD]="$( _sv_is_uint "$nthr" && printf '%s' "$nthr")"
  SV[env.DENDRA_JUDGE_MEM_LIMIT_MB]="$( _sv_is_uint "$lim" && printf '%s' "$lim")"
  local gloss="nothing decided"
  case "$mode" in resident) gloss="kept loaded between audits" ;; on-demand) gloss="loaded for audits only" ;;
    impossible) gloss="no room for the model even at the quietest moment observed" ;; esac
  local used=""
  if _sv_is_uint "${SV[mem_total_mb]}" && _sv_is_uint "$amax" && _sv_is_uint "$amin"; then
    used="$(_sv_gb $(( SV[mem_total_mb] - amax ))) to $(_sv_gb $(( SV[mem_total_mb] - amin ))) GB"
  else used="? GB"; fi
  local kindw="$SV_KIND"; [ "$kindw" = '?' ] && kindw="a host of unread kind"
  case "$lim" in none) capw="no memory cap (the room is not known to hold the loaded model: the judge's check before each load decides)" ;;
    *) capw="memory capped at $(_sv_gb "$lim") GB" ;; esac
  m="[hw] judge survey: ${kindw}, $(_sv_gb "${SV[mem_total_mb]}") GB of RAM; the system and its programs use ${used}, $(_sv_gb "${SV[swap_used_mb]}") GB sit in swap, $(_sv_gb "$wmax") GB of file cache is in use; the judge model takes about $(_sv_gb "$e") GB (${src}). Mode: ${mode} (${gloss}). ${cpus} of ${SV[physical_cores]} physical cores, ${capw}, lowest CPU priority."
  [ "$SV_KIND" = wsl ] && m="$m Windows: $(_sv_gb "$SV_WIN_AVAIL") of $(_sv_gb "$SV_WIN_TOTAL") GB free; this VM was given $(_sv_gb "${SV[mem_total_mb]}") GB (.wslconfig memory: $SV_WSLC_MEM; absent means half of the RAM)."
  SV[human]="$(_sv_clean "$m")"

  local order="survey_schema host_kind systemd_default_target mem_total_mb mem_available_min_mb mem_available_max_mb active_file_mb swap_total_mb swap_used_mb swap_kind swap_out_pages_since_boot swap_out_pages_window uptime_s psi_some_avg10 psi_some_avg300 psi_full_avg10 psi_full_avg300 swappiness top_processes dendra_containers_mb judge_engine_mb judge_resident physical_cores logical_cpus docker_memory_limit docker_swap_limit docker_cpu_quota docker_cpu_shares cgroup_version disk_read_mb_s model_file_mb judge_context judge_num_parallel judge_kv_mb judge_buffers_mb windows_total_mb windows_available_mb windows_pagefile_used_mb wslconfig_memory wslconfig_swap wslconfig_processors judge_footprint_mb judge_footprint_source headroom_mb room_quiet_mb room_busy_mb audit_window_s judge_time_s load_estimate_s cpu_pct cpu_pct_min cpu_pct_max judge_num_thread judge_cpus judge_mem_limit_mb judge_mem_swap_limit judge_mode judge_mode_why env.DENDRA_JUDGE_MODE env.DENDRA_JUDGE_HOST_KIND env.DENDRA_JUDGE_NUM_THREAD env.DENDRA_JUDGE_MEM_LIMIT_MB human"
  local k v n=0
  if [ "$SURVEY_JSON" = 1 ]; then
    printf '{'
    for k in $order; do
      v="${SV[$k]}"
      [ "$n" = 0 ] || printf ','; n=1
      case "$v" in
        '?') printf '"%s":null' "$k" ;;
        *) if [[ "$v" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && [[ ! "$v" =~ ^-?0[0-9] ]]; then printf '"%s":%s' "$k" "$v"; else printf '"%s":"%s"' "$k" "$v"; fi ;;
      esac
    done
    printf '}\n'
  else
    for k in $order; do printf '%s=%s\n' "$k" "${SV[$k]}"; done
  fi
  [ "$mode" = unknown ] && exit 3
  exit 0
}

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
  judge-survey) judge_survey ;;
  judge-cpu) judge_cpu ;;
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
