#!/usr/bin/env bash
# judges5_public.sh — start a heterogeneous audit-judge pool (5 miner-judges) against the public Dendra network,
# in one command.
#
# Sizing — the slash bar is relative, not a fixed "4 out of 5". The draw targets 15 seats
# (`auditCommitteeDrawSize`, chain/x/jobs/keeper/audit_committee.go); the number of seats actually anchored
# is min(draw size, eligible judge-miners). A hard slash then requires ceil(2/3) of the anchored seats to vote
# "invalid" and a strict majority of the voting stake (chain/x/jobs/keeper/antievasion.go, auditRelativeBar).
# With five registered judge-miners, five seats are anchored and the bar is four of five — which is why this pool
# is sized at five. As the eligible pool grows the bar grows with it: sizing a pool on "4 out of 5" and then
# joining a network of fifteen judges buys 5/15 = 33 % of the seats, not a majority.
#
# Every seat runs the judge model the chain names (modelregistry audit_judge_model) on the CPU, and the
# probe seats a judge only there (MOE_CPU_MIN_RAM_MB), so this launcher provides no judge-model diversity.
# The chain does not enforce judge models on-chain.
#
# Each judge = a native miner (also serves inference) + reveal_worker + judge_worker, sharing one
# on-chain identity, pointed at the public RPC/relay. Judges are funded from the local node's operator key
# (the public faucet is PoW-gated and miner self-funding does not solve PoW).
#
# Run on the PC that already runs the synced node kit (deploy/testnet-node), with dendrad + python3 + ollama in
# PATH (a GPU is optional: the miners serve on it when present). Idempotent: re-running re-funds only if
# needed and restarts the workers.
#
# Usage (WSL, repo root):
#   HOST=testnet-api.dendranetwork.com tr -d '\r' < deploy/launch/judges5_public.sh | bash
#   # options (env): JUDGES=5  MOE_COUNT=3  KEYDIR=~/.dendra-judges  FUND_UDNDR=200000
#   #                MOE_MODEL=qwen3:30b-a3b-instruct-2507-q4_K_M  NEMO_MODEL=mistral-nemo
#   #                NODE_KIT=deploy/testnet-node (source of the operator 'validator' key for funding)
set -u

HOST="${HOST:-testnet-api.dendranetwork.com}"
JUDGES="${JUDGES:-5}"
# Models. Empty by default = auto-sized from the measured hardware (deploy/hw_probe.sh, resolved below).
# Hard-coded defaults were a trap: `mistral-nemo` (8.7 GB) was handed to an 8 GB card, Ollama offloaded
# 70% of it to CPU, inference blew past the deadline (ReadTimeout) and jobs never settled. Never size a
# model from a guess — size it from the VRAM that is actually there.
# Co-resident identities on one box now share one model (a single copy in VRAM instead of N families
# thrashing); committee heterogeneity comes from different machines, which hw_probe.sh spreads by
# hashing the machine id. Explicit overrides still win.
MOE_COUNT="${MOE_COUNT:-3}"
MOE_MODEL="${MOE_MODEL:-}"
NEMO_MODEL="${NEMO_MODEL:-}"
KEYDIR="${KEYDIR:-$HOME/.dendra-judges}"
OLD_JUDGE_PREFIX="juge"   # the judge name prefix of keys created before 2026-10-10, read only to migrate them and stop old workers
# The stake is read from the chain, never typed here. The floor is governed: once it moves, a literal
# value here would fund every judge below the bar, every `create-miner` would be refused, and this
# script would still report them as funded. The values are resolved from
# `/dendra/jobs/v1/params` a few lines down, and an explicit override still wins.
MINER_STAKE="${MINER_STAKE:-}"               # empty = read min_stake from the chain (see _resolve_stake)
FUND_UDNDR="${FUND_UDNDR:-}"                 # empty = stake + margin, derived from the same reading
FUND_FROM="${FUND_FROM:-validator}"          # operator key inside the node container that funds the judges
REPO="${DENDRA_REPO:-$PWD}"
NODE_KIT="${NODE_KIT:-$REPO/deploy/testnet-node}"
MODEA="$REPO/services"
LOG="${DENDRA_JUDGE_LOG:-/tmp/dendra-judges}"; mkdir -p "$LOG" "$KEYDIR"

