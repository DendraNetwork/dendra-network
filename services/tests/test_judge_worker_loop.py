"""The shipped loop, run for real: `judge_worker.py --once`, as a process, against a fake chain, relay and engine.

Why a process. Every guard of the judge can be right in its own function and still be skipped by the loop that
calls it -- a correct cosine function that its loop feeds the wrong embedding lets no vote through, and no
bench of the function alone can see it. So this bench starts the worker the way the kit does and reads what
it did: the transactions its `dendrad` received, the reveals it fetched, the references its engine was asked
for, and the counters it wrote. Nothing in the worker is patched.

The fakes, and what each one stands for:
  * `dendrad` -- a script on a hermetic PATH (its directory and a python3 link, nothing else): the job list,
    the anchored juries (one may change between two reads: {"seq": [...]}), the commits, and `create-commit`
    with the chain's refusals ("index already set", an unauthorized signer); a success at block prints no
    `code:` line, as proto3 omits a zero;
  * the relay -- sealed reveals and request envelopes over HTTP, every GET recorded;
  * the engine -- tests/test_judge_anchor_embedding.py::FakeEngine: 768-number embeddings, the Ollama length
    refusal, references, and the judge model's YES/NO read from the worker's own prompt templates, with an
    unreadable answer where a test asks for one.
The primaries' anchors are made by `Miner._embed_output` through the same engine: the miner's real path.

The audits of one pass (one each): A honest with a request cap of 96; B not on this judge's jury; C a
divergent answer; D already voted; E word salad; F a jury that cannot be read; G no jury anchored; L an answer
longer than the engine's context; R a request envelope missing; K a verdict already on chain that the read
did not see; Q a verdict the chain refuses; X listed last but disputed first, whose judge call fails half-way.

Word salad without its envelope (`salad`, one pass): the coherence stage needs no request cap, so a missing or
unreadable envelope does not switch it off.

Several passes of one process (`passes`, `--passes 3 --retry-pause 0`): what the judge keeps between passes
lives in its memory only, so it is measured in one process: a judgment is given once, a decided verdict is
posted again and never judged again, an audit without a jury is read again until one is anchored, an unread
jury is read again, and an unreadable read of the judge model is tried again.

What a cheat and a retry cannot buy (`cheat`, three passes): a question shown without its salt is never graded; a
retried audit whose references once agreed does not vote valid on a divergence sampled at the retry, while a
divergence at the first judgment still does; and an answer's coherence is read once across the retries.

The weights digest (`weights`, one pass): every verdict names the judge model and carries the digest the engine
that serves the verdict calls lists for it, and no digest when that engine gives none.
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
# The root can be pointed elsewhere (DENDRA_MODEA), as in tests/test_relay_startup_alarm.py: a mutated copy of the worker is
# loaded and run from there, never this file's own tree. Here only finds this bench's sibling fakes.
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
    # closes_after.json: a job listed as disputed for its first k reads, then closed
    n = load("list_reads.json", 0) + 1
    save("list_reads.json", n)
    closes = load("closes_after.json", {})
    jobs = [dict(j, state="closed") if 0 < closes.get(j["job_id"], 0) < n else j for j in load("jobs.json", [])]
    print(json.dumps({"job": jobs, "pagination": {}}))
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
    worker's own prompts and reads the data out of them, so a template that changes cannot go unnoticed."""
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


GIB = 1024 ** 3


