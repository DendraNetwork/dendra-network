#!/usr/bin/env bash
# join_validator.sh — attaches THIS machine as VALIDATOR #2 on the testnet hosted by a remote node, and
# anchors its VRF key. Purpose: prove the DECENTRALISED VRF in a DISTRIBUTED setting (two operators on two
# separate machines), which moves the "VRF - anti-grinding" panel from red to green once the contributor
# count reaches the committee_min_vrf_contributors floor.
#
# Requires: ~/dendra (the source tree), Go, python3, and the remote node online. No local Docker (plain dendrad).
# Usage:  tr -d '\r' < deploy/testnet/join_validator.sh | bash -s -- <REMOTE_IP> [--bond <udndr>] [--fresh]
#   (from the root of a clone: the faucet's proof of work is solved by the clone's own faucet.py)
# THE BOND: --bond <udndr>, or by default ONE drip of the remote faucet, read from its status. See "funding" below.
set -uo pipefail
# OPTIONS ARE PARSED, NOT GUESSED FROM A POSITION.
# `$1` is the REMOTE IP. Testing `$1` for `--fresh` gave that flag no usable position at all: written
# first it became the host (every request went to `http://--fresh:26657`) AND matched the destroy
# branch, so the operator key was erased and the run then failed; written second it was silently
# ignored, so an operator who asked to start over kept the old home and never knew.
# An unknown argument is REFUSED rather than absorbed: this script erases a validator identity.
FRESH=0
VPS=""
BOND_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fresh) FRESH=1 ;;
    # A positive whole number of udndr, written without a leading zero: the bond is never parsed from anything else.
    --bond)  BOND_ARG="${2:-}"
             case "$BOND_ARG" in ""|0*|*[!0-9]*) echo "[join-val] FATAL: --bond needs a positive whole number of udndr (e.g. 1000000)"; exit 2 ;; esac
             shift ;;
    -*)      echo "[join-val] FATAL: unknown option: $1"; echo "  usage: bash join_validator.sh <REMOTE_IP> [--bond <udndr>] [--fresh]"; exit 2 ;;
    *)       [ -z "$VPS" ] || { echo "[join-val] FATAL: two addresses given ($VPS, $1)"; exit 2; }
             VPS="$1" ;;
  esac
  shift
done
[ -n "$VPS" ] || { echo "[join-val] FATAL: missing <REMOTE_IP>"; echo "  usage: bash join_validator.sh <REMOTE_IP> [--bond <udndr>] [--fresh]"; exit 2; }
export PATH="$PATH:$HOME/go/bin:/usr/local/go/bin"
D="$HOME/go/bin/dendrad"; VRF="$HOME/go/bin/dendra-vrf"
H="$HOME/.dendra-join"; CHAIN=dendra
KB="--keyring-backend test --home $H"
RPC="http://$VPS:26657"; FAUCET="http://$VPS:4500"
NLOCAL="tcp://127.0.0.1:36657"        # for dendrad --node (tcp:// scheme)
NLOCAL_HTTP="http://127.0.0.1:36657"  # for curl (http:// scheme — /status is plain HTTP)
BOND=""                          # the bond of validator #2, set by fund_validator below (udndr)
LOG="/tmp/dendra-join.log"
step(){ echo; echo "######## $* ########"; }
die(){ echo "  FAILED: $*"; [ -f "$LOG" ] && tail -15 "$LOG"; exit 1; }