export DENDRA_NODE="tcp://$HOST:26657"
export DENDRA_RELAY="http://$HOST:8645"
export DENDRA_FAUCET="http://$HOST:4500"
# Read from the network, never defaulted: the default was the previous chain's name.
export DENDRA_CHAIN_ID="${DENDRA_CHAIN_ID:?set DENDRA_CHAIN_ID to the CHAIN_ID of network-info.txt}"

say(){ printf '%s\n' "$*"; }
die(){ printf '[judges] FATAL: %s\n' "$*" >&2; exit 1; }
warn(){ printf '[judges] WARN: %s\n' "$*"; }

# --- relay token (write fallback only) : from the launch env file, never hardcoded ---
# The public relay does not require it to serve a worker: `GET list` — the only route a worker needs
# to learn a job awaits it — is open to anyone. This is a fallback for unsigned writes, nothing more.
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
if [ -z "${DENDRA_RELAY_TOKEN:-}" ] && [ -f "$ENVF" ]; then
  DENDRA_RELAY_TOKEN="$(grep -E '^DENDRA_RELAY_TOKEN=' "$ENVF" | head -1 | cut -d= -f2-)"
fi
[ -n "${DENDRA_RELAY_TOKEN:-}" ] || warn "DENDRA_RELAY_TOKEN empty (set it in $ENVF) — reads are unaffected; what breaks is these workers' unsigned writes, and only on a relay set to DENDRA_RELAY_SIGN=enforce."
export DENDRA_RELAY_TOKEN

# --- pre-flight ---
command -v dendrad >/dev/null 2>&1 || die "dendrad not in PATH (the node kit builds it inside Docker; install the CLI on the host or run from the node container)."
command -v python3 >/dev/null 2>&1 || die "python3 required."
command -v ollama  >/dev/null 2>&1 || die "ollama required (the miners serve on it; the judge runs on a CPU-only instance)."
[ -f "$MODEA/miner.py" ] || die "run from the repo root (services not found)."
curl -fsS -m 8 "http://$HOST:26657/status" >/dev/null 2>&1 || die "public RPC unreachable (http://$HOST:26657) — is the network up?"

# _resolve_stake — MINER_STAKE and FUND_UDNDR from the chain's own `min_stake`, or a loud refusal.
#
# Read through the REST gateway, parsed as JSON: `min_stake` is a string in proto JSON, and a grep for
# a digit run would happily match another parameter's value. If the parameter cannot be read we do not
# fall back to a number — funding below the floor registers nobody while printing success, which is the
# exact failure this replaces. An unreadable floor is a stop, not a default.
_resolve_stake(){
  local raw floor
  raw="$(curl -fsS -m 10 "http://$HOST:1317/dendra/jobs/v1/params" 2>/dev/null || true)"
  floor="$(printf '%s' "$raw" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(0)
p=d.get("params") if isinstance(d,dict) else None
v=(p or {}).get("min_stake")
if v not in (None,""):
    try: print(int(str(v)))
    except Exception: pass
' 2>/dev/null)"
  case "$floor" in
    ''|*[!0-9]*|0) die "could not read min_stake from http://$HOST:1317/dendra/jobs/v1/params — refusing to guess a stake. Funding below the floor registers nobody and reports success." ;;
  esac
  [ -n "$MINER_STAKE" ] || MINER_STAKE="$floor"
  if [ "$MINER_STAKE" -lt "$floor" ] 2>/dev/null; then
    die "MINER_STAKE=$MINER_STAKE is below the chain floor min_stake=$floor — create-miner would be refused for every judge."
  fi
  # Margin over the stake: the bank balance must cover the stake and leave the account non-empty after
  # it is locked. A quarter of the floor is arbitrary and stated as such; it is not read from anywhere.
  [ -n "$FUND_UDNDR" ] || FUND_UDNDR=$(( MINER_STAKE + MINER_STAKE / 4 ))
  say "  [judges] stake per judge = $MINER_STAKE udndr (chain min_stake=$floor), funding = $FUND_UDNDR udndr"
}
_resolve_stake

