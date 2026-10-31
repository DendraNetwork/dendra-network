# -*- coding: utf-8 -*-
"""The kit version: read from the kit's file, declared in every deposit body, compared with the network's.

What is pinned:
  * modea.kit_version reads the integer on line 1 of the file and answers None, never 0, when it cannot;
    `with_kit_version` adds the field to an object body and rewrites nothing else;
  * relay_client sends the field inside the body it signs and posts (`put_status`, `put_answer`), so the
    relay reads it under the signature and no header carries it;
  * miner.parse_network_info keeps the three kit keys, in the character set deploy/join.sh accepts,
    and interprets nothing; kit_status leaves `kit_behind` null (and says why) without a URL, without a
    readable file, without KIT_VERSION in it, or without this kit's own number, and compares otherwise;
  * the banner is printed while behind only, names the published and the running kit, the instruction and
    the deadline, and its first words are `Update available`.
The subjects can be pointed at mutated copies (DENDRA_DAEMON_FILE, DENDRA_RELAY_CLIENT_FILE).
"""
import http.server
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

import pytest

# The directory of the modules under test. DENDRA_MODEA_DIR points this bench at a copy, so a mutation
# placed on that copy is what gets loaded, never the file shipped next to this bench.
MODEA = os.environ.get("DENDRA_MODEA_DIR", str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, MODEA)

from modea import kit_version as kitv  # noqa: E402


def _load(env, name):
    if os.environ.get(env):
        spec = importlib.util.spec_from_file_location(name, os.environ[env])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    return importlib.import_module(name)


md = _load("DENDRA_DAEMON_FILE", "miner")
rc = _load("DENDRA_RELAY_CLIENT_FILE", "relay_client")


# -- the file ---------------------------------------------------------------------------------------------
def test_the_version_is_the_integer_on_line_one(tmp_path):
    f = tmp_path / "KIT_VERSION"
    f.write_text("2\n# a comment on line two\n", encoding="utf-8")
    assert kitv.read_kit_version(str(f)) == 2


@pytest.mark.parametrize("text", ["", "\n", "two", "2.1", "-1", "# 2", " 2 x"])
def test_an_unreadable_number_is_none_never_zero(tmp_path, text):
    f = tmp_path / "KIT_VERSION"
    f.write_text(text, encoding="utf-8")
    assert kitv.read_kit_version(str(f)) is None


def test_a_missing_file_is_none(tmp_path):
    assert kitv.read_kit_version(str(tmp_path / "absent")) is None


def test_the_environment_names_the_file(tmp_path, monkeypatch):
    f = tmp_path / "v"
    f.write_text("7\n", encoding="utf-8")
    monkeypatch.setenv(kitv.KIT_VERSION_FILE_ENV, str(f))
    assert kitv.kit_version_file() == str(f) and kitv.read_kit_version() == 7


def _kit_module_in(tree):
    """The shipped modea/kit_version.py, copied into a fabricated source tree and loaded from there, so the
    path it computes from its own location is measured without depending on where this bench runs (the
    battery runs tests/ from a copy of prototype/ that has no docker/ next to it)."""
    dst = tree / "prototype" / "mode-a" / "modea" / "kit_version.py"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(Path(MODEA, "modea", "kit_version.py").read_bytes())
    spec = importlib.util.spec_from_file_location("kit_version_in_fabricated_tree", str(dst))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_source_tree_file_is_the_default_outside_the_image(tmp_path, monkeypatch):
    monkeypatch.delenv(kitv.KIT_VERSION_FILE_ENV, raising=False)
    if os.path.exists(kitv.IMAGE_FILE):
        pytest.skip("running inside the image")
    mod = _kit_module_in(tmp_path)
    kit_file = tmp_path / "docker" / "KIT_VERSION"
    assert mod.kit_version_file() == str(kit_file)
    kit_file.parent.mkdir()
    kit_file.write_text("4", encoding="utf-8")
    assert mod.read_kit_version() == 4


def test_a_source_tree_without_the_file_answers_none_never_zero(tmp_path, monkeypatch):
    monkeypatch.delenv(kitv.KIT_VERSION_FILE_ENV, raising=False)
    if os.path.exists(kitv.IMAGE_FILE):
        pytest.skip("running inside the image")
    mod = _kit_module_in(tmp_path)
    assert not os.path.exists(mod.kit_version_file()) and mod.read_kit_version() is None


