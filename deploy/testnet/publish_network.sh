#!/usr/bin/env bash
# publish_network.sh — G4 (internal audit 2026-06-25) : EXTRAIT + PUBLIE les infos réseau du testnet pour que des
# TIERS puissent REJOINDRE (nœud complet / validateur). Sans ça, `deploy/testnet-node` attend GENESIS_URL/SEEDS
# que rien ne produit → multi-opérateurs impossible ET le DÉBIT distribué réel (item e du socle) non mesurable.
#
# Produit  deploy/testnet/published/{genesis.json, network-info.txt}  = chain-id + GENESIS_URL + SHA256 + SEEDS
# (node-id@IP:26656). Les joiners copient network-info.txt dans deploy/testnet-node/.env.
#
# À LANCER LÀ OÙ LE STACK TOURNE (le VPS, ou local si la chaîne tourne en docker) :
#   tr -d '\r' < deploy/testnet/publish_network.sh | bash -s -- <IP_PUBLIQUE> [GENESIS_BASE_URL]
# Idempotent (ré-extrait à chaque appel). Le node-id/genesis viennent du conteneur `chain`.
set -u
HOST="${1:?Usage: ... | bash -s -- <IP_ou_HOST_PUBLIC> [GENESIS_BASE_URL]}"
BASE_URL="${2:-http://$HOST:8088}"   # URL où tu hébergeras genesis.json (défaut : un http.server local sur :8088)
# audit 2026-07-02 : plus de chemin PERSO en dur (fuite d'identite dans le repo public). DENDRA_REPO
# prime ; sinon on remonte depuis le cwd jusqu'a la racine du depot/stack (docker-compose.yml + deploy/).
_find_repo(){ local d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "[ERR] racine du depot introuvable : exporte DENDRA_REPO=/chemin/du/depot"; exit 1; }
OUT="$REPO/deploy/testnet/published"
DC="${DENDRA_COMPOSE:-docker compose}"
command -v docker >/dev/null 2>&1 || { echo "[ERR] docker absent (lance ce script sur l'hôte du stack)"; exit 1; }
mkdir -p "$OUT"

echo "## 1) node-id + genesis depuis le conteneur 'chain'"
NID=$($DC exec -T chain dendrad tendermint show-node-id </dev/null 2>/dev/null | tr -d '\r\n ')  # </dev/null : sinon docker exec MANGE le stdin d'un script pipe (fin silencieuse exit 0 — bug vu au launch 07-17)
[ -n "$NID" ] || { echo "[ERR] node-id introuvable — le service 'chain' tourne-t-il ? ($DC ps)"; exit 1; }
$DC exec -T chain cat /root/.dendra/config/genesis.json </dev/null > "$OUT/genesis.json" 2>/dev/null
[ -s "$OUT/genesis.json" ] || { echo "[ERR] genesis.json vide/inaccessible"; exit 1; }

CID=$(python3 -c "import json;print(json.load(open('$OUT/genesis.json')).get('chain_id','dendra'))" 2>/dev/null || echo dendra)
SHA=$(sha256sum "$OUT/genesis.json" | cut -d' ' -f1)
SEEDS="$NID@$HOST:26656"

# POINT DE CONFIANCE POUR LE STATE-SYNC. Sur une chaîne ayant subi un changement consensus-breaking,
# rejouer l'historique fait PANIQUER un nouveau nœud (l'AppHash recalculé diffère de celui signé). Le
# joiner doit donc démarrer d'un snapshot, ce qui exige une hauteur + son hash, publiés par l'opérateur.
# On prend une hauteur RÉCENTE mais déjà committée (h-10) : trop récente, le snapshot n'existe pas encore.
SS_H=$($DC exec -T chain dendrad status --node tcp://127.0.0.1:26657 </dev/null 2>/dev/null \
  | grep -oE '"latest_block_height":"[0-9]+"' | head -1 | grep -oE '[0-9]+')
