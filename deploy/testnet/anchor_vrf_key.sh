#!/usr/bin/env bash
# anchor_vrf_key.sh - anchors a validator's VRF key on-chain (decentralized VRF bootstrap).
#
# To be run by the operator and by every joiner once their node is in consensus (catching_up=false). It
# generates a VRF key (when absent), writes it into the home directory (loaded at the next (re)start through
# DENDRA_VRF_KEY_FILE) and anchors the public key on chain with a proof of possession
# (register-validator-vrf-key). Idempotent.
#
# Why this is required: without the anchor, a validator does not contribute to the VRF seed (it does not
# count toward committee_min_vrf_contributors). While the contributor count stays below the floor, no audit
# committee is drawn at all: nothing served is verified, and every miner's share stays held at settlement
# instead of being released. So this step is what unblocks the draw. It is necessary but not sufficient for
# payment: a drawn committee still concludes only when `audit_min_quorum` jurors vote, and the jury excludes
# the miner under audit, so the sampled share stays held until enough miners are registered. Anchoring lifts
# the seed floor; it does not lift the jury floor.
# Every validator must run it so that committees are drawn from a decentralised VRF.
#
# Usage (WSL):
#   HOME_DIR=~/.dendra CHAIN_ID=<CHAIN_ID of network-info.txt> NODE=tcp://127.0.0.1:26657 VAL_KEY=validator \
#     tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | bash
set -uo pipefail
D="${DENDRAD:-$HOME/go/bin/dendrad}"; VRF="${DENDRAVRF:-$HOME/go/bin/dendra-vrf}"
HOME_DIR="${HOME_DIR:-$HOME/.dendra}"
# Never defaulted: the only plausible default is a previous chain's name, and a key
# anchored there is anchored nowhere.
CHAIN_ID="${CHAIN_ID:?set CHAIN_ID to the CHAIN_ID of network-info.txt}"
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
# Written in POSIX shell: this script is also piped into other shells (`| bash -s`,
# `docker exec -T node bash -s`, and minimal images where /bin/sh is dash), where process substitution
# breaks with "redirection unexpected", and only on the key generation path, the documented remedy for a
# corrupted key.
# The secret reaches dendra-vrf through the environment, never through argv. `ps` and /proc/<pid>/cmdline are
# readable by every local process, /proc/<pid>/environ by the owner only. `dendra-vrf` reads DENDRA_VRF_SK
# and then takes no key argument (docker/entrypoint-chain.sh uses the same channel).
# VSK stays a shell variable and is never exported: only the two calls below receive it.
if [ -s "$KEYFILE" ]; then
  VSK=$(cat "$KEYFILE"); VPK=$(DENDRA_VRF_SK="$VSK" "$VRF" pubkey)
  echo "[vrf] existing VRF key reused (pub $(printf '%.16s' "$VPK")...)"
else
  _KG=$("$VRF" keygen) || { echo "[vrf] FATAL: 'dendra-vrf keygen' failed"; exit 1; }
  # keygen writes "<sk>\t<pk>", with a tab, not a space (cmd/dendra-vrf/main.go: Printf "%s\t%s\n").
  # `cut -f` splits on the tab by default, so it matches the producer's format, as docker/entrypoint-chain.sh
  # does in its VRF bootstrap; ${_KG%% *} would split on spaces only and return the whole string.
  VSK=$(printf '%s\n' "$_KG" | cut -f1)
  VPK=$(printf '%s\n' "$_KG" | cut -f2)
  [ -n "$VSK" ] && [ -n "$VPK" ] && [ "$VSK" != "$VPK" ] \
    || { echo "[vrf] FATAL: unexpected keygen output (expected '<sk><TAB><pk>', got ${#_KG} characters; not printed, it may hold the secret)"; exit 1; }
  echo "$VSK" > "$KEYFILE"; chmod 600 "$KEYFILE"
  echo "[vrf] VRF key generated -> $KEYFILE (pub $(printf '%.16s' "$VPK")...)"
fi

