#!/usr/bin/env bash
# node_reachability.sh -- can anyone reach this node, or does it only reach out?
#
# Why this exists: a node advertises `tcp://0.0.0.0:26656` when `external_address` is unset. That means
# "I listen on every local interface", which nobody outside can dial. Measured on the live network: five
# nodes out of five unreachable, each dialling out to the same single host, so the network was a star with
# one hub and no lateral link, while every operator saw a healthy node (it syncs, it signs, it reports no
# error).
#
# A node with one peer has one source for every consensus message. When a block proposal is missed there is
# no second place to get it, the round is played without it, and the validator precommits nil: present,
# never absent, never jailed, contributing nothing. No indicator the chain publishes shows that failure.
#
# What this script cannot do: test inbound reachability, which requires someone outside dialling in. It
# observes what did arrive. Zero inbound peers on a long-running node is strong evidence, not proof: a new
# node legitimately has none yet, and the summary states the uptime it was read at so the reader can judge.
#
# Usage:  bash deploy/node_reachability.sh
#         DENDRA_LOCAL_RPC=http://host:26657 bash deploy/node_reachability.sh
#         DENDRA_NETINFO_FILE=a.json DENDRA_STATUS_FILE=b.json bash deploy/node_reachability.sh   (bench)
#         bash deploy/node_reachability.sh --self-test
#         bash deploy/node_reachability.sh --json     one line of JSON for a program, nothing else on stdout
#
# Exit codes (the same with --json): 0 other nodes have connected in, 1 this node cannot be reached or nothing
# has connected in, 2 not measured (the RPC did not answer, or answered something unreadable).

set -u

RPC="${DENDRA_LOCAL_RPC:-http://localhost:26657}"

# The source is announced in the summary. A green obtained from injected fixtures and a green obtained
# from a live node must not read the same, or a bench result gets quoted as a measurement.
if [ -n "${DENDRA_NETINFO_FILE:-}" ] || [ -n "${DENDRA_STATUS_FILE:-}" ]; then
  SOURCE=injected
else
  SOURCE=node
fi

_get() {  # _get <path> <fixture-var-content>
  if [ -n "${2:-}" ]; then cat "$2" 2>/dev/null; return; fi
  curl -s -m 15 "$RPC$1" 2>/dev/null || true
}

JSON=0
[ "${1:-}" = "--json" ] && JSON=1
# In JSON mode the human text goes nowhere and file descriptor 3 carries the one line a program reads.
if [ "$JSON" = 1 ]; then exec 3>&1 1>/dev/null; fi

emit_json() {  # emit_json <state> <case> <exit code>
  export R_STATE="$1" R_CASE="$2" R_EXIT="$3" R_SOURCE="$SOURCE" R_LISTEN="${LISTEN:-}" R_ADV="${ADVERTISED:-}"
  export R_IN="${INBOUND:-}" R_OUT="${OUTBOUND:-}" R_HEIGHT="${HEIGHT:-}" R_CATCH="${CATCHUP:-}" R_RPCWILD="${RPCWILD:-}"
  python3 -c '
import json, os
e = os.environ
def num(k):
    v = e.get(k, "")
    return int(v) if v.isdigit() else None
listen = e.get("R_LISTEN", "")
state = e["R_STATE"]
doc = {"state": state, "case": e["R_CASE"], "source": e["R_SOURCE"], "exit": int(e["R_EXIT"]),
       "listen": listen or None, "advertised": e.get("R_ADV") or None,
       "inbound": num("R_IN"), "outbound": num("R_OUT"), "height": num("R_HEIGHT"),
       "catching_up": {"True": True, "False": False}.get(e.get("R_CATCH", "")),
       "rpc_wildcard": {"1": True, "0": False}.get(e.get("R_RPCWILD", "")),
       "address": listen.split("://", 1)[-1] if state == "open" and listen else None}
print(json.dumps(doc))
' >&3
}

case "${1:-}" in
  --self-test)
    # The bench lives in the development repository and is not part of a published tree. `exec` on a
    # missing path answers 127 with no explanation, which reads as a broken script rather than as an
    # absent file. Three answers, as everywhere here: ran / failed / not measured.
    _BENCH="$(dirname "$0")/../dendra/onchain-staging/dendra_reachability_guard_test.sh"
    if [ ! -f "$_BENCH" ]; then
      echo "self-test not measured: the bench is not present in this tree (development repository only)."
      exit 2
    fi
    exec bash "$_BENCH" ;;
esac

NET="$(_get /net_info "${DENDRA_NETINFO_FILE:-}")"
ST="$(_get /status "${DENDRA_STATUS_FILE:-}")"

