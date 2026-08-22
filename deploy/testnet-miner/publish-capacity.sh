#!/usr/bin/env bash
# publish-capacity.sh — declare this machine's hardware to the network capacity registry.
#
# WHY THIS FILE EXISTS. `join.sh` publishes the inventory; the docker-compose kit never did. An
# operator who starts the miner with `docker compose up` — the documented path of THIS kit — therefore
# runs a node the registry has never heard of, and the public /network page shows zero.
#
# That is not a cosmetic gap. The chat page reads `verified.live_nodes` from the same registry and
# REFUSES to accept a prompt while it is zero, on purpose: a page that cannot confirm a miner is
# serving must not invite one. So a missing inventory does not merely under-report the network — it
# closes the public chat, while the miner sits there answering nothing. Both symptoms, one cause, and
# the cause looked exactly like a young network with no operators.
#
# ⚠️ IT RUNS ON THE HOST, NOT IN THE CONTAINER. The miner container has no GPU (the ollama container
# does), so a probe run inside it would report zero cards and publish a FALSE inventory — worse than
# publishing nothing, because it would be displayed as an operator's declaration.
#
# ⚠️ AND IT MUST REPEAT. The registry marks a report stale after 24 h and purges it after 7 days
# (capacity_server.py). A single publication at install time disappears on its own, silently, and the
# chat closes again days later for a reason nobody will connect to this.
#
# Usage — from anywhere:
#   bash deploy/testnet-miner/publish-capacity.sh
#
# Keep it fresh with cron (hourly is ample; the report is a few hundred bytes).
# ⚠️ Keep `bash -lc` and keep the log: cron has a minimal PATH, and a probe that cannot find nvidia-smi
# declares a CPU tier with a smaller model instead of failing.
#   (crontab -l 2>/dev/null; echo "17 * * * * bash -lc 'bash $PWD/deploy/testnet-miner/publish-capacity.sh' >> /tmp/dendra-capacity.log 2>&1") | crontab -
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PROBE="$ROOT/deploy/hw_probe.sh"
ENVF="$HERE/.env"

say(){ printf '%s\n' "$*"; }
die(){ printf '  [STOP] %s\n' "$*" >&2; exit 1; }

[ -r "$PROBE" ] || die "hardware probe not found: $PROBE"
[ -r "$ENVF" ]  || die "configuration not found: $ENVF"

# Read ONLY the two variables needed, by their exact names. A broad pattern such as `^DENDRA_` would
# also match the relay token, which lives in this very file — and a secret read by accident is a
# secret that eventually gets printed.
MINER_ID="$(sed -n 's/^MINER_ID=//p'          "$ENVF" | head -1 | tr -d '\r"')"
NODE="$(sed -n 's/^DENDRA_NODE=//p'           "$ENVF" | head -1 | tr -d '\r"')"
: "${DENDRA_CAPACITY_URL:=$(sed -n 's/^DENDRA_CAPACITY_URL=//p' "$ENVF" | head -1 | tr -d '\r"')}"
[ -n "$MINER_ID" ] || die "MINER_ID missing from $ENVF"

# The registry URL. DENDRA_CAPACITY_URL wins; DENDRA_NODE is only a fallback.
#
# ⚠️ DENDRA_NODE IS NOT A RELIABLE SOURCE FOR IT. In the recommended setup the miner runs its OWN node
# beside it, so the value in this file is deliberately CONTAINER-LOCAL (`tcp://host.docker.internal:26657`)
# -- correct for the miner, useless as a public endpoint. Deriving the registry from it yields
# `https://host.docker.internal/capacity`, which resolves to nothing from the host.
#
# That failure would be both silent and delayed: the install-time publication succeeds (it is made from
# the operator's public endpoint), then every scheduled run posts into the void, and 24 h later the report
# ages out -- /network falls back to zero and the public chat closes its composer, for a reason nobody
# would connect to an installation made the day before.
#
# So a host that cannot be reached from outside is REFUSED, never posted to. An unreachable registry is a
# configuration error to be told, not an attempt to be made.
URL="${DENDRA_CAPACITY_URL:-}"
if [ -z "$URL" ] && [ -n "$NODE" ]; then
  HOST="$(printf '%s' "$NODE" | sed -E 's#^[a-z]+://##; s#:[0-9]+$##; s#/.*$##')"
  case "$HOST" in
    host.docker.internal|localhost|127.*|0.0.0.0|::1|[[]::1[]]|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
      die "DENDRA_NODE points at a LOCAL node ($HOST), which is correct for the container but cannot be
       the public capacity registry. Nothing was published -- posting there would fail silently and this
       node would drop off /network 24 h later, closing the public chat with it.
       Fix: add the operator's PUBLIC endpoint to $ENVF, on its own line:
         DENDRA_CAPACITY_URL=https://HOST/capacity      (HOST = the operator's public host)
       deploy/join.sh writes that line when it generates the kit; a hand-written .env will not have it." ;;
    "") die "DENDRA_NODE is present but carries no host" ;;
    *) URL="https://$HOST/capacity" ;;
  esac
