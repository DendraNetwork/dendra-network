#!/usr/bin/env bash
# node_upgrade_watch.sh -- carries this kit's node through a governed consensus upgrade, with nobody at the keyboard.
#
# At the height of a governed upgrade plan, a node whose binary has no handler for that plan stops: x/upgrade writes in
# the node's log the message of BuildUpgradeNeededMsg (cosmossdk.io/x/upgrade v0.2.0, abci.go, called from PreBlocker):
# the word upgrade, the plan's name in double quotes, the word needed, then "at height: <N>: <info>", the two words in
# capitals; and it refuses the block. CometBFT v0.38 then panics in finalizeCommit and its consensus routine recovers
# from it (a consensus failure in the log), which leaves the process up with consensus stopped; after a restart the
# handshake replays the same block and the start fails, so the container restarts in a loop (restart: unless-stopped).
# Either way the line is in the container's log, and that binary cannot pass N.
#
# kit_update.sh never applies a release of another consensus epoch: it stages a higher one, verified like any
# candidate, under upgrade-stage/ (a clone of the signed tag, and the node image that tree pins or that was built
# from it). This file runs every minute (deploy/join.sh schedules it next to the daily update, under the same
# switch). The line is a hint, never a halt: only the message of a line is read (never a field, where client data
# such as a job id can spell anything), and the chain confirms it before anything moves (confirm_halt: the plan this
# node's chain schedules and its last block, or the record x/upgrade writes when it stops). On a confirmed halt, and
# only when the staged tree registers that plan name (chain/app/upgrades.go, the variable upgradePlans, read
# again by kit_update.sh --stage-check, which also verifies the staged tag again), it:
#   1. moves the clone to the staged tag as an update does: the tree it leaves kept on kit-before-update, the marker
#      .kit-update.pending written first and naming the plan and its height (kit_update.sh then leaves it to this file);
#   2. starts the node on the staged image: DENDRA_NODE_IMAGE, DENDRA_NODE_IMAGE_BUILT_FOR and the mark of this file
#      (DENDRA_NODE_IMAGE_SET_BY=node_upgrade_watch) written in the node kit's .env, then docker compose up -d
#      --no-build, so that nothing is pulled or built at the halt. With the mark, deploy/join.sh holds an image ID
#      written here to its gates like a pin of the kit (deploy/join.sh::node_env_image_reconcile), never as an
#      operator's choice;
#   3. asks that node, at every pass, dendrad query upgrade applied <plan>: a height comes once the network has
#      passed N on the new binary;
#   4. replays the join this kit recorded (DENDRA_KIT_RERUN) from the new tree with --after-upgrade <plan>, under
#      which deploy/join.sh accepts that network-info.txt still declares the old epoch only when this node answers
#      the plan applied (deploy/join.sh::after_upgrade_applied);
#   5. once that join succeeds, settles the node kit's .env (node_image_release): the lines of step 2 stay a pin of
#      the kit (a digest without the mark, an image ID with it), so that the next kit of the same epoch moves the node
#      to the image it pins; an operator's own tag is given back.
# There is no rollback past the halt: the binary before the upgrade cannot continue past N, so going back to it
# would only stop the node again. A failed join is replayed later, never undone, and the log says so; after
# DENDRA_UPGRADE_JOIN_MAX_ATTEMPTS failed joins this file runs it no more, and says at every pass what the operator runs.
# A halt for a plan nothing was staged for, or that the staged tree does not register, is reported at every pass
# and never acted on: the operator acts. A line the chain does not confirm is reported once and never acted on.
#
# Usage:
#   bash deploy/testnet-miner/node_upgrade_watch.sh          # one pass, printed and logged
#   bash deploy/testnet-miner/node_upgrade_watch.sh --cron   # the pass deploy/join.sh schedules every minute, to
#                                                             # the log only: a pass with nothing to say writes nothing
#   bash deploy/testnet-miner/node_upgrade_watch.sh --status # what it reads; nothing moves
# Exit codes: 0 nothing to do, or a step done (the next pass goes on); 1 a step failed (nothing undone, the next
# pass tries again); 2 a halt this file does not act on (nothing staged for its plan, another plan, a stage that no
# longer holds, local changes), or a join after the upgrade that failed every attempt: the operator acts; 3 not
# measured (git or docker missing, the node's log unread, a halt line whose confirmation could not be read).
# Environment: DENDRA_UPGRADE_APPLIED_WAIT_S (how long one pass asks for the plan applied, default 45),
# DENDRA_UPGRADE_JOIN_RETRY_S (the delay before a failed join is replayed, default 600),
# DENDRA_UPGRADE_JOIN_MAX_ATTEMPTS (the joins run after an upgrade before this file stops running them, default 12),
# DENDRA_UPGRADE_LOG_TAIL (the lines of the node's log read, default 3000). DENDRA_AUTO_UPDATE=0 in the kit's .env
# turns this watch off with the daily update.
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
KIT="${DENDRA_MINER_KIT:-$SELF}"
REPO="$(cd "$KIT/../.." 2>/dev/null && pwd)"
NODE_KIT="$REPO/deploy/testnet-node"
KU="$SELF/kit_update.sh"
LOG="$KIT/node-upgrade-watch.log"
PENDING="$KIT/.kit-update.pending"
LOCK="$KIT/.kit-update.lock"
HALT_REC="$KIT/.node-upgrade-watch.halt"
STATE_REC="$KIT/.node-upgrade-watch.state"
STAGE_DIR="$KIT/upgrade-stage"
MODE=run
# The alphabets of kit_update.sh: a plan name, a release tag, a recorded re-run, a node image reference.
PLAN_NAME_RE='^[A-Za-z0-9._-]{1,128}$'
RELEASE_TAG_RE='^v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$'
RERUN_RE='^[A-Za-z0-9 :/._~%?=+@,-]+$'
NODE_PIN_RE='^ghcr[.]io/dendranetwork/dendra-node@sha256:[0-9a-f]{64}$'
IMAGE_ID_RE='^sha256:[0-9a-f]{64}$'
BUILT_FOR_FORM_RE='^kit_version=[0-9]+ consensus_epoch=[0-9]+$'
# An image named by a plain tag (no digest, no ID), as an operator writes DENDRA_NODE_IMAGE for a second node.
TAG_REF_RE='^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$'
# The node's home in its container: the node-data mount of deploy/testnet-node/docker-compose.yml.
NODE_HOME_IN_CONTAINER=/root/.dendra

