#!/usr/bin/env bash
# kit_update.sh -- the automatic update of an installed kit, within bounds (ADR-058 decision 3).
#
# What it does, and nothing else: it moves this clone to the newest release tag of the project's repository
# and replays the join this kit recorded, when every bound below holds. Each bound is a refusal, not a warning:
#   * the candidate is a tag of the form vMAJOR.MINOR.PATCH published on origin (never a local tag, never a
#     branch, main included: a branch moves under its name, a tag names one tree);
#   * its tree carries the same docker/CONSENSUS_EPOCH as this clone (another epoch is a new binary and a
#     governed upgrade, never an automatic act) and a higher docker/KIT_VERSION. A lower epoch is refused; a
#     higher one is staged and never applied (the staged release, below);
#   * the tag carries an SSH signature, and that signature verifies with this clone's docker/RELEASE_SIGNERS
#     (never with the file the candidate ships), under git's own namespace, with gpg.program pointed at /bin/false
#     so that no gpg signature is ever checked against this host's keyring; and that file is the one the kit
#     recorded (DENDRA_RELEASE_SIGNERS_SHA256);
#   * the candidate's docker/RELEASE_SIGNERS is the same file, or a rotation of two shapes only: lines added and
#     none removed (a key the kit already trusts signed the tag that brings the new line), or lines removed and
#     none added with the signing key still named. Any other change of that file is a change of trust root and
#     stays a gesture on the machine (deploy/join.sh --accept-new-signers prints the fingerprints). An accepted
#     rotation re-records the file in the kit's .env before the join, and the record is restored on a rollback;
#   * the candidate's VERSION file says what its gates say and names a committed source: the 40 hexadecimal
#     digits of a commit (the generator writes that line from git log; a tree it could not attribute reads
#     unknown there, and a hand-made tree reads anything). The generator's own seal (.build-complete) is not
#     in a tagged tree, so VERSION is the only account the tree carries, and the signature is the proof;
#   * the plan is printed before anything moves, and the run is logged under this kit (kit-update.log);
#   * the tree the clone leaves is kept on the branch kit-before-update, as the manual instruction does, and a
#     marker (.kit-update.pending) is written before the checkout and removed when the update is done or undone:
#     a run that finds the marker finishes the interrupted update (the join replayed from the new tree, or the
#     clone brought back) instead of reading a clone already on the new tag as nothing to do;
#   * the join is replayed from the recorded re-run (DENDRA_KIT_RERUN, deploy/join.sh::persist_kit_update),
#     without --judge, --no-judge or --accept-new-signers: the judge seat is read from the kit's .env
#     (DENDRA_NO_JUDGE, DENDRA_MINER_JUDGE), so judge.sh on and off hold across updates, and the acceptance of
#     a new signers file is a gesture of one run, refused in a recorded line; when the join fails, or the miner's
#     heartbeat does not come within a bounded wait, the clone goes back to kit-before-update and the join is
#     replayed from there;
#   * DENDRA_AUTO_UPDATE=0 in the kit's .env turns the whole of it off (deploy/join.sh --no-auto-update).
# A re-run the new kit refuses for a reason that is a decision and not a failure (a machine without a GPU below
# the judge floor, deploy/hw_probe.sh --role: refused) is not rolled back: the exit procedure is printed and the
# miner is stopped, since rolling back would restore the mining the network ends.
# The first installation is never this file: it stays downloaded, read, run (deploy/install.sh).
#
# The staged release. A candidate of a higher docker/CONSENSUS_EPOCH that passes every bound above (the tag, its
# signature, VERSION, the signers file) is not applied: it is staged under upgrade-stage/, a directory of this kit
# that deploy/join.sh does not read, as a clone of the signed tag (tree/) and a record (stage.env), with the node
# image that tree pins (docker/NODE_IMAGE, pulled by digest) or, when it pins none, an image built now from the staged
# tree, so that nothing is downloaded or built at the halt. node_upgrade_watch.sh, run every minute, applies the stage
# when the node stops at the height of a plan the staged tree registers, and leaves a marker this file then leaves to
# it. Every run verifies the stage again and removes one that no longer holds (a tag that does not verify with this
# clone's docker/RELEASE_SIGNERS, a staged tree that changed, an epoch this clone already runs), and says so.
# A kit that runs no node of its own (--remote-rpc or --no-node in its recorded join) stages nothing: no node of
# this kit runs that image. It applies the release of a higher epoch as an update, once the chain it reads (DENDRA_NODE
# of the kit's .env) answers one of the plans of that release applied: the network then runs the new epoch. The join
# runs with --after-upgrade <plan>, which deploy/join.sh checks through the same RPC (remote_upgrade).
#
# Usage:
#   bash deploy/testnet-miner/kit_update.sh          # plan, then apply; prints and logs
#   bash deploy/testnet-miner/kit_update.sh --plan   # the plan only: nothing moves
#   bash deploy/testnet-miner/kit_update.sh --cron   # the daily run deploy/join.sh schedules: output to the log only
#   bash deploy/testnet-miner/kit_update.sh --stage-check   # the staged release verified again, as key=value lines
# Exit codes (three answers, never two): 0 nothing to do, or updated and the miner answered; 1 the update failed
# (rolled back, and the log says whether the rollback worked); 2 refused by a bound, nothing moved (a release of a
# higher epoch ends here as well, staged or not); 3 not measured (origin unreachable, a tool missing): nothing moved
# either. --stage-check: 0 a stage that holds; 2 none, removed, or without its node image; 3 not measured.
# Environment: DENDRA_KIT_UPDATE_HEARTBEAT_WAIT_S (the bounded wait for the miner's first heartbeat after the
# re-run, default 600), DENDRA_STATUS_FILE (the heartbeat file inside the miner's container, the daemon's own
# default otherwise). Both exist for the benches as much as for an operator.
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
KIT="${DENDRA_MINER_KIT:-$SELF}"
REPO="$(cd "$KIT/../.." 2>/dev/null && pwd)"
LOG="$KIT/kit-update.log"
PENDING="$KIT/.kit-update.pending"
MODE=apply
case "${1:-}" in
  "") : ;;
  --plan) MODE=plan ;;
  --cron) MODE=cron ;;
  --stage-check) MODE=stage-check ;;
  -h|--help) awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; exit 0 ;;
  *) printf '[kit-update] unknown argument: %s (see --help)\n' "$1" >&2; exit 2 ;;
esac
[ "$MODE" = cron ] && exec >> "$LOG" 2>&1

TS(){ date -u +%FT%TZ 2>/dev/null; }
# --stage-check answers its caller on stdout and writes nothing to the log: node_upgrade_watch.sh logs what it does.
say(){ printf '%s\n' "$*"; case "$MODE" in cron|stage-check) : ;; *) printf '%s %s\n' "$(TS)" "$*" >> "$LOG" 2>/dev/null ;; esac; }
[ "$MODE" = cron ] && say(){ printf '%s %s\n' "$(TS)" "$*"; }
refuse(){ say "[kit-update] refused: $*"; say "[kit-update] nothing moved."; exit 2; }
unmeasured(){ say "[kit-update] not measured: $*"; say "[kit-update] nothing moved."; exit 3; }
failed(){ say "[kit-update] FAILED: $*"; exit 1; }

RELEASE_TAG_RE='^v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$'
# The alphabet of a recorded re-run: deploy/join.sh::persist_kit_update writes the re-run on letters, digits and
# space : / . _ ~ % ? = + @ , -. The hint line may carry ; & ( ), a re-run never does, and this file refuses a
# line that holds any of them rather than hand it to a shell.
RERUN_RE='^[A-Za-z0-9 :/._~%?=+@,-]+$'
kitval(){ [ -r "$KIT/.env" ] || return 0; tr -d '\r' < "$KIT/.env" | sed -n "s/^$1=//p" | head -1; }
# kitset <KEY> <value> [unset] -- one line of the kit's .env, in place (the same edit deploy/join.sh::_miner_env_set makes).
# Its status is the write's, never the cleanup's.
kitset(){
  local f="$KIT/.env" t rc
  t="$(mktemp)" || return 1
  awk -v k="$1" -v v="$2" -v u="${3:-}" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s && u == "") print k "=" v; s = 1; next }
    { print }
    END { if (!s && u == "") print k "=" v }' "$f" > "$t" && cat "$t" > "$f"; rc=$?
  rm -f "$t"
  return "$rc"
}
gate(){ printf '%s' "$1" | head -1 | tr -dc 0-9; }   # gate <text> -> the integer on its first line, or nothing

# ---------------------------------------------------------------- the lock, shared with node_upgrade_watch.sh
# One run at a time moves this clone: this file and the watch take the same directory. A lock whose run is gone is
# taken over: its pid names no process (a run cut by a reboot), or it is older than six hours (a lock written before
# the pid was). It is renamed before it is removed, so that two runs that both find it stale do not both hold it.
lock_stale(){ # lock_stale <dir> -> 0 when the run that holds it is gone
  local p
  [ -n "$(find "$1" -maxdepth 0 -mmin +360 2>/dev/null)" ] && return 0
  p="$(head -1 "$1/pid" 2>/dev/null | tr -dc 0-9)"
  [ -n "$p" ] && [ -d /proc/self ] && [ ! -d "/proc/$p" ] && return 0
  return 1
}
lock_take(){ # lock_take <dir> -> 0 held by this run (its pid written inside), 1 held by another run
  local s="$1.stale.$$"
  if ! mkdir "$1" 2>/dev/null; then
    lock_stale "$1" || return 1
    mv "$1" "$s" 2>/dev/null && rm -rf "$s"
    mkdir "$1" 2>/dev/null || return 1
    LOCK_WAS_STALE=1
  fi
  printf '%s\n' "$$" > "$1/pid"
  return 0
}
lock_release(){ rm -f "$1/pid"; rmdir "$1" 2>/dev/null; }
LOCK_WAS_STALE=0

