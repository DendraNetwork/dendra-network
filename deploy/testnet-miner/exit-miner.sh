#!/usr/bin/env bash
# exit-miner.sh -- take THIS miner off the Dendra network: deregister it on chain (`delete-miner`) and
# have the chain return its remaining stake to the key that registered it (its owner).
#
# WHAT IT DOES, IN THIS ORDER:
#   1. reads the miner's identity from its own volume (the identifier the daemon resolved, and the
#      address of the key that registered it), then its registration and stake from the chain;
#   2. with --yes only: sets the DRAIN (below), so that the miner takes no new work and no new jury seat
#      while the ones it holds resolve;
#   3. asks the chain whether it would accept the exit: a SIMULATION (`--dry-run`), which changes nothing.
#      While the chain refuses, it lists what it can read of what holds the miner -- its seats on open audit
#      juries, its own jobs under audit -- and, when the chain bounds them, the height from which they are
#      unwound with no verdict. With --wait it asks again, once a minute, until the chain accepts;
#   4. with --yes only: stops the miner (a running daemon registers again, and re-stakes, as soon as it
#      sees itself absent), simulates again, broadcasts `delete-miner`, waits for the transaction to be
#      INCLUDED, then reads the chain back: the miner must be gone and the balance must have risen.
#
# IT CHANGES NOTHING WITHOUT --yes, save --undrain, which is a request of its own. Run it once without the
# flag: it prints what the chain answers.
#
# THE CHAIN MAY REFUSE, AND THAT IS NOT A FAILURE OF THIS FILE. `DeleteMiner` refuses while the miner holds
# an obligation: a fee retained on a job it served, a seat on an audit committee that has not resolved yet
# (a seat holds it while it stays eligible as a juror, juror_freshness_blocks after its last commit), a job
# under dispute, or a job it answered that is not settled yet. The rule is
# chain/x/jobs/keeper/miner_vitality.go::hasOpenObligation, called by
# chain/x/jobs/keeper/msg_server_miner.go::DeleteMiner. The refusal is the chain's own message, printed
# as it is; the exit becomes possible once that instance is closed. It is a reading to repeat later, not a
# breakage. A refusal met AFTER this file stopped a RUNNING miner STARTS IT AGAIN: a stopped miner cannot file
# the reveal a job of its own under audit may be waiting for, and that reveal is one of the obligations that
# keep the exit refused (restart_after_refusal). A miner that was already stopped when this file ran stays
# stopped -- save in owner mode with --signed, where the stop was this file's own, made by the run that
# prepared the owner's transaction.
#
# THE DRAIN. A running miner is drawn onto new audit juries, and each seat holds it until that audit
# resolves: a miner that keeps mining can stay refused for as long as it runs. --yes therefore sets the drain
# FIRST: a file named `drain` in the miner's key directory (the one that holds availability-last.json, in
# its miner-keys volume). While the file is there the daemon proves no presence and takes no new work, and it
# keeps filing the reveals its own audited jobs wait for; its heartbeat then says `draining`. A miner without
# a presence proof is drawn onto no new jury once availability is measured on the chain
# (avail_epoch_blocks above 0, chain/x/jobs/keeper/presence.go::minerPresentAt). Keep a draining miner
# RUNNING. The marker stays until the exit is verified -- this file then removes it, so that a miner started
# again on these keys takes work -- or until --undrain removes it. A marker left by an earlier run on a miner
# the chain no longer lists (its exit taken another way) is said, and removed with --yes once the miner is
# stopped: a draining daemon does not register. A miner of an earlier kit does not know the drain: this file
# says so when the heartbeat carries no drain state, and such a miner is stopped instead (the procedure below).
# A miner that proves no presence -- draining, or stopped -- is exposed to the availability slash where the
# chain arms it (chain/x/jobs/keeper/availability.go::runAvailabilitySlash): beside a refusal this file
# reads those parameters and says whether it is armed.
#
# AFTER IT: the stake is back on the address that registered the miner, minus whatever was slashed earlier
# (that part was never refundable). The miner stays STOPPED. Starting it again -- `docker compose up -d` in
# this directory, or the Dendra application's Start button -- registers it again and stakes again (in owner
# mode: prints the registration its owner signs again). To remove
# the kit from this machine afterwards: bash deploy/uninstall.sh. The exit is recorded in miner-health.EXITED
# here, so that the hourly self-test (miner_health.sh) does not report the stopped miner as a problem.
#
# WHERE IT RUNS: on the host that runs the miner kit, from any directory. Every chain read and the
# transaction run in a one-off container of the miner's own image (`docker compose run --rm --no-deps`),
# which carries dendrad, the keyring volume, the passphrase of an encrypted keyring (mounted read-only, as
# for the miner) and the network the miner reads the chain on -- this host needs Docker and nothing else,
# not even python3 or jq. The keyring is opened by the miner's own module (modea/keyring.py): the backend
# from the state of the volume, the passphrase on dendrad's stdin, never in its argv. The program that runs in
# that container is part of this file, and the modules it calls come from the same tree: when this kit sits in
# a clone, the clone's services/modea is mounted read-only into the container and read first -- an
# image built from an earlier tree may not carry modea/keyring.py at all. Outside a clone, the image's own
# modules are used, and this file says so.
#
# OWNER MODE (deploy/join.sh --owner): the miner was registered by its OWNER, a key that is not on this
# machine, and the chain accepts delete-miner from that key only -- the stake goes back to it. This file then
# never broadcasts with this machine's key. With --yes it sets the drain, stops the miner, simulates the exit
# FROM THE OWNER'S ADDRESS, writes the unsigned delete-miner next to this file (delete-miner.json) and prints
# the command that signs it, offline, on the owner's machine (modea/owner_tx.py builds both); run again with
# --yes --signed <file>, it checks that the file is the owner's signed delete-miner for THIS miner, broadcasts
# it, and verifies the exit as above, the stake back on the OWNER's address.
#
# ONE IDENTITY PER CARD (deploy/join.sh --gpus): each card is a miner identity of its own, a "slot"
# (deploy/testnet-miner/slots.sh list). --slot <k> names the identity this file acts on: its keys, its
# containers, its marker. On such a machine --yes WITHOUT --slot is refused -- an irreversible step is never
# taken on slot 0 by default -- and a run without --yes and without --slot simulates the exit of every slot.
# Leaving with a slot k also marks it retired (deploy/join.sh then leaves it stopped on a plain re-run).
#
# Usage:
#   bash deploy/testnet-miner/exit-miner.sh          # read and simulate, change nothing
#   bash deploy/testnet-miner/exit-miner.sh --yes    # drain, then leave if the chain accepts (owner mode: prepare)
#   bash deploy/testnet-miner/exit-miner.sh --yes --wait   # drain, ask the chain again until it accepts, leave
#   bash deploy/testnet-miner/exit-miner.sh --yes --signed delete-miner.signed.json   # owner mode: send it
#   bash deploy/testnet-miner/exit-miner.sh --slot 2 [--yes]   # the identity of the card of slot 2
#   bash deploy/testnet-miner/exit-miner.sh --undrain      # remove the drain: the miner takes work again
#   bash deploy/testnet-miner/exit-miner.sh --status       # one line per identity, for deploy/uninstall.sh:
#        registration slot=<k> state=<registered|absent|unread> id=<id> stake=<udndr> why=<text>
#
# A MACHINE WITHOUT A USABLE NVIDIA GPU, REGISTERED UNDER AN EARLIER KIT (a CPU machine below the judge's RAM
# floor). The current kit runs no mining model on the CPU: deploy/hw_probe.sh --role answers `refused` for such a
# machine, and deploy/join.sh then changes nothing -- the miner it runs keeps its stake locked and keeps being
# drawn onto audit juries it does not judge. To take it off the network and get the stake back:
#   1. In the clone (cd ~/dendra-network when the installer made it), bring it to the current release:
#        git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main
#      Do not run deploy/join.sh on this machine: it refuses it and changes nothing.
#   2. Read what holds it, changing nothing -- the miner keeps running:
#        bash deploy/testnet-miner/exit-miner.sh
#      While the chain refuses, it names the open audits the miner sits on, the jobs of its own under audit
#      and, when the chain bounds them, the height from which they are unwound with no verdict. It says too
#      whether the chain measures availability, and whether it slashes a miner that proves no presence.
#   3. If it names a job of the miner's OWN under audit, keep the miner running until its log shows that job
#      revealed (a line "[reveal] ... revealed <job>"; a "[reveal]" line that names the job without it says
#      why it was not, and a miner that cannot reveal gains nothing by running longer):
#        docker compose -p dendra-miner logs miner | grep reveal
#      A stopped miner files no reveal. Without it, that audit concludes AGAINST the miner -- a slash -- when
#      enough judges vote "0" on the missing reveal: the kit's judge abstains on it by default
#      (services/judge_worker.py::NOREVEAL_ABSTAIN, DENDRA_JUDGE_NOREVEAL_ABSTAIN), and nothing makes
#      every judge do so. An audit that concludes neither way is unwound (audit_unwind_blocks): the client
#      gets the held fee back, and the miner is neither paid nor slashed for that job.
#   4. Then stop the miner, so that it takes no new work and no new jury seat:
#        docker compose -p dendra-miner stop
#      A miner of an earlier kit does not know the drain. Stopped, it proves no presence: it is drawn onto no
#      new jury while the chain measures availability. Where step 2 says the availability slash is ARMED, each
#      epoch it stays stopped and registered counts toward a slash of its stake: go on to step 5 at once.
#   5. Leave once the chain accepts -- --wait asks it again once a minute until it does (Ctrl-C stops
#      waiting, nothing is sent):
#        bash deploy/testnet-miner/exit-miner.sh --yes --wait
#      Exit 0: the miner is deregistered and its remaining stake is back on the address that registered it,
#      both read back from the chain.
#   6. Optional: bash deploy/uninstall.sh prints what it would remove; --yes removes it.
#
# Exit codes -- three answers, never two:
#   0  left the network and verified; or nothing to leave (this identity is not registered); --undrain: the
#      marker removed, or none; --status: every identity read
#   1  a step was attempted and failed (broadcast rejected, not included, still registered afterwards, the
#      drain not written)
#   2  refused: no --yes, the chain does not accept the exit yet (its message is printed), this keyring
#      does not hold the key that registered the miner, or (owner mode) the exit waits for the owner's
#      signature -- prepared, nothing sent
#   3  not measurable: Docker or the kit unreadable, the identity unreadable, the node not answering --
#      an unknown is never read as "nothing to leave"; --status: an identity whose registration was not read
set -u

