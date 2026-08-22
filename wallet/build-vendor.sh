#!/usr/bin/env bash
# Builds the CosmJS bundle served by the wallet — LOCALLY, with no CDN.
#
# WHY. The wallet is the page where the user types their recovery phrase. Loading its cryptographic
# library from a third-party CDN is inadmissible there, for two reasons:
#
#   1. NO integrity check is possible. The `integrity` attribute (SRI) does not exist on a dynamic
#      `import()`, and jsDelivr's `/+esm` bundle itself re-imports its dependencies from the CDN.
#      Copying two files alongside would therefore close nothing: what is required is a
#      SELF-CONTAINED bundle. If the CDN ever serves hostile code, it runs on the seed-entry page.
#
#   2. CosmJS 0.32.4 is DEPRECATED FOR SECURITY REASONS upstream: it uses `elliptic`, whose
#      maintainers write "several security-relevant bugs [...] private keys might still be at
#      risk". Fixed from 0.34.0 on, which replaces the cryptographic library. The 0.39 chosen here
#      drops `elliptic` in favour of `@noble/curves`, `@noble/hashes` and `@scure/bip39`.
#
# WHAT THIS SCRIPT VERIFIES, RATHER THAN ASSUMES:
#   - DERIVATION IS IDENTICAL from one version to the next — the same mnemonic yields the same
#     address, so a version bump loses access to NO existing account (step 2);
#   - the produced bundle no longer contains any remote import (step 4);
#   - the served page loads THAT bundle, and nothing else (step 5).
#
# WHAT REMAINS TO BE PROVEN, AND THAT THIS SCRIPT DOES NOT PROVE: behaviour in a BROWSER. Node is no
# substitute — trying to emulate one by removing `Buffer` breaks `undici`, an internal Node
# component, not the bundle. Hence the manual test, mandatory, printed at the end of the script.
#
# USAGE:   bash wallet/build-vendor.sh
set -euo pipefail

VERSION="0.39.0"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/wallet/web/vendor"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v npm >/dev/null || { echo "[X] npm required"; exit 1; }
mkdir -p "$OUT"

echo "## 1) dependencies (isolated in $WORK, nothing is installed into the repository)"
# REPRODUCIBILITY. Pinning `@cosmjs/*@0.39.0` IS NOT ENOUGH: npm resolves TRANSITIVE dependencies at
# install time, so two builds made on the same day can differ by a few bytes — and therefore by
# SHA256. A published digest is then worth nothing: nobody can reproduce it, and "check the hash"
# becomes an empty phrase. The lockfile freezes the WHOLE tree; it is committed next to the script so
# that third-party verification is actually possible. That is the difference between publishing a
# recipe and publishing a proof.
LOCK="$ROOT/wallet/vendor-build"
cd "$WORK"
if [ -f "$LOCK/package-lock.json" ] && [ -f "$LOCK/package.json" ]; then
  cp "$LOCK/package.json" "$LOCK/package-lock.json" .
  npm ci --silent --no-audit --no-fund >/dev/null
  echo "   [OK] dependency tree FROZEN (npm ci on the committed lockfile)"
else
  npm init -y >/dev/null 2>&1
  npm i --silent --no-audit --no-fund \
    esbuild buffer process \
    "@cosmjs/proto-signing@$VERSION" "@cosmjs/stargate@$VERSION" >/dev/null
  mkdir -p "$LOCK"; cp package.json package-lock.json "$LOCK/"
  echo "   [!] lockfile MISSING -> generated in wallet/vendor-build/. COMMIT IT:"
  echo "       without it, the published SHA256 is not reproducible by a third party."
fi

