"""THE JUDGE RECOMPUTES THE ANCHORED EMBEDDING EXACTLY AS THE MINER MADE IT -- through the real engine path.

WHY THIS BENCH EXISTS. A juror used to compare the embedding the primary had anchored (384 numbers, from its
engine) with one it computed another way (64 numbers, word hashing). The lengths never matched, every revealed
audit ended in an abstention, and no test noticed: the only seam that could have, the judge's embedding call,
was INJECTED in the one function that used it, and nothing ever ran the shipped one.

So nothing is injected here. A fake Ollama ANSWERS OVER HTTP the way the real one does -- 768-dimensional
vectors on /api/embeddings, and HTTP 500 "the input length exceeds the context length" past its context --
and both the miner's anchor and the juror's recomputation go through `modea.inference.OllamaBackend` and
`modea.miner.answer_embedding` to reach it. The only thing patched is the process's own DENDRA_EMBED_MODE,
which is read once at import.

What is NOT covered here: the real model's numbers (measured separately: same function, GPU against CPU, same
engine version, cosine above 0.99999), and an engine that silently truncates instead of refusing (it would
make the two sides compute differently, which abstains -- see modea/miner.py).
"""
import hashlib
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import judge_worker as jw  # noqa: E402
from modea import inference  # noqa: E402
from modea import miner as M  # noqa: E402

CONTEXT_WORDS = 120          # the fake engine's context, in words: past it, the Ollama refusal
DIMS = 768                   # nomic-embed-text's width; the chain keeps the first 384
OLLAMA_LENGTH_REFUSAL = {"error": "the input length exceeds the context length"}


def engine_vector(text):
    """The fake engine's embedding: deterministic, 768 floats, unit norm, close for texts sharing words."""
    acc = [0.0] * DIMS
    for w in text.lower().split():
        h = hashlib.sha256(w.encode("utf-8")).digest()
        for k in range(4):
            idx = int.from_bytes(h[2 * k:2 * k + 2], "big") % DIMS
            acc[idx] += 1.0 if h[8 + k] & 1 else -1.0
    n = math.sqrt(sum(x * x for x in acc))
    return [x / n for x in acc] if n else []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        code, doc = self.server.owner.answer(self.path, body)
        out = json.dumps(doc).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class FakeEngine:
    """An Ollama-shaped engine on a real socket. `embed_calls` records (words, status) per embedding request;
    `generations` records (model, prompt, options) per /api/generate. `fail_embed(text)` may return an
    (HTTP status, document) to answer instead of a vector; `on_generate(model, prompt, options)` answers
    /api/generate (the loop bench sets it)."""

    def __init__(self):
        self.embed_calls, self.generations = [], []
        self.fail_embed = None
        self.on_generate = None
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.owner = self
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._lock = threading.Lock()

    def answer(self, path, body):
        if path == "/api/embeddings":
            text = str(body.get("prompt", ""))
            words = len(text.split())
            forced = self.fail_embed(text) if self.fail_embed else None
            if forced:
                status, doc = forced
            elif words > CONTEXT_WORDS:
                status, doc = 500, OLLAMA_LENGTH_REFUSAL
            else:
                status, doc = 200, {"embedding": engine_vector(text)}
            with self._lock:
                self.embed_calls.append((words, status, text))
            return status, doc
        if path == "/api/generate":
            model, prompt, opts = body.get("model", ""), str(body.get("prompt", "")), body.get("options") or {}
            with self._lock:
                self.generations.append((model, prompt, opts))
            if self.on_generate:
                said = self.on_generate(model, prompt, opts)
                if isinstance(said, tuple):          # (HTTP status, document): an engine that fails
                    return said
                return 200, {"response": said, "eval_count": 3, "prompt_eval_count": 7}
            return 200, {"response": "ready", "eval_count": 3, "prompt_eval_count": 7}
        return 404, {"error": "route"}

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def engine(monkeypatch):
    e = FakeEngine()
    monkeypatch.setenv("OLLAMA_ENDPOINT", e.url)
    monkeypatch.setenv("DENDRA_EMBED_API_MODEL", "bench-embed")
    monkeypatch.setattr(M, "_EMBED_MODE", "backend")   # the compose's setting, read once at import
    yield e
    e.close()


ANSWER = ("Paris is the capital of France and its largest city, home to the national government and to "
          "about two million inhabitants.")
LONG = " ".join(f"sentence{i % 37} about the river number {i} and the bridge {i * 7}" for i in range(60))


def _root_for(job, prim, anchor):
    return lambda k: anchor if k == f"{job}__{prim}" else ""


