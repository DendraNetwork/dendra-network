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
#
# Environment (all optional):
#   CONFIG_URL       network-info.txt of the network to join (default: the public network's)
#   DENDRA_DIR       where the repository lives or gets cloned (default: $HOME/dendra-network)
#   DENDRA_REPO_URL  git URL to clone (default: the public repository)
#   DENDRA_MIN_DISK_GB / DENDRA_MIN_DISK_GB_JUDGE / DENDRA_MIN_RAM_MB   refusal floors, see below
#
# Exit codes -- three answers, never two:
#   0  done, or a plan with nothing missing
#   1  a step was attempted and failed
#   2  refused: no --yes while changes are needed, unsupported host (no apt, WSL 1), too little disk
#      or RAM, a clone that carries local work or cannot be inspected, a path or URL this file
#      cannot pass on safely
#   3  not measurable: the host could not be read (no /etc/os-release, no /proc/meminfo, no disk
#      figure) -- an unknown is never treated as a pass
set -u

# ---------------------------------------------------------------- flags
YES=0; CHECK=0; LIGHT=0; ROLE=judge
while [ $# -gt 0 ]; do case "$1" in
  --yes)       YES=1; shift;;
  --check)     CHECK=1; shift;;
  --light)     LIGHT=1; shift;;
  --judge)     ROLE=judge; shift;;
  --miner)     ROLE=miner; shift;;
  --validator) ROLE=validator; shift;;
  -h|--help)   sed -n '2,57p' "$0"; exit 0;;
  *) echo "[install] unknown argument: $1 (see --help)"; exit 2;;
esac; done

say(){ printf '%s\n' "$*"; }
warn(){ printf '  [!] %s\n' "$*"; }
refuse(){ printf '  [REFUSED] %s\n' "$*" >&2; }
unmeasurable(){ printf '  [?] %s\n' "$*" >&2; exit 3; }

# ---------------------------------------------------------------- defaults and test seams
# The *_FILE / *_DIR / *_MOUNT variables below exist so that a bench can point this script at a
# fabricated host instead of the real one. They are test seams: never set them on a real machine.
CONFIG_URL="${CONFIG_URL:-http://api.dendranetwork.com:8088/network-info.txt}"
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

# A path or a URL is passed on to other shells below. One that carries a single quote cannot be
# quoted safely, so it is refused HERE, before anything is read or changed, rather than after Docker
# is installed.
case "$DENDRA_DIR$CONFIG_URL" in *"'"*) refuse "DENDRA_DIR or CONFIG_URL contains a single quote, which this file cannot pass on safely."; exit 2;; esac
case "$CONFIG_URL" in
  http://*|https://*) : ;;
  *) refuse "CONFIG_URL must start with http:// or https:// (got: $CONFIG_URL)"; exit 2;;
esac

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
if [ -d "$DENDRA_DIR/.git" ]; then
  if command -v git >/dev/null 2>&1; then
    CLONE=present
    DIRTY="$(git -C "$DENDRA_DIR" status --porcelain --untracked-files=no 2>/dev/null)"
    [ -n "$DIRTY" ] && CLONE=dirty
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
say "  docker       : $DOCKER$( [ "$DOCKER" != missing ] && printf ' (daemon %s, %s running container(s)%s)' "$DAEMON" "$RUNNING" "$( [ "$RUNNING" = '?' ] && printf ' -- not readable from this account yet' )" )"
say "  nvidia toolkit: $TOOLKIT"
say "  tools missing: ${NEED_TOOLS:-none}"
say "  clone        : $CLONE ($DENDRA_DIR)"

# ---------------------------------------------------------------- 2. what must change, and refusals
PLAN=""
add(){ PLAN="${PLAN}  - $1
"; }

DISK_FLOOR="$MIN_DISK_GB"; [ "$ROLE" = judge ] && DISK_FLOOR="$MIN_DISK_GB_JUDGE"
if [ "$DISK_GB" -lt "$DISK_FLOOR" ] 2>/dev/null; then
  refuse "free disk ${DISK_GB} GB under ${_disk_path} is below the floor of ${DISK_FLOOR} GB for the $ROLE role (DENDRA_MIN_DISK_GB / DENDRA_MIN_DISK_GB_JUDGE)."
  say "         The models, the images and the first build do not fit; the failure would surface an hour"
  say "         later inside a pull or build log. Free space, or point Docker's data root at a larger disk."
  [ "$ROLE" = judge ] && say "         --miner has a lower floor (${MIN_DISK_GB} GB): it does not pull the judge model."
  [ "$HIVE" = 1 ] && say "         On a HiveOS rig the system flash is usually the constraint: add a drive for Docker."
  exit 2
fi
if [ "$RAM_MB" -lt "$MIN_RAM_MB" ]; then
  refuse "RAM ${RAM_MB} MB is below the floor of ${MIN_RAM_MB} MB (DENDRA_MIN_RAM_MB): the first start compiles the chain binary."
  [ "$WSL" = 1 ] && say "         Under WSL the VM gets about half of the PC's memory unless %UserProfile%\\.wslconfig sets [wsl2] memory=."
  exit 2
fi
[ "$RAM_MB" -lt "$WARN_RAM_MB" ] && warn "RAM ${RAM_MB} MB: the first build may be slow or fail on a rig this size; a second attempt resumes from the cache."
if [ "$ROLE" = judge ]; then
  say "  [i] the judge role is decided at join time by deploy/hw_probe.sh (MOE_CPU_MIN_RAM_MB, or the GPU tier);"
  say "      this host reads RAM_MB=${RAM_MB}. If the probe declines, join.sh keeps the miner role."
fi

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
    warn "GPU present, toolkit not confirmed, no apt: install it by hand -> https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html (the miner runs on CPU otherwise)"
  fi
fi
[ "$IN_GROUP" = 0 ] && add "add $(id -un) to the docker group (usermod -aG docker: membership of that group is equivalent to root on this host)"
case "$CLONE" in
  missing)    add "clone $DENDRA_REPO_URL into $DENDRA_DIR";;
  present)    add "fast-forward the clone in $DENDRA_DIR to the published tree (refused if it carries local commits or files the new tree would overwrite)";;
  dirty)      refuse "the clone in $DENDRA_DIR has local changes to tracked files; a fast-forward would discard them."
              printf '%s\n' "$DIRTY" | head -5 | sed 's/^/           /'
              say "         This file only fast-forwards a clone that carries no local work -- a commit here would be"
              say "         discarded too. Move your changes to another clone, or point DENDRA_DIR elsewhere."
              exit 2;;
  unreadable) refuse "$DENDRA_DIR is a git clone but git is not installed, so it cannot be inspected before a fast-forward. Install git first (apt-get install git), or point DENDRA_DIR elsewhere."; exit 2;;
  foreign)    refuse "$DENDRA_DIR is a git clone that does not carry deploy/join.sh: not this repository. Point DENDRA_DIR elsewhere."; exit 2;;
  occupied)   refuse "$DENDRA_DIR exists and is not a git clone. Point DENDRA_DIR elsewhere."; exit 2;;
