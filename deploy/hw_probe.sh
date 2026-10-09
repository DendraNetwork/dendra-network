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
#   bash deploy/hw_probe.sh --can-judge     # one word: true | false | unknown
#   bash deploy/hw_probe.sh --list-gpus     # one line per NVIDIA card, for a shell (see below)
#   bash deploy/hw_probe.sh --gpu GPU-<uuid> --json <id>   # the report of ONE card (one identity per card)
#   bash deploy/hw_probe.sh --judge-floor-mb               # the judge's RAM floor, after its hard clamp
#   bash deploy/hw_probe.sh --role [--no-gpu]              # the ROLE of this machine on the testnet (below)
#   bash deploy/hw_probe.sh --judge-allowed <model>        # one word: true | false -- is <model> on the judge
#                                                          # allow-list below (the hard list, narrowed by
#                                                          # DENDRA_JUDGE_ALLOWLIST)? A reading of this file's list,
#                                                          # asked by deploy/join.sh of a DENDRA_JUDGE_MODEL
#
# --role IS THE ONE DECISION every installer applies (deploy/join.sh, deploy/install.sh and through it
# deploy/install.ps1 and the HiveOS package, docker/cloud-start.sh). Line 1 is one word, the lines after it say
# why, in words an operator can act on:
#   miner    a usable NVIDIA card: the machine mines on the card, as it always did (exit 0)
#   judge    no usable card and at least MOE_CPU_MIN_RAM_MB of system RAM: the machine JUDGES on the CPU, and
#            answers the requests the chain assigns it with that same judge model, on the kit's CPU instance.
#            No mining model is pulled (exit 0)
#   refused  no usable card, and the RAM below MOE_CPU_MIN_RAM_MB (or the judge model off this box's allow-list):
#            the testnet runs no mining model on the CPU, so this machine has no role (exit 0: a reading)
#   unknown  no usable card, and the system RAM could not be read: nothing was decided, and an unread gate is
#            never a pass (exit 3)
# --no-gpu decides as if this host had no usable card: the caller MEASURED that its engine cannot reach the card
# the host lists (deploy/join.sh: Docker without the NVIDIA container toolkit).
#
# --can-judge answers ONE question, for a caller that has to decide before anything is installed
# (deploy/install.sh sizes its disk floor on it): does THIS box clear the judge gate below? Three
# answers, never two: `true` and `false` are readings and exit 0; `unknown` exits 3 and means the RAM
# could not be read, so nothing was decided. A caller must not fold `unknown` into either reading.
#
# --list-gpus prints one line per card, in a form a shell splits without parsing JSON:
#   <index>|<uuid>|<vram_total_mb>|<vram_free_mb>|<tier>|<model>|<pci_bus_id>|<name>
# The name comes LAST because it may hold spaces. tier and model come from the SAME ladder and the SAME
# family pick (hashed on the machine) as the report below: one function, called by both paths. A field
# that could not be read is `?`, never 0. Three states, never two: no nvidia-smi on this host -> no line,
# exit 0 (a reading: this kit has no NVIDIA card to use); nvidia-smi present and failing -> no line,
# exit 3 (unknown); otherwise one line per card, exit 0. "No devices were found" is nvidia-smi's own
# reading of zero cards, and is read as such.
# --gpu <uuid> keeps ONLY that card: its name, its VRAM, gpu_count 1. A UUID that is not a card of this
# host is refused (exit 2, nothing on stdout) -- never replaced by the largest card or by the CPU tier,
# since the identity it was asked for runs on that card and on no other. A failing nvidia-smi exits 3.
# The UUID NEVER appears in the JSON: it is a unique hardware identifier, and the JSON is published.
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
# The ONE form a card UUID takes here. MIG instances (MIG-...) are refused: one identity per PHYSICAL card.
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
# THE CARDS, ONE ROW EACH, read ONCE: index, UUID, PCI bus id, total and free memory, and the name LAST (a
# name may hold a comma; nothing after it has to be split). The PCI bus id is read because it is what the
# engine names: measured on ollama 0.32.1, its "inference compute" line names a card by index and pci_id,
# never by UUID -- so the per-card pinning check (deploy/testnet-miner/slots.sh) needs it.
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
# --gpu ON A HOST WHOSE CARDS CANNOT BE READ: unknown, and nothing is printed -- a report of another card,
# or of the CPU, would be published as the report of the identity that runs on the card asked for. The same
# for a card that IS listed and whose memory is not (`[N/A]`): its row would be skipped below, and the report
# would fall to the CPU tier -- a reading of nothing, published as the reading of that card.
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
  # Sum VRAM across GPUs but keep the LARGEST single card as the usable budget: Ollama loads one model
  # on one device by default, so two 8 GB cards do NOT let you run a 12 GB model.
  # `memory.total` IS THE SIZE OF THE CARD, NOT WHAT IS AVAILABLE ON IT. A desktop session, a browser
  # with hardware acceleration, or another model already resident all take VRAM that this figure keeps
  # counting as ours. The tier is still decided on the TOTAL -- it describes what the card can hold, and
  # a machine that will run headless should not be downgraded because someone ran the probe from their
  # desktop -- but the FREE figure is read too, so the gap can be stated instead of discovered when the
  # model refuses to load. Reported, never a verdict: this probe measures capacity, it does not book it.
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
# --no-gpu: the caller MEASURED that its engine cannot reach the card this host lists. The card stays named (the
# role's reason says which one is unusable), and none counts: the machine is decided as one without a card.
GPU_SEEN="$GPU_NAME"
if [ "$NO_GPU" = 1 ]; then GPU_COUNT=0; VRAM_MB=0; VRAM_FREE_MB=""; GPU_NAME=""; fi
# SYSTEM RAM, AND AN UNREADABLE ONE STAYS UNREAD. This figure gates the judge seat below; it used to fall
# back to 0 when `free` gave nothing, so a box whose memory could not be read was reported as one that
# cannot judge -- a reading nobody made. Empty means unread: the judge gate then decides nothing
# (--can-judge says `unknown`), the JSON says null, and only the CPU model SIZE below takes the
# smallest tier, which is the safe direction for a size and never a verdict on the gate.
# LC_ALL=C: procps-ng TRANSLATES the row label ("Mem:" reads "Speicher:" in German), and a label this line no longer
# finds is an unread RAM -- a host with every gigabyte the judge needs, told it cannot be decided.
RAM_MB="$(LC_ALL=C free -m 2>/dev/null | awk '/^Mem:/{print $2; exit}')"
case "$RAM_MB" in ''|*[!0-9]*) RAM_MB="" ;; esac
# FREE DISK, ON THE PATH THAT WILL ACTUALLY HOLD THE WEIGHTS. This probe selects models whose q4_K_M
# footprint runs from ~1 GB at tier 0 to ~19 GB at tier 5, and it used to select them without ever
# looking at whether the box could store one. The pull then fails long after the operator has been told
# the machine qualifies -- the worst moment to learn it. Docker keeps images and volumes under its own
# root, which is where the model lands, so that is the filesystem measured; the home directory is the
# fallback when docker is not installed yet. `?` when it cannot be read: unknown is not "enough".
# --no-docker, and the two modes that print no disk figure, ask the engine nothing: the figure is then
# UNREAD (null in the JSON, `?` in the report), never measured somewhere else under the same name.
if [ "$NO_DOCKER" = 1 ] || [ "$MODE" = "list-gpus" ] || [ "$MODE" = "judge-floor-mb" ] || [ "$MODE" = "role" ] || [ "$MODE" = "judge-allowed" ]; then
  DISK_PATH="(not measured: no docker call was made)"; DISK_FREE_MB=""