fi
[ -n "$URL" ] || die "cannot determine the registry URL (neither DENDRA_CAPACITY_URL nor DENDRA_NODE in $ENVF)"

# ⚠️ THE PROBE MUST NOT INHERIT A CRIPPLED PATH. `nvidia-smi` does not live in a standard bin directory
# on every host — under WSL it is /usr/lib/wsl/lib/nvidia-smi, on container hosts /usr/local/nvidia/bin.
# A scheduler (cron, systemd timer) runs with a MINIMAL PATH, not a login shell: the probe then finds no
# nvidia-smi, concludes "no GPU", drops to the CPU tier and declares a SMALLER MODEL than the one this
# miner actually serves. Nothing fails, nothing is logged — a FALSE inventory is published and displayed
# as the operator's own declaration.
export PATH="$PATH:/usr/lib/wsl/lib:/usr/local/nvidia/bin:/opt/nvidia/bin:/usr/local/cuda/bin"

JSON="$(bash "$PROBE" --json "$MINER_ID" 2>/dev/null | tail -1)"
case "$JSON" in
  '{'*) : ;;
  *) die "hardware probe unreadable — nothing published (an empty declaration is worth less than none)" ;;
esac

# READ THE FIELD BY ITS KEY — never match the document. Searching the text for `"gpu_count":0` is a
# predicate on bytes, and it answers "not zero" to two situations that are nothing alike: a report
# that says one card, and a report where the field is MISSING. It also answers "not zero" to a report
# that says zero with a space after the colon. All three then skip the cross-check below, which is the
# one that decides whether this machine may publish at all.
# The report is emitted flat, on one line, by the probe in this same kit, so the key anchors the read
# — and no interpreter is required, which matters on a host that has none (see the read-back at the
# end of this file).
json_uint(){ printf '%s' "$2" | sed -nE "s/.*\"$1\"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p" | head -1; }

# ⛔ A DECLARATION THAT CONTRADICTS A DIRECTLY OBSERVABLE FACT IS NOT PUBLISHED.
# The PATH repair above covers the locations we know; this covers the ones nobody has hit yet. If the
# probe says "no GPU" while nvidia-smi answers with one, the probe is UNDER-declaring — and an
# under-declared inventory is not a smaller truth, it is a false one: the tier and the advertised model
# fall with it. Refusing is the only honest outcome, because publishing keeps the page looking healthy.
# The count feeds a SAFETY guard, so its absence is an ERROR and never a permissive default: a report
# without the field is not a report of zero cards, it is a report nobody can read — and reading it as
# "no GPU declared" would skip the very cross-check below.
GPU_COUNT="$(json_uint gpu_count "$JSON")"
[ -n "$GPU_COUNT" ] || die "the probe's report carries no readable gpu_count — nothing published.
       The declaration cannot be checked against this machine, and an unchecked inventory is shown
       on the public page as YOUR declaration."
if [ "$GPU_COUNT" -eq 0 ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -q .; then
    say "  [STOP] the probe declares NO GPU while nvidia-smi reports one:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/           /'
    say "         NOTHING was published. A false inventory is shown as YOUR declaration, and it downgrades"
    say "         both the tier and the model this node advertises."
    say "         Run it from a login shell, so the probe sees the environment you see:"
    say "           bash -lc 'bash $HERE/publish-capacity.sh'"
    exit 1
  fi
fi

# `miner_ids` carries the ON-CHAIN identity of this machine. Without it the registry cannot tell an
# anonymous declaration from an effectively staked node, and the `verified` subtotal — the only one a
# public surface displays — would stay at zero even after a successful publication.
#
# ⚠️ THE NAME IN THE .env IS A REQUEST, NOT AN IDENTITY. MINER_ID holds the `m-<hash>` join.sh derives;
# miner.py ALIGNS it to the identifier the chain accepts (`dm1…`) and persists THAT one in the
# miner volume, at /data/keys/identite-resolue. Only the aligned form can exist in the on-chain miner
# registry, so a declaration carrying the .env name lists an identity the registry's cross-check can
# never match: the POST returns 200, `verified.live_nodes` stays 0 and the public chat stays closed
# with a green publication as the only trace. `node_id` above is a DIFFERENT field — the key the
# registry files the report under — and stays exactly as the probe emitted it.
#
# Same adoption rule as the container glue (docker/entrypoint-services.sh): strip ALL whitespace, and
# adopt ONLY a derived identifier. Blanks pass a non-empty test and junk is a corrupt memory; neither
# is an identity. The configured name stays the fallback, so a stopped miner still publishes hardware.
IDENTITY="$MINER_ID"
RESOLVED=""
if command -v docker >/dev/null 2>&1; then
  RESOLVED="$( (cd "$HERE" && docker compose exec -T miner cat /data/keys/identite-resolue) 2>/dev/null | tr -d '[:space:]' )"
fi
case "$RESOLVED" in
  dm1*) IDENTITY="$RESOLVED" ;;
  *)    say "  [i] the identity resolved by the daemon is not readable (miner stopped, or no alignment yet)."
        say "      Declaring the configured name $MINER_ID — the registry verifies it only if that exact"
        say "      name exists on-chain. Start the miner once, then run this again." ;;
