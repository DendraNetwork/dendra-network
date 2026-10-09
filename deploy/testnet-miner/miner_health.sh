#!/usr/bin/env bash
# miner_health.sh -- is THIS machine's miner doing its job? One command, on the host that runs the miner
# kit. It needs Docker and nothing else here: no JSON parser runs on this host.
#
#   bash deploy/testnet-miner/miner_health.sh              # a report
#   bash deploy/testnet-miner/miner_health.sh --json       # one JSON document (the Dendra application reads it)
#   bash deploy/testnet-miner/miner_health.sh --cron FILE  # the hourly job deploy/join.sh installs
#   bash deploy/testnet-miner/miner_health.sh --slot 2     # one identity of a machine that runs one per card
#   options: --quick (no model probe) · --write (C6 also re-deposits the anchored key on the relay, signed)
#            · --deadline S (the self-test's own bound inside the container; see SELFTEST_DEADLINE_S)
#
# WHAT IT CHECKS ON THIS HOST (H1-H6), then what it asks the miner container (C1-C10):
#   H1 containers   the slot's containers run: state and restart count, read with docker inspect
#   H2 image        the running miner image is the one the slot names, and the one this clone pins
#   H3 kit version  docker/KIT_VERSION of this clone and of the running image, against network-info's
#   H4 consensus    docker/CONSENSUS_EPOCH, the same three ways
#   H5 schedule     the hourly job is in the crontab, and its last run is recent
#   H6 GPU pinning  one identity per card only: the slot's engine runs on ITS card (slots.sh pin) --
#                   pinned (ok), on another card or the CPU (ko), or not readable (unmeasured)
#   C1-C10          python3 /app/miner_selftest.py --host, run IN the slot's miner container (docker exec):
#                   the node and its sync, the registration and keys, presence, the work queue, the relay
#                   write, the model, the capacity line, the processes, the judge's engine. Its header says
#                   what each reads.
#   A4 (advice)     the judge role, read in the container (miner_selftest.py --judge-role): when it is OFF,
#                   "miner -- silent juror seat". Advice is listed apart and never changes the exit code.
# Its remedies name THIS machine's update instruction and re-run of deploy/join.sh when join.sh recorded them in
# the kit's .env (DENDRA_KIT_UPDATE, DENDRA_KIT_RERUN), and the generic ones otherwise.
#
# ONE IDENTITY PER CARD (deploy/join.sh --gpus): each card is a miner identity of its own, a "slot", found
# through deploy/testnet-miner/slots.sh -- by its compose project, never by a directory every slot shares.
# Without --slot, and with more than one active slot, this file runs ITSELF once per active slot (--slot k;
# a retired slot is not checked) and gives ONE verdict for the machine: the worst slot's. The clone's gates
# (H3, H4) and the schedule (H5) are checked once, in slot 0's part; each self-test gets its share of the
# deadline, never less than SELFTEST_SLOT_FLOOR_S. --cron then writes ONE alert file for the machine, with a
# section per slot, and one miner-health.last.json PER slot: slot 0's where it always was, slot k's in its
# own directory (deploy/testnet-miner/gpu/<k>/). The crontab keeps ONE line. --part, --scheduled and
# --no-machine-checks are how this file runs one slot's part of such a run; nothing else passes them.
#
# --cron FILE: FILE EXISTS IF AND ONLY IF SOMETHING IS WRONG. It is removed when every check is ok. It
# holds the full report when a check is ko, keeping the time the problem was first seen. When a check
# could not be measured it holds a note that says so -- and such a run never erases a standing alert: a
# run that measured less does not overwrite one that measured a problem. miner-health.last.json, next to
# FILE, is ALWAYS written; the application reads it, and its age.
# This file writes nothing anywhere else, and changes nothing in Docker. Without --write it causes no
# write at all: C6 compares the relay's copy of this miner's key with the chain's and says what it finds.
# With --write (and only then), C6 re-deposits, signed, the encryption key the chain anchors for this
# miner -- never a different key, and nothing when it cannot sign. The hourly job does not pass --write.
#
# Exit codes -- three answers, never two:
#   0  every check ran and is ok (or this miner left the network with exit-miner.sh)
#   1  at least one check is ko
#   2  no ko, but at least one check could not be measured, or the run itself was impossible (Docker
#      unreadable, no kit set up here). A run that measured nothing is a 2, never a 0.
set -u

MODE=report; CRON_FILE=""; QUICK=0; WRITE=0; DEADLINE=""; SLOT_ARG=""; PART=""; SCHEDULED=0; NO_MACHINE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) [ "$MODE" = cron ] && { printf '%s\n' "[miner-health] --json and --cron are two modes: pick one" >&2; exit 2; }
            MODE=json; shift;;
    --slot)
      SLOT_ARG="${2:-}"
      case "$SLOT_ARG" in 0|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) : ;;
        *) printf '%s\n' "[miner-health] --slot needs a slot number (bash deploy/testnet-miner/slots.sh list)" >&2; exit 2;; esac
      shift 2;;
    # One slot's part of a machine run: this file runs itself so, and writes its result into that directory.
    --part)
      PART="${2:-}"
      [ -n "$PART" ] && [ -d "$PART" ] || { printf '%s\n' "[miner-health] --part needs an existing directory" >&2; exit 2; }
      shift 2;;
    --scheduled) SCHEDULED=1; shift;;
    --no-machine-checks) NO_MACHINE=1; shift;;
    --cron)
      [ "$MODE" = json ] && { printf '%s\n' "[miner-health] --json and --cron are two modes: pick one" >&2; exit 2; }
      CRON_FILE="${2:-}"
      # --cron TAKES A PATH, AND ONLY A PATH: `--cron --help` would otherwise create, and later remove, a
      # file named --help -- the alert surface of an unattended watch that nobody would ever look at.
      case "$CRON_FILE" in
        "") printf '%s\n' "[miner-health] --cron needs a file path" >&2; exit 2;;
        -*) printf '%s\n' "[miner-health] --cron needs a file PATH, and \"$CRON_FILE\" starts with '-': write ./$CRON_FILE if that really is the file." >&2
            exit 2;;
      esac
      MODE=cron; shift 2;;
    --quick) QUICK=1; shift;;
    --write) WRITE=1; shift;;
    # The default since --write exists; still accepted, so that a line written for an older kit runs.
    --no-write) WRITE=0; shift;;
    --deadline)
      DEADLINE="${2:-}"
      case "$DEADLINE" in ''|*[!0-9]*) DEADLINE=0;; esac
      [ "$DEADLINE" -ge 1 ] 2>/dev/null || { printf '%s\n' "[miner-health] --deadline needs a whole number of seconds, at least 1" >&2; exit 2; }
      shift 2;;
    -h|--help)
      _help="$(awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0" 2>/dev/null)"
      if [ -n "${_help:-}" ]; then printf '%s\n' "$_help"; exit 0; fi
      printf '%s\n' "[miner-health] --help prints this file's own header, and this run has no file to read (\$0 is \"$0\")." >&2
      exit 2;;
    *) printf '%s\n' "[miner-health] unknown argument: $1 (see --help)" >&2; exit 2;;
  esac
done
if [ -n "$PART" ]; then
  [ "$MODE" = report ] && [ -n "$SLOT_ARG" ] || { printf '%s\n' "[miner-health] --part runs one slot (--slot), without --json or --cron" >&2; exit 2; }
  MODE=part
