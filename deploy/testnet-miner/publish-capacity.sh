#!/usr/bin/env bash
# publish-capacity.sh — declare this machine's hardware to the network capacity registry.
#
# WHY THIS FILE EXISTS. `join.sh` publishes the inventory; the docker-compose kit never did. An
# operator who starts the miner with `docker compose up` — the documented path of THIS kit — therefore
# runs a node the registry has never heard of, and the public /network page shows zero.
#
# WHAT READS IT, AND WHAT DOES NOT. /network shows the capacity these reports declare. The public chat
# does NOT count them: it reads `onchain.present` from the same registry service -- the miners registered
# on chain with a presence proof the chain accepted, which that service reads FROM THE CHAIN
# (capacity_server.py::onchain_block), never from a report anyone can post. Only a registry service that
# does not publish the `onchain` block leaves the chat on its fallback reading, `verified.live_nodes`, the
# signed reports of this file. So a missing inventory under-reports the hardware of the network; whether
# the chat opens is a reading of the chain.
#
# ⚠️ IT RUNS ON THE HOST, NOT IN THE CONTAINER. The miner container has no GPU (the ollama container
# does), so a probe run inside it would report zero cards and publish a FALSE inventory — worse than
# publishing nothing, because it would be displayed as an operator's declaration.
#
# ⚠️ AND IT MUST REPEAT. The registry marks a report stale after 24 h and purges it after 7 days
# (capacity_server.py). A single publication at install time disappears on its own, silently, and the
# node leaves /network's capacity listing days later, for a reason nobody will connect to this.
#
# ONLY A SIGNED REPORT OF A REGISTERED MINER IS SENT. The registry files a report under the identity its
# signature proves, and an unsigned or unproven one under a key of its own (capacity_server.py::storage_key):
# a machine that sent both was listed twice. So this file sends nothing unsigned, and nothing before the chain
# records this identity with the signing key as its operator -- the one report the registry can prove. The
# registration is read, before signing, where the report is signed (the miner container, or this host for a
# bare-metal install). Until it reads "registered", nothing is sent and the hourly job asks again; the first
# report goes out at the first run after the registration.
#
# Exit codes (one slot; with several, the worst: 1, then 3, then 2, then 0):
#   0  published, signed
#   1  a refusal or a failure: configuration, probe, contradiction, no way to sign, the registry refused
#   2  not published: the chain records no such miner, or records it for another operator
#   3  not published: the registration could not be read (node, key or answer) -- never read as registered
#
# Usage — from anywhere:
#   bash deploy/testnet-miner/publish-capacity.sh              # every active identity of this machine
#   bash deploy/testnet-miner/publish-capacity.sh --slot <k>   # one of them (deploy/testnet-miner/slots.sh)
#
# ONE IDENTITY PER CARD (deploy/join.sh --gpus): each identity publishes its OWN report -- its card, its
# identity, signed inside ITS container -- and every slot but slot 0 says `host_share:"secondary"`, so the
# registry counts the machine's RAM and cores once. The card's UUID never leaves the machine: the probe never
# writes it in the JSON. One line per slot in the log, prefixed [slot k]; the exit code is the worst of them.
# The cron line stays ONE line, without --slot: it covers every slot.
#
# EACH REPORT ALSO DECLARES ITS IDENTITY'S JUDGE ROLE (`judge_role`: active | mute | off | unknown), read in that
# slot's container by its own self-test and signed with the rest -- see "THE JUDGE ROLE" below.
#
# Keep it fresh with cron (hourly is ample; the report is a few hundred bytes).
# ⚠️ Keep `bash -lc` and keep the log: cron has a minimal PATH, and a probe that cannot find nvidia-smi
# declares a CPU tier with a smaller model instead of failing.
#   (crontab -l 2>/dev/null; echo "17 * * * * bash -lc 'bash $PWD/deploy/testnet-miner/publish-capacity.sh' >> /tmp/dendra-capacity.log 2>&1") | crontab -
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PROBE="$ROOT/deploy/hw_probe.sh"

say(){ printf '%s\n' "$*"; }
die(){ printf '  [STOP] %s\n' "$*" >&2; exit 1; }

[ -r "$PROBE" ] || die "hardware probe not found: $PROBE"
# THE SLOTS ARE READ THROUGH THE ONE LIBRARY THAT KNOWS THEM: which env, which container (by its compose
# labels, never by a name pattern: `name=miner` matches the engines as well, and every miner of a rig).
[ -r "$HERE/slots.sh" ] || die "the slot library is missing from this kit: $HERE/slots.sh"
# shellcheck disable=SC1091
. "$HERE/slots.sh" || die "the slot library could not be loaded: $HERE/slots.sh"
ONLY=""
case "${1:-}" in
  --slot) ONLY="${2:-}"; _slot_k_ok "$ONLY" || die "--slot needs a slot number (bash $HERE/slots.sh list)" ;;
  "") : ;;
  *) die "unknown argument: $1 (usage: publish-capacity.sh [--slot <k>])" ;;
