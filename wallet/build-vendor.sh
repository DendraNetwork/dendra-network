#!/usr/bin/env bash
# Builds the CosmJS bundle served by the wallet, locally and without a CDN.
#
# The wallet is the page where the user types a recovery phrase, so its cryptographic library must
# not come from a third-party CDN:
#
#   1. No integrity check is possible. The `integrity` attribute (SRI) does not apply to a dynamic
#      `import()`, and the CDN's `/+esm` bundle re-imports its own dependencies from the CDN. Only a
#      self-contained bundle closes this; otherwise hostile CDN code would run on the seed-entry page.
#
#   2. CosmJS 0.32.4 is deprecated upstream for security reasons: it uses `elliptic`, whose
#      maintainers write "several security-relevant bugs [...] private keys might still be at
#      risk". 0.34.0 replaced the cryptographic library. The 0.39 used here relies on
#      `@noble/curves`, `@noble/hashes` and `@scure/bip39` instead.
#
# What the script checks:
#   - derivation is identical across versions: the same mnemonic yields the same address, so a
#     version bump cannot lock anyone out of an existing account (step 2);
#   - the bundle contains no remote import (step 4);
#   - the served page loads that bundle and nothing else (step 5).
#
# What it cannot check is browser behaviour. Node is no substitute (emulating a browser by removing
# `Buffer` breaks Node's own `undici`), so the manual browser test printed at the end is required.
#
# Usage:   bash wallet/build-vendor.sh
set -euo pipefail

VERSION="0.39.0"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/wallet/web/vendor"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v npm >/dev/null || { echo "[X] npm required"; exit 1; }
mkdir -p "$OUT"

echo "## 1) dependencies (isolated in $WORK, nothing is installed into the repository)"
# Pinning `@cosmjs/*@0.39.0` is not enough for a reproducible digest: npm resolves transitive
# dependencies at install time, so two builds can differ by a few bytes. The committed lockfile
# freezes the whole tree, which lets a third party reproduce the published SHA-256.
LOCK="$ROOT/wallet/vendor-build"
cd "$WORK"
if [ -f "$LOCK/package-lock.json" ] && [ -f "$LOCK/package.json" ]; then
  cp "$LOCK/package.json" "$LOCK/package-lock.json" .
  npm ci --silent --no-audit --no-fund >/dev/null
  echo "   [OK] dependency tree frozen (npm ci on the committed lockfile)"
else
  npm init -y >/dev/null 2>&1
  npm i --silent --no-audit --no-fund \
    esbuild buffer process \
    "@cosmjs/proto-signing@$VERSION" "@cosmjs/stargate@$VERSION" >/dev/null
  mkdir -p "$LOCK"; cp package.json package-lock.json "$LOCK/"
  echo "   [!] lockfile missing, generated in wallet/vendor-build/. Commit it:"
  echo "       without it, the published SHA256 is not reproducible by a third party."
fi

echo "## 2) check before the build: derivation must not change"
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
  echo "[X] derivation differs: do not deploy."
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
# `Buffer` and `process` are Node globals that CosmJS still touches. Without these injected polyfills
# the bundle throws `Cannot read properties of undefined (reading 'alloc')`, but only in the browser,
# where no Node test would catch it.
cat > shim.js <<'EOF'
import { Buffer } from "buffer";
import process from "process";
export { Buffer, process };
EOF
./node_modules/.bin/esbuild entry.js --bundle --format=esm --platform=browser --target=es2020 \
  --inject:./shim.js --define:global=globalThis --minify --legal-comments=none \
  --outfile="$OUT/cosmjs-$VERSION.js"

echo "## 4) guard: no remote import may remain"
if grep -qE 'from"https?://|import\("https?://' "$OUT/cosmjs-$VERSION.js"; then
  echo "[X] the bundle still calls out to a CDN, so self-hosting would achieve nothing."; exit 3
fi
echo "   [OK] self-contained bundle"

echo "## 5) guard: the served page must load the local bundle and nothing else"
# The bundle only helps if it is the one the page loads, so a CDN URL coming back into the page fails
# the build here instead of reaching production.
if grep -q "cdn.jsdelivr.net" "$ROOT/wallet/web/index.html"; then
  echo "[X] wallet/web/index.html still calls a CDN, so the local bundle would achieve nothing."; exit 4
fi
if ! grep -q "./vendor/cosmjs-$VERSION.js" "$ROOT/wallet/web/index.html"; then
  echo "[X] wallet/web/index.html does not load ./vendor/cosmjs-$VERSION.js."; exit 4
