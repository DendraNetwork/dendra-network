"""THE STORE ALARM AT STARTUP, measured on the real process.

A relay restarted over a store that already holds more than FILL_ALARM of a kind's budget must say so
in its startup banner: no deposit may arrive to trigger the line, and an operator reading the boot is
the one who can still act before deposits are refused. The banner is printed by `main()`, which a bench
that imports the module never runs — test_relay_store.py holds `_boot_store` and
`_startup_alarm_lines`, and only starting the process holds that someone PRINTS them.

The store is filled on disk BEFORE the start, through the relay's own `DiskStore`, past the alarm of the
compiled budget floor: the environment can raise the budget and never lower it, so the floor is the
smallest budget a real process runs with. That is about 55 MB written to a throwaway directory.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

# THE ROOT IS POINTABLE (DENDRA_MODEA), as in test_boot_public.py: a bench pinned to its own path reads
# the shipped tree whatever copy it is shown, so mutating a copy would return the same green.
ROOT = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

import relay_store  # noqa: E402

FLOOR_MIB = 64          # relay.py::KIND_BUDGET_FLOOR, read back below from the banner it prints
BODY = 1 << 20          # one MiB per record: 52 of them pass 80 % of the floor, 50 would not


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_relay(store, timeout=12):
    """Start the shipped relay on loopback over `store`; return everything it printed before the timeout
    stopped it (a relay that starts listens until killed)."""
    env = dict(os.environ)
    for k in ("DENDRA_PUBLIC", "DENDRA_RELAY_TOKEN", "DENDRA_RELAY_TOKEN_PREV", "DENDRA_RELAY_KIND_BUDGET_MIB",
              "DENDRA_RELAY_RETENTION"):
        env.pop(k, None)
    env.update({"DENDRA_RELAY_STORE": store, "DENDRA_RELAY_HOST": "127.0.0.1", "PYTHONUNBUFFERED": "1"})

    def _txt(x):
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")
    try:
        p = subprocess.run([sys.executable, str(ROOT / "relay.py"), str(_free_port())], env=env,
                           timeout=timeout, capture_output=True, text=True)
        return p.returncode, _txt(p.stdout) + _txt(p.stderr)
    except subprocess.TimeoutExpired as t:
        return -1, _txt(t.stdout) + _txt(t.stderr)


def _filled(prefix, n):
    store = tempfile.mkdtemp(prefix=prefix)
    disk = relay_store.DiskStore(store, ["reveal"])
    for i in range(n):
        disk.write("reveal", "job%013d__dm1alarm" % i, b"v" * BODY)
    return store


def test_a_store_reloaded_over_the_alarm_is_named_by_the_startup_banner():
    store = _filled("dendra-relay-startup-alarm-", 52)
    try:
        code, out = _run_relay(store)
    finally:
        shutil.rmtree(store, ignore_errors=True)
    assert code == -1, f"the relay did not stay up: code {code}: {out[-600:]}"
    assert "budget %d MiB/kind" % FLOOR_MIB in out, f"not the compiled floor this case is sized for: {out[:600]}"
    assert "52 record(s) reloaded" in out, out[:600]
    assert "store ALARM at startup: kind reveal reloaded at 81% of its budget" in out, out[:900]
    assert "dm1alarm" not in out, "the banner printed a key: it names categories, never identifiers"


def test_a_store_under_the_alarm_starts_without_it():
    store = _filled("dendra-relay-startup-calm-", 3)
    try:
        code, out = _run_relay(store)
    finally:
        shutil.rmtree(store, ignore_errors=True)
    assert code == -1 and "3 record(s) reloaded" in out, out[-600:]
    assert "store ALARM" not in out, out[:900]
