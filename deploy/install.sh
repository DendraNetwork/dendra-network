#!/usr/bin/env bash
# install.sh -- prepare a Linux host for a Dendra miner, then hand over to deploy/join.sh.
#
# WHAT THIS FILE IS FOR. `join.sh` is the canonical entry point, and it assumes a host that already
# has Docker with Compose v2, the NVIDIA container toolkit when there is a GPU, git, and a clone of
# this repository. On a workstation that is a ten-minute detour; on a mining rig it is the whole
# afternoon, and it is where most people who wanted to run a miner stopped. This file does that
# detour -- and only that: it does not join the network, it does not touch a key, it does not
# verify the chain. When the host is ready it runs `join.sh`, which does all of those.
#
# WHAT IT DOES, IN THIS ORDER, AND NOTHING ELSE:
#   1. reads the host: distribution, RAM, free disk, NVIDIA GPU, Docker, Compose, the tools it needs;
#   2. installs what is missing from the distribution's or the vendor's apt repositories -- Docker
#      Engine + Compose v2 when there is no Docker at all, Compose alone when an Engine is already
#      installed (an installed Engine is never replaced, whatever its state), the NVIDIA container
#      toolkit when a GPU is present and Docker has no `nvidia` runtime, and git/curl/gpg when
#      absent (Ubuntu, Debian, and HiveOS, which is Ubuntu underneath);
#   3. adds the invoking user to the `docker` group;
#   4. clones the public repository into DENDRA_DIR, or fast-forwards a clone that carries no local
#      work -- a clone with local edits or local commits is refused, never reset;
#   5. runs `deploy/join.sh --judge` from that clone (or --miner / --validator per the flags).
# Run without the hardware probe next to it (downloaded alone), it places the clone (4) right after the tools git
# needs, asks the clone's probe for the role, and only then does the rest of 2 and 3 (see NO MINING MODEL below).
#
# IT CHANGES NOTHING WITHOUT --yes. Run it once without the flag: it prints what it would do and
# exits 2 when something is missing, 0 when nothing is. Every system change is listed before it
# happens -- including the Docker daemon restart the NVIDIA toolkit requires, which stops every
# running container on the host for a moment.
#
# IT IS NOT MEANT TO BE PIPED FROM A URL. Download it, read it, then run it -- the same rule the
# documentation applies to join.sh. This file verifies nothing about the chain; join.sh checks the
# genesis SHA-256 published in network-info.txt before joining.
#
# NO INBOUND PORT IS OPENED, AND NONE IS NEEDED FOR A MINER: it dials out to the relay, the RPC and
# the faucet. A home connection behind carrier-grade NAT qualifies. A VALIDATOR is a different role:
# it should be reachable on 26656, which `deploy/node_reachability.sh` measures after the fact.
#
# Usage:
#   bash deploy/install.sh                  # plan only, no change (same as --check)
#   bash deploy/install.sh --yes            # prepare the host, then join as miner + judge
#   bash deploy/install.sh --yes --light    # same, reading the chain from the public RPC
#                                           # (join.sh --remote-rpc: no local node, no local
#                                           # verification of the chain -- an explicit trade)
#   bash deploy/install.sh --yes --miner    # mine without asking for the judge role
#   bash deploy/install.sh --yes --validator
#   bash deploy/install.sh --yes --miner --ref vX.Y.Z --payout-address dendra1...
#                                           # clone the RELEASE TAG vX.Y.Z instead of following main
#                                           # (DENDRA_REF does the same), and hand the payout address
#                                           # to join.sh, which verifies it before anything changes
#   bash deploy/install.sh --yes --owner dendra1...
#                                           # owner mode (join.sh --owner): that key, NOT on this machine,
#                                           # registers the miner and holds its stake; handed to join.sh,
#                                           # which verifies its checksum before anything changes
#   ... --ref vX.Y.Z --miner-image-file <that release's miner-image.txt>
#                                           # and write the miner image that release built into the
#                                           # clone (see THE MINER IMAGE A PACKAGE CARRIES below)
#
# A TAG, NOT A BRANCH. --ref takes vMAJOR.MINOR.PATCH only, and the clone is checked to sit EXACTLY on
# that tag: a branch moves under the same name, a tag names one tree. With --ref the clone is never
# fast-forwarded to main -- the HiveOS package (deploy/hiveos) pins its own tag this way, so a rig runs the
# tree its package was built from, and updates only when its package is replaced.
#
# THE MINER IMAGE A PACKAGE CARRIES (--miner-image-file, with --ref only). A tagged tree pins no miner image
# (docker/MINER_IMAGE names none on a tag: an image is pinned after the release that built it), so a host on
# a tag builds the miner image, compiling dendrad. The HiveOS package carries the two lines of its release's
# miner-image.txt (`# built-for:` then DENDRA_MINER_IMAGE=). With this flag, once the clone is CHECKED to sit
# on the tag, they are written into the clone's docker/MINER_IMAGE -- only when the tag's file names no image,
# never over a value -- and join.sh judges them like any pin (deploy/join.sh::miner_image_pin): official
# digest, `built-for` gates equal to the clone's, the image's platform equal to the engine's; otherwise it
# builds from the clone and says why. That write is the ONE local change this file accepts in a clone: a copy
# of the bytes it wrote is kept in the clone's .git, a clone whose only change is EXACTLY those bytes is not
# local work, and they are undone before the clone moves (to another tag, or to main). Any other change to
# the file is local work, refused as before. Without the flag nothing is written: the image is built.
#   bash deploy/install.sh --yes --gpus all # one miner identity PER NVIDIA CARD (join.sh --gpus): each
#                                           # with its own key, stake, faucet drip and 24-word phrase
#
# NO MINING MODEL ON THE CPU. Before anything changes, this file asks deploy/hw_probe.sh --role (the probe next to
# it): a host with a usable NVIDIA card mines, as before; one without is a JUDGE on the CPU when it has at least
# MOE_CPU_MIN_RAM_MB of RAM (--miner is then refused), is refused below it (exit 2), and is not decided when its
# RAM cannot be read (exit 3). The probe says why, and this file prints it.
# THIS FILE DOWNLOADED ALONE -- the documented path: download it, read it, run it -- has no probe next to it. The
# role is then asked of the CLONE's deploy/hw_probe.sh, and only once this file has placed and checked that clone:
# the order becomes the tools git needs, the clone, the role, and only then Docker, the NVIDIA toolkit and the
# docker group. A host with no role is refused there (exit 2, or 3 when it cannot be decided), with the clone
# placed and nothing else of the plan done; the plan says so before --yes.
#
# THE JUDGE DISK FLOOR FOLLOWS THE PROBE. The judge role is asked for by default, and it pulls a second,
# much larger model. Its higher disk floor only matters to a host that CAN judge, so when free disk sits
# between the two floors this file asks deploy/hw_probe.sh, the one NEXT TO IT (--can-judge): `false`
# lowers the floor to the miner's (join.sh keeps the miner role), `true` keeps the judge floor, and an
# unknown answer or a missing probe keeps the judge floor too, saying why.
#
# Undoing it: bash deploy/uninstall.sh (plan only; --yes to remove what the kit set up, keys kept).
#
# UPDATING THE KIT -- the ONE instruction of this kit, the same words in deploy/join.sh (kit_update_hint) and
# deploy/testnet-miner/miner_health.sh: from the clone,
#   git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main
# then re-run deploy/join.sh with the same options (the tree the clone leaves stays on the branch
# kit-before-update until the next update: a commit of your own is never dropped in silence). Running this
# file again with --yes also updates the clone and runs join.sh, but with ITS flags only: it keeps none of
# those it was first given (without --miner it asks for the judge role, without --light it runs a node of its
# own), so give it the same flags as the first time.
# `git pull` does not work: each release republishes the repository as a NEW root commit, so git pull refuses
# ("refusing to merge unrelated histories") and leaves the old tree; a clone it left there is recognised below
# as an earlier publication, not as local work.
#
# Environment (all optional):
#   CONFIG_URL       network-info.txt of the network to join (default: the public network's)
#   DENDRA_DIR       where the repository lives or gets cloned (default: $HOME/dendra-network)
#   DENDRA_REPO_URL  git URL to clone (default: the public repository)
#   DENDRA_REF       the release tag to clone and stay on (as --ref; the flag wins)
#   DENDRA_INSTALL_RESULT_FILE  a file that receives the word of the last action (planned, joined,
#                    join_unmeasured, join_refused, join_failed, refused, failed): how the HiveOS package tells
#                    the two exit 3 apart
#   DENDRA_MIN_DISK_GB / DENDRA_MIN_DISK_GB_JUDGE / DENDRA_MIN_RAM_MB   refusal floors, see below
#
# Exit codes -- three answers, never two:
#   0  done, or a plan with nothing missing
#   1  a step was attempted and failed
#   2  refused: no --yes while changes are needed, unsupported host (no apt, WSL 1), too little disk
#      or RAM, no role on the testnet (deploy/hw_probe.sh --role: refused, or --miner without a usable GPU),
#      a clone that carries local work or cannot be inspected, a path or URL this file cannot pass on safely,
#      or join.sh refused (its own exit 2: nothing was started by it -- action=join_refused)
#   3  not measurable: the host could not be read (no /etc/os-release, no /proc/meminfo, no disk
#      figure, no role decided by deploy/hw_probe.sh --role), or join.sh started the miner and could not measure its health within its bound (join.sh
#      exit 3: the miner keeps running and registering) -- an unknown is never treated as a pass
set -u

