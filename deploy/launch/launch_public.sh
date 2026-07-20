#!/usr/bin/env bash
# launch_public.sh — one-command PUBLIC LAUNCH kit.
#
# Deploys the public incentivized network on the VPS: chain (launch genesis, optimistic verification + veto),
# hardened relay/faucet(PoW)/gateway (fail-closed), The Proof + Season-0 points (compose profile `public`),
# monitoring, and genesis/seeds publication. THEN prints the follow-up sequence (heterogeneous judges, 2nd
# validator, validation run) — the wide announcement stays gated on the validation review.
#
# Prerequisite: ~/.dendra-launch.env generated and GREEN:  tr -d '\r' < deploy/launch/launch_env_check.sh | bash
# Usage (WSL, repo root):
#   export SSHPASS='<mdp root VPS>'
#   tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <IP_VPS> [HOSTNAME_PUBLIC]
set -uo pipefail
VPS="${1:?Usage: export SSHPASS=mdp ; ... | bash -s -- <IP_VPS> [HOSTNAME_PUBLIC]}"
PUBHOST="${2:-$VPS}"
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
_find_repo(){ local d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "ECHEC: racine du depot introuvable (exporte DENDRA_REPO)"; exit 1; }
: "${SSHPASS:?export SSHPASS=mdp AVANT (jamais committe)}"
SSHO="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
RSH="sshpass -e ssh -n $SSHO root@$VPS"
die(){ echo "  ECHEC: $*"; exit 1; }
command -v sshpass >/dev/null || die "installe sshpass : sudo apt install -y sshpass"
command -v rsync   >/dev/null || die "installe rsync : sudo apt install -y rsync"

# --- TRACEUR D'ETAPES : detecte un bloc SAUTE SILENCIEUSEMENT -------------------------------------
# Un bloc a en-tete INCONDITIONNEL peut ne pas s'executer sans qu'aucune erreur ne s'affiche : cache
# de systeme de fichiers servant une version perimee, vol de stdin par une commande dans un script
# pipe, troncature de lecture. Le lancement se declare alors UP avec une etape manquante.
# Plutot que de deviner, on DETECTE la classe entiere : chaque etape se declare, et la fin du script
# refuse de conclure si une etape obligatoire n'a pas laisse sa marque. Un saut silencieux devient un
# ECHEC BRUYANT — le lancement public ne peut plus se declarer reussi en etant incomplet.
_STEPS_DONE=""
# On ne retient que l'IDENTIFIANT (premier mot : "7b)"), pas le libelle — sinon l'assertion finale
# comparerait des phrases entieres et echouerait au moindre mot change.
step(){ _STEPS_DONE="$_STEPS_DONE|${1%% *}|"; echo "########## $* ##########"; }
_ran(){ case "$_STEPS_DONE" in *"|$1|"*) return 0;; *) return 1;; esac; }
assert_steps(){
  _missing=""
  for s in "$@"; do _ran "$s" || _missing="$_missing $s"; done
  [ -z "$_missing" ] && return 0
  echo "  ECHEC: etape(s) OBLIGATOIRE(S) non executee(s) :$_missing"
  echo "  Un bloc du script a ete saute SILENCIEUSEMENT — le reseau est potentiellement incomplet"
  echo "  (ex. passerelle non financee = chat casse). Ne rien annoncer. Verifie que le fichier lu est"
  echo "  bien a jour (\`grep -c '^step ' $REPO/deploy/launch/launch_public.sh\`) puis relance."
  exit 1
}

step "0) GATE C4 : .env public VERT obligatoire"
[ -f "$ENVF" ] || die ".env public absent — lance launch_env_check.sh --init puis le check"
tr -d '\r' < "$REPO/deploy/launch/launch_env_check.sh" | bash || die "launch_env_check ROUGE — corrige avant de deployer"

step "1) SSH + Docker"
$RSH 'echo ssh OK; docker --version 2>/dev/null || echo NO_DOCKER' || die "SSH KO (IP/mdp/port 22 ?)"
$RSH 'docker --version >/dev/null 2>&1' || { echo "  install Docker..."; $RSH 'curl -fsSL https://get.docker.com | sh' || die "install docker KO"; }

