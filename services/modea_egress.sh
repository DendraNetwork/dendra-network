#!/usr/bin/env bash
# modea_egress.sh — EGRESS FIREWALL for the Mode A miner (anti-exfiltration, CR-04/05).
#
# The application-level no_egress (hardening.py) is best-effort: a native binary bypasses it. The REAL
# control is an OS firewall: the miner is ALLOWED to reach ONLY the relay / the chain / Ollama, and
# everything else is DROPPED. Even with a compromised client, plaintext cannot be exfiltrated elsewhere.
#
# Granularity is per USER (a dedicated UID): it does NOT touch the egress of the other applications on
# the machine. The miner daemon must then run under that UID
# (e.g. runuser -u dendra-miner -- modea_confine.sh ...).
#
# REQUIRES root (nft/iptables). Stated plainly: root remains able to disable the firewall, so this
# control targets exfiltration by a compromised CLIENT or a non-root process, not a malicious root
# operator (only Mode B/MPC or a TEE closes that last hole).
#
# Usage:
#   sudo bash modea_egress.sh on  <relay_host> <relay_port> <miner_uid>   # install the rules
#   sudo bash modea_egress.sh off                                          # remove the rules
# Example:
#   sudo useradd -r -s /usr/sbin/nologin dendra-miner 2>/dev/null || true
#   sudo bash modea_egress.sh on 10.0.0.5 8645 "$(id -u dendra-miner)"
set -uo pipefail
ACTION="${1:-}"
TABLE="dendra_modea"

need_root() { [ "$(id -u)" = "0" ] || { echo "ERROR: run with sudo (firewall rules require root)."; exit 1; }; }

off_nft()  { nft delete table inet "$TABLE" 2>/dev/null || true; }
off_ipt()  { iptables -D OUTPUT -m owner --uid-owner "$MU" -j DENDRA_MODEA 2>/dev/null || true
             iptables -F DENDRA_MODEA 2>/dev/null || true; iptables -X DENDRA_MODEA 2>/dev/null || true; }

case "$ACTION" in
  off)
    need_root
    if command -v nft >/dev/null 2>&1; then off_nft; echo "[egress] nft rules removed."; fi
    if command -v iptables >/dev/null 2>&1; then MU="${4:-0}"; off_ipt 2>/dev/null || true; echo "[egress] iptables rules removed (if present)."; fi
    ;;
  on)
    need_root
    RH="${2:?relay_host required}"; RP="${3:?relay_port required}"; MU="${4:?miner_uid required}"
    if command -v nft >/dev/null 2>&1; then
      off_nft
      # Allow: loopback (chain 26657, Ollama 11434, faucet 4500), DNS, and the relay RH:RP.
      # DROP every other NEW flow leaving the miner UID.
      nft -f - <<EOF
table inet $TABLE {
  chain out {
    type filter hook output priority 0; policy accept;
    meta skuid $MU ip daddr 127.0.0.1 accept
    meta skuid $MU ip6 daddr ::1 accept
    meta skuid $MU udp dport 53 accept
    meta skuid $MU tcp dport 53 accept
    meta skuid $MU ip daddr $RH tcp dport $RP accept
    meta skuid $MU ct state established,related accept
    meta skuid $MU drop
  }
}
EOF
      echo "[egress] nft installed: UID $MU -> only loopback + DNS + $RH:$RP allowed, everything else DROP."
    elif command -v iptables >/dev/null 2>&1; then
      off_ipt
      iptables -N DENDRA_MODEA
      iptables -A DENDRA_MODEA -o lo -j RETURN
      iptables -A DENDRA_MODEA -d 127.0.0.1 -j RETURN
      iptables -A DENDRA_MODEA -p udp --dport 53 -j RETURN
      iptables -A DENDRA_MODEA -p tcp --dport 53 -j RETURN
      iptables -A DENDRA_MODEA -d "$RH" -p tcp --dport "$RP" -j RETURN
      iptables -A DENDRA_MODEA -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
      iptables -A DENDRA_MODEA -j DROP
      iptables -A OUTPUT -m owner --uid-owner "$MU" -j DENDRA_MODEA
      echo "[egress] iptables installed: UID $MU -> only loopback + DNS + $RH:$RP allowed, everything else DROP."
    else
      echo "ERROR: neither nft nor iptables present -> cannot install the egress firewall."; exit 1
    fi
    ;;
  *)
    echo "Usage: sudo bash modea_egress.sh on <relay_host> <relay_port> <miner_uid> | off"; exit 1;;
esac
