#!/usr/bin/env bash
# judge.sh -- one switch for the judge of this kit, and a status that reads what the judge produced.
#
#   bash deploy/testnet-miner/judge.sh on      # pull the judge model (compose service judge-model-init), set
#                                              # DENDRA_MINER_JUDGE=1 in the kit's .env, recreate the miner's container only
#   bash deploy/testnet-miner/judge.sh off     # clear it and recreate the same container; the judge's CPU engine is
#                                              # stopped, its model stays on disk; the refusal is recorded (DENDRA_NO_JUDGE=1)
#   bash deploy/testnet-miner/judge.sh status  # what the judge produced on chain for this identity, each figure with
#                                              # the height it was read at, and the juror's abstentions by stage from its own log
#
# A process that runs is not a status; a verdict on chain is. `status` counts this identity's verdict commits
# (`<job>__verdict__<id>`, judge_worker.py) on audits whose anchored jury names it (GET
# /dendra/jobs/v1/audit_committee/{job_id}), prints the height the chain was read at, and reads the abstentions by
# stage from the judge's own state file in the miner's container (modea/heartbeat.py::judge_state_path, written by
# judge_worker.py::JudgeLedger; the stage is the one judge_worker.py::decide_verdict names).
# `on` seats the judge only where deploy/hw_probe.sh --can-judge answers true (an unread gate seats nothing) and
# where the disk clears the judge floor (DENDRA_MIN_DISK_GB_JUDGE, the figure deploy/install.sh applies), and says
# what it downloads before it pulls. It then asks what the machine can give the judge (deploy/hw_probe.sh
# --judge-survey, with the judge's CPU engine started for it when it was not running, and stopped again on a refusal,
# so that the survey reads a model already on disk): it prints the probe's line, refuses a first seat where the survey says impossible or could not
# read the machine (rc 2, nothing changed), keeps a seat the kit already holds on demand, and writes the DENDRA_JUDGE_*
# lines the survey prints into the .env. `status` ends with the judge's memory: mode and ceiling, the guard's gate and
# its reason, whether the model is loaded, its footprint, the last load, the unloads and out-of-memory errors, read from
# the daemon's heartbeat (judge_memory) and the juror's state. It then prints the judge's seat with the chain: the
# declaration, the height of its last verdict and the concluded audits it was seated on without answering
# (heartbeat key judge_declaration) against judge_missed_draws_max read from the chain's params; a judge the chain
# stopped drawing is reported with what brings it back: the miner declares it again by itself once its judge can
# answer (services/miner.py::judge_return_action), and the restart and the commands that do it by
# hand for a judge the miner already declared again once (miner.py::JudgeSeat). The desktop
# application and the HiveOS package call this file.
# A kit with one identity per card (deploy/join.sh --gpus) carries one env per slot: there the switch is a re-run
# of deploy/join.sh with --judge or --no-judge, and this file says so.
# Every compose command goes through slots.sh (slot_compose 0, the kit's own copy), and the node kit is read with
# `docker exec` on the container its compose labels name: a tool of the miner kit never runs compose itself, since
# a command typed here would act on slot 0's identity whatever slot was meant (dendra_slot_compose_garde.sh).
# Exit codes: 0 done · 1 a step failed · 2 refused (the gate, the disk, the role on the CPU, a slot kit, `off` while
# GPU mining is off) · 3 not
# measured (no docker, no python3, nothing to read the chain with).
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
KIT="${DENDRA_MINER_KIT:-$SELF}"
REPO="$(cd "$KIT/../.." 2>/dev/null && pwd)"
NODE_KIT="$REPO/deploy/testnet-node"
PROBE="$REPO/deploy/hw_probe.sh"
VERB="${1:-}"
say(){ printf '%s\n' "$*"; }
# A refusal of this script names the section of the site that settles it. site_topic reads what the message names and
# site_url answers the section of that topic; that every topic is a section of the site is checked by
# dendra/onchain-staging/dendra_kit_site_links_test.py.
SITE_BASE="https://dendranetwork.com"
site_url(){
  case "$1" in
    install) printf '%s/miner/#install' "$SITE_BASE" ;;
    need)    printf '%s/miner/#need' "$SITE_BASE" ;;
    judge)   printf '%s/miner/#judge' "$SITE_BASE" ;;
    help)    printf '%s/miner/#help' "$SITE_BASE" ;;
  esac
}
site_topic(){
  case "$*" in
    *"no kit here"*) printf install ;;
    *"cannot judge"*|*"can judge"*|*"hardware probe"*|*disk*|*"GPU mining"*) printf need ;;
    *"did not start"*|*"was not pulled"*|*"not recreated"*) printf help ;;
    *) printf judge ;;
  esac
}
refusal_link(){ printf '[judge] More: %s\n' "$(site_url "$(site_topic "$*")")" >&2; }
refuse(){ printf '[judge] refused: %s\n' "$*" >&2; refusal_link "$*"; exit 2; }
unmeasured(){ printf '[judge] not measured: %s\n' "$*" >&2; refusal_link "$*"; exit 3; }
failed(){ printf '[judge] FAILED: %s\n' "$*" >&2; refusal_link "$*"; exit 1; }
kitval(){ [ -r "$KIT/.env" ] || return 0; tr -d '\r' < "$KIT/.env" | sed -n "s/^$1=//p" | head -1; }
# env_set <KEY> <value> [unset] -- one line of the kit's .env, through slots.sh::slot_env_set on slot 0: the file is
# replaced in one rename and keeps its mode. Written in place, it was truncated first, and the Stop of the desktop
# application, which kills this script's process group, could leave it empty.
env_set(){ slot_env_set 0 "$@"; }
case "$VERB" in
  on|off|status) : ;;
  -h|--help|"") awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; [ -n "$VERB" ] && exit 0; exit 2 ;;
  *) printf '[judge] unknown verb: %s (on, off or status)\n' "$VERB" >&2; exit 2 ;;
