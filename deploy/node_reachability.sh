#!/usr/bin/env bash
# node_reachability.sh — can anyone REACH this node, or does it only reach out?
#
# ⛔ WHY THIS EXISTS. Every node on this network advertises `tcp://0.0.0.0:26656` — the address a node
# publishes when `external_address` is unset. It means "I listen on every local interface"; to anyone
# reading it from outside it is undialable. Measured on the live network: FIVE nodes out of five, zero
# reachable, every one of them dialling out to the same single host. The network is a star with one hub
# and no lateral link, and nothing anywhere said so. Each operator saw a healthy node — because from the
# inside it IS healthy: it syncs, it signs, it reports no error.
#
# The cost is not theoretical. A node with one peer has ONE source for every consensus message. When a
# block proposal is missed there is no second place to get it, the round is played without it, and the
# validator precommits nil — present, never absent, never jailed, contributing nothing. That failure is
# invisible from every indicator the chain publishes.
#
# ⚠️ WHAT THIS SCRIPT CANNOT DO, AND SAYS SO RATHER THAN PRETEND. It cannot test inbound reachability:
# that requires someone OUTSIDE dialling in, which no local check can perform. It observes what did
# arrive. Zero inbound peers over a long-running node is strong evidence, not proof — a brand-new node
# legitimately has none yet, and the summary states the uptime it was read at so the reader can judge.
#
# Usage:  bash deploy/node_reachability.sh
#         DENDRA_LOCAL_RPC=http://host:26657 bash deploy/node_reachability.sh
#         DENDRA_NETINFO_FILE=a.json DENDRA_STATUS_FILE=b.json bash deploy/node_reachability.sh   (bench)
#         bash deploy/node_reachability.sh --self-test

set -u

RPC="${DENDRA_LOCAL_RPC:-http://localhost:26657}"

# The source is ANNOUNCED in the summary. A green obtained from injected fixtures and a green obtained
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

case "${1:-}" in
  --self-test)
    # The bench lives in the development repository and is not part of a published tree. `exec` on a
    # missing path answers 127 with no explanation, which reads as a broken script rather than as an
    # absent file. Three answers, as everywhere here: ran / failed / NOT MEASURED.
    _BENCH="$(dirname "$0")/../dendra/onchain-staging/dendra_joignabilite_garde_test.sh"
    if [ ! -f "$_BENCH" ]; then
      echo "self-test NOT MEASURED: the bench is not present in this tree (development repository only)."
      exit 2
    fi
    exec bash "$_BENCH" ;;
esac

NET="$(_get /net_info "${DENDRA_NETINFO_FILE:-}")"
ST="$(_get /status "${DENDRA_STATUS_FILE:-}")"

# Rule of zero: an unreadable answer is neither a zero nor a pass. It exits 2 and says NOT MEASURED,
# because "this node has no inbound peers" and "this node could not be asked" are different statements
# and only one of them is about the network.
if [ -z "$NET" ] || [ -z "$ST" ]; then
  echo "REACHABILITY_SUMMARY source=$SOURCE node=unreachable inbound=? outbound=? advertised=?"
  echo "  NOT MEASURED. $RPC did not answer. That is not a verdict about the network, it is the"
  echo "  absence of one — the node may be down, or the RPC bound elsewhere. Nothing is concluded."
  exit 2
fi

REPORT="$(NET="$NET" ST="$ST" python3 - <<'PY'
import ipaddress, json, os, sys

try:
    net = json.loads(os.environ["NET"])["result"]
    st = json.loads(os.environ["ST"])["result"]
except Exception:
    sys.exit(3)

peers = net.get("peers") or []
# A peer this node dialled is `is_outbound: true`. One it ACCEPTED is inbound — and inbound is the only
# evidence that anybody out there can reach this address at all.
inbound = sum(1 for p in peers if not p.get("is_outbound"))
outbound = len(peers) - inbound

ni = st.get("node_info") or {}
listen = ni.get("listen_addr") or ""
rpc_addr = (ni.get("other") or {}).get("rpc_address") or ""
si = st.get("sync_info") or {}

