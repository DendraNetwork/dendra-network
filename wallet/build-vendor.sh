#!/usr/bin/env bash
# Construit le bundle CosmJS servi par le wallet — LOCALEMENT, sans CDN.
#
# POURQUOI. `wallet/web/index.html` charge aujourd'hui CosmJS depuis jsDelivr par `import()`
# dynamique, sur la page meme ou l'utilisateur saisit sa phrase mnemonique. Deux problemes :
#
#   1. AUCUNE verification d'integrite n'est possible. L'attribut `integrity` (SRI) n'existe pas
#      sur un `import()` dynamique, et le bundle `/+esm` de jsDelivr reimporte lui-meme ses
#      dependances depuis le CDN. Copier deux fichiers a cote ne fermerait donc rien : il faut un
#      bundle AUTONOME. Si le CDN sert un jour du code hostile, il s'execute sur la page a seeds.
#
#   2. La version epinglee (0.32.4) est DEPRECIEE POUR RAISON DE SECURITE en amont : elle utilise
#      `elliptic`, dont les mainteneurs ecrivent « several security-relevant bugs [...] private
#      keys might still be at risk ». Corrige a partir de 0.34.0, qui remplace la bibliotheque
#      cryptographique. La 0.39 retenue ici elimine `elliptic` au profit de `@noble/curves`,
#      `@noble/hashes` et `@scure/bip39`.
#
# VERIFIE AVANT D'ECRIRE CE SCRIPT (1re main, pas suppose) :
#   - les 4 symboles utilises par le wallet existent en 0.39 ;
#   - la DERIVATION EST IDENTIQUE : le meme mnemonique donne la meme adresse en 0.32.4 et en 0.39,
#     donc une montee de version ne fait perdre l'acces a AUCUN compte existant ;
#   - le bundle produit ne contient plus aucun import distant.
#
# CE QUI RESTE A PROUVER, ET QUE CE SCRIPT NE PROUVE PAS : le comportement en NAVIGATEUR. Node
# n'en est pas un substitut — tenter de l'emuler en retirant `Buffer` casse `undici`, un composant
# interne de Node, et non le bundle. D'ou l'etape 3 ci-dessous, qui est manuelle et obligatoire.
#
# USAGE :   bash wallet/build-vendor.sh
set -euo pipefail

VERSION="0.39.0"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/wallet/web/vendor"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v npm >/dev/null || { echo "[X] npm requis"; exit 1; }
mkdir -p "$OUT"

echo "## 1) dependances (isolees dans $WORK, rien n'est installe dans le depot)"
# REPRODUCTIBILITE. Epingler `@cosmjs/*@0.39.0` NE SUFFIT PAS : npm resout les dependances
# TRANSITIVES au moment de l'installation, si bien que deux builds du meme jour peuvent differer
# de quelques octets — et donc de SHA256. Un condensat publie ne vaut alors rien : personne ne peut
# le reproduire, et « verifiez le hash » devient une formule creuse. Le lockfile fige l'arbre
# ENTIER ; il est committe a cote du script pour que la verification tierce soit reellement
# possible. C'est la difference entre publier une recette et publier une preuve.
LOCK="$ROOT/wallet/vendor-build"
cd "$WORK"
if [ -f "$LOCK/package-lock.json" ] && [ -f "$LOCK/package.json" ]; then
  cp "$LOCK/package.json" "$LOCK/package-lock.json" .
  npm ci --silent --no-audit --no-fund >/dev/null
  echo "   [OK] arbre de dependances FIGE (npm ci sur le lockfile committe)"
else
  npm init -y >/dev/null 2>&1
  npm i --silent --no-audit --no-fund \
    esbuild buffer process \
    "@cosmjs/proto-signing@$VERSION" "@cosmjs/stargate@$VERSION" >/dev/null
  mkdir -p "$LOCK"; cp package.json package-lock.json "$LOCK/"
  echo "   [!] lockfile ABSENT -> genere dans wallet/vendor-build/. COMMITTE-LE :"
  echo "       sans lui, le SHA256 publie n'est pas reproductible par un tiers."
fi

echo "## 2) verification AVANT build : la derivation ne doit pas changer"
cat > derive.cjs <<'EOF'
const { DirectSecp256k1HdWallet } = require("@cosmjs/proto-signing");
// Vecteur de test public standard (BIP-39). Ce n'est evidemment pas une cle utilisee quelque part.
const M = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
(async () => {
  const w = await DirectSecp256k1HdWallet.fromMnemonic(M, { prefix: "dendra" });
  console.log((await w.getAccounts())[0].address);
})();
EOF
GOT="$(node derive.cjs)"
EXPECTED="dendra19rl4cm2hmr8afy4kldpxz3fka4jguq0ax8k3fr"
if [ "$GOT" != "$EXPECTED" ]; then
  echo "[X] DERIVATION DIFFERENTE — ne PAS deployer."
  echo "    attendu : $EXPECTED"
  echo "    obtenu  : $GOT"
  echo "    Une adresse differente signifie que les comptes existants deviendraient inaccessibles."
  exit 2
