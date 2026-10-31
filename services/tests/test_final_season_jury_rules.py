"""Bench of the jury set (`final_season_rules.RULES_JURY`): a verdict paid whatever its value, on the audits
of the programme concluded or unwound, committed before the resolution and after the reveal sealed to its juror; a
"1" on a controlled-fault request of the classes incoherent answer or no reveal takes the juror's verdicts and judge
day away for the day; the judge presence reward still seated on concluded audits only; the controlled-fault identities
of every period begun by the day removed before the calculation and published; a day ranked only with the reveal of
each of those periods, matching its commitment, listing every request its seed draws, its memo on chain below the
period's first block; a relay store that cannot be read, or an unwind the index lost, defers the day.

The sets published before keep their fingerprints (the kit set's for floor 3 is the one decision 24 cites), a
day before the set keeps its bytes, and the set stays dormant: no day is fixed here but in a bench's own data
directory. The mutants at the end are copies of the shipped modules with one bound removed, each run against the case
that holds it.
"""
import hashlib
import json
import os
import re
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.environ.get("DENDRA_MODEA_DIR"):
    sys.path.insert(0, os.environ["DENDRA_MODEA_DIR"])

import pytest  # noqa: E402

import final_season_chain as C  # noqa: E402
import final_season_facts as F  # noqa: E402
import final_season_rank as RK  # noqa: E402
import final_season_rules as R  # noqa: E402
from final_season_calc import Identity, compute_day, to_json  # noqa: E402

KIT_MIN = 3
FINGERPRINT_KIT_3 = "e297f5cabbfa7a28711e64ce89373bf15fadf72da3f01dca1290ece6ff2d438a"
PUBLISHED_MORE_REQUESTS = "58769883109f2ccef8d385acfbd93e3623511180cb6619669fc713f2e593ba0f"
GEN = "dendra1generator"
NODE = "tcp://n:26657"
RPC = C.rpc_url(NODE)
DAY = R.RULES["day_blocks"]
START = 1
FINALITY = 1_000
TIP = 30 * DAY
END = R.end_epoch()
KIT_DAY, JURY_DAY = 10, 12
FIRST, LAST = R.day_bounds(JURY_DAY, START)
FIRSTS = dict(unwound_from=1, doubled_from=4, more_requests_from=6, kit_rules_from=KIT_DAY)
PAYER = "dendra1" + "p" * 38
TRAP = "q1"
TRAP_ADDRESS = "dendra1" + "q" * 38
SEED = bytes(range(32))
DOC = {"classes": list(R.FAULT_CLASSES), "fault_bps": 10_000, "from_day": JURY_DAY, "to_day": JURY_DAY + 1,
       "nonce": "ab" * 32, "seed_sha256": hashlib.sha256(SEED).hexdigest(),
       "traps": [{"miner_id": TRAP, "payout_address": TRAP_ADDRESS}], "version": 1}
DOC_BYTES = R.canonical_fault_document(DOC)
COMMIT = hashlib.sha256(DOC_BYTES).hexdigest()
PERIOD = {"from_day": JURY_DAY, "to_day": JURY_DAY + 1, "commitment": COMMIT}


def job_of_class(cls, prefix):
    """The first job id `<prefix><n>` the seed draws in class `cls` (fault_bps 10 000: every request is faulted)."""
    for n in range(10_000):
        jid = f"{prefix}{n}"
        if R.fault_draw(SEED, jid, DOC["fault_bps"], DOC["classes"]) == cls:
            return jid
    raise AssertionError(cls)


T_NR = job_of_class("no_reveal", "tnr")
T_INC = job_of_class("incoherent_answer", "tinc")
T_FF = job_of_class("plausible_false_fact", "tff")


def block_time(h):
    return END - 100 * 86400 + 5 * h


def _floor():
    if R.KIT_MIN is None:
        R.set_kit_min(KIT_MIN)
    assert R.KIT_MIN == KIT_MIN
    return R.RULES_KIT


def _jury(monkeypatch, periods=((JURY_DAY, JURY_DAY + 1, COMMIT),), payer=PAYER):
    """The jury set fixed in this process for one case, put back as it was at the end of the case."""
    _floor()
    for name in ("FAULT_PERIODS", "FAULT_PAYER", "RULES_JURY", "RULE_HISTORY"):
        monkeypatch.setattr(R, name, getattr(R, name))
    R.FAULT_PERIODS = R.FAULT_PAYER = R.RULES_JURY = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)
    return R.set_fault_commitments(periods, payer)


def _no_jury(monkeypatch):
    _floor()
    for name in ("FAULT_PERIODS", "FAULT_PAYER", "RULES_JURY", "RULE_HISTORY"):
        monkeypatch.setattr(R, name, getattr(R, name))
    R.FAULT_PERIODS = R.FAULT_PAYER = R.RULES_JURY = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)


# -- the rule set ----------------------------------------------------------------------------------------------------
def test_the_earlier_fingerprints_do_not_move_and_the_jury_set_adds_three_keys(monkeypatch):
    assert R.fingerprint(R.kit_rules(KIT_MIN)) == FINGERPRINT_KIT_3
    assert R.fingerprint(R.RULES_MORE_REQUESTS) == PUBLISHED_MORE_REQUESTS
    jury = _jury(monkeypatch)
    added = {k: v for k, v in jury.items() if k not in R.RULES_KIT}
    assert added == {"juror_rule": R.JUROR_RULE, "fault_payer": PAYER, "fault_periods": [PERIOD]}
    assert all(jury[k] == v for k, v in R.RULES_KIT.items())
    assert R.RULE_HISTORY == R.PUBLISHED_SETS + (R.RULES_KIT, jury) and R.rule_set_of(R.fingerprint(jury)) == (5, jury)
    assert R.fingerprint(R.kit_rules(KIT_MIN)) == FINGERPRINT_KIT_3                      # still, with the set known
    assert R.pays_jury_rule(jury) and R.pays_judge_presence(jury) and R.finality_rule(jury) == R.FINALITY
    assert not any(R.pays_jury_rule(s) for s in R.PUBLISHED_SETS + (R.RULES_KIT,))
    # the fingerprint names the commitment, the period and the payer
    other = hashlib.sha256(b"other").hexdigest()
    for periods, payer in ((((JURY_DAY, JURY_DAY + 1, other),), PAYER), (((JURY_DAY, JURY_DAY + 2, COMMIT),), PAYER),
                           (((JURY_DAY, JURY_DAY + 1, COMMIT),), "dendra1" + "z" * 38)):
        assert R.fingerprint(R.jury_rules(KIT_MIN, periods, payer)) != R.fingerprint(jury)
    with pytest.raises(ValueError, match="jury set's keys"):
        R.pays_jury_rule(dict(R.RULES_KIT, juror_rule=R.JUROR_RULE))
    with pytest.raises(ValueError, match="unknown juror rule"):
        R.pays_jury_rule(dict(jury, juror_rule="paid_if_consistent"))
    # a second set in one process is refused
    with pytest.raises(ValueError, match="already fixed"):
        R.set_fault_commitments(((JURY_DAY, JURY_DAY + 1, other),), PAYER)


def test_the_periods_and_the_payer_are_read_in_one_form_and_refused_in_any_other():
    two = f"12-17:{COMMIT},18-21:{'c' * 64}"
    assert R.fault_periods_setting(two) == ((12, 17, COMMIT), (18, 21, "c" * 64))
    assert R.fault_periods_setting("") is None and R.fault_periods_setting(None) is None
    for bad in (f"12:{COMMIT}", f"17-12:{COMMIT}", f"12-17:{COMMIT.upper()}", f"12-17:{COMMIT},17-20:{'c' * 64}",
                f"12-17:{COMMIT},18-20:{COMMIT}", "12-17:abc"):
        with pytest.raises(ValueError):
            R.fault_periods_setting(bad)
    with pytest.raises(ValueError, match="at least one"):
        R.check_fault_periods(())
    assert R.fault_payer_setting(f" {PAYER} ") == PAYER and R.fault_payer_setting("") is None
    for bad in ("cosmos1abc", "dendra1short", PAYER.upper()):
        with pytest.raises(ValueError):
            R.fault_payer_setting(bad)
    assert R.fault_memo(COMMIT) == "dendra-red-team-" + COMMIT


