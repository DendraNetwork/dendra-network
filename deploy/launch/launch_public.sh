#!/usr/bin/env bash
# launch_public.sh: one-command public launch kit.
#
# Deploys the public incentivized network on the VPS: chain (launch genesis, optimistic verification + veto),
# hardened relay/faucet(PoW)/gateway (fail-closed), The Proof + points (compose profile `public`), and
# genesis/seeds publication. No chat and no monitoring stack on this host: it carries the only
# validator, and the images it runs are built on the operator machine, never on it. It then prints the
# follow-up sequence (heterogeneous judges, second validator, validation run); the wide announcement stays
# gated on the validation review.
#
# Prerequisite: ~/.dendra-launch.env generated and green:  tr -d '\r' < deploy/launch/launch_env_check.sh | bash
# Usage (WSL, repo root); key authentication is detected automatically:
#   tr -d '\r' < deploy/launch/launch_public.sh | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
#
# An environment variable goes on `bash`, never in front of `tr`, and the wrong form fails silently.
# This script is piped, so `VAR=1 tr -d '\r' < … | bash` sets VAR for `tr`, which does not read it,
# and bash sees nothing. No error, no warning: the run behaves as if the flag was never passed.
# The cost is silent: the genesis purge does not run, and a chain that looks fresh can come from a
# volume deleted by hand earlier.
#   Right:  tr -d '\r' < deploy/launch/launch_public.sh | DENDRA_FRESH=1 bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
#   Right:  export DENDRA_FRESH=1 ; tr -d '\r' < … | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
#   Wrong:  DENDRA_FRESH=1 tr -d '\r' < … | bash -s -- <VPS_IP> [PUBLIC_HOSTNAME]
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
# --- Key first, password only if asked for ---------------------------------------------------------
#
# Requiring a root password (a mandatory `SSHPASS`) would lock out an operator with key-only access
# and password login disabled, and the usual workaround (re-enabling password login for one command)
# leaves the door open afterwards. A launcher that only
# works on an unhardened machine teaches operators not to harden it.
#
# The key is tried first. `BatchMode=yes` makes the attempt fail fast instead of opening a prompt
# inside a piped script, where stdin is already taken.
if [ -n "${SSHPASS:-}" ]; then
  command -v sshpass >/dev/null || die "SSHPASS is set but sshpass is missing: sudo apt install -y sshpass"
  SSH_MODE="password"; RSH="sshpass -e ssh -n $SSHO root@$VPS"; RSYNC_PFX="sshpass -e"
  RSH_IN="sshpass -e ssh $SSHO root@$VPS"
elif ssh -o BatchMode=yes -o PasswordAuthentication=no $SSHO -n "root@$VPS" true 2>/dev/null; then
  SSH_MODE="key"; RSH="ssh -n -o BatchMode=yes $SSHO root@$VPS"; RSYNC_PFX=""
  RSH_IN="ssh -o BatchMode=yes $SSHO root@$VPS"
else
  echo "  FAILED: cannot reach root@$VPS."
  echo "    By key (recommended):  ssh-copy-id -i ~/.ssh/id_ed25519.pub root@$VPS"
  echo "      then check:          ssh -o PasswordAuthentication=no -o BatchMode=yes root@$VPS 'echo ok'"
  echo "    By password:           export SSHPASS='...' before running this script"
  exit 1
fi
# RSH carries `-n` on purpose: this script is piped into bash, so its own stdin is the rest of the
# script, and an ssh that reads stdin would swallow the steps that follow. RSH_IN is the one exception,
# for the single command that must read a stream (the image transfer, step 4), and it is only ever used
# at the end of a pipe, where stdin is that pipe and never the script.
echo "  ssh: authenticating by $SSH_MODE"
command -v rsync   >/dev/null || die "install rsync: sudo apt install -y rsync"

# --- Step tracer: detects a block that is silently skipped ---------------------------------------
# A block with an unconditional header can fail to execute without printing any error: a filesystem
# cache serving a stale version, stdin stolen by a command inside a piped script, a truncated read. The
# launch then declares itself up with a missing step.
# The whole class is detected instead: every step declares itself, and the end of the script refuses to
# conclude if a mandatory step left no mark. A silent skip becomes a visible failure, so a public launch
# cannot declare success while incomplete.
_STEPS_DONE=""
# Only the identifier is kept (the first word, e.g. "7b)"), not the label: otherwise the final assertion
# would compare whole sentences and fail on the slightest wording change.
step(){ _STEPS_DONE="$_STEPS_DONE|${1%% *}|"; echo "########## $* ##########"; }
_ran(){ case "$_STEPS_DONE" in *"|$1|"*) return 0;; *) return 1;; esac; }
assert_steps(){
  _missing=""
  for s in "$@"; do _ran "$s" || _missing="$_missing $s"; done
  [ -z "$_missing" ] && return 0
  echo "  FAILED: mandatory step(s) not executed:$_missing"
  echo "  A block of this script was silently skipped - the network is potentially incomplete"
  echo "  (for example an unfunded gateway means chat is broken). Announce nothing. Check that the file"
  echo "  being read is up to date (\`grep -c '^step ' $REPO/deploy/launch/launch_public.sh\`) then re-run."
  exit 1
}

step "0b) gate: the images about to be built (chain + services) must correspond to a commit"
# The publication gate's rule, applied to the chain. `build_public_repo.sh` removes its seal and refuses
# to publish when a published path is uncommitted, because a tree that matches no commit cannot be
# re-verified by the third party who clones it. The chain binary needs the same test: the launcher used
# to compute `git describe --dirty`, print the suffix, and deploy anyway, so the network could run a
# binary nobody, including its own operator, could rebuild (the live chain's `abci_info` answered `-dirty`).
# It also defeats the joiner's own check. `verify_consensus_epoch` compares declared epochs; two trees
# can agree on the epoch and still produce different binaries when one was built from uncommitted
# edits. An epoch match then reads as "same state machine" and is not one.
# Measured on the paths that enter the image, not on the whole tree. `git describe --dirty` turns dirty
# on any tracked edit anywhere, including internal notes, which is the normal state of this repository,
# and a gate that never stays quiet stops protecting anything. The chain image is built from
# `chain/` plus the two `docker/` files it copies.
# Three states, not two. `2>/dev/null` swallows a git failure and the substitution yields an empty
# string, which is also what a clean tree yields, so "git could not measure" read as "nothing is dirty".
# Without that, the same uncommitted edit to chain/app/app.go is refused in a git tree and reported
# as built from committed sources in a non-git tree: a tarball, a copy made by hand, or a checkout whose
# gitdir is unreachable all take the quiet path.
# `... || echo unknown`, used further down in this file, gives failure its own distinct value.
# The gate also covers the services image, which ranks and pays the Final Testnet Season (final-season,
# final-season-generator). It is built from `services/` plus `docker/Dockerfile.services` and
# `docker/entrypoint-services.sh` (the files its Dockerfile copies besides the chain image). Both images
# are measured: checking one while printing "committed sources" would vouch for the other unseen.
_IMG_DIRTY="$(git -C "$REPO" status --porcelain -- chain docker/Dockerfile.chain docker/entrypoint-chain.sh services docker/Dockerfile.services docker/entrypoint-services.sh 2>/dev/null)" || _IMG_DIRTY="?"
if [ "$_IMG_DIRTY" = "?" ]; then
  echo "  git cannot read $REPO, so whether the image sources are committed is unknown here."
  echo "  (no git, not a repository, or a gitdir this shell cannot reach)"
  if [ "${DENDRA_ALLOW_DIRTY_BUILD:-0}" = "1" ]; then
    echo "  WARNING: DENDRA_ALLOW_DIRTY_BUILD=1 -> deploying without knowing what the binary matches."
  else
    echo "  A launch is the one moment this must be known: an unverifiable binary is not weaker than"
    echo "  an unreproducible one -- nobody can rebuild either. Run this from the git checkout, or,"
    echo "  on purpose:  DENDRA_ALLOW_DIRTY_BUILD=1 ..."
    die "cannot verify that the chain and services image sources are committed -> refusing to deploy blind."
  fi
elif [ -n "$_IMG_DIRTY" ]; then
  echo "  the following paths enter the chain or services image and are not committed:"
  printf '%s