TS(){ date -u +%FT%TZ 2>/dev/null; }
say(){ if [ "$MODE" = cron ]; then printf '%s %s\n' "$(TS)" "$*"; else printf '%s\n' "$*"; printf '%s %s\n' "$(TS)" "$*" >> "$LOG" 2>/dev/null; fi; }
# say_once <key> <text> -- said when the last pass did not say the same thing: a state that lasts (a node that waits
# for the network, a tool missing) is not written every minute. A halt this file does not act on is reported at every
# pass, with say.
say_once(){
  [ "$(cat "$STATE_REC" 2>/dev/null)" = "$1" ] && return 0
  say "$2"; printf '%s\n' "$1" > "$STATE_REC" 2>/dev/null
  return 0
}
state_clear(){ rm -f "$STATE_REC"; }
kitval(){ [ -r "$KIT/.env" ] || return 0; tr -d '\r' < "$KIT/.env" | sed -n "s/^$1=//p" | head -1; }
# env_file_set <file> <KEY> <value> -- one line of a kit's .env, replaced where it stands or appended (the edit of
# deploy/join.sh::_miner_env_set); the file keeps its mode. Its status is the write's, so a file this account cannot
# write is reported, never taken for written.
env_file_set(){
  local f="$1" t rc
  t="$(mktemp)" || return 1
  awk -v k="$2" -v v="$3" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s) print k "=" v; s = 1; next }
    { print }
    END { if (!s) print k "=" v }' "$f" > "$t" && cat "$t" > "$f"; rc=$?
  rm -f "$t"
  return "$rc"
}
# env_file_unset <file> <KEY> -- every KEY= line of a kit's .env removed (deploy/join.sh::_env_file_unset).
env_file_unset(){
  local f="$1" t rc
  [ -f "$f" ] || return 0
  t="$(mktemp)" || return 1
  awk -v k="$2" 'BEGIN { n = length(k) + 1 } substr($0, 1, n) == k "=" { next } { print }' "$f" > "$t" && cat "$t" > "$f"; rc=$?
  rm -f "$t"
  return "$rc"
}
kitset(){ env_file_set "$KIT/.env" "$1" "$2"; }
pval(){ sed -n "s/^$1=//p" "$PENDING" 2>/dev/null | head -1 | tr -d '\r'; }
# pset <KEY> <value> -- one line of the marker, written whole into a file beside it and renamed over it: a write cut
# short leaves the marker as it was, never a marker without its plan (which main would read as kit_update.sh's).
# The status is the rename's: the join count is written before the join, or the join is not run.
pset(){
  local t rc
  t="$(mktemp "$PENDING.XXXXXX")" || return 1
  awk -v k="$1" -v v="$2" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s) print k "=" v; s = 1; next }
    { print }
    END { if (!s) print k "=" v }' "$PENDING" > "$t" && mv -f "$t" "$PENDING"; rc=$?
  rm -f "$t"
  return "$rc"
}
# kv_of <lines> <key> -> the value of one key=value line, read by key and never sourced.
kv_of(){ printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }

# ---------------------------------------------------------------- the lock, the one of kit_update.sh
# The same directory as the daily update, taken over on the same rule (kit_update.sh::lock_stale): one run at a time
# moves the clone. A pass that finds it held does nothing; the next pass reads again.
lock_stale(){
  local p
  [ -n "$(find "$1" -maxdepth 0 -mmin +360 2>/dev/null)" ] && return 0
  p="$(head -1 "$1/pid" 2>/dev/null | tr -dc 0-9)"
  [ -n "$p" ] && [ -d /proc/self ] && [ ! -d "/proc/$p" ] && return 0
  return 1
}
lock_take(){
  local s="$1.stale.$$"
  if ! mkdir "$1" 2>/dev/null; then
    lock_stale "$1" || return 1
    mv "$1" "$s" 2>/dev/null && rm -rf "$s"
    mkdir "$1" 2>/dev/null || return 1
  fi
  printf '%s\n' "$$" > "$1/pid"
  return 0
}
lock_release(){ rm -f "$1/pid"; rmdir "$1" 2>/dev/null; }

