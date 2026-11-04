#!/usr/bin/env python3
"""Dendra — the desktop application of a Dendra miner (Linux).

    dendra                 open the application (a native window when GTK and WebKit are installed,
                           the default browser otherwise)
    dendra --no-window     open nothing: serve the interface and print the path of its access file (to
                           open in a browser on this machine, or, on a host without one, to read for the
                           address to open through an SSH tunnel to the printed port)
    dendra --windows --port P --access-file F
                           serve the interface to the Windows browser of a PC whose miner runs in WSL 2,
                           as `deploy/app/open_windows.ps1` starts it: the one-time token comes on the
                           standard input, F is the access page that script wrote in the Windows user's
                           local application data, and P is the fixed port WSL forwards to Windows

What it does
It drives what `deploy/install.sh` and `deploy/join.sh` set up: the miner kit under
`deploy/testnet-miner`, its containers and its keys. It shows the result of the miner's self-test
(`deploy/testnet-miner/miner_health.sh`: the last hourly run with its age, and a quick run on demand), the
miner's balance and Final Testnet Season figures (those of the latest day the programme has ranked and
published, and what the ranked days paid in total); it starts, stops and restarts the miner; it shows the recovery phrase of
each new key until three of its words are typed back, and then removes it from this machine; it says
whether the miner's keys are kept encrypted or unencrypted, and whether the Final Testnet Season pays this
machine's own key; it declares where the Final Testnet Season rewards go. It splits what the ranked days
earned by role (work, verdicts, presence, the judge presence reward) and projects it to the season's end, naming
the average it uses and how it counts the days left (`earnings_view`); the projection promises nothing. It shows the judge role the miner
container reported to the last check (a judge that is off is shown as such), the judge's memory as its daemon's memory
watch wrote it (resident or on demand, whether the guard lets the model load, the audits left out for lack of memory;
`judge_memory_view`; a reading that is missing is "not read", never a healthy memory), the judge's seat with the chain (its
declaration and the audits it was seated on without answering, against the limit at which the chain stops drawing it,
`judge_seat_from` and `judge_seat_limit`), and how this machine updates its kit,
as `deploy/join.sh` recorded it. It has two switches: Judge (the kit's `judge.sh on|off`) and GPU mining (with the
card off, the miner answers on the CPU with the judge model), each greyed with its reason where the machine
cannot take it. It says in plain words how the miner is (green, orange or red, from one table of rules, each line
with its gesture and the page of the site that settles it), and it tests the machine on request: the speed of the
miner's engine in words per second and the time of one verdict, measured inside the miner container by the calls
the miner and the judge make, or "not measured" with the reason.
Its first screen is a dashboard: one card per subject (the judge's mode and abstentions, the capacity report, the
kit, the node, the relay, the work of the latest ranked day), each green, amber or red with one sentence. A
diagnosis view lists every problem the existing outputs name, each with what is wrong, why it matters (pay, judge,
network) and the exact command or setting that fixes it; a pay view lists the points and DNDR of every ranked day,
with the ranking's own reason in plain words for a day paid nothing; on a machine that runs one identity per card,
one row per card carries what it serves, the judge's memory, the work and pay of the latest ranked day, and its
judge. When something blocks the pay, a desktop notification says so, once per blocker until it clears, through
the machine's notifier; and the containers' log lines are each explained in a plain sentence, the raw line one
click away. It installs nothing and joins nothing by itself: when the miner kit is not there, it says which
command does that.
It tells whether the node of this machine can be reached from outside (`deploy/node_reachability.sh`), names the
public address when it can, and says how to open port 26656, which is optional.
On a machine that runs one identity per card (`deploy/join.sh --gpus`), it lists every identity and acts on
the one selected (never slot 0 by default) and can declare one payout address for all of them, each
identity signing its own declaration.

What it never does
It opens no port to the network: the interface listens on 127.0.0.1 only, every API call carries a
random session generated at start-up, and a request whose Host is not that loopback address is refused, so
neither another web page nor another machine can drive it. The session has two halves. The session token travels
in one cookie only (HttpOnly, SameSite=Strict, Path=/, host-only for 127.0.0.1, named after the port): it is in no
URL, no access file, no command line, no terminal and no storage a script of the page can read. A browser sends
that cookie to every port of 127.0.0.1, so it is not enough alone: the other half is a value kept in the tab's
sessionStorage, which only a page of this port can read, and the page sends it in a header. Every answer to the
session carries a third value the page checks, so a program that takes the port after this application stopped is
not read as Dendra. Every launch gets in the same way, by a one-time token posted to /handoff: on Linux the browser
opens an access file only this user can read (mode 0600, in a 0700 directory) and the native window receives that
form in-process; from Windows (`--windows`) open_windows.ps1 writes the access page and hands the one-time token on
the standard input. /handoff accepts the one-time token once, removes the access page, sets the cookie and leaves
the other two values in the tab. Before it opens the browser, the Windows script checks at /handoff/proof that the
program on the port holds that token. WSL 2 forwards the port to the loopback of Windows only, and the application
stops when the script's window closes. Showing the 24 recovery words and changing the payout address also take a
confirmation code that this application prints in the terminal or window that started it, never on the page
(`App.issue_code`), beside the change it confirms. A launch with neither a terminal nor a window refuses those two
gestures and says how to start it. It never sends a key or the recovery phrase anywhere: the phrase is opened inside
the miner container (with the keyring's passphrase, which stays there) and shown on this screen only.
Stop comes first: it interrupts the action in progress (its subprocess is terminated, then killed after a short
grace) and then stops the miner of the identity it names and of those the action acts on; another action never
makes it wait for long or refuses it (`App.take_stop`).
"""
from __future__ import annotations

import argparse
import base64
import calendar
import datetime
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import pathlib
import re
import secrets
import shlex
import shutil
import signal
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
    from final_season_rules import RULES, end_epoch, fingerprint, kit_rules, pays_judge_presence, rule_set_of  # noqa: E402
except ImportError:            # a kit without the services tree: the formula is not rerun, nor shown
    RULES, gross_of, Identity, rule_set_of, end_epoch, pays_judge_presence = None, None, None, None, None, None
    fingerprint, kit_rules = None, None

KIT = os.environ.get("DENDRA_MINER_KIT", os.path.join(REPO, "deploy", "testnet-miner"))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "dendra")
KEYDIR_IN_CONTAINER = "/data/keys"
RECOVERY_IN_CONTAINER = "/data/keys/recovery-phrase.json"
MINER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ADDRESS = re.compile(r"^dendra1[02-9ac-hj-np-z]{38,58}$")
INSTALL_HINT = "bash deploy/install.sh --yes"
# The docker CLI's refusal when this session's user cannot open the daemon's socket, in both the older
# ("...the Docker daemon socket at...") and the newer ("...the docker API at...") wording. It is what a
# user sees right after the installer added them to the `docker` group: the group applies to new sessions.
DOCKER_DENIED = re.compile(r"permission denied while trying to connect to the docker", re.I)
NO_DOCKER_ACCESS = "No access to Docker: log out and log back in (you were just added to the docker group)"
# How long the browser's access file may wait for its use at /handoff before it and its one-time token go.
LAUNCH_FILE_SECONDS = 120
# The one-time token open_windows.ps1 hands over on the standard input: 32 random bytes in URL-safe base64
# (43 characters), refused shorter so that a truncated pipe cannot leave a guessable token.
HANDOFF_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}")
# A Windows console set to UTF-8 makes the writer of a child's standard input start with a byte order mark: it is
# no part of the token, and the token line is read without it.
BYTE_ORDER_MARK = chr(0xFEFF)
# open_windows.ps1 checks, before it opens the browser, that the program on the port holds the one-time token:
# it posts a fresh nonce (16 random bytes, in hex) and expects HMAC-SHA256(token, label + nonce) in hex.
HANDOFF_PROOF_LABEL = b"dendra-handoff-proof:"
HANDOFF_NONCE = re.compile(r"[0-9a-f]{32}")
# A fixed port is one WSL can forward before the application starts; the privileged range is not taken.
WINDOWS_PORT_MIN, WINDOWS_PORT_MAX = 1024, 65535
# The session cookie's name carries the port: cookies are not kept apart by port, and two copies of this
# application in one browser must not overwrite each other's session.
SESSION_COOKIE = "dendra_session_"
# That cookie also reaches every other port of 127.0.0.1, where any local program may listen. The page sends the
# other half of the session in this header, from its tab's sessionStorage, which a page of another port cannot read.
SESSION_HEADER = "X-Dendra-Session"
# Every answer to the session carries this value, which the page holds from /handoff and never sends: a program
# that takes the port after this application stopped cannot show it, and the page stops there.
INSTANCE_HEADER = "X-Dendra-Instance"
# The confirmation code asked before the recovery words are shown or the payout address changes: printed in the
# terminal or window that started this application, never sent to the page. It is given for one change (the payout's
# address and slots, the slot whose words are shown), and goes with its use or with its CODE_TRIES-th refused try.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_SECONDS = 300
CODE_TRIES = 3
CODE_PURPOSES = {"recovery": "showing the 24 recovery words", "payout": "changing the payout address"}
CODE_HEADER = "X-Dendra-Code"
# Started from the applications menu (install_app.sh writes Terminal=false) without the native window, this
# application prints into the session's log, which nobody reads: it then shows no code at all and says this. The
# page shows at most 300 characters of an error (ui/index.html, askCode), this sentence included.
NO_CODE_SINK = ("Dendra was started without a terminal or its own window, so it cannot show a confirmation code. To "
                "show the 24 recovery words or change the payout address, start Dendra from a terminal by typing "
                "dendra, or install python3-gi and gir1.2-webkit2-4.1 for its own window")
# Stop first: the subprocess of the action it interrupts gets SIGTERM, then SIGKILL after STOP_GRACE_S. Stop waits
# at most STOP_WAIT_S for that action to give the lock back, then stops the miner beside whatever still runs (a read
# that is not a subprocess of the action: the list of identities, the reachability check).
STOP_GRACE_S = 5
STOP_WAIT_S = 20
INTERRUPTED_RC = 130
INTERRUPTED_TEXT = "interrupted by Stop"
# Slot 0's compose project (the `name:` of the kit's docker-compose.yml, slots.sh::slot_project) and the service
# judge.sh on runs as a one-off container to pull the judge's model.
SLOT0_PROJECT = "dendra-miner"
JUDGE_PULL_SERVICE = "judge-model-init"


# --- the miner kit ---
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


# --- one identity per card ---
# deploy/join.sh --gpus runs one miner identity per NVIDIA card, a "slot": slot 0 is this kit's installation
# (compose project dendra-miner, the kit's .env), slot k its own project, env and files under gpu/<k>/.
# deploy/testnet-miner/slots.sh is the one place that knows a slot; this application asks it (`list`, and
# `run` for a slot k's compose commands) and keeps no second copy of which project, env or files a slot has.
SLOTS_LIB = os.path.join(KIT, "slots.sh")


