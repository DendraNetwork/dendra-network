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
    # overnight. With this flag the opposite is assumed: the incumbent keeps the supermajority, the
    # newcomer contributes ONLY to the VRF seed (the floor counts contributors, not their weight), and
    # its going offline freezes nothing. Use it in a test phase only, and knowing that "nobody produces
    # alone" is NOT acquired — it is already not the case today with a single validator at 100 %.
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
DC="docker compose -f $KIT/docker-compose.yml"
say(){ printf '%s\n' "$*"; }
die(){ echo "  FAIL: $*"; exit 1; }
# -T: no TTY, so the command is scriptable. </dev/null: without it `docker exec` EATS the stdin of a
# piped script and the rest of the file is silently skipped.
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
BOND="${AMOUNT:-$SUGG}"
NEWTOP=$(( MAXV > BOND ? MAXV : BOND )); NEWTOT=$(( TOTAL + BOND ))
PCT=$(python3 -c "print(f'{100*$NEWTOP/$NEWTOT:.1f}')" 2>/dev/null || echo "?")
say "  validators today : $NVAL   total bonded: $TOTAL udndr   largest: $MAXV udndr"
say "  proposed bond    : $BOND udndr  ->  largest would hold ${PCT}% of $NEWTOT (must stay < 66.7%)"
if python3 -c "import sys; sys.exit(0 if 3*$NEWTOP < 2*$NEWTOT else 1)"; then
  say "  [OK] no validator would reach 2/3: nobody would produce blocks alone."
  say "       Accepted trade-off: with two validators, losing EITHER ONE stops the chain."
elif [ "$SUPERMAJ" = "1" ]; then
  say "  [!] SUPERMAJORITY ACCEPTED (--accept-supermajority): the largest would hold ${PCT}% >= 66.7%."
  say "      What you gain: the newcomer contributes to the VRF seed, so anti-grinding activates,"
  say "      and its going offline - sleep, restart, outage - does NOT stop the chain."
  say "      What you lose: the incumbent can produce blocks alone. It ALREADY can today, at 100 %:"
  say "      this flag does not create that property, it only declines to pretend two machines fix"
  say "      something that needs four."
else
  die "this amount would leave a validator at 2/3 or above - raise it with --amount, or take the
     opposite trade-off with --accept-supermajority (read what it costs: --help)"
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
      2>/dev/null | grep -q '"code":0' || die "funding tx refused (operator pocket '$FUND_FROM' empty?)"
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
  say "        bash deploy/bond_validator.sh --amount 5000000udndr --accept-supermajority --yes-bond"
  say "      (5000000udndr = 5 DNDR. See --help for what --accept-supermajority costs and why it is"
  say "       required here: our own validator already holds far more than two thirds.)"
  say ""
  say "  · OPERATOR BOOTSTRAP ONLY — if you hold root on the genesis node, fund from its pocket:"
  say "      sshpass -e ssh root@<VPS_IP> \"cd ~/dendra && docker compose exec -T chain dendrad tx bank send $FUND_FROM $ADDR ${NEED}udndr --keyring-backend test --chain-id $CHAIN_ID --node tcp://localhost:26657 --gas-prices 0udndr --yes\""
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
try:
    if int(c) >= int(mn): print("  [OK] the decentralized seed has enough contributors.")
    else: print("  [!] not enough contributors yet — the committee seed is still centralised.")
except Exception: pass'
say ""
say "  If contributors has not moved, give it a few blocks: it only counts once this validator has"
say "  actually SIGNED a vote extension with the anchored key. If it stays flat, the anchoring is the"
say "  thing to re-check (deploy/testnet/anchor_vrf_key.sh fails loudly rather than printing OK)."
