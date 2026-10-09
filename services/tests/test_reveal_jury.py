"""THE REVEAL GOES TO THE ANCHORED JURY, AND TO NOBODY ELSE (`reveal_helpers.read_jury`,
`reveal_helpers.decide_reveal`, `reveal_worker.reveal_disputed_job`, `reveal_worker.main`).

WHAT THIS BENCH DEFENDS. A primary that reveals an audited job hands a client's prompt and its answer
to whoever it seals them to. Only the jury the chain ANCHORED on the job can vote
(`antievasion.go::auditVerdictTally`), so every other recipient is a reader with no role. The bench
locks both halves:
  - what must OPEN: each anchored juror receives a copy it can open, with the salt that lets it check
    the anchored prompt commitment;
  - what must stay SHUT: a registered miner outside the jury receives nothing and cannot open a juror's
    copy; a jury that cannot be read, that is not anchored yet, that is empty, or that belongs to a
    human dispute seals NOTHING -- never a copy for "every registered miner".

THE CHAIN OUTPUTS ARE THE CLI'S OWN. The success JSON, the two NotFound lines and the unreachable-node
line below are what `dendrad query jobs audit-committee` prints (stdout and stderr kept apart), so a
reader written against another shape goes red here rather than on a live audit.

THE SHIPPED PATH IS EXECUTED, NOT RE-WRITTEN. The end-to-end cases call `reveal_disputed_job` and
`main()` from the module, with real X25519/AES-GCM, an in-memory relay and a fake `dendrad` answering
per command. Section (6) goes one step further: the shipped runner (`reveal_worker.run_rc`) starts a
`dendrad` EXECUTABLE found on a hermetic PATH, so the stream split, the timeout and the "could not run"
answer are those of the code that ships. Not covered here: the real `dendrad` binary and the network.
"""
import json
import os
import sys
import time as _time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modea import crypto
import relay_client as relay
import reveal_helpers as rv
import reveal_worker as rw

RELAY_URL = "http://relay.invalid"
ME = "dm1primaryaaaaaaaaaaaaaaaa"
JOB = "job1791487835079"
PROMPT = "Summarise the attached clause in one sentence."
ANSWER = "The clause caps liability at the fees paid in the last twelve months."

# --- what the CLI prints (stdout / stderr), kept verbatim -------------------------------------------
SONIC_WARNING = ("WARNING: sonic/ast only supports (go1.17~1.26 and amd64 CPU) or (go1.20~1.26 and arm64 "
                 "CPU), but your environment is not suitable and will fallback to encoding/json\n")
NOT_ANCHORED = ("rpc error: code = NotFound desc = rpc error: code = NotFound desc = no jury anchored for "
                "this job (the audit was never opened, or was deferred for want of an eligible pool) "
                "— this is NOT an empty jury: key not found\n")
UNKNOWN_JOB = "rpc error: code = NotFound desc = rpc error: code = NotFound desc = unknown job: key not found\n"
UNREACHABLE = ('post failed: Post "http://127.0.0.1:1": dial tcp 127.0.0.1:1: connect: connection '
               'refused\n')
# A dendrad built before `query jobs audit-committee` existed (stderr, rc=1): the usage of `query jobs`,
# abridged here (its list of subcommands is cut), then the error on the first flag.
OLD_CLI = ('Usage:\n  dendrad query jobs [flags]\n  dendrad query jobs [command]\n\n'
           'Flags:\n  -h, --help   help for jobs\n\n'
           'Use "dendrad query jobs [command] --help" for more information about a command.\n\n'
           'unknown flag: --output\n')


def ok_json(members, height="31666"):
    d = {}
    if members:
        d["members"] = list(members)
    if height:
        d["anchored_height"] = height
    return json.dumps(d, indent=2) + "\n"


class FakeChain:
    """A `run_rc`-shaped runner: answers `audit-committee` per (job, redo) and records every command.

    An answer is `(rc, stdout, stderr)`. A command this fake does not know raises, so a reader that
    drifts to another query (`assigned-committee`, `list-miner`) names itself instead of reading a
    plausible default."""

    def __init__(self, audit, redo=(1, "", NOT_ANCHORED)):
        self.answers = {False: audit, True: redo}
        self.cmds = []

    def __call__(self, cmd, t=60):
        self.cmds.append(list(cmd))
        if cmd[:4] != ["dendrad", "query", "jobs", "audit-committee"]:
            raise AssertionError(f"unexpected command: {cmd}")
        ans = self.answers["--redo" in cmd]
        if isinstance(ans, BaseException):
            raise ans
        return ans


class FakeRelay:
    """In-memory relay with the write-once guard; every get is recorded."""

    def __init__(self):
        self.store = {}
        self.gets = []

    def put_status(self, base, kind, key, obj, **kw):
        if f"{kind}/{key}" in self.store:
            return "exists"
        self.store[f"{kind}/{key}"] = obj
        return "ok"

    def put(self, base, kind, key, obj, **kw):
        return self.put_status(base, kind, key, obj, **kw) == "ok"

    def get(self, base, kind, key, retries=1):
        self.gets.append(f"{kind}/{key}")
        return self.store.get(f"{kind}/{key}")

    def reveals(self):
        return sorted(k for k in self.store if k.startswith("reveal/"))


