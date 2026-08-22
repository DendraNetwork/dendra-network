#!/usr/bin/env bash
# anchor_vrf_key.sh - anchors a validator's VRF key on-chain (decentralized VRF bootstrap).
#
# To be run by the OPERATOR and by EVERY JOINER once their node is in consensus (catching_up=false). It
# generates a VRF key (when absent), writes it into the home directory (loaded at the next (re)start through
# DENDRA_VRF_KEY_FILE) and ANCHORS the public key on-chain with a proof-of-possession
# (register-validator-vrf-key). Idempotent.
#
# WHY THIS IS MANDATORY: without the anchor, a validator does NOT CONTRIBUTE to the VRF seed (it does not
# count toward committee_min_vrf_contributors). While the contributor count stays below the floor, no audit
# committee is drawn at all: nothing served is verified, and every miner's share stays HELD at settlement
# instead of being released. This step is therefore not a formality for the validator running it — it is what
# unblocks the DRAW. ⚠️ It is necessary and NOT sufficient for payment: a drawn committee still concludes only
# when `audit_min_quorum` jurors vote, and the jury excludes the miner under audit — so the sampled half stays
# held until enough miners are registered. Anchoring lifts the seed floor; it does not lift the jury floor.
# Every validator must run it so that committees are drawn from a decentralized VRF.
#
# Usage (WSL):
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
command -v "$VRF" >/dev/null 2>&1 || { echo "[vrf] FATAL: dendra-vrf not found (from ~/dendra: go build -o ~/go/bin/dendra-vrf ./cmd/dendra-vrf)"; exit 1; }

A=$("$D" keys show "$VAL_KEY" -a $KB 2>/dev/null) || { echo "[vrf] FATAL: key '$VAL_KEY' absent from the keyring ($HOME_DIR)"; exit 1; }
echo "[vrf] validator (operator) = $A"

# 1) VRF key: reuse it when present (idempotent), otherwise generate one.
# The shebang is bash, so `read ... < <(...)` worked under normal execution. But this script is also PIPED
# into other shells (`| bash -s`, `docker exec -T node bash -s`, and minimal images where /bin/sh is dash),
# where process substitution breaks with "redirection unexpected" - on the GENERATION path only, which is
# exactly the documented remedy for a corrupted key. Rewritten in POSIX: same behaviour, no dependency on
# the shell that runs it.
if [ -s "$KEYFILE" ]; then
  VSK=$(cat "$KEYFILE"); VPK=$("$VRF" pubkey "$VSK")
  echo "[vrf] existing VRF key reused (pub $(printf '%.16s' "$VPK")...)"
else
  _KG=$("$VRF" keygen) || { echo "[vrf] FATAL: 'dendra-vrf keygen' failed"; exit 1; }
  # keygen writes "<sk>\t<pk>" - a TAB, not a space (cmd/dendra-vrf/main.go: Printf "%s\t%s\n").
  # The original `read -r VSK VPK` worked because it splits on the FULL IFS (space AND tab). A first
  # "POSIX" rewrite used ${_KG%% *}/${_KG##* }, which split ONLY on spaces: on tab-separated input both
  # return the whole string. `cut -f` defaults to the tab, so it matches the producer's format - and that
  # is already what docker/entrypoint-chain.sh:169 does.
  VSK=$(printf '%s\n' "$_KG" | cut -f1)
  VPK=$(printf '%s\n' "$_KG" | cut -f2)
  [ -n "$VSK" ] && [ -n "$VPK" ] && [ "$VSK" != "$VPK" ] \
    || { echo "[vrf] FATAL: unexpected keygen output (expected '<sk> <pk>'): $_KG"; exit 1; }
  echo "$VSK" > "$KEYFILE"; chmod 600 "$KEYFILE"
  echo "[vrf] VRF key generated -> $KEYFILE (pub $(printf '%.16s' "$VPK")...)"
fi

# 2) proof-of-possession over dendra/vrf-pop/<operator>
POP=$("$VRF" prove "$VSK" "dendra/vrf-pop/$A")

