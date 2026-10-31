"""Bench of the Dendra desktop application's local server: who may call it, and the first-launch steps.

What is replaced: docker (the kit's containers and the miner's keyring) — the application only reads and
drives them, and that driving is a `docker compose` command line — and the Final Testnet Season service, by a
local HTTP server that answers what each case needs, with rankings made by the programme's own calculation
(`publish`). What is real: the HTTP server, the token and Host checks, the access file the browser opens,
the recovery-phrase confirmation, the address check, the formula rerun on a ranked day, the reading of the
programme's rankings, the command lines handed to the miner container (run on this machine by
`in_container`, or by the miner's own CLI), the page's own script (run by node when it is installed) and
the window check of `install_app.sh`."""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Kept before any case replaces them on the modules the application shares with this bench.
_RUN, _WHICH, _SLEEP = subprocess.run, shutil.which, time.sleep

# The subject can be pointed elsewhere (a mutated copy) with DENDRA_APP_DIR; by default it is the
# shipped `deploy/app`, found by walking up from this file.
APP = os.environ.get("DENDRA_APP_DIR", "")
if not APP:
    APP = os.path.dirname(os.path.abspath(__file__))
    while APP != os.path.dirname(APP) and not os.path.isdir(os.path.join(APP, "deploy", "app")):
        APP = os.path.dirname(APP)
    APP = os.path.join(APP, "deploy", "app")
sys.path.insert(0, APP)
# Where the miner image's own code lives (WORKDIR /app in the image): the commands the application hands to
# the miner container (`python3 -m modea.keyring ...`) are run from there by `in_container`.
SERVICES = next((p for p in (os.path.join(os.path.dirname(os.path.dirname(APP)), "prototype", "mode-a"),
                             os.path.join(os.path.dirname(os.path.dirname(APP)), "services"))
                 if os.path.isdir(os.path.join(p, "modea"))), os.getcwd())

import pytest  # noqa: E402

WORDS = ("abandon ability able about above absent absorb abstract absurd abuse access accident "
         "account accuse achieve acid acoustic acquire across act action actor actress actual").split()
OTHER = ("zebra zero zone zoo youth young yellow year wrist write wrong yard "
         "wreck worth world work wool wood wolf witness wise wire winter wing").split()
NL = chr(10)
DENIED = ("permission denied while trying to connect to the Docker daemon socket at "
          "unix:///var/run/docker.sock: dial unix /var/run/docker.sock: connect: permission denied")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    kit = tmp_path / "kit"
    kit.mkdir()
    monkeypatch.setenv("DENDRA_MINER_KIT", str(kit))
    import dendra_app as A
    importlib.reload(A)
    srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    A.APP = A.App(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield A, kit
    srv.shutdown()


class _Programme(BaseHTTPRequestHandler):
    """A programme service that answers each route with what the case put there, 404 otherwise, and keeps
    the list of paths it was asked for."""

    def log_message(self, *a):
        return

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        self.server.seen.append(path)
        code, body = self.server.routes.get(path, (404, b'{"error": "route"}'))
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def programme():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Programme)
    srv.routes, srv.seen = {}, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def route(path, code, doc):
        srv.routes["/final-season/v1/" + path] = (code, doc if isinstance(doc, bytes) else json.dumps(doc).encode())
    route.seen = srv.seen
    yield f"http://127.0.0.1:{srv.server_address[1]}/final-season/v1", route
    srv.shutdown()


def publish(route, days):
    """Rankings of days 0..N-1 as the service writes them: each day's identities (`final_season_calc.Identity`)
    through the programme's own calculation, what the earlier days paid carried over, then the next day
    "not published". The bench reads the format the service writes, not a shape written for the bench."""
    import dataclasses
    import final_season_calc as SC
    docs, paid = [], 0
    for n, identities in enumerate(days):
        # The service's facts always carry an address (declared, else the operator): a row without one is
        # not paid, which is not what these cases are about.
        identities = [i if i.payout_address else dataclasses.replace(i, payout_address=f"dendra1{i.miner_id}")
                      for i in identities]
        doc = SC.to_json(SC.compute_day(n, identities, paid))
        paid += doc["total_paid_udndr"]
        route(f"ranking/day-{n:03d}.json", 200, doc)
        docs.append(doc)
    route(f"ranking/day-{len(days):03d}.json", 404, {"error": "not published"})
    return docs


def row_of(doc, mid="dm1abc"):
    return next(x for x in doc["ranking"] if x["miner_id"] == mid)


def status_doc(day, end_height=None):
    """The service's own status (`final_season_server._status`) on day `day`. By default the season is
    running: the latest block's time is ten days before the end (`rules.end_time`). With `end_height` the
    chain has passed the end, and the season's last day is the one holding that block, derived as the
    service derives it (`State.last_day`), never written by the case."""
    import final_season_rules as R
    ended = end_height is not None
    return {"season": "final-testnet", "start_height": 100, "height": 100 + 17_280 * day + 5, "day": day,
            "rules": R.RULES, "rules_fingerprint": R.fingerprint(),
            "latest_block_time": R.end_epoch() + (60 if ended else -10 * 86400), "ended": ended,
            "end_height": end_height, "last_day": R.day_of_height(end_height, 100) if ended else None}


def no_docker(A, monkeypatch, mid="dm1abc"):
    """The overview's docker and chain readings, replaced: these cases are about the programme."""
    monkeypatch.setattr(A, "containers", lambda: ([], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": mid, "address": ""})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    monkeypatch.setattr(A, "recovery_phrase", lambda: {"present": False})
    monkeypatch.setattr(A, "recovery_head", lambda *a: {"present": False})
    monkeypatch.setattr(A, "keys_status", lambda: {})


def in_container(tmp_path, env=None):
    """The miner container, played on this machine: the command line the application hands to `docker
    compose exec` is run, from the miner image's code directory, with /data/keys mapped to a local
    directory and the container's environment given by the case (no keyring directory of the machine, no
    passphrase file unless the case names one). A fake that answered by matching the command would bench
    the match, not the line."""
    keys = tmp_path / "container-keys"
    keys.mkdir(exist_ok=True)
    seen = []
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DENDRA_KEYRING_DIR": str(keys / "cosmos"),
            "DENDRA_KEYRING_PASSPHRASE_FILE": str(tmp_path / "no-passphrase-file"), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(*cmd, timeout=60):
        argv = [str(keys) + c[len("/data/keys"):] if c.startswith("/data/keys") else c for c in cmd]
        if argv[0] == "python3":
            argv[0] = sys.executable
        r = _RUN(argv, capture_output=True, text=True, timeout=timeout, cwd=SERVICES,
                 env={**base, **(env or {})})
        seen.append((cmd, r.stdout + r.stderr))
        return r.returncode, r.stdout + r.stderr
    return keys, seen, run


def launched(tmp_path, monkeypatch):
    """The application as `main` starts it, without a native window and with the browser replaced by a
    list of what it was handed. HOME is the case's own, so no real `~/snap` decides where the file goes."""
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("DENDRA_MINER_KIT", str(tmp_path / "kit"))
    import dendra_app as A
    importlib.reload(A)
    import webbrowser
    handed = []
    monkeypatch.setattr(A, "open_window", lambda base, page: False)
    monkeypatch.setattr(webbrowser, "open", lambda u, *a, **k: handed.append(u) or True)
    return A, run, handed


def stop_at_once(_seconds):
    raise KeyboardInterrupt


# The page's script, run under node with a document reduced to what the script touches: elements by id
# (those the page hides at first start hidden), created elements, text nodes and fetch, which answers each
# path with the replies the case gave (the last one repeats). The plan's steps then click and type.
UI_HARNESS = """
"use strict";
const fs = require("fs");
const vm = require("vm");
const page = fs.readFileSync(process.argv[2], "utf8");
const plan = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const src = page.slice(page.indexOf("<script>") + 8, page.lastIndexOf("</script>"));
function stub(id, hidden) {
  const classes = new Set(hidden ? ["hidden"] : []);
  return { id: id, value: "", dataset: {}, style: {}, children: [], _html: "", _text: "",
    get textContent() { return this._text; }, set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; this.children = []; },
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { if (on === undefined ? !classes.has(c) : on) classes.add(c); else classes.delete(c); } },
    appendChild(ch) { this.children.push(ch); return ch; } };
}
const els = {};
for (const [id, hidden] of plan.ids) els[id] = stub(id, hidden);
const calls = [], bodies = [], heads = [], cleared = [];
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
async function fetch(path, opt) {
  calls.push({ path: path, method: (opt && opt.method) || "GET" });
  heads.push({ path: path, headers: (opt && opt.headers) || {}, credentials: (opt && opt.credentials) || null });
  if (opt && opt.body !== undefined) bodies.push({ path: path, body: JSON.parse(opt.body) });
  const q = plan.replies[path] || [{ status: 404, data: {} }];
  const r = q.length > 1 ? q.shift() : q[0];
  // Every answer carries the instance value of the Dendra that served the page, unless the reply names another
  // (null for none), as a program that took the port after Dendra stopped would answer.
  const inst = has(r, "instance") ? r.instance : plan.instance;
  return { status: r.status, json: async () => r.data,
    headers: { get: (n) => (String(n).toLowerCase() === "x-dendra-instance" ? inst : null) } };
}
let timer = 0;
const ctx = vm.createContext({
  document: {
    getElementById: (id) => els[id] || (els[id] = stub(id, false)),
    createElement: () => stub("", false),
    createTextNode: (t) => ({ textContent: String(t) }),
    querySelectorAll: (sel) => (sel === "#ask input" ? els.ask.children : []),
  },
  // The tab's sessionStorage holds what /handoff left there, and the page only reads it. No localStorage, history
  // or document.cookie: the page keeps no token of its own, and a use of any of them is an error here.
  sessionStorage: { getItem: (k) => (has(plan.storage, k) ? plan.storage[k] : null) },
  location: { pathname: "/" }, fetch: fetch, setInterval: () => ++timer, clearInterval: (t) => cleared.push(t),
  console: console,
});
const settle = async () => { for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r)); };
(async () => {
  vm.runInContext(src, ctx);
  await settle();
  for (const step of plan.steps) {
    if (step[0] === "click") await els[step[1]].onclick();
    if (step[0] === "type") els.ask.children.forEach((inp) => { inp.value = step[1][inp.dataset.pos] || ""; });
    if (step[0] === "select") { els[step[1]].value = step[2]; await els[step[1]].onchange(); }
    if (step[0] === "fill") els[step[1]].value = step[2];
    await settle();
  }
  const out = { text: {}, hidden: {}, calls: calls, bodies: bodies, heads: heads, timers: timer, cleared: cleared,
    words: els.words.children.map((d) => d.children.map((t) => t.textContent).join("")),
    rules: els.statusRules.children.map((li) => ({ cls: li.className, parts: li.children.map((c) => (
      { text: c.textContent, href: c.href || null, cls: c.className || "" })) })) };
  out.cls = {}; out.disabled = {};
  for (const id of Object.keys(els)) { out.text[id] = els[id].textContent; out.hidden[id] = els[id].classList.contains("hidden");
    out.disabled[id] = els[id].disabled === true;
    out.cls[id] = typeof els[id].className === "string" ? els[id].className : ""; }
  out.kids = {};
  for (const id of Object.keys(els)) { out.kids[id] = els[id].children.map((c) => c.textContent); }
  out.rows = {};
  for (const id of Object.keys(els)) { out.rows[id] = els[id].children.map((c) => (c.children || []).map((t) => t.textContent)); }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify({ error: String((e && e.stack) || e) })); process.exit(1); });
"""


# What /handoff leaves in the tab's sessionStorage, as the page's script reads it.
UI_SESSION, UI_INSTANCE = "S" * 43, "I" * 43
UI_STORAGE = {"dendra-session": UI_SESSION, "dendra-instance": UI_INSTANCE}


def run_ui(tmp_path, replies, steps=(), storage=None):
    node = _WHICH("node")
    if not node:
        pytest.skip("node is not installed: the page's script is not run here (declared, not passed)")
    page_path = os.path.join(APP, "ui", "index.html")
    with open(page_path, encoding="utf-8") as f:
        page = f.read()
    ids = []
    for attrs in re.findall(r'<[a-z0-9]+ ([^>]*)>', page):
        m, c = re.search(r'id="([^"]+)"', attrs), re.search(r'class="([^"]*)"', attrs)
        if m:
            ids.append([m.group(1), bool(c) and "hidden" in c.group(1).split()])
    (tmp_path / "ui_harness.js").write_text(UI_HARNESS, encoding="utf-8")
    (tmp_path / "ui_plan.json").write_text(json.dumps({"ids": ids, "replies": replies, "steps": list(steps),
                                                       "instance": UI_INSTANCE,
                                                       "storage": UI_STORAGE if storage is None else storage}))
    # The page writes amounts in the reader's locale ("2,38" under fr_FR): the bench pins one, so that what a
    # case reads is the page's figure and not the machine's language.
    r = _RUN([node, str(tmp_path / "ui_harness.js"), page_path, str(tmp_path / "ui_plan.json")],
             capture_output=True, text=True, timeout=60,
             env=dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8"))
    out = json.loads(r.stdout or "{}")
    assert r.returncode == 0 and "error" not in out, r.stdout + r.stderr
    return out


def session_cookie(A, token=True):
    """The Cookie header a browser sends once /handoff has set the session (dendra_app.py::Handler._handoff)."""
    return f"{A.SESSION_COOKIE}{A.APP.port}={A.APP.token if token is True else token}"


def session_headers(A, token=True):
    """Both halves of the session as the page sends them: the cookie, and the header from the tab's sessionStorage
    (dendra_app.py::Handler._auth). Read with getattr so that a case run against an application from before the
    header fails on the decision it guards, not on a name the older application lacks."""
    return {"Cookie": session_cookie(A, token),
            getattr(A, "SESSION_HEADER", "X-Dendra-Session"): getattr(A.APP, "tab_key", "")}


CODE_SHAPE = re.compile("([A-Z0-9]{4})-([A-Z0-9]{4})")


def fresh_code(A, purpose, strict=True, **target):
    """A confirmation code as the terminal shows it: asked through /api/code for the change `target` names (a
    payout's address and slot, the slot whose words are shown), as the page asks it, and read from what the
    application printed (dendra_app.py::App.issue_code), never from the answer. Not strict, a change /api/code
    refuses gets False."""
    lines = []
    A.APP.code_sinks.append(lines.append)
    try:
        status, body = call(A, "/api/code", dict({"for": purpose}, **target), code=False)
    finally:
        A.APP.code_sinks.remove(lines.append)
    if not strict and status != 200:
        return False
    assert status == 200 and body.get("sent") is True and len(lines) == 1, (status, body, lines)
    m = CODE_SHAPE.search(lines[0])
    assert m and m.group(1) + m.group(2) not in json.dumps(body)
    return m.group(1) + "-" + m.group(2)


def call(A, path, body=None, token=True, host=None, code=True, tab=True):
    """One call as the page makes it: both halves of the session (`tab` False leaves the header out), and for the
    two routes that take a confirmation code (GET /api/recovery, POST /api/payout) a fresh code for the change the
    call names when `code` is True (False sends none, a string sends it). A change /api/code refuses gets no code,
    and the route says why."""
    takes = (body is None and path.split("?")[0] == "/api/recovery") or (path == "/api/payout" and isinstance(body, dict))
    if code is True and takes and token is True:
        if body is None:
            slot = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("slot")
            code = fresh_code(A, "recovery", strict=False, **({"slot": slot[0]} if slot else {}))
        else:
            code = fresh_code(A, "payout", strict=False, **{k: body[k] for k in ("address", "slot") if k in body})
    elif code is True:
        code = False
    if takes and body is not None and code is not False and "code" not in body:
        body = dict(body, code=code)
    url = f"http://127.0.0.1:{A.APP.port}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("Content-Type", "application/json")    # as the page's api() posts
    if token:
        for k, v in session_headers(A, token).items():
            if k == "Cookie" or tab:
                req.add_header(k, v)
    if takes and body is None and code is not False:
        req.add_header(A.CODE_HEADER, code)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] == b"{" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw[:1] == b"{" else raw)


def test_the_page_is_served_but_the_api_needs_the_token(app):
    A, _ = app
    code, page = call(A, "/", token=False)
    assert code == 200 and b"Dendra" in page
    assert call(A, "/api/overview", token=False)[0] == 401
    assert call(A, "/api/overview", token="wrong")[0] == 401
    assert call(A, "/api/start", body={}, token=False)[0] == 401


def test_a_foreign_host_header_is_refused_even_with_the_token(app):
    A, _ = app
    # DNS rebinding: a page on evil.example resolved to 127.0.0.1 sends its own Host header.
    assert call(A, "/", host="evil.example")[0] == 403
    assert call(A, "/api/overview", host=f"evil.example:{A.APP.port}")[0] in (401, 403)


@pytest.mark.parametrize("headless", [False, True])
def test_the_token_reaches_no_command_line_and_no_terminal(tmp_path, monkeypatch, capsys, headless):
    # A URL carrying the token, printed or passed to the browser's argv, is readable by every local user
    # (/proc/<pid>/cmdline, session logs), and with it the 24 words. The browser gets a 0600 file instead.
    # R73: the launch from Linux goes through /handoff as the Windows one does. The file holds a one-time token in
    # a form, never the session token; /handoff spends it, removes the file and sets the session cookie.
    A, run, handed = launched(tmp_path, monkeypatch)
    seen = {}

    def while_running(_seconds):
        files = sorted((run / "dendra").glob("*.html"))
        seen.update(files=files, token=A.APP.token, dir_mode=(run / "dendra").stat().st_mode & 0o777)
        text = files[0].read_text()
        seen.update(file_mode=files[0].stat().st_mode & 0o777, holds=A.APP.token in text, text=text)
        once = re.search('name="t" value="([A-Za-z0-9_-]{43})"', text)
        seen["handoff"] = handoff(A, urllib.parse.urlencode({"t": once.group(1) if once else ""}).encode())
        seen.update(auth=call(A, "/api/overview")[0], left=files[0].exists())
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--no-window"] if headless else []) == 0
    out, tok = capsys.readouterr().out, seen["token"]
    assert "http://127.0.0.1:" in out and tok not in out
    assert all(tok not in u and "#t=" not in u for u in handed)
    if not headless:
        assert len(handed) == 1 and handed[0].startswith("file://") and handed[0].endswith(".html")
    else:
        # a host without a browser: the message says to copy the file and where to tunnel to
        assert str(seen["files"][0]) in out and f"SSH tunnel to port {A.APP.port}" in out
    assert len(seen["files"]) == 1 and seen["file_mode"] == 0o600 and seen["dir_mode"] == 0o700
    assert seen["holds"] is False and "#t=" not in seen["text"] and 'action="http://127.0.0.1:' in seen["text"]
    code, body, headers = seen["handoff"]
    assert code == 200 and tok not in body
    cookie = headers.get("Set-Cookie", "")
    assert cookie == f"{A.SESSION_COOKIE}{A.APP.port}={tok}; HttpOnly; SameSite=Strict; Path=/", cookie
    # /handoff removed the file: the session lives on in the browser's cookie only
    assert seen["auth"] == 200 and seen["left"] is False


def test_a_browser_that_does_not_start_leaves_the_file_and_says_where_it_is(tmp_path, monkeypatch, capsys):
    # webbrowser.open returns False with no display (an SSH session, for one). The reader must then get the
    # file's path, and the file must stay.
    import webbrowser
    A, run, handed = launched(tmp_path, monkeypatch)
    monkeypatch.setattr(webbrowser, "open", lambda u, *a, **k: False)
    timers = []
    monkeypatch.setattr(A.threading, "Timer", lambda *a, **k: timers.append(a) or (_ for _ in ()).throw(AssertionError("timer")))
    seen = {}

    def while_running(_seconds):
        seen["files"] = sorted((run / "dendra").glob("*.html"))
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main([]) == 0
    out = capsys.readouterr().out
    assert "No browser could be started" in out and str(seen["files"][0]) in out
    assert A.APP.token not in out and timers == []      # the file is not timed out: it is the only way in


def test_an_unused_access_file_is_removed_after_its_delay(tmp_path, monkeypatch):
    # The browser may never come (closed, refused the file): the file then goes after LAUNCH_FILE_SECONDS.
    A, run, handed = launched(tmp_path, monkeypatch)
    assert A.LAUNCH_FILE_SECONDS == 120
    monkeypatch.setattr(A, "LAUNCH_FILE_SECONDS", 0.3)
    seen = {}

    def while_running(_seconds):
        f = sorted((run / "dendra").glob("*.html"))[0]
        deadline = time.monotonic() + 10
        while f.exists() and time.monotonic() < deadline:
            _SLEEP(0.05)
        seen["gone"] = not f.exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main([]) == 0 and len(handed) == 1
    assert seen["gone"] is True


def test_the_access_directory_must_be_this_user_s_own(tmp_path, monkeypatch):
    # A directory planted under that name (a link elsewhere, or another user's) would receive the token.
    A, run, handed = launched(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (run / "dendra").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(A.time, "sleep", stop_at_once)
    assert A.main([]) == 2 and handed == [] and list(elsewhere.iterdir()) == []
    (run / "dendra").unlink()
    assert A.launch_dir() == str(run / "dendra")
    uid = os.getuid()
    monkeypatch.setattr(A.os, "getuid", lambda: uid + 1)
    with pytest.raises(OSError):
        A.launch_dir()


def test_a_snap_browser_gets_the_access_file_in_its_own_directory(tmp_path, monkeypatch):
    # Ubuntu's default Firefox is a snap: it is refused $XDG_RUNTIME_DIR and the hidden directories of
    # $HOME, so it answered "access denied" to the access file. It can read ~/snap/<name>/common.
    A, run, handed = launched(tmp_path, monkeypatch)
    common = tmp_path / "home" / "snap" / "firefox" / "common"
    common.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "xdg-mime").write_text(NL.join([
        "#!/bin/sh", '[ "$1 $2 $3" = "query default text/html" ] || exit 1', 'echo "$FAKE_HTML_HANDLER"', ""]))
    (bin_dir / "xdg-mime").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_HTML_HANDLER", "firefox_firefox.desktop")
    monkeypatch.setattr(A.time, "sleep", stop_at_once)
    assert A.main([]) == 0 and len(handed) == 1
    assert handed[0].startswith((common / "dendra").as_uri() + "/") and handed[0].endswith(".html")
    assert (common / "dendra").stat().st_mode & 0o777 == 0o700
    # a browser that is not a snap, or a snap without its directory here: the per-user runtime directory
    for handler in ("firefox.desktop", "chromium_chromium.desktop"):
        monkeypatch.setenv("FAKE_HTML_HANDLER", handler)
        assert A.launch_dir() == str(run / "dendra")


def test_install_app_checks_webkit_not_only_gtk(tmp_path):
    # GTK 3 alone is on most desktops: the script said nothing, and the application opened in the browser.
    # The real python3 runs the script's check against a `gi` that has the `GIR` namespaces each case lists.
    bin_dir, gi_dir, home = tmp_path / "bin", tmp_path / "fakegi" / "gi", tmp_path / "home"
    for d in (bin_dir, gi_dir, home):
        d.mkdir(parents=True)
    (gi_dir / "__init__.py").write_text(NL.join([
        "import os",
        "HAVE = set(os.environ.get('FAKE_GI', '').split(','))",
        "def require_version(ns, v):",
        "    if f'{ns}-{v}' not in HAVE:",
        "        raise ValueError(f'Namespace {ns} not available for version {v}')", ""]))
    # A hermetic PATH: only the tools the script needs, so nothing of this machine answers in their place.
    os.symlink(sys.executable, bin_dir / "python3")
    for tool in ("mkdir", "chmod", "cp", "dirname", "cat"):
        os.symlink(_WHICH(tool), bin_dir / tool)
    script = os.path.join(APP, "install_app.sh")
    for have, advised in (("Gtk-3.0", True), ("", True), ("Gtk-3.0,WebKit2-4.1", False), ("Gtk-3.0,WebKit2-4.0", False)):
        r = _RUN([_WHICH("bash"), script], capture_output=True, text=True, timeout=60,
                 env={"HOME": str(home), "PATH": str(bin_dir), "PYTHONPATH": str(tmp_path / "fakegi"), "FAKE_GI": have})
        assert r.returncode == 0, r.stdout + r.stderr
        assert ("gir1.2-webkit2-4.1" in r.stdout) is advised, (have, r.stdout)
    assert (home / ".local" / "bin" / "dendra").is_file()


def test_without_a_kit_the_overview_says_how_to_install(app):
    A, _ = app
    code, d = call(A, "/api/overview")
    assert code == 200 and d["installed"] is False and "install.sh" in d["hint"]


def kept_phrase(A, monkeypatch, address="dendra1x", words=WORDS):
    """A phrase kept on the volume, and the removal the application asks the container for, recorded: the
    removal takes the phrase away only for the address it names, as `modea.keyring forget-recovery` does."""
    phrase = {"present": True, "address": address, "words": list(words)}
    removed = []

    def forget(addr):
        removed.append(addr)
        if phrase.get("present") and phrase.get("address") == addr:
            phrase.clear()
            phrase["present"] = False
            return True, ""
        return False, "the phrase on the volume is not the one confirmed"
    monkeypatch.setattr(A, "recovery_phrase", lambda: dict(phrase))
    monkeypatch.setattr(A, "forget_recovery", forget)
    return phrase, removed


def test_recovery_needs_three_words_typed_back(app, monkeypatch):
    A, _ = app
    phrase, removed = kept_phrase(A, monkeypatch)
    code, d = call(A, "/api/recovery")
    assert code == 200 and d["words"] == WORDS and len(d["ask"]) == 3
    wrong = {str(i): "nope" for i in d["ask"]}
    assert call(A, "/api/recovery/confirm", body={"words": wrong})[0] == 400
    assert "recovery_confirmed_address" not in A.settings()
    assert removed == [], "a refused confirmation removes nothing"
    right = {str(i): WORDS[i - 1].upper() + " " for i in d["ask"]}
    code, r = call(A, "/api/recovery/confirm", body={"words": right})
    assert code == 200 and r.get("removed") is True
    assert A.settings()["recovery_confirmed_address"] == "dendra1x"
    # confirming removed the phrase from the machine, for its address only: nothing is left to serve
    assert removed == ["dendra1x"]
    code, d = call(A, "/api/recovery")
    assert d.get("present") is False and "words" not in d


def test_a_new_key_s_phrase_is_shown_again(app, monkeypatch):
    # The confirmation was kept once per user: a later key's phrase was never shown.
    A, _ = app
    phrase, removed = kept_phrase(A, monkeypatch, "dendra1aaa", WORDS)
    d = call(A, "/api/recovery")[1]
    assert call(A, "/api/recovery/confirm", body={"words": {str(i): WORDS[i - 1] for i in d["ask"]}})[0] == 200
    assert call(A, "/api/recovery")[1].get("present") is False and removed == ["dendra1aaa"]
    asked_for_a = d["ask"]
    phrase.update(present=True, address="dendra1bbb", words=OTHER)
    # positions drawn for the old phrase prove nothing about the new one, and remove nothing
    assert call(A, "/api/recovery/confirm",
                body={"words": {str(i): OTHER[i - 1] for i in asked_for_a}})[0] == 409
    assert removed == ["dendra1aaa"]
    code, d = call(A, "/api/recovery")
    assert code == 200 and d.get("present") is True and d["words"] == OTHER and len(d["ask"]) == 3
    # an old per-user flag hides nothing, and an empty address is never a confirmed key
    A.save_settings({"recovery_confirmed": True})
    assert call(A, "/api/recovery")[1].get("present") is True
    assert A.phrase_confirmed({"present": True, "address": ""}, {"recovery_confirmed_address": ""}) is False


def test_a_phrase_confirmed_by_an_older_application_is_offered_again_to_remove_it(app, monkeypatch):
    # An older application recorded the confirmation and never removed the file: it is still on the
    # machine, so it is served again -- the only way to remove it is the same three words.
    A, _ = app
    A.save_settings({"recovery_confirmed_address": "dendra1x"})
    phrase, removed = kept_phrase(A, monkeypatch)
    d = call(A, "/api/recovery")[1]
    assert d.get("present") is True and d.get("confirmed_before") is True and len(d["ask"]) == 3


def test_confirm_without_a_shown_phrase_is_refused(app, monkeypatch):
    A, _ = app
    phrase, removed = kept_phrase(A, monkeypatch, "a")
    assert call(A, "/api/recovery/confirm", body={"words": {"1": WORDS[0]}})[0] == 409
    assert removed == []


def test_a_phrase_without_an_address_cannot_be_confirmed(app, monkeypatch):
    # The confirmation of no address was accepted (200, stored "") and never honoured, so the screen hid
    # the phrase and brought it back at the next refresh, forever.
    A, _ = app
    phrase, removed = kept_phrase(A, monkeypatch, "")
    d = call(A, "/api/recovery")[1]
    assert d["words"] == WORDS
    code, r = call(A, "/api/recovery/confirm", body={"words": {str(i): WORDS[i - 1] for i in d["ask"]}})
    assert code == 422 and "no address" in r["error"]
    assert "recovery_confirmed_address" not in A.settings() and removed == []


def test_a_removal_that_fails_is_said_and_never_called_done(app, monkeypatch):
    A, _ = app
    phrase, removed = kept_phrase(A, monkeypatch)
    monkeypatch.setattr(A, "forget_recovery", lambda addr: (False, "the container did not answer"))
    d = call(A, "/api/recovery")[1]
    code, r = call(A, "/api/recovery/confirm", body={"words": {str(i): WORDS[i - 1] for i in d["ask"]}})
    assert code == 502 and r["removed"] is False and "could not be removed" in r["error"]


def test_the_overview_reads_whose_phrase_is_kept_never_its_words(app, tmp_path, monkeypatch):
    # The overview read the whole phrase file at every refresh, confirmed or not: the 24 words came into
    # this process every 15 seconds. It needs the address only; the words come when they are shown.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    keys, seen, run = in_container(tmp_path)
    phrase = keys / "recovery-phrase.json"
    phrase.write_text(json.dumps({"address": "dendra1aaa", "name": "dm1abc", "mnemonic": " ".join(WORDS)}))
    monkeypatch.setattr(A, "miner_exec", run)
    monkeypatch.setattr(A, "containers", lambda: ([], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1aaa"})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    words_out = lambda: [cmd for cmd, out in seen if " ".join(WORDS[:3]) in out]  # noqa: E731
    assert call(A, "/api/overview")[1]["recovery_confirmed"] is False
    assert seen and words_out() == []
    d = call(A, "/api/recovery")[1]            # the words, read because they are to be shown
    assert d["words"] == WORDS and words_out() != []
    assert call(A, "/api/recovery/confirm", body={"words": {str(i): WORDS[i - 1] for i in d["ask"]}})[0] == 200
    seen.clear()
    # confirmed means removed: nothing left to confirm, and no words read to find that out
    assert not phrase.exists()
    assert call(A, "/api/overview")[1]["recovery_confirmed"] is None
    assert seen and words_out() == []
    # a new key's phrase is seen from its address alone; a file that is not a phrase is no phrase
    phrase.write_text(json.dumps({"address": "dendra1bbb", "mnemonic": " ".join(OTHER)}))
    assert call(A, "/api/overview")[1]["recovery_confirmed"] is False
    phrase.write_text(json.dumps({"address": "dendra1bbb", "mnemonic": "only five words in here"}))
    assert call(A, "/api/overview")[1]["recovery_confirmed"] is None
    phrase.unlink()
    assert call(A, "/api/overview")[1]["recovery_confirmed"] is None


def _seal_phrase(path, passphrase, doc):
    """A phrase file as the daemon writes it with an encrypted keyring: sealed by modea.crypto."""
    sys.path.insert(0, SERVICES)
    from modea import crypto
    crypto.store_secret(str(path), json.dumps(doc).encode("utf-8"), passphrase, crypto.AAD_RECOVERY)


def test_confirming_removes_EXACTLY_the_phrase_file_and_only_after_the_words(app, tmp_path, monkeypatch):
    # The button says "remove it from this machine": the run command line removes that one file, sealed
    # with the keyring's passphrase here, once three words matched -- and a refused or a stale
    # confirmation removes nothing (both halves). Every other file of the volume stays as it is.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    pf = tmp_path / "keyring-passphrase"
    pf.write_text("p" * 64 + NL)
    keys, seen, run = in_container(tmp_path, env={"DENDRA_KEYRING_PASSPHRASE_FILE": str(pf)})
    phrase = keys / "recovery-phrase.json"
    _seal_phrase(phrase, "p" * 64, {"address": "dendra1aaa", "name": "dm1abc", "mnemonic": " ".join(WORDS)})
    assert " ".join(WORDS[:3]).encode() not in phrase.read_bytes(), "sealed: the words are not on the disk unencrypted"
    (keys / "cosmos").mkdir()
    (keys / "cosmos" / "dm1abc.info").write_text("kept")
    (keys / "dm1abc.sk").write_text("kept")
    (keys / "recovery-phrase.json.bak").write_text("kept")
    before = sorted(p.name for p in keys.rglob("*"))
    monkeypatch.setattr(A, "miner_exec", run)
    monkeypatch.setattr(A, "containers", lambda: ([], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1aaa"})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    d = call(A, "/api/recovery")[1]
    assert d["words"] == WORDS, "the application reads the sealed phrase, opened inside the container"
    assert call(A, "/api/recovery/confirm", body={"words": {str(i): "nope" for i in d["ask"]}})[0] == 400
    assert phrase.exists(), "refused words remove nothing"
    # stale: the key changed between the words shown and the words typed
    _seal_phrase(phrase, "p" * 64, {"address": "dendra1bbb", "name": "dm1abc", "mnemonic": " ".join(OTHER)})
    assert call(A, "/api/recovery/confirm", body={"words": {str(i): WORDS[i - 1] for i in d["ask"]}})[0] == 409
    assert phrase.exists(), "a stale confirmation removes nothing"
    d = call(A, "/api/recovery")[1]
    code, r = call(A, "/api/recovery/confirm", body={"words": {str(i): OTHER[i - 1] for i in d["ask"]}})
    assert code == 200 and r["removed"] is True and not phrase.exists()
    after = sorted(p.name for p in keys.rglob("*"))
    assert after == [n for n in before if n != "recovery-phrase.json"], (before, after)
    assert call(A, "/api/overview")[1]["recovery_confirmed"] is None


def test_the_overview_says_how_the_keys_are_kept_and_where_the_season_pays(app, tmp_path, monkeypatch):
    # Read inside the container in the same call as the phrase's address: the keyring's backend, the key
    # files sealed or not, and the payout declaration the programme accepted.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_PAYOUT_ADDRESS=" + NL)
    keys, seen, run = in_container(tmp_path)
    (keys / "resolved-identity").write_text("dm1abc" + NL)
    (keys / "cosmos" / "keyring-test").mkdir(parents=True)
    (keys / "cosmos" / "keyring-test" / "dm1abc.info").write_text("x")
    monkeypatch.setattr(A, "miner_exec", run)
    monkeypatch.setattr(A, "containers", lambda: ([], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1machine"})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    ov = call(A, "/api/overview")[1]
    assert ov["keys"]["at_rest"] == "clear" and ov["keys"]["backend"] == "test"
    assert ov["payout"]["to_machine_key"] is True and ov["payout"]["declared"] == ""
    # declared elsewhere: the record the programme's acceptance wrote in the volume
    (keys / "payout-declared.json").write_text(json.dumps({"miner_id": "dm1abc", "address": "dendra1cold"}))
    ov = call(A, "/api/overview")[1]
    assert ov["payout"]["to_machine_key"] is False and ov["payout"]["declared"] == "dendra1cold"
    # a declaration recorded for another identity is not this one's
    (keys / "payout-declared.json").write_text(json.dumps({"miner_id": "dm1other", "address": "dendra1cold"}))
    assert call(A, "/api/overview")[1]["payout"]["to_machine_key"] is True
    # an encrypted keyring whose passphrase is not there is "not opened", never "unencrypted"
    import shutil as _sh
    _sh.rmtree(keys / "cosmos" / "keyring-test")
    (keys / "cosmos" / "keyring-file").mkdir()
    (keys / "cosmos" / "keyring-file" / "keyhash").write_text("h")
    k = call(A, "/api/overview")[1]["keys"]
    assert k["at_rest"] is None and "passphrase missing" in k["error"]
    # nothing read at all: neither clear nor encrypted
    monkeypatch.setattr(A, "keys_status", lambda: {})
    ov = call(A, "/api/overview")[1]
    assert ov["keys"]["at_rest"] is None and ov["payout"]["to_machine_key"] is None


def test_the_page_shows_the_keys_the_payout_banner_and_the_new_button(app, tmp_path):
    A, _ = app
    base = dict(call(A, "/api/overview")[1], installed=True, identity={"miner_id": "dm1abc"}, containers=[],
                docker_error=None, chain={}, recovery_confirmed=None)
    page = call(A, "/", token=False)[1].decode("utf-8")
    assert "I wrote it down — remove it from this machine" in page
    clear = dict(base, keys={"at_rest": "clear", "backend": "test", "error": "", "clear_files": []},
                 payout={"to_machine_key": True, "declared": "", "setting": "", "machine_address": "dendra1machine"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": clear}]})
    assert "unencrypted" in ui["text"]["keysAtRest"] and "encrypt-keys.sh" in ui["text"]["keysAtRest"]
    assert ui["hidden"]["payoutBanner"] is False and "this machine's own key (dendra1machine)" in ui["text"]["payoutBannerText"]
    enc = dict(base, keys={"at_rest": "encrypted", "backend": "file", "error": "", "clear_files": []},
               payout={"to_machine_key": False, "declared": "dendra1cold", "setting": "", "machine_address": "dendra1machine"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": enc}]})
    assert ui["text"]["keysAtRest"].startswith("encrypted") and ui["hidden"]["payoutBanner"] is True
    assert "dendra1cold" in ui["text"]["payoutDeclared"]
    # not read is not "unencrypted", and an unread payout raises no banner
    unread = dict(base, keys={"at_rest": None, "backend": None, "error": "", "clear_files": []}, payout={"to_machine_key": None})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": unread}]})
    assert ui["text"]["keysAtRest"] == "not read" and ui["hidden"]["payoutBanner"] is True


def test_payout_address_is_checked_before_anything_runs(app, monkeypatch):
    A, _ = app
    ran = []
    monkeypatch.setattr(A, "miner_exec", lambda *a, **k: ran.append(a) or (0, "ok"))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1x"})
    assert call(A, "/api/payout", body={"address": "cosmos1zzz"})[0] == 400
    assert call(A, "/api/payout", body={"address": "dendra1; rm -rf /"})[0] == 400
    assert not ran
    good = "dendra1" + "q" * 38
    code, _ = call(A, "/api/payout", body={"address": good})
    assert code == 200 and ran and ran[0][-1] == good
    assert A.settings()["payout_address"] == good


def test_in_owner_mode_the_application_never_signs_the_payout_declaration(app, tmp_path, monkeypatch):
    # Owner mode: the programme accepts the declaration from the owner's key only, and it is not on this
    # machine. The application says how to declare it, and runs nothing; without the setting, as before.
    A, kit = app
    ran = []
    monkeypatch.setattr(A, "miner_exec", lambda *a, **k: ran.append(a) or (0, "ok"))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1machine"})
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MINER_OWNER=dendra1owner" + NL)
    good = "dendra1" + "q" * 38
    code, r = call(A, "/api/payout", body={"address": good})
    assert code == 409 and "dendra1owner" in r["error"] and "payout-prepare" in r["error"] and not ran
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    assert call(A, "/api/payout", body={"address": good})[0] == 200 and ran
    # the page's banner names the owner and the owner's steps
    base = dict(call(A, "/api/overview")[1], installed=True, identity={"miner_id": "dm1abc"}, containers=[],
                docker_error=None, chain={}, recovery_confirmed=None,
                keys={"at_rest": "encrypted", "backend": "file", "error": "", "clear_files": []})
    owned = dict(base, payout={"to_machine_key": True, "declared": "", "setting": "", "machine_address": "dendra1machine",
                               "owner": "dendra1owner"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": owned}]})
    assert ui["hidden"]["payoutBanner"] is False
    assert "owned by dendra1owner" in ui["text"]["payoutBannerText"] and "payout-prepare" in ui["text"]["payoutBannerText"]


def test_the_payout_command_is_one_the_miner_s_cli_accepts(app, monkeypatch):
    # The application hands the miner container a command line. The shipped CLI (`final_season_miner.main`) is
    # run on that very line here, its signed declaration recorded instead of sent: a renamed option, or an
    # identity the CLI would not sign for, fails here and not in front of a miner.
    A, _ = app
    import final_season_miner as M
    ran = []
    monkeypatch.setattr(A, "miner_exec", lambda *a, **k: ran.append(a) or (0, '{"ok": true}'))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1x"})
    good = "dendra1" + "q" * 38
    assert call(A, "/api/payout", body={"address": good})[0] == 200
    argv = ran[0]
    assert argv[:2] == ("python3", "final_season_miner.py")
    declared = []
    monkeypatch.setattr(M, "PROGRAMME", "http://programme.example/final-season/v1")
    monkeypatch.setattr(M, "resolved_identity", lambda path=None: "")
    monkeypatch.setattr(M, "declare_payout",
                        lambda base, mid, address, sign=None: declared.append((base, mid, address)) or (200, {"ok": True}))
    assert M.main(list(argv[2:])) == 0
    assert declared == [("http://programme.example/final-season/v1", "dm1abc", good)]


def test_no_docker_access_is_said_and_never_shown_as_no_container(app, monkeypatch):
    # Right after the installer adds the user to the docker group, this session still cannot open the
    # socket: that failure was an empty list, shown as "no miner container".
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/docker")
    # docker's own answer, as run_child (the one runner of every command) hands it to compose
    monkeypatch.setattr(A, "run_child", lambda argv, timeout, **k: (1, "", DENIED, ""))
    code, d = call(A, "/api/overview")
    assert code == 200 and d["containers"] is None
    assert d["docker_error"] == A.NO_DOCKER_ACCESS
    assert d["docker_error"] == ("No access to Docker: log out and log back in "
                                 "(you were just added to the docker group)")
    code, d = call(A, "/api/start", body={})
    assert d["rc"] != 0 and d["text"].startswith(A.NO_DOCKER_ACCESS)
    # the payout said "miner not running" to a session that simply cannot see it
    code, d = call(A, "/api/payout", body={"address": "dendra1" + "q" * 38})
    assert code == 409 and d["error"] == A.NO_DOCKER_ACCESS
    # Docker that answers with no container is a reading: an empty list, and no error
    monkeypatch.setattr(A, "run_child", lambda argv, timeout, **k: (0, "", "", ""))
    assert A.containers() == ([], None)


def test_the_page_says_no_access_to_docker(app, tmp_path, monkeypatch):
    # The server's half alone proves nothing on screen: the page's own script is run on its answer.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(A, "run_child", lambda argv, timeout, **k: (1, "", DENIED, ""))
    ov = call(A, "/api/overview")[1]
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
    # the pill is the state's word (STATUS_RULES); the state's headline and the container line say why
    assert (ui["text"]["state"], ui["cls"]["state"]) == ("Act now", "pill bad") and ui["text"]["ctr"] == A.NO_DOCKER_ACCESS
    assert A.NO_DOCKER_ACCESS in ui["text"]["statusHeadline"]
    # and Docker that answers with no container says so, with no error
    ov = dict(ov, containers=[], docker_error=None)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
    assert ui["text"]["ctr"] == "none"


def test_the_page_shows_unread_values_as_question_marks(app, tmp_path):
    # The server's half (None for an unread value) is benched below; the screen's half is benched here, by
    # the page's own script: day null is not "day 1", an unread total is not "0 DNDR", and figures that
    # were not read are not "0 requests".
    A, _ = app
    import final_season_rules
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=True)
    unread_prog = {"enabled": True,
                   "status": {"day": None, "rules": final_season_rules.RULES, "ended": None, "last_day": None},
                   "ranked_days": None, "season_paid_udndr": None, "latest": None}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=unread_prog)}]})
    t = ui["text"]
    assert t["day"] == "the programme service has no block height yet"
    assert t["ranked"] == "?" and t["paid"] == "?" and t["lastPaid"] == "?"
    assert t["presence"] == "?" and t["requests"] == "?" and t["verdicts"] == "?" and t["lastDay"] == "?"
    # a status that could not be read at all is "?", not day 1
    no_status = dict(unread_prog, status=None)
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=no_status)}]})["text"]
    assert t["day"] == "?"
    # nothing ranked yet is a reading, and it is not "?"
    running = {"day": 0, "rules": final_season_rules.RULES, "ended": False, "last_day": None}
    none_yet = dict(unread_prog, status=running, ranked_days=0, season_paid_udndr=0)
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=none_yet)}]})["text"]
    # The season ends at a fixed instant read against block times, not after a count of
    # days, so the screen gives the day and the end, never "N of M".
    assert t["day"] == "day 1, the season ends 7 November 2026, 23:59 UTC"
    assert t["ranked"] == "none yet" and t["lastDay"] == "no day ranked yet"
    assert t["paid"] == "0 DNDR over 0 ranked day(s)" and t["lastPaid"] == "—"

    def day_text(**status):
        prog = dict(none_yet, status=dict(running, **status))
        return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=prog)}]})["text"]["day"]
    # `ended` decides, not a count of days: a late day is
    # still running while the chain is before the end, and the season is over once it is past it, with the
    # last day the service found.
    assert day_text(day=40) == "day 41, the season ends 7 November 2026, 23:59 UTC"
    assert day_text(day=31, ended=True, last_day=29) == "the season is over (its last day was day 30)"
    # past the end, but the season's last block not found yet: over, and no last day guessed
    assert day_text(day=31, ended=True) == "the season is over"
    # an `ended` that is missing or not a boolean is "?": an unread end is neither "running" nor "over"
    assert day_text(ended=None) == "?"
    for bad in ("false", 0, 1, "true"):
        assert day_text(ended=bad) == "?", bad
    no_ended = dict(none_yet, status={k: v for k, v in running.items() if k != "ended"})
    assert run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=no_ended)}]})["text"]["day"] \
        == "?"
    assert day_text(day=-1) == "not started"
    # the end is written from the rules the server sends, never restated by the page
    assert day_text(rules=dict(final_season_rules.RULES, end_time="2026-12-01T00:00:00Z")) \
        == "day 1, the season ends 30 November 2026, 23:59 UTC"
    assert day_text(rules={k: v for k, v in final_season_rules.RULES.items() if k != "end_time"}) \
        == "day 1, end not readable"