SS_RPC=""; SS_HASH=""
if [ -n "$SS_H" ] && [ "$SS_H" -gt 20 ] 2>/dev/null; then
  SS_H=$((SS_H - 10))
  # On interroge le RPC CometBFT DIRECTEMENT plutôt que la CLI : `dendrad query block` a changé de
  # signature selon les versions du SDK (--type=height, positionnel, absent…) et rendait une sortie vide
  # SANS erreur — le point de confiance était alors publié amputé de son hash, donc inutilisable, en
  # silence. L'endpoint `/block?height=` est stable depuis Tendermint et ne dépend pas de la version CLI.
  # `tr -d ' \t'` AVANT le grep : un JSON indenté écrit `"hash": "…"` avec une espace après le
  # deux-points. C'est la QUATRIÈME fois aujourd'hui qu'une extraction échoue sur ce détail (compte
  # signataire, relecture de clé VRF, ici). Le motif est toujours le même : on écrit le regex d'après
  # le JSON compact qu'on a en tête, le service rend de l'indenté, et l'extraction rend vide SANS erreur.
  SS_HASH=$($DC exec -T chain curl -s "http://127.0.0.1:26657/block?height=$SS_H" </dev/null 2>/dev/null \
    | tr -d ' \t' | grep -oE '"hash":"[0-9A-Fa-f]{64}"' | head -1 | cut -d'"' -f4)
  [ -n "$SS_HASH" ] && SS_RPC="http://$HOST:26657,http://$HOST:26657"   # 2 entrées = exigence CometBFT
fi
# COHÉRENCE : les 3 valeurs n'ont de sens qu'ENSEMBLE. Une hauteur sans hash produit un config.toml que
# CometBFT refuse au démarrage — on préfère ne RIEN publier plutôt qu'un point de confiance amputé.
if [ -z "$SS_HASH" ] || [ -z "$SS_RPC" ]; then SS_H=""; SS_HASH=""; SS_RPC=""; fi

cat > "$OUT/network-info.txt" <<EOF
# Dendra testnet — INFOS RÉSEAU PUBLIQUES (G4). À coller dans deploy/testnet-node/.env par chaque joiner.
CHAIN_ID=$CID
GENESIS_URL=$BASE_URL/genesis.json
GENESIS_SHA256=$SHA
SEEDS=$SEEDS
PERSISTENT_PEERS=$SEEDS
DENDRA_NODE=tcp://$HOST:26657
DENDRA_RELAY=http://$HOST:8645
FAUCET=http://$HOST:4500
# --- state-sync : OBLIGATOIRE sur cette chaîne (changement consensus-breaking dans l'historique).
# Sans ces 3 valeurs, un nouveau nœud rejoue depuis le genesis et PANIQUE. Vides = l'opérateur n'a pas
# encore de snapshot exploitable ; dans ce cas, ne pas inviter de tiers à rejoindre.
STATESYNC_RPC=$SS_RPC
STATESYNC_TRUST_HEIGHT=$SS_H
STATESYNC_TRUST_HASH=$SS_HASH
EOF
if [ -z "$SS_HASH" ]; then
  echo "[publish] ⚠️ point de confiance state-sync INDISPONIBLE (hauteur ou hash illisible)."
  echo "          Un joiner rejouerait l'historique et paniquerait -> N'INVITE PERSONNE tant que c'est vide."
else
  echo "[publish] point de confiance state-sync publie : hauteur $SS_H"
fi

echo "## 2) PUBLIÉ -> $OUT/"
cat "$OUT/network-info.txt"
echo "------------------------------------------------------------"
echo "## 3) HÉBERGE le genesis à GENESIS_URL. Le plus simple :"
echo "      cd $OUT && nohup python3 -m http.server 8088 >/tmp/dendra-genesis-http.log 2>&1 &"
echo "   (ou commit deploy/testnet/published/genesis.json, ou un CDN/S3). Ouvre le port 8088 + le P2P 26656."
echo "## 4) Un joiner (LE plus simple, T10) :  CONFIG_URL=$BASE_URL/network-info.txt bash deploy/join.sh"
echo "   (network-info.txt porte desormais les 7 cles : chain/genesis/sha/seeds + NODE/RELAY/FAUCET -> une URL suffit.)"
echo "   Heberge network-info.txt A COTE du genesis (meme http.server). Manuel/avance : README des kits."