def test_a_day_takes_the_jury_set_from_its_first_day_after_decision_24_only(monkeypatch):
    jury = _jury(monkeypatch)
    got = [R.rules_for_day_with_jury(d, 1, 4, 6, KIT_DAY, JURY_DAY) for d in range(15)]
    assert got[:JURY_DAY] == [R.rules_for_day(d, 1, 4, 6, KIT_DAY) for d in range(JURY_DAY)]
    assert all(r is jury for r in got[JURY_DAY:])
    assert all(R.rules_for_day_with_jury(d, 1, 4, 6, KIT_DAY, None) is R.rules_for_day(d, 1, 4, 6, KIT_DAY)
               for d in range(15))
    with pytest.raises(TypeError):
        R.rules_for_day_with_jury(12, 1, 4, 6, KIT_DAY)                       # no default
    for bad in (-1, "12", 12.0, True):
        with pytest.raises(ValueError, match="jury set"):
            R.rules_for_day_with_jury(12, 1, 4, 6, KIT_DAY, bad)
    for kit_from, jury_from in ((KIT_DAY, KIT_DAY - 1), (None, JURY_DAY)):
        with pytest.raises(ValueError, match="before decision 24"):
            R.rules_for_day_with_jury(13, 1, 4, 6, kit_from, jury_from)
    R.RULES_JURY = None
    with pytest.raises(ValueError, match="no fault commitment is fixed"):
        R.rules_for_day_with_jury(12, 1, 4, 6, KIT_DAY, JURY_DAY)
    assert R.rules_for_day_with_jury(11, 1, 4, 6, KIT_DAY, JURY_DAY) is R.RULES_KIT


def test_a_reader_takes_the_set_only_from_values_its_fingerprint_binds(monkeypatch):
    _no_jury(monkeypatch)
    fp = R.fingerprint(R.jury_rules(KIT_MIN, ((JURY_DAY, JURY_DAY + 1, COMMIT),), PAYER))
    keys = {"kit_min": KIT_MIN, "fault_periods": [PERIOD], "fault_payer": PAYER}
    assert R.jury_set_named(dict(keys, fault_payer="dendra1" + "z" * 38), fp) is None and R.RULES_JURY is None
    assert R.jury_set_named(dict(keys, kit_min=KIT_MIN + 1), fp) is None and R.RULES_JURY is None
    assert R.jury_set_named(None, fp) is None and R.jury_set_named(keys, None) is None
    jury = R.jury_set_named(keys, fp)
    assert jury is R.RULES_JURY and R.fingerprint(jury) == fp and R.rule_set_of(fp)[1] is jury


def test_the_draw_is_the_documented_one():
    d = hashlib.sha256(SEED + T_NR.encode("utf-8")).digest()
    assert DOC["classes"][int.from_bytes(d[8:16], "big") % 3] == "no_reveal"
    assert R.fault_draw(SEED, T_NR, 0, DOC["classes"]) is None                  # bounded below: 0 faults nothing
    assert len({T_NR, T_INC, T_FF}) == 3
    rules = {"fault_periods": [{"from_day": 12, "to_day": 13, "commitment": COMMIT},
                               {"from_day": 14, "to_day": 16, "commitment": "c" * 64}]}
    assert [len(R.started_fault_periods(rules, d)) for d in (11, 12, 13, 14, 20)] == [0, 1, 1, 2, 2]


# -- the reveal, pure ----------------------------------------------------------------------------------------------
def reveal_of(faults, seed=SEED, commitment=COMMIT):
    return {"version": 1, "commitment": commitment, "seed": seed.hex(), "faults": faults}


def entry(jid, cls, miner=TRAP, faulted=True):
    return {"job_id": jid, "miner_id": miner, "class": cls, "question": f"question of {jid}",
            "answer": f"answer of {jid}", "faulted": faulted}


def drawn(jid):
    return R.fault_draw(SEED, jid, DOC["fault_bps"], DOC["classes"])


# gq1: a programme request of q1 the seed draws (fault_bps 10 000), served honestly, listed as such.
ENTRIES = [entry(T_NR, "no_reveal"), entry(T_INC, "incoherent_answer"), entry(T_FF, "plausible_false_fact"),
           entry("gq1", drawn("gq1"), faulted=False)]


def test_a_reveal_is_accepted_only_when_it_matches_its_commitment_its_seed_and_its_draw():
    got = F.check_fault_reveal(PERIOD, DOC_BYTES, reveal_of(ENTRIES))
    assert got["traps"] == {TRAP: TRAP_ADDRESS} and got["seed"] == SEED
    assert set(got["faults"]) == {T_NR, T_INC, T_FF, "gq1"} and got["faults"]["gq1"]["faulted"] is False
    bad_cases = [
        (DOC_BYTES, reveal_of([dict(entry(T_NR, "no_reveal"), faulted=1)]), "whether it was faulted"),
        (DOC_BYTES, reveal_of([{k: v for k, v in entry(T_NR, "no_reveal").items() if k != "faulted"}]),
         "whether it was faulted"),
        (DOC_BYTES, reveal_of([entry("gq1", drawn("gq1"), faulted=False), entry(T_NR, "incoherent_answer",
                                                                                 faulted=False)]), "the seed draws"),
        (DOC_BYTES + b" ", reveal_of(ENTRIES), "sha256 is not the commitment"),
        (json.dumps(DOC, sort_keys=True).encode(), reveal_of(ENTRIES), "sha256 is not the commitment"),
        (DOC_BYTES, reveal_of(ENTRIES, seed=bytes(32)), "seed"),
        (DOC_BYTES, reveal_of(ENTRIES, commitment="c" * 64), "names commitment"),
        (DOC_BYTES, reveal_of([entry(T_NR, "incoherent_answer")]), "the seed draws"),
        (DOC_BYTES, reveal_of([entry(T_NR, "no_reveal", miner="p1")]), "does not commit"),
        (DOC_BYTES, reveal_of([entry(T_NR, "no_reveal"), entry(T_NR, "no_reveal")]), "not one job id"),
        (DOC_BYTES, dict(reveal_of(ENTRIES), extra=1), "exactly"),
    ]
    for doc, rev, said in bad_cases:
        with pytest.raises(ValueError, match=said):
            F.check_fault_reveal(PERIOD, doc, rev)
    # a document of other days than the set's period, though its own sha256 matches it, is refused
    other = dict(DOC, to_day=JURY_DAY + 5)
    ob = R.canonical_fault_document(other)
    with pytest.raises(ValueError, match="names days"):
        F.check_fault_reveal(dict(PERIOD, commitment=hashlib.sha256(ob).hexdigest()), ob,
                             reveal_of(ENTRIES, commitment=hashlib.sha256(ob).hexdigest()))


def test_the_reveal_reader_answers_three_ways(tmp_path):
    d = tmp_path / "reveal"
    assert F.read_fault_reveal("", COMMIT)[0] == F.STORE_UNREAD
    assert F.read_fault_reveal(str(d), COMMIT)[0] == F.STORE_UNREAD                 # no such directory
    d.mkdir()
    assert F.read_fault_reveal(str(d), COMMIT)[0] == F.FAULT_ABSENT
    (d / f"{COMMIT}.commitment.json").write_bytes(DOC_BYTES)
    assert F.read_fault_reveal(str(d), COMMIT)[0] == F.FAULT_ABSENT                 # one file of two
    (d / f"{COMMIT}.reveal.json").write_text("{", encoding="utf-8")
    assert F.read_fault_reveal(str(d), COMMIT)[0] == F.STORE_UNREAD                 # torn
    (d / f"{COMMIT}.reveal.json").write_text(json.dumps(reveal_of(ENTRIES)), encoding="utf-8")
    state, doc, rev = F.read_fault_reveal(str(d), COMMIT)
    assert state == "read" and doc == DOC_BYTES and rev == reveal_of(ENTRIES)


def test_a_drawn_request_left_out_of_a_reveal_is_named():
    checked = F.check_fault_reveal(PERIOD, DOC_BYTES, reveal_of(ENTRIES))
    jobs = [{"id": j, "client": GEN, "miner_id": TRAP} for j in (T_NR, T_INC, T_FF, "gq1", "gq2")]
    jobs += [{"id": "o1", "client": "dendra1other", "miner_id": TRAP}, {"id": "p9", "client": GEN, "miner_id": "p1"}]
    settled = {j["id"]: 100 for j in jobs}
    # gq2 is drawn (fault_bps 10 000) and not listed; o1 is not a programme request, p9 not of an identity of the period
    assert F.drawn_not_listed(checked, jobs, settled, GEN, 100, 100) == [("gq2", TRAP, drawn("gq2"))]
    assert F.drawn_not_listed(checked, jobs, settled, GEN, 101, 200) == []          # settled outside the blocks
    assert F.drawn_not_listed(checked, jobs[:4], settled, GEN, 100, 100) == []


# -- the jurors, pure ------------------------------------------------------------------------------------------------
def test_a_verdict_is_paid_whatever_its_value_and_only_after_its_reveal():
    votes = {"a": {"j1": "1", "j2": "0", "j3": "1", "p": "1"}}
    arrivals = {"a__j1__p": 150.0, "a__j2__p": 150.0, "a__j3__p": 150.0, "a__p__p": 150.0}
    committed = {"a": {"j1": 200, "j2": 210, "j3": 100, "p": 220}}
    times = {200: 200.0, 210: 210.0, 100: 100.0, 220: 220.0}
    counts, proof = F.jury_verdicts(votes, {"a": "p"}, arrivals, committed, times)
    assert counts == {"j1": 1, "j2": 1}                                  # the audited miner earns nothing
    assert proof == {"a": {"j1": "ok", "j2": "ok", "j3": F.JUDGE_VERDICT_BEFORE_REVEAL}}
    counts, proof = F.jury_verdicts(votes, {"a": "p"}, dict(arrivals, a__j1__p=None, a__j2__p=F.STORE_UNREAD),
                                    committed, times)
    assert counts == {} and proof["a"]["j1"] == F.JUDGE_NO_REVEAL and proof["a"]["j2"] == F.JUDGE_STORE_UNREAD
    with pytest.raises(KeyError):                                       # an arrival not read is never defaulted
        F.jury_verdicts(votes, {"a": "p"}, {}, committed, times)


