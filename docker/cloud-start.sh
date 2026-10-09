#!/usr/bin/env bash
# cloud-start.sh -- the entry point of the single-container miner image for a RENTED GPU pod (RunPod, Vast:
# docker/Dockerfile.cloud, published as ghcr.io/dendranetwork/dendra-miner-cloud). Read deploy/cloud/README.md.
#
# WHAT IT RUNS, AND NOTHING ELSE: Ollama on 127.0.0.1 and ONE miner, which reads the chain from the network's
# public RPC. No judge: judging needs the RAM deploy/hw_probe.sh asks for (MOE_CPU_MIN_RAM_MB) and a second
# Ollama instance on the CPU, on a machine billed by the hour. Like any miner started without --judge, the pod
# can still be drawn into a jury (the chain draws from the miners it counts as alive, with no judge capability
# of its own), and it does not vote there. No node of its own. No inbound port: the miner only dials out, to
# the relay, the RPC and the faucet.
#
# EVERY REFUSAL COMES BEFORE ANYTHING STARTS, in this order:
#   1. the payout address, DENDRA_PAYOUT_ADDRESS: the PUBLIC address of a key that is NOT on this pod.
#      Checked by final_season_address.py::payable_address, through deploy/join.sh::payout_address_check:
#      one address one character off is refused, not paid to nobody. A value that looks like a recovery
#      phrase or a private key is refused WITHOUT being printed. Nothing here ever imports a key. On a
#      restart, the address of a key this pod's keyring already holds is refused too (cloud_pod_key).
#   2. a persistent volume at DENDRA_WORKSPACE (default /workspace): the miner's identity and stake live
#      there, and a pod without one loses them at its first restart. A mount point is not enough: one that
#      lives in memory (tmpfs, ramfs) or in the container's own layer (overlay) is refused as well.
#      DENDRA_ACCEPT_EPHEMERAL=1 accepts that.
#   3. ONE GPU: one miner serves one card, and the network pays a miner, not a card. A pod with more is
#      refused; DENDRA_ALLOW_IDLE_GPUS=1 accepts paying for the others. And a USABLE one: the testnet runs no
#      mining model on the CPU, so a pod mines only when deploy/hw_probe.sh --role says `miner`.
#   4. the network's identity, by deploy/join.sh's OWN functions, sourced in library mode -- the genesis
#      the network serves against docker/GENESIS_SHA256 baked into this image, the consensus epoch, the
#      kit version -- fail-closed exactly as `join.sh --remote-rpc`. One implementation, not two.
# Then: 5. Ollama on 127.0.0.1, the model deploy/hw_probe.sh picks for this card and the embedder, then the
# miner (/entrypoint.sh, the miner image's own), each in a PROCESS GROUP of its own (setsid), so that stopping
# one stops everything it started; 6. the payout declaration, made by the miner once it is registered, with a
# LOCK (DENDRA_PAYOUT_LOCK=1), as the identity's FIRST declaration -- the only one the programme locks from the
# machine's own key -- this script only WATCHES it, says "payout NOT declared" until the miner's
# record shows it, "NOT locked" for as long as the record does not say the programme locked it, and
# "REFUSED for good" once the miner has recorded that the programme refused the lock (nothing filed);
# 7. supervision: if Ollama or the miner stops, everything this script started stops, and so does this script.
# When this script is the container's entry point, its exit ends the container; started from a provider's
# on-start script instead, it ends the miner and Ollama but NOT the instance, which stays billed.
#
# THE KEYS ON A RENTED MACHINE. The key that signs the miner's work lives on the pod's volume, in clear, and
# the host can read it: it is a disposable key, funded by the faucet. What the season pays goes to the
# payout address, which is not here, and the lock keeps a copy of the pod's key from changing it later.
# Leaving the network from a pod is not automated in this image (deploy/cloud/README.md, "Leaving the
# network, and ending a rental"): a pod ended without leaving keeps its registration until the chain evicts it.
#
# Exit codes -- three answers, never two:
#   0  stopped on request (SIGTERM, SIGINT, SIGHUP)
#   1  a process died (Ollama or the miner), or a step that was started failed
#   2  refused, the reason named; nothing was started
#   3  not measurable: something this script needs in order to decide could not be read; nothing started
# A line `DENDRA_CLOUD_STATE=<refused|unmeasurable|running|died|stopped|failed>` says the same in a form a
# script can read.
set -u