esac
[ -r "$KIT/.env" ] || refuse "no kit here ($KIT/.env): join first (deploy/join.sh)."
command -v docker >/dev/null 2>&1 || unmeasured "docker is not installed: the judge runs in the kit's containers."
{ [ -r "$KIT/slots.sh" ] && . "$KIT/slots.sh" && declare -F slot_compose >/dev/null 2>&1; } || unmeasured "the slot library ($KIT/slots.sh) cannot be loaded: which identity this would act on is not known."
SLOT_IDS="$(slot_ids --all)" || unmeasured "the slots of this kit ($KIT/gpu) cannot be read: which identity this would act on is not known."
if [ "$(printf '%s' "$SLOT_IDS" | awk 'NF{n++} END{print n+0}')" -ge 2 ]; then
  refuse "this kit runs one identity per card (deploy/join.sh --gpus): its judge is switched by re-running deploy/join.sh with --judge or --no-judge and the options you used ($(kitval DENDRA_KIT_RERUN))."
fi
# The role on the CPU: a machine without a usable GPU is a judge or nothing (deploy/join.sh::write_cpu_judge_override
# writes the override that says so). Its judge is not a switch.
CPU_ROLE=0
grep -q 'judge role on the CPU' "$KIT/docker-compose.override.yml" 2>/dev/null && CPU_ROLE=1

