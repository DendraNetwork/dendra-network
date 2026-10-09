#!/usr/bin/env python3
"""Dendra — the desktop application of a Dendra miner (Linux).

    dendra                 open the application (a native window when GTK and WebKit are installed,
                           the default browser otherwise)
    dendra --no-window     open nothing: serve the interface and print the path of its access file (to
                           open in a browser on this machine, or, on a host without one, to read for the
                           address to open through an SSH tunnel to the printed port)

WHAT IT DOES
It drives what `deploy/install.sh` and `deploy/join.sh` set up: the miner kit under
`deploy/testnet-miner`, its containers and its keys. It shows the result of the miner's self-test
(`deploy/testnet-miner/miner_health.sh`: the last hourly run with its age, and a quick run on demand), the
miner's balance and Final Testnet Season figures (those of the latest day the programme has ranked and
published, and what the ranked days paid in total); it starts, stops and restarts the miner; it shows the recovery phrase of
each new key until three of its words are typed back, and then removes it from this machine; it says
whether the miner's keys are kept encrypted or in clear, and whether the Final Testnet Season pays this
machine's own key; it declares where the Final Testnet Season rewards go. It shows the judge role the miner
container reported to the last check (a silent juror seat is said as such) and how this machine updates its kit,
as `deploy/join.sh` recorded it. It installs nothing and joins nothing by itself: when the miner kit is not
there, it says which command does that.
On a machine that runs one identity per card (`deploy/join.sh --gpus`), it lists every identity and acts on
the one selected -- never on slot 0 by default -- and can declare one payout address for all of them, each
identity signing its own declaration.

WHAT IT NEVER DOES
It opens no port to the network: the interface listens on 127.0.0.1 only, every API call carries a
random token generated at start-up, and a request whose Host is not that loopback address is refused, so
neither another web page nor another machine can drive it. The token never appears on a command line
or in the terminal, where other users of the machine can read it (`/proc/<pid>/cmdline`, session logs):
the native window receives it in-process, and the browser opens a file only this user can read
(mode 0600, in a 0700 directory), removed after the first authenticated request. It never sends a key or
the recovery phrase anywhere: the phrase is opened inside the miner container (with the keyring's
passphrase, which stays there) and shown on this screen only.
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
    from final_season_rules import RULES, rule_set_of  # noqa: E402
except ImportError:            # a kit without the services tree: the formula is not rerun, nor shown
    RULES, gross_of, Identity, rule_set_of = None, None, None, None

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


# ── one identity per card ───────────────────────────────────────────────────────────────────────
# deploy/join.sh --gpus runs one miner identity per NVIDIA card, a "slot": slot 0 is this kit's installation
# (compose project dendra-miner, the kit's .env), slot k its own project, env and files under gpu/<k>/.
# deploy/testnet-miner/slots.sh is the ONE place that knows a slot; this application asks it (`list`, and
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
    """([k, ...], None) or (None, (HTTP status, body)). A missing slot names slot 0 ONLY on a kit that runs one
    identity: on a machine that runs several, an action is never taken on slot 0 by default. `all` is every
    ACTIVE slot (a retired one is stopped on purpose). An unknown slot is a 400."""
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
    """fn(*a) for slot 0 -- the call this module always made -- and fn(*a, slot=k) for a slot k."""
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


def compose(*args, timeout=120, slot=0) -> tuple[int, str]:
    """`docker compose` for one identity. Slot 0 is the kit's directory form, as join.sh starts it: compose
    reads the kit's .env -- its COMPOSE_FILE and its COMPOSE_PROFILES. A slot k goes through slots.sh (`run`),
    which hands compose that slot's project, env and files and nothing of slot 0's. A refusal of the Docker
    socket is named in the first line of the output, so every action that shows its output says what to do
    instead of a raw error.
    THE JUDGE'S PROFILE IS NEVER DERIVED FROM THE JUDGE ROLE: every slot of a machine may vote, only slot 0
    hosts the judge's engine, and a profile named for a slot k would start a second engine there and pull the
    judge's model again. It is named here only for slot 0 of an OLDER kit, whose .env has no COMPOSE_PROFILES
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
    try:
        r = subprocess.run(argv, cwd=KIT, capture_output=True, text=True, timeout=timeout, env=run_env)
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
    # Read INSIDE the container by the miner's own keyring module (modea/keyring.py): it picks the backend
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
    """REST of the node the miner reads, as seen from THIS host. A miner on its own node names it by a
    container-side name: the node's alias on the dendra-chain network (`dendra-node`, or the node kit's
    project name), or host.docker.internal in a kit from an older join.sh. Neither resolves on the host,
    where the node kit publishes its REST on 127.0.0.1. A name without a dot is such an alias, never a
    public endpoint, and is read as this machine; an IPv6 literal (colons) is not a name."""
    node = env.get("DENDRA_NODE", "")
    host = node.split("://", 1)[-1].rsplit(":", 1)[0] if node else ""
    if host in ("host.docker.internal", "") or ("." not in host and ":" not in host):
        host = "127.0.0.1"
    return f"http://{host}:1317"