# ---------------------------------------------------------------- the staged release of a higher epoch
# What a stage is: the header. Its record is key=value lines, read by key and never sourced.
STAGE_DIR="$KIT/upgrade-stage"; STAGE_REC="$STAGE_DIR/stage.env"; STAGE_TREE="$STAGE_DIR/tree"
# A plan name as this kit reads it, in a halt line or in a staged tree. A name outside it is never matched.
PLAN_NAME_RE='^[A-Za-z0-9._-]{1,128}$'
# The forms deploy/join.sh accepts for a node image: NODE_IMAGE_RE and BUILT_FOR_RE there, the same expressions.
NODE_PIN_RE='^ghcr[.]io/dendranetwork/dendra-node@sha256:[0-9a-f]{64}$'
STAGE_BUILT_FOR_RE='^# built-for: kit_version=([0-9]+) consensus_epoch=([0-9]+) release=(v[0-9]+[.][0-9]+[.][0-9]+)$'
IMAGE_ID_RE='^sha256:[0-9a-f]{64}$'
stage_val(){ sed -n "s/^$1=//p" "$STAGE_REC" 2>/dev/null | head -1 | tr -d '\r'; }

# stage_tag_verified <git dir> <tag> <signers file> -> 0 and the signing key's fingerprint on stdout when the tag is
# annotated, carries an SSH signature block and verifies with that file; 1 and the reason otherwise. The two gates
# the candidate passes below (tag_sig_format, then verify-tag with gpg.program pointed at /bin/false), on a clone
# given by its directory.
stage_tag_verified(){
  local b d='-----' out  # the dashes of a block line are composed here, so no key-block marker is written in this file
  [ "$(git -C "$1" cat-file -t "refs/tags/$2" 2>/dev/null)" = tag ] || { printf '%s is not an annotated tag there' "$2"; return 1; }
  b="$(git -C "$1" cat-file tag "refs/tags/$2" 2>/dev/null | grep "^${d}BEGIN " | tail -1)"
  [ "$b" = "${d}BEGIN SSH SIGNATURE${d}" ] || { printf 'it carries %s, and only an SSH signature is verified' "${b:-no signature block}"; return 1; }
  if out="$(git -C "$1" -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile="$3" verify-tag "refs/tags/$2" 2>&1)"; then
    printf '%s' "$out" | grep -o 'SHA256:[A-Za-z0-9+/=]*' | head -1; return 0
  fi
  printf '%s' "$out" | head -3 | tr '\n' ' '; return 1
}

# plans_of_tree <tree> -> PLANS (space-separated), PLANS_STATE (read|none|unreadable), PLANS_WHY: the plan names the
# tree's binary can apply, read from chain/app/upgrades.go, the variable upgradePlans. Its items are string
# literals or constants declared in that file (const X = "name", alone or in a const block). Any other item, a name
# outside PLAN_NAME_RE, or no such variable, is unreadable: a halt is never matched against a guessed list.
plans_of_tree(){
  local f="$1/chain/app/upgrades.go" out l p
  PLANS=""; PLANS_STATE=unreadable; PLANS_WHY=""
  [ -r "$f" ] || { PLANS_WHY="chain/app/upgrades.go is not in the staged tree"; return 0; }
  out="$(tr -d '\r' < "$f" | awk '
    function trim(s) { sub(/^[[:blank:]]+/, "", s); sub(/[[:blank:]]+$/, "", s); return s }
    { l = $0; sub(/[/][/].*$/, "", l) }
    inconst && l ~ /^[[:blank:]]*[)]/ { inconst = 0 }
    l ~ /^const[[:blank:]]*[(][[:blank:]]*$/ { inconst = 1; next }
    {
      c = ""
      if (l ~ /^const[[:blank:]]+/) { c = l; sub(/^const[[:blank:]]+/, "", c) } else if (inconst) c = l
      if (c ~ /^[[:blank:]]*[A-Za-z_][A-Za-z0-9_]*[[:blank:]]*(string[[:blank:]]*)?=[[:blank:]]*"[^"]*"[[:blank:]]*$/) {
        name = trim(c); sub(/[^A-Za-z0-9_].*$/, "", name)
        val = c; sub(/^[^"]*"/, "", val); sub(/"[[:blank:]]*$/, "", val)
        cst[name] = val
      }
    }
    invar { body = body " " l; if (l ~ /[}]/) { invar = 0; done = 1 }; next }
    l ~ /^var[[:blank:]]+upgradePlans[[:blank:]]*=[[:blank:]]*[[][]][[:blank:]]*string[[:blank:]]*[{]/ {
      found = 1; body = l; sub(/^[^{]*[{]/, "", body)
      if (body ~ /[}]/) done = 1; else invar = 1
      next
    }
    l ~ /^var[[:blank:]]+upgradePlans[[:blank:]]+[[][]][[:blank:]]*string[[:blank:]]*$/ { found = 1; done = 1; body = ""; next }
    END {
      if (!found || !done) { print "ERR it declares no var upgradePlans = []string{...}"; exit }
      sub(/[}].*$/, "", body)
      n = split(body, it, ",")
      for (i = 1; i <= n; i++) {
        x = trim(it[i])
        if (x == "") continue
        if (x ~ /^"[^"]*"$/) { print "PLAN " substr(x, 2, length(x) - 2); continue }
        if (x ~ /^[A-Za-z_][A-Za-z0-9_]*$/ && (x in cst)) { print "PLAN " cst[x]; continue }
        print "ERR an item of upgradePlans is neither a string literal nor a constant of the file: " x
        exit
      }
    }')"
  case "$out" in
    *"ERR "*) PLANS_WHY="chain/app/upgrades.go: $(printf '%s\n' "$out" | sed -n 's/^ERR //p' | head -1)"; return 0 ;;
  esac
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    p="${l#PLAN }"
    [[ $p =~ $PLAN_NAME_RE ]] || { PLANS=""; PLANS_WHY="a name in upgradePlans is outside the alphabet this kit reads (letters, digits and . _ -): $p"; return 0; }
    PLANS="${PLANS:+$PLANS }$p"
  done <<< "$out"
  if [ -n "$PLANS" ]; then PLANS_STATE=read; else PLANS_STATE=none; PLANS_WHY="upgradePlans of the staged tree is empty"; fi
}

# stage_node_pin <tree> -> STAGE_PIN, STAGE_PIN_STATE (pinned|none|refused), STAGE_PIN_WHY: the rule of
# deploy/join.sh::image_pin and deploy/join.sh::image_built_for on the staged tree's docker/NODE_IMAGE (exactly one
# value line DENDRA_NODE_IMAGE=<the official namespace and a full digest>, exactly one '# built-for:' line whose two
# gates are that tree's docker/KIT_VERSION and docker/CONSENSUS_EPOCH). The bench of node_upgrade_watch.sh runs both
# readers on the same files and holds them equal.
stage_node_pin(){
  local f="$1/docker/NODE_IMAGE" lines n v bf nb kv ce
  STAGE_PIN=""; STAGE_PIN_STATE=none; STAGE_PIN_WHY=""
  [ -r "$f" ] || { STAGE_PIN_WHY="docker/NODE_IMAGE is not in the staged tree"; return 0; }
  lines="$(tr -d '\r' < "$f" | grep -vE '^[[:space:]]*(#|$)' || true)"
  n="$(printf '%s' "$lines" | grep -c . || true)"
  [ "${n:-0}" != 0 ] || { STAGE_PIN_WHY="docker/NODE_IMAGE of the staged tree names no image"; return 0; }
  STAGE_PIN_STATE=refused
  [ "$n" = 1 ] || { STAGE_PIN_WHY="docker/NODE_IMAGE of the staged tree carries $n value lines where exactly one is expected"; return 0; }
  case "$lines" in
    DENDRA_NODE_IMAGE=*) v="${lines#DENDRA_NODE_IMAGE=}" ;;
    *) STAGE_PIN_WHY="docker/NODE_IMAGE of the staged tree is not of the form DENDRA_NODE_IMAGE=<reference>"; return 0 ;;
  esac
  [ -n "$v" ] || { STAGE_PIN_STATE=none; STAGE_PIN_WHY="docker/NODE_IMAGE of the staged tree names no image"; return 0; }
  printf '%s\n' "$v" | LC_ALL=C grep -Eq "$NODE_PIN_RE" || { STAGE_PIN_WHY="docker/NODE_IMAGE of the staged tree names '$v', not an image of the official namespace by full digest"; return 0; }
  bf="$(tr -d '\r' < "$f" | grep -E "$STAGE_BUILT_FOR_RE" || true)"
  nb="$(printf '%s' "$bf" | grep -c . || true)"
  kv="$(gate "$(cat "$1/docker/KIT_VERSION" 2>/dev/null)")"; ce="$(gate "$(cat "$1/docker/CONSENSUS_EPOCH" 2>/dev/null)")"
  [ "${nb:-0}" = 1 ] || { STAGE_PIN_WHY="docker/NODE_IMAGE of the staged tree pins an image without exactly one '# built-for:' line (${nb:-0} found)"; return 0; }
  [ -n "$kv" ] && [ -n "$ce" ] || { STAGE_PIN_WHY="the gates of the staged tree could not be read"; return 0; }
  [[ "$bf" =~ $STAGE_BUILT_FOR_RE ]] || { STAGE_PIN_WHY="the '# built-for:' line of the staged tree could not be read"; return 0; }
  if [ "$((10#${BASH_REMATCH[1]}))" != "$((10#$kv))" ] || [ "$((10#${BASH_REMATCH[2]}))" != "$((10#$ce))" ]; then
    STAGE_PIN_WHY="the node image pinned by the staged tree was built for kit ${BASH_REMATCH[1]} / epoch ${BASH_REMATCH[2]}, and that tree is kit $kv / epoch $ce"; return 0
  fi
  STAGE_PIN="$v"; STAGE_PIN_STATE=pinned
}

