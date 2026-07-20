#!/usr/bin/env bash
# vps_reset.sh — REMET LE VPS A ZERO. Diagnostic (logs chain) PUIS wipe total.
#   - arrete la pile, supprime conteneurs + volumes + images + cache de build
#   - efface ~/dendra
#   - Docker lui-meme reste installe
# Usage (WSL, Terminal 1) :  export SSHPASS='<mdp root vps>' ; bash deploy/testnet/vps_reset.sh <IP_VPS>
set -uo pipefail
VPS="${1:?Usage: export SSHPASS=mdp ; bash deploy/testnet/vps_reset.sh IP_VPS}"
: "${SSHPASS:?export SSHPASS=mdp AVANT (jamais committe)}"
SSHO="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
RSH="sshpass -e ssh -n $SSHO root@$VPS"
command -v sshpass >/dev/null || { echo "  installe sshpass : sudo apt install -y sshpass"; exit 1; }

echo "########## 1) DIAGNOSTIC — derniers logs de la chaine (avant wipe) ##########"
$RSH 'cd ~/dendra 2>/dev/null && docker compose logs --tail 50 chain 2>&1 | tail -50 || echo "  (aucune pile en cours)"' || { echo "  ECHEC SSH (IP / mdp / port 22 ?)"; exit 1; }

echo "########## 2) WIPE TOTAL ##########"
$RSH 'cd ~/dendra 2>/dev/null && docker compose down -v --remove-orphans 2>/dev/null; \
  ids=$(docker ps -aq); [ -n "$ids" ] && docker rm -f $ids; \
  docker system prune -af --volumes; \
  rm -rf ~/dendra; \
  echo "--- disque ---"; df -h /' || { echo "  ECHEC wipe"; exit 1; }

echo "=============================================================="
echo "  VPS VIERGE (Docker conserve). Pret pour le deploiement testnet propre."
echo "=============================================================="