fi

KIT="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -n "$KIT" ] || { printf '%s\n' "[miner-health] cannot locate the kit next to this file" >&2; exit 2; }
KIT_P="$(cd "$KIT" && pwd -P)"
REPO="$(cd "$KIT/../.." 2>/dev/null && pwd)"
SELF="$KIT/$(basename "$0")"
LAST_JSON="$KIT/miner-health.last.json"
[ "$MODE" = cron ] && LAST_JSON="$(dirname -- "$CRON_FILE")/miner-health.last.json"
EXITED="$KIT/miner-health.EXITED"
DEFAULT_ALERT="$KIT/miner-health.ALERT"
NOW_ISO="$(date -u +%FT%TZ 2>/dev/null || printf '?')"
NOW_EPOCH="$(date -u +%s 2>/dev/null || printf 0)"
# THE SCHEDULE'S PERIOD: the crontab line join.sh::install_miner_health_cron writes runs once an hour
# (a minute field, then four `*`). The ONE place this file states it -- dendra_mineur_sante_test.sh confronts
# it with that line -- and it travels in miner-health.last.json as schedule_period_s, which the application
# reads instead of keeping its own copy. A last run older than two periods is a schedule that did not fire.
SCHEDULE_PERIOD_S=3600
# THE SELF-TEST'S BOUND, DERIVED, NOT RETYPED. The self-test bounds ITSELF, inside the container
# (miner_selftest.py --deadline-s): a bound applied to `docker exec` from here only kills the client, and the
# self-test it started would keep running in the container. Half the schedule's period by default, so a
# run always ends before the next one starts (--deadline sets another, as the application does for its
# own shorter wait). The exec gets twice that: a backstop for a daemon that does not answer at all.
SELFTEST_DEADLINE_S="${DEADLINE:-$(( SCHEDULE_PERIOD_S / 2 ))}"
SELFTEST_TIMEOUT_S=$(( 2 * SELFTEST_DEADLINE_S ))
# ONE IDENTITY PER CARD: the slots are checked one after the other, so each self-test gets the deadline
# divided by the number of slots -- and never less than this FLOOR, a CHOICE (not a measure): room for C1's
# wait for a block and one generation.
SELFTEST_SLOT_FLOOR_S=120

# ---------------------------------------------------------------- the update instruction of THIS machine
# kitval <KEY> -> KEY in the KIT's own .env (slot 0's file), carriage return stripped: the lines that describe
# the machine and its clone, never one slot.
kitval(){ [ -r "$KIT/.env" ] || return 0; tr -d '\r' < "$KIT/.env" | sed -n "s/^$1=//p" | head -1; }
# _kit_line <KEY> -> the value when it holds only the characters deploy/join.sh::persist_kit_update writes
# (letters, digits, space and : / . _ ~ % ? = + @ , ; & ( ) -), else NOTHING: a line edited by hand into
# something else is not printed as the kit's instruction.
_kit_line(){
  local v; v="$(kitval "$1")"
  [ -n "$v" ] && [ -z "$(printf '%s' "$v" | LC_ALL=C tr -d 'A-Za-z0-9 :/._~%?=+@,;&()-')" ] && printf '%s' "$v"
}
# HOW THIS MACHINE UPDATES, AND ITS EXACT RE-RUN, as deploy/join.sh recorded them (DENDRA_KIT_UPDATE and
# DENDRA_KIT_RERUN, persist_kit_update): a package pinned to a release tag (HiveOS, install.sh --ref) is NOT
# updated by moving the clone to main, and a re-run without the options the kit was set up with (--judge,
# --gpus, ...) is another installation. A kit set up by an older join.sh has neither line: the generic
# instruction below stands, and says to re-run with the same options.
KIT_UPDATE="$(_kit_line DENDRA_KIT_UPDATE)"; KIT_RERUN="$(_kit_line DENDRA_KIT_RERUN)"
REJOIN_CMD="bash deploy/join.sh"
[ -n "$(kitval CONFIG_URL)" ] || REJOIN_CMD="CONFIG_URL=<the network-info URL> bash deploy/join.sh"
# THE KIT'S ONE UPDATE CONSIGNE (the same git words as deploy/join.sh::kit_update_hint and deploy/install.sh).
# `checkout -B`, never `reset --hard`: it refuses rather than overwrite a file git does not track; the branch
# kit-before-update keeps the tree the clone leaves, so a commit of the operator's own is not dropped in silence.
# One re-run only: install.sh --yes keeps none of the flags it was first given (join.sh::kit_update_hint).
UPDATE="cd $REPO && git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main, then $REJOIN_CMD with the same options   (the tree you leave stays on the branch kit-before-update until the next update; each release republishes the repository as a NEW root commit: git pull refuses it, 'refusing to merge unrelated histories')"
if [ -n "$KIT_UPDATE" ]; then
  UPDATE="$KIT_UPDATE"
  case "$KIT_UPDATE" in *"git checkout -B main origin/main"*)
    UPDATE="$KIT_UPDATE   (each release republishes the repository as a NEW root commit: git pull refuses it, 'refusing to merge unrelated histories')";; esac
fi
# The re-run a remedy names: this machine's own, with its options, when deploy/join.sh recorded it.
REJOIN_EXACT="${KIT_RERUN:-bash $REPO/deploy/join.sh with the options this kit was set up with}"

# ---------------------------------------------------------------- the record of checks
NCHK=0; NOK=0; NKO=0; NUN=0; HJSON=""; HTEXT=""; NOTE=""
ST_JSON=""; ST_RC=""; ST_TEXT=""
# The judge role read in the container (JR_WORD empty = not asked), and this file's own ADVICE: no state, so
# it moves neither a count nor the exit code -- the same rule as the self-test's advice (A1-A3).
JR_WORD=""; JR_WHY=""; ADV_JSON=""; ADV_TEXT=""
# js <text> -> a JSON string. Writing JSON needs no parser: newlines and tabs become spaces, the other
# control bytes are dropped, then backslash and quote are escaped -- the backslash first.
js(){
  printf '"%s"' "$(printf '%s' "$1" | LC_ALL=C tr '\t\n\r' '   ' | LC_ALL=C tr -d '\000-\037\177' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}
worse(){ if [ "$1" = ko ] || [ "$2" = ko ]; then echo ko; elif [ "$1" = ok ] && [ "$2" = ok ]; then echo ok; else echo unmeasured; fi; }
# rec <id> <name> <state> <measured> [reason] [fix] -- a state that is not ok or ko is counted UNMEASURED.
rec(){
  local id="$1" name="$2" st="$3" me="$4" re="${5:-}" fx="${6:-}" tag
  case "$st" in ok) NOK=$((NOK+1)); tag="[ok]";; ko) NKO=$((NKO+1)); tag="[KO]";; *) st=unmeasured; NUN=$((NUN+1)); tag="[??]";; esac
  NCHK=$((NCHK+1))
  HJSON="${HJSON:+$HJSON,}{\"id\":$(js "$id"),\"name\":$(js "$name"),\"state\":$(js "$st"),\"measured\":$(js "$me"),\"reason\":$(js "$re"),\"fix\":$(js "$fx")}"
  HTEXT="${HTEXT}$(printf '  %-5s %-3s %-32s : %s' "$tag" "$id" "$name" "$me")