# ---------------------------------------------------------------- funding: the public faucet, or by hand
# THE BOND IS WHAT THE ACCOUNT CAN PAY, AND THE ACCOUNT IS FUNDED BY ONE DRIP OR BY ITS OPERATOR -- SAID, NEVER
# ASSUMED. A public faucet grants ONE drip per address per day, of the amount its status names (`amount`), asks a
# proof of work (`pow_bits`), and counts drips per IP and per day (faucet.py::_rate_ok): a POST without a
# proof is refused, and a bond above what the account holds makes create-validator fail at execution.
#   - the bond is --bond <udndr> when given, else ONE drip of this faucet, read from its status, never guessed;
#   - a bond below one unit of consensus power is refused before anything is asked (POWER_REDUCTION_UDNDR below);
#   - pcval's balance is READ first, and a drip is asked only below the bond, its proof of work solved by the
#     faucet's OWN solver (faucet.solve_pow: a second copy drifts from the verifier, and the faucet then
#     refuses every proof while both sides look correct);
#   - every refusal is named -- cooldown, IP quota, proof, unknown amount, unreachable -- with the way out: fund
#     the address by hand, or wait. Nothing from create-validator on runs on an account that cannot pay the bond.
POW_MAX_S="${DENDRA_FAUCET_POW_MAX_S:-600}"
# ONE UNIT OF CONSENSUS POWER, in udndr. The staking module turns a bond into consensus power by dividing it by the
# power reduction -- the SDK's sdk.DefaultPowerReduction, which this chain's code does not override -- and its
# end-of-block update (the staking keeper's ApplyAndReturnValidatorSetUpdates) stops at the first validator whose
# power is 0: below this, create-validator EXECUTES and the validator is never bonded. This value only refuses a
# bond early; bond_check below reads the status the chain gives, which is what decides.
POWER_REDUCTION_UDNDR=1000000

# faucet_status -> "<amount udndr>|<pow_bits>" from GET $FAUCET, or NOTHING when the status is not read whole: an
# amount that is not a positive whole number of udndr, or a proof difficulty that is not a whole number, is unread.
faucet_status(){
  curl -s -m 15 "$FAUCET/" 2>/dev/null | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
if not isinstance(d, dict): sys.exit(0)
a, b = d.get("amount"), d.get("pow_bits")
if not isinstance(a, str) or not a.endswith("udndr") or not a[:-5].isdigit() or int(a[:-5]) <= 0: sys.exit(0)
if isinstance(b, bool) or not isinstance(b, int) or b < 0: sys.exit(0)
print("%d|%d" % (int(a[:-5]), b))' 2>/dev/null
}

# faucet_services_dir -> the directory holding faucet.py in this clone (services in the development
# tree, services in the published one), found from this file's place or from the directory it is run in.
faucet_services_dir(){
  local r s
  for r in "$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" "$PWD"; do
    for s in services services; do
      [ -n "$r" ] && [ -f "$r/$s/faucet.py" ] && { printf '%s' "$r/$s"; return 0; }
    done
  done
  return 1
}

# balance_of <address> -> the udndr balance, a whole number, or NOTHING when the node did not answer. proto3 omits
# an empty list: an answer without `balances` is an account holding nothing, a zero that was read.
balance_of(){
  "$D" query bank balances "$1" --node "$NLOCAL" -o json 2>/dev/null | python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
bs = d.get("balances", []) if isinstance(d, dict) else None
if not isinstance(bs, list): sys.exit(0)
print(sum(int(c.get("amount", 0) or 0) for c in bs if isinstance(c, dict) and c.get("denom") == "udndr"))' 2>/dev/null
}

