#!/usr/bin/env bash
# entrypoint-chain.sh - noeud Dendra TESTNET persistant.
#   dendrad start NATIF (fini ignite chain serve : plus de recompilation ni de clefs aleatoires).
#   1er boot   : init + clefs stables (validator/bob/communaute/equipe/reserve/gw) + comptes genesis
#                + params OPTIMISTES (ADR-025) + registre modeles (llama3.1:8b DEFAUT [P2 GO proprio 2026-07-04]
#                + mistral-nemo + nomic) + gentx + collect.
#   N-ieme boot: genesis present dans le volume -> etat reutilise (PERSISTANT).
# Offre FIXE 10 M udndr preservee. gw = 0 au genesis (finance au faucet, comme les mineurs).
set -e
export PATH="$PATH:/go/bin:/usr/local/go/bin"
BIN=dendrad
HOME_DIR="${DENDRA_HOME:-/root/.dendra}"
CHAIN_ID="${DENDRA_CHAIN_ID:-dendra}"
DENOM=udndr
KB="--keyring-backend test --home $HOME_DIR"
GEN="$HOME_DIR/config/genesis.json"

command -v "$BIN" >/dev/null 2>&1 || { echo "[chain] FATAL: dendrad introuvable"; exit 1; }
command -v jq    >/dev/null 2>&1 || { echo "[chain] FATAL: jq introuvable (ajouter au Dockerfile)"; exit 1; }
mkdir -p "$HOME_DIR"