# stage_image <tree> -> STAGE_IMAGE (a digest reference, or the ID of an image built here), STAGE_IMAGE_KIND
# (pin|build|none), STAGE_IMAGE_WHY. The pin of the staged tree, pulled and started only on an engine of its platform
# (the rule of deploy/join.sh::image_platform); otherwise the node image built now from the staged tree, tagged
# dendra/node:upgrade-<tag> so that no prune removes it before the halt. Reads CAND and CAND_COMMIT.
stage_image(){
  local t="$1" id img host blog="$STAGE_DIR/node-image-build.log"
  STAGE_IMAGE=""; STAGE_IMAGE_KIND=none; STAGE_IMAGE_WHY=""
  command -v docker >/dev/null 2>&1 || { STAGE_IMAGE_WHY="docker is not on this host"; return 0; }
  stage_node_pin "$t"
  if [ "$STAGE_PIN_STATE" = pinned ]; then
    if docker pull -q "$STAGE_PIN" >/dev/null 2>&1 && docker image inspect "$STAGE_PIN" >/dev/null 2>&1; then
      img="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$STAGE_PIN" 2>/dev/null | tr -d '\r')"
      host="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null | tr -d '\r')"
      case "$img" in /*|*/|"") img="" ;; esac
      case "$host" in /*|*/|"") host="" ;; esac
      if [ -n "$img" ] && [ "$img" = "$host" ]; then STAGE_IMAGE="$STAGE_PIN"; STAGE_IMAGE_KIND=pin; return 0; fi
      say "[kit-update] the node image pinned by $CAND is for ${img:-an unread platform} and this engine runs ${host:-an unread platform}: it is built from the staged tree instead."
    else
      say "[kit-update] the node image pinned by $CAND could not be pulled: it is built from the staged tree instead."
    fi
  else
    say "[kit-update] $STAGE_PIN_WHY: the node image is built from the staged tree."
  fi
  [ -r "$t/docker/Dockerfile.node" ] || { STAGE_IMAGE_WHY="the staged tree pins no usable node image and carries no docker/Dockerfile.node"; return 0; }
  if ! docker build -f "$t/docker/Dockerfile.node" -t "dendra/node:upgrade-$CAND" --build-arg VERSION="$CAND" --build-arg COMMIT="$CAND_COMMIT" "$t" > "$blog" 2>&1; then
    STAGE_IMAGE_WHY="the node image could not be built from the staged tree (the end of $blog says why)"
    say "[kit-update] the build of the node image failed; the end of its output:"; tail -n 5 "$blog" 2>/dev/null | sed 's/^/    /'
    return 0
  fi
  id="$(docker image inspect --format '{{.Id}}' "dendra/node:upgrade-$CAND" 2>/dev/null | tr -d '\r')"
  [[ $id =~ $IMAGE_ID_RE ]] || { STAGE_IMAGE_WHY="the ID of the node image built from the staged tree could not be read"; return 0; }
  STAGE_IMAGE="$id"; STAGE_IMAGE_KIND=build
}

