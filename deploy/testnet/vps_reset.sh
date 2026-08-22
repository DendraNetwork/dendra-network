#!/usr/bin/env bash
# vps_reset.sh - DESTROYS the testnet hosted on a VPS: stack, containers, volumes, images, build
# cache and ~/dendra. Docker itself stays installed.
#
# THIS SCRIPT IS BUILT BACKWARDS FROM THE REST OF THE KIT: instead of doing its best, it looks for
# reasons to REFUSE. What it erases is not only the operator's own state - a joined validator's
# bonded stake and a registered miner's balance live in the very volumes it removes, and no later
# work reconstitutes them. A destructive command must therefore not depend on the freshness of the
# document that quotes it: the gates live HERE, where they cannot go stale.
#
# TWO MODES, safest first:
#   DRY RUN (default)                       reads the remote inventory, prints every destructive
#                                           command in the order it would run, executes NONE.
#                                           Repeatable as often as needed.
#   DESTRUCTION (--go --confirm I-DESTROY)  for real, and only once the gates below are green.
#
# THE GATES (each one guards a loss that cannot be bought back):
#   1. TWO SEPARATE ARGUMENTS. `--go` alone does nothing, the phrase alone does nothing. A wipe must
#      not be one keystroke away from a command that appears in a document.
#   2. THE REMOTE STATE WAS ACTUALLY READ. No SSH, no destruction: wiping a machine whose state
#      could not be read is acting blind on the half that costs the most to rebuild.
#   3. NO THIRD PARTY IS ON THE CHAIN. The chain is queried BEFORE anything is touched: more than
#      one validator in the set, or one registered miner, means these volumes carry a stake that
#      belongs to somebody else. NOT KNOWING IS NOT A GREEN LIGHT - an unreachable chain keeps this
#      gate RED rather than opening it, because "no answer" and "nobody there" are the same silence.
#      Deliberate override, typed in full: --erase-third-parties I-ERASE-OTHER-OPERATORS
#      It is restated in the final verdict: this gate can be passed, it cannot be passed quietly.
#
# WHAT THIS SCRIPT DOES NOT DO: shelter keys. It removes the volumes that hold
# priv_validator_key.json and the genesis keyring, and it does not copy them out first. Back them up
# before running it with --go; a validator key erased kills that validator for good.
#
# Usage - one line, from the repository root:
#   bash deploy/testnet/vps_reset.sh <VPS_IP>                            (dry run, destroys nothing)
#   SSHPASS='<root password>' bash deploy/testnet/vps_reset.sh <VPS_IP> --go --confirm I-DESTROY
set -uo pipefail

VPS=""; GO=0; DRY=0; CONF=""; THIRD=""
PHRASE="I-DESTROY"; THIRD_PHRASE="I-ERASE-OTHER-OPERATORS"
usage(){
  echo "Usage:"
  echo "  bash deploy/testnet/vps_reset.sh <VPS_IP>                       (dry run, destroys nothing)"
  echo "  SSHPASS='<root password>' bash deploy/testnet/vps_reset.sh <VPS_IP> --go --confirm $PHRASE"
}
# `shift 2` on a flag whose value is missing shifts NOTHING and returns non-zero, which turns this
# loop into an infinite one. Each value is therefore consumed in two guarded steps.
while [ $# -gt 0 ]; do case "$1" in
  --go) GO=1; shift;;
  --dry-run|--simulation) DRY=1; shift;;
  --confirm) CONF="${2:-}"; shift; [ $# -gt 0 ] && shift;;
  --erase-third-parties) THIRD="${2:-}"; shift; [ $# -gt 0 ] && shift;;
  -h|--help) usage; exit 0;;
  -*) echo "  unknown flag: $1"; usage; exit 2;;
  *) [ -z "$VPS" ] && VPS="$1"; shift;;
esac; done
[ -n "$VPS" ] || { usage; exit 2; }

# `--dry-run` and `--go` together have no meaning, and the ambiguity is on the wrong side: refuse
# rather than guess which one wins.
if [ "$DRY" = "1" ] && [ "$GO" = "1" ]; then
  echo "[STOP] --dry-run and --go contradict each other. Choose: read, or destroy."; exit 2
fi
MODE="DRY RUN"; [ "$GO" = "1" ] && MODE="DESTRUCTION"
RED=0; TS="$(date -u +%Y%m%d-%H%M%S)"
say(){ printf '%s\n' "$*"; }
ok(){ say "   [OK] $*"; }
ko(){ RED=$((RED+1)); say "   [RED] $*"; }
# In DRY RUN every destructive step goes through `sg`: it prints at its exact place in the sequence,
# with the command AS IT WOULD BE RUN, and it is counted. The count is what lets an operator say
# "I reviewed N gestures" instead of "I reviewed the script".
NGEST=0; sg(){ NGEST=$((NGEST+1)); say "   [DRY RUN] $*"; }

