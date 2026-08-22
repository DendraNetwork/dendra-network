#!/usr/bin/env bash
# bond_validator.sh — bond a SECOND validator and anchor its VRF key, in one confirmed command.
#
# WHY THIS EXISTS
#   `join.sh --validator` syncs the node and then PRINTS a six-step sequence: create a key, fund it,
#   author a validator.json, pipe a script into the container, set an env var, restart, verify. That
#   sequence is correct and it is never wrong to keep a bond deliberate — but printing six fiddly steps
#   is how a network ends up running for days on a single validator, with `contributors: 0`, which makes
#   the committee seed centralised and every downstream measurement worthless. This script keeps the
#   deliberation and removes the assembly.
#
# THE INVARIANT IS PRESERVED: bonding LOCKS UP STAKE, so nothing is bonded without `--yes-bond`.
#   Without that flag the script inspects, computes the amount, prints the exact plan (address, amounts,
#   resulting stake distribution) and stops. Run it once to see, once to do.
#   Precisely what a dry run does write: it creates the local operator key if absent (idempotent, in the
#   node's own keyring). That is the only write — no transaction is broadcast, no stake is bonded, no
#   money moves, no container is restarted. Saying "read-only" would have been a small lie, and this
#   project does not get to demand honest claims from its documentation and not from its scripts.
#
# Usage (WSL, repo root) — the node must already be synced by `deploy/join.sh --validator`:
#   bash deploy/bond_validator.sh                                  # DRY RUN: shows the plan, changes nothing
#   bash deploy/bond_validator.sh --yes-bond                       # bonds (funding done manually, printed)
#   export SSHPASS='<vps root password>'
#   bash deploy/bond_validator.sh --vps <IP> --yes-bond            # also funds from the operator pocket
#
# ⚠️ THE DEFAULT AMOUNT MATCHES THE LARGEST VALIDATOR, AND THE FAUCET CANNOT REACH IT.
#   With no `--amount`, the plan below proposes the incumbent's own stake (`X=M` in step 3). On this
#   network that is 1,000,000 DNDR, while the public faucet hands out 10 DNDR per address per day —
#   about 110,000 draws. A newcomer funded the public way therefore CANNOT take the default, and the
#   2/3 guard then refuses any smaller amount, so the script stops with no reachable option printed.
#   Both escapes exist, and `--help` has to reach this far to show them (see the range at the parser):
#     · `--amount <udndr>`             bond what you actually have
#     · `--accept-supermajority`       accept that the incumbent keeps >2/3 (read what it costs below)
#   A newcomer with faucet money wants BOTH: a small `--amount`, and the flag that acknowledges the
#   incumbent stays dominant — which is already true today and which this flag does not create.
#
# Options: --amount <udndr>  force the bonded amount   --accept-supermajority   --moniker <name>
#          --key <name> (default: validator)   --vps <IP>   --yes-bond
set -uo pipefail

YES=0; VPS=""; AMOUNT=""; MONIKER=""; VAL_KEY="validator"; FUND_FROM="${DENDRA_FUND_FROM:-bob}"; SUPERMAJ=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes-bond) YES=1;;
    # Allow an amount that leaves the INCUMBENT above 2/3. This is not a convenience escape hatch: it
    # is the opposite trade-off from the one this script takes by default, and it defends itself in one
    # precise case. By default the script refuses to let a single validator produce blocks alone
    # (censorship resistance) — at the price that ALL validators are then needed to advance, i.e. zero
    # fault tolerance at two. On a desktop machine that goes to sleep, that price is a chain that stops
    # overnight. With this flag the opposite is assumed: the incumbent keeps the supermajority and its
    # going offline freezes nothing.
    # ⛔ BUT THE LINE THAT USED TO SIT HERE WAS MEASURABLY FALSE, AND IT SOLD THIS FLAG ON IT. It said
    # the newcomer "contributes ONLY to the VRF seed (the floor counts contributors, not their
    # weight)". It does not. Under a supermajority the incumbent's own prevote IS the polka, so it
    # forms before the block finishes reaching anyone else; a validator cannot sign a block it does not
    # have, so it precommits nil — and a vote extension rides ONLY on a Commit precommit, taking the
    # VRF contribution with it. Measured on this chain (see docs/adr/ADR-041): a bonded, unjailed,
    # never-absent newcomer contributed to the seed on 10–21 % of heights, and the count reached the
    # floor on 17.9 % of them. The seed stays centralised almost all the time.
    # So the honest trade-off is: this flag buys AVAILABILITY (the newcomer can sleep) and buys almost
    # NOTHING for decentralisation. The way to buy both is THREE validators, incumbent below 2/3 with
    # TWO partners, so that incumbent + either one still concludes — see the same record.
    # Use it in a test phase only, and knowing that "nobody produces alone" is NOT acquired.
    --accept-supermajority) SUPERMAJ=1;;
    --vps)      VPS="${2:?--vps needs an IP}"; shift;;
    --amount)   AMOUNT="${2:?--amount needs a value in udndr}"; shift;;
    --moniker)  MONIKER="${2:?--moniker needs a name}"; shift;;
    --key)      VAL_KEY="${2:?--key needs a name}"; shift;;
    # RANGE MUST REACH THE END OF THE HEADER, i.e. past the `Options:` lines and past the two flags a
    # newcomer needs (`--amount`, `--accept-supermajority`). A range that stops earlier prints the
    # rationale and cuts off before them: the operator hits the 2/3 guard, is told to "raise it with
    # --amount", and has no way to learn the flag exists. A help text that stops before the flags is
    # not help — widen this range whenever the header grows.
    -h|--help)  sed -n '2,38p' "$0"; exit 0;;
    *) echo "unknown option: $1 (see --help)"; exit 1;;
  esac; shift
