"""Bench of the Dendra desktop application's local server: who may call it, and the first-launch steps.

What is replaced: docker (the kit's containers and the miner's keyring) — the application only reads and
drives them, and that driving is a `docker compose` command line — and the Final Testnet Season service, by a
local HTTP server that answers what each case needs, with rankings made by the programme's own calculation
(`publish`). What is real: the HTTP server, the token and Host checks, the access file the browser opens,
the recovery-phrase confirmation, the address check, the formula rerun on a ranked day, the reading of the
programme's rankings, the command lines handed to the miner container (RUN on this machine by
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
# the miner container (`python3 -m modea.keyring ...`) are RUN from there by `in_container`.
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
    compose exec` is RUN, from the miner image's code directory, with /data/keys mapped to a local
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
    monkeypatch.setattr(A, "open_window", lambda url: False)
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
const calls = [], bodies = [];
async function fetch(path, opt) {
  calls.push({ path: path, method: (opt && opt.method) || "GET" });
  if (opt && opt.body !== undefined) bodies.push({ path: path, body: JSON.parse(opt.body) });
  const q = plan.replies[path] || [{ status: 404, data: {} }];
  const r = q.length > 1 ? q.shift() : q[0];
  return { status: r.status, json: async () => r.data };
}
const ctx = vm.createContext({
  document: {
    getElementById: (id) => els[id] || (els[id] = stub(id, false)),
    createElement: () => stub("", false),
    createTextNode: (t) => ({ textContent: String(t) }),
    querySelectorAll: (sel) => (sel === "#ask input" ? els.ask.children : []),
  },
  location: { hash: "#t=bench", pathname: "/" }, history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {} }, fetch: fetch, setInterval: () => 1, console: console,
});
const settle = async () => { for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r)); };
(async () => {
  vm.runInContext(src, ctx);
  await settle();
  for (const step of plan.steps) {
    if (step[0] === "click") await els[step[1]].onclick();
    if (step[0] === "type") els.ask.children.forEach((inp) => { inp.value = step[1][inp.dataset.pos] || ""; });
    if (step[0] === "select") { els[step[1]].value = step[2]; await els[step[1]].onchange(); }
    await settle();
  }
  const out = { text: {}, hidden: {}, calls: calls, bodies: bodies,
    words: els.words.children.map((d) => d.children.map((t) => t.textContent).join("")) };
  out.cls = {};
  for (const id of Object.keys(els)) { out.text[id] = els[id].textContent; out.hidden[id] = els[id].classList.contains("hidden");
    out.cls[id] = typeof els[id].className === "string" ? els[id].className : ""; }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify({ error: String((e && e.stack) || e) })); process.exit(1); });
"""


