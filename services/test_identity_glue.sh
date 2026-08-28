#!/usr/bin/env bash
# Bench for the entrypoint's identity resolution — the wait that decides whether a miner reveals.
#
# WHY IT EXISTS. `entrypoint-services.sh` waited for `/data/keys/$ID.sk`, with `$ID` coming from the
# .env (`MINER_ID`, derived by join.sh as `m-<hash>`). But `miner.py` ALIGNS the identity to the
# one the chain accepts BEFORE naming any file, so the key lands at `dm1….sk` and that wait could
# never succeed. Both workers sit inside the `if [ -f ]` that follows, so on the canonical install
# path NEITHER reveal_worker NOR judge_worker ever started. Revealing is the PRIMARY'S OBLIGATION:
# without it a sampled job has no artifact to judge, the client's fee stays held with no bound, and
# the miner is neither paid nor slashed — while the container reads "Up" and the only message blames
# a missing key that exists under the other name.
#
# The rule under test is small and its failure is expensive, which is exactly what deserves a bench.
# PURE logic: no docker, no chain, no keyring. Temp dirs stand in for the volume.
set -u
N=0; KO=0

# FAITHFUL COPY of the resolution inserted into entrypoint-services.sh.
resoudre() {
  KEYDIR="$1"; ID="$2"; EFF="$ID"; j=0
  while [ $j -lt 3 ]; do
    if [ -s "$KEYDIR/identite-resolue" ]; then
      _e="$(tr -d '[:space:]' < "$KEYDIR/identite-resolue" 2>/dev/null)"
      case "$_e" in dm1*) EFF="$_e" ;; esac
    fi
    [ -f "$KEYDIR/$EFF.sk" ] && break
    j=$((j+1))
  done
  printf '%s' "$EFF"
}

cas() {
  N=$((N+1))
  if [ "$2" = "$3" ]; then printf '  OK  %s\n' "$1"
  else KO=$((KO+1)); printf '  KO  %s  (attendu %s, obtenu %s)\n' "$1" "$2" "$3"; fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

echo "== THE CASE THAT SILENCED EVERY NEW MINER =="
D="$T/a"; mkdir -p "$D"
printf 'dm1teqlpyx9sctv4d954eejvz\n' > "$D/identite-resolue"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"          # the daemon wrote the key under the RESOLVED name
cas "asked 'm-abc123' -> workers follow dm1…" \
    "dm1teqlpyx9sctv4d954eejvz" "$(resoudre "$D" "m-abc123")"

echo
echo "== AN EXPLICIT dm1 IS UNCHANGED (the operator's own setup) =="
D="$T/b"; mkdir -p "$D"
printf 'dm1teqlpyx9sctv4d954eejvz\n' > "$D/identite-resolue"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"
cas "asked dm1… -> same dm1…" \
    "dm1teqlpyx9sctv4d954eejvz" "$(resoudre "$D" "dm1teqlpyx9sctv4d954eejvz")"

echo
echo "== NO MEMORY YET: fall back to the asked name, never invent one =="
D="$T/c"; mkdir -p "$D"
: > "$D/m-abc123.sk"                            # older daemon, key under the asked name
cas "no memory file -> keeps 'm-abc123'" "m-abc123" "$(resoudre "$D" "m-abc123")"

echo
echo "== AN EMPTY OR CORRUPT MEMORY MUST NOT WIN =="
D="$T/d"; mkdir -p "$D"
: > "$D/identite-resolue"                       # empty file
: > "$D/m-abc123.sk"
cas "empty memory -> keeps the asked name" "m-abc123" "$(resoudre "$D" "m-abc123")"

D="$T/e"; mkdir -p "$D"
printf '   \n' > "$D/identite-resolue"          # whitespace only
: > "$D/m-abc123.sk"
cas "blank memory -> keeps the asked name" "m-abc123" "$(resoudre "$D" "m-abc123")"

D="$T/e2"; mkdir -p "$D"
printf 'pas-un-identifiant\n' > "$D/identite-resolue"   # junk, not a derived id
: > "$D/m-abc123.sk"
cas "junk memory ignored, never adopted" "m-abc123" "$(resoudre "$D" "m-abc123")"

echo
echo "== A TRAILING CR (a .env edited on Windows) MUST NOT POISON THE NAME =="
D="$T/f"; mkdir -p "$D"
printf 'dm1teqlpyx9sctv4d954eejvz\r\n' > "$D/identite-resolue"
: > "$D/dm1teqlpyx9sctv4d954eejvz.sk"
cas "CRLF stripped" "dm1teqlpyx9sctv4d954eejvz" "$(resoudre "$D" "m-abc123")"

echo
echo "== NOTHING AT ALL: the loop ends, the caller reports rather than hangs =="
D="$T/g"; mkdir -p "$D"
cas "no key, no memory -> asked name returned" "m-abc123" "$(resoudre "$D" "m-abc123")"

echo
printf 'BANC_IDENTITY_GLUE_RESUME cas=%d ko=%d\n' "$N" "$KO"
[ "$KO" -eq 0 ] || exit 1