print("INBOUND=%d" % inbound)
print("OUTBOUND=%d" % outbound)
print("LISTEN=%s" % listen)
print("RPCADDR=%s" % rpc_addr)
print("HEIGHT=%s" % si.get("latest_block_height", "?"))
print("CATCHUP=%s" % si.get("catching_up", "?"))
# An address is UNDIALABLE when the node publishes a wildcard: 0.0.0.0 and :: mean "every local
# interface", which is meaningful locally and meaningless to a reader on the other side of a router.
#
# ⛔ BUT A WILDCARD IS ONLY THE LOUDEST WAY TO BE UNDIALABLE, AND TESTING FOR IT ALONE PASSED THE
# QUIET HALF OF THE SAME FAULT. 192.168.1.42, 100.64.0.7 and even 127.0.0.1 are SYNTACTICALLY REAL
# addresses that no node elsewhere can dial — and unlike a wildcard they let peers on the SAME LAN
# connect, so the inbound count below can be non-zero while the node stays invisible to the rest of
# the network. Measured: with one inbound peer and listen_addr=tcp://127.0.0.1:26656 this script
# answered "genuine relay for the network", exit 0, for a node no other machine on earth can reach.
#
# The classes are named as NETWORKS and matched by CONTAINMENT, never by a string prefix — 100.64
# and 100.65 are both carrier NAT while 100.128 is not, and no prefix test gets that right. They are
# listed here rather than taken from ipaddress.is_private because that property answers a DIFFERENT
# question: it is also true of the documentation ranges (203.0.113.0/24, 2001:db8::/32), which are
# ordinary unicast addresses and not a topology fault. What is judged here is SCOPE — an address
# whose reach stops at a local network, or at a provider's NAT.
_LOCAL_SCOPE = [
    ("loopback",   "RFC 1122/4291", ["127.0.0.0/8", "::1/128"]),
    ("private",    "RFC 1918/4193", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]),
    ("link-local", "RFC 3927/4291", ["169.254.0.0/16", "fe80::/10"]),
    # Kept apart from the three above because the FIX is different, and getting it wrong costs an
    # operator an evening: this address is handed out by the PROVIDER's NAT, so no amount of port
    # forwarding on their own router opens it.
    ("carrier-nat", "RFC 6598", ["100.64.0.0/10"]),
]

def _host(addr):
    s = addr.split("://", 1)[-1].split("/", 1)[0]
    if s.startswith("["):
        return s[1:].split("]", 1)[0]
    return s.split(":", 1)[0] if s.count(":") == 1 else s

def _classify(addr):
    # THREE ANSWERS, NOT TWO. A host NAME cannot be classified without resolving it, and this script
    # does not resolve — a DNS lookup would make the verdict depend on the resolver, and a name is
    # the RECOMMENDED form of external_address, so refusing it would be a false red on the healthy
    # path. `unknown` is announced and handed to the inbound check below; it is never folded into
    # `public`, which would be this same defect one level down.
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
  echo "  NOT MEASURED. The node answered but the payload could not be read. Same rule as above:"
  echo "  unknown is not zero, and nothing is concluded."
  exit 2
fi
eval "$REPORT"

echo "  advertised address : $LISTEN ($ADVERTISED)"
echo "  peers              : $INBOUND inbound / $OUTBOUND outbound"
echo "  height             : $HEIGHT (catching_up=$CATCHUP)"
echo

RC=0
if [ "$WILDCARD" = "1" ]; then
  echo "  ✗ THIS NODE PUBLISHES A WILDCARD ADDRESS, SO NOBODY CAN DIAL IT."
  echo "    '$LISTEN' tells other nodes to connect to 'every interface', which is not an address they"
  echo "    can use. Until this is a real host:port, this node is a LEAF: it takes blocks from the"
  echo "    network and gives none back, and it has a single path for every consensus message."
  echo "    Fix, in this order — the second without the first advertises an address that does not answer:"
  echo "      1. forward TCP 26656 on your router to this machine, port 26656. ONLY that port."
  echo "      2. set external_address in config.toml to your public host:port, then restart."
  RC=1