if [ ! -f "$GEN" ]; then
  echo "[chain] ===== PREMIER BOOT : construction du genesis testnet ($CHAIN_ID) ====="
  rm -rf "$HOME_DIR/config" "$HOME_DIR/data" 2>/dev/null || true
  $BIN init "${DENDRA_MONIKER:-dendra-testnet}" --chain-id "$CHAIN_ID" --default-denom "$DENOM" --home "$HOME_DIR" >/dev/null 2>&1

  for k in validator bob communaute equipe reserve gw; do
    if ! $BIN keys show "$k" $KB >/dev/null 2>&1; then
      $BIN keys add "$k" $KB --output json >> "$HOME_DIR/keys-backup.jsonl" 2>/dev/null
    fi
  done
  VAL=$($BIN keys show validator -a $KB)
  BOB=$($BIN keys show bob -a $KB)
  COM=$($BIN keys show communaute -a $KB)
  EQ=$($BIN keys show equipe -a $KB)
  RES=$($BIN keys show reserve -a $KB)
  GW=$($BIN keys show gw -a $KB)
  echo "[chain] validator=$VAL"
  echo "[chain] bob(faucet)=$BOB"
  echo "[chain] gw(passerelle)=$GW (finance au faucet)"
  chmod 600 "$HOME_DIR/keys-backup.jsonl" 2>/dev/null || true

  $BIN genesis add-genesis-account "$VAL" 2700000000000$DENOM $KB
  $BIN genesis add-genesis-account "$BOB" 100000000000$DENOM $KB
  $BIN genesis add-genesis-account "$COM" 3400000000000$DENOM $KB
  $BIN genesis add-genesis-account "$EQ" 500000000000$DENOM $KB
  $BIN genesis add-genesis-account "$RES" 3300000000000$DENOM $KB

  # G2 (internal audit 2026-06-22) — GENESIS UNIFIÉ : ce bloc jobs.params DOIT rester aligné sur
  # chain/config.optimistic.yml + docs/RUNBOOK-PROD-ACTIVATION (gates N1/N2/veto). Avant, ce stack de LANCEMENT
  # activait l'optimiste SANS hold_bps (N1), SANS audit_min_quorum (veto N=5) et avec timeout=30 (sous N2) -> il
  # CONTOURNAIT les gates du mode optimiste. Reglage : hold_bps=10000 (N1) + audit_min_quorum=4 + timeout=120 (N2-safe).
  # NB : seuls N1/N2/veto sont des params de SÉCURITÉ a aligner. audit_sample_bps=5000 (testnet, 50% audite) differe
  # VOLONTAIREMENT du 10000 de config.optimistic.yml (demo = 100% audite, slash visible) — ce n'est pas un gate.
  tmp=$(mktemp)
  # audit_sample_bps PILOTABLE par env (defaut 5000 = testnet 50%, inchange). Pour une DEMO/GOLD de slash
  # deterministe (le primaire est tire par HASH uniforme, pas par le stake -> a 50% le tricheur passe souvent
  # entre les gouttes), poser DENDRA_AUDIT_SAMPLE_BPS=10000 (100% audite) : tout job du tricheur est juge.
  # 100% audite est un test PLUS severe des faux-slash (tous les honnetes passent le juge). N'est PAS un gate.
  ASB="${DENDRA_AUDIT_SAMPLE_BPS:-5000}"
  # audit_resolve_timeout PILOTABLE (2026-07-04, finding run muet-HÉTÉRO) : 120 blocs ~2 min suffisent à un
  # comité HOMOGÈNE chaud, mais 2 modèles-juges co-résidents (CPU partagé, chargements alternés) postent leurs
  # verdicts "0" APRÈS le timeout -> le muet se résout en no-quorum (silence_slash -EV, mais pas de slash DUR).
  # Banc hétéro : DENDRA_AUDIT_RESOLVE_TIMEOUT=360. Défaut 120 INCHANGÉ. (Validate exige > dispute_window=10.)
  ARTO="${DENDRA_AUDIT_RESOLVE_TIMEOUT:-120}"
  # BOND DE DISPUTE. N'etait pose NULLE PART -> defaut proto 0 -> contester etait GRATUIT, et la
  # confiscation anti-grief (dispute infondee -> bond en Tresorerie) confisquait zero : l'instrument
  # existait mais n'avait aucun mordant. Ancrage retenu = `min_stake` (50000 udndr = 0,05 DNDR),
  # soit ~11 prix de job (4500) et 1/200 d'une goutte de faucet (10 DNDR) : assez cher pour que le
  # grief coute, assez accessible pour qu'un utilisateur honnete puisse contester. Il lui est RENDU
  # si un comite a ete convoque sans repondre, et rembourse+recompense si sa dispute est fondee.
  # ⚠️ Parametre ECONOMIQUE -> valeur a arbitrer par le internal audit ; override : DENDRA_DISPUTE_BOND.
  DBOND="${DENDRA_DISPUTE_BOND:-50000}"
  # F3 (2026-07-10) : x/mint DE-ENREGISTRE de l'app (zero mint STRUCTUREL) -> on ne POSE plus de
  # section mint (un module non enregistre avec section genesis fait echouer le boot) ; on PURGE
  # au contraire tout residu (del) au cas ou un vieux genesis/volume en porterait une.
  jq --arg asb "$ASB" --arg arto "$ARTO" --arg dbond "$DBOND" '
    del(.app_state.mint)
    | .app_state.jobs.params.verification_mode = "1"
    | .app_state.jobs.params.audit_sample_bps = $asb
    | .app_state.jobs.params.min_stake = "50000"
    | .app_state.jobs.params.slash_leak_bps = "8000"
    | .app_state.jobs.params.fee_burn_bps = "500"
    | .app_state.jobs.params.dispute_window = "10"
    | .app_state.jobs.params.dispute_bond = $dbond
    | .app_state.jobs.params.audit_resolve_timeout = $arto
    | .app_state.jobs.params.hold_bps = "10000"
    | .app_state.jobs.params.audit_min_quorum = "4"
    | .app_state.jobs.params.silence_slash_bps = "2000"
    | .app_state.jobs.params.committee_seed_source = "1"
    | .app_state.jobs.params.avail_require_demand = true
    | .app_state.emission.params.reserve_release_bps = "2"
    | .app_state.emission.params.work_split_bps = "5000"
    | .app_state.emission.params.avail_split_bps = "2000"
    | .app_state.emission.params.work_gate_bps = "15000"
    | .app_state.emission.params.epoch_blocks = "300"
    | .app_state.modelregistry.models = [
        {"id":"llama3.1:8b-instruct-q4_K_M","weights_sha256":"","quant":"q4_K_M","engine":"ollama","hw_class":"consumer-gpu","active":true,"hf_repo":"","hf_revision":""},
        {"id":"mistral-nemo","weights_sha256":"","quant":"","engine":"ollama","hw_class":"consumer-gpu","active":true,"hf_repo":"","hf_revision":""},
        {"id":"nomic-embed-text","weights_sha256":"","quant":"","engine":"ollama","hw_class":"consumer-gpu","active":true,"hf_repo":"","hf_revision":""}
      ]
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: patch jq du genesis a echoue"; exit 1; }

  # E4 / BOOTSTRAP VRF DÉCENTRALISÉE (internal audit 2026-06-26) — couplé à committee_seed_source=1 ci-dessus :
  #  - committee_min_vrf_contributors = PLANCHER (⌈2N/3⌉ du set PRÉVU ; défaut 2, « à relever avec N »). Une graine
  #    à < N contributeurs -> committeeBaseSeed ALERTE + repli legacy (jamais de halte, anti-régression VISIBLE).
  #  - vote_extensions_enable_height = param CONSENSUS (hors app_state) qui active les vote-extensions ABCI++ (la VRF
  #    décentralisée produit la graine). SANS clés VRF ancrées : extensions vides -> alerte + repli legacy (non-bloquant).
  #  Chaque validateur ancre sa clé : deploy/testnet/anchor_vrf_key.sh (puis (re)start avec DENDRA_VRF_KEY_FILE).
  MINVRF="${DENDRA_MIN_VRF_CONTRIB:-2}"; VEH="${DENDRA_VE_ENABLE_HEIGHT:-20}"
  tmp=$(mktemp)
  jq --arg m "$MINVRF" --arg h "$VEH" '
      .app_state.jobs.params.committee_min_vrf_contributors = $m
    | (if has("consensus") then .consensus.params.abci.vote_extensions_enable_height = $h
       else .consensus_params.abci.vote_extensions_enable_height = $h end)
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" \
    || { echo "[chain] ATTENTION: patch bootstrap VRF (min_vrf/vote_extensions) a echoue -> committee_seed_source=1 ALERTERA (repli legacy visible, non bloquant) ; verifier la structure consensus du genesis."; rm -f "$tmp"; }
  echo "[chain] bootstrap VRF : committee_seed_source=1, committee_min_vrf_contributors=$MINVRF, vote_extensions_enable_height=$VEH (chaque validateur doit ancrer sa cle VRF : deploy/testnet/anchor_vrf_key.sh)"

  # ADR-022 PLEIN (internal audit 2026-06-27 ; SLIDING-WINDOW lot scaling 2026-07-01) — disponibilite SLASHABLE (liveness).
  # DORMANT par defaut (DENDRA_AVAIL_SLASH != 1 -> RIEN pose, avail slash OFF, l'interim vrf_pubkey-obligatoire
  # tient le farm). ARMING = DENDRA_AVAIL_SLASH=1, UNIQUEMENT apres la PREUVE LIVE de non-faux-positif au
  # soft-launch (honnete survit une coupure breve + p_miss reel >=~99% uptime). Pose ATOMIQUEMENT epoque=300 +
  # les 5 params slash calibres pour le SLIDING (k=5/W=25 grand-public : bleed honnete @99% ~0,5%/an, chronique
  # exacte 16%, coupure survecue 20 min ; bench-results/avail-slash-calibration-sliding.json — le k=4/W=20
  # TUMBLING est CADUC : l'algo keeper a change, PENDING validation internal audit) -> impossible d'armer avec un
  # mauvais epoch (la prise dure de internal audit). Surchargeable par env. Params.Validate exige cette machinerie
  # quand slash_bps>0 (dont W<=64, capacite du bitmask).
  if [ "${DENDRA_AVAIL_SLASH:-0}" = "1" ]; then
    AEB="${DENDRA_AVAIL_EPOCH_BLOCKS:-300}"; ADL="${DENDRA_AVAIL_DEADLINE_BLOCKS:-150}"
    AFK="${DENDRA_AVAIL_FAIL_K:-5}"; AFW="${DENDRA_AVAIL_FAIL_WINDOW:-25}"
    ASB="${DENDRA_AVAIL_SLASH_BPS:-500}"; ASM="${DENDRA_AVAIL_SLASH_MAX:-0}"
    # garde-fou OPÉRATEUR (message clair AVANT le jq ; sinon validate-genesis rejette quand même = fail-closed, mais
    # trace obscure). Reproduit les préconditions de params.Validate() pour une surcharge env fat-fingerée.
    # internal audit 2026-06-27 (residu) : la garde rejette AUSSI epoch < 2*deadline -> un override DENDRA_AVAIL_EPOCH_BLOCKS=16
    # (qui passait "16>0") ne peut plus casser la calibration en SILENCE (l'echeance doit rester <= moitie d'epoque).
    # SLIDING 2026-07-01 : + rejette window > 64 (capacite du bitmask ; sinon Params.Validate le rejettera de toute facon).
    if ! { [ "$AFK" -gt 0 ] && [ "$AFW" -ge "$AFK" ] && [ "$AFW" -le 64 ] && [ "$ADL" -gt 0 ] && [ "$AEB" -ge $((2 * ADL)) ] && [ "$ASB" -le 10000 ]; } 2>/dev/null; then
      echo "[chain] FATAL: arming ADR-022 incoherent (exige avail_fail_k>0, avail_fail_k<=avail_fail_window<=64 [bitmask sliding], avail_deadline_blocks>0, avail_epoch_blocks>=2*avail_deadline_blocks [echeance <= moitie d'epoque, calibration], avail_slash_bps<=10000). Recu epoch=$AEB deadline=$ADL k=$AFK window=$AFW slash=$ASB max=$ASM"; exit 1
    fi
    tmp=$(mktemp)
    jq --arg eb "$AEB" --arg dl "$ADL" --arg k "$AFK" --arg w "$AFW" --arg s "$ASB" --arg m "$ASM" '
        .app_state.jobs.params.avail_epoch_blocks = $eb
      | .app_state.jobs.params.avail_deadline_blocks = $dl
      | .app_state.jobs.params.avail_fail_k = $k
      | .app_state.jobs.params.avail_fail_window = $w
      | .app_state.jobs.params.avail_slash_bps = $s
      | .app_state.jobs.params.avail_slash_max = $m
    ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: arming ADR-022 (avail slash) jq a echoue"; exit 1; }
    echo "[chain] ADR-022 ARME: avail slash ON (epoch=$AEB deadline=$ADL k=$AFK/$AFW slash=${ASB}bps max=$ASM) -- preuve live faite ?"
  else
    echo "[chain] ADR-022 avail slash DORMANT (DENDRA_AVAIL_SLASH!=1) -- armer SEULEMENT apres preuve live non-faux-positif (soft-launch)"
  fi

  $BIN genesis gentx validator 1000000000000$DENOM --chain-id "$CHAIN_ID" $KB >/dev/null 2>&1
  $BIN genesis collect-gentxs --home "$HOME_DIR" >/dev/null 2>&1
  if $BIN genesis validate-genesis --home "$HOME_DIR" >/dev/null 2>&1; then
    echo "[chain] genesis VALIDE."
  else
    # internal audit 2026-06-26 : FATAL au niveau OUTILLAGE (echec propre) plutot qu'un crash au boot. Capte notamment
    # un genesis violant l'invariant #8 (params +EV) via GenesisState.Validate().
    echo "[chain] FATAL: validate-genesis a echoue (genesis invalide ; ex. invariant #8 / params +EV) :" >&2
    $BIN genesis validate-genesis --home "$HOME_DIR" >&2 || true
    exit 1
  fi
  echo "[chain] ===== genesis testnet pret (fige dans le volume) ====="
