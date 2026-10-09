#!/usr/bin/env bash
# node_image_gate.sh <dist dir> <image dir> <version> -- the dendrad each NODE IMAGE carries is the RELEASED
# binary, byte for byte. Called by .github/workflows/release.yml (job node-image) before the two platform
# images are joined under one reference.
#
# WHY IT EXISTS. docker/Dockerfile.node builds dendrad with the flags of the release's `build` job and says the
# result is the published binary for the same tag and platform. A claim like that drifts in silence -- a flag
# dropped from one side, a toolchain resolved differently in the image -- and the image is what a new node
# runs its consensus with. So the two figures are compared, every release, for every platform the image
# serves:
#   <dist dir>/dendrad_<version>_linux_<arch>.bin.sha256   written by the `build` job ("<sha256>  <path>")
#   <image dir>/dendrad-linux-<arch>.sha256                written by the `node-image` job ("<sha256>")
#
# THREE ANSWERS, never two: 0 = equal on every platform; 1 = a mismatch somewhere; 2 = a figure missing or
# unreadable, which is NOT a pass. A mismatch is reported before an unreadable figure, since it is the finding.
set -u

dist="${1:-}"; img="${2:-}"; ver="${3:-}"
if [ -z "$dist" ] || [ -z "$img" ] || [ -z "$ver" ]; then
  echo "usage: node_image_gate.sh <dist dir> <image dir> <version>"
  echo "NODE_IMAGE_GATE state=unread platforms=0"
  exit 2
fi

# sha_of FILE -> the first field of its first line, when it is 64 lowercase hex digits; nothing otherwise.
sha_of(){
  local v
  v="$(head -1 "$1" 2>/dev/null | tr -d '\r' | cut -d' ' -f1)"
  case "$v" in *[!0-9a-f]*|'') return 0 ;; esac
  [ "${#v}" = 64 ] && printf '%s' "$v"
  return 0
}

bad=0; unread=0; n=0
for arch in amd64 arm64; do
  n=$((n + 1))
  want="$(sha_of "$dist/dendrad_${ver}_linux_${arch}.bin.sha256")"
  got="$(sha_of "$img/dendrad-linux-${arch}.sha256")"
  if [ -z "$want" ] || [ -z "$got" ]; then
    unread=$((unread + 1))
    echo "  [??] linux/$arch: released ${want:-UNREAD} / image ${got:-UNREAD} -- nothing is compared, nothing passes"
  elif [ "$got" = "$want" ]; then
    echo "  [ok] linux/$arch: the image's dendrad is the released binary ($got)"
  else
    bad=$((bad + 1))
    echo "  [KO] linux/$arch: the image's dendrad is $got, the released binary is $want"
  fi
done

if [ "$bad" != 0 ]; then
  echo "NODE_IMAGE_GATE state=mismatch platforms=$n mismatches=$bad unread=$unread"
  echo "The node image does not carry the released dendrad: it is not published. Find which side changed"
  echo "(docker/Dockerfile.node or the build job of .github/workflows/release.yml) before re-running."
  exit 1
fi
if [ "$unread" != 0 ]; then
  echo "NODE_IMAGE_GATE state=unread platforms=$n mismatches=0 unread=$unread"
  exit 2
fi
echo "NODE_IMAGE_GATE state=equal platforms=$n mismatches=0 unread=0"
exit 0
