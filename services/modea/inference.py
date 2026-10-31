"""Inference backends for the miner.

- OllamaBackend: calls the local Ollama instance and reuses the model already installed.
- MockBackend:   deterministic answer with no GPU, for tests and sandboxes.
- ChatCompletionsBackend: any local engine that serves the chat-completions API
  (DENDRA_INFER_BACKEND=chat-completions, DENDRA_INFER_URL, DENDRA_INFER_MODEL, DENDRA_INFER_API_KEY).

Rule: no backend ever logs content. Prompts and outputs live only in process memory. OS hardening —
mlock, sandbox, no egress — is described in the Mode A security note (development repository) and layered on top for the production client.
"""
from __future__ import annotations

import hashlib
import os

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


def ollama_timeout_s() -> int:
    """The bound on one generation (OLLAMA_TIMEOUT, default 600 s). Read in one place, because the
    miner's heartbeat derives how long its loop may stay silent from it (modea/heartbeat.py::max_age_s)."""
    return int(os.environ.get("OLLAMA_TIMEOUT", "600"))


# -- Embedding: three outcomes, not two -----------------------------------------------------------------
# An embedding call ends in one of three ways: a vector for the whole text, a refusal because the text does
# not fit the embedding model's context, or any other failure. The second is the only one a caller can
# recover from, by embedding the text in pieces (modea/miner.py::answer_embedding), so it is kept apart from
# the third. Folded into "no vector", a long answer would never be committed, with the same message as an
# engine that is down.
EMBED_TOO_LONG = "the text exceeds the embedding model's context"

# Match patterns against the engines' own error text, not messages of ours. Ollama's /api/embeddings answers
# HTTP 500 "the input length exceeds the context length" (measured on Ollama 0.32.1 with nomic-embed-text);
# the other two are how other local engines word the same refusal. A pattern
# that matched an unrelated failure would split a text the engine could have embedded whole -- and the
# juror, whose engine answers, would then compare two different computations and abstain: the cost of a
# wrong match is a lost verdict, never a slash.
_CONTEXT_REFUSAL_HINTS = ("exceeds the context length", "maximum context length",
                          "exceeds the available context size", "input is too large to process")


def context_refusal(body) -> bool:
    """Does an engine's error text say the input does not fit the model's context?"""
    t = str(body or "").lower()
    return any(h in t for h in _CONTEXT_REFUSAL_HINTS)


def _embed_post(url, payload, headers=None):
    """POST one embedding request -> (json document, None) | (None, EMBED_TOO_LONG) | (None, why).

    The body of a refusal is read before it is classified: the length refusal and every other refusal share
    the same status code on Ollama (500), and only the engine's text tells them apart."""
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=120)
    except Exception as e:  # noqa: BLE001 -- nothing answered: say so, never guess
        return None, f"the embedding engine did not answer ({type(e).__name__})"
    try:
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 -- the engine answered and refused: its text says why
        body = str(getattr(r, "text", "") or "")
        if context_refusal(body):
            return None, EMBED_TOO_LONG
        return None, f"the embedding engine refused ({type(e).__name__}: {' '.join(body.split())[:160]})"
    try:
        return r.json(), None
    except Exception as e:  # noqa: BLE001
        return None, f"the embedding engine's answer is not JSON ({type(e).__name__})"


