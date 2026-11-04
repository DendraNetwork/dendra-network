#!/usr/bin/env bash
# validator_health.sh: in one command, and with no secret, tell a validator operator whether their
# validator is jailed, whether it was slashed, how much margin is left before the next jail, and what
# the network numbers mean for them.
#
#   bash deploy/validator_health.sh                     # identifies the validator from the local node
#   bash deploy/validator_health.sh dendravaloper1...   # any validator, from any machine
#   DENDRA_REST=http://HOST:1317 bash deploy/validator_health.sh
#   bash deploy/validator_health.sh --cron /path/alert  # writes the file only when something is wrong
#
# DENDRA_REST is binding. When it is set, it is the only endpoint tried: if it does not answer, this
# script stops with exit 2 and reports nothing. It will not quietly fall back to another node and hand
# you a healthy-looking report about somebody else's validator. Leave it unset to let the script find
# an endpoint on its own; the report then names which one it picked and where it came from.
#
# Exit codes:  0 = healthy; 1 = a named answer that is not health (jailed, slashed, not bonded, node
#              not caught up, below the seed floor, seed stale, or no validator on this machine);
#              2 = unusable (chain unreachable, DENDRA_REST unreachable, a required tool missing, no
#              JSON parser, self-test failed, or a run that stopped before finishing its report).
#              A value that could not be read prints `?`, is never compared away, and never returns 0:
#              "I do not know" is not "everything is fine".
#
# Three states, never two. A field that is absent from a document this script parsed is the zero of its
# type (proto3 omits every field worth its zero). A field that is present and cannot be read as what it
# claims to be is an unknown and prints `?`. A document that could not be read at all is a third case.
# Only the first of the three may support a reassuring sentence, and every number that enters an
# arithmetic expression passes `_int` or `_dec` first: an unvalidated string in `$(( ))` under `set -u`
# does not print a wrong number, it kills the run in the middle of its own report.
#
# This script never stops without saying so. If it stops before finishing, the EXIT trap says so on the
# real stderr and, under --cron, records it, because an unattended watch that dies silently looks the
# same as one that found nothing wrong.
#
# Read-only. This script never signs and never broadcasts a transaction. Unjail is a deliberate act by
# the operator: the exact command is printed, and the operator runs it.
#
# Why this exists: the endpoint operators were given to watch cannot show the cause.
# `committee_seed_health` (the endpoint the runbooks point at) returns latest_contributors and the
# required floor, and nothing else: no jail, no slash, not even which validators are expected. A
# validator that drops out of the consensus set therefore shows up as a contributor count that falls,
# with no way to tell a jail from a vote-extension failure. This script names the cause instead of
# leaving it to be guessed.
set -u

TIMEOUT="${DENDRA_HTTP_TIMEOUT:-15}"
TARGET=""; TARGET_SRC=""; CRON_FILE=""
# How this script names itself in the commands it prints. Read from the environment because a caller
# may run it through a pipe (the CRLF-safe form the runbooks use), and `$0` is then literally "bash",
# which would make every command this script tells the operator to run uncopyable.
SELF_CMD="${DENDRA_HEALTH_CMD:-bash $0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --cron)
      CRON_FILE="${2:-}"
      # `--cron` takes a path, and only a path. Accepting whatever token follows turns `--cron --help`
      # or a pair of swapped arguments into a file quietly created in the current directory under an
      # absurd name, which then becomes the alert file of an unattended watch nobody will look at.
      case "$CRON_FILE" in
        "") printf '%s\n' "[health] --cron needs a file path" >&2; exit 2;;
        -*) printf '%s\n' "[health] --cron needs a file PATH, and \"$CRON_FILE\" starts with '-'." >&2
            printf '%s\n' "[health] That is an option, not a path. If it really is the file you mean, write it" >&2
            printf '%s\n' "[health] as ./$CRON_FILE so it can be created and removed without ambiguity." >&2
            exit 2;;
      esac
      shift 2;;
    # The help text is the header block itself, delimited by its leading `#` rather than by a line
    # number: a hard-coded range silently starts printing the wrong thing the first time a line is
    # added above it.
    #
    # A piped run has no file to read. Through `cat script | bash -s -- --help`, `$0` is literally
    # "bash": awk then opens nothing, and printing nothing with exit 0 would claim success. So that
    # case exits 2 and says why.
    -h|--help)
      _help="$(awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0" 2>/dev/null)"
      if [ -n "${_help:-}" ]; then printf '%s\n' "$_help"; exit 0; fi
      printf '%s\n' "[health] --help prints this file's own header, and this run has no file to read:" >&2
      printf '%s\n' "[health] \$0 is \"$0\", so the script was piped into a shell instead of being run from" >&2
      printf '%s\n' "[health] disk. Save it first, then ask it again:" >&2
      printf '%s\n' "[health]     curl -fsSL <url> -o validator_health.sh && bash validator_health.sh --help" >&2
      exit 2;;
    -*) printf '%s\n' "[health] unknown option: $1 (see --help)" >&2; exit 2;;
    *) TARGET="$1"; shift;;
  esac
done

# _cron_note REASON: under --cron, a run that cannot measure anything must still leave a trace.
# Every fatal exit below happens before the report exists, so without this the alert file is never touched at
# all: an operator whose chain has been unreachable for a week sees an empty directory, which is the
# agreed sign of good health.
#
# It refreshes its own note and never touches any other file. The two kinds of file carry opposite
# amounts of knowledge, and one rule cannot serve both:
#  - A standing alert (written by _alert_write, or anything else sitting at that path) comes from a run
#    that measured something. This run measured nothing, so it has nothing to add: overwriting it would
#    replace a verdict with the absence of one.
#  - A note this function wrote says only "this watch could not run". Left frozen, a watch that died on
#    Monday looks the same as one that has been failing every hour since, and how long it has been going
#    on is the number an operator needs first. So the `first seen at` of its own note is kept and its
#    `last checked` is brought forward on every tick.
# The note is recognised by the banner it carries, not by its path: the path is whatever the operator
# passed, and any other file standing there belongs to somebody else. The banner is written once, into
# a variable, so the string that is recognised and the string that is written cannot drift apart.
CRON_NOTE_BANNER="Dendra validator health — this watch could not run"
_cron_note(){
  [ -n "$CRON_FILE" ] || return 0
  local now first mine=0 f
  now="$(date -u +%FT%TZ 2>/dev/null || printf '?')"
  first="$now"
  if [ -e "$CRON_FILE" ]; then
    # `sed` may itself be the missing tool that brought us here; it then prints nothing, `mine` stays
    # 0, and the file is left alone. Not touching what cannot be identified is the only safe default
    # for the one action in this script that overwrites something.
    [ "$(sed -n '1p' "$CRON_FILE" 2>/dev/null)" = "$CRON_NOTE_BANNER" ] && mine=1
    if [ "$mine" = 0 ]; then
      printf '%s\n' "[health] $1 — the standing alert $CRON_FILE knows more than this run does and is left as it is." >&2
      return 0
    fi
    # The sed block is addressed and quits on the first match: reading every matching line would make
    # `first` two lines long the moment a file carried two of them, and the note written back would
    # carry a forged second field. Only the timestamp is taken back, never the rest of the line, which
    # would carry the explanatory suffix along while the next write appends its own.
    f="$(sed -n '/^  first seen at : /{s/^  first seen at : \([^ ][^ ]*\).*$/\1/p;q;}' "$CRON_FILE" 2>/dev/null)"
    [ -n "${f:-}" ] && first="$f"
  fi
  if ( { printf '%s\n' "$CRON_NOTE_BANNER"
         printf '  first seen at : %s\n' "$first"
         printf '  last checked  : %s\n' "$now"
         printf '\n  %s\n' "$1"
         printf '\n  Nothing was measured, so nothing is claimed. This file is not a report about your\n'
         printf '  validator: it is the absence of one. An empty directory would have read as good news.\n'
       } > "$CRON_FILE" ) 2>/dev/null; then
    if [ "$mine" = 1 ]; then
      printf '%s\n' "[health] refreshed $CRON_FILE: this watch still cannot run (first seen $first)." >&2
    else
      printf '%s\n' "[health] wrote $CRON_FILE: this watch could not run." >&2
    fi
  else
    printf '%s\n' "[health] could not write $CRON_FILE either: check the path and its directory." >&2
  fi
}

# ---------------------------------------------------------------- tools
# No text predicate on JSON. A `grep '"jailed":true'` answers on the shape of the text: it matches a
# different validator's field in the same document, it misses an indented variant, and it reads a
# missing field as a reassuring "false". So a real parser is required, and its absence is a refusal,
# never a degraded mode that still prints a verdict.
# The same applies to every tool that produces a number. `awk` computes the measured block interval,
# the amount and percentage of a slash, the jail threshold and every duration printed below. Without
# it, its substitutions come back empty, and an empty string reads as 0 in shell arithmetic, so the
# report would print ` s/block (measured ...)`, ` udndr lost` and a jail threshold twice its real value
# on stdout, while the only complaint goes to stderr. `sed`, `tr` and `date` carry the endpoint host,
# the consensus address and every timestamp. So their absence is a refusal too.
for _t in curl awk sed tr date; do
  command -v "$_t" >/dev/null 2>&1 || {
    printf '%s\n' "[health] FATAL: $_t is required and is not installed." >&2
    printf '%s\n' "[health] Numbers below would be computed with it; without it they would be printed" >&2
    printf '%s\n' "[health] empty and read as zero, which is the most reassuring value there is." >&2
    _cron_note "a required tool ($_t) is missing on this machine, so this run measured nothing"
    exit 2; }
done
JSON=""
# DENDRA_JSON forces the engine, so that both implementations of the JSON primitives can be run against
# the same live chain and compared: two implementations that are never compared drift apart.
case "${DENDRA_JSON:-}" in
  python3|jq) command -v "$DENDRA_JSON" >/dev/null 2>&1 && JSON="$DENDRA_JSON" \
              || { printf '%s\n' "[health] FATAL: DENDRA_JSON=$DENDRA_JSON but that program is not installed." >&2
                   _cron_note "DENDRA_JSON=$DENDRA_JSON is not installed, so this run measured nothing"; exit 2; };;
  "") : ;;
  *) printf '%s\n' "[health] FATAL: DENDRA_JSON must be python3 or jq." >&2
     _cron_note "DENDRA_JSON holds a value this script cannot use, so this run measured nothing"; exit 2;;
esac
if [ -n "$JSON" ]; then :
elif command -v python3 >/dev/null 2>&1; then JSON=python3
elif command -v jq >/dev/null 2>&1; then JSON=jq
else
  printf '%s\n' "[health] FATAL: neither python3 nor jq is installed." >&2
  printf '%s\n' "[health] This script parses JSON; it will not guess by matching text. Install one:" >&2
  printf '%s\n' "[health]     sudo apt-get install -y jq        # or: sudo apt-get install -y python3" >&2
  _cron_note "no JSON parser is installed on this machine, so this run measured nothing"
  exit 2
fi

TMP="$(mktemp -d 2>/dev/null || printf '%s' "/tmp/dendra-health.$$")"
mkdir -p "$TMP" 2>/dev/null || true

# fd 9 is the real stderr, kept open on purpose. Under --cron the report is captured with `2>&1` into a
# file inside $TMP, and $TMP is what the EXIT trap deletes, so a run that stops in the middle would lose
# its last words twice: once into the file, once with the directory. fd 9 survives both.
exec 9>&2
REPORT_STARTED=0; REPORT_DONE=0
_on_exit(){
  local rc=$?
  if [ "$REPORT_STARTED" = 1 ] && [ "$REPORT_DONE" = 0 ]; then
    printf '%s\n' "[health] Aborted: this run stopped in the middle of its report (shell status $rc)." >&9
    printf '%s\n' "[health] Nothing below or above it is a verdict about your validator: the run did not" >&9
    printf '%s\n' "[health] finish, so it measured nothing conclusive. Re-run it by hand to see where it" >&9
    printf '%s\n' "[health] stops:  $SELF_CMD ${TARGET:-}" >&9
    # `2>&9` because under --cron the report's redirection may still be in force here, and the
    # file it points at is inside the directory this trap is about to delete.
    [ -n "$CRON_FILE" ] && _cron_note "this watch stopped in the middle of its report and could not reach a verdict" 2>&9
    rm -rf "$TMP" 2>/dev/null || true
    exit 2
  fi
  rm -rf "$TMP" 2>/dev/null || true
}
trap _on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ABSENT="__DENDRA_ABSENT__"
# The field separator is deliberately not a tab (see _jrows).
US="$(printf '\037')"

# _jqpath a.b.0.c -> ."a"."b"[0]."c"   (keys are quoted, so a field name is never re-parsed as syntax)
# awk prints nothing at all for empty input, so the fallback to "." also lives outside the awk program:
# an empty filter would be a jq syntax error, i.e. an unusable reader instead of a whole-document read.
_jqpath(){
  local o
  o="$(printf '%s' "$1" | awk -F. '{o="";for(i=1;i<=NF;i++){if($i=="")continue;if($i ~ /^[0-9]+$/)o=o"["$i"]";else o=o".\""$i"\""}if(o=="")o=".";print o}')"
  [ -n "$o" ] || o="."
  printf '%s' "$o"
}

# _jvalid FILE: the document parses. Checked once per fetch, so a later path miss means "field absent",
# not "corrupt file"; the two must not be confused (one is a zero, the other is a `?`).
_jvalid(){
  case "$JSON" in
    python3) python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" >/dev/null 2>&1;;
    # `jq empty` is the validity check: it produces no output and exits non-zero only on a parse error.
    # `jq -e . file` would answer on the value instead, and a document that is just `null` or `false`
    # is valid JSON that would be declared corrupt.
    jq)      jq empty "$1" >/dev/null 2>&1;;
  esac
}

# _jget FILE DOTPATH prints the scalar. rc 0 = present, rc 4 = absent, rc 3 = unreadable document.
# An absent field is not turned into a zero here: the caller decides, per field, whether absence means
# the zero of the type (it does, in proto3) or an unknown that must surface as `?`.
_jget(){
  case "$JSON" in
    python3)
      python3 -c '
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(3)
for k in [s for s in sys.argv[2].split(".") if s!=""]:
    if isinstance(d,list):
        try: d=d[int(k)]
        except Exception: sys.exit(4)
    elif isinstance(d,dict):
        if k not in d: sys.exit(4)
        d=d[k]
    else: sys.exit(4)
if d is None: sys.exit(4)
if isinstance(d,bool): print("true" if d else "false")
elif isinstance(d,(dict,list)): print(json.dumps(d))
else: print(d)
' "$1" "$2"
      ;;
    jq)
      local f out
      f="$(_jqpath "$2")"
      # Every `try ... catch` is parenthesised as a whole. `try A catch null | B` lets the catch body
      # swallow the pipeline that follows, which turns a missing field into a silently different
      # program rather than into the absent sentinel this function promises to return.
      out="$(jq -r "(try ($f) catch null) | if .==null then \"$ABSENT\" elif (type==\"object\" or type==\"array\") then tojson else tostring end" "$1" 2>/dev/null)" || return 4
      [ "$out" = "$ABSENT" ] && return 4
      printf '%s\n' "$out"
      ;;
  esac
}

