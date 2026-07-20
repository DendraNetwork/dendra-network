#!/usr/bin/env bash
# anchor_vrf_key.sh — ancre la cle VRF d'un validateur on-chain (E4 / bootstrap VRF decentralisee, internal audit 2026-06-26).
#
# A lancer par l'OPERATEUR et CHAQUE JOINER, une fois leur noeud dans le consensus (catching_up=false). Genere une
# cle VRF (si absente), l'ecrit dans le home (chargee au prochain (re)start via DENDRA_VRF_KEY_FILE) et ANCRE la
# pubkey on-chain avec proof-of-possession (register-validator-vrf-key, VE-02). Idempotent.
#
# POURQUOI c'est OBLIGATOIRE en testnet RECOMPENSE : sans ancrage, un validateur NE CONTRIBUE PAS a la graine VRF
# (il ne compte pas vers committee_min_vrf_contributors). Tant que les contributeurs < plancher, committeeBaseSeed
# ALERTE (log SECURITE) + repli legacy = anti-grinding INACTIF. Chaque validateur DOIT donc le lancer pour que les
# comites soient tires par VRF decentralisee.
#
# Usage (WSL) :
#   HOME_DIR=~/.dendra CHAIN_ID=dendra NODE=tcp://127.0.0.1:26657 VAL_KEY=validator \
#     tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | bash
set -uo pipefail
D="${DENDRAD:-$HOME/go/bin/dendrad}"; VRF="${DENDRAVRF:-$HOME/go/bin/dendra-vrf}"
HOME_DIR="${HOME_DIR:-$HOME/.dendra}"
CHAIN_ID="${CHAIN_ID:-dendra}"
NODE="${NODE:-tcp://127.0.0.1:26657}"
VAL_KEY="${VAL_KEY:-validator}"
KB="--keyring-backend test --home $HOME_DIR"
KEYFILE="$HOME_DIR/config/vrf_key"

command -v "$D"   >/dev/null 2>&1 || D=dendrad
command -v "$VRF" >/dev/null 2>&1 || VRF=dendra-vrf
command -v "$VRF" >/dev/null 2>&1 || { echo "[vrf] FATAL: dendra-vrf introuvable (depuis ~/dendra : go build -o ~/go/bin/dendra-vrf ./cmd/dendra-vrf)"; exit 1; }

A=$("$D" keys show "$VAL_KEY" -a $KB 2>/dev/null) || { echo "[vrf] FATAL: cle '$VAL_KEY' absente du keyring ($HOME_DIR)"; exit 1; }
echo "[vrf] validateur (operateur) = $A"

# 1) cle VRF : reutiliser si presente (idempotent), sinon generer
# Le shebang est bash, donc `read ... < <(...)` FONCTIONNAIT a l'execution normale. Mais ce script est
# aussi PIPE dans d'autres shells (`| bash -s`, `docker exec -T node bash -s`, et les images minimales
# ou /bin/sh = dash), et la substitution de processus y casse en « redirection unexpected » — sur le
# chemin GENERATION uniquement, c'est-a-dire exactement le remede documente en cas de cle corrompue.
# Reecrit en POSIX : meme comportement, plus de dependance au shell qui l'execute.
if [ -s "$KEYFILE" ]; then
  VSK=$(cat "$KEYFILE"); VPK=$("$VRF" pubkey "$VSK")
  echo "[vrf] cle VRF existante reutilisee (pub $(printf '%.16s' "$VPK")...)"
else
  _KG=$("$VRF" keygen) || { echo "[vrf] FATAL: 'dendra-vrf keygen' a echoue"; exit 1; }
  # keygen ecrit "<sk>\t<pk>" — TABULATION, pas espace (cmd/dendra-vrf/main.go: Printf "%s\t%s\n").
  # Le `read -r VSK VPK` d'origine marchait parce qu'il decoupe sur l'IFS COMPLET (espace ET tab). Une
  # premiere reecriture "POSIX" a utilise ${_KG%% *}/${_KG##* }, qui ne coupent QUE sur l'espace : sur une
  # entree tabulee les deux rendent la chaine entiere. `cut -f` a la tabulation par defaut, donc il colle
  # au format du producteur — et c'est deja ce que fait docker/entrypoint-chain.sh:169.
  VSK=$(printf '%s\n' "$_KG" | cut -f1)
  VPK=$(printf '%s\n' "$_KG" | cut -f2)
  [ -n "$VSK" ] && [ -n "$VPK" ] && [ "$VSK" != "$VPK" ] \
    || { echo "[vrf] FATAL: sortie de keygen inattendue (attendu '<sk> <pk>') : $_KG"; exit 1; }
  echo "$VSK" > "$KEYFILE"; chmod 600 "$KEYFILE"
  echo "[vrf] cle VRF generee -> $KEYFILE (pub $(printf '%.16s' "$VPK")...)"