# ---------------------------------------------------------------- flags
YES=0; CHECK=0; LIGHT=0; ROLE=judge; GPUS=""
REF="${DENDRA_REF:-}"; PAYOUT=""; OWNER=""; PKG_PIN_FILE=""
while [ $# -gt 0 ]; do case "$1" in
  --yes)       YES=1; shift;;
  --check)     CHECK=1; shift;;
  --light)     LIGHT=1; shift;;
  --judge)     ROLE=judge; shift;;
  --miner)     ROLE=miner; shift;;
  --validator) ROLE=validator; shift;;
  --ref)       REF="${2:-}"; [ -n "$REF" ] || { echo "[install] --ref needs a release tag (vX.Y.Z)"; exit 2; }; shift 2;;
  --payout-address) PAYOUT="${2:-}"; [ -n "$PAYOUT" ] || { echo "[install] --payout-address needs an address (dendra1...)"; exit 2; }; shift 2;;
  --owner)     OWNER="${2:-}"; [ -n "$OWNER" ] || { echo "[install] --owner needs an address (dendra1...)"; exit 2; }; shift 2;;
  --miner-image-file) PKG_PIN_FILE="${2:-}"; [ -n "$PKG_PIN_FILE" ] || { echo "[install] --miner-image-file needs a file (a release's miner-image.txt)"; exit 2; }; shift 2;;
  --gpus)      GPUS="${2:-}"; [ -n "$GPUS" ] || { echo "[install] --gpus needs a value: all, card indices (0,2) or card UUIDs"; exit 2; }; shift 2;;
  -h|--help)   awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; exit 0;;
  *) echo "[install] unknown argument: $1 (see --help)"; exit 2;;
esac; done
# ONE IDENTITY PER CARD: --gpus is relayed to join.sh, and validated HERE FIRST, by the expression join.sh
# applies (GPUS_SPEC_RE, the same text in both files): the value reaches a command line built as a string
# (`as_docker "... join.sh $JOIN_ARGS"`), where an unvalidated value is an injection. The expression judges
# the WHOLE value (bash's `=~`: ^ and $ are the ends of the string), never one line of it: `grep -q` succeeds
# when ONE line matches, and the next line of a value carrying a newline reached `sh -c`.
GPUS_SPEC_RE='^(all|[0-9]+(,[0-9]+)*|GPU-[0-9a-f-]+(,GPU-[0-9a-f-]+)*)$'
if [ -n "$GPUS" ]; then
  [[ $GPUS =~ $GPUS_SPEC_RE ]] \
    || { echo "[install] --gpus $GPUS is not all, a list of card indices (0,2) or a list of card UUIDs (GPU-...). Nothing was changed."; exit 2; }
  [ "$ROLE" != validator ] || { echo "[install] --gpus runs one MINER identity per card; a validator is one node. Nothing was changed."; exit 2; }
fi

say(){ printf '%s\n' "$*"; }
warn(){ printf '  [!] %s\n' "$*"; }
refuse(){ printf '  [REFUSED] %s\n' "$*" >&2; }
unmeasurable(){ printf '  [?] %s\n' "$*" >&2; exit 3; }

# ---------------------------------------------------------------- defaults and test seams
# The *_FILE / *_DIR / *_MOUNT variables below exist so that a bench can point this script at a
# fabricated host instead of the real one. They are test seams: never set them on a real machine.
CONFIG_URL="${CONFIG_URL:-https://testnet-api.dendranetwork.com/network-info.txt}"
DENDRA_DIR="${DENDRA_DIR:-$HOME/dendra-network}"
DENDRA_REPO_URL="${DENDRA_REPO_URL:-https://github.com/DendraNetwork/dendra-network.git}"
OS_RELEASE="${DENDRA_OS_RELEASE:-/etc/os-release}"
MEMINFO="${DENDRA_MEMINFO:-/proc/meminfo}"
PROC_VERSION="${DENDRA_PROC_VERSION:-/proc/version}"
HIVE_DIR="${DENDRA_HIVE_DIR:-/hive}"
WSL_HOST_MOUNT="${DENDRA_WSL_HOST_MOUNT:-/mnt/c}"
SYSTEMD_DIR="${DENDRA_SYSTEMD_DIR:-/run/systemd/system}"
SYSROOT="${DENDRA_SYSROOT:-}"
# Floors. The mining model is a few GB on disk, the images and the build cache add several more, and
# the first start compiles the chain binary, which needs memory. The judge role pulls a second, much
# larger model (the kit pins it in deploy/testnet-miner/docker-compose.yml, service judge-model-init,
# where its size is stated next to the tag), hence a higher floor for that role. Below these values
# the install does not fail loudly -- it fails an hour later inside a pull or build log -- so it is
# refused up front instead.
MIN_DISK_GB="${DENDRA_MIN_DISK_GB:-12}"
MIN_DISK_GB_JUDGE="${DENDRA_MIN_DISK_GB_JUDGE:-32}"
MIN_RAM_MB="${DENDRA_MIN_RAM_MB:-4000}"
WARN_RAM_MB=8000
# The hardware probe asked whether this host can judge: the one shipped NEXT TO this file, never one
# from the clone, which is not updated yet at this point and may not be this repository at all.
# DENDRA_HW_PROBE is a test seam, like the ones above.
HW_PROBE="${DENDRA_HW_PROBE:-$(dirname "$0")/hw_probe.sh}"
# NO PROBE NEXT TO THIS FILE (it was downloaded alone): the role is DEFERRED to the clone's probe, asked only after
# this file has placed and checked the clone (step 3, before Docker, the toolkit or the group). A probe NAMED by
# the seam and absent is not deferred: it stays what it was, a missing reading (exit 3).
PROBE_DEFERRED=0
[ -z "${DENDRA_HW_PROBE:-}" ] && [ ! -r "$HW_PROBE" ] && PROBE_DEFERRED=1

# A path or a URL is passed on to other shells below. One that carries a single quote cannot be
# quoted safely, so it is refused HERE, before anything is read or changed, rather than after Docker
# is installed.
case "$DENDRA_DIR$CONFIG_URL" in *"'"*) refuse "DENDRA_DIR or CONFIG_URL contains a single quote, which this file cannot pass on safely."; exit 2;; esac
case "$CONFIG_URL" in
  http://*|https://*) : ;;
  *) refuse "CONFIG_URL must start with http:// or https:// (got: $CONFIG_URL)"; exit 2;;
esac
# The tag and the two addresses are passed on to git and to join.sh: refused HERE when their form is not the
# one they must have, before anything is read or changed. The addresses' checksum is join.sh's to check, with
# the same refusals (it exits 2 before anything changes on its side).
# (The tag form is the release workflow's -- publish_release.sh, .github/release_notes.sh: no leading zero.)
# EACH FORM JUDGES THE WHOLE VALUE (bash's `=~`, where ^ and $ are the ends of the string), as GPUS_SPEC_RE does:
# `grep -x` judges one LINE, so a value whose first line had the right form passed with any second line -- and
# the addresses are spliced into the command string join.sh runs under (JOIN_ARGS, as_docker).
REF_RE='^v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$'
ADDR_FORM_RE='^[A-Za-z0-9]{1,128}$'
if [ -n "$REF" ] && ! [[ $REF =~ $REF_RE ]]; then
  refuse "--ref / DENDRA_REF must be a release tag vMAJOR.MINOR.PATCH, without leading zeros (got: $REF): a branch moves under its name, a tag names one tree."; exit 2
fi
# WHAT "UPDATE" MEANS FOR THIS COPY, handed to join.sh, which prints it on its "Update:" lines instead of the
# git command of a clone that follows main. A clone placed on a TAG does not follow main: resetting it to
# origin/main would leave the release it was installed from. A package (HiveOS) names its own path in
# DENDRA_UPDATE_HINT; it is passed on as a quoted word, so only plain characters are accepted.
UPDATE_HINT="${DENDRA_UPDATE_HINT:-}"
[ -z "$UPDATE_HINT" ] && [ -n "$REF" ] && UPDATE_HINT="run deploy/install.sh again with --ref set to a newer release tag"
if printf '%s' "$UPDATE_HINT" | LC_ALL=C grep '[^-A-Za-z0-9 .,:/_()]' >/dev/null; then
  refuse "DENDRA_UPDATE_HINT holds a character this file does not pass on (only letters, digits, spaces and . , : / _ ( ) -)."; exit 2
fi
if [ -n "$PAYOUT" ] && ! [[ $PAYOUT =~ $ADDR_FORM_RE ]]; then
  refuse "--payout-address holds characters no address has: it is not passed on (the value is not printed)."; exit 2
fi
[ "$ROLE" = validator ] && [ -n "$PAYOUT" ] && { refuse "--payout-address applies to a miner, not to --validator."; exit 2; }
# OWNER MODE (join.sh --owner): the same form and the same role rule as join.sh's -- a miner, the judge role
# included (join.sh runs it as a miner), never a validator. Its checksum is join.sh's to verify.
if [ -n "$OWNER" ] && ! [[ $OWNER =~ $ADDR_FORM_RE ]]; then
  refuse "--owner holds characters no address has: it is not passed on (the value is not printed)."; exit 2
fi
[ "$ROLE" = validator ] && [ -n "$OWNER" ] && { refuse "--owner applies to a miner, not to --validator. Nothing was changed."; exit 2; }

# >>> THE MINER IMAGE A PACKAGE CARRIES (chantier 2) -- see the header. Its file is READ here, before anything
# changes, so the plan can say what will happen to it; it is written only after the clone is checked on the tag.
# PKG_PIN_STATE: "" (no flag) | ok | unusable (PKG_PIN_WHY says why: nothing is then written, the image is built).
PKG_PIN_STATE=""; PKG_PIN_WHY=""
if [ -n "$PKG_PIN_FILE" ]; then
  [ -n "$REF" ] || { refuse "--miner-image-file goes with --ref: a release's miner image belongs to that release's tag, and a clone that follows main pins its own."; exit 2; }
  [ "$ROLE" = validator ] && { refuse "--miner-image-file applies to a miner, not to --validator."; exit 2; }
  PKG_PIN_STATE=unusable
  if [ ! -f "$PKG_PIN_FILE" ] || [ ! -r "$PKG_PIN_FILE" ]; then
    PKG_PIN_WHY="the package's miner image file ($PKG_PIN_FILE) cannot be read"
  else
    # EXACTLY the two lines of that release's miner-image.txt, each ending with a newline. The value's exact
    # form (official namespace, full digest) and the gates are join.sh's to judge, from the clone; here, only
    # the shape, the release it names and the characters, so that nothing else reaches the file.
    _pn="$(grep -c '' "$PKG_PIN_FILE" 2>/dev/null || true)"
    _p1="$(sed -n 1p "$PKG_PIN_FILE")"; _p2="$(sed -n 2p "$PKG_PIN_FILE")"
    if [ "$_pn" = 2 ] && [ -z "$(tail -c1 "$PKG_PIN_FILE")" ] \
       && printf '%s\n' "$_p1" | LC_ALL=C grep -Eqx '# built-for: kit_version=[0-9]+ consensus_epoch=[0-9]+ release=v[0-9]+[.][0-9]+[.][0-9]+' \
       && [ "${_p1##* release=}" = "$REF" ] \
       && printf '%s\n' "$_p2" | LC_ALL=C grep -Eqx 'DENDRA_MINER_IMAGE=[A-Za-z0-9./_:@-]+'; then
      PKG_PIN_STATE=ok
    else
      PKG_PIN_WHY="the package's miner image file is not the two lines of the miner-image.txt of $REF ('# built-for: ... release=$REF', then 'DENDRA_MINER_IMAGE=...')"
    fi
  fi