say "=============================================================="
say "  VPS RESET - $(date -u '+%F %T UTC')   host=$VPS   mode=$MODE"
say "=============================================================="
[ "$GO" = "1" ] || say "  Nothing will be destroyed in this mode. Read below, then decide."

# --- HOW THE VPS IS REACHED ------------------------------------------------------------------------
# `StrictHostKeyChecking=accept-new` rather than `no`: an unknown machine is accepted on first
# contact, a host key that CHANGES is refused. With `no`, `docker compose down` could be sent to an
# interceptor. `BatchMode=yes` makes the key attempt fail fast instead of opening an interactive
# prompt in the middle of a wipe.
SSH_MODE=""
if [ -n "${SSHPASS:-}" ] && command -v sshpass >/dev/null 2>&1; then
  SSH_MODE="password"
  S(){ sshpass -e ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$VPS" "$@"; }
elif ssh -o BatchMode=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=accept-new \
         -o ConnectTimeout=15 -n "root@$VPS" true 2>/dev/null; then
  SSH_MODE="key"
  S(){ ssh -n -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$VPS" "$@"; }
else
  S(){ return 1; }
fi

say ""
say "########## 1) DIAGNOSTICS - last chain logs (read only) ##########"
if [ -n "$SSH_MODE" ] && S 'true' 2>/dev/null; then
  ok "gate 2: remote state readable (over $SSH_MODE authentication)"
  S 'cd ~/dendra 2>/dev/null && docker compose logs --tail 50 chain 2>&1 | tail -50 || echo "  (no stack running)"'
else
  ko "gate 2: NO SSH ACCESS to $VPS - the remote state cannot be read.
      Destroying a machine whose state could not be read is acting blind on the half that costs
      the most to rebuild. Fix the access first:
        by key      : ssh-copy-id -i ~/.ssh/id_ed25519.pub root\@$VPS
        by password : SSHPASS='...' bash deploy/testnet/vps_reset.sh $VPS --go --confirm $PHRASE
                      (sshpass required: sudo apt install -y sshpass)"
fi

say ""
say "########## 2) INVENTORY - what would be destroyed (read, not assumed) ##########"
if [ -n "$SSH_MODE" ]; then
  S "docker ps -a --format '      container {{.Names}} ({{.Status}})' 2>/dev/null | head -15"
  S "docker volume ls --format '      volume {{.Name}}' 2>/dev/null | head -15"
  S "du -sh ~/dendra 2>/dev/null | sed 's|^|      |' ; df -h / | tail -1 | sed 's|^|      disk |'"
fi

say ""
say "########## 3) WHO ELSE IS ON THIS CHAIN ##########"
# The chain is asked, not the document. Both endpoints are the ones a third party is told to use, so
# they are reachable exactly when a third party could have joined.
if ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  ko "gate 3: python3 or curl missing on this machine - who else holds value on this chain cannot
      be judged. A security criterion with no reading is an error, never a permissive default."
else
  TIERS_OUT="$(python3 - "$VPS" <<'PYEOF' 2>&1
import json
import sys
import urllib.error
import urllib.request

host = sys.argv[1]
# `?` means NOT READ. It never equals 0: a count that could not be fetched does not read as "none".
UNREAD = "?"


def _get(url, timeout=20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


st = _get("http://%s:26657/validators?per_page=100" % host, timeout=15)
vals = None
if isinstance(st, dict):
    res = st.get("result")
    if isinstance(res, dict) and isinstance(res.get("validators"), list):
        vals = res["validators"]
# Bounded from BELOW as well as above: a responding node always reports at least one validator, so
# an empty set is a failed read, not an empty chain. `total` is a JSON string and is not counted on.
nval = len(vals) if vals else None

mn = _get("http://%s:1317/dendra/jobs/v1/miner?pagination.limit=500" % host, timeout=25)
nmin = None
truncated = False
if isinstance(mn, dict):
    lst = mn.get("miner")
    # proto3 omits an empty repeated field: `miner` ABSENT means zero miners, which is a real
    # reading. Only a failed request is unknown.
    nmin = len(lst) if isinstance(lst, list) else 0
    pg = mn.get("pagination")
    if isinstance(pg, dict) and (pg.get("next_key") or ""):
        truncated = True

print("   validators in the set : %s" % (nval if nval is not None else UNREAD))
print("   registered miners     : %s%s" % (
    nmin if nmin is not None else UNREAD, " (further page pending)" if truncated else ""))

if nval is None or nmin is None:
    print("   VERDICT: NOT MEASURED - the chain did not answer on %s:26657 / %s:1317." % (host, host))
    print("            Not knowing is not a green light. Either the chain is already down, or this")
    print("            machine cannot reach it; neither says the volumes are worthless to others.")
    raise SystemExit(2)
if nmin == 0 and truncated:
    print("   VERDICT: NOT MEASURED - the miner registry answered a truncated page with no entry.")
    raise SystemExit(2)
if nval > 1 or nmin > 0:
    print("   VERDICT: THIRD PARTIES PRESENT - %d validator(s) and %d registered miner(s)." % (nval, nmin))
    print("            Bonded stake and miner balances live in the volumes this script removes.")
    raise SystemExit(1)
print("   VERDICT: only this operator (1 validator, 0 miner) - nobody else loses value here.")
raise SystemExit(0)
PYEOF
)"; TIERS_RC=$?
  printf '%s\n' "$TIERS_OUT"
  THIRD_ALERT=""
  case "$TIERS_RC" in
    0) ok "gate 3: no third party holds value on this chain" ;;
    *) if [ "$THIRD" = "$THIRD_PHRASE" ]; then
         THIRD_ALERT="gate 3 KNOWINGLY OVERRIDDEN (--erase-third-parties $THIRD_PHRASE)"
         say "   [WARN] $THIRD_ALERT"
       else
         ko "gate 3: see the verdict above. This wipe would erase what a third party bonded, and no
      later work gives it back. Announce and wait, or - in full knowledge - add:
        --erase-third-parties $THIRD_PHRASE"
       fi ;;
  esac
