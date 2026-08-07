#!/usr/bin/env bash
# launch_env_check.sh — generate/validate the public .env AND prove the fail-closed boot refusals
# (gateway/relay/faucet) on a dry run BEFORE any deployment.
#
# Usage (WSL, repo root):
#   tr -d '\r' < deploy/launch/launch_env_check.sh | bash -s -- --init   # first time: generate ~/.dendra-launch.env
#   tr -d '\r' < deploy/launch/launch_env_check.sh | bash                # check + dry-run refusal proof
# The secrets file lives OUTSIDE the repo (~/.dendra-launch.env) — never committed, rsynced to VPS:.env by the kit.
set -u
REPO="${DENDRA_REPO:-$(pwd)}"
[ -f "$REPO/docker-compose.yml" ] && [ -d "$REPO/services" ] || {
  echo "[ERR] run from the repository ROOT"; exit 1; }
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
FAIL=0

# --rotate-secrets — replace the two secrets IN PLACE, keeping everything else.
#
# ⛔ WHY THIS EXISTS. `--init` refuses to overwrite and says "delete it first to REGENERATE the
# secrets". That advice is a trap: this file also carries tuned values (audit timeout, CORS origins,
# faucet limits) and the COLD POCKET ADDRESSES. Deleting it to rotate two secrets throws all of that
# away, and the addresses are the one thing that cannot be regenerated — the keys are offline.
# So the operator does the natural thing instead: keeps the leaked secret, because rotating costs
# too much. A rotation procedure nobody can afford is a rotation procedure nobody performs.
# This path rotates ONLY the secrets, and prints no value: a secret echoed to a terminal is a secret
# in a scrollback buffer.
if [ "${1:-}" = "--rotate-secrets" ]; then
  [ -f "$ENVF" ] || { echo "[ERR] $ENVF does not exist — use --init first."; exit 1; }
  cp -a "$ENVF" "$ENVF.avant-rotation" && chmod 600 "$ENVF.avant-rotation"
  KEY=$(openssl rand -hex 24); TOK=$(openssl rand -hex 24)
  sed -i -e "s|^DENDRA_API_KEY=.*|DENDRA_API_KEY=$KEY|" \
         -e "s|^DENDRA_RELAY_TOKEN=.*|DENDRA_RELAY_TOKEN=$TOK|" "$ENVF"
  # Verify by SHAPE, never by value: the point is to prove the rotation happened without reprinting
  # what was rotated. Comparing the two files tells us both lines actually changed.
  CH=$(diff <(grep -E '^DENDRA_(API_KEY|RELAY_TOKEN)=' "$ENVF.avant-rotation") \
            <(grep -E '^DENDRA_(API_KEY|RELAY_TOKEN)=' "$ENVF") | grep -c '^<')
  if [ "$CH" = "2" ]; then
    echo "[OK] both secrets rotated in place (48 hex chars each). Everything else kept."
    echo "     Previous file: $ENVF.avant-rotation — DELETE IT once the new secrets are deployed."
  else
    echo "[ERR] expected 2 changed lines, saw $CH. $ENVF may lack one of the two keys — check it."
    exit 1
  fi
  exit 0
fi
if [ "${1:-}" = "--init" ]; then
  if [ -f "$ENVF" ]; then
    echo "[SKIP] $ENVF already exists. To rotate the secrets WITHOUT losing the tuned values and the"
    echo "       cold-pocket addresses:  ... | bash -s -- --rotate-secrets"
  else
    KEY=$(openssl rand -hex 24)
    TOK=$(openssl rand -hex 24)
    sed -e "s|^DENDRA_API_KEY=.*|DENDRA_API_KEY=$KEY|" \
        -e "s|^DENDRA_RELAY_TOKEN=.*|DENDRA_RELAY_TOKEN=$TOK|" \
        "$REPO/deploy/launch/.env.public.example" > "$ENVF"
    chmod 600 "$ENVF"
    echo "[OK] generated: $ENVF (strong secrets, chmod 600 — OUTSIDE the repo)"
  fi
fi