CLOUD_WS="${DENDRA_WORKSPACE:-/workspace}"
CLOUD_REPO="${DENDRA_REPO:-/opt/dendra}"
# Test seams, like install.sh's: where the miner image's files sit. Never set them on a real pod.
CLOUD_APP="${DENDRA_APP_DIR:-/app}"
CLOUD_ENTRY="${DENDRA_MINER_ENTRYPOINT:-/entrypoint.sh}"
CLOUD_KEYS_LINK="${DENDRA_KEYS_LINK:-/data/keys}"
CLOUD_WATCH_S="${DENDRA_PAYOUT_WATCH_S:-600}"
CLOUD_OLLAMA_WAIT_S="${DENDRA_OLLAMA_WAIT_S:-180}"
# The public network's settings, as deploy/install.sh names them; CONFIG_URL points a pod at another network.
CLOUD_DEFAULT_CONFIG_URL="https://testnet-api.dendranetwork.com/network-info.txt"
CLOUD_UPDATE_HINT="redeploy this pod with the image that docker/CLOUD_IMAGE names on the main branch of https://github.com/DendraNetwork/dendra-network (the volume keeps the miner's identity and stake)"

cloud_say(){ printf '[cloud] %s\n' "$*"; }
cloud_state(){ printf 'DENDRA_CLOUD_STATE=%s\n' "$1"; }
cloud_refuse(){ printf '[cloud] REFUSED: %s\n' "$*" >&2; cloud_state refused; exit 2; }
cloud_unmeasurable(){ printf '[cloud] NOT MEASURABLE: %s\n' "$*" >&2; cloud_state unmeasurable; exit 3; }

# cloud_payout_state <keys dir> <payout address> -> ONE line, never a guess:
#   waiting                 the miner has not resolved its identity yet (it registers first)
#   none <id>               no declaration recorded for this identity
#   locked <id>             the programme ACCEPTED this address for this identity, and said it is locked
#   unlocked <id>           accepted, without the lock
#   lock_refused <id>       the programme REFUSED to lock this address for this identity: nothing was filed
#   other <id> <address>    the record names another address
#   unreadable <why>        a file is there and cannot be read: not a declaration, and not "none" either
# It reads the record the miner writes when the programme accepts a declaration
# (final_season_miner.py::record_declaration, modea/keyring.py::PAYOUT_RECORD), and, when that one does not
# name this address for this identity, the record of a refused lock (final_season_miner.py::LOCK_REFUSED,
# written by miner.py::maybe_declare_payout on a 409 "lock_refused"). The files are written by this
# kit, not by proto3: an absent "locked" is false because the miner writes it only on the programme's
# "locked": true, and an absent "miner_id" or "address" is the empty string, which matches nothing.
cloud_payout_state(){
  python3 -I -c '
import json, sys
keys, want = sys.argv[1], sys.argv[2].lower()
try:
    with open(keys + "/identite-resolue", encoding="utf-8") as f:
        ident = "".join(f.read().split())
except FileNotFoundError:
    print("waiting")
    sys.exit(0)
except OSError as e:
    print("unreadable identity (" + type(e).__name__ + ")")
    sys.exit(0)
if not ident.startswith("dm1"):
    print("waiting")
    sys.exit(0)
def record(name):
    """The record as an object, None when the file is absent; exits with "unreadable" otherwise."""
    try:
        with open(keys + "/" + name, encoding="utf-8") as f:
            rec = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print("unreadable record (" + type(e).__name__ + ")")
        sys.exit(0)
    if not isinstance(rec, dict):
        print("unreadable record (not an object)")
        sys.exit(0)
    return rec
rec = record("payout-declared.json")
if rec is not None and str(rec.get("miner_id") or "") == ident and str(rec.get("address") or "").lower() == want:
    print(("locked " if rec.get("locked") is True else "unlocked ") + ident)
    sys.exit(0)
# What the programme refused is read only when no accepted declaration names this address: a lock refused on
# top of an earlier declaration of it leaves that declaration standing, which is what is said above.
ref = record("payout-lock-refused.json")
if ref is not None and str(ref.get("miner_id") or "") == ident and str(ref.get("address") or "").lower() == want \
        and ref.get("refused") == "lock_refused":
    print("lock_refused " + ident)
elif rec is None or str(rec.get("miner_id") or "") != ident:
    print("none " + ident)
else:
    print("other " + ident + " " + str(rec.get("address") or ""))
' "$1" "$2" 2>/dev/null || printf 'unreadable record (python3 failed)\n'
}