def test_the_formula_rerun_is_the_published_one(app):
    # 0.05 DNDR per verified programme request, 0.008 per
    # consistent verdict, 0.002 per availability window proven on chain, at most 50 a day, paid only on a day
    # with verified work.
    # The parts are the shipped calculation applied to part of the facts, never a rate restated by the app.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules
    assert A.RULES is final_season_rules.RULES and A.gross_of is SC.gross_of
    assert A.formula_of(40, 3, 1) == {"work_udndr": 150_000, "juror_udndr": 8_000, "presence_udndr": 80_000,
                                      "judge_udndr": 0, "probation_udndr": 0, "gross_udndr": 238_000}
    assert A.formula_of(60, 1, 0)["presence_udndr"] == 50 * 2_000
    # presence is paid only on a day with at least one verified request
    assert A.formula_of(40, 0, 2) == {"work_udndr": 0, "juror_udndr": 16_000, "presence_udndr": 0,
                                      "judge_udndr": 0, "probation_udndr": 0, "gross_udndr": 16_000}
    for facts in ((0, 0, 0), (7, 2, 3), (200, 30, 9), (1, 1, 0)):
        f = A.formula_of(*facts)
        assert f["gross_udndr"] == SC.gross_of(SC.Identity("x", *facts))
        assert f["work_udndr"] + f["juror_udndr"] + f["presence_udndr"] == f["gross_udndr"]
    assert b"public node" not in call(A, "/", token=False)[1]


def test_the_page_offers_no_exam_no_class_and_no_window_question(app):
    # The Final Testnet Season sets no window question and no class exam:
    # a page that offered a retake, or showed a class or an estimate, would describe a
    # programme that does not exist.
    A, _ = app
    assert call(A, "/api/exam/retake", body={})[0] == 404
    page = call(A, "/", token=False)[1].decode("utf-8").lower()
    for gone in ("exam", "model class", "question open", "opens at block", "windows today", "estimate"):
        assert gone not in page, gone
    assert "the current day has no figure yet" in page
    assert not hasattr(A, "estimate") and not hasattr(A, "RETAKE_FLAG")


def test_a_service_without_a_block_height_does_not_break_the_overview(app, programme, monkeypatch):
    A, kit = app
    import final_season_calc as SC
    base, route = programme
    route("status", 200, dict(status_doc(0), height=0, day=None))
    # Published rankings are there, but without the current day the walk has no bound: nothing is read.
    publish(route, [[SC.Identity("dm1abc", verified_requests=2)]])
    p = A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")
    assert p["status"]["day"] is None and p["latest"] is None
    assert p["season_paid_udndr"] is None and p["ranked_days"] is None
    assert not any("/ranking/" in s for s in route.seen)
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    code, d = call(A, "/api/overview")
    assert code == 200 and d["programme"]["status"]["day"] is None and "estimate" not in d


def test_a_failed_programme_read_is_unread_not_zero(app, programme):
    # A failed read shown as 0 paid or 0 requests would be a real value an identity can have.
    A, _ = app
    import final_season_calc as SC
    base, route = programme
    view = lambda: A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")  # noqa: E731
    route("status", 200, status_doc(2))
    route("ranking/day-000.json", 502, b"bad gateway")
    p = view()
    assert p["season_paid_udndr"] is None and p["ranked_days"] is None and p["latest"] is None
    # a ranking that answers but is not one is not read either, and neither is the total
    good = publish(route, [[SC.Identity("dm1abc", presence=10, verified_requests=2)]])[0]
    assert view()["ranked_days"] == 1
    mine = row_of(good)
    others = [x for x in good["ranking"] if x is not mine]
    for bad in (b"{not json", b"", dict(good, day=1), dict(good, day="0"), {k: v for k, v in good.items() if k != "ranking"},
                dict(good, ranking=others + [dict(mine, paid_udndr="1200000")]),
                dict(good, ranking=others + [dict(mine, presence=10.0)]),
                dict(good, ranking=others + [dict(mine, verdicts=True)]),
                dict(good, ranking=others + [{k: v for k, v in mine.items() if k != "verified_requests"}]),
                dict(good, ranking=good["ranking"] + [mine])):
        route("ranking/day-000.json", 200, bad)
        p = view()
        assert p["season_paid_udndr"] is None and p["ranked_days"] is None and p["latest"] is None, bad
    # nothing answered at all
    p = A.programme_view({"DENDRA_FINAL_SEASON_URL": "http://127.0.0.1:9/final-season/v1"}, "dm1abc")
    assert p["status"] is None and p["latest"] is None and p["ranked_days"] is None


def test_the_latest_published_ranking_is_read_and_the_season_summed(app, programme):
    A, _ = app
    import final_season_calc as SC
    base, route = programme
    view = lambda: A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")  # noqa: E731
    d0, d1 = publish(route, [
        [SC.Identity("dm1abc", presence=10, verified_requests=2), SC.Identity("someone", presence=50, verified_requests=9)],
        [SC.Identity("dm1abc", presence=40, verified_requests=3, verdicts=1), SC.Identity("someone", verified_requests=1)]])
    route("status", 200, status_doc(5))
    p = view()
    assert p["ranked_days"] == 2 and p["season_paid_udndr"] == row_of(d0)["paid_udndr"] + row_of(d1)["paid_udndr"]
    L = p["latest"]
    assert L["day"] == 1 and L["listed"] is True and L["rules_differ"] is False
    assert (L["presence"], L["verified_requests"], L["verdicts"]) == (40, 3, 1)
    assert L["paid_udndr"] == row_of(d1)["paid_udndr"] == 238_000
    assert L["formula"]["gross_udndr"] == L["gross_udndr"] == 238_000
    # the programme's evidence is not read: the figures are the ranking's
    assert not any("evidence" in s for s in route.seen)
    # the current day is never asked for: a day is ranked only once final, past its last block
    route("ranking/day-002.json", 200, dict(d1, day=2))
    route("status", 200, status_doc(2))
    route.seen.clear()
    assert view()["ranked_days"] == 2
    assert route.seen and not any(s.endswith("/ranking/day-002.json") for s in route.seen)
    # before the season starts, no day can be ranked: a reading, and nothing is asked for
    route("status", 200, dict(status_doc(0), height=50, day=-1))
    route.seen.clear()
    p = view()
    assert p["ranked_days"] == 0 and p["season_paid_udndr"] == 0 and p["latest"] is None
    assert not any("/ranking/" in s for s in route.seen)


def test_an_identity_missing_from_a_ranking_had_no_fact_that_day(app, programme):
    # The ranking lists every identity with a verified request, a verdict or a proven window
    # (`final_season_facts.day_identities`): absent is zero, a reading, and said as not listed.
    A, _ = app
    import final_season_calc as SC
    base, route = programme
    d0, _d1 = publish(route, [[SC.Identity("dm1abc", verified_requests=4)],
                              [SC.Identity("someone", presence=50, verified_requests=9)]])
    route("status", 200, status_doc(3))
    p = A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")
    L = p["latest"]
    assert L["day"] == 1 and L["listed"] is False
    assert (L["presence"], L["verified_requests"], L["verdicts"], L["paid_udndr"]) == (0, 0, 0, 0)
    assert p["season_paid_udndr"] == row_of(d0)["paid_udndr"] == 200_000


def test_a_ranking_under_other_rules_is_shown_but_the_formula_is_not_rerun(app, programme):
    # Another copy of the rules gives another figure: the screen would call a difference of versions a
    # difference of figures.
    A, _ = app
    import final_season_calc as SC
    base, route = programme
    d0 = publish(route, [[SC.Identity("dm1abc", presence=40, verified_requests=3, verdicts=1)]])[0]
    route("ranking/day-000.json", 200, dict(d0, rules_fingerprint="0" * 64))
    route("status", 200, status_doc(1))
    L = A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")["latest"]
    assert L["rules_differ"] is True and L["formula"] is None and L["paid_udndr"] == row_of(d0)["paid_udndr"]


def test_a_day_ranked_under_an_earlier_rule_set_is_rerun_under_that_set(app, programme, monkeypatch):
    # Decision 18 added a rule set: the days ranked before it carry the fingerprint of RULES_17, not the one
    # of the rules in force. The day's set is found by its fingerprint among every set the season published
    # (`final_season_rules.rule_set_of`) and the formula is rerun under it.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    me = [SC.Identity("dm1abc", presence=40, verified_requests=3, verdicts=1, payout_address="dendra1dm1abc")]
    view = lambda: A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")["latest"]  # noqa: E731
    route("status", 200, status_doc(1))
    d0 = SC.to_json(SC.compute_day(0, me, 0, R.RULES_17))
    assert d0["rules_fingerprint"] == R.fingerprint(R.RULES_17) != R.fingerprint()
    route("ranking/day-000.json", 200, d0)
    L = view()
    assert L["day"] == 0 and L["rules_differ"] is False
    assert L["formula"]["gross_udndr"] == L["gross_udndr"] == 238_000
    # RULES_17 and the rules in force pay the same rates, so the figure alone cannot tell which set ran.
    # A set whose work rate differs, added to the published ones for this case only, does: its day must come
    # out at its rate.
    other = dict(R.RULES_17, work_per_request=2 * R.RULES_17["work_per_request"])
    monkeypatch.setattr(R, "RULE_HISTORY", R.RULE_HISTORY + (other,))
    d0 = SC.to_json(SC.compute_day(0, me, 0, other))
    route("ranking/day-000.json", 200, d0)
    L = view()
    assert L["rules_differ"] is False and L["formula"]["work_udndr"] == 3 * other["work_per_request"]
    assert L["formula"]["gross_udndr"] == L["gross_udndr"] == row_of(d0)["gross_udndr"]
    # A ranking that names no rule set is not rerun under any: absent is not "the rules in force".
    route("ranking/day-000.json", 200, {k: v for k, v in d0.items() if k != "rules_fingerprint"})
    L = view()
    assert L["rules_differ"] is True and L["formula"] is None


def test_the_page_shows_the_latest_ranked_day(app, programme, tmp_path, monkeypatch):
    # The server's reading, then the page's own script on it: the published figures, what the formula
    # makes of them, and nothing for the current day.
    A, kit = app
    import final_season_calc as SC
    base, route = programme
    publish(route, [[SC.Identity("dm1abc", presence=10, verified_requests=2)],
                    [SC.Identity("dm1abc", presence=40, verified_requests=3, verdicts=1)]])
    route("status", 200, status_doc(2))
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    ov = call(A, "/api/overview")[1]
    assert "estimate" not in ov and ov["programme"]["latest"]["day"] == 1
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    assert t["day"] == "day 3, the season ends 7 November 2026, 23:59 UTC"
    assert t["ranked"] == "2" and t["paid"] == "0.358 DNDR over 2 ranked day(s)"
    assert t["lastDay"] == "2 (ranking/day-001.json)" and t["lastPaid"] == "0.238 DNDR"
    assert (t["presence"], t["requests"], t["verdicts"]) == ("40", "3", "1")
    assert t["lastParts"] == "The formula on these facts: work 0.15 + juror 0.008 + presence 0.08 = 0.238 DNDR gross."
    # the formula is written from the rules the server sends, never restated by the page
    note = t["formulaNote"]
    for part in ("0.05 DNDR per verified programme request", "0.008 DNDR per verdict consistent with the outcome",
                 "0.002 DNDR per availability window proven on chain, at most 50 a day",
                 "only on a day with at least one verified request", "There is no cap per identity",
                 "at most 50 DNDR a day, shared pro rata beyond, and 1,500 DNDR over the season",
                 "It sends 600 requests a day"):
        assert part in note, part
    rules = dict(ov["rules"], work_per_request=700_000, presence_max_windows=40)
    note = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, rules=rules)}]})["text"]["formulaNote"]
    assert "0.7 DNDR per verified programme request" in note and "at most 40 a day" in note
    # After the end the service's day keeps counting and `ended` says the season is over: the overview hands
    # the service's reading on unchanged, the walk stops at the first day not published, and the page names
    # the last day the service found (the one holding the season's last block, here the last of day index 1).
    route("status", 200, status_doc(5, end_height=100 + 17_280 * 2 - 1))
    route.seen.clear()
    ov = call(A, "/api/overview")[1]
    st = ov["programme"]["status"]
    assert st["ended"] is True and st["last_day"] == 1 and st["end_height"] == 100 + 17_280 * 2 - 1
    assert ov["programme"]["ranked_days"] == 2 and ov["programme"]["latest"]["day"] == 1
    assert route.seen and not any(s.endswith("/ranking/day-003.json") for s in route.seen)
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    assert t["day"] == "the season is over (its last day was day 2)"
    assert t["ranked"] == "2" and t["paid"] == "0.358 DNDR over 2 ranked day(s)"
    assert t["lastDay"] == "2 (ranking/day-001.json)" and t["lastPaid"] == "0.238 DNDR"


def test_the_formula_shown_is_the_set_the_service_names_for_the_day_and_each_day_is_rerun_under_its_own(
        app, programme, tmp_path, monkeypatch):
    # Days ranked under RULES then under the doubled rates: each day's formula is rerun under the set its own
    # fingerprint names, and the note shows the set of the current day as the service's status names it. A
    # status whose fingerprint names no set this application knows shows no formula, never another set's rates.
    A, kit = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    me = [SC.Identity("dm1abc", presence=10, verified_requests=2, payout_address="dendra1dm1abc")]
    d0 = SC.to_json(SC.compute_day(0, me, 0, R.RULES))
    d1 = SC.to_json(SC.compute_day(1, me, d0["total_paid_udndr"], R.RULES_DOUBLED))
    route("ranking/day-000.json", 200, d0)
    route("ranking/day-001.json", 200, d1)
    route("ranking/day-002.json", 404, {"error": "not published"})
    route("status", 200, dict(status_doc(2), rules=R.RULES_DOUBLED, rules_fingerprint=R.fingerprint(R.RULES_DOUBLED),
                              doubled_rates_from_day=1, unwound_audit_work_from_day=0))
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    ov = call(A, "/api/overview")[1]
    assert ov["rules"] == R.RULES_DOUBLED
    L = ov["programme"]["latest"]
    assert L["day"] == 1 and L["rules_differ"] is False and L["gross_udndr"] == 2 * row_of(d0)["gross_udndr"]
    assert L["formula"]["gross_udndr"] == L["gross_udndr"] and L["formula"]["work_udndr"] == 2 * 100_000
    assert A.ranked_row(0, d0, "dm1abc")["formula"]["gross_udndr"] == row_of(d0)["gross_udndr"]
    assert ov["programme"]["season_paid_udndr"] == d0["total_paid_udndr"] + d1["total_paid_udndr"]
    note = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]["formulaNote"]
    for part in ("0.1 DNDR per verified programme request", "0.016 DNDR per verdict consistent with the outcome",
                 "0.004 DNDR per availability window proven on chain",
                 "at most 100 DNDR a day, shared pro rata beyond, and 3,000 DNDR over the season"):
        assert part in note, part
    # before the doubled day the status names RULES, and the note says their rates
    route("status", 200, status_doc(0))
    assert call(A, "/api/overview")[1]["rules"] == R.RULES
    # a set this application does not know, or no fingerprint (the service has no height yet): no formula
    for st in (dict(status_doc(2), rules_fingerprint="0" * 64), dict(status_doc(2), rules_fingerprint=None)):
        route("status", 200, st)
        ov = call(A, "/api/overview")[1]
        assert ov["rules"] is None
        assert run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]["formulaNote"] == ""


def test_the_note_says_the_daily_volume_of_the_set_the_service_names_for_the_day(app, programme, tmp_path,
                                                                                 monkeypatch):
    # Decision 23 changes `requests_per_day` only. The application reads it from the set the status's fingerprint
    # names, never from the status's own `rules`: the volume of the doubled days before the new set, the new one
    # from its first day.
    A, kit = app
    import final_season_rules as R
    base, route = programme
    route("ranking/day-000.json", 404, {"error": "not published"})
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    for day, rules in ((5, R.RULES_DOUBLED), (6, R.RULES_MORE_REQUESTS)):
        route("status", 200, dict(status_doc(day), rules=dict(rules, requests_per_day=1), unwound_audit_work_from_day=1,
                                  rules_fingerprint=R.fingerprint(rules), doubled_rates_from_day=4,
                                  more_requests_from_day=6))
        ov = call(A, "/api/overview")[1]
        assert ov["rules"] == rules
        note = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]["formulaNote"]
        assert f"It sends {rules['requests_per_day']} requests a day" in note, note


def test_the_page_says_why_a_ranked_day_paid_below_its_gross(app, programme, tmp_path, monkeypatch):
    # Presence without work, more windows than are paid, an identity not listed, a gross the formula does
    # not give, and rules the formula cannot be rerun with: each said in words, from the server's reading.
    A, kit = app
    import final_season_calc as SC
    base, route = programme
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    route("status", 200, status_doc(1))

    def page_for(identities, **change):
        doc = publish(route, [identities])[0]
        if change:
            route("ranking/day-000.json", 200, dict(doc, **change))
        return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": call(A, "/api/overview")[1]}]})["text"]

    t = page_for([SC.Identity("dm1abc", presence=30)])
    assert t["lastPaid"] == "0 DNDR" and t["presence"] == "30" and t["requests"] == "0"
    assert "presence 0 = 0 DNDR gross." in t["lastParts"]
    assert "Reason: presence not paid: no verified request counted that day." in t["lastParts"]
    t = page_for([SC.Identity("dm1abc", presence=60, verified_requests=1)])
    assert t["presence"] == "60 (at most 50 are paid)" and t["lastPaid"] == "0.15 DNDR"
    t = page_for([SC.Identity("someone", presence=50, verified_requests=9)])
    assert t["lastPaid"] == "0 DNDR" and t["presence"] == "0" and t["lastDay"] == "1 (ranking/day-000.json)"
    assert t["lastParts"].startswith("This identity is not in that day's ranking")
    doc = publish(route, [[SC.Identity("dm1abc", verified_requests=2)]])[0]
    t = page_for([SC.Identity("dm1abc", verified_requests=2)],
                 ranking=[dict(row_of(doc), gross_udndr=300_000, payable_udndr=300_000, paid_udndr=300_000)])
    assert "= 0.1 DNDR gross; the ranking states 0.3 DNDR: they differ." in t["lastParts"]
    t = page_for([SC.Identity("dm1abc", verified_requests=2)], rules_fingerprint="0" * 64)
    assert "the formula is not rerun here" in t["lastParts"] and t["lastPaid"] == "0.1 DNDR"
    # no cap per identity: a large day is paid whole
    t = page_for([SC.Identity("dm1abc", verified_requests=30)])
    assert t["lastPaid"] == "1.5 DNDR" and "Gross" not in t["lastParts"] and "Reason" not in t["lastParts"]
    # the pro rata is the ranking's, shown with its reason: 30 identities grossing 2.1 DNDR ask 63 a day
    t = page_for([SC.Identity(m, presence=50, verified_requests=40) for m in ["dm1abc"] + [f"dm1x{n:02d}" for n in range(29)]])
    assert "Gross 2.1 DNDR, payable 2.1 DNDR, paid 1.666666 DNDR." in t["lastParts"]
    assert "Reason: daily budget shared pro rata." in t["lastParts"]


def test_a_miner_without_the_programme_address_is_told_what_it_costs(app, tmp_path, monkeypatch):
    # The Final Testnet Season counts what the chain records whether or not the kit names the programme:
    # without its address nothing failed to be read, and the screen does not call the miner outside the
    # season. It says what the setting is for: reading the rankings here, and naming where the rewards go.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    no_docker(A, monkeypatch)
    ov = call(A, "/api/overview")[1]
    assert ov["programme"]["enabled"] is False and ov["programme"]["latest"] is None
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    assert "DENDRA_FINAL_SEASON_URL" in t["day"] and "?" not in t["day"]
    assert t["lastParts"].startswith(
        "The Final Testnet Season counts what the chain records whether or not this setting is there")
    assert t["ranked"] == t["paid"] == t["lastPaid"] == "—"


def test_the_page_shows_the_new_key_s_phrase_after_a_refused_confirmation(tmp_path):
    # The key changed while the old phrase was on screen: the confirmation was refused (409) and the old
    # words stayed, the page never asking for the new ones until it was reloaded.
    view = {"installed": True, "containers": [], "docker_error": None, "identity": {}, "chain": {},
            "programme": {"enabled": False}, "recovery_confirmed": False}
    # Since R73 the words take a confirmation code: after a 409 the old words go, and the new key's words need a
    # new code, which the page asks for again rather than fetching them.
    first = {"present": True, "address": "dendra1aaa", "words": WORDS, "ask": [1, 2, 3]}
    second = {"present": True, "address": "dendra1bbb", "words": OTHER, "ask": [4, 5, 6]}
    typed = ["type", {"1": WORDS[0], "2": WORDS[1], "3": WORDS[2]}]
    shown = [["fill", "recoveryCode", "ABCD-EFGH"], ["click", "showWordsBtn"]]
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}],
                           "/api/recovery": [{"status": 200, "data": first}, {"status": 200, "data": second}],
                           "/api/recovery/confirm": [{"status": 409, "data": {"error": "no phrase to confirm"}}]},
                shown + [typed, ["click", "confirmBtn"]])
    assert ui["words"] == [] and ui["hidden"]["recovery"] is False and ui["hidden"]["recoveryGate"] is False
    assert "key changed" in ui["text"]["recoveryMsg"] and "new code" in ui["text"]["recoveryMsg"]
    assert [c["path"] for c in ui["calls"]].count("/api/recovery") == 1
    # wrong words keep the same phrase on screen, with the reason
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}],
                           "/api/recovery": [{"status": 200, "data": first}, {"status": 200, "data": second}],
                           "/api/recovery/confirm": [{"status": 400, "data": {"error": "those words do not match"}}]},
                shown + [typed, ["click", "confirmBtn"]])
    assert ui["words"] == WORDS and ui["text"]["recoveryMsg"] == "those words do not match"


def test_rest_base_from_the_kit_settings(app):
    A, _ = app
    assert A.rest_base({"DENDRA_NODE": "tcp://host.docker.internal:26657"}) == "http://127.0.0.1:1317"
    assert A.rest_base({"DENDRA_NODE": "tcp://api.example:26657"}) == "http://api.example:1317"
    # What join.sh writes for a miner on its own node: the node's alias on the dendra-chain network, which
    # resolves inside the container only. From this host it is the node kit's loopback publish.
    assert A.rest_base({"DENDRA_NODE": "tcp://dendra-node:26657"}) == "http://127.0.0.1:1317"
    # The rule is the shape, not a list: a second node's project name is an alias too.
    assert A.rest_base({"DENDRA_NODE": "tcp://second-node_2:26657"}) == "http://127.0.0.1:1317"
    # A dotted address is never rewritten, whether a name or an IPv4 address.
    assert A.rest_base({"DENDRA_NODE": "tcp://203.0.113.9:26657"}) == "http://203.0.113.9:1317"


def test_the_exit_panel_shows_the_command_and_runs_nothing(app, tmp_path):
    # Leaving the network is irreversible and the chain refuses it while an obligation is open, for a time
    # no button can promise: the screen shows the registration, the stake and the command, and nothing
    # on it can deregister the miner.
    A, kit = app
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={"miner_id": "dm1abc"}, containers=[],
              docker_error=None, chain={"registered": True, "stake_udndr": 1500000}, recovery_confirmed=True,
              exit_command=A.exit_command())
    assert A.exit_command().endswith(os.path.join("deploy", "testnet-miner", "exit-miner.sh"))
    assert A.exit_command().startswith("bash ") and "--yes" not in A.exit_command()
    for route in ("/api/exit", "/api/delete-miner", "/api/leave"):
        assert call(A, route, body={})[0] == 404, route
    page = call(A, "/", token=False)[1].decode("utf-8")
    # No control of the page names leaving: the only buttons are the ones that were already there.
    assert not re.search(r"<button[^>]*>[^<]*(leave|exit|deregister|delete)", page, re.I)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
    t = ui["text"]
    assert t["exitState"] == "yes" and t["exitStake"] == "1.5 DNDR" and t["exitCmd"] == A.exit_command()
    assert not [c for c in ui["calls"] if c["method"] == "POST"]
    # An unread registration is "not read", never "no"; a reading of "not registered" says there is nothing.
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, chain={})}]})["text"]
    assert t["exitState"] == "not read" and t["exitStake"] == "not read"
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, chain={"registered": False})}]})["text"]
    assert t["exitState"] == "no: there is nothing to leave" and t["exitStake"] == "—"


def test_in_owner_mode_the_exit_card_says_the_stake_goes_back_to_the_owner(app, tmp_path):
    # Owner mode: the chain returns the stake to the owner, --yes only prepares the delete-miner the owner
    # signs, and a restart prints the owner's create-miner -- the card said the opposite of all three. Read as
    # exit-miner.sh reads it: from the chain (the registration's creator), else from the kit's setting.
    A, kit = app
    ident = {"miner_id": "dm1abc", "address": "dendra1machine"}
    owned = A.exit_notes({}, ident, {"creator": "dendra1owner", "registered": True})
    assert owned["owner"] == "dendra1owner"
    assert "dendra1owner" in owned["after"] and "never to this machine's key" in owned["after"]
    assert "this miner's address" not in owned["after"] and "registers it again" not in owned["after"]
    assert "--yes --signed" in owned["how"] and "delete-miner the owner signs" in owned["how"]
    # the chain not read: the kit's setting says it
    assert A.exit_notes({"DENDRA_MINER_OWNER": "dendra1owner"}, ident, {"creator": None})["owner"] == "dendra1owner"
    # registered by this machine's own key, or no owner anywhere: the card as before
    for env, ch in (({}, {"creator": "dendra1machine"}), ({}, {"creator": None})):
        plain = A.exit_notes(env, ident, ch)
        assert plain["owner"] == "" and "returns its remaining stake to this miner's address" in plain["after"]
        assert "add --yes to leave" in plain["how"]


def test_the_exit_card_shows_the_servers_text(app, tmp_path):
    A, kit = app
    ident = {"miner_id": "dm1abc", "address": "dendra1machine"}
    owned = A.exit_notes({}, ident, {"creator": "dendra1owner", "registered": True})
    ov = dict(call(A, "/api/overview")[1], installed=True, identity=ident, containers=[], docker_error=None,
              chain={"registered": True, "stake_udndr": 1500000}, recovery_confirmed=None,
              exit_command=A.exit_command(), exit_notes=owned)
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    assert t["exitAfter"] == owned["after"] and t["exitHow"] == owned["how"]


def test_the_removal_of_the_phrase_is_asked_with_yes(app, monkeypatch):
    # The module removes nothing without --yes; the application passes it, and only from the confirmation.
    A, _ = app
    seen = []
    monkeypatch.setattr(A, "miner_exec", lambda *a, **k: seen.append(a) or (0, '{"removed": true}'))
    assert A.forget_recovery("dendra1x") == (True, "")
    [argv] = seen
    assert argv[:4] == ("python3", "-m", "modea.keyring", "forget-recovery") and "--yes" in argv
    assert argv[argv.index("--address") + 1] == "dendra1x"


# -- the miner's self-test (deploy/testnet-miner/miner_health.sh) -----------------------------------
# The screen reads miner-health.last.json, which the hourly job writes, and runs a quick check on demand.
# It never decides health itself: no recorded run is "never", a file that is not the document is
# "unread", and neither is shown as fine.
# The period the documents of this bench carry. Not the script's value, and deliberately unlike it: what is
# pinned here is that the application applies the period the document names -- an application that kept its
# own copy of the script's period would judge these documents against the wrong one, and fail. That the
# script's SCHEDULE_PERIOD_S is the period of the crontab line join.sh writes is checked elsewhere
# (the miner-health bench of the development repository).
SCHEDULE_S = 1234


def health_doc(rc=0, epoch=None, **over):
    d = {"schema": 1, "tool": "miner_health", "generated_at": "2026-10-08T10:00:00Z",
         "generated_epoch": int(time.time()) - 600 if epoch is None else epoch, "schedule_period_s": SCHEDULE_S,
         "rc": rc, "quick": False, "note": "",
         "summary": {"ok": 13, "ko": 1 if rc == 1 else 0, "unmeasured": 1 if rc == 2 else 0, "checks": 14},
         "host": [{"id": "H1", "name": "containers", "state": "ok", "measured": "miner: running", "reason": "", "fix": ""}],
         "selftest": {"checks": [{"id": "C4", "name": "availability (presence)", "state": "ko" if rc == 1 else "ok",
                                  "measured": "ABSENT", "reason": "", "fix": "docker compose -p dendra-miner logs miner"}]}}
    d.update(over)
    return d


def test_health_without_a_recorded_run_is_never_and_never_ok(app):
    A, kit = app
    assert call(A, "/api/health", token=False)[0] == 401
    code, h = call(A, "/api/health")
    assert code == 200 and h["state"] == "never" and "rc" not in h and "summary" not in h


def test_a_malformed_last_run_is_unread(app):
    A, kit = app
    last = kit / "miner-health.last.json"
    last.write_text("{this is not json")
    assert call(A, "/api/health")[1]["state"] == "unread"
    for bad in (dict(health_doc(), rc="0"), dict(health_doc(), rc=7), dict(health_doc(), generated_epoch=None),
                {k: v for k, v in health_doc().items() if k != "summary"},
                dict(health_doc(), summary={"ok": 1, "ko": 0, "checks": 1}), ["not", "an", "object"]):
        last.write_text(json.dumps(bad))
        assert call(A, "/api/health")[1]["state"] == "unread", bad


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_last_run_that_cannot_be_opened_is_unread_never_never(app):
    # The file exists and is not readable: that is not "no check has run" (never) -- it is a reading that failed.
    A, kit = app
    last = kit / "miner-health.last.json"
    last.write_text(json.dumps(health_doc()))
    os.chmod(last, 0)
    try:
        h = call(A, "/api/health")[1]
    finally:
        os.chmod(last, 0o600)
    assert h["state"] == "unread" and "could not be read" in h["why"], h


def test_the_age_of_the_last_run_is_computed_and_a_stale_one_is_said(app, monkeypatch):
    A, kit = app
    no_docker(A, monkeypatch)
    last = kit / "miner-health.last.json"
    last.write_text(json.dumps(health_doc(epoch=int(time.time()) - 600)))
    h = call(A, "/api/health")[1]
    assert h["state"] == "read" and 590 <= h["age_s"] <= 700 and h["stale"] is False and h["rc"] == 0
    assert h["period_s"] == SCHEDULE_S
    last.write_text(json.dumps(health_doc(epoch=int(time.time()) - 3 * SCHEDULE_S)))
    assert call(A, "/api/health")[1]["stale"] is True
    # The period comes from the document, never from a copy kept in the application: a document that does
    # not carry it (an older script), or carries it in another type, is judged neither stale nor fresh.
    old = {k: v for k, v in health_doc(epoch=int(time.time()) - 3 * SCHEDULE_S).items() if k != "schedule_period_s"}
    for d in (old, dict(old, schedule_period_s=str(SCHEDULE_S)), dict(old, schedule_period_s=0)):
        last.write_text(json.dumps(d))
        h = call(A, "/api/health")[1]
        assert h["state"] == "read" and h["stale"] is None and h["period_s"] is None, d
    # ... and a period twice as long makes the same age fresh: the document's value is the one applied.
    last.write_text(json.dumps(health_doc(epoch=int(time.time()) - 3 * SCHEDULE_S, schedule_period_s=2 * SCHEDULE_S)))
    assert call(A, "/api/health")[1]["stale"] is False
    # Both halves of the document reach the screen; a state that is none of the three words is unmeasured.
    doc = health_doc(rc=1)
    doc["host"][0]["state"] = "fine"
    last.write_text(json.dumps(doc))
    h = call(A, "/api/health")[1]
    assert {c["id"]: c["state"] for c in h["checks"]} == {"H1": "unmeasured", "C4": "ko"}
    # and the overview carries the same reading
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    assert call(A, "/api/overview")[1]["health"]["rc"] == 1


def test_run_now_needs_the_token_and_runs_the_quick_check(app):
    A, kit = app
    assert call(A, "/api/health/run", body={}, token=False)[0] == 401
    assert call(A, "/api/health/run", body={})[0] == 404, "no script in this kit: said, not faked"
    (kit / "miner_health.sh").write_text(
        "#!/usr/bin/env bash" + NL + 'printf "%s " "$@" > "$(dirname "$0")/args"' + NL +
        "echo 'progress on the way'" + NL + "echo '" + json.dumps(health_doc(rc=2)) + "'" + NL + "exit 2" + NL)
    code, d = call(A, "/api/health/run", body={})
    assert code == 200 and d["rc"] == 2 and d["health"]["state"] == "read" and d["health"]["rc"] == 2
    # The self-test's own deadline goes down with it, shorter than this screen's wait: killing the process
    # group here reaches the script and the docker client, never what runs inside the container.
    assert (kit / "args").read_text().split() == ["--json", "--quick", "--deadline", str(A.HEALTH_RUN_DEADLINE_S)]
    assert 1 <= A.HEALTH_RUN_DEADLINE_S < A.HEALTH_RUN_TIMEOUT_S