# Rule of zero: an unreadable answer is neither a zero nor a pass. It exits 2 and says not measured,
# because "this node has no inbound peers" and "this node could not be asked" are different statements
# and only one of them is about the network.
if [ -z "$NET" ] || [ -z "$ST" ]; then
  echo "REACHABILITY_SUMMARY source=$SOURCE node=unreachable inbound=? outbound=? advertised=?"
  echo "  Not measured: $RPC did not answer, so nothing is concluded about the network. The node"
  echo "  may be down, or its RPC bound elsewhere."
  if [ "$JSON" = 1 ]; then emit_json unknown node-unreachable 2; fi
  exit 2
fi

REPORT="$(NET="$NET" ST="$ST" python3 - <<'PY'
import ipaddress, json, os, shlex, sys

try:
    net = json.loads(os.environ["NET"])["result"]
    st = json.loads(os.environ["ST"])["result"]
except Exception:
    sys.exit(3)

peers = net.get("peers") or []
# A peer this node dialled is `is_outbound: true`. One it accepted is inbound, and inbound is the only
# evidence that anybody out there can reach this address at all.
inbound = sum(1 for p in peers if not p.get("is_outbound"))
outbound = len(peers) - inbound

ni = st.get("node_info") or {}
listen = ni.get("listen_addr") or ""
rpc_addr = (ni.get("other") or {}).get("rpc_address") or ""
si = st.get("sync_info") or {}

print("INBOUND=%d" % inbound)
print("OUTBOUND=%d" % outbound)
print("LISTEN=%s" % shlex.quote(listen))
print("RPCADDR=%s" % shlex.quote(rpc_addr))
print("HEIGHT=%s" % shlex.quote(str(si.get("latest_block_height", "?"))))
print("CATCHUP=%s" % shlex.quote(str(si.get("catching_up", "?"))))
# An address is undialable when the node publishes a wildcard: 0.0.0.0 and :: mean "every local
# interface", which is meaningful locally and meaningless to a reader on the other side of a router.
#
# A wildcard is only the most obvious case. 192.168.1.42, 100.64.0.7 and even 127.0.0.1 are real
# addresses that no node elsewhere can dial, and unlike a wildcard they let peers on the same LAN
# connect, so the inbound count below can be non-zero while the node stays invisible to the rest of the
# network. Measured: with one inbound peer and listen_addr=tcp://127.0.0.1:26656 this script once
# answered "genuine relay for the network", exit 0.
#
# The classes are named as networks and matched by containment, never by a string prefix: 100.64 and
# 100.65 are both carrier NAT while 100.128 is not. They are listed here rather than taken from
# ipaddress.is_private because that property answers a different question: it is also true of the
# documentation ranges (203.0.113.0/24, 2001:db8::/32), which are ordinary unicast addresses and not a
# topology fault. What is judged here is scope: an address whose reach stops at a local network, or at a
# provider's NAT.
_LOCAL_SCOPE = [
    ("loopback",   "RFC 1122/4291", ["127.0.0.0/8", "::1/128"]),
    ("private",    "RFC 1918/4193", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]),
    ("link-local", "RFC 3927/4291", ["169.254.0.0/16", "fe80::/10"]),
    # Kept apart from the three above because the fix is different: this address is handed out by the
    # provider's NAT, so no amount of port forwarding on the operator's own router opens it.
    ("carrier-nat", "RFC 6598", ["100.64.0.0/10"]),
]

def _host(addr):
    s = addr.split("://", 1)[-1].split("/", 1)[0]
    if s.startswith("["):
        return s[1:].split("]", 1)[0]
    return s.split(":", 1)[0] if s.count(":") == 1 else s

def _classify(addr):
    # Three answers, not two. A host name cannot be classified without resolving it, and this script
    # does not resolve (a DNS lookup would make the verdict depend on the resolver). A name is the
    # recommended form of external_address, so refusing it would be a false red on the healthy path.
    # `unknown` is announced and handed to the inbound check below; it is never folded into `public`.
    try:
        ip = ipaddress.ip_address(_host(addr))
    except ValueError:
        return "unknown", ""
    if ip.is_unspecified:
        return "wildcard", ""
    for name, rfc, nets in _LOCAL_SCOPE:
        if any(ip in ipaddress.ip_network(n) for n in nets):
            return name, rfc
    return "public", ""

wildcard = 1 if ("0.0.0.0" in listen or "[::]" in listen or listen == "") else 0
adv, adv_rfc = ("wildcard", "") if wildcard else _classify(listen)
print("WILDCARD=%d" % wildcard)
# Quoted: the RFC label carries a space, and this block is consumed by `eval`.
print('ADVERTISED="%s"' % adv)
print('ADVRFC="%s"' % adv_rfc)
print("RPCWILD=%d" % (1 if ("0.0.0.0" in rpc_addr or "[::]" in rpc_addr) else 0))
PY
)" || REPORT=""

if [ -z "$REPORT" ]; then
  echo "REACHABILITY_SUMMARY source=$SOURCE node=answered-unparsable inbound=? outbound=? advertised=?"
  echo "  Not measured: the node answered but the payload could not be read. Same rule as above:"
  echo "  unknown is not zero, and nothing is concluded."
  if [ "$JSON" = 1 ]; then emit_json unknown unparsable 2; fi
  exit 2
