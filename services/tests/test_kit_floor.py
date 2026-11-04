"""Bench of the kit floor read in one place (modea/kit_floor.py).

The relay, the faucet and the season rules read DENDRA_KIT_MIN, and the first two DENDRA_KIT_DEADLINE, through one
module. These cases hold its forms, its three states and its comparison at the deadline: one second before it the
floor applies to nothing, at the deadline and one second after it the floor applies. Mutated copies of the module
must each fail the case that names them. Each shipped reader is then run as a process from its own environment, and
must answer what the module answers.
"""
import calendar
import importlib.util
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# DENDRA_MODEA_DIR puts a copy of the modules first on the path (the mutation benches); the shipped directory stays
# behind it for the modules the copy does not hold. A copy that holds none of the modules this bench drives is refused:
# the bench would load the shipped files and pass with every sign of a measurement it did not make.
POINTED = os.environ.get("DENDRA_MODEA_DIR", "")
SUBJECTS = ("modea/kit_floor.py", "final_season_rules.py", "relay.py", "faucet.py")
if POINTED and not any(os.path.isfile(os.path.join(POINTED, s)) for s in SUBJECTS):
    raise FileNotFoundError(f"DENDRA_MODEA_DIR={POINTED}: none of {', '.join(SUBJECTS)} is there, "
                            f"this bench would measure the shipped files instead")
READ_FROM = os.pathsep.join([POINTED, HERE] if POINTED else [HERE])
sys.path.insert(0, HERE)
if POINTED:
    sys.path.insert(0, POINTED)


def _shipped(name):
    """The file `name` of the pointed copy when it holds one, else the shipped one."""
    for d in (POINTED, HERE):
        if d and os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    raise FileNotFoundError(name)


import final_season_rules as R  # noqa: E402
from final_season_calc import Identity, kit_allows_pay  # noqa: E402
from modea import kit_floor  # noqa: E402

FINGERPRINT_KIT_3 = "e297f5cabbfa7a28711e64ce89373bf15fadf72da3f01dca1290ece6ff2d438a"
D_TEXT = "2031-05-06T07:08:09Z"
D = float(calendar.timegm((2031, 5, 6, 7, 8, 9, 0, 0, 0)))       # computed without strptime, the path under test
ERR = "error"
WIDE = "".join(chr(0xFF10 + int(c)) if c in "0123456789" else c for c in D_TEXT)   # full-width digits

MIN_OK = (("1", 1), ("3", 3), (" 3 ", 3), ("999999999", 999_999_999), (None, None), ("", None), ("   ", None))
MIN_BAD = ("0", "00", "03", "-1", "+3", "x", "3.0", "3 4", "1000000000", "1234567890", chr(178), chr(0x0663),
           chr(0xFF13), 3, 3.0, True, b"3")
DEADLINE_OK = ((D_TEXT, D), (" " + D_TEXT + " ", D), ("1970-01-01T00:00:10Z", 10.0), (None, None), ("", None))
DEADLINE_BAD = ("2031-05-06 07:08:09", "2031-05-06T07:08:09", "2031-05-06T07:08:09+00:00", "2031-05-06T07:08:09z",
                "2031-5-6T7:8:9Z", "2031-02-30T00:00:00Z", "2031-05-06T24:00:00Z", "2031-05-06T23:59:60Z",
                "2031-05-06T07:08:09ZZ", "x2031-05-06T07:08:09Z", WIDE, 1_000_000.0)


def _call(f, *a):
    try:
        return f(*a)
    except ValueError:
        return ERR


