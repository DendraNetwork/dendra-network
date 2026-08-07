#!/usr/bin/env bash
# join_validator.sh — attaches THIS machine as VALIDATOR #2 on the testnet hosted by a remote node, and
# anchors its VRF key. Purpose: prove the DECENTRALISED VRF in a DISTRIBUTED setting (two operators on two
# separate machines), which moves the "VRF - anti-grinding" panel from red to green once the contributor
# count reaches the committee_min_vrf_contributors floor.
#
# Requires: ~/dendra (the source tree), Go, and the remote node online. No local Docker (plain dendrad).
# Usage:  tr -d '\r' < deploy/testnet/join_validator.sh | bash -s -- <REMOTE_IP>
set -uo pipefail
VPS="${1:?Usage: bash join_validator.sh <REMOTE_IP>}"
export PATH="$PATH:$HOME/go/bin:/usr/local/go/bin"
D="$HOME/go/bin/dendrad"; VRF="$HOME/go/bin/dendra-vrf"
H="$HOME/.dendra-join"; CHAIN=dendra
KB="--keyring-backend test --home $H"
RPC="http://$VPS:26657"; FAUCET="http://$VPS:4500"
NLOCAL="tcp://127.0.0.1:36657"        # for dendrad --node (tcp:// scheme)
NLOCAL_HTTP="http://127.0.0.1:36657"  # for curl (http:// scheme — /status is plain HTTP)
BOND="9000000udndr"              # bond of validator #2 (the faucet hands out 10 DNDR)
LOG="/tmp/dendra-join.log"
step(){ echo; echo "######## $* ########"; }
die(){ echo "  FAILED: $*"; [ -f "$LOG" ] && tail -15 "$LOG"; exit 1; }

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
if [ "${1:-}" = "--fresh" ]; then
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
sed -i 's/^timeout_commit = .*/timeout_commit = "1s"/' "$C"
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

step "4) fund pcval from the faucet + create-validator (bond $BOND)"
curl -s -X POST "$FAUCET" -d "{\"address\":\"$A\"}" >/dev/null; sleep 6
PK=$("$D" comet show-validator --home "$H")
cat > "$H/cv.json" <<JSON
{"pubkey":$PK,"amount":"$BOND","moniker":"pc-val2","commission-rate":"0.10","commission-max-rate":"0.20","commission-max-change-rate":"0.01","min-self-delegation":"1"}
JSON
# CONFIRM EXECUTION, not acceptance. A `"code":0` on broadcast only means the transaction ENTERED THE
# MEMPOOL; it can still fail at EXECUTION. Piping the broadcast output to `grep | head` tested nothing,
# and `|| true` swallowed even a binary failure, so an unbonded validator or an unanchored VRF key still
# let the script print its final success while the chain ran one contributor short (anti-grinding
# INACTIVE) or with a phantom validator. Only `query tx <hash>` yields the execution result.
confirm_tx() { # $1 = broadcast JSON output, $2 = label; sets CONFIRMED=1/0
  CONFIRMED=0
  _HASH=$(printf '%s' "$1" | tr -d ' \t' | grep -o '"txhash":"[A-Fa-f0-9]*"' | head -1 | cut -d'"' -f4)
  [ -n "$_HASH" ] || { echo "  [ERR] $2: no txhash -> the transaction was not even broadcast."; return; }
  for _ in $(seq 1 12); do
    sleep 5
    _Q=$("$D" query tx "$_HASH" --node "$NLOCAL" -o json 2>/dev/null)
    _C=$(printf '%s' "$_Q" | tr -d ' \t' | grep -o '"code":[0-9]*' | head -1 | cut -d: -f2)
    [ -n "$_C" ] && break
  done
  if [ "$_C" = "0" ]; then CONFIRMED=1; echo "  [OK] $2 EXECUTED (tx $_HASH)."
  elif [ -n "$_C" ]; then echo "  [ERR] $2 REJECTED at execution (code=$_C, tx $_HASH)."
  else echo "  [ERR] $2: tx $_HASH never included after 60s."; fi
}

CV_OUT=$("$D" tx staking create-validator "$H/cv.json" --from pcval $KB --chain-id "$CHAIN" --node "$NLOCAL" --fees 0udndr --yes -o json 2>&1)
confirm_tx "$CV_OUT" "create-validator"; BOND_OK=$CONFIRMED

step "5) ANCHOR pcval's VRF key on-chain (proof of possession)"
POP=$("$VRF" prove "$VSK" "dendra/vrf-pop/$A")
VRF_OUT=$("$D" tx jobs register-validator-vrf-key "$VPK" "$POP" --from pcval $KB --chain-id "$CHAIN" --node "$NLOCAL" --fees 0udndr --yes -o json 2>&1)
confirm_tx "$VRF_OUT" "VRF key anchoring"; VRF_OK=$CONFIRMED

echo
echo "============================================================"
if [ "${BOND_OK:-0}" = 1 ] && [ "${VRF_OK:-0}" = 1 ]; then
  echo "  Validator #2 attached and VRF key anchored — BOTH transactions confirmed on-chain."
else
  echo "  INCOMPLETE — do NOT count this validator:"
  [ "${BOND_OK:-0}" = 1 ] || echo "     - create-validator NOT confirmed -> this node is not in the active set."
  [ "${VRF_OK:-0}" = 1 ]  || echo "     - VRF key NOT anchored -> it does not contribute to the seed, anti-grinding INACTIVE."
  echo "     Diagnose, then re-run: both steps are idempotent."
fi
echo "  Verify from the remote node that the contributor count reaches 2:"
echo "    docker compose exec -T chain dendrad query jobs committee-seed-health -o json"
echo "    docker compose logs chain | grep 'E4 decentralized VRF seed' | tail   # expect contributors=2"
echo "  -> 'VRF - anti-grinding' panel green = DECENTRALISED VRF proven in a distributed setting."
echo "  (Leave this node running. To stop it: pkill -f 'dendrad start --home $H')"
echo "============================================================"