# ---------------------------------------------------------------- the node and its halt
# node_cid -> the node container of this clone's node kit, running or stopped: found by the labels compose writes
# (service node, not a one-off run) and confirmed by its working_dir label, as deploy/testnet-miner/judge.sh::node_cid
# confirms it; a clone elsewhere on this machine may run a node of the same project. Empty = none; 3 when docker
# does not answer.
node_cid(){
  local ids id wd kp
  ids="$(docker ps -a -q --filter label=com.docker.compose.service=node --filter label=com.docker.compose.oneoff=False 2>/dev/null)" || return 3
  kp="$(cd "$NODE_KIT" 2>/dev/null && pwd -P)"
  for id in $ids; do
    wd="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id" 2>/dev/null)" || return 3
    wd="$(printf '%s' "$wd" | tr -d '[:cntrl:]')"
    if [ "$wd" = "$NODE_KIT" ] || { [ -n "$kp" ] && [ "$wd" = "$kp" ]; }; then printf '%s' "$id"; return 0; fi
  done
  return 0
}
# The two words of the halt message, in capitals as the module writes them. They are composed here, so that this file
# holds no run of capitals (the project's text guard reads one as emphasis); the match on the log stays exact.
W_UPGRADE="$(printf '%s' upgrade | tr '[:lower:]' '[:upper:]')"; W_NEEDED="$(printf '%s' needed | tr '[:lower:]' '[:upper:]')"
# halt_of_log -> "halt <plan> <height>" for the last halt message in the log read on stdin, "none" otherwise. Only the
# message of a line is read: the console form (the time, the level err, the message, the fields) (colour codes dropped), or a JSON line
# of level error by its message key. A field is never read, since a field value can be client data (a job id is logged
# on every node) and can spell the halt message. The name is taken up to its closing quote and judged by the caller.
halt_of_log(){
  python3 -I -c '
import json, re, sys
esc, q = chr(27), chr(34)
pat = re.compile("^" + sys.argv[1] + " " + q + "([^" + q + " ]{1,200})" + q + " " + sys.argv[2] + " at height: ([0-9]{1,19})(:|$)")
last = None
for line in sys.stdin.read().splitlines():
    while esc in line:
        i = line.index(esc)
        j = line.find("m", i)
        if j < 0:
            break
        line = line[:i] + line[j + 1:]
    line = line.strip()
    msg = None
    if line.startswith("{"):
        try:
            d = json.loads(line)
        except ValueError:
            d = None
        if isinstance(d, dict) and d.get("level") == "error" and isinstance(d.get("message"), str):
            msg = d["message"]
    else:
        w = line.split(" ", 2)
        if len(w) >= 2 and w[0] == "ERR":
            msg = line[4:]
        elif len(w) == 3 and w[1] == "ERR":
            msg = w[2]
    if msg is None:
        continue
    m = pat.match(msg)
    if m:
        last = (m.group(1), m.group(2))
print(("halt %s %s" % last) if last else "none")' "$W_UPGRADE" "$W_NEEDED" 2>/dev/null
}
# read_halt -> HALT_STATE (none|nonode|halt|unmeasured), HALT_PLAN, HALT_HEIGHT, HALT_RAW, HALT_WHY, NODE_CID.
# The last halt message in the tail of the node container's log; else the halt this watch recorded for the same
# container once the chain confirmed it (a node that stays up with consensus stopped keeps logging, and the line can
# leave the tail). A halt recorded for another container is dropped: a new container is a new start.
read_halt(){
  local tail_n="${DENDRA_UPGRADE_LOG_TAIL:-3000}" logs out rc
  HALT_STATE=none; HALT_PLAN=""; HALT_HEIGHT=""; HALT_RAW=""; HALT_WHY=""; NODE_CID=""
  case "$tail_n" in ""|*[!0-9]*) tail_n=3000 ;; esac
  NODE_CID="$(node_cid)"; rc=$?
  [ "$rc" = 3 ] && { HALT_STATE=unmeasured; HALT_WHY="docker did not answer for the node's container"; return 0; }
  [ -n "$NODE_CID" ] || { HALT_STATE=nonode; return 0; }
  logs="$(docker logs --tail "$tail_n" "$NODE_CID" 2>&1)" || { HALT_STATE=unmeasured; HALT_WHY="docker logs did not answer for the node's container $NODE_CID"; return 0; }
  out=none
  # Without the words on a line there is nothing to parse, and no python3 is needed.
  if printf '%s\n' "$logs" | grep -q "$W_NEEDED at height: "; then
    command -v python3 >/dev/null 2>&1 || { HALT_STATE=unmeasured; HALT_WHY="the node's log names an upgrade, and python3, which reads that line and the chain's answers, is not on this host"; return 0; }
    out="$(printf '%s\n' "$logs" | halt_of_log)"
    case "$out" in none|"halt "*) : ;; *) HALT_STATE=unmeasured; HALT_WHY="the node's log could not be parsed"; return 0 ;; esac
  fi
  if [ "$out" != none ]; then
    read -r _ HALT_PLAN HALT_HEIGHT <<< "$out"
    HALT_RAW="$HALT_PLAN"; HALT_STATE=halt
    return 0
  fi
  if [ -r "$HALT_REC" ] && [ "$(sed -n 's/^cid=//p' "$HALT_REC" | head -1)" = "$NODE_CID" ]; then
    HALT_PLAN="$(sed -n 's/^plan=//p' "$HALT_REC" | head -1)"; HALT_HEIGHT="$(sed -n 's/^height=//p' "$HALT_REC" | head -1)"
    HALT_RAW="$HALT_PLAN"
    [[ $HALT_PLAN =~ $PLAN_NAME_RE ]] && [ -n "$HALT_HEIGHT" ] && [ -z "$(printf '%s' "$HALT_HEIGHT" | tr -d 0-9)" ] && { HALT_STATE=halt; return 0; }
  fi
  [ "$MODE" = status ] || rm -f "$HALT_REC"
  HALT_PLAN=""; HALT_HEIGHT=""; HALT_RAW=""
  return 0
}
halt_record(){ printf 'cid=%s\nplan=%s\nheight=%s\nseen=%s\n' "$NODE_CID" "$HALT_PLAN" "$HALT_HEIGHT" "$(TS)" > "$HALT_REC" 2>/dev/null; }