else
DISK_PATH="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || printf '')"
[ -n "${DISK_PATH:-}" ] && [ -d "$DISK_PATH" ] || DISK_PATH="${HOME:-/}"
DISK_FREE_MB="$(df -Pm "$DISK_PATH" 2>/dev/null | awk 'NR==2{print $4}')"; DISK_FREE_MB="${DISK_FREE_MB:-}"
fi
CPU_CORES="$(nproc 2>/dev/null || echo 1)"
CPU_MODEL="$(awk -F: '/model name/{gsub(/^ +/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null || echo unknown)"

# Usable budget in MB. GPU: keep 15% headroom for the KV cache/context (a model at 100% of VRAM
# thrashes). No GPU: fall back to RAM with a much harsher cap — CPU inference is slow, stay small. (On the
# testnet no machine MINES on the CPU any more -- see the ROLE below: these CPU rows only fill `tier` and `budget_mb`.)
if [ "$VRAM_MB" -gt 0 ]; then
  BUDGET_MB=$(( VRAM_MB * 85 / 100 )); BACKEND="gpu"
else
  BUDGET_MB=$(( ${RAM_MB:-0} * 40 / 100 )); BACKEND="cpu"
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
# _tier_for <budget_mb> -> sets TIER, CANDIDATES, TIER_MIN_MB from the ladder. ONE function, called for the
# machine below and for each card by --list-gpus: a per-card tier computed by a copy of this loop would
# drift from the one the report publishes.
_tier_for(){
  local _t _min _models
  TIER=0; CANDIDATES="llama3.2:1b"; TIER_MIN_MB=0
  while IFS='|' read -r _t _min _models; do
    [ -n "${_t:-}" ] || continue
    # TIER_MIN_MB is what the tier REQUIRES; BUDGET_MB is the envelope this machine happens to have.
    # They are different numbers, and only the first answers "will the model fit". Kept here, where
    # the ladder is read, so it cannot drift from the row that granted the tier.
    if [ "$1" -ge "$_min" ]; then TIER="$_t"; CANDIDATES="$_models"; TIER_MIN_MB="$_min"; break; fi
  done < <(printf '%s\n' "$LADDER" | grep -v '^$')
}