# stage_tree_finishes <tree> -> 0 when the staged tree can finish the upgrade once the clone sits on it, else 1 and
# STAGE_TREE_WHY. node_upgrade_watch.sh replays the join from that tree with --after-upgrade, and the cron line of
# deploy/join.sh runs that tree's node_upgrade_watch.sh: a tag without either would leave the node on the new image
# with nothing to run the join, and kit_update.sh leaves a marker that names a plan to the watch.
stage_tree_finishes(){
  STAGE_TREE_WHY=""
  if [ ! -r "$1/deploy/testnet-miner/node_upgrade_watch.sh" ]; then
    STAGE_TREE_WHY="the staged tree carries no deploy/testnet-miner/node_upgrade_watch.sh, which finishes the upgrade once the clone sits on it"
    return 1
  fi
  # The option is read from the argument loop of that join.sh, the place that parses it.
  if ! grep -qE '^[[:space:]]*--after-upgrade[)]' "$1/deploy/join.sh" 2>/dev/null; then
    STAGE_TREE_WHY="the deploy/join.sh of the staged tree takes no --after-upgrade option, which the join after the upgrade runs with"
    return 1
  fi
  return 0
}
# stage_drop <why> -> the stage removed (said by the caller), or, under --plan, only named.
stage_drop(){
  STAGE_STATE=removed; STAGE_WHY="$1"
  if [ "$MODE" = plan ]; then STAGE_WHY="$1 (not removed: --plan)"; else rm -rf "$STAGE_DIR"; fi
}
# stage_check -> STAGE_STATE (absent|ok|incomplete|removed), STAGE_WHY, the record (S_TAG, S_COMMIT, S_KIT, S_EPOCH,
# S_IMAGE, S_IMAGE_KIND, S_BUILT_FOR, S_ROTATION, S_SIGNERS_NEW, S_KEY) and the plans read again from the staged tree
# (plans_of_tree). Reads EPOCH_NOW, SIGNERS and SIG_SHA of this clone. A stage is never kept on its record alone:
# the tag is verified again with this clone's signers file, and what no longer holds is removed.
stage_check(){
  local head st vk tc kv ce id
  STAGE_STATE=absent; STAGE_WHY=""; PLANS=""; PLANS_STATE=unreadable; PLANS_WHY=""; STAGE_TREE_WHY=""
  S_TAG=""; S_COMMIT=""; S_KIT=""; S_EPOCH=""; S_IMAGE=""; S_IMAGE_KIND=""; S_BUILT_FOR=""; S_ROTATION=""; S_SIGNERS_NEW=""; S_KEY=""
  [ -e "$STAGE_DIR" ] || return 0
  S_TAG="$(stage_val tag)"; S_COMMIT="$(stage_val commit)"
  S_KIT="$(gate "$(stage_val kit_version)")"; S_EPOCH="$(gate "$(stage_val consensus_epoch)")"
  S_IMAGE="$(stage_val node_image)"; S_IMAGE_KIND="$(stage_val node_image_kind)"
  S_ROTATION="$(stage_val rotation)"; S_SIGNERS_NEW="$(stage_val signers_new)"
  if ! [[ $S_TAG =~ $RELEASE_TAG_RE ]] || [ "${#S_COMMIT}" != 40 ] || [ -n "$(printf '%s' "$S_COMMIT" | tr -d '0-9a-f')" ] \
     || [ -z "$S_KIT" ] || [ -z "$S_EPOCH" ]; then
    stage_drop "its record ($STAGE_REC) is unreadable or incomplete"; return 0
  fi
  case "$S_ROTATION" in ""|addition|removal) : ;; *) stage_drop "its record names a rotation this file does not follow"; return 0 ;; esac
  case "$S_IMAGE_KIND:$S_IMAGE" in
    pin:*) [[ $S_IMAGE =~ $NODE_PIN_RE ]] || { stage_drop "its record names a node image that is not a pin of the official namespace"; return 0; } ;;
    build:*) [[ $S_IMAGE =~ $IMAGE_ID_RE ]] || { stage_drop "its record names a built node image that is not an image ID"; return 0; } ;;
    none:|:) S_IMAGE="" ;;
    *) stage_drop "its record names a node image of an unknown kind"; return 0 ;;
  esac
  S_BUILT_FOR="kit_version=$((10#$S_KIT)) consensus_epoch=$((10#$S_EPOCH))"
  [ "$S_EPOCH" -gt "$EPOCH_NOW" ] 2>/dev/null || { stage_drop "$S_TAG carries consensus epoch $S_EPOCH and this clone runs $EPOCH_NOW: nothing is left to apply"; return 0; }
  [ "$(stage_val signers_base)" = "$SIG_SHA" ] || { stage_drop "this clone's docker/RELEASE_SIGNERS is not the file $S_TAG was staged with; the next daily run stages it again with the file this kit holds now"; return 0; }
  head="$(git -C "$STAGE_TREE" rev-parse -q --verify HEAD 2>/dev/null)"
  [ "$head" = "$S_COMMIT" ] || { stage_drop "the staged clone is not on the commit of $S_TAG"; return 0; }
  st="$(git -C "$STAGE_TREE" status --porcelain --untracked-files=no 2>/dev/null)" || { stage_drop "the staged clone cannot be read"; return 0; }
  [ -z "$st" ] || { stage_drop "the staged clone carries changes to tracked files"; return 0; }
  vk="$(stage_tag_verified "$STAGE_TREE" "$S_TAG" "$SIGNERS")" || { stage_drop "the staged tag $S_TAG does not verify with this clone's docker/RELEASE_SIGNERS: $vk"; return 0; }
  S_KEY="$vk"
  tc="$(git -C "$STAGE_TREE" rev-parse -q --verify "refs/tags/$S_TAG^{commit}" 2>/dev/null)"
  [ "$tc" = "$S_COMMIT" ] || { stage_drop "the staged tag $S_TAG names another commit than its record"; return 0; }
  kv="$(gate "$(cat "$STAGE_TREE/docker/KIT_VERSION" 2>/dev/null)")"; ce="$(gate "$(cat "$STAGE_TREE/docker/CONSENSUS_EPOCH" 2>/dev/null)")"
  [ "$kv" = "$S_KIT" ] && [ "$ce" = "$S_EPOCH" ] || { stage_drop "the gates of the staged tree are not those of its record"; return 0; }
  plans_of_tree "$STAGE_TREE"
  STAGE_STATE=ok
  stage_tree_finishes "$STAGE_TREE" || { STAGE_STATE=incomplete; STAGE_WHY="$STAGE_TREE_WHY"; return 0; }
  if [ -z "$S_IMAGE" ]; then STAGE_STATE=incomplete; STAGE_WHY="no node image was prepared for $S_TAG ($(stage_val node_image_why))"; return 0; fi
  command -v docker >/dev/null 2>&1 || { STAGE_STATE=incomplete; STAGE_WHY="docker is not on this host, so whether the staged node image is here is not known"; return 0; }
  id="$(docker image inspect --format '{{.Id}}' "$S_IMAGE" 2>/dev/null | tr -d '\r')"
  [[ $id =~ $IMAGE_ID_RE ]] || { STAGE_STATE=incomplete; STAGE_WHY="the staged node image $S_IMAGE is not on this host"; return 0; }
  return 0
}
# stage_say -- what the stage holds, after stage_check or stage_candidate.
stage_say(){
  case "$STAGE_STATE" in
    removed) say "[kit-update] the staged release was removed: $STAGE_WHY"; return 0 ;;
    ok|incomplete) : ;;
    *) return 0 ;;
  esac
  say "[kit-update] staged: $S_TAG ($S_COMMIT), kit $S_KIT, epoch $S_EPOCH, in $STAGE_DIR; tag verified with this clone's docker/RELEASE_SIGNERS (key ${S_KEY:-?})"
  case "$PLANS_STATE" in
    read) say "  plans      $PLANS (chain/app/upgrades.go::upgradePlans of the staged tree)" ;;
    *) say "  plans      none read ($PLANS_WHY): node_upgrade_watch.sh acts on no halt for this stage, and the operator moves the kit by hand" ;;
  esac
  if [ -n "$STAGE_TREE_WHY" ]; then
    say "  not acted on: $STAGE_TREE_WHY. node_upgrade_watch.sh does not act on this stage, and no node image is prepared for it; the release has to be cut again from a tree that carries both"
    return 0
  fi
  case "$STAGE_STATE:$S_IMAGE_KIND" in
    ok:pin) say "  node image $S_IMAGE (pinned by docker/NODE_IMAGE of the staged tree, pulled)" ;;
    ok:build) say "  node image $S_IMAGE (built from the staged tree, tagged dendra/node:upgrade-$S_TAG)" ;;
    *) say "  node image not prepared: $STAGE_WHY. node_upgrade_watch.sh does not act on a stage without it; the next daily run prepares it again" ;;
  esac
  say "  at the halt of one of these plans, node_upgrade_watch.sh moves the clone to $S_TAG, starts the node on that image and replays the recorded join with --after-upgrade <plan>"
}
# stage_write -- the record of the stage being made, written whole into a temporary file and moved over the old one.
stage_write(){
  local t="$STAGE_REC.tmp"
  {
    printf 'tag=%s\n' "$CAND"
    printf 'commit=%s\n' "$CAND_COMMIT"
    printf 'kit_version=%s\n' "$KIT_CAND"
    printf 'consensus_epoch=%s\n' "$EPOCH_CAND"
    printf 'source_commit=%s\n' "$V_SRC"
    printf 'signing_key=%s\n' "$VKEY"
    printf 'signers_base=%s\n' "$SIG_SHA"
    printf 'signers_new=%s\n' "$NEW_SIG_SHA"
    printf 'rotation=%s\n' "$ROTATION"
    printf 'plans=%s\n' "$PLANS"
    printf 'node_image=%s\n' "$STAGE_IMAGE"
    printf 'node_image_kind=%s\n' "$STAGE_IMAGE_KIND"
    printf 'node_image_why=%s\n' "$STAGE_IMAGE_WHY"
    printf 'staged_at=%s\n' "$(TS)"
  } > "$t" && mv -f "$t" "$STAGE_REC"
}
# stage_candidate -- CAND passed every bound and carries a higher epoch than this clone: staged now, or found staged
# already. The clone of the tag is made by git alone, from this clone (where the tag was fetched and verified), and
# its tag is verified again there before anything else is done with it.
stage_candidate(){
  local new="$STAGE_DIR/tree.new" vk="" keep=0
  if [ "$STAGE_STATE" = ok ] && [ "$S_TAG" = "$CAND" ] && [ "$S_COMMIT" = "$CAND_COMMIT" ]; then
    say "[kit-update] $CAND is staged already, and verified again by this run."
    stage_say; return 0
  fi
  if [ "$STAGE_STATE" = incomplete ] && [ "$S_TAG" = "$CAND" ] && [ "$S_COMMIT" = "$CAND_COMMIT" ]; then
    keep=1; say "[kit-update] $CAND is staged without its node image ($STAGE_WHY): the image is prepared again."
  elif [ "$STAGE_STATE" = ok ] || [ "$STAGE_STATE" = incomplete ]; then
    say "[kit-update] the stage of $S_TAG is replaced by $CAND, the newest release of a higher epoch."
    rm -rf "$STAGE_DIR"
  fi
  mkdir -p "$STAGE_DIR" || { STAGE_STATE=failed; STAGE_WHY="$STAGE_DIR could not be made"; return 1; }
  if [ "$keep" = 0 ]; then
    rm -rf "$new" "$STAGE_TREE"
    if ! { git -c init.defaultBranch=stage init -q "$new" && git -C "$new" fetch -q --no-tags "$REPO" "refs/tags/$CAND:refs/tags/$CAND" \
           && git -C "$new" -c advice.detachedHead=false checkout -q --detach "refs/tags/$CAND"; } >/dev/null 2>&1; then
      rm -rf "$STAGE_DIR"; STAGE_STATE=failed; STAGE_WHY="the clone of $CAND under $STAGE_DIR could not be made (git init, a fetch from this clone, a checkout)"; return 1
    fi
    if [ "$(git -C "$new" rev-parse -q --verify HEAD 2>/dev/null)" != "$CAND_COMMIT" ] || ! vk="$(stage_tag_verified "$new" "$CAND" "$SIGNERS")"; then
      rm -rf "$STAGE_DIR"; STAGE_STATE=failed; STAGE_WHY="the staged clone of $CAND does not hold the tag this run verified (${vk:-another commit})"; return 1
    fi
    mv "$new" "$STAGE_TREE" || { rm -rf "$STAGE_DIR"; STAGE_STATE=failed; STAGE_WHY="the staged clone could not be put in place"; return 1; }
  fi
  plans_of_tree "$STAGE_TREE"
  # Nothing is pulled or built for a tree that cannot finish the upgrade: the watch never acts on it.
  if stage_tree_finishes "$STAGE_TREE"; then
    stage_image "$STAGE_TREE"
  else
    STAGE_IMAGE=""; STAGE_IMAGE_KIND=none; STAGE_IMAGE_WHY="$STAGE_TREE_WHY"
  fi
  stage_write || { rm -rf "$STAGE_DIR"; STAGE_STATE=failed; STAGE_WHY="the record $STAGE_REC could not be written"; return 1; }
  stage_check
  stage_say
  [ "$STAGE_STATE" = ok ] || [ "$STAGE_STATE" = incomplete ]
}
# --stage-check: the stage verified again (stage_check) and answered as key=value lines, for node_upgrade_watch.sh.
# It takes no lock (its caller holds it) and moves nothing; a stage that no longer holds is removed, as by a daily run.
stage_check_mode(){
  local t rec k v
  for t in git ssh-keygen sha256sum; do
    command -v "$t" >/dev/null 2>&1 || { printf 'stage=unmeasured\nwhy=%s is not installed\n' "$t"; return 3; }
  done
  git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { printf 'stage=unmeasured\nwhy=%s is not a git clone\n' "$REPO"; return 3; }
  EPOCH_NOW="$(gate "$(cat "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null)")"; SIGNERS="$REPO/docker/RELEASE_SIGNERS"
  [ -n "$EPOCH_NOW" ] && [ -r "$SIGNERS" ] || { printf 'stage=unmeasured\nwhy=this clone has no readable docker/CONSENSUS_EPOCH or docker/RELEASE_SIGNERS\n'; return 3; }
  SIG_SHA="$(sha256sum "$SIGNERS" | cut -d' ' -f1)"; rec="$(kitval DENDRA_RELEASE_SIGNERS_SHA256)"
  if [ -n "$rec" ] && [ "$rec" != "$SIG_SHA" ]; then
    printf 'stage=unmeasured\nwhy=docker/RELEASE_SIGNERS of this clone is not the file this kit recorded, and nothing is verified with it\n'; return 3
  fi
  stage_check
  printf 'stage=%s\nwhy=%s\n' "$STAGE_STATE" "$STAGE_WHY"
  for k in tag:S_TAG commit:S_COMMIT kit_version:S_KIT consensus_epoch:S_EPOCH plans:PLANS plans_state:PLANS_STATE plans_why:PLANS_WHY \
           node_image:S_IMAGE node_image_kind:S_IMAGE_KIND node_image_built_for:S_BUILT_FOR rotation:S_ROTATION \
           signers_new:S_SIGNERS_NEW signing_key:S_KEY; do
    v="${k#*:}"; printf '%s=%s\n' "${k%%:*}" "${!v}"
  done
  [ "$STAGE_STATE" = ok ] && return 0
  return 2
}

[ "$MODE" = stage-check ] || say "== [kit-update] $(TS) mode $MODE, clone $REPO =="
[ -r "$KIT/.env" ] || refuse "no kit here ($KIT/.env): nothing to update."
if [ "$MODE" = stage-check ]; then stage_check_mode; exit $?; fi
AUTO="$(kitval DENDRA_AUTO_UPDATE)"
case "$AUTO" in
  0) say "[kit-update] automatic update is off (DENDRA_AUTO_UPDATE=0 in $KIT/.env): nothing is done. To turn it on: bash $REPO/deploy/join.sh --auto-update, with the options you used."; exit 0 ;;
  ""|1) : ;;
  *) refuse "DENDRA_AUTO_UPDATE=$AUTO in $KIT/.env is neither 0 nor 1." ;;
