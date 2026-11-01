#!/usr/bin/env bash
# ship_chain_image.sh -- builds the chain image from a tree on this machine, ships it to a host with
# `docker save | gzip | ssh docker load`, and compares the image the host then holds with the one built here.
# It starts nothing: no compose command runs, here or on the host.
#
# Why: before a governed upgrade the new chain image must already sit on the validator host
# (docs/RUNBOOK-MONTEE-EPOQUE-15.md, section 4), while the host keeps running the current image until the halt.
# deploy/launch/launch_public.sh step 4 builds, ships and compares the same way, then runs `up`, and its guard
# 3c refuses an epoch change without DENDRA_FRESH=1, which destroys the chain.
#
# Usage: bash deploy/launch/ship_chain_image.sh <tree> <host> <image:tag> [--repo <dir> --rev <rev>] [--as-latest]
#   <tree>       the build context (docker/Dockerfile.chain inside it): a git checkout whose top level is <tree>, with
#                chain, docker/Dockerfile.chain and docker/entrypoint-chain.sh committed (exit 2 otherwise), or an
#                export (git archive) named by --repo and --rev, which must match that revision on chain and docker.
#                From WSL on /mnt/c git reports clean files as modified (it ignores core.autocrlf): use the export there.
#   <host>       reached as root over ssh, the key first and SSHPASS only when it is set, as launch_public.sh does.
#   <image:tag>  the tag built here and loaded on the host. The host's compose service `chain` runs dendra/chain:latest,
#                so that tag is refused unless --as-latest is given, and the script then says what it means.
# Build identity, as launch_public.sh step 4 computes it: DENDRA_BUILD_VERSION from
#   `git describe --tags --match 'v[0-9]*' --always` and DENDRA_BUILD_COMMIT the short sha, passed to the build as
#   VERSION and COMMIT (the arguments docker-compose.yml maps them to).
# Exit: 0 shipped and identical on both sides; 1 usage or a refused argument; 2 build identity not established or
#   local build failed; 3 host unreachable or transfer failed; 4 the host does not say which image it holds;
#   5 the image differs between here and the host.
set -uo pipefail
usage(){ echo "usage: bash deploy/launch/ship_chain_image.sh <tree> <host> <image:tag> [--repo <dir> --rev <rev>] [--as-latest]"; exit 1; }
refuse(){ echo "  refused: $2"; exit "$1"; }
TREE=""; HOST=""; TAG=""; REPO=""; REV=""; AS_LATEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --as-latest) AS_LATEST=1 ;;
    --repo) [ $# -ge 2 ] || usage; REPO="$2"; shift ;;
    --rev) [ $# -ge 2 ] || usage; REV="$2"; shift ;;
    -*) echo "unknown option: $1"; usage ;;
    *) if [ -z "$TREE" ]; then TREE="$1"; elif [ -z "$HOST" ]; then HOST="$1"; elif [ -z "$TAG" ]; then TAG="$1"; else usage; fi ;;
  esac
  shift
done
[ -n "$TREE" ] && [ -n "$HOST" ] && [ -n "$TAG" ] || usage

echo "== 1) the tag =="
case "$TAG" in *[[:space:]]*|*@*) refuse 1 "the image tag must be name:tag, got: $TAG" ;; esac
LAST="${TAG##*/}"
case "$LAST" in *:?*) ;; *) refuse 1 "name the tag explicitly (name:tag): without one docker means latest, got: $TAG" ;; esac
NAME="${TAG%:*}"; TAGPART="${TAG##*:}"
IS_LATEST=0
if [ "$TAGPART" = latest ] && { [ "$NAME" = dendra/chain ] || [ "$NAME" = docker.io/dendra/chain ]; }; then IS_LATEST=1; fi
if [ "$IS_LATEST" = 1 ] && [ "$AS_LATEST" = 0 ]; then
  refuse 1 "$TAG is the image the host's compose service chain runs (docker-compose.yml): give another tag, or --as-latest if that is the intent"