# ---------------------------------------------------------------- on
judge_on(){
  local can rc m free floor jpath droot size survey src smode survey_human survey_why survey_env survey_err errf seated ondemand_forced written k v
  local ovb ov_mem ov_cpus ov_thr s_cpus s_thr fp hr mism rerun eng_was eng_started=0
  [ "$CPU_ROLE" = 1 ] && { say "[judge] this machine judges on the CPU as its whole role (no usable GPU): the judge is already its role, nothing to switch."; exit 0; }
  [ -r "$PROBE" ] || refuse "the hardware probe is not in this clone ($PROBE): whether this machine can judge cannot be read, and an unread gate seats nothing."
  can="$(bash "$PROBE" --can-judge 2>/dev/null | head -1 | tr -d '[:space:]')"; rc=$?
  case "$rc:$can" in
    0:true) say "[judge] this machine can judge (deploy/hw_probe.sh --can-judge: true)." ;;
    0:false) refuse "this machine cannot judge (deploy/hw_probe.sh --can-judge: false): the judge runs the network's judge model on the CPU and needs MOE_CPU_MIN_RAM_MB of system RAM (bash deploy/hw_probe.sh --judge-floor-mb prints it). Seating it anyway would make it a mute seat." ;;
    *) refuse "whether this machine can judge is unknown (deploy/hw_probe.sh --can-judge: exit $rc, '${can:-no answer}'): its RAM was not read, and an unread gate seats no judge. Run bash deploy/hw_probe.sh to see what it reads." ;;
  esac
  # What this machine can give the judge, measured now: deploy/hw_probe.sh --judge-survey (lines key=value). The probe
  # makes the decision, this file copies what it printed and never recomputes it. The settings the operator keeps in the
  # kit's .env are handed to the probe, which reads them from its environment, so a re-seat does not forget them; so are
  # the kit's directories, the mode recorded there, the judge engine's parallel slots and the judge model, which the probe
  # cannot find by itself (deploy/join.sh gives it the same). The judge's run-time settings go with them: the probe
  # refuses (exit 2) one that the judge's reader in the miner container would refuse.
  m="$(kitval DENDRA_JUDGE_MODEL_ID)"
  [ -n "$m" ] || m="$(bash "$PROBE" --json --no-docker 2>/dev/null | grep -o '"judge_model":"[^"]*"' | cut -d'"' -f4)"
  [ -n "$m" ] || m="qwen3:30b-a3b-instruct-2507-q4_K_M"
  # The judge's CPU engine runs during the survey, so that the survey reads the model's file and metadata from it when the
  # model is already on this disk (a seat taken again after `off`, which stops the engine); without it the survey falls
  # back to the figures declared for the model. It is started here only when it was not running, and stopped again on a
  # refusal below, so that a refusal leaves the kit as it was. The survey watches the memory before it asks the engine,
  # which leaves the engine that time to answer.
  eng_was="$(slot_cid 0 ollama-cpu --running)" || eng_was=unread
  if [ -z "$eng_was" ]; then
    SLOT_KEEP_ENV=DENDRA_JUDGE_MODEL_ID DENDRA_JUDGE_MODEL_ID="$m" slot_compose 0 --profile judge up -d ollama-cpu >/dev/null 2>&1 && eng_started=1
  fi
  refuse_on(){ [ "$eng_started" = 1 ] && slot_compose 0 --profile judge stop ollama-cpu >/dev/null 2>&1; refuse "$@"; }
  errf="$(mktemp)" || errf=/dev/null
  survey="$(env DENDRA_SURVEY_MINER_KIT="$KIT" DENDRA_SURVEY_NODE_KIT="$NODE_KIT" DENDRA_SURVEY_REPO="$REPO" \
            DENDRA_SURVEY_JUDGE_MODEL="$m" DENDRA_SURVEY_RECORDED_MODE="$(kitval DENDRA_JUDGE_MODE)" \
            DENDRA_JUDGE_MODE_CHOICE="$(kitval DENDRA_JUDGE_MODE_CHOICE)" DENDRA_JUDGE_CPU_PCT="$(kitval DENDRA_JUDGE_CPU_PCT)" \
            DENDRA_JUDGE_HEADROOM_MB="$(kitval DENDRA_JUDGE_HEADROOM_MB)" DENDRA_SURVEY_NUM_PARALLEL="$(kitval DENDRA_CPU_NUM_PARALLEL)" \
            DENDRA_JUDGE_KEEPALIVE="$(kitval DENDRA_JUDGE_KEEPALIVE)" DENDRA_JUDGE_ONDEMAND_KEEPALIVE="$(kitval DENDRA_JUDGE_ONDEMAND_KEEPALIVE)" \
            DENDRA_MEMWATCH_PERIOD_S="$(kitval DENDRA_MEMWATCH_PERIOD_S)" DENDRA_MEMWATCH_PSI_SOME_PCT="$(kitval DENDRA_MEMWATCH_PSI_SOME_PCT)" \
            DENDRA_MEMWATCH_PROMOTE_AFTER_S="$(kitval DENDRA_MEMWATCH_PROMOTE_AFTER_S)" \
            DENDRA_JUDGE_TIMEOUT_S="${DENDRA_JUDGE_TIMEOUT_S:-$(kitval DENDRA_JUDGE_TIMEOUT_S)}" \
            DENDRA_PRESENCE_PROBE_S="${DENDRA_PRESENCE_PROBE_S:-$(kitval DENDRA_PRESENCE_PROBE_S)}" \
            bash "$PROBE" --judge-survey 2>"$errf")"; src=$?
  survey="$(printf '%s\n' "$survey" | tr -d '\r')"
  survey_err="$(head -1 "$errf" 2>/dev/null | tr -d '\r')"; [ "$errf" = /dev/null ] || rm -f "$errf"
  smode="$(printf '%s\n' "$survey" | sed -n 's/^judge_mode=//p' | head -1 | tr -d '[:space:]')"
  survey_human="$(printf '%s\n' "$survey" | sed -n 's/^human=//p' | head -1)"
  [ -n "$survey_human" ] && say "$survey_human"
  survey_why="$(printf '%s\n' "$survey" | sed -n 's/^judge_mode_why=//p' | head -1)"
  seated=0; [ "$(kitval DENDRA_MINER_JUDGE)" = 1 ] && seated=1
  ondemand_forced=0
  case "$src:$smode" in
    0:resident|0:on-demand) say "[judge] mode: $smode${survey_why:+ ($survey_why)}." ;;
    0:impossible)
      # A seat this kit already holds is kept on demand (the guard abstains when there is no room); a first seat is not taken.
      if [ "$seated" = 1 ]; then ondemand_forced=1; say "[judge] this kit already holds the seat: it stays, on demand, and the memory guard abstains from an audit when there is no room${survey_why:+ ($survey_why)}."
      else refuse_on "this machine has no room for the judge's model even at the quietest moment it was watched${survey_why:+ ($survey_why)}: the seat is not taken. Nothing was changed."; fi ;;
    2:*) refuse_on "a judge setting of this kit's .env is refused by the probe (deploy/hw_probe.sh --judge-survey${survey_err:+: $survey_err}). Correct or remove that line of $KIT/.env, then run this command again." ;;
    *) refuse_on "what this machine can give the judge is not known (deploy/hw_probe.sh --judge-survey: exit $src, judge_mode '${smode:-no answer}'${survey_err:+, it said: $survey_err}): an unread survey seats no judge. Nothing was changed." ;;
  esac
  # The lines the survey asks to be kept, checked before anything is pulled: a line this switch would not write is a refusal,
  # never a silent drop.
  survey_env="$(printf '%s\n' "$survey" | sed -n 's/^env[.]\(DENDRA_JUDGE_[A-Z0-9_]*\)=\([A-Za-z0-9._:-]*\)$/\1 \2/p')"
  [ "$(printf '%s\n' "$survey_env" | awk 'NF || /./ {n++} END {print n+0}')" = "$(printf '%s\n' "$survey" | grep -c '^env[.]')" ] \
    || refuse_on "the survey printed an env line this switch will not write (it keeps DENDRA_JUDGE_* lines whose value is made of letters, digits and . _ : - only). Nothing was changed."
  # The judge engine's limits in force are the ones deploy/join.sh wrote into the override, on an earlier run: this switch
  # does not rewrite them. It checks them against the machine as measured now and refuses, nothing changed, where they
  # do not fit it: a memory limit under the judge's footprint plus the headroom keeps the judge's check before each load
  # closed (that check compares the footprint with the limit) and stops every load, and a CPU quota or a thread count
  # other than the share measured now are another day's figures. The .env then receives the memory limit in force, the
  # one that check compares with, never a limit the survey would write and the engine does not have.
  survey_val(){ awk -v k="$1=" 'index($0, k) == 1 { print substr($0, length(k) + 1); exit }' <<< "$survey"; }
  _uint(){ case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }
  ovb="$(awk '/^  ollama-cpu:/ { f = 1; next } f && /^  [^ ]/ { f = 0 } f' "$KIT/docker-compose.override.yml" 2>/dev/null)"
  ov_mem="$(awk '$1 == "mem_limit:" { v = $2; if (sub(/m$/, "", v) && v ~ /^[0-9]+$/) print v; exit }' <<< "$ovb")"
  ov_cpus="$(awk '$1 == "cpus:" { v = $2; gsub(/"/, "", v); print v; exit }' <<< "$ovb")"
  ov_thr="$(awk '$1 == "LLAMA_ARG_THREADS:" { v = $2; gsub(/"/, "", v); print v; exit }' <<< "$ovb")"
  s_cpus="$(survey_val judge_cpus)"; _uint "$s_cpus" || s_cpus=""
  s_thr="$(survey_val judge_num_thread)"; _uint "$s_thr" || s_thr=""
  fp="$(survey_val judge_footprint_mb)"; hr="$(survey_val headroom_mb)"
  mism=""
  [ "$ov_cpus" = "$s_cpus" ] || mism="cpus ${ov_cpus:-none} in the override, ${s_cpus:-none} measured now"
  [ "$ov_thr" = "$s_thr" ] || mism="${mism:+$mism; }threads ${ov_thr:-none} in the override, ${s_thr:-none} measured now"
  if [ -n "$ov_mem" ]; then
    if _uint "$fp" && _uint "$hr"; then
      [ "$ov_mem" -ge $(( fp + hr )) ] || mism="${mism:+$mism; }mem_limit $ov_mem MiB in the override, under the judge's footprint $fp MiB ($(survey_val judge_footprint_source)) plus the headroom $hr MiB measured now"
    else
      mism="${mism:+$mism; }mem_limit $ov_mem MiB in the override, and the judge's footprint is not known now to compare it with"
    fi
  fi
  if [ -n "$mism" ]; then
    rerun="$(kitval DENDRA_KIT_RERUN)"
    refuse_on "the judge engine's limits that deploy/join.sh wrote into $KIT/docker-compose.override.yml on an earlier run do not fit this machine as measured now ($mism). Nothing was changed. The installation writes them again, with the seat: ${rerun:-bash $REPO/deploy/join.sh} --judge"
  fi
  if [ -n "$ov_mem" ]; then say "[judge] memory: the judge engine is capped at $ov_mem MiB (the override), which holds the judge's footprint measured now."
  else say "[judge] memory: no limit on the judge engine in the override; the judge's check before each load decides on fresh readings."; fi
  size="$(sed -n 's/.*judge model .*(\(~[0-9][0-9]* GB\)[,)].*/\1/p' "$KIT/docker-compose.yml" 2>/dev/null | head -1)"
  # The disk, before the pull: a first seating lands the model under Docker's data root (the Windows drive under
  # WSL). A kit that already seats the judge has pulled it, and the free-space figure counts it as used.
  if [ "$(kitval DENDRA_MINER_JUDGE)" != 1 ]; then
    floor="${DENDRA_MIN_DISK_GB_JUDGE:-32}"
    if grep -qi microsoft /proc/version 2>/dev/null; then jpath="${DENDRA_WSL_HOST_MOUNT:-/mnt/c}"
    else droot="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"; jpath="$droot"; fi
    free=""; [ -n "$jpath" ] && free="$(df -BG --output=avail "$jpath" 2>/dev/null | tail -1 | tr -dc '0-9')"
    [ -n "$free" ] || refuse_on "free disk where the judge model lands could not be measured (${jpath:-docker info named no data root}): the seat is not taken on an unread floor (DENDRA_MIN_DISK_GB_JUDGE, ${floor} GB)."
    [ "$free" -ge "$floor" ] 2>/dev/null || refuse_on "free disk ${free}G under $jpath is below the judge floor of ${floor} GB (DENDRA_MIN_DISK_GB_JUDGE, deploy/install.sh): the judge model $m (${size:-size as the compose file states it}) would land there. Free space first."
    say "[judge] the seat pulls the judge model $m (${size:-size as deploy/testnet-miner/docker-compose.yml states it} on disk) into the kit's CPU instance; free disk ${free}G under $jpath clears the floor of ${floor} GB."
  else
    say "[judge] this kit already names the judge (DENDRA_MINER_JUDGE=1): the model $m is pulled again only if it is missing."
  fi
  say "[judge] starting the CPU engine and pulling the model (compose services ollama-cpu, judge-model-init)."
  # The model reaches compose through the environment: the .env receives it only once the pull succeeded, so a
  # failed pull leaves the .env as it was. slot_compose removes every variable the compose files interpolate
  # unless SLOT_KEEP_ENV names it.
  SLOT_KEEP_ENV=DENDRA_JUDGE_MODEL_ID DENDRA_JUDGE_MODEL_ID="$m" slot_compose 0 --profile judge up -d ollama-cpu || failed "the CPU engine (ollama-cpu) did not start."
  SLOT_KEEP_ENV=DENDRA_JUDGE_MODEL_ID DENDRA_JUDGE_MODEL_ID="$m" slot_compose 0 --profile judge run --rm --no-deps judge-model-init || failed "the judge model was not pulled (judge-model-init): the kit's .env is unchanged, the miner runs as before."
  env_set DENDRA_MINER_JUDGE 1
  env_set DENDRA_JUDGE_ENDPOINT "http://ollama-cpu:11434"
  env_set DENDRA_JUDGE_MODEL_ID "$m"
  env_set COMPOSE_PROFILES judge
  env_set DENDRA_NO_JUDGE 0
  # The settings the survey chose, written with the rest once the pull succeeded (a failed pull leaves the .env as it was).
  written=""
  while read -r k v; do
    [ -n "$k" ] || continue
    [ "$ondemand_forced" = 1 ] && [ "$k" = DENDRA_JUDGE_MODE ] && v=on-demand
    [ "$k" = DENDRA_JUDGE_MEM_LIMIT_MB ] && v="$ov_mem"
    env_set "$k" "$v"; written="${written:+$written, }$k=$v"
  done <<EOF