def test_run_now_is_bounded_in_time_and_one_at_a_time(app, monkeypatch):
    A, kit = app
    (kit / "miner_health.sh").write_text("#!/usr/bin/env bash" + NL + "sleep 30" + NL)
    monkeypatch.setattr(A, "HEALTH_RUN_TIMEOUT_S", 1)
    t0 = time.monotonic()
    code, d = call(A, "/api/health/run", body={})
    assert code == 504 and "1 s" in d["error"] and time.monotonic() - t0 < 10
    assert A._HEALTH_RUN.acquire(blocking=False)
    try:
        assert call(A, "/api/health/run", body={})[0] == 409
    finally:
        A._HEALTH_RUN.release()


def test_the_health_card_says_never_shows_what_is_wrong_and_runs_a_quick_check(app, tmp_path):
    A, _ = app
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=True)
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health={
        "state": "never", "why": "no check has run on this machine yet"})}]})["text"]
    assert t["healthState"] == "never checked" and t["hOk"] == "—" and "no check has run" in t["healthWhen"]
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    assert t["healthState"] == "not read", "an overview without the reading is not a healthy miner"
    read = A._health_from(health_doc(rc=1))
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=read)}]})
    t = ui["text"]
    assert t["healthState"] == "a problem" and t["hKo"] == "1 ko" and t["hOk"] == "13 ok"
    assert "FAIL C4 availability (presence): ABSENT" in t["healthList"] and "fix: docker compose" in t["healthList"]
    assert ui["hidden"]["healthList"] is False and "min ago" in t["healthWhen"]
    stale = A._health_from(health_doc(rc=0, epoch=int(time.time()) - 3 * SCHEDULE_S))
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=stale)}]})["text"]
    assert "has not run for two of its periods" in t["healthWhen"]
    no_period = A._health_from({k: v for k, v in health_doc(rc=0).items() if k != "schedule_period_s"})
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=no_period)}]})["text"]
    assert "its age is not judged" in t["healthWhen"] and "two of its periods" not in t["healthWhen"]
    quick = A._health_from(health_doc(rc=0, epoch=int(time.time()), quick=True))
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=read)}],
                           "/api/health/run": [{"status": 200, "data": {"rc": 0, "health": quick}}]},
                [["click", "healthBtn"]])
    t = ui["text"]
    assert {"path": "/api/health/run", "method": "POST"} in ui["calls"]
    assert t["healthState"] == "all checks ok" and t["healthWhen"].startswith("quick check") and ui["hidden"]["healthList"]


def test_the_self_test_s_advice_is_shown_apart_and_never_colours_the_verdict(app, tmp_path):
    # Keys unencrypted, the phrase still on the machine, the season paying the machine's key: choices, listed
    # apart with what changes them. A run whose checks are all ok stays "all checks ok".
    A, _ = app
    adv = [{"id": "A1", "name": "keys at rest", "measured": "the keyring is the test keyring",
            "fix": "bash deploy/testnet-miner/encrypt-keys.sh"},
           {"id": "A3", "name": "Final Testnet Season payout", "measured": "no payout address is declared",
            "fix": "bash deploy/join.sh --payout-address dendra1..."}]
    doc = health_doc(rc=0)
    doc["selftest"]["checks"][0]["state"] = "ok"
    doc["selftest"]["advice"] = adv + ["not an object"]
    h = A._health_from(doc)
    assert h["rc"] == 0 and [a["id"] for a in h["advice"]] == ["A1", "A3"]
    assert {c["id"]: c["state"] for c in h["checks"]} == {"H1": "ok", "C4": "ok"}
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=None, health=h)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
    t = ui["text"]
    assert t["healthState"] == "all checks ok" and ui["hidden"]["healthList"] is True
    assert ui["hidden"]["healthAdvice"] is False and "A1 keys at rest" in t["healthAdvice"]
    assert "do: bash deploy/testnet-miner/encrypt-keys.sh" in t["healthAdvice"]
    # a document without advice shows none, and its absence is not an error
    h = A._health_from(health_doc(rc=0))
    assert h["advice"] == []


# -- the judge role off (D3 b), the header pill (B8), the update instruction (A10), the subsidy (A11) ---------
# miner_health.sh reads the judge role in the container and carries it (`judge_role`), with its own advice A4 when
# the role is off; the screen's Role line comes from that reading, never from the kit's setting alone. The header
# pill is the state's level and word (STATUS_RULES), never a rule of its own. The update instruction is the line
# deploy/join.sh wrote into the kit's .env, read and never built here.
SILENT = {"id": "A4", "name": "judge role off", "measured": "miner -- judge role off: this identity declares no judge, and its daemon withdraws one the chain still holds",
          "fix": "nothing, if this machine only mines"}


def test_the_role_is_the_container_s_reading_and_a_silent_seat_is_said(app, tmp_path, monkeypatch):
    A, kit = app
    doc = health_doc(rc=0, judge_role={"word": "off", "why": "the judge role is not requested"}, advice=[SILENT])
    doc["selftest"]["checks"][0]["state"] = "ok"
    doc["selftest"]["advice"] = [{"id": "A1", "name": "keys at rest", "measured": "test keyring", "fix": "encrypt"}]
    h = A._health_from(doc)
    # the script's advice first, then the self-test's; neither moves the verdict
    assert h["rc"] == 0 and [a["id"] for a in h["advice"]] == ["A4", "A1"]
    assert h["judge_role"] == {"word": "off", "why": "the judge role is not requested"}
    # a word that is none of the four, or no field at all, is not read: never "off", never "active"
    for bad in ({"word": "offf"}, "off", None, {"why": "x"}):
        assert A._health_from(dict(doc, judge_role=bad))["judge_role"] is None, bad
    # the overview: the role read in the container, and the setting said apart as what was requested
    no_docker(A, monkeypatch)
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MINER_JUDGE=1" + NL)
    (kit / "miner-health.last.json").write_text(json.dumps(doc))
    r = call(A, "/api/overview")[1]["role"]
    assert r["word"] == "off" and r["requested"] is True and isinstance(r["age_s"], int)
    (kit / "miner-health.last.json").unlink()
    r = call(A, "/api/overview")[1]["role"]
    assert r["word"] is None and r["requested"] is True, "a check never run says nothing of the role"
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=None)
    for role, want, never in ((dict(word="off", why="", requested=False), "miner — not a juror (the judge is off: its daemon withdraws a declaration the chain holds", "miner and juror"),
                              (dict(word="active", why="", requested=True), "miner and juror", "silent"),
                              (dict(word="mute", why="", requested=True), "mute juror seat", "miner and juror"),
                              (dict(word=None, why="", requested=True), "whether it judges was not read", "miner and juror"),
                              (dict(word="unknown", why="x", requested=False), "not a juror (the judge role is not requested", "miner and juror")):
        t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, role=role)}]})["text"]
        assert want in t["role"] and never not in t["role"], (role, t["role"])
    # an older server sends no `role`: its setting is a request, never shown as a juror that votes
    old = {k: v for k, v in ov.items() if k != "role"}
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(old, judge=True)}]})["text"]
    assert "whether it judges was not read" in t["role"] and t["role"] != "miner and juror"


def test_the_header_pill_is_the_state_card_s_level_and_word_from_one_table(app, tmp_path):
    # The pill had rules of its own (the container and the last check): it stayed green over a red card when the
    # kit was refused or the node was behind while the checks were fresh. It is now the level and the word of the
    # state, which STATUS_RULES decides once.
    A, _ = app
    by_id = {c[0]: c[1] for c in scenarios(A)}
    fresh = A._health_from(health_doc(rc=0, epoch=int(time.time()) - 60))
    running = [{"service": "miner", "state": "running", "status": "Up"}]
    pill = {"green": "pill ok", "orange": "pill warn", "red": "pill bad"}
    cases = [("green", facts_of(A))] + [(rid, by_id[rid]) for rid in ("kit_refused", "node_behind", "stopped", "phrase", "no_report")]
    levels = set()
    for name, facts in cases:
        st = A.status_view(facts)
        levels.add(st["level"])
        # the container runs and the last check is fresh and ok in every case: the pill follows the state alone
        ov = dict(_page_overview(A, status=st), health=fresh, containers=running)
        ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
        assert (ui["text"]["state"], ui["cls"]["state"]) == (st["word"], pill[st["level"]]), (name, ui["text"]["state"])
        assert ui["text"]["statusWord"] == st["word"] and ui["cls"]["statusWord"].endswith(pill[st["level"]][5:]), name
    assert levels == {"green", "orange", "red"}
    # Docker did not answer: the pill is the state's red, and the container line says why
    st = A.status_view(by_id["docker"])
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(_page_overview(A, status=st), docker_error="Docker did not answer: x")}]})
    assert (ui["text"]["state"], ui["cls"]["state"]) == ("Act now", "pill bad") and ui["text"]["ctr"] == "Docker did not answer: x"
    # a server that sends no state (an older one) is "state not read", never green, whatever its health says
    old = dict(_page_overview(A), health=fresh, containers=running)
    old.pop("status", None)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": old}]})
    assert (ui["text"]["state"], ui["cls"]["state"]) == ("state not read", "pill warn")


def test_a_quick_check_redraws_the_pill_from_the_server_s_state_never_from_its_own_verdict(app, tmp_path):
    # The server reads a quick check run here as the newest report (newest_report), so the page asks the overview
    # again after one; a quick check that is all ok leaves the pill orange while the server says the hourly
    # schedule is old (a dead cron), and turns it green only when the server says green.
    A, _ = app
    quick = A._health_from(health_doc(rc=0, epoch=int(time.time()), quick=True))
    never = A.status_view(facts_of(A, schedule={"state": "never", "why": "no check has run on this machine yet"}, report=None))
    old = A.status_view(facts_of(A, age=5 * SCHEDULE_S))
    green = A.status_view(facts_of(A))
    assert (never["level"], old["level"], green["level"]) == ("orange", "orange", "green")

    def after_click(second):
        ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(_page_overview(A, status=never), health={"state": "never", "why": "x"})},
                                                 {"status": 200, "data": dict(_page_overview(A, status=second), health=quick)}],
                               "/api/health/run": [{"status": 200, "data": {"rc": 0, "health": quick}}]}, [["click", "healthBtn"]])
        paths = [c["path"] for c in ui["calls"]]
        assert "/api/health/run" in paths and "/api/overview" in paths[paths.index("/api/health/run") + 1:], paths
        return ui["text"]["state"], ui["cls"]["state"]

    assert after_click(green) == ("All good", "pill ok")
    assert after_click(old) == ("Needs attention", "pill warn")


def test_the_update_instruction_is_the_kit_s_line_read_never_built(app, tmp_path, monkeypatch):
    A, kit = app
    no_docker(A, monkeypatch)
    upd = ("cd /the/clone && git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main, "
           "then re-run CONFIG_URL=http://ni.example/n.txt bash /the/clone/deploy/join.sh --judge (with the same options)")
    rerun = "CONFIG_URL=http://ni.example/n.txt bash /the/clone/deploy/join.sh --judge"
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_KIT_UPDATE=" + upd + NL + "DENDRA_KIT_RERUN=" + rerun + NL)
    assert call(A, "/api/overview")[1]["update"] == {"instruction": upd, "rerun": rerun}
    # a line edited into something join.sh does not write is not shown; no line at all is shown as not recorded
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + 'DENDRA_KIT_UPDATE=cd /x && rm -rf "$HOME"' + NL)
    assert call(A, "/api/overview")[1]["update"] == {"instruction": None, "rerun": None}
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=None)
    assert ov["update"] == {"instruction": None, "rerun": None}
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    assert t["kitUpdate"].startswith("not recorded by the deploy/join.sh")
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, update={"instruction": upd, "rerun": rerun})}]})["text"]
    assert t["kitUpdate"] == upd


def test_the_subsidy_is_said_accrued_and_claimable_only_from_a_funded_pool(app):
    with open(os.path.join(APP, "ui", "index.html"), encoding="utf-8") as f:
        page = f.read()
    assert "Subsidy to claim" not in page and "claims its subsidy by itself when there is some" not in page
    assert "<dt>Subsidy accrued</dt>" in page and "emission work pool" in page
    assert "msg_server_claim_subsidy.go::ClaimSubsidy" in page and "miner.py::SUBSIDY_MIN" in page


# -- one identity per card (deploy/join.sh --gpus) -------------------------------------------------------
# The kit is fabricated with the shipped slot library (deploy/testnet-miner/slots.sh, found from this file,
# never from the subject's directory: a mutated copy of the application has no kit next to it), slot 0's
# .env, a slot 1 and a retired slot 2. `docker` is a script on PATH that records each argv and the variables
# it was handed: the application's command lines are run through the real slots.sh, never matched as text.
def _shipped(*parts):
    d = os.path.dirname(os.path.abspath(__file__))
    while d != os.path.dirname(d) and not os.path.isfile(os.path.join(d, "deploy", *parts)):
        d = os.path.dirname(d)
    return os.path.join(d, "deploy", *parts)


SLOTS_SH = _shipped("testnet-miner", "slots.sh")
U1 = "GPU-bbbb1111-1111-4111-8111-111111111111"
U2 = "GPU-cccc2222-2222-4222-8222-222222222222"
FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
printf 'MINER_ID=%s COMPOSE_PROFILES=%s COMPOSE_PROJECT_NAME=%s\\n' "${MINER_ID-unset}" "${COMPOSE_PROFILES-unset}" "${COMPOSE_PROJECT_NAME-unset}" >> "$FAKE_LOG.env"
case " $* " in *" ps "*) echo '{"Service":"miner","State":"running","Status":"Up"}' ;; esac
exit 0
"""


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """(A, kit, log): the application over a kit that runs one identity per card."""
    if not os.path.isfile(SLOTS_SH) or not _WHICH("bash"):
        pytest.skip("the shipped slots.sh or bash is not here: the slot cases are not run (declared, not passed)")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    kit = tmp_path / "kit"
    (kit / "gpu" / "1").mkdir(parents=True)
    (kit / "gpu" / "2").mkdir(parents=True)
    shutil.copy(SLOTS_SH, kit / "slots.sh")
    (kit / "docker-compose.yml").write_text("services:" + NL + "  miner:" + NL + "    image: x" + NL +
                                            "    command: [\"${MINER_ID}\"]" + NL)
    (kit / ".env").write_text("MINER_ID=dm1slot0" + NL + "DENDRA_MINER_JUDGE=1" + NL + "COMPOSE_PROFILES=judge" + NL)
    for k, u, extra in ((1, U1, ""), (2, U2, "DENDRA_SLOT_RETIRED=2026-10-01T00:00:00Z" + NL)):
        (kit / "gpu" / str(k) / "override.yml").write_text("")
        (kit / "gpu" / str(k) / ".env").write_text(
            f"DENDRA_SLOT={k}{NL}DENDRA_GPU_UUID={u}{NL}COMPOSE_PROJECT_NAME=dendra-miner-g{k}{NL}"
            f"COMPOSE_FILE=docker-compose.yml:gpu/{k}/override.yml{NL}MINER_ID=dm1slot{k}{NL}DENDRA_MINER_JUDGE=1{NL}" + extra)
    b = tmp_path / "bin"
    b.mkdir()
    (b / "docker").write_text(FAKE_DOCKER)
    os.chmod(b / "docker", 0o755)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PATH", str(b) + os.pathsep + os.environ.get("PATH", ""))
    # What join.sh exports, and what an operator's shell may carry: a slot k never takes them.
    monkeypatch.setenv("MINER_ID", "CANARIEXP")
    monkeypatch.setenv("COMPOSE_PROFILES", "judge")
    monkeypatch.setenv("DENDRA_MINER_KIT", str(kit))
    import dendra_app as A
    importlib.reload(A)
    srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    A.APP = A.App(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield A, kit, log
    srv.shutdown()


def _argv(log):
    return log.read_text().splitlines() if log.exists() else []


def test_the_identities_are_listed_by_the_slot_library(rig):
    A, kit, log = rig
    rows, why = A.slots()
    assert why is None and [(r["slot"], r["project"], r["state"]) for r in rows] == [
        (0, "dendra-miner", "active"), (1, "dendra-miner-g1", "active"), (2, "dendra-miner-g2", "retired")]
    assert rows[1]["uuid"] == U1 and rows[1]["dir"] == str(kit / "gpu" / "1") and rows[0]["dir"] == str(kit)
    # A list that cannot be read is reported, never an empty machine.
    os.chmod(kit / "gpu", 0)
    try:
        rows, why = A.slots()
    finally:
        os.chmod(kit / "gpu", 0o700)
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        assert rows is None and "could not be listed" in why


def test_a_slot_k_command_goes_through_slots_sh_and_carries_nothing_of_slot_0(rig):
    A, kit, log = rig
    rc, out = A.compose("ps", "-a", "--format", "json", slot=1)
    [line] = _argv(log)
    assert rc == 0 and line == ("compose -p dendra-miner-g1 --env-file gpu/1/.env -f docker-compose.yml "
                                "-f gpu/1/override.yml ps -a --format json")
    # never the judge's profile for a slot k, though its role is judge: the engine is slot 0's
    assert "--profile" not in line
    # and the variables of the process are not handed to it (the env file decides)
    assert (log.parent / "docker.log.env").read_text().splitlines() == [
        "MINER_ID=unset COMPOSE_PROFILES=unset COMPOSE_PROJECT_NAME=unset"]


def test_slot_0_takes_its_project_and_identity_from_its_env_file_never_from_the_process(rig, monkeypatch):
    # A slot k's env sourced in the shell that started the application: its project would rename slot 0
    # (another key volume) and its MINER_ID would give slot 0 another identity. The kit's .env decides.
    A, kit, log = rig
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "dendra-miner-g1")
    A.compose("ps")
    assert _argv(log) == ["compose ps"]
    assert (log.parent / "docker.log.env").read_text().splitlines() == [
        "MINER_ID=unset COMPOSE_PROFILES=unset COMPOSE_PROJECT_NAME=unset"]
    # what the kit's files do not interpolate, and is not a COMPOSE_*, still reaches docker
    assert "PATH" in A.slot0_compose_env(A.kit_env()) and "MINER_ID" not in A.slot0_compose_env(A.kit_env())


def test_slot_0_runs_as_it_always_did(rig):
    A, kit, log = rig
    A.compose("ps")
    # A current kit: its .env names the profile, so none is added (and no project name: the directory form).
    assert _argv(log) == ["compose ps"]
    # An older kit, whose .env has no COMPOSE_PROFILES line: the judge's profile is named, for slot 0 only.
    (kit / ".env").write_text("MINER_ID=dm1slot0" + NL + "DENDRA_MINER_JUDGE=1" + NL)
    A.compose("ps")
    A.compose("ps", slot=1)
    lines = _argv(log)
    assert lines[1] == "compose --profile judge ps" and "--profile" not in lines[2]


def test_an_action_names_its_identity_and_an_unknown_one_is_refused(rig):
    A, kit, log = rig
    for body in ({}, {"slot": ""}, {"slot": 9}, {"slot": "x"}, {"slot": "01"}, {"slot": -1}):
        code, d = call(A, "/api/start", body=body)
        assert code == 400 and "error" in d, body
    assert _argv(log) == [], "nothing ran, and never slot 0 by default"
    code, d = call(A, "/api/start", body={"slot": 1})
    assert code == 200 and d["rc"] == 0
    assert _argv(log)[-1].startswith("compose -p dendra-miner-g1 --env-file gpu/1/.env") and _argv(log)[-1].endswith(" up -d --no-build")
    # all = every active identity: slot 0 and slot 1, never the retired slot 2
    open(log, "w").close()
    code, d = call(A, "/api/restart", body={"slot": "all"})
    assert code == 200 and [r["slot"] for r in d["results"]] == [0, 1]
    assert not [x for x in _argv(log) if "dendra-miner-g2" in x]
    # a retired slot is never started: slots.sh refuses it, and the screen shows the refusal
    code, d = call(A, "/api/start", body={"slot": 2})
    assert code == 200 and d["rc"] == 2 and "the slot is retired" in d["text"]
    for path in ("/api/logs", "/api/health", "/api/recovery"):
        assert call(A, path)[0] == 400, path
        assert call(A, path + "?slot=9")[0] == 400, path


def test_the_overview_is_one_identity_and_lists_them_all(rig, monkeypatch):
    A, kit, log = rig
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    (kit / "gpu" / "1" / "miner-health.last.json").write_text(json.dumps(health_doc(rc=1, advice=None)))
    code, ov = call(A, "/api/overview")
    assert code == 200 and ov["slot"] == 0 and ov["multi"] is True
    assert [s["slot"] for s in ov["slots"]] == [0, 1, 2] and ov["slots"][1]["health"]["rc"] == 1
    assert ov["exit_command"].endswith("exit-miner.sh --slot 0"), "on a machine of several, slot 0 is named too"
    assert ov["slots"][2]["exit_command"].endswith("exit-miner.sh --slot 2")
    code, ov1 = call(A, "/api/overview?slot=1")
    assert code == 200 and ov1["slot"] == 1 and ov1["health"]["rc"] == 1
    assert any("dendra-miner-g1" in x for x in _argv(log)) and ov1["exit_command"].endswith("--slot 1")
    assert call(A, "/api/overview?slot=7")[0] == 400
    code, h = call(A, "/api/health?slot=1")
    assert code == 200 and h["rc"] == 1


def test_the_quick_check_names_the_identity_on_a_machine_of_several(rig):
    A, kit, log = rig
    (kit / "miner_health.sh").write_text(
        "#!/usr/bin/env bash" + NL + 'printf "%s " "$@" > "$(dirname "$0")/args"' + NL +
        "echo '" + json.dumps(health_doc(rc=0)) + "'" + NL)
    assert call(A, "/api/health/run", body={})[0] == 400
    code, d = call(A, "/api/health/run", body={"slot": 1})
    assert code == 200 and (kit / "args").read_text().split()[-2:] == ["--slot", "1"]


def test_one_payout_address_for_every_identity_each_signing_its_own(rig, monkeypatch):
    A, kit, log = rig
    ran = []
    monkeypatch.setattr(A, "miner_identity", lambda slot=0: {"miner_id": f"dm1slot{slot}", "address": f"dendra1m{slot}"})
    monkeypatch.setattr(A, "miner_exec", lambda *a, slot=0, **k: ran.append((slot, a)) or (0, "ok"))
    good = "dendra1" + "q" * 38
    code, d = call(A, "/api/payout", body={"address": good, "slot": "all"})
    assert code == 200 and [r["slot"] for r in d["results"]] == [0, 1] and all(r["rc"] == 0 for r in d["results"])
    assert [(s, a[a.index("--miner") + 1], a[-1]) for s, a in ran] == [(0, "dm1slot0", good), (1, "dm1slot1", good)]
    assert A.settings()["payout_address"] == good
    ran.clear()
    assert call(A, "/api/payout", body={"address": good, "slot": 1})[0] == 200 and [s for s, _ in ran] == [1]
    ran.clear()
    assert call(A, "/api/payout", body={"address": good})[0] == 400 and not ran


def test_a_payout_for_every_identity_that_stop_interrupted_says_each_one_s_state(rig, monkeypatch):
    # Stop ends the first identity's exec: its declaration may be on record or not, and the second is not sent.
    A, kit, log = rig
    ran = []
    monkeypatch.setattr(A, "miner_identity", lambda slot=0: {"miner_id": f"dm1slot{slot}", "address": f"dendra1m{slot}"})

    def interrupted(*a, slot=0, **k):
        ran.append(slot)
        A.APP.stop_asked = True                    # what take_stop sets while this action runs
        return A.INTERRUPTED_RC, A.INTERRUPTED_TEXT
    monkeypatch.setattr(A, "miner_exec", interrupted)
    code, d = call(A, "/api/payout", body={"address": "dendra1" + "q" * 38, "slot": "all"})
    assert code == 409 and d["interrupted"] is True and ran == [0], d
    assert [(r["slot"], r["declared"]) for r in d["results"]] == [(0, None), (1, False)], d
    assert "payout_address" not in A.settings()


def test_each_identity_s_phrase_is_confirmed_on_its_own(rig, monkeypatch):
    A, kit, log = rig
    phrases = {0: ("dendra1zero", WORDS), 1: ("dendra1one", OTHER)}
    removed = []
    monkeypatch.setattr(A, "recovery_phrase", lambda slot=0: {"present": True, "address": phrases[slot][0], "words": list(phrases[slot][1])})
    monkeypatch.setattr(A, "forget_recovery", lambda addr, slot=0: removed.append((slot, addr)) or (True, ""))
    code, r1 = call(A, "/api/recovery?slot=1")
    assert code == 200 and r1["slot"] == 1 and r1["address"] == "dendra1one"
    words1 = {str(i): OTHER[i - 1] for i in r1["ask"]}
    # the positions were drawn for slot 1's phrase: they prove nothing about slot 0's
    assert call(A, "/api/recovery/confirm", body={"slot": 0, "words": words1})[0] == 409
    code, r1 = call(A, "/api/recovery?slot=1")
    words1 = {str(i): OTHER[i - 1] for i in r1["ask"]}
    assert call(A, "/api/recovery/confirm", body={"slot": 1, "words": words1})[0] == 200
    code, r0 = call(A, "/api/recovery?slot=0")
    words0 = {str(i): WORDS[i - 1] for i in r0["ask"]}
    assert call(A, "/api/recovery/confirm", body={"slot": 0, "words": words0})[0] == 200
    assert removed == [(1, "dendra1one"), (0, "dendra1zero")]
    s = A.settings()
    # both confirmations are kept: encrypt-keys.sh of slot 1 must not read its phrase as never written down
    assert s["recovery_confirmed_address"] == "dendra1zero" and s["recovery_confirmed_addresses"] == ["dendra1one", "dendra1zero"]


def test_the_page_selects_an_identity_and_every_call_names_it(rig, tmp_path):
    A, kit, log = rig
    base = dict(call(A, "/api/overview")[1], identity={"miner_id": "dm1slot0"}, containers=[], docker_error=None,
                chain={}, recovery_confirmed=None, health={"state": "never", "why": "x"})
    ov1 = dict(base, slot=1, identity={"miner_id": "dm1slot1"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": base}],
                           "/api/overview?slot=1": [{"status": 200, "data": ov1}],
                           "/api/start": [{"status": 200, "data": {"rc": 0, "text": ""}}],
                           "/api/logs?slot=1": [{"status": 200, "data": {"rc": 0, "text": "log of slot 1"}}]},
                [["select", "slotSel", "1"], ["click", "startBtn"], ["click", "logsBtn"], ["click", "payoutAllBtn"]])
    assert ui["hidden"]["slotsCard"] is False and ui["hidden"]["payoutAllBtn"] is False
    assert "slot 2  dendra-miner-g2  retired" in ui["text"]["slotsList"]
    assert {"path": "/api/overview?slot=1", "method": "GET"} in ui["calls"] and ui["text"]["mid"] == "dm1slot1"
    assert {"path": "/api/start", "body": {"slot": 1}} in ui["bodies"]
    assert ui["text"]["logs"] == "log of slot 1"
    assert {"path": "/api/payout", "body": {"address": "", "code": "", "slot": "all"}} in ui["bodies"]
    # one identity on the machine: the list stays hidden and the calls are the ones they were
    one = dict(base, multi=False, slots=[base["slots"][0]])
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": one}],
                           "/api/start": [{"status": 200, "data": {"rc": 0, "text": ""}}]}, [["click", "startBtn"]])
    assert ui["hidden"]["slotsCard"] is True and ui["hidden"]["payoutAllBtn"] is True
    assert {"path": "/api/start", "body": {}} in ui["bodies"]


# --- the kit banner: the daemon's kit_behind / kit_latest, read from its heartbeat -------------------------
# Three answers, never two. The reader the application ships is run for real (in_container) against a
# heartbeat file the case writes, so a missing file, a missing field and a null are three different inputs.
def _kit_overview(A, kit, tmp_path, monkeypatch, heartbeat):
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_KIT_UPDATE=git pull and run deploy/join.sh again" + NL)
    hb = tmp_path / "heartbeat.json"
    if heartbeat is not None:
        hb.write_text(heartbeat)
    _, _, run = in_container(tmp_path, env={"DENDRA_STATUS_FILE": str(hb)})
    monkeypatch.setattr(A, "miner_exec", run)
    monkeypatch.setattr(A, "containers", lambda: ([], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1aaa"})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    return call(A, "/api/overview")[1]


def test_the_overview_reads_kit_behind_in_three_states(app, tmp_path, monkeypatch):
    A, kit = app
    behind = _kit_overview(A, kit, tmp_path, monkeypatch, json.dumps({"kit_behind": True, "kit_latest": "7"}))
    assert behind["kit"] == {"behind": True, "latest": "7", "why": ""}
    current = _kit_overview(A, kit, tmp_path, monkeypatch, json.dumps({"kit_behind": False, "kit_latest": "7"}))
    assert current["kit"]["behind"] is False and current["kit"]["latest"] == "7"
    # not read, four ways: a null, a heartbeat older than the field, no heartbeat file, a heartbeat that is no JSON.
    # None of them is false.
    for text in (json.dumps({"kit_behind": None, "kit_latest": None}), json.dumps({"schema": 1}), None, "not json"):
        d = _kit_overview(A, kit, tmp_path, monkeypatch, text)["kit"]
        assert d["behind"] is None and d["why"].startswith(A.KIT_NOT_READ), (text, d)
    # a value that is not a boolean is not "behind": only the JSON true raises the banner
    assert _kit_overview(A, kit, tmp_path, monkeypatch, json.dumps({"kit_behind": "true"}))["kit"]["behind"] is None


def test_the_page_shows_the_update_banner_only_when_behind_and_never_reads_null_as_current(app, tmp_path):
    A, _ = app
    base = dict(call(A, "/api/overview")[1], installed=True, identity={"miner_id": "dm1abc"}, containers=[],
                docker_error=None, chain={}, recovery_confirmed=None,
                update={"instruction": "git pull and run deploy/join.sh again", "rerun": None})
    page = call(A, "/", token=False)[1].decode("utf-8")
    assert 'id="kitBanner"' in page and 'id="kitCheck"' in page

    def shown(kit):
        data = dict(base) if kit is None else dict(base, kit=kit)
        return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": data}]})

    ui = shown({"behind": True, "latest": "7", "why": ""})
    assert ui["hidden"]["kitBanner"] is False
    assert "Kit 7 is published" in ui["text"]["kitBannerText"] and "git pull and run deploy/join.sh again" in ui["text"]["kitBannerText"]
    ui = shown({"behind": False, "latest": "7", "why": ""})
    assert ui["hidden"]["kitBanner"] is True and ui["text"]["kitCheck"] == "matches the published kit"
    for kit in ({"behind": None, "latest": None, "why": "version check not read: the daemon has not compared its kit with the network's yet"},
                None):  # None: an older server that sends no `kit` at all
        ui = shown(kit)
        assert ui["hidden"]["kitBanner"] is True
        assert ui["text"]["kitCheck"].startswith("version check not read") and "matches" not in ui["text"]["kitCheck"]


# --- the two switches: Judge and GPU mining ---
# What is replaced: docker (each command line is logged and answers 0, or fails on purpose on the words FAKE_FAIL
# names), the kit's judge.sh (it logs its verb and edits the .env as the real one does, or fails with FAKE_JUDGE_RC)
# and the hardware probe (FAKE_CAN_JUDGE, FAKE_PROBE_RC, FAKE_FLOOR). What is real: the HTTP routes, the reading of
# the kit's .env, the greying and its reasons, the order of the steps, the .env edit and its rollback. The shipped
# probe, the shipped compose file, the shipped serve_guard.py and the shipped judge.sh are run by the cases after.
FAKE_SW_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
if [ -n "${FAKE_FAIL:-}" ]; then case "$*" in *"$FAKE_FAIL"*) echo "failed on purpose" >&2; exit 1 ;; esac; fi
# FAKE_ONEOFF names a running one-off container of this kit, as `compose run` labels it.
case "$*" in
  "ps -q "*"oneoff=True"*) [ -z "${FAKE_ONEOFF:-}" ] || echo "$FAKE_ONEOFF" ;;
  "inspect "*) echo "$DENDRA_MINER_KIT" ;;
esac
exit 0
"""
FAKE_JUDGE_SH = """#!/usr/bin/env bash
echo "judge.sh $1" >> "$FAKE_LOG"
# judge.sh on starts the engine and pulls the model before it writes the .env: FAKE_JUDGE_HANG holds it there.
[ -z "${FAKE_JUDGE_HANG:-}" ] || sleep "$FAKE_JUDGE_HANG"
f="$DENDRA_MINER_KIT/.env"
rc="${FAKE_JUDGE_RC:-0}"
if [ "$rc" = 0 ]; then
  grep -v -e "^DENDRA_MINER_JUDGE=" -e "^COMPOSE_PROFILES=" "$f" > "$f.t"; cat "$f.t" > "$f"; rm -f "$f.t"
  if [ "$1" = on ]; then echo "DENDRA_MINER_JUDGE=1" >> "$f"; echo "COMPOSE_PROFILES=judge" >> "$f"; fi
  if [ "$1" = off ]; then echo "DENDRA_MINER_JUDGE=0" >> "$f"; fi
fi
echo "[judge] fake $1"
exit "$rc"
"""
FAKE_PROBE = """#!/usr/bin/env bash
echo "probe $*" >> "$FAKE_LOG"
if [ "$1" = --judge-floor-mb ]; then echo "${FAKE_FLOOR:-26000}"; exit 0; fi
rc="${FAKE_PROBE_RC:-0}"
[ "$rc" = 0 ] || exit "$rc"
if [ "${FAKE_CAN_JUDGE:-true}" = absent ]; then echo '{"ram_mb":16000}'; exit 0; fi
printf '{"ram_mb":16000,"can_judge":%s}' "${FAKE_CAN_JUDGE:-true}"
"""
JUDGE_MODEL = "qwen3:30b-a3b-instruct-2507-q4_K_M"
SW_ENV = NL.join(["# Generated by deploy/join.sh (kept as written)", "MINER_ID=dm1abc", "DENDRA_MODEL_ID=mistral-nemo:12b",
                  "DENDRA_EMBED_API_MODEL=nomic-embed-text", "DENDRA_RELAY_TOKEN=keep-me",
                  "DENDRA_JUDGE_MODEL_ID=" + JUDGE_MODEL, "DENDRA_MINER_JUDGE=0", ""])


@pytest.fixture()
def sw(app, tmp_path, monkeypatch):
    """(A, kit, log): the application over a kit with judge.sh, a probe and docker replaced."""
    if not _WHICH("bash"):
        pytest.skip("bash is not here: the switch cases are not run (declared, not passed)")
    A, kit = app
    b = tmp_path / "swbin"
    b.mkdir()
    (b / "docker").write_text(FAKE_SW_DOCKER)
    os.chmod(b / "docker", 0o755)
    (kit / "judge.sh").write_text(FAKE_JUDGE_SH)
    probe = tmp_path / "probe" / "hw_probe.sh"
    probe.parent.mkdir()
    probe.write_text(FAKE_PROBE)
    monkeypatch.setattr(A, "HW_PROBE", str(probe))
    log = tmp_path / "sw.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PATH", str(b) + os.pathsep + os.environ.get("PATH", ""))
    for v in ("FAKE_FAIL", "FAKE_JUDGE_RC", "FAKE_PROBE_RC", "FAKE_CAN_JUDGE", "FAKE_FLOOR", "WSL_DISTRO_NAME",
              "FAKE_JUDGE_HANG", "FAKE_ONEOFF"):
        monkeypatch.delenv(v, raising=False)
    (kit / ".env").write_text(SW_ENV)
    os.chmod(kit / ".env", 0o600)
    no_docker(A, monkeypatch)
    monkeypatch.setattr(A, "kit_reading", lambda slot=0: (
        {"behind": None, "latest": None, "why": A.KIT_NOT_READ},
        {"mine": None, "min": None, "deadline": None, "read": None, "why": A.KIT_NOT_READ}))
    monkeypatch.setattr(A, "judge_memory_view", lambda env, slot=0: {"state": "unread", "why": "judge memory not read"})
    monkeypatch.setattr(A, "capacity_round_view", lambda slot=0: {"state": "unread", "why": "capacity not read"})
    A._PROBE.update(at=0.0, view=None)
    return A, kit, log


def _sw(A):
    A._PROBE.update(at=0.0, view=None)
    return call(A, "/api/overview")[1]["switches"]


def _lines(log, prefix=""):
    return [ln for ln in (log.read_text().splitlines() if log.exists() else []) if ln.startswith(prefix)]


def test_both_switches_are_greyed_with_the_probe_s_reason_and_run_nothing(sw, monkeypatch, tmp_path):
    A, kit, log = sw
    monkeypatch.setenv("FAKE_CAN_JUDGE", "false")
    monkeypatch.setenv("FAKE_FLOOR", "31337")
    s = _sw(A)
    assert s["can_judge"]["answer"] == "false"
    assert s["judge"] == {"on": False, "enabled": False, "why": s["can_judge"]["why"]}
    assert s["gpu"]["on"] is True and s["gpu"]["enabled"] is False and s["can_judge"]["why"] in s["gpu"]["why"]
    why = s["judge"]["why"]
    # the floor is the probe's (31337 here), named by its variable, with the RAM the probe read
    assert "MOE_CPU_MIN_RAM_MB" in why and "31337 MB" in why and "16000 MB" in why and "can_judge false" in why
    for path, on in (("/api/judge", True), ("/api/gpu", False)):
        code, body = call(A, path, {"on": on})
        assert code == 409 and "cannot judge" in body["error"], (path, body)
    assert _lines(log, "judge.sh") == [] and _lines(log, "docker") == []
    # unknown, three ways, greys both switches too and is never read as true
    for env, rc, said in (("null", "0", "could not read this machine's RAM"), ("absent", "0", "no readable can_judge"),
                          ("true", "3", "no readable can_judge")):
        monkeypatch.setenv("FAKE_CAN_JUDGE", env)
        monkeypatch.setenv("FAKE_PROBE_RC", rc)
        s = _sw(A)
        assert s["can_judge"]["answer"] == "unknown" and said in s["can_judge"]["why"], (env, rc, s)
        assert s["judge"]["enabled"] is False and s["gpu"]["enabled"] is False
    monkeypatch.setattr(A, "HW_PROBE", str(tmp_path / "nowhere" / "hw_probe.sh"))
    s = _sw(A)
    assert s["can_judge"]["answer"] == "unknown" and "not in this clone" in s["judge"]["why"]
    assert _lines(log, "judge.sh") == []


