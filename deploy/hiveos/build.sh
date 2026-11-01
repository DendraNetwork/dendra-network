#!/usr/bin/env bash
# build.sh <release tag vX.Y.Z> [output directory] [miner-image.txt] -- builds the HiveOS custom-miner package
# of a release:
#   <out>/dendra-X.Y.Z.tar.gz   one folder, `dendra`, as HiveOS requires: the h-* scripts, the manifest with
#                               the version and the release tag written in, and lib/ (install.sh, the hardware
#                               probe it reads, kit_pins.txt (the versions and signing keys it installs Docker
#                               and the NVIDIA toolkit at), the payout address check, and backup-keys.sh, which backs up
#                               and restores the miner's keys with no clone on the rig after a reflash), all
#                               from the tree being built,
#                               plus lib/miner-image.txt, the miner image of this release, when given
#                               and, in the manifest, the governed upgrade plan the tree registers
#   <out>/SHA256SUMS.hiveos     its checksum
# Run by .github/workflows/release.yml (job hiveos-package) on the tagged tree, with the miner-image.txt of the
# same release; job hiveos-attach attaches both to the published release.
#
# The miner image of the release. A tagged tree pins no miner image (docker/MINER_IMAGE names none on a tag), so
# a rig on the tag would compile the miner. Given the release's miner-image.txt, the package carries its two
# lines, and h-run.sh hands them to install.sh (--miner-image-file), which writes them into the clone for
# join.sh to check. They are checked here before anything is packed: exactly two lines, `# built-for:` naming
# this tag and this tree's gates (docker/KIT_VERSION, docker/CONSENSUS_EPOCH, line 1), then a value that names
# an image by digest, never by tag. Anything else is refused: a package carrying an image of another release
# or another kit would pull code other than the tree it installs. Without the third argument the package
# carries no image, says so, and a rig builds the image from the clone.
#
# The upgrade plan. A rig runs with the automatic update off, so no watch moves its node across a governed upgrade:
# the package of the new epoch does, once the chain halted at the plan's height. Its h-run.sh passes the plan to
# install.sh (--after-upgrade), which relays it to deploy/join.sh (after_upgrade_applied, node_halt_confirmed). The
# plan is read from this tree by the tree's own reader, `deploy/testnet-miner/kit_update.sh --plans` (plans_of_tree,
# the variable upgradePlans of chain/app/upgrades.go), and written into the manifest as DENDRA_UPGRADE_PLAN.
# A tree that registers no plan writes it empty. One that registers several is refused: the package names one plan,
# and nothing this file reads says which of them brings a network to this tree's epoch. A tree whose plans cannot
# be read is refused too: a package that cannot tell would leave a rig stopped at the halt with nothing to pass it.
#
# The version is the tag without its v, and it has no dash: HiveOS reads the archive name as
# <name>-<version>.tar.gz, so a dash inside the version would be read as part of the name. Only
# vMAJOR.MINOR.PATCH is accepted, without leading zeros as the release workflow writes it; anything else is
# refused before a file is written.
# The archive is reproducible, like the release's other archives: fixed entry order, timestamps taken from
# SOURCE_DATE_EPOCH or else from the last commit, no owner names, gzip without its own timestamp. Without a
# reproducible date the build is refused rather than stamped with the clock.
# Every file in it ends its lines with LF: HiveOS runs these scripts with bash, and a carriage return would
# break them. A file that carries one is refused.
# Exit codes: 0 built · 1 a step failed · 2 refused (version, tree, miner image, upgrade plan) · 3 no reproducible date.
set -uo pipefail
TAG="${1:-}"
OUT="${2:-dist-hiveos}"
MINER_PIN="${3:-}"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
ROOT="$(cd "$HERE/../.." 2>/dev/null && pwd)"
refuse(){ printf '[hiveos] refused: %s\n' "$*" >&2; exit 2; }
# has_cr <file> -> 0 when the file carries a carriage return (byte 0d). Not `grep -q`: under pipefail, grep -q
# leaves at the first match, od dies on the closed pipe, and the pipeline reports a failure -- the one file
# that carries a carriage return would then be the one that passes.
has_cr(){ od -An -v -tx1 "$1" | grep -w 0d >/dev/null; }

# The tag form of the release workflow (publish_release.sh, .github/release_notes.sh): no leading zero.
printf '%s\n' "$TAG" | grep -Ex 'v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)' >/dev/null || refuse "the release tag must be vMAJOR.MINOR.PATCH without leading zeros (got '$TAG'): the package version is that number, and HiveOS reads a dash in it as part of the package name."
VER="${TAG#v}"

SRC="$HERE/dendra"
# The payout address check is the miner's own, wherever the tree keeps the miner's code.
SVC=""
for _d in "$ROOT/services" "$ROOT/services"; do
  [ -r "$_d/final_season_address.py" ] && { SVC="$_d"; break; }