echo "## 1) validate the public .env ($ENVF)"
[ -f "$ENVF" ] || { echo "   [ERR] missing — run first:  ... | bash -s -- --init"; exit 1; }
# ⛔ A KEY ASSIGNED TWICE IS A SILENT COIN FLIP, AND ONE OF THE COINS CARRIES 3.3 M DNDR.
# This file is SOURCED, so bash keeps the LAST assignment. That rule is invisible in the file and it
# is not shared: a reader sees the first line, a `grep -m1` parser takes the first line, and the shell
# takes the last one. Appending a corrected value with `>>` — the natural way to fix a mistake — is
# exactly what produces the duplicate, and the stale value it leaves behind is usually the one that
# was replaced for a reason. On DENDRA_RESERVE_ADDR that means a genesis handing the Reserve to a
# compromised address, with nothing on screen to say so.
# We do not resolve the ambiguity, we REFUSE it: the file must assign each key at most once.
DUPES=$(grep -oE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$ENVF" | tr -d ' =' | sort | uniq -d)
if [ -n "$DUPES" ]; then
  echo "   [ERR] $ENVF assigns the same key more than once:"
  printf '%s\n' "$DUPES" | sed 's/^/         /'
  echo "         The shell keeps the LAST one; other readers take the first. Which value applies is"
  echo "         therefore unknowable from the file. Keep exactly one line per key, then re-run."
  echo "         To see them:  grep -nE '^($(printf '%s' "$DUPES" | paste -sd'|'))=' $ENVF"
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENVF"; set +a
chk(){ if eval "$2"; then echo "   OK  $1"; else echo "   FAIL  $1"; FAIL=1; fi; }
chk "DENDRA_PUBLIC=1"                           '[ "${DENDRA_PUBLIC:-0}" = "1" ]'
chk "API_KEY strong (>=32 chars, not default)"  '[ "${#DENDRA_API_KEY}" -ge 32 ] && [ "$DENDRA_API_KEY" != "dendra" ] && ! printf %s "$DENDRA_API_KEY" | grep -q GENERATED'
chk "RELAY_TOKEN strong (>=16 chars)"           '[ "${#DENDRA_RELAY_TOKEN}" -ge 16 ] && ! printf %s "$DENDRA_RELAY_TOKEN" | grep -q GENERATED'
chk "FAUCET_POW_BITS >= 18"                     '[ "${DENDRA_FAUCET_POW_BITS:-0}" -ge 18 ]'
chk "WEBUI_AUTH=True"                           '[ "${WEBUI_AUTH:-False}" = "True" ]'
chk "AVAIL_SLASH dormant"                       '[ "${DENDRA_AVAIL_SLASH:-0}" = "0" ]'
chk "AUDIT_RESOLVE_TIMEOUT >= 120"              '[ "${DENDRA_AUDIT_RESOLVE_TIMEOUT:-0}" -ge 120 ]'

echo "## 2) dry-run BOOT-REFUSAL proof (fail-closed guards bite BEFORE the VPS)"
cd "$REPO/services"
# 2a. gateway: PUBLIC=1 WITHOUT a key -> refusal expected (exit 2)
OUT=$(DENDRA_PUBLIC=1 DENDRA_API_KEY= DENDRA_GW_PORT=18661 timeout 10 python3 gateway.py 2>&1); RC=$?
if [ "$RC" != 0 ] && [ "$RC" != 124 ]; then echo "   OK  gateway refuses without a key (rc=$RC)"
else echo "   FAIL  gateway booted WITHOUT a key (rc=$RC)"; printf '%s\n' "$OUT" | tail -2; FAIL=1; fi
# 2b. relay: PUBLIC=1 WITHOUT a token -> refusal expected
OUT=$(DENDRA_PUBLIC=1 DENDRA_RELAY_TOKEN= timeout 10 python3 relay.py 18645 2>&1); RC=$?
if [ "$RC" != 0 ] && [ "$RC" != 124 ]; then echo "   OK  relay refuses without a token (rc=$RC)"
else echo "   FAIL  relay booted WITHOUT a token (rc=$RC)"; printf '%s\n' "$OUT" | tail -2; FAIL=1; fi
# 2c. faucet: PUBLIC=1 WITHOUT PoW -> refusal expected (exit 2)
OUT=$(DENDRA_PUBLIC=1 DENDRA_FAUCET_POW_BITS=0 DENDRA_FAUCET_PORT=14500 DENDRA_HOME=/tmp timeout 10 python3 faucet.py 2>&1); RC=$?
if [ "$RC" != 0 ] && [ "$RC" != 124 ]; then echo "   OK  faucet refuses without PoW (rc=$RC)"
else echo "   FAIL  faucet booted WITHOUT PoW (rc=$RC)"; printf '%s\n' "$OUT" | tail -2; FAIL=1; fi
cd "$REPO"

echo "## 3) verdict"
if [ "$FAIL" = 0 ]; then
  echo "   GREEN: public .env valid + the 3 boot refusals PROVEN on a dry run."
  echo "   Next: tr -d '\\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP>"
else
  echo "   RED — fix the above before any public deployment."
fi
exit $FAIL