def test_only_a_one_on_an_incoherent_or_unrevealed_fault_contradicts():
    proof = {"n": {"jurors": {"j1": {"vote": "1"}, "j2": {"vote": "0"}, "j3": {"vote": "1"}, "j4": {"vote": "1"}}},
             "i": {"jurors": {"j5": {"vote": "1"}, "j6": {"vote": "0"}}},
             "f": {"jurors": {"j7": {"vote": "1"}}},
             "x": {"jurors": {"j8": {"vote": "1"}}}}
    arrivals = {"n__j1__q": None, "n__j2__q": None, "n__j3__q": 5.0, "n__j4__q": F.STORE_UNREAD}
    out, pr = F.fault_contradictions({"n": "no_reveal", "i": "incoherent_answer", "f": "plausible_false_fact"},
                                     proof, arrivals, {"n": "q", "i": "q", "f": "q", "x": "p"})
    assert out == {"j1", "j5"}
    assert pr["n"]["votes"]["j2"] == {"vote": "0", "contradicted": False}
    assert pr["n"]["votes"]["j3"]["why"] == F.FAULT_REVEAL_ARRIVED and pr["n"]["votes"]["j4"]["why"] == F.FAULT_ARRIVAL_UNREAD
    assert pr["f"]["votes"]["j7"]["why"] == F.FAULT_CLASS_CONTRADICTS_NOBODY and "x" not in pr


# -- the ranking of a day under the set, on a fake chain -------------------------------------------------------------
UNWOUND = "open+paid+optimistic+disputed+resolved+unwound"
VINDICATED = "open+paid+optimistic+disputed+resolved+vindicated+quorum"
JURORS = ("j1", "j2", "j3", "j4", "j5", "j6", "j7")


def _store(path, records):
    import relay_store
    st = relay_store.DiskStore(str(path), [F.REVEAL_KIND])
    for key, t in records.items():
        st.write(F.REVEAL_KIND, key, b"sealed", ts=t)
    return str(path)


def _job(jid, state, miner):
    return {"id": jid, "state": state, "miner_id": miner, "client": GEN, "slashed_primary": False, "audit_voters": 0}


@pytest.fixture()
def chain(monkeypatch, tmp_path):
    """Day 12 under the jury set, period 12 to 13 of identity q1, registered at FIRST+5. `a1`: an audit of p1 unwound
    at FIRST+300, jury j1, j2, j3, j4, j6, every reveal at FIRST+150; j1 votes 1 and j2 votes 0 after it, j3 votes
    before it, j4 after the unwind, j6 votes 1. Three faults of q1, each unwound: the no-reveal one (j1 votes 1, j5
    votes 0, nothing sealed), the incoherent one (j2 votes 0, j7 votes 1, reveals at FIRST+150), the false fact (j6
    votes 1). p1 and q1 each serve one programme request settled in the day; q1's (gq1) is drawn and served honestly."""
    calls = {"tip": TIP, "jobs": [_job("g1", "open+paid+optimistic", "p1"), _job("gq1", "open+paid+optimistic", TRAP),
                                  _job("a1", UNWOUND, "p1"), _job(T_NR, UNWOUND, TRAP), _job(T_INC, UNWOUND, TRAP),
                                  _job(T_FF, UNWOUND, TRAP)],
             "settled": {"g1": FIRST + 40, "gq1": FIRST + 41, "a1": FIRST + 50, T_NR: FIRST + 51, T_INC: FIRST + 52,
                         T_FF: FIRST + 53},
             "drawn": {"a1": FIRST + 60, T_NR: FIRST + 70, T_INC: FIRST + 80, T_FF: FIRST + 90},
             "unwound": {"a1": FIRST + 300, T_NR: FIRST + 400, T_INC: FIRST + 410, T_FF: FIRST + 420},
             "juries": {"a1": ["j1", "j2", "j3", "j4", "j6", "p1"], T_NR: ["j1", "j5", TRAP], T_INC: ["j2", "j7", TRAP],
                        T_FF: ["j6", TRAP]},
             "votes": {"a1": {"j1": "1", "j2": "0", "j3": "1", "j4": "1", "j6": "1"}, T_NR: {"j1": "1", "j5": "0"},
                       T_INC: {"j2": "0", "j7": "1"}, T_FF: {"j6": "1"}},
             "committed": {("a1", "j1"): FIRST + 200, ("a1", "j2"): FIRST + 210, ("a1", "j3"): FIRST + 100,
                           ("a1", "j4"): FIRST + 350, ("a1", "j6"): FIRST + 220, (T_NR, "j1"): FIRST + 250,
                           (T_NR, "j5"): FIRST + 260, (T_INC, "j2"): FIRST + 270, (T_INC, "j7"): FIRST + 280,
                           (T_FF, "j6"): FIRST + 290},
             "memo": FIRST - 500, "memo_reads": [], "windows": {"p1": 2, TRAP: 2}, "resolution_reads": [],
             "resolved": {}, "registered": {TRAP: FIRST + 5}}

    def node_get(url, timeout=10.0):
        m = re.fullmatch(re.escape(RPC) + r"/block\?height=(\d+)", url)
        if m:
            h = int(m.group(1))
            return {"result": {"block": {"header": {"height": str(h), "time": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(block_time(h)))}}}}
        raise AssertionError("unexpected read " + url)

    def verdict_commits(node, audited, candidates):
        return {j["id"]: {m: {"vote": calls["votes"][j["id"]][m], "creator": "dendra1op" + m}
                          for m in candidates.get(j["id"], []) if m in calls["votes"].get(j["id"], {})
                          and m != j["miner_id"]}
                for j in audited if calls["votes"].get(j["id"])}

    def commit_height(node, key, creator, lo, hi):
        jid, _, juror = key.partition("__verdict__")
        h = calls["committed"].get((jid, juror))
        return h if h is not None and lo <= h <= hi else None

    def memo(node, payer, m):
        calls["memo_reads"].append((payer, m))
        if isinstance(calls["memo"], Exception):
            raise calls["memo"]
        return calls["memo"] if m == R.fault_memo(COMMIT) and payer == PAYER else None

    def resolution(node, rpc, jid):
        calls["resolution_reads"].append(jid)
        return calls["resolved"].get(jid)

    monkeypatch.setattr(C, "_get", node_get)
    monkeypatch.setattr(C, "_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected cli")))
    monkeypatch.setattr(C, "jobs", lambda node: calls["jobs"])
    monkeypatch.setattr(C, "settle_heights", lambda node, sender=None, max_pages=2000: dict(calls["settled"]))
    monkeypatch.setattr(C, "miners", lambda node: [{"id": m, "operator": "dendra1op" + m}
                                                   for m in ("p1", TRAP) + JURORS])
    monkeypatch.setattr(C, "require_index_from", lambda rpc, h: None)
    monkeypatch.setattr(C, "audit_draw", lambda rpc, jid: (calls["drawn"].get(jid), calls["juries"].get(jid, [])))
    monkeypatch.setattr(C, "verdict_commits", verdict_commits)
    monkeypatch.setattr(C, "audit_resolution", resolution)
    monkeypatch.setattr(C, "audit_unwind_height", lambda rpc, jid: calls["unwound"].get(jid))
    monkeypatch.setattr(C, "commit_height", commit_height)
    monkeypatch.setattr(C, "fault_commitment_height", memo)
    monkeypatch.setattr(C, "presence_proofs", lambda node, first, last, eb: (
        dict(calls["windows"]), {m: "dendra1op" + m for m in calls["windows"]}))
    monkeypatch.setattr(C, "miner_registration", lambda node, mid, op, lo, hi: (
        calls["registered"].get(mid) if lo <= calls["registered"].get(mid, lo - 1) <= hi else None))
    arrive = block_time(FIRST + 150)
    calls["store"] = _store(tmp_path / "relay-store",
                            {**{f"a1__{m}__p1": arrive for m in ("j1", "j2", "j3", "j4", "j6")},
                             f"{T_INC}__j2__{TRAP}": arrive, f"{T_INC}__j7__{TRAP}": arrive,
                             f"{T_FF}__j6__{TRAP}": arrive})
    d = tmp_path / "reveal"
    d.mkdir()
    (d / f"{COMMIT}.commitment.json").write_bytes(DOC_BYTES)
    calls["reveal"] = str(d)
    write_reveal(calls, ENTRIES)
    _jury(monkeypatch)
    return calls