esac
# The registry URL from the ENVIRONMENT wins over every slot's file -- taken once, before any slot is read,
# so one slot's value never becomes the next one's.
CAPURL_ENV="${DENDRA_CAPACITY_URL:-}"

# _judge_role_word <what `miner_selftest.py --judge-role` printed> -> active | mute | off | unknown.
# EXACTLY ONE `DENDRA_JUDGE_ROLE <word>` line, and the word EXACTLY one of the three a container can establish;
# anything else -- no line (an image older than --judge-role, an exec that failed), two lines, another word --
# is unknown. Never off: an answer nobody could read is not "no judge here". One trailing carriage return is the
# only byte removed (a line that crossed a terminal): a control byte anywhere else is part of the word, so the
# word is another word -- deleting it would turn `act<0x01>ive` into `active`.
_judge_role_word(){
  printf '%s\n' "${1:-}" | awk 'BEGIN { cr = sprintf("%c", 13) }
    index($0, "DENDRA_JUDGE_ROLE ") == 1 { n++; w = substr($0, 19) }
    END { if (n != 1) { print "unknown"; exit }
          if (substr(w, length(w)) == cr) w = substr(w, 1, length(w) - 1)
          print ((w == "active" || w == "mute" || w == "off") ? w : "unknown") }'
}

# _registration_word <what REG_PY printed> -> registered | absent | other | unknown. The same reading as the judge
# role: EXACTLY ONE `DENDRA_REGISTRATION <word>` line, the word one of the four REG_PY prints; anything else --
# no line, two lines, another word, `unread` -- is unknown, and an unknown is never read as registered.
_registration_word(){
  printf '%s\n' "${1:-}" | awk 'BEGIN { cr = sprintf("%c", 13) }
    index($0, "DENDRA_REGISTRATION ") == 1 { n++; w = substr($0, 21) }
    END { if (n != 1) { print "unknown"; exit }
          if (substr(w, length(w)) == cr) w = substr(w, 1, length(w) - 1)
          print ((w == "registered" || w == "absent" || w == "other") ? w : "unknown") }'
}

# REG_PY -- the registration of <identity>, read on chain, against the address of <key>: run by python3 where the
# report was signed (this host, or the miner container), with that place's dendrad, keyring and DENDRA_NODE. It
# needs only what signing already needs there (dendrad, modea.relay_signature). It prints one word and its reason:
#   registered  the chain records <identity> and its operator is the address of <key>: the report will be proven;
#   absent      the chain answered NotFound: no such miner yet;
#   other       the chain records it for another operator: the report would not be proven;
#   unread      anything else (dendrad, the node, the answer, the key).
# The record is PARSED; an absent operator is the empty string (proto3 omits it), which no address equals.
REG_PY="$(cat <<'PY'
import json, os, subprocess, sys
mid, key = sys.argv[1], sys.argv[2]
def say(word, why):
    print("DENDRA_REGISTRATION " + word)
    print("DENDRA_REGISTRATION_WHY " + " ".join(str(why).split())[:240])
    raise SystemExit(0)
node = (os.environ.get("DENDRA_NODE") or "").strip()
cmd = ["dendrad", "query", "jobs", "get-miner", mid, "--output", "json"] + (["--node", node] if node else [])
try:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
except Exception as e:
    say("unread", "dendrad could not be run here (" + type(e).__name__ + ")")
try:
    d = json.loads(p.stdout or "")
except ValueError:
    d = None
if not isinstance(d, dict):
    if "code = NotFound" in (p.stdout or "") + (p.stderr or ""):
        say("absent", "the chain records no miner " + mid + " yet")
    say("unread", "the miner registry gave no record: " + ((p.stderr or p.stdout or "no answer").strip())[:160])
m = d.get("miner")
if not isinstance(m, dict):
    say("unread", "the answer is not a miner record")
operator = str(m.get("operator", "") or "")
try:
    sys.path.insert(0, os.getcwd())
    from modea import relay_signature as rs
    kdir = (os.environ.get("DENDRA_KEYRING_DIR") or "").strip() or None
    address = rs.address_from_key(key, keyring_backend=None, keyring_dir=kdir)
except Exception as e:
    say("unread", "the address of the signing key was not read (" + type(e).__name__ + ")")
if not address:
    say("unread", "the signing key has no address")
if operator == address:
    say("registered", mid + " is registered, operated by this key (" + address + ")")
say("other", mid + " is registered for the operator " + (operator or "(none)") + ", while this key is " + address)
PY
)"