# ═══ (1) THE DEFECT, CLOSED: the juror's number is the miner's number ═══════════════════════════════════════
def test_the_judge_recomputes_the_miners_anchor_through_the_real_engine(engine):
    anchor = M.Miner("m-prim", backend="ollama")._embed_output(ANSWER)
    assert len(anchor.split(",")) == 384, "the chain keeps 384 of the engine's 768 numbers"
    before = len(engine.embed_calls)
    cos, stage, why = jw.anchor_reading("job1", "m-prim", ANSWER, backend=inference.OllamaBackend(),
                                        get_root=_root_for("job1", "m-prim", anchor))
    assert stage == "" and cos is not None, why
    assert cos >= jw.ANCHOR_IDENTITY_COS and abs(cos - 1.0) < 1e-12
    assert len(engine.embed_calls) == before + 1, "the juror must ask ITS engine, not a local function"
    assert jw.reveal_matches_anchor("job1", "m-prim", ANSWER, backend=inference.OllamaBackend(),
                                    get_root=_root_for("job1", "m-prim", anchor)) == cos


def test_the_old_word_hashing_could_never_meet_an_engine_anchor(engine, monkeypatch):
    """The defect's own shape, kept as a witness: a juror whose setting differs from the miner's gets a
    vector of another length, and that is an ABSTENTION, never a cosine."""
    anchor = M.Miner("m-prim", backend="ollama")._embed_output(ANSWER)
    assert len(M._embed(ANSWER).split(",")) == 64 != len(anchor.split(","))
    monkeypatch.setattr(M, "_EMBED_MODE", "hash")      # this juror's OWN (different) setting
    cos, stage, why = jw.anchor_reading("job1", "m-prim", ANSWER, backend=inference.OllamaBackend(),
                                        get_root=_root_for("job1", "m-prim", anchor))
    assert cos is None and stage == "anchor-shape" and "64" in why and "384" in why


def test_the_anchors_shape_never_picks_the_method(engine):
    """A 64-number anchor (the shape word hashing makes) does not make a backend-mode juror hash: the
    anchor is supplied by the audited party, and a method picked from it would be picked by them."""
    hashed_anchor = M._feature_embed(ANSWER)
    before = len(engine.embed_calls)
    cos, stage, _ = jw.anchor_reading("job1", "m-prim", ANSWER, backend=inference.OllamaBackend(),
                                      get_root=_root_for("job1", "m-prim", hashed_anchor))
    assert cos is None and stage == "anchor-shape"
    assert len(engine.embed_calls) == before + 1, "the juror computed its own way, through its engine"


def test_a_different_answer_abstains_before_anything_is_generated(engine):
    anchor = M.Miner("m-prim", backend="ollama")._embed_output(ANSWER)
    other = "Berlin hosts the parliament of Germany, while Madrid lies in the centre of Spain."
    verdict, stage, why, _tr = jw.judge_revealed(
        "job1", "m-prim", {"prompt": "What is the capital of France?", "answer": other},
        backend=inference.OllamaBackend(), judge_model="qwen-judge", max_out=0,
        get_root=_root_for("job1", "m-prim", anchor), get_fields=lambda k: {})
    assert verdict is None and stage == "anchor-mismatch", why
    assert engine.generations == [], "no reference is generated for an answer nobody anchored"


def test_an_absent_anchor_abstains_and_is_retried(engine):
    cos, stage, _ = jw.anchor_reading("job1", "m-prim", ANSWER, backend=inference.OllamaBackend(),
                                      get_root=lambda k: "")
    assert cos is None and stage == "anchor-unreadable" and stage in jw.RETRY_STAGES


# ═══ (2) A TEXT THAT FITS: the historical computation, to the byte ══════════════════════════════════════════
def test_a_text_that_fits_keeps_the_historical_computation(engine):
    got = M.answer_embedding(ANSWER, inference.OllamaBackend())
    assert got == M._quantize_embed(engine_vector(ANSWER)), "every anchor already on chain must stay valid"
    assert [s for _w, s, _t in engine.embed_calls] == [200]


# ═══ (3) A TEXT PAST THE CONTEXT: split, deterministically, identically on both sides ══════════════════════
def test_a_long_answer_is_split_and_both_sides_agree(engine):
    assert len(LONG.split()) > CONTEXT_WORDS
    anchor = M.Miner("m-prim", backend="ollama")._embed_output(LONG)
    statuses = [s for _w, s, _t in engine.embed_calls]
    assert statuses[0] == 500 and statuses.count(200) >= 2, statuses
    assert all(w <= CONTEXT_WORDS for w, s, _t in engine.embed_calls if s == 200)
    assert len(anchor.split(",")) == 384
    cos, stage, why = jw.anchor_reading("jobL", "m-prim", LONG, backend=inference.OllamaBackend(),
                                        get_root=_root_for("jobL", "m-prim", anchor))
    assert stage == "" and abs(cos - 1.0) < 1e-12, why


def test_the_pieces_cover_the_text_and_are_weighted_by_length(engine):
    pieces = M._engine_pieces(LONG, inference.OllamaBackend().embed_checked, [M.EMBED_MAX_CALLS])
    assert "".join(text for _w, status, text in engine.embed_calls if status == 200) == LONG
    assert sum(w for w, _v in pieces) == len(LONG)
    mean = M._weighted_mean(pieces)
    total = float(len(LONG))
    i = 5
    assert mean[i] == sum(w * v[i] for w, v in pieces) / total


