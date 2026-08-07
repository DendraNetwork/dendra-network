#!/usr/bin/env bash
# launch_public.sh — one-command PUBLIC LAUNCH kit.
#
# Deploys the public incentivized network on the VPS: chain (launch genesis, optimistic verification + veto),
# hardened relay/faucet(PoW)/gateway (fail-closed), The Proof + Season-0 points (compose profile `public`),
# monitoring, and genesis/seeds publication. THEN prints the follow-up sequence (heterogeneous judges, 2nd
# validator, validation run) — the wide announcement stays gated on the validation review.
#
# Prerequisite: ~/.dendra-launch.env generated and GREEN:  tr -d '\r' < deploy/launch/launch_env_check.sh | bash
# Usage (WSL, repo root) — key authentication is detected on its own:
#   tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
# On a VPS that still accepts a root password (prefer keys):
#   export SSHPASS='...' ; tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
set -uo pipefail
VPS="${1:?Usage: ... | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]}"
PUBHOST="${2:-$VPS}"
ENVF="${DENDRA_LAUNCH_ENV:-$HOME/.dendra-launch.env}"
_find_repo(){ local d="$PWD"; while [ "$d" != "/" ]; do
  [ -f "$d/docker-compose.yml" ] && [ -d "$d/deploy" ] && { echo "$d"; return 0; }; d="$(dirname "$d")"; done; return 1; }
REPO="${DENDRA_REPO:-$(_find_repo || true)}"
[ -n "${REPO:-}" ] && [ -d "$REPO" ] || { echo "FAILED: repository root not found (export DENDRA_REPO)"; exit 1; }
die(){ echo "  FAILED: $*"; exit 1; }
SSHO="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
# ── KEY FIRST, PASSWORD ONLY IF ASKED FOR ───────────────────────────────────────────────────────
#
# ⛔ THIS SCRIPT REQUIRED A ROOT PASSWORD TO LAUNCH A NETWORK. `SSHPASS` was mandatory, so an
# operator who had done the right thing — key-only access, password login disabled — could not run
# the launcher at all. The workaround people reach for is to set a dummy value or to re-enable
# password login for one command, and the second one leaves the door open afterwards. A launcher
# that only works on an unhardened machine teaches operators not to harden it.
#
# The key is tried first. `BatchMode=yes` makes the attempt fail fast instead of opening a prompt
# inside a piped script, where stdin is already taken.
if [ -n "${SSHPASS:-}" ]; then
  command -v sshpass >/dev/null || die "SSHPASS is set but sshpass is missing: sudo apt install -y sshpass"
  SSH_MODE="password"; RSH="sshpass -e ssh -n $SSHO root@$VPS"; RSYNC_PFX="sshpass -e"
elif ssh -o BatchMode=yes -o PasswordAuthentication=no $SSHO -n "root@$VPS" true 2>/dev/null; then
  SSH_MODE="key"; RSH="ssh -n -o BatchMode=yes $SSHO root@$VPS"; RSYNC_PFX=""
else
  echo "  FAILED: cannot reach root@$VPS."
  echo "    By KEY (recommended):  ssh-copy-id -i ~/.ssh/id_ed25519.pub root@$VPS"
  echo "      then check:          ssh -o PasswordAuthentication=no -o BatchMode=yes root@$VPS 'echo ok'"
  echo "    By PASSWORD:           export SSHPASS='...' before running this script"
  exit 1
fi
echo "  ssh: authenticating by $SSH_MODE"
command -v rsync   >/dev/null || die "install rsync: sudo apt install -y rsync"