# _jrows FILE ARRAYPATH FIELD...: one line per array element, fields joined by US (0x1F). A missing
# field is an empty column; the caller distinguishes empty-because-zero from empty-because-unknown.
#
# The separator is 0x1F and not a tab, for a functional reason.
# TAB is an IFS whitespace character: `IFS=$'\t' read -r a b c` merges runs of tabs and drops empty
# fields, so `A<TAB><TAB>C` gives a=A b=C: the line shifts left by one column and every later value is
# read under its neighbour's name. On a validator row that puts the bond status in `moniker`, the jail
# flag in `bonding status`, and the validator is no longer recognised at all. A moniker is free text
# that any operator writes on chain with `tx staking edit-validator`, and the chain accepts an empty one
# (x/staking/types/validator.go EnsureLength bounds only the maximum), so this is an ordinary field, not
# a hostile endpoint. 0x1F is not an IFS whitespace character, so an empty column stays where it is.
#
# A value must never be able to forge a boundary. Under this separator exactly two characters can: a
# newline ends the record (`read` splits on that and nothing else), and a 0x1F ends the field. A newline
# inside a moniker would start a fabricated record, enough to add a validator to the network census or
# remove a jailed one from the list of validators that need unjailing.
# TAB and CR are replaced too, for a different reason: measured, `IFS=0x1F read` leaves a TAB or a CR
# inside the value and shifts nothing. What they corrupt is what is displayed (a CR rewrites the line
# over itself, so a report can show something no field contains) and what is compared (a consensus key
# carrying a CR matches nothing, and this script identifies "my validator" by comparing exactly that).
# Both readers therefore replace all four characters inside every value with a space, and the
# self-test below checks the two implementations on exactly that.
_jrows(){
  local file="$1" arr="$2"; shift 2
  case "$JSON" in
    python3)
      python3 -c '
import json,sys
f=sys.argv[1]; ap=[s for s in sys.argv[2].split(".") if s!=""]; fields=sys.argv[3:]
SEP="\x1f"
TR={9:" ",10:" ",13:" ",31:" "}
try: d=json.load(open(f))
except Exception: sys.exit(3)
for k in ap:
    if isinstance(d,dict) and k in d: d=d[k]
    elif isinstance(d,list):
        try: d=d[int(k)]
        except Exception: d=None; break
    else: d=None; break
if d is None: d=[]
if not isinstance(d,list): sys.exit(4)
def g(o,p):
    for k in p.split("."):
        if isinstance(o,dict) and k in o: o=o[k]
        else: return ""
    if o is None: return ""
    if isinstance(o,bool): return "true" if o else "false"
    if isinstance(o,(dict,list)): return json.dumps(o).translate(TR)
    return str(o).translate(TR)
for e in d: print(SEP.join(g(e,p) for p in fields))
' "$file" "$arr" "$@"
      ;;
    jq)
      local af sel p pf
      af="$(_jqpath "$arr")"; sel=""
      for p in "$@"; do
        pf="$(_jqpath "$p")"
        if [ -z "$sel" ]; then sel="(try ($pf) catch null)"; else sel="$sel, (try ($pf) catch null)"; fi
      done
      [ -n "$sel" ] || sel="."
      # `((try X catch null) // [])`: the alternative applies to the whole try, not to the catch body.
      # Written as `try X catch null // []` it would parse as `catch (null // [])`, so an absent array
      # would reach `.[]` as null and jq would error out, giving an empty result that reads like "this
      # array is empty", a different and reassuring claim.
      # `@tsv` is not used: it joins with a TAB, and a TAB cannot express an empty column once bash
      # splits on it. The same escaping is done explicitly and identically in both readers, and `join`
      # puts 0x1F between the fields.
      jq -r "((try ($af) catch null) // []) | .[] | [ $sel ] | map(if .==null then \"\" elif (type==\"object\" or type==\"array\") then tojson else tostring end | gsub(\"[\t\n\r\u001f]\";\" \")) | join(\"\u001f\")" "$file" 2>/dev/null
      ;;
  esac
}

# _jtype FILE DOTPATH: the JSON type of the value, in jq's vocabulary: boolean / number / string /
# object / array, and `null` for a value that is null or whose path is not there (the same conflation
# `_jget` already makes, so the two primitives cannot disagree). `?` when the document is unreadable.
# It exists because `_jget` prints a scalar, and the string "true" and the boolean true print the same.
# On a criterion whose reassuring answer is a word (has_recent_seed), that confusion is one-sided: the
# number 1 gets refused while the string "true" gets believed. So the type is checked, not assumed.
_jtype(){
  case "$JSON" in
    python3)
      python3 -c '
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(3)
for k in [s for s in sys.argv[2].split(".") if s!=""]:
    if isinstance(d,list):
        try: d=d[int(k)]
        except Exception: d=None; break
    elif isinstance(d,dict):
        if k not in d: d=None; break
        d=d[k]
    else: d=None; break
if d is None: print("null")
elif isinstance(d,bool): print("boolean")
elif isinstance(d,(int,float)): print("number")
elif isinstance(d,str): print("string")
elif isinstance(d,list): print("array")
else: print("object")
' "$1" "$2" 2>/dev/null || printf '?\n'
      ;;
    jq)
      local f out
      f="$(_jqpath "$2")"
      out="$(jq -r "(try ($f) catch null) | type" "$1" 2>/dev/null)" || { printf '?\n'; return 0; }
      [ -n "$out" ] || out="?"
      printf '%s\n' "$out"
      ;;
  esac
}

# _fetch URL OUTFILE: 200 and parseable, or failure. Anything else is a `?`, never a value.
_fetch(){
  local code
  code="$(curl -sS -m "$TIMEOUT" -o "$2" -w '%{http_code}' "$1" 2>/dev/null)" || code="000"
  [ "$code" = "200" ] || return 1
  _jvalid "$2" || return 1
  return 0
}

# ---------------------------------------------------------------- numbers, and the three states of one
# One reader for every number, with the same three states as the JSON reader above.
# A string that reaches `$(( ))` is evaluated as a variable name; under `set -u` that is a fatal error
# in the middle of the report, which then stops with a truncated page and no verdict (or, under --cron,
# with nothing at all). A string that reaches `awk` arithmetic is worse: it is silently coerced to zero,
# which skews every derived figure (a jail threshold of zero blocks, a slash of one hundred percent, a
# doubled margin). So no value from the network enters either without passing through here first, and
# the callers below have to say what they do with `?`.
#
# The 18-digit ceiling matters: 2^64 wraps silently in shell arithmetic, and a missed block counter of
# exactly 2^64 came back as a full margin. A number too big to compare is not compared.
_int(){   # _int <value> [<min>] [<max>] -> the integer with leading zeros removed, or `?`
  local v="${1:-}" s
  case "$v" in ''|*[!0-9]*) printf '?'; return 0;; esac
  s="${v#"${v%%[!0]*}"}"; [ -n "$s" ] || s=0
  [ "${#s}" -le 18 ] || { printf '?'; return 0; }
  if [ -n "${2:-}" ]; then [ "$s" -ge "$2" ] 2>/dev/null || { printf '?'; return 0; }; fi
  if [ -n "${3:-}" ]; then [ "$s" -le "$3" ] 2>/dev/null || { printf '?'; return 0; }; fi
  printf '%s' "$s"
}
_dec(){   # _dec <value> -> an unsigned decimal (digits, at most one dot, at least one digit), or `?`
  local v="${1:-}" i f
  case "$v" in ''|*[!0-9.]*) printf '?'; return 0;; esac
  case "$v" in *.*.*) printf '?'; return 0;; esac
  i="${v%%.*}"; f=""; case "$v" in *.*) f="${v#*.}";; esac
  [ -n "$i$f" ] || { printf '?'; return 0; }
  printf '%s' "$v"
}
# The slash is computed on digits, not on doubles. `awk` holds numbers as IEEE doubles: past 2^53 a
# subtraction quietly loses its low digits, so the amount of stake burned would be off by millions
# while being announced as "derived on chain". Comparison and difference are therefore done digit by
# digit, exactly; only the percentage, where a relative error of 1e-16 changes nothing, is left to
# floating point.
_bigcmp(){ awk -v a="$1" -v b="$2" 'BEGIN{
  sub(/^0+/,"",a); sub(/^0+/,"",b); if(a=="")a="0"; if(b=="")b="0";
  if(length(a)!=length(b)){ print (length(a)<length(b)) ? -1 : 1; exit }
  if((a "")==(b "")){ print 0; exit }
  print ((a "") < (b "")) ? -1 : 1 }'; }
_bigsub(){ awk -v a="$1" -v b="$2" 'BEGIN{                   # requires a >= b, both runs of digits
  sub(/^0+/,"",a); sub(/^0+/,"",b); if(a=="")a="0"; if(b=="")b="0";
  n=length(a); while(length(b)<n) b="0" b;
  borrow=0; out="";
  for(i=n;i>=1;i--){ d=(substr(a,i,1)+0)-(substr(b,i,1)+0)-borrow;
                     if(d<0){ d+=10; borrow=1 } else borrow=0; out=d out }
  sub(/^0+/,"",out); if(out=="")out="0"; print out }'; }

# ---------------------------------------------------------------- self-test of the JSON primitives
# Two implementations of one primitive drift apart. A host carries python3 or jq, rarely both, so each
# host only ever exercises half of the two readers above, and the half it never runs could report a
# wrong verdict silently. So the reader about to be used is checked on every run against a fixture
# whose answers are known. Failing it refuses to report at all, rather than reporting through a broken
# parser. The false boolean in the fixture matters: `jailed:false` has to come back as "false" and never
# as absent, because absence is what this script escalates to `?` and to a non-zero exit.
#
# The fixture carries the characters that break the format, since a self-test on well-behaved data
# would miss the divergence it exists to find: a row whose middle column is empty, a value carrying a
# TAB, a value carrying a newline. Those are the shapes that shift a column, forge a record, or drop a
# field, and a chain-written moniker can hold all three. The expected strings below are written out in
# full so the check is on the bytes.
_selftest(){
  local fx="$TMP/selftest.json" bad="$TMP/selftest_bad.json" rows exp
  printf '%s' '{"a":{"b":"x1"},"n":7,"t":true,"f":false,"z":0,"s":"","st":"true",
    "list":[{"k":"r1","m":"","d":{"e":"v1"}},{"k":"r2","m":"a\tb"},{"k":"r3","m":"c\nd","d":{"e":"v3"}}]}' > "$fx" || return 20
  printf '%s' '{"a":' > "$bad" || return 20
  _jvalid "$fx" || return 1
  _jvalid "$bad" && return 2
  [ "$(_jget "$fx" a.b)" = "x1" ] || return 3
  [ "$(_jget "$fx" n)" = "7" ] || return 4
  [ "$(_jget "$fx" f)" = "false" ] || return 5
  [ "$(_jget "$fx" z)" = "0" ] || return 6
  _jget "$fx" nope >/dev/null 2>&1 && return 7
  [ "$(_jget "$fx" list.1.k)" = "r2" ] || return 8
  rows="$(_jrows "$fx" list k m d.e)"
  exp="$(printf 'r1%s%sv1\nr2%sa b%s\nr3%sc d%sv3' "$US" "$US" "$US" "$US" "$US" "$US")"
  [ "$rows" = "$exp" ] || return 9
  # The split is replayed here too, not only the printing. Comparing `_jrows` to a string proves what
  # the reader emits; the report depends on what `read` does with it. This is the loop every section
  # below uses, run against the row that carries the empty column, plus a count, because a newline
  # inside a value must not have become a fourth record.
  local _k _m _e _n=0 _bad=0
  while IFS="$US" read -r _k _m _e; do
    _n=$(( _n + 1 ))
    case "$_k" in r1) [ -z "$_m" ] || _bad=10; [ "$_e" = "v1" ] || _bad=11;; esac
  done <<EOF
$rows
EOF
  [ "$_bad" = 0 ] || return "$_bad"
  [ "$_n" = 3 ] || return 12
  [ "$(_jtype "$fx" t)" = "boolean" ] || return 13
  [ "$(_jtype "$fx" st)" = "string" ] || return 14
  [ "$(_jtype "$fx" n)" = "number" ] || return 15
  [ "$(_jtype "$fx" nope)" = "null" ] || return 16
  # the numeric readers are part of the contract too: their three states are what every section below
  # leans on, and a broken `_int` would turn every guard added on top of it into a decoration.
  [ "$(_int 42)" = "42" ] || return 17
  [ "$(_int 007)" = "7" ] || return 17          # never re-read as octal by $(( ))
  [ "$(_int abc)" = "?" ] || return 17
  [ "$(_int '')" = "?" ] || return 17
  [ "$(_int -40)" = "?" ] || return 17
  [ "$(_int 18446744073709551616)" = "?" ] || return 17
  [ "$(_int 7 0 5)" = "?" ] || return 17
  [ "$(_dec 0.5)" = "0.5" ] || return 18
  [ "$(_dec abc)" = "?" ] || return 18
  [ "$(_dec 1.2.3)" = "?" ] || return 18
  [ "$(_bigsub 1000000000000000000000000 999999990000000000000000)" = "10000000000000000" ] || return 19
  [ "$(_bigcmp 10 9)" = "1" ] && [ "$(_bigcmp 9 10)" = "-1" ] && [ "$(_bigcmp 010 10)" = "0" ] || return 19
  return 0
}
_selftest; ST=$?
if [ "$ST" -ne 0 ]; then
  printf '%s\n' "[health] FATAL: the $JSON JSON reader failed this script's own self-test (check $ST)." >&2
  printf '%s\n' "[health] Every number below would come from that reader, so nothing is reported." >&2
  printf '%s\n' "[health] Try the other reader:  DENDRA_JSON=python3 $SELF_CMD   (or DENDRA_JSON=jq $SELF_CMD)" >&2
  _cron_note "the $JSON reader failed this script's own self-test (check $ST), so this run measured nothing"
  exit 2
fi

_host_of(){ printf '%s' "$1" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#/.*$##; s#:[0-9]+$##'; }
_epoch(){ date -u -d "$1" +%s 2>/dev/null; }   # GNU date; empty on any other date(1) -> durations become `?`

# ---------------------------------------------------------------- endpoint
# The kit publishes one endpoint, `DENDRA_NODE=tcp://HOST:26657` (deploy/testnet/publish_network.sh:68),
# and that is what network-info.txt carries. The module queries used here are REST, so the API endpoint
# is derived from that same host at its standard port, the same derivation join.sh uses for the
# capacity registry (join.sh, capacity_url). Nothing is hard-coded: every candidate below comes from
# something the operator already has.
#
# DENDRA_REST is exclusive. Were it merely the first candidate in the list, pointing the script at your
# own node and getting no answer would produce a complete, healthy-looking report about a different
# node, with the operator convinced they were reading their own. That is the same wrong answer as
# identifying "my validator" from someone else's public RPC (see the self-identification note in
# section 1). So when DENDRA_REST is set, the candidate list contains it and nothing else, and a silence
# there is fatal. Falling back is a decision this script does not make on the operator's behalf.
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"; REPO="$(cd "${SELF:-.}/.." 2>/dev/null && pwd)"
CAND=""; CAND_SRC=""
_addc(){ # $1 = url, $2 = where that url came from (printed in the footer)
  case " $CAND " in *" $1 "*) return 0;; esac
  CAND="$CAND${CAND:+ }$1"
  CAND_SRC="$CAND_SRC$1 $2
"
}
_srcof(){ printf '%s' "$CAND_SRC" | while IFS=' ' read -r _u _s; do [ "$_u" = "$1" ] && { printf '%s' "$_s"; break; }; done; }

REST_FORCED=0
if [ -n "${DENDRA_REST:-}" ]; then
  REST_FORCED=1
  _addc "${DENDRA_REST%/}" "DENDRA_REST (forced: no other endpoint was tried)"