fi
echo "   [OK] index.html loads ./vendor/cosmjs-$VERSION.js, 0 CDN calls"
# Throwaway page for the browser test below: identical to the served page but outside the web root,
# so a test in progress is never mistaken for the deployed page. It is derived, never edited by hand.
cp "$ROOT/wallet/web/index.html" "$ROOT/wallet/web/_vendor-test.html"

# Three outcomes here, not two. The next two sections publish into `sites/app/` and read
# `sites/app/_headers`. That tree is absent from a published clone, where this script would otherwise
# create it and then fail with exit 6, reporting a defect where there is only an absence.
# A third party's goal is already met above (vendored bundle, no CDN, SHA-256 printed below).
# Wherever `sites/app/` exists, a missing `_headers` is a real defect and still fails hard.
if [ -d "$ROOT/sites/app" ]; then
echo "## 6) publication under the app site (sites/app/wallet/): copy, never edit"
# `sites/app/` is the source of app.dendranetwork.com and serves the wallet under /wallet/, so
# `./vendor/...` must resolve there. The single source stays `wallet/web/`. The assembly
# (deploy/site/assemble.py) then writes sites/dist/app/ with the header partial in place; it refuses
# any change to the inline script bytes, so the digests checked in step 7 are those of the served page.
#
# The comparison runs before the copy: after `cp` the two files are equal whatever happened. A
# difference found first means someone edited the published copy instead of its source, and copying
# over it would silently destroy that edit, so the script stops and asks for an explicit decision.
SITE="$ROOT/sites/app/wallet"
mkdir -p "$SITE/vendor"
if [ -f "$SITE/index.html" ] && ! cmp -s "$ROOT/wallet/web/index.html" "$SITE/index.html"; then
  echo "[X] sites/app/wallet/index.html differs from its source wallet/web/index.html:"
  diff -u "$ROOT/wallet/web/index.html" "$SITE/index.html" | head -40 | sed "s/^/      /" || true
  if [ "${WALLET_SYNC:-0}" != "1" ]; then
    echo "    The published page is derived. Port the difference into wallet/web/index.html (the"
    echo "    source) and run again, or overwrite the copy from the source on purpose:"
    echo "        WALLET_SYNC=1 bash wallet/build-vendor.sh"
    exit 5
  fi
  echo "   [!] WALLET_SYNC=1: the published copy is overwritten from the source."
fi
cp "$ROOT/wallet/web/index.html"            "$SITE/index.html"
cp "$OUT/cosmjs-$VERSION.js"                "$SITE/vendor/cosmjs-$VERSION.js"
echo "   [OK] sites/app/wallet/ synchronised (page + bundle)"

echo "## 7) guard: the policy applied to /wallet/ must name this page's inline script, and nothing else"
# The wallet page has its own policy, which names its single inline script by SHA-256 instead of
# allowing every inline script. The digest lives in `sites/app/_headers`, and only this check ties the two
# files together. A stale digest stops the page from running at all, so it must be caught here.
#
# The check parses the file instead of grepping for the digest. A grep only proves the string exists:
# it stayed green with 'unsafe-inline' added beside the digests, and with both /wallet/ rules deleted
# and the digests left in a comment. So the file is parsed into blocks by path and directives by name,
# the rule that binds the wallet URLs is selected, and its script-src is read token by token. Step 7b
# replays those two states against the guard.
HEADERS="$ROOT/sites/app/_headers"
[ -f "$HEADERS" ] || { echo "[X] sites/app/_headers is missing: /wallet/ would be served without its own policy."; exit 6; }
cat > csp_check.cjs <<'EOF'
"use strict";
// usage: node csp_check.cjs <page.html> [<_headers>]
//   page only      -> checks the page, prints "OK <digest-LF> <digest-CRLF>"
//   page + headers -> also checks the policy that binds the wallet URLs
// failure: prints "FAIL:<reason>" on stdout, the explanation on stderr, exits 1.
const fs = require("fs"), crypto = require("crypto");
const PAGE = process.argv[2], HEADERS = process.argv[3];
let LF = null, CRLF = null;
function die(reason, msg) {
  console.log("FAIL:" + reason);
  console.error("    " + msg);
  if (LF) {
    console.error("    The script-src of the rules serving /wallet/ must carry both of these, quotes included:");
    console.error("        '" + LF + "'");
    console.error("        '" + CRLF + "'");
  }
  process.exit(1);
}