done

_find_repo(){ d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "FAIL: repo root not found (export DENDRA_REPO)"; exit 1; }
KIT="$REPO/deploy/testnet-node"
# ⛔ SEAM: overridable so a bench can drive this script against a FAKE node. Without it nothing could
# execute the confirmation below, and a guard no bench can invoke is a guard that dies unnoticed —
# which is exactly how the broadcast-vs-execution defect survived here. Default unchanged.
DC="${DENDRA_DC_CMD:-docker compose -f $KIT/docker-compose.yml}"
say(){ printf '%s\n' "$*"; }
die(){ echo "  FAIL: $*"; exit 1; }
# -T: no TTY, so the command is scriptable. </dev/null: without it `docker exec` EATS the stdin of a
# piped script and the rest of the file is silently skipped.
nx(){ $DC exec -T node "$@" </dev/null 2>/dev/null; }

CHAIN_ID="$(grep -E '^CHAIN_ID=' "$KIT/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r')"; CHAIN_ID="${CHAIN_ID:-dendra}"
HOME_DIR=/root/.dendra
KB="--keyring-backend test --home $HOME_DIR"
NODE_LOCAL="tcp://localhost:26657"

# confirm_execution — THE RESULT OF A TRANSACTION IS WHAT THE CHAIN EXECUTED.
# ZERO RULE, the reading this repository applies to every JSON answer: NEVER test a JSON field
# with a TEXTUAL predicate. The proto3 codec OMITS a field at its zero value, so on a SUCCESSFUL
# transaction `code` is ABSENT — `grep '"code":0'` then reads success as "no answer".
# `deploy/launch/launch_public.sh` records the incident: the gateway seeding was reported refused on a
# send that had gone through. These two readers PARSE, and they keep THREE answers apart:
#   executed (code 0) · rejected (code != 0) · no answer yet (empty output, keep polling).
# What proves an answer arrived is `txhash` — a hash is never zero, so it is never omitted. Once it is
# there, an ABSENT `code` is a zero, which is success. `python3` is not a new dependency here: the
# supermajority guard below already decides with it.

# execution_code — reads a `query tx` answer on stdin. Prints the execution code, or NOTHING when no
# usable answer came back. Empty is the third answer, never a fourth spelling of success.
execution_code() {
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
if isinstance(d, dict) and d.get("txhash"): print(d.get("code", 0))
' 2>/dev/null
}

# broadcast_txhash — reads a broadcast answer on stdin, prints its txhash (nothing when there is none,
# which is how "not even broadcast" is told apart from "broadcast and rejected").
broadcast_txhash() {
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
if isinstance(d, dict): print(d.get("txhash", "") or "")
' 2>/dev/null
}

# broadcast_accepted — reads a broadcast answer on stdin. Returns 0 only if the chain took it in.
broadcast_accepted() {
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(1)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(1)
sys.exit(0 if isinstance(d, dict) and d.get("txhash") and int(d.get("code", 0)) == 0 else 1)
' 2>/dev/null
}

# A `"code":0` on BROADCAST only says it entered the mempool; with zero fees the ante-handler
# passes and it can still fail at EXECUTION. The pattern comes from
# `deploy/testnet/join_validator.sh`, which has documented it in capitals for a long time; this
# script, which became the recommended path, had not adopted it. Returns 0 if and only if the chain
# executed it. $1 = broadcast JSON output, $2 = label.
confirm_execution() {
  _BR="$1"; _LBL="$2"
  _HASH="$(printf '%s' "$_BR" | broadcast_txhash)"
  [ -n "$_HASH" ] || { say "  [ERR] $_LBL: not even broadcast (no txhash)."; return 1; }
  _CODE=""
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    _Q="$($DC exec -T node dendrad query tx "$_HASH" --node "$NODE_LOCAL" -o json 2>/dev/null </dev/null)"
    _CODE="$(printf '%s' "$_Q" | execution_code)"
    [ -n "$_CODE" ] && break
    [ "${DENDRA_BOND_NOSLEEP:-0}" = "1" ] || sleep 5
  done
  [ -n "$_CODE" ] || { say "  [ERR] $_LBL: tx $_HASH never made it into a block."; return 1; }
  [ "$_CODE" = "0" ] || { say "  [ERR] $_LBL REJECTED at EXECUTION (code=$_CODE, tx $_HASH)."; return 1; }
  say "  [OK] $_LBL EXECUTED (tx $_HASH)."
  return 0
}