# cloud_pod_key <keys dir> <address> -> ONE line, never a guess:
#   own <file>         the address is the address of a key this pod's keyring holds (<keys>/cosmos/keyring-*)
#   other              it is none of them, or there is no keyring yet (a first start: the miner creates it)
#   unreadable <why>   a keyring is there and its addresses cannot be read
# The keyring names each key's address in a file `<hex of the address>.address`, in clear in both backends:
# the address is read from that name, with the miner's own bech32 module (modea/cosmos_addr.py, under
# DENDRA_APP_DIR), and no key is opened.
cloud_pod_key(){
  python3 -I -c '
import os, sys
sys.path.insert(0, sys.argv[1])
keys, want = sys.argv[2], sys.argv[3].lower()
try:
    from modea import cosmos_addr
except Exception as e:
    print("unreadable (the address module could not be loaded: " + type(e).__name__ + ")")
    sys.exit(0)
root = os.path.join(keys, "cosmos")
if not os.path.isdir(root):
    print("other")
    sys.exit(0)
try:
    for d in sorted(os.listdir(root)):
        if not d.startswith("keyring-") or not os.path.isdir(os.path.join(root, d)):
            continue
        for f in sorted(os.listdir(os.path.join(root, d))):
            if not f.endswith(".address"):
                continue
            try:
                raw = bytes.fromhex(f[:-len(".address")])
            except ValueError:
                print("unreadable (" + d + "/" + f + " is not named by an address)")
                sys.exit(0)
            if not raw:
                print("unreadable (" + d + "/" + f + " is not named by an address)")
                sys.exit(0)
            if cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(raw, 8, 5)) == want:
                print("own " + d + "/" + f)
                sys.exit(0)
except OSError as e:
    print("unreadable (" + type(e).__name__ + ")")
    sys.exit(0)
print("other")
' "$CLOUD_APP" "$1" "$2" 2>/dev/null || printf 'unreadable (python3 failed)\n'
}

# cloud_payout_watch <keys dir> <address> -- prints the state until the record shows the declaration LOCKED,
# then stops. A declaration recorded WITHOUT the lock does not end the watch, and this says "NOT locked" at each
# pass: the programme grants the lock only on an identity's FIRST declaration (final_season_server.py), so an
# identity filed without it stays unlocked, and only a new one -- a new volume -- declares with the lock.
cloud_payout_watch(){
  local st own
  while :; do
    own="$(cloud_pod_key "$1" "$2")"
    case "$own" in
      own*)
        cloud_say "payout NOT declared, and it will not be: $2 is the address of a key this pod holds (${own#own }), which the host can read."
        cloud_say "  The miner refuses to lock it. Redeploy with DENDRA_PAYOUT_ADDRESS set to the public address of a key that is not on this pod."
        printf 'DENDRA_CLOUD_PAYOUT=refused_own_key\n'
        sleep "$CLOUD_WATCH_S"; continue ;;
    esac
    st="$(cloud_payout_state "$1" "$2")"
    case "$st" in
      "locked "*)
        cloud_say "payout declared and LOCKED: the Final Testnet Season pays ${st#locked } to $2, and refuses any other address for it."
        cloud_say "  That is what the programme at ${DENDRA_FINAL_SEASON_URL:-?} answered, as named by ${CLOUD_CONFIG_URL:-?}."
        case "${CLOUD_CONFIG_URL:-}" in
          http://*) cloud_say "  That address was read over plain HTTP, which authenticates nothing: the programme's published evidence (evidence/day-<N>.jsonl) shows whether the lock is filed." ;;
        esac
        printf 'DENDRA_CLOUD_PAYOUT=declared\n'; return 0 ;;
      "unlocked "*)
        cloud_say "payout declared to $2 for ${st#unlocked }, but NOT locked: a copy of this pod's key could still change it."
        cloud_say "  The programme locks an address only on an identity's FIRST declaration, and this one was filed without the lock: it stays unlocked. The miner's log says what the programme answered."
        printf 'DENDRA_CLOUD_PAYOUT=declared_unlocked\n'
        sleep "$CLOUD_WATCH_S"; continue ;;
      "lock_refused "*)
        cloud_say "payout NOT declared to $2 for ${st#lock_refused }, and it will not be with the lock: the programme REFUSED to lock it, for good, and filed nothing."
        cloud_say "  It locks an address only on an identity's FIRST declaration, and this identity has declared before without the lock. The season pays the address declared before (this pod's key if none), which a copy of this pod's key can still change."
        cloud_say "  Only a new identity -- a new volume -- declares with the lock. The miner's log says what the programme answered."
        printf 'DENDRA_CLOUD_PAYOUT=lock_refused\n'
        sleep "$CLOUD_WATCH_S"; continue ;;
      waiting)
        cloud_say "payout NOT declared: the miner has not registered yet. It funds itself at the faucet, then registers;"
        cloud_say "  a faucet pays a limited number of times per IP address, and a host shared by several pods can exhaust it."
        cloud_say "  The miner's log names what it waits for." ;;
      "none "*)
        cloud_say "payout NOT declared for ${st#none }: the miner declares it once registered and retries what can change; its log says why not yet." ;;
      "other "*)
        cloud_say "payout NOT declared to $2: the record names another address (${st##* }). If that address is locked, it cannot be changed." ;;
      *)
        cloud_say "payout NOT declared: $st" ;;
    esac
    printf 'DENDRA_CLOUD_PAYOUT=not_declared\n'
    sleep "$CLOUD_WATCH_S"
  done
}

