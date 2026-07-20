#!/usr/bin/env bash
# judges5_public.sh — start a heterogeneous AUDIT-JUDGE pool (5 miner-judges) against the PUBLIC Dendra network,
# in ONE command. The audit committee has 5 seats and needs >=4 participants to resolve a sampled audit, so a
# real slash path requires at least 5 registered judge-miners. Heterogeneity (>=2 distinct judge models) closes
# correlated false-slash: this pool runs 3x MoE (qwen3:30b-a3b) + 2x mistral-nemo.
#
# Each judge = a native miner (also serves inference) + reveal_worker + judge_worker, sharing one
# on-chain identity, pointed at the public RPC/relay. Judges are FUNDED from the local node's operator key
# (the public faucet is PoW-gated and miner self-funding does not solve PoW).
#
# Run on the PC that already runs the synced node kit (deploy/testnet-node), with dendrad + python3 + ollama in
# PATH and a GPU. Idempotent: re-running re-funds only if needed and restarts the workers.
#
# Usage (WSL, repo root):
#   HOST=api.dendranetwork.com tr -d '\r' < deploy/launch/judges5_public.sh | bash
#   # options (env): JUDGES=5  MOE_COUNT=3  KEYDIR=~/.dendra-judges  FUND_UDNDR=200000
#   #                MOE_MODEL=qwen3:30b-a3b-instruct-2507-q4_K_M  NEMO_MODEL=mistral-nemo
#   #                NODE_KIT=deploy/testnet-node (source of the operator 'validator' key for funding)
set -u

HOST="${HOST:-api.dendranetwork.com}"
JUDGES="${JUDGES:-5}"
# Models. EMPTY BY DEFAULT = auto-sized from the MEASURED hardware (deploy/hw_probe.sh, resolved below).
# Hard-coded defaults were a trap: `mistral-nemo` (8.7 GB) was handed to an 8 GB card, Ollama offloaded
# 70% of it to CPU, inference blew past the deadline (ReadTimeout) and jobs never settled. Never size a
# model from a guess — size it from the VRAM that is actually there.
# Co-resident identities on ONE box now share ONE model (a single copy in VRAM instead of N families
# thrashing); committee HETEROGENEITY comes from DIFFERENT MACHINES, which hw_probe.sh spreads by
# hashing the machine id. Explicit overrides still win.
MOE_COUNT="${MOE_COUNT:-3}"
MOE_MODEL="${MOE_MODEL:-}"
NEMO_MODEL="${NEMO_MODEL:-}"
KEYDIR="${KEYDIR:-$HOME/.dendra-judges}"
MINER_STAKE="${MINER_STAKE:-60000}"          # on-chain stake per judge; MUST be >= chain min_stake (50000)
FUND_UDNDR="${FUND_UDNDR:-200000}"          # bank balance per judge (stake + margin; gas is free at min-gas=0)
FUND_FROM="${FUND_FROM:-validator}"          # operator key inside the node container that funds the judges
REPO="${DENDRA_REPO:-$PWD}"
NODE_KIT="${NODE_KIT:-$REPO/deploy/testnet-node}"
MODEA="$REPO/services"
LOG="${DENDRA_JUDGE_LOG:-/tmp/dendra-judges}"; mkdir -p "$LOG" "$KEYDIR"

export DENDRA_NODE="tcp://$HOST:26657"
export DENDRA_RELAY="http://$HOST:8645"
export DENDRA_FAUCET="http://$HOST:4500"
export DENDRA_CHAIN_ID="${DENDRA_CHAIN_ID:-dendra}"

say(){ printf '%s\n' "$*"; }
die(){ printf '[judges] FATAL: %s\n' "$*" >&2; exit 1; }
warn(){ printf '[judges] WARN: %s\n' "$*"; }

# --- relay token (public relay requires it) : from the launch env file, never hardcoded ---
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
if [ -z "${DENDRA_RELAY_TOKEN:-}" ] && [ -f "$ENVF" ]; then
  DENDRA_RELAY_TOKEN="$(grep -E '^DENDRA_RELAY_TOKEN=' "$ENVF" | head -1 | cut -d= -f2-)"
fi
[ -n "${DENDRA_RELAY_TOKEN:-}" ] || warn "DENDRA_RELAY_TOKEN empty (set it in $ENVF) — the public relay will reject workers without it."
export DENDRA_RELAY_TOKEN