# LIBRARY MODE — the same seam as `join.sh`: load the functions and stop before any action, so a
# bench can drive the SHIPPED decision instead of replaying its rule.
case "${DENDRA_BOND_LIB:-0}" in 1) return 0 2>/dev/null || exit 0;; esac

# ZERO RULE. `catching_up` is a BOOL, and `false` -- the value that means SYNCED, the one this gate
# exists to wait for -- is the zero of its type. A textual read that needs the word `false` to be
# present therefore refuses the very state it is looking for, and tells the operator to "wait for
# sync" on a node that finished syncing. The answer is parsed; what proves an answer arrived is
# `sync_info`; once it is there, an ABSENT `catching_up` IS false. Three answers, never two:
# synced / catching-up / nothing readable -- and the third refuses, because bonding a lagging
# validator gets it jailed and an unknown state is not a green light.
sync_state() { # reads a `status` answer on stdin; prints synced | catching-up | NOTHING
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
si = d.get("sync_info")
if not isinstance(si, dict): si = d.get("SyncInfo")
if not isinstance(si, dict): sys.exit(0)
print("catching-up" if si.get("catching_up", False) else "synced")
' 2>/dev/null
}

say "== [bond] 1) node reachable and SYNCED? =="
nx dendrad status >/dev/null || die "node container not answering — run 'deploy/join.sh --validator' first."
CU="$(nx dendrad status | sync_state)"
case "$CU" in
  synced)      ;;
  catching-up) die "node still catching_up — bonding a lagging validator gets it jailed. Wait for sync." ;;
  *)           die "node sync state NOT READABLE — refusing to bond on a state nobody measured." ;;
esac
say "  [OK] synced (catching_up=false), chain-id=$CHAIN_ID"

say "== [bond] 2) operator key (idempotent) =="
ADDR="$(nx dendrad keys show "$VAL_KEY" -a $KB | tr -d '\r\n ')"
if [ -z "$ADDR" ]; then
  # ⛔ THE MNEMONIC IS THE ONLY WAY BACK, AND IT IS PRINTED EXACTLY ONCE.
  # `keys add` writes its BIP39 seed phrase to the command output and never again. `nx()` appends
  # `2>/dev/null`, and a `>/dev/null` on the call would close the other channel, so running key creation
  # through either one discards the phrase into a closed pipe. The key then exists ONLY inside the
  # container's keyring, in the `test` backend, unencrypted — one lost volume and the bonded stake is
  # unreachable forever, with no phrase anyone could have written down. A destroyed recovery phrase is
  # not a UX detail: it is the difference between a redeployable validator and stake that is gone.
  # So: capture the output, keep it on the HOST (outside the Docker volume, which is what disappears),
  # mode 0600, and say plainly what it is.
  _KEYOUT="$($DC exec -T node dendrad keys add "$VAL_KEY" $KB --output json </dev/null 2>&1)" \
    || die "key creation failed: $(printf '%s' "$_KEYOUT" | tail -3)"
  _BAK="$KIT/validator-key-backup.json"
  ( umask 077; printf '%s\n' "$_KEYOUT" > "$_BAK" ) || die "cannot write the key backup to $_BAK"
  chmod 600 "$_BAK" 2>/dev/null || true
  say ""
  say "  =========================== READ THIS ONCE ==========================="
  say "  A new operator key was created. Its RECOVERY PHRASE is the only way to"
  say "  restore this validator if the Docker volume is lost — and the keyring"
  say "  backend is 'test', i.e. UNENCRYPTED on disk."
  say "  Written to: $_BAK  (mode 0600, on the host, outside the volume)"
  say "  Move it somewhere safe and delete it from here. Anyone holding that"
  say "  phrase controls the bonded stake."
  say "  ======================================================================"
  say ""
  ADDR="$(nx dendrad keys show "$VAL_KEY" -a $KB | tr -d '\r\n ')"
fi
[ -n "$ADDR" ] || die "no address for key '$VAL_KEY'"
say "  key '$VAL_KEY' = $ADDR"
say "  NOTE: keyring 'test' = keys UNENCRYPTED on disk. Fine for a resettable testnet, never for real value."