else
  [ -n "${DENDRA_NODE:-}" ] && _addc "http://$(_host_of "$DENDRA_NODE"):1317" "derived from DENDRA_NODE"
  if [ -n "${CONFIG_URL:-}" ]; then
    _n="$(curl -fsSL -m "$TIMEOUT" "$CONFIG_URL" 2>/dev/null | sed -n 's/^DENDRA_NODE=//p' | head -1 | tr -d '\r')"
    [ -n "${_n:-}" ] && _addc "http://$(_host_of "$_n"):1317" "derived from DENDRA_NODE published at CONFIG_URL"
  fi
  # A validator machine always holds SEEDS=<node-id>@HOST:26656 in the node kit's .env: the operator's
  # public host, without asking the operator for anything.
  if [ -f "${REPO:-}/deploy/testnet-node/.env" ]; then
    _s="$(sed -n 's/^SEEDS=//p' "$REPO/deploy/testnet-node/.env" 2>/dev/null | head -1 | tr -d '\r')"
    _s="${_s%%,*}"; _s="${_s##*@}"; _s="${_s%%:*}"
    [ -n "${_s:-}" ] && _addc "http://$_s:1317" "derived from the seeds of deploy/testnet-node/.env"
  fi
  if [ -f "${REPO:-}/deploy/testnet-miner/.env" ]; then
    _m="$(sed -n 's/^DENDRA_NODE=//p' "$REPO/deploy/testnet-miner/.env" 2>/dev/null | head -1 | tr -d '\r')"
    if [ -n "${_m:-}" ]; then
      _m="$(_host_of "$_m")"
      # The miner's DENDRA_NODE is a container-side name: the node's alias on the dendra-chain network
      # (`dendra-node`, or the node kit's project name), or host.docker.internal from an older kit. Neither
      # resolves from the host, where the node kit publishes on 127.0.0.1. A name without a dot is such an
      # alias (never a public endpoint) and is read as this machine (an IPv6 literal, with colons, is not).
      case "$_m" in host.docker.internal|*.*|*:*) : ;; *) _m="127.0.0.1" ;; esac
      [ "$_m" = "host.docker.internal" ] && _m="127.0.0.1"
      _addc "http://$_m:1317" "derived from DENDRA_NODE in deploy/testnet-miner/.env"
    fi
  fi
  _addc "http://127.0.0.1:1317" "this machine (last-resort default)"
fi

# The repository root as an absolute path, verified by the file it must contain. Every command this
# script prints is meant to be pasted into a shell whose working directory nobody knows, so a relative
# `-f deploy/testnet-node/docker-compose.yml` fails for anyone standing anywhere else, with "no
# configuration file provided", which names nothing and reads like a broken kit.
COMPOSE=""
[ -n "${REPO:-}" ] && [ -f "$REPO/deploy/testnet-node/docker-compose.yml" ] && COMPOSE="$REPO/deploy/testnet-node/docker-compose.yml"
if [ -z "$COMPOSE" ]; then
  # join.sh runs this script from a temporary copy (it strips CR first) and passes the real location in
  # DENDRA_HEALTH_CMD; `$0` then points into /tmp and the repository is nowhere above it. That variable
  # is the only place the real path survives, so it is looked at rather than guessed.
  _sp="${SELF_CMD##* }"
  case "$_sp" in
    */deploy/validator_health.sh)
      _sr="${_sp%/deploy/validator_health.sh}"
      [ -f "$_sr/deploy/testnet-node/docker-compose.yml" ] && COMPOSE="$_sr/deploy/testnet-node/docker-compose.yml";;
  esac
fi
# Last source: the current directory. An operator who pipes this script straight from the mirror runs a
# copy in /tmp, where neither $0 nor DENDRA_HEALTH_CMD knows about a repository, but they usually paste
# that one-liner from inside the clone they already have. Recognised by the presence of the file itself,
# never by a name, and checked last so a real repository always wins.
if [ -z "$COMPOSE" ] && [ -f "./deploy/testnet-node/docker-compose.yml" ]; then
  COMPOSE="$(pwd)/deploy/testnet-node/docker-compose.yml"
fi
# _compose_cmd PREFIX prints the `docker compose -f ... exec -T node` line, absolute when the file was
# actually found, and otherwise preceded by the `cd` that makes the relative form true. Never a bare
# relative path pretending to work from anywhere.
_compose_cmd(){
  if [ -n "$COMPOSE" ]; then
    printf '%sdocker compose -f %s exec -T node \\\n' "$1" "$COMPOSE"
  else
    printf '%scd <the dendra repository you cloned>    # the -f path below is relative to it\n' "$1"
    printf '%sdocker compose -f deploy/testnet-node/docker-compose.yml exec -T node \\\n' "$1"
  fi
}

# This runs before any network dependency on purpose.
# Further down, a machine with no REST API in range makes the script exit 2, rightly refusing to claim
# anything. RPC exposure is a local property that needs no API, and a machine that cannot reach one is
# the machine most likely to be misconfigured, so the check must not sit behind that dependency.
# --- RPC exposure, reported here rather than left in a comment -------------------------------------
# `deploy/testnet-node/docker-compose.yml` publishes the RPC on 127.0.0.1 by default. A co-located miner
# reaches its node on the `dendra-chain` Docker network, by the node's alias, so loopback costs it
# nothing. The exception is a miner kit generated by an older join.sh, whose .env still names
# `host.docker.internal` (the Docker bridge gateway, which a loopback bind cuts off): that one is called
# out below.
#
# `join.sh --validator` never mentions the RPC binding, and a requirement that lives only in a compose
# comment is read once at install time, on the machine whose failure stops the chain.
#
# What is measured is the effect, not the declaration. Reading `DENDRA_RPC_BIND` from a `.env` would
# report what someone meant; `docker port` reports what Docker actually published. A knob set in the
# wrong `.env`, or a container started before it was set, differs from the intent, and only the second
# reading catches it.
#
# Comparing the two also catches the worst case: a declaration of `127.0.0.1` on a port Docker still
# publishes on `0.0.0.0` (the file was edited, the container never recreated) warns. The operator
# believes the RPC is closed; the effect says otherwise, and the effect is what an outsider reaches.
#
# Three states. Published on all interfaces: reported, with its cost. Published on loopback: fine.
# Unreadable (no docker, no such container, another topology): reported as not measured. An exposure
# that cannot be established is not an absence of exposure.
# The declaration is read where the operator is told to put it, not only in this shell's environment.
# `testnet-node/README.md` instructs `echo 'DENDRA_RPC_BIND=...' >> .env`, and docker compose reads
# that file but a plain shell does not. Checking only the environment would keep warning an operator
# who has declared their choice in the `.env` the documentation names, which teaches people to ignore
# the warning. The environment still wins: that is what an override means.
# Two stacks keep the same declaration in two places, and both are read. A joiner runs the node kit and
# writes its `.env` beside `deploy/testnet-node/docker-compose.yml`; an operator runs the root stack
# and writes the repository's own `.env`. Reading only one would keep warning a host whose owner has
# decided, in the file their own stack reads.
# --- The measurement is written once, the costs are not ---------------------------------------------
# Three ports, three different consequences, one way of establishing the facts. The three-state logic
# (published wide / published narrow / unreadable) is a single function so a fix to it reaches all
# three; the text that follows each verdict is per port, because closing each one costs something
# different.
#
# `tr -d '\042\047 \r'` strips quotes and CR by octal code on purpose: writing the quote characters
# themselves here would need four levels of shell escaping.
_expo_decl=""
_expo_map=""
_expo_read(){ # $1 = port, $2 = knob name
  case "$2" in
    DENDRA_RPC_BIND)  _expo_decl="${DENDRA_RPC_BIND:-}" ;;
    DENDRA_API_BIND)  _expo_decl="${DENDRA_API_BIND:-}" ;;
    DENDRA_GRPC_BIND) _expo_decl="${DENDRA_GRPC_BIND:-}" ;;
    *)                _expo_decl="" ;;
  esac
  for _e in "${COMPOSE%/*}/.env" "${REPO:-.}/.env"; do
    [ -n "$_expo_decl" ] && break
    [ -f "$_e" ] || continue
    _expo_decl="$(grep -E "^[[:space:]]*$2=" "$_e" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\042\047 \r')"
  done
  _n="$(docker ps --filter "publish=$1" --format '{{.Names}}' 2>/dev/null | head -1)"
  _expo_map=""
  [ -n "$_n" ] && _expo_map="$(docker port "$_n" "$1" 2>/dev/null | head -1)"
}
_expo_wide(){ printf '%s' "$_expo_map" | grep -qE '^(0\.0\.0\.0|\[::\]|::):'; }

printf '\n'

# --- 26657, the consensus RPC -----------------------------------------------------------------------
_expo_read "${DENDRA_RPC_PORT:-26657}" DENDRA_RPC_BIND
if [ -z "$_expo_map" ]; then
  printf '  ·  RPC exposure not measured — no container publishing %s was readable from here.\n' "${DENDRA_RPC_PORT:-26657}"
  printf '     Not knowing is not "closed": if this node runs outside docker, or under another\n'
  printf '     project name, check the binding yourself before assuming it is private.\n'
elif _expo_wide && [ "$_expo_decl" = "0.0.0.0" ]; then
  # A warning that never goes quiet stops being read. An operator who publishes the RPC on purpose (it
  # is how `DENDRA_NODE` is served to joiners) would meet this warning on every hourly run and learn to
  # skip it, missing the accidental case too. So the effect is always measured and printed; only the
  # tone depends on whether someone declared it.
  printf '  ·  RPC published on every interface (%s) — declared via DENDRA_RPC_BIND, not accidental.\n' "$_expo_map"
  printf '     /net_info and /dump_consensus_state are readable by anyone; that is the price of the\n'
  printf '     choice, and it is yours to make. Nothing to do.\n'
elif _expo_wide; then
  printf '  !! Your RPC is published on every interface (%s).\n' "$_expo_map"
  printf '     Anyone can read /net_info (your peer list, hence the topology around a machine whose\n'
  printf '     failure stops the chain) and /dump_consensus_state. CometBFT has no per-method filter:\n'
  printf '     `unsafe = false` closes the destructive commands, not these two. It is the whole port\n'
  printf '     or nothing, so the only lever is the bind address.\n'
  printf '     To close it, set DENDRA_RPC_BIND=127.0.0.1 in deploy/testnet-node/.env, then from that\n'
  printf '     directory: docker compose up -d. If this node serves a public RPC on purpose (an operator\n'
  printf '     DENDRA_NODE, a state-sync source), declare DENDRA_RPC_BIND=0.0.0.0 instead and this goes quiet.\n'
  printf '     A miner kit on this host loses nothing: it reaches the node on the dendra-chain network.\n'
else
  printf '  ok RPC published on %s — not reachable from outside this host.\n' "$_expo_map"
fi
# The one miner a loopback bind cuts off: a kit generated by an older join.sh, whose .env still points
# the container at host.docker.internal (the bridge gateway, which a 127.0.0.1 publish cuts off).
# Reported when it can be read; re-running deploy/join.sh rewrites that line to the node's alias.
_mk="${REPO:-.}/deploy/testnet-miner/.env"
if [ -n "$_expo_map" ] && ! _expo_wide && [ -f "$_mk" ] \
   && sed -n 's/^DENDRA_NODE=//p' "$_mk" 2>/dev/null | grep -q 'host\.docker\.internal'; then
  printf '  !! The miner kit on this host still reads its node through host.docker.internal, and the RPC is\n'
  printf '     published on %s only: that miner cannot reach its node. Re-run deploy/join.sh: it points the\n' "$_expo_map"
  printf '     miner at the node by its alias on the dendra-chain network (from the CONFIG_URL the kit names,\n'
  printf '     or in place when it names none).\n'
fi

# --- 1317, the REST API: joiners depend on it, which changes the advice -----------------------------
_expo_read 1317 DENDRA_API_BIND
if [ -z "$_expo_map" ]; then
  printf '  ·  REST (1317) exposure not measured — no container publishing it was readable here.\n'
elif _expo_wide && [ "$_expo_decl" = "0.0.0.0" ]; then
  printf '  ·  REST published on every interface (%s) — declared via DENDRA_API_BIND.\n' "$_expo_map"
elif _expo_wide; then
  printf '  ·  REST (1317) is published on every interface (%s), and on an operator host that is\n' "$_expo_map"
  printf '     usually right: `deploy/join.sh` derives its endpoint from DENDRA_NODE as <host>:1317,\n'
  printf '     the launch scripts read min_stake there and refuse to guess without it, and the kit\n'
  printf '     README sends people to it for the counters that say what the network is short of.\n'
  printf '     Closing it breaks that documented path for everyone but you. Declare the choice with\n'
  printf '     DENDRA_API_BIND=0.0.0.0 rather than leaving it unsaid, or bind it to 127.0.0.1 on a\n'
  printf '     validator that serves no joiners.\n'
else
  printf '  ok REST (1317) published on %s — not reachable from outside this host.\n' "$_expo_map"
fi

# --- 9090, gRPC: the cheapest port to close ---------------------------------------------------------
_expo_read 9090 DENDRA_GRPC_BIND
if [ -z "$_expo_map" ]; then
  printf '  ·  gRPC (9090) exposure not measured — no container publishing it was readable here.\n'
elif _expo_wide && [ "$_expo_decl" = "0.0.0.0" ]; then
  printf '  ·  gRPC published on every interface (%s) — declared via DENDRA_GRPC_BIND.\n' "$_expo_map"
elif _expo_wide; then
  printf '  !! gRPC (9090) is published on every interface (%s), and nothing published reads it.\n' "$_expo_map"
  printf '     No script, page or document in this repository consumes the chain gRPC from outside;\n'
  printf '     it is reachable because it was never given a knob, not because something needs it.\n'
  printf '     That makes it the cheapest hardening available on a validator:\n'
  printf '       echo DENDRA_GRPC_BIND=127.0.0.1 >> .env && docker compose up -d\n'
  printf '     If your tooling uses it, declare DENDRA_GRPC_BIND=0.0.0.0 and this line goes quiet.\n'
else
  printf '  ok gRPC (9090) published on %s — not reachable from outside this host.\n' "$_expo_map"
fi

REST=""
for _c in $CAND; do
  if _fetch "$_c/cosmos/staking/v1beta1/validators?pagination.limit=300" "$TMP/validators.json"; then REST="$_c"; break; fi
done
if [ -z "$REST" ] && [ "$REST_FORCED" = 1 ]; then
  printf '%s\n' "[health] FATAL: DENDRA_REST=$DENDRA_REST did not answer, and nothing else was tried." >&2
  printf '%s\n' "[health] You named an endpoint, so that is the only one this script will read. Reporting on" >&2
  printf '%s\n' "[health] some other node instead would produce a full, confident report about a validator" >&2
  printf '%s\n' "[health] that is not the one you are asking about — and it would probably look healthy." >&2
  printf '%s\n' "[health] Either fix the address, or unset DENDRA_REST and let the script choose:" >&2
  printf '%s\n' "[health]     unset DENDRA_REST; $SELF_CMD [dendravaloper1...]" >&2
  printf '%s\n' "[health] If you run the node yourself and its API port is closed, query it from inside:" >&2
  _compose_cmd "[health]     " >&2
  printf '%s\n' "[health]       dendrad query staking validators -o json --node tcp://localhost:26657" >&2
  _cron_note "DENDRA_REST=$DENDRA_REST did not answer, so this run could not read the chain at all"
  exit 2
