# -*- coding: utf-8 -*-
"""The signed capacity report, sent from the miner container without cron (capacity_sender.py), and the messages.

The season pays nothing to an identity without a signed capacity report (final_season_calc.kit_allows_pay). The
report used to leave from the host only, through a crontab line that never runs where cron does not. These cases
execute the delivered code:
  * the chain the kit runs: the delivered publish-capacity.sh --inventory stores the probe's inventory through a
    fake docker into a fabricated key volume, then the delivered capacity_sender.py --once, on a PATH that holds
    no crontab, signs it through the delivered signer (capacity_sign.signed_headers, modea.relay_signature, a fake
    dendrad) and posts it; the bytes signed are the bytes posted;
  * a refusal and an unregistered identity are retried, sooner than the hourly period, and a sent report waits
    the period;
  * every slot signs its own report with its own key, and only a slot k says host_share secondary;
  * no key, no inventory, no whole gpu_count or no public registry address: nothing is signed, nothing is posted;
  * the sender reads the registration and the local hosts as publish-capacity.sh does (REG_PY and _is_local_host,
    extracted from the delivered script and run beside the sender's own readers);
  * every operator-facing message about the report says what it decides: join.sh's functions, publish-capacity.sh,
    the entrypoint, the sender's own lines.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# DENDRA_MODEA_DIR points this bench at a copy of services; DENDRA_REPO names the tree that carries deploy/
# (the battery runs pytest from a copy of prototype/ and passes it).
MODEA = Path(os.environ.get("DENDRA_MODEA_DIR") or Path(__file__).resolve().parent.parent)
REPO = Path(os.environ.get("DENDRA_REPO") or MODEA.parent.parent)
sys.path.insert(0, str(MODEA))

import capacity_sender as CS  # noqa: E402
from modea import relay_canon, relay_signature as RS  # noqa: E402

PUBLISH = REPO / "deploy" / "testnet-miner" / "publish-capacity.sh"
SLOTS = REPO / "deploy" / "testnet-miner" / "slots.sh"
JOIN = REPO / "deploy" / "join.sh"
ENTRY = REPO / "docker" / "entrypoint-services.sh"
PAY = "the season pays nothing to an identity without a signed capacity report"
ADDRS = {"dm1slotzero0000000000000": "dendra1slotzeroaddress", "dm1slottwo00000000000000": "dendra1slottwoaddress"}
MID0, MID2 = list(ADDRS)

# The fake dendrad: the keyring knows the names of FAKE_KEYS, the registry answers from FAKE_STATE/miner.json
# (the word `notfound` is the chain's NotFound), and `tx sign` records the memo it signed.
FAKE_DENDRAD = r'''#!/usr/bin/env python3
import json, os, sys
S = os.environ["FAKE_STATE"]
a = sys.argv[1:]
with open(os.path.join(S, "dendrad.log"), "a") as f:
    f.write(json.dumps(a) + "\n")
keys = json.loads(os.environ.get("FAKE_KEYS") or "{}")
if a[:2] == ["keys", "show"]:
    if a[2] in keys:
        print(keys[a[2]]); sys.exit(0)
    sys.stderr.write("Error: " + a[2] + " is not a valid name or address: key not found\n"); sys.exit(1)
if a[:3] == ["query", "jobs", "get-miner"]:
    p = os.path.join(S, "miner-" + a[3] + ".json")
    raw = open(p).read() if os.path.exists(p) else "notfound"
    if raw.strip() == "notfound":
        sys.stderr.write("Error: rpc error: code = NotFound desc = miner not found\n"); sys.exit(1)
    print(raw); sys.exit(0)
if a[:3] == ["query", "auth", "account"]:
    print(json.dumps({"account": {"account_number": "7", "sequence": "3"}})); sys.exit(0)
if a[:2] == ["tx", "sign"]:
    doc = json.load(open(a[2]))
    name = a[a.index("--from") + 1]
    with open(os.path.join(S, "signed.log"), "a") as f:
        f.write(json.dumps({"from": name, "memo": doc["body"]["memo"]}) + "\n")
    import base64
    doc["auth_info"]["signer_infos"] = [{"public_key": {"key": base64.b64encode(bytes([2]) + bytes(32)).decode()},
                                         "mode_info": {"single": {"mode": "SIGN_MODE_LEGACY_AMINO_JSON"}}}]
    doc["signatures"] = [base64.b64encode(bytes(64)).decode()]
    print(json.dumps(doc)); sys.exit(0)
sys.exit(64)
'''

# The fake docker: one running miner container (cid0) for the kit, whose /data/keys is FAKE_VOL and whose /app is
# the delivered services directory. `exec` runs the command here, with its -e variables and that path mapped.
FAKE_DOCKER = r'''#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_STATE/docker.log"
cmd="$1"; shift
case "$cmd" in
  ps)
    case "$*" in *"com.docker.compose.service=miner"*) [ -n "${FAKE_RUNNING:-}" ] && printf 'cid0\n' ;; esac
    exit 0 ;;
  inspect) printf '%s\n' "$FAKE_KIT"; exit 0 ;;
  exec)
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -i) shift ;;
        -e) export "$(printf '%s' "$2" | sed "s#/data/keys#$FAKE_VOL#g")"; shift 2 ;;
        -w) shift 2 ;;
        *) break ;;
      esac
    done
    shift
    args=()
    for a in "$@"; do args+=("$(printf '%s' "$a" | sed "s#/data/keys#$FAKE_VOL#g")"); done
    cd "$FAKE_APP" || exit 1
    exec "${args[@]}" ;;
esac
exit 0
'''

# The host probe of the fabricated kit: one card, the id it is given last.
FAKE_PROBE = r'''#!/usr/bin/env bash
id="${@: -1}"
printf '{"node_id":"%s","machine":"abcdef123456","backend":"gpu","gpu":"RTX 3060","gpu_count":1,"vram_mb":12288,"ram_mb":65536,"cpu_cores":16,"tier":3,"model":"m","can_judge":true}\n' "$id"
'''

TOOLS = ("bash", "sh", "sed", "head", "tr", "awk", "grep", "sort", "cut", "cat", "mv", "dirname", "tail", "curl",
         "env", "printf", "mktemp", "rm", "wc")


BENCH_HTTPS_URL = "https://registry.bench.test/capacity"


def _bench_tls(d):
    """A self-signed certificate for registry.bench.test, which SSL_CERT_FILE makes the sender trust."""
    cert, key = d / "registry.pem", d / "registry.key"
    p = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=registry.bench.test",
                        "-addext", "subjectAltName=DNS:registry.bench.test"], capture_output=True, text=True)
    assert p.returncode == 0, "openssl could not make the bench certificate: " + p.stderr
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return str(cert), ctx


class Registry:
    """The capacity registry, reached through a proxy: the sender refuses a loopback address, as it must, so the
    case names a public-looking host and the proxy setting brings the request here. The sender posts only to
    https, so the proxy also opens the tunnel the client asks for and serves it with a certificate for that host."""

    def __init__(self, tls_dir, answers=None):
        self.calls, self.answers = [], list(answers or [])
        self.cert, tls = _bench_tls(tls_dir)
        reg = self

        class H(BaseHTTPRequestHandler):
            tunnel = ""

            def log_message(self, *a):
                pass

            def do_CONNECT(self):
                # The https request arrives inside the tunnel, with a relative path: it is filed under its host.
                self.send_response(200, "Connection established")
                self.end_headers()
                self.connection = tls.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb", buffering=0)
                self.tunnel = "https://" + self.path.rsplit(":", 1)[0]
                self.close_connection = False
                self.handle_one_request()
                self.close_connection = True

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                # header names are case-insensitive (urllib sends `X-dendra-miner`): kept lowercased
                reg.calls.append(("POST", self.tunnel + self.path, {k.lower(): v for k, v in self.headers.items()},
                                  self.rfile.read(n)))
                code = reg.answers.pop(0) if reg.answers else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_GET(self):
                reg.calls.append(("GET", self.path, dict(self.headers), b""))
                body = json.dumps({"verified": {"live_nodes": 1}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.proxy = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def reg(tmp_path_factory):
    r = Registry(tmp_path_factory.mktemp("tls"))
    yield r
    r.close()


def _bin(tmp, with_docker=True):
    b = tmp / "bin"
    b.mkdir(exist_ok=True)
    for t in TOOLS:
        p = shutil.which(t)
        if p and not (b / t).exists():
            os.symlink(p, b / t)
    if not (b / "python3").exists():
        os.symlink(sys.executable, b / "python3")
    (b / "dendrad").write_text(FAKE_DENDRAD)
    os.chmod(b / "dendrad", 0o755)
    if with_docker:
        (b / "docker").write_text(FAKE_DOCKER)
        os.chmod(b / "docker", 0o755)
    assert not (b / "crontab").exists()
    return b


def _kit(tmp):
    """A fabricated clone: the delivered publish-capacity.sh and slots.sh, a fake probe, one slot's .env."""
    root = tmp / "clone"
    kit = root / "deploy" / "testnet-miner"
    kit.mkdir(parents=True)
    shutil.copy(PUBLISH, kit / "publish-capacity.sh")
    shutil.copy(SLOTS, kit / "slots.sh")
    (root / "deploy" / "hw_probe.sh").write_text(FAKE_PROBE)
    (root / "docker").mkdir()
    (root / "docker" / "KIT_VERSION").write_text("4\n")
    (kit / ".env").write_text("MINER_ID=m-bench\nDENDRA_NODE=tcp://dendra-node:26657\n"
                              "DENDRA_CAPACITY_URL=http://registry.bench.test/capacity\n")
    return root, kit


