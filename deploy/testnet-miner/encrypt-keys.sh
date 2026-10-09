#!/usr/bin/env bash
# encrypt-keys.sh -- encrypt the keys of a miner that keeps them IN CLEAR (the `test` keyring), in place,
# without changing its address. Plan first, nothing without --yes.
#
# WHAT IT DOES, IN THIS ORDER (with --yes):
#   1. reads the miner's keyring from its own volume, in a one-off container of the miner image: its state
#      (test, file, both, none), its keys and their addresses, and whether the 24-word recovery phrase of
#      the key is still on the volume;
#   2. REFUSES while that phrase has not been written down: an encrypted keyring whose passphrase file is
#      lost can only be rebuilt from those words, and without them the account and its rewards are gone;
#   3. creates the passphrase file when there is none -- ~/.config/dendra/miner-secrets/keyring-passphrase,
#      the directory 0700, the file 0600, outside the volume and outside this clone -- and writes its
#      directory into this kit's .env (DENDRA_SECRETS_DIR), which mounts it read-only into the miner;
#   4. stops the miner (a running miner holds the keyring), then, in a one-off container: exports every key
#      from keyring-test, imports it into keyring-file under the passphrase, reads each back and compares
#      its address with the one it had -- keyring-test is removed only once EVERY address matches; on any
#      failure keyring-file is removed and keyring-test is left as it was;
#   5. seals the miner's other key files (.sk, .vrf, .attestkey) with the same passphrase, atomically;
#   6. starts the miner again and reads the keyring back: encrypted, same address.
#
# IT CHANGES NOTHING WITHOUT --yes. Run it once without the flag: it prints what it found and what it would do.
#
# WHAT ENCRYPTION PROTECTS, SAID PLAINLY: the miner-keys volume and every backup of it -- a copy of the volume
# alone no longer holds a usable key. NOT a host that is compromised while the miner runs: the passphrase
# sits on the same host, by design, so that the miner restarts unattended.
# BACK UP BOTH the volume and the passphrase file, and keep them apart: together they are a key in clear.
#
# WHERE IT RUNS: on the host that runs the miner kit, from any directory. Every read of the keyring and the
# migration run in a one-off container of the miner's own image (`docker compose run --rm --no-deps`), with
# the miner's own keyring module (modea/keyring.py): the host needs Docker, nothing else.
#
# ONE IDENTITY PER CARD (deploy/join.sh --gpus): each card is a miner identity of its own, a "slot", with its
# own keyring and its own 24-word phrase, and ONE passphrase file for the whole machine. --slot <k> names the
# identity, --all takes them one after the other (each refused on its own if its phrase is not written down).
# On such a machine --yes needs one of the two: nothing is encrypted on slot 0 by default.
#
# Usage:
#   bash deploy/testnet-miner/encrypt-keys.sh                 # read and plan, change nothing
#   bash deploy/testnet-miner/encrypt-keys.sh --yes           # encrypt
#   ... --recovery-phrase-written                              # declare that the 24 words are on paper when
#                                                               # this machine holds no record of it
#   ... --no-recovery-phrase="<why>"                           # declare that this key HAS NO 24 words you
#                                                               # hold (created before the kit kept the phrase:
#                                                               # dendrad printed it once, into a log nobody
#                                                               # read). A reason is required; the passphrase
#                                                               # file and the volume become its ONLY backup
#   ... --slot <k> | --all                                     # one identity per card: which one(s)
#
# Exit codes -- three answers, never two:
#   0  encrypted and verified; or nothing to do (already encrypted AND opening with its passphrase, or no
#      key yet)
#   1  a step was attempted and failed (the keyring is then left as it was, or the message says where it is)
#   2  refused: no --yes, the recovery phrase not written down (nor declared absent, with a reason), a
#      declaration without its reason or contradicting the volume, two keyrings at once, a passphrase directory
#      that is not a directory of its own (deploy/testnet-miner/passphrase-dir.sh)
#   3  not measurable: Docker or the kit unreadable, the keyring unreadable -- an encrypted keyring whose
#      passphrase is missing or does not open it included
set -u

