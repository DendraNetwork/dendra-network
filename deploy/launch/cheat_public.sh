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
#   # options (env): CHEAT_MULT=5  CHEAT_STAKE=<udndr>  FUND_UDNDR=<udndr>  MAXMIN=30  REVEAL=1(default)|0(mute)
#   #   CHEAT_STAKE defaults to CHEAT_MULT x the chain's min_stake, read at run time.
set -u

HOST="${HOST:-api.dendranetwork.com}"
CHEAT_ID="${CHEAT_ID:-cheater}"
# ⛔ THE CHEATER'S STAKE IS DERIVED, AND IT HAS TO BE.
# It used to be a literal justified against the judges' own literal. Both were sized against a floor
# that has since moved: at 300000 against judges at the current floor, the stake-weighted draw would
# pick the cheater LESS often, not more -- the demonstration would quietly stop demonstrating -- and
# below the floor it would not even register. The multiple is what this script actually needs, so the
# multiple is what it states; the base comes from the chain.
CHEAT_STAKE="${CHEAT_STAKE:-}"           # empty = CHEAT_MULT x the chain's min_stake (see _resolve_cheat_stake)
CHEAT_MULT="${CHEAT_MULT:-5}"            # how far ABOVE the floor, so the stake-weighted draw favours it
FUND_UDNDR="${FUND_UDNDR:-}"             # empty = stake + margin, derived from the same reading
FUND_FROM="${FUND_FROM:-validator}"
REVEAL="${REVEAL:-1}"                    # 1 = cheater reveals its fake (committee judges divergent -> hard slash);
                                         # 0 = mute (no reveal -> committee posts 0 after grace -> hard slash)
MAXMIN="${MAXMIN:-30}"                   # give up after this many minutes (MoE judges are slow on CPU)
REPO="${DENDRA_REPO:-$PWD}"
NODE_KIT="${NODE_KIT:-$REPO/deploy/testnet-node}"
NODE_COMPOSE="$NODE_KIT/docker-compose.yml"

# _resolve_cheat_stake — the floor comes from the chain, the multiple from this script.
# An unreadable floor STOPS: funding below it registers nobody while the run prints progress, and a
# demonstration that registers nobody proves nothing while looking like it is working.
_resolve_cheat_stake(){
  local raw floor
  raw="$(curl -fsS -m 10 "http://$HOST:1317/dendra/jobs/v1/params" 2>/dev/null || true)"
  floor="$(printf '%s' "$raw" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(0)
v=(d.get("params") or {}).get("min_stake") if isinstance(d,dict) else None
if v not in (None,""):
    try: print(int(str(v)))
    except Exception: pass
' 2>/dev/null)"
  case "$floor" in
    ''|*[!0-9]*|0) die "could not read min_stake from http://$HOST:1317/dendra/jobs/v1/params -- refusing to guess the cheater stake." ;;
  esac
  [ -n "$CHEAT_STAKE" ] || CHEAT_STAKE=$(( floor * CHEAT_MULT ))
  if [ "$CHEAT_STAKE" -lt "$floor" ] 2>/dev/null; then
    die "CHEAT_STAKE=$CHEAT_STAKE is below the chain floor min_stake=$floor -- the cheater would not register."
  fi
  [ -n "$FUND_UDNDR" ] || FUND_UDNDR=$(( CHEAT_STAKE + CHEAT_STAKE / 4 ))
}
_resolve_cheat_stake
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
# ⚠ NOT an access token for the relay: `GET list` is OPEN since 2026-08-13, and reading needs nothing.
# It is a WRITE fallback — the mock workers this script spawns post UNSIGNED, so a relay left at
# DENDRA_RELAY_SIGN=enforce refuses them. Whoever runs this operates the stack, so they hold it.
[ -n "${DENDRA_RELAY_TOKEN:-}" ] || die "DENDRA_RELAY_TOKEN empty (write fallback for the unsigned mock workers; reading the relay needs no token)."
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

