#!/usr/bin/env bash
# uninstall.sh -- remove what the Dendra kit set up on this host. Plan first, nothing without --yes.
#
# WHAT IT MEASURES, THEN PRINTS AS A PLAN (nothing is assumed: what is not there is not in the plan):
#   - the compose projects started from THIS clone's kits (deploy/testnet-miner, deploy/testnet-node),
#     found by the labels compose puts on their containers -- every container, whatever its profile;
#   - ONE IDENTITY PER CARD (deploy/join.sh --gpus): the project of every slot the miner kit's machine files
#     name (deploy/testnet-miner/gpu/<k>/.env), with or without a container -- a slot whose containers are
#     gone still holds a staked key in its own miner-keys volume;
#   - their volumes: the miner's keys (miner-keys), the model weights, the node's chain data (node-data);
#   - the `dendra-chain` network the node kit owns, and the `dendra-rig` network of a shared CPU judge;
#   - the crontab lines that run a file of THIS clone (the capacity re-publication, the validator watch,
#     the miner self-test, anything else pointing into it) -- lines that do not are never touched;
#   - the miner self-test's files next to the miner kit (miner-health.ALERT, .last.json, .EXITED);
#   - the desktop application's files, when they point at this clone, and its settings (app.json);
#   - whether each miner identity of this host is still REGISTERED on the chain, read through the miner kit's
#     exit tool (deploy/testnet-miner/exit-miner.sh --status), in three states: registered, not registered,
#     unknown -- a registration that could not be read is never "not registered". One reading per miner key
#     volume of this clone: a volume no reading covers is unknown, and a reading of a volume this clone does not
#     hold (another clone's project) is set aside. The reading builds and pulls nothing: without the miner
#     image on this host it is unknown.
#
# A REGISTERED MINER IS NOT UNINSTALLED. Removing the kit does not take a miner off the network: its stake
#   stays locked under its identity, it keeps being drawn onto audit juries it no longer answers, and a job of
#   its own under audit waits for a reveal it no longer files. Leaving is exit-miner.sh's job -- it gives the
#   stake back -- and --yes is refused while an identity of this host is registered (exit 2), or while that
#   cannot be read (exit 3). --keep-registration removes the kit anyway and keeps the keys, so that the
#   identity can still leave from a kit installed again on them; never with --delete-keys, which is refused
#   until every identity reads as not registered.
#
# WHAT --yes DOES, IN THIS ORDER:
#   1. backs up the miner's keys and the node's keys (everything in node-data except the chain data) into
#      $HOME/dendra-backup-<UTC>, readable by you only, OUTSIDE the clone, and VERIFIES each archive by
#      listing it. A backup that cannot be made or verified stops the run: nothing is removed;
#   2. stops and removes the containers and their images: docker compose -p <project> down --rmi all.
#      NEVER `down -v`: volumes are removed one by one, by name, below. The slots k go first, highest first,
#      WITHOUT --rmi (they run the images slot 0 runs), then slot 0 with --rmi all;
#   3. removes the model-weight volumes, and node-data only for a node MEASURED as not a validator;
#   4. removes this clone's crontab lines, keeping every other line byte for byte, and reads the table
#      back to check it; then the miner self-test's files, which no scheduled run can write again;
#   5. removes the application's launcher, menu entry, icon and settings.
#
# WHAT IT KEEPS, ALWAYS (and prints the commands that would remove each):
#   - the miner's keys (Docker volume dendra-miner_miner-keys, and dendra-miner-g<k>_miner-keys for each card's
#     identity): its address holds the stake and the Final Testnet Season payments. --delete-keys removes it
#     too, and only after the verified backup of the same run -- there is no way to skip that backup;
#   - the slots' machine files (deploy/testnet-miner/gpu/): the table that binds each kept key to its card.
#     --delete-keys removes them, after the same verified backup;
#   - the keyring's passphrase (the file keyring-passphrase in the directory DENDRA_SECRETS_DIR names in the
#     miner kit's .env, by default ~/.config/dendra/miner-secrets): an encrypted keyring does not open
#     without it. The verified backup includes THAT FILE (an archive 0600, said as such), and --delete-keys
#     removes that file with the volume, never alone -- then the directory only if nothing else is left in
#     it. --delete-keys is refused when that directory is not one of its own (deploy/testnet-miner/
#     passphrase-dir.sh: $HOME, a directory above it, one that holds other files): nothing of the user's goes;
#   - this clone; Docker, the NVIDIA container toolkit, the docker group, the apt repositories.
#
# A VALIDATOR IS NOT UNINSTALLED. Stopping a bonded node jails it and slashes its stake. The voting power
#   of EVERY running node of this clone is READ (a second node project, DENDRA_PROJECT, is a node too):
#   above 0 on any of them, this file refuses; unreadable on any running one, it refuses too (an unknown
#   is not "not a validator"). node-data is removed only for a project whose node was MEASURED at 0; a
#   stopped node's is KEPT. A jailed validator also reads 0: its keys are in the backup. --keep-node
#   leaves the node kit entirely as it is.
#
# Usage:
#   bash deploy/uninstall.sh                 # measure and print the plan, change nothing
#   bash deploy/uninstall.sh --yes           # do it (keys kept)
#   bash deploy/uninstall.sh --yes --delete-keys
#   bash deploy/uninstall.sh --yes --keep-node   # remove the miner side only
#   bash deploy/uninstall.sh --yes --keep-registration   # a miner still registered: remove the kit, keep its keys
#
# Under Windows: install.ps1 -Uninstall runs this file inside the distribution, then removes the logon
# task. Neither file unregisters the distribution (`wsl --unregister` deletes its volumes, keys included).
#
# Exit codes -- three answers, never two:
#   0  done, or nothing of the kit on this host
#   1  a step was attempted and failed (backup, removal, crontab write-back)
#   2  refused: changes needed and no --yes; a validator; a miner identity still registered on the chain;
#      --delete-keys with no miner keys to back up, or with a passphrase that does not live in a directory of
#      its own
#   3  not measurable: Docker present but unreadable from this account, a running node whose voting power
#      cannot be read, a miner registration that cannot be read, a crontab that cannot be read -- an unknown
#      is never "nothing to remove"
set -u

YES=0; DELETE_KEYS=0; KEEP_NODE=0; KEEP_REG=0
while [ $# -gt 0 ]; do case "$1" in
  --yes) YES=1; shift;;
  --delete-keys) DELETE_KEYS=1; shift;;
  --keep-node) KEEP_NODE=1; shift;;
  --keep-registration) KEEP_REG=1; shift;;
  -h|--help) awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; exit 0;;
  *) echo "[uninstall] unknown argument: $1 (see --help)"; exit 2;;
esac; done

