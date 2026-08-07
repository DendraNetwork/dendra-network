"""Inference backends for the miner.

- OllamaBackend: calls the local Ollama instance and reuses the model already installed.
- MockBackend:   deterministic answer with no GPU, for tests and sandboxes.

RULE: no backend ever **logs content**. Prompts and outputs live only in process memory. OS hardening —
mlock, sandbox, no egress — is described in MODE-A-SECURITE and layered on top for the production client.
"""
from __future__ import annotations

import hashlib
import os

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


class MockBackend:
    name = "mock"

    def generate(self, prompt: str, max_out: int = 0, temperature=None) -> str:
        # `temperature` is accepted for API compatibility but IGNORED: the mock stays deterministic.
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

    def generate(self, prompt: str, max_out: int = 0, temperature=None) -> str:
        if requests is None:
            raise RuntimeError("the requests module is required for OllamaBackend")
        # Output cap: requested by the client (max_out), otherwise the default; hard upper bound against abuse.
        np = max_out or int(os.environ.get("OLLAMA_NUM_PREDICT", "2048"))
        np = min(max(64, np), int(os.environ.get("OLLAMA_NUM_PREDICT_MAX", "8192")))
        # SERVING (default, temperature=None): DETERMINISTIC decoding (temp 0, top_k 1, seed 0), because
        # served work must be reproducible. SAMPLING (a juror probing ambiguity, temperature>0): the
        # temperature is set AND top_k/seed are REMOVED — keeping them would make the K draws identical, so
        # the ambiguity would never surface.
        if temperature is None:
            opts = {"temperature": 0.0, "top_k": 1, "seed": 0, "num_predict": np}
        else:
            opts = {"temperature": float(temperature), "num_predict": np}
        payload = {
            "model": self.model, "prompt": prompt, "stream": False,
            "options": opts,
        }
        # OLLAMA_TIMEOUT: a fixed 600 s means a juror whose CPU-bound Ollama saturates blocks for 10 min PER
        # generation, cascading into a missed quorum. Configurable, with the default UNCHANGED; the bench kit
        # sets about 240 s for jurors — a retry or an abstention beats a stall.
        r = requests.post(f"{self.endpoint}/api/generate", json=payload,
                          timeout=int(os.environ.get("OLLAMA_TIMEOUT", "600")))
        r.raise_for_status()
        j = r.json()
        # The model's REAL token counts, so pricing is exact to the token.
        self.in_tok = int(j.get("prompt_eval_count", 0)) or max(1, len(prompt) // 4)
        self.out_tok = int(j.get("eval_count", 0)) or max(1, len(j.get("response", "")) // 4)
        return j.get("response", "")

    def embed(self, text: str):
        """Embedding through Ollama /api/embeddings (dedicated model DENDRA_EMBED_API_MODEL).
        Returns None to fall back to the local embedder."""
        if requests is None:
            return None
        model = os.environ.get("DENDRA_EMBED_API_MODEL", "")
        if not model:
            return None
        try:
            r = requests.post(f"{self.endpoint}/api/embeddings",
                              json={"model": model, "prompt": text}, timeout=120)
            r.raise_for_status()
            return r.json().get("embedding")
        except Exception:
            return None


class OpenAIBackend:
    """OpenAI-compatible backend: covers LocalAI, vLLM, llama.cpp (llama-server), LM Studio, TGI and others.
    It unifies chat (/v1/chat/completions) AND embeddings (/v1/embeddings) behind ONE endpoint, which
    simplifies the miner. Backends are PLUGGABLE, so no single engine is a dependency. Nothing is logged."""
    name = "openai"

    def __init__(self, model: str = "", endpoint: str = "", embed_model: str = ""):
        self.endpoint = (endpoint or os.environ.get("DENDRA_INFER_URL")
                         or os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
        self.model = (model or os.environ.get("DENDRA_INFER_MODEL")
                      or os.environ.get("OPENAI_MODEL", "llama3.1:8b-instruct-q4_K_M"))
        self.embed_model = embed_model or os.environ.get("DENDRA_EMBED_API_MODEL", "")
        self.api_key = os.environ.get("OPENAI_API_KEY", "")  # usually empty for a local engine

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def generate(self, prompt: str, max_out: int = 0, temperature=None) -> str:
        if requests is None:
            raise RuntimeError("the requests module is required for OpenAIBackend")
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
                          headers=self._headers(), timeout=600)
        r.raise_for_status()
        j = r.json()
        usage = j.get("usage", {}) or {}
        choices = j.get("choices") or [{}]
        txt = ((choices[0].get("message") or {}).get("content", "")) or ""
        self.in_tok = int(usage.get("prompt_tokens", 0)) or max(1, len(prompt) // 4)
        self.out_tok = int(usage.get("completion_tokens", 0)) or max(1, len(txt) // 4)
        return txt

    def embed(self, text: str):
        """Embedding through the SAME endpoint (/v1/embeddings), unifying chat and embeddings.
        Returns None to fall back to the local embedder."""
        if requests is None or not self.embed_model:
            return None
        try:
            r = requests.post(f"{self.endpoint}/embeddings",
                              json={"model": self.embed_model, "input": text},
                              headers=self._headers(), timeout=120)
            r.raise_for_status()
            data = r.json().get("data") or [{}]
            return data[0].get("embedding")
        except Exception:
            return None


# OpenAI-compatible backends: same protocol, different endpoints and ports depending on the engine.
_OPENAI_ALIASES = {"openai", "localai", "vllm", "llamacpp", "llama.cpp", "lmstudio", "tgi", "sglang"}


def get_backend(name: str = ""):
    """PLUGGABLE backend factory. An empty name reads DENDRA_INFER_BACKEND (default 'ollama')."""
    name = (name or os.environ.get("DENDRA_INFER_BACKEND", "ollama")).lower()
    if name == "mock":
        return MockBackend()
    if name == "ollama":
        return OllamaBackend()
    if name in _OPENAI_ALIASES:
        return OpenAIBackend()
    raise ValueError(f"unknown backend: {name!r}")