echo "## 2) check BEFORE build: derivation must not change"
cat > derive.cjs <<'EOF'
const { DirectSecp256k1HdWallet } = require("@cosmjs/proto-signing");
// Standard public test vector (BIP-39). Obviously not a key used anywhere.
const M = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
(async () => {
  const w = await DirectSecp256k1HdWallet.fromMnemonic(M, { prefix: "dendra" });
  console.log((await w.getAccounts())[0].address);
})();
EOF
GOT="$(node derive.cjs)"
EXPECTED="dendra19rl4cm2hmr8afy4kldpxz3fka4jguq0ax8k3fr"
if [ "$GOT" != "$EXPECTED" ]; then
  echo "[X] DERIVATION DIFFERS — do NOT deploy."
  echo "    expected : $EXPECTED"
  echo "    got      : $GOT"
  echo "    A different address means existing accounts would become inaccessible."
  exit 2
fi
echo "   [OK] same mnemonic -> same address ($EXPECTED)"

echo "## 3) self-contained bundle"
cat > entry.js <<'EOF'
export { DirectSecp256k1HdWallet } from "@cosmjs/proto-signing";
export { SigningStargateClient, StargateClient, GasPrice } from "@cosmjs/stargate";
EOF
# `Buffer`/`process` are Node globals that CosmJS still touches in places: without these injected
# polyfills, the bundle throws `Cannot read properties of undefined (reading 'alloc')` IN THE
# BROWSER only — that is, exactly where no Node test would have caught it.
cat > shim.js <<'EOF'
import { Buffer } from "buffer";
import process from "process";
export { Buffer, process };
EOF
./node_modules/.bin/esbuild entry.js --bundle --format=esm --platform=browser --target=es2020 \
  --inject:./shim.js --define:global=globalThis --minify --legal-comments=none \
  --outfile="$OUT/cosmjs-$VERSION.js"

echo "## 4) guard: NOT ONE remote import may remain"
if grep -qE 'from"https?://|import\("https?://' "$OUT/cosmjs-$VERSION.js"; then
  echo "[X] the bundle still calls out to a CDN — self-hosting would achieve nothing."; exit 3
fi
echo "   [OK] self-contained bundle"

echo "## 5) guard: the served page must point at the local bundle, and at NOTHING else"
# The bundle only protects if it is REALLY the one the page loads. These two assertions are therefore
# checked at every build against the real file: a regression that reintroduced a CDN URL turns the
# build red instead of shipping to production unnoticed.
if grep -q "cdn.jsdelivr.net" "$ROOT/wallet/web/index.html"; then
  echo "[X] wallet/web/index.html still calls a CDN — the local bundle would achieve nothing."; exit 4
fi
if ! grep -q "./vendor/cosmjs-$VERSION.js" "$ROOT/wallet/web/index.html"; then
  echo "[X] wallet/web/index.html does not load ./vendor/cosmjs-$VERSION.js."; exit 4
fi
echo "   [OK] index.html loads ./vendor/cosmjs-$VERSION.js, 0 CDN calls"
# Throwaway page for the browser test below: identical to the served page, but outside the web root,
# so a test in progress is never mistaken for the deployed page. Derived, therefore never edited by
# hand (two hand-maintained copies always end up diverging).
cp "$ROOT/wallet/web/index.html" "$ROOT/wallet/web/_vendor-test.html"

echo "## 6) publication under the web root (site/wallet/) — copy, never edit"
# `site/` is the deployed root; the wallet is served there under /wallet/, so `./vendor/...` must
# resolve there. The single source stays `wallet/web/`.
#
# THE COMPARISON HAPPENS BEFORE THE COPY. Comparing the copy to its source AFTER copying it asserts
# nothing — `cp` has just made them equal, so such a check cannot go red whatever the state of the
# tree was. Compared BEFORE, a difference means the published artefact was edited instead of its
# source, and copying over it would destroy that edit without a word. Hence: stop, show what
# differs, and require an explicit decision.
SITE="$ROOT/site/wallet"
mkdir -p "$SITE/vendor"
if [ -f "$SITE/index.html" ] && ! cmp -s "$ROOT/wallet/web/index.html" "$SITE/index.html"; then
  echo "[X] site/wallet/index.html DIFFERS from its source wallet/web/index.html:"
  diff -u "$ROOT/wallet/web/index.html" "$SITE/index.html" | head -40 | sed "s/^/      /" || true
  if [ "${WALLET_SYNC:-0}" != "1" ]; then
    echo "    The published page is DERIVED. Port the difference into wallet/web/index.html (the"
    echo "    source), then re-run — or, to overwrite the copy from the source on purpose:"
    echo "        WALLET_SYNC=1 bash wallet/build-vendor.sh"
    exit 5
  fi
  echo "   [!] WALLET_SYNC=1 — the published copy is overwritten from the source."