def _volume(tmp, mid=MID0, name="vol"):
    vol = tmp / name
    (vol / "cosmos" / "keyring-test").mkdir(parents=True)
    (vol / "cosmos" / "keyring-test" / (mid + ".info")).write_text("")
    (vol / "resolved-identity").write_text(mid + "\n")
    return vol


def _env(tmp, b, reg, vol, **over):
    state = tmp / "state"
    state.mkdir(exist_ok=True)
    (tmp / "KIT_VERSION").write_text("4\n")
    env = {"PATH": str(b), "HOME": str(tmp), "FAKE_STATE": str(state), "FAKE_KEYS": json.dumps(ADDRS),
           "FAKE_VOL": str(vol), "FAKE_APP": str(MODEA), "FAKE_RUNNING": "1",
           "http_proxy": reg.proxy, "HTTP_PROXY": reg.proxy, "no_proxy": "", "NO_PROXY": "",
           "https_proxy": reg.proxy, "HTTPS_PROXY": reg.proxy, "SSL_CERT_FILE": reg.cert,
           "DENDRA_CAPACITY_URL": "http://registry.bench.test/capacity", "DENDRA_NODE": "tcp://dendra-node:26657",
           "DENDRA_KEYRING_DIR": str(vol / "cosmos"), "DENDRA_KEYRING_PASSPHRASE_FILE": str(tmp / "no-passphrase"),
           "DENDRA_SLOT": "0", "DENDRA_CAPACITY_STATUS_FILE": str(tmp / "capacity-status.json"),
           "DENDRA_KIT_VERSION_FILE": str(tmp / "KIT_VERSION"), "DENDRA_MINER_JUDGE": "0",
           "DENDRA_STATUS_FILE": str(tmp / "heartbeat.json"), "DENDRA_JUDGE_STATE_FILE": str(tmp / "judge-state.json"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(over)
    return env


def _registered(tmp, mid, operator):
    (tmp / "state").mkdir(exist_ok=True)
    (tmp / "state" / f"miner-{mid}.json").write_text(json.dumps({"miner": {"miner_id": mid, "operator": operator}}))


def _send_once(env, vol, asked="m-bench"):
    # The sender posts only to https (capacity_sender.registry_url); publish-capacity.sh keeps the bench's http URL.
    env = dict(env, DENDRA_CAPACITY_URL=BENCH_HTTPS_URL)
    return subprocess.run([sys.executable, str(MODEA / "capacity_sender.py"), "--id", asked, "--keydir", str(vol),
                           "--once"], env=env, capture_output=True, text=True, timeout=120)


# -- the chain the kit runs, with no cron anywhere -----------------------------------------------------------------
def test_without_cron_the_inventory_the_host_stores_is_signed_and_posted_by_the_container(tmp_path, reg):
    b, (root, kit), vol = _bin(tmp_path), _kit(tmp_path), _volume(tmp_path)
    env = _env(tmp_path, b, reg, vol, FAKE_KIT=str(kit))
    # 1. the host stores the checked inventory in the identity's key volume, and sends nothing
    p = subprocess.run(["bash", str(kit / "publish-capacity.sh"), "--inventory"], env=env, capture_output=True,
                       text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    inv = json.loads((vol / CS.INVENTORY_NAME).read_text())
    assert inv["node_id"] == "m-bench" and inv["gpu_count"] == 1 and "miner_ids" not in inv
    assert reg.posts() == [] and PAY in p.stdout
    # 2. the container's sender, before the chain records the identity: nothing posted, said with the pay
    s = _send_once(env, vol)
    assert s.returncode == 2 and reg.posts() == [], s.stdout + s.stderr
    assert "not sent (absent)" in s.stdout and PAY.capitalize() in s.stdout
    # 3. once it does, with this key as its operator: one signed report, the bytes signed are the bytes posted
    _registered(tmp_path, MID0, ADDRS[MID0])
    s = _send_once(env, vol)
    assert s.returncode == 0, s.stdout + s.stderr
    posts = reg.posts()
    assert len(posts) == 1 and posts[0][1] == BENCH_HTTPS_URL
    body, headers = posts[0][3], posts[0][2]
    doc = json.loads(body)
    assert doc["miner_ids"] == [MID0] and doc["node_id"] == "m-bench" and doc["kit_version"] == 4
    assert doc["judge_role"] == "off" and "host_share" not in doc and doc["gpu_count"] == 1
    assert headers[RS.HEADER_MINER.lower()] == MID0 and headers[RS.HEADER_ACCT.lower()] == "7"
    assert headers[RS.HEADER_SEQ.lower()] == "3" and headers[RS.HEADER_SIG.lower()]
    signed = [json.loads(x) for x in (tmp_path / "state" / "signed.log").read_text().splitlines()]
    assert signed == [{"from": MID0, "memo": relay_canon.digest("capacity", MID0, body, MID0, 0).hex()}]
    st = json.loads((tmp_path / "capacity-status.json").read_text())
    assert st["outcome"] == "sent" and st["sent_at"] == st["at"] and st["pay_note"] == PAY
    assert not shutil.which("crontab", path=env["PATH"])


def test_the_manual_tool_still_publishes_and_stores_the_inventory_even_unregistered(tmp_path, reg):
    b, (root, kit), vol = _bin(tmp_path), _kit(tmp_path), _volume(tmp_path)
    env = _env(tmp_path, b, reg, vol, FAKE_KIT=str(kit))
    p = subprocess.run(["bash", str(kit / "publish-capacity.sh")], env=env, capture_output=True, text=True, timeout=180)
    assert p.returncode == 2, p.stdout + p.stderr
    assert (vol / CS.INVENTORY_NAME).exists(), "the inventory is stored before the registration is read"
    assert PAY in p.stdout and "grep capacity" in p.stdout and "hourly job" not in p.stdout
    _registered(tmp_path, MID0, ADDRS[MID0])
    p = subprocess.run(["bash", str(kit / "publish-capacity.sh")], env=env, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stdout + p.stderr
    assert len(reg.posts()) == 1 and json.loads(reg.posts()[0][3])["miner_ids"] == [MID0]


def test_inventory_mode_without_a_running_miner_stores_nothing_and_says_why(tmp_path, reg):
    b, (root, kit), vol = _bin(tmp_path), _kit(tmp_path), _volume(tmp_path)
    env = _env(tmp_path, b, reg, vol, FAKE_KIT=str(kit), FAKE_RUNNING="")
    p = subprocess.run(["bash", str(kit / "publish-capacity.sh"), "--inventory"], env=env, capture_output=True,
                       text=True, timeout=120)
    assert p.returncode == 3 and not (vol / CS.INVENTORY_NAME).exists(), p.stdout + p.stderr
    assert PAY in p.stdout and "--inventory" in p.stdout


# -- the rounds: retried after a refusal, the period once sent ------------------------------------------------------
class Fakes:
    def __init__(self, posts=(200,), regs=None, address=None):
        self.post_codes, self.posted, self.signed = list(posts), [], []
        self.regs = list(regs or [])
        self.address = address

    def deps(self):
        def run(cmd, timeout, cwd=None):
            if cmd[:4] == ["dendrad", "query", "jobs", "get-miner"]:
                op = self.regs.pop(0) if self.regs else ADDRS.get(cmd[4], "")
                if op is None:
                    return 1, "", "rpc error: code = NotFound desc = not found"
                if op == "garbage":
                    return 1, "not json", "connection refused"
                return 0, json.dumps({"miner": {"operator": op}}), ""
            if "--judge-role" in cmd:
                return 0, "DENDRA_JUDGE_ROLE active\nDENDRA_JUDGE_ROLE_WHY bench\n", ""
            if "--judge-memory" in cmd:
                return 0, "DENDRA_JUDGE_MODE resident\nDENDRA_JUDGE_MEMORY_ABSTENTIONS 2\n", ""
            return 1, "", "unexpected"

        def address_of(key):
            if self.address is not None:
                return self.address(key)
            return ADDRS[key]

        def sign(body, key, ident, node):
            self.signed.append((key, ident, body))
            return {RS.HEADER_MINER: ident}

        def post(url, body, headers):
            self.posted.append((url, body, headers))
            return (self.post_codes.pop(0) if self.post_codes else 200), ""

        return CS.Deps(run=run, address_of=address_of, sign=sign, post=post, kit=lambda: 4, now=lambda: 1_000_000)


def _inventory(vol, **over):
    inv = {"node_id": "m-bench", "gpu_count": 1, "vram_mb": 8192, "ram_mb": 32768}
    inv.update(over)
    vol.mkdir(parents=True, exist_ok=True)
    (vol / CS.INVENTORY_NAME).write_text(json.dumps(inv))


def _envd(slot="0", **over):
    e = {"DENDRA_CAPACITY_URL": "https://api.example.net/capacity", "DENDRA_NODE": "tcp://dendra-node:26657",
         "DENDRA_SLOT": slot}
    e.update(over)
    return e


def test_a_refusal_is_retried_sooner_than_the_period_and_a_sent_report_waits_the_period(tmp_path, monkeypatch):
    monkeypatch.setenv(CS.STATUS_FILE_ENV, str(tmp_path / "status.json"))
    vol = _volume(tmp_path)
    _inventory(vol)
    f = Fakes(posts=[503, 200, 200])
    waits = []
    res = CS.run_forever(MID0, str(vol), _envd(), f.deps(), sleep=waits.append, rounds=3)
    assert [r["outcome"] for r in res] == ["refused", "sent", "sent"]
    assert waits == [CS.FIRST_DELAY_S, CS.RETRY_FIRST_S, CS.PERIOD_S]
    assert len(f.posted) == 3 and all(p[1] == f.posted[0][1] for p in f.posted)


def test_an_unregistered_or_unread_identity_is_asked_again_and_the_wait_grows_to_the_period(tmp_path, monkeypatch):
    monkeypatch.setenv(CS.STATUS_FILE_ENV, str(tmp_path / "status.json"))
    vol = _volume(tmp_path)
    _inventory(vol)
    # NotFound, then another operator, then unread (no record), NotFound twice, then registered
    f = Fakes(regs=[None, "dendra1someoneelse", "garbage", None, None, ADDRS[MID0]])
    waits = []
    res = CS.run_forever(MID0, str(vol), _envd(), f.deps(), sleep=waits.append, rounds=6)
    assert [r["outcome"] for r in res] == ["absent", "other", "unread", "absent", "absent", "sent"]
    assert waits == [CS.FIRST_DELAY_S, 300, 600, 1200, 2400, 3600]
    assert len(f.posted) == 1 and len(f.signed) == 1
    assert CS.retry_delay(50) == CS.RETRY_MAX_S
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["outcome"] == "sent" and st["next_at"] == 1_000_000 + CS.PERIOD_S


def test_each_slot_signs_its_own_report_and_only_a_slot_k_is_secondary(tmp_path):
    v0, v2 = _volume(tmp_path, MID0, "vol0"), _volume(tmp_path, MID2, "vol2")
    _inventory(v0)
    _inventory(v2, node_id="m-bench-g2")
    f = Fakes()
    r0 = CS.one_round("m-bench", str(v0), _envd("0"), f.deps())
    r2 = CS.one_round("m-bench-g2", str(v2), _envd("2"), f.deps())
    assert r0["outcome"] == r2["outcome"] == "sent"
    (k0, i0, b0), (k2, i2, b2) = f.signed
    assert (k0, i0) == (MID0, MID0) and (k2, i2) == (MID2, MID2)
    d0, d2 = json.loads(b0), json.loads(b2)
    assert "host_share" not in d0 and d2["host_share"] == "secondary"
    assert d0["miner_ids"] == [MID0] and d2["miner_ids"] == [MID2] and d2["node_id"] == "m-bench-g2"
    assert d0["judge_role"] == "active" and d0["judge_mode"] == "resident" and d0["judge_memory_abstentions"] == 2
    # the fields read here follow the inventory, in publish-capacity.sh's order
    assert list(d2)[-6:] == list(CS.ADDED) and list(d0)[-5:] == [k for k in CS.ADDED if k != "host_share"]
    assert [p[1] for p in f.posted] == [b0, b2], "the body posted is the body signed"
    # a slot that is no slot number declares nothing rather than a wrong share
    assert CS.one_round("m-bench", str(v0), _envd("02"), f.deps())["outcome"] == "bad-slot"


def test_without_a_key_nothing_is_signed_nor_sent(tmp_path, reg):
    vol = _volume(tmp_path)
    _inventory(vol)

    def no_key(key):
        raise RS.SignatureUnavailable("dendrad keys show -> code 1: key not found")
    f = Fakes(address=no_key)
    r = CS.one_round(MID0, str(vol), _envd(), f.deps())
    assert r["outcome"] == "no-key" and f.signed == [] and f.posted == []
    assert "nothing is signed, so nothing is sent" in r["why"]
    # the same through the delivered process and the fake dendrad: a keyring without the identity's key
    b = _bin(tmp_path, with_docker=False)
    _registered(tmp_path, MID0, ADDRS[MID0])
    env = _env(tmp_path, b, reg, vol, FAKE_KEYS=json.dumps({"someone-else": "dendra1x"}))
    s = _send_once(env, vol)
    assert s.returncode == 1 and reg.posts() == [] and "not sent (no-key)" in s.stdout, s.stdout + s.stderr
    assert not (tmp_path / "state" / "signed.log").exists()


def test_no_inventory_a_bad_gpu_count_or_no_public_registry_posts_nothing(tmp_path):
    vol = _volume(tmp_path)
    f = Fakes()
    assert CS.one_round(MID0, str(vol), _envd(), f.deps())["outcome"] == "no-inventory"
    for bad in ({"gpu_count": None}, {"gpu_count": "1"}, {"gpu_count": -1}, {"gpu_count": True}, {"node_id": ""}):
        _inventory(vol, **bad)
        assert CS.one_round(MID0, str(vol), _envd(), f.deps())["outcome"] == "no-inventory", bad
    (vol / CS.INVENTORY_NAME).write_text(json.dumps({"node_id": "m-bench"}))
    r = CS.one_round(MID0, str(vol), _envd(), f.deps())
    assert r["outcome"] == "no-inventory" and "gpu_count" in r["why"], "an absent count is not zero cards"
    _inventory(vol)
    for env in (_envd(DENDRA_CAPACITY_URL=""), _envd(DENDRA_CAPACITY_URL="http://127.0.0.1:8092/capacity"),
                _envd(DENDRA_CAPACITY_URL="https://dendra-node/capacity"), _envd(DENDRA_CAPACITY_URL="", DENDRA_NODE=""),
                _envd(DENDRA_CAPACITY_URL="http://api.example.net/capacity")):
        assert CS.one_round(MID0, str(vol), env, f.deps())["outcome"] == "no-url", env
    assert f.posted == [] and f.signed == []
    # the host's inventory never decides the identity, the role or the kit: the container's reading wins
    _inventory(vol, miner_ids=["dm1forged"], judge_role="active", kit_version=99, host_share="secondary")
    CS.one_round(MID0, str(vol), _envd(), f.deps())
    d = json.loads(f.posted[-1][1])
    assert d["miner_ids"] == [MID0] and d["kit_version"] == 4 and "host_share" not in d


def test_only_an_https_registry_url_is_sent_to():
    # An unencrypted report can be captured and posted again later: the sender refuses any other scheme.
    for url in ("http://api.example.net/capacity", "HTTP://api.example.net/capacity", "api.example.net/capacity",
                "ftp://api.example.net/capacity", "httpss://api.example.net/capacity"):
        got, why = CS.registry_url({"DENDRA_CAPACITY_URL": url})
        assert got == "" and "not https" in why, (url, got, why)
    for url in ("https://api.example.net/capacity", "HTTPS://api.example.net/capacity"):
        assert CS.registry_url({"DENDRA_CAPACITY_URL": url}) == (url, ""), url
    # the URL derived from a public DENDRA_NODE is https
    assert CS.registry_url({"DENDRA_NODE": "tcp://203.0.113.7:26657"}) == ("https://203.0.113.7/capacity", "")
    # a local host is still refused as local, before the scheme is read
    got, why = CS.registry_url({"DENDRA_CAPACITY_URL": "http://127.0.0.1:8092/capacity"})
    assert got == "" and "local host" in why, why


# -- the same readings as publish-capacity.sh --------------------------------------------------------------------
def _between(text, first, last_prefix):
    lines = text.split("\n")
    i = lines.index(first)
    j = next(k for k in range(i + 1, len(lines)) if lines[k].startswith(last_prefix))
    return "\n".join(lines[i:j + 1])


def test_the_registration_is_read_as_REG_PY_reads_it(tmp_path, monkeypatch):
    src = PUBLISH.read_text(encoding="utf-8")
    m = re.search(r"REG_PY=\"\$\(cat <<'PY'\n(.*?)\nPY\n\)\"", src, re.S)
    assert m, "REG_PY not found in the delivered publish-capacity.sh"
    reg_py = m.group(1)
    b = _bin(tmp_path, with_docker=False)
    state = tmp_path / "state"
    state.mkdir()
    env = {"PATH": str(b), "FAKE_STATE": str(state), "FAKE_KEYS": json.dumps(ADDRS), "HOME": str(tmp_path),
           "DENDRA_KEYRING_DIR": str(_volume(tmp_path) / "cosmos"),
           "DENDRA_KEYRING_PASSPHRASE_FILE": str(tmp_path / "none"), "PYTHONDONTWRITEBYTECODE": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cases = {"registered": json.dumps({"miner": {"operator": ADDRS[MID0]}}),
             "other": json.dumps({"miner": {"operator": "dendra1else"}}),
             "proto3 no operator": json.dumps({"miner": {"miner_id": MID0}}),
             "absent": "notfound", "garbage": "not json", "not a miner": json.dumps({"x": 1})}
    seen = {}
    for name, raw in cases.items():
        (state / f"miner-{MID0}.json").write_text(raw)
        for key in (MID0, "unknown-key"):
            p = subprocess.run([sys.executable, "-", MID0, key], input=reg_py, cwd=str(MODEA), env=dict(os.environ),
                               capture_output=True, text=True, timeout=60)
            shell_word = next(x.split(" ", 1)[1] for x in p.stdout.splitlines() if x.startswith("DENDRA_REGISTRATION "))
            word, _why = CS.registration(MID0, key, "", CS._run, CS._address_default)
            assert word == shell_word, (name, key, word, shell_word)
            seen[(name, key)] = word
    assert seen[("registered", MID0)] == "registered" and seen[("proto3 no operator", MID0)] == "other"
    assert seen[("absent", MID0)] == "absent" and seen[("garbage", MID0)] == "unread"
    assert seen[("registered", "unknown-key")] == "unread"


def test_local_hosts_and_urls_are_read_as_publish_capacity_reads_them(tmp_path):
    fn = _between(PUBLISH.read_text(encoding="utf-8"), "_is_local_host(){", "_host_of(){")
    hosts = ["host.docker.internal", "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "10.1.2.3", "192.168.1.9",
             "172.16.0.1", "172.31.9.9", "172.32.0.1", "172.15.0.1", "dendra-node", "api.dendranetwork.com",
             "203.0.113.7", "fe80::1", "", "registry.bench.test"]
    urls = ["https://api.dendranetwork.com/capacity", "http://127.0.0.1:8092/capacity", "tcp://dendra-node:26657",
            "https://host:443/capacity", "tcp://203.0.113.7:26657", "https://dendra-node/capacity"]
    script = fn + "\n" + "\n".join(
        [f"_is_local_host '{h}' && echo L || echo P" for h in hosts] + [f"_host_of '{u}'; echo" for u in urls])
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    out = p.stdout.split("\n")
    assert [x == "L" for x in out[:len(hosts)]] == [CS.is_local_host(h) for h in hosts], (out, hosts)
    assert out[len(hosts):len(hosts) + len(urls)] == [CS.host_of(u) for u in urls]


def test_the_two_senders_name_the_same_file_and_the_same_phrase():
    src = PUBLISH.read_text(encoding="utf-8")
    assert f"INVENTORY_FILE=/data/keys/{CS.INVENTORY_NAME}\n" in src
    assert f'PAY_NOTE="{CS.PAY_NOTE}"\n' in src and CS.PAY_NOTE == PAY


# -- the entrypoint starts it beside the daemon, in every form of the container ---------------------------------
def test_the_entrypoint_starts_the_sender_for_the_resolved_identity_with_neither_worker(tmp_path):
    src = ENTRY.read_text(encoding="utf-8")
    lines = src.split("\n")
    start = lines.index('    EFF="$ID"')
    stop = next(i for i, l in enumerate(lines) if l.startswith("    # Only the miner is waited on"))
    body = "\n".join(lines[start:stop]).replace("/data/keys", "$KD") + "\nwait\n"
    assert "capacity_sender.py" in body
    (tmp_path / "block.sh").write_text(body)
    b = tmp_path / "wb"
    b.mkdir()
    for t in ("sh", "cat", "sed", "tr"):
        os.symlink(shutil.which(t), b / t)
    real_sleep = shutil.which("sleep")
    (b / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (b / "python3").write_text('#!/bin/sh\ncase "$1" in\n  capacity_sender.py|reveal_worker.py|judge_worker.py) '
                               'echo "started $1 $2 $3 $4 $5" >> "$WLOG"; exec "$REAL_SLEEP" 3 ;;\nesac\nexit 0\n')
    os.chmod(b / "sleep", 0o755)
    os.chmod(b / "python3", 0o755)
    kd = tmp_path / "kd"
    kd.mkdir()
    (kd / "resolved-identity").write_text("dm1bench\n")
    (kd / "dm1bench.sk").write_text("")
    for reveal in ("0", "1"):
        log = tmp_path / f"w{reveal}.log"
        env = {"PATH": str(b), "KD": str(kd), "WLOG": str(log), "ID": "m-0123", "REAL_SLEEP": real_sleep,
               "REVEAL_ON": reveal, "JUDGE_ON": "0", "RELAY": "http://relay"}
        subprocess.run(["sh", "-c", '"$REAL_SLEEP" 2 & MPID=$!; . "$0"', str(tmp_path / "block.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
        started = sorted(set(log.read_text().splitlines()))
        assert f"started capacity_sender.py --id dm1bench --keydir {kd}" in started, started
        assert any("reveal_worker.py" in s for s in started) == (reveal == "1"), started
    assert "exec python3 miner.py" not in src, "no form of the container runs the daemon without the sender"


# -- the messages ----------------------------------------------------------------------------------------------------
def _join_fn(name):
    lines = JOIN.read_text(encoding="utf-8").split("\n")
    i = lines.index(name + "(){")
    j = next(k for k in range(i + 1, len(lines)) if lines[k] == "}")
    return "\n".join(lines[i:j + 1])


def _join_run(tmp, calls, rc="0", crontab=False, **env_over):
    """Runs join.sh's delivered capacity functions against a fake publish-capacity.sh exiting `rc`."""
    repo = tmp / "repo"
    kit = repo / "deploy" / "testnet-miner"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / "publish-capacity.sh").write_text('#!/bin/bash\necho "PUBLISH $*" >> "$PLOG"\nexit "$FAKE_RC"\n')
    b = tmp / "jbin"
    b.mkdir(exist_ok=True)
    for t in ("bash", "sed", "grep", "cat", "mktemp", "rm", "cp", "tail", "printf"):
        if shutil.which(t) and not (b / t).exists():
            os.symlink(shutil.which(t), b / t)
    if crontab:
        (b / "crontab").write_text('#!/bin/bash\n[ "$1" = -l ] && { echo "no crontab for bench" >&2; exit 1; }\nexit 0\n')
        os.chmod(b / "crontab", 0o755)
    elif (b / "crontab").exists():
        (b / "crontab").unlink()
    fns = "\n".join(_join_fn(n) for n in ("capacity_url", "cron_add_line", "install_capacity_cron", "schedule_capacity",
                                          "publish_capacity_signed", "store_capacity_inventory", "capacity_after_join"))
    script = ("say(){ printf '%s\\n' \"$*\"; }\nwarn(){ printf 'WARN %s\\n' \"$*\"; }\n" + fns + "\n" + calls + "\n")
    env = {"PATH": str(b), "REPO": str(repo), "MINER_KIT": str(kit), "PLOG": str(tmp / "publish.log"),
           "FAKE_RC": rc, "HOME": str(tmp), "DENDRA_CAPACITY_URL": "https://api.example.net/capacity"}
    env.update(env_over)
    p = subprocess.run(["bash", "-c", 'REPO="$REPO"; MINER_KIT="$MINER_KIT"; ' + script], env=env,
                       capture_output=True, text=True, timeout=60)
    log = (tmp / "publish.log").read_text() if (tmp / "publish.log").exists() else ""
    (tmp / "publish.log").unlink(missing_ok=True)
    return p.stdout + p.stderr, log


def test_join_says_what_the_report_decides_and_who_sends_it(tmp_path):
    out, _ = _join_run(tmp_path, "schedule_capacity")
    assert PAY in out and "by itself" in out and "only affects the public capacity display" not in out, out
    out, _ = _join_run(tmp_path, "schedule_capacity", DENDRA_CAPACITY_URL="", DENDRA_NODE="tcp://203.0.113.7:26657")
    assert "WARN capacity registry URL unknown" in out and PAY in out.split("WARN")[1], out
    out, _ = _join_run(tmp_path, "install_capacity_cron")
    assert "no crontab on this host: the miner sends its signed capacity report by itself" in out and "drops off" not in out
    out, _ = _join_run(tmp_path, "install_capacity_cron", crontab=True)
    assert "capacity re-publication scheduled (crontab, hourly, minute 17" in out, out
    for rc in ("0", "1", "2", "3"):
        out, log = _join_run(tmp_path, "publish_capacity_signed", rc=rc)
        assert "the season pays nothing to an identity without one" in out and log == "PUBLISH \n", (rc, out, log)
        assert "hourly job" not in out, (rc, out)
        if rc != "0":
            assert "by itself" in out, (rc, out)


def test_after_the_join_the_inventory_is_stored_and_the_report_named_with_its_pay(tmp_path):
    out, log = _join_run(tmp_path, "MINER_REGISTERED=0; capacity_after_join")
    assert log == "PUBLISH --inventory\n" and PAY in out and "grep capacity" in out, (out, log)
    out, log = _join_run(tmp_path, "MINER_REGISTERED=1; capacity_after_join")
    assert log == "PUBLISH \n", log
    out, log = _join_run(tmp_path, "MULTI=1; capacity_after_join")
    assert log == "PUBLISH --inventory --slot 0\n", log
    out, log = _join_run(tmp_path, "MINER_REGISTERED=0; capacity_after_join", rc="1")
    assert "WARN the hardware inventory was not stored" in out and PAY in out, out


def test_the_entrypoint_and_the_sender_lines_name_the_pay():
    src = ENTRY.read_text(encoding="utf-8")
    assert "No signed capacity report is sent either, and " + PAY in src
    sent = CS.line_of({"outcome": "sent", "why": "ok", "kit_version": 4, "judge_role": "active"}, CS.PERIOD_S)
    assert sent.startswith("sent: ") and "next round in 60 min" in sent
    no = CS.line_of({"outcome": "absent", "why": "no miner"}, 300)
    assert no == "not sent (absent): no miner. " + PAY[0].upper() + PAY[1:] + "; next round in 5 min"


# -- a redirect is an answer, never a report filed ------------------------------------------------------------------
def test_a_redirect_of_the_registry_is_a_refusal_never_a_sent_report(monkeypatch):
    # urllib replays a 301, 302 or 303 as a GET without the body and reads its 200: the delivered poster must not.
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append(("POST", self.rfile.read(n)))
            self.send_response(int(self.path.rsplit("/", 1)[1]))
            self.send_header("Location", "/moved")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            seen.append(("GET", b""))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for code in (301, 302, 303, 307, 308):
            seen.clear()
            got = CS._post_default("http://127.0.0.1:%d/capacity/%d" % (srv.server_address[1], code), b'{"a":1}', {})
            assert got == (code, ""), (code, got)
            assert seen == [("POST", b'{"a":1}')], (code, seen)
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_302_round_is_retried_as_a_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv(CS.STATUS_FILE_ENV, str(tmp_path / "status.json"))
    vol = _volume(tmp_path)
    _inventory(vol)
    f = Fakes(posts=[302])
    waits = []
    res = CS.run_forever(MID0, str(vol), _envd(), f.deps(), sleep=waits.append, rounds=1)
    assert res[0]["outcome"] == "refused" and "HTTP 302" in res[0]["why"], res
    assert json.loads((tmp_path / "status.json").read_text())["sent_at"] is None


# -- every refusal of publish-capacity.sh says what it costs ---------------------------------------------------------
def test_each_refusal_of_the_manual_tool_says_the_season_pays_nothing(tmp_path, reg):
    b, (root, kit), vol = _bin(tmp_path), _kit(tmp_path), _volume(tmp_path)
    probe = root / "deploy" / "hw_probe.sh"
    good_probe = probe.read_text()
    nosmi = b / "nvidia-smi"

    def run(**over):
        env = _env(tmp_path, b, reg, vol, FAKE_KIT=str(kit), **over)
        p = subprocess.run(["bash", str(kit / "publish-capacity.sh")], env=env, capture_output=True, text=True,
                           timeout=180)
        return p.returncode, p.stdout + p.stderr

    cases = []
    # a local node and no registry address in the kit
    (kit / ".env").write_text("MINER_ID=m-bench\nDENDRA_NODE=tcp://dendra-node:26657\n")
    cases.append(("local node", run(DENDRA_CAPACITY_URL="")))
    # a registry address on a local host
    cases.append(("local url", run(DENDRA_CAPACITY_URL="http://127.0.0.1:9/capacity")))
    # the probe: unreadable, without a gpu_count, and no GPU while nvidia-smi lists one
    probe.write_text("#!/usr/bin/env bash\necho not-json\n")
    cases.append(("probe unreadable", run()))
    probe.write_text("#!/usr/bin/env bash\necho '{\"node_id\":\"m-bench\"}'\n")
    cases.append(("no gpu_count", run()))
    probe.write_text("#!/usr/bin/env bash\necho '{\"node_id\":\"m-bench\",\"gpu_count\":0}'\n")
    nosmi.write_text("#!/bin/sh\necho 'RTX bench, 12288 MiB'\n")
    os.chmod(nosmi, 0o755)
    cases.append(("no GPU declared", run()))
    nosmi.unlink()
    probe.write_text(good_probe)
    for name, (rc, out) in cases:
        assert rc == 1, (name, out)
        assert PAY in " ".join(out.split()), (name, out)
        assert "public chat" not in out, (name, out)
    assert reg.posts() == []


# -- the rented pod writes its own inventory -------------------------------------------------------------------------
CLOUD = REPO / "docker" / "cloud-start.sh"


def _cloud_fn():
    lines = CLOUD.read_text(encoding="utf-8").split("\n")
    i = lines.index("cloud_store_inventory(){")
    j = next(k for k in range(i + 1, len(lines)) if lines[k] == "}")
    return "\n".join(lines[i:j + 1])


def test_the_rented_pod_stores_the_inventory_its_sender_reads(tmp_path):
    b = tmp_path / "pbin"
    b.mkdir()
    for t in ("bash", "tail", "mv", "grep"):
        os.symlink(shutil.which(t), b / t)
    os.symlink(sys.executable, b / "python3")
    probe, kd = tmp_path / "probe.sh", tmp_path / "keys"
    kd.mkdir()
    script = ("cloud_say(){ printf '[cloud] %s' \"$*\"; echo; }\n" + _cloud_fn() +
              '\ncloud_store_inventory "$KD" m-pod; echo "RC=$?"\n')

    def run(probe_out, smi=False):
        probe.write_text("#!/usr/bin/env bash\necho '" + probe_out + "'\n")
        smi_path = b / "nvidia-smi"
        if smi:
            smi_path.write_text("#!/bin/sh\necho 'RTX pod'\n")
            os.chmod(smi_path, 0o755)
        elif smi_path.exists():
            smi_path.unlink()
        inv = kd / CS.INVENTORY_NAME
        if inv.exists():
            inv.unlink()
        p = subprocess.run(["bash", "-c", script], env={"PATH": str(b), "KD": str(kd), "CLOUD_PROBE": str(probe)},
                           capture_output=True, text=True, timeout=60)
        return p.stdout + p.stderr, (inv.read_text() if inv.exists() else None)

    good = '{"node_id":"m-pod","backend":"gpu","gpu_count":1,"vram_mb":24576}'
    out, inv = run(good)
    assert "RC=0" in out and inv == good + "\n", out
    doc, why = CS.read_inventory(str(kd / CS.INVENTORY_NAME))
    assert doc["gpu_count"] == 1 and why == ""
    for bad, smi in (('{"node_id":"m-pod"}', False), ("not json", False), ('{"node_id":"m-pod","gpu_count":"1"}', False),
                     ('{"node_id":"m-pod","gpu_count":0}', True)):
        out, inv = run(bad, smi)
        assert "RC=1" in out and inv is None and PAY in out.lower(), (bad, out)
    out, inv = run('{"node_id":"m-pod","gpu_count":0}', False)
    assert "RC=0" in out and inv is not None, out
    # the pod calls it before the miner starts, and hands the sender a registry address
    src = CLOUD.read_text(encoding="utf-8")
    call = src.index('\ncloud_store_inventory "$CLOUD_KEYS" "$MINER_ID"\n')
    start = src.index('exec setsid env "${CLOUD_MINER_ENV[@]}"')
    assert call < start and '  DENDRA_CAPACITY_URL="$CLOUD_CAPACITY_URL"\n' in src
    assert 'CLOUD_CAPACITY_URL="$(capacity_url)"' in src