# --- pre-flight ---
command -v dendrad >/dev/null 2>&1 || die "dendrad not in PATH (the node kit builds it inside Docker; install the CLI on the host or run from the node container)."
command -v python3 >/dev/null 2>&1 || die "python3 required."
command -v ollama  >/dev/null 2>&1 || die "ollama required (judge models run locally on your GPU)."
[ -f "$MODEA/miner.py" ] || die "run from the repo root (services not found)."
curl -fsS -m 8 "http://$HOST:26657/status" >/dev/null 2>&1 || die "public RPC unreachable (http://$HOST:26657) — is the network up?"

NODE_COMPOSE="$NODE_KIT/docker-compose.yml"
[ -f "$NODE_COMPOSE" ] || die "node kit compose not found ($NODE_COMPOSE) — this script funds judges from its operator key."

# --- HARDWARE GATE: is this box allowed to JUDGE? -----------------------------------------------
# A judge that is too weak does not merely judge badly: it votes DIVERGENT on answers that are correct
# but worded differently, and an unfair verdict costs an honest miner its stake. deploy/hw_probe.sh
# gates the role on the MEASURED VRAM and on an allow-list of validated judge models: below tier 3
# (mistral-nemo class) this box may mine, but must not seat a judge.
# PAS d'identifiant passé ici : hw_probe.sh dérive lui-même un PSEUDONYME (hash de la machine). Cette
# ligne passait `$(hostname)`, ce qui écrasait le pseudonyme et publiait « DESKTOP-XXXX » en clair dans
# le registre public et sur la page /network. L'anonymisation était codée dans la sonde et défaite à
# l'appel — une garde contournée par son propre appelant ne protège rien.
HW_JSON="$(bash "$REPO/deploy/hw_probe.sh" --json 2>/dev/null || echo '{}')"
CAN_JUDGE="$(printf '%s' "$HW_JSON" | grep -o '"can_judge":[a-z]*' | cut -d: -f2)"; [ -n "$CAN_JUDGE" ] || CAN_JUDGE=false
HW_TIER="$(printf '%s' "$HW_JSON" | grep -o '"tier":[0-9]*' | cut -d: -f2)"
if [ "$CAN_JUDGE" != "true" ]; then
  warn "hardware tier ${HW_TIER:-?} -> this box will MINE but NOT judge (an under-powered judge slashes"
  warn "honest miners). Seat the audit committee on a machine with >= 12 GB VRAM (>= 24 GB for the MoE judge)."
  warn "Override at your own risk: DENDRA_FORCE_JUDGE=1"
  [ "${DENDRA_FORCE_JUDGE:-0}" = "1" ] && { CAN_JUDGE=true; warn "DENDRA_FORCE_JUDGE=1 -> judges started anyway."; }
fi