# --- STEP TRACER: detects a block that is SILENTLY SKIPPED ---------------------------------------
# A block with an UNCONDITIONAL header can fail to execute without printing any error: a filesystem
# cache serving a stale version, stdin stolen by a command inside a piped script, a truncated read. The
# launch then declares itself UP with a missing step.
# Rather than guessing, the whole class is DETECTED: every step declares itself, and the end of the
# script refuses to conclude if a mandatory step left no mark. A silent skip becomes a LOUD FAILURE, so
# a public launch can no longer declare success while being incomplete.
_STEPS_DONE=""
# Only the IDENTIFIER is kept (the first word, e.g. "7b)"), not the label: otherwise the final assertion
# would compare whole sentences and fail on the slightest wording change.
step(){ _STEPS_DONE="$_STEPS_DONE|${1%% *}|"; echo "########## $* ##########"; }
_ran(){ case "$_STEPS_DONE" in *"|$1|"*) return 0;; *) return 1;; esac; }
assert_steps(){
  _missing=""
  for s in "$@"; do _ran "$s" || _missing="$_missing $s"; done
  [ -z "$_missing" ] && return 0
  echo "  FAILED: MANDATORY step(s) not executed:$_missing"
  echo "  A block of this script was SILENTLY SKIPPED - the network is potentially incomplete"
  echo "  (for example an unfunded gateway means chat is broken). Announce nothing. Check that the file"
  echo "  being read is up to date (\`grep -c '^step ' $REPO/deploy/launch/launch_public.sh\`) then re-run."
  exit 1
}

step "0) GATE: a GREEN public .env is mandatory"
[ -f "$ENVF" ] || die "public .env missing - run launch_env_check.sh --init then the check"
tr -d '\r' < "$REPO/deploy/launch/launch_env_check.sh" | bash || die "launch_env_check RED - fix before deploying"

step "1) SSH + Docker"
$RSH 'echo ssh OK; docker --version 2>/dev/null || echo NO_DOCKER' || die "SSH failed (IP / password / port 22?)"
$RSH 'docker --version >/dev/null 2>&1' || { echo "  installing Docker..."; $RSH 'curl -fsSL https://get.docker.com | sh' || die "docker install failed"; }

step "2) repository + .env -> VPS:~/dendra"
# DEFAULT-DENY: ship ONLY what the host RUNS (chain source, services, container defs, deploy kit).
# An exclude-list is the wrong shape here — it fails open: any file added to the repo tomorrow lands on a
# public host reachable by password over SSH unless someone remembers to exclude it. The allow-list below
# fails closed instead: a new file has to be named to be shipped. A compromise of the host must not hand
# over the project's internal working material as a bonus.
# --delete-excluded also PURGES anything the host still carries from an earlier, wider sync.
$RSYNC_PFX rsync -az --checksum --delete --delete-excluded -e "ssh $SSHO" \
  --exclude '.git' --exclude '__pycache__/' --exclude '*.pyc' \
  --include 'chain/***' --include 'services/***' --include 'tokenomics/***' --include 'docker/***' --include 'deploy/***' \
  --include 'docker-compose.yml' \
  `# The snapshot body runs ON THIS HOST: the export opens the application database, goleveldb opens` \
  `# it only once, so it needs the node stopped AND the real home. Without this line the script is` \
  `# simply absent from the server and the snapshot ADR-039 promises cannot be produced at all.` \
  --include 'dendra/' --include 'dendra/onchain-staging/***' \
  --exclude '*' \
  "$REPO/" "root@$VPS:~/dendra/" || die "repository rsync failed"
$RSYNC_PFX rsync -az -e "ssh $SSHO" "$ENVF" "root@$VPS:~/dendra/.env" || die ".env rsync failed"
$RSH 'chmod 600 ~/dendra/.env' || true
echo "  repository + .env copied (bench-results/ artefacts STAY local: development data)"

step "3) entrypoint integrity check (anti-truncation)"
$RSH 'cd ~/dendra && for s in docker/entrypoint-chain.sh docker/entrypoint-services.sh; do sh -n "$s" || { echo "  INVALID/TRUNCATED: $s"; exit 3; }; done && echo "  entrypoints OK"' || die "invalid entrypoint on the VPS"

if [ "${DENDRA_FRESH:-0}" = "1" ]; then
  echo "########## 3b) GENESIS RESET (DENDRA_FRESH=1) - purging volumes = start from zero ##########"
  # PUBLIC LAUNCH: start from a CLEAN genesis (Season-0 points at zero, no bench/test data in history).
  # Without this, an existing genesis volume is REUSED -> you would launch public on top of test data.
  $RSH 'cd ~/dendra && docker compose --profile public --profile monitoring down -v 2>/dev/null; docker volume rm dendra_dendra-home 2>/dev/null; true'
  echo "  volumes purged -> the next boot builds a FRESH genesis"