/* ---------- 1. the page ---------------------------------------------------------------- */
const src = fs.readFileSync(PAGE, "utf8");
// Count every script element: a second inline script placed before the module is just as uncovered
// by the digest as one placed after it, and runs under the same policy.
const scripts = (src.match(/<script\b/gi) || []).length;
if (scripts !== 1) die("script-count", "the page holds " + scripts + " <script> element(s); one digest covers exactly one, wherever the others sit.");
// A hash-based script-src refuses inline event-handler attributes and javascript: URLs outright,
// which would leave the deployed page half broken, so both are caught here.
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
// Grammar: a line with no leading whitespace is a path; the indented "Name: value" lines under it
// are that path's headers. Full-line comments are stripped first, so a digest kept in a comment
// satisfies nothing.
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

// The page is fetched at both of these; a rule matching one is not guaranteed to match the other.
const URLS = ["/wallet/", "/wallet/index.html"];
// Tokens that undo the digest. 'strict-dynamic' is listed on purpose: with it, the hashed script may
// load any further script, on the page where a seed is typed.
const LOOSE = ["'unsafe-inline'", "'unsafe-eval'", "'strict-dynamic'", "'unsafe-hashes'", "*", "data:", "blob:", "http:", "https:"];

for (const url of URLS) {
  const hits = blocks.filter(x => toRe(x.path).test(url) && x.headers["content-security-policy"]);
  // No rule means the platform default, which is no policy at all.
  if (!hits.length) die("no-rule", "no rule of the _headers file carries a Content-Security-Policy for " + url + ".");
  // The binding rule is the most specific match. If the platform applies only the most specific rule,
  // it is this one; if it merges every matching rule, the browser enforces the intersection, which is
  // at least as strict as this one.
  const top = Math.max.apply(null, hits.map(x => literal(x.path)));
  for (const blk of hits.filter(x => literal(x.path) === top)) {
    const dirs = {};
    for (const part of blk.headers["content-security-policy"].split(";")) {
      const t = part.trim().split(/\s+/).filter(Boolean);
      if (t.length) dirs[t[0].toLowerCase()] = t.slice(1);
    }
    // Read the policy as the browser does: with no script-src it falls back to default-src.
    const eff = dirs["script-src"] || dirs["default-src"];
    const which = dirs["script-src"] ? "script-src" : "default-src (script-src absent; the browser falls back to it)";
    if (!eff) die("no-script-src", blk.path + " has neither script-src nor default-src, so nothing constrains a script on " + url + ".");
    // Browsers treat CSP keywords and schemes as ASCII case-insensitive, so this comparison does too:
    // with 'UNSAFE-INLINE' in script-src, a case-sensitive check answered OK while the browser ran the
    // inline script. The digest comparison below stays case-sensitive, because base64 is.
    const effLower = eff.map(t => t.toLowerCase());
    for (const bad of LOOSE) {
      if (effLower.indexOf(bad) >= 0) die("loose-" + bad.replace(/\W/g, ""), blk.path + ": " + which + " contains " + bad + " for " + url + ", which lets an inline script run next to the seed whatever the digest.");
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

echo "   ## 7b) bench of the guard itself: it must fail on what it exists to forbid"
# Each case below is a state that must not ship; the bench asserts the guard refuses it for the right
# reason. The first two are the states in which a grep for the digest stayed green.
BENCH_FAIL=0
bench () { # $1 = label, $2 = page, $3 = headers ("-" = none), $4 = expected verdict
  local out rc=0 exp="$4"
  if [ "$3" = "-" ]; then out="$(node csp_check.cjs "$2" 2>/dev/null)" || rc=$?
  else                    out="$(node csp_check.cjs "$2" "$3" 2>/dev/null)" || rc=$?; fi
  out="$(printf '%s' "$out" | awk '{print $1}')"
  if [ "$out" = "$exp" ]; then printf '      [ok]   %-52s -> %s\n' "$1" "$out"
  else printf '      [FAIL] %-52s -> %s (expected %s)\n' "$1" "$out" "$exp"; BENCH_FAIL=1; fi
}

# (a) 'unsafe-inline' put back beside the digests: the digests are still in the right rules, and the
#     policy no longer forbids anything.
sed "s|script-src 'self' 'sha256-|script-src 'self' 'unsafe-inline' 'sha256-|g" "$HEADERS" > h_unsafe
# (b) both /wallet/ rules deleted, digests archived in a comment: the string survives, the rule does
#     not, and /wallet/ falls back to the site-wide policy that allows every inline script.
awk 'BEGIN{keep=1} /^[^ \t#]/ {keep = ($0 !~ /^\/wallet/)} keep {print}' "$HEADERS" > h_comment
printf "#   archived digests: '%s' '%s'\n" "$CSP_LF" "$CSP_CRLF" >> h_comment
# (c) one digest gone stale, the failure this step was first written for.
sed "s|'$CSP_LF' ||" "$HEADERS" > h_stale
# (d) script-src dropped from the /wallet/ rules: the browser falls back to default-src, which names
#     no digest. A checker that only looks at script-src reports nothing here.
sed "s|script-src 'self' 'sha256-[^;]*; ||" "$HEADERS" > h_nosrc
# (e) a second inline script inserted before the module: outside a digest, inside the same policy.
awk '/<script type="module">/ && !d {print "<script>window.__x=1;</script>"; d=1} 1' "$SITE/index.html" > p_two.html
# (f) the same defeat as (a) in upper case. Browsers read CSP keywords case-insensitively, so
#     `script-src 'self' 'UNSAFE-INLINE'` runs the inline script; a case-sensitive check missed it.
sed "s|script-src 'self' 'sha256-|script-src 'self' 'UNSAFE-INLINE' 'sha256-|g" "$HEADERS" > h_upper

bench "untouched page + untouched _headers"              "$SITE/index.html" "$HEADERS"  "OK"
bench "(a) 'unsafe-inline' back beside the digests"      "$SITE/index.html" h_unsafe    "FAIL:loose-unsafeinline"
bench "(b) /wallet/ rules deleted, digests in a comment" "$SITE/index.html" h_comment   "FAIL:loose-unsafeinline"
bench "(c) one digest stale"                             "$SITE/index.html" h_stale     "FAIL:digest-missing"
bench "(d) script-src dropped, default-src takes over"   "$SITE/index.html" h_nosrc     "FAIL:digest-missing"
bench "(e) a second <script> before the module"          p_two.html         "-"         "FAIL:script-count"
bench "(f) 'UNSAFE-INLINE' in upper case"                "$SITE/index.html" h_upper     "FAIL:loose-unsafeinline"
if [ "$BENCH_FAIL" != 0 ]; then
  echo "[X] the /wallet/ CSP guard accepts a state it exists to forbid, so it cannot vouch for the file."
  exit 6
fi

# stdout carries the verdict, stderr the explanation and the two lines to paste: only stdout is muted.
if ! node csp_check.cjs "$SITE/index.html" "$HEADERS" >/dev/null; then
  echo "[X] the policy sites/app/_headers applies to /wallet/ does not cover the page served there (reason above)."
  exit 6
fi
echo "   [OK] /wallet/ and /wallet/* name this page's script (LF and CRLF), with no token that undoes it"
else
  echo "## 6-7) publication under sites/app/ and the /wallet/ policy guard -- [not measured]"
  echo "   sites/app/ is not in this tree, which is expected in a published clone: the app site"
  echo "   is hosted separately with its own headers. This run did establish the rest: the"
  echo "   bundle is vendored, loads no CDN, and its SHA-256 is printed below so that a third"
  echo "   party can reproduce it."
fi

echo
echo "============================================================"
echo " Written : $OUT/cosmjs-$VERSION.js"
echo " Size    : $(wc -c < "$OUT/cosmjs-$VERSION.js") B"
echo " SHA256  : $(sha256sum "$OUT/cosmjs-$VERSION.js" | cut -d' ' -f1)"
echo
echo " Required manual step: browser test (Node is no substitute):"
echo "   cd $ROOT/wallet/web && python3 -m http.server 8899"
echo "   then open http://localhost:8899/_vendor-test.html and check, in this order:"
echo "     a) create a wallet -> 24 words displayed"
echo "     b) import it in another tab -> same address"
echo "     c) query the balance -> the network answers"
echo "     d) click Lock -> the 24 words and the import box are empty, not merely hidden"
echo "     e) create a wallet again, follow a link in the top nav and press Back ->"
echo "        the 24 words are gone and window.__pendingMnemonic is null (console)"
echo "     f) create a wallet, switch to another window, come back -> the 24 words are still there"
echo "     g) browser console -> no error"
echo "   (b) is the test that matters for the bundle: it shows derivation works in the browser,"
echo "   not only under Node. (d), (e) and (f) matter for the page. (e) cannot be replaced by"
echo "   reading the code: the back/forward cache restores a live document, and the test needs a"
echo "   server that does not send Cache-Control: no-store, since that header disables the cache"
echo "   and makes the test pass for the wrong reason. (f) is the other half: erasing a phrase"
echo "   its owner has not written down yet also does harm, so leaving the page wipes it and"
echo "   looking away does not."
echo "============================================================"