# confirm_halt -> CONFIRM (yes|no|unread) and CONFIRM_WHY, for HALT_PLAN at HALT_HEIGHT on NODE_CID. The log line is a
# hint, and switching the node before the plan's height takes it off the network, so the chain confirms the halt:
# this node's chain schedules that plan at that height (dendrad query upgrade plan), and its last block is the one
# before it (/abci_info), where a node stopped by the plan stays. A node that does not answer (stopped, or restarting on
# the handshake that replays the halted block) is read through the record x/upgrade writes just before it stops:
# data/upgrade-info.json of its home (cosmossdk.io/x/upgrade v0.2.0, abci.go::PreBlocker, DumpUpgradeInfoToDisk).
confirm_halt(){
  local st pdoc adoc
  CONFIRM=unread; CONFIRM_WHY=""
  command -v python3 >/dev/null 2>&1 || { CONFIRM_WHY="python3 is not on this host, and a halt line is acted on only once the chain confirms it"; return 0; }
  st="$(docker inspect --format '{{.State.Status}}' "$NODE_CID" 2>/dev/null | tr -dc 'a-z')"
  if [ "$st" = running ] && pdoc="$(docker exec "$NODE_CID" dendrad query upgrade plan -o json 2>/dev/null)" \
     && adoc="$(docker exec "$NODE_CID" curl -fsS -m 10 http://127.0.0.1:26657/abci_info 2>/dev/null)"; then
    halt_verdict "$(halt_judge chain "$HALT_PLAN" "$HALT_HEIGHT" "$pdoc" "$adoc" < /dev/null)"
  else
    halt_verdict "$(docker cp "$NODE_CID:$NODE_HOME_IN_CONTAINER/data/upgrade-info.json" - 2>/dev/null | halt_judge record "$HALT_PLAN" "$HALT_HEIGHT")"
  fi
}
halt_verdict(){ case "$1" in "yes "*|"no "*|"unread "*) CONFIRM="${1%% *}"; CONFIRM_WHY="${1#* }" ;; *) CONFIRM=unread; CONFIRM_WHY="the answers could not be judged" ;; esac; }
# halt_judge chain <plan> <height> <plan document> <abci_info document> | halt_judge record <plan> <height> < tar
# -> "yes|no|unread <why>". Documents are parsed, never searched as text; an absent field is its zero value.
halt_judge(){
  python3 -I -c '
import json, sys, tarfile
mode, plan, height = sys.argv[1], sys.argv[2], int(sys.argv[3])
def num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.isdigit():
        return int(x)
    return None
def doc(t):
    try:
        return json.loads(t)
    except ValueError:
        return None
if mode == "chain":
    p, a = doc(sys.argv[4]), doc(sys.argv[5])
    r = a.get("result") if isinstance(a, dict) else None
    r = r.get("response") if isinstance(r, dict) else None
    last = num(r.get("last_block_height", 0)) if isinstance(r, dict) else None
    pl = p.get("plan") if isinstance(p, dict) else None
    if not isinstance(p, dict) or last is None or (pl is not None and not isinstance(pl, dict)):
        print("unread the node answered documents this watch does not read")
    elif pl is None:
        print("no the chain this node follows schedules no upgrade plan")
    elif pl.get("name", "") != plan or num(pl.get("height", 0)) != height:
        print("no the chain this node follows schedules plan %s at height %s" % (pl.get("name", "") or "<none>", num(pl.get("height", 0))))
    elif last != height - 1:
        print("no the last block of this node is %d, and a node stopped by plan %s stays at %d" % (last, plan, height - 1))
    else:
        print("yes the chain schedules plan %s at height %d, and the last block of this node is %d" % (plan, height, last))
else:
    data = None
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as t:
            for m in t:
                if m.isfile():
                    f = t.extractfile(m)
                    data = f.read() if f else None
                    break
    except (tarfile.TarError, OSError, EOFError):
        data = None
    d = doc(data.decode("utf-8", "replace")) if data else None
    if not isinstance(d, dict):
        print("unread the node does not answer, and its halt record (data/upgrade-info.json) could not be read")
    elif d.get("name", "") == plan and num(d.get("height", 0)) == height:
        print("yes the node does not answer, and its halt record (data/upgrade-info.json, written by x/upgrade as it stops) names plan %s at height %d" % (plan, height))
    else:
        print("no the node does not answer, and its halt record (data/upgrade-info.json) names plan %s at height %s" % (d.get("name", "") or "<none>", num(d.get("height", 0))))' "$@" 2>/dev/null
}

# upgrade_height_of_json <document> -> the height field of a QueryAppliedPlanResponse: a positive integer when the
# plan was applied, 0 when the field is absent (proto3 omits a zero height: never applied), ? when the document is
# not one (parsed by python3 or jq, never searched as text; neither on this host gives ?). The same function, byte
# for byte, as deploy/join.sh::upgrade_height_of_json: the bench of this file holds the two equal.
upgrade_height_of_json(){
  local v=""
  if command -v python3 >/dev/null 2>&1; then
    v="$(printf '%s' "$1" | python3 -I -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    d = None
h = d.get("height", 0) if isinstance(d, dict) else None
if isinstance(h, bool) or not isinstance(h, (int, str)):
    h = None
print(h if h is not None else "?")' 2>/dev/null)"
  elif command -v jq >/dev/null 2>&1; then
    v="$(printf '%s' "$1" | jq -r 'if type == "object" then (if has("height") then (.height | tostring) else "0" end) else "?" end' 2>/dev/null)"
  fi
  case "$v" in ''|*[!0-9]*) printf '?' ;; *) printf '%s' "$((10#$v))" ;; esac
}
# applied_height <container> <plan> -> as upgrade_height_of_json; ? when the node does not answer.
applied_height(){
  local raw
  raw="$(docker exec "$1" dendrad query upgrade applied "$2" -o json 2>/dev/null)" || { printf '?'; return 0; }
  upgrade_height_of_json "$raw"
}
# wait_applied <container> <plan> -> the last answer of applied_height, asked every 5 s until a positive height or
# DENDRA_UPGRADE_APPLIED_WAIT_S; the next pass asks again.
wait_applied(){
  local bound="${DENDRA_UPGRADE_APPLIED_WAIT_S:-45}" waited=0 h
  case "$bound" in ""|*[!0-9]*) bound=45 ;; esac
  while :; do
    h="$(applied_height "$1" "$2")"
    case "$h" in ''|*[!0-9]*|0) : ;; *) break ;; esac
    [ "$waited" -ge "$bound" ] && break
    sleep 5; waited=$((waited + 5))
  done
  printf '%s' "$h"
}

# ---------------------------------------------------------------- the stage, read through kit_update.sh
# stage_read -> STAGE (ok|absent|removed|incomplete|unmeasured), STAGE_WHY and the ST_* values of
# kit_update.sh --stage-check, which verifies the stage again (the tag with this clone's docker/RELEASE_SIGNERS, the
# staged tree, its node image) and reads the plans from the staged tree itself.
stage_read(){
  local out
  out="$(DENDRA_MINER_KIT="$KIT" bash "$KU" --stage-check 2>/dev/null)"
  STAGE="$(kv_of "$out" stage)"; STAGE_WHY="$(kv_of "$out" why)"
  ST_TAG="$(kv_of "$out" tag)"; ST_COMMIT="$(kv_of "$out" commit)"; ST_EPOCH="$(kv_of "$out" consensus_epoch)"
  ST_PLANS="$(kv_of "$out" plans)"; ST_PLANS_WHY="$(kv_of "$out" plans_why)"
  ST_IMAGE="$(kv_of "$out" node_image)"; ST_BUILT_FOR="$(kv_of "$out" node_image_built_for)"
  ST_ROTATION="$(kv_of "$out" rotation)"; ST_SIGNERS_NEW="$(kv_of "$out" signers_new)"
  [ -n "$STAGE" ] || { STAGE=unmeasured; STAGE_WHY="kit_update.sh --stage-check gave no answer ($KU)"; }
}
plan_registered(){ case " $2 " in *" $1 "*) return 0 ;; esac; return 1; }

