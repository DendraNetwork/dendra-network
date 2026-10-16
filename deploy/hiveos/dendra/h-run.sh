#!/usr/bin/env bash
# h-run.sh -- HiveOS starts it to run the miner, and stops it (TERM, or HUP when its screen session closes).
#
# Nothing happens without consent. The flight sheet's Extra config must carry YES=1 (h-config.sh). Without it
# this script runs the installer's plan only (lib/install.sh --check, which changes nothing), prints it, and
# waits. With it, it runs lib/install.sh --yes for the requested role (miner by default), on the release tag this
# package was built for (DENDRA_REF in h-manifest.conf: never main, never a pull), with the flight sheet's
# wallet as the payout address, which deploy/join.sh writes for the miner to declare once it is registered.
# Then it follows the miner's logs in the foreground, which is what HiveOS shows.
#
# Stopping the miner in HiveOS stops what this package started. The containers are started with
# `restart: unless-stopped`, so killing this script alone would leave them running, out of sight. The trap
# below stops, in this order:
#   1. an installation still running: install.sh runs in a process group of its own (setsid), and the whole
#      group is signalled (install.sh, the join.sh it runs, and the docker commands join.sh runs), then
#      waited on until none of its processes is left. Signalling install.sh alone would leave join.sh running,
#      and it would start the containers after they were stopped;
#   2. the miner's compose project, `docker compose -p dendra-miner stop`, by project name, which reaches
#      every service of it, profiled ones included;
#   3. the node's, when this package installed one (LIGHT=0): its project is the one the node kit names
#      (DENDRA_PROJECT in deploy/testnet-node/.env of the clone, else the compose file's own default).
# Starting again resumes them without re-installing, while the settings and the release tag are those of
# the last install.
#
# A failure waits instead of exiting: HiveOS restarts a miner that exits, and an installer started again every
# minute would retry the same failure in a loop. The reason is printed; fix it, then restart the miner.
# Exit 3 is not a failure, and it covers two cases. install.sh writes the word of its last action into
# DENDRA_INSTALL_RESULT_FILE: `join_unmeasured` is join.sh's 3 (the containers run, and the miner had neither
# registered nor failed within join.sh's bound and keeps registering), so the install is applied and its logs
# are followed; any other word, or none, is a 3 before anything was started (the host or its role could not be
# read), which waits like a failure and is reported as "not decided", not as "failed".
#
# lib/ holds, from the release this package was built from: install.sh, the hardware probe it reads, the
# payout address check h-config.sh uses, and -- when the release built one -- miner-image.txt, the miner image
# of that release, which install.sh writes into the clone (--miner-image-file) for join.sh to judge
# (deploy/hiveos/build.sh puts them there).
#
# State and keys. What this file remembers between starts (the settings last applied, the installer's last
# word) lives in STATE, outside the package directory, which a package update replaces. The miner's key lives
# in the Docker volume dendra-miner_miner-keys and its keyring passphrase in ~/.config/dendra/miner-secrets:
# both on the system drive, which a reflash rewrites. backup-keys.sh writes them into one encrypted archive to
# copy off the rig, and restores it; the package carries it (lib/backup-keys.sh), since after a reflash there
# is no clone to take it from. While a restore is pending (backup-keys.sh restore found no Docker), this file
# installs nothing: a miner started on an empty volume would create a new key, and so a new identity.
# The configuration home is resolved as backup-keys.sh resolves it (XDG_CONFIG_HOME, else root's home from the
# password database, else HOME), and a pending restore is looked for there and under HOME: a restore run with a
# sudo that kept the caller's HOME, or without sudo, is seen all the same.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
PROJECT=dendra-miner
CLONE="${DENDRA_DIR:-$HOME/dendra-network}"
if [ -n "${XDG_CONFIG_HOME:-}" ]; then CONF_HOME="$XDG_CONFIG_HOME/dendra"
elif [ "$EUID" = 0 ]; then CONF_HOME="$(getent passwd 0 2>/dev/null | cut -d: -f6)"; case "$CONF_HOME" in /*) CONF_HOME="$CONF_HOME/.config/dendra" ;; *) CONF_HOME="" ;; esac
else CONF_HOME="$HOME/.config/dendra"; fi
STATE="${DENDRA_HIVE_STATE:-$CONF_HOME/hiveos}"
PENDINGS=("$CONF_HOME/restore-pending")
[ "$HOME/.config/dendra" != "$CONF_HOME" ] && PENDINGS+=("$HOME/.config/dendra/restore-pending")
# The key tool: the package's own copy (build.sh packs it).
KEYTOOL="$HERE/lib/backup-keys.sh"
LOGS_PID=""; NAP_PID=""; INSTALL_PID=""; HR_NODE=0; LIGHT=""
# What "update" means for a rig: the package of a newer release, never git in the clone. install.sh hands it
# to join.sh, which prints it on every "Update:" line.
HR_UPDATE_HINT="install the HiveOS package of a newer release: change the flight sheet Installation URL"

hr_say(){ printf '[dendra] %s\n' "$*"; }

hr_group_alive(){ # hr_group_alive <pgid> -> 0 while a process of that group lives (a zombie is not one)
  local f line rest st pp pg more
  for f in /proc/[0-9]*/stat; do
    IFS= read -r line 2>/dev/null < "$f" || continue
    rest="${line##*) }"
    read -r st pp pg more <<< "$rest"
    [ "$pg" = "$1" ] && [ "$st" != Z ] && return 0
  done
  return 1
}

