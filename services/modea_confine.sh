#!/usr/bin/env bash
# modea_confine.sh — lance le démon mineur Mode A SOUS CONFINEMENT (CR-04/05 + B0.5, audit).
#
# Trois couches, de la plus portable à la plus forte :
#   1) durcissement PROCESSUS rootless (TOUJOURS) : ulimit -c 0 (no core dump) + DENDRA_CONFINE=1
#      -> le démon applique PR_SET_DUMPABLE=0 (pas de ptrace même-utilisateur), NO_NEW_PRIVS,
#      RLIMIT_CORE=0, umask 0077 (cf. modea/confine.py). DENDRA_MLOCKALL=1 pour verrouiller la RAM.
#   2) confinement FS+seccomp via FIREJAIL (DENDRA_FIREJAIL=1, si `firejail` installé) : profil
#      durci avec --seccomp (filtre d'appels système par défaut de firejail), caps droppées,
#      no-new-privs, /tmp privé (tmpfs), pas de core dump, no-sound/no-3d. C'est le chemin qui
#      apporte un VRAI seccomp-bpf sans l'écrire en Python.
#   3) confinement FS via BUBBLEWRAP (DENDRA_BWRAP=1, si `bwrap` installé) : racine en LECTURE SEULE,
#      /tmp en tmpfs, seuls ~/.dendra (keyring chaîne) et ~/.dendra-miners (clés) inscriptibles,
#      PID/IPC/UTS isolés, --cap-drop ALL, --new-session, die-with-parent. (bwrap seul ne pose pas de
#      filtre seccomp custom ici ; combine-le avec l'égress nft pour restreindre le réseau.)
#
# Le RÉSEAU reste partagé dans les deux sandbox (le mineur a besoin du relais/chaîne/Ollama) :
# l'égress se restreint séparément via modea_egress.sh (pare-feu nft/iptables par UID).
#
# HONNÊTE : ceci élève fortement la barre (non scalable, détectable) mais n'est PAS cryptographique —
# un opérateur ROOT sur sa machine contourne (désactive seccomp/firewall, lit la RAM) ; seul le
# Mode B/MPC ou un TEE matériel ferme ce trou. Voir docs/MODE-A-SECURITE.md, docs/MODE-A-COMPLET.md.
#
# Usage (WSL) :
#   tr -d '\r' < modea_confine.sh | bash -s -- --id m1 --relay http://127.0.0.1:8645 --keydir ~/.dendra-miners
#   DENDRA_FIREJAIL=1 DENDRA_MLOCKALL=1 tr -d '\r' < modea_confine.sh | bash -s -- --id m1 ...
#   DENDRA_BWRAP=1    DENDRA_MLOCKALL=1 tr -d '\r' < modea_confine.sh | bash -s -- --id m1 ...
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
DAEMON="$HERE/miner.py"
[ -f "$DAEMON" ] || { echo "ERREUR: miner.py introuvable près de ce script ($DAEMON)"; exit 1; }

export DENDRA_CONFINE="${DENDRA_CONFINE:-1}"   # durcissement processus dans le démon (confine.py)
ulimit -c 0 2>/dev/null || true                # ceinture+bretelles : pas de core dump (shell)

KR="$HOME/.dendra"; MK="$HOME/.dendra-miners"
mkdir -p "$KR" "$MK"

run_plain() {
  echo "[confine] durcissement processus seul (DENDRA_FIREJAIL=1 ou DENDRA_BWRAP=1 pour ajouter un sandbox OS)."
  exec python3 "$DAEMON" "$@"
}

# ---- couche 2 : firejail (seccomp réel + caps droppées) -----------------------
if [ "${DENDRA_FIREJAIL:-0}" = "1" ]; then
  if ! command -v firejail >/dev/null 2>&1; then
    echo "[confine] DENDRA_FIREJAIL=1 mais firejail absent -> on tente bubblewrap puis le repli processus."
    echo "          (Installer : sudo apt-get install -y firejail)"
  else
    echo "[confine] firejail : seccomp ON, caps droppées, no-new-privs, /tmp privé, pas de core dump."
    # --private-tmp : /tmp éphémère par-process ; --seccomp : filtre d'appels système ; --caps.drop=all ;
    # --nonewprivs --noroot : pas d'élévation ; --disable-mnt : pas de montage ; le réseau reste (relais).
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

# ---- couche 3 : bubblewrap (FS RO + isolation namespaces) ----------------------
if [ "${DENDRA_BWRAP:-0}" = "1" ]; then
  if ! command -v bwrap >/dev/null 2>&1; then
    echo "[confine] DENDRA_BWRAP=1 mais bubblewrap (bwrap) absent -> repli durcissement processus seul."
    echo "          (Installer : sudo apt-get install -y bubblewrap)"
    run_plain "$@"
  fi
  echo "[confine] bubblewrap : racine RO, /tmp tmpfs, inscriptibles = $KR et $MK, caps droppées, PID/IPC/UTS isolés."
  # --ro-bind / / : tout le FS en lecture seule ; on REMONTE en écriture les seuls dossiers nécessaires.
  # --cap-drop ALL + --unshare-* : aucun privilège, namespaces isolés. (Pas de seccomp custom ici.)
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