def slots() -> tuple:
    """([{"slot", "project", "state", "uuid", "miner_id", "dir"}], None) when the kit's slots were read,
    (None, why) when they were not -- never [] for a list that could not be read. A kit without the slot
    library predates it and runs one identity: slot 0, the kit itself."""
    if not os.path.isfile(SLOTS_LIB):
        return [{"slot": 0, "project": "dendra-miner", "state": "active", "uuid": "",
                 "miner_id": kit_env().get("MINER_ID", ""), "dir": KIT}], None
    try:
        r = subprocess.run(["bash", SLOTS_LIB, "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"the identities of this machine could not be listed: {type(e).__name__}"
    if r.returncode != 0:
        said = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
        return None, "the identities of this machine could not be listed: " + (said[-1][:300] if said else f"exit {r.returncode}")
    rows = []
    for line in r.stdout.splitlines():
        p = line.rstrip("\r").split("|", 5)
        if len(p) != 6 or not p[0].isdigit():
            return None, f"the list of identities carries a line this application does not read: {line[:120]!r}"
        rows.append({"slot": int(p[0]), "project": p[1], "state": p[2], "uuid": p[3], "miner_id": p[4], "dir": p[5]})
    if not rows or rows[0]["slot"] != 0:
        return None, "the list of identities does not start with slot 0"
    return rows, None


def resolve_slots(value, allow_all=False) -> tuple:
    """([k, ...], None) or (None, (HTTP status, body)). A missing slot names slot 0 only on a kit that runs one
    identity: on a machine that runs several, an action is never taken on slot 0 by default. `all` is every
    active slot (a retired one is stopped on purpose). An unknown slot is a 400."""
    rows, why = slots()
    if rows is None:
        return None, (502, {"error": why})
    if value is None or value == "":
        if len(rows) == 1:
            return [0], None
        return None, (400, {"error": f"this machine runs {len(rows)} miner identities, one per card: name the slot"})
    if value == "all":
        if not allow_all:
            return None, (400, {"error": "this takes one slot, not all"})
        ks = [r["slot"] for r in rows if r["state"] == "active"]
        return (ks, None) if ks else (None, (409, {"error": "no active identity on this machine"}))
    s = str(value)
    if not s.isdigit() or (len(s) > 1 and s.startswith("0")):
        return None, (400, {"error": f"'{s[:20]}' is not a slot number"})
    known = {r["slot"]: r for r in rows if r["state"] in ("active", "retired")}
    if int(s) not in known:
        return None, (400, {"error": f"no slot {int(s)} on this machine"})
    return [int(s)], None


def slot_row(k: int) -> dict:
    rows, _ = slots()
    return next((r for r in rows or [] if r["slot"] == k), {"slot": k, "dir": KIT if k == 0 else ""})


def _for(fn, slot, *a, **kw):
    """fn(*a) for slot 0, the call this module always made, and fn(*a, slot=k) for a slot k."""
    return fn(*a, **kw) if not slot else fn(*a, slot=slot, **kw)


COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def slot0_compose_env(env: dict) -> dict:
    """The environment slot 0's `docker compose` runs with: this process's, without any COMPOSE_* and without
    the variables the kit's compose files interpolate (read from those files, never listed here). Slot 0 is
    the kit's directory, and its .env names its project, files and identity -- but the environment of the
    process wins over that file: a COMPOSE_PROJECT_NAME exported in the shell that started this application
    (a slot k's env sourced by hand carries one) would rename slot 0, so another key volume, and a MINER_ID
    would give it another identity. slots.sh::slot_compose does the same for every slot."""
    names = set()
    for f in [x for x in (env.get("COMPOSE_FILE") or "docker-compose.yml").split(":") if x]:
        try:
            with open(os.path.join(KIT, f), encoding="utf-8") as fh:
                names.update(COMPOSE_VAR.findall(fh.read()))
        except OSError:
            continue
    return {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_") and k not in names}


def _signal_group(pgid: int, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def run_child(argv, timeout, cwd=None, env=None, merge=False) -> tuple:
    """(exit code, stdout, stderr, how) of a command in its own process group. `how` is "" when it ended by
    itself, "timeout" when it was killed past `timeout`, "interrupted" when Stop asked for the action that runs it
    (the group gets SIGTERM, then SIGKILL after STOP_GRACE_S; or it is not started at all), and "error" when it
    could not start (stderr says why). Only the thread that runs the current action is interrupted: a read made for
    a refresh of the page is not part of it."""
    app = APP
    mine = app is not None and app.runs_action_here()
    kw = dict(cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
              text=True, start_new_session=True)
    try:
        if mine:
            # Checked and registered under the lock Stop takes, so a child is never started after Stop looked.
            with app.lock:
                if app.stop_requested():
                    return INTERRUPTED_RC, "", INTERRUPTED_TEXT, "interrupted"
                p = subprocess.Popen(argv, **kw)
                app.children[p.pid] = p
        else:
            p = subprocess.Popen(argv, **kw)
    except OSError as e:
        return 1, "", f"{type(e).__name__}: {e}", "error"
    deadline = time.monotonic() + timeout
    how, term_at, killed_at = "", None, None
    try:
        while True:
            try:
                out, err = p.communicate(timeout=0.2 if mine else max(0.05, deadline - time.monotonic()))
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if killed_at is not None and now - killed_at >= STOP_GRACE_S:
                    # Killed, and its output still open: a process that left the group holds the pipe. The
                    # action is not held for it.
                    out, err = "", ""
                    break
                if mine and term_at is None and how == "" and app.stop_requested():
                    how, term_at = "interrupted", now
                    _signal_group(p.pid, signal.SIGTERM)
                elif term_at is not None and killed_at is None and now - term_at >= STOP_GRACE_S:
                    killed_at = now
                    _signal_group(p.pid, signal.SIGKILL)
                elif how == "" and now >= deadline:
                    how, killed_at = "timeout", now
                    _signal_group(p.pid, signal.SIGKILL)
    finally:
        if mine:
            with app.lock:
                app.children.pop(p.pid, None)
    rc = p.poll()
    return (rc if rc is not None else -int(signal.SIGKILL)), out or "", err or "", how


def compose_verb(args) -> str:
    """The compose command in `args`, after the global options a caller puts first (`--profile judge stop`)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] == "--profile" else 1
    return args[i] if i < len(args) else ""


def compose(*args, timeout=120, slot=0) -> tuple[int, str]:
    """`docker compose` for one identity. Slot 0 is the kit's directory form, as join.sh starts it: compose
    reads the kit's .env -- its COMPOSE_FILE and its COMPOSE_PROFILES. A slot k goes through slots.sh (`run`),
    which hands compose that slot's project, env and files and nothing of slot 0's. A refusal of the Docker
    socket is named in the first line of the output, so every action that shows its output says what to do
    instead of a raw error.
    The judge's profile is never derived from the judge role: every slot of a machine may vote, but only slot 0
    hosts the judge's engine, and a profile named for a slot k would start a second engine there and pull the
    judge's model again. It is named here only for slot 0 of an older kit, whose .env has no COMPOSE_PROFILES
    line at all (its judge was started with --profile judge)."""
    if not shutil.which("docker"):
        return 127, "docker is not installed"
    run_env = None
    if slot:
        if not os.path.isfile(SLOTS_LIB):
            return 2, f"this kit has no slot library ({SLOTS_LIB}): it runs one identity"
        argv = ["bash", SLOTS_LIB, "run", str(int(slot)), *args]
    else:
        env = kit_env()
        prof = ["--profile", "judge"] if ("COMPOSE_PROFILES" not in env and env.get("DENDRA_MINER_JUDGE") == "1") else []
        argv = ["docker", "compose", *prof, *args]
        run_env = slot0_compose_env(env)
    rc, stdout, stderr, how = run_child(argv, timeout, cwd=KIT, env=run_env)
    if how == "interrupted":
        return INTERRUPTED_RC, INTERRUPTED_TEXT
    if how == "timeout":
        return 1, f"TimeoutExpired: docker compose {compose_verb(args)} did not end within {timeout} s"
    if how == "error":
        return 1, stderr
    out = stdout + stderr
    if rc != 0 and DOCKER_DENIED.search(out):
        out = NO_DOCKER_ACCESS + _NL + out
    return rc, out


def docker_error(out: str) -> str:
    """What the screen says when `docker compose` failed."""
    if out.startswith(NO_DOCKER_ACCESS):
        return NO_DOCKER_ACCESS
    last = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return "Docker did not answer: " + (last[-1][:300] if last else "no output")


def remove_judge_pull() -> tuple:
    """(rc, text). Removes the running one-off containers of slot 0's judge model pull: judge.sh on starts it with
    `compose run`, and `compose stop` never reaches a one-off container. Each is found by its compose labels and
    confirmed by its working_dir label, as slots.sh::slot_cid confirms a container, since a clone elsewhere on this
    machine may use the same project name."""
    if not shutil.which("docker"):
        return 127, "docker is not installed"
    try:
        r = subprocess.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + SLOT0_PROJECT,
                            "--filter", "label=com.docker.compose.service=" + JUDGE_PULL_SERVICE,
                            "--filter", "label=com.docker.compose.oneoff=True"], capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            return r.returncode, "the judge model pull could not be looked for: " + docker_error(r.stdout + r.stderr)
        kit, mine = {KIT, os.path.realpath(KIT)}, []
        for cid in r.stdout.split():
            w = subprocess.run(["docker", "inspect", "--format",
                                '{{index .Config.Labels "com.docker.compose.project.working_dir"}}', cid],
                               capture_output=True, text=True, timeout=30)
            if w.returncode == 0 and w.stdout.strip() in kit:
                mine.append(cid)
        if not mine:
            return 0, ""
        d = subprocess.run(["docker", "rm", "-f", *mine], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"the judge model pull could not be removed: {type(e).__name__}"
    if d.returncode != 0:
        return d.returncode, "the judge model pull could not be removed: " + docker_error(d.stdout + d.stderr)
    return 0, f"the judge model pull was stopped ({len(mine)} container(s) of {JUDGE_PULL_SERVICE} removed)"


def containers(slot=0) -> tuple:
    """(rows, None) when Docker answered, (None, why) when it did not. A failure is never an empty list:
    that reads as "no miner container" to a user who simply cannot reach Docker yet."""
    rc, out = compose("ps", "-a", "--format", "json", slot=slot)
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


def miner_exec(*cmd, timeout=60, slot=0) -> tuple[int, str]:
    return compose("exec", "-T", "miner", *cmd, timeout=timeout, slot=slot)


def miner_identity(slot=0) -> dict:
    """The keyring of the miner container: its address and its on-chain identifier (the key name)."""
    # Read inside the container by the miner's own keyring module (modea/keyring.py): it picks the backend
    # from the state of the volume, hands an encrypted keyring its passphrase on stdin, and refuses the
    # empty list dendrad answers when that passphrase is wrong. The keyring directory is the container's
    # own setting (DENDRA_KEYRING_DIR in the kit's compose file), never restated here.
    rc, out = _for(miner_exec, slot, "python3", "-m", "modea.keyring", "list")
    if rc != 0:
        # A session that Docker refuses has a miner it cannot see, not a miner that is not running.
        if out.startswith(NO_DOCKER_ACCESS):
            return {"error": NO_DOCKER_ACCESS}
        said = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("keyring: ")]
        return {"keyring_error": said[-1][len("keyring: "):][:400]} if said else {}
    try:
        keys = json.loads(out[out.find("["):])
    except ValueError:
        return {}
    keys = [k for k in keys if MINER_ID.match(k.get("name", ""))]
    if not keys:
        return {}
    pref = [k for k in keys if k.get("name", "").startswith("dm1")] or keys
    return {"miner_id": pref[0]["name"], "address": pref[0].get("address", "")}


# --- the network ---
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


def _node_host(env: dict) -> str:
    """The host of the node the miner reads (DENDRA_NODE of its .env), "" when the kit names none."""
    node = env.get("DENDRA_NODE", "")
    return node.split("://", 1)[-1].rsplit(":", 1)[0] if node else ""


def node_is_here(env: dict) -> bool:
    """True when the node the miner reads runs on this machine: it is named by a container-side name (see rest_base),
    or by none. A host with a dot, or an IPv6 literal, is a node elsewhere."""
    host = _node_host(env)
    return host in ("host.docker.internal", "") or ("." not in host and ":" not in host)


def rest_base(env: dict) -> str:
    """REST of the node the miner reads, as seen from this host. A miner on its own node names it by a
    container-side name: the node's alias on the dendra-chain network (`dendra-node`, or the node kit's
    project name), or host.docker.internal in a kit from an older join.sh. Neither resolves on the host,
    where the node kit publishes its REST on 127.0.0.1. A name without a dot is such an alias, never a
    public endpoint, and is read as this machine; an IPv6 literal (colons) is not a name."""
    host = "127.0.0.1" if node_is_here(env) else _node_host(env)
    return f"http://{host}:1317"


# The command that takes this miner off the network (deploy/testnet-miner/exit-miner.sh). The screen
# shows it and runs nothing: leaving is irreversible, and the chain refuses it while the miner still holds
# an obligation (a held fee, a jury seat on an unresolved audit, an open dispute), for a time no button can
# promise. The script reads the miner's identity and stake, asks the chain what it would answer, and
# changes nothing without --yes. On a machine that runs one identity per card it names the slot -- always,
# slot 0 included: exit-miner.sh refuses --yes without --slot there.
def exit_command(slot=0, multi=False) -> str:
    base = f"bash {os.path.join(REPO, 'deploy', 'testnet-miner', 'exit-miner.sh')}"
    return f"{base} --slot {int(slot)}" if (slot or multi) else base


def exit_notes(env: dict, ident: dict, chain: dict) -> dict:
    """What the exit card says, which is not the same in owner mode (deploy/join.sh --owner): there the
    chain returns the stake to the owner, `--yes` only prepares the delete-miner the owner signs, and a
    restart prints the owner's create-miner instead of registering. Owner mode is read as exit-miner.sh
    reads it from the chain (the key that registered the miner) and, when the registration was not
    read, from the kit's DENDRA_MINER_OWNER setting."""
    machine = str(ident.get("address") or "").lower()
    creator = str((chain or {}).get("creator") or "")
    owner = creator or str(env.get("DENDRA_MINER_OWNER", "")).strip()
    obligation = ("The chain refuses while the miner still holds an obligation (a fee held on its work, a seat on "
                  "an audit not yet resolved, an open dispute): that is a reading to repeat later, not a failure.")
    if owner and owner.lower() != machine:
        return {"owner": owner,
                "how": ("This screen does not leave the network for you. This miner is owned by " + owner + ": in a "
                        "terminal, this command shows what the chain would answer and changes nothing; --yes stops "
                        "the miner and prepares the delete-miner the owner signs, and --yes --signed <file> sends "
                        "the signed one:"),
                "after": ("Leaving deregisters the miner, and the chain returns its remaining stake to its owner, "
                          + owner + ", never to this machine's key. " + obligation + " Starting the miner again does "
                          "not register it: it prints the create-miner its owner signs.")}
    return {"owner": "",
            "how": ("This screen does not leave the network for you. In a terminal, this command shows what the "
                    "chain would answer and changes nothing; add --yes to leave:"),
            "after": ("Leaving stops the miner, deregisters it, and the chain returns its remaining stake to this "
                      "miner's address. " + obligation + " Starting the miner again registers it again, with a new "
                      "stake.")}


# --- the miner's self-test ---
# The hourly job (join.sh::install_miner_health_cron) writes miner-health.last.json next to the kit on
# every run; this screen reads it, with its age, and runs a quick check on demand. It never decides
# health itself: the verdict is the script's, and a missing or malformed file is shown as such.
HEALTH_SCRIPT = os.path.join(KIT, "miner_health.sh")
HEALTH_LAST = os.path.join(KIT, "miner-health.last.json")
# The schedule's period is read from the document, not kept here: miner_health.sh writes it
# (schedule_period_s, from its SCHEDULE_PERIOD_S, which its bench compares with the crontab line join.sh
# installs). A last run older than two periods is a schedule that did not fire (cron not running, a WSL
# distribution that was shut down), and the screen says so. A document without the period -- written by
# an older script -- is not judged stale or fresh: the screen says it cannot tell.
# "Run now" runs the script with --quick (no model probe, no relay check, no write): what is left is a few
# reads and C1's wait for a new block. Bounded, so a hung exec never holds the screen, and one at a time.
HEALTH_RUN_TIMEOUT_S = 240
# The self-test's own bound inside the container, passed down with --deadline: half of this screen's wait,
# so the run ends there before the screen gives up on it. Killing the process group below reaches the
# script and the docker client only -- never what runs in the container.
HEALTH_RUN_DEADLINE_S = HEALTH_RUN_TIMEOUT_S // 2
_HEALTH_RUN = threading.Lock()
_HEALTH_STATES = ("ok", "ko", "unmeasured")


def _health_from(doc) -> dict:
    """The screen's view of one miner_health.sh document. A document without its time, its exit code or
    its counts is 'unread' -- never a green, and never a red guessed from the parts that are there."""
    if not isinstance(doc, dict):
        return {"state": "unread", "why": "the check's result is not a JSON object"}
    ep, rc, s = doc.get("generated_epoch"), doc.get("rc"), doc.get("summary")
    if type(ep) is not int or type(rc) is not int or rc not in (0, 1, 2) or not isinstance(s, dict) \
            or not all(type(s.get(k)) is int for k in ("ok", "ko", "unmeasured", "checks")):
        return {"state": "unread", "why": "the check's result carries no readable time, exit code or counts"}
    checks = [c for c in (doc.get("host") or []) if isinstance(c, dict)]
    st = doc.get("selftest")
    if isinstance(st, dict):
        checks += [c for c in (st.get("checks") or []) if isinstance(c, dict)]
    rows = [{k: str(c.get(k) or "") for k in ("id", "name", "measured", "reason", "fix")} for c in checks]
    for row, c in zip(rows, checks):
        row["state"] = c.get("state") if c.get("state") in _HEALTH_STATES else "unmeasured"
    # Advice, apart from the checks: the script's own (A4, the judge role off) then the self-test's (A1-A3).
    # It carries no state and never changes the verdict.
    advice = []
    for adv in (doc.get("advice"), st.get("advice") if isinstance(st, dict) else None):
        advice += [{k: str(a.get(k) or "") for k in ("id", "name", "measured", "fix")}
                   for a in (adv if isinstance(adv, list) else []) if isinstance(a, dict)]
    age = max(0, int(time.time()) - ep)
    period = doc.get("schedule_period_s")
    period = period if type(period) is int and period > 0 else None
    return {"state": "read", "rc": rc, "generated_at": str(doc.get("generated_at") or ""), "generated_epoch": ep,
            "age_s": age, "stale": (age > 2 * period) if period else None, "period_s": period,
            "quick": doc.get("quick") is True,
            "summary": {k: s[k] for k in ("ok", "ko", "unmeasured", "checks")}, "checks": rows,
            "advice": advice, "judge_role": _judge_role_from(doc.get("judge_role")),
            "note": str(doc.get("note") or "")}


# The judge role, as the container reported it. miner_health.sh asks the miner container (miner_selftest.py
# --judge-role, the reader publish-capacity.sh puts in the signed capacity report) and carries the word in its
# document. One of four words, or None: any other word, or a document without the field (written by an older
# script, or a run that did not reach the container), is treated as unread, never as "off" or "active".
JUDGE_ROLE_WORDS = ("active", "mute", "off", "unknown")


def _judge_role_from(jr) -> dict | None:
    if not isinstance(jr, dict) or jr.get("word") not in JUDGE_ROLE_WORDS:
        return None
    return {"word": jr["word"], "why": str(jr.get("why") or "")[:300]}


def role_view(env: dict, health: dict) -> dict:
    """The Role line of the overview. The chain draws declared judges into juries, and only an identity whose
    judge runs actually votes. So the line comes from the role read in the container by the last check (`word`,
    with that check's age), and the kit's setting (DENDRA_MINER_JUDGE) only says what was requested: a judge that
    was requested but not read is never shown as a juror that votes."""
    jr = health.get("judge_role") if isinstance(health, dict) and health.get("state") == "read" else None
    return {"word": jr["word"] if isinstance(jr, dict) else None, "why": jr["why"] if isinstance(jr, dict) else "",
            "age_s": health.get("age_s") if isinstance(jr, dict) else None,
            "requested": env.get("DENDRA_MINER_JUDGE") == "1"}


# The update instruction of this machine, as deploy/join.sh recorded it in the kit's .env (persist_kit_update):
# how this machine updates its kit, and the exact re-run of join.sh with its options. Read, never built here; a
# value with a character join.sh does not write is not shown (None), and None is shown as "not recorded".
_KIT_LINE = re.compile(r"[A-Za-z0-9 :/._~%?=+@,;&()-]+")


def update_view(env: dict) -> dict:
    out = {}
    for key, name in (("DENDRA_KIT_UPDATE", "instruction"), ("DENDRA_KIT_RERUN", "rerun")):
        v = env.get(key, "")
        out[name] = v if _KIT_LINE.fullmatch(v) else None
    return out


# The kit against what the network publishes, as the miner's own daemon read it. Once an
# hour the daemon writes `kit_behind` (true, false, or null when it has not compared the two numbers) and
# `kit_latest` (the published number, as text) into its heartbeat file. They are read here, inside the
# container, by key. Three answers, never two: true raises the banner, false raises nothing, and null -- a first
# hour, an unreadable network-info, a daemon older than the field, a heartbeat that could not be read -- is shown
# as "version check not read", never as up to date.
_NL = chr(10)
_KIT_READER = _NL.join([
    "import json, os",
    "p = os.environ.get('DENDRA_STATUS_FILE') or '/tmp/dendra-miner-status.json'",
    "try:",
    "    d = json.load(open(p, encoding='utf-8'))",
    "except Exception:",
    "    raise SystemExit(3)",
    "b = d.get('kit_behind', None) if isinstance(d, dict) else None",
    "w = 'true' if b is True else ('false' if b is False else 'null')",
    "print(w, ''.join(c for c in str(d.get('kit_latest') or '') if c.isalnum() or c in '._-')[:32])",
    # The floor the daemon read in the same file (miner.py::kit_status): this kit's number, KIT_MIN and
    # KIT_DEADLINE, then whether the network's file was read at all (1, 0, or - for a daemon older than the field).
    "dd = d if isinstance(d, dict) else {}",
    "fl = lambda v: ''.join(c for c in str('' if v is None else v) if c.isalnum() or c in ':._-')[:32] or '-'",
    "rd = '-' if 'kit_read_at' not in dd else ('0' if dd.get('kit_read_at') is None else '1')",
    "print('floor', fl(dd.get('kit_version')), fl(dd.get('kit_min')), fl(dd.get('kit_deadline')), rd)",
    # The daemon's own reason for a comparison it did not make (kit_why), so the screen does not guess one.
    "wy = ''.join(c for c in str(dd.get('kit_why') or '') if c.isalnum() or c in ' :/._-()' + chr(39))[:200]",
    "print('why', wy)",
])
_KIT_ANSWER = re.compile(r"(true|false|null)(?: ([A-Za-z0-9._-]{1,32}))?")
_KIT_FLOOR_ANSWER = re.compile(r"floor ([A-Za-z0-9:._-]{1,32}) ([A-Za-z0-9:._-]{1,32}) ([A-Za-z0-9:._-]{1,32}) ([01-])")
_KIT_WHY_ANSWER = re.compile(r"why(?: ([A-Za-z0-9 :/._()'-]{1,200}))?")
KIT_NOT_READ = "version check not read"


def _floor_from(out: str, why: str) -> dict:
    """The kit floor line of the reader's answer: {"mine", "min", "deadline"} as text or None, and "read" True,
    False or None (the daemon read the network's file / has not yet / is older than the field)."""
    found = [m for m in (_KIT_FLOOR_ANSWER.fullmatch(ln.strip()) for ln in out.splitlines()) if m]
    if not found:
        return {"mine": None, "min": None, "deadline": None, "read": None, "why": why}
    mine, kmin, deadline, rd = found[-1].groups()
    none = lambda v: None if v == "-" else v
    return {"mine": none(mine), "min": none(kmin), "deadline": none(deadline),
            "read": {"1": True, "0": False, "-": None}[rd], "why": ""}


def kit_reading(slot=0) -> tuple:
    """(kit, floor) from one read of the daemon's heartbeat. `kit` is what kit_view returns. `floor` is the
    network's kit floor as the same daemon read it (miner.py::kit_status): None for every value that was
    not read, never a number."""
    rc, out = _for(miner_exec, slot, "python3", "-I", "-c", _KIT_READER, timeout=30)
    if rc != 0:
        why = KIT_NOT_READ + ": the miner's heartbeat could not be read"
        return {"behind": None, "latest": None, "why": why}, _floor_from("", why)
    found = [m for m in (_KIT_ANSWER.fullmatch(ln.strip()) for ln in out.splitlines()) if m]
    if not found:
        why = KIT_NOT_READ + ": the heartbeat gave no readable answer"
        return {"behind": None, "latest": None, "why": why}, _floor_from("", why)
    floor = _floor_from(out, KIT_NOT_READ + ": the heartbeat carries no kit floor")
    word, latest = found[-1].group(1), found[-1].group(2)
    behind = {"true": True, "false": False, "null": None}[word]
    if behind is None:
        said = [m.group(1) for m in (_KIT_WHY_ANSWER.fullmatch(ln.strip()) for ln in out.splitlines()) if m and m.group(1)]
        return {"behind": None, "latest": latest,
                "why": KIT_NOT_READ + ": " + (said[-1] if said else
                                              "the daemon has not compared its kit with the network's yet")}, floor
    return {"behind": behind, "latest": latest, "why": ""}, floor


def kit_view(slot=0) -> dict:
    """{'behind': True | False | None, 'latest': str | None, 'why': str}. None is 'not read' and nothing else:
    the container does not answer, the heartbeat is missing or has no such field, or the answer is not one of
    the three words."""
    return kit_reading(slot)[0]


# The judge's memory, as the daemon's memory watch wrote it into its heartbeat (key judge_memory) and as the juror's
# ledger counted it (counters.abstentions for the memory guard, counters.last_load). Read inside the container, by key,
# and handed over as one JSON object: the block is printed as it is, and this module reads it again field by field.
# Three answers about the block, never two: `read`; `absent` (the heartbeat was read and carries none: the judge is not
# requested in this kit); `unread` (the container does not answer, the heartbeat is no object, the block is no object,
# or it is missing while the judge is requested -- a daemon older than the memory watch, or one that has not written
# it yet). Nothing here is ever shown as a healthy memory by default.
_JUDGE_READER = _NL.join([
    "import json, os, time",
    "def load(p):",
    "    try:",
    "        with open(p, encoding='utf-8') as f:",
    "            d = json.load(f)",
    "    except Exception:",
    "        return None",
    "    return d if isinstance(d, dict) else None",
    "hb = load(os.environ.get('DENDRA_STATUS_FILE') or '/tmp/dendra-miner-status.json')",
    "js = load(os.environ.get('DENDRA_JUDGE_STATE_FILE') or '/tmp/dendra-judge-state.json')",
    "if hb is None:",
    "    raise SystemExit(3)",
    "c = js.get('counters') if isinstance(js, dict) else None",
    "print(json.dumps({'now': int(time.time()), 'has_block': 'judge_memory' in hb, 'block': hb.get('judge_memory'),",
    "                  'declaration': hb.get('judge_declaration'),",
    "                  'abstentions': c.get('abstentions') if isinstance(c, dict) else None,",
    "                  'since': c.get('since') if isinstance(c, dict) else None,",
    "                  'last_load': c.get('last_load') if isinstance(c, dict) else None}))",
])
# The stage the juror names when its memory check refuses a load (judge_worker.py::RETRY_STAGES), as the key of its
# abstentions; a ledger that is an object and names no such stage counted none (the ledger writes only what happened).
MEMORY_GUARD_STAGE = "memory-guard"


def _whole(v):
    return v if type(v) is int and v >= 0 else None


# The judge's seat with the chain, as the daemon's declaration loop wrote it into the same heartbeat (key
# judge_declaration, miner.py::JudgeSeat): what it last read of the chain about this identity's judge. The chain
# stops drawing a judge seated on judge_missed_draws_max concluded audits without an answer (keeper/judge.go::
# eligibleAsJudgeAt); the parameter reads 0 as the compiled value, so it is 3 when the chain posts none.
JUDGE_MISSED_DRAWS_MAX_COMPILED = 3


def judge_seat_from(doc, requested: bool):
    """The page's view of the seat in one answer of `_JUDGE_READER`, without the chain's limit (see `judge_seat_limit`):
    None when the judge is not requested or the answer is unusable; else {"state": "declared" | "not_declared" |
    "unread", "why", "declared_height", "last_verdict_height", "missed_draws", "age_s"}. A count that is not a whole
    number is None (not read), never 0."""
    if not requested or not isinstance(doc, dict) or _whole(doc.get("now")) is None:
        return None
    blk = doc.get("declaration")
    if not isinstance(blk, dict):
        return {"state": "unread", "why": "the daemon's heartbeat carries no judge_declaration (an older daemon, or one that has "
                                           "not written it yet)"}
    st = blk.get("state")
    if st not in ("declared", "not_declared"):
        why = blk.get("why") if isinstance(blk.get("why"), str) and blk.get("why") else "the daemon's last state is " + str(st)
        return {"state": "unread", "why": " ".join(why.split())[:160]}
    at = _whole(blk.get("at"))
    age = max(0, doc["now"] - at) if at else None
    if st == "not_declared":
        return {"state": "not_declared", "why": "", "age_s": age}
    read = blk.get("read") if isinstance(blk.get("read"), dict) else {}
    return {"state": "declared", "why": "", "age_s": age, "declared_height": _whole(read.get("declared_height")),
            "last_verdict_height": _whole(read.get("last_verdict_height")), "missed_draws": _whole(read.get("missed_draws"))}


def judge_seat_limit(seat, chain: dict):
    """The seat of `judge_seat_from` with the chain's limit: `missed_max` (None when the chain's params were not read)
    and `excluded` (True at or above it, None when that is not known)."""
    if not isinstance(seat, dict):
        return seat
    out = dict(seat)
    limit = chain.get("judge_missed_draws_max") if isinstance(chain, dict) else None
    out["missed_max"] = limit if type(limit) is int and limit > 0 else None
    missed = seat.get("missed_draws")
    out["excluded"] = (missed >= out["missed_max"]) if seat.get("state") == "declared" and missed is not None and out["missed_max"] else None
    return out


def judge_memory_from(doc, requested: bool) -> dict:
    """The page's view of one answer of `_JUDGE_READER`: {"state": "read" | "absent" | "unread", "why": str, ...}."""
    if not isinstance(doc, dict) or _whole(doc.get("now")) is None:
        return {"state": "unread", "why": "judge memory not read: the heartbeat gave no readable answer"}
    if doc.get("has_block") is not True:
        if requested:
            return {"state": "unread", "why": "judge memory not read: the judge is requested and the daemon's heartbeat carries no "
                                              "judge_memory (a daemon older than the memory watch, or one that has not written it yet)"}
        return {"state": "absent", "why": "the judge is not requested in this kit, so no memory watch runs"}
    jm = doc.get("block")
    if not isinstance(jm, dict):
        return {"state": "unread", "why": "judge memory not read: judge_memory in the heartbeat is not an object"}
    at = _whole(jm.get("at"))
    gate_why = jm.get("gate_why")
    ab = doc.get("abstentions")
    # zero is a reading only from a ledger that was read: an object that names no memory-guard stage counted none
    abstained = None if not isinstance(ab, dict) else (_whole(ab.get(MEMORY_GUARD_STAGE)) if MEMORY_GUARD_STAGE in ab else 0)
    ll = doc.get("last_load")
    load_s = ll.get("load_s") if isinstance(ll, dict) else None
    pause = _whole(jm.get("pause_until"))
    footprint_source = jm.get("footprint_source")
    return {"state": "read", "why": "",
            "mode": jm.get("mode") if jm.get("mode") in ("resident", "on-demand") else None,
            "ceiling": jm.get("ceiling") if isinstance(jm.get("ceiling"), str) and jm.get("ceiling") else None,
            "gate": jm.get("gate") if jm.get("gate") in ("open", "closed") else None,
            "gate_why": " ".join(gate_why.split())[:200] if isinstance(gate_why, str) else "",
            "loaded": jm.get("resident") if type(jm.get("resident")) is bool else None,
            "age_s": max(0, doc["now"] - at) if at else None,
            "footprint_mb": _whole(jm.get("footprint_mb")),
            "footprint_source": footprint_source if isinstance(footprint_source, str) else None,
            "mem_available_mb": _whole(jm.get("mem_available_mb")), "mem_total_mb": _whole(jm.get("mem_total_mb")),
            "swap_used_mb": _whole(jm.get("swap_used_mb")), "swap_total_mb": _whole(jm.get("swap_total_mb")),
            "unloads": _whole(jm.get("unloads")), "oom_seen": _whole(jm.get("oom_seen")),
            "paused": bool(pause and pause > doc["now"]),
            "memory_abstentions": abstained, "counters_since": _whole(doc.get("since")),
            "last_load_s": load_s if isinstance(load_s, (int, float)) and not isinstance(load_s, bool) and load_s >= 0 else None}


def judge_memory_view(env: dict, slot=0) -> dict:
    """The judge's memory for one identity (see `_JUDGE_READER`)."""
    rc, out = _for(miner_exec, slot, "python3", "-I", "-c", _JUDGE_READER, timeout=30)
    if rc != 0:
        return {"state": "unread", "why": "judge memory not read: the miner's heartbeat could not be read", "seat": None}
    doc = None
    for line in reversed(out.splitlines()):
        if line.strip().startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                doc = None
            break
    requested = env.get("DENDRA_MINER_JUDGE") == "1"
    return dict(judge_memory_from(doc, requested), seat=judge_seat_from(doc, requested))


def health_view(slot=0, sdir=None) -> dict:
    """The last scheduled run of one identity: slot 0's next to the kit, a slot k's in its own directory
    (miner_health.sh writes one per slot). Three answers: 'never' (no run recorded -- nothing says the miner
    is fine), 'unread' (a file that is not the document), 'read' (with its age)."""
    last = HEALTH_LAST if not slot else os.path.join(sdir or os.path.join(KIT, "gpu", str(int(slot))), "miner-health.last.json")
    try:
        with open(last, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"state": "never", "why": "no check has run on this machine yet: the hourly job writes "
                                          f"{last} (deploy/join.sh schedules it), or run one now"}
    except OSError as e:
        return {"state": "unread", "why": f"{last} could not be read ({type(e).__name__})"}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"state": "unread", "why": f"{last} is not JSON"}
    return _health_from(doc)


def run_health(slot=None) -> tuple:
    """(HTTP status, body) of a quick run, one at a time and bounded in time. `slot` names one identity of a
    machine that runs several (--slot): without it the script would check every slot and give the machine's
    verdict, which is not one identity's."""
    if not os.path.isfile(HEALTH_SCRIPT):
        return 404, {"error": f"this kit has no {os.path.basename(HEALTH_SCRIPT)}: update the clone and re-run deploy/join.sh"}
    if not _HEALTH_RUN.acquire(blocking=False):
        return 409, {"error": "a check is already running"}
    try:
        # Its own process group, so the bound below reaches the script and the docker client it started.
        # It does not reach the self-test inside the container -- a killed `docker exec` client leaves its
        # process running there -- which is why the self-test is given its own, shorter, deadline.
        argv = ["bash", HEALTH_SCRIPT, "--json", "--quick", "--deadline", str(HEALTH_RUN_DEADLINE_S)]
        if slot is not None:
            argv += ["--slot", str(int(slot))]
        rc, out, err, how = run_child(argv, HEALTH_RUN_TIMEOUT_S)
        if how == "error":
            return 502, {"error": f"the check could not be started: {err}"}
        if how == "timeout":
            return 504, {"error": f"the check did not finish within {HEALTH_RUN_TIMEOUT_S} s"}
        if how == "interrupted":
            return 409, {"error": f"the check was {INTERRUPTED_TEXT}", "interrupted": True}
        r = subprocess.CompletedProcess(argv, rc, out, err)
        doc = None
        for line in reversed(r.stdout.splitlines()):
            if line.startswith("{"):
                try:
                    doc = json.loads(line)
                except ValueError:
                    doc = None
                break
        if doc is None:
            return 502, {"rc": r.returncode, "error": "the check printed no result", "text": (r.stdout + r.stderr)[-2000:]}
        view = _health_from(doc)
        if view["state"] == "read":
            _QUICK[int(slot or 0)] = doc
        return 200, {"rc": r.returncode, "health": view}
    finally:
        _HEALTH_RUN.release()


# The last quick check of each identity, kept in this process: the state in plain words reads the newest report,
# and a quick check run from the page is newer than the hourly one until the next hourly run. The document is
# kept, not its view, so that its age is counted again at every reading.
_QUICK = {}


def newest_report(sched: dict, slot=0):
    """The newest readable health report of an identity, with where it came from, or None when neither the
    hourly one (`sched`, a health_view) nor a quick check run here is readable.

    A quick check does not run every check (miner_selftest.py::run_checks leaves out the model probe and the
    relay check under --quick). A check the newer report does not carry stays as the hourly report read it:
    `carried`, with that report's age in `carried_age_s`. So a quick check never wipes a problem it did not look at."""
    q = _QUICK.get(int(slot or 0))
    qv = _health_from(q) if q is not None else None
    hourly = sched.get("state") == "read"
    if qv is not None and qv["state"] == "read" and (not hourly or qv["generated_epoch"] >= sched["generated_epoch"]):
        seen = {c["id"] for c in qv["checks"]}
        carried = [c for c in sched["checks"] if c["id"] not in seen] if hourly else []
        return dict(qv, source="quick", carried=carried, carried_age_s=sched["age_s"] if carried else None)
    return dict(sched, source="scheduled", carried=[], carried_age_s=None) if hourly else None


def slots_summary(rows: list, multi: bool) -> list:
    """Every identity of this machine, as the overview lists it: what the kit's files say (project, state,
    card, requested identity), the last scheduled self-test of each (a file read, no Docker call) and the
    command that leaves with it. Whether its 24-word phrase still waits on the machine is the self-test's own
    advice (A2): an identity whose check never ran says nothing about it, which is not "no phrase"."""
    out = []
    for r in rows:
        h = health_view(r["slot"], r.get("dir"))
        adv = (h.get("advice") or []) if h.get("state") == "read" else None
        out.append({"slot": r["slot"], "project": r["project"], "state": r["state"], "uuid": r["uuid"],
                    "miner_id": r["miner_id"],
                    "health": {k: h.get(k) for k in ("state", "rc", "age_s", "stale", "why")},
                    "phrase_to_confirm": None if adv is None else any(a.get("id") == "A2" for a in adv),
                    "exit_command": exit_command(r["slot"], multi)})
    return out


def chain_view(env: dict, ident: dict) -> dict:
    """Balance, registration, subsidy: each value is a reading or None (None = not read), never a 0
    that stands for a failure.

    The subsidy shown is what has accrued, not what can be claimed now. `subsidy_claimable_udndr` is the cap the chain
    computes (demand x work_gate_bps / 10000, minus what was claimed: msg_server_claim_subsidy.go::ClaimSubsidy),
    and the chain pays it from the emission work pool: while that pool holds nothing, the claim is refused
    ("emission work pool is empty") until a later emission epoch funds it. The screen says so, and never that
    the miner will collect it now."""
    base, addr, mid = rest_base(env), ident.get("address", ""), ident.get("miner_id", "")
    out = {"rest": base, "balance_udndr": None, "registered": None, "stake_udndr": None,
           "subsidy_claimable_udndr": None, "creator": None, "judge_missed_draws_max": None}
    if addr:
        b = _get_json(f"{base}/cosmos/bank/v1beta1/balances/{addr}")
        if isinstance(b, dict):
            out["balance_udndr"] = sum(int(c.get("amount", 0)) for c in b.get("balances", []) if c.get("denom") == "udndr")
    if mid:
        m = _get_json(f"{base}/dendra/jobs/v1/miner/{urllib.parse.quote(mid)}")
        p = _get_json(f"{base}/dendra/jobs/v1/params")
        if isinstance(p, dict) and isinstance(p.get("params"), dict):
            # proto3 omits a parameter at its zero, and the chain reads that zero as the compiled limit
            raw = p["params"].get("judge_missed_draws_max", 0)
            try:
                out["judge_missed_draws_max"] = int(raw or 0) or JUDGE_MISSED_DRAWS_MAX_COMPILED
            except (TypeError, ValueError):
                out["judge_missed_draws_max"] = None
        if isinstance(m, dict):
            rec = m.get("miner")
            out["registered"] = isinstance(rec, dict)
            if isinstance(rec, dict):
                out["stake_udndr"] = int(rec.get("stake", 0) or 0)
                # The key that registered the miner: the one the chain returns the stake to on exit.
                out["creator"] = str(rec.get("creator") or "")
                if isinstance(p, dict) and isinstance(p.get("params"), dict):
                    cap = int(rec.get("demand", 0) or 0) * int(p["params"].get("work_gate_bps", 0) or 0) // 10000
                    out["subsidy_claimable_udndr"] = max(0, cap - int(rec.get("subsidy_claimed", 0) or 0))
    return out


# The figures of a ranking row (`final_season_calc.to_json`) this screen shows. Each is required, as
# `final_season_payout.load_day` reads them back: a missing field, or one that is not an integer, is a ranking
# not read, never a 0.
RANKED_FIGURES = ("presence", "verified_requests", "verdicts", "gross_udndr", "payable_udndr", "paid_udndr")


def formula_of(presence: int, requests: int, verdicts: int, rules: dict | None = None,
               judge_active: int = 0, probation: bool = False) -> dict | None:
    """The published formula (`final_season_calc.gross_of`) rerun on one day's published facts, part by part,
    so the screen can say what each fact earned. Every part is the one formula applied to part of the
    facts, never a rate restated here. `rules` is the rule set the day was ranked under (None: the rules in
    force). `judge_active` and `probation` are the two facts only a day under the judge presence set
    (`final_season_rules.pays_judge_presence`) publishes; a set without them refuses a row that carries one
    (`gross_of`). None without the services tree."""
    if gross_of is None:
        return None
    work = gross_of(Identity("me", verified_requests=int(requests)), rules)
    juror = gross_of(Identity("me", verdicts=int(verdicts)), rules)
    judge = gross_of(Identity("me", judge_active=int(judge_active)), rules)
    plain = gross_of(Identity("me", presence=int(presence), verified_requests=int(requests),
                              verdicts=int(verdicts)), rules)
    gross = gross_of(Identity("me", presence=int(presence), verified_requests=int(requests),
                              verdicts=int(verdicts), judge_active=int(judge_active), probation=probation), rules)
    # Presence is paid only on a day with a verified request: its part is what the whole formula adds to
    # the other two, which is 0 on a day without work whatever the windows proven. The probation share is
    # applied last, on the whole gross (`gross_of`): its part is what it takes off the sum of the others,
    # 0 or less.
    return {"work_udndr": work, "juror_udndr": juror, "presence_udndr": plain - work - juror,
            "judge_udndr": judge, "probation_udndr": gross - plain - judge, "gross_udndr": gross}


def _judge_facts(row: dict, required: bool) -> tuple:
    """(judge_active, probation) of a ranking row. A set that pays the judge presence publishes both on every
    row (`final_season_calc.to_json`), so there they are required and typed: 0 or 1 (not a bool), true or false
    (not a 0 or a 1). Under a set that does not pay it a row carries neither, and the two are 0 and false by
    that set's definition; a row that carries one anyway is read as it says, and `gross_of` then refuses it."""
    if not required and "judge_active" not in row and "probation" not in row:
        return 0, False
    ja, pr = row.get("judge_active"), row.get("probation")
    if type(ja) is not int or ja not in (0, 1) or type(pr) is not bool:
        raise ValueError("judge_active is not 0 or 1, or probation is not true or false")
    return ja, pr


def rule_set_named(fp, *floors):
    """The rule set whose fingerprint is `fp`, or None. The sets the rules module holds are looked up first
    (`final_season_rules.rule_set_of`). The set of decision 24 exists only once its kit floor is fixed
    (`final_season_rules.kit_rules`), and this application never fixes it: the service publishes the floor with the
    set (`status.rules.kit_min`, `ranking.inputs.kit_floor.kit_min`), so each floor given here is tried as a
    candidate, and the set is taken only when its fingerprint is `fp`. A floor that is not a kit number, or that
    names another set, finds nothing: None, never the nearest set. Nothing in the rules module is changed."""
    if rule_set_of is None:
        return None
    found = rule_set_of(fp)[1]
    if found is not None:
        return found
    for floor in floors:
        if type(floor) is int and floor >= 1:
            cand = kit_rules(floor)
            if fingerprint(cand) == fp:
                return cand
    return None


def _published_floor(doc):
    """`inputs.kit_floor.kit_min` of a ranking, or None when the document does not carry it (a day ranked under an
    earlier set has no such field, and a floor that is not an integer is not one)."""
    inputs = doc.get("inputs") if isinstance(doc, dict) else None
    kf = inputs.get("kit_floor") if isinstance(inputs, dict) else None
    km = kf.get("kit_min") if isinstance(kf, dict) else None
    return km if type(km) is int else None


def _status_floor(st):
    """`rules.kit_min` of the service's status, which carries it on a day under decision 24 only; None otherwise."""
    rules = st.get("rules") if isinstance(st, dict) else None
    km = rules.get("kit_min") if isinstance(rules, dict) else None
    return km if type(km) is int else None


def ranked_row(day: int, ranking, mid: str, status_floor=None) -> dict:
    """This identity's line of the published ranking of `day`; ValueError when the document is not the
    ranking of that day. An identity absent from a ranking had no fact counted that day — the ranking lists
    every identity with a verified request, a verdict or a proven window (`final_season_facts.day_identities`)
    — so it reads as zeros, shown as not listed."""
    if not isinstance(ranking, dict) or type(ranking.get("day")) is not int or ranking["day"] != day \
            or not isinstance(ranking.get("ranking"), list):
        raise ValueError(f"not the ranking of day {day}")
    rows = [x for x in ranking["ranking"] if isinstance(x, dict) and x.get("miner_id") == mid]
    if len(rows) > 1:
        raise ValueError(f"{mid} is ranked twice on day {day}")
    # The formula is rerun under the rule set the ranking was computed with, found by the fingerprint the
    # ranking carries among every set the season published (`final_season_rules.rule_set_of`, RULE_HISTORY): a
    # day ranked under an earlier set is rerun under that set, never under the rules in force since. A
    # fingerprint of no published set, or none at all, is not rerun: another copy of the rules gives another
    # figure, and the screen would call a difference of versions a difference of figures.
    fp = ranking.get("rules_fingerprint")
    # The set of decision 24 is rebuilt from the floor the day publishes, or else from the one the status gives
    # (`status_floor`, None when it gives none), and taken only when its fingerprint is the day's (`rule_set_named`).
    rules = rule_set_named(fp, _published_floor(ranking), status_floor)
    if rows:
        if not all(type(rows[0].get(k)) is int for k in RANKED_FIGURES):   # not a float, a string, a bool
            raise ValueError(f"day {day}: a figure of {mid} is not an integer")
        out = {k: rows[0][k] for k in RANKED_FIGURES}
        out.update(day=day, listed=True, reason=str(rows[0].get("reason") or ""))
        # The judge presence and the probation, published by the sets that pay them only: required on a row
        # of such a set, zero by definition on a row of an earlier one, and not read under a set not known.
        if rules is None:
            out.update(judge_active=None, probation=None)
        else:
            out["judge_active"], out["probation"] = _judge_facts(rows[0], pays_judge_presence(rules))
    else:
        out = dict.fromkeys(RANKED_FIGURES, 0)
        out.update(day=day, listed=False, reason="", judge_active=0, probation=False)
    out["rules_fingerprint"] = fp if isinstance(fp, str) else None
    out["rules_differ"] = rule_set_of is not None and rules is None
    out["formula"] = (formula_of(out["presence"], out["verified_requests"], out["verdicts"], rules,
                                 out["judge_active"], out["probation"])
                      if rules is not None else None)
    return out


def rules_in_force(prog: dict):
    """The rule set of the programme's current day, for the formula shown on screen: the one whose fingerprint
    the service's status names, found among every set the season published (`final_season_rules.rule_set_of`).
    None when the status was not read, names no day yet, or names a set this application does not know: the
    screen then shows no formula, never the rates of another set."""
    st = (prog or {}).get("status")
    if rule_set_of is None or not isinstance(st, dict):
        return None
    return rule_set_named(st.get("rules_fingerprint"), _status_floor(st))


def programme_view(env: dict, mid: str) -> dict:
    """The identity's Final Testnet Season figures as the programme published them: its line of the latest
    ranked day (`ranking/day-NNN.json`) and what all the ranked days paid it. A day is ranked only once it
    is final, past its last block (`final_season_rank.finality_blocks`), so the current day has no figure:
    nothing here is estimated or read live. Each value is a reading or None (not read): a failed read is
    never shown as 0 paid or 0 requests, which are real values an identity can have."""
    base = env.get("DENDRA_FINAL_SEASON_URL", "").rstrip("/")
    out = {"enabled": bool(base), "url": base, "status": None, "ranked_days": None,
           "season_paid_udndr": None, "latest": None, "earnings": None, "days": None}
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
    paid, d, latest, ranked = 0, 0, None, []
    # Rankings are published in day order, each once its day is final: the first day "not published" ends
    # the walk, and no day from the current one on can be ranked yet, so none is asked for.
    while d < day:
        code, body = _fetch(f"{base}/ranking/day-{d:03d}.json")
        if _not_published(code, body):
            break                              # the days from here on are not ranked yet
        if code != 200:
            return out                         # a ranking not read: the total would be a partial sum
        try:
            latest = ranked_row(d, json.loads(body), mid, _status_floor(st))
        except (ValueError, TypeError, AttributeError, KeyError):
            return out
        ranked.append(latest)
        paid += latest["paid_udndr"]
        d += 1
    out.update(season_paid_udndr=paid, ranked_days=d, latest=latest, earnings=earnings_view(env, st, ranked),
               days=pay_days_view(ranked))
    return out


# --- what the identity earned by role, and where that leads to the season's end ---
# The projection is the average of the identity's last ranked days, so it is only as good as its window: it names
# the window and the end it counts to, and it promises nothing (the programme's caps and pro rata can cut it).
PROJECTION_DAYS = 7
# The block interval is measured over the chain's last `day_blocks` blocks, and kept this long: the page reads
# the overview every few seconds and one measure is one request to the node.
BLOCK_TTL_S = 300
_BLOCKS: dict = {}
BLOCK_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:[.][0-9]{1,9})?Z")
EARNED = ("work_udndr", "juror_udndr", "presence_udndr", "judge_udndr", "probation_udndr", "gross_udndr")


def _int(v) -> bool:
    return type(v) is int


def roles_view(ranked: list) -> dict | None:
    """What the ranked days earned, by role, each part being the published formula rerun on the day's published
    facts (`formula_of`) under the set the day was ranked under. A day counts only when that rerun gives the gross
    the ranking states: another figure is a difference of versions or of rules, and a sum over it would be neither.
    `days` of `of` ranked days are summed; None for the sums when none is, never 0. `payable_udndr` and
    `paid_udndr` are the ranking's own, over the same days: the gross becomes payable when an address, the kit
    and the registration allow it, and paid after the day's pro rata (`final_season_calc.compute_day`)."""
    if not ranked:
        return None
    split = [r for r in ranked if isinstance(r.get("formula"), dict) and r["formula"]["gross_udndr"] == r["gross_udndr"]]
    out = {"days": len(split), "of": len(ranked)}
    for k in EARNED:
        out[k] = sum(r["formula"][k] for r in split) if split else None
    out["payable_udndr"] = sum(r["payable_udndr"] for r in split) if split else None
    out["paid_udndr"] = sum(r["paid_udndr"] for r in split) if split else None
    return out


def _header_epoch(doc):
    """Whole UTC seconds of the header time in a node's block answer (`/cosmos/base/tendermint/v1beta1/blocks/H`);
    None when it is not there or not a time."""
    try:
        t = doc["block"]["header"]["time"]
    except (KeyError, TypeError):
        return None
    m = BLOCK_TIME.fullmatch(t) if isinstance(t, str) else None
    return None if m is None else calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))