NODE_COMPOSE="$NODE_KIT/docker-compose.yml"
[ -f "$NODE_COMPOSE" ] || die "node kit compose not found ($NODE_COMPOSE) — this script funds judges from its operator key."

# --- hardware gate: is this box allowed to judge? -----------------------------------------------
# A judge that is too weak does not merely judge badly: it votes divergent on answers that are correct
# but worded differently, and an unfair verdict costs an honest miner its stake. deploy/hw_probe.sh
# gates the role on the system RAM and on an allow-list of validated judge models: it seats only the
# MoE judge on the CPU (MOE_CPU_MIN_RAM_MB); below that this box may mine, but must not seat a judge.
# No machine identifier is passed here: hw_probe.sh derives its own pseudonym (a hash of the machine).
# Passing `$(hostname)` overrides that pseudonym and publishes the real host name in clear text in the
# public registry and on the /network page. The anonymisation lives in the probe and must not be undone
# by its caller — a guard bypassed by the code that calls it protects nothing.
HW_JSON="$(bash "$REPO/deploy/hw_probe.sh" --json 2>/dev/null || echo '{}')"
CAN_JUDGE="$(printf '%s' "$HW_JSON" | grep -o '"can_judge":[a-z]*' | cut -d: -f2)"; [ -n "$CAN_JUDGE" ] || CAN_JUDGE=false
HW_TIER="$(printf '%s' "$HW_JSON" | grep -o '"tier":[0-9]*' | cut -d: -f2)"
if [ "$CAN_JUDGE" != "true" ]; then
  warn "the probe grants no judge seat -> this box will mine but not judge (an under-powered judge slashes"
  warn "honest miners). The probe seats a judge on the CPU only: the network's pinned MoE judge model, which"
  warn "needs MOE_CPU_MIN_RAM_MB of system RAM, with or without a GPU. deploy/hw_probe.sh is the arbiter."
  warn "Override at your own risk: DENDRA_FORCE_JUDGE=1"
  [ "${DENDRA_FORCE_JUDGE:-0}" = "1" ] && { CAN_JUDGE=true; warn "DENDRA_FORCE_JUDGE=1 -> judges started anyway."; }
fi

# Auto-size the model from the probe unless the operator pinned one. This is what stops a node from
# ever loading a model bigger than its card again (the CPU-offload -> ReadTimeout -> unsettled-job chain).
HW_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"model":"[^"]*"' | cut -d'"' -f4)"
# The judge model can differ from the mining model: on a small-GPU/large-RAM box the probe seats a
# MoE judge on the CPU (few active params -> usable) while the GPU keeps serving inference at speed.
HW_JUDGE_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"judge_model":"[^"]*"' | cut -d'"' -f4)"
HW_MOE_CPU="$(printf '%s' "$HW_JSON" | grep -o '"moe_cpu":[0-9]*' | cut -d: -f2)"
[ "${HW_MOE_CPU:-0}" = "1" ] && say "  [hw] CPU-MoE judge path: '$HW_JUDGE_MODEL' on CPU — all seats use the same model, so Ollama keeps one copy resident (judgments serialise; the audit quorum needs several seats)."
if [ -z "$MOE_MODEL" ]; then
  # No model of its own choosing. The probe names the model this box serves (`model`: the card's pick, or the
  # judge model on the CPU), and names none when the box has no role on the testnet (deploy/hw_probe.sh --role).
  # A fallback model here would serve a mining model on a CPU the probe refused, so nothing is started instead.
  [ -n "$HW_MODEL" ] || die "deploy/hw_probe.sh names no model for this box (bash deploy/hw_probe.sh --role says why): the testnet runs no mining model on the CPU. Nothing was started."
  MOE_MODEL="$HW_MODEL"
  say "  [hw] tier ${HW_TIER:-?} -> auto-sized model: $MOE_MODEL"
fi
[ -n "$NEMO_MODEL" ] || NEMO_MODEL="$MOE_MODEL"   # one model per box: a single copy resident in VRAM

