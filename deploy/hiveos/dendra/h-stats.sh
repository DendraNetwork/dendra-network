#!/usr/bin/env bash
# h-stats.sh -- the HiveOS agent sources it every few seconds and reads two variables from it:
#   khs    the hashrate in kH/s. Always 0: an inference miner computes no hashes, and a number of requests
#          or tokens shown as "H/s" would be a hashrate this miner does not have. HiveOS therefore shows 0,
#          and its hashrate watchdog must be off for this rig, or it restarts the miner in a loop
#          (deploy/hiveos/README.md).
#   stats  a JSON object: hs [0], hs_units, algo, ver (this package's version, the kit_version of the clone
#          it installed, and the miner's health: the verdict of its last hourly check and that check's age),
#          uptime (since the miner's container started), and ar = [commits anchored, commits refused], read
#          from the miner's own heartbeat (modea/heartbeat.py, the file STATUS_FILE_DEFAULT inside the miner's
#          container): create-commit transactions since the miner's process started, anchored when their
#          commitment was then read on the chain, refused when the chain answered them with a non-zero code
#          (miner._COMMITS says what neither counts).
# The health goes in `ver` because the HiveOS dashboard shows `ver` and does not show unknown fields.
# It is read from deploy/testnet-miner/miner-health.last.json, the document the hourly check writes on every
# run (deploy/testnet-miner/miner_health.sh), and judged as the Dendra application judges it: `health ok`,
# `health fail` or `health ??` (not everything measured), then the age of that run, and `stale` once it is older
# than two of the periods the document names. A document that names no period has its age shown and its
# freshness not judged: `stale?`, as the application says "age not judged", never shown as fresh. No document
# is `health never`; a file that is not a valid document (no time, no exit code, no counts) is `health unread`.
# Neither is ever `ok`.
# The judge's memory mode follows the health in `ver`, read from the same heartbeat (key judge_memory, written by the
# daemon's memory watch): `judge resident`, `judge on-demand`, `judge on-demand held` when the guard's gate is closed
# (the model is let in again when room returns), `judge ?` when the block is there and cannot be read, and `judge stale`
# when its time is older than the bound the miner's own self-test applies (modea/heartbeat.py::max_age_s: two of the
# longest single step of the daemon's loop, 2 * max(600, OLLAMA_TIMEOUT), the timeout read from the miner container's
# environment): a daemon that stopped does not keep a mode on the dashboard. A heartbeat that cannot be read, or carries
# no such key (the judge is not requested, or the daemon is older than the memory watch), adds nothing: no word is
# better than a word nobody read.
# Zero is a reading, an absence is not. `ar` is written only when the heartbeat carries both counters as whole
# numbers: a miner that anchored nothing shows [0, 0]; a heartbeat that cannot be read, or that does not carry
# the counters, omits `ar`, so unknown is never shown as zero. `uptime` follows the same rule.
# Sourced by the agent: it never exits, never sets an option, and every other name it uses is local.

_dendra_hive_stats(){
  local here ver kit cid hb started py last cenv
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  ver="$(awk 'index($0, "CUSTOM_VERSION=") == 1 { print substr($0, 16); exit }' "$here/h-manifest.conf" 2>/dev/null)"
  kit="$(head -1 "${DENDRA_DIR:-$HOME/dendra-network}/docker/KIT_VERSION" 2>/dev/null | tr -dc '0-9')"
  last="${DENDRA_DIR:-$HOME/dendra-network}/deploy/testnet-miner/miner-health.last.json"
  case "$ver" in ""|*[!0-9.]*) ver="?" ;; esac
  [ -n "$kit" ] || kit="?"
  cid=""; hb=""; started=""; py=""; cenv=""
  if command -v docker >/dev/null 2>&1; then
    cid="$(timeout 5 docker ps -q --filter label=com.docker.compose.project=dendra-miner --filter label=com.docker.compose.service=miner 2>/dev/null | head -1)"
  fi
  if [ -n "$cid" ]; then
    hb="$(timeout 5 docker exec "$cid" cat /tmp/dendra-miner-status.json 2>/dev/null)"
    started="$(timeout 5 docker inspect -f '{{.State.StartedAt}}' "$cid" 2>/dev/null)"
    cenv="$(timeout 5 docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null)"
  fi
  if command -v python3 >/dev/null 2>&1; then
    py="$(python3 -I -c '
import datetime, json, sys, time
ver, kit, hb, started, last, cenv = sys.argv[1:7]