def block_seconds(env: dict, st: dict, rules: dict) -> tuple:
    """(seconds per block, why not): the chain's block interval, measured between the status's latest block and the
    block `day_blocks` earlier, which the miner's node serves. The interval emerges from the network and varies, so
    it is measured when it is used and never assumed; unread, it is None with the reason."""
    h, t, span = st.get("height"), st.get("latest_block_time"), rules.get("day_blocks")
    if not (_int(h) and _int(t) and _int(span)) or span < 1:
        return None, "the status does not give the latest block's height and time"
    if h - span < 1:
        return None, "the chain has not yet run one season day of blocks"
    base = rest_base(env)
    got = _BLOCKS.get(base)
    if got and time.time() - got[0] < BLOCK_TTL_S:
        return got[1], ""
    before = _header_epoch(_get_json(f"{base}/cosmos/base/tendermint/v1beta1/blocks/{h - span}"))
    if before is None or t <= before:
        return None, "the node did not answer with the time of an earlier block"
    secs = (t - before) / span
    _BLOCKS[base] = (time.time(), secs)
    return secs, ""


def blocks_left(env: dict, st: dict, rules: dict, ranked_days: int) -> dict:
    """The blocks of the season that are in no ranked day yet, to the season's last block, read from the status:
    `end_height` once the chain has passed the end (a reading), else counted from the chain's clock, the end being
    the rule's instant (`final_season_rules.end_epoch`) and the interval the one measured (`block_seconds`). The
    last day is cut where the season ends, so a part-day counts for its blocks. {"blocks": None, "why": ...} when
    it cannot be counted."""
    start, db, ended = st.get("start_height"), rules.get("day_blocks"), st.get("ended")
    if not (_int(start) and _int(db)) or db < 1:
        return {"blocks": None, "why": "the status does not give the season's first block"}
    first = start + ranked_days * db
    if ended is True:
        eh = st.get("end_height")
        if not _int(eh):
            return {"blocks": None, "why": "the season is over but its last block is not found yet"}
        return {"blocks": max(0, eh - first + 1), "how": "last_block", "end_height": eh}
    if ended is not False:
        return {"blocks": None, "why": "whether the season is over is not read"}
    secs, why = block_seconds(env, st, rules)
    if secs is None:
        return {"blocks": None, "why": why}
    left = end_epoch(rules) - st["latest_block_time"]
    if left <= 0:
        return {"blocks": None, "why": "the chain's clock is past the end but the status says the season runs"}
    return {"blocks": max(0, st["height"] + int(left / secs) - first + 1), "how": "clock",
            "block_seconds": round(secs, 3), "span_blocks": rules["day_blocks"]}