fi
# <<< THE MINER IMAGE A PACKAGE CARRIES

# Under WSL the NVIDIA tools live outside the standard PATH; a probe that cannot find nvidia-smi
# concludes "no GPU" and prepares a CPU-only host by mistake.
export PATH="$PATH:/usr/lib/wsl/lib:/usr/local/nvidia/bin:/opt/nvidia/bin"

# ---------------------------------------------------------------- 1. read the host
say "== [install] reading the host =="
[ -r "$OS_RELEASE" ] || unmeasurable "$OS_RELEASE is not readable: the distribution cannot be identified."
[ -r "$MEMINFO" ]    || unmeasurable "$MEMINFO is not readable: the memory cannot be measured."
OS_ID="$(sed -n 's/^ID=//p' "$OS_RELEASE" | tr -d '"' | head -1)"
OS_LIKE="$(sed -n 's/^ID_LIKE=//p' "$OS_RELEASE" | tr -d '"' | head -1)"
HIVE=0; [ -d "$HIVE_DIR" ] && HIVE=1
APT=0
case " $OS_ID $OS_LIKE " in *" ubuntu "*|*" debian "*) APT=1;; esac
# Docker's repository is keyed on the BASE distribution, and its suite on the base's codename: on a
# derivative (Mint, Pop, HiveOS) VERSION_CODENAME names the derivative, UBUNTU_CODENAME names the
# Ubuntu underneath. Docker's own instructions read UBUNTU_CODENAME first for exactly that reason.
DOCKER_DISTRO=ubuntu
case "$OS_ID" in debian) DOCKER_DISTRO=debian;; esac
case " $OS_LIKE " in *" debian "*) [ "$OS_ID" = ubuntu ] || DOCKER_DISTRO=debian;; esac
case " $OS_LIKE " in *" ubuntu "*) DOCKER_DISTRO=ubuntu;; esac
if [ "$DOCKER_DISTRO" = ubuntu ]; then
  OS_CODENAME="$(sed -n 's/^UBUNTU_CODENAME=//p' "$OS_RELEASE" | tr -d '"' | head -1)"
  [ -n "$OS_CODENAME" ] || OS_CODENAME="$(sed -n 's/^VERSION_CODENAME=//p' "$OS_RELEASE" | tr -d '"' | head -1)"
else
  OS_CODENAME="$(sed -n 's/^VERSION_CODENAME=//p' "$OS_RELEASE" | tr -d '"' | head -1)"
fi
# WSL comes in two generations and only the second runs Docker: a WSL 1 kernel string names
# Microsoft without WSL2, and nothing this file could install would make a daemon start there.
WSL=0; grep -qi microsoft "$PROC_VERSION" 2>/dev/null && WSL=1
if [ "$WSL" = 1 ] && ! grep -q 'WSL2' "$PROC_VERSION" 2>/dev/null; then
  refuse "this distribution runs under WSL 1, where Docker cannot run. From Windows: wsl --set-version <distribution> 2, then run this file again."
  exit 2
fi

RAM_MB="$(awk '/^MemTotal:/ {printf "%d", $2/1024}' "$MEMINFO")"
[ -n "$RAM_MB" ] || unmeasurable "MemTotal not found in $MEMINFO."

ROOT_USER=0; [ "$(id -u)" = 0 ] && ROOT_USER=1
SUDO=""; [ "$ROOT_USER" = 1 ] || SUDO=sudo
if [ "$ROOT_USER" = 0 ] && ! command -v sudo >/dev/null 2>&1; then
  refuse "not root and no sudo: this file installs packages and cannot do it from here."
  exit 2
fi
IN_GROUP=0; id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker && IN_GROUP=1
[ "$ROOT_USER" = 1 ] && IN_GROUP=1

# Docker, read as THREE separate facts, because folding them into one word cost an Engine. Whether a
# CLI is installed decides what may be installed: an installed Engine is never replaced (docker-ce
# declares Conflicts: docker.io, so installing it over docker.io removes docker.io and stops every
# container it ran). Whether Compose v2 is there decides what is added. Whether the DAEMON answers
# is a third fact, and it is read with the rights that can read it -- a user not yet in the docker
# group gets "permission denied" from a perfectly running daemon, which is not "Docker missing".
dk(){ # dk <docker args...> : the docker CLI, through sudo when this user cannot read the daemon
  if [ "$IN_GROUP" = 1 ]; then docker "$@"; else $SUDO -n docker "$@" 2>/dev/null || docker "$@"; fi
}
DOCKER=missing; COMPOSE=absent; DAEMON=n/a; DOCKER_IO=0
if command -v docker >/dev/null 2>&1; then
  DOCKER=present
  docker compose version >/dev/null 2>&1 && COMPOSE=present
  [ "$COMPOSE" = present ] || DOCKER=present-without-compose
  if dk info >/dev/null 2>&1; then DAEMON=up; else DAEMON=down-or-unreadable; fi
  dpkg -s docker.io >/dev/null 2>&1 && DOCKER_IO=1
fi
RUNNING="?"
if [ "$DAEMON" = up ]; then
  if _ps="$(dk ps -q 2>/dev/null)"; then RUNNING="$(printf '%s\n' "$_ps" | grep -c . || true)"; RUNNING="${RUNNING:-0}"; fi
fi

# Free disk. Under WSL the root filesystem is a sparse virtual disk that reports its own maximum
# size (a terabyte by default), not the space left on the Windows drive that holds it; the reading
# that matters there is the host drive's. When neither figure can be read, the floor cannot be
# judged and this file says so instead of skipping the check.
DOCKER_ROOT=/var/lib/docker
if [ "$DAEMON" = up ]; then
  _dr="$(dk info --format '{{.DockerRootDir}}' 2>/dev/null)"; [ -n "$_dr" ] && DOCKER_ROOT="$_dr"
fi
if [ "$WSL" = 1 ]; then
  _disk_path="$WSL_HOST_MOUNT"; DISK_NOTE="(Windows drive under $WSL_HOST_MOUNT)"
  [ -d "$_disk_path" ] || unmeasurable "under WSL the free space is the Windows drive's, and $WSL_HOST_MOUNT is not mounted: it cannot be measured from here."
else
  _disk_path="$DOCKER_ROOT"; [ -d "$_disk_path" ] || _disk_path="/"; DISK_NOTE=""
fi
DISK_GB="$(df -BG --output=avail "$_disk_path" 2>/dev/null | tail -1 | tr -dc '0-9')"
[ -n "$DISK_GB" ] || unmeasurable "free disk under $_disk_path could not be measured (df gave no figure)."

GPU=none
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU="$(nvidia-smi -L 2>/dev/null | head -1 | sed -E 's/^GPU [0-9]+: //; s/ \(UUID.*//')"
  [ -n "$GPU" ] || GPU=present
fi
# How many cards: counted by lines (awk), never with grep -c, which prints 0 AND fails. `?` when nvidia-smi
# answered the first question and not this one -- never a count of 0. Its output is captured WITH its exit
# status first: in a pipeline the status is awk's, and awk counts an empty answer as 0.
GPU_N=0
if [ "$GPU" != none ]; then
  _gpu_l="$(nvidia-smi -L 2>/dev/null)" \
    && GPU_N="$(printf '%s\n' "$_gpu_l" | awk '/^GPU [0-9]+:/ { n++ } END { print n + 0 }')" \
    || GPU_N="?"
fi
TOOLKIT=n/a
if [ "$GPU" != none ]; then
  TOOLKIT=absent
  # Configured is what can be read from the host without pulling an image; whether the card is
  # actually visible from a container is join.sh's test, made with the image it needs anyway. A
  # daemon that cannot be asked leaves the answer unknown, not "absent".
  if [ "$DAEMON" = up ]; then
    dk info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"' && TOOLKIT=present
  elif [ "$DOCKER" != missing ]; then
    TOOLKIT="?"
  fi
fi

# Tools this file and join.sh call directly. They used to be installed only alongside Docker, so a
# host that already had Docker and no git failed at the clone with a message the plan never announced.
NEED_TOOLS=""
command -v git  >/dev/null 2>&1 || NEED_TOOLS="$NEED_TOOLS git"
command -v curl >/dev/null 2>&1 || NEED_TOOLS="$NEED_TOOLS curl"
[ "$TOOLKIT" != present ] && [ "$GPU" != none ] && ! command -v gpg >/dev/null 2>&1 && NEED_TOOLS="$NEED_TOOLS gnupg"

# The clone. A directory that carries .git but cannot be inspected because git is not installed is
# neither "present" nor "missing": nothing may be reset in it until it has been read.
CLONE=missing; DIRTY=""
# The copy of the miner image pin this file wrote (--miner-image-file), kept where git never looks.
PKG_PIN_MARK="$DENDRA_DIR/.git/dendra-package-miner-image"; PKG_PIN_OURS=0
# pkg_pin_is_ours -> 0 when docker/MINER_IMAGE holds EXACTLY the bytes this file wrote there (raw bytes: no
# filter, no line-ending conversion). Asked only of a clone whose ONLY change is that file.
pkg_pin_is_ours(){
  local a b
  [ -f "$PKG_PIN_MARK" ] || return 1
  a="$(git -C "$DENDRA_DIR" hash-object --no-filters "$DENDRA_DIR/docker/MINER_IMAGE" 2>/dev/null)"
  b="$(git -C "$DENDRA_DIR" hash-object --no-filters "$PKG_PIN_MARK" 2>/dev/null)"
  [ -n "$a" ] && [ "$a" = "$b" ]
}
if [ -d "$DENDRA_DIR/.git" ]; then
  if command -v git >/dev/null 2>&1; then
    CLONE=present
    DIRTY="$(git -C "$DENDRA_DIR" status --porcelain --untracked-files=no 2>/dev/null)"
    # THE ONE EXEMPTION, AND IT NAMES ITS BYTES: the pin this file wrote, alone, byte for byte. A family of
    # changes ("anything in docker/MINER_IMAGE") would let a hand edit, or a pin of another release left in
    # place, through -- and it would then be undone, or carried to another tag, without a word.
    if [ -n "$DIRTY" ]; then
      if [ "$DIRTY" = " M docker/MINER_IMAGE" ] && pkg_pin_is_ours; then PKG_PIN_OURS=1; else CLONE=dirty; fi
    fi
    git -C "$DENDRA_DIR" ls-files --error-unmatch deploy/join.sh >/dev/null 2>&1 || CLONE=foreign
  else
    CLONE=unreadable
  fi