say(){ printf '%s\n' "$*"; }
warn(){ printf '  [!] %s\n' "$*"; }
refuse(){ printf '  [REFUSED] %s\n' "$*" >&2; }
unmeasurable(){ printf '  [?] %s\n' "$*" >&2; exit 3; }
fail(){ printf '  [FAILED] %s\n' "$*" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
[ -n "$REPO" ] && [ -f "$REPO/deploy/join.sh" ] || { echo "[uninstall] the repository is not around this file (no deploy/join.sh)."; exit 3; }
REPO_P="$(cd "$REPO" && pwd -P)"
MINER_KIT="$REPO/deploy/testnet-miner"; NODE_KIT="$REPO/deploy/testnet-node"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/dendra"
APP_BIN="$HOME/.local/bin/dendra"
APP_DESK="$HOME/.local/share/applications/dendra.desktop"
APP_ICON="$HOME/.local/share/icons/hicolor/scalable/apps/dendra.svg"

# ---------------------------------------------------------------- 1. measure
say "== [uninstall] reading this host (clone: $REPO) =="

# Docker, three states: absent (nothing of the kit can be running), readable, unreadable. Unreadable is
# NOT "nothing to remove": a user not yet in the docker group gets "permission denied" from a daemon that
# runs the whole kit.
DOCKER=absent
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then DOCKER=up
  else
    unmeasurable "docker is installed but its daemon does not answer this account ('docker info' fails). Nothing can be said about the kit's containers and volumes. If you were just added to the docker group, log out and back in; or start the daemon. Nothing was changed."
  fi
fi

# Containers of THIS clone's kits, by the labels compose writes (project, working directory, service,
# state). The working directory is what ties a project to this clone: two clones share the miner's
# fixed project name, and only the one that started it owns it.
CTRS=""; MINER_PROJ=""; NODE_PROJ=""; NODE_RUNNING=""
if [ "$DOCKER" = up ]; then
  CTRS="$(docker ps -a --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.service"}}|{{.State}}|{{.Names}}' 2>/dev/null)" \
    || unmeasurable "docker ps failed: the containers cannot be listed."
  _proj_of(){ # _proj_of <kit dir> -> distinct project names whose containers were started from it
    printf '%s\n' "$CTRS" | awk -F'|' -v a="$1" -v b="$2" '$1!="" && ($2==a || $2==b) {print $1}' | sort -u
  }
  MINER_PROJ="$(_proj_of "$MINER_KIT" "$REPO_P/deploy/testnet-miner")"
  NODE_PROJ="$(_proj_of "$NODE_KIT" "$REPO_P/deploy/testnet-node")"
  # EVERY running node container of this clone, one "<project>|<container>" per line. Not the first one:
  # docker ps lists the newest first, and a second node project started from the same kit (a try-out next
  # to a validator) would hide the validator behind it.
  NODE_RUNNING="$(printf '%s\n' "$CTRS" | awk -F'|' -v a="$NODE_KIT" -v b="$REPO_P/deploy/testnet-node" '($2==a || $2==b) && $3=="node" && $4=="running" {print $1 "|" $5}')"
fi
# Volumes are not labelled with a directory: they are named <project>_<volume>. The miner's project name
# is fixed by its compose file; the node's comes from the node kit (DENDRA_PROJECT), read the way compose
# resolves it. A project with no container left still has its volumes found this way.
NODE_NAME_CFG="$( cd "$NODE_KIT" 2>/dev/null && docker compose config 2>/dev/null | sed -n 's/^name:[[:space:]]*//p' | head -1 | tr -d '[:space:]"' )"
VOLS=""
if [ "$DOCKER" = up ]; then
  VOLS="$(docker volume ls -q 2>/dev/null)" || unmeasurable "docker volume ls failed: the volumes cannot be listed."
fi
has_vol(){ printf '%s\n' "$VOLS" | grep -qxF -- "$1"; }
# A project name whose containers were started from ANOTHER directory belongs to another clone: its
# volumes are not this clone's to back up or remove, even though the names match.
_foreign(){ # _foreign <project> <kit> <kit, physical path> -> 0 when another directory started it
  printf '%s\n' "$CTRS" | awk -F'|' -v p="$1" -v a="$2" -v b="$3" '$1==p && $2!=a && $2!=b {f=1} END{exit f?0:1}'
}
NODE_DEFAULT="${NODE_NAME_CFG:-dendra-node}"; MINER_DEFAULT=dendra-miner; FOREIGN=""
if _foreign "$NODE_DEFAULT" "$NODE_KIT" "$REPO_P/deploy/testnet-node"; then FOREIGN="$NODE_DEFAULT"; NODE_DEFAULT=""; fi
if _foreign "$MINER_DEFAULT" "$MINER_KIT" "$REPO_P/deploy/testnet-miner"; then FOREIGN="${FOREIGN:+$FOREIGN }$MINER_DEFAULT"; MINER_DEFAULT=""; fi
# ONE IDENTITY PER CARD: the project of every slot k the miner kit's machine files name, read through the kit's
# slot library -- with or without a container: a slot whose containers are gone still has its miner-keys
# volume, and without this union its staked key would be neither backed up nor listed. A slot whose project
# was started from another directory is another clone's, like any project. Slots that cannot be read stop
# the run: a key that may be missed is not "nothing to back up".
SLOT_NAMES=""
if [ -e "$MINER_KIT/gpu" ] || [ -r "$MINER_KIT/slots.sh" ]; then
  { [ -r "$MINER_KIT/slots.sh" ] && . "$MINER_KIT/slots.sh" && declare -F slot_ids >/dev/null 2>&1; } \
    || unmeasurable "the slot library of the miner kit ($MINER_KIT/slots.sh) cannot be loaded: a card's identity and its staked key could be missed. Nothing was changed."
  _sids="$(slot_ids --all)" \
    || unmeasurable "the slots of the miner kit ($MINER_KIT/gpu) cannot be read: a card's identity and its staked key could be missed. Nothing was changed."
  for _k in $_sids; do
    [ "$_k" = 0 ] && continue
    _sp="$(slot_project "$_k")"
    if _foreign "$_sp" "$MINER_KIT" "$REPO_P/deploy/testnet-miner"; then FOREIGN="${FOREIGN:+$FOREIGN }$_sp"; MINER_PROJ="$(printf '%s\n' "$MINER_PROJ" | grep -vxF -- "$_sp")"; continue; fi
    SLOT_NAMES="${SLOT_NAMES:+$SLOT_NAMES }$_sp"
  done
fi
NODE_NAMES="$(printf '%s\n%s\n' "$NODE_PROJ" "$NODE_DEFAULT" | grep -v '^$' | sort -u)"
MINER_NAMES="$(printf '%s\n%s\n%s\n' "$MINER_PROJ" "$MINER_DEFAULT" "$(printf '%s\n' $SLOT_NAMES)" | grep -v '^$' | sort -u)"
# THE ORDER OF THE MINER PROJECTS' REMOVAL: the slots k first, the highest first, then any other project of the
# kit, then slot 0 LAST -- it alone with --rmi all: the slots run the images slot 0 runs.
MINER_DOWN="$(printf '%s\n' $MINER_PROJ | awk '/^dendra-miner-g[0-9]+$/ { k = substr($0, 15); print "1 " k " " $0; next }
  $0 == "dendra-miner" { print "3 0 " $0; next } NF { print "2 0 " $0 }' | sort -k1,1n -k2,2nr | awk '{ print $3 }')"
KEYS_VOL=""; MODEL_VOLS=""; NODE_VOLS=""
for p in $MINER_NAMES; do
  has_vol "${p}_miner-keys" && KEYS_VOL="${KEYS_VOL:+$KEYS_VOL }${p}_miner-keys"
  for v in ollama-models ollama-judge-models; do has_vol "${p}_$v" && MODEL_VOLS="${MODEL_VOLS:+$MODEL_VOLS }${p}_$v"; done
