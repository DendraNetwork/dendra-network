#!/usr/bin/env bash
# install.sh -- prepare a Linux host for a Dendra miner, then hand over to deploy/join.sh.
#
# What this file is for. `join.sh` is the canonical entry point, and it assumes a host that already has
# Docker with Compose v2, the NVIDIA container toolkit when there is a GPU, git, and a clone of this
# repository. On a workstation that takes ten minutes; on a mining rig it can take an afternoon, and it is
# where many people who wanted to run a miner gave up. This file prepares the host and nothing more: it
# does not join the network, it does not touch a key, it does not verify the chain. When the host is
# ready it runs `join.sh`, which does all of those.
#
# What it does, in this order, and nothing else:
#   1. reads the host: distribution, RAM, free disk, NVIDIA GPU, Docker, Compose, the tools it needs;
#   2. installs what is missing from the distribution's or the vendor's apt repositories (the vendors' at the
#      versions deploy/kit_pins.txt pins, see "Pinned downloads" below): Docker Engine +
#      Compose v2 when there is no Docker at all, Compose alone when an Engine is already installed (an
#      installed Engine is never replaced, whatever its state), the NVIDIA container toolkit when a GPU is
#      present and Docker has no `nvidia` runtime, and git/curl/gpg when absent (Ubuntu, Debian, and HiveOS,
#      which is Ubuntu underneath);
#   3. adds the invoking user to the `docker` group;
#   4. clones the public repository into DENDRA_DIR, or reuses a clone that carries no local work (a clone with
#      local edits or local commits is refused, never reset), and places it on a signed release tag: the one
#      --ref names, or else the newest release tag whose consensus epoch is the one network-info.txt declares
#      (never main, see "A tag, not a branch" below);
#   5. runs `deploy/join.sh --judge` from that clone (or --miner / --validator per the flags).
# Run without the hardware probe next to it (downloaded alone), it places the clone (4) right after the tools git
# needs, asks the clone's probe for the role, and only then does the rest of 2 and 3 (see "No mining model" below).
#
# It changes nothing without --yes. Run it once without the flag: it prints what it would do and exits 2
# when something is missing, 0 when nothing is. Every system change is listed before it happens, including
# the Docker daemon restart the NVIDIA toolkit requires, which briefly stops every running container on the
# host.
#
# It is not meant to be piped from a URL. Download it, read it, then run it, as the documentation also
# advises for join.sh. This file verifies nothing about the chain; join.sh checks the genesis SHA-256
# published in network-info.txt before joining.
#
# No inbound port is opened, and a miner needs none: it dials out to the relay, the RPC and the faucet. A
# home connection behind carrier-grade NAT qualifies. A validator is a different role: it should be
# reachable on 26656, which `deploy/node_reachability.sh` measures after the fact.
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
#                                           # place the clone on the release tag vX.Y.Z instead of the one
#                                           # resolved from the network's epoch (DENDRA_REF does the same), and
#                                           # hand the payout address to join.sh, which verifies it first
#   bash deploy/install.sh --yes --owner dendra1...
#                                           # owner mode (join.sh --owner): that key, not on this machine,
#                                           # registers the miner and holds its stake; handed to join.sh,
#                                           # which verifies its checksum before anything changes
#   ... --ref vX.Y.Z --miner-image-file <that release's miner-image.txt>
#                                           # and write the miner image that release built into the
#                                           # clone (see "The miner image a package carries" below)
#
# A tag, not a branch. The clone always ends on a release tag, checked to sit exactly on it: a branch moves under
# the same name, a tag names one tree, and main is republished by force and carries no signature. --ref takes
# vMAJOR.MINOR.PATCH only. Without it, the tag is resolved: the CONSENSUS_EPOCH that CONFIG_URL declares is read
# (parsed as join.sh parses it, never sourced), the release tags origin publishes are listed, fetched by their full
# name from the newest, and the first whose docker/CONSENSUS_EPOCH reads that epoch on line 1 is the one (during a
# governed upgrade, a release of the next epoch is published before the network declares it, and the release of
# the declared epoch is the one a new installation runs). A network-info that declares no epoch, or no release
# tag of that epoch, is a refusal; this file never falls back to main. A clone already present whose
# docker/CONSENSUS_EPOCH is above the declared epoch is refused before git moves it, never placed on an older epoch:
# the refusal prints this run again with --ref and the release tag of the tree the clone holds, or, for a commit no
# tag names, the newest release of the clone's epoch that origin publishes (clone_epoch_gate).
#
# A clone made by install.ps1. On Windows, deploy/install.ps1 clones the repository into ~/dendra-network and runs
# this file of main, copied with the probe and the pins into the clone's .git (dendra-installer; never the clone's own
# deploy/install.sh, which is an older release's once the clone sits on it). So the clone is there before this file
# starts, on main, which during a governed upgrade carries the next epoch. install.ps1 writes a marker into the clone's .git (dendra-fresh-clone: a token of its run,
# then the commit it cloned) and passes the same token in DENDRA_FRESH_CLONE. The clone counts as made for this run
# only when the marker carries that token and the clone is still as git clone left it: HEAD on the commit the
# marker names, one entry in the reflog of HEAD (the clone), one local branch, and no file git does not track,
# ignored files included (fresh_handover_read). It is then placed on the release of the declared epoch, as a clone
# this run makes, and the marker is removed once the clone sits on its tag. Any other clone, with or without the
# marker, is a clone already present, and the gate above applies to it.
# The HiveOS package (deploy/hiveos) pins its
# own tag with --ref, so a rig runs the tree its package was built from, and updates only when its package is
# replaced.
#
# The miner image a package carries (--miner-image-file, with --ref only). A tagged tree pins no miner image
# (docker/MINER_IMAGE names none on a tag: an image is pinned after the release that built it), so a host on
# a tag builds the miner image, compiling dendrad. The HiveOS package carries the two lines of its release's
# miner-image.txt (`# built-for:` then DENDRA_MINER_IMAGE=). With this flag, once the clone is checked to sit
# on the tag, they are written into the clone's docker/MINER_IMAGE (only when the tag's file names no image,
# never over a value), and join.sh checks them like any pin (deploy/join.sh::miner_image_pin): official
# digest, `built-for` gates equal to the clone's, the image's platform equal to the engine's; otherwise it
# builds from the clone and says why. That write is the one local change this file accepts in a clone: a copy
# of the bytes it wrote is kept in the clone's .git, a clone whose only change is exactly those bytes is not
# local work, and they are undone before the clone moves (to another tag, or to main). Any other change to
# the file is local work, refused as before. Without the flag nothing is written: the image is built.
#   bash deploy/install.sh --yes --gpus all # one miner identity per NVIDIA card (join.sh --gpus): each
#                                           # with its own key, stake, faucet drip and 24-word phrase
#   bash deploy/install.sh --yes --no-judge # decline the judge seat on a host that can judge (the seat is on by
#                                           # default there; join.sh --no-judge records the refusal)
#   bash deploy/install.sh --yes --no-auto-update
#                                           # turn off the daily automatic update of the kit (join.sh writes
#                                           # DENDRA_AUTO_UPDATE=0; on by default, see "Updating the kit")
#   bash deploy/install.sh --yes --payout-lock --payout-address dendra1...
#                                           # declare that address locked with its first declaration (the HiveOS
#                                           # package passes it; a locked address cannot be corrected)
#   bash deploy/install.sh --yes --ref vX.Y.Z --after-upgrade <plan>
#                                           # the installation after the governed upgrade <plan>, relayed to
#                                           # join.sh as it is (deploy/join.sh::after_upgrade_applied): the HiveOS
#                                           # package of a new epoch passes it, since a rig has no upgrade watch
#
# The upgrade plan (--after-upgrade). A gesture of one run, relayed to join.sh, which never records it in the kit's
# re-run. join.sh uses it only where that upgrade explains a difference: a clone one epoch ahead of the epoch
# network-info.txt declares, or a node pinned to an earlier epoch's image, and then only once the chain confirms the
# node at that plan's halt (it then starts the node on the clone's image) or answers the plan applied. The name is
# checked here first, with the alphabet join.sh and deploy/testnet-miner/node_upgrade_watch.sh read (PLAN_NAME_RE),
# since it reaches the command string join.sh runs under.
#
# The release signature. The tag (given or resolved) must be a tag object whose header names
# it: a signature covers the header, not the name a forge publishes it under, so the signed object of an older
# release republished under a newer name is refused. Its signature is verified with docker/RELEASE_SIGNERS (the
# allowed_signers format git reads), and which file depends on the clone. A clone already present verifies the tag
# before anything moves, with the file of the tree it sits on, never with the file the tag ships, as the automatic
# update does; that file must be the one the kit recorded (DENDRA_RELEASE_SIGNERS_SHA256 in the .env of the miner kit
# or of the node kit). A present tree without the file uses the tag's own file only when it is the recorded one. A
# clone made by this run, or a present one with neither a file nor a record, has no trust root yet: once it sits on
# the tag, the tag is verified with the file of that tree, trusting HTTPS to the forge for that first file as for the
# genesis pin, and the plan says so. A tag that does not verify, a tree without the file, or a host without
# ssh-keygen stops the install with nothing started. The newest release of the declared epoch must verify: an older
# one is never taken in its place. join.sh then records the file's SHA-256 in the kit's .env, refuses a file that
# differs at every later run, and the automatic update verifies every candidate tag with the recorded file.
#
# No mining model on the CPU. Before anything changes, this file asks deploy/hw_probe.sh --role (the probe next
# to it): a host with a usable NVIDIA card mines, as before; one without is a judge on the CPU when it has at
# least MOE_CPU_MIN_RAM_MB of RAM (--miner is then refused), is refused below it (exit 2), and is not decided
# when its RAM cannot be read (exit 3). The probe says why, and this file prints it.
# Downloaded alone (the documented path: download it, read it, run it), this file has no probe next to it. The
# role is then asked of the clone's deploy/hw_probe.sh, once this file has placed and checked that clone: the
# order becomes the tools git needs, the clone, the role, and only then Docker, the NVIDIA toolkit and the
# docker group. A host with no role is refused there (exit 2, or 3 when it cannot be decided), with the clone
# placed and nothing else of the plan done; the plan says so before --yes.
#
# The judge disk floor follows the probe. The judge role is asked for by default, and it pulls a second, much
# larger model. Its higher disk floor only matters to a host that can judge, so when free disk sits between
# the two floors this file asks the deploy/hw_probe.sh next to it (--can-judge): `false` lowers the floor to
# the miner's (join.sh keeps the miner role), `true` keeps the judge floor, and an unknown answer or a missing
# probe keeps the judge floor too, saying why.
#
# Pinned downloads (deploy/kit_pins.txt). What this file installs from Docker's and NVIDIA's apt repositories is
# installed at the versions that file pins, from a repository whose signing key has the fingerprint it pins: each key
# is downloaded to a file of this run and compared (gpg --show-keys) before apt is told to trust it, and each pinned
# version must be offered exactly once by that repository for this host, or the install stops there (exit 2) and
# says what to do. The file is read next to this one or, when this file was downloaded alone, in the clone once it
# sits on its verified release tag: the tag's signature covers it. An Engine or a toolkit already installed is kept
# as it is, whatever its version: when that version is not the pin, the run prints the version found and the pin, and
# goes on (pin_version_notes). Docker builds each release for the distribution releases it supports at that time, so
# a codename with no build of the pinned Engine (an older or a newer release) stops the run before anything is installed
# from Docker's repository, and the refusal names the way by hand (docker_by_hand); the distribution's own
# docker-compose-v2 (next to a docker.io Engine) comes at the
# version of that distribution's release. The models are pulled by the kit's compose file, not by this file.
#
# Undoing it: bash deploy/uninstall.sh (plan only; --yes to remove what the kit set up, keys kept).
#
# Updating the kit. This is the kit's single update instruction, printed by deploy/join.sh (kit_update_hint) with
# the release tag it names already resolved (the newest one origin publishes at the epoch network-info.txt
# declares, the same rule as above): from the clone,
#   git fetch origin refs/tags/<tag>:refs/tags/<tag> && git -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile=docker/RELEASE_SIGNERS verify-tag refs/tags/<tag> && git branch -f kit-before-update && git checkout --detach refs/tags/<tag>
# (the tag verified with the signers file of the tree the clone sits on, before it moves), then re-run deploy/join.sh
# with the same options (the tree the clone leaves stays on the branch
# kit-before-update until the next update, so a commit of your own is never silently dropped). Running this
# file again with --yes also places the clone on that release and runs join.sh, but only with the flags given
# this time: it keeps none of those it was first given (without --miner it asks for the judge role, without
# --light it runs a node of its own), so give it the same flags as the first time.
# From kit v0.2.2 the kit also updates itself: join.sh schedules a daily run of
# deploy/testnet-miner/kit_update.sh, which moves the clone to a newer release tag only when that tag verifies
# with the clone's docker/RELEASE_SIGNERS and carries the same consensus epoch, never to a branch; it prints its
# plan, logs under the kit, keeps the tree it leaves on kit-before-update, replays the recorded join and rolls
# back when that fails. --no-auto-update (DENDRA_AUTO_UPDATE=0) turns it off. The first installation is never
# automatic: this file stays downloaded, read, run.
# `git pull` does not work: each release republishes the repository as a new root commit, so git pull refuses
# ("refusing to merge unrelated histories") and leaves the old tree; a clone left in that state is recognised
# below as an earlier publication, not as local work. The same republication is why `git fetch origin` brings
# none of the release tags off main: a tag is always fetched by its full name.
#
# Environment (all optional):
#   CONFIG_URL       network-info.txt of the network to join (default: the public network's)
#   DENDRA_DIR       where the repository lives or gets cloned (default: $HOME/dendra-network)
#   DENDRA_REPO_URL  git URL to clone (default: the public repository)
#   DENDRA_REF       the release tag to clone and stay on (as --ref; the flag wins)
#   DENDRA_FRESH_CLONE  the token install.ps1 wrote into the marker of the clone it made (see "A clone made by
#                    install.ps1"); without a marker that carries it, the token changes nothing
#   DENDRA_INSTALL_RESULT_FILE  a file that receives the word of the last action (planned, joined,
#                    join_unmeasured, join_refused, join_failed, refused, failed): how the HiveOS package tells
#                    the two exit 3 apart
#   DENDRA_MIN_DISK_GB / DENDRA_MIN_DISK_GB_JUDGE / DENDRA_MIN_RAM_MB   refusal floors, see below
#
# Exit codes (three answers, never two):
#   0  done, or a plan with nothing missing
#   1  a step was attempted and failed
#   2  refused: no --yes while changes are needed, unsupported host (no apt, WSL 1), too little disk
#      or RAM, no role on the testnet (deploy/hw_probe.sh --role: refused, or --miner without a usable GPU),
#      a clone that carries local work or cannot be inspected, a path or URL this file cannot pass on safely,
#      without --ref a network-info.txt that declares no readable CONSENSUS_EPOCH or no release tag of that
#      epoch, or a clone present at a consensus epoch above the declared one, a release tag that does not verify, a deploy/kit_pins.txt that is missing or wrong when Docker
#      or the NVIDIA toolkit is to be installed, a signing key or a version other than the pinned one, or join.sh
#      refused (its own exit 2: it started nothing; action=join_refused)
#   3  not measurable: the host could not be read (no /etc/os-release, no /proc/meminfo, no disk
#      figure, no role decided by deploy/hw_probe.sh --role), without --ref a network-info.txt that does not
#      answer or a present clone whose docker/CONSENSUS_EPOCH holds no number, or join.sh started the miner and could not
#      measure its health within its bound (join.sh exit 3: the miner keeps running and registering).
#      An unknown is never treated as a pass.
set -u

