#!/bin/sh
# Dispatches the Python service to launch (first argument). Configuration comes from the environment
# (docker compose).
set -e
ROLE="${1:-relay}"; shift 2>/dev/null || true
RELAY="${DENDRA_RELAY:-http://relay:8645}"

case "$ROLE" in
  relay)
    exec python3 relay.py "${RELAY_PORT:-8645}" ;;
  gateway)
    # gateway_fund funds the FREE TIER, and its failure must stay LOUD. Swallowing it with `|| true` yields a
    # "healthy" container and a running gateway that serves NOT ONE free-tier job, with no line saying
    # so — the symptom reads as "0 jobs, quiet network". The usual cause of a first failure is that the
    # chain is not ready at boot, so this loop RETRIES, then SHOUTS. It stays deliberately non-fatal: the
    # PAID tier does not depend on this funding, and killing the gateway would punish paying clients for
    # a free-tier outage. The failure is made impossible to miss in `docker logs` instead.
    _i=0
    until python3 gateway_fund.py; do
      _i=$((_i+1))
      if [ "$_i" -ge 10 ]; then
        echo "ALERT: gateway_fund FAILED $_i times -> gateway NOT funded, the FREE TIER will serve NO job (the paid tier still works). Diagnose BEFORE announcing anything: docker logs <gateway> | grep gateway_fund" >&2
        break
      fi
      echo "gateway_fund: attempt $_i/10 failed — retrying in 6 s (chain not ready yet?)" >&2
      sleep 6
    done
    exec python3 gateway.py ;;
  exporter)
    exec python3 exporter.py ;;
  faucet)
    exec python3 faucet.py ;;
  miner)
    ID="${1:-m1}"
    # This probe cannot succeed against a protected relay unless it authenticates. Calling /list WITHOUT
    # an auth header makes a public relay answer 401, `urlopen` raise, and the loop never break: 60 x 2 s
    # = 120 s burned on EVERY container start, after which the miner starts anyway. A probe that cannot
    # observe success in the topology it targets measures nothing; it only delays. So send the token, and
    # treat 401 as "the relay ANSWERS": it is an HTTP response, the service is there and only access is
    # refused, and that case is already caught upstream by join.sh.
    i=0; while [ $i -lt 60 ]; do
      DENDRA_RELAY_TOKEN="${DENDRA_RELAY_TOKEN:-}" python3 - "$RELAY" <<'PROBE' 2>/dev/null && break
import os, sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1].rstrip("/") + "/list")
tok = os.environ.get("DENDRA_RELAY_TOKEN", "")
if tok:
    req.add_header("X-Dendra-Token", tok)
try:
    urllib.request.urlopen(req, timeout=3)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code in (401, 403) else 1)   # the relay ANSWERS: it is alive
except Exception:
    sys.exit(1)