class _NetworkForbidden(BaseException):
    """BaseException: `relay_client` turns an `Exception` into a quiet "refused"; this one crosses."""


@pytest.fixture
def fake_relay(monkeypatch):
    fr = FakeRelay()
    monkeypatch.setattr(relay, "put_status", fr.put_status)
    monkeypatch.setattr(relay, "put", fr.put)
    monkeypatch.setattr(relay, "get", fr.get)

    def _forbidden(*a, **k):
        raise _NetworkForbidden("relay_client reached the network: a transport entry point is not faked")

    monkeypatch.setattr(relay.urllib.request, "urlopen", _forbidden)
    return fr


class World:
    """A primary with a served job sealed at the relay, and a registry of keyed miners."""

    def __init__(self, fr, n_miners=6):
        self.sk, self.pk = crypto.gen_keypair()
        self.keys = {}
        for i in range(n_miners):
            self.keys[f"dm1miner{i}aaaaaaaaaaaaaaaa"] = crypto.gen_keypair()
        self.registry = [{"miner_id": ME, "enc_pubkey": self.pk.hex()}] + [
            {"miner_id": m, "enc_pubkey": pk.hex()} for m, (sk, pk) in self.keys.items()]
        self.fr = fr
        self.seed(JOB)
        self.miners_calls = 0

    def seed(self, job_id, prompt=PROMPT, answer=ANSWER):
        """The client's sealed prompt and this primary's sealed answer for `job_id`, at the relay."""
        aad = job_id.encode()
        c_sk, c_pk = crypto.gen_keypair()
        k = crypto.derive_session_key(c_sk, self.pk, info=aad)
        req = crypto.encrypt(k, prompt.encode(), aad=aad)
        res = crypto.encrypt(k, answer.encode(), aad=aad)
        self.fr.store[f"req/{job_id}__{ME}"] = {"client_eph_pk": c_pk.hex(), "nonce": req.nonce.hex(),
                                                "ct": req.ct.hex()}
        self.fr.store[f"res/{job_id}__{ME}"] = {"nonce": res.nonce.hex(), "ct": res.ct.hex()}

    def ids(self):
        return list(self.keys)

    def miners(self):
        self.miners_calls += 1
        return self.registry


def reveal(world, chain):
    return rw.reveal_disputed_job(ME, RELAY_URL, world.sk, JOB, node_args=[], runner=chain,
                                  miners_fn=world.miners)


# === (1) read_jury: three states, from what the CLI really prints ===================================

def test_a_read_jury_returns_its_members_in_anchor_order():
    jury = ["dm1c", "dm1a", "dm1b"]
    r = rv.read_jury(JOB, [], FakeChain((0, ok_json(jury), SONIC_WARNING)))
    assert r.state == rv.JURY_READ and r.members == tuple(jury)


def test_a_warning_on_stderr_does_not_spoil_the_json_on_stdout():
    # `run()` glues the streams; the jury reader must not, or this answer would read as UNKNOWN.
    r = rv.read_jury(JOB, [], FakeChain((0, ok_json(["dm1a"]), SONIC_WARNING)))
    assert r.state == rv.JURY_READ


@pytest.mark.parametrize("out", [ok_json([], height="12"), "{}\n"])
def test_an_omitted_members_field_is_an_EMPTY_jury_not_a_failure(out):
    # proto3 omits an empty repeated field: `{}` IS the encoding of an anchor with no member.
    r = rv.read_jury(JOB, [], FakeChain((0, out, "")))
    assert r.state == rv.JURY_READ and r.members == ()


def test_a_field_added_next_to_members_does_not_stop_the_reveal():
    # An additive change of the response must not silence every primary (silence is slashed).
    out = json.dumps({"members": ["dm1a", "dm1b"], "anchored_height": "9", "seated_by": "draw"})
    r = rv.read_jury(JOB, [], FakeChain((0, out, "")))
    assert r.state == rv.JURY_READ and r.members == ("dm1a", "dm1b")


@pytest.mark.parametrize("err", [NOT_ANCHORED, UNKNOWN_JOB])
def test_notfound_is_ABSENT(err):
    r = rv.read_jury(JOB, [], FakeChain((1, "", SONIC_WARNING + err)))
    assert r.state == rv.JURY_ABSENT


@pytest.mark.parametrize("answer", [
    (1, "", UNREACHABLE),                                          # node down
    (1, "", "rpc error: code = Unavailable desc = connection closed\n"),
    (1, "", ""),                                                   # failed, silent
    (None, "", "TimeoutExpired: timed out after 30 seconds"),      # did not run
    (0, "not json", ""),
    (0, "[]", ""),
    (0, json.dumps({"jurors": ["dm1a"]}), ""),                     # another message, not "empty"
    (0, json.dumps({"members": "dm1a,dm1b"}), ""),                 # not a list
    (0, json.dumps({"members": ["dm1a", 7]}), ""),                 # not an identifier
    (0, json.dumps({"members": ["dm1a", "--node"]}), ""),          # would become an option
    (0, json.dumps({"members": ["dm1a", "a/b"]}), ""),             # would break a relay key
    # the status is read from stderr ALONE: a NotFound line on stdout does not make an outage "absent"
    (1, "rpc error: code = NotFound desc = x\n", "rpc error: code = Unavailable desc = connection closed\n"),
    (1, "", OLD_CLI),                                              # a dendrad without the query
])
def test_anything_else_is_UNKNOWN_never_absent_never_empty(answer):
    r = rv.read_jury(JOB, [], FakeChain(answer))
    assert r.state == rv.JURY_UNKNOWN and r.members == ()


