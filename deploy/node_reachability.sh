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
import json, os, sys

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
print("WILDCARD=%d" % (1 if ("0.0.0.0" in listen or "[::]" in listen or listen == "") else 0))
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

echo "  advertised address : $LISTEN"
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
echo "REACHABILITY_SUMMARY source=$SOURCE inbound=$INBOUND outbound=$OUTBOUND wildcard=$WILDCARD rpc_wildcard=$RPCWILD height=$HEIGHT defects=$RC"
exit $RC