PROBE
      i=$((i+1)); sleep 2
    done
    # ADR-026/028: DENDRA_MINER_JUDGE=1 makes this miner ALSO sit on the optimistic AUDIT COMMITTEE
    # (reveal + verdict), which is what gives the audit TEETH — without jurors, audited jobs self-vindicate
    # at timeout and the audit is inert. It serves BOTH topologies: flag the work miners, or run dedicated
    # CPU-only miner containers as jurors when GPUs are scarce. judge_worker.py is FATAL if the key
    # <id>.sk is missing (miner writes it at registration), so start the miner FIRST, WAIT for the
    # key, THEN start the jurors. DORMANT by default (=0: miner behaviour strictly unchanged).
    #
    # TWO SWITCHES, BECAUSE AN OBLIGATION IS NOT A ROLE. A single flag driving both reveal_worker AND
    # judge_worker would price them alike, and they are not alike:
    #   - REVEALING is an OBLIGATION of the primary: zero inference, one POST to the relay per audited
    #     job. Without it the audit has NO artefact to judge, the job defers without bound, the client's
    #     fee stays held forever, and the primary is neither paid nor sanctioned.
    #   - JUDGING is a ROLE: it requires a second Ollama instance (otherwise the juror infers its
    #     reference on the very backend it is meant to check) and concerns committee members only.
    # Conflated, revealing costs what a role costs, so nobody arms it and primaries do not reveal — a
    # miner then runs for a day without a single reveal and nothing reports it.
    # DELIBERATE DEFAULT: DENDRA_MINER_REVEAL=1. Revealing is not an option to arm, it is the
    # counterpart of being paid for a job. DENDRA_MINER_JUDGE stays DORMANT (=0).
    REVEAL_ON="${DENDRA_MINER_REVEAL:-1}"
    JUDGE_ON="${DENDRA_MINER_JUDGE:-0}"
    if [ "$REVEAL_ON" != "1" ] && [ "$JUDGE_ON" != "1" ]; then
      exec python3 miner.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
        --faucet "${FAUCET:-http://chain:4500}" --backend "${BACKEND:-ollama}"
    fi
    python3 miner.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
      --faucet "${FAUCET:-http://chain:4500}" --backend "${BACKEND:-ollama}" &
    MPID=$!
    # judge_worker is FATAL if the key <id>.sk is missing (miner writes it at registration):
    # start the miner FIRST, WAIT for the key, THEN the workers.
    # WAIT ON THE IDENTITY THE DAEMON ACTUALLY REGISTERS UNDER, not the one that was asked for.
    # `$ID` comes from the .env (`MINER_ID`), which join.sh derives as `m-<hash>`. But miner.py
    # ALIGNS that to the identifier the chain accepts — `a.id = align_identity(...)` — before naming
    # any file, so the key is written as `dm1….sk`, never `m-<hash>.sk`. Waiting on the asked name
    # therefore times out forever, and BOTH workers below sit inside the `if` that follows.
    # The cost is severe and silent: revealing is the PRIMARY'S OBLIGATION, so with no reveal_worker a
    # sampled job has no artifact to judge, the client's fee stays held with no bound, and the miner is
    # neither paid nor slashed — while the container reads "Up" and the only message blames a missing
    # key that exists under the other name. With `--judge`, the seat is MUTE after the hardware gate,
    # a 19 GB model pull and every DENDRA_JUDGE_* wired correctly.
    # The daemon persists the accepted identity (`identite-resolue`, same volume): read it rather than
    # guess, and fall back to `$ID` only when the file is absent.
    EFF="$ID"
    j=0
    while [ $j -lt 90 ]; do
      if [ -s /data/keys/identite-resolue ]; then
        # ALL whitespace, not just CR/LF: a file holding only blanks passes `-s` (non-empty) and
        # would survive `tr -d '\r\n'` as spaces, making the entrypoint look for `/data/keys/   .sk`.
        # And only a DERIVED identifier is adopted — same rule as miner: a corrupt memory is
        # ignored, never adopted.
        _e="$(tr -d '[:space:]' < /data/keys/identite-resolue 2>/dev/null)"
        case "$_e" in dm1*) EFF="$_e" ;; esac
      fi
      [ -f "/data/keys/$EFF.sk" ] && break
      j=$((j+1)); sleep 2
    done
    [ "$EFF" = "$ID" ] || echo "[glue] identity RESOLVED by the daemon: '$ID' -> '$EFF' (workers follow it)"
    ID="$EFF"
    if [ -f "/data/keys/$ID.sk" ]; then
      if [ "$REVEAL_ON" = "1" ]; then
        echo "[reveal] key $ID ready -> reveal_worker (primary obligation; no inference)"
        # Output goes to STDOUT, NOT to /tmp. Redirected into the CONTAINER's /tmp, not one line from
        # these workers reaches `docker compose logs miner`, and /tmp vanishes on restart on top of that:
        # a failed revealer or juror is then invisible from outside while the container still reads "Up"
        # — and it is that silence, not the failure, that costs a day of mining. Lines are prefixed so
        # they stay readable in a shared stream.
        python3 reveal_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys 2>&1 | sed 's/^/[reveal] /' &
      fi
      if [ "$JUDGE_ON" = "1" ]; then
        JEP="${DENDRA_JUDGE_ENDPOINT:-${OLLAMA_ENDPOINT:-http://localhost:11434}}"
        JM=""; [ -n "${DENDRA_JUDGE_MODEL_OVERRIDE:-}" ] && JM="--model-id ${DENDRA_JUDGE_MODEL_OVERRIDE}"
        # A juror that judges on the SAME endpoint as the miner infers its reference on the very backend
        # it is supposed to check. Say so at startup rather than discovering it afterwards.
        [ "$JEP" = "${OLLAMA_ENDPOINT:-}" ] && echo "[judge] WARNING: juror and miner share $JEP (point DENDRA_JUDGE_ENDPOINT at a second instance)"
        # The juror must not start before its model exists. `judge-model-init` pulls ~19 GB in the
        # background and NOTHING depends on it (`restart: "no"`, no `depends_on`). The key `<id>.sk` is
        # written within seconds, so the juror would start long before the download finished and return
        # 404 on every job. That case heals itself — unless the pull FAILS (network, full disk,
        # non-existent tag), and then the seat stays MUTE forever without a signal. So wait for the model,
        # say so, and shout when it drags.
        # Wait for the MODEL, not merely the engine: `/api/tags` answers within seconds while the pull
        # takes minutes. Bounded at 30 min (19 GB over an ordinary link), with progress reported every
        # 2 min so a long wait does not look like a hang.
        # Wait for the model that will ACTUALLY be used, not the one the kit downloaded.
        # judge_worker.py::resolve_judge_model applies this precedence: --model-id, then the ON-CHAIN pin
        # (modelregistry.audit_judge_model), then DENDRA_JUDGE_MODEL_ID. Waiting on rank 3 while the
        # worker will run with rank 2 validates a model that never serves, and the seat stays mute (404 on
        # every generation) after a wait declared successful.
        # Resolution is best-effort: unreachable chain or missing dendrad falls back to rank 3.
        JWM="${DENDRA_JUDGE_MODEL_ID:-}"
        JEFF="${DENDRA_JUDGE_MODEL_OVERRIDE:-}"
        if [ -z "$JEFF" ]; then
          JEFF="$(timeout 25 dendrad query modelregistry params --output json ${DENDRA_NODE:+--node "$DENDRA_NODE"} 2>/dev/null \
            | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