$survey_env
EOF
  if [ "$ondemand_forced" = 1 ] && ! printf '%s\n' "$survey_env" | grep -q '^DENDRA_JUDGE_MODE '; then
    env_set DENDRA_JUDGE_MODE on-demand; written="${written:+$written, }DENDRA_JUDGE_MODE=on-demand"
  fi
  say "[judge] $KIT/.env: DENDRA_MINER_JUDGE=1, DENDRA_JUDGE_ENDPOINT=http://ollama-cpu:11434, COMPOSE_PROFILES=judge, DENDRA_NO_JUDGE=0${written:+, $written}."
  say "[judge] recreating the miner's container only (its judge and reveal workers start inside it)."
  slot_compose 0 --profile judge up -d --no-deps --force-recreate miner || failed "the miner's container was not recreated: $(slot_run_hint 0 --profile judge up -d --no-deps --force-recreate miner)"
  say "[judge] on. Verdicts appear on chain once the chain draws this identity into a jury: bash $KIT/judge.sh status"
}

# ---------------------------------------------------------------- off
judge_off(){
  [ "$CPU_ROLE" = 1 ] && refuse "this machine judges on the CPU as its whole role (no usable GPU, deploy/hw_probe.sh --role: judge): without the judge it has no role on the testnet. To leave, read bash $KIT/exit-miner.sh --help."
  # GPU mining off (the desktop application's switch, deploy/app/dendra_app.py::switch_gpu): the miner answers on the
  # CPU with the judge model, which serve_guard.py accepts only in the judge role. Without the judge it would not start.
  [ -n "$(kitval DENDRA_SERVE_ENDPOINT)" ] && refuse "GPU mining is off on this kit (DENDRA_SERVE_ENDPOINT=$(kitval DENDRA_SERVE_ENDPOINT) in $KIT/.env): the miner answers on the CPU with the judge model, which it may do only as a judge (serve_guard.py). Turn GPU mining on first, in the Dendra application."
  env_set DENDRA_MINER_JUDGE 0
  env_set DENDRA_JUDGE_ENDPOINT "" unset
  env_set COMPOSE_PROFILES "" unset
  env_set DENDRA_NO_JUDGE 1
  say "[judge] $KIT/.env: DENDRA_MINER_JUDGE=0, DENDRA_NO_JUDGE=1 (a re-run of deploy/join.sh and the automatic update keep this refusal; --judge or 'judge.sh on' lifts it)."
  say "[judge] recreating the miner's container only."
  slot_compose 0 up -d --no-deps --force-recreate miner || failed "the miner's container was not recreated: $(slot_run_hint 0 up -d --no-deps --force-recreate miner)"
  slot_compose 0 --profile judge stop ollama-cpu judge-model-init >/dev/null 2>&1 || true
  # The seat on chain is the daemon's: it reads the role at its start (services/miner.py::judge_role_on)
  # and, on the epoch 15 chain, withdraws a declaration it reads there (services/miner.py::JudgeSeat).
  say "[judge] off. The CPU engine is stopped and the judge model stays on disk. On a network that still runs v0.2.2 this miner is still drawn into juries, where it never votes (a mute seat); from v0.3.0 a seat needs a judge declaration, which the restarted miner withdraws once it reads that the chain holds one (its log says what it read)."
}