done
for p in $NODE_NAMES; do has_vol "${p}_node-data" && NODE_VOLS="${NODE_VOLS:+$NODE_VOLS }${p}_node-data"; done
NET=absent
if [ "$DOCKER" = up ] && docker network inspect dendra-chain >/dev/null 2>&1; then NET=present; fi
# The network of the shared CPU judge (deploy/join.sh --gpus --judge): it belongs to no project, so no `down`
# removes it -- this file does, once every miner project is down.
NET_RIG=absent
if [ "$DOCKER" = up ] && docker network inspect dendra-rig >/dev/null 2>&1; then NET_RIG=present; fi

# THE VALIDATOR GUARD. Each running node's own `dendrad status` carries validator_info.voting_power. It is
# PARSED (python3, else jq), never matched as text, and it has NO DEFAULT: this is a safety criterion, and
# `dendrad status` is CometBFT's JSON, not proto3 -- it writes voting_power on every answer, zero included
# (cometbft rpc/core/types ValidatorInfo, no omitempty). So an absent, null, empty or non-integer value is
# a status this file does not recognise: unread, never 0. A missing validator_info, or no parser, too.
_vp_parse(){ # stdin: the status document -> prints the voting power, or nothing
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys
t=sys.stdin.read(); i=t.find("{")
try: d=json.loads(t[i:]) if i>=0 else None
except ValueError: d=None
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
v=None
if isinstance(d,dict):
    for k in ("validator_info","ValidatorInfo"):
        if isinstance(d.get(k),dict): v=d[k]; break
p=v.get("voting_power", v.get("VotingPower")) if v is not None else None
if isinstance(p,int) and not isinstance(p,bool) and p>=0: print(p)
elif isinstance(p,str) and p.isascii() and p.isdigit(): print(int(p))' 2>/dev/null
  elif command -v jq >/dev/null 2>&1; then
    jq -r '(.result // .) | (.validator_info // .ValidatorInfo) | select(type=="object") | (.voting_power // .VotingPower) | select(type=="string" or type=="number") | tostring | select(test("^[0-9]+$"))' 2>/dev/null
  fi
}
# One reading per running node container. VP_POS: validators found; VP_UNREAD: running nodes whose power
# was not read; VP_ZERO: the PROJECTS whose node was measured at 0 -- the only ones whose chain data goes.
VP_POS=""; VP_UNREAD=""; VP_ZERO=""; VP_TEXT=""
while IFS='|' read -r _np _nc; do
  [ -n "$_nc" ] || continue
  _st="$(docker exec "$_nc" dendrad status 2>&1)"
  _v="$(printf '%s' "$_st" | _vp_parse | tail -1)"
  case "$_v" in
    ''|*[!0-9]*) _v="?"; VP_UNREAD="${VP_UNREAD:+$VP_UNREAD }$_nc" ;;
    0) VP_ZERO="${VP_ZERO:+$VP_ZERO }$_np" ;;
    *) VP_POS="${VP_POS:+$VP_POS, }$_nc (voting power $_v)" ;;
  esac
  VP_TEXT="${VP_TEXT:+$VP_TEXT, }$_nc: voting power $_v"
done <<EOF
$NODE_RUNNING
EOF
_measured_zero(){ case " $VP_ZERO " in *" $1 "*) return 0;; esac; return 1; }

# Crontab, three states: a table (possibly empty), no table ("no crontab for <user>" is a reading), or
# unread. An unread table is never written back: `crontab FILE` replaces the WHOLE table.
CRON=absent; CRON_ALL=""; CRON_MINE=""
if command -v crontab >/dev/null 2>&1; then
  _ce="$(mktemp)"
  if CRON_ALL="$(crontab -l 2>"$_ce")"; then CRON=read
  elif grep -qi 'no crontab' "$_ce" 2>/dev/null; then CRON=read; CRON_ALL=""
  else CRON=unread; fi
  rm -f "$_ce"
  # A line of THIS clone names a path inside it: "<clone>/" with its slash, so a sibling clone whose
  # path merely starts the same way is not taken.
  [ "$CRON" = read ] && CRON_MINE="$(printf '%s\n' "$CRON_ALL" | grep -F -- "$REPO/" || true)"
  if [ "$CRON" = read ] && [ "$KEEP_NODE" = 1 ]; then
    CRON_MINE="$(printf '%s\n' "$CRON_MINE" | grep -v 'validator_health\.sh' || true)"
  fi
fi

# The miner self-test's files, next to the miner kit (deploy/testnet-miner/miner_health.sh --cron, and the
# exit marker exit-miner.sh writes): state of this machine, removed with the crontab line that writes them.
# ARRAYS, NOT WORDS: a clone under a path with a space ("My Projects") would otherwise be split, and `rm`
# would remove the pieces -- a file of the user's that merely shares the first word.
HEALTH_FILES=()
for _hf in miner-health.ALERT miner-health.last.json miner-health.EXITED; do
  [ -e "$MINER_KIT/$_hf" ] && HEALTH_FILES+=("$MINER_KIT/$_hf")
done
# ... and each slot k's, in its own directory (gpu/<k>/): the hourly run writes one result per identity.
for _hf in "$MINER_KIT"/gpu/*/miner-health.last.json "$MINER_KIT"/gpu/*/miner-health.EXITED; do
  [ -e "$_hf" ] && HEALTH_FILES+=("$_hf")
done
# The slots' machine files: kept, as the keys are -- they bind each kept key to its card.
GPU_DIR=""; [ -d "$MINER_KIT/gpu" ] && GPU_DIR="$MINER_KIT/gpu"

# The application: its launcher is ours only when it runs THIS clone's dendra_app.py.
APP_FILES=()
if [ -f "$APP_BIN" ] && grep -qF -- "$REPO/deploy/app/dendra_app.py" "$APP_BIN" 2>/dev/null; then
  APP_FILES+=("$APP_BIN")
  [ -f "$APP_DESK" ] && APP_FILES+=("$APP_DESK")
  [ -f "$APP_ICON" ] && APP_FILES+=("$APP_ICON")
fi
[ -f "$CONFIG_DIR/app.json" ] && APP_FILES+=("$CONFIG_DIR/app.json")
_list(){ local f o=""; for f in "$@"; do o="${o:+$o }$(printf '%q' "$f")"; done; printf '%s' "$o"; }