# 2a) LOCAL SELF-VERIFICATION before spending a transaction. The on-chain handler rejects an invalid PoP
# with ErrUnauthorized (msg_server_register_validator_vrf.go:41) - better to know it HERE, for free, with a
# message that says WHAT is broken, than to read a bare "code":4.
if ! "$VRF" verify "$VPK" "dendra/vrf-pop/$A" "$POP" >/dev/null 2>&1; then
  echo "[vrf] FATAL: the proof-of-possession does not even verify LOCALLY."
  echo "       The private key in $KEYFILE does not match the derived pubkey, or the file is corrupted."
  echo "       Remedy: move the key aside and let it be regenerated -"
  echo "         mv $KEYFILE $KEYFILE.bad && <re-run this script>"
  echo "       (anchoring a NEW key is safe: the handler overwrites, it is idempotent.)"
  exit 1
fi
echo "[vrf] proof-of-possession verified locally (pub $(printf '%.16s' "$VPK")...)"

# 2a-bis) ACCOUNT PRE-FLIGHT. Without it, the chain rejects the anchoring with
#   "signature verification failed; please verify account number (0) and chain-id (dendra)"
# even though the PoP was valid. Signing with account_number=0 means the CLI could NOT read the on-chain
# account before signing - so the problem is not the VRF, it is access to the account. Without this
# pre-flight the failure is wrongly blamed on the VRF key and the wrong subsystem gets debugged.
ACC=$("$D" query auth account "$A" -o json --node "$NODE" 2>&1)
# Whitespace is STRIPPED before extraction: the output is INDENTED JSON, so the field reads
# `"account_number": "12"`, with a space after the colon. A guard that required `"account_number":"12"`
# without the space refused a perfectly readable account and printed "UNREADABLE" right above the account
# it had just printed. A guard that is wrong costs more than no guard: it blocks the correct path AND
# points the diagnosis at the wrong culprit.
ACCNUM=$(printf '%s' "$ACC" | tr -d ' \t' | grep -oE '"account_number":"?[0-9]+' | head -1 | grep -oE '[0-9]+$')
if [ -z "$ACCNUM" ]; then
  if printf '%s' "$ACC" | grep -q '"address"'; then
    # The chain ANSWERED (the account exists): it is OUR extraction that failed, not the account.
    echo "[vrf] WARNING: account found but 'account_number' not extracted - unexpected output format."
    echo "       This is NOT blocking: the transaction below will tell the truth (code + raw_log)."
    printf '%s\n' "$ACC" | head -8 | sed 's/^/       | /'
  else
    echo "[vrf] FATAL: account $A not found on $NODE - signing would produce account_number=0"
    echo "       and a 'signature verification failed' rejection. Chain response:"
    printf '%s\n' "$ACC" | head -5 | sed 's/^/       | /'
    echo "       Most likely cause: the account never received funds on THIS chain (an account only"
    echo "       exists after a first credit); or --node points at another chain."
    exit 1
  fi
else
  echo "[vrf] account readable on-chain (account_number=$ACCNUM)"
fi

# ⛔ NO INTERPRETER HERE, AND THAT IS NOT A LICENCE TO GREP BLINDLY. This script does not only run on
# an operator's host: `deploy/bond_validator.sh` PIPES IT INTO THE `node` CONTAINER
# (`docker compose exec -T node bash -s`), and that image is `debian:bookworm-slim` plus
# ca-certificates, curl and coreutils -- nothing else (`docker/Dockerfile.node`). No python3, no jq.
# A reader that needs one prints NOTHING there, the caller reads nothing as failure, and a VRF
# anchoring that WORKED comes out as "ANCHORING FAILED" with exit 1 -- after a bond that went through,
# leaving the validator unable to feed the committee seed. Measuring the host says nothing about the
# machine a script is injected into.
# The zero rule still holds, and it does not need a parser to hold. What must never be confused is
# "the field is absent because it is zero" with "nothing answered at all". So EXISTENCE is decided on
# a field that is never omitted -- `txhash`, a non-empty string -- and only once it is there is `code`
# read, defaulting to the ZERO OF ITS TYPE when absent. Empty output keeps its own meaning: no answer.
tx_code() { # reads a broadcast or `query tx` answer on stdin; prints its code, or NOTHING
  _ans="$(cat)"
  _flat="$(printf '%s' "$_ans" | tr -d ' 	')"
  case "$_flat" in *'"txhash":"'[0-9A-Fa-f]*) ;; *) return 0;; esac
  _c="$(printf '%s' "$_flat" | grep -oE '"code":[0-9]+' | head -1 | cut -d: -f2)"
  printf '%s\n' "${_c:-0}"
}