def _violations(m) -> list:
    """The names of the cases module `m` fails; an empty list is the contract held."""
    bad = []

    def check(name, ok):
        if not ok:
            bad.append(name)

    for raw, want in MIN_OK:
        check(f"min {raw!r}", _call(m.parse_kit_min, raw) == want)
    for raw in MIN_BAD:
        check(f"min {raw!r} refused", _call(m.parse_kit_min, raw) == ERR)
        check(f"pair with min {raw!r} refused", m.policy_from_env(raw, D_TEXT)[:2] == (None, None)
              and bool(m.policy_from_env(raw, D_TEXT)[2]))
    for raw, want in DEADLINE_OK:
        check(f"deadline {raw!r}", _call(m.parse_kit_deadline, raw) == want)
    for raw in DEADLINE_BAD:
        check(f"deadline {raw!r} refused", _call(m.parse_kit_deadline, raw) == ERR)
        check(f"pair with deadline {raw!r} refused", m.policy_from_env("3", raw)[:2] == (None, None)
              and bool(m.policy_from_env("3", raw)[2]))
    check("dormant", m.policy_from_env(None, None) == (None, None, None) and m.policy_from_env("", " ") == (None, None, None))
    check("half floor", m.policy_from_env("3", "")[:2] == (None, None) and bool(m.policy_from_env("3", "")[2]))
    check("half deadline", m.policy_from_env("", D_TEXT)[:2] == (None, None) and bool(m.policy_from_env("", D_TEXT)[2]))
    check("armed", m.policy_from_env("3", D_TEXT) == (3, D, None))
    floor, ts, _ = m.policy_from_env("3", D_TEXT)
    check("D-1 applies nothing", m.floor_in_force(floor, ts, ts - 1) is None)
    check("D applies the floor", m.floor_in_force(floor, ts, ts) == 3)
    check("D+1 applies the floor", m.floor_in_force(floor, ts, ts + 1) == 3)
    check("dormant applies nothing", m.floor_in_force(None, None, ts + 1) is None)
    check("half refused", _call(m.floor_in_force, 3, None, ts) == ERR and _call(m.floor_in_force, None, ts, ts) == ERR)
    return bad


def test_the_module_holds_its_forms_its_three_states_and_the_deadline():
    assert _violations(kit_floor) == []


MUTANTS = (
    ("the comparison made strict", "    return floor if now >= deadline_ts else None",
     "    return floor if now > deadline_ts else None", "D applies the floor"),
    ("the comparison applied before the deadline", "    return floor if now >= deadline_ts else None",
     "    return floor if now >= deadline_ts - 1 else None", "D-1 applies nothing"),
    ("the nine-digit bound lifted", 'KIT_MIN_FORM = re.compile("[1-9][0-9]{0,8}")',
     'KIT_MIN_FORM = re.compile("[1-9][0-9]*")', "min '1000000000' refused"),
    ("a floor of zero accepted", 'KIT_MIN_FORM = re.compile("[1-9][0-9]{0,8}")',
     'KIT_MIN_FORM = re.compile("[0-9]{1,9}")', "min '0' refused"),
    ("the deadline form dropped", "    if not KIT_DEADLINE_FORM.fullmatch(s):", "    if False:",
     "deadline '2031-5-6T7:8:9Z' refused"),
    ("a half configuration read as dormant", "        if not m and not d:", "        if not m or not d:", "half floor"),
    ("a half policy read as dormant at the deadline", "    if floor is None and deadline_ts is None:",
     "    if floor is None or deadline_ts is None:", "half refused"),
)


@pytest.mark.parametrize("label,before,after,caught_by", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_each_mutant_of_the_module_fails_the_case_that_names_it(tmp_path, label, before, after, caught_by):
    with open(kit_floor.__file__, encoding="utf-8", newline="") as f:
        src = f.read()
    assert src.count(before) == 1, f"{label}: the text to mutate is not in the module exactly once"
    path = tmp_path / "kit_floor_mutant.py"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(before, after))
    spec = importlib.util.spec_from_file_location("kit_floor_mutant_%d" % MUTANTS.index(
        (label, before, after, caught_by)), path)
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutant)
    assert caught_by in _violations(mutant), label


def test_the_pay_floor_compares_at_the_floor_and_has_no_default():
    floor = kit_floor.parse_kit_min("3")
    rules = R.kit_rules(floor)
    assert R.fingerprint(rules) == FINGERPRINT_KIT_3
    paid = [kit_allows_pay(Identity("m", kit_version=v), rules) for v in (floor - 1, floor, floor + 1)]
    assert paid == [False, True, True]
    assert kit_allows_pay(Identity("m"), rules) is False                    # no signed report is not a current kit
    without_floor = {k: v for k, v in rules.items() if k != "kit_min"}
    with pytest.raises(ValueError):
        kit_allows_pay(Identity("m", kit_version=floor), without_floor)
    for bad in ("0", "1000000000", "03"):
        with pytest.raises(ValueError, match="DENDRA_KIT_MIN"):
            R.kit_min_setting(bad)


def test_no_reader_keeps_a_form_of_its_own():
    for name in ("relay.py", "faucet.py", "final_season_rules.py"):
        with open(_shipped(name), encoding="utf-8") as f:
            src = f.read()
        assert "kit_floor." in src and "[1-9][0-9]" not in src, name


