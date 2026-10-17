# The faucet and the gateway sign from two keyrings, each on a volume that one service alone mounts
# (docker-compose.yml: faucet-keys, gateway-keys). Each adopts its key once from the chain's shared keyring
# (docker/entrypoint-services.sh::adopt_service_key) and is refused a start while its keyring lacks that key, holds
# another one, or holds a key the shared keyring does not know under that name.
#
# The cases run the delivered entrypoint (or its delivered service-key block) with sh, on fabricated keyrings, with
# a hermetic PATH: `dendrad` there is a stand-in that reads the address written on the first line of a fabricated
# record, so no machine's real dendrad or keyring is ever read. A real record is encrypted; the copy does not look
# inside it, so a fabricated one measures the same thing. A second line in each record stands for the secret, and
# no case lets it reach the output.
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MODEA = Path(os.environ.get("DENDRA_MODEA") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(MODEA))
from modea import cosmos_addr as ca  # noqa: E402

NL = chr(10)
SECRET = "fabricated-secret-line-that-must-never-be-printed"
BLOCK_START = "# >>> service-key"
BLOCK_END = "# <<< service-key"


def _carries(d):
    return (d / "docker" / "entrypoint-services.sh").is_file() and (d / "docker-compose.yml").is_file()


def _root():
    """The tree that carries both the service code and docker/ (the private tree and the published one differ).
    DENDRA_REPO names it when this directory runs away from it (the battery runs pytest from a copy of prototype/).
    A DENDRA_REPO that does not carry both files fails: a measurement was asked for and cannot be made."""
    named = os.environ.get("DENDRA_REPO") or ""
    if named:
        assert _carries(Path(named)), "DENDRA_REPO=" + named + " does not carry docker/entrypoint-services.sh and docker-compose.yml"
        return Path(named)
    for d in [MODEA, *MODEA.parents]:
        if _carries(d):
            return d
    return None


ROOT = _root()
if ROOT is None:
    pytest.skip("docker/entrypoint-services.sh and docker-compose.yml are not found above " + str(MODEA)
                + ": the service-key cases are not run (declared, not passed); DENDRA_REPO names the tree that carries them",
                allow_module_level=True)
SH = shutil.which("sh")


def _delivered_entrypoint():
    return (ROOT / "docker" / "entrypoint-services.sh").read_bytes().replace(bytes([13]), b"").decode()


def _delivered_block():
    lines = _delivered_entrypoint().split(NL)
    starts = [i for i, l in enumerate(lines) if l == BLOCK_START]
    ends = [i for i, l in enumerate(lines) if l == BLOCK_END]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0], (starts, ends)
    block = lines[starts[0]:ends[0] + 1]
    assert len(block) < 150, "the extraction is not bounded by its markers"
    return NL.join(block) + NL


FAKE_DENDRAD = """#!{py}
import os, sys
a = sys.argv[1:]
log = os.environ.get("FAKE_DENDRAD_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(a) + chr(10))
if a[:2] != ["keys", "show"] or "-a" not in a or "--keyring-backend" not in a or "--keyring-dir" not in a:
    sys.exit(64)
name, backend, kdir = a[2], a[a.index("--keyring-backend") + 1], a[a.index("--keyring-dir") + 1]
if backend not in ("test", "file"):
    sys.exit(64)
if backend == "file":
    # An encrypted keyring opens with the passphrase dendrad reads on stdin, one line per prompt.
    given = sys.stdin.read().split(chr(10))[0]
    if not given or given != os.environ.get("FAKE_PASSPHRASE", ""):
        print("Error: " + name + " is not a valid name or address: too many failed passphrase attempts", file=sys.stderr)
        sys.exit(1)
try:
    first = open(os.path.join(kdir, "keyring-" + backend, name + ".info")).read().split(chr(10))[0]
except OSError:
    sys.exit(1)
if not first.startswith("dendra1"):
    sys.exit(1)
print(first)
"""