"
  if [ "$st" != ok ]; then
    [ -n "$re" ] && HTEXT="${HTEXT}          why : $re
"
    [ -n "$fx" ] && HTEXT="${HTEXT}          fix : $fx
"
  elif [ -n "$re" ]; then
    HTEXT="${HTEXT}          note: $re
"
  fi
}

# adv <id> <name> <measured> <fix> -- one line of advice: printed apart, carried in the document's `advice`
# (the application lists it with the self-test's), and NEVER counted: advice has no state.
adv(){
  ADV_JSON="${ADV_JSON:+$ADV_JSON,}{\"id\":$(js "$1"),\"name\":$(js "$2"),\"measured\":$(js "$3"),\"fix\":$(js "$4")}"
  ADV_TEXT="${ADV_TEXT}$(printf '  [advice] %-3s %-28s : %s' "$1" "$2" "$3")
          do  : $4
"
}

# _judge_role_word <what `miner_selftest.py --judge-role` printed> -> active | mute | off | unknown. The reader of
# deploy/testnet-miner/publish-capacity.sh, the same text (dendra_mineur_sante_test.sh confronts the two): EXACTLY
# ONE `DENDRA_JUDGE_ROLE <word>` line and one of the three words a container can establish, else unknown -- never
# off. One trailing carriage return is the only byte removed.
_judge_role_word(){
  printf '%s\n' "${1:-}" | awk 'BEGIN { cr = sprintf("%c", 13) }
    index($0, "DENDRA_JUDGE_ROLE ") == 1 { n++; w = substr($0, 19) }
    END { if (n != 1) { print "unknown"; exit }
          if (substr(w, length(w)) == cr) w = substr(w, 1, length(w) - 1)
          print ((w == "active" || w == "mute" || w == "off") ? w : "unknown") }'
}

# _end_field <name> <END line> -> the number, or NOTHING when the field is absent or not a number. An
# absent count is never a 0: the run it would describe did not report it.
_end_field(){ printf '%s\n' "$2" | tr ' ' '\n' | sed -n "s/^$1=\([0-9][0-9]*\)$/\1/p" | head -1; }

# envval <KEY> -> the value of KEY in the slot's env -- the kit's .env for slot 0 -- (CR stripped: a .env
# edited on Windows still reads).
envval(){ local f="${SENV:-$KIT/.env}"; [ -r "$f" ] || return 0; tr -d '\r' < "$f" | sed -n "s/^$1=//p" | head -1 | sed 's/^"\(.*\)"$/\1/'; }

# ---------------------------------------------------------------- the verdict and its three outputs
CRON_ALERT_BANNER="Dendra miner health -- A PROBLEM IS STANDING"
CRON_NOTE_BANNER="Dendra miner health -- NOT EVERYTHING COULD BE MEASURED"
REPORT_DONE=0
_first_seen(){ # _first_seen <banner> -> the "first seen" of FILE when FILE carries that banner, else now
  local f=""
  if [ -f "$CRON_FILE" ] && [ "$(sed -n '1p' "$CRON_FILE" 2>/dev/null)" = "$1" ]; then
    f="$(sed -n '/^  first seen at : /{s/^  first seen at : \([^ ][^ ]*\).*$/\1/p;q;}' "$CRON_FILE" 2>/dev/null)"
  fi
  printf '%s' "${f:-$NOW_ISO}"
}
_cron_write(){ # _cron_write <banner> <body>
  local first; first="$(_first_seen "$1")"
  if ( { printf '%s\n' "$1"
         printf '  first seen at : %s   (kept from the run that first saw it)\n' "$first"
         printf '  last checked  : %s\n\n' "$NOW_ISO"
         printf '%s\n' "$2"; } > "$CRON_FILE" ) 2>/dev/null; then return 0; fi
  printf '%s\n' "[miner-health] could NOT write $CRON_FILE: this watch is silent by accident, not by health. Check the path and its directory." >&2
  return 1
}
_write_last(){ # _write_last <document> [file, by default LAST_JSON]
  local f="${2:-$LAST_JSON}" d t; d="$(dirname -- "$f")"; t="$d/.miner-health.last.json.$$.tmp"
  if ( printf '%s\n' "$1" > "$t" ) 2>/dev/null && mv -f -- "$t" "$f" 2>/dev/null; then return 0; fi
  rm -f -- "$t" 2>/dev/null
  printf '%s\n' "[miner-health] could NOT write $f: the application will show the previous run, or none." >&2
  return 1
}
_cron_alert(){ # _cron_alert <rc> <body> -- the alert file of --cron: it exists if and only if something is wrong
  case "$1" in
    0)
      if [ -e "$CRON_FILE" ]; then
        rm -f -- "$CRON_FILE" 2>/dev/null
        if [ -e "$CRON_FILE" ]; then
          printf '%s\n' "[miner-health] every check passed but $CRON_FILE could NOT be removed: remove it by hand." >&2
          ( printf 'Dendra miner health -- THIS ALERT IS STALE\n\n  The run at %s measured every check ok and could not delete this file.\n  Remove it by hand; the watch recreates it if a problem returns.\n' "$NOW_ISO" > "$CRON_FILE" ) 2>/dev/null
        fi
      fi;;
    1) _cron_write "$CRON_ALERT_BANNER" "$2";;
    *)
      # ⛔ A RUN THAT MEASURED LESS NEVER ERASES ONE THAT MEASURED A PROBLEM. A standing alert stays as
      # it is; this run's note is written only where there is no alert -- its own earlier note is
      # refreshed (first seen kept), and a file this watch did not write is left alone.
      if [ -e "$CRON_FILE" ] && [ "$(sed -n '1p' "$CRON_FILE" 2>/dev/null)" = "$CRON_ALERT_BANNER" ]; then
        printf '%s\n' "[miner-health] not everything could be measured; the standing alert $CRON_FILE knows more and is left as it is." >&2
      elif [ -e "$CRON_FILE" ] && [ "$(sed -n '1p' "$CRON_FILE" 2>/dev/null)" != "$CRON_NOTE_BANNER" ]; then
        printf '%s\n' "[miner-health] $CRON_FILE is not a file this watch wrote: left as it is." >&2
      else
        _cron_write "$CRON_NOTE_BANNER" "$2"
      fi;;
  esac
}
finish(){
  local rc verdict body doc st sel jr
  if [ "$NCHK" -eq 0 ]; then rc=2
  elif [ "$NKO" -gt 0 ]; then rc=1
  elif [ "$NUN" -gt 0 ]; then rc=2
  else rc=0; fi
  case "$rc" in
    0) verdict="OK";;
    1) verdict="A PROBLEM IS STANDING";;
    *) verdict="NOT EVERYTHING COULD BE MEASURED";;
  esac
  [ "$NCHK" -eq 0 ] && verdict="NOTHING WAS MEASURED"
  body="== Dendra miner health ($KIT${SLOT_LABEL:-}) ==