def test_the_unknown_detail_names_the_error_not_the_warning_printed_before_it():
    r = rv.read_jury(JOB, [], FakeChain((1, "", SONIC_WARNING + UNREACHABLE)))
    assert r.state == rv.JURY_UNKNOWN and "connection refused" in r.detail and "sonic" not in r.detail


def test_a_dendrad_without_the_query_is_named_as_such():
    # Waiting does not cure it, so "rc=1: unknown flag: --output" alone would send the operator looking
    # at the network. The cause is in the detail, and nothing is sealed.
    r = rv.read_jury(JOB, [], FakeChain((1, "", SONIC_WARNING + OLD_CLI)))
    assert r.state == rv.JURY_UNKNOWN and "unknown flag: --output" in r.detail
    assert "does not know `query jobs audit-committee`" in r.detail and "same source" in r.detail
    assert rv.decide_reveal(r, None, ME).action == rv.WAIT_UNREADABLE
    # ... and only that answer carries it: an outage does not.
    assert "does not know" not in rv.read_jury(JOB, [], FakeChain((1, "", UNREACHABLE))).detail


def test_read_jury_hands_its_timeout_to_the_runner():
    seen = []

    def runner(cmd, t):                     # no default: a call without the timeout raises here
        seen.append(t)
        return 0, ok_json(["dm1a"]), ""

    assert rv.read_jury(JOB, [], runner, t=7).state == rv.JURY_READ and seen == [7]


def test_a_runner_that_raises_is_UNKNOWN():
    r = rv.read_jury(JOB, [], FakeChain(OSError("dendrad: not found")))
    assert r.state == rv.JURY_UNKNOWN


def test_the_query_is_audit_committee_with_flags_before_the_terminator():
    chain = FakeChain((0, ok_json(["dm1a"]), ""))
    rv.read_jury(JOB, ["--node", "tcp://node:26657"], chain)
    rv.read_jury(JOB, ["--node", "tcp://node:26657"], chain, redo=True)
    plain, redo = chain.cmds
    assert plain == ["dendrad", "query", "jobs", "audit-committee", "--output", "json",
                     "--node", "tcp://node:26657", "--", JOB]
    assert redo.index("--redo") < redo.index("--") and redo[-1] == JOB
    assert not any("assigned-committee" in c for c in plain + redo), "the WORK committee is not the jury"


def test_duplicates_in_the_anchor_are_counted_once():
    r = rv.read_jury(JOB, [], FakeChain((0, ok_json(["dm1a", "dm1b", "dm1a"]), "")))
    assert r.members == ("dm1a", "dm1b")


# === (2) decide_reveal: the only state that seals is a READ jury with someone in it =================

READ = rv.JuryRead(rv.JURY_READ, ("dm1a", "dm1b"), "")
EMPTY = rv.JuryRead(rv.JURY_READ, (), "")
ABSENT = rv.JuryRead(rv.JURY_ABSENT, (), "NotFound")
UNKNOWN = rv.JuryRead(rv.JURY_UNKNOWN, (), "rc=1: post failed")


@pytest.mark.parametrize("audit,redo,action", [
    (READ, None, rv.SEAL),
    (READ, READ, rv.SEAL),                         # an audit jury wins over a re-adjudication jury
    (EMPTY, None, rv.NO_REVEAL_EMPTY_JURY),
    (UNKNOWN, None, rv.WAIT_UNREADABLE),
    (ABSENT, READ, rv.NO_REVEAL_HUMAN_DISPUTE),
    (ABSENT, EMPTY, rv.NO_REVEAL_HUMAN_DISPUTE),
    (ABSENT, ABSENT, rv.WAIT_NO_JURY),
    (ABSENT, UNKNOWN, rv.WAIT_UNREADABLE),
    (ABSENT, None, rv.WAIT_UNREADABLE),
    (rv.JuryRead("weird", ("dm1a",), ""), None, rv.WAIT_UNREADABLE),
])
def test_decision_table(audit, redo, action):
    plan = rv.decide_reveal(audit, redo, ME)
    assert plan.action == action
    assert (plan.members != ()) == (action == rv.SEAL)


def test_the_primary_is_never_its_own_target_and_a_jury_of_itself_is_empty():
    assert rv.decide_reveal(rv.JuryRead(rv.JURY_READ, (ME, "dm1a"), ""), None, ME).members == ("dm1a",)
    assert rv.decide_reveal(rv.JuryRead(rv.JURY_READ, (ME,), ""), None, ME).action == rv.NO_REVEAL_EMPTY_JURY


def test_the_redo_anchor_is_read_only_when_the_audit_anchor_is_absent():
    chain = FakeChain((0, ok_json(["dm1a"]), ""))
    rv.plan_reveal(JOB, ME, [], chain)
    assert len(chain.cmds) == 1
    chain = FakeChain((1, "", NOT_ANCHORED))
    rv.plan_reveal(JOB, ME, [], chain)
    assert len(chain.cmds) == 2 and "--redo" in chain.cmds[1]