# THE KEYRING'S PASSPHRASE: the directory the miner kit's .env names (DENDRA_SECRETS_DIR), else the kit's
# default. An empty line in that .env is the choice of keys in clear (deploy/join.sh --plain-keys): then
# only the default place is looked at. A relative value is the kit's own ./secrets, which holds nothing.
SECRETS_DIR="$(sed -n 's/^DENDRA_SECRETS_DIR=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
case "$SECRETS_DIR" in /*) : ;; *) SECRETS_DIR="$CONFIG_DIR/miner-secrets" ;; esac
SECRETS_PRESENT=0
[ -e "$SECRETS_DIR/keyring-passphrase" ] && SECRETS_PRESENT=1
# ONE PASSPHRASE FILE PER MACHINE is what deploy/join.sh --gpus writes into every slot. A slot whose env names
# ANOTHER directory holding a passphrase (edited by hand) still has a keyring that opens only with it: that
# file is backed up too, and --delete-keys is refused rather than guess which one goes with which key.
# One directory per line (a path may hold a space).
SECRETS_OTHERS=""
for _se in "$MINER_KIT"/gpu/*/.env; do
  [ -f "$_se" ] || continue
  _sd="$(sed -n 's/^DENDRA_SECRETS_DIR=//p' "$_se" 2>/dev/null | head -1 | tr -d '\r')"
  case "$_sd" in /*) : ;; *) continue ;; esac
  [ "$_sd" != "$SECRETS_DIR" ] && [ -e "$_sd/keyring-passphrase" ] || continue
  printf '%s\n' "$SECRETS_OTHERS" | grep -qxF -- "$_sd" || SECRETS_OTHERS="${SECRETS_OTHERS:+$SECRETS_OTHERS
}$_sd"
done
# IS THAT DIRECTORY ONE OF ITS OWN? The ONE check join.sh and encrypt-keys.sh source too. Only --delete-keys
# depends on it (the backup takes the passphrase file alone, whatever the directory): a removal under $HOME,
# or in a directory that holds other files, is refused. A check that cannot be loaded decides nothing.
SECRETS_WHY=""
if [ "$SECRETS_PRESENT" = 1 ]; then
  if [ -r "$MINER_KIT/passphrase-dir.sh" ] && . "$MINER_KIT/passphrase-dir.sh" && declare -F dendra_secrets_dir_check >/dev/null 2>&1; then
    if SECRETS_WHY="$(dendra_secrets_dir_check "$SECRETS_DIR" "$REPO")"; then SECRETS_WHY=""
    else SECRETS_WHY="${SECRETS_WHY:-refused by the directory check}"; fi
  else
    SECRETS_WHY="?"
  fi
fi

# THE REGISTRATION ON THE CHAIN, one identity per line, read through the miner kit's own exit tool: it reads the
# identity in the key volume and the record on the chain the way it would leave with it (one-off containers of
# the miner's image -- nothing starts, nothing is sent). Read only when this clone holds a miner key volume:
# without one, no identity of this host can be registered. THREE STATES, NEVER TWO: a line that is not a
# reading, a missing line, a missing tool or an exit code other than 0 makes the registration UNKNOWN -- never
# "not registered".
# _reg_field <name> <line> -> the value of name= in one `registration ...` line; `why` is the rest of the line.
_reg_field(){
  printf '%s\n' "$2" | awk -v k="$1" '{
    if (k == "why") { i = index($0, " why="); if (i) print substr($0, i + 5); exit }
    for (i = 2; i <= NF; i++) { n = $i; sub(/=.*/, "", n); if (n == "why") exit
      if (n == k) { v = $i; sub(/^[^=]*=/, "", v); print v; exit } } }'
}
# _reg_vol <slot> -> the miner-keys volume of that slot's project (slots.sh names a slot's project; slot 0 is the
# kit's fixed name when the library is not loaded). Empty when it cannot be named.
_reg_vol(){
  if declare -F slot_project >/dev/null 2>&1; then
    local p; p="$(slot_project "$1" 2>/dev/null)" && [ -n "$p" ] && printf '%s_miner-keys\n' "$p"
  elif [ "$1" = 0 ]; then printf 'dendra-miner_miner-keys\n'; fi
  return 0
}
# ONE READING PER KEY VOLUME, AND ONLY FOR THEM. The lines are matched to the volumes this clone holds (KEYS_VOL):
#   - a line whose slot's volume is NOT among them reads an identity this clone does not hold -- a project
#     started from another directory (another clone, whose volumes this file leaves alone), or a slot with no
#     key volume -- and decides nothing here: it is set aside and said;
#   - a line whose slot cannot be named is UNKNOWN, never set aside;
#   - a key volume of this clone that no line reads -- a project of this kit that no slot names any more -- is
#     UNKNOWN: "read for every identity" is said only when every key volume has its line.
REG_READ=none; REG_LINES=""; REG_ALL=""; REG_WHY=""; REG_ASIDE=""; HAS_REG=0; HAS_UNREAD=0
if [ -n "$KEYS_VOL" ]; then
  REG_READ=unread
  if [ ! -r "$MINER_KIT/exit-miner.sh" ]; then
    REG_WHY="$MINER_KIT/exit-miner.sh is missing: the registration is read through it"
  else
    _ro="$(bash "$MINER_KIT/exit-miner.sh" --status 2>&1)"; _rrc=$?
    _all="$(printf '%s\n' "$_ro" | grep '^registration slot=')"; REG_ALL="$_all"
    if [ -z "$_all" ]; then
      REG_WHY="exit-miner.sh --status printed no registration (exit $_rrc): $(printf '%s\n' "$_ro" | grep . | tail -1)"
    else
      REG_READ=read; _aside_unread=0
      while IFS= read -r _l; do
        [ -n "$_l" ] || continue
        _v="$(_reg_vol "$(_reg_field slot "$_l")")"
        if [ -z "$_v" ]; then
          HAS_UNREAD=1; REG_WHY="${REG_WHY:+$REG_WHY; }slot $(_reg_field slot "$_l"): its key volume cannot be named"
          continue
        fi
        case " $KEYS_VOL " in
          *" $_v "*) REG_LINES="${REG_LINES:+$REG_LINES
}$_l" ;;
          # Said when it would have decided something here (registered, or unread); an absent one says nothing.
          *) case "$(_reg_field state "$_l")" in
               absent) : ;;
               registered) REG_ASIDE="${REG_ASIDE:+$REG_ASIDE, }slot $(_reg_field slot "$_l") ($_v, registered)" ;;
               *) _aside_unread=1; REG_ASIDE="${REG_ASIDE:+$REG_ASIDE, }slot $(_reg_field slot "$_l") ($_v, unread)" ;;
             esac
             continue ;;
        esac
        case "$(_reg_field state "$_l")" in
          registered) HAS_REG=1 ;;
          absent) : ;;
          *) HAS_UNREAD=1 ;;
        esac
      done <<EOF
$_all
EOF
      for _v in $KEYS_VOL; do
        _seen=0
        while IFS= read -r _l; do
          [ -n "$_l" ] && [ "$(_reg_vol "$(_reg_field slot "$_l")")" = "$_v" ] && _seen=1
        done <<EOF
$REG_LINES
EOF
        [ "$_seen" = 1 ] || { HAS_UNREAD=1; REG_WHY="${REG_WHY:+$REG_WHY; }$_v: no registration was read for it (no slot of this kit names its project)"; }
      done
      # A tool that exits on an error has not read every identity -- unless what it could not read is one of the
      # identities set aside above, whose own line says so.
      if [ "$_rrc" != 0 ] && [ "$HAS_UNREAD" = 0 ] && { [ "$_rrc" != 3 ] || [ "$_aside_unread" = 0 ]; }; then
        HAS_UNREAD=1; REG_WHY="exit-miner.sh --status exited $_rrc: $(printf '%s\n' "$_ro" | grep . | tail -1)"
      fi
    fi
  fi
  [ "$REG_READ" = unread ] && HAS_UNREAD=1
fi
# _reg_list <state> -> "<id> (slot k, stake X udndr)" for each identity read in that state, one per line.
_reg_list(){
  local l
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    if [ "$1" = unread ]; then
      case "$(_reg_field state "$l")" in registered|absent) continue ;; esac
      printf '%s (slot %s): %s\n' "$(_reg_field id "$l")" "$(_reg_field slot "$l")" "$(_reg_field why "$l")"
    else
      [ "$(_reg_field state "$l")" = "$1" ] || continue
      printf '%s (slot %s, stake %s udndr)\n' "$(_reg_field id "$l")" "$(_reg_field slot "$l")" "$(_reg_field stake "$l")"
    fi
  done <<EOF