step "2) depot + .env -> VPS:~/dendra"
# DEFAULT-DENY: ship ONLY what the host RUNS (chain source, services, container defs, deploy kit).
# An exclude-list is the wrong shape here — it fails open: any file added to the repo tomorrow lands on a
# public host reachable by password over SSH unless someone remembers to exclude it. The allow-list below
# fails closed instead: a new file has to be named to be shipped. A compromise of the host must not hand
# over the project's internal working material as a bonus.
# --delete-excluded also PURGES anything the host still carries from an earlier, wider sync.
sshpass -e rsync -az --checksum --delete --delete-excluded -e "ssh $SSHO" \
  --exclude '.git' --exclude '__pycache__/' --exclude '*.pyc' \
  --include 'chain/***' --include 'prototype/***' --include 'docker/***' --include 'deploy/***' \
  --include 'docker-compose.yml' \
  --exclude '*' \
  "$REPO/" "root@$VPS:~/dendra/" || die "rsync depot KO"
sshpass -e rsync -az -e "ssh $SSHO" "$ENVF" "root@$VPS:~/dendra/.env" || die "rsync .env KO"
$RSH 'chmod 600 ~/dendra/.env' || true
echo "  depot + .env copies (les artefacts bench-results/ RESTENT locaux — donnees de dev, P4)"

step "3) verif integrite entrypoints (anti-troncature)"
$RSH 'cd ~/dendra && for s in docker/entrypoint-chain.sh docker/entrypoint-services.sh; do sh -n "$s" || { echo "  INVALIDE/TRONQUE: $s"; exit 3; }; done && echo "  entrypoints OK"' || die "entrypoint invalide cote VPS"

if [ "${DENDRA_FRESH:-0}" = "1" ]; then
  echo "########## 3b) RESET GENESIS (DENDRA_FRESH=1) — purge des volumes = repart de 0 ##########"
  # PUBLIC LAUNCH: start from a CLEAN genesis (Season-0 points at zero, no bench/test data in history).
  # Without this, an existing genesis volume is REUSED -> you would launch public on top of test data.
  $RSH 'cd ~/dendra && docker compose --profile public --profile monitoring down -v 2>/dev/null; docker volume rm dendra_dendra-home 2>/dev/null; true'
  echo "  volumes purged -> the next boot builds a FRESH genesis"
fi

step "4) build + up (core + public + monitoring) — LONG on the first build (~6-8 min)"
$RSH 'cd ~/dendra && docker compose build chain && docker compose build relay && docker compose --profile public --profile monitoring up -d' || die "docker build/up failed"

step "5) wait for RPC (LOCAL — the public firewall opens only AFTER all gates)"
# The wait is done LOCALLY (RSH -> docker exec / curl localhost); public exposure (section 9) happens ONLY
# after the supply assert + on-chain gates are green, so a non-compliant network is never reachable.
ok=0
for i in $(seq 1 90); do
  sleep 6
  # `grep -q latest_block_height` testait la presence du NOM DE LA CLE : une chaine repondant mais
  # FIGEE A 0 (consensus non demarre, pair unique, panic au replay) passait cette porte comme "UP".
  # On exige une HAUTEUR NUMERIQUE > 0. `tr -d ' \t'` : le RPC HTTP de CometBFT rend du JSON INDENTE,
  # le motif compact n'y matche pas et rendrait une valeur VIDE -- sans erreur.
  H=$($RSH "curl -s http://localhost:26657/status 2>/dev/null | tr -d ' \t' | grep -o '\"latest_block_height\":\"[0-9]*\"' | grep -o '[0-9]*' | head -1")
  [ -n "$H" ] && [ "$H" -gt 0 ] 2>/dev/null && { ok=1; break; }
  [ $((i % 5)) -eq 0 ] && echo "  ... waiting for RPC ($i/90)"
done
[ "$ok" = 1 ] || { echo "  RPC silent. Diagnose: ssh root@$VPS 'cd ~/dendra && docker compose ps; docker compose logs --tail 100 chain'"; exit 1; }

step "6) on-chain check of the LAUNCH gates (verification mode + hold + audit quorum)"
$RSH 'cd ~/dendra && docker compose exec -T chain dendrad query jobs params -o json' 2>/dev/null \
  | python3 -c '