publish_slot(){
K="$1"
ENVF="$(slot_env "$K")"
[ -r "$ENVF" ]  || die "configuration not found: $ENVF"

# Read ONLY the two variables needed, by their exact names. A broad pattern such as `^DENDRA_` would
# also match the relay token, which lives in this very file — and a secret read by accident is a
# secret that eventually gets printed.
# ALL whitespace is stripped, not just the carriage return: none of these four values can hold a
# space, and every test downstream is a NON-EMPTY test. `KEY=   ` used to survive as three spaces,
# which passes `[ -n ... ]`, passes `:=`, and is then used as an identifier -- the exact hole the
# identity adoption below already names: a blank passes an existence test and is not an identity.
MINER_ID="$(sed -n 's/^MINER_ID=//p'          "$ENVF" | head -1 | tr -d '[:space:]"')"
NODE="$(sed -n 's/^DENDRA_NODE=//p'           "$ENVF" | head -1 | tr -d '[:space:]"')"
DENDRA_CAPACITY_URL="${CAPURL_ENV:-$(sed -n 's/^DENDRA_CAPACITY_URL=//p' "$ENVF" | head -1 | tr -d '[:space:]"')}"
[ -n "$MINER_ID" ] || die "MINER_ID missing from $ENVF"
# THE CARD OF THIS IDENTITY. Slot 0 of a kit without --gpus names none: its report is the machine's, as it
# always was. Any other slot names its card, and its report is that card's (--gpu); a slot k that names none
# is refused, never reported as the machine.
GPU_UUID="$(slot_val "$K" DENDRA_GPU_UUID 2>/dev/null)"
PROBE_GPU=()
if [ -n "$GPU_UUID" ]; then PROBE_GPU=(--gpu "$GPU_UUID")
elif [ "$K" != 0 ]; then die "slot $K names no card (DENDRA_GPU_UUID) in $ENVF: nothing published"; fi
# THE JUDGE ROLE ON THE CPU IS DECLARED AS WHAT IT SERVES. On a machine whose card the engine cannot reach (Docker
# without the NVIDIA container toolkit), deploy/join.sh decided the role with --no-gpu and wrote the override that
# puts the miner on the CPU instance (write_cpu_judge_override). The probe run as-is still reads the card on the host,
# and the report then declared a GPU backend, a card and the miner role, signed, for a machine that serves the judge
# model on its CPU. The kit's own override is what says which engine the miner uses: read there, the probe is asked
# with --no-gpu, as join.sh asked it.
CPU_JUDGE_KIT=0
if [ "$K" = 0 ] && grep -qsF 'OLLAMA_ENDPOINT: "http://ollama-cpu:11434"' "$HERE/docker-compose.override.yml"; then
  CPU_JUDGE_KIT=1; PROBE_GPU=(--no-gpu)
fi

# The registry URL. DENDRA_CAPACITY_URL wins; DENDRA_NODE is only a fallback.
#
# ⚠️ DENDRA_NODE IS NOT A RELIABLE SOURCE FOR IT. In the recommended setup the miner runs its OWN node
# beside it, so the value in this file is deliberately CONTAINER-LOCAL (`tcp://dendra-node:26657`, the
# node's alias on the dendra-chain network; `tcp://host.docker.internal:26657` in a kit from an older
# join.sh) -- correct for the miner, useless as a public endpoint. Deriving the registry from it yields
# `https://dendra-node/capacity`, which resolves to nothing from the host.
#
# That failure would be both silent and delayed: the install-time publication succeeds (it is made from
# the operator's public endpoint), then every scheduled run posts into the void, and 24 h later the report
# ages out -- /network falls back to zero and the public chat closes its composer, for a reason nobody
# would connect to an installation made the day before.
#
# So a host that cannot be reached from outside is REFUSED, never posted to. An unreachable registry is a
# configuration error to be told, not an attempt to be made.
# THE CHECK BELOW GUARDED THE FALLBACK ONLY, AND THE FALLBACK IS NOT THE FIELD THAT WINS.
# `DENDRA_CAPACITY_URL` takes precedence (see above) and it is the line `deploy/join.sh` writes into
# the kit. When it was set, the host was never confronted. Measured with the fallback serving
# as its own witness:
#   DENDRA_NODE=tcp://host.docker.internal:26657, no URL      -> REFUSED
#   DENDRA_CAPACITY_URL=https://host.docker.internal/capacity -> PUBLISHED
# Same host, opposite verdicts, decided only by which field carried it -- and what follows is exactly
# the silent, delayed failure described at length above.
# A NAME WITHOUT A DOT IS A CONTAINER OR LAN ALIAS, NEVER A PUBLIC ENDPOINT. The default setup now writes
# `DENDRA_NODE=tcp://dendra-node:26657` (the node kit's project name, its alias on the dendra-chain network,
# or whatever DENDRA_PROJECT names): a list of known local names would miss the next project name, so the
# rule is the shape. No certificate is issued for a single-label name, and nothing outside this host
# resolves one. An IPv6 literal (colons, no dot) is left to the explicit entries above it.
_is_local_host(){
  case "$1" in
    host.docker.internal|localhost|127.*|0.0.0.0|::1|[[]::1[]]|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    *.*|*:*) return 1 ;;
    *) return 0 ;;
  esac
}
_host_of(){ printf %s "$1" | sed -E 's#^[a-z]+://##; s#:[0-9]+$##; s#/.*$##'; }
URL="${DENDRA_CAPACITY_URL:-}"
if [ -z "$URL" ] && [ -n "$NODE" ]; then
  HOST="$(printf '%s' "$NODE" | sed -E 's#^[a-z]+://##; s#:[0-9]+$##; s#/.*$##')"
  # ONE LIST, TWO USES. The `case` that lived here carried the local-host list inline, so it could
  # only ever guard THIS branch -- the fallback. It is now `_is_local_host`, called here and again on
  # the final URL below, whichever field produced it. A second copy of a list like this drifts, and
  # this repository has already paid for a rule propagated by copy.
  if [ -z "$HOST" ]; then
    die "DENDRA_NODE is present but carries no host"
  elif _is_local_host "$HOST"; then
    die "DENDRA_NODE points at a LOCAL node ($HOST), which is correct for the container but cannot be
       the public capacity registry. Nothing was published -- posting there would fail silently and this
       node would drop off /network 24 h later, closing the public chat with it.
       Fix: add the operator's PUBLIC endpoint to $ENVF, on its own line:
         DENDRA_CAPACITY_URL=https://HOST/capacity      (HOST = the operator's public host)
       deploy/join.sh writes that line when it generates the kit; a hand-written .env will not have it."
  else
    URL="https://$HOST/capacity"
  fi