esac
RERUN="$(kitval DENDRA_KIT_RERUN)"
[ -n "$RERUN" ] || refuse "no recorded re-run in $KIT/.env (DENDRA_KIT_RERUN): the kit was set up by a join.sh older than this file, or the line was not written. Run deploy/join.sh once by hand, with your options; it records the re-run."
[[ $RERUN =~ $RERUN_RE ]] || refuse "the recorded re-run holds a character this file does not hand to a shell (allowed: letters, digits, space and : / . _ ~ % ? = + @ , -). Re-run deploy/join.sh by hand."
case "$RERUN" in *" deploy/join.sh"*|*/deploy/join.sh*) : ;; *) refuse "the recorded re-run does not run deploy/join.sh: $RERUN" ;; esac
# Three options describe one run and never a replay. --accept-new-signers accepts any docker/RELEASE_SIGNERS without
# an operator reading its fingerprints: a recorded line that carries it is refused, never cleaned. --judge and
# --no-judge name the seat of the run that recorded them; the seat is in the kit's .env now (DENDRA_NO_JUDGE,
# DENDRA_MINER_JUDGE, read by deploy/join.sh), so a line written by an older join.sh is replayed without them, and
# a note in the log says so: replayed as recorded, a --judge would re-seat a judge that judge.sh off had cleared.
case " $RERUN " in
  *" --accept-new-signers "*) refuse "the recorded re-run carries --accept-new-signers, a gesture of one run that is never replayed: replayed daily it would accept every change of docker/RELEASE_SIGNERS without an operator reading the fingerprints. Re-run deploy/join.sh by hand without that option; it records the re-run again." ;;
esac
RERUN_CLEAN="$(printf '%s\n' "$RERUN" | awk '{ o = ""; for (i = 1; i <= NF; i++) if ($i != "--judge" && $i != "--no-judge") o = o (o == "" ? "" : " ") $i; print o }')"
if [ "$RERUN_CLEAN" != "$RERUN" ]; then
  say "[kit-update] note: the recorded re-run carried --judge or --no-judge, written by a join.sh older than this file; it is replayed without them. The judge seat is read from the kit's .env (DENDRA_NO_JUDGE, DENDRA_MINER_JUDGE), so judge.sh on and off hold across updates."
  RERUN="$RERUN_CLEAN"
fi
# Whether the recorded join runs a node of its own: deploy/join.sh does by default, not under --remote-rpc or
# --no-node, and the last of these options and --own-node wins, as in its argument loop. Read as words, never globbed.
OWN_NODE=1
read -r -a RERUN_WORDS <<< "$RERUN"
for w in "${RERUN_WORDS[@]}"; do case "$w" in --own-node) OWN_NODE=1 ;; --remote-rpc|--no-node) OWN_NODE=0 ;; esac; done
# --after-upgrade <plan> for the join of this run only: set by remote_upgrade, or read back from an interrupted one.
AFTER_UPGRADE=""; AFTER_HEIGHT=""

# ---------------------------------------------------------------- the tools and the lock
for t in git ssh-keygen sha256sum; do command -v "$t" >/dev/null 2>&1 || unmeasured "$t is not installed: without it no tag can be read or verified."; done
LOCK="$KIT/.kit-update.lock"
lock_take "$LOCK" || refuse "another run holds $LOCK (this file or node_upgrade_watch.sh, in progress)."
trap 'lock_release "$LOCK"' EXIT
[ "$LOCK_WAS_STALE" = 1 ] && say "[kit-update] a stale lock (its run is gone, or it is older than six hours) was removed."

# ---------------------------------------------------------------- this clone
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || refuse "$REPO is not a git clone: the kit cannot be moved to a tag."
DIRTY="$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)"
[ -z "$DIRTY" ] || refuse "the clone carries local changes to tracked files; an update would carry or lose them:$(printf '\n%s' "$DIRTY" | head -5)"
HEAD_NOW="$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)"
[ -n "$HEAD_NOW" ] || refuse "the clone has no readable HEAD."
BRANCH_NOW="$(git -C "$REPO" symbolic-ref -q --short HEAD 2>/dev/null || true)"
TAG_NOW="$(git -C "$REPO" describe --exact-match --tags HEAD 2>/dev/null | head -1 | tr -d '\r')"
[[ $TAG_NOW =~ $RELEASE_TAG_RE ]] || TAG_NOW=""
KIT_NOW="$(gate "$(cat "$REPO/docker/KIT_VERSION" 2>/dev/null)")"
EPOCH_NOW="$(gate "$(cat "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null)")"
[ -n "$KIT_NOW" ] && [ -n "$EPOCH_NOW" ] || refuse "the gates of this clone (docker/KIT_VERSION, docker/CONSENSUS_EPOCH) cannot be read."
SIGNERS="$REPO/docker/RELEASE_SIGNERS"
[ -r "$SIGNERS" ] || refuse "docker/RELEASE_SIGNERS is absent from this clone: a release tag cannot be verified, and a signature is required before anything moves (ADR-058 decision 4)."
SIG_SHA="$(sha256sum "$SIGNERS" | cut -d' ' -f1)"
SIG_REC="$(kitval DENDRA_RELEASE_SIGNERS_SHA256)"
ROTATION=""; NEW_SIG_SHA="$SIG_SHA"

# ---------------------------------------------------------------- the pieces of a move (used by a new update and by a resumed one)
# The decision the new tree may take on this machine (ADR-058 decision 5): a machine with no usable GPU below the
# judge floor is refused by deploy/hw_probe.sh --role of the new tree. Read before the join, so that its refusal
# is told apart from a failure: this one is not rolled back.
read_decision(){
  DECISION=0
  if [ -r "$REPO/deploy/hw_probe.sh" ]; then
    RW="$(bash "$REPO/deploy/hw_probe.sh" --role 2>/dev/null | head -1 | tr -d '[:space:]')"; RRC=$?
    [ "$RRC:$RW" = "0:refused" ] && DECISION=1
  fi
}

# heartbeat_fresh <since> -> 0 when the miner's status file was written at or after <since>; 1 not yet; 3 unread
# (no docker, no container, no python3, no file). The file is the daemon's own (modea/heartbeat.py::status_path).
STATUS_FILE="${DENDRA_STATUS_FILE:-/tmp/dendra-miner-status.json}"
heartbeat_fresh(){
  local cid doc
  command -v docker >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 || return 3
  cid="$(docker ps -q --filter label=com.docker.compose.project=dendra-miner --filter label=com.docker.compose.service=miner 2>/dev/null | head -1)"
  [ -n "$cid" ] || return 1
  doc="$(docker exec "$cid" cat "$STATUS_FILE" 2>/dev/null)" || return 1
  python3 -I -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    w = int(d.get("written_at", 0))
except Exception:
    sys.exit(3)
sys.exit(0 if w >= int(sys.argv[2]) else 1)' "$doc" "$1"
}
wait_heartbeat(){ # wait_heartbeat <since> -> fresh | absent | unread
  local bound="${DENDRA_KIT_UPDATE_HEARTBEAT_WAIT_S:-600}" waited=0 rc=1 unread=0
  case "$bound" in ""|*[!0-9]*) bound=600 ;; esac
  while :; do
    heartbeat_fresh "$1"; rc=$?
    [ "$rc" = 0 ] && { printf 'fresh'; return 0; }
    [ "$rc" = 3 ] && unread=1
    [ "$waited" -ge "$bound" ] && break
    sleep 10; waited=$((waited + 10))
  done
  if [ "$rc" = 3 ] && [ "$unread" = 1 ]; then printf 'unread'; else printf 'absent'; fi
}

run_rerun(){ # run_rerun -> the exit code of the recorded re-run (with --after-upgrade when set), output appended to the log
  local rc line="$RERUN${AFTER_UPGRADE:+ --after-upgrade $AFTER_UPGRADE}"
  if [ "$MODE" = cron ]; then ( cd "$REPO" && eval "$line" ); rc=$?
  else ( cd "$REPO" && eval "$line" ) 2>&1 | tee -a "$LOG"; rc=${PIPESTATUS[0]}; fi
  return "$rc"
}

# record_signers <sha> -- the signers record of the kit, after an accepted rotation (the new file) or on a rollback
# (the record the kit held before: restored, or dropped when it held none).
record_signers(){
  if [ -n "$1" ]; then kitset DENDRA_RELEASE_SIGNERS_SHA256 "$1"; else kitset DENDRA_RELEASE_SIGNERS_SHA256 "" unset; fi
}

rollback(){ # rollback <why> -> exit 1 always; the previous tree is restored and its join replayed
  local rc
  say "[kit-update] $1: going back to kit-before-update ($HEAD_NOW)."
  if [ -n "$BRANCH_NOW" ]; then git -C "$REPO" checkout -q -B "$BRANCH_NOW" kit-before-update 2>&1 | head -3
  else git -C "$REPO" checkout -q --detach kit-before-update 2>&1 | head -3; fi
  if [ "$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)" != "$HEAD_NOW" ]; then
    failed "the rollback did not put the clone back on $HEAD_NOW (the marker $PENDING stays, and the next run tries again): git checkout kit-before-update by hand, then $RERUN"
  fi
  if [ -n "$ROTATION" ]; then record_signers "$SIG_REC"; say "[kit-update] the signers record of the kit is back to what it held before the rotation (${SIG_REC:-none})."; fi
  rm -f "$PENDING"
  say "[kit-update] the previous tree is back. Replaying the recorded join from it."
  # The previous tree is not ahead of the network: the option of the upgrade does not follow it back.
  AFTER_UPGRADE=""
  run_rerun; rc=$?
  say "[kit-update] rollback join exited $rc. The update to $CAND failed and was undone; the kit runs its previous tree. Read $LOG, then update by hand when the cause is known."
  exit 1
}