# cloud_gpu_count -> the number of NVIDIA GPUs this pod sees, "none" without the NVIDIA tool, or "?" when the
# tool is there and its answer cannot be read. Three answers: an unread count is never zero.
cloud_gpu_count(){
  local out n bad
  command -v nvidia-smi >/dev/null 2>&1 || { printf 'none'; return 0; }
  out="$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null)" || { printf '?'; return 0; }
  n="$(printf '%s\n' "$out" | awk '/^[[:space:]]*[0-9]+[[:space:]]*$/ { n++ } END { print n + 0 }')"
  bad="$(printf '%s\n' "$out" | awk 'NF && !/^[[:space:]]*[0-9]+[[:space:]]*$/ { n++ } END { print n + 0 }')"
  if [ "$bad" != 0 ]; then printf '?'; else printf '%s' "$n"; fi
}

# The processes this script started, and how it stops them. Ollama and the miner each run in a PROCESS GROUP
# of their own, started by setsid, whose number is their pid: the miner image's entry point is a shell that
# starts miner.py (and its workers) in the background and waits for it, so a signal to that shell alone
# kills the shell and leaves the miner running, out of sight. The GROUP is signalled, and waited on until no
# process of it is left -- a zombie is not a process left.
CLOUD_OLLAMA_PID=""; CLOUD_MINER_PID=""; CLOUD_WATCH_PID=""; CLOUD_PULL_PID=""
cloud_leads_group(){ # cloud_leads_group <pid> -> 0 when <pid> is the leader of its own process group
  local line rest st pp pg more
  IFS= read -r line 2>/dev/null < "/proc/$1/stat" || return 1
  rest="${line##*) }"                  # the fields after the command name, which may hold spaces
  read -r st pp pg more <<< "$rest"
  [ "$pg" = "$1" ]
}
cloud_group_alive(){ # cloud_group_alive <pgid> -> 0 while a process of that group lives
  local f line rest st pp pg more
  for f in /proc/[0-9]*/stat; do
    IFS= read -r line 2>/dev/null < "$f" || continue
    rest="${line##*) }"
    read -r st pp pg more <<< "$rest"
    [ "$pg" = "$1" ] && [ "$st" != Z ] && return 0
  done
  return 1
}
cloud_own_group(){ # cloud_own_group <pid> <name> -- waits until setsid made <pid> a group leader
  local i
  for i in $(seq 1 50); do
    cloud_leads_group "$1" && return 0
    kill -0 "$1" 2>/dev/null || return 0     # already gone: the supervision below says so
    sleep 0.1
  done
  cloud_fail "$2 did not start in a process group of its own: stopping it could leave its children running."
}
cloud_stop_children(){
  local p i alive
  for p in $CLOUD_PULL_PID $CLOUD_WATCH_PID; do kill -TERM "$p" 2>/dev/null; done
  for p in $CLOUD_MINER_PID $CLOUD_OLLAMA_PID; do kill -TERM -- "-$p" 2>/dev/null; done
  for i in 1 2 3 4 5 6 7 8 9 10; do
    alive=""
    for p in $CLOUD_PULL_PID $CLOUD_WATCH_PID; do kill -0 "$p" 2>/dev/null && alive=1; done
    for p in $CLOUD_MINER_PID $CLOUD_OLLAMA_PID; do cloud_group_alive "$p" && alive=1; done
    [ -z "$alive" ] && return 0
    sleep 1
  done
  cloud_say "a process was still running 10 s after being asked to stop: killed."
  for p in $CLOUD_PULL_PID $CLOUD_WATCH_PID; do kill -KILL "$p" 2>/dev/null; done
  for p in $CLOUD_MINER_PID $CLOUD_OLLAMA_PID; do kill -KILL -- "-$p" 2>/dev/null; done
  return 0
}
# Started from a provider's on-start script rather than as the container's entry point, this script's exit
# stops what it started and NOT the instance: said, since the instance is what is billed.
cloud_instance_note(){
  [ "$$" = 1 ] && return 0
  cloud_say "this script is not the container's first process (PID $$): the miner and Ollama are stopped, the INSTANCE is not -- stop it from the provider's console, or it stays billed."
}
cloud_on_signal(){
  cloud_say "stop requested: stopping the miner and Ollama."
  cloud_stop_children
  cloud_state stopped
  exit 0
}
cloud_fail(){
  printf '[cloud] FAILED: %s\n' "$*" >&2
  cloud_stop_children
  cloud_instance_note
  cloud_state failed
  exit 1
}

# LIBRARY MODE: `DENDRA_CLOUD_LIB=1 . docker/cloud-start.sh` defines the functions above and stops here, so a
# bench drives the code that ships rather than a copy of it.
case "${DENDRA_CLOUD_LIB:-0}" in 1) return 0 2>/dev/null || exit 0;; esac

