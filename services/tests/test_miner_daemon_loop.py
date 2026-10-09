# -*- coding: utf-8 -*-
"""The miner daemon's LOOP, driven through main() for many passes on a clock the test moves.

WHAT IS PINNED, EACH HALF WITH ITS COUNTERPART:
  * A FAILING REQUEST IS REMEMBERED (JOB_FAILURES, a file of its own, never the commitment journal): a request whose
    inference always fails costs job_max_attempts() generations -- not one per pass -- when the engine answers a
    test request after each failure; while the engine itself is silent, nothing is given up, and the retries still
    wait their pauses. A refused deposit keeps its sealed response and replays THAT deposit, never the inference.
    A refused resumption waits its pause instead of a transaction per pass.
  * THE NEWEST REQUESTS FIRST, AND A STALE ONE IS NOT SERVED: the age is read in `job<milliseconds>`, an illegible
    age is served, and the filter never touches a journalled commitment. A request this machine's clock calls stale
    is judged again by the chain's clock (the latest block's time), and the younger age decides: a machine clock
    running ahead no longer silences a present miner; with no block time read, this machine's clock decides alone.
  * THE AVAILABILITY PROOF FIRST: at start before the first request, between two requests when a new challenge is
    open, on a clock rather than one pass in four -- and NEVER without a test inference that answered AND was
    embedded (an engine that cannot embed commits nothing: it neither proves presence nor gives a request up).
    A registry that could not be read at start does not silence a registered miner; a miner KNOWN not registered
    proves nothing.
  * DRAINING (a file `drain` next to the keys): no proof and no new request, while what was committed is finished.
  * READERS WITHOUT A DEFAULT: min_stake, a balance and the anchored VRF key, unread, are None -- never 50 000, 0 or
    "no key".
  * THE MODEL A COMMITMENT WAS COMPUTED WITH travels in the journal, and a resumption anchors with it; the served
    model is found in /api/tags with its tag; an empty weights_hash is read again.
  * THE ENCRYPTION KEY anchored on chain is confronted with this node's: a mismatch stops the proofs and is said,
    and nothing is ever rotated here. It is confronted again at a late registration and at the half-hourly reading,
    in the same process: a mismatch fixed on chain resumes the proofs.
  * OWNER MODE: a reading that failed (registry, min_stake) puts nothing off by the reprint delay.
  * THE FAILURES FILE is pruned only after a listing that was READ, and a success clears the failure it follows.
  * A DRAINING miner registers nothing, at start (a restart after its exit) or replayed; one the chain still
    records stays registered; the drain removed, the registration is attempted at the next pass.
  * THE WORK QUEUE is read as this miner's slice (`/list?suffix=`), never the whole network's.
  * THE CLIENT'S DECLARED WAIT judges an answer late or on time; without one, no verdict.
  * THE ENGINE'S VERSION is in the heartbeat.

The chain, the relay, the engine and the keys are replaced; the loop, its readers and its decisions are the shipped
ones. The subject can be a MUTATED copy (DENDRA_DAEMON_FILE), so a harness shows each case turn red when the
property it pins is removed.

WHAT THIS BENCH DOES NOT COVER, SAID HERE RATHER THAN HIDDEN BEHIND A NAME: a real relay's refusal codes, a real
Ollama (its /api/version and /api/tags are served by a stand-in that answers the documented shape; its embedder is
a stand-in that answers or raises), a real chain's inclusion delays and a real `dendrad status` (a document of the
documented shape), and a CLIENT clock behind the real time (its requests carry the time it wrote; a client clock
ahead gives a negative age, which is served).
"""
import http.server
import importlib.util
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

if os.environ.get("DENDRA_DAEMON_FILE"):
    _spec = importlib.util.spec_from_file_location("miner", os.environ["DENDRA_DAEMON_FILE"])
    md = importlib.util.module_from_spec(_spec)
    sys.modules["miner"] = md
    _spec.loader.exec_module(md)
else:
    import miner as md  # noqa: E402

from modea import crypto  # noqa: E402

ADDR = "dendra1bench"
MID = "dm1bench"
SUFFIX = "__" + MID
T0 = 1_800_000_000.0
NL = chr(10)
TXH = "AB" * 32
OK_BCAST = "code: 0" + NL + "txhash: " + TXH + NL
INCLUDED = NL.join(["code: 0", 'height: "42"', "txhash: " + TXH])
REFUSED = NL.join(["code: 7", "txhash: " + TXH, "raw_log: unauthorized"])
VSK, VPK = "a" * 128, "b" * 64
CHAL = "c" * 64


class Stop(Exception):
    pass


def jid_at(t):
    """A request identifier made at the time t, the way client.submit_job makes one."""
    return "job%d" % int(t * 1000)


