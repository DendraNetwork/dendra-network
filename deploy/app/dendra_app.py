#!/usr/bin/env python3
"""Dendra — the desktop application of a Dendra miner (Linux).

    dendra                 open the application (a native window when GTK and WebKit are installed,
                           the default browser otherwise)
    dendra --no-window     open nothing: serve the interface and print the path of its access file (to
                           open in a browser on this machine, or, on a host without one, to read for the
                           address to open through an SSH tunnel to the printed port)

WHAT IT DOES
It drives what `deploy/install.sh` and `deploy/join.sh` set up: the miner kit under
`deploy/testnet-miner`, its containers and its keys. It shows the miner's health, balance and
Final Testnet Season figures (those of the latest day the programme has ranked and published, and what
the ranked days paid in total); it starts, stops and restarts the miner; it shows the recovery phrase of
each new key once, until three of its words are typed back; it declares where the Final Testnet Season
rewards go. It installs nothing and joins nothing by itself: when the miner kit is not there, it says
which command does that.

WHAT IT NEVER DOES
It opens no port to the network: the interface listens on 127.0.0.1 only, every API call carries a
random token generated at start-up, and a request whose Host is not that loopback address is refused, so
neither another web page nor another machine can drive it. The token never appears on a command line
or in the terminal, where other users of the machine can read it (`/proc/<pid>/cmdline`, session logs):
the native window receives it in-process, and the browser opens a file only this user can read
(mode 0600, in a 0700 directory), removed after the first authenticated request. It never sends a key or
the recovery phrase anywhere: the phrase is read from the miner's own volume and shown on this screen
only.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
# The services directory is `services` in the development tree and `services` in the published
# one; the rules of the programme live there, once, and the formula rerun here must use the same file.
for _cand in ("services", os.path.join("prototype", "mode-a")):
    if os.path.isfile(os.path.join(REPO, _cand, "final_season_rules.py")):
        sys.path.insert(0, os.path.join(REPO, _cand))
        break
try:
    from final_season_calc import Identity, gross_of  # noqa: E402
    from final_season_rules import RULES, fingerprint  # noqa: E402
except ImportError:            # a kit without the services tree: the formula is not rerun, nor shown
    RULES, gross_of, Identity, fingerprint = None, None, None, None

KIT = os.environ.get("DENDRA_MINER_KIT", os.path.join(REPO, "deploy", "testnet-miner"))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "dendra")
RECOVERY_IN_CONTAINER = "/data/keys/recovery-phrase.json"
MINER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ADDRESS = re.compile(r"^dendra1[02-9ac-hj-np-z]{38,58}$")
INSTALL_HINT = "bash deploy/install.sh --yes"
# The docker CLI's refusal when this session's user cannot open the daemon's socket, in both the older
# ("...the Docker daemon socket at...") and the newer ("...the docker API at...") wording. It is what a
# user sees right after the installer added them to the `docker` group: the group applies to new sessions.
DOCKER_DENIED = re.compile(r"permission denied while trying to connect to the docker", re.I)
NO_DOCKER_ACCESS = "No access to Docker: log out and log back in (you were just added to the docker group)"
# How long the browser's access file may wait for its first authenticated request before it is removed.
LAUNCH_FILE_SECONDS = 120


# ── the miner kit ──────────────────────────────────────────────────────────────────────────────
def read_env(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def kit_env() -> dict:
    return read_env(os.path.join(KIT, ".env"))


def compose(*args, timeout=120) -> tuple[int, str]:
    """`docker compose` in the miner kit, with the profiles join.sh starts it with. A refusal of the
    Docker socket is named in the first line of the output, so every action that shows its output says
    what to do instead of a raw error."""
    if not shutil.which("docker"):
        return 127, "docker is not installed"
    prof = ["--profile", "judge"] if kit_env().get("DENDRA_MINER_JUDGE") == "1" else []
    try:
        r = subprocess.run(["docker", "compose", *prof, *args], cwd=KIT, capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"
    out = r.stdout + r.stderr
    if r.returncode != 0 and DOCKER_DENIED.search(out):
        out = f"{NO_DOCKER_ACCESS}\n{out}"
    return r.returncode, out


def docker_error(out: str) -> str:
    """What the screen says when `docker compose` failed."""
    if out.startswith(NO_DOCKER_ACCESS):
        return NO_DOCKER_ACCESS
    last = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return "Docker did not answer: " + (last[-1][:300] if last else "no output")


def containers() -> tuple:
    """(rows, None) when Docker answered, (None, why) when it did not. A failure is never an empty list:
    that reads as "no miner container" to a user who simply cannot reach Docker yet."""
    rc, out = compose("ps", "-a", "--format", "json")
    if rc != 0:
        return None, docker_error(out)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rows.append({"service": d.get("Service", ""), "state": d.get("State", ""), "status": d.get("Status", "")})
        elif line.startswith("["):
            try:
                rows += [{"service": d.get("Service", ""), "state": d.get("State", ""), "status": d.get("Status", "")}
                         for d in json.loads(line)]
            except ValueError:
                continue
    return rows, None


def miner_exec(*cmd, timeout=60) -> tuple[int, str]:
    return compose("exec", "-T", "miner", *cmd, timeout=timeout)


def miner_identity() -> dict:
    """The keyring of the miner container: its address and its on-chain identifier (the key name)."""
    # The keyring directory is the container's own setting (DENDRA_KEYRING_DIR in the kit's compose file),
    # read inside the container rather than restated here.
    rc, out = miner_exec("sh", "-c", 'dendrad keys list --keyring-backend test --keyring-dir "$DENDRA_KEYRING_DIR" '
                                     '--output json')
    if rc != 0:
        # A session that Docker refuses has a miner it cannot see, not a miner that is not running.
        return {"error": NO_DOCKER_ACCESS} if out.startswith(NO_DOCKER_ACCESS) else {}
    try:
        keys = json.loads(out[out.find("["):])
    except ValueError:
        return {}
    keys = [k for k in keys if MINER_ID.match(k.get("name", ""))]
    if not keys:
        return {}
    pref = [k for k in keys if k.get("name", "").startswith("dm1")] or keys
    return {"miner_id": pref[0]["name"], "address": pref[0].get("address", "")}


# ── the network ────────────────────────────────────────────────────────────────────────────────
def _get_json(url: str, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _fetch(url: str, timeout=10) -> tuple:
    """(HTTP status, body) of a GET, including an error status; (None, None) when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except OSError:
            return e.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def _not_published(code, body) -> bool:
    """The programme service's own answer for a day file that does not exist yet (`final_season_server._file`):
    a reading that there is nothing, unlike any other failure, which is a value not read."""
    if code != 404:
        return False
    try:
        return json.loads(body or b"{}").get("error") == "not published"
    except (ValueError, AttributeError):
        return False


