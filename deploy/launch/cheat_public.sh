#!/usr/bin/env bash
# cheat_public.sh — PROVE THE SLASH on the public network, in one command.
# Registers a MOCK "cheater" miner (serves garbage instead of real inference), drives honest traffic through
# the public gateway until the cheater is drawn as PRIMARY and its job is audit-sampled, then verifies:
#   - the cheater's stake is SLASHED (committee judged it INVALID),
#   - the honest judges' stakes stay INTACT (zero false slash).
# The primary draw is stake-weighted, so a HIGH cheater stake makes it primary on most jobs ->
# fast capture. The 5 heterogeneous judges (started by judges5_public.sh) must already be running.
#
# Run on the PC that runs the synced node kit + the judge pool, dendrad+python3 in PATH.
# Usage (WSL, repo root):
#   HOST=api.dendranetwork.com tr -d '\r' < deploy/launch/cheat_public.sh | bash
#   # options (env): CHEAT_STAKE=300000  FUND_UDNDR=400000  MAXMIN=30  REVEAL=1(default)|0(mute)
set -u

HOST="${HOST:-api.dendranetwork.com}"
CHEAT_ID="${CHEAT_ID:-tricheur}"
CHEAT_STAKE="${CHEAT_STAKE:-300000}"     # >> judges' 60000 -> stake-weighted draw picks it as primary most jobs
FUND_UDNDR="${FUND_UDNDR:-400000}"       # bank balance (stake + margin; gas is free at min-gas=0)
FUND_FROM="${FUND_FROM:-validator}"
REVEAL="${REVEAL:-1}"                    # 1 = cheater reveals its fake (committee judges divergent -> hard slash);
                                         # 0 = mute (no reveal -> committee posts 0 after grace -> hard slash)
MAXMIN="${MAXMIN:-30}"                   # give up after this many minutes (MoE judges are slow on CPU)
REPO="${DENDRA_REPO:-$PWD}"
NODE_KIT="${NODE_KIT:-$REPO/deploy/testnet-node}"
NODE_COMPOSE="$NODE_KIT/docker-compose.yml"
MODEA="$REPO/services"
KEYDIR="${KEYDIR:-$HOME/.dendra-judges}"
LOG="${DENDRA_CHEAT_LOG:-/tmp/dendra-cheat}"; mkdir -p "$LOG" "$KEYDIR"
ART="$REPO/bench-results/slash-live-public-$(date -u +%Y%m%dT%H%M%SZ).json"; mkdir -p "$REPO/bench-results"

export DENDRA_NODE="tcp://$HOST:26657"
export DENDRA_RELAY="http://$HOST:8645"
export DENDRA_FAUCET="http://$HOST:4500"
export DENDRA_CHAIN_ID="${DENDRA_CHAIN_ID:-dendra}"

say(){ printf '%s\n' "$*"; }
die(){ printf '[cheat] FATAL: %s\n' "$*" >&2; exit 1; }
warn(){ printf '[cheat] WARN: %s\n' "$*"; }

# --- secrets from the launch env file (relay token + gateway API key) ---
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
[ -f "$ENVF" ] || die "launch env file not found ($ENVF) — need DENDRA_RELAY_TOKEN + DENDRA_API_KEY."
[ -n "${DENDRA_RELAY_TOKEN:-}" ] || DENDRA_RELAY_TOKEN="$(grep -E '^DENDRA_RELAY_TOKEN=' "$ENVF" | head -1 | cut -d= -f2-)"
API_KEY="${DENDRA_API_KEY:-$(grep -E '^DENDRA_API_KEY=' "$ENVF" | head -1 | cut -d= -f2-)}"
export DENDRA_RELAY_TOKEN
[ -n "${DENDRA_RELAY_TOKEN:-}" ] || die "DENDRA_RELAY_TOKEN empty (needed for the public relay)."
[ -n "${API_KEY:-}" ] || die "DENDRA_API_KEY empty (needed to drive the gateway)."