YES=0; WRITTEN=0; SLOT=""; ALL=0; NOPHRASE=""
while [ $# -gt 0 ]; do case "$1" in
  --yes) YES=1; shift;;
  --recovery-phrase-written) WRITTEN=1; shift;;
  # A KEY THAT NEVER HAD A PHRASE ITS OWNER HOLDS cannot be declared "written": it is declared as what it is,
  # with its reason -- a declaration is a sentence, never a box ticked. The bare form and an empty or blank
  # reason are refused.
  --no-recovery-phrase=*)
          NOPHRASE="${1#--no-recovery-phrase=}"
          [ -n "$(printf '%s' "$NOPHRASE" | tr -d '[:space:]')" ] \
            || { echo "[encrypt-keys] --no-recovery-phrase needs a reason: --no-recovery-phrase=\"<why this key has no 24 words you hold>\". Nothing was done."; exit 2; }
          shift;;
  --no-recovery-phrase)
          echo "[encrypt-keys] --no-recovery-phrase needs a reason, in one word with it: --no-recovery-phrase=\"<why this key has no 24 words you hold>\". Nothing was done."; exit 2;;
  --slot) SLOT="${2:-}"
          case "$SLOT" in 0|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) : ;; *) echo "[encrypt-keys] --slot needs a slot number (bash deploy/testnet-miner/slots.sh list)"; exit 2;; esac
          shift 2;;
  --all) ALL=1; shift;;
  -h|--help) awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; exit 0;;
  *) echo "[encrypt-keys] unknown argument: $1 (see --help)"; exit 2;;
esac; done
[ "$ALL" = 1 ] && [ -n "$SLOT" ] && { echo "[encrypt-keys] --slot and --all: pick one"; exit 2; }
# The declaration is about ONE identity's 24 words: it is never made for several at once.
[ "$ALL" = 1 ] && [ "$WRITTEN" = 1 ] && { echo "[encrypt-keys] --recovery-phrase-written declares the words of ONE identity: use it with --slot <k>, one identity at a time"; exit 2; }
[ "$ALL" = 1 ] && [ -n "$NOPHRASE" ] && { echo "[encrypt-keys] --no-recovery-phrase declares the key of ONE identity: use it with --slot <k>, one identity at a time"; exit 2; }
# The two declarations contradict each other: the words are on paper, or there are none.
[ "$WRITTEN" = 1 ] && [ -n "$NOPHRASE" ] && { echo "[encrypt-keys] --recovery-phrase-written and --no-recovery-phrase say opposite things about the same 24 words: pick the one that is true. Nothing was done."; exit 2; }

say(){ printf '%s\n' "$*"; }
refuse(){ printf '  [REFUSED] %s\n' "$*" >&2; }
unmeasurable(){ printf '  [?] %s\n' "$*" >&2; exit 3; }
fail(){ printf '  [FAILED] %s\n' "$*" >&2; exit 1; }

KIT="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -n "$KIT" ] && [ -f "$KIT/docker-compose.yml" ] || unmeasurable "the miner kit (docker-compose.yml) is not next to this file."
[ -f "$KIT/.env" ] || unmeasurable "$KIT/.env is missing: no miner was set up from this kit (deploy/join.sh writes it)."
command -v docker >/dev/null 2>&1 || unmeasurable "docker is not installed: the miner's keys are read through it."
docker compose version >/dev/null 2>&1 || unmeasurable "docker compose (v2) does not answer."
APP_JSON="${XDG_CONFIG_HOME:-$HOME/.config}/dendra/app.json"

# ---------------------------------------------------------------- which identity: the slot
# Every compose command of this file goes through slots.sh (the one place that knows a slot's project, env
# and files): a hard-coded project name acts on slot 0 whatever slot was meant.
{ [ -r "$KIT/slots.sh" ] && . "$KIT/slots.sh" && declare -F slot_compose >/dev/null 2>&1; } \
  || unmeasurable "the slot library ($KIT/slots.sh) cannot be loaded: which identity this would act on is not known."