# 2) proof-of-possession over dendra/vrf-pop/<operator>
POP=$(DENDRA_VRF_SK="$VSK" "$VRF" prove "dendra/vrf-pop/$A")

# 2a) Local self-verification before spending a transaction. The on-chain handler rejects an invalid PoP
# with ErrUnauthorized (msg_server_register_validator_vrf.go:41) - better to know it here, for free, with a
# message that says what is broken, than to read a bare "code":4.
if ! "$VRF" verify "$VPK" "dendra/vrf-pop/$A" "$POP" >/dev/null 2>&1; then
  echo "[vrf] FATAL: the proof-of-possession does not even verify locally."
  echo "       The private key in $KEYFILE does not match the derived pubkey, or the file is corrupted."
  echo "       Remedy: move the key aside and let it be regenerated -"
  echo "         mv $KEYFILE $KEYFILE.bad && <re-run this script>"
  echo "       (anchoring a new key is safe: the handler overwrites, it is idempotent.)"
  exit 1
fi
echo "[vrf] proof-of-possession verified locally (pub $(printf '%.16s' "$VPK")...)"

# 2a-bis) Account pre-flight. Without it, the chain rejects the anchoring with
#   "signature verification failed; please verify account number (0) and chain-id (dendra)"
# even though the PoP was valid. Signing with account_number=0 means the CLI could not read the on-chain
# account before signing, which points at access to the account rather than at the VRF. Without this
# pre-flight the failure is wrongly blamed on the VRF key and the wrong subsystem gets debugged.
ACC=$("$D" query auth account "$A" -o json --node "$NODE" 2>&1)
# Whitespace is stripped before extraction: the output is indented JSON, so the field reads
# `"account_number": "12"`, with a space after the colon. A guard that required `"account_number":"12"`
# without the space refused a perfectly readable account and reported it unreadable right above the account
# it had just printed. A guard that is wrong costs more than no guard: it blocks the correct path and
# points the diagnosis at the wrong culprit.
ACCNUM=$(printf '%s' "$ACC" | tr -d ' \t' | grep -oE '"account_number":"?[0-9]+' | head -1 | grep -oE '[0-9]+$')
if [ -z "$ACCNUM" ]; then
  if printf '%s' "$ACC" | grep -q '"address"'; then
    # The chain answered (the account exists): it is our extraction that failed, not the account.
    echo "[vrf] WARNING: account found but 'account_number' not extracted - unexpected output format."
    echo "       This is not blocking: the transaction below will tell the truth (code + raw_log)."
    printf '%s\n' "$ACC" | head -8 | sed 's/^/       | /'
  else
    echo "[vrf] FATAL: account $A not found on $NODE - signing would produce account_number=0"
    echo "       and a 'signature verification failed' rejection. Chain response:"
    printf '%s\n' "$ACC" | head -5 | sed 's/^/       | /'
    echo "       Most likely cause: the account never received funds on this chain (an account only"
    echo "       exists after a first credit); or --node points at another chain."
    exit 1
  fi
else
  echo "[vrf] account readable on-chain (account_number=$ACCNUM)"
fi

# No interpreter here, but no blind grep either. This script does not only run on an operator's host:
# `deploy/bond_validator.sh` pipes it into the `node` container
# (`docker compose exec -T node bash -s`), and that image is `debian:bookworm-slim` plus
# ca-certificates, curl and coreutils, nothing else (`docker/Dockerfile.node`): no python3, no jq.
# A reader that needs one would print nothing there, and a VRF anchoring that worked would be reported as
# "Anchoring failed" with exit 1, after a bond that went through.
# A field absent because it is zero must not be confused with no answer at all. So existence is decided on
# a field that is never omitted (`txhash`, a non-empty string), and only once it is there is `code` read,
# defaulting to the zero of its type when absent. Empty output keeps its own meaning: no answer.
tx_code() { # reads a broadcast or `query tx` answer on stdin; prints its code, or nothing
  _ans="$(cat)"
  _flat="$(printf '%s' "$_ans" | tr -d ' 	')"
  case "$_flat" in *'"txhash":"'[0-9A-Fa-f]*) ;; *) return 0;; esac
  _c="$(printf '%s' "$_flat" | grep -oE '"code":[0-9]+' | head -1 | cut -d: -f2)"
  printf '%s\n' "${_c:-0}"
}