fi
if [ -z "$REST" ]; then
  printf '%s\n' "[health] FATAL: no reachable Dendra REST API. Tried: $CAND" >&2
  printf '%s\n' "[health] Nothing below can be answered, so nothing is claimed. Point the script at an API:" >&2
  printf '%s\n' "[health]     DENDRA_REST=http://<host>:1317 $SELF_CMD [dendravaloper1...]" >&2
  printf '%s\n' "[health] If you run the node yourself and its API port is closed, query it from inside:" >&2
  _compose_cmd "[health]     " >&2
  printf '%s\n' "[health]       dendrad query staking validators -o json --node tcp://localhost:26657" >&2
  _cron_note "no reachable Dendra REST API (tried: $CAND), so this run could not read the chain at all"
  exit 2
fi
REST_SRC="$(_srcof "$REST")"; [ -n "$REST_SRC" ] || REST_SRC="unknown origin"
if [ -n "${DENDRA_RPC:-}" ]; then RPC="$DENDRA_RPC"; RPC_SRC="DENDRA_RPC (set in the environment)"
else RPC="http://$(_host_of "$REST"):26657"; RPC_SRC="derived from the REST host above"; fi
RPC="${RPC%/}"

# ---------------------------------------------------------------- report
RC=0; SELF_BAD=0; NET_BAD=0; NO_VAL=0; NO_VAL_KIND=""; UNSURE=0
# A problem with your validator and a problem with the network are different news, and merging them
# would tell an operator whose node is healthy that it is not. They share the exit code (1 = a named
# problem) but the verdict says which one it is.
#
# A third case is neither: something this run could not measure. `_problem` states a fact, `_unknown`
# states an absence of fact, and only the second raises UNSURE. Nothing downstream may treat the two
# alike, in particular the cron mode, which may clear a standing alert on a positive all-clear and on
# nothing else. An unmeasured criterion is not a passed criterion.
_problem(){ printf '  !! %s\n' "$1"; SELF_BAD=1; RC=1; return 0; }
_unknown(){ printf '  !! %s\n' "$1"; SELF_BAD=1; RC=1; UNSURE=1; return 0; }
_net_unknown(){ printf '  !! %s\n' "$1"; NET_BAD=1; RC=1; UNSURE=1; return 0; }

main_report(){
RC=0; SELF_BAD=0; NET_BAD=0; NO_VAL=0; NO_VAL_KIND=""; UNSURE=0; SL_AMT=""

# --- chain identity, height, and the measured block interval -----------------------------------------
# The chain id is read on both endpoints and compared, because they may be two different nodes.
# The staking set, the slashing records and the committee seed are served by the REST endpoint; the
# chain id, the network height, the consensus set size and the block timestamps are served by the RPC
# one. Nothing forces them to be the same node, and DENDRA_RPC lets an operator point them apart
# without meaning to. Without the comparison, the report would mix two chains into one page and then
# print the other chain's id inside the unjail transaction it tells the operator to sign.
CHAIN_ID="?"; CHAIN_ID_RPC=""; CHAIN_ID_REST=""; CHAIN_SPLIT=0
NET_H_RAW=""; NET_H_SRC=""; CONS_RAW=""; CONS_SEEN=0
if _fetch "$RPC/status" "$TMP/status.json"; then
  CHAIN_ID_RPC="$(_jget "$TMP/status.json" result.node_info.network || printf '')"
  NET_H_RAW="$(_jget "$TMP/status.json" result.sync_info.latest_block_height || printf '')"
  [ -n "$NET_H_RAW" ] && NET_H_SRC="$RPC/status"
fi
if _fetch "$REST/dendra/jobs/v1/committee_seed_health" "$TMP/seed.json"; then SEED_OK=1; else SEED_OK=0; fi
if _fetch "$REST/cosmos/base/tendermint/v1beta1/node_info" "$TMP/node_info.json"; then
  CHAIN_ID_REST="$(_jget "$TMP/node_info.json" default_node_info.network || printf '')"
fi
if [ -n "$CHAIN_ID_RPC" ] && [ -n "$CHAIN_ID_REST" ] && [ "$CHAIN_ID_RPC" != "$CHAIN_ID_REST" ]; then
  CHAIN_SPLIT=1
elif [ -n "$CHAIN_ID_REST" ]; then CHAIN_ID="$CHAIN_ID_REST"
elif [ -n "$CHAIN_ID_RPC" ]; then CHAIN_ID="$CHAIN_ID_RPC"
fi
if [ -z "$NET_H_RAW" ] && [ "$SEED_OK" = 1 ]; then
  NET_H_RAW="$(_jget "$TMP/seed.json" current_height || printf '')"
  [ -n "$NET_H_RAW" ] && NET_H_SRC="$REST/dendra/jobs/v1/committee_seed_health"
fi
# `?` covers both "no height was served" and "a height was served that is not a height". The second
# would kill this run: `$(( NET_H - 1 ))` on the string "abc" is a variable reference, and under
# `set -u` that ends the script mid-report with `abc: unbound variable` and nothing else.
NET_H="$(_int "${NET_H_RAW:-}")"
if _fetch "$RPC/validators?per_page=100" "$TMP/cons.json"; then
  CONS_SEEN=1
  CONS_RAW="$(_jget "$TMP/cons.json" result.total || printf '')"
fi
# A consensus set of zero is a chain that signs nothing, and the line that prints it calls itself "the
# number that decides whether anything signs at all". So the floor is 1.
CONS_N="$(_int "${CONS_RAW:-}" 1)"

# The interval is measured, not assumed. The consensus does not fix a block interval: it emerges from
# timeout_commit and from propagation, so any duration printed below is a derived quantity and names
# the measurement it comes from. Two heights, two header timestamps, one division. When the timestamps
# cannot be read, the interval is `?` and every duration derived from it is `?` too, with no default.
ITV=""; ITV_TXT="? (block timestamps unreadable: durations below are '?', block counts remain exact)"
if [ "$NET_H" != "?" ]; then
  H2=$(( NET_H - 1 )); SPAN=500
  [ "$H2" -gt $(( SPAN + 1 )) ] || SPAN=$(( H2 - 1 ))
  if [ "$SPAN" -ge 10 ]; then
    H1=$(( H2 - SPAN ))
    if _fetch "$RPC/block?height=$H2" "$TMP/b2.json" && _fetch "$RPC/block?height=$H1" "$TMP/b1.json"; then
      T2="$(_jget "$TMP/b2.json" result.block.header.time || printf '')"
      T1="$(_jget "$TMP/b1.json" result.block.header.time || printf '')"
      E2="$(_epoch "${T2:-x}")"; E1="$(_epoch "${T1:-x}")"
      if [ -n "${E2:-}" ] && [ -n "${E1:-}" ] && [ "$E2" -gt "$E1" ] 2>/dev/null; then
        ITV="$(awk -v a="$E1" -v b="$E2" -v n="$SPAN" 'BEGIN{printf "%.2f",(b-a)/n}')"
        ITV_TXT="$ITV s/block (measured over $SPAN blocks, heights $H1 -> $H2)"
      fi
    fi
  fi
fi
# Is the chain still producing? The interval above cannot answer that.
# It divides two past header timestamps, so on a chain that stopped an hour ago it still prints a
# normal figure: every quantity in this report is read from the chain, and a chain that produces
# nothing simply stops changing any of them. `catching_up` reads false (there is nothing to catch up
# with), the validator stays bonded, and `missed_blocks_counter` freezes because missed blocks are only
# counted on blocks that get produced. Nothing here goes red on its own.
# The one quantity that separates "frozen" from "healthy" is the age of the last block against the wall
# clock, and it is compared to the measured interval rather than to a number written here.
# Zero rule, on a security criterion: an unreadable timestamp is `?`, never "fresh".
CHAIN_AGE=""; CHAIN_AGE_TXT="? (the last block's timestamp could not be read, so liveness is unknown)"
CHAIN_STALLED=0
CHAIN_T="$(_jget "$TMP/status.json" result.sync_info.latest_block_time 2>/dev/null || printf '')"
if [ -n "${CHAIN_T:-}" ]; then
  CHAIN_E="$(_epoch "$CHAIN_T")"
  NOW_E="$(date -u +%s 2>/dev/null || printf '')"
  if [ -n "${CHAIN_E:-}" ] && [ -n "${NOW_E:-}" ]; then
    CHAIN_AGE=$(( NOW_E - CHAIN_E ))
    [ "$CHAIN_AGE" -lt 0 ] && CHAIN_AGE=0
    # The bound is derived from the interval this run measured, and only falls back to a written
    # number when the interval itself is unknown. Twelve intervals is roughly a minute on this chain:
    # long enough that a slow round never trips it, short enough that a stop is named while it matters.
    if [ -n "${ITV:-}" ]; then
      CHAIN_MAX="$(awk -v i="$ITV" 'BEGIN{v=i*12; if(v<60)v=60; printf "%d", v}')"
      CHAIN_BOUND="12 x the measured ${ITV} s interval"
    else
      CHAIN_MAX=120; CHAIN_BOUND="120 s (the interval could not be measured, so this bound is written, not derived)"
    fi
    if [ "$CHAIN_AGE" -gt "$CHAIN_MAX" ]; then
      CHAIN_STALLED=1
      CHAIN_AGE_TXT="$(awk -v s="$CHAIN_AGE" 'BEGIN{printf "%d s ago", s}') -- above the bound of ${CHAIN_MAX} s ($CHAIN_BOUND)"
    else
      CHAIN_AGE_TXT="$(awk -v s="$CHAIN_AGE" 'BEGIN{printf "%d s ago", s}') (bound: ${CHAIN_MAX} s, $CHAIN_BOUND)"
    fi
  fi
fi

_mins(){ # blocks -> "~X.X min at the measured interval", or `?` when the interval is unknown
  if [ -n "${ITV:-}" ]; then awk -v n="$1" -v i="$ITV" 'BEGIN{printf "~%.1f min at the measured %.2f s/block", n*i/60, i}'
  else printf '%s' "? min (block interval unknown)"; fi
}

printf '=== Dendra validator health ============================================\n'
printf '  chain-id        : %s\n' "$CHAIN_ID"
printf '  api (REST)      : %s   <- staking set, slashing records, slashing params, committee seed\n' "$REST"
printf '                    source: %s\n' "$REST_SRC"
printf '  rpc             : %s   <- chain-id, consensus set, block timestamps\n' "$RPC"
printf '                    source: %s\n' "$RPC_SRC"
# The height names the endpoint that actually served it. It is read from the RPC when the RPC answers
# and from the REST committee_seed_health when it does not, so a fixed line crediting the RPC would be
# wrong exactly when the RPC is broken, which is when somebody reads this line.
printf '  network height  : %s\n' "$NET_H"
printf '                    source: %s\n' "${NET_H_SRC:-no endpoint served a height}"
printf '  block interval  : %s\n' "$ITV_TXT"
if [ "$CHAIN_SPLIT" = 1 ]; then
  printf '  The two endpoints do not name the same chain:\n'
  printf '      %s says "%s"\n' "$RPC" "$CHAIN_ID_RPC"
  printf '      %s says "%s"\n' "$REST" "$CHAIN_ID_REST"
  printf '  Half of this page would come from one chain and half from the other, so the chain-id is `?`\n'
  printf '  and it is not written into the unjail command below. Point both at the same node:\n'
  printf '      DENDRA_REST=http://HOST:1317 DENDRA_RPC=http://HOST:26657 %s\n' "$SELF_CMD"
  _net_unknown "the REST and RPC endpoints report different chain-ids — nothing on this page can be attributed to one chain"
fi
if [ -n "$NET_H_RAW" ] && [ "$NET_H" = "?" ]; then
  _net_unknown "$NET_H_SRC served a network height that is not a height (\"$NET_H_RAW\") — heights and durations below are '?', and a document that lies about its height is not a document to trust"
fi

# --- the validator rows (one fetch, reused by every section) -----------------------------------------
VROWS="$(_jrows "$TMP/validators.json" validators \
  operator_address description.moniker status jailed tokens delegator_shares \
  unbonding_height unbonding_time consensus_pubkey.key min_self_delegation)"
NEXT_KEY="$(_jget "$TMP/validators.json" pagination.next_key || printf '')"
PG_TOTAL="$(_int "$(_jget "$TMP/validators.json" pagination.total || printf '')")"

# The census is taken here, before section 1, because section 1 may not conclude "there is nothing to
# watch on this machine" without knowing whether the staking set was read at all. An endpoint that
# answers 200 with an empty validator list (a node resyncing, a pruned node, a node on another chain)
# would otherwise produce that reassuring sentence, and under --cron delete the standing alert of a
# jailed validator.
ST_TOTAL=0; ST_BONDED=0; ST_JAILED=0; JAILED_LIST=""
while IFS="$US" read -r _op _mon _st _jl _tok _shr _uh _ut _ck _msd; do
  [ -n "${_op:-}" ] || continue
  ST_TOTAL=$(( ST_TOTAL + 1 ))
  [ "${_st:-}" = "BOND_STATUS_BONDED" ] && ST_BONDED=$(( ST_BONDED + 1 ))
  if [ "${_jl:-}" = "true" ]; then
    ST_JAILED=$(( ST_JAILED + 1 ))
    JAILED_LIST="$JAILED_LIST$_op$US${_mon:-(no moniker)}
"
  fi
done <<EOF
$VROWS
EOF
# Pagination measures what was not read, so it counts as an unknown like everything else. A `[!]` that
# changed neither the exit code nor the verdict would sit above a line saying healthy, and under --cron
# would produce no file at all. `pagination.total` is checked too: a document that announces more
# validators than it hands over contradicts itself, and no next_key is needed for that.
PARTIAL=0
[ -n "$NEXT_KEY" ] && PARTIAL=1
if [ "$PG_TOTAL" != "?" ] && [ "$PG_TOTAL" -gt "$ST_TOTAL" ]; then PARTIAL=1; fi
if [ "$PARTIAL" = 1 ]; then
  printf '  [!] the validator list is incomplete (pagination.total=%s, %s rows served, next_key=%s):\n' \
    "$PG_TOTAL" "$ST_TOTAL" "${NEXT_KEY:-(none)}"
  printf '      the network counts below are partial, and a validator beyond the first page — possibly\n'
  printf '      yours — is invisible to this run.\n'
  _net_unknown "the validator list was truncated, so every count in section 4 is a lower bound and none of them is a clean bill of health"
fi

# --- 1. Your validator --------------------------------------------------------------------------------
printf '\n--- 1. Your validator --------------------------------------------------\n'

LOCAL_NOTE=""; LOCAL_OK=0; LOCAL_ANSWERED=0; LOCAL_IS_TARGET=0; LOCAL_CHAIN=""; LCU="?"; LCU_RAW=""; LH=""; LHI="?"
LOCAL_RPC="${DENDRA_LOCAL_RPC:-http://127.0.0.1:26657}"
if [ -z "$TARGET" ]; then
  # Self-identification is read from a local node, never from DENDRA_NODE.
  # DENDRA_NODE is the network operator's public RPC (the seed provider). Deriving "my validator" from
  # it would identify someone else's validator, which would probably look healthy. Identification
  # therefore uses DENDRA_LOCAL_RPC (127.0.0.1 by default, the port the node kit publishes) and the
  # source is printed, so a wrong identification is visible.
  LOCAL_RPC="${LOCAL_RPC%/}"
  if _fetch "$LOCAL_RPC/status" "$TMP/local.json"; then
    LOCAL_ANSWERED=1
    LPK="$(_jget "$TMP/local.json" result.validator_info.pub_key.value || printf '')"
    LH="$(_jget "$TMP/local.json" result.sync_info.latest_block_height || printf '')"
    LCU_RAW="$(_jget "$TMP/local.json" result.sync_info.catching_up)" || LCU_RAW=""
    LOCAL_CHAIN="$(_jget "$TMP/local.json" result.node_info.network || printf '')"
    # `catching_up` has three states, and the proto3 zero rule does not apply to this document.
    # This is CometBFT's JSON-RPC status, marshalled by encoding/json, not a proto3 message. It does not
    # omit a false boolean: measured on this network, /status carries "catching_up": false, as a JSON
    # boolean, while the node is caught up. So an absent field here means a document other than the one
    # this script asked for, rather than the zero of its type; and a value that is present without being a
    # boolean (the number 1, the string "yes") is an unknown. Calling everything except the literal
    # `true` false would guess the answer to "is this node signing", the question a downtime jail
    # depends on.
    LCU="?"; case "$(_jtype "$TMP/local.json" result.sync_info.catching_up)" in boolean) LCU="$LCU_RAW";; esac
    # LOCAL_OK means "this machine answered and named its consensus key", which is what turns "no
    # validator here" from an unknown into a measurement. Without the key the node answered but said
    # nothing identifying, and that is still an unknown.
    [ -n "$LPK" ] && LOCAL_OK=1
    if [ -n "$LPK" ]; then
      while IFS="$US" read -r _op _mon _st _jl _tok _shr _uh _ut _ck _msd; do
        [ -n "${_op:-}" ] || continue
        if [ "${_ck:-}" = "$LPK" ]; then
          TARGET="$_op"; LOCAL_IS_TARGET=1
          TARGET_SRC="the local node at $LOCAL_RPC (its consensus key signs for this validator)"; break
        fi
      done <<EOF