# faucet_drip <address> <pow_bits> -> DRIP_CODE (HTTP status, 000 when nothing answered) and DRIP_INFO (what the
# faucet said, or why nothing was asked). The proof is solved first, bounded by POW_MAX_S.
faucet_drip(){
  local addr="$1" bits="$2" svc nonce="" out prc
  DRIP_CODE=""; DRIP_INFO=""
  if [ "$bits" -gt 0 ]; then
    svc="$(faucet_services_dir)" || { DRIP_INFO="the faucet asks a proof of work ($bits bits) and this clone carries no faucet.py to solve it (run this script from the root of a clone)"; return 0; }
    echo "  solving the faucet's proof of work ($bits bits, at most ${POW_MAX_S} s)..."
    # Two failures, two causes: the solver that does not run (an import or a crash: python3's own exit code) is not
    # a proof that took too long (the solver ran and returned no nonce within the bound).
    out="$(mktemp)" || { DRIP_INFO="no temporary file for the solver's errors"; return 0; }
    nonce="$(python3 -I -c 'import sys
sys.path.insert(0, sys.argv[1])
import faucet as f
print(f.solve_pow(sys.argv[2], int(sys.argv[3]), deadline_s=float(sys.argv[4])))' "$svc" "$addr" "$bits" "$POW_MAX_S" 2>"$out")"
    prc=$?
    if [ "$prc" != 0 ]; then
      DRIP_INFO="the faucet's solver did not run from $svc (python3 exit $prc: $(tail -1 "$out" | cut -c1-200))"; rm -f "$out"; return 0
    fi
    rm -f "$out"
    [ -n "$nonce" ] || { DRIP_INFO="the proof of work was not solved within ${POW_MAX_S} s (DENDRA_FAUCET_POW_MAX_S sets the bound)"; return 0; }
  fi
  out="$(mktemp)" || { DRIP_INFO="no temporary file for the faucet's answer"; return 0; }
  DRIP_CODE="$(curl -s -m 300 -o "$out" -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
               -d "{\"address\":\"$addr\"${nonce:+,\"pow\":\"$nonce\"}}" "$FAUCET" 2>/dev/null)"
  DRIP_INFO="$(python3 -c '
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
if isinstance(d, dict): print(" ".join(str(d.get(k, "")) for k in ("error", "info") if d.get(k))[:300])' "$out" 2>/dev/null)"
  rm -f "$out"
  case "$DRIP_CODE" in ""|*[!0-9]*) DRIP_CODE=000 ;; esac
}

# fund_validator -> BOND (e.g. 1000000udndr) once pcval ($A) holds it, or exit 1 with what to do.
fund_validator(){
  local st drip="" bits="" want bal i
  st="$(faucet_status)"
  if [ -n "$st" ]; then drip="${st%%|*}"; bits="${st#*|}"; fi
  if [ -n "$BOND_ARG" ]; then want="$BOND_ARG"
  elif [ -n "$drip" ]; then want="$drip"
  else
    echo "  NOT FUNDED: the faucet's status ($FAUCET) gives no drip amount, and no --bond was given: the bond is never guessed."
    echo "     Fund $A yourself, then re-run with --bond <udndr> (at most what it holds)."
    exit 1
  fi
  # A BOND BELOW ONE UNIT OF CONSENSUS POWER BUYS NO VOTE: refused here, before a drip is asked or a stake is locked.
  # (`2>/dev/null`: a number too long for the test is far above the floor, and the test then fails -- not refused.)
  if [ "$want" -lt "$POWER_REDUCTION_UDNDR" ] 2>/dev/null; then
    echo "  REFUSED: a bond of $want udndr is below one unit of consensus power ($POWER_REDUCTION_UDNDR udndr): the validator would be created, hold its stake and never vote."
    if [ -n "$BOND_ARG" ]; then echo "     Re-run with --bond $POWER_REDUCTION_UDNDR or more (at most what $A holds)."
    else echo "     One drip of this faucet is $want udndr. Send at least $POWER_REDUCTION_UDNDR udndr to $A yourself, then re-run with --bond <udndr>."; fi
    exit 1
  fi
  bal="$(balance_of "$A")"
  if [ -n "$bal" ] && [ "$bal" -ge "$want" ]; then
    echo "  pcval holds $bal udndr, the bond is $want udndr: no drip asked."
    BOND="${want}udndr"; return 0
  fi
  if [ -z "$drip" ]; then
    echo "  NOT FUNDED: pcval holds ${bal:-an amount the node did not report} udndr, below the bond ($want udndr), and the faucet ($FAUCET) gives no readable status."
    echo "     Send at least $want udndr to $A yourself, then re-run (every step is safe to replay)."
    exit 1
  fi
  echo "  pcval holds ${bal:-an unread amount of} udndr, below the bond ($want udndr): asking ONE drip of $drip udndr from $FAUCET."
  faucet_drip "$A" "$bits"
  case "$DRIP_CODE" in
    200) : ;;
    429) echo "  NOT FUNDED: the faucet refused (HTTP 429: ${DRIP_INFO:-no reason given}). It pays one drip per address per day, and a"
         echo "     limited number per IP and per day. Send at least $want udndr to $A yourself, or wait, then re-run."
         exit 1 ;;
    "") echo "  NOT FUNDED: no drip was asked: $DRIP_INFO."
        echo "     Send at least $want udndr to $A yourself, then re-run."
        exit 1 ;;
    000) echo "  NOT FUNDED: the faucet ($FAUCET) did not answer the request."
         echo "     Send at least $want udndr to $A yourself, or try again later, then re-run."
         exit 1 ;;
    *)  echo "  NOT FUNDED: the faucet answered HTTP $DRIP_CODE${DRIP_INFO:+ ($DRIP_INFO)}."
        echo "     Send at least $want udndr to $A yourself, then re-run."
        exit 1 ;;
  esac
  for i in 1 2 3 4 5 6; do
    bal="$(balance_of "$A")"
    [ -n "$bal" ] && [ "$bal" -ge "$want" ] && break
    sleep 5
  done
  if [ -n "$bal" ] && [ "$bal" -ge "$want" ]; then
    echo "  funded: pcval holds $bal udndr (one drip: $drip udndr)."
    BOND="${want}udndr"; return 0
  fi
  echo "  NOT FUNDED: after the drip pcval holds ${bal:-an amount the node did not report} udndr, below the bond ($want udndr)."
  echo "     One drip of this faucet is $drip udndr: send the rest to $A yourself, or re-run with --bond <udndr> at most what it holds."
  exit 1
}
# -- end of the funding functions