fi

# 2) proof-of-possession (VE-02) sur dendra/vrf-pop/<operateur>
POP=$("$VRF" prove "$VSK" "dendra/vrf-pop/$A")

# 2a) AUTO-VERIFICATION LOCALE avant de depenser une tx. Le handler on-chain rejette une PoP invalide
# avec ErrUnauthorized (msg_server_register_validator_vrf.go:41) — autant le savoir ICI, gratuitement,
# et avec un message qui dit QUOI est casse, plutot que de lire un "code":4 nu.
if ! "$VRF" verify "$VPK" "dendra/vrf-pop/$A" "$POP" >/dev/null 2>&1; then
  echo "[vrf] FATAL: la proof-of-possession ne se verifie meme pas LOCALEMENT."
  echo "       La cle privee de $KEYFILE ne correspond pas a la pubkey derivee, ou le fichier est corrompu."
  echo "       Remede : mets la cle de cote et laisse-la se regenerer —"
  echo "         mv $KEYFILE $KEYFILE.bad && <relance ce script>"
  echo "       (ancrer une NOUVELLE cle est sans danger : le handler ecrase, c'est idempotent.)"
  exit 1
fi
echo "[vrf] proof-of-possession verifiee localement (pub $(printf '%.16s' "$VPK")...)"

# 2a-bis) PRE-VOL DU COMPTE. Sans lui, la chaine rejette l'ancrage avec
#   "signature verification failed; please verify account number (0) and chain-id (dendra)"
# alors que la PoP etait valide. Signer avec account_number=0 signifie que la CLI n'a PAS pu lire le
# compte on-chain avant de signer — le probleme n'est donc pas la VRF, c'est l'acces au compte. Sans ce
# pre-vol on impute a tort l'echec a la clé VRF et on part debugger le mauvais sous-systeme.
ACC=$("$D" query auth account "$A" -o json --node "$NODE" 2>&1)
# On SUPPRIME les espaces avant d'extraire : la sortie est du JSON INDENTE, donc le champ s'ecrit
# `"account_number": "12"` — avec une espace apres le deux-points. La premiere version de cette garde
# exigeait `"account_number":"12"` sans espace, et refusait donc un compte parfaitement lisible en
# affichant « ILLISIBLE » juste au-dessus du compte qu'elle venait d'imprimer. Une garde qui se trompe
# coute plus cher que pas de garde : elle bloque le chemin correct ET oriente le diagnostic vers un
# faux coupable.
ACCNUM=$(printf '%s' "$ACC" | tr -d ' \t' | grep -oE '"account_number":"?[0-9]+' | head -1 | grep -oE '[0-9]+$')
if [ -z "$ACCNUM" ]; then
  if printf '%s' "$ACC" | grep -q '"address"'; then
    # La chaine a REPONDU (le compte existe) : c'est NOTRE extraction qui a echoue, pas le compte.
    echo "[vrf] ATTENTION: compte trouve mais 'account_number' non extrait — format de sortie inattendu."
    echo "       On NE bloque PAS pour autant : la tx ci-dessous dira la verite (code + raw_log)."
    printf '%s\n' "$ACC" | head -8 | sed 's/^/       | /'
  else
    echo "[vrf] FATAL: le compte $A est introuvable sur $NODE — signer produirait account_number=0"
    echo "       et un rejet 'signature verification failed'. Reponse de la chaine :"
    printf '%s\n' "$ACC" | head -5 | sed 's/^/       | /'
    echo "       Piste principale : le compte n'a jamais recu de fonds sur CETTE chaine (un compte"
    echo "       n'existe qu'apres un premier credit) ; ou --node pointe une autre chaine."
    exit 1
  fi
else
  echo "[vrf] compte lisible on-chain (account_number=$ACCNUM)"
