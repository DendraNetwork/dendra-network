#!/usr/bin/env bash
# h-config.sh -- HiveOS runs it before h-run.sh. It turns the flight sheet's fields into the configuration
# file h-run.sh reads (CUSTOM_CONFIG_FILENAME, from h-manifest.conf), and decides NOTHING else:
#   CUSTOM_TEMPLATE     "Wallet and worker template": set it to %WAL% alone. The wallet is the PAYOUT ADDRESS
#                       (dendra1...): where the Final Testnet Season pays this rig's miner.
#   CUSTOM_URL          "Pool URL": optional. The network-info.txt of the network to join; empty, the public
#                       network's, as deploy/install.sh names it.
#   CUSTOM_USER_CONFIG  "Extra config arguments": KEY=VALUE, ONE PER LINE, from this allow-list only:
#                         YES=1             consent. Without it nothing is installed or started: h-run.sh
#                                           prints the installer's plan (install.sh --check) and waits.
#                         ROLE=miner|judge  miner by default. A judge needs the RAM deploy/hw_probe.sh asks
#                                           for (MOE_CPU_MIN_RAM_MB) and a larger disk. A rig without a usable
#                                           NVIDIA card mines nothing (no mining model runs on the CPU):
#                                           install.sh applies deploy/hw_probe.sh --role, judge or refused.
#                         LIGHT=1           read the chain from the network's public RPC instead of running a
#                                           node on this rig (install.sh --light).
# A key outside the allow-list is IGNORED, and NAMED. A value outside its allowed set -- and any value that
# carries a quote, a dollar sign, a backquote, a space or any other character outside letters, digits and
# . _ : / @ , = ? % + ~ - -- is REFUSED, never cleaned up: the refusal is written into the file, and h-run.sh
# then starts nothing and says why.
# The payout address is checked with the miner's own rule (final_season_address.py::payable_address, shipped
# in this package's lib/ from the release it was built from): an address one character off is refused here,
# before anything is installed. A refused wallet is printed only when it starts with dendra1 (a checksum error
# is worth seeing); anything else -- a recovery phrase, a private key in whatever encoding -- is refused
# without being printed or written.
# The configuration is READ BACK before it is said to be written; when it cannot be written, the previous one
# is overwritten with a refusal or removed, never left for h-run.sh to apply.
# HiveOS SOURCES this file: it returns, it never exits, and it leaves no setting of its own behind.

_dendra_hive_payable(){ # <lib dir> <address> -> 0 payable | 1 refused | 3 not verifiable; the reason on stdout
  [ -r "$1/final_season_address.py" ] || { echo "the address check is not in this package ($1)"; return 3; }
  command -v python3 >/dev/null 2>&1 || { echo "python3 is required to verify the address checksum, and this rig has none"; return 3; }
  python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1])
try:
    from final_season_address import payable_address
except Exception as e:
    print("the address check could not be loaded: " + type(e).__name__)
    sys.exit(3)
why = payable_address(sys.argv[2])
print(why)
sys.exit(1 if why else 0)' "$1" "$2" 2>/dev/null
}

_dendra_hive_config(){
  local here conf tmp wallet url line k v why rc shown body back refused="" ignored="" seen=" " yes=0 role=miner light=0
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  conf="${DENDRA_HIVE_CONF:-${CUSTOM_CONFIG_FILENAME:-$here/dendra.conf}}"
  wallet="${CUSTOM_TEMPLATE:-}"
  url="${CUSTOM_URL:-}"

  # the payout address -- required, checked, and never echoed when it looks like a secret
  if [ -z "$wallet" ]; then
    refused="${refused}the flight sheet names no wallet: set the wallet to your payout address (dendra1...) and the template to %WAL%
"
  else
    case "$wallet" in
      *[[:space:]]*)
        refused="${refused}the wallet holds several words: it looks like a RECOVERY PHRASE, which never belongs in a flight sheet (its value is not printed); if it is real, treat it as exposed
"; wallet="" ;;
    esac
  fi
  if [ -n "$wallet" ] && printf '%s\n' "$wallet" | grep -Eqx '(0x)?[0-9A-Fa-f]{64}'; then
    refused="${refused}the wallet looks like a PRIVATE KEY, which never belongs in a flight sheet (its value is not printed); if it is real, treat it as exposed
"; wallet=""
  fi
  if [ -n "$wallet" ]; then
    case "$wallet" in
      *[!A-Za-z0-9]*)
        refused="${refused}the wallet holds characters no address has: set the template to %WAL% alone, with no worker name (its value is not printed)
"; wallet="" ;;
    esac
  fi
  if [ -n "$wallet" ]; then
    why="$(_dendra_hive_payable "$here/lib" "$wallet")"; rc=$?
    # The value is shown only when it IS meant as an address (dendra1...): a checksum error is then worth
    # seeing. Anything else -- a private key in another encoding, words run together -- is never printed.
    case "$wallet" in
      dendra1*|DENDRA1*) shown="the wallet $wallet" ;;
      *) shown="the wallet (its value is not printed: it does not start with dendra1)" ;;
    esac
    case "$rc" in
      0) wallet="$(printf '%s' "$wallet" | tr 'A-Z' 'a-z')" ;;
      1) refused="${refused}$shown is not a payable address: ${why:-refused}
