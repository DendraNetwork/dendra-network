#!/usr/bin/env bash
# launch_env_check.sh -- generate or validate the public .env, and prove the fail-closed boot refusals
# (gateway/relay/faucet) on a dry run before any deployment.
#
# Usage (WSL, repo root):
#   tr -d '\r' < deploy/launch/launch_env_check.sh | bash -s -- --init   # first time: generate ~/.dendra-launch.env
#   tr -d '\r' < deploy/launch/launch_env_check.sh | bash                # check + dry-run refusal proof
# The secrets file lives outside the repo (~/.dendra-launch.env) — never committed, rsynced to VPS:.env by the kit.
set -u
REPO="${DENDRA_REPO:-$(pwd)}"
[ -f "$REPO/docker-compose.yml" ] && [ -d "$REPO/services" ] || {
  echo "[ERR] run from the repository root"; exit 1; }
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
FAIL=0

# --rotate-secrets -- replace the two secrets in place, keeping everything else.
#
# Why this exists: `--init` refuses to overwrite and says "delete it first to regenerate the secrets",
# but this file also carries tuned values (audit timeout, CORS origins, faucet limits) and the cold pocket
# addresses, which cannot be regenerated because their keys are offline. Deleting the file to rotate two
# secrets would lose all of that, so operators would keep a leaked secret instead.
# This path rotates only the secrets and prints no value, so no secret ends up in a scrollback buffer.
if [ "${1:-}" = "--rotate-secrets" ]; then
  [ -f "$ENVF" ] || { echo "[ERR] $ENVF does not exist — use --init first."; exit 1; }
  cp -a "$ENVF" "$ENVF.before-rotation" && chmod 600 "$ENVF.before-rotation"
  KEY=$(openssl rand -hex 24); TOK=$(openssl rand -hex 24)
  sed -i -e "s|^DENDRA_API_KEY=.*|DENDRA_API_KEY=$KEY|" \
         -e "s|^DENDRA_RELAY_TOKEN=.*|DENDRA_RELAY_TOKEN=$TOK|" "$ENVF"
  # Verify by shape, never by value: the point is to prove the rotation happened without reprinting
  # what was rotated. Comparing the two files tells us both lines actually changed.
  CH=$(diff <(grep -E '^DENDRA_(API_KEY|RELAY_TOKEN)=' "$ENVF.before-rotation") \
            <(grep -E '^DENDRA_(API_KEY|RELAY_TOKEN)=' "$ENVF") | grep -c '^<')
  if [ "$CH" = "2" ]; then
    echo "[OK] both secrets rotated in place (48 hex chars each). Everything else kept."
    echo "     Previous file: $ENVF.before-rotation. Delete it once the new secrets are deployed."
  else
    echo "[ERR] expected 2 changed lines, saw $CH. $ENVF may lack one of the two keys — check it."
    exit 1
  fi
  exit 0
fi
if [ "${1:-}" = "--init" ]; then
  if [ -f "$ENVF" ]; then
    echo "[SKIP] $ENVF already exists. To rotate the secrets without losing the tuned values and the"
    echo "       cold-pocket addresses:  ... | bash -s -- --rotate-secrets"
  else
    KEY=$(openssl rand -hex 24)
    TOK=$(openssl rand -hex 24)
    S1T=$(openssl rand -hex 24)
    S1G=$(openssl rand -hex 24)
    sed -e "s|^DENDRA_API_KEY=.*|DENDRA_API_KEY=$KEY|" \
        -e "s|^DENDRA_RELAY_TOKEN=.*|DENDRA_RELAY_TOKEN=$TOK|" \
        -e "s|^DENDRA_FINAL_SEASON_INTERNAL_TOKEN=.*|DENDRA_FINAL_SEASON_INTERNAL_TOKEN=$S1T|" \
        -e "s|^DENDRA_FINAL_SEASON_GRADER_TOKEN=.*|DENDRA_FINAL_SEASON_GRADER_TOKEN=$S1G|" \
        "$REPO/deploy/launch/.env.public.example" > "$ENVF"
    chmod 600 "$ENVF"
    echo "[OK] generated: $ENVF (strong secrets, chmod 600 — outside the repo)"
  fi
fi

