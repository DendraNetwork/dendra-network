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
    # NO MINING MODEL ON THE CPU, applied to the ENGINE before the miner starts (serve_guard.py): the installers
    # decide the role (deploy/hw_probe.sh --role), and every other way of starting this container passes here.
    # 0 on a GPU, or the judge role on the CPU; 2 refused; 3 not measured -- the container stops, and its restart
    # policy asks again. Never before the miner registers anything: the daemon starts after it.
    # >>> serve-guard
    python3 serve_guard.py || exit $?
    # <<< serve-guard
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
    # THE WAITS BELOW END WITH THE DAEMON. This one (up to 180 s) and the wait for the judge model (up to 30 min)
    # each read `kill -0 "$MPID"` on every round: a daemon that stops during either wait ends it, no worker is
    # started after it, and the script reaches `wait "$MPID"` at once, so the container stops with the daemon.
    # Without it, a daemon that died during the judge model's wait left the container up for the rest of the 30
    # min, then started a judge_worker for a miner that no longer ran.
    EFF="$ID"
    j=0
    while [ $j -lt 90 ]; do
      kill -0 "$MPID" 2>/dev/null || break
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
    # THE WORKERS ARE SUPERVISED: each one is relaunched when it stops, after a pause that grows while it keeps
    # failing. Started once in the background with nothing watching, a reveal worker that died left the container
    # "Up" and healthy while every audit of this miner's jobs lost its artefact, and a judge that died left a mute
    # seat. One worker is one function (reveal_once, judge_once below); supervise runs it, prefixes its lines, and
    # prints the worker's OWN exit code when it stops. A pipeline returns the status of its LAST command, the sed
    # that prefixes, so the worker's code travels back on file descriptor 3 instead of being swallowed.
    # It relaunches only while the miner daemon lives: once the daemon is gone this script returns from
    # `wait "$MPID"`, the container stops, and `restart: unless-stopped` starts the daemon and its workers again.
    # The pause starts at 5 s, doubles up to 300 s, and is back to 5 s after a run of 600 s or more (CHOSEN, not
    # measured: a worker that ran ten minutes was working, so its next stop is a new failure, not the same one).
    # `set +e` comes first: this script runs under `set -e`, and a function started in the background inherits it.
    # Measured without it: the worker's non-zero exit ends the group before its code is reported (every stop then
    # reads "unknown"), and a command of the loop that fails -- a clock that cannot be read -- ends the
    # supervision before the first launch, without printing a line.
    # `sed -u`: without it sed holds the worker's lines in its buffer, and a refused deposit can stay out of
    # `docker logs` for hours (measured with GNU sed 4.9: nothing shown after 3 s without -u, every line with it).
    _sv_num(){ case "$1" in ''|*[!0-9]*) return 1 ;; esac; }
    supervise(){ # supervise <tag> <function>
      set +e
      exec 4>&1
      _sv_pause=5
      while :; do
        _sv_t0="$(date +%s 2>/dev/null)"
        _sv_rc="$( { { "$2" 3>&- 4>&- 2>&1; echo "$?" >&3; } | sed -u "s/^/[$1] /" >&4; } 3>&1 )"
        _sv_t1="$(date +%s 2>/dev/null)"
        # A code, or no code at all (the group was killed before it could report one): never a guessed 0.
        _sv_num "$_sv_rc" || _sv_rc="unknown (no code came back)"
        # The run's length, or `?` when the clock could not be read: an unread length never resets the pause.
        _sv_up="?"
        _sv_num "$_sv_t0" && _sv_num "$_sv_t1" && _sv_up=$((_sv_t1 - _sv_t0))
        [ "$_sv_up" != "?" ] && [ "$_sv_up" -ge 600 ] && _sv_pause=5
        if ! kill -0 "${MPID:-}" 2>/dev/null; then
          echo "[$1] stopped with exit code $_sv_rc after ${_sv_up} s, and the miner daemon is gone: not relaunched (the container stops with the daemon)"
          return 0
        fi
        echo "[$1] STOPPED with exit code $_sv_rc after ${_sv_up} s: relaunched in ${_sv_pause} s (the pause doubles up to 300 s while it keeps stopping)"
        sleep "$_sv_pause"
        _sv_pause=$((_sv_pause * 2)); [ "$_sv_pause" -le 300 ] || _sv_pause=300
      done
    }
    if ! kill -0 "$MPID" 2>/dev/null; then
      echo "[glue] the miner daemon stopped before its workers started: neither reveal nor judge is started, and the container stops with the daemon"
    elif [ -f "/data/keys/$ID.sk" ]; then
      if [ "$REVEAL_ON" = "1" ]; then
        echo "[reveal] key $ID ready -> reveal_worker (primary obligation; no inference)"
        # Output goes to STDOUT, NOT to /tmp. Redirected into the CONTAINER's /tmp, not one line from
        # these workers reaches `docker compose logs miner`, and /tmp vanishes on restart on top of that:
        # a failed revealer or juror is then invisible from outside while the container still reads "Up"
        # — and it is that silence, not the failure, that costs a day of mining. Lines are prefixed so
        # they stay readable in a shared stream.
        # ⛔ DENDRA_SIGN_KEY="$ID", THE RESOLVED IDENTITY, FOR EACH WORKER. Compose hands every process of this
        # container DENDRA_SIGN_KEY=${MINER_ID} (deploy/testnet-miner/docker-compose.yml), the `m-<hash>` join.sh
        # wrote; the daemon RENAMES that keyring entry to the derived `dm1...` and re-points ITS OWN signer
        # (relay_client.set_sign_key), but these workers are other processes, and relay_client reads the
        # variable at import: they signed with a name the keyring no longer holds. Every reveal then went out
        # UNSIGNED -- refused by a relay that requires signatures, the audit never concluding and the client's
        # fee held -- and the "is not a valid name or address" it printed made deploy/join.sh::wait_healthy
        # declare a sound miner NOT HEALTHY. `$ID` is the keyring entry the daemon signs with.
        reveal_once(){
          DENDRA_SIGN_KEY="$ID" python3 reveal_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys
        }
        supervise reveal reveal_once &
      fi
      if [ "$JUDGE_ON" = "1" ]; then
        JEP="${DENDRA_JUDGE_ENDPOINT:-${OLLAMA_ENDPOINT:-http://localhost:11434}}"
        JM=""; [ -n "${DENDRA_JUDGE_MODEL_OVERRIDE:-}" ] && JM="--model-id ${DENDRA_JUDGE_MODEL_OVERRIDE}"
        # A juror that judges on the SAME endpoint as the miner infers its reference on the very backend
        # it is supposed to check. Say so at startup rather than discovering it afterwards.
        # ONE EXCEPTION, AND IT IS A ROLE: the judge on the CPU (a machine without a usable GPU, deploy/hw_probe.sh
        # --role: judge). There the miner SERVES the judge model, on the CPU instance, so one engine holds one
        # model for both and nothing is evicted; the warning would send the operator after a fault it does not have.
        if [ "$JEP" = "${OLLAMA_ENDPOINT:-}" ]; then
          if [ -n "${OLLAMA_MODEL:-}" ] && [ "${OLLAMA_MODEL:-}" = "${DENDRA_JUDGE_MODEL_ID:-}" ]; then
            echo "[judge] judge role on the CPU: the juror and the miner share $JEP and ONE model ($OLLAMA_MODEL), so neither evicts the other"
          else
            echo "[judge] WARNING: juror and miner share $JEP (point DENDRA_JUDGE_ENDPOINT at a second instance)"
          fi
        fi
        # The juror must not start before its model exists. `judge-model-init` pulls ~19 GB in the
        # background and NOTHING depends on it (no `depends_on`). The key `<id>.sk` is written within
        # seconds, so the juror would start long before the download finished and return 404 on every job.
        # A pull that FAILS is retried (`restart: on-failure`): a network failure heals on its own, a full disk
        # only once space is freed on it, and a tag that does not exist fails at every retry, the seat staying
        # MUTE. So wait for the model, say so, and shout when it drags.
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
          kill -0 "$MPID" 2>/dev/null || break
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
        # "ready" IS SAID ONLY OF A JUDGE THAT CAN VOTE. It used to follow the MUTE SEAT warning as well, so the
        # last judge line of a seat that will never vote read "ready" -- and the last line is the one read.
        # The daemon is read too: a wait that the daemon's stop ended is neither a model found nor 30 min spent.
        JLIVE=1; kill -0 "$MPID" 2>/dev/null || JLIVE=0
        if [ "$JOK" = "1" ] && [ "$JLIVE" = "1" ]; then
          echo "[judge] judge model AVAILABLE on $JEP after ${jw}s"
          echo "[judge] key $ID ready -> judge_worker (committee role; judge Ollama=$JEP)"
        elif [ "$JLIVE" = "1" ]; then
          echo "[judge] WARNING: judge model ${JWM:-?} not found on $JEP after 30 min."
          echo "[judge] The worker starts anyway, but without its model it does not vote -> MUTE SEAT, until the model is on $JEP (judge-model-init retries a failed pull)."
          echo "[judge] Check: docker compose logs judge-model-init  (pull failed? disk full? tag missing?)"
          echo "[judge] judge_worker started WITHOUT its model (committee role; judge Ollama=$JEP): a MUTE SEAT, it cannot vote until the model is there"
        else
          echo "[judge] the miner daemon stopped during the wait for the judge model (${jw}s): judge_worker is NOT started, and the container stops with the daemon"
        fi
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
        # DENDRA_SIGN_KEY="$ID" is a different matter, and required: the juror's deposits are signed by the
        # resolved identity, for the reason given at reveal_worker above.
        # Supervised like the reveal worker: a judge_worker that stops -- its startup probe refused, its keys
        # unreadable, an exception -- is relaunched after the growing pause, and its exit code is printed.
        judge_once(){
          DENDRA_SIGN_KEY="$ID" python3 judge_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
            --reveal-grace "${DENDRA_REVEAL_GRACE:-2}" --adjudicate $JM
        }
        if [ "$JLIVE" = "1" ]; then
          supervise judge judge_once &
        fi
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
    # back, with its workers. The workers are supervised by `supervise` above, each in its own background
    # loop: /bin/sh has no `wait -n`, so this script cannot wait on the daemon and the workers at once, and
    # a worker's stop is answered where it happens -- its exit code printed, then a relaunch.
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
  final-season)
    # Final Testnet Season programme service (ADR-047). Refuses to start without
    # DENDRA_FINAL_SEASON_START_HEIGHT.
    exec python3 final_season_server.py ;;
  final-season-generator)
    exec python3 final_season_generator.py ;;
  final-season-payout)
    # The weekly payment, run BY HAND from the generator's service (it mounts the programme keyring):
    #   docker compose run --rm final-season-generator final-season-payout --week N [--yes]
    # `run` replaces the service's command, so the role is named again as the first word.
    exec python3 final_season_payout.py --rankings http://final-season:8093/final-season/v1/ranking "$@" ;;
  capacity)
    # Network capacity registry: hardware inventory and served models, DECLARED by operators and not
    # proven on-chain. Read it as an inventory, never as a proof of compute power. Persisted on DISK
    # (DENDRA_CAPACITY_DB): a registry that forgets on restart is a bug.
    exec python3 capacity_server.py ;;
  *)
    echo "unknown role: $ROLE (relay|gateway|exporter|faucet|miner <id>|proof|points|capacity|final-season|final-season-generator|final-season-payout|cli ...)"; exit 2 ;;
esac