' "$_IMG_DIRTY" | head -10 | sed 's/^/    /'
  if [ "${DENDRA_ALLOW_DIRTY_BUILD:-0}" = "1" ]; then
    echo "  WARNING: DENDRA_ALLOW_DIRTY_BUILD=1 -> deploying a binary that matches no commit."
    echo "           Nobody will be able to rebuild it, and a joiner comparing epochs will see a match"
    echo "           while running different code. Acceptable for a throwaway test, never for a launch."
  else
    echo "  A binary built from these would correspond to no commit: it cannot be rebuilt by anyone,"
    echo "  including you, and the joiner's epoch check would report agreement between different code."
    echo "  Commit them (or stash them), then relaunch. To deploy anyway, on purpose:"
    echo "    DENDRA_ALLOW_DIRTY_BUILD=1 ..."
    die "image source paths are uncommitted -> refusing to deploy an unreproducible image."
  fi
fi
echo "  OK the chain and services images are built from committed sources"

step "0) gate: a green public .env is mandatory"
[ -f "$ENVF" ] || die "public .env missing - run launch_env_check.sh --init then the check"
# The chain id comes from the network file, and only from it. With a hard-coded name, a
# relaunch under another name would send the gateway's funding and the VRF anchoring to a chain that no
# longer exists, and both fail as signature errors rather than as a name mismatch.
# Read with grep, never sourced here: the file also holds secrets, and nothing else is needed from it.
NET_CHAIN_ID="$(grep -E '^DENDRA_CHAIN_ID=' "$ENVF" | tail -1 | cut -d= -f2- | tr -dc 'a-z0-9-')"
[ -n "$NET_CHAIN_ID" ] || die "DENDRA_CHAIN_ID is missing from $ENVF (the network file names the chain; nothing defaults it)"
tr -d '\r' < "$REPO/deploy/launch/launch_env_check.sh" | bash || die "launch_env_check red - fix before deploying"

step "1) SSH + host prerequisites (rsync, curl, python3, Docker)"
$RSH 'echo ssh OK; docker --version 2>/dev/null || echo NO_DOCKER' || die "SSH failed (IP / password / port 22?)"
# rsync runs on both ends: a freshly installed host often lacks it, and step 2 then dies with a message
# about the transfer rather than about the missing program.
# Two more programs the host needs are checked too. `curl` fetches the Docker installer: the old
# `curl … | sh` on a host without curl printed "command not found" into a pipe whose status is that of
# `sh`, which, fed nothing, exits 0, so the install was reported as done and installed nothing.
# `python3` serves network-info (the dendra-netinfo unit, step 8) and reads the chain id in
# publish_network.sh; without it the launch died at step 8 after the chain was already up. Each program
# is checked, installed when absent, and checked again: an exit status of 0 from the installer is a
# claim, the program answering is the measurement.
_host_tool(){ # <program> <apt package>: present, installed, or the launch stops here
  $RSH "command -v $1 >/dev/null 2>&1" && return 0
  echo "  installing $1 on the host..."
  $RSH "apt-get update -qq && apt-get install -y -qq $2" || die "$1 is missing on the host and could not be installed (apt-get install $2)"
  $RSH "command -v $1 >/dev/null 2>&1" || die "$1 is still missing on the host after apt-get install $2"
}
_host_tool rsync rsync
_host_tool curl curl
_host_tool python3 python3
# Downloaded to a file, then run: a pipe would hand `sh` an empty script on any download failure and
# report its exit 0. Then both `docker` and its compose plugin must answer: this launcher drives the host
# through `docker compose`, and an install that brings only one of the two is not done.
$RSH 'docker --version >/dev/null 2>&1' || {
  echo "  installing Docker..."
  $RSH 'curl -fsSL https://get.docker.com -o /root/get-docker.sh && sh /root/get-docker.sh' || die "docker install failed"
  $RSH 'docker --version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1' \
    || die "the Docker install reported success, but docker (or its compose plugin) does not answer on the host"
}
# A host that already had docker skipped the block above, so its compose plugin was never checked. It is
# checked here on every path, so a docker without compose stops at step 1 and not at the first `docker compose`.
$RSH 'docker compose version >/dev/null 2>&1' \
  || die "docker answers on the host but its compose plugin does not (install docker-compose-plugin)"

step "2) repository + .env -> VPS:~/dendra"
# Default deny: ship only what the host runs (chain source, services, container definitions, deploy kit).
# An exclude list fails open: any file added to the repository tomorrow would land on a public host
# reachable over SSH unless someone remembers to exclude it. The allow list below fails closed instead: a
# new file has to be named to be shipped, and a compromise of the host does not expose the project's
# internal working material.
# --delete-excluded also purges anything the host still carries from an earlier, wider sync.
#
# Three answers, not two. Three of the includes below name directories that exist only in the
# development tree: `chain/`, `prototype/` and `dendra/onchain-staging/`. Run from a published
# clone they match nothing, and rsync does not say so: the transfer succeeds having copied less than
# the reader believes, and the snapshot capability cannot be produced on the host.
# So the absence is reported here rather than discovered on the server, in the same shape
# `deploy/node_reachability.sh --self-test` uses for the same situation.
for _absent in chain prototype dendra/onchain-staging; do
  [ -d "$REPO/$_absent" ] && continue
  echo "  [not measured] $_absent is not in this tree, so it will not be copied. From a published"
  echo "                 clone that is expected -- those directories are development-only. What it"
  echo "                 costs: snapshot production is unavailable on the host, and the"
  echo "                 node still starts, joins and validates without it."
done
# The purge must spare the one file on the host that is not a copy. `~/dendra/.consensus_epoch` is
# written on the host after a successful `up` (step 4) and read by guard 3c and by publish_network.sh;
# it exists in no tree. `--exclude '*'` plus `--delete-excluded` would delete it at every run: every
# relaunch without DENDRA_FRESH=1 would die at 3c ("epoch unknown"), and the manual fix 3c prints would
# be erased again on the next try. A protect rule (`P`, anchored at the transfer root, placed before the excludes)
# keeps it out of both deletions; nothing else changes.
$RSYNC_PFX rsync -az --checksum --delete --delete-excluded -e "ssh $SSHO" \
  --filter 'P /.consensus_epoch' \
  --exclude '.git' --exclude '__pycache__/' --exclude '*.pyc' \
  --include 'chain/***' --include 'services/***' --include 'tokenomics/***' --include 'docker/***' --include 'deploy/***' \
  --include 'docker-compose.yml' \
  `# The snapshot body runs on this host: the export opens the application database, goleveldb opens` \
  `# it only once, so it needs the node stopped and the real home. Without this line the script is` \
  `# simply absent from the server and no snapshot can be produced at all.` \
  --include 'dendra/' --include 'dendra/onchain-staging/***' \
  --exclude '*' \
  "$REPO/" "root@$VPS:~/dendra/" || die "repository rsync failed"
$RSYNC_PFX rsync -az -e "ssh $SSHO" "$ENVF" "root@$VPS:~/dendra/.env" || die ".env rsync failed"
$RSH 'chmod 600 ~/dendra/.env' || true
echo "  repository + .env copied (bench-results/ artefacts stay local: development data)"

step "3) entrypoint integrity check (anti-truncation)"
$RSH 'cd ~/dendra && for s in docker/entrypoint-chain.sh docker/entrypoint-services.sh; do sh -n "$s" || { echo "  invalid or truncated: $s"; exit 3; }; done && echo "  entrypoints OK"' || die "invalid entrypoint on the VPS"