def run_ui(tmp_path, replies, steps=()):
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
    (tmp_path / "ui_plan.json").write_text(json.dumps({"ids": ids, "replies": replies, "steps": list(steps)}))
    # The page writes amounts in the reader's locale ("2,38" under fr_FR): the bench pins one, so that what a
    # case reads is the page's figure and not the machine's language.
    r = _RUN([node, str(tmp_path / "ui_harness.js"), page_path, str(tmp_path / "ui_plan.json")],
             capture_output=True, text=True, timeout=60,
             env=dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8"))
    out = json.loads(r.stdout or "{}")
    assert r.returncode == 0 and "error" not in out, r.stdout + r.stderr
    return out


def call(A, path, body=None, token=True, host=None):
    url = f"http://127.0.0.1:{A.APP.port}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    if token:
        req.add_header("X-Dendra-App", A.APP.token if token is True else token)
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
    A, run, handed = launched(tmp_path, monkeypatch)
    seen = {}

    def while_running(_seconds):
        files = sorted((run / "dendra").glob("*.html"))
        seen.update(files=files, token=A.APP.token, dir_mode=(run / "dendra").stat().st_mode & 0o777)
        seen.update(file_mode=files[0].stat().st_mode & 0o777, holds=A.APP.token in files[0].read_text())
        seen.update(auth=call(A, "/api/overview")[0], left=files[0].exists())
        raise KeyboardInterrupt

    monkeypatch.setattr(A.time, "sleep", while_running)
    assert A.main(["--no-window"] if headless else []) == 0
    out, tok = capsys.readouterr().out, seen["token"]
    assert "http://127.0.0.1:" in out and tok not in out
    assert all(tok not in u for u in handed)
    if not headless:
        assert len(handed) == 1 and handed[0].startswith("file://") and handed[0].endswith(".html")
    else:
        # a host without a browser: the message says to read the file and where to tunnel to
        assert str(seen["files"][0]) in out and f"SSH tunnel to port {A.APP.port}" in out
    assert len(seen["files"]) == 1 and seen["file_mode"] == 0o600 and seen["dir_mode"] == 0o700
    assert seen["holds"] is True
    # the first authenticated request removes the file: the token lives on in the page only
    assert seen["auth"] == 200 and seen["left"] is False


def test_a_browser_that_does_not_start_leaves_the_file_and_says_where_it_is(tmp_path, monkeypatch, capsys):
    # webbrowser.open answering False (no display, an SSH session) used to be ignored: the reader was told
    # "Opening Dendra in your browser", got no page and no path, and the file went after its delay.
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
    # The real python3 runs the script's check against a `gi` that has the GIR namespaces each case lists.
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
    # confirming REMOVED the phrase from the machine, for its address only: nothing is left to serve
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
    # confirmed means REMOVED: nothing left to confirm, and no words read to find that out
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
    # The button says "remove it from this machine": the RUN command line removes that one file, sealed
    # with the keyring's passphrase here, once three words matched -- and a refused or a stale
    # confirmation removes nothing (both halves). Every other file of the volume stays as it is.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    pf = tmp_path / "keyring-passphrase"
    pf.write_text("p" * 64 + NL)
    keys, seen, run = in_container(tmp_path, env={"DENDRA_KEYRING_PASSPHRASE_FILE": str(pf)})
    phrase = keys / "recovery-phrase.json"
    _seal_phrase(phrase, "p" * 64, {"address": "dendra1aaa", "name": "dm1abc", "mnemonic": " ".join(WORDS)})
    assert " ".join(WORDS[:3]).encode() not in phrase.read_bytes(), "sealed: the words are not on the disk in clear"
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
    (keys / "identite-resolue").write_text("dm1abc" + NL)
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
    # a declaration recorded for ANOTHER identity is not this one's
    (keys / "payout-declared.json").write_text(json.dumps({"miner_id": "dm1other", "address": "dendra1cold"}))
    assert call(A, "/api/overview")[1]["payout"]["to_machine_key"] is True
    # an encrypted keyring whose passphrase is not there is "not opened", never "in clear"
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
    assert "IN CLEAR" in ui["text"]["keysAtRest"] and "encrypt-keys.sh" in ui["text"]["keysAtRest"]
    assert ui["hidden"]["payoutBanner"] is False and "this machine's own key (dendra1machine)" in ui["text"]["payoutBannerText"]
    enc = dict(base, keys={"at_rest": "encrypted", "backend": "file", "error": "", "clear_files": []},
               payout={"to_machine_key": False, "declared": "dendra1cold", "setting": "", "machine_address": "dendra1machine"})
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": enc}]})
    assert ui["text"]["keysAtRest"].startswith("encrypted") and ui["hidden"]["payoutBanner"] is True
    assert "dendra1cold" in ui["text"]["payoutDeclared"]
    # not read is not "in clear", and an unread payout raises no banner
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
    # Owner mode: the programme accepts the declaration from the OWNER's key only, and it is not on this
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
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **k: A.subprocess.CompletedProcess(cmd, 1, "", DENIED))
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
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **k: A.subprocess.CompletedProcess(cmd, 0, "", ""))
    assert A.containers() == ([], None)


def test_the_page_says_no_access_to_docker(app, tmp_path, monkeypatch):
    # The server's half alone proves nothing on screen: the page's own script is run on its answer.
    A, kit = app
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **k: A.subprocess.CompletedProcess(cmd, 1, "", DENIED))
    ov = call(A, "/api/overview")[1]
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
    assert ui["text"]["state"] == "no access to Docker" and ui["text"]["ctr"] == A.NO_DOCKER_ACCESS
    # and Docker that answers with no container says so, with no error
    ov = dict(ov, containers=[], docker_error=None)
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": ov}]})
    assert ui["text"]["state"] == "no miner container" and ui["text"]["ctr"] == "none"