fi

step "3c) CONSENSUS-BREAKING guard"
# WHY. A redeployment WITHOUT a purge replayed the history with a consensus-breaking binary: `wrong
# Block.Header.AppHash`, chain halted. Nothing in the kit prevented it, and `docker restart` even made
# the node look up to date. Compatibility cannot be guessed from a binary: it must be DECLARED.
# `docker/CONSENSUS_EPOCH` (in the repository) carries the epoch of the code; the VPS remembers the epoch
# of the running chain (~/dendra/.consensus_epoch, written after every successful `up`). Different
# epochs + an existing volume + no DENDRA_FRESH=1 => REFUSAL before the build.
LOCAL_EPOCH="$(head -1 "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
[ -n "$LOCAL_EPOCH" ] || die "docker/CONSENSUS_EPOCH missing or unreadable - the consensus guard cannot work without it"
if ! $RSH 'docker volume inspect dendra_dendra-home >/dev/null 2>&1'; then
  echo "  no chain volume on the VPS -> nothing to fork, the guard passes (fresh genesis at boot)"
else
  REMOTE_EPOCH="$($RSH 'head -1 ~/dendra/.consensus_epoch 2>/dev/null | tr -dc 0-9' || true)"
  if [ "${DENDRA_FRESH:-0}" = "1" ]; then
    echo "  DENDRA_FRESH=1: the purge already happened in 3b -> the guard passes (local epoch $LOCAL_EPOCH)"
  elif [ -z "$REMOTE_EPOCH" ]; then
    echo "  FAILED: a chain EXISTS on the VPS but its consensus epoch is UNKNOWN"
    echo "  (a chain from before this guard). There is no way to prove that the deployed code can REPLAY"
    echo "  its history without forking. Two ways out:"
    echo "    - FRESH genesis:   re-run with DENDRA_FRESH=1"
    echo "    - you KNOW it is compatible: sshpass -e ssh root@$VPS 'echo $LOCAL_EPOCH > ~/dendra/.consensus_epoch' then re-run"
    die "unknown consensus epoch on an existing chain - the history is not replayed blindly"
  elif [ "$REMOTE_EPOCH" != "$LOCAL_EPOCH" ]; then
    echo "  FAILED: code = epoch $LOCAL_EPOCH, running chain = epoch $REMOTE_EPOCH."
    echo "  Deploying THIS code on THIS history is a consensus-breaking replay = AppHash panic."
    echo "  A FRESH genesis is mandatory:"
    echo "    re-run with DENDRA_FRESH=1 (purges the volumes, Season-0 points reset to zero)"
    die "incompatible consensus epochs - refusing to reproduce the fork"
  else
    echo "  OK epoch $LOCAL_EPOCH == chain in place ($REMOTE_EPOCH): replay on a compatible history"
  fi
fi

step "4) build + up (core + public + monitoring) - LONG on the first build (~6-8 min)"
# BUILD IDENTITY, computed HERE and nowhere else. The rsync above excludes .git, so the VPS has no
# repository to interrogate: whatever identifies this build has to travel with the command. Without
# it the ARG defaults in Dockerfile.chain stand and the running binary answers `dev` / `unknown` —
# which is what it has been answering, and what makes the ADR-039 snapshot's third leg (the exact
# binary) name no source. `export` rather than a `VAR=x cmd` prefix: a prefix binds to the FIRST
# command only, and `build relay` / `up -d` would run without it.
_BV="$(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo dev)"
_BC="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  build identity: version=$_BV commit=$_BC"
$RSH "cd ~/dendra && export DENDRA_BUILD_VERSION='$_BV' DENDRA_BUILD_COMMIT='$_BC' && docker compose build chain && docker compose build relay && docker compose --profile public --profile monitoring up -d" || die "docker build/up failed"
# The chain now running is the one of THIS epoch: record it for the next 3c guard.
$RSH "printf '%s\n' '$LOCAL_EPOCH' > ~/dendra/.consensus_epoch" || echo "  WARNING: epoch not recorded on the VPS (guard 3c will ask for an explicit decision at the next deployment)"