# --- pre-flight ---
command -v dendrad >/dev/null 2>&1 || die "dendrad not in PATH."
command -v python3 >/dev/null 2>&1 || die "python3 required."
[ -f "$MODEA/miner.py" ] || die "run from the repo root (services not found)."
[ -f "$NODE_COMPOSE" ] || die "node kit compose not found ($NODE_COMPOSE) — funds the cheater from its operator key."
curl -fsS -m 8 "http://$HOST:26657/status" >/dev/null 2>&1 || die "public RPC unreachable (http://$HOST:26657)."
curl -fsS -m 8 "http://$HOST:8651/health" >/dev/null 2>&1 || die "public gateway unreachable (http://$HOST:8651)."
JN=$(dendrad query jobs list-miner --output json --node "$DENDRA_NODE" 2>/dev/null | tr -d ' \n' | grep -oE '"miner_id":"juge[0-9]+"' | sort -u | wc -l)
[ "${JN:-0}" -ge 4 ] || warn "only $JN judge(s) registered — the audit floor is 4; start judges5_public.sh first or the slash may never resolve."

stake_of(){ dendrad query jobs get-miner "$1" --output json --node "$DENDRA_NODE" 2>/dev/null | tr -d ' \n' | grep -oE '"stake":"[0-9]+"' | grep -oE '[0-9]+' | head -1; }
bal_of(){ dendrad query bank balances "$1" --output json --node "$DENDRA_NODE" 2>/dev/null | tr -d ' \n' | grep -oE '"amount":"[0-9]+"' | grep -oE '[0-9]+' | head -1; }
fund_from_operator(){ local to="$1" amt="$2" out; out=$(docker compose -f "$NODE_COMPOSE" exec -T node dendrad tx bank send "$FUND_FROM" "$to" "${amt}udndr" --keyring-backend test --chain-id "$DENDRA_CHAIN_ID" --node "tcp://localhost:26657" --yes -o json 2>&1); echo "$out" | grep -q '"code":0' || { warn "funding tx not accepted: $(echo "$out" | tr -d '\n' | cut -c1-180)"; return 1; }; }

# --- 1) cheater key + funding (idempotent) ---
dendrad keys show "$CHEAT_ID" -a --keyring-backend test >/dev/null 2>&1 || dendrad keys add "$CHEAT_ID" --keyring-backend test >/dev/null 2>&1
CADDR="$(dendrad keys show "$CHEAT_ID" -a --keyring-backend test 2>/dev/null)"
[ -n "$CADDR" ] || die "no cheater address."
cur="$(bal_of "$CADDR")"; cur="${cur:-0}"
if [ "$cur" -lt "$CHEAT_STAKE" ] 2>/dev/null; then
  say "[cheat] funding $CADDR ($cur -> $FUND_UDNDR udndr) from '$FUND_FROM'"
  fund_from_operator "$CADDR" "$FUND_UDNDR" || warn "funding failed (operator balance?)"
  for _ in $(seq 1 20); do cur="$(bal_of "$CADDR")"; [ "${cur:-0}" -ge "$CHEAT_STAKE" ] 2>/dev/null && break; sleep 3; done
fi
[ "${cur:-0}" -ge "$CHEAT_STAKE" ] 2>/dev/null || die "cheater under required stake (bal=$cur < $CHEAT_STAKE)."

# --- 2) snapshot honest judges' stakes BEFORE (to prove zero false slash) ---
JUDGES="$(dendrad query jobs list-miner --output json --node "$DENDRA_NODE" 2>/dev/null | tr -d ' \n' | grep -oE '"miner_id":"juge[0-9]+"' | grep -oE 'juge[0-9]+' | sort -u)"
say "[cheat] honest judges under watch: $(echo $JUDGES | tr '\n' ' ')"
declare -A JB; for j in $JUDGES; do JB[$j]="$(stake_of "$j")"; done

# --- 3) start the MOCK cheater (+ reveal unless mute) ---
pkill -f "miner.py --id $CHEAT_ID" 2>/dev/null; pkill -f "reveal_worker.py --id $CHEAT_ID" 2>/dev/null; sleep 2
say "[cheat] starting MOCK cheater '$CHEAT_ID' (stake=$CHEAT_STAKE -> primary most jobs)"
DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" DENDRA_ALLOW_MOCK=1 DENDRA_MINER_STAKE="$CHEAT_STAKE" \
  python3 -u "$MODEA/miner.py" --id "$CHEAT_ID" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" --faucet "$DENDRA_FAUCET" --backend mock \
  >"$LOG/miner-$CHEAT_ID.log" 2>&1 &
i=0; while [ $i -lt 40 ] && [ ! -f "$KEYDIR/$CHEAT_ID.sk" ]; do i=$((i+1)); sleep 2; done
[ -f "$KEYDIR/$CHEAT_ID.sk" ] || { warn "cheater not registered yet (see $LOG/miner-$CHEAT_ID.log)"; }
if [ "$REVEAL" = 1 ]; then
  DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" python3 -u "$MODEA/reveal_worker.py" --id "$CHEAT_ID" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" >"$LOG/reveal-$CHEAT_ID.log" 2>&1 &
  say "  cheater REVEALS its fake -> committee judges divergent -> hard slash"
