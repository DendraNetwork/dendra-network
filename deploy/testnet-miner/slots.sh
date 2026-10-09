#!/usr/bin/env bash
# slots.sh — ONE MINER IDENTITY PER NVIDIA CARD: the one place that knows what a "slot" is.
#
# A slot is one miner identity, bound to one card by the card's UUID.
#   slot 0   this kit's installation as it always was: compose project `dendra-miner` (the `name:` of
#            docker-compose.yml), the kit's .env, its docker-compose.override.yml, the volumes
#            dendra-miner_*. It is NEVER renamed: renaming a project orphans the volume that holds a staked
#            key, and the identity with it.
#   slot k   (k >= 1) compose project `dendra-miner-g<k>`, machine files gpu/<k>/.env and gpu/<k>/override.yml
#            next to this file (directory 0700, files 0600: the env carries the relay token), its own
#            miner-keys volume, and the model store of slot 0 mounted as an external volume.
# deploy/join.sh --gpus writes the slots. Everything that acts on one identity goes THROUGH this file
# (join.sh, publish-capacity.sh, and the tools that start, stop, check or leave one identity): a second
# implementation of "which project, which env, which files" is how two tools end up acting on two
# different identities while both believe they act on the same one.
#
# THREE FACTS, MEASURED ON COMPOSE (v5.3.0), DECIDE THE FORM OF A SLOT'S COMMAND:
#   (a) a compose command that names a slot k's project with -p and nothing else reads the kit's .env and its
#       docker-compose.override.yml -- slot 0's identity, card, judge profile and judge volume. So the command of a slot k always
#       carries ALL of it: -p, --env-file gpu/<k>/.env, and one -f per file of the slot. `slot_argv` builds
#       it; nothing else spells a slot k's project name on a compose command line.
#   (b) THE ENVIRONMENT OF THE PROCESS WINS OVER --env-file. deploy/join.sh exports DENDRA_MODEL_ID; an
#       operator may have MINER_ID or COMPOSE_PROFILES in their shell: a slot k would then take slot 0's
#       model, identity or judge profile, through --env-file and the -f flags. So `slot_compose` removes
#       from the environment EVERY variable the slot's compose files interpolate -- the list is DERIVED from
#       the `${NAME` forms of those files, never typed here -- and every COMPOSE_* variable. The Docker
#       variables (DOCKER_HOST, DOCKER_CONTEXT, DOCKER_CONFIG) stay: they say which engine, not what runs.
#       The image a slot k starts is the one its env file names (join.sh writes there the image slot 0
#       actually started), so DENDRA_MINER_IMAGE is removed with the others. The same holds for SLOT 0, read
#       from the kit's own .env: an exported COMPOSE_PROJECT_NAME renames it (another key volume), a slot k's
#       MINER_ID or COMPOSE_FILE exported in the shell gives it that identity or that card.
#   (c) A KEY MISSING FROM AN ENV FILE IS A WARNING TO COMPOSE (exit 0, the value empty): a slot k would
#       start with an empty MINER_ID. So `slot_check` reads, before anything is started or created, every
#       variable the compose files use WITHOUT a default -- derived the same way -- plus the slot's own
#       lines, and refuses (exit 2) when one is empty. A key absent from a slot's env is a refusal, never
#       a default.
# NEVER --project-directory: compose records the kit as the containers' working_dir label, and that label
# is how a container is told apart from a clone elsewhere on the same machine with the same project name.
#
# Sourced:  . deploy/testnet-miner/slots.sh      (defines functions; does nothing else)
# CLI:      bash deploy/testnet-miner/slots.sh list          # k|project|state|uuid|miner_id|dir per slot (dir last)
#           bash deploy/testnet-miner/slots.sh argv <k>      # slot k's compose command, one word per line
#           bash deploy/testnet-miner/slots.sh dir <k>       # the directory of slot k's machine files
#           bash deploy/testnet-miner/slots.sh run <k> ...   # run it from the kit: run 1 up -d --no-build
#           bash deploy/testnet-miner/slots.sh check <k>     # exit 0, or 2 with the empty keys named
#           bash deploy/testnet-miner/slots.sh pin <k>       # pinned | not-pinned | unknown, and why
#           bash deploy/testnet-miner/slots.sh registration <k>   # registered | deferred:<why> | refused | mismatch | pending | unread
# Exit codes: 0 done; 2 refused (a slot that does not exist, a key missing, a malformed file); 3 not
# measured (an unreadable file, an engine that does not answer) -- never folded into "no slot".