step "5) wait for RPC (LOCAL — the public firewall opens only AFTER all gates)"
# The wait is done LOCALLY (RSH -> docker exec / curl localhost); public exposure (section 9) happens ONLY
# after the supply assert + on-chain gates are green, so a non-compliant network is never reachable.
ok=0
for i in $(seq 1 90); do
  sleep 6
  # `grep -q latest_block_height` tested the presence of the KEY NAME: a chain that answers but is
  # FROZEN AT 0 (consensus not started, single peer, replay panic) passed this gate as "UP".
  # A NUMERIC HEIGHT > 0 is required. `tr -d ' \t'`: the CometBFT HTTP RPC returns INDENTED JSON, so a
  # compact pattern does not match and would return an EMPTY value -- with no error.
  H=$($RSH "curl -s http://localhost:26657/status 2>/dev/null | tr -d ' \t' | grep -o '\"latest_block_height\":\"[0-9]*\"' | grep -o '[0-9]*' | head -1")
  [ -n "$H" ] && [ "$H" -gt 0 ] 2>/dev/null && { ok=1; break; }
  [ $((i % 5)) -eq 0 ] && echo "  ... waiting for RPC ($i/90)"
done
[ "$ok" = 1 ] || { echo "  RPC silent. Diagnose: ssh root@$VPS 'cd ~/dendra && docker compose ps; docker compose logs --tail 100 chain'"; exit 1; }

step "6) on-chain check of the LAUNCH gates (verification mode + hold + audit quorum)"
$RSH 'cd ~/dendra && docker compose exec -T chain dendrad query jobs params -o json' 2>/dev/null \
  | python3 -c '
import json,sys
p=json.load(sys.stdin).get("params",{})
want={"verification_mode":"1","hold_bps":"10000","audit_min_quorum":"4"}
bad=[f"{k}={p.get(k)}(expected {v})" for k,v in want.items() if str(p.get(k))!=v]
print("  FAIL gates: "+", ".join(bad) if bad else "  OK verification_mode=1 + hold_bps=10000 + audit_min_quorum=4")
print("  audit_sample_bps=%s audit_resolve_timeout=%s silence_slash_bps=%s" % (p.get("audit_sample_bps"),p.get("audit_resolve_timeout"),p.get("silence_slash_bps")))
sys.exit(1 if bad else 0)' || die "on-chain gates NON compliant — announce NOTHING; review the printed output"

step "7) ASSERT supply <= 10,000,000 DNDR (zero-mint) — die BEFORE exposure if non-compliant"
# Zero-mint invariant: exactly ONE denom (udndr) and a supply that never EXCEEDS the 10,000,000 DNDR genesis
# cap (a mint would exceed it; the soft burn only lowers it). A parasite mint/denom blocks the launch.
$RSH 'cd ~/dendra && docker compose exec -T chain dendrad q bank total-supply -o json' 2>/dev/null \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
sup=d.get("supply") or d.get("amount") or []
if isinstance(sup,dict): sup=[sup]
udndr=[c for c in sup if c.get("denom")=="udndr"]
amt=int(udndr[0].get("amount","0")) if udndr else -1
CAP=10000000000000
ok = len(sup)==1 and len(udndr)==1 and 0 < amt <= CAP
print("  OK supply = %d udndr (<= 10,000,000 DNDR, single denom udndr — zero-mint; burned=%d)" % (amt, CAP-amt) if ok else "  FAIL supply="+repr(sup))
sys.exit(0 if ok else 1)' || die "supply NON compliant (expected <=10000000000000 udndr, single denom udndr, >0) — parasite mint/denom -> stop BEFORE any exposure"