def test_the_judge_switch_calls_judge_sh_on_and_off(sw, monkeypatch):
    A, kit, log = sw
    s = _sw(A)
    assert s["judge"] == {"on": False, "enabled": True, "why": ""} and s["gpu"]["enabled"] is True
    for bad in ({}, {"on": "yes"}, {"on": 1}):
        assert call(A, "/api/judge", bad)[0] == 400, bad
    assert call(A, "/api/judge", {"on": True}, token=False)[0] == 401
    code, body = call(A, "/api/judge", {"on": True})
    assert code == 200 and body["rc"] == 0 and "[judge] fake on" in body["text"]
    assert _lines(log, "judge.sh") == ["judge.sh on"] and A.kit_env()["DENDRA_MINER_JUDGE"] == "1"
    s = _sw(A)
    assert s["judge"] == {"on": True, "enabled": True, "why": ""}
    code, body = call(A, "/api/judge", {"on": True})
    assert code == 409 and "already on" in body["error"]
    code, body = call(A, "/api/judge", {"on": False})
    assert code == 200 and _lines(log, "judge.sh") == ["judge.sh on", "judge.sh off"]
    # judge.sh's own refusal or failure is shown with its output, never "done"
    monkeypatch.setenv("FAKE_JUDGE_RC", "2")
    code, body = call(A, "/api/judge", {"on": True})
    assert code == 502 and body["rc"] == 2 and "fake on" in body["text"]
    # a judge that turns off with the probe saying false: off needs no gate
    monkeypatch.delenv("FAKE_JUDGE_RC")
    (kit / ".env").write_text(SW_ENV.replace("DENDRA_MINER_JUDGE=0", "DENDRA_MINER_JUDGE=1"))
    monkeypatch.setenv("FAKE_CAN_JUDGE", "false")
    s = _sw(A)
    assert s["judge"]["on"] is True and s["judge"]["enabled"] is True and s["gpu"]["enabled"] is False


def test_the_card_off_seats_the_judge_serves_on_the_cpu_frees_the_card_and_comes_back(sw):
    A, kit, log = sw
    code, body = call(A, "/api/gpu", {"on": False})
    assert code == 200 and body["rc"] == 0 and body["note"] == "", body
    assert _lines(log, "judge.sh") + _lines(log, "docker") == [
        "judge.sh on",
        "docker compose --profile judge up -d --no-deps ollama-cpu",
        "docker compose --profile judge exec -T ollama-cpu ollama pull nomic-embed-text",
        "docker compose --profile judge up -d --no-deps --force-recreate ollama-cpu miner",
        "docker compose stop ollama"]
    env = A.kit_env()
    assert (env["DENDRA_SERVE_ENDPOINT"], env["DENDRA_SERVE_MODEL_ID"], env["DENDRA_CPU_KEEP_ALIVE"],
            env["DENDRA_CPU_NUM_PARALLEL"]) == ("http://ollama-cpu:11434", JUDGE_MODEL, "1h", "2")
    text = (kit / ".env").read_text()
    assert text.startswith("# Generated by deploy/join.sh (kept as written)") and "DENDRA_RELAY_TOKEN=keep-me" in text
    assert env["DENDRA_MODEL_ID"] == "mistral-nemo:12b", "the card's model stays recorded for the way back"
    assert oct(os.stat(kit / ".env").st_mode & 0o777) == "0o600"
    s = _sw(A)
    assert s["gpu"] == {"on": False, "enabled": True, "why": ""}
    assert s["judge"]["on"] is True and s["judge"]["enabled"] is False and "Turn GPU mining on first" in s["judge"]["why"]
    code, body = call(A, "/api/judge", {"on": False})
    assert code == 409 and "Turn GPU mining on first" in body["error"] and _lines(log, "judge.sh") == ["judge.sh on"]
    assert call(A, "/api/gpu", {"on": False})[0] == 409
    log.write_text("")
    code, body = call(A, "/api/gpu", {"on": True})
    assert code == 200 and _lines(log) == [
        "docker compose up -d --no-deps ollama",
        "docker compose --profile judge up -d --no-deps --force-recreate ollama-cpu miner"]
    env = A.kit_env()
    assert not any(k in env for k in ("DENDRA_SERVE_ENDPOINT", "DENDRA_SERVE_MODEL_ID", "DENDRA_CPU_KEEP_ALIVE",
                                      "DENDRA_CPU_NUM_PARALLEL")) and env["DENDRA_RELAY_TOKEN"] == "keep-me"
    s = _sw(A)
    assert s["gpu"] == {"on": True, "enabled": True, "why": ""} and s["judge"]["enabled"] is True


def test_a_failed_step_leaves_the_card_on_and_the_env_as_it_was(sw, monkeypatch):
    A, kit, log = sw
    # judge.sh on refused: nothing else runs
    monkeypatch.setenv("FAKE_JUDGE_RC", "2")
    code, body = call(A, "/api/gpu", {"on": False})
    assert code == 502 and "could not be seated" in body["error"] and _lines(log, "docker") == []
    monkeypatch.delenv("FAKE_JUDGE_RC")
    seated = SW_ENV.replace("DENDRA_MINER_JUDGE=0", "DENDRA_MINER_JUDGE=1" + NL + "COMPOSE_PROFILES=judge")
    # no embedder named: refused before any docker call, no default put in its place
    (kit / ".env").write_text(seated.replace("DENDRA_EMBED_API_MODEL=nomic-embed-text" + NL, ""))
    code, body = call(A, "/api/gpu", {"on": False})
    assert code == 409 and "DENDRA_EMBED_API_MODEL" in body["error"] and _lines(log, "docker") == []
    # the pull fails: the .env is untouched and the miner is not recreated
    (kit / ".env").write_text(seated)
    monkeypatch.setenv("FAKE_FAIL", "pull")
    code, body = call(A, "/api/gpu", {"on": False})
    assert code == 502 and "could not be pulled" in body["error"] and (kit / ".env").read_text() == seated
    assert not [ln for ln in _lines(log, "docker") if "--force-recreate" in ln]
    # the restart fails: the .env goes back as it was, and the miner is recreated on it
    monkeypatch.setenv("FAKE_FAIL", "--force-recreate")
    log.write_text("")
    code, body = call(A, "/api/gpu", {"on": False})
    assert code == 502 and "back as it was" in body["error"] and (kit / ".env").read_text() == seated
    assert len([ln for ln in _lines(log) if "--force-recreate" in ln]) == 2 and "stop ollama" not in log.read_text()
    assert _sw(A)["gpu"]["on"] is True
    # and on the way back: the lines stay when the card's engine does not start
    monkeypatch.delenv("FAKE_FAIL")
    assert call(A, "/api/gpu", {"on": False})[0] == 200
    off = (kit / ".env").read_text()
    monkeypatch.setenv("FAKE_FAIL", "up -d --no-deps ollama")
    code, body = call(A, "/api/gpu", {"on": True})
    assert code == 502 and (kit / ".env").read_text() == off and _sw(A)["gpu"]["on"] is False


def test_the_switches_are_greyed_on_the_cpu_role_on_several_cards_and_on_an_unread_kit(sw, monkeypatch):
    A, kit, log = sw
    (kit / "docker-compose.override.yml").write_text("# Generated by deploy/join.sh: the judge role on the CPU" + NL)
    s = _sw(A)
    assert s["judge"]["on"] is True and s["judge"]["enabled"] is False and "whole role" in s["judge"]["why"]
    assert s["gpu"]["on"] is False and s["gpu"]["enabled"] is False and "no usable card" in s["gpu"]["why"]
    assert call(A, "/api/gpu", {"on": True})[0] == 409 and call(A, "/api/judge", {"on": False})[0] == 409
    (kit / "docker-compose.override.yml").write_text("services: {}" + NL)
    assert _sw(A)["judge"]["enabled"] is True
    # a served engine this screen does not write: unknown, never off; turning the card on is the way back
    (kit / ".env").write_text(SW_ENV.replace("DENDRA_MINER_JUDGE=0", "DENDRA_MINER_JUDGE=1") +
                              "DENDRA_SERVE_ENDPOINT=http://elsewhere:11434" + NL + "DENDRA_SERVE_MODEL_ID=" + JUDGE_MODEL + NL)
    s = _sw(A)
    assert s["gpu"]["on"] is None and s["gpu"]["enabled"] is True and "does not write" in s["gpu"]["why"]
    assert s["judge"]["enabled"] is False and "Turn GPU mining on first" in s["judge"]["why"]
    # the right engine with a model that is not the judge's: serve_guard.py would refuse it, so not "off" either
    (kit / ".env").write_text(SW_ENV + "DENDRA_SERVE_ENDPOINT=http://ollama-cpu:11434" + NL + "DENDRA_SERVE_MODEL_ID=mistral-nemo:12b" + NL)
    assert _sw(A)["gpu"]["on"] is None
    # a judge setting that is neither 0 nor 1
    (kit / ".env").write_text(SW_ENV.replace("DENDRA_MINER_JUDGE=0", "DENDRA_MINER_JUDGE=yes"))
    s = _sw(A)
    assert s["judge"]["on"] is None and s["judge"]["enabled"] is False and "neither 0 nor 1" in s["judge"]["why"]
    # no judge.sh in the kit
    (kit / ".env").write_text(SW_ENV)
    (kit / "judge.sh").unlink()
    s = _sw(A)
    assert not s["judge"]["enabled"] and not s["gpu"]["enabled"] and "no judge.sh" in s["gpu"]["why"]
    # one identity per card: the slot is named, and both switches stay greyed
    monkeypatch.setattr(A, "slots", lambda: ([{"slot": 0, "project": "dendra-miner", "state": "active", "uuid": "",
                                               "miner_id": "dm1abc", "dir": str(kit)},
                                              {"slot": 1, "project": "dendra-miner-g1", "state": "active", "uuid": "",
                                               "miner_id": "dm1b", "dir": str(kit / "gpu" / "1")}], None))
    assert call(A, "/api/gpu", {"on": False})[0] == 400
    code, body = call(A, "/api/gpu", {"on": False, "slot": 0})
    assert code == 409 and "one identity per card" in body["error"]
    assert _lines(log, "judge.sh") == [] and _lines(log, "docker") == []


def test_slot_0_left_with_its_card_off_on_several_cards_offers_only_the_way_back(sw, monkeypatch):
    A, kit, log = sw
    # GPU mining turned off on one identity, then deploy/join.sh --gpus kept that .env: slot 0 still serves on the CPU
    off = (SW_ENV.replace("DENDRA_MINER_JUDGE=0", "DENDRA_MINER_JUDGE=1" + NL + "COMPOSE_PROFILES=judge")
           + "DENDRA_SERVE_ENDPOINT=http://ollama-cpu:11434" + NL + "DENDRA_SERVE_MODEL_ID=" + JUDGE_MODEL + NL
           + "DENDRA_CPU_KEEP_ALIVE=1h" + NL + "DENDRA_CPU_NUM_PARALLEL=2" + NL)
    (kit / ".env").write_text(off)
    monkeypatch.setattr(A, "slots", lambda: ([{"slot": 0, "project": "dendra-miner", "state": "active", "uuid": "",
                                               "miner_id": "dm1abc", "dir": str(kit)},
                                              {"slot": 1, "project": "dendra-miner-g1", "state": "active", "uuid": "",
                                               "miner_id": "dm1b", "dir": str(kit / "gpu" / "1")}], None))
    s = A.switches_view(A.kit_env(), True)
    assert s["gpu"]["on"] is False and s["gpu"]["enabled"] is True, s
    assert "gives slot 0 its card back" in s["gpu"]["why"] and "cannot be turned off here" in s["gpu"]["why"]
    assert s["judge"]["enabled"] is False and "turn it on first" in s["judge"]["why"]
    # another slot named: refused, since both switches act on slot 0's kit
    code, body = call(A, "/api/gpu", {"on": True, "slot": 1})
    assert code == 409 and "act on slot 0" in body["error"]
    # lines this screen does not write: unknown, and still only the way back (never _gpu_off)
    (kit / ".env").write_text(off.replace("http://ollama-cpu:11434", "http://elsewhere:11434"))
    s = A.switches_view(A.kit_env(), True)
    assert s["gpu"]["on"] is None and s["gpu"]["enabled"] is True and "does not write" in s["gpu"]["why"]
    code, body = call(A, "/api/gpu", {"on": False, "slot": 0})
    assert code == 409 and "can only be turned on" in body["error"]
    assert _lines(log, "judge.sh") == [] and _lines(log, "docker") == []
    (kit / ".env").write_text(off)
    code, body = call(A, "/api/gpu", {"on": True, "slot": 0})
    assert code == 200 and _lines(log) == [
        "docker compose up -d --no-deps ollama",
        "docker compose --profile judge up -d --no-deps --force-recreate ollama-cpu miner"], body
    env = A.kit_env()
    assert not any(k in env for k in A.GPU_OFF_KEYS) and env["DENDRA_RELAY_TOKEN"] == "keep-me"
    s = A.switches_view(env, True)
    assert s["gpu"]["on"] is True and s["gpu"]["enabled"] is False and "bound to its card" in s["gpu"]["why"]
    assert "turn it on first" not in s["judge"]["why"]


def test_one_switch_at_a_time(sw):
    A, kit, log = sw
    assert A._SWITCH_RUN.acquire(blocking=False)
    try:
        code, body = call(A, "/api/judge", {"on": True})
    finally:
        A._SWITCH_RUN.release()
    assert code == 409 and "already being changed" in body["error"] and _lines(log, "judge.sh") == []


def test_stop_during_judge_on_stops_the_judge_s_engine_and_its_model_pull(sw, monkeypatch):
    # judge.sh on starts the judge's CPU engine and pulls its model (a one-off `compose run`) before it writes
    # COMPOSE_PROFILES=judge to the .env. A Stop that interrupts it there (R74) ran a plain `compose stop` under
    # that .env, which reaches neither: the engine and the pull went on after Stop said the miner was stopped.
    import concurrent.futures
    A, kit, log = sw
    monkeypatch.setattr(A, "STOP_GRACE_S", 0.5)
    monkeypatch.setenv("FAKE_JUDGE_HANG", "30")
    monkeypatch.setenv("FAKE_ONEOFF", "0123abcd")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        switch = pool.submit(call, A, "/api/judge", {"on": True})
        deadline = time.monotonic() + 10
        while not (A.APP.children and _lines(log, "judge.sh")) and time.monotonic() < deadline:
            _SLEEP(0.05)
        code, body = call(A, "/api/stop", {})
        switched = switch.result(timeout=15)
    assert code == 200 and body["rc"] == 0 and body["interrupted"] == "the Judge switch", body
    assert switched[1]["rc"] == A.INTERRUPTED_RC and "COMPOSE_PROFILES" not in A.kit_env(), switched
    docker = _lines(log, "docker")
    assert "docker compose --profile judge stop" in docker, docker
    assert any(ln.startswith("docker ps -q") and "service=judge-model-init" in ln and "oneoff=True" in ln
               for ln in docker), docker
    assert "docker rm -f 0123abcd" in docker and "removed" in body["text"], (docker, body)


def test_a_stop_that_interrupted_no_switch_looks_for_no_model_pull(sw):
    # Only a switch starts the pull: any other Stop leaves one-off containers alone (exit-miner.sh and
    # encrypt-keys.sh run theirs by hand), and still names the judge's profile for slot 0.
    A, kit, log = sw
    code, body = call(A, "/api/stop", {})
    assert code == 200 and body["interrupted"] is None, body
    assert _lines(log, "docker") == ["docker compose --profile judge stop"]


def test_the_page_shows_both_switches_greys_them_with_the_reason_and_sends_the_position(app, tmp_path):
    A, _ = app
    base = dict(call(A, "/api/overview")[1], installed=True, identity={"miner_id": "dm1abc"}, containers=[],
                docker_error=None, chain={}, recovery_confirmed=None)
    grey = {"judge": {"on": False, "enabled": False, "why": "this machine cannot judge (x)"},
            "gpu": {"on": True, "enabled": False, "why": "with the card off it has to be able to judge: x"}, "can_judge": None}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(base, switches=grey)}]})
    assert ui["disabled"]["judgeBtn"] is True and ui["disabled"]["gpuBtn"] is True
    assert ui["text"]["judgeWhy"] == "Judge: this machine cannot judge (x)." and "has to be able to judge" in ui["text"]["gpuWhy"]
    assert ui["text"]["judgeState"] == "off" and ui["text"]["gpuState"].startswith("on")
    assert ui["text"]["judgeBtn"] == "Turn the judge on" and ui["text"]["gpuBtn"] == "Turn GPU mining off"
    # an older server sends no switches: both greyed, not read
    old = {k: v for k, v in base.items() if k != "switches"}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": old}]})
    assert ui["disabled"]["judgeBtn"] and ui["disabled"]["gpuBtn"] and ui["text"]["gpuState"] == "not read"
    # enabled: the click sends the opposite of the position shown, as a position
    live = {"judge": {"on": True, "enabled": True, "why": ""}, "gpu": {"on": False, "enabled": True, "why": ""}, "can_judge": None}
    replies = {"/api/overview": [{"status": 200, "data": dict(base, switches=live)}],
               "/api/judge": [{"status": 200, "data": {"rc": 0, "text": "", "note": ""}}],
               "/api/gpu": [{"status": 502, "data": {"rc": 1, "error": "the miner did not start on the card"}}]}
    ui = run_ui(tmp_path, replies, steps=[["click", "judgeBtn"]])
    assert {"path": "/api/judge", "body": {"on": False}} in ui["bodies"] and ui["text"]["switchMsg"] == "done"
    ui = run_ui(tmp_path, replies, steps=[["click", "gpuBtn"]])
    assert {"path": "/api/gpu", "body": {"on": True}} in ui["bodies"]
    assert ui["text"]["switchMsg"].startswith("not done: the miner did not start on the card")
    assert ui["text"]["gpuBtn"] == "Turn GPU mining on" and ui["text"]["gpuState"].startswith("off")
    # a greyed switch sends nothing when clicked
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(base, switches=grey)}]}, steps=[["click", "gpuBtn"]])
    assert not [c for c in ui["calls"] if c["path"] in ("/api/gpu", "/api/judge")]


def test_the_shipped_probe_decides_and_its_floor_is_read_from_it(app, monkeypatch):
    A, _ = app
    probe = _shipped("hw_probe.sh")
    if not os.path.isfile(probe) or not _WHICH("bash"):
        pytest.skip("the shipped hw_probe.sh or bash is not here (declared, not passed)")
    monkeypatch.setattr(A, "HW_PROBE", probe)
    monkeypatch.delenv("DENDRA_MOE_CPU_MIN_RAM_MB", raising=False)
    r = _RUN(["bash", probe, "--can-judge"], capture_output=True, text=True, timeout=120)
    A._PROBE.update(at=0.0, view=None)
    v = A.can_judge_view()
    if r.returncode == 3:
        assert v["answer"] == "unknown"
        return
    assert r.returncode == 0 and v["answer"] == r.stdout.strip(), (r.stdout, v)
    # a default raised past this machine's RAM: the probe says false, and the reason carries the probe's number
    monkeypatch.setenv("DENDRA_MOE_CPU_MIN_RAM_MB", "987654321")
    A._PROBE.update(at=0.0, view=None)
    v = A.can_judge_view()
    assert v["answer"] == "false" and "987654321 MB" in v["why"] and "MOE_CPU_MIN_RAM_MB" in v["why"], v


def test_the_shipped_compose_serves_the_card_or_the_cpu_as_the_env_says_and_serve_guard_accepts_it(sw, tmp_path):
    A, kit, log = sw
    compose_file = _shipped("testnet-miner", "docker-compose.yml")
    docker = shutil.which("docker", path=os.environ.get("PATH", "").split(os.pathsep, 1)[-1])
    if not docker or not os.path.isfile(compose_file):
        pytest.skip("docker compose or the shipped compose file is not here (declared, not passed)")
    sys.path.insert(0, SERVICES)
    import serve_guard

    def config():
        r = _RUN([docker, "compose", "-f", compose_file, "--env-file", str(kit / ".env"), "--profile", "judge",
                  "config", "--format", "json"], capture_output=True, text=True, timeout=120,
                 env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
        if r.returncode != 0 and "unknown flag" in r.stderr:
            pytest.skip("this docker compose has no config --format json (declared, not passed)")
        assert r.returncode == 0, r.stderr[-2000:]
        s = json.loads(r.stdout)["services"]
        return s["miner"]["environment"], s["ollama-cpu"].get("environment") or {}

    (kit / ".env").write_text(SW_ENV.replace("DENDRA_MINER_JUDGE=0", "DENDRA_MINER_JUDGE=1"))
    m, c = config()
    assert (m["OLLAMA_ENDPOINT"], m["OLLAMA_MODEL"], m["DENDRA_MODEL_ID"]) == ("http://ollama:11434", "mistral-nemo:12b", "mistral-nemo:12b")
    assert c.get("OLLAMA_KEEP_ALIVE", "") == "" and c.get("OLLAMA_NUM_PARALLEL", "") == ""
    # the lines the switch writes, read by the shipped file: every process of the miner sees the CPU and the judge model
    A.env_edit({"DENDRA_SERVE_ENDPOINT": A.CPU_ENGINE, "DENDRA_SERVE_MODEL_ID": JUDGE_MODEL,
                "DENDRA_CPU_KEEP_ALIVE": A.CPU_KEEP_ALIVE, "DENDRA_CPU_NUM_PARALLEL": A.CPU_NUM_PARALLEL})
    m, c = config()
    assert (m["OLLAMA_ENDPOINT"], m["OLLAMA_MODEL"], m["DENDRA_MODEL_ID"]) == ("http://ollama-cpu:11434", JUDGE_MODEL, JUDGE_MODEL)
    assert (c["OLLAMA_KEEP_ALIVE"], c["OLLAMA_NUM_PARALLEL"]) == ("1h", "2")
    assert A.gpu_setting(A.kit_env()) == (False, "")
    # the model on the CPU (size_vram 0): the shipped serve_guard.py lets the miner start in the judge role only
    on_cpu = lambda endpoint, model, wait_s: (19 * 10 ** 9, 0)  # noqa: E731
    assert serve_guard.decide(dict(m), measure=on_cpu) == serve_guard.OK
    assert serve_guard.decide(dict(m, DENDRA_MINER_JUDGE="0"), measure=on_cpu) == serve_guard.REFUSED


def test_judge_sh_off_refuses_while_the_card_is_off(tmp_path):
    judge = _shipped("testnet-miner", "judge.sh")
    if not os.path.isfile(judge) or not os.path.isfile(SLOTS_SH) or not _WHICH("bash"):
        pytest.skip("the shipped judge.sh, slots.sh or bash is not here (declared, not passed)")
    kit = tmp_path / "kit"
    kit.mkdir()
    shutil.copy(SLOTS_SH, kit / "slots.sh")
    (kit / "docker-compose.yml").write_text("services:" + NL + "  miner:" + NL + "    image: x" + NL)
    b = tmp_path / "bin"
    b.mkdir()
    (b / "docker").write_text(FAKE_SW_DOCKER)
    os.chmod(b / "docker", 0o755)
    log = tmp_path / "docker.log"
    env = {"PATH": str(b) + os.pathsep + os.environ.get("PATH", ""), "HOME": str(tmp_path), "FAKE_LOG": str(log),
           "DENDRA_MINER_KIT": str(kit)}
    off = "MINER_ID=dm1abc" + NL + "DENDRA_MINER_JUDGE=1" + NL + "DENDRA_SERVE_ENDPOINT=http://ollama-cpu:11434" + NL
    (kit / ".env").write_text(off)
    r = _RUN(["bash", judge, "off"], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 2 and "GPU mining is off on this kit" in r.stderr, r.stdout + r.stderr
    assert (kit / ".env").read_text() == off and not [ln for ln in _lines(log) if " up " in ln + " "]
    # the same kit with the card on: the refusal above is this guard's, since off then goes through
    (kit / ".env").write_text(off.replace("DENDRA_SERVE_ENDPOINT=http://ollama-cpu:11434" + NL, ""))
    r = _RUN(["bash", judge, "off"], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DENDRA_MINER_JUDGE=0" in (kit / ".env").read_text()


def test_judge_sh_replaces_the_env_in_one_rename_and_keeps_its_mode(tmp_path):
    # The Stop of the desktop application kills judge.sh's whole process group. Its .env was rewritten in place
    # (`cat "$t" > "$f"`, which truncates first): a kill between the two left the kit's .env empty or cut, relay
    # token and identity with it. A file replaced by a rename is either the old one or the new one.
    judge = _shipped("testnet-miner", "judge.sh")
    if not os.path.isfile(judge) or not os.path.isfile(SLOTS_SH) or not _WHICH("bash"):
        pytest.skip("the shipped judge.sh, slots.sh or bash is not here (declared, not passed)")
    kit = tmp_path / "kit"
    kit.mkdir()
    shutil.copy(SLOTS_SH, kit / "slots.sh")
    (kit / "docker-compose.yml").write_text("services:" + NL + "  miner:" + NL + "    image: x" + NL)
    b = tmp_path / "bin"
    b.mkdir()
    (b / "docker").write_text(FAKE_SW_DOCKER)
    os.chmod(b / "docker", 0o755)
    env = {"PATH": str(b) + os.pathsep + os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "FAKE_LOG": str(tmp_path / "docker.log"), "DENDRA_MINER_KIT": str(kit)}
    f = kit / ".env"
    f.write_text("MINER_ID=dm1abc" + NL + "DENDRA_RELAY_TOKEN=keep-me" + NL + "DENDRA_MINER_JUDGE=1" + NL +
                 "COMPOSE_PROFILES=judge" + NL)
    os.chmod(f, 0o600)
    orig = f.read_bytes()
    # The old file is held open, so its inode stays taken: ext4 hands a freed inode number to the next file at once,
    # and judge.sh off renames four times. Written in place, the old descriptor would read the new text.
    with open(f, "rb") as old:
        r = _RUN(["bash", judge, "off"], capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert os.fstat(old.fileno()).st_ino != f.stat().st_ino, "the .env was written in place, not replaced"
        assert old.read() == orig, "the .env was written in place, not replaced"
    assert f.stat().st_mode & 0o777 == 0o600
    assert f.read_text().splitlines() == ["MINER_ID=dm1abc", "DENDRA_RELAY_TOKEN=keep-me", "DENDRA_MINER_JUDGE=0",
                                          "DENDRA_NO_JUDGE=1"]
    assert sorted(p.name for p in kit.iterdir()) == [".env", "docker-compose.yml", "slots.sh"]


# --- the state in plain words: STATUS_RULES, one table ----------------------------------------------------------
# What is replaced: nothing in the rules (they run as shipped, on facts the cases build with the application's own
# readers: `_health_from` on a health document, `kit_floor_state`). What is real in the end-to-end cases: the HTTP
# overview, the reader the application hands to the container (run by `in_container` against a heartbeat file the
# case writes) and the quick check's script. The clock is never the machine's: a deadline is far in the past or far
# in the future, or the case passes `now`.
import datetime  # noqa: E402

DEADLINE = "2026-10-24T12:00:00Z"
DEADLINE_TS = int(datetime.datetime(2026, 10, 24, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
OK_CHECKS = [("C1", "node RPC", "ok", "", ""), ("C2", "node caught up", "ok", "", ""),
             ("C3", "registration and keys", "ok", "", ""), ("C11", "judge role (declared)", "ok", "", "")]


def checks_of(rows):
    return [{"id": i, "name": n, "state": s, "measured": m, "reason": "", "fix": f} for i, n, s, m, f in rows]


def report_doc(checks, age=600, period=SCHEDULE_S):
    ko = sum(c["state"] == "ko" for c in checks)
    un = sum(c["state"] == "unmeasured" for c in checks)
    d = health_doc(rc=1 if ko else (2 if un else 0), epoch=int(time.time()) - age, schedule_period_s=period)
    d["host"] = []
    d["selftest"] = {"checks": checks}
    d["summary"] = {"ok": len(checks) - ko - un, "ko": ko, "unmeasured": un, "checks": len(checks)}
    return d


def facts_of(A, rows=OK_CHECKS, age=600, period=SCHEDULE_S, **over):
    sched = A._health_from(report_doc(checks_of(rows), age, period))
    f = {"now": DEADLINE_TS - 86400, "docker": None, "miner": {"running": True, "word": "running"},
         "report": dict(sched, source="scheduled", carried=[], carried_age_s=None), "schedule": sched,
         "role": {"word": "active", "why": "", "age_s": 60, "requested": True},
         "kit": {"behind": False, "latest": "4", "why": ""},
         "floor": {"mine": "4", "min": "3", "deadline": DEADLINE, "read": True, "why": ""},
         "phrase": False, "update": "git pull and run deploy/join.sh again"}
    f.update(over)
    return f


def ids_of(st):
    return [r["id"] for r in st["rules"]]


def facts_from_doc(A, doc, **over):
    """Facts whose hourly report is this health document as it is, not one built from check rows."""
    sched = A._health_from(doc)
    return facts_of(A, schedule=sched, report=dict(sched, source="scheduled", carried=[], carried_age_s=None), **over)


# A report that exits 2 with nothing counted behind it: miner_health.sh::finish gives rc 2 when no check was measured.
EMPTY_DOC = dict(report_doc([]), rc=2, summary={"ok": 0, "ko": 0, "unmeasured": 1, "checks": 1}, host=[], selftest=None)


def scenarios(A):
    """(rule id, facts, level, the ids that fire, words the line must carry): one case per rule of the table."""
    behind = {"behind": True, "latest": "4", "why": ""}
    low = {"mine": "2", "min": "3", "deadline": DEADLINE, "read": True, "why": ""}
    ko = lambda cid, name, measured, fix="": OK_CHECKS[:1] + [(cid, name, "ko", measured, fix)]  # noqa: E731
    return [
        ("docker", facts_of(A, docker="Docker did not answer: no output", miner={"running": None, "word": ""}),
         "red", ["docker"], ["Docker did not answer: no output"]),
        ("stopped", facts_of(A, miner={"running": False, "word": "exited"}), "red", ["stopped"], ["exited"]),
        ("kit_refused", facts_of(A, now=DEADLINE_TS + 1, floor=low, kit=behind), "red", ["kit_refused"],
         ["kit 2", "minimum 3", "2026-10-24 12:00:00 UTC", "git pull and run deploy/join.sh again"]),
        ("node_behind", facts_of(A, rows=ko("C2", "node caught up", "catching up, 900 block(s) behind", "wait for it")),
         "red", ["node_behind"], ["900 block(s) behind", "wait for it"]),
        ("judge_mute", facts_of(A, role={"word": "mute", "why": "judge_worker.py is not running", "age_s": 5, "requested": True},
                                rows=ko("C11", "judge role (declared)", "mute seat: judge_worker.py is not running", "restart miner")),
         "red", ["judge_mute"], ["judge_worker.py is not running", "restart miner"]),
        ("capacity_report", facts_of(A, rows=ko("C8", "signed capacity report", "no line for m-bench in the registry",
                                                "bash deploy/testnet-miner/publish-capacity.sh")),
         "red", ["capacity_report"], ["no line for m-bench in the registry",
                                      "The season pays nothing to an identity without a signed capacity report",
                                      "publish-capacity.sh"]),
        ("check_failed", facts_of(A, rows=ko("C3", "registration and keys", "not registered", "run join.sh")),
         "red", ["check_failed"], ["C3 registration and keys", "not registered", "run join.sh"]),
        ("kit_soon", facts_of(A, now=DEADLINE_TS - 100, floor=low, kit=behind), "orange", ["kit_soon"],
         ["kit 2", "minimum 3", "2026-10-24 12:00:00 UTC"]),
        ("kit_behind", facts_of(A, kit=behind), "orange", ["kit_behind"], ["kit 4"]),
        ("report_old", facts_of(A, age=5 * SCHEDULE_S), "orange", ["report_old"], ["2 h"]),
        ("no_report", facts_of(A, schedule={"state": "never", "why": "no check has run on this machine yet"}, report=None),
         "orange", ["no_report"], ["no check has run on this machine yet"]),
        ("not_measured", facts_of(A, rows=OK_CHECKS + [("C5", "work queue", "unmeasured", "", "")]), "orange",
         ["not_measured"], ["1 health check(s)", "C5"]),
        ("report_numbers", None, "orange", ["report_numbers"], ["exit code 2", "1 not measured"]),
        ("age_unjudged", facts_of(A, period=None), "orange", ["age_unjudged"], ["age is not judged"]),
        ("phrase", facts_of(A, phrase=True), "orange", ["phrase"], ["24 recovery words"]),
        ("phrase_unread", facts_of(A, phrase=None), "orange", ["phrase_unread"], ["was not read"]),
        ("kit_unread", facts_of(A, kit={"behind": None, "latest": None, "why": "version check not read: first hour"}),
         "orange", ["kit_unread"], ["first hour"]),
    ]


def test_every_rule_of_the_table_fires_alone_on_its_own_facts_with_its_words_and_its_page(app):
    A, _ = app
    table = {r["id"] for r in A.STATUS_RULES}
    cases = scenarios(A)
    assert {c[0] for c in cases} == table, "a rule of the table has no case here, or a case has no rule"
    for rid, facts, level, fired, words in cases:
        st = A.status_view(facts if facts is not None else facts_from_doc(A, EMPTY_DOC))
        assert ids_of(st) == fired and st["level"] == level, (rid, ids_of(st), st["level"])
        line = st["rules"][0]
        assert line["id"] == rid and line["level"] == level and st["word"] == A.LEVEL_WORDS[level], rid
        assert st["headline"] == line["says"] and st["do"] == line["do"] and line["do"], rid
        for w in words:
            assert w in line["says"] + " " + line["do"], (rid, w, line)
        assert line["rule"] == "dendra_app.py::STATUS_RULES" and "::" in line["source"], rid
        # the gesture leads to the site's page, or to a card of this application: one of the two, never neither
        assert bool(line["page"]) != bool(line["in_app"]), rid
        if line["page"]:
            assert line["page"]["url"].startswith(A.SITE + "/") and "#" in line["page"]["url"], rid


def test_green_is_every_fact_read_and_fine_and_it_says_what_was_read(app):
    A, _ = app
    st = A.status_view(facts_of(A))
    assert st["level"] == "green" and st["word"] == "All good" and st["rules"] == []
    assert st["do"] == "Nothing to do." and len(st["checked"]) == 4
    assert any("this kit matches the published kit" in c for c in st["checked"])
    # a network that publishes no floor is a reading, said as such in the green line
    none = A.status_view(facts_of(A, floor={"mine": "4", "min": None, "deadline": None, "read": True, "why": ""}))
    assert none["level"] == "green" and any("no kit floor" in c for c in none["checked"])


def test_a_fact_that_was_not_read_is_never_green(app):
    A, _ = app
    unread = {"read": None, "why": ""}
    cases = {
        "docker": dict(docker="Docker did not answer: x", miner={"running": None, "word": ""}),
        "miner state": dict(miner={"running": None, "word": ""}),
        "schedule never": dict(schedule={"state": "never", "why": "no check has run"}, report=None),
        "schedule unread": dict(schedule={"state": "unread", "why": "not JSON"}, report=None),
        "only a quick check": dict(schedule={"state": "never", "why": "no check has run"}),
        "no period": dict(period=None),
        "version check": dict(kit={"behind": None, "latest": None, "why": "version check not read"}),
        "floor not read yet": dict(floor={"mine": "4", "min": None, "deadline": None, "read": False, "why": ""}),
        "floor from an old daemon": dict(floor=dict({"mine": None, "min": None, "deadline": None}, **unread)),
        "own kit number": dict(floor={"mine": None, "min": "3", "deadline": DEADLINE, "read": True, "why": ""}),
        "floor that is no number": dict(floor={"mine": "4", "min": "x3", "deadline": DEADLINE, "read": True, "why": ""}),
        "floor without a deadline": dict(floor={"mine": "2", "min": "3", "deadline": None, "read": True, "why": ""}),
        "phrase": dict(phrase=None),
    }
    for name, over in cases.items():
        f = facts_of(A, **over)
        st = A.status_view(f)
        assert st["level"] != "green" and st["rules"], (name, st["level"])
    # a fact the state reads and the caller forgot is an error, never a default
    for key in facts_of(A):
        f = facts_of(A)
        del f[key]
        with pytest.raises(KeyError):
            A.status_view(f)


def test_the_worst_level_leads_and_a_stopped_miner_hides_what_only_a_running_one_can_say(app):
    A, _ = app
    behind = {"behind": True, "latest": "4", "why": ""}
    both = A.status_view(facts_of(A, kit=behind, rows=OK_CHECKS[:1] + [("C3", "registration and keys", "ko", "x", "")]))
    assert ids_of(both) == ["check_failed", "kit_behind"] and both["level"] == "red"
    assert both["headline"] == both["rules"][0]["says"], "the red line leads, though the orange rule is earlier in the table's code"
    # Docker down: the facts read inside the container are unread, and saying so would bury the one cause
    down = A.status_view(facts_of(A, docker="Docker did not answer: x", miner={"running": None, "word": ""},
                                  kit={"behind": None, "latest": None, "why": "w"}, phrase=None,
                                  floor={"mine": None, "min": None, "deadline": None, "read": None, "why": ""}))
    assert ids_of(down) == ["docker"]
    stopped = A.status_view(facts_of(A, miner={"running": False, "word": "exited"}, phrase=None, kit=behind))
    assert ids_of(stopped) == ["stopped"]
    # the same facts with the miner running say the rest
    running = A.status_view(facts_of(A, phrase=None, kit=behind))
    assert ids_of(running) == ["kit_behind", "phrase_unread"]


def test_the_capacity_line_says_what_the_miners_sender_can_do_and_says_it_once(app, tmp_path):
    # The gesture is C8's own fix, built by the delivered self-test from the sender's last round: no promise that
    # the miner sends it by itself when that round says it cannot, and the reason shown rather than hidden.
    A, _ = app
    sys.path.insert(0, SERVICES)
    import miner_selftest as MS
    env = {"DENDRA_CAPACITY_URL": "https://api.example.net/capacity",
           "DENDRA_CAPACITY_STATUS_FILE": str(tmp_path / "capacity-status.json")}
    cases = {"no-inventory": ("cannot send it until this is fixed", "no hardware inventory"),
             "absent": ("sends it by itself once the chain records", "grep capacity"),
             "refused": ("tries again by itself", "HTTP 503")}
    for outcome, (gesture, why) in cases.items():
        (tmp_path / "capacity-status.json").write_text(json.dumps(
            {"outcome": outcome, "at": int(time.time()), "why": why if outcome != "absent" else "no miner yet"}))
        fix = MS.capacity_fix(env, "SENT-FIX")
        st = A.status_view(facts_of(A, rows=OK_CHECKS[:1] + [("C8", "signed capacity report", "ko", "no line", fix)]))
        line = st["rules"][0]
        assert ids_of(st) == ["capacity_report"] and line["do"] == fix, (outcome, line)
        assert gesture in line["do"] and why in line["do"], (outcome, line)
        assert ("by itself" in line["do"]) == (outcome != "no-inventory"), (outcome, line)
        said = line["says"] + " " + line["do"]
        assert said.count("by itself") <= 1 and said.lower().count("the season pays nothing") == 1, (outcome, said)
    # no public address in the sender's environment: the fix says so, never that the miner sends by itself
    fix = MS.capacity_fix({"DENDRA_NODE": "tcp://dendra-node:26657"}, "SENT-FIX")
    assert "cannot send it" in fix and "DENDRA_CAPACITY_URL" in fix and "by itself" not in fix, fix


def test_a_check_with_its_own_rule_is_not_said_twice(app):
    A, _ = app
    assert set(A.CLAIMED_CHECKS) == {"C2", "C8", "C11"}
    # C8 has its own line, which says what the report decides; the general rule leaves it alone
    cap = A.status_view(facts_of(A, rows=OK_CHECKS[:1] + [("C8", "signed capacity report", "ko", "no line", "")]))
    assert ids_of(cap) == ["capacity_report"] and "pays nothing" in cap["rules"][0]["says"]
    st = A.status_view(facts_of(A, rows=OK_CHECKS[:1] + [("C2", "node caught up", "ko", "9 behind", ""),
                                                          ("C11", "judge role (declared)", "ko", "MUTE: x", ""),
                                                          ("C4", "availability (presence)", "ko", "ABSENT", "")]))
    assert ids_of(st) == ["node_behind", "judge_mute", "check_failed"]
    assert "C4 availability" in st["rules"][2]["says"] and "more" not in st["rules"][2]["says"]
    two = A.status_view(facts_of(A, rows=OK_CHECKS[:1] + [("C3", "registration and keys", "ko", "a", ""), ("C4", "availability (presence)", "ko", "b", "")]))
    assert "(and 1 more)" in two["rules"][0]["says"]
    # a mute seat is read from the role word alone too, and from the check alone
    assert ids_of(A.status_view(facts_of(A, role={"word": "mute", "why": "w", "age_s": 1, "requested": True}))) == ["judge_mute"]
    assert ids_of(A.status_view(facts_of(A, rows=OK_CHECKS[:1] + [("C11", "judge role (declared)", "ko", "MUTE: y", "")]))) == ["judge_mute"]


def test_the_kit_floor_is_applied_as_the_relay_applies_it(app):
    A, _ = app
    fl = lambda mine, kmin, dl=DEADLINE, read=True: {"mine": mine, "min": kmin, "deadline": dl, "read": read, "why": ""}  # noqa: E731
    state = lambda f, now: A.kit_floor_state(f, now)[0]  # noqa: E731
    # from the deadline on (now >= deadline), never a second before
    assert state(fl("2", "3"), DEADLINE_TS - 1) == "soon" and state(fl("2", "3"), DEADLINE_TS) == "refused"
    assert state(fl("3", "3"), DEADLINE_TS + 10 ** 6) == "clear", "at the floor passes"
    assert state(fl("4", "3"), DEADLINE_TS + 10 ** 6) == "clear"
    assert state(fl("2", None, None), DEADLINE_TS) == "none"
    assert state(fl("2", "3", "2026-10-24 12:00"), DEADLINE_TS) == "unread", "a deadline in another form is not read"
    assert state(fl("12", "9"), DEADLINE_TS) == "clear", "kit numbers compare as numbers, not as text"
    assert state(fl("2", "3", read=None), DEADLINE_TS) == "unread" and state(fl("2", "3", read=False), DEADLINE_TS) == "unread"
    assert state(None, DEADLINE_TS) == "unread"


def test_every_citation_of_the_table_names_a_symbol_that_exists_and_a_page_that_has_the_section(app):
    A, _ = app
    repo = os.path.dirname(os.path.dirname(APP))
    homes = [SERVICES, os.path.join(SERVICES, "modea"), os.path.join(repo, "deploy", "testnet-miner"), APP]
    for r in A.STATUS_RULES:
        fname, _, sym = r["source"].partition("::")
        path = next((os.path.join(h, fname) for h in homes if os.path.isfile(os.path.join(h, fname))), None)
        assert path and sym, (r["id"], r["source"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert re.search("^(def |class )?" + re.escape(sym) + "[ (:=]", text, re.M), (r["id"], r["source"])
        assert callable(r["test"]) and r["level"] in ("red", "orange")
        page = r.get("page")
        if page is None:
            assert r.get("in_app"), r["id"]
            continue
        assert not r.get("in_app"), r["id"]
        site = os.path.join(repo, "site", page[0].strip("/"), "index.html")
        if not os.path.isfile(site):
            pytest.skip("the site tree is not in this checkout: the anchors are not read (declared, not passed)")
        with open(site, encoding="utf-8") as fh:
            assert 'id="' + page[1] + '"' in fh.read(), (r["id"], page)


def test_the_table_is_ordered_red_first_and_the_levels_are_the_three_words(app):
    A, _ = app
    assert A.LEVELS == ("green", "orange", "red") and set(A.LEVEL_WORDS) == set(A.LEVELS)
    levels = [r["level"] for r in A.STATUS_RULES]
    assert levels == sorted(levels, key=lambda x: -A.LEVELS.index(x)), "red rules, then orange rules"
    assert len({r["id"] for r in A.STATUS_RULES}) == len(A.STATUS_RULES)


# --- the state, end to end: the overview over a heartbeat, a health file and a quick check ----------------------
def _status_overview(A, kit, tmp_path, monkeypatch, heartbeat, rows=OK_CHECKS, age=600, running=True, keys=None):
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_KIT_UPDATE=git pull and run deploy/join.sh again" + NL)
    hb = tmp_path / "heartbeat.json"
    hb.write_text(json.dumps(heartbeat))
    (kit / "miner-health.last.json").write_text(json.dumps(report_doc(checks_of(rows), age)))
    _, _, run = in_container(tmp_path, env={"DENDRA_STATUS_FILE": str(hb)})
    monkeypatch.setattr(A, "miner_exec", run)
    monkeypatch.setattr(A, "containers", lambda: ([{"service": "miner", "state": "running" if running else "exited", "status": ""}], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1aaa"})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: {})
    monkeypatch.setattr(A, "keys_status", lambda: {"recovery": {"present": False, "words": 0}} if keys is None else keys)
    return call(A, "/api/overview")[1]


def _hb(**over):
    d = {"kit_behind": False, "kit_latest": "4", "kit_version": 4, "kit_min": "3", "kit_deadline": DEADLINE, "kit_read_at": 1}
    d.update(over)
    return d


def test_the_overview_says_green_only_when_everything_it_reads_is_fine(app, tmp_path, monkeypatch):
    A, kit = app
    st = _status_overview(A, kit, tmp_path, monkeypatch, _hb())["status"]
    assert st["level"] == "green" and st["rules"] == [], st
    # the daemon's kit numbers, read from the heartbeat by the reader the application ships
    assert [x["id"] for x in _status_overview(A, kit, tmp_path, monkeypatch, _hb(kit_version=2, kit_deadline="2020-01-01T00:00:00Z", kit_behind=True))["status"]["rules"]] == ["kit_refused"]
    soon = _status_overview(A, kit, tmp_path, monkeypatch, _hb(kit_version=2, kit_deadline="2099-01-01T00:00:00Z", kit_behind=True))["status"]
    assert soon["level"] == "orange" and [x["id"] for x in soon["rules"]] == ["kit_soon"]
    assert "2099-01-01 00:00:00 UTC" in soon["headline"] and "git pull and run deploy/join.sh again" in soon["do"]
    # not read: a daemon that has not read the network's file, and one older than the field
    for hb in (_hb(kit_read_at=None, kit_behind=None, kit_latest=None, kit_min=None, kit_deadline=None),
               {"kit_behind": False, "kit_latest": "4"}):
        got = _status_overview(A, kit, tmp_path, monkeypatch, hb)["status"]
        assert got["level"] == "orange" and [x["id"] for x in got["rules"]] == ["kit_unread"], (hb, got)
    # a stopped miner, a failed check, an old report
    assert [x["id"] for x in _status_overview(A, kit, tmp_path, monkeypatch, _hb(), running=False)["status"]["rules"]] == ["stopped"]
    bad = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), rows=OK_CHECKS[:1] + [("C3", "registration and keys", "ko", "not registered", "run join.sh")])["status"]
    assert bad["level"] == "red" and "not registered" in bad["headline"]
    old = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), age=4 * SCHEDULE_S)["status"]
    assert [x["id"] for x in old["rules"]] == ["report_old"]
    # a phrase still on the machine, and a keys status that was not read
    ph = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), keys={"recovery": {"present": True, "words": 24, "address": "dendra1aaa"}})["status"]
    assert [x["id"] for x in ph["rules"]] == ["phrase"]
    assert [x["id"] for x in _status_overview(A, kit, tmp_path, monkeypatch, _hb(), keys={})["status"]["rules"]] == ["phrase_unread"]


def test_a_quick_check_run_here_is_the_newest_report_until_the_hourly_one_is_newer(app, tmp_path, monkeypatch):
    A, kit = app
    bad_rows = OK_CHECKS[:1] + [("C3", "registration and keys", "ko", "not registered", "run join.sh")]
    # the hourly report says a problem; a quick check, newer, says all is well
    ok_quick = report_doc(checks_of(OK_CHECKS), age=10)
    (kit / "miner_health.sh").write_text("#!/usr/bin/env bash" + NL + "echo '" + json.dumps(ok_quick) + "'" + NL)
    first = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), rows=bad_rows)["status"]
    assert first["level"] == "red"
    assert call(A, "/api/health/run", body={})[0] == 200
    after = call(A, "/api/overview")[1]["status"]
    assert after["level"] == "green", after
    # the reverse: a quick check that finds a problem turns an hourly report that was fine red
    A._QUICK.clear()
    bad_quick = report_doc(checks_of(bad_rows), age=10)
    (kit / "miner_health.sh").write_text("#!/usr/bin/env bash" + NL + "echo '" + json.dumps(bad_quick) + "'" + NL + "exit 1" + NL)
    assert _status_overview(A, kit, tmp_path, monkeypatch, _hb())["status"]["level"] == "green"
    assert call(A, "/api/health/run", body={})[0] == 200
    assert call(A, "/api/overview")[1]["status"]["level"] == "red"
    # an hourly report newer than the quick check takes over again
    (kit / "miner-health.last.json").write_text(json.dumps(report_doc(checks_of(OK_CHECKS), age=1)))
    assert call(A, "/api/overview")[1]["status"]["level"] == "green"
    A._QUICK.clear()
    # and a quick check never stands in for the hourly report that never ran
    (kit / "miner-health.last.json").unlink()
    (kit / "miner_health.sh").write_text("#!/usr/bin/env bash" + NL + "echo '" + json.dumps(ok_quick) + "'" + NL)
    assert call(A, "/api/health/run", body={})[0] == 200
    only_quick = call(A, "/api/overview")[1]["status"]
    assert [x["id"] for x in only_quick["rules"]] == ["no_report"]


C7_KO = ("C7", "model answers", "ko", "the model does not answer", "restart the miner")
C7_OK = ("C7", "model answers", "ok", "", "")


def _run_quick_with(kit, A, rows, age=10):
    """Replace the kit's health script by one that prints a quick check's document, and run it from the page."""
    A._QUICK.clear()
    doc = dict(report_doc(checks_of(rows), age=age), quick=True)
    (kit / "miner_health.sh").write_text("#!/usr/bin/env bash" + NL + "echo '" + json.dumps(doc) + "'" + NL)
    assert call(A, "/api/health/run", body={})[0] == 200


def test_a_quick_check_does_not_wipe_a_problem_it_did_not_look_at(app, tmp_path, monkeypatch):
    # The quick run skips the model probe (C7) and the relay check (C6): miner_selftest.py::run_checks. The problem the
    # hourly report read in C7 stands until a run measures C7 again.
    A, kit = app
    first = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), rows=OK_CHECKS + [C7_KO])["status"]
    assert first["level"] == "red" and ids_of(first) == ["check_failed"] and "C7 model answers" in first["headline"]
    _run_quick_with(kit, A, OK_CHECKS)
    after = call(A, "/api/overview")[1]["status"]
    assert after["level"] == "red" and ids_of(after) == ["check_failed"], after
    assert "C7 model answers" in after["headline"] and "the model does not answer" in after["headline"]
    # a quick run that did measure that check takes its place
    _run_quick_with(kit, A, OK_CHECKS + [C7_OK])
    assert call(A, "/api/overview")[1]["status"]["level"] == "green"
    # and the green says where C7 comes from when the quick run did not carry it
    _status_overview(A, kit, tmp_path, monkeypatch, _hb(), rows=OK_CHECKS + [C7_OK])
    _run_quick_with(kit, A, OK_CHECKS)
    ok = call(A, "/api/overview")[1]["status"]
    assert ok["level"] == "green" and any("C7 result stands from the hourly report" in c for c in ok["checked"]), ok
    A._QUICK.clear()