$VROWS
EOF
    fi
    # The note shows which of the three states each value is in, because `?` alone covers two cases: a
    # value this machine never reported and a value it reported in the wrong shape. They point at
    # different things to look at.
    LHI="$(_int "${LH:-}")"
    if   [ "$LHI" != "?" ];   then LOCAL_NOTE="local node: height $LHI"
    elif [ -n "${LH:-}" ];    then LOCAL_NOTE="local node: height ? (\"$LH\" is not a height)"
    else                           LOCAL_NOTE="local node: height ? (none reported)"; fi
    if   [ "$LCU" != "?" ];      then LOCAL_NOTE="$LOCAL_NOTE, catching_up=$LCU"
    elif [ -n "${LCU_RAW:-}" ];  then LOCAL_NOTE="$LOCAL_NOTE, catching_up=? (\"$LCU_RAW\" is not a boolean)"
    else                              LOCAL_NOTE="$LOCAL_NOTE, catching_up=? (none reported)"; fi
    if [ "$LCU" = "true" ] && [ "$NET_H" != "?" ] && [ "$LHI" != "?" ]; then
      LOCAL_NOTE="$LOCAL_NOTE -> $(( NET_H - LHI )) blocks behind the network"
    fi
  fi
fi

if [ -z "$TARGET" ]; then
  printf '  operator        : ?\n'
  # These states look alike and mean opposite things, so they are kept apart:
  #  - the node answered, named its key, the staking set was read and not empty, both endpoints agree
  #    on the chain, and that key is in no validator: a measured "nothing to watch here". Normal on a
  #    machine that has not run create-validator yet, and the cron mode is right to stay silent.
  #  - the node did not answer at all: this run is blind. It cannot see a jail, a slash, or that
  #    everything is fine. On a validator machine an unreachable local RPC looks exactly like the
  #    outage that gets a validator jailed.
  #  - the node answered but the staking set came back empty, or the local node names a different
  #    chain from the endpoint: "my key is in no validator" is then a statement about a document that
  #    could not have contained it. It is an unknown, and the reassuring branch would delete a standing
  #    alert.
  # Merged, the later states borrowed the reassurance of the first, and the cron mode deleted a standing
  # alert precisely when the machine went down or the endpoint went blank.
  NO_VAL_WHY=""
  if [ "$LOCAL_OK" = 1 ] && [ "$ST_TOTAL" = 0 ]; then
    NO_VAL_WHY="empty"
  elif [ "$LOCAL_OK" = 1 ] && [ -n "$LOCAL_CHAIN" ] && [ "$CHAIN_ID" != "?" ] && [ "$LOCAL_CHAIN" != "$CHAIN_ID" ]; then
    NO_VAL_WHY="otherchain"
  fi
  if [ "$LOCAL_OK" = 1 ] && [ -z "$NO_VAL_WHY" ]; then
    printf '  [!] No validator on this machine.\n'
    printf '      The local node at %s answered and its consensus key belongs to no\n' "$LOCAL_RPC"
    printf '      validator in the staking set of %s (%s validators read). That is exactly what a node\n' "$CHAIN_ID" "$ST_TOTAL"
    printf '      that has never bonded looks like, and it is normal until you run create-validator.\n'
    NO_VAL_KIND="unbonded"
  elif [ "$NO_VAL_WHY" = "empty" ]; then
    printf '  [!] The staking set came back empty — this run cannot say anything about this machine.\n'
    printf '      %s answered 200 with a validator list containing nothing. "Your key is in no\n' "$REST"
    printf '      validator" is then a statement about a document that could not have contained it:\n'
    printf '      a resyncing node, a pruned node, or a node on another chain all look like this.\n'
    NO_VAL_KIND="blind"; UNSURE=1
  elif [ "$NO_VAL_WHY" = "otherchain" ]; then
    printf '  [!] This machine is on a different chain from the endpoint — nothing here compares.\n'
    printf '      The local node at %s says it runs "%s"; the endpoint serves "%s".\n' "$LOCAL_RPC" "$LOCAL_CHAIN" "$CHAIN_ID"
    printf '      Your key would not be in this staking set even if your validator were perfectly fine,\n'
    printf '      so "no validator on this machine" measures nothing here: it reflects the mismatch.\n'
    NO_VAL_KIND="blind"; UNSURE=1
  else
    printf '  [!] This machine could not be read — the local RPC %s did not name a consensus key.\n' "$LOCAL_RPC"
    if [ "$LOCAL_ANSWERED" = 1 ]; then
      printf '      It did answer, and this run read its height from that same answer — but the answer\n'
      printf '      carried no result.validator_info.pub_key.value, so there is no key to look up. A\n'
      printf '      node that answers without naming a key and a node that is down are two different\n'
      printf '      states, and this is the first one: look at the node config, not at the network.\n'
    else
      printf '      It did not answer at all. Nothing below is about your validator, because this run\n'
      printf '      never found it. This is an unknown, not a clean bill of health: a node that stops\n'
      printf '      answering is also a node that stops signing, and enough of that is what a\n'
      printf '      downtime jail is made of.\n'
      printf '      If the node is running on another address or port, say so:\n'
      printf '        DENDRA_LOCAL_RPC=http://HOST:26657 %s\n' "$SELF_CMD"
    fi
    NO_VAL_KIND="blind"; UNSURE=1
  fi
  printf '      Name the validator explicitly:  %s dendravaloper1...\n' "$SELF_CMD"
  [ -n "$LOCAL_NOTE" ] && printf '      (%s)\n' "$LOCAL_NOTE"
  SELF_BAD=1; RC=1; NO_VAL=1
else
  [ -n "$TARGET_SRC" ] || TARGET_SRC="the command line"
  V_OP=""; V_MON=""; V_ST=""; V_JL=""; V_TOK=""; V_SHR=""; V_UH=""; V_UT=""; V_CK=""; V_MSD=""
  while IFS="$US" read -r _op _mon _st _jl _tok _shr _uh _ut _ck _msd; do
    [ "${_op:-}" = "$TARGET" ] || continue
    V_OP="$_op"; V_MON="$_mon"; V_ST="$_st"; V_JL="$_jl"; V_TOK="$_tok"; V_SHR="$_shr"
    V_UH="$_uh"; V_UT="$_ut"; V_CK="$_ck"; V_MSD="$_msd"; break
  done <<EOF
$VROWS
EOF
  printf '  operator        : %s\n' "$TARGET"
  printf '  identified from : %s\n' "$TARGET_SRC"
  [ -n "$LOCAL_NOTE" ] && printf '  %s\n' "$LOCAL_NOTE"
  if [ -z "$V_OP" ]; then
    printf '  [!] This operator address is not in the staking set of chain %s.\n' "$CHAIN_ID"
    printf '      It has never bonded here, or it belongs to another chain. Nothing else is known\n'
    printf '      about it, so nothing more is printed for it.\n'
    # Named, not unknown, and not the same as "this machine has no validator": somebody asked about a
    # specific address and the chain does not have it. Under cron that is worth an alert: the watch was
    # pointed at something that does not exist.
    SELF_BAD=1; RC=1; NO_VAL=1; NO_VAL_KIND="absent"
  else
    printf '  moniker         : %s\n' "${V_MON:-(none)}"
    case "${V_ST:-}" in
      BOND_STATUS_BONDED)    printf '  bonding status  : bonded (in the consensus set: this validator signs blocks)\n';;
      BOND_STATUS_UNBONDING) printf '  bonding status  : unbonding -> out of the consensus set: it signs nothing at all\n'; _problem "not bonded: this validator contributes nothing to consensus and earns nothing";;
      BOND_STATUS_UNBONDED)  printf '  bonding status  : unbonded -> out of the consensus set: it signs nothing at all\n'; _problem "not bonded: this validator contributes nothing to consensus and earns nothing";;
      "")                    printf '  bonding status  : ?\n'; _unknown "bonding status unreadable (field absent) — treated as unknown, not as healthy";;
      # The catch-all of a security question takes the strictest policy. A status this script does not
      # recognise (BOND_STATUS_UNSPECIFIED is the proto3 zero of the enum, so it is also what the chain
      # would omit) is judged, not only printed: otherwise the verdict would say "bonded, not jailed"
      # about a validator whose status was never read as bonded.
      *)                     printf '  bonding status  : %s\n' "$V_ST"
                             _unknown "bonding status \"$V_ST\" is not a status this script knows — an unrecognised value is an unknown, never a pass";;
    esac
    # A node that is catching up is not signing, whatever the staking module still says about it. The
    # lag was computed and printed above; the next section explains that missing blocks in a row is
    # what triggers a downtime jail.
    #
    # What this machine said about itself is judged, not only reprinted. Validated and then compared to
    # nothing, a local height of "abc" would print `height ?` and a catching_up of `1` would print
    # `catching_up=1`, each on a page ending in a healthy verdict and exit 0.
    # This applies only when this machine is the validator being reported on: elsewhere the local node
    # is not the subject, and the branch that could not find a validator here already says so.
    if [ "$LOCAL_IS_TARGET" = 1 ]; then
      case "$LCU" in
        true)  _problem "Your node is catching up ($LOCAL_NOTE): a node behind the head of the chain signs nothing, and blocks missed while it catches up count towards the jail threshold in section 3";;
        false) : ;;
        *)     _unknown "this machine did not say whether it is catching up ($LOCAL_NOTE) — a node behind the head of the chain signs nothing, so this is the question a downtime jail is made of, and it is not answered by reading anything that is not the word \"false\" as a no";;
      esac
      if [ "$LHI" = "?" ]; then
        _unknown "this machine did not report a height this script can read ($LOCAL_NOTE) — how far behind the network it is cannot be stated, so it is not stated"
      fi
    fi
    # An absent `jailed` is not read as `jailed:false` here. proto3 omits a boolean worth false, so
    # absence is formally the zero of the type, but this is the question the script exists to answer,
    # and a shape change would silently pick the reassuring answer. So an unreadable field surfaces as
    # `?` and costs a non-zero exit.
    case "${V_JL:-}" in
      true)  printf '  jailed          : yes\n'; _problem "jailed: out of the consensus set until you unjail (section 2)";;
      false) printf '  jailed          : no\n';;
      *)     printf '  jailed          : ?\n'; _unknown "jail flag unreadable — not read as \"no\": the reassuring answer is exactly the one that must not be guessed";;
    esac
    printf '  tokens          : %s udndr\n' "${V_TOK:-?}"
    # Slash detection, derived on chain. Delegating mints shares in proportion to tokens, so tokens and
    # delegator_shares only ever diverge downwards, and only through a slash. tokens < delegator_shares
    # is therefore a slash that has happened, and the gap is what it cost.
    #
    # Both operands are validated as numbers first, and an invalid one gives a third state.
    # A non-empty check is not a numeric check: `awk` reads any word it cannot parse as zero, so an
    # unreadable `tokens` would announce a full slash and an unreadable `delegator_shares` would announce
    # that nothing was ever burned, both "derived on chain". A value that is present and not a number is
    # `?`, and `?` costs a failure.
    V_TOKD="$(_dec "${V_TOK:-}")"; V_SHRD="$(_dec "${V_SHR:-}")"
    if [ "$V_TOKD" != "?" ] && [ "$V_SHRD" != "?" ]; then
      SL_T="${V_TOKD%%.*}"; [ -n "$SL_T" ] || SL_T=0     # udndr are integers; the shares carry 18 decimals
      SL_S="${V_SHRD%%.*}"; [ -n "$SL_S" ] || SL_S=0
      case "$(_bigcmp "$SL_S" "$SL_T")" in
        1)
          SL_AMT="$(_bigsub "$SL_S" "$SL_T")"
          SL_PCT="$(awk -v d="$SL_AMT" -v s="$SL_S" 'BEGIN{ if(s+0<=0){printf "?";exit} p=100*d/s; if(p>=0.001) printf "%.3f",p; else printf "<0.001" }')"
          printf '  slashed         : yes — %s udndr lost (%s %% of the original stake)\n' "$SL_AMT" "$SL_PCT"
          printf '                    derived on chain: tokens (%s) are below delegator_shares (%s),\n' "$SL_T" "$SL_S"
          printf '                    and shares are only ever burned by a slash. Slashed tokens never come back.\n'
          # A burned stake costs a failure too: otherwise an unattended watch would clear the standing alert
          # that holds the only record of the incident.
          _problem "slashed: $SL_AMT udndr ($SL_PCT %) were burned and are gone for good. This is a past event, not a fault running right now — but it is not health, and it is not something an unattended watch may quietly forget"
          ;;
        0)
          printf '  slashed         : no (tokens == delegator_shares: nothing was ever burned)\n'
          ;;
        *)
          # tokens above delegator_shares is not something the staking module produces. Saying
          # "nothing was ever burned" here would be a claim about a document that already contradicts
          # the chain it claims to come from.
          printf '  slashed         : ?  (tokens %s are above delegator_shares %s)\n' "$SL_T" "$SL_S"
          _unknown "tokens exceed delegator_shares, which the staking module never produces — this document does not describe a validator this script can reason about, so no slash claim is made in either direction"
          ;;
      esac
    else
      printf '  slashed         : ?  (tokens=%s delegator_shares=%s — not both readable as numbers)\n' \
        "${V_TOK:-(absent)}" "${V_SHR:-(absent)}"
      _unknown "the slash check could not be made — not read as \"nothing was burned\": this is the one line that answers \"did I lose money\", and it does not answer it by guessing"
    fi
    case "${V_UH:-0}" in
      0|"") : ;;
      *) printf '  unbonding       : started at height %s, ends %s\n' "$V_UH" "${V_UT:-?}";;
    esac
  fi
fi