$REG_LINES
EOF
}
# _leave_cmd -> the exit command for the registered identities (with --slot on a machine of several: counted on
# every line the tool printed, the ones set aside included -- exit-miner.sh refuses --yes without --slot there).
_leave_cmd(){
  local l s n=0
  n="$(printf '%s\n' "$REG_ALL" | grep -c '^registration ')"
  while IFS= read -r l; do
    [ -n "$l" ] && [ "$(_reg_field state "$l")" = registered ] || continue
    s="$(_reg_field slot "$l")"
    if [ "$n" -ge 2 ]; then printf 'bash %s --slot %s --yes --wait\n' "$(printf '%q' "$MINER_KIT/exit-miner.sh")" "$s"
    else printf 'bash %s --yes --wait\n' "$(printf '%q' "$MINER_KIT/exit-miner.sh")"; fi
  done <<EOF
$REG_LINES
EOF
}

# ---------------------------------------------------------------- 2. decide, and the refusals
NODE_PRESENT=0; [ -n "$NODE_PROJ$NODE_VOLS" ] && NODE_PRESENT=1
# node-data goes only for a project whose node was MEASURED at 0; every other one is kept.
DROP_VOLS=""; KEEP_VOLS=""
if [ "$NODE_PRESENT" = 1 ] && [ "$KEEP_NODE" = 0 ]; then
  if [ -n "$VP_POS" ]; then
    refuse "a node of this clone is a VALIDATOR: $VP_POS. Uninstalling would stop it, and a stopped validator"
    say "         is jailed and slashed. Unbond first (the stake returns after the unbonding period), wait until"
    say "         the voting power reads 0, then run this again. To remove only the miner side: --keep-node."
    exit 2
  fi
  if [ -n "$VP_UNREAD" ]; then
    refuse "the node(s) $VP_UNREAD run and their voting power cannot be read (dendrad status, or no python3/jq to parse it)."
    say "         Stopping a bonded validator jails it and slashes its stake, and an unread voting power is not a"
    say "         zero. Read it yourself: docker exec <container> dendrad status   (validator_info.voting_power)."
    say "         To remove only the miner side: --keep-node."
    exit 3
  fi
  for v in $NODE_VOLS; do
    if _measured_zero "${v%_node-data}"; then DROP_VOLS="${DROP_VOLS:+$DROP_VOLS }$v"
    else KEEP_VOLS="${KEEP_VOLS:+$KEEP_VOLS }$v"; fi
  done
fi
DROP_NODE_DATA=0; [ -n "$DROP_VOLS" ] && DROP_NODE_DATA=1
if [ "$DELETE_KEYS" = 1 ] && [ -z "$KEYS_VOL" ]; then
  refuse "--delete-keys, but no miner-keys volume exists here: there is nothing to back up, so nothing to delete."
  exit 2
fi
if [ "$DELETE_KEYS" = 1 ] && [ "$SECRETS_WHY" = "?" ]; then
  unmeasurable "--delete-keys, and the check of the passphrase directory ($MINER_KIT/passphrase-dir.sh) cannot be loaded: whether $SECRETS_DIR is a directory of its own is not known. Nothing was changed."
fi
if [ "$DELETE_KEYS" = 1 ] && [ -n "$SECRETS_OTHERS" ]; then
  refuse "--delete-keys, and the slots of this machine name more than one passphrase directory ($SECRETS_DIR, and:"
  printf '%s\n' "$SECRETS_OTHERS" | sed 's/^/           /' >&2
  say "         ). Which file opens which key is not this file's to guess: align DENDRA_SECRETS_DIR in every env of"
  say "         $MINER_KIT (gpu/<k>/.env included), or remove the files yourself. Nothing was changed."
  exit 2
fi
if [ "$DELETE_KEYS" = 1 ] && [ -n "$SECRETS_WHY" ]; then
  refuse "--delete-keys, and the keyring passphrase lives in $SECRETS_DIR, which cannot be treated as its own"
  say "         directory: $SECRETS_WHY. Nothing was changed. Move keyring-passphrase into a directory of its own,"
  say "         name it as DENDRA_SECRETS_DIR in $MINER_KIT/.env, and run this again -- or remove the file yourself."
  exit 2
fi
# --delete-keys ON A REGISTERED IDENTITY: its stake would then come back only from the archive, through a kit
# installed again on it. Refused whatever else is asked -- --keep-registration included -- and on an unknown.
if [ "$DELETE_KEYS" = 1 ] && [ "$HAS_REG" = 1 ]; then
  refuse "--delete-keys, and a miner identity of this host is still REGISTERED on the chain:"
  _reg_list registered | sed 's/^/           /' >&2
  say "         Its keys are what takes it off the network and gives its stake back. Leave first, then run this again:"
  _leave_cmd | sed 's/^/           /'
  say "         Nothing was changed."
  exit 2
fi
if [ "$DELETE_KEYS" = 1 ] && [ "$HAS_UNREAD" = 1 ]; then
  unmeasurable "--delete-keys, and whether every miner identity of this host is registered could not be read ($(_reg_list unread | tr '\n' ' ')${REG_WHY}): keys that may still hold a stake are not deleted on an unknown. Nothing was changed."
fi

# ---------------------------------------------------------------- 3. the plan
say "  docker       : $DOCKER"
say "  miner        : $( [ -n "$MINER_PROJ" ] && printf '%s' "$MINER_PROJ" | tr '\n' ' ' || printf 'no container from this clone' )"
[ -n "$SLOT_NAMES" ] && say "  card slots   : $SLOT_NAMES (one identity per card, from $MINER_KIT/gpu)"
say "  node         : ${NODE_PROJ:-no container from this clone}${VP_TEXT:+ (running: $VP_TEXT)}"
say "  volumes      : keys ${KEYS_VOL:-none} | models ${MODEL_VOLS:-none} | node ${NODE_VOLS:-none}"
say "  passphrase   : $( [ "$SECRETS_PRESENT" = 1 ] && printf '%s' "$(printf '%q' "$SECRETS_DIR/keyring-passphrase") (the keyring passphrase)" || printf 'none' )"
[ -n "$SECRETS_OTHERS" ] && say "                 and, named by a slot's env: $(printf '%s\n' "$SECRETS_OTHERS" | tr '\n' ' ')"
say "  network      : dendra-chain $NET | dendra-rig $NET_RIG"
say "  crontab      : $CRON$( [ "$CRON" = read ] && printf ', %s line(s) of this clone' "$(printf '%s\n' "$CRON_MINE" | grep -c .)" )"
say "  application  : $( [ "${#APP_FILES[@]}" -gt 0 ] && _list "${APP_FILES[@]}" || printf none)"
[ -n "$FOREIGN" ] && say "  other clone  : $FOREIGN was started from another directory: its containers and volumes are left alone"
case "$REG_READ" in
  none) say "  registration : no miner key volume from this clone, so no identity of this host to read" ;;
  *)
    if [ "$HAS_REG" = 1 ]; then
      say "  registration : STILL REGISTERED on the chain -- uninstalling does not take a miner off the network:"
      _reg_list registered | sed 's/^/                 /'
    fi
    if [ "$HAS_UNREAD" = 1 ]; then
      say "  registration : UNKNOWN -- it could not be read, and that is never 'not registered':"
      { _reg_list unread; [ -n "$REG_WHY" ] && printf '%s\n' "$REG_WHY"; } | sed 's/^/                 /'
    fi
    [ "$HAS_REG$HAS_UNREAD" = 00 ] && say "  registration : not registered on the chain (read for each miner key volume of this clone: $KEYS_VOL)"
    [ -n "$REG_ASIDE" ] && say "  registration : set aside, its key volume is not this clone's to remove: $REG_ASIDE" ;;