def rest_base(env: dict) -> str:
    """REST of the node the miner reads. A miner on its own node uses host.docker.internal inside the
    container; from the host that is 127.0.0.1."""
    node = env.get("DENDRA_NODE", "")
    host = node.split("://", 1)[-1].rsplit(":", 1)[0] if node else ""
    if host in ("host.docker.internal", ""):
        host = "127.0.0.1"
    return f"http://{host}:1317"


def chain_view(env: dict, ident: dict) -> dict:
    """Balance, registration, subsidy: each value is a reading or None (None = not read), never a 0
    that stands for a failure."""
    base, addr, mid = rest_base(env), ident.get("address", ""), ident.get("miner_id", "")
    out = {"rest": base, "balance_udndr": None, "registered": None, "stake_udndr": None,
           "subsidy_claimable_udndr": None}
    if addr:
        b = _get_json(f"{base}/cosmos/bank/v1beta1/balances/{addr}")
        if isinstance(b, dict):
            out["balance_udndr"] = sum(int(c.get("amount", 0)) for c in b.get("balances", []) if c.get("denom") == "udndr")
    if mid:
        m = _get_json(f"{base}/dendra/jobs/v1/miner/{urllib.parse.quote(mid)}")
        p = _get_json(f"{base}/dendra/jobs/v1/params")
        if isinstance(m, dict):
            rec = m.get("miner")
            out["registered"] = isinstance(rec, dict)
            if isinstance(rec, dict):
                out["stake_udndr"] = int(rec.get("stake", 0) or 0)
                if isinstance(p, dict) and isinstance(p.get("params"), dict):
                    cap = int(rec.get("demand", 0) or 0) * int(p["params"].get("work_gate_bps", 0) or 0) // 10000
                    out["subsidy_claimable_udndr"] = max(0, cap - int(rec.get("subsidy_claimed", 0) or 0))
    return out