fi
if [ "$AS_LATEST" = 1 ] && [ "$IS_LATEST" = 0 ]; then refuse 1 "--as-latest goes with dendra/chain:latest only, got: $TAG"; fi
if [ "$AS_LATEST" = 1 ]; then
  echo "   --as-latest: the host's dendra/chain:latest becomes this image once it is loaded. Nothing restarts the service"
  echo "   now; its next start (restart: always after a crash, a reboot, an up) runs this image. While an upgrade plan"
  echo "   is pending, a binary that carries the plan's handler stops at once on the upgrade module's guard (binary updated"
  echo "   before trigger); the epoch 14 image is no longer under that tag on the host to fall back to."
fi
echo "   $TAG"

echo "== 2) the build identity of $TREE =="
[ -f "$TREE/docker/Dockerfile.chain" ] && [ -d "$TREE/chain" ] || refuse 1 "$TREE has no docker/Dockerfile.chain or no chain"
TREE="$(cd "$TREE" && pwd -P)" || refuse 1 "$TREE is not a readable directory"
# `git -C` answers for the nearest repository above: a tree is a checkout only when it is that repository's top level.
TOP="$(git -C "$TREE" rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$TOP" ] && TOP="$(cd "$TOP" && pwd -P)"
if [ -n "$TOP" ] && [ "$TOP" = "$TREE" ]; then
  [ -z "$REPO$REV" ] || refuse 1 "$TREE is a git checkout: --repo and --rev are for an export"
  # The paths launch_public.sh gate 0b reads for the chain image. An uncommitted or untracked file there would ship
  # a binary nobody can rebuild from the commit it names; a git failure leaves that unknown, which is refused too.
  ST="$(git -C "$TREE" status --porcelain --untracked-files=all -- chain docker/Dockerfile.chain docker/entrypoint-chain.sh 2>/dev/null)"
  STRC=$?
  [ "$STRC" = 0 ] || refuse 2 "git status failed on $TREE (exit $STRC): whether chain and the image files are committed is unknown; export the revision and give --repo and --rev"
  [ -z "$ST" ] || refuse 2 "$TREE has uncommitted or untracked files in chain or the image files ($(printf '%s' "$ST" | head -3 | tr '[:space:]' ' ')); commit them, or export the revision and give --repo and --rev"
  # Files git ignores under chain still enter the Dockerfile's copy of chain/ unless .dockerignore drops them: counted, not judged.
  if IGN="$(git -C "$TREE" status --porcelain --ignored -- chain 2>/dev/null)"; then
    NIGN="$(printf '%s' "$IGN" | grep -c '^!!')"
  else
    NIGN="?"
  fi
  BV="$(git -C "$TREE" describe --tags --match 'v[0-9]*' --always 2>/dev/null)"
  BC="$(git -C "$TREE" rev-parse --short HEAD 2>/dev/null)"
  echo "   checkout at $TREE, chain and the image files committed"
  echo "   not read: $NIGN path(s) git ignores under chain, which the build context holds unless .dockerignore drops them"
else
  [ -n "$REPO" ] && [ -n "$REV" ] || refuse 1 "$TREE is not the top level of a git checkout: give --repo <dir> --rev <rev> for the revision it was exported from"
  BC="$(git -C "$REPO" rev-parse --short "$REV^{commit}" 2>/dev/null)" || refuse 2 "$REV is not a commit of $REPO"
  BV="$(git -C "$REPO" describe --tags --match 'v[0-9]*' --always "$BC" 2>/dev/null)"
  # The identity names a revision, so the export must hold that revision's files, no more and no fewer.
  CMP="$(mktemp -d)" || refuse 2 "mktemp failed"
  trap 'rm -rf "$CMP"' EXIT
  git -C "$REPO" archive --format=tar "$BC" chain docker 2>/dev/null | tar -x -C "$CMP" 2>/dev/null || refuse 2 "git archive of $BC failed"
  DIFF="$(diff -rq "$CMP/chain" "$TREE/chain" 2>&1; diff -rq "$CMP/docker" "$TREE/docker" 2>&1)"
  [ -z "$DIFF" ] || refuse 2 "$TREE differs from $BC on chain or docker: $(printf '%s' "$DIFF" | head -3 | tr '[:space:]' ' ')"
  echo "   export of $BC from $REPO, identical on chain and docker"