elif [ -e "$DENDRA_DIR" ]; then
  CLONE=occupied
fi

say "  distribution : ${OS_ID:-?} ${OS_CODENAME:-} $( [ "$HIVE" = 1 ] && printf '(HiveOS)' )$( [ "$WSL" = 1 ] && printf '(WSL 2)' )"
say "  RAM          : ${RAM_MB} MB"
say "  free disk    : ${DISK_GB} GB under ${_disk_path} ${DISK_NOTE}"
say "  GPU          : $GPU"
[ "$GPU" != none ] && say "  GPUs         : $GPU_N NVIDIA card(s) ($( [ -n "$GPUS" ] && printf 'one miner identity per card selected by --gpus %s' "$GPUS" || printf 'one miner identity unless --gpus all' ))"
say "  docker       : $DOCKER$( [ "$DOCKER" != missing ] && printf ' (daemon %s, %s running container(s)%s)' "$DAEMON" "$RUNNING" "$( [ "$RUNNING" = '?' ] && printf ' -- not readable from this account yet' )" )"
say "  nvidia toolkit: $TOOLKIT"
say "  tools missing: ${NEED_TOOLS:-none}"
say "  clone        : $CLONE ($DENDRA_DIR)$( [ "$PKG_PIN_OURS" = 1 ] && printf ', carrying the miner image pin this installer wrote into docker/MINER_IMAGE' )"

# ---------------------------------------------------------------- 2. what must change, and refusals
# THE ROLE OF THIS HOST ON THE TESTNET, asked BEFORE anything changes, of the probe NEXT TO this file:
# deploy/hw_probe.sh --role, the one decision join.sh applies too (and through this file, install.ps1 and the HiveOS
# package). A validator serves no model and is not asked. Four answers, three of them readings:
#   miner    a usable NVIDIA card: unchanged, the role asked for applies;
#   judge    no usable card and at least MOE_CPU_MIN_RAM_MB of RAM: the judge role on the CPU, and --miner is
#            refused (no mining model runs on the CPU on the testnet);
#   refused  no usable card and less RAM: refused here, exit 2, with the probe's reason;
#   anything else -- unknown (the RAM unread), a missing probe, an answer that is not one of the words, a code
#            that does not go with it -- exit 3: nothing is installed on a guess.
HOST_ROLE="?"; HOST_ROLE_WHY=""
host_role(){
  local out rc w
  if [ ! -r "$HW_PROBE" ]; then HOST_ROLE_WHY="no hardware probe where it was looked for ($HW_PROBE)"; return 0; fi
  if command -v timeout >/dev/null 2>&1; then
    out="$(timeout 60 bash "$HW_PROBE" --role 2>/dev/null)"; rc=$?
  else
    out="$(bash "$HW_PROBE" --role 2>/dev/null)"; rc=$?
  fi
  w="$(printf '%s\n' "$out" | head -1 | tr -d '[:space:]')"
  HOST_ROLE_WHY="$(printf '%s\n' "$out" | tail -n +2)"
  case "$rc:$w" in
    0:miner|0:judge|0:refused) HOST_ROLE="$w" ;;
    3:unknown) HOST_ROLE=unknown ;;
    *) HOST_ROLE_WHY="the probe gave no reading (exit $rc, answer '${w:-<empty>}')" ;;
  esac
  return 0
}
# apply_role <what has changed so far> -- the decision, applied: it returns on a role, exits 2 when the host has
# none (or --miner without a usable card), exits 3 when it cannot be decided. Asked once: here of the probe next
# to this file, or after the clone of the clone's probe (PROBE_DEFERRED), where ROLE_STEP=after-clone records the
# word of that action (the clone is a change, so the run is no longer one that stopped before any).
ROLE_STEP=before
role_word(){ [ "$ROLE_STEP" = after-clone ] && resume "$CLONE" "$1"; return 0; }
apply_role(){
  host_role
  case "$HOST_ROLE" in
    miner) say "  role         : $ROLE (deploy/hw_probe.sh --role: miner, a usable NVIDIA card)" ;;
    judge)
      if [ "$ROLE" = miner ]; then
        refuse "--miner on a host with no usable NVIDIA GPU: the testnet runs no mining model on the CPU (deploy/hw_probe.sh --role: judge)."
        printf '%s\n' "$HOST_ROLE_WHY" | sed 's/^/           /' >&2
        say "         Run this file again WITHOUT --miner: this host then joins as a judge on the CPU. $1"
        [ "$HIVE" = 1 ] && say "         On a HiveOS rig: ROLE=judge in the flight sheet's Extra config, then restart the miner."
        role_word refused
        exit 2
      fi
      say "  role         : judge on the CPU (deploy/hw_probe.sh --role: judge)"
      printf '%s\n' "$HOST_ROLE_WHY" | sed 's/^/                 /' ;;
    refused)
      refuse "this host has no role on the testnet (deploy/hw_probe.sh --role: refused). $1"
      printf '%s\n' "$HOST_ROLE_WHY" | sed 's/^/           /' >&2
      role_word refused
      exit 2 ;;
    *)
      printf '%s\n' "${HOST_ROLE_WHY:-the probe gave no reason}" | sed 's/^/           /' >&2
      role_word role_unmeasured
      unmeasurable "whether this host can join could not be decided (deploy/hw_probe.sh --role: ${HOST_ROLE}): an unknown is never a pass. $1" ;;
  esac
}
if [ "$ROLE" != validator ]; then
  if [ "$PROBE_DEFERRED" = 1 ]; then
    say "  role         : NOT DECIDED YET -- no deploy/hw_probe.sh next to this file (it was downloaded alone). The"
    say "                 clone's probe decides it (--role) once the clone is placed, BEFORE Docker, the NVIDIA toolkit or"
    say "                 the docker group are touched: a host with no role is refused there, with only the clone placed."
  else
    apply_role "Nothing was changed."
  fi
fi
PLAN=""
add(){ PLAN="${PLAN}  - $1
"; }

# THE JUDGE FLOOR IS FOR A HOST THAT CAN JUDGE. The judge role is the default, and it used to raise the
# disk floor on every host -- including one whose RAM can never clear the judge gate, where join.sh
# would demote it to the miner role anyway and the judge model would never be pulled. Such a host was
# refused for a model it was never going to download. So when free disk sits between the two floors,
# the probe next to this file is asked, and it is the probe's gate that decides (MOE_CPU_MIN_RAM_MB in
# deploy/hw_probe.sh: the figure is read there, never restated here).
# THREE ANSWERS, NEVER TWO. `false` is a reading: the miner floor applies. `true` keeps the judge floor.
# Anything else -- `unknown` (the probe could not read the RAM), a missing probe, an answer that is
# none of the three words, a failed run -- decides nothing, so the judge floor stays: an unknown is
# never read as "cannot judge", which would lower a floor on a guess.
JUDGE_PROBE="?"; JUDGE_PROBE_WHY=""
judge_probe(){
  local out rc
  if [ ! -r "$HW_PROBE" ]; then
    JUDGE_PROBE_WHY="no hardware probe next to this file ($HW_PROBE)$( [ "$PROBE_DEFERRED" = 1 ] && printf ': it was downloaded alone, and an unread gate never lowers a floor' )"; return 0
  fi
  if command -v timeout >/dev/null 2>&1; then
    out="$(timeout 60 bash "$HW_PROBE" --can-judge 2>/dev/null)"; rc=$?
  else
    out="$(bash "$HW_PROBE" --can-judge 2>/dev/null)"; rc=$?
  fi
  out="$(printf '%s\n' "$out" | head -1 | tr -d '[:space:]')"
  case "$rc:$out" in
    0:true)  JUDGE_PROBE=true ;;
    0:false) JUDGE_PROBE=false ;;
    3:unknown) JUDGE_PROBE_WHY="the probe could not read this host's RAM" ;;
    *) JUDGE_PROBE_WHY="the probe gave no reading (exit $rc, answer '${out:-<empty>}')" ;;
  esac
  return 0
}
DISK_FLOOR="$MIN_DISK_GB"
if [ "$ROLE" = judge ]; then
  DISK_FLOOR="$MIN_DISK_GB_JUDGE"
  if [ "$DISK_GB" -lt "$MIN_DISK_GB_JUDGE" ] 2>/dev/null; then
    judge_probe
    if [ "$JUDGE_PROBE" = false ]; then
      DISK_FLOOR="$MIN_DISK_GB"
      say "  [i] judge role asked, but deploy/hw_probe.sh --can-judge answered false: this host (RAM_MB=${RAM_MB} read"
      say "      here) is below the judge gate, MOE_CPU_MIN_RAM_MB in deploy/hw_probe.sh. The judge model will not"
      say "      be pulled, so the miner floor applies (${MIN_DISK_GB} GB, DENDRA_MIN_DISK_GB); join.sh keeps the miner role."
    fi
  fi