import json,sys
p=json.load(sys.stdin).get("params",{})
want={"verification_mode":"1","hold_bps":"10000","audit_min_quorum":"4"}
bad=[f"{k}={p.get(k)}(expected {v})" for k,v in want.items() if str(p.get(k))!=v]
print("  FAIL gates: "+", ".join(bad) if bad else "  OK verification_mode=1 + hold_bps=10000 + audit_min_quorum=4")
print("  audit_sample_bps=%s audit_resolve_timeout=%s silence_slash_bps=%s" % (p.get("audit_sample_bps"),p.get("audit_resolve_timeout"),p.get("silence_slash_bps")))
sys.exit(1 if bad else 0)' || die "on-chain gates NON compliant — announce NOTHING; review the printed output"

step "7) ASSERT supply <= 10,000,000 DNDR (zero-mint) — die BEFORE exposure if non-compliant"
# Zero-mint invariant: exactly ONE denom (udndr) and a supply that never EXCEEDS the 10,000,000 DNDR genesis
# cap (a mint would exceed it; the soft burn only lowers it). A parasite mint/denom blocks the launch.
$RSH 'cd ~/dendra && docker compose exec -T chain dendrad q bank total-supply -o json' 2>/dev/null \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
sup=d.get("supply") or d.get("amount") or []
if isinstance(sup,dict): sup=[sup]
udndr=[c for c in sup if c.get("denom")=="udndr"]
amt=int(udndr[0].get("amount","0")) if udndr else -1
CAP=10000000000000
ok = len(sup)==1 and len(udndr)==1 and 0 < amt <= CAP
print("  OK supply = %d udndr (<= 10,000,000 DNDR, single denom udndr — zero-mint; burned=%d)" % (amt, CAP-amt) if ok else "  FAIL supply="+repr(sup))
sys.exit(0 if ok else 1)' || die "supply NON compliant (expected <=10000000000000 udndr, single denom udndr, >0) — parasite mint/denom -> stop BEFORE any exposure"

step "7b) BOOTSTRAP du compte de subvention 'gw' (sinon le free-tier est mort-ne)"
# La passerelle se finance normalement AU FAUCET, mais le faucet est PoW-gate en public : elle n'y
# arrive pas seule, abandonne apres 40 essais ("gw non finance apres 40 essais") et plus AUCUN job ne
# peut s'ouvrir — le client recoit "[Dendra] the network could not process the request". Sur un genesis
# frais c'est SYSTEMATIQUE (volume purge = compte a zero), et ca se repare a la main apres coup.
# On amorce donc ici, une fois, depuis une poche du genesis. Idempotent : rien n'est envoye
# si le solde suffit deja.
GW_MIN="${DENDRA_GW_MIN_UDNDR:-10000000}"
GW_TOPUP="${DENDRA_GW_TOPUP_UDNDR:-100000000}"
GW_ADDR=""
for _ in $(seq 1 30); do
  GW_ADDR="$($RSH "cd ~/dendra && docker compose logs gateway 2>/dev/null | grep -o 'gw=dendra1[a-z0-9]*' | head -1 | cut -d= -f2" 2>/dev/null | tr -d '\r\n ')"
  [ -n "$GW_ADDR" ] && break
  sleep 4
done
if [ -z "$GW_ADDR" ]; then
  echo "  WARN: adresse du compte gw introuvable dans les logs de la passerelle — free-tier a verifier a la main."
else
  GW_BAL="$($RSH "cd ~/dendra && docker compose exec -T chain dendrad query bank balances $GW_ADDR -o json --node tcp://localhost:26657" 2>/dev/null | python3 -c 'import json,sys
try:
    b=json.load(sys.stdin).get("balances",[])
    print(next((c["amount"] for c in b if c.get("denom")=="udndr"), "0"))
