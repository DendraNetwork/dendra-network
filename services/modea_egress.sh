#!/usr/bin/env bash
# modea_egress.sh — PARE-FEU D'ÉGRESS pour le mineur Mode A (anti-exfiltration, CR-04/05).
#
# Le no_egress applicatif (hardening.py) est best-effort (un binaire natif le contourne). Le contrôle
# RÉEL est un pare-feu OS : on AUTORISE le mineur à ne sortir QUE vers le relais / la chaîne / Ollama,
# et on DROP tout le reste. Ainsi, même client compromis, le clair ne peut pas être exfiltré ailleurs.
#
# Granularité par UTILISATEUR (UID dédié) : ne touche PAS l'égress des autres applis de la machine.
# Le démon mineur doit alors tourner sous cet UID (ex: runuser -u dendra-miner -- modea_confine.sh ...).
#
# REQUIERT root (nft/iptables). HONNÊTE : root reste capable de désactiver le pare-feu -> ce contrôle
# vise l'exfiltration par un CLIENT compromis / un process non-root, pas un opérateur root malveillant
# (seul Mode B/MPC ou un TEE ferme ce dernier trou).
#
# Usage :
#   sudo bash modea_egress.sh on  <relay_host> <relay_port> <miner_uid>   # pose les règles
#   sudo bash modea_egress.sh off                                          # retire les règles
# Exemple :
#   sudo useradd -r -s /usr/sbin/nologin dendra-miner 2>/dev/null || true
#   sudo bash modea_egress.sh on 10.0.0.5 8645 "$(id -u dendra-miner)"
set -uo pipefail
ACTION="${1:-}"
TABLE="dendra_modea"

need_root() { [ "$(id -u)" = "0" ] || { echo "ERREUR: lancer avec sudo (pare-feu = root)."; exit 1; }; }

off_nft()  { nft delete table inet "$TABLE" 2>/dev/null || true; }
off_ipt()  { iptables -D OUTPUT -m owner --uid-owner "$MU" -j DENDRA_MODEA 2>/dev/null || true
             iptables -F DENDRA_MODEA 2>/dev/null || true; iptables -X DENDRA_MODEA 2>/dev/null || true; }

case "$ACTION" in
  off)
    need_root
    if command -v nft >/dev/null 2>&1; then off_nft; echo "[egress] règles nft retirées."; fi
    if command -v iptables >/dev/null 2>&1; then MU="${4:-0}"; off_ipt 2>/dev/null || true; echo "[egress] règles iptables retirées (si présentes)."; fi
    ;;
  on)
    need_root
    RH="${2:?relay_host requis}"; RP="${3:?relay_port requis}"; MU="${4:?miner_uid requis}"
    if command -v nft >/dev/null 2>&1; then
      off_nft
      # Autorise : loopback (chaîne 26657, Ollama 11434, faucet 4500), DNS, et le relais RH:RP.
      # DROP tout autre flux NEW sortant de l'UID mineur.
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
      echo "[egress] nft posé : UID $MU -> seuls loopback + DNS + $RH:$RP autorisés, reste DROP."
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
      echo "[egress] iptables posé : UID $MU -> seuls loopback + DNS + $RH:$RP autorisés, reste DROP."
    else
      echo "ERREUR: ni nft ni iptables présents -> impossible de poser le pare-feu d'égress."; exit 1
    fi
    ;;
  *)
    echo "Usage: sudo bash modea_egress.sh on <relay_host> <relay_port> <miner_uid> | off"; exit 1;;
esac