say "== [bond] 3) current stake distribution + safe amount =="
VJSON="$(nx dendrad query staking validators -o json --node $NODE_LOCAL)"
PLAN="$(printf '%s' "$VJSON" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("ERR|0|0|0"); raise SystemExit
vs=d.get("validators",[])
toks=[int(v.get("tokens","0")) for v in vs if v.get("status")=="BOND_STATUS_BONDED"] or [int(v.get("tokens","0")) for v in vs]
T=sum(toks); M=max(toks) if toks else 0
# Match the LARGEST validator: with one incumbent that is a 50/50 split, and with several it can never
# push anyone over 2/3. A validator above 2/3 makes the others invisible to the committee seed.
X=M if M>0 else 1000000
print("OK|%d|%d|%d|%d" % (T,M,X,len(toks)))
')"
case "$PLAN" in ERR*|"") die "could not read the validator set (RPC down?)";; esac
IFS='|' read -r _ TOTAL MAXV SUGG NVAL <<EOF
$PLAN
EOF
# THE DENOM SUFFIX IS ADDED BY THIS SCRIPT (step 5 writes "${BOND}udndr"), so an --amount that already
# carries it breaks BOTH the arithmetic below and the transaction body. The failure is worse than a
# crash: `$(( ))` errors and leaves NEWTOP/NEWTOT empty, the guard's python then dies of a SyntaxError,
# `if` reads a non-zero exit as "false", and WITHOUT --accept-supermajority the script stops with
# "this amount would leave a validator at 2/3 or above" -- a refusal that names the wrong cause and
# sends the operator to RAISE an amount that was never the problem. Measured, not supposed.
# This script printed that exact malformed example itself, in --help and in the community checklist.
case "${AMOUNT:-0}" in
  ''|*[!0-9]*) die "--amount takes a BARE number of udndr (got '$AMOUNT'): write --amount 600000000000, NOT 600000000000udndr -- this script appends the denom itself" ;;
esac
BOND="${AMOUNT:-$SUGG}"
NEWTOP=$(( MAXV > BOND ? MAXV : BOND )); NEWTOT=$(( TOTAL + BOND ))
PCT=$(python3 -c "print(f'{100*$NEWTOP/$NEWTOT:.1f}')" 2>/dev/null || echo "?")
say "  validators today : $NVAL   total bonded: $TOTAL udndr   largest: $MAXV udndr"
# THE SHARE THAT IS PRINTED HAS TO BE THE ONE THE OPERATOR IS BUYING. This block used to report only
# the LARGEST validator's share -- the number the 2/3 guard needs -- and never the newcomer's own. An
# operator bonding the suggested minimum reads "[OK] no validator would reach 2/3", concludes the set is
# healthy, and never learns that its own weight is a rounding error in it.
YOURPCT="$(python3 -c "print(f'{100*$BOND/$NEWTOT:.4f}')" 2>/dev/null || echo "?")"
say "  proposed bond    : $BOND udndr  ->  largest would hold ${PCT}% of $NEWTOT (must stay < 66.7%)"
say "  YOUR share       : ${YOURPCT}% of the voting power once bonded"
if python3 -c "import sys; sys.exit(0 if 3*$NEWTOP < 2*$NEWTOT else 1)"; then
  say "  [OK] no validator would reach 2/3: nobody would produce blocks alone."
  say "       Accepted trade-off: with two validators, losing EITHER ONE stops the chain."
elif [ "$SUPERMAJ" = "1" ]; then
  say "  [!] SUPERMAJORITY ACCEPTED (--accept-supermajority): the largest would hold ${PCT}% >= 66.7%."
  say "      What you gain: availability. This validator going offline - sleep, restart, outage - does"
  say "      NOT stop the chain, because the incumbent concludes without it."
  say "      What you lose, and it is more than it looks: this validator will precommit nil on most"
  say "      blocks. Not a fault - the incumbent's own prevote is already a supermajority, so it forms"
  say "      before the block reaches you, and nobody signs a block they do not have. Measured on this"
  say "      chain: 10-21 % Commit, so your VRF contribution reaches the seed on 10-21 % of heights."
  say "      You will look perfectly healthy throughout: present, never absent, never jailed."
  say "      To actually count, the incumbent must drop below 2/3 - ideally with TWO partners, so that"
  say "      losing either one does not stop the chain. See docs/adr/ADR-041."
else
  die "this amount would leave a validator at 2/3 or above - raise it with --amount, or take the
     opposite trade-off with --accept-supermajority (read what it costs: --help)"
fi