def _meminfo(available_mb, active_file_mb=2048, total_mb=65536):
    """A /proc/meminfo as the kernel writes it (kB)."""
    rows = [("MemTotal", total_mb), ("MemFree", available_mb // 2), ("MemAvailable", available_mb),
            ("Active(file)", active_file_mb), ("SwapTotal", 8192), ("SwapFree", 8192)]
    return "".join(f"{k}: {v * 1024:>12} kB" + chr(10) for k, v in rows)


def _serve_get(monkeypatch):
    """The fake engines answer GET /api/ps with their `ps` list (the models loaded, 500 when it is None), and
    /api/tags with their `tags` list or 404 when they have none."""
    import test_judge_anchor_embedding as tae

    def do_get(self):
        owner = self.server.owner
        listed = getattr(owner, "tags", None)
        if self.path == "/api/ps" and getattr(owner, "ps", []) is None:
            code, doc = 500, {"error": "the engine failed"}
        elif self.path == "/api/ps":
            code, doc = 200, {"models": getattr(owner, "ps", [])}
        elif self.path == "/api/tags" and listed is not None:
            code, doc = 200, {"models": listed}
        else:
            code, doc = 404, {"error": "route"}
        out = json.dumps(doc).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    monkeypatch.setattr(tae._Handler, "do_GET", do_get, raising=False)


class World:
    def __init__(self, tmp, engine):
        self.tmp, self.engine = tmp, engine
        # The judge model is loaded unless a case says otherwise: the memory check (judge_worker.JudgeMemoryGuard)
        # then passes without a load, and the cases below judge as they did before it existed.
        engine.ps = [{"name": JUDGE_MODEL, "model": JUDGE_MODEL, "size": 19 * GIB}]
        self.proc = tmp / "proc"
        (self.proc / "proc").mkdir(parents=True)
        (self.proc / "proc" / "meminfo").write_text(_meminfo(48000))
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
        self.closes = {}  # job_id -> list reads after which the chain lists it closed
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
        EXPLODE an engine failure, MUMBLE an unreadable second same-fact read (the answer against the first
        reference, once), HAZY an unreadable first multiplicity read (once). "?" is no verdict."""
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
            return "NO" if "GARBAGE" in coh[0] else "YES"
        if _fill(prompt, RELEVANT) is not None:
            return "YES"
        multi = _fill(prompt, MULTI)
        if multi is not None:
            if "HAZY" in multi[2] and self._seen("HAZY") == 1:
                return "?"
            return "NO"
        same = _fill(prompt, SAME)
        if same is not None:
            if "MUMBLE" in same[1] and self._seen("MUMBLE") == 2:
                return "?"
            if "WOBBLE" in same[1] and self._seen("WOBBLE") == 2:
                return "?"
            return "YES" if same[0].strip() == same[1].strip() else "NO"
        return "?"

    def audit(self, job, prompt, answer, *, reference=None, jury="seated", max_out=0, dh=10, request=True,
              already=None, shown=None):
        """`shown`: the question the reveal shows, without its salt, while the commit anchors `prompt` -- the
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
                            ("flaky_reads.json", self.flaky), ("refuse.json", self.refuse),
                            ("closes_after.json", self.closes), ("list_reads.json", 0)):
            (self.state / name).write_text(json.dumps(value))
        how = ["--once"] if passes == 1 else ["--passes", str(passes), "--retry-pause", "0"]
        env = {"PATH": str(self.bin), "HOME": str(self.tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONUNBUFFERED": "1", "FAKE_STATE": str(self.state), "DENDRA_NODE": "tcp://127.0.0.1:9",
               "DENDRA_CHAIN_ID": "bench-1", "OLLAMA_ENDPOINT": self.engine.url, "OLLAMA_MODEL": REF_MODEL,
               "DENDRA_EMBED_MODE": "backend", "DENDRA_EMBED_API_MODEL": "bench-embed",
               "DENDRA_JUDGE_STATE_FILE": str(self.tmp / "judge-state.json"), "DENDRA_JUDGE_GEN_DELAY": "0",
               "DENDRA_STATUS_FILE": str(self.tmp / "daemon-status.json"),
               "DENDRA_MEMWATCH_PROC_ROOT": str(self.proc),
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
    _serve_get(monkeypatch)
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
    # Listed last, disputed first: judged first, and its judge call fails half-way.
    w.audit("jobX", "Question X: the capital of France?", "EXPLODE Paris is the capital of France.", dh=9)
    yield w
    w.close()
    engine.close()


def test_the_loop_posts_only_what_it_may_and_only_on_its_seats(world):
    rc = world.run()
    out = world.out
    assert rc == 0, out[-3000:]
    assert "divergence guard on" in out
    made = world.created()
    # what it posted, and the value
    assert {j: (c["result"], c["code"]) for j, c in made.items() if c["code"] == 0} == \
        {"jobA": ("1", 0), "jobE": ("0", 0), "jobF": ("1", 0), "jobL": ("1", 0)}, out[-3000:]
    # what the chain refused, and why it is not the same thing
    assert made["jobK"]["code"] == 18 and made["jobQ"]["code"] == 4
    # what it did not post: off its seat, a divergence under the guard, already voted, no jury, no request,
    # a judgment that failed half-way
    for job in ("jobB", "jobC", "jobD", "jobG", "jobR", "jobX"):
        assert job not in made, (job, out[-3000:])


def test_a_judgment_that_fails_abstains_on_that_audit_only_and_the_oldest_comes_first(world):
    assert world.run() == 0, world.out[-3000:]
    judge_calls = [p for m, p, _o in world.engine.generations if m == JUDGE_MODEL]
    assert judge_calls and "EXPLODE" in judge_calls[0], "the oldest dispute (listed last) is judged first"
    assert "abstention on jobX (stage judge-error)" in world.out
    assert world.created()["jobA"]["result"] == "1", "the audits after the failure were still judged"


def test_off_seat_and_unanchored_audits_are_never_opened(world):
    assert world.run() == 0, world.out[-3000:]
    assert world.fetched("jobB") == [] and world.fetched("jobG") == []
    assert world.references("jobB") == []
    assert world.fetched("jobF"), "a jury that cannot be read is judged anyway"


def test_an_audit_already_voted_on_is_not_judged_again(world):
    assert world.run() == 0, world.out[-3000:]
    assert world.references("jobD") == [], "the own verdict is read before judging"


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
    assert c["abstention_attempts"] == {"divergence-held": 1, "judge-error": 1, "request-unreadable": 1}
    assert c["abstentions"] == {"divergence-held": 1}, "the audits still open and tried again count once over"
    assert (c["seats_seen"], c["not_seated"], c["seats_unread"]) == (9, 1, 1)
    assert "verdict refused by the chain for jobQ" in world.out
    assert "disputes will not close" not in world.out


def test_armed_a_divergence_is_voted_invalid(world):
    assert world.run(DENDRA_JUDGE_DIVERGENCE_SLASH="1") == 0, world.out[-3000:]
    made = world.created()
    assert (made["jobC"]["result"], made["jobC"]["code"]) == ("0", 0)
    assert world.counters()["abstention_attempts"] == {"judge-error": 1, "request-unreadable": 1}


def test_a_value_other_than_1_keeps_the_guard(world):
    assert world.run(DENDRA_JUDGE_DIVERGENCE_SLASH="true") == 0, world.out[-3000:]
    assert "jobC" not in world.created()
    assert "is neither 0 nor 1" in world.out


def _engine_world(tmp_path, monkeypatch):
    _serve_get(monkeypatch)
    engine = FakeEngine()
    monkeypatch.setenv("OLLAMA_ENDPOINT", engine.url)
    monkeypatch.setenv("DENDRA_EMBED_API_MODEL", "bench-embed")
    monkeypatch.setattr(M, "_EMBED_MODE", "backend")
    return engine, World(tmp_path, engine)


# Word salad without its envelope.
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
    assert c["abstention_attempts"] == {"request-unreadable": 2}
    assert "abstention on jobV (stage request-unreadable)" in salad.out, "an unreadable read is not word salad"


def test_with_the_coherence_stage_off_a_missing_envelope_abstains(salad):
    assert salad.run(DENDRA_TWOSTAGE="0") == 0, salad.out[-3000:]
    assert salad.created() == {}, salad.out[-3000:]
    assert salad.counters()["abstention_attempts"] == {"divergence-held": 1, "request-unreadable": 4}


# Several passes of one process.
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
    assert c["abstention_attempts"] == {"divergence-held": 2, "multiok-unreadable": 1, "request-unreadable": 1,
                                        "sc-unreadable": 1}
    # jobM, unreadable once then judged, and jobU, off this seat at the second read, left nothing without an answer
    assert c["abstentions"] == {"divergence-held": 2}
    assert passes.out.count("abstention on jobC (stage divergence-held)") == 1


# What a cheat and a retry cannot buy.
@pytest.fixture
def cheat(tmp_path, monkeypatch):
    """P a wrong answer revealed without its salt, next to a question it answers right (the commit anchors the real
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
    assert "abstention on jobP (stage prompt-unverified)" in cheat.out
    assert cheat.out.count("abstention on jobP") == 1, "a final abstention: not judged again"


def test_a_retry_after_agreeing_references_never_votes_on_an_ambiguity(cheat):
    assert cheat.run(passes=3) == 0, cheat.out[-3000:]
    made = cheat.created()
    assert "abstention on jobW (stage sc-unreadable)" in cheat.out, "the fixture did produce the unreadable read"
    assert "abstention on jobW (stage sc-diverge)" in cheat.out, "the fixture did produce the divergence at the retry"
    assert "jobW" not in made, "sampling noise at a retry bought a valid vote: %r" % made.get("jobW")
    # the control: a divergence at the first judgment is a proven ambiguity, and votes valid (DENDRA_JUDGE_ABSTAIN_VOTE=1)
    assert (made["jobZ"]["result"], made["jobZ"]["code"]) == ("1", 0), cheat.out[-3000:]


def test_the_coherence_of_an_answer_is_read_once_across_retries(cheat):
    assert cheat.run(passes=3) == 0, cheat.out[-3000:]
    coh = [p for m, p, _o in cheat.engine.generations
           if m == JUDGE_MODEL and _fill(p, COHERENT) is not None and "WOBBLE" in _fill(p, COHERENT)[0]]
    assert len(coh) == 1, "the answer of jobW was read for coherence %d times over two attempts" % len(coh)


# === weights: each verdict names the judge model and carries its weights digest ===========================
# x/jobs/keeper/msg_server_commit.go::CreateCommit, with enforce_model_registry on, refuses a verdict whose
# model holds a weights anchor and that carries no weights_hash, or another one. The worker reads the digest
# by reveal_worker.py::read_weights_digest on the engine that serves its verdict calls
# (modea/judge.py::judge_endpoint), and writes the flag only for a digest it found.
JUDGE_WEIGHTS = "9d" * 32
REF_WEIGHTS = "4e" * 32


@pytest.fixture
def served_tags(monkeypatch):
    """The fake engines answer GET /api/tags with their `tags` list, or 404 when they have none (and /api/ps)."""
    _serve_get(monkeypatch)


def verdict_flags(world):
    """{flag: value} of the flags of every verdict `create-commit` the worker sent, refused ones included."""
    p = world.state / "dendrad.log"
    out = []
    for line in (p.read_text().splitlines() if p.exists() else []):
        a = json.loads(line)
        if a[:3] != ["tx", "jobs", "create-commit"] or "--" not in a:
            continue
        cut = a.index("--")
        if a[cut + 1:cut + 5][3:] != ["verdict"]:
            continue
        head = a[:cut]
        out.append({f: head[head.index(f) + 1] for f in ("--model-id", "--weights-hash") if f in head})
    return out


def test_weights_a_digest_the_engine_cannot_give_is_not_written(world, served_tags):
    assert world.run() == 0, world.out[-3000:]
    made = verdict_flags(world)
    assert len(made) == 6, made
    assert made == [{"--model-id": JUDGE_MODEL}] * len(made), made
    assert "verdict model: model 'qwen-judge', weights digest unreadable" in world.out


def test_weights_every_verdict_carries_the_digest_of_the_judge_model(world, served_tags):
    world.engine.tags = [{"name": REF_MODEL, "digest": REF_WEIGHTS}, {"name": JUDGE_MODEL, "digest": JUDGE_WEIGHTS}]
    assert world.run() == 0, world.out[-3000:]
    made = verdict_flags(world)
    assert len(made) == 6, made
    assert made == [{"--model-id": JUDGE_MODEL, "--weights-hash": JUDGE_WEIGHTS}] * len(made), made


def test_weights_the_digest_is_read_where_the_verdicts_are_served(world, served_tags):
    judge_engine = FakeEngine()
    try:
        judge_engine.ps = world.engine.ps
        judge_engine.on_generate = world._generate
        judge_engine.tags = [{"name": JUDGE_MODEL, "digest": JUDGE_WEIGHTS}]
        world.engine.tags = [{"name": JUDGE_MODEL, "digest": REF_WEIGHTS}]
        assert world.run(DENDRA_JUDGE_ENDPOINT=judge_engine.url) == 0, world.out[-3000:]
        made = verdict_flags(world)
        assert made and all(m.get("--weights-hash") == JUDGE_WEIGHTS for m in made), made
    finally:
        judge_engine.close()


def test_weights_the_kit_model_does_not_replace_the_model_that_judges(world, served_tags):
    # The kit names DENDRA_JUDGE_MODEL_ID and the engine holds it, while the chain pins the model every judge
    # call uses (the fake dendrad answers audit_judge_model = qwen-judge). The verdict names the model that
    # judged and carries its digest, never the kit's.
    kit_model, kit_weights = "kit-judge", "7c" * 32
    world.engine.tags = [{"name": kit_model, "digest": kit_weights}, {"name": JUDGE_MODEL, "digest": JUDGE_WEIGHTS}]
    assert world.run(DENDRA_JUDGE_MODEL_ID=kit_model) == 0, world.out[-3000:]
    made = verdict_flags(world)
    assert len(made) == 6, made
    assert made == [{"--model-id": JUDGE_MODEL, "--weights-hash": JUDGE_WEIGHTS}] * len(made), made
    assert {m for m, _p, _o in world.engine.generations} <= {JUDGE_MODEL, REF_MODEL}, "the kit model judged"
    assert "DENDRA_JUDGE_MODEL_ID is 'kit-judge' but the judge model is 'qwen-judge'" in world.out


def test_weights_the_no_reveal_verdict_carries_the_digest_too(tmp_path, monkeypatch, served_tags):
    # The second place a verdict is posted: a "0" on an absent reveal, reached when the abstention is disarmed.
    engine, w = _engine_world(tmp_path, monkeypatch)
    try:
        engine.tags = [{"name": JUDGE_MODEL, "digest": JUDGE_WEIGHTS}]
        w.audit("jobN", "Question N: the capital of France?", "Paris is the capital of France.", dh=10)
        w.relay.store["reveal"].clear()
        assert w.run(DENDRA_JUDGE_NOREVEAL_ABSTAIN="0") == 0, w.out[-3000:]
        assert w.posts("jobN") and w.posts("jobN")[0]["result"] == "0", w.out[-3000:]
        assert verdict_flags(w) == [{"--model-id": JUDGE_MODEL, "--weights-hash": JUDGE_WEIGHTS}], w.out[-3000:]
    finally:
        w.close()
        engine.close()


# === memory: the judge model is loaded only when the machine has room for it (judge_worker.JudgeMemoryGuard) ====
# The engine checks no free memory before it loads a model on the CPU (ollama 0.34.4), so a juror that judged on a
# machine without room would push it into swap. These cases run the shipped loop with the judge model not loaded
# (/api/ps lists nothing), a fabricated /proc/meminfo, and the model's size listed by /api/tags.
def _bodies(engine):
    """Every POST body the engine received, in order (the keep_alive a load or a call sent)."""
    seen = []
    orig = engine.answer

    def answer(path, body):
        seen.append((path, body))
        return orig(path, body)
    engine.answer = answer
    return seen


# The engine's metadata of the judge model (/api/show): 48 layers, 4 KV heads of 128, a 384 MiB context cache at 4096
# tokens and one slot (tests/test_memwatch.py::QWEN3_MOE_INFO), so the footprint before a load is 19 456 + 384 MiB.
JUDGE_INFO = {"general.architecture": "qwen3moe", "qwen3moe.block_count": 48, "qwen3moe.attention.head_count_kv": 4,
              "qwen3moe.attention.key_length": 128, "qwen3moe.attention.value_length": 128}


def _show_model_info(engine):
    orig = engine.answer

    def answer(path, body):
        if path == "/api/show":
            return 200, {"model_info": JUDGE_INFO}
        return orig(path, body)
    engine.answer = answer


@pytest.fixture
def memory(tmp_path, monkeypatch):
    engine, w = _engine_world(tmp_path, monkeypatch)
    engine.ps = []
    engine.tags = [{"name": JUDGE_MODEL, "size": 19 * GIB}, {"name": REF_MODEL, "size": 5 * GIB}]
    _show_model_info(engine)
    w.bodies = _bodies(engine)
    w.audit("jobA", "Question A: the capital of France?", "Paris is the capital of France.", dh=10)
    yield w
    w.close()
    engine.close()


def _loads(w):
    return [b for p, b in w.bodies if p == "/api/generate" and b.get("model") == JUDGE_MODEL and b.get("prompt") == ""]


def test_memory_without_room_the_audit_abstains_and_nothing_is_loaded(memory):
    # 12 000 MiB available, 2 048 of warm cache: 9 952 MiB of room for a 19 456 MiB model.
    (memory.proc / "proc" / "meminfo").write_text(_meminfo(12000))
    assert memory.run(passes=3) == 0, memory.out[-3000:]
    assert [m for m, _p, _o in memory.engine.generations if m == JUDGE_MODEL] == [], "the judge model was called"
    assert "jobA" not in memory.created()
    assert memory.counters()["abstention_attempts"] == {"memory-guard": 3}, "tried again at every pass"
    assert memory.out.count("abstention on jobA (stage memory-guard)") == 1, "printed once"
    assert "9952 MiB free beside the warm cache" in memory.out and "needs 19840 MiB" in memory.out


def test_memory_with_room_the_model_is_loaded_first_with_the_mode_keep_alive(memory):
    assert memory.run() == 0, memory.out[-3000:]
    loads = _loads(memory)
    assert len(loads) == 1, loads
    judge_calls = [b for p, b in memory.bodies if p == "/api/generate" and b.get("model") == JUDGE_MODEL]
    assert judge_calls[0] is loads[0], "the load comes before any judge call"
    # No daemon heartbeat: the juror measures for itself and loads in on-demand mode (600 s, OLLAMA_TIMEOUT unset).
    assert {b["keep_alive"] for b in judge_calls} == {"600s"}, judge_calls
    assert (memory.created()["jobA"]["result"], memory.created()["jobA"]["code"]) == ("1", 0), memory.out[-3000:]
    ll = memory.counters()["last_load"]
    assert ll["available_before_mb"] == 48000 and ll["available_after_mb"] == 48000 and ll["load_s"] is not None


def test_memory_the_daemons_resident_decision_sets_the_keep_alive(memory):
    import time as _t
    (memory.tmp / "daemon-status.json").write_text(json.dumps({"judge_memory": {
        "at": int(_t.time()), "mode": "resident", "footprint_mb": 20000, "pause_until": None}}))
    assert memory.run() == 0, memory.out[-3000:]
    judge_calls = [b for p, b in memory.bodies if p == "/api/generate" and b.get("model") == JUDGE_MODEL]
    assert judge_calls and {b["keep_alive"] for b in judge_calls} == {"1h"}, judge_calls


def test_memory_the_daemons_footprint_and_pause_close_the_gate(memory):
    import time as _t
    now = int(_t.time())
    (memory.tmp / "daemon-status.json").write_text(json.dumps({"judge_memory": {
        "at": now, "mode": "on-demand", "footprint_mb": 47000, "footprint_source": "engine-ps",
        "pause_until": None}}))
    assert memory.run() == 0, memory.out[-3000:]
    assert _loads(memory) == [] and "needs 47000 MiB (the daemon's decision" in memory.out, memory.out[-3000:]
    (memory.tmp / "daemon-status.json").write_text(json.dumps({"judge_memory": {
        "at": now, "mode": "on-demand", "footprint_mb": 100, "footprint_source": "engine-ps",
        "pause_until": now + 600}}))
    assert memory.run() == 0, memory.out[-3000:]
    assert _loads(memory) == [] and "paused after a memory shortage" in memory.out


def test_memory_an_out_of_memory_load_is_counted_and_not_tried_again_during_its_pause(memory):
    plain = memory._generate

    def killed(model, prompt, opts):
        if model == JUDGE_MODEL and prompt == "":
            return 500, {"error": "llama runner process has terminated: signal: killed"}
        return plain(model, prompt, opts)
    memory.engine.on_generate = killed
    assert memory.run(passes=3) == 0, memory.out[-3000:]
    assert len(_loads(memory)) == 1, "a killed load is tried once per pause, not at every pass"
    c = memory.counters()
    assert c["oom_seen"] == 1 and c["abstention_attempts"] == {"memory-guard": 3}, c
    assert "ran out of memory loading the judge model" in memory.out


def test_memory_an_unread_meminfo_is_never_a_pass(memory):
    (memory.proc / "proc" / "meminfo").unlink()
    assert memory.run() == 0, memory.out[-3000:]
    assert _loads(memory) == [] and "MemAvailable not read" in memory.out


def test_memory_a_judge_call_killed_for_memory_mid_audit_is_the_memory_guards(memory):
    # The model is loaded (no load needed), and the engine is killed under it at the first judge call.
    memory.engine.ps = [{"name": JUDGE_MODEL, "model": JUDGE_MODEL, "size": 19 * GIB}]
    plain = memory._generate

    def killed(model, prompt, opts):
        if model == JUDGE_MODEL and prompt:
            return 500, {"error": "llama runner process has terminated: signal: killed"}
        return plain(model, prompt, opts)
    memory.engine.on_generate = killed
    assert memory.run() == 0, memory.out[-3000:]
    c = memory.counters()
    assert c["abstention_attempts"] == {"memory-guard": 1} and c["oom_seen"] == 1, c


def test_memory_during_a_pause_no_judge_call_reloads_the_model(memory):
    import time as _t
    # The daemon unloaded the model and pauses; the juror's check passes (the model shows loaded at the check,
    # then the daemon's unload lands): the next judge call is held, not sent.
    now = int(_t.time())
    (memory.tmp / "daemon-status.json").write_text(json.dumps({"judge_memory": {
        "at": now, "mode": "on-demand", "footprint_mb": 100, "pause_until": now + 600}}))
    memory.engine.ps = [{"name": JUDGE_MODEL, "model": JUDGE_MODEL, "size": 19 * GIB}]
    plain = memory._generate
    calls = []

    def unloaded_after_first(model, prompt, opts):
        calls.append(model)
        if model == JUDGE_MODEL:
            memory.engine.ps = []
        return plain(model, prompt, opts)
    memory.engine.on_generate = unloaded_after_first
    assert memory.run() == 0, memory.out[-3000:]
    assert calls.count(JUDGE_MODEL) == 1, calls
    assert memory.counters()["abstention_attempts"] == {"memory-guard": 1}
    assert "Held" in memory.out and "paused after a memory shortage" in memory.out


def test_memory_after_the_pause_a_judge_call_mid_audit_still_passes_the_check(memory):
    # The model leaves after the first judge call and no pause runs (it ended, or the engine's own keep_alive let
    # the model go): the next call would reload 19 GB, so it passes the check, which this machine fails.
    (memory.proc / "proc" / "meminfo").write_text(_meminfo(12000))
    memory.engine.ps = [{"name": JUDGE_MODEL, "model": JUDGE_MODEL, "size": 19 * GIB}]
    plain = memory._generate
    calls = []

    def unloaded_after_first(model, prompt, opts):
        calls.append(model)
        if model == JUDGE_MODEL:
            memory.engine.ps = []
        return plain(model, prompt, opts)
    memory.engine.on_generate = unloaded_after_first
    assert memory.run() == 0, memory.out[-3000:]
    assert calls.count(JUDGE_MODEL) == 1, calls
    assert memory.counters()["abstention_attempts"] == {"memory-guard": 1}
    assert "needs 19840 MiB" in memory.out, memory.out[-3000:]


def test_memory_an_audit_left_without_an_answer_counts_once_when_it_is_over(memory):
    (memory.proc / "proc" / "meminfo").write_text(_meminfo(12000))
    memory.closes["jobA"] = 3   # disputed for three passes, then closed by the others
    assert memory.run(passes=4) == 0, memory.out[-3000:]
    c = memory.counters()
    assert c["abstention_attempts"] == {"memory-guard": 3}, c
    assert c["abstentions"] == {"memory-guard": 1}, "one audit left out for lack of memory, not three attempts"


def test_memory_settings_written_wrong_and_an_unread_engine_are_their_own_stages(memory):
    assert memory.run(DENDRA_MEMWATCH_PERIOD_S="0.000001") == 0, memory.out[-3000:]
    assert memory.counters()["abstention_attempts"] == {"memory-settings": 1}, memory.counters()
    assert "WARNING: the judge's memory settings are written wrong" in memory.out
    assert "DENDRA_MEMWATCH_PERIOD_S" in memory.out and _loads(memory) == []
    memory.engine.ps = None   # /api/ps fails: the loaded models are not read
    assert memory.run() == 0, memory.out[-3000:]
    assert memory.counters()["abstention_attempts"] == {"judge-engine-unread": 1}, memory.counters()