def write_reveal(chain, entries):
    """The period's reveal file in the chain's reveal directory, with `entries`."""
    with open(os.path.join(chain["reveal"], f"{COMMIT}.reveal.json"), "w", encoding="utf-8") as f:
        json.dump(reveal_of(entries), f)


def records(day=JURY_DAY, extra_answered=()):
    out = [{"type": "payout", "day": 0, "miner_id": "p1", "address": "dendra1payee", "height": 5},
           {"type": "work_sealed", "day": day, "answers": 2, "drawn": 0,
            "answered": {"p1": ["g1", "a1"] + list(extra_answered), TRAP: ["gq1", T_NR, T_INC, T_FF]}}]
    first = R.day_bounds(day, START)[0]
    out += [{"type": F.CAPACITY, "day": day, "miner_id": m, "kit_version": KIT_MIN, "judge_role": "active",
             "height": first + 10} for m in ("p1", TRAP) + JURORS]
    return out


def rank(chain, day=JURY_DAY, recs=None, jury_from=JURY_DAY, reveal="chain", store="chain", replay=None, mod=None):
    return (mod or RK).rank_day(day, records(day) if recs is None else recs, NODE, GEN, START, 0, FINALITY,
                                chain["tip"], 288, relay_store=chain["store"] if store == "chain" else store,
                                fault_reveal_dir=chain["reveal"] if reveal == "chain" else reveal,
                                replay_from=replay, jury_rules_from=jury_from, **FIRSTS)


def by_id(out):
    return {r["miner_id"]: r for r in out["ranking"]}


def test_under_the_set_a_timely_verdict_after_its_reveal_is_paid_whatever_it_says_and_a_blind_one_empties_the_day(chain):
    out = rank(chain)
    assert out["rules_fingerprint"] == R.fingerprint(R.RULES_JURY)
    rows = by_id(out)
    jury = out["inputs"]["jury"]
    # j2: a "0" on a1 and a "0" on the incoherent fault, each after its reveal: two verdicts. Every audit of the day is
    # unwound, and the judge presence reward is seated on concluded audits only (R8 of 23/10): no judge day for anyone
    assert (rows["j2"]["verdicts"], rows["j2"]["judge_active"]) == (2, 0)
    assert rows["j2"]["gross_udndr"] == 2 * 16_000
    assert out["inputs"]["judge_presence"]["audits"] == {} and out["inputs"]["judge_presence"]["identities"] == {}
    # j6: a "1" on a1 and a "1" on the false fact: paid, never contradicted
    assert (rows["j6"]["verdicts"], rows["j6"]["judge_active"]) == (2, 0)
    # j1: a "1" on the no-reveal fault, sealed nothing: its verdict on a1 is gone
    assert "j1" not in rows and "j1" in jury["contradicted"]
    # j7: a "1" on the incoherent fault after its reveal: contradicted, nothing paid
    assert "j7" not in rows and jury["contradicted"] == ["j1", "j7"]
    # j5: a "0" on the no-reveal fault is no contradiction, and with no reveal sealed to it, no verdict either
    assert "j5" not in jury["contradicted"] and jury["verdicts"][T_NR]["j5"] == F.JUDGE_NO_REVEAL
    # j3 before its reveal, j4 after the unwind: not paid, and said
    assert jury["verdicts"]["a1"]["j3"] == F.JUDGE_VERDICT_BEFORE_REVEAL and "j3" not in rows
    assert out["inputs"]["verdicts"]["a1"]["jurors"]["j4"]["why"] == F.AFTER_RESOLUTION and "j4" not in rows
    # an unwound audit is resolved by its unwind event, never by audit_resolution
    assert out["inputs"]["verdicts"]["a1"]["resolved_at"] == FIRST + 300
    assert out["inputs"]["verdicts"]["a1"]["resolved_in"] == C.END_BLOCK and chain["resolution_reads"] == []
    # the identity of the period is out of the calculation, published with its facts and its registration
    assert TRAP not in rows
    faults = jury["faults"]
    (pub,) = faults["periods"]
    assert pub["traps"][TRAP]["verified_requests"] == 1 and pub["traps"][TRAP]["payout_address"] == TRAP_ADDRESS
    assert pub["traps"][TRAP]["registered_at"] == FIRST + 5 and pub["traps"][TRAP]["registered_before_period"] is False
    assert faults["work"] == {"traps": 1, "all": 2} and faults["period"] == PERIOD and pub["period"] == PERIOD
    assert pub["document"].encode("utf-8") == DOC_BYTES and pub["seed"] == SEED.hex()
    assert pub["commitment_memo_height"] == FIRST - 500 and pub["period_first_block"] == FIRST
    assert faults["jobs"][T_NR]["question"] == f"question of {T_NR}" and faults["jobs"][T_NR]["audited"] is True
    assert faults["jobs"][T_INC]["anchor_key"] == f"{T_INC}__{TRAP}" and faults["jobs"][T_INC]["commitment"] == COMMIT
    # the drawn request served honestly is listed with its answer, and is no fault
    assert faults["jobs"]["gq1"]["faulted"] is False and faults["jobs"]["gq1"]["votes"] is None
    assert faults["drawn_served_honestly"] == 1 and sum(c["faults"] for c in faults["by_class"].values()) == 3
    assert faults["by_class"]["no_reveal"] == {"faults": 1, "audited": 1, "votes_1": 1, "votes_0": 1,
                                               "contradicted_votes": 1}
    assert faults["by_class"]["plausible_false_fact"]["contradicted_votes"] == 0
    assert jury["fault_periods"] == [PERIOD] and jury["fault_payer"] == PAYER and jury["from_day"] == JURY_DAY
    assert chain["memo_reads"] == [(PAYER, R.fault_memo(COMMIT))]
    # p1 is ranked as before: its request and its presence
    assert rows["p1"]["verified_requests"] == 1 and rows["p1"]["gross_udndr"] == 100_000 + 2 * 4_000
    # the same facts, the same bytes: a reader without the relay's store and without the reveal's directory replays
    # the arrivals and checks the published reveal again, against the same commitment and the same chain
    again = rank(chain, store="", reveal="", replay=out)
    assert again == out


def test_a_verdict_on_an_unwound_audit_before_its_unwind_is_paid_and_at_or_after_it_is_not(chain):
    chain["committed"][("a1", "j6")] = FIRST + 300                          # in the unwind's own block: before its end
    assert by_id(rank(chain))["j6"]["verdicts"] == 2
    chain["committed"][("a1", "j6")] = FIRST + 301
    out = rank(chain)
    assert by_id(out)["j6"]["verdicts"] == 1
    assert out["inputs"]["verdicts"]["a1"]["jurors"]["j6"]["why"] == F.AFTER_RESOLUTION
    # an unwound audit whose unwind the index does not give: the index lost it, and the day waits, never ranked with
    # its verdicts unpaid (a recompute on a sound node would pay them, and differ)
    del chain["unwound"]["a1"]
    with pytest.raises(C.ChainUnreadable, match="is unwound but its audit_retention_unwound event"):
        rank(chain)


def test_the_judge_day_is_seated_on_concluded_audits_only_and_a_contradicted_juror_loses_it(chain):
    # a1 concluded by quorum (the miner cleared), resolved at the end of FIRST+300; the faults stay unwound
    chain["jobs"] = [dict(j, state=VINDICATED) if j["id"] == "a1" else j for j in chain["jobs"]]
    chain["resolved"]["a1"] = (FIRST + 300, C.END_BLOCK)
    out = rank(chain)
    rows, jp = by_id(out), out["inputs"]["judge_presence"]
    assert sorted(jp["audits"]) == ["a1"] and jp["audits"]["a1"]["resolved_at"] == FIRST + 300
    # j2 votes "0" on a cleared miner: under the jury set no contradiction, its verdicts and its judge day are paid
    assert (rows["j2"]["verdicts"], rows["j2"]["judge_active"]) == (2, 1)
    assert rows["j2"]["gross_udndr"] == 2 * 16_000 + 500_000
    assert (rows["j6"]["verdicts"], rows["j6"]["judge_active"]) == (2, 1)
    # j1: a "1" on the no-reveal fault takes its verdict on a1 and its judge day away
    assert "j1" not in rows and jp["identities"]["j1"]["why"] == F.JUDGE_CONTRADICTED
    # j3 before its reveal, j4 after the resolution: no judge day
    assert jp["identities"]["j3"]["why"] == F.JUDGE_VERDICT_BEFORE_REVEAL
    assert jp["identities"]["j4"]["why"] == F.JUDGE_NO_TIMELY_VERDICT
    # the faults, unwound, seat nobody
    assert all(set(jp["identities"][m]["audits"]) == {"a1"} for m in jp["identities"])