# ---------------------------------------------------------------- flags
YES=0; CHECK=0; LIGHT=0; ROLE=judge; GPUS=""
REF="${DENDRA_REF:-}"; PAYOUT=""; OWNER=""; PKG_PIN_FILE=""; AFTER_UPGRADE=""
# --no-judge declines the seat (the miner role, relayed as join.sh --miner --no-judge, so a re-run keeps
# the refusal); the automatic update switch and the payout lock are relayed to join.sh as they are. The
# environment names them too (DENDRA_AUTO_UPDATE, DENDRA_PAYOUT_LOCK: the HiveOS package passes the first).
AUTO_UPDATE="${DENDRA_AUTO_UPDATE:-1}"; PAYOUT_LOCK="${DENDRA_PAYOUT_LOCK:-0}"
# The options of this run, as given: a refusal that names the next run prints them back (clone_epoch_gate).
INSTALL_ARGS=("$@")
# A refusal of this installer names the section of the site that settles it. site_topic reads what the message names
# and answers a topic; site_url answers the section of that topic. Both live in this file, which is often downloaded
# alone. That every topic is a section of the site, and that every refusal of the kit has a topic, is checked by
# dendra/onchain-staging/dendra_kit_site_links_test.py.
SITE_BASE="https://dendranetwork.com"
site_url(){
  case "$1" in
    need)    printf '%s/miner/#need' "$SITE_BASE" ;;
    install) printf '%s/miner/#install' "$SITE_BASE" ;;
    windows) printf '%s/miner/#windows' "$SITE_BASE" ;;
    update)  printf '%s/update/#update' "$SITE_BASE" ;;
    upgrade) printf '%s/docs/#upgrade-window' "$SITE_BASE" ;;
    help)    printf '%s/miner/#help' "$SITE_BASE" ;;
  esac
}
site_topic(){
  case "$*" in
    *upgrade*|*"network rules"*) printf upgrade ;;
    *WSL*) printf windows ;;
    *"free disk"*|*RAM*) printf need ;;
    *"--ref"*|*"release tag"*|*" tag "*|*RELEASE_SIGNERS*|*kit-before-update*|*"ls-remote"*) printf update ;;
    *toolkit*|*"signing key"*|*"apt-get"*|*"apt-based"*|*Docker*|*docker*|*Compose*|*compose*|*sudo*|*root*|*kit_pins*|*codename*|*"repository file"*|*usermod*|*"these tools"*) printf install ;;
    *clone*|*CONFIG_URL*) printf update ;;
    *GPU*|*gpu*|*hw_probe*|*" role"*|*"--miner"*|*"--judge"*) printf need ;;
    *) printf help ;;
  esac
}
refusal_link(){ printf '  More: %s\n' "$(site_url "$1")" >&2; }
while [ $# -gt 0 ]; do case "$1" in
  --yes)       YES=1; shift;;
  --check)     CHECK=1; shift;;
  --light)     LIGHT=1; shift;;
  --judge)     ROLE=judge; shift;;
  --miner|--no-judge) ROLE=miner; shift;;
  --auto-update)    AUTO_UPDATE=1; shift;;
  --no-auto-update) AUTO_UPDATE=0; shift;;
  --payout-lock)    PAYOUT_LOCK=1; shift;;
  --validator) ROLE=validator; shift;;
  --ref)       REF="${2:-}"; [ -n "$REF" ] || { echo "[install] --ref needs a release tag (vX.Y.Z)"; refusal_link update; exit 2; }; shift 2;;
  --payout-address) PAYOUT="${2:-}"; [ -n "$PAYOUT" ] || { echo "[install] --payout-address needs an address (dendra1...)"; refusal_link help; exit 2; }; shift 2;;
  --owner)     OWNER="${2:-}"; [ -n "$OWNER" ] || { echo "[install] --owner needs an address (dendra1...)"; refusal_link help; exit 2; }; shift 2;;
  --miner-image-file) PKG_PIN_FILE="${2:-}"; [ -n "$PKG_PIN_FILE" ] || { echo "[install] --miner-image-file needs a file (a release's miner-image.txt)"; refusal_link update; exit 2; }; shift 2;;
  --gpus)      GPUS="${2:-}"; [ -n "$GPUS" ] || { echo "[install] --gpus needs a value: all, card indices (0,2) or card UUIDs"; refusal_link need; exit 2; }; shift 2;;
  --after-upgrade) AFTER_UPGRADE="${2:-}"; [ -n "$AFTER_UPGRADE" ] || { echo "[install] --after-upgrade needs the name of the upgrade plan. Nothing was changed."; refusal_link upgrade; exit 2; }; shift 2;;
  -h|--help)   awk 'NR>1{ if ($0 !~ /^#/) exit; print }' "$0"; exit 0;;
  *) echo "[install] unknown argument: $1 (see --help)"; refusal_link help; exit 2;;
esac; done
# One identity per card: --gpus is relayed to join.sh, and validated here first, by the expression join.sh
# applies (GPUS_SPEC_RE, the same text in both files): the value reaches a command line built as a string
# (`as_docker "... join.sh $JOIN_ARGS"`), where an unvalidated value is an injection. The expression judges
# the whole value (bash's `=~`: ^ and $ are the ends of the string), never one line of it: `grep -q` succeeds
# when one line matches, and the next line of a value carrying a newline would reach `sh -c`.
GPUS_SPEC_RE='^(all|[0-9]+(,[0-9]+)*|GPU-[0-9a-f-]+(,GPU-[0-9a-f-]+)*)$'
if [ -n "$GPUS" ]; then
  [[ $GPUS =~ $GPUS_SPEC_RE ]] \
    || { echo "[install] --gpus $GPUS is not all, a list of card indices (0,2) or a list of card UUIDs (GPU-...). Nothing was changed."; refusal_link need; exit 2; }
  [ "$ROLE" != validator ] || { echo "[install] --gpus runs one miner identity per card; a validator is one node. Nothing was changed."; refusal_link need; exit 2; }
fi
# The upgrade plan (see the header): the alphabet join.sh applies, judged on the whole value like GPUS_SPEC_RE.
PLAN_NAME_RE='^[A-Za-z0-9._-]{1,128}$'
if [ -n "$AFTER_UPGRADE" ] && ! [[ $AFTER_UPGRADE =~ $PLAN_NAME_RE ]]; then
  echo "[install] --after-upgrade takes the name of an upgrade plan (letters, digits and . _ -, at most 128): this value is not passed on. Nothing was changed."; refusal_link upgrade; exit 2
fi

say(){ printf '%s\n' "$*"; }
warn(){ printf '  [!] %s\n' "$*"; }
refuse(){ printf '  [refused] %s\n' "$*" >&2; refusal_link "$(site_topic "$*")"; }
unmeasurable(){ printf '  [?] %s\n' "$*" >&2; refusal_link "$(site_topic "$*")"; exit 3; }
case "$AUTO_UPDATE" in 0|1) : ;; *) refuse "DENDRA_AUTO_UPDATE must be 0 or 1 (got: $AUTO_UPDATE). Nothing was changed."; exit 2;; esac
case "$PAYOUT_LOCK" in 0|1) : ;; *) refuse "DENDRA_PAYOUT_LOCK must be 0 or 1 (got: $PAYOUT_LOCK). Nothing was changed."; exit 2;; esac
[ "$ROLE" = validator ] && [ "$PAYOUT_LOCK" = 1 ] && { refuse "--payout-lock applies to a miner, not to --validator. Nothing was changed."; exit 2; }

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
# the install does not fail loudly: it fails an hour later inside a pull or build log, so it is
# refused up front instead.
MIN_DISK_GB="${DENDRA_MIN_DISK_GB:-12}"
MIN_DISK_GB_JUDGE="${DENDRA_MIN_DISK_GB_JUDGE:-32}"
MIN_RAM_MB="${DENDRA_MIN_RAM_MB:-4000}"
WARN_RAM_MB=8000
# The hardware probe asked whether this host can judge: the one shipped next to this file, never one
# from the clone, which is not updated yet at this point and may not be this repository at all.
# DENDRA_HW_PROBE is a test seam, like the ones above.
HW_PROBE="${DENDRA_HW_PROBE:-$(dirname "$0")/hw_probe.sh}"
# No probe next to this file (it was downloaded alone): the role is deferred to the clone's probe, asked only after
# this file has placed and checked the clone (step 3, before Docker, the toolkit or the group). A probe named by
# the seam and absent is not deferred: it stays a missing reading (exit 3).
PROBE_DEFERRED=0
[ -z "${DENDRA_HW_PROBE:-}" ] && [ ! -r "$HW_PROBE" ] && PROBE_DEFERRED=1
# The versions and signing-key fingerprints of what this file installs from Docker's and NVIDIA's repositories
# (deploy/kit_pins.txt, read by pins_load): the copy next to this file, from the same tree as the probe; or, when this
# file was downloaded alone, the copy in the clone, which is then placed and its tag verified before anything is
# installed from those repositories (pins_require). DENDRA_KIT_PINS is a test seam, like the ones above: a file it
# names and that is absent is a missing pin file, never a deferred one.
KIT_PINS="${DENDRA_KIT_PINS:-$(dirname "$0")/kit_pins.txt}"
PINS_DEFERRED=0
[ -z "${DENDRA_KIT_PINS:-}" ] && [ ! -e "$KIT_PINS" ] && PINS_DEFERRED=1
NVIDIA_REPO="https://nvidia.github.io/libnvidia-container"
# pins_load -> PINS_STATE (read | absent | unreadable), PINS_WHY and the PIN_* values. Every key is read: a file with
# one key wrong is not trusted for the others either. No key has a default: a missing pin stops what needs it.
PINS_STATE=absent; PINS_WHY="the pins are read from the clone once it is placed"
PIN_DOCKER_KEY=""; PIN_DOCKER_ENGINE=""; PIN_DOCKER_CONTAINERD=""; PIN_DOCKER_BUILDX=""; PIN_DOCKER_COMPOSE=""
PIN_NVIDIA_KEY=""; PIN_NVIDIA_TOOLKIT=""
pin_get(){ # pin_get <key> <ERE of the value> -> PIN_VAL, or 1 with PINS_WHY
  local n
  PIN_VAL=""
  n="$(awk -F= -v k="$1" '$1 == k { n++ } END { print n + 0 }' "$KIT_PINS" 2>/dev/null)"
  [ "$n" = 1 ] || { PINS_WHY="$KIT_PINS names $1 ${n:-?} time(s), and each pin is named exactly once"; return 1; }
  PIN_VAL="$(awk -v k="$1" 'index($0, k "=") == 1 { print substr($0, length(k) + 2) }' "$KIT_PINS")"
  [[ $PIN_VAL =~ $2 ]] && return 0
  PINS_WHY="$KIT_PINS gives $1 a value outside its form ($(printf '%s' "$PIN_VAL" | LC_ALL=C tr -cd 'A-Za-z0-9._:-' | cut -c1-48))"
  PIN_VAL=""; return 1
}
pins_load(){
  local hex='^[0-9A-F]{40}$' up='^[0-9]+([.][0-9]+)+$' full='^[0-9]+([.][0-9]+)+-[0-9]+$'
  PINS_STATE=unreadable; PINS_WHY=""
  if [ ! -e "$KIT_PINS" ]; then PINS_STATE=absent; PINS_WHY="there is no pin file at $KIT_PINS"; return 0; fi
  { [ -f "$KIT_PINS" ] && [ -r "$KIT_PINS" ]; } || { PINS_WHY="$KIT_PINS cannot be read"; return 0; }
  pin_get DOCKER_APT_KEY_FINGERPRINT "$hex" || return 0; PIN_DOCKER_KEY="$PIN_VAL"
  pin_get DOCKER_ENGINE_VERSION "$up" || return 0; PIN_DOCKER_ENGINE="$PIN_VAL"
  pin_get DOCKER_CONTAINERD_VERSION "$up" || return 0; PIN_DOCKER_CONTAINERD="$PIN_VAL"
  pin_get DOCKER_BUILDX_VERSION "$up" || return 0; PIN_DOCKER_BUILDX="$PIN_VAL"
  pin_get DOCKER_COMPOSE_VERSION "$up" || return 0; PIN_DOCKER_COMPOSE="$PIN_VAL"
  pin_get NVIDIA_APT_KEY_FINGERPRINT "$hex" || return 0; PIN_NVIDIA_KEY="$PIN_VAL"
  pin_get NVIDIA_CONTAINER_TOOLKIT_VERSION "$full" || return 0; PIN_NVIDIA_TOOLKIT="$PIN_VAL"
  PINS_STATE=read
}
[ "$PINS_DEFERRED" = 0 ] && pins_load
# pins_line -> the host reading's line; pin_words <docker|compose|nvidia> -> what the plan says is installed.
pins_line(){
  if [ "$PINS_STATE" = read ]; then
    printf '%s' "Docker Engine $PIN_DOCKER_ENGINE, Compose $PIN_DOCKER_COMPOSE, NVIDIA toolkit $PIN_NVIDIA_TOOLKIT, for what this file installs ($KIT_PINS); an installed Engine or toolkit is kept as it is"
  elif [ "$PINS_DEFERRED" = 1 ]; then
    printf '%s' "read from deploy/kit_pins.txt in the clone, once it sits on its release tag"
  else
    printf '%s' "not read: $PINS_WHY"
  fi
}
pin_words(){
  if [ "$PINS_STATE" != read ]; then
    printf '%s' "at the versions, and with the signing key, pinned by deploy/kit_pins.txt in the clone (read once the clone sits on its release tag, before anything is installed from there)"
    return 0
  fi
  case "$1" in
    docker)  printf '%s' "Engine $PIN_DOCKER_ENGINE, containerd.io $PIN_DOCKER_CONTAINERD, buildx $PIN_DOCKER_BUILDX and Compose $PIN_DOCKER_COMPOSE, from a repository whose signing key is $PIN_DOCKER_KEY ($KIT_PINS)" ;;
    compose) printf '%s' "Compose $PIN_DOCKER_COMPOSE, from a repository whose signing key is $PIN_DOCKER_KEY ($KIT_PINS)" ;;
    nvidia)  printf '%s' "version $PIN_NVIDIA_TOOLKIT of its four packages, from a repository whose signing key is $PIN_NVIDIA_KEY ($KIT_PINS)" ;;
  esac
}

# A path or a URL is passed on to other shells below. One that carries a single quote cannot be
# quoted safely, so it is refused here, before anything is read or changed, rather than after Docker
# is installed.
case "$DENDRA_DIR$CONFIG_URL" in *"'"*) refuse "DENDRA_DIR or CONFIG_URL contains a single quote, which this file cannot pass on safely."; exit 2;; esac
case "$CONFIG_URL" in
  http://*|https://*) : ;;
  *) refuse "CONFIG_URL must start with http:// or https:// (got: $CONFIG_URL)"; exit 2;;