# The capacity registry is not fed from here. It keeps a signed report per identity (the kit's
# deploy/testnet-miner/publish-capacity.sh signs with the miner's key, after its registration is read on chain),
# and this launcher holds no signer: an unsigned report of `judge1..N` would be filed apart from any signed one,
# as a declaration nobody can attribute.
say "  [hw] capacity: not published from this launcher (the registry keeps signed reports; see deploy/testnet-miner/publish-capacity.sh)."

# fund one address from the operator key held in the node container (min-gas=0 -> no fee)
# A chain answer is parsed, never grepped. The proto3 codec omits a field at its zero value, so on a
# successful transaction `code` is absent, and a textual predicate would read that as "no answer" and
# refuse what the chain accepted (a gateway seeding that went through was once reported refused). What
# proves an answer arrived is `txhash`, which is never zero and so never omitted; once it is there, an
# absent `code` is a zero.
# Three answers stay apart: executed (0) / refused (non-zero) / no usable answer (empty output).
tx_code() { # reads a broadcast or `query tx` answer on stdin; prints its code, or nothing
  python3 -c '
import json, sys
t = sys.stdin.read(); i = t.find("{")
if i < 0: sys.exit(0)
try: d, _ = json.JSONDecoder().raw_decode(t[i:])
except Exception: sys.exit(0)
if isinstance(d, dict) and d.get("txhash"): print(d.get("code", 0))
' 2>/dev/null
}

fund_from_operator(){
  local to="$1" amt="$2" out
  out=$(docker compose -f "$NODE_COMPOSE" exec -T node \
    dendrad tx bank send "$FUND_FROM" "$to" "${amt}udndr" \
      --keyring-backend test --chain-id "$DENDRA_CHAIN_ID" --node "$DENDRA_NODE" --yes -o json 2>&1)
  [ "$(printf '%s' "$out" | tx_code)" = "0" ] || { warn "funding tx not accepted: $(echo "$out" | tr -d '\n' | cut -c1-180)"; return 1; }
}

bal_of(){ # udndr balance of an address; 0 when the account holds none, `?` when nothing answered
  # Zero rule. A coin at zero is simply absent from `balances`, and a textual read cannot tell that
  # apart from a node that said nothing -- both come back empty. The answer is parsed, the denom is
  # selected (taking the first amount picks whichever coin happens to come first), the default is the
  # zero of the type, and a failed query keeps its own sentinel.
  dendrad query bank balances "$1" --output json --node "$DENDRA_NODE" 2>/dev/null | python3 -c 'import sys, json
try:
    b = json.load(sys.stdin).get("balances", [])
    print(next((int(c.get("amount", 0)) for c in b if c.get("denom") == "udndr"), 0))
except Exception: print("?")'
}

model_for(){ [ "$1" -le "$MOE_COUNT" ] && echo "$MOE_MODEL" || echo "$NEMO_MODEL"; }

