#!/usr/bin/env bash
# operator_testnet.sh - deploie le TESTNET Dendra PERSISTANT sur le VPS (une commande).
#   rsync depot -> VPS:~/dendra ; verifie l'integrite des entrypoints (anti-troncature) ;
#   build (chain puis services) ; up (5 services core) ; pare-feu ; attend le RPC ; imprime la commande mineur.
# Prerequis : VPS deja remis a zero (deploy/testnet/vps_reset.sh).
# Usage (WSL) : export SSHPASS='<mdp root>' ; bash deploy/testnet/operator_testnet.sh <IP_VPS>
set -uo pipefail
VPS="${1:?Usage: export SSHPASS=mdp ; bash deploy/testnet/operator_testnet.sh IP_VPS}"
: "${SSHPASS:?export SSHPASS=mdp AVANT (jamais committe)}"
# audit 2026-07-02 : plus de chemin PERSO en dur (fuite d'identite dans le repo public). DENDRA_REPO
# prime ; sinon on remonte depuis le cwd jusqu'a la racine du depot (marqueurs docker-compose.yml + deploy/).
_find_repo(){ local d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "  ECHEC: racine du depot introuvable : exporte DENDRA_REPO=/chemin/du/depot"; exit 1; }
SSHO="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
RSH="sshpass -e ssh -n $SSHO root@$VPS"
die(){ echo "  ECHEC: $*"; exit 1; }
command -v sshpass >/dev/null || die "installe sshpass : sudo apt install -y sshpass"
command -v rsync   >/dev/null || die "installe rsync : sudo apt install -y rsync"

echo "########## 1) SSH + Docker ##########"
$RSH 'echo ssh OK; docker --version 2>/dev/null || echo NO_DOCKER' || die "SSH KO (IP/mdp/port 22 ?)"
$RSH 'docker --version >/dev/null 2>&1' || { echo "  install Docker..."; $RSH 'curl -fsSL https://get.docker.com | sh' || die "install docker KO"; }

echo "########## 2) copie du depot -> VPS:~/dendra (checksum) ##########"
sshpass -e rsync -az --checksum --delete -e "ssh $SSHO" --exclude .git --exclude __pycache__ --exclude '*.pyc' "$REPO/" "root@$VPS:~/dendra/" || die "rsync KO"
echo "  depot copie"

echo "########## 3) verif integrite des entrypoints (anti-troncature mount) ##########"
$RSH 'cd ~/dendra && for s in docker/entrypoint-chain.sh docker/entrypoint-services.sh; do sh -n "$s" || { echo "  INVALIDE/TRONQUE: $s"; exit 3; }; done && echo "  entrypoints OK"' || die "entrypoint invalide cote VPS (relance : le mount a tronque a la copie)"

echo "########## 4) build (chain puis services) + up -- LONG au 1er build chaine (~6-8 min) ##########"
$RSH 'cd ~/dendra && docker compose build chain && docker compose build relay && docker compose up -d' || die "docker build/up KO (colle la sortie)"

echo "########## 5) pare-feu (best-effort) ##########"
$RSH 'command -v ufw >/dev/null && { ufw allow 26657/tcp; ufw allow 26656/tcp; ufw allow 8645/tcp; ufw allow 4500/tcp; ufw allow 8651/tcp; ufw allow 8080/tcp; }; true' || true

echo "########## 6) attente du RPC public ##########"
ok=0
for i in $(seq 1 90); do
  sleep 6
  [ "$(curl -s "http://$VPS:26657/status" 2>/dev/null | tr -d ' \t' | grep -o '"latest_block_height":"[0-9]*"' | grep -o '[0-9]*' | head -1)" -gt 0 ] 2>/dev/null && { ok=1; break; }
  [ $((i % 5)) -eq 0 ] && echo "  ... attente RPC ($i/90)"
done
echo
if [ "$ok" = 1 ]; then
  H=$(curl -s "http://$VPS:26657/status" 2>/dev/null | tr -d ' \t' | grep -o '"latest_block_height":"[0-9]*"' | grep -o '[0-9]*' | head -1)
  echo "=============================================================="
  echo "  TESTNET DENDRA EN LIGNE (height=$H, chain-id=dendra, PERSISTANT)."
  echo "  Chat (OpenWebUI) : http://$VPS:8080"
  echo "  RPC public       : http://$VPS:26657/status"
  echo "  Faucet           : http://$VPS:4500   |  Passerelle : http://$VPS:8651/v1"
  echo
  echo "  MINEUR (PC ou GPU loue) sous llama3.1:8b (defaut P2 2026-07-04 ; mistral-nemo reste enregistre), stake >= min_stake (50000) :"
  echo "    cd $REPO && HOST=$VPS DENDRA_MINER_STAKE=60000 bash deploy/rented-devnet/rented_miner.sh mineur-PC"
  echo "=============================================================="
else
  echo "  RPC muet. Diagnostic (colle-moi la sortie) :"
  echo "    sshpass -e ssh $SSHO root@$VPS 'cd ~/dendra && docker compose ps; docker compose logs --tail 100 chain'"
  exit 1
fi