def projection_view(env: dict, st: dict, ranked: list) -> dict:
    """What the identity would still be assigned to the season's end, if its last ranked days went on. The
    hypothesis is part of the answer: the average per day of the last `PROJECTION_DAYS` ranked days that were
    ranked under the rules in force today (`status.rules_fingerprint`), a day on probation left out (it earned a
    share of a normal day); a ranked day where the identity is not listed counts as the 0 it is. Over the
    blocks the season has left (`blocks_left`), a part-day for its share. {"available": False, "why": ...}
    when there is nothing to average, and the per-day figures without a total when the end cannot be counted."""
    rules = rules_in_force({"status": st})
    if rules is None:
        return {"available": False, "why": "the rules of the current day are not known to this application"}
    fp = st.get("rules_fingerprint")
    window = [r for r in ranked if r["rules_fingerprint"] == fp and r["probation"] is not True][-PROJECTION_DAYS:]
    if not window:
        return {"available": False, "why": "no ranked day yet under the rules in force today (a probation day does not count)"}
    if not all(isinstance(r["formula"], dict) and r["formula"]["gross_udndr"] == r["gross_udndr"] for r in window):
        return {"available": False, "why": "a ranked day's gross differs from the published formula rerun on its facts"}
    n = len(window)
    per_day = {k: sum(r["formula"][k] for r in window) // n for k in EARNED}
    per_day["payable_udndr"] = sum(r["payable_udndr"] for r in window) // n
    per_day["paid_udndr"] = sum(r["paid_udndr"] for r in window) // n
    days = sorted(r["day"] for r in window)
    out = {"available": True, "window_days": n, "first_day": days[0], "last_day": days[-1],
           "per_day": per_day, "end": blocks_left(env, st, rules, len(ranked)), "to_end": None}
    if out["end"]["blocks"] is not None:
        out["end"]["days"] = round(out["end"]["blocks"] / rules["day_blocks"], 2)
        out["to_end"] = {k: v * out["end"]["blocks"] // rules["day_blocks"] for k, v in per_day.items()}
    return out


def earnings_view(env: dict, st: dict, ranked: list) -> dict:
    """The identity's earnings by role over the ranked days, and the projection to the season's end."""
    return {"roles": roles_view(ranked), "projection": projection_view(env, st, ranked) if ranked else
            {"available": False, "why": "no day is ranked yet"}}


# --- recovery phrase and settings ---
def settings() -> dict:
    try:
        with open(os.path.join(CONFIG_DIR, "app.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def private_config_dir() -> str:
    """CONFIG_DIR, created 0700, and narrowed to 0700 when an older version left it open to others: it holds the
    settings (the payout address), the machine tests and the kit's action lock, which only this user reads."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    if stat.S_IMODE(os.stat(CONFIG_DIR).st_mode) & 0o077:
        os.chmod(CONFIG_DIR, 0o700)
    return CONFIG_DIR


def write_private_json(path: str, doc) -> None:
    """`doc` written to `path` in CONFIG_DIR atomically, readable by this user only from its first byte: the
    temporary file is created 0600, never chmod-ed after the content is in it."""
    private_config_dir()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)               # a temporary file left by an older version keeps its own mode
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def save_settings(s: dict) -> None:
    write_private_json(os.path.join(CONFIG_DIR, "app.json"), s)


def recovery_phrase(slot=0) -> dict:
    """The kept phrase, opened inside the miner container by its keyring module: with an encrypted keyring
    the file is sealed under the keyring's passphrase, which never leaves the container either."""
    rc, out = _for(miner_exec, slot, "python3", "-m", "modea.keyring", "recovery", RECOVERY_IN_CONTAINER, timeout=30)
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


def keys_status(slot=0) -> dict:
    """What the overview needs at every refresh, read inside the container in one call
    (`python3 -m modea.keyring status /data/keys`): the keyring's backend, the key files sealed or in
    clear, whose recovery phrase is kept -- its address, never its words, which leave the container only
    when they are to be shown (`recovery_phrase`) -- and the payout declaration the programme accepted.
    {} when it was not read."""
    rc, out = _for(miner_exec, slot, "python3", "-m", "modea.keyring", "status", KEYDIR_IN_CONTAINER, timeout=30)
    if rc != 0:
        return {}
    try:
        d = json.loads(out[out.find("{"):])
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def recovery_head(status: dict | None = None) -> dict:
    """Whose phrase is kept, without the phrase. {"present": False} also when nothing was read."""
    st = keys_status() if status is None else status
    r = st.get("recovery") if isinstance(st, dict) else None
    if not isinstance(r, dict):
        return {"present": False}
    try:
        n = int(r.get("words") or 0)
    except (TypeError, ValueError):
        return {"present": False}
    if r.get("present") is None:
        return {"present": False, "unreadable": str(r.get("error") or "the phrase file does not open")}
    return {"present": n in (12, 24), "address": str(r.get("address") or "")}


def phrase_fact(st) -> bool | None:
    """Whether a recovery phrase waits on this machine, for the state in plain words: True, False, or None when the
    keys status did not say. Only the keyring's two answers count (modea/keyring.py::recovery_head): a phrase of 12
    or 24 words, or none -- no file, or a file that holds no phrase. A file that exists and did not open
    ({"present": None}), a status without the field, and a word count that is not a number are None, never False."""
    r = st.get("recovery") if isinstance(st, dict) else None
    if not isinstance(r, dict):
        return None
    present = r.get("present")
    if present is not True and present is not False:
        return None
    if "words" in r and type(r["words"]) is not int:
        return None
    if present is True:
        return True if r.get("words") in (12, 24) else None
    return False


def forget_recovery(address: str, slot=0) -> tuple:
    """(removed, why). Removes exactly the kept phrase file, inside the container, and only when the phrase
    it holds is still the one of `address` -- the one whose words were just typed back."""
    # --yes: the module removes nothing without it. It is passed here only, after the three words matched.
    rc, out = _for(miner_exec, slot, "python3", "-m", "modea.keyring", "forget-recovery", RECOVERY_IN_CONTAINER,
                   "--address", address, "--yes", timeout=30)
    try:
        d = json.loads(out[out.find("{"):]) if "{" in out else {}
    except ValueError:
        d = {}
    if rc == 0 and d.get("removed") is True:
        return True, ""
    return False, str(d.get("why") or out.strip()[-300:] or f"exit {rc}")


def keys_view(st: dict) -> dict:
    """How the miner's keys are kept, as the container read it: {"at_rest": "encrypted" | "clear" | None,
    "backend", "error", "clear_files"}. None is a value not read, never "clear" and never "encrypted"."""
    kr = st.get("keyring") if isinstance(st, dict) else None
    if not isinstance(kr, dict):
        return {"at_rest": None, "backend": None, "error": "", "clear_files": []}
    files = st.get("files") if isinstance(st.get("files"), dict) else {}
    clear = sorted(n for n, v in files.items() if v == "clear" and not n.endswith(".json"))
    backend = kr.get("backend")
    at_rest = None
    if backend == "test":
        at_rest = "clear"
    elif backend == "file":
        at_rest = "clear" if clear else "encrypted"
    return {"at_rest": at_rest, "backend": backend, "error": str(kr.get("error") or ""), "clear_files": clear}


def payout_view(env: dict, st: dict, ident: dict) -> dict:
    """Where the Final Testnet Season pays this identity: the declaration the programme accepted (recorded
    in the miner's volume), the kit's setting (DENDRA_PAYOUT_ADDRESS), and whether the season pays this
    machine's own key. `to_machine_key` is None when the record was not read."""
    mid = str(ident.get("miner_id") or "")
    machine = str(ident.get("address") or "")
    rec = st.get("payout") if isinstance(st, dict) and "payout" in st else "unread"
    # `owner`: the miner's owner in owner mode (deploy/join.sh --owner), whose key alone may declare.
    out = {"declared": "", "setting": env.get("DENDRA_PAYOUT_ADDRESS", ""), "machine_address": machine,
           "to_machine_key": None, "owner": env.get("DENDRA_MINER_OWNER", "").strip()}
    if rec == "unread" or not mid:
        return out
    if isinstance(rec, dict) and rec.get("miner_id") == mid and rec.get("address"):
        out["declared"] = str(rec["address"])
        out["to_machine_key"] = (out["declared"].lower() == machine.lower()) if machine else None
    else:
        out["to_machine_key"] = True
    return out


def phrase_confirmed(rp: dict, s: dict) -> bool:
    """True only when the phrase on the miner's volume is the one whose words were typed back: the
    confirmation is kept per address, so a new key's phrase is shown again. A phrase without an address
    is never taken as confirmed (two empty strings are not the same key), and the old per-user flag
    `recovery_confirmed` is ignored: it hid every later key's phrase."""
    addr = str(rp.get("address") or "")
    many = s.get("recovery_confirmed_addresses")
    return bool(rp.get("present")) and bool(addr) and (
        addr == s.get("recovery_confirmed_address") or (isinstance(many, list) and addr in [str(x) for x in many]))


def record_confirmation(s: dict, address: str) -> dict:
    """The settings with `address` recorded as confirmed: the last one (`recovery_confirmed_address`, which
    every older reader reads) and, one identity per card, the list of all of them -- N identities are N
    phrases, and confirming one must not forget another (encrypt-keys.sh reads both, modea/keyring.py::plan)."""
    s.pop("recovery_confirmed", None)
    s["recovery_confirmed_address"] = address
    many = s.get("recovery_confirmed_addresses")
    many = [str(x) for x in many] if isinstance(many, list) else []
    if address not in many:
        many.append(address)
    s["recovery_confirmed_addresses"] = many
    return s


def challenge_positions(n_words: int) -> list:
    """Three positions the user must type back before the phrase is marked as written down."""
    rnd = secrets.SystemRandom()
    return sorted(rnd.sample(range(1, n_words + 1), 3))


# --- the two switches: Judge and GPU mining ---
# Judge: the kit's own switch, deploy/testnet-miner/judge.sh on|off, run as it is. This screen decides nothing
# about the seat: it greys the switch, with the reason, where judge.sh would refuse.
# GPU mining: with the card off the machine keeps answering the requests the chain assigns it, on the kit's CPU
# instance with the judge model, as a machine without a card does (deploy/join.sh::write_cpu_judge_override).
# serve_guard.py accepts a model on the CPU only in the judge role, so the card goes off with the judge on, and the
# judge is not switched off while the card is off.
JUDGE_SH = os.path.join(KIT, "judge.sh")
# The probe that judge.sh reads (its probe variable): the deploy directory of this kit's clone.
HW_PROBE = os.path.normpath(os.path.join(KIT, "..", "..", "deploy", "hw_probe.sh"))
# The override deploy/join.sh writes for the judge role on the CPU, and the words judge.sh::CPU_ROLE looks for in it.
CPU_OVERRIDE = os.path.join(KIT, "docker-compose.override.yml")
CPU_ROLE_MARK = "judge role on the CPU"
# The kit's CPU instance, as judge.sh::judge_on names it in DENDRA_JUDGE_ENDPOINT.
CPU_ENGINE = "http://ollama-cpu:11434"
# The lines of the kit's .env that turn the card off, read by deploy/testnet-miner/docker-compose.yml: the miner's
# engine and served model, and the keep-alive and slots of ollama-cpu. Those two values are the ones
# deploy/join.sh::write_cpu_judge_override gives the CPU instance when it serves the miner next to the judge: one
# hour keeps the judge model loaded between requests, and a second slot keeps a request from waiting behind a verdict.
GPU_OFF_KEYS = ("DENDRA_SERVE_ENDPOINT", "DENDRA_SERVE_MODEL_ID", "DENDRA_CPU_KEEP_ALIVE", "DENDRA_CPU_NUM_PARALLEL")
CPU_KEEP_ALIVE, CPU_NUM_PARALLEL = "1h", "2"
# A model tag as the kit's .env may carry it: compose expands `$` in that file, so nothing else is written there.
_ENV_VALUE = re.compile(r"[A-Za-z0-9._:/@-]{1,200}")
# The first seat pulls the judge model (its size is in the kit's compose file) at the speed of this machine's
# connection, so the bound is generous; one switch runs at a time.
SWITCH_TIMEOUT_S = 3 * 3600
COMPOSE_STEP_TIMEOUT_S = 600
# The probe reads the RAM, the cards and the judge's allow-list; a machine's RAM does not move between two
# refreshes of this screen, so its answer is kept for PROBE_TTL_S.
PROBE_TIMEOUT_S = 60
PROBE_TTL_S = 300
_SWITCH_RUN = threading.Lock()
_PROBE_LOCK = threading.Lock()
_PROBE = {"at": 0.0, "view": None}


def _probe(*args) -> tuple:
    """(exit code, stdout) of deploy/hw_probe.sh, or (None, why) when it did not run."""
    rc, out, _err, how = run_child(["bash", HW_PROBE, *args], PROBE_TIMEOUT_S)
    if how:
        return None, how
    return rc, out


def _can_judge_read() -> dict:
    if not os.path.isfile(HW_PROBE):
        return {"answer": "unknown", "why": f"the hardware probe is not in this clone ({HW_PROBE}): whether this "
                                            "machine can judge is not known, and an unread gate seats no judge"}
    rc, out = _probe("--json", "--no-docker")
    doc = None
    if rc == 0:
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), "")
        try:
            doc = json.loads(line) if line else None
        except ValueError:
            doc = None
    if not isinstance(doc, dict) or "can_judge" not in doc:
        return {"answer": "unknown", "why": "deploy/hw_probe.sh --json gave no readable can_judge (exit "
                                            f"{rc}): whether this machine can judge is not known, and an unread gate "
                                            "seats no judge. bash deploy/hw_probe.sh shows what it reads"}
    cj = doc["can_judge"]
    if cj is True:
        return {"answer": "true", "why": ""}
    if cj is not False:
        return {"answer": "unknown", "why": "deploy/hw_probe.sh could not read this machine's RAM (can_judge "
                                            "null): an unread gate seats no judge"}
    frc, fout = _probe("--judge-floor-mb")
    floor = fout.strip() if frc == 0 and fout.strip().isdigit() else "?"
    ram = str(doc.get("ram_mb")) if type(doc.get("ram_mb")) is int else "?"
    why = (f"this machine cannot judge (deploy/hw_probe.sh: can_judge false, {ram} MB of system RAM read). The "
           f"judge runs the network's judge model on the CPU: it needs MOE_CPU_MIN_RAM_MB of system RAM, {floor} MB "
           "(bash deploy/hw_probe.sh --judge-floor-mb), and that model on the probe's allow-list")
    if os.environ.get("WSL_DISTRO_NAME"):
        why += ". Under WSL 2 this is the RAM of the WSL VM: [wsl2] memory= in the .wslconfig of your Windows profile raises it"
    return {"answer": "false", "why": why}


def can_judge_view() -> dict:
    """{'answer': 'true' | 'false' | 'unknown', 'why': str}: deploy/hw_probe.sh's own decision (`can_judge` in its
    JSON, the variable --can-judge prints), with its RAM reading and its floor for the reason. Three answers, never
    two: a probe that did not run, an unread RAM (null) or a JSON without the field is 'unknown', which greys a
    switch as 'false' does and says why. The floor is read from the probe, never kept here."""
    with _PROBE_LOCK:
        if _PROBE["view"] is not None and time.monotonic() - _PROBE["at"] < PROBE_TTL_S:
            return _PROBE["view"]
    view = _can_judge_read()
    with _PROBE_LOCK:
        _PROBE.update(at=time.monotonic(), view=view)
    return view


def judge_setting(env: dict):
    """DENDRA_MINER_JUDGE of the kit's .env: True, False (0, or no line: the zero value), None for any other value."""
    v = env.get("DENDRA_MINER_JUDGE", "")
    return True if v == "1" else (False if v in ("", "0") else None)


def cpu_role():
    """True when the kit runs the judge role on the CPU (no usable card), False when its override says otherwise or
    there is none, None when the override cannot be read."""
    try:
        with open(CPU_OVERRIDE, encoding="utf-8") as f:
            return CPU_ROLE_MARK in f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return None


def gpu_setting(env: dict) -> tuple:
    """(True, "") the card serves (no serving line: the kit's own engine), (False, "") the card is off (the lines
    this screen writes), (None, why) for anything else: a served model on the CPU that is not the judge model is one
    serve_guard.py refuses."""
    ep, m = env.get("DENDRA_SERVE_ENDPOINT", ""), env.get("DENDRA_SERVE_MODEL_ID", "")
    if not ep and not m:
        return True, ""
    jm = env.get("DENDRA_JUDGE_MODEL_ID", "")
    if ep == CPU_ENGINE and m and m == jm:
        return False, ""
    return None, (f"the kit's .env names a serving engine or model this screen does not write (DENDRA_SERVE_ENDPOINT "
                  f"'{ep[:80]}', DENDRA_SERVE_MODEL_ID '{m[:80]}', judge model '{jm[:80]}'): serve_guard.py accepts "
                  "a model on the CPU only when it is the judge model. Turning GPU mining on removes these lines")


def switches_view(env: dict, multi: bool) -> dict:
    """Both switches of slot 0: {"judge" | "gpu": {"on": True | False | None, "enabled", "why"}, "can_judge"}.
    `enabled` says whether the switch can be pressed now, and `why` says why not."""
    judge = judge_setting(env)
    gpu, gwhy = gpu_setting(env)
    out = {"judge": {"on": judge, "enabled": False, "why": ""}, "gpu": {"on": gpu, "enabled": False, "why": gwhy},
           "can_judge": None}
    if multi:
        out["judge"]["why"] = ("this machine runs one identity per card (deploy/join.sh --gpus): its judge is switched "
                               "by re-running deploy/join.sh with --judge or --no-judge, and judge.sh refuses here")
        if gpu is True:
            out["gpu"]["why"] = ("this machine runs one identity per card (deploy/join.sh --gpus): each identity is "
                                 "bound to its card, and stopping that identity frees it")
            return out
        # Slot 0 kept the serving lines when join.sh --gpus kept its .env: its card idles while it answers on the
        # CPU. Turning the card on is the one way offered here; turning it off is not.
        out["judge"]["why"] += ". GPU mining is off on slot 0: turn it on first"
        out["gpu"].update(enabled=True, why=(
            "slot 0 answers on the CPU and its card is idle (its .env still names DENDRA_SERVE_ENDPOINT, kept when "
            "deploy/join.sh --gpus kept that .env): turning GPU mining on gives slot 0 its card back. On a machine "
            "with one identity per card it cannot be turned off here" + (". " + gwhy if gwhy else "")))
        return out
    if not os.path.isfile(JUDGE_SH):
        out["judge"]["why"] = out["gpu"]["why"] = (f"this kit has no judge.sh ({JUDGE_SH}): update the clone and "
                                                   "re-run deploy/join.sh")
        return out
    role = cpu_role()
    if role is None:
        out["judge"]["why"] = out["gpu"]["why"] = (f"{CPU_OVERRIDE} could not be read: which engine this miner "
                                                   "uses is not known")
        return out
    if role:
        out["judge"].update(on=True, why="this machine has no usable card: judging on the CPU is its whole role "
                                         "(judge.sh), so it is not a switch")
        out["gpu"].update(on=False, why="this machine has no usable card: it already answers on the CPU with the "
                                        "judge model (deploy/join.sh wrote the judge role on the CPU)")
        return out
    cj = can_judge_view()
    out["can_judge"] = cj
    if judge is None:
        out["judge"]["why"] = (f"DENDRA_MINER_JUDGE in the kit's .env is '{env.get('DENDRA_MINER_JUDGE', '')[:40]}', "
                               "neither 0 nor 1: re-run deploy/join.sh")
    elif judge and gpu is not True:
        out["judge"]["why"] = ("GPU mining is off: on the CPU this machine answers with the judge model, which "
                               "serve_guard.py accepts only in the judge role. Turn GPU mining on first")
    elif judge or cj["answer"] == "true":
        out["judge"]["enabled"] = True
    else:
        out["judge"]["why"] = cj["why"]
    if gpu is not True:
        out["gpu"]["enabled"] = True
    elif cj["answer"] == "true":
        out["gpu"]["enabled"] = True
    else:
        out["gpu"]["why"] = ("with the card off this machine answers on the CPU with the judge model, so it has to "
                             "be able to judge: " + cj["why"])
    return out


def _run_bounded(argv, timeout: int, env=None) -> tuple:
    """(exit code, output) of a command in its own process group, killed whole past `timeout` or when Stop
    interrupts the action that runs it."""
    rc, out, err, how = run_child(argv, timeout, cwd=KIT, env=env, merge=True)
    if how == "error":
        return 1, err
    if how == "timeout":
        return 124, f"stopped after {timeout} s"
    if how == "interrupted":
        return INTERRUPTED_RC, (out + _NL + INTERRUPTED_TEXT).lstrip()
    return rc, out


def run_judge_sh(verb: str) -> tuple:
    return _run_bounded(["bash", JUDGE_SH, verb], SWITCH_TIMEOUT_S, env=dict(os.environ, DENDRA_MINER_KIT=KIT))


def _env_text() -> str:
    with open(os.path.join(KIT, ".env"), encoding="utf-8", newline="") as f:
        return f.read()


def _env_put(text: str) -> None:
    # Written in place: the file keeps its mode (0600, it carries the relay token) and its owner.
    with open(os.path.join(KIT, ".env"), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def env_edit(changes: dict) -> str:
    """Sets (value) or removes (None) lines of the kit's .env, in place, every other line kept as written: the
    edit judge.sh::env_set makes. Returns the text as it was, for a rollback."""
    before = _env_text()
    out, seen = [], set()
    for ln in before.splitlines(keepends=True):
        key = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else None
        if key in changes:
            if key not in seen and changes[key] is not None:
                body = ln.rstrip(chr(13) + chr(10))
                out.append(f"{key}={changes[key]}" + (ln[len(body):] or _NL))
            seen.add(key)
            continue
        out.append(ln)
    for k, v in changes.items():
        if v is not None and k not in seen:
            if out and not out[-1].endswith(_NL):
                out[-1] += _NL
            out.append(f"{k}={v}{_NL}")
    _env_put("".join(out))
    return before


def _switch_preflight(which: str, on: bool):
    """(None, view) when the switch can go to `on` now, ((status, body), None) otherwise."""
    rows, why = slots()
    if rows is None:
        return (502, {"error": why}), None
    multi = len(rows) > 1
    sw = switches_view(kit_env(), multi)[which]
    if multi and which == "gpu" and not on:
        return (409, {"error": "this machine runs one identity per card (deploy/join.sh --gpus): GPU mining can only "
                               "be turned on here"}), None
    if sw["on"] is on:
        return (409, {"error": f"{'the judge' if which == 'judge' else 'GPU mining'} is already {'on' if on else 'off'}"}), None
    if not sw["enabled"]:
        return (409, {"error": sw["why"]}), None
    return None, sw


def switch_judge(on: bool) -> tuple:
    err, _ = _switch_preflight("judge", on)
    if err:
        return err
    rc, out = run_judge_sh("on" if on else "off")
    return (200 if rc == 0 else 502), {"rc": rc, "text": out[-4000:]}


def _recreate(services, judge: bool) -> tuple:
    prof = ["--profile", "judge"] if judge else []
    return compose(*prof, "up", "-d", "--no-deps", "--force-recreate", *services, timeout=COMPOSE_STEP_TIMEOUT_S)


def _gpu_off() -> tuple:
    said = []
    if judge_setting(kit_env()) is not True:
        rc, out = run_judge_sh("on")
        said.append(out)
        if rc != 0:
            return 502, {"rc": rc, "error": "the judge could not be seated, so the card stays on (judge.sh on)",
                         "text": out[-4000:]}
    env = kit_env()
    model, embed = env.get("DENDRA_JUDGE_MODEL_ID", ""), env.get("DENDRA_EMBED_API_MODEL", "")
    seated = " The judge was seated on the way (judge.sh on) and stays on." if said else ""
    for name, v in (("DENDRA_JUDGE_MODEL_ID", model), ("DENDRA_EMBED_API_MODEL", embed)):
        if not _ENV_VALUE.fullmatch(v):
            return 409, {"rc": 2, "error": f"the kit's .env carries no usable {name} ('{v[:80]}'): the card stays on. "
                                           "Re-run deploy/join.sh." + seated}
    # The miner's answers are checked with the verification embedder, which it then asks of the CPU instance too.
    rc, out = compose("--profile", "judge", "up", "-d", "--no-deps", "ollama-cpu", timeout=COMPOSE_STEP_TIMEOUT_S)
    if rc == 0:
        rc, out = compose("--profile", "judge", "exec", "-T", "ollama-cpu", "ollama", "pull", embed,
                          timeout=SWITCH_TIMEOUT_S)
    if rc != 0:
        return 502, {"rc": rc, "error": f"the verification embedder {embed} could not be pulled into the CPU "
                                        "instance: the card stays on. " + docker_error(out) + seated, "text": out[-4000:]}
    before = env_edit({"DENDRA_SERVE_ENDPOINT": CPU_ENGINE, "DENDRA_SERVE_MODEL_ID": model,
                       "DENDRA_CPU_KEEP_ALIVE": CPU_KEEP_ALIVE, "DENDRA_CPU_NUM_PARALLEL": CPU_NUM_PARALLEL})
    rc, out = _recreate(["ollama-cpu", "miner"], True)
    if rc != 0:
        _env_put(before)
        rc2, out2 = _recreate(["ollama-cpu", "miner"], True)
        return 502, {"rc": rc, "error": "the miner did not start on the CPU instance; the kit's .env is back as it "
                                        "was" + ("" if rc2 == 0 else ", and the miner could not be recreated either: "
                                                 + docker_error(out2)) + "." + seated, "text": out[-4000:]}
    # The card's engine holds the mining model for its keep-alive: stopped, it frees the card now.
    rc, sout = compose("stop", "ollama", timeout=COMPOSE_STEP_TIMEOUT_S)
    note = "" if rc == 0 else "the card's engine (ollama) could not be stopped, so it may hold the card until its keep-alive ends: " + docker_error(sout)
    return 200, {"rc": 0, "text": (_NL.join(said) + _NL + out)[-4000:], "note": note}


def _gpu_on() -> tuple:
    judge = judge_setting(kit_env()) is True
    before = env_edit({k: None for k in GPU_OFF_KEYS})
    services = ["ollama-cpu", "miner"] if judge else ["miner"]
    rc, out = compose("up", "-d", "--no-deps", "ollama", timeout=COMPOSE_STEP_TIMEOUT_S)
    if rc == 0:
        rc, out = _recreate(services, judge)
    if rc != 0:
        _env_put(before)
        rc2, out2 = _recreate(services, judge)
        return 502, {"rc": rc, "error": "the miner did not start on the card; the kit's .env is back as it was"
                                        + ("" if rc2 == 0 else ", and the miner could not be recreated either: "
                                           + docker_error(out2)), "text": out[-4000:]}
    return 200, {"rc": 0, "text": out[-4000:], "note": ""}


def switch_gpu(on: bool) -> tuple:
    err, _ = _switch_preflight("gpu", on)
    if err:
        return err
    return _gpu_on() if on else _gpu_off()


# --- the state in plain words ---
# One table, STATUS_RULES, decides the state shown at the top of the page. Each rule carries its level, the test
# that reads the facts, the words that say it, the gesture, the page of the site that settles it, and `source`:
# the function that decides the thing the rule reads (a threshold is cited from there, never restated here). The
# state is the worst level among the rules that fire. Green is no rule firing, and it needs every fact to have been
# read: a fact that was not read is a rule of its own that fires orange, so a green never rests on a blank.
SITE = "https://dendranetwork.com"
LEVELS = ("green", "orange", "red")
LEVEL_WORDS = {"green": "All good", "orange": "Needs attention", "red": "Act now"}
KIT_NUMBER = re.compile(r"[0-9]{1,6}")
# The form network-info publishes KIT_DEADLINE in (faucet.py::KIT_DEADLINE_FORMAT).
KIT_DEADLINE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Checks that have a rule of their own below; the general "a check failed" rule leaves them to it.
CLAIMED_CHECKS = ("C2", "C8", "C11")


def kit_floor_state(fl, now) -> tuple:
    """(state, why) for the network's kit floor against this kit. "clear": this kit is at or above KIT_MIN; "none":
    the network publishes no floor; "soon": below it, before the deadline; "refused": below it, from the deadline
    on (`now >= deadline`, as the relay applies it: relay.py::kit_floor_now); "unread" for anything the
    miner did not read. An unread number is never read as a kit that passes. A kit with no number of its own, under
    a published floor, is as refused as one below it."""
    if not isinstance(fl, dict) or fl.get("read") is not True:
        why = (fl or {}).get("why") if isinstance(fl, dict) else ""
        if not why:
            why = ("the miner has not read the network's kit numbers yet; it reads them once an hour"
                   if isinstance(fl, dict) and fl.get("read") is False
                   else "this miner's heartbeat does not say whether it read the network's kit numbers")
        return "unread", why
    mine, kmin, dl = fl.get("mine"), fl.get("min"), fl.get("deadline")
    numbered = mine is not None and KIT_NUMBER.fullmatch(mine) is not None
    if kmin is None:
        # No floor, so nothing refuses this kit; but a kit that cannot say its own number is still not compared.
        return ("none", "the network publishes no kit floor") if numbered else ("unread", "this kit's own number was not read")
    if not KIT_NUMBER.fullmatch(kmin):
        return "unread", "the network's KIT_MIN is not a kit number"
    if numbered and int(mine) >= int(kmin):
        return "clear", ""
    # Below the floor, or no number at all: the relay refuses both from the deadline
    # (relay.py::verdict_kit_policy: "below {floor}, or none, is refused").
    try:
        ts = datetime.datetime.strptime(dl or "", KIT_DEADLINE_FORMAT).replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return "unread", "the network publishes a kit floor without a deadline this application can read"
    return ("refused" if now >= ts else "soon"), ""


def age_words(s) -> str:
    s = max(0, int(s))
    return f"{s} s" if s < 90 else (f"{round(s / 60)} min" if s < 5400 else f"{round(s / 3600)} h")


def _lines(f: dict) -> list:
    """Every check line the state reads: the newest report's own, then the ones it carries from the hourly report
    (newest_report: a check a quick run did not do)."""
    r = f["report"]
    return (r["checks"] + r["carried"]) if r else []


def _check_of(f: dict, cid: str):
    return next((c for c in _lines(f) if c.get("id") == cid), None)


def _kit_fields(f: dict) -> dict:
    fl = f["floor"] or {}
    dl = fl.get("deadline") or ""
    return {"mine": fl.get("mine") or "unknown", "min": fl.get("min"), "deadline": dl.replace("T", " ").replace("Z", " UTC")}


def _t_docker(f):
    return {"why": f["docker"].rstrip(".")} if isinstance(f["docker"], str) else None


def _t_stopped(f):
    # A miner whose state was not read is not a running one; when Docker did not answer, that is the docker rule's.
    if isinstance(f["docker"], str):
        return None
    return {"word": f["miner"]["word"]} if f["miner"]["running"] is not True else None


def _t_kit_refused(f):
    return _kit_fields(f) if kit_floor_state(f["floor"], f["now"])[0] == "refused" else None


def _t_node_behind(f):
    c = _check_of(f, "C2")
    return {"measured": c["measured"].rstrip("."), "fix": c["fix"]} if c and c["state"] == "ko" else None


def _t_judge_mute(f):
    c, role = _check_of(f, "C11"), f["role"]
    if (c and c["state"] == "ko") or (isinstance(role, dict) and role.get("word") == "mute"):
        why = (role or {}).get("why") or (c or {}).get("measured") or "the judge engine or its worker is down"
        return {"why": why.rstrip("."), "fix": (c or {}).get("fix") or
                "Restart the miner. If the judge stays mute, the judge section names the engine it needs."}
    return None


def _t_capacity_report(f):
    # C8: the signed capacity report, which the season's pay depends on (final_season_calc.kit_allows_pay).
    # Its fix names what the miner's own sender can do from its last round (miner_selftest.py::capacity_fix): this
    # line promises no report the sender will not send.
    c = _check_of(f, "C8")
    if not c or c["state"] != "ko":
        return None
    return {"measured": c["measured"].rstrip("."), "fix": c["fix"] or "Read the Health card: it gives the fix."}


def _t_check_failed(f):
    kos = [c for c in _lines(f) if c["state"] == "ko" and c["id"] not in CLAIMED_CHECKS]
    if not kos:
        return None
    c = kos[0]
    return {"id": c["id"], "name": c["name"], "measured": c["measured"].rstrip("."),
            "more": f" (and {len(kos) - 1} more)" if len(kos) > 1 else "",
            "fix": c["fix"] or "Read the Health card: it gives the fix."}


def _t_kit_soon(f):
    return _kit_fields(f) if kit_floor_state(f["floor"], f["now"])[0] == "soon" else None


def _t_kit_behind(f):
    if f["kit"]["behind"] is True and kit_floor_state(f["floor"], f["now"])[0] not in ("soon", "refused"):
        return {"latest": f["kit"]["latest"] or "?"}
    return None


def _t_report_old(f):
    s = f["schedule"]
    return {"age": age_words(s["age_s"])} if s["state"] == "read" and s["stale"] is True else None


def _t_no_report(f):
    # The hourly report is the one that rings an alarm when the miner is wrong while nobody looks: a quick check run
    # here does not stand in for it.
    s = f["schedule"]
    return {"why": s.get("why") or "the last report is not readable"} if s["state"] != "read" else None


def _t_not_measured(f):
    ids = [c["id"] for c in _lines(f) if c["state"] == "unmeasured"]
    return {"n": len(ids), "ids": ", ".join(ids)} if ids else None


def _t_report_numbers(f):
    # The lines are what the other rules read. The report's own numbers (miner_health.sh::finish: exit code 0 only
    # when something was measured and nothing failed) must agree with them: a report that counts a failure, or
    # nothing at all, while no line names one, is not a pass.
    r = f["report"]
    if r is None:
        return None
    s = r["summary"]
    sound = r["rc"] == 0 and s["ko"] == 0 and s["unmeasured"] == 0 and s["checks"] > 0
    if sound or any(c["state"] in ("ko", "unmeasured") for c in _lines(f)):
        return None
    return {"rc": r["rc"], "ko": s["ko"], "un": s["unmeasured"], "n": s["checks"]}


def _t_age_unjudged(f):
    s = f["schedule"]
    return {} if s["state"] == "read" and s["stale"] is None else None


def _t_phrase(f):
    return {} if f["phrase"] is True else None


def _t_phrase_unread(f):
    return {} if f["phrase"] is None else None


def _t_kit_unread(f):
    st, why = kit_floor_state(f["floor"], f["now"])
    if f["kit"]["behind"] is None or st == "unread":
        return {"why": (f["kit"]["why"] or why) if f["kit"]["behind"] is None else why}
    return None


# Order is the order of display inside a level. `needs_miner`: the fact comes from inside the miner's container, so
# a stopped miner or an unreachable Docker already explains it and the rule stays quiet.
STATUS_RULES = (
    {"id": "docker", "level": "red", "test": _t_docker, "source": "dendra_app.py::containers",
     "says": "{why}.",
     "do": "Start Docker. If you just ran the installer, log out and log back in, then reload this page.",
     "page": ("/miner/", "help")},
    {"id": "stopped", "level": "red", "test": _t_stopped, "source": "dendra_app.py::containers",
     "says": "The miner is not running ({word}).", "do": "Press Start in the Miner card.", "page": ("/miner/", "help")},
    {"id": "kit_refused", "level": "red", "test": _t_kit_refused, "needs_miner": True,
     "source": "relay.py::kit_floor_now",
     "says": "The network refuses this kit since {deadline} (kit {mine}, minimum {min}): the relay refuses its deposits and "
             "the season stops paying it.",
     "do": "Update the kit now: {update}", "page": ("/update/", "update")},
    {"id": "node_behind", "level": "red", "test": _t_node_behind, "source": "miner_selftest.py::c2_sync",
     "says": "The node is behind the network: {measured}.",
     "do": "Wait a few minutes, then press Run now in the Health card. {fix}", "page": ("/node/", "check")},
    {"id": "judge_mute", "level": "red", "test": _t_judge_mute, "source": "miner_selftest.py::judge_role",
     "says": "The judge is on but gives no verdict: {why}.", "do": "{fix}", "page": ("/miner/", "judge")},
    {"id": "capacity_report", "level": "red", "test": _t_capacity_report, "source": "miner_selftest.py::c8_capacity",
     "says": "The signed capacity report of this identity is not counted: {measured}. The season pays nothing to an "
             "identity without a signed capacity report.",
     "do": "{fix}", "page": ("/miner/", "check")},
    {"id": "check_failed", "level": "red", "test": _t_check_failed, "source": "miner_selftest.py::aggregate",
     "says": "A health check failed: {id} {name}: {measured}{more}.", "do": "{fix}", "page": ("/miner/", "check")},
    {"id": "kit_soon", "level": "orange", "test": _t_kit_soon, "needs_miner": True,
     "source": "relay.py::kit_floor_now",
     "says": "This kit stops being accepted on {deadline} (kit {mine}, minimum {min}): after that the relay refuses its "
             "deposits and the season stops paying it.",
     "do": "Update before then: {update}", "page": ("/update/", "deadline")},
    {"id": "kit_behind", "level": "orange", "test": _t_kit_behind, "needs_miner": True,
     "source": "miner.py::kit_status",
     "says": "A newer kit is published (kit {latest}).", "do": "Update: {update}", "page": ("/update/", "update")},
    {"id": "report_old", "level": "orange", "test": _t_report_old, "source": "dendra_app.py::_health_from",
     "says": "The hourly health check has not run for {age}.",
     "do": "Press Run now in the Health card. If the report stays old, the hourly job is not running: run deploy/join.sh again.",
     "page": ("/miner/", "check")},
    {"id": "no_report", "level": "orange", "test": _t_no_report, "source": "dendra_app.py::health_view",
     "says": "The hourly health report is missing: {why}", "do": "Press Run now in the Health card. If no report appears within an hour, the hourly job is not running: run deploy/join.sh again.",
     "page": ("/miner/", "check")},
    {"id": "not_measured", "level": "orange", "test": _t_not_measured, "source": "miner_selftest.py::aggregate",
     "says": "{n} health check(s) could not be measured: {ids}.",
     "do": "Press Run now, then read the Health card: each unmeasured check says why.", "page": ("/miner/", "check")},
    {"id": "report_numbers", "level": "orange", "test": _t_report_numbers, "source": "miner_health.sh::finish",
     "says": "The health report is not a pass and names no check that failed (exit code {rc}, {ko} failed, {un} not "
             "measured, {n} counted).",
     "do": "Press Run now in the Health card. If the report stays like this, run deploy/join.sh again.",
     "page": ("/miner/", "check")},
    {"id": "age_unjudged", "level": "orange", "test": _t_age_unjudged, "source": "dendra_app.py::_health_from",
     "says": "The health report does not say how often it should run, so its age is not judged.",
     "do": "Run deploy/join.sh again: it installs the hourly job with its period.", "page": ("/miner/", "check")},
    {"id": "phrase", "level": "orange", "test": _t_phrase, "needs_miner": True, "source": "dendra_app.py::recovery_head",
     "says": "Your 24 recovery words are still on this machine.",
     "do": "Write them on paper and confirm them in the recovery card at the top of this page: confirming removes them "
           "from this machine.", "in_app": "the recovery card"},
    {"id": "phrase_unread", "level": "orange", "test": _t_phrase_unread, "needs_miner": True,
     "source": "dendra_app.py::keys_status",
     "says": "Whether a recovery phrase is waiting on this machine was not read.",
     "do": "Reload this page. If it stays unread, press Run now in the Health card: C3 reads the keys.",
     "page": ("/miner/", "check")},
    {"id": "kit_unread", "level": "orange", "test": _t_kit_unread, "needs_miner": True,
     "source": "miner.py::kit_status",
     "says": "The kit version is not compared with the network's: {why}",
     "do": "Wait up to an hour: the miner compares its kit with the network's once an hour. If it stays unread, run "
           "deploy/join.sh again: it sets the address of the network file.", "page": ("/update/", "check")},
)


def _said(rule: dict, got: dict, update) -> dict:
    fields = dict(got, update=update or "follow the update page")
    page = rule.get("page")
    return {"id": rule["id"], "level": rule["level"], "says": rule["says"].format(**fields),
            "do": rule["do"].format(**fields).strip(), "in_app": rule.get("in_app", ""),
            "page": ({"url": SITE + page[0] + "#" + page[1], "label": "dendranetwork.com" + page[0] + "#" + page[1]}
                     if page else None),
            "rule": "dendra_app.py::STATUS_RULES", "source": rule["source"]}


def status_view(f: dict) -> dict:
    """The state in plain words from the facts of one identity (a missing key is a bug and raises, never a default):
    `docker` (None, or why Docker did not answer), `miner` ({"running": True | False | None, "word"}), `report`
    (the newest readable health report, or None), `schedule` (the hourly report's health_view), `role`, `kit`
    (kit_view), `floor` (kit_reading), `phrase` (True, False or None), `update` (the kit's recorded instruction),
    `now`. Returns {"level", "word", "headline", "do", "rules", "checked"}."""
    fired, quiet, update = [], False, f["update"]
    for r in STATUS_RULES:
        if r.get("needs_miner") and quiet:
            continue
        got = r["test"](f)
        if got is None:
            continue
        fired.append(_said(r, got, update))
        if r["id"] in ("docker", "stopped"):
            quiet = True
    fired.sort(key=lambda x: -LEVELS.index(x["level"]))     # a stable sort: the table's order stays within a level
    level = fired[0]["level"] if fired else "green"
    checked = []
    if not fired:
        r = f["report"]
        carried = (f" (the {', '.join(c['id'] for c in r['carried'])} result stands from the hourly report, "
                   f"{age_words(r['carried_age_s'])} old)") if r["carried"] else ""
        checked = ["Docker answers and the miner is running",
                   f"the last health report is {age_words(r['age_s'])} old and every check is ok" + carried,
                   "this kit matches the published kit" + ("" if kit_floor_state(f["floor"], f["now"])[0] == "clear" else
                                                         " (the network publishes no kit floor)"),
                   "no recovery phrase is waiting on this machine"]
    return {"level": level, "word": LEVEL_WORDS[level],
            "headline": fired[0]["says"] if fired else "Nothing needs your attention.",
            "do": fired[0]["do"] if fired else "Nothing to do.", "rules": fired, "checked": checked}


# --- "Test my machine" ---
# The speed of the miner's engine and the time of one verdict are measured inside the miner container by the calls
# the miner and the judge make: the miner's backend (modea/inference.py::OllamaBackend.generate, deterministic
# decoding, the container's own OLLAMA_ENDPOINT and OLLAMA_MODEL) and the judge's decision (judge_worker.py::
# decide_verdict, with the flags the judge process reads, on the judge's own endpoint and model). Nothing is posted
# to the chain, the relay or the programme. What is read back is a count of words and a number of seconds; the
# rate is computed here, once, and a part that did not run is "not measured" with its reason, never a figure.
MT_FILE = "machine-test.json"
MT_TOKENS = 200            # the cap on one generation (max_out); the same for the speed and for the verdict's references
MT_CALL_S = 300            # the bound on one call, which includes loading the model the first time
MT_DEADLINE_S = 1800       # the bound on the whole test inside the container (an alarm there; killing `docker exec` is not enough)
MT_PROMPT = "Write one short paragraph about how a river reaches the sea."
MT_QUESTION = "What is the capital of France?"
MT_ANSWER = "The capital of France is Paris."
MT_MARK = "DENDRA_MACHINE_TEST "
# The only exit stage of decide_verdict that is a full verdict on a valid answer: coherence, relevance, two
# references and the two same-fact readings (judge_worker.py::decide_verdict, stage "sc-valid").
MT_FULL_STAGE = "sc-valid"
_MT_RUN = threading.Lock()
MACHINE_TEST_SCRIPT = _NL.join([
    "import json, signal, sys, time",
    "arg = json.loads(sys.argv[1])",
    "out = {'throughput': None, 'verdict': None}",
    # Not an Exception: decide_verdict retries a generation that raised one, and the bound must stop the test.
    "class Late(BaseException):",
    "    pass",
    "def late(signum, frame):",
    "    raise Late()",
    "def why(e):",
    "    if isinstance(e, Late):",
    "        return 'the test ran past its bound'",
    "    if type(e).__name__ == 'Held':",
    "        return 'held by the memory guard, which refuses a load the machine has no room for: ' + str(e)[:200]",
    "    return type(e).__name__ + ': ' + str(e)[:200]",
    "signal.signal(signal.SIGALRM, late)",
    "signal.alarm(int(arg['deadline_s']))",
    "be, stopped = None, False",
    "try:",
    "    from modea.inference import get_backend",
    "    be = get_backend('ollama')",
    "    t = time.monotonic()",
    "    be.generate('ok', max_out=64, timeout_s=arg['call_s'])",
    "    warm = time.monotonic() - t",
    "    t = time.monotonic()",
    "    text = be.generate(arg['prompt'], max_out=arg['tokens'], timeout_s=arg['call_s'])",
    "    took = time.monotonic() - t",
    "    row = {'words': len(text.split()), 'seconds': took, 'warm_seconds': warm, 'model': str(be.model)}",
    "    try:",
    "        import urllib.request",
    "        ps = json.load(urllib.request.urlopen(be.endpoint.rstrip('/') + '/api/ps', timeout=10))",
    "        m = next((x for x in ps.get('models', []) if be.model in (x.get('name'), x.get('model'))), None)",
    "        if m:",
    "            row['size'], row['size_vram'] = m.get('size'), m.get('size_vram')",
    "    except Exception:",
    "        pass",
    "    out['throughput'] = row",
    "except (Exception, Late) as e:",
    "    stopped = isinstance(e, Late)",
    "    out['throughput'] = {'error': why(e)}",
    "if arg['judge']:",
    "    if stopped:",
    "        out['verdict'] = {'error': why(Late())}",
    "    else:",
    "        try:",
    "            import judge_worker as jw",
    "            from modea.judge import llm_coherent",
    "            if be is None:",
    "                from modea.inference import get_backend",
    "                be = get_backend('ollama')",
    "            model, source = jw.resolve_judge_model('')",
    "            t = time.monotonic()",
    "            llm_coherent('ok', model=model, timeout=arg['call_s'])",
    "            load = time.monotonic() - t",
    "            trace = {}",
    "            t = time.monotonic()",
    "            verdict = jw.decide_verdict(arg['answer'], arg['question'], be, model, twostage=jw.TWOSTAGE,",
    "                                        gen_retries=jw.GEN_RETRIES, gen_delay=jw.GEN_DELAY, relevance=jw.RELEVANCE,",
    "                                        selfconsist=jw.SELFCONSIST, sc_temperature=jw.SC_TEMPERATURE,",
    "                                        multiok=jw.MULTIOK, trace=trace, max_out=arg['tokens'])",
    "            took = time.monotonic() - t",
    "            out['verdict'] = {'seconds': took, 'load_seconds': load, 'stage': str(trace.get('stage', '')),",
    "                              'generations': trace.get('n_gen'), 'model': str(model), 'source': str(source),",
    "                              'read': verdict}",
    "        except (Exception, Late) as e:",
    "            out['verdict'] = {'error': why(e)}",
    "signal.alarm(0)",
    "print('" + MT_MARK + "' + json.dumps(out), flush=True)",
])


def _positive(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def rate_of(words, seconds):
    """Words per second, or None when either figure is not a measurement: no word is not a rate of 0."""
    if type(words) is not int or words < 1 or not _positive(seconds):
        return None
    return round(words / seconds, 2)


def placement_of(size, vram):
    """Where the engine holds the model, from its /api/ps reading: "card" (all of it in video memory), "cpu" (none
    of it), "split", or None when the two sizes were not read as sizes."""
    if type(size) is not int or type(vram) is not int or size < 1 or not 0 <= vram <= size:
        return None
    return "cpu" if vram == 0 else ("card" if vram == size else "split")


def _unmeasured(why: str, page=None) -> dict:
    out = {"state": "not measured", "why": why}
    if page:
        out["page"] = {"url": SITE + page[0] + "#" + page[1], "label": "dendranetwork.com" + page[0] + "#" + page[1]}
    return out


def _model_name(v):
    return v if isinstance(v, str) and _ENV_VALUE.fullmatch(v) else None


def speed_part(row) -> dict:
    if not isinstance(row, dict):
        return _unmeasured("the test returned no reading of the speed")
    if "error" in row:
        return _unmeasured("the engine did not answer the test: " + str(row["error"])[:300])
    rate = rate_of(row.get("words"), row.get("seconds"))
    if rate is None:
        return _unmeasured("the engine returned no word, or no time was measured: a speed needs both")
    warm = row.get("warm_seconds")
    return {"state": "measured", "words": row["words"], "seconds": round(row["seconds"], 3), "words_per_s": rate,
            "warm_seconds": round(warm, 3) if _positive(warm) else None, "model": _model_name(row.get("model")),
            "placement": placement_of(row.get("size"), row.get("size_vram"))}


def verdict_part(row, judge_on: bool) -> dict:
    if not judge_on:
        return _unmeasured("the judge is off on this machine: switch it on in the Switches card, then test again "
                           "(its model is downloaded the first time)", ("/miner/", "judge"))
    if not isinstance(row, dict):
        return _unmeasured("the test returned no reading of a verdict")
    if "error" in row:
        return _unmeasured("the judge did not answer the test: " + str(row["error"])[:300], ("/miner/", "judge"))
    stage = str(row.get("stage") or "")
    if stage != MT_FULL_STAGE or row.get("read") is not True:
        return _unmeasured(f"the judge stopped at stage '{stage[:40]}' before a full verdict on a valid sample "
                           "(its model may be missing on the judge engine, or it answered in a form it cannot read)",
                           ("/miner/", "judge"))
    if not _positive(row.get("seconds")) or type(row.get("generations")) is not int:
        return _unmeasured("the test measured no time for the verdict")
    load = row.get("load_seconds")
    return {"state": "measured", "seconds": round(row["seconds"], 3), "generations": row["generations"],
            "load_seconds": round(load, 3) if _positive(load) else None, "model": _model_name(row.get("model")),
            "stage": stage}


def judge_runs(env: dict, slot=0) -> bool:
    """Does the judge run on this identity: requested in its .env, or the whole role of a machine without a card
    (slot 0's CPU override). Only a judge that runs has a verdict to time."""
    return env.get("DENDRA_MINER_JUDGE") == "1" or (not slot and cpu_role() is True)


def _tail_reason(out: str) -> str:
    if out.startswith(NO_DOCKER_ACCESS):
        return NO_DOCKER_ACCESS
    last = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return "the test could not run in the miner container: " + (last[-1][:300] if last else "no output")


def machine_view(out: str, judge_on: bool, now=None) -> dict:
    """The page's view of one test, from the container's output: the line after MT_MARK, or, when there is none,
    every part not measured with the reason the command gave."""
    doc = None
    for ln in reversed(out.splitlines()):
        if ln.startswith(MT_MARK):
            try:
                doc = json.loads(ln[len(MT_MARK):])
            except ValueError:
                doc = None
            break
    if not isinstance(doc, dict):
        why = _tail_reason(out)
        return {"at": int(time.time() if now is None else now), "tokens": MT_TOKENS, "throughput": _unmeasured(why),
                "verdict": verdict_part(None, judge_on) if not judge_on else _unmeasured(why)}
    return {"at": int(time.time() if now is None else now), "tokens": MT_TOKENS,
            "throughput": speed_part(doc.get("throughput")), "verdict": verdict_part(doc.get("verdict"), judge_on)}


def _mt_view_ok(v) -> bool:
    """A stored test is shown only if it has the shape machine_view gives: a time, and two parts each measured with
    a positive figure or not measured with a reason."""
    if not isinstance(v, dict) or type(v.get("at")) is not int:
        return False
    for key, fig in (("throughput", "words_per_s"), ("verdict", "seconds")):
        p = v.get(key)
        if not isinstance(p, dict) or p.get("state") not in ("measured", "not measured"):
            return False
        if p["state"] == "measured" and not _positive(p.get(fig)):
            return False
        if p["state"] == "not measured" and not isinstance(p.get("why"), str):
            return False
    return True


def machine_tests() -> dict:
    try:
        with open(os.path.join(CONFIG_DIR, MT_FILE), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: v for k, v in doc.items() if k.isdigit() and _mt_view_ok(v)}


def save_machine_test(slot: int, view: dict) -> None:
    docs = machine_tests()
    docs[str(int(slot))] = view
    write_private_json(os.path.join(CONFIG_DIR, MT_FILE), docs)


def run_machine_test(k=0) -> tuple:
    """(HTTP status, body) of one test of identity k, one at a time. A test that measured nothing is not saved: the
    last test that measured something stays on the page, with its age."""
    if not _MT_RUN.acquire(blocking=False):
        return 409, {"error": "a test is already running"}
    try:
        env = kit_env() if not k else read_env(os.path.join(slot_row(k).get("dir") or "", ".env"))
        if not env:
            return 404, {"error": "no miner is set up on this machine yet"}
        judge_on = judge_runs(env, k)
        arg = {"deadline_s": MT_DEADLINE_S, "call_s": MT_CALL_S, "tokens": MT_TOKENS, "prompt": MT_PROMPT,
               "question": MT_QUESTION, "answer": MT_ANSWER, "judge": judge_on}
        rc, out = _for(miner_exec, k, "python3", "-c", MACHINE_TEST_SCRIPT, json.dumps(arg), timeout=MT_DEADLINE_S + 120)
        view = machine_view(out, judge_on)
        if any(view[part]["state"] == "measured" for part in ("throughput", "verdict")):
            save_machine_test(k, view)
        return 200, view
    finally:
        _MT_RUN.release()


# --- HTTP ---
# --- is this node reachable from outside? ---
# deploy/node_reachability.sh decides: it reads the node's own RPC, the address the node advertises and the peers
# that dialled in. This section runs it and says its answer in words, and it concludes no more than the script does:
# not measured stays not measured, and a miner that reads a node elsewhere has no port to open here. Opening the port
# is optional (a miner dials out and needs no inbound port), so this reading does not change the state of the miner.
REACHABILITY_SH = os.path.join(REPO, "deploy", "node_reachability.sh")
NODE_KIT = os.path.join(REPO, "deploy", "testnet-node")
# The defaults of the node kit's compose file, in its ports section: "${DENDRA_RPC_BIND:-127.0.0.1}:${DENDRA_RPC_PORT:-26657}:26657"
# and "${DENDRA_P2P_PORT:-26656}:26656". A bench reads that file and compares.
NODE_DEFAULTS = {"rpc_bind": "127.0.0.1", "rpc_port": "26657", "p2p_port": "26656"}
REACH_TIMEOUT_S = 45
REACH_TTL_S = 60
_REACH = {"at": 0.0, "view": None}
_REACH_LOCK = threading.Lock()
_REACH_RUN = threading.Lock()
# case -> (state the script gives it, what the screen says). {listen} is the address the node advertises and
# {address} the one peers dial. Every case the script prints is here: a case it prints that is not here is not read.
REACH_SAYS = {
    "open": ("open", "Other nodes have connected to yours, so it relays blocks for the network and does not only take "
                     "them. Peers dial it at {address}."),
    "no-inbound": ("closed", "Your node advertises {listen}, but no node has ever connected in. The port may not be open "
                             "yet, a firewall may drop it, or the node may be too recent to have been dialled: check again "
                             "later. This check cannot dial you from outside, it only sees who arrived."),
    "wildcard": ("closed", "Your node advertises no address that anyone can dial: it says it listens on every interface. It "
                           "works, but it takes blocks from the network and gives none back."),
    "carrier-nat": ("closed", "Your internet provider's network (carrier-grade NAT) gave this machine {listen}, an address "
                              "that is not yours. A router setting cannot open it. Ask your provider for a public IPv4 or "
                              "IPv6 address, or leave the node as it is: it works without one."),
    "loopback": ("closed", "Your node advertises {listen}, an address that only this machine can reach."),
    "private": ("closed", "Your node advertises {listen}, an address that only your own network can reach, so nobody outside "
                          "can dial it."),
    "link-local": ("closed", "Your node advertises {listen}, an address that only the network segment you are on can reach, "
                             "so nobody outside can dial it."),
    "node-unreachable": ("unknown", "The RPC of the node did not answer, so nothing is concluded. The node may be down or "
                                    "still starting."),
    "unparsable": ("unknown", "The RPC of the node answered something this check cannot read, so nothing is concluded."),
}
REACH_WORDS = {"open": "reachable", "closed": "not reachable", "unknown": "not measured", "none": "no node here"}
# The cases a router forward and the node's public address can fix. Carrier-grade NAT cannot be fixed on the router.
REACH_FIX_CASES = ("wildcard", "loopback", "private", "link-local", "no-inbound")


def _port_of(env: dict, key: str, default: str):
    """The port the node kit's .env names under `key`, the compose file's default when the line is absent, None when
    the line is not a port number."""
    v = env.get(key, "")
    if v == "":
        return default
    return v if v.isdigit() and not v.startswith("0") and int(v) < 65536 else None


def reach_rpc(nenv: dict) -> tuple:
    """(url, None) of the node's RPC as this host reaches it, or (None, why). The kit publishes it on
    DENDRA_RPC_BIND:DENDRA_RPC_PORT; a wildcard bind is reached on the loopback."""
    port = _port_of(nenv, "DENDRA_RPC_PORT", NODE_DEFAULTS["rpc_port"])
    if port is None:
        return None, "DENDRA_RPC_PORT in the node kit's .env is not a port number, so its RPC cannot be asked"
    bind = nenv.get("DENDRA_RPC_BIND", "") or NODE_DEFAULTS["rpc_bind"]
    host = "127.0.0.1" if bind == "0.0.0.0" else bind
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return None, "DENDRA_RPC_BIND in the node kit's .env is not an IPv4 address, so its RPC cannot be asked"
    return f"http://{host}:{port}", None


def _ext_host_refused(host: str) -> bool:
    """The host part that docker/node-join.sh refuses for DENDRA_EXTERNAL_ADDRESS: empty, a wildcard, a loopback, a
    private range (10/8, 172.16/12, 192.168/16), link-local, or carrier-grade NAT (100.64/10). Same patterns, same case."""
    if host in ("", "0.0.0.0", "localhost", "::", "[::]", "[::1]"):
        return True
    if any(host.startswith(p) for p in ("127.", "10.", "192.168.", "169.254.")):
        return True
    first, dot1, rest = host.partition(".")
    second, dot2, _tail = rest.partition(".")
    if not (dot1 and dot2):
        return False
    if first == "172":
        return second in {str(i) for i in range(16, 32)}
    if first == "100":
        return second in {str(i) for i in range(64, 128)}
    return False


def external_address_refusal(value: str):
    """None when docker/node-join.sh starts a node with DENDRA_EXTERNAL_ADDRESS=value (an empty value is the default and
    is accepted), else why it exits instead. A bench runs the shipped test of that script on the same values."""
    if value == "":
        return None
    if ":" not in value:
        return "it must be host:port, with the port peers dial"
    host, _colon, port = value.rpartition(":")
    if port == "" or not all(c in "0123456789" for c in port):
        return "its port is missing or is not a number"
    if _ext_host_refused(host):
        return "that address is a wildcard, a loopback, a private or a carrier-grade NAT address, so peers cannot dial it"
    return None


def open_steps(p2p, setting: str = "") -> list:
    """What opens the port, in the order that works (the address step without the router step advertises an address that
    does not answer). p2p is the port the kit publishes on this machine, None when its .env does not name a port;
    `setting` is the DENDRA_EXTERNAL_ADDRESS the node kit's .env holds now: when the node would refuse it, the first
    step says so, because starting the node with it leaves the node stopped."""
    if p2p is None:
        return ["DENDRA_P2P_PORT in the node kit's .env is not a port number: correct it first."]
    node_dir = shlex.quote(NODE_KIT)
    steps = []
    why = external_address_refusal(setting)
    if why is not None:
        steps.append(f"{NODE_KIT}/.env holds DENDRA_EXTERNAL_ADDRESS={setting[:120]}, and the node refuses to start with it: "
                     f"{why}. Correct that line, or empty it, before the node is started again.")
    steps += [
        f"On your router, forward TCP port 26656 to this machine, port {p2p}. That port only, and never put this machine "
        "in the router's DMZ: the RPC of the node (port 26657) must stay private.",
        f"Tell the node its public address: set DENDRA_EXTERNAL_ADDRESS=<your public IP address or name>:26656 in "
        f"{NODE_KIT}/.env. Use the address the internet sees for your router, not the one this machine has on your network. "
        "The node refuses to start when the port is missing, or when the address is local, private, a wildcard or behind "
        "carrier-grade NAT. After you save the line, press Check now: this card says so when the node would refuse it.",
        f"Then run: cd {node_dir} && docker compose up -d. This restarts the node, and the miner above cannot read the chain "
        "until it is back. If the node does not start, empty the DENDRA_EXTERNAL_ADDRESS line and run the same command "
        "again: the node starts as it did before.",
        "Check again here after a few minutes. This check sees the nodes that connected in, so it turns to reachable once a "
        "peer has dialled yours.",
    ]
    return steps


def wsl_steps(p2p) -> list:
    """The part of the way in that lies between Windows and the WSL 2 virtual machine."""
    port = p2p if p2p is not None else NODE_DEFAULTS["p2p_port"]
    return [
        "Windows 11 (version 22H2 or later): add networkingMode=mirrored under [wsl2] in the .wslconfig file of your Windows "
        "user folder (create it if it is missing), then run: wsl --shutdown",
        "In an administrator PowerShell, let this one port into the WSL virtual machine: New-NetFirewallHyperVRule "
        "-Name DendraP2P -DisplayName 'Dendra P2P' -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' "
        f"-Protocol TCP -LocalPorts {port}",
        "On Windows 10, or without mirrored networking, WSL takes a new internal address at every start, so a port rule "
        "breaks at the next restart: the node then stays as it is, reaching out only.",
        "The router forward above must point at this Windows PC.",
    ]


def _reach_unread(why: str) -> dict:
    return {"state": "unknown", "case": "not-read", "why": why}


def _reach_doc_ok(doc, rc) -> bool:
    """The script's JSON line against its exit code and against the table above. Anything else is not read."""
    if not isinstance(doc, dict) or doc.get("source") != "node":
        return False
    state, case = doc.get("state"), doc.get("case")
    if {0: "open", 1: "closed", 2: "unknown"}.get(rc) != state or doc.get("exit") != rc:
        return False
    if case not in REACH_SAYS or REACH_SAYS[case][0] != state:
        return False
    for k in ("listen", "address"):
        if doc.get(k) is not None and type(doc[k]) is not str:
            return False
    for k in ("inbound", "outbound"):
        if doc.get(k) is not None and type(doc[k]) is not int:
            return False
    return state != "open" or (type(doc.get("address")) is str and doc["address"] != "")


def run_reachability(rpc: str) -> dict:
    """The script's own reading of the node at `rpc`, or {"state": "unknown", "case": "not-read", "why"}. The
    environment of this process does not decide what is read: the fixture variables of the script's bench are removed."""
    if not os.path.isfile(REACHABILITY_SH):
        return _reach_unread(f"the check is not in this clone ({REACHABILITY_SH})")
    env = {k: v for k, v in os.environ.items() if k not in ("DENDRA_NETINFO_FILE", "DENDRA_STATUS_FILE")}
    env["DENDRA_LOCAL_RPC"] = rpc
    try:
        r = subprocess.run(["bash", REACHABILITY_SH, "--json"], capture_output=True, text=True, timeout=REACH_TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _reach_unread(f"the check did not run ({type(e).__name__})")
    line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("{")), "")
    try:
        doc = json.loads(line) if line else None
    except ValueError:
        doc = None
    if not _reach_doc_ok(doc, r.returncode):
        return _reach_unread(f"the check answered something this application does not read (exit {r.returncode})")
    return doc


def _reach_none(case: str, says: str, now: int) -> dict:
    return {"state": "none", "case": case, "word": REACH_WORDS["none"], "says": says, "peers": "not applicable", "advertised": None,
            "reachable_at": None, "steps": [], "wsl_steps": [], "checked_at": now, "optional": True}


def reachability_from(doc: dict, nenv: dict, now: int) -> dict:
    """The screen's reading of one answer of the script: the state and its word, what it says, the peers, the address the
    node advertises, the address peers dial (only when it is open), and the steps that open the port (only for a case
    they fix)."""
    state, case = doc["state"], doc["case"]
    if case == "not-read":
        says = doc.get("why", "")
    else:
        says = REACH_SAYS[case][1].format(listen=str(doc.get("listen") or "")[:200], address=str(doc.get("address") or "")[:200])
    p2p = _port_of(nenv, "DENDRA_P2P_PORT", NODE_DEFAULTS["p2p_port"])
    steps = open_steps(p2p, nenv.get("DENDRA_EXTERNAL_ADDRESS", "")) if case in REACH_FIX_CASES else []
    wsl = wsl_steps(p2p) if steps and os.environ.get("WSL_DISTRO_NAME") else []
    inbound, outbound = doc.get("inbound"), doc.get("outbound")
    peers = f"{inbound} inbound, {outbound} outbound" if type(inbound) is int and type(outbound) is int else "not read"
    return {"state": state, "case": case, "word": REACH_WORDS[state], "says": says, "peers": peers,
            "advertised": doc.get("listen") or None, "reachable_at": doc.get("address") if state == "open" else None,
            "steps": steps, "wsl_steps": wsl, "checked_at": now, "optional": True}


def _reachability_read() -> dict:
    now = int(time.time())
    env = kit_env()
    if not env:
        return _reach_none("no-kit", "No miner is set up on this machine, so there is no node to test.", now)
    if not node_is_here(env):
        return _reach_none("remote-rpc", f"This miner reads the chain from {_node_host(env)[:120]}: no node runs on this "
                                         "machine, so there is no port to open.", now)
    nenv = read_env(os.path.join(NODE_KIT, ".env"))
    rpc, why = reach_rpc(nenv)
    return reachability_from(_reach_unread(why) if rpc is None else run_reachability(rpc), nenv, now)


def reachability_view(force=False) -> dict:
    """The reading, kept REACH_TTL_S seconds so that a page that asks every minute runs the script once; `force` reads again."""
    with _REACH_LOCK:
        if not force and _REACH["view"] is not None and time.monotonic() - _REACH["at"] < REACH_TTL_S:
            return _REACH["view"]
    view = _reachability_read()
    with _REACH_LOCK:
        _REACH.update(at=time.monotonic(), view=view)
    return view


# --- the capacity sender's last round ---
# The Final Testnet Season pays nothing to an identity without a signed capacity report, and the sender
# (services/capacity_sender.py, one per identity) writes every round's outcome to its status file
# (capacity_sender.write_status). It is read here, inside the container, by key, the way the heartbeat is.
_CAPACITY_READER = _NL.join([
    "import json, os",
    "p = os.environ.get('DENDRA_CAPACITY_STATUS_FILE') or '/tmp/dendra-capacity-status.json'",
    "try:",
    "    with open(p, encoding='utf-8') as f:",
    "        d = json.load(f)",
    "except FileNotFoundError:",
    "    raise SystemExit(3)",
    "except Exception:",
    "    raise SystemExit(4)",
    "if not isinstance(d, dict):",
    "    raise SystemExit(4)",
    "print(json.dumps(d))",
])
# The registry stops counting a report after a day (capacity_sender.PERIOD_S comment): a sent round older
# than that is a report the season does not count any more, said as such and never as fine.
CAPACITY_STALE_S = 86400


def capacity_round_view(slot=0) -> dict:
    """The last round of this identity's capacity sender. Three answers: 'never' (the sender wrote no round
    yet: it writes one about a minute after the miner starts, and nothing while the miner is stopped),
    'unread' (the container did not answer, or the file is not the document), 'read' (with the outcome and
    its age). A round that was not read is never shown as sent."""
    rc, out = _for(miner_exec, slot, "python3", "-I", "-c", _CAPACITY_READER, timeout=30)
    if rc == 3:
        return {"state": "never", "why": "the sender has written no round yet: it writes one about a minute after "
                                         "the miner starts, and nothing while the miner is stopped"}
    if rc != 0:
        # compose() answers 127 itself when there is no docker; any other code is the container's or compose's.
        if rc == 127 and out.startswith("docker is not installed"):
            return {"state": "unread", "why": "Docker is not installed on this machine"}
        last = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"state": "unread", "why": "the miner container did not answer: "
                                          + (last[-1][:200] if last else f"exit code {rc} and no output")}
    doc = None
    for line in reversed(out.splitlines()):
        if line.strip().startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                doc = None
            break
    if not isinstance(doc, dict) or not isinstance(doc.get("outcome"), str) or not doc.get("outcome"):
        return {"state": "unread", "why": "the sender's last round is not a document this application reads"}
    at, sent_at = doc.get("at"), doc.get("sent_at")
    return {"state": "read", "outcome": doc["outcome"][:40],
            "why": " ".join(str(doc.get("why") or "").split())[:300],
            "age_s": max(0, int(time.time()) - at) if type(at) is int else None,
            # The sender's last sent report, which the registry counts for a day from its sending. None: the
            # sender has sent none since it started (capacity_sender.py::main keeps it in memory), not "never".
            "sent_at": sent_at if type(sent_at) is int else None,
            "sent_age_s": max(0, int(time.time()) - sent_at) if type(sent_at) is int else None,
            "next_at": doc.get("next_at") if type(doc.get("next_at")) is int else None,
            "identity": str(doc.get("identity") or "")[:80],
            "http_code": doc.get("http_code") if type(doc.get("http_code")) is int else None,
            "note": str(doc.get("pay_note") or "")}


# --- the dashboard: one card per subject, each green, amber or red with one sentence ---
# The six subjects the owner asked for. Each card is decided from facts the overview already reads (the
# newest health report, the judge's memory and seat, the sender's last round, the kit floor, the latest
# ranked day): a card whose facts were not read is amber, never green, so a green dashboard rests on
# readings only.
DASH_GREEN, DASH_AMBER, DASH_RED = "green", "amber", "red"


def _dndr(u: int) -> str:
    return f"{u / 1e6:g} DNDR"


def _report_check(f: dict, cid: str):
    """One check line of the newest report (a quick run's carried lines included), or None."""
    r = f.get("report")
    if not isinstance(r, dict):
        return None
    return next((c for c in (r.get("checks") or []) + (r.get("carried") or []) if c.get("id") == cid), None)


def _line_age(f: dict, cid: str) -> tuple:
    """(stale, age_s) of the report that gave the check line `cid`: the newest report's own line, or a line it
    carries from the hourly report (newest_report). `stale` is True, False, or None when the report does not
    say how often it runs (its age is then not judged, as STATUS_RULES age_unjudged says)."""
    r = f.get("report") if isinstance(f.get("report"), dict) else None
    if r is None:
        return None, None
    if any(c.get("id") == cid for c in r.get("checks") or []):
        return r.get("stale"), r.get("age_s")
    sched = f.get("schedule") if isinstance(f.get("schedule"), dict) else {}
    return sched.get("stale"), r.get("carried_age_s")


def _as_of(f: dict, cids) -> tuple:
    """(stale, words): whether any of the lines `cids` comes from a report older than its schedule, and the
    clause that dates what they say ("at the last health check, 40 min ago")."""
    ages = [_line_age(f, c) for c in cids if _report_check(f, c)]
    stale = True if any(s is True for s, _ in ages) else (None if any(s is None for s, _ in ages) else False)
    known = [a for _, a in ages if type(a) is int]
    when = f"at the last health check, {age_words(max(known))} ago" if known else "at the last health check"
    return stale, when


STALE_REPORT = (" The report is older than its schedule, so this is not what holds now: the hourly check has "
                "not run since (read the State card).")


def _dash_judge(f: dict) -> tuple:
    role = f.get("role") or {}
    jm = f.get("jmem") if isinstance(f.get("jmem"), dict) else {}
    seat = jm.get("seat") if isinstance(jm.get("seat"), dict) else {}
    word = role.get("word")
    if seat.get("excluded") is True:
        return DASH_RED, ("The chain has stopped drawing this judge: it was seated on " + str(seat.get("missed_draws"))
                          + " concluded audit(s) without an answer.")
    if word == "mute":
        return DASH_RED, ("The judge is requested and gives no verdict (" + (role.get("why") or "its worker or its "
                          "model is absent") + "): the chain draws this identity into juries until its missed draws "
                          "reach judge_missed_draws_max or neither its last verdict nor its declaration is within "
                          "judge_verdict_window_blocks, and every seat is silent.")
    if word == "off":
        # The same rule as diagnosis_view's judge-off: an identity that does not judge earns no juror pay, so no green.
        return DASH_AMBER, ("The judge is off on this identity: it declares no judge, and its daemon withdraws one the "
                            "chain holds, so the chain stops drawing it into new juries (the Judge switch turns it on "
                            "where this machine can judge).")
    if word is None:
        return DASH_AMBER, "Whether the judge runs was not read" + (": " + role.get("why") if role.get("why") else "") + "."
    if word != "active":
        # "unknown": the judge is requested and its role could not be read (miner_selftest.py::JUDGE_ROLES), which
        # is never folded into off nor into active.
        return DASH_AMBER, ("Whether the judge runs was not read: the last check read its role as " + word
                            + (" (" + role.get("why").rstrip(".") + ")" if role.get("why") else "") + ".")
    miner = f.get("miner") or {}
    stale_age = role.get("age_s")
    sched = f.get("schedule") if isinstance(f.get("schedule"), dict) else {}
    when = f"the last health check, {age_words(stale_age)} ago," if type(stale_age) is int else "the last health check"
    if miner.get("running") is False:
        return DASH_AMBER, (f"The judge does not run now: the miner container is not running ({when} read its role "
                            "as active).")
    if miner.get("running") is not True:
        return DASH_AMBER, (f"Whether the judge runs now was not read: Docker did not say whether the miner container "
                            f"runs ({when} read its role as active).")
    if sched.get("stale") is True:
        return DASH_AMBER, f"The judge's role was read as active by {when} and not since." + STALE_REPORT
    if jm.get("state") == "unread":
        return DASH_AMBER, "The judge runs, and its memory was not read (" + (jm.get("why") or "no reading") + ")."
    if jm.get("paused") is True:
        return DASH_AMBER, "The judge's memory guard has paused the loads of the model: it loads again when the machine has room."
    if jm.get("gate") == "closed":
        return DASH_AMBER, "The judge's memory guard holds the model back (" + (jm.get("gate_why") or "no room read") + ")."
    ab = jm.get("memory_abstentions")
    if type(ab) is int and ab > 0:
        return DASH_AMBER, f"{ab} audit(s) were left out for lack of memory: each is juror pay lost."
    mode_words = {"resident": "resident (the model stays loaded)",
                  "on-demand": "on demand (the model is loaded for audits)"}.get(jm.get("mode"), "mode not read")
    load = ("the model is loaded" if jm.get("loaded") is True
            else ("the model is not loaded now" if jm.get("loaded") is False else "the model's load was not read"))
    ab_words = (f"{ab} audit(s) left out for lack of memory" if type(ab) is int
                else "the audits left out for lack of memory were not counted")
    return DASH_GREEN, f"The judge runs, {mode_words}, {load}, {ab_words}."


# The sender's rounds that it repeats by itself with a chance of success: miner_selftest.py::CAPACITY_RETRIED, and
# capacity_sender.ABSENT (it sends once the chain records the identity). Any other outcome waits on the operator.
CAPACITY_RETRIED = ("unread", "refused", "unreachable", "error", "absent")


def capacity_verdict(f: dict) -> dict:
    """{"level", "says", "blocks", "round"}: the capacity report of one identity, for the dashboard card, the pay
    blockers and the diagnosis alike. What the registry counts decides first (C8 of a report that is not older
    than its schedule); then the sender's last sent report, which the registry counts for a day from its
    sending; then the last round. Red, and a pay blocker, only when no counted or fresh report is known and the
    sender cannot send by itself, or when the last sent one is known to be older than a day. A round that was
    not read blocks nothing. `round` is True when the verdict comes from the sender's round, not from C8."""
    pay = "The season pays nothing to an identity without one."
    c8 = _report_check(f, "C8")
    cap = f.get("capacity") or {}
    outcome = cap.get("outcome") if cap.get("state") == "read" else None
    if c8 and c8.get("state") == "ko":
        return {"level": DASH_RED, "blocks": True, "round": False,
                "says": "The identity's signed capacity report is not counted (" + (c8.get("measured") or "not "
                        "measured") + "). " + pay}
    stale, when = _as_of(f, ["C8"])
    if c8 and c8.get("state") == "ok" and stale is not True:
        says = f"The registry counted this identity's signed capacity report {when}: the season can pay its ranked days."
        if outcome is None or outcome == "sent":
            return {"level": DASH_GREEN, "blocks": False, "round": False, "says": says}
        if outcome in CAPACITY_RETRIED:
            return {"level": DASH_GREEN, "blocks": False, "round": False,
                    "says": says + f" The sender's last round did not go out ({outcome}); it tries again by itself."}
        return {"level": DASH_AMBER, "blocks": False, "round": True,
                "says": says + f" The sender's last round did not go out ({outcome}): "
                               + str(cap.get("why") or "no reason given").rstrip(".") + ". It cannot send the next "
                               "one by itself, and the registry stops counting a report after a day."}
    if cap.get("state") == "read":
        if outcome == "sent":
            age = cap.get("age_s")
            if type(age) is int and age > CAPACITY_STALE_S:
                return {"level": DASH_AMBER, "blocks": False, "round": True,
                        "says": f"The last signed report went out {age_words(age)} ago: the registry stops counting a "
                                "report after a day, and a newer one should have been sent."}
            ago = f", {age_words(age)} ago" if type(age) is int else ""
            return {"level": DASH_GREEN, "blocks": False, "round": True,
                    "says": f"The signed capacity report went out{ago}: the season can pay this identity's ranked days."}
        sent_age = cap.get("sent_age_s")
        why = str(cap.get("why") or "no reason given").rstrip(".") + "."
        missed = f"The sender's last round did not go out ({outcome}): {why}"
        if type(sent_age) is int and sent_age <= CAPACITY_STALE_S:
            return {"level": DASH_AMBER, "blocks": False, "round": True,
                    "says": f"{missed} The report it sent {age_words(sent_age)} ago is counted for a day from its "
                            "sending" + ("; the sender tries again by itself." if outcome in CAPACITY_RETRIED
                                         else ", and the sender cannot send the next one by itself.")}
        if outcome in CAPACITY_RETRIED and sent_age is None:
            return {"level": DASH_AMBER, "blocks": False, "round": True,
                    "says": f"{missed} The sender tries again by itself; whether a report of this identity is "
                            "counted now was not read."}
        lapsed = (f" The last report it sent went out {age_words(sent_age)} ago, and the registry stops counting a "
                  "report after a day." if type(sent_age) is int else "")
        return {"level": DASH_RED, "blocks": True, "round": True,
                "says": f"The signed capacity report did not go out ({outcome}): {why}{lapsed} {pay}"}
    if cap.get("state") == "never":
        return {"level": DASH_AMBER, "blocks": False, "round": True,
                "says": "The capacity sender has written no round yet. The season pays nothing to an identity "
                        "without a signed capacity report."}
    return {"level": DASH_AMBER, "blocks": False, "round": True,
            "says": "The capacity sender's last round was not read" + (f": {cap.get('why')}" if cap.get("why") else "")
                    + ". Whether the season can pay this identity is not known."}


def _dash_capacity(f: dict) -> tuple:
    v = capacity_verdict(f)
    return v["level"], v["says"]


def _dash_kit(f: dict) -> tuple:
    st, why = kit_floor_state(f.get("floor"), f.get("now") or time.time())
    kit = f.get("kit") or {}
    mine = (f.get("floor") or {}).get("mine") or "unknown"
    if st == "refused":
        fields = _kit_fields(f)
        return DASH_RED, (f"The network refuses this kit since {fields['deadline']} (kit {fields['mine']}, minimum "
                          f"{fields['min']}): the relay refuses its deposits and the season stops paying it.")
    if st == "soon":
        fields = _kit_fields(f)
        return DASH_AMBER, (f"This kit stops being accepted on {fields['deadline']} (kit {fields['mine']}, minimum "
                            f"{fields['min']}): update before then.")
    if kit.get("behind") is True:
        return DASH_AMBER, f"A newer kit is published (kit {kit.get('latest') or '?'}); this machine runs kit {mine}."
    if st == "unread":
        return DASH_AMBER, "This kit was not compared with the network's" + (f" ({why})" if why else "") + "."
    if st == "none":
        return DASH_GREEN, f"The network publishes no kit floor, so nothing refuses this kit (kit {mine})."
    return DASH_GREEN, f"This kit (kit {mine}) is at or above the network's published floor."


def _dash_node(f: dict) -> tuple:
    c1, c2 = _report_check(f, "C1"), _report_check(f, "C2")
    stale, when = _as_of(f, ["C1", "C2"])
    if c2 and c2.get("state") == "ko":
        return DASH_RED, f"The node is behind the network ({c2.get('measured')}): a challenge read too late is work lost."
    if c1 and c1.get("state") == "ko":
        return DASH_RED, f"The node does not answer ({c1.get('measured')}): the miner reads no chain and does no work."
    if any(c and c.get("state") == "unmeasured" for c in (c1, c2)):
        return DASH_AMBER, "The node checks could not be measured in the last health report: read the Health card."
    if not c1 and not c2:
        return DASH_AMBER, "No health report has run yet, so whether the node answers was not read."
    if stale is True:
        return DASH_AMBER, f"The node answered and was caught up with the network {when}." + STALE_REPORT
    return DASH_GREEN, f"The node answered and was caught up with the network {when}."


def _dash_relay(f: dict) -> tuple:
    c5, c6 = _report_check(f, "C5"), _report_check(f, "C6")
    stale, when = _as_of(f, ["C5", "C6"])
    bad = next((c for c in (c5, c6) if c and c.get("state") == "ko"), None)
    if bad:
        return DASH_RED, (f"The relay ({bad.get('id')}): {bad.get('measured')}. Work arrives through the relay, and "
                          "answers are anchored on it.")
    if any(c and c.get("state") == "unmeasured" for c in (c5, c6)):
        return DASH_AMBER, "The relay checks could not be measured in the last health report: read the Health card."
    if not c5 and not c6:
        return DASH_AMBER, "No health report has run yet, so the relay was not read."
    says = f"The relay's queue answered, and this miner's key on it matched the chain's, {when}."
    if stale is True:
        return DASH_AMBER, says + STALE_REPORT
    return DASH_GREEN, says


def _dash_work(f: dict) -> tuple:
    w = f.get("work")
    if not isinstance(w, dict):
        if f.get("season_enabled") is False:
            return DASH_AMBER, ("The season's address is not in this kit's settings, so the rankings are not read "
                                "here; the season itself counts what the chain records.")
        return DASH_AMBER, "No ranked day is readable for this identity: the season's figures were not read."
    day = w.get("day")
    n = day + 1 if type(day) is int else "?"
    if not w.get("listed"):
        return DASH_AMBER, (f"Season day {n}, the latest ranked, counts no fact for this identity: no verified "
                            "request, verdict or proven window.")
    paid = w.get("paid_udndr")
    if type(paid) is int and paid > 0:
        return DASH_GREEN, (f"Season day {n}, the latest ranked, paid {_dndr(paid)} for {w.get('verified_requests')} "
                            f"verified request(s), {w.get('verdicts')} verdict(s) and {w.get('presence')} proven "
                            "window(s).")
    heavy = any(k in str(w.get("reason") or "") for k in ("capacity report", "no payout address", "below the floor",
                                                          "not datable"))
    says = f"Season day {n}, the latest ranked, paid nothing"
    if w.get("gross_udndr"):
        says += f" of a gross of {_dndr(w['gross_udndr'])}"
    reason = reason_plain_text(str(w.get("reason") or ""))
    return (DASH_RED if heavy else DASH_AMBER), says + "." + (f" {reason}." if reason else "")


DASH_CARDS = (("judge", "Judge mode and abstentions", _dash_judge),
              ("capacity", "Capacity report", _dash_capacity),
              ("kit", "Kit", _dash_kit),
              ("node", "Node", _dash_node),
              ("relay", "Relay", _dash_relay),
              ("work", "Work of the day", _dash_work))


def dashboard_view(f: dict) -> dict:
    """The first screen's cards, in the order of DASH_CARDS, each one sentence; `level` is the worst of them."""
    cards = []
    for cid, name, fn in DASH_CARDS:
        level, says = fn(f)
        cards.append({"id": cid, "name": name, "level": level, "says": says})
    worst = DASH_GREEN
    for c in cards:
        if c["level"] == DASH_RED:
            worst = DASH_RED
            break
        if c["level"] == DASH_AMBER:
            worst = DASH_AMBER
    return {"level": worst, "cards": cards}


# --- the diagnosis: every check the kit already has, each problem with its stake and its fix ---
# What each self-test check decides, in the terms of the season: why the thing it reads matters (pay,
# judge, network). The words follow the check's own docstring (miner_selftest.py), never a new theory.
CHECK_MATTERS = {
    "C1": "the node is this miner's window on the chain: without it, no challenge is read and no answer is sent",
    "C2": "a node behind the network reads its challenge too late to answer: the work is paid to another",
    "C3": "the chain ranks and pays registered identities only",
    "C4": "presence is drawn every day: a window not proven is presence reward lost, and a smaller weight in the work draw",
    "C5": "the relay's queue is where the work arrives: unreadable, the miner stands idle",
    "C6": "a copy of this miner's key on the relay that differs from the chain's anchors nothing: answers could score zero",
    "C7": "the model is what answers the programme's requests: a probe that fails is work refused",
    "C8": "the season pays nothing to an identity without a fresh signed capacity report",
    "C9": "the daemon and its workers do the mining: a stopped one does nothing until it is restarted",
    "C10": "a judge engine without its model gives no verdict, and the seat votes nothing",
    "C11": "a judge drawn into juries that gives no verdict holds a mute seat: no juror pay",
    "C12": "the judge's memory decides whether its model may load: a guard that stays closed is a seat that stays silent",
}
# Why each rule of the state table matters, for the diagnosis lines built from it.
RULE_MATTERS = {
    "docker": "nothing of the miner can be read or driven while Docker does not answer",
    "stopped": "a miner that does no work earns nothing, and its presence lapses",
    "kit_refused": "the season stops paying a kit the network refuses, and the relay refuses its deposits",
    "node_behind": "a node behind the network reads its challenge too late to answer: the work is paid to another",
    "judge_mute": "a judge that gives no verdict holds a mute seat: no juror pay, and the seat may be dropped",
    "capacity_report": "the season pays nothing to an identity without a fresh signed capacity report",
    "kit_soon": "after the deadline the relay refuses this kit's deposits and the season stops paying it",
    "kit_behind": "a newer kit is published; staying behind leads to the floor above",
    "report_old": "the hourly check is the one that rings while nobody looks: without it, a standing problem stays unseen",
    "no_report": "without the hourly check, a standing problem stays unseen",
    "not_measured": "a check that could not be measured is neither fine nor broken: it hides a problem like a blank",
    "report_numbers": "a report whose own numbers disagree with its lines cannot be trusted",
    "age_unjudged": "the report's age cannot be judged, so a stopped schedule stays unseen",
    "phrase": "anyone who copies this machine's keys can spend what the miner earns and take the account",
    "phrase_unread": "whether a recovery phrase waits on this machine is not known",
    "kit_unread": "a kit the network refuses must be updated before the deadline; unread, the deadline cannot be watched",
}
# The fix of a round that sent nothing, by the sender's own outcome word (capacity_sender.one_round): what
# the operator can do, or that the sender retries by itself.
CAPACITY_ROUND_FIX = {
    "no-url": "set DENDRA_CAPACITY_URL in the kit's .env to the registry address the network's file publishes "
              "(deploy/join.sh writes it), then restart the miner (the Miner card's Restart)",
    "no-inventory": "write the hardware inventory: bash deploy/testnet-miner/publish-capacity.sh --inventory",
    "bad-slot": "correct DENDRA_SLOT in this identity's .env (a slot number), then restart the miner",
    "no-key": "the signing key does not resolve in this container's keyring: read the sender's line in the Logs "
              "section of this application",
    "unsigned": "signing returned nothing: read the sender's line in the Logs section of this application",
    "absent": "nothing to do by hand: the sender sends the report once the chain records this identity, then every hour",
    "other": "the chain records this identity for another operator's key: register this identity again with the key "
             "this machine holds (deploy/join.sh), and the sender sends by itself",
    "unread": "the chain's record could not be read: the sender tries again by itself",
    "refused": "the registry refused the report (the HTTP code is in the sender's line): read the Logs section",
    "unreachable": "the registry did not answer: the sender tries again by itself (five minutes, then slower)",
}


def diagnosis_view(f: dict) -> dict:
    """Every problem the existing outputs name, each with what is wrong, why it matters and the exact fix:
    the fired rules of the state table (each failed check of its own instead of the generic one), then the
    readings the table does not carry (the sender's last round, the judge's memory and seat, the payout
    record, the season's address). A problem is never invented from a fact that was not read."""
    st = status_view(f)
    problems = []
    for r in st["rules"]:
        if r["id"] == "check_failed":
            continue                       # each failed check is listed on its own below
        problems.append({"id": r["id"], "level": "amber" if r["level"] == "orange" else r["level"], "what": r["says"],
                         "matters": RULE_MATTERS.get(r["id"], ""), "fix": r["do"], "source": r["source"]})
    fired = {p["id"] for p in problems}
    claimed = {"C2"} if "node_behind" in fired else set()
    claimed |= {"C8"} if "capacity_report" in fired else set()
    claimed |= {"C11"} if "judge_mute" in fired else set()
    report = f.get("report")
    carried_ids = {c.get("id") for c in (report or {}).get("carried") or []}
    for c in (report or {}).get("checks") or []:
        if c.get("state") == "ok" or c.get("id") in claimed:
            continue
        problems.append({"id": c.get("id") or "?", "level": "red" if c.get("state") == "ko" else "amber",
                         "what": f"{c.get('name')}: {c.get('measured')}"
                                 + (" (the result stands from the hourly report)" if c.get("id") in carried_ids else ""),
                         "matters": CHECK_MATTERS.get(c.get("id"), ""),
                         "fix": c.get("fix") or "Read the Health card: it gives the fix.",
                         "source": "miner_selftest.py::run_checks"})
    cap = f.get("capacity") or {}
    cv = capacity_verdict(f)
    # One rule with the dashboard card and the pay blockers (capacity_verdict); a C8 that is ko is the fired
    # capacity_report rule's line above, never said twice.
    if "C8" not in claimed and cv["round"] and cv["level"] != DASH_GREEN:
        if cap.get("state") == "read" and cap.get("outcome") != "sent":
            fix = CAPACITY_ROUND_FIX.get(cap.get("outcome"), "Read the sender's line in the Logs section of this application")
        elif cap.get("state") == "read":
            fix = ("the sender sends one every hour while the miner runs: read its last line in the Logs section of "
                   "this application")
        else:
            fix = ("press Run now in the Health card, or read the sender's line in the Logs section of this "
                   "application")
        problems.append({"id": "capacity-round", "level": cv["level"], "what": cv["says"],
                         "matters": CHECK_MATTERS["C8"], "fix": fix, "source": "capacity_sender.py::one_round"})
    jm = f.get("jmem") if isinstance(f.get("jmem"), dict) else {}
    seat = jm.get("seat") if isinstance(jm.get("seat"), dict) else {}
    if "judge_mute" not in fired:
        if jm.get("state") == "unread" and (f.get("role") or {}).get("requested"):
            problems.append({"id": "judge-memory", "level": "amber",
                             "what": "The judge's memory was not read (" + (jm.get("why") or "no reading") + ").",
                             "matters": CHECK_MATTERS["C12"],
                             "fix": "reload this page, then press Run now in the Health card: C12 reads the memory watch",
                             "source": "dendra_app.py::judge_memory_view"})
        elif jm.get("state") == "read" and jm.get("gate") == "closed":
            problems.append({"id": "judge-memory", "level": "amber",
                             "what": "The judge's memory guard holds the model back (" + (jm.get("gate_why") or "no room "
                                     "read") + ").",
                             "matters": CHECK_MATTERS["C12"],
                             "fix": "free RAM on the machine, or seat the judge again with a smaller ceiling "
                                    "(deploy/join.sh --judge): the guard opens when there is room",
                             "source": "dendra_app.py::judge_memory_view"})
        elif jm.get("paused") is True:
            problems.append({"id": "judge-memory", "level": "amber",
                             "what": "The judge's memory guard has paused the loads of the model.",
                             "matters": CHECK_MATTERS["C12"],
                             "fix": "free RAM on the machine: the guard resumes when its readings stay clean",
                             "source": "dendra_app.py::judge_memory_view"})
        ab = jm.get("memory_abstentions")
        if jm.get("state") == "read" and type(ab) is int and ab > 0:
            problems.append({"id": "judge-abstentions", "level": "amber",
                             "what": f"{ab} audit(s) were left out for lack of memory since the counters began.",
                             "matters": CHECK_MATTERS["C11"],
                             "fix": "free RAM on the machine, so the model stays loaded through the audits",
                             "source": "dendra_app.py::judge_memory_view"})
    if seat.get("excluded") is True and "judge_mute" not in fired:
        problems.append({"id": "judge-seat", "level": "red",
                         "what": "The chain has stopped drawing this judge: it was seated on "
                                 f"{seat.get('missed_draws')} concluded audit(s) without an answer.",
                         "matters": "a judge the chain does not draw earns no juror pay",
                         "fix": "remove the cause above: the daemon then declares the judge again by itself once its "
                                "judge worker runs, its engine holds the model and the memory guard lets it load, which "
                                "clears the count (the "
                                "Logs card shows the declaration). If the daemon already declared it again once and the "
                                "count excluded it again with no answer, it waits for a restart: the Miner card's "
                                "Restart, or turn the Judge switch off, then on once the Logs card shows that the daemon "
                                "withdrew the judge declaration (or bash deploy/testnet-miner/judge.sh status says the "
                                "chain holds no judge declaration)",
                         "source": "dendra_app.py::judge_seat_from"})
    if (f.get("role") or {}).get("word") == "off" and "judge_mute" not in fired:
        problems.append({"id": "judge-off", "level": "amber",
                         "what": "The judge is off on this identity: it declares no judge, and its daemon withdraws "
                                 "one the chain holds, so the chain stops drawing it into new juries.",
                         "matters": "an identity that does not judge earns no juror pay",
                         "fix": "turn the Judge switch on, on a machine that can judge (bash deploy/hw_probe.sh "
                                "--can-judge): the Switches card greys it with its reason where this machine cannot",
                         "source": "dendra_app.py::role_view"})
    pv = f.get("payout") or {}
    if f.get("season_enabled") is False:
        problems.append({"id": "season-address", "level": "amber",
                         "what": "The rankings are not read here: no Final Testnet Season address is in this kit's "
                                 "settings, so the pay of the ranked days cannot be watched.",
                         "matters": "the season can be paying or not; this application cannot say",
                         "fix": "set DENDRA_FINAL_SEASON_URL in the kit's .env (deploy/join.sh writes it from the "
                                "network's file)",
                         "source": "dendra_app.py::programme_view"})
    elif pv.get("declared") == "" and pv.get("to_machine_key") is not None:
        fix = ("declare one in the Where rewards go card of this application, or with bash deploy/join.sh "
               "--payout-address dendra1...")
        if pv.get("owner"):
            fix = ("prepare it here with final_season_miner.py payout-prepare and sign it on the owner's machine "
                   "(deploy/testnet-miner/README.md, owner mode)")
        problems.append({"id": "payout-address", "level": "amber",
                         "what": "The programme has no payout address on record for this identity: a day with work is "
                                 "ranked with nothing payable.",
                         "matters": "the season pays the address on record only",
                         "fix": fix, "source": "dendra_app.py::payout_view"})
    problems.sort(key=lambda p: ("red", "amber").index(p["level"]))
    level = st["level"]
    if any(p["level"] == "red" for p in problems):
        level = "red"
    elif problems and level == "green":
        level = "orange"
    checked = st["checked"]
    if not problems:
        if cap.get("state") == "read" and cap.get("outcome") == "sent":
            checked.append("the capacity sender's last round was sent")
        if pv.get("declared"):
            checked.append("the programme holds a payout address for this identity")
    return {"level": level, "problems": problems, "checked": checked}


# --- the pay, by season day ---
# The reasons a day paid less than its gross, in the words of the programme's calculation
# (final_season_calc.compute_day), each with its plain sentence. A reason this table does not know is
# shown as the ranking wrote it.
REASON_PLAIN = (
    ("no signed capacity report at or before the day's end",
     "no signed capacity report of this identity reached the registry before the day's end, and the season pays "
     "nothing to an identity without one"),
    ("no payout address known",
     "the programme has no payout address on record for this identity"),
    ("presence not paid: no verified request counted that day",
     "the windows proven that day were paid nothing: presence is paid only on a day with at least one verified request"),
    ("registration not datable: not paid",
     "the season could not date this identity's registration, so the day is not paid"),
    ("daily budget shared pro rata",
     "the day's budget did not cover every identity's gross, and it was shared pro rata"),
)
_KIT_BELOW = re.compile(r"kit ([0-9]{1,6}) below the floor ([0-9]{1,6}): not paid")
_PROBATION = re.compile(r"probation: first full day after registration, ([0-9]{1,3}) percent of the gross")


def reason_plain_text(reason: str, listed=True) -> str:
    """The ranking's own reason in plain words, part by part (compute_day joins its parts with '; ')."""
    reason = (reason or "").strip()
    if not reason:
        return "" if listed else ("the ranking lists no row for this identity that day: no verified request, verdict "
                                  "or proven window was counted for it")
    out = []
    for part in reason.split("; "):
        m = _KIT_BELOW.fullmatch(part)
        if m:
            out.append(f"kit {m.group(1)} was below the floor of {m.group(2)} the network published, so the day was "
                       "not paid")
            continue
        m = _PROBATION.fullmatch(part)
        if m:
            out.append(f"the day was this identity's first full day after registration (probation): it was paid "
                       f"{m.group(1)} percent of its gross")
            continue
        out.append(next((plain for said, plain in REASON_PLAIN if said == part), part))
    return "; ".join(out)


def pay_days_view(ranked: list) -> list:
    """The identity's ranked days, oldest first, each with its figures and its reason in plain words. Points
    are the pay: one testnet DNDR the rankings paid is one point (the public my-miner page reads them the
    same way), so the season total is the caller's season_paid_udndr."""
    return [{"day": r["day"], "listed": r["listed"], "presence": r["presence"],
             "verified_requests": r["verified_requests"], "verdicts": r["verdicts"],
             "gross_udndr": r["gross_udndr"], "payable_udndr": r["payable_udndr"], "paid_udndr": r["paid_udndr"],
             "reason": r["reason"], "reason_plain": reason_plain_text(r["reason"], r["listed"]),
             "rules_differ": r["rules_differ"]} for r in ranked]


# --- one row per card and identity ---
# What the identity serves, read from its own .env the way gpu_setting reads slot 0's: the kit's own
# engine on the card, or the CPU instance with the judge model (deploy/join.sh::write_cpu_judge_override).
def served_model(env: dict) -> dict:
    ep, m = env.get("DENDRA_SERVE_ENDPOINT", ""), env.get("DENDRA_SERVE_MODEL_ID", "")
    if not ep and not m:
        return {"where": "card", "model": env.get("DENDRA_MODEL_ID", "") or None, "why": ""}
    jm = env.get("DENDRA_JUDGE_MODEL_ID", "")
    if ep == CPU_ENGINE and m and m == jm:
        return {"where": "cpu", "model": m, "why": ""}
    return {"where": None, "model": m or None,
            "why": "the .env names a serving engine or model this application does not read"}


# Days are published in order, each once final, so the latest is found by walking down from the day before
# the status's; a handful of tries covers a season that is a few days behind its status.
LATEST_RANK_TRIES = 8
_CARD_FIGURES = ("presence", "verified_requests", "verdicts", "gross_udndr", "payable_udndr", "paid_udndr", "rank")


def latest_ranking(env: dict) -> tuple:
    """(day, rows, why): the machine's latest published ranking, read once and shared by every identity's row.
    `rows` keeps what a card row shows, by miner_id (final_season_calc.to_json); a figure that is not an
    integer is left out, and the row is then shown with what is there."""
    base = env.get("DENDRA_FINAL_SEASON_URL", "").rstrip("/")
    if not base:
        return None, {}, "no Final Testnet Season address in this kit's settings"
    st = _get_json(f"{base}/status")
    day = st.get("day") if isinstance(st, dict) else None
    if type(day) is not int or isinstance(day, bool) or day < 1:
        return None, {}, "the season's status gives no ranked day yet"
    for d in range(day - 1, max(-1, day - LATEST_RANK_TRIES), -1):
        code, body = _fetch(f"{base}/ranking/day-{d:03d}.json")
        if code == 404:
            continue                        # no day file there: the day is not ranked, the ones below may be
        if code != 200:
            return None, {}, f"the ranking of day {d} was not read (HTTP {code})"
        try:
            doc = json.loads(body)
        except ValueError:
            return None, {}, f"the ranking of day {d} is not JSON"
        rows = {}
        for x in (doc.get("ranking") if isinstance(doc, dict) else None) or []:
            if isinstance(x, dict) and isinstance(x.get("miner_id"), str):
                rows[x["miner_id"]] = {k: x[k] for k in _CARD_FIGURES + ("reason",)
                                       if k in x and (k == "reason" or type(x[k]) is int)}
                rows[x["miner_id"]]["reason_plain"] = reason_plain_text(str(x.get("reason") or ""), True)
        return d, rows, ""
    return None, {}, "no day is ranked yet"


def cards_route() -> tuple:
    """(HTTP status, body) of one row per card and identity of this machine: what it serves, its judge's
    memory, its work and pay of the latest ranked day, its judge and its last check. The judge's memory is
    one read inside each identity's container, so the page asks when the Cards section is on screen."""
    env0 = kit_env()
    if not env0:
        return 200, {"installed": False, "rows": None, "error": ""}
    rows, serr = slots()
    if rows is None:
        return 200, {"installed": True, "rows": None, "error": serr}
    day, ranked, why = latest_ranking(env0)
    out = []
    for r in rows:
        env = env0 if not r["slot"] else read_env(os.path.join(r.get("dir") or "", ".env"))
        jm = judge_memory_view(env, r["slot"])
        hv = health_view(r["slot"], r.get("dir"))
        work = ranked.get(r["miner_id"]) if r["miner_id"] else None
        out.append({"slot": r["slot"], "state": r["state"], "uuid": r["uuid"], "miner_id": r["miner_id"],
                    "served": served_model(env),
                    "memory": {k: jm.get(k) for k in ("state", "why", "mode", "footprint_mb", "mem_available_mb",
                                                      "mem_total_mb")},
                    "judge": ((hv.get("judge_role") or {}).get("word") if hv.get("state") == "read" else None),
                    "health": {k: hv.get(k) for k in ("state", "rc", "age_s")},
                    "work": work, "day": day})
    return 200, {"installed": True, "day": day, "rows": out, "error": "", "ranking_why": why}


# --- alerts: a desktop notification when something blocks the pay ---
# What blocks the pay of one identity, each with one sentence: the five readings the season's own rules
# turn into zero pay (final_season_calc.compute_day) plus a miner that does no work. A fact that was not
# read is no blocker.
def pay_blockers(f: dict) -> list:
    out = []
    miner = f.get("miner") or {}
    if miner.get("running") is False:
        out.append({"id": "stopped", "says": f"The miner is not running ({miner.get('word') or 'stopped'}): a miner "
                                             "that does no work is paid nothing."})
    c8 = _report_check(f, "C8")
    cap = f.get("capacity") or {}
    cv = capacity_verdict(f)               # the dashboard card's rule: a round that was not read blocks nothing
    if cv["blocks"] and c8 and c8.get("state") == "ko":
        out.append({"id": "capacity", "says": "The season pays nothing to an identity without a signed capacity "
                    "report (" + (c8.get("measured") or "not measured") + ")."})
    elif cv["blocks"]:
        out.append({"id": "capacity", "says": f"The signed capacity report did not go out ({cap.get('outcome')}): the "
                                              "season pays nothing to an identity without one."})
    if kit_floor_state(f.get("floor"), f.get("now") or time.time())[0] == "refused":
        fields = _kit_fields(f)
        out.append({"id": "kit_refused", "says": f"The network refuses this kit since {fields['deadline']}: the "
                                                 "season stops paying it."})
    c3 = _report_check(f, "C3")
    if c3 and c3.get("state") == "ko":
        out.append({"id": "registration", "says": "The chain records no registration for this identity ("
                    + (c3.get("measured") or "not registered") + "): the season ranks registered identities only."})
    pv = f.get("payout") or {}
    if f.get("season_enabled") and pv.get("declared") == "" and pv.get("to_machine_key") is not None:
        out.append({"id": "payout", "says": "The programme has no payout address on record for this identity: a day "
                                            "with work is ranked with nothing payable."})
    return out


NOTIFY_BIN = "notify-send"
_NOTIFY_LOCK = threading.Lock()
_ALERT_STATE = {"notified": {}, "last_checked_at": None, "last_notified_at": None,
                "last_notified_says": "", "last_failure": ""}


def _notify(title: str, body: str) -> tuple:
    """(shown, why not) of one desktop notification, through the machine's notifier. It runs nothing else."""
    bin_path = shutil.which(NOTIFY_BIN)
    if not bin_path:
        return False, ("no desktop notifier on this machine: install the libnotify sender (notify-send) to receive "
                       "it; the blockers stay listed on the Alerts card")
    # The notifier reads the body as markup, and some desktops make a link in it clickable: a value the miner container
    # wrote (the capacity round's outcome) is shown as text.
    body = html.escape(body, quote=False)
    try:
        r = subprocess.run([bin_path, "-a", "Dendra", "-u", "critical", title, body], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"the notifier did not run ({type(e).__name__})"
    if r.returncode != 0:
        return False, ((r.stderr or r.stdout or "").strip()[-200:] or f"exit {r.returncode}")
    return True, ""


def alerts_enabled() -> bool:
    return settings().get("alerts", True) is not False


def alerts_view(blockers: list, slot: int) -> dict:
    with _NOTIFY_LOCK:
        state = {k: (dict(v) if k == "notified" else v) for k, v in _ALERT_STATE.items()}
    return {"enabled": alerts_enabled(), "notifier": bool(shutil.which(NOTIFY_BIN)), "blockers": blockers,
            "slot": slot, "last_checked_at": state["last_checked_at"],
            "last_notified_at": state["last_notified_at"], "last_notified_says": state["last_notified_says"],
            "last_failure": state["last_failure"]}


def alerts_tick(slot: int, blockers: list) -> None:
    """The desktop notification, once per blocker until it clears. It fires when the facts are read (the
    page polls the overview), so an application that runs with no page open sends none; the Alerts card
    says when the last check ran. A blocker is marked before the send, and unmarked on a failure that a
    later read can repair; with no notifier on the machine it stays marked, and the card says why."""
    with _NOTIFY_LOCK:
        _ALERT_STATE["last_checked_at"] = int(time.time())
        if not blockers or not alerts_enabled():
            for key in [key for key in _ALERT_STATE["notified"] if key[0] == slot]:
                del _ALERT_STATE["notified"][key]
            return
        fresh = [b for b in blockers if not _ALERT_STATE["notified"].get((slot, b["id"]))]
        for b in fresh:
            _ALERT_STATE["notified"][(slot, b["id"])] = True
    if not fresh:
        return
    title = f"Dendra: the season pay of slot {slot} is blocked"
    body = " ".join(b["says"] for b in fresh)[:800]
    shown, why = _notify(title, body)
    with _NOTIFY_LOCK:
        if shown:
            _ALERT_STATE.update(last_notified_at=int(time.time()), last_notified_says=body, last_failure="")
        else:
            _ALERT_STATE["last_failure"] = why
            if not why.startswith("no desktop notifier"):
                for b in fresh:
                    _ALERT_STATE["notified"].pop((slot, b["id"]), None)


def alerts_route(slot: int) -> tuple:
    """(HTTP status, body) of the Alerts card: what blocks the pay of one identity now, read from its facts.
    The reading sends no notification: the overview read does that, once per new blocker."""
    env0 = kit_env()
    if not env0:
        return 200, {"installed": False}
    rows, _ = slots()
    row = next((r for r in rows or [] if r["slot"] == slot), {"slot": slot, "dir": KIT})
    env = env0 if not slot else read_env(os.path.join(row.get("dir") or "", ".env"))
    ident = _for(miner_identity, slot)
    st = _for(keys_status, slot)
    hv = health_view(slot, row.get("dir"))
    crows, derr = _for(containers, slot)
    miner = next((c for c in crows or [] if c["service"] == "miner"), None)
    kit, floor = _for(kit_reading, slot)
    facts = {"now": time.time(), "docker": derr,
             "miner": {"running": None if crows is None else bool(miner and miner["state"] == "running"),
                       "word": (miner["state"] if miner else "no miner container") if crows is not None else ""},
             "report": newest_report(hv, slot), "schedule": hv, "role": role_view(env, hv), "kit": kit,
             "floor": floor, "phrase": phrase_fact(st), "update": update_view(env0)["instruction"],
             "capacity": _for(capacity_round_view, slot), "jmem": {}, "payout": payout_view(env, st, ident),
             "work": None, "season_enabled": bool(env.get("DENDRA_FINAL_SEASON_URL", "").strip())}
    return 200, alerts_view(pay_blockers(facts), slot)


# --- the containers' log, explained ---
# One table says what a line means, from the producers' own words (miner.py, judge_worker.py,
# reveal_worker.py, modea/memwatch.py, capacity_sender.line_of). A line no rule reads is left with its raw
# text and no sentence: nothing is guessed from a word in it.
LOG_RULES = (
    (re.compile(r"^\[daemon\] FATAL:"), "red",
     "The miner's main process stopped on a fatal error: it does no work until it is started again."),
    (re.compile(r"^\[daemon\].*clock is about [0-9]+ s "), "amber",
     "This machine's clock and the chain's disagree by seconds, and requests answered at the wrong time are "
     "refused: let the system clock follow a time service."),
    (re.compile(r"^\[daemon\].*availability proof refused"), "amber",
     "The chain refused one availability proof: that window's presence is not counted."),
    (re.compile(r"^\[daemon\].*availability not proven"), "amber",
     "One availability window was not proven: that window's presence reward is lost, and the work draw weighs "
     "this identity less."),
    (re.compile(r"^\[daemon\].*availability challenge unreadable"), "amber",
     "One availability challenge could not be read from the chain: that window is not proven."),
    (re.compile(r"^\[daemon\].*Ollama unreachable"), "red",
     "The engine that serves the model did not answer: the miner answers nothing until it is back (the Miner "
     "card's Restart brings the containers up again)."),
    (re.compile(r"^\[daemon\].*judge declared on chain"), "info",
     "The daemon declared this identity's judge on chain: the chain draws it into juries again."),
    (re.compile(r"^\[daemon\].*judge declaration withdrawn"), "info",
     "The daemon withdrew this identity's judge declaration: the chain stops drawing it into juries."),
    (re.compile(r"^\[daemon\].*declare-judge .* not confirmed"), "amber",
     "The judge's declaration was not confirmed by the chain; the daemon reads the seat again and retries by itself."),
    (re.compile(r"^\[judge\] FATAL:"), "red",
     "The judge worker stopped on a fatal error: it gives no verdict until the miner is restarted, and the chain "
     "draws this identity into juries until its missed draws reach judge_missed_draws_max or neither its last "
     "verdict nor its declaration is within judge_verdict_window_blocks."),
    (re.compile(r"^\[judge\] WARNING:"), "amber",
     "The judge worker warns of a condition that can cost verdicts; the Judge line of the overview says what the "
     "seat decided."),
    (re.compile(r"^\[judge\].*list-job unreadable"), "amber",
     "One round of the judge could not read its work list; it reads again next round."),
    (re.compile(r"^\[reveal\] FATAL:"), "red",
     "The reveal worker stopped on a fatal error: disputed jobs are not revealed until the miner is restarted."),
    (re.compile(r"^\[reveal\].*reveal (incomplete|failed)"), "amber",
     "A disputed job could not be revealed in this round; the worker keeps it and retries."),
    (re.compile(r"^\[reveal\].* revealed "), "info",
     "A disputed job was revealed to the anchored jurors."),
    (re.compile(r"^\[memwatch\] judge model unloaded"), "info",
     "The memory guard unloaded the judge model to protect the machine's RAM; it loads again when there is room."),
    (re.compile(r"^\[memwatch\] WARNING"), "amber",
     "The memory watch warns of a condition around the judge's memory; the Judge line of the overview says what "
     "was decided."),
    (re.compile(r"^sent: "), "info",
     "The identity's signed capacity report was sent to the registry: the season can pay its ranked days."),
    (re.compile(r"^not sent \("), "red",
     "The signed capacity report did not go out: the season pays nothing to an identity without one. The line "
     "names the outcome and when the sender tries again."),
)
_LOG_PREFIX = re.compile(r"^(?P<svc>[^ |\t]+)\s*\|\s?(?P<rest>.*)$")


def explain_log_line(line: str) -> dict:
    """One log line with its plain sentence when the table reads it; the raw line travels with it always."""
    m = _LOG_PREFIX.match(line)
    svc, rest = (m.group("svc"), m.group("rest")) if m else ("", line)
    for rx, sev, says in LOG_RULES:
        if rx.search(rest):
            return {"service": svc, "raw": line, "severity": sev, "says": says}
    return {"service": svc, "raw": line, "severity": "", "says": ""}


def logs_explained_route(slot: int) -> tuple:
    """(HTTP status, body): the containers' last log lines, each with its sentence, and the raw text of the
    whole tail for the one-click view."""
    rc, out = compose("logs", "--no-color", "--tail", "400", timeout=90, slot=slot)
    if rc != 0:
        # A refusal of compose is not a log line: the answer carries it once, as the reason the log was not read.
        why = ("Docker is not installed on this machine" if rc == 127 and out.startswith("docker is not installed")
               else docker_error(out))
        return 200, {"rc": rc, "slot": slot, "lines": [], "error": why, "text": out[-60000:]}
    return 200, {"rc": rc, "slot": slot, "lines": [explain_log_line(ln) for ln in out.splitlines()],
                 "text": out[-60000:], "error": ""}


# --- one action at a time on this kit ---
# Two copies of this application can run on one kit (one started from Windows, one from Linux). Each action
# takes this file lock beside the in-process one, so their `docker compose` calls never run against each other.
# The holder writes which action it runs and on which identities, for the refusal and the Stop of the other.
# judge.sh and join.sh run by hand do not take it. The file is in this user's configuration directory, named after
# the kit's real path, and never in the kit, which is in the clone (build_public_repo.sh copies the working tree).
def action_lock_path() -> str:
    return os.path.join(CONFIG_DIR, "action-" + hashlib.sha256(os.path.realpath(KIT).encode()).hexdigest()[:16] + ".lock")



ACTION_WORDS = {"/api/start": "Start", "/api/stop": "Stop", "/api/restart": "Restart",
                "/api/recovery/confirm": "the recovery confirmation", "/api/health/run": "a health check",
                "/api/machine-test": "Test my machine", "/api/payout": "the payout declaration",
                "/api/reachability/run": "the reachability check", "/api/alerts": "the alerts setting",
                "/api/judge": "the Judge switch", "/api/gpu": "the GPU mining switch"}


def busy_words(path, since, here: bool) -> str:
    """The refusal of an action while another runs: which one, since when, and where."""
    what = ACTION_WORDS.get(path, "an action")
    when = f", started {age_words(time.time() - since)} ago" if type(since) is int else ""
    where = "" if here else ", in another copy of this application on this kit"
    long = (f"; the Judge and GPU switches can take up to {SWITCH_TIMEOUT_S // 3600} h and Test my machine up to "
            f"{MT_DEADLINE_S // 60} min" if path in ("/api/judge", "/api/gpu", "/api/machine-test") else "")
    return f"another action is already running ({what}{when}{where}): this one can start once it ends{long}"


def action_slots(path: str, doc: dict) -> tuple:
    """([k, ...], None) or (None, (HTTP status, body)): the identities the action `path` acts on, named before it
    takes the locks. Stop stops them whatever identity its own request names, since the page may show another one
    by then. Both switches act on slot 0's kit; the other actions run no compose of their own."""
    if path in ("/api/start", "/api/restart"):
        return resolve_slots(doc.get("slot"), allow_all=True)
    if path in ("/api/machine-test", "/api/health/run"):
        return resolve_slots(doc.get("slot"))
    if path in ("/api/judge", "/api/gpu"):
        return [0], None
    return [], None


def slot_list(value) -> list:
    """The identities an action recorded (App._start_action, kit_lock_try), as slot numbers. A holder from before
    the record writes none, and its Stop then stops the identities the Stop names, as it did."""
    if not isinstance(value, list):
        return []
    return sorted({k for k in value if type(k) is int and k >= 0})


def kit_lock_try(path: str, ks: list) -> tuple:
    """("held", fd or None) once this process holds the kit's action lock (None where the system has no file
    locks), ("busy", what the holder wrote) when another process holds it, ("error", why) when it cannot be opened.
    The holder writes its action, its start, its pid and the identities `ks` it acts on."""
    try:
        import fcntl
    except ImportError:
        return "held", None
    lock = action_lock_path()
    try:
        private_config_dir()
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        return "error", f"the kit's action lock {lock} could not be opened ({type(e).__name__})"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            doc = json.loads(os.read(fd, 4096) or b"{}")
        except (OSError, ValueError):
            doc = {}
        os.close(fd)
        return "busy", (doc if isinstance(doc, dict) else {})
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"path": path, "since": int(time.time()), "pid": os.getpid(),
                                  "slots": list(ks)}).encode(), 0)
    except OSError:
        pass                               # the lock is held; only the words of another's refusal are lost
    return "held", fd