fi
if [ "$DISK_GB" -lt "$DISK_FLOOR" ] 2>/dev/null; then
  refuse "free disk ${DISK_GB} GB under ${_disk_path} is below the floor of ${DISK_FLOOR} GB for the $ROLE role (DENDRA_MIN_DISK_GB / DENDRA_MIN_DISK_GB_JUDGE)."
  say "         The models, the images and, when an image is built from the clone, its build cache do not fit;"
  say "         the failure would surface an hour later inside a pull or build log. Free space, or point"
  say "         Docker's data root at a larger disk."
  if [ "$ROLE" = judge ]; then
    case "$JUDGE_PROBE" in
      true) say "         deploy/hw_probe.sh --can-judge answered true: this host clears the judge gate, so the judge model would be pulled." ;;
      *)    say "         The judge floor stays because nothing measured that this host cannot judge: ${JUDGE_PROBE_WHY:-no probe answer}." ;;
    esac
    # --miner is a way out ONLY for a host that can mine: on one without a usable card it is refused (the role
    # above), so naming it there sent the operator to a second refusal.
    case "$HOST_ROLE:$GPU" in
      miner:*) say "         --miner has a lower floor (${MIN_DISK_GB} GB): it does not pull the judge model." ;;
      judge:*) say "         --miner is no way out here: this host has no usable NVIDIA card, so the judge on the CPU is its only role, and the judge model is what it pulls." ;;
      *:none)  say "         --miner is no way out here: no NVIDIA card was read on this host, and without one the testnet runs no mining model." ;;
      *)       say "         --miner has a lower floor (${MIN_DISK_GB} GB) on a host whose NVIDIA card Docker can use: it does not pull the judge model." ;;
    esac
  fi
  [ "$HIVE" = 1 ] && say "         On a HiveOS rig the system flash is usually the constraint: add a drive for Docker."
  exit 2
fi
if [ "$RAM_MB" -lt "$MIN_RAM_MB" ]; then
  refuse "RAM ${RAM_MB} MB is below the floor of ${MIN_RAM_MB} MB (DENDRA_MIN_RAM_MB): an image built from the clone compiles the chain binary, which is what happens unless the clone pins a prebuilt image for this platform (docker/NODE_IMAGE, docker/MINER_IMAGE) and its pull succeeds."
  [ "$WSL" = 1 ] && say "         Under WSL the VM gets about half of the PC's memory unless %UserProfile%\\.wslconfig sets [wsl2] memory=."
  exit 2
fi
[ "$RAM_MB" -lt "$WARN_RAM_MB" ] && warn "RAM ${RAM_MB} MB: a build from the clone (there is none when the clone pins a prebuilt image for this platform and its pull succeeds) may be slow or fail on a rig this size; a second attempt resumes from the cache."
if [ "$ROLE" = judge ] && [ "$HOST_ROLE" = miner ]; then
  say "  [i] the judge role is decided at join time by deploy/hw_probe.sh: the judge runs on the CPU (MOE_CPU_MIN_RAM_MB);"
  say "      this host reads RAM_MB=${RAM_MB}. If the probe declines, join.sh keeps the miner role, on the card."
fi

[ "$PROBE_DEFERRED" = 1 ] && [ "$ROLE" != validator ] && add "IN THIS ORDER, since no probe sits next to this file: the tools git needs (when listed below), then the clone, then the ROLE from the clone's deploy/hw_probe.sh --role -- a host with no role stops there (exit 2, or 3 when it cannot be decided), with the clone placed -- and only then Docker, the NVIDIA toolkit and the docker group"
NEED_DOCKER_REPO=0; COMPOSE_PKG=""
case "$DOCKER" in
  missing)
    if [ "$APT" = 1 ]; then
      [ -n "$OS_CODENAME" ] || { refuse "the distribution codename could not be read from $OS_RELEASE, and Docker's repository needs it."; exit 2; }
      NEED_DOCKER_REPO=1
      add "install Docker Engine + Compose v2 from https://download.docker.com/linux/${DOCKER_DISTRO} (${OS_CODENAME})"
    else
      refuse "Docker is missing and ${OS_ID:-this distribution} is not apt-based: install it by hand first -> https://docs.docker.com/engine/install/"
      exit 2
    fi;;
  present-without-compose)
    if [ "$APT" = 1 ]; then
      # The package that fits the Engine that is THERE: Ubuntu's docker-compose-v2 depends on
      # docker.io, so on a docker-ce Engine it would drag docker.io in and collide. Choose by the
      # installed Engine, never by which package happens to be available.
      if [ "$DOCKER_IO" = 1 ] && apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        COMPOSE_PKG=docker-compose-v2
        add "install Compose v2 as the distribution's docker-compose-v2 package (the installed docker.io Engine is kept)"
      else
        [ -n "$OS_CODENAME" ] || { refuse "the distribution codename could not be read from $OS_RELEASE, and Docker's repository needs it."; exit 2; }
        NEED_DOCKER_REPO=1; COMPOSE_PKG=docker-compose-plugin
        add "install Compose v2 as docker-compose-plugin from https://download.docker.com/linux/${DOCKER_DISTRO} (the installed Engine is kept)"
      fi
    else
      refuse "Docker is installed without Compose v2, and ${OS_ID:-this distribution} is not apt-based: install the compose plugin by hand -> https://docs.docker.com/compose/install/"
      exit 2
    fi;;
esac
[ "$DAEMON" = down-or-unreadable ] && add "start the Docker daemon if it is not running (systemctl enable --now docker, or service docker start) -- it may simply be unreadable from this account until the group applies"
if [ -n "$NEED_TOOLS" ]; then
  if [ "$APT" = 1 ]; then add "install missing tools:${NEED_TOOLS}"
  else refuse "these tools are missing and ${OS_ID:-this distribution} is not apt-based:${NEED_TOOLS}"; exit 2; fi
fi
if [ "$TOOLKIT" = absent ] || [ "$TOOLKIT" = "?" ]; then
  if [ "$APT" = 1 ]; then
    _rr="the running container(s) stop and restart"
    [ "$RUNNING" != "?" ] && _rr="the $RUNNING running container(s) stop and restart"
    add "install nvidia-container-toolkit from https://nvidia.github.io/libnvidia-container if the daemon has no nvidia runtime, register it, then RESTART the Docker daemon -- $_rr"
  else
    warn "GPU present, toolkit not confirmed, no apt: install it by hand -> https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html (without it Docker cannot use the card, and join.sh decides as for a host without one: deploy/hw_probe.sh --role --no-gpu, a judge on the CPU or refused)"
  fi
fi
[ "$IN_GROUP" = 0 ] && add "add $(id -un) to the docker group (usermod -aG docker: membership of that group is equivalent to root on this host)"
case "$CLONE" in
  missing)    if [ -n "$REF" ]; then add "clone the release tag $REF of $DENDRA_REPO_URL into $DENDRA_DIR (never main)"
              else add "clone $DENDRA_REPO_URL into $DENDRA_DIR"; fi;;
  present)    if [ -n "$REF" ]; then add "place the clone in $DENDRA_DIR on the release tag $REF (refused if it carries local commits, if the tag moved, or if the tag's tree would overwrite files)"
              else add "fast-forward the clone in $DENDRA_DIR to the published tree (refused if it carries local commits or files the new tree would overwrite)"; fi;;
  dirty)      refuse "the clone in $DENDRA_DIR has local changes to tracked files; a fast-forward would discard them."
              printf '%s\n' "$DIRTY" | head -5 | sed 's/^/           /'
              say "         This file only fast-forwards a clone that carries no local work -- a commit here would be"
              say "         discarded too. Move your changes to another clone, or point DENDRA_DIR elsewhere."
              exit 2;;
  unreadable) refuse "$DENDRA_DIR is a git clone but git is not installed, so it cannot be inspected before a fast-forward. Install git first (apt-get install git), or point DENDRA_DIR elsewhere."; exit 2;;
  foreign)    refuse "$DENDRA_DIR is a git clone that does not carry deploy/join.sh: not this repository. Point DENDRA_DIR elsewhere."; exit 2;;
  occupied)   refuse "$DENDRA_DIR exists and is not a git clone. Point DENDRA_DIR elsewhere."; exit 2;;
esac
[ "$PKG_PIN_OURS" = 1 ] && add "undo the miner image pin this installer wrote into docker/MINER_IMAGE (the clone's only local change, byte for byte what it wrote) before the clone moves"
case "$PKG_PIN_STATE" in
  ok)       add "once the clone sits on $REF: write the miner image of $REF the package carries ($PKG_PIN_FILE) into the clone's docker/MINER_IMAGE, if the tag names none (never over a value); join.sh then judges it -- official digest, built-for gates, platform -- or builds the image from the clone";;
  unusable) warn "$PKG_PIN_WHY: nothing will be written, and join.sh builds the miner image from the clone.";;
esac
JOIN_ARGS=""
case "$ROLE" in judge) JOIN_ARGS="--judge";; miner) JOIN_ARGS="--miner";; validator) JOIN_ARGS="--validator";; esac
[ "$LIGHT" = 1 ] && [ "$ROLE" != validator ] && JOIN_ARGS="$JOIN_ARGS --remote-rpc"
# Their characters were checked above (letters and digits only): they can sit in the command string below.
[ -n "$PAYOUT" ] && JOIN_ARGS="$JOIN_ARGS --payout-address $PAYOUT"
[ -n "$OWNER" ] && JOIN_ARGS="$JOIN_ARGS --owner $OWNER"
# Validated above (GPUS_SPEC_RE): letters, digits, commas and dashes only.
[ -n "$GPUS" ] && JOIN_ARGS="$JOIN_ARGS --gpus $GPUS"
add "run: CONFIG_URL=$CONFIG_URL bash deploy/join.sh $JOIN_ARGS   (from $DENDRA_DIR)"
if [ -n "$GPUS" ]; then
  add "join.sh --gpus $GPUS: ONE MINER IDENTITY PER CARD -- each takes one faucet drip to register (the faucet caps drips per IP and per day: DENDRA_FAUCET_IP_DAILY; a refused identity retries by itself), locks its own stake (min_stake, read on chain), and has its own 24-word recovery phrase to write down. It needs a payout address (--payout-address, or one already in the kit). The free disk floor above is for ONE model: cards of different sizes take one model each. Its own plan, read on this host, writing nothing: bash deploy/join.sh --gpus $GPUS --plan"
fi
# What join.sh schedules is part of what --yes accepts, so it is in the plan rather than discovered later.
[ "$ROLE" != validator ] && add "join.sh then adds one hourly line to this user's crontab: the miner's self-test (deploy/testnet-miner/miner_health.sh --cron, read only: it writes nothing on the relay), whose file deploy/testnet-miner/miner-health.ALERT exists while a check fails (an alert) or could not be measured (a note)"
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  add "add the Dendra application to this user's menu (three files under \$HOME, no package)"
fi

