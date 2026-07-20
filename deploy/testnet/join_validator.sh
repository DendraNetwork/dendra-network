#!/usr/bin/env bash
# join_validator.sh — branche CETTE machine (ton PC/WSL) comme VALIDATEUR #2 sur le testnet du VPS, et ancre sa
# cle VRF. But : prouver la VRF DECENTRALISEE en DISTRIBUE (2 operateurs sur 2 machines separees) -> le panneau
# "VRF - anti-grinding" passe ROUGE->VERT (contributors atteint le plancher committee_min_vrf_contributors).
#
# Pre-requis : ~/dendra (la source, deja la), Go, et le VPS en ligne. Pas de docker requis localement (dendrad nu).
# Usage (WSL) :  tr -d '\r' < deploy/testnet/join_validator.sh | bash -s -- <IP_VPS>
set -uo pipefail
VPS="${1:?Usage: bash join_validator.sh <IP_VPS>}"
export PATH="$PATH:$HOME/go/bin:/usr/local/go/bin"
D="$HOME/go/bin/dendrad"; VRF="$HOME/go/bin/dendra-vrf"
H="$HOME/.dendra-join"; CHAIN=dendra
KB="--keyring-backend test --home $H"
RPC="http://$VPS:26657"; FAUCET="http://$VPS:4500"
NLOCAL="tcp://127.0.0.1:36657"        # pour dendrad --node (schema tcp://)
NLOCAL_HTTP="http://127.0.0.1:36657"  # pour curl (schema http:// — le /status est HTTP)
BOND="9000000udndr"              # bond du validateur #2 (le faucet donne 10 DNDR)
LOG="/tmp/dendra-join.log"
step(){ echo; echo "######## $* ########"; }
die(){ echo "  ECHEC: $*"; [ -f "$LOG" ] && tail -15 "$LOG"; exit 1; }

step "0) binaires frais (dendrad + dendra-vrf) depuis ~/dendra"
[ -d "$HOME/dendra" ] || die "~/dendra introuvable"
( cd "$HOME/dendra" && go build -o "$D" ./cmd/dendrad && go build -o "$VRF" ./cmd/dendra-vrf ) || die "go build"

step "1) init + genesis du VPS + pair persistant"
pkill -f "dendrad start --home $H" 2>/dev/null && sleep 2
rm -rf "$H"; "$D" init pc-val2 --chain-id "$CHAIN" --home "$H" >/dev/null 2>&1 || die "init"
curl -s "$RPC/genesis" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin)["result"]["genesis"]))' > "$H/config/genesis.json" || die "genesis"
[ -s "$H/config/genesis.json" ] || die "genesis vide (RPC du VPS injoignable ?)"
NODEID=$(curl -s "$RPC/status" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["node_info"]["id"])') || die "node-id"
echo "  seed = $NODEID@$VPS:26656"
C="$H/config/config.toml"
sed -i "s|^persistent_peers = .*|persistent_peers = \"$NODEID@$VPS:26656\"|" "$C"
sed -i 's|^laddr = "tcp://127.0.0.1:26657"|laddr = "tcp://0.0.0.0:36657"|' "$C"
sed -i 's|^laddr = "tcp://0.0.0.0:26656"|laddr = "tcp://0.0.0.0:36656"|' "$C"
sed -i 's/^timeout_commit = .*/timeout_commit = "1s"/' "$C"
sed -i 's/^addr_book_strict = .*/addr_book_strict = false/' "$C"

step "2) cle validateur (pcval) + cle VRF (ecrite dans le home)"
"$D" keys add pcval $KB >/dev/null 2>&1 || true
A=$("$D" keys show pcval -a $KB) || die "cle pcval"
echo "  pcval = $A"
read -r VSK VPK < <("$VRF" keygen); echo "$VSK" > "$H/config/vrf_key"; chmod 600 "$H/config/vrf_key"
echo "  cle VRF generee (pub ${VPK:0:16}...)"

step "3) demarrer le noeud PC (sync depuis le VPS) AVEC la cle VRF"
DENDRA_VRF_KEY_FILE="$H/config/vrf_key" nohup "$D" start --home "$H" --minimum-gas-prices 0udndr > "$LOG" 2>&1 &
echo "  demarre (log: $LOG). Attente de la synchro (catching_up:false)..."
ok=0; for i in $(seq 1 80); do sleep 3; curl -s "$NLOCAL_HTTP/status" 2>/dev/null | grep -q '"catching_up":false' && { ok=1; break; }; [ $((i%5)) -eq 0 ] && echo "  ... sync ($i/80)"; done
[ "$ok" = 1 ] || die "pas de synchro (P2P bloque ? port 26656 du VPS ouvert ?)"
echo "  SYNCHRONISE."

