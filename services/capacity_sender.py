#!/usr/bin/env python3
"""capacity_sender.py -- this identity's signed capacity report, sent from the miner container, without cron.

Why it exists. The Final Testnet Season pays nothing to an identity without a signed capacity report: a day is
paid only when a signed report of the identity, filed by the day's end, declares a kit at or above the floor
(final_season_calc.kit_allows_pay, read through final_season_facts.kit_report). Until this file the report left
from the host only: deploy/join.sh once, when the registration was already seen, then a crontab line. A host
where cron does not run (WSL starts no cron by itself) sent nothing after the install, and its identities mined
and were paid nothing.

This worker runs in the miner container. docker/entrypoint-services.sh starts it next to the daemon and
relaunches it when it stops, so a report goes out whenever the miner runs, for every identity: each slot of
deploy/join.sh --gpus runs its own container, its own key and its own copy of this worker.

What it sends is what deploy/testnet-miner/publish-capacity.sh sends, built and checked the same way:
  * the hardware inventory the host probe produced (deploy/hw_probe.sh). The probe cannot run here: the miner
    container sees no card, and a report written here would declare none. publish-capacity.sh writes the
    inventory it checked into this identity's key volume (INVENTORY_NAME), at install (deploy/join.sh) and at
    every later run. The volume belongs to this identity alone and outlives the container, so a recreated
    container or an updated image keeps it;
  * miner_ids, host_share (slot k), judge_role, judge_mode, judge_memory_abstentions and kit_version, read here,
    where publish-capacity.sh reads them too (it asks this container);
  * signed by the identity's key (capacity_sign.signed_headers), and only once the chain records this identity
    with that key as its operator (registration, the reading of publish-capacity.sh's REG_PY);
  * posted to DENDRA_CAPACITY_URL, refused when it names a host nothing outside this machine reaches or a
    scheme other than https.
Once sent, again after PERIOD_S. After a round that sent nothing, again after RETRY_FIRST_S, doubled up to
RETRY_MAX_S. Every round writes its outcome to the status file (status_path), which miner_selftest.py (check C8)
reads.

Usage (the entrypoint runs it; by hand, inside the miner container):
    python3 capacity_sender.py --id <identity> --keydir /data/keys [--once]
--once runs one round and exits as publish-capacity.sh does: 0 sent, 2 not registered or registered for another
operator, 3 registration not read, 1 anything else.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The file the host writes into the key volume (publish-capacity.sh, INVENTORY_FILE: the bench compares the two).
INVENTORY_NAME = "capacity-inventory.json"
STATUS_FILE_ENV = "DENDRA_CAPACITY_STATUS_FILE"
STATUS_FILE_DEFAULT = "/tmp/dendra-capacity-status.json"
PERIOD_S = 3600          # the registry marks a report stale after a day; hourly, as the crontab line always was
FIRST_DELAY_S = 60       # the daemon registers first: a round at once would only read "absent"
RETRY_FIRST_S = 300
RETRY_MAX_S = 3600
POST_TIMEOUT_S = 15      # curl -m 15 in publish-capacity.sh
QUERY_TIMEOUT_S = 30     # REG_PY's bound on the registry query
SELFTEST_TIMEOUT_S = 120

# The phrase every message about the report carries (the messages bench reads it in each of them).
PAY_NOTE = "the season pays nothing to an identity without a signed capacity report"

# Outcomes of one round. The first four are REG_PY's words; the others name where the round stopped.
SENT = "sent"
REGISTERED, ABSENT, OTHER, UNREAD = "registered", "absent", "other", "unread"
NO_URL, NO_INVENTORY, BAD_SLOT, NO_KEY = "no-url", "no-inventory", "bad-slot", "no-key"
UNSIGNED, REFUSED, UNREACHABLE = "unsigned", "refused", "unreachable"
EXIT_OF = {SENT: 0, ABSENT: 2, OTHER: 2, UNREAD: 3}

# The fields this worker adds, in the order publish-capacity.sh appends them. An inventory that carries one is
# rewritten: what the container reads wins over what the host wrote.
ADDED = ("miner_ids", "host_share", "judge_role", "judge_mode", "judge_memory_abstentions", "kit_version")


def status_path() -> str:
    return os.environ.get(STATUS_FILE_ENV) or STATUS_FILE_DEFAULT


def retry_delay(failures: int) -> int:
    """The wait after `failures` rounds in a row that sent nothing: RETRY_FIRST_S, doubled, at most RETRY_MAX_S."""
    n = max(1, int(failures))
    return min(RETRY_MAX_S, RETRY_FIRST_S * (2 ** min(n - 1, 16)))


# -- the registry address: publish-capacity.sh::_host_of and _is_local_host, the same rules ----------------------
_LOCAL_NAMES = ("host.docker.internal", "localhost", "0.0.0.0", "::1", "[::1]")
_LOCAL_PREFIXES = ("127.", "10.", "192.168.") + tuple(f"172.{n}." for n in range(16, 32))


def host_of(url: str) -> str:
    s = re.sub(r"^[a-z]+://", "", url)
    s = re.sub(r":[0-9]+$", "", s)
    return re.sub(r"/.*$", "", s)


def is_local_host(host: str) -> bool:
    """A host nothing outside this machine reaches: a loopback or private address, or a name without a dot (a
    container or LAN alias). An IPv6 literal (colons, no dot) is left to the explicit entries."""
    if host in _LOCAL_NAMES or host.startswith(_LOCAL_PREFIXES):
        return True
    return not ("." in host or ":" in host)


def _plain(v) -> str:
    # publish-capacity.sh strips every space and double quote from these values (`tr -d '[:space:]"'`).
    return re.sub(r'[\s"]', "", str(v or ""))


def registry_url(env) -> tuple:
    """(url, why): DENDRA_CAPACITY_URL, or a URL derived from DENDRA_NODE's host; ("", why) when neither gives a
    public https one. In the kit DENDRA_NODE is container-local, so the derivation is refused there, as on the host.
    Only https is sent to: an unencrypted report can be captured on the way and posted again later, and the
    registry refuses an older one only once the account sequence has moved (capacity_server.py::older_than_stored)."""
    url = _plain(env.get("DENDRA_CAPACITY_URL"))
    node = _plain(env.get("DENDRA_NODE"))
    if not url and node:
        host = host_of(node)
        if not host:
            return "", "DENDRA_NODE is present but carries no host"
        if is_local_host(host):
            return "", (f"DENDRA_NODE points at a local node ({host}), which cannot be the public capacity registry; "
                        f"the kit's .env needs DENDRA_CAPACITY_URL (deploy/join.sh writes it)")
        url = f"https://{host}/capacity"
    if not url:
        return "", "no registry address: neither DENDRA_CAPACITY_URL nor DENDRA_NODE is set"
    if is_local_host(host_of(url)):
        return "", f"the capacity registry URL points at a local host ({url}), which nothing outside this machine reaches"
    if not url.lower().startswith("https://"):
        return "", (f"the capacity registry URL is not https ({url}): a signed report sent unencrypted can be captured "
                    f"and posted again; the kit's .env needs DENDRA_CAPACITY_URL=https://<host>/capacity")
    return url, ""


# -- what the host wrote -------------------------------------------------------------------------------------
def inventory_path(keydir: str) -> str:
    return os.path.join(keydir, INVENTORY_NAME)


def read_inventory(path: str) -> tuple:
    """(inventory, why). The inventory is the probe's flat JSON object as publish-capacity.sh checked it. Its
    gpu_count feeds that check, so an inventory without a whole gpu_count is refused, never read as zero cards."""
    hint = ("the host writes it: bash deploy/testnet-miner/publish-capacity.sh --inventory (deploy/join.sh runs it); "
            "on a rented pod, docker/cloud-start.sh writes it at each start")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, f"no hardware inventory at {path}; {hint}"
    except OSError as e:
        return None, f"the hardware inventory {path} is unreadable ({type(e).__name__}); {hint}"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, f"the hardware inventory {path} is not JSON; {hint}"
    if not isinstance(doc, dict):
        return None, f"the hardware inventory {path} is not a JSON object; {hint}"
    gc = doc.get("gpu_count")
    if type(gc) is not int or gc < 0:
        return None, f"the hardware inventory {path} carries no readable gpu_count; {hint}"
    nid = doc.get("node_id")
    if not isinstance(nid, str) or not nid.strip():
        return None, f"the hardware inventory {path} carries no node_id; {hint}"
    return doc, ""


def identity(keydir: str, asked: str) -> str:
    """The identity the daemon resolved (a `dm1...` name in its memory file), else the name asked for: the same
    adoption rule as docker/entrypoint-services.sh and publish-capacity.sh."""
    try:
        from modea import keyring as kring
        path = kring.resolved_identity_file(keydir)
        with open(path, encoding="utf-8") as f:
            v = "".join(f.read().split())
    except (OSError, ImportError):
        v = ""
    return v if v.startswith("dm1") else asked


# -- the readings made in this container ----------------------------------------------------------------------
def _run(cmd, timeout, cwd=None) -> tuple:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout or "", p.stderr or ""


def one_line(text: str, prefix: str):
    """The text after `prefix` on the one line that starts with it; None when no line or several do. One trailing
    carriage return is the only byte removed (publish-capacity.sh::_judge_role_word, _jm_line)."""
    hits = [ln[len(prefix):] for ln in (text or "").split("\n") if ln.startswith(prefix)]
    if len(hits) != 1:
        return None
    w = hits[0]
    return w[:-1] if w.endswith("\r") else w


def _why_line(text: str, prefix: str) -> str:
    for ln in (text or "").split("\n"):
        if ln.startswith(prefix):
            return re.sub(r"[\x00-\x1f\x7f]", "", ln[len(prefix):])[:240]
    return ""


def judge_role(run) -> tuple:
    """(active | mute | off | unknown, why), read by the container's self-test (miner_selftest.py --judge-role).
    Anything but exactly one line with one of the three words is unknown, never off."""
    try:
        _rc, out, _err = run([sys.executable, os.path.join(HERE, "miner_selftest.py"), "--judge-role"],
                             SELFTEST_TIMEOUT_S, HERE)
    except Exception as e:  # noqa: BLE001 -- a reading that fails is unknown
        return "unknown", f"the self-test could not be run ({type(e).__name__})"
    w = one_line(out, "DENDRA_JUDGE_ROLE ")
    why = _why_line(out, "DENDRA_JUDGE_ROLE_WHY ") or "the self-test gave no readable answer"
    return (w if w in ("active", "mute", "off") else "unknown"), why


def judge_memory(run) -> tuple:
    """(resident | on-demand | none | unknown, abstentions or None), by miner_selftest.py --judge-memory. An
    abstention count that is not a whole number is left out of the report: an absent count is not a zero."""
    try:
        _rc, out, _err = run([sys.executable, os.path.join(HERE, "miner_selftest.py"), "--judge-memory"],
                             SELFTEST_TIMEOUT_S, HERE)
    except Exception:  # noqa: BLE001 -- a reading that fails is unknown
        return "unknown", None
    mode = one_line(out, "DENDRA_JUDGE_MODE ")
    ab = one_line(out, "DENDRA_JUDGE_MEMORY_ABSTENTIONS ")
    return (mode if mode in ("resident", "on-demand", "none") else "unknown"), (
        int(ab) if ab and re.fullmatch(r"[0-9]+", ab) else None)


def kit_version():
    """The kit number of the image that runs (modea.kit_version, the number the daemon declares), or None."""
    try:
        from modea import kit_version as kitv
        return kitv.read_kit_version()
    except Exception:  # noqa: BLE001 -- an unread number is declared absent
        return None


def registration(mid: str, key: str, node: str, run, address_of) -> tuple:
    """(registered | absent | other | unread, why): publish-capacity.sh's REG_PY, the same reading. The record is
    parsed; an absent operator is the empty string (proto3 omits it), which no address equals."""
    cmd = ["dendrad", "query", "jobs", "get-miner", mid, "--output", "json"] + (["--node", node] if node else [])
    try:
        _rc, out, err = run(cmd, QUERY_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 -- unread, never registered
        return UNREAD, f"dendrad could not be run here ({type(e).__name__})"
    try:
        d = json.loads(out or "")
    except ValueError:
        d = None
    if not isinstance(d, dict):
        if "code = NotFound" in (out or "") + (err or ""):
            return ABSENT, f"the chain records no miner {mid} yet"
        return UNREAD, "the miner registry gave no record: " + ((err or out or "no answer").strip())[:160]
    m = d.get("miner")
    if not isinstance(m, dict):
        return UNREAD, "the answer is not a miner record"
    operator = str(m.get("operator", "") or "")
    try:
        address = address_of(key)
    except Exception as e:  # noqa: BLE001
        return UNREAD, f"the address of the signing key was not read ({type(e).__name__})"
    if not address:
        return UNREAD, "the signing key has no address"
    if operator == address:
        return REGISTERED, f"{mid} is registered, operated by this key ({address})"
    return OTHER, f"{mid} is registered for the operator {operator or '(none)'}, while this key is {address}"


def build_body(inv: dict, ident: str, slot: str, role: str, mode: str, abstentions, kit) -> bytes:
    """The exact bytes signed and sent: the inventory, then the fields read here, flat, without spaces."""
    doc = {k: v for k, v in inv.items() if k not in ADDED}
    doc["miner_ids"] = [ident]
    if slot != "0":
        doc["host_share"] = "secondary"   # the registry counts the machine's RAM and cores once
    doc["judge_role"] = role
    doc["judge_mode"] = mode
    if abstentions is not None:
        doc["judge_memory_abstentions"] = abstentions
    if kit is not None:
        doc["kit_version"] = kit
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=True).encode("ascii")


# -- signing and sending ----------------------------------------------------------------------------------------
def _address_default(key: str) -> str:
    from modea import relay_signature as rs
    return rs.address_from_key(key, keyring_backend=None, keyring_dir=(os.environ.get("DENDRA_KEYRING_DIR") or None))


def _sign_default(body: bytes, key: str, ident: str, node: str) -> dict:
    import capacity_sign
    return capacity_sign.signed_headers(body, key, ident, node=node or None,
                                        keyring_dir=(os.environ.get("DENDRA_KEYRING_DIR") or None))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the registry's answer, never followed. urllib would replay a 301, 302 or 303 as a GET without
    the body and read its 200 as a report filed; curl in publish-capacity.sh (no -L) reads the 30x as a refusal."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_default(url: str, body: bytes, headers: dict) -> tuple:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **headers))
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=POST_TIMEOUT_S) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001 -- unreachable is reported, then tried again
        return None, f"{type(e).__name__}: {e}"[:200]


class Deps:
    """What a round reaches outside this process. A bench replaces any of them."""

    def __init__(self, run=None, address_of=None, sign=None, post=None, kit=None, now=None):
        self.run = run or _run
        self.address_of = address_of or _address_default
        self.sign = sign or _sign_default
        self.post = post or _post_default
        self.kit = kit or kit_version
        self.now = now or time.time


def one_round(asked: str, keydir: str, env, deps: Deps) -> dict:
    """One attempt. Nothing is posted unless the inventory is readable, the key resolves, the chain records this
    identity with that key as its operator, and the body is signed: never an unsigned report, which the registry
    would file beside the signed one of the same machine (capacity_server.py::storage_key)."""
    ident = identity(keydir, asked)
    out = {"identity": ident, "at": int(deps.now())}

    def done(outcome, why, **more):
        out.update(more, outcome=outcome, why=why)
        return out

    url, why = registry_url(env)
    if not url:
        return done(NO_URL, why)
    out["url"] = url
    inv, why = read_inventory(inventory_path(keydir))
    if inv is None:
        return done(NO_INVENTORY, why)
    slot = str(env.get("DENDRA_SLOT") or "").strip() or "0"   # absent is slot 0, the zero value
    if not re.fullmatch(r"0|[1-9][0-9]*", slot):
        return done(BAD_SLOT, f"DENDRA_SLOT={slot!r} is not a slot number: host_share cannot be declared")
    # The signing key is the identity, as in publish-capacity.sh (its signing key defaults to the identity): the daemon
    # renamed its keyring entry to it, and the compose value of DENDRA_SIGN_KEY names the entry before the rename.
    key = ident
    try:
        address = deps.address_of(key)
    except Exception as e:  # noqa: BLE001
        address, err = "", f"{type(e).__name__}: {str(e)[:160]}"
    else:
        err = "it has no address"
    if not address:
        return done(NO_KEY, f"the signing key {key} does not resolve in this container's keyring ({err}): "
                            f"nothing is signed, so nothing is sent")
    node = _plain(env.get("DENDRA_NODE"))
    reg, why = registration(ident, key, node, deps.run, lambda _k: address)
    if reg != REGISTERED:
        return done(reg, why)
    role, role_why = judge_role(deps.run)
    mode, abstentions = judge_memory(deps.run)
    kit = deps.kit()
    body = build_body(inv, ident, slot, role, mode, abstentions, kit)
    out.update(judge_role=role, judge_role_why=role_why, judge_mode=mode, kit_version=kit, slot=slot)
    try:
        headers = deps.sign(body, key, ident, node)
    except Exception as e:  # noqa: BLE001
        return done(UNSIGNED, f"signing returned nothing ({type(e).__name__}: {str(e)[:160]}): nothing is sent")
    if not headers:
        return done(UNSIGNED, "signing returned nothing: nothing is sent")
    code, why = deps.post(url, body, headers)
    if code == 200:
        return done(SENT, f"signed report of {ident} accepted by {url}", http_code=200)
    if code is None:
        return done(UNREACHABLE, f"{url} did not answer ({why})")
    return done(REFUSED, f"{url} refused the report (HTTP {code})", http_code=code)


def line_of(res: dict, next_s: int) -> str:
    """The log line of one round."""
    nxt = f"next round in {max(1, round(next_s / 60))} min"
    if res["outcome"] == SENT:
        return (f"sent: {res['why']} (kit {res.get('kit_version')}, judge role {res.get('judge_role')}); {nxt}")
    return f"not sent ({res['outcome']}): {res['why']}. {PAY_NOTE[0].upper()}{PAY_NOTE[1:]}; {nxt}"


def write_status(res: dict, sent_at, next_at: int) -> None:
    doc = dict(res, sent_at=sent_at, next_at=next_at, pay_note=PAY_NOTE)
    path = status_path()
    try:
        fd, tmp = tempfile.mkstemp(prefix=".dendra-capacity.", suffix=".tmp", dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"the status file {path} could not be written ({type(e).__name__}): check C8 will not see this round",
              flush=True)


def run_forever(asked: str, keydir: str, env, deps: Deps, sleep=time.sleep, rounds=None) -> list:
    """Rounds until the process stops (or `rounds` of them, for a bench). Returns the results."""
    results, failures, sent_at, delay = [], 0, None, FIRST_DELAY_S
    while rounds is None or len(results) < rounds:
        sleep(delay)
        try:
            res = one_round(asked, keydir, env, deps)
        except Exception as e:  # noqa: BLE001 -- one round that raises is one round that sent nothing
            res = {"identity": asked, "at": int(deps.now()), "outcome": "error",
                   "why": f"the round raised {type(e).__name__}: {str(e)[:160]}"}
        if res["outcome"] == SENT:
            failures, sent_at, delay = 0, res["at"], PERIOD_S
        else:
            failures += 1
            delay = retry_delay(failures)
        write_status(res, sent_at, int(deps.now()) + delay)
        print(line_of(res, delay), flush=True)
        results.append(res)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send this identity's signed capacity report (miner container).")
    ap.add_argument("--id", required=True, help="the identity (the entrypoint passes the one the daemon resolved)")
    ap.add_argument("--keydir", default="/data/keys")
    ap.add_argument("--once", action="store_true", help="one round, then exit 0 sent, 2 not registered, 3 unread, 1 else")
    a = ap.parse_args(argv)
    deps = Deps()
    if a.once:
        res = one_round(a.id, a.keydir, os.environ, deps)
        write_status(res, res["at"] if res["outcome"] == SENT else None, res["at"])
        print(line_of(res, PERIOD_S if res["outcome"] == SENT else RETRY_FIRST_S), flush=True)
        return EXIT_OF.get(res["outcome"], 1)
    print(f"signed capacity report of {identity(a.keydir, a.id)}: first round in {FIRST_DELAY_S} s, then hourly once "
          f"sent ({PAY_NOTE})", flush=True)
    run_forever(a.id, a.keydir, os.environ, deps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