# The figures of a ranking row (`final_season_calc.to_json`) this screen shows. Each is REQUIRED, as
# `final_season_payout.load_day` reads them back: a missing field, or one that is not an integer, is a ranking
# not read, never a 0.
RANKED_FIGURES = ("presence", "verified_requests", "verdicts", "gross_udndr", "payable_udndr", "paid_udndr")


def formula_of(presence: int, requests: int, verdicts: int) -> dict | None:
    """The published formula (`final_season_calc.gross_of`) rerun on one day's published facts, part by part,
    so the screen can say what each fact earned. Every part is the one formula applied to part of the
    facts, never a rate restated here. None without the services tree."""
    if gross_of is None:
        return None
    work = gross_of(Identity("me", verified_requests=int(requests)))
    juror = gross_of(Identity("me", verdicts=int(verdicts)))
    gross = gross_of(Identity("me", presence=int(presence), verified_requests=int(requests),
                              verdicts=int(verdicts)))
    # Presence is paid only on a day with a verified request: its part is what the whole formula adds to
    # the other two, which is 0 on a day without work whatever the windows proven.
    return {"work_udndr": work, "juror_udndr": juror, "presence_udndr": gross - work - juror,
            "gross_udndr": gross}


def ranked_row(day: int, ranking, mid: str) -> dict:
    """This identity's line of the published ranking of `day`; ValueError when the document is not the
    ranking of that day. An identity ABSENT from a ranking had no fact counted that day — the ranking lists
    every identity with a verified request, a verdict or a proven window (`final_season_facts.day_identities`)
    — so it reads as zeros, said as not listed."""
    if not isinstance(ranking, dict) or type(ranking.get("day")) is not int or ranking["day"] != day \
            or not isinstance(ranking.get("ranking"), list):
        raise ValueError(f"not the ranking of day {day}")
    rows = [x for x in ranking["ranking"] if isinstance(x, dict) and x.get("miner_id") == mid]
    if len(rows) > 1:
        raise ValueError(f"{mid} is ranked twice on day {day}")
    if rows:
        if not all(type(rows[0].get(k)) is int for k in RANKED_FIGURES):   # not a float, a string, a bool
            raise ValueError(f"day {day}: a figure of {mid} is not an integer")
        out = {k: rows[0][k] for k in RANKED_FIGURES}
        out.update(day=day, listed=True, reason=str(rows[0].get("reason") or ""))
    else:
        out = dict.fromkeys(RANKED_FIGURES, 0)
        out.update(day=day, listed=False, reason="")
    # The formula is rerun only under the rules the ranking was computed with: another copy of the rules
    # gives another figure, and the screen would call a difference of versions a difference of figures.
    same = fingerprint is not None and ranking.get("rules_fingerprint") == fingerprint()
    out["rules_differ"] = fingerprint is not None and not same
    out["formula"] = formula_of(out["presence"], out["verified_requests"], out["verdicts"]) if same else None
    return out


def programme_view(env: dict, mid: str) -> dict:
    """The identity's Final Testnet Season figures as the programme PUBLISHED them: its line of the latest
    ranked day (`ranking/day-NNN.json`) and what all the ranked days paid it. A day is ranked only once it
    is final, past its last block (`final_season_rank.finality_blocks`), so the current day has no figure:
    nothing here is estimated or read live. Each value is a reading or None (not read): a failed read is
    never shown as 0 paid or 0 requests, which are real values an identity can have."""
    base = env.get("DENDRA_FINAL_SEASON_URL", "").rstrip("/")
    out = {"enabled": bool(base), "url": base, "status": None, "ranked_days": None,
           "season_paid_udndr": None, "latest": None}
    if not base:
        return out
    st = _get_json(f"{base}/status")
    out["status"] = st
    if not isinstance(st, dict) or not mid:
        return out
    day = st.get("day")
    # `day` is null while the service has no block height yet (`final_season_server._status`): the walk below has
    # no bound then, and nothing is read. Before the season starts (negative) there is no day to walk.
    if not isinstance(day, int) or isinstance(day, bool):
        return out
    paid, d, latest = 0, 0, None
    # Rankings are published in day order, each once its day is final: the first day "not published" ends
    # the walk, and no day from the current one on can be ranked yet, so none is asked for.
    while d < day:
        code, body = _fetch(f"{base}/ranking/day-{d:03d}.json")
        if _not_published(code, body):
            break                              # the days from here on are not ranked yet
        if code != 200:
            return out                         # a ranking not read: the total would be a partial sum
        try:
            latest = ranked_row(d, json.loads(body), mid)
        except (ValueError, TypeError, AttributeError, KeyError):
            return out
        paid += latest["paid_udndr"]
        d += 1
    out.update(season_paid_udndr=paid, ranked_days=d, latest=latest)
    return out


