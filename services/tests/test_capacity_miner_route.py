# -*- coding: utf-8 -*-
"""GET /capacity/miner/<miner id>, the route the "My miner" page reads (capacity_server.py::miner_view).

What is pinned, against `_switch`, `miner_id_form`, `miner_classes`, `miner_view` and the HTTP handler:
  * the route is off unless DENDRA_CAPACITY_MINER_ROUTE=1, read from the shipped file: the line that decides is
    extracted and run, and the whole file is imported in a fresh interpreter and asked over HTTP. Off, it
    answers 404 and reads nothing; a value that is neither 0 nor 1 stops the service. Three mutants that turn
    the route on by default are caught;
  * the id's form is checked before the store or the registry is read: a bad form answers 400 and reads nothing;
  * three answers, never two: `known` and `absent` (the on-chain registry was read and holds the id, or does
    not) and `?` (503) when it could not be read recently;
  * no answer matches a single row of GET /capacity on the fabricated data: a report is published when at least
    ANONYMITY_FLOOR registered rows share its kit, role and stale flag, and `withheld` otherwise;
  * a stored record without a field reads that field's zero value (kit 0, role unknown, age `?`, stale);
  * no field of the hardware row leaves the route, by key and by value.
The subject can be pointed at a mutated copy (DENDRA_CAPACITY_SERVER_FILE), so a harness shows each case
turns red.
"""
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

MODEA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODEA))

if os.environ.get("DENDRA_CAPACITY_SERVER_FILE"):
    _spec = importlib.util.spec_from_file_location("capacity_server", os.environ["DENDRA_CAPACITY_SERVER_FILE"])
    cs = importlib.util.module_from_spec(_spec)
    sys.modules["capacity_server"] = cs
    _spec.loader.exec_module(cs)
else:
    import capacity_server as cs  # noqa: E402

from modea import cosmos_addr  # noqa: E402

SUBJECT = Path(os.environ.get("DENDRA_CAPACITY_SERVER_FILE") or cs.__file__).resolve()
SWITCH_VAR = "DENDRA_CAPACITY_MINER_ROUTE"


def _bech32(hrp: str, payload: bytes) -> str:
    return cosmos_addr.bech32_encode(hrp, cosmos_addr._convertbits(payload, 8, 5))


def _mid(seed: str) -> str:
    """A well-formed miner id, derived here so that no real identity is written in the bench."""
    return _bech32("dm", hashlib.sha256(seed.encode()).digest()[:10])


MID_SIGNED, MID_DECLARED, MID_REGISTERED, MID_UNKNOWN = (_mid(s) for s in ("signed", "declared", "reg", "nobody"))

# Written here, not read from the server, so that a lower floor or a finer step there turns this red.
ANONYMITY_FLOOR = 5
AGE_STEP_MIN = 15
# What the route may publish. Written here, not read from the server, so that a field added there turns this red.
TOP_KEYS = {"miner_id", "status", "registered_onchain", "signed_report", "stale_threshold_s", "age_step_min",
            "anonymity_min", "generated_at", "_provenance"}
REPORT_KEYS = {"kit_version", "judge_role", "last_report_age_min", "stale"}
PROVENANCE_KEYS = {"signed_report", "registered_onchain"}
# Distinctive text values of the hardware row below. None of them may appear anywhere in a body the route sends.
# The numbers (VRAM, RAM, cores) are left to the key check: a digit string can occur in a timestamp by chance.
HARDWARE_VALUES = ("NVIDIA GeForce RTX 3060", "RTX 30", "abcdef123456", "m-hw-node", "mistral-nemo",
                   "198.51.100.7")


def _stored(miner_id, proven=True, kit=3, role="active", ts=None, node_id="m-hw-node", machine="abcdef123456"):
    body = {"node_id": node_id, "machine": machine, "backend": "gpu", "gpu": "NVIDIA GeForce RTX 3060",
            "gpu_count": 1, "vram_mb": 12288, "ram_mb": 65536, "cpu_cores": 16, "tier": 3, "model": "mistral-nemo",
            "can_judge": True, "judge_backend": "cpu", "miner_ids": [miner_id], "judge_role": role,
            "kit_version": kit, "ip": "198.51.100.7"}
    rep = cs._clean(json.loads(json.dumps(body)))
    rep["_proven"] = miner_id if proven else ""
    if ts is not None:
        rep["ts"] = ts
    return rep


