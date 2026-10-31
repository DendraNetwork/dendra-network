"""The owner's decisions R73 (the session in a cookie, and a confirmation code before the recovery words or a new
payout address) and R74 (Stop always first) in the desktop application, run against the shipped deploy/app (or
DENDRA_APP_DIR).

What is real: the HTTP server, the cookie it sets and reads, the confirmation code and the sinks that show it,
run_child with real processes (bash and sleep, one of them deaf to SIGTERM), the kit's file lock and the stop
request, taken and read by a second process. What is replaced: `docker compose`, by a function that runs a real
long process through the application's own run_child, and the miner container's readers.

The requests carry the cookie, the session header and the old X-Dendra-App header: against an application from
before R73 a case then fails for the decision it guards, not because the old application cannot read the cookie."""
import concurrent.futures
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time

from test_dendra_app import (APP, CODE_SHAPE, UI_INSTANCE, UI_SESSION, app, call, fresh_code, rig, run_ui,  # noqa: F401
                             session_cookie, session_headers)  # app and rig are fixtures

GOOD = "dendra1" + "q" * 38
DEAF = ["bash", "-c", "trap '' 15; sleep 30"]        # ignores SIGTERM (15), as a busy child can: only SIGKILL ends it
LONG = ["bash", "-c", "sleep 30"]