[ $# -eq 0 ] || cloud_refuse "unknown argument(s): $* -- this entry point reads its settings from the environment (deploy/cloud/README.md)"
# `wait -n` with a list of processes and `-p` exist from bash 5.1; without them the supervision below cannot
# tell which process stopped.
if [ "${BASH_VERSINFO[0]:-0}" -lt 5 ] || { [ "${BASH_VERSINFO[0]}" -eq 5 ] && [ "${BASH_VERSINFO[1]:-0}" -lt 1 ]; }; then
  cloud_unmeasurable "bash ${BASH_VERSION:-?} cannot supervise the processes (bash 5.1 or later is needed)"
fi
trap cloud_on_signal TERM INT HUP

# ── 1. the payout address ─────────────────────────────────────────────────────────────────────────────
CLOUD_PAYOUT="${DENDRA_PAYOUT_ADDRESS:-}"
[ -n "$CLOUD_PAYOUT" ] || cloud_refuse "no payout address. Set DENDRA_PAYOUT_ADDRESS to the PUBLIC address (dendra1...) of a key that is NOT on this pod: the Final Testnet Season pays there. Without it, it would pay this pod's own key, which the host of a rented machine can read."
case "$CLOUD_PAYOUT" in
  *[[:space:]]*) cloud_refuse "DENDRA_PAYOUT_ADDRESS holds several words: it looks like a RECOVERY PHRASE, which never belongs on a rented machine. Its value is not printed. If it is a real phrase, treat it as exposed and move what it holds." ;;
esac
if printf '%s\n' "$CLOUD_PAYOUT" | grep -Eqx '(0x)?[0-9A-Fa-f]{64}'; then
  cloud_refuse "DENDRA_PAYOUT_ADDRESS looks like a PRIVATE KEY, which never belongs on a rented machine. Its value is not printed. If it is a real key, treat it as exposed and move what it holds."
fi
case "$CLOUD_PAYOUT" in
  *[!A-Za-z0-9]*) cloud_refuse "DENDRA_PAYOUT_ADDRESS holds characters no address has (its value is not printed): an address is dendra1 followed by letters and digits." ;;
esac

# ── 2. the persistent volume ──────────────────────────────────────────────────────────────────────────
if [ "${DENDRA_ACCEPT_EPHEMERAL:-0}" = 1 ]; then
  cloud_say "DENDRA_ACCEPT_EPHEMERAL=1: $CLOUD_WS is NOT checked for persistence. Whatever the disk does not keep -- the"
  cloud_say "  miner's key, its identity, its stake -- is lost when the pod's disk is, and a new identity starts from zero."
elif [ ! -d "$CLOUD_WS" ]; then
  cloud_refuse "$CLOUD_WS does not exist. Attach a persistent volume there (RunPod: volume mount path; Vast: a volume, or set DENDRA_WORKSPACE to where yours is mounted), or set DENDRA_ACCEPT_EPHEMERAL=1 to accept losing the miner's identity with the pod."
elif ! command -v mountpoint >/dev/null 2>&1; then
  cloud_unmeasurable "the mountpoint tool is not in this image, so whether $CLOUD_WS is a persistent volume cannot be told. Set DENDRA_ACCEPT_EPHEMERAL=1 to go on without knowing."
else
  mountpoint -q "$CLOUD_WS"; _rc=$?
  [ "$_rc" = 0 ] || cloud_refuse "$CLOUD_WS is not a mounted volume (mountpoint: $_rc): the pod's own disk does not survive every stop, and the miner's identity and stake would go with it. Attach a volume at $CLOUD_WS, set DENDRA_WORKSPACE to where yours is mounted, or set DENDRA_ACCEPT_EPHEMERAL=1."
  # A mount point is not yet a volume: a tmpfs is a mount point that lives in memory, and the container's own
  # root (DENDRA_WORKSPACE=/) is a mount point of the container's layer. Neither survives the pod.
  command -v stat >/dev/null 2>&1 || cloud_unmeasurable "the stat tool is not in this image, so what $CLOUD_WS is mounted on cannot be told. Set DENDRA_ACCEPT_EPHEMERAL=1 to go on without knowing."
  _fs="$(stat -f -c %T "$CLOUD_WS" 2>/dev/null)"
  case "$_fs" in
    "") cloud_unmeasurable "the filesystem of $CLOUD_WS cannot be read (stat -f), so whether it survives the pod cannot be told. Set DENDRA_ACCEPT_EPHEMERAL=1 to go on without knowing." ;;
    tmpfs|ramfs|overlayfs|overlay|aufs)
      cloud_refuse "$CLOUD_WS is mounted on $_fs, which lives in memory or in the container's own layer: the miner's identity and stake would not survive the pod. Attach a persistent volume at $CLOUD_WS, set DENDRA_WORKSPACE to where yours is mounted, or set DENDRA_ACCEPT_EPHEMERAL=1." ;;
  esac
  cloud_say "$CLOUD_WS: a mounted volume ($_fs)."