# ---- WILL YOUR VOTE ACTUALLY BE NEEDED? -----------------------------------------------------------
# THIS WARNING USED TO LIVE INSIDE THE SUPERMAJORITY BRANCH, WHICH IS THE WRONG CONDITION. What makes a
# validator vote `nil` is not that ONE incumbent holds two thirds -- it is that the OTHERS reach two
# thirds WITHOUT it. The commit then forms before the block has reached this node, and nobody signs a
# block they do not have. So the test is on the existing set as a whole, and it is derived rather than
# written: the incumbents alone clear 2/3 of the new total exactly when TOTAL >= 2 x BOND.
# A validator in that position is PRESENT, never absent, never jailed, and contributes almost nothing.
# The `[OK]` printed above answers a different question -- whether anyone produces blocks alone -- and
# says nothing about whether YOUR vote counts.
if [ "$TOTAL" -ge $(( BOND * 2 )) ] 2>/dev/null; then
  say ""
  say "  [!] YOUR VOTE WILL RARELY BE NEEDED, AND THAT IS NOT A FAULT."
  say "      The validators already bonded hold at least two thirds of the new total WITHOUT you, so the"
  say "      commit forms before the block reaches this node -- and a node does not sign a block it does"
  say "      not have. You will precommit nil on most heights, which means your VRF share reaches the"
  say "      committee seed on those heights only. Measured on this chain under a supermajority: 10-21 %"
  say "      of heights. You will look perfectly healthy throughout: bonded, present, never absent,"
  say "      never jailed, and almost without effect."
  say "      What changes it is not a bigger bond by one operator, it is MORE operators: see"
  say "      docs/adr/ADR-041 for why the target is three validators rather than two."
fi

BAL="$(nx dendrad query bank balances "$ADDR" -o json --node $NODE_LOCAL | python3 -c '
import json,sys
try: b=json.load(sys.stdin).get("balances",[])
except Exception: b=[]
print(next((c["amount"] for c in b if c.get("denom")=="udndr"),"0"))' 2>/dev/null || echo 0)"
BAL="${BAL:-0}"
NEED=$(( BOND + BOND / 10 ))   # bond + 10% headroom (fees are 0 on this testnet, but leave slack)
say "  balance of $ADDR : $BAL udndr (needs >= $NEED)"

VALOPER="$(nx dendrad keys show "$VAL_KEY" --bech val -a $KB | tr -d '\r\n ')"
ALREADY=0
[ -n "$VALOPER" ] && nx dendrad query staking validator "$VALOPER" -o json --node $NODE_LOCAL | grep -q '"operator_address"' && ALREADY=1

say "== [bond] 4) funding =="
# This check has to come BEFORE the transfer, not after. Placed after it, a re-run on an ALREADY-bonded
# validator dutifully sends a second bond's worth of tokens and only then says "already a validator —
# skipping". That is idempotent in name only: it skips the cheap step and repeats the expensive one,
# moving a second bond's worth of tokens for nothing.
if [ "$ALREADY" = 1 ]; then
  say "  [OK] $VALOPER is already bonded — no funding needed, nothing to send."
elif [ "$BAL" -ge "$NEED" ] 2>/dev/null; then
  say "  [OK] already funded — nothing to send."
elif [ -n "$VPS" ]; then
  [ -n "${SSHPASS:-}" ] || die "--vps given but SSHPASS is not exported."
  command -v sshpass >/dev/null || die "install sshpass: sudo apt install -y sshpass"
  if [ "$YES" = 1 ]; then
    # PRE-FLIGHT the operator pocket. A broadcast returning "code":0 only means the tx entered the
    # MEMPOOL — it can still fail at execution, and insufficient funds is the usual reason: a transfer
    # can report success and never land. Reading the source balance
    # first turns "the transfer silently did nothing" into "bob has X, you need Y".
    SRC_BAL="$(sshpass -e ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$VPS" \
      "cd ~/dendra && A=\$(docker compose exec -T chain dendrad keys show $FUND_FROM -a --keyring-backend test 2>/dev/null | tr -d '\r\n ') && docker compose exec -T chain dendrad query bank balances \$A -o json --node tcp://localhost:26657 2>/dev/null" \
      2>/dev/null | python3 -c '