fi
eval "$REPORT"

echo "  advertised address : $LISTEN ($ADVERTISED)"
echo "  peers              : $INBOUND inbound / $OUTBOUND outbound"
echo "  height             : $HEIGHT (catching_up=$CATCHUP)"
echo

RC=0
CASE=open
if [ "$WILDCARD" = "1" ]; then
  CASE=wildcard
  echo "  [FAIL] This node publishes a wildcard address, so nobody can dial it."
  echo "    '$LISTEN' tells other nodes to connect to 'every interface', which is not an address they"
  echo "    can use. Until this is a real host:port, this node is a leaf: it takes blocks from the"
  echo "    network and gives none back, and it has a single path for every consensus message."
  echo "    Fix, in this order (the second without the first advertises an address that does not answer):"
  echo "      1. forward TCP 26656 on your router to this machine, port 26656. Only that port."
  echo "      2. set external_address in config.toml to your public host:port, then restart."
  RC=1
elif [ "$ADVERTISED" = "carrier-nat" ]; then
  CASE=carrier-nat
  echo "  [FAIL] This address is not yours: your provider's NAT handed it out ($ADVRFC)."
  echo "    '$LISTEN' sits inside 100.64.0.0/10, the range reserved for carrier-grade NAT. It looks"
  echo "    like an ordinary address but is not one: the public address in front of it is shared"
  echo "    with other subscribers, and you do not control it."
  echo "    Forwarding a port cannot fix this, which is why it is reported apart from the case above:"
  echo "    your router settings have no effect on this one."
  echo "      1. ask your provider for a public IPv4, or for IPv6, then advertise that host:port."
  echo "      2. or run the node somewhere a public address exists."
  echo "      3. or keep it outbound-only, knowing it stays a leaf and relays nothing back."
  RC=1
elif [ "$ADVERTISED" = "loopback" ] || [ "$ADVERTISED" = "private" ] || [ "$ADVERTISED" = "link-local" ]; then
  CASE="$ADVERTISED"
  echo "  [FAIL] This node advertises a $ADVERTISED address, which only its own network can reach."
  echo "    '$LISTEN' is a real address, which is why the problem is easy to miss: being $ADVERTISED"
  echo "    ($ADVRFC), it stops at this network. Peers on the same LAN still connect, so the inbound"
  echo "    count above can be non-zero while nobody outside can dial this node at all: an inbound"
  echo "    peer is evidence about the LAN here, not about the network."
  echo "    Fix, in this order (the second without the first advertises an address that does not answer):"
  echo "      1. forward TCP 26656 on your router to this machine, port 26656. Only that port."
  echo "      2. set external_address in config.toml to your public host:port, then restart."
  RC=1
elif [ "$INBOUND" -eq 0 ]; then
  CASE=no-inbound
  echo "  [FAIL] An address is advertised, but nothing has ever connected in."
  echo "    '$LISTEN' is a real address, so the remaining explanation is that it does not answer from"
  echo "    outside: the port is not forwarded, a firewall drops it, or the operator is behind CGNAT"
  echo "    and cannot receive at all. Verify from another machine, not from this one."
  RC=1
else
  echo "  [OK] $INBOUND node(s) reached this one: it is a genuine relay for the network, not only a consumer."
fi

# A separate hazard, and a costlier one. The kit publishes 26657 next to 26656. An operator who forwards
# "the node" wholesale, or puts the machine in a router DMZ, exposes an unauthenticated RPC: anyone can
# submit transactions, read the topology and run expensive queries. It does not expose the signing key
# (that is a file).
if [ "$RPCWILD" = "1" ]; then
  echo
  echo "  [!] RPC listens on $RPCADDR. Forward port 26656 only, and never put this machine in a DMZ:"
  echo "    an open RPC is a different exposure from an open P2P port, and a worse one."
fi

echo
echo "  [i] Not covered: this check cannot dial itself from outside. It reports what did arrive, so a"
echo "    node started minutes ago legitimately shows zero inbound. Read it against the height above."
if [ "$ADVERTISED" = "unknown" ]; then
  echo "    Nor was the advertised address classified: '$LISTEN' is a name, and resolving it would make"
  echo "    this verdict depend on whichever resolver answered. A name is the recommended form, so it"
  echo "    is not held against you, but 'unknown' here means unclassified, never verified-reachable."
fi
echo "REACHABILITY_SUMMARY source=$SOURCE inbound=$INBOUND outbound=$OUTBOUND advertised=$ADVERTISED wildcard=$WILDCARD rpc_wildcard=$RPCWILD height=$HEIGHT defects=$RC"
if [ "$JSON" = 1 ]; then
  if [ "$RC" = 0 ]; then emit_json open "$CASE" 0; else emit_json closed "$CASE" "$RC"; fi
fi
exit $RC