ALL_SLOTS="$(slot_ids --all)" || unmeasurable "the slots of this kit ($KIT/gpu) cannot be read: which identity this would act on is not known."
NSLOTS="$(printf '%s\n' "$ALL_SLOTS" | awk 'NF{n++} END{print n+0}')"
if [ -z "$SLOT" ] && { [ "$ALL" = 1 ] || [ "$NSLOTS" -ge 2 ]; }; then
  if [ "$ALL" != 1 ] && [ "$YES" = 1 ]; then
    refuse "this machine runs $NSLOTS miner identities, one per card: name one (--slot <k>) or take them all (--all). Nothing was done."
    exit 2
  fi
  # Each identity on its own, one after the other. Exit: 3 if one could not be read, else 1 if one failed,
  # else 2 if one was refused or only planned, else 0.
  _worst=0
  for _k in $ALL_SLOTS; do
    say ""
    say "######## slot $_k ($(slot_project "$_k"), $(slot_state "$_k")) ########"
    if [ "$YES" = 1 ]; then bash "$KIT/$(basename "$0")" --slot "$_k" --yes; else bash "$KIT/$(basename "$0")" --slot "$_k"; fi
    _rc=$?
    case "$_rc" in 0|1|2|3) : ;; *) _rc=3 ;; esac
    case "$_rc:$_worst" in
      3:*|*:3) _worst=3 ;;
      1:*|*:1) _worst=1 ;;
      2:*|*:2) _worst=2 ;;
      *) : ;;
    esac
  done
  exit "$_worst"
fi
SLOT="${SLOT:-0}"
case "$(slot_state "$SLOT")" in
  active|retired) : ;;
  missing) refuse "this kit has no slot $SLOT (bash $KIT/slots.sh list shows them). Nothing was done."; exit 2 ;;
  *) unmeasurable "the env of slot $SLOT ($(slot_env "$SLOT")) cannot be read." ;;
esac
ENVF="$(slot_env "$SLOT")"
KEYS_VOLUME="$(slot_project "$SLOT")_miner-keys"
if [ "$SLOT" = 0 ]; then
  STOP_HINT="docker compose -p dendra-miner stop miner"; START_HINT="cd $KIT && docker compose up -d --no-build"; SLOT_FLAG=""
else
  STOP_HINT="$(slot_run_hint "$SLOT" stop miner)"; START_HINT="$(slot_run_hint "$SLOT" up -d --no-build)"; SLOT_FLAG=" --slot $SLOT"
fi
# A RETIRED slot (its card left join.sh --gpus) is stopped on purpose, and nothing starts it but join.sh:
# its keys are encrypted in place and it stays stopped.
RETIRED=0; [ "$(slot_state "$SLOT")" = retired ] && RETIRED=1

# The passphrase directory: the one the slot's env names, else slot 0's (ONE passphrase file per machine:
# every identity is encrypted with it), else the kit's default place, outside the clone.
SD="$(sed -n 's/^DENDRA_SECRETS_DIR=//p' "$ENVF" 2>/dev/null | head -1 | tr -d '\r')"
if [ "$SLOT" != 0 ]; then
  _sd0="$(sed -n 's/^DENDRA_SECRETS_DIR=//p' "$KIT/.env" 2>/dev/null | head -1 | tr -d '\r')"
  if [ -n "$SD" ] && [ -n "$_sd0" ] && [ "$SD" != "$_sd0" ]; then
    refuse "slot $SLOT names DENDRA_SECRETS_DIR=$SD and slot 0 names $_sd0: this machine keeps ONE passphrase file for every identity. Align the two lines (deploy/join.sh --gpus writes slot 0's into each slot). Nothing was changed."
    exit 2
  fi
  [ -n "$SD" ] || SD="$_sd0"
