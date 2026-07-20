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
# Options: --amount <udndr>  force the bonded amount   --moniker <name>   --key <name> (default: validator)
set -uo pipefail

YES=0; VPS=""; AMOUNT=""; MONIKER=""; VAL_KEY="validator"; FUND_FROM="${DENDRA_FUND_FROM:-bob}"
while [ $# -gt 0 ]; do
  case "$1" in
    --yes-bond) YES=1;;
    --vps)      VPS="${2:?--vps needs an IP}"; shift;;
    --amount)   AMOUNT="${2:?--amount needs a value in udndr}"; shift;;
    --moniker)  MONIKER="${2:?--moniker needs a name}"; shift;;
    --key)      VAL_KEY="${2:?--key needs a name}"; shift;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown option: $1 (see --help)"; exit 1;;
  esac; shift
done

_find_repo(){ d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "FAIL: repo root not found (export DENDRA_REPO)"; exit 1; }
KIT="$REPO/deploy/testnet-node"
DC="docker compose -f $KIT/docker-compose.yml"
say(){ printf '%s\n' "$*"; }
die(){ echo "  FAIL: $*"; exit 1; }
# -T: no TTY, so the command is scriptable. </dev/null: `docker exec` otherwise EATS the stdin of a
# piped script and the rest of the file is silently skipped (measured on this project, twice).
nx(){ $DC exec -T node "$@" </dev/null 2>/dev/null; }

CHAIN_ID="$(grep -E '^CHAIN_ID=' "$KIT/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r')"; CHAIN_ID="${CHAIN_ID:-dendra}"
HOME_DIR=/root/.dendra
KB="--keyring-backend test --home $HOME_DIR"
NODE_LOCAL="tcp://localhost:26657"

say "== [bond] 1) node reachable and SYNCED? =="
nx dendrad status >/dev/null || die "node container not answering — run 'deploy/join.sh --validator' first."
CU="$(nx dendrad status | grep -oE '"catching_up": *(true|false)' | grep -oE 'true|false' | head -1)"
[ "$CU" = "false" ] || die "node still catching_up — bonding a lagging validator gets it jailed. Wait for sync."
say "  [OK] synced (catching_up=false), chain-id=$CHAIN_ID"

say "== [bond] 2) operator key (idempotent) =="
ADDR="$(nx dendrad keys show "$VAL_KEY" -a $KB | tr -d '\r\n ')"
if [ -z "$ADDR" ]; then
  nx dendrad keys add "$VAL_KEY" $KB >/dev/null || die "key creation failed"
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
BOND="${AMOUNT:-$SUGG}"
NEWTOP=$(( MAXV > BOND ? MAXV : BOND )); NEWTOT=$(( TOTAL + BOND ))
PCT=$(python3 -c "print(f'{100*$NEWTOP/$NEWTOT:.1f}')" 2>/dev/null || echo "?")
say "  validators today : $NVAL   total bonded: $TOTAL udndr   largest: $MAXV udndr"
say "  proposed bond    : $BOND udndr  ->  largest would hold ${PCT}% of $NEWTOT (must stay < 66.7%)"
python3 -c "import sys; sys.exit(0 if 3*$NEWTOP < 2*$NEWTOT else 1)" \
  || die "this amount would leave a validator at/above 2/3 — raise it with --amount"

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
# This check has to come BEFORE the transfer, not after. In its first version it came after, so a re-run
# on an ALREADY-bonded validator dutifully sent it a second bond's worth of tokens and only then said
# "already a validator — skipping". Idempotent in name only: it skipped the cheap step and repeated the
# expensive one, moving a second bond's worth of tokens for nothing.
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
      2>/dev/null | grep -q '"code":0' || die "funding tx refused (operator pocket '$FUND_FROM' empty?)"
    # Poll until the balance ACTUALLY rises, and say so only if it did. The first version printed
    # "[OK] funded (balance=)" with an empty value — the parse had failed and the script called it a
    # success anyway. A claim with a blank number in it is not a claim.
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
  say "  NOT funded and no --vps given. Fund it from the operator pocket, then re-run:"
  say "    sshpass -e ssh root@<VPS_IP> \"cd ~/dendra && docker compose exec -T chain dendrad tx bank send $FUND_FROM $ADDR ${NEED}udndr --keyring-backend test --chain-id $CHAIN_ID --node tcp://localhost:26657 --gas-prices 0udndr --yes\""
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
  MON="${MONIKER:-dendra-$(hostname 2>/dev/null | cut -c1-16 || echo val2)}"
  # SDK 0.50+ removed the --amount/--pubkey flags: create-validator now takes a JSON file.
  $DC exec -T node sh -c "PK=\$(dendrad tendermint show-validator --home $HOME_DIR); printf '{\"pubkey\": %s, \"amount\": \"${BOND}udndr\", \"moniker\": \"$MON\", \"commission-rate\": \"0.10\", \"commission-max-rate\": \"0.20\", \"commission-max-change-rate\": \"0.01\", \"min-self-delegation\": \"1\"}' \"\$PK\" > /tmp/validator.json; dendrad tx staking create-validator /tmp/validator.json --from $VAL_KEY $KB --chain-id $CHAIN_ID --node $NODE_LOCAL --gas-prices 0udndr --yes -o json" \
    </dev/null 2>/dev/null | grep -q '"code":0' || die "create-validator refused — check the balance and 'docker compose logs node'"
  say "  [OK] bond submitted ($BOND udndr, moniker=$MON)"
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
( cd "$KIT" && docker compose up -d ) >/dev/null 2>&1 || die "node restart failed"
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
# The field is latest_contributors, not "contributors" — grepping the wrong name made the first version
# print "unreadable" on a perfectly readable answer. Parse the JSON instead of guessing at its spelling.
nx dendrad query jobs committee-seed-health -o json --node $NODE_LOCAL | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("  seed health: unreadable (RPC?)"); raise SystemExit
g=lambda *k: next((d[x] for x in k if x in d), None)
c   = g("latest_contributors","latestContributors")
mn  = g("committee_min_vrf_contributors","committeeMinVrfContributors")
rec = g("has_recent_seed","hasRecentSeed")
print("  seed: contributors=%s  required>=%s  recent_seed=%s" % (c, mn, rec))
try:
    if int(c) >= int(mn): print("  [OK] the decentralized seed has enough contributors.")
    else: print("  [!] not enough contributors yet — the committee seed is still centralised.")
except Exception: pass'
say ""
say "  If contributors has not moved, give it a few blocks: it only counts once this validator has"
say "  actually SIGNED a vote extension with the anchored key. If it stays flat, the anchoring is the"
say "  thing to re-check (deploy/testnet/anchor_vrf_key.sh now fails loudly instead of printing OK)."