"; wallet="" ;;
      *) refused="${refused}the wallet cannot be verified on this rig: ${why:-unknown}
"; wallet="" ;;
    esac
  fi

  # the network -- optional
  if [ -n "$url" ]; then
    case "$url" in
      *[!A-Za-z0-9._:/@,=?%+~-]*)
        refused="${refused}the Pool URL holds a character this package does not pass on (a quote, a dollar sign, a space...): leave it empty, or give the network-info.txt address
"; url="" ;;
      http://*|https://*) : ;;
      *) refused="${refused}the Pool URL is not an http(s) address: leave it empty, or give the network-info.txt address
"; url="" ;;
    esac
  fi

  # the extra config -- an allow-list, one KEY=VALUE per line
  while IFS= read -r line || [ -n "$line" ]; do
    # A line ending typed in a browser may carry a carriage return: it ends the line, it is not a value.
    line="${line%[[:cntrl:]]}"
    case "$line" in ""|"#"*) continue ;; esac
    case "$line" in
      *=*) k="${line%%=*}"; v="${line#*=}" ;;
      *) refused="${refused}an Extra config line is not KEY=VALUE (one per line)
"; continue ;;
    esac
    case "$k" in
      ""|[0-9]*|*[!A-Za-z0-9_]*) refused="${refused}an Extra config line does not start with a NAME= (one KEY=VALUE per line)
"; continue ;;
    esac
    case "$k" in
      YES|ROLE|LIGHT) : ;;
      *) ignored="${ignored:+$ignored,}$k"; continue ;;
    esac
    case "$seen" in *" $k "*) refused="${refused}$k is given twice in the Extra config
"; continue ;; esac
    seen="$seen$k "
    case "$v" in
      *[!A-Za-z0-9._:/@,=?%+~-]*|"")
        refused="${refused}the value of $k carries a character this package does not pass on, or is empty (one KEY=VALUE per line, no quotes, no spaces)
"; continue ;;
    esac
    case "$k=$v" in
      YES=0|YES=1) yes="$v" ;;
      ROLE=miner|ROLE=judge) role="$v" ;;
      LIGHT=0|LIGHT=1) light="$v" ;;
      YES=*) refused="${refused}YES takes 1 (consent) or 0, not $v
" ;;
      ROLE=*) refused="${refused}ROLE takes miner or judge, not $v
" ;;
      LIGHT=*) refused="${refused}LIGHT takes 1 or 0, not $v
" ;;
    esac
  done <<< "${CUSTOM_USER_CONFIG:-}"

  [ -n "$ignored" ] && printf '[dendra] Extra config: IGNORED (not in the allow-list YES, ROLE, LIGHT): %s\n' "$ignored"
  # The file is built in memory, written once, and READ BACK before it is said to be written. A write that
  # fails -- a full disk, a read-only directory -- leaves no usable configuration behind: never the PREVIOUS
  # one, whose YES=1 h-run.sh would apply to a flight sheet that may no longer say so, and never an empty one
  # announced as written.
  body="$(printf '# Written by h-config.sh from the flight sheet. Read by h-run.sh, never sourced.\nWALLET=%s\nCONFIG_URL=%s\nYES=%s\nROLE=%s\nLIGHT=%s\nIGNORED=%s\n' "$wallet" "$url" "$yes" "$role" "$light" "$ignored"
          printf '%s' "$refused" | while IFS= read -r line; do [ -n "$line" ] && printf 'REFUSED=%s\n' "$line"; done
          printf 'x')"
  body="${body%x}"
  mkdir -p "$(dirname "$conf")" 2>/dev/null
  tmp="$conf.tmp.$$"
  back=""
  if printf '%s' "$body" > "$tmp" 2>/dev/null && mv -f "$tmp" "$conf" 2>/dev/null; then
    back="$(cat "$conf" 2>/dev/null; printf 'x')"
  fi
  if [ "$back" != "${body}x" ]; then
    rm -f "$tmp" 2>/dev/null
    line="the flight sheet could not be written to this file: restart the miner once the cause is fixed"
    # Overwritten in place first (a read-only directory may hold a writable file), removed otherwise.
    printf 'REFUSED=%s\n' "$line" > "$conf" 2>/dev/null
    [ "$(cat "$conf" 2>/dev/null)" = "REFUSED=$line" ] || rm -f "$conf" 2>/dev/null
    if [ -e "$conf" ] && ! grep -q '^REFUSED=' "$conf" 2>/dev/null; then
      printf '[dendra] REFUSED: the configuration could not be written to %s, and the previous one could not be removed: h-run.sh would apply it. Remove it by hand.\n' "$conf"
    else
      printf '[dendra] REFUSED: the configuration could not be written to %s: nothing is started until it can be.\n' "$conf"
    fi
    return 1
  fi
  if [ -n "$refused" ]; then
    printf '%s' "$refused" | while IFS= read -r line; do [ -n "$line" ] && printf '[dendra] REFUSED: %s\n' "$line"; done
  else
    printf '[dendra] configuration written: role %s, light %s, consent %s, payout address %s\n' "$role" "$light" "$yes" "$wallet"
  fi
  return 0
}

_dendra_hive_config
_dendra_hc_rc=$?
unset -f _dendra_hive_config _dendra_hive_payable
return "$_dendra_hc_rc" 2>/dev/null || exit "$_dendra_hc_rc"