def test_the_page_shows_unread_values_as_question_marks(app, tmp_path):
    # The server's half (None for an unread value) is benched below; the SCREEN's half is benched here, by
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
    # Owner's decision of 2026-10-07: the season ends at a fixed instant read against block times, not after
    # a count of days, so the screen gives the day and the end, never "N of M" (this replaced "1 of 30").
    assert t["day"] == "day 1, the season ends 7 November 2026, 23:59 UTC"
    assert t["ranked"] == "none yet" and t["lastDay"] == "no day ranked yet"
    assert t["paid"] == "0 DNDR over 0 ranked day(s)" and t["lastPaid"] == "—"

    def day_text(**status):
        prog = dict(none_yet, status=dict(running, **status))
        return run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, programme=prog)}]})["text"]["day"]
    # `ended` decides, not a count of days (this replaced "day 30 of 30 is the season over"): a late day is
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
    # Owner's decisions of 2026-10-05 and 2026-10-06: 0.05 DNDR per verified programme request, 0.008 per
    # consistent verdict, 0.002 per availability window proven on chain, at most 50 a day, paid only on a day
    # with verified work.
    # The parts are the shipped calculation applied to part of the facts, never a rate restated by the app.
    A, _ = app
    import final_season_calc as SC
    import final_season_rules
    assert A.RULES is final_season_rules.RULES and A.gross_of is SC.gross_of
    assert A.formula_of(40, 3, 1) == {"work_udndr": 150_000, "juror_udndr": 8_000, "presence_udndr": 80_000,
                                      "gross_udndr": 238_000}
    assert A.formula_of(60, 1, 0)["presence_udndr"] == 50 * 2_000
    # presence is paid only on a day with at least one verified request
    assert A.formula_of(40, 0, 2) == {"work_udndr": 0, "juror_udndr": 16_000, "presence_udndr": 0,
                                      "gross_udndr": 16_000}
    for facts in ((0, 0, 0), (7, 2, 3), (200, 30, 9), (1, 1, 0)):
        f = A.formula_of(*facts)
        assert f["gross_udndr"] == SC.gross_of(SC.Identity("x", *facts))
        assert f["work_udndr"] + f["juror_udndr"] + f["presence_udndr"] == f["gross_udndr"]
    assert b"public node" not in call(A, "/", token=False)[1]


def test_the_page_offers_no_exam_no_class_and_no_window_question(app):
    # The Final Testnet Season sets no window question and no class exam (owner's decisions of
    # 2026-10-05): a page that offered a retake, or showed a class or an estimate, would describe a
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
    # out at ITS rate.
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
    assert t["ranked"] == "—" and t["paid"] == "—" and t["lastPaid"] == "—"


def test_the_page_shows_the_new_key_s_phrase_after_a_refused_confirmation(tmp_path):
    # The key changed while the old phrase was on screen: the confirmation was refused (409) and the old
    # words stayed, the page never asking for the new ones until it was reloaded.
    view = {"installed": True, "containers": [], "docker_error": None, "identity": {}, "chain": {},
            "programme": {"enabled": False}, "recovery_confirmed": False}
    first = {"present": True, "address": "dendra1aaa", "words": WORDS, "ask": [1, 2, 3]}
    second = {"present": True, "address": "dendra1bbb", "words": OTHER, "ask": [4, 5, 6]}
    typed = ["type", {"1": WORDS[0], "2": WORDS[1], "3": WORDS[2]}]
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}],
                           "/api/recovery": [{"status": 200, "data": first}, {"status": 200, "data": second}],
                           "/api/recovery/confirm": [{"status": 409, "data": {"error": "no phrase to confirm"}}]},
                [typed, ["click", "confirmBtn"]])
    assert ui["words"] == OTHER and ui["hidden"]["recovery"] is False and "key changed" in ui["text"]["recoveryMsg"]
    # wrong words keep the same phrase on screen, with the reason
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": view}],
                           "/api/recovery": [{"status": 200, "data": first}, {"status": 200, "data": second}],
                           "/api/recovery/confirm": [{"status": 400, "data": {"error": "those words do not match"}}]},
                [typed, ["click", "confirmBtn"]])
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
    # Owner mode: the chain returns the stake to the OWNER, --yes only prepares the delete-miner the owner
    # signs, and a restart prints the owner's create-miner -- the card said the opposite of all three. Read as
    # exit-miner.sh reads it: from the CHAIN (the registration's creator), else from the kit's setting.
    A, kit = app
    ident = {"miner_id": "dm1abc", "address": "dendra1machine"}
    owned = A.exit_notes({}, ident, {"creator": "dendra1owner", "registered": True})
    assert owned["owner"] == "dendra1owner"
    assert "dendra1owner" in owned["after"] and "never to this machine's key" in owned["after"]
    assert "this miner's address" not in owned["after"] and "registers it again" not in owned["after"]
    assert "--yes --signed" in owned["how"] and "delete-miner the OWNER signs" in owned["how"]
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