def health(path, now):
    """The last hourly check, as one word and an age: never -- no run recorded --, unread -- a file that is
    not the document --, or ok / fail / ?? with the age of the run, `stale` past two of its periods, and `stale?`
    when the document names no period (its freshness cannot be judged)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return "health never"
    except (OSError, ValueError):
        return "health unread"
    try:
        doc = json.loads(raw)
    except ValueError:
        return "health unread"
    if not isinstance(doc, dict):
        return "health unread"
    ep, rc, s = doc.get("generated_epoch"), doc.get("rc"), doc.get("summary")
    if (type(ep) is not int or type(rc) is not int or rc not in (0, 1, 2) or not isinstance(s, dict)
            or not all(type(s.get(k)) is int for k in ("ok", "ko", "unmeasured", "checks"))):
        return "health unread"
    age = max(0, int(now) - ep)
    m = age // 60
    a = ("%dm" % m) if m < 120 else (("%dh" % (m // 60)) if m < 2880 else ("%dd" % (m // 1440)))
    period = doc.get("schedule_period_s")
    if type(period) is not int or period <= 0:
        fresh = " stale?"
    else:
        fresh = " stale" if age > 2 * period else ""
    return "health %s %s%s" % ({0: "ok", 1: "fail", 2: "??"}[rc], a, fresh)

def stale_bound(env_text):
    """How old the judge_memory block of the daemon may be, in seconds: modea/heartbeat.py::max_age_s, which is
    2 * max(DENDRAD_CALL_BOUND_S, OLLAMA_TIMEOUT) with DENDRAD_CALL_BOUND_S at 600 and OLLAMA_TIMEOUT at 600 when the
    container sets none. `env_text` is the environment of the container, one NAME=value per line; None when it was not
    read (an empty answer) or holds a timeout that is no whole number."""
    if not env_text.strip():
        return None
    timeout = 600
    for line in env_text.splitlines():
        if line.startswith("OLLAMA_TIMEOUT="):
            value = line[len("OLLAMA_TIMEOUT="):]
            if not value.isdigit():
                return None
            timeout = int(value)
    return 2 * max(600, timeout)

def judge_word(text, now, bound):
    """The judge memory mode from the heartbeat of the daemon (key judge_memory, written by its memory watch), as a
    word for `ver`: " judge resident" or " judge on-demand", with " held" when the gate of the guard is closed (the
    model is not let in until room returns); " judge ?" when the block is there and its mode or its time cannot be
    read, or the bound it is judged by could not be read (`bound` is None); " judge stale" when the block is older
    than `bound` seconds at `now`; nothing when the heartbeat cannot be read or carries no such key (the judge is not
    requested, or the daemon is older than the memory watch: nothing is claimed either way)."""
    try:
        doc = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(doc, dict) or "judge_memory" not in doc:
        return ""
    jm = doc["judge_memory"]
    at = jm.get("at") if isinstance(jm, dict) else None
    if (not isinstance(jm, dict) or jm.get("mode") not in ("resident", "on-demand")
            or isinstance(at, bool) or not isinstance(at, int) or at <= 0):
        return " judge ?"
    if bound is None:
        return " judge ?"
    if max(0, int(now) - at) > bound:
        return " judge stale"
    return " judge " + jm["mode"] + (" held" if jm.get("gate") == "closed" else "")

out = {"hs": [0], "hs_units": "khs", "algo": "dendra",
       "ver": ver + " kit" + kit + " " + health(last, time.time()) + judge_word(hb, time.time(), stale_bound(cenv))}

def counters(text):
    """[anchored, refused] when the heartbeat carries both as whole numbers, else None (not read)."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    a, r = doc.get("commits_anchored"), doc.get("commits_refused")
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (a, r)):
        return None
    return [a, r]

ar = counters(hb)
if ar is not None:
    out["ar"] = ar
try:
    t = datetime.datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    up = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
    if t.year > 1 and up >= 0:
        out["uptime"] = up
except ValueError:
    pass
print(json.dumps(out, separators=(",", ":")))
' "$ver" "$kit" "$hb" "$started" "$last" "$cenv" 2>/dev/null)"
  fi
  # Without python3 nothing is parsed, so nothing read is reported: the fixed fields only, no `ar`, and the
  # health said unread. ($ver and $kit hold digits, dots or "?" only: nothing in them needs quoting in JSON.)
  [ -n "$py" ] || py="$(printf '{"hs":[0],"hs_units":"khs","algo":"dendra","ver":"%s kit%s health unread"}' "$ver" "$kit")"
  printf '%s' "$py"
}

stats="$(_dendra_hive_stats)"
khs=0
unset -f _dendra_hive_stats