fi
[ -n "$URL" ] || die "cannot determine the registry URL (neither DENDRA_CAPACITY_URL nor DENDRA_NODE in $ENVF)"
# THE SAME LIST, ON THE URL ACTUALLY POSTED TO -- whichever field produced it.
if _is_local_host "$(_host_of "$URL")"; then
  die "the capacity registry URL points at a LOCAL host ($URL), which cannot be reached from outside.
       Nothing was published -- posting there fails silently, and this node drops off /network 24 h
       later, closing the public chat with it.
       Fix: put the operator PUBLIC endpoint in $ENVF:
         DENDRA_CAPACITY_URL=https://HOST/capacity      (HOST = the operator public host)"
fi

# ⚠️ THE PROBE MUST NOT INHERIT A CRIPPLED PATH. `nvidia-smi` does not live in a standard bin directory
# on every host — under WSL it is /usr/lib/wsl/lib/nvidia-smi, on container hosts /usr/local/nvidia/bin.
# A scheduler (cron, systemd timer) runs with a MINIMAL PATH, not a login shell: the probe then finds no
# nvidia-smi, concludes "no GPU", drops to the CPU tier and declares a SMALLER MODEL than the one this
# miner actually serves. Nothing fails, nothing is logged — a FALSE inventory is published and displayed
# as the operator's own declaration.
export PATH="$PATH:/usr/lib/wsl/lib:/usr/local/nvidia/bin:/opt/nvidia/bin:/usr/local/cuda/bin"

JSON="$(bash "$PROBE" "${PROBE_GPU[@]}" --json "$MINER_ID" 2>/dev/null | tail -1)"
case "$JSON" in
  '{'*) : ;;
  *) die "hardware probe unreadable — nothing published (an empty declaration is worth less than none)" ;;
esac

# READ THE FIELD BY ITS KEY — never match the document. Searching the text for `"gpu_count":0` is a
# predicate on bytes, and it answers "not zero" to two situations that are nothing alike: a report
# that says one card, and a report where the field is MISSING. It also answers "not zero" to a report
# that says zero with a space after the colon. All three then skip the cross-check below, which is the
# one that decides whether this machine may publish at all.
# The report is emitted flat, on one line, by the probe in this same kit, so the key anchors the read
# — and no interpreter is required, which matters on a host that has none (see the read-back at the
# end of this file).
json_uint(){ printf '%s' "$2" | sed -nE "s/.*\"$1\"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p" | head -1; }

# ⛔ A DECLARATION THAT CONTRADICTS A DIRECTLY OBSERVABLE FACT IS NOT PUBLISHED.
# The PATH repair above covers the locations we know; this covers the ones nobody has hit yet. If the
# probe says "no GPU" while nvidia-smi answers with one, the probe is UNDER-declaring — and an
# under-declared inventory is not a smaller truth, it is a false one: the tier and the advertised model
# fall with it. Refusing is the only honest outcome, because publishing keeps the page looking healthy.
# The count feeds a SAFETY guard, so its absence is an ERROR and never a permissive default: a report
# without the field is not a report of zero cards, it is a report nobody can read — and reading it as
# "no GPU declared" would skip the very cross-check below.
GPU_COUNT="$(json_uint gpu_count "$JSON")"
[ -n "$GPU_COUNT" ] || die "the probe's report carries no readable gpu_count — nothing published.
       The declaration cannot be checked against this machine, and an unchecked inventory is shown
       on the public page as YOUR declaration."