def test_the_newest_report_carries_what_the_newer_one_did_not_run(app):
    A, _ = app
    sched = A._health_from(report_doc(checks_of(OK_CHECKS + [C7_KO]), age=900))
    A._QUICK.clear()
    A._QUICK[0] = dict(report_doc(checks_of(OK_CHECKS), age=5), quick=True)
    try:
        r = A.newest_report(sched)
        assert r["source"] == "quick" and [c["id"] for c in r["carried"]] == ["C7"] and r["carried_age_s"] >= 900
        # a check both reports carry is the newer one's, and an older quick check carries nothing
        A._QUICK[0] = dict(report_doc(checks_of(OK_CHECKS + [C7_OK]), age=5), quick=True)
        assert A.newest_report(sched)["carried"] == []
        A._QUICK[0] = dict(report_doc(checks_of(OK_CHECKS), age=5000), quick=True)
        old = A.newest_report(sched)
        assert old["source"] == "scheduled" and old["carried"] == []
    finally:
        A._QUICK.clear()


def test_a_report_that_is_not_a_pass_and_names_no_failed_check_is_never_green(app):
    A, _ = app
    base = report_doc(checks_of(OK_CHECKS))
    cases = {
        "exit 2, nothing counted (the multi-identity document)": EMPTY_DOC,
        "exit 1, no line that failed": dict(base, rc=1, summary=dict(base["summary"], ko=1, ok=3)),
        "exit 0 with a failure counted": dict(base, summary=dict(base["summary"], ko=1, ok=3)),
        "exit 0 with a check not measured counted": dict(base, summary=dict(base["summary"], unmeasured=1, ok=3)),
        "exit 0, nothing counted": dict(base, summary={"ok": 0, "ko": 0, "unmeasured": 0, "checks": 0}),
        "exit 2 though every count says a pass": dict(base, rc=2),
    }
    for name, doc in cases.items():
        st = A.status_view(facts_from_doc(A, doc))
        assert st["level"] == "orange" and ids_of(st) == ["report_numbers"], (name, ids_of(st), st["level"])
    # a line that failed has its own rule, and the numbers add nothing to it
    assert ids_of(A.status_view(facts_of(A, rows=OK_CHECKS + [C7_KO]))) == ["check_failed"]
    assert A.status_view(facts_of(A))["level"] == "green"


def test_a_phrase_that_was_not_read_is_never_read_as_no_phrase(app):
    A, _ = app
    for name, st in {"no status": None, "empty status": {}, "no recovery field": {"keyring": {}}, "recovery that is no object": {"recovery": "x"},
                     "file that did not open": {"recovery": {"present": None, "error": "PermissionError: x"}},
                     "words that are no number": {"recovery": {"present": True, "words": "?"}},
                     "no word count for a phrase": {"recovery": {"present": True}},
                     "word count of a phrase that is not one": {"recovery": {"present": True, "words": 7}},
                     "absent that is no word": {"recovery": {"present": "no"}},
                     "no word count that is no number": {"recovery": {"present": False, "words": "?"}}}.items():
        assert A.phrase_fact(st) is None, name
    assert A.phrase_fact({"recovery": {"present": False}}) is False, "the keyring's answer for no file"
    assert A.phrase_fact({"recovery": {"present": False, "words": 0}}) is False, "a file that holds no phrase"
    assert A.phrase_fact({"recovery": {"present": True, "words": 24}}) is True
    assert A.phrase_fact({"recovery": {"present": True, "words": 12}}) is True


def test_the_overview_does_not_say_no_phrase_when_the_phrase_file_did_not_open(app, tmp_path, monkeypatch):
    A, kit = app
    for keys in ({"recovery": {"present": None, "error": "PermissionError: x"}}, {"keyring": {}},
                 {"recovery": {"present": True, "words": "?"}}):
        got = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), keys=keys)["status"]
        assert got["level"] == "orange" and ids_of(got) == ["phrase_unread"], (keys, got)
    absent = _status_overview(A, kit, tmp_path, monkeypatch, _hb(), keys={"recovery": {"present": False}})["status"]
    assert absent["level"] == "green"


def test_a_kit_with_no_number_of_its_own_is_refused_like_one_below_the_floor(app, tmp_path, monkeypatch):
    # relay.py::verdict_kit_policy: from the deadline a deposit whose body declares kit_version below the floor,
    # or none, is refused. A kit whose KIT_VERSION file is unreadable has none.
    A, kit = app
    fl = lambda mine, kmin, dl=DEADLINE: {"mine": mine, "min": kmin, "deadline": dl, "read": True, "why": ""}  # noqa: E731
    assert A.kit_floor_state(fl(None, "3"), DEADLINE_TS - 1)[0] == "soon"
    assert A.kit_floor_state(fl(None, "3"), DEADLINE_TS)[0] == "refused"
    assert A.kit_floor_state(fl("abc", "3"), DEADLINE_TS)[0] == "refused"
    assert A.kit_floor_state(fl(None, None, None), DEADLINE_TS)[0] == "unread", "no floor: nothing refuses it, but it is not compared"
    assert A.kit_floor_state(fl(None, "3", "2026-10-24 12:00"), DEADLINE_TS)[0] == "unread"
    why = "this kit's KIT_VERSION file is unreadable: nothing to compare with the network's"
    gone = dict(kit_version=None, kit_behind=None, kit_latest="4", kit_why=why)
    past = _status_overview(A, kit, tmp_path, monkeypatch, _hb(kit_deadline="2020-01-01T00:00:00Z", **gone))["status"]
    assert past["level"] == "red" and ids_of(past) == ["kit_refused", "kit_unread"], past
    assert "kit unknown" in past["headline"] and "git pull and run deploy/join.sh again" in past["do"]
    # the reason shown for the unread comparison is the daemon's own, not a guess
    said = past["rules"][1]["says"]
    assert "KIT_VERSION file is unreadable" in said and "has not compared" not in said, said
    future = _status_overview(A, kit, tmp_path, monkeypatch, _hb(kit_deadline="2099-01-01T00:00:00Z", **gone))["status"]
    assert future["level"] == "orange" and ids_of(future) == ["kit_soon", "kit_unread"], future
    nofloor = _status_overview(A, kit, tmp_path, monkeypatch, _hb(kit_min=None, kit_deadline=None, **gone))["status"]
    assert ids_of(nofloor) == ["kit_unread"] and nofloor["level"] == "orange"


# --- "Test my machine" --------------------------------------------------------------------------------------------
MODEL ="llama3.1:8b-instruct-q4_K_M"
JUDGE = "judge-model:1"
# The judge's three prompts are told apart by their opening, read from the shipped module (modea/judge.py), never typed here.
if SERVICES not in sys.path:
    sys.path.insert(0, SERVICES)
from modea import judge as J  # noqa: E402

KIND_STARTS = [("coherence", J.COHERENCE_PROMPT[:30]), ("relevance", J.RELEVANCE_PROMPT[:30]), ("same-fact", J.PROMPT[:30])]


def kind_of(prompt):
    return next((k for k, s in KIND_STARTS if prompt.startswith(s)), None)


class _Engine(BaseHTTPRequestHandler):
    """A model engine as Ollama answers it. Each request body is kept. The reply comes from the prompt as a model's
    would: the judge's three prompts get the case's judge word, the speed prompt gets the case's number of words
    after its delay, a reference question gets a sentence after its own delay. A model the case lists as missing is
    answered 404, as Ollama does."""

    def log_message(self, *a):
        return

    def _reply(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/ps":
            return self._reply(200, {"models": self.server.cfg["ps"]})
        if self.path == "/api/tags":
            # what the memory guard reads before a call that could load the judge model: each model with its size
            return self._reply(200, {"models": [{"name": m, "model": m, "size": 5 * 1024 ** 3} for m in (MODEL, JUDGE)]})
        return self._reply(404, {"error": "route"})

    def do_POST(self):
        cfg = self.server.cfg
        doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/show":
            return self._reply(200, {"model_info": {
                "general.architecture": "llama", "llama.block_count": 32, "llama.attention.head_count_kv": 8,
                "llama.attention.key_length": 128, "llama.attention.value_length": 128}})
        self.server.seen.append(doc)
        prompt = doc.get("prompt", "")
        if doc.get("model") in cfg["missing"]:
            return self._reply(404, {"error": "model not found"})
        if kind_of(prompt):
            text = cfg["judge_reply"]
        elif prompt == "ok":
            text = "ok"
        elif prompt == cfg["speed_prompt"]:
            time.sleep(cfg["delay"])
            text = " ".join(["word"] * cfg["words"])
        else:
            time.sleep(cfg["ref_delay"])
            text = "The capital of France is Paris."
        return self._reply(200, {"response": text, "eval_count": len(text.split()), "prompt_eval_count": 7})


@pytest.fixture()
def mt(app, tmp_path, monkeypatch):
    """(A, kit, engine): the application over a kit whose miner container is played on this machine against a model
    engine of the bench. The miner's model and the judge's differ, so each call can be told apart."""
    A, kit = app
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    srv.seen = []
    srv.cfg = {"words": 120, "delay": 0.4, "ref_delay": 0.0, "judge_reply": "YES", "missing": set(),
               "speed_prompt": A.MT_PROMPT, "ps": [{"name": MODEL, "model": MODEL, "size": 6 * 10 ** 9, "size_vram": 6 * 10 ** 9}]}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MINER_JUDGE=1" + NL)
    # The memory guard reads this machine's memory before a call that could load the judge model: the bench machine has
    # room for it, whatever the host that runs the bench has free.
    root = tmp_path / "memroot"
    (root / "proc" / "pressure").mkdir(parents=True)
    (root / "proc" / "meminfo").write_text("".join(f"{k}: {v * 1024} kB" + NL for k, v in (
        ("MemTotal", 204800), ("MemFree", 100000), ("MemAvailable", 200000), ("Active(file)", 2048), ("SwapTotal", 0), ("SwapFree", 0))))
    (root / "proc" / "vmstat").write_text("pswpout 0" + NL)
    (root / "proc" / "pressure" / "memory").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0" + NL
                                                       + "full avg10=0.00 avg60=0.00 avg300=0.00 total=0" + NL)
    _, _, run = in_container(tmp_path, env={"OLLAMA_ENDPOINT": f"http://127.0.0.1:{srv.server_address[1]}", "OLLAMA_MODEL": MODEL,
                                            "DENDRA_JUDGE_MODEL_ID": JUDGE, "DENDRA_MEMWATCH_PROC_ROOT": str(root)})
    monkeypatch.setattr(A, "miner_exec", run)
    yield A, kit, srv
    srv.shutdown()


def test_the_machine_test_makes_the_miner_s_and_the_judge_s_calls_and_reports_what_they_took(mt):
    A, kit, eng = mt
    code, v = call(A, "/api/machine-test", body={})
    assert code == 200, v
    sp, vd = v["throughput"], v["verdict"]
    assert sp["state"] == "measured" and sp["words"] == 120 and sp["model"] == MODEL and sp["placement"] == "card"
    # the time is the one request's: not under the engine's delay, and the rate follows from the two figures
    assert 0.4 <= sp["seconds"] < 10 and sp["words_per_s"] == pytest.approx(120 / sp["seconds"], rel=0.02)
    assert vd["state"] == "measured" and vd["stage"] == A.MT_FULL_STAGE and vd["generations"] == 2 and vd["model"] == JUDGE
    assert vd["seconds"] > 0 and vd["load_seconds"] is not None
    # the speed is the miner's serving call: deterministic decoding, the container's model, the test's own cap
    fast = [d for d in eng.seen if d["prompt"] == A.MT_PROMPT]
    assert len(fast) == 1 and fast[0]["model"] == MODEL and fast[0]["stream"] is False
    assert fast[0]["options"] == {"temperature": 0.0, "top_k": 1, "seed": 0, "num_predict": A.MT_TOKENS}
    # the verdict is the judge's decision: the judge model reads the sample three ways (coherence, relevance,
    # same fact twice) after one warm-up read, and the miner's model writes the two references, sampled
    judged = [kind_of(d["prompt"]) for d in eng.seen if d["model"] == JUDGE]
    assert judged == ["coherence", "coherence", "relevance", "same-fact", "same-fact"], judged
    refs = [d for d in eng.seen if d["model"] == MODEL and d["prompt"] == A.MT_QUESTION]
    assert len(refs) == 2 and all(d["options"] == {"temperature": 0.7, "num_predict": A.MT_TOKENS} for d in refs)
    # nothing reached the programme, the chain or the relay: the engine is the only thing the test talked to
    assert A.machine_tests()["0"]["throughput"]["words_per_s"] == sp["words_per_s"]


def test_a_part_that_did_not_run_is_not_measured_with_its_reason_and_never_a_figure(mt, monkeypatch):
    A, kit, eng = mt
    # no word returned: a speed of 0 words per second would be a figure about nothing
    eng.cfg["words"] = 0
    v = call(A, "/api/machine-test", body={})[1]
    assert v["throughput"]["state"] == "not measured" and "no word" in v["throughput"]["why"]
    assert not {"words_per_s", "words", "seconds"} & set(v["throughput"])
    assert v["verdict"]["state"] == "measured", "the judge part does not depend on the speed part"
    eng.cfg["words"] = 120
    # a judge that reads the sample as word salad ends at its first stage: that is not a verdict's time
    eng.cfg["judge_reply"] = "NO"
    v = call(A, "/api/machine-test", body={})[1]
    assert v["verdict"]["state"] == "not measured" and "'coherence'" in v["verdict"]["why"] and "seconds" not in v["verdict"]
    assert v["throughput"]["state"] == "measured"
    eng.cfg["judge_reply"] = "YES"
    # the judge model missing on its engine
    eng.cfg["missing"] = {JUDGE}
    v = call(A, "/api/machine-test", body={})[1]
    assert v["verdict"]["state"] == "not measured" and "did not answer" in v["verdict"]["why"] and "seconds" not in v["verdict"]
    assert v["verdict"]["page"]["url"] == A.SITE + "/miner/#judge"
    eng.cfg["missing"] = set()
    # the judge off: the part says so and the engine is never asked a judge's question
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MINER_JUDGE=0" + NL)
    eng.seen.clear()
    v = call(A, "/api/machine-test", body={})[1]
    assert v["verdict"]["state"] == "not measured" and "judge is off" in v["verdict"]["why"]
    assert not [d for d in eng.seen if d["model"] == JUDGE] and v["throughput"]["state"] == "measured"


def test_a_test_that_measured_nothing_is_said_and_not_saved_and_the_last_good_one_stays(mt, tmp_path, monkeypatch):
    A, kit, eng = mt
    assert call(A, "/api/machine-test", body={})[0] == 200
    saved = A.machine_tests()["0"]
    eng.shutdown()
    eng.server_close()                                    # the engine no longer answers
    code, v = call(A, "/api/machine-test", body={})
    assert code == 200 and v["throughput"]["state"] == "not measured" and v["verdict"]["state"] == "not measured"
    assert "did not answer" in v["throughput"]["why"] and "did not answer" in v["verdict"]["why"]
    assert A.machine_tests()["0"] == saved
    # a container that cannot be reached: the command's own words, not a figure
    monkeypatch.setattr(A, "miner_exec", lambda *a, **k: (1, 'service "miner" is not running'))
    v = call(A, "/api/machine-test", body={})[1]
    assert "is not running" in v["throughput"]["why"] and "is not running" in v["verdict"]["why"]
    monkeypatch.setattr(A, "miner_exec", lambda *a, **k: (1, A.NO_DOCKER_ACCESS + NL + "denied"))
    assert call(A, "/api/machine-test", body={})[1]["throughput"]["why"] == A.NO_DOCKER_ACCESS
    assert A.machine_tests()["0"] == saved


def test_the_test_is_bounded_inside_the_container_and_the_bound_stops_a_retrying_judge_too(mt, monkeypatch):
    A, kit, eng = mt
    # the speed request outlasts the bound: the script stops itself, well before the engine would have answered
    monkeypatch.setattr(A, "MT_DEADLINE_S", 2)
    eng.cfg["delay"] = 8.0
    t0 = time.monotonic()
    v = call(A, "/api/machine-test", body={})[1]
    assert time.monotonic() - t0 < 6.5, "the alarm inside the container did not stop the test"
    assert "ran past its bound" in v["throughput"]["why"] and "ran past its bound" in v["verdict"]["why"]
    # the bound falls in the judge's first reference: decide_verdict retries a generation that raised an
    # exception, so the bound is not one, and the test still ends at the bound
    monkeypatch.setattr(A, "MT_DEADLINE_S", 3)
    eng.cfg["delay"], eng.cfg["ref_delay"] = 0.0, 8.0
    t0 = time.monotonic()
    v = call(A, "/api/machine-test", body={})[1]
    assert time.monotonic() - t0 < 7, "the judge retried past the bound"
    assert v["throughput"]["state"] == "measured"
    assert v["verdict"]["state"] == "not measured" and "ran past its bound" in v["verdict"]["why"]


def test_the_machine_test_route_needs_the_token_a_known_identity_a_kit_and_runs_one_at_a_time(mt, tmp_path):
    A, kit, eng = mt
    assert call(A, "/api/machine-test", body={}, token=False)[0] == 401
    assert call(A, "/api/machine-test", body={"slot": "9"})[0] == 400
    assert A._MT_RUN.acquire(blocking=False)
    try:
        code, d = call(A, "/api/machine-test", body={})
        assert code == 409 and "already running" in d["error"]
    finally:
        A._MT_RUN.release()
    (kit / ".env").unlink()
    assert call(A, "/api/machine-test", body={})[0] == 404


def test_rate_and_place_are_computed_from_measurements_only(app):
    A, _ = app
    assert A.rate_of(120, 4.0) == 30.0 and A.rate_of(10, 3.0) == 3.33
    for words, secs in ((0, 1.0), (-3, 1.0), (10, 0), (10, -1.0), (10, float("nan")), (10, float("inf")), (True, 1.0),
                        (10.5, 1.0), ("10", 1.0), (None, 1.0), (10, None), (10, True)):
        assert A.rate_of(words, secs) is None, (words, secs)
    assert A.placement_of(100, 100) == "card" and A.placement_of(100, 0) == "cpu" and A.placement_of(100, 40) == "split"
    for size, vram in ((None, 5), (100, None), (0, 0), (100, 101), (100, -1), ("100", 100), (True, 1), (100.0, 100.0)):
        assert A.placement_of(size, vram) is None, (size, vram)
    # a speed part from a row without a model reading keeps the place unread, not guessed
    p = A.speed_part({"words": 50, "seconds": 2.0, "warm_seconds": 1.0, "model": MODEL})
    assert p["state"] == "measured" and p["words_per_s"] == 25.0 and p["placement"] is None
    assert A.speed_part({"words": 50})["state"] == "not measured" and A.speed_part(None)["state"] == "not measured"
    assert A.speed_part({"error": "x"})["state"] == "not measured" and A.verdict_part({"error": "x"}, True)["state"] == "not measured"
    full = {"seconds": 12.5, "stage": A.MT_FULL_STAGE, "generations": 2, "read": True, "model": JUDGE, "load_seconds": 3.0}
    assert A.verdict_part(full, True)["seconds"] == 12.5
    for bad in (dict(full, stage="sc-diverge"), dict(full, read=None), dict(full, read=False), dict(full, seconds=0),
                dict(full, seconds=None), dict(full, generations=None)):
        assert A.verdict_part(bad, True)["state"] == "not measured", bad
    assert A.verdict_part(full, False)["state"] == "not measured", "a judge that is off has no verdict to time"


def test_a_stored_test_is_shown_only_in_the_shape_the_application_writes(app):
    A, _ = app
    good = {"at": 1700000000, "tokens": 200,
            "throughput": {"state": "measured", "words": 10, "seconds": 2.0, "words_per_s": 5.0},
            "verdict": {"state": "not measured", "why": "the judge is off"}}
    os.makedirs(A.CONFIG_DIR, exist_ok=True)
    path = os.path.join(A.CONFIG_DIR, A.MT_FILE)
    for doc, kept in ((json.dumps({"0": good}), True), (json.dumps({"0": dict(good, at="x")}), False),
                      (json.dumps({"0": dict(good, throughput={"state": "measured", "words_per_s": 0})}), False),
                      (json.dumps({"0": dict(good, throughput={"state": "measured", "words_per_s": -2.0})}), False),
                      (json.dumps({"0": dict(good, verdict={"state": "measured"})}), False),
                      (json.dumps({"0": dict(good, verdict={"state": "not measured"})}), False),
                      (json.dumps({"0": dict(good, verdict={"state": "fine", "seconds": 3})}), False),
                      (json.dumps({"x": good}), False), (json.dumps([good]), False), ("not json", False)):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        assert ("0" in A.machine_tests()) is kept, doc
        if kept:
            assert A.machine_tests()["0"] == good


def test_the_overview_carries_the_last_test_and_the_state_of_the_same_identity(app, tmp_path, monkeypatch):
    A, kit = app
    assert _status_overview(A, kit, tmp_path, monkeypatch, _hb())["machine_test"] is None
    view = {"at": 1700000000, "tokens": 200, "throughput": {"state": "measured", "words": 10, "seconds": 2.0, "words_per_s": 5.0},
            "verdict": {"state": "not measured", "why": "the judge is off"}}
    A.save_machine_test(0, view)
    assert call(A, "/api/overview")[1]["machine_test"] == view
    assert oct(os.stat(os.path.join(A.CONFIG_DIR, A.MT_FILE)).st_mode & 0o777) == "0o600"


# --- the two cards of the page ------------------------------------------------------------------------------------
def _rule(**over):
    r = {"id": "docker", "level": "red", "says": "Docker does not answer: x.", "do": "Start Docker.", "in_app": "",
         "page": {"url": "https://dendranetwork.com/miner/#help", "label": "dendranetwork.com/miner/#help"},
         "rule": "dendra_app.py::STATUS_RULES", "source": "dendra_app.py::containers"}
    r.update(over)
    return r


def _page_overview(A, **over):
    return dict(call(A, "/api/overview")[1], installed=True, identity={"miner_id": "dm1abc"}, containers=[], docker_error=None,
                chain={}, recovery_confirmed=None, **over)


def test_the_page_says_the_state_in_the_server_s_words_and_draws_no_rule_of_its_own(app, tmp_path):
    A, _ = app
    page = call(A, "/", token=False)[1].decode("utf-8")
    assert 'id="statusCard"' in page and 'id="statusWord"' in page and 'id="statusRules"' in page
    red = {"level": "red", "word": "Act now", "headline": "Docker does not answer: x.", "do": "Start Docker.",
           "rules": [_rule(), _rule(id="stopped", level="orange", says="The miner is not running (exited).",
                                    page={"url": "https://elsewhere.example/steal", "label": "elsewhere"})], "checked": []}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": _page_overview(A, status=red)}]})
    assert ui["text"]["statusWord"] == "Act now" and "bad" in ui["cls"]["statusWord"]
    assert ui["text"]["statusHeadline"] == "Docker does not answer: x."
    first, second = ui["rules"]
    assert first["cls"] == "red" and second["cls"] == "orange"
    # the first rule is the headline: its sentence is not said twice; the next ones say theirs
    assert first["parts"][0]["text"] == "What to do: Start Docker."
    assert first["parts"][1]["href"] == "https://dendranetwork.com/miner/#help" and "dendranetwork.com/miner/#help" in first["parts"][1]["text"]
    assert "STATUS_RULES" in first["parts"][2]["text"] and "dendra_app.py::containers" in first["parts"][2]["text"]
    assert [p["text"] for p in second["parts"]][:2] == ["The miner is not running (exited).", "What to do: Start Docker."]
    assert not [p for p in second["parts"] if p["href"]], "a link that leaves the project's site is not drawn"
    green = {"level": "green", "word": "All good", "headline": "Nothing needs your attention.", "do": "Nothing to do.",
             "rules": [], "checked": ["Docker answers and the miner is running", "this kit matches the published kit"]}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": _page_overview(A, status=green)}]})
    assert ui["text"]["statusWord"] == "All good" and "ok" in ui["cls"]["statusWord"] and ui["rules"] == []
    assert "Docker answers and the miner is running; this kit matches" in ui["text"]["statusChecked"]
    orange = dict(green, level="orange", word="Needs attention", rules=[_rule(level="orange", in_app="the recovery card", page=None)])
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": _page_overview(A, status=orange)}]})
    assert "warn" in ui["cls"]["statusWord"] and ui["text"]["statusChecked"] == ""
    assert "(in this application: the recovery card)" in ui["rules"][0]["parts"][0]["text"]
    # a server that sends no state (an older one) is "not read", never green
    old = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None, chain={}, recovery_confirmed=None)
    old.pop("status", None)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": old}]})
    assert ui["text"]["statusWord"] == "not read" and "warn" in ui["cls"]["statusWord"] and ui["rules"] == []


def test_the_page_runs_the_test_and_shows_a_figure_only_for_a_part_that_was_measured(app, tmp_path):
    A, _ = app
    page = call(A, "/", token=False)[1].decode("utf-8")
    assert 'id="testBtn"' in page and 'id="testSpeed"' in page and 'id="testVerdict"' in page
    done = {"at": int(time.time()) - 30, "tokens": 200,
            "throughput": {"state": "measured", "words": 120, "seconds": 4.0, "words_per_s": 30.0, "warm_seconds": 1.0,
                           "model": MODEL, "placement": "cpu"},
            "verdict": {"state": "not measured", "why": "the judge is off on this machine", "page": None}}
    ov = _page_overview(A)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}], "/api/machine-test": [{"status": 200, "data": done}]},
                steps=[["click", "testBtn"]])
    assert ui["text"]["testSpeed"] == ("30 words per second (120 words in 4 s), the model runs on the CPU, in system memory, model " + MODEL)
    assert ui["text"]["testVerdict"] == "not measured: the judge is off on this machine"
    assert ui["text"]["testMsg"] == "done" and "ago" in ui["text"]["testWhen"]
    assert {"path": "/api/machine-test", "method": "POST"} in ui["calls"]
    both = dict(done, throughput={"state": "not measured", "why": "the engine did not answer the test: x"},
                verdict={"state": "measured", "seconds": 12.5, "generations": 2, "load_seconds": 3.0, "model": JUDGE, "stage": "sc-valid"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}], "/api/machine-test": [{"status": 200, "data": both}]},
                steps=[["click", "testBtn"]])
    assert ui["text"]["testSpeed"] == "not measured: the engine did not answer the test: x"
    assert ui["text"]["testVerdict"].startswith("12.5 s for one verdict on a one-sentence sample (2 reference answers, judge model " + JUDGE)
    # a refusal is shown, and the figures already on screen are not touched
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, machine_test=done)}],
                           "/api/machine-test": [{"status": 409, "data": {"error": "a test is already running"}}]},
                steps=[["click", "testBtn"]])
    assert ui["text"]["testMsg"] == "not done: a test is already running" and ui["text"]["testSpeed"].startswith("30 words per second")
    assert ui["disabled"]["testBtn"] is False
    # a test that measured nothing is not saved by the server, and the refresh that follows must not wipe it
    nothing = dict(done, throughput={"state": "not measured", "why": "x"}, verdict={"state": "not measured", "why": "y"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}], "/api/machine-test": [{"status": 200, "data": nothing}],
                           "/api/start": [{"status": 200, "data": {"rc": 0}}]}, steps=[["click", "testBtn"], ["click", "startBtn"]])
    assert ui["text"]["testSpeed"] == "not measured: x" and ui["text"]["testVerdict"] == "not measured: y"
    assert [c["path"] for c in ui["calls"]].count("/api/overview") == 2, "the refresh after the click did run"


# --- Earnings by role, and the projection to the season's end (C9) ---
# The figures are the programme's published rankings (`publish_under` writes them with the programme's own
# calculation, each day under the rule set the case names); the rates are read from `final_season_rules`, never
# written in a case; the chain's block interval is read from a node of the bench (`node`).
BLOCKS_PATH = "/cosmos/base/tendermint/v1beta1/blocks/"
DAY_BLOCKS = 17_280


