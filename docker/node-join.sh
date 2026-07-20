#!/bin/sh
# Joins a PUBLIC Dendra testnet as a full NODE (syncs from the seeds + public genesis). Idempotent:
# init once, then dendrad start. To BECOME A VALIDATOR: see deploy/testnet-node/README.md (the manual
# create-validator step = stake bond, a deliberate operator action, never automatic).
set -e
HOME_DIR="${DENDRA_HOME:-/root/.dendra}"
CHAIN_ID="${CHAIN_ID:-dendra}"   # same chain-id as the operator side; the downloaded genesis is authoritative.
MONIKER="${MONIKER:-dendra-node}"
: "${GENESIS_URL:?Set GENESIS_URL to the public genesis.json provided by the operator}"
SEEDS="${SEEDS:-}"                       # node_id@host:26656 (comma-separated)
PERSISTENT_PEERS="${PERSISTENT_PEERS:-}" # same format (persistent peers)
GENESIS_SHA256="${GENESIS_SHA256:-}"     # expected sha256 (see network-info.txt) -> verifies the downloaded genesis (anti-MITM)
CFG="$HOME_DIR/config"

# ---------------------------------------------------------------------------------------------------
# FORK CHECK — à CHAQUE démarrage, pas seulement au premier.
# Après un reset de genesis côté opérateur (DENDRA_FRESH=1), un nœud non contrôlé continue de tourner
# sur l'ANCIENNE chaîne. Symptômes trompeurs : `catching_up: false` (vrai — il n'est pas en rattrapage,
# il n'a simplement plus de pairs qui lui parlent), hauteur FIGÉE très au-dessus de celle du réseau
# réel, et un jeu de validateurs cohérent... mais celui d'un fork mort. Toute
# lecture on-chain faite via ce nœud était fausse sans le dire, et toute tx était rejetee.
# Cause : le bloc ci-dessous ne s'exécutait QUE si genesis.json n'existait pas, donc le SHA256 fourni
# par l'opérateur n'était plus jamais comparé après le premier boot — la garde anti-MITM elle-même ne
# valait que pour le premier démarrage.
if [ -f "$CFG/genesis.json" ] && [ -n "$GENESIS_SHA256" ]; then
  LOCAL_SHA=$(sha256sum "$CFG/genesis.json" | cut -d' ' -f1)
  if [ "$LOCAL_SHA" != "$GENESIS_SHA256" ]; then
    echo "[node] ====================================================================" >&2
    echo "[node] LE GENESIS LOCAL N'EST PAS CELUI DU RESEAU." >&2
    echo "[node]   local  : $LOCAL_SHA" >&2
    echo "[node]   reseau : $GENESIS_SHA256" >&2
    echo "[node] Deux causes possibles : (a) l'operateur a REMIS A ZERO le reseau (nouveau genesis) et" >&2
    echo "[node] ton etat local est celui de l'ancienne chaine ; (b) MITM sur le genesis." >&2
    echo "[node] Dans les DEUX cas, demarrer serait pire que s'arreter : le noeud repondrait a des" >&2
    echo "[node] requetes avec l'etat d'une chaine morte, sans jamais le signaler." >&2
    if [ "${DENDRA_AUTO_RESET_ON_GENESIS_CHANGE:-0}" = "1" ]; then
      echo "[node] DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 -> purge de l'etat local et re-synchro a neuf." >&2
      echo "[node] (les CLES du keyring sont conservees : seul l'etat de la chaine est efface)" >&2
      dendrad comet unsafe-reset-all --home "$HOME_DIR" >/dev/null 2>&1 \
        || dendrad tendermint unsafe-reset-all --home "$HOME_DIR" >/dev/null 2>&1 \
        || rm -rf "$HOME_DIR/data"
      rm -f "$CFG/genesis.json"
    else
      echo "[node] ARRET. Pour repartir a neuf sur le reseau courant :" >&2
      echo "[node]   docker compose -f deploy/testnet-node/docker-compose.yml down -v" >&2
      echo "[node]   puis relance deploy/join.sh (les cles du volume sont perdues avec -v : re-cree-les)" >&2
      echo "[node] Ou, en conservant les cles : DENDRA_AUTO_RESET_ON_GENESIS_CHANGE=1 au prochain boot." >&2
      echo "[node] ====================================================================" >&2
      exit 1
    fi
  else
    echo "[node] genesis local == genesis reseau (sha256 verifie a chaque boot)."
  fi
fi