# ---------------------------------------------------------------- the bond: executed is not bonded
# CREATE-VALIDATOR EXECUTED SAYS THE VALIDATOR EXISTS, NOT THAT IT VOTES. The staking module bonds a validator at
# the end of a block only when its power is above 0 and the active set has room for it (staking param
# max_validators); otherwise the validator stays UNBONDED with its stake. And the VRF anchoring below executes all
# the same, since it asks for no bonded status (msg_server_register_validator_vrf.go::RegisterValidatorVrfKey).
# So the bond counts once the chain reports BOND_STATUS_BONDED, read after the transaction -- never on the
# transaction's code alone.
# validator_status <valoper> -> the status the node reports (BOND_STATUS_...), or NOTHING when it did not answer.
# ZERO RULE: an answer that names the validator and omits `status` carries its zero, BOND_STATUS_UNSPECIFIED --
# a status that was read, and not a bonded one.
validator_status(){
  "$D" query staking validator "$1" --node "$NLOCAL" -o json 2>/dev/null | python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
v = d.get("validator", d) if isinstance(d, dict) else None
if not isinstance(v, dict) or not v.get("operator_address"): sys.exit(0)
s = v.get("status", 0)
names = {0: "BOND_STATUS_UNSPECIFIED", 1: "BOND_STATUS_UNBONDED", 2: "BOND_STATUS_UNBONDING", 3: "BOND_STATUS_BONDED"}
if isinstance(s, int) and not isinstance(s, bool): s = names.get(s, "")
if isinstance(s, str) and s.startswith("BOND_STATUS_"): print(s)' 2>/dev/null
}
# bond_check <executed: 1|0> -> BOND_OK=1 only when create-validator executed AND the chain reports pcval's validator
# BONDED; otherwise BOND_OK=0 and the reason, said.
bond_check(){
  local valoper st="" i
  BOND_OK=0
  [ "$1" = 1 ] || return 0
  valoper="$("$D" keys show pcval --bech val -a $KB 2>/dev/null)"
  if [ -z "$valoper" ]; then
    echo "  [ERR] the operator address of pcval could not be read: its validator's status is not read, so it is NOT counted as bonded."
    return 0
  fi
  for i in 1 2 3 4 5 6; do
    st="$(validator_status "$valoper")"
    [ "$st" = BOND_STATUS_BONDED ] && break
    sleep 5
  done
  case "$st" in
    BOND_STATUS_BONDED) BOND_OK=1; echo "  [OK] $valoper is BONDED: it is in the active set." ;;
    "") echo "  [ERR] create-validator executed, but the node did not report the status of $valoper: NOT counted as bonded." ;;
    *)  echo "  [ERR] create-validator executed, and the chain reports $valoper $st: it holds its stake and does not vote."
        echo "     The chain bonds a validator whose power is above 0 (a bond of at least $POWER_REDUCTION_UDNDR udndr) when the active set has room for it (staking param max_validators)." ;;
  esac
}
# -- end of the bond check