if [ "${DENDRA_FRESH:-0}" = "1" ]; then
  echo "########## 3b) genesis reset (DENDRA_FRESH=1) - purging volumes = start from zero ##########"
  # Public launch: start from a clean genesis (points at zero, no bench or test data in the history).
  # Without this, an existing genesis volume is reused and the public network would start on test data.
  # The keyring volumes go with the chain. The chain creates the faucet's and the gateway's keys at genesis
  # in `service-keys`; each service signs from its own copy, in `faucet-keys` and `gateway-keys`
  # (docker/entrypoint-services.sh::adopt_service_key), and the faucet keeps its drip ledger beside its key.
  # All of them belong to one genesis. Kept across a reset, the new chain would reuse keys funded by the old
  # one and refuse addresses that dripped on a network that no longer exists, and a service volume kept alone
  # holds a key the new `service-keys` does not carry, which adopt_service_key refuses at the seeded start.
  #
  # `down -v` takes every volume, and one of them must survive the chain. `final-season-data` holds the
  # programme's draw key (secret.bin), its evidence logs and the published daily rankings; the public
  # texts promise those rankings survive a reset, and `down -v` alone would erase them with the chain. So
  # before the purge the volume is copied off the host, to the operator's machine, through the same
  # remote shell; the purge does not run unless the copy is readable and holds something. The writers are
  # stopped first (a ranking half-written during the copy is a torn file), and restarted if the copy
  # fails, since the launch then stops here. Three answers on the volume, never two: present, absent, or
  # "docker did not answer", which is not "absent" and would let the purge delete it unseen.
  _S1VOL=dendra_final-season-data
  # Membership is read from one listing, with shell builtins only: a second call (`volume inspect`) that
  # fails on a daemon hiccup would read "absent" and let the purge run with no archive.
  _S1V="$($RSH "_l=\$(docker volume ls -q 2>/dev/null) || { echo unreadable; exit 0; }; for _v in \$_l; do [ \"\$_v\" = $_S1VOL ] && { echo present; exit 0; }; done; echo absent" 2>/dev/null || echo unreadable)"
  case "$_S1V" in
    absent) echo "  no $_S1VOL volume on the host: no Final Testnet Season data to keep" ;;
    present)
      _S1_DIR="${DENDRA_FINAL_SEASON_ARCHIVE_DIR:-$HOME/dendra-final-season-archives}"
      _S1_ARCH="$_S1_DIR/final-season-data-$VPS-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
      _s1_abort(){ rm -f "$_S1_ARCH"; $RSH 'cd ~/dendra && docker compose --profile public start final-season final-season-generator >/dev/null 2>&1; true'; die "3b: $* Nothing was purged."; }
      # umask 077: the archive carries secret.bin, the draw key. It is a secret on this machine too.
      ( umask 077 && mkdir -p "$_S1_DIR" ) || die "3b: cannot create $_S1_DIR to keep the Final Testnet Season data -- nothing was purged"
      $RSH 'cd ~/dendra && docker compose --profile public stop final-season final-season-generator >/dev/null 2>&1; true'
      ( umask 077 && $RSH "mp=\$(docker volume inspect -f '{{.Mountpoint}}' $_S1VOL) && [ -d \"\$mp\" ] && tar -C \"\$mp\" -czf - ." > "$_S1_ARCH" ) \
        || _s1_abort "the copy of $_S1VOL off the host failed."
      [ -s "$_S1_ARCH" ] || _s1_abort "the copy of $_S1VOL is empty ($_S1_ARCH)."
      _S1_LIST="$(tar -tzf "$_S1_ARCH" 2>/dev/null)" || _s1_abort "the copy of $_S1VOL is not a readable archive ($_S1_ARCH)."
      # `grep -c` prints 0 and also returns 1: the count is read from what it prints, never `|| echo 0`.
      _S1_N="$(printf '%s\n' "$_S1_LIST" | grep -vc -e '^\./$' -e '^$')"
      [ "${_S1_N:-0}" -gt 0 ] 2>/dev/null || _s1_abort "the copy of $_S1VOL holds no file. If the volume really is empty, remove it by hand (ssh root@$VPS 'docker volume rm $_S1VOL') and re-run."
      echo "  Final Testnet Season data kept before the purge: $_S1_ARCH ($_S1_N entries, $(wc -c < "$_S1_ARCH" | tr -d ' ') bytes, mode 600 -- it holds secret.bin)" ;;
    *) die "3b: could not read whether $_S1VOL exists on the host (got '$_S1V'). Not knowing is not 'absent', and the purge below would delete it -- nothing was purged." ;;
  esac
  # The gateway volume name is assembled because the mirror generator rewrites the module name it
  # contains into a shorter one in every published file, which would name a volume that does not exist.
  _gw=gateway
  _GW_VOL="dendra_${_gw}-keys"
  _CHAIN_VOLS="dendra_dendra-home dendra_service-keys dendra_faucet-keys $_GW_VOL"
  $RSH 'cd ~/dendra && docker compose --profile public --profile monitoring --profile chat down -v 2>/dev/null; docker volume rm '"$_CHAIN_VOLS"' 2>/dev/null; true'
  # The purge is measured, not just announced. Every error above is swallowed (`2>/dev/null`), the
  # remote command ends in `; true`, and no exit status is read, so printing "volumes purged" without a
  # reading would announce it even when a volume survived. Three answers, never two: a volume that
  # cannot be inspected is not an absent volume.
  _VOL="$($RSH 'for v in '"$_CHAIN_VOLS"'; do docker volume inspect $v >/dev/null 2>&1 && { echo present; exit 0; }; done; echo absent' 2>/dev/null || echo unreadable)"
  case "$_VOL" in
    absent) echo "  volumes purged (verified absent) -> the next boot builds a fresh genesis" ;;
    present)
      # Report why it survived, rather than prescribe the very command that has just failed, whose
      # error the `2>/dev/null` above threw away. A volume almost always survives for one
      # reason: a container still references it, and `docker volume rm` says so and names it.
      _WHY="$($RSH 'for v in '"$_CHAIN_VOLS"'; do docker volume rm $v 2>&1; docker ps -a --filter volume=$v --format "{{.Names}}"; done' 2>&1 || true)"
      die "3b: a chain volume (dendra_dendra-home, dendra_service-keys, dendra_faucet-keys or $_GW_VOL) survived the purge. Booting now would replay the old
     history with the new binary -- an AppHash panic, or worse, a silent fork.
     What the VPS answers:
$(printf '%s' "$_WHY" | sed 's/^/       /')
     A volume that will not go is held by a container. Remove the holders first, then the volume:
       ssh root@$VPS 'docker rm -f <the names above>'
       ssh root@$VPS 'docker volume rm $_CHAIN_VOLS'
     then re-run this launcher." ;;
    *) die "3b: could not read whether the chain volume still exists (got '$_VOL'). Not knowing is not
     the same as it being gone, and this is the step that decides whether the genesis is fresh." ;;
  esac
fi

step "3c) consensus-breaking guard"
# Why: a redeployment without a purge once replayed the history with a consensus-breaking binary (`wrong
# Block.Header.AppHash`, chain halted). Nothing in the kit prevented it, and `docker restart` even made
# the node look up to date. Compatibility cannot be guessed from a binary, so it is declared:
# `docker/CONSENSUS_EPOCH` (in the repository) carries the epoch of the code; the VPS remembers the epoch
# of the running chain (~/dendra/.consensus_epoch, written after every successful `up`). Different
# epochs + an existing volume + no DENDRA_FRESH=1 => refusal before the build.
LOCAL_EPOCH="$(head -1 "$REPO/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
[ -n "$LOCAL_EPOCH" ] || die "docker/CONSENSUS_EPOCH missing or unreadable - the consensus guard cannot work without it"
if ! $RSH 'docker volume inspect dendra_dendra-home >/dev/null 2>&1'; then
  echo "  no chain volume on the VPS -> nothing to fork, the guard passes (fresh genesis at boot)"
else
  # No `DENDRA_FRESH=1` branch sits here. This point is reached only when the volume still exists, which
  # with DENDRA_FRESH=1 means 3b did not remove it, and a branch answering "the purge already happened in
  # 3b, the guard passes" would skip the epoch comparison below. 3b proves the volume is gone and dies
  # otherwise: the volume that exists outranks what an operator asked for, and the comparison always
  # runs.
  REMOTE_EPOCH="$($RSH 'head -1 ~/dendra/.consensus_epoch 2>/dev/null | tr -dc 0-9' || true)"
  if [ -z "$REMOTE_EPOCH" ]; then
    echo "  FAILED: a chain exists on the VPS but its consensus epoch is unknown"
    echo "  (a chain from before this guard). There is no way to prove that the deployed code can replay"
    echo "  its history without forking. Two ways out:"
    echo "    - fresh genesis:   re-run with DENDRA_FRESH=1"
    echo "    - you know it is compatible: ssh root@$VPS 'echo $LOCAL_EPOCH > ~/dendra/.consensus_epoch' then re-run"
    echo "      (step 2 protects that file from its purge, so the decision survives the re-run)"
    die "unknown consensus epoch on an existing chain - the history is not replayed blindly"
  elif [ "$REMOTE_EPOCH" != "$LOCAL_EPOCH" ]; then
    echo "  FAILED: code = epoch $LOCAL_EPOCH, running chain = epoch $REMOTE_EPOCH."
    echo "  Deploying this code on this history is a consensus-breaking replay = AppHash panic."
    echo "  A fresh genesis is mandatory:"
    echo "    re-run with DENDRA_FRESH=1 (purges the volumes, Season-0 points reset to zero)"
    die "incompatible consensus epochs - refusing to reproduce the fork"
  else
    echo "  OK epoch $LOCAL_EPOCH == chain in place ($REMOTE_EPOCH): replay on a compatible history"
  fi