echo "## 1) validate the public .env ($ENVF)"
[ -f "$ENVF" ] || { echo "   [ERR] missing — run first:  ... | bash -s -- --init"; exit 1; }
# A key assigned twice is a silent ambiguity, and one of these keys carries 3.3 M DNDR.
# This file is sourced, so bash keeps the last assignment, while a reader or a `grep -m1` parser takes the
# first. Appending a corrected value with `>>`, the natural way to fix a mistake, produces exactly that
# duplicate. On DENDRA_RESERVE_ADDR it could hand the Reserve to a compromised address, with nothing on
# screen to say so. So the ambiguity is refused: the file must assign each key at most once.
# The pattern also counts `export FOO=...` lines (which `set -a ; . file` treats the same way) and
# strips all whitespace, tabs included, so the key is normalised the way the shell normalises it.
DUPES=$(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$ENVF" \
  | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | sort | uniq -d)
if [ -n "$DUPES" ]; then
  echo "   [ERR] $ENVF assigns the same key more than once:"
  printf '%s\n' "$DUPES" | sed 's/^/         /'
  echo "         The shell keeps the last one; other readers take the first. Which value applies is"
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
# ADR-046 / ADR-048 item 8: the network file names the chain and the cold pockets. A missing chain id is
# refused by the chain itself at boot; a missing cold address is not (the genesis builder generates a key
# on the host and warns), so it is refused here, before anything is deployed.
chk "CHAIN_ID set (no default exists)"         'printf %s "${DENDRA_CHAIN_ID:-}" | grep -qE "^[a-z0-9][a-z0-9-]{2,49}$"'
for _cold in DENDRA_COMMUNITY_ADDR DENDRA_TEAM_ADDR DENDRA_DELEGATION_ADDR; do
  chk "$_cold is a dendra1 address (cold pocket)" "printf %s \"\${$_cold:-}\" | grep -qE '^dendra1[02-9ac-hj-np-z]{38,}$'"
done
chk "AUDIT_SAMPLE_BPS in [500, 10000]"          '[ "${DENDRA_AUDIT_SAMPLE_BPS:-0}" -ge 500 ] && [ "${DENDRA_AUDIT_SAMPLE_BPS:-0}" -le 10000 ]'
# ADR-046: gRPC has no public consumer, and ufw does not filter a port Docker publishes. Absent means
# the compose default, 0.0.0.0: refused, never assumed closed.
chk "GRPC_BIND=127.0.0.1 (gRPC not public)"      '[ "${DENDRA_GRPC_BIND:-}" = "127.0.0.1" ]'
# ADR-047: the Final Testnet Season's first block is a published fact; the internal token keeps a
# neighbour container from writing trap evidence.
chk "FINAL_SEASON_START_HEIGHT is a block height >= 1"  'printf %s "${DENDRA_FINAL_SEASON_START_HEIGHT:-}" | grep -qE "^[1-9][0-9]*$"'
chk "FINAL_SEASON_INTERNAL_TOKEN strong (>=32 chars)"  '[ "${#DENDRA_FINAL_SEASON_INTERNAL_TOKEN}" -ge 32 ] && ! printf %s "$DENDRA_FINAL_SEASON_INTERNAL_TOKEN" | grep -q GENERATED'
chk "FINAL_SEASON_GRADER_TOKEN strong (>=32 chars)"    '[ "${#DENDRA_FINAL_SEASON_GRADER_TOKEN}" -ge 32 ] && ! printf %s "$DENDRA_FINAL_SEASON_GRADER_TOKEN" | grep -q GENERATED'

echo "## 2) dry-run boot-refusal proof (fail-closed guards bite before the VPS)"
# A non-zero exit code does not prove that the guard refused.
# A service that dies of a syntax error, a missing import, a busy port or a bad interpreter exits non-zero
# too, so a deleted guard would still look fine. Each refusal path exits with the documented code 2 and
# prints its refusal with a reason, so the proof asks for both. Anything else is reported as inconclusive
# rather than as a pass.
refusal_proven() { # $1 = label, $2 = rc, $3 = output
  # Match the word, not one exact wording: the faucet ends its fatal error with "Refusing to boot.", and
  # requiring one literal spelling would turn a correct refusal into an inconclusive one and block a
  # relaunch. rc=2 alone is never enough; "refused", "Refusing" and "refuses" all count, hence -i.
  if [ "$2" = "2" ] && printf '%s' "$3" | grep -qiE 'refus(ed|ing|es)'; then
    echo "   OK  $1 (rc=2, refusal message present)"
  elif [ "$2" = "0" ]; then
    echo "   FAIL  $1: it booted"; printf '%s\n' "$3" | tail -2; FAIL=1
  else
    echo "   inconclusive  $1: died with rc=$2 and no refusal message, which proves nothing about the guard"
    printf '%s\n' "$3" | tail -2; FAIL=1
  fi
}
cd "$REPO/services"
# 2a. gateway: PUBLIC=1 without a key -> refusal expected (exit 2)
OUT=$(DENDRA_PUBLIC=1 DENDRA_API_KEY= DENDRA_GW_PORT=18661 timeout 10 python3 gateway.py 2>&1); RC=$?
refusal_proven "gateway" "$RC" "$OUT"
# 2b. relay: PUBLIC=1 without a token -> refusal expected
OUT=$(DENDRA_PUBLIC=1 DENDRA_RELAY_TOKEN= timeout 10 python3 relay.py 18645 2>&1); RC=$?
refusal_proven "relay" "$RC" "$OUT"
# 2c. faucet: PUBLIC=1 without PoW -> refusal expected (exit 2)
OUT=$(DENDRA_PUBLIC=1 DENDRA_FAUCET_POW_BITS=0 DENDRA_FAUCET_PORT=14500 DENDRA_HOME=/tmp timeout 10 python3 faucet.py 2>&1); RC=$?
refusal_proven "faucet" "$RC" "$OUT"
cd "$REPO"

echo "## 3) verdict"
if [ "$FAIL" = 0 ]; then
  echo "   Green: the public .env is valid and the 3 boot refusals are proven on a dry run."
  echo "   Next: tr -d '\\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP>"
else
  echo "   Red: fix the above before any public deployment."
fi
exit $FAIL