fi
# What stops the miner and everything it starts: setsid, and the process table it is read back from.
command -v setsid >/dev/null 2>&1 || cloud_unmeasurable "setsid (util-linux) is not in this image: the processes this script starts could not be stopped as a whole."
[ -r /proc/self/stat ] || cloud_unmeasurable "/proc is not readable here: the processes this script starts could not be watched as a whole."
[ "$$" = 1 ] || cloud_say "this script is not the container's first process (PID $$): when it stops, the miner and Ollama stop with it, and the instance does not."

# ── 3. one GPU ────────────────────────────────────────────────────────────────────────────────────────
CLOUD_GPUS="$(cloud_gpu_count)"
case "$CLOUD_GPUS" in
  none) cloud_say "no NVIDIA tool in this pod: the role check below decides (no mining model runs on the CPU)." ;;
  "?")
    if [ "${DENDRA_ALLOW_IDLE_GPUS:-0}" = 1 ]; then
      cloud_say "the number of GPUs could not be read (nvidia-smi gave no usable answer); DENDRA_ALLOW_IDLE_GPUS=1: going on."
    else
      cloud_unmeasurable "nvidia-smi is here and its GPU list could not be read, so whether this pod pays for cards that would stay idle cannot be told. Set DENDRA_ALLOW_IDLE_GPUS=1 to go on without knowing."
    fi ;;
  0) cloud_say "nvidia-smi lists no GPU: the role check below decides (no mining model runs on the CPU)." ;;
  1) cloud_say "one GPU: one miner." ;;
  *)
    if [ "${DENDRA_ALLOW_IDLE_GPUS:-0}" = 1 ]; then
      cloud_say "$CLOUD_GPUS GPUs and ONE miner (DENDRA_ALLOW_IDLE_GPUS=1): the network pays a miner, not a card, so the other cards add nothing."
    else
      cloud_refuse "this pod has $CLOUD_GPUS GPUs and this image runs ONE miner: the network pays a miner, not a card, so you would pay for cards that add nothing. Rent a single-GPU pod, or set DENDRA_ALLOW_IDLE_GPUS=1."
    fi ;;
esac
# THE ROLE, BY THE ONE DECISION THE KIT APPLIES EVERYWHERE: deploy/hw_probe.sh --role (baked into this image next to
# join.sh). The testnet runs no mining model on the CPU, and this image runs no judge (see the header), so a pod
# mines only when the probe says `miner`: `judge` and `refused` are refused here with the probe's reason, and an
# unknown answer -- no reading, a RAM it could not read -- decides nothing, so nothing starts.
CLOUD_PROBE="$CLOUD_REPO/deploy/hw_probe.sh"
[ -r "$CLOUD_PROBE" ] || cloud_unmeasurable "this image is incomplete: $CLOUD_PROBE is what decides whether this pod can mine, and it is not there."
_rout="$(bash "$CLOUD_PROBE" --role 2>/dev/null)"; _rrc=$?
_role="$(printf '%s\n' "$_rout" | head -1 | tr -d '[:space:]')"
_rwhy="$(printf '%s\n' "$_rout" | tail -n +2)"
case "$_rrc:$_role" in
  0:miner) cloud_say "role: miner (deploy/hw_probe.sh --role): ${_rwhy:-a usable NVIDIA card}" ;;
  0:judge) cloud_refuse "no usable GPU in this pod (deploy/hw_probe.sh --role: judge): the testnet runs no mining model on the CPU, and this image runs no judge. ${_rwhy} Rent a pod with an NVIDIA GPU; a machine of your own without one joins as a judge with the compose kit (deploy/join.sh)." ;;
  0:refused) cloud_refuse "this pod has no role on the testnet (deploy/hw_probe.sh --role: refused): ${_rwhy} Rent a pod with an NVIDIA GPU." ;;
  *) cloud_unmeasurable "whether this pod can mine could not be decided (deploy/hw_probe.sh --role exit $_rrc, answer '${_role:-<empty>}'): ${_rwhy:-no reason given}" ;;
esac

# ── 4. the network's identity, by deploy/join.sh's own functions ─────────────────────────────────────
if [ ! -r "$CLOUD_REPO/deploy/join.sh" ] || [ ! -d "$CLOUD_REPO/deploy/testnet-node" ]; then
  cloud_unmeasurable "this image is incomplete: $CLOUD_REPO/deploy/join.sh (and its tree) is what checks the network, and it is not there."
fi
CLOUD_CONFIG_URL="${CONFIG_URL:-$CLOUD_DEFAULT_CONFIG_URL}"
# join.sh reads CONFIG_URL and its own arguments while it is sourced: neither may reach it.
unset CONFIG_URL
export DENDRA_REPO="$CLOUD_REPO"
set --
DENDRA_JOIN_LIB=1 . "$CLOUD_REPO/deploy/join.sh" || cloud_unmeasurable "deploy/join.sh could not be loaded from $CLOUD_REPO."
# A refusal inside join.sh is a refusal here: code 2, and the update path of a POD rather than of a clone.
die(){
  printf '[join] REFUSED: %s\n' "$*" >&2
  cloud_say "On a pod there is no clone to update: to run current code, $CLOUD_UPDATE_HINT."
  cloud_state refused
  exit 2
}
# The pod reads the chain from the network's public RPC, exactly as `join.sh --remote-rpc`: the genesis check
# then FAILS CLOSED when the network serves none to compare.
OWN_NODE=0