fi

step "4) build here, ship with save/load, compare the image configuration, start on the host without building"
# Why not on the host: compiling the chain at every deployment would mean minutes of Go toolchain on the
# one machine that carries the whole network, and a 6 GB build environment left on it. The images are
# built on this machine, shipped with `docker save | docker load`, and the
# host only runs them (`up --no-build`). The host is asked which image it holds and the answer is
# compared with the one built here: identical, or a refusal. Not knowing is not a match.
# The image ID is not what is compared. `.Id` is whatever digest the daemon's image store keys the image
# by: the config digest in the classic store, the manifest digest in the containerd store (Docker
# Desktop, recent engines), so a correct transfer between two stores produced two different IDs and a
# refusal. The fingerprint below is read from the image configuration, which `save | load` carries byte
# for byte whatever the store: the layer digests (the filesystem) and the runtime settings (entrypoint,
# command, environment, working directory, user). Layers alone would let a change made only to
# ENTRYPOINT or ENV, which adds no layer, pass as identical.
command -v docker >/dev/null || die "docker is required on this machine: the images are built here, not on the host"
# Build identity, computed here and nowhere else. Without it the ARG defaults in Dockerfile.chain stand
# and the running binary answers `dev` / `unknown`, which leaves the snapshot's third leg (the
# exact binary) without a source. `export` rather than a `VAR=x cmd` prefix: a prefix binds to the first
# command only, and the second build would run without it.
# `--match 'v[0-9]*'`: the name comes from a release tag only. Without it `--tags` takes the nearest tag
# of any kind, and a maintainer's working tags would name the binary the network runs (measured with the
# release tags withdrawn: `trace/<branch>-106-g<sha>`). With no release tag yet, `--always` gives the sha.
_BV="$(git -C "$REPO" describe --tags --match 'v[0-9]*' --always --dirty 2>/dev/null || echo dev)"
_BC="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  build identity: version=$_BV commit=$_BC"
# The chain image first: the services image copies `dendrad` out of it (Dockerfile.services).
( cd "$REPO" && export DENDRA_BUILD_VERSION="$_BV" DENDRA_BUILD_COMMIT="$_BC" \
    && docker compose build chain && docker compose build relay ) </dev/null || die "local image build failed"
_IMGS="dendra/chain:latest dendra/services:latest"
docker save $_IMGS | gzip -1 | $RSH_IN 'gunzip | docker load' || die "image transfer to the host failed"
_FP='{{json .RootFS.Layers}} {{json .Config.Entrypoint}} {{json .Config.Cmd}} {{json .Config.Env}} {{json .Config.WorkingDir}} {{json .Config.User}}'
for _i in $_IMGS; do
  _here="$(docker image inspect -f "$_FP" "$_i" 2>/dev/null || true)"
  _there="$($RSH "docker image inspect -f '$_FP' $_i" 2>/dev/null || true)"
  # A layer list without a single digest is not a fingerprint: empty, `null` and an error all land here.
  case "$_here"  in '['*sha256:*) ;; *) die "4: image $_i is not readable here after the build (got '${_here:-nothing}')" ;; esac
  case "$_there" in '['*sha256:*) ;; *) die "4: the host does not say which image $_i it holds (got '${_there:-nothing}'); not knowing is not a match" ;; esac
  [ "$_here" = "$_there" ] || die "4: image $_i differs between here and the host (layers or runtime settings):
     built : $_here
     host  : $_there"
  echo "  $_i  $(printf '%s' "${_here%%]*}" | grep -o 'sha256:' | wc -l | tr -d ' ') layers + runtime settings identical on the host"
done
# The seed default. The faucet and the gateway sign from their own keyring volumes (faucet-keys, gateway-keys);
# each takes its key from the chain's shared keyring only at a start where DENDRA_SERVICE_KEYS_SEED=service-keys
# (docker/entrypoint-services.sh::adopt_service_key). Without it a fresh genesis, whose chain has just created
# both keys there, would leave both services refusing to start. Set on every run, whatever the operator's file
# says: on a fresh genesis and on the first deployment of the separated keyrings each key is copied once; on any
# other run nothing is copied, and the start still checks that each volume's key is the shared keyring's key of
# that name, which refuses a volume left from another genesis. Step 7b clears it and recreates both services.
$RSH 'cd ~/dendra && sed -i "/^DENDRA_SERVICE_KEYS_SEED=/d" .env && { echo; echo DENDRA_SERVICE_KEYS_SEED=service-keys; } >> .env && [ "$(grep -cx DENDRA_SERVICE_KEYS_SEED=service-keys .env)" = 1 ]' || die "4: could not set DENDRA_SERVICE_KEYS_SEED=service-keys in the host's .env; without it the faucet and the gateway cannot take their keys"
echo "  DENDRA_SERVICE_KEYS_SEED=service-keys set on the host for this start (cleared at step 7b)"
$RSH "cd ~/dendra && docker compose --profile public up -d --no-build" || die "docker up failed on the host"
# The chain now running belongs to this epoch: record it for the next 3c guard.
$RSH "printf '%s\n' '$LOCAL_EPOCH' > ~/dendra/.consensus_epoch" || echo "  WARNING: epoch not recorded on the VPS (guard 3c will ask for an explicit decision at the next deployment)"

step "5) wait for RPC (checked from the host itself)"
# The wait is done on the host (RSH -> curl localhost).
# The network is already public at this point. Docker writes its own iptables rules for every port a
# container publishes, ahead of ufw, so the `up` of step 4 already exposes everything
# docker-compose.yml binds to 0.0.0.0: RPC 26657, P2P 26656, REST 1317, faucet 4500, relay 8645 and the
# HTTPS front 80/443 (gRPC only if DENDRA_GRPC_BIND is not loopback). A gate that fails from here on
# stops the launch on a network that is already reachable; it does not stop it from being exposed.
# Step 9 only governs what ufw filters, i.e. what does not go through Docker (network-info on :8088).
# The steps are not reordered because the gates below need the running chain.
ok=0
for i in $(seq 1 90); do
  sleep 6
  # `grep -q latest_block_height` only tested the presence of the key name: a chain that answers but is
  # frozen at 0 (consensus not started, single peer, replay panic) passed this gate as up.
  # A numeric height > 0 is required. `tr -d ' \t'`: the CometBFT HTTP RPC returns indented JSON, so a
  # compact pattern would not match and would return an empty value, with no error.
  H=$($RSH "curl -s http://localhost:26657/status 2>/dev/null | tr -d ' \t' | grep -o '\"latest_block_height\":\"[0-9]*\"' | grep -o '[0-9]*' | head -1")
  [ -n "$H" ] && [ "$H" -gt 0 ] 2>/dev/null && { ok=1; break; }
  [ $((i % 5)) -eq 0 ] && echo "  ... waiting for RPC ($i/90)"
done
[ "$ok" = 1 ] || { echo "  RPC silent. Diagnose: ssh root@$VPS 'cd ~/dendra && docker compose ps; docker compose logs --tail 100 chain'"; exit 1; }

step "6) on-chain check of the launch gates (verification mode + hold + audit quorum)"
$RSH 'cd ~/dendra && docker compose exec -T chain dendrad query jobs params -o json' 2>/dev/null \
  | python3 -c '