# ── the miner's self-test (deploy/testnet-miner/miner_health.sh) ───────────────────────────────────
# The screen reads miner-health.last.json, which the hourly job writes, and runs a quick check on demand.
# It never decides health itself: no recorded run is "never", a file that is not the document is
# "unread", and neither is shown as fine.
# The period the documents of this bench carry. NOT the script's value, and deliberately unlike it: what is
# pinned here is that the application applies the period the DOCUMENT names -- an application that kept its
# own copy of the script's period would judge these documents against the wrong one, and fail. That the
# script's SCHEDULE_PERIOD_S is the period of the crontab line join.sh writes is confronted elsewhere
# (dendra/onchain-staging/dendra_mineur_sante_test.sh).
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
    # The file EXISTS and is not readable: that is not "no check has run" (never) -- it is a reading that failed.
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
    # THE PERIOD COMES FROM THE DOCUMENT, never from a copy kept in the application: a document that does
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
    assert "KO C4 availability (presence): ABSENT" in t["healthList"] and "fix: docker compose" in t["healthList"]
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
    # Keys in clear, the phrase still on the machine, the season paying the machine's key: choices, listed
    # apart with what changes them. A run whose checks are all ok stays "all checks ok".
    A, _ = app
    adv = [{"id": "A1", "name": "keys at rest", "measured": "the keyring is the TEST keyring",
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


# ── the silent juror seat (D3 b), the header pill (B8), the update instruction (A10), the subsidy (A11) ─────────
# miner_health.sh reads the judge role IN THE CONTAINER and carries it (`judge_role`), with its own advice A4 when
# the role is off; the screen's Role line comes from that reading, never from the kit's setting alone. The header
# pill is the container AND the last check, never green on "never checked". The update instruction is the line
# deploy/join.sh wrote into the kit's .env, read and never built here.
SILENT = {"id": "A4", "name": "silent juror seat", "measured": "miner -- silent juror seat: the judge role is off",
          "fix": "nothing, if this machine only mines"}


def test_the_role_is_the_container_s_reading_and_a_silent_seat_is_said(app, tmp_path, monkeypatch):
    A, kit = app
    doc = health_doc(rc=0, judge_role={"word": "off", "why": "the judge role is not requested"}, advice=[SILENT])
    doc["selftest"]["checks"][0]["state"] = "ok"
    doc["selftest"]["advice"] = [{"id": "A1", "name": "keys at rest", "measured": "TEST keyring", "fix": "encrypt"}]
    h = A._health_from(doc)
    # the script's advice first, then the self-test's; neither moves the verdict
    assert h["rc"] == 0 and [a["id"] for a in h["advice"]] == ["A4", "A1"]
    assert h["judge_role"] == {"word": "off", "why": "the judge role is not requested"}
    # a word that is none of the four, or no field at all, is not read: never "off", never "active"
    for bad in ({"word": "offf"}, "off", None, {"why": "x"}):
        assert A._health_from(dict(doc, judge_role=bad))["judge_role"] is None, bad
    # the overview: the role read in the container, and the setting said apart as what was REQUESTED
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
    for role, want, never in ((dict(word="off", why="", requested=False), "miner — silent juror seat", "miner and juror"),
                              (dict(word="active", why="", requested=True), "miner and juror", "silent"),
                              (dict(word="mute", why="", requested=True), "MUTE juror seat", "miner and juror"),
                              (dict(word=None, why="", requested=True), "whether it judges was not read", "miner and juror"),
                              (dict(word="unknown", why="x", requested=False), "silent juror seat (the judge role is not requested", "miner and juror")):
        t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, role=role)}]})["text"]
        assert want in t["role"] and never not in t["role"], (role, t["role"])
    # an OLDER server sends no `role`: its setting is a request, never shown as a juror that votes
    old = {k: v for k, v in ov.items() if k != "role"}
    t = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(old, judge=True)}]})["text"]
    assert "whether it judges was not read" in t["role"] and t["role"] != "miner and juror"