fi
[ -n "$SD" ] || SD="${XDG_CONFIG_HOME:-$HOME/.config}/dendra/miner-secrets"
# The check is passphrase-dir.sh, next to this file: the ONE copy join.sh and uninstall.sh source too -- a
# directory of its own, outside the clone, never $HOME. It is read before anything; missing, nothing is judged.
[ -r "$KIT/passphrase-dir.sh" ] && . "$KIT/passphrase-dir.sh" && declare -F dendra_secrets_dir_check >/dev/null 2>&1 \
  || unmeasurable "the check of the passphrase directory ($KIT/passphrase-dir.sh) cannot be loaded."
_sdwhy="$(dendra_secrets_dir_check "$SD" "$(cd "$KIT/../.." 2>/dev/null && pwd)")" \
  || { refuse "DENDRA_SECRETS_DIR=$SD cannot hold the keyring passphrase: $_sdwhy."; exit 2; }
PF="$SD/keyring-passphrase"

# ---------------------------------------------------------------- the helper, run INSIDE the miner image
# helper <subcommand> [args] -- the miner image's own `python3 -m modea.keyring`, in a one-off container of
# the slot's miner service, through slots.sh: from the kit's directory, with the slot's env and files (slot 0:
# the kit's .env and its COMPOSE_FILE, as always), so that it mounts THIS slot's keys.
# DENDRA_SECRETS_DIR is passed in the ENVIRONMENT of compose (never of the container): compose mounts that
# directory read-only at /run/dendra-secrets, exactly as for the miner. SLOT_KEEP_ENV keeps it there for a
# slot k, whose command otherwise leaves out every variable its compose files read (slots.sh, (b)).
H_OUT=""; H_ERRF="$(mktemp)"; trap 'rm -f "$H_ERRF"' EXIT
helper(){ # helper <stdin file|-> <args...>
  local in="$1"; shift
  if [ "$in" = - ]; then in=/dev/null; fi
  H_OUT="$( SLOT_KEEP_ENV=DENDRA_SECRETS_DIR DENDRA_SECRETS_DIR="$H_SD" slot_compose "$SLOT" run --rm --no-deps -T \
            --entrypoint python3 miner -m modea.keyring "$@" < "$in" 2>"$H_ERRF" )"
  [ -n "$H_OUT" ] || H_OUT="why=$(tail -1 "$H_ERRF" 2>/dev/null)"
}
val(){ printf '%s\n' "$H_OUT" | sed -n "s/^$1=//p" | head -1; }

# _env_set <KEY> <value> -- one line of the slot's env, replaced in place or appended; every other line
# copied as it is, and the file keeps its mode (it carries the relay token). A slot k's env is rewritten by
# slots.sh (slot_env_set), slot 0's in place as it always was.
_env_set(){
  local t
  [ "$SLOT" = 0 ] || { slot_env_set "$SLOT" "$1" "$2"; return $?; }
  t="$(mktemp)" || return 1
  awk -v k="$1" -v v="$2" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s) print k "=" v; s = 1; next }
    { print }
    END { if (!s) print k "=" v }' "$ENVF" > "$t" && cat "$t" > "$ENVF"
  local rc=$?
  rm -f "$t"
  return $rc
}