def iso_utc(t):
    """The time t as CometBFT writes `latest_block_time`: RFC 3339, nanoseconds, `Z`."""
    whole = int(t // 1)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + ".%09dZ" % int(round((t - whole) * 1e9))


def fresh_chain_clock():
    """The daemon's chain clock as a process starts it: nothing read, nothing said."""
    return {"block_time": None, "mono_at": 0.0, "tried_at": None, "said": False, "unread_said": False}


class Engine:
    """The served model behind presence_probe: answers while `up`, answers empty while `silent`, else raises."""

    def __init__(self, world):
        self.w = world
        self.endpoint = ""

    def generate(self, prompt, max_out=0, temperature=None, timeout_s=None):
        self.w.events.append(("probe", self.w.now))
        if self.w.engine == "up":
            return "ready"
        if self.w.engine == "silent":
            return ""
        raise ConnectionError("engine down")


class Res:
    def __init__(self, n):
        self.content_embed = "0.5,-0.25,%d" % n
        self.prompt_commit = "0.75,%d" % n
        self.in_tok, self.out_tok = 3, 5
        self.sealed_result = types.SimpleNamespace(nonce=bytes([n % 256]) * 24, ct=bytes([7]) * 8)


class Miner:
    def __init__(self, world):
        self.w = world
        self.backend = Engine(world)

    def handle_job(self, jid, eph, sealed, max_out=0):
        w = self.w
        w.generations.append((jid, w.now))
        w.events.append(("infer", jid, w.now))
        w.now += w.gen_s
        if w.on_infer:
            w.on_infer(jid)
        if w.infer_fails(jid):
            raise RuntimeError("generation failed")
        if w.embedder != "up":           # modea/miner.py::Miner._embed_output raises: no commitment can be made
            raise RuntimeError("no embedding")
        return Res(len(w.generations))


class World:
    """The relay, the chain, the engine and the keys around ONE miner, on a clock the test moves."""

    def __init__(self, tmp_path, monkeypatch, reqs=(), poll=30.0, vsk=False, challenge="", engine="up",
                 enc_anchored=None):
        self.mp = monkeypatch
        self.now = T0
        self.poll = float(poll)
        self.passes, self.max_passes = 0, 1
        self.keydir = tmp_path / "keys"
        self.keydir.mkdir(parents=True, exist_ok=True)
        self.status = tmp_path / "status.json"
        sk, pub = crypto.gen_keypair()
        crypto.save_sk(sk, str(self.keydir / (MID + ".sk")), "")
        self.mypub = pub.hex()
        self.enc_anchored = self.mypub if enc_anchored is None else enc_anchored
        self.reqs, self.order, self.res = {}, [], []
        self.deposits, self.refuse_res = [], 0
        self.commits, self.commit_calls, self.anchor_refuse = {}, [], 0
        self.proofs, self.chal_reads, self.challenge = [], 0, challenge
        self.events, self.generations = [], []
        self.gen_s, self.on_infer, self.on_sleep = 1.0, None, None
        self.infer_fails = lambda jid: False
        self.engine = engine
        self.embedder = "up"              # the embedding a request needs: "up", or anything else for down
        self.vrf = (VSK, VPK) if vsk else ("", "")
        self.whash = ""
        self.backend_name = "mock"
        self.miner = Miner(self)
        self.reg = md.PRESENT             # the registry: PRESENT, ABSENT or UNREAD (`reg_at`, a callable, wins)
        self.reg_at = None
        self.faucet_calls, self.creates, self.create_registers = [], 0, False
        self.listing_fails = False        # the relay's listing answers its failure value ({})
        self.listing_suffixes = []        # the `suffix` of every listing the loop asked for
        self.chain_offset = None          # the latest block's time minus this clock; None = `dendrad status` unread
        self.status_reads = 0
        for j in reqs:
            self.add(j)
        self._patch()

    def add(self, jid, **extra):
        key = jid + SUFFIX
        body = {"client_eph_pk": "00" * 32, "nonce": "00" * 24, "ct": "00" * 8, "max_out": 0}
        body.update(extra)
        self.reqs[key] = body
        if key not in self.order:
            self.order.append(key)
        return key

    def _patch(self):
        mp = self.mp
        mp.setenv("DENDRA_CONFINE", "0")
        mp.setenv("DENDRA_STATUS_FILE", str(self.status))
        for v in ("DENDRA_JOB_MAX_ATTEMPTS", "DENDRA_REQUEST_MAX_AGE_S", "DENDRA_SLOT", "DENDRA_PRESENCE_PROBE_S"):
            mp.delenv(v, raising=False)
        md._STATUS.clear()
        mp.setattr(md, "_STATUS_WARNED", False)
        mp.setattr(md, "_COMMITS", {"commits_anchored": 0, "commits_refused": 0})
        mp.setattr(md, "_COMMITS_SENT", set())
        mp.setattr(md, "_JOB_COUNTS", {k: 0 for k in md._JOB_COUNTS})
        mp.setattr(md, "_ENGINE", {"answered_at": 0.0, "unanswered_at": 0.0})
        mp.setattr(md, "_PROBE_STATE", {"failed_at": 0.0, "said_for": "", "bad_bound_said": False})
        mp.setattr(md, "_ENV_SAID", set())
        mp.setattr(md, "_CHAIN_CLOCK", fresh_chain_clock(), raising=False)
        mp.setattr(md, "answer_embedding", self.embed, raising=False)
        mp.setattr(md, "faucet_fund_classified", self.faucet)
        mp.setattr(md, "PAYOUT_ADDRESS", "")
        mp.setattr(md, "MODEL_ID", "")
        mp.setattr(md, "time", types.SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.now,
                                                     sleep=self.sleep))
        mp.setattr(md, "keys_addr", lambda name, keydir=None: ADDR)
        mp.setattr(md, "align_identity", lambda name, addr, owner="": MID)
        mp.setattr(md, "decide_owner", lambda mid, addr, configured=None, read=None: "")
        mp.setattr(md.relay_signature, "address_from_key", lambda *a, **k: ADDR)
        mp.setattr(md.relay, "set_sign_key", lambda name: None)
        mp.setattr(md.relay, "listing", self.listing)
        mp.setattr(md.relay, "get", self.get)
        mp.setattr(md.relay, "put", self.put)
        mp.setattr(md, "model_weights_hash", lambda: self.whash)
        mp.setattr(md, "vrf_identity", lambda keydir, mid: self.vrf)
        mp.setattr(md, "miner_operator", lambda mid: ADDR)
        mp.setattr(md, "registry_record", self.record)
        mp.setattr(md, "pick_backend", lambda want: self.backend_name)
        mp.setattr(md, "Miner", lambda *a, **k: self.miner)
        mp.setattr(md, "query", self.query)
        mp.setattr(md, "run", self.run)
        mp.setattr(md, "tx_from", self.tx_from)
        mp.setattr(md, "subprocess", types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(stdout="ab" * 80)))

    # -- the embedder and the faucet ----------------------------------------------------------------------
    def embed(self, text, backend=None):
        self.events.append(("embed", self.now))
        if self.embedder != "up":
            raise RuntimeError("DENDRA_EMBED_MODE=backend but the backend provides no embedding")
        return "0.1,0.2"

    def faucet(self, url, addr):
        self.faucet_calls.append(self.now)
        return False, "rate limited", "ip_quota"

    # -- the relay ----------------------------------------------------------------------------------------
    def listing(self, base, suffix=None):
        """relay_client.listing: the whole queue, or the keys ending with `suffix` (every kind named, even empty)."""
        self.listing_suffixes.append(suffix)
        if self.listing_fails:
            return {}
        doc = {"req": list(self.order), "res": list(self.res), "pub": []}
        if suffix:
            doc = {k: [x for x in v if x.endswith(suffix)] for k, v in doc.items()}
        return doc

    def get(self, base, kind, key, retries=1):
        return dict(self.reqs[key]) if kind == "req" and key in self.reqs else None

    def put(self, base, kind, key, obj, **kw):
        if kind != "res":
            return True
        self.deposits.append((key, json.dumps(obj, sort_keys=True), self.now))
        if self.refuse_res > 0:
            self.refuse_res -= 1
            self.events.append(("deposit-refused", key, self.now))
            return False
        if key not in self.res:
            self.res.append(key)
        self.events.append(("deposit", key, self.now))
        return True

    # -- the chain ----------------------------------------------------------------------------------------
    def record(self, mid):
        state = self.reg_at(self.now) if self.reg_at else self.reg
        if state == md.UNREAD:
            return md.UNREAD, {"why": "connection refused"}
        if state == md.ABSENT:
            return md.ABSENT, {}
        return md.PRESENT, {"creator": ADDR, "operator": ADDR, "region": "eu", "stake": 1_000_000,
                            "enc_pubkey": self.enc_anchored, "vrf_pubkey": self.vrf[1]}

    def query(self, sub, *pos, flags=()):
        if sub == "get-avail-challenge":
            self.chal_reads += 1
            return json.dumps({"challenge": self.challenge, "epoch": "3"})
        if sub == "params":
            return json.dumps({"params": {"min_stake": "1000000"}})
        if sub == "get-commit":
            return ("commit: " + self.commits[pos[0]]) if pos[0] in self.commits else "commit: null"
        if sub == "get-miner":
            return json.dumps({"miner": {}})
        return ""

    def run(self, c, t=None, stdin=""):
        if list(c[:3]) == ["dendrad", "query", "tx"]:
            return INCLUDED
        if list(c[:2]) == ["dendrad", "status"]:
            self.status_reads += 1
            if self.chain_offset is None:
                return "Error: post failed: connection refused"
            return json.dumps({"node_info": {}, "sync_info": {
                "latest_block_height": "42", "latest_block_time": iso_utc(self.now + self.chain_offset),
                "catching_up": False}})
        return ""

    def tx_from(self, frm, sub, *pos, flags=()):
        if sub == "create-miner":
            self.creates += 1
            if self.create_registers:
                self.reg = md.PRESENT
                return OK_BCAST
            return ""
        if sub == "create-commit":
            self.commit_calls.append((pos[0], pos, list(flags), self.now))
            self.events.append(("commit", pos[0], self.now))
            if self.anchor_refuse > 0:
                self.anchor_refuse -= 1
                return REFUSED
            self.commits[pos[0]] = pos[2]
            return OK_BCAST
        if sub == "prove-availability":
            self.proofs.append((pos[1], self.now))
            self.events.append(("prove", pos[1], self.now))
            return OK_BCAST
        return ""

    # -- the clock ----------------------------------------------------------------------------------------
    def sleep(self, s):
        self.now += s
        if s == self.poll:
            self.passes += 1
            if self.on_sleep:
                self.on_sleep(self.passes)
            if self.passes >= self.max_passes:
                raise Stop()

    def go(self, passes=1, once=False):
        argv = ["miner.py", "--id", MID, "--relay", "http://relay.invalid", "--keydir", str(self.keydir),
                "--poll", str(self.poll)]
        if once:
            argv.append("--once")
        self.mp.setattr(sys, "argv", argv)
        self.passes, self.max_passes = 0, passes
        try:
            md.main()
        except Stop:
            pass
        return self

    def hb(self):
        return json.loads(self.status.read_text(encoding="utf-8"))

    def failures(self):
        p = self.keydir / md.JOB_FAILURES
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]


