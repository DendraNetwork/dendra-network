#!/usr/bin/env bash
# passphrase-dir.sh -- the single check of the directory that holds the miner's keyring passphrase.
# Sourced, never run, by the three scripts that touch that directory:
#   deploy/join.sh                      creates it and the passphrase, sets it 0700, writes it into .env;
#   deploy/testnet-miner/encrypt-keys.sh  the same, when it encrypts a keyring that was unencrypted;
#   deploy/uninstall.sh                 backs the passphrase up and, under --delete-keys, removes it.
#
# Why a directory of its own, and why one check. The kit sets that directory 0700, mounts it into the miner,
# archives it and, under uninstall --delete-keys, removes the passphrase from it. A directory that already
# holds something else ($HOME, ~/.ssh, the application's settings in ~/.config/dendra) is changed by each of
# those steps, and a removal that takes the whole directory would cost ~/.ssh its authorized_keys and,
# with DENDRA_SECRETS_DIR=$HOME, the verified backup written under $HOME. Three copies of the check would
# drift apart (one refusing a directory inside the clone, another not, none refusing $HOME), so it lives
# here once.
#
# dendra_secrets_dir_check <dir> <clone> -> 0 when <dir> may hold the passphrase; otherwise prints why and
# returns 1. Accepted: an absolute, plain path (letters, digits, / . _ -: compose reads the .env line as it
# is written; no empty, `.` or `..` component), outside the clone (which is published), that is neither /,
# nor $HOME, nor a directory above $HOME or above the application's settings directory, and that -- when it
# exists, symbolic links resolved -- holds nothing but `keyring-passphrase` and the temporary file a
# creation leaves while it writes. Pure bash: no tool is assumed, so it runs on a bare host.
dendra_secrets_dir_check(){
  local d="$1" clone="${2:-}" home="${HOME:-}" cfg p hp cp ccp f n
  [ "$d" = / ] && { echo "it is the root directory: the passphrase needs a directory of its own"; return 1; }
  d="${d%/}"
  case "$d" in /*) : ;; *) echo "not an absolute path"; return 1 ;; esac
  case "$d" in *[!A-Za-z0-9/._-]*) echo "it holds characters the kit's .env cannot carry (only letters, digits, / . _ -)"; return 1 ;; esac
  case "$d/" in *//*|*/./*|*/../*) echo "it is not a plain path (an empty, . or .. component)"; return 1 ;; esac
  cfg="${XDG_CONFIG_HOME:-$home/.config}/dendra"; cfg="${cfg%/}"
  # The written path first: a directory above $HOME (or $HOME itself) is refused whether or not it exists.
  if [ -n "$clone" ]; then
    case "$d/" in "${clone%/}/"*) echo "it is inside the clone, which is published"; return 1 ;; esac
  fi
  if [ -n "$home" ]; then
    case "${home%/}/" in "$d/"*) echo "it is $home or a directory above it: the passphrase needs a directory of its own"; return 1 ;; esac
  fi
  case "$cfg/" in "$d/"*) echo "it is the application's settings directory ($cfg) or a directory above it"; return 1 ;; esac
  if [ -e "$d" ] || [ -L "$d" ]; then
    [ -d "$d" ] || { echo "it exists and is not a directory"; return 1; }
    p="$(cd "$d" 2>/dev/null && pwd -P)" || { echo "it exists and cannot be entered"; return 1; }
    # Then the physical path: a link to $HOME, or into the clone, is that place.
    if [ -n "$clone" ] && ccp="$(cd "$clone" 2>/dev/null && pwd -P)"; then
      case "$p/" in "$ccp/"*) echo "it resolves inside the clone ($p), which is published"; return 1 ;; esac
    fi
    if [ -n "$home" ] && hp="$(cd "$home" 2>/dev/null && pwd -P)"; then
      case "$hp/" in "$p/"*) echo "it resolves to $hp or a directory above it: the passphrase needs a directory of its own"; return 1 ;; esac
    fi
    if cp="$(cd "$cfg" 2>/dev/null && pwd -P)"; then
      case "$cp/" in "$p/"*) echo "it resolves to the application's settings directory ($cp) or a directory above it"; return 1 ;; esac
    fi
    for f in "$d"/* "$d"/.[!.]* "$d"/..?*; do
      [ -e "$f" ] || [ -L "$f" ] || continue
      n="${f##*/}"
      case "$n" in
        keyring-passphrase|.keyring-passphrase.*) : ;;
        *) echo "it already holds $n: the kit sets this directory 0700, backs it up and removes the passphrase from it, so it needs a directory of its own (the kit's default: $cfg/miner-secrets)"; return 1 ;;
      esac
    done
  fi
  return 0
}