fi
cp "$ROOT/wallet/web/index.html"            "$SITE/index.html"
cp "$OUT/cosmjs-$VERSION.js"                "$SITE/vendor/cosmjs-$VERSION.js"
echo "   [OK] site/wallet/ synchronised (page + bundle)"

echo "## 7) guard: the policy APPLIED to /wallet/ must name THIS page's inline script, and nothing else"
# The wallet page is served under a policy of its own, which names its single inline script by
# SHA-256 instead of allowing every inline script. That digest lives in `site/_headers`, a different
# file: nothing but this check ties the two together. A digest left stale does not degrade the page,
# it stops it from running at all — so the mismatch has to be found here, not in production.
#
# THIS CHECK PARSES, IT DOES NOT GREP — and the difference is the whole point. Looking for the digest
# string ANYWHERE in `site/_headers` asserts that A STRING EXISTS IN A FILE, not that A POLICY
# APPLIES. Measured on this repository: such a check stays green with 'unsafe-inline' put back beside
# the digests, and green again with both /wallet/ rules deleted and the digests left in a comment —
# i.e. green in the two states it exists to forbid. So the file is parsed into blocks by path and
# directives by name, the rule that actually binds the wallet URLs is selected, and its script-src is
# read token by token. Those two states are replayed against this guard in step 7b: a guard nobody
# has seen go red for the right reason is a guard nobody has tested.
HEADERS="$ROOT/site/_headers"
[ -f "$HEADERS" ] || { echo "[X] site/_headers is missing: /wallet/ would be served without its own policy."; exit 6; }
cat > csp_check.cjs <<'EOF'
"use strict";
// usage: node csp_check.cjs <page.html> [<_headers>]
//   page only      -> checks the page, prints "OK <digest-LF> <digest-CRLF>"
//   page + headers -> also checks the policy that BINDS the wallet URLs
// failure: prints "FAIL:<reason>" on stdout, the explanation on stderr, exits 1.
const fs = require("fs"), crypto = require("crypto");
const PAGE = process.argv[2], HEADERS = process.argv[3];
let LF = null, CRLF = null;
function die(reason, msg) {
  console.log("FAIL:" + reason);
  console.error("    " + msg);
  if (LF) {
    console.error("    The script-src of the rules serving /wallet/ must carry BOTH of these, quotes included:");
    console.error("        '" + LF + "'");
    console.error("        '" + CRLF + "'");
  }
  process.exit(1);
}

/* ---------- 1. the page ---------------------------------------------------------------- */
const src = fs.readFileSync(PAGE, "utf8");
// COUNT THEM ALL. Asking whether a second <script> appears AFTER the module answers a narrower
// question than the message claims: a second inline script placed BEFORE the module is just as
// uncovered by the digest, and runs under the same policy.
const scripts = (src.match(/<script\b/gi) || []).length;
if (scripts !== 1) die("script-count", "the page holds " + scripts + " <script> element(s); one digest covers exactly one, wherever the others sit.");
// A hash-based script-src refuses inline event-handler attributes and javascript: URLs outright.
// Either one turns the deployed page into a half-dead thing; it has to be caught here.
const handlers = (src.match(/<[a-z][^>]*\son[a-z]+\s*=/gi) || []).length;
if (handlers) die("inline-handler", handlers + " inline event-handler attribute(s): a hash-based script-src refuses them.");
const jsurl = (src.match(/javascript:/gi) || []).length;
if (jsurl) die("javascript-url", jsurl + " javascript: URL(s): a hash-based script-src refuses them.");