say ""
say "== [install] plan =="
printf '%s' "$PLAN"
MISSING=0
[ "$DOCKER" != present ] && MISSING=$((MISSING+1))
[ "$DAEMON" = down-or-unreadable ] && MISSING=$((MISSING+1))
[ -n "$NEED_TOOLS" ] && MISSING=$((MISSING+1))
{ [ "$TOOLKIT" = absent ] || [ "$TOOLKIT" = "?" ]; } && [ "$APT" = 1 ] && MISSING=$((MISSING+1))
[ "$IN_GROUP" = 0 ] && MISSING=$((MISSING+1))
[ "$CLONE" = missing ] && MISSING=$((MISSING+1))

resume(){ # resume <clone_state> <action>
  printf 'DENDRA_INSTALL_RESUME distro=%s codename=%s hive=%s wsl=%s docker=%s daemon=%s toolkit=%s gpu=%s disk_gb=%s ram_mb=%s clone=%s action=%s\n' \
    "${OS_ID:-?}" "${OS_CODENAME:-?}" "$HIVE" "$WSL" "$DOCKER" "$DAEMON" "$TOOLKIT" "$(printf '%s' "$GPU" | tr ' ' '_')" "$DISK_GB" "$RAM_MB" "$1" "$2"
  # THE ACTION, for a caller that cannot read this output (the HiveOS package shows it on screen): one word in
  # DENDRA_INSTALL_RESULT_FILE, written whole (renamed into place). Its exit code alone does not tell 3 apart:
  # "nothing decided, nothing installed" and "join.sh started the miner and could not measure its health"
  # (action=join_unmeasured). A run that stops before any action (an early refusal) writes nothing.
  if [ -n "${DENDRA_INSTALL_RESULT_FILE:-}" ]; then
    printf '%s\n' "$2" > "$DENDRA_INSTALL_RESULT_FILE.tmp" 2>/dev/null \
      && mv -f "$DENDRA_INSTALL_RESULT_FILE.tmp" "$DENDRA_INSTALL_RESULT_FILE" 2>/dev/null
  fi
  return 0
}

if [ "$YES" != 1 ]; then
  if [ "$MISSING" = 0 ]; then
    say "  nothing to install on this host. Re-run with --yes to fast-forward the clone and start join.sh."
    resume "$CLONE" planned
    exit 0
  fi
  say ""
  say "  $MISSING change(s) needed. Nothing was done. Re-run with --yes to apply the plan above."
  resume "$CLONE" planned
  exit 2
fi
[ "$CHECK" = 1 ] && { say "  --check with --yes: the check wins, nothing is done."; resume "$CLONE" planned; exit 0; }

# ---------------------------------------------------------------- 3. apply
step(){ printf '\n== [install] %s ==\n' "$*"; }
fail(){ printf '  [FAILED] %s\n' "$*" >&2; resume "$CLONE" failed; exit 1; }
# systemd is detected the way systemd itself recommends (sd_booted: the directory it creates at
# boot), not by `is-system-running`, whose exit code is non-zero for `degraded` -- a running systemd
# with one failed unit, the usual state under WSL and on rigs.
has_systemd(){ [ -d "$SYSTEMD_DIR" ]; }
daemon_start(){
  if has_systemd; then $SUDO systemctl enable --now docker; else $SUDO service docker start; fi
}
daemon_restart(){
  if has_systemd; then $SUDO systemctl restart docker; else $SUDO service docker restart; fi
}

# A repository file that is written and whose first `apt-get update` then fails is removed again:
# left in place it makes every later `apt-get update` on the host fail, long after this file is gone.
add_docker_repo(){
  $SUDO install -m 0755 -d "$SYSROOT/etc/apt/keyrings" || fail "mkdir /etc/apt/keyrings"
  $SUDO curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" -o "$SYSROOT/etc/apt/keyrings/docker.asc" || fail "download of Docker's signing key"
  $SUDO chmod a+r "$SYSROOT/etc/apt/keyrings/docker.asc"
  ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$ARCH" "$DOCKER_DISTRO" "$OS_CODENAME" | $SUDO tee "$SYSROOT/etc/apt/sources.list.d/docker.list" >/dev/null || fail "writing docker.list"
  if ! $SUDO apt-get update -qq; then
    $SUDO rm -f "$SYSROOT/etc/apt/sources.list.d/docker.list"
    fail "apt-get update with Docker's repository (the repository file was removed again)"
  fi
}

# PLACING THE CLONE, as one function: called at its usual place (after Docker, the toolkit and the group), or
# right after the tools when the role is DEFERRED to the clone's probe (PROBE_DEFERRED, see the header). Its body
# keeps the indentation it had at the top level, so that a bench which mutates one of its lines finds it unchanged.
place_clone(){
# THE CLONE ON A RELEASE TAG (--ref / DENDRA_REF). Never main, never a pull: a missing clone is cloned AT the
# tag, a present one is placed on it (fetching that tag only), and in both cases the result is CHECKED --
# HEAD must be the commit the tag names. `git fetch` refuses to move a tag this clone already holds, so a
# tag that changed upstream is a refusal, never a silent update. Local work is measured BEFORE anything
# moves, as for the fast-forward below: HEAD must be a published commit, the tip origin last served or a tag.
# >>> THE PIN THIS FILE WROTE IS UNDONE BEFORE THE CLONE MOVES (--miner-image-file, see the header). Left in
# place, a checkout to a tag whose docker/MINER_IMAGE is the same file would carry it along, and the new tree
# would pull the image of the OLD release. A copy left by a write that did not complete is removed as well.
if [ "$PKG_PIN_OURS" = 1 ]; then
  step "undoing the miner image pin this installer wrote into docker/MINER_IMAGE"
  git -C "$DENDRA_DIR" checkout -q -- docker/MINER_IMAGE || fail "git checkout -- docker/MINER_IMAGE (undoing the pin this installer wrote)"
  [ -z "$(git -C "$DENDRA_DIR" status --porcelain --untracked-files=no 2>/dev/null)" ] \
    || fail "the clone in $DENDRA_DIR still carries a local change after the pin this installer wrote was undone"
  rm -f "$PKG_PIN_MARK"
  say "  [OK] docker/MINER_IMAGE is the tree's own again"
elif [ -f "$PKG_PIN_MARK" ] && { [ "$CLONE" = present ] || [ "$CLONE" = missing ]; }; then
  rm -f "$PKG_PIN_MARK"
fi
# <<< THE PIN THIS FILE WROTE IS UNDONE
if [ -n "$REF" ] && { [ "$CLONE" = missing ] || [ "$CLONE" = present ]; }; then
  if [ "$CLONE" = missing ]; then
    step "cloning the release tag $REF"
    git clone --branch "$REF" "$DENDRA_REPO_URL" "$DENDRA_DIR" || fail "git clone --branch $REF $DENDRA_REPO_URL"
  else
    step "placing the clone on the release tag $REF"
    _base="$(git -C "$DENDRA_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
    [ -n "$_base" ] || _base=main
    _head="$(git -C "$DENDRA_DIR" rev-parse -q --verify HEAD 2>/dev/null)"
    _tip="$(git -C "$DENDRA_DIR" rev-parse -q --verify "refs/remotes/origin/$_base" 2>/dev/null)"
    if [ -z "$_head" ]; then
      refuse "the clone in $DENDRA_DIR has no readable HEAD: it cannot be told whether it carries local work. Clone afresh into another DENDRA_DIR."
      resume "$CLONE" refused; exit 2
    fi
    if [ "$_head" != "$_tip" ] && ! git -C "$DENDRA_DIR" describe --exact-match --tags HEAD >/dev/null 2>&1; then
      refuse "the clone in $DENDRA_DIR sits on a commit that is neither what origin/$_base last served nor a tag: it carries local work, and moving it to $REF would leave that work behind. Nothing was changed."
      resume "$CLONE" refused; exit 2
    fi
    if ! _fe="$(git -C "$DENDRA_DIR" fetch --quiet origin "refs/tags/$REF:refs/tags/$REF" 2>&1)"; then
      refuse "the tag $REF could not be fetched, or it differs from the $REF this clone already holds (a tag that moved upstream is not followed). Nothing was changed. git said:"
      printf '%s\n' "$_fe" | head -6 | sed 's/^/           /'
      resume "$CLONE" refused; exit 2
    fi
    if ! _co="$(git -C "$DENDRA_DIR" checkout -q --detach "refs/tags/$REF" 2>&1)"; then
      refuse "the tree of $REF would overwrite files in $DENDRA_DIR that git does not track. Nothing was changed. git said:"
      printf '%s\n' "$_co" | head -8 | sed 's/^/           /'
      resume "$CLONE" refused; exit 2
    fi
  fi
  _at="$(git -C "$DENDRA_DIR" rev-parse -q --verify HEAD 2>/dev/null)"
  _tag="$(git -C "$DENDRA_DIR" rev-parse -q --verify "refs/tags/$REF^{commit}" 2>/dev/null)"
  if [ -z "$_tag" ] || [ "$_at" != "$_tag" ]; then
    fail "the clone in $DENDRA_DIR is not on the commit of the tag $REF (HEAD ${_at:-unreadable}, tag ${_tag:-absent}): $REF is not a tag of $DENDRA_REPO_URL, or the checkout did not happen"
  fi
  say "  [OK] the clone sits on the release tag $REF ($_at)"
  CLONE=pinned
fi

# >>> THE MINER IMAGE A PACKAGE CARRIES IS WRITTEN ONLY HERE: after the clone is CHECKED on the tag, before
# join.sh, which judges it. Every way out of pkg_pin_write leaves either the tag's own file, or EXACTLY the
# bytes recorded in PKG_PIN_MARK -- the only local change the next run accepts. It never stops the install:
# without the pin, join.sh builds the image from the clone, the safe direction.
pkg_pin_undo(){ rm -f "${1:-}" "$PKG_PIN_MARK"; git -C "$DENDRA_DIR" checkout -q -- docker/MINER_IMAGE 2>/dev/null; }
pkg_pin_write(){
  local f="$DENDRA_DIR/docker/MINER_IMAGE" vals tmp
  if [ "$PKG_PIN_STATE" != ok ]; then
    warn "$PKG_PIN_WHY: nothing is written, and join.sh builds the miner image from the clone."; return 0
  fi
  if [ ! -f "$f" ]; then
    say "  [i] the tag carries no docker/MINER_IMAGE: the package's pin is not written, and join.sh builds the miner image from the clone."; return 0
  fi
  vals="$(grep -vE '^[[:space:]]*(#|$)' "$f" || true)"
  case "$vals" in
    ""|"DENDRA_MINER_IMAGE=") : ;;
    *) say "  [i] the tag's docker/MINER_IMAGE already names a value: the package's pin is never written over it (join.sh judges the tag's own)."; return 0 ;;
  esac
  if [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ]; then
    warn "the tag's docker/MINER_IMAGE does not end with a newline: the package's pin is not written, and join.sh builds the miner image from the clone."; return 0
  fi
  tmp="$(mktemp "$DENDRA_DIR/.git/dendra-pin.XXXXXX")" || { warn "no temporary file in $DENDRA_DIR/.git: the package's pin is not written, and join.sh builds the miner image from the clone."; return 0; }
  # The tag's file, without the empty value line it may carry and without a `# built-for:` line left there
  # with no value under it (join.sh wants exactly one), then the package's two lines.
  if ! { grep -vx -e 'DENDRA_MINER_IMAGE=' -e '# built-for: .*' "$f"; cat "$PKG_PIN_FILE"; } > "$tmp" || ! chmod 0644 "$tmp" \
     || ! cp "$tmp" "$PKG_PIN_MARK" || ! mv -f "$tmp" "$f"; then
    pkg_pin_undo "$tmp"
    warn "the package's pin could not be written into docker/MINER_IMAGE (the tag's own file is kept): join.sh builds the miner image from the clone."; return 0
  fi
  if ! pkg_pin_is_ours; then
    pkg_pin_undo
    warn "docker/MINER_IMAGE does not read back as what was written: the tag's own file is put back, and join.sh builds the miner image from the clone."; return 0
  fi
  say "  [OK] wrote the miner image of $REF the package carries into docker/MINER_IMAGE ($(sed -n 1p "$PKG_PIN_FILE" | sed 's/^# //')); join.sh judges it next"
}
if [ -n "$PKG_PIN_FILE" ] && [ "$CLONE" = pinned ]; then
  step "the miner image the package carries"
  pkg_pin_write