_pwhy="$(payout_address_check "$CLOUD_PAYOUT")"; _prc=$?
case "$_prc" in
  0) CLOUD_PAYOUT="$(printf '%s' "$CLOUD_PAYOUT" | tr 'A-Z' 'a-z')"
     cloud_say "payout address $CLOUD_PAYOUT: checksum verified, payable." ;;
  1) cloud_refuse "DENDRA_PAYOUT_ADDRESS is not a payable address: ${_pwhy:-refused}. Nothing was started." ;;
  *) cloud_unmeasurable "the payout address cannot be verified in this image: ${_pwhy:-unknown}." ;;
esac

if ! _load_config_url "$CLOUD_CONFIG_URL" soft; then
  cloud_unmeasurable "the network's settings could not be read from $CLOUD_CONFIG_URL: nothing can be checked against them."
fi
for _k in DENDRA_NODE DENDRA_RELAY FAUCET; do
  [ -n "${!_k:-}" ] || cloud_refuse "the network's settings ($CLOUD_CONFIG_URL) name no $_k: this pod would not know where to mine."
done
[ -n "${DENDRA_FINAL_SEASON_URL:-}" ] || cloud_refuse "the network's settings ($CLOUD_CONFIG_URL) name no Final Testnet Season programme (DENDRA_FINAL_SEASON_URL): the payout address could not be declared, and the season would pay this pod's own key."
case "$DENDRA_NODE" in
  tcp://*) _rpc="http://${DENDRA_NODE#tcp://}" ;;
  http://*|https://*) _rpc="$DENDRA_NODE" ;;
  *) cloud_refuse "the network's RPC is not an address this pod can dial: $DENDRA_NODE" ;;
esac
if ! curl -fsS -m 10 "$_rpc/status" >/dev/null 2>&1; then
  cloud_unmeasurable "the network's RPC ($DENDRA_NODE) does not answer: the chain cannot be read from here."
fi
cloud_say "network settings read from $CLOUD_CONFIG_URL; RPC $DENDRA_NODE answers."
# Every "Update:" line join.sh prints -- genesis and kit alike -- names the update path of a POD, never git.
DENDRA_UPDATE_HINT="$CLOUD_UPDATE_HINT"
verify_genesis_info
verify_consensus_epoch
verify_kit_version

# ── the volume, linked where the miner image keeps its keys ──────────────────────────────────────────
CLOUD_DATA="$CLOUD_WS/dendra"
CLOUD_KEYS="$CLOUD_DATA/keys"
( umask 077; mkdir -p "$CLOUD_KEYS" "$CLOUD_DATA/ollama" ) || cloud_refuse "$CLOUD_DATA cannot be created: the volume is not writable."
chmod 700 "$CLOUD_KEYS" 2>/dev/null
if [ -L "$CLOUD_KEYS_LINK" ]; then
  ln -sfn "$CLOUD_KEYS" "$CLOUD_KEYS_LINK" || cloud_fail "$CLOUD_KEYS_LINK cannot point at $CLOUD_KEYS"
elif [ -d "$CLOUD_KEYS_LINK" ]; then
  # A directory that holds files would be HIDDEN by the link: keys made on the pod's own disk earlier.
  if [ -n "$(ls -A "$CLOUD_KEYS_LINK" 2>/dev/null)" ]; then
    cloud_refuse "$CLOUD_KEYS_LINK already holds files, on the pod's own disk: they may be a miner's keys, and linking the volume there would hide them. Move them to $CLOUD_KEYS first."
  fi
  rmdir "$CLOUD_KEYS_LINK" && ln -s "$CLOUD_KEYS" "$CLOUD_KEYS_LINK" || cloud_fail "$CLOUD_KEYS_LINK cannot point at $CLOUD_KEYS"
elif [ -e "$CLOUD_KEYS_LINK" ]; then
  cloud_refuse "$CLOUD_KEYS_LINK exists and is neither a link nor a directory."
else
  mkdir -p "$(dirname "$CLOUD_KEYS_LINK")" && ln -s "$CLOUD_KEYS" "$CLOUD_KEYS_LINK" || cloud_fail "$CLOUD_KEYS_LINK cannot point at $CLOUD_KEYS"
