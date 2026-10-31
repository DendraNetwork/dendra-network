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
# cannot say what changed, or where it stands on the two gates, is not created.
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

# The update instruction is the kit's own, read from deploy/join.sh::kit_update_hint in the tagged tree: its git
# words are never retyped here, since a second copy would drift from the one the kit prints. Unreadable -> an
# error, like a missing gate: notes that cannot say how to update are not written.
[ -r deploy/join.sh ] || die "deploy/join.sh is missing: the update instruction (kit_update_hint) cannot be read"
UPD="$(sed -n '/^kit_update_hint(){/,/^}/p' deploy/join.sh | tr -d '\r' \
       | sed -n 's/.*printf .cd %s && \(git fetch origin [^,]*\), then re-run.*/\1/p' | head -1)"
[[ "$UPD" == "git fetch origin "* ]] || die "deploy/join.sh: the git words of kit_update_hint could not be read"
# kit_update_hint fills the tag in with printf; the notes name the tag they are written for.
UPD="${UPD//%s/$TAG}"
[[ "$UPD" != *%* ]] || die "deploy/join.sh: the git words of kit_update_hint carry a printf field other than the tag: $UPD"

# The upgrade plans this tree's binary registers, read by the tree's own reader (deploy/testnet-miner/kit_update.sh
# --plans, which moves nothing). A tree that registers one is the release of a governed upgrade: a network on an
# earlier epoch adopts it at the plan's height, not at publication, and the notes say what a joiner does meanwhile.
# An unreadable list is an error, like a missing gate: notes that cannot say whether the release waits for a plan
# are not written.
[ -r deploy/testnet-miner/kit_update.sh ] || die "deploy/testnet-miner/kit_update.sh is missing: the upgrade plans of this tree cannot be read"
PLANS_OUT="$(bash deploy/testnet-miner/kit_update.sh --plans . 2>/dev/null || true)"
PLANS_STATE=""; PLANS=""; PLANS_WHY=""
while IFS= read -r _l; do
  case "$_l" in plans_state=*) PLANS_STATE="${_l#*=}" ;; plans=*) PLANS="${_l#*=}" ;; plans_why=*) PLANS_WHY="${_l#*=}" ;; esac
done <<< "$PLANS_OUT"
case "$PLANS_STATE" in
  read|none) : ;;
  *) die "the upgrade plans of this tree could not be read (${PLANS_WHY:-no answer from deploy/testnet-miner/kit_update.sh --plans})" ;;
esac

# The tag's section of CHANGELOG.md: from its `## <tag>` heading to the next `## ` heading. Exactly one
# such heading, and at least one line of text under it. Fenced code blocks are recognised before headings,
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
  function link_base(s, img) {
    skip = 0
    if (s ~ /^([A-Za-z][A-Za-z0-9+.-]*:|\/\/|#)/ || s ~ /^($|\)|>)/) return ""
    if (substr(s, 1, 1) == "/") skip = 1
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
      p = link_base(s, 0); if (skip) s = substr(s, 2)
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
        out = out link_base(substr(line, i), im); if (skip) i++
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

EOF
if [ "$PLANS_STATE" = read ]; then
  BT='`'; PLAN_LIST=""
  for _p in $PLANS; do PLAN_LIST="${PLAN_LIST:+$PLAN_LIST, }$BT$_p$BT"; done
  printf '## A governed upgrade\n\n'
  printf 'This tree registers the upgrade plan %s (`upgradePlans` of `chain/app/upgrades.go`). A network\n' "$PLAN_LIST"
  printf 'on an earlier epoch moves to epoch %s through that plan, at the height its proposal sets, not when this\n' "$CE"
  cat <<'EOF'
release is published. Until the network's `network-info.txt` declares that epoch, `deploy/join.sh` refuses
this tree. Before the chain applies the plan, the refusal names the release of the declared epoch to install
instead; once the chain has applied it, or the kit's node stands at its halt, it prints the re-run with
`--after-upgrade <plan>`. A kit whose automatic update is on stages this release and applies it when its node
halts at the plan's height. From that halt until `network-info.txt` declares the new epoch, no new installation
works. The commands below hold once it does.

EOF
fi
cat <<'EOF'
## Join the network

EOF
l '```' \
  "git clone $URL.git && cd $NAME" \
  'CONFIG_URL=https://testnet-api.dendranetwork.com/network-info.txt bash deploy/join.sh --judge' \
  '```'
cat <<'EOF'

Already have a clone? Each publication of this repository is a new single commit, so `git pull`
refuses. From the clone, the kit's update instruction (`deploy/join.sh`, `kit_update_hint`):

EOF
l '```' "$UPD" '```'
cat <<'EOF'

then run `deploy/join.sh` again with the same options as the first time. The tag is verified with the
signers file of the tree the clone sits on before anything moves (a clone without `docker/RELEASE_SIGNERS`
cannot run that step: run `deploy/install.sh` instead, which chooses its trust root and says how).
`checkout --detach` refuses rather than overwrite a file git does not track, and the tree the clone leaves
stays on the branch the command names until the next update. `deploy/install.sh` keeps none of the options it was first given, so running
it again is not the same update.

## Binaries

Static `dendrad` binaries: no Go toolchain is needed to run a node.

EOF
l '```' \
  "tar xzf dendrad_${TAG}_linux_amd64.tar.gz" \
  "./dendrad_${TAG}_linux_amd64/dendrad version" \
  '```'
# The principal the sums are verified under is the one docker/RELEASE_SIGNERS names (publish_release.sh reads
# the same line). A tree without that file ships no signature to verify, and the notes say so.
PRINC=""
if [ -r docker/RELEASE_SIGNERS ]; then
  PRINC="$(awk 'NF >= 3 && $1 !~ /^#/ { print $1; exit }' docker/RELEASE_SIGNERS)"
fi
if [ -n "$PRINC" ]; then
cat <<'EOF'

Verify what you downloaded: the signature of the checksum list first, with the signers file of this
tree, then the sums.

EOF
l '```' "ssh-keygen -Y verify -f docker/RELEASE_SIGNERS -I $PRINC -n dendra-release -s SHA256SUMS.sig < SHA256SUMS" "sha256sum -c SHA256SUMS --ignore-missing" '```'
cat <<'EOF'

`SHA256SUMS.sig` is attached after the build, by the project's offline key; until it is there, the sums
are unsigned. The HiveOS package has its own list, `SHA256SUMS.hiveos`, and its own signature,
`SHA256SUMS.hiveos.sig`, verified the same way.

EOF
else
cat <<'EOF'

Verify what you downloaded:

```
sha256sum -c SHA256SUMS --ignore-missing
```

This tree ships no `docker/RELEASE_SIGNERS`, so the checksum lists of this release carry no signature to
verify.

EOF
fi
cat <<'EOF'

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
in the clone, never from the network, and applies it to a new node only: a node that already holds chain
state keeps the binary it holds it with. Until the two lines of `node-image.txt` are committed to the tree
it clones, and on an engine of another platform, it builds the node image from the clone.
EOF