# _migrate_judge_name <k>: moves judge k from its old name (OLD_JUDGE_PREFIX) to judge<k>.
# The keyring entry is renamed, never created again: the address stays the same, and so does the stake
# registered from it. Its key files in KEYDIR (encryption, VRF and attestation keys) move with it.
# Returns 0 when judge<k> can be used (migrated now, or nothing to migrate) and 1 when the judge must be
# skipped. Creating judge<k> next to a surviving old key would register a new, unfunded identity and
# strand the old stake, so every doubtful case stops here and says what to resolve by hand.
# The files move first and move back if anything fails: a run that stops halfway leaves everything
# under the old name, where the next run finds it and tries again. `mv -n` never overwrites, so a file
# already present under the new name stops the move instead of being replaced.
_migrate_judge_name(){
  local old="$OLD_JUDGE_PREFIX$1" new="judge$1" oaddr naddr ext moved=""
  oaddr="$(dendrad keys show "$old" -a --keyring-backend test 2>/dev/null)"
  [ -n "$oaddr" ] || return 0
  naddr="$(dendrad keys show "$new" -a --keyring-backend test 2>/dev/null)"
  if [ -n "$naddr" ]; then
    warn "keys '$old' ($oaddr) and '$new' ($naddr) both exist: $new skipped. Keep the one that holds the stake and delete the other by hand."
    return 1
  fi
  for ext in sk vrf attestkey; do
    [ -e "$KEYDIR/$old.$ext" ] || continue
    mv -n "$KEYDIR/$old.$ext" "$KEYDIR/$new.$ext" 2>/dev/null
    if [ -e "$KEYDIR/$old.$ext" ] || [ ! -e "$KEYDIR/$new.$ext" ]; then
      for ext in $moved; do mv -n "$KEYDIR/$new.$ext" "$KEYDIR/$old.$ext" 2>/dev/null; done
      warn "$KEYDIR/$old.* could not be moved to $new.* (a file of that name exists, or the directory is read-only): $new skipped, nothing renamed."
      return 1
    fi
    moved="$moved $ext"
  done
  dendrad keys rename "$old" "$new" -y --keyring-backend test >/dev/null 2>&1
  naddr="$(dendrad keys show "$new" -a --keyring-backend test 2>/dev/null)"
  if [ "$naddr" != "$oaddr" ]; then
    if [ "$(dendrad keys show "$old" -a --keyring-backend test 2>/dev/null)" = "$oaddr" ]; then
      for ext in $moved; do mv -n "$KEYDIR/$new.$ext" "$KEYDIR/$old.$ext" 2>/dev/null; done
      warn "renaming key '$old' to '$new' failed: $new skipped, no key created, everything left under '$old'."
    else
      warn "renaming key '$old' to '$new' left neither name resolving to $oaddr: $new skipped. Check 'dendrad keys list --keyring-backend test'; the key files are under '$new'."
    fi
    return 1
  fi
  say "  [$new] renamed from '$old': same key and address ($oaddr), so the same stake"
  return 0
}

# --- pull the models once (idempotent): served model + embeddings (a judge-miner can also be drawn as
#     the primary, so it must be able to serve inference) + the judge models ---
SERVED_MODEL="${DENDRA_MODEL_ID:-llama3.1:8b-instruct-q4_K_M}"
EMBED_MODEL="${DENDRA_EMBED_API_MODEL:-nomic-embed-text}"
say "== [judges] pulling models (once) =="
_pulled=""
for m in "$SERVED_MODEL" "$EMBED_MODEL" ${HW_JUDGE_MODEL:-} $(for k in $(seq 1 "$JUDGES"); do model_for "$k"; done); do
  case "$_pulled" in *"|$m|"*) continue;; esac; _pulled="$_pulled|$m|"
  say "  ollama pull $m"; ollama pull "$m" >/dev/null 2>&1 || warn "pull $m failed (will retry at first use)"
done

# --- stop any previous judge workers (clean restart), those started under the old names included ---
for w in reveal_worker judge_worker miner; do
  pkill -f "$w.py --id ($OLD_JUDGE_PREFIX|judge)[0-9]" 2>/dev/null || true
done
sleep 2

# --- evict a stale judge model from the GPU instance (measured trap) ---
# Killing the workers does not unload the model: Ollama keeps it resident for its keep-alive window. A
# previous run that judged on :11434 therefore leaves ~19 GB of MoE sitting on the GPU, and the miners we
# are about to start probe a card that is already full -> a fatal Ollama-unreachable ReadTimeout, zero
# miners, every job stuck `open`. The judge model belongs on :11435 (CPU) only, so
# evicting it from :11434 is always correct here — never touches the mining model.
if [ "${HW_MOE_CPU:-0}" = "1" ] && [ -n "${HW_JUDGE_MODEL:-}" ]; then
  if ollama ps 2>/dev/null | grep -q -- "$HW_JUDGE_MODEL"; then
    say "  [judges] evicting '$HW_JUDGE_MODEL' from the GPU instance (leftover of a previous run — it would starve the miners)"
    ollama stop "$HW_JUDGE_MODEL" >/dev/null 2>&1 || true
    sleep 2
  fi
fi