import json,sys
p=json.load(sys.stdin).get("params",{})
want={"verification_mode":"1","hold_bps":"10000","audit_min_quorum":"4"}
bad=[f"{k}={p.get(k)}(expected {v})" for k,v in want.items() if str(p.get(k))!=v]
print("  FAIL gates: "+", ".join(bad) if bad else "  OK verification_mode=1 + hold_bps=10000 + audit_min_quorum=4")
print("  audit_sample_bps=%s audit_resolve_timeout=%s silence_slash_bps=%s" % (p.get("audit_sample_bps"),p.get("audit_resolve_timeout"),p.get("silence_slash_bps")))
sys.exit(1 if bad else 0)' || die "on-chain gates not compliant — announce nothing; review the printed output"

step "7) assert supply <= 10,000,000 DNDR (zero-mint) — die before the kit is published if non-compliant"
# Zero-mint invariant: exactly one denom (udndr) and a supply that never exceeds the 10,000,000 DNDR genesis
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
sys.exit(0 if ok else 1)' || die "supply not compliant (expected <=10000000000000 udndr, single denom udndr, >0) — parasite mint/denom -> stop before the join kit is published (the Docker ports are already reachable since step 4: take the stack down)"

step "7b) bootstrap of the 'gw' subsidy account (otherwise the free tier is stillborn)"
# The gateway normally funds itself at the faucet, but the public faucet is PoW-gated: the gateway cannot
# get through on its own, gives up after 40 attempts, and no job can be opened any more (the client
# receives "[Dendra] the network could not process the request"). On a fresh genesis this happens every
# time (a purged volume means a zero balance), and it then has to be repaired by hand.
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
    # Zero rule: never test a JSON field with a textual predicate. `grep -q '"code":0'` depends on the
    # rendering (spacing, order, presence), and the proto3 codec omits zero values: on an output where
    # `code` was absent the grep failed, reporting "gw seeding refused" on a successful send. The field is
    # parsed and read with the zero value of its type.
    if $RSH "cd ~/dendra && docker compose exec -T chain dendrad tx bank send bob $GW_ADDR ${GW_TOPUP}udndr --keyring-backend test --home /root/.dendra-svc --chain-id $NET_CHAIN_ID --node tcp://localhost:26657 --gas-prices 0udndr --yes -o json" 2>/dev/null \
       | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)          # unreadable means unknown, which counts as a failure (never an assumed success)
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
# Recreation 2 of the service keyrings. The seed set at step 4 comes off the host's .env and the faucet and the
# gateway are recreated without the shared keyring, so neither can read the other's key any more. The result is
# read, with addresses and file names only, never a key: in each new container dendrad reads the service's key
# in its own keyring and finds the chain's key of that name (read in service-keys, the reference); that keyring
# holds no other key record; the container does not mount dendra_service-keys; its log carries no refusal and no
# seed-mount warning. A reading that does not come back counts as a failure.
_SKREF_BOB="$($RSH 'cd ~/dendra && docker compose exec -T chain dendrad keys show bob -a --keyring-backend test --home /root/.dendra-svc' 2>/dev/null | tr -dc 'a-z0-9')"
_SKREF_GW="$($RSH 'cd ~/dendra && docker compose exec -T chain dendrad keys show gw -a --keyring-backend test --home /root/.dendra-svc' 2>/dev/null | tr -dc 'a-z0-9')"
case "$_SKREF_BOB $_SKREF_GW" in
  "dendra1"*" dendra1"*) ;;
  *) die "7b: the chain's service keyring did not give the addresses of bob and gw (read '${_SKREF_BOB:-nothing}' and '${_SKREF_GW:-nothing}'), so the faucet's and the gateway's keyrings cannot be checked against them" ;;
esac
$RSH 'cd ~/dendra && sed -i "/^DENDRA_SERVICE_KEYS_SEED=/d" .env && [ "$(grep -c "^DENDRA_SERVICE_KEYS_SEED=" .env)" = 0 ] && docker compose up -d --no-deps --no-build faucet gateway' >/dev/null 2>&1 || die "7b: could not clear DENDRA_SERVICE_KEYS_SEED on the host and recreate the faucet and the gateway; until that is done each can read the other's key. By hand: ssh root@$VPS 'cd ~/dendra && sed -i /^DENDRA_SERVICE_KEYS_SEED=/d .env && docker compose up -d --no-deps --no-build faucet gateway'"
_sk_check(){ # <service> <key> <reference address>: prints nothing when the service holds that key alone, else why
  _a="$($RSH "cd ~/dendra && docker compose exec -T $1 dendrad keys show $2 -a --keyring-backend test --home /root/.dendra" 2>/dev/null | tr -dc 'a-z0-9')"
  [ "$_a" = "$3" ] || { echo "$1 reads its key '$2' as '${_a:-nothing}', the chain's '$2' is $3; "; return 0; }
  _r="$($RSH "cd ~/dendra && docker compose exec -T $1 sh -c 'cd /root/.dendra/keyring-test && echo *.info'" 2>/dev/null | tr -dc 'A-Za-z0-9._ -')"
  [ "$_r" = "$2.info" ] || { echo "$1 holds the key records '${_r:-nothing}', expected $2.info alone; "; return 0; }
  _m="$($RSH 'cd ~/dendra && c=$(docker compose ps -q '"$1"') && [ -n "$c" ] && docker inspect -f "{{range .Mounts}}{{.Name}},{{end}}" $c' 2>/dev/null | tr -dc 'A-Za-z0-9_,.-')"
  case "$_m" in
    *dendra_service-keys*) echo "$1 still mounts dendra_service-keys ($_m); "; return 0 ;;
    *"dendra_$1-keys,"*) ;;
    *) echo "$1 mounts '${_m:-nothing readable}', which does not name dendra_$1-keys; "; return 0 ;;
  esac
  _l="$($RSH "cd ~/dendra && docker compose logs $1 2>&1 | grep -cF -e 'refusing to start' -e 'shared keyring is mounted'" 2>/dev/null | tr -dc 0-9)"
  [ "$_l" = 0 ] || echo "$1 logged ${_l:-an unreadable number of} refusal or seed-mount line(s) since its recreation; "
}
_SK_WHY="not read"
for _ in $(seq 1 30); do
  sleep 4
  _SK_WHY="$(_sk_check faucet bob "$_SKREF_BOB")$(_sk_check gateway gw "$_SKREF_GW")"
  [ -z "$_SK_WHY" ] && break
done
[ -z "$_SK_WHY" ] || die "7b: the faucet's and the gateway's keyrings are not separated as expected: $_SK_WHY
     Both keys are still in service-keys, so nothing is lost. Read why: ssh root@$VPS 'cd ~/dendra && docker compose logs --tail 20 faucet gateway'
     A refusal names its cause (a volume left from another genesis is removed from that volume, never from service-keys).
     Then re-run this launcher: step 4 sets the seed again, so a key not yet taken is taken then."
echo "  OK keyrings separated: the faucet holds bob ($_SKREF_BOB) alone, the gateway gw ($_SKREF_GW) alone, neither mounts dendra_service-keys"

step "7c) operator VRF anchoring (verified, not best effort)"
# `entrypoint-chain.sh` attempts this anchoring as a background task at boot. A background task can
# generate the key and then anchor nothing, printing neither a success nor its own failure message. The
# network then runs with `contributors < min_required`, that is with anti-grinding inactive, while the
# chain logs a warning at every block that nobody reads. A security guard cannot rest on a background
# best effort, so the anchoring is replayed here, in the foreground, and confirmed by a committed
# transaction. Idempotent (the handler overwrites the key).
# Blocking rather than a warning, because with `audit_unwind_blocks` armed "the network runs,
# it is only less decentralized" is false: without a decentralised seed no audit is ever
# drawn, every due job is deferred, and past the bound every one is refunded to its client, so the miners
# of that window are paid nothing while the chain looks healthy. On a single-validator network the seed
# is this one key, so the launch stops here rather than open a network that would pay nobody.
$RSH "cd ~/dendra && tr -d '\r' < deploy/testnet/anchor_vrf_key.sh | docker compose exec -T -e HOME_DIR=/root/.dendra -e CHAIN_ID=$NET_CHAIN_ID -e NODE=tcp://127.0.0.1:26657 -e VAL_KEY=validator chain bash -s" \
  || die "operator VRF anchoring not confirmed. Without it no decentralised seed exists, no audit is drawn and, once audit_unwind_blocks has passed, every job is refunded and no miner is paid. Replay deploy/testnet/anchor_vrf_key.sh, then relaunch."