esac
# The tag and the two addresses are passed on to git and to join.sh: they are refused here when their form is
# wrong, before anything is read or changed. The addresses' checksum is join.sh's to check, with the same
# refusals (it exits 2 before anything changes on its side).
# (The tag form is the release workflow's, from publish_release.sh and .github/release_notes.sh: no leading zero.)
# Each form judges the whole value (bash's `=~`, where ^ and $ are the ends of the string), as GPUS_SPEC_RE does.
# `grep -x` judges one line, so a value whose first line had the right form passed with any second line, and
# the addresses are spliced into the command string join.sh runs under (JOIN_ARGS, as_docker).
REF_RE='^v(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$'
ADDR_FORM_RE='^[A-Za-z0-9]{1,128}$'
if [ -n "$REF" ] && ! [[ $REF =~ $REF_RE ]]; then
  refuse "--ref / DENDRA_REF must be a release tag vMAJOR.MINOR.PATCH, without leading zeros (got: $REF): a branch moves under its name, a tag names one tree."; exit 2
fi
# Without --ref the release tag is resolved from the network's epoch (see "A tag, not a branch" in the header).
REF_RESOLVE=0; [ -z "$REF" ] && REF_RESOLVE=1
# net_epoch_read -> NET_EPOCH, NET_EPOCH_STATE (read | absent | unreadable | unreached) and NET_EPOCH_WHY: the
# CONSENSUS_EPOCH that CONFIG_URL declares, parsed as join.sh parses that file (KEY=VALUE lines, never sourced, the
# last one wins). Absent is a reading of the file, unreached is no reading at all: two states, never folded.
NET_EPOCH=""; NET_EPOCH_STATE=""; NET_EPOCH_WHY=""
net_epoch_read(){
  local doc v
  NET_EPOCH=""; NET_EPOCH_STATE=unreached; NET_EPOCH_WHY=""
  command -v curl >/dev/null 2>&1 || { NET_EPOCH_WHY="curl is not installed"; return 0; }
  doc="$(curl -fsSL --max-time 30 "$CONFIG_URL" 2>/dev/null)" || { NET_EPOCH_WHY="$CONFIG_URL did not answer"; return 0; }
  doc="$(printf '%s\n' "$doc" | tr -d '\r')"
  if ! printf '%s\n' "$doc" | grep -q '^CONSENSUS_EPOCH='; then
    NET_EPOCH_STATE=absent; NET_EPOCH_WHY="$CONFIG_URL declares no CONSENSUS_EPOCH"; return 0
  fi
  v="$(printf '%s\n' "$doc" | sed -n 's/^CONSENSUS_EPOCH=//p' | tail -1)"
  case "$v" in
    ""|*[!0-9]*) NET_EPOCH_STATE=unreadable
       NET_EPOCH_WHY="the CONSENSUS_EPOCH that $CONFIG_URL declares is not a number ($(printf '%s' "$v" | LC_ALL=C tr -cd 'A-Za-z0-9._-' | cut -c1-40))"
       return 0 ;;
  esac
  NET_EPOCH="$v"; NET_EPOCH_STATE=read
}
# net_epoch_gate <what has changed so far> [summary] -- returns on a read epoch; otherwise the refusal says what to
# do: exit 2 on a file that declares none or an unreadable one, exit 3 when the file was not reached. With
# "summary", the word of the action is written (the run has changed something by then).
net_epoch_gate(){
  local how="Check that CONFIG_URL is the network-info.txt of the network you join, or name a release yourself with --ref vX.Y.Z (a signed release tag that runs that network's rules)."
  [ "$NET_EPOCH_STATE" = read ] && return 0
  case "$NET_EPOCH_STATE" in
    absent|unreadable)
      refuse "$NET_EPOCH_WHY. Without --ref this file installs the newest signed release tag that runs the rules the network declares, and it never falls back to main. $how $1"
      [ "${2:-}" = summary ] && summary "$CLONE" refused
      exit 2 ;;
  esac
  [ "${2:-}" = summary ] && summary "$CLONE" role_unmeasured
  unmeasurable "the network rules of the network were not read ($NET_EPOCH_WHY), and without it no release tag can be chosen: this file never falls back to main. $how $1"
}
# inst_word <word> -> the word bare when a shell reads it back unchanged, else quoted by printf %q.
INST_BARE_RE='^[A-Za-z0-9,._:/@%+=-]+$'
inst_word(){ if [[ $1 =~ $INST_BARE_RE ]]; then printf '%s' "$1"; else printf '%q' "$1"; fi; }
# clone_epoch_gate [summary] -- without --ref, a clone already present whose docker/CONSENSUS_EPOCH is above the epoch
# CONFIG_URL declares is refused before git moves anything: during a governed upgrade the network's file declares the
# old epoch until its operator republishes it, and a node that passed the upgrade cannot run the release of the older
# epoch. The refusal prints this run again on the release tag that names the clone's commit, read with git tag
# --points-at, else with git ls-remote (neither changes anything); join.sh, run from that tree, then asks the chain
# (deploy/join.sh::verify_consensus_epoch). A commit no tag names (main republished without a number, which the old
# update instruction followed) gets the newest release tag of the clone's epoch, or, when origin publishes none, the
# release of the declared epoch (epoch_gate_tags); origin's tags unread is not measured (exit 3). A tree without the
# file is placed as before; a file whose line 1 holds no number is not measured (exit 3), never read as an epoch.
# Asked once, of a clone that neither this run nor install.ps1 for this run made (fresh_handover_read).
# epoch_gate_tags <epoch> -> EG_STATE (read|unread), EG_CE (the newest release tag of that epoch), EG_NE (the newest
# of NET_EPOCH) and EG_WHY. Origin's tags are fetched by name into a namespace of this function and removed again:
# HEAD, the working tree and refs/tags are not changed. Defined here, before the plan calls the gate: the tag
# namespace helpers of the placement come later in this file.
EG_NS=refs/dendra-install-epoch-gate
epoch_gate_clear(){
  git -C "$DENDRA_DIR" for-each-ref --format='%(refname)' "$EG_NS/" 2>/dev/null \
    | while read -r _r; do git -C "$DENDRA_DIR" update-ref -d "$_r" 2>/dev/null; done
  return 0
}
epoch_gate_tags(){
  local want="$1" lsr names t e
  EG_STATE=unread; EG_CE=""; EG_NE=""; EG_WHY=""
  lsr="$(git -C "$DENDRA_DIR" ls-remote --tags origin 2>/dev/null)" || { EG_WHY="git ls-remote --tags origin did not answer"; return 0; }
  names="$(printf '%s\n' "$lsr" | awk 'NF >= 2 { print $2 }' | sed -n 's#^refs/tags/##p' | grep -v '[{][}]$' \
           | grep -E "$REF_RE" | sort -rV)"
  epoch_gate_clear
  for t in $names; do
    if ! git -C "$DENDRA_DIR" fetch --quiet --no-tags origin "+refs/tags/$t:$EG_NS/$t" >/dev/null 2>&1; then
      epoch_gate_clear; EG_WHY="the tag $t could not be fetched from origin"; return 0
    fi
    e="$(git -C "$DENDRA_DIR" show "$EG_NS/$t:docker/CONSENSUS_EPOCH" 2>/dev/null | head -1 | tr -dc 0-9)"
    [ -z "$EG_NE" ] && [ "$e" = "$NET_EPOCH" ] && EG_NE="$t"
    if [ "$e" = "$want" ]; then EG_CE="$t"; break; fi
  done
  epoch_gate_clear
  EG_STATE=read
}
CLONE_EPOCH_DONE=0
clone_epoch_gate(){
  local f="$DENDRA_DIR/docker/CONSENSUS_EPOCH" ce head t a cmd
  [ "$CLONE_EPOCH_DONE" = 1 ] && return 0
  { [ "$REF_RESOLVE" = 1 ] && [ "$CLONE" = present ] && [ "${CLONE_FRESH:-0}" != 1 ] && [ "$FRESH_HANDOVER" != 1 ] && [ "$NET_EPOCH_STATE" = read ]; } || return 0
  CLONE_EPOCH_DONE=1
  [ -e "$f" ] || return 0
  ce="$(head -1 "$f" 2>/dev/null | tr -dc 0-9)"
  if [ -z "$ce" ]; then
    [ "${1:-}" = summary ] && summary "$CLONE" role_unmeasured
    unmeasurable "$f holds no network rules number on its line 1, so whether the release the network declares would move the clone in $DENDRA_DIR to earlier network rules is unknown, and it is not moved on a guess. Nothing was changed in the clone."
  fi
  [ "$((10#$ce))" -gt "$((10#$NET_EPOCH))" ] 2>/dev/null || return 0
  head="$(git -C "$DENDRA_DIR" rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null)"
  t=""
  if [ -n "$head" ]; then
    t="$(git -C "$DENDRA_DIR" tag --points-at HEAD 2>/dev/null | grep -E "$REF_RE" | sort -rV | head -1)"
    # Origin's peeled lines name the commit each tag points at (refs/tags/vX.Y.Z^{}).
    [ -n "$t" ] || t="$(git -C "$DENDRA_DIR" ls-remote --tags origin 2>/dev/null | awk -v h="$head" '$1 == h { n = $2; sub(/^refs[/]tags[/]/, "", n); if (substr(n, length(n) - 2) == "^{}") n = substr(n, 1, length(n) - 3); print n }' | grep -E "$REF_RE" | sort -rV | head -1)"
  fi
  cmd="CONFIG_URL=$(inst_word "$CONFIG_URL") DENDRA_DIR=$(inst_word "$DENDRA_DIR") bash $(inst_word "$0")"
  for a in "${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}"; do cmd="$cmd $(inst_word "$a")"; done
  EG_STATE=read; EG_CE=""; EG_NE=""
  [ -n "$t" ] || epoch_gate_tags "$ce"
  if [ "$EG_STATE" != read ]; then
    [ "${1:-}" = summary ] && summary "$CLONE" role_unmeasured
    unmeasurable "the clone in $DENDRA_DIR follows newer network rules than $CONFIG_URL declares, and no release tag names the commit it holds (${head:-unread}). Origin's release tags were not read ($EG_WHY), so the release that keeps it on those rules cannot be named, and without --ref this file never moves a clone to earlier network rules. Nothing was changed. Run this same command again once origin answers: $cmd"
  fi
  refuse "the clone in $DENDRA_DIR follows newer network rules than $CONFIG_URL declares, and without --ref this file never moves a clone to earlier network rules: during a governed upgrade the network's file declares the old rules until its operator republishes it, and a node that passed the upgrade cannot run the release of the older rules. Nothing was changed."
  local what="the release tag of the tree it holds"
  if [ -z "$t" ] && [ -n "$EG_CE" ]; then
    say "         No release tag names the commit this clone holds (${head:-unread}), a publication of main without a number."
    t="$EG_CE"; what="the newest release that runs the rules of this clone, as origin publishes it"
  fi
  if [ -n "$t" ]; then
    say "         To keep this clone on its rules, run this installation on $t, $what:"
    say "           $cmd --ref $t"
    say "         join.sh, run from that tree, then asks the chain: while the network declares the older rules it stops and"
    say "         prints the next command, the same join with --after-upgrade and the plan's name when the chain answers that"
    say "         plan applied or this kit's node stands at its halt, and otherwise the release the network declares."
  elif [ -n "$EG_NE" ]; then
    say "         No release tag names the commit this clone holds (${head:-unread}), and no release tag origin publishes"
    say "         runs the rules of this clone: that tree was never released. $EG_NE is the newest release of the rules"
    say "         $CONFIG_URL declares; to place this clone on it, run this installation on that tag:"
    say "           $cmd --ref $EG_NE"
  else
    say "         No release tag names the commit this clone holds (${head:-unread}), and no release tag origin publishes"
    say "         runs the rules of this clone or the rules $CONFIG_URL declares: no release is named here."
  fi
  if [ -n "$t" ]; then
    say "         Once $CONFIG_URL declares the rules of this clone, this same command without --ref places the clone on the newest"
    say "         release of those rules."
  fi
  [ "${1:-}" = summary ] && summary "$CLONE" refused
  exit 2
}
# What "update" means for this copy, handed to join.sh, which prints it on its "Update:" lines instead of the
# git command of a clone. A package (HiveOS) names its own path in DENDRA_UPDATE_HINT; it is passed on as a quoted
# word, so only plain characters are accepted. A clone, on a tag given by --ref or resolved, gets none: join.sh
# names the release tag of the declared epoch itself (deploy/join.sh::kit_update_hint), with the options of its run.
UPDATE_HINT="${DENDRA_UPDATE_HINT:-}"
if printf '%s' "$UPDATE_HINT" | LC_ALL=C grep '[^-A-Za-z0-9 .,:/_()]' >/dev/null; then
  refuse "DENDRA_UPDATE_HINT holds a character this file does not pass on (only letters, digits, spaces and . , : / _ ( ) -)."; exit 2
fi
if [ -n "$PAYOUT" ] && ! [[ $PAYOUT =~ $ADDR_FORM_RE ]]; then
  refuse "--payout-address holds characters no address has: it is not passed on (the value is not printed)."; exit 2
fi
[ "$ROLE" = validator ] && [ -n "$PAYOUT" ] && { refuse "--payout-address applies to a miner, not to --validator."; exit 2; }
# Owner mode (join.sh --owner): the same form and the same role rule as join.sh's: a miner, the judge role
# included (join.sh runs it as a miner), never a validator. Its checksum is join.sh's to verify.
if [ -n "$OWNER" ] && ! [[ $OWNER =~ $ADDR_FORM_RE ]]; then
  refuse "--owner holds characters no address has: it is not passed on (the value is not printed)."; exit 2
fi
[ "$ROLE" = validator ] && [ -n "$OWNER" ] && { refuse "--owner applies to a miner, not to --validator. Nothing was changed."; exit 2; }

# >>> The miner image a package carries (see the header). Its file is read here, before anything changes, so
# the plan can say what will happen to it; it is written only after the clone is checked on the tag.
# PKG_PIN_STATE: "" (no flag) | ok | unusable (PKG_PIN_WHY says why: nothing is then written, the image is built).
PKG_PIN_STATE=""; PKG_PIN_WHY=""
if [ -n "$PKG_PIN_FILE" ]; then
  [ -n "$REF" ] || { refuse "--miner-image-file goes with --ref: a release's miner image belongs to that release's tag, and a clone that follows main pins its own."; exit 2; }
  [ "$ROLE" = validator ] && { refuse "--miner-image-file applies to a miner, not to --validator."; exit 2; }
  PKG_PIN_STATE=unusable
  if [ ! -f "$PKG_PIN_FILE" ] || [ ! -r "$PKG_PIN_FILE" ]; then
    PKG_PIN_WHY="the package's miner image file ($PKG_PIN_FILE) cannot be read"
  else
    # Exactly the two lines of that release's miner-image.txt, each ending with a newline. The value's exact
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
# <<< The miner image a package carries

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
# Docker's repository is keyed on the base distribution, and its suite on the base's codename: on a
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