# ══ A FAILING REQUEST IS REMEMBERED ═══════════════════════════════════════════════════════════════════════
def test_an_inference_that_always_fails_costs_max_attempts_generations(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    w.infer_fails = lambda jid: True
    w.go(passes=200)                      # 200 passes of 30 s: well past every pause
    gens = [t for _, t in w.generations]
    assert len(gens) == md.job_max_attempts() == 3, (
        "one generation per FAILURE COUNTED, never one per pass: %d over %d passes" % (len(gens), w.passes))
    assert gens[1] - gens[0] >= md.JOB_RETRY_PAUSES_S[0] and gens[2] - gens[1] >= md.JOB_RETRY_PAUSES_S[1]
    assert len(w.kinds("probe")) == 3, "the engine is asked a test request after each failure"
    hb = w.hb()
    assert (hb["inference_failed"], hb["abandoned"], hb["deposit_refused"]) == (3, 1, 0)
    entry = w.failures()["jobalpha" + SUFFIX]
    assert entry["abandoned"] is True and entry["counted"] == 3 and entry["kind"] == "inference"
    out = capsys.readouterr().out
    assert out.count("ABANDONED after 3 failed inferences") == 1
    assert md._read_journal(w.keydir) == {}, "a failure is never written into the commitment journal"


def test_DENDRA_JOB_MAX_ATTEMPTS_sets_the_count(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    monkeypatch.setenv("DENDRA_JOB_MAX_ATTEMPTS", "2")
    w.infer_fails = lambda jid: True
    w.go(passes=200)
    assert len(w.generations) == 2 and w.hb()["abandoned"] == 1


def test_a_failure_while_the_engine_is_silent_never_gives_the_request_up(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"], engine="down")
    w.infer_fails = lambda jid: True
    w.go(passes=200)                      # 6 000 s
    n = len(w.generations)
    assert w.hb()["abandoned"] == 0 and not w.failures()["jobalpha" + SUFFIX].get("abandoned"), (
        "a failure the engine did not answer around is the engine's: it gives nothing up")
    assert 4 <= n <= 7, "still retried, at the pauses (60, 300, 1800, 1800 s ...): %d generations" % n
    assert n < w.passes / 10, "never one generation per pass"
    assert "so this failure does not count against the request" in capsys.readouterr().out


def test_the_pause_is_read_from_the_file_by_the_next_process(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    w.infer_fails = lambda jid: True
    w.go(once=True)
    assert len(w.generations) == 1
    w.now += 10                           # a restart ten seconds later: the pause is not over
    w.go(once=True)
    assert len(w.generations) == 1, "the file kept the pause across the restart"
    w.now += md.JOB_RETRY_PAUSES_S[0]
    w.go(once=True)
    assert len(w.generations) == 2


def test_a_refused_deposit_replays_the_same_sealed_response_never_the_inference(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, reqs=["jobbeta"])
    w.refuse_res = 1
    w.go(passes=10)
    key = "jobbeta" + SUFFIX
    assert len(w.generations) == 1, "the inference ran ONCE"
    assert len(w.deposits) == 2 and w.deposits[0][1] == w.deposits[1][1], "the SAME sealed response, twice"
    assert w.deposits[1][2] - w.deposits[0][2] >= md.JOB_RETRY_PAUSES_S[0], "the replay waited its pause"
    assert [c[1][2] for c in w.commit_calls] == [Res(1).content_embed], (
        "the commitment journalled with that response is the one anchored")
    hb = w.hb()
    assert hb["deposit_refused"] == 1 and hb["commits_anchored"] == 1 and hb["inference_failed"] == 0
    assert key not in w.failures() and md._read_journal(w.keydir) == {}
    assert "KEPT after a refused deposit is now deposited" in capsys.readouterr().out


def test_the_journal_carries_no_retry_schedule(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobbeta"])
    w.refuse_res = 99
    w.go(once=True)
    key = "jobbeta" + SUFFIX
    assert set(md._pending_commitment(w.keydir, key)) <= {"commit", "pcommit", "ts", "model_id", "weights_hash"}
    entry = w.failures()[key]
    assert entry["kind"] == "deposit" and entry["sealed"]["ct"] == (bytes([7]) * 8).hex()


def test_a_refused_resumption_waits_its_pause(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, poll=3.0)
    key = w.add("jobeps")
    md._journal_commitment(w.keydir, key, "0.1,0.2", "0.3,0.4")
    w.res.append(key)
    w.anchor_refuse = 99
    w.go(passes=30)                       # 90 s
    assert len(w.commit_calls) == 2, "one attempt, then one after the first pause: %r" % (
        [round(c[3] - T0) for c in w.commit_calls],)
    assert md._pending_commitment(w.keydir, key)["commit"] == "0.1,0.2", "the commitment stays journalled"


def test_an_entry_for_a_request_the_relay_dropped_is_pruned(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    w.infer_fails = lambda jid: True
    w.go(once=True)
    assert "jobalpha" + SUFFIX in w.failures()
    w.order.clear()
    w.reqs.clear()
    w.go(once=True)
    assert w.failures() == {}


def test_an_unreadable_failures_file_retries_as_before(tmp_path, monkeypatch, capsys):
    (tmp_path / md.JOB_FAILURES).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(md, "_FAILURES_WARNED", {"read": False, "write": False})
    assert md._read_failures(tmp_path) == {} and md._read_failures(tmp_path) == {}
    assert capsys.readouterr().out.count("could not be read") == 1


def test_the_pauses_and_the_abandon_rule():
    f = {}
    e = md.note_job_failure(f, "k", "inference", "X", 1000.0, counted=True)
    assert (e["failures"], e["counted"], e["next_at"]) == (1, 1, 1060.0) and not e.get("abandoned")
    e = md.note_job_failure(f, "k", "inference", "X", 1100.0, counted=False)
    assert (e["failures"], e["counted"], e["next_at"]) == (2, 1, 1400.0)
    assert md.job_due(e, 1399.0) is False and md.job_due(e, 1400.0) is True
    md.note_job_failure(f, "k", "inference", "X", 1500.0, counted=True)
    e = md.note_job_failure(f, "k", "inference", "X", 3400.0, counted=True)
    assert e["abandoned"] is True and md.job_due(e, 10 ** 12) is False
    e = md.note_job_failure(f, "k", "deposit", "refused", 4000.0, sealed={"ct": "00"})
    assert (e["kind"], e["failures"], e["counted"]) == ("deposit", 1, 0) and not e.get("abandoned")
    for _ in range(9):
        e = md.note_job_failure(f, "k", "deposit", "refused", 5000.0)
    assert not e.get("abandoned") and e["next_at"] == 5000.0 + md.JOB_RETRY_PAUSES_S[-1], (
        "a refused deposit is never given up: its commitment is journalled")


# ══ THE NEWEST FIRST, AND A STALE REQUEST IS NOT SERVED ════════════════════════════════════════════════════
def test_the_keys_are_walked_newest_first_with_an_illegible_age_last():
    keys = [jid_at(T0 - 100) + SUFFIX, "jobzeta" + SUFFIX, jid_at(T0 - 5) + SUFFIX, "other__dm1else",
            jid_at(T0 - 50) + SUFFIX, "jobeta" + SUFFIX]
    assert md.newest_first(keys, SUFFIX) == [jid_at(T0 - 5) + SUFFIX, jid_at(T0 - 50) + SUFFIX,
                                             jid_at(T0 - 100) + SUFFIX, "jobeta" + SUFFIX, "jobzeta" + SUFFIX]


def test_the_age_is_read_only_from_job_and_thirteen_digits():
    assert md.request_age_s(jid_at(T0 - 700), T0) == pytest.approx(700, abs=0.01)
    for other in ("job12345", "jobalpha", "job" + "1" * 14, "x" + jid_at(T0)):
        assert md.request_age_s(other, T0) is None, other
    assert md.request_age_s(jid_at(T0 + 30), T0) < 0, "a client clock ahead is read as it is: not stale"


def test_a_stale_request_is_not_served_and_is_counted_once(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, reqs=[jid_at(T0 - 700)], poll=3.0)   # `dendrad status` does not answer
    w.go(passes=5)
    assert w.generations == [] and w.hb()["requests_stale"] == 1, "no chain clock read: this machine's decides"
    out = capsys.readouterr().out
    assert out.count("NOT served, made") == 1
    assert out.count("the chain's clock could not be read") == 1, "the missing second opinion is said, once"


def test_the_newer_request_is_served_first(tmp_path, monkeypatch):
    old, new = jid_at(T0 - 100), jid_at(T0 - 50)
    w = World(tmp_path, monkeypatch, reqs=[old, new])
    w.go(once=True)
    assert [j for j, _ in w.generations] == [new, old]


def test_an_illegible_age_is_served(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobgamma", jid_at(T0 - 700)])
    w.go(once=True)
    assert [j for j, _ in w.generations] == ["jobgamma"]


def test_a_journalled_commitment_is_anchored_however_old(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    key = w.add(jid_at(T0 - 90_000))
    md._journal_commitment(w.keydir, key, "0.1,0.2", "0.3,0.4")
    w.res.append(key)
    w.go(once=True)
    assert [c[0] for c in w.commit_calls] == [key] and w.hb()["requests_stale"] == 0


def test_the_age_filter_never_touches_a_journalled_request_whatever_its_path(tmp_path, monkeypatch):
    """Two journalled requests made long ago, neither of whose responses reached the relay: one whose refused
    deposit kept its sealed response (replayed), one whose kept response was lost with its file (served again, as
    before the file existed). Neither is dropped as stale: the filter is for NEW requests only."""
    w = World(tmp_path, monkeypatch)
    kept = w.add(jid_at(T0 - 90_000))
    lost = w.add(jid_at(T0 - 80_000))
    md._journal_commitment(w.keydir, kept, "0.1,0.2", "0.3,0.4")
    md._journal_commitment(w.keydir, lost, "0.5,0.6", "0.7,0.8")
    md._write_failures(w.keydir, {kept: {"kind": "deposit", "failures": 1, "counted": 0, "next_at": 0,
                                         "sealed": {"nonce": "00", "ct": "11", "in_tok": 1, "out_tok": 1}}})
    w.go(once=True)
    sealed = json.dumps({"nonce": "00", "ct": "11", "in_tok": 1, "out_tok": 1}, sort_keys=True)
    assert [d[1] for d in w.deposits if d[0] == kept] == [sealed], "the kept response is deposited, as kept"
    assert [j for j, _ in w.generations] == [lost[: -len(SUFFIX)]], "the lost one is served again, never dropped"
    assert w.hb()["requests_stale"] == 0


def test_DENDRA_REQUEST_MAX_AGE_S_sets_the_age_and_a_bad_value_is_said(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, reqs=[jid_at(T0 - 700)])
    monkeypatch.setenv("DENDRA_REQUEST_MAX_AGE_S", "1000")
    w.go(once=True)
    assert len(w.generations) == 1
    w2 = World(tmp_path / "b", monkeypatch, reqs=[jid_at(T0 - 700)])
    monkeypatch.setenv("DENDRA_REQUEST_MAX_AGE_S", "ten minutes")
    w2.go(passes=3)
    assert w2.generations == [], "a value that is not a number falls back to the default, not to 'no limit'"
    assert capsys.readouterr().out.count("DENDRA_REQUEST_MAX_AGE_S='ten minutes' is not a positive number") == 1


# ══ THE AVAILABILITY PROOF FIRST, AND NEVER WITHOUT A PROBE ════════════════════════════════════════════════
def _order(w):
    return [e[0] for e in w.events if e[0] in ("prove", "infer")]


def test_at_start_the_proof_comes_before_the_first_request(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"], vsk=True, challenge=CHAL)
    w.go(once=True)
    assert _order(w) == ["prove", "infer"]
    assert w.hb()["presence"]["result"] == "proven"


def test_a_new_challenge_between_two_requests_is_proven_before_the_next(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=[jid_at(T0 - 60), jid_at(T0 - 30)], vsk=True, challenge="")
    w.gen_s = md.AVAIL_CHECK_S + 10

    def open_window(jid):
        w.challenge = CHAL
    w.on_infer = open_window
    w.go(once=True)
    assert _order(w) == ["infer", "prove", "infer"], "the new window is proven BEFORE the next request"


def test_a_window_opening_while_the_queue_is_empty_is_proven_at_the_head_of_a_pass(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, poll=3.0, vsk=True, challenge="")
    opened = {}

    def open_window(n):
        if n == 2:
            w.challenge, opened["at"] = CHAL, w.now
    w.on_sleep = open_window
    w.go(passes=20)
    assert [c for c, _ in w.proofs] == [CHAL]
    assert w.proofs[0][1] - opened["at"] <= md.AVAIL_CHECK_S + w.poll, "on the clock, with no request to wait for"


def test_no_proof_without_a_test_inference_that_answered(tmp_path, monkeypatch):
    for state in ("silent", "down"):
        w = World(tmp_path / state, monkeypatch, vsk=True, challenge=CHAL, engine=state)
        w.go(passes=20)
        assert w.proofs == [], "%s: a model that does not answer proves no presence" % state
        assert w.kinds("probe"), "%s: the test inference was asked" % state
        assert w.hb()["presence"]["result"] == "model did not answer"


def test_the_challenge_is_read_on_a_clock_not_every_pass(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, poll=3.0, vsk=True, challenge="")
    w.go(passes=40)                      # 120 s
    expected = 1 + int((w.now - T0) // md.AVAIL_CHECK_S)
    assert expected - 1 <= w.chal_reads <= expected, (w.chal_reads, expected)


# ══ THE DRAIN ════════════════════════════════════════════════════════════════════════════════════════════
def test_draining_proves_nothing_and_takes_no_new_request_but_finishes_its_commitments(tmp_path, monkeypatch,
                                                                                         capsys):
    w = World(tmp_path, monkeypatch, reqs=["jobdelta"], vsk=True, challenge=CHAL)
    pending = w.add("jobeps")
    md._journal_commitment(w.keydir, pending, "0.1,0.2", "0.3,0.4")
    w.res.append(pending)
    (w.keydir / md.DRAIN_FILE).write_text("", encoding="utf-8")
    w.go(passes=4)
    assert w.proofs == [] and w.generations == [], "no proof and no new request while draining"
    assert [c[0] for c in w.commit_calls] == [pending], "a journalled commitment is still anchored"
    hb = w.hb()
    assert hb["draining"] is True and hb["presence"]["result"] == "not attempted"
    assert "starts DRAINING" in capsys.readouterr().out


def test_a_drain_removed_resumes_and_is_said(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, reqs=["jobdelta"])
    drain = w.keydir / md.DRAIN_FILE
    drain.write_text("", encoding="utf-8")
    w.on_sleep = lambda n: drain.unlink() if n == 2 else None
    w.go(passes=4)
    assert [j for j, _ in w.generations] == ["jobdelta"] and w.hb()["draining"] is False
    assert "no longer draining" in capsys.readouterr().out


def test_drain_state_has_three_answers(tmp_path, monkeypatch):
    assert md.drain_state(tmp_path, prev=True) is False
    (tmp_path / md.DRAIN_FILE).write_text("", encoding="utf-8")
    assert md.drain_state(tmp_path, prev=False) is True
    monkeypatch.setattr(md.os, "lstat", lambda p: (_ for _ in ()).throw(PermissionError("denied")))
    assert md.drain_state(tmp_path, prev=True) is True and md.drain_state(tmp_path, prev=False) is False, (
        "a state that cannot be read keeps the one it had")


# ══ READERS WITHOUT A DEFAULT ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("out, want", [
    (json.dumps({"params": {"min_stake": "1000000"}}), 1_000_000),
    (json.dumps({"params": {}}), 0),                                  # read, and proto3 omits a zero
    ("params:" + NL + '  min_stake: "1000"' + NL + "  avail_epoch_blocks: \"288\"", 1000),
    ("params:" + NL + "  allow:" + NL + "  - a" + NL + '  min_stake: "5"', None),   # not flat: not read
    ("Error: rpc error: code = Unavailable desc = connection refused", None),
    ("", None),
    (json.dumps({"params": {"min_stake": "many"}}), None),
])
def test_min_stake_is_read_or_none_never_a_default(monkeypatch, out, want):
    monkeypatch.setattr(md, "query", lambda sub, *p, flags=(): out)
    assert md.chain_min_stake() == want


def test_an_unread_min_stake_registers_nothing_and_asks_the_faucet_nothing(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.ABSENT, {}))
    monkeypatch.setattr(md, "query", lambda sub, *p, flags=(): "Error: connection refused")
    monkeypatch.setattr(md, "faucet_fund_classified", lambda url, addr: calls.append("faucet") or (True, "", ""))
    monkeypatch.setattr(md, "tx_from", lambda *a, **k: calls.append("tx") or "")
    a = types.SimpleNamespace(id=MID, faucet="http://f", keydir=str(tmp_path))
    assert md.register_attempt(a, ADDR, "ab" * 32, "") == (md.UNREAD, "params_unread")
    out = capsys.readouterr().out
    assert calls == [] and "min_stake could not be read" in out
    assert "ready" not in out and "create-miner" not in out, "deploy/join.sh::wait_healthy reads those words"


@pytest.mark.parametrize("out, want", [
    (json.dumps({"balances": [{"denom": "udndr", "amount": "123"}]}), 123),
    (json.dumps({"balances": [{"denom": "other", "amount": "9"}]}), 0),
    (json.dumps({"balances": [], "pagination": {}}), 0),
    (json.dumps({"pagination": {}}), 0),                              # proto3 omits an empty list
    ("Error: rpc error: code = Unavailable", None),
    (json.dumps({"balances": [{"denom": "udndr", "amount": "lots"}]}), None),
])
def test_a_balance_is_read_or_none(monkeypatch, out, want):
    monkeypatch.setattr(md, "run", lambda c, t=None, stdin="": out)
    assert md.bal_token(ADDR) == want


def test_owner_mode_asks_the_faucet_nothing_for_a_balance_it_could_not_read(tmp_path, monkeypatch, capsys):
    faucet, prepared = [], []
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.ABSENT, {}))
    monkeypatch.setattr(md, "bal_token", lambda who: None)
    monkeypatch.setattr(md, "faucet_fund", lambda url, who: faucet.append(who) or (True, ""))
    monkeypatch.setattr(md, "owner_prepare", lambda *a, **k: prepared.append(a[1]) or (True, "         ok"))
    a = types.SimpleNamespace(id=MID, keydir=str(tmp_path), faucet="http://f", once=True)
    monkeypatch.setattr(md, "_registration_stake", lambda: 1000)
    assert md.owner_wait(a, ADDR, "dendra1owner", "ab" * 32, "", sleep=lambda s: None) is False
    assert faucet == [] and prepared == ["create-miner"]
    assert "has no funds" not in capsys.readouterr().out, "an unread balance is not an empty account"
    monkeypatch.setattr(md, "_registration_stake", lambda: None)
    prepared.clear()
    md.owner_wait(a, ADDR, "dendra1owner", "ab" * 32, "", sleep=lambda s: None)
    assert prepared == [] and "min_stake could not be read" in capsys.readouterr().out


def test_an_unread_vrf_key_is_never_announced_missing(monkeypatch):
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.UNREAD, {"why": "node down"}))
    assert md.miner_vrf_onchain(MID) is None
    assert md.vrf_key_notice(MID, VPK, None) == ""
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.PRESENT, {"creator": ADDR, "operator": ADDR}))
    assert md.miner_vrf_onchain(MID) == "", "a record read without the field: proto3's empty string"
    assert "NO VRF KEY ANCHORED ON-CHAIN" in md.vrf_key_notice(MID, VPK, "")
    assert "VRF KEY MISMATCH" in md.vrf_key_notice(MID, VPK, "d" * 64)
    assert md.vrf_key_notice(MID, VPK, VPK.upper()) == ""


def test_main_says_nothing_about_a_vrf_key_it_could_not_read(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, vsk=True)
    monkeypatch.setattr(md, "miner_vrf_onchain", lambda mid: None)
    w.go(once=True)
    out = capsys.readouterr().out
    assert "NO VRF KEY" not in out and "VRF KEY MISMATCH" not in out


# ══ THE MODEL A COMMITMENT WAS COMPUTED WITH ═════════════════════════════════════════════════════════════
def test_a_resumption_anchors_with_the_journalled_model_never_the_current_one(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    key = w.add("jobeps")
    md._journal_commitment(w.keydir, key, "0.1,0.2", "0.3,0.4", model_id="m-old", weights_hash="aa" * 32)
    w.res.append(key)
    monkeypatch.setattr(md, "MODEL_ID", "m-new")
    w.go(once=True)
    assert w.commit_calls[0][2] == ["--model-id", "m-old", "--weights-hash", "aa" * 32]


def test_an_entry_journalled_before_the_model_fields_is_anchored_without_them(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    key = w.add("jobeps")
    md._journal_commitment(w.keydir, key, "0.1,0.2", "0.3,0.4")
    w.res.append(key)
    monkeypatch.setattr(md, "MODEL_ID", "m-new")
    w.go(once=True)
    assert w.commit_calls[0][2] == []


def test_the_first_attempt_journals_the_flags_it_anchors_with(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    monkeypatch.setattr(md, "MODEL_ID", "m-x")
    w.whash = "cc" * 32
    w.anchor_refuse = 3
    w.go(once=True)
    entry = md._pending_commitment(w.keydir, "jobalpha" + SUFFIX)
    assert (entry.get("model_id"), entry.get("weights_hash")) == ("m-x", "cc" * 32)
    assert all(c[2] == md._journal_flags(entry) for c in w.commit_calls) and len(w.commit_calls) == 3


def test_an_empty_weights_hash_is_read_again(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobone"])
    monkeypatch.setattr(md, "MODEL_ID", "m-x")

    def later(n):
        if n == 3:
            w.whash = "dd" * 32
        if n == 12:                       # 360 s after the start: past WHASH_RETRY_S
            w.add("jobtwo")
    w.on_sleep = later
    w.go(passes=14)
    assert [c[2] for c in w.commit_calls] == [["--model-id", "m-x"],
                                              ["--model-id", "m-x", "--weights-hash", "dd" * 32]]


class _Srv:
    def __init__(self, routes):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("model_id, listed, found", [
    ("mistral-nemo", "mistral-nemo:latest", True),       # an untagged name, listed with Ollama's implicit tag
    ("mistral-nemo:latest", "mistral-nemo:latest", True),
    ("qwen3:14b", "qwen3:14b", True),
    ("qwen3", "qwen3:14b", False),                        # another tag is another model
    ("hf.co/user/model", "hf.co/user/model:latest", True),
    ("registry.local:5000/model", "registry.local:5000/model:latest", True),   # a port is not a tag
])
def test_the_served_model_is_found_with_its_tag(monkeypatch, model_id, listed, found):
    srv = _Srv({"/api/tags": {"models": [{"name": listed, "model": listed, "digest": "sha256:" + "e" * 64}]}})
    try:
        monkeypatch.setenv("OLLAMA_ENDPOINT", srv.url)
        monkeypatch.setattr(md, "MODEL_ID", model_id)
        assert md.model_weights_hash() == ("e" * 64 if found else "")
    finally:
        srv.close()


# ══ THE ENCRYPTION KEY ANCHORED ═════════════════════════════════════════════════════════════════════════
def test_the_encryption_key_has_three_states_and_a_mismatch_is_said_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    rec = {"enc_pubkey": "ff" * 32}
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.PRESENT, dict(rec)))
    st = md.check_enc_key(MID, "11" * 32)
    out = capsys.readouterr().out
    assert st == md.ENC_MISMATCH and out.count("ENCRYPTION KEY MISMATCH") == 1
    assert "--new-enc-pubkey " + "11" * 32 in out and "never taken for you" in out
    assert json.loads((tmp_path / "status.json").read_text())["enc_key"]["state"] == "mismatch"
    assert md.check_enc_key(MID, "11" * 32, st) == md.ENC_MISMATCH and capsys.readouterr().out == ""
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.UNREAD, {"why": "down"}))
    assert md.check_enc_key(MID, "11" * 32, st) == md.ENC_MISMATCH, "unread changes nothing"
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.ABSENT, {}))
    assert md.check_enc_key(MID, "11" * 32, None) is None
    rec["enc_pubkey"] = ("11" * 32).upper()
    monkeypatch.setattr(md, "registry_record", lambda mid: (md.PRESENT, dict(rec)))
    assert md.check_enc_key(MID, "11" * 32, st) == md.ENC_OK
    assert "proofs resume" in capsys.readouterr().out
    rec["enc_pubkey"] = ""
    assert md.check_enc_key(MID, "11" * 32, md.ENC_OK) == md.ENC_UNANCHORED
    assert "NO ENCRYPTION KEY ANCHORED" in capsys.readouterr().out


