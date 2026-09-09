#!/usr/bin/env bash
# modea_confine.sh — runs the Mode A miner daemon UNDER CONFINEMENT.
#
# Three layers, from the most portable to the strongest:
#   1) rootless PROCESS hardening (ALWAYS): ulimit -c 0 (no core dump) plus DENDRA_CONFINE=1, which
#      makes the daemon apply PR_SET_DUMPABLE=0 (no same-user ptrace), NO_NEW_PRIVS, RLIMIT_CORE=0 and
#      umask 0077 (see modea/confine.py). DENDRA_MLOCKALL=1 locks the RAM.
#   2) filesystem and seccomp confinement via FIREJAIL (DENDRA_FIREJAIL=1, when `firejail` is
#      installed): hardened profile with --seccomp (firejail's default syscall filter), capabilities
#      dropped, no-new-privs, private /tmp (tmpfs), no core dump, no sound and no 3D. This is the path
#      that brings a REAL seccomp-bpf filter without writing one in Python.
#   3) filesystem confinement via BUBBLEWRAP (DENDRA_BWRAP=1, when `bwrap` is installed): READ-ONLY
#      root, /tmp on tmpfs, only ~/.dendra (chain keyring) and ~/.dendra-miners (keys) writable,
#      isolated PID/IPC/UTS namespaces, --cap-drop ALL, --new-session, die-with-parent. bwrap alone
#      installs no custom seccomp filter here; combine it with the nft egress rules to restrict
#      networking.
#
# The NETWORK stays shared in both sandboxes (the miner needs the relay, the chain and Ollama); egress
# is restricted separately by modea_egress.sh (per-UID nft/iptables firewall).
#
# HONEST SCOPE: this raises the bar substantially — the attack stops being scalable and becomes
# detectable — but it is NOT cryptographic. An operator with ROOT on its own machine bypasses it
# (disables seccomp and the firewall, reads the RAM); only Mode B/MPC or a hardware TEE closes that
# hole. See docs/MODE-A-SECURITE.md and docs/MODE-A-COMPLET.md.
#
# Usage:
#   tr -d '\r' < modea_confine.sh | bash -s -- --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
#   DENDRA_FIREJAIL=1 DENDRA_MLOCKALL=1 tr -d '\r' < modea_confine.sh | bash -s -- --id m1 ...
#   DENDRA_BWRAP=1    DENDRA_MLOCKALL=1 tr -d '\r' < modea_confine.sh | bash -s -- --id m1 ...
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
DAEMON="$HERE/miner.py"
[ -f "$DAEMON" ] || { echo "ERROR: miner.py not found next to this script ($DAEMON)"; exit 1; }

export DENDRA_CONFINE="${DENDRA_CONFINE:-1}"   # process hardening inside the daemon (confine.py)
ulimit -c 0 2>/dev/null || true                # belt and braces: no core dump from the shell either

KR="$HOME/.dendra"; MK="$HOME/.dendra-miners"
mkdir -p "$KR" "$MK"

run_plain() {
  echo "[confine] process hardening only (set DENDRA_FIREJAIL=1 or DENDRA_BWRAP=1 to add an OS sandbox)."
  exec python3 "$DAEMON" "$@"
}

# ---- layer 2: firejail (real seccomp + dropped capabilities) ------------------
if [ "${DENDRA_FIREJAIL:-0}" = "1" ]; then
  if ! command -v firejail >/dev/null 2>&1; then
    echo "[confine] DENDRA_FIREJAIL=1 but firejail is absent -> trying bubblewrap, then the process fallback."
    echo "          (Install: sudo apt-get install -y firejail)"
  else
    echo "[confine] firejail: seccomp ON, capabilities dropped, no-new-privs, private /tmp, no core dump."
    # --private-tmp: per-process ephemeral /tmp; --seccomp: syscall filter; --caps.drop=all;
    # --nonewprivs --noroot: no elevation; the network stays reachable (relay).
    exec firejail --quiet \
      --seccomp \
      --caps.drop=all \
      --nonewprivs \
      --noroot \
      --private-tmp \
      --rlimit-core=0 \
      --nogroups --nosound --no3d --notv \
      python3 "$DAEMON" "$@"
  fi
fi

# ---- layer 3: bubblewrap (read-only filesystem + namespace isolation) ---------
if [ "${DENDRA_BWRAP:-0}" = "1" ]; then
  if ! command -v bwrap >/dev/null 2>&1; then
    echo "[confine] DENDRA_BWRAP=1 but bubblewrap (bwrap) is absent -> falling back to process hardening only."
    echo "          (Install: sudo apt-get install -y bubblewrap)"
    run_plain "$@"
  fi
  echo "[confine] bubblewrap: read-only root, tmpfs /tmp, writable = $KR and $MK, capabilities dropped, isolated PID/IPC/UTS."
  # --ro-bind / /: the whole filesystem read-only; only the directories that need writes are remounted.
  # --cap-drop ALL plus --unshare-*: no privileges, isolated namespaces. No custom seccomp filter here.
  exec bwrap \
    --ro-bind / / \
    --dev /dev --proc /proc \
    --tmpfs /tmp \
    --bind "$KR" "$KR" \
    --bind "$MK" "$MK" \
    --cap-drop ALL \
    --unshare-pid --unshare-ipc --unshare-uts \
    --die-with-parent --new-session \
    python3 "$DAEMON" "$@"
fi

run_plain "$@"