# ZERO RULE: a chain answer is PARSED, never grepped. The proto3 codec OMITS a field at its zero
# value, so on a SUCCESSFUL transaction `code` is ABSENT -- a textual predicate reads that as "no
# answer" and refuses what the chain accepted. `deploy/launch/launch_public.sh` records the cost: a
# gateway seeding that had gone through was reported refused. What proves an answer arrived is
# `txhash`, which is never zero and so never omitted; once it is there, an absent `code` IS a zero.
# Three answers stay apart: executed (0) / refused (non-zero) / no usable answer (empty output).
tx_code() { # reads a broadcast or `query tx` answer on stdin; prints its code, or NOTHING
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
if isinstance(d, dict) and d.get("txhash"): print(d.get("code", 0))
' 2>/dev/null
}

# ZERO RULE. `stake` is a uint64 and the proto3 codec OMITS a field at its zero value, so a miner
# slashed to EXACTLY zero carries no `stake` in the answer and a textual read returns nothing. This
# script exists to PROVE a slash: reading "nothing" as "unchanged" makes the hardest slash -- the one
# that takes the whole bond -- come out as "no slash", inside a signed artifact. The slash is clamped
# to the bond, so exactly zero is the normal shape of a maximal slash, not an edge case.
# The answer is parsed, the default is the ZERO OF THE TYPE, and a query that did not answer gets its
# own distinct sentinel `?`: "this miner has nothing left" and "the node said nothing" are different
# facts, and only one of them proves anything.
# The correct shape already lived here: `dendra/onchain-staging/dendra_b02_slash_live.sh`.
stake_of(){ dendrad query jobs get-miner "$1" --output json --node "$DENDRA_NODE" 2>/dev/null | python3 -c 'import sys, json
try: print(json.load(sys.stdin).get("miner", {}).get("stake", 0))
except Exception: print("?")'; }
bal_of(){ dendrad query bank balances "$1" --output json --node "$DENDRA_NODE" 2>/dev/null | python3 -c 'import sys, json
try:
    b = json.load(sys.stdin).get("balances", [])
    print(next((int(c.get("amount", 0)) for c in b if c.get("denom") == "udndr"), 0))
except Exception: print("?")'; }
fund_from_operator(){ local to="$1" amt="$2" out; out=$(docker compose -f "$NODE_COMPOSE" exec -T node dendrad tx bank send "$FUND_FROM" "$to" "${amt}udndr" --keyring-backend test --chain-id "$DENDRA_CHAIN_ID" --node "tcp://localhost:26657" --yes -o json 2>&1); [ "$(printf '%s' "$out" | tx_code)" = "0" ] || { warn "funding tx not accepted: $(echo "$out" | tr -d '\n' | cut -c1-180)"; return 1; }; }

# --- 1) cheater key + funding (idempotent) ---
dendrad keys show "$CHEAT_ID" -a --keyring-backend test >/dev/null 2>&1 || dendrad keys add "$CHEAT_ID" --keyring-backend test >/dev/null 2>&1
CADDR="$(dendrad keys show "$CHEAT_ID" -a --keyring-backend test 2>/dev/null)"
[ -n "$CADDR" ] || die "no cheater address."
cur="$(bal_of "$CADDR")"; case "$cur" in ''|'?') warn "balance NOT MEASURED (node unreachable?) -- treated as 0"; cur=0;; esac
if [ "$cur" -lt "$CHEAT_STAKE" ] 2>/dev/null; then
  say "[cheat] funding $CADDR ($cur -> $FUND_UDNDR udndr) from '$FUND_FROM'"
  fund_from_operator "$CADDR" "$FUND_UDNDR" || warn "funding failed (operator balance?)"
  for _ in $(seq 1 20); do cur="$(bal_of "$CADDR")"; case "$cur" in ''|'?') cur=0;; esac; [ "$cur" -ge "$CHEAT_STAKE" ] 2>/dev/null && break; sleep 3; done