# ── recovery phrase and settings ───────────────────────────────────────────────────────────────
def settings() -> dict:
    try:
        with open(os.path.join(CONFIG_DIR, "app.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    p = os.path.join(CONFIG_DIR, "app.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.chmod(p + ".tmp", 0o600)
    os.replace(p + ".tmp", p)


def recovery_phrase() -> dict:
    rc, out = miner_exec("cat", RECOVERY_IN_CONTAINER, timeout=30)
    if rc != 0:
        return {"present": False}
    try:
        d = json.loads(out[out.find("{"):])
    except ValueError:
        return {"present": False}
    words = str(d.get("mnemonic", "")).split()
    if len(words) not in (12, 24):
        return {"present": False}
    return {"present": True, "address": d.get("address", ""), "words": words}


# Run INSIDE the miner container: the address of the kept phrase and its number of words, never the words.
_RECOVERY_HEAD = ("import json, sys; d = json.load(open(sys.argv[1])); "
                  "print(json.dumps({'address': str(d.get('address') or ''), "
                  "'words': len(str(d.get('mnemonic') or '').split())}))")


def recovery_head() -> dict:
    """What the overview needs to know at every refresh, whose phrase is kept, without the phrase: the
    words leave the container only when they are to be shown (`recovery_phrase`)."""
    rc, out = miner_exec("python3", "-c", _RECOVERY_HEAD, RECOVERY_IN_CONTAINER, timeout=30)
    if rc != 0:
        return {"present": False}
    try:
        d = json.loads(out[out.find("{"):])
        n = int(d["words"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"present": False}
    return {"present": n in (12, 24), "address": str(d.get("address") or "")}


def phrase_confirmed(rp: dict, s: dict) -> bool:
    """True only when the phrase on the miner's volume is the one whose words were typed back: the
    confirmation is kept per ADDRESS, so a new key's phrase is shown again. A phrase without an address
    is never taken as confirmed (two empty strings are not the same key), and the old per-user flag
    `recovery_confirmed` is ignored: it hid every later key's phrase."""
    addr = str(rp.get("address") or "")
    return bool(rp.get("present")) and bool(addr) and addr == s.get("recovery_confirmed_address")


def challenge_positions(n_words: int) -> list:
    """Three positions the user must type back before the phrase is marked as written down."""
    rnd = secrets.SystemRandom()
    return sorted(rnd.sample(range(1, n_words + 1), 3))


# ── HTTP ───────────────────────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, port: int):
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.challenge = None          # (address of the phrase shown, positions asked)
        self.launch_file = None        # the browser's access file, until its first authenticated request
        self.lock = threading.Lock()

    def forget_launch_file(self) -> None:
        with self.lock:
            path, self.launch_file = self.launch_file, None
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def snap_browser_dir() -> str:
    """`~/snap/<name>/common` when the program that opens an .html file here is a snap, "" otherwise.
    A snap browser (Ubuntu's default Firefox is one) is confined: it is refused $XDG_RUNTIME_DIR and the
    hidden directories of $HOME alike, so it would answer "access denied" to the access file; its own
    `~/snap/<name>/common` it can read. Any other browser reads that directory too."""
    try:
        r = subprocess.run(["xdg-mime", "query", "default", "text/html"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # A snap's desktop files are named <snap>_<application>.desktop (firefox_firefox.desktop).
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]*)_[A-Za-z0-9_.-]+[.]desktop", r.stdout.strip())
    if r.returncode != 0 or not m:
        return ""
    d = os.path.join(os.path.expanduser("~"), "snap", m.group(1), "common")
    return d if os.path.isdir(d) else ""


def launch_dir() -> str:
    """A directory only this user can enter: in a snap browser's own directory when the browser is one,
    else under $XDG_RUNTIME_DIR (a per-user tmpfs), else under the user's cache directory. Refused if it
    is not a real directory owned by this user."""
    base = snap_browser_dir()
    if not base:
        base = os.environ.get("XDG_RUNTIME_DIR", "")
    if not base or not os.path.isdir(base):
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "dendra")
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{d} is not a directory of this user")
    os.chmod(d, 0o700)
    return d


def write_launch_file(port: int, token: str) -> str:
    """A page that sends the browser to the interface with its token in the fragment. The browser is given
    the PATH of this file, so the token is in no command line; the file is created mode 0600."""
    fd, path = tempfile.mkstemp(prefix="open-", suffix=".html", dir=launch_dir())
    url = f"http://127.0.0.1:{port}/#t={token}"
    page = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            f'<meta http-equiv="refresh" content="0;url={url}"><title>Dendra</title>'
            f'<script>location.replace({json.dumps(url)})</script><a href="{url}">Open Dendra</a>')
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
    os.chmod(path, 0o600)
    return path


APP: App | None = None
UI = os.path.join(HERE, "ui", "index.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "dendra-app"

    def log_message(self, fmt, *args):
        return

    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{APP.port}", f"localhost:{APP.port}")

    def _auth(self) -> bool:
        ok = self._host_ok() and hmac.compare_digest(self.headers.get("X-Dendra-App", ""), APP.token)
        if ok and APP.launch_file:
            APP.forget_launch_file()       # the browser has the token: its access file has done its job
        return ok

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(UI, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if not self._auth():
            return self._send(401, {"error": "token"})
        if path == "/api/overview":
            return self._send(200, self._overview())
        if path == "/api/recovery":
            return self._recovery()
        if path == "/api/logs":
            rc, out = compose("logs", "--no-color", "--tail", "200", "miner", timeout=60)
            return self._send(200, {"rc": rc, "text": out[-60000:]})
        return self._send(404, {"error": "route"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "token"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            doc = json.loads(self.rfile.read(n) or b"{}") if 0 <= n <= 65536 else {}
        except ValueError:
            doc = {}
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/api/start", "/api/stop", "/api/restart"):
            verb = {"/api/start": ("up", "-d", "--no-build"), "/api/stop": ("stop",),
                    "/api/restart": ("restart", "miner")}[path]
            rc, out = compose(*verb, timeout=600)
            return self._send(200, {"rc": rc, "text": out[-4000:]})
        if path == "/api/recovery/confirm":
            return self._recovery_confirm(doc)
        if path == "/api/payout":
            return self._payout(doc)
        return self._send(404, {"error": "route"})

    def _overview(self) -> dict:
        env = kit_env()
        if not env:
            return {"installed": False, "kit": KIT,
                    "hint": f"No miner on this machine yet. In a terminal, from {REPO}: {INSTALL_HINT}"}
        rows, derr = containers()
        ident = miner_identity()
        # The published figures only (`programme_view`): the current day is not ranked yet, and this screen
        # puts no estimate in place of a ranking.
        prog = programme_view(env, ident.get("miner_id", ""))
        s = settings()
        # The phrase's address only: the words are not fetched at every refresh (`recovery_head`).
        rp = recovery_head()
        return {"installed": True, "kit": KIT, "containers": rows, "docker_error": derr, "identity": ident,
                "chain": chain_view(env, ident), "programme": prog,
                # None: no phrase to show (none kept for this key, or not read).
                "recovery_confirmed": phrase_confirmed(rp, s) if rp.get("present") else None,
                "payout_address": s.get("payout_address", ""),
                "judge": env.get("DENDRA_MINER_JUDGE") == "1", "rules": RULES}

    def _recovery(self):
        rp = recovery_phrase()
        if phrase_confirmed(rp, settings()):
            return self._send(200, {"present": False, "confirmed": True})
        if not rp.get("present"):
            return self._send(200, rp)
        with APP.lock:
            APP.challenge = (rp["address"], challenge_positions(len(rp["words"])))
            ask = APP.challenge[1]
        return self._send(200, dict(rp, ask=ask))

    def _recovery_confirm(self, doc):
        rp = recovery_phrase()
        with APP.lock:
            shown = APP.challenge
        # The positions were drawn for ONE phrase: if the key changed since, they prove nothing about it.
        if not rp.get("present") or not shown or shown[0] != rp["address"]:
            return self._send(409, {"error": "no phrase to confirm"})
        # The confirmation is kept per address (`phrase_confirmed`): one kept for no address would be
        # accepted here and never honoured there, and the phrase would come back at every refresh.
        if not rp["address"]:
            return self._send(422, {"error": "this phrase names no address, so its confirmation cannot be kept: "
                                             "write it down; it will keep being shown"})
        given = doc.get("words") or {}
        ok = all(str(given.get(str(i), "")).strip().lower() == rp["words"][i - 1] for i in shown[1])
        if not ok:
            return self._send(400, {"error": "those words do not match: check what you wrote down"})
        s = settings()
        s.pop("recovery_confirmed", None)
        s["recovery_confirmed_address"] = rp["address"]
        save_settings(s)
        return self._send(200, {"ok": True})

    def _payout(self, doc):
        addr = str(doc.get("address", "")).strip()
        if not ADDRESS.match(addr):
            return self._send(400, {"error": "a dendra1... address is required"})
        ident = miner_identity()
        if not ident.get("miner_id"):
            return self._send(409, {"error": ident.get("error") or "miner not running"})
        rc, out = miner_exec("python3", "final_season_miner.py", "payout", "--miner", ident["miner_id"],
                             "--address", addr)
        if rc == 0:
            s = settings()
            s["payout_address"] = addr
            save_settings(s)
        return self._send(200 if rc == 0 else 502, {"rc": rc, "text": out[-2000:]})


def open_window(url: str) -> bool:
    """A native window when GTK 3 and WebKit2 are installed; False otherwise (the caller falls back to the
    default browser)."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gtk, WebKit2
    except (ImportError, ValueError):
        return False
    win = Gtk.Window(title="Dendra")
    win.set_default_size(1100, 760)
    view = WebKit2.WebView()
    view.load_uri(url)
    win.add(view)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    return True


def _run(a) -> int:
    base = f"http://127.0.0.1:{APP.port}/"
    # The native window receives the token in-process: it is in no command line.
    if not a.no_window and open_window(f"{base}#t={APP.token}"):
        return 0
    # Everything else goes through the access file: a URL carrying the token, printed or handed to the
    # browser's command line, is readable by any local user (/proc/<pid>/cmdline, session logs), and with
    # it the 24 words.
    try:
        APP.launch_file = write_launch_file(APP.port, APP.token)
    except OSError as e:
        print(f"Dendra cannot write its access file ({e}): nothing was opened.", flush=True)
        return 2
    if a.no_window:
        # No timer here: on a host without a browser the file is READ (for the address to open through an
        # SSH tunnel), which no request marks; it goes at the first authenticated request or when Dendra stops.
        print(f"Dendra is running at {base}. Its access link is in {APP.launch_file} (readable by you only, "
              f"removed after first use): open that file in a browser on this machine, or, on a host without "
              f"one, read the address in it and open it through an SSH tunnel to port {APP.port}. That address "
              f"stays valid until Dendra stops; keep it to open another tab.", flush=True)
    else:
        import webbrowser
        # The browser may not start at all (no display, an SSH session) or may not be able to read the file:
        # the file's path is printed either way, and a failed start keeps the file instead of timing it out,
        # so the reader is never left with nothing to open.
        print(f"Opening Dendra ({base}) in your browser (install python3-gi and gir1.2-webkit2-4.1 for a "
              f"window). If no page appears, open {APP.launch_file} in a browser (readable by you only, "
              f"removed after first use).", flush=True)
        try:
            started = webbrowser.open(pathlib.Path(APP.launch_file).as_uri())
        except Exception:  # noqa: BLE001 — a launcher that raises is a launcher that did not start
            started = False
        if started:
            t = threading.Timer(LAUNCH_FILE_SECONDS, APP.forget_launch_file)
            t.daemon = True
            t.start()
        else:
            print(f"No browser could be started. Open {APP.launch_file} in a browser on this machine, or read "
                  f"the address in it and open it through an SSH tunnel to port {APP.port}.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    global APP
    ap = argparse.ArgumentParser(description="Dendra miner application")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    APP = App(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return _run(a)
    finally:
        APP.forget_launch_file()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