# replay_and_judge -- the clone sits on the candidate: the recorded join is replayed and judged by the miner's
# heartbeat; every way out removes the marker, except a rollback that did not restore the tree.
replay_and_judge(){
  local SINCE JRC HB
  if [ -n "$ROTATION" ]; then record_signers "$NEW_SIG_SHA"; say "[kit-update] the kit's signers record now names the rotated file ($NEW_SIG_SHA); the join verifies with it."; fi
  read_decision
  SINCE="$(date +%s)"
  run_rerun; JRC=$?
  say "[kit-update] the recorded join exited $JRC."
  case "$JRC" in
    0|3)
      HB="$(wait_heartbeat "$SINCE")"
      case "$HB" in
        fresh) rm -f "$PENDING"; say "[kit-update] updated to $CAND: the miner wrote its heartbeat after the restart. Done."; exit 0 ;;
        unread)
          if [ "$JRC" = 0 ]; then rm -f "$PENDING"; say "[kit-update] updated to $CAND: the join reported the miner healthy; its heartbeat file was not read from here (docker or python3 missing on this host, or no status file), so that reading is not measured. Done."; exit 0; fi
          rollback "the join did not measure the miner (exit 3) and its heartbeat could not be read within the bound" ;;
        *) rollback "no heartbeat from the miner within ${DENDRA_KIT_UPDATE_HEARTBEAT_WAIT_S:-600} s of the restart (join exit $JRC)" ;;
      esac ;;
    2)
      if [ "$DECISION" = 1 ]; then
        rm -f "$PENDING"
        say "[kit-update] the new kit refuses this machine on a decision of the network, not on a failure: deploy/hw_probe.sh --role of $CAND says refused (no usable GPU below the judge floor). The previous tree is not restored, since it would mine where the network ends mining."
        say "[kit-update] exit procedure (keeps your stake): read  bash $KIT/exit-miner.sh --help  then run it; the bond is returned by the exit."
        # Every identity of the kit is stopped, each through slots.sh (the copy of the tree now checked out): a
        # compose command typed here would stop slot 0 only, and a rig's other cards would keep mining.
        if command -v docker >/dev/null 2>&1; then
          if { [ -r "$KIT/slots.sh" ] && . "$KIT/slots.sh" && declare -F slot_compose >/dev/null 2>&1; } && KSLOTS="$(slot_ids --all)"; then
            for k in $KSLOTS; do slot_compose "$k" stop 2>&1 | head -5; done
            say "[kit-update] the miner's containers are stopped, every identity of this kit (slots: $(echo $KSLOTS)): $(slot_run_hint 0 stop)."
          else
            say "[kit-update] the miner's containers were left running: the slot library ($KIT/slots.sh) or this kit's slots could not be read. Stop each identity with bash $KIT/slots.sh run <k> stop."
          fi
        fi
        exit 2
      fi
      rollback "the join refused on the new tree (exit 2)" ;;
    *) rollback "the join failed on the new tree (exit $JRC)" ;;
  esac
}

# ---------------------------------------------------------------- an update interrupted after its checkout
# The marker names the candidate, its commit, the tree the clone left and the signers record: a run that finds
# it finishes that update instead of reading a clone already on the new tag as nothing to do (the containers
# would run the old kit, the .env would not be rewritten, and the daemon would show Update available for good).
pval(){ sed -n "s/^$1=//p" "$PENDING" 2>/dev/null | head -1 | tr -d '\r'; }
if [ -r "$PENDING" ]; then
  P_CAND="$(pval candidate)"; P_COMMIT="$(pval candidate_commit)"; P_FROM="$(pval from)"; P_BRANCH="$(pval branch)"
  P_REC="$(pval signers_record)"; P_ROT="$(pval rotation)"; P_NEW="$(pval signers_new)"; P_AFTER="$(pval after_upgrade)"
  # A marker that names an upgrade plan is node_upgrade_watch.sh's: the node stopped at that plan's height, and
  # nothing is rolled back past it (the binary before the upgrade cannot continue). That file finishes it.
  if [ -n "$(pval upgrade_plan)" ]; then
    say "[kit-update] a governed upgrade to ${P_CAND:-?} (plan $(pval upgrade_plan), height $(pval upgrade_height)) is in progress: node_upgrade_watch.sh finishes it, and nothing is rolled back past the halt. Nothing moved; its log is $KIT/node-upgrade-watch.log (joins run after the upgrade: $(pval join_attempts | tr -dc 0-9 | grep . || echo 0); once they reach its bound, the log names the join to run by hand)."
    exit 0
  fi
  say "[kit-update] an update to ${P_CAND:-?} was interrupted after its checkout (marker $PENDING, written $(date -u -r "$PENDING" +%FT%TZ 2>/dev/null || echo 'at an unread time')): the clone was moved and the join may not have run."
  if [ -z "$P_CAND" ] || [ -z "$P_COMMIT" ] || [ -z "$P_FROM" ]; then
    refuse "the marker is unreadable (candidate, candidate_commit or from missing): finish by hand, then remove it. From $REPO: git checkout --detach refs/tags/<the candidate>, then $RERUN; or git checkout kit-before-update, then the same re-run."
  fi
  if [ "$MODE" = plan ]; then say "[kit-update] --plan: the next run without --plan replays the recorded join from $P_CAND when the clone sits on it, or brings the clone back to $P_FROM. Nothing moved."; exit 0; fi
  if [ -n "$P_AFTER" ] && ! [[ $P_AFTER =~ $PLAN_NAME_RE ]]; then
    refuse "the marker names an upgrade plan outside the alphabet of a plan name (after_upgrade): finish by hand, then remove it."
  fi
  CAND="$P_CAND"; CAND_COMMIT="$P_COMMIT"; HEAD_NOW="$P_FROM"; BRANCH_NOW="$P_BRANCH"; SIG_REC="$P_REC"; ROTATION="$P_ROT"; NEW_SIG_SHA="${P_NEW:-$SIG_SHA}"
  AFTER_UPGRADE="$P_AFTER"
  if [ "$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)" = "$CAND_COMMIT" ]; then
    say "[kit-update] the clone sits on $CAND: replaying the recorded join from it, as the interrupted run would have."
    replay_and_judge
  fi
  rollback "the clone sits on neither $CAND nor the tree it left"
fi

if [ -n "$SIG_REC" ] && [ "$SIG_REC" != "$SIG_SHA" ]; then
  refuse "docker/RELEASE_SIGNERS of this clone (sha256 $SIG_SHA) is not the file this kit recorded ($SIG_REC): the trust root changed under the kit. deploy/join.sh --accept-new-signers records a new one on purpose; nothing is verified with a file the kit did not record."
fi
[ -n "$SIG_REC" ] || say "[kit-update] note: this kit recorded no signers file yet (DENDRA_RELEASE_SIGNERS_SHA256 absent); the clone's docker/RELEASE_SIGNERS ($SIG_SHA) is used, and deploy/join.sh records it at its next run."
# The staged release, verified again at every run: one that no longer holds is removed here, and said.
stage_check
if [ "$OWN_NODE" = 0 ] && { [ "$STAGE_STATE" = ok ] || [ "$STAGE_STATE" = incomplete ]; }; then
  stage_drop "this kit runs no node of its own (--remote-rpc in its recorded join), so no node of it runs the staged image; a release of a higher epoch is applied here once the chain answers its plan applied"
fi
case "$STAGE_STATE" in
  removed) stage_say ;;
  ok|incomplete) say "[kit-update] staged: $S_TAG, epoch $S_EPOCH$([ "$STAGE_STATE" = ok ] || printf ', its node image missing'), applied by node_upgrade_watch.sh at the halt of one of its plans (${PLANS:-none read})." ;;
esac
say "[kit-update] this clone:${TAG_NOW:-not at a release tag}${BRANCH_NOW:+ (branch $BRANCH_NOW)} $HEAD_NOW, kit $KIT_NOW, consensus epoch $EPOCH_NOW"

# ---------------------------------------------------------------- the candidate: the newest release tag origin publishes
LSR="$(git -C "$REPO" ls-remote --tags origin 2>/dev/null)"; LSR_RC=$?
[ "$LSR_RC" = 0 ] || unmeasured "origin did not answer git ls-remote (exit $LSR_RC): whether a newer release exists is unknown."
CAND="$(printf '%s\n' "$LSR" | awk 'NF >= 2 { print $2 }' | sed -n 's#^refs/tags/##p' | grep -v '[{][}]$' \
       | grep -E "$RELEASE_TAG_RE" | sort -V | tail -1)"
[ -n "$CAND" ] || { say "[kit-update] origin publishes no release tag of the form vMAJOR.MINOR.PATCH: nothing to update to."; exit 0; }
if [ "$CAND" = "$TAG_NOW" ]; then say "[kit-update] this clone sits on $CAND, the newest release origin publishes: nothing to do."; exit 0; fi
# The tag alone, fetched by its full name. git refuses to move a tag this clone already holds under that name,
# so a tag that changed upstream is a refusal here, never a silent update. Not --quiet: git's reason is kept.
if ! FE="$(git -C "$REPO" fetch --no-tags origin "refs/tags/$CAND:refs/tags/$CAND" 2>&1)"; then
  refuse "the tag $CAND could not be fetched, or it differs from the $CAND this clone already holds (a tag that moved upstream is not followed). git said: $(printf '%s' "$FE" | grep -v '^From ' | head -3 | tr '\n' ' ')"