def test_the_header_pill_is_the_container_and_the_last_check_never_green_on_never(app, tmp_path):
    A, _ = app
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, docker_error=None, chain={},
              recovery_confirmed=None, containers=[{"service": "miner", "state": "running", "status": "Up"}])
    fresh = A._health_from(health_doc(rc=0, epoch=int(time.time()) - 60))
    cases = [({"state": "never", "why": "no check"}, "running · never checked", "pill warn"),
             ({"state": "unread", "why": "not JSON"}, "running · health not read", "pill warn"),
             (None, "running · health not read", "pill warn"),
             (A._health_from(health_doc(rc=1)), "running · a problem: C4 availability (presence)", "pill bad"),
             (A._health_from(health_doc(rc=2)), "running · not everything measured", "pill warn"),
             (A._health_from(health_doc(rc=0, epoch=int(time.time()) - 3 * SCHEDULE_S)), "is cron running?", "pill warn"),
             (A._health_from({k: v for k, v in health_doc(rc=0).items() if k != "schedule_period_s"}),
              "running · all checks ok, age not judged", "pill warn"),
             (fresh, "running · all checks ok, ", "pill ok")]
    for health, want, cls in cases:
        ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=health)}]})
        assert want in ui["text"]["state"] and ui["cls"]["state"] == cls, (health, ui["text"]["state"], ui["cls"]["state"])
    # a stopped miner is red whatever its last check said; Docker unread keeps its own red word
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(
        ov, health=fresh, containers=[{"service": "miner", "state": "exited", "status": "Exited (1)"}])}]})
    assert ui["text"]["state"] == "exited" and ui["cls"]["state"] == "pill bad"
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=fresh, docker_error="Docker did not answer: x")}]})
    assert ui["text"]["state"] == "Docker did not answer" and ui["cls"]["state"] == "pill bad"
    # a quick check run here, newer than the scheduled one, is what the pill says
    quick = A._health_from(health_doc(rc=0, epoch=int(time.time()), quick=True))
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health={"state": "never", "why": "x"})}],
                           "/api/health/run": [{"status": 200, "data": {"rc": 0, "health": quick}}]}, [["click", "healthBtn"]])
    assert ui["text"]["state"] == "running · quick check ok" and ui["cls"]["state"] == "pill ok"