${NOTE:+  note: $NOTE
}${HTEXT}${ST_TEXT:+$ST_TEXT
}${ADV_TEXT:+  advice (a choice the owner of this machine can change; it does not change the exit code):
${ADV_TEXT:-}}  verdict: $verdict (exit $rc): $NOK ok, $NKO ko, $NUN unmeasured, of $NCHK check(s)"
  st="${ST_RC:-null}"; sel="${ST_JSON:-null}"
  # judge_role: the word read in the container and why -- null when it was not asked (no running miner, or a
  # run that stopped before). It is a reading, carried for the application's Role line; never a check.
  jr="null"; [ -n "${JR_WORD:-}" ] && jr="{\"word\":$(js "$JR_WORD"),\"why\":$(js "${JR_WHY:-}")}"
  doc="{\"schema\":1,\"tool\":\"miner_health\",\"generated_at\":$(js "$NOW_ISO"),\"generated_epoch\":${NOW_EPOCH:-0},\"schedule_period_s\":$SCHEDULE_PERIOD_S,\"rc\":$rc,\"verdict\":$(js "$verdict"),\"kit\":$(js "$KIT"),\"slot\":${SLOT:-0},\"project\":$(js "${SLOT_PROJECT:-}"),\"quick\":$([ "$QUICK" = 1 ] && echo true || echo false),\"no_write\":$([ "$WRITE" = 1 ] && echo false || echo true),\"note\":$(js "$NOTE"),\"summary\":{\"ok\":$NOK,\"ko\":$NKO,\"unmeasured\":$NUN,\"checks\":$NCHK},\"host\":[${HJSON}],\"selftest_rc\":$st,\"selftest\":$sel,\"judge_role\":$jr,\"advice\":[${ADV_JSON:-}]}"
  REPORT_DONE=1
  case "$MODE" in
    json) printf '%s\n' "$doc";;
    report) printf '%s\n' "$body";;
    cron)
      _write_last "$doc"
      _cron_alert "$rc" "$body";;
    part)
      # One slot's part of a machine run: its document, its report and its counts, for the run that started it.
      { printf '%s\n' "$doc" > "$PART/doc.json" && printf '%s\n' "$body" > "$PART/body.txt" \
          && printf '%s %s %s %s\n' "$NOK" "$NKO" "$NUN" "$NCHK" > "$PART/counts"; } 2>/dev/null \
        || printf '%s\n' "[miner-health] could NOT write the part of slot ${SLOT:-?} in $PART" >&2;;
  esac
  exit "$rc"
}
_on_exit(){
  local rc=$?
  if [ "$REPORT_DONE" = 0 ]; then
    printf '%s\n' "[miner-health] ABORTED before its verdict (shell status $rc): nothing above is a verdict." >&2
    if [ "$MODE" = cron ] && ! { [ -e "$CRON_FILE" ] && [ "$(sed -n '1p' "$CRON_FILE" 2>/dev/null)" = "$CRON_ALERT_BANNER" ]; }; then
      _cron_write "$CRON_NOTE_BANNER" "  This watch stopped before its verdict (shell status $rc): nothing was concluded." >/dev/null 2>&1
    fi
    exit 2
  fi
}
trap _on_exit EXIT

# ---------------------------------------------------------------- 0. the kit
if [ ! -f "$KIT/.env" ]; then
  rec H1 "containers" unmeasured "no .env in $KIT: no miner was set up from this kit" "" "bash $REPO/deploy/join.sh"
  finish
fi

# ---------------------------------------------------------------- 0b. which identity: the slot
# slots.sh is the one place that knows a slot: its project, its env, its containers (by their labels).
if ! { [ -r "$KIT/slots.sh" ] && . "$KIT/slots.sh" && declare -F slot_cid >/dev/null 2>&1; }; then
  rec H1 "containers" unmeasured "the slot library ($KIT/slots.sh) cannot be loaded: which containers are this kit's is not known" "" \
      "update the kit: $UPDATE"
  finish
fi

# run_machine <active slots> <count> -> one run of this file per slot (--slot k --part DIR), then ONE verdict
# for the machine: the worst slot's. Each slot's result is its own document and report; a slot whose run left
# no result is unmeasured, never ok.
run_machine(){
  local ks="$1" n="$2" k d rc per worst=0 c1 c2 c3 c4 mok=0 mko=0 mun=0 mch=0 docs="" bodies="" sv="" doc body verdict mdoc
  per=$(( SELFTEST_DEADLINE_S / n )); [ "$per" -ge "$SELFTEST_SLOT_FLOOR_S" ] || per="$SELFTEST_SLOT_FLOOR_S"
  MW="$(mktemp -d 2>/dev/null)" || { rec H1 "containers" unmeasured "no temporary directory for the run of each slot"; finish; }
  for k in $ks; do
    d="$MW/$k"; mkdir -p "$d" || { rec H1 "containers" unmeasured "no temporary directory for the run of slot $k"; finish; }
    set -- --slot "$k" --part "$d" --deadline "$per"
    [ "$QUICK" = 1 ] && set -- "$@" --quick
    [ "$WRITE" = 1 ] && set -- "$@" --write
    [ "$MODE" = cron ] && set -- "$@" --scheduled
    [ "$k" != 0 ] && set -- "$@" --no-machine-checks
    bash "$SELF" "$@" >/dev/null 2>"$d/err"; rc=$?
    case "$rc" in 0|1|2) : ;; *) rc=2 ;; esac
    c1=""; c2=""; c3=""; c4=""
    [ -s "$d/counts" ] && read -r c1 c2 c3 c4 < "$d/counts"
    # Four counts, each a number: an absent count is not a 0 (the run it would describe did not report it).
    case "$c1:$c2:$c3:$c4" in *[!0-9:]*|:*|*::*|*:) c4="" ;; esac
    if [ -s "$d/doc.json" ] && [ -s "$d/body.txt" ] && [ -n "$c4" ]; then
      mok=$((mok + c1)); mko=$((mko + c2)); mun=$((mun + c3)); mch=$((mch + c4))
      doc="$(cat "$d/doc.json")"; body="$(cat "$d/body.txt")"
      if [ "$MODE" = cron ]; then
        if [ "$k" = 0 ]; then _write_last "$doc"; else _write_last "$doc" "$(slot_dir "$k")/miner-health.last.json"; fi
      fi
    else
      rc=2; mun=$((mun + 1)); mch=$((mch + 1)); doc=null
      body="  [??]  slot $k: its check left no result (exit $rc): $(tail -1 "$d/err" 2>/dev/null)"
    fi
    case "$rc:$worst" in 1:*|*:1) worst=1 ;; 2:*|*:2) worst=2 ;; *) : ;; esac
    docs="${docs:+$docs,}{\"slot\":$k,\"project\":$(js "$(slot_project "$k")"),\"rc\":$rc,\"doc\":$doc}"
    bodies="${bodies}[slot $k] $(slot_project "$k")
$body

"
    sv="${sv:+$sv, }slot $k exit $rc"
  done
  rm -rf -- "$MW"
  case "$worst" in 0) verdict="OK";; 1) verdict="A PROBLEM IS STANDING";; *) verdict="NOT EVERYTHING COULD BE MEASURED";; esac
  body="== Dendra miner health ($KIT): $n identities, one per card ==