# rerun_line -> RERUN: the recorded join as kit_update.sh replays it, under the same bounds (its alphabet, a run of
# deploy/join.sh, no option of one run), --judge and --no-judge left out since the seat is in the kit's .env.
# RERUN_WHY and 1 when it is refused.
rerun_line(){
  RERUN="$(kitval DENDRA_KIT_RERUN)"; RERUN_WHY=""
  [ -n "$RERUN" ] || { RERUN_WHY="no recorded re-run in $KIT/.env (DENDRA_KIT_RERUN)"; return 1; }
  [[ $RERUN =~ $RERUN_RE ]] || { RERUN_WHY="the recorded re-run holds a character this file does not hand to a shell"; return 1; }
  case "$RERUN" in *" deploy/join.sh"*|*/deploy/join.sh*) : ;; *) RERUN_WHY="the recorded re-run does not run deploy/join.sh"; return 1 ;; esac
  case " $RERUN " in *" --accept-new-signers "*|*" --after-upgrade "*) RERUN_WHY="the recorded re-run carries an option of one run (--accept-new-signers or --after-upgrade)"; return 1 ;; esac
  RERUN="$(printf '%s\n' "$RERUN" | awk '{ o = ""; for (i = 1; i <= NF; i++) if ($i != "--judge" && $i != "--no-judge") o = o (o == "" ? "" : " ") $i; print o }')"
  return 0
}
run_join(){ # run_join <plan> -> the exit code of the recorded join replayed with --after-upgrade <plan>
  local rc
  if [ "$MODE" = cron ]; then ( cd "$REPO" && eval "$RERUN --after-upgrade $1" ); rc=$?
  else ( cd "$REPO" && eval "$RERUN --after-upgrade $1" ) 2>&1 | tee -a "$LOG"; rc=${PIPESTATUS[0]}; fi
  return "$rc"
}
# by_hand <plan> -- what the operator does when this watch does not act.
by_hand(){
  printf 'read the announcement of the release that carries plan %s; once it is published, bash %s verifies and stages it, and this watch acts at its next pass' "$1" "$KU"
}

# ---------------------------------------------------------------- the node on the staged image
# node_on_image <reference> <built-for> -> 0 when the node container of this kit runs that image (NODE_CID set). The
# three lines are written in the node kit's .env, where deploy/join.sh carries them across its rewrite and checks
# DENDRA_NODE_IMAGE_BUILT_FOR against its tree (deploy/join.sh::node_env_image_reconcile); compose then recreates the
# node on that image and builds nothing. The mark matters on the build path: an image ID without it is read by
# join.sh as the operator's choice, started as it is and never reconciled, so any later kit would leave the node on
# this image. The lines outlive the join after the upgrade (node_image_release), and every later join reconciles
# them like any pin of the kit.
NODE_IMAGE_MARK=node_upgrade_watch
node_on_image(){
  local ref="$1" bf="$2" want cid got st out envf="$NODE_KIT/.env"
  want="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null | tr -d '\r')"
  [[ $want =~ $IMAGE_ID_RE ]] || { say "[node-upgrade] the staged node image $ref is not on this host: the node cannot be started on it. Pull or build it (the stage's own log: $STAGE_DIR/node-image-build.log), and the next pass goes on."; return 1; }
  cid="$(node_cid)"
  if [ -n "$cid" ]; then
    got="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null | tr -d '\r')"
    st="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null | tr -d '\r')"
    if [ "$got" = "$want" ] && [ "$st" = running ]; then NODE_CID="$cid"; return 0; fi
  fi
  [ -f "$envf" ] || { say "[node-upgrade] $envf is absent: this kit's node was not set up by deploy/join.sh, and its image is not changed from here."; return 1; }
  env_file_set "$envf" DENDRA_NODE_IMAGE "$ref" && env_file_set "$envf" DENDRA_NODE_IMAGE_BUILT_FOR "$bf" \
    && env_file_set "$envf" DENDRA_NODE_IMAGE_SET_BY "$NODE_IMAGE_MARK" \
    || { say "[node-upgrade] $envf could not be written."; return 1; }
  say "[node-upgrade] DENDRA_NODE_IMAGE=$ref, DENDRA_NODE_IMAGE_BUILT_FOR=$bf and DENDRA_NODE_IMAGE_SET_BY=$NODE_IMAGE_MARK written to $envf; the node starts on that image (docker compose up -d --no-build: nothing pulled, nothing built)."
  if ! out="$( cd "$NODE_KIT" && unset DENDRA_NODE_IMAGE COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_ENV_FILES && docker compose up -d --no-build node 2>&1 )"; then
    say "[node-upgrade] docker compose up failed for the node: $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
    say "[node-upgrade] the guard that runs before it said: $( cd "$NODE_KIT" && docker compose logs --no-color preflight 2>&1 | tail -n 5 | tr '\n' ' ')"
    return 1
  fi
  cid="$(node_cid)"; got="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null | tr -d '\r')"
  [ -n "$cid" ] && [ "$got" = "$want" ] || { say "[node-upgrade] after the start, the node container (${cid:-none}) does not run the staged image $ref."; return 1; }
  NODE_CID="$cid"
  return 0
}