# --- create keys, fund, and launch each judge ---
MIGRATION_SKIPPED=""   # judges whose old-name key could not be migrated: started in neither phase
for k in $(seq 1 "$JUDGES"); do
  jid="judge$k"; JM="$(model_for "$k")"
  # 0) a key left under the old name is renamed first, or step 1 would create a second identity
  _migrate_judge_name "$k" || { MIGRATION_SKIPPED="$MIGRATION_SKIPPED $k"; continue; }
  # 1) key (idempotent) + address
  dendrad keys show "$jid" -a --keyring-backend test >/dev/null 2>&1 \
    || dendrad keys add "$jid" --keyring-backend test >/dev/null 2>&1
  addr="$(dendrad keys show "$jid" -a --keyring-backend test 2>/dev/null)"
  [ -n "$addr" ] || { warn "$jid: no address, skipped"; continue; }
  # 2) fund from operator only if under the required stake (idempotent). Threshold = MINER_STAKE, not
  #    FUND_UDNDR: an already-registered judge keeps well above stake, so we must not re-fund it (5 rapid
  #    sends from one operator account collide on the account sequence). We wait for tx inclusion (balance
  #    rises) between judges, which serializes the sends and avoids the nonce race.
  cur="$(bal_of "$addr")"; case "$cur" in ''|'?') warn "balance not measured for $addr, treated as 0"; cur=0;; esac
  if [ "$cur" -lt "$MINER_STAKE" ] 2>/dev/null; then
    say "  [$jid] funding $addr ($cur -> $FUND_UDNDR udndr) from '$FUND_FROM'"
    fund_from_operator "$addr" "$FUND_UDNDR" || warn "$jid funding tx failed (operator balance? see node logs)"
    for _ in $(seq 1 20); do cur="$(bal_of "$addr")"; case "$cur" in ''|'?') cur=0;; esac; [ "$cur" -ge "$MINER_STAKE" ] 2>/dev/null && break; sleep 3; done
  fi
  # Three counters, because they count three different things. Counting `SEATED` on a balance would let
  # a box with can_judge=false fund five accounts and announce "5/5 judges actually seated" while no judge
  # process exists. Being funded is not being registered, and being registered is
  # not judging: only a spawned `judge_worker` posts a verdict.
  #   FUNDED     -> the account holds the stake (this line)
  #   REGISTERED -> create-miner landed, the sk file exists (below)
  #   SEATED     -> a judge worker was actually started (phase 2 only)
  if [ "${cur:-0}" -ge "$MINER_STAKE" ] 2>/dev/null; then FUNDED=$((${FUNDED:-0}+1))
  else warn "$jid under required stake (bal=$cur < $MINER_STAKE) — it will not register; check operator balance."; fi
  # 3) miner-judge daemon (self-registers create-miner once funded), then reveal + judge workers
  say "  [$jid] start (model=$JM, stake=$MINER_STAKE)"
  # DENDRA_RELAY_TOKEN passed explicitly to every process (the public relay rejects untokened requests, and
  # relay_client reads it at import time) — belt-and-suspenders on top of the global export.
  # OLLAMA_MODEL/DENDRA_MODEL_ID: without these the miner ignores the probe entirely and falls back to
  # the hard-coded default of modea/inference.py — i.e. the whole "never load a model bigger than the
  # card" fix had no effect on the served inference. Measured gap, fixed here.
  DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" DENDRA_MINER_JUDGE=1 DENDRA_JUDGE_MODEL="$JM" DENDRA_JUDGE_MODEL_ID="$JM" DENDRA_MINER_STAKE="$MINER_STAKE" \
  OLLAMA_MODEL="$JM" DENDRA_MODEL_ID="$JM" \
    python3 -u "$MODEA/miner.py" --id "$jid" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" --faucet "$DENDRA_FAUCET" --backend ollama \
    >"$LOG/miner-$jid.log" 2>&1 &
  # wait for the on-chain identity (sk file) before starting the workers
  i=0; while [ $i -lt 40 ] && [ ! -f "$KEYDIR/$jid.sk" ]; do i=$((i+1)); sleep 2; done
  if [ -f "$KEYDIR/$jid.sk" ]; then REGISTERED=$((${REGISTERED:-0}+1))
  else warn "$jid not registered yet (see $LOG/miner-$jid.log) — continuing"; fi
  DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" python3 -u "$MODEA/reveal_worker.py" --id "$jid" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" \
    >"$LOG/reveal-$jid.log" 2>&1 &