fi
[ -n "${BV:-}" ] && [ -n "${BC:-}" ] || refuse 2 "git describe or rev-parse answered nothing: an image without its identity names no binary"
export DENDRA_BUILD_VERSION="$BV" DENDRA_BUILD_COMMIT="$BC"
echo "   build identity: version=$DENDRA_BUILD_VERSION commit=$DENDRA_BUILD_COMMIT"

echo "== 3) ssh to root@$HOST =="
SSHO="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
# shellcheck disable=SC2086
if [ -n "${SSHPASS:-}" ]; then
  command -v sshpass >/dev/null 2>&1 || refuse 3 "SSHPASS is set but sshpass is missing: sudo apt install -y sshpass"
  SSH_MODE=password; RSH="sshpass -e ssh -n $SSHO root@$HOST"; RSH_IN="sshpass -e ssh $SSHO root@$HOST"
elif ssh -o BatchMode=yes -o PasswordAuthentication=no $SSHO -n "root@$HOST" true 2>/dev/null; then
  SSH_MODE=key; RSH="ssh -n -o BatchMode=yes $SSHO root@$HOST"; RSH_IN="ssh -o BatchMode=yes $SSHO root@$HOST"
else
  echo "  cannot reach root@$HOST by key. By key: ssh-copy-id root@$HOST; by password: export SSHPASS before this script."
  exit 3
fi
echo "   authenticating by $SSH_MODE"

echo "== 4) build here =="
command -v docker >/dev/null 2>&1 || refuse 2 "docker is required on this machine: the image is built here, not on the host"
docker build -f "$TREE/docker/Dockerfile.chain" --build-arg VERSION="$DENDRA_BUILD_VERSION" --build-arg COMMIT="$DENDRA_BUILD_COMMIT" -t "$TAG" "$TREE" < /dev/null || refuse 2 "the local image build failed"

echo "== 5) ship with save and load =="
# RSH_IN reads the pipe; every other ssh call carries -n.
docker save "$TAG" | gzip -1 | $RSH_IN 'gunzip | docker load' || refuse 3 "the image transfer to $HOST failed"

echo "== 6) compare the image configuration on both sides =="
# Layers and runtime settings, read from the image configuration that save and load carry byte for byte; the
# image ID depends on the daemon's store and is not compared (launch_public.sh step 4).
FP='{{json .RootFS.Layers}} {{json .Config.Entrypoint}} {{json .Config.Cmd}} {{json .Config.Env}} {{json .Config.WorkingDir}} {{json .Config.User}}'
HERE="$(docker image inspect -f "$FP" "$TAG" 2>/dev/null || true)"
THERE="$($RSH "docker image inspect -f '$FP' $TAG" 2>/dev/null || true)"
# A layer list without a single digest is not a fingerprint: empty, null and an error all land here.
case "$HERE" in '['*sha256:*) ;; *) refuse 2 "image $TAG is not readable here after the build (got '${HERE:-nothing}')" ;; esac
case "$THERE" in '['*sha256:*) ;; *) refuse 4 "the host does not say which image $TAG it holds (got '${THERE:-nothing}'); not knowing is not a match" ;; esac
if [ "$HERE" != "$THERE" ]; then
  echo "  refused: image $TAG differs between here and the host (layers or runtime settings):"
  echo "     built : $HERE"
  echo "     host  : $THERE"
  exit 5
fi
echo "   $TAG: $(printf '%s' "${HERE%%]*}" | grep -o 'sha256:' | wc -l | tr -d '[:space:]') layers and the runtime settings identical on $HOST"
echo "   Nothing was started: no compose command ran, and the containers of $HOST run what they ran before."