step "7b) BOOTSTRAP of the 'gw' subsidy account (otherwise the free tier is stillborn)"
# The gateway normally funds itself AT THE FAUCET, but the faucet is PoW-gated in public: it cannot get
# through on its own, gives up after 40 attempts, and NO job can be opened any more - the client
# receives "[Dendra] the network could not process the request". On a fresh genesis this happens
# SYSTEMATICALLY (a purged volume means a zero balance), and it then has to be repaired by hand.
# The account is therefore bootstrapped here, once, from a genesis pocket. Idempotent: nothing is sent
# when the balance is already sufficient.
GW_MIN="${DENDRA_GW_MIN_UDNDR:-10000000}"
GW_TOPUP="${DENDRA_GW_TOPUP_UDNDR:-100000000}"
GW_ADDR=""
for _ in $(seq 1 30); do
  GW_ADDR="$($RSH "cd ~/dendra && docker compose logs gateway 2>/dev/null | grep -o 'gw=dendra1[a-z0-9]*' | head -1 | cut -d= -f2" 2>/dev/null | tr -d '\r\n ')"
  [ -n "$GW_ADDR" ] && break
  sleep 4
done
if [ -z "$GW_ADDR" ]; then
  echo "  WARN: gw account address not found in the gateway logs - check the free tier by hand."
else
  GW_BAL="$($RSH "cd ~/dendra && docker compose exec -T chain dendrad query bank balances $GW_ADDR -o json --node tcp://localhost:26657" 2>/dev/null | python3 -c 'import json,sys
try:
    b=json.load(sys.stdin).get("balances",[])
    print(next((c["amount"] for c in b if c.get("denom")=="udndr"), "0"))
except Exception:
    print("0")')"
  GW_BAL="${GW_BAL:-0}"
  if [ "$GW_BAL" -lt "$GW_MIN" ] 2>/dev/null; then
    echo "  gw=$GW_ADDR balance=$GW_BAL (< $GW_MIN) -> seeding $GW_TOPUP udndr from 'bob'"
    # ZERO RULE: NEVER test a JSON field with a TEXTUAL predicate. `grep -q '"code":0'` depends on the
    # rendering (spacing, order, presence). The proto3 codec OMITS zero values: on an output where `code`
    # is absent, the grep failed, reporting "gw seeding refused" on a SUCCESSFUL send, and the free tier
    # stayed stillborn on a false alarm. The field is PARSED and read with the zero value of its type.
    if $RSH "cd ~/dendra && docker compose exec -T chain dendrad tx bank send bob $GW_ADDR ${GW_TOPUP}udndr --keyring-backend test --chain-id dendra --node tcp://localhost:26657 --gas-prices 0udndr --yes -o json" 2>/dev/null \
       | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)          # unreadable = WE DO NOT KNOW -> failure (never an assumed success)
sys.exit(0 if int(d.get("code",0))==0 else 1)'; then
      echo "  gw seeded -> restarting the gateway (it had exhausted its faucet attempts)"
      $RSH 'cd ~/dendra && docker compose restart gateway' >/dev/null 2>&1
    else
      echo "  WARN: gw seeding refused (empty 'bob' pocket?) - the free tier will stay inactive and jobs will fail."
    fi
  else
    echo "  OK gw=$GW_ADDR already funded (balance=$GW_BAL udndr)"
  fi
fi

step "7c) operator VRF ANCHORING (verified, not best effort)"
# `entrypoint-chain.sh` attempts this anchoring as a BACKGROUND TASK at boot. A background task can
# generate the key and then anchor nothing, printing NEITHER a success NOR its own failure message. The
# network then runs with `contributors < min_required`, that is with **anti-grinding INACTIVE**, while
# the chain shouts about it at every block in a log nobody reads. A background best effort on a security
# guard is a contradiction: it is replayed here, in the foreground, with confirmation by committed
# transaction. Idempotent (the handler overwrites the key) and NON blocking: without the anchor the
# network runs, it is only less decentralized - and that is SAID loudly rather than refusing to start.
$RSH "cd ~/dendra && tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | docker compose exec -T -e HOME_DIR=/root/.dendra -e CHAIN_ID=dendra -e NODE=tcp://127.0.0.1:26657 -e VAL_KEY=validator chain bash -s" \
  || echo "  [!] operator VRF anchoring NOT confirmed -> the seed stays UNDER-DECENTRALIZED (anti-grinding inactive). Replay it by hand before any measurement that matters."