# Anchored does not mean producing. The anchor says the chain knows the key; the seed exists only once
# the validator signs its vote extensions with it. That is read from the chain, as JSON, with the zero
# rule: an absent `has_recent_seed` is false, an absent `latest_contributors` is 0, and a query that does
# not answer is unknown, which blocks like a no.
_seed_ok=""
for _i in $(seq 1 24); do
  _seed_ok="$($RSH "cd ~/dendra && docker compose exec -T chain dendrad query jobs committee-seed-health -o json --node tcp://127.0.0.1:26657" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unknown"); sys.exit()
has = bool(d.get("has_recent_seed", False))
contrib = int(d.get("latest_contributors", 0))
floor = int(d.get("committee_min_vrf_contributors", 0))
print("yes" if has and floor >= 1 and contrib >= floor else "no")
' 2>/dev/null)"
  [ "$_seed_ok" = "yes" ] && break
  sleep 5
done
[ "$_seed_ok" = "yes" ] || die "no decentralised seed after anchoring (last reading: ${_seed_ok:-no answer}). The validator is not signing its vote extensions with the anchored key: check DENDRA_VRF_KEY_FILE in the chain container, then relaunch."
echo "  [OK] decentralised seed produced (has_recent_seed, contributors >= floor)"

step "7d) Final Testnet Season programme: the generator's address, then the service"
# The chain creates the `generator` key at genesis, in the programme keyring; its address is what the
# programme service counts as work (the jobs that account opens). It is read from the chain container,
# never typed: a hand-copied address that is off by one character would rank every day at zero work,
# with nothing to say why. Written into the host's .env (replacing an earlier line), then the two
# programme services are (re)created so they take it.
_GEN="$($RSH "cd ~/dendra && docker compose exec -T chain dendrad keys show generator -a --keyring-backend test --home /root/.dendra-prog" 2>/dev/null | tr -dc 'a-z0-9')"
case "$_GEN" in
  dendra1*) echo "  generator account: $_GEN" ;;
  *) die "7d: the generator key is not readable in the programme keyring (got '${_GEN:-nothing}'). Without it no day can be ranked." ;;
esac
$RSH "cd ~/dendra && sed -i '/^DENDRA_FINAL_SEASON_GENERATOR=/d' .env && printf 'DENDRA_FINAL_SEASON_GENERATOR=%s\n' '$_GEN' >> .env && docker compose --profile public up -d --no-build final-season final-season-generator" \
  || die "7d: the programme services did not start"
# The service is asked rather than assumed to be right: it answers its own status from inside its
# container (it publishes no port). Three readings decide: the availability window length (0 means
# `avail_epoch_blocks` is not armed: the chain then refuses every availability proof, so presence can be
# neither proven nor read and no day can be ranked, and `presence.go::minerPresentAt` counts every miner
# as present, so a machine that is switched off is still drawn for the programme's requests), the
# season's first block (the network file's value), and the generator address the service was given.
# The third reading is measured: naming "$_GEN" in the [OK] line below, the value this script has just
# written into .env, would say nothing about whether the service received it. A container that was not
# recreated, or a compose file that stopped passing DENDRA_FINAL_SEASON_GENERATOR, ranks no day at all
# (the service skips ranking without the address). The status endpoint does not expose the address, so
# the reading is the container environment, taken by the same process that asks the status:
# `docker compose exec` runs with the environment the container was created with, which is the one the
# service read at start. An empty variable prints `-`, never the expected value by default.
_WANT_START="$(grep -E '^DENDRA_FINAL_SEASON_START_HEIGHT=' "$ENVF" | tail -1 | cut -d= -f2- | tr -dc 0-9)"
_S1=""
for _i in $(seq 1 24); do
  _S1="$(printf '%s\n' 'import json, os, urllib.request' \
      'd = json.load(urllib.request.urlopen("http://127.0.0.1:8093/final-season/v1/status", timeout=10))' \
      'print(int(d.get("window_blocks", 0)), int(d.get("start_height", 0)), os.environ.get("DENDRA_FINAL_SEASON_GENERATOR") or "-")' \
    | $RSH_IN "cd ~/dendra && docker compose exec -T final-season python3 -" 2>/dev/null | tail -1)"
  case "$_S1" in [1-9]*\ *\ *) break ;; esac
  sleep 5
done
_S1_EB=""; _S1_START=""; _S1_GENV=""
read -r _S1_EB _S1_START _S1_GENV _ <<< "$_S1"
[ -n "$_S1" ] && [ "${_S1_EB:-0}" -gt 0 ] 2>/dev/null \
  || die "7d: the programme service does not report its windows (got '${_S1:-no answer}'). A window length of 0 means avail_epoch_blocks is not armed: presence can be neither proven nor read, no day can be ranked, and the chain counts every miner as present."
[ "$_S1_START" = "$_WANT_START" ] \
  || die "7d: the service runs the season from block $_S1_START, the network file says $_WANT_START."
[ "$_S1_GENV" = "$_GEN" ] \
  || die "7d: the final-season container was not given the generator: its environment carries DENDRA_FINAL_SEASON_GENERATOR='${_S1_GENV:-?}', the chain's generator is $_GEN. Without it no day is ranked. Recreate it: ssh root@$VPS 'cd ~/dendra && docker compose --profile public up -d --no-build --force-recreate final-season final-season-generator'"
echo "  [OK] Final Testnet Season service: availability windows of $_S1_EB blocks, season from block $_S1_START, generator $_GEN"
echo "       (generator measured in the final-season container environment; the status endpoint does not expose it)"

step "8) publish genesis/seeds"
# The miner image is no longer published from here. network-info.txt travels over plain HTTP, and the
# image it named is code that a joiner pulls and runs next to the volume holding its miner keys: whoever
# could rewrite the file in transit chose that code (a digest pins bytes, not their author). The pin
# lives in `docker/MINER_IMAGE` of the tree a joiner clones over HTTPS git, like docker/GENESIS_SHA256,
# and deploy/join.sh reads it there. A value left in the network file is reported, not silently dropped.
grep -qE '^DENDRA_MINER_IMAGE=.' "$ENVF" 2>/dev/null \
  && echo "  [i] DENDRA_MINER_IMAGE in $ENVF is no longer read: the miner image is pinned by docker/MINER_IMAGE in the published tree"
$RSH "cd ~/dendra && tr -d '\r' < deploy/testnet/publish_network.sh | bash -s -- $PUBHOST" || die "publish_network failed"
# :8088 is served by systemd (Restart=always, survives reboot and ssh detach); a plain nohup dies with the session.
$RSH 'cat >/etc/systemd/system/dendra-netinfo.service <<EOF
[Unit]
Description=Dendra network-info http (:8088)
After=network.target
[Service]
WorkingDirectory=/root/dendra/deploy/testnet/published
ExecStart=/usr/bin/python3 -m http.server 8088
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable dendra-netinfo 2>/dev/null; systemctl restart dendra-netinfo; sleep 1; curl -sf -m5 http://localhost:8088/network-info.txt | grep -q CHAIN_ID' || die "network-info is not served on :8088 (systemd dendra-netinfo)"
echo "  network-info served on :8088 (systemd dendra-netinfo)"

step "8b) guard - the published host must actually answer on the published ports"
# Why this guard exists: a host name that leads to a CDN in front of the website rather than to the VPS
# publishes a kit nobody can join (genesis timeouts, no P2P), and nothing else in this script notices it.
#
# The guard tests what the kit promises, from the VPS, on the exact name that was published.
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
  echo "  FAILED: the published host '$PUBHOST' does not answer on:$_pub_fail"
  echo "  The published join kit is unusable: a third party can neither fetch the genesis,"
  echo "  nor join the P2P network, nor query the RPC. The network runs, but it is closed."
  echo "  Most frequent cause: '$PUBHOST' is proxied by a CDN (only 80/443 pass through)."
  echo "  Remedy: re-run with a DNS-only subdomain pointing at the VPS, for example:"
  echo "      ... | bash -s -- $VPS api.<your-domain>"
  die "published host unreachable - nothing is announced until the kit works"
fi
echo "  OK published host '$PUBHOST' reachable on :8088 (genesis + network-info) and :26657 (RPC)"

