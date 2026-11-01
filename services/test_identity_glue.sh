#!/usr/bin/env bash
# Bench for the entrypoint's identity resolution, the wait that decides whether a miner reveals.
#
# `entrypoint-services.sh` must wait for the key under the identity the daemon resolved
# (`resolved-identity`, a `dm1...` name), not under the `MINER_ID` of the .env (`m-<hash>`): the daemon
# aligns the identity to the one the chain accepts before naming any file. Waiting for the wrong name
# starts neither reveal_worker nor judge_worker, so a sampled job has no artifact to judge and the
# client's fee stays held, while the container reads "Up".
#
# Pure logic: no docker, no chain, no keyring. Temporary directories stand in for the volume.
set -u
N=0; KO=0

# A copy of the resolution in entrypoint-services.sh: keep the two identical.
resolve() {
  KEYDIR="$1"; ID="$2"; EFF="$ID"; j=0
  while [ $j -lt 3 ]; do
    _rf="$KEYDIR/resolved-identity"; [ -s "$_rf" ] || _rf="$KEYDIR/identite-resolue"
    if [ -s "$_rf" ]; then
      _e="$(tr -d '[:space:]' < "$_rf" 2>/dev/null)"
      case "$_e" in dm1*) EFF="$_e" ;; esac
    fi
    [ -f "$KEYDIR/$EFF.sk" ] && break
    j=$((j+1))
  done
  printf '%s' "$EFF"
}

check() {
  N=$((N+1))
  if [ "$2" = "$3" ]; then printf '  OK  %s\n' "$1"
  else KO=$((KO+1)); printf '  KO  %s  (expected %s, got %s)\n' "$1" "$2" "$3"; fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

echo "== A new miner: the workers follow the resolved identity =="
D="$T/a"; mkdir -p "$D"
printf 'dm1teqlpyx9sctv4d954eejvz\n' > "$D/resolved-identity"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"          # the daemon wrote the key under the resolved name
check "asked 'm-abc123' -> workers follow dm1…" \
    "dm1teqlpyx9sctv4d954eejvz" "$(resolve "$D" "m-abc123")"

echo
echo "== An explicit dm1 identity is unchanged (the operator's own setup) =="
D="$T/b"; mkdir -p "$D"
printf 'dm1teqlpyx9sctv4d954eejvz\n' > "$D/resolved-identity"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"
check "asked dm1… -> same dm1…" \
    "dm1teqlpyx9sctv4d954eejvz" "$(resolve "$D" "dm1teqlpyx9sctv4d954eejvz")"

echo
echo "== No memory yet: fall back to the asked name, never invent one =="
D="$T/c"; mkdir -p "$D"
: > "$D/m-abc123.sk"                            # older daemon, key under the asked name
check "no memory file -> keeps 'm-abc123'" "m-abc123" "$(resolve "$D" "m-abc123")"

echo
echo "== An empty or corrupt memory never wins =="
D="$T/d"; mkdir -p "$D"
: > "$D/resolved-identity"                       # empty file
: > "$D/m-abc123.sk"
check "empty memory -> keeps the asked name" "m-abc123" "$(resolve "$D" "m-abc123")"

D="$T/e"; mkdir -p "$D"
printf '   \n' > "$D/resolved-identity"          # whitespace only
: > "$D/m-abc123.sk"
check "blank memory -> keeps the asked name" "m-abc123" "$(resolve "$D" "m-abc123")"

D="$T/e2"; mkdir -p "$D"
printf 'not-an-identifier\n' > "$D/resolved-identity"   # junk, not a derived id
: > "$D/m-abc123.sk"
check "junk memory ignored, never adopted" "m-abc123" "$(resolve "$D" "m-abc123")"

echo
echo "== A trailing CR (a .env edited on Windows) does not corrupt the name =="
D="$T/f"; mkdir -p "$D"
printf 'dm1teqlpyx9sctv4d954eejvz\r\n' > "$D/resolved-identity"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"
check "CRLF stripped" "dm1teqlpyx9sctv4d954eejvz" "$(resolve "$D" "m-abc123")"

echo
echo "== A volume from an older kit: the file under its earlier name is still read =="
D="$T/h"; mkdir -p "$D"
echo dm1teqlpyx9sctv4d954eejvz > "$D/identite-resolue"   # the pre-rename name (French), data
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"
check "old file name only -> workers follow dm1…" "dm1teqlpyx9sctv4d954eejvz" "$(resolve "$D" "m-abc123")"
D="$T/i"; mkdir -p "$D"
echo dm1aaaaaaaaaaaaaaaaaaaaaa > "$D/identite-resolue"
echo dm1teqlpyx9sctv4d954eejvz > "$D/resolved-identity"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"
check "both names -> the current one wins" "dm1teqlpyx9sctv4d954eejvz" "$(resolve "$D" "m-abc123")"

echo
echo "== Nothing at all: the loop ends, and the caller reports instead of hanging =="
D="$T/g"; mkdir -p "$D"
check "no key, no memory -> asked name returned" "m-abc123" "$(resolve "$D" "m-abc123")"

echo
printf 'IDENTITY_GLUE_SUMMARY cases=%d ko=%d\n' "$N" "$KO"
[ "$KO" -eq 0 ] || exit 1