step "8) publish genesis/seeds"
$RSH "cd ~/dendra && tr -d '\r' < deploy/testnet/publish_network.sh | bash -s -- $PUBHOST" || die "publish_network failed"
# :8088 served by systemd (Restart=always, survives reboot + ssh detach) — a plain nohup dies with the session.
$RSH 'cat >/etc/systemd/system/dendra-netinfo.service <<UNIT
[Unit]
Description=Dendra network-info http (:8088)
After=network.target
[Service]
WorkingDirectory=/root/dendra/deploy/testnet/published
ExecStart=/usr/bin/python3 -m http.server 8088
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable dendra-netinfo 2>/dev/null; systemctl restart dendra-netinfo; sleep 1; curl -sf -m5 http://localhost:8088/network-info.txt | grep -q CHAIN_ID' || die "network-info :8088 KO (systemd dendra-netinfo)"
echo "  network-info served on :8088 (systemd dendra-netinfo)"

step "8b) GUARD - the published host must ACTUALLY answer on the published ports"
# WHY THIS GUARD EXISTS. A launch declared "PUBLIC NETWORK STARTED" after publishing `GENESIS_URL`,
# `SEEDS` and `DENDRA_NODE` on a host that pointed at the website CDN rather than at the VPS: the genesis
# timed out, P2P was unreachable, and NO third party could join the network. Nothing reported it - the
# script published a name without ever checking that it led anywhere. An untested join kit is a dead
# kit, and it is all the more expensive because it is distributed to strangers who have no diagnostic
# means of their own.
#
# The guard tests what the kit PROMISES, from the VPS, on the EXACT name that was published.
# Usual cause of failure: a proxied domain (CDN) where only 80/443 pass. Remedy: re-run with a DNS-only
# subdomain pointing at the VPS (for example `api.<domain>`), not the bare domain.
_pub_fail=""
$RSH "curl -fsS -m 8 -o /dev/null http://$PUBHOST:8088/network-info.txt" >/dev/null 2>&1 \
  || _pub_fail="$_pub_fail network-info(:8088)"
$RSH "curl -fsS -m 8 -o /dev/null http://$PUBHOST:8088/genesis.json" >/dev/null 2>&1 \
  || _pub_fail="$_pub_fail genesis(:8088)"
$RSH "curl -fsS -m 8 -o /dev/null http://$PUBHOST:26657/status" >/dev/null 2>&1 \
  || _pub_fail="$_pub_fail rpc(:26657)"
if [ -n "$_pub_fail" ]; then
  echo "  FAILED: the PUBLISHED host '$PUBHOST' does NOT answer on:$_pub_fail"
  echo "  The published join kit is UNUSABLE: a third party can neither fetch the genesis,"
  echo "  nor join the P2P network, nor query the RPC. The network runs, but it is CLOSED."
  echo "  Most frequent cause: '$PUBHOST' is proxied by a CDN (only 80/443 pass through)."
  echo "  Remedy: re-run with a DNS-only subdomain pointing at the VPS, for example:"
  echo "      ... | bash -s -- $VPS api.<your-domain>"
  die "published host unreachable - nothing is announced until the kit works"
fi
echo "  OK published host '$PUBHOST' REACHABLE on :8088 (genesis + network-info) and :26657 (RPC)"

step "9) firewall — PUBLIC EXPOSURE (LAST: every gate above is green)"
# Open the public ports ONLY here. If supply/gates/publish had failed (die), we never reach this point, so a
# non-compliant network is never exposed.
# 80/443 are NOT optional: Caddy needs 80 for the ACME challenge and 443 for the whole HTTPS front
# (api./chat./proof.), and the nodes POST their capacity report over it. Omitting them left HTTPS shut
# on any host where ufw is actually enabled, right after printing "public ports opened".
# 8092 stays CLOSED on purpose: the capacity registry is reached through Caddy (/capacity), not raw.
# PORTS OPENED HERE ARE ONLY THE ONES THAT LISTEN PUBLICLY. 8651 (gateway), 8080, 8090 and 8091 are
# bound to 127.0.0.1 in docker-compose.yml and are reached through Caddy, so opening them in ufw
# granted nothing — it only made the firewall describe a surface that does not exist, which is the
# kind of rule someone later reads as "these are public". Measured from outside after the change:
# 8651/8080/8090/8091 refuse the connection, 8088 answers. Removing them changes no reachability;
# it stops the firewall from lying. 8088 (genesis + network-info) and 26656/26657/4500/8645 stay:
# those DO listen publicly and are published in network-info.txt.
$RSH 'command -v ufw >/dev/null && { ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 26657/tcp; ufw allow 26656/tcp; ufw allow 8645/tcp; ufw allow 4500/tcp; ufw allow 8088/tcp; }; true' || true
echo "  public ports opened, HTTPS included (Grafana :3000 / Prometheus :9099 NOT exposed — SSH tunnel)"

