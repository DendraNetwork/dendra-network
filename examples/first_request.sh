#!/usr/bin/env bash
# A first request to the Dendra public testnet, through the gateway's keyless path.
#
#   bash examples/first_request.sh
#
# The gateway serves the chat-completions API (/v1/chat/completions). It opens the job on chain and
# pays its fee from its own testnet account, so the keyless path needs no key, no wallet and no DNDR.
# DENDRA_GATEWAY_URL points the script at another gateway: https://testnet-api.dendranetwork.com/v1, or
# http://127.0.0.1:8651/v1 for a stack you run yourself. Both answer 401 without a key: set
# DENDRA_API_KEY and the script sends it as "Authorization: Bearer <key>". To ask something else, edit
# the "content" value in BODY.
set -euo pipefail

BASE="${DENDRA_GATEWAY_URL:-https://testnet-api.dendranetwork.com/demo/v1}"
BODY='{"model":"dendra-network","messages":[{"role":"user","content":"Capital of Australia? Answer in one word."}]}'
AUTH=()
if [ -n "${DENDRA_API_KEY:-}" ]; then
  AUTH=(-H "Authorization: Bearer $DENDRA_API_KEY")
fi

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

# The gateway answers once the miner has answered and the job is settled: up to DENDRA_TIMEOUT for the
# answer, then up to commit_wait for the miner's commit, plus two transactions. A shorter client timeout
# fails on the client side only, and the job goes on.
if ! CODE="$(curl -sS --max-time 600 -o "$OUT" -w '%{http_code}' -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} -d "$BODY" "$BASE/chat/completions")"; then
  echo "no answer from $BASE within 600 s, or the connection failed" >&2
  exit 1
fi

echo "HTTP $CODE"
cat "$OUT"
echo

# 200 carries choices[0].message.content and the "dendra" object (job_id, miner_id, verify, ...).
# A failed job (429, 502, 503, 504) carries an "error" object whose "code" names the cause: quota_exhausted,
# upstream_error or no_result, quota_unavailable, no_committee_response. A 403 from the content filter carries
# "code": "illegal_content" beside a plain "error" string. 400, 401, 404 and 413 carry a plain string.
[ "$CODE" = "200" ]