SLOTS_KIT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
SLOT_UUID_RE='^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

slots_kit(){ printf '%s\n' "$SLOTS_KIT"; }

# _slots_tools -> 0 when the tools this file reads files with are on this host; otherwise exit 3 and the missing
# one named. Without them a reading comes back EMPTY, and empty means "no slot", "no key missing", "nothing to
# remove from the environment": three answers that look like a result and are a failure. Measured: a host
# without `basename` made every slot k vanish from the list, and the uninstaller then saw no staked key there.
_slots_tools(){
  local t
  for t in grep sed sort cut awk tr; do
    command -v "$t" >/dev/null 2>&1 || { echo "slots: '$t' is not installed on this host: no slot is read without it" >&2; return 3; }
  done
}

# _slot_k_ok <k> -> 0 when <k> is a slot number: 0, or a positive integer written without a leading zero.
_slot_k_ok(){ case "${1:-}" in 0) return 0 ;; [1-9]|[1-9]*[0-9]) case "$1" in *[!0-9]*) return 1 ;; esac; return 0 ;; *) return 1 ;; esac; }

slot_project(){ _slot_k_ok "${1:-}" || { echo "slots: '${1:-}' is not a slot number" >&2; return 2; }
  if [ "$1" = 0 ]; then printf 'dendra-miner\n'; else printf 'dendra-miner-g%s\n' "$1"; fi; }
slot_dir(){ _slot_k_ok "${1:-}" || return 2; if [ "$1" = 0 ]; then printf '%s\n' "$SLOTS_KIT"; else printf '%s/gpu/%s\n' "$SLOTS_KIT" "$1"; fi; }
slot_env(){ _slot_k_ok "${1:-}" || return 2; if [ "$1" = 0 ]; then printf '%s/.env\n' "$SLOTS_KIT"; else printf '%s/gpu/%s/.env\n' "$SLOTS_KIT" "$1"; fi; }

# slot_val <k> <KEY> -> the value (carriage return removed); EMPTY when the key is absent from a file that
# was read; exit 3 when the file is missing or unreadable -- "no file" is not "no value".
slot_val(){
  local f v
  f="$(slot_env "${1:-}")" || return 2
  case "${2:-}" in ""|*[!A-Z0-9_]*) echo "slots: '${2:-}' is not a variable name" >&2; return 2 ;; esac
  [ -f "$f" ] && [ -r "$f" ] || return 3
  # sed's own status, then the first line and its carriage return cut in the shell: a reader that failed is
  # exit 3, never an empty value. Each match is marked with a leading `=`, so that a key written EMPTY prints
  # an empty line and an absent key prints nothing, as they always did.
  v="$(sed -n "s/^$2=/=/p" "$f")" || return 3
  [ -n "$v" ] || return 0
  v="${v%%$'\n'*}"; v="${v#=}"
  printf '%s\n' "${v%$'\r'}"
}

# slot_state <k> -> active | retired | missing ; `?` and exit 3 when its env cannot be read.
slot_state(){
  local f v
  f="$(slot_env "${1:-}")" || return 2
  [ -e "$f" ] || { printf 'missing\n'; return 0; }
  v="$(slot_val "$1" DENDRA_SLOT_RETIRED)" || { printf '?\n'; return 3; }
  if [ -n "$v" ]; then printf 'retired\n'; else printf 'active\n'; fi
}

# slot_ids [--active|--all] -> one k per line: 0 first, then every gpu/<k>/.env in numeric order. --active
# keeps the slots whose state is active. A gpu/ directory that exists and cannot be listed is exit 3, never
# "no slot": a rig whose slots cannot be read is not a rig with one identity.
slot_ids(){
  local how="${1:---all}" d b ks="" k st sorted
  case "$how" in --active|--all) : ;; *) echo "slots: slot_ids takes --active or --all" >&2; return 2 ;; esac
  _slots_tools || return 3
  if [ -e "$SLOTS_KIT/gpu" ]; then
    [ -d "$SLOTS_KIT/gpu" ] && [ -r "$SLOTS_KIT/gpu" ] && [ -x "$SLOTS_KIT/gpu" ] \
      || { echo "slots: $SLOTS_KIT/gpu exists and cannot be listed" >&2; return 3; }
    for d in "$SLOTS_KIT"/gpu/*; do
      [ -e "$d/.env" ] || continue
      b="${d##*/}"
      [ "$b" != 0 ] && _slot_k_ok "$b" || continue
      ks="$ks$b