fi
# <<< THE MINER IMAGE A PACKAGE CARRIES

if [ "$CLONE" = missing ]; then
  step "cloning the repository"
  git clone "$DENDRA_REPO_URL" "$DENDRA_DIR" || fail "git clone $DENDRA_REPO_URL"
  CLONE=fresh
elif [ "$CLONE" = present ]; then
  step "fast-forwarding the clone"
  # The published tree is re-issued as a single root commit on every publication, so `git pull` finds
  # no common ancestor and refuses, and -- the trap -- once the new root has been fetched, the OLD
  # publication looks like a local commit. Local work is therefore measured BEFORE the fetch, against
  # the tip this clone last received: HEAD equal to it means no local commit. Then the branch is
  # moved with `checkout -B`, which, unlike `reset --hard`, refuses when an untracked file would be
  # overwritten by the new tree. Untracked files that keep their untracked paths -- the kit's .env
  # among them -- are not touched.
  BR="$(git -C "$DENDRA_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
  [ -n "$BR" ] || BR=main
  CUR="$(git -C "$DENDRA_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ -n "$CUR" ] && [ "$CUR" != HEAD ] && [ "$CUR" != "$BR" ]; then
    refuse "the clone in $DENDRA_DIR is on branch '$CUR', not '$BR': this file only fast-forwards '$BR'. Switch branches yourself, or point DENDRA_DIR elsewhere."
    resume "$CLONE" refused; exit 2
  fi
  if ! git -C "$DENDRA_DIR" rev-parse -q --verify "refs/remotes/origin/$BR" >/dev/null 2>&1; then
    refuse "the clone in $DENDRA_DIR has no record of what it last received from origin/$BR, so local commits cannot be told from published ones. Clone afresh into another DENDRA_DIR."
    resume "$CLONE" refused; exit 2
  fi
  AHEAD="$(git -C "$DENDRA_DIR" rev-list --count "refs/remotes/origin/$BR..HEAD" 2>/dev/null)"
  [ -n "$AHEAD" ] || fail "could not compare the clone with its last origin/$BR"
  # ⛔ "BEYOND WHAT IT RECEIVED" IS NOT "LOCAL" ONCE A `git pull` HAS FAILED. The pull FETCHES first: origin/$BR
  # then names the new publication's root, the merge refuses (unrelated histories), and HEAD stays on the OLD
  # publication -- which the count above calls one commit "beyond" origin, and which this file used to refuse
  # as local work, exit 2, telling the operator to move work that does not exist. An earlier publication is
  # recognised by what origin SERVED, read in the reflogs: refs/remotes/origin/$BR (every value a fetch gave it)
  # and refs/remotes/origin/HEAD. ⚠️ MEASURED (git 2.53): `git clone` writes NO reflog entry for
  # refs/remotes/origin/$BR -- the publication a clone started from is logged only under origin/HEAD and HEAD
  # ("clone: from ..."), which are read too. HEAD among them is published work.
  # ⛔ BUT A REFLOG EXPIRES (gc.reflogExpire, gc.reflogExpireUnreachable: an old publication is unreachable from
  # origin/$BR once the next one is fetched), and after a `git gc` the clone that failed its pull months ago
  # was refused as "1 local commit" again. So a SECOND sign, read from the commit itself: HEAD is a ROOT
  # commit, as every publication is (publish_release.sh issues one root commit per publication) and as a
  # commit of one's own is not -- it has a parent. The one local commit that is also a root, an amended
  # publication, is not lost either: the move below keeps the tree it leaves on the branch kit-before-update.
  # HEAD that neither sign recognises is refused as before.
  if [ "$AHEAD" != 0 ]; then
    _head="$(git -C "$DENDRA_DIR" rev-parse -q --verify HEAD 2>/dev/null)"
    _served="$(git -C "$DENDRA_DIR" log -g --format=%H "refs/remotes/origin/$BR" 2>/dev/null)"; _served_rc=$?
    _served="$_served
$(git -C "$DENDRA_DIR" log -g --format=%H refs/remotes/origin/HEAD 2>/dev/null)
$(git -C "$DENDRA_DIR" log -g --format='%H %gs' HEAD 2>/dev/null | sed -n 's/^\([0-9a-f]*\) clone: from .*/\1/p')"
    _pub=""
    if [ -n "$_head" ] && [ "$_served_rc" = 0 ] && printf '%s\n' "$_served" | grep -Fqx -- "$_head"; then
      _pub="origin served it before (its reflogs say so)"
    fi
    # `rev-list --parents` prints the commit, then its parents: the commit ALONE is a root.
    _parents="$(git -C "$DENDRA_DIR" rev-list --parents -n 1 HEAD 2>/dev/null)"
    if [ -z "$_pub" ] && [ -n "$_head" ] && [ "$_parents" = "$_head" ]; then
      _pub="it is a root commit, as every publication is (its reflog entries may have expired)"
    fi
    if [ -n "$_pub" ]; then
      say "  [i] HEAD ($_head) is an EARLIER PUBLICATION, not a local commit: $_pub."
      say "      A failed 'git pull' leaves a clone exactly here: each release republishes the repository as a NEW root"
      say "      commit, so git pull refuses ('refusing to merge unrelated histories') and the tree stays old. The"
      say "      clone is moved the way the kit's update instruction says: git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main"
    else
      # Counted WITHOUT a root: on top of an earlier publication (a failed pull), that root is not local work.
      _local="$(git -C "$DENDRA_DIR" rev-list --count --min-parents=1 "refs/remotes/origin/$BR..HEAD" 2>/dev/null)"
      refuse "the clone in $DENDRA_DIR carries ${_local:-$AHEAD} local commit(s) beyond what it received from origin/$BR; a fast-forward would discard them."
      git -C "$DENDRA_DIR" log --oneline "refs/remotes/origin/$BR..HEAD" 2>/dev/null | head -5 | sed 's/^/           /'
      [ "$_served_rc" = 0 ] || say "         (the reflog of origin/$BR could not be read, so an earlier publication cannot be told from local work.)"
      say "         Move that work to another clone, or point DENDRA_DIR elsewhere. Nothing was changed."
      resume "$CLONE" refused; exit 2
    fi
  fi
  git -C "$DENDRA_DIR" fetch --quiet origin "$BR" || fail "git fetch origin $BR"
  # The tree the clone leaves is kept on kit-before-update, as the instruction does (see the header).
  if ! _kb="$(git -C "$DENDRA_DIR" branch -f kit-before-update HEAD 2>&1)"; then
    refuse "the tree of the clone in $DENDRA_DIR could not be kept on the branch kit-before-update, so it is not moved (only origin/$BR was fetched). git said:"
    printf '%s\n' "$_kb" | head -4 | sed 's/^/           /'
    resume "$CLONE" refused; exit 2
  fi
  if ! _co="$(git -C "$DENDRA_DIR" checkout -q -B "$BR" "origin/$BR" 2>&1)"; then
    refuse "the published tree would overwrite files in $DENDRA_DIR that git does not track. Nothing was changed. git said:"
    printf '%s\n' "$_co" | head -8 | sed 's/^/           /'
    resume "$CLONE" refused; exit 2
  fi
  CLONE=updated
fi
[ -r "$DENDRA_DIR/deploy/join.sh" ] || fail "$DENDRA_DIR/deploy/join.sh is not there: this is not the repository this file expects"
CLONE_PLACED=1
}
CLONE_PLACED=0

TOOLS_DONE=0
if [ -n "$NEED_TOOLS" ] || [ "$DOCKER" != present ]; then
  step "installing tools"
  $SUDO apt-get update -qq || fail "apt-get update"
  $SUDO apt-get install -y -qq ca-certificates curl git ${NEED_TOOLS} || fail "apt-get install ca-certificates curl git${NEED_TOOLS}"
  TOOLS_DONE=1
fi