class Box:
    def __init__(self, tmp):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for tool in ("cp", "mv", "mkdir", "chmod", "rm"):
            os.symlink(shutil.which(tool), self.bin / tool)
        os.symlink(sys.executable, self.bin / "python3")
        d = self.bin / "dendrad"
        d.write_text(FAKE_DENDRAD.format(py=sys.executable))
        d.chmod(0o755)
        self.log = tmp / "dendrad.log"
        self.entry = tmp / "entrypoint.sh"
        self.entry.write_text(_delivered_entrypoint())
        self.block = tmp / "block.sh"
        self.block.write_text(_delivered_block())
        self.seed = tmp / "seed"
        self.own = tmp / "own"
        self.seed.mkdir()
        self.own.mkdir()

    def env(self, **kw):
        # The passphrase file is named, and absent unless a case writes it: the machine's own is never read.
        e = {"PATH": str(self.bin), "HOME": str(self.tmp), "FAKE_DENDRAD_LOG": str(self.log),
             "DENDRA_HOME": str(self.own), "DENDRA_KEY_SEED_HOME": str(self.seed),
             "DENDRA_SERVICE_KEYS_SEED": "service-keys", "DENDRA_KEYRING_PASSPHRASE_FILE": str(self.tmp / "passphrase")}
        e.update(kw)
        return {k: v for k, v in e.items() if v is not None}

    def run(self, role, **kw):
        return subprocess.run([SH, str(self.entry), role], cwd=str(MODEA), env=self.env(**kw),
                              capture_output=True, text=True, timeout=30)

    def call(self, shell, **kw):
        return subprocess.run([SH, "-c", ". " + str(self.block) + "; " + shell], cwd=str(MODEA),
                              env=self.env(**kw), capture_output=True, text=True, timeout=30)

    def dendrad_calls(self, flag):
        """The value of `flag` in each dendrad call, in order."""
        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text().split(NL):
            a = line.split(" ")
            if flag in a:
                out.append(a[a.index(flag) + 1])
        return out


def make_key(home, name, index=True, backend="test"):
    """A fabricated keyring entry: <name>.info and its address index <hex>.address, in keyring-<backend>."""
    raw = os.urandom(20)
    addr = ca.bech32_encode("dendra", ca._convertbits(raw, 8, 5))
    kr = home / ("keyring-" + backend)
    kr.mkdir(parents=True, exist_ok=True)
    (kr / (name + ".info")).write_text(addr + NL + SECRET + NL)
    if index:
        (kr / (raw.hex() + ".address")).write_text("index of " + name)
    return addr, raw.hex()


def shared_keyring(box):
    """What the chain leaves in `service-keys`: both keys, the mnemonic backup and the faucet's ledger."""
    bob = make_key(box.seed, "bob")
    gw = make_key(box.seed, "gw")
    (box.seed / "keys-backup.jsonl").write_text('{"name":"bob","mnemonic":"' + SECRET + '"}' + NL)
    (box.seed / "faucet-state.json").write_text('{"addr_last": {}}')
    return bob, gw


def files(home, backend="test"):
    kr = home / ("keyring-" + backend)
    return sorted(p.name for p in kr.iterdir()) if kr.is_dir() else []


@pytest.fixture
def box(tmp_path):
    assert SH, "sh is required"
    return Box(tmp_path)


def test_the_faucet_adopts_its_key_and_its_ledger_and_nothing_else(box):
    (bob_addr, bob_hex), (gw_addr, _gw_hex) = shared_keyring(box)
    # PUBLIC=1 without PoW: the faucet stops on its own refusal, which it reaches only past the key stage.
    r = box.run("faucet", DENDRA_FAUCET_FROM="bob", DENDRA_PUBLIC="1", DENDRA_FAUCET_POW_BITS="0")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "requires DENDRA_FAUCET_POW_BITS" in out, out
    assert "key 'bob' adopted from the shared keyring" in out and bob_addr in out, out
    assert "signing key 'bob' (" + bob_addr + ") is the only key" in out, out
    assert "warning: the shared keyring is mounted" in out, out
    assert files(box.own) == sorted(["bob.info", bob_hex + ".address"])
    assert (box.own / "keyring-test" / "bob.info").read_bytes() == (box.seed / "keyring-test" / "bob.info").read_bytes()
    assert (box.own / "faucet-state.json").read_bytes() == (box.seed / "faucet-state.json").read_bytes()
    assert not (box.own / "keys-backup.jsonl").exists()
    assert SECRET not in out and gw_addr not in out
    assert str(box.seed) not in box.dendrad_calls("--keyring-dir"), "dendrad never runs on the read-only shared keyring"
    assert box.dendrad_calls("--keyring-backend") == ["test"], "the backend is the one the disk holds"


