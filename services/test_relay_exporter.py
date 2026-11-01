#!/usr/bin/env python3
"""The full relay observability path: real relay -> /stats -> exporter -> Prometheus.

The relay does not log its requests on purpose: a request path carries identifiers
(`<kind>/<jobId>__<minerId>`). Without logs the risk moves to availability: a third party that fills
the store gets legitimate deposits refused (507), and nothing says so.

Aggregate counters answer that, but the two halves can break separately: the relay may publish
counters the exporter does not read, or the exporter may publish frozen metrics that nothing feeds.
This test measures the whole path rather than each link.

It also checks that no identifier leaks. A key carrying identifiers is deposited, and no fragment of it
may appear in the Prometheus output, or the supervision layer would rebuild the leak that the silent
`log_message` avoids.

Usage: python3 test_relay_exporter.py (exit 0 = green, 1 = red, 2 = not measurable)
"""
import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

JID, MID = "job1785333936418", "miner7"
TOKEN = "a" * 32
V, K = 0, 0


def ok(m):
    global V
    V += 1
    print(f"  OK   {m}")


def ko(m, detail=""):
    global K
    K += 1
    print(f"  FAIL {m}")
    if detail:
        print(f"      {detail}")


try:
    from prometheus_client import generate_latest  # noqa: F401
except Exception:
    # Not measurable is not green. Without the dependency nothing is claimed: exit code 2, which is
    # distinct from green.
    print("  ~ not measured: prometheus_client is missing (pip install --break-system-packages prometheus_client)")
    print("    Relay observability is not verified, which is not the same as \"it works\".")
    sys.exit(2)

# The store lives in a temporary directory: the relay would otherwise create `relay-store/` in the
# current directory, and `test_relay_store.py`, which runs after this script in the same working copy,
# checks that no such residue exists.
_STORE = tempfile.mkdtemp(prefix="dendra-relay-exporter-")
os.environ["DENDRA_RELAY_STORE"] = _STORE
os.environ["DENDRA_RELAY_TOKEN"] = TOKEN
os.environ["DENDRA_RELAY_RATE"] = "1000"
import relay as rs  # noqa: E402

importlib.reload(rs)
srv = rs.ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)

print("== Relay observability path: relay -> /stats -> exporter -> Prometheus ==")
try:
    # A real deposit, under a key carrying identifiers: this is the subject of the witness below.
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/pub/{JID}__{MID}", data=b'{"pub":"ab"}',
                                 method="POST", headers={"X-Dendra-Token": TOKEN})
    urllib.request.urlopen(req, timeout=5)

    os.environ["DENDRA_RELAY_BASE"] = f"http://127.0.0.1:{PORT}"
    # No `importlib.reload` on the exporter: it would re-register its Gauges and prometheus_client
    # raises `DuplicateTimeseries`. Since `RELAY_BASE` is read at import, the address must be set
    # before the import, and only once, which is what the line above does.
    import exporter as ex  # noqa: E402

    ex.refresh_relay()
    from prometheus_client import generate_latest

    output = generate_latest().decode()

    if "dendra_relay_up 1.0" in output:
        ok("the exporter reached the relay and says so (dendra_relay_up=1)")
    else:
        ko("dendra_relay_up is not 1 — the observability chain is broken",
           [l for l in output.splitlines() if "relay_up" in l])

    for m in ("dendra_relay_store_entries", "dendra_relay_store_cap", "dendra_relay_store_saturation"):
        if m in output:
            ok(f"{m} is published")
        else:
            ko(f"{m} is missing: the store-saturation signal is incomplete")

    saturation_lines = [l for l in output.splitlines() if l.startswith("dendra_relay_store_saturation")]
    if saturation_lines and float(saturation_lines[0].split()[-1]) >= 0:
        ok(f"the saturation signal carries a number: {saturation_lines[0]}")
    else:
        ko("saturation unreadable", saturation_lines)

    # The witness: no identifier may travel through to the supervision layer.
    leaks = [f for f in (JID, MID, f"{JID}__{MID}") if f in output]
    if leaks:
        ko(f"identifier(s) in the metrics: {leaks}; supervision would rebuild the leak that the "
           "silent log avoids")
    else:
        ok("witness: the deposit carried a jobId and a minerId, and neither reaches Prometheus")

    # The witness only counts if the measurement happened; otherwise "nothing found" is true of nothing.
    if any(l.startswith('dendra_relay_store_entries{kind="pub"}') and float(l.split()[-1]) >= 1
           for l in output.splitlines()):
        ok("...and the deposit was counted: the absence of identifiers comes from a real measurement")
    else:
        ko("the deposit appears nowhere — the witness above would prove nothing")

    # Unreachable relay => up=0, and above all no exception, which would blind the chain metrics too.
    srv.shutdown()
    time.sleep(0.1)
    try:
        ex.refresh_relay()
        output2 = generate_latest().decode()
        if "dendra_relay_up 0.0" in output2:
            ok("silent relay -> dendra_relay_up=0, without an exception (the chain is not blamed)")
        else:
            ko("silent relay but up != 0: a frozen metric reads as \"everything is fine\"")
    except Exception as e:
        ko(f"refresh_relay raised on a silent relay ({type(e).__name__}); it would blind the whole exporter")
finally:
    try:
        srv.shutdown()
    except Exception:
        pass
    shutil.rmtree(_STORE, ignore_errors=True)

print(f"\nRELAY_EXPORTER_SUMMARY green={V} red={K}")
sys.exit(1 if K else 0)