# Deterministic family pick, hashed on the MACHINE — NOT on the identity. Several judge identities
# sharing one GPU MUST run the SAME model: otherwise Ollama juggles several families on one card and
# everything thrashes: co-resident identities on two families exceed the card, Ollama offloads to CPU,
# inference misses its deadline and jobs never settle. Diversity therefore comes from DIFFERENT machines,
# which is exactly what the audit committee needs, and it stays stable across restarts. The same hash for
# every card of this machine: two cards of one tier run one model, kept once in a shared model store.
_model_for(){ # _model_for <candidates> -> sets MODEL (and nothing else: its counters are local)
  local _n _h _pick
  _n=$(printf '%s' "$1" | awk -F, '{print NF}')
  _h=$(printf '%s' "$MACHINE_KEY" | sha256sum | tr -dc '0-9' | cut -c1-6)
  _pick=$(( (10#${_h:-0} % _n) + 1 ))
  MODEL="$(printf '%s' "$1" | cut -d, -f"$_pick")"
}
_tier_for "$BUDGET_MB"
_model_for "$CANDIDATES"

# JUDGE-ROLE GATE.
# A judge that is too weak does not merely judge badly: it votes DIVERGENT on answers that are correct
# but worded differently, and an unfair verdict costs an honest miner its stake. The project already
# bans qwen3:4b as a judge for exactly that reason; anything below the mistral-nemo class is weaker
# still. Hence: a node may MINE at any tier, and JUDGES only with an allow-listed model, on the CPU (the
# MoE below, gated on system RAM). An under-powered judge is the fastest way to damage an honest network.
# The judge gate binds the MODEL: modea/judge.py validates mistral-nemo and the MoE, and bans qwen3:4b
# for unfair verdicts. Allow-list, therefore: an unproven judge model never sits, whatever the hardware.
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

# NO JUDGE IS SEATED ON THE GPU. This file used to seat one from tier 3 up with the card's own model
# (mistral-nemo), and that seat could never vote. Three facts, each read in the code that applies it:
#   · deploy/join.sh points EVERY judge at the kit's CPU instance (DENDRA_JUDGE_ENDPOINT=ollama-cpu);
#   · the judge model a juror runs is the one the network PINS on chain, read first
#     (judge_worker.py::resolve_judge_model, modelregistry `audit_judge_model`): the MoE below;
#   · the kit downloads the model this file named, not the pinned one.
# A GPU-tier box therefore pulled mistral-nemo into the CPU instance, waited 30 minutes for the pinned
# MoE that never came, and sat on juries as a MUTE SEAT. Judging is the CPU path below, with or without a
# card: the pinned MoE on the CPU, gated on system RAM. `judge_backend` stays in the JSON: "cpu" or
# "none" — the capacity registry reads it.
CAN_JUDGE=false
JUDGE_BACKEND="none"
# DECLARED, not inferred from an absence: dendra_site_verite_garde.sh reads this line, and refuses any
# page that announces a VRAM floor for judging while it says 0.
JUDGE_ON_GPU=0

# THE JUDGE — THE MoE ON THE CPU. A Mixture-of-Experts activates only a fraction of its weights per token
# (qwen3:30b-a3b = ~3B ACTIVE), so it runs at usable speed on CPU where a DENSE model of the same file
# size would be hopeless. This is the project's ORIGINAL validated judge configuration (MoE q4 on CPU,
# GPU left free for mining, ~10.6 s/case). The generic CPU cap above exists to stop DENSE models from
# crawling — applying it to a MoE would wrongly lock out the ONLY way to bring the audit layer back
# without buying hardware. Requires enough RAM to hold the weights; run ONE such judge per box.
MOE_CPU_MODEL="${DENDRA_MOE_CPU_MODEL:-qwen3:30b-a3b-instruct-2507-q4_K_M}"
MOE_CPU_MIN_RAM_MB="${DENDRA_MOE_CPU_MIN_RAM_MB:-26000}"   # ~18.6 GB weights + OS/mining headroom
# HARD FLOOR, not overridable downwards: without it `DENDRA_MOE_CPU_MIN_RAM_MB=0` (plus a tweaked
# MOE_CPU_MODEL) would turn ANY box into a judge — including with a model far too small to judge fairly.
# An env var must never be able to disable a safety gate: it may raise the default, or lower it down to
# this hard floor, never past it.
[ "${MOE_CPU_MIN_RAM_MB:-0}" -ge 24000 ] 2>/dev/null || MOE_CPU_MIN_RAM_MB=24000
# NOTE: this sets the JUDGE model only. On a box with a card, `model` (serving/mining) stays the GPU-sized pick —
# the box mines fast on the card AND judges on the CPU at the same time; conflating the two would drag mining
# down to CPU speed and re-create the timeout problem we just fixed. Without a card, see the ROLE below.
MOE_CPU=0
JUDGE_MODEL="$MODEL"
if [ "$CAN_JUDGE" != "true" ] && [ -n "$RAM_MB" ] && [ "$RAM_MB" -ge "$MOE_CPU_MIN_RAM_MB" ] && _is_allowed_judge "$MOE_CPU_MODEL"; then
  MOE_CPU=1; CAN_JUDGE=true; JUDGE_BACKEND="cpu"
  JUDGE_MODEL="$MOE_CPU_MODEL"
fi

# ---------------------------------------------------------------- the ROLE of this machine on the testnet
# NO MINING MODEL ON THE CPU. A machine without a usable card used to mine with the small model its RAM bought
# (the CPU rows of the ladder above). On the testnet that is OUT: such a machine is a JUDGE when it clears the
# judge gate just above, and is REFUSED when it does not. A judge is drawn by the chain like any present miner,
# as the primary of a request too (chain/x/jobs/keeper/committee.go::deriveCommitteeOrdered draws on vitality
# and presence, with no role), so it ANSWERS those requests with the same judge model on the same CPU instance
# rather than leaving them open. An unread RAM decides nothing. A machine with a usable card: unchanged.
# ROLE_WHY says why in words an operator can act on; every installer prints it (--role).
ROLE_WHY=""
if [ "$VRAM_MB" -gt 0 ]; then
  ROLE=miner
  ROLE_WHY="a usable NVIDIA card ($GPU_NAME, $VRAM_MB MB of VRAM): this machine mines on the card, with $MODEL."
else
  # Why no card is usable, in the words the operator can act on.
  case "$NO_GPU:$GPU_SMI" in
    1:*) _nocard="the NVIDIA card this host lists (${GPU_SEEN:-?}) is not reachable by the container engine (measured by the caller: with the NVIDIA container toolkit installed, the card mines)" ;;
    0:failed) _nocard="nvidia-smi is installed and FAILS, so no card can be read here (when nvidia-smi answers, a card mines)" ;;
    *) _nocard="no NVIDIA card this kit can use" ;;
  esac
  if [ -z "$RAM_MB" ]; then
    ROLE=unknown; MODEL=""; CANDIDATES=""
    ROLE_WHY="$_nocard, and the system RAM could not be read (free -m): whether this machine reaches MOE_CPU_MIN_RAM_MB (${MOE_CPU_MIN_RAM_MB} MB) is UNKNOWN, and an unread gate is not a pass. Nothing was decided."
  elif [ "$CAN_JUDGE" = true ]; then
    ROLE=judge; MODEL="$JUDGE_MODEL"; CANDIDATES="$JUDGE_MODEL"
    ROLE_WHY="$_nocard, and ${RAM_MB} MB of system RAM, at or above MOE_CPU_MIN_RAM_MB (${MOE_CPU_MIN_RAM_MB} MB): this machine JUDGES on the CPU with $JUDGE_MODEL, and answers the requests the chain assigns it with that same model, on the kit's CPU instance. No mining model is pulled."
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
  # whether a model fits, and would have to re-derive the ladder — which is how two answers to
  # one question start.
  printf '"cpu_cores":%d,"cpu":"%s","budget_mb":%d,"tier_min_mb":%d,"tier":%d,"model":"%s","judge_model":"%s",' \
    "$CPU_CORES" "$CPU_MODEL" "$BUDGET_MB" "$TIER_MIN_MB" "$TIER" "$MODEL" "$JUDGE_MODEL"
  # `can_judge` is null on an unread RAM, as --can-judge says unknown: the gate decided nothing, and a
  # consumer must not read a decision into it. A capacity registry that only counts judges reads null as
  # "not a judge", which is the safe direction; a gate reads it as unknown.
  # `role` is the decision --role prints, null when it is unknown (the same reading as `can_judge` above).
  # `model` is what the machine SERVES: the card's pick, or the judge model on the CPU, or "" with no role.
  printf '"can_judge":%s,"moe_cpu":%d,"judge_backend":"%s","role":%s,"candidates":"%s"}\n' \
    "$( [ -n "$RAM_MB" ] && printf '%s' "$CAN_JUDGE" || printf null)" "$MOE_CPU" "$JUDGE_BACKEND" \
    "$( [ "$ROLE" = unknown ] && printf null || printf '"%s"' "$ROLE")" "$CANDIDATES"
}