const OPEN = "<script type=\"module\">";
const a = src.indexOf(OPEN), b = src.indexOf("</script>", a);
if (a < 0 || b < 0) die("no-module", "no inline module script found in " + PAGE);
const body = src.slice(a + OPEN.length, b).replace(/\r\n/g, "\n");
const h = x => "sha256-" + crypto.createHash("sha256").update(x, "utf8").digest("base64");
// Two digests for one script: the same page checked out with CRLF endings is a different byte
// string, and this repository normalises line endings on checkout (core.autocrlf).
LF = h(body); CRLF = h(body.replace(/\n/g, "\r\n"));
if (!HEADERS) { console.log("OK " + LF + " " + CRLF); process.exit(0); }

/* ---------- 2. the policy file ---------------------------------------------------------- */
// Grammar: a line with no leading whitespace is a PATH; the indented "Name: value" lines under it
// are that path's headers. A full-line comment is NOT a rule — stripped first, so a digest parked in
// a comment satisfies nothing.
const blocks = [];
let cur = null;
for (const raw of fs.readFileSync(HEADERS, "utf8").split(/\r?\n/)) {
  if (/^\s*#/.test(raw) || !raw.trim()) continue;
  if (!/^\s/.test(raw)) { cur = { path: raw.trim(), headers: {} }; blocks.push(cur); continue; }
  const i = raw.indexOf(":");
  if (i < 0 || !cur) continue;
  const name = raw.slice(0, i).trim().toLowerCase(), val = raw.slice(i + 1).trim();
  // The same header twice in one block: the platform sends both and the browser enforces both.
  cur.headers[name] = cur.headers[name] ? cur.headers[name] + ", " + val : val;
}
const toRe = p => new RegExp("^" + p
  .replace(/[.+^${}()|[\]\\?]/g, "\\$&")
  .replace(/:[A-Za-z_][A-Za-z0-9_]*/g, "[^/]+")
  .replace(/\*/g, ".*") + "$");
const literal = p => p.replace(/\*/g, "").replace(/:[A-Za-z_][A-Za-z0-9_]*/g, "").length;

// The page is fetched at BOTH of these; a rule matching one is not guaranteed to match the other.
const URLS = ["/wallet/", "/wallet/index.html"];
// Tokens that give back what the digest just took away. 'strict-dynamic' is here on purpose: with
// it, the one hashed script may load any further script it likes — on the page where a seed is typed.
const LOOSE = ["'unsafe-inline'", "'unsafe-eval'", "'strict-dynamic'", "'unsafe-hashes'", "*", "data:", "blob:", "http:", "https:"];

for (const url of URLS) {
  const hits = blocks.filter(x => toRe(x.path).test(url) && x.headers["content-security-policy"]);
  // ABSENCE IS NOT CONFORMITY. No rule means the platform default, i.e. no policy at all.
  if (!hits.length) die("no-rule", "no rule of site/_headers carries a Content-Security-Policy for " + url + ".");
  // The rule that binds = the most specific match. Under either platform semantics this is the one
  // that decides: if only the most specific rule is applied, it is this one; if every matching rule
  // is merged, the browser enforces the INTERSECTION, which is at least as strict as this one.
  const top = Math.max.apply(null, hits.map(x => literal(x.path)));
  for (const blk of hits.filter(x => literal(x.path) === top)) {
    const dirs = {};
    for (const part of blk.headers["content-security-policy"].split(";")) {
      const t = part.trim().split(/\s+/).filter(Boolean);
      if (t.length) dirs[t[0].toLowerCase()] = t.slice(1);
    }
    // READ WHAT THE CONSUMER READS: a browser with no script-src falls back to default-src.
    const eff = dirs["script-src"] || dirs["default-src"];
    const which = dirs["script-src"] ? "script-src" : "default-src (script-src absent; the browser falls back to it)";
    if (!eff) die("no-script-src", blk.path + " has neither script-src nor default-src, so nothing constrains a script on " + url + ".");
    // CSP KEYWORDS AND SCHEMES ARE ASCII CASE-INSENSITIVE TO THE BROWSER, so this comparison must be
    // too. Measured both ways: with 'UNSAFE-INLINE' in script-src, a case-sensitive check answered OK
    // while Chrome ran the inline script; with 'self' alone it blocked it. A guard that reads the
    // policy differently from the consumer that enforces it is not a guard.
    // The DIGEST comparison below stays case-sensitive on purpose: base64 is not case-insensitive,
    // and folding it would let a wrong digest pass.
    const effLower = eff.map(t => t.toLowerCase());
    for (const bad of LOOSE) {
      if (effLower.indexOf(bad) >= 0) die("loose-" + bad.replace(/\W/g, ""), blk.path + ": " + which + " contains " + bad + " for " + url + " — that runs an inline script next to the seed, digest or no digest.");
    }
    if (eff.indexOf("'" + LF + "'") < 0 || eff.indexOf("'" + CRLF + "'") < 0) {
      die("digest-missing", blk.path + ": " + which + " does not name this page's script for " + url + " (both the LF and the CRLF form are required).");
    }
  }
}
console.log("OK " + LF + " " + CRLF);
EOF

# Digests first, from the page alone: they are what the mutations below have to be built from, and
# the page-level checks (one script, no handler attribute, no javascript: URL) gate everything after.
CSP_D="$(node csp_check.cjs "$SITE/index.html")" || { echo "[X] the page served at /wallet/ cannot be covered by a single digest (reason above)."; exit 6; }
CSP_LF="$(printf '%s' "$CSP_D"   | awk '{print $2}')"
CSP_CRLF="$(printf '%s' "$CSP_D" | awk '{print $3}')"

echo "   ## 7b) bench OF THE GUARD ITSELF — it must go red on what it exists to forbid"
# A guard nobody has watched fail proves nothing. Each case below is a state that MUST NOT ship; the
# bench asserts the guard names it, and names it for the right reason. The first two are the states
# in which a `grep` of the digest stayed green.
BENCH_FAIL=0
bench () { # $1 = label, $2 = page, $3 = headers ("-" = none), $4 = expected verdict
  local out rc=0 exp="$4"
  if [ "$3" = "-" ]; then out="$(node csp_check.cjs "$2" 2>/dev/null)" || rc=$?
  else                    out="$(node csp_check.cjs "$2" "$3" 2>/dev/null)" || rc=$?; fi
  out="$(printf '%s' "$out" | awk '{print $1}')"
  if [ "$out" = "$exp" ]; then printf '      [ok]   %-52s -> %s\n' "$1" "$out"
  else printf '      [FAIL] %-52s -> %s (expected %s)\n' "$1" "$out" "$exp"; BENCH_FAIL=1; fi
}

# (a) 'unsafe-inline' put back beside the digests: the digests are still in the file, and still in
#     the right rules — and the policy no longer forbids anything.
sed "s|script-src 'self' 'sha256-|script-src 'self' 'unsafe-inline' 'sha256-|g" "$HEADERS" > h_unsafe
# (b) both /wallet/ rules deleted, digests archived in a comment: the string survives, the rule does
#     not, and /wallet/ falls back to the site-wide policy that allows every inline script.
awk 'BEGIN{keep=1} /^[^ \t#]/ {keep = ($0 !~ /^\/wallet/)} keep {print}' "$HEADERS" > h_comment
printf "#   archived digests: '%s' '%s'\n" "$CSP_LF" "$CSP_CRLF" >> h_comment
# (c) one digest gone stale — the failure mode this step was written for in the first place.
sed "s|'$CSP_LF' ||" "$HEADERS" > h_stale
# (d) script-src dropped from the /wallet/ rules: the browser falls back to default-src, which names
#     no digest. A checker that only looks at script-src reports nothing here.
sed "s|script-src 'self' 'sha256-[^;]*; ||" "$HEADERS" > h_nosrc
# (e) a second inline script inserted BEFORE the module: outside a digest, inside the same policy.
awk '/<script type="module">/ && !d {print "<script>window.__x=1;</script>"; d=1} 1' "$SITE/index.html" > p_two.html
# (f) THE SAME DEFEAT AS (a), IN ANOTHER CASE. CSP keywords are ASCII case-insensitive to the browser:
#     measured, `script-src 'self' 'UNSAFE-INLINE'` runs the inline script in Chrome while `'self'`
#     alone blocks it. A case-sensitive check answered OK here — the guard read the policy differently
#     from the consumer that enforces it, which is the whole failure this step exists to prevent.
sed "s|script-src 'self' 'sha256-|script-src 'self' 'UNSAFE-INLINE' 'sha256-|g" "$HEADERS" > h_upper

bench "untouched page + untouched _headers"              "$SITE/index.html" "$HEADERS"  "OK"
bench "(a) 'unsafe-inline' back beside the digests"      "$SITE/index.html" h_unsafe    "FAIL:loose-unsafeinline"
bench "(b) /wallet/ rules deleted, digests in a comment" "$SITE/index.html" h_comment   "FAIL:loose-unsafeinline"
bench "(c) one digest stale"                             "$SITE/index.html" h_stale     "FAIL:digest-missing"
bench "(d) script-src dropped, default-src takes over"   "$SITE/index.html" h_nosrc     "FAIL:digest-missing"
bench "(e) a second <script> BEFORE the module"          p_two.html         "-"         "FAIL:script-count"
bench "(f) 'UNSAFE-INLINE' in caps — the browser obeys"  "$SITE/index.html" h_upper     "FAIL:loose-unsafeinline"
if [ "$BENCH_FAIL" != 0 ]; then
  echo "[X] the /wallet/ CSP guard does not go red on a state it exists to forbid — it cannot vouch for the file."
  exit 6
fi

# stdout carries the verdict, stderr the explanation and the two lines to paste: only stdout is muted.
if ! node csp_check.cjs "$SITE/index.html" "$HEADERS" >/dev/null; then
  echo "[X] the policy site/_headers applies to /wallet/ does not cover the page served there (reason above)."
  exit 6
fi
echo "   [OK] /wallet/ and /wallet/* name this page's script (LF and CRLF), with no token that undoes it"

echo
echo "============================================================"
echo " WRITTEN : $OUT/cosmjs-$VERSION.js"
echo " SIZE    : $(wc -c < "$OUT/cosmjs-$VERSION.js") B"
echo " SHA256  : $(sha256sum "$OUT/cosmjs-$VERSION.js" | cut -d' ' -f1)"
echo
echo " MANDATORY MANUAL STEP — BROWSER test (Node is no substitute):"
echo "   cd $ROOT/wallet/web && python3 -m http.server 8899"
echo "   then open http://localhost:8899/_vendor-test.html and check, in this order:"
echo "     a) create a wallet -> 24 words displayed"
echo "     b) import it in another tab -> SAME address"
echo "     c) query the balance -> the network answers"
echo "     d) click Lock -> the 24 words and the import box are EMPTY, not merely hidden"
echo "     e) create a wallet again, then FOLLOW A LINK in the top nav and press BACK ->"
echo "        the 24 words are gone and window.__pendingMnemonic is null (console)"
echo "     f) create a wallet, switch to another window, come back -> the 24 words are STILL THERE"
echo "     g) browser console -> NO error"
echo "   (b) is the test that counts for the bundle: it proves derivation really works on"
echo "   the browser side, not only under Node. (d), (e) and (f) are the ones that count for"
echo "   the page. (e) cannot be replaced by reading the code: the back/forward cache restores"
echo "   a live document, which is a browser behaviour, and it needs a server that does NOT"
echo "   send Cache-Control: no-store — that header alone disables the cache and makes the"
echo "   test pass for the wrong reason. (f) is the other half: erasing a phrase its owner has"
echo "   not written down yet is a damage too, so leaving the page wipes, looking away does not."
echo "============================================================"