import json,sys
try: b=json.load(sys.stdin).get("balances",[])
except Exception: b=[]
print(next((c["amount"] for c in b if c.get("denom")=="udndr"),"0"))' 2>/dev/null)"
    case "${SRC_BAL:-}" in ''|*[!0-9]*) SRC_BAL=0;; esac
    say "  operator pocket '$FUND_FROM' holds $SRC_BAL udndr (needs $NEED)"
    if [ "$SRC_BAL" -lt "$NEED" ] 2>/dev/null; then
      die "'$FUND_FROM' cannot cover $NEED udndr. Either pick a pocket that can (DENDRA_FUND_FROM=<key>),
       or lower the bond with --amount — but keep it above half the incumbent's stake, otherwise the
       biggest validator stays over 2/3 and the others stop counting toward the committee seed."
    fi
    say "  sending $NEED udndr from the operator pocket '$FUND_FROM' on $VPS ..."
    sshpass -e ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$VPS" \
      "cd ~/dendra && docker compose exec -T chain dendrad tx bank send $FUND_FROM $ADDR ${NEED}udndr --keyring-backend test --chain-id $CHAIN_ID --node tcp://localhost:26657 --gas-prices 0udndr --yes -o json" \
      2>/dev/null | broadcast_accepted || die "funding tx refused (operator pocket '$FUND_FROM' empty?)"
    # Poll until the balance ACTUALLY rises, and say so only if it did. Announcing success straight off
    # the broadcast prints "[OK] funded (balance=)" with an empty value whenever the parse fails — a
    # failure reported as a success. A claim with a blank number in it is not a claim.
    OKFUND=0
    for _ in $(seq 1 20); do
      BAL="$(nx dendrad query bank balances "$ADDR" -o json --node $NODE_LOCAL | python3 -c '
import json,sys
try: b=json.load(sys.stdin).get("balances",[])
except Exception: b=[]
print(next((c["amount"] for c in b if c.get("denom")=="udndr"),"0"))' 2>/dev/null)"
      case "${BAL:-}" in ''|*[!0-9]*) BAL=0;; esac
      [ "$BAL" -ge "$NEED" ] 2>/dev/null && { OKFUND=1; break; }
      sleep 3
    done
    [ "$OKFUND" = 1 ] || die "funding tx was accepted but the balance never reached $NEED (read: $BAL) — check the operator pocket and the node's view of the chain."
    say "  [OK] funded (balance=$BAL udndr)"
  else
    say "  (dry run) would send $NEED udndr from '$FUND_FROM' on $VPS to $ADDR"
  fi
else
  # ⛔ THE REMEDIES BELOW ARE ORDERED BY WHO CAN ACTUALLY RUN THEM.
  # This branch is reached by exactly the person who does NOT hold root on someone else's box: an
  # outside operator bonding their own validator. A remedy opening with `sshpass -e ssh root@<VPS>`
  # is a command they cannot run, and it reads as "you are not supposed to be here". So the joiner's
  # self-service path comes first; the operator bootstrap stays, named as such.
  say "  NOT funded yet, and no --vps given. Two ways forward, depending on who you are:"
  say ""
  say "  · JOINING FROM OUTSIDE — use the public faucet, then bond what you actually have:"
  say "      the faucet hands out 10 DNDR per address per 24h (proof-of-work gated); the wallet page"
  say "      prints the exact call for $ADDR. Bonding needs at least 1 DNDR for non-zero voting power."
  say "      Then re-run with an amount you can cover, acknowledging the incumbent's share:"
  say "        bash deploy/bond_validator.sh --amount 5000000 --accept-supermajority --yes-bond"
  say "      (5000000udndr = 5 DNDR. See --help for what --accept-supermajority costs and why it is"
  say "       required here: read the largest validator's share above -- it is ${PCT}% of the bonded"
  say "       total as measured by THIS run, not a figure written into this script.)"
  say ""
  say "  · OPERATOR BOOTSTRAP ONLY — if you hold root on the genesis node, fund from its pocket:"
  say "      sshpass -e ssh root@<VPS_IP> \"cd ~/dendra && docker compose exec -T chain dendrad tx bank send $FUND_FROM $ADDR ${NEED}udndr --keyring-backend test --chain-id $CHAIN_ID --node tcp://localhost:26657 --gas-prices 0udndr --yes\""
  # ⛔ AND IT REFUSES, INSTEAD OF FALLING THROUGH. This branch printed two remedies and then let the
  # rest run: with --yes-bond, an account with a zero balance went straight to create-validator. The
  # transaction then enters the mempool (zero fees, the ante-handler passes) and fails at EXECUTION,
  # where the script was not looking. A printed remedy is not an applied remedy.
  die "not funded: bond REFUSED. Apply one of the two remedies above, then re-run."
fi

if [ "$YES" != 1 ]; then
  say ""
  say "=============================================================================="
  say "  DRY RUN — no transaction was broadcast, no stake bonded, no funds moved."
  say "  (The only thing written: the local operator key, if it did not exist yet.)"
  say "  Bonding LOCKS UP STAKE, so it needs an explicit flag."
  say "  Plan: bond $BOND udndr from $ADDR  ->  largest validator ${PCT}% of total"
  say "  Then: anchor the VRF key, restart the node with it, verify the seed contributors."
  say "  Run it for real:   bash deploy/bond_validator.sh${VPS:+ --vps $VPS} --yes-bond"
  say "=============================================================================="
  exit 0
fi

say "== [bond] 5) create-validator (idempotent) =="
if [ "$ALREADY" = 1 ]; then
  say "  [OK] already a validator ($VALOPER) — skipping the bond."