# ---------------------------------------------------------------- status
# The chain is read from this machine's node when the kit runs one (deploy/testnet-node, dendrad in its container),
# else over REST from DENDRA_REST (the API listener of a node of the network); nothing else is guessed.
# node_cid -> the running node container of this clone's node kit: found by the labels compose writes (service
# node, not a one-off run), then confirmed by its working_dir label, as slots.sh::slot_cid confirms a miner's: a
# clone elsewhere on this machine may run a node of the same project name. Empty = none; 3 when docker does not answer.
node_cid(){
  local ids id wd kp
  ids="$(docker ps -q --filter label=com.docker.compose.service=node --filter label=com.docker.compose.oneoff=False 2>/dev/null)" || return 3
  kp="$(cd "$NODE_KIT" 2>/dev/null && pwd -P)"
  for id in $ids; do
    wd="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id" 2>/dev/null)" || return 3
    wd="$(printf '%s' "$wd" | tr -d '[:cntrl:]')"
    if [ "$wd" = "$NODE_KIT" ] || { [ -n "$kp" ] && [ "$wd" = "$kp" ]; }; then echo "$id"; return 0; fi
  done
  return 0
}
NODE_CID=""
chain_q(){ # chain_q <dendrad query args...> -> JSON on stdout, or 1
  if [ -n "${DENDRA_REST:-}" ]; then
    case "$1" in
      status) curl -fsS -m 15 "${DENDRA_REST%/}/cosmos/base/tendermint/v1beta1/blocks/latest" ;;
      params) curl -fsS -m 15 "${DENDRA_REST%/}/dendra/jobs/v1/params" ;;
      audit-committee) curl -fsS -m 15 "${DENDRA_REST%/}/dendra/jobs/v1/audit_committee/$2" ;;
      list-commit) curl -fsS -m 60 "${DENDRA_REST%/}/dendra/jobs/v1/commit?pagination.limit=${3:-1000}${4:+&pagination.key=$4}" ;;
      *) return 1 ;;
    esac
    return
  fi
  [ -r "$NODE_KIT/docker-compose.yml" ] && [ -n "$NODE_CID" ] || return 1
  case "$1" in
    status) docker exec "$NODE_CID" dendrad status 2>/dev/null ;;
    params) docker exec "$NODE_CID" dendrad query jobs params -o json 2>/dev/null ;;
    audit-committee) docker exec "$NODE_CID" dendrad query jobs audit-committee "$2" -o json 2>/dev/null ;;
    list-commit) docker exec "$NODE_CID" dendrad query jobs list-commit --page-limit "${3:-1000}" ${4:+--page-key "$4"} -o json 2>/dev/null ;;
    *) return 1 ;;
  esac
}
judge_status(){
  local cid id hb js height j cid_why="no running miner container"
  command -v python3 >/dev/null 2>&1 || unmeasured "python3 is not installed on this host: the chain's answers are JSON, parsed and never pattern-matched."
  if ! cid="$(slot_cid 0 miner --running)"; then cid=""; cid_why="docker did not answer when the miner's container was looked up"; fi
  id=""
  if [ -n "$cid" ]; then
    hb="$(docker exec "$cid" cat "${DENDRA_STATUS_FILE:-/tmp/dendra-miner-status.json}" 2>/dev/null)"
    id="$(printf '%s' "$hb" | python3 -I -c 'import json,sys
try:
    print(json.load(sys.stdin).get("miner_id") or "")
except Exception:
    print("")' 2>/dev/null)"
    js="$(docker exec "$cid" cat "${DENDRA_JUDGE_STATE_FILE:-/tmp/dendra-judge-state.json}" 2>/dev/null)"
  fi
  [ -n "$id" ] || id="$(kitval MINER_ID)"
  case "$id" in dm1*) : ;; *) unmeasured "this kit's identity is not readable (no dm1... in the miner's heartbeat nor in $KIT/.env): nothing on chain can be attributed." ;; esac
  say "[judge] identity $id; kit says DENDRA_MINER_JUDGE=$(kitval DENDRA_MINER_JUDGE) (what was asked, not what was produced)"
  if [ -z "${DENDRA_REST:-}" ] && [ -r "$NODE_KIT/docker-compose.yml" ]; then
    NODE_CID="$(node_cid)" || NODE_CID=""
  fi
  if ! height="$(chain_q status)" || [ -z "$height" ]; then
    unmeasured "the chain could not be read: no running node container of the node kit in $NODE_KIT answering 'dendrad status', and no DENDRA_REST set (the API listener of a node of the network, http://host:1317)."
  fi
  height="$(printf '%s' "$height" | python3 -I -c 'import json,sys
