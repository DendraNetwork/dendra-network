#!/usr/bin/env bash
# .github/release_notes.sh -- the title and the notes of a GitHub release, read from the tagged tree.
#
#   bash .github/release_notes.sh --title <tag>              # one line: the release title
#   bash .github/release_notes.sh <tag> <repository-url>     # the notes, Markdown, on stdout
#
# Run it from the root of the tree the tag points at; the release workflow does. Every figure it prints
# comes from a file of that tree: `kit_version` and `consensus_epoch` from VERSION, the summary of the
# changes from the tag's section of CHANGELOG.md. A figure typed into notes by hand goes stale without a
# sound, so none is typed here.
#
# A missing file, field or section stops it with an error, never with an empty paragraph: a release that
# cannot say what changed, or where it stands on the two gates, is not created (docs/adr/ADR-050).
set -euo pipefail

die(){ echo "release_notes.sh: $*" >&2; exit 1; }
l(){ printf '%s\n' "$@"; }

TITLE=0
if [ "${1:-}" = "--title" ]; then TITLE=1; shift; fi
TAG="${1:-}"
URL="${2:-}"
[ -n "$TAG" ] || die "usage: release_notes.sh --title <tag> | release_notes.sh <tag> <repository-url>"
[[ "$TAG" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
  || die "'$TAG' is not of the form vMAJOR.MINOR.PATCH"

# A gate is read as digits or not at all, from the first line that names it. No default: an absent field
# is an error, never a zero, since a zero would publish a gate the tree does not carry. Tests are made
# with `[[ =~ ]]` rather than a pipe into `grep -q`, which exits at its first match and, under pipefail,
# turns the writer's SIGPIPE into a failure on long input.
field(){
  local v
  [ -r VERSION ] || die "VERSION is missing: this tree does not say which gates it carries"
  v="$(sed -n "s/^$1=//p" VERSION | tr -d '\r' | head -1)"
  [[ "$v" =~ ^[0-9]+$ ]] || die "VERSION: '$1' is missing or not a number ('$v')"
  printf '%s' "$((10#$v))"
}
KV="$(field kit_version)"
CE="$(field consensus_epoch)"

if [ "$TITLE" = 1 ]; then
  printf 'Dendra %s — kit %s · epoch %s\n' "$TAG" "$KV" "$CE"
  exit 0
fi
[ -n "$URL" ] || die "the notes need the repository URL: release_notes.sh <tag> <repository-url>"
NAME="${URL##*/}"

# THE UPDATE CONSIGNE IS THE KIT'S ONE, read from deploy/join.sh::kit_update_hint in the tagged tree: its git
# words are never retyped here, since a second copy drifts from the one the kit prints. Unreadable -> an error,
# like a missing gate: notes that cannot say how to update are not written.
[ -r deploy/join.sh ] || die "deploy/join.sh is missing: the update consigne (kit_update_hint) cannot be read"
UPD="$(sed -n '/^kit_update_hint(){/,/^}/p' deploy/join.sh | tr -d '\r' \
       | sed -n 's/.*printf .cd %s && \(git fetch origin [^,]*\), then re-run.*/\1/p' | head -1)"
[[ "$UPD" == "git fetch origin "* ]] || die "deploy/join.sh: the git words of kit_update_hint could not be read"

# The tag's section of CHANGELOG.md: from its `## <tag>` heading to the next `## ` heading. Exactly one
# such heading, and at least one line of text under it. Fenced code blocks are recognised BEFORE headings,
# in the count as in the section: a `## ` line inside a block is code, not the start of another section.
[ -r CHANGELOG.md ] || die "CHANGELOG.md is missing"
N="$(awk -v t="$TAG" '
  { sub(/\r$/, "") }
  /^[ \t]*(```|~~~)/ { f = !f; next }
  !f && /^## / && $2 == t { n++ }
  END { print n + 0 }' CHANGELOG.md)"
[ "$N" = 1 ] || die "CHANGELOG.md has $N sections headed '## $TAG'; exactly one is expected"
# A relative link resolves against the repository in CHANGELOG.md, and against the release page once the
# section is pasted into a release body, where it is dead. Each one is rewritten to the same file at this
# tag, under `blob/` for a link and `raw/` for an image: inline links `](target)` and `](<target>)`,
# leading spaces kept, and reference definitions `[name]: target` (footnotes `[^n]:` are text). A target
# that starts with a scheme (any case), `//` or `#` is left as it is; a target from the repository root
# (`/docs/x.md`) loses its first `/`. Inline code spans, delimited by backtick runs of equal length, and
# fenced code blocks are left verbatim; a block still open at the end of the section is refused.
SECTION="$(awk -v t="$TAG" -v base="$URL/blob/$TAG/" -v raw="$URL/raw/$TAG/" '
  BEGIN { bt = sprintf("%c", 96) }
  function lien(s, img) {
    saute = 0
    if (s ~ /^([A-Za-z][A-Za-z0-9+.-]*:|\/\/|#)/ || s ~ /^($|\)|>)/) return ""
    if (substr(s, 1, 1) == "/") saute = 1
    return img ? raw : base
  }
  { sub(/\r$/, "") }
  {
    if ($0 ~ /^[ \t]*(```|~~~)/) { fence = !fence; if (on) print; next }
    if (!fence && $0 ~ /^## /) { if (on) exit; if ($2 == t) { on = 1; next } }
    if (!on) next
    if (fence) { print; next }
    if (match($0, /^[ \t]*\[[^]^][^]]*\]:[ \t]*/)) {
      head = substr($0, 1, RLENGTH); s = substr($0, RLENGTH + 1)
      if (substr(s, 1, 1) == "<") { head = head "<"; s = substr(s, 2) }
      p = lien(s, 0); if (saute) s = substr(s, 2)
      print head p s; next
    }
    line = $0; n = length(line); out = ""; code = 0; clen = 0; depth = 0; i = 1
    while (i <= n) {
      c = substr(line, i, 1)
      if (c == bt) {
        L = 0; while (substr(line, i + L, 1) == bt) L++
        if (!code) { code = 1; clen = L } else if (L == clen) code = 0
        out = out substr(line, i, L); i += L; continue
      }
      if (code) { out = out c; i++; continue }
      if (c == "[") { depth++; img[depth] = (i > 1 && substr(line, i - 1, 1) == "!"); out = out c; i++; continue }
      if (c == "]" && substr(line, i + 1, 1) == "(") {
        im = (depth > 0) ? img[depth] : 0; if (depth > 0) depth--
        out = out "]("; i += 2
        if (substr(line, i, 1) == "<") { out = out "<"; i++ }
        while (substr(line, i, 1) == " " || substr(line, i, 1) == "\t") { out = out substr(line, i, 1); i++ }
        out = out lien(substr(line, i), im); if (saute) i++
        continue
      }
      if (c == "]" && depth > 0) depth--
      out = out c; i++
    }
    print out
  }
  END { if (on && fence) exit 3 }' CHANGELOG.md)" \
  || die "the section '## $TAG' of CHANGELOG.md leaves a code block open"
[[ "$SECTION" =~ [^[:space:]] ]] || die "the section '## $TAG' of CHANGELOG.md is empty"

printf 'Kit version **%s** · consensus epoch **%s**\n\n' "$KV" "$CE"
printf '## What changed\n\n%s\n\n' "$SECTION"

cat <<'EOF'
## Do I need to update?

Two numbers, and they do not mean the same thing. The network publishes both in `network-info.txt` and
this tree carries both in `VERSION`, so the comparison needs no one's word.

| | If yours differs |
|---|---|
| **consensus_epoch** | Your binary takes different state transitions: a **fork**. The node syncs, then halts on an AppHash mismatch, which looks like a network problem and is not one. `deploy/join.sh` **refuses**. |
| **kit_version** | The scripts and services **you** run are behind on fixes. The node still joins; `deploy/join.sh` **warns**. A defect there is silent by nature, since the code does its job badly rather than failing, so update when convenient. |

The version number follows these two gates: the minor number moves with `consensus_epoch`, the patch
number with `kit_version` alone. A change that moves neither is republished without a number, save an
exceptional release whose section says why (`CHANGELOG.md`).

## Join the network

EOF
l '```' \
  "git clone $URL.git && cd $NAME" \
  'CONFIG_URL=https://testnet-api.dendranetwork.com/network-info.txt bash deploy/join.sh --judge' \
  '```'
cat <<'EOF'

Already have a clone? Each publication of this repository is a new single commit, so `git pull`
refuses. From the clone, the kit's update consigne (`deploy/join.sh`, `kit_update_hint`):

EOF
l '```' "$UPD" '```'
cat <<'EOF'

then run `deploy/join.sh` again with the same options as the first time. `checkout -B` refuses rather
than overwrite a file git does not track, and the tree the clone leaves stays on the branch the command
names until the next update. `deploy/install.sh` keeps none of the options it was first given, so running
it again is not the same update.

## Binaries

Static `dendrad` binaries: no Go toolchain is needed to run a node.

EOF
l '```' \
  "tar xzf dendrad_${TAG}_linux_amd64.tar.gz" \
  "./dendrad_${TAG}_linux_amd64/dendrad version" \
  '```'
cat <<'EOF'

Verify what you downloaded:

```
sha256sum -c SHA256SUMS --ignore-missing
```

`SHA256SUMS.binaries` lists the SHA-256 of each **uncompressed** binary. Those are reproducible: the
toolchain is pinned by the `toolchain` directive in `chain/go.mod`, the build path is stripped, the VCS
stamp is disabled, and the version string is injected rather than written into a file. Building the same
tag on any machine yields the same bytes:

EOF
l '```' \
  "git clone $URL.git dendra" \
  "cd dendra && git checkout $TAG" \
  'cd chain' \
  'GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -buildvcs=false \' \
  '  -ldflags "-X github.com/cosmos/cosmos-sdk/version.Name=dendra \' \
  '            -X github.com/cosmos/cosmos-sdk/version.AppName=dendrad \' \
  "            -X github.com/cosmos/cosmos-sdk/version.Version=$TAG \\" \
  '            -X github.com/cosmos/cosmos-sdk/version.Commit=$(git rev-parse HEAD)" \' \
  '  -o dendrad ./cmd/dendrad' \
  'sha256sum dendrad' \
  '```'
cat <<'EOF'

Compare that value with the matching `linux_amd64` line of `SHA256SUMS.binaries`. A mismatch means the
published binary was not built from this tag, and should be reported through `SECURITY.md` rather than
used. The archive checksums in `SHA256SUMS` cover download integrity only, since they depend on how the
archive was packed.

## Prebuilt miner image

`miner-image.txt` names the miner image this release built, by digest. `deploy/join.sh` reads the image
from `docker/MINER_IMAGE` in the clone, never from the network: it pulls this one once the two lines of
`miner-image.txt` are committed to the tree it clones, and builds the image from the clone until then. It
starts the pulled image only when its platform is that of the Docker engine, and builds from the clone
otherwise.

## Prebuilt node image

`node-image.txt` names the node image this release built, by digest: one reference for `linux/amd64` and
`linux/arm64`. It is attached after the release is published, by a separate job, and only when its gate
passed: that job copied `dendrad` out of each platform's image and compared its SHA-256 with the matching
`linux` line of `SHA256SUMS.binaries` (`.github/node_image_gate.sh`), so the image runs the binary this
release publishes. A release without `node-image.txt` has no prebuilt node image. `deploy/join.sh` reads the image from `docker/NODE_IMAGE`
in the clone, never from the network, and applies it to a NEW node only: a node that already holds chain
state keeps the binary it holds it with. Until the two lines of `node-image.txt` are committed to the tree
it clones, and on an engine of another platform, it builds the node image from the clone.
EOF