class MockBackend:
    name = "mock"

    def generate(self, prompt: str, max_out: int = 0, temperature=None, timeout_s=None) -> str:
        # `temperature` and `timeout_s` are accepted for API compatibility but ignored: the mock stays
        # deterministic and answers at once.
        # Deterministic answer, nothing logged.
        h = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        out = f"[mock:{h}] simulated answer to the request (length {len(prompt)})."
        self.in_tok = max(1, len(prompt) // 4)   # estimate: there is no real model behind the mock
        self.out_tok = max(1, len(out) // 4)
        return out


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str = "", endpoint: str = ""):
        # Configurable through the environment: OLLAMA_ENDPOINT / OLLAMA_MODEL.
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")
        self.endpoint = endpoint or os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434")

    def generate(self, prompt: str, max_out: int = 0, temperature=None, timeout_s=None) -> str:
        """`timeout_s` bounds this call (a test inference, miner.presence_probe); without it the bound
        is ollama_timeout_s(), the one the heartbeat derives its staleness from."""
        if requests is None:
            raise RuntimeError("the requests module is required for OllamaBackend")
        # Output cap: requested by the client (max_out), otherwise the default; hard upper bound against abuse.
        np = max_out or int(os.environ.get("OLLAMA_NUM_PREDICT", "2048"))
        np = min(max(64, np), int(os.environ.get("OLLAMA_NUM_PREDICT_MAX", "8192")))
        # Serving (default, temperature=None): deterministic decoding (temp 0, top_k 1, seed 0), because
        # served work must be reproducible. Sampling (a juror probing ambiguity, temperature>0): the
        # temperature is set and top_k/seed are removed — keeping them would make the K draws identical, so
        # the ambiguity would never surface.
        if temperature is None:
            opts = {"temperature": 0.0, "top_k": 1, "seed": 0, "num_predict": np}
        else:
            opts = {"temperature": float(temperature), "num_predict": np}
        payload = {
            "model": self.model, "prompt": prompt, "stream": False,
            "options": opts,
        }
        # On a machine whose miner serves the judge model (OLLAMA_ENDPOINT is the judge's engine), this call
        # reaches the same engine as the verdicts: it sends the same runner options (none: memwatch.engine_options),
        # or the engine would reload the whole model at every turn of callers, and the keep_alive of the judge's
        # mode, or each request would put the engine's own timer back (modea/memwatch.py). A call while the judge model
        # is not loaded would load it: it first passes the check of the room for it, and is held back when that check
        # is closed (memwatch.Held), whoever calls: the daemon, the self-test, the application's machine test.
        from modea import memwatch
        if memwatch.is_judge_engine(self.endpoint):
            memwatch.hold_call(self.endpoint, self.model)
            opts.update(memwatch.engine_options())
            payload["keep_alive"] = memwatch.keepalive_for_call()
        # OLLAMA_TIMEOUT: a fixed 600 s means a juror whose CPU-bound Ollama saturates blocks for 10 min per
        # generation, cascading into a missed quorum. Configurable, with the default unchanged; the bench kit
        # sets about 240 s for jurors — a retry or an abstention beats a stall.
        r = requests.post(f"{self.endpoint}/api/generate", json=payload,
                          timeout=(timeout_s if timeout_s else ollama_timeout_s()))
        try:
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 -- the engine's own text says why (out of memory, model absent)
            body = " ".join(str(getattr(r, "text", "") or "").split())[:200]
            if not body or not isinstance(e, requests.HTTPError):
                raise
            raise requests.HTTPError(f"{e} (the engine said: {body})", response=getattr(e, "response", r)) from e
        j = r.json()
        # The model's real token counts, so pricing is exact to the token.
        self.in_tok = int(j.get("prompt_eval_count", 0)) or max(1, len(prompt) // 4)
        self.out_tok = int(j.get("eval_count", 0)) or max(1, len(j.get("response", "")) // 4)
        return j.get("response", "")

    def embed_checked(self, text: str):
        """Embedding through Ollama /api/embeddings (dedicated model DENDRA_EMBED_API_MODEL).
        -> (vector, None) for the whole text | (None, EMBED_TOO_LONG) | (None, why)."""
        if requests is None:
            return None, "the requests module is required for an embedding"
        model = os.environ.get("DENDRA_EMBED_API_MODEL", "")
        if not model:
            return None, "DENDRA_EMBED_API_MODEL is not set"
        doc, why = _embed_post(f"{self.endpoint}/api/embeddings", {"model": model, "prompt": text})
        if doc is None:
            return None, why
        vec = doc.get("embedding") if isinstance(doc, dict) else None
        return (vec, None) if vec else (None, "the embedding engine returned no vector")

    def embed(self, text: str):
        """The vector of `embed_checked`, or None for any failure (a length refusal included): the
        historical contract, kept for the callers that only ask whether the engine embeds at all."""
        return self.embed_checked(text)[0]


class ChatCompletionsBackend:
    """Backend for any local engine that serves the chat-completions API (/v1/chat/completions).
    It unifies chat (/v1/chat/completions) and embeddings (/v1/embeddings) behind one endpoint, which
    simplifies the miner. Backends are pluggable, so no single engine is a dependency. Nothing is logged."""
    name = "chat-completions"

    def __init__(self, model: str = "", endpoint: str = "", embed_model: str = ""):
        self.endpoint = (endpoint or os.environ.get("DENDRA_INFER_URL")
                         or "http://localhost:8080/v1").rstrip("/")
        self.model = (model or os.environ.get("DENDRA_INFER_MODEL")
                      or "llama3.1:8b-instruct-q4_K_M")
        self.embed_model = embed_model or os.environ.get("DENDRA_EMBED_API_MODEL", "")
        self.api_key = os.environ.get("DENDRA_INFER_API_KEY", "")  # usually empty for a local engine

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def generate(self, prompt: str, max_out: int = 0, temperature=None, timeout_s=None) -> str:
        if requests is None:
            raise RuntimeError("the requests module is required for ChatCompletionsBackend")
        np = max_out or int(os.environ.get("OLLAMA_NUM_PREDICT", "2048"))
        np = min(max(64, np), int(os.environ.get("OLLAMA_NUM_PREDICT_MAX", "8192")))
        # Same rule as OllamaBackend: the default is deterministic serving; temperature>0 means sampling
        # with no fixed seed, otherwise the K draws would be identical.
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "top_p": 1.0, "max_tokens": np, "stream": False,
        }
        if temperature is None:
            payload["temperature"] = 0.0
            payload["seed"] = 0
        else:
            payload["temperature"] = float(temperature)
        r = requests.post(f"{self.endpoint}/chat/completions", json=payload,
                          headers=self._headers(), timeout=(timeout_s if timeout_s else 600))
        r.raise_for_status()
        j = r.json()
        usage = j.get("usage", {}) or {}
        choices = j.get("choices") or [{}]
        txt = ((choices[0].get("message") or {}).get("content", "")) or ""
        self.in_tok = int(usage.get("prompt_tokens", 0)) or max(1, len(prompt) // 4)
        self.out_tok = int(usage.get("completion_tokens", 0)) or max(1, len(txt) // 4)
        return txt

    def embed_checked(self, text: str):
        """Embedding through the same endpoint (/v1/embeddings), unifying chat and embeddings.
        -> (vector, None) for the whole text | (None, EMBED_TOO_LONG) | (None, why)."""
        if requests is None:
            return None, "the requests module is required for an embedding"
        if not self.embed_model:
            return None, "DENDRA_EMBED_API_MODEL is not set"
        doc, why = _embed_post(f"{self.endpoint}/embeddings", {"model": self.embed_model, "input": text},
                               headers=self._headers())
        if doc is None:
            return None, why
        data = doc.get("data") if isinstance(doc, dict) else None
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        vec = first.get("embedding")
        return (vec, None) if vec else (None, "the embedding engine returned no vector")

    def embed(self, text: str):
        """The vector of `embed_checked`, or None for any failure (a length refusal included): the
        historical contract, kept for the callers that only ask whether the engine embeds at all."""
        return self.embed_checked(text)[0]


# Every engine that serves the chat-completions API speaks the same protocol; only DENDRA_INFER_URL differs.
CHAT_COMPLETIONS = "chat-completions"


def get_backend(name: str = ""):
    """Pluggable backend factory. An empty name reads DENDRA_INFER_BACKEND (default 'ollama')."""
    name = (name or os.environ.get("DENDRA_INFER_BACKEND", "ollama")).lower()
    if name == "mock":
        return MockBackend()
    if name == "ollama":
        return OllamaBackend()
    if name == CHAT_COMPLETIONS:
        return ChatCompletionsBackend()
    raise ValueError(f"unknown backend: {name!r}")