def kit_action_lock(path: str, ks: list) -> tuple:
    """(True, fd or None) once this process holds the kit's action lock, (False, (HTTP status, body)) when
    another process holds it or the lock cannot be opened."""
    state, got = kit_lock_try(path, ks)
    if state == "held":
        return True, got
    if state == "error":
        return False, (503, {"error": got})
    return False, (409, {"error": busy_words(got.get("path"), got.get("since"), here=False),
                         "running": got.get("path") if isinstance(got.get("path"), str) else None})


# Stop in one copy of this application asks the copy that holds the kit's lock to interrupt its action: it writes
# that copy's process id here, and the holder's run_child reads it while its subprocess runs. The file is in the
# same directory as the lock, which only this user can enter.
def stop_request_path() -> str:
    return action_lock_path() + ".stop"


def ask_other_copy_to_stop(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    path = stop_request_path()
    try:
        private_config_dir()
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": pid, "at": time.time()}))
        os.replace(path + ".tmp", path)
    except OSError:
        return False
    return True


def stop_asked_of_me(since: float) -> bool:
    """True when another copy's Stop named this process after `since` (the start of the action that runs)."""
    try:
        with open(stop_request_path(), encoding="utf-8") as f:
            doc = json.loads(f.read(4096))
    except (OSError, ValueError):
        return False
    return (isinstance(doc, dict) and doc.get("pid") == os.getpid() and type(doc.get("at")) in (int, float)
            and doc["at"] >= since)


def forget_stop_request() -> None:
    try:
        os.remove(stop_request_path())
    except OSError:
        pass


def kit_action_unlock(fd) -> None:
    if fd is None:
        return
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    os.close(fd)                           # closing the descriptor releases the lock


def _print_line(line: str) -> None:
    print(line, flush=True)


class App:
    def __init__(self, port: int):
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.challenge = None          # (address of the phrase shown, positions asked)
        self.launch_file = None        # the browser's access file, until /handoff spends its one-time token
        self.once = None               # the one-time token of the access page or window, until it is used
        self.lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.action_running = None     # {"path", "since", "slots"} of the action this process runs, while it runs
        self.action_thread = None      # the thread that runs it: only its subprocesses are interrupted by Stop
        self.action_since = 0.0        # time.time() at its start, for a stop request from another copy
        self.stop_asked = False        # Stop asked this process's action to end
        self.stop_pending = 0          # how many Stops have not ended yet: no other action starts meanwhile
        self.children = {}             # pid -> Popen of the action's running subprocess
        self.tab_key = secrets.token_urlsafe(32)   # the session's other half, sent by the page (SESSION_HEADER)
        self.instance = secrets.token_urlsafe(32)  # sent with every answer to the session, never asked of it
        self.codes = {}                # purpose -> {"value", "target", "until", "tries"} of its code, until it goes
        self.code_sinks = [_print_line]  # where a code is shown: the terminal, and the native window when there is one
        self.code_refused = ""         # why no code can be shown at all (NO_CODE_SINK), or ""

    def runs_action_here(self) -> bool:
        return self.action_thread is not None and self.action_thread == threading.get_ident()

    def stop_requested(self) -> bool:
        return self.stop_asked or stop_asked_of_me(self.action_since)

    def _start_action(self, path: str, since: float, ks: list) -> None:
        self.action_running = {"path": path, "since": int(time.time()), "slots": list(ks)}
        self.action_since = since
        self.action_thread = threading.get_ident()
        self.stop_asked = False

    def take_action(self, path: str, ks: list) -> tuple:
        """(True, handle) when this process may run the action `path` on the identities `ks` (action_slots), (False,
        (HTTP status, body)) when it may not. One action at a time in this process (`action_lock`) and on this kit
        (`kit_action_lock`, which another copy of this application takes too). The refusal names the action that
        runs. Stop goes through take_stop instead, and while a Stop waits no other action starts."""
        if self.stop_pending:
            return False, (409, {"error": busy_words("/api/stop", None, here=True), "running": "/api/stop"})
        if not self.action_lock.acquire(blocking=False):
            run = self.action_running or {}
            return False, (409, {"error": busy_words(run.get("path"), run.get("since"), here=True),
                                 "running": run.get("path")})
        # Read before the kit's lock is taken: another copy's Stop that finds the lock held writes its request after
        # this instant, so the request counts for this action (stop_asked_of_me).
        since = time.time()
        try:
            ok, held = kit_action_lock(path, ks)
        except BaseException:
            self.action_lock.release()
            raise
        if not ok:
            self.action_lock.release()
            return False, held
        # A Stop that arrived between the check above and the locks found this action not yet started: it asked
        # it to end (stop_asked), and starting now would clear that. The action gives way, and Stop runs first.
        with self.lock:
            stopping = self.stop_pending > 0
            if not stopping:
                self._start_action(path, since, ks)
        if stopping:
            kit_action_unlock(held)
            self.action_lock.release()
            return False, (409, {"error": busy_words("/api/stop", None, here=True), "running": "/api/stop"})
        return True, held

    def release_action(self, held) -> None:
        self.action_running = None
        self.action_thread = None
        kit_action_unlock(held)
        self.action_lock.release()

    def take_stop(self) -> dict:
        """Stop is never refused for another action. It asks the action that runs, in this process or in another
        copy on this kit, to end (run_child terminates its subprocess, then kills it after STOP_GRACE_S), waits at
        most STOP_WAIT_S for the locks, and stops the miner beside anything still running past that. Returns what
        release_stop needs and what the answer says: {"local", "held", "interrupted", "where", "beside", "slots"},
        where "slots" are the identities the interrupted action acts on, which Stop stops too. Every call is followed
        by one release_stop: until then no other action starts here, so an action that ends after a Stop that went
        beside it never runs its compose against Stop's own."""
        out = {"local": False, "held": None, "interrupted": None, "where": None, "beside": "", "slots": []}
        with self.lock:
            self.stop_pending += 1
        try:
            if not self.action_lock.acquire(blocking=False):
                with self.lock:
                    run = self.action_running or {}
                    if run.get("path") != "/api/stop":
                        # An action that holds the lock but has not started yet (take_action) gives way when it
                        # sees stop_pending, so only one that runs is named as interrupted.
                        if run.get("path"):
                            out.update(interrupted=ACTION_WORDS.get(run.get("path"), "an action"), where="here",
                                       slots=slot_list(run.get("slots")))
                        self.stop_asked = True
                if not self.action_lock.acquire(timeout=STOP_WAIT_S):
                    held_by = out["interrupted"] or ("another Stop" if run.get("path") == "/api/stop" else "an action")
                    out["beside"] = (f"{held_by} had not ended after {STOP_WAIT_S} s: "
                                     "the miner was stopped beside it")
                    return out
            out["local"] = True
            with self.lock:
                self._start_action("/api/stop", time.time(), [])
            deadline = time.monotonic() + STOP_WAIT_S
            while True:
                state, got = kit_lock_try("/api/stop", [])
                if state == "held":
                    out["held"] = got
                    forget_stop_request()
                    return out
                if state == "error":
                    out["beside"] = got + ": the miner was stopped without it"
                    return out
                # Asked at every look, of the pid read then: the holder writes it just after its flock, so a first
                # look can find none, and a request is read only against the action running when it was written.
                if got.get("path") != "/api/stop":
                    if got.get("path") or out["interrupted"] is None:
                        out.update(interrupted=ACTION_WORDS.get(got.get("path"), "an action"), where="another copy")
                    # Added to those of an action here that ended first: Stop stops both.
                    out["slots"] = sorted(set(out["slots"]) | set(slot_list(got.get("slots"))))
                    ask_other_copy_to_stop(got.get("pid"))
                if time.monotonic() >= deadline:
                    out["beside"] = (f"the action of another copy of this application on this kit had not ended after "
                                     f"{STOP_WAIT_S} s: the miner was stopped beside it")
                    return out
                time.sleep(0.2)
        except BaseException:
            with self.lock:
                self.stop_pending -= 1
            raise

    def release_stop(self, got: dict) -> None:
        try:
            if got["local"]:
                self.release_action(got["held"])
        finally:
            with self.lock:
                self.stop_pending -= 1

    def issue_code(self, purpose: str, target: tuple, words: str) -> None:
        """A new confirmation code for `purpose`, given for `target` (the payout's address and slots, or the slot
        whose words are shown) and replacing only the earlier code of that purpose. Every sink (the terminal or
        window that started this application) shows it beside `words`, the target in plain words, so the owner
        reads what the code confirms. It is never returned to the caller."""
        value = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        with self.lock:
            self.codes[purpose] = {"value": value, "target": target, "until": time.monotonic() + CODE_SECONDS,
                                   "tries": 0}
            sinks = list(self.code_sinks)
        line = (f"Dendra confirmation code for {CODE_PURPOSES[purpose]}{words}: {value[:4]}-{value[4:]} (it works "
                f"once, for {CODE_SECONDS // 60} minutes; type it in the Dendra page)")
        for sink in sinks:
            try:
                sink(line)
            except Exception:  # noqa: BLE001 -- a window that is gone must not keep the code from the terminal
                pass

    def spend_code(self, given, purpose: str, target: tuple) -> str:
        """"" when `given` is the code of `purpose`, in time and given for `target`: the code is then used. Otherwise
        why not: "none" (no code of that purpose waits), "expired", "target" (the code of another change) or "wrong".
        A refused try counts against that code alone, which goes at its CODE_TRIES-th, so it is guessed at most
        CODE_TRIES times and a try for the other purpose never touches it. Compared as bytes in constant time."""
        got = re.sub("[ -]", "", given).upper().encode("latin-1", "replace")
        with self.lock:
            code = self.codes.get(purpose)
            if code is None:
                return "none"
            if time.monotonic() >= code["until"]:
                del self.codes[purpose]
                return "expired"
            same = hmac.compare_digest(got, code["value"].encode("ascii"))
            if same and code["target"] == target:
                del self.codes[purpose]
                return ""
            code["tries"] += 1
            if code["tries"] >= CODE_TRIES:
                del self.codes[purpose]
            return "target" if same else "wrong"

    def take_once(self, got: bytes) -> bool:
        """True for the one-time token, once: it is spent by the request that shows it, under the lock, so
        two requests that race with it cannot both get in."""
        with self.lock:
            ok = self.once is not None and hmac.compare_digest(got, self.once.encode())
            if ok:
                self.once = None
        return ok

    def proof_of(self, nonce: str):
        """HMAC-SHA256(one-time token, HANDOFF_PROOF_LABEL + nonce) in hex, or None once the token is spent (or in a
        launch that has none). It neither spends the token nor shows it."""
        with self.lock:
            once = self.once
        if once is None:
            return None
        return hmac.new(once.encode(), HANDOFF_PROOF_LABEL + nonce.encode(), hashlib.sha256).hexdigest()

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


def access_page(port: int, once: str) -> str:
    """The page that posts the one-time token to /handoff, the form open_windows.ps1 writes for Windows: the token
    is in the body of a POST, never in a URL that the browser's history keeps."""
    if not HANDOFF_TOKEN.fullmatch(once):
        raise ValueError("the one-time token is not URL-safe base64")
    return ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Dendra</title>'
            f'<form id="f" method="post" action="http://127.0.0.1:{port}/handoff">'
            f'<input type="hidden" name="t" value="{once}">'
            '<p>Dendra opens in this tab. If it does not, press <button type="submit">Open Dendra</button>.</p></form>'
            '<script>document.getElementById("f").submit()</script>')


