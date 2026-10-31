"""The model-registry fields of the reveal markers and the jury verdicts: `--model-id` and `--weights-hash`.

With enforce_model_registry on, x/jobs/keeper/msg_server_commit.go::CreateCommit checks every commit, markers
and verdicts included: a registered model, and the weights digest the registry anchors for it. This bench
defends the kit side, in reveal_worker.py:
  * `read_weights_digest` answers in three states (found, absent, unreadable) against an engine on a real
    socket, and never guesses an endpoint or a digest;
  * on every engine answer it finds the same digest as miner.py::model_weights_hash, the function the
    registry tool registers with, so a marker or a verdict passes the registry exactly when a miner commit
    does; the one place they part is a digest of another shape, which the miner anchors and this reader drops;
  * `ModelAnchor` writes a flag only for a value it read, reads a digest it did not find again after its
    interval, and never again once found;
  * `anchor_marker` and the shipped loop of `main` send those flags on the marker commit.
The verdict side, run as a process, is in tests/test_judge_worker_loop.py (the `weights` cases).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# A mutated copy of the workers can be loaded from elsewhere (DENDRA_MODEA), as in tests/test_judge_worker_loop.py.
MODEA = Path(os.environ.get("DENDRA_MODEA") or HERE.parent)
sys.path.insert(0, str(MODEA))

import reveal_worker as rw  # noqa: E402

DIGEST = "2af3b81862c6be03c769683af18efdadb2c33f60ff32ab6f83e42c043d6c7816"
OTHER = "b" * 64


class _Tags(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        owner = self.server.owner
        owner.gets.append(self.path)
        status, body = owner.answer if self.path == "/api/tags" else (404, b"{}")
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Engine:
    """An engine that answers GET /api/tags with `answer` = (HTTP status, document or raw bytes)."""

    def __init__(self, answer):
        self.answer, self.gets = answer, []
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Tags)
        self._srv.owner = self
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


def tags(*models):
    return 200, {"models": list(models)}


@pytest.fixture
def engine():
    made = []

    def make(answer):
        e = Engine(answer)
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


def closed_port_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Tags)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.server_close()
    return url


# === (1) three states, read on a real socket ==============================================================

FOUND_CASES = [
    ("tagged name", "qwen3:14b", [{"name": "qwen3:14b", "model": "qwen3:14b", "digest": DIGEST}]),
    ("untagged name, listed as latest", "mistral-nemo", [{"name": "mistral-nemo:latest", "digest": DIGEST}]),
    ("matched on the model field", "llama3.1:8b", [{"name": "alias", "model": "llama3.1:8b", "digest": DIGEST}]),
    ("algorithm prefix", "qwen3:14b", [{"name": "qwen3:14b", "digest": "sha256:" + DIGEST}]),
    ("first of two listed", "qwen3:14b", [{"name": "other:1", "digest": OTHER},
                                          {"name": "qwen3:14b", "digest": DIGEST}]),
]


@pytest.mark.parametrize("label,model,listed", FOUND_CASES, ids=[c[0] for c in FOUND_CASES])
def test_a_listed_model_is_found_with_its_digest(engine, label, model, listed):
    e = engine(tags(*listed))
    state, digest, _ = rw.read_weights_digest(model, e.url)
    assert (state, digest) == (rw.DIGEST_FOUND, DIGEST), label


def test_a_model_the_engine_does_not_list_is_absent(engine):
    for listed in ([], [{"name": "qwen3:14b", "digest": DIGEST}]):
        e = engine(tags(*listed))
        state, digest, detail = rw.read_weights_digest("mistral-nemo", e.url)
        assert (state, digest) == (rw.DIGEST_ABSENT, ""), detail
        assert "mistral-nemo:latest is not listed" in detail


UNREADABLE_CASES = [
    ("HTTP 500", (500, {"error": "boom"})),
    ("not JSON", (200, b"<html>no</html>")),
    ("no list of models", (200, {"tags": []})),
    ("models is not a list", (200, {"models": {"name": "qwen3:14b"}})),
    ("listed without a digest", tags({"name": "qwen3:14b"})),
    ("digest of another shape", tags({"name": "qwen3:14b", "digest": "abc"})),
    ("digest that is not a string", tags({"name": "qwen3:14b", "digest": 12})),
]


@pytest.mark.parametrize("label,answer", UNREADABLE_CASES, ids=[c[0] for c in UNREADABLE_CASES])
def test_an_answer_that_cannot_be_used_is_unreadable_never_absent(engine, label, answer):
    e = engine(answer)
    state, digest, _ = rw.read_weights_digest("qwen3:14b", e.url)
    assert (state, digest) == (rw.DIGEST_UNREADABLE, ""), label


def test_no_endpoint_and_no_answer_are_unreadable_and_no_endpoint_is_guessed():
    # The fetch answers the model's digest at any address: a guessed endpoint, even one that points at a live
    # engine, would read it. Without an endpoint nothing is fetched and the state says why.
    asked = []

    def fetch_anything(url):
        asked.append(url)
        return json.dumps({"models": [{"name": "qwen3:14b", "digest": DIGEST}]})

    for ep in ("", "   ", None, "/"):
        assert rw.read_weights_digest("qwen3:14b", ep, fetch=fetch_anything) == (
            rw.DIGEST_UNREADABLE, "", "no engine endpoint is set"), repr(ep)
    assert asked == [], "an address was guessed: %r" % asked
    assert rw.read_weights_digest("qwen3:14b", closed_port_url())[:2] == (rw.DIGEST_UNREADABLE, "")


def test_no_model_named_reads_nothing(engine):
    e = engine(tags({"name": "qwen3:14b", "digest": DIGEST}))
    assert rw.read_weights_digest("", e.url)[:2] == (rw.DIGEST_ABSENT, "")
    assert e.gets == [], "nothing is asked of the engine for a model nobody named"


# === (2) the same digest as the miner's commits ==========================================================

def test_on_every_answer_the_digest_is_the_one_the_miner_anchors(engine, monkeypatch):
    md = pytest.importorskip("miner")
    cases = [(m, (200, {"models": listed})) for _l, m, listed in FOUND_CASES]
    cases += [("mistral-nemo", tags({"name": "qwen3:14b", "digest": DIGEST}))]
    cases += [("qwen3:14b", a) for label, a in UNREADABLE_CASES if label != "digest of another shape"]
    for model, answer in cases:
        e = engine(answer)
        monkeypatch.setattr(md, "MODEL_ID", model)
        monkeypatch.setenv("OLLAMA_ENDPOINT", e.url)
        assert rw.read_weights_digest(model, e.url)[1] == md.model_weights_hash(), (model, answer)
    # The one place they part: the miner anchors a digest of another shape as it reads it, which the chain
    # can never match against a registered SHA-256; this reader writes nothing.
    e = engine(tags({"name": "qwen3:14b", "digest": "abc"}))
    monkeypatch.setattr(md, "MODEL_ID", "qwen3:14b")
    monkeypatch.setenv("OLLAMA_ENDPOINT", e.url)
    assert (md.model_weights_hash(), rw.read_weights_digest("qwen3:14b", e.url)[1]) == ("abc", "")


# === (3) ModelAnchor: a flag only for a value read ==========================================================

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_found_gives_both_flags_and_is_never_read_again(engine):
    e = engine(tags({"name": "qwen3:14b", "digest": DIGEST}))
    clock = Clock()
    a = rw.ModelAnchor("qwen3:14b", e.url, clock=clock, retry_s=300)
    assert a.flags() == ["--model-id", "qwen3:14b", "--weights-hash", DIGEST]
    clock.t += 10_000
    assert a.flags() == ["--model-id", "qwen3:14b", "--weights-hash", DIGEST]
    assert e.gets == ["/api/tags"], "a digest found is not read again"


def test_absent_and_unreadable_give_the_model_alone(engine):
    for answer in (tags(), (500, {})):
        e = engine(answer)
        a = rw.ModelAnchor("qwen3:14b", e.url, clock=Clock())
        assert a.flags() == ["--model-id", "qwen3:14b"]
        assert "without --weights-hash" in a.describe()


def test_no_model_gives_no_flag_and_asks_nothing(engine):
    e = engine(tags({"name": "qwen3:14b", "digest": DIGEST}))
    for model in ("", "  ", None):
        a = rw.ModelAnchor(model, e.url, clock=Clock())
        assert a.flags() == [] and a.refresh() is False
    assert e.gets == []


def test_a_digest_not_found_is_read_again_after_its_interval_only(engine):
    e = engine((500, {}))
    clock = Clock()
    a = rw.ModelAnchor("qwen3:14b", e.url, clock=clock, retry_s=300)
    assert a.flags() == ["--model-id", "qwen3:14b"]
    e.answer = tags({"name": "qwen3:14b", "digest": DIGEST})
    clock.t += 299
    assert a.flags() == ["--model-id", "qwen3:14b"], "read again before the interval"
    assert len(e.gets) == 1
    clock.t += 1
    assert a.flags() == ["--model-id", "qwen3:14b", "--weights-hash", DIGEST]
    assert len(e.gets) == 2


# === (4) the marker commit carries the flags ===============================================================

def test_anchor_marker_sends_the_flags_it_is_given(monkeypatch):
    sent = []
    monkeypatch.setattr(rw.relay, "put", lambda *a: True)
    monkeypatch.setattr(rw, "tx_from", lambda frm, *a, flags=(): sent.append((a, tuple(flags))) or "code: 0")
    fl = ["--model-id", "qwen3:14b", "--weights-hash", DIGEST]
    ok, _ = rw.anchor_marker("m1", "http://relay", 3, ["jA"], flags=fl)
    assert ok and sent[0][1] == tuple(fl)
    assert sent[0][0][0] == "create-commit" and sent[0][0][-1] == rw.rmk.MARKER_KIND


class _Stop(BaseException):
    pass


def _run_main_two_rounds(monkeypatch, tmp_path, env):
    """The shipped loop of reveal_worker.main for two rounds: one job revealed in epoch 0, then the height
    moves into epoch 1 and the marker of epoch 0 is anchored. What the chain was sent is returned."""
    for k in ("DENDRA_MODEL_ID", "OLLAMA_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "m1.sk").write_text("x")
    monkeypatch.setattr(sys, "argv", ["reveal_worker.py", "--id", "m1", "--relay", "http://relay", "--keydir",
                                      str(tmp_path), "--epoch-blocks", "10", "--poll", "0"])

    class KR:
        open_with = None
    monkeypatch.setattr(rw, "_keyring", lambda: KR)
    monkeypatch.setattr(rw.crypto, "load_sk", lambda *a, **k: b"k")
    heights = iter([5, 15, 15, 15])
    monkeypatch.setattr(rw, "current_height", lambda: next(heights))
    rounds = iter([[("job1", "open+paid+optimistic+disputed", "m1")], [], []])
    monkeypatch.setattr(rw, "list_jobs", lambda: next(rounds))
    monkeypatch.setattr(rw, "reveal_disputed_job",
                        lambda *a, **k: rw.Outcome(rw.REVEALED, 1, ("j1",), (), "1 of 1 keyed juror(s)"))
    monkeypatch.setattr(rw.relay, "put", lambda *a: True)
    sent = []
    monkeypatch.setattr(rw, "tx_from", lambda frm, *a, flags=(): sent.append((a, tuple(flags))) or "code: 0")
    sleeps = []

    def sleep(_s):
        sleeps.append(1)
        if len(sleeps) >= 2:
            raise _Stop()
    monkeypatch.setattr(rw.time, "sleep", sleep)
    with pytest.raises(_Stop):
        rw.main()
    return [s for s in sent if s[0][:1] == ("create-commit",)]


def test_the_loop_anchors_the_marker_with_the_miner_model_and_its_digest(engine, monkeypatch, tmp_path, capsys):
    e = engine(tags({"name": "qwen3:14b", "digest": DIGEST}))
    made = _run_main_two_rounds(monkeypatch, tmp_path, {"DENDRA_MODEL_ID": "qwen3:14b", "OLLAMA_ENDPOINT": e.url})
    assert len(made) == 1, made
    args, flags = made[0]
    assert args[1] == rw.rmk.marker_key(0, "m1") and args[-1] == rw.rmk.MARKER_KIND
    assert flags == ("--model-id", "qwen3:14b", "--weights-hash", DIGEST)
    assert "marker model: model 'qwen3:14b', weights digest" in capsys.readouterr().out


def test_the_loop_without_an_endpoint_names_the_model_and_guesses_no_digest(engine, monkeypatch, tmp_path):
    made = _run_main_two_rounds(monkeypatch, tmp_path, {"DENDRA_MODEL_ID": "qwen3:14b"})
    assert [f for _a, f in made] == [("--model-id", "qwen3:14b")]


def test_the_loop_without_a_model_sends_no_flag(engine, monkeypatch, tmp_path):
    e = engine(tags({"name": "qwen3:14b", "digest": DIGEST}))
    made = _run_main_two_rounds(monkeypatch, tmp_path, {"OLLAMA_ENDPOINT": e.url})
    assert [f for _a, f in made] == [()]
    assert e.gets == []