esac

PLAN=""
add(){ PLAN="${PLAN}  - $1
"; }
# THE REGISTRATION COMES FIRST IN THE PLAN, because it is the one step this file does not take.
if [ "$HAS_REG" = 1 ] && [ "$KEEP_REG" = 0 ]; then
  add "FIRST, leave the network with the miner kit's exit tool -- it gives the stake back; --yes is refused here while an identity is registered: $(_leave_cmd | tr '\n' ';' | sed 's/;$//; s/;/ ; /g')"
elif [ "$HAS_UNREAD" = 1 ] && [ "$KEEP_REG" = 0 ]; then
  add "FIRST, read the registration again once the node and the miner image answer (bash $(printf '%q' "$MINER_KIT/exit-miner.sh") --status) -- --yes is refused while it is unknown"
fi
if [ "$KEEP_REG" = 1 ] && [ "$HAS_REG$HAS_UNREAD" != 00 ]; then
  add "KEEP the registration (--keep-registration): the identity stays on the chain with its stake, and its keys are KEPT -- it leaves later from a kit installed again on them (exit-miner.sh --yes)"
fi
BACKUP_NEEDED=0
[ -n "$KEYS_VOL" ] && BACKUP_NEEDED=1
[ "$DROP_NODE_DATA" = 1 ] && BACKUP_NEEDED=1
_sx=""; [ "$SECRETS_PRESENT" = 1 ] && _sx="; the keyring passphrase"
[ -n "$SECRETS_OTHERS" ] && _sx="$_sx; the passphrase file(s) a slot names"
[ "$BACKUP_NEEDED" = 1 ] && add "back up the keys (${KEYS_VOL:+miner: $KEYS_VOL}${KEYS_VOL:+${DROP_VOLS:+; }}${DROP_VOLS:+node: config/ and keyrings of $DROP_VOLS}$_sx) into \$HOME/dendra-backup-<UTC>, mode 0600, and verify each archive"
for p in $MINER_DOWN; do
  if [ "$p" = dendra-miner ]; then add "docker compose -p $p down --rmi all   (containers and their images; volumes are NOT removed by it)"
  else add "docker compose -p $p down   (containers only: its images are the ones slot 0 runs; volumes are NOT removed by it)"; fi
done
[ "$NET_RIG" = present ] && add "docker network rm dendra-rig   (the shared judge's network, once every miner project is down)"
if [ "$KEEP_NODE" = 0 ]; then
  for p in $NODE_PROJ; do add "docker compose -p $p down --rmi all"; done
fi
[ -n "$MODEL_VOLS" ] && add "docker volume rm $MODEL_VOLS   (model weights: downloaded again on a next install)"
if [ "$KEEP_NODE" = 0 ]; then
  [ -n "$DROP_VOLS" ] && add "docker volume rm $DROP_VOLS   (chain data of a node measured with voting power 0; its keys are in the backup)"
  [ -n "$KEEP_VOLS" ] && add "KEEP $KEEP_VOLS: no running node of that project was read (a stopped node's voting power cannot be), so its data is not removed"
fi
[ "$KEEP_NODE" = 1 ] && [ "$NODE_PRESENT" = 1 ] && add "KEEP the node kit as it is (--keep-node): ${NODE_PROJ:-no container}, ${NODE_VOLS:-no volume}, and its crontab line"
[ "$DELETE_KEYS" = 1 ] && add "docker volume rm $KEYS_VOL   (--delete-keys: only after the backup above is verified)"
[ "$DELETE_KEYS" = 1 ] && [ "$SECRETS_PRESENT" = 1 ] && add "remove the keyring passphrase $(printf '%q' "$SECRETS_DIR/keyring-passphrase"), then its directory if nothing else is in it   (--delete-keys: with the volume, after the backup above is verified)"
[ "$DELETE_KEYS" = 1 ] && [ -n "$GPU_DIR" ] && add "remove the slots' machine files $(printf '%q' "$GPU_DIR")   (--delete-keys: with the keys they bind to the cards, after the backup above is verified)"
[ "$CRON" = read ] && [ -n "$CRON_MINE" ] && add "remove $(printf '%s\n' "$CRON_MINE" | grep -c .) crontab line(s) that run a file of this clone; every other line is kept as it is"
[ "$CRON" = unread ] && add "crontab NOT READ: it is left untouched -- check it yourself for lines naming $REPO/ (crontab -l)"
[ "${#HEALTH_FILES[@]}" -gt 0 ] && add "remove the miner self-test's files: $(_list "${HEALTH_FILES[@]}")"
[ "${#APP_FILES[@]}" -gt 0 ] && add "remove the application files: $(_list "${APP_FILES[@]}")"

say ""
say "== [uninstall] plan =="
if [ -z "$PLAN" ]; then
  say "  nothing of the Dendra kit on this host (from this clone). Nothing to do."
  [ "$CRON" = unread ] && { say "  (the crontab could not be read: see above)"; exit 3; }
  exit 0
fi
printf '%s' "$PLAN"
say ""
say "  KEPT, ALWAYS -- remove them yourself if you mean to:"
[ "$DELETE_KEYS" = 1 ] || say "    miner keys : docker volume rm ${KEYS_VOL:-dendra-miner_miner-keys}   (only once the backup is copied somewhere safe)"
[ "$DELETE_KEYS" = 1 ] || [ "$SECRETS_PRESENT" = 0 ] || say "    passphrase : rm -- $(printf '%q' "$SECRETS_DIR/keyring-passphrase")   (the keyring does not open without it: only with the volume, after its backup)"
[ "$DELETE_KEYS" = 1 ] || [ -z "$GPU_DIR" ] || say "    card slots : rm -r -- $(printf '%q' "$GPU_DIR")   (it says which kept key is which card's identity: only with the keys)"
if [ -n "$KEYS_VOL" ] || [ "$SECRETS_PRESENT" = 1 ]; then
  say "  ⚠ The miner-keys volume and the passphrase file are this miner's account TOGETHER: losing the volume, or the"
  say "    passphrase while the keyring is encrypted, loses the account and its rewards -- unless its 24-word recovery"
  say "    phrase was written down. Keep the backup of both, and keep the two apart: together they are a key in clear."
fi
say "    this clone : rm -rf -- $(printf '%q' "$REPO")"
say "    Docker, the NVIDIA toolkit, the apt repositories and the docker group were installed for the whole"
say "    system and may serve other things: sudo apt-get remove docker-ce docker-ce-cli containerd.io"
say "    docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit ; sudo rm /etc/apt/sources.list.d/docker.list"
say "    /etc/apt/sources.list.d/nvidia-container-toolkit.list ; sudo gpasswd -d $(id -un 2>/dev/null || echo "\$USER") docker"
say "  ⚠ Once its containers are gone, the miner-keys volume is referenced by nothing: a routine"
say "    'docker volume prune' would delete it without asking. Keep the backup."

if [ "$YES" != 1 ]; then
  say ""
  say "  Nothing was done. Re-run with --yes to apply the plan above."
  exit 2
fi
# A REGISTERED IDENTITY, OR ONE NOT READ: nothing is applied. Uninstalling would leave it on the chain with its
# stake, drawn onto juries it no longer answers -- and an unknown is not "not registered".
if [ "$HAS_REG" = 1 ] && [ "$KEEP_REG" = 0 ]; then
  refuse "a miner identity of this host is still REGISTERED on the chain: uninstalling does not take it off the network."
  _reg_list registered | sed 's/^/           /' >&2
  say "         Leave first -- the exit tool drains the miner, waits until the chain accepts, and gives the stake back:"
  _leave_cmd | sed 's/^/           /'
  say "         then run this again. To remove the kit anyway and keep the keys: --yes --keep-registration. Nothing was done."
  exit 2
fi
if [ "$HAS_UNREAD" = 1 ] && [ "$KEEP_REG" = 0 ]; then
  unmeasurable "whether every miner identity of this host is registered could not be read ($(_reg_list unread | tr '\n' ' ')${REG_WHY}). An unknown is not 'not registered': read it again once the node and the miner image answer (bash $MINER_KIT/exit-miner.sh --status), or remove the kit anyway and keep the keys: --yes --keep-registration. Nothing was done."
fi

# ---------------------------------------------------------------- 4. apply
step(){ printf '\n== [uninstall] %s ==\n' "$*"; }

# 4.1 THE BACKUP, VERIFIED, BEFORE ANYTHING IS REMOVED. Written in the HOME of this account and never in
# the clone: deploy/ is published as a whole, and a key archive must never sit in a tree that is.
BACKUP=""
# THE IMAGE THAT READS THE KEYS IS THE KIT'S OWN. It runs with the miner-keys volume mounted, and the
# archive it writes is all that stands before --delete-keys: so it is taken from the containers of THIS
# clone's kits only, never from another project on the host, and nothing is pulled.
_tar_image(){ # an image already present here that carries tar: never a pull during an uninstall
  local i
  for i in $(printf '%s\n' "$CTRS" | awk -F'|' -v m1="$MINER_KIT" -v m2="$REPO_P/deploy/testnet-miner" \
               -v n1="$NODE_KIT" -v n2="$REPO_P/deploy/testnet-node" '$2==m1 || $2==m2 || $2==n1 || $2==n2 {print $5}') ; do
    docker inspect --format '{{.Config.Image}}' "$i" 2>/dev/null
  done | sort -u | while read -r i; do [ -n "$i" ] && docker image inspect "$i" >/dev/null 2>&1 && { echo "$i"; break; }; done
}
_backup_vol(){ # _backup_vol <volume> <archive> <tar args before '.'>...
  local vol="$1" out="$2"; shift 2
  docker run --rm --network none -v "$vol:/k:ro" --entrypoint tar "$TARIMG" -C /k "$@" -cf - . > "$out" 2>/dev/null || return 1
  chmod 600 "$out"
  [ -s "$out" ] || return 1
  tar -tf "$out" >/dev/null 2>&1 || return 1
}
if [ "$BACKUP_NEEDED" = 1 ]; then
  step "backing up the keys"
  TARIMG="$(_tar_image)"
  if [ -z "$TARIMG" ]; then
    # Then the images the kit's compose files name -- still the kit's, never a stranger's.
    for i in dendra/miner:latest dendra/node:latest ollama/ollama:latest; do
      docker image inspect "$i" >/dev/null 2>&1 && { TARIMG="$i"; break; }
    done
  fi
  [ -n "$TARIMG" ] || fail "no image of this clone's kit is present on this host, and this file pulls nothing and borrows no other project's image: the keys cannot be backed up, so nothing was removed. Start the kit once (cd $(printf '%q' "$MINER_KIT") && docker compose up -d), then run this again."
  BACKUP="$HOME/dendra-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  case "$BACKUP/" in "$REPO/"*|"$REPO_P/"*) fail "the backup would land inside the clone ($BACKUP), which is published: nothing was removed.";; esac
  ( umask 077; mkdir -p "$BACKUP" ) || fail "cannot create $BACKUP: nothing was removed."
  chmod 700 "$BACKUP"
  for v in $KEYS_VOL; do
    _backup_vol "$v" "$BACKUP/$v.tar" || fail "the backup of $v failed or does not list: nothing was removed (partial files are in $BACKUP)."
    # The keyring is what holds the address: an archive without it is not a backup of the miner.
    tar -tf "$BACKUP/$v.tar" 2>/dev/null | grep -q '^\./cosmos/' \
      || fail "the archive of $v holds no keyring (./cosmos/): it does not back up the miner's address. Nothing was removed."
    say "  [OK] $BACKUP/$v.tar  ($(tar -tf "$BACKUP/$v.tar" | grep -c .) entries, keyring included)"
  done
  # THE KEYRING'S PASSPHRASE, in the same backup: an encrypted keyring in an archive is worth nothing
  # without it. THE FILE, NEVER ITS DIRECTORY: a directory that also held other things -- $HOME, ~/.ssh --
  # would be copied whole into an archive that names itself the passphrase. Archived by the host's own tar,
  # and verified the same way.
  if [ "$SECRETS_PRESENT" = 1 ]; then
    ( umask 077; tar -C "$SECRETS_DIR" -cf "$BACKUP/miner-secrets.tar" ./keyring-passphrase ) 2>/dev/null \
      || fail "the backup of the keyring passphrase ($SECRETS_DIR/keyring-passphrase) failed: nothing was removed."
    chmod 600 "$BACKUP/miner-secrets.tar"
    tar -tf "$BACKUP/miner-secrets.tar" 2>/dev/null | grep -q '^\./keyring-passphrase$' \
      || fail "the archive of $SECRETS_DIR holds no keyring-passphrase: nothing was removed."
    say "  [OK] $BACKUP/miner-secrets.tar  (the keyring passphrase: with the volume's archive it opens the keys --"
    say "       keep the two apart, or offline)"
  fi
  # A passphrase file a slot names apart from slot 0's (edited by hand): backed up as well, one archive each.
  _n=1
  while IFS= read -r _sd; do
    [ -n "$_sd" ] || continue
    _n=$((_n + 1))
    ( umask 077; tar -C "$_sd" -cf "$BACKUP/miner-secrets-$_n.tar" ./keyring-passphrase ) 2>/dev/null \
      || fail "the backup of the keyring passphrase ($_sd/keyring-passphrase) failed: nothing was removed."
    chmod 600 "$BACKUP/miner-secrets-$_n.tar"
    tar -tf "$BACKUP/miner-secrets-$_n.tar" 2>/dev/null | grep -q '^\./keyring-passphrase$' \
      || fail "the archive of $_sd holds no keyring-passphrase: nothing was removed."
    say "  [OK] $BACKUP/miner-secrets-$_n.tar  (the keyring passphrase in $_sd, which a slot's env names)"
  done <<EOF
$SECRETS_OTHERS
EOF
  if [ "$DROP_NODE_DATA" = 1 ]; then
    for v in $DROP_VOLS; do
      _backup_vol "$v" "$BACKUP/$v.keys.tar" --exclude=./data || fail "the backup of the node keys in $v failed: nothing was removed."
      tar -tf "$BACKUP/$v.keys.tar" 2>/dev/null | grep -q '^\./config/priv_validator_key\.json$' \
        || fail "the archive of $v holds no config/priv_validator_key.json: nothing was removed."
      say "  [OK] $BACKUP/$v.keys.tar  (config/ and keyrings, chain data excluded)"
    done
  fi
  [ -n "${WSL_DISTRO_NAME:-}" ] && say "  from Windows, that folder is inside the distribution $WSL_DISTRO_NAME (Explorer: Linux > $WSL_DISTRO_NAME > ${BACKUP#/})"
fi

# 4.2 CONTAINERS AND IMAGES, by project name: the -p form resolves the project by its labels, so every
# container is taken whatever profile started it -- a per-directory `down` misses the judge's. Run from
# a directory with no compose file, so that nothing but the label decides. Never -v.
# The slots k first, highest first, WITHOUT --rmi: they run slot 0's images, which go with slot 0, last.
for p in $MINER_DOWN; do
  step "removing the miner project $p"
  if [ "$p" = dendra-miner ]; then
    ( cd / && docker compose -p "$p" down --rmi all ) || fail "docker compose -p $p down --rmi all failed (see above)."
  else
    ( cd / && docker compose -p "$p" down ) || fail "docker compose -p $p down failed (see above)."
  fi
done
# The shared judge's network belongs to no project: removed here, once every miner project is down.
if [ "$NET_RIG" = present ]; then
  if docker network rm dendra-rig >/dev/null 2>&1; then say "  [OK] network dendra-rig removed"
  else warn "the dendra-rig network is still there: a container outside this clone's kits is attached to it."; fi
fi
if [ "$KEEP_NODE" = 0 ]; then
  for p in $NODE_PROJ; do
    step "removing the node project $p"
    ( cd / && docker compose -p "$p" down --rmi all ) || fail "docker compose -p $p down --rmi all failed (see above)."
  done
fi

# 4.3 VOLUMES, BY NAME. The keys only under --delete-keys, and only here, after the verified backup.
RM_VOLS="$MODEL_VOLS"
[ "$DROP_NODE_DATA" = 1 ] && RM_VOLS="$RM_VOLS $DROP_VOLS"
[ "$DELETE_KEYS" = 1 ] && [ -n "$BACKUP" ] && RM_VOLS="$RM_VOLS $KEYS_VOL"
for v in $RM_VOLS; do
  docker volume rm "$v" >/dev/null 2>&1 || fail "docker volume rm $v failed (still used by a container?)."
  say "  [OK] volume $v removed"
done
# The passphrase goes with the keys, never before them and never alone: only under --delete-keys, only
# once the volume is removed and the backup above, which holds both, is verified. ⛔ THE FILE, then the
# directory only if it is EMPTY (rmdir refuses otherwise): `rm -rf` on the directory took whatever else it
# held -- with DENDRA_SECRETS_DIR=$HOME, the verified backup written under $HOME a few lines above.
if [ "$DELETE_KEYS" = 1 ] && [ -n "$BACKUP" ] && [ "$SECRETS_PRESENT" = 1 ] && [ -z "$SECRETS_WHY" ] && [ -s "$BACKUP/miner-secrets.tar" ]; then
  rm -f -- "$SECRETS_DIR/keyring-passphrase" || fail "the keyring passphrase could not be removed: $SECRETS_DIR/keyring-passphrase"
  rmdir -- "$SECRETS_DIR" 2>/dev/null || say "  [i] $SECRETS_DIR is kept: something else is in it"
  say "  [OK] keyring passphrase removed ($SECRETS_DIR/keyring-passphrase); its copy is in the backup"
fi
# The slots' machine files go with the keys they bind to the cards: only under --delete-keys, after the backup.
if [ "$DELETE_KEYS" = 1 ] && [ -n "$BACKUP" ] && [ -n "$GPU_DIR" ]; then
  rm -rf -- "$GPU_DIR" || fail "the slots' machine files could not be removed: $GPU_DIR"
  say "  [OK] slots' machine files removed ($GPU_DIR)"
fi
if [ "$KEEP_NODE" = 0 ] && docker network inspect dendra-chain >/dev/null 2>&1; then
  warn "the dendra-chain network is still there: a container outside this clone's kits is attached to it."
fi

# 4.4 THE CRONTAB. `crontab FILE` replaces the WHOLE table, so the table written back is the one READ above
# minus this clone's lines, and it is read again afterwards and compared, byte for byte.
if [ "$CRON" = read ] && [ -n "$CRON_MINE" ]; then
  step "removing this clone's crontab lines"
  _cdir="$(mktemp -d)"
  # Read again, into a FILE: the lines that stay are copied from the table itself, byte for byte, and a
  # read that fails now stops here rather than writing back a table that was never read.
  crontab -l > "$_cdir/before" 2>/dev/null || { rm -rf "$_cdir"; fail "the crontab could not be read again: it was not changed."; }
  grep -vF -- "$REPO/" "$_cdir/before" > "$_cdir/after"
  if [ "$KEEP_NODE" = 1 ]; then
    grep -F -- "$REPO/" "$_cdir/before" | grep 'validator_health\.sh' >> "$_cdir/after"
  fi
  crontab "$_cdir/after" || { rm -rf "$_cdir"; fail "crontab could not be written; the table was not changed by this file."; }
  crontab -l > "$_cdir/check" 2>/dev/null
  if ! cmp -s "$_cdir/after" "$_cdir/check"; then
    rm -rf "$_cdir"; fail "the crontab read back differs from what was written: check it with crontab -l."
  fi
  rm -rf "$_cdir"
  say "  [OK] $(printf '%s\n' "$CRON_MINE" | grep -c .) line(s) removed, the others kept"
fi

# 4.5 THE MINER SELF-TEST'S FILES, after the crontab line that writes them is gone.
if [ "${#HEALTH_FILES[@]}" -gt 0 ]; then
  rm -f -- "${HEALTH_FILES[@]}" || fail "the miner self-test's files could not be removed: $(_list "${HEALTH_FILES[@]}")"
  say "  [OK] miner self-test files removed"
fi

# 4.6 THE APPLICATION.
if [ "${#APP_FILES[@]}" -gt 0 ]; then
  rm -f -- "${APP_FILES[@]}"
  rmdir "$CONFIG_DIR" 2>/dev/null || true
  say "  [OK] application files removed"
fi

say ""
say "  Done."
[ -n "$BACKUP" ] && say "  The keys are backed up in $BACKUP (readable by you only). Copy them somewhere safe."
[ "$DELETE_KEYS" = 1 ] || [ -z "$KEYS_VOL" ] || say "  The miner's keys are KEPT in the volume ${KEYS_VOL}."
[ "$DELETE_KEYS" = 1 ] || [ "$SECRETS_PRESENT" = 0 ] || say "  The keyring passphrase is KEPT in $SECRETS_DIR: the keys do not open without it."
[ "$CRON" = unread ] && { say "  The crontab could not be read and was left untouched: check it (crontab -l)."; exit 3; }
exit 0