say "== [encrypt-keys] reading the miner's keyring (kit: $KIT$( [ "$SLOT" = 0 ] || printf ', slot %s: %s' "$SLOT" "$(slot_project "$SLOT")")) =="
# The read mounts the passphrase directory only if it already exists: a plan creates nothing.
H_SD=""; [ -f "$PF" ] && H_SD="$SD"
if [ -r "$APP_JSON" ]; then helper "$APP_JSON" plan /data/keys --app-settings -; else helper - plan /data/keys; fi
STATE="$(val state)"; MID="$(val id)"; ADDR="$(val address)"; NKEYS="$(val keys)"
RECOVERY="$(val recovery)"; RADDR="$(val recovery_address)"; CONFIRMED="$(val confirmed_address)"
say "  keyring     : ${STATE:-unread} $( [ -n "$NKEYS" ] && printf '(%s key(s): %s)' "$NKEYS" "$(val names)")"
say "  identity    : ${MID:-none resolved yet}${ADDR:+ -> $ADDR}"
say "  recovery    : ${RECOVERY:-unread}${RADDR:+ (phrase of $RADDR)}"
if [ -f "$PF" ]; then say "  passphrase  : $PF (exists)"
elif [ "$STATE" = file ]; then say "  passphrase  : $PF MISSING -- the keyring is encrypted and does not open without it"
else say "  passphrase  : $PF (to create)"; fi

case "$STATE" in
  file)
    # ALREADY ENCRYPTED IS "NOTHING TO DO" ONLY WHEN IT OPENS. A `file` keyring whose passphrase is missing
    # or wrong is the state in which the miner stops: the plan reads it back (`opens`, modea/keyring.py::plan)
    # and anything but a positive reading is that, said with its reason -- never an [OK].
    if [ "$(val opens)" != yes ]; then
      if [ -f "$PF" ]; then
        printf '  [?] %s\n' "the keyring is ENCRYPTED and the passphrase $PF does NOT open it: $(val why)" >&2
      else
        printf '  [?] %s\n' "the keyring is ENCRYPTED and its passphrase is MISSING ($PF): $(val why)" >&2
      fi
      say "      The miner stops on this keyring and creates no key. Restore the keyring-passphrase it was encrypted"
      say "      with from your backup into $SD (or name its directory as DENDRA_SECRETS_DIR in $ENVF)."
      say "      Without it, the 24-word recovery phrase rebuilds the key. Nothing was changed."
      exit 3
    fi
    say "  [OK] the keyring is already ENCRYPTED and opens with its passphrase: nothing to do."
    if [ "$(sed -n 's/^DENDRA_SECRETS_DIR=//p' "$ENVF" 2>/dev/null | head -1 | tr -d '\r')" != "$SD" ]; then
      say "  [!] but $ENVF does not name $SD as DENDRA_SECRETS_DIR: the miner would not find its passphrase."
      say "      Fix that line by hand (the directory that holds the keyring-passphrase this keyring was encrypted with)."
    fi
    exit 0 ;;
  none)
    say "  [i] no key in the volume yet. The next start creates the keyring ENCRYPTED when $ENVF names the"
    say "      passphrase directory (deploy/join.sh writes it; --plain-keys was the choice of keys in clear)."
    exit 0 ;;
  both)
    refuse "keyring-test AND keyring-file both hold keys in the volume: two possible identities. This file does not"
    say "         choose between them. Keep the one whose address is this miner's operator (dendrad query jobs get-miner $MID)"
    say "         and move the other out of the volume, by hand."
    exit 2 ;;
  test) : ;;
  *) unmeasurable "the keyring could not be read: $(val why)" ;;
esac
[ -n "$MID" ] && [ -n "$ADDR" ] || unmeasurable "the miner's identity or its address could not be read from the volume: $(val why)"