# Docker is read as three separate facts, because folding them into one word once cost an Engine. Whether
# a CLI is installed decides what may be installed: an installed Engine is never replaced (docker-ce
# declares Conflicts: docker.io, so installing it over docker.io removes docker.io and stops every
# container it ran). Whether Compose v2 is there decides what is added. Whether the daemon answers is a
# third fact, read with the rights that can read it: a user not yet in the docker group gets
# "permission denied" from a running daemon, which does not mean Docker is missing.
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
# How many cards: counted by lines (awk), never with grep -c, which prints 0 and also fails. `?` when
# nvidia-smi answered the first question and not this one, never a count of 0. Its output is captured with
# its exit status first: in a pipeline the status is awk's, and awk counts an empty answer as 0.
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
# tk_pkg_read -> TK_PKG, what dpkg says of the toolkit's four packages when the runtime may be missing:
#   installed   nvidia-container-toolkit is installed (TK_VER): it is kept at its version and only registered with
#               Docker, since installing the pinned version over a newer one is a downgrade that apt refuses;
#   absent      none of the four is installed: the pinned version is installed;
#   partial     the toolkit is not installed but some of its libraries are (TK_WHY): refused, they could be newer;
#   broken      the toolkit is installed and nvidia-ctk cannot be found to register it: refused;
#   unreadable  dpkg-query is missing, fails, or lists a half-done install (TK_WHY): refused, nothing is
#               installed over a state that cannot be read.
TK_PKG=n/a; TK_VER=""; TK_WHY=""; TK_LIBS=""
tk_pkg_read(){
  local p out rc st v libs=""
  TK_PKG=unreadable; TK_VER=""; TK_WHY=""; TK_LIBS=""
  command -v dpkg-query >/dev/null 2>&1 || { TK_WHY="dpkg-query is not installed"; return 0; }
  for p in nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1; do
    out="$(dpkg-query -W -f='${db:Status-Abbrev}|${Version}' "$p" 2>/dev/null)"; rc=$?
    # dpkg-query exits 1 for a package it has never heard of, and prints its state for one it knows.
    [ "$rc" = 1 ] && continue
    [ "$rc" = 0 ] || { TK_WHY="dpkg-query exited $rc on $p"; return 0; }
    st="${out%%|*}"; v="${out#*|}"
    case "$st" in
      ?i|?i[[:space:]]) if [ "$p" = nvidia-container-toolkit ]; then TK_VER="$v"; else libs="$libs $p $v"; TK_LIBS="$TK_LIBS $p"; fi ;;
      ?n|?n[[:space:]]|?c|?c[[:space:]]) : ;;
      *) TK_WHY="dpkg lists $p in state '$(printf '%s' "$st" | LC_ALL=C tr -cd 'a-zA-Z')', a half-done install (sudo dpkg --configure -a repairs it)"; return 0 ;;
    esac
  done
  if [ -n "$TK_VER" ]; then
    command -v nvidia-ctk >/dev/null 2>&1 && { TK_PKG=installed; return 0; }
    TK_PKG=broken; TK_WHY="nvidia-container-toolkit $TK_VER is installed and nvidia-ctk, which registers it with Docker, cannot be found"
    return 0
  fi
  if [ -n "$libs" ]; then TK_PKG=partial; TK_WHY="nvidia-container-toolkit is not installed, and some of its packages are:$libs"; return 0; fi
  TK_PKG=absent
}
# tk_refusal -> what a refused toolkit state says, with what to do. The command it prints names the packages read.
tk_refusal(){
  local guide="https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
  case "$TK_PKG" in
    partial)
      printf '%s' "$TK_WHY. The toolkit at the version deploy/kit_pins.txt pins could be older than them, and apt refuses that downgrade. Either install the toolkit by hand at their version ($guide), or remove them (${SUDO:+$SUDO }apt-get remove$TK_LIBS); then run this file again: an installed toolkit is kept at its version and only registered with Docker." ;;
    broken)
      printf '%s' "$TK_WHY. Repair that install by hand ($guide), then run this file again: an installed toolkit is kept at its version and only registered with Docker." ;;
    *)
      printf '%s' "the NVIDIA toolkit's packages cannot be read from dpkg ($TK_WHY), so whether one is installed is unknown, and nothing is installed over that. Repair dpkg, or install the toolkit by hand ($guide), then run this file again." ;;
  esac
}
# tk_version_read -> the version dpkg lists for an installed nvidia-container-toolkit, or nothing (not installed as a
# package, or not read). Reads only: TK_PKG, which decides what is installed, is tk_pkg_read's alone.
tk_version_read(){
  local out
  command -v dpkg-query >/dev/null 2>&1 || return 0
  out="$(dpkg-query -W -f='${db:Status-Abbrev}|${Version}' nvidia-container-toolkit 2>/dev/null)" || return 0
  case "${out%%|*}" in ?i|?i[[:space:]]) printf '%s' "${out#*|}" ;; esac
}
# pin_version_notes [plan] -- an Engine or a toolkit already installed is kept as it is, whatever its version. When that
# version is not the one deploy/kit_pins.txt pins, the run prints the version it found and the pinned version, and goes on. Three
# readings for each: the pin (nothing said), another version (a warning), not read (said, with the reason). Said once,
# at the plan, or, for a file downloaded alone, once the clone's pins are read (place_clone).
PIN_NOTES_DONE=0
pin_version_notes(){
  local v
  [ "$PIN_NOTES_DONE" = 1 ] && return 0
  if [ "$PINS_STATE" != read ]; then
    [ "${1:-}" = plan ] && [ "$PINS_DEFERRED" = 1 ] && return 0
    PIN_NOTES_DONE=1
    if [ "$DOCKER" != missing ] || [ "$TOOLKIT" = present ] || [ "$TK_PKG" = installed ]; then
      say "  [i] the versions of what is already installed (Docker, the NVIDIA toolkit) are not compared with a pin: $PINS_WHY."
    fi
    return 0
  fi
  PIN_NOTES_DONE=1
  if [ "$DOCKER" != missing ]; then
    v="$(docker --version 2>/dev/null | awk 'NR == 1 && $1 == "Docker" && $2 == "version" { v = $3; sub(/,$/, "", v); print v }')"
    if [ -z "$v" ]; then
      say "  [i] the version of the installed Docker was not read (docker --version named none): it is not compared with the pin, Engine $PIN_DOCKER_ENGINE ($KIT_PINS). The installed Engine is kept as it is."
    elif [ "$v" != "$PIN_DOCKER_ENGINE" ]; then
      warn "Docker $v is installed, and $KIT_PINS pins Engine $PIN_DOCKER_ENGINE: the installed Engine is kept as it is (this file never replaces one), and the installation goes on."
    fi
  fi
  [ "$GPU" != none ] || return 0
  v=""
  if [ "$TK_PKG" = installed ]; then v="$TK_VER"
  elif [ "$TOOLKIT" = present ]; then
    v="$(tk_version_read)"
    [ -n "$v" ] || say "  [i] Docker has an nvidia runtime, and dpkg names no installed nvidia-container-toolkit version: the toolkit is not compared with the pin, $PIN_NVIDIA_TOOLKIT ($KIT_PINS)."
  fi
  if [ -n "$v" ] && [ "$v" != "$PIN_NVIDIA_TOOLKIT" ]; then
    warn "nvidia-container-toolkit $v is installed, and $KIT_PINS pins $PIN_NVIDIA_TOOLKIT: it is kept at its version (only registered with Docker when the daemon has no nvidia runtime), and the installation goes on."
  fi
}

# Tools this file and join.sh call directly. They are installed even when Docker is already present: a
# host with Docker and no git would otherwise fail at the clone with a message the plan never announced.
NEED_TOOLS=""
command -v git  >/dev/null 2>&1 || NEED_TOOLS="$NEED_TOOLS git"
command -v curl >/dev/null 2>&1 || NEED_TOOLS="$NEED_TOOLS curl"

# The clone. A directory that carries .git but cannot be inspected because git is not installed is
# neither "present" nor "missing": nothing may be reset in it until it has been read.
CLONE=missing; DIRTY=""
# The copy of the miner image pin this file wrote (--miner-image-file), kept where git never looks.
PKG_PIN_MARK="$DENDRA_DIR/.git/dendra-package-miner-image"; PKG_PIN_OURS=0
# pkg_pin_is_ours returns 0 when docker/MINER_IMAGE holds exactly the bytes this file wrote there (raw bytes:
# no filter, no line-ending conversion). Asked only of a clone whose only change is that file.
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
    # The single exemption names its bytes: the pin this file wrote, alone, byte for byte. Exempting a family
    # of changes ("anything in docker/MINER_IMAGE") would let through a hand edit, or a pin of another release
    # left in place, which would then be undone, or carried to another tag, without a word.
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
# fresh_handover_read -> FRESH_HANDOVER (1 for a clone install.ps1 made for this run, see "A clone made by install.ps1"
# in the header) and FRESH_WHY, the reading that refused it, for a clone that carries the marker. Every reading must
# say so; one that cannot be read refuses, and the clone is then a clone already present.
FRESH_MARK="$DENDRA_DIR/.git/dendra-fresh-clone"
FRESH_TOKEN="${DENDRA_FRESH_CLONE:-}"
FRESH_TOKEN_RE='^[0-9a-f]{32}$'
FRESH_HANDOVER=0; FRESH_WHY=""
fresh_handover_read(){
  local mh head refl n st
  { [ -n "$FRESH_TOKEN" ] && [ -e "$FRESH_MARK" ]; } || return 0
  [[ $FRESH_TOKEN =~ $FRESH_TOKEN_RE ]] || { FRESH_WHY="DENDRA_FRESH_CLONE is not a token of the form install.ps1 writes"; return 0; }
  [ "$CLONE" = present ] || { FRESH_WHY="the clone reads as $CLONE"; return 0; }
  { [ -f "$FRESH_MARK" ] && [ -r "$FRESH_MARK" ]; } || { FRESH_WHY="$FRESH_MARK cannot be read"; return 0; }
  mh="$(awk -v t="token=$FRESH_TOKEN" 'NR == 1 && $0 == t { ok = 1 } NR == 2 && ok && $0 ~ /^head=[0-9a-f]{40}$/ { h = substr($0, 6) } END { if (NR == 2) print h }' "$FRESH_MARK" 2>/dev/null)"
  [ -n "$mh" ] || { FRESH_WHY="the marker does not carry the token this run was given, or is not its two lines (token, head)"; return 0; }
  head="$(git -C "$DENDRA_DIR" rev-parse -q --verify HEAD 2>/dev/null)"
  [ "$head" = "$mh" ] || { FRESH_WHY="HEAD (${head:-unread}) is not the commit the marker names ($mh)"; return 0; }
  refl="$(git -C "$DENDRA_DIR" log -g --format=%gs HEAD 2>/dev/null)" || { FRESH_WHY="the reflog of HEAD cannot be read"; return 0; }
  n="$(printf '%s\n' "$refl" | awk 'NF { n++ } END { print n + 0 }')"
  case "$n:$refl" in
    "1:clone: from "*) : ;;
    *) FRESH_WHY="the reflog of HEAD holds $n entries, not the one entry of the clone: HEAD moved since git clone"; return 0 ;;
  esac
  n="$(git -C "$DENDRA_DIR" for-each-ref --format='%(refname)' refs/heads 2>/dev/null | awk 'NF { n++ } END { print n + 0 }')"
  [ "$n" = 1 ] || { FRESH_WHY="the clone holds $n local branches, not the one git clone makes"; return 0; }
  st="$(git -C "$DENDRA_DIR" status --porcelain --ignored --untracked-files=all 2>/dev/null)" || { FRESH_WHY="git status cannot be read in the clone"; return 0; }
  [ -z "$st" ] || { FRESH_WHY="the clone holds files git clone did not write ($(printf '%s\n' "$st" | head -1 | cut -c1-80))"; return 0; }
  FRESH_HANDOVER=1
}
fresh_handover_read

say "  distribution : ${OS_ID:-?} ${OS_CODENAME:-} $( [ "$HIVE" = 1 ] && printf '(HiveOS)' )$( [ "$WSL" = 1 ] && printf '(WSL 2)' )"
say "  RAM          : ${RAM_MB} MB"
say "  free disk    : ${DISK_GB} GB under ${_disk_path} ${DISK_NOTE}"
say "  GPU          : $GPU"
[ "$GPU" != none ] && say "  GPUs         : $GPU_N NVIDIA card(s) ($( [ -n "$GPUS" ] && printf 'one miner identity per card selected by --gpus %s' "$GPUS" || printf 'one miner identity unless --gpus all' ))"
say "  docker       : $DOCKER$( [ "$DOCKER" != missing ] && printf ' (daemon %s, %s running container(s)%s)' "$DAEMON" "$RUNNING" "$( [ "$RUNNING" = '?' ] && printf ' -- not readable from this account yet' )" )"
say "  nvidia toolkit: $TOOLKIT"
say "  kit pins     : $(pins_line)"
say "  tools missing: ${NEED_TOOLS:-none}"
say "  clone        : $CLONE ($DENDRA_DIR)$( [ "$PKG_PIN_OURS" = 1 ] && printf ', carrying the miner image pin this installer wrote into docker/MINER_IMAGE' )"
if [ "$FRESH_HANDOVER" = 1 ]; then
  say "  [i] install.ps1 made this clone for this run, and it is still as git clone left it: it is placed on its release tag as a clone this run makes."
elif [ -n "$FRESH_WHY" ]; then
  say "  [i] this clone carries the marker of a clone install.ps1 made, and does not count as made for this run: $FRESH_WHY. It is placed as a clone already present."
fi

# ---------------------------------------------------------------- 2. what must change, and refusals
# The role of this host on the testnet, asked before anything changes, of the probe next to this file:
# deploy/hw_probe.sh --role, the same decision join.sh applies (and, through this file, install.ps1 and the
# HiveOS package). A validator serves no model and is not asked. Four answers, three of them readings:
#   miner    a usable NVIDIA card: unchanged, the role asked for applies;
#   judge    no usable card and at least MOE_CPU_MIN_RAM_MB of RAM: the judge role on the CPU, and --miner is
#            refused (no mining model runs on the CPU on the testnet);
#   refused  no usable card and less RAM: refused here, exit 2, with the probe's reason;
#   anything else (unknown because the RAM was not read, a missing probe, an answer that is not one of the
#            words, a code that does not go with it): exit 3, nothing is installed on a guess.
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
# apply_role <what has changed so far> applies the decision: it returns on a role, exits 2 when the host has
# none (or --miner without a usable card), exits 3 when it cannot be decided. Asked once: here of the probe next
# to this file, or after the clone of the clone's probe (PROBE_DEFERRED), where ROLE_STEP=after-clone records the
# word of that action (the clone is a change, so the run is no longer one that stopped before any).
ROLE_STEP=before
role_word(){ [ "$ROLE_STEP" = after-clone ] && summary "$CLONE" "$1"; return 0; }
apply_role(){
  host_role
  case "$HOST_ROLE" in
    miner) say "  role         : $ROLE (deploy/hw_probe.sh --role: miner, a usable NVIDIA card)" ;;
    judge)
      if [ "$ROLE" = miner ]; then
        refuse "--miner on a host with no usable NVIDIA GPU: the testnet runs no mining model on the CPU (deploy/hw_probe.sh --role: judge)."
        printf '%s\n' "$HOST_ROLE_WHY" | sed 's/^/           /' >&2
        say "         Run this file again without --miner: this host then joins as a judge on the CPU. $1"
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
    say "  role         : not decided yet -- no deploy/hw_probe.sh next to this file (it was downloaded alone). The"
    say "                 clone's probe decides it (--role) once the clone is placed, before Docker, the NVIDIA toolkit or"
    say "                 the docker group are touched: a host with no role is refused there, with only the clone placed."
  else
    apply_role "Nothing was changed."
  fi