def test_with_kit_version_adds_the_field_and_rewrites_nothing_else():
    assert kitv.with_kit_version({"pub": "01"}, 3) == {"pub": "01", "kit_version": 3}
    assert kitv.with_kit_version({"pub": "01", "kit_version": 1}, 3) == {"pub": "01", "kit_version": 1}
    assert kitv.with_kit_version({"pub": "01"}, None) == {"pub": "01"}
    assert kitv.with_kit_version(["x"], 3) == ["x"]
    src = {"pub": "01"}
    kitv.with_kit_version(src, 3)
    assert src == {"pub": "01"}, "the caller's object is not mutated"


# -- the client: the field travels in the signed body --------------------------------------------------
class _Sink:
    def __init__(self):
        self.bodies = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                outer.bodies.append((self.path, self.rfile.read(n), dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_put_status_declares_the_kit_in_the_body_it_posts(monkeypatch):
    monkeypatch.setattr(rc, "_KIT_VERSION", 5)
    monkeypatch.setattr(rc, "_SIGN_KEY", "")
    s = _Sink()
    try:
        assert rc.put_status(s.url, "pub", "dm1x", {"pub": "01"}) == "ok"
        assert rc.put_answer(s.url, "attest", "dm1x", {"measured_hash": "ab"})[0] == 200
    finally:
        s.close()
    assert [json.loads(b) for _p, b, _h in s.bodies] == [{"pub": "01", "kit_version": 5},
                                                          {"measured_hash": "ab", "kit_version": 5}]
    assert not any(k.lower().startswith("x-dendra-kit") for _p, _b, h in s.bodies for k in h), \
        "the version travels in the body, never in a header"


def test_an_unreadable_kit_file_leaves_the_body_without_the_field(monkeypatch):
    monkeypatch.setattr(rc, "_KIT_VERSION", None)
    monkeypatch.setattr(rc, "_SIGN_KEY", "")
    s = _Sink()
    try:
        rc.put_status(s.url, "pub", "dm1x", {"pub": "01"})
    finally:
        s.close()
    assert json.loads(s.bodies[0][1]) == {"pub": "01"}


def test_the_client_reads_the_same_file_as_the_daemon(tmp_path, monkeypatch):
    # The client reads the file once, at import: a fresh copy of the module, loaded with the environment
    # naming a fabricated file, carries that file's number. Comparing the imported module with the reader
    # would compare None with None on a tree without docker/KIT_VERSION, and prove nothing.
    f = tmp_path / "KIT_VERSION"
    f.write_text("6", encoding="utf-8")
    monkeypatch.setenv(kitv.KIT_VERSION_FILE_ENV, str(f))
    spec = importlib.util.spec_from_file_location("relay_client_with_fabricated_kit", os.path.join(MODEA, "relay_client.py"))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh._KIT_VERSION == kitv.read_kit_version() == 6
    assert fresh.declaring({"pub": "01"}) == {"pub": "01", "kit_version": 6}


# -- network-info: parsed, never executed ------------------------------------------------------------
def test_parse_network_info_keeps_the_three_kit_keys_only():
    text = ("# the network\nCHAIN_ID=dendra-testnet\nKIT_VERSION=3\nKIT_MIN=3\n"
            "KIT_DEADLINE=2026-10-23T23:59:00Z\nDENDRA_RELAY=https://relay.example\n")
    assert md.parse_network_info(text) == {"KIT_VERSION": "3", "KIT_MIN": "3", "KIT_DEADLINE": "2026-10-23T23:59:00Z"}


@pytest.mark.parametrize("line", ["KIT_VERSION=$(touch /tmp/x)", "KIT_VERSION=3; rm -rf /", "KIT_VERSION=3 4",
                                  "KIT_VERSION=`id`", "KIT_VERSION=", "KIT_VERSION", "KIT_MIN=3µ"])
def test_a_value_outside_the_character_set_is_dropped(line):
    assert "KIT_VERSION" not in md.parse_network_info(line) and "KIT_MIN" not in md.parse_network_info(line)


def test_parse_network_info_takes_the_first_equals_sign_only():
    assert md.parse_network_info("KIT_DEADLINE=2026-10-23T23:59:00Z=x") == {}, "a second = is outside the set"
    assert md.parse_network_info(" KIT_VERSION = 4 ") == {"KIT_VERSION": "4"}


# -- the comparison: null until both numbers are known -------------------------------------------------
NULL = {"kit_latest": None, "kit_behind": None}


def test_no_url_means_no_comparison():
    st = md.kit_status(2, "")
    assert {k: st[k] for k in NULL} == NULL and st["kit_version"] == 2 and "DENDRA_CONFIG_URL" in st["kit_why"]
    assert set(st) == {"kit_version", "kit_latest", "kit_behind", "kit_min", "kit_deadline", "kit_read_at", "kit_why"}


def test_an_unreadable_file_leaves_behind_null_and_says_why():
    def boom(url):
        raise OSError("link down")
    st = md.kit_status(2, "https://api.example/network-info.txt", fetch=boom)
    assert {k: st[k] for k in NULL} == NULL and "unreadable" in st["kit_why"] and st["kit_read_at"] is None


def test_a_url_that_is_not_https_compares_nothing_and_says_why():
    called = []
    st = md.kit_status(2, "http://api.example/network-info.txt", fetch=lambda u: called.append(u) or "KIT_VERSION=3\n")
    assert {k: st[k] for k in NULL} == NULL and "https://" in st["kit_why"] and called == []


def test_a_file_without_kit_version_compares_nothing():
    st = md.kit_status(2, "https://u", fetch=lambda u: "CHAIN_ID=x\nKIT_MIN=3\n", now=1000)
    assert {k: st[k] for k in NULL} == NULL and st["kit_min"] == "3" and st["kit_read_at"] == 1000
    assert "KIT_VERSION" in st["kit_why"]


def test_this_kit_unreadable_compares_nothing_but_the_published_number_is_carried():
    st = md.kit_status(None, "https://u", fetch=lambda u: "KIT_VERSION=3\n")
    assert st["kit_latest"] == "3" and st["kit_behind"] is None and "unreadable" in st["kit_why"]


@pytest.mark.parametrize("mine, latest, behind", [(2, "3", True), (3, "3", False), (4, "3", False), (0, "1", True)])
def test_behind_is_strictly_below_the_published_number(mine, latest, behind):
    st = md.kit_status(mine, "https://u", fetch=lambda u: f"KIT_VERSION={latest}\nKIT_MIN=3\nKIT_DEADLINE=2026-10-23T23:59:00Z\n")
    assert st["kit_behind"] is behind and st["kit_latest"] == latest and st["kit_why"] == ""
    assert st["kit_min"] == "3" and st["kit_deadline"] == "2026-10-23T23:59:00Z"


def test_kit_latest_is_text_as_the_contract_with_the_readers_says():
    st = md.kit_status(2, "https://u", fetch=lambda u: "KIT_VERSION=3\n")
    assert isinstance(st["kit_latest"], str) and isinstance(st["kit_behind"], bool)


# -- the banner --------------------------------------------------------------------------------------
def test_the_banner_is_printed_while_behind_only():
    behind = md.kit_status(2, "https://u", fetch=lambda u: "KIT_VERSION=3\nKIT_MIN=3\nKIT_DEADLINE=2026-10-23T23:59:00Z\n")
    text = md.kit_banner(behind, "cd /opt/kit && git fetch origin")
    assert text.startswith("[daemon] Update available: kit 3 is published, this machine runs kit 2.")
    assert "cd /opt/kit && git fetch origin" in text
    assert "Update before 2026-10-23T23:59:00Z" in text and "below 3" in text
    assert "UPDATE" not in text, "no capitals for emphasis"
    current = md.kit_status(3, "https://u", fetch=lambda u: "KIT_VERSION=3\n")
    assert md.kit_banner(current, "x") == ""
    unknown = md.kit_status(2, "")
    assert md.kit_banner(unknown, "x") == "", "null is not behind"


def test_the_banner_without_a_deadline_names_no_deadline():
    behind = md.kit_status(2, "https://u", fetch=lambda u: "KIT_VERSION=3\n")
    text = md.kit_banner(behind, "")
    assert "Update before" not in text and "deploy/join.sh" in text


def test_the_cadence_is_an_hour():
    assert md.KIT_CHECK_S == 3600.0