step "0) fresh binaries (dendrad + dendra-vrf) from ~/dendra"
[ -d "$HOME/dendra" ] || die "~/dendra not found"
( cd "$HOME/dendra" && go build -o "$D" ./cmd/dendrad && go build -o "$VRF" ./cmd/dendra-vrf ) || die "go build"

step "1) init + remote genesis + persistent peer"
pkill -f "dendrad start --home $H" 2>/dev/null && sleep 2
# THIS HOME IS THE ONLY COPY OF THE OPERATOR KEY, so it is never destroyed implicitly.
# An unconditional `rm -rf "$H"` means every re-run — resuming after an interruption, replaying a
# step — wipes the keyring that holds the bonded stake, with no confirmation and no backup. A lost
# validator identity is not recoverable: the stake stays on-chain under a key nobody holds any more.
# Init therefore runs only when the home is absent, and erasing it requires an explicit `--fresh`.
if [ "$FRESH" = "1" ]; then
  # THE KEY IS COPIED OUT BEFORE IT IS DESTROYED, because the comment above is right: a lost validator
  # identity is not recoverable, and an undelegation is a transaction SIGNED BY THAT KEY. A flag is a
  # keystroke; the stake it can strand is not. The backup is silent about its contents -- it lands
  # beside the operator, and nothing is printed to the screen.
  if [ -d "$H" ]; then
    _BK="$HOME/dendra-join-keys-$(date -u +%Y%m%dT%H%M%SZ).tgz"
    if tar czf "$_BK" -C "$H" config/priv_validator_key.json config/node_key.json config/vrf_key keyring-test 2>/dev/null; then
      echo "[join-val] --fresh: keys copied to $_BK BEFORE destroying $H"
    else
      echo "[join-val] --fresh: nothing to back up in $H (no key found) -- continuing"
    fi
  fi
  echo "[join-val] --fresh: destroying $H (operator key included)"
  rm -rf "$H"
elif [ -f "$H/config/genesis.json" ]; then
  echo "[join-val] $H already exists -> init SKIPPED (safe re-run). Use --fresh to start over."