# The shipped readers, each imported in a process of its own from the environment it is given, as a service starts.
CHILD = """
import json, os, sys
for d in reversed(sys.argv[1].split(os.pathsep)):
    sys.path.insert(0, d)
which, pairs = sys.argv[2], json.loads(sys.argv[3])
out = {}
try:
    if which == "rules":
        import final_season_rules as M
        out["import"] = "ok"
        out["kit_min"] = M.KIT_MIN
        out["fingerprint"] = None if M.RULES_KIT is None else M.fingerprint(M.RULES_KIT)
        table = []
        for a, b in pairs:
            try:
                table.append(M.kit_min_setting(a))
            except ValueError:
                table.append("error")
        out["table"] = table
    else:
        M = __import__("relay" if which == "relay" else "faucet")
        out["import"] = "ok"
        ts = M.KIT_DEADLINE_TS
        out["policy"] = [M.KIT_FLOOR, ts, M.KIT_POLICY_ERROR is not None]
        out["at"] = None if ts is None else [M.kit_floor_now(ts - 1), M.kit_floor_now(ts), M.kit_floor_now(ts + 1)]
        out["table"] = [[p[0], p[1], p[2] is not None] for p in (M.kit_policy_from_env(a, b) for a, b in pairs)]
except ValueError as e:
    out["import"] = "refused: " + str(e)
print(json.dumps(out))
"""
PAIRS = [("", ""), ("3", ""), ("", D_TEXT), ("3", D_TEXT), ("999999999", D_TEXT), ("1000000000", D_TEXT),
         ("03", D_TEXT), (chr(178), D_TEXT), ("3", "2031-5-6T7:8:9Z"), ("3", "2031-02-30T00:00:00Z"),
         ("3", D_TEXT.replace("T", " ")), (" 3 ", " " + D_TEXT + " ")]
ENVS = {"armed": {"DENDRA_KIT_MIN": "3", "DENDRA_KIT_DEADLINE": D_TEXT},
        "ten digits": {"DENDRA_KIT_MIN": "1234567890", "DENDRA_KIT_DEADLINE": D_TEXT},
        "unset": {}}


def _run(tmp_path, which, extra):
    env = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "LANG") if k in os.environ}
    env.update(DENDRA_RELAY_STORE=str(tmp_path / "store"), DENDRA_HOME=str(tmp_path / "home"),
               DENDRA_FAUCET_STATE="", **extra)
    p = subprocess.run([sys.executable, "-c", CHILD, READ_FROM, which, json.dumps(PAIRS)], env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, (which, extra, p.stderr[-800:])
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("which", ["relay", "faucet"])
@pytest.mark.parametrize("state", list(ENVS))
def test_the_relay_and_the_faucet_as_shipped_answer_what_the_module_answers(tmp_path, which, state):
    got = _run(tmp_path, which, ENVS[state])
    assert got["import"] == "ok", got
    want_table = [[f, t, e is not None] for f, t, e in (kit_floor.policy_from_env(a, b) for a, b in PAIRS)]
    assert got["table"] == want_table, which
    want = kit_floor.policy_from_env(ENVS[state].get("DENDRA_KIT_MIN"), ENVS[state].get("DENDRA_KIT_DEADLINE"))
    assert got["policy"] == [want[0], want[1], want[2] is not None], (which, state)
    if state == "armed":
        assert got["policy"] == [3, D, False] and got["at"] == [None, 3, 3], (which, got)
    elif state == "ten digits":
        assert got["policy"] == [None, None, True] and got["at"] is None, (which, got)
    else:
        assert got["policy"] == [None, None, False] and got["at"] is None, (which, got)


@pytest.mark.parametrize("state", list(ENVS))
def test_the_season_rules_as_shipped_answer_what_the_module_answers(tmp_path, state):
    got = _run(tmp_path, "rules", ENVS[state])
    if state == "ten digits":
        assert got["import"].startswith("refused: ") and "DENDRA_KIT_MIN" in got["import"], got
        return
    assert got["import"] == "ok", got
    assert got["table"] == [_call(kit_floor.parse_kit_min, a) for a, _ in PAIRS]
    if state == "armed":
        assert got["kit_min"] == 3 and got["fingerprint"] == FINGERPRINT_KIT_3, got
    else:
        assert got["kit_min"] is None and got["fingerprint"] is None, got