fi
PLAN=""
add(){ PLAN="${PLAN}  - $1
"; }

# The judge floor is for a host that can judge. The judge role is the default, and raising the disk floor
# on every host would include one whose RAM can never clear the judge gate, where join.sh demotes it to
# the miner role anyway and the judge model is never pulled: that host would be refused for a model it
# was never going to download. So when free disk sits between the two floors, the probe
# next to this file is asked, and the probe's gate decides (MOE_CPU_MIN_RAM_MB in deploy/hw_probe.sh: the
# figure is read there, never restated here).
# Three answers, never two. `false` is a reading: the miner floor applies. `true` keeps the judge floor.
# Anything else (`unknown` because the probe could not read the RAM, a missing probe, an answer that is
# none of the three words, a failed run) decides nothing, so the judge floor stays: an unknown is never
# read as "cannot judge", which would lower a floor on a guess.
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
    # --miner is a way out only for a host that can mine: on one without a usable card it is refused (the role
    # above), so suggesting it there sent the operator to a second refusal.
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

[ "$PROBE_DEFERRED" = 1 ] && [ "$ROLE" != validator ] && add "In this order, since no probe sits next to this file: the tools git needs (when listed below), then the clone, then the role from the clone's deploy/hw_probe.sh --role -- a host with no role stops there (exit 2, or 3 when it cannot be decided), with the clone placed -- and only then Docker, the NVIDIA toolkit and the docker group"
NEED_DOCKER_REPO=0; COMPOSE_PKG=""
case "$DOCKER" in
  missing)
    if [ "$APT" = 1 ]; then
      [ -n "$OS_CODENAME" ] || { refuse "the distribution codename could not be read from $OS_RELEASE, and Docker's repository needs it."; exit 2; }
      NEED_DOCKER_REPO=1
      add "install Docker Engine + Compose v2 from https://download.docker.com/linux/${DOCKER_DISTRO} (${OS_CODENAME}): $(pin_words docker); another key, or a version that repository does not offer for ${OS_CODENAME}, stops the install there"
    else
      refuse "Docker is missing and ${OS_ID:-this distribution} is not apt-based: install it by hand first -> https://docs.docker.com/engine/install/"
      exit 2
    fi;;
  present-without-compose)
    if [ "$APT" = 1 ]; then
      # The package that fits the Engine already installed: Ubuntu's docker-compose-v2 depends on
      # docker.io, so on a docker-ce Engine it would drag docker.io in and collide. Choose by the
      # installed Engine, never by which package happens to be available.
      if [ "$DOCKER_IO" = 1 ] && apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        COMPOSE_PKG=docker-compose-v2
        add "install Compose v2 as the distribution's docker-compose-v2 package (the installed docker.io Engine is kept; the package comes at the version of the distribution's release, which deploy/kit_pins.txt does not pin)"
      else
        [ -n "$OS_CODENAME" ] || { refuse "the distribution codename could not be read from $OS_RELEASE, and Docker's repository needs it."; exit 2; }
        NEED_DOCKER_REPO=1; COMPOSE_PKG=docker-compose-plugin
        add "install Compose v2 as docker-compose-plugin from https://download.docker.com/linux/${DOCKER_DISTRO} (the installed Engine is kept): $(pin_words compose); another key, or a version that repository does not offer, stops the install there"
      fi
    else
      refuse "Docker is installed without Compose v2, and ${OS_ID:-this distribution} is not apt-based: install the compose plugin by hand -> https://docs.docker.com/compose/install/"
      exit 2
    fi;;
esac
# The pins are needed when this file installs from Docker's or NVIDIA's repository (pins_require). Next to this file
# they were read above, and a file that is missing or wrong stops the run here, before anything changes. gpg reads
# the signing keys, so it is one of the tools this file installs when it is missing.
# The toolkit's packages are read once, here: a state that is refused stops the run now when the daemon is known to
# have no nvidia runtime, and once the daemon answers when that is not known yet ("?").
if { [ "$TOOLKIT" = absent ] || [ "$TOOLKIT" = "?" ]; } && [ "$APT" = 1 ]; then
  tk_pkg_read
  case "$TK_PKG" in
    installed|absent) : ;;
    *) if [ "$TOOLKIT" = absent ]; then refuse "$(tk_refusal) Nothing was changed."; exit 2; fi ;;
  esac
fi
# An installed Engine or toolkit at another version than the pin: said, and kept (pin_version_notes).
pin_version_notes plan
PINS_NEEDED=0
[ "$NEED_DOCKER_REPO" = 1 ] && PINS_NEEDED=1
[ "$TK_PKG" = absent ] && PINS_NEEDED=1
if [ "$PINS_NEEDED" = 1 ]; then
  if [ "$PINS_DEFERRED" = 0 ] && [ "$PINS_STATE" != read ]; then
    refuse "$PINS_WHY. What this file installs from Docker's or NVIDIA's repository is installed at the versions, and with the signing keys, that file pins, and without them nothing is installed from there. Run the deploy/install.sh of a release, next to its deploy/kit_pins.txt; or install Docker$( [ "$GPU" != none ] && printf ' and the NVIDIA container toolkit' ) by hand first, then run this file again: an installed Engine or toolkit is kept as it is. Nothing was changed."
    exit 2
  fi
  command -v gpg >/dev/null 2>&1 || case " $NEED_TOOLS " in *" gnupg "*) : ;; *) NEED_TOOLS="$NEED_TOOLS gnupg" ;; esac
fi
[ "$DAEMON" = down-or-unreadable ] && add "start the Docker daemon if it is not running (systemctl enable --now docker, or service docker start) -- it may simply be unreadable from this account until the group applies"
if [ -n "$NEED_TOOLS" ]; then
  if [ "$APT" = 1 ]; then add "install missing tools:${NEED_TOOLS}"
  else refuse "these tools are missing and ${OS_ID:-this distribution} is not apt-based:${NEED_TOOLS}"; exit 2; fi
fi
if [ "$TOOLKIT" = absent ] || [ "$TOOLKIT" = "?" ]; then
  if [ "$APT" = 1 ]; then
    _rr="the running container(s) stop and restart"
    [ "$RUNNING" != "?" ] && _rr="the $RUNNING running container(s) stop and restart"
    case "$TK_PKG" in
      installed) add "register the installed nvidia-container-toolkit $TK_VER with Docker if the daemon has no nvidia runtime (nvidia-ctk runtime configure; the package is kept at its version, whatever deploy/kit_pins.txt pins), then restart the Docker daemon -- $_rr" ;;
      absent) add "install nvidia-container-toolkit from $NVIDIA_REPO if the daemon has no nvidia runtime ($(pin_words nvidia); another key, or a version that repository does not offer, stops the install there), register it, then restart the Docker daemon -- $_rr" ;;
      *) add "stop there if the daemon has no nvidia runtime, with nothing installed for the toolkit: $(tk_refusal)" ;;
    esac
  else
    warn "GPU present, toolkit not confirmed, no apt: install it by hand -> https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html (without it Docker cannot use the card, and join.sh decides as for a host without one: deploy/hw_probe.sh --role --no-gpu, a judge on the CPU or refused)"
  fi
fi
[ "$IN_GROUP" = 0 ] && add "add $(id -un) to the docker group (usermod -aG docker: membership of that group is equivalent to root on this host)"
# Without --ref, the epoch the release is chosen by is read now when curl is here, so that a network-info that
# declares none is refused before anything changes; without curl it is read once the tools are installed.
if [ "$REF_RESOLVE" = 1 ] && command -v curl >/dev/null 2>&1; then
  net_epoch_read
  net_epoch_gate "Nothing was changed."
  # A clone already present above that epoch is refused here, before anything changes (clone_epoch_gate).
  clone_epoch_gate
fi
REL_WORDS="the release tag $REF"
if [ "$REF_RESOLVE" = 1 ]; then
  if [ "$NET_EPOCH_STATE" = read ]; then
    REL_WORDS="the newest release tag that runs the rules $CONFIG_URL declares, resolved from the tags origin publishes"
  else
    REL_WORDS="the newest release tag that runs the rules $CONFIG_URL declares (read once curl is installed; a file that declares none is refused there)"
  fi
fi
case "$CLONE" in
  missing)    if [ -n "$REF" ]; then add "clone the release tag $REF of $DENDRA_REPO_URL into $DENDRA_DIR (never main)"
              else add "clone $DENDRA_REPO_URL into $DENDRA_DIR and check out $REL_WORDS (never main; refused when no release tag of those rules exists)"; fi;;
  present)    add "place the clone in $DENDRA_DIR on $REL_WORDS (never main; refused if it carries local commits, if a tag moved, or if the tag's tree would overwrite files)";;
  dirty)      refuse "the clone in $DENDRA_DIR has local changes to tracked files; moving it to a release tag would carry or discard them."
              printf '%s\n' "$DIRTY" | head -5 | sed 's/^/           /'
              say "         This file only moves a clone that carries no local work -- a commit here would be"
              say "         left behind too. Move your changes to another clone, or point DENDRA_DIR elsewhere."
              exit 2;;
  unreadable) refuse "$DENDRA_DIR is a git clone but git is not installed, so it cannot be inspected before it moves. Install git first (apt-get install git), or point DENDRA_DIR elsewhere."; exit 2;;
  foreign)    refuse "$DENDRA_DIR is a git clone that does not carry deploy/join.sh: not this repository. Point DENDRA_DIR elsewhere."; exit 2;;
  occupied)   refuse "$DENDRA_DIR exists and is not a git clone. Point DENDRA_DIR elsewhere."; exit 2;;
esac
[ "$PKG_PIN_OURS" = 1 ] && add "undo the miner image pin this installer wrote into docker/MINER_IMAGE (the clone's only local change, byte for byte what it wrote) before the clone moves"
case "$PKG_PIN_STATE" in
  ok)       add "once the clone sits on $REF: write the miner image of $REF the package carries ($PKG_PIN_FILE) into the clone's docker/MINER_IMAGE, if the tag names none (never over a value); join.sh then judges it -- official digest, built-for gates, platform -- or builds the image from the clone";;
  unusable) warn "$PKG_PIN_WHY: nothing will be written, and join.sh builds the miner image from the clone.";;
esac
JOIN_ARGS=""
# The miner role declines the judge seat on purpose: join.sh seats one by default on a host that can judge, so
# --miner alone would still seat it; --no-judge records the refusal, which a re-run and the automatic update keep.
case "$ROLE" in judge) JOIN_ARGS="--judge";; miner) JOIN_ARGS="--miner --no-judge";; validator) JOIN_ARGS="--validator";; esac
[ "$LIGHT" = 1 ] && [ "$ROLE" != validator ] && JOIN_ARGS="$JOIN_ARGS --remote-rpc"
[ "$AUTO_UPDATE" = 0 ] && [ "$ROLE" != validator ] && JOIN_ARGS="$JOIN_ARGS --no-auto-update"
[ "$PAYOUT_LOCK" = 1 ] && JOIN_ARGS="$JOIN_ARGS --payout-lock"
# Their characters were checked above (letters and digits only): they can sit in the command string below.
[ -n "$PAYOUT" ] && JOIN_ARGS="$JOIN_ARGS --payout-address $PAYOUT"
[ -n "$OWNER" ] && JOIN_ARGS="$JOIN_ARGS --owner $OWNER"
# Validated above (GPUS_SPEC_RE): letters, digits, commas and dashes only.
[ -n "$GPUS" ] && JOIN_ARGS="$JOIN_ARGS --gpus $GPUS"
# Validated above (PLAN_NAME_RE): letters, digits and . _ - only.
[ -n "$AFTER_UPGRADE" ] && JOIN_ARGS="$JOIN_ARGS --after-upgrade $AFTER_UPGRADE"
add "run: CONFIG_URL=$CONFIG_URL bash deploy/join.sh $JOIN_ARGS   (from $DENDRA_DIR)"
[ -n "$AFTER_UPGRADE" ] && add "join.sh --after-upgrade $AFTER_UPGRADE: the join after that governed upgrade, for this run only. It changes something only where that upgrade explains a difference: a clone one upgrade ahead of the rules network-info.txt declares, or a node pinned to the image of earlier rules. There, it goes on only once the chain confirms this kit's node at the plan's halt (the node is then started on the clone's node image) or answers the plan applied; otherwise join.sh refuses before it starts the node (deploy/join.sh::verify_consensus_epoch, node_env_image_reconcile)"
if [ -n "$GPUS" ]; then
  add "join.sh --gpus $GPUS: one miner identity per card -- each takes one faucet drip to register (the faucet caps drips per IP and per day: DENDRA_FAUCET_IP_DAILY; a refused identity retries by itself), locks its own stake (min_stake, read on chain), and has its own 24-word recovery phrase to write down. It needs a payout address (--payout-address, or one already in the kit). The free disk floor above is for one model: cards of different sizes take one model each. Its own plan, read on this host, writing nothing: bash deploy/join.sh --gpus $GPUS --plan"
fi
# What join.sh schedules is part of what --yes accepts, so it is in the plan rather than discovered later.
[ "$ROLE" != validator ] && add "join.sh then adds one hourly line to this user's crontab: the miner's self-test (deploy/testnet-miner/miner_health.sh --cron, read only: it writes nothing on the relay), whose file deploy/testnet-miner/miner-health.ALERT exists while a check fails (an alert) or could not be measured (a note)"
if [ "$ROLE" != validator ]; then
  if [ "$AUTO_UPDATE" = 0 ]; then add "automatic update: off (--no-auto-update, DENDRA_AUTO_UPDATE=0 in the kit's .env): the kit is updated by hand, with the instruction above"
  else add "automatic update: on. join.sh adds one daily crontab line, deploy/testnet-miner/kit_update.sh --cron: it moves the clone to a newer release tag only when the tag verifies with the clone's docker/RELEASE_SIGNERS and carries the same network rules, never to a branch; it prints its plan, logs under the kit, keeps the tree it leaves on kit-before-update, replays this join and rolls back when that fails. --no-auto-update turns it off"; fi
fi
if [ "$CLONE" = present ]; then
  add "verify the signature of $( [ -n "$REF" ] && printf 'the tag %s' "$REF" || printf 'that tag' ) before the clone moves, with the docker/RELEASE_SIGNERS of the tree it sits on, which must be the file this kit recorded (DENDRA_RELEASE_SIGNERS_SHA256), never with the file the tag ships; a tree without that file uses the tag's own file only when it is the recorded one, and with neither a file nor a record the tag is verified once placed, with the file of its tree, as a first installation is. A tag that does not verify, or that does not name itself, stops the install with nothing started (the key's fingerprint is printed)"
else
  add "verify the signature of $( [ -n "$REF" ] && printf 'the tag %s' "$REF" || printf 'that tag' ) with the signers file of its own tree (docker/RELEASE_SIGNERS; the key's fingerprint is printed): a tag that does not verify, or that does not name itself, stops the install there, with nothing started. A first installation trusts HTTPS to the forge for that file, as it trusts the genesis pin: join.sh records its SHA-256 in the kit's .env, refuses a file that differs at every later run, and the automatic update verifies every candidate tag with the recorded file"
fi
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