p = d.get("params", d) if isinstance(d, dict) else {}
print((p.get("audit_judge_model") or p.get("auditJudgeModel") or "").strip())' 2>/dev/null)"
        fi
        [ -n "$JEFF" ] || JEFF="$JWM"
        if [ -n "$JWM" ] && [ "$JEFF" != "$JWM" ]; then
          if [ -n "${DENDRA_JUDGE_MODEL_OVERRIDE:-}" ]; then JSRC="--model-id"; else JSRC="on-chain pin"; fi
          echo "[judge] EFFECTIVE judge model = '$JEFF' ($JSRC), not '$JWM' (DENDRA_JUDGE_MODEL_ID)."
          echo "[judge] The on-chain verdict will nonetheless DECLARE '$JWM': align the two values, otherwise"
          echo "[judge] the judge-model diversity metric counts a model that did not judge."
        fi
        JWM="$JEFF"
        jw=0; JOK=0
        while [ $jw -lt 1800 ]; do
          if DENDRA_JM="$JWM" python3 - "$JEP" <<'JPROBE' 2>/dev/null; then JOK=1; break; fi
import os, sys, urllib.request
try:
    tags = urllib.request.urlopen(sys.argv[1].rstrip("/") + "/api/tags", timeout=5).read().decode()
except Exception:
    sys.exit(1)