def kit_set(monkeypatch, kit_min=5):
    """The judge presence rule set (decision 24) with a floor of the case's choosing, made known to the application
    for this case only: `rule_set_of` reads the published sets from the rules module."""
    import final_season_rules as R
    rules = R.kit_rules(kit_min)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS + (rules,))
    return rules


def publish_under(route, days, sets):
    """Like `publish`, each day under its own rule set (`sets[n]`), what the earlier days paid carried over."""
    import dataclasses
    import final_season_calc as SC
    docs, paid = [], 0
    for n, identities in enumerate(days):
        identities = [i if i.payout_address else dataclasses.replace(i, payout_address=f"dendra1{i.miner_id}")
                      for i in identities]
        doc = SC.to_json(SC.compute_day(n, identities, paid, sets[n]))
        paid += doc["total_paid_udndr"]
        route(f"ranking/day-{n:03d}.json", 200, doc)
        docs.append(doc)
    route(f"ranking/day-{len(days):03d}.json", 404, {"error": "not published"})
    return docs


def status_under(rules, day, end_height=None):
    """The service's status on `day` with the rules of that day, as `rules_for_day` names them."""
    import final_season_rules as R
    return dict(status_doc(day, end_height), rules=rules, rules_fingerprint=R.fingerprint(rules))


@pytest.fixture()
def node(app, monkeypatch):
    """A node of the bench that serves block headers (`put(height, epoch)`) and keeps the paths it was asked for."""
    A, _ = app
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Programme)
    srv.routes, srv.seen = {}, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(A, "rest_base", lambda env: f"http://127.0.0.1:{srv.server_address[1]}")

    def put(height, epoch):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".123456789Z"
        srv.routes[BLOCKS_PATH + str(height)] = (200, json.dumps({"block": {"header": {"time": stamp}}}).encode())

    def raw(height, code, body):
        srv.routes[BLOCKS_PATH + str(height)] = (code, body)
    put.raw = raw
    yield put, srv.seen
    srv.shutdown()


def view_of(A, base, mid="dm1abc"):
    return A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, mid)


def clock_scene(A, route, put, ranked_requests, rules, day=11, secs=5):
    """Days ranked with `ranked_requests` verified requests each (10 windows proven), the chain at `secs` seconds a
    block, the status on `day` ten days before the season's end."""
    import final_season_calc as SC
    days = [[SC.Identity("dm1abc", presence=10, verified_requests=n)] for n in ranked_requests]
    docs = publish_under(route, days, [rules] * len(days))
    st = status_under(rules, day)
    route("status", 200, st)
    put(st["height"] - DAY_BLOCKS, st["latest_block_time"] - DAY_BLOCKS * secs)
    return docs, st


def test_the_formula_names_the_judge_presence_and_the_probation(app):
    # Decision 24 adds the judge presence reward and the probation share to the gross. The probation is applied
    # last, on the whole gross, so its part is what it takes off the sum of the others: the parts add up to the
    # gross whichever way it falls. A set without them refuses a row that carries one.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    kit = R.kit_rules(5)
    f = A.formula_of(40, 3, 1, kit, 1, False)
    assert f == {"work_udndr": 3 * kit["work_per_request"], "juror_udndr": kit["juror_per_verdict"],
                 "presence_udndr": 40 * kit["presence_per_window"], "judge_udndr": R.JUDGE_ACTIVE_PER_DAY,
                 "probation_udndr": 0, "gross_udndr": f["work_udndr"] + f["juror_udndr"] + f["presence_udndr"]
                 + R.JUDGE_ACTIVE_PER_DAY}
    p = A.formula_of(40, 3, 1, kit, 1, True)
    assert p["gross_udndr"] == SC.gross_of(SC.Identity("x", 40, 3, 1, judge_active=1, probation=True), kit)
    assert p["gross_udndr"] == f["gross_udndr"] * R.PROBATION_FIRST_DAY_BPS // 10_000
    assert p["probation_udndr"] < 0 and p["judge_udndr"] == R.JUDGE_ACTIVE_PER_DAY
    assert sum(p[k] for k in ("work_udndr", "juror_udndr", "presence_udndr", "judge_udndr", "probation_udndr")) \
        == p["gross_udndr"]
    # no judge day: no reward, whatever the set
    assert A.formula_of(40, 3, 1, kit, 0, False)["judge_udndr"] == 0
    # a set that has neither refuses a row that carries one
    for facts in ((1, False), (0, True)):
        with pytest.raises(ValueError):
            A.formula_of(40, 3, 1, R.RULES_DOUBLED, *facts)


def test_a_ranking_row_publishes_the_judge_facts_only_under_the_set_that_pays_them(app, monkeypatch):
    # Under the judge presence set every row carries `judge_active` (0 or 1) and `probation` (true or false):
    # required and typed, since a row without them would be read as a day without a judge reward. Under an
    # earlier set a row carries neither and they are 0 and false. A set not known gives no reading of them.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    kit = kit_set(monkeypatch)
    me = [SC.Identity("dm1abc", presence=10, verified_requests=2, judge_active=1, kit_version=5,
                      payout_address="dendra1dm1abc")]
    doc = SC.to_json(SC.compute_day(0, me, 0, kit))
    row = A.ranked_row(0, doc, "dm1abc")
    assert (row["judge_active"], row["probation"]) == (1, False) and row["rules_differ"] is False
    assert row["formula"]["judge_udndr"] == R.JUDGE_ACTIVE_PER_DAY
    assert row["formula"]["gross_udndr"] == row["gross_udndr"] == row_of(doc)["gross_udndr"]
    assert row["rules_fingerprint"] == R.fingerprint(kit)
    mine = row_of(doc)

    def read(**change):
        bad = {k: v for k, v in mine.items() if k not in change}
        bad.update({k: v for k, v in change.items() if v != "absent"})
        return A.ranked_row(0, dict(doc, ranking=[bad]), "dm1abc")
    for change in ({"judge_active": "absent"}, {"probation": "absent"},
                   {"judge_active": "absent", "probation": "absent"}, {"judge_active": 2}, {"judge_active": -1},
                   {"judge_active": True}, {"judge_active": "1"}, {"judge_active": 1.0}, {"probation": 1},
                   {"probation": 0}, {"probation": "false"}, {"probation": None}):
        with pytest.raises(ValueError):
            read(**change)
    assert read(probation=True)["probation"] is True
    # the reader refuses them itself, before the formula would (a probation of 1 is refused there too)
    for bad in ({}, {"judge_active": 1}, {"probation": False}, {"judge_active": True, "probation": False},
                {"judge_active": 1, "probation": 0}, {"judge_active": 1, "probation": "false"},
                {"judge_active": 1, "probation": None}):
        with pytest.raises(ValueError):
            A._judge_facts(bad, True)
    assert A._judge_facts({"judge_active": 1, "probation": True}, True) == (1, True)
    assert A._judge_facts({}, False) == (0, False)
    # an earlier set: neither is published, both are zero by its definition ...
    old = [SC.Identity("dm1abc", presence=40, verified_requests=3, payout_address="dendra1dm1abc")]
    d0 = SC.to_json(SC.compute_day(0, old, 0, R.RULES_DOUBLED))
    assert "judge_active" not in row_of(d0)
    r0 = A.ranked_row(0, d0, "dm1abc")
    assert (r0["judge_active"], r0["probation"]) == (0, False) and r0["formula"]["gross_udndr"] == r0["gross_udndr"]
    # ... and a row of that set that claims a judge day is a ranking not read, not a day with a reward
    with pytest.raises(ValueError):
        A.ranked_row(0, dict(d0, ranking=[dict(row_of(d0), judge_active=1, probation=False)]), "dm1abc")
    # a set this application does not know: no formula, and the two facts are not read (None, not 0)
    r1 = A.ranked_row(0, dict(doc, rules_fingerprint="0" * 64), "dm1abc")
    assert r1["judge_active"] is None and r1["probation"] is None and r1["formula"] is None
    assert r1["rules_differ"] is True
    # an identity not in the ranking had no fact that day
    r2 = A.ranked_row(0, doc, "someone")
    assert (r2["listed"], r2["judge_active"], r2["probation"]) == (False, 0, False)


def test_the_earnings_are_summed_by_role_over_the_ranked_days(app, programme, monkeypatch):
    # Each day's parts are the published formula rerun on its published facts under the set the day was ranked
    # under: work, verdicts, presence, the judge presence reward and the probation share. The expected figures are
    # written from the rate tables of the rules module, and the gross and the paid are the rankings' own.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    kit = kit_set(monkeypatch)
    W, D, K = R.RULES, R.RULES_DOUBLED, kit
    sets = [W, D, K, K]
    docs = publish_under(route, [
        [SC.Identity("dm1abc", presence=10, verified_requests=2), SC.Identity("someone", verified_requests=9)],
        [SC.Identity("dm1abc", presence=40, verified_requests=3, verdicts=1)],
        [SC.Identity("dm1abc", presence=20, verified_requests=4, verdicts=2, judge_active=1, kit_version=5)],
        [SC.Identity("dm1abc", verified_requests=5, verdicts=1, judge_active=1, probation=True, kit_version=5),
         SC.Identity("someone", verified_requests=9)]], sets)
    route("status", 200, status_under(K, 6, end_height=100 + DAY_BLOCKS * 6 - 1))
    e = view_of(A, base)["earnings"]
    roles = e["roles"]
    plain3 = 5 * K["work_per_request"] + K["juror_per_verdict"]
    gross3 = (plain3 + R.JUDGE_ACTIVE_PER_DAY) * R.PROBATION_FIRST_DAY_BPS // 10_000
    want = {"days": 4, "of": 4,
            "work_udndr": 2 * W["work_per_request"] + 3 * D["work_per_request"] + 9 * K["work_per_request"],
            "juror_udndr": D["juror_per_verdict"] + 3 * K["juror_per_verdict"],
            "presence_udndr": 10 * W["presence_per_window"] + 40 * D["presence_per_window"]
            + 20 * K["presence_per_window"],
            "judge_udndr": 2 * R.JUDGE_ACTIVE_PER_DAY,
            "probation_udndr": gross3 - plain3 - R.JUDGE_ACTIVE_PER_DAY}
    for k, v in want.items():
        assert roles[k] == v, k
    # the parts add up to the gross, which is the sum of the rankings' own gross figures
    assert roles["gross_udndr"] == sum(roles[k] for k in ("work_udndr", "juror_udndr", "presence_udndr",
                                                           "judge_udndr", "probation_udndr"))
    assert roles["gross_udndr"] == sum(row_of(d)["gross_udndr"] for d in docs)
    assert roles["payable_udndr"] == sum(row_of(d)["payable_udndr"] for d in docs)
    assert roles["paid_udndr"] == sum(row_of(d)["paid_udndr"] for d in docs) == view_of(A, base)["season_paid_udndr"]
    assert roles["probation_udndr"] < 0 and roles["judge_udndr"] > 0


def test_a_day_whose_gross_the_formula_does_not_give_is_left_out_and_counted(app, programme):
    # A gross the rerun formula does not give (another copy of the rules) or a set not known would put a sum
    # that is neither the ranking's nor the formula's on the screen: such a day is left out of the parts, the
    # gross and the sums beside them, the count says how many days were read, and a total over none is None.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    docs = publish_under(route, [[SC.Identity("dm1abc", presence=10, verified_requests=n)] for n in (1, 2, 3)],
                         [R.RULES_DOUBLED] * 3)
    route("status", 200, status_under(R.RULES_DOUBLED, 6, end_height=100 + DAY_BLOCKS * 6 - 1))
    full = view_of(A, base)["earnings"]["roles"]
    assert (full["days"], full["of"]) == (3, 3)
    bad = dict(row_of(docs[1]), gross_udndr=row_of(docs[1])["gross_udndr"] + 1)
    route("ranking/day-001.json", 200, dict(docs[1], ranking=[bad]))
    part = view_of(A, base)["earnings"]["roles"]
    assert (part["days"], part["of"]) == (2, 3)
    rate = R.RULES_DOUBLED["work_per_request"]
    assert part["work_udndr"] == (1 + 3) * rate and part["paid_udndr"] == row_of(docs[0])["paid_udndr"] + row_of(docs[2])["paid_udndr"]
    route("ranking/day-001.json", 200, dict(docs[1], rules_fingerprint="0" * 64))
    assert view_of(A, base)["earnings"]["roles"]["days"] == 2
    for n in (0, 1, 2):
        route(f"ranking/day-{n:03d}.json", 200, dict(docs[n], rules_fingerprint="0" * 64))
    none = view_of(A, base)["earnings"]["roles"]
    assert (none["days"], none["of"]) == (0, 3)
    for k in ("work_udndr", "juror_udndr", "presence_udndr", "judge_udndr", "probation_udndr", "gross_udndr",
              "payable_udndr", "paid_udndr"):
        assert none[k] is None, k
    # no day ranked: no sums, and no projection either
    route("status", 200, status_under(R.RULES_DOUBLED, 0))
    e = view_of(A, base)["earnings"]
    assert e["roles"] is None and e["projection"] == {"available": False, "why": "no day is ranked yet"}


def test_the_projection_counts_to_the_end_from_the_chains_clock_and_names_how(app, programme, node):
    # The season ends at a fixed instant; before the chain passes it, the blocks left are counted from the
    # clock at the interval measured between the latest block and the block one day of blocks earlier, a
    # part-day counted for its blocks. The average is that of the last 7 ranked days of the rules in force.
    A, _ = app
    import final_season_rules as R
    base, route = programme
    put, seen = node
    D = R.RULES_DOUBLED
    docs, st = clock_scene(A, route, put, range(1, 10), D)
    pr = view_of(A, base)["earnings"]["projection"]
    assert pr["available"] is True and pr["window_days"] == A.PROJECTION_DAYS == 7
    assert (pr["first_day"], pr["last_day"]) == (2, 8)
    # days 2..8 asked 3..9 requests: 6 on average; 10 windows a day; no verdict, no judge day
    assert pr["per_day"]["work_udndr"] == 6 * D["work_per_request"]
    assert pr["per_day"]["presence_udndr"] == 10 * D["presence_per_window"]
    assert pr["per_day"]["juror_udndr"] == pr["per_day"]["judge_udndr"] == pr["per_day"]["probation_udndr"] == 0
    assert pr["per_day"]["paid_udndr"] == sum(row_of(d)["paid_udndr"] for d in docs[2:]) // 7
    secs = 5
    blocks = st["height"] + (R.end_epoch() - st["latest_block_time"]) // secs - (st["start_height"] + 9 * DAY_BLOCKS) + 1
    assert pr["end"] == {"blocks": blocks, "how": "clock", "block_seconds": 5.0, "span_blocks": DAY_BLOCKS,
                         "days": round(blocks / DAY_BLOCKS, 2)}
    assert 12.0 <= pr["end"]["days"] < 12.01
    assert pr["to_end"]["work_udndr"] == 6 * D["work_per_request"] * blocks // DAY_BLOCKS
    assert pr["to_end"]["paid_udndr"] == pr["per_day"]["paid_udndr"] * blocks // DAY_BLOCKS
    assert seen == [BLOCKS_PATH + str(st["height"] - DAY_BLOCKS)]
    # a chain slower than 5 s a block has fewer blocks left to the same instant: the interval is measured
    put(st["height"] - DAY_BLOCKS, st["latest_block_time"] - DAY_BLOCKS * 10)
    A._BLOCKS.clear()
    slow = view_of(A, base)["earnings"]["projection"]["end"]
    assert slow["block_seconds"] == 10.0
    assert slow["blocks"] == st["height"] + (R.end_epoch() - st["latest_block_time"]) // 10 \
        - (st["start_height"] + 9 * DAY_BLOCKS) + 1


def test_the_projection_counts_to_the_last_block_once_the_chain_has_passed_the_end(app, programme, node):
    # After the end the status names the season's last block (`end_height`): the days left are a reading and the
    # node is not asked for the interval. The last day is cut where the season ends.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    put, seen = node
    D = R.RULES_DOUBLED
    publish_under(route, [[SC.Identity("dm1abc", presence=10, verified_requests=n)] for n in range(1, 10)], [D] * 9)
    end = 100 + DAY_BLOCKS * 11 + DAY_BLOCKS // 2 - 1          # the middle of day index 11, the season's last
    route("status", 200, status_under(D, 12, end_height=end))
    pr = view_of(A, base)["earnings"]["projection"]
    assert pr["end"] == {"blocks": 2 * DAY_BLOCKS + DAY_BLOCKS // 2, "how": "last_block", "end_height": end, "days": 2.5}
    assert pr["to_end"]["work_udndr"] == pr["per_day"]["work_udndr"] * 5 // 2
    assert seen == []
    # every day ranked: nothing left, and that is a reading (0), not a missing one
    route("status", 200, status_under(D, 12, end_height=100 + DAY_BLOCKS * 9 - 1))
    pr = view_of(A, base)["earnings"]["projection"]
    assert pr["end"]["blocks"] == 0 and pr["to_end"]["paid_udndr"] == 0 and pr["available"] is True
    # a last block before the first unranked one is not a negative number of days
    route("status", 200, status_under(D, 12, end_height=100 + DAY_BLOCKS * 3))
    assert view_of(A, base)["earnings"]["projection"]["end"]["blocks"] == 0


def test_the_projection_averages_only_the_days_of_the_rules_in_force_and_leaves_probation_out(app, programme, monkeypatch):
    # Days ranked under another set were paid at other rates, and a day on probation earned a share of a normal
    # day: neither says what the next days will bring. A day on which the identity is not listed counts as the
    # zero it is.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    kit = kit_set(monkeypatch)
    W, D, K = R.RULES, R.RULES_DOUBLED, kit
    day = lambda n, **kw: [SC.Identity("dm1abc", presence=10, verified_requests=n, kit_version=5, **kw)]  # noqa: E731
    other = [SC.Identity("someone", verified_requests=3)]
    docs = publish_under(route, [day(50), day(60), day(2, judge_active=1, probation=True), day(4), other,
                                 day(6, judge_active=1)], [W, D, K, K, K, K])
    route("status", 200, status_under(K, 8, end_height=100 + DAY_BLOCKS * 8 - 1))
    pr = view_of(A, base)["earnings"]["projection"]
    # days 3, 5 and 6 (index 3, 4, 5) under the kit set; index 2 is a probation day; 0 and 1 are other sets
    assert pr["window_days"] == 3 and (pr["first_day"], pr["last_day"]) == (3, 5)
    assert pr["per_day"]["work_udndr"] == (4 + 0 + 6) * K["work_per_request"] // 3
    assert pr["per_day"]["judge_udndr"] == 1 * R.JUDGE_ACTIVE_PER_DAY // 3
    assert pr["per_day"]["probation_udndr"] == 0
    assert pr["per_day"]["paid_udndr"] == (row_of(docs[3])["paid_udndr"] + 0 + row_of(docs[5])["paid_udndr"]) // 3
    # only a probation day under the rules in force: nothing to average
    route("status", 200, status_under(K, 3, end_height=100 + DAY_BLOCKS * 8 - 1))
    assert view_of(A, base)["earnings"]["projection"] == {
        "available": False, "why": "no ranked day yet under the rules in force today (a probation day does not count)"}
    # no ranked day under the rules in force: the earlier rates are not carried over
    route("status", 200, status_under(K, 2, end_height=100 + DAY_BLOCKS * 8 - 1))
    assert view_of(A, base)["earnings"]["projection"]["why"].startswith("no ranked day yet under the rules in force")
    # the window is the last 7 days, not every day
    many = [[SC.Identity("dm1abc", presence=10, verified_requests=n)] for n in [100] + [1] * 7]
    publish_under(route, many, [D] * 8)
    route("status", 200, status_under(D, 10, end_height=100 + DAY_BLOCKS * 10 - 1))
    p8 = view_of(A, base)["earnings"]["projection"]
    assert p8["window_days"] == 7 and p8["first_day"] == 1 and p8["per_day"]["work_udndr"] == D["work_per_request"]


def test_a_projection_that_cannot_be_made_says_why_and_a_missing_end_leaves_the_total_out(app, programme, node):
    A, _ = app
    import final_season_rules as R
    base, route = programme
    put, seen = node
    D = R.RULES_DOUBLED
    docs, st = clock_scene(A, route, put, range(1, 4), D, day=11)
    view = lambda: view_of(A, base)["earnings"]["projection"]  # noqa: E731
    assert view()["available"] is True

    def end_unread(why, **change):
        route("status", 200, dict(st, **change))
        A._BLOCKS.clear()
        pr = view()
        assert pr["available"] is True and pr["to_end"] is None and pr["end"] == {"blocks": None, "why": why}, change
        assert pr["per_day"]["paid_udndr"] == sum(row_of(d)["paid_udndr"] for d in docs) // 3
    # the season is over but its last block is not found: no guess
    end_unread("the season is over but its last block is not found yet", ended=True, end_height=None)
    end_unread("the season is over but its last block is not found yet", ended=True, end_height="9")
    # whether it is over is not read
    end_unread("whether the season is over is not read", ended=None)
    end_unread("whether the season is over is not read", ended="false")
    # the chain's clock is past the end but the status says it runs
    end_unread("the chain's clock is past the end but the status says the season runs",
               latest_block_time=R.end_epoch() + 5)
    # the status does not give the latest block
    for change in ({"height": None}, {"height": True}, {"height": "100"}, {"latest_block_time": None},
                   {"latest_block_time": 1.5}):
        end_unread("the status does not give the latest block's height and time", **change)
    end_unread("the status does not give the season's first block", start_height=None)
    # a chain younger than one season day of blocks has no interval to measure
    end_unread("the chain has not yet run one season day of blocks", height=DAY_BLOCKS)
    # a node that does not answer, or answers a time that is not one, or whose clock does not advance
    route("status", 200, st)
    earlier = st["height"] - DAY_BLOCKS
    for bad in (None, (404, b"{}"), (200, b"{}"), (200, b"not json"),
                (200, json.dumps({"block": {"header": {"time": "yesterday"}}}).encode()),
                (200, json.dumps({"block": {"header": {"time": 12}}}).encode())):
        A._BLOCKS.clear()
        if bad is None:
            put(earlier, st["latest_block_time"] + 1)          # a clock that does not advance
        else:
            put.raw(earlier, *bad)
        pr = view()
        assert pr["to_end"] is None and pr["end"] == {
            "blocks": None, "why": "the node did not answer with the time of an earlier block"}, bad
    # a clock that stands still is no interval either
    put(earlier, st["latest_block_time"])
    A._BLOCKS.clear()
    assert view()["end"]["blocks"] is None
    # a status the rules of which this application does not know
    route("status", 200, dict(st, rules_fingerprint="0" * 64))
    assert view() == {"available": False, "why": "the rules of the current day are not known to this application"}


def test_the_block_interval_is_measured_once_in_a_while_not_at_every_refresh(app, programme, node, monkeypatch):
    A, _ = app
    import final_season_rules as R
    base, route = programme
    put, seen = node
    clock_scene(A, route, put, range(1, 4), R.RULES_DOUBLED)
    first = view_of(A, base)["earnings"]["projection"]["end"]
    again = view_of(A, base)["earnings"]["projection"]["end"]
    assert first == again and len(seen) == 1
    monkeypatch.setattr(A, "BLOCK_TTL_S", 0)
    view_of(A, base)
    assert len(seen) == 2


def test_a_projection_over_a_day_the_formula_does_not_give_is_refused(app, programme, node):
    A, _ = app
    import final_season_rules as R
    base, route = programme
    put, seen = node
    docs, st = clock_scene(A, route, put, range(1, 4), R.RULES_DOUBLED)
    bad = dict(row_of(docs[2]), gross_udndr=row_of(docs[2])["gross_udndr"] + 1)
    route("ranking/day-002.json", 200, dict(docs[2], ranking=[bad]))
    assert view_of(A, base)["earnings"]["projection"] == {
        "available": False, "why": "a ranked day's gross differs from the published formula rerun on its facts"}


def test_the_overview_carries_the_earnings_and_a_failed_read_carries_none(app, programme, node, monkeypatch):
    A, kit = app
    import final_season_rules as R
    base, route = programme
    put, seen = node
    clock_scene(A, route, put, range(1, 4), R.RULES_DOUBLED)
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    ov = call(A, "/api/overview")[1]
    e = ov["programme"]["earnings"]
    assert e["roles"]["days"] == 3 and e["projection"]["available"] is True
    assert e["roles"]["paid_udndr"] == ov["programme"]["season_paid_udndr"]
    # a ranking that cannot be read is no earnings, not earnings of 0
    route("ranking/day-001.json", 502, b"bad gateway")
    assert call(A, "/api/overview")[1]["programme"]["earnings"] is None
    # no programme address: nothing to read
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    assert call(A, "/api/overview")[1]["programme"]["earnings"] is None


# What the page writes of the earnings, from the figures the server sends (written by hand: this half of the
# bench is the page's script, under node).
ROLES = {"days": 3, "of": 3, "work_udndr": 1_500_000, "juror_udndr": 80_000, "presence_udndr": 200_000,
         "judge_udndr": 1_000_000, "probation_udndr": -300_000, "gross_udndr": 2_480_000,
         "payable_udndr": 2_480_000, "paid_udndr": 2_000_000}
TO_END = {"work_udndr": 2_000_000, "juror_udndr": 160_000, "presence_udndr": 400_000, "judge_udndr": 1_000_000,
          "probation_udndr": 0, "gross_udndr": 3_560_000, "payable_udndr": 3_560_000, "paid_udndr": 1_750_000}
PER_DAY = dict(TO_END, paid_udndr=700_000)
CLOCK_END = {"blocks": 207_366, "how": "clock", "block_seconds": 5.0, "span_blocks": 17_280, "days": 12.0}
LAST_END = {"blocks": 43_200, "how": "last_block", "end_height": 190_000, "days": 2.5}


def earnings_page(A, tmp_path, earnings, rules=None, **programme):
    import final_season_rules as R
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=True, rules=R.RULES_DOUBLED if rules is None else rules)
    prog = dict({"enabled": True, "status": {"day": 5, "rules": R.RULES_DOUBLED, "ended": False, "last_day": None},
                 "ranked_days": 3, "season_paid_udndr": 2_000_000, "latest": None, "earnings": earnings}, **programme)
    return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=prog)}]})["text"]


def test_the_page_writes_the_earnings_by_role_and_a_projection_that_names_its_hypothesis(app, tmp_path):
    A, _ = app
    proj = {"available": True, "window_days": 7, "first_day": 2, "last_day": 8, "per_day": PER_DAY,
            "end": CLOCK_END, "to_end": TO_END}
    t = earnings_page(A, tmp_path, {"roles": ROLES, "projection": proj})
    assert t["earnWork"] == "1.5 DNDR; projected to the end: 2 DNDR more"
    assert t["earnJuror"] == "0.08 DNDR; projected to the end: 0.16 DNDR more"
    assert t["earnPresence"] == "0.2 DNDR; projected to the end: 0.4 DNDR more"
    assert t["earnJudge"] == "1 DNDR; projected to the end: 1 DNDR more"
    assert t["earnProbation"] == "-0.3 DNDR; projected to the end: 0 DNDR more"
    assert t["earnGross"] == "2.48 DNDR; projected to the end: 3.56 DNDR more"
    assert t["earnPaid"] == "2 DNDR; projected to the end: 1.75 DNDR more"
    assert t["earnCoverage"] == ""
    # the average, its window, the end and how it was counted are in one sentence, and it says "about"
    assert t["earnProjection"] == (
        "At the average of the last 7 ranked day(s) under today's rules (days 3 to 9; a first-day probation day is "
        "left out), this identity is paid 0.7 DNDR a day; counting about 12 day(s) to the season's end (the end "
        "read from the chain's clock at 5 s per block, measured over the last 17280 blocks, and a part-day counted "
        "for its blocks), which comes to 1.75 DNDR more.")
    # the page shows the daily cap and the pro rata, from the rules the server sends
    assert t["earnLimits"].startswith("A projection, not a promise. The programme pays at most 100 DNDR a day to all "
                                      "identities together, shared pro rata beyond, and 3,000 DNDR over the season.")
    assert "weekly payment" in t["earnLimits"]
    import final_season_rules as R
    t = earnings_page(A, tmp_path, {"roles": ROLES, "projection": proj}, rules=dict(R.RULES, cap_programme_day=7_000_000))
    assert "at most 7 DNDR a day" in t["earnLimits"] and "1,500 DNDR over the season" in t["earnLimits"]
    # the season's last block known: the days left are a reading
    last = dict(proj, end=LAST_END, first_day=4, last_day=4, window_days=1)
    t = earnings_page(A, tmp_path, {"roles": ROLES, "projection": last})
    assert t["earnProjection"] == (
        "At the average of the last 1 ranked day(s) under today's rules (day 5; a first-day probation day is left "
        "out), this identity is paid 0.7 DNDR a day; the season's last block is 190000, so 2.5 day(s) are still to "
        "be ranked, a part-day counted for its blocks, which comes to 1.75 DNDR more.")


def test_the_page_says_what_it_cannot_project_and_never_shows_a_missing_figure_as_zero(app, tmp_path):
    A, _ = app
    off = {"available": False, "why": "no ranked day yet under the rules in force today (a probation day does not count)"}
    t = earnings_page(A, tmp_path, {"roles": ROLES, "projection": off})
    assert t["earnProjection"] == "No projection: " + off["why"] + "."
    assert t["earnWork"] == "1.5 DNDR" and t["earnPaid"] == "2 DNDR"          # no projected figure beside them
    # the end cannot be counted: the per-day figure stays, the total is not made up
    noend = {"available": True, "window_days": 3, "first_day": 0, "last_day": 2, "per_day": PER_DAY,
             "end": {"blocks": None, "why": "the season is over but its last block is not found yet"}, "to_end": None}
    t = earnings_page(A, tmp_path, {"roles": ROLES, "projection": noend})
    assert t["earnProjection"] == (
        "At the average of the last 3 ranked day(s) under today's rules (days 1 to 3; a first-day probation day is "
        "left out), this identity is paid 0.7 DNDR a day. The days left to the season's end cannot be counted: the "
        "season is over but its last block is not found yet.")
    assert "more" not in t["earnWork"]
    # the page names the days left out of the sums
    t = earnings_page(A, tmp_path, {"roles": dict(ROLES, days=2), "projection": off})
    assert t["earnCoverage"].startswith("Read for 2 of 3 ranked days")
    # no day could be read: each figure is a question mark, not 0
    nothing = dict.fromkeys(ROLES, None)
    nothing.update(days=0, of=3)
    t = earnings_page(A, tmp_path, {"roles": nothing, "projection": off})
    assert t["earnWork"] == t["earnGross"] == t["earnPaid"] == t["earnJudge"] == "?"
    assert t["earnCoverage"].startswith("Read for 0 of 3 ranked days")
    # no day ranked: nothing, and not a 0
    t = earnings_page(A, tmp_path, {"roles": None, "projection": {"available": False, "why": "no day is ranked yet"}},
                      ranked_days=0)
    assert t["earnWork"] == "—" and t["earnProjection"] == "No projection: no day is ranked yet."
    # the rankings were not read: a question mark everywhere
    t = earnings_page(A, tmp_path, None, ranked_days=None, season_paid_udndr=None)
    assert t["earnWork"] == t["earnPaid"] == "?" and t["earnProjection"] == ""
    # no rules, no cap sentence
    t = earnings_page(A, tmp_path, {"roles": ROLES, "projection": off}, rules={})
    assert t["earnLimits"] == ""
    # no programme address: nothing to say
    t = earnings_page(A, tmp_path, None, enabled=False)
    assert t["earnWork"] == t["earnPaid"] == "—" and t["earnProjection"] == "" and t["earnLimits"] == ""


def test_the_page_names_the_judge_presence_and_the_probation_in_the_latest_day(app, tmp_path):
    A, _ = app
    latest = {"day": 3, "listed": True, "presence": 20, "verified_requests": 5, "verdicts": 1, "gross_udndr": 609_600,
              "payable_udndr": 609_600, "paid_udndr": 609_600, "reason": "", "rules_differ": False,
              "formula": {"work_udndr": 500_000, "juror_udndr": 16_000, "presence_udndr": 0, "judge_udndr": 500_000,
                          "probation_udndr": -406_400, "gross_udndr": 609_600}}
    t = earnings_page(A, tmp_path, None, latest=latest)
    assert t["lastParts"] == ("The formula on these facts: work 0.5 + juror 0.016 + presence 0 + judge presence 0.5 "
                              "- probation 0.4064 = 0.6096 DNDR gross.")
    plain = dict(latest, formula=dict(latest["formula"], judge_udndr=0, probation_udndr=0, gross_udndr=516_000),
                 gross_udndr=516_000, payable_udndr=516_000, paid_udndr=516_000)
    assert earnings_page(A, tmp_path, None, latest=plain)["lastParts"] == (
        "The formula on these facts: work 0.5 + juror 0.016 + presence 0 = 0.516 DNDR gross.")


def test_the_earnings_page_reads_what_the_server_reads_end_to_end(app, programme, node, tmp_path, monkeypatch):
    # The server's reading of a real scenario, then the page's script on it.
    A, kit = app
    import final_season_rules as R
    base, route = programme
    put, seen = node
    D = R.RULES_DOUBLED
    docs, st = clock_scene(A, route, put, range(1, 4), D)
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=" + base + NL)
    no_docker(A, monkeypatch)
    ov = call(A, "/api/overview")[1]
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})["text"]
    e = ov["programme"]["earnings"]
    fmt = lambda u: f"{u / 1e6:,.6f}".rstrip("0").rstrip(".") + " DNDR"  # noqa: E731
    work = (1 + 2 + 3) * D["work_per_request"]
    assert e["roles"]["work_udndr"] == work
    assert t["earnWork"] == fmt(work) + "; projected to the end: " + fmt(e["projection"]["to_end"]["work_udndr"]) + " more"
    assert t["earnPaid"] == fmt(sum(row_of(d)["paid_udndr"] for d in docs)) + "; projected to the end: " + fmt(
        e["projection"]["to_end"]["paid_udndr"]) + " more"
    assert "At the average of the last 3 ranked day(s) under today's rules (days 1 to 3;" in t["earnProjection"]
    assert "at most 100 DNDR a day" in t["earnLimits"]


def test_the_payable_and_the_paid_are_the_rankings_own_and_the_projection_averages_what_was_paid(
        app, programme, monkeypatch):
    # The gross becomes payable when an address, the kit and the registration allow it, and paid after the day's
    # pro rata: three figures, summed as the rankings state them, and the projection averages what was paid.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    kit = kit_set(monkeypatch)
    D, K = R.RULES_DOUBLED, kit
    crowd = [SC.Identity(f"dm1x{n:02d}", presence=50, verified_requests=40, kit_version=5) for n in range(30)]
    docs = publish_under(route, [
        [SC.Identity("dm1abc", presence=10, verified_requests=2)],                              # before the set
        [SC.Identity("dm1abc", presence=50, verified_requests=40, kit_version=5)] + crowd,     # the daily cap bites
        [SC.Identity("dm1abc", presence=10, verified_requests=2, kit_version=4)],              # a kit below the floor
        [SC.Identity("dm1abc", presence=10, verified_requests=2, kit_version=5)]], [D, K, K, K])
    zero, one, two, three = (row_of(d) for d in docs)
    assert one["paid_udndr"] < one["payable_udndr"] == one["gross_udndr"] > 0
    assert two["gross_udndr"] > 0 == two["payable_udndr"] == two["paid_udndr"]
    route("status", 200, status_under(K, 5, end_height=100 + DAY_BLOCKS * 5 - 1))
    rows = (zero, one, two, three)
    roles = view_of(A, base)["earnings"]["roles"]
    for fig in ("gross", "payable", "paid"):
        assert roles[fig + "_udndr"] == sum(r[fig + "_udndr"] for r in rows), fig
    assert roles["gross_udndr"] > roles["payable_udndr"] > roles["paid_udndr"]
    # the projection averages the days of the set in force (the last three), and what they were paid, not their gross
    pr = view_of(A, base)["earnings"]["projection"]
    assert pr["window_days"] == 3 and (pr["first_day"], pr["last_day"]) == (1, 3)
    for fig in ("gross", "payable", "paid"):
        assert pr["per_day"][fig + "_udndr"] == sum(r[fig + "_udndr"] for r in rows[1:]) // 3, fig
    assert pr["per_day"]["paid_udndr"] < pr["per_day"]["payable_udndr"] < pr["per_day"]["gross_udndr"]


# --- The set of decision 24 in a process that was not given the kit floor (C9) ---
# The application never sets DENDRA_KIT_MIN: the service publishes the floor with the set (`status.rules.kit_min`
# and, on every ranking of that set, `inputs.kit_floor.kit_min`), and the rules module of this process does not hold
# the set. The cases below publish under the set (the service's side, which holds it) and then drop it from the
# rules module before the application reads: `kit_set` alone leaves it known to the reader, which hides the path.


def forget_kit_set(monkeypatch):
    """The rules module of an application without DENDRA_KIT_MIN: the published sets only."""
    import final_season_rules as R
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS)
    monkeypatch.setattr(R, "RULES_KIT", None)
    monkeypatch.setattr(R, "KIT_MIN", None)
    assert R.rule_set_of(R.fingerprint(R.kit_rules(5)))[1] is None


def with_floor(doc, kit_min):
    """A ranking as the service publishes it under decision 24: the floor under `inputs.kit_floor`."""
    return dict(doc, inputs={"kit_floor": {"kit_min": kit_min}})


def kit_day(monkeypatch, **facts):
    """One day of dm1abc ranked under the set of decision 24 (floor 5), and the set itself."""
    import final_season_calc as SC
    kit = kit_set(monkeypatch)
    me = [SC.Identity("dm1abc", presence=10, verified_requests=2, judge_active=1, kit_version=5,
                      payout_address="dendra1dm1abc", **facts)]
    return SC.to_json(SC.compute_day(0, me, 0, kit)), kit


def test_a_day_of_the_judge_presence_set_is_rerun_without_the_floor_in_the_environment(app, monkeypatch):
    # The floor comes from the day's own published inputs, or from the status; the set built from it is taken only
    # when its fingerprint is the day's. A floor that names another set, or is not a kit number, finds nothing.
    A, _ = app
    import final_season_rules as R
    doc, kit = kit_day(monkeypatch)
    forget_kit_set(monkeypatch)
    for row in (A.ranked_row(0, with_floor(doc, 5), "dm1abc"), A.ranked_row(0, doc, "dm1abc", 5)):
        assert row["rules_differ"] is False and row["judge_active"] == 1 and row["probation"] is False
        assert row["formula"]["judge_udndr"] == R.JUDGE_ACTIVE_PER_DAY
        assert row["formula"]["gross_udndr"] == row["gross_udndr"] == row_of(doc)["gross_udndr"]
        assert row["rules_fingerprint"] == R.fingerprint(kit)
    # the process still does not hold the set: nothing was fixed by reading
    assert R.rule_set_of(R.fingerprint(kit))[1] is None and R.KIT_MIN is None
    # no floor at all, or one of another set: the set is not known, nothing is read of its two facts
    for read in (A.ranked_row(0, doc, "dm1abc"), A.ranked_row(0, with_floor(doc, 4), "dm1abc"),
                 A.ranked_row(0, doc, "dm1abc", 6), A.ranked_row(0, with_floor(doc, 4), "dm1abc", 6)):
        assert read["rules_differ"] is True and read["formula"] is None
        assert read["judge_active"] is None and read["probation"] is None
    # the published floor and the status's: either one that names the set is enough
    assert A.ranked_row(0, with_floor(doc, 4), "dm1abc", 5)["formula"] is not None
    assert A.ranked_row(0, with_floor(doc, 5), "dm1abc", 4)["formula"] is not None
    # a floor that is not a kit number is no floor: a bool is not 1, and 0 or a negative number is no kit
    for bad in (True, 0, -5, "5", 5.0, None, [5]):
        read = A.ranked_row(0, with_floor(doc, bad), "dm1abc", bad)
        assert read["rules_differ"] is True and read["formula"] is None, bad
    assert A.ranked_row(0, dict(doc, inputs={"kit_floor": 5}), "dm1abc")["rules_differ"] is True
    assert A.ranked_row(0, dict(doc, inputs=[5]), "dm1abc")["rules_differ"] is True