d = json.load(sys.stdin)
h = (d.get("sync_info") or {}).get("latest_block_height") or (d.get("block") or {}).get("header", {}).get("height") or ""
print(h)' 2>/dev/null)"
  [ -n "$height" ] || unmeasured "the chain's height could not be read from its answer."
  say "[judge] chain read at height $height"
  # This identity's verdict commits, page by page, then the anchored jury of each audit (the last 50 at most: one
  # query per audit). A verdict outside the anchored jury is ignored on chain and is counted apart here.
  j="$(mktemp -d)" || unmeasured "no temporary directory."
  local key="" n=0 pages=0 pageout
  while :; do
    pageout="$(chain_q list-commit "" 1000 "$key")" || break
    printf '%s' "$pageout" > "$j/page.$pages"
    key="$(printf '%s' "$pageout" | python3 -I -c 'import json,sys
d = json.load(sys.stdin); p = d.get("pagination") or {}
print(p.get("next_key") or "")' 2>/dev/null)"
    pages=$((pages + 1))
    [ -n "$key" ] && [ "$key" != null ] || break
    [ "$pages" -lt 200 ] || break
  done
  [ "$pages" -gt 0 ] || { rm -rf "$j"; unmeasured "the commits could not be listed from the chain (list-commit)."; }
  python3 -I - "$j" "$id" "$pages" > "$j/verdicts.txt" <<'PY'
import json, sys
d, mid, pages = sys.argv[1], sys.argv[2], int(sys.argv[3])
suffix = "__verdict__" + mid
jobs = []
for i in range(pages):
    with open(f"{d}/page.{i}", encoding="utf-8") as f:
        doc = json.load(f)
    for c in doc.get("commit") or []:
        jid = str(c.get("job_id") or c.get("jobId") or "")
        if jid.endswith(suffix):
            jobs.append(jid[: -len(suffix)])
print("\n".join(jobs))
PY
  local total seated=0 unseated=0 unread=0 last_seat="" checked=0 job seat
  total="$(grep -c . "$j/verdicts.txt" 2>/dev/null)"; total="${total:-0}"
  for job in $(tail -n 50 "$j/verdicts.txt"); do
    checked=$((checked + 1))
    if seat="$(chain_q audit-committee "$job")"; then
      case "$(printf '%s' "$seat" | python3 -I -c 'import json,sys
d = json.load(sys.stdin); mid = sys.argv[1]
mem = d.get("members") or d.get("jurors") or d.get("miner_ids") or []
mem = [m if isinstance(m, str) else str(m.get("miner_id") or m.get("minerId") or "") for m in mem]
h = d.get("height") or d.get("anchored_height") or d.get("anchoredHeight") or ""
print(("seated" if mid in mem else "unseated") + " " + str(h))' "$id" 2>/dev/null)" in
        seated*) seated=$((seated + 1)); last_seat="$(printf '%s' "$seat" | python3 -I -c 'import json,sys
d = json.load(sys.stdin); print(d.get("height") or d.get("anchored_height") or d.get("anchoredHeight") or "")' 2>/dev/null)" ;;
        unseated*) unseated=$((unseated + 1)) ;;
        *) unread=$((unread + 1)) ;;
      esac
    else
      unread=$((unread + 1))
    fi
  done
  say "[judge] verdicts posted on chain by $id: $total commit(s) of the form <job>__verdict__$id (read at height $height)"
  if [ "$total" -gt 0 ]; then
    say "        of the last $checked: $seated on an audit whose anchored jury names this identity, $unseated outside the jury (ignored by the chain), $unread whose jury could not be read"
    [ -n "$last_seat" ] && say "        last seated verdict: its jury was anchored at height $last_seat (the commit itself carries no height; read at height $height)"
  fi
  rm -rf "$j"
  # The juror's own account: the judge state file in the miner's container (judge_worker.py::JudgeLedger).
  if [ -z "${cid:-}" ]; then
    say "[judge] abstentions by stage: not read ($cid_why)"
  elif [ -z "${js:-}" ]; then
    say "[judge] abstentions by stage: not read (no judge state in the miner's container: the judge worker has not started, or DENDRA_MINER_JUDGE is 0)"
  else
    printf '%s' "$js" | python3 -I -c 'import json, sys, time