else
  # ⛔ THE MONIKER BELOW IS CONSENSUS STATE, AND CONSENSUS STATE IS FOREVER.
  # It lands in the staking description: served to anyone by the public REST endpoint and written into
  # a block that no edit can rewrite. Deriving it from `hostname` would publish, for a box named
  # DESKTOP-XXXX, `moniker="dendra-DESKTOP-XXXX"` — a breach of invariant ⑦ (identifiers are
  # PSEUDONYMOUS) that no later correction can take back. Of the three identity doors (miner identity,
  # join.sh's node moniker, this one), this is the ONLY one whose value is consensus state rather than
  # a local file or a gossip field, so it is the one where the leak is unrecoverable. Hence the hash
  # below, and the same derivation as join.sh's node moniker so both doors publish one pseudonym.
  # ⚠️ The example above is a PLACEHOLDER, never a real machine name: a comment that explains a leak
  # is a published surface like any other, and spelling out an actual hostname here would republish
  # exactly what the line below exists to suppress.
  MON="${MONIKER:-dendra-$( { cat /etc/machine-id 2>/dev/null || hostname 2>/dev/null || echo val; } | tr -d '\n' | head -c 64 | sha256sum | cut -c1-8)}"
  # SDK 0.50+ removed the --amount/--pubkey flags: create-validator now takes a JSON file, so the
  # pubkey is read inside the container and the descriptor is written there before the send.
  BR="$($DC exec -T node sh -c "PK=\$(dendrad tendermint show-validator --home $HOME_DIR); printf '{\"pubkey\": %s, \"amount\": \"${BOND}udndr\", \"moniker\": \"$MON\", \"commission-rate\": \"0.10\", \"commission-max-rate\": \"0.20\", \"commission-max-change-rate\": \"0.01\", \"min-self-delegation\": \"1\"}' \"\$PK\" > /tmp/validator.json; dendrad tx staking create-validator /tmp/validator.json --from $VAL_KEY $KB --chain-id $CHAIN_ID --node $NODE_LOCAL --gas auto --gas-adjustment 1.3 --gas-prices 0udndr --yes -o json" </dev/null 2>/dev/null)"
  # CONFIRM EXECUTION, NOT ACCEPTANCE -- the pattern from `deploy/testnet/join_validator.sh`. A
  # `"code":0` on BROADCAST only says the transaction entered the mempool; with zero fees the
  # ante-handler passes and it can still fail at execution (insufficient funds, out of gas). Only
  # `query tx <hash>` gives the execution result, and nothing below may run without it.
  confirm_execution "$BR" "create-validator" || die "the bond is NOT effective — nothing else in this script may proceed"
  say "  [OK] bond EXECUTED ($BOND udndr, moniker=$MON)"
  sleep 8
fi

say "== [bond] 6) anchor the VRF key (without it this validator does NOT feed the committee seed) =="
tr -d '\r' < "$REPO/deploy/testnet/anchor_vrf_key.sh" | $DC exec -T \
  -e HOME_DIR="$HOME_DIR" -e CHAIN_ID="$CHAIN_ID" -e NODE="$NODE_LOCAL" -e VAL_KEY="$VAL_KEY" \
  node bash -s || die "VRF anchoring failed — see the output above"

say "== [bond] 7) restart the node WITH the VRF key (dendrad reads it at boot) =="
# The key is useless until dendrad is told where it lives: without this the anchoring is on-chain but the
# node never signs a vote extension with it, and `contributors` stays flat while everything "looks" fine.
touch "$KIT/.env"
grep -v '^DENDRA_VRF_KEY_FILE=' "$KIT/.env" > "$KIT/.env.tmp" 2>/dev/null || true
printf 'DENDRA_VRF_KEY_FILE=%s/config/vrf_key\n' "$HOME_DIR" >> "$KIT/.env.tmp"
mv "$KIT/.env.tmp" "$KIT/.env"
# --force-recreate is INDISPENSABLE. `docker compose up -d` recreates a container only when the
# CONFIGURATION has changed. But join.sh preserves DENDRA_VRF_KEY_FILE from a previous install, so the
# line just written is IDENTICAL to the old one, compose sees no change, and the node keeps running
# without ever having read its key. That is exactly the failure described two lines above - valid
# on-chain anchoring, zero signed vote extensions, a contributors count that never moves - and nothing
# on screen reports it: the step prints "OK".
# ⛔ BUT A RECREATE ALSO REPOINTS THE CONTAINER AT WHATEVER ITS IMAGE TAG NOW RESOLVES TO, and a tag
# is mutable: any local `docker build` of this Dockerfile — a second node tried out, a rebuild from a
# different checkout — moves `dendra/node:latest` under a container that is happily running the old
# image. Recreating then swaps the CONSENSUS BINARY of a node that is producing blocks on a live
# chain. A binary that computes state differently produces a different app hash, and the node forks
# or halts. That failure looks like the network breaking, not like a bond.
# So the swap is REFUSED, not warned about: at this point the operator is bonding, not upgrading.
_img_run="$(docker inspect "$($DC ps -q node 2>/dev/null | head -1)" --format '{{.Image}}' 2>/dev/null || true)"
_img_tag="$(docker image inspect "${DENDRA_NODE_IMAGE:-dendra/node:latest}" --format '{{.Id}}' 2>/dev/null || true)"
if [ -n "$_img_run" ] && [ -n "$_img_tag" ] && [ "$_img_run" != "$_img_tag" ]; then
  die "the image tag moved under this node: it RUNS ${_img_run%%:*}...${_img_run: -12}, the tag now points at ${_img_tag%%:*}...${_img_tag: -12}.
   Recreating here would restart consensus on a DIFFERENT binary and can fork this node off the chain.
   Bond and upgrade are two separate gestures: settle the image first (rebuild the running one, or
   pin DENDRA_NODE_IMAGE in $KIT/.env to the digest this node is on), then run this script again."