def test_the_rules_in_force_are_rebuilt_from_the_floor_the_status_gives(app, monkeypatch):
    # The status carries the set (`rules`, with `kit_min`) and its fingerprint: the screen's formula and the
    # projection use the set only when the two agree, and a status that names a set no floor explains gives none.
    A, _ = app
    import final_season_rules as R
    kit = kit_set(monkeypatch)
    forget_kit_set(monkeypatch)
    st = status_under(kit, 18)
    assert A.rules_in_force({"status": st}) == kit
    assert A.rules_in_force({"status": dict(st, rules_fingerprint=R.fingerprint(R.kit_rules(6)))}) is None
    assert A.rules_in_force({"status": dict(st, rules=R.kit_rules(6))}) is None
    assert A.rules_in_force({"status": dict(st, rules=dict(kit, kit_min=True))}) is None
    assert A.rules_in_force({"status": dict(st, rules=None)}) is None
    assert A.rules_in_force({"status": dict(st, rules_fingerprint="0" * 64)}) is None
    assert A.rules_in_force({"status": dict(st, rules_fingerprint=None)}) is None
    assert A.rules_in_force({"status": status_under(R.RULES_DOUBLED, 3)}) == R.RULES_DOUBLED
    assert A.rules_in_force({}) is None and A.rules_in_force({"status": None}) is None


def test_the_earnings_under_the_judge_presence_set_are_read_without_the_floor_in_the_environment(
        app, programme, monkeypatch):
    # The whole path, the way the application runs: the days are ranked under decision 24 by a service that holds
    # the set, the application does not. The judge presence reward is rerun from the floor the status gives (the
    # rankings here carry no inputs), the roles are summed over every day and the projection is made.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules as R
    base, route = programme
    kit = kit_set(monkeypatch)
    D = R.RULES_DOUBLED
    day = lambda n, **kw: [SC.Identity("dm1abc", presence=10, verified_requests=n, kit_version=5, **kw)]  # noqa: E731
    docs = publish_under(route, [day(3), day(2, judge_active=1), day(4, judge_active=1), day(5)], [D, kit, kit, kit])
    route("status", 200, status_under(kit, 4, end_height=100 + DAY_BLOCKS * 8 - 1))
    forget_kit_set(monkeypatch)
    view = view_of(A, base)
    roles = view["earnings"]["roles"]
    assert (roles["days"], roles["of"]) == (4, 4), "no day was left out for want of the set"
    assert roles["judge_udndr"] == 2 * R.JUDGE_ACTIVE_PER_DAY
    assert roles["gross_udndr"] == sum(row_of(d)["gross_udndr"] for d in docs)
    pr = view["earnings"]["projection"]
    assert pr["available"] is True and (pr["first_day"], pr["last_day"], pr["window_days"]) == (1, 3, 3)
    assert pr["per_day"]["judge_udndr"] == 2 * R.JUDGE_ACTIVE_PER_DAY // 3
    assert pr["to_end"] is not None and pr["end"]["how"] == "last_block"
    assert A.rules_in_force(view) == kit
    # the status gives no floor (it names an earlier set while the days are ranked under this one): the days are
    # left out and said so, and the projection averages only the day of the set the status names
    route("status", 200, status_under(D, 4, end_height=100 + DAY_BLOCKS * 8 - 1))
    part = view_of(A, base)["earnings"]
    assert (part["roles"]["days"], part["roles"]["of"]) == (1, 4)
    assert (part["projection"]["window_days"], part["projection"]["first_day"]) == (1, 0)
    # the same days with their own published floor are read whatever the status says
    for n in (1, 2, 3):
        route(f"ranking/day-{n:03d}.json", 200, with_floor(docs[n], 5))
    full = view_of(A, base)["earnings"]
    assert (full["roles"]["days"], full["roles"]["of"]) == (4, 4) and full["roles"]["judge_udndr"] == 2 * R.JUDGE_ACTIVE_PER_DAY


# --- The Windows browser (C11): deploy/app/open_windows.ps1 starts `dendra_app.py --windows` in WSL 2 ---
# What is real here: the application's main, its HTTP server on the loopback, the one-time token read from the
# standard input, the /handoff route and the access page it removes. What is not: the PowerShell side (the page
# it writes is made here with the same form), and WSL's forwarding of the port to Windows.
import io  # noqa: E402
import socket  # noqa: E402

ONCE = "A" * 20 + "b" * 20 + "-_9"          # 43 characters, the length of 32 bytes in URL-safe base64


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_HELD_INPUTS = []      # the write ends a case holds open, as open_windows.ps1 holds the application's input


@pytest.fixture(autouse=True)
def _release_held_inputs():
    yield
    while _HELD_INPUTS:
        try:
            os.close(_HELD_INPUTS.pop())
        except OSError:
            pass


def held_input(text):
    """A standard input like the one open_windows.ps1 gives: a pipe whose write end stays open while the case
    runs (its end stops the application), with `text` already written."""
    r, w = os.pipe()
    os.write(w, text.encode())
    _HELD_INPUTS.append(w)
    return os.fdopen(r, "r", encoding="utf-8")


def windows_launched(tmp_path, monkeypatch, stdin_text=ONCE + chr(10)):
    """The application as open_windows.ps1 starts it: an access page written beforehand, the token on the
    standard input, which stays open, a fixed port."""
    A, run, handed = launched(tmp_path, monkeypatch)
    page = tmp_path / "localappdata" / "Dendra" / "open-1.html"
    page.parent.mkdir(parents=True)
    page.write_text('<form method="post" action="/handoff"><input type="hidden" name="t" value="' + ONCE + '"></form>')
    monkeypatch.setattr(A.sys, "stdin", held_input(stdin_text))
    return A, run, handed, page, free_port()


def handoff(A, body, host=None, ctype="application/x-www-form-urlencoded", headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{A.APP.port}/handoff", data=body, method="POST")
    req.add_header("Content-Type", ctype)
    if host:
        req.add_header("Host", host)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_the_windows_browser_gets_in_once_by_a_token_in_no_url(tmp_path, monkeypatch, capsys):
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    seen = {}

    def while_running(_seconds):
        good = urllib.parse.urlencode({"t": ONCE}).encode()
        # absent, empty, doubled, wrong: refused, and none of them spends the token or removes the page
        seen["absent"] = handoff(A, b"")[0]
        seen["no_field"] = handoff(A, b"x=1")[0]
        seen["doubled"] = handoff(A, good + b"&" + good)[0]
        seen["wrong"] = handoff(A, urllib.parse.urlencode({"t": ONCE[:-1] + "x"}).encode())[0]
        seen["foreign_host"] = handoff(A, good, host=f"evil.example:{A.APP.port}")[0]
        seen["page_kept"] = page.exists()
        # the one-time token is not the session token: it opens no API route
        seen["once_on_api"] = call(A, "/api/overview", token=ONCE)[0]
        seen["no_token_on_api"] = call(A, "/api/overview", token=False)[0]
        # The browser posts the access page from a file: its Origin is "null" and the request is cross-site.
        seen["first"] = handoff(A, good, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
        seen["page_left"] = page.exists()
        seen["replayed"] = handoff(A, good)[0]
        seen["session_on_api"] = call(A, "/api/overview")[0]
        seen["session"] = A.APP.token
        seen["tab"], seen["instance"] = A.APP.tab_key, A.APP.instance
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    assert (seen["absent"], seen["no_field"], seen["doubled"], seen["wrong"]) == (401, 401, 401, 401)
    assert seen["foreign_host"] == 403 and seen["page_kept"] is True
    assert seen["once_on_api"] == 401 and seen["no_token_on_api"] == 401
    code, body, headers = seen["first"]
    assert code == 200 and seen["page_left"] is False
    # R73: the session token is in the cookie only (HttpOnly, SameSite=Strict, Path=/, no Domain): never in the
    # body, a URL or anything a script of the page can read
    assert headers.get("Set-Cookie") == f"{A.SESSION_COOKIE}{port}={seen['session']}; HttpOnly; SameSite=Strict; Path=/"
    assert seen["session"] not in body
    # The session's other half and the instance value go to the tab's sessionStorage, by the answer's one script,
    # which its policy runs by hash and before the refresh moves the tab.
    script = re.findall("<script>(.*?)</script>", body, re.S)
    assert len(script) == 1 and "sessionStorage.setItem" in script[0], body
    assert seen["tab"] in script[0] and seen["instance"] in script[0]
    assert body.index("</script>") < body.index('content="0;url=/"')
    import base64
    import hashlib
    digest = base64.b64encode(hashlib.sha256(script[0].encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}';" in headers.get("Content-Security-Policy", "")
    assert ONCE not in body and "#t=" not in body and 'content="0;url=/"' in body
    assert headers.get("Cache-Control") == "no-store" and headers.get("Referrer-Policy") == "no-referrer"
    assert seen["replayed"] == 401 and seen["session_on_api"] == 200
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{port}/" in out and ONCE not in out and seen["session"] not in out
    # nothing is opened on the Linux side and no Linux access file is written
    assert handed == [] and not (run / "dendra").exists()


def test_two_racing_handoffs_get_in_once(tmp_path, monkeypatch):
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    codes = []

    def while_running(_seconds):
        good = urllib.parse.urlencode({"t": ONCE}).encode()
        # Four at once: the server's listen queue holds five (socketserver's request_queue_size), so none is
        # turned away by the queue instead of being answered.
        go = threading.Barrier(4)

        def one():
            go.wait()
            try:
                codes.append(handoff(A, good)[0])
            except OSError as e:
                codes.append(repr(e))
        ts = [threading.Thread(target=one) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    assert sorted(codes, key=str) == [200, 401, 401, 401]


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("case", ["no_port", "low_port", "relative_page", "missing_page", "page_is_a_link",
                                  "page_is_a_folder", "terminal", "empty_stdin", "short_token", "token_with_a_space",
                                  "access_file_alone"])
def test_windows_mode_refuses_before_anything_listens(tmp_path, monkeypatch, capsys, case):
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    argv = ["--windows", "--port", str(port), "--access-file", str(page)]
    if case == "no_port":
        argv = ["--windows", "--access-file", str(page)]
    elif case == "low_port":
        argv[2] = "80"
    elif case == "relative_page":
        argv[4] = "open-1.html"
    elif case == "missing_page":
        page.unlink()
    elif case == "page_is_a_link":
        target = tmp_path / "elsewhere.html"
        page.rename(target)
        page.symlink_to(target)
    elif case == "page_is_a_folder":
        page.unlink()
        page.mkdir()
    elif case == "terminal":
        monkeypatch.setattr(A.sys, "stdin", _Tty(ONCE + chr(10)))
    elif case == "empty_stdin":
        monkeypatch.setattr(A.sys, "stdin", io.StringIO(""))
    elif case == "short_token":
        monkeypatch.setattr(A.sys, "stdin", io.StringIO(ONCE[:42] + chr(10)))
    elif case == "token_with_a_space":
        monkeypatch.setattr(A.sys, "stdin", io.StringIO(ONCE[:20] + " " + ONCE[20:] + chr(10)))
    elif case == "access_file_alone":
        argv = ["--no-window", "--access-file", str(page)]

    def no_server(*a, **k):
        raise AssertionError("a server was started")

    monkeypatch.setattr(A, "ThreadingHTTPServer", no_server)
    assert A.main(argv) == 2
    out = capsys.readouterr().out
    assert "Dendra refused" in out and "Nothing was started" in out and ONCE[:20] not in out
    assert handed == []


def test_windows_mode_listens_on_the_loopback_only(tmp_path, monkeypatch):
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    bound, seen = [], {}
    real = A.ThreadingHTTPServer

    class Recorded(real):
        def __init__(self, address, handler):
            bound.append(address)
            super().__init__(address, handler)

    monkeypatch.setattr(A, "ThreadingHTTPServer", Recorded)
    # A non-loopback address of this machine, when it has one (a UDP connect sends nothing).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            other = s.getsockname()[0]
        except OSError:
            other = ""

    def while_running(_seconds):
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            seen["loopback"] = True
        if other and not other.startswith("127."):
            try:
                socket.create_connection((other, port), timeout=3).close()
                seen["other"] = "connected"
            except OSError:
                seen["other"] = "refused"
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    assert bound == [("127.0.0.1", port)] and seen["loopback"] is True
    print(f"non-loopback address tried: {other or 'none on this machine'}")
    assert seen.get("other", "refused") == "refused"


def test_a_port_already_held_is_refused_in_words(tmp_path, monkeypatch, capsys):
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    with socket.socket() as held:
        held.bind(("127.0.0.1", port))
        held.listen(1)
        assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 2
    out = capsys.readouterr().out
    assert f"cannot listen on 127.0.0.1 port {port}" in out and "Nothing was started" in out


def test_the_access_page_is_removed_when_dendra_stops_unused(tmp_path, monkeypatch):
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    monkeypatch.setattr(A.time, "sleep", stop_at_once)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    assert not page.exists()


def test_open_windows_ps1_speaks_the_application_s_own_options():
    # A contract read from the shipped script (it runs on Windows, not here): the options it hands to
    # dendra_app.py are options of its parser, the token goes on the standard input and never on the command
    # line, the page posts the field and route /handoff reads, and the port bounds are the application's.
    import dendra_app as A
    ps1 = open(os.path.join(APP, "open_windows.ps1"), encoding="utf-8").read()
    argv_line = next(line for line in ps1.splitlines() if line.startswith("$argv = @("))
    # the options after python3 and the application's path are the application's; those before are wsl.exe's
    opts = re.findall(r'"(--[a-z-]+)"', argv_line.split('"python3", $AppPath', 1)[1])
    known = A.arg_parser()._option_string_actions
    assert {"--windows", "--port", "--access-file"} <= set(opts) and all(o in known for o in opts)
    assert "$Token" not in argv_line and "$psi.Arguments" in ps1
    assert "$app.StandardInput.Write($Token" in ps1 and "$psi.RedirectStandardInput = $true" in ps1
    assert "New-Object byte[] 32" in ps1 and 'name="t"' in ps1 and "/handoff" in ps1
    assert f"$PortMin = {A.WINDOWS_PORT_MIN}; $PortMax = {A.WINDOWS_PORT_MAX}" in ps1
    assert "<form" in ps1 and 'method="post"' in ps1 and "?t=" not in ps1 and "#t=" not in ps1
    assert f'$ProofLabel = "{A.HANDOFF_PROOF_LABEL.decode()}"' in ps1 and "/handoff/proof" in ps1
    # the input is held open while the script runs: its end is what stops the application
    assert "$app.StandardInput.Close()" not in ps1 and "$app.StandardInput.Flush()" in ps1


_PROOF_NONCE = "00112233445566778899aabbccddeeff"


def post_form(A, route, body, host=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{A.APP.port}{route}", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in dict(headers or {}, **({"Host": host} if host else {})).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def expected_proof(token, nonce):
    import hashlib
    import hmac
    return hmac.new(token.encode(), b"dendra-handoff-proof:" + nonce.encode(), hashlib.sha256).hexdigest()


def test_the_handoff_proof_answers_for_the_token_without_spending_it(tmp_path, monkeypatch):
    # open_windows.ps1 posts a nonce to /handoff/proof before it opens the browser, and opens it only on
    # HMAC-SHA256(token, label + nonce): a program that took the port first cannot answer it.
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    seen = {}

    def while_running(_seconds):
        seen["right"] = post_form(A, "/handoff/proof", b"n=" + _PROOF_NONCE.encode())
        seen["other_nonce"] = post_form(A, "/handoff/proof", b"n=" + ("f" * 32).encode())
        seen["bad"] = [post_form(A, "/handoff/proof", b)[0] for b in
                       (b"", b"n=XYZ", b"n=" + b"0" * 31, b"n=" + b"0" * 33, b"n=" + b"A" * 32,
                        b"n=" + _PROOF_NONCE.encode() + b"&n=" + _PROOF_NONCE.encode(), b"m=" + _PROOF_NONCE.encode())]
        seen["foreign_host"] = post_form(A, "/handoff/proof", b"n=" + _PROOF_NONCE.encode(), host=f"evil.example:{A.APP.port}")[0]
        seen["page_kept"] = page.exists()
        seen["handoff"] = post_form(A, "/handoff", urllib.parse.urlencode({"t": ONCE}).encode())[0]
        seen["after_spent"] = post_form(A, "/handoff/proof", b"n=" + _PROOF_NONCE.encode())
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    code, body = seen["right"]
    assert code == 200 and json.loads(body) == {"proof": expected_proof(ONCE, _PROOF_NONCE)}
    assert ONCE not in body and expected_proof("another token", _PROOF_NONCE) not in body
    assert json.loads(seen["other_nonce"][1])["proof"] == expected_proof(ONCE, "f" * 32)
    assert seen["bad"] == [400] * 7 and seen["foreign_host"] == 403
    # asking for the proof spends nothing: the page is there and the token still opens the browser's way in once
    assert seen["page_kept"] is True and seen["handoff"] == 200
    assert seen["after_spent"][0] == 410 and "proof" not in json.loads(seen["after_spent"][1])


def test_the_routes_the_windows_script_calls_are_served(tmp_path, monkeypatch):
    # Every address open_windows.ps1 posts to is a route of this application: one the script calls and the
    # application lacks would answer 404, and the script would stop Dendra as an impostor.
    ps1 = open(os.path.join(APP, "open_windows.ps1"), encoding="utf-8").read()
    routes = sorted(set(re.findall(r"http://127[.]0[.]0[.]1:(?:[$][{]?Port[}]?|' [+] [$]Port [+] ')(/[a-z/]+)", ps1)))
    assert "/handoff/proof" in routes and "/handoff" in routes, routes
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch)
    seen = {}

    def while_running(_seconds):
        for r in routes:
            seen[r] = post_form(A, r, b"x=1")[0]
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    assert all(code != 404 for code in seen.values()) and set(seen) == set(routes), seen


def test_a_byte_order_mark_before_the_token_is_not_part_of_it(tmp_path, monkeypatch):
    # Observed with wsl.exe started from a PowerShell whose console is set to UTF-8: it hands the Linux side a
    # first line that starts with U+FEFF. The token is read without it, and nothing else is accepted.
    A, run, handed, page, port = windows_launched(tmp_path, monkeypatch, stdin_text=chr(0xFEFF) + ONCE + chr(10))
    seen = {}

    def while_running(_seconds):
        seen["handoff"] = post_form(A, "/handoff", urllib.parse.urlencode({"t": ONCE}).encode())[0]
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--windows", "--port", str(port), "--access-file", str(page)]) == 0
    assert seen["handoff"] == 200


def _app_process(tmp_path, argv, stdin):
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(tmp_path / "home"), XDG_RUNTIME_DIR=str(tmp_path / "run"),
               XDG_CONFIG_HOME=str(tmp_path / "cfg"), DENDRA_MINER_KIT=str(tmp_path / "kit"), PATH=os.environ.get("PATH", ""))
    return subprocess.Popen([sys.executable, os.path.join(APP, "dendra_app.py")] + argv, stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)


def _answers(port, windows):
    for _ in range(100):
        try:
            if windows:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/handoff/proof", data=b"n=" + _PROOF_NONCE.encode(), method="POST")
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.status == 200
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except (OSError, urllib.error.URLError):
            _SLEEP(0.1)
    return False


def _ended(p, seconds=10):
    try:
        return p.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return "still running"


@pytest.mark.parametrize("how", ["input_end", "hangup", "terminate"])
def test_windows_mode_stops_when_its_launcher_ends_and_removes_the_page(tmp_path, how):
    # Observed from Windows: killing wsl.exe gives the process here SIGHUP and the end of its input;
    # closing the input, or killing the PowerShell that holds it, gives the end of its input alone. Each one
    # stops the application here, which removes the access page on its way out (exit code 0).
    import signal as sg
    page = tmp_path / "open-1.html"
    page.write_text("page")
    port = free_port()
    p = _app_process(tmp_path, ["--windows", "--port", str(port), "--access-file", str(page)], subprocess.PIPE)
    try:
        p.stdin.write((ONCE + chr(10)).encode())
        p.stdin.flush()
        assert _answers(port, True), p.stdout.read1(4000) if p.poll() is not None else "no answer"
        if how == "input_end":
            p.stdin.close()
        else:
            p.send_signal(sg.SIGHUP if how == "hangup" else sg.SIGTERM)
        rc = _ended(p)
    finally:
        if p.poll() is None:
            p.kill()
        if not p.stdin.closed:
            p.stdin.close()
    out = p.stdout.read().decode()
    assert rc == 0, (how, rc, out)
    assert not page.exists() and ONCE not in out


def test_the_linux_flow_never_stops_on_its_input_and_keeps_the_default_hangup(tmp_path):
    # Started from Linux (a terminal, systemd, nohup, a job in the background), the standard input may be closed from
    # the start: it never stops the application, and the hang-up keeps its default action.
    import signal as sg
    port = free_port()
    p = _app_process(tmp_path, ["--no-window", "--port", str(port)], subprocess.DEVNULL)
    try:
        assert _answers(port, False)
        _SLEEP(1.5)
        alive = p.poll() is None
        p.send_signal(sg.SIGHUP)
        rc = _ended(p)
    finally:
        if p.poll() is None:
            p.kill()
    assert alive is True and rc == -sg.SIGHUP, (alive, rc, p.stdout.read().decode())


# --- the Judge line: the judge's memory, read from the daemon's heartbeat and the juror's ledger -------------------
# The reader the application ships (_JUDGE_READER) is run for real (in_container) against files the case writes, so a
# missing file, a missing key, a null and a zero are different inputs. The block is written field by field, as the
# daemon's memory watch writes it (key judge_memory): this bench does not import the module that writes it.
def _jm_block(**over):
    now = int(time.time())
    b = {"at": now - 4, "ceiling": "resident", "mode": "on-demand", "mode_since": now - 900, "mode_why": "bench",
         "gate": "closed", "gate_why": "available memory 9000 MB is under the 19600 MB the model needs", "resident": False,
         "footprint_mb": 19600, "footprint_source": "engine-ps", "load_s": 41.5, "mem_total_mb": 32000,
         "mem_available_mb": 9000, "swap_total_mb": 8192, "swap_used_mb": 2100, "swap_out_pages_period": 0,
         "psi_some_avg10": 3.5, "psi_full_avg10": 0.0, "psi_some_avg300": 1.0, "unloads": 2, "oom_seen": 1, "pause_until": None}
    b.update(over)
    return b


def _ledger_doc(abstentions=3, **over):
    c = {"since": 1760000000, "abstentions": {"memory-guard": abstentions} if isinstance(abstentions, int) else abstentions,
         "last_load": {"at": 1760000500, "load_s": 41.5, "available_before_mb": 30000, "available_after_mb": 9500, "engine_size_mb": 19600}}
    c.update(over)
    return {"schema": 1, "model": "qwen-judge", "source": "chain", "counters": c}


def _judge_overview(A, kit, tmp_path, monkeypatch, heartbeat, ledger=None, judge="1", chain=None):
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + f"DENDRA_MINER_JUDGE={judge}" + NL)
    hb, js = tmp_path / "heartbeat.json", tmp_path / "judge-state.json"
    hb.unlink(missing_ok=True)
    js.unlink(missing_ok=True)
    if heartbeat is not None:
        hb.write_text(heartbeat if isinstance(heartbeat, str) else json.dumps(heartbeat))
    if ledger is not None:
        js.write_text(ledger if isinstance(ledger, str) else json.dumps(ledger))
    _, _, run = in_container(tmp_path, env={"DENDRA_STATUS_FILE": str(hb), "DENDRA_JUDGE_STATE_FILE": str(js)})
    monkeypatch.setattr(A, "miner_exec", run)
    monkeypatch.setattr(A, "containers", lambda: ([], None))
    monkeypatch.setattr(A, "miner_identity", lambda: {"miner_id": "dm1abc", "address": "dendra1aaa"})
    monkeypatch.setattr(A, "chain_view", lambda env, ident: chain or {})
    return call(A, "/api/overview")[1]["judge_memory"]


def test_the_overview_reads_the_judge_s_memory_from_the_heartbeat_and_the_ledger(app, tmp_path, monkeypatch):
    A, kit = app
    m = _judge_overview(A, kit, tmp_path, monkeypatch, {"written_at": int(time.time()), "judge_memory": _jm_block()}, _ledger_doc(3))
    assert m["state"] == "read" and m["why"] == ""
    assert (m["mode"], m["ceiling"], m["gate"], m["loaded"]) == ("on-demand", "resident", "closed", False), m
    assert m["gate_why"] == "available memory 9000 MB is under the 19600 MB the model needs", m
    assert (m["footprint_mb"], m["footprint_source"], m["mem_available_mb"], m["swap_used_mb"]) == (19600, "engine-ps", 9000, 2100), m
    assert (m["unloads"], m["oom_seen"], m["paused"]) == (2, 1, False), m
    assert (m["memory_abstentions"], m["counters_since"], m["last_load_s"]) == (3, 1760000000, 41.5), m
    assert isinstance(m["age_s"], int) and 0 <= m["age_s"] < 600, m
    # a pause in progress is read from pause_until, in the container's own clock
    p = _judge_overview(A, kit, tmp_path, monkeypatch, {"judge_memory": _jm_block(pause_until=int(time.time()) + 300)}, _ledger_doc(0))
    assert p["paused"] is True and p["memory_abstentions"] == 0, p


def test_the_judge_s_memory_counts_zero_abstentions_only_from_a_ledger_it_read(app, tmp_path, monkeypatch):
    A, kit = app
    hb = {"judge_memory": _jm_block()}
    # a ledger that is an object and names no memory-guard stage counted none: the ledger writes only what happened
    assert _judge_overview(A, kit, tmp_path, monkeypatch, hb, _ledger_doc({"judge-error": 4}))["memory_abstentions"] == 0
    assert _judge_overview(A, kit, tmp_path, monkeypatch, hb, _ledger_doc({}))["memory_abstentions"] == 0
    # a ledger that was not read, or whose abstentions are no object, or whose count is no number, is not a zero
    for ledger in (None, "not json", _ledger_doc(None), _ledger_doc({"memory-guard": "many"}), _ledger_doc({"memory-guard": -1}),
                   {"schema": 1, "model": "m"}):
        m = _judge_overview(A, kit, tmp_path, monkeypatch, hb, ledger)
        assert m["state"] == "read" and m["memory_abstentions"] is None, (ledger, m)


def test_the_judge_s_memory_has_three_answers_and_never_a_healthy_default(app, tmp_path, monkeypatch):
    A, kit = app
    # the judge is requested and the heartbeat carries no block: unread, never read as fine
    m = _judge_overview(A, kit, tmp_path, monkeypatch, {"written_at": 1})
    assert m["state"] == "unread" and "carries no judge_memory" in m["why"], m
    # the judge is not requested: absent, said as such
    m = _judge_overview(A, kit, tmp_path, monkeypatch, {"written_at": 1}, judge="0")
    assert m["state"] == "absent" and "not requested" in m["why"] and "mode" not in m, m
    # the container gives no heartbeat, or one that is no JSON, or not an object
    for hb in (None, "not json", "[1, 2]"):
        m = _judge_overview(A, kit, tmp_path, monkeypatch, hb)
        assert m["state"] == "unread" and "could not be read" in m["why"], (hb, m)
    # a block that is no object
    for block in ("healthy", None, [1]):
        m = _judge_overview(A, kit, tmp_path, monkeypatch, {"judge_memory": block})
        assert m["state"] == "unread", (block, m)


def test_the_judge_s_memory_fields_that_are_null_or_odd_are_none_never_zero(app, tmp_path, monkeypatch):
    A, kit = app
    block = _jm_block(mode="whatever", gate=None, resident=None, footprint_mb=None, unloads=None, oom_seen="2", at=None,
                      mem_available_mb=-5, gate_why=12, ceiling="", footprint_source=7)
    m = _judge_overview(A, kit, tmp_path, monkeypatch, {"judge_memory": block}, _ledger_doc(1))
    assert m["state"] == "read", m
    for k in ("mode", "gate", "loaded", "footprint_mb", "unloads", "oom_seen", "age_s", "mem_available_mb", "ceiling", "footprint_source"):
        assert m[k] is None, (k, m)
    assert m["gate_why"] == "" and m["paused"] is False, m


def test_the_page_writes_the_judge_line_from_the_three_answers(app, tmp_path, monkeypatch):
    A, kit = app
    no_docker(A, monkeypatch)
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MINER_JUDGE=1" + NL)
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=None)

    def line(m):
        return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, judge_memory=m)}]})["text"]["judgeMemory"]
    read = {"state": "read", "why": "", "mode": "on-demand", "ceiling": "resident", "gate": "closed",
            "gate_why": "available memory 9000 MB is under the 19600 MB the model needs", "loaded": False, "age_s": 30,
            "unloads": 2, "oom_seen": 1, "paused": True, "memory_abstentions": 3, "counters_since": 1760000000}
    t = line(read)
    for part in ("on demand (the model is loaded for audits only)", "load held back: available memory 9000 MB is under the 19600 MB the model needs",
                 "model not loaded", "3 audit(s) left out for lack of memory since 2025-10-09", "2 emergency unload(s), 1 out-of-memory error(s)",
                 "paused before the next load", "read 30 s ago"):
        assert part in t, (part, t)
    t = line(dict(read, mode="resident", gate="open", loaded=True, unloads=0, oom_seen=0, paused=False, memory_abstentions=0))
    assert "resident (the model stays loaded)" in t and "room to load the model" in t and "model loaded" in t, t
    assert "0 audit(s) left out for lack of memory" in t and "emergency" not in t and "paused" not in t, t
    # what was not read is reported as such, never turned into a zero or into a healthy line
    t = line(dict(read, mode=None, gate=None, loaded=None, memory_abstentions=None, age_s=None, unloads=None, oom_seen=None, paused=False))
    for part in ("mode not read", "load check not read", "model state not read", "audits left out for lack of memory: not read", "read at an unknown time"):
        assert part in t, (part, t)
    assert "0 audit" not in t and "emergency" not in t, t
    assert line({"state": "absent", "why": "the judge is not requested in this kit, so no memory watch runs"}) == \
        "the judge is not requested in this kit, so no memory watch runs"
    t = line({"state": "unread", "why": "judge memory not read: the miner's heartbeat could not be read"})
    assert t.startswith("not read (judge memory not read") and "resident" not in t, t
    # an older server sends no judge_memory at all
    old = {k: v for k, v in ov.items() if k != "judge_memory"}
    assert run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": old}]})["text"]["judgeMemory"] == "not read"


# --- the Judge seat line: the chain's declaration of this judge, the audits it was seated on without an answer ---------
# The block is written as the daemon's declaration loop writes it (key judge_declaration); the chain's limit comes from
# chain_view, which reads the jobs params (judge_missed_draws_max, whose zero is the compiled 3).
def _decl_block(missed=0, state="declared", age=30, **over):
    b = {"at": int(time.time()) - age, "state": state, "role": "on", "draining": False, "sent": None, "why": "",
         "read": {"declared": state == "declared", "declared_height": 100, "last_verdict_height": 900, "missed_draws": missed}}
    b.update(over)
    return b


def _seat_overview(A, kit, tmp_path, monkeypatch, decl, limit=3, judge="1", **extra):
    hb = {"judge_memory": _jm_block(), **extra}
    if decl is not None:
        hb["judge_declaration"] = decl
    return _judge_overview(A, kit, tmp_path, monkeypatch, hb, _ledger_doc(0), judge=judge,
                           chain={"judge_missed_draws_max": limit})["seat"]


def test_the_seat_is_read_from_the_declaration_the_daemon_wrote_and_the_chain_s_limit(app, tmp_path, monkeypatch):
    A, kit = app
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(missed=2))
    assert (s["state"], s["declared_height"], s["last_verdict_height"], s["missed_draws"], s["missed_max"], s["excluded"]) == \
        ("declared", 100, 900, 2, 3, False), s
    assert isinstance(s["age_s"], int) and 0 <= s["age_s"] < 600, s
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(missed=3))
    assert (s["missed_draws"], s["missed_max"], s["excluded"]) == (3, 3, True), s
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(missed=1), limit=1)
    assert s["excluded"] is True, s


def test_the_seat_is_not_judged_without_the_chain_s_limit_nor_without_a_count(app, tmp_path, monkeypatch):
    A, kit = app
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(missed=9), limit=None)
    assert (s["missed_draws"], s["missed_max"], s["excluded"]) == (9, None, None), s
    for bad in (0, -1, "3", True):
        s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(missed=9), limit=bad)
        assert s["missed_max"] is None and s["excluded"] is None, (bad, s)
    for read in ({"declared": True}, {"missed_draws": "2"}, {"missed_draws": -1}, {"missed_draws": None}, "x", None):
        s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(read=read))
        assert s["state"] == "declared" and s["missed_draws"] is None and s["excluded"] is None, (read, s)


def test_the_seat_has_its_own_answers_and_never_a_clean_default(app, tmp_path, monkeypatch):
    A, kit = app
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(state="not_declared"))
    assert s["state"] == "not_declared" and "missed_draws" not in s, s
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(state="no_read", why="this dendrad has no `query jobs miner-judge`"))
    assert s["state"] == "unread" and "miner-judge" in s["why"], s
    s = _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(state="unknown", why=""))
    assert s["state"] == "unread" and "unknown" in s["why"], s
    s = _seat_overview(A, kit, tmp_path, monkeypatch, None)
    assert s["state"] == "unread" and "carries no judge_declaration" in s["why"], s
    s = _seat_overview(A, kit, tmp_path, monkeypatch, "declared")
    assert s["state"] == "unread", s
    # the judge is not requested: there is no seat to show; an unreadable heartbeat shows none either
    assert _seat_overview(A, kit, tmp_path, monkeypatch, _decl_block(missed=5), judge="0") is None
    m = _judge_overview(A, kit, tmp_path, monkeypatch, "not json", chain={"judge_missed_draws_max": 3})
    assert m["seat"] is None, m


def test_chain_view_reads_the_missed_draws_limit_and_a_zero_is_the_compiled_one(app, monkeypatch):
    A, _ = app
    env, ident = {"DENDRA_NODE": "tcp://api.example:26657"}, {"miner_id": "dm1abc", "address": ""}

    def view(params):
        monkeypatch.setattr(A, "_get_json", lambda url: params if url.endswith("/params") else None)
        return A.chain_view(env, ident)["judge_missed_draws_max"]
    assert view({"params": {"judge_missed_draws_max": "5"}}) == 5
    assert view({"params": {}}) == A.JUDGE_MISSED_DRAWS_MAX_COMPILED == 3          # proto3 omits the zero: the compiled limit
    assert view({"params": {"judge_missed_draws_max": "0"}}) == 3
    assert view(None) is None                                                     # not read is not 3
    assert view({"params": {"judge_missed_draws_max": "many"}}) is None
    assert view({"nope": 1}) is None


def test_the_page_writes_the_seat_line_from_the_chain_s_count_and_limit(app, tmp_path, monkeypatch):
    A, kit = app
    no_docker(A, monkeypatch)
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MINER_JUDGE=1" + NL)
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, containers=[], docker_error=None,
              chain={}, recovery_confirmed=None)

    def line(seat, state="read"):
        jm = {"state": state, "why": "", "seat": seat}
        return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, judge_memory=jm)}]})["text"]["judgeSeat"]
    seat = {"state": "declared", "why": "", "age_s": 40, "declared_height": 100, "last_verdict_height": 900,
            "missed_draws": 2, "missed_max": 3, "excluded": False}
    t = line(seat)
    assert "declared at height 100, last verdict at height 900, missed draws 2 of 3" in t and "excluded" not in t and "read 40 s ago" in t, t
    t = line(dict(seat, missed_draws=3, excluded=True))
    for part in ("missed draws 3 of 3", "excluded from juries: seated on 3 concluded audit(s) without an answer",
                 "turn the Judge switch off, then on", "clears the count"):
        assert part in t, (part, t)
    # what was not read is reported as such, never turned into a zero or into a clean line
    t = line(dict(seat, missed_draws=None, excluded=None))
    assert "missed draws ? of 3" in t and "the missed draws were not read" in t and "excluded" not in t, t
    t = line(dict(seat, missed_max=None, excluded=None))
    assert "missed draws 2 of ?" in t and "the chain's limit was not read" in t and "excluded" not in t, t
    t = line(dict(seat, declared_height=None, last_verdict_height=None))
    assert "declared at height ?, last verdict at height ?" in t, t
    assert "no judge declaration" in line({"state": "not_declared", "why": "", "age_s": 5})
    assert line({"state": "unread", "why": "the daemon's last state is 'unknown'"}).startswith("not read (the daemon's last state")
    assert line(None) == "not read"
    assert line(None, state="absent") == "the judge is not requested in this kit"
    old = {k: v for k, v in ov.items() if k != "judge_memory"}
    assert run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": old}]})["text"]["judgeSeat"] == "not read"


def test_a_judge_call_the_memory_guard_refused_is_not_measured_and_says_who_refused(mt, tmp_path, monkeypatch):
    # A call that could load the judge model passes the check of the room for it, whoever makes it (modea/memwatch.py::
    # hold_call). The test's judge part is such a call: refused, it measured nothing, and the engine was never asked.
    from modea import memwatch as mwm
    if not hasattr(mwm, "hold_call"):
        pytest.skip("the memory guard of this tree holds a call only during a pause: declared, not passed")
    A, kit, eng = mt
    env = {"OLLAMA_ENDPOINT": f"http://127.0.0.1:{eng.server_address[1]}", "OLLAMA_MODEL": MODEL, "DENDRA_JUDGE_MODEL_ID": JUDGE,
           "DENDRA_MEMWATCH_PROC_ROOT": str(tmp_path / "memroot"), "DENDRA_JUDGE_MEM_LIMIT_MB": "1"}
    _, _, run = in_container(tmp_path, env=env)
    monkeypatch.setattr(A, "miner_exec", run)
    eng.seen.clear()
    v = call(A, "/api/machine-test", body={})[1]
    assert v["verdict"]["state"] == "not measured" and "held by the memory guard" in v["verdict"]["why"], v["verdict"]
    assert "seconds" not in v["verdict"]
    assert not [d for d in eng.seen if d["model"] == JUDGE], "the engine was asked a judge's question the guard had refused"
    assert v["throughput"]["state"] == "measured", "the miner's own model is loaded: its call goes"


# --- The five additions: dashboard, diagnosis, pay by day, one row per card, alerts, explained log -------------
# The facts are built with the same readers the shipped cases use (`facts_of`); the container reads and the
# notifier are replaced. What is real: the tables the server decides from (the dashboard cards, the
# diagnosis, the pay blockers), the plain words of the ranking's reasons, the routes and the page's drawing.
SENT = {"state": "read", "outcome": "sent", "why": "signed report of dm1abc accepted", "age_s": 600,
        "sent_at": 1, "next_at": 2, "identity": "dm1abc", "http_code": 200, "note": ""}