fi

echo "[vrf] ancrage on-chain (register-validator-vrf-key)..."
# On garde la sortie ENTIERE. L'ancienne version la passait a `grep ... || true` : un rejet de la chaine
# ressortait en "code":4 sans le raw_log, et le script imprimait "OK" juste apres. Un ancrage refuse
# affiche donc "OK" alors que le validateur ne contribue a RIEN.
TXOUT=$("$D" tx jobs register-validator-vrf-key "$VPK" "$POP" --from "$VAL_KEY" $KB --chain-id "$CHAIN_ID" --node "$NODE" --yes -o json 2>&1)
CODE=$(printf '%s' "$TXOUT" | grep -oE '"code":[0-9]+' | head -1 | cut -d: -f2)
TXH=$(printf '%s' "$TXOUT" | grep -oE '"txhash":"[0-9A-Fa-f]+"' | head -1 | cut -d'"' -f4)
if [ "${CODE:-1}" != "0" ]; then
  echo "[vrf] ECHEC de l'ancrage — code=${CODE:-?} txhash=${TXH:-none}"
  echo "[vrf] message de la chaine :"
  printf '%s\n' "$TXOUT" | grep -oE '"raw_log":"[^"]*"' | head -1 | sed 's/^/       /'
  printf '%s\n' "$TXOUT" | head -20 | sed 's/^/       | /'
  echo "[vrf] Le validateur NE contribue PAS a la graine tant que ceci echoue. Ne pas ignorer."
  exit 1
fi
echo "[vrf] tx acceptee (txhash=$TXH)"

# 2b) CONFIRMATION REELLE : attendre que la tx soit COMMITTEE et lire SON code.
# Le code renvoye au broadcast ci-dessus ne dit qu'une chose : la tx est entree dans le MEMPOOL. Une tx
# acceptee la peut echouer a l'execution (fonds insuffisants, etat change entre-temps) — c'est exactement
# ce qui arrive a un virement annonce reussi et jamais arrive. Seul `query tx <hash>`
# donne le resultat d'execution.
# NB : on ne peut PAS relire la cle ancree — `ValidatorVrfPubkey` (keeper.go:41) est ecrite par le
# handler mais n'est exposee par AUCUNE requete (absente de l'autocli). Une premiere version de ce
# script interrogeait `query jobs validator-vrf-key`, qui n'existe pas : la commande echouait, le
# resultat vide etait lu comme « la cle n'est pas la », et le script criait au loup sur un ancrage sain.
CONFIRMED=0
if [ -n "$TXH" ]; then
  echo "[vrf] attente de l'inclusion dans un bloc..."
  i=0
  while [ $i -lt 12 ]; do
    i=$((i+1)); sleep 3
    QT=$("$D" query tx "$TXH" -o json --node "$NODE" 2>/dev/null) || continue
    printf '%s' "$QT" | grep -q '"txhash"' || continue
    QCODE=$(printf '%s' "$QT" | tr -d ' \t' | grep -oE '"code":[0-9]+' | head -1 | cut -d: -f2)
    if [ "${QCODE:-1}" = "0" ]; then
      echo "[vrf] CONFIRME : tx EXECUTEE avec succes dans un bloc (code=0)."
      CONFIRMED=1
    else
      echo "[vrf] ECHEC A L'EXECUTION — la tx etait dans le mempool mais a ete rejetee au bloc (code=$QCODE) :"
      printf '%s\n' "$QT" | grep -oE '"raw_log":"[^"]*"' | head -1 | sed 's/^/       /'
      echo "[vrf] Le validateur NE contribue PAS a la graine. Ne pas ignorer."
      exit 1
    fi
    break
  done
fi
[ "$CONFIRMED" = 1 ] || echo "[vrf] ATTENTION : impossible de confirmer l'inclusion de $TXH en 36 s — verifie a la main :
       $D query tx $TXH --node $NODE"

echo
echo "[vrf] IMPORTANT : (re)demarre ton noeud avec  DENDRA_VRF_KEY_FILE=$KEYFILE  pour qu'il SIGNE ses"
echo "     vote-extensions (sinon la cle est ancree mais le noeud ne fournit aucune preuve = ne contribue pas)."
echo "     Verifie ensuite :  $D query jobs committee-seed-health --node $NODE   (latest_contributors doit monter)"