def _crowd(tag, n, **kw):
    """n signed reports alike, from n distinct ids and machines: (reports, ids)."""
    ids = [_mid("%s-%d" % (tag, i)) for i in range(n)]
    reps = [_stored(m, node_id="n-%s-%d" % (tag, i), machine="m-%s-%d" % (tag, i), **kw) for i, m in enumerate(ids)]
    return reps, ids


def _store(*reps):
    return {cs.storage_key(r): r for r in reps}


def _keys(doc):
    """Every key of a JSON document, at any depth."""
    out = set()
    if isinstance(doc, dict):
        for k, v in doc.items():
            out.add(k)
            out |= _keys(v)
    elif isinstance(doc, list):
        for v in doc:
            out |= _keys(v)
    return out


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The real handler on a free port, over a store file of the test's own; the registry is the test's."""
    db = tmp_path / "capacity.json"
    monkeypatch.setattr(cs, "DB", db)
    state = {"registry": set(), "reads": []}

    def registry():
        state["reads"].append("registry")
        return state["registry"]

    real_load = cs._load_strict

    def load():
        state["reads"].append("store")
        return real_load()

    monkeypatch.setattr(cs, "_fresh_registry", registry)
    monkeypatch.setattr(cs, "_load_strict", load)
    monkeypatch.setattr(cs, "MINER_ROUTE_ON", True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        url = "http://127.0.0.1:%d%s" % (srv.server_address[1], path)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    state["db"], state["get"] = db, get
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


# -- the switch -------------------------------------------------------------------------------------------------

def test_an_unset_switch_keeps_the_route_off(monkeypatch):
    monkeypatch.delenv(SWITCH_VAR, raising=False)
    assert cs._switch(SWITCH_VAR) is False
    monkeypatch.setenv(SWITCH_VAR, "0")
    assert cs._switch(SWITCH_VAR) is False
    monkeypatch.setenv(SWITCH_VAR, "1")
    assert cs._switch(SWITCH_VAR) is True


@pytest.mark.parametrize("typo", ["", "true", "yes", "on", " 1", "1 ", "01", "2"])
def test_a_switch_value_that_is_neither_0_nor_1_stops_the_service(monkeypatch, typo):
    monkeypatch.setenv(SWITCH_VAR, typo)
    with pytest.raises(SystemExit):
        cs._switch(SWITCH_VAR)


def test_the_route_off_answers_404_and_reads_nothing(server, monkeypatch):
    monkeypatch.setattr(cs, "MINER_ROUTE_ON", False)
    server["db"].write_text(json.dumps(_store(_stored(MID_SIGNED, ts=int(time.time()) - 60))), "utf-8")
    server["registry"] = {MID_SIGNED}
    for mid in (MID_SIGNED, MID_UNKNOWN, "dm1notanid"):
        code, body = server["get"]("/capacity/miner/" + mid)
        assert code == 404, (mid, body)
        assert json.loads(body) == {"error": "not found"}
    assert server["reads"] == [], "the route read the store or the registry while it was off"
    code, _body = server["get"]("/health")
    assert code == 200, "the switch turns off one route only"


# -- the switch as shipped --------------------------------------------------------------------------------------
# The cases above call `_switch` and set MINER_ROUTE_ON by hand. These run what the shipped file runs: the
# `_switch` it defines with the one line that assigns MINER_ROUTE_ON, and the whole file in a fresh
# interpreter whose environment holds nothing but PATH, the store path and the variable under test.

def _decision_source(path: Path):
    """The shipped `_switch` and the one top-level line that assigns MINER_ROUTE_ON, as the file writes them."""
    src = path.read_text("utf-8")
    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_switch"]
    lines = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
             and any(isinstance(t, ast.Name) and t.id == "MINER_ROUTE_ON"
                     for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    assert len(fns) == 1, "the file defines `_switch` once at the top level"
    assert len(lines) == 1, "the file assigns MINER_ROUTE_ON once at the top level"
    return ast.get_source_segment(src, fns[0]) + chr(10) + ast.get_source_segment(src, lines[0]) + chr(10)


def _run_shipped_line(path: Path, value, monkeypatch):
    if value is None:
        monkeypatch.delenv(SWITCH_VAR, raising=False)
    else:
        monkeypatch.setenv(SWITCH_VAR, value)
    ns = {"os": os}
    exec(compile(_decision_source(path), str(path), "exec"), ns)
    return ns["MINER_ROUTE_ON"]


PROBE = """
import importlib.util, json, sys, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location("capacity_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:%d/capacity/miner/dm1notanid" % srv.server_address[1], timeout=5) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
srv.shutdown()
print(json.dumps({"flag": mod.MINER_ROUTE_ON, "code": code}))
"""


def _served(path: Path, value, tmp_path: Path):
    """Import the file whole in a fresh interpreter and ask it for the route: (return code, answer or stderr)."""
    env = {"PATH": os.environ.get("PATH", ""), "DENDRA_CAPACITY_DB": str(tmp_path / "never-written.json")}
    if "SYSTEMROOT" in os.environ:          # sockets on Windows need it
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if value is not None:
        env[SWITCH_VAR] = value
    out = subprocess.run([sys.executable, "-I", "-c", PROBE, str(path), str(MODEA)], env=env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return out.returncode, out.stderr
    return 0, json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("value, on", [(None, False), ("0", False), ("1", True)])
def test_the_shipped_line_decides_the_route(monkeypatch, value, on):
    assert _run_shipped_line(SUBJECT, value, monkeypatch) is on


@pytest.mark.parametrize("value, code", [(None, 404), ("0", 404), ("1", 400)])
def test_the_shipped_file_serves_the_route_only_when_turned_on(tmp_path, value, code):
    rc, got = _served(SUBJECT, value, tmp_path)
    assert rc == 0, got[-800:]
    assert got == {"flag": code != 404, "code": code}, "400 = served, and the bad id refused before any read"


def test_a_switch_typo_stops_the_shipped_file(tmp_path):
    rc, err = _served(SUBJECT, "yes", tmp_path)
    assert rc != 0 and "is neither 0 nor 1" in err


# Each one turns the route on when the variable is unset, at a different place: the line, the switch, the dispatch.
MUTANTS = {
    "line": [('MINER_ROUTE_ON = _switch("DENDRA_CAPACITY_MINER_ROUTE")', "MINER_ROUTE_ON = True")],
    "switch": [('    if raw is None or raw == "0":', '    if raw == "0":'),
               ('    if raw == "1":', '    if raw is None or raw == "1":')],
    "dispatch": [("elif path.startswith(MINER_ROUTE) and MINER_ROUTE_ON:", "elif path.startswith(MINER_ROUTE):")],
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_a_mutant_that_turns_the_route_on_by_default_is_caught(tmp_path, monkeypatch, name):
    text = SUBJECT.read_bytes()
    for old, new in MUTANTS[name]:
        old, new = old.encode("utf-8"), new.encode("utf-8")
        assert text.count(old) == 1, "the mutation no longer applies to the shipped file: %s" % name
        text = text.replace(old, new)
    mutant = tmp_path / "capacity_server.py"
    mutant.write_bytes(text)
    line_on = _run_shipped_line(mutant, None, monkeypatch)
    rc, got = _served(mutant, None, tmp_path)
    assert rc == 0, got[-800:]
    assert line_on is True or got["code"] != 404, "the bench missed a route turned on by default (%s)" % name


# -- the form ---------------------------------------------------------------------------------------------------

def test_a_miner_id_is_accepted_in_its_canonical_form():
    assert cs.miner_id_form(MID_SIGNED) == MID_SIGNED
    assert cs.miner_id_form(MID_SIGNED.upper()) == MID_SIGNED, "bech32 in capitals names the same id"


@pytest.mark.parametrize("bad", [
    "", "dm1", "dm", "x" * 91,
    _bech32("dendra", hashlib.sha256(b"acct").digest()[:20]),     # an account address: another prefix
    _bech32("dendra", hashlib.sha256(b"acct").digest()[:10]),     # another prefix, the miner's payload length
    _bech32("dx", hashlib.sha256(b"acct").digest()[:10]),
    _bech32("dm", hashlib.sha256(b"long").digest()[:20]),         # the miner prefix, a 20-byte payload
    _bech32("dm", hashlib.sha256(b"short").digest()[:9]),
    MID_SIGNED[:-1] + ("q" if MID_SIGNED[-1] != "q" else "p"),     # the checksum broken
    MID_SIGNED[:5] + MID_SIGNED[5:].upper(),                       # mixed case
    MID_SIGNED + "/x", "../capacity", "dm1%2F..",
])
def test_a_bad_form_is_refused(bad):
    assert cs.miner_id_form(bad) == ""


def test_the_form_is_checked_before_anything_is_read(server):
    for bad in ("dm1notanid", _bech32("dendra", hashlib.sha256(b"acct").digest()[:20]), MID_SIGNED + "/x"):
        code, body = server["get"]("/capacity/miner/" + bad)
        assert code == 400, (bad, body)
        assert json.loads(body)["error"] == "not a miner id"
    assert server["reads"] == [], "a bad form read the store or the registry"


def test_a_missing_checker_is_not_a_bad_form(server, monkeypatch):
    def missing(raw):
        raise ImportError("modea")
    monkeypatch.setattr(cs, "miner_id_form", missing)
    code, body = server["get"]("/capacity/miner/" + MID_SIGNED)
    assert code == 503 and json.loads(body)["status"] == "?"


# -- known, absent and ? ----------------------------------------------------------------------------------------

def test_an_unknown_id_is_absent_when_the_registry_was_read():
    code, doc = cs.miner_view(_store(_stored(MID_SIGNED)), MID_UNKNOWN, {MID_SIGNED}, time.time())
    assert code == 200
    assert doc["status"] == "absent" and doc["registered_onchain"] is False and doc["signed_report"] is None


def test_an_id_is_question_mark_when_the_registry_was_not_read():
    reps, ids = _crowd("q", ANONYMITY_FLOOR)
    for mid in (MID_UNKNOWN, ids[0]):
        code, doc = cs.miner_view(_store(*reps), mid, None, time.time())
        assert code == 503
        assert doc["status"] == "?" and "signed_report" not in doc, "no report without the registry"


def test_a_store_never_written_holds_nothing(server):
    assert not server["db"].exists()
    code, body = server["get"]("/capacity/miner/" + MID_UNKNOWN)
    assert code == 200 and json.loads(body)["status"] == "absent"


def test_an_unreadable_store_is_question_mark_never_absent(server):
    server["db"].write_text("{ not json", "utf-8")
    code, body = server["get"]("/capacity/miner/" + MID_UNKNOWN)
    doc = json.loads(body)
    assert code == 503 and doc["status"] == "?", doc
    server["db"].write_text("[]", "utf-8")
    code, body = server["get"]("/capacity/miner/" + MID_UNKNOWN)
    assert code == 503 and json.loads(body)["status"] == "?", "a store that is not an object is not empty"


def test_a_signed_report_is_published_with_its_kit_role_and_age():
    now = time.time()
    reps, ids = _crowd("c", ANONYMITY_FLOOR, kit=3, role="mute", ts=int(now) - 40 * 60)
    code, doc = cs.miner_view(_store(*reps), ids[0], set(ids), now)
    assert code == 200 and doc["status"] == "known" and doc["registered_onchain"] is True
    assert doc["signed_report"] == {"kit_version": 3, "judge_role": "mute", "last_report_age_min": 30,
                                    "stale": False}
    assert doc["stale_threshold_s"] == cs.STALE_S
    assert doc["age_step_min"] == AGE_STEP_MIN and doc["anonymity_min"] == ANONYMITY_FLOOR


@pytest.mark.parametrize("minutes, shown", [(0, 0), (14, 0), (15, 15), (44, 30), (61, 60), (1500, 1500)])
def test_the_age_is_rounded_down_to_its_step(minutes, shown):
    now = 1_800_000_000.0
    rep = _stored(MID_SIGNED, ts=int(now) - minutes * 60 - 5)
    assert cs._report_age_min(rep, now) == shown


def test_a_stale_signed_report_says_so():
    now = time.time()
    reps, ids = _crowd("s", ANONYMITY_FLOOR, ts=int(now) - cs.STALE_S - 120)
    _code, doc = cs.miner_view(_store(*reps), ids[0], set(ids), now)
    assert doc["signed_report"]["stale"] is True


def test_a_registered_id_without_a_report_is_known_with_none():
    reps, ids = _crowd("r", ANONYMITY_FLOOR)
    for store in ({}, _store(*reps)):
        code, doc = cs.miner_view(store, MID_REGISTERED, set(ids) | {MID_REGISTERED}, time.time())
        assert code == 200 and doc["status"] == "known" and doc["signed_report"] is None


def test_a_signed_report_of_an_unregistered_id_is_not_published():
    reps, ids = _crowd("u", ANONYMITY_FLOOR)
    code, doc = cs.miner_view(_store(*reps), ids[0], set(ids[1:]), time.time())
    assert code == 200 and doc["status"] == "absent" and doc["signed_report"] is None


def test_an_unsigned_report_is_never_published():
    reps, ids = _crowd("d", ANONYMITY_FLOOR)
    store = _store(*reps, _stored(MID_DECLARED, proven=False, node_id="n-a", machine="m-a"))
    _code, doc = cs.miner_view(store, MID_DECLARED, set(ids), time.time())
    assert doc["status"] == "absent", "an unsigned report naming an id does not make it known"
    _code, doc = cs.miner_view(store, MID_DECLARED, set(ids) | {MID_DECLARED}, time.time())
    assert doc["status"] == "known" and doc["signed_report"] is None, "an unsigned report is not the signed one"


def test_the_declared_twin_of_a_signed_report_changes_nothing():
    now = time.time()
    reps, ids = _crowd("t", ANONYMITY_FLOOR)
    twin = _stored(ids[0], proven=False, node_id="n-t-0", machine="m-t-0")
    alone = cs.miner_view(_store(*reps), ids[0], set(ids), now)
    with_twin = cs.miner_view(_store(*reps, twin), ids[0], set(ids), now)
    assert alone == with_twin and isinstance(alone[1]["signed_report"], dict)


# -- no answer points to one row --------------------------------------------------------------------------------

def _class_of(row):
    return (row["kit_version"], row["judge_role"], row["stale"])


def _rows_each_answer_can_be(answers, nodes):
    """What a reader holding every answer of the route and GET /capacity can tell: for each registered id, the
    registered rows it can be. A published report matches the rows of its class; `withheld` matches the rows of
    no published class; null matches none (the id has no row)."""
    reg = [n for n in nodes if n["registered_onchain"] is True]
    shown = {_class_of(r) for r in answers.values() if isinstance(r, dict)}
    out = {}
    for mid, r in answers.items():
        if r is None:
            out[mid] = []
        elif r == "withheld":
            out[mid] = [n for n in reg if _class_of(n) not in shown]
        else:
            out[mid] = [n for n in reg if _class_of(n) == _class_of(r)]
    return out


def _answers(store, registry, now, monkeypatch):
    monkeypatch.setattr(cs, "_onchain_miner_ids", lambda: set(registry))
    nodes = cs.aggregate(store, chain={})["nodes"]
    answers = {}
    for mid in sorted(registry):
        code, doc = cs.miner_view(store, mid, registry, now)
        assert code == 200 and doc["status"] == "known", doc
        answers[mid] = doc["signed_report"]
    return answers, nodes


def test_no_answer_points_to_one_row(monkeypatch):
    """Two crowds, of 6 and 5, and two reports alone in their class. Published one by one, the lone reports would
    each point to their row; withheld, they alone would form the withheld class. The crowd of 5 is pooled with
    them, and only the crowd of 6 is published. Four unsigned rows share the first lone report's class: rows a
    reader sees as not registered never make a class large enough."""
    now = time.time()
    big, big_ids = _crowd("big", ANONYMITY_FLOOR + 1, kit=4, role="active")
    mid_, mid_ids = _crowd("mid", ANONYMITY_FLOOR, kit=3, role="active")
    lone1, lone1_ids = _crowd("lone1", 1, kit=3, role="mute")
    lone2, lone2_ids = _crowd("lone2", 1, kit=2, role="off", ts=int(now) - cs.STALE_S - 120)
    unregistered, _u = _crowd("unreg", 1, kit=9, role="active")
    declared = [_stored(_mid("decl-%d" % i), proven=False, kit=3, role="mute", node_id="n-decl-%d" % i,
                        machine="m-decl-%d" % i) for i in range(ANONYMITY_FLOOR - 1)]
    store = _store(*big, *mid_, *lone1, *lone2, *unregistered, *declared)
    registry = set(big_ids + mid_ids + lone1_ids + lone2_ids) | {MID_REGISTERED, _mid("reg-2")}
    answers, nodes = _answers(store, registry, now, monkeypatch)
    rows = _rows_each_answer_can_be(answers, nodes)
    for mid, cand in rows.items():
        if answers[mid] is None:
            continue
        assert len(cand) >= ANONYMITY_FLOOR, "the answer for %s matches %d row(s) of GET /capacity: %r" % (
            mid, len(cand), sorted(n["node_id"] for n in cand))
    published = [isinstance(answers[m], dict) and answers[m]["kit_version"] == 4 for m in big_ids]
    assert all(published), "a class shared by enough rows is published"
    assert all(answers[m] == "withheld" for m in mid_ids + lone1_ids + lone2_ids)
    assert answers[MID_REGISTERED] is None


def test_rows_the_reader_holds_beat_the_floor_so_the_route_stays_off(monkeypatch):
    """A reader can register identities, with stake from the public faucet, and sign reports whose kit, role and
    stale flag it chooses. Four of them in the class of a report alone in its own make that class published; the
    reader then drops its own rows, and one row is left: the target's. The floor counts rows, and rows can be
    made. The link is counted and printed, and while it exists the shipped file keeps the route off by default."""
    now = time.time()
    stale = int(now) - cs.STALE_S - 120
    big, big_ids = _crowd("big", ANONYMITY_FLOOR + 1, kit=4, role="active")
    target, target_ids = _crowd("target", 1, kit=3, role="off", ts=stale)
    store, registry = _store(*big, *target), set(big_ids + target_ids)
    before, _nodes = _answers(store, registry, now, monkeypatch)
    assert before[target_ids[0]] == "withheld"
    held, held_ids = _crowd("held", ANONYMITY_FLOOR - 1, kit=3, role="off", ts=stale)
    store, registry = _store(*big, *target, *held), registry | set(held_ids)
    answers, nodes = _answers(store, registry, now, monkeypatch)
    own = {"n-held-%d" % i for i in range(len(held))}
    left = [n for n in _rows_each_answer_can_be(answers, nodes)[target_ids[0]] if n["node_id"] not in own]
    print("MINER_ROUTE_HELD_ROWS held=%d rows_left=%d floor=%d" % (len(held), len(left), ANONYMITY_FLOOR))
    if len(left) < ANONYMITY_FLOOR:
        assert _run_shipped_line(SUBJECT, None, monkeypatch) is False, (
            "rows the reader holds leave %d row(s) for one answer, and the route is on by default" % len(left))


def test_while_the_registered_rows_are_few_every_registered_id_reads_withheld(monkeypatch):
    """Fewer registered rows than the floor: no class can be large enough, and a registered id with no row reads
    like the others, so the route adds nothing to what GET /capacity and the chain already show."""
    now = time.time()
    reps, ids = _crowd("few", ANONYMITY_FLOOR - 2, kit=4, role="active")
    answers, nodes = _answers(_store(*reps), set(ids) | {MID_REGISTERED}, now, monkeypatch)
    assert set(answers.values()) == {"withheld"}, answers
    registered_rows = [n for n in nodes if n["registered_onchain"] is True]
    for cand in _rows_each_answer_can_be(answers, nodes).values():
        assert len(cand) == len(registered_rows)


def test_the_withheld_decision_does_not_depend_on_the_id():
    now = time.time()
    reps, ids = _crowd("same", ANONYMITY_FLOOR, kit=4)
    lone, lone_ids = _crowd("other", 1, kit=1)
    store, registry = _store(*reps, *lone), set(ids + lone_ids)
    first = cs.miner_classes(store, registry, now)
    assert all(cs.miner_classes(store, registry, now) == first for _ in ids + lone_ids)


def test_the_floor_is_the_bench_floor():
    assert cs.MINER_ANONYMITY_MIN == ANONYMITY_FLOOR and cs.MINER_AGE_STEP_MIN == AGE_STEP_MIN


# -- the zero rule ----------------------------------------------------------------------------------------------

def test_absent_fields_read_their_zero_value():
    """A record stored before a field existed carries none: kit 0 (no number declared), role unknown (never
    off), age `?` (the time cannot be read) and stale (it cannot be dated as recent)."""
    reps, ids = _crowd("z", ANONYMITY_FLOOR)
    for rep in reps:
        for k in ("kit_version", "judge_role", "ts"):
            del rep[k]
    _code, doc = cs.miner_view(_store(*reps), ids[0], set(ids), time.time())
    assert doc["signed_report"] == {"kit_version": 0, "judge_role": "unknown", "last_report_age_min": "?",
                                    "stale": True}


@pytest.mark.parametrize("ts", ["1700000000", True, 0, -5, 10 ** 12])
def test_a_time_that_cannot_be_read_is_question_mark(ts):
    rep = _stored(MID_SIGNED)
    rep["ts"] = ts
    assert cs._report_age_min(rep, time.time()) == "?"


# -- no hardware ------------------------------------------------------------------------------------------------

def test_no_hardware_field_leaves_the_route(server):
    now = int(time.time())
    crowd, crowd_ids = _crowd("hw", ANONYMITY_FLOOR - 1)
    server["db"].write_text(json.dumps(_store(_stored(MID_SIGNED, ts=now - 60), *crowd,
                                              _stored(MID_SIGNED, proven=False, ts=now - 60),
                                              _stored(MID_DECLARED, proven=False, node_id="n-d", machine="m-d"))),
                            "utf-8")
    server["registry"] = {MID_SIGNED, MID_REGISTERED} | set(crowd_ids)
    bodies = []
    for mid in (MID_SIGNED, MID_DECLARED, MID_REGISTERED, MID_UNKNOWN):
        code, body = server["get"]("/capacity/miner/" + mid)
        assert code == 200, (mid, body)
        doc = json.loads(body)
        assert set(doc) == TOP_KEYS, sorted(set(doc) ^ TOP_KEYS)
        assert set(doc["_provenance"]) == PROVENANCE_KEYS
        if isinstance(doc["signed_report"], dict):
            assert set(doc["signed_report"]) == REPORT_KEYS, doc["signed_report"]
        bodies.append(body.decode("utf-8"))
    assert json.loads(bodies[0])["signed_report"]["kit_version"] == 3, "the bench reads a real signed report"
    stored = cs._clean({"node_id": "x"})
    hardware = set(stored) - {"kit_version", "judge_role", "ts"} | {"ip", "_proven", "machine", "node_id"}
    for body in bodies:
        leaked = hardware & _keys(json.loads(body))
        assert not leaked, "hardware or identity-row fields published: %s" % sorted(leaked)
        for v in HARDWARE_VALUES:
            assert v not in body, "a hardware value left the route: %r" % v
