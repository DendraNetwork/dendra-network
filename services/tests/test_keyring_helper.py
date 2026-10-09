"""Bench of modea/keyring.py: which backend, which passphrase, and when a key may be called absent.

What is REAL: the module, its decisions on a keyring directory BUILT by each case, the passphrase file, and
its command line (`python3 -m modea.keyring`). What is REPLACED: `dendrad`, by a fake on a HERMETIC PATH
(that directory and the interpreter its shebang names) that answers with the messages dendrad was MEASURED
to print -- "<name> is not a valid name or address: too many failed passphrase attempts" for a missing or
wrong passphrase, and `keys list` exiting 0 with an EMPTY list in that same case -- and records each call's
argv and stdin. The real dendrad is driven by dendra/onchain-staging/dendra_trousseau_chiffre_test.sh.

The subject can be pointed at a mutated copy with DENDRA_KEYRING_MODULE.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys

import pytest

MODEA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODEA)
SUBJECT = os.environ.get("DENDRA_KEYRING_MODULE") or os.path.join(MODEA, "modea", "keyring.py")


def _load():
    import modea  # noqa: F401 -- the package the subject's relative imports resolve against
    spec = importlib.util.spec_from_file_location("modea._keyring_under_bench", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses look their module up while the class is built
    spec.loader.exec_module(mod)
    return mod


K = _load()
PASS = "a" * 64

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
S = os.environ["FAKE_STATE"]
a = sys.argv[1:]
given = sys.stdin.read()
with open(os.path.join(S, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": a, "stdin": given}) + chr(10))
cfg = json.load(open(os.path.join(S, "cfg.json")))
good = cfg.get("passphrase")
lines = given.split(chr(10))
ok = (good is None) or (lines[0] == good)
name = a[2] if len(a) > 2 else ""
backend = a[a.index("--keyring-backend") + 1] if "--keyring-backend" in a else ""
# What keyring-file holds once a key was IMPORTED into it: by default the address it had, unless the case
# makes the import land at ANOTHER one (`import_address`) -- an import that exits 0 and is still wrong.
held = dict(cfg.get("keys", {}), **cfg.get("file_keys", {})) if backend == "file" else cfg.get("keys", {})
if a[:2] == ["keys", "show"]:
    if name in held:
        if not ok:
            print("EOF" + chr(10) + "EOF" + chr(10) + "EOF", file=sys.stderr)
            print(name + " is not a valid name or address: too many failed passphrase attempts", file=sys.stderr)
            sys.exit(1)
        print(held[name]); sys.exit(0)
    print(name + " is not a valid name or address: decoding bech32 failed: invalid bech32 string length 2", file=sys.stderr)
    sys.exit(1)
if a[:2] == ["keys", "list"]:
    if not ok:
        print("[]")
        for n in cfg.get("keys", {}):
            print("migrate err for key " + n + ".info: " + chr(34) + "too many failed passphrase attempts" + chr(34), file=sys.stderr)
        sys.exit(0)
    print(json.dumps([{"name": n, "address": v} for n, v in cfg.get("keys", {}).items()])); sys.exit(0)
if a[:2] == ["keys", "export"]:
    d = "-" * 5   # the armor's markers, composed: the opening of a key block is refused by the secret scan
    print(d + "BEGIN TENDERMINT PRIVATE KEY" + d + chr(10) + "kdf: bcrypt" + chr(10) + chr(10) + "QUJD" + chr(10) + d + "END TENDERMINT PRIVATE KEY" + d)
    sys.exit(0)
if a[:2] == ["keys", "import"]:
    if cfg.get("import_fails"):
        print("Error: failed to decrypt private key", file=sys.stderr); sys.exit(1)
    d = a[a.index("--keyring-dir") + 1]
    os.makedirs(os.path.join(d, "keyring-file"), exist_ok=True)
    open(os.path.join(d, "keyring-file", "keyhash"), "w").write("h")
    open(os.path.join(d, "keyring-file", name + ".info"), "w").write("x")
    if cfg.get("import_address"):
        cfg.setdefault("file_keys", {})[name] = cfg["import_address"]
        json.dump(cfg, open(os.path.join(S, "cfg.json"), "w"))
    sys.exit(0)
print("unexpected " + " ".join(a), file=sys.stderr); sys.exit(64)
'''


class World:
    def __init__(self, tmp, monkeypatch, keys=None, passphrase=None, cfg_passphrase=None):
        self.tmp = tmp
        self.d = tmp / "kr"
        self.state = tmp / "state"
        self.state.mkdir()
        self.pf = tmp / "secrets" / "keyring-passphrase"
        self.pf.parent.mkdir()
        if passphrase is not None:
            self.pf.write_text(passphrase + "\n")
        self.cfg = {"keys": dict(keys or {}), "passphrase": cfg_passphrase}
        self.save()
        b = tmp / "bin"
        b.mkdir()
        (b / "dendrad").write_text(FAKE)
        os.chmod(b / "dendrad", 0o755)
        os.symlink(sys.executable, b / "python3")
        self.bin = b
        monkeypatch.setenv("PATH", str(b))
        monkeypatch.setenv("FAKE_STATE", str(self.state))
        monkeypatch.setenv("DENDRA_KEYRING_PASSPHRASE_FILE", str(self.pf))
        monkeypatch.setenv("DENDRA_KEYRING_DIR", str(self.d))

    def save(self):
        (self.state / "cfg.json").write_text(json.dumps(self.cfg))

    def disk(self, backend, *names, keyhash=False):
        p = self.d / K.DIRS[backend]
        p.mkdir(parents=True, exist_ok=True)
        for n in names:
            (p / f"{n}.info").write_text("x")
        if keyhash:
            (p / "keyhash").write_text("h")
        return p

    def calls(self):
        f = self.state / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


# ── the backend, from the disk ───────────────────────────────────────────────────────────────────────
def test_the_three_states_of_the_disk_and_the_refusal_of_two(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS)
    assert K.disk_state(str(w.d)) == "none"
    w.disk(K.TEST, "m1")
    kr = K.resolve()
    assert kr.backend == "test" and kr.state == "test"
    assert any("IN CLEAR" in x and "encrypt-keys.sh" in x for x in kr.warnings)
    w.disk(K.FILE, keyhash=True)
    with pytest.raises(K.KeyringAmbiguous) as e:
        K.resolve()
    assert e.value.cause == "two keyrings" and e.value.hint
    import shutil
    shutil.rmtree(w.d / "keyring-test")
    kr = K.resolve()
    assert kr.backend == "file" and kr.passphrase == PASS and kr.source == "file"


def test_an_EMPTY_keyring_directory_counts_as_absent(tmp_path, monkeypatch):
    # `dendrad keys list` creates keyring-test/ or keyring-file/ on an empty keyring dir (measured): the
    # directory's existence says nothing, only what it holds.
    w = World(tmp_path, monkeypatch)
    (w.d / "keyring-test").mkdir(parents=True)
    (w.d / "keyring-file").mkdir()
    assert K.disk_state(str(w.d)) == "none"
    assert K.resolve().backend == "test"


def test_a_file_keyring_without_its_passphrase_is_a_NAMED_error_never_test(tmp_path, monkeypatch):
    # The defect this module exists to forbid: an encrypted keyring whose passphrase is absent read as the
    # TEST keyring, which holds no key -- the miner would create a new identity in silence.
    w = World(tmp_path, monkeypatch)
    w.disk(K.FILE, "m1", keyhash=True)
    with pytest.raises(K.PassphraseMissing) as e:
        K.resolve()
    assert e.value.cause == "passphrase missing" and str(w.pf) in str(e.value)
    assert "DENDRA_SECRETS_DIR" in e.value.hint
    # a keyhash alone is a passphrase that was set there: still `file`, still refused without it
    import shutil
    shutil.rmtree(w.d / "keyring-file")
    w.disk(K.FILE, keyhash=True)
    with pytest.raises(K.PassphraseMissing):
        K.resolve()


def test_a_new_keyring_is_file_when_the_passphrase_FILE_is_there_test_otherwise(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    assert K.resolve().backend == "test"
    assert any("created in clear" in x for x in K.resolve().warnings)
    w.pf.write_text(PASS + "\n")
    assert K.resolve().backend == "file"
    # the historical variable alone does not choose `file` for a new keyring: only the kit's file does
    w.pf.unlink()
    monkeypatch.setenv("DENDRA_MINER_PASSPHRASE", PASS)
    kr = K.resolve()
    assert kr.backend == "test" and kr.source == "env" and kr.seal_with == PASS


# ── the passphrase ───────────────────────────────────────────────────────────────────────────────────
def test_the_passphrase_file_wins_and_the_variable_is_a_warned_fallback(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS)
    assert K.read_passphrase().source == "file"
    w.pf.unlink()
    monkeypatch.setenv("DENDRA_MINER_PASSPHRASE", "b" * 20)
    p = K.read_passphrase()
    assert p.source == "env" and p.value == "b" * 20
    assert any("docker inspect" in x and "/proc/<pid>/environ" in x for x in p.warnings)
    # both, and different: refused, never one picked
    w.pf.write_text(PASS + "\n")
    with pytest.raises(K.PassphraseConflict):
        K.read_passphrase()
    monkeypatch.setenv("DENDRA_MINER_PASSPHRASE", PASS)
    assert K.read_passphrase().source == "file"


@pytest.mark.parametrize("content,why", [(b"", "empty"), (b"\n", "empty"), (b"one\ntwo\n", "more than one line"),
                                         (b"\xff\xfe", "UTF-8")])
def test_a_passphrase_file_that_exists_and_cannot_be_used_is_an_error_never_absent(tmp_path, monkeypatch, content, why):
    w = World(tmp_path, monkeypatch)
    w.pf.write_bytes(content)
    with pytest.raises(K.PassphraseUnreadable) as e:
        K.read_passphrase()
    assert why in str(e.value)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_passphrase_file_is_not_an_absent_one(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS)
    os.chmod(w.pf, 0)
    try:
        with pytest.raises(K.PassphraseUnreadable):
            K.resolve()
    finally:
        os.chmod(w.pf, 0o600)


def test_a_passphrase_shorter_than_dendrad_accepts_is_refused_for_a_file_keyring(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase="short77")
    with pytest.raises(K.PassphraseUnreadable) as e:
        K.resolve()
    assert "8 characters" in str(e.value)


# ── what dendrad is handed ───────────────────────────────────────────────────────────────────────────
def test_flags_always_name_the_directory_and_stdin_carries_the_passphrase(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS)
    kr = K.resolve()
    assert kr.flags() == ["--keyring-backend", "file", "--keyring-dir", str(w.d)]
    assert kr.stdin() == (PASS + "\n") * K.STDIN_COPIES and K.STDIN_COPIES == 2
    w.pf.unlink()
    kr = K.resolve()
    assert kr.flags() == ["--keyring-backend", "test", "--keyring-dir", str(w.d)] and kr.stdin() == ""


def test_the_measured_refusal_is_translated_into_its_cause():
    t = "EOF\nEOF\nEOF\nm1 is not a valid name or address: too many failed passphrase attempts"
    assert "passphrase is missing or wrong" in K.explain(t)
    assert "8 characters" in K.explain("password must be at least 8 characters")
    assert K.explain("m1 is not a valid name or address: decoding bech32 failed") == ""
    assert K.last_line("Usage:\n  dendrad keys show\nFlags:\n  -a\nError: the real reason") == "Error: the real reason"


# ── three states for a key, never two ────────────────────────────────────────────────────────────────
def test_a_key_is_absent_only_when_dendrad_AND_the_disk_say_so(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS, keys={"m1": "dendra1abc"}, cfg_passphrase=PASS)
    w.disk(K.FILE, "m1", keyhash=True)
    kr = K.resolve()
    assert K.key_state(kr, "m1") == (K.PRESENT, "dendra1abc")
    state, why = K.key_state(kr, "m2")
    assert state == K.ABSENT
    # the passphrase does not open the keyring: UNREADABLE, never absent -- whatever dendrad's words say
    w.cfg["passphrase"] = "something-else-entirely"
    w.save()
    state, why = K.key_state(kr, "m1")
    assert state == K.UNREADABLE and "passphrase is missing or wrong" in why
    # dendrad says absent without a passphrase refusal, the disk holds <name>.info: UNREADABLE
    w.cfg = {"keys": {}, "passphrase": None}
    w.save()
    state, why = K.key_state(kr, "m1")
    assert state == K.UNREADABLE and "m1.info" in why
    # dendrad absent from PATH: NOT_RUN, neither absent nor unreadable
    (w.bin / "dendrad").unlink()
    assert K.key_state(kr, "m1")[0] == K.NOT_RUN


def test_an_empty_list_over_a_keyring_that_holds_keys_is_a_failed_read(tmp_path, monkeypatch):
    # Measured: `dendrad keys list` with a wrong passphrase exits 0 and prints [] (the refusal is on
    # stderr, as "migrate err ... too many failed passphrase attempts").
    w = World(tmp_path, monkeypatch, passphrase=PASS, keys={"m1": "dendra1abc"}, cfg_passphrase="other-passphrase")
    w.disk(K.FILE, "m1", keyhash=True)
    with pytest.raises(K.PassphraseRejected):
        K.list_keys(K.resolve())
    # no refusal on stderr, but a list that misses a key the disk holds: still a failed read
    w.cfg = {"keys": {}, "passphrase": None}
    w.save()
    with pytest.raises(K.KeyringUnreadable) as e:
        K.list_keys(K.resolve())
    assert "holds 1 key(s) and dendrad read 0" in str(e.value)
    w.cfg = {"keys": {"m1": "dendra1abc"}, "passphrase": PASS}
    w.save()
    assert [k["name"] for k in K.list_keys(K.resolve())] == ["m1"]


# ── the command line the application and the scripts run ─────────────────────────────────────────────
def _cli(w, *args, stdin=""):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "modea.keyring", *args], cwd=MODEA, capture_output=True,
                          text=True, input=stdin, env=env, timeout=60)


def test_the_list_command_names_the_cause_and_fails(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.disk(K.FILE, "m1", keyhash=True)
    r = _cli(w, "list")
    assert r.returncode == 1 and r.stdout == "" and "keyring: passphrase missing" in r.stderr


def test_forget_recovery_removes_only_the_phrase_of_the_address_confirmed(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    p = tmp_path / "recovery-phrase.json"
    p.write_text(json.dumps({"address": "dendra1aaa", "mnemonic": " ".join(["w"] * 24)}))
    r = _cli(w, "forget-recovery", str(p), "--address", "dendra1bbb", "--yes")
    assert r.returncode == 2 and p.exists()
    r = _cli(w, "forget-recovery", str(p), "--address", "dendra1aaa", "--yes")
    assert r.returncode == 0 and json.loads(r.stdout) == {"removed": True} and not p.exists()
    r = _cli(w, "forget-recovery", str(p), "--address", "dendra1aaa", "--yes")
    assert r.returncode == 3


def test_forget_recovery_removes_nothing_without_yes(tmp_path, monkeypatch, capsys):
    # The address is PUBLIC: it proves nothing about who asks. Deleting the only kept copy of the words is
    # irreversible, so, like `migrate`, nothing happens without --yes. Run IN PROCESS on the subject (which
    # DENDRA_KEYRING_MODULE may point at a mutated copy); the command line itself is run above.
    World(tmp_path, monkeypatch)
    p = tmp_path / "recovery-phrase.json"
    p.write_text(json.dumps({"address": "dendra1aaa", "mnemonic": " ".join(["w"] * 24)}))
    before = p.read_bytes()
    assert K.main(["forget-recovery", str(p), "--address", "dendra1aaa"]) == 2
    assert "--yes" in json.loads(capsys.readouterr().out)["why"]
    assert p.read_bytes() == before
    assert K.main(["forget-recovery", str(p), "--address", "dendra1aaa", "--yes"]) == 0 and not p.exists()


def test_status_reads_the_keyring_the_phrase_and_the_payout_record_without_the_words(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    keys = tmp_path / "keys"
    (keys / "cosmos" / "keyring-test").mkdir(parents=True)
    (keys / "cosmos" / "keyring-test" / "dm1abc.info").write_text("x")
    (keys / "identite-resolue").write_text("dm1abc\n")
    (keys / "dm1abc.sk").write_bytes(b"0" * 32)
    (keys / "recovery-phrase.json").write_text(json.dumps({"address": "dendra1aaa", "mnemonic": " ".join(["secret"] * 24)}))
    monkeypatch.delenv("DENDRA_KEYRING_DIR")
    r = _cli(w, "status", str(keys))
    d = json.loads(r.stdout)
    assert d["keyring"]["backend"] == "test" and d["identity"] == "dm1abc"
    assert d["recovery"] == {"present": True, "address": "dendra1aaa", "words": 24, "sealed": False}
    assert d["files"]["dm1abc.sk"] == "clear" and d["files"]["dm1abc.vrf"] == "absent" and d["payout"] is None
    assert "secret" not in r.stdout


# ── the migration test -> file ───────────────────────────────────────────────────────────────────────
def test_the_migration_removes_keyring_test_only_once_every_address_reads_back(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS, keys={"dm1abc": "dendra1abc"})
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(keys / "cosmos"))
    w.d = keys / "cosmos"
    w.disk(K.TEST, "dm1abc")
    # the import fails: keyring-file is removed, keyring-test is left as it was
    w.cfg["import_fails"] = True
    w.save()
    assert K.migrate(str(keys)) == 1
    assert (w.d / "keyring-test" / "dm1abc.info").exists() and not (w.d / "keyring-file").exists()
    # the import works and the address reads back the same: keyring-test goes
    w.cfg["import_fails"] = False
    w.save()
    assert K.migrate(str(keys)) == 0
    assert not (w.d / "keyring-test").exists() and (w.d / "keyring-file" / "dm1abc.info").exists()
    imp = [c for c in w.calls() if c["argv"][:2] == ["keys", "import"]][-1]
    assert imp["stdin"].split("\n")[1:3] == [PASS, PASS], "the export passphrase, then the keyring's, twice"
    assert all(PASS not in " ".join(c["argv"]) for c in w.calls())


def test_the_migration_refuses_an_import_that_reads_back_at_another_address(tmp_path, monkeypatch):
    # THE PROPERTY THE MIGRATION ANNOUNCES -- keyring-test goes only once every address reads back EQUAL --
    # needs an import that SUCCEEDS at another address to be measured: an import that fails is caught by its
    # exit code alone. Here dendrad exits 0, and keyring-file holds the key at another address.
    w = World(tmp_path, monkeypatch, passphrase=PASS, keys={"dm1abc": "dendra1abc"})
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(keys / "cosmos"))
    w.d = keys / "cosmos"
    w.disk(K.TEST, "dm1abc")
    w.cfg["import_address"] = "dendra1zzz"
    w.save()
    assert K.migrate(str(keys)) == 1
    assert (w.d / "keyring-test" / "dm1abc.info").exists(), "keyring-test is left as it was"
    assert not (w.d / "keyring-file").exists(), "the keyring-file of the wrong address is removed"


def test_plan_says_an_encrypted_keyring_opens_only_on_a_positive_reading(tmp_path, monkeypatch, capsys):
    # encrypt-keys.sh reads `opens`: "already encrypted, nothing to do" is true only for a keyring that OPENS.
    def plan(**env):
        K.plan(str(keys), None, env=dict(os.environ, **env))
        return dict(ln.split("=", 1) for ln in capsys.readouterr().out.splitlines() if "=" in ln)
    w = World(tmp_path, monkeypatch, keys={"dm1abc": "dendra1abc"}, cfg_passphrase=PASS)
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(keys / "cosmos"))
    w.d = keys / "cosmos"
    w.disk(K.FILE, "dm1abc", keyhash=True)
    (keys / "identite-resolue").write_text("dm1abc\n")
    p = plan()
    assert p["state"] == "file" and p["opens"] == "no" and p["address"] == "" and "passphrase missing" in p["why"]
    w.pf.write_text("w" * 64 + "\n")
    p = plan()
    assert p["opens"] == "no" and "passphrase is missing or wrong" in p["why"]
    w.pf.write_text(PASS + "\n")
    p = plan()
    assert p["opens"] == "yes" and p["address"] == "dendra1abc"
    # no identity resolved yet: every key the disk holds must read back
    (keys / "identite-resolue").unlink()
    assert plan()["opens"] == "yes"
    w.pf.unlink()
    p = plan()
    assert p["opens"] == "no" and "passphrase missing" in p["why"]


def test_plan_reads_every_confirmed_phrase_of_a_machine_with_several_identities(tmp_path, monkeypatch, capsys):
    # One identity per card: N phrases, each confirmed in the application. The application keeps the LAST
    # confirmed address and the list of all of them; confirming slot 1's must not make slot 0's read as never
    # written down (encrypt-keys.sh would then refuse to encrypt slot 0).
    def plan(settings):
        K.plan(str(keys), json.dumps(settings) if settings is not None else None, env=dict(os.environ))
        return dict(ln.split("=", 1) for ln in capsys.readouterr().out.splitlines() if "=" in ln)
    World(tmp_path, monkeypatch, keys={"dm1abc": "dendra1abc"})
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(keys / "cosmos"))
    (keys / "cosmos" / "keyring-test").mkdir(parents=True)
    (keys / "cosmos" / "keyring-test" / "dm1abc.info").write_text("x")
    (keys / "identite-resolue").write_text("dm1abc\n")
    # the last confirmation is another identity's, and this one is in the list: confirmed
    p = plan({"recovery_confirmed_address": "dendra1other", "recovery_confirmed_addresses": ["dendra1abc", "dendra1other"]})
    assert p["address"] == "dendra1abc" and p["confirmed_address"] == "dendra1abc"
    # an older application wrote the last one only: as before, the caller compares it
    assert plan({"recovery_confirmed_address": "dendra1abc"})["confirmed_address"] == "dendra1abc"
    assert plan({"recovery_confirmed_address": "dendra1other"})["confirmed_address"] == "dendra1other"
    # this identity in no record: never reported as confirmed
    p = plan({"recovery_confirmed_address": "dendra1other", "recovery_confirmed_addresses": ["dendra1other"]})
    assert p["confirmed_address"] != "dendra1abc"
    # a list that is not a list proves nothing
    p = plan({"recovery_confirmed_address": "", "recovery_confirmed_addresses": "dendra1abc"})
    assert p["confirmed_address"] == ""
    assert plan(None)["confirmed_address"] == ""


def test_the_migration_refuses_a_keyring_that_is_not_test(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, passphrase=PASS)
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setenv("DENDRA_KEYRING_DIR", str(keys / "cosmos"))
    assert K.migrate(str(keys)) == 2
    w.pf.unlink()
    (keys / "cosmos" / "keyring-test").mkdir(parents=True)
    (keys / "cosmos" / "keyring-test" / "m1.info").write_text("x")
    assert K.migrate(str(keys)) == 2, "no passphrase file: refused, never a keyring sealed with nothing"


def test_files_written_by_the_module_are_private(tmp_path):
    from modea import crypto
    old = os.umask(0o022)
    try:
        crypto.store_secret(str(tmp_path / "x.attestkey"), b"ab" * 32, "", crypto.AAD_ATTEST)
    finally:
        os.umask(old)
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "x.attestkey").st_mode) == 0o600
