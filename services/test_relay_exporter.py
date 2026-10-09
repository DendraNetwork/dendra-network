#!/usr/bin/env python3
"""The FULL relay observability path: real relay -> /stats -> exporter -> Prometheus.

═══ WHY THIS TEST EXISTS ═══════════════════════════════════════════════════════════════════════════
The relay DOES NOT LOG its requests, and that is a GUARD: a request path carries the identifiers
(`<kind>/<jobId>__<minerId>`). The real hole that decision uncovered is not confidentiality — it is
availability: a third party that fills the store makes legitimate deposits REFUSED (507), and the
network goes dark for a day to a week **with nothing saying so**.

Aggregate counters are the answer. But the two halves can break separately, and the relay's own unit
benches only see one of them: the relay may publish perfect counters the exporter does not read, or
the exporter may publish frozen metrics nothing feeds any more. The CHAIN is measured, not its links.

AND THE WITNESS IS THE HALF THAT MATTERS. A key CARRYING identifiers is deposited, and no fragment of
it may appear in the Prometheus output. Without that witness, "counters" would drift into "aggregated
paths" at the first evolution, and the supervision layer would rebuild the very leak the silence of
`log_message` exists to avoid.

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
JETON = "a" * 32
V, K = 0, 0


def ok(m):
    global V
    V += 1
    print(f"  ✓ {m}")


def ko(m, detail=""):
    global K
    K += 1
    print(f"  ✗ {m}")
    if detail:
        print(f"      {detail}")


try:
    from prometheus_client import generate_latest  # noqa: F401
except Exception:
    # ⚠️ NOT MEASURABLE IS NOT GREEN. Without the dependency nothing is claimed: exit code 2, which is
    # distinct from green.
    print("  ~ NOT MEASURED: prometheus_client is missing (pip install --break-system-packages prometheus_client)")
    print("    Relay observability is NOT verified. That is not the same as \"it works\".")
    sys.exit(2)

# ⛔ THE STORE LIVES IN A TEMPORARY DIRECTORY, OR THIS TEST BREAKS ANOTHER ONE. The relay creates
# `relay-store/` in the CURRENT directory, and `test_relay_store.py` checks precisely that no residue
# exists. The suite runs the standalone scripts in alphabetical order within the SAME working copy, and
# "relais" sorts before "relay", so this test would manufacture the residue that fails the next one — a
# red that accuses neither script, only their ORDER.
_STORE = tempfile.mkdtemp(prefix="dendra-relais-exporter-")
os.environ["DENDRA_RELAY_STORE"] = _STORE
os.environ["DENDRA_RELAY_TOKEN"] = JETON
os.environ["DENDRA_RELAY_RATE"] = "1000"
import relay as rs  # noqa: E402

importlib.reload(rs)
srv = rs.ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)

print("== RELAY OBSERVABILITY CHAIN: relay -> /stats -> exporter -> Prometheus ==")
try:
    # A REAL deposit, under a key carrying identifiers: this is the subject of the witness below.
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/pub/{JID}__{MID}", data=b'{"pub":"ab"}',
                                 method="POST", headers={"X-Dendra-Token": JETON})
    urllib.request.urlopen(req, timeout=5)

    os.environ["DENDRA_RELAY_BASE"] = f"http://127.0.0.1:{PORT}"
    # ⚠️ NO `importlib.reload` ON THE EXPORTER: it would re-register its Gauges and prometheus_client
    # raises `DuplicateTimeseries`. Since `RELAY_BASE` is read AT IMPORT, the address must be set
    # BEFORE the import, and only once, which is what the line above does.
    import exporter as ex  # noqa: E402

    ex.refresh_relay()
    from prometheus_client import generate_latest

    sortie = generate_latest().decode()

    if "dendra_relay_up 1.0" in sortie:
        ok("the exporter REACHED the relay and says so (dendra_relay_up=1)")
    else:
        ko("dendra_relay_up is not 1 — the observability chain is broken",
           [l for l in sortie.splitlines() if "relay_up" in l])

    for m in ("dendra_relay_store_entries", "dendra_relay_store_cap", "dendra_relay_store_saturation"):
        if m in sortie:
            ok(f"{m} is published")
        else:
            ko(f"{m} is MISSING — the store-saturation signal is incomplete")

    lignes_sat = [l for l in sortie.splitlines() if l.startswith("dendra_relay_store_saturation")]
    if lignes_sat and float(lignes_sat[0].split()[-1]) >= 0:
        ok(f"the saturation signal carries a number: {lignes_sat[0]}")
    else:
        ko("saturation unreadable", lignes_sat)

    # ⛔ THE WITNESS: no identifier may travel through to the supervision layer.
    fuites = [f for f in (JID, MID, f"{JID}__{MID}") if f in sortie]
    if fuites:
        ko(f"⛔ IDENTIFIER(S) in the metrics: {fuites} — supervision REBUILDS the leak that the "
           "silence of the log avoids")
    else:
        ok("WITNESS: the deposit carried a jobId and a minerId, and NEITHER reaches Prometheus")

    # ...and the witness is only worth something if the measurement actually happened, otherwise
    # "nothing found" is true of nothing.
    if any(l.startswith('dendra_relay_store_entries{kind="pub"}') and float(l.split()[-1]) >= 1
           for l in sortie.splitlines()):
        ok("...and the deposit WAS counted: the absence of identifiers is about a real measurement")
    else:
        ko("the deposit appears nowhere — the witness above would prove nothing")

    # Unreachable relay => up=0, and above all NO exception, which would blind the chain metrics too.
    srv.shutdown()
    time.sleep(0.1)
    try:
        ex.refresh_relay()
        sortie2 = generate_latest().decode()
        if "dendra_relay_up 0.0" in sortie2:
            ok("silent relay -> dendra_relay_up=0, without an exception (the chain is not blamed)")
        else:
            ko("silent relay but up != 0: a FROZEN metric reads as \"everything is fine\"")
    except Exception as e:
        ko(f"refresh_relay RAISED on a silent relay ({type(e).__name__}) — it would blind the whole exporter")
finally:
    try:
        srv.shutdown()
    except Exception:
        pass
    shutil.rmtree(_STORE, ignore_errors=True)

print(f"\nRELAY_EXPORTER_RESUME green={V} red={K}")
sys.exit(1 if K else 0)