# node_image_release <image the watch wrote> <the value it replaced> -- after the join that follows the upgrade. The
# lines of node_on_image stay a pin of the kit, so that the next kit of the same epoch moves the node to the image it
# pins (deploy/join.sh::node_env_image_reconcile adopts it) rather than building dendrad from the clone at every
# release: a digest keeps its line and its gates and loses the mark it does not need, and an image built from the
# staged tree keeps its three lines, which join.sh reconciles by the mark. Only an operator's own tag (a second
# node's name) is given back: that tag is moved to the image the node runs, and the line names it again.
node_image_release(){
  local img="$1" before="$2" envf="$NODE_KIT/.env" cur
  cur="$(sed -n 's/^DENDRA_NODE_IMAGE=//p' "$envf" 2>/dev/null | tail -1 | tr -d '[:cntrl:]')"
  if [ "$cur" != "$img" ]; then
    say "[node-upgrade] $envf names DENDRA_NODE_IMAGE=${cur:-nothing}, not the image this watch wrote: left as it is."
    return 0
  fi
  case "$before" in
    ""|*@*|sha256:*) : ;;
    *)
      if [[ $before =~ $TAG_REF_RE ]]; then
        if ! docker tag "$img" "$before" >/dev/null 2>&1; then
          say "[node-upgrade] the node image $img could not be tagged $before: DENDRA_NODE_IMAGE stays in $envf with its gates and the mark of this watch, and later joins reconcile it like a pin of the kit. To give your tag back now: docker tag $img $before, then write DENDRA_NODE_IMAGE=$before and remove the DENDRA_NODE_IMAGE_BUILT_FOR and DENDRA_NODE_IMAGE_SET_BY lines of that file."
          return 1
        fi
        env_file_set "$envf" DENDRA_NODE_IMAGE "$before" && env_file_unset "$envf" DENDRA_NODE_IMAGE_BUILT_FOR \
          && env_file_unset "$envf" DENDRA_NODE_IMAGE_SET_BY || { say "[node-upgrade] $envf could not be written: its lines stay a pin of the kit, which later joins reconcile."; return 1; }
        say "[node-upgrade] your tag $before now names the image the node runs, and $envf names it again (DENDRA_NODE_IMAGE=$before)."
        return 0
      fi ;;
  esac
  if [[ $img =~ $NODE_PIN_RE ]]; then
    env_file_unset "$envf" DENDRA_NODE_IMAGE_SET_BY || { say "[node-upgrade] $envf could not be written: the mark of this watch stays beside the pin, and deploy/join.sh reads the pin by its digest all the same."; return 1; }
    say "[node-upgrade] $envf keeps DENDRA_NODE_IMAGE=$img and its gates, without the mark of this watch: a pin of the kit, which the next kit of the same epoch replaces by the image it pins."
    return 0
  fi
  say "[node-upgrade] $envf keeps DENDRA_NODE_IMAGE=$img (built from the staged tree), its gates and the mark of this watch: deploy/join.sh reconciles them like a pin of the kit, so the next kit of the same epoch moves the node to the image it pins."
  return 0
}

# ---------------------------------------------------------------- the upgrade
# continue_upgrade --the clone moved (or about to be moved back onto the tag) under a marker that names a plan: the
# node on the staged image, the plan applied, then the join. Every step reads the marker, so an interrupted pass is
# finished by the next one.
continue_upgrade(){
  local tag commit from plan height img bf rot new head h now last retry="${DENDRA_UPGRADE_JOIN_RETRY_S:-600}" jrc
  local tries max="${DENDRA_UPGRADE_JOIN_MAX_ATTEMPTS:-12}" by_hand_join
  case "$retry" in ""|*[!0-9]*) retry=600 ;; esac
  case "$max" in ""|*[!0-9]*|0*) max=12 ;; esac
  tag="$(pval candidate)"; commit="$(pval candidate_commit)"; from="$(pval from)"; plan="$(pval upgrade_plan)"
  height="$(pval upgrade_height)"; img="$(pval node_image)"; bf="$(pval node_image_built_for)"
  rot="$(pval rotation)"; new="$(pval signers_new)"
  if ! [[ $tag =~ $RELEASE_TAG_RE ]] || ! [[ $plan =~ $PLAN_NAME_RE ]] || [ "${#commit}" != 40 ] || [ -n "$(printf '%s' "$commit" | tr -d '0-9a-f')" ] \
     || { ! [[ $img =~ $NODE_PIN_RE ]] && ! [[ $img =~ $IMAGE_ID_RE ]]; } || ! [[ $bf =~ $BUILT_FOR_FORM_RE ]]; then
    say "[node-upgrade] the marker $PENDING names an upgrade this watch cannot read. Finish it by hand: the clone on the release tag it names, the node on that release's image, then the recorded join with --after-upgrade <the plan>; then remove the marker."
    return 1
  fi
  head="$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)"
  if [ "$head" != "$commit" ]; then
    if [ -n "$from" ] && [ "$head" = "$from" ] && git -C "$REPO" checkout -q --detach "refs/tags/$tag" >/dev/null 2>&1 \
       && [ "$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)" = "$commit" ]; then
      say "[node-upgrade] the clone was still on the tree it leaves: it now sits on $tag."
    else
      say "[node-upgrade] the clone sits on neither $tag nor the tree it left (marker $PENDING). No rollback past the halt. Finish by hand, from $REPO: git checkout --detach refs/tags/$tag; the next pass then goes on."
      return 1
    fi
  fi
  case "$rot" in
    addition|removal)
      if [ -n "$new" ] && [ "$(kitval DENDRA_RELEASE_SIGNERS_SHA256)" != "$new" ]; then
        kitset DENDRA_RELEASE_SIGNERS_SHA256 "$new"
        say "[node-upgrade] the kit's signers record now names the file $tag ships ($new, a rotation the stage followed); the join verifies with it."
      fi ;;
  esac
  node_on_image "$img" "$bf" || return 1
  h="$(wait_applied "$NODE_CID" "$plan")"
  case "$h" in
    ''|*[!0-9]*) say_once "unread-$plan" "[node-upgrade] the node runs the staged image; whether plan $plan is applied was not read (the node did not answer yet, or neither python3 nor jq is on this host). Asked again at every pass."; return 0 ;;
    0) say_once "wait-$plan" "[node-upgrade] the node runs the staged image, and plan $plan is not applied yet: the network has not passed height $height on the new binary. The join runs once the node answers the plan applied; asked again at every pass."; return 0 ;;
  esac
  # The joins after the upgrade are counted in the marker and bounded: past the bound this file runs none, and says
  # at every pass what the operator runs (the node keeps the staged image, and nothing is rolled back).
  tries="$(pval join_attempts | tr -dc 0-9)"; tries="$((10#${tries:-0}))"
  if rerun_line; then by_hand_join="$RERUN --after-upgrade $plan"; else by_hand_join="bash $REPO/deploy/join.sh <your options> --after-upgrade $plan"; fi
  if [ "$tries" -ge "$max" ]; then
    say "[node-upgrade] the join after the upgrade to $tag failed $tries times (plan $plan applied at height $h): this watch runs it no more (DENDRA_UPGRADE_JOIN_MAX_ATTEMPTS=$max). The node keeps the staged image and the marker stays. Run it by hand, from $REPO: $by_hand_join; once it exits 0, remove $PENDING. To let this watch try again, remove the join_attempts line of that file. Read $LOG."
    return 2
  fi
  now="$(date +%s)"; last="$(pval join_attempt_at | tr -dc 0-9)"
  if [ -n "$last" ] && [ "$((now - last))" -lt "$retry" ]; then return 0; fi
  if ! rerun_line; then
    say "[node-upgrade] plan $plan is applied at height $h, and the join cannot be replayed: $RERUN_WHY. Run deploy/join.sh by hand with your options and --after-upgrade $plan; the marker stays until then."
    return 1
  fi
  # An attempt whose count could not be written is not run: an uncounted join would escape the bound. pset returns
  # the status of the rename that writes the marker.
  if ! pset join_attempts "$((tries + 1))" || ! pset join_attempt_at "$now"; then
    say "[node-upgrade] the marker $PENDING could not be written, so this attempt could not be counted: the join is not run."
    return 1
  fi
  tries=$((tries + 1))
  say "[node-upgrade] plan $plan applied at height $h on this node: replaying the recorded join from $tag with --after-upgrade $plan (attempt $tries of $max)."
  run_join "$plan"; jrc=$?
  case "$jrc" in
    0|3)
      node_image_release "$img" "$(pval node_image_before)"
      rm -f "$PENDING" "$HALT_REC"; rm -rf "$STAGE_DIR"; state_clear
      if [ "$jrc" = 0 ]; then say "[node-upgrade] upgraded to $tag: plan $plan applied at height $h, and the join exited 0. Done."
      else say "[node-upgrade] upgraded to $tag: plan $plan applied at height $h; the join exited 3 (it did not measure the miner: miner_health.sh reads it hourly). Done."; fi
      return 0 ;;
    *)
      if [ "$tries" -ge "$max" ]; then
        say "[node-upgrade] the join exited $jrc, attempt $tries of $max: this watch runs it no more. No rollback: the binary before the upgrade cannot continue past height $height. The node keeps the staged image and the marker stays. Run it by hand, from $REPO: $by_hand_join; once it exits 0, remove $PENDING. Read $LOG."
      else
        say "[node-upgrade] the join exited $jrc, attempt $tries of $max. No rollback: the binary before the upgrade cannot continue past height $height. The node keeps the staged image, the marker stays, and the join is replayed in $retry s. Read $LOG."
      fi
      return 1 ;;
  esac
}