if [ "$GPU_COUNT" -eq 0 ] && [ "$CPU_JUDGE_KIT" = 0 ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -q .; then
    say "  [STOP] the probe declares NO GPU while nvidia-smi reports one:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/           /'
    say "         NOTHING was published. A false inventory is shown as YOUR declaration, and it downgrades"
    say "         both the tier and the model this node advertises."
    say "         Run it from a login shell, so the probe sees the environment you see:"
    say "           bash -lc 'bash $HERE/publish-capacity.sh'"
    exit 1
  fi
fi

# `miner_ids` carries the ON-CHAIN identity of this machine. Without it the registry cannot tell an
# anonymous declaration from an effectively staked node, and the `verified` subtotal — the only one a
# public surface displays — would stay at zero even after a successful publication.
#
# ⚠️ THE NAME IN THE .env IS A REQUEST, NOT AN IDENTITY. MINER_ID holds the `m-<hash>` join.sh derives;
# miner.py ALIGNS it to the identifier the chain accepts (`dm1…`) and persists THAT one in the
# miner volume, at /data/keys/identite-resolue. Only the aligned form can exist in the on-chain miner
# registry, so a declaration carrying the .env name lists an identity the registry's cross-check can
# never match: the POST returns 200 and the report never enters the `verified` block, with a green
# publication as the only trace. `node_id` above is a DIFFERENT field — the key the
# registry files the report under — and stays exactly as the probe emitted it.
#
# Same adoption rule as the container glue (docker/entrypoint-services.sh): strip ALL whitespace, and
# adopt ONLY a derived identifier. Blanks pass a non-empty test and junk is a corrupt memory; neither
# is an identity. The configured name stays the fallback, so a stopped miner still publishes hardware.
IDENTITY="$MINER_ID"
RESOLVED=""; _mc=""
# THIS SLOT'S miner container, found by its compose labels (slots.sh slot_cid): the identity is read there,
# and the report is signed there -- never in the first container whose NAME contains "miner".
if command -v docker >/dev/null 2>&1; then
  _mc="$(slot_cid "$K" miner --running 2>/dev/null)"
  [ -n "$_mc" ] && RESOLVED="$(docker exec "$_mc" cat /data/keys/identite-resolue 2>/dev/null | tr -d '[:space:]')"
fi
case "$RESOLVED" in
  dm1*) IDENTITY="$RESOLVED" ;;
  *)    say "  [i] the identity resolved by the daemon is not readable (miner stopped, or no alignment yet)."
        say "      Declaring the configured name $MINER_ID — the registry verifies it only if that exact"
        say "      name exists on-chain. Start the miner once, then run this again." ;;
esac
JSON="$(printf '%s' "$JSON" | sed -E "s#\}\$#,\"miner_ids\":[\"$IDENTITY\"]}#")"
# A SLOT k SHARES ITS MACHINE: its report says so, so the registry counts the machine's RAM and cores once
# (capacity_server.py::aggregate). Added BEFORE the signature, which covers the exact bytes sent. Slot 0 adds
# nothing: an absent host_share is a primary report, which is what every older kit sends.
[ "$K" = 0 ] || JSON="$(printf '%s' "$JSON" | sed -E 's#\}$#,"host_share":"secondary"}#')"

# THE JUDGE ROLE THIS IDENTITY DECLARES -- READ IN ITS CONTAINER, NEVER GUESSED HERE.
# The chain knows no judge role: it draws every present miner into a jury, and only a miner started with the
# role, whose judge worker runs and whose engine holds the model it judges with, posts a verdict. `can_judge`,
# from the probe above, says this machine has the RAM to judge; it does not say anything judges. Nobody could
# tell how many judges run before a jury formed.
# The word is read where the identity and the signature are: in THIS slot's miner container, by the container's
# own self-test (miner_selftest.py --judge-role) -- its environment is the one that started, or did not start,
# judge_worker.py, its processes are there, and its judge endpoint is the one it reaches (slot 0's ollama-cpu,
# or dendra-judge-cpu for a slot k). This host's .env is NOT that reading: a value edited after the container
# started, or a container started from another file, would publish a role nothing runs.
# active (requested, the worker runs, and its engine holds the model THAT worker resolved at its start, which is
# the chain's pin) | mute (requested, and the worker or that model missing: drawn, and silent) | off | unknown. Unknown
# when the container cannot be asked -- no docker, no running miner, an image older than --judge-role --, and
# never off: an answer nobody could read is not "no judge here". Added BEFORE the signature, which covers the
# exact bytes sent.
JUDGE_ROLE="unknown"; _JR_WHY=""
if [ -n "$_mc" ]; then
  _jr_out="$(docker exec -w /app "$_mc" python3 miner_selftest.py --judge-role </dev/null 2>/dev/null)"
  JUDGE_ROLE="$(_judge_role_word "$_jr_out")"
  _JR_WHY="$(printf '%s\n' "$_jr_out" | sed -n 's/^DENDRA_JUDGE_ROLE_WHY //p' | head -1 | tr -d '[:cntrl:]' | cut -c1-240)"
  [ -n "$_JR_WHY" ] || _JR_WHY="the container $_mc gave no readable answer (an image older than --judge-role?)"
elif ! command -v docker >/dev/null 2>&1; then
  _JR_WHY="docker is absent: the role is read in the miner container, never guessed on this host"
else
  _JR_WHY="no running miner container to read it in"
fi
JSON="$(printf '%s' "$JSON" | sed -E "s#\}\$#,\"judge_role\":\"$JUDGE_ROLE\"}#")"
say "  [i] judge role declared: $JUDGE_ROLE ($_JR_WHY)"
if [ "$JUDGE_ROLE" = mute ]; then
  _jr_slot=""; [ "$K" = 0 ] || _jr_slot=" --slot $K"
  say "      a MUTE seat: the chain draws this identity into juries and it says nothing. Its self-test names the"
  say "      remedy (check C11): bash $HERE/miner_health.sh$_jr_slot"
