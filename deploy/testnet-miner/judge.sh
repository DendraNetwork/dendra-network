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
# what it downloads before it pulls. The desktop application and the HiveOS package call this file.
# A kit with one identity per card (deploy/join.sh --gpus) carries one env per slot: there the switch is a re-run
# of deploy/join.sh with --judge or --no-judge, and this file says so.
# Every compose command goes through slots.sh (slot_compose 0, the kit's own copy), and the node kit is read with
# `docker exec` on the container its compose labels name: a tool of the miner kit never runs compose itself, since
# a command typed here would act on slot 0's identity whatever slot was meant (dendra_slot_compose_garde.sh).
# Exit codes: 0 done · 1 a step failed · 2 refused (the gate, the disk, the role on the CPU, a slot kit) · 3 not
# measured (no docker, no python3, nothing to read the chain with).
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
KIT="${DENDRA_MINER_KIT:-$SELF}"
REPO="$(cd "$KIT/../.." 2>/dev/null && pwd)"
NODE_KIT="$REPO/deploy/testnet-node"
PROBE="$REPO/deploy/hw_probe.sh"
VERB="${1:-}"
say(){ printf '%s\n' "$*"; }
refuse(){ printf '[judge] refused: %s\n' "$*" >&2; exit 2; }
unmeasured(){ printf '[judge] not measured: %s\n' "$*" >&2; exit 3; }
failed(){ printf '[judge] FAILED: %s\n' "$*" >&2; exit 1; }
kitval(){ [ -r "$KIT/.env" ] || return 0; tr -d '\r' < "$KIT/.env" | sed -n "s/^$1=//p" | head -1; }
# env_set <KEY> <value> [unset] -- one line of the kit's .env, in place (the same edit deploy/join.sh::_miner_env_set makes).
env_set(){
  local f="$KIT/.env" t
  t="$(mktemp)" || return 1
  awk -v k="$1" -v v="$2" -v u="${3:-}" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s && u == "") print k "=" v; s = 1; next }
    { print }
    END { if (!s && u == "") print k "=" v }' "$f" > "$t" && cat "$t" > "$f"
  rm -f "$t"
}
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
  local can rc m free floor jpath droot size
  [ "$CPU_ROLE" = 1 ] && { say "[judge] this machine judges on the CPU as its whole role (no usable GPU): the judge is already its role, nothing to switch."; exit 0; }
  [ -r "$PROBE" ] || refuse "the hardware probe is not in this clone ($PROBE): whether this machine can judge cannot be read, and an unread gate seats nothing."
  can="$(bash "$PROBE" --can-judge 2>/dev/null | head -1 | tr -d '[:space:]')"; rc=$?
  case "$rc:$can" in
    0:true) say "[judge] this machine can judge (deploy/hw_probe.sh --can-judge: true)." ;;
    0:false) refuse "this machine cannot judge (deploy/hw_probe.sh --can-judge: false): the judge runs the network's judge model on the CPU and needs MOE_CPU_MIN_RAM_MB of system RAM (bash deploy/hw_probe.sh --judge-floor-mb prints it). Seating it anyway would make it a mute seat." ;;
    *) refuse "whether this machine can judge is unknown (deploy/hw_probe.sh --can-judge: exit $rc, '${can:-no answer}'): its RAM was not read, and an unread gate seats no judge. Run bash deploy/hw_probe.sh to see what it reads." ;;
  esac
  m="$(kitval DENDRA_JUDGE_MODEL_ID)"
  [ -n "$m" ] || m="$(bash "$PROBE" --json --no-docker 2>/dev/null | grep -o '"judge_model":"[^"]*"' | cut -d'"' -f4)"
  [ -n "$m" ] || m="qwen3:30b-a3b-instruct-2507-q4_K_M"
  size="$(sed -n 's/.*judge model .*(\(~[0-9][0-9]* GB\)[,)].*/\1/p' "$KIT/docker-compose.yml" 2>/dev/null | head -1)"
  # The disk, before the pull: a first seating lands the model under Docker's data root (the Windows drive under
  # WSL). A kit that already seats the judge has pulled it, and the free-space figure counts it as used.
  if [ "$(kitval DENDRA_MINER_JUDGE)" != 1 ]; then
    floor="${DENDRA_MIN_DISK_GB_JUDGE:-32}"
    if grep -qi microsoft /proc/version 2>/dev/null; then jpath="${DENDRA_WSL_HOST_MOUNT:-/mnt/c}"
    else droot="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"; jpath="$droot"; fi
    free=""; [ -n "$jpath" ] && free="$(df -BG --output=avail "$jpath" 2>/dev/null | tail -1 | tr -dc '0-9')"
    [ -n "$free" ] || refuse "free disk where the judge model lands could not be measured (${jpath:-docker info named no data root}): the seat is not taken on an unread floor (DENDRA_MIN_DISK_GB_JUDGE, ${floor} GB)."
    [ "$free" -ge "$floor" ] 2>/dev/null || refuse "free disk ${free}G under $jpath is below the judge floor of ${floor} GB (DENDRA_MIN_DISK_GB_JUDGE, deploy/install.sh): the judge model $m (${size:-size as the compose file states it}) would land there. Free space first."
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
  say "[judge] $KIT/.env: DENDRA_MINER_JUDGE=1, DENDRA_JUDGE_ENDPOINT=http://ollama-cpu:11434, COMPOSE_PROFILES=judge, DENDRA_NO_JUDGE=0."
  say "[judge] recreating the miner's container only (its judge and reveal workers start inside it)."
  slot_compose 0 --profile judge up -d --no-deps --force-recreate miner || failed "the miner's container was not recreated: $(slot_run_hint 0 --profile judge up -d --no-deps --force-recreate miner)"
  say "[judge] on. Verdicts appear on chain once the chain draws this identity into a jury: bash $KIT/judge.sh status"
}