def test_the_gateway_adopts_gw_and_never_the_faucets_key(box):
    (bob_addr, _), (gw_addr, gw_hex) = shared_keyring(box)
    r = box.call('adopt_service_key gateway "$(gateway_key_name)"', DENDRA_SUBSIDY_CLIENT="gw")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert files(box.own) == sorted(["gw.info", gw_hex + ".address"])
    assert "signing key 'gw' (" + gw_addr + ") is the only key" in out
    assert not (box.own / "faucet-state.json").exists()
    assert SECRET not in out and bob_addr not in out


def test_without_the_variable_the_seed_mount_is_not_read(box):
    # Second recreation: the key is in place; /seed is the service's own volume in compose, here a keyring holding
    # another `bob`, which would be refused if it were compared. Without the variable nothing compares or copies.
    addr, _ = make_key(box.own, "bob")
    make_key(box.seed, "bob")
    (box.seed / "faucet-state.json").write_text("{}")
    r = box.call("adopt_service_key faucet bob && adopt_faucet_ledger", DENDRA_SERVICE_KEYS_SEED="")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "signing key 'bob' (" + addr + ") is the only key" in out
    assert "adopted" not in out and "shared keyring is mounted" not in out and "ledger" not in out
    assert not (box.own / "faucet-state.json").exists()


def test_a_gateway_without_its_key_and_without_the_seed_is_refused(box):
    shared_keyring(box)
    r = box.run("gateway", DENDRA_SERVICE_KEYS_SEED="", DENDRA_SUBSIDY_CLIENT="gw")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "refusing to start" in out and "DENDRA_SERVICE_KEYS_SEED=service-keys" in out, out
    assert files(box.own) == []
    assert "gateway_fund" not in out