# --- 2. Jail ------------------------------------------------------------------------------------------
# The jail lives in x/slashing, keyed by the consensus address, while everything above lives in
# x/staking, keyed by the operator address. Nothing on chain joins the two for you: the consensus
# address is sha256(consensus pubkey)[:20], which is what the bech32 valcons address encodes. That
# derivation is done here rather than asked of the operator.
_valcons_hex(){ # bech32 -> 20-byte hex. Checksum is not verified: these strings come from the chain.
  local s cs d p i c v acc=0 bits=0 out=""
  s="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"; cs="qpzry9x8gf2tvdw0s3jn54khce6mua7l"
  d="${s##*1}"; p=$(( ${#d} - 6 )); [ "$p" -gt 0 ] || return 1
  i=0
  while [ "$i" -lt "$p" ]; do
    c="${d:$i:1}"; case "$cs" in *"$c"*) v="${cs%%${c}*}"; v=${#v};; *) return 1;; esac
    acc=$(( (acc << 5) | v )); bits=$(( bits + 5 ))
    while [ "$bits" -ge 8 ]; do bits=$(( bits - 8 )); out="$out$(printf '%02x' $(( (acc >> bits) & 255 )))"; acc=$(( acc & ((1 << bits) - 1) )); done
    i=$(( i + 1 ))
  done
  printf '%s' "$out"
}
# An undecodable key must yield nothing, not the hash of nothing. `base64 -d` writes zero bytes and
# exits non-zero on bad input; piping it straight into sha256sum hides both, and what comes out is
# sha256("") = e3b0c442..., a well-formed address that matches no validator. The caller would then read
# "this key is in no signing record" and print a claim about history that nothing supports. So the
# decode is written to a file and checked before anything is hashed: no bytes, no answer.
_pk_hex(){
  [ -n "${1:-}" ] || return 1
  printf '%s' "$1" | base64 -d > "$TMP/pk.bin" 2>/dev/null || return 1
  [ -s "$TMP/pk.bin" ] || return 1
  sha256sum "$TMP/pk.bin" 2>/dev/null | cut -c1-40
}

SI_ROWS=""; SI_OK=0; SI_PARTIAL=0; SI_N=0; SI_PGT="?"
if _fetch "$REST/cosmos/slashing/v1beta1/signing_infos?pagination.limit=300" "$TMP/signing.json"; then
  SI_ROWS="$(_jrows "$TMP/signing.json" info address start_height index_offset jailed_until tombstoned missed_blocks_counter)"
  SI_OK=1
  # Truncation is measured the same two ways as for the validator list, because the sentence printed
  # when no record matches ("A validator with no signing record has never been in the consensus set")
  # is a claim about history, and a truncated document cannot support it. The request carries
  # pagination.limit=300, so past 300 validators your record may simply be on page two. next_key says
  # so directly; pagination.total announcing more records than were handed over says the same thing
  # without one.
  [ -n "$(_jget "$TMP/signing.json" pagination.next_key || printf '')" ] && SI_PARTIAL=1
  while IFS="$US" read -r _a _rest; do [ -n "${_a:-}" ] && SI_N=$(( SI_N + 1 )); done <<EOF
$SI_ROWS
EOF
  SI_PGT="$(_int "$(_jget "$TMP/signing.json" pagination.total || printf '')")"
  if [ "$SI_PGT" != "?" ] && [ "$SI_PGT" -gt "$SI_N" ]; then SI_PARTIAL=1; fi
fi
SI_ADDR=""; SI_SH=""; SI_JU=""; SI_TS=""; SI_MISS=""
# SI_LOOKED means this run actually compared this validator's consensus address against a complete list.
# It is the only state allowed to support the sentence about history below. Six different situations end
# here with an empty SI_ADDR and only one of them means what that sentence says: the list was never
# fetched (the endpoint answered 500), the list would not parse, the validator row carried no consensus
# key to look up, this machine could not derive the address from that key, the document withheld rows,
# or the validator genuinely has no record. The empty result looks the same in all six, so the reason
# is carried along rather than inferred where the sentence is printed.
SI_LOOKED=0; SI_WHY=""
if [ "$SI_OK" != 1 ]; then
  SI_WHY="the signing-info list could not be read from $REST at all (no answer, or an answer that is not JSON)"
elif [ -z "${V_CK:-}" ]; then
  SI_WHY="this validator's row carries no consensus_pubkey.key, so there was nothing to look up"
else
  MY_HEX="$(_pk_hex "$V_CK")"
  if [ -z "$MY_HEX" ]; then
    # `_pk_hex` needs base64, sha256sum and cut. They are deliberately not in the required-tools list
    # at the top: their absence affects no number, only this lookup, so it is reported here instead of
    # refusing to run on a host that would still answer every other question on this page correctly.
    SI_WHY="this machine could not derive the consensus address from the validator's key (base64, sha256sum and cut are what that derivation needs)"
  else
    while IFS="$US" read -r _a _sh _io _ju _tb _mc; do
      [ -n "${_a:-}" ] || continue
      [ "$(_valcons_hex "$_a" 2>/dev/null)" = "$MY_HEX" ] || continue
      SI_ADDR="$_a"; SI_SH="$_sh"; SI_JU="$_ju"; SI_TS="$_tb"; SI_MISS="$_mc"; break
    done <<EOF
$SI_ROWS
EOF
    if [ "$SI_PARTIAL" = 1 ]; then
      SI_WHY="the signing-info list is paginated and this run read only its first page ($SI_N rows served, pagination.total=$SI_PGT), so \"no record\" here means \"not on page one\""
    else
      SI_LOOKED=1
    fi
  fi
fi

SL_PARAMS_OK=0; P_WIN=""; P_MIN=""; P_DUR=""; P_FRAC=""; P_WINI="?"; P_MIND="?"; P_DURS="?"; P_DURD="?"; P_FRACD="?"
if _fetch "$REST/cosmos/slashing/v1beta1/params" "$TMP/slparams.json"; then
  P_WIN="$(_jget "$TMP/slparams.json" params.signed_blocks_window || printf '')"
  P_MIN="$(_jget "$TMP/slparams.json" params.min_signed_per_window || printf '')"
  P_DUR="$(_jget "$TMP/slparams.json" params.downtime_jail_duration || printf '')"
  P_FRAC="$(_jget "$TMP/slparams.json" params.slash_fraction_downtime || printf '')"
  # All four go through the same gate, not just the two that feed an arithmetic expression.
  # The window and the minimum were bounded because a wrong number there produces a wrong threshold;
  # the penalty feeds no `$(( ))`, and printed straight out of the document, a
  # slash_fraction_downtime="abc" would come out as "abc of the bonded stake", stated as a fact, under a
  # healthy verdict and an exit code of 0. They all come from one document, and a document that cannot
  # say what a jail costs is not one to build a threshold from either.
  #
  # A protobuf Duration is rendered in JSON as a count of seconds with an `s` suffix ("600s", measured
  # on this chain); Go's own "1m8s" is not that shape, and stripping every non-digit from it yields 18.
  # So it is read as a number twice, for two jobs: as a decimal to judge the document (a fractional
  # second is a legal parameter and must not raise an alarm), and as an integer for the subtraction that
  # derives the moment of a jail in section 2 (which is otherwise not derived).
  case "${P_DUR:-}" in
    *s) P_DURD="$(_dec "${P_DUR%s}")"; P_DURS="$(_int "${P_DUR%s}")";;
    *)  P_DURD="$(_dec "${P_DUR:-}")"; P_DURS="$(_int "${P_DUR:-}")";;
  esac
  # A slash fraction is a Dec bounded by the chain's own definition, not by what looks reasonable:
  # cosmos-sdk x/slashing/types/params.go validateSlashFractionDowntime refuses a negative one and
  # one above 1. A "fraction" of 40 would print "40 of the bonded stake" and mean nothing.
  P_FRACD="$(_dec "${P_FRAC:-}")"
  if [ "$P_FRACD" != "?" ] && ! awk -v d="$P_FRACD" 'BEGIN{exit !(d>=0 && d<=1)}'; then P_FRACD="?"; fi
  # The formula comes from the function that applies it, and its operands are validated too.
  # A non-empty test is not a numeric test, and `awk` turns any word it cannot parse into 0, so an
  # unreadable min_signed_per_window produced "0 blocks must be signed inside every window" and a
  # margin twice the real one, printed as a fact, with exit 0. Both operands are bounded here by the
  # chain's own definitions: the window is a count of blocks (>= 1) and min_signed_per_window is a
  # fraction of it (0 < f <= 1, cosmos-sdk x/slashing/types/params.go validateMinSignedPerWindow).
  P_WINI="$(_int "${P_WIN:-}" 1)"
  P_MIND="$(_dec "${P_MIN:-}")"
  if [ "$P_MIND" != "?" ] && ! awk -v d="$P_MIND" 'BEGIN{exit !(d>0 && d<=1)}'; then P_MIND="?"; fi
  [ "$P_WINI" != "?" ] && [ "$P_MIND" != "?" ] && SL_PARAMS_OK=1
fi

printf '\n--- 2. Jail ------------------------------------------------------------\n'
if [ -z "${V_OP:-}" ]; then
  printf '  (no validator identified — nothing to say about its jail)\n'
elif [ -z "$SI_ADDR" ]; then
  printf '  signing info    : ?  (no slashing record matched this validator consensus key)\n'
  if [ "$SI_LOOKED" = 1 ]; then
    printf '  A validator with no signing record has never been in the consensus set. If it is bonded,\n'
    printf '  this is an unknown, not a clean bill of health.\n'
  else
    # The affirmative sentence above is a claim about history, and it is not made from a lookup that
    # never happened. It is the only line on this page that says whether this validator was ever in
    # the consensus set, the information whose absence once had an operator chasing intermittent vote
    # extensions for two days.
    printf '  And this run did not establish that: %s.\n' "$SI_WHY"
    printf '  So "no record" says nothing here about whether this validator has ever been in the\n'
    printf '  consensus set, and nothing about its jail history either. Read the endpoint by hand:\n'
    printf '      %s/cosmos/slashing/v1beta1/signing_infos?pagination.limit=1000\n' "$REST"
  fi
  SELF_BAD=1; RC=1; UNSURE=1
else
  printf '  consensus addr  : %s\n' "$SI_ADDR"
  case "${SI_TS:-}" in
    true)
      printf '  tombstoned      : yes — unjail is impossible, permanently.\n'
      printf '                    Tombstoning is the penalty for double signing (two different blocks\n'
      printf '                    signed at the same height), not for downtime. This consensus key can\n'
      printf '                    never validate again on this chain. The only path is a new validator\n'
      printf '                    with a new consensus key — and check first why the old key signed\n'
      printf '                    twice (two nodes sharing one priv_validator_key is the usual cause).\n'
      _problem "tombstoned: this key is permanently barred from consensus"
      ;;
    false) : ;;
    *) printf '  tombstoned      : ?\n'; _unknown "tombstone flag unreadable — not read as \"no\"";;
  esac
  # Three states again, and only two of them may say "never jailed":
  #  - the field is absent or empty: the proto3 zero of a timestamp, never jailed. A measurement.
  #  - the field parses to epoch <= 0: the same zero, written out. A measurement.
  #  - the field is there and does not parse as a date: an unknown. This is the only line in the report
  #    about the history of jails; an unreadable value must not fall into the `else` below, which
  #    prints "has never been jailed on this chain".
  JU_E=""; JU_READ=1
  if [ -n "${SI_JU:-}" ]; then
    JU_E="$(_epoch "$SI_JU")"
    [ -n "${JU_E:-}" ] || JU_READ=0
  else
    JU_E=0
  fi
  if [ "$JU_READ" = 0 ]; then
    printf '  jail record     : ?  (jailed_until="%s" is not a date this machine can read)\n' "$SI_JU"
    _unknown "the jail history could not be read — not read as \"never jailed\": this is the only line that says whether this validator was ever jailed"
  elif [ "$JU_E" -gt 0 ] 2>/dev/null; then
    NOW="$(date -u +%s 2>/dev/null || printf '')"
    # "since when" is derived, not stored: the chain writes jailed_until = block time + the
    # downtime_jail_duration parameter it just read, so subtracting that parameter gives the moment.
    # P_DURS is that parameter read as a Duration (see the parameters block above): a whole number of
    # seconds, or nothing. Stripping every non-digit instead turns "1m8s" into 18 and reads a leading
    # zero as an octal prefix, and both produce a date that is wrong without ever looking wrong.
    if [ "$P_DURS" != "?" ]; then
      JAILED_AT="$(date -u -d "@$(( JU_E - P_DURS ))" +%FT%TZ 2>/dev/null || printf '?')"
      printf '  jailed at       : %s  (derived: jailed_until minus downtime_jail_duration=%s)\n' "$JAILED_AT" "$P_DUR"
    else
      printf '  jailed at       : ?  (downtime_jail_duration="%s" is not a plain number of seconds, so\n' "${P_DUR:-(absent)}"
      printf '                    the moment of the jail is not derived; jailed_until below is raw from the chain)\n'
    fi
    printf '  jailed until    : %s\n' "$SI_JU"
    if [ -n "${NOW:-}" ] && [ "$NOW" -ge "$JU_E" ] 2>/dev/null; then
      printf '  jail period     : over (%s ago) -> you can unjail right now.\n' "$(awk -v d="$(( NOW - JU_E ))" 'BEGIN{if(d<3600)printf "%d min",d/60; else if(d<86400)printf "%.1f h",d/3600; else printf "%.1f days",d/86400}')"
    elif [ -n "${NOW:-}" ]; then
      printf '  jail period     : still running — %s left. Unjail will be rejected before that.\n' "$(awk -v d="$(( JU_E - NOW ))" 'BEGIN{if(d<3600)printf "%d min",d/60; else printf "%.1f h",d/3600}')"
    fi
  else
    printf '  jail record     : none — this validator has never been jailed on this chain\n'
  fi
  if [ "${V_JL:-}" = "true" ] && [ "${SI_TS:-}" != "true" ]; then
    printf '\n  To unjail, run this yourself. This script never signs and never broadcasts anything.\n'
    printf '  It also costs a fee-paying transaction, so it is your key and your decision.\n'
    # A command that cannot be pasted is worse than no command: the reader blames the network for a
    # hole in the text. When the chain id could not be read it stays `?` everywhere else in this
    # report, so it is stated here rather than hidden in a command that will fail on paste.
    [ "$CHAIN_ID" = "?" ] && printf '  Note: the chain-id could not be read — replace the ? below with the CHAIN_ID from your node .env.\n'
    printf '    node running under the kit (docker):\n'
    # The -f path is absolute (or preceded by the cd that makes it true). `docker compose` resolves it
    # against the current directory, and nobody pastes a command from the directory the author had.
    # `--keyring-backend test` and `--chain-id` are required: the kit's client.toml ships
    # keyring-backend="os" and chain-id="" (measured), while the key was created under the `test`
    # backend, so without both flags this command asks an empty keyring for a key on a chain with no
    # name, and fails in a way that reads like the key is gone.
    _compose_cmd '      '
    printf '        dendrad tx slashing unjail --from validator --keyring-backend test \\\n'
    printf '        --chain-id %s --node tcp://localhost:26657 --gas auto --gas-adjustment 1.3 --yes\n' "$CHAIN_ID"
    printf '    dendrad installed on this machine:\n'
    printf '      dendrad tx slashing unjail --from <your-operator-key> --keyring-backend test \\\n'
    printf '        --chain-id %s --node %s --gas auto --gas-adjustment 1.3 --yes\n' "$CHAIN_ID" "${DENDRA_NODE:-tcp://$(_host_of "$RPC"):26657}"
    printf '    Replace `validator` with the key name you used at create-validator time.\n'
    printf '    The node must be up and caught up before you run this, and that is not a formality:\n'
    printf '      · `docker compose exec` attaches to a running container. On a stopped one it answers\n'
    printf '        `service "node" is not running` and nothing else — that message is about docker, not\n'
    printf '        about your key or your validator. Start the node first (`... up -d`).\n'
    printf '      · unjailing a node that is still behind puts it straight back in jail: it will miss the\n'
    printf '        next window exactly as it missed the last one.\n'
    printf '    To see what it would send without sending anything, append --generate-only: it prints the\n'
    printf '    MsgUnjail and broadcasts nothing.\n'
    printf '    Then re-run this script: bonding status must return to bonded.\n'
  fi