summary(){ # summary <clone_state> <action>
  printf 'DENDRA_INSTALL_SUMMARY distro=%s codename=%s hive=%s wsl=%s docker=%s daemon=%s toolkit=%s gpu=%s disk_gb=%s ram_mb=%s clone=%s action=%s\n' \
    "${OS_ID:-?}" "${OS_CODENAME:-?}" "$HIVE" "$WSL" "$DOCKER" "$DAEMON" "$TOOLKIT" "$(printf '%s' "$GPU" | tr ' ' '_')" "$DISK_GB" "$RAM_MB" "$1" "$2"
  # The action, for a caller that cannot read this output (the HiveOS package shows it on screen): one word in
  # DENDRA_INSTALL_RESULT_FILE, written whole (renamed into place). The exit code alone cannot tell apart the
  # two meanings of 3: "nothing decided, nothing installed" and "join.sh started the miner and could not
  # measure its health" (action=join_unmeasured). A run that stops before any action (an early refusal)
  # writes nothing.
  if [ -n "${DENDRA_INSTALL_RESULT_FILE:-}" ]; then
    printf '%s\n' "$2" > "$DENDRA_INSTALL_RESULT_FILE.tmp" 2>/dev/null \
      && mv -f "$DENDRA_INSTALL_RESULT_FILE.tmp" "$DENDRA_INSTALL_RESULT_FILE" 2>/dev/null
  fi
  return 0
}

if [ "$YES" != 1 ]; then
  if [ "$MISSING" = 0 ]; then
    say "  nothing to install on this host. Re-run with --yes to place the clone on its release tag and start join.sh."
    summary "$CLONE" planned
    exit 0
  fi
  say ""
  say "  $MISSING change(s) needed. Nothing was done. Re-run with --yes to apply the plan above."
  summary "$CLONE" planned
  exit 2
fi
[ "$CHECK" = 1 ] && { say "  --check with --yes: the check wins, nothing is done."; summary "$CLONE" planned; exit 0; }

# ---------------------------------------------------------------- 3. apply
step(){ printf '\n== [install] %s ==\n' "$*"; }
fail(){ printf '  [FAILED] %s\n' "$*" >&2; refusal_link "$(site_topic "$*")"; summary "$CLONE" failed; exit 1; }
# systemd is detected the way systemd itself recommends (sd_booted: the directory it creates at
# boot), not by `is-system-running`, whose exit code is non-zero for `degraded`: a running systemd
# with one failed unit, the usual state under WSL and on rigs.
has_systemd(){ [ -d "$SYSTEMD_DIR" ]; }
daemon_start(){
  if has_systemd; then $SUDO systemctl enable --now docker; else $SUDO service docker start; fi
}
daemon_restart(){
  if has_systemd; then $SUDO systemctl restart docker; else $SUDO service docker restart; fi
}

# key_fpr_check <key file> <pinned fingerprint> -> 0 when the file holds exactly one primary key and its fingerprint is
# the pinned one; 1 otherwise, with KEY_WHY. gpg reads the file in a home of its own, so no keyring of this user changes.
KEY_WHY=""
key_fpr_check(){
  local h out rc fprs n
  KEY_WHY=""
  command -v gpg >/dev/null 2>&1 || { KEY_WHY="gpg is not installed, so the key could not be read"; return 1; }
  h="$(mktemp -d)" || { KEY_WHY="no temporary directory for gpg"; return 1; }
  out="$(gpg --batch --quiet --homedir "$h" --show-keys --with-colons "$1" 2>/dev/null)"; rc=$?
  rm -rf "$h"
  [ "$rc" = 0 ] || { KEY_WHY="gpg could not read the file as a key (exit $rc)"; return 1; }
  fprs="$(printf '%s' "$out" | awk -F: '$1 == "pub" { p = 1; next } p && $1 == "fpr" { print toupper($10); p = 0 }')"
  n="$(printf '%s' "$fprs" | awk 'NF { n++ } END { print n + 0 }')"
  [ "$n" = 1 ] || { KEY_WHY="the file holds $n primary keys ($(printf '%s' "$fprs" | awk 'NF { printf "%s ", $0 }')), and the pin names one"; return 1; }
  [ "$fprs" = "$2" ] || { KEY_WHY="its fingerprint is $fprs"; return 1; }
  return 0
}
# apt_pinned <package> <ERE of the version> <source> -> APT_V: the one version of <package> that apt offers from a
# source whose address holds <source> and that matches the expression; 1 otherwise, with APT_N the number that match.
APT_V=""; APT_N=0
apt_pinned(){
  local all v
  APT_V=""; APT_N=0
  all="$(apt-cache madison "$1" 2>/dev/null | awk -F'|' -v src="$3" 'index($3, src) { v = $2; gsub(/[[:space:]]/, "", v); print v }' | sort -u)"
  for v in $all; do
    [[ $v =~ $2 ]] || continue
    APT_N=$((APT_N + 1)); APT_V="$v"
  done
  [ "$APT_N" = 1 ] && return 0
  APT_V=""; return 1
}
# docker_pkg_pin <package> <upstream version> -> APT_PIN (<package>=<the build of that version for this codename>), or
# the run stops here (exit 2), with nothing installed from Docker's repository. On a host that had no Docker, the
# repository file this run wrote is removed again; next to an installed Engine it is left as that Engine uses it.
DOCKER_SRC="https://download.docker.com/linux/"
APT_PIN=""
docker_pkg_pin(){
  local left="The repository file was left in place for the installed Engine."
  APT_PIN=""
  if apt_pinned "$1" "^([0-9]+:)?${2//./[.]}-[0-9]+~" "$DOCKER_SRC"; then APT_PIN="$1=$APT_V"; return 0; fi
  if [ "$DOCKER" = missing ]; then
    $SUDO rm -f "$SYSROOT/etc/apt/sources.list.d/docker.list"; left="The repository file was removed again."
  fi
  refuse "Docker's repository for ${DOCKER_DISTRO} ${OS_CODENAME} offers $APT_N build(s) of $1 $2, the version $KIT_PINS pins, and this file installs exactly one. Nothing was installed from it. $left $(docker_by_hand)"
  summary "$CLONE" refused; exit 2
}
# docker_by_hand -> what to do when the pinned build is not there, read on this host. Docker builds each release for
# the distribution releases it supports at that time, so an older or newer codename can have no build of the pin at
# all; the distribution's own docker.io (and docker-compose-v2, where apt offers it) is then named, with the command.
docker_by_hand(){
  local s="${SUDO:+$SUDO }" compose_doc="https://docs.docker.com/compose/install/linux/"
  if [ "$DOCKER" = missing ]; then
    if apt-cache show docker.io >/dev/null 2>&1 && apt-cache show docker-compose-v2 >/dev/null 2>&1; then
      printf '%s' "Install Docker by hand, either from Docker's instructions for a release they support (https://docs.docker.com/engine/install/) or as this distribution's own packages (${s}apt-get install docker.io docker-compose-v2), then run this file again: an installed Engine is kept as it is."
    elif apt-cache show docker.io >/dev/null 2>&1; then
      printf '%s' "Install Docker by hand, either from Docker's instructions for a release they support (https://docs.docker.com/engine/install/) or as this distribution's own Engine (${s}apt-get install docker.io) with Compose v2 by hand ($compose_doc), then run this file again: an installed Engine is kept as it is."
    else
      printf '%s' "Install Docker Engine and Compose v2 by hand (https://docs.docker.com/engine/install/), then run this file again: an installed Engine is kept as it is."
    fi
  else
    printf '%s' "Install Compose v2 by hand next to the installed Engine ($compose_doc), then run this file again: the installed Engine is kept as it is."
  fi
}

# A repository file that is written and whose first `apt-get update` then fails is removed again:
# left in place it makes every later `apt-get update` on the host fail, long after this file is gone.
add_docker_repo(){
  local _k
  pins_require "Docker's repository"
  $SUDO install -m 0755 -d "$SYSROOT/etc/apt/keyrings" || fail "mkdir /etc/apt/keyrings"
  # The key is read before it is trusted: downloaded to a file of this run, its fingerprint compared with the pin,
  # and only then written where apt reads it.
  _k="$(mktemp)" || fail "a temporary file for Docker's signing key"
  curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" -o "$_k" || { rm -f "$_k"; fail "download of Docker's signing key"; }
  if ! key_fpr_check "$_k" "$PIN_DOCKER_KEY"; then
    rm -f "$_k"
    refuse "the signing key https://download.docker.com/linux/${DOCKER_DISTRO}/gpg serves is not the one $KIT_PINS pins (DOCKER_APT_KEY_FINGERPRINT=$PIN_DOCKER_KEY): $KEY_WHY. Nothing was written to apt's sources. Compare it with the fingerprint Docker publishes (https://docs.docker.com/engine/install/); until a release of the kit pins another key, install Docker Engine and Compose v2 by hand from there, then run this file again: an installed Engine is kept as it is."
    summary "$CLONE" refused; exit 2
  fi
  $SUDO install -m 0644 "$_k" "$SYSROOT/etc/apt/keyrings/docker.asc" || { rm -f "$_k"; fail "writing Docker's signing key"; }
  rm -f "$_k"
  $SUDO chmod a+r "$SYSROOT/etc/apt/keyrings/docker.asc"
  ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$ARCH" "$DOCKER_DISTRO" "$OS_CODENAME" | $SUDO tee "$SYSROOT/etc/apt/sources.list.d/docker.list" >/dev/null || fail "writing docker.list"
  if ! $SUDO apt-get update -qq; then
    $SUDO rm -f "$SYSROOT/etc/apt/sources.list.d/docker.list"
    fail "apt-get update with Docker's repository (the repository file was removed again)"
  fi
}

# clone_work_check -- a present clone carries no local work, or the run stops here (exit 2). Measured before anything
# moves, and before anything is fetched: HEAD must be a published commit, that is the tip origin last served, a tag,
# or an earlier publication. A failed `git pull` leaves a clone on an earlier publication: each release republishes
# the repository as a new root commit, so the pull fetches the new root, refuses the merge ("unrelated histories")
# and leaves HEAD where it was. That publication is recognised by what origin served, read in the reflogs
# (refs/remotes/origin/<branch>, refs/remotes/origin/HEAD, and the "clone: from" entry of HEAD, since `git clone`
# logs the publication a clone started from there only), or, once those entries expired (gc.reflogExpire), by what
# it is: a root commit, as every publication is (publish_release.sh issues one root commit per publication). The one
# local commit that is also a root, an amended publication, is not lost either: place_clone keeps the tree the
# clone leaves on the branch kit-before-update. A HEAD that no sign recognises is refused. Measured once per run.
CLONE_WORK_CHECKED=0; CLONE_FRESH=0
clone_work_check(){
  local _base _head _tip _served _served_rc _pub _parents _local
  [ "$CLONE_WORK_CHECKED" = 1 ] && return 0
  _base="$(git -C "$DENDRA_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
  [ -n "$_base" ] || _base=main
  _head="$(git -C "$DENDRA_DIR" rev-parse -q --verify HEAD 2>/dev/null)"
  _tip="$(git -C "$DENDRA_DIR" rev-parse -q --verify "refs/remotes/origin/$_base" 2>/dev/null)"
  if [ -z "$_head" ]; then
    refuse "the clone in $DENDRA_DIR has no readable HEAD: it cannot be told whether it carries local work. Clone afresh into another DENDRA_DIR."
    summary "$CLONE" refused; exit 2
  fi
  if [ "$_head" != "$_tip" ] && ! git -C "$DENDRA_DIR" describe --exact-match --tags HEAD >/dev/null 2>&1; then
    _pub=""
    _served="$(git -C "$DENDRA_DIR" log -g --format=%H "refs/remotes/origin/$_base" 2>/dev/null)"; _served_rc=$?
    _served="$_served
$(git -C "$DENDRA_DIR" log -g --format=%H refs/remotes/origin/HEAD 2>/dev/null)
$(git -C "$DENDRA_DIR" log -g --format='%H %gs' HEAD 2>/dev/null | sed -n 's/^\([0-9a-f]*\) clone: from .*/\1/p')"
    if [ -n "$_head" ] && [ "$_served_rc" = 0 ] && printf '%s\n' "$_served" | grep -Fqx -- "$_head"; then
      _pub="origin served it before (its reflogs say so)"
    fi
    # `rev-list --parents` prints the commit, then its parents: the commit alone means a root.
    _parents="$(git -C "$DENDRA_DIR" rev-list --parents -n 1 HEAD 2>/dev/null)"
    if [ -z "$_pub" ] && [ -n "$_head" ] && [ "$_parents" = "$_head" ]; then
      _pub="it is a root commit, as every publication is (its reflog entries may have expired)"
    fi
    if [ -z "$_pub" ]; then
      # Counted without a root: on top of an earlier publication (a failed pull), that root is not local work.
      _local="$(git -C "$DENDRA_DIR" rev-list --count --min-parents=1 "refs/remotes/origin/$_base..HEAD" 2>/dev/null)"
      refuse "the clone in $DENDRA_DIR carries ${_local:-unread} local commit(s) beyond what it received from origin/$_base, and it sits on neither a tag nor an earlier publication: moving it to a release tag would leave that work behind."
      git -C "$DENDRA_DIR" log --oneline -5 HEAD 2>/dev/null | head -5 | sed 's/^/           /'
      [ "$_served_rc" = 0 ] || say "         (the reflog of origin/$_base could not be read, so an earlier publication cannot be told from local work.)"
      say "         Move that work to another clone, or point DENDRA_DIR elsewhere. Nothing was changed."
      summary "$CLONE" refused; exit 2
    fi
    say "  [i] HEAD ($_head) is an earlier publication, not a local commit: $_pub."
    say "      A failed 'git pull' leaves a clone exactly here: each release republishes the repository as a new root"
    say "      commit, so git pull refuses ('refusing to merge unrelated histories') and the tree stays old."
  fi
  CLONE_WORK_CHECKED=1
}