# The keys are the ones JudgeLedger.doc writes. A key the file does not carry is "?" (not known), never 0 (known).
def num(v):
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else "?"
d = json.load(sys.stdin); c = d.get("counters")
model = d.get("model") or "?"
if not isinstance(c, dict):
    print("[judge] judge worker (its own log, model %s): counters not written yet" % model)
    sys.exit(0)
since = c.get("since"); ab = c.get("abstentions"); at = c.get("abstention_attempts"); an = c.get("verdicts_anchored")
since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)) if isinstance(since, int) and not isinstance(since, bool) and since > 0 else "?"
if not isinstance(an, dict):
    an = {}
print("[judge] judge worker (its own log, model %s, since %s):" % (model, since))
print("        anchored by it since then: valid %s, invalid %s; refused by the chain: %s" % (num(an.get("1")), num(an.get("0")), num(c.get("verdicts_refused"))))
# `abstentions` counts the audits the judge left without an answer, each once, at its last stage; `abstention_attempts`
# counts every attempt that ended without a judgment, retries included. The ledger writes only the stages that happened,
# so a stage missing from a dict that was read counted none; a dict that was not read is "?".
if not isinstance(ab, dict):
    print("        abstentions by stage: ?")
else:
    stages = sorted(set(ab) | set(at if isinstance(at, dict) else {}))
    if not stages:
        print("        abstentions by stage: none counted")
    for k in stages:
        line = "        abstention at stage %s: %s" % (k, num(ab[k]) if k in ab else "0")
        if isinstance(at, dict):
            line += " audit(s) left without an answer (%s attempt(s) without a judgment)" % (num(at[k]) if k in at else "0")
        print(line)' 2>/dev/null || say "[judge] abstentions by stage: the judge state could not be parsed"
  fi
  # The judge's memory, as the daemon's memory watch wrote it (heartbeat key judge_memory) and the juror's last load
  # (judge state, counters.last_load). Both come from the reads above, in the miner's container.
  if [ -z "${cid:-}" ]; then
    say "[judge] memory: not read ($cid_why)"
  else
    judge_memory_status "$hb" "$js" "$(kitval DENDRA_MINER_JUDGE)"
    judge_seat_status "$hb" "$(chain_q params 2>/dev/null)" "$KIT" "$(slot_run_hint 0 restart miner)"
  fi
}

# judge_seat_status <heartbeat text> <chain params text> <kit dir> <command that restarts the miner> -> the seat
# lines. The command is slot_run_hint's: a restart starts a new daemon, which may declare the judge again. The seat
# is what the chain holds about this judge, as the daemon's declaration loop last read it (heartbeat key judge_declaration, written by
# miner.py::JudgeSeat): a declaration, the height of the judge's last verdict, and the audits it was seated on
# without answering. The chain stops drawing a judge at judge_missed_draws_max of them (keeper/judge.go::
# eligibleAsJudgeAt); that parameter reads 0 as the compiled 3, so an absent one is 3, and one that was not read is
# "not read", never a limit. A key the files do not carry is "?", never a zero.
judge_seat_status(){
  local d
  d="$(mktemp -d)" || { say "[judge] seat: not read (no temporary directory)"; return 0; }
  printf '%s' "${1:-}" > "$d/hb"; printf '%s' "${2:-}" > "$d/params"
  python3 -I -c 'import json, sys, time
def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) else None
def whole(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0
def num(v):
    return "?" if not whole(v) else str(v)
hb, params, kit, restart = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3], sys.argv[4]
if hb is None:
    print("[judge] seat: not read (the daemon heartbeat could not be read)")
    sys.exit(0)
blk = hb.get("judge_declaration")
if not isinstance(blk, dict):
    print("[judge] seat: not read (the daemon heartbeat carries no judge_declaration: an older daemon, or one that has not written it yet)")
    sys.exit(0)
state = blk.get("state")
if state not in ("declared", "not_declared"):
    why = " ".join(str(blk.get("why") or ("the daemon state is " + str(state))).split())[:160]
    print("[judge] seat: not read (%s)" % why)
    sys.exit(0)
at = blk.get("at")
age = ("read %d s ago" % max(0, int(time.time()) - at)) if whole(at) and at > 0 else "read at an unreadable time"
if state == "not_declared":
    print("[judge] seat: the chain holds no judge declaration for this identity, so it is drawn into no jury (%s)" % age)
    sys.exit(0)
read = blk.get("read") if isinstance(blk.get("read"), dict) else {}
missed = read.get("missed_draws")
limit = None
p = params.get("params") if isinstance(params, dict) else None
if isinstance(p, dict):
    v = p.get("judge_missed_draws_max", 0)
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = None
    if v is not None and v >= 0:
        limit = v if v > 0 else 3
print("[judge] seat: declared at height %s, last verdict at height %s, missed draws %s of %s (%s)" % (
    num(read.get("declared_height")), num(read.get("last_verdict_height")), num(missed),
    "?" if limit is None else limit, age))
if not whole(missed):
    print("        the missed draws were not read, so whether the chain still draws this judge is not known")