fi
echo "   [OK] meme mnemonique -> meme adresse ($EXPECTED)"

echo "## 3) bundle autonome"
cat > entry.js <<'EOF'
export { DirectSecp256k1HdWallet } from "@cosmjs/proto-signing";
export { SigningStargateClient, StargateClient, GasPrice } from "@cosmjs/stargate";
EOF
# `Buffer`/`process` sont des globales Node que CosmJS touche encore par endroits : sans ces
# polyfills injectes, le bundle leve `Cannot read properties of undefined (reading 'alloc')` DANS
# LE NAVIGATEUR uniquement — c'est-a-dire la ou aucun test Node ne l'aurait vu.
cat > shim.js <<'EOF'
import { Buffer } from "buffer";
import process from "process";
export { Buffer, process };
EOF
./node_modules/.bin/esbuild entry.js --bundle --format=esm --platform=browser --target=es2020 \
  --inject:./shim.js --define:global=globalThis --minify --legal-comments=none \
  --outfile="$OUT/cosmjs-$VERSION.js"

echo "## 4) garde : plus AUCUN import distant ne doit subsister"
if grep -qE 'from"https?://|import\("https?://' "$OUT/cosmjs-$VERSION.js"; then
  echo "[X] le bundle rappelle encore un CDN — le self-hosting ne servirait a rien."; exit 3
fi
echo "   [OK] bundle autonome"

echo "## 5) page de TEST, derivee du vrai wallet"
# On ne teste pas `index.html` tel quel : il pointe encore vers le CDN, donc le test ne dirait RIEN
# du bundle. On ne duplique pas non plus le wallet a la main — deux copies finissent par diverger,
# et c'est precisement ainsi que la fonction d'echappement du chat s'etait retrouvee plus faible
# que celle de /network. La page de test est donc DERIVEE du fichier reel a chaque build, par la
# seule substitution des deux URLs. Elle n'est pas destinee a etre committee.
sed -E 's#https://cdn\.jsdelivr\.net/npm/@cosmjs/proto-signing@[0-9.]+/\+esm#./vendor/cosmjs-'"$VERSION"'.js#; s#https://cdn\.jsdelivr\.net/npm/@cosmjs/stargate@[0-9.]+/\+esm#./vendor/cosmjs-'"$VERSION"'.js#' \
  "$ROOT/wallet/web/index.html" > "$ROOT/wallet/web/_vendor-test.html"
if grep -q "cdn.jsdelivr.net" "$ROOT/wallet/web/_vendor-test.html"; then
  echo "[X] la substitution a echoue : la page de test appelle encore le CDN, elle ne prouverait rien."
  rm -f "$ROOT/wallet/web/_vendor-test.html"; exit 4
fi
echo "   [OK] _vendor-test.html genere, 0 appel CDN"

echo
echo "============================================================"
echo " ECRIT   : $OUT/cosmjs-$VERSION.js"
echo " TAILLE  : $(wc -c < "$OUT/cosmjs-$VERSION.js") o"
echo " SHA256  : $(sha256sum "$OUT/cosmjs-$VERSION.js" | cut -d' ' -f1)"
echo
echo " ETAPE MANUELLE OBLIGATOIRE — test NAVIGATEUR (Node ne le remplace pas) :"
echo "   cd $ROOT/wallet/web && python3 -m http.server 8899"
echo "   puis ouvrir http://localhost:8899/_vendor-test.html et verifier, dans cet ordre :"
echo "     a) creer un wallet -> 12 mots affiches"
echo "     b) l'importer dans un autre onglet -> MEME adresse"
echo "     c) consulter le solde -> le reseau repond"
echo "     d) console du navigateur -> AUCUNE erreur"
echo "   (b) est le test qui compte : il prouve que la derivation fonctionne vraiment cote"
echo "   navigateur, pas seulement sous Node."
echo
echo " index.html (le wallet REEL) est INCHANGE et continue d'utiliser le CDN : rien ne peut"
echo " partir en production tant que le test n'est pas concluant. Si (a)-(d) sont verts, la"
echo " bascule consiste a appliquer au vrai fichier la meme substitution que ci-dessus."
echo " En cas d'echec : rm wallet/web/_vendor-test.html && rm -rf wallet/web/vendor"
echo "============================================================"