# Auto-size the model from the probe unless the operator pinned one. This is what stops a node from
# ever loading a model bigger than its card again (the CPU-offload -> ReadTimeout -> unsettled-job chain).
HW_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"model":"[^"]*"' | cut -d'"' -f4)"
# The JUDGE model can differ from the MINING model: on a small-GPU/large-RAM box the probe seats a
# MoE judge on the CPU (few ACTIVE params -> usable) while the GPU keeps serving inference at speed.
HW_JUDGE_MODEL="$(printf '%s' "$HW_JSON" | grep -o '"judge_model":"[^"]*"' | cut -d'"' -f4)"
HW_MOE_CPU="$(printf '%s' "$HW_JSON" | grep -o '"moe_cpu":[0-9]*' | cut -d: -f2)"
[ "${HW_MOE_CPU:-0}" = "1" ] && say "  [hw] CPU-MoE judge path: '$HW_JUDGE_MODEL' on CPU — all seats use the SAME model, so Ollama keeps ONE copy resident (judgments serialise; the audit quorum needs several seats)."
if [ -z "$MOE_MODEL" ]; then
  MOE_MODEL="${HW_MODEL:-llama3.2:3b}"
  say "  [hw] tier ${HW_TIER:-?} -> auto-sized model: $MOE_MODEL"
fi
[ -n "$NEMO_MODEL" ] || NEMO_MODEL="$MOE_MODEL"   # one model per box: a single copy resident in VRAM

# Publish this box to the network capacity registry (declarative inventory feeding the /network page).
# Best-effort by design: the registry is observability, never a precondition for mining.
CAP_URL="${DENDRA_CAPACITY_URL:-https://api.dendranetwork.com/capacity}"
# Attach the ON-CHAIN identities this box runs. node_id is a privacy pseudonym, so without this the
# registry could never tell a staked operator from an anonymous claim and displayed "unregistered"
# for everyone — a false statement on a public page.
CAP_IDS="$(for k in $(seq 1 "$JUDGES"); do printf 'juge%s\n' "$k"; done)"
CAP_JSON="$(printf '%s' "$HW_JSON" | DENDRA_CAP_IDS="$CAP_IDS" python3 -c 'import json,sys,os
try:
    d=json.load(sys.stdin)
    d["miner_ids"]=[x for x in os.environ.get("DENDRA_CAP_IDS","").split() if x]
    print(json.dumps(d))
except Exception:
    sys.exit(1)' 2>/dev/null)"
[ -n "$CAP_JSON" ] || CAP_JSON="$HW_JSON"
if curl -fsS -m 5 -X POST "$CAP_URL" -H 'Content-Type: application/json' -d "$CAP_JSON" >/dev/null 2>&1; then
  say "  [hw] capacity published to the network registry"
else
  warn "capacity registry unreachable ($CAP_URL) — mining continues, inventory just misses this box."
fi

# fund one address from the operator key held in the node container (min-gas=0 -> no fee)
fund_from_operator(){
  local to="$1" amt="$2" out
  out=$(docker compose -f "$NODE_COMPOSE" exec -T node \
    dendrad tx bank send "$FUND_FROM" "$to" "${amt}udndr" \
      --keyring-backend test --chain-id "$DENDRA_CHAIN_ID" --node "$DENDRA_NODE" --yes -o json 2>&1)
  echo "$out" | grep -q '"code":0' || { warn "funding tx not accepted: $(echo "$out" | tr -d '\n' | cut -c1-180)"; return 1; }
}

bal_of(){ # bank udndr balance of an address, via public RPC (JSON is pretty-printed -> strip spaces/newlines)
  dendrad query bank balances "$1" --output json --node "$DENDRA_NODE" 2>/dev/null \
    | tr -d ' \n' | grep -oE '"amount":"[0-9]+"' | grep -oE '[0-9]+' | head -1
}

model_for(){ [ "$1" -le "$MOE_COUNT" ] && echo "$MOE_MODEL" || echo "$NEMO_MODEL"; }

# --- pull the models once (idempotent): served model + embeddings (a judge-miner can also be drawn as
#     the PRIMARY, so it must be able to serve inference) + the judge models ---
SERVED_MODEL="${DENDRA_MODEL_ID:-llama3.1:8b-instruct-q4_K_M}"
EMBED_MODEL="${DENDRA_EMBED_API_MODEL:-nomic-embed-text}"
say "== [judges] pulling models (once) =="
_pulled=""
for m in "$SERVED_MODEL" "$EMBED_MODEL" ${HW_JUDGE_MODEL:-} $(for k in $(seq 1 "$JUDGES"); do model_for "$k"; done); do
  case "$_pulled" in *"|$m|"*) continue;; esac; _pulled="$_pulled|$m|"
  say "  ollama pull $m"; ollama pull "$m" >/dev/null 2>&1 || warn "pull $m failed (will retry at first use)"
done

# --- stop any previous judge workers (clean restart) ---
pkill -f 'reveal_worker.py --id juge' 2>/dev/null || true
pkill -f 'judge_worker.py --id juge'  2>/dev/null || true
pkill -f 'miner.py --id juge'  2>/dev/null || true
sleep 2

# --- EVICT a stale judge model from the GPU instance (measured trap) ---
# Killing the workers does NOT unload the model: Ollama keeps it resident for its keep-alive window. A
# previous run that judged on :11434 therefore leaves ~19 GB of MoE sitting on the GPU, and the miners we
# are about to start probe a card that is already full -> `FATAL Ollama injoignable (ReadTimeout)`, zero
# miners, every job stuck `open`. Observed exactly that. The judge model belongs on :11435 (CPU) only, so
# evicting it from :11434 is always correct here — never touches the mining model.
if [ "${HW_MOE_CPU:-0}" = "1" ] && [ -n "${HW_JUDGE_MODEL:-}" ]; then
  if ollama ps 2>/dev/null | grep -q -- "$HW_JUDGE_MODEL"; then
    say "  [judges] evicting '$HW_JUDGE_MODEL' from the GPU instance (leftover of a previous run — it would starve the miners)"
    ollama stop "$HW_JUDGE_MODEL" >/dev/null 2>&1 || true
    sleep 2
  fi
fi