done
for _f in "$SRC/h-manifest.conf" "$SRC/h-config.sh" "$SRC/h-run.sh" "$SRC/h-stats.sh" "$ROOT/deploy/install.sh" "$ROOT/deploy/hw_probe.sh" "$ROOT/deploy/kit_pins.txt" "$ROOT/deploy/testnet-miner/backup-keys.sh" "$ROOT/deploy/testnet-miner/kit_update.sh"; do
  [ -r "$_f" ] || refuse "$_f is missing from this tree."
done
if [ -z "$SVC" ] || [ ! -r "$SVC/modea/cosmos_addr.py" ] || [ ! -r "$SVC/modea/__init__.py" ]; then
  refuse "the payout address check (final_season_address.py and modea/cosmos_addr.py) is not in this tree."
fi
# Line endings of the sources, before anything is read from them: a manifest checked out with CRLF would
# otherwise be refused below for a name it does not have ('dendra' followed by a carriage return).
for _f in "$SRC/h-manifest.conf" "$SRC/h-config.sh" "$SRC/h-run.sh" "$SRC/h-stats.sh"; do
  if has_cr "$_f"; then refuse "${_f#"$ROOT"/} carries a carriage return (a Windows checkout?): HiveOS runs these files with bash."; fi
done
NAME="$(awk 'index($0, "CUSTOM_NAME=") == 1 { print substr($0, 13); exit }' "$SRC/h-manifest.conf")"
[ "$NAME" = dendra ] || refuse "h-manifest.conf names the package '$NAME', and its folder is 'dendra': HiveOS needs the two equal."

# The release's miner image (third argument): judged before anything is written, see the header.
if [ -n "$MINER_PIN" ]; then
  [ -f "$MINER_PIN" ] && [ -r "$MINER_PIN" ] || refuse "the miner image file $MINER_PIN cannot be read."
  if has_cr "$MINER_PIN"; then refuse "the miner image file carries a carriage return: it would reach the clone's docker/MINER_IMAGE."; fi
  _kv="$(head -1 "$ROOT/docker/KIT_VERSION" 2>/dev/null | tr -dc 0-9)"
  _ce="$(head -1 "$ROOT/docker/CONSENSUS_EPOCH" 2>/dev/null | tr -dc 0-9)"
  [ -n "$_kv" ] && [ -n "$_ce" ] || refuse "the gates of this tree (docker/KIT_VERSION, docker/CONSENSUS_EPOCH) cannot be read: the miner image cannot be checked against them."
  _want="# built-for: kit_version=$((10#$_kv)) consensus_epoch=$((10#$_ce)) release=$TAG"
  _n="$(grep -c '' "$MINER_PIN" 2>/dev/null || true)"
  _l1="$(sed -n 1p "$MINER_PIN")"; _l2="$(sed -n 2p "$MINER_PIN")"
  [ "$_n" = 2 ] && [ -z "$(tail -c1 "$MINER_PIN")" ] || refuse "the miner image file is not exactly two lines ('# built-for: ...' then 'DENDRA_MINER_IMAGE=...')."
  [ "$_l1" = "$_want" ] || refuse "the miner image file's first line is '$_l1', and this package needs '$_want': an image built for another release or another kit is not this tree's."
  printf '%s\n' "$_l2" | LC_ALL=C grep -Ex 'DENDRA_MINER_IMAGE=[a-z0-9._/-]+@sha256:[0-9a-f]{64}' >/dev/null \
    || refuse "the miner image file's second line is not 'DENDRA_MINER_IMAGE=<image>@sha256:<64 lowercase hex>': a tag can be moved, a digest cannot."
fi

# The upgrade plan of this tree (see the header), read before anything is written. kit_update.sh --plans answers
# key=value lines and 0 (plans read), 2 (none registered) or 3 (unreadable); its fields are read by key.
_po="$(bash "$ROOT/deploy/testnet-miner/kit_update.sh" --plans "$ROOT" 2>/dev/null)"; _prc=$?
_pstate="$(printf '%s\n' "$_po" | sed -n 's/^plans_state=//p' | head -1)"
_plans="$(printf '%s\n' "$_po" | sed -n 's/^plans=//p' | head -1)"
_pwhy="$(printf '%s\n' "$_po" | sed -n 's/^plans_why=//p' | head -1)"
PLAN=""
case "$_prc:$_pstate" in
  0:read)
    _pc="$(printf '%s\n' "$_plans" | wc -w | tr -dc 0-9)"
    [ "$_pc" = 1 ] || refuse "this tree registers $_pc upgrade plans ($_plans) and a package names one: nothing this file reads says which of them brings a network to this tree's epoch."
    printf '%s\n' "$_plans" | LC_ALL=C grep -Ex '[A-Za-z0-9._-]{1,128}' >/dev/null \
      || refuse "the upgrade plan of this tree ('$_plans') is outside the alphabet a rig passes on (letters, digits and . _ -)."
    PLAN="$_plans" ;;
  2:none) : ;;
  *) refuse "the upgrade plans of this tree cannot be read (deploy/testnet-miner/kit_update.sh --plans, exit $_prc: ${_pwhy:-no answer}): a package that cannot tell whether its tree carries a plan would leave a rig stopped at the halt with nothing to pass it." ;;