hr_node_project(){ # the node kit's compose project: DENDRA_PROJECT of its .env, read and never sourced
  local p
  p="$(awk 'index($0, "DENDRA_PROJECT=") == 1 { print substr($0, 16); exit }' "$CLONE/deploy/testnet-node/.env" 2>/dev/null | tr -d '\r')"
  case "$p" in ""|*[!A-Za-z0-9_-]*) p=dendra-node ;; esac
  printf '%s' "$p"
}

hr_stop(){
  local i
  hr_say "stop requested."
  [ -n "$NAP_PID" ] && kill "$NAP_PID" 2>/dev/null
  [ -n "$LOGS_PID" ] && kill "$LOGS_PID" 2>/dev/null
  if [ -n "$INSTALL_PID" ]; then
    hr_say "stopping the installation still running (install.sh, join.sh and what they started)."
    kill -TERM -- "-$INSTALL_PID" 2>/dev/null
    for i in $(seq 1 30); do hr_group_alive "$INSTALL_PID" || break; sleep 1; done
    if hr_group_alive "$INSTALL_PID"; then
      hr_say "the installation did not stop within 30 s: killed."
      kill -KILL -- "-$INSTALL_PID" 2>/dev/null; sleep 1
    fi
  fi
  if command -v docker >/dev/null 2>&1; then
    hr_say "stopping the containers of the compose project $PROJECT (otherwise they keep running)."
    docker compose -p "$PROJECT" stop
    [ "$HR_NODE" = 1 ] && { hr_say "stopping the node this package installed ($(hr_node_project))."; docker compose -p "$(hr_node_project)" stop; }
    if [ -n "$INSTALL_PID" ]; then
      # A container start the installation asked for before it was stopped may still be completing in the
      # Docker daemon: stopped again once that has had time to land.
      sleep 2
      docker compose -p "$PROJECT" stop
      [ "$HR_NODE" = 1 ] && docker compose -p "$(hr_node_project)" stop
    fi
  fi
  exit 0
}
trap hr_stop TERM HUP INT

hr_wait(){ # hr_wait <message> -- stays alive, doing nothing, until HiveOS stops the miner
  hr_say "$*"
  while :; do
    sleep 3600 &
    NAP_PID=$!
    wait "$NAP_PID"
    NAP_PID=""
  done
}

hr_leads_group(){ # hr_leads_group <pid> -> 0 when <pid> leads its own process group
  local line rest st pp pg more
  IFS= read -r line 2>/dev/null < "/proc/$1/stat" || return 1
  rest="${line##*) }"
  read -r st pp pg more <<< "$rest"
  [ "$pg" = "$1" ]
}

hr_install(){ # hr_install <install.sh arguments...> -- in a process group of its own; the exit code in _rc
  local i
  setsid env "${ENVV[@]}" bash "$INSTALL" "$@" &
  INSTALL_PID=$!
  for i in $(seq 1 50); do
    hr_leads_group "$INSTALL_PID" && break
    kill -0 "$INSTALL_PID" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$INSTALL_PID" 2>/dev/null && ! hr_leads_group "$INSTALL_PID"; then
    kill -TERM "$INSTALL_PID" 2>/dev/null; wait "$INSTALL_PID" 2>/dev/null; INSTALL_PID=""
    hr_wait "refused: install.sh did not start in a process group of its own, so a stop could not reach what it starts. Nothing more was done."
  fi
  wait "$INSTALL_PID"
  _rc=$?
  INSTALL_PID=""
}

hr_conf_get(){ # hr_conf_get <file> <KEY> -> the value of the first KEY= line; read, never evaluated
  awk -v k="$2" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1" 2>/dev/null
}