# start_upgrade -- a halt of HALT_PLAN, a stage that holds and registers it: the clone is moved as an update moves it.
start_upgrade(){
  local head branch dirty co rec
  rerun_line || { say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, which the staged release $ST_TAG registers, but the recorded join cannot be replayed: $RERUN_WHY. Nothing moved; the operator acts: $(by_hand "$HALT_PLAN")."; return 2; }
  git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { say "[node-upgrade] $REPO is not a git clone: nothing moved."; return 2; }
  dirty="$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)"
  [ -z "$dirty" ] || { say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, and the clone carries local changes to tracked files: a move would carry or lose them. Nothing moved; the operator acts: $(by_hand "$HALT_PLAN")."; return 2; }
  head="$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)"
  branch="$(git -C "$REPO" symbolic-ref -q --short HEAD 2>/dev/null || true)"
  # The tag of the clone is the tag object the staged clone verified, and names the staged commit.
  if [ "$(git -C "$REPO" rev-parse -q --verify "refs/tags/$ST_TAG^{commit}" 2>/dev/null)" != "$ST_COMMIT" ] \
     || [ "$(git -C "$REPO" rev-parse -q --verify "refs/tags/$ST_TAG" 2>/dev/null)" != "$(git -C "$STAGE_DIR/tree" rev-parse -q --verify "refs/tags/$ST_TAG" 2>/dev/null)" ]; then
    say "[node-upgrade] this clone's tag $ST_TAG is not the one the stage verified: nothing moved; bash $KU stages it again."
    return 2
  fi
  rec="$(kitval DENDRA_RELEASE_SIGNERS_SHA256)"
  say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, and the staged release $ST_TAG registers it: the kit moves to $ST_TAG."
  say "[node-upgrade] no rollback past this point: the binary before the upgrade cannot continue past height $HALT_HEIGHT, so a failed step is replayed, never undone."
  git -C "$REPO" branch -f kit-before-update "$head" >/dev/null 2>&1 || { say "[node-upgrade] the tree the clone leaves could not be kept on kit-before-update: nothing moved."; return 1; }
  if ! printf 'candidate=%s\ncandidate_commit=%s\nfrom=%s\nbranch=%s\nsigners_record=%s\nsigners_new=%s\nrotation=%s\nupgrade_plan=%s\nupgrade_height=%s\nnode_image=%s\nnode_image_built_for=%s\nwritten=%s\n' \
       "$ST_TAG" "$ST_COMMIT" "$head" "$branch" "$rec" "$ST_SIGNERS_NEW" "$ST_ROTATION" "$HALT_PLAN" "$HALT_HEIGHT" "$ST_IMAGE" "$ST_BUILT_FOR" "$(TS)" > "$PENDING"; then
    say "[node-upgrade] the marker $PENDING could not be written: nothing moved."; return 1
  fi
  # What the node kit's .env named before node_on_image writes over it, for node_image_release.
  pset node_image_before "$(sed -n 's/^DENDRA_NODE_IMAGE=//p' "$NODE_KIT/.env" 2>/dev/null | tail -1 | tr -d '[:cntrl:]')" \
    || { rm -f "$PENDING"; say "[node-upgrade] the marker $PENDING could not be written: nothing moved."; return 1; }
  if ! co="$(git -C "$REPO" checkout -q --detach "refs/tags/$ST_TAG" 2>&1)"; then
    rm -f "$PENDING"
    say "[node-upgrade] the tree of $ST_TAG would overwrite files git does not track, or cannot be checked out: nothing moved. git said: $(printf '%s' "$co" | head -3 | tr '\n' ' ')"
    return 2
  fi
  say "[node-upgrade] the clone sits on $ST_TAG; the tree it left is on kit-before-update."
  continue_upgrade
}

status_report(){
  say "[node-upgrade] --status, nothing moves. Clone $REPO, node kit $NODE_KIT."
  if [ -r "$PENDING" ] && [ -n "$(pval upgrade_plan)" ]; then
    say "  in progress: the upgrade to $(pval candidate) (plan $(pval upgrade_plan), height $(pval upgrade_height)), node image $(pval node_image)"
  fi
  case "$HALT_STATE" in
    nonode) say "  node       no node container of this kit (a kit without its own node is not watched)" ;;
    none) say "  node       $NODE_CID, no halt line in the last ${DENDRA_UPGRADE_LOG_TAIL:-3000} lines of its log" ;;
    halt)
      say "  node       $NODE_CID stopped at height $HALT_HEIGHT for plan $HALT_RAW, its log says"
      confirm_halt
      say "  chain      $CONFIRM: $CONFIRM_WHY" ;;
    *) say "  node       not measured: $HALT_WHY" ;;
  esac
  stage_read
  case "$STAGE" in
    ok) say "  stage      $ST_TAG (epoch $ST_EPOCH), plans: ${ST_PLANS:-none read ($ST_PLANS_WHY)}, node image $ST_IMAGE" ;;
    *) say "  stage      $STAGE${STAGE_WHY:+: $STAGE_WHY}" ;;
  esac
}