${bodies}  machine verdict: $verdict (exit $worst): $sv"
  mdoc="{\"schema\":1,\"tool\":\"miner_health\",\"generated_at\":$(js "$NOW_ISO"),\"generated_epoch\":${NOW_EPOCH:-0},\"schedule_period_s\":$SCHEDULE_PERIOD_S,\"rc\":$worst,\"verdict\":$(js "$verdict"),\"kit\":$(js "$KIT"),\"quick\":$([ "$QUICK" = 1 ] && echo true || echo false),\"no_write\":$([ "$WRITE" = 1 ] && echo false || echo true),\"note\":$(js "$n identities, one per card: one document per slot"),\"summary\":{\"ok\":$mok,\"ko\":$mko,\"unmeasured\":$mun,\"checks\":$mch},\"host\":[],\"selftest_rc\":null,\"selftest\":null,\"slots\":[${docs}]}"
  REPORT_DONE=1
  case "$MODE" in
    json) printf '%s\n' "$mdoc";;
    report) printf '%s\n' "$body";;
    cron) _cron_alert "$worst" "$body";;
  esac
  exit "$worst"
}

if [ -z "$SLOT_ARG" ]; then
  if ! ACTIVE="$(slot_ids --active)"; then
    rec H1 "containers" unmeasured "the slots of this kit ($KIT/gpu) cannot be read: which identities run here is not known"
    finish
  fi
  NACT="$(printf '%s\n' "$ACTIVE" | awk 'NF{n++} END{print n+0}')"
  [ "$NACT" -ge 2 ] && run_machine "$ACTIVE" "$NACT"
  SLOT=0
else
  SLOT="$SLOT_ARG"
  case "$(slot_state "$SLOT")" in
    active|retired) : ;;
    missing) rec H1 "containers" unmeasured "this kit has no slot $SLOT" "" "bash $KIT/slots.sh list"; finish ;;
    *) rec H1 "containers" unmeasured "the env of slot $SLOT ($(slot_env "$SLOT")) cannot be read"; finish ;;
  esac
fi
SENV="$(slot_env "$SLOT")"; SLOT_PROJECT="$(slot_project "$SLOT")"
if [ "$SLOT" != 0 ]; then
  SLOT_LABEL=", slot $SLOT: $SLOT_PROJECT"
  EXITED="$(slot_dir "$SLOT")/miner-health.EXITED"
  LAST_JSON="$(slot_dir "$SLOT")/miner-health.last.json"
  if [ "$(slot_state "$SLOT")" = retired ]; then
    rec H0 "retired" ok "slot $SLOT was retired by deploy/join.sh (its card left --gpus): it is stopped on purpose, nothing is checked" \
        "it stays REGISTERED: drawn as a juror while it is fresh, it never votes" "to leave the network with it: bash $KIT/exit-miner.sh --slot $SLOT"
    finish
  fi
fi
MINER_ID_ENV="$(envval MINER_ID)"
CONFIG_URL_ENV="$(envval CONFIG_URL)"
CAP_URL="$(envval DENDRA_CAPACITY_URL)"
JUDGE_ENV="$(envval DENDRA_MINER_JUDGE)"
IMG_ENV="$(envval DENDRA_MINER_IMAGE)"

# ---------------------------------------------------------------- 1. Docker, and the kit's containers
if ! command -v docker >/dev/null 2>&1; then
  rec H1 "containers" unmeasured "docker is not installed on this host" "nothing of the kit can be read" "bash $REPO/deploy/install.sh"
  finish
fi
TO=""; command -v timeout >/dev/null 2>&1 && TO="timeout"
if ! ${TO:+$TO 30} docker info >/dev/null 2>&1; then
  rec H1 "containers" unmeasured "the Docker daemon does not answer this account" "an account just added to the docker group needs a new session" \
      "log out and back in, or start the Docker daemon"
  finish
fi
# The slot's containers are found by the labels compose writes (slots.sh::slot_cid): the PROJECT names the
# identity -- every slot shares this kit's directory, so a directory alone would answer with the first miner
# of any slot -- and the working directory ties it to THIS kit (two clones share the miner's project name).
# Every profile's containers carry both.
if ! MINER_CID="$(slot_cid "$SLOT" miner 2>/dev/null)"; then
  rec H1 "containers" unmeasured "docker ps failed: the kit's containers cannot be listed"
  finish
fi
cid_of(){ slot_cid "$SLOT" "$1" 2>/dev/null; }
MINER_STATE=""
[ -n "$MINER_CID" ] && MINER_STATE="$(docker inspect -f '{{.State.Status}}' "$MINER_CID" 2>/dev/null | tr -d '\r')"

# A MINER THAT LEFT THE NETWORK ON PURPOSE IS NOT A PROBLEM. exit-miner.sh writes this marker once the
# chain confirms the deregistration, and stops the miner: an hourly alert about a stopped, unregistered
# miner would then sound forever about a decision. The marker counts only while the miner stays stopped:
# a miner started again registers again, so the checks resume and the marker goes.
if [ -f "$EXITED" ]; then
  if [ "$MINER_STATE" != running ]; then
    _when="$(sed -n 's/^  at[[:space:]]*: //p' "$EXITED" 2>/dev/null | head -1)"
    rec H0 "left the network" ok "this miner left the network${_when:+ at $_when} (exit-miner.sh${SLOT_LABEL:+ --slot $SLOT}): nothing is checked" \
        "starting the miner again registers it again, and these checks resume" "to rejoin: $REJOIN_EXACT"
    finish
  fi
  rm -f -- "$EXITED" 2>/dev/null && NOTE="the miner runs again: the exit recorded by exit-miner.sh no longer applies, its marker was removed"
fi

# ---------------------------------------------------------------- H1 containers
# THE JUDGE'S ENGINE (ollama-cpu) IS EXPECTED ONLY IN THE SLOT THAT HOSTS IT: the slot whose env starts the
# judge profile (COMPOSE_PROFILES=judge), never inferred from the judge ROLE. Voting (DENDRA_MINER_JUDGE) and
# hosting the engine are two facts: on a machine with one identity per card every slot votes, through slot
# 0's engine, and only slot 0 hosts it. One exception, slot 0 of an OLDER kit, whose .env has no
# COMPOSE_PROFILES line at all: its judge was started with --profile judge, so there the role says it.
PROFILE_LINE=0; grep -q '^COMPOSE_PROFILES=' "$SENV" 2>/dev/null && PROFILE_LINE=1
HOSTS_JUDGE=0; OLD_PROFILE=0
case ",$(envval COMPOSE_PROFILES | tr -d ' ')," in *,judge,*) HOSTS_JUDGE=1;; esac
if [ "$SLOT" = 0 ] && [ "$PROFILE_LINE" = 0 ] && [ "$JUDGE_ENV" = 1 ]; then HOSTS_JUDGE=1; OLD_PROFILE=1; fi
SVCS="miner ollama"; [ "$HOSTS_JUDGE" = 1 ] && SVCS="$SVCS ollama-cpu"
H1S=ok; H1M=""; H1R=""
for s in $SVCS; do
  id="$(cid_of "$s")"
  if [ -z "$id" ]; then
    H1S="$(worse "$H1S" ko)"; H1M="${H1M:+$H1M; }$s: no container"
    continue
  fi
  line="$(docker inspect -f '{{.State.Status}}|{{.RestartCount}}' "$id" 2>/dev/null | tr -d '\r')"
  sst="${line%%|*}"; rcount="${line#*|}"
  case "$rcount" in ''|*[!0-9]*) rcount="?";; esac
  case "$sst" in
    running) H1M="${H1M:+$H1M; }$s: running (restarted $rcount time(s))";;
    "") H1S="$(worse "$H1S" unmeasured)"; H1M="${H1M:+$H1M; }$s: not read";;
    *) H1S="$(worse "$H1S" ko)"; H1M="${H1M:+$H1M; }$s: $sst (restarted $rcount time(s))"
       [ "$s" = miner ] && H1R="a stopped miner serves no job and proves no presence";;
  esac