except Exception:
    print("0")')"
  GW_BAL="${GW_BAL:-0}"
  if [ "$GW_BAL" -lt "$GW_MIN" ] 2>/dev/null; then
    echo "  gw=$GW_ADDR solde=$GW_BAL (< $GW_MIN) -> amorcage de $GW_TOPUP udndr depuis 'bob'"
    if $RSH "cd ~/dendra && docker compose exec -T chain dendrad tx bank send bob $GW_ADDR ${GW_TOPUP}udndr --keyring-backend test --chain-id dendra --node tcp://localhost:26657 --gas-prices 0udndr --yes -o json" 2>/dev/null | grep -q '"code":0'; then
      echo "  gw amorce -> redemarrage de la passerelle (elle avait epuise ses tentatives faucet)"
      $RSH 'cd ~/dendra && docker compose restart gateway' >/dev/null 2>&1
    else
      echo "  WARN: amorcage gw refuse (poche 'bob' vide ?) — le free-tier restera inactif, les jobs echoueront."
    fi
  else
    echo "  OK gw=$GW_ADDR deja finance (solde=$GW_BAL udndr)"
  fi
fi

step "7c) ANCRAGE VRF de l'operateur (verifie, pas best-effort)"
# `entrypoint-chain.sh` tente cet ancrage en TACHE DE FOND au boot. Une tache de fond peut generer la cle
# puis ne rien ancrer sans imprimer NI succes NI son propre message d'echec. Le reseau tourne alors avec
# `contributors < min_required`, donc **anti-grinding INACTIF**, pendant que la chaine
# le criait a chaque bloc dans un log que personne ne lit. Un best-effort en arriere-plan sur une garde de
# securite est un oxymore : on le rejoue ici, au premier plan, avec confirmation par tx committee.
# Idempotent (le handler ecrase la cle) et NON bloquant : sans ancrage le reseau tourne, il est juste
# moins decentralise — on le DIT fort plutot que de refuser de demarrer.
$RSH "cd ~/dendra && tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | docker compose exec -T -e HOME_DIR=/root/.dendra -e CHAIN_ID=dendra -e NODE=tcp://127.0.0.1:26657 -e VAL_KEY=validator chain bash -s" \
  || echo "  [!] ancrage VRF de l'operateur NON confirme -> la graine restera SOUS-DECENTRALISEE (anti-grinding inactif). A rejouer a la main avant toute mesure qui compte."

step "8) publish genesis/seeds"
$RSH "cd ~/dendra && tr -d '\r' < deploy/testnet/publish_network.sh | bash -s -- $PUBHOST" || die "publish_network failed"
# :8088 served by systemd (Restart=always, survives reboot + ssh detach) — a plain nohup dies with the session.
$RSH 'cat >/etc/systemd/system/dendra-netinfo.service <<UNIT
[Unit]
Description=Dendra network-info http (:8088)
After=network.target
[Service]
WorkingDirectory=/root/dendra/deploy/testnet/published
ExecStart=/usr/bin/python3 -m http.server 8088
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable dendra-netinfo 2>/dev/null; systemctl restart dendra-netinfo; sleep 1; curl -sf -m5 http://localhost:8088/network-info.txt | grep -q CHAIN_ID' || die "network-info :8088 KO (systemd dendra-netinfo)"
echo "  network-info served on :8088 (systemd dendra-netinfo)"

step "9) firewall — PUBLIC EXPOSURE (LAST: every gate above is green)"
# Open the public ports ONLY here. If supply/gates/publish had failed (die), we never reach this point, so a
# non-compliant network is never exposed.
# 80/443 are NOT optional: Caddy needs 80 for the ACME challenge and 443 for the whole HTTPS front
# (api./chat./proof.), and the nodes POST their capacity report over it. Omitting them left HTTPS shut
# on any host where ufw is actually enabled, right after printing "public ports opened".
# 8092 stays CLOSED on purpose: the capacity registry is reached through Caddy (/capacity), not raw.
$RSH 'command -v ufw >/dev/null && { ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 26657/tcp; ufw allow 26656/tcp; ufw allow 8645/tcp; ufw allow 4500/tcp; ufw allow 8651/tcp; ufw allow 8080/tcp; ufw allow 8088/tcp; ufw allow 8090/tcp; ufw allow 8091/tcp; }; true' || true
echo "  public ports opened, HTTPS included (Grafana :3000 / Prometheus :9099 NOT exposed — SSH tunnel)"

# --- GATE FINALE : aucune etape obligatoire n'a le droit d'avoir ete sautee -------------------------
# 3b est ABSENT de cette liste : il est conditionnel (DENDRA_FRESH=1). Toutes les autres sont
# structurelles — si l'une manque, le reseau expose est incomplet et on ne le declare PAS "UP".
assert_steps "0)" "1)" "2)" "3)" "4)" "5)" "6)" "7)" "7b)" "7c)" "8)" "9)"