step "8c) guard - the three genesis fingerprints agree, and the published kit names that genesis"
# Why this guard exists: join.sh compares the downloaded genesis with GENESIS_SHA256, and both travel in
# the same network-info.txt from the same host, so that check cannot tell a forged host from a real one.
# `docker/GENESIS_SHA256` in the tree a joiner clones is the second source join.sh cross-checks against
# (keyed on CHAIN_ID). It does not update itself: a relaunch with a fresh genesis leaves the pin naming
# the old genesis, and from that moment every joiner is refused a legitimate network. So this step
# takes the three fingerprints otherwise compared by hand (container, served file, declared line),
# refuses if they disagree (a publication defect), and rewrites line 1 of the pin when the local tree
# differs. It does not die there, because the pin is published after the launch, in the same session:
# commit, regenerate the mirror, push.
_fp_cont="$($RSH 'cd ~/dendra && docker compose exec -T chain sha256sum /root/.dendra/config/genesis.json' 2>/dev/null | awk '{print $1}' | tr -dc 'a-f0-9')"
_fp_serv="$($RSH "curl -fsS -m 20 http://$PUBHOST:8088/genesis.json | sha256sum" 2>/dev/null | awk '{print $1}' | tr -dc 'a-f0-9')"
_fp_info="$($RSH "curl -fsS -m 8 http://$PUBHOST:8088/network-info.txt" 2>/dev/null | tr -d '\r' | sed -n 's/^GENESIS_SHA256=//p' | head -1 | tr -dc 'a-f0-9')"
_fp_chain="$($RSH "curl -fsS -m 8 http://$PUBHOST:8088/network-info.txt" 2>/dev/null | tr -d '\r' | sed -n 's/^CHAIN_ID=//p' | head -1 | tr -dc 'A-Za-z0-9_.-')"
if [ -z "$_fp_cont" ] || [ -z "$_fp_serv" ] || [ -z "$_fp_info" ] || [ -z "$_fp_chain" ]; then
  die "genesis fingerprints not measured (container='$_fp_cont' served='$_fp_serv' declared='$_fp_info' chain='$_fp_chain') - an unreadable fingerprint is not an agreeing one"
fi
if [ "$_fp_cont" != "$_fp_serv" ] || [ "$_fp_serv" != "$_fp_info" ]; then
  echo "  FAILED: the three genesis fingerprints disagree:"
  echo "    container : $_fp_cont"
  echo "    served    : $_fp_serv"
  echo "    declared  : $_fp_info"
  die "network-info.txt and :8088 do not describe the genesis the chain runs - do not announce this kit"
fi
echo "  OK container = served = declared : $_fp_cont (chain $_fp_chain)"
_pin_f="$REPO/docker/GENESIS_SHA256"
_pin_line="$(head -1 "$_pin_f" 2>/dev/null | tr -dc 'A-Za-z0-9=_.-')"
if [ "$_pin_line" = "$_fp_chain=$_fp_cont" ]; then
  echo "  OK the local tree pins this genesis (docker/GENESIS_SHA256) - once published, joiners cross-check it"
else
  # The launcher writes line 1 itself (it holds the value and the tree) and keeps the commentary below
  # it: printing the line to copy left a window in which every joiner was refused a legitimate network
  # for as long as a human took to type it. What remains manual is commit, regenerate the mirror, push.
  _pin_tmp="$(mktemp)"
  { printf '%s\n' "$_fp_chain=$_fp_cont"; [ -f "$_pin_f" ] && tail -n +2 "$_pin_f"; } > "$_pin_tmp" && mv "$_pin_tmp" "$_pin_f" \
    || { rm -f "$_pin_tmp"; die "could not write $_pin_f - the published kit would keep naming the old genesis"; }
  echo "  [!] docker/GENESIS_SHA256 in the local tree named '${_pin_line:-<nothing>}' - line 1 rewritten to:"
  echo "          $_fp_chain=$_fp_cont"
  echo "      Until this is committed, the mirror regenerated and pushed, every joiner that cross-checks"
  echo "      the pin is refused a legitimate network. Do it in the same gesture as this launch."
fi

step "9) firewall rules (ufw): the ports Docker publishes have been public since step 4"
# This step is not the moment of exposure. Docker publishes every port a container binds to 0.0.0.0
# through its own iptables rules, which ufw does not filter: RPC, P2P, REST, faucet, relay and HTTPS are
# reachable from the `up` of step 4 (see step 5). These rules decide what does not go through Docker
# (the network-info server on :8088), and they describe the public surface for whoever reads the
# firewall. A gate that died above stopped the launch and the publication of the kit; it did not keep
# the Docker ports closed.
# 80/443 are required: Caddy needs 80 for the ACME challenge and 443 for the whole HTTPS front
# (testnet-api./testnet-proof.), and the nodes POST their capacity report over it. Omitting them left HTTPS shut
# on any host where ufw is actually enabled, right after printing "public ports opened".
# 8092 stays closed on purpose: the capacity registry is reached through Caddy (/capacity), not directly.
# Only ports that listen publicly are opened here. 8651 (gateway), 8080, 8090 and 8091 are bound to
# 127.0.0.1 in docker-compose.yml and are reached through Caddy, so opening them in ufw granted nothing
# and made the firewall describe a surface that does not exist. Measured from outside after the change:
# 8651/8080/8090/8091 refuse the connection, 8088 answers. 8088 (genesis + network-info) and
# 26656/26657/4500/8645 stay: those do listen publicly and are published in network-info.txt.
$RSH 'command -v ufw >/dev/null && { ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 26657/tcp; ufw allow 26656/tcp; ufw allow 8645/tcp; ufw allow 4500/tcp; ufw allow 8088/tcp; }; true' || true
echo "  public ports opened, HTTPS included"

step "9b) remote proof: the published kit answers from outside the VPS"
# Step 8b tests from the VPS (curl over SSH) and before the firewall is opened: when PUBHOST points at
# the VPS itself, the traffic comes back through the local interface and passes even if a firewall (ufw,
# or the provider edge) blocks everything from outside. 8b therefore proves a local property, not a
# remote one. Here, after the firewall, the test runs from the operator machine (this script), that is
# from the real internet, which is what a third party will see. Every endpoint that network-info.txt
# publishes is tested, the seeds, the relay and the faucet included.
# The HTTPS front is tested too. publish_network.sh publishes DENDRA_FINAL_SEASON_URL
# (https://<name>/final-season/v1, through Caddy): the payout declaration (`final_season_miner.py payout`)
# and the desktop application's Final Testnet Season view reach the programme there, over TLS, with the
# certificate Caddy obtains by ACME after step 4. A front with no certificate (DNS not pointed, port 80
# shut, ACME rate limit) fails this step; passing it would leave every declaration and season view
# failing TLS silently. Ranking and payment do not read it: they rest on chain facts.
# The URL is read from the served network-info.txt (the one a joiner gets), and `<url>/status` is
# polled over HTTPS with certificate verification for about two minutes (the ACME order can still be in
# flight). Three answers, never two: published and answering, published and not answering (a
# distribution failure), or not published (reported, and skipped). A network-info that could not be
# read leaves the URL unknown, which is not "not published": it is already counted as failed.
command -v curl >/dev/null || die "curl missing on the operator machine (required for the remote proof): sudo apt install -y curl"
_dist_fail=""
_NI_OK=1
_NI="$(curl -fsS -m 10 "http://$PUBHOST:8088/network-info.txt" 2>/dev/null)" || { _NI_OK=0; _dist_fail="$_dist_fail network-info(:8088)"; }
curl -fsS -m 10 -o /dev/null "http://$PUBHOST:8088/genesis.json"     2>/dev/null || _dist_fail="$_dist_fail genesis(:8088)"
curl -fsS -m 10 -o /dev/null "http://$PUBHOST:26657/status"          2>/dev/null || _dist_fail="$_dist_fail rpc(:26657)"
# Pure reachability (any HTTP response is enough, even a 404: the daemon is what is tested, not a route).
curl -sS  -m 10 -o /dev/null "http://$PUBHOST:8645/list"             2>/dev/null || _dist_fail="$_dist_fail relay(:8645)"
curl -sS  -m 10 -o /dev/null "http://$PUBHOST:4500/"                 2>/dev/null || _dist_fail="$_dist_fail faucet(:4500)"
# P2P :26656 is not HTTP: a plain TCP connection test (what a joining node will do).
timeout 8 bash -c "exec 3<>/dev/tcp/$PUBHOST/26656" 2>/dev/null      || _dist_fail="$_dist_fail p2p(:26656)"
_S1_SEEN="not measured (network-info unreadable)"
if [ "$_NI_OK" = 1 ]; then
  _S1_URL="$(printf '%s\n' "$_NI" | tr -d '\r' | sed -n 's/^DENDRA_FINAL_SEASON_URL=//p' | tail -1)"
  case "$_S1_URL" in
    '') _S1_SEEN="not published"
        echo "  [i] network-info.txt publishes no DENDRA_FINAL_SEASON_URL: the HTTPS front is not tested. Miners joining"
        echo "      from this file are still ranked and paid on chain facts, to their operator address, but cannot"
        echo "      declare a payout address or open the application's Final Testnet Season view (publish with the"
        echo "      certified name)." ;;
    https://*)
        _s1_ok=0
        for _k in $(seq 1 24); do
          curl -fsS -m 5 -o /dev/null "$_S1_URL/status" 2>/dev/null && { _s1_ok=1; break; }
          [ $((_k % 6)) -eq 0 ] && echo "  ... HTTPS front not answering yet ($_S1_URL/status, try $_k/24)"
          sleep 5
        done
        if [ "$_s1_ok" = 1 ]; then _S1_SEEN="$_S1_URL"
        else _dist_fail="$_dist_fail https-final-season($_S1_URL/status)"; fi ;;
    *)  _dist_fail="$_dist_fail final-season-url-not-https($_S1_URL)" ;;
  esac