def test_a_day_before_the_set_keeps_its_bytes(chain):
    chain["tip"] = TIP
    day = JURY_DAY - 1
    for k in ("settled", "drawn", "unwound"):
        chain[k] = {j: h - DAY for j, h in chain[k].items()}
    chain["committed"] = {k: h - DAY for k, h in chain["committed"].items()}
    with_set = rank(chain, day=day)
    without = rank(chain, day=day, jury_from=None)
    assert with_set == without and with_set["rules_fingerprint"] == FINGERPRINT_KIT_3
    assert "jury" not in with_set["inputs"] and chain["memo_reads"] == []
    # An identity registered before the set is ranked like anyone on such a day: secret, it cannot be left out. Its
    # period publishes its registration (`registered_before_period`), and the interface asks the launcher to register
    # each identity at or after its period's first block, so that it is never in a day before it.
    assert TRAP in by_id(with_set)
    assert "j2" not in by_id(with_set)                                    # an unwound audit pays no juror before it


def test_an_identity_registered_before_its_period_is_published_as_such(chain):
    chain["registered"][TRAP] = FIRST - 10
    pub = rank(chain)["inputs"]["jury"]["faults"]["periods"][0]["traps"][TRAP]
    assert (pub["registered_at"], pub["registered_before_period"]) == (FIRST - 10, True)
    del chain["registered"][TRAP]                                        # none in the season, and it served this day
    pub = rank(chain)["inputs"]["jury"]["faults"]["periods"][0]["traps"][TRAP]
    assert (pub["registered_at"], pub["registered_before_period"]) == (None, True)


def test_a_day_of_a_period_is_refused_without_its_reveal_or_with_one_that_does_not_match(chain, tmp_path):
    for reveal in ("", str(tmp_path / "nowhere")):
        with pytest.raises(ValueError, match="cannot be read"):
            rank(chain, reveal=reveal)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="not in .* yet"):
        rank(chain, reveal=str(empty))
    d = tmp_path / "bad"
    d.mkdir()
    (d / f"{COMMIT}.commitment.json").write_bytes(DOC_BYTES.replace(b'"version":1', b'"version":2'))
    (d / f"{COMMIT}.reveal.json").write_text(json.dumps(reveal_of(ENTRIES)), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 is not the commitment"):
        rank(chain, reveal=str(d))
    (d / f"{COMMIT}.commitment.json").write_bytes(DOC_BYTES)
    (d / f"{COMMIT}.reveal.json").write_text(json.dumps(reveal_of([entry(T_NR, "plausible_false_fact")])),
                                             encoding="utf-8")
    with pytest.raises(ValueError, match="the seed draws"):
        rank(chain, reveal=str(d))
    # a drawn request left out: a fault withdrawn once the votes are known would spare j1 (no-reveal) or j7
    for left_out in (T_NR, "gq1"):
        (d / f"{COMMIT}.reveal.json").write_text(json.dumps(reveal_of([e for e in ENTRIES if e["job_id"] != left_out])),
                                                 encoding="utf-8")
        with pytest.raises(ValueError, match=f"leaves out 1 request.s. settled this day that its seed draws, the first "
                                             f"{left_out} of {TRAP}"):
            rank(chain, reveal=str(d))
    # a revealed fault the chain records for another miner
    (d / f"{COMMIT}.reveal.json").write_text(json.dumps(reveal_of(ENTRIES)), encoding="utf-8")
    chain["jobs"] = [dict(j, miner_id="p1") if j["id"] == T_INC else j for j in chain["jobs"]]
    with pytest.raises(ValueError, match="the chain records"):
        rank(chain, reveal=str(d))


def test_under_the_set_a_relay_store_that_cannot_be_read_defers_the_day(chain, tmp_path):
    for store in ("", str(tmp_path / "no-store")):
        with pytest.raises(C.ChainUnreadable, match="the relay's store cannot be read"):
            rank(chain, store=store)
    # a third party replays the published arrivals, as before
    out = rank(chain)
    assert rank(chain, store="", reveal="", replay=out) == out
    # under decision 24 alone the same store, absent, pays no judge day and ranks the day, as it was published
    chain["tip"] = TIP
    for k in ("settled", "drawn", "unwound"):
        chain[k] = {j: h - DAY for j, h in chain[k].items()}
    chain["committed"] = {k: h - DAY for k, h in chain["committed"].items()}
    assert rank(chain, day=JURY_DAY - 1, store="")["inputs"]["judge_presence"]["relay_store"] == F.STORE_UNREAD


def test_a_day_of_a_period_is_refused_when_the_memo_is_absent_or_not_below_the_periods_first_block(chain):
    chain["memo"] = None
    with pytest.raises(ValueError, match="is not on chain"):
        rank(chain)
    for h in (FIRST, FIRST + 1):
        chain["memo"] = h
        with pytest.raises(ValueError, match="not below block"):
            rank(chain)
    chain["memo"] = FIRST - 1
    assert rank(chain)["inputs"]["jury"]["faults"]["periods"][0]["commitment_memo_height"] == FIRST - 1
    chain["memo"] = C.ChainUnreadable("node down")
    with pytest.raises(C.ChainUnreadable):
        rank(chain)


def test_a_day_of_the_set_before_its_first_period_is_refused(chain):
    R.RULES_JURY = None
    R.FAULT_PERIODS = R.FAULT_PAYER = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)
    R.set_fault_commitments(((JURY_DAY + 1, JURY_DAY + 2, COMMIT),), PAYER)
    # day 12 is under the set and before its only period: q1 is secret until the reveal and would be paid
    with pytest.raises(ValueError, match="no fault period holds it while a later one is to come"):
        rank(chain, reveal="")
    assert chain["memo_reads"] == []


def test_a_day_after_the_last_period_needs_its_reveal_and_leaves_its_identities_out(chain):
    # the same requests, settled on day 14, after the period of days 12 to 13
    day = JURY_DAY + 2
    for k in ("settled", "drawn", "unwound"):
        chain[k] = {j: h + 2 * DAY for j, h in chain[k].items()}
    chain["committed"] = {k: h + 2 * DAY for k, h in chain["committed"].items()}
    empty = chain["reveal"] + "-none"
    os.makedirs(empty)
    with pytest.raises(ValueError, match="not in .* yet"):
        rank(chain, day=day, reveal=empty)
    out = rank(chain, day=day)
    faults = out["inputs"]["jury"]["faults"]
    assert faults["period"] is None and [p["period"] for p in faults["periods"]] == [PERIOD]
    assert TRAP not in by_id(out) and faults["periods"][0]["traps"][TRAP]["ranked"] is True
    # a fault of the period settled after it is a fault still: j1 and j7 are contradicted
    assert out["inputs"]["jury"]["contradicted"] == ["j1", "j7"]
    assert rank(chain, day=day, store="", reveal="", replay=out) == out


def _over_budget(chain, n):
    for k in range(n):
        for miner in ("p1", TRAP):
            jid = f"w{miner}{k}"
            chain["jobs"].append(_job(jid, "open+paid+optimistic", miner))
            chain["settled"][jid] = FIRST + 1000 + k
    # every request of q1 is drawn (fault_bps 10 000): listed, served honestly
    write_reveal(chain, ENTRIES + [entry(f"w{TRAP}{k}", drawn(f"w{TRAP}{k}"), faulted=False) for k in range(n)])
    return _over_budget_records(n)


def test_the_period_identities_take_no_part_of_the_cap_or_the_pro_rata(chain):
    recs = _over_budget(chain, 600)
    out = rank(chain, recs=recs)
    p1 = by_id(out)["p1"]
    # 600 requests of p1 at 0.1 DNDR: 60 DNDR, below the 100 DNDR of the day once q1's 600 are out
    assert p1["verified_requests"] == 600 and p1["paid_udndr"] == p1["payable_udndr"] == 600 * 100_000 + 2 * 4_000
    assert out["pro_rata"] == [1, 1] and TRAP not in by_id(out)
    assert out["inputs"]["jury"]["faults"]["work"] == {"traps": 600, "all": 1200}
    assert out["inputs"]["jury"]["faults"]["drawn_served_honestly"] == 600 + 1                # and gq1


# -- the payer -------------------------------------------------------------------------------------------------------
def _season(path, sets, judge=()):
    from modea import cosmos_addr
    a, b = [cosmos_addr.bech32_encode("dendra", cosmos_addr._convertbits(bytes([n] * 20), 8, 5)) for n in (1, 2)]
    paid = 0
    for d, rules in enumerate(sets):
        kit = R.pays_judge_presence(rules)
        rows = [Identity("m1", verified_requests=300, payout_address=a, kit_version=KIT_MIN if kit else None,
                         judge_active=1 if (kit and d in judge) else 0),
                Identity("m2", verified_requests=300, payout_address=b, kit_version=KIT_MIN if kit else None)]
        r = compute_day(d, rows, paid, rules)
        paid += r.total_paid
        out = to_json(r)
        out["inputs"] = {"last_height": (d + 1) * DAY, "season_end_height": None}
        if kit:
            out["inputs"]["kit_floor"] = {"kit_min": rules["kit_min"], "reports": {}}
        if R.pays_jury_rule(rules):
            out["inputs"]["jury"] = {"kit_min": rules["kit_min"], "fault_periods": rules["fault_periods"],
                                     "fault_payer": rules["fault_payer"]}
        (path / f"day-{d:03d}.json").write_text(json.dumps(out), encoding="utf-8")