# --- PREUVE DE BOUT EN BOUT : le chat repond-il VRAIMENT ? -----------------------------------------
# 7b finance la passerelle, mais "finance" != "sert" : un reseau peut se declarer UP pendant que tout
# job renvoie "[Dendra] the network could not process the request". On le VERIFIE ici,
# depuis l'exterieur, avant d'imprimer la banniere de succes. NON bloquant : sans mineur connecte
# (cas normal juste apres un lancement), aucun job ne peut aboutir — c'est un diagnostic, pas une gate.
echo
echo "  verif de bout en bout (la passerelle sert-elle un job ?)..."
_GWKEY="$(grep -E '^DENDRA_API_KEY=' "$ENVF" 2>/dev/null | cut -d= -f2-)"
_PROBE="$(curl -sS --max-time 90 "http://$PUBHOST:8651/v1/chat/completions" \
  -H "Authorization: Bearer $_GWKEY" -H 'Content-Type: application/json' \
  -d '{"model":"dendra-network","messages":[{"role":"user","content":"ping"}]}' 2>/dev/null || true)"
_SERVING=0
case "$_PROBE" in
  *'"job_id"'*) _SERVING=1
                echo "  [OK] un job s'est ouvert et REGLE — la passerelle sert (bloc de preuve 'dendra' present)." ;;
  *'could not process'*) _WHY="aucun job n'aboutit (mineur absent, ou compte de subvention 'gw' vide)"
                         echo "  [!] la passerelle repond mais AUCUN job n'aboutit. Deux causes usuelles :"
                         echo "      (a) aucun mineur connecte -> normal a ce stade, lance le pool (B ci-dessous) ;"
                         echo "      (b) compte de subvention 'gw' vide -> relance ce script (7b est idempotent)." ;;
  '')                    _WHY="la passerelle (:8651) ne repond pas"
                         echo "  [!] aucune reponse de la passerelle (:8651) — verifie 'docker compose logs gateway'." ;;
  *)                     _WHY="reponse inattendue de la passerelle"
                         echo "  [!] reponse inattendue de la passerelle — verifie 'docker compose logs gateway'." ;;
esac

echo
echo "=============================================================================="
# La sonde ci-dessus reste un DIAGNOSTIC, pas une gate (sans mineur connecte, aucun job ne peut
# aboutir : c'est l'etat normal juste apres un lancement, et bloquer ici serait faux). Mais la
# BANNIERE, elle, ne doit pas annoncer "UP" quand la sonde vient de dire le contraire : c'est
# exactement ainsi qu'un reseau s'est declare operationnel en servant des erreurs a chaque job.
# Non-bloquant ET honnete : on continue, en nommant ce qui ne sert pas encore.
if [ "$_SERVING" = 1 ]; then
  echo "  PUBLIC NETWORK UP ET SERVANT (optimistic verification + veto, faucet PoW, The Proof :8090, points :8091)."
else
  echo "  PUBLIC NETWORK DEMARRE mais NE SERT PAS ENCORE — $_WHY."
  echo "  (infra en place ; refais la verif de bout en bout apres avoir lance le pool de mineurs)"
fi
echo "  RPC http://$PUBHOST:26657 | gateway http://$PUBHOST:8651/v1 | faucet :4500 | chat :8080"
echo "  network-info : http://$PUBHOST:8088/network-info.txt (genesis + SHA256 + seeds)"
echo
echo "  NOT ANNOUNCED YET — remaining sequence (in order):"
echo "  A) 2nd VALIDATOR (another machine, keep total stake distribution <2/3) + anchor both VRF keys:"
echo "     deploy/join.sh --validator   (then create-validator + deploy/testnet/anchor_vrf_key.sh)"
echo "  B) HETEROGENEOUS JUDGES (>=2 distinct judge models):"
echo "     deploy/launch/judges5_public.sh   (RAM-based model pick; override MOE_MODEL / MOE_COUNT)"
echo "  C) VALIDATION RUN: tr -d '\r' < dendra/onchain-staging/dendra_c3_validation.sh | bash -s -- $PUBHOST"
echo "     -> triplet artifact (0/0/0) -> validation review -> publish + announce"
echo "=============================================================================="