fi
if [ -n "$_dist_fail" ]; then
  echo "  FAILED: from outside, '$PUBHOST' does not answer on:$_dist_fail"
  echo "  Step 8b was green (tested from the VPS), so the blockage is between the VPS and the internet:"
  echo "  a missing ufw rule, the provider firewall (cloud panel), or a CDN proxy (only 80/443)."
  echo "  For the HTTPS front: the name must point at the VPS (DNS only), port 80 must reach Caddy for the"
  echo "  ACME challenge, and 'docker compose logs caddy' says why no certificate was issued."
  echo "  A third party cannot join: announce nothing until this step is green."
  die "published kit unreachable from outside - the network runs but it is closed"
fi
echo "  OK '$PUBHOST' reachable from outside: 8088 (genesis+info), 26657 (RPC), 26656 (P2P), 8645 (relay), 4500 (faucet)"
echo "     HTTPS front (Final Testnet Season): $_S1_SEEN"

# --- Final gate: no mandatory step may have been skipped -------------------------------------------
# 3b is absent from this list because it is conditional (DENDRA_FRESH=1). All the others are structural:
# if one is missing, the exposed network is incomplete and it is not declared up.
assert_steps "0)" "1)" "2)" "3)" "3c)" "4)" "5)" "6)" "7)" "7b)" "7c)" "7d)" "8)" "8b)" "9)" "9b)"

# --- End-to-end check: does the chat actually answer? ----------------------------------------------
# Step 7b funds the gateway, but funded is not serving: a network can declare itself up while every
# job returns "[Dendra] the network could not process the request". That is checked here, from the
# outside, before printing the success banner. Not blocking: with no miner connected (the normal case
# right after a launch) no job can complete, so this is a diagnostic, not a gate.
echo
echo "  end-to-end check (is the gateway serving a job?)..."
_GWKEY="$(grep -E '^DENDRA_API_KEY=' "$ENVF" 2>/dev/null | cut -d= -f2-)"
# Through Caddy, not the raw port: `http://$PUBHOST:8651` is bound to 127.0.0.1 in docker-compose.yml,
# so from anywhere but the container it refuses the connection, and a probe there could only ever report
# "not serving", with the failure hidden by the `|| true` below.
# Assumption: $PUBHOST is the name Caddy holds a certificate for, the same name the whole deployment
# already depends on for TLS. If it is an IP, this probe fails on the certificate and says so (see below).
# The key reaches curl by stdin, never by argv. `-H "Authorization: Bearer <key>"` put the gateway's
# API key on curl's command line for up to 90 s, where `ps` and /proc/<pid>/cmdline show it to every
# local user. `-H @-` reads the header from stdin; `printf` is a shell builtin, so the key is in no
# process's argument list at any point.
_PROBE="$(printf 'Authorization: Bearer %s\n' "$_GWKEY" | curl -sS --max-time 90 "https://$PUBHOST/v1/chat/completions" \
  -H @- -H 'Content-Type: application/json' \
  -d '{"model":"dendra-network","messages":[{"role":"user","content":"ping"}]}' 2>/dev/null || true)"
_SERVING=0
case "$_PROBE" in
  *'"job_id"'*) _SERVING=1
                echo "  [OK] a job was opened and settled - the gateway is serving (proof block present)." ;;
  *'could not process'*) _WHY="no job completes (no miner, or an empty 'gw' subsidy account)"
                         echo "  [!] the gateway answers but no job completes. Two usual causes:"
                         echo "      (a) no miner connected -> normal at this stage, start the pool (B below);"
                         echo "      (b) empty 'gw' subsidy account -> re-run this script (7b is idempotent)." ;;
  '')                    # Report the address that was actually probed.
                         # Neither `:8651` nor the gateway logs are the place to look: :8651 is
                         # loopback-only by design (docker-compose.yml) and is never probed, since
                         # the request goes to https://$PUBHOST/v1/chat/completions.
                         # When $PUBHOST is an IP the certificate cannot match it and curl fails
                         # before reaching any container, so the gateway logs show nothing. The
                         # two causes are told apart here.
                         case "$PUBHOST" in
                           *[!0-9.]*) _WHY="no answer from https://$PUBHOST/v1 (the gateway is reached through Caddy)"
                                      echo "  [!] no answer from https://$PUBHOST/v1 - check 'docker compose logs gateway' and 'logs caddy'."
                                      echo "      (:8651 is loopback-only by design and is not the address to test.)" ;;
                           *)         _WHY="probe inconclusive: \$PUBHOST is an IP, so TLS cannot match the certificate"
                                      echo "  [!] probe inconclusive, not a failure: '$PUBHOST' is an IP address, and the"
                                      echo "      certificate holds a name, so https://$PUBHOST/ cannot verify. The gateway"
                                      echo "      may well be serving. Re-run with the certified hostname as the 2nd"
                                      echo "      argument, or test by hand:  curl -sS -o /dev/null -w '%{http_code}\\n' \\"
                                      echo "        https://<your-hostname>/v1/models     # 401 = it answers and wants a key" ;;
                         esac ;;
  *)                     _WHY="unexpected answer from the gateway"
                         echo "  [!] unexpected answer from the gateway - check 'docker compose logs gateway'." ;;
esac

echo
echo "=============================================================================="
# The probe above stays a diagnostic, not a gate (with no miner connected no job can complete, which is
# the normal state right after a launch). The banner, however, must not announce "up" when the probe
# just said the opposite: that is how a network once declared itself operational while serving an
# error on every job. So it continues, naming what is not serving yet.
if [ "$_SERVING" = 1 ]; then
  echo "  Public network up and serving (optimistic verification + veto, PoW faucet, The Proof :8090, points :8091)."
else
  echo "  Public network started but not serving yet - $_WHY."
  echo "  (infrastructure in place; re-run the end-to-end check after starting the miner pool)"
fi
# The gateway is not published on :8651: that port is bound to 127.0.0.1 in docker-compose.yml and
# refuses every outside connection by design. The entry point is Caddy, on the certified name, over
# HTTPS.
echo "  RPC http://$PUBHOST:26657 | gateway https://<certified-hostname>/v1 (Caddy) | faucet :4500"
echo "  network-info : http://$PUBHOST:8088/network-info.txt (genesis + SHA256 + seeds)"
echo
# One validator, by decision. The sequence below does not open with a second validator: an
# operator following that advice would reintroduce the two-machine halt the relaunch exists to
# remove.
echo "  Not announced yet — remaining sequence (in order):"
echo "  A) commit docker/GENESIS_SHA256 (rewritten in step 8c): joiners cross-check the genesis against it"
echo "  B) publish the mirror, then the site; post the announcement"
echo "  C) the jury concludes with audit_min_quorum voters (read it on chain, never from a document);"
echo "     audit_sample_bps is raised by governance once that many jurors vote"
echo "=============================================================================="