fi

# SIGNED, OR NOT SENT. The `verified` block of /capacity counts only reports whose signature goes back to
# the OPERATOR of the miner id they name (ADR-045 (10)): NAMING a staked id is not enough, and neither is a
# chosen `node_id` that looks like one. The registry still ACCEPTS an unsigned deposit, from a kit older
# than this rule; this kit no longer sends one: it would be filed beside the signed report of the same
# machine, under another key, and listed a second time.
# The body is signed EXACTLY as it leaves: the signature covers a digest of those bytes, so
# re-serialising between signing and sending produces a different body.
SIGN_ARGS=()
SIGN_KEY="$(sed -n 's/^DENDRA_SIGN_KEY=//p' "$ENVF" 2>/dev/null | head -1 | tr -d '[:space:]"')"
# THE KEY IS DERIVED FROM THE IDENTITY BEING CLAIMED, NOT READ FROM A FILE NOBODY WRITES.
# `deploy/join.sh` never emits DENDRA_SIGN_KEY into the .env it generates — zero occurrences in
# the whole file — so this sed returned empty for every operator who joined the documented way,
# the else-branch below fired, and the report went out UNSIGNED forever. The compose default at
# docker-compose.yml:146 does not help: it hands the value to the CONTAINER, while this script
# runs on the HOST and parses the file.
# The default is $IDENTITY and not $MINER_ID on purpose: the body one line above claims
# $IDENTITY, and a signature made under a different name proves nothing about the name being
# claimed. Deriving it here also means an install that is ALREADY running repairs itself on the
# next cron tick, with no re-run of join.sh — which a value frozen at join time could not do,
# since at that moment the alignment has not happened yet.
: "${SIGN_KEY:=$IDENTITY}"
SIGNEUR="$ROOT/services/capacity_sign.py"
# ONE PLACE READS THE REGISTRATION AND SIGNS: the miner container of THIS slot when one runs (found above by
# its labels; a miner installed the documented way runs in Docker, and its keyring lives in the container
# VOLUME, out of the host's reach), else THIS host when it carries the signer and a python3 (a bare-metal
# install, where the host keyring is the real one). The container needs nothing from the host: it carries its
# own python3, signer and dendrad -- which is why a host that "runs Docker and nothing else", the NOMINAL one,
# signs at all. The registration and the signature come from the same keyring, so a report goes out only
# under the key the chain records.
#
# REGISTERED WITH THIS KEY AS ITS OPERATOR, OR NOT SENT -- and read BEFORE signing (REG_PY, three states). A
# signature the chain cannot tie to this identity proves nothing: the registry would file the report as a
# declaration, beside the signed one the same machine sends once it is registered. Read first, a miner the
# chain does not record yet -- its account not funded, its create-miner not landed -- is said as such (exit 2),
# hourly until it is, rather than as a signature that failed.
#
# AND THE REPORT SEPARATES THREE FACTS, NOT TWO. "Signing failed" and "signing was never attempted"
# are different events, and the second one has a cause worth naming. Nothing is sent in either case, so
# this line is the only trace an operator ever gets: one that blames the identity for a missing python3
# sends them to re-check the one thing that was right.
#
# THE EMPTY-IDENTITY STATE IS NOT DECIDED HERE, AND A BRANCH FOR IT WOULD BE A LIE OF COVERAGE.
# $SIGN_KEY falls back to $IDENTITY, which falls back to $MINER_ID, and the `die "MINER_ID missing
# from ..."` at the top of this file has already refused to run without one. A "no identity to sign
# as" branch here could never fire; it would only tell a reader that this block handles a state it
# never sees, while the real refusal happens far earlier and says something else entirely.
_ON=""
if [ -n "$_mc" ]; then
  # The container of THIS slot (found above by its labels), never the first one a name pattern returns.
  _ON=container
elif [ -r "$SIGNEUR" ] && command -v python3 >/dev/null 2>&1; then
  _ON=host