else
  say "  cheater MUTE (no reveal) -> committee posts 0 after grace -> hard slash"
fi
CS0="$(stake_of "$CHEAT_ID")"; CS0="${CS0:-$CHEAT_STAKE}"
say "[cheat] cheater stake BEFORE = $CS0"

# --- 4) drive traffic (background) + poll cheater stake until it drops (slash) or timeout ---
say "[cheat] driving gateway traffic; watching for the slash (max ${MAXMIN} min)..."
STOP="$LOG/stop"; rm -f "$STOP"
( while [ ! -f "$STOP" ]; do
    q=$((RANDOM % 90 + 10)); w=$((RANDOM % 90 + 10))
    curl -s -m 90 -X POST "http://$HOST:8651/v1/chat/completions" \
      -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
      -d "{\"model\":\"dendra\",\"messages\":[{\"role\":\"user\",\"content\":\"What is $q plus $w? Reply with just the number.\"}]}" >/dev/null 2>&1
    sleep 1
  done ) &
TRAFFIC=$!

SLASHED=0; CS1="$CS0"; deadline=$(( $(date +%s) + MAXMIN*60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 15
  CS1="$(stake_of "$CHEAT_ID")"; CS1="${CS1:-$CS0}"
  say "  ... cheater stake = $CS1 (before $CS0)"
  if [ "${CS1:-0}" -lt "${CS0:-0}" ] 2>/dev/null; then SLASHED=1; break; fi
done
touch "$STOP"; kill "$TRAFFIC" 2>/dev/null; pkill -f "miner.py --id $CHEAT_ID" 2>/dev/null; pkill -f "reveal_worker.py --id $CHEAT_ID" 2>/dev/null

# --- 5) verdict + artifact ---
HONEST_INTACT=1; declare -A JA
for j in $JUDGES; do JA[$j]="$(stake_of "$j")"; [ "${JA[$j]:-0}" -lt "${JB[$j]:-0}" ] 2>/dev/null && HONEST_INTACT=0; done
{
  echo "{"
  echo "  \"_provenance\": {\"date\": \"$(date -u +%FT%TZ)\", \"kit\": \"cheat_public\", \"host\": \"$HOST\", \"reveal\": $([ "$REVEAL" = 1 ] && echo true || echo false),"
  echo "    \"claim\": \"SLASH proof on the public network: a mock cheater is drawn primary, audited by the heterogeneous committee, and slashed; honest judges intact.\"},"
  echo "  \"cheater\": {\"id\": \"$CHEAT_ID\", \"stake_before\": ${CS0:-null}, \"stake_after\": ${CS1:-null}, \"slashed\": $([ "$SLASHED" = 1 ] && echo true || echo false)},"
  echo -n "  \"honest_judges\": {"; first=1; for j in $JUDGES; do [ $first = 1 ] || echo -n ", "; first=0; echo -n "\"$j\": {\"before\": ${JB[$j]:-null}, \"after\": ${JA[$j]:-null}}"; done; echo "},"
  echo "  \"honest_intact\": $([ "$HONEST_INTACT" = 1 ] && echo true || echo false),"
  echo "  \"slash_proven\": $([ "$SLASHED" = 1 ] && [ "$HONEST_INTACT" = 1 ] && echo true || echo false)"
  echo "}"
} > "$ART"

say ""
say "=============================================================================="
if [ "$SLASHED" = 1 ] && [ "$HONEST_INTACT" = 1 ]; then
  say "  ✅ SLASH PROVEN LIVE: cheater '$CHEAT_ID' stake $CS0 -> $CS1 (slashed), honest judges INTACT."
elif [ "$SLASHED" = 1 ]; then
  say "  ⚠️ cheater slashed ($CS0 -> $CS1) BUT an honest judge also lost stake — investigate (false slash?)."
else
  say "  ⏳ no slash observed within ${MAXMIN} min. Likely the cheater wasn't audited yet (MoE judges slow) or"
  say "     too few judges. Re-run with a longer MAXMIN, or check $LOG/ and the judge logs."
fi
say "  Artifact: $ART"
say "  Submit it alongside the C3 triplet artifact — this is the missing slash-proof part of the launch gate."
say "=============================================================================="