step "9b) REMOTE proof: the published kit answers from OUTSIDE the VPS"
# Step 8b tests from the VPS (curl over SSH) and BEFORE the firewall is opened: when PUBHOST points at
# the VPS itself, the traffic comes back through the local interface and PASSES even if a firewall (ufw,
# or the provider edge) blocks everything from outside. 8b therefore proves a LOCAL property while
# announcing a REMOTE one. Here, AFTER the firewall, the test runs from the OPERATOR machine (this
# script), that is from the real internet: what a third party will see. EVERYTHING that network-info.txt
# publishes is tested - SEEDS, RELAY and FAUCET were published and NEVER tested (3 of the 5 values
# intended for third parties).
command -v curl >/dev/null || die "curl missing on the operator machine (required for the remote proof): sudo apt install -y curl"
_dist_fail=""
curl -fsS -m 10 -o /dev/null "http://$PUBHOST:8088/network-info.txt" 2>/dev/null || _dist_fail="$_dist_fail network-info(:8088)"
curl -fsS -m 10 -o /dev/null "http://$PUBHOST:8088/genesis.json"     2>/dev/null || _dist_fail="$_dist_fail genesis(:8088)"
curl -fsS -m 10 -o /dev/null "http://$PUBHOST:26657/status"          2>/dev/null || _dist_fail="$_dist_fail rpc(:26657)"
# Pure reachability (any HTTP response is enough, even a 404: the DAEMON is what is tested, not a route).
curl -sS  -m 10 -o /dev/null "http://$PUBHOST:8645/list"             2>/dev/null || _dist_fail="$_dist_fail relay(:8645)"
curl -sS  -m 10 -o /dev/null "http://$PUBHOST:4500/"                 2>/dev/null || _dist_fail="$_dist_fail faucet(:4500)"
# P2P :26656 is not HTTP: a plain TCP connection test (what a joining node will do).
timeout 8 bash -c "exec 3<>/dev/tcp/$PUBHOST/26656" 2>/dev/null      || _dist_fail="$_dist_fail p2p(:26656)"
if [ -n "$_dist_fail" ]; then
  echo "  FAILED: from OUTSIDE, '$PUBHOST' does not answer on:$_dist_fail"
  echo "  Step 8b was GREEN (tested from the VPS), so the blockage is BETWEEN the VPS and the internet:"
  echo "  a missing ufw rule, the PROVIDER firewall (cloud panel), or a CDN proxy (only 80/443)."
  echo "  A third party CANNOT join: announce nothing until this step is green."
  die "published kit unreachable from outside - the network runs but it is CLOSED"
fi
echo "  OK '$PUBHOST' reachable FROM OUTSIDE: 8088 (genesis+info), 26657 (RPC), 26656 (P2P), 8645 (relay), 4500 (faucet)"

# --- FINAL GATE: no mandatory step is allowed to have been skipped ---------------------------------
# 3b is ABSENT from this list: it is conditional (DENDRA_FRESH=1). All the others are structural - if
# one is missing, the exposed network is incomplete and it is NOT declared "UP".
assert_steps "0)" "1)" "2)" "3)" "3c)" "4)" "5)" "6)" "7)" "7b)" "7c)" "8)" "8b)" "9)" "9b)"

