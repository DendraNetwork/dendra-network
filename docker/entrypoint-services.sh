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
    # gateway_fund funds the free tier, and its failure must be visible. Swallowing it with `|| true` would give
    # a healthy-looking gateway that serves no free-tier job and logs nothing, which reads like a quiet
    # network. A first failure usually means the chain is not ready at boot, so this loop retries, then
    # prints an alert. It stays non-fatal on purpose: the paid tier does not depend on this funding, and
    # stopping the gateway would punish paying clients for a free-tier outage.
    _i=0
    until python3 gateway_fund.py; do
      _i=$((_i+1))
      if [ "$_i" -ge 10 ]; then
        echo "alert: gateway_fund failed $_i times -> gateway not funded, the free tier will serve no job (the paid tier still works). Diagnose before announcing anything: docker logs <gateway> | grep gateway_fund" >&2
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
    # This probe only waits for the relay to answer. Against a protected relay, calling /list without an
    # auth header gets a 401, `urlopen` raises, and the loop would never break: 120 s lost on every container
    # start before the miner starts anyway. So the token is sent, and a 401 counts as "the relay answers":
    # the service is there and only access is refused, a case join.sh already reports upstream.
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
    sys.exit(0 if e.code in (401, 403) else 1)   # the relay answers: it is alive
except Exception:
    sys.exit(1)