"
    done
  fi
  sorted="$(printf '%s' "$ks" | sort -n)" || { echo "slots: the slots of $SLOTS_KIT/gpu could not be sorted" >&2; return 3; }
  for k in 0 $sorted; do
    if [ "$how" = --active ]; then
      st="$(slot_state "$k")" || return 3
      [ "$st" = active ] || continue
    fi
    printf '%s\n' "$k"
  done
}

# slot_next_free -> the smallest k >= 1 that no gpu/<k>/ uses, retired slots included: a retired slot keeps
# its identity and its stake, so its number is never handed to another card.
slot_next_free(){
  local k=1
  while [ -e "$SLOTS_KIT/gpu/$k" ]; do k=$((k+1)); done
  printf '%s\n' "$k"
}

# slot_compose_files <k> (k >= 1) -> the entries of COMPOSE_FILE in the slot's env, one per line, each a
# plain relative path to a file of the kit. An empty or malformed list is a refusal: the kit's own
# COMPOSE_FILE is slot 0's (it lists slot 0's override, so its card) and is never a fallback.
slot_compose_files(){
  local k="${1:-}" v e rest
  _slot_k_ok "$k" && [ "$k" != 0 ] || { echo "slots: slot_compose_files takes a slot k >= 1" >&2; return 2; }
  v="$(slot_val "$k" COMPOSE_FILE)" || { echo "slots: $(slot_env "$k") is missing or unreadable" >&2; return 3; }
  [ -n "$v" ] || { echo "slots: slot $k has no COMPOSE_FILE in $(slot_env "$k")" >&2; return 2; }
  rest="$v:"
  while [ -n "$rest" ]; do
    e="${rest%%:*}"; rest="${rest#*:}"
    case "$e" in
      ""|/*|*..*|*[!A-Za-z0-9._/-]*) echo "slots: slot $k names '$e' in COMPOSE_FILE: not a plain path of this kit" >&2; return 2 ;;
    esac
    [ -f "$SLOTS_KIT/$e" ] || { echo "slots: slot $k names $e in COMPOSE_FILE, and the kit has no such file" >&2; return 2; }
    printf '%s\n' "$e"
  done
}

# slot_argv <k> -> slot k's compose command, one argument per line. Slot 0 is the directory form it always
# was (the kit's .env names its files and profile); -p only restates its `name:`, so every command reads
# the same in a log. Slot k carries everything: see (a) above.
slot_argv(){
  local k="${1:-}" files f
  _slot_k_ok "$k" || { echo "slots: '$k' is not a slot number" >&2; return 2; }
  if [ "$k" = 0 ]; then printf 'docker\ncompose\n-p\ndendra-miner\n'; return 0; fi
  files="$(slot_compose_files "$k")" || return $?
  printf 'docker\ncompose\n-p\n%s\n--env-file\ngpu/%s/.env\n' "$(slot_project "$k")" "$k"
  while IFS= read -r f; do [ -n "$f" ] && printf -- '-f\n%s\n' "$f"; done <<EOF
$files
EOF
}

# _slot_files <k> -> the compose files that decide slot k (the kit's base file, and what COMPOSE_FILE adds).
_slot_files(){
  local v e rest
  if [ "$1" = 0 ]; then
    v="$(slot_val 0 COMPOSE_FILE 2>/dev/null)"; [ -n "$v" ] || v="docker-compose.yml"
    rest="$v:"
    while [ -n "$rest" ]; do e="${rest%%:*}"; rest="${rest#*:}"; [ -n "$e" ] && [ -f "$SLOTS_KIT/$e" ] && printf '%s\n' "$e"; done
    return 0
  fi
  slot_compose_files "$1"
}

# _slot_interpolated <with-default|without-default> <file>... -> the variable names the files interpolate.
# DERIVED from the files, never typed: a variable added to docker-compose.yml tomorrow is covered tomorrow.
_slot_interpolated(){
  local how="$1"; shift
  ( cd "$SLOTS_KIT" || exit 3
    if [ "$how" = all ]; then
      grep -ohE '\$\{[A-Za-z_][A-Za-z0-9_]*' "$@" 2>/dev/null | cut -c3-
    else
      grep -ohE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' "$@" 2>/dev/null | sed 's/^..//; s/.$//'
    fi ) | sort -u
}

# slot_check <k> [one-off] -> 0 when slot k's env holds every value a start needs; otherwise exit 2 and the
# missing keys named. Read before compose creates or starts anything: see (c) above. `one-off` is a container
# that runs one command and is removed (`run --rm`, how exit-miner.sh and encrypt-keys.sh read a slot's keys):
# it is allowed on a RETIRED slot -- a retired identity is still registered, and leaving the network with it is
# exactly what it is retired for -- and refused, like a start, on every other missing value.
slot_check(){
  local k="${1:-}" how="${2:-}" f files miss="" n v
  _slot_k_ok "$k" || { echo "slots: '$k' is not a slot number" >&2; return 2; }
  _slots_tools || { echo "slots: slot $k cannot be checked: nothing is started" >&2; return 2; }
  f="$(slot_env "$k")"
  [ -f "$f" ] && [ -r "$f" ] || { echo "slots: slot $k: $f is missing or unreadable: nothing is started" >&2; return 2; }
  if [ "$k" != 0 ]; then
    [ "$(slot_val "$k" DENDRA_SLOT)" = "$k" ] || miss="$miss DENDRA_SLOT=$k"
    [ "$(slot_val "$k" COMPOSE_PROJECT_NAME)" = "$(slot_project "$k")" ] || miss="$miss COMPOSE_PROJECT_NAME=$(slot_project "$k")"
    printf '%s\n' "$(slot_val "$k" DENDRA_GPU_UUID)" | grep -Eq "$SLOT_UUID_RE" || miss="$miss DENDRA_GPU_UUID(a card UUID)"
    [ "$how" = one-off ] || [ -z "$(slot_val "$k" DENDRA_SLOT_RETIRED)" ] || miss="$miss (the slot is RETIRED: re-add its card with join.sh --gpus)"
  fi
  files="$(_slot_files "$k")" || { echo "slots: slot $k: its compose files cannot be listed: nothing is started" >&2; return 2; }
  [ -n "$files" ] || { echo "slots: slot $k: no compose file found: nothing is started" >&2; return 2; }
  # shellcheck disable=SC2046
  for n in $(_slot_interpolated without-default $(printf '%s\n' "$files")); do
    v="$(slot_val "$k" "$n")"
    [ -n "$v" ] || miss="$miss $n"
  done
  [ -z "$miss" ] && return 0
  echo "slots: slot $k ($(slot_project "$k")) cannot start: $f lacks$miss. Compose would only WARN and start it with those empty; refused, nothing was started." >&2
  return 2
}

# slot_compose <k> <compose arguments...> -> runs slot k's command from the kit. A verb that creates or starts
# containers is preceded by slot_check. For EVERY slot, slot 0 included, the interpolated variables and every
# COMPOSE_* leave the environment first: see (b) above -- slot 0's .env names its project and files, and a
# COMPOSE_PROJECT_NAME or a MINER_ID exported in the shell (a slot k's env sourced by hand) would otherwise
# give slot 0 another project, so another key volume, or another identity on its own volume.
# THE VERB IS FOUND, NEVER GUESSED: the first word without a dash used to be taken for it, so the VALUE of an
# option placed before it (`--ansi never up`, `--profile judge up`) was read as the verb, and the check was
# skipped. A slot k's command is already complete (its project, env and files): any option before the verb
# is refused. Slot 0 accepts the global options that name no identity (--profile, --ansi, --progress,
# --parallel, --dry-run, --compatibility), each value skipped as such; anything else is refused.
# THE VOLUMES ARE NEVER REMOVED HERE: `down -v` / `rm -v` (and their long and grouped forms) are refused for
# every slot -- a miner-keys volume holds a staked key, and leaving the network is exit-miner.sh's job.
# SLOT_KEEP_ENV="NAME ..." names, one call at a time, the variables the CALLER sets on purpose for that call
# and that must reach compose: encrypt-keys.sh mounts no passphrase directory while it only reads a plan, by
# handing compose an empty DENDRA_SECRETS_DIR. A name the caller does not list is removed as above; COMPOSE_*
# can never be kept.
slot_compose(){
  local k="${1:-}" out verb a files n skip="" after=""
  local -a argv unset_args
  _slots_tools || return 3
  out="$(slot_argv "$k")" || return $?
  shift
  mapfile -t argv <<EOF
$out
EOF
  verb=""
  for a in "$@"; do
    if [ -n "$skip" ]; then skip=""; continue; fi
    case "$a" in
      -*) [ "$k" = 0 ] || { echo "slots: slot $k: '$a' before the compose verb -- this slot's command already names its project, env file and compose files; options go after the verb. Nothing was run." >&2; return 2; }
          case "$a" in
            --profile|--ansi|--progress|--parallel) skip=1 ;;
            --profile=*|--ansi=*|--progress=*|--parallel=*|--dry-run|--compatibility) : ;;
            *) echo "slots: slot 0: '$a' before the compose verb is not an option this file passes (it could name another project, env file or compose file). Nothing was run." >&2; return 2 ;;
          esac ;;
      *) verb="$a"; break ;;
    esac
  done
  case "$verb" in
    up|create|start|restart) slot_check "$k" || return 2 ;;
    run) slot_check "$k" one-off || return 2 ;;
    down|rm)
      for a in "$@"; do
        [ -n "$after" ] || { [ "$a" = "$verb" ] && after=1; continue; }
        case "$a" in
          --volumes|--volumes=*|-v*|-[!-]*v*) echo "slots: slot $k: '$verb $a' would remove volumes, among them the one that holds this identity's staked key. Refused; to stop it: bash $SLOTS_KIT/slots.sh run $k stop -- to leave the network: bash $SLOTS_KIT/exit-miner.sh --slot $k. Nothing was run." >&2; return 2 ;;
        esac
      done ;;
  esac
  files="$(_slot_files "$k")" || return $?
  unset_args=()
  # shellcheck disable=SC2046
  for n in $(_slot_interpolated all $(printf '%s\n' "$files")) $(compgen -e | grep '^COMPOSE_'); do
    case "$n" in COMPOSE_*) : ;; *) case " ${SLOT_KEEP_ENV:-} " in *" $n "*) continue ;; esac ;; esac
    unset_args+=(-u "$n")
  done
  ( cd "$SLOTS_KIT" || exit 3; exec env "${unset_args[@]}" "${argv[@]}" "$@" )
}

# slot_run_hint <k> <compose arguments...> -> the command a person types to run that on slot k, for a message.
# Always the slots.sh form: a slot k's command written out by hand would need its -p, --env-file and every -f.
slot_run_hint(){ local k="${1:-}"; shift; printf 'bash %s/slots.sh run %s %s' "$SLOTS_KIT" "$k" "$*"; }

# slot_cid <k> <service> [--running] -> the id of slot k's container for that service, found by its compose
# LABELS (project, service, not a one-off run) and then confirmed by its working_dir label: a clone
# elsewhere on this machine may run a project of the same name. Empty = none; exit 3 when docker does not
# answer -- never "none".
slot_cid(){
  local k="${1:-}" s="${2:-}" p ids id wd kp
  local -a all=(-a)
  p="$(slot_project "$k")" || return 2
  [ -n "$s" ] || { echo "slots: slot_cid needs a service name" >&2; return 2; }
  [ "${3:-}" = --running ] && all=()
  ids="$(docker ps "${all[@]}" -q --filter "label=com.docker.compose.project=$p" \
           --filter "label=com.docker.compose.service=$s" --filter "label=com.docker.compose.oneoff=False" 2>/dev/null)" || return 3
  kp="$(cd "$SLOTS_KIT" 2>/dev/null && pwd -P)"
  for id in $ids; do
    wd="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id" 2>/dev/null)" || return 3
    wd="$(printf '%s' "$wd" | tr -d '\r')"
    if [ "$wd" = "$SLOTS_KIT" ] || [ "$wd" = "$kp" ]; then printf '%s\n' "$id"; return 0; fi
  done
  return 0
}

# slot_registration <k> -> what slot k's daemon says about its on-chain registration, read from its heartbeat
# (modea/heartbeat.py, parsed by the container's own python3, never searched as text):
#   registered | refused | deferred:<reason> (the faucet's: ip_quota, addr_cooldown, global_cap, pow, ...)
#   mismatch   the chain records ANOTHER operator for this identity (miner.check_operator writes
#              `identity_mismatch`): its commits are refused, whatever its registration says -- read first
#   pending    the heartbeat was read and the daemon has not attempted a registration yet
#   unread     no running miner, docker did not answer, or a heartbeat that cannot be read
slot_registration(){
  local cid out
  cid="$(slot_cid "${1:-}" miner --running)" || { printf 'unread\n'; return 0; }
  [ -n "$cid" ] || { printf 'unread\n'; return 0; }
  out="$(docker exec -w /app "$cid" python3 -c 'import sys
sys.path.insert(0, "/app")
try:
    from modea import heartbeat as hb
    d, why = hb.read()
except Exception:
    d = None
if not isinstance(d, dict):
    print("unread"); raise SystemExit
m = d.get("identity_mismatch")
if isinstance(m, dict) and m.get("operator"):
    print("mismatch"); raise SystemExit
r = d.get("registration")
if r is None:
    print("pending"); raise SystemExit
s = r.get("state") if isinstance(r, dict) else None
if s in ("registered", "refused"):
    print(s)
elif s == "deferred":
    print("deferred:" + (str(r.get("reason") or "") or "unknown"))
else:
    print("unread")' 2>/dev/null)"
  case "$out" in registered|refused|mismatch|pending|deferred:*) printf '%s\n' "$out" ;; *) printf 'unread\n' ;; esac
}

# slot_env_set <k> <KEY> <value> [unset] -- one line of slot k's env, rewritten in place: replaced where it
# stands, appended when absent, removed with `unset`. The file keeps its mode (it carries the relay token)
# and is replaced in one rename from a temporary file of the same directory.
slot_env_set(){
  local f t
  f="$(slot_env "${1:-}")" || return 2
  case "${2:-}" in ""|*[!A-Z0-9_]*) return 2 ;; esac
  [ -f "$f" ] || return 3
  # The temporary name falls under the publisher's `.env.bak*` exclusion: a run interrupted between these
  # lines leaves a copy of a token-bearing file, never one a mirror would carry.
  t="$(umask 077; mktemp "$(dirname "$f")/.env.bak-edit.XXXXXX")" || return 3
  if ! awk -v k="$2" -v v="${3:-}" -v u="${4:-}" 'BEGIN { n = length(k) + 1 }
    substr($0, 1, n) == k "=" { if (!s && u == "") print k "=" v; s = 1; next }
    { print }
    END { if (!s && u == "") print k "=" v }' "$f" > "$t"; then
    rm -f "$t"; return 3
  fi
  chmod --reference="$f" "$t" 2>/dev/null || chmod 600 "$t"
  mv -f "$t" "$f"
}

# ---------------------------------------------------------------- is the engine of a slot on its own card?
# MEASURED on ollama 0.32.1, the engine names the card it runs on in one startup line:
#   msg="inference compute" id=0 filter_id=0 library=CUDA ... pci_id=0000:08:00.0 ... total="8.0 GiB"
# by index and PCI bus id -- never by UUID -- and an unknown CUDA_VISIBLE_DEVICES makes it fall back, in
# silence, to `msg="inference compute" id=cpu library=cpu`. So the check compares the PCI bus id of the
# line(s) with the one nvidia-smi gives for the slot's UUID. THREE ANSWERS: pinned (one CUDA line, this
# card), not-pinned (the CPU, another card, or more than one card), unknown (no line, a line whose form
# this check does not know, an unreadable log or card). The kit pins the engine image (the `x-ollama-image` line
# of docker-compose.yml), and a later pin may change this line's format: it then reads `unknown`, never `pinned`.
# slot_pin_judge <engine log file> <pci bus id of the card> -> "verdict|why" on one line.
slot_pin_judge(){
  [ -r "${1:-}" ] || { printf 'unknown|the engine log could not be read\n'; return 0; }
  awk -v want="${2:-}" '
    function norm(p,   a, n, d) {
      p = tolower(p); n = split(p, a, ":"); if (n != 3) return ""
      d = a[1]; sub(/^0+/, "", d); if (d == "") d = "0"
      return d ":" a[2] ":" a[3]
    }
    /msg="inference compute"/ {
      n++; lib = ""; pci = ""
      if (match($0, /library=[^ ]+/)) lib = tolower(substr($0, RSTART + 8, RLENGTH - 8))
      if (match($0, /pci_id=[^ ]+/)) pci = substr($0, RSTART + 7, RLENGTH - 7)
      gsub(/"/, "", lib); gsub(/"/, "", pci)
      if (lib == "cuda") { nc++; if (norm(pci) == "") nopci++; else if (norm(want) != "" && norm(pci) == norm(want)) hit++ }
      else if (lib == "cpu") ncpu++
      else other++
    }
    END {
      if (n == 0) { print "unknown|no \"inference compute\" line in the engine log since its last start"; exit }
      if (other > 0) { print "unknown|the engine names a library this check does not know"; exit }
      if (nc == 0) { print "not-pinned|the engine runs on the CPU (library=cpu): its card is not visible to it"; exit }
      if (ncpu > 0) { print "unknown|the engine log names both a card and the CPU"; exit }
      if (nopci > 0) { print "unknown|the engine line names no pci_id"; exit }
      if (nc > 1) { print "not-pinned|the engine sees " nc " cards, not one"; exit }
      if (norm(want) == "") { print "unknown|the PCI bus id of this slot'"'"'s card could not be read"; exit }
      if (hit == 1) { print "pinned|the engine runs on this card only"; exit }
      print "not-pinned|the engine runs on another card"
    }' "$1"
}

# slot_pin <k> -> slot_pin_judge on slot k's engine since its last start, and the card its env names.
slot_pin(){
  local k="${1:-}" uuid cid started t pci rc
  _slot_k_ok "$k" || { echo "slots: '$k' is not a slot number" >&2; return 2; }
  uuid="$(slot_val "$k" DENDRA_GPU_UUID)" || { printf 'unknown|the env of slot %s cannot be read\n' "$k"; return 0; }
  printf '%s\n' "$uuid" | grep -Eq "$SLOT_UUID_RE" || { printf 'unknown|slot %s names no card (single-identity mode pins none)\n' "$k"; return 0; }
  cid="$(slot_cid "$k" ollama --running)"; rc=$?
  [ "$rc" = 0 ] || { printf 'unknown|docker did not answer\n'; return 0; }
  [ -n "$cid" ] || { printf 'unknown|no running engine for slot %s\n' "$k"; return 0; }
  started="$(docker inspect --format '{{.State.StartedAt}}' "$cid" 2>/dev/null | tr -d '\r')"
  [ -n "$started" ] || { printf 'unknown|the start time of the engine could not be read\n'; return 0; }
  pci="$(bash "$SLOTS_KIT/../hw_probe.sh" --list-gpus 2>/dev/null | awk -F'|' -v u="$uuid" '$2 == u { print $7 }')"
  t="$(mktemp)" || { printf 'unknown|no temporary file\n'; return 0; }
  if ! docker logs --since "$started" "$cid" > "$t" 2>&1; then rm -f "$t"; printf 'unknown|the engine log could not be read\n'; return 0; fi
  slot_pin_judge "$t" "$pci"
  rm -f "$t"
}

# ---------------------------------------------------------------- command line
_slots_main(){
  local cmd="${1:-}" k st uuid mid out rc
  case "$cmd" in
    list)
      out="$(slot_ids --all)" || { echo "slots: the slots of this kit cannot be read" >&2; return 3; }
      for k in $out; do
        st="$(slot_state "$k")"; uuid="$(slot_val "$k" DENDRA_GPU_UUID 2>/dev/null)"; mid="$(slot_val "$k" MINER_ID 2>/dev/null)"
        printf '%s|%s|%s|%s|%s|%s\n' "$k" "$(slot_project "$k")" "$st" "$uuid" "$mid" "$(slot_dir "$k")"
      done ;;
    argv) shift; slot_argv "${1:-}" ;;
    dir)  shift; slot_dir "${1:-}" || { echo "slots: '${1:-}' is not a slot number" >&2; return 2; } ;;
    run)  shift; k="${1:-}"; shift; slot_compose "$k" "$@" ;;
    check) shift; slot_check "${1:-}" ;;
    pin)  shift; slot_pin "${1:-}" ;;
    registration) shift; slot_registration "${1:-}" ;;
    *) echo "usage: bash slots.sh list | argv <k> | dir <k> | run <k> <compose args...> | check <k> | pin <k> | registration <k>" >&2; return 2 ;;
  esac
}
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  _slots_main "$@"
  exit $?
fi