# --- END-TO-END PROOF: does the chat REALLY answer? ------------------------------------------------
# Step 7b funds the gateway, but "funded" is not "serving": a network can declare itself UP while every
# job returns "[Dendra] the network could not process the request". That is VERIFIED here, from the
# outside, before printing the success banner. NON blocking: with no miner connected (the normal case
# right after a launch) no job can complete - this is a diagnostic, not a gate.
echo
echo "  end-to-end check (is the gateway serving a job?)..."
_GWKEY="$(grep -E '^DENDRA_API_KEY=' "$ENVF" 2>/dev/null | cut -d= -f2-)"
# THROUGH CADDY, NOT THE RAW PORT. This probe asked `http://$PUBHOST:8651` — a port bound to
# 127.0.0.1 in docker-compose.yml, so from anywhere but the container it refuses the connection. The
# check could therefore only ever report "not serving", including on a network that was serving
# perfectly: a verification incapable of returning its positive answer, silenced by the `|| true`
# below. ASSUMPTION, NAMED: $PUBHOST is the name Caddy holds a certificate for — the same name the
# whole deployment already depends on for TLS. If it is an IP, this probe fails on the certificate
# and reports "not serving", which is a visible wrong answer rather than a permanent one.
_PROBE="$(curl -sS --max-time 90 "https://$PUBHOST/v1/chat/completions" \
  -H "Authorization: Bearer $_GWKEY" -H 'Content-Type: application/json' \
  -d '{"model":"dendra-network","messages":[{"role":"user","content":"ping"}]}' 2>/dev/null || true)"
_SERVING=0
case "$_PROBE" in
  *'"job_id"'*) _SERVING=1
                echo "  [OK] a job was opened and SETTLED - the gateway is serving (proof block present)." ;;
  *'could not process'*) _WHY="no job completes (no miner, or an empty 'gw' subsidy account)"
                         echo "  [!] the gateway answers but NO job completes. Two usual causes:"
                         echo "      (a) no miner connected -> normal at this stage, start the pool (B below);"
                         echo "      (b) empty 'gw' subsidy account -> re-run this script (7b is idempotent)." ;;
  '')                    _WHY="the gateway (:8651) does not answer"
                         echo "  [!] no answer from the gateway (:8651) - check 'docker compose logs gateway'." ;;
  *)                     _WHY="unexpected answer from the gateway"
                         echo "  [!] unexpected answer from the gateway - check 'docker compose logs gateway'." ;;
esac

echo
echo "=============================================================================="
# The probe above stays a DIAGNOSTIC, not a gate (with no miner connected no job can complete: that is
# the normal state right after a launch, and blocking here would be wrong). The BANNER, however, must
# not announce "UP" when the probe just said the opposite: that is exactly how a network once declared
# itself operational while serving an error on every job.
# Non-blocking AND honest: it continues, naming what is not serving yet.
if [ "$_SERVING" = 1 ]; then
  echo "  PUBLIC NETWORK UP AND SERVING (optimistic verification + veto, PoW faucet, The Proof :8090, points :8091)."
else
  echo "  PUBLIC NETWORK STARTED but NOT SERVING YET - $_WHY."
  echo "  (infrastructure in place; re-run the end-to-end check after starting the miner pool)"
fi
echo "  RPC http://$PUBHOST:26657 | gateway http://$PUBHOST:8651/v1 | faucet :4500 | chat :8080"
echo "  network-info : http://$PUBHOST:8088/network-info.txt (genesis + SHA256 + seeds)"
echo
echo "  NOT ANNOUNCED YET — remaining sequence (in order):"
echo "  A) 2nd VALIDATOR (another machine, keep total stake distribution <2/3) + anchor both VRF keys:"
echo "     deploy/join.sh --validator   (then create-validator + deploy/testnet/anchor_vrf_key.sh)"
echo "  B) HETEROGENEOUS JUDGES (>=2 distinct judge models):"
echo "     deploy/launch/judges5_public.sh   (RAM-based model pick; override MOE_MODEL / MOE_COUNT)"
echo "  C) VALIDATION RUN: HOST=$PUBHOST bash deploy/launch/cheat_public.sh"
echo "     -> triplet artifact (0/0/0) -> validation review -> publish + announce"
echo "=============================================================================="