fi

# --- VERDICT OF THE GATES --------------------------------------------------------------------------
say ""
say "--------------------------------------------------------------"
# The override is restated where the DECISION is taken, not only where it scrolled past.
[ -z "${THIRD_ALERT:-}" ] || {
  say "  [WARN] REMINDER - $THIRD_ALERT"
  say "  Somebody else's bonded stake goes with these volumes, and it is not recoverable."
}
if [ "$RED" != "0" ]; then
  say "  $RED GATE(S) RED - NOTHING HAS BEEN TOUCHED, and nothing will be."
  if [ "$GO" = "1" ]; then say "--------------------------------------------------------------"; exit 3; fi
  say "  (DRY RUN: the sequence is still shown below. These gates would stop a real --go.)"
else
  ok "the blocking gates are green"
fi
if [ "$GO" != "1" ]; then
  say ""
  say "  DRY RUN - the destructive sequence, gesture by gesture:"
elif [ "$CONF" != "$PHRASE" ]; then
  say "  [STOP] --go without --confirm $PHRASE. A wipe does not happen on a single argument."
  say "--------------------------------------------------------------"
  exit 2
fi

say ""
say "########## 4) FULL WIPE ##########"
WIPE='cd ~/dendra 2>/dev/null && docker compose down -v --remove-orphans 2>/dev/null; \
  ids=$(docker ps -aq); [ -n "$ids" ] && docker rm -f $ids; \
  docker system prune -af --volumes; \
  rm -rf ~/dendra; \
  echo "--- disk ---"; df -h /'
if [ "$GO" != "1" ]; then
  sg "ssh root@$VPS : cd ~/dendra && docker compose down -v --remove-orphans   (stops the stack, DELETES its volumes)"
  sg "ssh root@$VPS : docker rm -f \$(docker ps -aq)                            (removes every container on the host)"
  sg "ssh root@$VPS : docker system prune -af --volumes                         (removes EVERY image, volume and build cache on the host, dendra's or not)"
  sg "ssh root@$VPS : rm -rf ~/dendra                                           (IRREVERSIBLE)"
else
  S "$WIPE" || { echo "  WIPE FAILED"; exit 1; }
fi

say ""
say "=============================================================="
if [ "$GO" != "1" ]; then
  say "  DRY RUN FINISHED - $NGEST destructive gesture(s) shown, ZERO executed."
  say "  Nothing was stopped, removed or deleted on $VPS."
  if [ "$RED" != "0" ]; then
    say "  But $RED GATE(S) were RED: a real --go would have stopped BEFORE the first gesture."
    say "=============================================================="
    exit 3
  fi
  say "  When the sequence above looks right, and only then:"
  say "    bash deploy/testnet/vps_reset.sh $VPS --go --confirm $PHRASE"
else
  say "  VPS CLEAN (Docker kept). Ready for a fresh testnet deployment."
  say "  Reference for this run: $TS"
fi
say "=============================================================="