echo "[vrf] anchoring on-chain (register-validator-vrf-key)..."
# The WHOLE output is kept. Piping it to `grep ... || true` made a chain rejection surface as a bare
# "code":4 without the raw_log, and the script printed "OK" right afterwards. A refused anchoring would
# therefore display "OK" while the validator contributes NOTHING.
TXOUT=$("$D" tx jobs register-validator-vrf-key "$VPK" "$POP" --from "$VAL_KEY" $KB --chain-id "$CHAIN_ID" --node "$NODE" --yes -o json 2>&1)
CODE=$(printf '%s' "$TXOUT" | tx_code)
TXH=$(printf '%s' "$TXOUT" | grep -oE '"txhash":"[0-9A-Fa-f]+"' | head -1 | cut -d'"' -f4)
if [ "${CODE:-1}" != "0" ]; then
  echo "[vrf] ANCHORING FAILED - code=${CODE:-?} txhash=${TXH:-none}"
  echo "[vrf] chain message:"
  printf '%s\n' "$TXOUT" | grep -oE '"raw_log":"[^"]*"' | head -1 | sed 's/^/       /'
  printf '%s\n' "$TXOUT" | head -20 | sed 's/^/       | /'
  echo "[vrf] The validator does NOT contribute to the seed while this fails. Do not ignore."
  exit 1
fi
echo "[vrf] tx accepted (txhash=$TXH)"

# 2b) REAL CONFIRMATION: wait for the transaction to be COMMITTED and read ITS code.
# The code returned by the broadcast above says one thing only: the transaction entered the MEMPOOL. A
# transaction accepted there can still fail at execution (insufficient funds, state changed meanwhile).
# Only `query tx <hash>` gives the execution result.
# Note: the anchored key cannot be read back - `ValidatorVrfPubkey` (keeper.go:41) is written by the
# handler but exposed by NO query (it is absent from autocli). Do not try to read it back through
# `query jobs validator-vrf-key`: that command does not exist, so it fails, its empty result reads as
# "the key is not there", and the script cries wolf over a perfectly healthy anchoring.
CONFIRMED=0
if [ -n "$TXH" ]; then
  echo "[vrf] waiting for inclusion in a block..."
  i=0
  while [ $i -lt 12 ]; do
    i=$((i+1)); sleep 3
    QT=$("$D" query tx "$TXH" -o json --node "$NODE" 2>/dev/null) || continue
    printf '%s' "$QT" | grep -q '"txhash"' || continue
    QCODE=$(printf '%s' "$QT" | tx_code)
    if [ "${QCODE:-1}" = "0" ]; then
      echo "[vrf] CONFIRMED: tx EXECUTED successfully in a block (code=0)."
      CONFIRMED=1
    else
      echo "[vrf] EXECUTION FAILED - the tx was in the mempool but was rejected at block level (code=$QCODE):"
      printf '%s\n' "$QT" | grep -oE '"raw_log":"[^"]*"' | head -1 | sed 's/^/       /'
      echo "[vrf] The validator does NOT contribute to the seed. Do not ignore."
      exit 1
    fi
    break
  done
fi
[ "$CONFIRMED" = 1 ] || echo "[vrf] WARNING: could not confirm the inclusion of $TXH within 36 s - check by hand:
       $D query tx $TXH --node $NODE"

echo
echo "[vrf] IMPORTANT: (re)start your node with  DENDRA_VRF_KEY_FILE=$KEYFILE  so that it SIGNS its"
echo "     vote extensions (otherwise the key is anchored but the node provides no proof = no contribution)."
echo "     Then check:  $D query jobs committee-seed-health --node $NODE   (latest_contributors must rise)"