def write_launch_file(port: int, once: str) -> str:
    """The access page, in a file only this user can read (mode 0600, in a 0700 directory). The browser is given
    the PATH of this file, so no token is on a command line; it holds the one-time token, never the session's."""
    fd, path = tempfile.mkstemp(prefix="open-", suffix=".html", dir=launch_dir())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(access_page(port, once))
    os.chmod(path, 0o600)
    return path


APP: App | None = None
UI = os.path.join(HERE, "ui", "index.html")
_SCRIPT_BLOCK = re.compile(rb"<script>(.*?)</script>", re.S)


def page_csp(page: bytes) -> str:
    """The Content-Security-Policy of a page this server sends: its own inline scripts run, by their hash, and
    nothing else; it may call this server only, and no other page may frame it. The session cookie is HttpOnly,
    so no script reads it, but a script that got in could still call the API with it."""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s).digest()).decode("ascii") + "'"
                      for s in _SCRIPT_BLOCK.findall(page))
    scripts = hashes or "'none'"
    return (f"default-src 'none'; script-src {scripts}; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def code_target(purpose: str, doc: dict) -> tuple:
    """(target, words, None) for the change a confirmation code is asked for, read as the route that spends the
    code reads it: the payout's address and its slot or `all` (Handler._payout), or the slot whose words are shown
    (GET /api/recovery). `target` is (address, slots) and `words` names them for the line beside the code.
    (None, "", (HTTP status, body)) when that route would refuse them, so no code is printed for them."""
    addr = ""
    if purpose == "payout":
        addr = str(doc.get("address", "")).strip()
        if not ADDRESS.match(addr):
            return None, "", (400, {"error": "type the dendra1... address first: the code is given for that address"})
    ks, err = resolve_slots(doc.get("slot"), allow_all=purpose == "payout")
    if err:
        return None, "", err
    if doc.get("slot") == "all":
        words = " of every identity (slots " + ", ".join(str(k) for k in ks) + ")"
    elif doc.get("slot") in (None, ""):
        words = ""                     # a kit that runs one identity
    else:
        words = f" of slot {ks[0]}"
    return (addr, tuple(ks)), words + (f" to {addr}" if addr else ""), None


def stdout_is_terminal() -> bool:
    try:
        return sys.stdout is not None and sys.stdout.isatty()
    except (OSError, ValueError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "dendra-app"

    def log_message(self, fmt, *args):
        return

    # While an action holds its locks its answer waits here, and leaves once they are released (`_held`): a
    # client that reads the answer and calls again at once finds the action ended, never a 409 for it.
    _held_reply = None
    # Set by _auth: the answers to this request carry INSTANCE_HEADER.
    _authed = False

    def _held(self, run, release):
        self._held_reply = []
        try:
            run()
        finally:
            replies, self._held_reply = self._held_reply, None
            release()
        for reply in replies[:1]:
            self._send(*reply)

    def _send(self, code, obj, ctype="application/json", extra=()):
        if self._held_reply is not None:
            self._held_reply.append((code, obj, ctype, extra))
            return None
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", page_csp(body))
        if self._authed:
            self.send_header(INSTANCE_HEADER, APP.instance)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{APP.port}", f"localhost:{APP.port}")

    def _auth(self) -> bool:
        """The session: its cookie and the header the page sends from its tab's sessionStorage, both required, each
        compared as bytes in constant time (compare_digest refuses a str that is not ASCII, and a header is whatever
        the client sent: http.server decodes it as latin-1). The cookie alone is not enough, since a browser sends
        it to every port of 127.0.0.1. A request a browser marks as coming from another site is refused even with
        both."""
        self._authed = False
        if not self._host_ok():
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site != "same-origin":
            return False
        # A browser names the page that sent a request: when it does, that page is this application's own.
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + self.headers.get("Host", ""):
            return False
        name = SESSION_COOKIE + str(APP.port)
        want = APP.token.encode("ascii")
        ok = False
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                k, sep, v = part.strip().partition("=")
                if sep and k == name:
                    ok = hmac.compare_digest(v.strip().encode("latin-1", "replace"), want) or ok
        tab = (self.headers.get(SESSION_HEADER) or "").strip().encode("latin-1", "replace")
        self._authed = hmac.compare_digest(tab, APP.tab_key.encode("ascii")) and ok
        return self._authed

    def _content_length(self, most: int):
        """The body's length, or None when the header is not a number in 0..most."""
        raw = self.headers.get("Content-Length") or "0"
        if not raw.isascii() or not raw.isdigit():
            return None
        n = int(raw)
        return n if n <= most else None

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(UI, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if not self._auth():
            return self._send(401, {"error": "token"})
        # ?slot=k names one identity of a machine that runs one per card (deploy/join.sh --gpus).
        qslot = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("slot") or [None])[0]
        if path == "/api/overview":
            return self._send(*self._overview(qslot))
        if path == "/api/reachability":
            return self._send(200, reachability_view())
        if path in ("/api/recovery", "/api/logs", "/api/health"):
            ks, err = resolve_slots(qslot)
            if err:
                return self._send(*err)
            k = ks[0]
            if path == "/api/recovery":
                refused = self._code_refusal(self.headers.get(CODE_HEADER), "recovery", ("", (k,)))
                if refused:
                    return self._send(*refused)
                return self._recovery(k)
            if path == "/api/logs":
                rc, out = compose("logs", "--no-color", "--tail", "200", "miner", timeout=60, slot=k)
                return self._send(200, {"rc": rc, "text": out[-60000:], "slot": k})
            return self._send(200, health_view(k, slot_row(k).get("dir")))
        if path == "/api/cards":
            return self._send(*cards_route())
        if path == "/api/alerts":
            ks, err = resolve_slots(qslot)
            if err:
                return self._send(*err)
            return self._send(*alerts_route(ks[0]))
        if path == "/api/logs/explained":
            ks, err = resolve_slots(qslot)
            if err:
                return self._send(*err)
            return self._send(*logs_explained_route(ks[0]))
        return self._send(404, {"error": "route"})

    def _handoff(self):
        """The way in of every launch: the access page (a file on Linux, open_windows.ps1's page on Windows, a page
        loaded in-process in the native window) posts the one-time token as a form field, and the answer sets the
        session cookie, then sends the tab to the interface. The session token is in no URL, no page body and no
        storage a script reads. The answer's own script leaves the session's other half (SESSION_HEADER) and the
        instance value (INSTANCE_HEADER) in that tab's sessionStorage, which only a page of this port can read; the
        page's policy runs that script by its hash. The one-time token is spent here, and the access page goes with
        it."""
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        n = self._content_length(4096)
        if n is None:
            return self._send(400, {"error": "the Content-Length header is not a number from 0 to 4096"})
        if n == 0:
            return self._send(401, {"error": "token"})
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("ascii", "replace"))
        got = form.get("t") or []
        if len(got) != 1 or not APP.take_once(got[0].encode()):
            return self._send(401, {"error": "token"})
        APP.forget_launch_file()
        cookie = f"{SESSION_COOKIE}{APP.port}={APP.token}; HttpOnly; SameSite=Strict; Path=/"
        # Both values are URL-safe base64, so the JSON literal holds no quote or tag. The script runs as it is
        # parsed, before the refresh below moves the tab.
        keep = json.dumps({"dendra-session": APP.tab_key, "dendra-instance": APP.instance})
        page = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Dendra</title>'
                f'<script>try {{ const k = {keep}; for (const n in k) sessionStorage.setItem(n, k[n]); }} catch (e) {{}}'
                '</script><meta http-equiv="refresh" content="0;url=/"><p>Opening Dendra. If nothing happens, '
                '<a href="/">open it here</a>.</p>')
        return self._send(200, page.encode(), "text/html; charset=utf-8", extra=(("Set-Cookie", cookie),))

    def _handoff_proof(self):
        """open_windows.ps1 asks this before it opens the browser: the answer proves that the program on the port
        holds the one-time token, so a program that took the port first never receives the access page. The
        token is neither spent nor shown; once it is spent, the route proves nothing and says so (410)."""
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= 256:
            return self._send(400, {"error": "nonce"})
        got = urllib.parse.parse_qs(self.rfile.read(n).decode("ascii", "replace")).get("n") or []
        if len(got) != 1 or not HANDOFF_NONCE.fullmatch(got[0]):
            return self._send(400, {"error": "nonce"})
        proof = APP.proof_of(got[0])
        if proof is None:
            return self._send(410, {"error": "spent"})
        return self._send(200, {"proof": proof})

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path == "/handoff":
            return self._handoff()
        if urllib.parse.urlsplit(self.path).path == "/handoff/proof":
            return self._handoff_proof()
        if not self._auth():
            return self._send(401, {"error": "token"})
        n = self._content_length(65536)
        if n is None:
            return self._send(400, {"error": "the Content-Length header is not a number from 0 to 65536"})
        # The page posts JSON (ui/index.html, api). A form or a plain-text body is what another page can send
        # without asking the browser first, so it is refused before anything is read.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, {"error": "the body must be sent as application/json"})
        try:
            doc = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "the body is not JSON"})
        if not isinstance(doc, dict):
            return self._send(400, {"error": "the body is not a JSON object"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/code":
            # Not an action: it prints a code and changes nothing, so it never waits behind one.
            purpose = doc.get("for")
            if purpose not in CODE_PURPOSES:
                return self._send(400, {"error": "say for: recovery or for: payout"})
            if APP.code_refused:
                return self._send(409, {"error": APP.code_refused})
            target, words, err = code_target(purpose, doc)
            if err:
                return self._send(*err)
            APP.issue_code(purpose, target, words)
            return self._send(200, {"sent": True, "seconds": CODE_SECONDS,
                                    "where": "the terminal or window that started Dendra"})
        if path == "/api/stop":
            # The identities are named before anything is interrupted: a Stop that would stop no miner (an
            # unknown slot, a list that cannot be read) ends nothing either.
            ks, err = resolve_slots(doc.get("slot"), allow_all=True)
            if err:
                return self._send(*err)
            got = APP.take_stop()
            return self._held(lambda: self._stop(ks, got), lambda: APP.release_stop(got))
        if path not in ACTION_WORDS:
            return self._send(404, {"error": "route"})
        ks, err = action_slots(path, doc)
        if err:
            return self._send(*err)
        ok, held = APP.take_action(path, ks)
        if not ok:
            return self._send(*held)
        return self._held(lambda: self._post_action(path, doc, ks), lambda: APP.release_action(held))

    def _code_refusal(self, given, purpose: str, target: tuple):
        """None when `given` is the code of `purpose` given for `target`, else (HTTP status, body). The refusal
        never repeats the code, given or expected."""
        if not isinstance(given, str) or not given.strip():
            return 403, {"error": f"{CODE_PURPOSES[purpose]} takes a confirmation code: press Get a code, then type "
                                  "the code Dendra printed in the terminal or window that started it",
                         "code": "required"}
        why = APP.spend_code(given, purpose, target)
        if why == "target":
            what = "address or identity" if purpose == "payout" else "identity"
            return 403, {"error": f"that confirmation code was given for another {what} (the line beside the code "
                                  "names it): press Get a code for this one", "code": "refused"}
        if why:
            return 403, {"error": "that confirmation code is wrong, expired, for another purpose or already used: "
                                  "press Get a code for a new one", "code": "refused"}
        return None

    def _stop(self, ks, got):
        """Stop: the miner of the identities `ks` (one, or every active one) and of those the interrupted action
        acts on (got["slots"]), stopped after that action was asked to end. Slot 0 names the judge's profile whatever
        its .env says: judge.sh on starts the judge's engine and pulls its model before it writes
        COMPOSE_PROFILES=judge, and that engine must stop too. After a switch, the model pull, a one-off container,
        is removed as well (remove_judge_pull)."""
        switch = got["interrupted"] in (ACTION_WORDS["/api/judge"], ACTION_WORDS["/api/gpu"])
        results = []
        for k in sorted(set(ks) | set(got["slots"])):
            rc, out = compose(*(("--profile", "judge") if k == 0 else ()), "stop", timeout=600, slot=k)
            if k == 0 and switch:
                prc, pout = remove_judge_pull()
                rc, out = (rc or prc), _NL.join(t for t in (out, pout) if t)
            results.append({"slot": k, "rc": rc, "text": out[-4000:]})
        worst = next((r["rc"] for r in results if r["rc"] != 0), 0)
        text = results[0]["text"] if len(results) == 1 else _NL.join(f"[slot {r['slot']}] {r['text']}" for r in results)
        return self._send(200, {"rc": worst, "text": text[-4000:], "results": results,
                                "interrupted": got["interrupted"], "interrupted_where": got["where"],
                                "beside": got["beside"]})

    def _post_action(self, path, doc, ks):
        """The action `path`, on the identities `ks` that action_slots named before the locks were taken."""
        if path in ("/api/start", "/api/restart"):
            # One identity, or `all` (every active one); a missing slot is slot 0 only on a kit that runs one.
            # Stop is not here: it takes its own way in (`_stop`, `App.take_stop`).
            verb = {"/api/start": ("up", "-d", "--no-build"), "/api/restart": ("restart", "miner")}[path]
            results = []
            for k in ks:
                rc, out = compose(*verb, timeout=600, slot=k)
                results.append({"slot": k, "rc": rc, "text": out[-4000:]})
            worst = next((r["rc"] for r in results if r["rc"] != 0), 0)
            text = results[0]["text"] if len(results) == 1 else "\n".join(f"[slot {r['slot']}] {r['text']}" for r in results)
            return self._send(200, {"rc": worst, "text": text[-4000:], "results": results})
        if path == "/api/recovery/confirm":
            ks, err = resolve_slots(doc.get("slot"))
            if err:
                return self._send(*err)
            return self._recovery_confirm(doc, ks[0])
        if path == "/api/health/run":
            rows, _ = slots()
            # A kit that runs one identity runs the script as it always did; on a machine with several, the
            # identity is named, or the script would check every slot and answer for the machine.
            code, body = run_health(ks[0] if rows is not None and len(rows) > 1 else None)
            return self._send(code, body)
        if path == "/api/machine-test":
            return self._send(*run_machine_test(ks[0]))
        if path == "/api/payout":
            return self._payout(doc)
        if path == "/api/reachability/run":
            if not _REACH_RUN.acquire(blocking=False):
                return self._send(409, {"error": "a reachability check is already running"})
            try:
                return self._send(200, reachability_view(force=True))
            finally:
                _REACH_RUN.release()
        if path == "/api/alerts":
            if type(doc.get("enabled")) is not bool:
                return self._send(400, {"error": "say enabled: true or enabled: false"})
            s = settings()
            s["alerts"] = doc["enabled"]
            save_settings(s)
            return self._send(200, {"enabled": doc["enabled"]})
        if path in ("/api/judge", "/api/gpu"):
            # {"on": true | false}: the position asked for, never a toggle of whatever the kit holds.
            if type(doc.get("on")) is not bool:
                return self._send(400, {"error": "say on: true or on: false"})
            _ks, err = resolve_slots(doc.get("slot"))
            if err:
                return self._send(*err)
            # Both switches act on slot 0's kit (its .env, its judge.sh), whatever identity the page shows.
            if _ks != [0]:
                return self._send(409, {"error": "the switches act on slot 0 (the kit's own .env and judge.sh): "
                                                 "select slot 0"})
            if not _SWITCH_RUN.acquire(blocking=False):
                return self._send(409, {"error": "a switch is already being changed"})
            try:
                fn = switch_judge if path == "/api/judge" else switch_gpu
                return self._send(*fn(doc["on"]))
            finally:
                _SWITCH_RUN.release()
        return self._send(404, {"error": "route"})

    def _overview(self, qslot=None) -> tuple:
        """(HTTP status, body). The overview of one identity (slot 0 unless ?slot=k names another) and the
        list of every identity of this machine (`slots`), each with its last self-test and its exit command.
        One identity's containers, keys and figures are read per refresh: the others are read when selected."""
        env0 = kit_env()
        if not env0:
            return 200, {"installed": False, "kit": KIT,
                         "hint": f"No miner on this machine yet. In a terminal, from {REPO}: {INSTALL_HINT}"}
        all_rows, serr = slots()
        multi = all_rows is not None and len(all_rows) > 1
        k = 0
        if qslot not in (None, ""):
            ks, err = resolve_slots(qslot)
            if err:
                return err
            k = ks[0]
        row = next((r for r in all_rows or [] if r["slot"] == k), {"slot": k, "dir": KIT})
        env = env0 if k == 0 else read_env(os.path.join(row["dir"], ".env"))
        rows, derr = _for(containers, k)
        ident = _for(miner_identity, k)
        # The published figures only (`programme_view`): the current day is not ranked yet, and this screen
        # puts no estimate in place of a ranking.
        prog = programme_view(env, ident.get("miner_id", ""))
        s = settings()
        # One read inside the container: the keyring, the key files, the phrase's address (never its
        # words, which are fetched only to be shown) and the payout declaration on record.
        st = _for(keys_status, k)
        rp = recovery_head(st)
        ch = chain_view(env, ident)
        hv = health_view(k, row.get("dir"))
        kit, floor = _for(kit_reading, k)
        jmem = _for(judge_memory_view, k, env)
        if isinstance(jmem, dict) and "seat" in jmem:
            jmem = dict(jmem, seat=judge_seat_limit(jmem["seat"], ch))
        miner = next((c for c in rows or [] if c["service"] == "miner"), None)
        pv = payout_view(env, st, ident)
        # Every screen reads the same facts, each one a reading or None (not read): the state in plain
        # words, the dashboard's cards, the diagnosis, and the pay blockers the alert watches.
        facts = {"now": time.time(), "docker": derr,
                 "miner": {"running": None if rows is None else bool(miner and miner["state"] == "running"),
                           "word": (miner["state"] if miner else "no miner container") if rows is not None else ""},
                 "report": newest_report(hv, k), "schedule": hv, "role": role_view(env, hv), "kit": kit,
                 "floor": floor, "phrase": phrase_fact(st), "update": update_view(env0)["instruction"],
                 "capacity": _for(capacity_round_view, k), "jmem": jmem, "payout": pv,
                 "work": prog.get("latest"), "season_enabled": prog.get("enabled")}
        status = status_view(facts)
        blockers = pay_blockers(facts)
        alerts_tick(k, blockers)
        return 200, {"installed": True, "kit": KIT, "containers": rows, "docker_error": derr, "identity": ident,
                     "chain": ch, "programme": prog,
                     # A phrase still on the volume is one to confirm, and confirming removes it: False while it
                     # is there. None: no phrase to show (none kept for this key, or not read).
                     "recovery_confirmed": False if rp.get("present") else None,
                     "recovery_unreadable": rp.get("unreadable", ""),
                     "keys": keys_view(st),
                     "payout": pv,
                     "payout_address": s.get("payout_address", ""),
                     # Read-only: the registration and the stake are the `chain` readings above; this is the
                     # command that leaves, shown, never run from here.
                     "exit_command": exit_command(k, multi),
                     # ... and what leaving does, which owner mode changes (the stake goes back to the owner).
                     "exit_notes": exit_notes(env, ident, ch),
                     # The last scheduled self-test, with its age; never a verdict made here.
                     "health": hv,
                     # `judge` is the kit's setting (kept for older pages); `role` is what the container said.
                     "judge": env.get("DENDRA_MINER_JUDGE") == "1", "role": role_view(env, hv),
                     # The judge's memory: mode, the guard's gate, the model's load, the abstentions for lack of memory.
                     "judge_memory": jmem,
                     # The Judge and GPU mining switches: the machine's, read from slot 0's kit.
                     "switches": switches_view(env0, multi),
                     # How this machine updates its kit: the machine's line, slot 0's .env, whatever slot is shown.
                     "update": update_view(env0), "rules": rules_in_force(prog),
                     # The kit against the network's, as this identity's daemon read it: true, false or null (not read).
                     "kit": kit,
                     # The state in plain words (STATUS_RULES) and the last "Test my machine" that measured something.
                     "status": status, "machine_test": machine_tests().get(str(k)),
                     # The dashboard's cards, the diagnosis, the capacity sender's last round, and the alert's
                     # pay blockers with the state of the notifications.
                     "dashboard": dashboard_view(facts), "diagnosis": diagnosis_view(facts),
                     "capacity": facts["capacity"], "alerts": alerts_view(blockers, k),
                     # One identity per card: which one this is, and every one of this machine.
                     "slot": k, "multi": multi, "slots": slots_summary(all_rows, multi) if all_rows is not None else None,
                     "slots_error": serr or ""}

    def _recovery(self, k=0):
        # A phrase still on the volume is served, confirmed earlier or not: confirming is what removes it
        # from this machine, and a phrase confirmed by an older version of this application is still there.
        rp = _for(recovery_phrase, k)
        if not rp.get("present"):
            return self._send(200, dict(rp, slot=k))
        with APP.lock:
            # The positions are drawn for a single phrase of a single identity: the slot is part of what they prove.
            APP.challenge = (k, rp["address"], challenge_positions(len(rp["words"])))
            ask = APP.challenge[2]
        return self._send(200, dict(rp, ask=ask, slot=k, confirmed_before=phrase_confirmed(rp, settings())))

    def _recovery_confirm(self, doc, k=0):
        rp = _for(recovery_phrase, k)
        with APP.lock:
            shown = APP.challenge
        # The positions were drawn for one phrase: if the key changed since, or another identity's phrase was
        # shown since, they prove nothing about this one.
        if not rp.get("present") or not shown or shown[0] != k or shown[1] != rp["address"]:
            return self._send(409, {"error": "no phrase to confirm"})
        # The confirmation is kept per address (`phrase_confirmed`), and the removal checks the address
        # again inside the container: a phrase that names no address can be neither.
        if not rp["address"]:
            return self._send(422, {"error": "this phrase names no address, so its confirmation cannot be kept: "
                                             "write it down; it will keep being shown"})
        given = doc.get("words") or {}
        ok = all(str(given.get(str(i), "")).strip().lower() == rp["words"][i - 1] for i in shown[2])
        if not ok:
            return self._send(400, {"error": "those words do not match: check what you wrote down"})
        save_settings(record_confirmation(settings(), rp["address"]))
        with APP.lock:
            APP.challenge = None
        # Removed only now, after the three words matched, and only the file of this address: the button
        # says "remove it from this machine", and a phrase that was never written down must not be lost.
        removed, why = _for(forget_recovery, k, rp["address"])
        if not removed:
            return self._send(502, {"error": "the words match, but the phrase could not be removed from this "
                                             f"machine: {why}", "removed": False})
        return self._send(200, {"ok": True, "removed": True, "slot": k})

    def _payout(self, doc):
        addr = str(doc.get("address", "")).strip()
        if not ADDRESS.match(addr):
            return self._send(400, {"error": "a dendra1... address is required"})
        # One identity, or `all`: the same address declared by every active identity, each signing in its own
        # container with its own key -- the programme records one declaration per identity.
        ks, err = resolve_slots(doc.get("slot"), allow_all=True)
        if err:
            return self._send(*err)
        # Checked once the request is otherwise valid, so a mistyped address costs no try of the code.
        refused = self._code_refusal(doc.get("code"), "payout", (addr, tuple(ks)))
        if refused:
            return self._send(*refused)
        results = []
        for k in ks:
            if APP.stop_requested():
                # Stop came during an earlier identity's declaration: this one is not sent.
                results.append({"code": 409, "rc": INTERRUPTED_RC, "slot": k, "declared": False, "interrupted": True,
                                "error": "not sent: Stop interrupted the payout declaration before this identity"})
                continue
            results.append(dict(self._declare(addr, k), slot=k))
        codes = [r.pop("code") for r in results]
        worst = next((r["rc"] for r in results if r.get("rc", 1) != 0), 0)
        if any(r.get("interrupted") for r in results) and len(results) > 1:
            # The code is spent and some declarations may be on record: the answer says, per identity, what is known.
            return self._send(409, {"rc": worst, "results": results, "interrupted": True,
                                    "text": "\n".join(f"[slot {r['slot']}] " + (r.get("text") or r.get("error") or "")
                                                      for r in results)[-4000:]})
        if worst == 0:
            s = settings()
            s["payout_address"] = addr
            save_settings(s)
        if len(results) == 1:
            r = results[0]
            if "error" in r:
                return self._send(codes[0], {k: r[k] for k in ("error", "slot", "declared", "interrupted") if k in r})
            return self._send(codes[0], r)
        return self._send(200 if worst == 0 else 502, {"rc": worst, "results": results,
                                                       "text": "\n".join(f"[slot {r['slot']}] " + (r.get("text") or r.get("error") or "")
                                                                         for r in results)[-4000:]})

    def _declare(self, addr: str, k: int) -> dict:
        """One identity's payout declaration: {"code", "rc", "text"} or {"code", "error"}."""
        ident = _for(miner_identity, k)
        if not ident.get("miner_id"):
            why = ident.get("error") or (f"the miner's keys do not open: {ident['keyring_error']}"
                                         if ident.get("keyring_error") else "miner not running")
            return {"code": 409, "rc": 1, "error": why}
        # Owner mode: the programme accepts the declaration from the owner's key only, and it is not here.
        # Signing it with this machine's key would be refused there; this says so before anything is signed.
        # (One owner address registers one miner: a machine that runs several identities has no owner mode.)
        env = kit_env() if not k else read_env(os.path.join(slot_row(k).get("dir") or "", ".env"))
        owner = env.get("DENDRA_MINER_OWNER", "").strip()
        if owner and owner.lower() != str(ident.get("address") or "").lower():
            return {"code": 409, "rc": 1,
                    "error": f"this miner is owned by {owner}: the programme accepts its payout "
                             "declaration signed with the owner's key only, which is not on this "
                             "machine. Prepare it here, sign it there: docker compose -p dendra-miner "
                             "exec -T miner python3 final_season_miner.py payout-prepare --address "
                             f"{addr} > payout.json (it prints the signing command), then payout-submit "
                             "< payout.signed.json."}
        rc, out = _for(miner_exec, k, "python3", "final_season_miner.py", "payout", "--miner", ident["miner_id"],
                       "--address", addr)
        if rc == INTERRUPTED_RC and out == INTERRUPTED_TEXT:
            # Stop ended the exec while the declaration was being sent. The process in the container may have
            # finished it, so this answers what is known: not known, and where to read it.
            return {"code": 409, "rc": rc, "declared": None, "interrupted": True,
                    "error": "Stop interrupted the payout declaration while it was being sent: whether the programme "
                             "recorded it is not known here. The payout line of this page reads the declaration on "
                             "record; check it before declaring again, with a new code (this one is spent)."}
        return {"code": 200 if rc == 0 else 502, "rc": rc, "text": out[-2000:]}


def open_window(base: str, page: str) -> bool:
    """A native window when GTK 3 and WebKit2 are installed; False otherwise (the caller falls back to the
    default browser). The access page is loaded in-process, so its one-time token is in no URL and no file. A
    confirmation code is shown in a line of the window above the page, which the page cannot read."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import GLib, Gtk, WebKit2
    except (ImportError, ValueError):
        return False
    win = Gtk.Window(title="Dendra")
    win.set_default_size(1100, 760)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    code_line = Gtk.Label()
    code_line.set_selectable(True)
    code_line.set_no_show_all(True)
    box.pack_start(code_line, False, False, 6)
    view = WebKit2.WebView()
    box.pack_start(view, True, True, 0)
    win.add(box)

    def show_code(line: str) -> None:
        def on_main():
            code_line.set_text(line)
            code_line.show()
            return False
        GLib.idle_add(on_main)
    APP.code_sinks.append(show_code)
    view.load_html(page, base)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    return True


def forget_access() -> None:
    """The access file and its one-time token go together, once nobody used them in time."""
    with APP.lock:
        APP.once = None
    APP.forget_launch_file()


def _wait(stop=None) -> int:
    """Until Ctrl+C; for --windows, also until `stop` is set (watch_input_end, stop_on_hangup)."""
    try:
        while stop is None or not stop.is_set():
            time.sleep(3600 if stop is None else 0.5)
    except KeyboardInterrupt:
        pass
    return 0


def watch_input_end(stream, stop: threading.Event) -> threading.Thread:
    """--windows only. open_windows.ps1 holds the standard input open while it runs, so the end of that input is
    the end of the script, however it ended: read to the end, then set `stop`. Never armed for a launch from Linux,
    whose standard input may be closed from the start (systemd, nohup, a job in the background)."""
    def run():
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass
        stop.set()
    t = threading.Thread(target=run, name="input-end", daemon=True)
    t.start()
    return t


def stop_on_hangup(stop: threading.Event) -> dict:
    """--windows only. When wsl.exe itself is killed (its console window closed), the process here receives SIGHUP
    along with the end of its input, and the default action would end it before it removes the access page. Returns
    the handlers it replaced, for main to put back."""
    before = {}
    for sig in (signal.SIGHUP, signal.SIGTERM):
        before[sig] = signal.signal(sig, lambda *_: stop.set())
    return before


def _run(a, stop=None) -> int:
    base = f"http://127.0.0.1:{APP.port}/"
    if a.windows:
        # open_windows.ps1 opens the Windows browser on its access page: nothing to open here, nothing to print
        # that carries a token.
        print(f"Dendra is running at {base} for the Windows browser (WSL 2 forwards port {APP.port} to the "
              f"loopback of Windows). Its one-time access page is removed at first use. Dendra stops when the "
              f"window of open_windows.ps1 closes, or with Ctrl+C. Confirmation codes appear here.", flush=True)
        return _wait(stop)
    # Every launch from Linux gets in as the Windows one does: a one-time token posted to /handoff, which sets the
    # session cookie. The session token itself is never handed out.
    with APP.lock:
        APP.once = secrets.token_urlsafe(32)
        once = APP.once
    # The native window receives the access page in-process: it is in no command line and no file.
    if not a.no_window and open_window(base, access_page(APP.port, once)):
        return 0
    # Everything else goes through the access file: a token printed or handed to the browser's command line is
    # readable by any local user (/proc/<pid>/cmdline, session logs).
    try:
        APP.launch_file = write_launch_file(APP.port, once)
    except OSError as e:
        print(f"Dendra cannot write its access file ({e}): nothing was opened.", flush=True)
        return 2
    if a.no_window:
        # No timer here: on a host without a browser the file is copied to the machine whose browser opens it,
        # which no request marks; it goes at its first use or when Dendra stops.
        print(f"Dendra is running at {base}. Its access page is {APP.launch_file} (readable by you only, "
              f"removed after first use): open that file in a browser on this machine, or, on a host without "
              f"one, copy it to the machine whose browser you use and open it there, with an SSH tunnel to port "
              f"{APP.port} (ssh -L {APP.port}:127.0.0.1:{APP.port}). Once in, that tab keeps the session (reloading "
              f"it too) until Dendra stops. Confirmation codes appear here.", flush=True)
    else:
        import webbrowser
        if not stdout_is_terminal():
            APP.code_refused = NO_CODE_SINK
        # The browser may not start at all (no display, an SSH session) or may not be able to read the file:
        # the file's path is printed either way, and a failed start keeps the file instead of timing it out,
        # so the reader is never left with nothing to open.
        print(f"Opening Dendra ({base}) in your browser (install python3-gi and gir1.2-webkit2-4.1 for a "
              f"window). If no page appears, open {APP.launch_file} in a browser (readable by you only, "
              f"removed after first use). Confirmation codes appear here.", flush=True)
        try:
            started = webbrowser.open(pathlib.Path(APP.launch_file).as_uri())
        except Exception:  # noqa: BLE001 — a launcher that raises is a launcher that did not start
            started = False
        if started:
            t = threading.Timer(LAUNCH_FILE_SECONDS, forget_access)
            t.daemon = True
            t.start()
        else:
            print(f"No browser could be started. Open {APP.launch_file} in a browser on this machine, or read "
                  f"the address in it and open it through an SSH tunnel to port {APP.port}.", flush=True)
    return _wait(stop)


def arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Dendra miner application")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    # Started by deploy/app/open_windows.ps1: the token on the standard input, the access page it wrote.
    ap.add_argument("--windows", action="store_true")
    ap.add_argument("--access-file", default="")
    return ap


def windows_handoff(a, stdin) -> tuple:
    """(one-time token, None) for --windows, or (None, why it is refused). Read before anything listens: a
    fixed port, an access page that is a plain file, and a token from a pipe, whose value is never printed."""
    if not WINDOWS_PORT_MIN <= a.port <= WINDOWS_PORT_MAX:
        return None, f"--windows needs a fixed --port from {WINDOWS_PORT_MIN} to {WINDOWS_PORT_MAX}"
    if not os.path.isabs(a.access_file):
        return None, "--windows needs --access-file, the absolute path of the access page open_windows.ps1 wrote"
    try:
        st = os.lstat(a.access_file)
    except OSError as e:
        return None, f"the access page {a.access_file} cannot be read ({e.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return None, f"the access page {a.access_file} is not a plain file"
    try:
        tty = stdin is None or stdin.isatty()
    except (OSError, ValueError):
        tty = True
    if tty:
        return None, "--windows reads its token from a pipe (open_windows.ps1), never from a terminal"
    token = stdin.readline(256).strip().lstrip(BYTE_ORDER_MARK)
    if not HANDOFF_TOKEN.fullmatch(token):
        return None, "the token on the standard input is not 32 random bytes in URL-safe base64 (it is not printed)"
    return token, None


def main(argv=None) -> int:
    global APP
    a = arg_parser().parse_args(argv)
    once = None
    if a.windows:
        once, why = windows_handoff(a, sys.stdin)
        if why:
            print(f"Dendra refused: {why}. Nothing was started.", flush=True)
            return 2
    elif a.access_file:
        print("Dendra refused: --access-file goes with --windows. Nothing was started.", flush=True)
        return 2
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"Dendra cannot listen on 127.0.0.1 port {a.port} ({e.strerror}): another program holds it, "
              f"perhaps another Dendra. Nothing was started.", flush=True)
        return 2
    APP = App(srv.server_address[1])
    # Only the launch from Windows stops on the end of its input or on a hang-up: open_windows.ps1 holds that input.
    stop, replaced = (threading.Event(), {}) if a.windows else (None, {})
    if a.windows:
        APP.once, APP.launch_file = once, a.access_file
    if stop is not None:
        replaced = stop_on_hangup(stop)
        watch_input_end(sys.stdin, stop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return _run(a, stop)
    finally:
        for sig, handler in replaced.items():
            signal.signal(sig, handler)
        APP.forget_launch_file()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