done
# The restart names the judge's profile when this kit runs a judge: a compose up without it starts no
# profiled service, so ollama-cpu -- the instance that serves the verdicts -- would stay stopped. A kit
# written by a current join.sh carries COMPOSE_PROFILES=judge in its .env; an older one does not.
H1_UP="docker compose up -d"; [ "$OLD_PROFILE" = 1 ] && H1_UP="docker compose --profile judge up -d"
if [ "$SLOT" = 0 ]; then
  rec H1 "containers" "$H1S" "$H1M" "$H1R" "$([ "$H1S" = ok ] || printf 'cd %s && %s   (from that directory, without -f); logs: docker compose -p dendra-miner logs --tail 80 miner' "$KIT" "$H1_UP")"
else
  rec H1 "containers" "$H1S" "$H1M" "$H1R" "$([ "$H1S" = ok ] || printf '%s; logs: %s' "$(slot_run_hint "$SLOT" up -d --no-build)" "$(slot_run_hint "$SLOT" logs --tail 80 miner)")"
fi

# ---------------------------------------------------------------- H2 image
# The pin's FORM is read the way join.sh::image_pin reads it, against the same expression
# (join.sh::MINER_IMAGE_RE; dendra_mineur_sante_test.sh confronts the two). Its `# built-for:` gates are not
# judged here (join.sh::image_built_for does it): a pin join.sh refused for them reads `pinned` below, with
# the kit running the image built from the clone, and the message names that case.
MINER_IMAGE_RE='^ghcr\.io/dendranetwork/dendra-miner@sha256:[0-9a-f]{64}$'
PIN=""; PIN_STATE=none
if [ -r "$REPO/docker/MINER_IMAGE" ]; then
  _pl="$(tr -d '\r' < "$REPO/docker/MINER_IMAGE" | grep -vE '^[[:space:]]*(#|$)' || true)"
  _pn="$(printf '%s\n' "$_pl" | grep -c . || true)"
  case "$_pn" in
    0) PIN_STATE=none;;
    1) case "$_pl" in
         DENDRA_MINER_IMAGE=*) PIN="${_pl#DENDRA_MINER_IMAGE=}"
           if [ -z "$PIN" ]; then PIN_STATE=none
           elif printf '%s\n' "$PIN" | LC_ALL=C grep -Eq "$MINER_IMAGE_RE"; then PIN_STATE=pinned
           else PIN_STATE=refused; PIN=""; fi;;
         *) PIN_STATE=refused;;
       esac;;
    ''|*[!0-9]*) PIN_STATE=unread;;
    *) PIN_STATE=refused;;
  esac
fi
if [ -z "$MINER_CID" ]; then
  rec H2 "image" unmeasured "no miner container (see H1)"
else
  WANT="${IMG_ENV:-dendra/miner:latest}"
  _ci="$(docker inspect -f '{{.Config.Image}}|{{.Image}}' "$MINER_CID" 2>/dev/null | tr -d '\r')"
  C_IMG="${_ci%%|*}"; C_ID="${_ci#*|}"
  W_ID="$(docker image inspect -f '{{.Id}}' "$WANT" 2>/dev/null | tr -d '\r')"
  REJOIN="$REJOIN_EXACT   (it starts the image this clone pins, or builds it from the clone)"
  H2_UP="cd $KIT && docker compose up -d"; [ "$SLOT" = 0 ] || H2_UP="$(slot_run_hint "$SLOT" up -d --no-build)"
  if [ -z "$_ci" ]; then
    rec H2 "image" unmeasured "the miner container's image was not read"
  elif [ "$C_IMG" != "$WANT" ]; then
    rec H2 "image" ko "the miner container was started from $C_IMG, the kit names $WANT" \
        "a container started by hand, or before the kit's .env changed" "$H2_UP"
  elif [ -z "$W_ID" ]; then
    rec H2 "image" unmeasured "the image the kit names ($WANT) is not on this host any more" "the container runs an image whose tag was removed"
  elif [ "$W_ID" != "$C_ID" ]; then
    rec H2 "image" ko "the container runs an OLDER build of $WANT than the one on this host" \
        "the image was rebuilt or pulled after the container started" "$H2_UP"
  else
    case "$PIN_STATE:${IMG_ENV:+set}" in
      pinned:set)
        if [ "$IMG_ENV" = "$PIN" ]; then rec H2 "image" ok "the image this clone pins ($PIN)"
        else rec H2 "image" ko "this clone pins $PIN, the kit still runs $IMG_ENV" "the clone was updated without re-running join.sh" "$REJOIN"; fi;;
      pinned:)  rec H2 "image" ok "built from this clone" "this clone pins $PIN; join.sh builds instead when the pin's '# built-for:' gates are not this clone's, when the pinned image's platform is not this engine's, or when the pull fails";;
      none:set|refused:set|unread:set)
        rec H2 "image" ko "this clone pins no image (a kit version bump empties docker/MINER_IMAGE), and the kit still runs $IMG_ENV" \
            "an image pinned for an older kit runs the older code" "$REJOIN";;
      *) rec H2 "image" ok "built from this clone (it pins no image)";;
    esac
  fi
fi

# ---------------------------------------------------------------- H3, H4 the gates: this clone, the image, the network
NETINFO=""; NET_WHY=""
if [ -z "$CONFIG_URL_ENV" ]; then
  NET_WHY="no CONFIG_URL in $KIT/.env (deploy/join.sh writes it; a kit from an older join.sh has none)"
elif ! command -v curl >/dev/null 2>&1; then
  NET_WHY="curl is not installed: network-info cannot be fetched"
elif ! NETINFO="$(curl -fsS -m 15 "$CONFIG_URL_ENV" 2>/dev/null)"; then
  NETINFO=""; NET_WHY="network-info could not be fetched from $CONFIG_URL_ENV"
fi
# network-info is KEY=VALUE text, read by key -- the same file join.sh::_load_config_url parses.
netval(){ printf '%s\n' "$NETINFO" | tr -d '\r' | sed -n "s/^$1=//p" | head -1; }
THEIR_KIT="$(netval KIT_VERSION | tr -dc 0-9)"
THEIR_EPOCH="$(netval CONSENSUS_EPOCH | tr -dc 0-9)"
REF_RPC="$(netval DENDRA_NODE)"
case "$REF_RPC" in *[!A-Za-z0-9:/._@,-]*) REF_RPC="";; esac
MINE_KIT="$(head -1 "$REPO/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)"
MINE_EPOCH="$(head -1 "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
IMG_KIT=""; IMG_EPOCH=""
if [ "$MINER_STATE" = running ]; then
  IMG_KIT="$(docker exec "$MINER_CID" cat /app/built-for/KIT_VERSION 2>/dev/null | head -1 | tr -dc 0-9)"
  IMG_EPOCH="$(docker exec "$MINER_CID" cat /app/built-for/CONSENSUS_EPOCH 2>/dev/null | head -1 | tr -dc 0-9)"
