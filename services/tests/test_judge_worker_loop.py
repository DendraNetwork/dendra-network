"""THE SHIPPED LOOP, RUN FOR REAL: `judge_worker.py --once`, as a process, against a fake chain, relay and engine.

WHY A PROCESS. Every guard of the judge can be right in its own function and still be skipped by the loop that
calls it -- a correct cosine function that its loop feeds the wrong embedding lets no vote through, and no
bench of the function alone can see it. So this bench starts the worker the way the kit does and reads what
it DID: the transactions its `dendrad` received, the reveals it fetched, the references its engine was asked
for, and the counters it wrote. Nothing in the worker is patched.

THE FAKES, and what each one stands for:
  * `dendrad` -- a script on a HERMETIC PATH (its directory and a python3 link, nothing else): the job list,
    the anchored juries (one may change between two reads: {"seq": [...]}), the commits, and `create-commit`
    with the chain's refusals ("index already set", an unauthorized signer); a success at block prints NO
    `code:` line, as proto3 omits a zero;
  * the relay -- sealed reveals and request envelopes over HTTP, every GET recorded;
  * the engine -- tests/test_judge_anchor_embedding.py::FakeEngine: 768-number embeddings, the Ollama length
    refusal, references, and the judge model's OUI/NON read from the worker's own prompt templates, with an
    unreadable answer where a test asks for one.
The primaries' anchors are made by `Miner._embed_output` through the same engine: the miner's real path.

THE AUDITS OF ONE PASS (one each): A honest with a request cap of 96; B not on this judge's jury; C a
divergent answer; D already voted; E word salad; F a jury that cannot be read; G no jury anchored; L an answer
longer than the engine's context; R a request envelope missing; K a verdict already on chain that the read
did not see; Q a verdict the chain refuses; X listed last but disputed first, whose judge call fails half-way.

WORD SALAD WITHOUT ITS ENVELOPE (`salad`, one pass): the coherence stage needs no request cap, so a missing or
unreadable envelope does not switch it off.

SEVERAL PASSES OF ONE PROCESS (`passes`, `--passes 3 --retry-pause 0`): what the judge keeps between passes
lives in its memory only, so it is measured in one process: a judgment is given once, a decided verdict is
posted again and never judged again, an audit without a jury is read again until one is anchored, an unread
jury is read again, and an unreadable read of the judge model is tried again.

WHAT A CHEAT AND A RETRY CANNOT BUY (`cheat`, three passes): a question shown without its salt is never graded; a
retried audit whose references once agreed does not vote VALID on a divergence sampled at the retry, while a
divergence at the first judgment still does; and an answer's coherence is read once across the retries.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# THE ROOT IS POINTABLE (DENDRA_MODEA), as in tests/test_relay_startup_alarm.py: a mutated copy of the worker is
# loaded and RUN from there, never this file's own tree. HERE only finds this bench's sibling fakes.
MODEA = Path(os.environ.get("DENDRA_MODEA") or HERE.parent)
sys.path.insert(0, str(MODEA))
sys.path.insert(0, str(HERE))

import reveal_helpers as rv  # noqa: E402
from modea import crypto  # noqa: E402
from modea import judge as J  # noqa: E402
from modea import miner as M  # noqa: E402
from test_judge_anchor_embedding import CONTEXT_WORDS, FakeEngine  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "posix", reason="a hermetic PATH with a fake dendrad needs POSIX")

JUDGE = "m-judge"
JUDGE_MODEL = "qwen-judge"
REF_MODEL = "bench-model"

FAKE_DENDRAD = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
S = os.environ["FAKE_STATE"]
A = sys.argv[1:]


def load(name, default):
    p = os.path.join(S, name)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return json.load(f)


def save(name, value):
    with open(os.path.join(S, name), "w") as f:
        json.dump(value, f)


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


with open(os.path.join(S, "dendrad.log"), "a") as f:
    f.write(json.dumps(A) + chr(10))
POS = A[A.index("--") + 1:] if "--" in A else []
if A[:1] == ["status"]:
    print(json.dumps({"node_info": {"network": "bench-1"}}))
    sys.exit(0)
if A[:3] == ["query", "modelregistry", "params"]:
    print(json.dumps({"params": {"audit_judge_model": "qwen-judge"}}))
    sys.exit(0)
if A[:3] == ["query", "jobs", "list-job"]:
    print(json.dumps({"job": load("jobs.json", []), "pagination": {}}))
    sys.exit(0)
if A[:3] == ["query", "jobs", "audit-committee"]:
    jury = load("juries.json", {}).get(POS[0])
    if isinstance(jury, dict) and "seq" in jury:   # a jury that changes between two reads
        reads = load("jury_reads.json", {})
        n = reads.get(POS[0], 0)
        reads[POS[0]] = n + 1
        save("jury_reads.json", reads)
        jury = jury["seq"][min(n, len(jury["seq"]) - 1)]
    if jury == "NOTFOUND":
        fail("Error: rpc error: code = NotFound desc = no jury anchored for this job: key not found")
    if not isinstance(jury, list):
        fail("Error: rpc error: code = Unavailable desc = connection refused")
    print(json.dumps({"members": jury, "anchored_height": "5"}))
    sys.exit(0)
if A[:3] == ["query", "jobs", "get-commit"]:
    key = POS[0]
    if key in load("flaky_reads.json", []):
        fail("Error: rpc error: code = Unavailable desc = connection refused")
    c = load("commits.json", {}).get(key)
    if c is None:
        fail("Error: rpc error: code = NotFound desc = not found")
    print(json.dumps({"commit": c}))
    sys.exit(0)
if A[:2] == ["query", "tx"]:
    t = load("txs.json", {}).get(A[2])
    if t is None:
        fail("Error: tx not found")
    if t["code"]:
        print("code: " + str(t["code"]))
    print('height: "12"')
    print("raw_log: '" + t["raw_log"] + "'")
    print("txhash: " + A[2])
    sys.exit(0)
if A[:3] == ["tx", "jobs", "create-commit"]:
    key, pcommit, result, kind = POS[:4]
    commits = load("commits.json", {})
    h = hashlib.sha256(json.dumps(A).encode()).hexdigest().upper()
    if key in load("refuse.json", []):
        code, raw = 4, "failed to execute message; message index: 0: only the operator of the miner may anchor its commit: unauthorized"
    elif key in commits:
        code, raw = 18, "failed to execute message; message index: 0: index already set: invalid request"
    else:
        commits[key] = {"index": key, "promptCommit": pcommit, "result": result, "kind": kind}
        save("commits.json", commits)
        code, raw = 0, ""
    txs = load("txs.json", {})
    txs[h] = {"code": code, "raw_log": raw}
    save("txs.json", txs)
    created = load("created.json", [])
    created.append({"key": key, "result": result, "code": code})
    save("created.json", created)
    print("code: 0")
    print("txhash: " + h)
    sys.exit(0)
fail("fake dendrad: unexpected " + " ".join(A))
'''


class _Relay(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        self.server.gets.append(self.path)
        _, kind, key = (self.path.split("/", 2) + ["", ""])[:3]
        doc = self.server.store.get(kind, {}).get(key)
        out = json.dumps(doc if doc is not None else {"error": "not found"}).encode()
        self.send_response(200 if doc is not None else 404)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _template(t, *fields):
    """The constant pieces of a judge prompt template, split at its {fields}: the fake model recognises the
    worker's OWN prompts and reads the data out of them, so a template that changes cannot go unnoticed."""
    parts, rest = [], t
    for f in fields:
        head, rest = rest.split("{" + f + "}", 1)
        parts.append(head)
    return parts + [rest]