fi
# On a restart the volume holds the pod's own keyring: a payout address that is one of ITS keys would be locked
# for good on a key the host can read. Refused before anything starts; on a first start there is no keyring
# yet, and the miner refuses to lock its own key itself.
_own="$(cloud_pod_key "$CLOUD_KEYS" "$CLOUD_PAYOUT")"
case "$_own" in
  own*) cloud_refuse "DENDRA_PAYOUT_ADDRESS is the address of a key this pod's keyring holds (${_own#own }): the host of a rented machine can read that key, and the lock would hold that address for good. Set DENDRA_PAYOUT_ADDRESS to the public address of a key that is NOT on this pod. Nothing was started." ;;
  other) : ;;
  *) cloud_unmeasurable "whether DENDRA_PAYOUT_ADDRESS is a key of this pod cannot be told: $_own. Nothing was started." ;;
esac

# ── 5. Ollama on 127.0.0.1, the model, then the miner ────────────────────────────────────────────────
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_MODELS="$CLOUD_DATA/ollama"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-1h}"
setsid ollama serve &
CLOUD_OLLAMA_PID=$!
cloud_own_group "$CLOUD_OLLAMA_PID" "Ollama"
_up=0
for _i in $(seq 1 "$CLOUD_OLLAMA_WAIT_S"); do
  if ollama list >/dev/null 2>&1; then _up=1; break; fi
  kill -0 "$CLOUD_OLLAMA_PID" 2>/dev/null || cloud_fail "Ollama stopped before it answered."
  sleep 1
done
[ "$_up" = 1 ] || cloud_fail "Ollama did not answer on $OLLAMA_HOST."
for _m in "$DENDRA_MODEL_ID" "$DENDRA_EMBED_API_MODEL"; do
  cloud_say "pulling $_m into $OLLAMA_MODELS (once; the volume keeps it). The pod is billed while it downloads."
  ollama pull "$_m" &
  CLOUD_PULL_PID=$!
  wait "$CLOUD_PULL_PID" || cloud_fail "the download of $_m failed."
  CLOUD_PULL_PID=""
done

# The identity the miner resolved on an earlier start, when there is one: its name in the logs from the start.
_id="$(tr -d '[:space:]' < "$CLOUD_KEYS/identite-resolue" 2>/dev/null)"
case "$_id" in dm1*) MINER_ID="$_id" ;; esac
# What the miner's container receives in the compose kit, set here by name: the same variables, with the
# lock asked for (DENDRA_PAYOUT_LOCK=1) and no judge.
CLOUD_MINER_ENV=(
  DENDRA_NODE="$DENDRA_NODE" DENDRA_RELAY="$DENDRA_RELAY" FAUCET="$FAUCET"
  DENDRA_FAUCET_POW_MAX_S="${DENDRA_FAUCET_POW_MAX_S:-300}"
  OLLAMA_ENDPOINT="http://127.0.0.1:11434" DENDRA_MODEL_ID="$DENDRA_MODEL_ID" OLLAMA_MODEL="$DENDRA_MODEL_ID"
  BACKEND=ollama DENDRA_MINER_STAKE="${DENDRA_MINER_STAKE:-}"
  DENDRA_KEYRING_DIR="$CLOUD_KEYS_LINK/cosmos" DENDRA_SIGN_KEY="$MINER_ID"
  DENDRA_PAYOUT_ADDRESS="$CLOUD_PAYOUT" DENDRA_PAYOUT_LOCK=1 DENDRA_MINER_OWNER=
  DENDRA_FINAL_SEASON_URL="$DENDRA_FINAL_SEASON_URL"
  DENDRA_EMBED_MODE=backend DENDRA_EMBED_API_MODEL="$DENDRA_EMBED_API_MODEL"
  DENDRA_MINER_JUDGE=0
)
( cd "$CLOUD_APP" && exec setsid env "${CLOUD_MINER_ENV[@]}" "$CLOUD_ENTRY" miner "$MINER_ID" ) &
CLOUD_MINER_PID=$!
cloud_own_group "$CLOUD_MINER_PID" "the miner"
cloud_say "miner started for the network of $CLOUD_CONFIG_URL (model $DENDRA_MODEL_ID, no judge)."
cloud_state running

# ── 6. the payout declaration, watched ───────────────────────────────────────────────────────────────
cloud_payout_watch "$CLOUD_KEYS" "$CLOUD_PAYOUT" &
CLOUD_WATCH_PID=$!

# ── 7. supervision: the first of Ollama or the miner to stop stops the pod ─────────────────────────────
wait -n -p _dead "$CLOUD_OLLAMA_PID" "$CLOUD_MINER_PID"
_rc=$?
case "${_dead:-}" in
  "$CLOUD_OLLAMA_PID") _who="Ollama" ;;
  "$CLOUD_MINER_PID") _who="the miner" ;;
  *) _who="a supervised process" ;;
esac
printf '[cloud] %s stopped (exit %s): stopping the pod, so that it does not stay billed while serving nothing. Its log above says why.\n' "$_who" "$_rc" >&2
cloud_stop_children
cloud_instance_note
cloud_state died
exit 1