def test_a_keyring_holding_a_second_key_is_refused(box):
    make_key(box.own, "bob")
    make_key(box.own, "gw")
    r = box.run("faucet", DENDRA_SERVICE_KEYS_SEED="", DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "holds other keys than 'bob': gw" in out, out
    assert "listening" not in out


def test_a_key_from_another_genesis_is_refused_and_left_as_it_was(box):
    shared_keyring(box)
    stale, _ = make_key(box.own, "bob")
    before = (box.own / "keyring-test" / "bob.info").read_bytes()
    r = box.run("faucet", DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "is not the shared keyring's 'bob'" in out and stale in out, out
    assert (box.own / "keyring-test" / "bob.info").read_bytes() == before


def test_an_adopted_record_the_shared_keyring_does_not_index_is_rolled_back(box):
    make_key(box.seed, "bob", index=False)
    r = box.run("faucet", DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "is not the shared keyring's 'bob'" in out, out
    assert files(box.own) == []


def test_a_record_dendrad_cannot_read_is_refused(box):
    kr = box.own / "keyring-test"
    kr.mkdir()
    (kr / "bob.info").write_text("not an address" + NL)
    r = box.run("faucet", DENDRA_SERVICE_KEYS_SEED="", DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "does not read key 'bob'" in out, out


# --- The backend is read from the disk (modea/keyring.py::disk_state), never named by the entrypoint.
PASS = "fabricated passphrase"


def test_an_encrypted_keyring_is_read_as_file_with_its_passphrase(box):
    addr, _ = make_key(box.own, "bob", backend="file")
    (box.tmp / "passphrase").write_text(PASS + NL)
    r = box.call("adopt_service_key faucet bob", DENDRA_SERVICE_KEYS_SEED="", FAKE_PASSPHRASE=PASS)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "signing key 'bob' (" + addr + ") is the only key in " + str(box.own / "keyring-file") in out, out
    assert box.dendrad_calls("--keyring-backend") == ["file"]
    assert PASS not in out and SECRET not in out


def test_an_encrypted_keyring_without_its_passphrase_is_refused(box):
    make_key(box.own, "bob", backend="file")
    r = box.call("adopt_service_key faucet bob", DENDRA_SERVICE_KEYS_SEED="", FAKE_PASSPHRASE=PASS)
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "does not read key 'bob'" in out and "passphrase missing" in out, out


def test_a_wrong_passphrase_is_refused_and_named(box):
    make_key(box.own, "bob", backend="file")
    (box.tmp / "passphrase").write_text("another passphrase" + NL)
    r = box.call("adopt_service_key faucet bob", DENDRA_SERVICE_KEYS_SEED="", FAKE_PASSPHRASE=PASS)
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "passphrase is missing or wrong" in out, out
    assert "another passphrase" not in out


@pytest.mark.parametrize("where", ["own", "seed"])
def test_two_keyrings_under_one_home_are_refused(box, where):
    home = box.own if where == "own" else box.seed
    make_key(home, "bob")
    make_key(home, "bob", backend="file")
    before = (files(box.own), files(box.own, "file"))
    r = box.run("faucet", DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "keyring-test and keyring-file under " + str(home) + " both hold keys" in out, out
    assert (files(box.own), files(box.own, "file")) == before
    assert box.dendrad_calls("--keyring-dir") == []


def test_the_adoption_follows_the_shared_keyrings_backend(box):
    _, hx = make_key(box.seed, "bob", backend="file")
    make_key(box.seed, "gw", backend="file")
    (box.tmp / "passphrase").write_text(PASS + NL)
    r = box.call("adopt_service_key faucet bob", FAKE_PASSPHRASE=PASS)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert files(box.own, "file") == sorted(["bob.info", hx + ".address"]) and files(box.own) == []
    assert box.dendrad_calls("--keyring-backend") == ["file"]


def test_a_service_keyring_and_a_shared_keyring_of_two_backends_are_refused(box):
    shared_keyring(box)
    make_key(box.own, "bob", backend="file")
    r = box.run("faucet", DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "keyring is keyring-file and the shared keyring is keyring-test" in out, out
    assert files(box.own) == []


def test_an_empty_home_without_the_seed_names_both_directories(box):
    r = box.call("adopt_service_key faucet bob", DENDRA_SERVICE_KEYS_SEED="")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "neither keyring-test nor keyring-file holds a key" in out, out
    assert box.dendrad_calls("--keyring-dir") == []


@pytest.mark.parametrize("value", ["dendra-home", "programme-keys", "gateway-keys", "/root"])
def test_a_seed_naming_another_volume_is_refused(box, value):
    shared_keyring(box)
    r = box.run("faucet", DENDRA_SERVICE_KEYS_SEED=value, DENDRA_FAUCET_FROM="bob")
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "names a volume other than service-keys" in out, out
    assert files(box.own) == []


@pytest.mark.parametrize("role,env", [("faucet", {"DENDRA_FAUCET_FROM": None}),
                                      ("faucet", {"DENDRA_FAUCET_FROM": "bob;rm"}),
                                      ("gateway", {"DENDRA_SUBSIDY_CLIENT": None})])
def test_an_unnamed_key_is_refused_and_never_defaulted(box, role, env):
    shared_keyring(box)
    r = box.run(role, **env)
    out = r.stdout + r.stderr
    assert r.returncode == 2 and "no valid signing key name" in out, out
    assert files(box.own) == []


@pytest.mark.parametrize("free_tier", [None, "1", "0", "", "true"])
def test_the_gateway_key_is_the_one_the_gateway_signs_with(box, free_tier):
    env = {"DENDRA_SUBSIDY_CLIENT": "gw", "DENDRA_CLIENT": "paid", "DENDRA_FREE_TIER": free_tier}
    shell = box.call("gateway_key_name", **env).stdout
    # The gateway's own reading of the same environment (gateway.py, FREE_TIER and the two client names).
    e = {k: v for k, v in os.environ.items() if not k.startswith("DENDRA_")}
    e.update({k: v for k, v in env.items() if v is not None})
    e["DENDRA_GW_HOST"] = "127.0.0.1"
    py = subprocess.run([sys.executable, "-c", "import gateway as g; print(g.SUBSIDY_CLIENT if g.FREE_TIER else g.PAID_CLIENT)"],
                        cwd=str(MODEA), env=e, capture_output=True, text=True, timeout=60)
    assert py.returncode == 0, py.stderr
    assert shell == py.stdout.strip().split(NL)[-1], (shell, py.stdout)


def _mounts(service):
    """(source, target, read-only) of each short-syntax mount; a source may be `${VAR:-default}`, colon included."""
    out = []
    for v in service.get("volumes", []):
        assert isinstance(v, str), v
        if v.startswith("${"):
            end = v.index("}") + 1
            src, rest = v[:end], v[end + 1:]
        else:
            src, rest = v.split(":", 1)
        out.append((src, rest.split(":")[0], v.endswith(":ro")))
    return out


def _default(src):
    """The volume compose mounts when the .env leaves DENDRA_SERVICE_KEYS_SEED empty."""
    pre = "${DENDRA_SERVICE_KEYS_SEED:-"
    return src[len(pre):-1] if src.startswith(pre) and src.endswith("}") else src


def test_compose_gives_each_keyring_to_one_service():
    try:
        import yaml
    except ImportError:
        pytest.fail("PyYAML is required: without it this half of the guard would report nothing")
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    svc = c["services"]
    for name, vol in (("faucet", "faucet-keys"), ("gateway", "gateway-keys")):
        m = _mounts(svc[name])
        assert (vol, "/root/.dendra", False) in m, m
        assert ("${DENDRA_SERVICE_KEYS_SEED:-" + vol + "}", "/seed", True) in m, m
        env = svc[name]["environment"]
        assert env["DENDRA_HOME"] == "/root/.dendra" and env["DENDRA_KEY_SEED_HOME"] == "/seed"
        assert env["DENDRA_SERVICE_KEYS_SEED"] == "${DENDRA_SERVICE_KEYS_SEED:-}"
        mounted = {_default(s) for s, _, _ in m}
        assert mounted.isdisjoint({"service-keys", "dendra-home", "programme-keys"}), mounted
        assert vol in c["volumes"]
        users = sorted(n for n, s in svc.items() if vol in {_default(x) for x, _, _ in _mounts(s)})
        assert users == [name], users
    assert svc["faucet"]["environment"]["DENDRA_FAUCET_FROM"] == "bob"
    assert svc["gateway"]["environment"]["DENDRA_SUBSIDY_CLIENT"] == "gw"


# --- The gateway's start with the free tier on and off: gateway_fund funds DENDRA_SUBSIDY_CLIENT only. Stand-ins for
# the two programs print a marker, so the delivered entrypoint runs to its exec without starting a gateway.
STUB = "import sys; print('stub ' + sys.argv[0]); sys.exit(0)" + NL


@pytest.mark.parametrize("free_tier,key,funded", [("1", "gw", True), (None, "gw", True), ("0", "paid", False)])
def test_gateway_fund_runs_only_for_the_free_tier_account(box, free_tier, key, funded):
    app = box.tmp / "app"
    app.mkdir()
    (app / "gateway_fund.py").write_text(STUB)
    (app / "gateway.py").write_text(STUB)
    make_key(box.own, key)
    env = box.env(DENDRA_SERVICE_KEYS_SEED="", DENDRA_SUBSIDY_CLIENT="gw", DENDRA_CLIENT="paid",
                  DENDRA_FREE_TIER=free_tier, PYTHONPATH=str(MODEA))
    r = subprocess.run([SH, str(box.entry), "gateway"], cwd=str(app), env=env, capture_output=True, text=True, timeout=30)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "signing key '" + key + "'" in out and "stub gateway.py" in out, out
    assert ("stub gateway_fund.py" in out) == funded, out
    assert ("gateway_fund skipped" in out) == (not funded), out


# --- deploy/launch/launch_public.sh: the seed default (step 4) and the second recreation (step 7b). The delivered
# remote commands are extracted and run on a fabricated host directory, with a stand-in `docker`; the delivered
# `_sk_check` is extracted and run against fabricated answers. Nothing reaches a host.
Q = chr(34)
LAUNCHER = ROOT / "deploy" / "launch" / "launch_public.sh"
SEED_SET = "$RSH 'cd ~/dendra && sed -i " + Q + "/^DENDRA_SERVICE_KEYS_SEED=/d" + Q + " .env && { echo;"
SEED_CLEAR = "$RSH 'cd ~/dendra && sed -i " + Q + "/^DENDRA_SERVICE_KEYS_SEED=/d" + Q + " .env && [ " + Q + "$(grep -c"


def _launcher_lines():
    return LAUNCHER.read_bytes().replace(bytes([13]), b"").decode().split(NL)


def _remote(prefix):
    """The single-quoted remote command of the one delivered line starting with prefix."""
    hits = [l for l in _launcher_lines() if l.startswith(prefix)]
    assert len(hits) == 1, (prefix, hits)
    line = hits[0]
    start = line.index("'") + 1
    return line[start:line.index("' ", start)]


DOCKER_STUB = """#!{py}
import json, os, sys
a = sys.argv[1:]
with open(os.environ["DOCKER_LOG"], "a") as f:
    f.write(" ".join(a) + chr(10))
s = json.load(open(os.environ["DOCKER_SCENE"])) if os.environ.get("DOCKER_SCENE") else {{}}
if a[:3] == ["compose", "exec", "-T"]:
    svc, rest = a[3], a[4:]
    if rest[:3] == ["dendrad", "keys", "show"]:
        v = s.get(svc, {{}}).get("addr")
        print(v) if v else sys.exit(1)
    elif rest[:1] == ["sh"]:
        print(s.get(svc, {{}}).get("records", ""))
    sys.exit(0)
if a[:3] == ["compose", "ps", "-q"]:
    print("cid-" + a[3])
    sys.exit(0)
if a[:1] == ["inspect"]:
    print(s.get(a[-1][4:], {{}}).get("mounts", ""))
    sys.exit(0)
if a[:2] == ["compose", "logs"]:
    print(s.get(a[2], {{}}).get("log", ""))
    sys.exit(0)
sys.exit(0)
"""


@pytest.fixture
def host(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("sh", "bash", "sed", "grep", "tr", "cat"):
        os.symlink(shutil.which(tool), bin_ / tool)
    d = bin_ / "docker"
    d.write_text(DOCKER_STUB.format(py=sys.executable))
    d.chmod(0o755)
    (tmp_path / "dendra").mkdir()
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {"PATH": str(bin_), "HOME": str(tmp_path), "DOCKER_LOG": str(log)}
    return tmp_path, env


def _env_lines(home):
    return (home / "dendra" / ".env").read_text().split(NL)


def test_the_launcher_sets_the_seed_once_before_its_up_and_clears_it_at_7b(host):
    home, env = host
    lines = _launcher_lines()

    def at(pred):
        hits = [i for i, l in enumerate(lines) if pred(l)]
        assert len(hits) == 1, hits
        return hits[0]
    s4 = at(lambda l: l.startswith('step "4)'))
    seed = at(lambda l: l.startswith(SEED_SET))
    up = at(lambda l: l.startswith('$RSH "cd ~/dendra && docker compose --profile public up -d --no-build"'))
    s7b = at(lambda l: l.startswith('step "7b)'))
    clear = at(lambda l: l.startswith(SEED_CLEAR))
    s7c = at(lambda l: l.startswith('step "7c)'))
    assert s4 < seed < up < s7b < clear < s7c

    # The operator's file may carry the variable empty, set, or not at all: one seed line comes out.
    for before in ("A=1" + NL + "DENDRA_SERVICE_KEYS_SEED=" + NL, "A=1" + NL + "DENDRA_SERVICE_KEYS_SEED=service-keys" + NL, "A=1"):
        (home / "dendra" / ".env").write_text(before)
        r = subprocess.run(["sh", "-c", _remote(SEED_SET)], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        got = [l for l in _env_lines(home) if l.startswith("DENDRA_SERVICE_KEYS_SEED")]
        assert got == ["DENDRA_SERVICE_KEYS_SEED=service-keys"], got
        assert "A=1" in _env_lines(home)

    r = subprocess.run(["sh", "-c", _remote(SEED_CLEAR)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert not [l for l in _env_lines(home) if l.startswith("DENDRA_SERVICE_KEYS_SEED")]
    assert "A=1" in _env_lines(home)
    calls = (home / "docker.log").read_text().split(NL)
    assert "compose up -d --no-deps --no-build faucet gateway" in calls, calls


def _sk_check_function():
    lines = _launcher_lines()
    starts = [i for i, l in enumerate(lines) if l.startswith("_sk_check(){")]
    assert len(starts) == 1, starts
    ends = [i for i in range(starts[0], len(lines)) if lines[i] == "}"]
    assert ends and ends[0] - starts[0] < 30, "the extraction is not bounded"
    return NL.join(lines[starts[0]:ends[0] + 1]) + NL


# Docker compose names a volume <project>_<volume>, and the host's project is `dendra` (its directory ~/dendra).
# The names are assembled from that project because the publication renames service modules by plain text
# substitution, and a volume name written in one piece would be renamed with them.
PROJECT = "dendra"


def _vol(name):
    return PROJECT + "_" + name


GOOD = {"faucet": {"addr": "dendra1bob", "records": "bob.info", "mounts": _vol("faucet-keys") + "," + _vol("faucet-keys") + ",",
                   "log": "[faucet] signing key 'bob' (dendra1bob) is the only key in /root/.dendra/keyring-test"},
        "gateway": {"addr": "dendra1gw", "records": "gw.info",
                    "mounts": ",".join([_vol("gateway-keys"), _vol("gateway-keys"), _vol("gw-quota")]) + ",",
                    "log": "[gateway] signing key 'gw' (dendra1gw) is the only key in /root/.dendra/keyring-test"}}


def _bad(svc, **kw):
    s = json.loads(json.dumps(GOOD))
    s[svc].update(kw)
    return s


@pytest.mark.parametrize("scene,expect", [
    (GOOD, ""),
    (_bad("faucet", mounts=_vol("faucet-keys") + "," + _vol("service-keys") + ","), "faucet still mounts dendra_service-keys"),
    (_bad("gateway", records="bob.info gw.info"), "gateway holds the key records 'bob.info gw.info'"),
    (_bad("gateway", addr="dendra1other"), "gateway reads its key 'gw' as 'dendra1other'"),
    (_bad("faucet", addr=None), "faucet reads its key 'bob' as 'nothing'"),
    (_bad("faucet", mounts=""), "faucet mounts 'nothing readable'"),
    (_bad("gateway", log="[gateway] warning: the shared keyring is mounted read-only at /seed"), "gateway logged 1 refusal"),
    (_bad("faucet", log="[faucet] refusing to start: key 'bob' is not in this service's keyring"), "faucet logged 1 refusal"),
])
def test_the_7b_gate_reads_each_container(host, scene, expect):
    home, env = host
    sc = home / "scene.json"
    sc.write_text(json.dumps(scene))
    env = dict(env, DOCKER_SCENE=str(sc), RSH="sh -c")
    fn = home / "sk_check.sh"
    fn.write_text(_sk_check_function())
    r = subprocess.run(["bash", "-c", ". " + str(fn) + '; _sk_check faucet bob dendra1bob; _sk_check gateway gw dendra1gw'],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    if expect:
        assert expect in r.stdout, r.stdout
    else:
        assert r.stdout == "", r.stdout


def test_the_reset_purge_names_the_three_service_keyrings():
    p = ROOT / "dendra" / "onchain-staging" / "dendra_reset_total.sh"
    if not p.is_file():
        pytest.skip("the reset script is not part of the published tree")
    lines = p.read_bytes().replace(bytes([13]), b"").decode().split(NL)
    purge = [l for l in lines if "docker volume rm $VOL_HOME" in l]
    assert len(purge) == 1, purge
    defs = NL.join(l for l in lines if l.startswith("VOL_"))
    r = subprocess.run(["sh", "-c", defs + NL + 'echo $VOL_HOME $VOL_RELAY $VOL_SVC $VOL_KEYS'],
                       capture_output=True, text=True, timeout=10)
    named = set(r.stdout.split())
    assert {_vol("service-keys"), _vol("faucet-keys"), _vol("gateway-keys")} <= named, named
    assert "$VOL_SVC $VOL_KEYS" in purge[0], purge