# === (3) end to end, through the shipped `reveal_disputed_job` ======================================

def test_sealed_to_the_jury_only_and_every_juror_can_open(fake_relay):
    w = World(fake_relay, n_miners=6)
    jury = w.ids()[:3]
    outsiders = w.ids()[3:]
    o = reveal(w, FakeChain((0, ok_json(jury), "")))
    assert o.action == rw.REVEALED and o.delivered == 3 and set(o.targets) == set(jury)
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in jury)
    psalt = crypto.prompt_salt(w.sk, JOB)
    for m in jury:
        opened = rv.open_reveal(RELAY_URL, JOB, m, w.keys[m][0], ME)
        assert opened == {"prompt": PROMPT, "answer": ANSWER, "psalt": psalt}
    for out in outsiders:
        assert rv.open_reveal(RELAY_URL, JOB, out, w.keys[out][0], ME) is None, "no copy for an outsider"
        for m in jury:   # and an outsider's key opens no juror's copy
            assert rv.open_reveal(RELAY_URL, JOB, m, w.keys[out][0], ME) is None


@pytest.mark.parametrize("audit,redo,action", [
    ((1, "", UNREACHABLE), (1, "", NOT_ANCHORED), rv.WAIT_UNREADABLE),
    ((0, "garbage", ""), (1, "", NOT_ANCHORED), rv.WAIT_UNREADABLE),
    ((1, "", NOT_ANCHORED), (1, "", UNREACHABLE), rv.WAIT_UNREADABLE),
    ((1, "", NOT_ANCHORED), (1, "", NOT_ANCHORED), rv.WAIT_NO_JURY),
    ((0, ok_json([]), ""), (1, "", NOT_ANCHORED), rv.NO_REVEAL_EMPTY_JURY),
])
def test_no_jury_read_means_no_deposit_at_all(fake_relay, audit, redo, action):
    w = World(fake_relay, n_miners=6)
    o = reveal(w, FakeChain(audit, redo))
    assert o.action == action and o.delivered == 0
    assert fake_relay.reveals() == [], "nothing may be sealed -- above all not to the whole registry"
    # The jury is read FIRST: the cleartext is never brought back, the registry never consulted.
    assert not any(g.startswith(("req/", "res/")) for g in fake_relay.gets)
    assert w.miners_calls == 0


def test_a_human_dispute_seals_nothing_not_even_to_the_redo_jury(fake_relay):
    w = World(fake_relay, n_miners=6)
    redo_jury = w.ids()[:3]
    o = reveal(w, FakeChain((1, "", NOT_ANCHORED), (0, ok_json(redo_jury), "")))
    assert o.action == rv.NO_REVEAL_HUMAN_DISPUTE and o.delivered == 0
    assert fake_relay.reveals() == []


def test_a_juror_with_no_key_anywhere_is_reported_and_the_others_are_served(fake_relay):
    w = World(fake_relay, n_miners=4)
    ghost = "dm1exitedaaaaaaaaaaaaaaaaa"            # anchored, then gone from the registry
    jury = w.ids()[:2] + [ghost]
    o = reveal(w, FakeChain((0, ok_json(jury), "")))
    assert o.action == rw.REVEALED and o.delivered == 2 and o.unkeyed == (ghost,)
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in w.ids()[:2])


def test_a_jury_where_nobody_has_a_key_is_no_key_and_seals_nothing(fake_relay):
    w = World(fake_relay, n_miners=4)
    o = reveal(w, FakeChain((0, ok_json(["dm1ghostaaaaaaaaaaaaaaaaaa"]), "")))
    assert o.action == rw.NO_KEY and o.delivered == 0 and fake_relay.reveals() == []


def test_an_unreadable_registry_seals_nothing(fake_relay):
    w = World(fake_relay, n_miners=4)
    w.miners = lambda: None
    o = reveal(w, FakeChain((0, ok_json(w.ids()[:2]), "")))
    assert o.action == rw.REGISTRY_UNREADABLE and fake_relay.reveals() == []


def test_a_retry_after_a_full_delivery_still_counts_every_juror(fake_relay):
    w = World(fake_relay, n_miners=5)
    chain = FakeChain((0, ok_json(w.ids()[:3]), ""))
    assert reveal(w, chain).action == rw.REVEALED
    again = reveal(w, chain)                       # write-once: "exists" counts as delivered
    assert again.action == rw.REVEALED and again.delivered == 3 and len(fake_relay.reveals()) == 3


def test_a_partial_delivery_is_INCOMPLETE_not_a_reveal(fake_relay, monkeypatch):
    w = World(fake_relay, n_miners=4)
    jury = w.ids()[:3]
    real = fake_relay.put_status

    def refuses_the_third(base, kind, key, obj, **kw):
        return "refused" if jury[2] in key else real(base, kind, key, obj, **kw)

    monkeypatch.setattr(relay, "put_status", refuses_the_third)
    o = reveal(w, FakeChain((0, ok_json(jury), "")))
    assert o.action == rw.INCOMPLETE and o.delivered == 2