done

# --- Phase 2: seat the audit committee, only once every miner has passed its Ollama probe --------
# Order matters, and getting it wrong kills the miners:
#   `miner` probes Ollama at boot and exits with a fatal error on an unreachable-Ollama timeout (the mock
#   backend is forbidden in production). A judge loading a 20 GB MoE on CPU makes Ollama unresponsive for
#   minutes.
#   Starting judge N inside the loop therefore asphyxiated miners N+1..5 one by one — the network then
#   had judges but no miner, and every job stayed `open` until the client gave up.
# So: all miners first (Ollama free, probes succeed), committee afterwards.
# Seat count: `AdjudicateDispute` needs audit_min_quorum (4) verdicts out of a 5-seat committee. A single
# judge makes `AdjudicateDispute` report an incomplete fresh committee forever
# (chain/x/jobs/keeper/msg_server_adjudicate.go) and no dispute ever closes. All seats share the same
# model, so Ollama keeps one copy of the weights resident and serialises: the cost is latency, not memory.
if [ "$CAN_JUDGE" = "true" ]; then
  say "== [judges] miners up — seating the audit committee =="
  # A dedicated CPU Ollama for the committee. Sharing one instance does not work, and ordering alone does
  # not fix it: the committee loads a 20 GB MoE and serialises minute-long CPU generations, which evicts
  # the miner's GPU model and starves its inference until `miner` hits an Ollama ReadTimeout and
  # exits, leaving judges but no miner and every job stuck `open`.
  # Two roles, two queues:
  #   :11434  GPU  -> miners (paid inference, must stay fast)
  #   :11435  CPU  -> judges (slow by design, must never delay a paid job)
  # Same model store, so no re-download — only a second resident copy in RAM.
  JUDGE_OLLAMA="${DENDRA_JUDGE_OLLAMA:-http://127.0.0.1:11435}"
  if [ "${HW_MOE_CPU:-0}" = "1" ]; then
    if ! curl -fsS -m 3 "$JUDGE_OLLAMA/api/tags" >/dev/null 2>&1; then
      say "  [judges] starting a CPU-only Ollama for the committee on $JUDGE_OLLAMA (GPU stays for mining)"
      CUDA_VISIBLE_DEVICES="" OLLAMA_HOST="${JUDGE_OLLAMA#http://}" nohup ollama serve >"$LOG/ollama-judge.log" 2>&1 &
      for _ in $(seq 1 40); do curl -fsS -m 2 "$JUDGE_OLLAMA/api/tags" >/dev/null 2>&1 && break; sleep 2; done
    fi
    curl -fsS -m 3 "$JUDGE_OLLAMA/api/tags" >/dev/null 2>&1 \
      && say "  [judges] committee Ollama ready (CPU-only) — miners keep the GPU instance untouched" \
      || warn "committee Ollama did not come up on $JUDGE_OLLAMA — judges would starve the miners; see $LOG/ollama-judge.log"
  else
    JUDGE_OLLAMA="${OLLAMA_ENDPOINT:-http://127.0.0.1:11434}"   # GPU judge: same instance is fine
  fi
  for k in $(seq 1 "$JUDGES"); do
    jid="judge$k"; JJM="${HW_JUDGE_MODEL:-$(model_for "$k")}"
    case " $MIGRATION_SKIPPED " in *" $k "*) warn "$jid not seated: its key was not migrated (see above)"; continue ;; esac
    # DENDRA_EMBED_*: the judge falls back to a lexical hash embedder when unset (it then compares
    # words, not meaning). nomic-embed-text is already pulled above — actually wire it.
    # OLLAMA_TIMEOUT is raised on the CPU path: a MoE on CPU is correct but slow (~10 s/case).
    DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" OLLAMA_MODEL="$JJM" DENDRA_JUDGE_MODEL="$JJM" DENDRA_JUDGE_MODEL_ID="$JJM" \
    OLLAMA_ENDPOINT="$JUDGE_OLLAMA" DENDRA_JUDGE_ENDPOINT="$JUDGE_OLLAMA" \
    DENDRA_EMBED_MODE="${DENDRA_EMBED_MODE:-backend}" DENDRA_EMBED_API_MODEL="${DENDRA_EMBED_API_MODEL:-$EMBED_MODEL}" \
    OLLAMA_TIMEOUT="${DENDRA_JUDGE_OLLAMA_TIMEOUT:-$([ "${HW_MOE_CPU:-0}" = "1" ] && echo 600 || echo 240)}" \
    DENDRA_JUDGE_GEN_RETRIES="${DENDRA_JUDGE_GEN_RETRIES:-8}" \
      python3 -u "$MODEA/judge_worker.py" --id "$jid" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" --adjudicate --model-id "$JJM" \
      >"$LOG/judge-$jid.log" 2>&1 &
    SEATED=$((${SEATED:-0}+1)) # The only place SEATED grows: a judge seat exists once its worker runs.
    say "  [$jid] judge seated (model=$JJM$([ "${HW_MOE_CPU:-0}" = "1" ] && echo ", CPU MoE"))"
  done