def test_no_proof_while_the_encryption_key_differs_and_one_once_it_matches(tmp_path, monkeypatch, capsys):
    w = World(tmp_path / "a", monkeypatch, vsk=True, challenge=CHAL, enc_anchored="ff" * 32)
    w.go(passes=3)
    out = capsys.readouterr().out
    assert w.proofs == [] and out.count("ENCRYPTION KEY MISMATCH for " + MID) == 1
    assert "encryption pubkey is NOT deposited at the relay" in out
    assert w.hb()["presence"]["result"] == "not attempted"
    w2 = World(tmp_path / "b", monkeypatch, vsk=True, challenge=CHAL)
    w2.go(passes=3)
    assert [c for c, _ in w2.proofs] == [CHAL], "the same world with the key anchored proves"


# ══ THE CLIENT'S DECLARED WAIT ═════════════════════════════════════════════════════════════════════════
def test_the_declared_wait_is_a_positive_number_or_none():
    assert md.declared_wait_s({"wait_s": 240}) == 240.0 and md.declared_wait_s({"wait_s": 1.5}) == 1.5
    for body in ({"wait_s": "240"}, {"wait_s": True}, {"wait_s": 0}, {"wait_s": -3}, {"wait_s": float("inf")},
                 {"wait_s": float("nan")}, {}, None, {"max_out": 768}):
        assert md.declared_wait_s(body) is None, body
    assert md.answer_timing(7.0, 5.0) == "late" and md.answer_timing(5.0, 5.0) == "on_time"
    assert md.answer_timing(3.0, None) is None