echo "[vrf] anchoring on-chain (register-validator-vrf-key)..."
# The whole output is kept. Piping it to `grep ... || true` made a chain rejection surface as a bare
# "code":4 without the raw_log, and the script printed "OK" right afterwards. A refused anchoring would
# therefore display "OK" while the validator contributes nothing.
TXOUT=$("$D" tx jobs register-validator-vrf-key "$VPK" "$POP" --from "$VAL_KEY" $KB --chain-id "$CHAIN_ID" --node "$NODE" --yes -o json 2>&1)
CODE=$(printf '%s' "$TXOUT" | tx_code)
TXH=$(printf '%s' "$TXOUT" | grep -oE '"txhash":"[0-9A-Fa-f]+"' | head -1 | cut -d'"' -f4)
if [ "${CODE:-1}" != "0" ]; then
  echo "[vrf] Anchoring failed - code=${CODE:-?} txhash=${TXH:-none}"
  echo "[vrf] chain message:"
  printf '%s\n' "$TXOUT" | grep -oE '"raw_log":"[^"]*"' | head -1 | sed 's/^/       /'
  printf '%s\n' "$TXOUT" | head -20 | sed 's/^/       | /'
  echo "[vrf] The validator does not contribute to the seed while this fails. Do not ignore."
  exit 1
fi
echo "[vrf] tx accepted (txhash=$TXH)"

# 2b) Real confirmation: wait for the transaction to be committed and read its code.
# The code returned by the broadcast above says one thing only: the transaction entered the mempool. A
# transaction accepted there can still fail at execution (insufficient funds, state changed meanwhile).
# Only `query tx <hash>` gives the execution result.
# Note: the anchored key cannot be read back - `ValidatorVrfPubkey` (keeper.go:41) is written by the
# handler but exposed by no query (it is absent from autocli). Do not try to read it back through
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
      echo "[vrf] Confirmed: tx executed successfully in a block (code=0)."
      CONFIRMED=1
    else
      echo "[vrf] Execution failed - the tx was in the mempool but was rejected at block level (code=$QCODE):"
      printf '%s\n' "$QT" | grep -oE '"raw_log":"[^"]*"' | head -1 | sed 's/^/       /'
      echo "[vrf] The validator does not contribute to the seed. Do not ignore."
      exit 1
    fi
    break
  done
fi
# Three answers, never two. A tx rejected at block level exits 1 above ("The validator does not contribute
# to the seed"); a confirmed one falls through to 0. Failing to confirm within the window is neither: the
# tx may already be in a block. Exiting 0 would make "I could not look" read as "I looked and it is fine",
# and exiting 1 would report a failure nobody measured. Hence a distinct code, which the caller reads.
NOT_CONFIRMED=0
if [ "$CONFIRMED" != 1 ]; then
  echo "[vrf] not confirmed: the inclusion of $TXH could not be observed within 36 s."
  echo "      Neither a failure nor a success: the tx may already be in a block."
  echo "      Check by hand before counting this validator as contributing:"
  echo "        $D query tx $TXH --node $NODE"
  NOT_CONFIRMED=1
fi

echo
echo "[vrf] Important: (re)start your node with  DENDRA_VRF_KEY_FILE=$KEYFILE  so that it signs its"
echo "     vote extensions (otherwise the key is anchored but the node provides no proof = no contribution)."
echo "     Then check:  $D query jobs committee-seed-health --node $NODE   (latest_contributors must rise)"

# The status is rendered last, after the instructions above: they are needed whether or not the
# inclusion could be confirmed, and an early exit would have swallowed them.
[ "$NOT_CONFIRMED" = 1 ] && exit 2
exit 0