PROBE
      i=$((i+1)); sleep 2
    done
    # No mining model on the CPU, checked on the engine before the miner starts (serve_guard.py): the installers
    # decide the role (deploy/hw_probe.sh --role), and every other way of starting this container goes through
    # here. 0 on a GPU, or the judge role on the CPU; 2 refused; 3 not measured, in which case the container
    # stops and its restart policy asks again. It runs before the daemon, so before anything is registered.
    # >>> serve-guard
    python3 serve_guard.py || exit $?
    # <<< serve-guard
    # ADR-026/028: DENDRA_MINER_JUDGE=1 makes this miner also sit on the optimistic audit committee
    # (reveal + verdict). Without jurors, audited jobs vindicate themselves at timeout and the audit has no
    # effect. It serves both topologies: flag the work miners, or run dedicated CPU-only miner containers
    # as jurors when GPUs are scarce. judge_worker.py exits if the key <id>.sk is missing (miner
    # writes it at registration), so the miner starts first, then the jurors once the key exists.
    # Off by default (0: miner behaviour unchanged).
    #
    # Two switches, because revealing is an obligation and judging is a role:
    #   - revealing is an obligation of the primary: no inference, one POST to the relay per audited job.
    #     Without it the audit has nothing to judge, the job defers without bound, the client's fee stays
    #     held forever, and the primary is neither paid nor sanctioned;
    #   - judging is a role: it needs a second Ollama instance (otherwise the juror computes its reference
    #     on the backend it is meant to check) and concerns committee members only.
    # With one flag for both, revealing would cost what the role costs, nobody would turn it on, and
    # primaries would stop revealing without any report.
    # So DENDRA_MINER_REVEAL defaults to 1: revealing is the counterpart of being paid for a job.
    # DENDRA_MINER_JUDGE stays off (0).
    REVEAL_ON="${DENDRA_MINER_REVEAL:-1}"
    JUDGE_ON="${DENDRA_MINER_JUDGE:-0}"
    if [ "$REVEAL_ON" != "1" ] && [ "$JUDGE_ON" != "1" ]; then
      exec python3 miner.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
        --faucet "${FAUCET:-http://chain:4500}" --backend "${BACKEND:-ollama}"
    fi
    python3 miner.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
      --faucet "${FAUCET:-http://chain:4500}" --backend "${BACKEND:-ollama}" &
    MPID=$!
    # judge_worker exits if the key <id>.sk is missing (miner writes it at registration): start the
    # miner first, wait for the key, then start the workers.
    # Wait on the identity the daemon actually registers under, not the one that was asked for. `$ID` comes
    # from the .env (`MINER_ID`), which join.sh derives as `m-<hash>`, but miner.py aligns it to the
    # identifier the chain accepts (`a.id = align_identity(...)`) before naming any file, so the key is
    # written as `dm1....sk`. Waiting on the requested name would time out, and neither worker would start:
    # with no reveal_worker a sampled job has no artifact to judge, the client's fee stays held, and the
    # miner is neither paid nor slashed, while the container reads "Up".
    # The daemon persists the accepted identity (`resolved-identity`, same volume): read it rather than
    # guess, and fall back to `$ID` only when the file is absent. A volume written by an older kit holds it
    # under its former name, which is read when the current one is absent, until the daemon moves it at
    # its start.
    # The waits below end with the daemon. This one (up to 180 s) and the wait for the judge model (up to
    # 30 min) both check `kill -0 "$MPID"` on every round: if the daemon stops during either wait, no worker
    # is started and the script reaches `wait "$MPID"` at once, so the container stops with the daemon.
    EFF="$ID"
    j=0
    while [ $j -lt 90 ]; do
      kill -0 "$MPID" 2>/dev/null || break
      _rf=/data/keys/resolved-identity; [ -s "$_rf" ] || _rf=/data/keys/identite-resolue
      if [ -s "$_rf" ]; then
        # All whitespace, not just CR/LF: a file holding only blanks passes `-s` (non-empty) and would
        # survive `tr -d '\r\n'` as spaces, making the entrypoint look for `/data/keys/   .sk`.
        # Only a derived identifier is adopted, as in miner: a corrupt file is ignored.
        _e="$(tr -d '[:space:]' < "$_rf" 2>/dev/null)"
        case "$_e" in dm1*) EFF="$_e" ;; esac
      fi
      [ -f "/data/keys/$EFF.sk" ] && break
      j=$((j+1)); sleep 2
    done
    [ "$EFF" = "$ID" ] || echo "[glue] identity resolved by the daemon: '$ID' -> '$EFF' (workers follow it)"
    ID="$EFF"
    # The workers are supervised: each one is relaunched when it stops, after a pause that grows while it keeps
    # failing. Started once in the background with nothing watching, a dead reveal worker left the container
    # "Up" while every audit of this miner's jobs lost its artifact, and a dead judge left a silent seat. One
    # worker is one function (reveal_once, judge_once below); supervise runs it, prefixes its lines, and prints
    # the worker's own exit code when it stops. A pipeline returns the status of its last command (the sed that
    # prefixes), so the worker's code comes back on file descriptor 3.
    # It relaunches only while the miner daemon lives: once the daemon is gone this script returns from
    # `wait "$MPID"`, the container stops, and `restart: unless-stopped` starts the daemon and its workers again.
    # The pause starts at 5 s, doubles up to 300 s, and is back to 5 s after a run of 600 s or more (a chosen
    # value: a worker that ran ten minutes was working, so its next stop is a new failure).
    # `set +e` comes first: this script runs under `set -e`, which a background function inherits. Without it
    # the worker's non-zero exit ends the group before its code is reported, and a failing command of the loop
    # (a clock that cannot be read) ends the supervision before the first launch, without printing anything.
    # `sed -u`: without it sed buffers the worker's lines, and a refused deposit can stay out of `docker logs`
    # for hours (measured with GNU sed 4.9: nothing shown after 3 s without -u, every line with it).
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
        echo "[$1] stopped with exit code $_sv_rc after ${_sv_up} s: relaunched in ${_sv_pause} s (the pause doubles up to 300 s while it keeps stopping)"
        sleep "$_sv_pause"
        _sv_pause=$((_sv_pause * 2)); [ "$_sv_pause" -le 300 ] || _sv_pause=300
      done
    }
    if ! kill -0 "$MPID" 2>/dev/null; then
      echo "[glue] the miner daemon stopped before its workers started: neither reveal nor judge is started, and the container stops with the daemon"
    elif [ -f "/data/keys/$ID.sk" ]; then
      if [ "$REVEAL_ON" = "1" ]; then
        echo "[reveal] key $ID ready -> reveal_worker (primary obligation; no inference)"
        # Output goes to stdout, not to /tmp: redirected into the container's /tmp, no line from these workers
        # would reach `docker compose logs miner`, and /tmp is lost on restart, so a failed revealer or juror
        # would be invisible while the container still reads "Up". Lines are prefixed so they stay readable
        # in a shared stream.
        # Each worker gets DENDRA_SIGN_KEY="$ID", the resolved identity. Compose hands every process of this
        # container DENDRA_SIGN_KEY=${MINER_ID} (deploy/testnet-miner/docker-compose.yml), the `m-<hash>` join.sh
        # wrote; the daemon renames that keyring entry to the derived `dm1...` and re-points its own signer
        # (relay_client.set_sign_key), but these workers are separate processes, and relay_client reads the
        # variable at import. They would sign with a name the keyring no longer holds, so every reveal would go
        # out unsigned and be refused by a relay that requires signatures, and the "is not a valid name or
        # address" error made deploy/join.sh::wait_healthy report a sound miner as not healthy. `$ID` is the
        # keyring entry the daemon signs with.
        reveal_once(){
          DENDRA_SIGN_KEY="$ID" python3 reveal_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys
        }
        supervise reveal reveal_once &
      fi
      if [ "$JUDGE_ON" = "1" ]; then
        JEP="${DENDRA_JUDGE_ENDPOINT:-${OLLAMA_ENDPOINT:-http://localhost:11434}}"
        JM=""; [ -n "${DENDRA_JUDGE_MODEL_OVERRIDE:-}" ] && JM="--model-id ${DENDRA_JUDGE_MODEL_OVERRIDE}"
        # A juror that judges on the same endpoint as the miner computes its reference on the backend it is
        # supposed to check, so this is reported at startup.
        # One exception: the judge role on the CPU (a machine without a usable GPU, deploy/hw_probe.sh --role:
        # judge). There the miner serves the judge model on the CPU instance, so one engine holds one model for
        # both and nothing is evicted; a warning would point the operator at a fault that does not exist.
        if [ "$JEP" = "${OLLAMA_ENDPOINT:-}" ]; then
          if [ -n "${OLLAMA_MODEL:-}" ] && [ "${OLLAMA_MODEL:-}" = "${DENDRA_JUDGE_MODEL_ID:-}" ]; then
            echo "[judge] judge role on the CPU: the juror and the miner share $JEP and one model ($OLLAMA_MODEL), so neither evicts the other"
          else
            echo "[judge] WARNING: juror and miner share $JEP (point DENDRA_JUDGE_ENDPOINT at a second instance)"
          fi
        fi
        # The juror must not start before its model exists. `judge-model-init` pulls ~19 GB in the background
        # and nothing depends on it (no `depends_on`), while the key `<id>.sk` is written within seconds, so
        # the juror would start long before the download finished and get a 404 on every job.
        # A failed pull is retried (`restart: on-failure`): a network failure heals on its own, a full disk only
        # once space is freed, and a tag that does not exist fails at every retry, leaving the seat silent. So
        # this waits for the model, reports progress, and warns when it takes too long.
        # It waits for the model, not just the engine: `/api/tags` answers within seconds while the pull takes
        # minutes. Bounded at 30 min (19 GB over an ordinary link), with progress every 2 min.
        # It waits for the model that will actually be used, not the one the kit downloaded:
        # judge_worker.py::resolve_judge_model applies this precedence: --model-id, then the on-chain pin
        # (modelregistry.audit_judge_model), then DENDRA_JUDGE_MODEL_ID. Waiting on the third while the
        # worker runs with the second would validate a model that never serves.
        # Resolution is best-effort: an unreachable chain or a missing dendrad falls back to the third.
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
          echo "[judge] effective judge model = '$JEFF' ($JSRC), not '$JWM' (DENDRA_JUDGE_MODEL_ID)."
          echo "[judge] The on-chain verdict will nonetheless declare '$JWM': align the two values, otherwise"
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
        # "ready" is printed only for a judge that can vote. Printed after the mute-seat warning, it would make
        # the last judge line of a seat that will never vote read "ready", and the last line is the one people
        # read. The daemon is checked too: a wait ended by the daemon's stop is neither a model found nor a
        # timeout.
        JLIVE=1; kill -0 "$MPID" 2>/dev/null || JLIVE=0
        if [ "$JOK" = "1" ] && [ "$JLIVE" = "1" ]; then
          echo "[judge] judge model available on $JEP after ${jw}s"
          echo "[judge] key $ID ready -> judge_worker (committee role; judge Ollama=$JEP)"
        elif [ "$JLIVE" = "1" ]; then
          echo "[judge] WARNING: judge model ${JWM:-?} not found on $JEP after 30 min."
          echo "[judge] The worker starts anyway, but without its model it does not vote -> a mute seat, until the model is on $JEP (judge-model-init retries a failed pull)."
          echo "[judge] Check: docker compose logs judge-model-init  (pull failed? disk full? tag missing?)"
          echo "[judge] judge_worker started without its model (committee role; judge Ollama=$JEP): a mute seat, it cannot vote until the model is there"
        else
          echo "[judge] the miner daemon stopped during the wait for the judge model (${jw}s): judge_worker is not started, and the container stops with the daemon"
        fi
        # Do not prefix this line with `OLLAMA_ENDPOINT="$JEP"`: it silences the judge.
        # The judge process runs two inferences with two different models:
        #   - its own reference answer, a peer opinion, produced by the miner backend and model
        #     (`backend.generate`, judge_worker.py), which lives on the mining endpoint;
        #   - the verdict, produced by the judge model this node resolves at startup (`llm_judge`),
        #     routed by `judge_endpoint()` with precedence DENDRA_JUDGE_ENDPOINT > OLLAMA_ENDPOINT.
        #     (Resolved per node, not enforced by consensus: `audit_judge_model` is inert on chain,
        #     so nothing here is a model the committee is held to.)
        # Overriding OLLAMA_ENDPOINT is only correct while one Ollama serves both. With a separate judge
        # backend, the override also sends the reference call to the judge endpoint, where the miner's model
        # is not pulled: Ollama answers 404 "model not found", the worker prints "Ollama unreachable", and
        # the seat stays silent at every start (`/api/tags` 200 and `/api/generate` 404, repeatedly).
        # The override is also unnecessary: DENDRA_JUDGE_ENDPOINT routes the verdict on its own, and with it
        # unset, judge_endpoint() falls back to OLLAMA_ENDPOINT, so leaving OLLAMA_ENDPOINT alone is safe in
        # both topologies.
        # DENDRA_SIGN_KEY="$ID" is a different matter, and required: the juror's deposits are signed by the
        # resolved identity, for the reason given at reveal_worker above.
        # Supervised like the reveal worker: a judge_worker that stops (startup probe refused, unreadable keys,
        # an exception) is relaunched after the growing pause, and its exit code is printed.
        judge_once(){
          DENDRA_SIGN_KEY="$ID" python3 judge_worker.py --id "$ID" --relay "$RELAY" --keydir /data/keys \
            --reveal-grace "${DENDRA_REVEAL_GRACE:-2}" --adjudicate $JM
        }
        if [ "$JLIVE" = "1" ]; then
          supervise judge judge_once &
        fi
      fi
    else
      # Name both identities: a message saying "key m-xxxx still missing" while the key exists under `dm1...`
      # would send the operator looking for a file that was never going to appear.
      echo "[glue] WARNING: no key for '$ID' after 180s (asked for: '${1:-m1}', resolved: $( _r=/data/keys/resolved-identity; [ -s "$_r" ] || _r=/data/keys/identite-resolue; [ -s "$_r" ] && tr -d '\r\n' < "$_r" || echo 'none yet' ))"
      echo "[glue]          -> neither reveal nor judge started. The miner runs, but it reveals nothing:"
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
    # Read-only points index: stateless, recomputed from chain data.
    exec python3 points_indexer.py --serve ;;
  final-season)
    # Final Testnet Season programme service (ADR-047). Refuses to start without
    # DENDRA_FINAL_SEASON_START_HEIGHT.
    exec python3 final_season_server.py ;;
  final-season-generator)
    exec python3 final_season_generator.py ;;
  final-season-payout)
    # The weekly payment, run by hand from the generator's service (it mounts the programme keyring):
    #   docker compose run --rm final-season-generator final-season-payout --week N [--yes]
    # `run` replaces the service's command, so the role is named again as the first word.
    exec python3 final_season_payout.py --rankings http://final-season:8093/final-season/v1/ranking "$@" ;;
  capacity)
    # Network capacity registry: hardware inventory and served models, as declared by operators and not
    # proven on chain. Read it as an inventory, never as a proof of compute power. Persisted on disk
    # (DENDRA_CAPACITY_DB) so a restart does not empty it.
    exec python3 capacity_server.py ;;
  *)
    echo "unknown role: $ROLE (relay|gateway|exporter|faucet|miner <id>|proof|points|capacity|final-season|final-season-generator|final-season-payout|cli ...)"; exit 2 ;;
esac