# ---------------------------------------------------------------- off
judge_off(){
  [ "$CPU_ROLE" = 1 ] && refuse "this machine judges on the CPU as its whole role (no usable GPU, deploy/hw_probe.sh --role: judge): without the judge it has no role on the testnet. To leave, read bash $KIT/exit-miner.sh --help."
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
  say "[judge] off. The CPU engine is stopped and the judge model stays on disk. On the epoch 14 chain this miner is still drawn into juries, where it never votes (a mute seat); on the epoch 15 chain a seat needs a judge declaration, which the restarted miner withdraws once it reads that the chain holds one (its log says what it read)."
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
      audit-committee) curl -fsS -m 15 "${DENDRA_REST%/}/dendra/jobs/v1/audit_committee/$2" ;;
      list-commit) curl -fsS -m 60 "${DENDRA_REST%/}/dendra/jobs/v1/commit?pagination.limit=${3:-1000}${4:+&pagination.key=$4}" ;;
      *) return 1 ;;
    esac
    return
  fi
  [ -r "$NODE_KIT/docker-compose.yml" ] && [ -n "$NODE_CID" ] || return 1
  case "$1" in
    status) docker exec "$NODE_CID" dendrad status 2>/dev/null ;;
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
d = json.load(sys.stdin); c = d.get("counters") or {}
since = c.get("since"); ab = c.get("abstentions") or {}; an = c.get("anchored") or {}
print("[judge] judge worker (its own log, model %s, since %s):" % (d.get("model") or "?", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(since))) if since else "?"))
print("        anchored by it since then: valid %s, invalid %s; refused by the chain: %s" % (an.get("1", 0), an.get("0", 0), c.get("refused", 0)))
if ab:
    for k in sorted(ab):
        print("        abstention at stage %s: %s" % (k, ab[k]))
else:
    print("        abstentions by stage: none counted")' 2>/dev/null || say "[judge] abstentions by stage: the judge state could not be parsed"
  fi
}

case "$VERB" in on) judge_on ;; off) judge_off ;; status) judge_status ;; esac
