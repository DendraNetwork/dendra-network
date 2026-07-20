#!/bin/sh
# Dispatch du service Python a lancer (1er argument). Variables via l'environnement (compose).
set -e
ROLE="${1:-relay}"; shift 2>/dev/null || true
RELAY="${DENDRA_RELAY:-http://relay:8645}"

case "$ROLE" in
  relay)
    exec python3 relay.py "${RELAY_PORT:-8645}" ;;
  gateway)
    python3 gateway_fund.py || true
    exec python3 gateway.py ;;
  exporter)
    exec python3 exporter.py ;;
  faucet)
    exec python3 faucet.py ;;
  miner)
    ID="${1:-m1}"
    i=0; while [ $i -lt 60 ]; do
      python3 -c "import urllib.request,sys; urllib.request.urlopen('${RELAY}/list',timeout=3)" 2>/dev/null && break
      i=$((i+1)); sleep 2
    done
    # ADR-026/028 (finding 2026-06-27) : DENDRA_MINER_JUDGE=1 -> ce mineur participe AUSSI au COMITE D'AUDIT optimiste
    # (reveal+verdict) => l'audit a des DENTS (sinon les jobs audites auto-vindiquent au timeout = audit inerte sur le
    # stack). Sert les DEUX topologies : flaguer les work-miners (tout-mineur) OU lancer des conteneurs mineurs CPU
    # flagues (juges dedies, requis si peu de GPU). judge_worker.py est FATAL si la cle <id>.sk manque (creee par
    # miner a l'enregistrement) -> on lance le mineur D'ABORD, on ATTEND la cle, PUIS les juges.
    # DORMANT par defaut (=0 : comportement mineur strictement inchange ; arme deliberement pour l'audit/incentive).
    if [ "${DENDRA_MINER_JUDGE:-0}" = "1" ]; then
      python3 miner.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
        --faucet "${FAUCET:-http://chain:4500}" --backend "${BACKEND:-ollama}" &
      MPID=$!
      j=0; while [ $j -lt 90 ] && [ ! -f "/data/keys/$ID.sk" ]; do j=$((j+1)); sleep 2; done
      if [ -f "/data/keys/$ID.sk" ]; then
        JEP="${DENDRA_JUDGE_ENDPOINT:-${OLLAMA_ENDPOINT:-http://localhost:11434}}"
        JM=""; [ -n "${DENDRA_JUDGE_MODEL_OVERRIDE:-}" ] && JM="--model-id ${DENDRA_JUDGE_MODEL_OVERRIDE}"
        echo "[judge] cle $ID prete -> reveal_worker + judge_worker (comite d'audit ; Ollama juge=$JEP)"
        python3 reveal_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys >/tmp/reveal-"$ID".log 2>&1 &
        OLLAMA_ENDPOINT="$JEP" python3 judge_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
          --reveal-grace "${DENDRA_REVEAL_GRACE:-2}" --adjudicate $JM >/tmp/judge-"$ID".log 2>&1 &
      else
        echo "[judge] AVERTISSEMENT: cle $ID absente apres 180s -> juges non lances (mineur seul)"
      fi
      wait "$MPID"
    else
      exec python3 miner.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
        --faucet "${FAUCET:-http://chain:4500}" --backend "${BACKEND:-ollama}"
    fi ;;
  cli)
    exec python3 cli.py "$@" ;;
  proof)
    # WO-1 lancement public (2026-07-10) : The Proof = facade lecture-seule de verifiabilite (job recents,
    # pools, sante VRF). Publique par design (aucun secret) ; HOST via DENDRA_PROOF_HOST (compose).
    exec python3 the_proof.py ;;
  points)
    # WO-1 lancement public : leaderboard Saison 0 (PROVISOIRE, stateless, recalculable — Couche B).
    exec python3 points_indexer.py --serve ;;
  capacity)
    # Registre de capacite reseau : inventaire materiel + modeles servis, DECLARE par les operateurs
    # (non prouve on-chain — le lire comme un inventaire, jamais comme une preuve de puissance).
    # Persiste sur DISQUE (DENDRA_CAPACITY_DB) : un registre qui oublie au redemarrage est un bug.
    exec python3 capacity_server.py ;;
  *)
    echo "role inconnu: $ROLE (relay|gateway|exporter|faucet|miner <id>|proof|points|capacity|cli ...)"; exit 2 ;;
esac