# --- create keys, fund, and launch each judge ---
for k in $(seq 1 "$JUDGES"); do
  jid="juge$k"; JM="$(model_for "$k")"
  # 1) key (idempotent) + address
  dendrad keys show "$jid" -a --keyring-backend test >/dev/null 2>&1 \
    || dendrad keys add "$jid" --keyring-backend test >/dev/null 2>&1
  addr="$(dendrad keys show "$jid" -a --keyring-backend test 2>/dev/null)"
  [ -n "$addr" ] || { warn "$jid: no address, skipped"; continue; }
  # 2) fund from operator ONLY if under the required stake (idempotent). Threshold = MINER_STAKE, not
  #    FUND_UDNDR: an already-registered judge keeps well above stake, so we must NOT re-fund it (5 rapid
  #    sends from one operator account collide on the account sequence). We wait for tx INCLUSION (balance
  #    rises) between judges, which serializes the sends and avoids the nonce race.
  cur="$(bal_of "$addr")"; cur="${cur:-0}"
  if [ "$cur" -lt "$MINER_STAKE" ] 2>/dev/null; then
    say "  [$jid] funding $addr ($cur -> $FUND_UDNDR udndr) from '$FUND_FROM'"
    fund_from_operator "$addr" "$FUND_UDNDR" || warn "$jid funding tx failed (operator balance? see node logs)"
    for _ in $(seq 1 20); do cur="$(bal_of "$addr")"; [ "${cur:-0}" -ge "$MINER_STAKE" ] 2>/dev/null && break; sleep 3; done
  fi
  # SIEGE REELLEMENT POURVU, pas siege demande. Le compteur final affichait `$JUDGES`, c.-a-d. le
  # nombre VOULU : un juge sans adresse (skipped ci-dessus) ou sous-stake (donc il ne s'enregistrera
  # pas) etait quand meme compte. Comme le plancher d'audit vaut 4 sieges, annoncer 5 pour 3 sieges
  # reels fait croire le slash arme alors qu'il ne peut pas tomber.
  if [ "${cur:-0}" -ge "$MINER_STAKE" ] 2>/dev/null; then SEATED=$((${SEATED:-0}+1))
  else warn "$jid under required stake (bal=$cur < $MINER_STAKE) — it will not register; check operator balance."; fi
  # 3) miner-judge daemon (self-registers create-miner once funded), then reveal + judge workers
  say "  [$jid] start (model=$JM, stake=$MINER_STAKE)"
  # DENDRA_RELAY_TOKEN passed EXPLICITLY to every process (the public relay rejects untokened requests, and
  # relay_client reads it at import time) — belt-and-suspenders on top of the global export.
  # OLLAMA_MODEL/DENDRA_MODEL_ID: WITHOUT these the miner ignores the probe entirely and falls back to
  # the hard-coded default of modea/inference.py — i.e. the whole "never load a model bigger than the
  # card" fix had NO effect on the served inference. Measured gap, fixed here.
  DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" DENDRA_MINER_JUDGE=1 DENDRA_JUDGE_MODEL="$JM" DENDRA_JUDGE_MODEL_ID="$JM" DENDRA_MINER_STAKE="$MINER_STAKE" \
  OLLAMA_MODEL="$JM" DENDRA_MODEL_ID="$JM" \
    python3 -u "$MODEA/miner.py" --id "$jid" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" --faucet "$DENDRA_FAUCET" --backend ollama \
    >"$LOG/miner-$jid.log" 2>&1 &
  # wait for the on-chain identity (sk file) before starting the workers
  i=0; while [ $i -lt 40 ] && [ ! -f "$KEYDIR/$jid.sk" ]; do i=$((i+1)); sleep 2; done
  [ -f "$KEYDIR/$jid.sk" ] || { warn "$jid not registered yet (see $LOG/miner-$jid.log) — continuing"; }
  DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" python3 -u "$MODEA/reveal_worker.py" --id "$jid" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" \
    >"$LOG/reveal-$jid.log" 2>&1 &
done