fi
[ -f "$H/config/genesis.json" ] || "$D" init pc-val2 --chain-id "$CHAIN" --home "$H" >/dev/null 2>&1 || die "init"
curl -s "$RPC/genesis" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin)["result"]["genesis"]))' > "$H/config/genesis.json" || die "genesis"
[ -s "$H/config/genesis.json" ] || die "empty genesis (remote RPC unreachable?)"
NODEID=$(curl -s "$RPC/status" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["node_info"]["id"])') || die "node-id"
echo "  peer = $NODEID@$VPS:26656"
C="$H/config/config.toml"
sed -i "s|^persistent_peers = .*|persistent_peers = \"$NODEID@$VPS:26656\"|" "$C"
sed -i 's|^laddr = "tcp://127.0.0.1:26657"|laddr = "tcp://0.0.0.0:36657"|' "$C"
sed -i 's|^laddr = "tcp://0.0.0.0:26656"|laddr = "tcp://0.0.0.0:36656"|' "$C"
# A JOINING NODE MUST ADOPT THE CADENCE OF THE NETWORK IT JOINS. The mismatch is not symmetric, which
# is what makes it easy to miss: a node whose timeout_commit is SHORTER than its peers' opens its
# propose step earlier and stops waiting before they have built anything, so it prevotes nil on every
# block they propose. Carrying real voting power, such a node costs the network a full round on every
# turn of every slower validator -- while every health indicator stays green, because it signs
# normally, is never jailed, and the mean block time can even fall.
# This value is the live network's. Sites that write a different one target throwaway devnets and say
# so; the agreement between them is derived, not trusted.
# CADENCE: vivant
sed -i 's/^timeout_commit = .*/timeout_commit = "5s"/' "$C"
sed -i 's/^addr_book_strict = .*/addr_book_strict = false/' "$C"

step "2) validator key (pcval) + VRF key (written into the node home)"
"$D" keys add pcval $KB >/dev/null 2>&1 || true
A=$("$D" keys show pcval -a $KB) || die "pcval key"
echo "  pcval = $A"
read -r VSK VPK < <("$VRF" keygen); echo "$VSK" > "$H/config/vrf_key"; chmod 600 "$H/config/vrf_key"
echo "  VRF key generated (pub ${VPK:0:16}...)"

step "3) start the local node (syncing from the remote peer) WITH the VRF key"
DENDRA_VRF_KEY_FILE="$H/config/vrf_key" nohup "$D" start --home "$H" --minimum-gas-prices 0udndr > "$LOG" 2>&1 &
echo "  started (log: $LOG). Waiting for sync (catching_up:false)..."
ok=0; for i in $(seq 1 80); do sleep 3; curl -s "$NLOCAL_HTTP/status" 2>/dev/null | grep -q '"catching_up":false' && { ok=1; break; }; [ $((i%5)) -eq 0 ] && echo "  ... syncing ($i/80)"; done
[ "$ok" = 1 ] || die "no sync (P2P blocked? is port 26656 open on the remote node?)"
echo "  SYNCED."

step "4) fund pcval (one drip of the faucet, or by hand) + create-validator"
fund_validator
echo "  bond: $BOND"
PK=$("$D" comet show-validator --home "$H")
cat > "$H/cv.json" <<JSON
{"pubkey":$PK,"amount":"$BOND","moniker":"pc-val2","commission-rate":"0.10","commission-max-rate":"0.20","commission-max-change-rate":"0.01","min-self-delegation":"1"}
JSON
# CONFIRM EXECUTION, not acceptance. A `"code":0` on broadcast only means the transaction ENTERED THE
# MEMPOOL; it can still fail at EXECUTION. Piping the broadcast output to `grep | head` tested nothing,
# and `|| true` swallowed even a binary failure, so an unbonded validator or an unanchored VRF key still
# let the script print its final success while the chain ran one contributor short (anti-grinding
# INACTIVE) or with a phantom validator. Only `query tx <hash>` yields the execution result.
# ZERO RULE: a chain answer is PARSED, never grepped. The proto3 codec OMITS a field at its zero
# value, so on a SUCCESSFUL transaction `code` is ABSENT -- a textual predicate reads that as "no
# answer" and refuses what the chain accepted. `deploy/launch/launch_public.sh` records the cost: a
# gateway seeding that had gone through was reported refused. What proves an answer arrived is
# `txhash`, which is never zero and so never omitted; once it is there, an absent `code` IS a zero.
# Three answers stay apart: executed (0) / refused (non-zero) / no usable answer (empty output).
tx_code() { # reads a broadcast or `query tx` answer on stdin; prints its code, or NOTHING
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
if isinstance(d, dict) and d.get("txhash"): print(d.get("code", 0))
' 2>/dev/null
}