def test_the_payer_rebuilds_the_set_from_the_published_day_counts_its_days_as_doubled_and_refuses_a_return(tmp_path,
                                                                                                           monkeypatch):
    import final_season_payout as P
    jury = _jury(monkeypatch)
    sets = [R.rules_for_day_with_jury(d, 1, 4, 6, KIT_DAY, JURY_DAY) for d in range(14)]
    _season(tmp_path, sets, judge=(12,))
    # a payer that knows no jury set: it takes the keys from day 12, bound by the fingerprint
    R.RULES_JURY = R.FAULT_PERIODS = R.FAULT_PAYER = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)
    loaded = [P.load_day(str(tmp_path), d) for d in range(14)]
    assert P.check_days(loaded) == "" and R.fingerprint(R.RULES_JURY) == R.fingerprint(jury)
    assert P.check_doubled_from(loaded, 4) == "" and R.RULES_JURY in P.doubled_sets()
    assert sum(P.plan(1, str(tmp_path), 4)[0].values()) == sum(d.total_paid for d in loaded[7:14])
    # a day under decision 24 after a day under the jury set is refused
    back = tmp_path / "back"
    back.mkdir()
    _season(back, sets[:13] + [R.RULES_KIT])
    bad = P.check_days([P.load_day(str(back), d) for d in range(14)])
    assert bad.startswith("day 13: ranked under rules") and "earlier than the rules of a day before it" in bad
    # a published day whose keys do not give its fingerprint is none of the season's
    other = tmp_path / "other"
    other.mkdir()
    _season(other, sets)
    doc = json.loads((other / "day-012.json").read_text(encoding="utf-8"))
    doc["inputs"]["jury"]["fault_payer"] = "dendra1" + "z" * 38
    (other / "day-012.json").write_text(json.dumps(doc), encoding="utf-8")
    R.RULES_JURY = R.FAULT_PERIODS = R.FAULT_PAYER = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)
    bad = P.check_days([P.load_day(str(other), d) for d in range(13)])
    assert bad.startswith("day 12: ranked under rules") and "none of the season's" in bad and R.RULES_JURY is None


# -- the service fixes the set's day once, with its periods and its payer --------------------------------------------
def _ranked(data, d, rules):
    os.makedirs(os.path.join(data, "ranking"), exist_ok=True)
    with open(os.path.join(data, "ranking", f"day-{d:03d}.json"), "w", encoding="utf-8") as f:
        json.dump({"day": d, "rules_fingerprint": R.fingerprint(rules), "total_paid_udndr": 0}, f)


def _registry(path):
    store = {"signed::m1": {"node_id": "n", "_proven": "m1", "judge_role": "active", "ts": 1, "kit_version": KIT_MIN}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f)
    return str(path)


def _live(data, monkeypatch, tmp_path, ranked_to=11):
    """The owner's service with decision 24 fixed from day 10 and days 0 to `ranked_to` ranked, the chain in day 11."""
    import final_season_server as S
    _no_jury(monkeypatch)
    monkeypatch.setattr(S, "CAPACITY_DB", _registry(tmp_path / "capacity.json"))
    monkeypatch.setattr(S, "RELAY_STORE", str(tmp_path / "relay-store"))
    monkeypatch.setattr(S, "FAULT_REVEAL_DIR", str(tmp_path / "reveal"))
    _ranked(data, 0, R.RULES_17)
    monkeypatch.setattr(C, "height", lambda rpc: START + 9 * DAY + 100)
    S.State(data, 1, "1", "4", "6", "10", "none")
    for d in range(1, ranked_to + 1):
        _ranked(data, d, R.rules_for_day(d, 1, 4, 6, 10))
    monkeypatch.setattr(C, "height", lambda rpc: START + 11 * DAY + 100)
    return S


def _named(monkeypatch, periods=((JURY_DAY, JURY_DAY + 1, COMMIT),), payer=PAYER):
    """The environment of the start that fixes the day: the periods and the payer, as final_season_rules reads them."""
    monkeypatch.setattr(R, "FAULT_PERIODS", R.check_fault_periods(periods))
    monkeypatch.setattr(R, "FAULT_PAYER", payer)
    monkeypatch.setattr(R, "RULES_JURY", R.jury_rules(KIT_MIN, periods, payer))
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS + (R.RULES_KIT, R.RULES_JURY))


def _jury_file(data):
    with open(os.path.join(data, "jury-rules.json"), encoding="utf-8") as f:
        return json.load(f)


def test_the_service_fixes_the_day_with_its_periods_and_payer_once_and_the_record_wins(tmp_path, monkeypatch, capsys):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    # no commitment named and no day: the set cannot exist, no day is under it
    assert S.State(data, 1, "", "", "", "", "").jury_rules_from is None
    # a day named without the commitments: refused, nothing written
    with pytest.raises(SystemExit, match="are required with it"):
        S.State(data, 1, "", "", "", "", "12")
    _named(monkeypatch)
    # the commitments named and no day: refused, never read as "no day"
    with pytest.raises(SystemExit, match="DENDRA_FINAL_SEASON_JURY_RULES_FROM_DAY is not set"):
        S.State(data, 1, "", "", "", "", "")
    assert S.State(data, 1, "", "", "", "", "none").jury_rules_from is None
    assert not os.path.exists(os.path.join(data, "jury-rules.json"))
    st = S.State(data, 1, "", "", "", "", "12")
    assert st.jury_rules_from == 12
    rec = _jury_file(data)
    assert rec == {"from_day": 12, "set_by": "parameter", "first_unranked_day": 12, "kit_min": KIT_MIN,
                   "fault_payer": PAYER, "fault_periods": [PERIOD], "fixed_at_height": START + 11 * DAY + 100,
                   "rules_fingerprint": R.fingerprint(R.RULES_JURY), "rules_fingerprint_before": FINGERPRINT_KIT_3}
    assert "the jury set (verdicts paid after the reveal, fault periods 12-13) applies from day 12" in \
        capsys.readouterr().out
    # a later start with no environment reads the record and builds the set from it
    jury_fp = R.fingerprint(R.RULES_JURY)
    monkeypatch.setattr(R, "FAULT_PERIODS", None)
    monkeypatch.setattr(R, "FAULT_PAYER", None)
    monkeypatch.setattr(R, "RULES_JURY", None)
    monkeypatch.setattr(R, "RULE_HISTORY", R.PUBLISHED_SETS + (R.RULES_KIT,))
    assert S.State(data, 1, "", "", "", "", "").jury_rules_from == 12 and R.fingerprint(R.RULES_JURY) == jury_fp
    # another payer named against the record: refused; another day: refused; none: refused
    monkeypatch.setattr(R, "FAULT_PAYER", "dendra1" + "z" * 38)
    with pytest.raises(SystemExit, match="other fault periods or another"):
        S.State(data, 1, "", "", "", "", "")
    monkeypatch.setattr(R, "FAULT_PAYER", PAYER)
    with pytest.raises(SystemExit, match="names another day"):
        S.State(data, 1, "", "", "", "", "13")
    with pytest.raises(SystemExit, match="already apply from day 12"):
        S.State(data, 1, "", "", "", "", "none")
    # the status names the day and the set
    st = S.State(data, 1, "", "", "", "", "12")
    monkeypatch.setattr(S, "ST", st)
    st.latest_time = S.END - 10 * 86400
    st.height = START + 12 * DAY + 5
    got = S.Handler._status(None)
    assert got["jury_rules_from_day"] == 12 and got["rules"] == R.RULES_JURY and got["rules_fingerprint"] == jury_fp
    st.height = START + 11 * DAY + 5
    assert S.Handler._status(None)["rules"] == R.RULES_KIT