# THE ROLE, DEFERRED (no probe next to this file, see the header): the clone first, then ITS probe, and nothing
# below happens on a host that has no role. The clone is placed and checked exactly as it is further down -- it is
# the same function -- so its probe is this repository's, at the tree join.sh will run.
if [ "$PROBE_DEFERRED" = 1 ] && [ "$ROLE" != validator ]; then
  place_clone
  step "the role of this host (deploy/hw_probe.sh --role, from the clone)"
  HW_PROBE="$DENDRA_DIR/deploy/hw_probe.sh"
  ROLE_STEP=after-clone
  apply_role "Only the clone in $DENDRA_DIR was placed$( [ "$TOOLS_DONE" = 1 ] && printf ' (and the tools it needs installed)' ): Docker, the NVIDIA toolkit and the docker group were not touched."
fi

if [ "$DOCKER" = missing ]; then
  step "installing Docker Engine + Compose v2"
  add_docker_repo
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || fail "apt-get install docker-ce"
  daemon_start || fail "starting the Docker daemon"
  if ! has_systemd; then
    warn "no systemd: the Docker daemon was started with 'service docker start' and will not come back by itself after a reboot."
    [ "$WSL" = 1 ] && warn "under WSL, enable systemd: printf '[boot]\\nsystemd=true\\n' | sudo tee -a /etc/wsl.conf, then wsl --shutdown from Windows."
  fi
  docker compose version >/dev/null 2>&1 || fail "docker compose is still missing after the install"
  DOCKER=installed; DAEMON=up
elif [ "$DOCKER" = present-without-compose ]; then
  step "installing Compose v2 next to the installed Engine"
  [ "$NEED_DOCKER_REPO" = 1 ] && add_docker_repo
  $SUDO apt-get install -y -qq "$COMPOSE_PKG" || fail "apt-get install $COMPOSE_PKG"
  docker compose version >/dev/null 2>&1 || fail "docker compose is still missing after the install"
  DOCKER=compose-installed
fi
if [ "$DAEMON" = down-or-unreadable ]; then
  step "starting the Docker daemon"
  daemon_start >/dev/null 2>&1 || true
  if $SUDO -n docker info >/dev/null 2>&1 || docker info >/dev/null 2>&1; then DAEMON=up
  else fail "the Docker daemon does not answer 'docker info' even as root (under WSL, is systemd enabled?)"; fi
fi

if [ "$TOOLKIT" = "?" ]; then
  # The daemon answers now: read the runtime it was not possible to read before deciding anything.
  $SUDO -n docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"' && TOOLKIT=present || TOOLKIT=absent
fi
if [ "$TOOLKIT" = absent ] && [ "$APT" = 1 ]; then
  step "installing the NVIDIA container toolkit"
  $SUDO install -m 0755 -d "$SYSROOT/usr/share/keyrings" || fail "mkdir /usr/share/keyrings"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | $SUDO gpg --dearmor --yes -o "$SYSROOT/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg" || fail "download of NVIDIA's signing key"
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | $SUDO tee "$SYSROOT/etc/apt/sources.list.d/nvidia-container-toolkit.list" >/dev/null || fail "writing nvidia-container-toolkit.list"
  if ! $SUDO apt-get update -qq; then
    $SUDO rm -f "$SYSROOT/etc/apt/sources.list.d/nvidia-container-toolkit.list"
    fail "apt-get update with NVIDIA's repository (the repository file was removed again)"
  fi
  $SUDO apt-get install -y -qq nvidia-container-toolkit || fail "apt-get install nvidia-container-toolkit"
  $SUDO nvidia-ctk runtime configure --runtime=docker || fail "nvidia-ctk runtime configure"
  daemon_restart || fail "restarting the Docker daemon"
  TOOLKIT=installed
fi

GROUP_ADDED=0
if [ "$IN_GROUP" = 0 ]; then
  step "adding $(id -un) to the docker group"
  $SUDO usermod -aG docker "$(id -un)" || fail "usermod -aG docker"
  GROUP_ADDED=1
  say "  the membership applies to new logins; the rest of this run switches group explicitly."
fi
# THE LAST THING THIS RUN SAYS, WHEN IT ADDED THE GROUP. This run reaches Docker through sg / sudo -g;
# the login session does not carry the group until the next login. The desktop application runs
# `docker compose` as that session, so opened right away it gets "permission denied" from a daemon that
# is running fine -- and the one line that explained why scrolled past an hour of join.sh output. So it
# is repeated at the very end, on success and on failure alike.
relogin_notice(){
  [ "$GROUP_ADDED" = 1 ] || return 0
  [ "${RELOGIN_SAID:-0}" = 1 ] && return 0   # said once: the explicit calls and the EXIT trap below share it
  RELOGIN_SAID=1
  say ""
  say "  [!] LOG OUT AND BACK IN BEFORE OPENING THE DENDRA APPLICATION."
  say "      This run added $(id -un) to the docker group, and a group applies to NEW logins only: until"
  say "      you sign in again, the application (and any docker command you type) is refused by Docker."
  # A SESSION THAT OUTLIVES THE LOGIN KEEPS THE OLD GROUPS. A screen or tmux server started before this run --
  # the usual way to keep a shell on a rig reached over SSH -- hands every window it opens the groups it was
  # started with: signing in again over SSH and re-attaching keeps the refusal, and nothing says why.
  say "      A screen or tmux session started before this run keeps the OLD groups even after you sign in again:"
  say "      leave it and start a NEW one (screen -ls, tmux ls list them; tmux kill-server ends every tmux window),"
  say "      or run one command in the group right away: sg docker -c 'docker ps'"
  [ -n "${TMUX:-}${STY:-}" ] && say "      THIS terminal runs inside $( [ -n "${TMUX:-}" ] && printf tmux || printf screen ): it is one of those sessions."
  [ "$WSL" = 1 ] && say "      Under WSL: close every terminal of this distribution, or run 'wsl --terminate <distribution>' from Windows."
  return 0
}
# The early exits after this point (a daemon that does not answer, a clone refused, a fetch that fails)
# used to end the run without the notice; the trap says it on every way out.
[ "$GROUP_ADDED" = 1 ] && trap 'relogin_notice' EXIT
# Every docker call from here on runs in the docker group even when the login shell does not carry
# it yet. `sg` does it when the distribution ships it; newer Ubuntu releases do not, so `sudo -g`
# is the fallback -- PATH is carried across so that nvidia-smi under WSL stays reachable.
as_docker(){ # as_docker <command string>
  if [ "$IN_GROUP" = 1 ]; then sh -c "$1"
  elif command -v sg >/dev/null 2>&1; then sg docker -c "$1"
  else $SUDO -u "$(id -un)" -g docker env PATH="$PATH" HOME="$HOME" sh -c "$1"; fi
}
as_docker "docker info >/dev/null 2>&1" || fail "the Docker daemon does not answer 'docker info' as a member of the docker group (is it running? under WSL, is systemd enabled?)"

# THE CLONE, unless the deferred role placed it already (right after the tools, before Docker).
[ "$CLONE_PLACED" = 1 ] || place_clone

step "handing over to join.sh ($JOIN_ARGS)"
say "  from here on the output is join.sh's. It verifies the genesis digest, writes the kit, and starts it."
say ""
as_docker "cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' DENDRA_UPDATE_HINT='$UPDATE_HINT' bash deploy/join.sh $JOIN_ARGS"
RC=$?
# THE DESKTOP APPLICATION (ADR-047): only on a graphical session -- a rig reached over SSH has no menu
# to put it in. It writes three files under $HOME and installs no package; a failure here leaves the
# miner running and says how to retry.
install_app(){
  if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ -r "$DENDRA_DIR/deploy/app/install_app.sh" ]; then
    step "the Dendra application"
    bash "$DENDRA_DIR/deploy/app/install_app.sh" \
      || warn "the application was not added to the menu; retry with: bash $DENDRA_DIR/deploy/app/install_app.sh"
  fi
}
# miner_containers -> 0 the miner's container runs, 1 it does not, 3 not read. By the compose LABELS of the kit's
# project (dendra-miner, the `name:` of deploy/testnet-miner/docker-compose.yml) and its service.
miner_containers(){
  local out
  out="$(as_docker "docker ps -q --filter label=com.docker.compose.project=dendra-miner --filter label=com.docker.compose.service=miner" 2>/dev/null)" || return 3
  [ -n "$out" ] && return 0
  return 1
}
if [ "$RC" = 0 ]; then
  install_app
  relogin_notice
  resume "$CLONE" joined
  exit 0
fi
say ""
case "$RC" in
  3) say "  join.sh exited with 3: the miner's containers were started, and within its bound the miner neither"
     say "  registered nor failed -- NOT MEASURED, which is not a failure. Read where it stands in a few minutes:"
     say "    bash '$DENDRA_DIR/deploy/testnet-miner/miner_health.sh'" ;;
  # join.sh's 2 is a REFUSAL (a role, a model, an address, a release it will not run), decided before it started
  # anything. Kept as 2, never folded into 1: a refusal asks for a different setting, a failure for a retry.
  2) say "  join.sh REFUSED (exit 2): it started nothing, and its output above says why and what it would accept."
     say "  The host is prepared; once the cause is changed, re-run just it:"
     say "    cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' bash deploy/join.sh $JOIN_ARGS" ;;
  *) say "  join.sh exited with $RC. The host is prepared; read its output above, then re-run just it:"
     say "    cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' bash deploy/join.sh $JOIN_ARGS" ;;
esac
# ⛔ THE APPLICATION IS INSTALLED WHENEVER THE MINER'S CONTAINERS RUN, whatever join.sh concluded. It used to be
# installed on a clean exit only: join.sh exits 1 when it detects a failure WHILE THE CONTAINERS RUN, and that
# is precisely the miner whose operator needs the application (its health view, its logs, the Start/Stop
# buttons) -- the one who got none. A miner that runs is shown; one that does not, or a docker that does not
# answer, is said.
if [ "$ROLE" != validator ]; then
  miner_containers; MRC=$?
  case "$MRC" in
    0) say "  [i] the miner's containers RUN: the Dendra application is installed anyway, it shows what is wrong."
       install_app ;;
    1) say "  [i] no miner container runs: the Dendra application is not installed (run this file again once join.sh succeeds)." ;;
    *) say "  [?] whether the miner's containers run could not be read: the Dendra application is not installed." ;;
  esac
fi
relogin_notice
if [ "$RC" = 3 ]; then
  resume "$CLONE" join_unmeasured
  exit 3
fi
if [ "$RC" = 2 ]; then
  resume "$CLONE" join_refused
  exit 2
fi
resume "$CLONE" join_failed
exit 1