confirm_tx() { # $1 = broadcast JSON output, $2 = label; sets CONFIRMED=1/0
  CONFIRMED=0
  _HASH=$(printf '%s' "$1" | tr -d ' \t' | grep -o '"txhash":"[A-Fa-f0-9]*"' | head -1 | cut -d'"' -f4)
  [ -n "$_HASH" ] || { echo "  [ERR] $2: no txhash -> the transaction was not even broadcast."; return; }
  for _ in $(seq 1 12); do
    sleep 5
    _Q=$("$D" query tx "$_HASH" --node "$NLOCAL" -o json 2>/dev/null)
    _C=$(printf '%s' "$_Q" | tx_code)
    [ -n "$_C" ] && break
  done
  if [ "$_C" = "0" ]; then CONFIRMED=1; echo "  [OK] $2 EXECUTED (tx $_HASH)."
  elif [ -n "$_C" ]; then echo "  [ERR] $2 REJECTED at execution (code=$_C, tx $_HASH)."
  else echo "  [ERR] $2: tx $_HASH never included after 60s."; fi
}

CV_OUT=$("$D" tx staking create-validator "$H/cv.json" --from pcval $KB --chain-id "$CHAIN" --node "$NLOCAL" --fees 0udndr --yes -o json 2>&1)
confirm_tx "$CV_OUT" "create-validator"; bond_check "$CONFIRMED"

step "5) ANCHOR pcval's VRF key on-chain (proof of possession)"
# The secret reaches dendra-vrf through its environment, never its argv: a positional argument is
# readable by every local user in /proc/<pid>/cmdline for as long as the process runs.
POP=$(DENDRA_VRF_SK="$VSK" "$VRF" prove "dendra/vrf-pop/$A")
VRF_OUT=$("$D" tx jobs register-validator-vrf-key "$VPK" "$POP" --from pcval $KB --chain-id "$CHAIN" --node "$NLOCAL" --fees 0udndr --yes -o json 2>&1)
confirm_tx "$VRF_OUT" "VRF key anchoring"; VRF_OK=$CONFIRMED

echo
echo "============================================================"
if [ "${BOND_OK:-0}" = 1 ] && [ "${VRF_OK:-0}" = 1 ]; then
  echo "  Validator #2 BONDED and VRF key anchored — both confirmed on-chain."
  INCOMPLETE=0
else
  INCOMPLETE=1
  echo "  INCOMPLETE — do NOT count this validator:"
  [ "${BOND_OK:-0}" = 1 ] || echo "     - the validator is NOT bonded (create-validator not executed, or not reported BONDED) -> it is not in the active set and does not vote."
  [ "${VRF_OK:-0}" = 1 ]  || echo "     - VRF key NOT anchored -> it does not contribute to the seed, anti-grinding INACTIVE."
  echo "     Diagnose, then re-run: both steps are idempotent."
fi
echo "  Verify from the remote node that the contributor count reaches 2:"
echo "    docker compose exec -T chain dendrad query jobs committee-seed-health -o json"
echo "    docker compose logs chain | grep 'E4 decentralized VRF seed' | tail   # expect contributors=2"
echo "  -> 'VRF - anti-grinding' panel green = DECENTRALISED VRF proven in a distributed setting."
echo "  (Leave this node running. To stop it: pkill -f 'dendrad start --home $H')"
echo "============================================================"
# THE SCRIPT RETURNED 0 IN EXACTLY THE CASE WHERE IT SAYS NOT TO COUNT THIS VALIDATOR.
# `set -uo pipefail` carries no `set -e`, the INCOMPLETE branch ends on an `echo`, and every
# statement after it is an `echo` too — so the status was the last echo's: success. Anything
# reading $? was told the opposite of the sentence printed a few lines earlier.
[ "${INCOMPLETE:-1}" = 1 ] && exit 1
exit 0