# THE RECOVERY PHRASE MUST BE WRITTEN DOWN FIRST. Encrypted, the key depends on the passphrase file; if that
# file is lost, the 24 words are the only way back. Three readings:
#   - the phrase is still on the volume: it was never confirmed by the application (or by an older one that
#     did not remove it); confirming it there removes it, then this file runs;
#   - absent, and the application recorded the three words typed back for THIS address: written down;
#   - absent, with no record: only its owner knows which of two it is, and says it. Either the 24 words are on
#     paper (--recovery-phrase-written), or this key never had words its owner holds -- created before the kit
#     kept the phrase (miner.py::keep_recovery_phrase), dendrad printed it once into a container log
#     nobody read -- and "written" would be false: --no-recovery-phrase="<why>", which says what it costs.
case "$RECOVERY" in
  present)
    # THE WORDS ARE ON THIS VOLUME: "no recovery phrase" would be false. They are written down first.
    if [ -n "$NOPHRASE" ]; then
      refuse "--no-recovery-phrase: the 24-word recovery phrase of ${RADDR:-this key} IS on this machine. Write it down instead"
      say "         (the Dendra application shows it, asks for three of its words, then removes it). Nothing was done."
      exit 2
    fi
    if [ "$RADDR" = "$ADDR" ] && [ "$CONFIRMED" = "$ADDR" ]; then
      say "  [i] the recovery phrase was confirmed earlier and is still on the volume: it is sealed with the keys."
      say "      Open the Dendra application to remove it from this machine (three words, then the button)."
    else
      refuse "the 24-word recovery phrase of $ADDR is still on this machine and was NOT confirmed as written down."
      say "         Open the Dendra application: it shows the words, asks for three of them, and 'I wrote it down -"
      say "         remove it from this machine' removes it. Then run this again. Encrypting before that would leave"
      say "         the account depending on a passphrase file alone."
      exit 2
    fi ;;
  absent)
    if [ "$CONFIRMED" = "$ADDR" ]; then
      say "  [OK] the recovery phrase of $ADDR was typed back in the application: written down."
      [ -n "$NOPHRASE" ] && say "  [i] --no-recovery-phrase is not needed: this machine records the words of $ADDR as written down."
    elif [ "$WRITTEN" = 1 ]; then
      say "  [i] --recovery-phrase-written: you declare the 24 words of $ADDR are on paper. Nothing on this machine shows it."
    elif [ -n "$NOPHRASE" ]; then
      say "  [!] --no-recovery-phrase: you declare that $ADDR has NO 24 words you hold. Your reason: $NOPHRASE"
      say "      ONCE ENCRYPTED, THE PASSPHRASE FILE $PF AND THE VOLUME $KEYS_VOLUME ARE THE ONLY BACKUP OF THIS KEY:"
      say "      lose either one and the account, its stake and its rewards are gone -- no words can rebuild it."
    else
      refuse "nothing on this machine shows that the 24-word recovery phrase of $ADDR was written down (the application"
      say "         records it when three of its words are typed back). Say which of two is true:"
      say "           bash $KIT/encrypt-keys.sh${SLOT_FLAG} --recovery-phrase-written        # you hold the 24 words"
      say "           bash $KIT/encrypt-keys.sh${SLOT_FLAG} --no-recovery-phrase=\"<why>\"   # this key never had words you hold"
      say "         (a key created before the kit kept its phrase: dendrad printed it once, into a log nobody read). With"
      say "         the second, the passphrase file and the volume become the only backup of the key."
      exit 2
    fi ;;
  *) unmeasurable "whether the recovery phrase is on the volume could not be read: $(val why)" ;;
esac

say ""
say "== [encrypt-keys] plan =="
[ -f "$PF" ] && say "  - keep the passphrase file $PF" || say "  - create $PF (directory 0700, file 0600, 64 random hex digits)"
say "  - write DENDRA_SECRETS_DIR=$SD into $ENVF (mounted read-only into the miner at /run/dendra-secrets)"
say "  - stop the miner: $STOP_HINT"
say "  - in a one-off container: export each key from keyring-test, import it into keyring-file under the passphrase,"
say "    read each back and compare its address; remove keyring-test only once every address matches"
say "  - seal $MID.sk, $MID.vrf and $MID.attestkey with the same passphrase"
if [ "$RETIRED" = 1 ]; then say "  - leave the miner stopped (slot $SLOT is retired) and read the keyring back"
else say "  - start the miner again ($START_HINT) and read the keyring back"; fi
say ""
say "  AFTERWARDS, BACK UP BOTH: the Docker volume $KEYS_VOLUME AND $PF. Keep them apart:"
say "  together they are a key in clear. This protects the volume and its backups, not a compromised host."
[ -n "$NOPHRASE" ] && [ "$CONFIRMED" != "$ADDR" ] \
  && say "  WITHOUT 24 WORDS (--no-recovery-phrase), those two backups are the ONLY way back to this key."