fi
gate(){ # gate <id> <name> <file> <mine> <theirs> <image> <behind-is: older|different>
  local id="$1" nm="$2" f="$3" mine="$4" theirs="$5" img="$6" kind="$7"
  if [ -z "$mine" ]; then rec "$id" "$nm" unmeasured "docker/$f is unreadable in this clone"; return; fi
  if [ -z "$theirs" ]; then
    if [ -z "$NETINFO" ]; then rec "$id" "$nm" unmeasured "this clone is at $mine; the network's value was not read" "$NET_WHY" "re-run deploy/join.sh with CONFIG_URL"
    else rec "$id" "$nm" unmeasured "this clone is at $mine; network-info declares no $f" "nothing to compare with: ask the operator to publish $f="; fi
    return
  fi
  if [ "$kind" = older ] && [ "$mine" -lt "$theirs" ] 2>/dev/null; then
    rec "$id" "$nm" ko "KIT BEHIND: this clone is at $mine, the network publishes $theirs" \
        "the fixes you are missing are in the scripts and services YOU run: a defect there does its job badly rather than failing" "$UPDATE"; return
  fi
  if [ "$kind" = different ] && [ "$mine" != "$theirs" ]; then
    rec "$id" "$nm" ko "MISMATCH: this clone is at $mine, the network runs $theirs" \
        "a node built from this clone takes other state transitions than the network's" "$UPDATE"; return
  fi
  if [ "$MINER_STATE" != running ]; then rec "$id" "$nm" unmeasured "this clone is at $mine (network $theirs); the image's was not read: the miner is not running"; return; fi
  if [ -z "$img" ]; then
    rec "$id" "$nm" unmeasured "this clone is at $mine (network $theirs); the running image declares none (/app/built-for)" \
        "the image was built before this kit; what it runs cannot be compared" "$REJOIN_EXACT"; return
  fi
  if [ "$img" != "$mine" ]; then
    rec "$id" "$nm" ko "the running image was built for $img, this clone is at $mine (network $theirs)" \
        "the code the miner runs is not the code of this clone" "$REJOIN_EXACT"; return
  fi
  rec "$id" "$nm" ok "$mine: this clone and the running image match the network ($theirs)"
}
# The clone's gates and the schedule are the MACHINE's: in a run of every slot they are checked once, in
# slot 0's part (--no-machine-checks on the others).
if [ "$NO_MACHINE" != 1 ]; then
gate H3 "kit version" KIT_VERSION "$MINE_KIT" "$THEIR_KIT" "$IMG_KIT" older
gate H4 "consensus epoch" CONSENSUS_EPOCH "$MINE_EPOCH" "$THEIR_EPOCH" "$IMG_EPOCH" different
fi

# ---------------------------------------------------------------- H5 the schedule
CRON_LINE_HINT="bash -lc 'bash $SELF --cron $DEFAULT_ALERT'"
if [ "$NO_MACHINE" = 1 ]; then
  :
elif ! command -v crontab >/dev/null 2>&1; then
  rec H5 "schedule" unmeasured "no crontab on this host: nothing runs this check on a schedule" \
      "the container's healthcheck and the application still show the daemon's heartbeat" "run it hourly with this host's scheduler: $CRON_LINE_HINT"
else
  _ce="$(mktemp 2>/dev/null || printf '%s' "/tmp/miner-health.$$.err")"
  TAB="$(crontab -l 2>"$_ce")"; _crc=$?
  if [ "$_crc" -ne 0 ]; then
    if grep -qi 'no crontab' "$_ce" 2>/dev/null; then TAB=""; else TAB="__UNREAD__"; fi
  fi
  rm -f -- "$_ce"
  if [ "$TAB" = "__UNREAD__" ]; then
    rec H5 "schedule" unmeasured "the crontab could not be read"
  else
    NCRON="$(printf '%s\n' "$TAB" | grep -cF -- "$SELF --cron" || true)"
    case "$NCRON" in
      0) rec H5 "schedule" ko "this check is NOT scheduled (no crontab line runs $SELF --cron)" \
             "nothing will notice a problem until someone looks" "$REJOIN_EXACT (it schedules it), or add this line with crontab -e (piping crontab -l into crontab - replaces the whole table when crontab -l fails): 29 * * * * $CRON_LINE_HINT";;
      ''|*[!0-9]*) rec H5 "schedule" unmeasured "the crontab's lines for this check could not be counted";;
      *)
        if [ "$MODE" = cron ] || [ "$SCHEDULED" = 1 ]; then
          rec H5 "schedule" ok "scheduled (this run is the scheduled one)"
        else
          _cf="$(printf '%s\n' "$TAB" | grep -F -- "$SELF --cron" | head -1 | sed -n "s/.*--cron \([^ ']*\).*/\1/p")"
          _lj="$(dirname -- "${_cf:-$DEFAULT_ALERT}")/miner-health.last.json"
          _m="$(stat -c %Y -- "$_lj" 2>/dev/null || date -r "$_lj" +%s 2>/dev/null || true)"
          case "$_m" in
            ''|*[!0-9]*)
              if [ -e "$_lj" ]; then rec H5 "schedule" unmeasured "scheduled; the age of $_lj could not be read"
              else rec H5 "schedule" unmeasured "scheduled; no scheduled run is recorded yet ($_lj)" \
                       "the first runs at the minute the crontab line names; if this stays so for hours, cron is not running" \
                       "check that cron runs (under WSL: sudo service cron start)"; fi;;
            *)
              _age=$(( NOW_EPOCH - _m ))
              if [ "$_age" -gt $(( 2 * SCHEDULE_PERIOD_S )) ]; then
                rec H5 "schedule" ko "scheduled, but its last run was $(( _age / 60 )) min ago" \
                    "the schedule is hourly: a run missing for two periods is a cron that does not fire" "check that cron runs (under WSL: sudo service cron start)"
              else
                rec H5 "schedule" ok "scheduled; last run $(( _age / 60 )) min ago"
              fi;;
          esac
        fi;;
    esac
  fi
fi

# ---------------------------------------------------------------- H6 the card (one identity per card only)
# A slot bound to a card (DENDRA_GPU_UUID, written by join.sh --gpus) must compute on THAT card. An unknown
# or empty CUDA_VISIBLE_DEVICES does not fail: the engine falls back to the CPU in silence. slots.sh::slot_pin
# reads the engine's own startup line since its last start: pinned, not-pinned, or unknown -- never pinned
# by default. A kit with one identity binds no card, and this check does not run.
_uuid="$(envval DENDRA_GPU_UUID)"
if [ -n "$_uuid" ]; then
  _pin="$(slot_pin "$SLOT" 2>/dev/null)"; _pv="${_pin%%|*}"; _pw="${_pin#*|}"
  # THE REMEDY DEPENDS ON WHETHER THE CARD IS STILL HERE. A re-run of join.sh rewrites the override of a card
  # it lists; a card it does not list is never re-bound in silence (the slot is stopped), so re-running would
  # change nothing -- that remedy names the binding instead. An unread list keeps the re-run remedy.
  _h6fix="$REJOIN_EXACT   (it reads the cards again and rewrites each slot's override); then: bash $KIT/slots.sh pin $SLOT"
  if [ "$_pv" = not-pinned ] && _h6l="$(bash "$REPO/deploy/hw_probe.sh" --list-gpus 2>/dev/null)"; then
    [ "$(printf '%s\n' "$_h6l" | awk -F'|' -v u="$_uuid" '$2 == u { f = 1 } END { print f + 0 }')" = 1 ] \
      || _h6fix="its card $_uuid is not on this host now: bind this identity to a present card: bash $REPO/deploy/join.sh --reassign $SLOT=<card index>   (bash $REPO/deploy/hw_probe.sh --list-gpus lists the cards)"
  fi
  case "$_pv" in
    pinned) rec H6 "GPU pinning" ok "$_pw (card $_uuid)";;
    not-pinned) rec H6 "GPU pinning" ko "$_pw (card $_uuid)" \
        "this identity does not compute on its own card: on the CPU or on another slot's card, its jobs run slow or time out" \
        "$_h6fix";;
    *) rec H6 "GPU pinning" unmeasured "${_pw:-the pinning was not read} (card $_uuid)";;
  esac