fi
[ "$cur" -ge "$CHEAT_STAKE" ] 2>/dev/null || die "cheater under required stake (bal=$cur < $CHEAT_STAKE)."

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
CS0="$(stake_of "$CHEAT_ID")"
case "$CS0" in ''|'?') warn "stake BEFORE not measured -- falling back on the configured $CHEAT_STAKE; the proof is weaker by exactly that much"; CS0="$CHEAT_STAKE";; esac
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

SLASHED=0; CS1="$CS0"; CS1_LAST="$CS0"; deadline=$(( $(date +%s) + MAXMIN*60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 15
  CS1="$(stake_of "$CHEAT_ID")"
  # `?` means the node did not answer. It is NOT "the stake is unchanged": defaulting it that way is
  # exactly what turns a total slash into "no slash". Skip the round, keep the last measured value.
  case "$CS1" in ''|'?') say "  ... cheater stake NOT MEASURED this round (node silent)"; CS1="$CS1_LAST"; continue;; esac
  CS1_LAST="$CS1"
  say "  ... cheater stake = $CS1 (before $CS0)"
  if [ "$CS1" -lt "$CS0" ] 2>/dev/null; then SLASHED=1; break; fi
done
touch "$STOP"; kill "$TRAFFIC" 2>/dev/null; pkill -f "miner.py --id $CHEAT_ID" 2>/dev/null; pkill -f "reveal_worker.py --id $CHEAT_ID" 2>/dev/null

# --- 5) verdict + artifact ---
# A judge whose stake cannot be READ is not a judge that was slashed. The previous default turned
# every unanswered query into a false accusation against an honest participant.
# A `?` is not a number: emitting it raw would put INVALID JSON inside the very artifact the public
# claim rests on. Unknown becomes `null`, which a reader can tell apart from a measured value.
jnum(){ case "${1:-}" in ''|'?') printf 'null';; *) printf '%s' "$1";; esac; }
HONEST_INTACT=1; HONEST_UNKNOWN=0; declare -A JA
for j in $JUDGES; do
  JA[$j]="$(stake_of "$j")"
  case "${JA[$j]}" in ''|'?') HONEST_UNKNOWN=1; warn "judge $j stake NOT MEASURED -- intactness cannot be asserted"; continue;; esac
  case "${JB[$j]}" in ''|'?') HONEST_UNKNOWN=1; continue;; esac
  [ "${JA[$j]}" -lt "${JB[$j]}" ] 2>/dev/null && HONEST_INTACT=0
done
{
  echo "{"
  echo "  \"_provenance\": {\"date\": \"$(date -u +%FT%TZ)\", \"kit\": \"cheat_public\", \"host\": \"$HOST\", \"reveal\": $([ "$REVEAL" = 1 ] && echo true || echo false),"
  echo "    \"claim\": \"SLASH proof on the public network: a mock cheater is drawn primary, audited by the heterogeneous committee, and slashed; honest judges intact.\"},"
  echo "  \"cheater\": {\"id\": \"$CHEAT_ID\", \"stake_before\": $(jnum "$CS0"), \"stake_after\": $(jnum "$CS1"), \"slashed\": $([ "$SLASHED" = 1 ] && echo true || echo false)},"
  echo -n "  \"honest_judges\": {"; first=1; for j in $JUDGES; do [ $first = 1 ] || echo -n ", "; first=0; echo -n "\"$j\": {\"before\": $(jnum "${JB[$j]}"), \"after\": $(jnum "${JA[$j]}")}"; done; echo "},"
  echo "  \"honest_intact\": $( [ "$HONEST_UNKNOWN" = 1 ] && echo null || { [ "$HONEST_INTACT" = 1 ] && echo true || echo false; } ),"
  echo "  \"slash_proven\": $([ "$SLASHED" = 1 ] && [ "$HONEST_INTACT" = 1 ] && [ "$HONEST_UNKNOWN" = 0 ] && echo true || echo false)"
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