NOTSENT = dict(SENT, outcome="no-url", why="no registry address: neither DENDRA_CAPACITY_URL nor DENDRA_NODE is set")
JMEM_OK = {"state": "read", "why": "", "mode": "resident", "gate": "open", "gate_why": "", "loaded": True,
           "memory_abstentions": 0, "paused": False, "seat": None}
# The checks a green dashboard rests on: the node's, the relay's, the registration and the judge role.
DASH_OK_CHECKS = [("C1", "node RPC", "ok", "", ""), ("C2", "node caught up", "ok", "", ""),
                  ("C3", "registration and keys", "ok", "", ""), ("C5", "work queue", "ok", "", ""),
                  ("C6", "relay write", "ok", "", ""), ("C11", "judge role (declared)", "ok", "", "")]
DASH_WORK = {"day": 5, "listed": True, "verified_requests": 3, "verdicts": 1, "presence": 4,
             "gross_udndr": 5_000_000, "payable_udndr": 5_000_000, "paid_udndr": 5_000_000, "reason": ""}


def dash_facts(A, **over):
    rows = over.pop("rows", DASH_OK_CHECKS)
    work = over.pop("work", dict(DASH_WORK))
    f = facts_of(A, rows=rows, capacity=SENT, jmem=dict(JMEM_OK),
                 payout={"declared": "dendra1pool", "to_machine_key": False}, work=work)
    f.update(over)
    return f


def test_the_dashboard_shows_six_cards_and_each_says_one_sentence(app):
    A, _ = app
    d = A.dashboard_view(dash_facts(A))
    assert [c["id"] for c in d["cards"]] == ["judge", "capacity", "kit", "node", "relay", "work"]
    assert d["level"] == "green"
    for c in d["cards"]:
        assert c["level"] in ("green", "amber", "red") and c["says"] and c["says"].strip().endswith("."), c


def test_the_dashboard_cards_levels_come_from_the_facts(app):
    A, _ = app
    # one reading missing is amber, never green
    d = A.dashboard_view(dash_facts(A, capacity={"state": "never", "why": "no round yet"}))
    cap = next(c for c in d["cards"] if c["id"] == "capacity")
    assert cap["level"] == "amber" and "no round" in cap["says"] and d["level"] == "amber"
    # a sender that sent nothing, a mute judge and a refused kit are red
    d = A.dashboard_view(dash_facts(A, capacity=NOTSENT))
    assert next(c for c in d["cards"] if c["id"] == "capacity")["level"] == "red" and "did not go out" in \
        next(c for c in d["cards"] if c["id"] == "capacity")["says"]
    d = A.dashboard_view(dash_facts(A, role={"word": "mute", "why": "the worker is down", "age_s": 5, "requested": True}))
    assert next(c for c in d["cards"] if c["id"] == "judge")["level"] == "red"
    low = {"mine": "2", "min": "3", "deadline": DEADLINE, "read": True, "why": ""}
    d = A.dashboard_view(dash_facts(A, now=DEADLINE_TS + 1, floor=low))
    kit = next(c for c in d["cards"] if c["id"] == "kit")
    assert kit["level"] == "red" and "refuses this kit" in kit["says"]
    # a seat the chain stopped drawing is red on the judge card
    d = A.dashboard_view(dash_facts(A, jmem=dict(JMEM_OK, seat={"state": "declared", "why": "", "age_s": 4,
                                                               "missed_draws": 3, "missed_max": 3, "excluded": True})))
    assert next(c for c in d["cards"] if c["id"] == "judge")["level"] == "red"
    # an excluded seat that is also mute: the card says the chain has stopped drawing it
    d = A.dashboard_view(dash_facts(A, role={"word": "mute", "why": "x", "age_s": 5, "requested": True},
                                    jmem=dict(JMEM_OK, seat={"state": "declared", "why": "", "age_s": 4,
                                                              "missed_draws": 3, "missed_max": 3, "excluded": True})))
    says = next(c for c in d["cards"] if c["id"] == "judge")["says"]
    assert "has stopped drawing this judge" in says and "until its missed draws" not in says, says
    # the work of the latest ranked day: paid, paid nothing for a missing report, no fact counted
    work = {"day": 5, "listed": True, "verified_requests": 3, "verdicts": 1, "presence": 4,
            "gross_udndr": 5_000_000, "payable_udndr": 5_000_000, "paid_udndr": 5_000_000, "reason": ""}
    say = next(c for c in A.dashboard_view(dash_facts(A, work=work))["cards"] if c["id"] == "work")
    assert say["level"] == "green" and "Season day 6" in say["says"] and "5 DNDR" in say["says"]
    lost = dict(work, paid_udndr=0, payable_udndr=0,
                reason="no signed capacity report at or before the day's end")
    say = next(c for c in A.dashboard_view(dash_facts(A, work=lost))["cards"] if c["id"] == "work")
    assert say["level"] == "red" and "capacity report" in say["says"]
    idle = dict(work, listed=False, paid_udndr=0, payable_udndr=0, gross_udndr=0)
    say = next(c for c in A.dashboard_view(dash_facts(A, work=idle))["cards"] if c["id"] == "work")
    assert say["level"] == "amber" and "counts no fact" in say["says"]
    # nothing readable at all is amber, and says so
    say = next(c for c in A.dashboard_view(dash_facts(A, work=None))["cards"] if c["id"] == "work")
    assert say["level"] == "amber" and "not read" in say["says"]


def test_the_diagnosis_names_what_why_matters_and_the_exact_fix(app):
    A, _ = app
    rows = OK_CHECKS + [("C3", "registration and keys", "ko", "not registered", "run deploy/join.sh"),
                        ("C5", "work queue", "ko", "the relay refused the read", "read the relay's logs")]
    d = A.diagnosis_view(dash_facts(A, rows=rows))
    ids = [p["id"] for p in d["problems"]]
    assert "check_failed" not in ids, "the generic line is replaced by one entry per failed check"
    c3 = next(p for p in d["problems"] if p["id"] == "C3")
    assert c3["level"] == "red" and "not registered" in c3["what"] and c3["fix"] == "run deploy/join.sh"
    assert "registered identities only" in c3["matters"] and c3["source"].endswith("::run_checks")
    c5 = next(p for p in d["problems"] if p["id"] == "C5")
    assert "the relay refused the read" in c5["what"] and "work arrives" in c5["matters"]
    assert d["level"] == "red"
    # a judge off is found, with its fix; a mute judge is one entry, never three
    d = A.diagnosis_view(dash_facts(A, role={"word": "off", "why": "", "age_s": 5, "requested": False}))
    off = next(p for p in d["problems"] if p["id"] == "judge-off")
    assert "judge is off" in off["what"] and "hw_probe.sh --can-judge" in off["fix"]
    rows = OK_CHECKS + [("C11", "judge role (declared)", "ko", "mute seat: the worker is down", "restart miner")]
    d = A.diagnosis_view(dash_facts(A, role={"word": "mute", "why": "the worker is down", "age_s": 5, "requested": True},
                                    rows=rows))
    assert [p["id"] for p in d["problems"] if p["id"].startswith("judge")] == ["judge_mute"], \
        "the fired rule carries it; no judge-off, no judge-memory entry beside it"
    # a guard closed and abstentions are found when no check said them
    d = A.diagnosis_view(dash_facts(A, jmem=dict(JMEM_OK, gate="closed", gate_why="no room for the model")))
    mem = next(p for p in d["problems"] if p["id"] == "judge-memory")
    assert "holds the model back" in mem["what"] and "free RAM" in mem["fix"]


def test_the_diagnosis_reads_the_sender_s_round_the_seat_and_the_payout_record(app):
    A, _ = app
    d = A.diagnosis_view(dash_facts(A, capacity=NOTSENT))
    rnd = [p for p in d["problems"] if p["id"] == "capacity-round"]
    assert len(rnd) == 1 and rnd[0]["level"] == "red" and "DENDRA_CAPACITY_URL" in rnd[0]["fix"]
    # once the check itself is ko, the round is not said twice: the C8 line carries it
    rows = OK_CHECKS + [("C8", "signed capacity report", "ko", "no line for m-bench in the registry",
                         "bash deploy/testnet-miner/publish-capacity.sh")]
    d = A.diagnosis_view(dash_facts(A, capacity=NOTSENT, rows=rows))
    assert not [p for p in d["problems"] if p["id"] == "capacity-round"]
    assert any(p["id"] == "capacity_report" and "no line for m-bench" in p["what"] for p in d["problems"])
    # a sent round older than a day is amber: the registry stops counting it
    d = A.diagnosis_view(dash_facts(A, capacity=dict(SENT, age_s=2 * 86400)))
    old = [p for p in d["problems"] if p["id"] == "capacity-round"]
    assert len(old) == 1 and old[0]["level"] == "amber" and "after a day" in old[0]["what"]
    # a seat the chain stopped drawing, when no check said it
    d = A.diagnosis_view(dash_facts(A, jmem=dict(JMEM_OK, seat={"state": "declared", "why": "", "age_s": 4,
                                                               "missed_draws": 3, "missed_max": 3, "excluded": True})))
    seat = next(p for p in d["problems"] if p["id"] == "judge-seat")
    assert seat["level"] == "red" and "switch off, then on" in seat["fix"]
    # the reading it points to exists once the switch is off: the daemon's withdrawal in the Logs card, or judge.sh
    # status; the seat line is not read then, so the fix never waits for it
    assert "Logs card" in seat["fix"] and "judge.sh status" in seat["fix"] and "seat line" not in seat["fix"], seat
    # no payout address on record, and the season's address missing
    d = A.diagnosis_view(dash_facts(A, payout={"declared": "", "to_machine_key": True}))
    pay = next(p for p in d["problems"] if p["id"] == "payout-address")
    assert "no payout address on record" in pay["what"] and "join.sh --payout-address" in pay["fix"]
    d = A.diagnosis_view(dash_facts(A, season_enabled=False))
    assert any(p["id"] == "season-address" and "DENDRA_FINAL_SEASON_URL" in p["fix"] for p in d["problems"])
    # a record that was not read is no problem said as fact: the season-address line is the only one here
    d = A.diagnosis_view(dash_facts(A, payout={"declared": "", "to_machine_key": None}))
    assert [p["id"] for p in d["problems"]] == []
    # everything read and fine says what was read
    d = A.diagnosis_view(dash_facts(A))
    assert d["problems"] == [] and any("capacity sender" in c for c in d["checked"]) \
        and any("payout address" in c for c in d["checked"])


def test_the_sender_s_last_round_is_read_by_key(app, monkeypatch, tmp_path):
    A, _ = app
    doc = {"identity": "dm1abc", "at": int(time.time()) - 600, "outcome": "sent", "why": "signed report accepted",
           "sent_at": int(time.time()) - 600, "next_at": int(time.time()) + 3000,
           "pay_note": "the season pays nothing to an identity without a signed capacity report"}
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, timeout=60, slot=0: (0, json.dumps(doc)))
    v = A.capacity_round_view()
    assert v["state"] == "read" and v["outcome"] == "sent" and v["age_s"] == 600 and v["identity"] == "dm1abc"
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, timeout=60, slot=0: (3, ""))
    assert A.capacity_round_view()["state"] == "never"
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, timeout=60, slot=0: (1, "boom"))
    assert A.capacity_round_view()["state"] == "unread"
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, timeout=60, slot=0: (0, json.dumps({"at": 1})))
    assert A.capacity_round_view()["state"] == "unread"
    # the small reader the container runs: exit 3 with no file, the document with one
    p = tmp_path / "cap.json"
    env = dict(os.environ, DENDRA_CAPACITY_STATUS_FILE=str(p))
    r = subprocess.run([sys.executable, "-c", A._CAPACITY_READER], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 3
    p.write_text(json.dumps(doc))
    r = subprocess.run([sys.executable, "-c", A._CAPACITY_READER], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout)["outcome"] == "sent"


def test_a_day_paid_zero_says_why_in_plain_words(app):
    A, _ = app
    assert "pays nothing to an identity without one" in \
        A.reason_plain_text("no signed capacity report at or before the day's end")
    assert "no payout address on record" in A.reason_plain_text("no payout address known")
    assert "only on a day with at least one verified request" in \
        A.reason_plain_text("presence not paid: no verified request counted that day")
    assert "kit 4 was below the floor of 5" in A.reason_plain_text("kit 4 below the floor 5: not paid")
    assert "first full day after registration" in A.reason_plain_text(
        "probation: first full day after registration, 60 percent of the gross")
    assert "pro rata" in A.reason_plain_text("daily budget shared pro rata")
    both = A.reason_plain_text("no signed capacity report at or before the day's end; daily budget shared pro rata")
    assert both.count("; ") == 1 and "pro rata" in both
    assert A.reason_plain_text("") == ""
    assert "no row for this identity" in A.reason_plain_text("", listed=False)
    # a reason the table does not know is shown as the ranking wrote it
    assert A.reason_plain_text("some reason of a later set") == "some reason of a later set"


def test_the_pay_days_come_from_the_published_rankings(app, programme):
    A, _ = app
    import final_season_calc as SC
    base, route = programme
    d0, d1 = publish(route, [
        [SC.Identity("dm1abc", presence=10, verified_requests=2), SC.Identity("someone", verified_requests=9)],
        [SC.Identity("dm1abc", presence=40, verified_requests=3, verdicts=1)]])
    route("status", 200, status_doc(5))
    p = A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")
    assert [d["day"] for d in p["days"]] == [0, 1]
    assert p["days"][1]["paid_udndr"] == row_of(d1)["paid_udndr"]
    assert p["days"][0]["reason_plain"] == "" and p["days"][0]["reason"] == ""
    assert p["season_paid_udndr"] == sum(d["paid_udndr"] for d in p["days"])
    # an identity absent from a ranking is a day with no fact, said in words
    p = A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1nobody")
    assert p["days"][0]["listed"] is False and "no row for this identity" in p["days"][0]["reason_plain"]
    # a ranking that fails leaves the days out, never a partial list
    route("ranking/day-001.json", 502, b"bad gateway")
    route("status", 200, status_doc(5))
    assert A.programme_view({"DENDRA_FINAL_SEASON_URL": base}, "dm1abc")["days"] is None


def test_the_latest_ranking_is_read_once_for_the_machine(app, programme):
    A, _ = app
    import final_season_calc as SC
    base, route = programme
    publish(route, [
        [SC.Identity("dm1abc", presence=10, verified_requests=2), SC.Identity("dm1other", verified_requests=9)],
        [SC.Identity("dm1abc", verified_requests=3, verdicts=1), SC.Identity("dm1other", verified_requests=1)]])
    route("status", 200, status_doc(5))
    day, rows, why = A.latest_ranking({"DENDRA_FINAL_SEASON_URL": base})
    assert day == 1 and why == ""
    assert rows["dm1abc"]["verified_requests"] == 3 and rows["dm1abc"]["reason_plain"] == ""
    assert rows["dm1other"]["paid_udndr"] >= 0
    # a status without a day, and a ranking that fails, are whys, never rows guessed
    route("status", 200, dict(status_doc(5), day=None))
    day, rows, why = A.latest_ranking({"DENDRA_FINAL_SEASON_URL": base})
    assert day is None and rows == {} and why
    route("status", 200, status_doc(5))
    route("ranking/day-001.json", 502, b"bad gateway")
    day, rows, why = A.latest_ranking({"DENDRA_FINAL_SEASON_URL": base})
    assert day is None and "was not read" in why
    # no season address in the kit: said, and nothing is asked
    assert A.latest_ranking({})[2].startswith("no Final Testnet Season address")


def test_what_an_identity_serves_is_read_from_its_own_env(app):
    A, _ = app
    assert A.served_model({}) == {"where": "card", "model": None, "why": ""}
    assert A.served_model({"DENDRA_MODEL_ID": "mistral-nemo:12b"})["model"] == "mistral-nemo:12b"
    cpu = A.served_model({"DENDRA_SERVE_ENDPOINT": A.CPU_ENGINE, "DENDRA_SERVE_MODEL_ID": "judgem",
                          "DENDRA_JUDGE_MODEL_ID": "judgem"})
    assert cpu["where"] == "cpu" and cpu["model"] == "judgem"
    odd = A.served_model({"DENDRA_SERVE_ENDPOINT": "http://engine:11434", "DENDRA_SERVE_MODEL_ID": "m"})
    assert odd["where"] is None and odd["why"]


def test_one_row_per_card_carries_serves_memory_work_pay_judge(app, monkeypatch):
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_MODEL_ID=mistral-nemo:12b" + NL)
    (kit / "gpu" / "1").mkdir(parents=True)
    (kit / "gpu" / "1" / ".env").write_text("MINER_ID=dm1g1" + NL + f"DENDRA_SERVE_ENDPOINT={A.CPU_ENGINE}" + NL +
                                            "DENDRA_SERVE_MODEL_ID=judgem" + NL + "DENDRA_JUDGE_MODEL_ID=judgem" + NL)
    monkeypatch.setattr(A, "slots", lambda: ([{"slot": 0, "project": "dendra-miner", "state": "active",
                                               "uuid": "GPU-x", "miner_id": "dm1abc", "dir": str(kit)},
                                              {"slot": 1, "project": "dendra-miner-g1", "state": "active",
                                               "uuid": "GPU-y", "miner_id": "dm1g1", "dir": str(kit / "gpu" / "1")}], None))
    jm = dict(JMEM_OK, mode="on-demand", footprint_mb=19000, mem_available_mb=8000, mem_total_mb=32000)
    monkeypatch.setattr(A, "judge_memory_view", lambda env, slot=0: dict(jm))
    hv = {"state": "read", "rc": 0, "age_s": 60, "judge_role": {"word": "active", "why": ""}}
    monkeypatch.setattr(A, "health_view", lambda slot=0, sdir=None: dict(hv))
    monkeypatch.setattr(A, "latest_ranking", lambda env: (7, {"dm1abc": {"presence": 4, "verified_requests": 3,
                                                                        "verdicts": 1, "gross_udndr": 1_000_000,
                                                                        "payable_udndr": 1_000_000,
                                                                        "paid_udndr": 900_000, "rank": 1,
                                                                        "reason": "", "reason_plain": ""}}, ""))
    d = call(A, "/api/cards")[1]
    assert d["installed"] is True and d["day"] == 7 and [r["slot"] for r in d["rows"]] == [0, 1]
    r0 = d["rows"][0]
    assert r0["served"] == {"where": "card", "model": "mistral-nemo:12b", "why": ""}
    assert r0["memory"]["state"] == "read" and r0["memory"]["footprint_mb"] == 19000
    assert r0["work"]["paid_udndr"] == 900_000 and r0["judge"] == "active" and r0["health"]["rc"] == 0
    r1 = d["rows"][1]
    assert r1["served"]["where"] == "cpu" and r1["work"] is None
    # a machine whose identities cannot be listed says so, and is never an empty table
    monkeypatch.setattr(A, "slots", lambda: (None, "the identities of this machine could not be listed: x"))
    d = call(A, "/api/cards")[1]
    assert d["rows"] is None and "could not be listed" in d["error"]


def test_the_overview_carries_the_new_screens(app, monkeypatch):
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    no_docker(A, monkeypatch)
    monkeypatch.setattr(A, "capacity_round_view", lambda slot=0: dict(SENT))
    monkeypatch.setattr(A, "judge_memory_view", lambda env, slot=0: {"state": "absent",
                                                                    "why": "the judge is not requested", "seat": None})
    ov = call(A, "/api/overview")[1]
    assert ov["capacity"]["state"] == "read" and ov["capacity"]["outcome"] == "sent"
    assert [c["id"] for c in ov["dashboard"]["cards"]] == ["judge", "capacity", "kit", "node", "relay", "work"]
    # the readings that did not happen here (an empty container list, an unread kit, no report) are amber;
    # the stopped miner is the state's and the diagnosis's red, never a dashboard card of its own
    assert {c["id"] for c in ov["dashboard"]["cards"] if c["level"] == "amber"} >= {"kit", "judge", "node", "relay"}
    assert ov["dashboard"]["level"] == "amber"
    assert [p["id"] for p in ov["diagnosis"]["problems"]] == ["stopped", "no_report", "season-address"]
    assert [b["id"] for b in ov["alerts"]["blockers"]] == ["stopped"]
    assert ov["alerts"]["enabled"] is True
    assert "days" in ov["programme"]


def test_the_pay_blockers_and_the_notification_fire_once_until_the_blocker_clears(app, monkeypatch):
    A, _ = app
    shown = []

    def fake_notify(title, body):
        shown.append((title, body))
        return True, ""

    monkeypatch.setattr(A, "_notify", fake_notify)
    f = dash_facts(A, capacity=NOTSENT, miner={"running": False, "word": "exited"}, season_enabled=True,
                   payout={"declared": "", "to_machine_key": True})
    blockers = A.pay_blockers(f)
    assert {"capacity", "stopped", "payout"} <= {b["id"] for b in blockers}
    assert all("nothing" in b["says"] for b in blockers), blockers
    A.alerts_tick(0, blockers)
    A.alerts_tick(0, blockers)
    assert len(shown) == 1, "one notification per blocker until it clears"
    assert "capacity report" in shown[0][1] and shown[0][0].startswith("Dendra:")
    A.alerts_tick(0, [])
    A.alerts_tick(0, blockers)
    assert len(shown) == 2, "cleared, it rings again"
    # the switch off silences it, and the view says so
    monkeypatch.setattr(A, "settings", lambda: {"alerts": False})
    n = len(shown)
    A.alerts_tick(0, blockers)
    assert len(shown) == n and A.alerts_view(blockers, 0)["enabled"] is False
    # a fact that was not read is no blocker
    assert A.pay_blockers(dash_facts(A, miner={"running": None, "word": ""})) == []


def test_a_value_the_miner_container_wrote_reaches_the_notifier_as_text(app, monkeypatch):
    # The capacity round's outcome comes from a file inside the miner container and is pasted in the blocker's
    # sentence. The notifier reads the body as markup, and some desktops make a link in it clickable: a hostile
    # container could put its own link in a critical notification signed Dendra.
    A, _ = app
    sent = []
    real_which = A.shutil.which
    monkeypatch.setattr(A.shutil, "which", lambda n, *a, **k: "/usr/bin/notify-send" if n == A.NOTIFY_BIN else real_which(n, *a, **k))
    monkeypatch.setattr(A.subprocess, "run", lambda argv, **k: sent.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    lure = '<a href="http://x.example/">fix</a> & <b>now</b>'
    blockers = A.pay_blockers(dash_facts(A, capacity=dict(NOTSENT, outcome=lure)))
    assert "capacity" in [b["id"] for b in blockers] and any(lure in b["says"] for b in blockers), blockers
    A.alerts_tick(0, blockers)
    body = sent[0][-1]
    assert "<" not in body and ">" not in body, body
    assert "&lt;a href=" in body and "&amp;" in body and "&lt;b&gt;now&lt;/b&gt;" in body, body
    # the page draws the sentence as text (textContent): its view keeps the words as the container wrote them
    assert lure in A.alerts_view(blockers, 0)["last_notified_says"]


def test_the_alerts_route_and_the_toggle(app, monkeypatch):
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_FINAL_SEASON_URL=http://127.0.0.1:9/final-season/v1" + NL)
    no_docker(A, monkeypatch)
    monkeypatch.setattr(A, "keys_status", lambda: {"payout": None, "keyring": {"backend": "file"}})
    monkeypatch.setattr(A, "capacity_round_view", lambda slot=0: dict(SENT))
    monkeypatch.setattr(A, "kit_reading", lambda slot=0: ({"behind": False, "latest": "4", "why": ""},
        {"mine": "4", "min": "3", "deadline": DEADLINE, "read": True, "why": ""}))
    A._ALERT_STATE["notified"].clear()
    a = call(A, "/api/alerts")[1]
    assert a["enabled"] is True and a["notifier"] in (True, False)
    # the container list of this bench is empty, so the stopped rule fires beside the payout one
    assert [b["id"] for b in a["blockers"]] == ["stopped", "payout"], a["blockers"]
    # the toggle is kept in the application's settings
    assert call(A, "/api/alerts", body={"enabled": False})[1] == {"enabled": False}
    assert call(A, "/api/alerts")[1]["enabled"] is False
    assert call(A, "/api/alerts", body={})[0] == 400
    with open(os.path.join(A.CONFIG_DIR, "app.json"), encoding="utf-8") as fh:
        assert json.load(fh)["alerts"] is False
    # the reading sends no notification: the overview read does that
    monkeypatch.setattr(A, "_notify", lambda title, body: (True, ""))
    assert call(A, "/api/alerts")[1]["last_notified_at"] is None


def test_the_log_lines_are_explained_and_the_raw_line_travels(app, monkeypatch):
    A, _ = app
    lines = [
        "dendra-miner-miner-1  | [daemon] FATAL: Ollama unreachable (ConnectionError) and mock is forbidden",
        "dendra-miner-miner-1  | sent: signed report of dm1abc accepted by https://cap.example (kit 4, judge role active); next round in 60 min",
        "dendra-miner-miner-1  | not sent (no-url): no registry address. The season pays nothing to an identity without a signed capacity report; next round in 5 min",
        "dendra-miner-miner-1  | [judge] WARNING: the judge state was not written (OSError): verdict unaffected",
        "dendra-miner-miner-1  | [reveal] dm1abc revealed job123 to 2 anchored juror(s)",
        "dendra-miner-miner-1  | an entirely unknown line",
    ]
    monkeypatch.setattr(A, "compose", lambda *a, timeout=120, slot=0: (0, NL.join(lines)))
    d = call(A, "/api/logs/explained")[1]
    assert d["rc"] == 0 and [l["severity"] for l in d["lines"]] == ["red", "info", "red", "amber", "info", ""]
    assert d["lines"][0]["says"].startswith("The miner's main process stopped")
    assert d["lines"][1]["says"].startswith("The identity's signed capacity report was sent")
    assert d["lines"][2]["says"].startswith("The signed capacity report did not go out")
    assert d["lines"][4]["says"].startswith("A disputed job was revealed")
    assert d["lines"][5]["says"] == "" and d["lines"][5]["raw"].endswith("an entirely unknown line")
    assert d["lines"][5]["service"] == "dendra-miner-miner-1"
    assert d["text"].endswith("an entirely unknown line"), "the raw tail travels with the explanations"
    # a judge worker that died is drawn until a limit, and a restart does not end the limit
    fatal = A.explain_log_line("dendra-miner-miner-1  | [judge] FATAL: cannot reach the inference backend")
    assert (fatal["severity"] == "red" and "judge_missed_draws_max" in fatal["says"]
            and "judge_verdict_window_blocks" in fatal["says"] and "holds a mute seat" not in fatal["says"]), fatal
    # a compose refusal keeps its exit code and says why the log was not read, never as a log line
    monkeypatch.setattr(A, "compose", lambda *a, timeout=120, slot=0: (127, "docker is not installed" + NL))
    d = call(A, "/api/logs/explained")[1]
    assert d["rc"] == 127 and d["lines"] == [] and d["error"] == "Docker is not installed on this machine", d
    monkeypatch.setattr(A, "compose", lambda *a, timeout=120, slot=0: (1, "no configuration file provided: not found" + NL))
    d = call(A, "/api/logs/explained")[1]
    assert d["lines"] == [] and "no configuration file provided" in d["error"], d


def _card(d, cid):
    return next(c for c in d["cards"] if c["id"] == cid)


def test_the_judge_card_says_runs_only_for_a_judge_read_active(app):
    A, _ = app
    # "unknown": requested, and the role could not be read (miner_selftest.py::JUDGE_ROLES) -- never green
    f = dash_facts(A, role={"word": "unknown", "why": "the judge engine did not answer", "age_s": 60, "requested": True})
    d = A.dashboard_view(f)
    j = _card(d, "judge")
    assert j["level"] == "amber" and "was not read" in j["says"] and "runs," not in j["says"], j
    assert d["level"] != "green"
    # off: the same rule as the diagnosis's judge-off line, amber on both screens
    f = dash_facts(A, role={"word": "off", "why": "", "age_s": 60, "requested": False})
    assert _card(A.dashboard_view(f), "judge")["level"] == "amber"
    assert any(p["id"] == "judge-off" and p["level"] == "amber" for p in A.diagnosis_view(f)["problems"])
    # a stopped miner: its judge does not run now, whatever the last check read
    f = dash_facts(A, miner={"running": False, "word": "exited"})
    j = _card(A.dashboard_view(f), "judge")
    assert j["level"] == "amber" and "does not run now" in j["says"], j
    # active, fresh report, running miner: green
    assert _card(A.dashboard_view(dash_facts(A)), "judge")["level"] == "green"


def test_cards_read_from_a_report_older_than_its_schedule_are_amber_and_dated(app):
    A, _ = app
    f = dash_facts(A)
    f.update(facts_of(A, rows=DASH_OK_CHECKS, age=40 * 3600))
    f.update(capacity=SENT, jmem=dict(JMEM_OK), payout={"declared": "dendra1pool", "to_machine_key": False},
             work=dict(DASH_WORK))
    assert f["report"]["stale"] is True
    d = A.dashboard_view(f)
    for cid in ("node", "relay", "judge"):
        c = _card(d, cid)
        assert c["level"] == "amber" and "older than its schedule" in c["says"], c
    assert "40 h ago" in _card(d, "node")["says"]
    assert d["level"] != "green" and A.status_view(f)["level"] == "orange"
    # a fresh report: green, and the sentence still dates what it read
    d = A.dashboard_view(dash_facts(A))
    assert _card(d, "node")["level"] == "green" and "10 min ago" in _card(d, "node")["says"]


C8_OK = ("C8", "signed capacity report", "ok", "m-bench: fresh, and attributed to the operator the chain records", "")


def _cap(A, capacity, rows=DASH_OK_CHECKS):
    f = dash_facts(A, rows=rows, capacity=capacity)
    d = A.dashboard_view(f)
    return (_card(d, "capacity"), [b["id"] for b in A.pay_blockers(f)],
            [p for p in A.diagnosis_view(f)["problems"] if p["id"] == "capacity-round"])


def test_the_capacity_card_decides_on_what_the_registry_counts_before_the_last_round(app):
    A, _ = app
    unreachable = dict(SENT, outcome="unreachable", why="the registry did not answer", sent_at=None, sent_age_s=None)
    # C8 ok on a fresh report: a transient round is green, no blocker, no diagnosis line
    card, blockers, diag = _cap(A, unreachable, rows=DASH_OK_CHECKS + [C8_OK])
    assert card["level"] == "green" and "tries again by itself" in card["says"], card
    assert blockers == [] and diag == []
    # C8 ok, and a round only the operator can mend: amber, not a blocker yet
    card, blockers, diag = _cap(A, dict(NOTSENT, sent_age_s=None), rows=DASH_OK_CHECKS + [C8_OK])
    assert card["level"] == "amber" and blockers == [] and len(diag) == 1 and diag[0]["level"] == "amber"
    # no C8: a report sent an hour ago is still counted, so a transient round is amber and blocks nothing
    card, blockers, diag = _cap(A, dict(unreachable, sent_at=1, sent_age_s=3600))
    assert card["level"] == "amber" and "60 min ago" in card["says"] and blockers == [], card
    assert "answer. The report" in card["says"], card
    # a transient round with no report sent since the sender started: not known, amber, no blocker
    card, blockers, _ = _cap(A, unreachable)
    assert card["level"] == "amber" and "was not read" in card["says"] and blockers == [], card
    # a round that was not read is no blocker
    card, blockers, _ = _cap(A, {"state": "unread", "why": "the miner container did not answer: service not running"})
    assert card["level"] == "amber" and blockers == [] and "((" not in card["says"], card
    # the last report sent is known to be older than a day: red, and the pay is blocked
    card, blockers, diag = _cap(A, dict(unreachable, sent_at=1, sent_age_s=2 * 86400))
    assert card["level"] == "red" and "capacity" in blockers and diag and diag[0]["level"] == "red", card
    # a round only the operator can mend, and no report known: red, blocked
    card, blockers, _ = _cap(A, dict(NOTSENT, sent_age_s=None))
    assert card["level"] == "red" and "capacity" in blockers
    # the registry says it is not counted: red, whatever the round
    ko = ("C8", "signed capacity report", "ko", "no line for m-bench in the registry", "send it")
    card, blockers, diag = _cap(A, SENT, rows=DASH_OK_CHECKS + [ko])
    assert card["level"] == "red" and "capacity" in blockers and diag == []


def test_the_app_s_retried_rounds_are_the_self_test_s(app):
    A, _ = app
    import capacity_sender as CS
    import miner_selftest as MS
    assert set(A.CAPACITY_RETRIED) == set(MS.CAPACITY_RETRIED) | {CS.ABSENT}


def test_a_round_docker_could_not_read_says_why_once(app, monkeypatch):
    A, _ = app
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, timeout=60, slot=0: (127, "docker is not installed"))
    v = A.capacity_round_view()
    assert v == {"state": "unread", "why": "Docker is not installed on this machine"}
    monkeypatch.setattr(A, "miner_exec", lambda *cmd, timeout=60, slot=0: (1, 'service "miner" is not running' + NL))
    v = A.capacity_round_view()
    assert v["state"] == "unread" and "exit" not in v["why"] and 'service "miner" is not running' in v["why"]
    card = _card(A.dashboard_view(dash_facts(A, capacity=v)), "capacity")
    assert "((" not in card["says"] and "))" not in card["says"], card


def test_the_cards_table_says_not_read_when_the_ranking_was_not_read(app, tmp_path):
    A, _ = app
    row = {"slot": 0, "state": "active", "uuid": "GPU-x", "miner_id": "dm1abc",
           "served": {"where": "card", "model": "m", "why": ""}, "memory": {"state": "absent", "why": "off"},
           "judge": "active", "health": {"state": "read", "rc": 0, "age_s": 60}, "work": None, "day": None}
    unread = {"installed": True, "day": None, "rows": [row], "error": "",
              "ranking_why": "the ranking of day 6 was not read (HTTP 500)"}
    ov = _page_overview(A, programme={"enabled": False}, rules={})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}], "/api/cards": [{"status": 200, "data": unread}]},
                steps=[["click", "cardsBtn"]])
    assert "the ranking of day 6 was not read (HTTP 500)" in ui["text"]["cardsMsg"], ui["text"]["cardsMsg"]
    assert ui["rows"]["cardsBody"][0][7] == "not read", ui["rows"]["cardsBody"]
    # a ranked day that was read, where the identity has no row: "not listed" (a reading of zero)
    listed = dict(unread, day=6, ranking_why="", rows=[dict(row, day=6)])
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}], "/api/cards": [{"status": 200, "data": listed}]},
                steps=[["click", "cardsBtn"]])
    assert ui["text"]["cardsMsg"] == "" and ui["rows"]["cardsBody"][0][7] == "not listed"


def test_the_page_draws_the_dashboard_the_diagnosis_the_pay_and_the_alerts(app, tmp_path):
    A, _ = app
    ov = _page_overview(A,
        dashboard={"level": "amber", "cards": [
            {"id": "judge", "name": "Judge mode and abstentions", "level": "green",
             "says": "The judge runs, resident, the model is loaded, no audit left out."},
            {"id": "capacity", "name": "Capacity report", "level": "amber",
             "says": "The capacity sender has written no round yet."}]},
        diagnosis={"level": "red", "problems": [{"id": "capacity-round", "level": "red",
                    "what": "The identity's capacity sender did not send its last report (no-url): no registry address.",
                    "matters": "the season pays nothing to an identity without a fresh signed capacity report",
                    "fix": "set DENDRA_CAPACITY_URL in the kit's .env",
                    "source": "capacity_sender.py::one_round"}], "checked": []},
        alerts={"enabled": True, "notifier": False,
                "blockers": [{"id": "capacity", "says": "The season pays nothing to an identity without a signed capacity report."}],
                "last_checked_at": 1, "last_notified_at": None, "last_notified_says": "", "last_failure": ""},
        programme={"enabled": False}, rules={})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}],
                           "/api/alerts": [{"status": 200, "data": ov["alerts"]}]})
    t = ui["text"]
    assert t["dashLevel"] == "Needs you" and "warn" in ui["cls"]["dashLevel"]
    assert len(ui["kids"]["dashGrid"]) == 2
    assert t["diagWord"] == "1 problem(s) to act on" and len(ui["kids"]["diagList"]) == 1
    assert "DENDRA_FINAL_SEASON_URL" in t["payMsg"] and t["payTotal"] == "—"
    assert t["alertState"].startswith("on:") and len(ui["kids"]["alertList"]) == 1
    # a server without the new answers: the sections say they were not read, and the page holds
    old = dict(ov)
    for key in ("dashboard", "diagnosis", "alerts"):
        old.pop(key, None)
    old["programme"] = {"enabled": False}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": old}]})
    t = ui["text"]
    assert t["dashLevel"] == "not read" and t["diagWord"] == "not read" and t["alertState"] == "not read"
    assert t["cardsMsg"] == "The cards were not read."
    # the pay: two days, one paid nothing, chart and table drawn
    days = [
        {"day": 0, "listed": True, "presence": 2, "verified_requests": 1, "verdicts": 0,
         "gross_udndr": 1_000_000, "payable_udndr": 1_000_000, "paid_udndr": 1_000_000, "reason": "",
         "reason_plain": ""},
        {"day": 1, "listed": True, "presence": 0, "verified_requests": 0, "verdicts": 0,
         "gross_udndr": 0, "payable_udndr": 0, "paid_udndr": 0,
         "reason": "no signed capacity report at or before the day's end",
         "reason_plain": "no signed capacity report of this identity reached the registry before the day's end, "
                         "and the season pays nothing to an identity without one"}]
    ov2 = _page_overview(A, programme={"enabled": True, "status": {"day": 2}, "ranked_days": 2,
                                       "season_paid_udndr": 1_000_000, "days": days, "latest": None}, rules={})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov2}]})
    assert len(ui["kids"]["payChart"]) == 2 and len(ui["kids"]["payBody"]) == 2
    assert ui["text"]["payTotal"] == "1 DNDR" and "2 ranked day(s)" in ui["text"]["payPoints"]
    assert ui["text"]["payMsg"] == ""
    # the alerts switch sends the position asked for, never a toggle of an unknown state
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}],
                           "/api/alerts": [{"status": 200, "data": ov["alerts"]}]},
                steps=[["click", "alertBtn"]])
    posted = [b for b in ui["bodies"] if b["path"] == "/api/alerts"]
    assert posted and posted[0]["body"] == {"enabled": False}, "the payload was on, the click asks off"


def test_the_new_screens_explain_when_no_miner_is_installed(app, tmp_path):
    A, _ = app
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": {
        "installed": False, "hint": "No miner on this machine yet."}}]})
    for field in ("dashLevel", "cardsMsg", "diagWord", "payMsg", "alertMsg", "statusHeadline"):
        assert "no miner" in ui["text"][field].lower(), (field, ui["text"][field])
    for section in ("dashSection", "cardsSection", "diagSection", "paySection", "alertSection", "statusCard"):
        assert ui["hidden"][section] is False, section


def test_every_new_section_reports_a_stopped_miner(app, tmp_path):
    A, _ = app
    stopped = _rule(id="stopped", level="red", says="The miner is not running (no miner container).")
    status = {"level": "red", "word": "Act now", "headline": stopped["says"], "do": "Press Start.",
              "rules": [stopped], "checked": []}
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": _page_overview(A, status=status)}]})
    for field in ("dashStopped", "cardsStopped", "diagStopped", "payStopped", "alertsStopped", "detailsStopped"):
        assert ui["text"][field] == stopped["says"], (field, ui["text"][field])