fi

# --- 3. Margin before the next jail -------------------------------------------------------------------
# The threshold is taken from the function that applies it, not from a neighbouring sentence:
# cosmos-sdk x/slashing/keeper/infractions.go computes maxMissed = signedBlocksWindow - minSignedPerWindow
# and jails when MissedBlocksCounter > maxMissed, with minSignedPerWindow = round(min_signed_per_window *
# signedBlocksWindow) (x/slashing/keeper/params.go). Both parameters are read from this chain above; none
# of the numbers below is copied from any document.
printf '\n--- 3. Margin before the next jail -------------------------------------\n'
if [ "$SL_PARAMS_OK" != 1 ]; then
  printf '  slashing params : ?  (signed_blocks_window="%s", min_signed_per_window="%s")\n' \
    "${P_WIN:-(absent)}" "${P_MIN:-(absent)}"
  printf '                    One of them is not a number this script will build a threshold out of, so\n'
  printf '                    the jail threshold cannot be stated, and it is not stated.\n'
  SELF_BAD=1; RC=1; UNSURE=1
else
  MINSIGNED="$(awk -v d="$P_MIND" -v w="$P_WINI" 'BEGIN{printf "%d", int(d*w+0.5)}')"
  MINSIGNED="$(_int "$MINSIGNED" 0 "$P_WINI")"
  if [ "$MINSIGNED" = "?" ]; then
    printf '  slashing params : ?  (the derived minimum of signed blocks fell outside 0..%s)\n' "$P_WINI"
    SELF_BAD=1; RC=1; UNSURE=1
  else
  MAXMISS=$(( P_WINI - MINSIGNED ))
  printf '  signed_blocks_window  : %s blocks   (read from the chain)\n' "$P_WINI"
  printf '  min_signed_per_window : %s -> %s blocks must be signed inside every window\n' "$P_MIND" "$MINSIGNED"
  printf '  jail triggers when    : missed_blocks_counter goes above %s, so %s missed blocks inside one window\n' "$MAXMISS" "$(( MAXMISS + 1 ))"
  printf '  The rule in plain terms: missing %s blocks in a row is enough to be jailed and slashed —\n' "$(( MAXMISS + 1 ))"
  printf '                          that is %s. A reboot, a lost link, a\n' "$(_mins $(( MAXMISS + 1 )))"
  printf '                          full disk. It is not a question of if, only of when.\n'
  # Three states on each, printed in three different shapes, so that a value, a value that could not
  # be read, and a field that was never served never look alike. Absent is the proto3 zero of its type
  # and is reported as absent rather than shown as a bare 0: "0 of the bonded stake" reads as "downtime
  # costs me nothing", which nobody measured.
  P_FRAC_TXT="?"; P_DUR_TXT="?"
  if   [ -z "${P_FRAC:-}" ];  then P_FRAC_TXT="0 (the field is absent: the proto3 zero of its type)"
  elif [ "$P_FRACD" != "?" ]; then P_FRAC_TXT="$P_FRACD"
  else _unknown "slash_fraction_downtime=\"$P_FRAC\" is not a fraction of stake between 0 and 1 — what a jail costs is not stated by guessing, and it comes from the same document as the threshold above"; fi
  if   [ -z "${P_DUR:-}" ];   then P_DUR_TXT="0s (the field is absent: the proto3 zero of its type)"
  elif [ "$P_DURD" != "?" ];  then P_DUR_TXT="$P_DUR"
  else _unknown "downtime_jail_duration=\"$P_DUR\" is not a duration in seconds — how long a jail lasts is not stated by guessing, and it comes from the same document as the threshold above"; fi
  printf '  penalty               : %s of the bonded stake (slash_fraction_downtime) + jail for %s,\n' "$P_FRAC_TXT" "$P_DUR_TXT"
  printf '                          and removal from the consensus set until you unjail by hand.\n'
  if [ -n "${SI_ADDR:-}" ]; then
    if [ -z "${SI_MISS:-}" ]; then
      printf '  your missed count     : ?\n'; _unknown "missed_blocks_counter unreadable — no margin can be claimed, so none is"
    else
      # Bounded by what the chain guarantees and by what this shell can compare, not by the window.
      # Below: MissedBlocksCounter is a count (cosmos-sdk x/slashing ValidatorSigningInfo), so a negative
      # value is a document the chain never wrote, and it inflated the margin. Above: past 18 digits
      # shell arithmetic wraps silently, and a counter of exactly 2^64 came back as a full margin. Those
      # two bounds are properties of the type and of the arithmetic, and they always hold.
      #
      # The window is not a third bound, because it is governable. signed_blocks_window is a module
      # parameter; lowering it does not rewrite the counters already stored, so a counter above the
      # window this endpoint publishes right now is a state the chain produces, not a corrupt one.
      # Refusing it as unreadable turned the most dangerous position (past the whole allowance) into a
      # `?` on every run. It is judged instead, on the alarming side: the margin clamps to zero and the
      # warning below fires.
      SI_MISSI="$(_int "$SI_MISS" 0)"
      if [ "$SI_MISSI" = "?" ]; then
        printf '  your missed count     : ?  ("%s" is not a count of blocks this script can compare)\n' "$SI_MISS"
        _unknown "missed_blocks_counter is not a non-negative count — no margin can be claimed, so none is"
      else
      printf '  your missed count     : %s / %s in the current window\n' "$SI_MISSI" "$P_WINI"
      LEFT=$(( MAXMISS - SI_MISSI + 1 ))
      [ "$LEFT" -lt 0 ] && LEFT=0
      printf '  your margin           : %s more consecutive missed blocks would jail you (%s)\n' "$LEFT" "$(_mins "$LEFT")"
      if [ "$SI_MISSI" -gt "$P_WINI" ]; then
        printf '                          (this counter is above the %s-block window published above.\n' "$P_WINI"
        printf '                           signed_blocks_window is a module parameter and the counters already\n'
        printf '                           stored are not rewritten when it changes, so a larger window was in\n'
        printf '                           force when this one was written. The margin is zero either way, and\n'
        printf '                           the chain compares this counter against the current parameters.)\n'
      fi
      if [ "${V_JL:-}" = "true" ]; then
        printf '                          (this counter was reset when the jail was applied; it only starts\n'
        printf '                           moving again once you are back in the consensus set)\n'
      fi
      # The margin is used, not only printed. Computed and then compared to nothing, this section could
      # only help a human who was already looking, so an unattended watch could report only the jail,
      # that is, the loss after it had been taken.
      # The half is this script's choice, not the chain's. The chain publishes one number here, the
      # allowance (maxMissed); it defines no warning level, and the output says so.
      WARN_AT=$(( MAXMISS / 2 ))
      printf '  watch threshold       : this script reports a problem above %s missed blocks — half of the\n' "$WARN_AT"
      printf '                          %s the chain allows. The half is this script'"'"'s choice; only the %s\n' "$MAXMISS" "$MAXMISS"
      printf '                          comes from consensus.\n'
      if [ "$SI_MISSI" -gt "$WARN_AT" ]; then
        _problem "missing blocks now: $SI_MISSI of the $MAXMISS the chain allows are already spent, $LEFT left ($(_mins "$LEFT")). This is the state that ends in a jail and a slash, and it is reported before the loss rather than after it"
      fi
      fi
    fi
    SI_SHI="$(_int "${SI_SH:-}")"
    if [ "$SI_SHI" != "?" ] && [ "$NET_H" != "?" ]; then
      MINH=$(( SI_SHI + P_WINI ))
      [ "$NET_H" -le "$MINH" ] 2>/dev/null && printf '  grace                 : no downtime jail can apply before height %s (start_height + window)\n' "$MINH"
    fi
  fi
  fi
fi

# --- 4. Network ---------------------------------------------------------------------------------------
# (the census itself was taken right after the validator rows were read, because section 1 needs it)
printf '\n--- 4. Network ---------------------------------------------------------\n'
printf '  last block      : %s\n' "$CHAIN_AGE_TXT"
if [ "$CHAIN_STALLED" = 1 ]; then
  _problem "The chain is not producing blocks. The last block is $CHAIN_AGE s old, past the bound above. Every other number in this report is read from the chain, so on a stopped chain they all stay green: 'not catching up' means there is nothing to catch up with, and the missed-block counter freezes because missed blocks are only counted on blocks that happen. Look at the other validators, not at this one -- when no single validator holds two thirds on its own, one node being down can stop the chain for everyone, and it restarts when that node comes back. Whether that is the case here is a reading, not a constant: compare each 'tokens' from /cosmos/staking/v1beta1/validators against two thirds of the bonded pool."
elif [ -z "${CHAIN_AGE:-}" ]; then
  _net_unknown "the age of the last block could not be measured, so this run cannot tell a producing chain from a stopped one -- and a stopped chain leaves every other figure in this report looking perfectly normal"
fi
if [ "$CONS_N" = "?" ]; then
  printf '  consensus set   : ?   <- CometBFT %s/validators: who actually signs blocks\n' "$RPC"
  if [ "$CONS_SEEN" = 1 ] && [ -n "$CONS_RAW" ]; then
    _net_unknown "the consensus set size came back as \"$CONS_RAW\", which is not a count of validators (and a count of zero would mean the chain signs nothing at all) — the number that decides whether anything signs is unknown, so this report is not a clean bill of health"
  else
    _net_unknown "the consensus set size could not be read from $RPC/validators — the number that decides whether anything signs at all is unknown, so this report is not a clean bill of health"
  fi
else
  printf '  consensus set   : %s validator(s)   <- CometBFT /validators: who actually signs blocks\n' "$CONS_N"
fi
printf '  staking set     : %s validator(s) (%s bonded, %s jailed)\n' "$ST_TOTAL" "$ST_BONDED" "$ST_JAILED"
printf '                    The two sets are not the same. A jailed or unbonding validator stays listed\n'
printf '                    in x/staking and signs nothing. Counting the staking set to answer "how many\n'
printf '                    validators does this network have" gives an answer that is never wrong on\n'
printf '                    its own terms and never true about consensus.\n'
# proto3 omits every field worth its zero, and this endpoint relies on that heavily.
# chain/x/jobs/keeper/query_committee_seed_health.go only assigns latest_contributors and
# latest_seed_height inside the branch that found a seed at height h or h-1, so on the day this
# section matters most (the seed has stopped) the JSON has no latest_contributors at all. Reading an
# absent uint64 as the zero of its type is therefore correct here: zero contributors. The floor is the
# opposite case: CommitteeMinVrfContributors is only assigned inside
# `if p, err := q.k.Params.Get(ctx); err == nil`, so an absent floor means the parameters could not be
# loaded, and a floor of zero is not a floor. One is a measurement, the other an unknown, and neither
# may skip the comparison.
#
# _seedint FILE FIELD prints the integer, `0` when the field is absent (proto3 zero), `?` when the document
# could not be walked or the value is not a run of digits. `?` is never compared and never passes.
_seedint(){
  local v rc
  v="$(_jget "$1" "$2")"; rc=$?
  case "$rc" in
    0) _int "$v";;      # present: a run of digits, or `?`, through the same reader every other number uses
    4) printf '0';;
    *) printf '?';;
  esac
}
if [ "$SEED_OK" = 1 ]; then
  SEED_C="$(_seedint "$TMP/seed.json" latest_contributors)"
  SEED_MIN="$(_seedint "$TMP/seed.json" committee_min_vrf_contributors)"
  SEED_H="$(_seedint "$TMP/seed.json" latest_seed_height)"
  SEED_RECENT="$(_jget "$TMP/seed.json" has_recent_seed)"; _src=$?
  case "$_src" in
    4) SEED_RECENT="false";;                                   # proto3 omits a bool worth false
    # The type is checked, not only the word. `_jget` prints a scalar, so the string "true" and the
    # boolean true come back identical while the number 1 is refused, an asymmetry that favours the
    # reassuring answer.
    0) case "$(_jtype "$TMP/seed.json" has_recent_seed)" in boolean) : ;; *) SEED_RECENT="?";; esac;;
    *) SEED_RECENT="?";;
  esac
  # There are two bars, and this report reads both.
  # `committeeBaseSeedSourced` (chain/x/jobs/keeper/decentralized_seed.go) refuses the seed under
  # either of them: the head count (`committee_min_vrf_contributors`, governed) and the contributor
  # power share (`MinVrfContributorPowerBps`, a Go constant = 6667 bps). A set where the count is met
  # but a large contributor stays silent falls under the second bar (legacy fallback, anti-grinding
  # inactive), and this report still announced the floor as held. An absent field is `?`, never a
  # pass (a security criterion never defaults; see the zero rule).
  #
  # The second bar is judged exactly like the first: a miss sets NET_BAD and RC, so it never sits under
  # a healthy verdict and --cron never clears the standing alert on it.
  #
  # The absent field is the third answer, not a zero. `_seedint` maps an absent uint64 to the proto3
  # zero, which is right for a count (`latest_contributors`: absent means nobody contributed) and wrong
  # here, because zero would then be announced as a measured legacy fallback when the field may simply
  # not exist in the answering binary. `_jget` is read directly so that absent stays absent.
  SEED_PW="$(_jget "$TMP/seed.json" contributor_power_bps)" || SEED_PW="?"
  SEED_PW="$(_int "$SEED_PW")"
  printf '  committee seed  : latest_contributors=%s / committee_min_vrf_contributors=%s (seed height %s, has_recent_seed=%s)\n' \
    "$SEED_C" "$SEED_MIN" "$SEED_H" "$SEED_RECENT"
  printf '  seed power      : contributor_power_bps=%s   (second bar: >= 6667, or the draw is legacy)\n' "$SEED_PW"
  if [ "$SEED_PW" = "?" ]; then
    printf '\n  !! The second bar could not be established: the answer carries no contributor_power_bps,\n'
    printf '     and this run does not choose which reading applies. The chain writes that field only\n'
    printf '     for a height that produced a seed, and only from the power stored with it, so an\n'
    printf '     omission is one of three things: no seed at that height (the seed lines say so), a\n'
    printf '     seed taken from the legacy source because the power bar was missed (anti-grinding\n'
    printf '     inactive), or a node running a binary older than the field. None of the three is a\n'
    printf '     pass, so none is claimed: a contributor count sitting at its floor does not prove the\n'
    printf '     seed is decentralised. Upgrade the node that answered, or read the bar yourself from\n'
    printf '     its log line "decentralized VRF seed".\n'
    NET_BAD=1; RC=1; UNSURE=1
  # The pass is the positive test, so a comparison that cannot be made lands on the refusing side
  # rather than falling through to a silent "cleared".
  elif [ "$SEED_PW" -ge 6667 ] 2>/dev/null; then
    :
  else
    printf '\n  !! Contributor power under the second bar (%s < 6667 bps): the committee draw fell back to\n' "$SEED_PW"
    printf '     the legacy seed source and anti-grinding is inactive, even though the contributor count\n'
    printf '     above is met. The two bars are independent -- a set that meets the count while a heavy\n'
    printf '     validator stays silent fails this one -- so the count alone never proves the seed is\n'
    printf '     decentralised, and audits drawn now are drawn from a source a proposer can influence.\n'
    printf '     The validators carrying the missing power have not anchored a VRF key, or run without\n'
    printf '     DENDRA_VRF_KEY_FILE set (deploy/join.sh --validator, steps 4 and 5).\n'
    NET_BAD=1; RC=1
  fi

  # The invariant this report states in words is also checked in numbers.
  # "The contributor count cannot exceed the size of the consensus set" is written a few lines below,
  # but only inside the branch that fires when the seed is under its floor. A document claiming more VRF
  # contributors than there are signers describes a chain that cannot exist; both numbers are already
  # in hand, so the comparison costs nothing.
  if [ "$SEED_C" != "?" ] && [ "$CONS_N" != "?" ] && [ "$SEED_C" -gt "$CONS_N" ]; then
    printf '\n  !! These two numbers cannot both be true: %s VRF contributors are claimed for %s\n' "$SEED_C" "$CONS_N"
    printf '     validator(s) in the consensus set. A contributor emits its share inside a vote\n'
    printf '     extension, so it must be signing — the count cannot exceed the set.\n'
    printf '     They come from two documents read a moment apart (%s and %s), so a difference of\n' "$REST" "$RPC"
    printf '     one can be a validator that left between the two reads; a larger one cannot. Either\n'
    printf '     way this run has not established that the seed is above its floor, so it does not\n'
    printf '     say that it is.\n'
    NET_BAD=1; RC=1; UNSURE=1
  fi

  # --- the floor is compared on every run, and every branch reports something.
  if [ "$SEED_MIN" = "?" ] || ! [ "$SEED_MIN" -ge 1 ] 2>/dev/null; then
    printf '\n  !! The seed floor itself could not be established (committee_min_vrf_contributors=%s).\n' "$SEED_MIN"
    printf '     A floor of zero is not a floor: the chain leaves that field at its proto3 zero when the\n'
    printf '     jobs module parameters cannot be loaded. Comparing a count against it would turn an\n'
    printf '     unknown into a pass, so no comparison is claimed and this run is not a clean bill of\n'
    printf '     health. Read the endpoint by hand:  %s/dendra/jobs/v1/committee_seed_health\n' "$REST"
    NET_BAD=1; RC=1; UNSURE=1
  elif [ "$SEED_C" = "?" ]; then
    printf '\n  !! The contributor count could not be read, so the floor of %s was never checked. That\n' "$SEED_MIN"
    printf '     is reported as a failure, never as health: "I do not know" is not "everything is fine",\n'
    printf '     and the reassuring reading is exactly the one that must not be guessed here.\n'
    NET_BAD=1; RC=1; UNSURE=1
  elif [ "$SEED_C" -lt "$SEED_MIN" ]; then
    # `committee_seed_health` shows a contributor count falling below its floor and stops there, the
    # same picture whether vote extensions are failing or the validators that would produce them are
    # jailed. The likely cause is named here, with the count of jailed validators, so nobody has to
    # guess.
    printf '\n  !! Committee seed below its floor (%s < %s): audits cannot be drawn, so verified work is\n' "$SEED_C" "$SEED_MIN"
    printf '     not paid out.\n'
    NET_BAD=1; RC=1
    if [ "$ST_JAILED" -gt 0 ]; then
      printf '     Cause: %s of the %s validators in the staking set are jailed. A jailed validator is out\n' "$ST_JAILED" "$ST_TOTAL"
      printf '     of the consensus set: it signs no block, therefore it emits no vote extension, therefore\n'
      printf '     it contributes no VRF share. The contributor count cannot exceed the size of the\n'
      printf '     consensus set (%s). This is a jail, not an intermittent vote-extension failure —\n' "$CONS_N"
      printf '     the two are indistinguishable from the seed endpoint alone, which is why it is stated here.\n'
      printf '     Jailed validators (each one needs its own operator to unjail):\n'
      printf '%s' "$JAILED_LIST" | while IFS="$US" read -r _j _jm; do [ -n "${_j:-}" ] && printf '       %s  %s\n' "$_j" "$_jm"; done
      printf '     Once they are unjailed and signing again, the contributor count follows on its own.\n'
    else
      printf '     No validator is jailed, so the jail explanation does not apply here. The remaining\n'
      printf '     cause is a bonded validator that signs blocks but contributes no VRF share: its VRF key\n'
      printf '     is anchored on chain but the node was never restarted with DENDRA_VRF_KEY_FILE set\n'
      printf '     (see deploy/join.sh --validator, steps 4 and 5). Such a node looks perfectly healthy.\n'
    fi
  fi

  # --- has_recent_seed is judged, not merely displayed. A chain that stopped producing seeds hours
  # ago, with a stale contributor count still above the floor, would otherwise come out healthy. "Are
  # there enough contributors" and "is there a seed at all" are independent questions, and both are
  # asked here.
  case "$SEED_RECENT" in
    true) : ;;
    false)
      printf '\n  !! No recent committee seed (has_recent_seed=false): no decentralized seed was written at\n'
      printf '     the current height or the one before it. The committee randomness has stopped being\n'
      printf '     produced — audits cannot be drawn, whatever contributor count is shown above, because\n'
      printf '     that count belongs to whichever older height last managed to produce a seed.\n'
      NET_BAD=1; RC=1;;
    *)
      printf '\n  !! has_recent_seed could not be read (%s) — not read as "yes".\n' "$SEED_RECENT"
      NET_BAD=1; RC=1; UNSURE=1;;
  esac
