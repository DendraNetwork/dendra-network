# -*- coding: utf-8 -*-
"""Bench for two relay guards that only hold under concurrency: atomic write-once, and silent sockets.

Both cases EXECUTE the shipped code — they import `relay`, they do not restate its logic. A
bench that replays a rule instead of running it cannot see an invocation defect, and both defects
here were invocation defects: a lock released one line too early, and an attribute never set.
"""
import os, sys, socket, threading, time, tempfile, importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAIL = 0


def ok(msg):
    print("  [ok] " + msg)


def ko(msg):
    global FAIL
    FAIL = 1
    print("  [KO] " + msg)


def load(store_dir, **env):
    for k, v in env.items():
        os.environ[k] = v
    os.environ["DENDRA_RELAY_STORE"] = store_dir
    import relay as rs
    importlib.reload(rs)
    return rs


# -- 1) WRITE-ONCE: two CONCURRENT deposits carrying DIFFERENT bytes ------------------------------
def case_write_once():
    d = tempfile.mkdtemp()
    rs = load(d)
    rs._boot_store()

    results = []
    gate = threading.Barrier(2)

    def deposit(payload):
        gate.wait()              # both threads enter _put together
        try:
            rs._put("res", "jobX__minerY", payload)
            results.append(("ok", payload))
        except Exception as e:
            results.append((type(e).__name__, payload))

    t1 = threading.Thread(target=deposit, args=(b'{"a":1}',))
    t2 = threading.Thread(target=deposit, args=(b'{"b":2}',))
    t1.start(); t2.start(); t1.join(); t2.join()

    accepted = [r for r in results if r[0] == "ok"]
    refused = [r for r in results if r[0] == "Conflict"]
    if len(accepted) == 1 and len(refused) == 1:
        ok("concurrent write-once: exactly 1 accepted, 1 Conflict (%s)" % results)
    else:
        ko("concurrent write-once: %s (want 1 ok + 1 Conflict)" % results)

    # What is in RAM must be what is on DISK: a partial overwrite would separate the two, and the
    # sealed artifact the chain hashes is the one on disk.
    ram = rs._get("res", "jobX__minerY")
    rs.STORE["res"].clear(); rs.TS["res"].clear()
    rs._boot_store()
    disk = rs._get("res", "jobX__minerY")
    if ram == disk and ram is not None:
        ok("RAM and disk hold the SAME artifact after the race")
    else:
        ko("RAM=%r disk=%r: the race split them apart" % (ram, disk))

    # Re-depositing the SAME bytes stays accepted: a lost 200 must not freeze the job it completes.
    try:
        rs._put("res", "jobX__minerY", ram)
        ok("re-deposit of identical bytes: accepted, nothing destroyed")
    except Exception as e:
        ko("re-deposit of identical bytes refused: %s" % type(e).__name__)


# -- 2) SILENT SOCKET: the connection must be dropped, not held ------------------------------------
def case_silent_socket():
    d = tempfile.mkdtemp()
    rs = load(d, DENDRA_RELAY_TIMEOUT="2")
    rs._boot_store()

    if rs.Handler.timeout is None:
        ko("Handler.timeout is None: a silent socket would hold its thread for ever")
        return
    ok("Handler.timeout armed at %s s" % rs.Handler.timeout)

    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.settimeout(10)
        t0 = time.time()
        try:
            data = s.recv(4096)      # nothing is ever sent: the server must let go
            closed = (data == b"")
        except socket.timeout:
            closed = False
        except OSError:
            closed = True
        dt = time.time() - t0
        s.close()
        if closed and dt < 8:
            ok("silent socket dropped after %.1f s (< 8 s)" % dt)
        else:
            ko("silent socket still held after %.1f s (closed=%s)" % (dt, closed))
    finally:
        srv.shutdown(); srv.server_close()

    # Three answers on the value: unreadable and zero fall back to the default, never to None — a
    # zero would mean "no limit" to the socket layer, i.e. exactly the state this guard removes.
    for raw, want in (("", 30.0), ("0", 30.0), ("-5", 30.0), ("abc", 30.0), ("7.5", 7.5)):
        r = load(d, DENDRA_RELAY_TIMEOUT=raw)
        if r.Handler.timeout == want:
            ok("DENDRA_RELAY_TIMEOUT=%r -> %s" % (raw, r.Handler.timeout))
        else:
            ko("DENDRA_RELAY_TIMEOUT=%r -> %s (want %s)" % (raw, r.Handler.timeout, want))


# -- 3) A 409 IS A DELIVERY, NOT A FAILURE -------------------------------------------------------
#
# Every seal draws a fresh ephemeral key, so re-sealing the same content never reproduces the same
# bytes: any RETRY of a reveal deposit comes back 409. Read as a failure, that makes a worker report
# fewer deliveries than there are readable reveals and conclude a completed job failed.
def case_conflict_is_delivered():
    d = tempfile.mkdtemp()
    rs = load(d, DENDRA_RELAY_TIMEOUT="5", DENDRA_PUBLIC="0", DENDRA_RELAY_TOKEN="")
    rs._boot_store()

    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import relay_client
        importlib.reload(relay_client)

        st1 = relay_client.put_status(base, "reveal", "jobZ__jurorA", {"ct": "aaaa"})
        st2 = relay_client.put_status(base, "reveal", "jobZ__jurorA", {"ct": "bbbb"})  # re-seal: other bytes
        st3 = relay_client.put_status(base, "reveal", "jobZ__jurorA", {"ct": "aaaa"})  # identical bytes

        if st1 == "ok":
            ok("first deposit -> ok")
        else:
            ko("first deposit -> %r (want ok)" % st1)
        if st2 == "exists":
            ok("re-sealed deposit (different bytes) -> exists, NOT a refusal")
        else:
            ko("re-sealed deposit -> %r (want exists)" % st2)
        if st3 == "ok":
            ok("identical bytes -> ok, nothing destroyed")
        else:
            ko("identical bytes -> %r (want ok)" % st3)

        # And the boolean stays what its callers expect: True only on a fresh accepted deposit.
        if relay_client.put(base, "reveal", "jobZ__jurorB", {"ct": "cccc"}) is True:
            ok("put() still True on a fresh deposit")
        else:
            ko("put() no longer True on a fresh deposit")
        if relay_client.put(base, "reveal", "jobZ__jurorB", {"ct": "dddd"}) is False:
            ok("put() still False on a conflict (callers unchanged)")
        else:
            ko("put() changed meaning for its existing callers")
    finally:
        srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    print("== write-once under concurrency ==")
    case_write_once()
    print("== silent socket ==")
    case_silent_socket()
    print("== a conflict is a delivery ==")
    case_conflict_is_delivered()
    print("RELAY CONCURRENCY BENCH", "GREEN" if FAIL == 0 else "RED")
    sys.exit(FAIL)