fi

# ---------------------------------------------------------------- C1-C10, inside the miner container
selftest_unmeasured(){ rec C0 "self-test (C1-C10)" unmeasured "$1" "${2:-}" "${3:-}"; }
if [ "$MINER_STATE" != running ]; then
  selftest_unmeasured "not run: the miner container is not running (see H1)"
else
  # Three answers from one exec: 0 the self-test is in the image, 3 it is not, anything else the exec
  # itself failed. Only a measured absence is "an image built before it".
  docker exec "$MINER_CID" sh -c 'test -f /app/miner_selftest.py || exit 3' >/dev/null 2>&1; _p=$?
  if [ "$_p" = 3 ]; then
    selftest_unmeasured "the running image carries no self-test: it was built before it" \
        "an older pinned image, or a build from an older clone" "$REJOIN_EXACT"
  elif [ "$_p" != 0 ]; then
    selftest_unmeasured "docker exec into the miner container failed (exit $_p)"
  else
    _so="$(mktemp 2>/dev/null || printf '%s' "/tmp/miner-health.$$.out")"
    set -- --host
    [ "$QUICK" = 1 ] && set -- "$@" --quick
    [ "$WRITE" = 1 ] && set -- "$@" --write
    [ -n "$REF_RPC" ] && set -- "$@" --reference-rpc "$REF_RPC"
    [ -n "$CAP_URL" ] && set -- "$@" --capacity-url "$CAP_URL"
    [ -n "$MINER_ID_ENV" ] && set -- "$@" --node-id "$MINER_ID_ENV"
    # A slot k's fixes name ITS command (slots.sh run k), never slot 0's project.
    [ "$SLOT" != 0 ] && set -- "$@" --compose "bash $KIT/slots.sh run $SLOT"
    set -- "$@" --deadline-s "$SELFTEST_DEADLINE_S"
    ${TO:+$TO $SELFTEST_TIMEOUT_S} docker exec "$MINER_CID" python3 /app/miner_selftest.py "$@" > "$_so" 2>/dev/null; _xrc=$?
    _end="$(sed -n 's/^DENDRA_SELFTEST_END //p' "$_so" | tail -1)"
    e_rc="$(_end_field rc "$_end")"; e_ok="$(_end_field ok "$_end")"; e_ko="$(_end_field ko "$_end")"
    e_un="$(_end_field unmeasured "$_end")"; e_n="$(_end_field checks "$_end")"
    _json="$(sed -n 's/^DENDRA_SELFTEST_JSON //p' "$_so" | tail -1)"
    _busy="$(sed -n 's/^DENDRA_SELFTEST_BUSY //p' "$_so" | tail -1)"
    if [ -n "$_busy" ]; then
      selftest_unmeasured "not run: $_busy" "one self-test at a time runs in the container; this run did not start a second one"
    elif [ -z "$e_rc" ] || [ -z "$e_ok" ] || [ -z "$e_ko" ] || [ -z "$e_un" ] || [ -z "$e_n" ]; then
      if [ "$_xrc" = 124 ]; then selftest_unmeasured "the self-test did not finish within ${SELFTEST_TIMEOUT_S} s (its own deadline was ${SELFTEST_DEADLINE_S} s)"
      else selftest_unmeasured "the self-test did not finish (exit $_xrc, no final line)"; fi
    elif [ "$e_rc" != "$_xrc" ]; then
      selftest_unmeasured "the self-test's final line (rc $e_rc) and its exit code ($_xrc) disagree"
    elif [ "$e_n" -eq 0 ] 2>/dev/null; then
      selftest_unmeasured "the self-test ran no check"
    else
      case "$_json" in
        "{"*"}") ST_JSON="$_json";;
        *) ST_JSON="";;
      esac
      ST_RC="$e_rc"
      NOK=$((NOK + e_ok)); NKO=$((NKO + e_ko)); NUN=$((NUN + e_un)); NCHK=$((NCHK + e_n))
      ST_TEXT="$(sed -n 's/^DENDRA_SELFTEST_TEXT //p' "$_so")"
      [ -n "$ST_JSON" ] || NOTE="${NOTE:+$NOTE; }the self-test's document was not a JSON object: its counts are kept, its details are in the text report only"
    fi
    rm -f -- "$_so"
  fi
fi

# ---------------------------------------------------------------- the jury seat: ADVICE, never a check
# THE CHAIN DRAWS EVERY PRESENT MINER INTO JURIES, AND ONLY A JUDGE VOTES. An identity whose judge role is OFF
# holds jury seats all the same and never votes: a SILENT JUROR SEAT. Not judging is a choice (a machine below
# the judge's RAM floor mines and nothing else), so it is ADVICE -- it never changes the exit code and never
# raises an alert -- but it is SAID, because nothing else on this machine says it: the self-test skips its
# judge checks when the role is off. The role is READ IN THE CONTAINER, by the reader publish-capacity.sh puts in
# the signed capacity report (miner_selftest.py --judge-role): this host's .env says what was asked, not what the
# container runs. active and off are readings; mute is already a ko of the self-test (C11) and is not repeated
# here; unknown says nothing, and is never read as off.
if [ "$MINER_STATE" != running ]; then
  JR_WORD="unknown"; JR_WHY="not read: the miner container is not running (see H1)"
elif [ "${_p:-}" != 0 ]; then
  JR_WORD="unknown"; JR_WHY="not read: the miner container did not run its self-test (see C0)"
else
  _jr_out="$(${TO:+$TO 120} docker exec "$MINER_CID" python3 /app/miner_selftest.py --judge-role 2>/dev/null)"
  JR_WORD="$(_judge_role_word "$_jr_out")"
  JR_WHY="$(printf '%s\n' "$_jr_out" | sed -n 's/^DENDRA_JUDGE_ROLE_WHY //p' | head -1 | tr -d '[:cntrl:]' | cut -c1-240)"
  [ -n "$JR_WHY" ] || JR_WHY="the miner container gave no readable answer (an image older than --judge-role?)"
fi
if [ "$JR_WORD" = off ]; then
  adv A4 "silent juror seat" \
      "miner -- silent juror seat: the judge role is off on this identity, and the chain draws every present miner into juries all the same; each seat it holds gives no verdict, and an audit concludes only when enough of its jurors vote (antievasion.go::auditRelativeBar). Read in the container: $JR_WHY" \
      "nothing, if this machine only mines. To judge it needs the judge's RAM floor: bash $REPO/deploy/hw_probe.sh --can-judge answers true, false or unknown; on true: ${KIT_RERUN:+$KIT_RERUN --judge}${KIT_RERUN:-bash $REPO/deploy/join.sh --judge, with the other options this kit was set up with}"
fi

finish