else
  printf '  committee seed  : ?  (committee_seed_health unreachable — its floor was not checked)\n'
  NET_BAD=1; RC=1; UNSURE=1
fi

# --- Verdict ------------------------------------------------------------------------------------------
printf '\n========================================================================\n'
if [ "$RC" = 0 ]; then
  printf '  Verdict: healthy — bonded, not jailed, the committee seed is fresh and at or above its floor.\n'
elif [ "$NO_VAL_KIND" = "blind" ]; then
  # Never "nothing is broken on this machine": this run never reached the machine, and a reassuring
  # wording would give an unreachable node the same verdict as one that answered with nothing to report.
  # It does not name a cause either. Printing "the local RPC did not answer" here would cover four
  # different states, one of which is a local node that answered, and send the operator to debug an
  # address that works. The cause is stated once, in section 1, by the branch that knows which state
  # it was.
  printf '  Verdict: unknown — this machine could not be read. This run never found the validator it was\n'
  printf '           meant to watch (section 1 says why), so it cannot say whether your validator is\n'
  printf '           jailed, slashed, or perfectly fine. Treat it as a node that is not signing until you\n'
  printf '           have checked, because that is what it looks like from the outside.\n'
elif [ "$NO_VAL_KIND" = "absent" ]; then
  # Not "nothing is broken on this machine": naming an address on the command line stops this run from
  # looking at the local node, so this machine was never read. The measured fact is about the address,
  # and it is the only thing reported.
  printf '  Verdict: that address is not on this chain — the staking set was read and it does not\n'
  printf '           contain the operator you named. This says nothing about this machine: naming an\n'
  printf '           address is what stops this run from looking at the local node, so it did not.\n'
  printf '           Check the address, or pass no argument and let the local node identify itself.\n'
elif [ "$NO_VAL" = 1 ] && [ "$NET_BAD" = 0 ]; then
  printf '  Verdict: no validator here — nothing is broken on this machine, there is simply nothing to\n'
  printf '           watch yet. This becomes a real report the moment you bond (create-validator).\n'
elif [ "$NO_VAL" = 1 ]; then
  printf '  Verdict: no validator here — nothing to watch on this machine yet. The network problem in\n'
  printf '           section 4 is real and it affects everyone on this chain, miners included.\n'
elif [ "$SELF_BAD" = 1 ] && [ "$NET_BAD" = 1 ]; then
  printf '  Verdict: problem — with your validator and with the network. See the lines marked !!\n'
elif [ "$SELF_BAD" = 1 ]; then
  printf '  Verdict: problem — with your validator. See the lines marked !! above.\n'
  # A past loss and a running fault are both "not health", and they call for different actions.
  # An operator who has just unjailed is bonded, signing, and permanently poorer. Saying only "problem"
  # would send them hunting for something to fix; saying "healthy" would let an unattended watch delete
  # the only record that the loss happened. So it is named for what it is.
  if [ "${SL_AMT:-}" != "" ] && [ "${V_JL:-}" = "false" ] && [ "${V_ST:-}" = "BOND_STATUS_BONDED" ]; then
    printf '           This validator is bonded and not jailed right now: the !! above is a stake that was\n'
    printf '           burned in the past and cannot be recovered, not a fault you can act on today.\n'
  fi
else
  printf '  Verdict: your validator is fine, the network is not — your node is bonded and not jailed;\n'
  printf '           what is failing is network-wide (section 4). Nothing to fix on this machine.\n'
fi
# The footer names every source and how it was chosen. "Read from <url>" alone was true and useless:
# an operator who exports DENDRA_REST and reads a healthy report does not re-read the URL on the last
# line to check it is the one they asked for. Saying where the endpoint came from makes a report about
# the wrong node visible.
#
# It names every one of them. Crediting the REST endpoint with "every number" while the chain id, the
# height, the consensus set size and the block timestamps came from a different host was false, and
# the exclusivity DENDRA_REST promises does not extend to DENDRA_RPC.
printf '  Where the numbers came from — no endpoint is credited with numbers it did not serve:\n'
printf '    staking set, slashing records and params, committee seed : %s\n' "$REST"
printf '        [%s]\n' "$REST_SRC"
printf '    chain-id, consensus set, block times                     : %s\n' "$RPC"
printf '        [%s]\n' "$RPC_SRC"
printf '    the network height                                       : %s\n' "${NET_H_SRC:-(no endpoint served one)}"
if [ "$LOCAL_ANSWERED" = 1 ]; then
  printf '    which validator is this machine                          : %s\n' "$LOCAL_RPC"
  printf '        [DENDRA_LOCAL_RPC, or 127.0.0.1:26657 by default]\n'
fi
if [ -n "${TARGET:-}" ]; then printf '  Re-run: %s %s\n' "$SELF_CMD" "$TARGET"; else printf '  Re-run: %s\n' "$SELF_CMD"; fi
printf '========================================================================\n'
return "$RC"
}

# --- cron mode ----------------------------------------------------------------------------------------
# A log that grows every hour is another file nobody reads. So the cron mode writes nothing while
# healthy, and it removes a previous alert when, and only when, it has positively measured that there is
# nothing left to report. "Does this file exist" is then a question an operator can answer at a glance.
#
# Cron asks a different question from a human at a keyboard. Typed by hand, "no validator on this
# machine" is a non-answer and earns exit 1. Under cron, a node that answered and simply never bonded
# needs nobody: alerting on it would put a file there on day one, before the operator has bonded, and
# an alert that is on by default gets ignored on the day it is finally true.
# The exit code stays the same in both modes; only whether an alert is raised differs.
# The report is written to a file rather than captured in a command substitution on purpose: a
# substitution runs in a subshell, and the flags below would be set in a shell that then disappears.
#
# Removing the alert is the only destructive thing this script does, and this branch guards the moment
# it would do the most harm. "No validator identified" covers two opposite states, and if the
# unreachable one (the local node is down) took the quiet branch, the alert would be cleared: a jailed
# operator whose machine then failed would come back to an empty directory, in exactly the scenario that
# produces the next jail.
# Four outcomes, and only a positive all-clear removes anything:
#   - measured healthy                 -> remove
#   - measured "node up, never bonded" -> remove          (and the network must be fine too)
#   - a named problem                  -> write the full report
#   - anything this run could not read -> never remove; and if the run was blind (it could not even
#     find the validator) and an alert is already standing, that alert is left untouched rather than
#     overwritten by a report that knows less than the one already on disk.
#
# Writing and removing report failures the same way. Left alone, writing fails loudly (bash's own
# message, from the redirection) while removing fails silently (`2>/dev/null || true`), the wrong way
# round, since removing is the only irreversible act here. Both say what happened, in this script's own
# words, and the removal is written `rm -f --` so a path beginning with '-' is a path.
_alert_write(){   # $1 = the report body to keep
  local now first
  now="$(date -u +%FT%TZ 2>/dev/null || printf '?')"
  first="$now"
  # "detected at" is kept across ticks: rewritten on every tick, it would date a validator jailed since
  # Monday a minute ago, and how long it has been going on is the number the operator needs.
  # Only the timestamp is taken back, not the rest of the line. Reading everything after the colon
  # carries the explanatory suffix along, and the next write appends its own: the line would grow by
  # one parenthesis per tick.
  if [ -f "$CRON_FILE" ]; then
    local f; f="$(sed -n 's/^  first seen at : \([^ ][^ ]*\).*$/\1/p' "$CRON_FILE" 2>/dev/null | head -1)"
    [ -n "${f:-}" ] && first="$f"
  fi
  { printf 'Dendra validator health — a problem is standing\n'
    printf '  first seen at : %s   (kept from the run that first saw it)\n' "$first"
    printf '  last checked  : %s\n\n' "$now"
    cat "$1"; } > "$TMP/alert.txt" 2>/dev/null || {
      printf '%s\n' "[health] a problem was detected and this watch could not even assemble its alert." >&9
      return 1; }
  if ( cat "$TMP/alert.txt" > "$CRON_FILE" ) 2>/dev/null; then return 0; fi
  printf '%s\n' "[health] a problem was detected and this watch could not write $CRON_FILE." >&9
  printf '%s\n' "[health] There is now nowhere for it to be seen: check the path and the permissions of" >&9
  printf '%s\n' "[health] its directory. Until then this watch is silent by accident, not by health." >&9
  return 1
}
if [ -n "$CRON_FILE" ]; then
  REPORT_STARTED=1
  main_report > "$TMP/report.txt" 2>&1; RC2=$?
  REPORT_DONE=1
  ALL_CLEAR=0
  [ "$RC2" -eq 0 ] && [ "$UNSURE" = 0 ] && ALL_CLEAR=1
  [ "$NO_VAL_KIND" = "unbonded" ] && [ "$NET_BAD" = 0 ] && [ "$UNSURE" = 0 ] && ALL_CLEAR=1
  if [ "$ALL_CLEAR" = 1 ]; then
    if [ -e "$CRON_FILE" ]; then
      rm -f -- "$CRON_FILE" 2>/dev/null || true
      if [ -e "$CRON_FILE" ]; then
        printf '%s\n' "[health] this run measured an all-clear but could not remove $CRON_FILE." >&9
        printf '%s\n' "[health] Left alone it would keep reading as a problem that is no longer there." >&9
        ( printf 'Dendra validator health — this alert is stale\n\n  The run at %s measured an all-clear and could not delete this file.\n  Remove it by hand; the watch will recreate it if a real problem returns.\n' \
            "$(date -u +%FT%TZ 2>/dev/null || printf '?')" > "$CRON_FILE" ) 2>/dev/null \
          || printf '%s\n' "[health] and it could not be rewritten either — remove $CRON_FILE by hand." >&9
      fi
    fi
  elif [ "$NO_VAL_KIND" = "blind" ] && [ -f "$CRON_FILE" ]; then
    printf '%s\n' "[health] this run could not read the local node; the standing alert $CRON_FILE is left as it is." >&9
  else
    _alert_write "$TMP/report.txt" || true
  fi
  exit "$RC2"
fi

# The same safety net applies without --cron: an abort here reaches a human, but only as bash's own
# `unbound variable`, a line that names a shell internal and nothing the operator can act on.
REPORT_STARTED=1
main_report; RC1=$?          # captured before anything else runs: `exit $?` after another assignment
REPORT_DONE=1                # would exit with the status of that assignment, which is always 0
exit "$RC1"