fi
if [ -n "$_ON" ]; then
  _REG_OUT=""
  case "$_ON" in
    host)
      _REG_OUT="$(printf '%s\n' "$REG_PY" | ( cd "$ROOT/services" 2>/dev/null && DENDRA_NODE="$NODE" \
        python3 - "$IDENTITY" "$SIGN_KEY" ) 2>/dev/null)" ;;
    container)
      _REG_OUT="$(printf '%s\n' "$REG_PY" | docker exec -i -e DENDRA_NODE="$NODE" \
        -e DENDRA_KEYRING_DIR="${DENDRA_CONTAINER_KEYRING:-/data/keys/cosmos}" \
        -w /app "$_mc" python3 - "$IDENTITY" "$SIGN_KEY" 2>/dev/null)" ;;
  esac
  REGISTRATION="$(_registration_word "$_REG_OUT")"
  _REG_WHY="$(printf '%s\n' "$_REG_OUT" | sed -n 's/^DENDRA_REGISTRATION_WHY //p' | head -1 | tr -d '[:cntrl:]' | cut -c1-240)"
  _REG_WHERE="this host"; [ "$_ON" = container ] && _REG_WHERE="the container $_mc"
  case "$REGISTRATION" in
    registered)
      say "  [i] registration read on chain (in $_REG_WHERE): ${_REG_WHY:-registered}" ;;
    absent|other)
      say "  [i] NOT published: ${_REG_WHY:-the chain does not record $IDENTITY with this key as its operator} (read in $_REG_WHERE)."
      say "      The registry could not prove this report. The hourly job sends it at its first run after the"
      say "      registration; the miner's log says where the registration stands: docker compose -p dendra-miner logs miner"
      exit 2 ;;
    *)
      say "  [?] NOT published: whether the chain records $IDENTITY with this key as its operator was NOT READ"
      say "      in $_REG_WHERE (${_REG_WHY:-no readable answer}). Never read as registered: the hourly job asks again."
      if [ "$_ON" = host ]; then
        if command -v docker >/dev/null 2>&1; then say "      No miner container runs, and a Docker install keeps its keyring there: start the miner first."
        else say "      docker is absent here: a Docker install keeps its keyring in the miner container."; fi
      fi
      exit 3 ;;
  esac
fi
_SIGN_TRIED=""; _SIGN_WHY=""
if [ "$_ON" = host ]; then
  _SIGN_TRIED="this host"
  while IFS= read -r _l; do SIGN_ARGS+=("$_l"); done < <(
    printf '%s' "$JSON" | DENDRA_SIGN_KEY="$SIGN_KEY" MINER_ID="$IDENTITY" DENDRA_NODE="$NODE" \
      python3 "$SIGNEUR" 2>/dev/null || true)
elif [ "$_ON" = container ]; then
  _SIGN_TRIED="the container $_mc"
  while IFS= read -r _l; do SIGN_ARGS+=("$_l"); done < <(
    printf '%s' "$JSON" | docker exec -i \
      -e DENDRA_SIGN_KEY="$SIGN_KEY" -e MINER_ID="$IDENTITY" -e DENDRA_NODE="$NODE" \
      -e DENDRA_KEYRING_DIR="${DENDRA_CONTAINER_KEYRING:-/data/keys/cosmos}" \
      -w /app "$_mc" python3 capacity_sign.py 2>/dev/null || true)
  [ "${#SIGN_ARGS[@]}" -gt 0 ] && say "  [i] signed INSIDE the container $_mc (the keyring is there, not on this host)"
fi
if [ "${#SIGN_ARGS[@]}" -eq 0 ]; then
  # Name every route that could NOT be attempted, and why. A route that WAS attempted is reported
  # separately below: it failed, which is a different thing from never having run.
  if [ -z "$_ON" ]; then
    [ -r "$SIGNEUR" ] || _SIGN_WHY="${_SIGN_WHY:+$_SIGN_WHY; }the signer is not readable on this host ($SIGNEUR)"
    command -v python3 >/dev/null 2>&1 || _SIGN_WHY="${_SIGN_WHY:+$_SIGN_WHY; }this host has no python3 (normal for a Docker-only install)"
  fi
  if [ "$_ON" != container ]; then
    if ! command -v docker >/dev/null 2>&1; then
      _SIGN_WHY="${_SIGN_WHY:+$_SIGN_WHY; }docker is absent, so the container keyring is out of reach"
    else
      _SIGN_WHY="${_SIGN_WHY:+$_SIGN_WHY; }no running miner container to sign inside"
    fi
  fi
fi
if [ "${#SIGN_ARGS[@]}" -eq 0 ]; then
  say "  [STOP] report NOT SIGNED, so NOT published: an unsigned report is filed beside the signed one of this"
  say "         machine and listed twice, and it is never counted (capacity_server.py::storage_key)."
  [ -n "$_SIGN_TRIED" ] && say "      signing was ATTEMPTED on $_SIGN_TRIED and returned nothing (key, keyring or node)."
  [ -n "$_SIGN_WHY" ] && say "      signing was NOT attempted: $_SIGN_WHY."
  exit 1
fi
say "  [i] report SIGNED (${#SIGN_ARGS[@]} header arguments) -> it can enter the verified block"
CODE="$(printf '%s' "$JSON" | curl -s -m 15 -o /dev/null -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' "${SIGN_ARGS[@]}" --data-binary @- "$URL")"