CUSTOM_VERSION=""; DENDRA_REF=""; CUSTOM_CONFIG_FILENAME=""
[ -r "$HERE/h-manifest.conf" ] && . "$HERE/h-manifest.conf"
CONF="${DENDRA_HIVE_CONF:-${CUSTOM_CONFIG_FILENAME:-$HERE/dendra.conf}}"
hr_say "Dendra package ${CUSTOM_VERSION:-?}, release tag ${DENDRA_REF:-none}"
# The tag form of the release workflow (publish_release.sh, .github/release_notes.sh): no leading zero.
if ! printf '%s\n' "$DENDRA_REF" | grep -Eqx 'v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)'; then
  hr_wait "refused: this package names no release tag vMAJOR.MINOR.PATCH (h-manifest.conf): install the archive a release publishes, not the source folder. Nothing was started."
fi
[ -r "$CONF" ] || hr_wait "refused: no configuration at $CONF -- h-config.sh writes it from the flight sheet. Nothing was started."
# setsid (util-linux) is what lets a stop reach everything the installation started.
command -v setsid >/dev/null 2>&1 || hr_wait "refused: setsid (util-linux) is not on this rig: an installation could not be stopped as a whole. Nothing was started."

REFUSED="$(awk 'index($0, "REFUSED=") == 1 { print substr($0, 9) }' "$CONF")"
if [ -n "$REFUSED" ]; then
  printf '%s\n' "$REFUSED" | while IFS= read -r _l; do hr_say "refused: $_l"; done
  hr_wait "Fix the flight sheet, then restart the miner. Nothing was started."
fi
WALLET="$(hr_conf_get "$CONF" WALLET)"
URL="$(hr_conf_get "$CONF" CONFIG_URL)"
YES="$(hr_conf_get "$CONF" YES)"
ROLE="$(hr_conf_get "$CONF" ROLE)"
LIGHT="$(hr_conf_get "$CONF" LIGHT)"
IGNORED="$(hr_conf_get "$CONF" IGNORED)"
# h-config.sh wrote these; they are checked again here, because this file is what acts on them.
case "$YES" in 0|1) : ;; *) hr_wait "refused: the configuration's YES is neither 0 nor 1. Nothing was started." ;; esac
case "$ROLE" in miner|judge) : ;; *) hr_wait "refused: the configuration's ROLE is neither miner nor judge. Nothing was started." ;; esac
case "$LIGHT" in 0|1) : ;; *) hr_wait "refused: the configuration's LIGHT is neither 0 nor 1. Nothing was started." ;; esac
case "$WALLET" in dendra1*) : ;; *) hr_wait "refused: the configuration names no payout address. Nothing was started." ;; esac
case "$WALLET$URL" in *[!A-Za-z0-9._:/@,=?%+~-]*) hr_wait "refused: the configuration carries a character this package does not pass on. Nothing was started." ;; esac
[ -n "$IGNORED" ] && hr_say "Extra config keys ignored (not in the allow-list YES, ROLE, LIGHT): $IGNORED"

INSTALL="$HERE/lib/install.sh"
[ -r "$INSTALL" ] || hr_wait "refused: $INSTALL is not in this package. Nothing was started."
# --payout-lock (ADR-058 decision 6): the rig's payout address is declared locked with its first declaration, as
# the rented pod's is (ADR-053 decision 4); a rig runs its miner as root from a flight sheet on a disk the rig's
# software can read, and the reason the pod locks holds here. A locked address cannot be corrected: h-config.sh
# and this file print it before anything starts.
ARGS=("--$ROLE" --ref "$DENDRA_REF" --payout-lock)
[ "$LIGHT" = 1 ] && ARGS+=(--light)
# The miner image of this release, when the package carries it (lib/miner-image.txt, put there by build.sh from
# the release's miner-image.txt). A tag pins no image, so without it the rig compiles the miner. install.sh
# writes it into the clone only once the clone is checked out on the tag, and join.sh checks it like any pin;
# this script never touches the clone (install.sh clones it, then runs join.sh).
if [ -e "$HERE/lib/miner-image.txt" ]; then
  ARGS+=(--miner-image-file "$HERE/lib/miner-image.txt")
  hr_say "this package carries the miner image of $DENDRA_REF ($(sed -n 1p "$HERE/lib/miner-image.txt" 2>/dev/null | tr -cd 'A-Za-z0-9 .=:_-' | cut -c1-120)): the rig pulls it instead of compiling, when join.sh accepts it"
else
  hr_say "this package carries no miner image: the rig builds the miner image from the tag (dendrad compiled in Go)"
fi
[ -n "$CONF_HOME" ] || hr_wait "refused: the home directory of root cannot be read (getent passwd 0), and the package keeps its state and looks for a pending restore of the keys there: set XDG_CONFIG_HOME in the miner's environment. Nothing was started."
if ! { ( umask 077; mkdir -p "$STATE" ) 2>/dev/null && [ -w "$STATE" ]; }; then
  hr_wait "refused: the package's state directory $STATE cannot be created or written. Nothing was started."