case "$MODE" in
  json)  json ;;
  model)
    # The model this machine SERVES. A machine with no role serves none: nothing is printed, and the exit
    # code says which of the two it is (2 refused, 3 unknown) -- an empty line is never a model.
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
    # The floor AFTER its hard clamp: the number a caller reserves for the judge is derived here, where the
    # gate applies it, and never retyped beside it.
    printf '%s\n' "$MOE_CPU_MIN_RAM_MB" ;;
  list-gpus)
    case "$GPU_SMI" in
      failed) echo "[hw] nvidia-smi is present and failing: the cards of this host are UNKNOWN (not zero)" >&2; exit 3 ;;
      absent|none) exit 0 ;;
    esac
    while IFS= read -r _row; do
      # A blank line is no card. Any other line is one row: a line nvidia-smi printed INSTEAD of a card (a
      # lost card's "Unable to determine the device handle for GPU0000:03:00.0: Unknown Error") is a card
      # whose fields are unread, `?` -- never skipped, since the cards of this host are not fewer because one
      # could not be read. The index is the field as nvidia-smi wrote it, or `?`: digits picked out of that
      # message would be an index nobody wrote.
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
    # The gate above is the ONLY decision: this mode prints it, it does not re-derive it. An unread RAM
    # left CAN_JUDGE false without deciding anything, so it is told apart here, before the reading.
    if [ -z "$RAM_MB" ]; then printf 'unknown\n'; exit 3; fi
    printf '%s\n' "$CAN_JUDGE" ;;
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
      echo "  GPU        : none usable ($_nocard)"
      echo "               -> no mining model on the CPU on the testnet: the role below decides"
    fi
    echo "  RAM / CPU  : ${RAM_MB:-?} MB / ${CPU_CORES} cores ($CPU_MODEL)$( [ -n "$RAM_MB" ] || printf '  (RAM unread: the judge gate decides nothing)' )"
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
    if [ "$ROLE" = miner ]; then
      echo "  Budget     : ${BUDGET_MB} MB usable for the model (headroom kept for the KV cache)"
      echo "  Tier       : $TIER   candidates: $CANDIDATES"
      echo "  -> model   : $MODEL   (deterministic pick from the MACHINE = one model per card, diversity across machines)"
    elif [ "$ROLE" = judge ]; then
      echo "  -> model   : $MODEL   (the judge model, served on the CPU instance: no mining model is pulled)"
    else
      echo "  -> model   : none"
    fi
    if [ "$ROLE" = judge ]; then
      # Without a card the box judges, and the chain draws it like any present miner, as the primary of a
      # request too: it answers with the judge model, on the same CPU instance, rather than leaving it open.
      echo "  -> role    : JUDGE (CPU, $JUDGE_MODEL) -- no usable GPU"
      echo "               It judges on the CPU (a mixture-of-experts: few ACTIVE params), and answers the requests"
      echo "               the chain assigns it with that same model, on the same CPU instance. No mining model."
      echo "               Run ONE such judge on this box; the ${MOE_CPU_MIN_RAM_MB} MB RAM floor (MOE_CPU_MIN_RAM_MB) it"
      echo "               cleared is what holds the weights alongside the OS."
    elif [ "$ROLE" = refused ] || [ "$ROLE" = unknown ]; then
      echo "  -> role    : $( [ "$ROLE" = refused ] && printf 'NONE (refused)' || printf 'UNKNOWN (nothing decided)' )"
      printf '               %s\n' "$ROLE_WHY"
    elif [ "$MOE_CPU" = "1" ]; then
      echo "  -> role    : MINE (GPU, $MODEL) + JUDGE (CPU MoE, $JUDGE_MODEL)"
      echo "               MoE = few ACTIVE params -> usable on CPU while the GPU keeps mining."
      echo "               Run ONE such judge on this box (MOE_COUNT=1); the ${MOE_CPU_MIN_RAM_MB} MB RAM floor it just"
      echo "               cleared is what holds the weights alongside the OS and the mining process."
    else
      echo "  -> role    : MINE ONLY (GPU) — this box does not clear the JUDGE gate. Judging runs the network's"
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