def _fill(prompt, parts):
    """The values a template was filled with, or None when `prompt` is not that template."""
    if not prompt.startswith(parts[0]) or not prompt.endswith(parts[-1]):
        return None
    body, vals = prompt[len(parts[0]):len(prompt) - len(parts[-1])], []
    for sep in parts[1:-1]:
        val, body = body.split(sep, 1)
        vals.append(val)
    return vals + [body]


SAME = _template(J.PROMPT, "ref", "cand")
COHERENT = _template(J.COHERENCE_PROMPT, "text")
RELEVANT = _template(J.RELEVANCE_PROMPT, "prompt", "cand")
MULTI = _template(J.MULTI_PROMPT, "prompt", "ref", "cand")


def _seal(pub_hex, obj):
    """A reveal sealed to `pub_hex` in the format reveal_helpers.open_reveal reads."""
    eph_sk, eph_pk = crypto.gen_keypair()
    key = crypto.derive_session_key(eph_sk, bytes.fromhex(pub_hex), info=rv.REVEAL_INFO)
    sealed = crypto.encrypt(key, json.dumps(obj).encode())
    return {"client_eph_pk": eph_pk.hex(), "nonce": sealed.nonce.hex(), "ct": sealed.ct.hex()}


class World:
    def __init__(self, tmp, engine):
        self.tmp, self.engine = tmp, engine
        self.state = tmp / "state"
        self.state.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "dendrad").write_text(FAKE_DENDRAD)
        os.chmod(self.bin / "dendrad", 0o755)
        os.symlink(sys.executable, self.bin / "python3")
        self.keys = tmp / "keys"
        self.keys.mkdir()
        sk = crypto.gen_keypair()[0]
        crypto.save_sk(sk, str(self.keys / f"{JUDGE}.sk"))
        self.judge_pub = crypto.pub_bytes(sk).hex()
        self.jobs, self.juries, self.commits = [], {}, {}
        self.flaky, self.refuse, self.refs, self.prompts = [], [], {}, {}
        self.marks = {}   # marker -> how many judge prompts carrying it the engine has answered
        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), _Relay)
        self.relay.store, self.relay.gets = {"reveal": {}, "req": {}}, []
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        engine.on_generate = self._generate

    def _seen(self, mark):
        """How many judge prompts carrying `mark` the engine has answered, this one included."""
        self.marks[mark] = self.marks.get(mark, 0) + 1
        return self.marks[mark]

    def _generate(self, model, prompt, opts):
        """The reference model answers the scripted reference; the judge model reads its own templates.
        Markers in an answer script the judge model: GARBAGE is word salad, SHRUG an unreadable coherence read,
        EXPLODE an engine failure, MUMBLE an unreadable SECOND same-fact read (the answer against the first
        reference, once), HAZY an unreadable FIRST multiplicity read (once). "?" is no verdict."""
        if model == REF_MODEL:
            if prompt == "ok":
                return "ready"
            ref = self.refs.get(prompt, "no reference")
            if isinstance(ref, list):     # a scripted sequence: one per generation, the last one repeats
                return ref.pop(0) if len(ref) > 1 else ref[0]
            return ref
        coh = _fill(prompt, COHERENT)
        if coh is not None:
            if "EXPLODE" in coh[0]:
                return 500, {"error": "the engine failed in the middle of a judgment"}
            if "SHRUG" in coh[0]:
                return "?"
            return "NON" if "GARBAGE" in coh[0] else "OUI"
        if _fill(prompt, RELEVANT) is not None:
            return "OUI"
        multi = _fill(prompt, MULTI)
        if multi is not None:
            if "HAZY" in multi[2] and self._seen("HAZY") == 1:
                return "?"
            return "NON"
        same = _fill(prompt, SAME)
        if same is not None:
            if "MUMBLE" in same[1] and self._seen("MUMBLE") == 2:
                return "?"
            if "WOBBLE" in same[1] and self._seen("WOBBLE") == 2:
                return "?"
            return "OUI" if same[0].strip() == same[1].strip() else "NON"
        return "?"

    def audit(self, job, prompt, answer, *, reference=None, jury="seated", max_out=0, dh=10, request=True,
              already=None, shown=None):
        """`shown`: the question the reveal SHOWS, without its salt, while the commit anchors `prompt` -- the
        primary that picks its question after the fact."""
        prim = f"m-prim-{job}"
        salt = crypto.prompt_salt(crypto.gen_keypair()[0], job)
        anchor = M.Miner(prim, backend="ollama")._embed_output(answer)   # the miner's own path
        self.commits[f"{job}__{prim}"] = {"index": f"{job}__{prim}", "promptCommit": crypto.prompt_commitment(salt, prompt),
                                          "result": anchor}
        self.jobs.append({"job_id": job, "state": "open+paid+optimistic+disputed", "miner_id": prim,
                          "dispute_height": str(dh)})
        named = {"seated": [JUDGE, "m-other"], "not-seated": ["m-other", "m-third"]}
        self.juries[job] = named.get(jury, jury) if isinstance(jury, str) else jury
        self.relay.store["reveal"][f"{job}__{JUDGE}__{prim}"] = _seal(
            self.judge_pub, {"prompt": prompt, "answer": answer, "psalt": salt} if shown is None
            else {"prompt": shown, "answer": answer})
        if shown is not None:
            prompt = shown                # the references are generated for the question the judge is shown
        if request:
            self.relay.store["req"][f"{job}__{prim}"] = {"client_eph_pk": "00", "nonce": "00", "ct": "00",
                                                         "max_out": max_out}
        self.refs[prompt] = answer if reference is None else reference
        self.prompts[job] = prompt
        if already is not None:
            self.commits[f"{job}__verdict__{JUDGE}"] = {"index": f"{job}__verdict__{JUDGE}", "result": already}

    def run(self, passes=1, **extra):
        """One process of the worker: `--once` for one pass, `--passes N --retry-pause 0` for several, so an
        audit this judge could not judge is tried again at the very next pass."""
        for name, value in (("jobs.json", self.jobs), ("juries.json", self.juries), ("commits.json", self.commits),
                            ("flaky_reads.json", self.flaky), ("refuse.json", self.refuse)):
            (self.state / name).write_text(json.dumps(value))
        how = ["--once"] if passes == 1 else ["--passes", str(passes), "--retry-pause", "0"]
        env = {"PATH": str(self.bin), "HOME": str(self.tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONUNBUFFERED": "1", "FAKE_STATE": str(self.state), "DENDRA_NODE": "tcp://127.0.0.1:9",
               "DENDRA_CHAIN_ID": "bench-1", "OLLAMA_ENDPOINT": self.engine.url, "OLLAMA_MODEL": REF_MODEL,
               "DENDRA_EMBED_MODE": "backend", "DENDRA_EMBED_API_MODEL": "bench-embed",
               "DENDRA_JUDGE_STATE_FILE": str(self.tmp / "judge-state.json"), "DENDRA_JUDGE_GEN_DELAY": "0",
               "DENDRA_KEYRING_DIR": os.environ["DENDRA_KEYRING_DIR"],
               "DENDRA_KEYRING_PASSPHRASE_FILE": os.environ["DENDRA_KEYRING_PASSPHRASE_FILE"]}
        env.update(extra)
        r = subprocess.run([sys.executable, str(MODEA / "judge_worker.py"), "--id", JUDGE, "--relay",
                            f"http://127.0.0.1:{self.relay.server_address[1]}", "--keydir", str(self.keys),
                            *how, "--poll", "0"],
                           env=env, capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
        self.out = r.stdout + r.stderr
        return r.returncode

    def created(self):
        p = self.state / "created.json"
        return {c["key"].split("__")[0]: c for c in json.loads(p.read_text())} if p.exists() else {}

    def posts(self, job):
        """Every `create-commit` of this judge's verdict on `job`, in order, refused ones included."""
        p = self.state / "created.json"
        made = json.loads(p.read_text()) if p.exists() else []
        return [c for c in made if c["key"] == f"{job}__verdict__{JUDGE}"]

    def jury_reads(self, job):
        """How many times the worker asked the chain for the jury anchored on `job`."""
        p = self.state / "dendrad.log"
        n = 0
        for line in (p.read_text().splitlines() if p.exists() else []):
            a = json.loads(line)
            if a[:3] == ["query", "jobs", "audit-committee"] and "--" in a and a[a.index("--") + 1:] == [job]:
                n += 1
        return n

    def references(self, job):
        return [o for m, p, o in self.engine.generations if m == REF_MODEL and p == self.prompts[job]]

    def fetched(self, job):
        return [g for g in self.relay.gets if g.startswith(f"/reveal/{job}__")]

    def counters(self):
        return json.loads((self.tmp / "judge-state.json").read_text())["counters"]

    def close(self):
        self.relay.shutdown()
        self.relay.server_close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = FakeEngine()
    monkeypatch.setenv("OLLAMA_ENDPOINT", engine.url)
    monkeypatch.setenv("DENDRA_EMBED_API_MODEL", "bench-embed")
    monkeypatch.setattr(M, "_EMBED_MODE", "backend")
    w = World(tmp_path, engine)
    paris = "Paris is the capital of France."
    long_answer = " ".join(f"part{i} of the long answer about rivers and bridges {i}" for i in range(40))
    assert len(long_answer.split()) > CONTEXT_WORDS
    w.audit("jobA", "Question A: the capital of France?", paris, max_out=96, dh=10)
    w.audit("jobB", "Question B: the capital of France?", paris, jury="not-seated", dh=11)
    w.audit("jobC", "Question C: the capital of France?", "Berlin is the capital of France.", reference=paris, dh=12)
    w.audit("jobD", "Question D: the capital of France?", paris, already="1", dh=13)
    w.audit("jobE", "Question E: the capital of France?", "GARBAGE capital the Paris is", reference=paris, dh=14)
    w.audit("jobF", "Question F: the capital of France?", paris, jury="DOWN", dh=15)
    w.audit("jobG", "Question G: the capital of France?", paris, jury="NOTFOUND", dh=16)
    w.audit("jobL", "Question L: tell me about rivers.", long_answer, dh=17)
    w.audit("jobR", "Question R: the capital of France?", paris, request=False, dh=18)
    w.audit("jobK", "Question K: the capital of France?", paris, already="1", dh=19)
    w.flaky.append(f"jobK__verdict__{JUDGE}")
    w.audit("jobQ", "Question Q: the capital of France?", paris, dh=20)
    w.refuse.append(f"jobQ__verdict__{JUDGE}")
    # LISTED LAST, DISPUTED FIRST: judged first, and its judge call fails half-way.
    w.audit("jobX", "Question X: the capital of France?", "EXPLODE Paris is the capital of France.", dh=9)
    yield w
    w.close()
    engine.close()


def test_the_loop_posts_only_what_it_may_and_only_on_its_seats(world):
    rc = world.run()
    out = world.out
    assert rc == 0, out[-3000:]
    assert "divergence guard ON" in out
    made = world.created()
    # what it posted, and the value
    assert {j: (c["result"], c["code"]) for j, c in made.items() if c["code"] == 0} == \
        {"jobA": ("1", 0), "jobE": ("0", 0), "jobF": ("1", 0), "jobL": ("1", 0)}, out[-3000:]
    # what the chain refused, and why it is not the same thing
    assert made["jobK"]["code"] == 18 and made["jobQ"]["code"] == 4
    # what it did NOT post: off its seat, a divergence under the guard, already voted, no jury, no request,
    # a judgment that failed half-way
    for job in ("jobB", "jobC", "jobD", "jobG", "jobR", "jobX"):
        assert job not in made, (job, out[-3000:])


def test_a_judgment_that_fails_abstains_on_that_audit_only_and_the_oldest_comes_first(world):
    assert world.run() == 0, world.out[-3000:]
    judge_calls = [p for m, p, _o in world.engine.generations if m == JUDGE_MODEL]
    assert judge_calls and "EXPLODE" in judge_calls[0], "the oldest dispute (listed last) is judged first"
    assert "ABSTENTION on jobX (stage judge-error)" in world.out
    assert world.created()["jobA"]["result"] == "1", "the audits after the failure were still judged"


def test_off_seat_and_unanchored_audits_are_never_opened(world):
    assert world.run() == 0, world.out[-3000:]
    assert world.fetched("jobB") == [] and world.fetched("jobG") == []
    assert world.references("jobB") == []
    assert world.fetched("jobF"), "a jury that cannot be read is judged anyway"


def test_an_audit_already_voted_on_is_not_judged_again(world):
    assert world.run() == 0, world.out[-3000:]
    assert world.references("jobD") == [], "the own verdict is read BEFORE judging"


def test_every_reference_is_bounded_by_the_audited_request(world):
    assert world.run() == 0, world.out[-3000:]
    caps = [o.get("num_predict") for o in world.references("jobA")]
    assert caps and set(caps) == {96}, caps
    assert world.references("jobR") == [], "an unknown cap is an abstention before any generation"


def test_the_long_answer_is_checked_and_judged(world):
    assert world.run() == 0, world.out[-3000:]
    assert world.created()["jobL"]["result"] == "1"
    assert any(status == 500 for _w, status, _t in world.engine.embed_calls), "the context refusal was met"


def test_the_counters_say_what_happened(world):
    assert world.run() == 0, world.out[-3000:]
    c = world.counters()
    assert c["verdicts_anchored"] == {"0": 1, "1": 3}
    assert c["verdicts_already_on_chain"] == 2
    assert c["verdicts_refused"] == 1 and "unauthorized" in c["last_refusal"]["why"]
    assert c["abstentions"] == {"divergence-held": 1, "judge-error": 1, "request-unreadable": 1}
    assert (c["seats_seen"], c["not_seated"], c["seats_unread"]) == (9, 1, 1)
    assert "VERDICT REFUSED BY THE CHAIN for jobQ" in world.out
    assert "REAL FAILURE" not in world.out


def test_armed_a_divergence_is_voted_invalid(world):
    assert world.run(DENDRA_JUDGE_DIVERGENCE_SLASH="1") == 0, world.out[-3000:]
    made = world.created()
    assert (made["jobC"]["result"], made["jobC"]["code"]) == ("0", 0)
    assert world.counters()["abstentions"] == {"judge-error": 1, "request-unreadable": 1}


def test_a_value_other_than_1_keeps_the_guard(world):
    assert world.run(DENDRA_JUDGE_DIVERGENCE_SLASH="true") == 0, world.out[-3000:]
    assert "jobC" not in world.created()
    assert "is neither 0 nor 1" in world.out


def _engine_world(tmp_path, monkeypatch):
    engine = FakeEngine()
    monkeypatch.setenv("OLLAMA_ENDPOINT", engine.url)
    monkeypatch.setenv("DENDRA_EMBED_API_MODEL", "bench-embed")
    monkeypatch.setattr(M, "_EMBED_MODE", "backend")
    return engine, World(tmp_path, engine)


# ═══ WORD SALAD WITHOUT ITS ENVELOPE ════════════════════════════════════════════════════════════════════════
@pytest.fixture
def salad(tmp_path, monkeypatch):
    """E word salad with its envelope (the control); S word salad whose envelope is missing at the relay; T word
    salad whose envelope carries a cap the miner could not have read; H an honest answer without its envelope;
    V an answer whose coherence read is unreadable, without its envelope."""
    engine, w = _engine_world(tmp_path, monkeypatch)
    paris = "Paris is the capital of France."
    w.audit("jobE", "Question E: the capital of France?", "GARBAGE capital the Paris is", reference=paris, dh=10)
    w.audit("jobS", "Question S: the capital of France?", "GARBAGE France the of capital", reference=paris, dh=11,
            request=False)
    w.audit("jobT", "Question T: the capital of France?", "GARBAGE is Paris capital the", reference=paris, dh=12,
            max_out="abc")
    w.audit("jobH", "Question H: the capital of France?", paris, dh=13, request=False)
    w.audit("jobV", "Question V: the capital of France?", "SHRUG capital the Paris is", reference=paris, dh=14,
            request=False)
    yield w
    w.close()
    engine.close()


def test_word_salad_is_voted_invalid_without_the_requests_envelope(salad):
    assert salad.run() == 0, salad.out[-3000:]
    made = salad.created()
    assert {j: (c["result"], c["code"]) for j, c in made.items()} == \
        {"jobE": ("0", 0), "jobS": ("0", 0), "jobT": ("0", 0)}, salad.out[-3000:]
    for job in ("jobS", "jobT", "jobH", "jobV"):
        assert salad.references(job) == [], (job, "nothing bounded by the cap runs without it")
    c = salad.counters()
    assert c["verdicts_anchored"] == {"0": 3, "1": 0}
    assert c["abstentions"] == {"request-unreadable": 2}
    assert "ABSTENTION on jobV (stage request-unreadable)" in salad.out, "an unreadable read is not word salad"


def test_with_the_coherence_stage_off_a_missing_envelope_abstains(salad):
    assert salad.run(DENDRA_TWOSTAGE="0") == 0, salad.out[-3000:]
    assert salad.created() == {}, salad.out[-3000:]
    assert salad.counters()["abstentions"] == {"divergence-held": 1, "request-unreadable": 4}


# ═══ SEVERAL PASSES OF ONE PROCESS ══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def passes(tmp_path, monkeypatch):
    """A honest; C a divergence held by the guard; G no jury, ever; J no jury at the first read, a seat at the
    second; U a jury unread at the first read, off this seat at the second, its envelope missing; Q a verdict the
    chain refuses at every post; M an unreadable second same-fact read (once); N an unreadable multiplicity
    read (once), then a divergence held by the guard."""
    engine, w = _engine_world(tmp_path, monkeypatch)
    paris = "Paris is the capital of France."
    w.audit("jobA", "Question A: the capital of France?", paris, dh=10)
    w.audit("jobC", "Question C: the capital of France?", "Berlin is the capital of France.", reference=paris, dh=11)
    w.audit("jobG", "Question G: the capital of France?", paris, jury="NOTFOUND", dh=12)
    w.audit("jobJ", "Question J: the capital of France?", paris, jury={"seq": ["NOTFOUND", [JUDGE, "m-other"]]},
            dh=13)
    w.audit("jobU", "Question U: the capital of France?", paris, jury={"seq": ["DOWN", ["m-other", "m-third"]]},
            request=False, dh=14)
    w.audit("jobQ", "Question Q: the capital of France?", paris, dh=15)
    w.refuse.append(f"jobQ__verdict__{JUDGE}")
    w.audit("jobM", "Question M: the capital of France?", "MUMBLE Paris is the capital of France.", dh=16)
    w.audit("jobN", "Question N: the capital of France?", "HAZY Berlin is the capital of France.", reference=paris,
            dh=17)
    yield w
    w.close()
    engine.close()


def test_across_passes_a_judgment_is_given_once_and_a_decided_verdict_is_never_judged_again(passes):
    assert passes.run(passes=3) == 0, passes.out[-3000:]
    # a judgment is final for the process: the divergence held at the first pass is not judged again
    assert len(passes.references("jobC")) == 3, "r1, r2 and the confirming r3, once"
    # a verdict decided and refused is posted at every pass, and judged at the first one only
    assert [c["code"] for c in passes.posts("jobQ")] == [4, 4, 4]
    assert len(passes.references("jobQ")) == 2
    # an audit voted on is left alone
    assert [c["code"] for c in passes.posts("jobA")] == [0] and passes.jury_reads("jobA") == 1


def test_across_passes_what_could_not_be_read_is_read_again(passes):
    assert passes.run(passes=3) == 0, passes.out[-3000:]
    made = passes.created()
    # no jury yet: read again at every pass, never opened, and judged once a jury seats this judge
    assert passes.jury_reads("jobG") == 3 and passes.fetched("jobG") == []
    assert passes.jury_reads("jobJ") == 2 and (made["jobJ"]["result"], made["jobJ"]["code"]) == ("1", 0)
    # an unread jury is judged anyway and read again; off this seat at the second read, the audit is left
    assert passes.jury_reads("jobU") == 2 and len(passes.fetched("jobU")) == 1
    # an unreadable read of the judge model is tried again, as a whole judgment
    assert (made["jobM"]["result"], made["jobM"]["code"]) == ("1", 0)
    assert len(passes.references("jobM")) == 4
    assert "jobN" not in made and len(passes.references("jobN")) == 6


def test_across_passes_the_counters_count_every_attempt_and_print_each_once(passes):
    assert passes.run(passes=3) == 0, passes.out[-3000:]
    c = passes.counters()
    assert c["verdicts_anchored"] == {"0": 0, "1": 3}
    assert c["verdicts_refused"] == 3
    assert c["abstentions"] == {"divergence-held": 2, "multiok-unreadable": 1, "request-unreadable": 1,
                                "sc-unreadable": 1}
    assert passes.out.count("ABSTENTION on jobC (stage divergence-held)") == 1


# ═══ WHAT A CHEAT AND A RETRY CANNOT BUY (relecture adverse du 2026-10-09) ═══════════════════════════════════
@pytest.fixture
def cheat(tmp_path, monkeypatch):
    """P a wrong answer revealed WITHOUT its salt, next to a question it answers right (the commit anchors the real
    one); W two references that agree and a comparison with the answer unreadable once (WOBBLE), then, at the
    retry, a first reference and a second that diverge (the scripted sequence r1 r2 | r1 r2); Z references that
    diverge at the first judgment (the control: a proven ambiguity still votes)."""
    engine, w = _engine_world(tmp_path, monkeypatch)
    w.audit("jobP", "Question P: the capital of Australia?", "Berlin is the capital of Germany.",
            shown="Question P: the capital of Germany?", dh=10)
    w.audit("jobW", "Question W: the capital of France?", "WOBBLE Paris is the capital of France.", dh=11,
            reference=["WOBBLE Paris is the capital of France."] * 3 + ["Lyon is the capital of France."])
    w.audit("jobZ", "Question Z: name a city of France.", "Paris is a city of France.", dh=12,
            reference=["Lyon is a city of France.", "Nice is a city of France."])
    yield w
    w.close()
    engine.close()


def test_a_question_shown_without_its_salt_is_never_graded(cheat):
    assert cheat.run(passes=3) == 0, cheat.out[-3000:]
    assert "jobP" not in cheat.created(), cheat.out[-3000:]
    assert cheat.references("jobP") == [], "no reference generated for a question nobody verified"
    assert "ABSTENTION on jobP (stage prompt-unverified)" in cheat.out
    assert cheat.out.count("ABSTENTION on jobP") == 1, "a final abstention: not judged again"


def test_a_retry_after_agreeing_references_never_votes_on_an_ambiguity(cheat):
    assert cheat.run(passes=3) == 0, cheat.out[-3000:]
    made = cheat.created()
    assert "ABSTENTION on jobW (stage sc-unreadable)" in cheat.out, "the fixture did produce the unreadable read"
    assert "ABSTENTION on jobW (stage sc-diverge)" in cheat.out, "the fixture did produce the divergence at the retry"
    assert "jobW" not in made, "sampling noise at a retry bought a VALID vote: %r" % made.get("jobW")
    # the control: a divergence at the FIRST judgment is a proven ambiguity, and votes VALID (DENDRA_JUDGE_ABSTAIN_VOTE=1)
    assert (made["jobZ"]["result"], made["jobZ"]["code"]) == ("1", 0), cheat.out[-3000:]


def test_the_coherence_of_an_answer_is_read_once_across_retries(cheat):
    assert cheat.run(passes=3) == 0, cheat.out[-3000:]
    coh = [p for m, p, _o in cheat.engine.generations
           if m == JUDGE_MODEL and _fill(p, COHERENT) is not None and "WOBBLE" in _fill(p, COHERENT)[0]]
    assert len(coh) == 1, "the answer of jobW was read for coherence %d times over two attempts" % len(coh)