step "4) financer pcval (faucet du VPS) + create-validator (bond $BOND)"
curl -s -X POST "$FAUCET" -d "{\"address\":\"$A\"}" >/dev/null; sleep 6
PK=$("$D" comet show-validator --home "$H")
cat > "$H/cv.json" <<JSON
{"pubkey":$PK,"amount":"$BOND","moniker":"pc-val2","commission-rate":"0.10","commission-max-rate":"0.20","commission-max-change-rate":"0.01","min-self-delegation":"1"}
JSON
# CONFIRMATION D'EXECUTION, pas d'acceptation. Ces deux tx etaient envoyees puis leur sortie passee a
# `grep | head` : rien n'etait teste, et `|| true` avalait meme l'echec du binaire. Or un `"code":0` de
# broadcast signifie seulement « entree dans le MEMPOOL » — la tx peut echouer a l'EXECUTION. Un
# validateur non bonde ou une cle VRF non ancree laissaient donc le script afficher son succes final,
# pendant que la chaine tourne avec un contributeur de moins (anti-grinding INACTIF) ou un validateur
# fantome. Seul `query tx <hash>` donne le resultat d'execution.
confirm_tx() { # $1 = sortie JSON du broadcast, $2 = libelle ; ecrit CONFIRMED=1/0
  CONFIRMED=0
  _HASH=$(printf '%s' "$1" | tr -d ' \t' | grep -o '"txhash":"[A-Fa-f0-9]*"' | head -1 | cut -d'"' -f4)
  [ -n "$_HASH" ] || { echo "  [ERR] $2 : aucun txhash -> la tx n'a meme pas ete diffusee."; return; }
  for _ in $(seq 1 12); do
    sleep 5
    _Q=$("$D" query tx "$_HASH" --node "$NLOCAL" -o json 2>/dev/null)
    _C=$(printf '%s' "$_Q" | tr -d ' \t' | grep -o '"code":[0-9]*' | head -1 | cut -d: -f2)
    [ -n "$_C" ] && break
  done
  if [ "$_C" = "0" ]; then CONFIRMED=1; echo "  [OK] $2 EXECUTEE (tx $_HASH)."
  elif [ -n "$_C" ]; then echo "  [ERR] $2 REJETEE a l'execution (code=$_C, tx $_HASH)."
  else echo "  [ERR] $2 : tx $_HASH jamais incluse apres 60s."; fi
}

CV_OUT=$("$D" tx staking create-validator "$H/cv.json" --from pcval $KB --chain-id "$CHAIN" --node "$NLOCAL" --fees 0udndr --yes -o json 2>&1)
confirm_tx "$CV_OUT" "create-validator"; BOND_OK=$CONFIRMED

step "5) ANCRER la cle VRF de pcval on-chain (proof-of-possession)"
POP=$("$VRF" prove "$VSK" "dendra/vrf-pop/$A")
VRF_OUT=$("$D" tx jobs register-validator-vrf-key "$VPK" "$POP" --from pcval $KB --chain-id "$CHAIN" --node "$NLOCAL" --fees 0udndr --yes -o json 2>&1)
confirm_tx "$VRF_OUT" "ancrage cle VRF"; VRF_OK=$CONFIRMED

echo
echo "============================================================"
if [ "${BOND_OK:-0}" = 1 ] && [ "${VRF_OK:-0}" = 1 ]; then
  echo "  Validateur #2 (ton PC) branche + cle VRF ancree — les DEUX tx confirmees on-chain."
else
  echo "  ⛔ INCOMPLET — ne compte PAS ce validateur :"
  [ "${BOND_OK:-0}" = 1 ] || echo "     - create-validator NON confirmee -> le PC n'est pas dans le set actif."
  [ "${VRF_OK:-0}" = 1 ]  || echo "     - cle VRF NON ancree -> il ne contribue pas a la graine, anti-grinding INACTIF."
  echo "     Diagnostique puis relance : les deux etapes sont idempotentes."
fi
echo "  Verifie (depuis le VPS) que les contributeurs montent a 2 :"
echo "    docker compose exec -T chain dendrad query jobs committee-seed-health -o json"
echo "    docker compose logs chain | grep 'E4 decentralized VRF seed' | tail   # contributors=2 attendu"
echo "  -> panneau 'VRF - anti-grinding' = VERT = VRF DECENTRALISEE prouvee en distribue."
echo "  (Laisse ce noeud tourner. Pour l'arreter : pkill -f 'dendrad start --home $H')"
echo "============================================================"