# Origin's tags are read under a namespace of this file (TAG_NS), never straight into refs/tags: git refuses to move a
# tag this clone already holds, and a tag re-made upstream would then stop the reading of every other tag. A tag
# enters refs/tags only once chosen (tag_adopt), and only when the clone holds none of that name or the same object.
TAG_NS=refs/dendra-install-tags
tag_ns_clear(){
  git -C "$DENDRA_DIR" for-each-ref --format='%(refname)' "$TAG_NS/" 2>/dev/null \
    | while read -r _r; do git -C "$DENDRA_DIR" update-ref -d "$_r" 2>/dev/null; done
  return 0
}
# tag_ns_fetch <tag> -- origin's tag of that name, fetched into TAG_NS; a fetch that fails is a refusal (exit 2).
tag_ns_fetch(){
  if ! _fe="$(git -C "$DENDRA_DIR" fetch --quiet --no-tags origin "+refs/tags/$1:$TAG_NS/$1" 2>&1)"; then
    tag_ns_clear
    refuse "the tag $1 could not be fetched from $DENDRA_REPO_URL. Nothing was started. git said:"
    printf '%s\n' "$_fe" | head -6 | sed 's/^/           /'
    fresh_clone_drop
    summary "$CLONE" refused; exit 2
  fi
}
# tag_header <ref> <field> -> one field of the tag object's header (object, type, tag), read before its first blank line.
tag_header(){ git -C "$DENDRA_DIR" cat-file tag "$1" 2>/dev/null | tr -d '\r' | awk -v k="$2" 'NF == 0 { exit } $1 == k { print $2; exit }'; }
# tag_adopt <tag> -- the tag fetched into TAG_NS becomes refs/tags/<tag>. It must be a tag object of a commit whose
# header names <tag>: a signature covers the header, not the name a forge publishes it under, so the signed object of
# an older release republished under a newer name would otherwise pass for the newest one. A clone that holds another
# object under that name (a tag re-made upstream) is refused with the gesture that follows it on purpose.
tag_adopt(){
  local t="$1" n ty oid here
  n="$(tag_header "$TAG_NS/$t" tag)"; ty="$(tag_header "$TAG_NS/$t" type)"
  if [ "$n" != "$t" ] || [ "$ty" != commit ]; then
    tag_ns_clear
    refuse "the tag $t that $DENDRA_REPO_URL publishes is not a release tag object of that name (its header names ${n:-no tag}, of type ${ty:-unread}): the signed object of another release published under a newer name is how an older release would pass for the newest one, and it is not followed. Nothing was started."
    fresh_clone_drop
    summary "$CLONE" refused; exit 2
  fi
  oid="$(git -C "$DENDRA_DIR" rev-parse -q --verify "$TAG_NS/$t" 2>/dev/null)"
  here="$(git -C "$DENDRA_DIR" rev-parse -q --verify "refs/tags/$t" 2>/dev/null)"
  if [ -n "$here" ] && [ "$here" != "$oid" ]; then
    tag_ns_clear
    refuse "this clone holds a tag $t that differs from the $t $DENDRA_REPO_URL publishes now. A tag re-made upstream is not followed by itself: a forge that moves a tag is what the signature check guards against. If the project announced that it re-made $t, remove this clone's copy, then run this file again as you just did: git -C $(printf '%q' "$DENDRA_DIR") tag -d $t (the tag fetched then is verified like any other). Nothing was started."
    summary "$CLONE" refused; exit 2
  fi
  if [ -z "$here" ] && ! _ur="$(git -C "$DENDRA_DIR" update-ref "refs/tags/$t" "$oid" "" 2>&1)"; then
    tag_ns_clear
    refuse "the tag $t could not be written into this clone (refs/tags/$t). Nothing was started. git said:"
    printf '%s\n' "$_ur" | head -4 | sed 's/^/           /'
    fresh_clone_drop
    summary "$CLONE" refused; exit 2
  fi
  tag_ns_clear
}
# resolve_ref -- without --ref: REF becomes the newest release tag origin publishes whose docker/CONSENSUS_EPOCH reads
# NET_EPOCH on line 1 (the rule of the header, the same as deploy/join.sh::release_tag_of_epoch). The tags are listed
# on origin and fetched by their full name into TAG_NS, newest first, until one carries that epoch: `git fetch origin`
# brings none of the tags off main (each release is a new root commit). The chosen tag is then adopted (tag_adopt).
# No release of that epoch is a refusal: this file never falls back to main. The tag's signature is verified by
# place_clone, before the clone moves when it was already there.
resolve_ref(){
  local lsr names t e seen=""
  step "the release tag that runs the rules $CONFIG_URL declares"
  lsr="$(git -C "$DENDRA_DIR" ls-remote --tags origin 2>&1)" || { printf '%s\n' "$lsr" | head -4 | sed 's/^/           /' >&2; fresh_clone_drop; fail "git ls-remote --tags origin (the release tags of $DENDRA_REPO_URL could not be listed)"; }
  names="$(printf '%s\n' "$lsr" | awk 'NF >= 2 { print $2 }' | sed -n 's#^refs/tags/##p' | grep -v '[{][}]$' \
           | grep -E "$REF_RE" | sort -rV)"
  tag_ns_clear
  for t in $names; do
    tag_ns_fetch "$t"
    e="$(git -C "$DENDRA_DIR" show "$TAG_NS/$t:docker/CONSENSUS_EPOCH" 2>/dev/null | head -1 | tr -dc 0-9)"
    if [ "$e" = "$NET_EPOCH" ]; then
      REF="$t"
      tag_adopt "$t"
      say "  [OK] $t is the newest release tag of $DENDRA_REPO_URL that runs the rules $CONFIG_URL declares${seen:+ (newer tags with other rules:$seen)}"
      return 0
    fi
    seen="$seen $t"
  done
  tag_ns_clear
  refuse "no release tag of $DENDRA_REPO_URL runs the rules $CONFIG_URL declares${seen:+ (tags read:$seen)}: there is no release to install, and this file never falls back to main. Nothing was started."
  fresh_clone_drop
  summary "$CLONE" refused; exit 2
}
# fresh_clone_drop -- the clone this run made, with nothing checked out in it yet, is removed before the run stops:
# left in place, its empty working tree reads as local deletions, and the next run would refuse it as local work.
# Only a clone this run made (CLONE_FRESH, set when DENDRA_DIR did not exist); a clone that was there is never removed.
fresh_clone_drop(){
  [ "$CLONE_FRESH" = 1 ] || return 0
  rm -rf "$DENDRA_DIR" && say "         The clone this run made in $DENDRA_DIR is removed: nothing was checked out in it."
  CLONE=missing; CLONE_FRESH=0
}

# ref_verify <signers file> <where that file comes from> <what a refusal leaves> -- the signature of the release tag REF
# with that file, under git's own namespace. Three ways out, none silent: verified (the key's
# fingerprint and the file's digest printed, for the record join.sh writes), refused (a signature that does not
# verify: nothing is started), not measured (no ssh-keygen: git cannot check an SSH signature without it).
REF_SIG_DONE=0
ref_verify(){
  local _sg="$1" _src="$2" _left="$3" _d _blk _vt
  if ! command -v ssh-keygen >/dev/null 2>&1; then
    summary "$CLONE" role_unmeasured
    unmeasurable "ssh-keygen is not installed, so git cannot verify the SSH signature of $REF, and not measured is not a pass: apt-get install openssh-client, then run this file again. $_left"
  fi
  # Only an SSH signature is verified: the tag's signature block is read first, and git runs with gpg.program
  # pointed at /bin/false, since a tag signed with gpg would otherwise be checked against this host's gpg keyring,
  # where the signers file counts for nothing.
  _d='-----'  # the dashes of a block line are composed here, so no key-block marker is written in this file
  _blk="$(git -C "$DENDRA_DIR" cat-file tag "refs/tags/$REF" 2>/dev/null | grep "^${_d}BEGIN " | tail -1)"
  if [ "$_blk" != "${_d}BEGIN SSH SIGNATURE${_d}" ]; then
    refuse "the tag $REF carries ${_blk:-no signature block}: the project signs its releases with an SSH key named in docker/RELEASE_SIGNERS, and nothing else is verified. $_left"
    summary "$CLONE" refused; exit 2
  fi
  if _vt="$(git -C "$DENDRA_DIR" -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile="$_sg" verify-tag "refs/tags/$REF" 2>&1)"; then
    say "  [OK] the tag $REF verifies with $_src (key $(printf '%s' "$_vt" | grep -o 'SHA256:[A-Za-z0-9+/=]*' | head -1), file sha256 $(sha256sum "$_sg" 2>/dev/null | cut -d' ' -f1)). join.sh records that file in the kit's .env."
    REF_SIG_DONE=1
  else
    refuse "the tag $REF does not verify with $_src. $_left git said:"
    printf '%s\n' "$_vt" | head -4 | sed 's/^/           /' >&2
    say "         Reading, for anyone: git -c gpg.ssh.allowedSignersFile=docker/RELEASE_SIGNERS verify-tag $REF, in that clone."
    summary "$CLONE" refused; exit 2
  fi
}
# present_trust -- a clone that was there verifies REF before it moves, with the trust root it already holds, never
# with the file the tag ships (the forge serves both, so a tag verified with its own file proves only that the forge
# is consistent). The trust root is the docker/RELEASE_SIGNERS of the tree the clone sits on, and it must be the file
# the kit recorded (DENDRA_RELEASE_SIGNERS_SHA256, written by join.sh in the .env of the miner kit or of the node kit,
# both read whatever role this run asks for). Without that file, the tag's own file is used only when it is the
# recorded one. With neither a file nor a record there is no trust root yet: the tag is verified once the clone sits
# on it, as for a first installation (REF_SIG_DONE stays 0), and the run prints why.
present_trust(){
  local cur="$DENDRA_DIR/docker/RELEASE_SIGNERS" envf recs="" r sha tmp
  step "the signature of the release tag $REF, before the clone moves"
  for envf in "$DENDRA_DIR/deploy/testnet-miner/.env" "$DENDRA_DIR/deploy/testnet-node/.env"; do
    [ -e "$envf" ] || continue
    if [ ! -r "$envf" ]; then
      refuse "$envf cannot be read, so whether this kit recorded a docker/RELEASE_SIGNERS is unknown, and the tag is not verified without that answer. Nothing was changed."
      summary "$CLONE" refused; exit 2
    fi
    r="$(tr -d '\r' < "$envf" | sed -n 's/^DENDRA_RELEASE_SIGNERS_SHA256=//p' | head -1)"
    [ -n "$r" ] && recs="$recs $r"
  done
  if [ -r "$cur" ]; then
    sha="$(sha256sum "$cur" 2>/dev/null | cut -d' ' -f1)"
    for r in $recs; do
      [ "$r" = "$sha" ] && continue
      refuse "docker/RELEASE_SIGNERS of the tree this clone sits on (sha256 ${sha:-unread}) is not the file this kit recorded ($r): the trust root changed under the kit, and nothing is verified with a file the kit did not record. deploy/join.sh --accept-new-signers records a new one on purpose. Nothing was changed."
      summary "$CLONE" refused; exit 2
    done
    ref_verify "$cur" "docker/RELEASE_SIGNERS of the tree this clone sits on, before it moves" "Nothing was changed."
    return 0
  fi
  if [ -n "$recs" ]; then
    tmp="$(mktemp)" || fail "mktemp (a copy of the signers file $REF ships)"
    git -C "$DENDRA_DIR" show "refs/tags/$REF:docker/RELEASE_SIGNERS" > "$tmp" 2>/dev/null || : > "$tmp"
    sha="$(sha256sum "$tmp" 2>/dev/null | cut -d' ' -f1)"
    for r in $recs; do
      [ "$r" = "$sha" ] && [ -s "$tmp" ] && continue
      rm -f "$tmp"
      refuse "the tree this clone sits on carries no docker/RELEASE_SIGNERS, and the one $REF ships (sha256 ${sha:-unread}) is not the file this kit recorded ($r): nothing is verified with a file the kit did not record. Nothing was changed."
      summary "$CLONE" refused; exit 2
    done
    ref_verify "$tmp" "the docker/RELEASE_SIGNERS $REF ships, which is the file this kit recorded" "Nothing was changed."
    rm -f "$tmp"
    return 0
  fi
  say "  [i] the tree this clone sits on carries no docker/RELEASE_SIGNERS and this kit recorded none: there is no trust root yet, so the tag is verified with the file of its own tree once the clone sits on it, trusted over HTTPS to the forge this first time, as a first installation is."
}

# Placing the clone, as one function: called at its usual place (after Docker, the toolkit and the group), or
# right after the tools when the role is deferred to the clone's probe (PROBE_DEFERRED, see the header). Its body
# keeps the indentation it had at the top level, so that a bench which mutates one of its lines finds it unchanged.
place_clone(){
# The clone on a release tag (--ref / DENDRA_REF, or resolved by resolve_ref). Never main, never a pull: a missing
# clone is cloned at the tag (without --ref: cloned with nothing checked out, then placed on the tag resolved), a
# present one is placed on it (fetching that tag only), and in every case the result is checked: HEAD must be the
# commit the tag names. Local work is measured before anything moves (clone_work_check).
# >>> The pin this file wrote is undone before the clone moves (--miner-image-file, see the header). Left in
# place, a checkout to a tag whose docker/MINER_IMAGE is the same file would carry it along, and the new tree
# would pull the image of the old release. A copy left by a write that did not complete is removed as well.
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
# <<< The pin this file wrote is undone
# >>> Without --ref: the clone is made with nothing checked out, or a present one is measured for local work, then
# the release tag of the declared epoch is resolved (resolve_ref) and the clone placed on it as on a given tag.
if [ "$REF_RESOLVE" = 1 ] && [ "$CLONE" = missing ]; then
  step "cloning the repository (nothing is checked out until its release tag is resolved)"
  git clone --quiet --no-checkout "$DENDRA_REPO_URL" "$DENDRA_DIR" || fail "git clone --no-checkout $DENDRA_REPO_URL"
  CLONE=present; CLONE_FRESH=1
fi
if [ "$REF_RESOLVE" = 1 ] && [ "$CLONE" = present ]; then
  clone_epoch_gate summary
  clone_work_check
  resolve_ref
fi
# <<< Without --ref
[ -n "$REF" ] || fail "no release tag to place the clone on (this file never places it on main)"
if [ -n "$REF" ] && { [ "$CLONE" = missing ] || [ "$CLONE" = present ]; }; then
  if [ "$CLONE" = missing ]; then
    step "cloning the release tag $REF"
    git clone --branch "$REF" "$DENDRA_REPO_URL" "$DENDRA_DIR" || fail "git clone --branch $REF $DENDRA_REPO_URL"
  else
    step "placing the clone on the release tag $REF"
    clone_work_check
    tag_ns_clear
    tag_ns_fetch "$REF"
    tag_adopt "$REF"
    # A clone that was there verifies the tag before it moves, with the trust root it already holds (present_trust).
    [ "$CLONE_FRESH" = 1 ] || present_trust
    # The tree the clone leaves is kept on kit-before-update, as the kit's update instruction does: a commit taken
    # for a publication above (an amended root) then stays reachable. A clone made by this run has nothing to keep.
    if [ "$CLONE_FRESH" != 1 ] && ! _kb="$(git -C "$DENDRA_DIR" branch -f kit-before-update HEAD 2>&1)"; then
      refuse "the tree of the clone in $DENDRA_DIR could not be kept on the branch kit-before-update, so it is not moved (only the tag $REF was fetched). git said:"
      printf '%s\n' "$_kb" | head -4 | sed 's/^/           /'
      summary "$CLONE" refused; exit 2
    fi
    if ! _co="$(git -C "$DENDRA_DIR" checkout -q --detach "refs/tags/$REF" 2>&1)"; then
      refuse "the tree of $REF would overwrite files in $DENDRA_DIR that git does not track. Nothing was changed. git said:"
      printf '%s\n' "$_co" | head -8 | sed 's/^/           /'
      fresh_clone_drop
      summary "$CLONE" refused; exit 2
    fi
  fi
  _at="$(git -C "$DENDRA_DIR" rev-parse -q --verify HEAD 2>/dev/null)"
  _tag="$(git -C "$DENDRA_DIR" rev-parse -q --verify "refs/tags/$REF^{commit}" 2>/dev/null)"
  if [ -z "$_tag" ] || [ "$_at" != "$_tag" ]; then
    fail "the clone in $DENDRA_DIR is not on the commit of the tag $REF (HEAD ${_at:-unreadable}, tag ${_tag:-absent}): $REF is not a tag of $DENDRA_REPO_URL, or the checkout did not happen"
  fi
  say "  [OK] the clone sits on the release tag $REF ($_at)"
  CLONE=pinned
  # The clone has moved: the marker of install.ps1 has done its work, and a later run reads the clone as present.
  rm -f "$FRESH_MARK"
  # The tag the clone sits on names itself (tag_adopt checks it before a present clone moves; a clone made with
  # `git clone --branch` took the tag as the forge serves it, so it is read here for every path).
  _n="$(tag_header "refs/tags/$REF" tag)"; _ty="$(tag_header "refs/tags/$REF" type)"
  if [ "$_n" != "$REF" ] || [ "$_ty" != commit ]; then
    refuse "the tag $REF is not a release tag object of that name (its header names ${_n:-no tag}, of type ${_ty:-unread}): the signed object of another release published under a newer name is not followed. The clone sits on $REF; nothing was started."
    summary "$CLONE" refused; exit 2
  fi
  # A clone without a trust root (made by this run, or present with neither a signers file nor a record): the tag's
  # signature is verified with the signers file of the tree just placed, trusted over HTTPS to the forge this first time.
  if [ "$REF_SIG_DONE" != 1 ]; then
  step "the signature of the release tag $REF"
  _sg="$DENDRA_DIR/docker/RELEASE_SIGNERS"
  if [ ! -r "$_sg" ]; then
    refuse "the tree of $REF carries no docker/RELEASE_SIGNERS, so its tag cannot be verified, and a release whose signature cannot be checked is not installed (a release before v0.2.2, or a tree that is not the project's). The clone sits on $REF; nothing was started."
    summary "$CLONE" refused; exit 2
  fi
  ref_verify "$_sg" "docker/RELEASE_SIGNERS of its tree" "The clone sits on $REF; nothing was started."
  fi
fi

# >>> The miner image a package carries is written only here: after the clone is checked on the tag, before
# join.sh, which judges it. Every way out of pkg_pin_write leaves either the tag's own file, or exactly the
# bytes recorded in PKG_PIN_MARK, the only local change the next run accepts. It never stops the install:
# without the pin, join.sh builds the image from the clone, which is the safe direction.
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
# <<< The miner image a package carries

# The fast-forward of a clone to main is gone: every way above ends on a release tag, or the run has stopped.
[ -r "$DENDRA_DIR/deploy/join.sh" ] || fail "$DENDRA_DIR/deploy/join.sh is not there: this is not the repository this file expects"
CLONE_PLACED=1
# A file downloaded alone reads the clone's pins here when nothing needed them yet, for the version notes alone (what
# pins_require reads later is the same file).
if [ "$PINS_DEFERRED" = 1 ] && [ "$PINS_STATE" != read ]; then
  KIT_PINS="$DENDRA_DIR/deploy/kit_pins.txt"; pins_load
  [ "$PINS_STATE" = read ] && say "  [OK] pins read from $KIT_PINS: $(pins_line)"
fi
pin_version_notes
}
CLONE_PLACED=0
# pins_require <what> -- the pins are read, or the run stops here (exit 2), before anything is downloaded from <what>.
# When this file was downloaded alone the pins are the clone's: the clone is placed first and its tag verified, as for
# the deferred role, and its deploy/kit_pins.txt is read.
pins_require(){
  [ "$PINS_STATE" = read ] && return 0
  if [ "$PINS_DEFERRED" = 1 ]; then
    [ "$CLONE_PLACED" = 1 ] || place_clone
    # place_clone reads the clone's pins itself once the clone is placed.
    [ "$PINS_STATE" = read ] && return 0
    KIT_PINS="$DENDRA_DIR/deploy/kit_pins.txt"
    pins_load
    if [ "$PINS_STATE" = read ]; then say "  [OK] pins read from $KIT_PINS: $(pins_line)"; return 0; fi
  fi
  refuse "$PINS_WHY, and nothing is installed from $1 without the versions and the signing key deploy/kit_pins.txt pins. A release tag older than that file carries none: install Docker$( [ "$GPU" != none ] && printf ' and the NVIDIA container toolkit' ) by hand, then run this file again (an installed Engine or toolkit is kept as it is)."
  summary "$CLONE" refused; exit 2
}