def request(A, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", A.APP.port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def both(A, **more):
    return dict({"Host": f"127.0.0.1:{A.APP.port}", "X-Dendra-App": A.APP.token}, **session_headers(A), **more)


def post(A, path, doc):
    status, _, raw = request(A, "POST", path, body=json.dumps(doc), headers=both(A, **{"Content-Type": "application/json"}))
    return status, (json.loads(raw) if raw[:1] == b"{" else raw)


def get(A, path, **more):
    status, _, raw = request(A, "GET", path, headers=both(A, **more))
    return status, (json.loads(raw) if raw[:1] == b"{" else raw)


def wait_for(cond, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def verb_of(args):
    """The compose verb of an argument list, after the global options slot 0 may carry (`--profile judge stop`)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] == "--profile" else 1
    return args[i] if i < len(args) else ""


def compose_running(A, seen, argv):
    """`docker compose` replaced: `up` runs `argv` through the application's run_child and answers as compose does;
    every other verb is recorded and succeeds at once."""
    def compose(*args, timeout=120, slot=0):
        seen.append(verb_of(args))
        if verb_of(args) == "up":
            rc, out, err, how = A.run_child(argv, 60)
            return (A.INTERRUPTED_RC, A.INTERRUPTED_TEXT) if how == "interrupted" else (rc, out + err)
        return 0, verb_of(args) + " done"
    return compose


# --- R73: the session ---
def test_the_session_is_the_cookie_and_the_old_header_is_no_way_in(app):
    A, _ = app
    host = f"127.0.0.1:{A.APP.port}"
    tab = {A.SESSION_HEADER: A.APP.tab_key}
    assert request(A, "GET", "/api/overview", headers={"Host": host, "X-Dendra-App": A.APP.token})[0] == 401
    assert request(A, "GET", "/api/overview", headers=dict({"Host": host}, **session_headers(A)))[0] == 200
    # among other cookies of 127.0.0.1, which a browser sends to every port
    other = "a=1; " + session_cookie(A) + "; b=2"
    assert request(A, "GET", "/api/overview", headers=dict({"Host": host, "Cookie": other}, **tab))[0] == 200
    # the cookie of another copy on another port is not this one's
    wrong_port = f"{A.SESSION_COOKIE}{A.APP.port + 1}={A.APP.token}"
    assert request(A, "GET", "/api/overview", headers=dict({"Host": host, "Cookie": wrong_port}, **tab))[0] == 401
    # a request the browser marks as coming from another site is refused, session or not
    for site in ("cross-site", "same-site", "none"):
        h = dict({"Host": host, "Sec-Fetch-Site": site}, **session_headers(A))
        assert request(A, "POST", "/api/start", body="{}", headers=h)[0] == 401, site
    h = dict({"Host": host, "Sec-Fetch-Site": "same-origin"}, **session_headers(A))
    assert request(A, "GET", "/api/overview", headers=h)[0] == 200


def test_the_cookie_alone_is_no_session(app, monkeypatch):
    # A browser sends the cookie to every port of 127.0.0.1: a program listening on another port receives it from a
    # page the browser loads there, and connects here with it. Without the header from the tab's sessionStorage,
    # which a page of another port cannot read, it is refused, and nothing runs or prints.
    A, _ = app
    calls, lines = [], []
    monkeypatch.setattr(A, "compose", lambda *a, **k: calls.append(a) or (0, ""))
    A.APP.code_sinks.append(lines.append)
    host = f"127.0.0.1:{A.APP.port}"
    cookie_only = {"Host": host, "Cookie": session_cookie(A), "Content-Type": "application/json"}
    for path, body in (("/api/stop", {"slot": "all"}), ("/api/code", {"for": "recovery"}), ("/api/start", {})):
        assert request(A, "POST", path, body=json.dumps(body), headers=cookie_only)[0] == 401, path
    assert request(A, "GET", "/api/overview", headers=cookie_only)[0] == 401
    # a wrong or empty header is no better, and the header without the cookie neither
    for value in ("", "x" * 43, A.APP.token, A.APP.instance):
        h = dict(cookie_only, **{A.SESSION_HEADER: value})
        assert request(A, "POST", "/api/stop", body="{}", headers=h)[0] == 401, value
    h = {"Host": host, "Content-Type": "application/json", A.SESSION_HEADER: A.APP.tab_key}
    assert request(A, "POST", "/api/stop", body="{}", headers=h)[0] == 401
    assert calls == [] and lines == []
    # both halves: in
    h = dict(cookie_only, **{A.SESSION_HEADER: A.APP.tab_key})
    assert request(A, "POST", "/api/stop", body="{}", headers=h)[0] == 200 and len(calls) == 1


def test_only_the_session_s_answers_carry_the_instance_value(app):
    # The page reads an answer only when it carries this value (ui/index.html, api): a refusal never shows it, so a
    # program that took the port after Dendra stopped cannot have read it here first.
    A, _ = app
    host = f"127.0.0.1:{A.APP.port}"
    status, headers, _ = request(A, "GET", "/api/overview", headers=dict({"Host": host}, **session_headers(A)))
    assert status == 200 and headers.get(A.INSTANCE_HEADER) == A.APP.instance
    status, headers, _ = request(A, "POST", "/api/code", body="{}", headers=both(A, **{"Content-Type": "application/json"}))
    assert status == 400 and headers.get(A.INSTANCE_HEADER) == A.APP.instance
    for h in ({"Host": host}, {"Host": host, "Cookie": session_cookie(A)}):
        status, headers, _ = request(A, "GET", "/api/overview", headers=h)
        assert status == 401 and A.INSTANCE_HEADER not in headers
    status, headers, _ = request(A, "GET", "/", headers={"Host": host})
    assert status == 200 and A.INSTANCE_HEADER not in headers
    assert A.APP.instance not in (A.APP.token, A.APP.tab_key)


def test_the_page_sends_the_tab_s_half_and_holds_no_token(app, tmp_path):
    A, _ = app
    page = open(os.path.join(APP, "ui", "index.html"), encoding="utf-8").read()
    for word in ("localStorage", "document.cookie", "X-Dendra-App", "location.hash", "sessionStorage.setItem"):
        assert word not in page, word
    view = {"installed": True, "containers": [], "docker_error": None, "identity": {}, "chain": {},
            "programme": {"enabled": False}, "recovery_confirmed": None}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}]})
    assert ui["heads"] and all(h["credentials"] == "same-origin" for h in ui["heads"]), ui["heads"]
    assert all("X-Dendra-App" not in h["headers"] for h in ui["heads"])
    assert all(h["headers"].get("X-Dendra-Session") == UI_SESSION for h in ui["heads"]), ui["heads"]
    assert ui["cleared"] == [] and ui["timers"] == 4


def test_the_page_stops_on_an_answer_that_is_not_its_dendra_s(app, tmp_path):
    # Dendra stopped and another program took its port (a fixed one under Windows): the open tab's next poll reached
    # it, and the page drew its answer as Dendra's own, a command to run or a recovery card to fill in included.
    A, _ = app
    fake = {"installed": True, "containers": [], "docker_error": None, "identity": {"miner_id": "dm1squatter"},
            "chain": {}, "programme": {"enabled": False}, "recovery_confirmed": False,
            "status": {"level": "red", "word": "Run this now", "rules": []}}
    for instance in (None, "another"):
        ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": fake, "instance": instance}],
                               "/api/start": [{"status": 200, "data": {"rc": 0, "text": ""}}]},
                    [["click", "startBtn"], ["click", "recoveryCodeBtn"]])
        assert ui["text"]["state"].startswith("Dendra stopped") and ui["cls"]["state"] == "pill bad", ui["text"]["state"]
        assert ui["text"]["mid"] != "dm1squatter" and ui["hidden"]["recovery"] is True
        assert sorted(ui["cleared"]) == [1, 2, 3, 4], ui["cleared"]
        paths = [c["path"] for c in ui["calls"]]
        assert "/api/start" not in paths and "/api/code" not in paths, paths
    # a refused session ends the tab the same way, with its own words
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 401, "data": {"error": "token"}, "instance": None}]})
    assert ui["text"]["state"].startswith("no access: open Dendra from its launcher again") and sorted(ui["cleared"]) == [1, 2, 3, 4]
    # a tab /handoff never reached holds no session to send
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 401, "data": {}, "instance": None}]}, storage={})
    assert all(h["headers"].get("X-Dendra-Session") == "" for h in ui["heads"]) and sorted(ui["cleared"]) == [1, 2, 3, 4]


def test_the_native_window_gets_in_by_the_handoff_and_never_by_a_url(tmp_path, monkeypatch, capsys):
    # The window used to load http://127.0.0.1:P/#t=<session token>, a URL its history keeps. It now loads the
    # access page in-process, with a one-time token that /handoff spends.
    from test_dendra_app import handoff, launched
    import re
    import urllib.parse
    A, run, handed = launched(tmp_path, monkeypatch)
    seen = {}

    def window(base, page):
        seen.update(base=base, page=page, token=A.APP.token)
        once = re.search('name="t" value="([A-Za-z0-9_-]{43})"', page)
        seen["handoff"] = handoff(A, urllib.parse.urlencode({"t": once.group(1) if once else ""}).encode())
        seen["again"] = handoff(A, urllib.parse.urlencode({"t": once.group(1) if once else ""}).encode())[0]
        return True
    monkeypatch.setattr(A, "open_window", window)
    assert A.main([]) == 0
    assert seen["base"] == f"http://127.0.0.1:{A.APP.port}/" and "#" not in seen["base"]
    assert seen["token"] not in seen["page"] and "/handoff" in seen["page"]
    code, body, headers = seen["handoff"]
    assert code == 200 and seen["again"] == 401 and headers["Set-Cookie"].startswith(A.SESSION_COOKIE)
    assert handed == [] and not (run / "dendra").exists() and seen["token"] not in capsys.readouterr().out


# --- R73: the confirmation code ---
def test_the_recovery_words_need_a_code_printed_outside_the_page(app, monkeypatch):
    A, _ = app
    read = []
    monkeypatch.setattr(A, "recovery_phrase", lambda: read.append(1) or {"present": True, "address": "dendra1x",
                                                                         "words": ["sample"] * 24})
    status, body = get(A, "/api/recovery")
    assert status == 403 and body["code"] == "required" and read == []
    code = fresh_code(A, "recovery")
    status, body = get(A, "/api/recovery", **{A.CODE_HEADER: code.lower()})
    assert status == 200 and body["words"] == ["sample"] * 24 and read == [1]
    # spent by its first use
    status, body = get(A, "/api/recovery", **{A.CODE_HEADER: code})
    assert status == 403 and body["code"] == "refused" and read == [1]
    # a wrong try costs one of CODE_TRIES, and the code goes at the last: a code is guessed at most CODE_TRIES times
    code = fresh_code(A, "recovery")
    for _ in range(A.CODE_TRIES):
        assert get(A, "/api/recovery", **{A.CODE_HEADER: "AAAA-AAAA"})[0] == 403
    assert get(A, "/api/recovery", **{A.CODE_HEADER: code})[0] == 403 and read == [1]
    # a code asked for the payout does not open the words
    code = fresh_code(A, "payout", address=GOOD)
    assert get(A, "/api/recovery", **{A.CODE_HEADER: code})[0] == 403
    # nor does a code past its few minutes
    monkeypatch.setattr(A, "CODE_SECONDS", 0)
    code = fresh_code(A, "recovery")
    status, body = get(A, "/api/recovery", **{A.CODE_HEADER: code})
    assert status == 403 and read == [1]
    # no refusal repeats the code or the session token
    assert code.replace("-", "") not in json.dumps(body) and code not in json.dumps(body)
    assert A.APP.token not in json.dumps(body)


def test_the_payout_address_needs_a_code_and_a_bad_address_does_not_spend_it(app, monkeypatch):
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + chr(10))
    ran = []
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1self"})
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, **kw: ran.append(cmd) or (0, "declared"))
    status, body = post(A, "/api/payout", {"address": GOOD})
    assert status == 403 and body["code"] == "required" and ran == []
    code = fresh_code(A, "payout", address=GOOD)
    assert post(A, "/api/payout", {"address": "dendra1;rm", "code": code})[0] == 400
    status, body = post(A, "/api/payout", {"address": GOOD, "code": code})
    assert status == 200 and len(ran) == 1, body
    status, body = post(A, "/api/payout", {"address": GOOD, "code": code})
    assert status == 403 and len(ran) == 1 and code not in json.dumps(body)
    # a code asked for the words does not change the address
    code = fresh_code(A, "recovery")
    assert post(A, "/api/payout", {"address": GOOD, "code": code})[0] == 403 and len(ran) == 1


def test_a_code_reaches_the_terminal_and_never_the_page(app, capsys):
    A, _ = app
    status, body = post(A, "/api/code", {"for": "recovery"})
    out = capsys.readouterr().out
    assert status == 200 and body["sent"] is True and "terminal or window" in body["where"]
    line = [ln for ln in out.splitlines() if "confirmation code" in ln]
    assert len(line) == 1 and A.APP.token not in out
    code = line[0].split(": ")[1].split(" ")[0]
    assert len(code) == 9 and code not in json.dumps(body) and code.replace("-", "") not in json.dumps(body)
    assert all(c in A.CODE_ALPHABET for c in code.replace("-", ""))
    assert post(A, "/api/code", {"for": "keys"})[0] == 400


def test_the_page_asks_a_code_and_never_fetches_the_words_by_itself(app, tmp_path):
    A, _ = app
    view = {"installed": True, "containers": [], "docker_error": None, "identity": {}, "chain": {},
            "programme": {"enabled": False}, "recovery_confirmed": False}
    words = {"present": True, "address": "dendra1aaa", "words": ["w" + str(i) for i in range(24)], "ask": [1, 2, 3]}
    sent = {"status": 200, "data": {"sent": True, "seconds": 300, "where": "the terminal or window that started Dendra"}}
    replies = {"/api/overview": [{"status": 200, "data": view}], "/api/code": [sent],
               "/api/recovery": [{"status": 200, "data": words}],
               "/api/payout": [{"status": 200, "data": {"rc": 0, "text": ""}}]}
    ui = run_ui(tmp_path, replies)
    assert ui["hidden"]["recovery"] is False and ui["hidden"]["recoveryGate"] is False
    assert ui["hidden"]["recoveryWords"] is True and ui["words"] == []
    assert "/api/recovery" not in [c["path"] for c in ui["calls"]]
    ui = run_ui(tmp_path, replies, [["click", "recoveryCodeBtn"], ["fill", "recoveryCode", "abcd-efgh"],
                                    ["click", "showWordsBtn"], ["fill", "payout", GOOD], ["fill", "payoutCode", "WXYZ-2345"],
                                    ["click", "payoutBtn"]])
    assert {"path": "/api/code", "body": {"for": "recovery"}} in ui["bodies"]
    asked = [h for h in ui["heads"] if h["path"] == "/api/recovery"]
    assert len(asked) == 1 and asked[0]["headers"].get("X-Dendra-Code") == "abcd-efgh"
    assert len(ui["words"]) == 24 and ui["hidden"]["recoveryWords"] is False and ui["hidden"]["recoveryGate"] is True
    assert {"path": "/api/payout", "body": {"address": GOOD, "code": "WXYZ-2345"}} in ui["bodies"]
    # the payout's code is asked for the address typed, before anything is saved
    ui = run_ui(tmp_path, replies, [["fill", "payout", GOOD], ["click", "payoutCodeBtn"]])
    assert {"path": "/api/code", "body": {"for": "payout", "address": GOOD}} in ui["bodies"]


OTHER_ADDRESS = "dendra1" + "z" * 38


def code_in(line):
    m = CODE_SHAPE.search(line)
    return m.group(1) + "-" + m.group(2)


def test_a_payout_code_confirms_one_address_and_the_terminal_names_it(app, monkeypatch):
    # The code proved that the owner wanted some payout change at that moment. A script inside the page (an
    # extension, say) could wait for the owner's code and send it with its own address: the code is now given for
    # one address, which the line beside it names, and refused for any other.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + chr(10))
    ran, lines = [], []
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1self"})
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, **kw: ran.append(cmd) or (0, "declared"))
    A.APP.code_sinks.append(lines.append)
    status, body = post(A, "/api/code", {"for": "payout", "address": GOOD})
    assert status == 200 and len(lines) == 1 and f"changing the payout address to {GOOD}: " in lines[0], lines
    code = code_in(lines[0])
    status, body = post(A, "/api/payout", {"address": OTHER_ADDRESS, "code": code})
    assert status == 403 and body["code"] == "refused" and "another address" in body["error"] and ran == [], body
    # the refused try cost one of the code's tries, not the code: the owner's own change still goes with it
    status, body = post(A, "/api/payout", {"address": GOOD, "code": code})
    assert status == 200 and len(ran) == 1 and ran[0][-1] == GOOD, body
    # no code is printed for a change the route would refuse
    for doc in ({"for": "payout"}, {"for": "payout", "address": "dendra1;rm"}, {"for": "payout", "address": GOOD, "slot": 9}):
        assert post(A, "/api/code", doc)[0] == 400, doc
    assert len(lines) == 1


def test_a_code_confirms_one_identity_or_every_one_never_another(rig, monkeypatch):
    A, kit, log = rig
    ran, read, lines = [], [], []
    monkeypatch.setattr(A, "miner_identity", lambda slot=0: {"miner_id": f"dm1slot{slot}", "address": f"dendra1m{slot}"})
    monkeypatch.setattr(A, "miner_exec", lambda *a, slot=0, **k: ran.append(slot) or (0, "ok"))
    monkeypatch.setattr(A, "recovery_phrase", lambda slot=0: read.append(slot) or {
        "present": True, "address": f"dendra1m{slot}", "words": ["sample"] * 24})
    A.APP.code_sinks.append(lines.append)
    # a code for slot 1 names slot 1, and changes neither every identity nor another one
    assert post(A, "/api/code", {"for": "payout", "address": GOOD, "slot": 1})[0] == 200
    assert f"changing the payout address of slot 1 to {GOOD}: " in lines[-1], lines
    code = code_in(lines[-1])
    for slot in ("all", 0):
        status, body = post(A, "/api/payout", {"address": GOOD, "slot": slot, "code": code})
        assert status == 403 and body["code"] == "refused" and ran == [], (slot, body)
    assert post(A, "/api/payout", {"address": GOOD, "slot": 1, "code": code})[0] == 200 and ran == [1]
    # a code for every identity names them all, and is no code for one of them
    assert post(A, "/api/code", {"for": "payout", "address": GOOD, "slot": "all"})[0] == 200
    assert f"changing the payout address of every identity (slots 0, 1) to {GOOD}: " in lines[-1], lines
    code = code_in(lines[-1])
    assert post(A, "/api/payout", {"address": GOOD, "slot": 1, "code": code})[0] == 403 and ran == [1]
    assert post(A, "/api/payout", {"address": GOOD, "slot": "all", "code": code})[0] == 200 and ran == [1, 0, 1]
    # the words of slot 0 do not open slot 1's, and a code for the words takes no `all`
    assert post(A, "/api/code", {"for": "recovery", "slot": "all"})[0] == 400
    assert post(A, "/api/code", {"for": "recovery", "slot": 0})[0] == 200
    assert "showing the 24 recovery words of slot 0: " in lines[-1], lines
    code = code_in(lines[-1])
    assert get(A, "/api/recovery?slot=1", **{A.CODE_HEADER: code})[0] == 403 and read == []
    assert get(A, "/api/recovery?slot=0", **{A.CODE_HEADER: code})[0] == 200 and read == [0]


def test_the_code_gate_refuses_and_runs_nothing(app, monkeypatch):
    # The refusal half of the gate, each case through the HTTP server, with no docker call and no read of the words.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + chr(10))
    ran, read = [], []
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1self"})
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, **kw: ran.append(cmd) or (0, "declared"))
    monkeypatch.setattr(A, "compose", lambda *a, **k: ran.append(a) or (0, ""))
    monkeypatch.setattr(A, "recovery_phrase", lambda: read.append(1) or {"present": True, "address": "dendra1x",
                                                                         "words": ["sample"] * 24})
    # none sent
    assert get(A, "/api/recovery")[1]["code"] == "required"
    assert post(A, "/api/payout", {"address": GOOD})[1]["code"] == "required"
    # none asked: a code typed from nowhere
    assert get(A, "/api/recovery", **{A.CODE_HEADER: "ABCD-EFGH"})[1]["code"] == "refused"
    # wrong: refused, and at the CODE_TRIES-th the code is gone, the right one included
    words = fresh_code(A, "recovery")
    for _ in range(A.CODE_TRIES - 1):
        assert get(A, "/api/recovery", **{A.CODE_HEADER: "ABCD-EFGH"})[0] == 403
    assert get(A, "/api/recovery", **{A.CODE_HEADER: words})[0] == 200 and read == [1]
    read.clear()
    words = fresh_code(A, "recovery")
    for _ in range(A.CODE_TRIES):
        assert get(A, "/api/recovery", **{A.CODE_HEADER: "ABCD-EFGH"})[0] == 403
    assert get(A, "/api/recovery", **{A.CODE_HEADER: words})[0] == 403
    # a try for the other purpose, however many, never touches the owner's code; nor does asking the other code
    words = fresh_code(A, "recovery")
    for _ in range(A.CODE_TRIES + 2):
        assert post(A, "/api/payout", {"address": GOOD, "code": "ABCD-EFGH"})[0] == 403
    assert post(A, "/api/payout", {"address": GOOD, "code": words})[0] == 403
    payout = fresh_code(A, "payout", address=GOOD)
    assert get(A, "/api/recovery", **{A.CODE_HEADER: words})[0] == 200 and read == [1]
    # replayed
    assert get(A, "/api/recovery", **{A.CODE_HEADER: words})[1]["code"] == "refused"
    # expired
    monkeypatch.setattr(A, "CODE_SECONDS", 0)
    words = fresh_code(A, "recovery")
    assert get(A, "/api/recovery", **{A.CODE_HEADER: words})[1]["code"] == "refused"
    assert ran == [] and read == [1]
    # the payout code still waits for its own change
    monkeypatch.setattr(A, "CODE_SECONDS", 300)
    assert post(A, "/api/payout", {"address": GOOD, "code": payout})[0] == 200 and len(ran) == 1


def test_a_launch_with_no_terminal_and_no_window_shows_no_code_and_says_how(tmp_path, monkeypatch, capsys):
    # Started from the applications menu (install_app.sh writes Terminal=false) without the native window, the
    # code went to the session's log, and the page sent the owner to a terminal that does not exist.
    from test_dendra_app import launched
    A, run, handed = launched(tmp_path, monkeypatch)
    seen = {}

    def while_running(_seconds):
        lines = []
        A.APP.code_sinks.append(lines.append)
        seen["menu"] = post(A, "/api/code", {"for": "recovery"})
        seen["lines"] = lines
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main([]) == 0 and len(handed) == 1
    status, body = seen["menu"]
    assert status == 409 and "start Dendra from a terminal by typing dendra" in body["error"], body
    assert seen["lines"] == [] and "confirmation code for" not in capsys.readouterr().out
    # the page shows 300 characters of it (ui/index.html, askCode): the sentence ends on the page as it is written
    assert len("no code: " + body["error"]) <= 300 and body["error"].endswith("for its own window"), len(body["error"])

    class Tty(io.StringIO):
        def isatty(self):
            return True

    # the same launch from a terminal prints the code there
    again = tmp_path / "again"
    again.mkdir()
    A, run, handed = launched(again, monkeypatch)
    tty = Tty()
    monkeypatch.setattr(A.sys, "stdout", tty)

    def from_terminal(_seconds):
        seen["terminal"] = post(A, "/api/code", {"for": "recovery"})
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", from_terminal)
    assert A.main([]) == 0
    assert seen["terminal"][0] == 200 and "confirmation code for showing the 24 recovery words: " in tty.getvalue()


def test_the_page_shows_the_no_terminal_sentence_whole(app, tmp_path):
    # askCode cuts an error at 300 characters: the first sentence ran past it and ended mid-word on the page.
    A, _ = app
    view = {"installed": True, "containers": [], "docker_error": None, "identity": {}, "chain": {},
            "programme": {"enabled": False}, "recovery_confirmed": None}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}],
                           "/api/code": [{"status": 409, "data": {"error": A.NO_CODE_SINK}}]},
                [["click", "recoveryCodeBtn"]])
    assert ui["text"]["recoveryMsg"] == "no code: " + A.NO_CODE_SINK, ui["text"]["recoveryMsg"]


# --- R74: Stop always first ---
def test_stop_interrupts_the_action_in_progress_then_stops_the_miner(app, monkeypatch):
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "STOP_GRACE_S", 0.5)
    monkeypatch.setattr(A, "compose", compose_running(A, seen, DEAF))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(post, A, "/api/start", {})
        assert wait_for(lambda: bool(A.APP.children)), "the start never ran its subprocess"
        pgid = next(iter(A.APP.children))
        t0 = time.monotonic()
        status, body = post(A, "/api/stop", {})
        took = time.monotonic() - t0
        started = start.result(timeout=10)
    assert status == 200 and body["rc"] == 0, body
    assert (body["interrupted"], body["interrupted_where"], body["beside"]) == ("Start", "here", ""), body
    # SIGTERM was ignored, so SIGKILL after the grace ended it: not the 30 s of the sleep
    assert took < 8, took
    assert started[0] == 200 and started[1]["rc"] == A.INTERRUPTED_RC and "interrupted by Stop" in started[1]["text"]
    assert gone(pgid) and seen == ["up", "stop"]
    # the next action runs as usual: the interruption ended with the action it was asked of
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["true"]))
    assert post(A, "/api/start", {})[1]["rc"] == 0


def test_while_stop_waits_no_other_action_starts(app, monkeypatch):
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "STOP_GRACE_S", 2)
    monkeypatch.setattr(A, "compose", compose_running(A, seen, DEAF))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(post, A, "/api/start", {})
        assert wait_for(lambda: bool(A.APP.children))
        stop = pool.submit(post, A, "/api/stop", {})
        assert wait_for(lambda: A.APP.stop_pending > 0)
        status, body = post(A, "/api/restart", {})
        assert status == 409 and body["running"] == "/api/stop", body
        assert stop.result(timeout=15)[0] == 200 and start.result(timeout=15)[1]["rc"] == A.INTERRUPTED_RC
    assert seen == ["up", "stop"]


def test_stop_does_not_wait_for_long_behind_an_action_it_cannot_end(app, monkeypatch):
    # A step that is not a subprocess of the action (a read, here a wait on an event) cannot be interrupted:
    # Stop waits STOP_WAIT_S for it, then stops the miner beside it and says so.
    A, _ = app
    seen, entered, release = [], threading.Event(), threading.Event()
    monkeypatch.setattr(A, "STOP_WAIT_S", 0.5)

    def compose(*args, timeout=120, slot=0):
        seen.append(verb_of(args))
        if verb_of(args) == "up":
            entered.set()
            release.wait(10)
        return 0, ""
    monkeypatch.setattr(A, "compose", compose)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(post, A, "/api/start", {})
        assert entered.wait(5)
        try:
            status, body = post(A, "/api/stop", {})
        finally:
            release.set()
        start.result(timeout=10)
    assert status == 200 and body["interrupted"] == "Start" and "stopped beside it" in body["beside"], body
    assert seen == ["up", "stop"]


def test_stop_ends_the_action_of_another_copy_on_the_kit(app, monkeypatch):
    # The other copy holds the kit's lock as kit_lock_try writes it, and ends its action when the stop request
    # names it (the reading run_child makes, stop_asked_of_me). This copy's Stop asks, waits, then stops.
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["true"]))
    # the action interrupted is a switch: its model pull is looked for too (test_dendra_app.py covers that part)
    monkeypatch.setattr(A, "remove_judge_pull", lambda: seen.append("remove_judge_pull") or (0, ""))
    lock, request_file = A.action_lock_path(), A.stop_request_path()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, json, os, sys, time" + chr(10) +
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)" + chr(10) +
        "fcntl.flock(fd, fcntl.LOCK_EX)" + chr(10) +
        "os.ftruncate(fd, 0)" + chr(10) +
        "os.write(fd, json.dumps({'path': '/api/judge', 'since': int(time.time()) - 60, 'pid': os.getpid()}).encode())" + chr(10) +
        "print('held', flush=True)" + chr(10) +
        "end = time.time() + 20" + chr(10) +
        "while time.time() < end:" + chr(10) +
        "    try:" + chr(10) +
        "        doc = json.load(open(sys.argv[2]))" + chr(10) +
        "    except (OSError, ValueError):" + chr(10) +
        "        doc = {}" + chr(10) +
        "    if doc.get('pid') == os.getpid():" + chr(10) +
        "        print('asked', flush=True)" + chr(10) +
        "        break" + chr(10) +
        "    time.sleep(0.05)" + chr(10)), lock, request_file], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        # any other action is still refused while the other copy holds the kit
        assert post(A, "/api/start", {})[0] == 409
        status, body = post(A, "/api/stop", {})
        said = holder.stdout.readline().strip()
    finally:
        holder.wait(10)
    assert said == "asked"
    assert status == 200 and body["rc"] == 0, body
    assert (body["interrupted"], body["interrupted_where"], body["beside"]) == ("the Judge switch", "another copy", ""), body
    assert seen == ["stop", "remove_judge_pull"] and not os.path.exists(request_file)


def test_stop_keeps_asking_another_copy_that_had_not_written_its_pid_yet(app, monkeypatch):
    # The other copy takes the kit's lock, then writes which action it runs and its pid (kit_lock_try). A Stop that
    # looked in between read no pid, and never asked again: the other copy ran to its end beside Stop.
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "STOP_WAIT_S", 4)
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["true"]))
    lock, request_file = A.action_lock_path(), A.stop_request_path()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, json, os, sys, time" + chr(10) +
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)" + chr(10) +
        "fcntl.flock(fd, fcntl.LOCK_EX)" + chr(10) +
        "os.ftruncate(fd, 0)" + chr(10) +
        "print('held', flush=True)" + chr(10) +
        "sys.stdin.readline()" + chr(10) +
        "os.write(fd, json.dumps({'path': '/api/start', 'since': int(time.time()), 'pid': os.getpid()}).encode())" + chr(10) +
        "end = time.time() + 20" + chr(10) +
        "while time.time() < end:" + chr(10) +
        "    try:" + chr(10) +
        "        doc = json.load(open(sys.argv[2]))" + chr(10) +
        "    except (OSError, ValueError):" + chr(10) +
        "        doc = {}" + chr(10) +
        "    if doc.get('pid') == os.getpid():" + chr(10) +
        "        print('asked', flush=True)" + chr(10) +
        "        break" + chr(10) +
        "    time.sleep(0.05)" + chr(10)), lock, request_file],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            stop = pool.submit(post, A, "/api/stop", {})
            # Stop's first look finds the lock held and no pid in it
            assert wait_for(lambda: A.APP.stop_pending > 0 and A.APP.action_running is not None)
            time.sleep(0.5)
            holder.stdin.write("go" + chr(10))
            holder.stdin.flush()
            said = holder.stdout.readline().strip()
            status, body = stop.result(timeout=15)
    finally:
        holder.wait(10)
    assert said == "asked"
    assert status == 200 and body["beside"] == "" and body["interrupted"] == "Start", body
    assert seen == ["stop"]


def test_a_stop_request_written_as_the_action_takes_the_kit_counts_for_it(app, monkeypatch):
    # Another copy's Stop that finds the kit's lock held writes its request at once. The action used to date itself
    # after it had taken the lock, so a request written in between was read as older than it and ignored.
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["bash", "-c", "sleep 5"]))
    real_lock = A.kit_action_lock

    def kit_action_lock(path, ks):
        got = real_lock(path, ks)
        assert A.ask_other_copy_to_stop(os.getpid()) is True        # the other copy's Stop, just after the flock
        time.sleep(0.05)
        return got
    monkeypatch.setattr(A, "kit_action_lock", kit_action_lock)
    try:
        t0 = time.monotonic()
        status, body = post(A, "/api/start", {})
        took = time.monotonic() - t0
    finally:
        A.forget_stop_request()
    assert status == 200 and body["rc"] == A.INTERRUPTED_RC and took < 4, (body, took)


def test_after_a_stop_beside_a_stuck_action_no_action_starts_until_stop_has_stopped(app, monkeypatch):
    # Stop waited STOP_WAIT_S for an action it cannot end (a read), then stopped the miner beside it. Once that action
    # gave its lock back, a Start got in and ran `compose up` while Stop's `compose stop` was still running.
    A, _ = app
    seen, entered, release, stopping, stop_release = [], threading.Event(), threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(A, "STOP_WAIT_S", 0.5)

    def compose(*args, timeout=120, slot=0):
        seen.append(verb_of(args))
        if verb_of(args) == "up" and not entered.is_set():
            entered.set()
            release.wait(10)
        if verb_of(args) == "stop":
            stopping.set()
            stop_release.wait(10)
        return 0, ""
    monkeypatch.setattr(A, "compose", compose)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(post, A, "/api/start", {})
        assert entered.wait(5)
        stop = pool.submit(post, A, "/api/stop", {})
        try:
            assert stopping.wait(5), "Stop never ran its compose stop"
            release.set()
            start.result(timeout=10)
            status, body = post(A, "/api/start", {})
        finally:
            release.set()
            stop_release.set()
        stopped = stop.result(timeout=10)
    assert status == 409 and body["running"] == "/api/stop", body
    assert stopped[0] == 200 and "stopped beside it" in stopped[1]["beside"], stopped
    assert seen == ["up", "stop"]
    # once Stop has answered, actions run again
    assert post(A, "/api/start", {})[0] == 200 and seen == ["up", "stop", "up"]


def test_a_stop_request_from_another_copy_interrupts_this_copy_s_subprocess(app, monkeypatch):
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, LONG))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(post, A, "/api/start", {})
        assert wait_for(lambda: bool(A.APP.children))
        pgid = next(iter(A.APP.children))
        t0 = time.monotonic()
        assert A.ask_other_copy_to_stop(os.getpid()) is True
        status, body = start.result(timeout=15)
        took = time.monotonic() - t0
    assert status == 200 and body["rc"] == A.INTERRUPTED_RC and took < 5 and gone(pgid), (body, took)
    A.forget_stop_request()


def test_a_request_older_than_the_action_does_not_interrupt_it(app, monkeypatch):
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["bash", "-c", "sleep 0.5"]))
    assert A.ask_other_copy_to_stop(os.getpid()) is True
    time.sleep(0.05)
    status, body = post(A, "/api/start", {})
    assert status == 200 and body["rc"] == 0, body
    A.forget_stop_request()


def test_the_page_says_what_stop_interrupted(app, tmp_path):
    A, _ = app
    view = {"installed": True, "containers": [], "docker_error": None, "identity": {}, "chain": {},
            "programme": {"enabled": False}, "recovery_confirmed": None}
    for where, said in (("here", "Interrupted Start, then stopped the miner."),
                        ("another copy", "Interrupted Start (in another copy of Dendra on this machine), then stopped the miner.")):
        stop = {"status": 200, "data": {"rc": 0, "text": "", "interrupted": "Start", "interrupted_where": where, "beside": ""}}
        ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}], "/api/stop": [stop]}, [["click", "stopBtn"]])
        assert ui["text"]["minerMsg"] == said, ui["text"]["minerMsg"]
    plain = {"status": 200, "data": {"rc": 0, "text": "", "interrupted": None, "interrupted_where": None, "beside": ""}}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}], "/api/stop": [plain]}, [["click", "stopBtn"]])
    assert ui["text"]["minerMsg"] == "done"


# --- the answer, the locks, and Stop's edges ---
def test_an_action_s_answer_leaves_after_its_locks_are_released(app, monkeypatch):
    # A client that reads the answer and calls again at once must find the action ended. Released after the
    # answer was written, the locks still held made the next call a 409 under load.
    A, _ = app
    seen, locked_when_answered = [], []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["true"]))
    real = A.Handler.end_headers

    def end_headers(self):
        locked_when_answered.append(A.APP.action_lock.locked())
        return real(self)
    monkeypatch.setattr(A.Handler, "end_headers", end_headers)
    for path in ("/api/start", "/api/restart", "/api/stop"):
        assert post(A, path, {})[0] == 200, path
    assert locked_when_answered == [False, False, False]


def test_a_stop_that_arrives_between_the_two_locks_is_not_lost(app, monkeypatch):
    # Stop asks the action that holds the process's lock to end; an action that had that lock but had not started
    # yet used to clear the request as it started, and ran to its end while Stop waited beside it.
    A, _ = app
    seen, stops = [], []
    monkeypatch.setattr(A, "STOP_WAIT_S", 2)
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["bash", "-c", "sleep 4"]))
    real_lock = A.kit_action_lock
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def kit_action_lock(path, ks):
        if path == "/api/start":
            stops.append(pool.submit(post, A, "/api/stop", {}))
            assert wait_for(lambda: A.APP.stop_asked), "the Stop never asked"
        return real_lock(path, ks)
    monkeypatch.setattr(A, "kit_action_lock", kit_action_lock)
    try:
        status, body = post(A, "/api/start", {})
        stop_status, stop_body = stops[0].result(timeout=15)
    finally:
        pool.shutdown()
    assert status == 409 and body["running"] == "/api/stop", body
    assert stop_status == 200 and stop_body["beside"] == "" and stop_body["interrupted"] is None, stop_body
    assert seen == ["stop"]


def test_a_stop_that_names_no_identity_interrupts_nothing(app, monkeypatch):
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["bash", "-c", "sleep 2"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(post, A, "/api/start", {})
        assert wait_for(lambda: bool(A.APP.children))
        status, body = post(A, "/api/stop", {"slot": 9})
        started = start.result(timeout=15)
    assert status == 400 and "slot 9" in body["error"], body
    assert started[0] == 200 and started[1]["rc"] == 0, started
    assert seen == ["up"]


def test_a_compose_that_times_out_names_its_command(app, tmp_path, monkeypatch):
    # Slot 0's Stop puts --profile judge first, and the message named "--profile" instead of the command.
    A, _ = app
    b = tmp_path / "slowbin"
    b.mkdir()
    (b / "docker").write_text("#!/usr/bin/env bash" + chr(10) + "sleep 30" + chr(10))
    os.chmod(b / "docker", 0o755)
    monkeypatch.setenv("PATH", str(b) + os.pathsep + os.environ.get("PATH", ""))
    said = A.compose("--profile", "judge", "stop", timeout=0.5)
    assert said == (1, "TimeoutExpired: docker compose stop did not end within 0.5 s"), said


# --- R74 on a machine that runs several identities ---
# The page's Stop names the identity the page shows, which the owner may change while an action runs on another one.
# What is replaced: docker and slots.sh, by scripts that log each command line (`compose up`, and a slot's `up` or
# `exec`, wait FAKE_HANG seconds), and the list of identities. What is real: the routes, the locks, run_child and
# compose, which builds each command line as shipped.
MULTI_DOCKER = chr(10).join([
    "#!/usr/bin/env bash",
    'echo "docker $*" >> "$FAKE_LOG"',
    'case "$*" in',
    '  "compose up "*) [ -z "${FAKE_HANG:-}" ] || sleep "$FAKE_HANG" ;;',
    "esac",
    "exit 0", ""])
MULTI_SLOTS = chr(10).join([
    "#!/usr/bin/env bash",
    'echo "slots.sh $*" >> "$FAKE_LOG"',
    'case "$*" in',
    '  "run "*" up "*|"run "*" exec "*) [ -z "${FAKE_HANG:-}" ] || sleep "$FAKE_HANG" ;;',
    "esac",
    "exit 0", ""])


def two_identities(app, tmp_path, monkeypatch):
    """(A, log): the application over a kit that runs two identities, slot 0 (the kit itself) and slot 1."""
    A, kit = app
    b = tmp_path / "multibin"
    b.mkdir()
    (b / "docker").write_text(MULTI_DOCKER)
    os.chmod(b / "docker", 0o755)
    lib = tmp_path / "fake_slots.sh"
    lib.write_text(MULTI_SLOTS)
    log = tmp_path / "multi.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_HANG", "30")
    monkeypatch.setenv("PATH", str(b) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(A, "SLOTS_LIB", str(lib))
    monkeypatch.setattr(A, "STOP_GRACE_S", 0.5)
    (kit / ".env").write_text("MINER_ID=dm1slot0" + chr(10))
    one = kit / "gpu" / "1"
    one.mkdir(parents=True)
    (one / ".env").write_text("MINER_ID=dm1slot1" + chr(10))
    rows = [{"slot": 0, "project": "dendra-miner", "state": "active", "uuid": "", "miner_id": "dm1slot0", "dir": str(kit)},
            {"slot": 1, "project": "dendra-miner-g1", "state": "active", "uuid": "", "miner_id": "dm1slot1", "dir": str(one)}]
    monkeypatch.setattr(A, "slots", lambda: ([dict(r) for r in rows], None))
    return A, log


def logged(log):
    return log.read_text().splitlines() if log.exists() else []


def stopped_during(A, log, path, body, marker, stop_body):
    """(Stop's answer, the command lines logged from Stop on): the action `path` runs until its command line holding
    `marker` is logged, then Stop is posted with `stop_body`."""
    first = len(logged(log))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        act = pool.submit(post, A, path, body)
        assert wait_for(lambda: bool(A.APP.children) and any(marker in ln for ln in logged(log)[first:])), logged(log)
        before = len(logged(log))
        status, stop = post(A, "/api/stop", stop_body)
        act.result(timeout=30)
    assert status == 200 and stop["rc"] == 0, stop
    return stop, logged(log)[before:]


def test_stop_on_slot_1_stops_slot_0_that_the_gpu_switch_was_turning_on(app, tmp_path, monkeypatch):
    # GPU on acts on slot 0's kit, and is the switch a machine with one identity per card offers. Stop named the slot
    # the page showed by then, slot 1, and slot 0's card engine went on starting after Stop said it had stopped.
    A, log = two_identities(app, tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_switch_preflight", lambda which, on: (None, {}))
    stop, ran = stopped_during(A, log, "/api/gpu", {"on": True, "slot": 0}, "docker compose up -d --no-deps ollama",
                               {"slot": 1})
    assert stop["interrupted"] == "the GPU mining switch" and [r["slot"] for r in stop["results"]] == [0, 1], stop
    assert "docker compose --profile judge stop" in ran and "slots.sh run 1 stop" in ran, ran
    # the switch's model pull is looked for on slot 0, as after any switch
    assert any(ln.startswith("docker ps -q") and "oneoff=True" in ln for ln in ran), ran


def test_stop_on_slot_0_stops_slot_1_that_start_was_starting(app, tmp_path, monkeypatch):
    A, log = two_identities(app, tmp_path, monkeypatch)
    stop, ran = stopped_during(A, log, "/api/start", {"slot": 1}, "slots.sh run 1 up", {"slot": 0})
    assert stop["interrupted"] == "Start" and [r["slot"] for r in stop["results"]] == [0, 1], stop
    assert ran == ["docker compose --profile judge stop", "slots.sh run 1 stop"], ran
    # a Stop that names the identity the action acts on stops it once
    stop, ran = stopped_during(A, log, "/api/start", {"slot": 1}, "slots.sh run 1 up", {"slot": 1})
    assert [r["slot"] for r in stop["results"]] == [1] and ran == ["slots.sh run 1 stop"], (stop, ran)


def test_stop_on_slot_0_stops_slot_1_that_test_my_machine_was_loading(app, tmp_path, monkeypatch):
    # Killing the exec client leaves the test's script running in slot 1's container until its own limit: only
    # stopping slot 1 ends it.
    A, log = two_identities(app, tmp_path, monkeypatch)
    stop, ran = stopped_during(A, log, "/api/machine-test", {"slot": 1}, "slots.sh run 1 exec", {"slot": 0})
    assert stop["interrupted"] == "Test my machine" and [r["slot"] for r in stop["results"]] == [0, 1], stop
    assert "slots.sh run 1 stop" in ran, ran


def test_stop_stops_the_identity_another_copy_s_action_acts_on(app, tmp_path, monkeypatch):
    # The other copy writes the identities of its action into the kit's lock (kit_lock_try): this copy's Stop
    # reads them there, since the request of its own page names another one.
    A, log = two_identities(app, tmp_path, monkeypatch)
    lock, request_file = A.action_lock_path(), A.stop_request_path()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, json, os, sys, time" + chr(10) +
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)" + chr(10) +
        "fcntl.flock(fd, fcntl.LOCK_EX)" + chr(10) +
        "os.ftruncate(fd, 0)" + chr(10) +
        "os.write(fd, json.dumps({'path': '/api/start', 'since': int(time.time()), 'pid': os.getpid(), "
        "'slots': [1]}).encode())" + chr(10) +
        "print('held', flush=True)" + chr(10) +
        "end = time.time() + 20" + chr(10) +
        "while time.time() < end:" + chr(10) +
        "    try:" + chr(10) +
        "        doc = json.load(open(sys.argv[2]))" + chr(10) +
        "    except (OSError, ValueError):" + chr(10) +
        "        doc = {}" + chr(10) +
        "    if doc.get('pid') == os.getpid():" + chr(10) +
        "        break" + chr(10) +
        "    time.sleep(0.05)" + chr(10)), lock, request_file], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        status, body = post(A, "/api/stop", {"slot": 0})
    finally:
        holder.wait(10)
    assert status == 200 and (body["interrupted"], body["interrupted_where"], body["beside"]) == (
        "Start", "another copy", ""), body
    assert logged(log) == ["docker compose --profile judge stop", "slots.sh run 1 stop"], logged(log)
    # what the lock carries is read as slot numbers only; a holder from before the record names none
    assert A.slot_list([1, "2", True, -1, 1, 0]) == [0, 1] and A.slot_list(None) == []


def test_a_post_needs_json_and_this_application_s_own_origin(app, monkeypatch):
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "compose", compose_running(A, seen, ["true"]))
    for ctype in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        headers = both(A) if ctype is None else both(A, **{"Content-Type": ctype})
        assert request(A, "POST", "/api/start", body="{}", headers=headers)[0] == 415, ctype
    for origin in ("null", "http://evil.example", f"http://127.0.0.1:{A.APP.port + 1}", f"http://localhost:{A.APP.port}"):
        headers = both(A, **{"Content-Type": "application/json", "Origin": origin})
        assert request(A, "POST", "/api/start", body="{}", headers=headers)[0] == 401, origin
    assert seen == []
    headers = both(A, **{"Content-Type": "application/json; charset=utf-8", "Origin": f"http://127.0.0.1:{A.APP.port}"})
    assert request(A, "POST", "/api/start", body="{}", headers=headers)[0] == 200 and seen == ["up"]


def test_the_settings_are_this_user_s_only_from_their_first_byte(app, monkeypatch):
    A, _ = app
    modes = []
    real_dump = A.json.dump

    def dump(doc, f, *a, **k):
        modes.append(stat.S_IMODE(os.fstat(f.fileno()).st_mode))
        return real_dump(doc, f, *a, **k)
    monkeypatch.setattr(A.json, "dump", dump)
    old = os.umask(0o022)
    try:
        A.save_settings({"alerts": True})
        assert stat.S_IMODE(os.stat(A.CONFIG_DIR).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(os.path.join(A.CONFIG_DIR, "app.json")).st_mode) == 0o600
        # a directory an older version left open to others is narrowed
        os.chmod(A.CONFIG_DIR, 0o755)
        A.save_settings({"alerts": False})
        assert stat.S_IMODE(os.stat(A.CONFIG_DIR).st_mode) == 0o700
    finally:
        os.umask(old)
    assert modes == [0o600, 0o600], modes
    assert A.settings() == {"alerts": False}


def test_a_payout_that_stop_interrupted_answers_what_is_known(app, monkeypatch):
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + chr(10))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1self"})
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, **kw: (A.INTERRUPTED_RC, A.INTERRUPTED_TEXT))
    status, body = post(A, "/api/payout", {"address": GOOD, "code": fresh_code(A, "payout", address=GOOD)})
    assert status == 409 and body["declared"] is None and body["interrupted"] is True, body
    assert "not known" in body["error"] and "new code" in body["error"], body
    assert "payout_address" not in A.settings()