fi
[ "$(git -C "$REPO" cat-file -t "refs/tags/$CAND" 2>/dev/null)" = tag ] || refuse "$CAND is not an annotated tag: it carries no signature to verify."
CAND_COMMIT="$(git -C "$REPO" rev-parse -q --verify "refs/tags/$CAND^{commit}" 2>/dev/null)"
[ -n "$CAND_COMMIT" ] || refuse "the commit of $CAND cannot be read."
[ "$CAND_COMMIT" != "$HEAD_NOW" ] || { say "[kit-update] this clone already carries the tree of $CAND: nothing to do."; exit 0; }
KIT_CAND="$(gate "$(git -C "$REPO" show "refs/tags/$CAND:docker/KIT_VERSION" 2>/dev/null)")"
EPOCH_CAND="$(gate "$(git -C "$REPO" show "refs/tags/$CAND:docker/CONSENSUS_EPOCH" 2>/dev/null)")"
[ -n "$KIT_CAND" ] && [ -n "$EPOCH_CAND" ] || refuse "the gates of $CAND (docker/KIT_VERSION, docker/CONSENSUS_EPOCH) cannot be read from its tree."
# Another epoch is a new node binary and a governed upgrade, never applied here. A lower one is refused; a higher one
# goes through every bound below like any candidate, then is staged (stage_candidate) and the run ends with exit 2.
STAGE=0
if [ "$EPOCH_CAND" != "$EPOCH_NOW" ]; then
  [ "$EPOCH_CAND" -gt "$EPOCH_NOW" ] 2>/dev/null \
    || refuse "$CAND carries consensus epoch $EPOCH_CAND and this clone runs $EPOCH_NOW: another epoch is a new node binary and a governed upgrade, never an automatic act. Read the announcement of $CAND and the runbook before moving."
  STAGE=1
fi
if [ "$STAGE" = 0 ] && [ "$KIT_CAND" -le "$KIT_NOW" ] 2>/dev/null; then
  say "[kit-update] the newest release $CAND carries kit $KIT_CAND and this clone runs kit $KIT_NOW: nothing newer to move to."; exit 0
fi
# The candidate's VERSION: the tree's own account of its gates and of the commit it was generated from. A tree
# whose VERSION disagrees with its gates, or whose source_commit is not the 40 hexadecimal digits of a commit
# (the generator's fallback reads unknown, a hand-made file reads anything), is refused even under a good signature.
VER_CAND="$(git -C "$REPO" show "refs/tags/$CAND:VERSION" 2>/dev/null | tr -d '\r')"
[ -n "$VER_CAND" ] || refuse "$CAND carries no VERSION file: not a tree the generator published."
V_KIT="$(printf '%s\n' "$VER_CAND" | sed -n 's/^kit_version=//p' | head -1 | tr -dc 0-9)"
V_EPOCH="$(printf '%s\n' "$VER_CAND" | sed -n 's/^consensus_epoch=//p' | head -1 | tr -dc 0-9)"
V_SRC="$(printf '%s\n' "$VER_CAND" | sed -n 's/^source_commit=//p' | head -1)"
[ "$V_KIT" = "$KIT_CAND" ] && [ "$V_EPOCH" = "$EPOCH_CAND" ] || refuse "the VERSION of $CAND (kit ${V_KIT:-?}, epoch ${V_EPOCH:-?}) disagrees with its gates (kit $KIT_CAND, epoch $EPOCH_CAND): not a tree the generator certified."
if [ "${#V_SRC}" != 40 ] || [ -n "$(printf '%s' "$V_SRC" | tr -d '0-9a-f')" ]; then
  refuse "the VERSION of $CAND names no committed source (source_commit=${V_SRC:-absent}; the generator writes the 40 hexadecimal digits of the last commit that touched the published content): refused even under a good signature."
fi
# The signature, with this clone's file and nothing else. Two gates, the second the hard one: the tag's signature
# block must be an SSH one, and git verifies with gpg.program pointed at /bin/false, so a tag signed with gpg (which git
# would otherwise check against this host's gpg keyring, where docker/RELEASE_SIGNERS counts for nothing) fails.
tag_sig_format(){ # tag_sig_format <ref> -> ssh | pgp | none | other: the kind of the last signature block of the tag object
  local b d='-----'  # the dashes of a block line are composed here, so no key-block marker is written in this file
  b="$(git -C "$REPO" cat-file tag "$1" 2>/dev/null | grep "^${d}BEGIN " | tail -1)"
  case "$b" in
    "${d}BEGIN SSH SIGNATURE${d}") printf 'ssh' ;;
    "${d}BEGIN PGP SIGNATURE${d}") printf 'pgp' ;;
    "") printf 'none' ;;
    *) printf 'other' ;;
  esac
}
SIGFMT="$(tag_sig_format "refs/tags/$CAND")"
case "$SIGFMT" in
  ssh) : ;;
  pgp) refuse "the tag $CAND carries a gpg signature: the project signs its releases with an SSH key named in docker/RELEASE_SIGNERS, and nothing else is verified (a gpg signature would be checked against this host's gpg keyring, not against that file)." ;;
  none) refuse "the tag $CAND carries no signature block." ;;
  *) refuse "the tag $CAND carries a signature block of a kind this file does not verify (only an SSH signature, named in docker/RELEASE_SIGNERS)." ;;
esac
if ! VOUT="$(git -C "$REPO" -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile="$SIGNERS" verify-tag "refs/tags/$CAND" 2>&1)"; then
  refuse "the tag $CAND does not verify with this clone's docker/RELEASE_SIGNERS. git said: $(printf '%s' "$VOUT" | head -3 | tr '\n' ' ')"
fi
VKEY="$(printf '%s' "$VOUT" | grep -o 'SHA256:[A-Za-z0-9+/=]*' | head -1)"

# The candidate's own docker/RELEASE_SIGNERS (ADR-058 decision 4: a rotation adds a line and removes none for one
# release, then the old line goes). What is followed without a hand on the machine, two shapes and no other:
#   addition: every key line of this clone's file is still in the candidate's (the tag verified with this clone's
#             file, so a key the kit already trusts vouches for the new line);
#   removal:  every key line of the candidate's file is in this clone's, and the key that signed the tag is one of
#             them (nothing new is trusted; a line is dropped).
# A file that adds and removes at once, a file without a key, or any other change is a change of trust root, and
# stays a gesture on the machine. The join from the new tree is what refuses a file the kit did not record, so an
# accepted rotation is recorded before the join (replay_and_judge) and the record restored on a rollback.
keylines(){ awk '!/^[[:space:]]*(#|$)/ { for (i = 2; i <= NF; i++) if ($i ~ /^(sk-)?(ssh|ecdsa)-/) { print $1, $i, $(i + 1); break } }' | tr -d '\r' | sort -u; }
lines_absent(){ # lines_absent <haystack>; stdin: lines -> the lines not in <haystack>
  local l; while IFS= read -r l; do [ -n "$l" ] || continue; printf '%s\n' "$1" | grep -qxF -- "$l" || printf '%s\n' "$l"; done; }
fp_of_lines(){ # stdin: "principal keytype key" lines -> "SHA256:<fingerprint> principal" lines
  local t p k v fp; t="$(mktemp)" || return 1
  while read -r p k v; do [ -n "$v" ] || continue; printf '%s %s\n' "$k" "$v" > "$t"; fp="$(ssh-keygen -lf "$t" 2>/dev/null | awk '{ print $2 }')"; [ -n "$fp" ] && printf '%s %s\n' "$fp" "$p"; done
  rm -f "$t"; return 0
}
git -C "$REPO" cat-file -e "refs/tags/$CAND:docker/RELEASE_SIGNERS" 2>/dev/null || refuse "$CAND carries no docker/RELEASE_SIGNERS: the join from its tree could verify no release and would refuse it (deploy/join.sh::verify_release_signers)."
NEW_SIG_SHA="$(git -C "$REPO" show "refs/tags/$CAND:docker/RELEASE_SIGNERS" 2>/dev/null | sha256sum | cut -d' ' -f1)"
ROT_SAID=""
if [ "$NEW_SIG_SHA" != "$SIG_SHA" ]; then
  OLD_KEYS="$(keylines < "$SIGNERS")"
  NEW_KEYS="$(git -C "$REPO" show "refs/tags/$CAND:docker/RELEASE_SIGNERS" 2>/dev/null | keylines)"
  [ -n "$NEW_KEYS" ] || refuse "the docker/RELEASE_SIGNERS of $CAND names no key: a tree that could verify no release is not moved to."
  MISSING="$(printf '%s\n' "$OLD_KEYS" | lines_absent "$NEW_KEYS")"
  ADDED="$(printf '%s\n' "$NEW_KEYS" | lines_absent "$OLD_KEYS")"
  NEW_FPS="$(printf '%s\n' "$NEW_KEYS" | fp_of_lines)"
  if [ -z "$MISSING" ]; then
    ROTATION=addition
    ROT_SAID="$(printf '%s\n' "$ADDED" | fp_of_lines | tr '\n' ' ')"
  elif [ -z "$ADDED" ] && [ -n "$VKEY" ] && printf '%s\n' "$NEW_FPS" | grep -qF -- "$VKEY "; then
    ROTATION=removal
    ROT_SAID="$(printf '%s\n' "$MISSING" | fp_of_lines | tr '\n' ' ')"
  else
    refuse "the docker/RELEASE_SIGNERS of $CAND is neither this kit's file nor a rotation it follows by itself (lines added and none removed; or lines removed, none added, the signing key kept). Added: $(printf '%s\n' "$ADDED" | fp_of_lines | tr '\n' ' ')removed: $(printf '%s\n' "$MISSING" | fp_of_lines | tr '\n' ' ')signed by ${VKEY:-?}. Compare the fingerprints with the ones the project announced, then: git checkout --detach refs/tags/$CAND and $RERUN --accept-new-signers, by hand."
  fi