def test_a_quick_check_never_hides_a_stopped_schedule(app, tmp_path):
    # A quick check is one reading, taken once. Left standing on an open page, it kept the pill green over a
    # scheduled check that had stopped running (cron dead): "is cron running?" never showed. It stands in for the
    # schedule only while the schedule is not stale, and only while it is itself younger than two periods.
    A, _ = app
    ov = dict(call(A, "/api/overview")[1], installed=True, identity={}, docker_error=None, chain={},
              recovery_confirmed=None, containers=[{"service": "miner", "state": "running", "status": "Up"}])
    now = int(time.time())
    quick = A._health_from(health_doc(rc=0, epoch=now, quick=True))

    def pill(sched, q):
        ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": dict(ov, health=sched)}],
                               "/api/health/run": [{"status": 200, "data": {"rc": 0, "health": q}}]}, [["click", "healthBtn"]])
        return ui["text"]["state"], ui["cls"]["state"], ui["text"]["healthWhen"]

    # the scheduled check is stale (three periods old): the newer quick check is shown, and the pill says the schedule
    text, cls, when = pill(A._health_from(health_doc(rc=0, epoch=now - 3 * SCHEDULE_S)), quick)
    assert cls == "pill warn" and "quick check ok, but the scheduled check is" in text and "is cron running?" in text, text
    assert when.startswith("quick check, ")
    # the schedule is fresh: the quick check stands in for it, green
    text, cls, _ = pill(A._health_from(health_doc(rc=0, epoch=now - 60)), quick)
    assert (text, cls) == ("running · quick check ok", "pill ok")
    # no schedule ever ran and the quick check is itself older than two periods: said, never green
    old_quick = A._health_from(health_doc(rc=0, epoch=now - 3 * SCHEDULE_S, quick=True))
    text, cls, _ = pill({"state": "never", "why": "x"}, old_quick)
    assert cls == "pill warn" and "no scheduled check since: is cron running?" in text, text
    # a quick check that names no period (an older script) cannot be judged by its age: never green
    no_period = A._health_from({k: v for k, v in health_doc(rc=0, epoch=now, quick=True).items() if k != "schedule_period_s"})
    text, cls, _ = pill({"state": "never", "why": "x"}, no_period)
    assert cls == "pill warn" and "age not judged" in text, text


def test_the_update_instruction_is_the_kit_s_line_read_never_built(app, tmp_path, monkeypatch):
    A, kit = app
    no_docker(A, monkeypatch)
    upd = ("cd /the/clone && git fetch origin && git branch -f kit-before-update && git checkout -B main origin/main, "
           "then re-run CONFIG_URL=http://ni.example/n.txt bash /the/clone/deploy/join.sh --judge (with the same options)")
    rerun = "CONFIG_URL=http://ni.example/n.txt bash /the/clone/deploy/join.sh --judge"
    (kit / ".env").write_text("MINER_ID=dm1abc" + NL + "DENDRA_KIT_UPDATE=" + upd + NL + "DENDRA_KIT_RERUN=" + rerun + NL)
    assert call(A, "/api/overview")[1]["update"] == {"instruction": upd, "rerun": rerun}
    # a line edited into something join.sh does not write is not shown; no line at all is said as not recorded
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


# ── one identity per card (deploy/join.sh --gpus) ───────────────────────────────────────────────────────
# The kit is FABRICATED with the shipped slot library (deploy/testnet-miner/slots.sh, found from this file,
# never from the subject's directory: a mutated copy of the application has no kit next to it), slot 0's
# .env, a slot 1 and a RETIRED slot 2. `docker` is a script on PATH that records each argv and the variables
# it was handed: the application's command lines are RUN through the real slots.sh, never matched as text.
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
    # A list that cannot be read is said, never an empty machine.
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
    # An OLDER kit, whose .env has no COMPOSE_PROFILES line: the judge's profile is named, for slot 0 only.
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
    # all = every ACTIVE identity: slot 0 and slot 1, never the retired slot 2
    open(log, "w").close()
    code, d = call(A, "/api/restart", body={"slot": "all"})
    assert code == 200 and [r["slot"] for r in d["results"]] == [0, 1]
    assert not [x for x in _argv(log) if "dendra-miner-g2" in x]
    # a retired slot is never started: slots.sh refuses it, and the screen shows the refusal
    code, d = call(A, "/api/start", body={"slot": 2})
    assert code == 200 and d["rc"] == 2 and "RETIRED" in d["text"]
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
    assert {"path": "/api/payout", "body": {"address": "", "slot": "all"}} in ui["bodies"]
    # one identity on the machine: the list stays hidden and the calls are the ones they were
    one = dict(base, multi=False, slots=[base["slots"][0]])
    ui = run_ui(tmp_path, {"/api/overview": [{"status": 200, "data": one}],
                           "/api/start": [{"status": 200, "data": {"rc": 0, "text": ""}}]}, [["click", "startBtn"]])
    assert ui["hidden"]["slotsCard"] is True and ui["hidden"]["payoutAllBtn"] is True
    assert {"path": "/api/start", "body": {}} in ui["bodies"]