def test_the_service_refuses_a_day_that_has_begun_a_period_before_it_and_no_reveal_directory(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    _named(monkeypatch)
    monkeypatch.setattr(C, "height", lambda rpc: START + 12 * DAY + 5)
    with pytest.raises(SystemExit, match="names a day that has begun"):
        S.State(data, 1, "", "", "", "", "12")
    monkeypatch.setattr(C, "height", lambda rpc: START + 11 * DAY + 100)
    _named(monkeypatch, periods=((JURY_DAY - 1, JURY_DAY + 1, COMMIT),))
    with pytest.raises(SystemExit, match="starts before day 12"):
        S.State(data, 1, "", "", "", "", "12")
    # a day of the set before the first period, or between two, would pay the next period's secret identities
    _named(monkeypatch, periods=((JURY_DAY + 1, JURY_DAY + 2, COMMIT),))
    with pytest.raises(SystemExit, match="the first fault period starts on day 13, after day 12"):
        S.State(data, 1, "", "", "", "", "12")
    _named(monkeypatch, periods=((JURY_DAY, JURY_DAY + 1, COMMIT), (JURY_DAY + 3, JURY_DAY + 4, "c" * 64)))
    with pytest.raises(SystemExit, match="leave days between them under the set with no period"):
        S.State(data, 1, "", "", "", "", "12")
    _named(monkeypatch)
    monkeypatch.setattr(S, "FAULT_REVEAL_DIR", "")
    with pytest.raises(SystemExit, match="DENDRA_FINAL_SEASON_FAULT_REVEAL_DIR"):
        S.State(data, 1, "", "", "", "", "12")
    with pytest.raises(SystemExit, match="already ranked"):
        S.State(data, 1, "", "", "", "", "11")
    assert not os.path.exists(os.path.join(data, "jury-rules.json"))
    fresh = str(tmp_path / "fresh")
    os.makedirs(fresh)
    with pytest.raises(SystemExit, match="before decision 24"):
        S.State(fresh, 1, "1", "4", "6", "none", "12")
    assert not os.path.exists(os.path.join(fresh, "jury-rules.json"))


def test_the_ranking_loop_hands_the_jury_day_and_the_reveal_directory(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    S = _live(data, monkeypatch, tmp_path)
    _named(monkeypatch)
    st = S.State(data, 1, "", "", "", "", "12")
    monkeypatch.setattr(S, "ST", st)
    seen = []

    def rank_day(d, recs, *a, **k):
        seen.append((d, k))
        raise ValueError("not final")

    class Stop(BaseException):
        pass
    calls = []

    def sleep(_s):
        calls.append(_s)
        if len(calls) > 1:
            raise Stop
    monkeypatch.setattr(S.RK, "rank_day", rank_day)
    monkeypatch.setattr(S.RK, "finality_blocks", lambda params, rules: 100)
    monkeypatch.setattr(S.RK, "epoch_blocks", lambda params: 288)
    monkeypatch.setattr(S.C, "jobs_params", lambda rest: {})
    monkeypatch.setattr(S, "time", types.SimpleNamespace(sleep=sleep, time=lambda: 0.0, monotonic=lambda: 0.0))
    _ranked(data, 12, R.RULES_JURY)
    with pytest.raises(Stop):
        S.rank_days()
    assert seen == [(13, {"unwound_from": 1, "doubled_from": 4, "more_requests_from": 6, "kit_rules_from": 10,
                          "relay_store": str(tmp_path / "relay-store"), "jury_rules_from": 12,
                          "fault_reveal_dir": str(tmp_path / "reveal")})]


# -- the chain's readers ---------------------------------------------------------------------------------------------
def test_the_unwind_reader_reads_the_end_of_block_event_and_answers_none_when_the_index_holds_none(monkeypatch):
    blocks = {"j1": [120, 90]}

    def get(url, timeout=10.0):
        if "/block_search?" in url:
            jid = re.search(r"job_id%3D%27([^%]+)%27", url).group(1)
            hs = blocks.get(jid, [])
            return {"result": {"blocks": [{"block": {"header": {"height": str(h)}}} for h in hs],
                               "total_count": str(len(hs))}}
        m = re.search(r"/block_results\?height=(\d+)", url)
        if m:
            return {"result": {"finalize_block_events": [
                {"type": C.UNWIND_EVENT, "attributes": [{"key": "job_id", "value": "j1"}]},
                {"type": "audit_expired", "attributes": [{"key": "job_id", "value": "j1"}]}]}}
        raise AssertionError(url)
    monkeypatch.setattr(C, "_get", get)
    assert C.audit_unwind_height(RPC, "j1") == 90
    assert C.audit_unwind_height(RPC, "j2") is None
    # a block the index names, whose results hold no event list or not the event: unread, never "no unwind"
    for results in ({"result": {}}, {"result": {"finalize_block_events": None}}, {},
                    {"result": {"finalize_block_events": [{"type": "audit_expired",
                                                           "attributes": [{"key": "job_id", "value": "j1"}]}]}}):
        monkeypatch.setattr(C, "_get", lambda url, timeout=10.0, r=results: (
            get(url) if "/block_search?" in url else r))
        with pytest.raises(C.ChainUnreadable, match="block 90"):
            C.audit_unwind_height(RPC, "j1")

    def down(url, timeout=10.0):
        raise C.ChainUnreadable("no answer")
    monkeypatch.setattr(C, "_get", down)
    with pytest.raises(C.ChainUnreadable):
        C.audit_unwind_height(RPC, "j1")
    # the mutant that reads such a block as no event answers None, and is seen
    mut = mutated(C, swap('        raise ChainUnreadable(f"block {h} is named by the index for {UNWIND_EVENT}',
                          '        continue' + NL + '        raise ChainUnreadable(f"block {h} is named by the index for '
                          '{UNWIND_EVENT}'))
    mut._get = lambda url, timeout=10.0: get(url) if "/block_search?" in url else {"result": {"finalize_block_events": []}}
    assert mut.audit_unwind_height(RPC, "j1") is None
    monkeypatch.setattr(C, "_get", mut._get)
    with pytest.raises(C.ChainUnreadable, match="block 90"):
        C.audit_unwind_height(RPC, "j1")


def _send(frm, to, amount, denom="udndr"):
    return {"@type": C.SEND_ACTION, "from_address": frm, "to_address": to, "amount": [{"denom": denom, "amount": amount}]}


def test_the_memo_reader_takes_the_first_transfer_of_one_udndr_from_the_payer_to_itself(monkeypatch):
    memo = R.fault_memo(COMMIT)
    other = "dendra1" + "z" * 38
    txs = [{"height": "70", "tx": {"body": {"memo": memo, "messages": [_send(PAYER, PAYER, "1")]}}},
           {"height": "50", "tx": {"body": {"memo": memo, "messages": [_send(PAYER, PAYER, "1")]}}},
           {"height": "40", "tx": {"body": {"memo": memo, "messages": [_send(PAYER, other, "1")]}}},
           {"height": "30", "tx": {"body": {"memo": memo, "messages": [_send(PAYER, PAYER, "2")]}}},
           {"height": "25", "tx": {"body": {"memo": memo, "messages": [_send(PAYER, PAYER, "1"), _send(PAYER, PAYER, "1")]}}},
           {"height": "20", "code": 5, "tx": {"body": {"memo": memo, "messages": [_send(PAYER, PAYER, "1")]}}},
           {"height": "10", "tx": {"body": {"memo": memo + "0", "messages": [_send(PAYER, PAYER, "1")]}}}]
    queries = []

    def cli(args, node, timeout=60):
        queries.append(args[3])
        return {"txs": txs, "total_count": str(len(txs))}
    monkeypatch.setattr(C, "_cli", cli)
    assert C.fault_commitment_height(NODE, PAYER, memo) == 50
    assert queries == [f"message.sender='{PAYER}'"]
    assert C.fault_commitment_height(NODE, PAYER, R.fault_memo("c" * 64)) is None
    del txs[:2]
    assert C.fault_commitment_height(NODE, PAYER, memo) is None              # none of the rest is the commitment
    for bad in ("", None, "x' OR 1", "cosmos1abc"):
        with pytest.raises(C.ChainUnreadable, match="cannot be searched"):
            C.fault_commitment_height(NODE, bad, memo)


# -- the command line ------------------------------------------------------------------------------------------------
def test_the_command_line_reads_the_jury_day_and_the_set_from_the_status_and_replays_a_published_day(chain, monkeypatch,
                                                                                                    capsys):
    jury = R.RULES_JURY
    published = rank(chain)
    status = {"unwound_audit_work_from_day": 1, "doubled_rates_from_day": 4, "more_requests_from_day": 6,
              "kit_rules_from_day": KIT_DAY, "jury_rules_from_day": JURY_DAY, "rules": jury,
              "rules_fingerprint": R.fingerprint(jury)}
    reads = []

    def fetch(url):
        reads.append(url)
        if url == "http://e/status":
            return json.dumps(status).encode()
        if url == "http://e/ranking/day-012.json":
            return json.dumps(published).encode()
        if url.endswith(".json"):
            return json.dumps({"total_paid_udndr": 0}).encode()
        raise AssertionError(url)
    monkeypatch.setattr(RK, "_fetch", fetch)
    monkeypatch.setattr(RK, "_records", lambda base, day: records())
    monkeypatch.setattr(C, "jobs_params", lambda rest: {})
    monkeypatch.setattr(C, "height", lambda rpc: chain["tip"])
    monkeypatch.setattr(RK, "finality_blocks", lambda params, rules: FINALITY)
    monkeypatch.setattr(RK, "epoch_blocks", lambda params: 288)
    # a process that knows no jury set: it takes it from the status, bound by the status's fingerprint
    R.RULES_JURY = R.FAULT_PERIODS = R.FAULT_PAYER = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)
    base = ["--day", str(JURY_DAY), "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start", str(START)]
    rc = RK.main(base + ["--relay-store", chain["store"], "--fault-reveal", chain["reveal"]])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert json.loads(out) == published and R.fingerprint(R.RULES_JURY) == R.fingerprint(jury)
    # a third party: no store, no reveal directory, the published day in hand -- its reveal is checked again
    rc = RK.main(base + ["--compare", "http://e/ranking/day-012.json", "--relay-store", "", "--fault-reveal", ""])
    assert rc == 0 and capsys.readouterr().out.startswith("identical to the published ranking")
    # a day before decision 24 never reads the status for the jury day
    reads.clear()
    rc = RK.main(["--day", "5", "--evidence", "http://e/", "--node", NODE, "--generator", GEN, "--start", str(START),
                  "--unwound-from", "1", "--doubled-from", "4", "--more-requests-from", "6", "--kit-rules-from", "10"])
    capsys.readouterr()
    assert "http://e/status" not in reads
    assert RK.jury_rules_from_status(status) == JURY_DAY and RK.jury_rules_from_status({}) is None
    for bad in ("12", -1, True):
        with pytest.raises(ValueError, match="jury_rules_from_day"):
            RK.jury_rules_from_status(dict(status, jury_rules_from_day=bad))


# -- mutants: copies of the shipped modules with one bound removed, each run against the case that holds it ---------
NL = chr(10)


def swap(old, new):
    def edit(src):
        assert src.count(old) == 1, f"the mutant's text is not in the module exactly once: {old[:60]!r}"
        return src.replace(old, new)
    return edit


def mutated(module, *edits):
    with open(module.__file__, encoding="utf-8") as f:
        src = f.read()
    for e in edits:
        src = e(src)
    m = types.ModuleType(module.__name__ + "_mutant")
    m.__file__ = module.__file__
    exec(compile(src, module.__file__, "exec"), m.__dict__)
    return m


def _case_jury_values(chain, tmp_path, mod=None):
    rows = by_id(rank(chain, mod=mod))
    assert [rows.get(m, {}).get("verdicts") for m in ("j2", "j6")] == [2, 2] and "j3" not in rows


def _case_blind_one(chain, tmp_path, mod=None):
    out = rank(chain, mod=mod)
    assert "j1" not in by_id(out) and out["inputs"]["jury"]["contradicted"] == ["j1", "j7"]


def _case_refused_without_reveal(chain, tmp_path, mod=None):
    empty = tmp_path / f"empty-{len(os.listdir(tmp_path))}"
    empty.mkdir()
    with pytest.raises(ValueError):
        rank(chain, reveal=str(empty), mod=mod)


def _case_memo_above(chain, tmp_path, mod=None):
    chain["memo"] = FIRST
    with pytest.raises(ValueError):
        rank(chain, mod=mod)


def _case_pro_rata(chain, tmp_path, mod=None):
    recs = _over_budget(chain, 600) if "wp10" not in chain["settled"] else _over_budget_records(600)
    p1 = by_id(rank(chain, recs=recs, mod=mod))["p1"]
    assert p1["paid_udndr"] == 600 * 100_000 + 2 * 4_000


def _over_budget_records(n):
    recs = records()
    recs[1]["answered"] = {"p1": [f"wp1{k}" for k in range(n)], TRAP: [f"w{TRAP}{k}" for k in range(n)]}
    return recs


def _case_left_out(chain, tmp_path, mod=None):
    write_reveal(chain, [e for e in ENTRIES if e["job_id"] != T_NR])
    try:
        with pytest.raises(ValueError, match="leaves out"):
            rank(chain, mod=mod)
    finally:
        write_reveal(chain, ENTRIES)


def _case_no_judge_day_on_unwound(chain, tmp_path, mod=None):
    rows = by_id(rank(chain, mod=mod))
    assert rows["j2"]["judge_active"] == 0 and rows["j6"]["judge_active"] == 0


def _case_store_unread(chain, tmp_path, mod=None):
    with pytest.raises(C.ChainUnreadable):
        rank(chain, store="", mod=mod)


def _case_unwind_lost(chain, tmp_path, mod=None):
    chain["unwound"].pop("a1", None)
    with pytest.raises(C.ChainUnreadable):
        rank(chain, mod=mod)


def _case_before_first_period(chain, tmp_path, mod=None):
    R.RULES_JURY = R.FAULT_PERIODS = R.FAULT_PAYER = None
    R.RULE_HISTORY = R.PUBLISHED_SETS + (R.RULES_KIT,)
    R.set_fault_commitments(((JURY_DAY + 1, JURY_DAY + 2, COMMIT),), PAYER)
    with pytest.raises(ValueError):
        rank(chain, reveal="", mod=mod)


def _case_after_the_period(chain, tmp_path, mod=None):
    if not chain.get("shifted"):
        for k in ("settled", "drawn", "unwound"):
            chain[k] = {j: h + 2 * DAY for j, h in chain[k].items()}
        chain["committed"] = {k: h + 2 * DAY for k, h in chain["committed"].items()}
        chain["shifted"] = True
    assert TRAP not in by_id(rank(chain, day=JURY_DAY + 2, mod=mod))


_PAY_WHY = ('            if t is None:' + NL + '                why = JUDGE_NO_REVEAL' + NL
            + '            elif t == STORE_UNREAD:' + NL + '                why = JUDGE_STORE_UNREAD' + NL + '            else:' + NL
            + '                why = "" if commit_times')
_PAY_WHY_READ = ('            if votes[job][juror] != "1":' + NL + '                why = "vote_read"' + NL
                 + _PAY_WHY.replace('            if t is None:', '            elif t is None:'))
_REMOVE = '    rows = [i for i in rows if i.miner_id not in trap_ids]' + NL
_TO_JSON = '    out = to_json(compute_day(day, rows, paid_before, rules))' + NL
_AFTER = '    out["ranking"] = [r for r in out["ranking"] if r["miner_id"] not in trap_ids]' + NL
_CHECKED = '    checked = [checked_faults(day, rules, p, start, node, fault_reveal_dir, replay_from) for p in begun]' + NL
_CHECKED_ABSENT_EMPTY = ('    checked = []' + NL + '    for p in begun:' + NL + '        try:' + NL
                         + '            checked.append(checked_faults(day, rules, p, start, node, fault_reveal_dir, '
                           'replay_from))' + NL
                         + '        except ValueError as e:' + NL + '            if " yet" not in str(e):' + NL
                         + '                raise' + NL)
_LOST = '    if h is None:' + NL + '        raise C.ChainUnreadable(f"the audit of {jid} is unwound'
MUTANTS = [
    ("the vote's value read for the pay", F, "jury_verdicts", [swap(_PAY_WHY, _PAY_WHY_READ)], _case_jury_values),
    ("the condition of the reveal's time removed", F, "jury_verdicts",
     [swap('why = "" if commit_times[committed[job][juror]] > t else JUDGE_VERDICT_BEFORE_REVEAL', 'why = ""')],
     _case_jury_values),
    ("a false fact contradicts", F, "fault_contradictions",
     [swap('                if cls not in CONTRADICTING_CLASSES:', '                if False:')], _case_blind_one),
    ("a vote outside the faults contradicts", RK, None,
     [swap('contradicted, fault_votes = fault_contradictions(day_faults,',
           'contradicted, fault_votes = fault_contradictions(dict({j: "incoherent_answer" for j in verdict_proof}, '
           '**day_faults),')], _case_blind_one),
    ("the identities removed after the pro rata", RK, None, [swap(_REMOVE, ""), swap(_TO_JSON, _TO_JSON + _AFTER)],
     _case_pro_rata),
    ("an absent reveal read as a reveal without faults", RK, None, [swap(_CHECKED, _CHECKED_ABSENT_EMPTY)],
     _case_refused_without_reveal),
    ("a memo at or above the period's first block accepted", RK, None,
     [swap('    if not memo_h < fb:', '    if False:')], _case_memo_above),
    ("a drawn request left out of the reveal accepted", RK, None, [swap('        if missing:', '        if False:')],
     _case_left_out),
    ("the judge day seated on unwound audits", RK, None,
     [swap('            if audit_outcome(by_id[jid]) is None:' + NL + '                continue' + NL, '')],
     _case_no_judge_day_on_unwound),
    ("a relay store that cannot be read ranks the day", RK, None,
     [swap('        if jury and replay_from is None and store_state == STORE_UNREAD:', '        if False:')],
     _case_store_unread),
    ("an unwind the index lost read as unknown", RK, None,
     [swap(_LOST, '    if h is None:' + NL + '        return None' + NL
           + '        raise C.ChainUnreadable(f"the audit of {jid} is unwound')], _case_unwind_lost),
    ("a day before the first period ranked", RK, None,
     [swap('    if jury and period is None and len(begun) < len(rules["fault_periods"]):', '    if False:')],
     _case_before_first_period),
    ("the identities of a period paid after it", RK, None,
     [swap(_REMOVE, '    rows = [i for i in rows if i.miner_id not in trap_ids or period is None]' + NL)],
     _case_after_the_period),
]


@pytest.mark.parametrize("label,module,fn,edits,case", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_a_mutant_is_seen(chain, tmp_path, monkeypatch, label, module, fn, edits, case):
    case(chain, tmp_path)                                                # green on the shipped modules
    mut = mutated(module, *edits)
    if fn is not None:
        # a function of the facts, as the ranking imported it
        monkeypatch.setattr(RK, fn, getattr(mut, fn))
        mut = None
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        case(chain, tmp_path, mod=mut)