fi

# ---------------------------------------------------------------- a higher epoch, for a kit without a node of its own
# applied_height_via_rpc <rpc> <plan> -> the height a chain RPC answers for the plan (x/upgrade AppliedPlan, asked by
# /abci_query): a positive integer once applied, 0 when the answer carries no height (proto3 omits a zero: never
# applied), ? when the RPC did not answer, refused the query or answered something else (neither curl nor python3 on
# this host gives ?). The same text, byte for byte, as deploy/join.sh::applied_height_via_rpc: the bench holds them equal.
applied_height_via_rpc(){
  local rpc="$1" data doc v
  case "$rpc" in tcp://*) rpc="http://${rpc#tcp://}" ;; http://*|https://*) : ;; *) printf '?'; return 0 ;; esac
  if ! [[ $2 =~ ^[A-Za-z0-9._-]{1,127}$ ]] || ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    printf '?'; return 0
  fi
  # QueryAppliedPlanRequest{name}: field 1, a string shorter than 128 bytes, so its length is one byte.
  data="$(python3 -I -c 'import sys; b = sys.argv[1].encode(); print("0a%02x%s" % (len(b), b.hex()))' "$2")"
  doc="$(curl -fsS -m 10 "$rpc/abci_query?path=%22/cosmos.upgrade.v1beta1.Query/AppliedPlan%22&data=0x$data" 2>/dev/null)" || { printf '?'; return 0; }
  v="$(printf '%s' "$doc" | python3 -I -c '
import base64, json, sys
def uv(b, i):
    x, s = 0, 0
    while True:
        if i >= len(b) or s > 63:
            raise ValueError("varint")
        c = b[i]
        i += 1
        x |= (c & 127) << s
        s += 7
        if c < 128:
            return x, i
try:
    r = json.loads(sys.stdin.read())["result"]["response"]
    if not isinstance(r, dict) or r.get("code", 0) != 0:
        raise ValueError("refused")
    b = base64.b64decode(r.get("value") or "", validate=True)
    h, i = 0, 0
    if b:
        k, i = uv(b, 0)
        if k != 8:
            raise ValueError("field")
        h, i = uv(b, i)
    if i != len(b):
        raise ValueError("trailing")
    print(h)
except (ValueError, KeyError, TypeError):
    print("?")' 2>/dev/null)"
  case "$v" in ''|*[!0-9]*) printf '?' ;; *) printf '%s' "$((10#$v))" ;; esac
}
# remote_upgrade -- CAND carries a higher epoch and this kit runs no node of its own: nothing is staged or built. The
# plans of CAND's tree (chain/app/upgrades.go, read from the verified tag) are asked of the chain this kit reads;
# the first one applied sets AFTER_UPGRADE and the run goes on as an update. Otherwise the run ends: 2 when none is
# applied yet (the next daily run asks again), 3 when the chain's answer was not read.
remote_upgrade(){
  local d rpc p h unread=""
  rpc="$(kitval DENDRA_NODE)"
  d="$(mktemp -d)" || unmeasured "a temporary directory could not be made."
  mkdir -p "$d/chain/app" && git -C "$REPO" show "refs/tags/$CAND:chain/app/upgrades.go" > "$d/chain/app/upgrades.go" 2>/dev/null \
    || rm -f "$d/chain/app/upgrades.go"
  plans_of_tree "$d"; rm -rf "$d"
  [ "$PLANS_STATE" = read ] || refuse "$CAND carries consensus epoch $EPOCH_CAND and this clone runs $EPOCH_NOW. This kit runs no node of its own, so it moves once the chain answers a plan of $CAND applied, and no plan of that tree could be read ($PLANS_WHY). Read the announcement of $CAND."
  [ -n "$rpc" ] || refuse "$CAND carries consensus epoch $EPOCH_CAND, this kit runs no node of its own, and its .env names no chain RPC (DENDRA_NODE) to ask whether the network applied its plan ($PLANS). Read the announcement of $CAND."
  for p in $PLANS; do
    h="$(applied_height_via_rpc "$rpc" "$p")"
    case "$h" in ''|*[!0-9]*) unread="${unread:+$unread }$p" ;; 0) : ;; *) AFTER_UPGRADE="$p"; AFTER_HEIGHT="$h"; return 0 ;; esac
  done
  [ -z "$unread" ] || unmeasured "$CAND carries consensus epoch $EPOCH_CAND, and whether the chain at $rpc applied its plan ($unread) was not read (no answer, or curl or python3 missing on this host)."
  refuse "$CAND carries consensus epoch $EPOCH_CAND, and the chain at $rpc has applied none of its plans ($PLANS) yet. This kit runs no node of its own, so nothing is staged: it moves at the first daily run after the network's upgrade."
}
if [ "$STAGE" = 1 ] && [ "$OWN_NODE" = 0 ]; then
  remote_upgrade
  STAGE=0
fi

# ---------------------------------------------------------------- a higher epoch: staged, never applied
if [ "$STAGE" = 1 ]; then
  if [ "$MODE" = plan ]; then
    say "[kit-update] --plan: $CAND (epoch $EPOCH_CAND, this clone $EPOCH_NOW) passes every bound and would be staged in $STAGE_DIR, never applied: node_upgrade_watch.sh applies it at the halt of one of its plans. Nothing moved."
    exit 0
  fi
  if stage_candidate; then STAGED_SAID="It is staged, not applied (above)"
  else STAGED_SAID="It could not be staged ($STAGE_WHY); the next daily run tries again"; fi
  refuse "$CAND carries consensus epoch $EPOCH_CAND and this clone runs $EPOCH_NOW: another epoch is a new node binary and a governed upgrade, never an automatic act. $STAGED_SAID; read the announcement of $CAND."
fi

# ---------------------------------------------------------------- the plan
say "[kit-update] plan:"
say "  from   ${TAG_NOW:-$HEAD_NOW}${BRANCH_NOW:+ (branch $BRANCH_NOW)}, kit $KIT_NOW, epoch $EPOCH_NOW"
say "  to     $CAND ($CAND_COMMIT), kit $KIT_CAND, epoch $EPOCH_CAND, source ${V_SRC:0:12}"
say "  tag    SSH signature verified with $SIGNERS (key ${VKEY:-?})"
case "$ROTATION" in
  "") say "  signers the candidate ships the same docker/RELEASE_SIGNERS ($SIG_SHA)" ;;
  addition) say "  signers rotation followed: $CAND adds the key(s) ${ROT_SAID}and removes none; the kit's record moves to $NEW_SIG_SHA before the join, back on a rollback" ;;
  removal) say "  signers rotation followed: $CAND removes the key(s) ${ROT_SAID}and adds none, the signing key stays; the kit's record moves to $NEW_SIG_SHA before the join, back on a rollback" ;;
esac
say "  1. git branch -f kit-before-update $HEAD_NOW   (the tree the clone leaves stays there until the next update)"
say "  2. git checkout --detach refs/tags/$CAND         (never a branch; the marker $PENDING is written first, removed when done or undone)"
[ -z "$AFTER_UPGRADE" ] || say "  epoch  $EPOCH_CAND, after the governed upgrade $AFTER_UPGRADE, applied at height $AFTER_HEIGHT on the chain this kit reads ($(kitval DENDRA_NODE)); this kit runs no node of its own, so the release is applied as an update"
say "  3. $RERUN${AFTER_UPGRADE:+ --after-upgrade $AFTER_UPGRADE}"
say "  4. wait for the miner's heartbeat (bounded ${DENDRA_KIT_UPDATE_HEARTBEAT_WAIT_S:-600} s); on a failed join or no heartbeat: back to kit-before-update, then step 3 again"
say "  log    $LOG"
[ "$MODE" = plan ] && { say "[kit-update] --plan: nothing moved."; exit 0; }

# ---------------------------------------------------------------- the move
git -C "$REPO" branch -f kit-before-update "$HEAD_NOW" >/dev/null 2>&1 || refuse "the tree the clone leaves could not be kept on kit-before-update."
printf 'candidate=%s\ncandidate_commit=%s\nfrom=%s\nbranch=%s\nsigners_record=%s\nsigners_new=%s\nrotation=%s\nwritten=%s\n' \
  "$CAND" "$CAND_COMMIT" "$HEAD_NOW" "$BRANCH_NOW" "$SIG_REC" "$NEW_SIG_SHA" "$ROTATION" "$(TS)" > "$PENDING" \
  || refuse "the marker $PENDING could not be written: an interrupted update could not be finished by the next run."
if [ -n "$AFTER_UPGRADE" ] && ! echo "after_upgrade=$AFTER_UPGRADE" >> "$PENDING"; then
  rm -f "$PENDING"; refuse "the marker $PENDING could not be written: an interrupted update could not be finished by the next run."
fi
if ! CO="$(git -C "$REPO" checkout -q --detach "refs/tags/$CAND" 2>&1)"; then
  rm -f "$PENDING"
  refuse "the tree of $CAND would overwrite files git does not track, or cannot be checked out. git said: $(printf '%s' "$CO" | head -4 | tr '\n' ' ')"
fi
[ "$(git -C "$REPO" rev-parse -q --verify HEAD 2>/dev/null)" = "$CAND_COMMIT" ] || failed "the clone is not on $CAND_COMMIT after the checkout (the marker $PENDING stays: the next run brings the clone back)."
say "[kit-update] the clone sits on $CAND. Replaying the recorded join."
replay_and_judge