else
  say "  audit committee not seated (system RAM under MOE_CPU_MIN_RAM_MB, see deploy/hw_probe.sh) — this box mines only"
fi

sleep 5
say ""
say "=============================================================================="
SEATED="${SEATED:-0}"; FUNDED="${FUNDED:-0}"; REGISTERED="${REGISTERED:-0}"
# Each number is reported separately, because collapsing them let this banner announce judges that did
# not exist. They also fail for different reasons: FUNDED short -> operator balance; REGISTERED short ->
# create-miner did not land; SEATED short -> the hardware refused the judge role.
say "  funded: $FUNDED/$JUDGES   registered on-chain: $REGISTERED/$JUDGES   judge seats running: $SEATED/$JUDGES"
if [ "${HW_MOE_CPU:-0}" = "1" ]; then
  say "  Pool started on '$MOE_MODEL' (GPU :11434) + judge seats on '$HW_JUDGE_MODEL' (CPU :11435)"
  say "  One judge model network-wide: every seat runs the model the chain names, so more operators add no judge diversity."
else
  say "  Judge pool started ($MOE_COUNT x '$MOE_MODEL' + $((JUDGES-MOE_COUNT)) x '$NEMO_MODEL' requested)"
fi
# The number of seats running decides whether a slash can fire at all: below the quorum floor the audit
# is inert and a cheater walks. Saying so here beats discovering it by staring at verdicts that never
# arrive. It is gated on SEATED and not on FUNDED: paying for five judges on a box that cannot judge
# leaves the audit exactly as inert as paying for none.
if [ "$SEATED" -lt 4 ] 2>/dev/null; then
  say "  WARNING: $SEATED judge seat(s) running < quorum floor (4) -> no hard slash can fire (the audit is inert)."
  if [ "$SEATED" = "0" ] && [ "$FUNDED" -gt 0 ] 2>/dev/null; then
    say "     $FUNDED account(s) were funded but no judge runs here: this box mines only (system RAM under MOE_CPU_MIN_RAM_MB)."
    say "     Seats have to come from another operator's machine — funding more accounts on this one changes nothing."
  else
    say "     Usual cause: the operator balance is too low to fund every judge. Check it, then re-run (idempotent)."
  fi
fi
say "  Logs: $LOG/{miner,reveal,judge}-judgeN.log   Keys: $KEYDIR"
say "  Verify registration:   dendrad query jobs list-miner --node $DENDRA_NODE -o json"
say "  Verify model diversity (after first verdicts): distinct model_id across judges = heterogeneity requirement."
say "  Stop: pkill -f 'reveal_worker.py --id judge'; pkill -f 'judge_worker.py --id judge'; pkill -f 'miner.py --id judge'"
say ""
say "  Next: send warm-up traffic, then run the C3 validation:"
say "    DENDRA_API_KEY=<key from $ENVF> HOST=$HOST bash deploy/launch/cheat_public.sh"
say "=============================================================================="