esac
JOIN_ARGS=""
case "$ROLE" in judge) JOIN_ARGS="--judge";; miner) JOIN_ARGS="--miner";; validator) JOIN_ARGS="--validator";; esac
[ "$LIGHT" = 1 ] && [ "$ROLE" != validator ] && JOIN_ARGS="$JOIN_ARGS --remote-rpc"
add "run: CONFIG_URL=$CONFIG_URL bash deploy/join.sh $JOIN_ARGS   (from $DENDRA_DIR)"

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

if [ -n "$NEED_TOOLS" ] || [ "$DOCKER" != present ]; then
  step "installing tools"
  $SUDO apt-get update -qq || fail "apt-get update"
  $SUDO apt-get install -y -qq ca-certificates curl git ${NEED_TOOLS} || fail "apt-get install ca-certificates curl git${NEED_TOOLS}"
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

if [ "$IN_GROUP" = 0 ]; then
  step "adding $(id -un) to the docker group"
  $SUDO usermod -aG docker "$(id -un)" || fail "usermod -aG docker"
  say "  the membership applies to new logins; the rest of this run switches group explicitly."
fi
# Every docker call from here on runs in the docker group even when the login shell does not carry
# it yet. `sg` does it when the distribution ships it; newer Ubuntu releases do not, so `sudo -g`
# is the fallback -- PATH is carried across so that nvidia-smi under WSL stays reachable.
as_docker(){ # as_docker <command string>
  if [ "$IN_GROUP" = 1 ]; then sh -c "$1"
  elif command -v sg >/dev/null 2>&1; then sg docker -c "$1"
  else $SUDO -u "$(id -un)" -g docker env PATH="$PATH" HOME="$HOME" sh -c "$1"; fi
}
as_docker "docker info >/dev/null 2>&1" || fail "the Docker daemon does not answer 'docker info' as a member of the docker group (is it running? under WSL, is systemd enabled?)"

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
  if [ "$AHEAD" != 0 ]; then
    refuse "the clone in $DENDRA_DIR carries $AHEAD local commit(s) beyond what it received from origin/$BR; a fast-forward would discard them."
    git -C "$DENDRA_DIR" log --oneline "refs/remotes/origin/$BR..HEAD" 2>/dev/null | head -5 | sed 's/^/           /'
    say "         Move that work to another clone, or point DENDRA_DIR elsewhere. Nothing was changed."
    resume "$CLONE" refused; exit 2
  fi
  git -C "$DENDRA_DIR" fetch --quiet origin "$BR" || fail "git fetch origin $BR"
  if ! _co="$(git -C "$DENDRA_DIR" checkout -q -B "$BR" "origin/$BR" 2>&1)"; then
    refuse "the published tree would overwrite files in $DENDRA_DIR that git does not track. Nothing was changed. git said:"
    printf '%s\n' "$_co" | head -8 | sed 's/^/           /'
    resume "$CLONE" refused; exit 2
  fi
  CLONE=updated
fi
[ -r "$DENDRA_DIR/deploy/join.sh" ] || fail "$DENDRA_DIR/deploy/join.sh is not there: this is not the repository this file expects"

step "handing over to join.sh ($JOIN_ARGS)"
say "  from here on the output is join.sh's. It verifies the genesis digest, writes the kit, and starts it."
say ""
as_docker "cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' bash deploy/join.sh $JOIN_ARGS"
RC=$?
if [ "$RC" != 0 ]; then
  say ""
  say "  join.sh exited with $RC. The host is prepared; read its output above, then re-run just it:"
  say "    cd '$DENDRA_DIR' && CONFIG_URL='$CONFIG_URL' bash deploy/join.sh $JOIN_ARGS"
  resume "$CLONE" join_failed
  exit 1
fi
resume "$CLONE" joined