def test_an_outcome_reached_under_a_seal_plan_carries_it_and_a_carried_plan_is_not_read_again(fake_relay):
    w = World(fake_relay, n_miners=4)
    del fake_relay.store[f"req/{JOB}__{ME}"]
    chain = FakeChain((0, ok_json(w.ids()[:2]), ""))
    first = reveal(w, chain)
    assert first.action == rw.REQ_UNREADABLE and first.plan.action == rv.SEAL and len(chain.cmds) == 1
    again = rw.reveal_disputed_job(ME, RELAY_URL, w.sk, JOB, node_args=[], runner=chain,
                                   miners_fn=w.miners, plan=first.plan)
    assert again.action == rw.REQ_UNREADABLE and len(chain.cmds) == 1, "the carried jury was read again"
    # a plan that seals nothing is never carried: that jury may still be seated
    wait = reveal(w, FakeChain((1, "", UNREACHABLE)))
    assert wait.action == rv.WAIT_UNREADABLE and wait.plan is None


# === (3b) a key that is not a key ===================================================================

GOOD_HEX = crypto.gen_keypair()[1].hex()


@pytest.mark.parametrize("pub,usable", [
    pytest.param(GOOD_HEX, True, id="key"),
    pytest.param(GOOD_HEX.upper(), True, id="upper-case"),   # the chain's hex decoder takes either case
    pytest.param("zz" * 32, False, id="not-hex"),
    pytest.param(GOOD_HEX[:62], False, id="31-bytes"),
    pytest.param(GOOD_HEX + "00", False, id="33-bytes"),
    pytest.param(" ".join([GOOD_HEX[:32], GOOD_HEX[32:]]), False, id="space"),  # `bytes.fromhex` takes it
    pytest.param("00" * 32, False, id="low-order"),          # X25519 refuses the shared secret
    pytest.param(None, False, id="none"),
    pytest.param(1234, False, id="int"),
])
def test_usable_pub(pub, usable):
    assert rv.usable_pub(pub) is usable


def test_an_anchored_key_that_is_unusable_leaves_the_juror_unkeyed_never_the_relay_key(fake_relay):
    good = crypto.gen_keypair()[1].hex()
    fake_relay.store["pub/dm1j1"] = {"pub": good}          # the relay offers a perfectly usable key
    pubs = rv.committee_pubs(RELAY_URL, ME, [{"miner_id": "dm1j1", "enc_pubkey": "00" * 32}], jury=["dm1j1"])
    assert pubs == {}, "the anchor is authoritative: the relay's key never stands in for it"


def test_a_juror_key_that_is_not_a_key_is_no_key_and_the_round_goes_on(monkeypatch, tmp_path, fake_relay, capsys):
    # A juror with no anchor leaves "zz" at the relay, another anchored the all-zero point. Sealing to
    # either raises; unguarded, the exception would end the round and the next job of this primary
    # would get nothing.
    w = World(fake_relay, n_miners=4)
    poison, low = "dm1poisonaaaaaaaaaaaaaaaaa", "dm1lowaaaaaaaaaaaaaaaaaaaa"
    w.registry += [{"miner_id": poison}, {"miner_id": low, "enc_pubkey": "00" * 32}]
    fake_relay.store[f"pub/{poison}"] = {"pub": "zz"}
    j2 = "job1791487835091"
    w.seed(j2, "p2", "a2")
    rows = [(JOB, "open+paid+optimistic+disputed", ME), (j2, "open+paid+optimistic+disputed", ME)]
    chains = {JOB: FakeChain((0, ok_json(w.ids()[:2] + [poison, low]), "")),
              j2: FakeChain((0, ok_json(w.ids()[2:4]), ""))}
    out = run_main_once(monkeypatch, tmp_path, w, rows, chains, capsys)
    assert "loop:" not in out and "FAILED" not in out, out
    assert fake_relay.reveals() == sorted([f"reveal/{JOB}__{m}__{ME}" for m in w.ids()[:2]]
                                          + [f"reveal/{j2}__{m}__{ME}" for m in w.ids()[2:4]])
    assert f"revealed {JOB} to 2 anchored juror(s)" in out and poison in out and low in out
    assert f"revealed {j2} to 2 anchored juror(s)" in out


def test_one_job_that_raises_does_not_starve_the_next(monkeypatch, tmp_path, fake_relay, capsys):
    w = World(fake_relay, n_miners=4)
    j2 = "job1791487835092"
    w.seed(j2)
    calls = {"n": 0}
    registry = w.registry

    def miners_raising_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("an unexpected failure inside the first job")
        return registry

    w.miners = miners_raising_once
    rows = [(JOB, "open+paid+optimistic+disputed", ME), (j2, "open+paid+optimistic+disputed", ME)]
    chains = {JOB: FakeChain((0, ok_json(w.ids()[:2]), "")), j2: FakeChain((0, ok_json(w.ids()[2:4]), ""))}
    out = run_main_once(monkeypatch, tmp_path, w, rows, chains, capsys)
    assert "loop:" not in out, out
    assert f"{JOB}: the reveal FAILED (RuntimeError" in out
    assert fake_relay.reveals() == sorted(f"reveal/{j2}__{m}__{ME}" for m in w.ids()[2:4])


# === (4) the loop itself: `main()` for one round ====================================================