YES=0; SIGNED=""; SLOT=""; WAIT=0; UNDRAIN=0; STATUS=0
while [ $# -gt 0 ]; do case "$1" in
  --yes) YES=1; shift;;
  --signed) SIGNED="${2:-}"; [ -n "$SIGNED" ] || { echo "[exit-miner] --signed needs the file the owner signed"; exit 2; }; shift 2;;
  --slot) SLOT="${2:-}"
          case "$SLOT" in 0|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) : ;; *) echo "[exit-miner] --slot needs a slot number (bash deploy/testnet-miner/slots.sh list)"; exit 2;; esac
          shift 2;;
  --wait) WAIT=1; shift;;
  --undrain) UNDRAIN=1; shift;;
  --status) STATUS=1; shift;;
  -h|--help) awk 'NR>1{ if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"; exit 0;;
  *) echo "[exit-miner] unknown argument: $1 (see --help)"; exit 2;;
esac; done
# Each mode is one request: a reading (--status), the drain removed (--undrain), or the exit (--yes, --wait
# waiting for it, --signed sending the owner's). Mixed, which one was meant is not this file's to guess.
if [ "$STATUS" = 1 ] && { [ "$YES$UNDRAIN$WAIT" != 000 ] || [ -n "$SIGNED" ]; }; then
  echo "[exit-miner] --status only reads: it takes no --yes, --signed, --wait or --undrain"; exit 2
fi
if [ "$UNDRAIN" = 1 ] && { [ "$YES$WAIT" != 00 ] || [ -n "$SIGNED" ]; }; then
  echo "[exit-miner] --undrain gives the exit up: it takes no --yes, --signed or --wait"; exit 2
fi
if [ "$WAIT" = 1 ] && [ "$YES" != 1 ]; then
  echo "[exit-miner] --wait waits to leave: it needs --yes"; exit 2