esac

EPOCH="${SOURCE_DATE_EPOCH:-}"
[ -n "$EPOCH" ] || EPOCH="$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null)"
printf '%s\n' "$EPOCH" | grep -Ex '[0-9]+' >/dev/null || { printf '[hiveos] not measurable: no reproducible date (SOURCE_DATE_EPOCH, or a git tree).\n' >&2; exit 3; }

STAGE="$(mktemp -d)" || exit 1
trap 'rm -rf "$STAGE"' EXIT
P="$STAGE/dendra"
mkdir -p "$P/lib/modea" || exit 1
cp "$SRC/h-config.sh" "$SRC/h-run.sh" "$SRC/h-stats.sh" "$P/" || exit 1
cp "$ROOT/deploy/install.sh" "$ROOT/deploy/hw_probe.sh" "$ROOT/deploy/kit_pins.txt" "$ROOT/deploy/testnet-miner/backup-keys.sh" "$P/lib/" || exit 1
cp "$SVC/final_season_address.py" "$P/lib/" || exit 1
cp "$SVC/modea/__init__.py" "$SVC/modea/cosmos_addr.py" "$P/lib/modea/" || exit 1
if [ -n "$MINER_PIN" ]; then
  cp "$MINER_PIN" "$P/lib/miner-image.txt" || exit 1
  printf '[hiveos] the package carries the miner image of %s (lib/miner-image.txt)\n' "$TAG"
else
  printf '[hiveos] no miner image given: the package carries none, and a rig builds the miner image from the clone\n'
fi
# The version, the tag and the upgrade plan are written in; every other line of the manifest is copied as it is.
awk -v ver="$VER" -v tag="$TAG" -v plan="$PLAN" '
  index($0, "CUSTOM_VERSION=") == 1 { print "CUSTOM_VERSION=" ver; next }
  index($0, "DENDRA_REF=") == 1 { print "DENDRA_REF=" tag; next }
  index($0, "DENDRA_UPGRADE_PLAN=") == 1 { print "DENDRA_UPGRADE_PLAN=" plan; next }
  { print }' "$SRC/h-manifest.conf" > "$P/h-manifest.conf" || exit 1
if ! grep -qx "CUSTOM_VERSION=$VER" "$P/h-manifest.conf" || ! grep -qx "DENDRA_REF=$TAG" "$P/h-manifest.conf" \
   || [ "$(grep -c '^DENDRA_UPGRADE_PLAN=' "$P/h-manifest.conf")" != 1 ] || ! grep -Fqx "DENDRA_UPGRADE_PLAN=$PLAN" "$P/h-manifest.conf"; then
  printf '[hiveos] FAILED: the manifest did not take the version, the tag and the upgrade plan (one DENDRA_UPGRADE_PLAN= line).\n' >&2; exit 1
fi
if [ -n "$PLAN" ]; then
  printf '[hiveos] the package names the upgrade plan %s (DENDRA_UPGRADE_PLAN): h-run.sh passes it to install.sh --after-upgrade\n' "$PLAN"
else
  printf '[hiveos] this tree registers no upgrade plan: the package names none\n'
fi

# Line endings: a carriage return in any file of the package is refused (lib/ included, copied from the tree).
for _f in $(find "$P" -type f | sort); do
  if has_cr "$_f"; then refuse "${_f#"$STAGE"/} carries a carriage return: HiveOS runs these files with bash."; fi
done
find "$P" -type d -exec chmod 0755 {} +
find "$P" -type f -exec chmod 0644 {} +
chmod 0755 "$P/h-config.sh" "$P/h-run.sh" "$P/h-stats.sh" "$P/lib/install.sh" "$P/lib/hw_probe.sh" "$P/lib/backup-keys.sh"

mkdir -p "$OUT" || exit 1
ARCH="dendra-$VER.tar.gz"
if ! tar --sort=name --format=gnu --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner -C "$STAGE" -cf - dendra | gzip -n -9 > "$OUT/$ARCH"; then
  printf '[hiveos] FAILED: the archive could not be written.\n' >&2; exit 1
fi
( cd "$OUT" && sha256sum "$ARCH" > SHA256SUMS.hiveos ) || exit 1
printf '[hiveos] built %s/%s for %s\n' "$OUT" "$ARCH" "$TAG"
cat "$OUT/SHA256SUMS.hiveos"