elif [ "$ADVERTISED" = "carrier-nat" ]; then
  echo "  ✗ THIS ADDRESS IS NOT YOURS: YOUR PROVIDER'S NAT HANDED IT OUT ($ADVRFC)."
  echo "    '$LISTEN' sits inside 100.64.0.0/10, the range reserved for carrier-grade NAT. It looks"
  echo "    like an ordinary address and it is not one: the public address in front of it is shared"
  echo "    with other subscribers, and you do not control it."
  echo "    FORWARDING A PORT CANNOT FIX THIS, which is why it is said apart from the case above — the"
  echo "    router page an operator would spend the evening on has no effect on this one."
  echo "      1. ask your provider for a public IPv4, or for IPv6, then advertise that host:port."
  echo "      2. or run the node somewhere a public address exists."
  echo "      3. or keep it outbound-only, knowing it stays a LEAF and relays nothing back."
  RC=1
elif [ "$ADVERTISED" = "loopback" ] || [ "$ADVERTISED" = "private" ] || [ "$ADVERTISED" = "link-local" ]; then
  echo "  ✗ THIS NODE ADVERTISES A $ADVERTISED ADDRESS, WHICH ONLY ITS OWN NETWORK CAN REACH."
  echo "    '$LISTEN' is a real address, and that is exactly what makes it quiet: being $ADVERTISED"
  echo "    ($ADVRFC), it stops at this network. Peers on the same LAN still connect, so the inbound"
  echo "    count above can be non-zero while nobody outside can dial this node at all — an inbound"
  echo "    peer is evidence about the LAN here, not about the network."
  echo "    Fix, in this order — the second without the first advertises an address that does not answer:"
  echo "      1. forward TCP 26656 on your router to this machine, port 26656. ONLY that port."
  echo "      2. set external_address in config.toml to your PUBLIC host:port, then restart."
  RC=1
elif [ "$INBOUND" -eq 0 ]; then
  echo "  ✗ AN ADDRESS IS ADVERTISED, BUT NOTHING HAS EVER CONNECTED IN."
  echo "    '$LISTEN' is a real address, so the remaining explanation is that it does not answer from"
  echo "    outside: the port is not forwarded, a firewall drops it, or the operator is behind CGNAT"
  echo "    and cannot receive at all. Verify from another machine, not from this one."
  RC=1
else
  echo "  ✓ $INBOUND node(s) reached this one: it is a genuine relay for the network, not only a consumer."
fi

# ⛔ A SEPARATE HAZARD, AND THE ONE THAT ACTUALLY COSTS SOMETHING. The kit publishes 26657 next to
# 26656. An operator who forwards "the node" wholesale, or drops the machine into a router DMZ, exposes
# an unauthenticated RPC: anyone can submit transactions, read the topology and run expensive queries.
# It does not expose the signing key — that is a file — but it is a standing invitation.
if [ "$RPCWILD" = "1" ]; then
  echo
  echo "  ⚠ RPC listens on $RPCADDR. Forward port 26656 ONLY, and never put this machine in a DMZ:"
  echo "    an open RPC is a different exposure from an open P2P port, and a worse one."
fi

echo
echo "  ⊘ NOT COVERED: this check cannot dial itself from outside. It reports what DID arrive, so a"
echo "    node started minutes ago legitimately shows zero inbound. Read it against the height above."
if [ "$ADVERTISED" = "unknown" ]; then
  echo "    Nor was the advertised address classified: '$LISTEN' is a NAME, and resolving it would make"
  echo "    this verdict depend on whichever resolver answered. A name is the recommended form, so it"
  echo "    is not held against you — but 'unknown' here means unclassified, never verified-reachable."
fi
echo "REACHABILITY_SUMMARY source=$SOURCE inbound=$INBOUND outbound=$OUTBOUND advertised=$ADVERTISED wildcard=$WILDCARD rpc_wildcard=$RPCWILD height=$HEIGHT defects=$RC"
exit $RC