else
  echo "[chain] genesis existant -> etat persistant reutilise."
fi

# E4 / BOOTSTRAP VRF (internal audit 2026-06-26) — clé VRF du validateur AUTO-gérée :
#  - keygen si absente -> écrite dans le home (dendrad la charge via DENDRA_VRF_KEY_FILE et SIGNE ses
#    vote-extensions dès le démarrage, AUCUN restart) ;
#  - ancrage on-chain de la pubkey EN ARRIÈRE-PLAN (idempotent, best-effort) -> le validateur COMPTE dans
#    l'agrégation VRF (contribue à `committee_min_vrf_contributors`). Échec = grinding rouge (visible), jamais bloquant.
VRFKEY="$HOME_DIR/config/vrf_key"
if command -v dendra-vrf >/dev/null 2>&1; then
  [ -s "$VRFKEY" ] || { dendra-vrf keygen | cut -f1 > "$VRFKEY" && chmod 600 "$VRFKEY" && echo "[chain] cle VRF validateur generee ($VRFKEY)"; }
  (
    for _ in $(seq 1 90); do curl -s http://127.0.0.1:26657/status 2>/dev/null | grep -q latest_block_height && break; sleep 2; done
    VSK=$(cat "$VRFKEY" 2>/dev/null); VPK=$(dendra-vrf pubkey "$VSK" 2>/dev/null); A=$($BIN keys show validator -a $KB 2>/dev/null)
    POP=$(dendra-vrf prove "$VSK" "dendra/vrf-pop/$A" 2>/dev/null)
    # fix 2026-07-17 (vu au launch VPS) : le one-shot best-effort ratait EN SILENCE (race du 1er boot) et le
    # VPS ne contribuait pas a la graine (grinding ROUGE) jusqu'a un anchor manuel. Retry borne ; succes =
    # "code":0 DANS la reponse JSON (l'exit 0 du binaire ne suffit pas : un tx broadcaste peut porter code!=0).
    # ⚠️ `"code":0` dans la reponse du broadcast = la tx est entree dans le MEMPOOL, PAS qu'elle s'est
    # EXECUTEE. Une tx acceptee la peut echouer a l'execution (numero de compte illisible au moment de
    # signer, fonds, etat change entre-temps) et l'ancrage n'a alors JAMAIS eu lieu -- pendant que ce
    # script imprime "ancree". Consequence concrete : le validateur ne contribue pas a la graine, donc
    # l'anti-grinding est INACTIF, en silence. Seul `query tx <hash>` donne le resultat d'EXECUTION.
    # (Le durcissement existait deja dans deploy/testnet/anchor_vrf_key.sh ; il n'avait pas ete propage ici.)
    ok=0
    for _ in $(seq 1 10); do
      OUT=$($BIN tx jobs register-validator-vrf-key "$VPK" "$POP" --from validator $KB --chain-id "$CHAIN_ID" --node tcp://127.0.0.1:26657 --yes -o json 2>/dev/null)
      HASH=$(printf '%s' "$OUT" | tr -d ' \t' | grep -o '"txhash":"[A-Fa-f0-9]*"' | head -1 | cut -d'"' -f4)
      if [ -n "$HASH" ]; then
        # laisse le bloc s'inclure, puis LIS le resultat d'execution
        for _ in $(seq 1 12); do
          sleep 5
          QOUT=$($BIN query tx "$HASH" --node tcp://127.0.0.1:26657 -o json 2>/dev/null)
          QCODE=$(printf '%s' "$QOUT" | tr -d ' \t' | grep -o '"code":[0-9]*' | head -1 | cut -d: -f2)
          [ -n "$QCODE" ] && break
        done
        [ "$QCODE" = "0" ] && { ok=1; break; }
        [ -n "$QCODE" ] && echo "[chain] ERR: ancrage VRF REJETE a l'execution (code=$QCODE) -> le validateur ne contribue PAS a la graine"
      fi
      sleep 30
    done
    if [ "$ok" = 1 ]; then echo "[chain] pubkey VRF du validateur ancree on-chain (auto, code:0 verifie)"
    else echo "[chain] ERR: ancrage VRF auto NON confirme apres 10 essais -> manuel : deploy/testnet/anchor_vrf_key.sh (le grinding restera ROUGE sinon)"; fi
  ) &
else
  echo "[chain] (info) dendra-vrf absent de l'image -> pas d'ancrage VRF auto (rebuild l'image chain pour l'activer)"
fi

# CORS: let browser wallets / dApps reach the public RPC + REST (public testnet). Idempotent per boot.
CFGTOML="$HOME_DIR/config/config.toml"
[ -f "$CFGTOML" ] && sed -i 's|^cors_allowed_origins = .*|cors_allowed_origins = ["*"]|' "$CFGTOML"

# SNAPSHOTS — obligatoires dès qu'un changement CONSENSUS-BREAKING a eu lieu.
# Sans snapshot, un nouveau nœud REJOUE l'historique depuis le genesis. Or le binaire courant écrit
# `audit_committee/*` (ADR-032) alors que l'ancien ne l'écrivait pas : en rejouant un bloc ANTÉRIEUR à
# la montée où un audit avait été tiré, il recalcule un AppHash différent de celui déjà signé dans
# l'en-tête → panic au replay. Il n'existe AUCUNE valeur de garde de hauteur qui rattrape ça, parce que
# l'historique contient réellement les DEUX comportements.
# Le remède n'est donc pas de rejouer correctement, c'est de NE PAS REJOUER : on sert des snapshots, et
# le joiner state-sync à une hauteur POSTÉRIEURE à la montée. Coût : quelques Mo tous les 500 blocs.
APPTOML="$HOME_DIR/config/app.toml"
if [ -f "$APPTOML" ]; then
  sed -i 's|^snapshot-interval = .*|snapshot-interval = 500|' "$APPTOML"
  sed -i 's|^snapshot-keep-recent = .*|snapshot-keep-recent = 5|' "$APPTOML"
  grep -q '^snapshot-interval' "$APPTOML" || printf '\n[state-sync]\nsnapshot-interval = 500\nsnapshot-keep-recent = 5\n' >> "$APPTOML"
  echo "[chain] snapshots ACTIVES (tous les 500 blocs, 5 conserves) -> les joiners peuvent state-sync"
  echo "        au lieu de rejouer un historique qui contient un changement consensus-breaking."
fi

echo "[chain] starting dendrad (RPC 0.0.0.0:26657, REST 1317 with CORS)"
exec $BIN start --home "$HOME_DIR" \
  --rpc.laddr tcp://0.0.0.0:26657 \
  --api.enable --api.address tcp://0.0.0.0:1317 --api.enabled-unsafe-cors \
  --grpc.address 0.0.0.0:9090 \
  --minimum-gas-prices 0$DENOM