if [ "$CODE" = "200" ]; then
  say "  [OK] inventory published -> $URL  (identity $IDENTITY)"
  # READ THE REGISTRY BACK: an accepted POST does not prove the node is COUNTED. `verified` counts only
  # reports the registry proves against ITS reading of the on-chain miner registry, which is not the
  # reading made above -- so the counter is shown, never assumed.
  #
  # ⚠️ THE READ-BACK IS EXTRA INFORMATION, NOT THE VERDICT ON THE PUBLICATION. It needs python3, and a
  # miner installed the documented way runs Docker and nothing else — that host is the NOMINAL case,
  # not an edge one. Left as the last command of the script under `pipefail`, its absence became the
  # exit status of a run that had just published: 127 in the cron log, hourly, for a node that was
  # correctly declared. What this script reports is what it PUBLISHED; a missing interpreter is a
  # missing line of output.
  # TWO READINGS, SAID APART. `verified.live_nodes` counts the signed reports (this file's); `onchain.present`
  # counts the registered miners with an accepted presence proof, read from the CHAIN by the registry service --
  # the count the public chat reads. A block that is not an object, or a count that is not a whole number, is
  # not read. Inside a `verified` block that WAS read, an omitted count is a zero (the rule of zero). An `onchain`
  # block that says `measured: true` is read exactly as the public chat page reads it: `present` must be a number,
  # finite and not negative, and is the reading; `registered`, when it is such a number too, must not be below
  # it, and when it is not, the reading stands without it.
  if command -v python3 >/dev/null 2>&1; then
    curl -s -m 15 "$URL" | python3 -c 'import json,sys
import math
try: d=json.load(sys.stdin)
except Exception: print("     (registry unreadable on read-back)"); raise SystemExit
if not isinstance(d, dict): print("     (registry answer is not an object on read-back)"); raise SystemExit
def whole(x): return isinstance(x, int) and not isinstance(x, bool) and x >= 0
def count(x): return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
v = d.get("verified")
n = v.get("live_nodes", 0) if isinstance(v, dict) else None
if whole(n): print("     verified.live_nodes = %d   <- signed capacity reports the registry counts (/network)" % n)
else: print("     verified.live_nodes not read (no verified block, or not a whole number)")
o = d.get("onchain")
p, g = (o.get("present"), o.get("registered")) if isinstance(o, dict) else (None, None)
if isinstance(o, dict) and o.get("measured") is True and count(p) and (not count(g) or p <= g):
    of = (" of %s registered" % g) if count(g) else " (registered not read)"
    print("     onchain.present = %s%s   <- read from the chain by the registry; the public chat reads it" % (p, of))
elif isinstance(o, dict) and o.get("measured") is False:
    print("     onchain not measured by the registry: %s" % str(o.get("why", ""))[:200])
elif "onchain" in d:
    print("     onchain block not read (malformed)")
else:
    print("     no onchain block: this registry service does not publish it, and the chat then reads verified.live_nodes")
if n == 0: print("     [!] verified.live_nodes is 0, though the report was signed and the chain read above names this key as",
                 "the operator: the registry reads the miner registry through a cache of its own. Read it again later:",
                 "curl -s " + (sys.argv[1] if len(sys.argv) > 1 else ""))' "$URL" 2>/dev/null || true
  else
    say "     (no python3 here -> read-back skipped. The publication above stands; to read the registry"
    say "      -- verified.live_nodes, and onchain.present that the chat reads: curl -s $URL )"
  fi
  exit 0
fi
say "  [!] publication REFUSED (HTTP $CODE) -> $URL"
say "      This node's hardware will not be in /network's capacity listing (the chat counts miners from the chain, not from this report)."
# AND IT EXITS NON-ZERO. A refusal that returns success is invisible to everything that schedules this
# script: cron mails nothing, a wrapper reads 0, and the node quietly drops off /network 24 h later —
# the exact silence the whole file exists to close.
exit 1
}

# ---------------------------------------------------------------- every slot, or the one asked for
# Each slot runs in its own subshell: a refusal (die, exit 1) stops THAT slot's report, never the others'.
# The exit code is the worst of the slots, by what it means and not by its number: a failure (1), then a
# registration not read (3), then a miner not registered (2), then published (0). A code outside these four is
# a failure. A kit with one identity (slot 0, no card named) prints exactly what it always printed; with
# several, every line carries its slot.
_rank(){ case "$1" in 0) echo 0 ;; 2) echo 1 ;; 3) echo 2 ;; *) echo 3 ;; esac; }
if [ -n "$ONLY" ]; then
  _st="$(slot_state "$ONLY")" || die "the env of slot $ONLY cannot be read"
  [ "$_st" = active ] || die "slot $ONLY is $_st: nothing published (bash $HERE/slots.sh list)"
  SLOTS="$ONLY"
else
  SLOTS="$(slot_ids --active)" || die "the slots of this kit cannot be read ($HERE/gpu): nothing published"
  [ -n "$SLOTS" ] || die "configuration not found: $(slot_env 0)"
fi
_n="$(printf '%s\n' $SLOTS | awk 'NF{n++} END{print n+0}')"
RC=0
for _k in $SLOTS; do
  if [ "$_n" = 1 ] && [ "$_k" = 0 ] && [ -z "$(slot_val 0 DENDRA_GPU_UUID 2>/dev/null)" ]; then
    ( publish_slot "$_k" ); _rc=$?
  else
    ( publish_slot "$_k" ) 2>&1 | sed "s/^/[slot $_k] /"; _rc=$?
  fi
  case "$_rc" in 0|1|2|3) : ;; *) _rc=1 ;; esac
  [ "$(_rank "$_rc")" -gt "$(_rank "$RC")" ] && RC="$_rc"
done
exit "$RC"