fi
# Read from the directory it was named in: the helper below runs from the kit's directory.
case "$SIGNED" in ""|/*) : ;; *) SIGNED="$PWD/$SIGNED" ;; esac

say(){ printf '%s\n' "$*"; }
refuse(){ printf '  [REFUSED] %s\n' "$*" >&2; }
unmeasurable(){ printf '  [?] %s\n' "$*" >&2; exit 3; }
fail(){ printf '  [FAILED] %s\n' "$*" >&2; exit 1; }

KIT="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -n "$KIT" ] && [ -f "$KIT/docker-compose.yml" ] || unmeasurable "the miner kit (docker-compose.yml) is not next to this file."
[ -f "$KIT/.env" ] || unmeasurable "$KIT/.env is missing: no miner was set up from this kit (deploy/join.sh writes it)."
command -v docker >/dev/null 2>&1 || unmeasurable "docker is not installed: the miner's keys and the chain are read through it."
docker compose version >/dev/null 2>&1 || unmeasurable "docker compose (v2) does not answer."

# ---------------------------------------------------------------- which identity: the slot
# Every compose command of this file goes through slots.sh (the one place that knows a slot's project, env
# and files): a hard-coded project name acts on slot 0 whatever slot was meant.
{ [ -r "$KIT/slots.sh" ] && . "$KIT/slots.sh" && declare -F slot_compose >/dev/null 2>&1; } \
  || unmeasurable "the slot library ($KIT/slots.sh) cannot be loaded: which identity this would act on is not known."
ALL_SLOTS="$(slot_ids --all)" || unmeasurable "the slots of this kit ($KIT/gpu) cannot be read: which identity this would act on is not known."
NSLOTS="$(printf '%s\n' "$ALL_SLOTS" | awk 'NF{n++} END{print n+0}')"
if [ -z "$SLOT" ] && [ "$NSLOTS" -ge 2 ]; then
  if [ "$YES" = 1 ] || [ -n "$SIGNED" ] || [ "$UNDRAIN" = 1 ]; then
    refuse "this machine runs $NSLOTS miner identities, one per card: name the one to act on with (--slot <k>). Nothing was done."
    printf '%s\n' "$ALL_SLOTS" | while read -r _k; do
      [ -n "$_k" ] && say "      --slot $_k   $(slot_project "$_k")   $(slot_state "$_k")   miner $(slot_val "$_k" MINER_ID 2>/dev/null)"
    done
    exit 2
  fi
  # Without --yes: the simulation (or, with --status, the registration) of every slot, one after the other.
  # Exit: 3 if one could not be read, else 1 if one failed, else 2 if one would leave with --yes, else 0.
  _worst=0; _mode=""; [ "$STATUS" = 1 ] && _mode="--status"
  for _k in $ALL_SLOTS; do
    if [ "$STATUS" != 1 ]; then
      say ""
      say "######## slot $_k ($(slot_project "$_k"), $(slot_state "$_k")) ########"
    fi
    bash "$KIT/$(basename "$0")" --slot "$_k" $_mode; _rc=$?
    case "$_rc" in 0|1|2|3) : ;; *) _rc=3 ;; esac
    case "$_rc:$_worst" in
      3:*|*:3) _worst=3 ;;
      1:*|*:1) _worst=1 ;;
      2:*|*:2) _worst=2 ;;
      *) : ;;
    esac
  done
  if [ "$STATUS" != 1 ]; then
    say ""
    say "  Each identity leaves on its own: bash $KIT/$(basename "$0") --slot <k> --yes"
  fi
  exit "$_worst"
fi
SLOT="${SLOT:-0}"
case "$(slot_state "$SLOT")" in
  active|retired) : ;;
  missing) refuse "this kit has no slot $SLOT (bash $KIT/slots.sh list shows them). Nothing was done."; exit 2 ;;
  *) unmeasurable "the env of slot $SLOT ($(slot_env "$SLOT")) cannot be read." ;;
esac
SLOT_DIR="$(slot_dir "$SLOT")"
if [ "$SLOT" = 0 ]; then
  STOP_HINT="docker compose -p dendra-miner stop miner"; START_HINT="cd $KIT && docker compose up -d"; SLOT_FLAG=""
  LOG_HINT="docker compose -p dendra-miner logs miner"
else
  STOP_HINT="$(slot_run_hint "$SLOT" stop miner)"; START_HINT="$(slot_run_hint "$SLOT" up -d --no-build)"; SLOT_FLAG=" --slot $SLOT"
  LOG_HINT="$(slot_run_hint "$SLOT" logs miner)"
  # A retired slot is started by join.sh only (slots.sh refuses to start it): that is its remedy.
  [ "$(slot_state "$SLOT")" = retired ] && START_HINT="bash $(cd "$KIT/../.." 2>/dev/null && pwd)/deploy/join.sh --gpus <its card index, with the others>"
  [ "$STATUS" = 1 ] || say "== [exit-miner] slot $SLOT: $(slot_project "$SLOT"), card $(slot_val "$SLOT" DENDRA_GPU_UUID 2>/dev/null) =="
fi

# ---------------------------------------------------------------- the helper, run INSIDE the miner image
# One small program, run by the image's own python3 with the image's own dendrad: the host needs no
# interpreter, and every JSON answer is PARSED, never matched as text. It prints `key=value` lines.
# THE ZERO RULE: proto3 omits a field at its zero value, so a missing `stake` is a stake of 0 and a
# transaction answer without `code` is code 0 -- the normal form of a success. A query that FAILS is
# `state=unread`, never a zero.
PY="$(cat <<'PYEOF'
import json, os, re, subprocess, sys, time
sys.dont_write_bytecode = True
# THE CLONE'S MODULES FIRST, when the kit sits in a clone (MODEA_MOUNT below mounts them here, read-only): this
# program comes from the clone, and so does the library it calls.
if os.path.isfile("/opt/dendra-clone/modea/__init__.py"):
    sys.path.insert(0, "/opt/dendra-clone")
from modea import keyring as K
NODE = os.environ.get("DENDRA_NODE", "")
NF = ["--node", NODE] if NODE else []
KEYS = os.environ.get("DENDRA_KEYS_DIR", "/data/keys")
_KR = []


def kr():
    # THE MINER'S OWN KEYRING MODULE: the backend from the state of the volume, and for an encrypted
    # keyring the passphrase mounted at /run/dendra-secrets, handed to dendrad on stdin, never in argv.
    if not _KR:
        _KR.append(K.resolve(os.environ.get("DENDRA_KEYRING_DIR") or None))
    return _KR[0]


def run(argv, t=180):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=t)
    except Exception as e:
        return 127, "", type(e).__name__ + ": " + str(e)
    return r.returncode, r.stdout or "", r.stderr or ""


def say(k, v):
    print(k + "=" + " ".join(str(v).split()))


def doc(text):
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def first_line(*texts):
    for t in texts:
        for ln in (t or "").splitlines():
            if ln.strip():
                return ln.strip()[:400]
    return ""


def identity():
    mid, why = "", ""
    try:
        with open(os.path.join(KEYS, "identite-resolue"), encoding="utf-8") as f:
            v = f.read().strip()
        if v.startswith("dm1"):
            mid = v
    except OSError:
        pass
    try:
        k = kr()
    except K.KeyringError as e:
        say("id", "")
        say("address", "")
        say("why", str(e) + " -- " + e.hint)
        return
    if not mid:
        # Listed by the module, which confronts dendrad's answer with the disk: dendrad answers a wrong
        # passphrase with an EMPTY list and exit 0, which would read as "no key".
        try:
            names = [str(x.get("name", "")) for x in K.list_keys(k)]
            names = [n for n in names if n.startswith("dm1")]
            if len(names) == 1:
                mid = names[0]
            else:
                why = str(len(names)) + " dm1 key(s) in the keyring, exactly one expected"
        except K.KeyringError as e:
            why = str(e)
    addr = ""
    if mid:
        st, val = K.key_state(k, mid)
        if st == K.PRESENT:
            addr = val
        else:
            why = "the key " + mid + " does not resolve to an address: " + val
    say("id", mid)
    say("address", addr)
    say("why", why)


def miner(mid):
    rc, out, err = run(["dendrad", "query", "jobs", "get-miner", mid, "-o", "json", *NF])
    if rc != 0:
        # gRPC NotFound is the chain's answer that the record does not exist; anything else is unread.
        if "code = NotFound" in (out + err):
            say("state", "absent")
        else:
            say("state", "unread")
            say("why", first_line(err, out))
        return
    d = doc(out)
    m = d.get("miner") if d is not None else None
    if not isinstance(m, dict):
        say("state", "unread")
        say("why", "the answer is not a miner record")
        return
    try:
        stake = int(m.get("stake") or 0)
    except (TypeError, ValueError):
        say("state", "unread")
        say("why", "stake is not an integer")
        return
    say("state", "registered")
    say("stake", stake)
    say("creator", m.get("creator") or "")
    say("operator", m.get("operator") or "")


def g(d, *names):
    # One field under its proto name or its JSON name: the CLI has printed both.
    for n in names:
        if n in d:
            return d[n]
    return None


def status():
    # THE REGISTRATION ALONE (--status, read by deploy/uninstall.sh). It needs the identifier, not the address:
    # the one the daemon resolved; else the one dm1 key of the keyring; else, for a keyring of ONE key the daemon
    # never renamed, the identifier derived from that key's address. A volume without any key holds nothing
    # that can be registered from here. Anything else is unread, never "not registered".
    mid, why = "", ""
    try:
        with open(os.path.join(KEYS, "identite-resolue"), encoding="utf-8") as f:
            v = f.read().strip()
        if v.startswith("dm1"):
            mid = v
    except OSError:
        pass
    if not mid:
        try:
            keys = K.list_keys(kr())
            names = [str(x.get("name", "")) for x in keys]
            dm1 = [n for n in names if n.startswith("dm1")]
            if len(dm1) == 1:
                mid = dm1[0]
            elif not keys:
                say("id", "")
                say("state", "absent")
                say("why", "no key in this identity's volume: nothing here is registered")
                return
            elif len(keys) == 1 and not dm1:
                from modea.miner_id import miner_id_for_account
                mid = miner_id_for_account(str(keys[0].get("address", "")))
            else:
                why = str(len(dm1)) + " dm1 key(s) among " + str(len(keys)) + " in the keyring, exactly one expected"
        except K.KeyringError as e:
            why = str(e)
        except Exception as e:
            why = type(e).__name__ + ": " + str(e)
    say("id", mid)
    if not mid.startswith("dm1"):
        say("state", "unread")
        say("why", "the identity could not be read: " + (why or "no identifier"))
        return
    miner(mid)


def chain_height():
    # The node's latest height, '' when it cannot be read. `dendrad status` has printed its JSON on stdout or
    # on stderr depending on the release: both are read, and only a number is a height.
    rc, out, err = run(["dendrad", "status", *NF], t=60)
    for text in (out, err):
        i = (text or "").find("{")
        s = doc(text[i:]) if i >= 0 else None
        if s is None:
            continue
        si = g(s, "sync_info", "SyncInfo")
        v = str(g(si, "latest_block_height") or "") if isinstance(si, dict) else ""
        return v if v.isdigit() else ""
    return ""


def drain():
    # THE DRAIN MARKER: a file named `drain` in the key directory, next to availability-last.json. Its presence
    # is the whole interface with the daemon. Written whole (a temporary file, then a rename) and read back.
    p = os.path.join(KEYS, "drain")
    tmp = p + ".exit-miner.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("set by exit-miner.sh at " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + chr(10))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as e:
        say("drain", "failed")
        say("why", type(e).__name__ + ": " + str(e))
        return
    say("drain", "set" if os.path.isfile(p) else "failed")


def drain_read():
    # Whether the marker is there, changing nothing: present the way the daemon reads it (a dangling link counts,
    # its NAME is the signal), absent on "not found" only, unread on anything else.
    p = os.path.join(KEYS, "drain")
    if not os.path.isdir(KEYS):
        say("drain", "unread")
        say("why", KEYS + " is not a directory in this container")
        return
    try:
        os.lstat(p)
    except FileNotFoundError:
        say("drain", "absent")
        return
    except OSError as e:
        say("drain", "unread")
        say("why", type(e).__name__ + ": " + str(e))
        return
    say("drain", "present")


def undrain():
    p = os.path.join(KEYS, "drain")
    if not os.path.isdir(KEYS):
        say("drain", "unread")
        say("why", KEYS + " is not a directory in this container")
        return
    try:
        os.unlink(p)
    except FileNotFoundError:
        say("drain", "absent")
        return
    except OSError as e:
        say("drain", "failed")
        say("why", type(e).__name__ + ": " + str(e))
        return
    say("drain", "failed" if os.path.lexists(p) else "removed")


def obligations(mid):
    # WHAT HOLDS THE MINER, as far as the chain's queries show it: a READING printed beside the chain's refusal,
    # never in its place. The parameters that bound an audit, the height, then the jobs page by page -- every job
    # under audit (`+disputed`, not `+resolved`) whose anchored jury (or re-adjudication jury) seats this miner,
    # and every one of its own. Bounded: past MAX_PAGES pages of jobs or MAX_AUDITS open audits the reading is
    # PARTIAL, and says so. A held fee or an unsettled job has no query of its own: they are not listed.
    MAX_PAGES, MAX_AUDITS = 120, 300
    rc, out, err = run(["dendrad", "query", "jobs", "params", "-o", "json", *NF], t=60)
    d = doc(out) if rc == 0 else None
    p = g(d, "params") if d is not None else None
    if not isinstance(p, dict):
        say("obl", "unread")
        say("why", "the module's parameters: " + (first_line(err, out) or "not a JSON object"))
        return
    try:
        # proto3 omits a parameter at its zero value: absent is 0, and 0 DISARMS each of these.
        unwind = int(g(p, "audit_unwind_blocks", "auditUnwindBlocks") or 0)
        timeout = int(g(p, "audit_resolve_timeout", "auditResolveTimeout") or 0)
        hold = int(g(p, "hold_bps", "holdBps") or 0)
        avail = int(g(p, "avail_epoch_blocks", "availEpochBlocks") or 0)
        slash_bps = int(g(p, "avail_slash_bps", "availSlashBps") or 0)
        slash_max = int(g(p, "avail_slash_max", "availSlashMax") or 0)
        fail_k = int(g(p, "avail_fail_k", "availFailK") or 0)
        fail_w = int(g(p, "avail_fail_window", "availFailWindow") or 0)
    except (TypeError, ValueError):
        say("obl", "unread")
        say("why", "a parameter of the module is not an integer")
        return
    # THE AVAILABILITY SLASH, said BEFORE the jobs are read (a listing that fails does not hide it): at each epoch
    # boundary a BONDED miner absent from the presence records of avail_fail_k epochs within avail_fail_window
    # loses min(stake * avail_slash_bps / 10000, avail_slash_max) -- a draining miner, like a stopped one, proves
    # no presence. Dormant while any of the three is 0, and with no epoch at all while avail_epoch_blocks is 0:
    # chain/x/jobs/keeper/availability.go::runAvailabilitySlash, called by runAvailabilityEpoch.
    armed = avail > 0 and slash_bps > 0 and fail_k > 0 and fail_w > 0
    say("slash", "armed" if armed else "dormant")
    say("slash_bps", slash_bps)
    say("slash_max", slash_max)
    say("fail_k", fail_k)
    say("fail_w", fail_w)
    say("slash_epoch", avail)
    seats, own, audits, key, pages, partial = [], [], 0, "", 0, ""
    while not partial:
        argv = ["dendrad", "query", "jobs", "list-job", "--page-limit", "500", "-o", "json", *NF]
        if key:
            argv += ["--page-key", key]
        rc, out, err = run(argv, t=120)
        d = doc(out) if rc == 0 else None
        if d is None:
            say("obl", "unread")
            say("why", "list-job: " + (first_line(err, out) or "not a JSON object"))
            return
        rows = g(d, "job", "Job")
        if rows is None:
            rows = []  # proto3 omits an empty list: a page of zero jobs
        if not isinstance(rows, list):
            say("obl", "unread")
            say("why", "list-job: the jobs are not a list")
            return
        for j in rows:
            if not isinstance(j, dict):
                say("obl", "unread")
                say("why", "list-job: a job is not an object")
                return
            st = str(g(j, "state") or "")
            if "disputed" not in st or "resolved" in st:
                continue
            jid = str(g(j, "job_id", "jobId") or "")
            if not jid:
                say("obl", "unread")
                say("why", "list-job: a job under audit has no identifier")
                return
            audits += 1
            if audits > MAX_AUDITS:
                partial = "more than " + str(MAX_AUDITS) + " jobs under audit: the ones after them were not read"
                break
            try:
                opened = int(g(j, "dispute_height", "disputeHeight") or 0)
            except (TypeError, ValueError):
                opened = 0
            if str(g(j, "miner_id", "minerId") or "") == mid:
                own.append((opened, jid))
            for kind in ("jury", "redo"):
                a = ["dendrad", "query", "jobs", "audit-committee", jid, "-o", "json", *NF]
                if kind == "redo":
                    a.append("--redo")
                rc2, out2, err2 = run(a, t=60)
                if rc2 != 0:
                    if "code = NotFound" in (out2 + err2):
                        continue  # no such jury anchored (a deferred audit, or no re-adjudication): no seat
                    say("obl", "unread")
                    say("why", "audit-committee " + jid + ": " + first_line(err2, out2))
                    return
                c = doc(out2)
                members = g(c, "members") if c is not None else None
                if c is not None and members is None:
                    members = []  # proto3 omits an empty list
                if not isinstance(members, list):
                    say("obl", "unread")
                    say("why", "audit-committee " + jid + ": the members are not a list")
                    return
                if mid in members:
                    try:
                        anchored = int(g(c, "anchored_height", "anchoredHeight") or 0)
                    except (TypeError, ValueError):
                        anchored = 0
                    seats.append((opened or anchored, jid, kind))
        if partial:
            break
        pg = g(d, "pagination")
        key = str(g(pg, "next_key", "nextKey") or "") if isinstance(pg, dict) else ""
        pages += 1
        if not key:
            break
        if pages >= MAX_PAGES:
            partial = "more than " + str(MAX_PAGES) + " pages of jobs: the ones after them were not read"
    say("obl", "partial" if partial else "read")
    if partial:
        say("why", partial)
    say("height", chain_height())
    say("unwind", unwind)
    say("timeout", timeout)
    say("hold_bps", hold)
    say("avail", avail)
    say("audits", audits)
    for o, jid, kind in seats:
        say("seat", str(o) + " " + jid + " " + kind)
    for o, jid in own:
        say("own", str(o) + " " + jid)
    opened_all = [x[0] for x in seats] + [x[0] for x in own]
    # THE HEIGHT BY WHICH THE CHAIN HAS UNWOUND THEM WITH NO VERDICT -- the derivation of
    # services/final_season_rank.py::finality_blocks, applied to the audits read here.
    # chain/x/jobs/keeper/audit_unwind.go::unwindStrandedRetention unwinds a retention once it is
    # audit_unwind_blocks old, and only when a deferred step comes due again -- every audit_resolve_timeout
    # blocks for a drawn audit. A retention begins no later than its audit opens (dispute_height), so the
    # bound is the last opening + audit_unwind_blocks + audit_resolve_timeout. Given only when every term is
    # known: never over a partial reading, nor over a parameter at 0 (which disarms that path).
    if not opened_all:
        why = "no open audit seat and no audited job of its own was found"
    elif partial:
        why = "the reading is partial"
    elif unwind == 0:
        why = "audit_unwind_blocks is 0 on this chain: an audit with no verdict is not unwound"
    elif timeout == 0:
        why = "audit_resolve_timeout is 0 on this chain: a drawn audit has no deadline at which to be unwound"
    elif hold == 0:
        why = "hold_bps is 0 on this chain: no fee is retained, and only a retained fee is unwound"
    elif 0 in opened_all:
        why = "the chain records no opening height for one of these audits"
    else:
        why = ""
        say("earliest", max(opened_all) + unwind + timeout)
    if why:
        say("earliest", "none")
        say("earliest_why", why)


def balance(addr):
    rc, out, err = run(["dendrad", "query", "bank", "balances", addr, "-o", "json", *NF])
    d = doc(out) if rc == 0 else None
    if d is None or not isinstance(d.get("balances", []), list):
        say("state", "unread")
        say("why", first_line(err, out))
        return
    total = 0
    for c in d.get("balances", []):
        if isinstance(c, dict) and c.get("denom") == "udndr":
            total += int(c.get("amount") or 0)
    say("state", "read")
    say("udndr", total)


def chain_id():
    try:
        from modea import chain_id as cid
        return cid.resolve(tuple(NF))
    except Exception as e:
        say("why", "chain id: " + str(e))
        return ""


def simulate(mid, addr):
    cid = chain_id()
    if not cid:
        say("sim", "unread")
        return
    rc, out, err = run(["dendrad", "tx", "jobs", "delete-miner", mid, "--from", addr, "--chain-id", cid,
                        "--dry-run", *NF])
    # Only a POSITIVE proof is an acceptance: exit 0 AND the gas estimate the simulation prints.
    if rc == 0 and "gas estimate" in (out + err):
        say("sim", "accepted")
    else:
        say("sim", "refused")
        say("chain_said", first_line(err, out))
    say("height", chain_height())


def broadcast(mid):
    cid = chain_id()
    if not cid:
        say("bcast", "unread")
        return
    k = kr()
    rc, out, err = K.run(k, ["dendrad", "tx", "jobs", "delete-miner", mid, "--from", mid, *k.flags(), "--chain-id", cid,
                             "--gas", "auto", "--gas-adjustment", "1.6", "--yes", "-o", "json", *NF], timeout=180)
    if rc is None:
        say("bcast", "unread")
        say("why", err)
        return
    d = doc(out)
    if d is None:
        say("bcast", "unread")
        # A keyring refusal is NAMED: dendrad prints it after its usage text, as "not a valid name or address".
        say("why", K.explain(err + out) or first_line(err, out))
        return
    code = int(d.get("code") or 0)
    if code != 0:
        say("bcast", "rejected")
        say("code", code)
        say("raw_log", d.get("raw_log") or "")
        return
    say("bcast", "ok")
    say("txhash", d.get("txhash") or "")


def prepare(mid, owner):
    # OWNER MODE: the unsigned delete-miner from the OWNER's address and the command that signs it, both
    # built by modea/owner_tx.py -- the module the daemon prints the registration with. Nothing is signed.
    import base64
    from modea import owner_tx as O
    cid = chain_id()
    if not cid:
        say("prep", "unread")
        return
    p = O.prepare("delete-miner", (mid,), owner, cid, node_flags=tuple(NF), miner_id=mid, run=run)
    if not p["ok"]:
        say("prep", "refused")
        say("why", p["why"])
        say("chain_said", p.get("chain_said") or "")
        return
    say("prep", "ok")
    say("gas", p["gas"])
    say("unsigned_b64", base64.b64encode(p["unsigned"].encode("utf-8")).decode("ascii"))
    say("generate", O.shell(p["generate"]))
    say("sign", O.shell(O.sign_argv("delete-miner.json", owner, cid, p["account_number"], p["sequence"],
                                    "delete-miner.signed.json")))


def broadcast_signed(mid, owner):
    # OWNER MODE: the file on stdin must be the OWNER's signed delete-miner for THIS miner -- one message, the
    # right type, signer and miner, a signature -- before anything is sent: a signed transaction is otherwise
    # an opaque blob, and broadcasting whatever came back would send a transfer as readily as an exit.
    import tempfile
    from modea import owner_tx as O
    text = sys.stdin.read()
    why = O.check_tx(text, "delete-miner", owner, miner_id=mid, signed=True)
    if why:
        say("bcast", "refused")
        say("why", "the file is not the owner's signed delete-miner for " + mid + ": " + why)
        return
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        rc, out, err = run(["dendrad", "tx", "broadcast", path, "-o", "json", *NF])
    finally:
        os.unlink(path)
    d = doc(out)
    if d is None:
        say("bcast", "unread")
        say("why", first_line(err, out))
        return
    code = int(d.get("code") or 0)
    if code != 0:
        say("bcast", "rejected")
        say("code", code)
        say("raw_log", d.get("raw_log") or "")
        return
    say("bcast", "ok")
    say("txhash", d.get("txhash") or "")


def included(txhash):
    for _ in range(30):
        rc, out, err = run(["dendrad", "query", "tx", txhash, "-o", "json", *NF], t=30)
        d = doc(out) if rc == 0 else None
        if d is not None and int(d.get("height") or 0) > 0:
            code = int(d.get("code") or 0)
            say("incl", "ok" if code == 0 else "failed")
            say("code", code)
            say("raw_log", d.get("raw_log") or "")
            return
        time.sleep(2)
    say("incl", "unread")


try:
    {"identity": identity, "miner": miner, "balance": balance, "simulate": simulate,
     "broadcast": broadcast, "included": included, "prepare": prepare,
     "broadcast_signed": broadcast_signed, "status": status, "drain": drain, "undrain": undrain,
     "drain_read": drain_read, "obligations": obligations}[sys.argv[1]](*sys.argv[2:])
except Exception as e:
    # Whatever the step, an exception is a value NOT read: the caller sees no state and says unknown.
    say("why", "helper error: " + type(e).__name__ + ": " + str(e))
PYEOF
)"

# THE CLONE'S MODULES, mounted read-only where the program above looks first. The kit sits at
# <clone>/deploy/testnet-miner; an image built from an earlier tree may lack the modules this program calls
# (modea/keyring.py, modea/owner_tx.py), and a miner that cannot read its own identity cannot leave. The host
# directory is the clone's, which the kit's compose file already binds from (its ./secrets default): a host
# that runs the kit can mount it. Outside a clone, the image's own modules, and it is said once.
MODEA_MOUNT=()
_modea="$(cd "$KIT/../../services/modea" 2>/dev/null && pwd)"
if [ -n "$_modea" ] && [ -f "$_modea/__init__.py" ] && [ -f "$_modea/keyring.py" ]; then
  MODEA_MOUNT=(-v "$_modea:/opt/dendra-clone/modea:ro")
elif [ "$STATUS" != 1 ]; then
  say "  [i] no services/modea in a clone around this kit: the miner image's own modules are used"
fi
# helper <subcommand> [args] -- one-off container of the slot's miner service, through slots.sh: from the kit's
# directory, with the slot's env and files, so that it mounts THIS slot's keys and joins the network its miner
# reads the chain on (slot 0: the kit's .env and its COMPOSE_FILE, as always).
H_OUT=""; H_ERRF="$(mktemp)"; trap 'rm -f "$H_ERRF"' EXIT
helper(){
  H_OUT="$( slot_compose "$SLOT" run ${MODEA_MOUNT[@]+"${MODEA_MOUNT[@]}"} --rm --no-deps -T --entrypoint python3 miner -c "$PY" "$@" 2>"$H_ERRF" )"
  # compose's own refusal (no image, the network missing) is the only explanation when nothing came back.
  [ -n "$H_OUT" ] || H_OUT="why=$(tail -1 "$H_ERRF" 2>/dev/null)"
}
# helper_in <file> <subcommand> [args] -- the same, with <file> on the helper's stdin.
helper_in(){
  local f="$1"; shift
  H_OUT="$( slot_compose "$SLOT" run ${MODEA_MOUNT[@]+"${MODEA_MOUNT[@]}"} --rm --no-deps -T --entrypoint python3 miner -c "$PY" "$@" < "$f" 2>"$H_ERRF" )"
  [ -n "$H_OUT" ] || H_OUT="why=$(tail -1 "$H_ERRF" 2>/dev/null)"
}
val(){ printf '%s\n' "$H_OUT" | sed -n "s/^$1=//p" | head -1; }
vals(){ printf '%s\n' "$H_OUT" | sed -n "s/^$1=//p"; }
int(){ case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }
dndr(){ awk -v u="$1" 'BEGIN{ printf "%.6f DNDR", u/1000000 }'; }

# ---------------------------------------------------------------- --status: the registration, one line
# The line deploy/uninstall.sh reads -- one per identity, the reason last because it may hold spaces. Three
# states: registered, absent (the chain's NotFound, or a volume that holds no key), unread (anything else).
# ⛔ A READING BUILDS NOTHING. `compose run` BUILDS the service's image when its tag is absent -- measured on
# compose v5.3.0, `--pull never` included -- and uninstall.sh asks this line while it only prints a plan, often
# after an earlier uninstall removed the images. So the image is looked up first: absent, the registration is
# unread, and the reason names the image and how to bring it back.
if [ "$STATUS" = 1 ]; then
  _img="$(slot_compose "$SLOT" config --images miner 2>/dev/null | head -1 | tr -d '\r')"
  if [ -z "$_img" ]; then
    H_OUT="why=the miner image of this identity could not be named (docker compose config --images miner): nothing was run"
  elif _ie="$(docker image inspect "$_img" 2>&1 >/dev/null)"; then
    helper status
  else
    # "No such image" is the engine's answer that it is absent; any other failure is a lookup that did not happen.
    case "$_ie" in
      *"No such image"*) H_OUT="why=the miner image $_img is not on this host, and a reading would build it: nothing was built. Bring it back first (cd $KIT && docker compose build miner -- or pull, for an image pinned by digest), then read again" ;;
      *) H_OUT="why=the miner image $_img could not be looked up: $(printf '%s\n' "$_ie" | grep . | head -1)" ;;
    esac
  fi
  _st="$(val state)"
  case "$_st" in registered|absent) : ;; *) _st=unread ;; esac
  printf 'registration slot=%s state=%s id=%s stake=%s why=%s\n' "$SLOT" "$_st" "$(val id)" "$(val stake)" "$(val why)"
  [ "$_st" = unread ] && exit 3
  exit 0
fi

# ---------------------------------------------------------------- --undrain: give the exit up
if [ "$UNDRAIN" = 1 ]; then
  say "== [exit-miner] removing the drain =="
  helper undrain
  case "$(val drain)" in
    removed) say "  [OK] drain marker removed from the miner's key volume: the miner proves its presence and takes work"
             say "       again at its next pass (a stopped miner does once started: $START_HINT). A miner the chain no"
             say "       longer lists registers again first, and stakes again."; exit 0 ;;
    absent)  say "  [OK] no drain marker in the miner's key volume: nothing to remove."; exit 0 ;;
    failed)  fail "the drain marker could not be removed: $(val why)" ;;
    *)       unmeasurable "the miner's key volume could not be reached: $(val why)" ;;
  esac
fi

# absent_drain -- THE MINER IS NOT REGISTERED, AND A DRAIN MARKER MAY BE LEFT BY AN EARLIER RUN whose exit was taken
# another way: the owner's signed transaction broadcast from another machine, a run that could not read its own
# broadcast, an eviction by the chain. A draining daemon does not register (services/miner.py skips
# the registration while draining): left in the volume, the marker would keep a miner started again on these keys
# away from all work, with nothing on screen to say why. Without --yes it is said. With --yes it is removed once the
# miner is STOPPED; on a RUNNING miner it is what keeps it from registering again and staking again, and it is kept.
absent_drain(){
  local cid
  helper drain_read
  case "$(val drain)" in
    absent) say "  (If the miner is running, it registers again by itself: stop it with $STOP_HINT.)"; return 0 ;;
    present) : ;;
    *) say "  [?] whether a drain marker is left in the miner's key volume could not be read ($(val why))."
       say "  (If the miner is running and not draining, it registers again by itself: stop it with $STOP_HINT.)"
       return 0 ;;
  esac
  if [ "$YES" != 1 ]; then
    say "  [!] a drain marker left by an earlier run is still set in the miner's key volume: a miner started on these"
    say "      keys neither registers nor takes work while it is there. With --yes this file removes it once the miner"
    say "      is stopped; --undrain removes it now (a miner then registers again and stakes again)."
    return 0
  fi
  if ! cid="$(slot_cid "$SLOT" miner --running)"; then
    say "  [?] a drain marker left by an earlier run is still set, and whether the miner runs could not be read: the"
    say "      marker is KEPT (on a running miner it is what keeps it from registering again). Stop the miner"
    say "      ($STOP_HINT), then run this again with --yes -- or remove it: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
    return 0
  fi
  if [ -n "$cid" ]; then
    say "  [!] a drain marker left by an earlier run is still set, and the miner RUNS: the marker is what keeps it from"
    say "      registering again and staking again, so it is KEPT. Stop it ($STOP_HINT), then run this again with"
    say "      --yes to remove it -- or, to have it register again and take work: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
    return 0
  fi
  helper undrain
  case "$(val drain)" in
    removed|absent) say "  [OK] drain marker left by an earlier run removed: a miner started again on these keys registers again"
                    say "       (and stakes again) and takes work." ;;
    *) say "  [!] the drain marker left by an earlier run could not be removed ($(val why)): a miner started again on"
       say "      these keys would neither register nor take work until: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain" ;;
  esac
}

say "== [exit-miner] reading this miner =="
helper identity
MID="$(val id)"; ADDR="$(val address)"
[ -n "$MID" ] && [ -n "$ADDR" ] || unmeasurable "the miner's identity could not be read from its volume: $(val why)"
say "  identity : $MID"
say "  address  : $ADDR"

helper miner "$MID"
STATE="$(val state)"
case "$STATE" in
  absent)
    say "  on chain : NOT registered -- there is nothing to leave."
    absent_drain
    exit 0 ;;
  registered) : ;;
  *) unmeasurable "the chain could not be read (get-miner $MID): $(val why). Not knowing is not 'not registered'." ;;
esac
STAKE="$(val stake)"; CREATOR="$(val creator)"; OPERATOR="$(val operator)"
say "  on chain : registered, stake $(dndr "$STAKE") ($STAKE udndr), registered by $CREATOR"
# OWNER MODE is read from the CHAIN, not from a setting: registered by another key, operated by this one.
OWNER_MODE=0
if [ "$CREATOR" != "$ADDR" ]; then
  if [ -n "$CREATOR" ] && [ "$OPERATOR" = "$ADDR" ]; then
    OWNER_MODE=1
    say "  owner    : $CREATOR -- OWNER MODE: this machine's key operates the miner, the owner signs its exit,"
    say "             and the stake goes back to the owner. Nothing here is signed with this machine's key."
  else
    refuse "this miner was registered by $CREATOR, and this keyring holds $ADDR. The chain accepts delete-miner only from the key that registered it."
    exit 2
  fi
fi
if [ -n "$SIGNED" ] && [ "$OWNER_MODE" != 1 ]; then
  refuse "--signed is for a miner in owner mode; $MID was registered by this machine's key, which signs its own exit: run with --yes alone."
  exit 2
fi

# The stake goes back to the key that registered the miner: this machine's, or the owner's.
helper balance "$CREATOR"
BAL0=""; [ "$(val state)" = read ] && BAL0="$(val udndr)"
if [ -n "$BAL0" ]; then say "  balance  : $(dndr "$BAL0") ($CREATOR)"
else say "  balance  : ? (unread -- the refund will not be checked against it)"; fi

# simulate [after-stop] -- the chain asked, nothing sent. A simulation that cannot be read ends the run (exit 3), and
# says what it leaves: the drain set (with --yes), and AFTER THE STOP the miner started again as on a refusal --
# nothing was sent, so it is still registered, and stopped it cannot file a reveal that may be due.
simulate(){
  helper simulate "$MID" "$CREATOR"
  SIM="$(val sim)"; SAID="$(val chain_said)"
  [ "$SIM" = unread ] || return 0
  if [ "${1:-}" = after-stop ]; then
    unmeasurable "the exit could not be simulated after the stop: $(val why). Nothing was sent. $(restart_after_refusal "the exit was not simulated, so nothing was sent" "Run this file again once the node answers.")"
  elif [ "$YES" = 1 ]; then
    unmeasurable "the exit could not be simulated: $(val why). Nothing was sent. $DRAIN_TAIL"
  fi
  unmeasurable "the exit could not be simulated: $(val why)"
}
# refused_now <what has been done already> [read|read-drain] -- the chain's refusal, said as a reading; with
# `read`, what holds the miner is listed before the last sentence (report_obligations), and with `read-drain`
# what the miner's heartbeat says of the drain too (report_drain, the state read in DRAIN_STATE).
refused_now(){
  refuse "the chain does not accept this exit now. It said:"
  printf '           %s\n' "${SAID:-<no message>}" >&2
  say "         This is a reading, not a failure: the chain refuses while the miner holds an obligation -- a"
  say "         retained fee, a seat on an audit committee that has not resolved yet (while it stays eligible as"
  say "         a juror, juror_freshness_blocks after its last commit), a job under dispute, or a job it answered"
  say "         that is not settled yet."
  case "${2:-}" in
    read) report_obligations ;;
    read-drain) report_obligations; report_drain "$DRAIN_STATE" ;;
  esac
  say "         $1"
  exit 2
}
# report_obligations -- what the chain's queries show of what holds the miner (the helper's `obligations`), and
# the height from which the chain unwinds those audits with no verdict. A READING beside the refusal: it never
# decides anything, the simulation does.
report_obligations(){
  local h e n_seat n_own line o j k shown=0 rest
  helper obligations "$MID"
  case "$(val obl)" in
    read|partial) : ;;
    *) say "         [?] what holds it could not be read ($(val why)); the chain's refusal above stands."
       report_slash; return 0 ;;
  esac
  h="$(val height)"; int "$h" || h=""
  n_seat="$(vals seat | grep -c .)"; n_own="$(vals own | grep -c .)"
  say "         What holds it, read from the chain${h:+ at height $h} ($(val audits) job(s) under audit read):"
  while read -r o j k; do
    [ -n "$j" ] || continue
    shown=$((shown + 1)); [ "$shown" -le 10 ] || continue
    if [ "$k" = redo ]; then say "           - a seat on the re-adjudication jury of job $j (audit opened at height $o)"
    else say "           - a seat on the jury of job $j (audit opened at height $o)"; fi
  done <<EOF
$(vals seat)
EOF
  [ "$n_seat" -gt 10 ] && say "           - and $((n_seat - 10)) more jury seat(s)"
  shown=0
  while read -r o j; do
    [ -n "$j" ] || continue
    shown=$((shown + 1)); [ "$shown" -le 10 ] || continue
    say "           - its own job $j, under audit since height $o: the miner files its reveal while it RUNS"
  done <<EOF
$(vals own)
EOF
  [ "$n_own" -gt 10 ] && say "           - and $((n_own - 10)) more of its own jobs under audit"
  [ "$(val obl)" = partial ] && say "           [!] a PARTIAL reading: $(val why)"
  if [ "$((n_seat + n_own))" = 0 ]; then
    say "           no seat on an open audit and no audited job of its own: what holds it is a job it answered that"
    say "           is not settled yet, or a fee held until the audit lottery passes on that job -- these close"
    say "           without a verdict."
  fi
  e="$(val earliest)"
  if int "$e"; then
    rest=""
    if [ -n "$h" ]; then
      if [ "$e" -gt "$h" ]; then rest=", $((e - h)) blocks after height $h"
      else rest=", a height the chain has reached: each audit is unwound at its next deferral"; fi
    fi
    say "         With no verdict to close them sooner, the chain has unwound these audits by audit_unwind_blocks"
    say "         ($(val unwind)) + audit_resolve_timeout ($(val timeout)) blocks after each opened: the exit can be"
    say "         expected from height $e on$rest. A verdict frees a seat sooner; a deferred draw takes a few"
    say "         blocks more, and an audit the unwind does not reach can hold a seat longer. Only the chain's"
    say "         answer decides: this file asks it, and --wait asks it again until it accepts."
  elif [ "$((n_seat + n_own))" -gt 0 ]; then
    say "         No height can be given: $(val earliest_why)."
  fi
  if [ "$(val avail)" = 0 ]; then
    say "         [!] avail_epoch_blocks is 0 on this chain: presence is not measured, so neither the drain nor a stop"
    say "             keeps the miner off new juries -- it stays drawable while its last commit is within"
    say "             juror_freshness_blocks."
  fi
  report_slash
}
# report_slash -- what the chain's parameters say of a miner that proves NO PRESENCE, as a draining one does (and a
# stopped one): read by the helper's `obligations` before the jobs, three states -- armed, dormant (a parameter at
# 0, omitted by proto3: read as 0, never as unknown), or not read.
report_slash(){
  local m
  case "$(val slash)" in
    armed)
      m="$(val slash_max)"; [ "$m" = 0 ] && m="0: no cap"
      say "         [!] the availability slash is ARMED on this chain: a bonded miner that proves no presence -- DRAINING,"
      say "             or stopped -- loses avail_slash_bps ($(val slash_bps)) basis points of its stake, at most"
      say "             avail_slash_max ($m), each time it misses avail_fail_k ($(val fail_k)) epochs of"
      say "             avail_epoch_blocks ($(val slash_epoch)) blocks within avail_fail_window ($(val fail_w)). Every epoch"
      say "             spent draining counts: leave as soon as the chain accepts (--yes --wait), or give the exit up"
      say "             (--undrain), which has the miner prove its presence again." ;;
    dormant)
      say "         [i] the availability slash is dormant on this chain (avail_slash_bps, avail_fail_k, avail_fail_window"
      say "             or avail_epoch_blocks at 0): a miner that proves no presence, draining or stopped, loses no stake"
      say "             for it." ;;
    *) say "         [?] whether the chain slashes a miner that proves no presence (draining, or stopped) could not be"
       say "             read (the module's parameters, avail_slash_bps and its kin)." ;;
  esac
}
# drain_state -> confirmed | pending | not-draining | not-running | unread: what the RUNNING daemon's heartbeat says
# of the drain, read with the daemon's own reader (modea/heartbeat.py) in its own container, never matched as text.
# A daemon of this kit writes `draining` in every heartbeat from its first pass, as a boolean: true CONFIRMS; false
# is PENDING -- it reads the marker at its next pass, and the heartbeat is written at the end of a pass, so a reading
# taken just after the marker was set is false; a heartbeat WITHOUT the field was written by a daemon that does not
# know the drain (an earlier kit), or one still starting. Anything else is not a reading.
drain_state(){
  local cid out
  cid="$(slot_cid "$SLOT" miner --running)" || { printf 'unread\n'; return 0; }
  [ -n "$cid" ] || { printf 'not-running\n'; return 0; }
  out="$(docker exec -w /app "$cid" python3 -c 'import sys
sys.path.insert(0, "/app")
try:
    from modea import heartbeat as hb
    d, why = hb.read()
except Exception:
    d = None
if not isinstance(d, dict):
    print("unread")
elif "draining" not in d:
    print("not-draining")
elif d["draining"] is True:
    print("confirmed")
elif d["draining"] is False:
    print("pending")
else:
    print("unread")' 2>/dev/null)"
  case "$out" in confirmed|pending|not-draining) printf '%s\n' "$out" ;; *) printf 'unread\n' ;; esac
}
# report_drain <state from drain_state> -- what that state means for the exit, and what to do.
report_drain(){
  case "${1:-}" in
    confirmed)   say "         [OK] the miner's heartbeat says it is DRAINING: no presence proof, no new work, its reveals still"
                 say "              filed. KEEP IT RUNNING until the exit is accepted." ;;
    pending)     say "         [i] the miner runs this kit's daemon, and its heartbeat does not say it is draining YET: the daemon"
                 say "             reads the marker at its next pass and then says so (--wait reads it again). KEEP IT RUNNING;"
                 say "             if it never says so, its log says why: $LOG_HINT" ;;
    not-running) say "         [i] the miner is not running: it takes no work and no jury seat, and files no reveal (start it to"
                 say "             have one filed: $START_HINT -- with this kit it starts draining)." ;;
    not-draining)
      say "         [!] the miner runs and its heartbeat does not say it is draining, nor that it knows the drain: a"
      say "             miner of an earlier kit keeps taking jury seats, so this exit may never be accepted. Have it"
      say "             file the reveals its own audited jobs wait for, then stop it instead ($STOP_HINT), as"
      say "             --help describes for a machine registered under an earlier kit. (A daemon of this kit writes"
      say "             its drain state from its first pass: one still starting is read again by --wait.)" ;;
    *) say "         [?] whether the miner drains could not be read (its container, or its heartbeat): look at its log, or"
       say "             stop it instead ($STOP_HINT)." ;;
  esac
}
# THE LAST SENTENCE WHEN THE CHAIN REFUSES AN EXIT THAT WAS ASKED FOR (--yes): the drain stays set, whatever the
# miner's state, and the two ways on are named.
DRAIN_STATE=unread
DRAIN_TAIL="The drain marker stays set (removed once the exit is verified). Run bash $KIT/exit-miner.sh${SLOT_FLAG} --yes again -- with --wait it asks the chain until it accepts -- or give the exit up: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
# wait_for_acceptance -- --wait: the simulation again, once a minute, until the chain accepts. Nothing is sent
# while it waits; an interruption leaves the miner draining and says so. Five readings in a row that cannot be
# read stop the wait (exit 3): an unknown is not "still refused". A drain not confirmed yet (pending, or a
# heartbeat without the field) is read again at each refusal, and said once when it changes: the first reading
# is taken a few seconds after the marker, before the daemon's next pass.
POLL_S=60
wait_for_acceptance(){
  local polls=0 unread=0 h ds
  trap 'say ""; say "  [!] interrupted: nothing was sent. $DRAIN_TAIL"; exit 2' INT TERM
  say ""
  say "== [exit-miner] waiting for the chain to accept the exit (--wait: asked every $POLL_S s; Ctrl-C stops waiting, nothing is sent) =="
  while :; do
    sleep "$POLL_S" || { say "  [!] the wait was interrupted: nothing was sent. $DRAIN_TAIL"; exit 2; }
    helper simulate "$MID" "$CREATOR"
    SIM="$(val sim)"; SAID="$(val chain_said)"; polls=$((polls + 1))
    case "$SIM" in
      accepted) break ;;
      refused) unread=0; h="$(val height)"
               [ "$((polls % 10))" = 1 ] && say "  [..] $(date -u +%H:%M:%SZ)${h:+ height $h}: still refused"
               case "$DRAIN_STATE" in
                 pending|not-draining)
                   ds="$(drain_state)"
                   [ "$ds" = "$DRAIN_STATE" ] || { DRAIN_STATE="$ds"; say "  [..] the miner's drain, read again:"; report_drain "$ds"; } ;;
               esac ;;
      *) unread=$((unread + 1))
         [ "$unread" -ge 5 ] && unmeasurable "the exit could not be simulated $unread times in a row ($(val why)). Nothing was sent. $DRAIN_TAIL" ;;
    esac
  done
  trap - INT TERM
}
# restart_after_refusal -> the sentence refused_now ends with when the chain refuses AFTER this file stopped the
# miner. ⛔ A STOPPED MINER CANNOT REVEAL, AND A REVEAL MAY BE DUE: the primary of a job drawn for audit files
# its reveal (reveal_worker, started with the miner), and without it that audit cannot conclude -- the very
# obligation the chain just named, which a stopped miner would then keep open. A seat runs out on its own, an
# unfiled reveal does not. So a miner this file found RUNNING is STARTED AGAIN here, as it was before this run
# stopped it -- the drain marker still set -- and when that fails the operator is told to start it NOW. A
# miner that was ALREADY STOPPED when this file ran (WAS_RUNNING) is left as it was: it was stopped on purpose --
# a miner of an earlier kit, which does not know the drain, is stopped to keep it off new juries -- and starting
# it would put it back on them. Not knowing whether it ran is read as running: the reveal comes first.
# $1, when given, replaces the tail that names an obligation: a transaction the chain rejected or that failed is
# not a refusal by obligation, and is not said as one.
WAS_RUNNING=unknown
restart_after_refusal(){
  local why="${1:-an obligation that keeps this exit refused}" next="${2:-Run this file again once the chain no longer names an obligation.}"
  if [ "$WAS_RUNNING" = stopped ]; then
    printf '%s' "The miner was already STOPPED when this file ran, and stays stopped ($why). $next Stopped, it files no reveal: to have one filed, start it ($START_HINT). The drain marker stays set; to give the exit up: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
  elif [ "$(slot_state "$SLOT")" = active ] && slot_compose "$SLOT" up -d --no-build miner >/dev/null 2>&1; then
    printf '%s' "The miner was STARTED AGAIN by this file, the drain marker still set: stopped, it cannot file a reveal, and one may be due for a job of its own under audit -- $why. $next To give the exit up and take work again: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
  else
    printf '%s' "The miner is STOPPED and still registered: START IT AGAIN NOW ($START_HINT). Stopped, it cannot file a reveal, and one may be due for a job of its own under audit -- $why. The drain marker stays set; to give the exit up: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
  fi
}

# ---------------------------------------------------------------- the drain, FIRST (--yes)
# Set before the chain is asked, so that the miner takes no new seat while the ones it holds resolve. Written in
# the identity's key volume through the same one-off container as every reading, and read back.
if [ "$YES" = 1 ]; then
  say ""
  say "== [exit-miner] draining (the miner takes no new work and no new jury seat, and keeps filing its reveals) =="
  helper drain
  case "$(val drain)" in
    set)    say "  [OK] drain marker set in the miner's key volume (removed once the exit is verified; --undrain removes it)" ;;
    failed) fail "the drain marker could not be written: $(val why). Nothing else was done." ;;
    *)      unmeasurable "the miner's key volume could not be reached to set the drain: $(val why). Nothing else was done." ;;
  esac
fi

say ""
say "== [exit-miner] asking the chain (simulation, nothing is sent) =="
simulate
if [ "$SIM" != accepted ]; then
  [ "$YES" = 1 ] || refused_now "Nothing was changed. Run this again later -- with --yes, this file first sets the drain, so that the miner takes no new seat while the ones it holds resolve: bash $KIT/exit-miner.sh${SLOT_FLAG} --yes --wait" read
  DRAIN_STATE="$(drain_state)"
  [ "$WAIT" = 1 ] || refused_now "$DRAIN_TAIL" read-drain
  # --wait: the same reading once, then the chain is asked again until it accepts.
  refuse "the chain does not accept this exit yet. It said:"
  printf '           %s\n' "${SAID:-<no message>}" >&2
  report_obligations
  report_drain "$DRAIN_STATE"
  wait_for_acceptance
fi
say "  [OK] the chain would accept the exit now."

if [ "$YES" != 1 ]; then
  say ""
  if [ "$OWNER_MODE" = 1 ]; then
    say "  With --yes this file would: set the drain, stop the miner ($STOP_HINT), simulate"
    say "  again, write the UNSIGNED delete-miner of the owner $CREATOR to $SLOT_DIR/delete-miner.json and print"
    say "  the command that signs it on the owner's machine; then --yes --signed <file> broadcasts the signed"
    say "  file and checks that $(dndr "$STAKE") came back to $CREATOR. Nothing was done."
  else
    say "  With --yes this file would: set the drain, stop the miner ($STOP_HINT), simulate"
    say "  again, broadcast delete-miner, wait for its inclusion, then check that the miner is gone and that"
    say "  $(dndr "$STAKE") came back to $ADDR. Nothing was done. Re-run with --yes${SLOT_FLAG} to leave."
  fi
  exit 2
fi

say ""
say "== [exit-miner] leaving =="
# THE STOP COMES FIRST. A running daemon that sees itself absent from the registry registers again and
# stakes again: the exit would be undone within a minute, and the stake spent on a new registration.
# (In owner mode a running miner does not register again, but it can be drawn onto a new audit seat
# between this run and the owner's broadcast, and the chain would then refuse the exit.)
# Whether it RAN is read first: a refusal after the stop starts again only a miner this file stopped. With
# --signed, the stop is this file's own, from the run that prepared the owner's transaction: that miner ran.
WAS_RUNNING=unknown
if [ -n "$SIGNED" ]; then WAS_RUNNING=running
elif _cid="$(slot_cid "$SLOT" miner --running)"; then
  if [ -n "$_cid" ]; then WAS_RUNNING=running; else WAS_RUNNING=stopped; fi
fi
slot_compose "$SLOT" stop miner >/dev/null 2>&1 \
  || fail "the miner could not be stopped ($STOP_HINT): nothing was sent. $DRAIN_TAIL"
say "  [OK] miner stopped"
simulate after-stop
[ "$SIM" = accepted ] || refused_now "$(restart_after_refusal)"
if [ "$OWNER_MODE" = 1 ] && [ -z "$SIGNED" ]; then
  # PREPARED, NEVER SIGNED HERE: this machine does not hold the owner key, and the chain accepts the exit
  # from that key only. The unsigned transaction and the sign command come from modea/owner_tx.py.
  helper prepare "$MID" "$CREATOR"
  case "$(val prep)" in
    ok) : ;;
    refused) SAID="$(val chain_said)"; [ -n "$SAID" ] || SAID="$(val why)"
             refused_now "$(restart_after_refusal)" ;;
    # Nothing to sign, nothing sent: the owner's flow cannot go on, so the miner is started again as on a refusal.
    *) unmeasurable "the owner's delete-miner could not be prepared: $(val why). $(restart_after_refusal "no transaction was prepared, so nothing can be signed" "Run this file again with --yes once the node answers.")" ;;
  esac
  UNSIGNED="$SLOT_DIR/delete-miner.json"
  printf '%s' "$(val unsigned_b64)" | base64 -d > "$UNSIGNED" 2>/dev/null && [ -s "$UNSIGNED" ] \
    || unmeasurable "the unsigned delete-miner could not be written to $UNSIGNED (base64 -d). $(restart_after_refusal "no transaction was written, so nothing can be signed" "Run this file again with --yes once $SLOT_DIR can be written.")"
  say "  [OK] unsigned delete-miner written: $UNSIGNED (gas $(val gas); nothing was signed or sent)"
  say "       produced by: $(val generate)"
  say ""
  say "  1. on the machine that holds the owner key $CREATOR, offline (dendrad of the release binaries;"
  say "     add the --keyring-backend / --keyring-dir of that machine's keyring):"
  say "       $(val sign)"
  say "  2. back here, with the signed file:"
  say "       bash $KIT/exit-miner.sh${SLOT_FLAG} --yes --signed delete-miner.signed.json"
  say "     (or broadcast it from any machine that reaches the network: dendrad tx broadcast delete-miner.signed.json)"
  say ""
  say "  The miner stays STOPPED meanwhile: running, it could take a seat the chain then refuses the exit for. The"
  say "  drain marker stays set until the exit is verified. To give the exit up: START IT AGAIN ($START_HINT) and"
  say "  remove the drain: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
  exit 2
fi
# ⛔ AFTER THE STOP, EVERY END SAYS WHAT BECOMES OF THE MINER. restart_after_refusal used to follow the second
# simulation only: a broadcast the chain REJECTED, or a transaction included and FAILED, left the miner stopped
# and still registered -- the very state in which a reveal of its own may be due -- with "The miner is stopped."
# Those two are FINAL (a rejected transaction never enters a block, a failed one changed nothing): the miner is
# started again. An answer that cannot be read, or a transaction not seen in time, is NOT final -- it may still
# land, and a running daemon that then sees itself absent registers again and stakes again -- so the miner
# stays stopped, and the operator is told what to read before starting it.
STOPPED_UNKNOWN="The miner stays STOPPED: the exit may still be taken, and a running miner that sees itself gone registers again and stakes again. Run this file again: if it still names $MID registered, START IT AGAIN ($START_HINT) -- stopped, it cannot file a reveal that may be due. The drain marker stays set: once the miner reads as gone, --yes removes it; to give the exit up: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
if [ "$OWNER_MODE" = 1 ]; then
  [ -r "$SIGNED" ] || unmeasurable "the signed file $SIGNED cannot be read. Nothing was sent; the miner is STOPPED and still registered, waiting for the owner's signed delete-miner of $MID. To give up the exit, START IT AGAIN ($START_HINT) and remove the drain: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"
  helper_in "$SIGNED" broadcast_signed "$MID" "$CREATOR"
  # The FILE is refused here, before anything is sent: the owner's flow keeps the miner stopped while the right
  # file is signed (running, it could take a seat the chain then refuses the exit for) -- said, with the way back.
  [ "$(val bcast)" = refused ] && { refuse "$(val why). Nothing was sent; the miner is STOPPED and still registered, waiting for the owner's signed delete-miner of $MID. To give up the exit, START IT AGAIN ($START_HINT): stopped, it cannot file a reveal that may be due -- and remove the drain, so that it takes work again: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain"; exit 2; }
else
  helper broadcast "$MID"
fi
case "$(val bcast)" in
  ok) TXH="$(val txhash)"; say "  [OK] delete-miner broadcast: $TXH" ;;
  rejected) fail "the chain rejected delete-miner (code $(val code)): $(val raw_log). The miner is still registered. $(restart_after_refusal "an obligation the rejection leaves standing" "Run this file again: it reads where things stand.")" ;;
  *) fail "delete-miner gave no readable answer: $(val why). $STOPPED_UNKNOWN" ;;
esac
[ -n "$TXH" ] || fail "the broadcast was accepted without a transaction hash: its inclusion cannot be followed. $STOPPED_UNKNOWN"
helper included "$TXH"
case "$(val incl)" in
  ok) say "  [OK] included in a block" ;;
  failed) fail "the transaction was included and FAILED (code $(val code)): $(val raw_log). The miner is still registered. $(restart_after_refusal "an obligation the failed exit leaves standing" "Run this file again: it reads where things stand.")" ;;
  *) fail "the transaction was not seen in a block in time ($TXH). $STOPPED_UNKNOWN" ;;
esac

helper miner "$MID"
case "$(val state)" in
  absent) say "  [OK] the miner is no longer registered"
          # The hourly self-test (miner_health.sh) reads this marker: a stopped, deregistered miner is a
          # decision, not an alert to repeat every hour. It counts only while the miner stays stopped, and
          # join.sh removes it when it starts the miner again.
          if ( printf 'Dendra miner -- left the network\n  identity : %s\n  at       : %s\n  tx       : %s\n' \
                 "$MID" "$(date -u +%FT%TZ)" "$TXH" > "$SLOT_DIR/miner-health.EXITED" ) 2>/dev/null; then
            say "  [OK] recorded in $SLOT_DIR/miner-health.EXITED: the hourly self-test stays quiet while the miner stays stopped"
          else
            say "  [!] could not write $SLOT_DIR/miner-health.EXITED: the hourly self-test will report the stopped miner"
          fi
          # A slot k that left is also RETIRED: a plain re-run of deploy/join.sh then leaves it stopped, where it
          # would otherwise start it, and the daemon would register it again with a new stake.
          if [ "$SLOT" != 0 ] && [ "$(slot_state "$SLOT")" = active ]; then
            if slot_env_set "$SLOT" DENDRA_SLOT_RETIRED "$(date -u +%FT%TZ)"; then
              say "  [OK] slot $SLOT marked retired in $(slot_env "$SLOT"): a re-run of deploy/join.sh leaves it stopped"
            else
              say "  [!] slot $SLOT could not be marked retired in $(slot_env "$SLOT"): a re-run of deploy/join.sh would start it again"
            fi
          fi
          # THE DRAIN GOES WITH THE EXIT. Left in the volume, it would keep a miner started again on these keys
          # from registering (a draining daemon does not) and from all work, with nothing on screen to say why.
          helper undrain
          case "$(val drain)" in
            removed|absent) say "  [OK] drain marker removed: a miner started again on these keys takes work" ;;
            *) say "  [!] the drain marker could not be removed ($(val why)): a miner started again on these keys would"
               say "      neither register nor take work until: bash $KIT/exit-miner.sh${SLOT_FLAG} --undrain" ;;
          esac ;;
  registered) fail "the transaction was included, yet the chain still lists $MID. $STOPPED_UNKNOWN" ;;
  *) unmeasurable "included, but the miner record could not be read back: $(val why). $STOPPED_UNKNOWN" ;;
esac
helper balance "$CREATOR"
BAL1=""; [ "$(val state)" = read ] && BAL1="$(val udndr)"
if [ -z "$BAL0" ] || [ -z "$BAL1" ]; then
  say "  [?] balance not compared (unread before or after): read it with the Dendra application, or"
  say "      dendrad query bank balances $CREATOR"
elif [ "${STAKE:-0}" -gt 0 ] 2>/dev/null && [ "$BAL1" -le "$BAL0" ] 2>/dev/null; then
  fail "the miner is gone but the balance did not rise ($(dndr "$BAL0") -> $(dndr "$BAL1")) for a stake of $(dndr "$STAKE")."
else
  say "  [OK] balance $(dndr "$BAL0") -> $(dndr "$BAL1")"
fi
say ""
say "  Done. The miner is STOPPED and deregistered; starting it again registers it and stakes again."
if [ "$NSLOTS" -ge 2 ]; then
  say "  The other identities of this machine keep running (bash $KIT/slots.sh list)."
  [ "$SLOT" != 0 ] && say "  'deploy/join.sh --gpus all' would start this card's identity again (and register it with a new stake):" \
                   && say "  name the cards to keep instead (--gpus <indices>)."
else
  say "  To remove the kit from this machine: bash $(cd "$KIT/../.." 2>/dev/null && pwd)/deploy/uninstall.sh"
fi
exit 0