TOOLS_DONE=0
if [ -n "$NEED_TOOLS" ] || [ "$DOCKER" != present ]; then
  step "installing tools"
  $SUDO apt-get update -qq || fail "apt-get update"
  $SUDO apt-get install -y -qq ca-certificates curl git ${NEED_TOOLS} || fail "apt-get install ca-certificates curl git${NEED_TOOLS}"
  TOOLS_DONE=1
fi
# Without --ref and without curl at the plan, the network's epoch is read now, before Docker or the clone.
if [ "$REF_RESOLVE" = 1 ] && [ "$NET_EPOCH_STATE" != read ]; then
  step "the network rules the network declares ($CONFIG_URL)"
  net_epoch_read
  net_epoch_gate "Only the tools git needs were installed." summary
  say "  [OK] $CONFIG_URL declares the network rules"
fi

# The deferred role (no probe next to this file, see the header): the clone first, then its probe, and nothing
# below happens on a host that has no role. The clone is placed and checked by the same function as further
# down, so its probe is this repository's, at the tree join.sh will run.
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
  docker_pkg_pin docker-ce "$PIN_DOCKER_ENGINE"; _pce="$APT_PIN"
  docker_pkg_pin docker-ce-cli "$PIN_DOCKER_ENGINE"; _pcli="$APT_PIN"
  docker_pkg_pin containerd.io "$PIN_DOCKER_CONTAINERD"; _pctd="$APT_PIN"
  docker_pkg_pin docker-buildx-plugin "$PIN_DOCKER_BUILDX"; _pbx="$APT_PIN"
  docker_pkg_pin docker-compose-plugin "$PIN_DOCKER_COMPOSE"; _pcmp="$APT_PIN"
  $SUDO apt-get install -y -qq "$_pce" "$_pcli" "$_pctd" "$_pbx" "$_pcmp" || fail "apt-get install $_pce $_pcli $_pctd $_pbx $_pcmp"
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
  _pcmp="$COMPOSE_PKG"
  if [ "$COMPOSE_PKG" = docker-compose-plugin ]; then docker_pkg_pin docker-compose-plugin "$PIN_DOCKER_COMPOSE"; _pcmp="$APT_PIN"; fi
  $SUDO apt-get install -y -qq "$_pcmp" || fail "apt-get install $_pcmp"
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
if [ "$TOOLKIT" = absent ] && [ "$APT" = 1 ] && [ "$TK_PKG" = installed ]; then
  # An installed toolkit is kept at its version: only its runtime is registered with Docker.
  step "registering the installed NVIDIA container toolkit $TK_VER with Docker"
  $SUDO nvidia-ctk runtime configure --runtime=docker || fail "nvidia-ctk runtime configure"
  daemon_restart || fail "restarting the Docker daemon"
  TOOLKIT=registered
elif [ "$TOOLKIT" = absent ] && [ "$APT" = 1 ] && [ "$TK_PKG" != absent ]; then
  refuse "$(tk_refusal)"
  summary "$CLONE" refused; exit 2
elif [ "$TOOLKIT" = absent ] && [ "$APT" = 1 ]; then
  step "installing the NVIDIA container toolkit"
  pins_require "NVIDIA's repository"
  $SUDO install -m 0755 -d "$SYSROOT/usr/share/keyrings" || fail "mkdir /usr/share/keyrings"
  _nk="$(mktemp)" || fail "a temporary file for NVIDIA's signing key"
  curl -fsSL "$NVIDIA_REPO/gpgkey" -o "$_nk" || { rm -f "$_nk"; fail "download of NVIDIA's signing key"; }
  if ! key_fpr_check "$_nk" "$PIN_NVIDIA_KEY"; then
    rm -f "$_nk"
    refuse "the signing key $NVIDIA_REPO/gpgkey serves is not the one $KIT_PINS pins (NVIDIA_APT_KEY_FINGERPRINT=$PIN_NVIDIA_KEY): $KEY_WHY. Nothing was written to apt's sources. Compare it with the fingerprint NVIDIA publishes (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html); until a release of the kit pins another key, install the toolkit by hand from there, then run this file again: an installed toolkit is kept as it is."
    summary "$CLONE" refused; exit 2
  fi
  $SUDO gpg --batch --dearmor --yes -o "$SYSROOT/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg" < "$_nk" || { rm -f "$_nk"; fail "writing NVIDIA's signing key"; }
  rm -f "$_nk"
  # The repository line is written here, never taken from the server: a list file served over the network could name
  # another source, or one that apt would trust without this key.
  echo 'deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH) /' |
    $SUDO tee "$SYSROOT/etc/apt/sources.list.d/nvidia-container-toolkit.list" >/dev/null || fail "writing nvidia-container-toolkit.list"
  if ! $SUDO apt-get update -qq; then
    $SUDO rm -f "$SYSROOT/etc/apt/sources.list.d/nvidia-container-toolkit.list"
    fail "apt-get update with NVIDIA's repository (the repository file was removed again)"
  fi
  if ! apt_pinned nvidia-container-toolkit "^${PIN_NVIDIA_TOOLKIT//./[.]}$" "nvidia.github.io/libnvidia-container"; then
    $SUDO rm -f "$SYSROOT/etc/apt/sources.list.d/nvidia-container-toolkit.list"
    refuse "NVIDIA's repository offers $APT_N build(s) of nvidia-container-toolkit $PIN_NVIDIA_TOOLKIT, the version $KIT_PINS pins, and this file installs exactly one. The repository file was removed again, and nothing was installed from it. Install the toolkit by hand (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), then run this file again: an installed toolkit is kept as it is."
    summary "$CLONE" refused; exit 2
  fi
  _v="$PIN_NVIDIA_TOOLKIT"
  $SUDO apt-get install -y -qq "nvidia-container-toolkit=$_v" "nvidia-container-toolkit-base=$_v" "libnvidia-container-tools=$_v" "libnvidia-container1=$_v" || fail "apt-get install nvidia-container-toolkit $_v"
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
# The last thing this run says when it added the group. This run reaches Docker through sg / sudo -g;
# the login session does not carry the group until the next login. The desktop application runs
# `docker compose` as that session, so opened right away it gets "permission denied" from a daemon that
# is running fine, and the one line that explained why had scrolled past an hour of join.sh output. So
# it is repeated at the very end, on success and on failure alike.
relogin_notice(){
  [ "$GROUP_ADDED" = 1 ] || return 0
  [ "${RELOGIN_SAID:-0}" = 1 ] && return 0   # said once: the explicit calls and the EXIT trap below share it
  RELOGIN_SAID=1
  say ""
  say "  [!] Log out and back in before opening the Dendra application."
  say "      This run added $(id -un) to the docker group, and a group applies to new logins only: until"
  say "      you sign in again, the application (and any docker command you type) is refused by Docker."
  # A session that outlives the login keeps the old groups. A screen or tmux server started before this run
  # (the usual way to keep a shell on a rig reached over SSH) hands every window it opens the groups it was
  # started with: signing in again over SSH and re-attaching keeps the refusal, and nothing says why.
  say "      A screen or tmux session started before this run keeps the old groups even after you sign in again:"
  say "      leave it and start a new one (screen -ls, tmux ls list them; tmux kill-server ends every tmux window),"
  say "      or run one command in the group right away: sg docker -c 'docker ps'"
  [ -n "${TMUX:-}${STY:-}" ] && say "      This terminal runs inside $( [ -n "${TMUX:-}" ] && printf tmux || printf screen ): it is one of those sessions."
  [ "$WSL" = 1 ] && say "      Under WSL: close every terminal of this distribution, or run 'wsl --terminate <distribution>' from Windows."
  return 0
}
# The early exits after this point (a daemon that does not answer, a clone refused, a fetch that fails)
# would end the run without the notice, so the trap says it on every way out.
[ "$GROUP_ADDED" = 1 ] && trap 'relogin_notice' EXIT
# Every docker call from here on runs in the docker group even when the login shell does not carry
# it yet. `sg` does it when the distribution ships it; newer Ubuntu releases do not, so `sudo -g`
# is the fallback, with PATH carried across so that nvidia-smi under WSL stays reachable.
as_docker(){ # as_docker <command string>
  if [ "$IN_GROUP" = 1 ]; then sh -c "$1"
  elif command -v sg >/dev/null 2>&1; then sg docker -c "$1"
  else $SUDO -u "$(id -un)" -g docker env PATH="$PATH" HOME="$HOME" sh -c "$1"; fi
}
as_docker "docker info >/dev/null 2>&1" || fail "the Docker daemon does not answer 'docker info' as a member of the docker group (is it running? under WSL, is systemd enabled?)"

# The clone, unless the deferred role placed it already (right after the tools, before Docker).
[ "$CLONE_PLACED" = 1 ] || place_clone

step "handing over to join.sh ($JOIN_ARGS)"
say "  from here on the output is join.sh's. It verifies the genesis digest, writes the kit, and starts it."
say ""
as_docker "cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' DENDRA_UPDATE_HINT='$UPDATE_HINT' bash deploy/join.sh $JOIN_ARGS"
RC=$?
# The desktop application: only on a graphical session, since a rig reached over SSH has no
# menu to put it in. It writes three files under $HOME and installs no package; a failure here leaves
# the miner running and says how to retry.
install_app(){
  if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ -r "$DENDRA_DIR/deploy/app/install_app.sh" ]; then
    step "the Dendra application"
    bash "$DENDRA_DIR/deploy/app/install_app.sh" \
      || warn "the application was not added to the menu; retry with: bash $DENDRA_DIR/deploy/app/install_app.sh"
  fi
}
# miner_containers returns 0 when the miner's container runs, 1 when it does not, 3 when unread. It looks up the
# compose labels of the kit's project (dendra-miner, the `name:` of deploy/testnet-miner/docker-compose.yml) and
# its service.
miner_containers(){
  local out
  out="$(as_docker "docker ps -q --filter label=com.docker.compose.project=dendra-miner --filter label=com.docker.compose.service=miner" 2>/dev/null)" || return 3
  [ -n "$out" ] && return 0
  return 1
}
if [ "$RC" = 0 ]; then
  install_app
  relogin_notice
  summary "$CLONE" joined
  exit 0
fi
say ""
case "$RC" in
  3) say "  join.sh exited with 3: the miner's containers were started, and within its bound the miner neither"
     say "  registered nor failed: not measured, which is not a failure. Read where it stands in a few minutes:"
     say "    bash '$DENDRA_DIR/deploy/testnet-miner/miner_health.sh'" ;;
  # join.sh's 2 is a refusal (a role, a model, an address, a release it will not run), decided before it started
  # anything. Kept as 2, never folded into 1: a refusal asks for a different setting, a failure for a retry.
  2) say "  join.sh refused (exit 2): it started nothing, and its output above says why and what it would accept."
     say "  The host is prepared; once the cause is changed, re-run just it:"
     say "    cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' bash deploy/join.sh $JOIN_ARGS" ;;
  *) say "  join.sh exited with $RC. The host is prepared; read its output above, then re-run just it:"
     say "    cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' bash deploy/join.sh $JOIN_ARGS" ;;
esac
# The application is installed whenever the miner's containers run, whatever join.sh concluded. join.sh
# exits 1 when it detects a failure while the containers run, and that operator is the one who most needs
# the application (its health view, its logs, the Start/Stop buttons), so a clean exit is not required.
# A miner that runs is shown; one that does not, or a docker that does not answer, is reported.
if [ "$ROLE" != validator ]; then
  miner_containers; MRC=$?
  case "$MRC" in
    0) say "  [i] the miner's containers run: the Dendra application is installed anyway, it shows what is wrong."
       install_app ;;
    1) say "  [i] no miner container runs: the Dendra application is not installed (run this file again once join.sh succeeds)." ;;
    *) say "  [?] whether the miner's containers run could not be read: the Dendra application is not installed." ;;
  esac
fi
relogin_notice
if [ "$RC" = 3 ]; then
  summary "$CLONE" join_unmeasured
  exit 3
fi
if [ "$RC" = 2 ]; then
  summary "$CLONE" join_refused
  exit 2
fi
summary "$CLONE" join_failed
exit 1