esac
JSON="$(printf '%s' "$JSON" | sed -E "s#\}\$#,\"miner_ids\":[\"$IDENTITY\"]}#")"

# ADR-045 (10) — SIGNER QUAND ON PEUT, POSTER QUAND MEME SINON.
# The `verified` block of /capacity now counts only reports whose signature goes back to the OPERATOR
# of the miner id they name: NAMING a staked id is no longer enough, and neither is a chosen `node_id`
# that looks like one. But an unsigned deposit is still ACCEPTED -- one does not add a requirement to
# an operator that was running yesterday; that is the lesson the relay paid for.
# The body is signed EXACTLY as it leaves: the signature covers a digest of those bytes, so
# re-serialising between signing and sending produces a different body.
SIGN_ARGS=()
SIGN_KEY="$(sed -n 's/^DENDRA_SIGN_KEY=//p' "$ENVF" 2>/dev/null | head -1 | tr -d '\r"')"
SIGNEUR="$ROOT/services/capacity_sign.py"
if [ -n "$SIGN_KEY" ] && [ -r "$SIGNEUR" ] && command -v python3 >/dev/null 2>&1; then
  while IFS= read -r _l; do SIGN_ARGS+=("$_l"); done < <(
    printf '%s' "$JSON" | DENDRA_SIGN_KEY="$SIGN_KEY" MINER_ID="$MINER_ID" DENDRA_NODE="$NODE" \
      python3 "$SIGNEUR" 2>/dev/null || true)
  if [ "${#SIGN_ARGS[@]}" -gt 0 ]; then
    say "  [i] report SIGNED (${#SIGN_ARGS[@]} header arguments) -> it can enter the verified block"
  else
    say "  [!] signing IMPOSSIBLE (key, keyring or node) -> UNSIGNED deposit, accepted but outside the verified block"
  fi
else
  say "  [i] no signing key configured -> UNSIGNED deposit, accepted but outside the verified block"
fi
CODE="$(printf '%s' "$JSON" | curl -s -m 15 -o /dev/null -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' "${SIGN_ARGS[@]}" --data-binary @- "$URL")"

if [ "$CODE" = "200" ]; then
  say "  [OK] inventory published -> $URL  (identity $IDENTITY)"
  # READ THE REGISTRY BACK: an accepted POST does not prove the node is COUNTED. `verified` counts only
  # identities present in the on-chain miner registry, so an unregistered miner would publish
  # successfully and still never appear — leaving the chat closed with no error anywhere.
  #
  # ⚠️ THE READ-BACK IS EXTRA INFORMATION, NOT THE VERDICT ON THE PUBLICATION. It needs python3, and a
  # miner installed the documented way runs Docker and nothing else — that host is the NOMINAL case,
  # not an edge one. Left as the last command of the script under `pipefail`, its absence became the
  # exit status of a run that had just published: 127 in the cron log, hourly, for a node that was
  # correctly declared. What this script reports is what it PUBLISHED; a missing interpreter is a
  # missing line of output.
  if command -v python3 >/dev/null 2>&1; then
    curl -s -m 15 "$URL" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print("     (registry unreadable on read-back)"); raise SystemExit
n=d.get("verified",{}).get("live_nodes")
print(f"     verified.live_nodes = {n}   <- the number the chat gate reads")
if not n: print("     [!] still 0: this identity does not exist in the ON-CHAIN miner registry.")' 2>/dev/null || true
  else
    say "     (no python3 here -> read-back skipped. The publication above stands; to see the counter"
    say "      the chat gate reads: curl -s $URL )"
  fi
  exit 0
fi
say "  [!] publication REFUSED (HTTP $CODE) -> $URL"
say "      /network will stay at zero for this node, and the public chat will stay closed."
# AND IT EXITS NON-ZERO. A refusal that returns success is invisible to everything that schedules this
# script: cron mails nothing, a wrapper reads 0, and the node quietly drops off /network 24 h later —
# the exact silence the whole file exists to close.
exit 1