if [ "$YES" != 1 ]; then
  say ""
  _decl=""; [ "$WRITTEN" = 1 ] && _decl=" --recovery-phrase-written"
  [ -n "$NOPHRASE" ] && _decl=" --no-recovery-phrase=\"$NOPHRASE\""
  say "  Nothing was done. Re-run with --yes${SLOT_FLAG}${_decl} to encrypt."
  exit 2
fi

say ""
say "== [encrypt-keys] encrypting =="
if [ ! -f "$PF" ]; then
  ( umask 077; mkdir -p "$SD" ) || fail "cannot create $SD: nothing was changed."
  chmod 700 "$SD" || fail "cannot set $SD to 0700: nothing was changed."
  _t="$(umask 077; mktemp "$SD/.keyring-passphrase.XXXXXX")" || fail "cannot write in $SD: nothing was changed."
  ( umask 077; head -c 32 /dev/urandom | od -An -vtx1 | tr -dc '0-9a-f' > "$_t" ) || { rm -f "$_t"; fail "the passphrase could not be generated."; }
  [ "$(wc -c < "$_t" | tr -dc '0-9')" = 64 ] || { rm -f "$_t"; fail "the generated passphrase is not 64 hex digits: nothing was changed."; }
  chmod 600 "$_t"
  # `ln` refuses an existing name: a passphrase that appeared meanwhile is never replaced.
  ln "$_t" "$PF" 2>/dev/null || { rm -f "$_t"; fail "$PF appeared while it was being written: run this again, it will use it."; }
  rm -f "$_t"
  say "  [OK] passphrase created: $PF"
fi
_env_set DENDRA_SECRETS_DIR "$SD" || fail "$ENVF could not be updated: nothing else was changed."
say "  [OK] DENDRA_SECRETS_DIR=$SD written into $ENVF"
slot_compose "$SLOT" stop miner >/dev/null 2>&1 \
  || fail "the miner could not be stopped ($STOP_HINT): the keyring was not touched."
say "  [OK] miner stopped"
H_SD="$SD"
helper - migrate /data/keys --yes
RES="$(val result)"
printf '%s\n' "$H_OUT" | sed -n 's/^verified=/  [OK] same address after import: /p; s/^file=/  [OK] key file: /p'
case "$RES" in
  migrated) say "  [OK] keyring-test removed: the keyring is ENCRYPTED" ;;
  failed)
    say "  [FAILED] $(val why)"
    say "  $(val rolled_back)"
    [ "$RETIRED" = 1 ] || { slot_compose "$SLOT" up -d --no-build >/dev/null 2>&1 && say "  the miner was started again, on its keys in clear."; }
    exit 1 ;;
  *) fail "the migration gave no readable answer: $(val why). The miner is STOPPED; run this file again: it reads where the keyring stands." ;;
esac
if [ "$RETIRED" = 1 ]; then
  say "  [i] slot $SLOT is retired: it stays stopped (deploy/join.sh --gpus with its card starts it again)"
else
  slot_compose "$SLOT" up -d --no-build >/dev/null 2>&1 \
    || fail "the miner could not be started again ($START_HINT): the keyring IS encrypted."
  say "  [OK] miner started again"
fi
H_SD="$SD"
helper - plan /data/keys
if [ "$(val state)" = file ] && [ "$(val id)" = "$MID" ] && [ "$(val address)" = "$ADDR" ]; then
  say "  [OK] read back: the keyring is encrypted, $MID -> $ADDR (the same address)"
else
  fail "read back: keyring '$(val state)', identity '$(val id)', address '$(val address)' -- expected an encrypted keyring for $MID at $ADDR. $(val why)"
fi
say ""
say "  Done. BACK UP NOW: the volume $KEYS_VOLUME AND $PF, kept apart."
exit 0