want = os.environ.get("DENDRA_JM", "").strip()
sys.exit(0 if (not want or want in tags) else 1)   # no model named -> the engine alone is enough
JPROBE
          [ $((jw % 120)) = 0 ] && echo "[judge] waiting for judge model ${JWM:-(unnamed)} on $JEP — ~19 GB to download, ${jw}s elapsed"
          jw=$((jw+10)); sleep 10
        done
        if [ "$JOK" = "1" ]; then
          echo "[judge] judge model AVAILABLE on $JEP after ${jw}s"
        else
          echo "[judge] WARNING: judge model ${JWM:-?} not found on $JEP after 30 min."
          echo "[judge] The worker starts anyway, but without a model it will NEVER vote -> MUTE SEAT."
          echo "[judge] Check: docker compose logs judge-model-init  (pull failed? disk full? tag missing?)"
        fi
        echo "[judge] key $ID ready -> judge_worker (committee role; judge Ollama=$JEP)"
        # ⛔ NEVER PREFIX THIS LINE WITH `OLLAMA_ENDPOINT="$JEP"` — IT MUTES THE JUDGE.
        # The judge process does TWO inferences with TWO different models:
        #   · its own REFERENCE answer, a peer opinion, produced by the MINER backend and model
        #     (`backend.generate`, judge_worker.py) — that model lives on the MINING endpoint;
        #   · the VERDICT, produced by the judge model this node resolves at startup (`llm_judge`) —
        #     routed by `judge_endpoint()`, precedence DENDRA_JUDGE_ENDPOINT > OLLAMA_ENDPOINT.
        #     (Resolved per node, not enforced by consensus: `audit_judge_model` is inert on-chain,
        #     so nothing here may be described as a model the committee is held to.)
        # Overriding OLLAMA_ENDPOINT is only correct while ONE Ollama serves both. With a judge backend
        # of its own, that override also drags the REFERENCE call onto the judge endpoint, where the
        # miner's model is not pulled: Ollama answers 404 "model not found", the worker prints "Ollama
        # unreachable", and the seat goes MUTE at every start. The signature is `/api/tags` 200 and
        # `/api/generate` 404, on repeat, while the network is perfectly healthy.
        # The override is redundant as well as wrong: DENDRA_JUDGE_ENDPOINT routes the verdict on its
        # own — its docstring calls the addition "strictly additive". Leaving OLLAMA_ENDPOINT alone is
        # safe in BOTH topologies: with DENDRA_JUDGE_ENDPOINT unset, judge_endpoint() falls back to
        # OLLAMA_ENDPOINT and the single-backend behaviour is unchanged.
        python3 judge_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
          --reveal-grace "${DENDRA_REVEAL_GRACE:-2}" --adjudicate $JM 2>&1 | sed 's/^/[judge] /' &
      fi
    else
      # NAME BOTH IDENTITIES. A message reading "key m-xxxx still missing" while the key exists under
      # `dm1…` sends the operator looking for a file that was never going to appear, with nothing
      # linking it to the alignment printed minutes earlier. A diagnosis that blames the wrong object
      # costs more than no diagnosis.
      echo "[glue] WARNING: no key for '$ID' after 180s (asked for: '${1:-m1}', resolved: $( [ -s /data/keys/identite-resolue ] && tr -d '\r\n' < /data/keys/identite-resolue || echo 'none yet' ))"
      echo "[glue]          -> neither reveal nor judge started. The miner runs, but it reveals NOTHING:"
      echo "[glue]             a sampled job then has no artifact to judge, so its fee stays held forever."
    fi
    # Only the miner is waited on: if it dies the container dies and `restart: unless-stopped` brings it
    # back. The workers are VISIBLE in the logs, so their death is read rather than guessed. Supervising
    # their exit codes would need `wait -n`, which /bin/sh lacks; visibility covers the expensive case,
    # the SILENT failure.
    wait "$MPID" ;;
  cli)
    exec python3 cli.py "$@" ;;
  proof)
    # The Proof: read-only verifiability facade (recent jobs, pools, VRF health). Public by design (it
    # holds no secret); the listen address comes from DENDRA_PROOF_HOST.
    exec python3 the_proof.py ;;
  points)
    # Season 0 leaderboard: provisional, stateless, recomputable from chain data.
    exec python3 points_indexer.py --serve ;;
  capacity)
    # Network capacity registry: hardware inventory and served models, DECLARED by operators and not
    # proven on-chain. Read it as an inventory, never as a proof of compute power. Persisted on DISK
    # (DENDRA_CAPACITY_DB): a registry that forgets on restart is a bug.
    exec python3 capacity_server.py ;;
  *)
    echo "unknown role: $ROLE (relay|gateway|exporter|faucet|miner <id>|proof|points|capacity|cli ...)"; exit 2 ;;
esac