def run_main_once(monkeypatch, tmp_path, world, rows, chain_by_job, capsys):
    (tmp_path / f"{ME}.sk").write_bytes(b"placeholder: load_sk is faked below")
    monkeypatch.setattr(rw, "NODE", "")
    monkeypatch.setattr(rw.crypto, "load_sk", lambda path, opener=None: world.sk)
    monkeypatch.setattr(rw, "_keyring", lambda: types.SimpleNamespace(open_with=None))
    monkeypatch.setattr(rw, "list_jobs", lambda: rows)
    monkeypatch.setattr(rw, "list_miners", world.miners)

    def chain(cmd, t=60):
        return chain_by_job[cmd[-1]](cmd, t)

    monkeypatch.setattr(rw, "run_rc", chain)
    monkeypatch.setattr(sys, "argv", ["reveal_worker.py", "--id", ME, "--relay", RELAY_URL,
                                      "--keydir", str(tmp_path), "--once", "--no-marker"])
    rw.main()
    return capsys.readouterr().out


def test_main_seals_one_job_to_its_jury_and_nothing_for_an_unreadable_one(monkeypatch, tmp_path, fake_relay, capsys):
    w = World(fake_relay, n_miners=6)
    other = "job1791487835080"
    rows = [(JOB, "open+paid+optimistic+disputed", ME),
            (other, "open+paid+optimistic+disputed", ME),
            ("job1791487835081", "open+paid+optimistic+disputed+resolved", ME),
            ("job1791487835082", "open+paid+optimistic+disputed", "dm1someoneelseaaaaaaaaaaa")]
    chains = {JOB: FakeChain((0, ok_json(w.ids()[:2]), "")),
              other: FakeChain((1, "", UNREACHABLE))}
    out = run_main_once(monkeypatch, tmp_path, w, rows, chains, capsys)
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in w.ids()[:2])
    assert f"revealed {JOB} to 2 anchored juror(s)" in out
    assert f"{other}: NOTHING sealed ({rv.WAIT_UNREADABLE})" in out
    assert "loop:" not in out, out


def test_main_with_every_jury_unreadable_seals_nothing_whatever_the_registry(monkeypatch, tmp_path, fake_relay, capsys):
    w = World(fake_relay, n_miners=12)
    rows = [(JOB, "open+paid+optimistic+disputed", ME)]
    out = run_main_once(monkeypatch, tmp_path, w, rows, {JOB: FakeChain((1, "", UNREACHABLE))}, capsys)
    assert fake_relay.reveals() == [] and w.miners_calls == 0
    assert "loop:" not in out, out


class _StopLoop(BaseException):
    """Ends `main()` after N rounds: a BaseException crosses the loop's `except Exception`."""


def test_main_backs_off_between_jury_reads_and_a_late_jury_is_still_served(monkeypatch, tmp_path, fake_relay, capsys):
    # Several rounds of the REAL loop, on a fake clock: the jury is unreadable at first, so it is read
    # again after poll, 2*poll... -- never on every round, never abandoned. Once the jury becomes
    # readable (a late seat), the reveal goes to it.
    w = World(fake_relay, n_miners=5)
    clock = {"t": 1000.0, "sleeps": 0}
    rounds_before_jury = 7                       # the jury is unreadable during rounds 0..6
    last_round = 16

    def fake_sleep(s):
        clock["sleeps"] += 1
        clock["t"] += s / 2.0                    # half a poll per round: the back-off must skip rounds
        if clock["sleeps"] > last_round:
            raise _StopLoop()

    monkeypatch.setattr(rw, "time", types.SimpleNamespace(sleep=fake_sleep, monotonic=lambda: clock["t"]))
    reads = []
    jury = w.ids()[:2]

    def chain(cmd, t=60):
        reads.append(clock["sleeps"])
        if clock["sleeps"] < rounds_before_jury:
            return (1, "", UNREACHABLE)
        return (0, ok_json(jury), "")

    (tmp_path / f"{ME}.sk").write_bytes(b"placeholder")
    monkeypatch.setattr(rw, "NODE", "")
    monkeypatch.setattr(rw.crypto, "load_sk", lambda path, opener=None: w.sk)
    monkeypatch.setattr(rw, "_keyring", lambda: types.SimpleNamespace(open_with=None))
    monkeypatch.setattr(rw, "list_jobs", lambda: [(JOB, "open+paid+optimistic+disputed", ME)])
    monkeypatch.setattr(rw, "list_miners", w.miners)
    monkeypatch.setattr(rw, "run_rc", chain)
    monkeypatch.setattr(sys, "argv", ["reveal_worker.py", "--id", ME, "--relay", RELAY_URL,
                                      "--keydir", str(tmp_path), "--no-marker", "--poll", "1"])
    with pytest.raises(_StopLoop):
        rw.main()
    out = capsys.readouterr().out
    # poll=1, half a poll per round: waits of 1, 2, 4, 8 polls put the reads at rounds 0, 2, 6, 14;
    # the read of round 14 finds the late jury, seals to it, and the job is done (no read after it).
    assert reads == [0, 2, 6, 14], f"expected back-off reads at rounds 0, 2, 6, 14, got {reads}"
    assert out.count("NOTHING sealed") == 1, "one line per change of outcome, not one per round"
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in jury), out
    assert f"revealed {JOB} to 2 anchored juror(s)" in out