def test_a_changed_figure_in_a_long_answer_stays_above_the_bar(engine):
    """Why the cosine is NOT a proof of identity: one changed figure in a long answer is still above the
    bar (measured on the real model at 0.99998). The gate stops a different answer, not a corrected detail."""
    anchor = M.Miner("m-prim", backend="ollama")._embed_output(LONG)
    edited = LONG.replace("bridge 7 ", "bridge 8 ", 1)
    assert edited != LONG
    cos, stage, _ = jw.anchor_reading("jobL", "m-prim", edited, backend=inference.OllamaBackend(),
                                      get_root=_root_for("jobL", "m-prim", anchor))
    assert stage == "" and jw.ANCHOR_IDENTITY_COS <= cos < 1.0


def test_the_split_point_is_deterministic_and_ascii():
    assert M.embed_split_point("aaaa bbbb") == 4
    assert M.embed_split_point("abcdefgh") == 4                    # no whitespace: the middle
    assert M.embed_split_point("ab cd ef") == 5                    # the space NEAREST the middle (4)
    assert M.embed_split_point("ab c de") == 2                     # a tie at equal distance: the left one
    nbsp = chr(160)
    assert M.embed_split_point("a" + nbsp + "bcdefgh") == 4         # a non-ASCII space is not a cut point
    s = "x" * 10 + " " + "y" * 3
    assert M.embed_split_point(s) == 10
    for t in ("ab", "a b", "abc def ghi", LONG):
        j = M.embed_split_point(t)
        assert 0 < j < len(t)


def test_a_failure_that_is_not_length_is_never_split(engine):
    engine.fail_embed = lambda text: (500, {"error": "model 'bench-embed' not found"})
    with pytest.raises(RuntimeError, match="REFUSING to commit"):
        M.answer_embedding(LONG, inference.OllamaBackend())
    assert len(engine.embed_calls) == 1


def test_a_piece_that_fails_fails_the_whole_answer(engine):
    marker = LONG.split()[-1]
    engine.fail_embed = lambda text: ((500, {"error": "boom"}) if len(text.split()) <= CONTEXT_WORDS
                                      and marker in text.split() else None)
    with pytest.raises(RuntimeError, match="REFUSING to commit"):
        M.answer_embedding(LONG, inference.OllamaBackend())


def test_an_engine_that_refuses_everything_stops_at_its_budget(engine):
    engine.fail_embed = lambda text: (500, OLLAMA_LENGTH_REFUSAL)
    with pytest.raises(RuntimeError):
        M.answer_embedding(LONG, inference.OllamaBackend())
    assert len(engine.embed_calls) <= M.EMBED_MAX_CALLS


def test_embed_keeps_its_historical_contract(engine):
    b = inference.OllamaBackend()
    assert b.embed(LONG) is None
    assert b.embed_checked(LONG) == (None, inference.EMBED_TOO_LONG)
    v = b.embed(ANSWER)
    assert isinstance(v, list) and len(v) == DIMS
    engine.fail_embed = lambda text: (500, {"error": "boom"})
    vec, why = b.embed_checked(ANSWER)
    assert vec is None and why != inference.EMBED_TOO_LONG and "boom" in why


@pytest.mark.parametrize("text,expected", [
    ('{"error":"the input length exceeds the context length"}', True),
    ("This model's maximum context length is 512 tokens. However, you requested 900 tokens", True),
    ("the request exceeds the available context size, try increasing it", True),
    ("input is too large to process. increase the physical batch size", True),
    ('{"error":"model x not found, try pulling it first"}', False),
    ("", False),
    (None, False),
])
def test_the_length_refusal_is_read_from_the_engines_own_text(text, expected):
    assert inference.context_refusal(text) is expected


def test_the_hash_mode_is_unchanged(monkeypatch):
    monkeypatch.setattr(M, "_EMBED_MODE", "hash")
    assert M.answer_embedding(ANSWER) == M._feature_embed(ANSWER)
    assert M.Miner("m1", backend="mock")._embed_output(ANSWER) == M._feature_embed(ANSWER)


def test_an_engine_that_does_not_answer_is_a_refusal_to_commit(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("DENDRA_EMBED_API_MODEL", "bench-embed")
    monkeypatch.setattr(M, "_EMBED_MODE", "backend")
    with pytest.raises(RuntimeError, match="REFUSING to commit"):
        M.Miner("m1", backend="ollama")._embed_output(ANSWER)
    cos, stage, _ = jw.anchor_reading("j", "p", ANSWER, backend=inference.OllamaBackend(),
                                      get_root=lambda k: M._quantize_embed(engine_vector(ANSWER)))
    assert cos is None and stage == "anchor-unreadable"