fi
RESULT="$STATE/install-result"
# DENDRA_AUTO_UPDATE=0: a rig stays on the tree its package was built from, and updates by the package of a newer
# release (HR_UPDATE_HINT), never by moving the clone under the package. install.sh hands it to join.sh, which
# writes it in the kit's .env and schedules no automatic update (deploy/testnet-miner/kit_update.sh then refuses).
ENVV=(DENDRA_REF="$DENDRA_REF" DENDRA_UPDATE_HINT="$HR_UPDATE_HINT" DENDRA_INSTALL_RESULT_FILE="$RESULT" DENDRA_AUTO_UPDATE=0)
[ -n "$URL" ] && ENVV+=(CONFIG_URL="$URL")
hr_result(){ # the word install.sh wrote for its last action, or nothing: one line, lower-case letters and _ only
  local w
  w="$(head -n 1 "$RESULT" 2>/dev/null)"
  case "$w" in ""|*[!a-z_]*) printf '' ;; *) printf '%s' "$w" ;; esac
}
hr_say "role $ROLE, light $LIGHT, payout address $WALLET (declared locked with its first declaration: it cannot be corrected afterwards), network ${URL:-the public network}"

if [ "$YES" != 1 ]; then
  hr_say "Plan only: the flight sheet's Extra config has no YES=1, so nothing is installed or started."
  hr_install --check "${ARGS[@]}"
  hr_wait "That was the plan (install.sh --check exited $_rc); nothing was changed. To apply it, add a line YES=1 to the Extra config, then restart the miner."
fi

for PENDING in "${PENDINGS[@]}"; do
  [ -e "$PENDING" ] || continue
  hr_wait "refused: a restore of the miner's keys is pending ($PENDING, written by backup-keys.sh restore when Docker was missing). Installing now would start the miner on an empty volume, and it would create a new key: a new identity. Install Docker Engine and Compose v2 by hand (https://docs.docker.com/engine/install/), then finish the restore as root: bash $KEYTOOL restore <archive>. To give up the old identity and start a new one instead, remove $PENDING. Then restart the miner. Nothing was started."
done

# Without --light, the installation runs a node of its own: from here on, a stop stops it too.
[ "$LIGHT" = 0 ] && HR_NODE=1
KEY="$DENDRA_REF|$ROLE|$LIGHT|$WALLET|$URL"
MARK="$STATE/applied"
# A package from before the state moved out of its directory kept the mark there: it is moved once.
if [ -e "$HERE/.applied" ] && [ ! -e "$MARK" ]; then mv -f "$HERE/.applied" "$MARK" 2>/dev/null; fi
hr_resume(){
  if [ "$HR_NODE" = 1 ]; then docker compose -p "$(hr_node_project)" start || return 1; fi
  docker compose -p "$PROJECT" start
}
if [ -r "$MARK" ] && [ "$(cat "$MARK" 2>/dev/null)" = "$KEY" ] && hr_resume; then
  hr_say "resumed the miner installed earlier with these same settings and release tag."
else
  hr_say "installing (YES=1): $INSTALL --yes ${ARGS[*]} --payout-address $WALLET"
  rm -f "$RESULT"
  hr_install --yes "${ARGS[@]}" --payout-address "$WALLET"
  case "$_rc" in
    0) : ;;
    3) if [ "$(hr_result)" = join_unmeasured ]; then
         hr_say "install.sh exited 3: the miner's containers run, and within join.sh's bound it neither registered nor failed: not measured, not a failure. It keeps registering; its logs follow, and its self-test says where it stands: bash $CLONE/deploy/testnet-miner/miner_health.sh"
       else
         hr_wait "install.sh exited 3 without starting the miner ($(_w="$(hr_result)"; printf '%s' "${_w:-no action recorded}")): whether this rig can join was not decided -- read its output above, fix the cause, then restart the miner. It is not retried on its own."
       fi ;;
    *) hr_wait "install.sh exited $_rc: read its output above, fix the cause, then restart the miner. It is not retried on its own." ;;
  esac
  printf '%s\n' "$KEY" > "$MARK.tmp" && mv -f "$MARK.tmp" "$MARK"
fi
hr_say "keys: this rig's identity is its key (same key, same address, same identity), in the Docker volume dendra-miner_miner-keys on the system drive, which a reflash erases. Back it up in one encrypted archive and copy that off the rig: bash $KEYTOOL backup"

docker compose -p "$PROJECT" logs -f --tail 100 &
LOGS_PID=$!
wait "$LOGS_PID"
_rc=$?
LOGS_PID=""
hr_say "the miner's logs ended (exit $_rc): its containers stopped. HiveOS starts this package again; to stop for good, stop the miner in HiveOS."
exit 1