elif limit is None:
    print("        the chain parameter judge_missed_draws_max was not read, so whether %s missed draw(s) exclude this judge is not known" % missed)
elif missed >= limit:
    print("        excluded from juries: seated on %s concluded audit(s) without an answer (judge_missed_draws_max %s)." % (missed, limit))
    print("        Remove what keeps it from answering (the memory lines above): the miner then declares the judge again by itself once its judge worker runs, its judge engine holds the model and the memory guard lets it load, which clears the count; its log says why it waits.")
    print("        If the miner already declared it again once and the count excluded it again with no answer, it waits for a restart of the miner: %s, or the Restart button on the Miner card of the Dendra app. Where the judge is not the whole role of the machine (judge.sh off refuses there), off then on does it too: bash %s/judge.sh off, wait until bash %s/judge.sh status says the chain holds no judge declaration, then bash %s/judge.sh on (the miner withdraws the declaration when it restarts off and declares the judge again when it restarts on, which clears the count)." % (restart, kit, kit, kit))
elif missed > 0:
    print("        %s more concluded audit(s) without an answer and the chain stops drawing this judge (an answer resets the count to 0)" % (limit - missed))' "$d/hb" "$d/params" "${3:-}" "${4:-}" 2>/dev/null || say "[judge] seat: the heartbeat could not be parsed"
  rm -rf "$d"
}

# judge_memory_status <heartbeat text> <judge state text> <DENDRA_MINER_JUDGE of the kit> -> the memory lines. The keys are
# the ones the memory watch writes (modea/memwatch.py, through miner.py) and the juror's ledger; a key the files do not
# carry is "?" (not known), never a zero. Three answers about the block: read, absent (the judge is not requested, or the
# daemon is older than the watch), unreadable.
judge_memory_status(){
  local d
  d="$(mktemp -d)" || { say "[judge] memory: not read (no temporary directory)"; return 0; }
  printf '%s' "${1:-}" > "$d/hb"; printf '%s' "${2:-}" > "$d/js"
  python3 -I -c 'import json, sys, time
def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) else None
def whole(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0
def num(v):
    return str(v) if whole(v) else "?"
def mb(v):
    return (str(v) + " MB") if whole(v) else "?"
def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if whole(t) and t > 0 else "?"
def flt(v):
    return ("%g" % v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
hb, js, asked = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3] == "1"
if hb is None:
    print("[judge] memory: not read (the daemon heartbeat could not be read)")
    sys.exit(0)
if "judge_memory" not in hb:
    if asked:
        print("[judge] memory: not read (the judge is requested and the daemon heartbeat carries no judge_memory: a daemon older than the memory watch, or one that has not written it yet)")
    else:
        print("[judge] memory: the judge is not requested in this kit, so no memory watch runs")
    sys.exit(0)
jm = hb["judge_memory"]
if not isinstance(jm, dict):
    print("[judge] memory: not read (judge_memory in the daemon heartbeat is not an object)")
    sys.exit(0)
gate = jm.get("gate") if jm.get("gate") in ("open", "closed") else "?"
why = " ".join(str(jm.get("gate_why") or "").split())[:160]
res = jm.get("resident")
loaded = "yes" if res is True else ("no" if res is False else "?")
src = jm.get("footprint_source") if isinstance(jm.get("footprint_source"), str) else "?"
c = js.get("counters") if isinstance(js, dict) else None
ll = c.get("last_load") if isinstance(c, dict) else None
if not isinstance(c, dict):
    last = "?"
elif not isinstance(ll, dict):
    last = "none recorded"
else:
    last = "%s s at %s" % (flt(ll.get("load_s")) or "?", iso(ll.get("at")))
mode = jm.get("mode") if jm.get("mode") in ("resident", "on-demand") else "?"
ceiling = jm.get("ceiling") if isinstance(jm.get("ceiling"), str) and jm.get("ceiling") else "not set"
print("[judge] memory: mode %s (ceiling %s) since %s; gate %s%s; model loaded: %s; footprint %s (%s); last load %s; unloads %s, out-of-memory %s" % (
    mode, ceiling, iso(jm.get("mode_since")), gate, (" (" + why + ")") if why else "", loaded, mb(jm.get("footprint_mb")), src, last,
    num(jm.get("unloads")), num(jm.get("oom_seen"))))
at = jm.get("at")
age = ("%d s ago" % max(0, int(time.time()) - at)) if whole(at) and at > 0 else "at an unreadable time"
psi = [flt(jm.get("psi_some_avg10")), flt(jm.get("psi_full_avg10"))]
psi_text = ("pressure some/full avg10 %s/%s" % tuple(psi)) if None not in psi else "pressure not read (this kernel gives none, or the watch could not read it)"
print("        read %s; memory available %s of %s; swap used %s of %s; %s" % (
    age, mb(jm.get("mem_available_mb")), mb(jm.get("mem_total_mb")), mb(jm.get("swap_used_mb")), mb(jm.get("swap_total_mb")), psi_text))
if isinstance(ll, dict):
    print("        last load: memory available %s before, %s after; the engine reports %s" % (
        mb(ll.get("available_before_mb")), mb(ll.get("available_after_mb")), mb(ll.get("engine_size_mb"))))
pu = jm.get("pause_until")
if whole(pu) and pu > time.time():
    print("        paused until %s: the guard waits before it tries to load the model again" % iso(pu))' "$d/hb" "$d/js" "${3:-}" 2>/dev/null || say "[judge] memory: the heartbeat could not be parsed"
  rm -rf "$d"
}

case "$VERB" in on) judge_on ;; off) judge_off ;; status) judge_status ;; esac
