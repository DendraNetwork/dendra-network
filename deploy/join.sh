#!/usr/bin/env bash
# join.sh: join the Dendra testnet in one command (canonical path).
# Wraps the existing building blocks (docker/node-join.sh, deploy/testnet-miner, deploy/testnet-node,
# deploy/testnet/anchor_vrf_key.sh, miner.py); it does not reimplement them.
#
#   Miner (default):
#     CONFIG_URL=<URL of network-info.txt> bash deploy/join.sh
#     # or explicit:
#     DENDRA_NODE=tcp://HOST:26657 DENDRA_RELAY=http://HOST:8645 FAUCET=http://HOST:4500 bash deploy/join.sh
#   Miner-judge (audit committee):        ... bash deploy/join.sh --judge   (the default on a machine that can
#                                         judge, deploy/hw_probe.sh --can-judge: true; --no-judge declines it)
#   One identity per NVIDIA card:         ... bash deploy/join.sh --gpus all   (--plan shows it, changes nothing)
#   Validator (sync + guided bond + VRF): CONFIG_URL=... bash deploy/join.sh --validator
#   No usable NVIDIA GPU: no mining model runs on the CPU. With MOE_CPU_MIN_RAM_MB of RAM the machine joins as a
#   judge on the CPU (--judge implied), below it is refused: deploy/hw_probe.sh --role decides, and says why.
#
# Canonical defaults (the one place they are set; never the 'hash' backend on a real network):
#   DENDRA_MODEL_ID=llama3.1:8b-instruct-q4_K_M, DENDRA_EMBED_MODE=backend, DENDRA_EMBED_API_MODEL=nomic-embed-text
# Status: research / testnet. The validator bond is a deliberate action (confirmation required, never silent).
set -u

# The options of this run, kept before they are parsed away: the update instruction (kit_update_hint) tells the
# operator to re-run join.sh with the same options, and prints them rather than asking anyone to remember them.
JOIN_ARGS_ORIG=("$@")
ROLE=miner; JUDGE=0; MINER_ID="${MINER_ID:-}"
# How a new node reaches the head: --replay (every block from the genesis) or --statesync (a snapshot,
# verified against a trust point derived over TLS from docker/STATESYNC_RPC). Empty = the role decides,
# see node_sync_plan. Initialised here so that nothing inherited from the environment chooses it.
SYNC_CHOICE=""
# OWN_NODE: a miner runs its own full node unless it explicitly opts out. See the note in run_miner:
# the opt-out trades away the ability to verify the chain for itself, so it is a decision the operator
# makes on purpose, never a default.
OWN_NODE=1
# Keys at rest. A new miner's keyring is created encrypted, under a passphrase this script generates
# outside the volume and outside the clone; --plain-keys refuses that, explicitly. An existing installation
# whose keys are unencrypted is migrated by deploy/testnet-miner/encrypt-keys.sh only, never in passing.
PLAIN_KEYS=0
# The Final Testnet Season payout address (--payout-address): checked here, before anything changes.
PAYOUT_ADDRESS=""
# Owner mode (--owner): the address of the key that registers the miner and holds its stake, kept off this
# machine. Checked here, before anything changes, with the same checksum as the payout address.
OWNER_ADDRESS=""
# One identity per card (--gpus), opt-in: without it nothing below changes, to the byte. GPUS_SPEC is the
# operator's list, PLAN_ONLY prints the plan and changes nothing, REASSIGN binds an existing slot to another
# card on purpose (a slot never changes card in silence). See plan_gpu_slots.
GPUS_SPEC=""; PLAN_ONLY=0; REASSIGN=""; ID_GIVEN=0
# The judge is on by default on a machine that can judge: --no-judge declines the seat and
# is recorded in the kit's .env (DENDRA_NO_JUDGE), so a re-run and the automatic update keep the refusal; --judge
# asks for it and clears a recorded refusal. JUDGE_DEFAULTED says the seat comes from the default, not from
# --judge: that seat is checked against the disk floor before anything is pulled, an explicit --judge is warned.
NO_JUDGE=0; JUDGE_DEFAULTED=0
# The automatic update: on unless DENDRA_AUTO_UPDATE=0, carried in the kit's .env.
# --no-auto-update writes 0, --auto-update writes 1; empty here means "not given", and a kept .env then decides.
AUTO_UPDATE="${DENDRA_AUTO_UPDATE:-}"
# --payout-lock: the payout address is declared locked with the first declaration (extended
# to the HiveOS rig). Empty = not given; the kept .env then decides, else 0.
PAYOUT_LOCK=""
# --accept-new-signers: the one way past a docker/RELEASE_SIGNERS that differs from the one this kit recorded
# (verify_release_signers): it prints the fingerprints of the new file and records it.
ACCEPT_NEW_SIGNERS=0
# --after-upgrade <plan>: the join that follows a governed upgrade (deploy/testnet-miner/node_upgrade_watch.sh runs it).
# network-info.txt may still declare the epoch before the upgrade; verify_consensus_epoch then accepts this tree's
# higher epoch only when this kit's node answers that plan applied (after_upgrade_applied). A gesture of one run,
# never written into the recorded re-run. AFTER_UPGRADE_SWITCH=1 once this run starts the node on this tree's image
# at the plan's halt (node_halt_confirmed), which is how a kit whose watch is off crosses the upgrade.
AFTER_UPGRADE=""; AFTER_UPGRADE_SWITCH=0; AFTER_UPGRADE_HALT_WHY=""
while [ $# -gt 0 ]; do case "$1" in
  --validator) ROLE=validator; shift;;
  --miner) ROLE=miner; shift;;
  --judge) JUDGE=1; shift;;
  --no-judge) NO_JUDGE=1; shift;;
  --auto-update) AUTO_UPDATE=1; shift;;
  --no-auto-update) AUTO_UPDATE=0; shift;;
  --payout-lock) PAYOUT_LOCK=1; shift;;
  --accept-new-signers) ACCEPT_NEW_SIGNERS=1; shift;;
  --after-upgrade) AFTER_UPGRADE="${2:-}"; [ -n "$AFTER_UPGRADE" ] || { echo "[join] --after-upgrade needs the name of the upgrade plan. Nothing was changed."; exit 2; }; shift 2;;
  --own-node) OWN_NODE=1; shift;;
  --remote-rpc|--no-node) OWN_NODE=0; shift;;
  --replay) SYNC_CHOICE=replay; shift;;
  # --replay wins in either order: a later --statesync does not undo it (a replay needs no trust point).
  --statesync) [ "$SYNC_CHOICE" = replay ] || SYNC_CHOICE=statesync; shift;;
  --id) MINER_ID="${2:-}"; ID_GIVEN=1; shift 2;;
  --gpus) GPUS_SPEC="${2:-}"; [ -n "$GPUS_SPEC" ] || { echo "[join] --gpus needs a value: all, card indices (0,2) or card UUIDs. Nothing was changed."; exit 2; }; shift 2;;
  --plan) PLAN_ONLY=1; shift;;
  --reassign) REASSIGN="${2:-}"; [ -n "$REASSIGN" ] || { echo "[join] --reassign needs <slot>=<card index or UUID>. Nothing was changed."; exit 2; }; shift 2;;
  --plain-keys) PLAIN_KEYS=1; shift;;
  --payout-address) PAYOUT_ADDRESS="${2:-}"; [ -n "$PAYOUT_ADDRESS" ] || { echo "[join] --payout-address needs an address (dendra1...)"; exit 2; }; shift 2;;
  --owner) OWNER_ADDRESS="${2:-}"; [ -n "$OWNER_ADDRESS" ] || { echo "[join] --owner needs an address (dendra1...)"; exit 2; }; shift 2;;
  -h|--help)
    sed -n '2,18p' "$0" 2>/dev/null || true
    echo "Modes: (default) miner+own node | --judge miner+audit committee | --validator node+guided bond+VRF | --id NAME"
    echo "       --remote-rpc  read the chain from the operator's public RPC instead of running a node"
    echo "       --replay      a new node replays every block from the genesis instead of starting from a snapshot"
    echo "       --statesync   a new validator node is configured to state-sync, as a miner's new node is by default:"
    echo "                     it starts from a snapshot when a peer offers one (--replay wins over it)"
    echo "       --payout-address dendra1...  where the Final Testnet Season pays this miner (a key not on this machine)"
    echo "       --owner dendra1...  owner mode: that key (not on this machine) registers the miner and holds its stake;"
    echo "                           the miner prints the commands its owner signs, and mines once registered"
    echo "       --plain-keys  keep a new miner's keys unencrypted (by default they are encrypted under a generated passphrase)"
    echo "       --gpus all|0,2|GPU-<uuid>,...  one miner identity per NVIDIA card (each with its own stake and keys);"
    echo "                                      without it the kit runs one identity, as before"
    echo "       --plan  print what --gpus would do (cards, slots, judge, faucet, RAM) and change nothing"
    echo "       --no-judge    decline the judge seat on a machine that can judge (the seat is on by default there);"
    echo "                     recorded in the kit's .env, so a re-run and the automatic update keep the refusal"
    echo "       --no-auto-update | --auto-update   the daily automatic update of this kit (on by default; DENDRA_AUTO_UPDATE)"
    echo "       --payout-lock  declare the payout address locked with its first declaration (cannot be corrected later)"
    echo "       --accept-new-signers  accept a docker/RELEASE_SIGNERS that differs from the one this kit recorded (prints its fingerprints);"
    echo "                             a gesture of this run only, never written into the recorded re-run the automatic update replays"
    echo "       --after-upgrade <plan>  the join after the governed upgrade <plan> (node_upgrade_watch.sh runs it): a tree one epoch"
    echo "                             ahead of network-info.txt is accepted only when this kit's node (under --remote-rpc,"
    echo "                             the chain RPC it reads) answers that plan applied with a height; a gesture of this run only."
    echo "                             A node of this kit that the chain confirms at that plan's halt is first started on this"
    echo "                             tree's node image (the gesture of a kit whose automatic update is off, a HiveOS rig)"
    exit 0;;
  *) echo "[join] unknown arg: $1 (see --help)"; exit 2;;
esac; done
if [ "$JUDGE" = 1 ] && [ "$NO_JUDGE" = 1 ]; then
  printf '[join] --judge and --no-judge together: pick one. Nothing was changed.\n' >&2; exit 2
fi
case "$AUTO_UPDATE" in ""|0|1) : ;; *) printf '[join] DENDRA_AUTO_UPDATE=%s is neither 0 nor 1. Nothing was changed.\n' "$AUTO_UPDATE" >&2; exit 2 ;; esac
# The card list is validated before anything else: it ends up in file names and on compose command lines,
# and install.sh hands it over through a shell. One form or a refusal, never a repair. install.sh and
# install.ps1 apply this same expression before they relay it (GPUS_SPEC_RE, the same text in each file).
# The expression judges the whole value (bash's own `=~`, where ^ and $ are the ends of the string), never
# one line of it: `grep -q` succeeds as soon as one line matches, so a value carrying a newline would pass
# the gate with any second line, and install.sh runs that line in a shell.
GPUS_SPEC_RE='^(all|[0-9]+(,[0-9]+)*|GPU-[0-9a-f-]+(,GPU-[0-9a-f-]+)*)$'
REASSIGN_RE='^(0|[1-9][0-9]*)=([0-9]+|GPU-[0-9a-f-]+)$'
if [ -n "$GPUS_SPEC" ]; then
  [[ $GPUS_SPEC =~ $GPUS_SPEC_RE ]] \
    || { printf '[join] --gpus %s is not all, a list of card indices (0,2) or a list of card UUIDs (GPU-...). Nothing was changed.\n' "$GPUS_SPEC" >&2; exit 2; }
  [ "$ROLE" = miner ] || { printf '[join] --gpus applies to a miner (one identity per card), not to --%s. Nothing was changed.\n' "$ROLE" >&2; exit 2; }
fi
if [ -n "$AFTER_UPGRADE" ] && ! [[ $AFTER_UPGRADE =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
  printf '[join] --after-upgrade %s is not the name of an upgrade plan (letters, digits and . _ -). Nothing was changed.\n' "$AFTER_UPGRADE" >&2; exit 2
fi
if [ -n "$REASSIGN" ]; then
  [[ $REASSIGN =~ $REASSIGN_RE ]] \
    || { printf '[join] --reassign %s is not <slot>=<card index or UUID>. Nothing was changed.\n' "$REASSIGN" >&2; exit 2; }
fi
if [ "$PLAN_ONLY" = 1 ] && [ "$ROLE" != miner ]; then
  printf '[join] --plan describes a miner'"'"'s cards and identities, not --%s. Nothing was changed.\n' "$ROLE" >&2; exit 2
fi

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
REPO="$(cd "$SELF/.." 2>/dev/null && pwd)"
# Repository root, validated by a marker, never inferred on its own.
# `$0` is "deploy/join.sh" only when the script is run as a file. Read from a pipe
# (`tr -d '\r' < deploy/join.sh | bash -s -- --validator`, the form the runbooks recommend to strip
# CRLF), `$0` is "bash": `dirname` returns ".", SELF becomes the current directory, and the `..`
# climbs one level too far. The script would then look for `<parent>/deploy/testnet-node`, which does
# not exist, and die on "docker compose up failed", a message that says nothing about the real
# problem. So the root is verified by the presence of the kit, and searched upwards from the CWD when
# the computation is wrong. `DENDRA_REPO` still takes precedence.
if [ -n "${DENDRA_REPO:-}" ] && [ -d "$DENDRA_REPO/deploy/testnet-node" ]; then
  REPO="$DENDRA_REPO"
elif [ ! -d "$REPO/deploy/testnet-node" ]; then
  _d="$PWD"
  while [ "$_d" != "/" ] && [ -n "$_d" ]; do
    if [ -d "$_d/deploy/testnet-node" ]; then REPO="$_d"; break; fi
    _d="$(dirname "$_d")"
  done
fi
[ -d "$REPO/deploy/testnet-node" ] || { echo "[join] FATAL: repository root not found (try: export DENDRA_REPO=/path/to/Dendra)"; exit 1; }
MINER_KIT="$REPO/deploy/testnet-miner"
NODE_KIT="$REPO/deploy/testnet-node"

say(){ printf '%s\n' "$*"; }
die(){ printf '[join] FATAL: %s\n' "$*" >&2; exit 1; }
warn(){ printf '[join] WARN: %s\n' "$*"; }

# ---------------------------------------------------------------- keys at rest and the payout address
# payout_address_check <address> -> 0 payable | 1 refused | 3 not verifiable here; the reason on stdout.
# The check is the programme's own (final_season_address.py::payable_address, in this clone): the bech32
# checksum (an address one character off is refused, not paid to nobody) and the module accounts the
# chain refuses to credit. No second implementation here: two checks that drift apart accept what one of
# them refuses. Without python3 nothing is verified, and an unverified address is not written.
payout_address_check(){
  local svc="" c
  for c in "$REPO/services" "$REPO/services"; do
    [ -r "$c/final_season_address.py" ] && { svc="$c"; break; }
  done
  [ -n "$svc" ] || { echo "the address check (final_season_address.py) is not in this clone"; return 3; }
  command -v python3 >/dev/null 2>&1 || { echo "python3 is required to verify the address checksum, and this host has none"; return 3; }
  ( cd "$svc" && python3 -I -c '
import sys
sys.path.insert(0, ".")
try:
    from final_season_address import payable_address
except Exception as e:
    print("the address check could not be loaded: " + type(e).__name__)
    sys.exit(3)
why = payable_address(sys.argv[1])
print(why)
sys.exit(1 if why else 0)' "$1" 2>/dev/null )
}

# miner_secrets_dir -> the directory of the keyring passphrase: DENDRA_SECRETS_DIR from the environment,
# else the one the kit's .env names, else ~/.config/dendra/miner-secrets. Outside the miner-keys volume and
# outside this clone, which is published: a passphrase next to what it protects protects nothing.
miner_secrets_dir(){
  local d="${DENDRA_SECRETS_DIR:-}"
  if [ -z "$d" ] && [ -f "$MINER_KIT/.env" ]; then
    d="$(sed -n 's/^DENDRA_SECRETS_DIR=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  fi
  printf '%s' "${d:-${XDG_CONFIG_HOME:-$HOME/.config}/dendra/miner-secrets}"
}

# secrets_dir_ok <dir> -> 0 when <dir> may hold the passphrase; otherwise prints why and returns 1. The check
# is deploy/testnet-miner/passphrase-dir.sh, the one copy encrypt-keys.sh and uninstall.sh source too: an
# absolute plain path compose reads as written, outside this clone, and a directory of its own -- never /,
# $HOME or a directory above it, nor one that already holds something else, since the kit sets it 0700 and
# uninstall --delete-keys removes the passphrase from it. A check that cannot be loaded refuses: no default.
secrets_dir_ok(){
  local lib="$REPO/deploy/testnet-miner/passphrase-dir.sh"
  if ! declare -F dendra_secrets_dir_check >/dev/null 2>&1; then
    [ -r "$lib" ] || { echo "the check of that directory ($lib) is missing from this clone"; return 1; }
    . "$lib" || { echo "the check of that directory ($lib) could not be loaded"; return 1; }
  fi
  dendra_secrets_dir_check "$1" "$REPO"
}

# ensure_miner_passphrase <dir> -> the passphrase file <dir>/keyring-passphrase, created when absent: the
# directory 0700, the file 0600 from its creation (umask 077), 32 random bytes written as 64 hex digits.
# An existing file is never replaced: it is what an encrypted keyring opens with, and a new one would
# lock that keyring for good. The new file is linked into place (`ln` refuses an existing name) and only
# then is it there. Sets MINER_PASSPHRASE_STATE=created|kept; 1 on failure.
ensure_miner_passphrase(){
  local d="$1" f t n
  f="$d/keyring-passphrase"
  if [ -e "$f" ] || [ -L "$f" ]; then
    if [ ! -f "$f" ] || [ ! -s "$f" ]; then warn "$f exists and is not a non-empty file: it is left as it is"; return 1; fi
    chmod 700 "$d" 2>/dev/null; chmod 600 "$f" 2>/dev/null
    MINER_PASSPHRASE_STATE=kept; return 0
  fi
  ( umask 077; mkdir -p "$d" ) || { warn "cannot create $d"; return 1; }
  chmod 700 "$d" || return 1
  t="$(umask 077; mktemp "$d/.keyring-passphrase.XXXXXX")" || { warn "cannot write in $d"; return 1; }
  ( umask 077; head -c 32 /dev/urandom | od -An -vtx1 | tr -dc '0-9a-f' > "$t" ) || { rm -f "$t"; return 1; }
  n="$(wc -c < "$t" | tr -dc '0-9')"
  if [ "${n:-0}" != 64 ]; then rm -f "$t"; warn "the generated passphrase is not 64 hex digits (${n:-?})"; return 1; fi
  chmod 600 "$t" || { rm -f "$t"; return 1; }
  if ! ln "$t" "$f" 2>/dev/null; then
    rm -f "$t"; warn "$f appeared while it was being written: the existing one is kept"
    [ -s "$f" ] && { MINER_PASSPHRASE_STATE=kept; return 0; }
    return 1
  fi
  rm -f "$t"
  MINER_PASSPHRASE_STATE=created
  return 0
}

# miner_keys_at_rest -> sets KEYS_AT_REST (encrypted | plain) and SECRETS_VALUE (the directory written into
# the kit's .env, empty for plain). Encrypted by default: the passphrase file is created when absent and the
# directory is written into the .env, so a new keyring is born encrypted. Plain when --plain-keys is given,
# or when the kit's .env already says so (an empty DENDRA_SECRETS_DIR= line, written by an earlier
# --plain-keys): a choice is remembered, not silently overridden. Keys that already exist keep their form
# either way (the miner reads it from its volume) until deploy/testnet-miner/encrypt-keys.sh is run.
miner_keys_at_rest(){
  local why
  SECRETS_VALUE=""
  if [ "$PLAIN_KEYS" = 1 ]; then
    KEYS_AT_REST=plain
    say "  [!] --plain-keys: a new keyring of this miner is created unencrypted in the miner-keys volume (and in every backup of it)."
    return 0
  fi
  if [ -z "${DENDRA_SECRETS_DIR:-}" ] && [ -f "$MINER_KIT/.env" ] && grep -q '^DENDRA_SECRETS_DIR=$' "$MINER_KIT/.env" 2>/dev/null; then
    KEYS_AT_REST=plain
    say "  [i] keys at rest: unencrypted, as chosen at an earlier run (--plain-keys). To encrypt them: bash $MINER_KIT/encrypt-keys.sh"
    return 0
  fi
  SECRETS_VALUE="$(miner_secrets_dir)"
  why="$(secrets_dir_ok "$SECRETS_VALUE")" || die "the passphrase directory $SECRETS_VALUE cannot be used: $why. Set DENDRA_SECRETS_DIR=<an absolute path of its own, outside the clone> (moving keyring-passphrase there if it exists), or pass --plain-keys to keep the keys unencrypted."
  ensure_miner_passphrase "$SECRETS_VALUE" || die "the keyring passphrase could not be prepared in $SECRETS_VALUE (see above). Nothing was started with it."
  KEYS_AT_REST=encrypted
  if [ "$MINER_PASSPHRASE_STATE" = created ]; then
    say "  [OK] keyring passphrase created: $SECRETS_VALUE/keyring-passphrase (directory 0700, file 0600, outside the volume and the clone)"
  else
    say "  [OK] keyring passphrase kept: $SECRETS_VALUE/keyring-passphrase"
  fi
  say "      A new keyring is created encrypted with it. Keys that are already unencrypted stay so until: bash $MINER_KIT/encrypt-keys.sh"
  say "      Back up this file with the miner-keys volume: without it an encrypted keyring is lost, unless its 24 words were written down."
}

# keys_banner_line -> one line of the final banner: where the keys are, and what to back up, read on the
# volume by the running miner's own keyring module (python3 -m modea.keyring address: at_rest=). KEYS_AT_REST
# says what a new keyring would be: a volume that already holds an unencrypted keyring keeps it until
# encrypt-keys.sh runs, so this line reads the volume instead of announcing "(encrypted keyring)". A value it cannot read is reported, never guessed.
keys_banner_line(){
  local out st pf
  out="$( cd "$MINER_KIT" 2>/dev/null && docker compose exec -T miner python3 -m modea.keyring address /data/keys 2>/dev/null )"
  st="$(printf '%s\n' "$out" | sed -n 's/^at_rest=//p' | head -1 | tr -d '\r')"
  pf="${SECRETS_VALUE:-$(miner_secrets_dir)}/keyring-passphrase"
  case "$st" in
    encrypted)
      printf '%s' "Docker volume 'miner-keys', keyring encrypted (read on the volume) + $pf -> back up both; losing the passphrase without the 24 words loses the account" ;;
    clear)
      printf '%s' "Docker volume 'miner-keys' = your miner identity, keys unencrypted (read on the volume) -> back it up; to encrypt: bash $MINER_KIT/encrypt-keys.sh"
      [ "${KEYS_AT_REST:-}" = encrypted ] && printf '%s' " ($pf encrypts a new keyring only: the existing one stays unencrypted until then)" ;;
    none)
      printf '%s' "Docker volume 'miner-keys' holds no key yet: the miner creates it at its first start$( [ "${KEYS_AT_REST:-}" = encrypted ] && printf ' encrypted with %s' "$pf" || printf ' unencrypted')" ;;
    both)
      printf '%s' "Docker volume 'miner-keys' holds two keyrings (one unencrypted, one encrypted): the miner refuses to choose; bash $MINER_KIT/encrypt-keys.sh says which is which" ;;
    *)
      printf '%s' "Docker volume 'miner-keys' = your miner identity; whether its keys are encrypted was not read (the running miner did not answer): docker compose -p dendra-miner exec -T miner python3 -m modea.keyring address /data/keys" ;;
  esac
}

# Both checks run before anything changes: a refused address or a contradictory --plain-keys stops the
# run here, with no .env written, no container started, no file created.
if [ -n "$PAYOUT_ADDRESS" ]; then
  _pwhy="$(payout_address_check "$PAYOUT_ADDRESS")"; _prc=$?
  case "$_prc" in
    0) PAYOUT_ADDRESS="$(printf '%s' "$PAYOUT_ADDRESS" | tr 'A-Z' 'a-z')"
       say "[join] payout address $PAYOUT_ADDRESS: checksum verified, payable" ;;
    1) printf '[join] --payout-address %s is refused: %s. Nothing was changed.\n' "$PAYOUT_ADDRESS" "${_pwhy:-not a payable address}" >&2
       exit 2 ;;
    *) printf '[join] --payout-address %s cannot be verified on this host: %s. Nothing was changed. Declare it later from the Dendra application, or install python3 and run this again.\n' "$PAYOUT_ADDRESS" "${_pwhy:-unknown}" >&2
       exit 2 ;;
  esac
fi
if [ -n "$OWNER_ADDRESS" ]; then
  [ "$ROLE" = miner ] || { printf '[join] --owner applies to a miner, not to --%s. Nothing was changed.\n' "$ROLE" >&2; exit 2; }
  _owhy="$(payout_address_check "$OWNER_ADDRESS")"; _orc=$?
  case "$_orc" in
    0) OWNER_ADDRESS="$(printf '%s' "$OWNER_ADDRESS" | tr 'A-Z' 'a-z')"
       say "[join] owner $OWNER_ADDRESS: checksum verified. The miner's identity derives from it, and only its key registers, updates or deletes the miner." ;;
    1) printf '[join] --owner %s is refused: %s. Nothing was changed.\n' "$OWNER_ADDRESS" "${_owhy:-not an account address}" >&2
       exit 2 ;;
    *) printf '[join] --owner %s cannot be verified on this host: %s. Nothing was changed. A wrong owner is another identity: install python3 and run this again.\n' "$OWNER_ADDRESS" "${_owhy:-unknown}" >&2
       exit 2 ;;
  esac
fi
if [ "$PLAIN_KEYS" = 1 ] && [ -e "$(miner_secrets_dir)/keyring-passphrase" ]; then
  printf '[join] --plain-keys refused: %s exists, so the keyring of this miner may already be encrypted with it, and unencrypted keys would be a keyring it cannot open. Nothing was changed. To keep a miner unencrypted, start from a new volume.\n' "$(miner_secrets_dir)/keyring-passphrase" >&2
  exit 2
fi

# ---------------------------------------------------------------- (0) config
# CONFIG_URL -> network-info.txt is parsed, never `source`d: downloaded text is not executed. Only the
# expected keys are accepted, in strict KEY=VALUE format, and each value is validated against a safe
# character set before assignment. This closes command injection over a plaintext CONFIG_URL: a value is
# assigned literally (no eval), and any value containing shell metacharacters is rejected.
_load_config_url(){ # _load_config_url <url> [soft] -- soft: an unreachable URL warns and returns 1
  local url="$1" tmp; tmp="$(mktemp)"
  if ! curl -fsSL "$url" -o "$tmp"; then
    rm -f "$tmp"
    [ "${2:-}" = soft ] || die "CONFIG_URL unreachable: $url"
    warn "CONFIG_URL unreachable: $url -- continuing with the settings already in the kits' .env files"
    return 1
  fi
  local k v line
  while IFS= read -r line; do
    case "$line" in \#*|"") continue;; esac
    k="${line%%=*}"; v="${line#*=}"
    case "$k" in
      # DENDRA_CAPACITY_URL is listed deliberately: this allow-list drops any unknown key (that is its
      # purpose, against injection), the derivation below cannot produce a usable value on this network,
      # and without a way for the operator to publish the right one the joiner has no remedy at all.
      # KIT_MIN and KIT_DEADLINE: the kit number below which the network's services refuse a
      # kit, and the UTC instant from which they do. Read here for verify_kit_version; absent until the operator
      # publishes them.
      CHAIN_ID|GENESIS_URL|GENESIS_SHA256|SEEDS|PERSISTENT_PEERS|DENDRA_NODE|DENDRA_RELAY|FAUCET|EXPLORER_URL|DENDRA_CAPACITY_URL|DENDRA_FINAL_SEASON_URL|CONSENSUS_EPOCH|CONSENSUS_EPOCH_PROVEN|KIT_VERSION|KIT_MIN|KIT_DEADLINE)
        # value must be a plain URL / host:port / node-id / hex: no shell metacharacters, no spaces
        case "$v" in
          *[!A-Za-z0-9:/._@,-]*) warn "config: value for $k rejected (invalid characters) — ignored"; continue;;
        esac
        export "$k=$v";;
      # The miner image is code, not a network setting, and this file arrives over plain HTTP.
      # It stays off the allow-list above: whoever can rewrite network-info.txt in transit would choose
      # the image this script pulls and runs, next to the volume that holds the miner's keys. A digest
      # pins bytes, not their author: an attacker's digest is just as well-formed. The pin is read from
      # the clone instead (docker/MINER_IMAGE, see miner_image_pin), which arrives over HTTPS git like
      # docker/GENESIS_SHA256. It is reported rather than silently dropped, so an operator who still
      # publishes the key learns that it no longer does anything.
      DENDRA_MINER_IMAGE) say "  [i] config: DENDRA_MINER_IMAGE served by CONFIG_URL is ignored: the miner image is pinned by docker/MINER_IMAGE in your clone";;
      # The state-sync trust point, where a new node's trust begins, does not come from this file either.
      # A light client starts from the trusted hash, not from the genesis, so a trust point read from this
      # plain-HTTP file let whoever sits on the path hand a new node their own chain, bypassing the
      # genesis pinned in docker/GENESIS_SHA256. It is derived instead, at every join, over TLS, from the
      # servers docker/STATESYNC_RPC names in the clone (statesync_trust_point). Reported when a value is
      # served, so an operator who still publishes these keys learns that they no longer do anything.
      STATESYNC_RPC|STATESYNC_TRUST_HEIGHT|STATESYNC_TRUST_HASH)
        [ -z "$v" ] || say "  [i] config: $k served by CONFIG_URL is ignored: the state-sync trust point is derived over TLS from docker/STATESYNC_RPC in your clone";;
      # The node image is code, like the miner image: pinned by docker/NODE_IMAGE in the clone only.
      DENDRA_NODE_IMAGE) say "  [i] config: DENDRA_NODE_IMAGE served by CONFIG_URL is ignored: the node image is pinned by docker/NODE_IMAGE in your clone";;
      *) : ;;  # unknown key = ignored (no execution of remote content)
    esac
  done < "$tmp"
  rm -f "$tmp"
  say "[join] config loaded from CONFIG_URL (${DENDRA_NODE:-?} / ${DENDRA_RELAY:-?} / ${FAUCET:-?})"
}
# A re-run without CONFIG_URL reads the one this kit was set up from. The miner kit's .env names it
# (run_miner writes it there), and without it a plain `bash deploy/join.sh` reaches run_miner with no
# network settings in the shell: the node kit is rewritten (its RPC moved to loopback) while the miner
# kit's .env is not, and a miner written by an older join.sh keeps dialling host.docker.internal, where
# nothing answers any more. Only when the shell names none of the three settings: an explicit one wins.
# Read softly: a network-info that does not answer today leaves the kit as it is (run_miner still points
# the miner at its own node, in place), rather than stopping a re-run that worked without it.
_CU_SOFT=""
if [ -z "${CONFIG_URL:-}" ] && [ "$ROLE" = miner ] && [ -f "$MINER_KIT/.env" ] \
   && [ -z "${DENDRA_NODE:-}${DENDRA_RELAY:-}${FAUCET:-}" ]; then
  _cu="$(sed -n 's/^CONFIG_URL=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  case "$_cu" in
    ""|*[!A-Za-z0-9:/._~%?=+@,-]*) : ;;
    *) CONFIG_URL="$_cu"; _CU_SOFT=soft; say "[join] CONFIG_URL read from $MINER_KIT/.env ($_cu)" ;;
  esac
fi
if [ -n "${CONFIG_URL:-}" ] && _load_config_url "$CONFIG_URL" $_CU_SOFT; then
  :
elif [ -f "$MINER_KIT/.env" ] && [ "$ROLE" = miner ]; then
  say "[join] config from $MINER_KIT/.env"
elif [ -f "$NODE_KIT/.env" ] && [ "$ROLE" = validator ]; then
  say "[join] config from $NODE_KIT/.env"
fi
# A re-run without DENDRA_UPDATE_HINT reads the one this copy was set up with. A package pinned to a release tag
# (HiveOS, install.sh --ref) names how its copy updates in DENDRA_UPDATE_HINT, and persist_kit_update keeps it in
# the miner kit's .env (DENDRA_KIT_UPDATE_HINT). The recorded re-run (DENDRA_KIT_RERUN), a hand-run
# `bash deploy/join.sh` and a HiveOS miner started again without re-installing name none: without this read, such
# a re-run would write the git instruction of a clone that follows main over the package's own, and the hourly watch
# would then send a copy pinned to a tag to main. A run that names one wins. Only the characters
# persist_kit_update writes are read; anything else is ignored, and reported. A copy that stops updating that way
# drops the line from its .env by hand.
if [ -z "${DENDRA_UPDATE_HINT:-}" ] && [ -f "$MINER_KIT/.env" ]; then
  _uh="$(sed -n 's/^DENDRA_KIT_UPDATE_HINT=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  if [ -n "$_uh" ] && [ -z "$(printf '%s' "$_uh" | LC_ALL=C tr -d 'A-Za-z0-9 :/._~%?=+@,;&()-')" ]; then
    DENDRA_UPDATE_HINT="$_uh"; say "[join] update instruction of this copy read from $MINER_KIT/.env (DENDRA_KIT_UPDATE_HINT): $_uh"
  elif [ -n "$_uh" ]; then
    warn "DENDRA_KIT_UPDATE_HINT in $MINER_KIT/.env holds a character join.sh does not write: ignored (the update instruction is the git one of a clone)."
  fi
fi
# Three settings a re-run keeps unless the command line names them: the recorded refusal of the judge
# seat (DENDRA_NO_JUDGE=1, cleared by --judge), the automatic update switch (DENDRA_AUTO_UPDATE) and the payout
# lock (DENDRA_PAYOUT_LOCK). Read, never sourced; a value outside its two words is ignored and said.
if [ "$ROLE" = miner ] && [ -f "$MINER_KIT/.env" ]; then
  _nj="$(sed -n 's/^DENDRA_NO_JUDGE=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  if [ "$JUDGE" != 1 ] && [ "$NO_JUDGE" != 1 ] && [ "$_nj" = 1 ]; then
    NO_JUDGE=1; say "[join] the judge seat was declined on this kit (DENDRA_NO_JUDGE=1 in $MINER_KIT/.env): kept. --judge asks for it again."
  fi
  if [ -z "$AUTO_UPDATE" ]; then
    _au="$(sed -n 's/^DENDRA_AUTO_UPDATE=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
    case "$_au" in 0|1) AUTO_UPDATE="$_au" ;; "") : ;; *) warn "DENDRA_AUTO_UPDATE=$_au in $MINER_KIT/.env is neither 0 nor 1: ignored (the update stays on)." ;; esac
  fi
  if [ -z "$PAYOUT_LOCK" ]; then
    _pl="$(sed -n 's/^DENDRA_PAYOUT_LOCK=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
    case "$_pl" in 0|1) PAYOUT_LOCK="$_pl" ;; "") : ;; *) warn "DENDRA_PAYOUT_LOCK=$_pl in $MINER_KIT/.env is neither 0 nor 1: ignored (no lock is asked for)." ;; esac
  fi
fi
[ -n "$AUTO_UPDATE" ] || AUTO_UPDATE=1
[ -n "$PAYOUT_LOCK" ] || PAYOUT_LOCK=0

# Canonical defaults. DENDRA_EMBED_MODE=backend is fixed for the network (the 'hash' bag-of-words
# measures words, not meaning, so a mixed committee would misverify). Judge model is RAM-based (see below).
# The operator's explicit choice, captured before any default is applied.
# Without this capture, `DENDRA_MODEL_ID=... bash join.sh` would be set here and silently overwritten
# further down: an operator who asks for a precise model (because the on-chain registry requires it, or
# to stay homogeneous with the rest of the network) would get something else and never find out. A
# script may propose a default; it must not silently cancel an instruction.
DENDRA_MODEL_ID_EXPLICIT="${DENDRA_MODEL_ID:-}"
export DENDRA_MODEL_ID="${DENDRA_MODEL_ID:-llama3.1:8b-instruct-q4_K_M}"
export DENDRA_EMBED_MODE=backend
export DENDRA_EMBED_API_MODEL="${DENDRA_EMBED_API_MODEL:-nomic-embed-text}"
# Stable identity across runs.
# A `$RANDOM` suffix would mint a new miner on every run: new key, new address, previous stake lost,
# and the on-chain registration to redo (from an empty account, hence impossible). The `miner-keys`
# volume is not enough to preserve the identity: MINER_ID is what designates it, so a re-run that drew a
# new one would lose everything. The identity already written in the kit's .env is reused;
# `--id` still takes precedence.
# Carry-over is only valid while the chain accepts chosen identifiers. From consensus epoch 3 the
# chain derives the miner id from the signing address and refuses anything else, so an id inherited
# from a previous genesis is rejected at registration, and the daemon would keep retrying against a
# rule it cannot satisfy, which reads as "the network is broken" rather than "this id is stale".
if [ -z "$MINER_ID" ] && [ -f "$MINER_KIT/.env" ]; then
  MINER_ID="$(grep -E '^MINER_ID=' "$MINER_KIT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | tr -d '[:space:]')"
  case "$MINER_ID" in
    dm1*) [ -n "$MINER_ID" ] && say "[join] miner identity reused from .env: $MINER_ID (stake preserved)" ;;
    "")   : ;;
    # This branch does not `exit 2`: that would lock every newcomer out on their second run, on an
    # identifier this script wrote on the first (it derives `m-<hash>` below and puts it in the
    # .env, while the chain accepts only an id derived from the address).
    # The daemon resolves and persists the accepted identity in its own volume
    # (`miner.py`, `resolved-identity`), so a stale name here is not fatal: the next start
    # resumes the identity that actually holds the stake instead of minting a new one. What is left is
    # worth a note, not a refusal.
    *)    say "  [i] '$MINER_ID' in $MINER_KIT/.env is not a derived identifier (dm1...)."
          say "      The chain derives the miner id from your address and refuses any other value, so"
          say "      this name is not what will be registered. The daemon resolves the right one at"
          say "      startup and remembers it, so your registration and stake survive restarts."
          say "      Nothing to do. If you want the file to match what runs, read the dm1... value"
          say "      from the miner's first log line and put it here."
          say "      Note: an identity from an earlier genesis carries no balance — a fresh genesis"
          say "      starts every account at zero, whatever the identifier says." ;;
  esac
fi
# Identity default: never the hostname. Whatever lands here is written into the on-chain miner
# registry, world-readable and permanent. A registry entry cannot be edited or withdrawn, so a
# hostname reaching this variable is a leak that no later fix repairs.
# The pseudonym is derived from the machine id, like `_PSEUDO` in deploy/hw_probe.sh: stable across
# restarts (so the stake and the track record survive), reproducible by its owner alone (run the same
# sha256 locally to find yourself in a list), and meaningless to anyone else.
# Naming yourself stays possible with --id, as a deliberate act, never something a script inherits
# from a forgotten $(hostname).
# The braces matter. `a || b || c | sha256sum` parses as `a || b || (c | sha256sum)`: the pipeline
# binds to the last alternative only, so a successful fallback would emit its value unhashed.
PSEUDO_ID="m-$( { cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo machine; } | tr -d '\n' | head -c 64 | sha256sum | cut -c1-10)"
[ -n "$MINER_ID" ] || MINER_ID="$PSEUDO_ID"
# Anti-deanonymization guard: same rule, same floor and same remedy as the one in deploy/hw_probe.sh
# (its `_HOSTNAME` 4-character floor and the inclusion test that follows it).
# The test is inclusion, not equality, because callers build identifiers by concatenation: a name that
# merely contains the hostname leaks it just as completely as one that equals it.
# The 4-character floor is part of the rule. Hostnames like `pc`, `vm` or `dev` are substrings of
# ordinary text and identify nobody, so without the floor the guard fires on identifiers that leak
# nothing, including the pseudonym it hands out.
# The identifier is replaced, not refused. Refusing would cost the operator their whole join over a
# name the script can simply fix, with the same privacy outcome. Publishing the machine name stays
# possible, as a deliberate act.
_HOSTNAME="$(hostname 2>/dev/null || echo '')"
[ "${#_HOSTNAME}" -ge 4 ] || _HOSTNAME='__too_short_to_identify__'
case "$MINER_ID" in
  *"$_HOSTNAME"*)
    if [ "${DENDRA_ACCEPT_PUBLIC_NAME:-0}" = "1" ]; then
      say "  [!] the identity '$MINER_ID' contains this machine's hostname, kept because"
      say "      DENDRA_ACCEPT_PUBLIC_NAME=1. It goes into the on-chain miner registry: world-readable,"
      say "      and never editable nor withdrawable."
    else
      say "  [!] the identity '$MINER_ID' contains this machine's hostname. It would be written on-chain,"
      say "      publicly and permanently -> replaced by the pseudonym '$PSEUDO_ID'."
      say "      To publish the machine name instead, set DENDRA_ACCEPT_PUBLIC_NAME=1: a choice, never"
      say "      something a script inherits from a forgotten hostname."
      MINER_ID="$PSEUDO_ID"
    fi ;;
esac

# Judge model. The probe seats a judge only on the CPU, with the MoE qwen3:30b-a3b, from MOE_CPU_MIN_RAM_MB
# of system RAM, with or without a GPU (deploy/hw_probe.sh says why no judge sits on the GPU). Never qwen3:4b (removed:
# false-slashes in a distributed setting). Every judge the kit seats runs the model the chain names
# (modelregistry audit_judge_model) unless DENDRA_JUDGE_MODEL overrides it, so this kit brings no judge-model
# diversity of its own (that diversity is not decided yet). The on-chain verdict carries the model
# (DENDRA_JUDGE_MODEL_ID) so judge-model diversity stays measurable.
# The single source of truth for both the served model and the judge eligibility is deploy/hw_probe.sh:
# it measures the real VRAM/RAM, sizes the model to the card, and gates the judge seat behind an
# allow-list of validated judge models. A second, RAM-only heuristic here would diverge from
# it: it could seat a judge on a 4 GB card (RAM says yes, VRAM says no) and hand the miner a model bigger
# than its card. One probe, one answer.
HW_PROBE="$REPO/deploy/hw_probe.sh"
# The judge gate has three answers and is read as such. `--can-judge` prints true or false (a reading,
# exit 0) or unknown (exit 3: the RAM was not read, nothing was decided). It is asked directly rather than
# picked out of the JSON with a pattern: a gate is never a text match, and an unread probe is never `false`.
HW_JSON=""; HW_CAN_JUDGE="unknown"; HW_MODEL=""; HW_JUDGE_MODEL=""; HW_TIER=""
# --plan asks the engine nothing: the probe's one docker call (where Docker keeps the weights) is skipped and
# that disk figure reads unread -- the plan prints no disk figure.
_HW_ND=""; [ "$PLAN_ONLY" = 1 ] && _HW_ND="--no-docker"
if [ -r "$HW_PROBE" ]; then
  HW_JSON="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --json $_HW_ND 2>/dev/null || true)"
  HW_CAN_JUDGE="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --can-judge $_HW_ND 2>/dev/null | tail -1)"
  case "$HW_CAN_JUDGE" in true|false) : ;; *) HW_CAN_JUDGE="unknown" ;; esac
  HW_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"model":"[^"]*"' | cut -d'"' -f4)"
  HW_JUDGE_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"judge_model":"[^"]*"' | cut -d'"' -f4)"
  HW_TIER="$(printf '%s' "$HW_JSON" | grep -o '"tier":[0-9]*' | cut -d: -f2)"
  # The probe decides only when the operator imposed nothing, and when it decides, it says so: on a
  # large card it returns a top tier (qwen3:30b-a3b, ~19 GB) instead of the 8B announced at the top of
  # this file, which changes both what is downloaded and the model served to the network. A choice of
  # that reach must not be discovered by reading a generated .env.
  if [ -n "${DENDRA_MODEL_ID_EXPLICIT:-}" ]; then
    export DENDRA_MODEL_ID="$DENDRA_MODEL_ID_EXPLICIT"
    [ -n "$HW_MODEL" ] && [ "$HW_MODEL" != "$DENDRA_MODEL_ID" ] && \
      say "  [i] model imposed by you: $DENDRA_MODEL_ID (the probe would have picked $HW_MODEL for this hardware)"
  elif [ -n "$HW_MODEL" ]; then
    export DENDRA_MODEL_ID="$HW_MODEL"
    say "  [i] model chosen by the probe: $HW_MODEL (tier ${HW_TIER:-?}, sized from the GPU's memory; without a usable GPU, the judge model)."
    say "      To impose your own:  DENDRA_MODEL_ID=<model> bash deploy/join.sh ..."
  fi
fi
# The judge by default, decided on the probe's three answers and nothing else. `true` seats
# the judge unless the operator declined it (--no-judge, or the refusal recorded in the kit's .env); `false`
# seats nothing; `unknown` seats nothing and says why, since an unread gate is not a pass. The seat decided
# here is the default one (JUDGE_DEFAULTED=1): check_prereqs holds it to the disk floor before anything is
# pulled, and run_miner says what it downloads. A machine without a usable card is decided by engine_decide,
# which implies the seat or refuses the machine; --no-judge is refused there (run_miner).
if [ "$ROLE" = miner ] && [ "$JUDGE" != 1 ] && [ "$NO_JUDGE" != 1 ]; then
  case "$HW_CAN_JUDGE" in
    true) JUDGE=1; JUDGE_DEFAULTED=1
          say "  [i] judge: on by default, this machine can judge (deploy/hw_probe.sh --can-judge: true). --no-judge declines the seat." ;;
    false) say "  [i] judge: none, this machine cannot judge (deploy/hw_probe.sh --can-judge: false; the judge needs MOE_CPU_MIN_RAM_MB of system RAM)." ;;
    *) say "  [i] judge: none, whether this machine can judge is unknown (deploy/hw_probe.sh --can-judge gave no reading: its RAM was not read). An unread gate seats no judge; run bash deploy/hw_probe.sh to see what it reads." ;;
  esac
elif [ "$ROLE" = miner ] && [ "$NO_JUDGE" = 1 ]; then
  say "  [i] judge: declined (--no-judge). The chain still draws this miner into juries, where it never votes; the refusal is recorded in the kit's .env (DENDRA_NO_JUDGE=1), and --judge lifts it."
fi

# The role of this machine on the testnet, decided by the probe and nowhere else (deploy/hw_probe.sh --role):
# miner (a usable card) | judge (no usable card, at least MOE_CPU_MIN_RAM_MB of RAM) | refused (below it) |
# unknown (RAM unread, or no reading at all). Line 1 of the probe's answer is the word, the rest says why.
# hw_role [--no-gpu] -> HW_ROLE, HW_ROLE_WHY. A word that is none of the three readings, or a code that does not
# go with it, is unknown: a gate is never read from a text that merely looks like an answer.
HW_ROLE="unknown"; HW_ROLE_WHY=""
hw_role(){
  local out rc w
  HW_ROLE=unknown; HW_ROLE_WHY="the hardware probe ($HW_PROBE) is not readable in this clone, so nothing decided this machine's role"
  [ -r "$HW_PROBE" ] || return 0
  out="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --role "$@" 2>/dev/null)"; rc=$?
  w="$(printf '%s\n' "$out" | head -1 | tr -d '[:space:]')"
  HW_ROLE_WHY="$(printf '%s\n' "$out" | tail -n +2)"
  case "$rc:$w" in
    0:miner|0:judge|0:refused) HW_ROLE="$w" ;;
    3:unknown) HW_ROLE=unknown ;;
    *) HW_ROLE=unknown; HW_ROLE_WHY="deploy/hw_probe.sh --role gave no reading (exit $rc, answer '${w:-<empty>}'): nothing decided this machine's role" ;;
  esac
  return 0
}
hw_role

# DENDRA_JUDGE_MODEL is a judge model or nothing. Taken as given, on a machine without a usable GPU it would
# become the model served on the CPU instance (engine_decide compares only DENDRA_MODEL_ID), so
# DENDRA_JUDGE_MODEL=llama3.2:1b would put a mining model back on the CPU under the name "no mining model"; and on
# every machine it is written at rank 1 of the judge's precedence, above the model the chain pins: an unvalidated
# judge votes against correct answers worded differently and costs honest miners their stake. So the value is
# asked of the probe's allow-list (deploy/hw_probe.sh --judge-allowed: the hard list, which the environment may
# only narrow), before anything is written. An allow-list that cannot be read seats nothing.
if [ "$ROLE" = miner ] && [ -n "${DENDRA_JUDGE_MODEL:-}" ]; then
  _ja=""
  [ -r "$HW_PROBE" ] && _ja="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --judge-allowed "$DENDRA_JUDGE_MODEL" 2>/dev/null | head -1)"
  case "$_ja" in
    true) say "  [i] DENDRA_JUDGE_MODEL=$DENDRA_JUDGE_MODEL is on the judge allow-list of deploy/hw_probe.sh." ;;
    false)
      printf '[join] refused: DENDRA_JUDGE_MODEL=%s is not on the judge allow-list of deploy/hw_probe.sh (bash deploy/hw_probe.sh --judge-allowed <model> answers it). It would sit above the model the chain pins and, on a machine without a usable GPU, be the model served on the CPU. Unset it: the judge then runs the model the chain pins. Nothing was changed.\n' "$DENDRA_JUDGE_MODEL" >&2
      exit 2 ;;
    *)
      printf '[join] refused: whether DENDRA_JUDGE_MODEL=%s is a judge model could not be read (%s --judge-allowed gave no reading): an unread allow-list seats nothing. Nothing was changed.\n' "$DENDRA_JUDGE_MODEL" "$HW_PROBE" >&2
      exit 2 ;;
  esac
fi

_pick_judge_model(){
  [ -n "${DENDRA_JUDGE_MODEL:-}" ] && { echo "$DENDRA_JUDGE_MODEL"; return; }
  [ -n "$HW_JUDGE_MODEL" ] && { echo "$HW_JUDGE_MODEL"; return; }
  # The judge model the network names on chain (modelregistry audit_judge_model), which the probe seats on
  # the CPU. A different fallback would be downloaded and never used (the worker runs the on-chain model
  # first), leaving a mute seat.
  echo "qwen3:30b-a3b-instruct-2507-q4_K_M"
}

# Judge-seat gate: an under-powered or unvalidated judge votes against answers that are correct but
# worded differently, and an unfair verdict costs an honest miner its stake. A node may mine on any
# hardware; it may only judge when the probe says so.
_assert_can_judge(){
  [ "$HW_CAN_JUDGE" = "true" ] && return 0
  if [ "$HW_CAN_JUDGE" != "false" ]; then
    warn "whether this machine can judge is unknown: deploy/hw_probe.sh could not read its system RAM, so the judge"
    warn "gate (MOE_CPU_MIN_RAM_MB) decided nothing. An unread gate is not an open one: the role is not granted."
    warn "It can mine normally. Run 'bash deploy/hw_probe.sh' to see what it reads."
    return 1
  fi
  warn "this machine is not eligible to judge: the judge runs on the CPU and needs MOE_CPU_MIN_RAM_MB of system RAM."
  warn "It can mine normally. Seating an under-powered judge penalises honest miners, so the role is refused."
  warn "Run 'bash deploy/hw_probe.sh' to see the role it grants and the RAM it reads."
  return 1
}

# ---------------------------------------------------------------- the engine: on the card, or the judge on the CPU
# No mining model on the CPU (the testnet's rule). A miner whose card Docker can use mines on it, as before
# (ENGINE=gpu). Otherwise the probe decides, as for a machine without a card: judge -> ENGINE=cpu-judge (the judge
# model on the kit's CPU instance, which also answers the requests the chain assigns this miner; no mining model
# is pulled); refused or unknown -> refused here, before anything is written, exit 2, with the probe's reason.
# A card the host lists and Docker cannot reach is decided again with --no-gpu: the probe saw it, the engine
# cannot use it, and the host's reading alone would make it a miner that infers on the CPU.
ENGINE=""
# ollama_image -> the inference engine image the kit pins: the one `x-ollama-image` line of the miner kit's compose
# file, which every service running the engine points at. Empty when that line cannot be read: the GPU checks then
# say they measured nothing, rather than pull an image the miner would not run.
ollama_image(){
  awk -F'"' '$1 == "x-ollama-image: &ollama-image " && $2 ~ /^ollama.ollama:/ && NF == 3 { print $2; exit }' \
    "$MINER_KIT/docker-compose.yml" 2>/dev/null
}
# A refusal changes nothing, so it stops nothing either. A kit already installed on this machine keeps running with
# the image and the .env it was started with, and the start paths of the kit (the application's Start button,
# miner_health.sh's remedies, a HiveOS resume) start it again as it is. This is reported, with the one command that
# stops it, after the procedure that keeps its stake: a registered miner stopped before it has filed the reveals its own
# audited jobs wait for can be slashed, and a stopped miner is exposed to the availability slash where the chain
# arms it (deploy/testnet-miner/exit-miner.sh --help gives that procedure, and exit-miner.sh reads what holds it).
engine_kit_note(){
  [ -f "$MINER_KIT/.env" ] || return 0
  printf '       A miner kit is already installed here (%s/.env), and this refusal does not stop it. The testnet runs\n' "$MINER_KIT" >&2
  printf '       no mining model on the CPU: if it serves one, first read  bash deploy/testnet-miner/exit-miner.sh --help\n' >&2
  printf '       (a registered miner stopped too early can be slashed), then stop it with  docker compose -p dendra-miner stop\n' >&2
  return 0
}
engine_decide(){
  if [ "$GPU_OK" = 1 ] && [ "$TOOLKIT_OK" = 1 ]; then ENGINE=gpu; return 0; fi
  if [ "$HW_ROLE" = miner ] || [ "$GPU_OK" = 1 ]; then hw_role --no-gpu; fi
  case "$HW_ROLE" in
    judge)
      ENGINE=cpu-judge
      # The served model is the judge model, decided here so that everything after (the override, the .env,
      # the banner) names the same one. A mining model asked for by name is refused, never served on the CPU.
      local m; m="$(_pick_judge_model)"
      if [ -n "${DENDRA_MODEL_ID_EXPLICIT:-}" ] && [ "$DENDRA_MODEL_ID_EXPLICIT" != "$m" ]; then
        printf '[join] refused: DENDRA_MODEL_ID=%s asks for a model to mine with, and this machine has no usable GPU: on the CPU it serves the judge model (%s) only. Unset DENDRA_MODEL_ID. Nothing was changed.\n' "$DENDRA_MODEL_ID_EXPLICIT" "$m" >&2
        exit 2
      fi
      export DENDRA_MODEL_ID="$m"
      say "  [i] Role: judge on the CPU (deploy/hw_probe.sh --role: judge). Served model: $m, on the CPU instance."
      printf '%s\n' "$HW_ROLE_WHY" | sed 's/^/      /'
      return 0 ;;
    refused)
      printf '[join] refused: this machine has no role on the testnet (deploy/hw_probe.sh --role: refused).\n' >&2
      printf '%s\n' "$HW_ROLE_WHY" | sed 's/^/       /' >&2
      printf '       Nothing was changed.\n' >&2
      engine_kit_note
      exit 2 ;;
    *)
      printf '[join] refused: whether this machine can join is unknown (deploy/hw_probe.sh --role: unknown).\n' >&2
      printf '%s\n' "${HW_ROLE_WHY:-the probe gave no reason}" | sed 's/^/       /' >&2
      printf '       Run bash %s to see what it reads. Nothing was changed.\n' "$HW_PROBE" >&2
      engine_kit_note
      exit 2 ;;
  esac
}

# ---------------------------------------------------------------- (a) pre-flight (fail-fast + remediation)
GPU_OK=0; TOOLKIT_OK=0
check_prereqs(){
  say "== [join] pre-flight =="
  # Docker + compose v2
  if ! docker compose version >/dev/null 2>&1; then
    say "  [FAIL] Docker/Compose v2 missing -> https://docs.docker.com/engine/install/"
    # No Docker-free fallback is shipped, and none is named: a file outside the published repository
    # would send the reader to a dead end. Docker Compose v2 is a hard prerequisite
    # here, and the message says so.
    say "       Docker Compose v2 is a requirement of this kit: there is no Docker-free path in this repository."
    exit 1
  fi
  say "  [OK] docker compose"
  # Minimal config per role
  if [ "$ROLE" = miner ]; then
    { [ -n "${DENDRA_NODE:-}" ] && [ -n "${DENDRA_RELAY:-}" ] && [ -n "${FAUCET:-}" ]; } \
      || { [ -f "$MINER_KIT/.env" ]; } \
      || die "missing config: provide CONFIG_URL=<network-info.txt> (recommended) or fill $MINER_KIT/.env"
  else
    { [ -n "${GENESIS_URL:-}" ]; } || { [ -f "$NODE_KIT/.env" ]; } \
      || die "missing config: provide CONFIG_URL=<network-info.txt> (GENESIS_URL/SHA256/SEEDS) or fill $NODE_KIT/.env"
  fi
  # NVIDIA GPU: a card Docker can use mines on it; without one, engine_decide applies the testnet's CPU rule
  # (a judge on the CPU, or refused: no mining model runs on the CPU). The whole block concerns miners only.
  # Inference is what needs a card, and only the miner serves a model: a validator syncs, signs blocks
  # and contributes to the seed, none of which touches a GPU. So on --validator there is no missing
  # hardware to report, and no role to decide.
  if [ "$ROLE" = miner ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      GPU_OK=1
      say "  [OK] GPU: $(nvidia-smi -L | head -1)"
      local vram
      vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
      if [ -n "$vram" ] && [ "$vram" -lt 6000 ] 2>/dev/null; then
        warn "VRAM ~${vram} MB: the probe sizes a small model to this card; >=8 GB serves larger ones."
      fi
      # `--entrypoint` is required here: the ollama/ollama image sets ENTRYPOINT=/bin/ollama, so
      # `docker run ollama/ollama nvidia-smi` runs `ollama nvidia-smi`, an unknown command that fails
      # whatever the state of the toolkit. Without it the test could never succeed: the GPU override would
      # never be written and every miner using this kit would infer on the CPU while `nvidia-smi` answers
      # on the host.
      # The image is the engine the kit pins (ollama_image): the check pulls the image the miner will run,
      # never a `latest` it would not.
      local oimg; oimg="$(ollama_image)"
      if [ -z "$oimg" ]; then
        warn "the engine image the kit pins could not be read ($MINER_KIT/docker-compose.yml, its x-ollama-image line):"
        warn "  whether Docker sees the GPU was not measured. The clone is incomplete: update it, then run this again."
      elif docker run --rm --gpus all --entrypoint nvidia-smi "$oimg" -L >/dev/null 2>&1; then
        TOOLKIT_OK=1; say "  [OK] nvidia-container-toolkit (GPU visible to Docker)"
      else
        warn "GPU seen by the host but not by Docker -> install nvidia-container-toolkit to mine on it."
        warn "  check by hand:  docker run --rm --gpus all --entrypoint nvidia-smi $oimg -L"
      fi
    else
      say "  [i] no NVIDIA GPU usable here (nvidia-smi absent or failing)."
    fi
    engine_decide
  else
    say "  [i] GPU: none required for this role. A validator serves no model — it syncs, signs blocks"
    say "      and feeds the committee seed, and none of that runs inference."
  fi
  # Disk
  local freeg droot jfree jfloor jpath
  freeg=$(df -BG --output=avail "$REPO" 2>/dev/null | tail -1 | tr -dc '0-9')
  [ -n "$freeg" ] && [ "$freeg" -lt 10 ] 2>/dev/null && warn "free disk ${freeg}G under the clone: the served model, the images and the chain state -> plan for >=10-15 GB."
  # The judge model is the large pull (its size is stated next to its tag in deploy/testnet-miner/docker-compose.yml,
  # service judge-model-init), and it lands under Docker's data root, where the node's volume lives too, not
  # under this clone. A judge (the role on the CPU, or --judge on a machine that can judge) reads that disk against
  # the judge floor of deploy/install.sh: DENDRA_MIN_DISK_GB_JUDGE, with the same default (one figure, two readers;
  # the installer bench of the development repository checks they are equal). It warns and does not refuse: on a re-run of a judge whose
  # model is already pulled, that model is counted as used space. Under WSL the data root is a sparse virtual disk
  # that reports its own maximum, so the Windows drive is read, as deploy/install.sh does. A size it cannot read is reported.
  if [ "$ROLE" = miner ] && { [ "${ENGINE:-}" = cpu-judge ] || { [ "$JUDGE" = 1 ] && [ "$HW_CAN_JUDGE" = true ]; }; }; then
    jfloor="${DENDRA_MIN_DISK_GB_JUDGE:-32}"
    if grep -qi microsoft /proc/version 2>/dev/null; then
      jpath="${DENDRA_WSL_HOST_MOUNT:-/mnt/c}"
    else
      droot="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"; jpath="$droot"
    fi
    jfree=""; [ -n "$jpath" ] && jfree="$(df -BG --output=avail "$jpath" 2>/dev/null | tail -1 | tr -dc '0-9')"
    # A seat the default gives is held to the floor before anything is pulled: below it, or
    # unread, it seats nothing and says so, and the miner runs without it. Only a first seating is gated: a kit
    # whose .env already says DENDRA_MINER_JUDGE=1 has pulled its model, which the free-space figure counts as
    # used, so demoting it here would stop a judge that works. An explicit --judge, and the role on the CPU, are
    # warned as before.
    local jfirst=1
    [ "$(sed -n 's/^DENDRA_MINER_JUDGE=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')" = 1 ] && jfirst=0
    if [ -z "$jfree" ]; then
      if [ "$JUDGE_DEFAULTED" = 1 ] && [ "$jfirst" = 1 ]; then
        JUDGE=0; JUDGE_DEFAULTED=0
        say "  [i] judge: none. Free disk where the judge model lands was not measured (${jpath:-docker info named no data root}), and the seat the default gives is not taken on an unread floor (DENDRA_MIN_DISK_GB_JUDGE, ${jfloor} GB). The miner runs; bash deploy/testnet-miner/judge.sh on seats it once the disk is read."
      else
        warn "free disk where the judge model lands not measured (${jpath:-docker info named no data root}): it is the large pull of this kit."
      fi
    elif [ "$jfree" -lt "$jfloor" ] 2>/dev/null; then
      if [ "$JUDGE_DEFAULTED" = 1 ] && [ "$jfirst" = 1 ]; then
        JUDGE=0; JUDGE_DEFAULTED=0
        say "  [i] judge: none. Free disk ${jfree}G under $jpath is below the judge floor of ${jfloor} GB (DENDRA_MIN_DISK_GB_JUDGE, deploy/install.sh), so the seat the default gives is not taken: the judge model ($(_pick_judge_model), $(judge_model_size_note)) would land there. The miner runs; free space, then bash deploy/testnet-miner/judge.sh on."
      else
        warn "free disk ${jfree}G under $jpath is below the judge floor of ${jfloor} GB (DENDRA_MIN_DISK_GB_JUDGE, deploy/install.sh): the judge model is pulled there, next to the node's volume, unless it is there already. Free space first."
      fi
    elif [ "$JUDGE_DEFAULTED" = 1 ] && [ "$jfirst" = 1 ]; then
      say "  [i] judge: the seat pulls the judge model $(_pick_judge_model) ($(judge_model_size_note)) into the kit's CPU instance; free disk ${jfree}G under $jpath clears the floor of ${jfloor} GB."
    fi
  fi
  # Public RPC reachable (miner: required)
  if [ -n "${DENDRA_NODE:-}" ]; then
    local rpc_http="${DENDRA_NODE/tcp:\/\//http://}"
    if curl -fsS -m 8 "$rpc_http/status" >/dev/null 2>&1; then
      say "  [OK] public RPC reachable ($DENDRA_NODE)"
    else
      [ "$ROLE" = miner ] && die "public RPC unreachable ($DENDRA_NODE): operator down, wrong IP, or firewall. Check network-info.txt."
      warn "RPC $DENDRA_NODE unreachable (the node will use SEEDS to sync)."
    fi
  fi
}

# ---------------------------------------------------------------- (b) genesis SHA — anti-MITM, fail-closed
#
# This function fails closed, as the genesis check of `docker/node-join.sh` does, with one named opt-out
# for a trusted network. A fail-open guard such as
#     [ -n "$GENESIS_URL" ] && [ -n "$GENESIS_SHA256" ] || return 0
# would return success on an absent hash without printing anything, and skip the body on a failed
# download just as quietly. GENESIS_SHA256 comes from the same network-info.txt an attacker would serve,
# so omitting one line would disarm the whole check. The documentation says it fails closed, and this
# function is what keeps that true.
#
# Scope: a miner that runs its own node is protected twice (node-join.sh enforces this again inside the
# container). A miner that only runs the inference kit never reaches node-join.sh; for that operator
# this is the only check that the network they are about to serve is the one they think it is.
# ---------------------------------------------------------------- consensus epoch (fork, upstream)
# The genesis hash proves which history, not which state machine. This kit builds the node from the
# tree you cloned, so a tree one consensus epoch away from the running network produces a binary that
# takes different state transitions on the same blocks. The failure is not immediate and does not look
# like a version problem: the node syncs normally until the first transaction that exercises the
# change, then stops on an AppHash mismatch that reads like a slow disk or a bad peer.
# The chain cannot answer this on its own (`abci_info` carries no app_version here), so the network
# declares its epoch in the same config this script already fetches, and the two are compared.
# Three answers, never two: equal -> fine; different -> refuse; absent -> say what is not checked.
# Absent is the config of an operator who has not republished yet: refusing there would lock out every
# joiner on a network that is probably fine, so it warns and names the risk.
verify_consensus_epoch(){
  local mine theirs f="$REPO/docker/CONSENSUS_EPOCH"
  mine="$(head -1 "$f" 2>/dev/null | tr -dc 0-9)"
  theirs="$(printf '%s' "${CONSENSUS_EPOCH:-}" | tr -dc 0-9)"
  if [ -z "$mine" ]; then
    warn "docker/CONSENSUS_EPOCH unreadable in this tree -> cannot tell whether your build matches the network."
    return 0
  fi
  if [ -z "$theirs" ]; then
    warn "this network does not declare a consensus epoch, so whether your tree matches it is not checked."
    say  "       Your tree is at epoch $mine. If the operator is on a different one, your node will sync"
    say  "       normally and then stop on an AppHash mismatch at the first transaction that exercises"
    say  "       the difference — which looks like a network problem and is not one."
    say  "       Ask the operator to add CONSENSUS_EPOCH= to the file named by CONFIG_URL."
    return 0
  fi
  if [ "$mine" = "$theirs" ]; then
    # Matching the declaration is not matching the chain. The number in the config file is written by
    # the operator's publisher; unless it also says where it read it, an agreement here only proves
    # that two trees carry the same digit. `CONSENSUS_EPOCH_PROVEN=1` means the publisher took it from
    # the marker written after the chain was actually brought up; anything else means it took it from
    # the operator's tree, which can sit ahead of what is deployed.
    if [ "${CONSENSUS_EPOCH_PROVEN:-}" = "1" ]; then
      say "  [OK] consensus epoch $mine — your tree takes the same state transitions as this network,"
      say "       and the network declares this epoch from the chain it actually deployed."
    else
      say "  [OK] consensus epoch $mine — your tree matches what this network declares."
      warn "the network does not certify where that number came from: it may be the operator's tree"
      say  "       rather than the chain that is running. If they differ, your node syncs and then halts"
      say  "       on an AppHash mismatch. Ask the operator to publish with CONSENSUS_EPOCH_PROVEN=1."
    fi
    return 0
  fi
  # After a governed upgrade the network's file declares the old epoch until its operator republishes it.
  if [ -n "${AFTER_UPGRADE:-}" ] && after_upgrade_applied "$mine" "$theirs"; then return 0; fi
  say "  [FAIL] consensus epoch mismatch: your tree is at $mine, this network runs $theirs."
  say "       A node built here would agree on the genesis and disagree on the rules. It will sync"
  say "       until the first transaction that exercises the difference, then halt on an AppHash"
  say "       mismatch — a failure that looks like a slow peer and is not."
  say "       Ways out: check out the tree at the network's epoch, or wait for the operator to deploy"
  say "       yours. DENDRA_ALLOW_EPOCH_MISMATCH=1 forces it for a network you are testing against."
  if [ "${DENDRA_ALLOW_EPOCH_MISMATCH:-0}" = "1" ]; then
    warn "DENDRA_ALLOW_EPOCH_MISMATCH=1 -> joining anyway with a known state-machine difference."
    return 0
  fi
  die "consensus epoch mismatch ($mine vs $theirs) -> refusing to build a node that would fork."
}

# after_upgrade_applied <this tree's epoch> <the epoch network-info declares> -> 0 when the mismatch is the one a
# governed upgrade leaves behind (--after-upgrade <plan>): this tree is ahead of the declared epoch, this kit runs its
# own node, and that node answers `dendrad query upgrade applied <plan>` with a height. A node answers a height only
# once it runs a binary that applied the plan, which the network does after the plan's height, so the order is: the
# node is started on the new tree's image first (deploy/testnet-miner/node_upgrade_watch.sh writes DENDRA_NODE_IMAGE
# in the node kit's .env and runs docker compose up -d --no-build before it runs this join), then asked here, through
# the node kit (docker compose exec), every 10 s up to DENDRA_AFTER_UPGRADE_WAIT_S (default 300). A kit without a
# node of its own (--remote-rpc) asks the chain RPC it reads instead (DENDRA_NODE, else the miner kit's .env), by
# applied_height_via_rpc: deploy/testnet-miner/kit_update.sh::remote_upgrade runs this join once that RPC answers. It
# proves that the network applied that plan; that this tree carries the plan's binary is what the watch checks before
# it moves the clone. 1 otherwise, the reason said, and the refusal of verify_consensus_epoch stands.
after_upgrade_applied(){
  local mine="$1" theirs="$2" plan="$AFTER_UPGRADE" bound="${DENDRA_AFTER_UPGRADE_WAIT_S:-300}" waited=0 h="?" who="this kit's node"
  case "$bound" in ""|*[!0-9]*) bound=300 ;; esac
  if ! [ "$mine" -gt "$theirs" ] 2>/dev/null; then
    say "  [i] --after-upgrade $plan: this tree (epoch $mine) is not ahead of the network ($theirs), so no upgrade explains the difference."
    return 1
  fi
  if [ "${OWN_NODE:-1}" != 1 ]; then
    AFTER_UPGRADE_RPC="${DENDRA_NODE:-$(_env_file_get "$MINER_KIT/.env" DENDRA_NODE)}"
    if [ -z "$AFTER_UPGRADE_RPC" ]; then
      say "  [i] --after-upgrade $plan: this kit runs no node of its own and names no chain RPC (DENDRA_NODE), so nothing answers whether the plan was applied."
      return 1
    fi
    who="the chain RPC this kit reads ($AFTER_UPGRADE_RPC)"
  elif [ ! -r "$NODE_KIT/docker-compose.yml" ]; then
    say "  [i] --after-upgrade $plan: this kit has no node kit ($NODE_KIT), so no node answers whether the plan was applied."
    return 1
  fi
  while :; do
    h="$(after_upgrade_height "$plan")"
    case "$h" in ''|*[!0-9]*|0) : ;; *) break ;; esac
    # A node of this kit still on the binary before the upgrade stands at the plan's halt and answers no height. Once
    # the chain confirms that halt, this run starts it on this tree's node image and asks it again then
    # (after_upgrade_finish): the gesture of a kit whose watch is off, a HiveOS rig among them.
    if [ "$waited" = 0 ] && [ "${OWN_NODE:-1}" = 1 ] && node_halt_confirmed "$plan"; then
      AFTER_UPGRADE_SWITCH=1
      say "  [i] --after-upgrade $plan: this kit's node answers no height for that plan yet, and $AFTER_UPGRADE_HALT_WHY."
      say "       It is started on this tree's node image, then asked again before the join goes on (after_upgrade_finish)."
      return 0
    fi
    [ "$waited" -ge "$bound" ] && break
    sleep 10; waited=$((waited + 10))
  done
  case "$h" in
    0) say "  [i] --after-upgrade $plan: $who answers no height for that plan (the field is absent: proto3 omits a zero, the plan is not applied on the chain it follows)."
       return 1 ;;
    ''|*[!0-9]*) say "  [i] --after-upgrade $plan: whether that plan was applied was not read ($who did not answer, or the tools that read the answer are not on this host)."
       return 1 ;;
  esac
  say "  [OK] consensus epoch $mine: this network still declares $theirs, and $who answers plan $plan applied at height $h."
  say "       The network passed that governed upgrade; its operator republishes network-info.txt afterwards."
  return 0
}
# after_upgrade_height <plan> -> the height this kit's node answers for the plan, read by upgrade_height_of_json, or,
# for a kit without a node of its own, the height the chain RPC it reads answers (applied_height_via_rpc); ? when
# nothing answered.
after_upgrade_height(){
  local raw
  if [ "${OWN_NODE:-1}" != 1 ]; then applied_height_via_rpc "${AFTER_UPGRADE_RPC:-}" "$1"; return 0; fi
  raw="$( cd "$NODE_KIT" 2>/dev/null && docker compose exec -T node dendrad query upgrade applied "$1" -o json 2>/dev/null )" || { printf '?'; return 0; }
  upgrade_height_of_json "$raw"
}
# node_halt_confirmed <plan> -> 0 when the chain confirms that this kit's node stands at the halt of that plan, the
# reason in AFTER_UPGRADE_HALT_WHY either way. The rule of deploy/testnet-miner/node_upgrade_watch.sh::confirm_halt: a
# node that answers schedules the plan at a height and its last block is the one before it; a node that does not
# answer is read through data/upgrade-info.json, which x/upgrade writes as it stops. The next binary started before
# that height stops the node (x/upgrade refuses a binary that carries a plan not yet due), hence the confirmation.
node_halt_confirmed(){
  local plan="$1" cid st pdoc adoc v
  AFTER_UPGRADE_HALT_WHY="whether this kit's node stands at the halt of plan $plan was not read"
  command -v python3 >/dev/null 2>&1 || { AFTER_UPGRADE_HALT_WHY="python3, which reads the node's answers, is not on this host"; return 1; }
  cid="$( cd "$NODE_KIT" 2>/dev/null && docker compose ps -a -q node 2>/dev/null | head -1 | tr -dc '0-9A-Za-z' )"
  [ -n "$cid" ] || { AFTER_UPGRADE_HALT_WHY="no node container of this kit was found"; return 1; }
  st="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null | tr -dc 'a-z')"
  if [ "$st" = running ] && pdoc="$(docker exec "$cid" dendrad query upgrade plan -o json 2>/dev/null)" \
     && adoc="$(docker exec "$cid" curl -fsS -m 10 http://127.0.0.1:26657/abci_info 2>/dev/null)"; then
    v="$(node_halt_judge chain "$plan" "$pdoc" "$adoc" < /dev/null)"
  else
    v="$(docker cp "$cid:/root/.dendra/data/upgrade-info.json" - 2>/dev/null | node_halt_judge record "$plan")"
  fi
  case "$v" in
    "yes "*) AFTER_UPGRADE_HALT_WHY="${v#yes }"; return 0 ;;
    "no "*|"unread "*) AFTER_UPGRADE_HALT_WHY="${v#* }" ;;
  esac
  return 1
}
# node_halt_judge chain <plan> <plan document> <abci_info document> | node_halt_judge record <plan> < tar
# -> "yes|no|unread <why>". Documents are parsed, never searched as text; an absent field is its zero value.
node_halt_judge(){
  python3 -I -c '
import json, sys, tarfile
mode, plan = sys.argv[1], sys.argv[2]
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
    p, a = doc(sys.argv[3]), doc(sys.argv[4])
    r = a.get("result") if isinstance(a, dict) else None
    r = r.get("response") if isinstance(r, dict) else None
    last = num(r.get("last_block_height", 0)) if isinstance(r, dict) else None
    pl = p.get("plan") if isinstance(p, dict) else None
    if not isinstance(p, dict) or last is None or (pl is not None and not isinstance(pl, dict)):
        print("unread the node answered documents this run does not read")
    elif pl is None or pl.get("name", "") != plan or not num(pl.get("height", 0)):
        print("no the chain this node follows does not schedule plan %s" % plan)
    elif last != num(pl.get("height", 0)) - 1:
        print("no the last block of this node is %d, and a node stopped by plan %s stays at %d" % (last, plan, num(pl.get("height", 0)) - 1))
    else:
        print("yes the chain schedules plan %s at height %d, and this node stands at block %d, the halt" % (plan, num(pl.get("height", 0)), last))
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
    elif d.get("name", "") == plan and num(d.get("height", 0)):
        print("yes the node does not answer, and its halt record (data/upgrade-info.json, written by x/upgrade as it stops) names plan %s at height %d" % (plan, num(d.get("height", 0))))
    else:
        print("no the node does not answer, and its halt record (data/upgrade-info.json) does not name plan %s" % plan)' "$@" 2>/dev/null
}
# after_upgrade_finish -- after node_compose_up, when after_upgrade_applied or node_env_image_reconcile started this
# kit's node on this tree's image at the halt (AFTER_UPGRADE_SWITCH): the node is asked again whether the plan was
# applied, every 10 s up to DENDRA_AFTER_UPGRADE_WAIT_S. 1 and AFTER_UPGRADE_WHY when it does not answer a height.
after_upgrade_finish(){
  local bound="${DENDRA_AFTER_UPGRADE_WAIT_S:-300}" waited=0 h
  [ "${AFTER_UPGRADE_SWITCH:-0}" = 1 ] || return 0
  case "$bound" in ""|*[!0-9]*) bound=300 ;; esac
  while :; do
    h="$(after_upgrade_height "$AFTER_UPGRADE")"
    case "$h" in ''|*[!0-9]*|0) : ;; *) break ;; esac
    [ "$waited" -ge "$bound" ] && break
    sleep 10; waited=$((waited + 10))
  done
  case "$h" in
    0) AFTER_UPGRADE_WHY="this kit's node now runs this tree's node image, and it answers plan $AFTER_UPGRADE not applied yet: the network has not passed the plan's height on the new binary"; return 1 ;;
    ''|*[!0-9]*) AFTER_UPGRADE_WHY="this kit's node now runs this tree's node image, and whether plan $AFTER_UPGRADE was applied was not read (the node did not answer yet)"; return 1 ;;
  esac
  say "  [OK] this kit's node runs this tree's node image and answers plan $AFTER_UPGRADE applied at height $h."
  return 0
}
# applied_height_via_rpc <rpc> <plan> -> the height a chain RPC answers for the plan (x/upgrade AppliedPlan, asked by
# /abci_query): a positive integer once applied, 0 when the answer carries no height (proto3 omits a zero: never
# applied), ? when the RPC did not answer, refused the query or answered something else (neither curl nor python3 on
# this host gives ?). The same text, byte for byte, as deploy/testnet-miner/kit_update.sh::applied_height_via_rpc.
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
# upgrade_height_of_json <document> -> the height field of a QueryAppliedPlanResponse: a positive integer when the
# plan was applied, 0 when the field is absent (proto3 omits a zero height: never applied), ? when the document is
# not one (parsed by python3 or jq, never searched as text; neither on this host gives ?). The same function, byte
# for byte, as deploy/testnet-miner/node_upgrade_watch.sh::upgrade_height_of_json: its bench holds the two equal.
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

# Kit version: "is the code I run current?", a different question from "does my binary agree on the rules?".
#
# Three answers, and none of them refuses. A kit behind the network still joins: the defect it carries
# affects this operator, not the chain. Refusing here would lock a joiner out over a script fix, and the
# epoch check above already covers the case that must refuse. Two numbers, because one cannot serve
# both: merged, it would either refuse a joiner over a fix, or stay silent on a fork.
#
# Why it exists: a shipped defect had no way to announce itself. The operator ran code that could not do
# its job, everything looked healthy, and nothing said "update". The epoch could not carry that signal,
# because nothing about consensus had changed.
verify_kit_version(){
  local mine theirs f="$REPO/docker/KIT_VERSION"
  mine="$(head -1 "$f" 2>/dev/null | tr -dc 0-9)"
  theirs="$(printf '%s' "${KIT_VERSION:-}" | tr -dc 0-9)"
  if [ -z "$mine" ]; then
    warn "docker/KIT_VERSION unreadable in this tree -> cannot tell whether your kit is current."
    return 0
  fi
  if [ -z "$theirs" ]; then
    say "  [i] this network does not declare a kit version, so whether yours is current is not checked."
    say "      Yours is $mine. Ask the operator to add KIT_VERSION= to the file named by CONFIG_URL."
    return 0
  fi
  if [ "$mine" -ge "$theirs" ] 2>/dev/null; then
    say "  [OK] kit version $mine — current with what this network publishes."
    return 0
  fi
  warn "Kit behind: yours is $mine, this network publishes $theirs."
  say  "       Your node still joins. What you may be missing are fixes to the scripts and services"
  say  "       you run — and a defect there is silent by nature: the code does its job badly rather"
  say  "       than failing, so neither you nor the network can see it."
  kit_deadline_notice "$mine"
  say  "       Update:  $(kit_update_hint)"
  # DENDRA_UPDATE_HINT replaces the git instruction where this check runs from something other than a clone (the
  # cloud pod, a package on a release tag): the reason about git pull is given only with the git instruction.
  [ -n "${DENDRA_UPDATE_HINT:-}" ] \
    || say "       ('git pull' does not work here: each release republishes this repository as a new root commit, so it refuses with 'refusing to merge unrelated histories' and leaves the old tree in place.)"
  return 0
}

# kit_update_hint -> the single update instruction of this kit, printed and never run. The same git words stand in
# deploy/install.sh and deploy/testnet-miner/miner_health.sh (the kit-update bench of the development repository compares the
# three and executes them against a mirror republished as a new root commit). `checkout -B`, never
# `reset --hard`: it moves the branch to what origin serves and refuses rather than overwrite a file git does
# not track, and it carries an uncommitted change over instead of destroying it.
# `branch -f kit-before-update` comes first: `checkout -B` silently moves main away from a commit of the
# operator's own, which then lives in the reflog only. The branch keeps the tree the clone leaves (until the
# next update), so nothing the operator committed is silently dropped.
# One re-run, not two. "or bash deploy/install.sh --yes" is not added: install.sh keeps none of the flags it
# was first given (it asks for the judge role without --miner, runs a node of its own without --light), so for
# an operator who joined otherwise it would change the role or the topology. The re-run printed here carries this
# run's own options. DENDRA_UPDATE_HINT replaces the whole instruction where "update" means something else (the
# cloud pod: redeploy the image; a package on a release tag).
# `kit_update_hint rerun` prints the re-run alone: the one command line, built here and nowhere else.
# Six options describe an act of this run, not the installation, and are left out of the re-run: --after-upgrade
# <plan> (the join that follows one governed upgrade; replayed later it would read the answer of a plan long applied
# as leave to join a tree the network no longer declares); --plan (a
# re-run with it changes nothing); --reassign (a card index names a card in today's order; replayed after the
# cards changed, it would bind the slot to another card, and the binding it made is in the slot's env already);
# --accept-new-signers (a gesture of one run, with the fingerprints read by the operator: replayed daily by the
# automatic update it would accept every change of docker/RELEASE_SIGNERS unread, and kit_update.sh refuses a
# recorded line that carries it); --judge and --no-judge (the seat is in the kit's .env, DENDRA_NO_JUDGE and
# DENDRA_MINER_JUDGE, read at the top of this file and by judge.sh: replayed, a --judge would re-seat the judge
# that judge.sh off cleared, a --no-judge would clear the one judge.sh on seated).
# Every word is one a shell reads back unchanged: the clone's path, each option, CONFIG_URL. A word made only of
# letters, digits and , . _ : / @ % + = - is written bare (a card list `--gpus 0,2` stays `0,2`: printf %q would
# write `0\,2`, and that backslash keeps the line out of the kit's .env); any other word (a space in a path, a
# glob character, a tilde) is quoted by printf %q, so the printed command runs the same script with the same
# options, and persist_kit_update then says why that line is not written.
kit_update_hint(){
  local a w dir rerun skip=0 bare='^[A-Za-z0-9,._:/@%+=-]+$'
  dir="${REPO:-.}"; [[ $dir =~ $bare ]] || dir="$(printf '%q' "$dir")"
  rerun="bash $dir/deploy/join.sh"
  for a in "${JOIN_ARGS_ORIG[@]+"${JOIN_ARGS_ORIG[@]}"}"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in --after-upgrade) skip=1; continue ;; esac
    case "$a" in --reassign) skip=1; continue ;; --plan|--accept-new-signers|--judge|--no-judge) continue ;; esac
    w="$a"; [[ $w =~ $bare ]] || w="$(printf '%q' "$w")"
    rerun="$rerun $w"
  done
  if [ -n "${CONFIG_URL:-}" ]; then
    w="$CONFIG_URL"; [[ $w =~ $bare ]] || w="$(printf '%q' "$w")"
    rerun="CONFIG_URL=$w $rerun"
  fi
  if [ "${1:-}" = rerun ]; then printf '%s' "$rerun"; return 0; fi
  if [ -n "${DENDRA_UPDATE_HINT:-}" ]; then printf '%s' "$DENDRA_UPDATE_HINT"; return 0; fi
  printf 'cd %s && git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main, then re-run %s (with the same options; the tree you leave stays on the branch kit-before-update until the next update)' \
    "$dir" "$rerun"
}

# The instruction is written where the hourly watch reads it. A kit runs its own miner_health.sh, long after this
# script has returned, and that watch knows neither this run's options nor how this machine is updated: a
# generic instruction moves every clone to main and re-runs without the options, which is wrong for a package
# pinned to a release tag (HiveOS, install.sh --ref), and drops the options of the installation. So these lines go into the kit's
# .env, where deploy/testnet-miner/miner_health.sh and the Dendra application read the first two:
#   DENDRA_KIT_UPDATE       the update instruction of this machine (kit_update_hint above)
#   DENDRA_KIT_RERUN        the exact re-run of this join.sh (kit_update_hint rerun)
#   DENDRA_KIT_UPDATE_HINT  the package's own instruction (DENDRA_UPDATE_HINT), read back by a re-run that names
#                           none (the block after the CONFIG_URL read, at the top of this file); absent for a
#                           clone that follows main
# No secret: the options of join.sh carry none (addresses are public), the relay token is an environment
# variable and is never in the re-run, and a CONFIG_URL that carries credentials (user@host) is not written.
# Only values compose reads unchanged: compose reads this file and expands `$` and quotes in it, so a value with
# any character other than letters, digits, space and : / . _ ~ % ? = + @ , ; & ( ) - is not written, and an older
# line is removed, so that no reader keeps an instruction this run could not confirm. This is reported, and the
# readers then fall back to the generic instruction.
persist_kit_update(){
  local f="$MINER_KIT/.env" k v why
  [ -f "$f" ] || return 0
  for k in DENDRA_KIT_UPDATE DENDRA_KIT_RERUN DENDRA_KIT_UPDATE_HINT; do
    why=""
    case "$k" in
      DENDRA_KIT_UPDATE) v="$(kit_update_hint)" ;;
      DENDRA_KIT_RERUN) v="$(kit_update_hint rerun)" ;;
      # No package instruction is a clone that follows main: the line goes, and that is no fault to report. The
      # package's instruction carries no part of CONFIG_URL, so credentials there do not keep it out.
      *) v="${DENDRA_UPDATE_HINT:-}"
         [ -n "$v" ] || { _miner_env_set "$k" "" unset; continue; } ;;
    esac
    [ "$k" = DENDRA_KIT_UPDATE_HINT ] \
      || case "${CONFIG_URL:-}" in *://*@*) why="CONFIG_URL carries credentials (user@host)" ;; esac
    [ -n "$why" ] || [ -n "$v" ] || why="the value is empty"
    [ -n "$why" ] || [ -z "$(printf '%s' "$v" | LC_ALL=C tr -d 'A-Za-z0-9 :/._~%?=+@,;&()-')" ] \
      || why="the value holds a character compose would interpret: a quote, a dollar sign, or the backslash with which the re-run quotes a word a shell would not read as it is (a space in a path, a glob character, a tilde)"
    if [ -z "$why" ]; then
      _miner_env_set "$k" "$v"
    else
      _miner_env_set "$k" "" unset
      if [ "$k" = DENDRA_KIT_UPDATE_HINT ]; then
        warn "$k not written to $f: $why. A re-run that names no DENDRA_UPDATE_HINT prints the git instruction of a clone."
      else
        warn "$k not written to $f: $why. The hourly check falls back to the generic instruction (re-run deploy/join.sh with the options you used)."
      fi
    fi
  done
}

# kit_deadline_notice <this kit's number> -- the two keys of the kit floor, when network-info publishes them:
# KIT_MIN, the kit number below which the relay, the faucet and the season refuse a kit, and KIT_DEADLINE, the
# UTC instant from which they do. Printed under the "Kit behind" lines; absent keys print nothing, since
# nothing is then known about a refusal. Reading: curl -sf <CONFIG_URL>, keys KIT_MIN and KIT_DEADLINE.
kit_deadline_notice(){
  local mine="$1" kmin dl
  kmin="$(printf '%s' "${KIT_MIN:-}" | tr -dc 0-9)"; dl="${KIT_DEADLINE:-}"
  [ -n "$kmin" ] || return 0
  if [ "$mine" -lt "$kmin" ] 2>/dev/null; then
    say "       Update before ${dl:-the deadline the network publishes (KIT_DEADLINE)}: from then the network's services"
    say "       (relay, faucet, season) refuse a kit below $kmin (KIT_MIN), and yours is $mine."
  else
    say "       Kits below $kmin (KIT_MIN) are refused from ${dl:-the published deadline (KIT_DEADLINE)}; yours is $mine, above that floor."
  fi
  return 0
}

# judge_model_size_note -> the size of the judge model as the kit's compose file states it next to its pull
# (service judge-model-init), printed before the seat pulls anything. The engine reports
# a size only once the model is pulled, so before the pull this figure is the compose file's.
judge_model_size_note(){
  local s
  s="$(sed -n 's/.*judge model .*(\(~[0-9][0-9]* GB\)[,)].*/\1/p' "$MINER_KIT/docker-compose.yml" 2>/dev/null | head -1)"
  if [ -n "$s" ]; then printf 'about %s on disk, as deploy/testnet-miner/docker-compose.yml states next to its pull' "${s#\~}"
  else printf 'size not read from deploy/testnet-miner/docker-compose.yml'; fi
}

# ---------------------------------------------------------------- the release signers file and the tag
# docker/RELEASE_SIGNERS names the project's release key, in the allowed_signers format `ssh-keygen -Y verify`
# and `git verify-tag` read: a principal, the option namespaces="git,dendra-release", the key. It arrives with
# the clone, as docker/GENESIS_SHA256 does. The kit pins it: the first run records its SHA-256 in the kit's
# .env (DENDRA_RELEASE_SIGNERS_SHA256, trusted over HTTPS to the forge for that first time, as the genesis pin
# is), and every later run refuses a file that differs, unless --accept-new-signers, which prints the new
# file's fingerprints and records it. Whenever the clone sits on a release tag, the tag's signature is verified
# with that file, under git's own namespace; a tag that does not verify is not run. The automatic update
# (deploy/testnet-miner/kit_update.sh) verifies a candidate tag with this same file before anything moves.
# A file that is absent is reported: nothing is verified, and a clone at a tag is then refused, since a required
# signature that cannot be checked is not a pass.
RELEASE_SIGNERS_FILE="$REPO/docker/RELEASE_SIGNERS"
RELEASE_TAG_RE='^v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$'
SIGNERS_SHA_RECORD=""
# signers_key_lines <file> -> "principal keytype key" per entry: the key starts at the first field that names a
# key type, the options (none, or namespaces=...) sit before it. Comments and blank lines are skipped.
signers_key_lines(){
  awk '!/^[[:space:]]*(#|$)/ { for (i = 2; i <= NF; i++) if ($i ~ /^(sk-)?(ssh|ecdsa)-/) { print $1, $i, $(i + 1); break } }' "$1" 2>/dev/null | tr -d '\r'
}
# signers_fingerprints <file> -> "SHA256:<fingerprint> <principal>" per key; nothing when ssh-keygen is absent.
signers_fingerprints(){
  local f="$1" t p k v fp
  command -v ssh-keygen >/dev/null 2>&1 || return 1
  t="$(mktemp)" || return 1
  while read -r p k v; do
    [ -n "$v" ] || continue
    printf '%s %s\n' "$k" "$v" > "$t"
    fp="$(ssh-keygen -lf "$t" 2>/dev/null | awk '{ print $2 }')"
    [ -n "$fp" ] && printf '%s %s\n' "$fp" "$p"
  done < <(signers_key_lines "$f")
  rm -f "$t"
  return 0
}
# release_tag_here -> the release tag this clone sits on exactly, else nothing (a clone that follows main, a
# tree without git, a tag of another form).
release_tag_here(){
  local t
  t="$(git -C "$REPO" describe --exact-match --tags HEAD 2>/dev/null | head -1 | tr -d '\r')"
  [[ $t =~ $RELEASE_TAG_RE ]] && printf '%s' "$t"
  return 0
}
# verify_tag_signature <tag> <signers file> -> 0 when git verifies the tag with that file; the reason on stdout
# otherwise, and 1. Run with ssh-keygen present only: without it git cannot verify an SSH signature, and the
# caller treats that as not measured. Only an SSH signature is verified: the tag's signature block is read first,
# and git runs with gpg.program pointed at /bin/false, since a tag signed with gpg would otherwise be checked against
# this host's gpg keyring, where the signers file counts for nothing.
verify_tag_signature(){
  local out blk d='-----'  # the dashes of a block line are composed here, so no key-block marker is written in this file
  blk="$(git -C "$REPO" cat-file tag "$1" 2>/dev/null | grep "^${d}BEGIN " | tail -1)"
  if [ "$blk" != "${d}BEGIN SSH SIGNATURE${d}" ]; then
    printf 'the tag carries %s; the project signs its releases with an SSH key named in docker/RELEASE_SIGNERS, and nothing else is verified\n' "${blk:-no signature block}"; return 1
  fi
  if out="$(git -C "$REPO" -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile="$2" verify-tag "$1" 2>&1)"; then
    printf '%s' "$out" | grep -o 'SHA256:[A-Za-z0-9+/=]*' | head -1; return 0
  fi
  printf '%s\n' "$out" | head -4; return 1
}
verify_release_signers(){
  local f="$RELEASE_SIGNERS_FILE" have="" rec="" tag fps out
  SIGNERS_SHA_RECORD=""
  [ -r "$f" ] && have="$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)"
  [ "$ROLE" = miner ] && [ -f "$MINER_KIT/.env" ] && rec="$(sed -n 's/^DENDRA_RELEASE_SIGNERS_SHA256=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  fps="$(signers_fingerprints "$f" 2>/dev/null)"
  if [ -z "$have" ]; then
    if [ -n "$rec" ] && [ "$ACCEPT_NEW_SIGNERS" != 1 ]; then
      printf '[join] refused: docker/RELEASE_SIGNERS is absent or unreadable in this clone, and this kit recorded one (sha256 %s). A clone that lost its release signers file cannot verify any release: put the tree back (git checkout -- docker/RELEASE_SIGNERS), or re-run with --accept-new-signers to drop the record on purpose. Nothing was changed.\n' "$rec" >&2
      exit 2
    fi
    [ -n "$rec" ] && warn "docker/RELEASE_SIGNERS is absent from this clone and its record is dropped (--accept-new-signers): no release signature is verified until the file is back."
    [ -n "$rec" ] || say "  [i] docker/RELEASE_SIGNERS is absent from this clone: no release signature can be verified here, and none is recorded. The project ships it from kit v0.2.2."
  elif [ -z "$rec" ]; then
    SIGNERS_SHA_RECORD="$have"
    say "  [i] docker/RELEASE_SIGNERS recorded for this kit (sha256 $have): trusted over HTTPS to the forge this first time, as the genesis pin is. Every later run refuses a file that differs."
    [ -n "$fps" ] && printf '%s\n' "$fps" | sed 's/^/      key /'
    [ -n "$fps" ] || say "      (its fingerprints were not read: ssh-keygen is not installed, or the file names no key)"
  elif [ "$rec" = "$have" ]; then
    SIGNERS_SHA_RECORD="$have"
    say "  [OK] docker/RELEASE_SIGNERS matches the file this kit recorded."
  elif [ "$ACCEPT_NEW_SIGNERS" = 1 ]; then
    SIGNERS_SHA_RECORD="$have"
    warn "docker/RELEASE_SIGNERS differs from the file this kit recorded, and --accept-new-signers accepts it: recorded $rec, this clone $have. Its keys:"
    [ -n "$fps" ] && printf '%s\n' "$fps" | sed 's/^/      key /'
    [ -n "$fps" ] || say "      (fingerprints not read: ssh-keygen is not installed, or the file names no key)"
  else
    printf '[join] refused: docker/RELEASE_SIGNERS differs from the file this kit recorded (recorded sha256 %s, this clone %s). A changed signers file is a changed trust root for every release this kit will verify. Compare the fingerprints below with the ones the project announced, then re-run with --accept-new-signers to record this file. Nothing was changed.\n' "$rec" "$have" >&2
    [ -n "$fps" ] && printf '%s\n' "$fps" | sed 's/^/        key /' >&2
    exit 2
  fi
  tag="$(release_tag_here)"
  if [ -z "$tag" ]; then
    say "  [i] this clone is not at a release tag: no tag signature to verify (a clone that follows main, or a tree without git)."
    return 0
  fi
  if [ -z "$have" ]; then
    printf '[join] refused: this clone sits on the release tag %s and carries no readable docker/RELEASE_SIGNERS, so the tag signature cannot be verified, and a release whose signature cannot be checked is not run. Nothing was changed.\n' "$tag" >&2
    exit 2
  fi
  if ! command -v ssh-keygen >/dev/null 2>&1; then
    printf '[join] refused: this clone sits on the release tag %s, and ssh-keygen is not installed, so git cannot verify its SSH signature. Not measured is not a pass: install openssh-client, then re-run. Nothing was changed.\n' "$tag" >&2
    exit 2
  fi
  if out="$(verify_tag_signature "$tag" "$f")"; then
    say "  [OK] release tag $tag: signature verified with docker/RELEASE_SIGNERS (key ${out:-?})."
  else
    printf '[join] refused: the release tag %s does not verify with docker/RELEASE_SIGNERS of this clone. git said:\n' "$tag" >&2
    printf '%s\n' "$out" | sed 's/^/        /' >&2
    printf '        Reading, for anyone: git -c gpg.ssh.allowedSignersFile=docker/RELEASE_SIGNERS verify-tag %s. Nothing was changed.\n' "$tag" >&2
    exit 2
  fi
  return 0
}

# ---------------------------------------------------------------- the automatic update
# A daily crontab line runs deploy/testnet-miner/kit_update.sh --cron, which moves this clone to a newer release
# tag within its bounds (the same consensus epoch, a tag of the project's repository verified with this clone's
# docker/RELEASE_SIGNERS, never a branch; the plan printed and logged, kit-before-update kept, the recorded
# re-run replayed, a rollback when it fails) and nothing else. DENDRA_AUTO_UPDATE=0 in the kit's .env turns
# the whole of it off, and this function then removes the line. The hour is drawn from the machine id, so a
# fleet does not fetch at the same minute. The first installation is never this: it stays downloaded, read, run.
cron_remove_line(){ # cron_remove_line <mark> <what> -- removes the crontab lines that carry <mark>, keeping the rest
  local mark="$1" what="$2" d
  command -v crontab >/dev/null 2>&1 || return 0
  d="$(mktemp -d)" || return 1
  if crontab -l > "$d/tab" 2> "$d/err"; then :
  elif grep -qi 'no crontab' "$d/err" 2>/dev/null; then rm -rf "$d"; return 0
  else say "  [!] the crontab could not be read, so the $what line is not removed. Remove it yourself (crontab -e): the line that runs $mark"; rm -rf "$d"; return 1
  fi
  grep -qF -- "$mark" "$d/tab" || { rm -rf "$d"; return 0; }
  grep -vF -- "$mark" "$d/tab" > "$d/new"
  if crontab "$d/new" 2>/dev/null; then say "  [OK] $what unscheduled (crontab)"; rm -rf "$d"; return 0; fi
  say "  [!] the crontab could not be written: remove the $what line yourself (crontab -e)."; rm -rf "$d"; return 1
}
# The node upgrade watch (deploy/testnet-miner/node_upgrade_watch.sh) rides with it: a line every minute, written and
# removed with the daily one, since it acts only on a release of a higher epoch that the daily run staged.
install_kit_update_cron(){
  local script="$MINER_KIT/kit_update.sh" mark line h m seed watch="$MINER_KIT/node_upgrade_watch.sh"
  mark="$script --cron"
  if [ "$AUTO_UPDATE" = 0 ]; then
    say "  [i] automatic update: off (DENDRA_AUTO_UPDATE=0 in $MINER_KIT/.env). Update by hand: $(kit_update_hint)"
    cron_remove_line "$mark" "daily automatic update"
    cron_remove_line "$watch --cron" "node upgrade watch"
    return 0
  fi
  say "  [i] automatic update: on (DENDRA_AUTO_UPDATE=1). Once a day, deploy/testnet-miner/kit_update.sh moves this clone to a newer"
  say "      release tag of the project, only when the tag verifies with this clone's docker/RELEASE_SIGNERS and carries the"
  say "      same consensus epoch, never to a branch; it prints its plan, logs under $MINER_KIT/kit-update.log,"
  say "      keeps the tree it leaves on the branch kit-before-update, replays this run's re-run, and rolls back when"
  say "      that fails. Off switch: bash deploy/join.sh --no-auto-update (DENDRA_AUTO_UPDATE=0 in the kit's .env)."
  [ -f "$script" ] || { warn "deploy/testnet-miner/kit_update.sh is not in this clone: no automatic update is scheduled, update by hand ($(kit_update_hint))."; return 0; }
  seed="$( { cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo machine; } | sha256sum | cut -c1-8)"
  h=$(( 16#${seed:0:4} % 24 )); m=$(( 16#${seed:4:4} % 60 ))
  line="$m $h * * * bash -lc 'bash $script --cron'"
  case "$script" in
    *[!A-Za-z0-9/._-]*) say "  [!] this clone's path holds characters a crontab line cannot carry safely. Schedule it yourself: $line"; return 0 ;;
  esac
  if ! command -v crontab >/dev/null 2>&1; then
    say "  [!] no crontab on this host: schedule the daily update yourself, or run it by hand: bash $script"
    [ -f "$watch" ] && say "      and the node upgrade watch every minute: bash $watch --cron"
    return 0
  fi
  cron_add_line "$mark" "$line" "daily automatic update" "at $h:$(printf '%02d' "$m")"
  if [ -f "$watch" ]; then
    say "  [i] a release of a higher consensus epoch is staged by that run, never applied; node_upgrade_watch.sh, every minute,"
    say "      applies it when this node stops at the height of a plan that release registers (log: $MINER_KIT/node-upgrade-watch.log)."
    cron_add_line "$watch --cron" "* * * * * bash -lc 'bash $watch --cron'" "node upgrade watch" "every minute"
  fi
  return 0
}

verify_genesis_info(){
  # No GENESIS_URL is legitimate: the node kit may already carry a filled .env (see the config check
  # above), and node-join.sh enforces the hash from that file. Say so rather than skipping mutely.
  if [ -z "${GENESIS_URL:-}" ]; then
    # "The node kit enforces its own" is true only when there is a node kit.
    # With --remote-rpc no container starts and node-join.sh is never reached, so this is the only
    # check (see the header above). Returning 0 here would announce a protection that does not exist
    # in that mode: a miner pointed at an attacker's node, relay and faucet would pass every check,
    # register, stake, and serve inference on a chain whose identity was never verified. So this
    # fails closed, with the way out named.
    if [ "${OWN_NODE:-1}" = "0" ]; then
      if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
        warn "--remote-rpc with no GENESIS_URL + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> the chain identity is not verified (trusted network only)."
        return 0
      fi
      say "  [FAIL] --remote-rpc and no GENESIS_URL: nothing would check which chain this is."
      say "       Without a node kit there is no second line of defence — node-join.sh never runs in"
      say "       this mode, so this check is the only one, and it has nothing to check against."
      say "       Pass the operator's config (CONFIG_URL=<network-info.txt>), or set"
      say "       DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a network you already trust."
      die "--remote-rpc without GENESIS_URL -> refusing to serve a chain whose identity is unverified."
    fi
    say "  [i] no GENESIS_URL in this config -> genesis not checked here (the node kit enforces its own)"
    return 0
  fi
  if [ -z "${GENESIS_SHA256:-}" ]; then
    if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
      warn "GENESIS_SHA256 empty + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> anti-MITM verification disabled (trusted network only)."
      return 0
    fi
    say "  [FAIL] this config carries GENESIS_URL but no GENESIS_SHA256."
    say "       A network-info.txt without the hash cannot be verified — and that is exactly what a"
    say "       forged one would look like. Obtain the sha independently — security@dendranetwork.com,"
    say "       or a node you already trust — and put it in GENESIS_SHA256,"
    say "       or set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a network you already trust."
    die "genesis SHA256 missing from the config (anti-MITM) -> refusing to join blind."
  fi
  local tmp sha; tmp="$(mktemp)"
  if curl -fsSL "$GENESIS_URL" -o "$tmp" 2>/dev/null; then
    sha=$(sha256sum "$tmp" | cut -d' ' -f1)
    rm -f "$tmp"
    if [ "$sha" = "$GENESIS_SHA256" ]; then
      say "  [OK] genesis matches the hash published with it."
      # That is all it proves.
      # No wording here may claim network identity, because this check cannot establish it:
      # GENESIS_SHA256 arrives in the same network-info.txt as GENESIS_URL, over plain HTTP, from the
      # same host. Whoever can serve a forged genesis can serve the matching hash beside it, and this
      # check returns OK. What it does rule out: a corrupted download, a config that drifted from the
      # genesis it names, and an attacker who alters only one of the two.
      say "       It does not prove which network this is: the hash travels in the same file as the"
      say "       URL, over plain HTTP. It rules out a corrupted download and a config that drifted"
      say "       from its genesis — not an operator who serves you both."
      # The second source. `docker/GENESIS_SHA256` in the tree this script runs from names the genesis
      # this kit was published for, and it reaches you through the repository host, not through the host
      # that serves the genesis. Two hosts, one operator: this closes a forged genesis. It does not close
      # a forged operator (who holds both hosts), nor the other lines of network-info (trust point, RPC
      # endpoint, seeds), and no wording here may say more.
      # CHAIN_ID unlocks nothing. It travels in the same network-info.txt as the genesis, so a config
      # that names another chain id is compared all the same: a first version of this block treated
      # "another chain id" as out of scope and only warned, so a forged network-info needed one more
      # rewritten line to get past the pin. Three answers, never two: equal -> reported; different ->
      # refuse; missing or malformed -> refuse as well, because every published path runs from a clone
      # that carries the file (its absence means a clone older than the pin, or an edited tree). The
      # one named opt-out is the same as above: DENDRA_ALLOW_UNVERIFIED_GENESIS=1.
      # The line is read with `tr -dc` (a Windows checkout may add a carriage return) and the value
      # must be exactly 64 hex characters: a pin with a stray comment on line 1 is malformed, not a
      # different genesis, and the message says which.
      local pin_f="${REPO:-}/docker/GENESIS_SHA256" pin_l pin_id pin_sha pin_ok=0
      pin_l="$(head -1 "$pin_f" 2>/dev/null | tr -dc 'A-Za-z0-9=_.-')"
      pin_id="${pin_l%%=*}"
      pin_sha="$(printf '%s' "${pin_l#*=}" | tr 'A-F' 'a-f')"
      case "$pin_sha" in *[!0-9a-f]*) pin_sha="" ;; esac
      if [ "${#pin_sha}" = 64 ] && [ -n "$pin_id" ] && [ "$pin_id" != "$pin_l" ]; then pin_ok=1; fi
      if [ "$pin_ok" = 1 ] && [ "$pin_sha" = "$sha" ]; then
        say "  [OK] the served genesis is the one the published kit names (docker/GENESIS_SHA256, chain '$pin_id'):"
        say "       clone from the repository host, genesis from $GENESIS_URL -- two hosts, one operator."
        if [ -n "${CHAIN_ID:-}" ] && [ "$CHAIN_ID" != "$pin_id" ]; then
          warn "this config says CHAIN_ID='$CHAIN_ID' while the genesis it serves is the one pinned for '$pin_id' -> the config is inconsistent with its own genesis; the genesis is what was verified, ask the operator about network-info."
        fi
      elif [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
        if [ "$pin_ok" = 1 ]; then
          warn "the served genesis ($sha) is not the one docker/GENESIS_SHA256 names ($pin_sha) + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> joining a chain the published kit does not name (trusted network only)."
        else
          warn "docker/GENESIS_SHA256 missing or malformed in this tree + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> the served genesis is not cross-checked against the published kit (trusted network only)."
        fi
      elif [ "$pin_ok" != 1 ]; then
        say "  [FAIL] docker/GENESIS_SHA256 is missing or malformed in this tree (line 1 read as '${pin_l:-<empty>}')."
        say "       Every published path runs from a clone that carries it: yours predates its publication, or was edited."
        say "       Update:  $(kit_update_hint)"
        say "       or set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a network you already trust."
        die "no readable genesis pin in this tree -> refusing to join a chain the published kit cannot vouch for."
      else
        say "  [FAIL] the served genesis ($sha) is not the one the published kit names ($pin_sha, chain '$pin_id')."
        say "       Most likely: your copy of the kit predates a network relaunch -> $(kit_update_hint)."
        say "       Or the kit has not been republished yet after a relaunch: look at the last change to docker/GENESIS_SHA256 before writing to anyone."
        say "       If it still differs, this host serves a genesis the published kit does not name: stop, and write to security@dendranetwork.com."
        die "genesis differs from the one pinned in docker/GENESIS_SHA256 -> refusing to join a chain the published kit does not name."
      fi
    else
      die "published genesis SHA256 != expected ($GENESIS_SHA256) -> altered network / possible MITM. Get the sha from a second source (security@dendranetwork.com, or a node you already trust) before doing anything else."
    fi
  else
    # Never a silent skip: an unreachable genesis would leave the operator believing the check had passed.
    rm -f "$tmp"
    die "could not download the genesis at $GENESIS_URL -> the network identity cannot be verified. Check the URL, or the operator's host."
  fi
}

# ---------------------------------------------------------------- override GPU without editing YAML
# The override is a machine artefact, never a repository file: it reserves an NVIDIA device, and on a
# CPU-only / AMD / no-nvidia-container-toolkit host, `docker compose up` fails on the very first start
# with "could not select device driver", without naming the cause. Not writing it is not enough: it
# must be removed when the GPU is absent, otherwise a file left behind by an equipped machine (or by a
# repository clone) breaks the kit for an operator who never had a GPU.
write_gpu_override(){
  if [ "${ENGINE:-}" = cpu-judge ]; then write_cpu_judge_override; return $?; fi
  if [ "$GPU_OK" != 1 ] || [ "$TOOLKIT_OK" != 1 ]; then
    if [ -f "$MINER_KIT/docker-compose.override.yml" ]; then
      rm -f "$MINER_KIT/docker-compose.override.yml"
      say "  [i] GPU override removed: no NVIDIA card Docker can use (no mining model runs on the CPU: engine_decide)."
      say "      Keeping it would make 'docker compose up' fail on 'could not select device driver'."
    fi
    return 0
  fi
  cat > "$MINER_KIT/docker-compose.override.yml" <<'EOF'
# Generated by deploy/join.sh: enables the NVIDIA GPU for Ollama without editing docker-compose.yml.
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: all, capabilities: ["gpu"] }]
EOF
  say "  [OK] GPU enabled via docker-compose.override.yml (nothing to edit)"
}

# write_cpu_judge_override -> the override of a machine whose role is judge on the CPU (engine_decide). Same file,
# same rule as the GPU override above: a machine artefact, read by compose next to the base file, never edited by
# hand and never in the repository; it replaces whatever an earlier run left there (a GPU override on a machine
# that lost its card would make compose fail on the device driver).
# What it changes, and nothing else: model-init pulls the served model (the judge model, DENDRA_MODEL_ID here)
# and the verification embedder into the CPU instance (ollama-cpu) instead of a mining model into `ollama`;
# judge-model-init does not pull the same 19 GB a second time; ollama-cpu keeps the model loaded between requests
# (a reload of that size is time a request spends waiting); and the miner infers on ollama-cpu. The base file's
# `ollama` service still starts, and stays empty: model-init depends on it in the base file, and compose merges
# a dependency list rather than replacing it. Each pull that fails fails model-init (`|| exit 1`): the miner waits
# for model-init to complete successfully, and a status taken from the last command alone would let it start
# without the model it serves, with every assigned request answered by a 404.
write_cpu_judge_override(){
  cat > "$MINER_KIT/docker-compose.override.yml" <<'EOF' || { warn "docker-compose.override.yml could not be written in $MINER_KIT"; die "the judge role on the CPU needs its override: nothing was started."; }
# Generated by deploy/join.sh: the judge role on the CPU (deploy/hw_probe.sh --role: judge). A machine artefact.
# No usable GPU, so no mining model: the kit's CPU instance (ollama-cpu, profile judge) holds the judge model and
# the verification embedder; the miner answers the requests the chain assigns it with that model, on that
# instance, and the judge worker judges there. The base file's `ollama` service starts and stays empty.
services:
  model-init:
    depends_on: [ollama-cpu]
    environment:
      OLLAMA_HOST: "http://ollama-cpu:11434"
    command:
      - 'until ollama list >/dev/null 2>&1; do echo "waiting for the CPU Ollama..."; sleep 2; done;
         echo "Local download of the served model ${DENDRA_MODEL_ID} into the CPU instance (the judge model, one time only)...";
         ollama pull "${DENDRA_MODEL_ID}" || exit 1;
         echo "Local download of the verification embedder ${DENDRA_EMBED_API_MODEL:-nomic-embed-text} into the CPU instance...";
         ollama pull "${DENDRA_EMBED_API_MODEL:-nomic-embed-text}" || exit 1'
  judge-model-init:
    command:
      - 'if [ "${DENDRA_JUDGE_MODEL_ID:-}" = "${DENDRA_MODEL_ID}" ]; then echo "the judge model is the served model: model-init pulls it into the CPU instance"; exit 0; fi;
         until ollama list >/dev/null 2>&1; do echo "waiting for the CPU Ollama..."; sleep 2; done;
         ollama pull "${DENDRA_JUDGE_MODEL_ID:-qwen3:30b-a3b-instruct-2507-q4_K_M}"'
  # Two slots: here the miner's requests and its presence probe share this instance with the judge, and with one
  # slot a request waits behind a whole judge generation, past the client's wait. Each slot holds its own KV cache.
  ollama-cpu:
    environment:
      OLLAMA_KEEP_ALIVE: "1h"
      OLLAMA_NUM_PARALLEL: "2"
  miner:
    environment:
      OLLAMA_ENDPOINT: "http://ollama-cpu:11434"
EOF
  say "  [OK] judge role on the CPU via docker-compose.override.yml: no mining model is pulled; the CPU instance"
  say "       (ollama-cpu) holds $DENDRA_MODEL_ID and the embedder, and answers this miner's requests too"
}

# ================================================================ one identity per card (--gpus, opt-in)
# Without --gpus and without slots on disk, nothing in this section runs and the kit is what it was: one
# identity, the override above (`count: all`), the .env below. With it, each NVIDIA card gets its own miner
# identity (its own key, stake, keyring and 24-word phrase), bound to the card by its UUID:
#   slot 0 = this installation (project dendra-miner), never renamed; slot k = project dendra-miner-g<k>,
#   files gpu/<k>/.env and gpu/<k>/override.yml. deploy/testnet-miner/slots.sh is the one library that
#   knows a slot; this section plans the slots, writes their files and starts them, one at a time.
# What one more card does and does not do, stated where the option lives: each card is an identity with its own
# stake; it shares, by stake, a fixed daily volume of requests (`RULES` in final_season_rules.py); its presence
# is paid only on a day it served a verified request; it sits on juries; it adds no work to the network;
# and the identities of one operator may sit on the audit of that operator's own work.

# _slots_load -> the slot library, loaded once (a message on stderr and exit 1 when it is not in the clone).
_slots_load(){
  declare -F slot_argv >/dev/null 2>&1 && return 0
  [ -r "$MINER_KIT/slots.sh" ] || { echo "[join] the slot library $MINER_KIT/slots.sh is missing from this clone" >&2; return 1; }
  # shellcheck disable=SC1091
  . "$MINER_KIT/slots.sh" || { echo "[join] the slot library $MINER_KIT/slots.sh could not be loaded" >&2; return 1; }
}
_gpu_refuse(){ printf '[join] %s Nothing was changed.\n' "$*" >&2; return 2; }
_gl_of_uuid(){ local i=0; while [ "$i" -lt "${GPU_N:-0}" ]; do [ "${GL_UUID[$i]}" = "$1" ] && { printf '%s' "$i"; return 0; }; i=$((i+1)); done; return 1; }
_gl_of_index(){ local i=0; while [ "$i" -lt "${GPU_N:-0}" ]; do [ "${GL_IDX[$i]}" = "$1" ] && { printf '%s' "$i"; return 0; }; i=$((i+1)); done; return 1; }
# _retired_card_note <k> -> one line when slot k is retired and its card is on this host: the card stays idle
# (no other identity takes it), `all` does not start a retired identity again, naming the card does.
_retired_card_note(){
  local i
  [ "${SLOT_STATE[$1]:-}" = retired ] || return 0
  i="$(_gl_of_uuid "${SLOT_UUID[$1]}")" || return 0
  say "  slot $1 ($(slot_project "$1")) is retired and its card (index ${GL_IDX[$i]}) is here: it stays idle. --gpus all never starts a retired identity again; to start it, name its card (--gpus <indices including ${GL_IDX[$i]}>)."
}

# plan_gpu_slots -> reads the cards (deploy/hw_probe.sh --list-gpus, three states), the slots on disk and
# GPUS_SPEC, and fills MULTI, SLOT_KS, SLOT_UUID[k], SLOT_STATE[k], SLOT_MODEL[k]. Writes nothing. Exit 2,
# the reason on stderr, on every refusal, all of them taken before anything changes.
#   keep          the slot runs its card            new      a card that gets a new identity (smallest free k)
#   retire        its card left --gpus: stopped     retired  stopped earlier, and stays so
#   card-missing  its card is not on this host now: not started, not retired, never re-bound in silence
plan_gpu_slots(){
  local ids k u i d a b c e f g h list desired="" want="" u0="" rk rt ru nk n_run=0 owner payout
  MULTI=0; GPU_N=0; SLOT_KS=""; GPU_LIST_RC=""; HAS_SLOTS=0; N_RUN=0
  GL_IDX=(); GL_UUID=(); GL_TOT=(); GL_FREE=(); GL_TIER=(); GL_MODEL=(); GL_PCI=(); GL_NAME=()
  SLOT_UUID=(); SLOT_STATE=(); SLOT_MODEL=(); EX_UUID=(); EX_STATE=()
  _slots_load || { _gpu_refuse "The slot library could not be loaded."; return 2; }
  ids="$(slot_ids --all)" || { _gpu_refuse "The slots of this kit ($MINER_KIT/gpu) cannot be read: which identity runs which card is unknown."; return 2; }
  for k in $ids; do
    [ "$k" != 0 ] && HAS_SLOTS=1
    EX_UUID[$k]="$(slot_val "$k" DENDRA_GPU_UUID 2>/dev/null)"; EX_STATE[$k]="$(slot_state "$k")"
  done
  [ "$(slot_val 0 DENDRA_SLOT 2>/dev/null)" = 0 ] && HAS_SLOTS=1
  list="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --list-gpus 2>/dev/null)"; GPU_LIST_RC=$?
  if [ "$GPU_LIST_RC" = 0 ]; then
    while IFS='|' read -r a b c d e f g h; do
      [ -n "$a" ] || continue
      GL_IDX[$GPU_N]="$a"; GL_UUID[$GPU_N]="$b"; GL_TOT[$GPU_N]="$c"; GL_FREE[$GPU_N]="$d"
      GL_TIER[$GPU_N]="$e"; GL_MODEL[$GPU_N]="$f"; GL_PCI[$GPU_N]="$g"; GL_NAME[$GPU_N]="$h"
      GPU_N=$((GPU_N+1))
    done <<EOF
$list
EOF
  fi
  # No --gpus and no slot on disk: the single-identity kit, unchanged. One line says the option exists.
  if [ -z "$GPUS_SPEC" ] && [ "$HAS_SLOTS" = 0 ]; then
    [ -z "$REASSIGN" ] || { _gpu_refuse "--reassign binds a slot written by --gpus to another card, and this kit has none."; return 2; }
    if [ "$GPU_LIST_RC" = 0 ] && [ "$GPU_N" -ge 2 ]; then
      say "  [i] $GPU_N NVIDIA cards here; this kit runs one identity. To run one per card: bash deploy/join.sh --gpus all"
    fi
    return 0
  fi
  MULTI=1
  # Slots exist but the cards cannot be read: nothing is started or stopped, since an identity is bound to a
  # card, and a card nobody can see now may be a card that changed.
  [ "$GPU_LIST_RC" = 0 ] || { _gpu_refuse "The NVIDIA cards of this host could not be read (deploy/hw_probe.sh --list-gpus exit $GPU_LIST_RC: nvidia-smi fails). No slot is started or stopped while its card cannot be checked against the identity bound to it."; return 2; }
  # An explicit re-binding, applied to what is on disk before anything is decided from it.
  if [ -n "$REASSIGN" ]; then
    rk="${REASSIGN%%=*}"; rt="${REASSIGN#*=}"
    case "$rt" in
      GPU-*) _gl_of_uuid "$rt" >/dev/null || { _gpu_refuse "--reassign names $rt, which is not a card of this host."; return 2; }; ru="$rt" ;;
      *) i="$(_gl_of_index "$rt")" || { _gpu_refuse "--reassign names card index $rt, and this host has no such card."; return 2; }; ru="${GL_UUID[$i]}" ;;
    esac
    [ "$ru" != "?" ] || { _gpu_refuse "--reassign: the UUID of that card could not be read."; return 2; }
    [ "$(slot_state "$rk")" != missing ] || { _gpu_refuse "--reassign: slot $rk does not exist in this kit."; return 2; }
    for k in $ids; do
      [ "$k" != "$rk" ] && [ "${EX_UUID[$k]}" = "$ru" ] && { _gpu_refuse "--reassign: the card $ru is bound to slot $k; one card, one identity."; return 2; }
    done
    say "  [i] --reassign: slot $rk ($(slot_project "$rk")) is bound to the card $ru from now on (it was ${EX_UUID[$rk]:-unbound}); its identity, stake and keys do not move."
    EX_UUID[$rk]="$ru"
  fi
  # The cards wanted, in index order, by UUID: indices are translated once, here, and only UUIDs are kept.
  if [ -n "$GPUS_SPEC" ]; then
    case "$GPUS_SPEC" in
      all) want="all" ;;
      GPU-*) for u in $(printf '%s' "$GPUS_SPEC" | tr ',' ' '); do
               _gl_of_uuid "$u" >/dev/null || { _gpu_refuse "--gpus names $u, which is not a card of this host (nvidia-smi -L lists them)."; return 2; }
               want="$want $u"; done ;;
      *) for d in $(printf '%s' "$GPUS_SPEC" | tr ',' ' '); do
           i="$(_gl_of_index "$d")" || { _gpu_refuse "--gpus names card index $d, and this host has no such card (nvidia-smi -L lists them)."; return 2; }
           want="$want ${GL_UUID[$i]}"; done ;;
    esac
  else
    for k in $ids; do [ "${EX_STATE[$k]}" = active ] && [ -n "${EX_UUID[$k]}" ] && want="$want ${EX_UUID[$k]}"; done
  fi
  i=0
  while [ "$i" -lt "$GPU_N" ]; do
    case " $want " in *" all "*|*" ${GL_UUID[$i]} "*)
      [ "${GL_UUID[$i]}" != "?" ] || { _gpu_refuse "Card index ${GL_IDX[$i]}: its UUID could not be read, and an identity is bound to a card by its UUID."; return 2; }
      desired="$desired ${GL_UUID[$i]}" ;;
    esac
    i=$((i+1))
  done
  [ -n "$desired" ] || [ -z "$GPUS_SPEC" ] || { _gpu_refuse "--gpus $GPUS_SPEC selects no card on this host."; return 2; }
  # Slot 0 keeps the card its .env names; unbound, it takes the lowest-index wanted card no other slot holds.
  u0="${EX_UUID[0]:-}"
  if [ -n "$u0" ]; then
    _gl_of_uuid "$u0" >/dev/null || { _gpu_refuse "Slot 0 (this installation, project dendra-miner) is bound to the card $u0, which this host does not list now. Bind it to a present card on purpose: bash deploy/join.sh${GPUS_SPEC:+ --gpus $GPUS_SPEC} --reassign 0=<card index>"; return 2; }
    case " $desired " in *" $u0 "*) : ;; *)
      _gpu_refuse "Slot 0 is this installation (project dendra-miner): its card $u0 cannot be left out of --gpus. To stop mining with it, see deploy/testnet-miner/exit-miner.sh."; return 2 ;; esac
  else
    for u in $desired; do
      a=0; for k in $ids; do [ "$k" != 0 ] && [ "${EX_UUID[$k]}" = "$u" ] && a=1; done
      [ "$a" = 0 ] && { u0="$u"; break; }
    done
    # A re-run without --gpus whose slot 0 lost its binding (its .env rewritten by an older join.sh): it takes
    # the lowest-index card no slot holds, and says so; the slots k keep theirs.
    if [ -z "$u0" ] && [ -z "$GPUS_SPEC" ]; then
      i=0
      while [ "$i" -lt "$GPU_N" ]; do
        u="${GL_UUID[$i]}"; a=0
        for k in $ids; do [ "$k" != 0 ] && [ "${EX_UUID[$k]}" = "$u" ] && a=1; done
        if [ "$a" = 0 ] && [ "$u" != "?" ]; then u0="$u"; desired="$desired $u"; break; fi
        i=$((i+1))
      done
      [ -n "$u0" ] && say "  [i] slot 0 named no card in its .env: it is bound to $u0 (the lowest-index card no other slot holds)."
    fi
    [ -n "$u0" ] || { _gpu_refuse "No card is left for slot 0 (this installation): every card wanted is bound to another slot."; return 2; }
  fi
  SLOT_UUID[0]="$u0"; SLOT_STATE[0]=keep; SLOT_KS="0"
  for k in $ids; do
    [ "$k" = 0 ] && continue
    u="${EX_UUID[$k]}"; SLOT_UUID[$k]="$u"; SLOT_KS="$SLOT_KS $k"
    if ! _gl_of_uuid "$u" >/dev/null; then SLOT_STATE[$k]=card-missing
    else case " $desired " in
      # A retired slot starts again only when its card is named (by index or UUID): `all` selects the cards
      # present, it does not undo an exit. Re-added by `all`, a slot that left the network (exit-miner.sh
      # --slot) would register again with a new stake, and `all` is also what a re-run prints to resume.
      *" $u "*) if [ "${EX_STATE[$k]}" = retired ] && [ "$want" = all ]; then SLOT_STATE[$k]=retired; else SLOT_STATE[$k]=keep; fi ;;
      *) if [ "${EX_STATE[$k]}" = active ]; then SLOT_STATE[$k]=retire; else SLOT_STATE[$k]=retired; fi ;;
    esac; fi
  done
  nk=1
  for u in $desired; do
    a=0; for k in $SLOT_KS; do [ "${SLOT_UUID[$k]}" = "$u" ] && a=1; done
    [ "$a" = 1 ] && continue
    while [ -e "$MINER_KIT/gpu/$nk" ] || case " $SLOT_KS " in *" $nk "*) true ;; *) false ;; esac; do nk=$((nk+1)); done
    SLOT_UUID[$nk]="$u"; SLOT_STATE[$nk]=new; SLOT_KS="$SLOT_KS $nk"
  done
  SLOT_KS="$(printf '%s\n' $SLOT_KS | sort -n | tr '\n' ' ')"
  for k in $SLOT_KS; do
    case "${SLOT_STATE[$k]}" in keep|new) : ;; *) continue ;; esac
    n_run=$((n_run+1))
    i="$(_gl_of_uuid "${SLOT_UUID[$k]}")"
    if [ -n "${DENDRA_MODEL_ID_EXPLICIT:-}" ]; then SLOT_MODEL[$k]="$DENDRA_MODEL_ID_EXPLICIT"
    else
      SLOT_MODEL[$k]="${GL_MODEL[$i]}"
      [ "${SLOT_MODEL[$k]}" != "?" ] || { _gpu_refuse "Card ${SLOT_UUID[$k]}: its memory could not be read, so no model can be sized for it."; return 2; }
    fi
  done
  N_RUN="$n_run"
  # Two or more identities: the refusals that follow from one owner address and from N hot keys.
  if [ "$N_RUN" -ge 2 ]; then
    owner="$OWNER_ADDRESS"; [ -n "$owner" ] || owner="$(sed -n 's/^DENDRA_MINER_OWNER=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
    [ -z "$owner" ] || { _gpu_refuse "Owner mode ($owner) with $N_RUN cards: one owner address registers exactly one miner (CreateMiner); $N_RUN cards need $N_RUN owner addresses, and multi-GPU owner mode is not supported yet."; return 2; }
    payout="$PAYOUT_ADDRESS"; [ -n "$payout" ] || payout="$(sed -n 's/^DENDRA_PAYOUT_ADDRESS=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
    [ -n "$payout" ] || { _gpu_refuse "--gpus runs $N_RUN identities, each with its own hot key on this machine: declare once where all of them are paid, a key not on this machine (--payout-address dendra1...). Losing a hot key then costs its stake, not the season's pay."; return 2; }
  fi
  if [ -z "${DENDRA_MODEL_ID_EXPLICIT:-}" ] && [ -n "${SLOT_MODEL[0]:-}" ]; then
    export DENDRA_MODEL_ID="${SLOT_MODEL[0]}"
  elif [ -n "${DENDRA_MODEL_ID_EXPLICIT:-}" ]; then
    say "  [i] model imposed by you ($DENDRA_MODEL_ID_EXPLICIT): applied to every card, whatever its size."
  fi
  [ "$ID_GIVEN" = 1 ] && say "  [i] --id names slot 0 only: each other card's identity is derived from its own key."
  return 0
}

# print_gpu_plan -> what --gpus would do, read and computed, nothing written, no engine asked.
print_gpu_plan(){
  local i k s p
  say "== [join] plan: nothing is written, nothing is started, no engine is asked =="
  if [ "$GPU_LIST_RC" != 0 ]; then say "  cards: unknown (deploy/hw_probe.sh --list-gpus exit $GPU_LIST_RC)"
  else say "  cards read on this host (deploy/hw_probe.sh --list-gpus): $GPU_N"; fi
  printf '  %-5s %-40s %8s %4s %-34s %-5s %-18s %s\n' index card vram_mb tier model slot project state
  i=0
  while [ "$i" -lt "${GPU_N:-0}" ]; do
    s="-"; p="-"; k=""
    if [ "$MULTI" = 1 ]; then for k in $SLOT_KS; do [ "${SLOT_UUID[$k]}" = "${GL_UUID[$i]}" ] && { s="$k"; break; }; done; fi
    if [ "$s" != "-" ]; then p="$(slot_project "$s")"; else k=""; fi
    if [ "$MULTI" != 1 ]; then s="0"; p="dendra-miner"; fi
    printf '  %-5s %-40s %8s %4s %-34s %-5s %-18s %s\n' "${GL_IDX[$i]}" "${GL_UUID[$i]}" "${GL_TOT[$i]}" "${GL_TIER[$i]}" \
      "$( [ "$s" != "-" ] && [ "$MULTI" = 1 ] && printf '%s' "${SLOT_MODEL[$s]:-${GL_MODEL[$i]}}" || printf '%s' "${GL_MODEL[$i]}")" "$s" "$p" \
      "$( if [ "$MULTI" != 1 ]; then printf 'shared (one identity, count: all)'; elif [ "$s" = "-" ]; then printf 'not selected'; else printf '%s' "${SLOT_STATE[$s]}"; fi )"
    i=$((i+1))
  done
  if [ "$MULTI" = 1 ]; then
    for k in $SLOT_KS; do
      [ "${SLOT_STATE[$k]}" = card-missing ] && say "  slot $k ($(slot_project "$k")): its card ${SLOT_UUID[$k]} is not on this host now: stopped if it runs, not retired. To bind it to a present card: --reassign $k=<card index>"
      _retired_card_note "$k"
    done
    say "  identities that will run: $N_RUN (slot 0 = this installation, never renamed)"
  else
    say "  identities that will run: 1 (this kit without --gpus). One per card: bash deploy/join.sh --gpus all --plan"
  fi
  local n="${N_RUN:-1}"; [ "$MULTI" = 1 ] || n=1
  if [ "$JUDGE" = 1 ] && [ "$HW_CAN_JUDGE" = true ]; then
    say "  judge: one CPU judge for the machine, on slot 0 (alias dendra-judge-cpu on the dendra-rig network); every identity votes through it"
  else
    say "  judge: none ($( if [ "$JUDGE" = 1 ]; then printf 'this machine cannot judge: deploy/hw_probe.sh --can-judge says %s' "$HW_CAN_JUDGE"; elif [ "$NO_JUDGE" = 1 ]; then printf -- '--no-judge'; else printf 'deploy/hw_probe.sh --can-judge says %s' "$HW_CAN_JUDGE"; fi )): $n identities will be drawn as jurors and never vote"
  fi
  say "  faucet: each identity takes one drip to register; the faucet caps drips per IP and per day (DENDRA_FAUCET_IP_DAILY,"
  say "          services/faucet.py). A refused identity retries by itself; join.sh stops starting the next"
  say "          ones at the first IP-quota refusal and prints the command that resumes."
  say "  stake: each identity locks its own min_stake (dendrad query jobs params); keys: one keyring and one 24-word"
  say "         recovery phrase per identity, under this machine's one keyring passphrase."
  say "  RAM: a slot k starts only if MemAvailable minus DENDRA_RIG_RESERVE_MB (minus the judge floor while the judge"
  say "       model is not loaded) covers the footprint of slot 0, measured once slot 0 runs; unreadable = not started."
  say "  what a card adds: an identity with its own stake, sharing by stake a fixed daily volume of requests"
  say "       (final_season_rules.py), paid for its presence only on a day it served a verified request; it sits"
  say "       on juries and adds no work to the network; identities of one operator may sit on the audit of its own work."
}

# gpu_mode_preflight -> runs before the pre-flight, so that --plan touches no engine and every refusal comes
# before anything is written. Library mode never reaches it.
gpu_mode_preflight(){
  local k i
  [ "$ROLE" = miner ] || return 0
  plan_gpu_slots || exit 2
  if [ "$PLAN_ONLY" = 1 ]; then print_gpu_plan; exit 0; fi
  if [ "${MULTI:-0}" = 1 ]; then
    say "== [join] one identity per card: $N_RUN to run on this machine =="
    for k in $SLOT_KS; do
      i="$(_gl_of_uuid "${SLOT_UUID[$k]}")" || i=""
      say "  slot $k ($(slot_project "$k")): ${SLOT_STATE[$k]} -- card ${i:+${GL_IDX[$i]} ${GL_NAME[$i]} }${SLOT_UUID[$k]}${SLOT_MODEL[$k]:+, model ${SLOT_MODEL[$k]}}"
    done
  fi
  return 0
}

# gpu_select_measure -> GPU_SELECT=device_ids | env-only | unknown. Measured, not assumed (WSL or not): does
# the engine hand a container exactly the card asked for? A container is shown only that card -> the
# reservation names it (device_ids). It sees others -> the card is chosen by CUDA_VISIBLE_DEVICES alone
# (env-only). The probe fails -> unknown, treated as env-only and reported. CUDA_VISIBLE_DEVICES is written in
# every case; the hourly pinning check (slots.sh pin) says whether it held.
GPU_SELECT=""
gpu_select_measure(){
  local u="${SLOT_UUID[0]}" out rc oimg
  oimg="$(ollama_image)"
  if [ -z "$oimg" ]; then
    GPU_SELECT=unknown; warn "the engine image the kit pins could not be read ($MINER_KIT/docker-compose.yml): whether the engine hands a container one card only was not measured; each slot is held to its card by CUDA_VISIBLE_DEVICES, which the pinning check verifies"
    return 0
  fi
  out="$(docker run --rm --gpus "device=$u" --entrypoint nvidia-smi "$oimg" --query-gpu=uuid --format=csv,noheader 2>/dev/null)"; rc=$?
  out="$(printf '%s\n' "$out" | tr -d ' \r' | awk 'NF')"
  if [ "$rc" = 0 ] && [ "$out" = "$u" ]; then
    GPU_SELECT=device_ids; say "  [OK] the engine hands a container exactly the card it is given: each slot reserves its own card (device_ids)"
  elif [ "$rc" = 0 ] && [ -n "$out" ]; then
    GPU_SELECT=env-only; say "  [i] given one card, the engine shows a container others too: each slot is held to its card by CUDA_VISIBLE_DEVICES"
  else
    GPU_SELECT=unknown; warn "whether the engine hands a container one card only could not be measured: each slot is held to its card by CUDA_VISIBLE_DEVICES, which the pinning check verifies"
  fi
}

# gpu_slot_compose_files <k> -> the COMPOSE_FILE written into gpu/<k>/.env: the base kit, the overlay to this
# machine's node when it runs one (as for slot 0), and the slot's own override -- never slot 0's.
gpu_slot_compose_files(){
  local f="docker-compose.yml"
  [ "${OWN_NODE:-1}" = 1 ] && f="$f:docker-compose.local-node.yml"
  printf '%s:gpu/%s/override.yml' "$f" "$1"
}

# write_slot_override <k> <uuid> -> slot k's card, literally: no `${...}` in this file, so no default can make
# CUDA_VISIBLE_DEVICES empty -- and empty means no GPU, which is what the base file gives ollama-cpu on purpose.
# An unknown UUID has the same effect (the engine falls back to the CPU in silence): that is why the UUID is
# read again from the card list on every run and the pinning is checked afterwards. OLLAMA_NOPRUNE: the slots
# share one model store, and an engine that starts prunes the blobs it does not reference -- possibly those a
# pull in another slot is writing (option listed by `ollama serve --help` on 0.32.1).
write_slot_override(){
  local k="$1" u="$2" d f t
  printf '%s\n' "$u" | grep -Eq "$SLOT_UUID_RE" || { warn "slot $k: '$u' is not a card UUID: no override written"; return 1; }
  if [ "$k" = 0 ]; then d="$MINER_KIT"; f="$MINER_KIT/docker-compose.override.yml"
  else
    d="$MINER_KIT/gpu/$k"; f="$d/override.yml"
    ( umask 077; mkdir -p "$MINER_KIT/gpu" "$d" ) && chmod 700 "$MINER_KIT/gpu" "$d" || return 1
  fi
  t="$(umask 077; mktemp)" || return 1
  {
    echo "# Generated by deploy/join.sh --gpus: slot $k is bound to one card, by its UUID. A machine file, never published."
    echo "services:"
    echo "  ollama:"
    echo "    environment:"
    echo "      CUDA_VISIBLE_DEVICES: \"$u\""
    echo "      OLLAMA_NOPRUNE: \"1\""
    echo "    deploy:"
    echo "      resources:"
    echo "        reservations:"
    if [ "${GPU_SELECT:-}" = device_ids ]; then
      echo "          devices: [{ driver: nvidia, device_ids: [\"$u\"], capabilities: [\"gpu\"] }]"
    else
      echo "          devices: [{ driver: nvidia, count: all, capabilities: [\"gpu\"] }]"
    fi
    if [ "$JUDGE" = 1 ] && [ "$k" = 0 ]; then
      echo "  ollama-cpu:"
      echo "    networks:"
      echo "      default: {}"
      echo "      rig:"
      echo "        aliases: [dendra-judge-cpu]"
    elif [ "$JUDGE" = 1 ]; then
      echo "  miner:"
      echo "    networks:"
      echo "      default: {}"
      echo "      rig: {}"
    fi
    if [ "$k" != 0 ]; then
      echo "volumes:"
      echo "  ollama-models:"
      echo "    external: true"
      echo "    name: dendra-miner_ollama-models"
    fi
    if [ "$JUDGE" = 1 ]; then
      echo "networks:"
      echo "  rig:"
      echo "    external: true"
      echo "    name: dendra-rig"
    fi
  } > "$t" || { rm -f "$t"; return 1; }
  ( umask 077; cat "$t" > "$f" ) || { rm -f "$t"; return 1; }
  rm -f "$t"
  [ "$k" = 0 ] || chmod 600 "$f"
  return 0
}

# What a slot k takes from slot 0's .env, by name: the network, the relay and its token, the faucet, the
# capacity registry, the season's payout address, the passphrase directory (one per machine), the judge's
# model, and the image slot 0 started (read after it started). What it never takes, and why:
#   COMPOSE_PROFILES   the judge's engine is slot 0's, one per machine; a second would pull its model again;
#   DENDRA_MINER_JUDGE written from this run's role, like the judge's endpoint: carried from slot 0's .env
#                      (kept when the network could not be read) it said "judge" with no endpoint, and the
#                      verdicts went to the slot's own GPU engine -- one decision, two sources;
#   DENDRA_SLOT_RETIRED, and every line this function writes itself (identity, card, model, files, owner).
# The name of the key that signs relay deposits is not a line of any slot's env: this script writes it for no
# slot, and compose derives it from each slot's own MINER_ID. A line it does not name is never copied.
# The bench compares these lists with every variable docker-compose.yml interpolates. The daemon's and the serve
# guard's bounds (DENDRA_REGISTER_RETRY_S, DENDRA_PRESENCE_PROBE_S, DENDRA_SERVE_GUARD_WAIT_S) are carried like the
# proof-of-work bound: a value set once in slot 0's .env applies to every identity of the machine. So are the
# daemon's job retries and the age past which a request is not served (DENDRA_JOB_MAX_ATTEMPTS,
# DENDRA_REQUEST_MAX_AGE_S), and the judge's bound, divergence rule and ambiguity vote (DENDRA_JUDGE_TIMEOUT_S,
# DENDRA_JUDGE_DIVERGENCE_SLASH, DENDRA_JUDGE_ABSTAIN_VOTE): those rules are a decision of the machine's owner, and
# an identity that kept its own copy would judge under another rule than the one its owner set.
# The update instruction (DENDRA_KIT_UPDATE: one machine, one instruction, printed by every daemon of it) and the
# payout lock (DENDRA_PAYOUT_LOCK: the owner's decision for every identity declared from this machine) are carried
# for the same reason.
SLOT_CARRIED_KEYS="DENDRA_NODE DENDRA_CAPACITY_URL CONFIG_URL DENDRA_RELAY DENDRA_RELAY_TOKEN FAUCET DENDRA_EMBED_MODE DENDRA_EMBED_API_MODEL DENDRA_MINER_STAKE DENDRA_FINAL_SEASON_URL DENDRA_PAYOUT_ADDRESS DENDRA_PAYOUT_LOCK DENDRA_SECRETS_DIR DENDRA_MINER_IMAGE DENDRA_FAUCET_POW_MAX_S DENDRA_REGISTER_RETRY_S DENDRA_PRESENCE_PROBE_S DENDRA_SERVE_GUARD_WAIT_S DENDRA_JOB_MAX_ATTEMPTS DENDRA_REQUEST_MAX_AGE_S DENDRA_JUDGE_TIMEOUT_S DENDRA_JUDGE_DIVERGENCE_SLASH DENDRA_JUDGE_ABSTAIN_VOTE DENDRA_JUDGE_MODEL_ID DENDRA_JUDGE_MODEL_OVERRIDE DENDRA_KIT_UPDATE"
SLOT_NOT_CARRIED_KEYS="COMPOSE_PROFILES DENDRA_SLOT_RETIRED"

# write_slot_env <k> -> gpu/<k>/.env, derived from slot 0's .env as it stands after slot 0 started. 0600 from
# its creation (umask 077), in a 0700 directory, replaced in one rename. The identity requested is the one
# the file already names, else <pseudonym>-g<k>; the daemon then aligns it on the key, as for slot 0.
write_slot_env(){
  local k="$1" d="$MINER_KIT/gpu/$1" f t mid key v src="$MINER_KIT/.env"
  [ -f "$src" ] || { warn "slot $k: slot 0's env ($src) is missing, so there is nothing to derive slot $k from"; return 1; }
  ( umask 077; mkdir -p "$MINER_KIT/gpu" "$d" ) && chmod 700 "$MINER_KIT/gpu" "$d" || return 1
  f="$d/.env"
  mid=""; [ -f "$f" ] && mid="$(sed -n 's/^MINER_ID=//p' "$f" | head -1 | tr -d '\r[:space:]')"
  [ -n "$mid" ] || mid="${PSEUDO_ID}-g$k"
  t="$(umask 077; mktemp "$d/.env.XXXXXX")" || return 1
  {
    echo "# Generated by deploy/join.sh --gpus $(date -u +%FT%TZ): slot $k, one miner identity bound to one card."
    echo "# A machine file (it carries the relay token): never published. deploy/testnet-miner/slots.sh hands it to"
    echo "# compose with --env-file and one -f per entry of COMPOSE_FILE; the kit's own .env is slot 0's."
    echo "DENDRA_SLOT=$k"
    echo "DENDRA_GPU_UUID=${SLOT_UUID[$k]}"
    echo "COMPOSE_PROJECT_NAME=$(slot_project "$k")"
    echo "COMPOSE_FILE=$(gpu_slot_compose_files "$k")"
    echo "MINER_ID=$mid"
    echo "DENDRA_MODEL_ID=${SLOT_MODEL[$k]}"
    echo "DENDRA_MINER_OWNER="
    echo "DENDRA_MINER_JUDGE=$JUDGE"
    [ "$JUDGE" = 1 ] && echo "DENDRA_JUDGE_ENDPOINT=http://dendra-judge-cpu:11434"
    for key in $SLOT_CARRIED_KEYS; do
      grep -q "^$key=" "$src" || continue
      v="$(sed -n "s/^$key=//p" "$src" | head -1 | tr -d '\r')"
      printf '%s=%s\n' "$key" "$v"
    done
  } > "$t" || { rm -f "$t"; return 1; }
  chmod 600 "$t" && mv -f "$t" "$f"
}

# ensure_rig_network -> the dendra-rig network, which carries only the CPU judge of slot 0 (alias
# dendra-judge-cpu) and the miners of the slots k. Not dendra-chain: that one exists only while a node kit
# runs, belongs to the node's project, and would put the unauthenticated Ollama API in reach of the node's
# container. Not slot 0's default network either: there `ollama` would resolve to slot 0's card for a slot k
# miner as well. Created once, external everywhere, removed only by the uninstaller.
ensure_rig_network(){
  local ids id line wd kp
  if docker network inspect dendra-rig >/dev/null 2>&1; then
    kp="$(cd "$MINER_KIT" 2>/dev/null && pwd -P)"
    ids="$(docker network inspect dendra-rig --format '{{range $id, $c := .Containers}}{{$id}} {{end}}' 2>/dev/null)" \
      || die "the dendra-rig network exists but its members cannot be read: a judge alias there could be another kit's. Nothing was started."
    for id in $ids; do
      line="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{range .NetworkSettings.Networks}}{{range .Aliases}}{{.}} {{end}}{{end}}' "$id" 2>/dev/null | tr -d '\r')" \
        || die "a member of the dendra-rig network cannot be read. Nothing was started."
      wd="${line%%|*}"
      case " ${line#*|} " in *" dendra-judge-cpu "*)
        [ "$wd" = "$MINER_KIT" ] || [ "$wd" = "$kp" ] \
          || die "another kit ($wd) already serves the judge alias dendra-judge-cpu on dendra-rig: two judges under one name make every verdict ambiguous. Stop that kit's judge first. Nothing was started." ;;
      esac
    done
    say "  [OK] network dendra-rig present (it carries the shared CPU judge)"
    return 0
  fi
  docker network create --label com.dendranetwork.kit=miner-rig dendra-rig >/dev/null 2>&1 \
    || die "the dendra-rig network could not be created (docker network create dendra-rig): the slots name it, and compose would refuse them. Nothing was started."
  say "  [OK] network dendra-rig created (it carries the shared CPU judge, and nothing else)"
}

# ---------------------------------------------------------------- RAM, measured before each slot k
# _mem_available_mb -> MemAvailable of /proc/meminfo in MiB (under WSL, the VM's, set by .wslconfig); empty and
# exit 1 when it cannot be read -- never 0.
_mem_available_mb(){ cat /proc/meminfo 2>/dev/null | awk '$1 == "MemAvailable:" && $2 ~ /^[0-9]+$/ { print int($2 / 1024); f = 1; exit } END { if (!f) exit 1 }'; }
# _mem_to_mib <docker size> -> MiB rounded up, or `?` for a form it does not know.
_mem_to_mib(){
  printf '%s\n' "$1" | awk '{ v = $1
    if (!match(v, /^[0-9]+(\.[0-9]+)?/)) { print "?"; exit }
    n = substr(v, 1, RLENGTH) + 0; u = substr(v, RLENGTH + 1)
    if (u == "B") m = n / 1048576; else if (u == "KiB" || u == "kB" || u == "KB") m = n / 1024
    else if (u == "MiB" || u == "MB") m = n; else if (u == "GiB" || u == "GB") m = n * 1024
    else if (u == "TiB" || u == "TB") m = n * 1048576; else { print "?"; exit }
    printf "%d\n", (m == int(m)) ? m : int(m) + 1 }'
}
# model_residence <container> <engine url> <model|*> -> resident | absent | unread, read from the engine's
# /api/ps by the python3 of a miner container (JSON parsed, never searched). `*` = any model loaded.
# Inside a document that was read, a missing `models` is the zero value of a list: nothing loaded.
model_residence(){
  local out
  out="$(docker exec "$1" python3 -c 'import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1].rstrip("/") + "/api/ps", timeout=10) as r:
        d = json.load(r)
except Exception:
    print("unread"); raise SystemExit
if not isinstance(d, dict):
    print("unread"); raise SystemExit
ms = d.get("models", [])
if not isinstance(ms, list):
    print("unread"); raise SystemExit
names = set()
for m in ms:
    if isinstance(m, dict):
        names.update(str(m.get(k) or "") for k in ("name", "model"))
names.discard("")
want = sys.argv[2]
if want == "*":
    print("resident" if names else "absent")
else:
    alts = {want, want + ":latest"} if ":" not in want else {want}
    print("resident" if names & alts else "absent")' "$2" "$3" 2>/dev/null)"
  case "$out" in resident|absent) printf '%s\n' "$out" ;; *) printf 'unread\n' ;; esac
}
# measure_slot_footprint -> SLOT_FOOTPRINT_MB: what one slot takes in RAM, measured on slot 0 with its mining
# model resident (loaded on request when it is not: the first self-test may not have loaded it). Its engine and
# its miner, never the judge (counted once per machine, below). `?` when anything could not be read.
# DENDRA_SLOT_FOOTPRINT_MB=<n> declares it instead, and the output says so.
# Bounded from below: a container whose model is resident cannot weigh 0. docker stats prints `0B / 0B` when
# it cannot read the memory controller of the host -- a 0 produced by a tool that fails, never a measure --
# and a footprint of 0 would let every slot k through the RAM check. Declared or measured, 0 is `?`.
SLOT_FOOTPRINT_MB=""; SLOT_FOOTPRINT_WHY=""
measure_slot_footprint(){
  local mc oc res out c mu mb total=0 n=0
  if [ -n "${DENDRA_SLOT_FOOTPRINT_MB:-}" ]; then
    case "$DENDRA_SLOT_FOOTPRINT_MB" in *[!0-9]*) SLOT_FOOTPRINT_MB="?"; SLOT_FOOTPRINT_WHY="DENDRA_SLOT_FOOTPRINT_MB is not a number of MiB"; return 0 ;; esac
    [ "$DENDRA_SLOT_FOOTPRINT_MB" -gt 0 ] 2>/dev/null || { SLOT_FOOTPRINT_MB="?"; SLOT_FOOTPRINT_WHY="DENDRA_SLOT_FOOTPRINT_MB declares 0 MiB, and no slot takes nothing"; return 0; }
    SLOT_FOOTPRINT_MB="$DENDRA_SLOT_FOOTPRINT_MB"; say "  [i] footprint of one slot declared by DENDRA_SLOT_FOOTPRINT_MB: ${SLOT_FOOTPRINT_MB} MiB (not measured)"; return 0
  fi
  SLOT_FOOTPRINT_MB="?"
  mc="$(slot_cid 0 miner --running)" && oc="$(slot_cid 0 ollama --running)" || { SLOT_FOOTPRINT_WHY="docker did not answer"; return 0; }
  [ -n "$mc" ] && [ -n "$oc" ] || { SLOT_FOOTPRINT_WHY="slot 0's engine or miner is not running"; return 0; }
  res="$(model_residence "$mc" http://ollama:11434 "$DENDRA_MODEL_ID")"
  if [ "$res" = absent ]; then
    say "  [i] slot 0's model is not loaded: loading it, to measure what a slot takes (keep_alive 1h, no prompt)"
    docker exec "$mc" python3 -c 'import json, sys, urllib.request
req = urllib.request.Request(sys.argv[1] + "/api/generate", data=json.dumps({"model": sys.argv[2], "keep_alive": "1h"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
urllib.request.urlopen(req, timeout=600).read()' http://ollama:11434 "$DENDRA_MODEL_ID" >/dev/null 2>&1
    res="$(model_residence "$mc" http://ollama:11434 "$DENDRA_MODEL_ID")"
  fi
  [ "$res" = resident ] || { SLOT_FOOTPRINT_WHY="slot 0's model ($DENDRA_MODEL_ID) is $res in its engine after a load request"; return 0; }
  out="$(docker stats --no-stream --format '{{.Container}}|{{.MemUsage}}' "$oc" "$mc" 2>/dev/null)" || { SLOT_FOOTPRINT_WHY="docker stats did not answer"; return 0; }
  while IFS='|' read -r c mu; do
    [ -n "$c" ] || continue
    mb="$(_mem_to_mib "${mu%% /*}")"
    [ "$mb" != "?" ] || { SLOT_FOOTPRINT_WHY="docker stats printed a memory figure this kit cannot read ($mu)"; return 0; }
    [ "$mb" -gt 0 ] || { SLOT_FOOTPRINT_WHY="docker stats reports 0 for a running container ($mu): it does not measure memory on this host"; return 0; }
    total=$((total + mb)); n=$((n + 1))
  done <<EOF
$out
EOF
  [ "$n" = 2 ] || { SLOT_FOOTPRINT_WHY="docker stats did not report both of slot 0's containers"; return 0; }
  SLOT_FOOTPRINT_MB="$total"
  say "  [OK] footprint of one slot, measured on slot 0 with its model loaded: ${SLOT_FOOTPRINT_MB} MiB (engine + miner)"
}
# rig_ram_allows_slot -> RAM_VERDICT = yes | no | unknown, the reason in RAM_WHY (globals: called directly, never
# in a $( ) subshell, which would lose the reason). MemAvailable, minus a reserve kept for
# the system (DENDRA_RIG_RESERVE_MB: its default is a choice, not a measure -- room for the desktop, Docker and
# the node), minus the judge's floor while the judge's model is not loaded (once loaded it is already outside
# MemAvailable), must cover one slot's footprint. An unread figure is never 0: it is unknown, and the slot does
# not start.
RAM_WHY=""; RAM_VERDICT=""
rig_ram_allows_slot(){
  local avail reserve="${DENDRA_RIG_RESERVE_MB:-2048}" judge=0 res mc left
  RAM_VERDICT=unknown
  case "$SLOT_FOOTPRINT_MB" in ""|"?"|*[!0-9]*) RAM_WHY="the footprint of one slot could not be measured (${SLOT_FOOTPRINT_WHY:-not measured})"; return 0 ;; esac
  [ "$SLOT_FOOTPRINT_MB" -gt 0 ] || { RAM_WHY="a footprint of 0 MiB is not a measure: no slot takes nothing"; return 0; }
  case "$reserve" in ""|*[!0-9]*) RAM_WHY="DENDRA_RIG_RESERVE_MB is not a number of MiB"; return 0 ;; esac
  avail="$(_mem_available_mb)" || { RAM_WHY="MemAvailable could not be read in /proc/meminfo"; return 0; }
  if [ "$JUDGE" = 1 ]; then
    mc="$(slot_cid 0 miner --running)"
    res="unread"; [ -n "$mc" ] && res="$(model_residence "$mc" http://ollama-cpu:11434 '*')"
    case "$res" in
      resident) judge=0 ;;
      absent) judge="$(tr -d '\r' < "$HW_PROBE" | bash -s -- --judge-floor-mb 2>/dev/null | tail -1)"
              case "$judge" in ""|*[!0-9]*) RAM_WHY="the judge's RAM floor could not be read (deploy/hw_probe.sh --judge-floor-mb)"; return 0 ;; esac ;;
      *) RAM_WHY="whether the judge's model is loaded could not be read (slot 0's /api/ps on ollama-cpu)"; return 0 ;;
    esac
  fi
  left=$((avail - reserve - judge))
  RAM_WHY="MemAvailable ${avail} MiB - reserve ${reserve} MiB - judge ${judge} MiB = ${left} MiB, one slot takes ${SLOT_FOOTPRINT_MB} MiB"
  if [ "$left" -ge "$SLOT_FOOTPRINT_MB" ]; then RAM_VERDICT=yes; else RAM_VERDICT=no; fi
}

# wait_registration_terminal <k> -> SLOT_REG[k] = registered | refused | deferred:<reason> | mismatch | unread, read from
# the daemon's heartbeat in slot k's miner (slots.sh slot_registration), never from its log. First the miner has
# to run: its model may be pulled first (DENDRA_SLOT_START_BOUND_S, a chosen bound). Then the registration
# has to reach a terminal state within a bound derived from the slot's faucet proof-of-work bound
# (DENDRA_FAUCET_POW_MAX_S, else the default docker-compose.yml gives it) plus DENDRA_REGISTER_WAIT_EXTRA_S (a
# chosen margin: the credit to land and create-miner to be confirmed).
SLOT_REG=(); SLOT_REG_WHY=""
wait_registration_terminal(){
  local k="$1" pow extra="${DENDRA_REGISTER_WAIT_EXTRA_S:-300}" start="${DENDRA_SLOT_START_BOUND_S:-1800}" step=10 n i cid="" st="unread"
  SLOT_REG[$k]=unread; SLOT_REG_WHY=""
  pow="$(slot_val "$k" DENDRA_FAUCET_POW_MAX_S 2>/dev/null)"
  [ -n "$pow" ] || pow="$(sed -n 's/.*DENDRA_FAUCET_POW_MAX_S:-\([0-9][0-9.]*\)}.*/\1/p' "$MINER_KIT/docker-compose.yml" | head -1)"
  pow="${pow%%.*}"
  case "$pow$extra$start" in ""|*[!0-9]*) SLOT_REG_WHY="a wait bound of slot $k could not be read"; return 0 ;; esac
  n=$((start / step)); [ "$n" -ge 1 ] || n=1
  for i in $(seq 1 "$n"); do
    cid="$(slot_cid "$k" miner --running 2>/dev/null)"
    [ -n "$cid" ] && break
    _sync_nap "$step"
  done
  [ -n "$cid" ] || { SLOT_REG_WHY="the miner of slot $k was not running within ${start} s (its model may still be downloading)"; return 0; }
  n=$(((pow + extra) / step)); [ "$n" -ge 1 ] || n=1
  for i in $(seq 1 "$n"); do
    st="$(slot_registration "$k")"
    case "$st" in registered|refused|deferred:*|mismatch) SLOT_REG[$k]="$st"; return 0 ;; esac
    _sync_nap "$step"
  done
  SLOT_REG_WHY="no terminal registration within $((pow + extra)) s (last read: $st)"
}

# retire_slot <k> -> its card left --gpus: stopped (never down, never -v: the volume holds a staked key) and
# marked retired in its env. It keeps its identity, stake and keys, and stays registered on chain.
retire_slot(){
  local k="$1"
  say "  [i] slot $k ($(slot_project "$k"), card ${SLOT_UUID[$k]}) is no longer in --gpus: it is stopped, not removed."
  slot_compose "$k" stop >/dev/null 2>&1 || warn "slot $k could not be stopped: bash $MINER_KIT/slots.sh run $k stop"
  slot_env_set "$k" DENDRA_SLOT_RETIRED "$(date -u +%FT%TZ)" || warn "slot $k could not be marked retired in $(slot_env "$k")"
  say "      It keeps its identity, its stake and its keys (volume $(slot_project "$k")_miner-keys) and stays registered: drawn"
  say "      as a juror while it is fresh, it never votes. To leave the network with it, its stake returned:"
  say "        bash $MINER_KIT/exit-miner.sh --slot $k"
  say "      Adding the same card back later starts the same identity again."
}

# start_gpu_slots -> after slot 0 is healthy and its first self-test ran: retirements, then each slot k in order,
# each only if the RAM is measured to allow it and the previous one reached a terminal registration. The first
# IP-quota refusal of the faucet stops the sequence: the next identities would be refused the same way today.
SLOT_RESULT=()
start_gpu_slots(){
  local k st stop="" img dimg v resume
  # The command that resumes is this run's, never `--gpus all`: `all` would select cards this run left out,
  # and a re-run without --gpus keeps exactly the slots that are active. Printed on every early stop below.
  resume="bash $REPO/deploy/join.sh${GPUS_SPEC:+ --gpus $GPUS_SPEC}"
  for k in $SLOT_KS; do
    [ "$k" = 0 ] && continue
    case "${SLOT_STATE[$k]}" in
      retire) retire_slot "$k"; SLOT_RESULT[$k]="retired (stopped)" ;;
      retired) SLOT_RESULT[$k]="retired"; _retired_card_note "$k" ;;
      # Its card is gone: the slot is stopped (never down, never -v), not retired, not re-bound. Left running,
      # `restart: unless-stopped` would keep it alive, silently, in env-only mode on the CPU (an unknown
      # CUDA_VISIBLE_DEVICES is no error to the engine).
      card-missing) SLOT_RESULT[$k]="card missing: stopped"
        say "  [!] slot $k ($(slot_project "$k")): its card ${SLOT_UUID[$k]} is not on this host now. It is stopped; not retired, not re-bound."
        slot_compose "$k" stop >/dev/null 2>&1 || { SLOT_RESULT[$k]="card missing: not stopped"; warn "slot $k could not be stopped: bash $MINER_KIT/slots.sh run $k stop"; }
        say "      To bind its identity to a present card: bash $REPO/deploy/join.sh${GPUS_SPEC:+ --gpus $GPUS_SPEC} --reassign $k=<card index>" ;;
    esac
  done
  img="$(sed -n 's/^DENDRA_MINER_IMAGE=//p' "$MINER_KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  dimg="$(sed -n 's/^ *image: \${DENDRA_MINER_IMAGE:-\([^}]*\)}.*/\1/p' "$MINER_KIT/docker-compose.yml" | head -1)"
  [ -n "$img" ] || img="$dimg"
  measure_slot_footprint
  for k in $SLOT_KS; do
    [ "$k" = 0 ] && continue
    case "${SLOT_STATE[$k]}" in keep|new) : ;; *) continue ;; esac
    if [ -n "$stop" ]; then SLOT_RESULT[$k]="not started: $stop"; continue; fi
    say "== [join] slot $k: $(slot_project "$k"), card ${SLOT_UUID[$k]}, model ${SLOT_MODEL[$k]} =="
    # A slot whose miner runs already holds its memory: it is outside MemAvailable, and counting it again would
    # refuse, on a re-run, a slot that is running fine. The check is for a slot that starts. A docker that
    # does not answer is not "not running": the check applies.
    v=""
    if [ "${SLOT_STATE[$k]}" = keep ] && [ -n "$(slot_cid "$k" miner --running 2>/dev/null)" ]; then
      v=yes; say "  [i] slot $k is running: its memory is already taken, the RAM check is for a slot that starts"
    else
      rig_ram_allows_slot; v="$RAM_VERDICT"
    fi
    if [ "$v" != yes ]; then
      stop="RAM $v: $RAM_WHY"; SLOT_RESULT[$k]="not started: $stop"
      say "  [!] slot $k not started -- RAM $v: $RAM_WHY."
      say "      Free memory and re-run: $resume   (or declare DENDRA_SLOT_FOOTPRINT_MB / DENDRA_RIG_RESERVE_MB)"
      continue
    fi
    write_slot_env "$k" || die "slot $k: its env could not be written in $MINER_KIT/gpu/$k"
    write_slot_override "$k" "${SLOT_UUID[$k]}" || die "slot $k: its override could not be written in $MINER_KIT/gpu/$k"
    if [ -z "$img" ] || ! docker image inspect "$img" >/dev/null 2>&1; then
      stop="the image slot 0 started (${img:-unread}) is not on this machine"; SLOT_RESULT[$k]="not started: $stop"
      say "  [!] slot $k not started: $stop. Slots k never pull nor build: they start what slot 0 started."; continue
    fi
    # A slot started again after exit-miner.sh --slot registers again: the exit that file recorded no longer
    # holds, and its marker would keep the hourly self-test of this identity quiet (as for slot 0, below).
    if [ -f "$MINER_KIT/gpu/$k/miner-health.EXITED" ]; then
      rm -f "$MINER_KIT/gpu/$k/miner-health.EXITED"
      say "  [i] slot $k had left the network (exit-miner.sh --slot $k); starting it registers it again."
    fi
    if ! slot_compose "$k" up -d --no-build; then
      stop="slot $k did not start (see above)"; SLOT_RESULT[$k]="not started: compose refused"; continue
    fi
    wait_registration_terminal "$k"
    st="${SLOT_REG[$k]}"; SLOT_RESULT[$k]="started, registration: $st"
    case "$st" in
      registered) say "  [OK] slot $k registered" ;;
      deferred:ip_quota|deferred:global_cap)
        stop="the faucet refused slot $k (${st#deferred:}): the next identities would be refused the same way today"
        say "  [!] slot $k is running and not registered yet: the faucet refused it (${st#deferred:}). It retries by itself."
        say "      The next slots are not started today. To start them later (running slots are left as they are):"
        say "        $resume" ;;
      deferred:draining) say "  [!] slot $k is running, not registered, and draining (the file drain in its key volume, set by exit-miner.sh):"
                         say "      it registers nothing while the file is there. To have it register: bash $MINER_KIT/exit-miner.sh --slot $k --undrain" ;;
      deferred:stale_kit)
        # The faucet refused the kit as below the network's floor (faucet.py::H.do_POST):
        # the daemon retries, and every retry is refused the same way until the kit is updated, so the update is
        # named here rather than the retry. The next slots run the same kit and would be refused the same way.
        stop="the faucet refused slot $k (stale_kit): this kit is below the network's floor, and the next slots run the same kit"
        say "  [!] slot $k is running and not registered: the faucet refused it, this kit is below the network's floor (stale_kit)."
        say "      Retrying changes nothing until the kit is updated. Update: $(kit_update_hint)"
        say "      Its log names the floor: bash $MINER_KIT/slots.sh run $k logs miner" ;;
      deferred:*|refused) say "  [!] slot $k is running and not registered ($st): it retries by itself; its log: bash $MINER_KIT/slots.sh run $k logs miner" ;;
      mismatch) say "  [!] slot $k is running, and the chain records another operator for its identity (IDENTITY MISMATCH): every"
                say "      commit it sends is refused. The two ways out are in its log: bash $MINER_KIT/slots.sh run $k logs miner" ;;
      *) stop="the registration of slot $k could not be read ($SLOT_REG_WHY)"
         say "  [!] slot $k: $stop. The next slots are not started. Re-run when it is readable: $resume" ;;
    esac
  done
  publish_capacity_signed
  return 0
}

# print_status_slots -> one line per slot, with its card, model, identity, registration and pinning, and the
# commands of each slot through slots.sh (a slot's command written out with its -f flags would be one a
# reader could shorten to `-f docker-compose.yml up` -- the form that recreates a miner blind to its node).
print_status_slots(){
  local k i card mid pin
  say "  One identity per card ($N_RUN running on this machine; deploy/testnet-miner/slots.sh list shows them):"
  printf '  %-4s %-18s %-34s %-34s %-24s %s\n' slot project card model identity "registration | pinning"
  for k in $SLOT_KS; do
    i="$(_gl_of_uuid "${SLOT_UUID[$k]}")" && card="${GL_IDX[$i]}: ${GL_NAME[$i]}" || card="${SLOT_UUID[$k]} (absent)"
    mid="$(slot_val "$k" MINER_ID 2>/dev/null)"
    pin="-"; case "${SLOT_STATE[$k]}" in keep|new) pin="$(slot_pin "$k" 2>/dev/null)"; pin="${pin%%|*} (${pin#*|})" ;; esac
    printf '  %-4s %-18s %-34s %-34s %-24s %s | %s\n' "$k" "$(slot_project "$k")" "$card" "${SLOT_MODEL[$k]:--}" "${mid:--}" \
      "$( [ "$k" = 0 ] && slot_registration 0 2>/dev/null || printf '%s' "${SLOT_RESULT[$k]:-${SLOT_STATE[$k]}}")" "$pin"
  done
  if [ "$JUDGE" = 1 ]; then say "  Judge         : shared CPU judge on slot 0 (dendra-judge-cpu, network dendra-rig): every identity votes through it"
  else say "  Judge         : none -- $N_RUN identities cannot judge: $N_RUN mute jury seats (drawn as jurors, never voting)"; fi
  say "  Per slot      : bash $MINER_KIT/slots.sh run <k> logs -f miner      bash $MINER_KIT/slots.sh run <k> up -d --no-build"
  say "                  bash $MINER_KIT/slots.sh run <k> stop               bash $MINER_KIT/slots.sh pin <k>"
  say "                  bash $MINER_KIT/miner_health.sh --slot <k>          (without --slot: every slot, one verdict)"
  say "                  bash $MINER_KIT/exit-miner.sh --slot <k>            (leave the network with that identity only)"
  say "                  bash $MINER_KIT/encrypt-keys.sh --slot <k> | --all"
  say "  Keys          : one keyring and one 24-word recovery phrase per identity (volume dendra-miner-g<k>_miner-keys),"
  say "                  under this machine's one passphrase file: back up every volume and that one file."
}

# ---------------------------------------------------------------- capacity publication
# `hw_probe.sh` produces the capacity JSON; without a publisher nobody sends it, the `/capacity` registry
# never receives any data, and the public /network page shows zero everywhere for everyone. A running
# service, a page that displays it, and no producer: the failure is invisible because "0 nodes" is a
# perfectly plausible result on a young network.
# One publisher, and it signs: deploy/testnet-miner/publish-capacity.sh. It sends a report only once it is
# signed and the chain records this identity with the signing key as its operator, the one report the
# registry can prove. This script never posts a report of its own: an unsigned one, sent at install time
# before the miner existed on chain, was filed under another key than the signed one (capacity_server.py::
# storage_key) and listed the same machine twice. So this script schedules the publisher (schedule_capacity,
# when the containers start) and runs it at once when the registration is seen (capacity_after_join).
# The public capacity registry URL, derived once and shared by everything that publishes.
#
# It must be derived from the shell `DENDRA_NODE`, never from the kit's .env. In the default setup this
# script writes a container-local value into the kit (`DENDRA_NODE=tcp://dendra-node:26657`, the node's
# alias on the dendra-chain network, see MINER_RPC below): correct for the miner container, useless as a
# public endpoint. The shell variable keeps the operator's public RPC throughout (comment at the MINER_RPC
# block). Two producers deriving the same way from two different inputs is how the re-publisher once
# posted into the void while this script's own first publication looked green.
# And the derivation cannot work on a bare IP, which is what this network publishes. `DENDRA_NODE` in the
# operator's network-info.txt is `tcp://<IP>:26657`, so the line below builds `https://<IP>/capacity`, and
# TLS on a bare IP presents a certificate issued for a name, so the handshake fails before any route is
# consulted. Measured: `https://<IP>/capacity` returns 000 (TLS connect error) while
# `https://api.dendranetwork.com/capacity` returns 200 on the same address. The failure is silent: every
# scheduled publication fails at the handshake, and the joiner never appears on /network, which on a young
# network still reads as a plausible node count.
# So a host that is a bare IP yields no URL, and the caller says what to do rather than posting into the
# void. `DENDRA_CAPACITY_URL` (allow-listed above) is the operator's way to publish the real one.
capacity_url(){
  local u host
  u="${DENDRA_CAPACITY_URL:-}"
  if [ -z "$u" ] && [ -n "${DENDRA_NODE:-}" ]; then
    # The path is stripped before the port, and the order matters.
    # `s#:[0-9]+$##` only applies when the port ends the string. Run before the path removal, it never
    # saw a port followed by anything, so on `tcp://<IP>:26657/` (a form the CONFIG_URL filter accepts,
    # `/` being an allowed character) the port survived into `host`, its colon made the host look like a
    # name to the test below, and the bare IP this block exists to refuse was derived anyway, with the
    # RPC port attached. Measured on the shipped function: `tcp://<IP>:26657/` gave
    # `https://<IP>:26657/capacity` while the same address without the trailing slash gave nothing.
    host="$(printf '%s' "$DENDRA_NODE" | sed -E 's#^[A-Za-z]+://##; s#/.*$##; s#:[0-9]+$##')"
    # The test asks what TLS asks: is this host a name? A certificate is issued for a name, so only a
    # name can present one. Asking "does it look like an IPv4" let through every other shape that cannot
    # carry TLS either (an IPv6 literal, a leftover port, a path remnant, a stray space). The default is
    # inverted on purpose: a host that is not recognisably a name yields no URL, rather than a URL that
    # fails at handshake time and breaks the hourly re-publication.
    case "$host" in
      *[!A-Za-z0-9.-]*) u="" ;;   # a DNS name cannot hold this character, so this is not a name
      *[A-Za-z]*)       u="https://$host/capacity" ;;
      *)                u="" ;;   # digits and dots only = bare IPv4; see the block above for why
    esac
  fi
  printf '%s' "$u"
}

# cron_add_line <marker> <line> <what> [<detail>]: one writer for the three hourly jobs of this file. The
# crontab is read, compared, then written whole, and an unread table is never written. Piping
# `crontab -l 2>/dev/null` into `crontab -` does not do that: a table that fails to read for any reason other
# than "no crontab" (a permission, a broken spool) goes in as nothing, and `crontab -` replaces every job of
# the operator's own with one line. Three answers to the read: a table, an account with none (an empty
# table), or unread, and unread writes nothing and says what to add. <marker> is the fixed string whose
# presence means the job is already there. 0 when the line is in the table (already, or written now), 1 when
# it is not, and then the line to add is printed.
cron_add_line(){
  local mark="$1" line="$2" what="$3" detail="${4:-}" d
  d="$(mktemp -d)" || { say "  [!] no temporary directory: the crontab is left as it is. Add this line yourself:  $line"; return 1; }
  if crontab -l > "$d/tab" 2> "$d/err"; then :
  elif grep -qi 'no crontab' "$d/err" 2>/dev/null; then : > "$d/tab"
  else
    say "  [!] the crontab could not be read, so it is not rewritten (writing it back replaces it whole)."
    say "      Add this line yourself:  $line"
    rm -rf "$d"; return 1
  fi
  if grep -qF -- "$mark" "$d/tab"; then
    say "  [OK] $what already scheduled (crontab)"
    rm -rf "$d"; return 0
  fi
  cp "$d/tab" "$d/new"
  [ -s "$d/new" ] && [ -n "$(tail -c1 "$d/new")" ] && printf '\n' >> "$d/new"
  printf '%s\n' "$line" >> "$d/new"
  if crontab "$d/new" 2>/dev/null; then
    say "  [OK] $what scheduled (crontab${detail:+, $detail})"
    rm -rf "$d"; return 0
  fi
  say "  [!] could not write the crontab. Add this line yourself:"
  say "        $line"
  rm -rf "$d"; return 1
}

# The report expires. Publishing once at install time is a countdown: the registry ages a report out after
# 24 h and the node leaves the capacity listing that /network shows, days later, for a cause nobody will
# connect to this install. (The public chat does not count these reports: it reads `onchain.present`, the
# registered miners with a presence proof, which the registry service reads from the chain; only a service
# without that block falls back to `verified.live_nodes`.) So the repetition is set up here, while the
# operator is present, rather than described in a README.
install_capacity_cron(){
  local script="$REPO/deploy/testnet-miner/publish-capacity.sh"
  local CAP_LOG="${DENDRA_CAPACITY_LOG:-$MINER_KIT/publish-capacity.log}"
  [ -f "$script" ] || return 0
  command -v crontab >/dev/null 2>&1 || {
    say "  [!] no crontab on this host -- schedule this yourself, or the node drops off /network's capacity listing in 24 h:"
    say "        bash $script"
    return 0
  }
  # `bash -lc`, not plain `bash`. cron runs with a minimal PATH, and the hardware probe resolves
  # `nvidia-smi` through PATH; under WSL it lives in /usr/lib/wsl/lib, which a non-login shell does not
  # carry. Without the login shell the probe finds no GPU, publishes a CPU-tier inventory with a smaller
  # model than this miner serves, and the page shows that as the operator's own declaration.
  # stderr is kept, not discarded, so a refusal can be read.
  cron_add_line "publish-capacity.sh" "17 * * * * bash -lc 'bash $script' >> $CAP_LOG 2>&1" \
      "capacity re-publication" "hourly, minute 17 -- log: $CAP_LOG" \
    || say "      Without it the registry drops this node from /network's capacity listing after 24 h (keep the -l: without a login shell the probe sees no GPU and publishes a false tier)."
}

# ---------------------------------------------------------------- validator jail watch
# A validator operator cannot see from the outside that they are jailed.
# The only health endpoint published to operators so far, `committee_seed_health`, returns a contributor
# count and the floor it must reach, and nothing about jails, slashes, or which validators are expected.
# A validator that drops out of the consensus set looks, through that endpoint, exactly like a
# vote-extension failure: the number falls, and the cause is not in the answer. Everything below exists
# so the cause has a name, on the operator's own machine, in one command.
VH_SCRIPT="$REPO/deploy/validator_health.sh"

# Cron: hourly, and silent while healthy. The reasoning, because the cadence is not obvious:
# The remedy for a jail is a signed transaction, so no schedule can prevent one: by the time any check
# fires, the slash has already been taken (the chain applies it within the window that produced it).
# What a schedule buys is the end of the outage, the hours or days spent out of the set because nobody
# looked. That is human-paced, so hourly is enough; a tighter cadence would poll a public endpoint
# harder without shortening a human reaction.
# The output policy matters more than the cadence: a log that grows every hour is one more file nobody
# reads. So the check writes nothing while healthy and deletes any previous alert: the file exists if
# and only if there is a problem right now, and it holds the whole current report. "Does this file
# exist" stays answerable at a glance, months later, by someone who has forgotten this install.
# Only the validator role gets it: a miner has no validator to be jailed.
install_health_cron(){
  local alert="$NODE_KIT/validator-health.ALERT"
  local log_cmd="bash $VH_SCRIPT --cron $alert"
  [ -n "${DENDRA_NODE:-}" ] && log_cmd="DENDRA_NODE=$DENDRA_NODE $log_cmd"
  say ""
  say "  Alert file : $alert"
  say "               It does not exist while everything is fine. If it appears, your validator has a"
  say "               problem and the file contains the full report. Nothing else to watch."
  command -v crontab >/dev/null 2>&1 || {
    say "  [!] no crontab on this host — schedule this yourself, or a jail can sit unnoticed for days:"
    say "        $log_cmd"
    return 0
  }
  # `bash -lc` for the same reason as the capacity cron: cron runs with a minimal PATH and this check
  # needs curl and python3/jq resolved the way the operator's shell resolves them.
  cron_add_line "validator_health.sh" "43 * * * * bash -lc '$log_cmd'" "hourly jail watch" "minute 43" \
    || say "      Without it a jail can sit unnoticed for days."
}

# ---------------------------------------------------------------- the miner's own watch
# A miner can look healthy and earn nothing: containers up and logs scrolling, while its node has stopped
# following the chain, the chain anchors a key it does not hold, its reveal worker died, or its model no
# longer answers. deploy/testnet-miner/miner_health.sh asks those questions; this schedules it, hourly,
# with the jail watch's output policy: the alert file exists if and only if something is wrong, and holds
# the report; miner-health.last.json next to it is rewritten on every run (the Dendra application shows
# it, with its age). Minute 29, apart from the capacity job (17) and the jail watch (43): three jobs that
# never queue behind one another. `bash -lc` for the reason the capacity job gives: cron's PATH is minimal.
# The crontab is read before it is written, and an unread one is never written: `crontab <file>` replaces
# the whole table, so a table that could not be read would be replaced by this one line (cron_add_line).
MINER_HEALTH_MINUTE=29
install_miner_health_cron(){
  local script="$MINER_KIT/miner_health.sh" alert="$MINER_KIT/miner-health.ALERT" line
  [ -f "$script" ] || return 0
  line="$MINER_HEALTH_MINUTE * * * * bash -lc 'bash $script --cron $alert'"
  say ""
  say "  Miner alert file : $alert"
  say "                     It does not exist while every check passes. If it appears, it holds the report."
  case "$script$alert" in
    *[!A-Za-z0-9/._-]*)
      say "  [!] this clone's path holds characters a crontab line cannot carry safely. Schedule it yourself:"
      say "        $line" ;;
    *)
      if ! command -v crontab >/dev/null 2>&1; then
        say "  [!] no crontab on this host -- schedule this yourself, hourly, or nothing will notice a problem:"
        say "        $line"
      else
        cron_add_line "$script --cron" "$line" "hourly miner self-test" "minute $MINER_HEALTH_MINUTE"
      fi ;;
  esac
  say ""
  say "== [join] first miner self-test (the run the hourly job makes; it prints, and changes nothing on this host) =="
  bash "$script" 2>&1 | sed 's/^/  | /'
  return 0
}

# The numbers an operator needs here (how many blocks may be missed, what that is in minutes, what the
# slash costs) are not written into this script. They are governed parameters: any copy of them would
# be true until the first parameter change and wrong afterwards, with nothing to signal the change. So
# the command is run, once, and its output is what the operator reads.
# _jail_params_cmds: the two ways to read the downtime parameters off the chain. This function prints
# commands, never numbers.
# The REST guess is derived from DENDRA_NODE's host (the API listener is 1317 next to the RPC's 26657);
# it is a candidate, labelled as such, not an assertion about the operator's deployment.
_jail_params_cmds(){
  local host rest
  if [ -n "${DENDRA_REST:-}" ]; then rest="${DENDRA_REST%/}"
  elif [ -n "${DENDRA_NODE:-}" ]; then host="${DENDRA_NODE#*://}"; host="${host%%:*}"; rest="http://$host:1317"
  else rest=""; fi
  say "      docker compose -f $NODE_KIT/docker-compose.yml exec -T node dendrad query slashing params -o json"
  say "                                            # once your node is up — always available"
  [ -n "$rest" ] && say "      curl -s $rest/cosmos/slashing/v1beta1/params   # from any machine, if the operator exposes the API"
}

validator_health_notice(){
  # This branch exists for clones without the health script. A one-line "missing, no jail watch
  # installed" would leave the operator with a warning and no way to act on it, so the fallback hands
  # over the chain reads the script would have made.
  [ -f "$VH_SCRIPT" ] || {
    warn "deploy/validator_health.sh is not in this checkout — no automatic jail watch can be installed."
    say  "      Read your downtime exposure off the chain yourself, and put a reminder in your own"
    say  "      monitoring: a jailed validator keeps running and stops earning, with no local symptom."
    _jail_params_cmds
    say  "      signed_blocks_window minus round(signed_blocks_window x min_signed_per_window) is how"
    say  "      many blocks you may miss inside one window; the one after that jails you. The product"
    say  "      alone is how many you must sign, which is a different number as soon as the ratio"
    say  "      leaves 0.5 — cosmos-sdk x/slashing/keeper/infractions.go, maxMissed := signedBlocksWindow"
    say  "      - MinSignedPerWindow(), jailing when MissedBlocksCounter > maxMissed."
    say  "      slash_fraction_downtime is what a jail costs; downtime_jail_duration is how long it lasts."
    return 0
  }
  say ""
  say "  =================================================================="
  say "  Being jailed is the default outcome of unplanned downtime."
  say "  Miss enough consecutive blocks and the chain jails your validator and takes a percentage of"
  say "  your stake — any outage that outlasts the window is enough. Nothing warns you:"
  say "  your node keeps running, it simply stops being in the consensus set. The exact figures for"
  say "  this chain are printed below, read from the chain itself rather than repeated here:"
  say ""
  say "      bash $VH_SCRIPT                     # identifies your validator from this node"
  say "      bash $VH_SCRIPT dendravaloper1...   # or name it explicitly, from any machine"
  say "  =================================================================="
  # CRLF-safe invocation (the runbooks pipe scripts through `tr -d '\r'`), and DENDRA_HEALTH_CMD so the
  # commands it prints name the repository path rather than the temporary one it is running from.
  local _t; _t="$(mktemp)"
  tr -d '\r' < "$VH_SCRIPT" > "$_t" 2>/dev/null
  DENDRA_HEALTH_CMD="bash $VH_SCRIPT" bash "$_t" 2>&1 | sed 's/^/  | /'
  rm -f "$_t"
  say ""
  say "  A validator has not bonded yet at this point, so the report above says so and stops there."
  say "  Re-run it after create-validator: it then names your validator, your jail state and your margin."
  install_health_cron
}

# schedule_capacity, when the containers start: nothing is posted here. The hourly publisher is scheduled
# (install_capacity_cron); it sends a report once it is signed and the chain records this identity with the
# signing key as its operator, so the first report goes out at its first run after the registration, unless
# capacity_after_join sends it at once. A registry URL that cannot be derived is reported here, while the
# operator is present: every scheduled run would refuse it.
schedule_capacity(){
  if [ -z "$(capacity_url)" ]; then
    # Silence here is what kept every joiner off /network. Say it, and name the one fix.
    warn "capacity registry URL unknown -> this node will not appear on the /network page."
    say  "       Everything else works: this only affects the public capacity display."
    say  "       The address was not derivable because the network publishes a bare IP for the RPC, and"
    say  "       HTTPS on an IP cannot match a certificate issued for a name."
    say  "       Fix: ask the operator to add DENDRA_CAPACITY_URL=<https://name/capacity> to the file"
    say  "            named by CONFIG_URL, or export it yourself before running this script."
  fi
  say "  [i] capacity report: signed, and sent only once the chain records this miner (deploy/testnet-miner/publish-capacity.sh)."
  install_capacity_cron
  return 0
}

# The registry's verified block counts only signed reports, and the hourly job would send the first one up to an
# hour after the registration. So once the daemon has said it is registered, the signing publisher runs here, at
# once. It resolves the identity the chain knows (`dm1…`), reads its registration and signs inside the miner
# container where the keyring lives, and reads the registry back. Its four answers are reported separately; none fails the
# join: the hourly job asks again. The public chat counts miners from the chain (`onchain.present`), not
# from these reports: see install_capacity_cron.
publish_capacity_signed(){
  local script="$REPO/deploy/testnet-miner/publish-capacity.sh" rc
  [ -f "$script" ] || return 0
  say "== [join] signed capacity report (this machine's line in the registry /network shows) =="
  bash "$script"; rc=$?
  case "$rc" in
    0) : ;;
    2) say "  [i] capacity report not sent yet: the chain does not record this identity with this machine's key as its"
       say "      operator (see above). The hourly job sends it at its first run after the registration." ;;
    3) warn "capacity report not sent: the registration could not be read where the report is signed (see above); the hourly job asks again." ;;
    *) warn "the signed capacity report did not go through (exit $rc, see above); the hourly job retries it (crontab)." ;;
  esac
  return 0
}

# Called once wait_healthy has returned: sign now if the daemon said it is registered, otherwise say who
# will do it, since publish-capacity.sh sends nothing before the chain records the miner.
capacity_after_join(){
  # One identity per card: publish-capacity.sh signs one report per slot, so it runs once, after every slot
  # has started (start_gpu_slots), rather than now with slot 0 alone.
  [ "${MULTI:-0}" = 1 ] && return 0
  if [ "${MINER_REGISTERED:-0}" = 1 ]; then
    publish_capacity_signed
  else
    say "  [i] no registration seen yet: the signed capacity report waits for the hourly job (crontab),"
    say "      or run it yourself once the miner logs 'ready': bash $REPO/deploy/testnet-miner/publish-capacity.sh"
  fi
  return 0
}

# ---------------------------------------------------------------- model-registry guard (best-effort)
warn_model_registry(){
  command -v dendrad >/dev/null 2>&1 || return 0
  # The switch belongs to the jobs module, not to modelregistry. `enforce_model_registry` is field 14 of
  # dendra.jobs.v1.Params (chain/proto/dendra/jobs/v1/params.proto:63) and it is CreateCommit, in
  # x/jobs, that reads it. x/modelregistry carries the catalogue and audit_judge_model, nothing else.
  # Querying the wrong module answers without the field, the guard reads "not enforced", and the operator
  # whose commits are about to be refused for serving an unregistered model is told there is nothing to
  # watch. A missing field and a disabled switch look identical from outside, so the module queried has
  # to be the one that owns the parameter.
  local out enforce
  out=$(dendrad query jobs params -o json --node "${DENDRA_NODE:-tcp://127.0.0.1:26657}" 2>/dev/null) || return 0
  # Read the field by its key; do not count occurrences of a pattern. Comparing a `grep -c` to exactly
  # 1 answers on the shape of the text rather than on the value: an answer carrying the key on two
  # lines counts 2, which reads as "not enforced", and the operator whose commits are about to be
  # refused hears nothing. proto3 omits a boolean worth false, so absent and false are the same claim
  # and get the same silence; only an explicit `true` speaks. No interpreter is assumed: this kit runs
  # on hosts that carry neither jq nor python3.
  enforce=$(printf '%s' "$out" | sed -nE 's/.*"enforce_model_registry"[[:space:]]*:[[:space:]]*(true|false).*/\1/p' | head -1)
  [ "$enforce" = "true" ] && say "  [i] model registry enforced on-chain: keep DENDRA_MODEL_ID=$DENDRA_MODEL_ID (a different model = commits refused)."
}

# ---------------------------------------------------------------- healthcheck + status
# wait_healthy <kit> <service> -> three verdicts: 0 healthy (the miner is registered, or, in owner mode, it
# runs and waits for its owner to register it, the state that mode is designed to sit in, reported as such),
# 1 failed (a failure the daemon itself printed, a registration the chain refused or the faucet deferred, or
# another operator recorded for this identity), 3 not measured (neither, within the bound). Sets
# MINER_REGISTERED (read by capacity_after_join), 1 only for a registration read.
#
# It judges the miner as it is now, never its history. `docker compose logs` read whole gives every line the
# container ever printed, from every earlier start: one false failure at a first install (the chain's "key
# not found" about an account the faucet had not credited yet) then stays in that log for as long as the
# container lives, and every later run of this script would declare the miner not healthy (and, with --gpus,
# never start the other cards' identities, which start only after a healthy slot 0). So:
#   - the log is read from the current start of the miner container (its .State.StartedAt: a container that
#     restarted, or that this run recreated, starts a new window), else from this run's own start (MINER_UP_AT);
#   - the registration is read from the daemon's heartbeat (slots.sh::slot_registration: registered |
#     refused | deferred:<why> | mismatch | pending | unread), the state of the process now, which a
#     registration that failed an hour ago and succeeded since does not contradict. `mismatch` is an identity
#     the chain records under another operator (miner.check_operator): the heartbeat says it while it
#     holds, whereas the IDENTITY MISMATCH notice printed at start rotates out of a long-lived log.
# The bound is the one wait_registration_terminal derives for a slot k: the faucet's proof-of-work bound
# (DENDRA_FAUCET_POW_MAX_S) plus DENDRA_REGISTER_WAIT_EXTRA_S, never below the historical two minutes.
# Signatures that say the miner is broken now, whatever its registration: a keyring it cannot open, an identity
# whose operator is another key, an owner mode that could not start. "is not a valid name or address" is the
# keyring's wording for a key name it cannot resolve; the chain's "key not found" alone is not a signature:
# it ends the chain's answer about an account no credit has reached yet, the normal state of a first start.
# SERVE REFUSED is docker/entrypoint-services.sh's serve guard (services/serve_guard.py): the served model
# runs on the CPU outside the judge role, and the miner is not started.
WH_FAIL_RE="unauthorized|IDENTITY MISMATCH|is not a valid name or address|OWNER MODE NOT STARTED|KEYS NOT OPENED|SERVE REFUSED"
# A registration that failed, and one that happened: the last of these lines decides, when the heartbeat
# cannot be read (the daemon replays a failed registration, and a later success supersedes the failure).
# Not the word create-miner. The daemon prints it in lines before any registration (the stake warning of
# miner._registration_stake, "... -> create-miner would be rejected", and the chain's own usage text
# echoed after "ON-CHAIN REGISTRATION FAILED"), and taking the last such line for a registration
# would print [OK], sign a capacity report for a miner the chain does not hold and start the other cards. Only the line the
# daemon prints once the chain records it (miner._ready_line) is evidence.
# English only: no miner image this tree builds prints the French spellings an early image
# printed.
WH_REG_RE="ON-CHAIN REGISTRATION FAILED|miner .* ready|registered on-chain"
WH_REGFAIL_RE="ON-CHAIN REGISTRATION FAILED"
wait_healthy(){
  local kit="$1" svc="$2" rpc_http="${DENDRA_NODE:-}"; rpc_http="${rpc_http/tcp:\/\//http://}"
  local n=12 pow extra="${DENDRA_REGISTER_WAIT_EXTRA_S:-300}" since="" cid="" hb="unread" last="" hb_said=""
  pow="$(sed -n 's/^DENDRA_FAUCET_POW_MAX_S=//p' "$kit/.env" 2>/dev/null | head -1 | tr -d '\r')"
  [ -n "$pow" ] || pow="$(sed -n 's/.*DENDRA_FAUCET_POW_MAX_S:-\([0-9][0-9.]*\)}.*/\1/p' "$kit/docker-compose.yml" 2>/dev/null | head -1)"
  pow="${pow%%.*}"
  case "$pow$extra" in
    ""|*[!0-9]*) hb_said="  [i] the faucet's proof-of-work bound could not be read: the wait below is the historical ~2 min" ;;
    *) n=$(( (pow + extra) / 10 )); [ "$n" -ge 12 ] || n=12 ;;
  esac
  say "== [join] healthcheck (bounded ~$(( n * 10 + 15 )) s: the faucet's proof of work, the credit, the registration) =="
  [ -n "$hb_said" ] && say "$hb_said"
  # The heartbeat is read through the slot library, the one place that finds this kit's miner container; a
  # clone without it reads no heartbeat, and says so in the verdict (never a registration read as absent).
  _slots_load >/dev/null 2>&1 || true
  local h0 h1
  h0=$(curl -fsS -m 8 "$rpc_http/status" 2>/dev/null | grep -oE '"latest_block_height": *"[0-9]+"' | tr -dc '0-9')
  sleep 15
  h1=$(curl -fsS -m 8 "$rpc_http/status" 2>/dev/null | grep -oE '"latest_block_height": *"[0-9]+"' | tr -dc '0-9')
  if [ -n "$h0" ] && [ -n "$h1" ] && [ "$h1" -gt "$h0" ] 2>/dev/null; then
    say "  [OK] chain live (height $h0 -> $h1)"
  else
    warn "height not climbing (RPC stuck? consensus?) -> diagnostics below."
  fi
  # This check must not invert the signal.
  # Searching the logs for "register|create-miner|commit" is not enough: the miner's failure message
  # says "the chain will refuse every commit (unauthorized)", which contains the word "commit", so the
  # worst failure would read as "[OK] daemon activity detected". And in the nominal case on a quiet
  # network the miner only prints "waiting for jobs": no match, hence a mere warning.
  # So known failure signatures are searched first and stop the loop. Only then is positive evidence of
  # life looked for.
  local i reg=0 lg="" _om=""
  # Read by capacity_after_join: only a registration seen here lets the signed capacity report go out.
  MINER_REGISTERED=0
  for i in $(seq 1 "$n"); do
    # The window: since the miner container's current start, else since this run started (see above). With
    # neither (a library caller with no container), the whole log, as before.
    since=""; hb="unread"
    if declare -F slot_cid >/dev/null 2>&1; then
      cid="$(slot_cid 0 "$svc" --running 2>/dev/null)"
      [ -n "$cid" ] && since="$(docker inspect --format '{{.State.StartedAt}}' "$cid" 2>/dev/null | tr -d '\r')"
      hb="$(slot_registration 0 2>/dev/null)"; [ -n "$hb" ] || hb="unread"
    fi
    [ -n "$since" ] || since="${MINER_UP_AT:-}"
    lg="$(docker compose -f "$kit/docker-compose.yml" logs ${since:+--since "$since"} "$svc" 2>/dev/null)"
    # (a) failures that hold now, whatever the registration: never let them pass for activity.
    # `KEYS NOT OPENED` is the daemon's own stop on a keyring it cannot open (miner.py::_stop_on_keyring,
    # exit 4, restarted in a loop): quiet logs and no registration, which (c) below would read as a calm network.
    if printf '%s' "$lg" | grep -qE "$WH_FAIL_RE"; then
      say "  [!] Failure detected in the logs of $svc${since:+ (since $since)}:"
      printf '%s' "$lg" | grep -E "$WH_FAIL_RE" | tail -3 | sed 's/^/      /'
      if printf '%s' "$lg" | grep -q "KEYS NOT OPENED"; then
        say "      The miner cannot open its keys and stops before creating any. Its passphrase is the file"
        say "      keyring-passphrase in the directory DENDRA_SECRETS_DIR names in $kit/.env (mounted read-only"
        say "      at /run/dendra-secrets); an unencrypted keyring is encrypted by bash $kit/encrypt-keys.sh only."
        say "      Not declared OK."
      elif [ -n "${OWNER_ADDRESS:-}" ]; then
        say "      Owner mode: the registration its owner signs cannot be prepared as things stand. Not declared OK."
      else
        say "      This node would appear to mine while being mute to the network. Not declared OK."
      fi
      return 1
    fi
    if [ -n "${OWNER_ADDRESS:-}" ]; then
      # Owner mode: the miner never registers itself, and the commands it prints for its owner name
      # create-miner -- a word that is no evidence anywhere (WH_REG_RE), and (c) below would read the wait
      # this mode is designed for as not measured. The registration read on chain is the evidence of a registration; otherwise the
      # last "OWNER MODE -- " line says which of three states the daemon is in, and only one of them is
      # the expected wait. An unread registry is not measured, never [OK]; another operator on record is
      # a registered miner that does not mine until its owner signs the update-miner it prints.
      if [ "$hb" = registered ] || printf '%s' "$lg" | grep -qE "is registered by its owner .* and operated by this machine"; then reg=1; MINER_REGISTERED=1; break; fi
      _om="$(printf '%s' "$lg" | grep -E "OWNER MODE -- " | tail -1)"
      case "$_om" in
        *"could not be read"*) reg=owner_unread ;;
        *"its OPERATOR is"*) reg=owner_operator; break ;;
        ?*) reg=owner; break ;;
      esac
      sleep 10; continue
    fi
    # (b) the heartbeat, the registration as it is now: registered, or a refusal / deferral it stands in, or an
    # identity the chain records under another operator (whatever its log still holds).
    case "$hb" in
      registered) reg=1; MINER_REGISTERED=1; break ;;
      refused|deferred:*|mismatch) reg="hb:$hb"; break ;;
    esac
    # (c) without a heartbeat, positive evidence in the window: an actual registration, not a word inside an
    # error sentence -- and the last registration line decides, a failure followed by a success is a success.
    last="$(printf '%s' "$lg" | grep -E "$WH_REG_RE" | tail -1)"
    if [ -n "$last" ]; then
      if printf '%s' "$last" | grep -qE "$WH_REGFAIL_RE"; then reg="log:$last"; else reg=1; MINER_REGISTERED=1; fi
      break
    fi
    sleep 10
  done
  case "$reg" in
    1) say "  [OK] miner alive and registered (positive evidence, not a keyword match)" ;;
    hb:mismatch)
       say "  [!] Failure: IDENTITY MISMATCH -- the chain records another key as the operator of this identity, so it"
       say "      refuses every commit this machine sends (its heartbeat says so now, whatever its log still holds)."
       say "      The miner prints the two ways out when it starts: docker compose -p dendra-miner logs miner | grep -A14 'IDENTITY MISMATCH'"
       say "      (a long-lived container may have rotated that line out of its log: a restart of the miner prints it again)."
       say "      This node would appear to mine while being mute to the network. Not declared OK."
       return 1 ;;
    hb:deferred:draining)
       # The drain set by exit-miner.sh keeps a miner from registering again (miner.py::registration_step):
       # it does not retry by itself, and saying so would send the operator waiting.
       say "  [!] the miner runs, is not registered, and is draining: the file drain in its key volume, set by"
       say "      exit-miner.sh, keeps it from registering again while it is there. To have it register and take work:"
       say "        bash $kit/exit-miner.sh --undrain"
       say "      To finish leaving instead: bash $kit/exit-miner.sh --yes. Not declared OK."
       return 1 ;;
    hb:deferred:stale_kit)
       # The faucet refused this kit as below the network's floor (faucet.py::H.do_POST):
       # the daemon retries and is refused again until the kit is updated, so the update is what is named here.
       say "  [!] Failure: the miner runs and is not registered, so it receives no job."
       say "      Its heartbeat: the faucet refused it, this kit is below the network's floor (stale_kit)."
       say "      Retrying changes nothing until the kit is updated. Update: $(kit_update_hint)"
       say "      This node would appear to mine while being mute to the network. Not declared OK."
       return 1 ;;
    hb:*|log:*)
       say "  [!] Failure: the miner runs and is not registered, so it receives no job."
       case "$reg" in
         hb:refused) say "      Its heartbeat: the chain did not confirm its registration (create-miner)." ;;
         hb:deferred:*) say "      Its heartbeat: registration deferred by the faucet (${reg#hb:deferred:}); the daemon retries by itself." ;;
         *) say "      Its log: ${reg#log:}" ;;
       esac
       say "      It retries by itself; read it again with: bash $kit/miner_health.sh"
       say "      This node would appear to mine while being mute to the network. Not declared OK."
       return 1 ;;
    owner) say "  [OK] miner alive, in owner mode: it waits for its owner ($OWNER_ADDRESS) to register it, and mines"
           say "       once the chain records this machine's key as the operator. The three commands to run:"
           say "         docker compose -p dendra-miner logs miner | grep -A16 'OWNER MODE'" ;;
    owner_operator)
           say "  [!] Owner mode: the miner is registered, but the chain records another key as its operator, so it"
           say "      does not mine. Its owner ($OWNER_ADDRESS) signs the update-miner it prints, which names this"
           say "      machine's key the operator:"
           say "         docker compose -p dendra-miner logs miner | grep -A16 'OWNER MODE'" ;;
    owner_unread)
           warn "Owner mode: the miner could not read its registration from the chain within the bound, so whether it"
           warn "waits for its owner or for another operator is not known. Not declared OK. It reads it again on its own:"
           warn "  docker compose -p dendra-miner logs miner | grep -A16 'OWNER MODE'"
           return 3 ;;
    *) warn "Not measured: within the bound the miner neither reported a registration nor printed a known failure"
       warn "(heartbeat: $hb). It keeps trying on its own -- the faucet's proof of work, the credit, then create-miner."
       warn "Read where it stands in a few minutes: bash $kit/miner_health.sh"
       return 3 ;;
  esac
  return 0
}

# _jobs_params_read -> the command that reads the jobs params this miner's banner names: from this machine's
# own node when it runs one, else on the REST path /dendra/jobs/v1/params of the node DENDRA_NODE names (the
# API listener next to its RPC, as _jail_params_cmds guesses it: a candidate, labelled as one).
_jobs_params_read(){
  local host
  if [ "${OWN_NODE:-1}" = 1 ]; then
    printf 'docker compose -f %s/docker-compose.yml exec -T node dendrad query jobs params -o json' "$NODE_KIT"
  elif [ -n "${DENDRA_REST:-}" ]; then
    printf 'curl -s %s/dendra/jobs/v1/params' "${DENDRA_REST%/}"
  elif [ -n "${DENDRA_NODE:-}" ]; then
    host="${DENDRA_NODE#*://}"; host="${host%%:*}"
    printf 'curl -s http://%s:1317/dendra/jobs/v1/params   (if the operator exposes the API)' "$host"
  else
    printf 'the REST path /dendra/jobs/v1/params of any node of this network'
  fi
}

print_status_miner(){
  cat <<EOF
============================================================
  Dendra — miner '$MINER_ID' started
  Served model  : $DENDRA_MODEL_ID  (downloaded and served locally, on your machine$( [ "${ENGINE:-}" = cpu-judge ] && printf '%s' '; the judge model,
                  on the CPU instance ollama-cpu: no usable GPU, so no mining model -- see deploy/hw_probe.sh --role' ))
  Verification  : semantic embeddings ($DENDRA_EMBED_API_MODEL) — never 'hash' on a real network
  Chain (RPC)   : ${DENDRA_NODE:-?}
  Logs          : docker compose -p dendra-miner logs -f miner
  Health        : bash $MINER_KIT/miner_health.sh   (exit 0 every check ok, 1 a problem, 2 not everything
                  measured). Hourly, $MINER_KIT/miner-health.ALERT exists while a check fails or could not be measured.
  Start/stop    : cd $MINER_KIT && docker compose up -d   (from that directory, without -f: its .env
                  names the compose files of this machine, the network to your own node, the override (GPU,
                  or the judge on the CPU) and, for a judge, the judge profile -- the CPU instance that serves
                  its verdicts)
  Identity      : Docker volume 'miner-keys' = your miner identity -> do not delete it (else re-stake)
  Keys          : $(keys_banner_line)
  Rewards       : $( if [ -n "${PAYOUT_ADDRESS:-}" ] && [ -n "${OWNER_ADDRESS:-}" ]; then printf '%s' "Final Testnet Season to $PAYOUT_ADDRESS once the owner signs the declaration (the miner prints the three steps)"; elif [ -n "${PAYOUT_ADDRESS:-}" ]; then printf '%s' "Final Testnet Season paid to $PAYOUT_ADDRESS (declared by the miner once registered)"; else printf '%s' "Final Testnet Season paid to this machine's own key: declare another with --payout-address dendra1..."; fi )
  Owner         : $( [ -n "${OWNER_ADDRESS:-}" ] && printf '%s' "$OWNER_ADDRESS registers this miner and holds its stake (owner mode). The commands it signs:
                  docker compose -p dendra-miner logs miner | grep -A16 'OWNER MODE'   -- this protects the stake,
                  not the income: payments and the season's default payout go to this machine's key" || printf '%s' "this machine's key registers the miner and holds its stake (--owner dendra1... keeps the stake off this machine)")
  Leave         : bash $REPO/deploy/testnet-miner/exit-miner.sh     (shows what the chain would answer;
                  --yes deregisters the miner and returns its stake to its owner; in owner mode it
                  prepares the delete-miner the owner signs)
  Uninstall     : bash $REPO/deploy/uninstall.sh                    (plan only; --yes removes the kit and
                  keeps the miner's keys, after a verified backup)
  Faucet        : ${FAUCET:-?} (the miner self-funds; if PoW/cap blocks it -> ask on the testnet channel)
  If 0 jobs     : the work comes from the Final Testnet Season's generator, a fixed daily volume
                  (services/final_season_rules.py, requests_per_day), each request drawn among
                  the miners present on chain. OpenJob opens a job only while enough miners are eligible
                  at its anchor block (chain/x/jobs/keeper/pool_freeze.go, enoughEligibleMinersToVerify):
                  the jury floor (audit_min_quorum, through effectiveSlashFloor) plus the miner under
                  audit -- one miner when verification_mode=1, audit_unwind_blocks>0 and hold_bps>=10000
                  hold together. On which side of that floor the network stands is a reading, not a line
                  of this banner:
                    $(_jobs_params_read)
                    # verification_mode, audit_unwind_blocks, hold_bps, audit_min_quorum, juror_freshness_blocks
                  If this miner is registered and present and still receives nothing: check that its
                  registration holds a stake that is not zero, and that its availability proofs are proven
                  recently -- bash $MINER_KIT/miner_health.sh (checks C3 and C4) reads both.
                  If commits are refused instead: your model is not the one the on-chain registry lists.
============================================================
EOF
  [ "${MULTI:-0}" = 1 ] && print_status_slots
  return 0
}

# ---------------------------------------------------------------- miner image (prebuilt, pinned by the clone)
# Where the pin comes from is the guarantee, not its form. A digest names exact bytes; it says nothing
# about who built them. Read from network-info.txt, which the installer fetches over plain HTTP, the
# value would let a man in the middle hand over a digest-pinned image of his own, and this script
# would pull it and run it next to the volume that holds the miner's keys. So the pin lives in the
# clone, docker/MINER_IMAGE, which arrives over HTTPS git like docker/GENESIS_SHA256 and is written
# after a release from that release's miner-image.txt.
# Three states, never two: `pinned` (a full digest in the official namespace) is pulled; `none` (no
# file, or no value yet) builds and says so; `refused` (anything else) builds and warns. Building from
# the clone is the safe direction in every case (it runs the code the clone already carries), so no
# state other than `pinned` ever reaches a pull. The namespace is part of the rule: a full digest in
# someone else's registry is still someone else's code.
MINER_IMAGE_RE='^ghcr\.io/dendranetwork/dendra-miner@sha256:[0-9a-f]{64}$'
NODE_IMAGE_RE='^ghcr\.io/dendranetwork/dendra-node@sha256:[0-9a-f]{64}$'
# image_pin <name under docker/> <key> <ERE> <expected form, quoted in the refusal> -> IMAGE_PIN, IMAGE_STATE, IMAGE_WHY.
# One reader for both pins (docker/MINER_IMAGE, docker/NODE_IMAGE): two copies of a security decision
# drift apart. The miner and node wrappers below only name the file, the key and the form.
image_pin(){
  IMAGE_PIN=""; IMAGE_STATE=none; IMAGE_WHY=""
  local name="docker/$1" key="$2" re="$3" f lines n v
  f="${REPO:-}/$name"
  if [ ! -r "$f" ]; then
    IMAGE_WHY="$name is not in this tree"; return 0
  fi
  # A Windows checkout adds a CR; comment and blank lines carry no value.
  lines="$(tr -d '\r' < "$f" | grep -vE '^[[:space:]]*(#|$)' || true)"
  n="$(printf '%s' "$lines" | grep -c . || true)"
  if [ "${n:-0}" = 0 ]; then
    IMAGE_WHY="$name names no image yet (no release has been pinned)"; return 0
  fi
  IMAGE_STATE=refused
  if [ "$n" != 1 ]; then
    IMAGE_WHY="$name carries $n value lines where exactly one is expected"; return 0
  fi
  case "$lines" in
    "$key="*) v="${lines#"$key"=}" ;;
    *) IMAGE_WHY="$name is not of the form $key=<reference>"; return 0 ;;
  esac
  if [ -z "$v" ]; then
    IMAGE_STATE=none
    IMAGE_WHY="$name names no image yet (no release has been pinned)"; return 0
  fi
  if printf '%s\n' "$v" | LC_ALL=C grep -Eq "$re"; then
    IMAGE_PIN="$v"; IMAGE_STATE=pinned; return 0
  fi
  IMAGE_WHY="$name names '$v', which is not $4"
}
# miner_image_pin -> MINER_IMAGE_PIN, MINER_IMAGE_STATE (pinned|none|refused), MINER_IMAGE_WHY.
# The form above, then the gates (image_built_for, below): a pin whose `# built-for:` line does not name
# exactly this tree's docker/KIT_VERSION and docker/CONSENSUS_EPOCH is refused, and the image is built from
# the clone. The publication refuses such a pin on `main` (the image-pin guard of the development repository), but a pin can
# reach a clone without being published: the HiveOS package writes its release's miner-image.txt into the
# clone it installs (deploy/install.sh --miner-image-file). This is the joiner's own check, for that pin and
# for a clone that was edited or is older than its pin: the image would run code another kit was released with.
miner_image_pin(){
  image_pin MINER_IMAGE DENDRA_MINER_IMAGE "$MINER_IMAGE_RE" "ghcr.io/dendranetwork/dendra-miner@sha256:<64 lowercase hex>"
  MINER_IMAGE_PIN="$IMAGE_PIN"; MINER_IMAGE_STATE="$IMAGE_STATE"; MINER_IMAGE_WHY="$IMAGE_WHY"
  [ "$MINER_IMAGE_STATE" = pinned ] || return 0
  image_built_for MINER_IMAGE miner
  if [ "$IMAGE_GATES_OK" != 1 ]; then
    MINER_IMAGE_STATE=refused; MINER_IMAGE_PIN=""; MINER_IMAGE_WHY="$IMAGE_WHY"
  fi
}

# node_image_pin -> NODE_IMAGE_PIN, NODE_IMAGE_STATE (pinned|none|refused), NODE_IMAGE_WHY.
# The miner pin's rule, gates included, and the gates matter more here, because this image carries the
# consensus binary: an image of another epoch takes other state transitions and forks a new node at the first
# transaction that exercises the difference; an image of another kit carries another node-join.sh. The
# publication already refuses such a pin (the image-pin guard); this is the joiner's own check,
# for a clone that was edited or is older than its pin.
BUILT_FOR_RE='^# built-for: kit_version=([0-9]+) consensus_epoch=([0-9]+) release=(v[0-9]+\.[0-9]+\.[0-9]+)$'
# image_built_for <name under docker/> <role> -> IMAGE_GATES_OK (1|0), IMAGE_WHY, IMAGE_GATES.
# One rule for every pin (docker/MINER_IMAGE, docker/NODE_IMAGE), for the reason image_pin is shared: the
# file's `# built-for:` line must name exactly the gates of this tree (docker/KIT_VERSION and
# docker/CONSENSUS_EPOCH, line 1). A missing, doubled or malformed line, or an unreadable gate, is a refusal:
# an unread gate is not an equal one. IMAGE_GATES is "kit_version=K consensus_epoch=E" when they are equal.
image_built_for(){
  local bf nb kv ce
  IMAGE_GATES_OK=0; IMAGE_GATES=""
  bf="$(tr -d '\r' < "${REPO:-}/docker/$1" 2>/dev/null | grep -E "$BUILT_FOR_RE" || true)"
  nb="$(printf '%s' "$bf" | grep -c . || true)"
  kv="$(head -1 "${REPO:-}/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)"
  ce="$(head -1 "${REPO:-}/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
  if [ "${nb:-0}" != 1 ]; then
    IMAGE_WHY="docker/$1 pins an image without exactly one '# built-for:' line (${nb:-0} found): the gates it was built for are unknown"; return 0
  fi
  if [ -z "$kv" ] || [ -z "$ce" ]; then
    IMAGE_WHY="the gates of this tree (docker/KIT_VERSION, docker/CONSENSUS_EPOCH) could not be read, so the pinned $2 image cannot be checked against them"; return 0
  fi
  [[ "$bf" =~ $BUILT_FOR_RE ]] || { IMAGE_WHY="docker/$1: its '# built-for:' line could not be read"; return 0; }
  if [ "$((10#${BASH_REMATCH[1]}))" != "$((10#$kv))" ] || [ "$((10#${BASH_REMATCH[2]}))" != "$((10#$ce))" ]; then
    IMAGE_WHY="the pinned $2 image was built for kit ${BASH_REMATCH[1]} / epoch ${BASH_REMATCH[2]}, and this tree is kit $kv / epoch $ce"; return 0
  fi
  IMAGE_GATES_OK=1; IMAGE_GATES="kit_version=$((10#$kv)) consensus_epoch=$((10#$ce))"
}
# tree_gates -> TREE_GATES: "kit_version=K consensus_epoch=E" read from docker/KIT_VERSION and
# docker/CONSENSUS_EPOCH (line 1 of each), or empty when either cannot be read.
tree_gates(){
  local kv ce
  TREE_GATES=""
  kv="$(head -1 "${REPO:-}/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)"
  ce="$(head -1 "${REPO:-}/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
  [ -n "$kv" ] && [ -n "$ce" ] || return 0
  TREE_GATES="kit_version=$((10#$kv)) consensus_epoch=$((10#$ce))"
}
node_image_pin(){
  NODE_IMAGE_BUILT_FOR=""
  image_pin NODE_IMAGE DENDRA_NODE_IMAGE "$NODE_IMAGE_RE" "ghcr.io/dendranetwork/dendra-node@sha256:<64 lowercase hex>"
  NODE_IMAGE_PIN="$IMAGE_PIN"; NODE_IMAGE_STATE="$IMAGE_STATE"; NODE_IMAGE_WHY="$IMAGE_WHY"
  [ "$NODE_IMAGE_STATE" = pinned ] || return 0
  image_built_for NODE_IMAGE node
  if [ "$IMAGE_GATES_OK" != 1 ]; then
    NODE_IMAGE_STATE=refused; NODE_IMAGE_PIN=""; NODE_IMAGE_WHY="$IMAGE_WHY"; return 0
  fi
  # What node_compose_up writes beside the digest in the kit .env, so a later run can tell a pin of this
  # tree's gates from one an earlier release left there (node_env_image_reconcile).
  NODE_IMAGE_BUILT_FOR="$IMAGE_GATES"
}

# The kit .env must name the image that was actually started: a hand-run compose up reads it later, and
# a digest left there after a failed pull would point that restart at an image this machine does not
# have. Edited in place, so the file keeps the mode it was closed with (it carries the relay token).
_miner_env_image(){
  local f="$MINER_KIT/.env"
  [ -f "$f" ] || return 0
  if grep -q '^DENDRA_MINER_IMAGE=' "$f"; then
    sed -i "s|^DENDRA_MINER_IMAGE=.*|DENDRA_MINER_IMAGE=$1|" "$f"
  else
    printf 'DENDRA_MINER_IMAGE=%s\n' "$1" >> "$f"
  fi
}

# _miner_env_set <KEY> <value> [unset] -- one line of the miner kit's .env, edited in place like the image
# above (the file keeps its mode: it carries the relay token). Replaced where it stands, appended when
# absent; with `unset`, removed. Every other line is copied as it is.
_miner_env_set(){
  local f="$MINER_KIT/.env" t
  [ -f "$f" ] || return 0
  t="$(mktemp)" || return 1
  awk -v k="$1" -v v="$2" -v u="${3:-}" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s && u == "") print k "=" v; s = 1; next }
    { print }
    END { if (!s && u == "") print k "=" v }' "$f" > "$t" && cat "$t" > "$f"
  rm -f "$t"
}

# A digest names the bytes of one platform. The release builds the miner image on its own runner, and a
# single-platform image pulled on another one (an arm64 server, a 64-bit Raspberry Pi, WSL on a Windows arm64
# laptop) is accepted by the pull, then dies at start with "exec format error", or runs under emulation
# where the engine has one. So the platform is read from the pulled image and compared with the engine's,
# and only an equal pair is started; a build from the clone compiles for the engine's own platform.
# The engine, not the client: `.Server` is the daemon that runs the container. `.Client` is the CLI, and
# a laptop that drives a remote engine (DOCKER_HOST, a docker context) has one of each platform.
# Three states, as for the pin: `same` starts the pulled image, `different` builds here, and `unknown`
# (either side unreadable) builds here as well, with a warning: an unread platform is not a match.
# image_platform <reference> <miner|node> -> IMAGE_PLATFORM (same|different|unknown), IMAGE_WHY. Shared by
# both pins for the reason image_pin is: one decision, one place.
image_platform(){
  local img host
  IMAGE_PLATFORM=unknown; IMAGE_WHY=""
  img="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$1" 2>/dev/null | tr -d '\r' || true)"
  host="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null | tr -d '\r' || true)"
  # A half-read value ("linux/" or "/amd64") is as unread as an empty one.
  case "$img" in /*|*/|"") img="" ;; esac
  case "$host" in /*|*/|"") host="" ;; esac
  if [ -z "$img" ] || [ -z "$host" ]; then
    IMAGE_WHY="the platform of the pinned $2 image (${img:-unread}) or of this Docker engine (${host:-unread}) could not be read"
    return 0
  fi
  if [ "$img" = "$host" ]; then
    IMAGE_PLATFORM=same; return 0
  fi
  IMAGE_PLATFORM=different
  IMAGE_WHY="the pinned $2 image is built for $img and this Docker engine runs $host"
}
miner_image_platform(){
  image_platform "$1" miner
  MINER_IMAGE_PLATFORM="$IMAGE_PLATFORM"
  [ "$IMAGE_PLATFORM" = same ] || MINER_IMAGE_WHY="$IMAGE_WHY"
}

# miner_compose_up: starts the miner kit (reads COMPOSE_PROFILES_ARG, set by run_miner).
# The image is always passed explicitly on the compose command line: compose also reads the kit's .env,
# where an older join.sh wrote whatever network-info served, and the environment wins over that file.
miner_compose_up(){
  local foreign=""
  miner_image_pin
  if [ -n "${DENDRA_MINER_IMAGE:-}" ] && [ "$DENDRA_MINER_IMAGE" != "$MINER_IMAGE_PIN" ]; then
    say "  [i] DENDRA_MINER_IMAGE from the environment is ignored: the miner image is pinned by docker/MINER_IMAGE"
  fi
  case "$MINER_IMAGE_STATE" in
    pinned)
      say "  pulling the prebuilt miner image $MINER_IMAGE_PIN (pinned by docker/MINER_IMAGE in this clone; started only on an engine of its platform)"
      # The pull is judged by its effect, not by its exit code. On a service that also carries build:,
      # depending on its version compose may report a failed pull as a warning ("must be built from
      # source") and exit 0; the image is then simply absent and up --no-build dies on it. So the
      # image itself is looked up afterwards.
      if ( cd "$MINER_KIT" && DENDRA_MINER_IMAGE="$MINER_IMAGE_PIN" docker compose $COMPOSE_PROFILES_ARG pull miner ) \
         && docker image inspect "$MINER_IMAGE_PIN" >/dev/null 2>&1; then
        miner_image_platform "$MINER_IMAGE_PIN"
        case "$MINER_IMAGE_PLATFORM" in
          same)
            _miner_env_image "$MINER_IMAGE_PIN"
            ( cd "$MINER_KIT" && DENDRA_MINER_IMAGE="$MINER_IMAGE_PIN" docker compose $COMPOSE_PROFILES_ARG up -d --no-build ) \
              || die "docker compose up with the prebuilt image failed (see output above)"
            return 0 ;;
          different)
            say "  [i] $MINER_IMAGE_WHY: the miner image is built from this clone${DOCKER_DEFAULT_PLATFORM:+, for DOCKER_DEFAULT_PLATFORM=$DOCKER_DEFAULT_PLATFORM, which compose follows}"
            # Removed only after the build: a container left on it from an earlier start still holds the
            # reference until `up --build` replaces it, and docker refuses to remove an image in use.
            foreign="$MINER_IMAGE_PIN" ;;
          *)
            warn "$MINER_IMAGE_WHY: building the miner image from this clone instead" ;;
        esac
      else
        # A private package, a registry outage, no route to ghcr.io: none of them is a reason to stop a
        # join that can build the very same code from the clone it already holds.
        warn "the pinned miner image could not be pulled (private package, registry down, no route?): building it from this clone instead"
      fi
      ;;
    refused) warn "$MINER_IMAGE_WHY -- refused: the miner image is built from this clone instead" ;;
    *)       say "  [i] $MINER_IMAGE_WHY: the miner image is built from this clone" ;;
  esac
  _miner_env_image ""
  ( cd "$MINER_KIT" && DENDRA_MINER_IMAGE= docker compose $COMPOSE_PROFILES_ARG up -d --build ) || die "docker compose up failed (see output above)"
  if [ -n "$foreign" ]; then
    docker image rm "$foreign" >/dev/null 2>&1 || warn "the pulled image of another platform could not be removed: docker image rm $foreign"
  fi
}

# ---------------------------------------------------------------- the miner's own node, seen from its container
# local_node_rpc: the RPC address the miner container uses for the node running on this machine.
# The two kits meet on the node kit's `dendra-chain` network, where the node answers to its compose
# project name (deploy/testnet-node/docker-compose.yml, `aliases:`). That name is read from
# `docker compose config`, as start_local_node reads it for its volume check: a DENDRA_PROJECT set in the
# node kit's .env (a second node) is honoured, never re-derived here. An unreadable name returns 1 and
# writes nothing: an address that points at no node makes a blind miner, which is worse than a stop.
local_node_rpc(){
  local proj
  proj="$( cd "$NODE_KIT" 2>/dev/null && docker compose config 2>/dev/null | sed -n 's/^name:[[:space:]]*//p' | head -1 | tr -d '[:space:]"' )"
  # A compose project name is lower-case letters, digits, dashes and underscores; anything else was not read.
  case "$proj" in ""|*[!a-z0-9_-]*) return 1 ;; esac
  printf 'tcp://%s:26657' "$proj"
}

# miner_compose_files: the COMPOSE_FILE of a miner that reads this machine's node: the base kit, the
# overlay that attaches it to `dendra-chain`, and the GPU override when write_gpu_override left one.
# The override is listed by hand because COMPOSE_FILE turns off its automatic reading. Without it the
# miner would reach its node and Ollama would start on the CPU, with no error anywhere. The separator
# is ':' (COMPOSE_PATH_SEPARATOR on Linux, where this kit runs).
miner_compose_files(){
  local f="docker-compose.yml:docker-compose.local-node.yml"
  [ -f "$MINER_KIT/docker-compose.override.yml" ] && f="$f:docker-compose.override.yml"
  printf '%s' "$f"
}

# ---------------------------------------------------------------- roles
run_miner(){
  say "== [join] role: miner${JUDGE:+ (+judge)} =="
  verify_genesis_info
  verify_consensus_epoch
  verify_kit_version
  verify_release_signers
  if [ "${MULTI:-0}" = 1 ]; then
    # One identity per card needs the cards visible to Docker: a slot on the CPU would be an identity that
    # cannot serve its card's model in time. Refused here, before anything is written.
    { [ "$GPU_OK" = 1 ] && [ "$TOOLKIT_OK" = 1 ]; } || die "--gpus needs the NVIDIA cards visible to Docker (nvidia-container-toolkit), and the pre-flight above says they are not. Nothing was written, nothing was started."
    gpu_select_measure
  else
    write_gpu_override
  fi
  # The kit .env is written from the loaded config (an existing, already-filled .env is kept when no
  # explicit config is provided).
  # If this is a miner-judge, set the judge model (the probe's CPU judge) and carry it into the verdict.
  JUDGE_MODEL=""; JUDGE_MODEL_OVERRIDE=""
  # The judge role on the CPU is the whole role of a machine without a usable GPU (engine_decide): --judge is
  # implied, and the gate below is the one the probe has just applied. Said, since nobody passed the flag.
  # --no-judge has no meaning here: such a machine joins as a judge or not at all.
  if [ "${ENGINE:-}" = cpu-judge ] && [ "$NO_JUDGE" = 1 ]; then
    printf '[join] refused: --no-judge on a machine without a usable GPU. The testnet runs no mining model on the CPU, so this machine joins as a judge on the CPU or not at all (deploy/hw_probe.sh --role: judge). Re-run without --no-judge; a kit that recorded the refusal (DENDRA_NO_JUDGE=1 in %s/.env) lifts it with --judge. Nothing was changed.\n' "$MINER_KIT" >&2
    exit 2
  fi
  if [ "${ENGINE:-}" = cpu-judge ] && [ "$JUDGE" != 1 ]; then
    say "  [i] --judge implied: a machine without a usable GPU joins as a judge on the CPU (deploy/hw_probe.sh --role)."
    # --miner given on purpose (an update replays the options of the first run): it cannot mean "mine" here, and
    # what it turns into pulls the judge model, which is reported, since deploy/install.sh refuses --miner on such a host.
    case " ${JOIN_ARGS_ORIG[*]-} " in
      *" --miner "*) say "  [!] --miner was given, and this machine has no usable GPU: it has no miner role. It joins as a judge on the"
                     say "      CPU, and the judge model (the large pull of this kit) is pulled into its CPU instance." ;;
    esac
    JUDGE=1
  fi
  if [ "$JUDGE" = 1 ]; then
    # Hardware gate before anything else: an under-powered judge penalises honest miners, so the seat is
    # refused rather than degraded. The node still joins and mines normally.
    if ! _assert_can_judge; then
      # On the CPU there is no role to fall back to: no mining model runs there. The probe's two answers come
      # from one gate, so this is a disagreement between two readings of it, refused, never resolved by a guess.
      [ "${ENGINE:-}" = cpu-judge ] && die "deploy/hw_probe.sh --role says judge and --can-judge says ${HW_CAN_JUDGE}: two readings of one gate disagree, and a machine without a usable GPU has no other role. Nothing was started."
      # Declining the judge role does not decline the seat. The chain draws its jurors among live miners
      # (eligibility = a recent commit, x/jobs/keeper/miner_vitality.go), not among those who agreed to
      # judge. A node that mines without judging is therefore a mute seat: it raises the bar (2/3 of the
      # anchored seats) for everyone, and never votes. At 7 seats the bar is 5, and every mute seat
      # brings the audit closer to being unresolvable.
      # The default stays permissive (a public joiner must be able to mine without judging), but the
      # refusal is blocking when the operator declares they came for the committee:
      # DENDRA_JUDGE_REQUIRED=1.
      if [ "${DENDRA_JUDGE_REQUIRED:-0}" = "1" ]; then
        if [ "$HW_CAN_JUDGE" = "false" ]; then
          die "--judge requested but this hardware cannot judge (deploy/hw_probe.sh --can-judge: false). Joining anyway would occupy a mute jury seat and raise the quorum bar for the whole network. Refused on purpose (DENDRA_JUDGE_REQUIRED=1)."
        fi
        die "--judge requested and whether this hardware can judge is unknown (deploy/hw_probe.sh --can-judge: unknown, its RAM was not read). An unread gate seats no judge; joining as a miner would occupy a mute jury seat. Refused on purpose (DENDRA_JUDGE_REQUIRED=1)."
      fi
      JUDGE=0
      warn "--judge ignored: joining as a miner only, but the chain still draws this miner into audit committees (juror eligibility = a recent commit, not a declared role). A mining-only node is a mute seat that raises the 2/3 bar for everyone. Re-run on eligible hardware, or set DENDRA_JUDGE_REQUIRED=1 to make this fatal."
    else
      JUDGE_MODEL="$(_pick_judge_model)"
      case "$JUDGE_MODEL" in
        *qwen3:4b*) die "qwen3:4b is forbidden as a judge (unfair verdicts in a distributed setting). Unset DENDRA_JUDGE_MODEL: the probe picks the judge model.";;
      esac
      # Judge-model precedence (judge_worker.py::resolve_judge_model):
      #   1. --model-id            <- DENDRA_JUDGE_MODEL_OVERRIDE, written here
      #   2. the on-chain pin      <- modelregistry.audit_judge_model
      #   3. DENDRA_JUDGE_MODEL_ID <- fallback when the chain pins nothing
      # A value written only at rank 3 decides nothing as soon as the chain pins a model: the operator
      # believes they chose, the committee judges with something else, and the kit downloads 19 GB of a
      # model that will never be used. The operator's explicit choice is therefore carried to rank 1; a
      # model inferred from the hardware stays at rank 3, where the on-chain pin dominates it (the
      # intended homogeneity of the committee).
      if [ -n "${DENDRA_JUDGE_MODEL:-}" ]; then
        JUDGE_MODEL_OVERRIDE="$JUDGE_MODEL"
        say "  [i] Judge model = $JUDGE_MODEL — imposed by you: it wins over the on-chain pin."
      else
        JUDGE_MODEL_OVERRIDE=""
        say "  [i] Judge model = $JUDGE_MODEL (inferred by deploy/hw_probe.sh). The on-chain pin"
        say "      (modelregistry.audit_judge_model) wins if it exists; to impose your own, re-run with"
        say "      DENDRA_JUDGE_MODEL=<model> -- a model on the judge allow-list (bash deploy/hw_probe.sh --judge-allowed <model>)."
      fi
      if [ "${ENGINE:-}" = cpu-judge ]; then
        say "  [i] served model = $DENDRA_MODEL_ID, the judge model, on the CPU instance (no usable GPU, no mining model)."
      else
        say "  [i] served model = $DENDRA_MODEL_ID (sized by the probe). Committee diversity is an announcement gate."
      fi
    fi
  fi
  # One judge per machine, decided once above. With several identities and no judge, each of them is a
  # mute seat: drawn into juries, never voting. Permitted, and counted (DENDRA_JUDGE_REQUIRED=1 makes it fatal).
  if [ "${MULTI:-0}" = 1 ]; then
    [ "$JUDGE" = 1 ] || warn "no judge on this machine: $N_RUN identities will be drawn as jurors and never vote ($N_RUN mute jury seats). --judge on a machine that clears MOE_CPU_MIN_RAM_MB seats one CPU judge for all of them."
    write_slot_override 0 "${SLOT_UUID[0]}" || die "slot 0: its GPU override could not be written. Nothing was started."
    say "  [OK] slot 0 bound to the card ${SLOT_UUID[0]} (docker-compose.override.yml)"
  fi
  # The kit must not start deaf. If `DENDRA_RELAY_TOKEN` never reaches the generated .env, compose sets
  # it empty and relay_client takes a 401 on every request: the machine registers on-chain, takes a jury
  # seat, raises the 2/3 bar for everyone, and never receives a job. Seen from the network it looks like
  # a quiet miner, and the defect sits on the official onboarding path, so every new operator reproduces
  # it.
  # Fail closed: if the relay requires a token and none is available, refuse to generate a kit that
  # cannot work. An installation failure is cheaper than a mute node discovered a day later by reading
  # container logs.
  # Testing the presence of a secret proves nothing about its validity: a wrong, truncated or expired
  # token would pass a presence check and land on the same silent 401. So the relay is always probed.
  # The header is `X-Dendra-Token` (services/relay.py::_guard), not `Authorization: Bearer`.
  # Existing .env: the token and the judge role are carried over when they are not supplied again. A
  # re-run without DENDRA_RELAY_TOKEN would erase the kit's token and make the machine deaf; a re-run
  # without --judge would demote a judge to a mute seat. Both silently, on a machine that worked.
  _ENV="$MINER_KIT/.env"
  if [ -f "$_ENV" ]; then
    if [ -z "${DENDRA_RELAY_TOKEN:-}" ]; then
      _old="$(sed -n 's/^DENDRA_RELAY_TOKEN=//p' "$_ENV" | head -1)"
      [ -n "$_old" ] && { DENDRA_RELAY_TOKEN="$_old"; say "  [i] relay token carried over from the existing .env (not supplied on the command line)"; }
    fi
    # Same rewrite, same hazard: the .env is regenerated from scratch on every run, so a knob set once
    # and not repeated on the next command line would be silently reset to its default. The stake and
    # the PoW time bound are carried over for exactly the reason the token is.
    for _k in DENDRA_MINER_STAKE DENDRA_FAUCET_POW_MAX_S CONFIG_URL; do
      if [ -z "$(eval printf '%s' "\${$_k:-}")" ]; then
        _old="$(sed -n "s/^$_k=//p" "$_ENV" | head -1)"
        [ -n "$_old" ] && { export "$_k=$_old"; say "  [i] $_k carried over from the existing .env ($_old)"; }
      fi
    done
    # The payout address, carried over like the stake: it was verified when it was written.
    if [ -z "$PAYOUT_ADDRESS" ]; then
      _old="$(sed -n 's/^DENDRA_PAYOUT_ADDRESS=//p' "$_ENV" | head -1 | tr -d '\r')"
      [ -n "$_old" ] && { PAYOUT_ADDRESS="$_old"; say "  [i] DENDRA_PAYOUT_ADDRESS carried over from the existing .env ($_old)"; }
    fi
    # The owner is carried over too, and dropping it is never a side effect of a re-run: the identity of an
    # owner-mode miner derives from the owner, and a miner that lost the setting would derive another one.
    # (The miner also reads its owner back from the chain when the setting is missing; this keeps the two
    # from ever having to disagree.) Leaving owner mode is an edit of this file, made on purpose.
    _old="$(sed -n 's/^DENDRA_MINER_OWNER=//p' "$_ENV" | head -1 | tr -d '\r')"
    if [ -z "$OWNER_ADDRESS" ]; then
      [ -n "$_old" ] && { OWNER_ADDRESS="$_old"; say "  [i] DENDRA_MINER_OWNER carried over from the existing .env ($_old): this miner stays in owner mode"; }
    elif [ "$_old" != "$OWNER_ADDRESS" ]; then
      say "  [!] --owner on an existing kit: owner mode starts a new identity, derived from $OWNER_ADDRESS. A miner this"
      say "      machine already registered keeps its identity, and the miner refuses to start in owner mode on it"
      say "      (it says how to leave first: deploy/testnet-miner/exit-miner.sh, then a new miner-keys volume)."
    fi
    # --no-judge is that deliberate act: the seat is declined, said, and recorded (DENDRA_NO_JUDGE).
    if [ "$JUDGE" != "1" ] && [ "$NO_JUDGE" = 1 ] && [ "$(sed -n 's/^DENDRA_MINER_JUDGE=//p' "$_ENV" | head -1)" = "1" ]; then
      say "  [!] This node was a judge and --no-judge declines the seat: it becomes a mute seat (drawn by the chain, never voting)."
    elif [ "$JUDGE" != "1" ] && [ "$(sed -n 's/^DENDRA_MINER_JUDGE=//p' "$_ENV" | head -1)" = "1" ]; then
      say "  [!] This node was a judge and you are re-running without --judge: it would become a mute seat"
      say "      (drawn by the chain, never voting, the 2/3 bar rises for the whole network)."
      say "      Re-run with --judge, or set DENDRA_ACCEPT_MUTE_SEAT=1 if this is deliberate."
      [ "${DENDRA_ACCEPT_MUTE_SEAT:-0}" = "1" ] || exit 2
    fi
  fi
  # The same guard for every slot k: each env says whether its identity voted.
  if [ "${MULTI:-0}" = 1 ] && [ "$JUDGE" != "1" ]; then
    for _k in $SLOT_KS; do
      [ "$_k" = 0 ] && continue
      if [ "$(slot_val "$_k" DENDRA_MINER_JUDGE 2>/dev/null)" = 1 ]; then
        say "  [!] slot $_k was a judge and you are re-running without --judge: it would become a mute seat."
        say "      Re-run with --judge, or set DENDRA_ACCEPT_MUTE_SEAT=1 if this is deliberate."
        [ "${DENDRA_ACCEPT_MUTE_SEAT:-0}" = "1" ] || exit 2
      fi
    done
  fi
  if [ -n "${DENDRA_RELAY:-}" ]; then
    # Two probes, because a joiner needs two different questions answered and one does not imply the
    # other. A relay can answer your content reads and still never hand you a job.
    #   - GET pub/<id>: "does this relay talk to me at all?" 404 is a pass: the relay answered and
    #     simply holds nothing under that key, the expected state for a miner that has not worked yet.
    #   - GET list: "will it ever hand me work?" This is the work queue, and `relay.listing()` is the
    #     miner's only discovery call: behind a token, a machine registers, takes a jury seat and waits
    #     forever with no error to look for. It is not network-mapping metadata: the chain already
    #     publishes the same job-to-miner assignments to anyone who asks.
    # Only 401/403 is a refusal, on either. Neither probe may send a token: the question is what an
    # anonymous miner is served, and an operator's own secret would answer a different one.
    _probe="${MINER_ID:-preflight}"
    # No token here. An operator who sent `DENDRA_RELAY_TOKEN` would get 404 from a relay that gates
    # /pub behind the token, and the probe would then conclude "no shared secret needed to
    # read": the holder of the secret would certify an open door that a joiner without it finds shut. The
    # probe is anonymous, like the queue probe below.
    _code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' \
             "${DENDRA_RELAY%/}/pub/$_probe" 2>/dev/null || echo 000)"
    case "$_code" in
      200|404) say "  [OK] relay $DENDRA_RELAY answers this node (HTTP $_code) — no shared secret needed to read." ;;
      401|403)
        say "  [!] The relay $DENDRA_RELAY refuses even a read (HTTP $_code)."
        say "      This miner would register on-chain, take a jury seat, and never receive a job —"
        say "      one silent 401 per request. Stopping here is cheaper."
        say "      A current relay serves reads to anyone: getting this means the operator is running"
        say "      an older build that gates every route behind a shared token. Ask them to update,"
        say "      or pass one:  DENDRA_RELAY_TOKEN=<token> bash deploy/join.sh ..."
        exit 2 ;;
      000) say "  [!] relay $DENDRA_RELAY unreachable — there is no proof this node will be served."
           say "      This is not a green light: check the IP and the firewall before relying on this machine." ;;
      *)   say "  [i] relay $DENDRA_RELAY: HTTP $_code (neither served nor refused — keep an eye on it)" ;;
    esac

    # The work queue itself. Probed without the token even when one is set: the question a joiner needs
    # answered is whether an anonymous miner is served, and sending an operator's secret here would hide
    # a closed queue from whoever holds the secret.
    _lcode="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "${DENDRA_RELAY%/}/list" 2>/dev/null || echo 000)"
    case "$_lcode" in
      200) say "  [OK] the work queue (GET list) is open — this relay can hand this miner a job." ;;
      401|403)
        say "  [!] The relay $DENDRA_RELAY refuses the work queue (GET list -> HTTP $_lcode)."
        say "      Reads of your own content may work, but this is the only route by which a miner"
        say "      learns a job awaits it. This machine would register on-chain, take a jury seat,"
        say "      and wait forever without a single error to look for. Stopping here is cheaper."
        say "      A current relay serves this queue to anyone. Ask the operator to update."
        exit 2 ;;
      000) say "  [!] GET list unreachable — no proof this miner would ever be handed work." ;;
      *)   say "  [i] GET list: HTTP $_lcode (neither served nor refused — keep an eye on it)" ;;
    esac
  fi
  # --- The miner's own node: the default, and why ----------------------------------------------------
  # A miner needs a view of the chain: which jobs are open, what the seed is, whether its commit landed.
  # Pointing every miner at the operator's public RPC makes that operator a single point of failure (its
  # endpoint goes down, every miner goes blind at once), a point of censorship, and a party that has to
  # be trusted about the state of a chain whose purpose is not to require trust.
  # A miner already runs a machine 24/7 and already needs the chain, so it is the natural node operator.
  # `DENDRA_NODE` keeps pointing at the operator's public RPC throughout this script: it is what the
  # relay host is derived from, and what state-sync bootstraps against. What changes is the value handed
  # to the container, and only that.
  # The container reaches its node on the node kit's network, not through the host:
  # `host.docker.internal` (the Docker bridge gateway) only works while the node publishes its
  # RPC on every interface, and the node kit publishes it on 127.0.0.1, so the miner joins the
  # `dendra-chain` network (docker-compose.local-node.yml, enabled by COMPOSE_FILE in the kit's .env) and
  # dials the node by its alias, the node kit's compose project name.
  local MINER_RPC="${DENDRA_NODE:-}" MINER_COMPOSE_FILE=""
  if [ "$OWN_NODE" = 1 ]; then
    say "== [join] this miner will run its own node (recommended). Use --remote-rpc to depend on the operator's instead. =="
    start_local_node
    MINER_RPC="$(local_node_rpc)" || die "the node kit's compose project name could not be read ('docker compose config' in $NODE_KIT), so the miner cannot be told where its node answers. The node itself is up; re-run this command once that command answers."
    MINER_COMPOSE_FILE="$(miner_compose_files)"
    say "  [OK] miner will read the chain from its own node ($MINER_RPC, on the dendra-chain network)"
  else
    say "  [!] --remote-rpc: this miner depends on ${DENDRA_NODE:-?}. If that endpoint stops, this miner"
    say "      goes blind, and it cannot verify for itself what it is told about the chain."
  fi
  # The network-info address goes into the kit's .env so that the hourly self-test can read again what the
  # network publishes (KIT_VERSION, CONSENSUS_EPOCH, its public RPC) after this script has returned. It is
  # written only when it is a plain URL: compose reads that file and expands `$` in it.
  _cfg_url="${CONFIG_URL:-}"
  case "$_cfg_url" in
    *[!A-Za-z0-9:/._~%?=+@,-]*) warn "CONFIG_URL holds characters the kit's .env cannot carry safely: not written there; the hourly self-test will not compare this kit with the network."
                              _cfg_url="" ;;
  esac
  miner_keys_at_rest
  _ENV_WRITTEN=0
  if [ -n "${DENDRA_NODE:-}" ] && [ -n "${DENDRA_RELAY:-}" ] && [ -n "${FAUCET:-}" ]; then
    _ENV_WRITTEN=1
    cat > "$MINER_KIT/.env" <<EOF
# Generated by deploy/join.sh $(date -u +%FT%TZ)
DENDRA_NODE=$MINER_RPC
$(if [ -n "${MINER_COMPOSE_FILE:-}" ]; then
    # Written only for a miner that reads this machine's node. Setting it switches off compose's
    # automatic reading of docker-compose.override.yml, which is why miner_compose_files lists the GPU
    # override itself. (No backticks here: this comment lives inside an unquoted heredoc.)
    echo "# The compose files of this machine: the base kit, the network to this machine's own node"
    echo "# (docker-compose.local-node.yml) and, when there is one, the GPU override. compose reads this line"
    echo "# only when it runs from this directory without -f; with -f the miner is recreated blind to its node."
    echo "COMPOSE_FILE=$MINER_COMPOSE_FILE"
  fi)
# Public capacity registry. It is written here explicitly because DENDRA_NODE above is container-local in
# the default setup: anything re-deriving the registry from it would target the node's container alias and
# post into the void, which is invisible until the node silently drops off /network 24 h later.
DENDRA_CAPACITY_URL=$(capacity_url)
# The network-info this kit was set up from. Read by deploy/testnet-miner/miner_health.sh to compare this
# kit with what the network publishes; never sent to the container.
CONFIG_URL=${_cfg_url:-}
DENDRA_RELAY=$DENDRA_RELAY
DENDRA_RELAY_TOKEN=${DENDRA_RELAY_TOKEN:-}
FAUCET=$FAUCET
DENDRA_MODEL_ID=$DENDRA_MODEL_ID
DENDRA_EMBED_MODE=$DENDRA_EMBED_MODE
DENDRA_EMBED_API_MODEL=$DENDRA_EMBED_API_MODEL
MINER_ID=$MINER_ID
DENDRA_MINER_JUDGE=$JUDGE
# The judge seat declined on purpose (--no-judge): 1 is kept by a re-run and by the automatic update.
DENDRA_NO_JUDGE=$NO_JUDGE
# The daily automatic update of this kit (deploy/testnet-miner/kit_update.sh): 0 turns it off (--no-auto-update).
DENDRA_AUTO_UPDATE=$AUTO_UPDATE
# --payout-lock: 1 asks the programme to hold the payout address for good with its first declaration.
DENDRA_PAYOUT_LOCK=$PAYOUT_LOCK
# docker/RELEASE_SIGNERS as this kit recorded it (sha256): a later run refuses a file that differs
# (deploy/join.sh --accept-new-signers prints the new one and records it). Empty = none recorded.
DENDRA_RELEASE_SIGNERS_SHA256=$SIGNERS_SHA_RECORD
# Two settings that exist only if they are written here. compose forwards to the container only the
# variables its environment: block names, and it reads their values from this file, not from the
# environment of the shell that ran join.sh. Without these lines "DENDRA_MINER_STAKE=60000 bash deploy/join.sh"
# would be dropped in silence: the operator would believe they had set a registration stake, the container
# would never see the variable, and the only trace would be a create-miner that behaves differently.
# Empty stays the recommended value for the stake (the miner then reads min_stake from the chain, a
# governed parameter); an explicit choice is written here and never cancelled mutely.
DENDRA_MINER_STAKE=${DENDRA_MINER_STAKE:-}
# Final Testnet Season programme: where the programme answers. The payout declaration and
# the Dendra application's Final Testnet Season view read it. Empty = neither works; the identity is
# still ranked on what the chain records, and paid to its operator address.
DENDRA_FINAL_SEASON_URL=${DENDRA_FINAL_SEASON_URL:-}
# --payout-address (checksum verified), declared by the miner once registered. Empty = this machine's key.
DENDRA_PAYOUT_ADDRESS=${PAYOUT_ADDRESS:-}
# --owner (checksum verified): the off-machine key that registers this miner and holds its stake. Empty = itself.
DENDRA_MINER_OWNER=${OWNER_ADDRESS:-}
# Keyring passphrase directory, mounted read-only, outside the volume and the clone. Empty = unencrypted keys.
DENDRA_SECRETS_DIR=${SECRETS_VALUE:-}
# Prebuilt miner image, by digest, read from docker/MINER_IMAGE in the clone, never from network-info.
# Empty = the image is built on this machine from the clone. Written empty here, and set to the pin by
# miner_compose_up only once the pulled image has been found and its platform matched, right before it is
# started: a join interrupted during the pull must not leave a hand-run compose, or the application's
# Start button, pointed at an image this machine cannot run.
DENDRA_MINER_IMAGE=
DENDRA_FAUCET_POW_MAX_S=${DENDRA_FAUCET_POW_MAX_S:-}
# Rank 3 (fallback when the chain pins no model) and the declaration carried by the on-chain verdict:
# it is what makes judge-model diversity measurable.
DENDRA_JUDGE_MODEL_ID=$JUDGE_MODEL
# Rank 1: filled only on an explicit choice (DENDRA_JUDGE_MODEL), empty otherwise.
DENDRA_JUDGE_MODEL_OVERRIDE=$JUDGE_MODEL_OVERRIDE
$(if [ "$JUDGE" = 1 ]; then
    # Two engines. Left empty, this variable makes the judge run on the miner's Ollama: the
    # judge model (MoE ~19 GB) evicts the mining one, the miner exits on a fatal Ollama ReadTimeout,
    # and every assigned job stays open: the machine looks alive and serves nothing. ollama-cpu is
    # the kit's dedicated CPU instance.
    # (No backticks: this comment lives inside an unquoted heredoc, where bash executes them.)
    echo "DENDRA_JUDGE_ENDPOINT=http://ollama-cpu:11434"
    # The judge's services carry the profile judge, and a hand-run compose up starts no profiled service
    # unless it is named: compose reads this line from this directory, as it reads COMPOSE_FILE, so the
    # restart the banner prints starts ollama-cpu too. (No backticks: unquoted heredoc.)
    echo "COMPOSE_PROFILES=judge"
  fi)
EOF
    # The .env carries the relay token. Default permissions can leave it readable and writable by every
    # user of the machine. A secrets file is closed at creation time, never hardened afterwards.
    chmod 600 "$MINER_KIT/.env" 2>/dev/null || true
    say "  [OK] $MINER_KIT/.env written"
  elif [ "$OWN_NODE" = 1 ] && [ -f "$MINER_KIT/.env" ]; then
    # The kit is kept, but its route to the node is rewritten. No network settings reached this run (no CONFIG_URL,
    # or one that did not answer), so the .env is not regenerated -- but start_local_node above has just
    # rewritten the node kit, its RPC on loopback, and recreated the node. A .env from an older join.sh
    # still says host.docker.internal, which reached the node only while it published on every
    # interface: left as it is, the miner is blind from the next start. So the lines this machine's node
    # decides are written in place, and nothing else: the address by the node's alias, the compose files
    # (the GPU override included, since COMPOSE_FILE switches off its automatic reading), and the judge's
    # profile when the kit runs a judge.
    _miner_env_set DENDRA_NODE "$MINER_RPC"
    _miner_env_set COMPOSE_FILE "$MINER_COMPOSE_FILE"
    if [ "$(sed -n 's/^DENDRA_MINER_JUDGE=//p' "$MINER_KIT/.env" | head -1 | tr -d '\r')" = 1 ]; then
      _miner_env_set COMPOSE_PROFILES judge
    else
      _miner_env_set COMPOSE_PROFILES "" unset
    fi
    say "  [OK] $MINER_KIT/.env kept; its route to this machine's node rewritten in place (DENDRA_NODE=$MINER_RPC, COMPOSE_FILE=$MINER_COMPOSE_FILE)"
  fi
  # A kept .env still receives the lines this run decided: the payout address and the owner given on this
  # command line, and the passphrase directory, edited in place, every other line copied as it is.
  if [ "$_ENV_WRITTEN" != 1 ] && [ -f "$MINER_KIT/.env" ]; then
    [ -n "$PAYOUT_ADDRESS" ] && _miner_env_set DENDRA_PAYOUT_ADDRESS "$PAYOUT_ADDRESS"
    [ -n "$OWNER_ADDRESS" ] && _miner_env_set DENDRA_MINER_OWNER "$OWNER_ADDRESS"
    case "$KEYS_AT_REST" in
      encrypted) _miner_env_set DENDRA_SECRETS_DIR "$SECRETS_VALUE" ;;
      plain) [ "$PLAIN_KEYS" = 1 ] && _miner_env_set DENDRA_SECRETS_DIR "" ;;
    esac
    # The judge role on the CPU, written in place too: a kit written when this machine still mined on its CPU
    # would otherwise keep a mining model and no judge, while the override points the miner at the CPU
    # instance, an engine that holds a model this .env does not name, under a profile it does not start.
    if [ "${ENGINE:-}" = cpu-judge ]; then
      _miner_env_set DENDRA_MODEL_ID "$DENDRA_MODEL_ID"
      _miner_env_set DENDRA_MINER_JUDGE 1
      _miner_env_set DENDRA_JUDGE_ENDPOINT "http://ollama-cpu:11434"
      _miner_env_set DENDRA_JUDGE_MODEL_ID "$JUDGE_MODEL"
      _miner_env_set DENDRA_JUDGE_MODEL_OVERRIDE "$JUDGE_MODEL_OVERRIDE"
      _miner_env_set COMPOSE_PROFILES judge
    elif [ "$JUDGE" = 1 ]; then
      # The seat this run decided (--judge, or the default on a machine that can judge), written in place on a kept
      # .env: without these lines a kit written without a judge keeps running without one after an update.
      _miner_env_set DENDRA_MINER_JUDGE 1
      _miner_env_set DENDRA_JUDGE_ENDPOINT "http://ollama-cpu:11434"
      _miner_env_set DENDRA_JUDGE_MODEL_ID "$JUDGE_MODEL"
      _miner_env_set DENDRA_JUDGE_MODEL_OVERRIDE "$JUDGE_MODEL_OVERRIDE"
      _miner_env_set COMPOSE_PROFILES judge
    elif [ "$NO_JUDGE" = 1 ]; then
      _miner_env_set DENDRA_MINER_JUDGE 0
      _miner_env_set DENDRA_JUDGE_ENDPOINT "" unset
      _miner_env_set COMPOSE_PROFILES "" unset
    fi
    # The three kit settings (judge refusal, automatic update, payout lock) and the signers record, on a kept .env as on a written one.
    _miner_env_set DENDRA_NO_JUDGE "$NO_JUDGE"
    _miner_env_set DENDRA_AUTO_UPDATE "$AUTO_UPDATE"
    _miner_env_set DENDRA_PAYOUT_LOCK "$PAYOUT_LOCK"
    if [ -n "$SIGNERS_SHA_RECORD" ]; then _miner_env_set DENDRA_RELEASE_SIGNERS_SHA256 "$SIGNERS_SHA_RECORD"
    else _miner_env_set DENDRA_RELEASE_SIGNERS_SHA256 "" unset; fi
  fi
  # Slot 0 remembers its card from the first --gpus on: a re-run binds the same identity to the same card.
  # The base compose file never reads DENDRA_GPU_UUID (an empty value there would mean no GPU for everyone).
  if [ "${MULTI:-0}" = 1 ] && [ -f "$MINER_KIT/.env" ]; then
    _miner_env_set DENDRA_SLOT 0
    _miner_env_set DENDRA_GPU_UUID "${SLOT_UUID[0]}"
  fi
  # The update instruction and the re-run of this run, written or kept .env alike: read by the hourly watch.
  persist_kit_update
  # The daily automatic update, scheduled from the re-run just recorded (or unscheduled: DENDRA_AUTO_UPDATE=0).
  install_kit_update_cron
  warn_model_registry
  # The judge lives in profiled services (`judge`). Without this flag, docker compose starts neither
  # `ollama-cpu` nor its model download: `DENDRA_JUDGE_ENDPOINT` would point at a non-existent host and
  # every verdict would fail, silently from the miner's point of view.
  COMPOSE_PROFILES_ARG=""
  [ "$JUDGE" = 1 ] && COMPOSE_PROFILES_ARG="--profile judge"
  # A miner started again after exit-miner.sh registers again: the exit that file recorded (which keeps
  # the hourly watch quiet about a stopped miner) no longer applies.
  if [ -f "$MINER_KIT/miner-health.EXITED" ]; then
    rm -f "$MINER_KIT/miner-health.EXITED"
    say "  [i] this miner had left the network (exit-miner.sh); starting it registers it again."
  fi
  # The judge's network must exist before slot 0 starts: its override names it as external.
  [ "${MULTI:-0}" = 1 ] && [ "$JUDGE" = 1 ] && ensure_rig_network
  # Slot 0 is the kit's directory, and its .env names its project and files. The environment of this process
  # wins over that file: a COMPOSE_PROJECT_NAME left in the shell (a slot k's env sourced by hand carries one)
  # would start slot 0 under another project, with another miner-keys volume, so another identity on the stake.
  unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_ENV_FILES
  # This run's start, for wait_healthy: what the miner printed before it is history, not its state now. Five
  # seconds earlier than the clock says, so that a container started in the same second is not cut off.
  MINER_UP_AT="$(( $(date +%s) - 5 ))"
  # The prebuilt image, when the clone pins one, else a build from the clone: see miner_compose_up.
  miner_compose_up
  # The capacity report: scheduled here, sent signed once the chain records the miner (schedule_capacity).
  schedule_capacity
  # The exit status reports whether the join worked, never whether --judge was passed.
  # `wait_healthy` returns 1 when it finds a registration failure in the logs, and this script runs under
  # `set -u` with no `set -e`, so its verdict is kept in WH_RC: a bare call would drop it, and since a
  # function's status is its last command's, a trailing `[ "$JUDGE" = 1 ] && say ...` would make a
  # successful join without --judge exit 1, and a detected mute node with --judge exit 0.
  # Three exit statuses for a miner whose containers run, as wait_healthy has three verdicts: 0 healthy, 1 a
  # failure measured, 3 not measured (the miner neither registered nor failed within the bound: a faucet's
  # proof of work still running, a slow credit). 3 is never folded into 0 or into 1: deploy/install.sh
  # reads all three, and installs the desktop application whenever the containers run.
  wait_healthy "$MINER_KIT" miner; WH_RC=$?
  if [ "$WH_RC" = 0 ]; then
    capacity_after_join
    install_miner_health_cron
    [ "${MULTI:-0}" = 1 ] && start_gpu_slots
    print_status_miner
    [ "$JUDGE" = 1 ] && say "  [i] Judge mode: reveal_worker+judge_worker start once the miner key is ready (logs: grep judge)."
    return 0
  fi
  # The containers are up, so the banner still prints: it carries the log command the operator now
  # needs. What changes is that the status stops claiming a success nobody measured. The watch is
  # installed here too: its first pass names what is wrong, check by check.
  install_miner_health_cron
  print_status_miner
  if [ "$WH_RC" = 3 ]; then
    capacity_after_join
    warn "Not measured: the containers run and the miner is still on its way to a registration (see above)."
    [ "${MULTI:-0}" = 1 ] && warn "The other cards' identities start after a healthy slot 0 only: re-run once it is registered: bash $REPO/deploy/join.sh${GPUS_SPEC:+ --gpus $GPUS_SPEC}"
    warn "Exiting 3: neither a success nor a failure was measured."
    return 3
  fi
  warn "Not declared healthy — the failure printed above stands. The containers are running, so"
  warn "this node would appear to mine while being mute to the network. Exiting 1 on purpose."
  return 1
}

# ================================================================ sync watch
# Why this is a progress watch and not a timeout.
#
# A fixed deadline on a sync is bound to be wrong. Replaying a chain from block 1 takes as long as the
# chain is long: on a host applying ~23 blocks per second, ~92 000 blocks need about an hour, and that
# number grows every day the network runs. Any wall-clock limit is therefore certain to fire on a node
# that is working, and the operator, told to "check SEEDS/network", looks at something that is not the
# cause.
#
#  (a) A duration cannot tell "slow" from "dead". The chain only grows, hosts differ by an order of
#      magnitude in disk throughput, and any constant is wrong for somebody, today or in a month when
#      the chain is twice as long. What separates the two cases is progression. So the bound below is on
#      time without a new block, never on total time: a sync that crawls is waited on for as long as it
#      moves forward, and one that has stopped is reported in minutes rather than in half an hour.
#
#  (b) The old message named a cause the measurement contradicts. "check SEEDS/network" sent the
#      operator to inspect peers and firewall, while the one fact that mattered was never said: the
#      container keeps syncing after this script returns. So the failure path reports what was observed
#      (height reached, network head, rate this node actually sustained, how long since the height last
#      moved) and says explicitly that nothing is lost.
#
#  (c) A textual predicate on JSON cannot have three answers. The old line grepped the status output for
#      `"catching_up": *(true|false)`. A failed `docker exec`, a node not yet listening, a status document
#      in an unexpected shape, and an encoder that omits a boolean worth false all produce the same empty
#      string. The reader below returns `unreadable` as a value of its own and never lets it count as
#      caught up.

# ---- reading a status document --------------------------------------------------------------------
# A real parser is preferred and a by-key text read is the documented fallback, announced when it is
# used. This kit does not require an interpreter (see warn_model_registry: it runs on hosts that carry
# neither jq nor python3), so demanding one here would turn a sync wait into a hard prerequisite.
# The fallback still answers three states, because what makes a probe unreadable is decided on the
# document (is this a status document at all), not on whether one field could be extracted from it.
_SYNC_JSON=""

# _status_fields RAW -> "<height> <true|false|absent>", or the single word NOSTATUS.
# Extraction only. It decides nothing: `absent` is reported as `absent` and the caller applies the
# proto3 rule, so the place where "omitted" becomes "false" is one line, in one function, below.
_status_fields(){
  local raw="$1"
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf 'NOSTATUS\n'; return 0;; esac
  case "$_SYNC_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import json,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("NOSTATUS"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
s=None
if isinstance(d,dict):
    for k in ("sync_info","SyncInfo"):
        if isinstance(d.get(k),dict): s=d[k]; break
if s is None: print("NOSTATUS"); raise SystemExit(0)
try: h=int(str(s.get("latest_block_height",0)))
except Exception: h=0
if "catching_up" in s:
    v=s["catching_up"]; c="true" if (v is True or v=="true") else "false"
else:
    c="absent"
print("%d %s"%(h,c))
' 2>/dev/null
      ;;
    jq)
      # `.catching_up // "absent"` would be wrong, which is why `has` is used instead: jq treats
      # `false` as empty for the alternative operator, so a node that has caught up would be reported
      # as a node whose field is missing.
      printf '%s' "$raw" | jq -r '
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | ($d.sync_info // $d.SyncInfo) as $s
        | if ($s|type)!="object" then "NOSTATUS"
          else ((($s.latest_block_height // 0)|tostring) + " "
                + (if ($s|has("catching_up")) then ($s.catching_up|tostring) else "absent" end))
          end' 2>/dev/null
      ;;
    *)
      # Fallback: fields read by their key (never a count of occurrences, never a match on a whole
      # `"key": value` pair). The document is qualified first: no sync_info, no status.
      case "$raw" in *'"sync_info"'*|*'"SyncInfo"'*) : ;; *) printf 'NOSTATUS\n'; return 0;; esac
      local h c
      h="$(printf '%s' "$raw" | sed -nE 's/.*"latest_block_height"[[:space:]]*:[[:space:]]*"?([0-9]+)"?.*/\1/p' | head -1)"
      c="$(printf '%s' "$raw" | sed -nE 's/.*"catching_up"[[:space:]]*:[[:space:]]*"?(true|false)"?.*/\1/p' | head -1)"
      printf '%s %s\n' "${h:-0}" "${c:-absent}"
      ;;
  esac
}

# ---- The reader is checked before it is trusted -----------------------------------------------------
# A reader is picked here from what the host happens to carry, so at most one of the three branches
# above runs on any given machine, and the branch that runs on somebody else's machine may never have
# run on ours. A wrong option, a jq expression that does not compile, a python that is really python2:
# each returns nothing, which this loop would read as "unreadable" forever, turning into a 15-minute
# wait that ends in a false report. So the selected reader is made to answer a document whose correct
# answer is known, and a reader that fails its own fixture is not used.
# Fixture A is the RPC envelope (tests the `result` unwrap) and carries catching_up true.
# Fixture B is the bare `dendrad status` shape and carries catching_up false, the case a jq
# alternative operator silently swallows.
_SYNC_FIX_A='{"jsonrpc":"2.0","id":-1,"result":{"node_info":{"network":"dendra"},"sync_info":{"latest_block_hash":"AB12","latest_block_height":"55882","catching_up":true}}}'
_SYNC_FIX_B='{"node_info":{"network":"dendra"},"sync_info":{"latest_block_height":"92405","earliest_block_height":"1","catching_up":false},"validator_info":{"voting_power":"0"}}'
_sync_reader_ok(){
  [ "$(_status_fields "$_SYNC_FIX_A")" = "55882 true" ] || return 1
  [ "$(_status_fields "$_SYNC_FIX_B")" = "92405 false" ] || return 1
}
# --- The age of the last block: the only value that tells a live chain from a stopped one ----------
# `catching_up=false` does not say the chain is advancing: it says this node has applied everything
# it was offered. A network that stopped seven hours ago answers like a healthy one, and the node
# really is synchronised, with a dead chain. The height does not help either: it is high, just
# motionless. The one quantity that separates the two is the block timestamp.
#
# This reader returns that timestamp in epoch seconds, never an age: the age is computed by the caller
# through `_sync_now`, the clock already indirected for the bench. A reader calling its own clock would
# be untestable, and three branches with three clocks would be three places for a defect.
#
# It can return `?`. Converting an ISO date to epoch in portable shell is not a given: `date -d` is a
# GNU extension. A host without python3, without jq and without GNU date cannot date the block, and `?`
# is returned, which the caller displays as unknown and never reads as fresh. Same rule as everywhere
# here: a field absent means zero, while not having been able to find out is a distinct sentinel.
# That `?` must not disqualify the `_status_fields` reader: dating the block is a supplement, reading
# the height is the service. So this function has its own probe, apart from `_sync_reader_ok`.
_status_block_epoch(){
  local raw="$1" ts
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf '?\n'; return 0;; esac
  case "$_SYNC_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import calendar,json,re,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("?"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
s=None
if isinstance(d,dict):
    for k in ("sync_info","SyncInfo"):
        if isinstance(d.get(k),dict): s=d[k]; break
if s is None: print("?"); raise SystemExit(0)
t=s.get("latest_block_time")
if not isinstance(t,str) or not t: print("?"); raise SystemExit(0)
m=re.match(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})", t)
if not m: print("?"); raise SystemExit(0)
print(calendar.timegm(tuple(int(x) for x in m.groups())+(0,0,0)))
' 2>/dev/null || printf '?\n'
      ;;
    jq)
      # The fractional seconds are stripped before conversion: `fromdateiso8601` rejects
      # `...:26.772062712Z` and would raise, i.e. yield `?`, on a readable document.
      printf '%s' "$raw" | jq -r '
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | ($d.sync_info // $d.SyncInfo) as $s
        | if ($s|type)!="object" or (($s.latest_block_time // "")|type)!="string"
             or ($s.latest_block_time // "")=="" then "?"
          else (try (($s.latest_block_time|sub("\.[0-9]+";"")|sub("[+-][0-9]{2}:[0-9]{2}$";"Z")
                     |(if endswith("Z") then . else .+"Z" end))|fromdateiso8601|tostring)
                catch "?")
          end' 2>/dev/null || printf '?\n'
      ;;
    *)
      # Fallback: the string is extracted by its key, then handed to `date`. If that `date` is not
      # GNU it rejects `-d` and `?` is returned, an admission rather than an approximation.
      case "$raw" in *'"latest_block_time"'*) : ;; *) printf '?\n'; return 0;; esac
      ts="${raw#*\"latest_block_time\"}"; ts="${ts#*:}"; ts="${ts#*\"}"; ts="${ts%%\"*}"
      case "$ts" in [0-9][0-9][0-9][0-9]-*) : ;; *) printf '?\n'; return 0;; esac
      date -u -d "$ts" +%s 2>/dev/null || printf '?\n'
      ;;
  esac
}

# The same gesture as `_sync_reader_ok`, on a document whose answer is known: 2020-01-01T00:00:00Z is
# 1577836800. It is not called to pick the reader: it is called to learn whether dating is
# available at all, and to say so when it is not.
_BLOCK_TIME_FIX='{"result":{"sync_info":{"latest_block_height":"7","latest_block_time":"2020-01-01T00:00:00.123456789Z","catching_up":false}}}'
_block_time_available(){ [ "$(_status_block_epoch "$_BLOCK_TIME_FIX")" = "1577836800" ]; }

# Past this, a last block stops being proof of life. The value is an order of magnitude, stated as
# such, not a derivation: the interval between blocks is fixed by no consensus parameter (it emerges
# from `timeout_commit` and from propagation), so no number on the chain turns into this threshold.
# 300 s lets a hiccup, a restart or a slightly skewed clock through, and does not let a stop through.
# The comparison brings the clock of the joining machine into the verdict: a host with a wrong date
# sees an old block that is not old. That is why this finding warns and blocks nothing: the node
# itself is up to date.
: "${DENDRA_STALE_BLOCK_SECS:=300}"

_SYNC_READER_PICKED=0
_sync_pick_reader(){
  [ "$_SYNC_READER_PICKED" = 1 ] && return 0
  _SYNC_READER_PICKED=1
  local c
  for c in python3 jq __by_key__; do
    case "$c" in
      python3|jq) command -v "$c" >/dev/null 2>&1 || continue; _SYNC_JSON="$c";;
      *) _SYNC_JSON="";;
    esac
    if _sync_reader_ok; then
      [ -n "$_SYNC_JSON" ] || warn "neither python3 nor jq on this host: the sync check reads the status fields by key from the text. It still tells 'caught up' from 'still syncing' from 'unreadable' — install jq if you want the document parsed."
      return 0
    fi
    warn "the '$c' status reader failed its own fixture (a document whose answer is known) -> trying the next reader."
  done
  _SYNC_JSON=""
  warn "no working status reader on this host. The sync wait will report 'unreadable' rather than guess — install jq or python3."
  return 1
}

# _sync_state RAW -> "<state> <height> <catching_up>"
#   state  : synced | syncing | unreadable   <- three answers. `unreadable` is a sentinel of its own.
#   height : an integer inside a document that parsed; `?` when there is no document to read.
#
# The zero rule, applied twice and in opposite directions:
#   - Inside a document that parsed, an omitted boolean is the zero of its type, i.e. false. Absent and
#     false are the same claim and get the same treatment.
#   - But `catching_up` false is not sufficient, because `latest_block_height` is a uint and gets
#     omitted at zero too. A node that has applied nothing reports, by omission alone, "height 0, not
#     catching up", which read literally announces a finished sync at block zero. So the claim is
#     bounded from below: caught up requires 0 < height, not merely "not catching up".
_sync_state(){
  local f h c state
  f="$(_status_fields "$1")"
  case "$f" in ''|NOSTATUS*) printf 'unreadable ? ?\n'; return 0;; esac
  h="${f%% *}"; c="${f##* }"
  case "$h" in ''|*[!0-9]*) printf 'unreadable ? ?\n'; return 0;; esac
  case "$c" in true|false|absent) : ;; *) printf 'unreadable ? ?\n'; return 0;; esac
  if [ "$c" != "true" ] && [ "$h" -gt 0 ]; then state=synced; else state=syncing; fi
  printf '%s %s %s\n' "$state" "$h" "$c"
}

# ---- probes ----------------------------------------------------------------------------------------
# stderr is captured, not discarded: some SDK builds print `status` on stderr, and `2>/dev/null` on a
# probe turns "the answer went to the other stream" into "the node never answered". Everything before
# the first brace is dropped by the reader, so a docker warning in front of the document is harmless.
_probe_node_status(){ docker compose -f "$NODE_KIT/docker-compose.yml" exec -T node dendrad status 2>&1; }

# The network head, from the operator's public RPC. Best effort by design: it decorates the report and
# never decides anything, so `?` (not a number, not a zero) is what an unanswered RPC yields.
_probe_net_head(){
  local rpc raw line
  rpc="${DENDRA_NODE:-}"; [ -n "$rpc" ] || { printf '?'; return 0; }
  rpc="${rpc/tcp:\/\//http://}"
  raw="$(curl -fsS -m 8 "$rpc/status" 2>/dev/null)" || { printf '?'; return 0; }
  line="$(_sync_state "$raw")"
  case "$line" in unreadable*) printf '?';; *) line="${line#* }"; printf '%s' "${line%% *}";; esac
}

# The bench has to drive the shipped loop, not a copy of it: a bench that re-implements the decision
# proves things about the copy. So the probe, the head and the clock are indirected. The indirection is
# honoured only under DENDRA_SELFTEST=1, and none of these names is in the CONFIG_URL allow-list, so no
# downloaded network-info.txt can reach them.
_sync_probe(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_PROBE:-}" ]; then "$DENDRA_SELFTEST_PROBE"; return 0; fi
  _probe_node_status
}
_sync_head(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_HEAD:-}" ]; then printf '%s' "$DENDRA_SELFTEST_HEAD"; return 0; fi
  _probe_net_head
}
_sync_now(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_CLOCK:-}" ]; then
    cat "$DENDRA_SELFTEST_CLOCK" 2>/dev/null || printf '0'
    return 0
  fi
  date +%s
}
_sync_nap(){ [ "${DENDRA_SELFTEST:-0}" = "1" ] && return 0; sleep "$1"; }

# ---- State sync, seen from the log: what a height of 0 cannot say -----------------------------------------
# While a snapshot is restored the node reports height 0, for as long as the restore takes, and then
# jumps to the snapshot's height. Read from the height alone, a restore is a stall. What moves meanwhile is
# the node's log, so at height 0 a new restore line counts as progress and resets the stall clock.
# The lines are CometBFT's own (statesync/syncer.go). A progress line only ever extends the wait. A failure
# line can shorten it, and that is a deliberate decision. wait_for_sync stops on two conditions together:
# at least DENDRA_SYNC_RESTORE_FAILS failure lines in the window it reads (the last DENDRA_SYNC_LOG_TAIL
# lines of the log), and no advance line after the last of them. An advance line (a snapshot accepted, a
# chunk fetched or applied, the app verified, the snapshot restored) is written only while a snapshot is
# being restored. So three snapshots rejected and a fourth one restoring is not stopped: its chunks come
# after the last failure. A restore that keeps failing is stopped by a read that falls between one
# attempt's failure and the next attempt's acceptance; a read that falls inside an attempt waits for the
# next one. A phrasing that changes with a CometBFT release costs patience (the stall clock runs as before)
# or reads as no failure at all; it cannot invent one. --timestamps makes every line distinct, so "a new
# line" is a comparison of the last one with the last one seen.
SS_LOG_PROGRESS='Discovered new snapshot|Offering snapshot to ABCI app|Snapshot accepted, restoring|Fetching snapshot chunk|Applied snapshot chunk to ABCI app|Verified ABCI app|Snapshot restored'
SS_LOG_ADVANCE='Snapshot accepted, restoring|Fetching snapshot chunk|Applied snapshot chunk to ABCI app|Verified ABCI app|Snapshot restored'
SS_LOG_FAILURE='Snapshot rejected|Snapshot format rejected|Snapshot senders rejected|Snapshot sender rejected|Timed out waiting for snapshot chunks|Timed out validating snapshot|failed to fetch and verify|appHash verification failed|No valid peers found for snapshot'
SS_LOG_DISCOVERY='Discovering snapshots'
# Same indirection as the probes above; under DENDRA_SELFTEST=1 without the seam it reads nothing, so a
# bench never reads the logs of a real node that happens to share the project name.
_sync_logs(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ]; then
    [ -n "${DENDRA_SELFTEST_SYNCLOGS:-}" ] || return 1
    "$DENDRA_SELFTEST_SYNCLOGS"; return $?
  fi
  docker compose -f "$NODE_KIT/docker-compose.yml" logs --timestamps --no-color --tail "$DENDRA_SYNC_LOG_TAIL" node 2>/dev/null
}
# _ss_log_scan -> SS_LOG (restored|offered|discovering|silent|unread), SS_LOG_LAST (the last restore line),
# SS_LOG_FAIL (the last restore failure line), SS_LOG_NFAIL (how many failure lines the window holds),
# SS_LOG_FAIL_OPEN (1 when no advance line follows the last failure line, 0 otherwise or without failure).
# Four states read, and `unread` is not `silent`.
_ss_log_scan(){
  local lg nf na
  SS_LOG=unread; SS_LOG_LAST=""; SS_LOG_FAIL=""; SS_LOG_NFAIL=0; SS_LOG_FAIL_OPEN=0
  lg="$(_sync_logs)" || return 0
  [ -n "$lg" ] || return 0
  SS_LOG_LAST="$(printf '%s\n' "$lg" | grep -E "$SS_LOG_PROGRESS" | tail -1)"
  SS_LOG_FAIL="$(printf '%s\n' "$lg" | grep -E "$SS_LOG_FAILURE" | tail -1)"
  # `grep -c` prints 0 and returns 1 when nothing matches: the count is read, never completed by a second 0.
  SS_LOG_NFAIL="$(printf '%s\n' "$lg" | grep -cE "$SS_LOG_FAILURE")"
  # Line numbers of the last failure and of the last advance line, 0 when there is none.
  nf="$(printf '%s\n' "$lg" | grep -nE "$SS_LOG_FAILURE" | tail -1 | cut -d: -f1)"
  na="$(printf '%s\n' "$lg" | grep -nE "$SS_LOG_ADVANCE" | tail -1 | cut -d: -f1)"
  if [ -n "$nf" ] && [ "$nf" -gt "${na:-0}" ]; then SS_LOG_FAIL_OPEN=1; fi
  if printf '%s\n' "$lg" | grep -qF 'Snapshot restored'; then SS_LOG=restored
  elif [ -n "$SS_LOG_LAST" ]; then SS_LOG=offered
  elif printf '%s\n' "$lg" | grep -qF "$SS_LOG_DISCOVERY"; then SS_LOG=discovering
  else SS_LOG=silent; fi
  return 0
}

# ---- The fork detector -----------------------------------------------------------------------------
# A node can be up, peered, and never able to follow this chain. When the binary it was built from
# applies a block to a different result than the network did, CometBFT rejects that block, drops the
# peer, reconnects, and starts over, forever. The height freezes. From the inside this looks like a
# slow disk or a slow link, so a sync wait that reads only height and catching_up cannot tell the two
# apart, and reports the reassuring one.
# A single transaction is enough to cause it: one the chain refuses and a stale binary accepts leaves
# the two sides holding different state from that block onward.
#
# The obvious comparison is a false negative.
# Comparing the block headers this node already holds with the network's would always agree: a node
# stops at the last block it accepted, so every header it owns predates the disagreement. The value that
# differs is the state computed after that block. A CometBFT header for height h carries the application
# hash of the state before h was applied (what height h-1 left behind); the state after h is published
# only in the header of h+1. On this node, the state after its last block is answered by the application
# itself, in /abci_info, and the height it belongs to comes in the same document. So the comparison is
# asymmetric on purpose:
#     local  /abci_info         -> response.last_block_height    h, and
#                                  response.last_block_app_hash  (base64: state after h, as computed here)
#     public /block?height=h+1  -> block.header.app_hash          (hex: state after h, as the network published it)
#
# Not /status. Its `sync_info.latest_app_hash` is the app_hash of the header at latest_block_height, the
# state after h-1, one block earlier than the network side above. Read here, it makes every healthy node
# whose height stays still for one report interval compare two different states and report a fork, with
# the advice not to bond and `down -v` in the report. On the public RPC, at one height,
# /status latest_app_hash equals the header of that height, and /abci_info last_block_app_hash, decoded,
# equals the header of the next one. The fixtures below are those documents.
#
# Three answers, never two. `unknown` is a verdict of its own and is never folded into `agree`, nor into
# `fork`: an unread side costs nothing on a path that is already failing, while a guess costs an operator
# a bond placed on a fork, or a node home destroyed for a disagreement that does not exist.

# _apphash RAW -> the application hash in the header of a /block document, uppercased, or NOHASH.
#                 The network half only. A /status document is not read here, on purpose: its
#                 `latest_app_hash` is a header's hash too, but the header of this node's last block,
#                 the state one block earlier than the one compared (see above). Any document that is
#                 not a block answers NOHASH, and fixture S below holds every branch to it.
_FORK_JSON=""
_apphash(){
  local raw v; raw="$1"
  # Same prologue as `_status_fields`, for the same reason: a probe's stream can carry warnings ahead of
  # the document ("the attribute `version` is obsolete"), and a reader without this line returns NOHASH,
  # i.e. `unknown`, on a good document. Measured: the clean document answered, the same document behind
  # one warning line did not.
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf 'NOHASH\n'; return 0;; esac
  case "$_FORK_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import json,re,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("NOHASH"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
v=""
b=d.get("block") if isinstance(d,dict) else None
h=b.get("header") if isinstance(b,dict) else None
if isinstance(h,dict) and isinstance(h.get("app_hash"),str): v=h["app_hash"]
print(v.upper() if re.fullmatch("[0-9A-Fa-f]+",v) else "NOHASH")
' 2>/dev/null
      ;;
    jq)
      { printf '%s' "$raw" | jq -r '
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | ((($d | .block? | .header? | .app_hash?) // "")) as $v
        | if ($v|type)!="string" or ($v|length)==0 then "NOHASH"
          elif ([$v|explode|.[]|select((. < 48 or . > 57) and (. < 65 or . > 70) and (. < 97 or . > 102))]|length)>0 then "NOHASH"
          else ($v|ascii_upcase) end' 2>/dev/null || printf 'NOHASH\n'; } | head -1
      ;;
    *)
      # Read by its key, never by counting occurrences, and the first occurrence, never the last. In a
      # /block document the header comes first; a later "app_hash" can only belong to another header
      # (one carried by evidence), and a greedy match would read that one.
      v="$(printf '%s' "$raw" | grep -oE '"app_hash"[[:space:]]*:[[:space:]]*"[0-9A-Fa-f]+"' | head -1 | cut -d'"' -f4)"
      if [ -n "$v" ]; then printf '%s' "$v" | tr 'a-f' 'A-F'; printf '\n'; else printf 'NOHASH\n'; fi
      ;;
  esac
}

# _abci_pair RAW -> "<height> <HASH>" out of one /abci_info document: the height of the last block the
#                   application committed and the hash of the state that block left, decoded from base64
#                   to uppercase hex; "<height> NOHASH" when the hash is absent or does not decode; NOABCI
#                   when the document is not an /abci_info answer or its height does not read.
#   Zero rule: `last_block_height` is an int64 that proto3 omits at zero, so an absent height inside an
#   answer that parsed reads 0 ("nothing committed", which the verdict refuses), never a height borrowed
#   from elsewhere. An empty hash is omitted the same way and reads NOHASH.
#   Not jq's `@base64d`. It decodes into a UTF-8 string, so every byte that is not valid UTF-8 comes back
#   as U+FFFD: measured with the jq of the chain image, the first three bytes of a real hash read
#   [34, 65533, 65533]. Two different hashes could then read the same, a fork read as an agreement.
#   Every branch decodes the 6-bit groups itself, and the fixtures hold each one to a real answer.
_abci_pair(){
  local raw h v x core pad ok
  raw="$1"
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf 'NOABCI\n'; return 0;; esac
  case "$_FORK_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import base64,binascii,json,re,sys
raw=sys.stdin.read()
try: d,_=json.JSONDecoder().raw_decode(raw)
except Exception: print("NOABCI"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
r=d.get("response") if isinstance(d,dict) else None
if not isinstance(r,dict): print("NOABCI"); raise SystemExit(0)
h=r.get("last_block_height","0")
if isinstance(h,bool) or not isinstance(h,(str,int)) or not re.fullmatch("[0-9]+",str(h)):
    print("NOABCI"); raise SystemExit(0)
v=r.get("last_block_app_hash","")
x="NOHASH"
if isinstance(v,str) and v:
    try: b=base64.b64decode(v+"="*(-len(v)%4),validate=True)
    except (binascii.Error,ValueError): b=b""
    if b: x=b.hex().upper()
print(str(int(str(h)))+" "+x)
' 2>/dev/null
      ;;
    jq)
      { printf '%s' "$raw" | jq -r '
        def b64hex:
          (rtrimstr("=") | rtrimstr("=") | explode
           | map(if . >= 65 and . <= 90 then . - 65
                 elif . >= 97 and . <= 122 then . - 71
                 elif . >= 48 and . <= 57 then . + 4
                 elif . == 43 then 62 elif . == 47 then 63 else -1 end)) as $q
          | if ($q|length) == 0 or ($q|length) % 4 == 1 or ([$q[] | select(. < 0)] | length) > 0 then ""
            else [range(0; $q|length; 4) as $i
                  | $q[$i:$i+4] as $g
                  | (($g + [0,0,0])[0:4]) as $p
                  | ($p[0]*262144 + $p[1]*4096 + $p[2]*64 + $p[3]) as $n
                  | ([($n/65536|floor), (($n/256|floor) % 256), ($n % 256)][0:(($g|length)-1)])[]]
                 | map([(./16|floor), (. % 16)] | map(if . < 10 then 48 + . else 55 + . end) | implode)
                 | join("")
            end;
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | (($d | .response?) // null) as $r
        | if ($r|type)!="object" then "NOABCI"
          else (($r.last_block_height // "0") | if type=="number" then tostring elif type=="string" then . else "x" end) as $h
          | if ($h|length)==0 or ([$h|explode|.[]|select(. < 48 or . > 57)]|length)>0 then "NOABCI"
            else ($r.last_block_app_hash // "") as $v
            | (if ($v|type)=="string" and ($v|length)>0 then ($v|b64hex) else "" end) as $x
            | ($h|tonumber|tostring) + " " + (if $x=="" then "NOHASH" else $x end)
            end
          end' 2>/dev/null || printf 'NOABCI\n'; } | head -1
      ;;
    *)
      # By key, from the text, when neither parser is there. Height and hash come out of the same text,
      # so they still describe one instant. Decoding needs `base64` and `od`: without them this branch
      # answers NOHASH, fails its fixture, and the verdict is `unknown`: reported, never guessed.
      case "$raw" in *'"response"'*) : ;; *) printf 'NOABCI\n'; return 0;; esac
      h=0
      case "$raw" in
        *'"last_block_height"'*)
          h="$(printf '%s' "$raw" | grep -oE '"last_block_height"[[:space:]]*:[[:space:]]*"?[0-9]+"?[[:space:]]*[,}]' | head -1 | tr -dc '0-9')"
          [ -n "$h" ] || { printf 'NOABCI\n'; return 0; }
          h="${h#"${h%%[!0]*}"}"; [ -n "$h" ] || h=0
          ;;
      esac
      v="$(printf '%s' "$raw" | grep -oE '"last_block_app_hash"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4)"
      x=NOHASH
      case $(( ${#v} % 4 )) in 2) v="$v==";; 3) v="$v=";; esac
      core="${v%%=*}"; pad="${v#"$core"}"; ok=1
      [ -n "$core" ] || ok=0
      case "$core" in *[!A-Za-z0-9+/]*) ok=0;; esac
      case "$pad" in ''|=|==) : ;; *) ok=0;; esac
      [ $(( ${#v} % 4 )) -eq 0 ] || ok=0
      { command -v base64 >/dev/null 2>&1 && command -v od >/dev/null 2>&1; } || ok=0
      if [ "$ok" = 1 ]; then
        x="$(printf '%s' "$v" | base64 -d 2>/dev/null | od -A n -v -t x1 | tr -dc '0-9a-f' | tr 'a-f' 'A-F')"
        # The decoded length is derived from the text and checked: a decoder that stopped half-way, and
        # said so on a stream nobody reads, must not hand over a shorter hash as if it were whole.
        { [ -n "$x" ] && [ "${#x}" -eq $(( (${#v} / 4 * 3 - ${#pad}) * 2 )) ]; } || x=NOHASH
      fi
      printf '%s %s\n' "$h" "$x"
      ;;
  esac
}

# The readers are checked before they are trusted. A and S are the answers of one node at one height,
# B the header of the next block, all three read from the public RPC at one height (S and B trimmed to the
# fields that matter; B lowercased on purpose -- nothing guarantees two sources agree on case, and a
# case-sensitive comparison on hex invents forks). What each must answer:
#   A -> its height and its decoded hash, which is B's: a real agreement, read end to end;
#   S -> nothing, from either reader: its hash is the header of its own height, and reading it is the
#        defect described above;
#   N -> nothing: a reader that returned the empty string on a document carrying no hash would make two
#        unreadable sides compare equal -- a fork reported as an agreement, on the reassuring side;
#   E -> its height and NOHASH: an answer whose hash is omitted carries no hash.
_FORK_FIX_A='{"jsonrpc":"2.0","id":-1,"result":{"response":{"data":"dendra","version":"v0.1.7-81-g20e34d4","last_block_height":"25507","last_block_app_hash":"IpSBD5xbhEDUKjDwPpmYyySbPJiJu3P2AgnnwcGAOjI="}}}'
_FORK_FIX_B='{"jsonrpc":"2.0","id":-1,"result":{"block_id":{"hash":"497996439DBE5DB6DF90B89434508FC90CE66FDF7E50B32743EF156F4F0FA73C"},"block":{"header":{"height":"25508","app_hash":"2294810f9c5b8440d42a30f03e9998cb249b3c9889bb73f60209e7c1c1803a32"}}}}'
_FORK_FIX_S='{"jsonrpc":"2.0","id":-1,"result":{"sync_info":{"latest_block_hash":"951CE8AFB5191E357AFE9B56CFAB5BB7DA8CC4E698A97C6B82F5DF3CDD8D3610","latest_app_hash":"96E352E52D4D21F593898302C2ACE6AED2F2D3BE4EE35C4EFE1B386E0DA7936C","latest_block_height":"25507","catching_up":false}}}'
_FORK_FIX_N='{"jsonrpc":"2.0","id":-1,"result":{"node_info":{"network":"dendra"}}}'
_FORK_FIX_E='{"jsonrpc":"2.0","id":-1,"result":{"response":{"data":"dendra","last_block_height":"25507"}}}'
_FORK_FIX_X=2294810F9C5B8440D42A30F03E9998CB249B3C9889BB73F60209E7C1C1803A32
_fork_reader_ok(){
  [ "$(_abci_pair "$_FORK_FIX_A")" = "25507 $_FORK_FIX_X" ] || return 1
  [ "$(_apphash "$_FORK_FIX_B")" = "$_FORK_FIX_X" ] || return 1
  [ "$(_apphash "$_FORK_FIX_S")" = "NOHASH" ] || return 1
  [ "$(_abci_pair "$_FORK_FIX_S")" = "NOABCI" ] || return 1
  [ "$(_apphash "$_FORK_FIX_N")" = "NOHASH" ] || return 1
  [ "$(_abci_pair "$_FORK_FIX_N")" = "NOABCI" ] || return 1
  [ "$(_abci_pair "$_FORK_FIX_E")" = "25507 NOHASH" ] || return 1
}
_FORK_READER_PICKED=0
_FORK_READER_RC=1
_fork_pick_reader(){
  [ "$_FORK_READER_PICKED" = 1 ] && return "$_FORK_READER_RC"
  local c
  _FORK_READER_PICKED=1
  for c in python3 jq __by_key__; do
    case "$c" in
      python3|jq) command -v "$c" >/dev/null 2>&1 || continue; _FORK_JSON="$c";;
      *) _FORK_JSON="";;
    esac
    if _fork_reader_ok; then _FORK_READER_RC=0; return 0; fi
    warn "the '$c' app-hash reader failed its own fixture (a document whose answer is known) -> trying the next reader."
  done
  _FORK_JSON=""; _FORK_READER_RC=1
  warn "no app-hash reader on this host answered its fixtures: a frozen height will be reported as 'unknown', never as 'in agreement'."
  return 1
}

# Same indirection as the sync loop, same reason, same guard: honoured only under DENDRA_SELFTEST=1, and
# none of these names is in the CONFIG_URL allow-list, so no downloaded network-info.txt reaches them.
_fork_probe_block(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_BLOCK:-}" ]; then "$DENDRA_SELFTEST_BLOCK" "$1"; return $?; fi
  local rpc; rpc="${DENDRA_NODE:-}"; [ -n "$rpc" ] || return 1
  rpc="${rpc/tcp:\/\//http://}"
  curl -fsS -m 10 "$rpc/block?height=$1" 2>/dev/null
}
_fork_logs(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_LOGS:-}" ]; then "$DENDRA_SELFTEST_LOGS"; return 0; fi
  docker compose -f "$NODE_KIT/docker-compose.yml" logs --tail 200 node 2>/dev/null
}
# The local half, asked of the application through the node's own RPC, from inside the container: the
# image ships curl (docker/Dockerfile.node) and the RPC listens there whatever the host publishes. stderr
# is captured, as in _probe_node_status: an error text carries no document, so the reader answers NOABCI
# and the verdict stays `unknown` -- an image without curl costs the verdict, it never invents one.
_probe_node_abci(){ docker compose -f "$NODE_KIT/docker-compose.yml" exec -T node curl -fsS -m 10 http://127.0.0.1:26657/abci_info 2>&1; }
# Same indirection as _sync_probe, honoured only under DENDRA_SELFTEST=1, and the name is in no allow-list.
# And like _sync_logs, under DENDRA_SELFTEST=1 without the seam it reads nothing: a bench that forgets the
# seam must not query the real node that happens to share the project name.
_abci_probe(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ]; then
    [ -n "${DENDRA_SELFTEST_ABCI:-}" ] || return 0
    "$DENDRA_SELFTEST_ABCI"; return 0
  fi
  _probe_node_abci
}

FORK_STATE=unknown; FORK_WHY=""; FORK_LOCAL=""; FORK_NET=""; FORK_LOG=""; FORK_H=""; _FORK_SEEN_H=""; _FORK_TRIED_AT=0
# fork_verdict HEIGHT -> sets FORK_STATE to fork | agree | unknown. It always returns 0: it names a
# failure, it does not decide whether one happened.
# FORK_H is the height the comparison is about: the one /abci_info gave in the same answer as
# FORK_LOCAL, never the argument; empty while no such answer was read. Whatever prints FORK_LOCAL next to
# a height prints this one -- a height and a hash taken from two reads describe two instants.
fork_verdict(){
  local h hdoc doc pair next raw a b
  FORK_STATE=unknown; FORK_WHY=""; FORK_LOCAL=""; FORK_NET=""; FORK_LOG=""; FORK_H=""
  h="$1"
  case "$h" in ''|*[!0-9]*) FORK_WHY="the local height is not a number, so there is nothing to compare"; return 0;; esac
  [ "$h" -gt 0 ] || { FORK_WHY="this node has applied no block yet, so there is nothing to compare"; return 0; }
  _fork_pick_reader || { FORK_WHY="no reader on this host answered a document whose answer is known"; return 0; }
  # One read, one height, one hash, and the height comes from the document, never from the caller.
  # The height the loop holds was read on an earlier pass, from another document. Pairing it with a hash
  # read now compares two different instants, and reports a fork with total confidence on a node that is
  # merely making progress. /abci_info carries both in one answer; the argument only refuses height 0.
  doc="$(_abci_probe)"
  pair="$(_abci_pair "$doc")"
  case "$pair" in
    [0-9]*" "?*) : ;;
    *) FORK_WHY="this node's /abci_info could not be read, so the state it computed is unknown"; return 0;;
  esac
  hdoc="${pair%% *}"; a="${pair#* }"
  case "$hdoc" in ''|*[!0-9]*|0?*) FORK_WHY="this node's /abci_info carries an unreadable height, so nothing is concluded"; return 0;; esac
  [ "$hdoc" -gt 0 ] || { FORK_WHY="this node's /abci_info reports no committed block (an absent height is 0), so there is nothing to compare"; return 0; }
  FORK_H="$hdoc"
  case "$a" in ''|NOHASH|*[!0-9A-F]*) FORK_WHY="this node's /abci_info carries no readable application hash for height $hdoc"; return 0;; esac
  h="$hdoc"
  next=$(( hdoc + 1 ))
  raw="$(_fork_probe_block "$next")" || raw=""
  [ -n "$raw" ] || { FORK_WHY="block $next was not served, so the network's answer is unknown -- which is not an agreement"; return 0; }
  b="$(_apphash "$raw")"
  case "$b" in ''|NOHASH) FORK_WHY="block $next as served carries no application hash"; return 0;; esac
  FORK_LOCAL="$a"; FORK_NET="$b"
  if [ "$a" = "$b" ]; then
    FORK_STATE=agree
    FORK_WHY="both sides hold $a after height $h: this node is behind, nothing more"
  else
    FORK_STATE=fork
    FORK_WHY="after height $h this node computes $a while the network published $b"
    # Corroboration, never the measurement: the node usually says it itself, but a phrasing that
    # changes with the CometBFT release must never be what decides.
    FORK_LOG="$(_fork_logs | grep -m1 -F 'wrong Block.Header.AppHash' || true)"
  fi
  return 0
}

# ---- tunables --------------------------------------------------------------------------------------
# Named in the unit each one actually counts, and overridable — because the right value depends on a
# disk, not on an opinion held here.
#   stall (DENDRA_SYNC_STALL_SECS): how long the height may fail to increase before this script stops waiting. It is not a
#           budget for the sync; the sync may take all day as long as it advances.
#   max (DENDRA_SYNC_MAX_SECS): an absolute ceiling so an unattended run cannot loop forever. Deliberately far above any
#           plausible replay (24 h) — it is a stop, not a verdict.
DENDRA_SYNC_STALL_SECS="${DENDRA_SYNC_STALL_SECS:-900}"
DENDRA_SYNC_MAX_SECS="${DENDRA_SYNC_MAX_SECS:-86400}"
DENDRA_SYNC_POLL_SECS="${DENDRA_SYNC_POLL_SECS:-15}"
DENDRA_SYNC_REPORT_SECS="${DENDRA_SYNC_REPORT_SECS:-120}"
#   RESTORE_FAILS : how many state-sync failure lines (a rejected snapshot, a chunk that timed out, a trust
#           point the light client could not verify) the window below may hold, with no advance line after
#           the last one, while the height is 0 (see _ss_log_scan). A healthy restore logs none; CometBFT
#           retries a failed one on its own, and its retries look like progress to the stall clock. An order
#           of magnitude, owned as such: a retry passes, a loop stops.
#   LOG_TAIL : the window: how many of the node's last log lines are read for those lines. Failure lines
#           pushed out of it by a talkative restore are no longer counted; the stall clock still runs.
#   Both must be positive integers: anything else is refused (sync_settings_check), never read as zero.
DENDRA_SYNC_RESTORE_FAILS="${DENDRA_SYNC_RESTORE_FAILS:-3}"
DENDRA_SYNC_LOG_TAIL="${DENDRA_SYNC_LOG_TAIL:-400}"
# sync_settings_check -> 0, or 1 with SYNC_SETTINGS_WHY. Called before the node starts and again by
# wait_for_sync, so a value the loop cannot use is refused before anything is waited on.
sync_settings_check(){
  local k v
  SYNC_SETTINGS_WHY=""
  for k in DENDRA_SYNC_RESTORE_FAILS DENDRA_SYNC_LOG_TAIL; do
    eval "v=\"\${$k:-}\""
    case "$v" in
      ''|*[!0-9]*|0*) SYNC_SETTINGS_WHY="$k='$v' is not a positive integer"; return 1 ;;
    esac
  done
  return 0
}

# Observed rate, carried as hundredths of a block per second so the whole loop stays in integers.
SYNC_H="?"; SYNC_HEAD="?"; SYNC_RATE_CBS=""; SYNC_STALL_FOR=0; SYNC_ELAPSED=0; SYNC_WHY=""; SYNC_CU="?"

_fmt_rate(){
  case "${1:-}" in
    ''|*[!0-9]*) printf 'not measurable yet';;
    *) printf '%d.%02d blocks/s' $(( $1 / 100 )) $(( $1 % 100 ));;
  esac
}

# announce_sync_plan: printed before the wait, because the cost of waiting is only bearable when it was
# announced. The root cause of the wait is named here: the mode node_sync_plan chose, and why.
announce_sync_plan(){
  local head est
  _sync_pick_reader || true
  head="$(_sync_head)"
  say ""
  if [ "${NODE_SYNC_MODE:-}" = kept ]; then
    say "  [i] $NODE_SYNC_WHY."
    say "      The wait below is whatever is left between that home and the head of the network."
  elif [ "${NODE_SYNC_MODE:-}" != statesync ]; then
    say "  [i] This node replays the chain from block 1${NODE_SYNC_WHY:+ ($NODE_SYNC_WHY)}."
    say "      That is the whole of the wait below; it is not a fault of your machine or of your network."
    case "$head" in
      ''|*[!0-9]*)
        say "      The public RPC did not answer, so the size of the replay cannot be stated here." ;;
      *)
        est=$(( head * 100 / 2307 / 60 ))
        say "      Network head right now: $head blocks. At 23.07 blocks applied per second (a rate"
        say "      measured over 90 s on one host, and the only figure that exists before"
        say "      your node has applied anything), that is roughly ${est} min of replay. Your disk and"
        say "      your peers decide the real number; it is printed below, from your own node." ;;
    esac
    # Printed in the replay branch only: it describes a replay ("while it replays"), and printed before a state
    # sync or a kept home it announced a flood of a replay that does not happen.
    say "  [i] Expect a flood of red lines in the node logs while it replays:"
    say "      'ERR ... SECURITY: decentralized VRF seed UNDER-DECENTRALIZED ... LEGACY fallback'."
    say "      That line is emitted by the chain's jobs keeper (decentralized seed) for a block whose"
    say "      committee draw had too few anchored VRF contributors, including the current block: the"
    say "      keeper computes that seed at every EndBlock, so the line does not stop when your replay"
    say "      catches up. It stops when enough validators have anchored a VRF key, not when you sync."
  else
    say "  [i] State sync: the node starts from a snapshot a peer offers, verified against the trust point"
    say "      at block ${STATESYNC_TRUST_HEIGHT:-?} (derived over TLS from docker/STATESYNC_RPC), instead of replaying the chain."
    say "      Its height reads 0 while the snapshot is restored, then jumps to the snapshot's height: the"
    say "      restore lines in the node's log are what this script counts as progress meanwhile."
  fi
  say ""
}

sync_progress_line(){
  local h="$1" head="$2" pct="" eta="" left
  local r; r="$(_fmt_rate "$SYNC_RATE_CBS")"
  case "$head" in
    ''|*[!0-9]*) head="?";;
    *) [ "$head" -gt 0 ] && pct=" ($(( h * 100 / head )) %)";;
  esac
  case "$SYNC_RATE_CBS" in
    ''|*[!0-9]*) : ;;
    0) : ;;
    *) if [ "$head" != "?" ] && [ "$head" -gt "$h" ]; then
         left=$(( head - h ))
         eta=" -> ~$(( left * 100 / SYNC_RATE_CBS / 60 )) min left if this node holds the rate it has held so far"
       fi ;;
  esac
  say "  [..] height $h / network head $head$pct · $r sustained by this node$eta"
}

sync_failure_report(){
  say ""
  say "  ------------------------------------------------------------------"
  if [ "${FORK_STATE:-unknown}" = "fork" ]; then
    say "  Stop: this node is not behind. It disagrees with the chain."
    say ""
    say "  After applying height $FORK_H the two sides do not hold the same application state:"
    say "    this node computed    : $FORK_LOCAL"
    say "    the network published : $FORK_NET"
    [ -n "$FORK_LOG" ] && say "    the node says so itself: $FORK_LOG"
    say ""
    say "  Re-running this command will not help, and neither will waiting. A node that computes a"
    say "  different state rejects the block, drops the peer, reconnects, and starts over, forever."
    say "  Do not bond on this node: a bond placed here is placed on a fork, not on this chain."
    say ""
    say "  What it means in practice: the binary this node runs was not built from the source this chain"
    say "  runs. The two can be out of step in either direction, and the remedy is not the same:"
    say "    · the public mirror is behind the chain -> rebuilding from it changes nothing;"
    say "    · the public mirror is ahead of the chain (a consensus change is published but the"
    say "      operator has not restarted the network on it yet) -> rebuilding from it reproduces"
    say "      this fork, every time."
    say "  A blind 'reset --hard origin/main' is wrong in both cases: on a"
    say "  fresh clone it is a no-op that changes nothing, and on a modified clone it destroys work"
    say "  without fixing anything. It also invites an endless loop: rebuild, fork, wipe, repeat."
    say ""
    say "  Ask the operator which commit the chain is running before rebuilding anything. Until that"
    say "  answer matches your source, no rebuild can help. This node's data must go regardless: it"
    say "  was written by the wrong state machine, and keeping it keeps the disagreement."
    say ""
    say "  Back the keys up before the next line, and read why."
    say "  \`down -v\` deletes the volume, and this volume is not only chain data: it is the node home."
    say "  It holds config/priv_validator_key.json, config/vrf_key, and keyring-test/ -- the operator key"
    say "  that controls any stake this validator has bonded. Chain data can be downloaded again from"
    say "  any peer; that key cannot. Losing it does not merely mean re-registering: an undelegation is"
    say "  a transaction signed by that key, so a bonded stake whose key is gone can never be recovered"
    say "  by anyone, including us. Nothing on this network can undo it."
    say "  Copy them out first -- nothing is printed to the screen, the files land beside you:"
    say "    ssh <this host> \"docker run --rm -v \$(docker volume inspect ${DENDRA_PROJECT:-dendra-node}_node-data -f '{{.Name}}'):/d -w /d busybox tar cz config/priv_validator_key.json config/node_key.json config/vrf_key keyring-test\" > dendra-node-keys.tgz"
    say "    tar tzf dendra-node-keys.tgz     # must list priv_validator_key.json before you continue"
    say ""
    say "  Then, and only then:"
    say "    docker compose -f $NODE_KIT/docker-compose.yml down -v"
    say "  Do not re-run this command until your source has changed. Wiping the volume and rejoining"
    say "  with the same binary reproduces the same fork and destroys the volume again on every pass."
    say "  Once the source matches what the operator names, rebuild, then re-run and restore the keys."
    say "  ------------------------------------------------------------------"
    say ""
    return 0
  fi
  # A node at height 0 whose log shows a state sync is not a slow replay, and the generic report below --
  # "re-run, it resumes" -- would be wrong for it: a home created for a state sync keeps trying one at every
  # start. So the cause read in the log is named, and the way to a replay is given.
  if [ "${SYNC_H:-?}" = 0 ] && { [ "${SS_LOG:-}" = discovering ] || [ "${SS_LOG:-}" = offered ]; }; then
    if [ "$SS_LOG" = discovering ]; then
      say "  State sync: no snapshot offered."
      say "  The node searched for snapshots ('Discovering snapshots' in its log) and no peer it reached"
      say "  offered one. CometBFT does not fall back to a replay on its own: the node stays at height 0."
      say "  Snapshots are served by nodes that write them (snapshot-interval in their app.toml) and that this"
      say "  node is peered with (SEEDS / PERSISTENT_PEERS in $NODE_KIT/.env)."
      [ -n "${SS_LOG_FAIL:-}" ] && say "    last failure line : $(printf '%s' "$SS_LOG_FAIL" | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-200)"
    else
      say "  State sync: a snapshot was offered, and its restore stopped moving."
      say "    last restore line : $(printf '%s' "${SS_LOG_LAST:-?}" | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-200)"
      [ -n "${SS_LOG_FAIL:-}" ] && say "    last failure line : $(printf '%s' "$SS_LOG_FAIL" | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-200)"
      say "  The node keeps trying on its own; whether a later attempt succeeds depends on the peers and on the"
      say "  trust point (an RPC of docker/STATESYNC_RPC that stopped answering fails the light client)."
    fi
    say ""
    say "    why it stopped       : $SYNC_WHY"
    say "    at height 0 since the last restore progress : $SYNC_STALL_FOR s (DENDRA_SYNC_STALL_SECS)"
    say "    trust point written  : block ${STATESYNC_TRUST_HEIGHT:-<none>} (empty = this run did not configure the state sync)"
    say ""
    say "  To replay from block 1 instead: this home holds no block yet, and it was created for a state sync,"
    say "  which it retries at every start: re-running this command with it changes nothing. Remove it, then"
    say "  join with --replay:"
    say "    docker compose -f $NODE_KIT/docker-compose.yml down -v"
    say "    bash $REPO/deploy/join.sh --replay   (with the other options of this run)"
    say "  Warning: down -v deletes the node home, its keys included. A home this run created holds keys nothing has"
    say "  used yet; if you have used them since (a validator operator key in keyring-test/), copy config/ and"
    say "  keyring-test/ out first."
    say "  ------------------------------------------------------------------"
    say ""
    return 0
  fi
  say "  This script stopped waiting. The node did not stop syncing."
  say "  The container is still up and still working. The chain data already downloaded lives in the"
  say "  Docker volume, so nothing here is lost and a re-run resumes where this left off."
  say ""
  say "  What was observed (a report, not a diagnosis of your network):"
  say "    height reached       : $SYNC_H"
  say "    network head (RPC)   : $SYNC_HEAD   ('?' = the public RPC did not answer, so it is unknown)"
  say "    catching_up field    : $SYNC_CU   ('?' = the status document could not be read at all)"
  say "    rate this node held  : $(_fmt_rate "$SYNC_RATE_CBS")"
  say "    height last moved    : $SYNC_STALL_FOR s ago"
  say "    age of that block    : $SYNC_BLOCK_AGE s   ('?' = the timestamp could not be read; a large"
  say "                           number means the chain itself stopped producing, which is not a"
  say "                           problem with this node and no re-run will fix it)"
  say "    total wait           : $SYNC_ELAPSED s"
  say "    why it stopped       : $SYNC_WHY"
  say "    state comparison     : $FORK_STATE   ($FORK_WHY)"
  say ""
  say "  Look for yourself, in this order:"
  say "    docker compose -f $NODE_KIT/docker-compose.yml exec -T node dendrad status"
  say "    docker compose -f $NODE_KIT/docker-compose.yml logs --tail 80 node"
  say "  Then simply re-run this command: it picks the database up where it is."
  say "  ------------------------------------------------------------------"
  say ""
}

# wait_for_sync — returns 0 when the node has caught up, 1 when this script stopped waiting.
# _set_block_age DOC -- refreshes SYNC_BLOCK_AGE from the status document of this pass.
#
# Recomputed every pass, not only on the happy exit, because the report printed when the script gives
# up needs it just as much: "the height has not moved for 900 s" and "the last block this node holds
# is eight hours old" are the same fact seen twice, and only the second one names a chain that stopped.
#
# And it is deliberately not kept like the height is. An unreadable probe leaves the height, the
# stall clock and the rate untouched, because a height is a monotone fact that stays true. An age is
# not: it is a snapshot, and carrying the previous pass's value forward would print, next to a probe
# that answered nothing, an age that came from somewhere else. So an unreadable document yields `?`.
_set_block_age(){
  local stamp
  stamp="$(_status_block_epoch "$1")"
  case "$stamp" in
    ''|*[!0-9]*) SYNC_BLOCK_AGE="?" ;;
    *)           SYNC_BLOCK_AGE=$(( $(_sync_now) - stamp )) ;;
  esac
}

# _caught_up_line HEIGHT HEAD DOC: the end-of-sync announcement, in one place only.
# It was written twice, at both exits of the `synced` branch. Two copies of a concluding sentence
# is a sentence that gets half-corrected: the second one keeps the older truth.
#
# It carries the age of the last block, and that is its whole purpose. A line reading "caught up at
# height 20583" is true of a node up to date with a chain that has produced nothing for hours, and
# the joiner reading it concludes the network works. Three possible announcements, never two:
#   age readable and short -> the age is given, catching up is good news;
#   age readable and long  -> the age is given and a warning says the chain is not advancing. Nothing
#                             blocks: the node has finished its work, there is nothing more to wait
#                             for, and looping the joiner on a network outage would leave them with
#                             no diagnosis. They are told instead, which is what was missing;
#   age unreadable         -> the age is announced unknown. Never omitted, never shown as fresh.
_caught_up_line(){
  local h="$1" head="$2" doc="$3" age
  # The age was measured by `_set_block_age` on this same pass, from this same document. Measuring it
  # again here would read the clock a second time and print an age that is not the one the loop just
  # judged on -- two numbers for one fact.
  _set_block_age "$doc"
  age="$SYNC_BLOCK_AGE"
  case "$age" in
    '?') say "  [OK] node caught up at height $h (network head ${head}); age of the last block: unknown on this host (no python3, no jq, and a non-GNU date), so the script cannot tell a live chain from a stopped one." ;;
    *)
      # The comparison was bounded from above only, and the other side is the dangerous one.
      # `age` is `now - block_time`, so it goes negative when the block is dated ahead of this
      # machine. Measured on this very loop, with a witness: a clock 24 h behind, on a chain whose last
      # block is from 2020, printed "last block -86400s old." with no warning and rc=0 -- a stopped
      # chain announced as healthy to a joiner. The witness: the same document with a correct clock
      # warns "the chain is not producing". The loop could judge; it was reading one side only.
      # The comment on the threshold above already names the other drift ("a host with a wrong date
      # sees an old block that is not old") -- this is its mirror, and it was the unnamed one.
      # Three states, not two: with a clock this far behind, a live chain and a stopped one are
      # indistinguishable from here, so freshness is not measurable -- it is not "fresh".
      # The bound is the existing threshold, used symmetrically: no second number to keep in step,
      # and a few seconds of ordinary NTP skew still pass without crying wolf.
      if [ "$(( age + DENDRA_STALE_BLOCK_SECS ))" -le 0 ]; then
        say "  [OK] node caught up at height $h (network head ${head})."
        warn "This machine's clock disagrees with the chain: the last block is dated $(( 0 - age ))s in the future of this host. Freshness cannot be judged from here -- with a clock this far behind, a live chain and a stopped one look the same. Fix the date on this machine, then read this line again."
      elif [ "$age" -ge "$DENDRA_STALE_BLOCK_SECS" ]; then
        say "  [OK] node caught up at height $h (network head ${head})."
        warn "The node is up to date but the chain is not producing: the last block is ${age}s old (over ${DENDRA_STALE_BLOCK_SECS}s). Being caught up says this node applied everything offered to it, never that anything is being offered. Check the validators before concluding the network works — or check this machine's clock, which is the other way this number gets large."
      else
        say "  [OK] node caught up at height $h (network head ${head}); last block ${age}s old."
      fi
      ;;
  esac
}

# It never exits on its own: the caller owns the fatal, and the bench can run the real loop.
wait_for_sync(){
  local t0 tnow line state h cu first_h first_t last_h stall_since head_at last_report gap cu_confrontable doc
  _sync_pick_reader || true
  t0="$(_sync_now)"
  first_h=-1; first_t="$t0"; last_h=-1; stall_since="$t0"; head_at=0; last_report="$t0"
  SYNC_H="?"; SYNC_HEAD="?"; SYNC_RATE_CBS=""; SYNC_STALL_FOR=0; SYNC_ELAPSED=0; SYNC_WHY=""; SYNC_CU="?"
  # SYNC_BLOCK_AGE is not reset here, and that is a decision rather than an omission: `_set_block_age`
  # runs on every pass of the loop below, including the very first, so nothing from an earlier call can
  # survive into this one. A reset here would be a line that can never change an outcome — and a guard
  # that cannot fail is a guard that lies about being tested. The protection lives where it bites.
  # Reset with the rest, and for the same reason: a verdict left over from an earlier call would be
  # reported as the outcome of this one. `unknown` is the state to start from, never `agree`.
  FORK_STATE=unknown; FORK_WHY=""; FORK_LOCAL=""; FORK_NET=""; FORK_LOG=""; _FORK_SEEN_H=""; _FORK_TRIED_AT=0
  # The state-sync reading of this call, never one left over from an earlier one.
  SS_LOG=""; SS_LOG_LAST=""; SS_LOG_FAIL=""; SS_LOG_NFAIL=0; SS_LOG_FAIL_OPEN=0; local ss_seen=""
  if ! sync_settings_check; then SYNC_WHY="$SYNC_SETTINGS_WHY"; return 1; fi
  say "  [..] waiting for the node to catch up. This script waits as long as the height keeps climbing;"
  say "       it gives up if no new block is applied for $(( DENDRA_SYNC_STALL_SECS / 60 )) min. At height 0 a"
  say "       new restore line of the node's log counts as progress, and a snapshot restore that failed"
  say "       DENDRA_SYNC_RESTORE_FAILS=$DENDRA_SYNC_RESTORE_FAILS times in its last $DENDRA_SYNC_LOG_TAIL log lines, with nothing restored since,"
  say "       stops the wait (the node itself keeps trying)."
  while :; do
    tnow="$(_sync_now)"; SYNC_ELAPSED=$(( tnow - t0 ))
    # The document is kept: the timestamp is read from the same status as the height. Probing the node
    # again would give two snapshots, and an age that does not match the height announced next to it.
    doc="$(_sync_probe)"
    line="$(_sync_state "$doc")"
    state="${line%% *}"; line="${line#* }"; h="${line%% *}"; cu="${line##* }"
    _set_block_age "$doc"
    SYNC_CU="$cu"
    # Re-derived on every pass. A reason left over from an earlier iteration would be reported as the
    # cause of a stop that happened for a different reason two hours later.
    SYNC_WHY=""

    # `unreadable` deliberately does not touch the height, the stall clock or the rate: an unreadable
    # probe is an absence of measurement, so nothing here may move on the strength of it. The stall
    # clock keeps running, which is exactly right — a probe that stays unreadable is a stop.
    if [ "$state" != unreadable ]; then
      SYNC_H="$h"
      [ "$first_h" -lt 0 ] && [ "$h" -gt 0 ] && { first_h="$h"; first_t="$tnow"; }
      if [ "$h" -gt "$last_h" ]; then last_h="$h"; stall_since="$tnow"; fi
    fi
    # Height 0 is where a restore lives: a new restore line in the log is progress (see _ss_log_scan).
    # But a restore that fails and starts over also writes new restore lines: a rejected snapshot is
    # discovered, fetched and applied again -- and would reset the stall clock forever. So the failures
    # are counted: DENDRA_SYNC_RESTORE_FAILS of them in the window read, with no advance line after the
    # last one (a restore that has moved on since is not stopped), and this script stops waiting.
    if [ "$state" != unreadable ] && [ "$h" = 0 ]; then
      _ss_log_scan
      if [ "${SS_LOG_FAIL_OPEN:-0}" = 1 ] && [ "$SS_LOG_NFAIL" -ge "$DENDRA_SYNC_RESTORE_FAILS" ]; then
        SYNC_STALL_FOR=$(( tnow - stall_since ))
        SYNC_WHY="state sync: the snapshot restore failed $SS_LOG_NFAIL times in the node's last $DENDRA_SYNC_LOG_TAIL log lines (DENDRA_SYNC_LOG_TAIL), with nothing restored after the last failure (DENDRA_SYNC_RESTORE_FAILS=$DENDRA_SYNC_RESTORE_FAILS) -- the node keeps retrying on its own"
        return 1
      fi
      if [ -n "$SS_LOG_LAST" ] && [ "$SS_LOG_LAST" != "$ss_seen" ]; then
        ss_seen="$SS_LOG_LAST"; stall_since="$tnow"
      fi
    fi
    SYNC_STALL_FOR=$(( tnow - stall_since ))

    # The only rate this script is entitled to quote: the one this node produced, over this run.
    if [ "$first_h" -ge 0 ] && [ "$last_h" -gt "$first_h" ] && [ $(( tnow - first_t )) -ge 30 ]; then
      SYNC_RATE_CBS=$(( ( last_h - first_h ) * 100 / ( tnow - first_t ) ))
    fi

    if [ $(( tnow - head_at )) -ge "$DENDRA_SYNC_REPORT_SECS" ] || [ "$head_at" = 0 ]; then
      SYNC_HEAD="$(_sync_head)"; head_at="$tnow"
    fi

    if [ "$state" = synced ]; then
      # One ambiguity is left, and it is named. `catching_up` reported as absent is read as false by the
      # proto3 rule, which is correct for an encoder that omits zeros — but it is an inference, not an
      # answer. So in that single case the claim is checked against the head of the network before it is
      # believed. An explicit `false` is the node's own answer and is taken as such.
      # The head is tested for being a number rather than for differing from the sentinel, because an
      # arithmetic expression on anything else prints a bash error and evaluates to 0 — a gap of zero,
      # i.e. the answer this branch exists to question, arrived at by accident.
      # And the comparison is a bonus, not a condition: when the head cannot be read the omission is
      # accepted, because the proto3 reading is correct on its own and because an operator who joins on
      # SEEDS alone has no public RPC to compare anything with. Requiring a head here would hang that
      # operator forever on a check that is only ever a second opinion.
      gap=0
      case "$SYNC_HEAD" in ''|*[!0-9]*) cu_confrontable=0;; *) cu_confrontable=1; gap=$(( SYNC_HEAD - h ));; esac
      if [ "$cu" = absent ] && [ "$cu_confrontable" = 1 ]; then
        if [ "$gap" -gt 100 ]; then
          SYNC_WHY="the node omits catching_up (read as 'not catching up') but sits $gap blocks under the network head — the claim is not believed on its own"
          warn "status says caught up by omission at height $h while the public RPC is at $SYNC_HEAD -> still waiting."
        else
          _caught_up_line "$h" "$SYNC_HEAD" "$doc"
          return 0
        fi
      else
        _caught_up_line "$h" "$SYNC_HEAD" "$doc"
        return 0
      fi
    fi

    # A frozen height is not a diagnosis, and 900 s is a long time to be told nothing.
    # The comparison runs as soon as the height has been still for one report interval: a node that
    # is merely behind pays one /abci_info read (a docker compose exec into the node) and one block read
    # per report interval it rests on, which is nothing, while a node that disagrees is told so in two
    # minutes instead of fifteen. And once
    # the two states differ there is nothing left to wait for, so this stops waiting.
    #
    # What is remembered is an answer, never an attempt. Written before the call, `_FORK_SEEN_H` would
    # compare a height exactly once whatever came back, including `unknown`,
    # which is the state this detector defines as "no answer" everywhere else. One missed read (the
    # public RPC hiccups, the block is not served for a second) would then retire the comparison
    # for the rest of the run: the operator would wait the full stall window and get the generic
    # message, "no new block applied", whose report tells them to re-run, which for a fork
    # is the one thing that cannot help. On the shipped loop with one missed answer this gives
    # FORK_STATE=unknown, one comparison, "no new block applied for 900 s"; with the answer served
    # on the first try, FORK_STATE=fork after 120 s. Same node, same disagreement, opposite verdicts.
    # So `unknown` is retried, and `_FORK_TRIED_AT` keeps the promise made just above: at most one
    # pair of reads per report interval, whatever the reason it did not conclude.
    if [ "$SYNC_STALL_FOR" -ge "$DENDRA_SYNC_REPORT_SECS" ] && [ "$_FORK_SEEN_H" != "$h" ] \
       && [ $(( tnow - _FORK_TRIED_AT )) -ge "$DENDRA_SYNC_REPORT_SECS" ]; then
      # 0 as the starting value is safe rather than lucky: this branch needs SYNC_STALL_FOR to have
      # reached the report interval, and the stall clock starts at t0, so tnow is already at least
      # one interval above zero the first time control gets here.
      _FORK_TRIED_AT="$tnow"
      fork_verdict "$h"
      [ "$FORK_STATE" = unknown ] || _FORK_SEEN_H="$h"
      if [ "$FORK_STATE" = fork ]; then
        SYNC_WHY="Consensus fork -- $FORK_WHY"
        return 1
      fi
    fi

    if [ "$SYNC_STALL_FOR" -ge "$DENDRA_SYNC_STALL_SECS" ]; then
      [ -n "$SYNC_WHY" ] || SYNC_WHY="no new block applied for ${SYNC_STALL_FOR} s (limit ${DENDRA_SYNC_STALL_SECS} s, DENDRA_SYNC_STALL_SECS)"
      if [ "$state" != unreadable ] && [ "$h" = 0 ]; then
        case "$SS_LOG" in
          discovering) SYNC_WHY="state sync: no snapshot offered by the peers this node reached, for ${SYNC_STALL_FOR} s -- the node stays at height 0 and does not fall back to a replay on its own" ;;
          offered)     SYNC_WHY="state sync: a snapshot was offered, and its restore has not moved for ${SYNC_STALL_FOR} s" ;;
        esac
      fi
      [ "$state" = unreadable ] && SYNC_WHY="the status document could not be read for ${SYNC_STALL_FOR} s (limit ${DENDRA_SYNC_STALL_SECS} s) — this is 'no answer', not 'not caught up'"
      return 1
    fi
    if [ "$SYNC_ELAPSED" -ge "$DENDRA_SYNC_MAX_SECS" ]; then
      SYNC_WHY="absolute ceiling reached (${DENDRA_SYNC_MAX_SECS} s, DENDRA_SYNC_MAX_SECS) while the height was still climbing — raise it and re-run, nothing is wrong"
      return 1
    fi

    if [ $(( tnow - last_report )) -ge "$DENDRA_SYNC_REPORT_SECS" ]; then
      last_report="$tnow"
      if [ "$state" = unreadable ]; then
        warn "status unreadable (${SYNC_STALL_FOR} s without a readable answer) — still trying, the container is up."
      elif [ "$h" = 0 ] && [ -n "$SS_LOG_LAST" ]; then
        say "  [..] restoring a snapshot (state sync; the height reads 0 until it is applied). Last restore line:"
        say "       $(printf '%s' "$SS_LOG_LAST" | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-160)"
      elif [ "$h" = 0 ] && [ "$SS_LOG" = discovering ]; then
        say "  [..] state sync: searching for a snapshot among this node's peers (none offered yet, ${SYNC_STALL_FOR} s)."
      else
        sync_progress_line "$h" "$SYNC_HEAD"
      fi
    fi
    _sync_nap "$DENDRA_SYNC_POLL_SECS"
  done
}

# ================================================================ state sync: a trust point anchored in the clone
# A new node either replays every block from the genesis -- a wait that grows with every block the chain
# produces -- or starts from a snapshot a peer offers. A snapshot is only as trustworthy as the light-client
# trust point it is verified against: CometBFT starts from that (height, hash) pair and never looks at the
# genesis again. Read from network-info.txt, over plain HTTP, from the host that serves everything
# else, that pair would let whoever can rewrite the file hand a new node a chain of their own and walk
# around docker/GENESIS_SHA256.
#
# The anchor comes with the clone: docker/STATESYNC_RPC names RPC servers by https URL, and arrives over
# HTTPS git like the genesis pin. At every join the trust point is derived from them:
#   1. each server's /status, over TLS: its network must be CHAIN_ID;
#   2. H = the lowest head among them, minus STATESYNC_TRUST_LAG_BLOCKS;
#   3. each server's /block?height=H: the block must be H, its hash 64 hex digits, and every server must
#      serve the same hash.
# Three outcomes, never two. `agree` -> the node state-syncs from (H, hash). `unknown` (a server that does
# not answer, a document that does not read, a block that is not H, a hash of the wrong shape, no JSON
# parser) -> it replays from the pinned genesis, and says why: an unanswered question is never an
# agreement. `refused` (a server on another network, two servers that disagree, an anchor line that is not
# an https URL) -> join.sh stops, because the anchors contradict each other or the network; --replay, which
# needs no trust point, is the way past. `none` (no anchor in this clone, or a chain shorter than the lag)
# replays as well.
#
# What the agreement is worth. With one server listed, "every server agrees" holds trivially and the anchor
# reduces to the TLS identity of that server's operator: a certificate for that name. It closes a man in the
# middle on the HTTP path; it closes neither a compromised operator nor a compromised certificate authority.
# A second server, run by someone else, is what turns the agreement into a witness -- one URL per line.
#
# STATESYNC_TRUST_LAG_BLOCKS -- how far below the lowest head the trust point sits. An order of magnitude
# owned as such, not a derivation: no joiner can read the snapshot settings of the nodes it will meet. It
# is meant to put the trust point below the newest snapshot a peer offers -- a node started by
# docker/entrypoint-chain.sh writes one every `snapshot-interval` blocks -- so that the light client verifies forward
# from it, while staying far inside the light client's trust period. DENDRA_STATESYNC_TRUST_LAG overrides
# it; anything but a positive integer is refused, never read as zero.
STATESYNC_TRUST_LAG_BLOCKS=1000
SS_RPC_RE='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?$'

# _ss_read status|block RAW -> "<network> <height>" | "<height> <HASH or NOHASH>" | NODOC
# A trust point is never read from the text. Unlike the sync watch, this reader has no by-key fallback: in a
# /block document the key `hash` lives in block_id, in last_block_id and in every part-set header, and the
# wrong one would be a trust point for another block. Without a parser that passes its fixtures the trust
# point is not derived, and the node replays.
_SS_JSON=""
_ss_read(){
  local kind="$1" raw="$2"
  case "$raw" in *"{"*) raw="{${raw#*\{}";; *) printf 'NODOC\n'; return 0;; esac
  case "$_SS_JSON" in
    python3)
      printf '%s' "$raw" | python3 -c '
import json,re,sys
kind=sys.argv[1]
def num(v):
    s=str(v)
    return int(s) if re.fullmatch("[0-9]+", s) else None
try: d,_=json.JSONDecoder().raw_decode(sys.stdin.read())
except Exception: print("NODOC"); raise SystemExit(0)
if isinstance(d,dict) and isinstance(d.get("result"),dict): d=d["result"]
if not isinstance(d,dict): print("NODOC"); raise SystemExit(0)
if kind=="status":
    n=d.get("node_info"); s=d.get("sync_info")
    if not isinstance(n,dict) or not isinstance(s,dict): print("NODOC"); raise SystemExit(0)
    net=n.get("network")
    if not isinstance(net,str) or not net or any(c.isspace() for c in net): print("NODOC"); raise SystemExit(0)
    h=num(s.get("latest_block_height",0))
    if h is None: print("NODOC"); raise SystemExit(0)
    print("%s %d" % (net,h))
else:
    b=d.get("block"); bid=d.get("block_id")
    hd=b.get("header") if isinstance(b,dict) else None
    if not isinstance(hd,dict) or not isinstance(bid,dict): print("NODOC"); raise SystemExit(0)
    h=num(hd.get("height",0))
    if h is None: print("NODOC"); raise SystemExit(0)
    v=bid.get("hash")
    v=v.strip().upper() if isinstance(v,str) and v.strip() and not any(c.isspace() for c in v.strip()) else "NOHASH"
    print("%d %s" % (h,v))
' "$kind" 2>/dev/null || printf 'NODOC\n'
      ;;
    jq)
      printf '%s' "$raw" | jq -r --arg k "$kind" '
        def num: (tostring) as $s | if ($s|test("^[0-9]+$")) then ($s|tonumber|tostring) else error("nan") end;
        (if (type=="object" and has("result") and (.result|type)=="object") then .result else . end) as $d
        | if ($d|type)!="object" then "NODOC"
          elif $k=="status" then
            (if (($d.node_info|type)=="object") and (($d.sync_info|type)=="object")
                and (($d.node_info.network|type)=="string") and (($d.node_info.network|length)>0)
                and (($d.node_info.network|contains(" "))|not)
             then (try ($d.node_info.network + " " + (($d.sync_info.latest_block_height // 0)|num)) catch "NODOC")
             else "NODOC" end)
          else
            (if (($d.block|type)=="object") and (($d.block.header|type)=="object") and (($d.block_id|type)=="object")
             then (try ((($d.block.header.height // 0)|num) + " "
                        + (if (($d.block_id.hash|type)=="string") and (($d.block_id.hash|length)>0)
                               and (($d.block_id.hash|contains(" "))|not)
                           then ($d.block_id.hash|ascii_upcase) else "NOHASH" end)) catch "NODOC")
             else "NODOC" end)
          end' 2>/dev/null || printf 'NODOC\n'
      ;;
    *) printf 'NODOC\n' ;;
  esac
}

# The reader is tested before it is trusted, as the sync watch's is. Fixture B carries the trap a
# by-key reader falls into: a `hash` under last_block_id before the one under block_id. Fixture N has no
# block_id at all, and must answer NODOC rather than borrow the other hash.
_SS_FIX_S='{"jsonrpc":"2.0","id":-1,"result":{"node_info":{"network":"dendra-fix"},"sync_info":{"latest_block_height":"24048","catching_up":false}}}'
_SS_FIX_Z='{"node_info":{"network":"dendra-fix"},"sync_info":{"catching_up":true}}'
_SS_FIX_B='{"result":{"block":{"header":{"height":"23048","last_block_id":{"hash":"1111111111111111111111111111111111111111111111111111111111111111"}}},"block_id":{"hash":"d4940b3688aea5934a5bdeb8108620a2cf7d6f95470854cbdc32da0e9a736926"}}}'
_SS_FIX_N='{"result":{"block":{"header":{"height":"23048","last_block_id":{"hash":"1111111111111111111111111111111111111111111111111111111111111111"}}}}}'
_ss_reader_ok(){
  [ "$(_ss_read status "$_SS_FIX_S")" = "dendra-fix 24048" ] || return 1
  [ "$(_ss_read status "$_SS_FIX_Z")" = "dendra-fix 0" ] || return 1
  [ "$(_ss_read block "$_SS_FIX_B")" = "23048 D4940B3688AEA5934A5BDEB8108620A2CF7D6F95470854CBDC32DA0E9A736926" ] || return 1
  [ "$(_ss_read block "$_SS_FIX_N")" = "NODOC" ] || return 1
}
_ss_pick_reader(){
  local c
  for c in python3 jq; do
    command -v "$c" >/dev/null 2>&1 || continue
    _SS_JSON="$c"
    _ss_reader_ok && return 0
    warn "the '$c' trust-point reader failed its own fixtures -> trying the next one."
  done
  _SS_JSON=""
  return 1
}

# _ss_get URL -> the document, over TLS only (`--proto =https`: no plain-HTTP fallback, and no redirect is
# followed). Same indirection as the sync loop, same guard: honoured only under DENDRA_SELFTEST=1, and the
# name is in no allow-list, so no downloaded file reaches it.
_ss_get(){
  if [ "${DENDRA_SELFTEST:-0}" = "1" ] && [ -n "${DENDRA_SELFTEST_SSGET:-}" ]; then "$DENDRA_SELFTEST_SSGET" "$1"; return $?; fi
  curl -fsS --proto =https -m 15 "$1" 2>/dev/null
}

# statesync_rpc_list -> SS_SERVERS (distinct, one per line, no trailing slash), SS_LIST_STATE
# (listed|none|refused), SS_LIST_WHY. A line that is not an https URL refuses the whole list: the anchor is
# the clone, and a clone that names something else was edited or damaged.
statesync_rpc_list(){
  local f="${REPO:-}/docker/STATESYNC_RPC" lines bad
  SS_SERVERS=""; SS_LIST_STATE=none; SS_LIST_WHY=""
  if [ ! -r "$f" ]; then SS_LIST_WHY="docker/STATESYNC_RPC is not in this tree"; return 0; fi
  lines="$(tr -d '\r' < "$f" | grep -vE '^[[:space:]]*(#|$)' || true)"
  if [ -z "$lines" ]; then SS_LIST_WHY="docker/STATESYNC_RPC names no server"; return 0; fi
  bad="$(printf '%s\n' "$lines" | LC_ALL=C grep -vE "$SS_RPC_RE" || true)"
  if [ -n "$bad" ]; then
    SS_LIST_STATE=refused
    SS_LIST_WHY="docker/STATESYNC_RPC holds a line that is not an https URL ('$(printf '%s\n' "$bad" | head -1)')"
    return 0
  fi
  SS_SERVERS="$(printf '%s\n' "$lines" | sed 's#/$##' | awk '!seen[$0]++')"
  SS_LIST_STATE=listed
}

# statesync_trust_point -> STATESYNC_STATE (agree|unknown|refused|none), STATESYNC_WHY, and on agree
# SS_RPC (the servers, comma-separated), SS_HEIGHT, SS_HASH, SS_NSERVERS. It returns 0 in every case: it
# names an outcome, the caller decides what follows.
statesync_trust_point(){
  local lag srv doc line net h head="" hh hash first="" list="" unk="" ref="" n=0
  STATESYNC_STATE=unknown; STATESYNC_WHY=""; SS_RPC=""; SS_HEIGHT=""; SS_HASH=""; SS_NSERVERS=0
  lag="${DENDRA_STATESYNC_TRUST_LAG:-$STATESYNC_TRUST_LAG_BLOCKS}"
  case "$lag" in
    ''|*[!0-9]*|0*) STATESYNC_STATE=refused
                    STATESYNC_WHY="DENDRA_STATESYNC_TRUST_LAG='$lag' is not a positive number of blocks"; return 0 ;;
  esac
  statesync_rpc_list
  case "$SS_LIST_STATE" in
    none)    STATESYNC_STATE=none; STATESYNC_WHY="$SS_LIST_WHY"; return 0 ;;
    refused) STATESYNC_STATE=refused; STATESYNC_WHY="$SS_LIST_WHY"; return 0 ;;
  esac
  if [ -z "${CHAIN_ID:-}" ]; then
    STATESYNC_STATE=refused; STATESYNC_WHY="no CHAIN_ID to check the servers' network against"; return 0
  fi
  if ! _ss_pick_reader; then
    STATESYNC_WHY="neither python3 nor jq on this host passed its fixtures, and a trust point is never read from the text"
    return 0
  fi
  # 1. Every server is asked, even after a bad answer: a refusal anywhere outranks an unknown elsewhere.
  for srv in $SS_SERVERS; do
    n=$((n + 1))
    if ! doc="$(_ss_get "$srv/status")"; then unk="${unk:-$srv/status did not answer over TLS}"; continue; fi
    line="$(_ss_read status "$doc")"
    net="${line%% *}"; h="${line#* }"
    case "$line" in NODOC*|'') unk="${unk:-$srv/status is not a readable status document}"; continue ;; esac
    case "$h" in ''|*[!0-9]*) unk="${unk:-$srv/status carries no readable height}"; continue ;; esac
    if [ "$net" != "$CHAIN_ID" ]; then ref="${ref:-$srv serves the network '$net', not '$CHAIN_ID'}"; continue; fi
    if [ -z "$head" ] || [ "$h" -lt "$head" ]; then head="$h"; fi
    list="${list:+$list,}$srv"
  done
  SS_NSERVERS="$n"
  if [ -n "$ref" ]; then STATESYNC_STATE=refused; STATESYNC_WHY="$ref"; return 0; fi
  if [ -n "$unk" ]; then STATESYNC_WHY="$unk"; return 0; fi
  # Bounded from below: a head the zero rule turned into 0 (an omitted height) gives no trust point.
  if [ "$head" -le "$lag" ]; then
    STATESYNC_STATE=none
    STATESYNC_WHY="the servers' head ($head) is not above the trust lag ($lag blocks): there is no trust point to take, and the replay is short"
    return 0
  fi
  SS_HEIGHT=$((head - lag))
  # 2. The same block on every server.
  for srv in $SS_SERVERS; do
    if ! doc="$(_ss_get "$srv/block?height=$SS_HEIGHT")"; then unk="${unk:-$srv did not serve block $SS_HEIGHT over TLS}"; continue; fi
    line="$(_ss_read block "$doc")"
    case "$line" in NODOC*|'') unk="${unk:-$srv/block?height=$SS_HEIGHT is not a readable block document}"; continue ;; esac
    hh="${line%% *}"; hash="${line#* }"
    if [ "$hh" != "$SS_HEIGHT" ]; then unk="${unk:-$srv served block $hh when $SS_HEIGHT was asked}"; continue; fi
    case "$hash" in
      *[!0-9A-F]*|'') unk="${unk:-$srv serves no usable hash for block $SS_HEIGHT ($hash)}"; continue ;;
    esac
    if [ "${#hash}" != 64 ]; then unk="${unk:-$srv serves a hash of ${#hash} hex digits for block $SS_HEIGHT, not 64}"; continue; fi
    if [ -z "$first" ]; then first="$hash"
    elif [ "$hash" != "$first" ]; then ref="${ref:-the servers disagree on block $SS_HEIGHT: $first, and $hash from $srv}"; fi
  done
  if [ -n "$ref" ]; then STATESYNC_STATE=refused; STATESYNC_WHY="$ref"; SS_HEIGHT=""; return 0; fi
  if [ -n "$unk" ]; then STATESYNC_WHY="$unk"; SS_HEIGHT=""; return 0; fi
  STATESYNC_STATE=agree; SS_RPC="$list"; SS_HASH="$first"
  if [ "$n" = 1 ]; then
    STATESYNC_WHY="a single server named by docker/STATESYNC_RPC served block $SS_HEIGHT over TLS: the anchor is that server's TLS identity, not an independent witness"
  else
    STATESYNC_WHY="the $n servers named by docker/STATESYNC_RPC served the same block $SS_HEIGHT over TLS"
  fi
}

# _env_file_get FILE KEY -> the last value of that key in that file, CR stripped; empty when absent.
_env_file_get(){ sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -1 | tr -d '\r'; }
# _env_file_set FILE KEY VALUE -- one line of a kit .env, edited in place (the file keeps its mode): replaced
# where it stands, appended when absent; every other line is copied as it is. The status is the write's.
_env_file_set(){
  local t rc
  [ -f "$1" ] || return 0
  t="$(mktemp)" || return 1
  awk -v k="$2" -v v="$3" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s) print k "=" v; s = 1; next }
    { print }
    END { if (!s) print k "=" v }' "$1" > "$t" && cat "$t" > "$1"; rc=$?
  rm -f "$t"
  return "$rc"
}
# _env_file_unset FILE KEY -- removes every KEY= line of a kit .env, in place (the file keeps its mode).
_env_file_unset(){
  local t rc
  [ -f "$1" ] || return 0
  t="$(mktemp)" || return 1
  awk -v k="$2" 'BEGIN { n = length(k) + 1 } substr($0, 1, n) == k "=" { next } { print }' "$1" > "$t" && cat "$t" > "$1"; rc=$?
  rm -f "$t"
  return "$rc"
}

# The mark deploy/testnet-miner/node_upgrade_watch.sh writes beside the image it starts the node on
# (DENDRA_NODE_IMAGE_SET_BY): with it, an image ID in the node kit's .env is a pin of this kit, held to its gates
# like a digest, and not a choice of the operator.
NODE_IMAGE_WATCH_MARK=node_upgrade_watch
NODE_IMAGE_ID_RE='^sha256:[0-9a-f]{64}$'
# The form of DENDRA_NODE_IMAGE_BUILT_FOR and of TREE_GATES (tree_gates).
NODE_GATES_RE='^kit_version=([0-9]+) consensus_epoch=([0-9]+)$'
# _env_image_lines_unset -- the three lines of a pin this kit wrote, removed from the node kit's .env.
_env_image_lines_unset(){
  _env_file_unset "$NODE_KIT/.env" DENDRA_NODE_IMAGE
  _env_file_unset "$NODE_KIT/.env" DENDRA_NODE_IMAGE_BUILT_FOR
  _env_file_unset "$NODE_KIT/.env" DENDRA_NODE_IMAGE_SET_BY
}

# node_env_image_reconcile -> NODE_ENV_IMAGE (none|operator|current|adopt|superseded|dropped|upgrade|stale),
# NODE_ENV_IMAGE_WHY; returns 1 on `stale` (the caller stops). Reads the DENDRA_NODE_IMAGE line of the node kit's
# .env, after the rewrite (a value in the shell is the operator's choice for this run and is not judged here).
# A pin this kit wrote is not a choice of the operator: the digest node_compose_up writes so a restart keeps that
# binary, or the image ID node_upgrade_watch.sh writes with its mark at a governed upgrade. Read back on a later run
# as "an image the operator named", it would start a new node of the next consensus epoch on the binary of the
# previous one, or keep a node on the image of an old kit forever. So a value of the pin's own form
# (ghcr.io/dendranetwork/dendra-node@sha256:...), or an image ID beside DENDRA_NODE_IMAGE_SET_BY=node_upgrade_watch,
# carries DENDRA_NODE_IMAGE_BUILT_FOR, held to the gates of this tree (docker/KIT_VERSION, docker/CONSENSUS_EPOCH):
#   · equal gates -> current, or adopt when this tree pins another image of them (node_image_adopt);
#   · new node volume, any other gates or none -> dropped: the lines removed (and reported), and the pin of this tree
#     applies;
#   · the same consensus epoch, another kit_version -> adopt when this tree pins its node image, superseded when it
#     pins none it can use (the lines removed, the node built from the clone). One epoch takes one set of state
#     transitions, so this is the kit update of a node that holds chain state, and it is never refused;
#   · an earlier epoch, under --after-upgrade <plan>, on a node the chain confirms at that plan's halt
#     (node_halt_confirmed) -> upgrade: the lines removed, and the node started on this tree's node image
#     (node_compose_up), then asked whether the plan was applied (after_upgrade_finish);
#   · another epoch, a missing or unreadable gates line, or gates of this tree that could not be read, on a volume
#     that exists or could not be read -> stale, refused: changing the consensus binary of a node that holds chain
#     state is an upgrade. node_upgrade_watch.sh carries it: it writes the new image and its gates before it runs
#     the join with --after-upgrade, which then reads them as current.
# Any other value (a tag, an image ID without the mark, a digest of another repository) is the operator's, and wins.
node_env_image_reconcile(){
  local v bf mark form="by digest" be="" te=""
  NODE_ENV_IMAGE=none; NODE_ENV_IMAGE_WHY=""
  v="$(_env_file_get "$NODE_KIT/.env" DENDRA_NODE_IMAGE)"
  [ -n "$v" ] || return 0
  mark="$(_env_file_get "$NODE_KIT/.env" DENDRA_NODE_IMAGE_SET_BY)"
  if ! printf '%s\n' "$v" | grep -qE "$NODE_IMAGE_RE"; then
    if [ "$mark" = "$NODE_IMAGE_WATCH_MARK" ] && printf '%s\n' "$v" | grep -qE "$NODE_IMAGE_ID_RE"; then
      form="by ID, written by node_upgrade_watch.sh (DENDRA_NODE_IMAGE_SET_BY)"
    else
      NODE_ENV_IMAGE=operator; return 0
    fi
  fi
  bf="$(_env_file_get "$NODE_KIT/.env" DENDRA_NODE_IMAGE_BUILT_FOR)"
  tree_gates
  node_image_pin
  if [ -n "$TREE_GATES" ] && [ "$bf" = "$TREE_GATES" ] && [ "$NODE_IMAGE_STATE" = pinned ] && [ "$v" != "$NODE_IMAGE_PIN" ]; then
    NODE_ENV_IMAGE=adopt
    NODE_ENV_IMAGE_WHY="DENDRA_NODE_IMAGE=$v in $NODE_KIT/.env was pinned for $bf, the gates of this tree, which pins $NODE_IMAGE_PIN for them"
    return 0
  fi
  if [ -n "$TREE_GATES" ] && [ "$bf" = "$TREE_GATES" ]; then NODE_ENV_IMAGE=current; return 0; fi
  if [ -z "$TREE_GATES" ]; then
    NODE_ENV_IMAGE_WHY="DENDRA_NODE_IMAGE=$v in $NODE_KIT/.env names the kit's node image $form, and the gates of this tree (docker/KIT_VERSION, docker/CONSENSUS_EPOCH) could not be read to check it against"
  elif [ -z "$bf" ]; then
    NODE_ENV_IMAGE_WHY="DENDRA_NODE_IMAGE=$v in $NODE_KIT/.env names the kit's node image $form without a DENDRA_NODE_IMAGE_BUILT_FOR line: the gates it was built for are unknown, and this tree is $TREE_GATES"
  else
    NODE_ENV_IMAGE_WHY="DENDRA_NODE_IMAGE=$v in $NODE_KIT/.env was pinned for $bf, and this tree is $TREE_GATES"
  fi
  if [ "${NODE_VOLUME:-unknown}" = new ]; then
    _env_image_lines_unset
    NODE_ENV_IMAGE=dropped
    say "  [i] $NODE_ENV_IMAGE_WHY."
    say "      This node's volume is new: that line and its DENDRA_NODE_IMAGE_BUILT_FOR are removed from the .env,"
    say "      and the node image of this tree applies (docker/NODE_IMAGE, or a build from the clone)."
    return 0
  fi
  [[ $TREE_GATES =~ $NODE_GATES_RE ]] && te="$((10#${BASH_REMATCH[2]}))"
  [[ $bf =~ $NODE_GATES_RE ]] && be="$((10#${BASH_REMATCH[2]}))"
  if [ -n "$te" ] && [ "$be" = "$te" ]; then
    if [ "$NODE_IMAGE_STATE" = pinned ]; then NODE_ENV_IMAGE=adopt; return 0; fi
    _env_image_lines_unset
    NODE_ENV_IMAGE=superseded
    say "  [i] $NODE_ENV_IMAGE_WHY."
    say "      The consensus epoch is the same, and this tree pins no node image it can use: $NODE_IMAGE_WHY."
    say "      That line, its DENDRA_NODE_IMAGE_BUILT_FOR and DENDRA_NODE_IMAGE_SET_BY are removed from the .env, and the"
    say "      node is built from this clone."
    return 0
  fi
  # The join after a governed upgrade, on a node that stands at that plan's halt: the binary before the upgrade
  # cannot pass it, so a pin of an earlier epoch gives way to this tree's node image. Forward only.
  if [ -n "${AFTER_UPGRADE:-}" ] && [ "${OWN_NODE:-1}" = 1 ] && [ -n "$te" ] && [ -n "$be" ] && [ "$te" -gt "$be" ] \
     && { [ "${AFTER_UPGRADE_SWITCH:-0}" = 1 ] || node_halt_confirmed "$AFTER_UPGRADE"; }; then
    _env_image_lines_unset
    AFTER_UPGRADE_SWITCH=1; NODE_ENV_IMAGE=upgrade
    say "  [i] $NODE_ENV_IMAGE_WHY."
    say "      --after-upgrade $AFTER_UPGRADE: ${AFTER_UPGRADE_HALT_WHY:-the halt of that plan is confirmed}. The binary before the upgrade cannot pass it:"
    say "      that line, its DENDRA_NODE_IMAGE_BUILT_FOR and DENDRA_NODE_IMAGE_SET_BY are removed from the .env, and the"
    say "      node starts on this tree's node image."
    return 0
  fi
  NODE_ENV_IMAGE=stale
  return 1
}

# node_volume_state -> NODE_VOLUME (new|existing|unknown) and NODE_PROJECT. Read once, before the node
# kit's .env is rewritten, and used by everything that depends on it: the adoption of a home this run
# creates, the trust point (a home applies one only when it is created) and the node image pin (a new
# node only). The project name is the one compose resolves (shell, then this kit's .env, then the file's
# `name:`), read from `docker compose config`, never re-derived here. Three states: an unreadable name or
# volume list is `unknown`, never `new` -- `new` is the state that opens the pin and the trust point.
node_volume_state(){
  local v vols
  NODE_VOLUME=unknown
  NODE_PROJECT="$( cd "$NODE_KIT" 2>/dev/null && docker compose config 2>/dev/null | sed -n 's/^name:[[:space:]]*//p' | head -1 )"
  [ -n "$NODE_PROJECT" ] || return 0
  vols="$(docker volume ls -q 2>/dev/null)" || return 0
  NODE_VOLUME=new
  for v in $vols; do [ "$v" = "${NODE_PROJECT}_node-data" ] && NODE_VOLUME=existing; done
  return 0
}

# node_sync_carry -- the kept branch of node_sync_plan. The four STATESYNC_* lines of the node kit's .env are
# carried over only when all of these hold: the trust height is a positive block number and the trust hash
# 64 uppercase hex digits; every server of STATESYNC_RPC is one that docker/STATESYNC_RPC of this clone lists
# (https only); and STATESYNC_GENESIS_SHA256 is the GENESIS_SHA256 of this run. Anything else empties all
# four, and says why: a trust point that cannot be traced to this clone and this genesis -- one an older
# join.sh copied from network-info.txt over plain HTTP, or one of the network before a reset -- is not kept.
# Four empty lines are carried as they are, silently.
node_sync_carry(){
  local r h x g srv why="" _srvs
  r="$(_env_file_get "$NODE_KIT/.env" STATESYNC_RPC)"
  h="$(_env_file_get "$NODE_KIT/.env" STATESYNC_TRUST_HEIGHT)"
  x="$(_env_file_get "$NODE_KIT/.env" STATESYNC_TRUST_HASH)"
  g="$(_env_file_get "$NODE_KIT/.env" STATESYNC_GENESIS_SHA256)"
  [ -n "$r$h$x$g" ] || return 0
  case "$h" in ''|*[!0-9]*|0*) why="its trust height '$h' is not a positive block number" ;; esac
  if [ -z "$why" ]; then
    case "$x" in
      ''|*[!0-9A-F]*) why="its trust hash is not 64 uppercase hex digits" ;;
      *) [ "${#x}" = 64 ] || why="its trust hash is not 64 uppercase hex digits" ;;
    esac
  fi
  if [ -z "$why" ] && { [ -z "${GENESIS_SHA256:-}" ] || [ "$g" != "$GENESIS_SHA256" ]; }; then
    why="it is bound to genesis '${g:-<none>}', not to this network's GENESIS_SHA256 '${GENESIS_SHA256:-<none>}'"
  fi
  if [ -z "$why" ]; then
    statesync_rpc_list
    if [ "$SS_LIST_STATE" != listed ]; then
      why="docker/STATESYNC_RPC of this clone lists no https server it could be traced to ($SS_LIST_WHY)"
    elif [ -z "$r" ]; then
      why="it names no server"
    else
      IFS=, read -r -a _srvs <<< "$r"
      for srv in "${_srvs[@]}"; do
        printf '%s\n' "$SS_SERVERS" | grep -qxF -- "$srv" || { why="its server '$srv' is not one that docker/STATESYNC_RPC of this clone lists"; break; }
      done
    fi
  fi
  if [ -n "$why" ]; then
    say "  [i] the state-sync trust point of $NODE_KIT/.env is not carried over: $why."
    say "      Its four STATESYNC_* lines are emptied: a home that exists applies none, and a home recreated in"
    say "      this volume (DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1) then replays from the genesis."
    return 0
  fi
  STATESYNC_RPC="$r"; STATESYNC_TRUST_HEIGHT="$h"; STATESYNC_TRUST_HASH="$x"; STATESYNC_GENESIS_SHA256="$g"
}

# node_sync_plan -> NODE_SYNC_MODE (statesync|replay|kept), NODE_SYNC_WHY, and STATESYNC_RPC,
# STATESYNC_TRUST_HEIGHT, STATESYNC_TRUST_HASH, STATESYNC_GENESIS_SHA256: the four values start_local_node
# writes into the node kit's .env, empty unless a trust point was agreed (or carried over: node_sync_carry).
# Returns 1 when the trust point is refused (the caller stops).
# Who state-syncs by default: a miner's own node, which serves no consensus. A validator's node replays
# unless --statesync is given. --replay wins over --statesync, in either order (see the argument loop).
# A trust point is bound to its genesis: STATESYNC_GENESIS_SHA256 records the GENESIS_SHA256 it was derived
# under, and docker/node-join.sh applies it to that genesis only. A home recreated after a genesis change
# therefore never starts from a trust point of the previous network.
node_sync_plan(){
  STATESYNC_RPC=""; STATESYNC_TRUST_HEIGHT=""; STATESYNC_TRUST_HASH=""; STATESYNC_GENESIS_SHA256=""
  NODE_SYNC_MODE=replay; NODE_SYNC_WHY=""
  if [ "${NODE_VOLUME:-unknown}" != new ]; then
    # A home that exists applies no trust point while it lives: node-join.sh reads one only when it creates
    # the home -- which DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 does again inside an existing volume. Lines
    # that are still this clone's and this genesis's are carried over unchanged: rewriting them would change
    # the container's configuration, and compose would recreate it -- restarting a snapshot restore in
    # progress. The others are emptied (node_sync_carry).
    NODE_SYNC_MODE=kept
    node_sync_carry
    if [ "${NODE_VOLUME:-unknown}" = existing ]; then
      NODE_SYNC_WHY="this node's volume already exists: it resumes from what it holds, and no trust point applies to it"
    else
      NODE_SYNC_WHY="whether this node's volume is new could not be read: no trust point is derived for a home that may already exist"
    fi
    return 0
  fi
  case "$SYNC_CHOICE" in
    replay) NODE_SYNC_WHY="--replay was given"; return 0 ;;
    statesync) : ;;
    *) if [ "$ROLE" = validator ]; then
         NODE_SYNC_WHY="a validator's node replays by default (--statesync asks for a snapshot)"; return 0
       fi ;;
  esac
  if [ "$ROLE" = validator ]; then
    warn "--statesync on a validator: this node will hold no block below its snapshot. The kit makes state sync"
    warn "the default for a miner's node only, and a validator opts in with --statesync; deploy/bond_validator.sh"
    warn "refuses to bond a node that holds no block below its snapshot unless --accept-statesynced-node is given."
  fi
  # Bound to a genesis, or not taken: node-join.sh applies a trust point only to the genesis it was derived
  # under, and without GENESIS_SHA256 there is none to bind it to.
  if [ -z "${GENESIS_SHA256:-}" ]; then
    NODE_SYNC_WHY="no GENESIS_SHA256 to bind a trust point to"
    say "  [i] no state sync for this node: $NODE_SYNC_WHY. It replays from the genesis."
    return 0
  fi
  say "  [..] deriving the state-sync trust point over TLS (docker/STATESYNC_RPC in this clone)"
  statesync_trust_point
  case "$STATESYNC_STATE" in
    agree)
      NODE_SYNC_MODE=statesync; NODE_SYNC_WHY="$STATESYNC_WHY"
      STATESYNC_RPC="$SS_RPC"; STATESYNC_TRUST_HEIGHT="$SS_HEIGHT"; STATESYNC_TRUST_HASH="$SS_HASH"
      STATESYNC_GENESIS_SHA256="$GENESIS_SHA256"
      say "  [OK] state-sync trust point: block $SS_HEIGHT, hash $SS_HASH"
      say "       $STATESYNC_WHY." ;;
    refused)
      NODE_SYNC_WHY="$STATESYNC_WHY"
      return 1 ;;
    *)
      NODE_SYNC_WHY="$STATESYNC_WHY"
      say "  [i] no state sync for this node: $STATESYNC_WHY."
      say "      It replays from the genesis pinned by docker/GENESIS_SHA256: slower, and it needs no trust point." ;;
  esac
  return 0
}

# node_image_adopt <the image the node kit's .env names> -- the adopt branch of node_compose_up: a pin this kit
# wrote, of this tree's consensus epoch, gives way to the pin of this tree (node_env_image_reconcile). The pull is
# judged by its effect and the platform read (image_platform), as for a new node; then the .env names the pin and
# its gates, and the node is recreated on it with --no-build. A pull that fails, or a platform that is another or
# unread, keeps the image the node runs: it is of the same epoch, so it takes the same state transitions, and the
# lines are left for the next join to try again. Returns the status of `docker compose up`.
node_image_adopt(){
  local cur="$1"
  say "  [i] $NODE_ENV_IMAGE_WHY."
  say "      Same consensus epoch: this is a kit update of the node, which moves to the node image this tree pins."
  say "  pulling the prebuilt node image $NODE_IMAGE_PIN (pinned by docker/NODE_IMAGE; started only on an engine of its platform)"
  if ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$NODE_IMAGE_PIN" docker compose pull node ) \
     && docker image inspect "$NODE_IMAGE_PIN" >/dev/null 2>&1; then
    image_platform "$NODE_IMAGE_PIN" node
    if [ "$IMAGE_PLATFORM" = same ]; then
      _env_file_set "$NODE_KIT/.env" DENDRA_NODE_IMAGE "$NODE_IMAGE_PIN"
      _env_file_set "$NODE_KIT/.env" DENDRA_NODE_IMAGE_BUILT_FOR "$NODE_IMAGE_BUILT_FOR"
      _env_file_unset "$NODE_KIT/.env" DENDRA_NODE_IMAGE_SET_BY
      say "  [OK] DENDRA_NODE_IMAGE=$NODE_IMAGE_PIN and DENDRA_NODE_IMAGE_BUILT_FOR=$NODE_IMAGE_BUILT_FOR written to"
      say "       $NODE_KIT/.env: the node is recreated on that image (docker compose up -d --no-build)"
      ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$NODE_IMAGE_PIN" docker compose up -d --no-build ); return $?
    fi
    warn "$IMAGE_WHY: the node keeps the image it runs ($cur), of the same consensus epoch; a later join tries again"
    if [ "$IMAGE_PLATFORM" = different ] && [ "$cur" != "$NODE_IMAGE_PIN" ]; then
      docker image rm "$NODE_IMAGE_PIN" >/dev/null 2>&1 || warn "the pulled image of another platform could not be removed: docker image rm $NODE_IMAGE_PIN"
    fi
  else
    warn "the pinned node image could not be pulled (private package, registry down, no route?): the node keeps the image it runs ($cur), of the same consensus epoch; a later join tries again"
  fi
  ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$cur" docker compose up -d --no-build )
}

# node_compose_up -- starts the node kit, from the pinned image when everything allows it, else built from
# the clone. Returns the status of `docker compose up` (the caller reports a failure).
# The pin applies to a new node, and to a node whose .env names a pin this kit wrote for the same consensus
# epoch (node_image_adopt). Changing the image of a node that holds chain state to another epoch changes its
# consensus binary: that is an upgrade, a separate gesture, refused by node_env_image_reconcile and by the
# recreate guard of start_local_node.
# An image the operator named wins, and this is reported: DENDRA_NODE_IMAGE in the shell or in the kit's .env is how a second
# node keeps off the first one's tag, or how a running node is pinned to the image it is on. A pin of this kit in
# the .env (a digest of the pin's own form, or an image ID with the mark of node_upgrade_watch.sh) is first held to
# this tree's gates by node_env_image_reconcile (start_local_node).
# Never `--build` with a digest or an image ID: compose cannot tag a build with a reference by digest, and
# a build tagged with an ID's text (repository "sha256") names another image under the ID's spelling while
# the running node is recreated. A node whose .env names either is started with --no-build, on that image.
node_compose_up(){
  local mine foreign="" vol="${NODE_VOLUME:-unknown}"
  # At the halt of a governed upgrade (AFTER_UPGRADE_SWITCH), the pin of this tree applies to the node that holds
  # chain state as to a new one: its binary has to change, and the halt is confirmed (node_halt_confirmed).
  [ "${AFTER_UPGRADE_SWITCH:-0}" = 1 ] && vol=upgrade
  mine="${DENDRA_NODE_IMAGE:-$(_env_file_get "$NODE_KIT/.env" DENDRA_NODE_IMAGE)}"
  node_image_pin
  if [ -z "${DENDRA_NODE_IMAGE:-}" ] && [ -n "$mine" ] && [ "${NODE_ENV_IMAGE:-}" = adopt ] && [ "$NODE_IMAGE_STATE" = pinned ]; then
    node_image_adopt "$mine"; return $?
  fi
  if [ -n "$mine" ]; then
    case "$mine" in
      *@sha256:*|sha256:*)
        say "  [i] DENDRA_NODE_IMAGE=$mine names an image by digest or by ID: the node starts on it, nothing is built"
        ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$mine" docker compose up -d --no-build ); return $? ;;
      *)
        [ "$NODE_IMAGE_STATE" = pinned ] && \
          say "  [i] DENDRA_NODE_IMAGE=$mine is set: the node image is yours, docker/NODE_IMAGE is not applied"
        ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$mine" docker compose up -d --build ); return $? ;;
    esac
  fi
  case "$NODE_IMAGE_STATE:$vol" in
    pinned:new|pinned:upgrade)
      say "  pulling the prebuilt node image $NODE_IMAGE_PIN (pinned by docker/NODE_IMAGE; started only on an engine of its platform)"
      # Judged by its effect, not by its exit code, as for the miner image.
      if ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$NODE_IMAGE_PIN" docker compose pull node ) \
         && docker image inspect "$NODE_IMAGE_PIN" >/dev/null 2>&1; then
        image_platform "$NODE_IMAGE_PIN" node
        case "$IMAGE_PLATFORM" in
          same)
            _env_file_set "$NODE_KIT/.env" DENDRA_NODE_IMAGE "$NODE_IMAGE_PIN"
            _env_file_set "$NODE_KIT/.env" DENDRA_NODE_IMAGE_BUILT_FOR "$NODE_IMAGE_BUILT_FOR"
            say "  [OK] DENDRA_NODE_IMAGE=$NODE_IMAGE_PIN written to $NODE_KIT/.env: a restart keeps this binary"
            say "       (DENDRA_NODE_IMAGE_BUILT_FOR=$NODE_IMAGE_BUILT_FOR beside it: a later join checks it against its tree)"
            ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE="$NODE_IMAGE_PIN" docker compose up -d --no-build ); return $? ;;
          different)
            say "  [i] $IMAGE_WHY: the node image is built from this clone${DOCKER_DEFAULT_PLATFORM:+, for DOCKER_DEFAULT_PLATFORM=$DOCKER_DEFAULT_PLATFORM, which compose follows}"
            foreign="$NODE_IMAGE_PIN" ;;
          *)
            warn "$IMAGE_WHY: building the node image from this clone instead" ;;
        esac
      else
        warn "the pinned node image could not be pulled (private package, registry down, no route?): building it from this clone instead"
      fi ;;
    pinned:existing)
      say "  [i] this node's volume already exists: docker/NODE_IMAGE applies to a new node only, so it keeps being built"
      say "      from the clone. Changing the binary of a node that holds chain state is an upgrade, not a join." ;;
    pinned:*)
      say "  [i] whether this node's volume is new could not be read: docker/NODE_IMAGE applies to a new node only, so it is not applied" ;;
    refused:*) warn "$NODE_IMAGE_WHY -- refused: the node image is built from this clone instead" ;;
    *)         say "  [i] $NODE_IMAGE_WHY: the node image is built from this clone" ;;
  esac
  ( cd "$NODE_KIT" && DENDRA_NODE_IMAGE= docker compose up -d --build )
  local rc=$?
  if [ -n "$foreign" ]; then
    docker image rm "$foreign" >/dev/null 2>&1 || warn "the pulled image of another platform could not be removed: docker image rm $foreign"
  fi
  return "$rc"
}

# node_up_failure_report — what a failed node compose up actually said.
# "docker compose up failed" alone names nothing, and a frequent cause is the preflight
# service, which refuses (exit 78) a chain home already in the volume, one left by an earlier
# network for instance. Its explanation and remedies go to the preflight container's log, which a
# failed up does not show, so the operator would get one line naming nothing. The verdict is replayed here;
# an empty read is reported as such, never passed off as "the guard said nothing".
node_up_failure_report(){
  local lg
  lg="$( cd "$NODE_KIT" && docker compose logs --no-color preflight 2>&1 | tail -n 40 )"
  say "  ---- docker compose logs preflight (the guard that runs before the node) ----"
  if [ -n "$lg" ]; then
    printf '%s\n' "$lg" | sed 's/^/    /'
  else
    say "    (nothing could be read from it)"
  fi
  say "  Read it again with:  docker compose -f $NODE_KIT/docker-compose.yml logs preflight node"
}

# start_local_node — writes the node kit's .env, brings the node up, and waits until it has caught up.
#
# Shared by the validator role and by the miner role: a miner that runs its own node uses exactly the
# same node, configured exactly the same way. Duplicating the sequence would let the two copies drift,
# and the miner's copy, the one almost every operator runs, would be the one nobody re-reads.
start_local_node(){
  # The node kit's .env is authoritative, so it is read before anything is required.
  # `check_prereqs` accepts the mere existence of `$NODE_KIT/.env` as valid configuration, and
  # `verify_genesis_info` states that "the node kit enforces its own". Both hold only if the values in
  # that file actually reach this function. They do not arrive on their own: the invocation form this
  # script prints in its own help (`DENDRA_NODE=... DENDRA_RELAY=... FAUCET=... bash deploy/join.sh`),
  # like any plain re-run without CONFIG_URL, carries no GENESIS_URL in the shell. Such a run clears
  # the pre-flight, the hardware probe and the relay probe, and reaches this point with the setting
  # sitting in the neighbouring file rather than in the environment. Reading the kit here is what makes
  # the pre-flight's verdict true where it is used.
  # Order matters: the rewrite further down overwrites this file with the shell's values, so the
  # carry-over has to happen before it. Otherwise a re-run erases not only GENESIS_URL but
  # GENESIS_SHA256 and SEEDS — the digest that protects against a substituted genesis, and the peers
  # without which nothing ever syncs.
  if [ -f "$NODE_KIT/.env" ]; then
    for _k in CHAIN_ID GENESIS_URL GENESIS_SHA256 SEEDS PERSISTENT_PEERS DENDRA_EXTERNAL_ADDRESS; do
      if [ -z "$(eval printf '%s' "\${$_k:-}")" ]; then
        _v="$(sed -n "s/^$_k=//p" "$NODE_KIT/.env" 2>/dev/null | tail -1 | tr -d '\r')"
        [ -n "$_v" ] && { export "$_k=$_v"; say "  [i] $_k carried over from $NODE_KIT/.env"; }
      fi
    done
  fi
  [ -n "${GENESIS_URL:-}" ] || die "GENESIS_URL required to run a node. Either pass CONFIG_URL=<network-info.txt>, or run with --remote-rpc to read the chain from the operator's public RPC instead of starting one."
  # This .env is rewritten from scratch on every run. Anything a later step added to it would be lost —
  # in particular DENDRA_VRF_KEY_FILE, which bond_validator.sh writes so the node signs its vote
  # extensions. Losing it does not break anything visibly: the node still runs, still syncs, still looks
  # healthy — it just silently stops contributing to the committee seed. So we carry it over.
  # And DENDRA_VRF_KEY_FILE is not the only one. Everything the operator or another script of this
  # kit puts here is a decision, and rewriting the file from scratch silently revokes all of them:
  # DENDRA_ADOPT_EXISTING_HOME (the only way past the preflight guard — erase it and the very next
  # `compose up` exits 78 and this script dies on "docker compose up failed", the empty message its
  # own header condemns), DENDRA_NODE_IMAGE (pinning the image a running node is on), DENDRA_PROJECT
  # and the three port overrides (what keeps a second node off the first one's containers). So the rule
  # is inverted: this template owns the keys it writes, and every other DENDRA_* line already present
  # is carried over verbatim. A key this script does not know about is a decision it must not undo.
  local _keep=""
  if [ -f "$NODE_KIT/.env" ]; then
    _keep="$(grep -E '^DENDRA_[A-Z0-9_]+=' "$NODE_KIT/.env" 2>/dev/null \
              | grep -vE '^DENDRA_(NODE|RELAY|API|FAUCET)=' || true)"
  fi
  # The chain id comes from the network. It was written `${CHAIN_ID:-dendra}`: a
  # network-info without the key would have given the node kit the previous chain's name, and the node
  # would have refused the genesis it had just verified. network-info.txt publishes it from the genesis.
  [ -n "${CHAIN_ID:-}" ] || die "network-info.txt carries no CHAIN_ID: the node kit cannot be written without the name of the network it joins"
  # The sync wait's own settings are refused here, before anything is started on them.
  sync_settings_check || die "$SYNC_SETTINGS_WHY: the sync wait cannot use it. Unset it, or set a positive integer."
  # How this node reaches the head, decided before the file below is written, since it carries the answer.
  node_volume_state
  node_sync_plan || die "the state-sync trust point is refused: $NODE_SYNC_WHY. A node is not started from a trust point its anchors contradict. Check docker/STATESYNC_RPC in this clone (git status, git diff) and DENDRA_STATESYNC_TRUST_LAG, or join without a trust point: --replay replays from the genesis pinned by docker/GENESIS_SHA256."
  cat > "$NODE_KIT/.env" <<EOF
# Generated by deploy/join.sh $(date -u +%FT%TZ)
CHAIN_ID=$CHAIN_ID
GENESIS_URL=$GENESIS_URL
GENESIS_SHA256=${GENESIS_SHA256:-}
SEEDS=${SEEDS:-}
PERSISTENT_PEERS=${PERSISTENT_PEERS:-${SEEDS:-}}
# Advertised to peers so they can dial back. Empty = advertise nothing, which is right behind
# NAT. Set it to your public host:port once the P2P port is forwarded; the node refuses a
# private or wildcard value rather than gossiping one to everybody else.
DENDRA_EXTERNAL_ADDRESS=${DENDRA_EXTERNAL_ADDRESS:-}
# The moniker is not a display field: it travels inside the CometBFT NodeInfo, is exchanged with every
# peer, and is returned by /net_info for anyone who asks. A moniker built from the hostname therefore
# broadcast the operator's machine name to the whole peer-to-peer network. Same pseudonym as the miner
# identity, and for the same reason. It is frozen when the node is initialised: an existing node keeps
# the old one.
# No backticks and no command substitution in these comments. This heredoc is unquoted (<<EOF) because
# the values below must expand, so bash also expands, inside the comment lines, both a backtick pair
# and a dollar followed by a parenthesis, and runs whatever they contain. Marking an identifier as code
# that way here therefore executes it: the operator gets "command not found" on a join that is
# otherwise fine, and the quoted text is blanked out of the generated file. The delimiter cannot be
# quoted (<<'EOF') without freezing the values, so the rule falls on the text: name the two dangerous
# forms in words, and never write either of them here.
# This paragraph once broke its own rule: it spelled the dollar-parenthesis form out as an example, and
# every joiner saw a "command not found" right after the genesis check passed.
MONIKER=${MONIKER:-dendra-$( { cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo val; } | tr -d '\n' | head -c 64 | sha256sum | cut -c1-8)}
# State-sync trust point: derived over TLS from docker/STATESYNC_RPC by node_sync_plan, never read from
# network-info. Empty = the node replays from the genesis. Applied by node-join.sh when it creates the home,
# and only to the genesis named on the fourth line, the one it was derived under.
STATESYNC_RPC=${STATESYNC_RPC:-}
STATESYNC_TRUST_HEIGHT=${STATESYNC_TRUST_HEIGHT:-}
STATESYNC_TRUST_HASH=${STATESYNC_TRUST_HASH:-}
STATESYNC_GENESIS_SHA256=${STATESYNC_GENESIS_SHA256:-}
EOF
  if [ -n "$_keep" ]; then
    printf '%s\n' "$_keep" >> "$NODE_KIT/.env"
    # Named one by one: a carried-over decision the operator has forgotten about is as surprising as
    # one that was silently dropped. The count is printed so a rewrite that carries nothing is visible.
    say "  [i] $(printf '%s\n' "$_keep" | grep -c .) operator setting(s) carried across the rewrite:"
    printf '%s\n' "$_keep" | sed 's/^/        /'
  fi
  # The RPC bind is written, not left to the compose default. A choice already in this file was carried over
  # above with the other DENDRA_* lines and is kept as it is. Otherwise loopback is written: the miner kit
  # reaches this node on the dendra-chain network, not through the host, so nothing on this machine needs
  # the RPC on another interface. Written explicitly so that the file states the exposure the node runs
  # with -- deploy/validator_health.sh reads this declaration and compares it with what Docker published.
  if ! grep -q '^DENDRA_RPC_BIND=' "$NODE_KIT/.env" 2>/dev/null; then
    printf 'DENDRA_RPC_BIND=%s\n' "${DENDRA_RPC_BIND:-127.0.0.1}" >> "$NODE_KIT/.env"
  fi
  say "  [i] node RPC published on $(sed -n 's/^DENDRA_RPC_BIND=//p' "$NODE_KIT/.env" | tail -1):${DENDRA_RPC_PORT:-26657} (DENDRA_RPC_BIND in $NODE_KIT/.env)."
  say "      Set it to 0.0.0.0 there only for a node that serves a public RPC (an operator's DENDRA_NODE, a"
  say "      state-sync source for joiners): the miner kit does not need it, it reaches the node by its alias."
  # This file describes the validator identity and designates its VRF key (DENDRA_VRF_KEY_FILE): it is
  # closed at creation, like the miner kit's. Default permissions leave it readable by every account on
  # the machine — and a sensitive configuration file is never hardened after the fact.
  chmod 600 "$NODE_KIT/.env" 2>/dev/null || true
  say "  [OK] $NODE_KIT/.env written (SHA256 checked by node-join.sh at boot, fatal on mismatch)"
  # `--build` rebuilds and re-tags the image this compose names, and the node service carries both
  # `build:` and `image:`. On a machine that already runs a Dendra node, that moves the tag its
  # container will restart onto — and `up -d` then recreates it on a binary nobody chose. A different
  # binary computes state differently, so a node producing blocks on a live chain forks or halts.
  # The running container is pinned to an image ID, so it is untouched until something recreates it;
  # this is that something. Refuse rather than warn: at this point the operator is joining, not
  # upgrading, and the two are not the same gesture.
  local _run_img="" _tag_img="" _img_ref
  # The image a recreate would start is the one compose resolves (shell, then this kit's .env, then the
  # file's default), so compose is asked. Read from the shell alone, it would miss the pin every remedy
  # below tells the operator to write in the .env: that pin would be invisible to this guard, which would
  # then compare the running node with dendra/node:latest and refuse, or let through, the wrong thing.
  _img_ref="$( cd "$NODE_KIT" 2>/dev/null && docker compose config --images node 2>/dev/null | head -1 | tr -d '\r' )"
  _run_img="$(docker inspect "$( ( cd "$NODE_KIT" && docker compose ps -q node ) 2>/dev/null | head -1)" --format '{{.Image}}' 2>/dev/null || true)"
  # An image compose cannot name is not an image anyone checked: with a node running, that is a refusal.
  if [ -n "$_run_img" ] && [ -z "$_img_ref" ]; then
    die "a node is already running here, and the image a recreate would start could not be resolved ('docker compose config --images node' in $NODE_KIT answered nothing). Joining must never recreate a running node on an image nobody has checked: make that command answer, then re-run."
  fi
  _tag_img="$(docker image inspect "$_img_ref" --format '{{.Id}}' 2>/dev/null || true)"
  # Three answers, not two. An empty `_tag_img` does not mean "nothing to refuse", it means
  # "nobody can say what compose would resolve this image to": the image is not present locally, so an
  # `up`/`recreate` will pull it, and that is precisely the case where the consensus binary can change
  # under a running node. Treating that emptiness as a pass reads an unknown as reassurance, on the
  # criterion that decides whether a recreate can fork the node. On a security criterion an unknown is
  # a refusal. (The sibling script takes the same line: `bond_validator.sh` refuses an image it cannot
  # resolve.)
  if [ -n "$_run_img" ] && [ -z "$_tag_img" ]; then
    die "a node is already running here, and the image $_img_ref is not present locally, so nothing can
   say what a recreate would resolve it to. Pull or build it first (docker pull $_img_ref), or pin the
   running image with the line DENDRA_NODE_IMAGE=$_run_img in $NODE_KIT/.env (an image ID: started as it
   is, never rebuilt) — joining must never recreate a running node on an image nobody has checked."
  fi
  if [ -n "$_run_img" ] && [ -n "$_tag_img" ] && [ "$_run_img" != "$_tag_img" ]; then
    die "a node is already running here on image ...${_run_img: -12}, while $_img_ref now points at ...${_tag_img: -12}.
   Rebuilding would recreate that node on a different consensus binary and can fork it off the chain.
   Either pin the running image (the line DENDRA_NODE_IMAGE=$_run_img in $NODE_KIT/.env) and re-run, or start a
   separate node: set DENDRA_PROJECT, DENDRA_P2P_PORT, DENDRA_RPC_PORT, DENDRA_REST_PORT and
   DENDRA_NODE_IMAGE to values of your own (a host port left at its default is already taken by the
   running node, and the second one fails to start) — see deploy/testnet-node/docker-compose.yml.
   Joining must never recreate a node you did not mean to touch."
  fi
  # A home this run creates belongs to this kit copy. The preflight refuses an existing home unless
  # DENDRA_ADOPT_EXISTING_HOME=1, so a second run of this script on the same network (an install.sh re-run)
  # was refused by the node it had itself created. When the node volume does not exist yet, the home about
  # to be built belongs to this kit copy, and the adoption is written now, once. A volume that already
  # exists stays the operator's decision (the preflight's a/b/c), and an unreadable volume list writes
  # nothing. node-join.sh still refuses a home whose genesis is not this network's.
  # The volume state is node_volume_state's, read once before the .env was rewritten: the project name
  # compose resolves (a DENDRA_PROJECT set in the .env, for a second node, included), and `unknown` when
  # either read fails -- which writes nothing.
  if [ "${NODE_VOLUME:-unknown}" = new ] && ! grep -q '^DENDRA_ADOPT_EXISTING_HOME=' "$NODE_KIT/.env" 2>/dev/null; then
    printf 'DENDRA_ADOPT_EXISTING_HOME=1\n' >> "$NODE_KIT/.env"
    say "  [i] new node volume ${NODE_PROJECT}_node-data: DENDRA_ADOPT_EXISTING_HOME=1 written to $NODE_KIT/.env (this kit copy creates that home, so a re-run reuses it)"
  fi
  # A pin an earlier run wrote into the .env is held to this tree's gates first (node_env_image_reconcile).
  node_env_image_reconcile || die "$NODE_ENV_IMAGE_WHY.
   This node's volume already exists (or could not be read), and that image is not known to be of this tree's
   consensus epoch, so it is not replaced here: changing the consensus binary of a node that
   holds chain state is an upgrade, not a join. A governed upgrade is carried by deploy/testnet-miner/node_upgrade_watch.sh, which
   starts the node on the new image before it runs the join with --after-upgrade <plan>. Where that watch is off
   (DENDRA_AUTO_UPDATE=0, a HiveOS rig), run this same command with --after-upgrade <the plan the node stopped for>
   once the node stands at that plan's halt: the chain confirms the halt, the node starts on this tree's node image,
   and the join goes on once it answers the plan applied. If the network moved to
   a new genesis, remove the node's volume (docker compose -f $NODE_KIT/docker-compose.yml down -v, keys copied
   out first) and re-run. To keep this node on that image deliberately, name it by its image ID (the line
   DENDRA_NODE_IMAGE=<the ID that docker image inspect --format {{.Id}} prints for it>, an operator's choice that
   join.sh starts as it is) and remove the DENDRA_NODE_IMAGE_BUILT_FOR and DENDRA_NODE_IMAGE_SET_BY lines; to drop
   it, remove all three lines."
  # The prebuilt image for a new node when the clone pins one, else a build from the clone: node_compose_up.
  if ! node_compose_up; then
    node_up_failure_report
    die "docker compose up failed for the node (the preflight verdict, if it refused, is printed above)"
  fi
  after_upgrade_finish || die "$AFTER_UPGRADE_WHY. Nothing is rolled back: the binary before the upgrade cannot pass the halt.
   Run this same command again once the network has passed the plan's height; it asks the node again."
  announce_sync_plan
  if ! wait_for_sync; then
    sync_failure_report
    # The fatal sentence carries the measurement, not a hypothesis. Whatever a reader keeps from this
    # run, it is this line — so it names the height, the head, the rate and the fact that the node
    # itself is unaffected, and it points at nothing it has not observed.
    if [ "${FORK_STATE:-unknown}" = "fork" ]; then
      _fork_img="$(_env_file_get "$NODE_KIT/.env" DENDRA_NODE_IMAGE)"
      die "Consensus fork at height $FORK_H: this node computes $FORK_LOCAL where the network published $FORK_NET. This is not a slow sync, and re-running will not fix it: the binary is not built from the source this chain runs${_fork_img:+ (the node runs the image that the line DENDRA_NODE_IMAGE=$_fork_img of $NODE_KIT/.env names)}. Do not bond on this node."
    fi
    if [ "${SYNC_H:-?}" = 0 ] && { [ "${SS_LOG:-}" = discovering ] || [ "${SS_LOG:-}" = offered ]; }; then
      die "$SYNC_WHY. The node is still up and still trying; re-running this command changes nothing for a home created for a state sync. To replay from block 1: docker compose -f $NODE_KIT/docker-compose.yml down -v, then this command with --replay (see the report above)."
    fi
    die "stopped waiting for the sync at height $SYNC_H of a network head at $SYNC_HEAD ($(_fmt_rate "$SYNC_RATE_CBS") sustained, height last moved ${SYNC_STALL_FOR} s ago, waited ${SYNC_ELAPSED} s): $SYNC_WHY. The node container is still running and still syncing — see the report above; re-run this command to pick it up."
  fi
}

run_validator(){
  say "== [join] role: validator (sync -> confirmed bond -> VRF anchoring) =="
  # The anti-MITM gate matters most on this role, hence the call before any node starts.
  # `verify_genesis_info` checks the genesis actually served against the digest announced out of band.
  # This role is the one that locks up capital and signs blocks, so it has the most to lose from a
  # substituted genesis: a deceived miner serves work against the wrong chain and wastes its own
  # compute, while a deceived validator signs that chain with its stake bonded on it — and the bond
  # lives on the fork, not on the chain it meant to join.
  verify_genesis_info
  verify_consensus_epoch
  verify_kit_version
  verify_release_signers
  # What this run costs, stated before anything is spent.
  # The bond instructions and the jail figures both live at the end of this function, i.e. behind the
  # sync wait — which, with no state-sync trust point published, is tens of minutes. An operator who
  # walks away during that wait has been told nothing about the risk they are about to take on. This
  # block is a forward reference, not a second copy: it names the exposure and hands over the chain
  # reads, and the figures themselves are still printed once, at the end, by validator_health_notice.
  say ""
  say "  What this command is about to do, in order:"
  say "    1. start a full node and wait until it has caught up (the long step, see the note below);"
  say "    2. print the bond instructions. join.sh bonds nothing on its own, ever;"
  say "    3. print your jail exposure read from the chain, and schedule an hourly watch."
  say ""
  say "  Read this before step 2 rather than after it:"
  say "  once bonded, unplanned downtime jails your validator and takes a percentage of your"
  say "  stake. Any outage that outlasts the window is enough, and nothing warns you — your node keeps"
  say "  running, it simply stops being in the consensus set. How many blocks you may miss, for how"
  say "  long you stay jailed and what fraction is taken are governed parameters of this chain: no"
  say "  figure for them is written into this script, and none is repeated here. Read them yourself,"
  say "  now or at the end of this run:"
  _jail_params_cmds
  say ""
  start_local_node
  cat <<'EOF'
  ------------------------------------------------------------------
  Becoming a validator means locking up stake (a deliberate action).
  join.sh never bonds silently.

  One command (recommended) — still deliberate: it prints the plan and bonds nothing until you
  confirm, computes an amount that keeps every validator under 2/3, then anchors the VRF key and
  restarts the node with it (the step everyone skips, without which you do not feed the seed):

      bash deploy/bond_validator.sh              # shows the plan, changes nothing
      bash deploy/bond_validator.sh --yes-bond   # does it, end to end, then verifies

  Or the same thing by hand:
    1. Create your operator key (keyring 'test' = unencrypted, fine for devnet, not for real value):
         docker compose -f deploy/testnet-node/docker-compose.yml exec node \
           dendrad keys add validator --keyring-backend test
    2. Fund the address: external joiner -> faucet/channel; operator bootstrap -> send from the genesis
       node's 'validator' key (keep stake distribution <2/3: the faucet is not enough).
    3. Bond (create-validator) — SDK 0.50+ = validator.json file, see deploy/testnet-node/README.md.
    4. Anchor your VRF key. Without it your node produces blocks and contributes nothing to the seed,
       and the seed gates every audit draw, so nothing gets verified and no held payment is released.
       Docker node: pipe the script into the container (it has dendrad + dendra-vrf + the keyring):
         tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | docker compose -f deploy/testnet-node/docker-compose.yml \
           exec -T -e HOME_DIR=/root/.dendra -e CHAIN_ID="$(sed -n 's/^CHAIN_ID=//p' deploy/testnet-node/.env)" -e NODE=tcp://localhost:26657 -e VAL_KEY=validator node bash -s
    5. Tell the node where the key lives, then restart it. The variable is read from the node kit's
       env file, so the line goes there — exporting it in your shell does not reach the container.
       And the restart must force a recreate: docker compose up -d recreates a container only when the
       configuration changed, while join.sh preserves this very line across its rewrites, so an
       identical line reads as "no change", the node keeps running, and it never loads the key. The
       result is the failure step 4 warns about (anchored on-chain, zero signed vote extensions),
       while every command on screen prints success:
         echo 'DENDRA_VRF_KEY_FILE=/root/.dendra/config/vrf_key' >> deploy/testnet-node/.env
         docker compose -f deploy/testnet-node/docker-compose.yml up -d --force-recreate
    6. Verify (contributors should rise). dendrad lives inside the container, like in steps 1 and 4:
         docker compose -f deploy/testnet-node/docker-compose.yml exec -T node \
           dendrad query jobs committee-seed-health
       That command answers one question, whether the seed is being fed, and it cannot tell a jailed
       validator from a broken vote extension. For that, see the jail watch printed below.
    7. Check whether anyone can reach you. This is not a formality and it is not about your node's
       health: a node nobody can dial has one path for every consensus message, so a single missed
       block proposal is not recoverable and the round is played without it. Measured on this network,
       five nodes out of five were in that state, each dialling out to the same single host, and no
       indicator anywhere said so — because from the inside such a node looks perfectly healthy.
         bash deploy/node_reachability.sh
       It tells you which of the two fixes you need, in which order, and it refuses to guess: it
       cannot dial you from outside, so it reports what did arrive and says as much.
  ------------------------------------------------------------------
EOF
  validator_health_notice
}

# Library mode — `DENDRA_JOIN_LIB=1 . deploy/join.sh` loads the functions and stops before any role runs.
# Two consumers, and both exist so that a check lives in one place: the benches, which drive the code that
# actually ships instead of a copy of it, and docker/cloud-start.sh, the rented-pod entry point, which
# checks the network's identity with these very functions (verify_genesis_info, verify_consensus_epoch,
# verify_kit_version, payout_address_check) rather than with a second implementation that would drift.
# Sourcing runs this file's top-level code too (argument parsing, the hardware probe, the repository-root
# search): a consumer clears "$@" and leaves CONFIG_URL unset first. Nothing else in this file reads this
# variable, and it is not in the CONFIG_URL allow-list. The one-identity-per-card benches drive plan_gpu_slots,
# write_slot_env, write_slot_override and the start of the slots this way (dendra_multigpu_*_test.sh).
case "${DENDRA_JOIN_LIB:-0}" in 1) return 0 2>/dev/null || exit 0;; esac

# One identity per card: read the cards and the slots, refuse what must be refused, and print --plan --
# all before the pre-flight, which may run (and pull) a container.
gpu_mode_preflight
check_prereqs
case "$ROLE" in
  miner) run_miner;;
  validator) run_validator;;
esac