# === (5) back-off ===================================================================================

def test_recheck_delay_starts_at_the_poll_doubles_and_is_capped():
    d = [rv.recheck_delay(k, 4.0) for k in range(8)]
    assert d[0] == 4.0 and d[1] == 8.0 and d[2] == 16.0
    assert all(a <= b for a, b in zip(d, d[1:])) and max(d) == rv.RECHECK_CAP_S
    assert rv.recheck_delay(10 ** 6, 4.0) == rv.RECHECK_CAP_S


# === (6) the SHIPPED runner, against a `dendrad` executable on a hermetic PATH =====================

posix_only = pytest.mark.skipif(os.name != "posix", reason="a shebang executable on a hermetic PATH")


def fake_dendrad(monkeypatch, tmp_path, body):
    """Puts a `dendrad` EXECUTABLE in a directory and makes that directory the WHOLE PATH: the shipped
    `run_rc` finds it and nothing from the machine. `body` is Python, run with the CLI's argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "dendrad"
    exe.write_text(f"#!{sys.executable}\nimport json, sys, time\n{body}\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return exe


@posix_only
def test_shipped_runner_keeps_the_streams_apart_end_to_end(monkeypatch, tmp_path, fake_relay):
    # The CLI prints a warning on stderr and the jury on stdout. Glued together, the JSON no longer
    # parses and the jury reads as UNKNOWN: the primary would seal nothing, on every audit.
    w = World(fake_relay, n_miners=4)
    jury = w.ids()[:2]
    fake_dendrad(monkeypatch, tmp_path, f"sys.stderr.write({SONIC_WARNING!r})\n"
                                        f"sys.stdout.write({ok_json(jury)!r})\n")
    o = rw.reveal_disputed_job(ME, RELAY_URL, w.sk, JOB, node_args=[], miners_fn=w.miners)
    assert o.action == rw.REVEALED and set(o.targets) == set(jury), o


@posix_only
def test_shipped_runner_cuts_a_hung_dendrad_at_the_jury_timeout(monkeypatch, tmp_path):
    fake_dendrad(monkeypatch, tmp_path, "time.sleep(20)")
    start = _time.monotonic()
    r = rv.read_jury(JOB, [], rw.run_rc, t=1)
    assert r.state == rv.JURY_UNKNOWN and "Timeout" in r.detail
    assert _time.monotonic() - start < 10, "the timeout of read_jury did not reach the process"


@posix_only
def test_shipped_runner_on_a_missing_dendrad_is_unknown_never_notfound(monkeypatch, tmp_path, fake_relay):
    w = World(fake_relay, n_miners=4)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    rc, out, err = rw.run_rc(["dendrad", "version"])
    assert rc is None and out == "" and "FileNotFoundError" in err
    o = rw.reveal_disputed_job(ME, RELAY_URL, w.sk, JOB, node_args=[], miners_fn=w.miners)
    assert o.action == rv.WAIT_UNREADABLE and "did not run" in o.detail, o
    assert fake_relay.reveals() == [] and fake_relay.gets == [] and w.miners_calls == 0


@posix_only
def test_shipped_runner_names_a_dendrad_without_the_query(monkeypatch, tmp_path, fake_relay):
    w = World(fake_relay, n_miners=4)
    fake_dendrad(monkeypatch, tmp_path, f"sys.stderr.write({OLD_CLI!r})\nsys.exit(1)")
    o = rw.reveal_disputed_job(ME, RELAY_URL, w.sk, JOB, node_args=[], miners_fn=w.miners)
    assert o.action == rv.WAIT_UNREADABLE and "does not know `query jobs audit-committee`" in o.detail, o
    assert fake_relay.reveals() == [] and fake_relay.gets == []


# === (7) the loop over several rounds: what is retried, what is read once, what is named once =======

def run_main_rounds(monkeypatch, tmp_path, world, rows_fn, chain, rounds, capsys):
    """`main()` for `rounds` rounds on a fake clock that advances by the whole poll (1 s) per round.
    `rows_fn(round)` is the list-job answer, `chain(cmd, round)` answers the jury queries."""
    clock = {"t": 1000.0, "round": 0}

    def fake_sleep(s):
        clock["round"] += 1
        clock["t"] += s
        if clock["round"] >= rounds:
            raise _StopLoop()

    monkeypatch.setattr(rw, "time", types.SimpleNamespace(sleep=fake_sleep, monotonic=lambda: clock["t"]))
    (tmp_path / f"{ME}.sk").write_bytes(b"placeholder: load_sk is faked below")
    monkeypatch.setattr(rw, "NODE", "")
    monkeypatch.setattr(rw.crypto, "load_sk", lambda path, opener=None: world.sk)
    monkeypatch.setattr(rw, "_keyring", lambda: types.SimpleNamespace(open_with=None))
    monkeypatch.setattr(rw, "list_jobs", lambda: rows_fn(clock["round"]))
    monkeypatch.setattr(rw, "list_miners", lambda: world.miners())
    monkeypatch.setattr(rw, "run_rc", lambda cmd, t=60: chain(cmd, clock["round"]))
    monkeypatch.setattr(sys, "argv", ["reveal_worker.py", "--id", ME, "--relay", RELAY_URL,
                                      "--keydir", str(tmp_path), "--no-marker", "--poll", "1"])
    with pytest.raises(_StopLoop):
        rw.main()
    out = capsys.readouterr().out
    assert "loop:" not in out, out
    return out


OPEN = "open+paid+optimistic+disputed"


def test_loop_keeps_an_incomplete_job_until_every_juror_holds_its_copy(monkeypatch, tmp_path, fake_relay, capsys):
    w = World(fake_relay, n_miners=4)
    jury = w.ids()[:3]
    real = fake_relay.put_status
    rnd = {"n": 0}

    def refuses_the_third_in_round_0(base, kind, key, obj, **kw):
        return "refused" if (rnd["n"] == 0 and jury[2] in key) else real(base, kind, key, obj, **kw)

    monkeypatch.setattr(relay, "put_status", refuses_the_third_in_round_0)
    reads = []

    def rows(r):
        rnd["n"] = r
        return [(JOB, OPEN, ME)]

    def chain(cmd, r):
        reads.append(r)
        return 0, ok_json(jury), ""

    out = run_main_rounds(monkeypatch, tmp_path, w, rows, chain, 4, capsys)
    assert f"reveal INCOMPLETE for {JOB}" in out and f"revealed {JOB} to 3 anchored juror(s)" in out, out
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in jury)
    assert reads == [0], f"the jury was read again for a job whose jury is anchored: {reads}"


def test_loop_waits_for_a_late_request_reads_the_jury_once_and_names_the_wait_once(monkeypatch, tmp_path,
                                                                                   fake_relay, capsys):
    w = World(fake_relay, n_miners=4)
    jury = w.ids()[:2]
    req_key = f"req/{JOB}__{ME}"
    saved = fake_relay.store.pop(req_key)
    reads = []

    def rows(r):
        if r >= 5:
            fake_relay.store[req_key] = saved          # the envelope reaches the relay late
        return [(JOB, OPEN, ME)]

    def chain(cmd, r):
        reads.append(r)
        return 0, ok_json(jury), ""

    out = run_main_rounds(monkeypatch, tmp_path, w, rows, chain, 8, capsys)
    assert out.count("CANNOT be revealed now") == 1, out
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in jury), out
    assert reads == [0], f"one jury read per job, not one per round: {reads}"


def test_loop_serves_an_audit_jury_seated_after_a_human_dispute(monkeypatch, tmp_path, fake_relay, capsys):
    # A human dispute anchors a re-adjudication jury and no audit jury; the sampling lottery can still
    # seat an audit jury later. A primary that retired the job at the first look would be silent then.
    w = World(fake_relay, n_miners=5)
    jury, redo_jury = w.ids()[:2], w.ids()[2:4]

    def chain(cmd, r):
        if "--redo" in cmd:
            return 0, ok_json(redo_jury), ""
        return (1, "", NOT_ANCHORED) if r < 4 else (0, ok_json(jury), "")

    out = run_main_rounds(monkeypatch, tmp_path, w, lambda r: [(JOB, OPEN, ME)], chain, 12, capsys)
    assert f"NOTHING sealed ({rv.NO_REVEAL_HUMAN_DISPUTE})" in out
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in jury), out


def test_loop_serves_a_juror_that_anchors_its_key_late(monkeypatch, tmp_path, fake_relay, capsys):
    w = World(fake_relay, n_miners=2)
    late = {f"dm1late{i}aaaaaaaaaaaaaaaaaa": crypto.gen_keypair() for i in range(2)}
    base = list(w.registry)
    rnd = {"n": 0}

    def rows(r):
        rnd["n"] = r
        return [(JOB, OPEN, ME)]

    def miners():
        keyed = rnd["n"] >= 3
        return base + [{"miner_id": m, **({"enc_pubkey": pk.hex()} if keyed else {})} for m, (sk, pk) in late.items()]

    w.miners = miners
    out = run_main_rounds(monkeypatch, tmp_path, w, rows, lambda cmd, r: (0, ok_json(list(late)), ""), 8, capsys)
    assert f"NOTHING sealed ({rw.NO_KEY})" in out
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in late), out
    for m, (sk, _) in late.items():
        assert rv.open_reveal(RELAY_URL, JOB, m, sk, ME)["prompt"] == PROMPT


def test_loop_drops_the_kept_jury_of_a_job_that_left_the_list(monkeypatch, tmp_path, fake_relay, capsys):
    # The only window the purge has: a job that leaves the open set and is listed again is read afresh.
    w = World(fake_relay, n_miners=5)
    first, second = w.ids()[:2], w.ids()[2:4]
    req_key = f"req/{JOB}__{ME}"
    saved = fake_relay.store.pop(req_key)

    def rows(r):
        if r == 1:
            return []                                  # the job is not in the open set this round
        if r >= 2:
            fake_relay.store[req_key] = saved
        return [(JOB, OPEN, ME)]

    def chain(cmd, r):
        return 0, ok_json(first if r == 0 else second), ""

    run_main_rounds(monkeypatch, tmp_path, w, rows, chain, 4, capsys)
    assert fake_relay.reveals() == sorted(f"reveal/{JOB}__{m}__{ME}" for m in second)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