# The command that takes this miner off the network (deploy/testnet-miner/exit-miner.sh). The screen
# SHOWS it and runs nothing: leaving is irreversible, and the chain refuses it while the miner still holds
# an obligation (a held fee, a jury seat on an unresolved audit, an open dispute), for a time no button can
# promise. The script reads the miner's identity and stake, asks the chain what it would answer, and
# changes nothing without --yes. On a machine that runs one identity per card it names the slot -- always,
# slot 0 included: exit-miner.sh refuses --yes without --slot there.
def exit_command(slot=0, multi=False) -> str:
    base = f"bash {os.path.join(REPO, 'deploy', 'testnet-miner', 'exit-miner.sh')}"
    return f"{base} --slot {int(slot)}" if (slot or multi) else base


def exit_notes(env: dict, ident: dict, chain: dict) -> dict:
    """What the exit card says, which is not the same in owner mode (deploy/join.sh --owner): there the
    chain returns the stake to the OWNER, `--yes` only prepares the delete-miner the owner signs, and a
    restart prints the owner's create-miner instead of registering. Owner mode is read as exit-miner.sh
    reads it, from the CHAIN -- the key that registered the miner -- and, when the registration was not
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
                        "the miner and prepares the delete-miner the OWNER signs, and --yes --signed <file> sends "
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


# ── the miner's self-test ──────────────────────────────────────────────────────────────────────────
# The hourly job (join.sh::install_miner_health_cron) writes miner-health.last.json next to the kit on
# every run; this screen reads it, with its age, and runs a quick check on demand. It never decides
# health itself: the verdict is the script's, and a missing or malformed file is said as such.
HEALTH_SCRIPT = os.path.join(KIT, "miner_health.sh")
HEALTH_LAST = os.path.join(KIT, "miner-health.last.json")
# THE SCHEDULE'S PERIOD IS READ FROM THE DOCUMENT, NOT KEPT HERE: miner_health.sh writes it
# (schedule_period_s, from its SCHEDULE_PERIOD_S, which its bench confronts with the crontab line join.sh
# installs). A last run older than two periods is a schedule that did not fire (cron not running, a WSL
# distribution that was shut down), and the screen says so. A document without the period -- written by
# an older script -- is not judged stale or fresh: the screen says it cannot tell.
# "Run now" runs the script with --quick (no model probe, no relay check, no write): what is left is a few
# reads and C1's wait for a new block. Bounded, so a hung exec never holds the screen, and one at a time.
HEALTH_RUN_TIMEOUT_S = 240
# The self-test's own bound INSIDE the container, passed down with --deadline: half of this screen's wait,
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
    # ADVICE, apart from the checks: the script's own (A4, the silent juror seat) then the self-test's (A1-A3).
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


# THE JUDGE ROLE, AS THE CONTAINER SAID IT. miner_health.sh asks the miner container (miner_selftest.py
# --judge-role, the reader publish-capacity.sh puts in the signed capacity report) and carries the word in its
# document. One of four words, or None: a word that is none of them, or a document without the field (written by
# an older script, or a run that did not reach the container), is not read -- never "off", never "active".
JUDGE_ROLE_WORDS = ("active", "mute", "off", "unknown")


def _judge_role_from(jr) -> dict | None:
    if not isinstance(jr, dict) or jr.get("word") not in JUDGE_ROLE_WORDS:
        return None
    return {"word": jr["word"], "why": str(jr.get("why") or "")[:300]}


def role_view(env: dict, health: dict) -> dict:
    """The Role line of the overview. The CHAIN draws every present miner into juries; only an identity whose
    judge runs votes. So the line comes from the role READ IN THE CONTAINER by the last check (`word`, with that
    check's age), and the kit's setting (DENDRA_MINER_JUDGE) only says what was REQUESTED: a judge requested and
    not read is never shown as a juror that votes."""
    jr = health.get("judge_role") if isinstance(health, dict) and health.get("state") == "read" else None
    return {"word": jr["word"] if isinstance(jr, dict) else None, "why": jr["why"] if isinstance(jr, dict) else "",
            "age_s": health.get("age_s") if isinstance(jr, dict) else None,
            "requested": env.get("DENDRA_MINER_JUDGE") == "1"}


# THE UPDATE INSTRUCTION OF THIS MACHINE, as deploy/join.sh recorded it in the kit's .env (persist_kit_update):
# how this machine updates its kit, and the exact re-run of join.sh with its options. Read, never built here; a
# value with a character join.sh does not write is not shown (None), and None is said as "not recorded".
_KIT_LINE = re.compile(r"[A-Za-z0-9 :/._~%?=+@,;&()-]+")


def update_view(env: dict) -> dict:
    out = {}
    for key, name in (("DENDRA_KIT_UPDATE", "instruction"), ("DENDRA_KIT_RERUN", "rerun")):
        v = env.get(key, "")
        out[name] = v if _KIT_LINE.fullmatch(v) else None
    return out


def health_view(slot=0, sdir=None) -> dict:
    """The last scheduled run of one identity: slot 0's next to the kit, a slot k's in its own directory
    (miner_health.sh writes one per slot). THREE answers: 'never' (no run recorded -- nothing says the miner
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
        # It does NOT reach the self-test inside the container -- a killed `docker exec` client leaves its
        # process running there -- which is why the self-test is given its own, shorter, deadline.
        argv = ["bash", HEALTH_SCRIPT, "--json", "--quick", "--deadline", str(HEALTH_RUN_DEADLINE_S)]
        if slot is not None:
            argv += ["--slot", str(int(slot))]
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        except OSError as e:
            return 502, {"error": f"the check could not be started: {type(e).__name__}: {e}"}
        try:
            out, err = p.communicate(timeout=HEALTH_RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                p.kill()
            p.wait()
            return 504, {"error": f"the check did not finish within {HEALTH_RUN_TIMEOUT_S} s"}
        r = subprocess.CompletedProcess(p.args, p.returncode, out, err)
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
        return 200, {"rc": r.returncode, "health": _health_from(doc)}
    finally:
        _HEALTH_RUN.release()


def slots_summary(rows: list, multi: bool) -> list:
    """Every identity of this machine, as the overview lists it: what the kit's files say (project, state,
    card, requested identity), the last scheduled self-test of each -- a file read, no Docker call -- and the
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

    THE SUBSIDY IS WHAT HAS ACCRUED, NOT WHAT CAN BE CLAIMED NOW. `subsidy_claimable_udndr` is the cap the chain
    computes (demand x work_gate_bps / 10000, minus what was claimed: msg_server_claim_subsidy.go::ClaimSubsidy),
    and the chain pays it from the emission work pool: while that pool holds nothing, the claim is refused
    ("emission work pool is empty") until a later emission epoch funds it. The screen says so, and never that
    the miner will collect it now."""
    base, addr, mid = rest_base(env), ident.get("address", ""), ident.get("miner_id", "")
    out = {"rest": base, "balance_udndr": None, "registered": None, "stake_udndr": None,
           "subsidy_claimable_udndr": None, "creator": None}
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
                # The key that registered the miner: the one the chain returns the stake to on exit.
                out["creator"] = str(rec.get("creator") or "")
                if isinstance(p, dict) and isinstance(p.get("params"), dict):
                    cap = int(rec.get("demand", 0) or 0) * int(p["params"].get("work_gate_bps", 0) or 0) // 10000
                    out["subsidy_claimable_udndr"] = max(0, cap - int(rec.get("subsidy_claimed", 0) or 0))
    return out


# The figures of a ranking row (`final_season_calc.to_json`) this screen shows. Each is REQUIRED, as
# `final_season_payout.load_day` reads them back: a missing field, or one that is not an integer, is a ranking
# not read, never a 0.
RANKED_FIGURES = ("presence", "verified_requests", "verdicts", "gross_udndr", "payable_udndr", "paid_udndr")


def formula_of(presence: int, requests: int, verdicts: int, rules: dict | None = None) -> dict | None:
    """The published formula (`final_season_calc.gross_of`) rerun on one day's published facts, part by part,
    so the screen can say what each fact earned. Every part is the one formula applied to part of the
    facts, never a rate restated here. `rules` is the rule set the day was ranked under (None: the rules in
    force). None without the services tree."""
    if gross_of is None:
        return None
    work = gross_of(Identity("me", verified_requests=int(requests)), rules)
    juror = gross_of(Identity("me", verdicts=int(verdicts)), rules)
    gross = gross_of(Identity("me", presence=int(presence), verified_requests=int(requests),
                              verdicts=int(verdicts)), rules)
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
    # The formula is rerun under the rule set the ranking was computed with, found by the fingerprint the
    # ranking carries among every set the season published (`final_season_rules.rule_set_of`, RULE_HISTORY): a
    # day ranked under an earlier set is rerun under THAT set, never under the rules in force since. A
    # fingerprint of no published set, or none at all, is not rerun: another copy of the rules gives another
    # figure, and the screen would call a difference of versions a difference of figures.
    rules = None if rule_set_of is None else rule_set_of(ranking.get("rules_fingerprint"))[1]
    out["rules_differ"] = rule_set_of is not None and rules is None
    out["formula"] = (formula_of(out["presence"], out["verified_requests"], out["verdicts"], rules)
                      if rules is not None else None)
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


def recovery_phrase(slot=0) -> dict:
    """The kept phrase, opened INSIDE the miner container by its keyring module: with an encrypted keyring
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
    """What the overview needs at every refresh, read inside the container in ONE call
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


def forget_recovery(address: str, slot=0) -> tuple:
    """(removed, why). Removes EXACTLY the kept phrase file, inside the container, and only when the phrase
    it holds is still the one of `address` -- the one whose words were just typed back."""
    # --yes: the module removes nothing without it. It is passed HERE only, after the three words matched.
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
    """Where the Final Testnet Season pays this identity: the declaration the programme ACCEPTED (recorded
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
    confirmation is kept per ADDRESS, so a new key's phrase is shown again. A phrase without an address
    is never taken as confirmed (two empty strings are not the same key), and the old per-user flag
    `recovery_confirmed` is ignored: it hid every later key's phrase."""
    addr = str(rp.get("address") or "")
    many = s.get("recovery_confirmed_addresses")
    return bool(rp.get("present")) and bool(addr) and (
        addr == s.get("recovery_confirmed_address") or (isinstance(many, list) and addr in [str(x) for x in many]))


def record_confirmation(s: dict, address: str) -> dict:
    """The settings with `address` recorded as confirmed: the LAST one (`recovery_confirmed_address`, which
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
        # ?slot=k names one identity of a machine that runs one per card (deploy/join.sh --gpus).
        qslot = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("slot") or [None])[0]
        if path == "/api/overview":
            return self._send(*self._overview(qslot))
        if path in ("/api/recovery", "/api/logs", "/api/health"):
            ks, err = resolve_slots(qslot)
            if err:
                return self._send(*err)
            k = ks[0]
            if path == "/api/recovery":
                return self._recovery(k)
            if path == "/api/logs":
                rc, out = compose("logs", "--no-color", "--tail", "200", "miner", timeout=60, slot=k)
                return self._send(200, {"rc": rc, "text": out[-60000:], "slot": k})
            return self._send(200, health_view(k, slot_row(k).get("dir")))
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
            # One identity, or `all` (every active one); a missing slot is slot 0 only on a kit that runs one.
            ks, err = resolve_slots(doc.get("slot"), allow_all=True)
            if err:
                return self._send(*err)
            verb = {"/api/start": ("up", "-d", "--no-build"), "/api/stop": ("stop",),
                    "/api/restart": ("restart", "miner")}[path]
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
            ks, err = resolve_slots(doc.get("slot"))
            if err:
                return self._send(*err)
            rows, _ = slots()
            # A kit that runs one identity runs the script as it always did; on a machine with several, the
            # identity is named, or the script would check every slot and answer for the machine.
            code, body = run_health(ks[0] if rows is not None and len(rows) > 1 else None)
            return self._send(code, body)
        if path == "/api/payout":
            return self._payout(doc)
        return self._send(404, {"error": "route"})

    def _overview(self, qslot=None) -> tuple:
        """(HTTP status, body). The overview of ONE identity -- slot 0 unless ?slot=k names another -- and the
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
        return 200, {"installed": True, "kit": KIT, "containers": rows, "docker_error": derr, "identity": ident,
                     "chain": ch, "programme": prog,
                     # A phrase still on the volume is one to confirm, and confirming removes it: False while it
                     # is there. None: no phrase to show (none kept for this key, or not read).
                     "recovery_confirmed": False if rp.get("present") else None,
                     "recovery_unreadable": rp.get("unreadable", ""),
                     "keys": keys_view(st),
                     "payout": payout_view(env, st, ident),
                     "payout_address": s.get("payout_address", ""),
                     # Read-only: the registration and the stake are the `chain` readings above; this is the
                     # command that leaves, shown, never run from here.
                     "exit_command": exit_command(k, multi),
                     # ... and what leaving does, which owner mode changes (the stake goes back to the owner).
                     "exit_notes": exit_notes(env, ident, ch),
                     # The last scheduled self-test, with its age; never a verdict made here.
                     "health": hv,
                     # `judge` is the kit's SETTING (kept for older pages); `role` is what the container said.
                     "judge": env.get("DENDRA_MINER_JUDGE") == "1", "role": role_view(env, hv),
                     # How this machine updates its kit: the machine's line, slot 0's .env, whatever slot is shown.
                     "update": update_view(env0), "rules": RULES,
                     # One identity per card: which one this is, and every one of this machine.
                     "slot": k, "multi": multi, "slots": slots_summary(all_rows, multi) if all_rows is not None else None,
                     "slots_error": serr or ""}

    def _recovery(self, k=0):
        # A phrase still on the volume is served, confirmed earlier or not: confirming is what REMOVES it
        # from this machine, and a phrase confirmed by an older version of this application is still there.
        rp = _for(recovery_phrase, k)
        if not rp.get("present"):
            return self._send(200, dict(rp, slot=k))
        with APP.lock:
            # The positions are drawn for ONE phrase of ONE identity: the slot is part of what they prove.
            APP.challenge = (k, rp["address"], challenge_positions(len(rp["words"])))
            ask = APP.challenge[2]
        return self._send(200, dict(rp, ask=ask, slot=k, confirmed_before=phrase_confirmed(rp, settings())))

    def _recovery_confirm(self, doc, k=0):
        rp = _for(recovery_phrase, k)
        with APP.lock:
            shown = APP.challenge
        # The positions were drawn for ONE phrase: if the key changed since, or another identity's phrase was
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
        # ⛔ REMOVED ONLY NOW, after the three words matched, and only the file of THIS address: the button
        # says "remove it from this machine", and a phrase that was never written down must never go.
        removed, why = _for(forget_recovery, k, rp["address"])
        if not removed:
            return self._send(502, {"error": "the words match, but the phrase could not be removed from this "
                                             f"machine: {why}", "removed": False})
        return self._send(200, {"ok": True, "removed": True, "slot": k})

    def _payout(self, doc):
        addr = str(doc.get("address", "")).strip()
        if not ADDRESS.match(addr):
            return self._send(400, {"error": "a dendra1... address is required"})
        # One identity, or `all`: the same address declared by EVERY active identity, each signing in its own
        # container with its own key -- the programme records one declaration per identity.
        ks, err = resolve_slots(doc.get("slot"), allow_all=True)
        if err:
            return self._send(*err)
        results = [dict(self._declare(addr, k), slot=k) for k in ks]
        codes = [r.pop("code") for r in results]
        worst = next((r["rc"] for r in results if r.get("rc", 1) != 0), 0)
        if worst == 0:
            s = settings()
            s["payout_address"] = addr
            save_settings(s)
        if len(results) == 1:
            r = results[0]
            if "error" in r:
                return self._send(codes[0], {"error": r["error"], "slot": r["slot"]})
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
        # OWNER MODE: the programme accepts the declaration from the owner's key only, and it is not here.
        # Signing it with this machine's key would be refused there; this says so before anything is signed.
        # (One owner address registers one miner: a machine that runs several identities has no owner mode.)
        env = kit_env() if not k else read_env(os.path.join(slot_row(k).get("dir") or "", ".env"))
        owner = env.get("DENDRA_MINER_OWNER", "").strip()
        if owner and owner.lower() != str(ident.get("address") or "").lower():
            return {"code": 409, "rc": 1,
                    "error": f"this miner is owned by {owner}: the programme accepts its payout "
                             "declaration signed with the OWNER's key only, which is not on this "
                             "machine. Prepare it here, sign it there: docker compose -p dendra-miner "
                             "exec -T miner python3 final_season_miner.py payout-prepare --address "
                             f"{addr} > payout.json (it prints the signing command), then payout-submit "
                             "< payout.signed.json."}
        rc, out = _for(miner_exec, k, "python3", "final_season_miner.py", "payout", "--miner", ident["miner_id"],
                       "--address", addr)
        return {"code": 200 if rc == 0 else 502, "rc": rc, "text": out[-2000:]}


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