def test_an_answer_is_judged_against_the_wait_its_client_declared(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.add("jobslow", wait_s=5)
    w.add("jobfree")
    w.gen_s = 7.0
    w.go(once=True)
    hb = w.hb()
    assert (hb["answers_late"], hb["answers_on_time"], hb["answers_wait_undeclared"]) == (1, 0, 1)
    out = capsys.readouterr().out
    assert out.count("past the 5 s its client declared it waits") == 1


def test_the_job_record_carries_the_verdict(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.add("jobfast", wait_s=240)
    w.go(once=True)
    job = w.hb()["job"]
    assert (job["result"], job["wait_s"], job["late"]) == ("anchored", 240.0, False)
    w2 = World(tmp_path / "b", monkeypatch, reqs=["jobfree"])
    w2.go(once=True)
    job = w2.hb()["job"]
    assert job["wait_s"] is None and job["late"] is None, "no declared wait, no verdict"


# ══ THE ENGINE'S VERSION ═════════════════════════════════════════════════════════════════════════════
def test_the_engine_version_is_read_or_said_unread():
    srv = _Srv({"/api/version": {"version": "0.32.1"}})
    try:
        eng = types.SimpleNamespace(endpoint=srv.url)
        assert md.engine_version("ollama", eng) == {"engine": "ollama", "version": "0.32.1"}
    finally:
        srv.close()
    srv = _Srv({"/api/version": {}})
    try:
        got = md.engine_version("ollama", types.SimpleNamespace(endpoint=srv.url))
        assert got["version"] is None and "no version" in got["why"]
    finally:
        srv.close()
    got = md.engine_version("ollama", types.SimpleNamespace(endpoint="http://127.0.0.1:9"), timeout_s=2)
    assert got["version"] is None and got["why"].startswith("not read")
    assert md.engine_version("mock", None)["why"] == "not an Ollama engine"


def test_the_engine_version_is_in_the_heartbeat(tmp_path, monkeypatch, capsys):
    srv = _Srv({"/api/version": {"version": "0.32.1"}})
    try:
        w = World(tmp_path, monkeypatch)
        w.backend_name = "ollama"
        w.miner.backend.endpoint = srv.url
        w.go(once=True)
        assert w.hb()["engine_version"] == {"engine": "ollama", "version": "0.32.1"}
        assert "inference engine: Ollama 0.32.1" in capsys.readouterr().out
    finally:
        srv.close()


# ══ THE HEARTBEAT ══════════════════════════════════════════════════════════════════════════════════════
def test_every_new_counter_is_in_the_first_heartbeat_at_zero(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.go(once=True)
    hb = w.hb()
    assert {k: hb.get(k) for k in md._JOB_COUNTS} == {k: 0 for k in md._JOB_COUNTS}
    assert hb["draining"] is False


# ══ AN UNREAD REGISTRY IS NOT AN ABSENT MINER ═════════════════════════════════════════════════════════════
def test_the_registration_allows_a_proof_in_three_states_not_two():
    assert md.registration_allows_proof(md.REGISTERED, "") is True
    assert md.registration_allows_proof(md.UNREAD, "registry_unread") is True, "unknown is not absent"
    for st, why in ((md.UNREAD, "params_unread"), (md.DEFERRED, "ip_quota"), (md.REFUSED, "chain_refused")):
        assert md.registration_allows_proof(st, why) is False, (st, why)


def test_a_registry_unread_at_start_does_not_silence_a_registered_miner(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, poll=3.0, vsk=True, challenge=CHAL)
    w.reg_at = lambda now: md.UNREAD if now < T0 + 5 else md.PRESENT   # a node that answers a few seconds late
    w.go(passes=10)
    assert w.hb()["registration"]["reason"] == "registry_unread", "the fixture did produce the unread registry"
    assert w.proofs and w.proofs[0][1] - T0 <= md.AVAIL_CHECK_S, (
        "proven at start, not REGISTER_RETRY_S later: %r" % [round(t - T0) for _, t in w.proofs])


@pytest.mark.parametrize("why", ["ip_quota", "params_unread"])
def test_a_miner_known_not_registered_proves_nothing(tmp_path, monkeypatch, why):
    w = World(tmp_path, monkeypatch, vsk=True, challenge=CHAL)
    w.reg = md.ABSENT
    if why == "params_unread":
        monkeypatch.setattr(md, "chain_min_stake", lambda *a, **k: None)
    w.go(passes=30)                       # 900 s: one replayed attempt
    assert w.hb()["registration"]["reason"] == why, "the fixture did produce the absent miner"
    assert w.proofs == [] and w.chal_reads == 0 and not w.kinds("probe"), (
        "no challenge read, no test inference, no proof for a miner the chain is known not to record")


# ══ OWNER MODE: A FAILED READING PUTS NOTHING OFF ═══════════════════════════════════════════════════════
def _owner_run(tmp_path, monkeypatch, registry, stake, until_s):
    now, prepared = [T0], []

    def sleep(s):
        now[0] += s
        if now[0] - T0 > until_s:
            raise Stop()
    monkeypatch.setenv("DENDRA_STATUS_FILE", str(tmp_path / "status.json"))
    md._STATUS.clear()
    monkeypatch.setattr(md, "time", types.SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(md, "registry_record", lambda mid: registry(now[0] - T0))
    monkeypatch.setattr(md, "_registration_stake", lambda: stake(now[0] - T0))
    monkeypatch.setattr(md, "bal_token", lambda who: 10 ** 12)
    monkeypatch.setattr(md, "owner_prepare", lambda keydir, sub, args, owner, mid, extra=():
                        prepared.append((sub, now[0] - T0)) or (True, "         (prepared)"))
    a = types.SimpleNamespace(keydir=str(tmp_path), id=MID, faucet="http://faucet.invalid", once=False)
    try:
        md.owner_wait(a, ADDR, "dendra1owner", "ab" * 32, VPK, sleep=sleep)
    except Stop:
        pass
    return [t for sub, t in prepared if sub == "create-miner"]


def test_owner_mode_an_unread_min_stake_is_read_again_at_the_cadence_it_announces(tmp_path, monkeypatch, capsys):
    got = _owner_run(tmp_path, monkeypatch, lambda t: (md.ABSENT, {}), lambda t: None if t < 5 else 1000, 200)
    out = capsys.readouterr().out
    assert "read again in %.0f s" % md.OWNER_POLL_S in out
    assert got and got[0] <= 2 * md.OWNER_POLL_S, "announced %.0f s, prepared at %r" % (md.OWNER_POLL_S, got)


def test_owner_mode_an_unread_registry_is_read_again_at_the_cadence_it_announces(tmp_path, monkeypatch):
    got = _owner_run(tmp_path, monkeypatch,
                     lambda t: (md.UNREAD, {"why": "node down"}) if t < 5 else (md.ABSENT, {}), lambda t: 1000, 200)
    assert got and got[0] <= 2 * md.OWNER_POLL_S, "prepared at %r" % got


def test_owner_mode_a_long_outage_is_said_once_per_reprint_not_at_every_pass(tmp_path, monkeypatch, capsys):
    got = _owner_run(tmp_path, monkeypatch, lambda t: (md.ABSENT, {}), lambda t: None, 7000)
    said = capsys.readouterr().out.count("min_stake could not be read")
    assert got == [], "nothing prepared without a stake that was read"
    assert said == 1 + int(7000 // md.OWNER_REPRINT_S), "said %d times over 7 000 s" % said


# ══ TWO CLOCKS JUDGE A REQUEST STALE ═══════════════════════════════════════════════════════════════════
def test_a_block_time_is_read_as_cometbft_writes_it_or_not_at_all():
    assert md.rfc3339_epoch("2020-01-01T00:00:00.123456789Z") == pytest.approx(1577836800.123456789, abs=1e-6)
    assert md.rfc3339_epoch("2020-01-01T02:00:00+02:00") == 1577836800
    assert md.rfc3339_epoch("2019-12-31T22:00:00-02:00") == 1577836800
    assert md.rfc3339_epoch(iso_utc(T0 + 0.5)) == pytest.approx(T0 + 0.5, abs=1e-6)
    for bad in ("2020-01-01T00:00:00", "2020-13-01T00:00:00Z", "2020-02-30T00:00:00Z", "yesterday", "", None, 5):
        assert md.rfc3339_epoch(bad) is None, bad


def test_the_chain_clock_carries_its_last_reading_forward(monkeypatch):
    clock, reads, answers = [1000.0], [], [T0, None]
    monkeypatch.setattr(md, "time", types.SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(md, "_CHAIN_CLOCK", fresh_chain_clock())
    monkeypatch.setattr(md, "chain_block_time", lambda: reads.append(clock[0]) or answers[len(reads) - 1])
    assert md.chain_now() == T0
    clock[0] += 10
    assert md.chain_now() == T0 + 10 and reads == [1000.0], "within CHAIN_CLOCK_READ_S: not read again"
    clock[0] += md.CHAIN_CLOCK_READ_S
    assert md.chain_now() == T0 + 10 + md.CHAIN_CLOCK_READ_S, "a reading that failed keeps the previous one"
    assert len(reads) == 2
    monkeypatch.setattr(md, "_CHAIN_CLOCK", fresh_chain_clock())
    monkeypatch.setattr(md, "chain_block_time", lambda: None)
    assert md.chain_now() is None, "never read: no chain clock at all"


def test_a_miner_clock_ahead_of_the_chain_serves_fresh_requests_and_says_so(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, poll=3.0, vsk=True, challenge=CHAL)
    w.chain_offset = -700.0               # this machine's clock runs 700 s ahead of the chain's
    for i in range(3):
        w.add(jid_at(T0 - 700 - i))       # made a few seconds ago by the client's clock, which keeps the chain's time
    w.go(passes=20)
    assert w.proofs, "the miner is present"
    assert len(w.generations) == 3 and w.hb()["requests_stale"] == 0, "and it serves"
    assert 690 <= w.hb()["clock_ahead_of_chain_s"] <= 710
    assert capsys.readouterr().out.count("ahead of the chain's latest block") == 1


def test_a_request_both_clocks_call_stale_is_not_served(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, poll=3.0)
    w.chain_offset = 0.0
    for i in range(4):
        w.add(jid_at(T0 - 900 - i))
    w.go(passes=40)                       # 120 s
    assert w.generations == [] and w.hb()["requests_stale"] == 4
    assert 1 <= w.status_reads <= 1 + int((w.now - T0) // md.CHAIN_CLOCK_READ_S), (
        "the chain's clock read on a clock, not once per request and pass: %d" % w.status_reads)
    w2 = World(tmp_path / "b", monkeypatch, poll=3.0, reqs=[jid_at(T0 - 30)])
    w2.chain_offset = 0.0
    w2.go(passes=10)
    assert len(w2.generations) == 1 and w2.status_reads == 0, "a request this clock calls fresh asks the chain nothing"


# ══ AN ENGINE THAT ANSWERS BUT CANNOT EMBED SERVES NOTHING ════════════════════════════════════════════
def test_the_serving_probe_embeds_only_what_an_engine_answered():
    asked = []

    def emb(text):
        asked.append(text)
        return "0.1,0.2"

    def ok():
        return md.PROBE_ANSWERED, "", 0.2

    def silent():
        return md.PROBE_SILENT, "empty", 0.1
    assert md.serving_probe(ok, emb) == (md.PROBE_ANSWERED, "", 0.2) and asked == [md.PRESENCE_PROBE_PROMPT]
    assert md.serving_probe(silent, emb)[0] == md.PROBE_SILENT and len(asked) == 1, "no embedding of a silence"
    st, why, _ = md.serving_probe(ok, lambda t: (_ for _ in ()).throw(RuntimeError("no embedding")))
    assert st == md.PROBE_SILENT and "embedding" in why
    assert md.serving_probe(ok, lambda t: "")[0] == md.PROBE_SILENT
    assert md.serving_probe(ok, None) == (md.PROBE_ANSWERED, "", 0.2)


def test_an_embedder_down_gives_no_request_up_and_proves_nothing_until_it_is_back(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"], vsk=True, challenge=CHAL)
    w.embedder = "down"
    back = {}

    def comes_back(n):
        if n == 150:                      # 4 500 s
            w.embedder, back["at"] = "up", w.now
    w.on_sleep = comes_back
    w.go(passes=200)                      # 6 000 s
    before = [t for _, t in w.generations if t < back["at"]]
    assert 4 <= len(before) <= 7, "retried at the pauses while the embedder is down: %d" % len(before)
    assert not [t for _, t in w.proofs if t < back["at"]], "no presence while no answer can be committed"
    hb = w.hb()
    assert hb["abandoned"] == 0, "a failure the engine could not embed around is the engine's: nothing given up"
    assert hb["commits_anchored"] == 1 and w.proofs, "once the embedder is back: served, and present"


# ══ WHAT THE LOOP ALREADY DID RIGHT, NOW PINNED ═════════════════════════════════════════════════════════
def test_a_listing_that_was_not_read_prunes_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    w.infer_fails = lambda jid: True
    w.go(once=True)
    assert "jobalpha" + SUFFIX in w.failures()
    w.listing_fails = True                # a relay hiccup: the listing's failure value
    w.go(once=True)
    assert "jobalpha" + SUFFIX in w.failures(), "an unread listing is not a relay that dropped the request"


def test_a_success_clears_the_failure_it_follows(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    n = []
    w.infer_fails = lambda jid: (n.append(jid) or len(n)) == 1
    w.go(passes=10)                       # 300 s: past the first pause
    assert len(w.generations) == 2 and w.hb()["commits_anchored"] == 1
    assert "jobalpha" + SUFFIX not in w.failures()


def test_a_mismatch_fixed_on_chain_resumes_the_proofs_in_the_same_process(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, vsk=True, challenge=CHAL, enc_anchored="ff" * 32)

    def rotated(n):
        if n == 20:                       # 600 s: the owner anchors this node's key
            w.enc_anchored = w.mypub
    w.on_sleep = rotated
    w.go(passes=80)                       # 2 400 s: past the half-hourly reading
    assert w.proofs and w.proofs[0][1] - T0 >= 1800, "proven after the half-hourly reading, without a restart"
    assert "proofs resume" in capsys.readouterr().out


@pytest.mark.parametrize("anchored", ["mismatch", "match"])
def test_a_late_registration_confronts_the_encryption_key_before_any_proof(tmp_path, monkeypatch, capsys, anchored):
    w = World(tmp_path, monkeypatch, vsk=True, challenge=CHAL,
              enc_anchored=("ff" * 32) if anchored == "mismatch" else None)
    monkeypatch.setattr(md, "REGISTER_RETRY_S", 600.0)
    w.reg = md.ABSENT

    def recorded(n):
        if n == 5:                        # the chain records the miner (a create-miner included late)
            w.reg = md.PRESENT
    w.on_sleep = recorded
    w.go(passes=45)                       # 1 350 s: the replayed attempt, before the half-hourly reading
    assert w.hb()["registration"]["state"] == md.REGISTERED, "the fixture did register the miner late"
    if anchored == "mismatch":
        assert w.proofs == [] and "ENCRYPTION KEY MISMATCH for " + MID in capsys.readouterr().out
    else:
        assert w.proofs and w.proofs[0][1] - T0 >= 600, "registered late, then proven"


def test_a_draining_miner_replays_no_registration(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    monkeypatch.setattr(md, "REGISTER_RETRY_S", 600.0)
    w.reg = md.ABSENT
    (w.keydir / md.DRAIN_FILE).write_text("", encoding="utf-8")
    w.go(passes=50)                       # 1 500 s: two attempts would have been replayed
    assert w.faucet_calls == [] and w.creates == 0, (
        "no registration, at start or replayed, while draining: %r" % [round(t - T0) for t in w.faucet_calls])


def test_a_draining_miner_restarted_after_its_exit_does_not_register_again(tmp_path, monkeypatch, capsys):
    # The restart that brought this case: the exit took the miner out of the registry, the drain is still set
    # (exit-miner.sh keeps it on a running miner), and the process starts again -- `restart: unless-stopped`, a
    # reboot, the hourly re-run of join.sh. A create-miner that WOULD succeed is what is at stake: it stakes again.
    w = World(tmp_path, monkeypatch, vsk=True, challenge=CHAL)
    w.reg, w.create_registers = md.ABSENT, True
    (w.keydir / md.DRAIN_FILE).write_text("", encoding="utf-8")
    w.go(passes=10)
    assert w.creates == 0 and w.faucet_calls == [], "a drained daemon restarted sent create-miner %d time(s)" % w.creates
    assert w.reg == md.ABSENT and w.proofs == []
    hb = w.hb()
    assert hb["registration"]["reason"] == "draining" and hb["draining"] is True
    out = capsys.readouterr().out
    assert "is NOT registered, and it is DRAINING" in out
    assert "create-miner" not in out and " ready " not in out, "join.sh::wait_healthy reads those words as a registration"


def test_a_draining_miner_the_chain_still_records_stays_registered_at_start(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    (w.keydir / md.DRAIN_FILE).write_text("", encoding="utf-8")
    w.go(once=True)
    assert w.hb()["registration"] == {"state": md.REGISTERED, "reason": "", "retry_at": 0}
    assert w.faucet_calls == [] and w.creates == 0


def test_once_the_drain_is_removed_the_registration_is_attempted_at_the_next_pass(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    monkeypatch.setattr(md, "REGISTER_RETRY_S", 600.0)
    w.reg = md.ABSENT
    drain = w.keydir / md.DRAIN_FILE
    drain.write_text("", encoding="utf-8")
    gone = {}

    def undrain(n):
        if n == 3:
            drain.unlink()
            gone["at"] = w.now
    w.on_sleep = undrain
    w.go(passes=6)
    assert w.faucet_calls and min(w.faucet_calls) >= gone["at"], (
        "asked only once the drain was gone, and at once: %r" % [round(t - T0) for t in w.faucet_calls])
    assert min(w.faucet_calls) - gone["at"] <= 2 * w.poll


# ══ THE WORK QUEUE IS READ AS THIS MINER'S SLICE ═════════════════════════════════════════════════════════
def test_the_loop_reads_the_queue_filtered_on_its_own_suffix(tmp_path, monkeypatch):
    # relay.py::_list_suffix serves `/list?suffix=`; a loop that never asks for it carries every key of the
    # network on every round. Another miner's request in the queue is never served either way.
    w = World(tmp_path, monkeypatch, reqs=["jobalpha"])
    w.reqs["jobother__dm1someone"] = {"client_eph_pk": "00" * 32, "nonce": "00" * 24, "ct": "00" * 8, "max_out": 0}
    w.order.append("jobother__dm1someone")
    w.go(passes=3)
    assert w.listing_suffixes and set(w.listing_suffixes) == {SUFFIX}, w.listing_suffixes
    assert [j for j, _ in w.generations] == ["jobalpha"]