fi
# A missing tag is NOT a reason to continue quietly: compose would pull or build something unseen.
[ -n "$_img_tag" ] || die "image ${DENDRA_NODE_IMAGE:-dendra/node:latest} is not present locally; refusing to let a recreate resolve it on its own."
( cd "$KIT" && docker compose up -d --force-recreate ) >/dev/null 2>&1 || die "node restart failed"
# And PROVE it rather than hope: the container must have started AFTER the key was written.
_ts_key="$($DC exec -T node sh -c "stat -c %Y $HOME_DIR/config/vrf_key 2>/dev/null" </dev/null 2>/dev/null | tr -d '\r')"
_ts_up="$(docker inspect "$($DC ps -q node 2>/dev/null | head -1)" --format '{{.State.StartedAt}}' 2>/dev/null)"
say "  key written (epoch) : ${_ts_key:-?}   container started : ${_ts_up:-?}"
sleep 12

say "== [bond] 8) verification (the only thing that counts) =="
nx dendrad query staking validators -o json --node $NODE_LOCAL | python3 -c '
import json,sys
try: vs=json.load(sys.stdin).get("validators",[])
except Exception: vs=[]
tot=sum(int(v.get("tokens","0")) for v in vs) or 1
print("  validators: %d" % len(vs))
for v in vs:
    t=int(v.get("tokens","0"))
    print("    %-22s %14s udndr  %5.1f%%  %s" % (v.get("description",{}).get("moniker","?"), t, 100*t/tot, v.get("status","")))
top=max((int(v.get("tokens","0")) for v in vs), default=0)
print("  %s largest holds %.1f%% (must stay < 66.7%%)" % ("[OK]" if 3*top < 2*tot else "[!!]", 100*top/tot))'
# The field is latest_contributors, not "contributors" — grepping the wrong name prints "unreadable" on
# a perfectly readable answer. Parse the JSON instead of guessing at its spelling.
nx dendrad query jobs committee-seed-health -o json --node $NODE_LOCAL | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("  seed health: unreadable (RPC?)"); raise SystemExit
g=lambda *k: next((d[x] for x in k if x in d), None)
c   = g("latest_contributors","latestContributors")
mn  = g("committee_min_vrf_contributors","committeeMinVrfContributors")
rec = g("has_recent_seed","hasRecentSeed")
print("  seed: contributors=%s  required>=%s  recent_seed=%s" % (c, mn, rec))
# THREE ANSWERS, NEVER TWO. `latest_contributors` is omitted by proto3 BOTH when it is zero and when
# no seed exists at all, so `int(None)` raised and the old `except Exception: pass` swallowed the only
# decision this block makes -- the reassuring reading, in exactly the case that is not reassuring.
# Absent count -> 0 (a dead seed IS zero contributors, and that is an alert).
# Absent floor  -> UNKNOWN: a security criterion never defaults, so it reports a failed measurement.
if mn is None:
    print("  [!] committee_min_vrf_contributors is ABSENT from the answer: the floor is UNKNOWN, so")
    print("      nothing here proves the seed is decentralised. Check the node and re-run.")
elif int(c or 0) >= int(mn):
    print("  [OK] the decentralized seed has enough contributors.")
else:
    print("  [!] not enough contributors yet -- the committee seed is still centralised.")'
say ""
say "  If contributors has not moved, give it a few blocks: it only counts once this validator has"
say "  actually SIGNED a vote extension with the anchored key. If it stays flat, the anchoring is the"
say "  thing to re-check (deploy/testnet/anchor_vrf_key.sh fails loudly rather than printing OK)."