# --- PHASE 2 : seat the audit committee, ONLY once every miner has passed its Ollama probe --------
# ORDER MATTERS, and getting it wrong kills the miners:
#   `miner` probes Ollama at boot and exits FATAL on timeout ("Ollama injoignable (ReadTimeout)",
#   mock forbidden in prod). A judge loading a 20 GB MoE on CPU makes Ollama unresponsive for MINUTES.
#   Starting judge N inside the loop therefore asphyxiated miners N+1..5 one by one — the network then
#   had judges but NO miner, and every job stayed `open` until the client gave up.
# So: all miners first (Ollama free, probes succeed), committee afterwards.
# Seat count: `AdjudicateDispute` needs audit_min_quorum (4) verdicts out of a 5-seat committee. A single
# judge produced "comite frais incomplet" forever (msg_server_adjudicate.go:99) and no dispute ever
# closed. All seats share the SAME model, so Ollama keeps ONE copy of the weights resident and
# serialises: the cost is latency, not memory.
if [ "$CAN_JUDGE" = "true" ]; then
  say "== [judges] miners up — seating the audit committee =="
  # A DEDICATED CPU OLLAMA FOR THE COMMITTEE. Sharing one instance does not work, and ordering alone
  # does not fix it: the committee loads a 20 GB MoE and serialises minute-long CPU generations, which
  # evicts the miner's GPU model and starves its inference until `miner` hits an Ollama
  # ReadTimeout and exits FATAL — leaving judges but zero miners, every job stuck `open`.
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
    jid="juge$k"; JJM="${HW_JUDGE_MODEL:-$(model_for "$k")}"
    # DENDRA_EMBED_*: the judge falls back to a LEXICAL hash embedder when unset (it then compares
    # WORDS, not meaning). nomic-embed-text is already pulled above — actually wire it.
    # OLLAMA_TIMEOUT is raised on the CPU path: a MoE on CPU is correct but slow (~10 s/case).
    DENDRA_RELAY_TOKEN="$DENDRA_RELAY_TOKEN" OLLAMA_MODEL="$JJM" DENDRA_JUDGE_MODEL="$JJM" DENDRA_JUDGE_MODEL_ID="$JJM" \
    OLLAMA_ENDPOINT="$JUDGE_OLLAMA" DENDRA_JUDGE_ENDPOINT="$JUDGE_OLLAMA" \
    DENDRA_EMBED_MODE="${DENDRA_EMBED_MODE:-backend}" DENDRA_EMBED_API_MODEL="${DENDRA_EMBED_API_MODEL:-$EMBED_MODEL}" \
    OLLAMA_TIMEOUT="${DENDRA_JUDGE_OLLAMA_TIMEOUT:-$([ "${HW_MOE_CPU:-0}" = "1" ] && echo 600 || echo 240)}" \
    DENDRA_JUDGE_GEN_RETRIES="${DENDRA_JUDGE_GEN_RETRIES:-8}" \
      python3 -u "$MODEA/judge_worker.py" --id "$jid" --relay "$DENDRA_RELAY" --keydir "$KEYDIR" --adjudicate --model-id "$JJM" \
      >"$LOG/judge-$jid.log" 2>&1 &
    say "  [$jid] judge seated (model=$JJM$([ "${HW_MOE_CPU:-0}" = "1" ] && echo ", CPU MoE"))"
  done
else
  say "  audit committee NOT seated (hardware tier ${HW_TIER:-?} < 3) — this box mines only"
fi

sleep 5
say ""
say "=============================================================================="
SEATED="${SEATED:-0}"
if [ "${HW_MOE_CPU:-0}" = "1" ]; then
  say "  POOL started: $SEATED/$JUDGES miners funded & seated on '$MOE_MODEL' (GPU :11434) + judge seats on '$HW_JUDGE_MODEL' (CPU :11435)"
  say "  ONE judge model on this box -> heterogeneity is NOT met by this host alone; it needs a SECOND operator."
else
  say "  JUDGE POOL started: $SEATED/$JUDGES judges actually seated ($MOE_COUNT x '$MOE_MODEL' + $((JUDGES-MOE_COUNT)) x '$NEMO_MODEL' requested)"
fi
# Le nombre de sieges POURVUS decide si un slash peut seulement tomber : sous le plancher de quorum,
# l'audit est INERTE et un tricheur passe. Le dire ici evite de le decouvrir en lisant des verdicts absents.
if [ "$SEATED" -lt 4 ] 2>/dev/null; then
  say "  ⚠️  $SEATED sieges pourvus < plancher de quorum (4) -> AUCUN slash dur ne peut tomber (audit inerte)."
  say "     Cause usuelle : solde de l'operateur insuffisant pour financer tous les juges. Verifie, puis relance (idempotent)."
fi
say "  Logs: $LOG/{miner,reveal,judge}-jugeN.log   Keys: $KEYDIR"
say "  Verify registration:   dendrad query jobs list-miner --node $DENDRA_NODE -o json"
say "  Verify model diversity (after first verdicts): distinct model_id across judges = heterogeneity requirement."
say "  Stop: pkill -f 'reveal_worker.py --id juge'; pkill -f 'judge_worker.py --id juge'; pkill -f 'miner.py --id juge'"
say ""
say "  NEXT: send warm-up traffic, then run the C3 validation:"
say "    DENDRA_API_KEY=<key from $ENVF> tr -d '\\r' < dendra/onchain-staging/dendra_c3_validation.sh | bash -s -- $HOST"
say "=============================================================================="