if [ ! -f "$CFG/genesis.json" ]; then
  echo "[node] init $MONIKER (chain-id $CHAIN_ID)"
  dendrad init "$MONIKER" --chain-id "$CHAIN_ID" --home "$HOME_DIR" >/dev/null 2>&1 || true
  echo "[node] downloading public genesis: $GENESIS_URL"
  curl -fsSL "$GENESIS_URL" -o "$CFG/genesis.json"
  SHA=$(sha256sum "$CFG/genesis.json" | cut -d' ' -f1)
  echo "[node] genesis sha256: $SHA"
  # Anti-MITM: verification is MANDATORY by default (fail-closed). An empty GENESIS_SHA256 no longer performs a
  # silent SKIP (which would leave a public joiner unprotected). EXPLICIT opt-out for a trusted network only.
  if [ -z "$GENESIS_SHA256" ]; then
    if [ "${DENDRA_ALLOW_UNVERIFIED_GENESIS:-0}" = "1" ]; then
      echo "[node] WARNING: GENESIS_SHA256 empty + DENDRA_ALLOW_UNVERIFIED_GENESIS=1 -> anti-MITM verification DISABLED (trusted only)."
    else
      echo "[node] FATAL: GENESIS_SHA256 is required (anti-MITM). Copy the sha from network-info.txt into GENESIS_SHA256," >&2
      echo "       or set DENDRA_ALLOW_UNVERIFIED_GENESIS=1 for a TRUSTED network. ABORTING." >&2
      exit 1
    fi
  elif [ "$GENESIS_SHA256" != "$SHA" ]; then
    echo "[node] FATAL: genesis sha256 != expected ($GENESIS_SHA256) -> genesis tampered / possible MITM, ABORTING" >&2; exit 1
  else
    echo "[node] genesis sha256 VERIFIED (= expected, anti-MITM OK)."
  fi
  [ -n "$SEEDS" ] && sed -i "s|^seeds = .*|seeds = \"$SEEDS\"|" "$CFG/config.toml"
  [ -n "$PERSISTENT_PEERS" ] && sed -i "s|^persistent_peers = .*|persistent_peers = \"$PERSISTENT_PEERS\"|" "$CFG/config.toml"
  # RPC reachable from the container (P2P 26656 already listens on 0.0.0.0)
  sed -i 's|^laddr = "tcp://127.0.0.1:26657"|laddr = "tcp://0.0.0.0:26657"|' "$CFG/config.toml"

  # STATE-SYNC — ce n'est PAS une optimisation de confort ici, c'est une CORRECTION.
  # Le binaire courant écrit `audit_committee/*` (ADR-032) alors que l'ancien ne l'écrivait pas. Rejouer
  # l'historique depuis le genesis fait donc recalculer un AppHash différent de celui déjà signé, aux
  # hauteurs antérieures à la montée où un audit avait été tiré -> panic au replay. Aucune garde de
  # hauteur ne rattrape ça : l'historique contient réellement les deux comportements.
  # En state-syncant à une hauteur POSTÉRIEURE à la montée, le joiner ne rejoue jamais ces blocs.
  # DÉSACTIVÉ par défaut (STATESYNC_RPC vide) pour ne pas casser un réseau neuf sans snapshots ; le
  # network-info.txt de l'opérateur fournit les valeurs quand il y en a besoin.
  if [ -n "${STATESYNC_RPC:-}" ] && [ -n "${STATESYNC_TRUST_HEIGHT:-}" ] && [ -n "${STATESYNC_TRUST_HASH:-}" ]; then
    echo "[node] state-sync ACTIVE (trust_height=$STATESYNC_TRUST_HEIGHT) — l'historique n'est PAS rejoue."
    sed -i "/^\[statesync\]/,/^\[/{
      s|^enable = .*|enable = true|
      s|^rpc_servers = .*|rpc_servers = \"$STATESYNC_RPC\"|
      s|^trust_height = .*|trust_height = $STATESYNC_TRUST_HEIGHT|
      s|^trust_hash = .*|trust_hash = \"$STATESYNC_TRUST_HASH\"|
    }" "$CFG/config.toml"
  else
    echo "[node] state-sync inactif -> replay depuis le genesis."
    echo "       ⚠️ Sur un reseau ayant subi un changement consensus-breaking, le replay PANIQUE."
    echo "       Demande a l'operateur STATESYNC_RPC / _TRUST_HEIGHT / _TRUST_HASH s'il en publie."
  fi
fi

echo "[node] starting (syncing from seeds)..."
exec dendrad start --home "$HOME_DIR" --minimum-gas-prices "0udndr"