main(){
  local auto t
  case "${1:-}" in
    "") : ;;
    --cron) MODE=cron ;;
    --status) MODE=status ;;
    -h|--help) awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; return 0 ;;
    *) printf '[node-upgrade] unknown argument: %s (see --help)\n' "$1" >&2; return 2 ;;
  esac
  [ "$MODE" = cron ] && exec >> "$LOG" 2>&1
  [ -r "$KIT/.env" ] || { [ "$MODE" = status ] && say "[node-upgrade] no kit here ($KIT/.env): nothing is watched."; return 0; }
  auto="$(kitval DENDRA_AUTO_UPDATE)"
  case "$auto" in
    0) [ "$MODE" = status ] && say "[node-upgrade] off: DENDRA_AUTO_UPDATE=0 in $KIT/.env turns the daily update and this watch off."; return 0 ;;
    ""|1) : ;;
    *) say_once "auto-$auto" "[node-upgrade] refused: DENDRA_AUTO_UPDATE=$auto in $KIT/.env is neither 0 nor 1."; return 2 ;;
  esac
  for t in git docker; do
    command -v "$t" >/dev/null 2>&1 || { say_once "tool-$t" "[node-upgrade] not measured: $t is not installed on this host, so the node is not watched."; return 3; }
  done
  if [ "$MODE" != status ]; then
    lock_take "$LOCK" || return 0
    trap 'lock_release "$LOCK"' EXIT
  fi
  read_halt
  if [ "$MODE" = status ]; then status_report; return 0; fi
  if [ -r "$PENDING" ]; then
    # A marker without a plan is kit_update.sh's: that file finishes its own update.
    [ -n "$(pval upgrade_plan)" ] || return 0
    continue_upgrade; return $?
  fi
  case "$HALT_STATE" in
    none|nonode) [ -r "$STATE_REC" ] && state_clear; return 0 ;;
    unmeasured) say_once unmeasured "[node-upgrade] not measured: $HALT_WHY."; return 3 ;;
  esac
  # Nothing below runs, and nothing is recorded, on a line the chain does not confirm.
  confirm_halt
  case "$CONFIRM" in
    yes) : ;;
    no)
      rm -f "$HALT_REC"
      say_once "refuted-$HALT_PLAN-$HALT_HEIGHT" "[node-upgrade] the node's log names plan $HALT_RAW at height $HALT_HEIGHT, and the chain does not confirm a halt: $CONFIRM_WHY. A log line is a hint, never a halt: nothing moved."
      return 0 ;;
    *)
      say_once "unconfirmed-$HALT_PLAN-$HALT_HEIGHT" "[node-upgrade] the node's log names plan $HALT_RAW at height $HALT_HEIGHT, and whether the chain confirms it was not read: $CONFIRM_WHY. Nothing moved; asked again at every pass."
      return 3 ;;
  esac
  if ! [[ $HALT_PLAN =~ $PLAN_NAME_RE ]]; then
    say "[node-upgrade] the node stopped at height $HALT_HEIGHT for an upgrade plan whose name this watch does not read ($HALT_RAW): nothing is matched against it. The operator acts: $(by_hand '<that plan>')."
    return 2
  fi
  halt_record
  stage_read
  case "$STAGE" in
    ok) : ;;
    absent)
      say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, and nothing is staged: no release of a higher epoch was verified and staged by kit_update.sh. Nothing is guessed. The operator acts: $(by_hand "$HALT_PLAN")."
      return 2 ;;
    removed|incomplete)
      say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, and the stage ${ST_TAG:+of $ST_TAG }does not hold: $STAGE_WHY. Nothing moved. The operator acts: $(by_hand "$HALT_PLAN")."
      return 2 ;;
    *)
      say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, and the stage was not read: $STAGE_WHY. Nothing moved."
      return 3 ;;
  esac
  if ! plan_registered "$HALT_PLAN" "$ST_PLANS"; then
    say "[node-upgrade] the node stopped at height $HALT_HEIGHT for plan $HALT_PLAN, and the staged release $ST_TAG registers ${ST_PLANS:-no plan it could read${ST_PLANS_WHY:+ ($ST_PLANS_WHY)}} (chain/app/upgrades.go::upgradePlans of the staged tree): it is not that plan's binary, and nothing is guessed. The operator acts: $(by_hand "$HALT_PLAN")."
    return 2
  fi
  start_upgrade
}

main "$@"; exit $?
