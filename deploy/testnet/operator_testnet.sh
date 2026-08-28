#!/usr/bin/env bash
# operator_testnet.sh - deploys the PERSISTENT Dendra TESTNET on the VPS, in one command.
#   rsync the repo -> VPS:~/dendra; check the entrypoints are intact (guard against truncated copies);
#   build (chain, then services); up (5 core services); firewall; wait for the RPC; print the miner command.
# Prerequisite: the VPS has already been reset (deploy/testnet/vps_reset.sh).
# Usage (WSL): export SSHPASS='<root password>'; bash deploy/testnet/operator_testnet.sh <VPS_IP>
set -uo pipefail
VPS="${1:?Usage: export SSHPASS=password ; bash deploy/testnet/operator_testnet.sh VPS_IP}"
: "${SSHPASS:?export SSHPASS=password FIRST (never committed)}"
# No hardcoded personal path: that would leak an identity into the public repo. DENDRA_REPO wins;
# otherwise walk up from the cwd to the repo root, recognised by docker-compose.yml plus deploy/.
_find_repo(){ local d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "  FAIL: repo root not found: export DENDRA_REPO=/path/to/repo"; exit 1; }
SSHO="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
RSH="sshpass -e ssh -n $SSHO root@$VPS"
die(){ echo "  FAIL: $*"; exit 1; }
command -v sshpass >/dev/null || die "install sshpass: sudo apt install -y sshpass"
command -v rsync   >/dev/null || die "install rsync: sudo apt install -y rsync"

echo "########## 1) SSH + Docker ##########"
$RSH 'echo ssh OK; docker --version 2>/dev/null || echo NO_DOCKER' || die "SSH failed (IP, password, port 22?)"
$RSH 'docker --version >/dev/null 2>&1' || { echo "  installing Docker..."; $RSH 'curl -fsSL https://get.docker.com | sh' || die "docker install failed"; }

echo "########## 2) copy the repo -> VPS:~/dendra (checksum) ##########"
sshpass -e rsync -az --checksum --delete -e "ssh $SSHO" --exclude .git --exclude __pycache__ --exclude '*.pyc' "$REPO/" "root@$VPS:~/dendra/" || die "rsync failed"
echo "  repo copied"

echo "########## 3) entrypoint integrity check (guards against a truncated copy) ##########"
$RSH 'cd ~/dendra && for s in docker/entrypoint-chain.sh docker/entrypoint-services.sh; do sh -n "$s" || { echo "  INVALID/TRUNCATED: $s"; exit 3; }; done && echo "  entrypoints OK"' || die "invalid entrypoint on the VPS side (re-run: the copy was truncated)"

echo "########## 4) build (chain, then services) + up -- LONG on the first chain build (~6-8 min) ##########"
$RSH 'cd ~/dendra && docker compose build chain && docker compose build relay && docker compose up -d' || die "docker build/up failed (paste the output)"

echo "########## 5) firewall (best effort) ##########"
$RSH 'command -v ufw >/dev/null && { ufw allow 26657/tcp; ufw allow 26656/tcp; ufw allow 8645/tcp; ufw allow 4500/tcp; ufw allow 8651/tcp; ufw allow 8080/tcp; }; true' || true

echo "########## 6) waiting for the public RPC ##########"
ok=0
for i in $(seq 1 90); do
  sleep 6
  [ "$(curl -s "http://$VPS:26657/status" 2>/dev/null | tr -d ' \t' | grep -o '"latest_block_height":"[0-9]*"' | grep -o '[0-9]*' | head -1)" -gt 0 ] 2>/dev/null && { ok=1; break; }
  [ $((i % 5)) -eq 0 ] && echo "  ... waiting for the RPC ($i/90)"
done
echo
if [ "$ok" = 1 ]; then
  H=$(curl -s "http://$VPS:26657/status" 2>/dev/null | tr -d ' \t' | grep -o '"latest_block_height":"[0-9]*"' | grep -o '[0-9]*' | head -1)
  echo "=============================================================="
  echo "  DENDRA TESTNET ONLINE (height=$H, chain-id=dendra, PERSISTENT)."
  echo "  Chat (OpenWebUI) : http://$VPS:8080"
  echo "  Public RPC       : http://$VPS:26657/status"
  echo "  Faucet           : http://$VPS:4500   |  Gateway : http://$VPS:8651/v1"
  echo
  echo "  MINER (local PC or rented GPU) on llama3.1:8b (the default; mistral-nemo stays registered):"
  # The command printed here used to name a script that is not part of the published repository, so the
  # one line an operator copies at the end of a successful deployment led nowhere. deploy/join.sh IS
  # published and accepts the endpoints directly, which is what this stack exposes (it publishes no
  # network-info.txt — that is publish_network.sh). Leaving the stake unset is deliberate: the miner
  # then reads min_stake FROM THE CHAIN, so the command cannot go stale when the parameter is governed.
  echo "    cd $REPO && DENDRA_NODE=tcp://$VPS:26657 DENDRA_RELAY=http://$VPS:8645 FAUCET=http://$VPS:4500 \\"
  echo "      bash deploy/join.sh --id miner-PC"
  echo "=============================================================="
else
  echo "  RPC silent. Diagnostics (paste the output):"
  echo "    sshpass -e ssh $SSHO root@$VPS 'cd ~/dendra && docker compose ps; docker compose logs --tail 100 chain'"
  exit 1
fi
